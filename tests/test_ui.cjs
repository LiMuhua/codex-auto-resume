// No browser/account access. Execute the actual UI script with a minimal DOM to
// deterministically exercise overlapping requests; visual checks remain separate.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const DEFAULT_CONTINUATION = '默认续跑内容：继续核对进度。';
class Element {
  constructor() { this.children=[];this.listeners={};this.attrs={};this.dataset={};this.value='';this.placeholder='';this._text='';this.hidden=false;this.open=false;this.checked=false; }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return this._text + this.children.map(c=>c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text='';this.children=children; }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener(name,fn) { (this.listeners[name]??=[]).push(fn); }
  emit(name) { for(const fn of this.listeners[name]??[]) fn({preventDefault(){}}); }
  querySelectorAll(selector) { const all=this.children.flatMap(c=>[c,...c.querySelectorAll('*')]);return selector==='*'?all:all.filter(c=>c.className?.split(' ').includes(selector.slice(1))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
  showModal() { this.open=true; }
  scrollIntoView() { this.scrolled=true; }
  focus() { this.focused=true; }
  close() { this.open=false;this.emit('close'); }
}
function harness(shared = {}) {
  const elements=new Map(), pending=[];
  const storage=shared.storage || new Map();
  const session=shared.session || new Map();
  const locks=shared.locks || {held:false};
  const replaced=shared.replaced || [];
  const el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  // login() looks its submit button up inside the form; keep the synthetic
  // DOM shaped like index.html so token-fragment auto-login can run.
  const loginButton=el('login-button');loginButton.className='utton';el('login-form').append(loginButton);
  el('continuation').placeholder=DEFAULT_CONTINUATION;
  const context=vm.createContext({
    document:{getElementById:el,createElement:()=>new Element(),addEventListener(){},hidden:false},
    sessionStorage:{getItem(key){return session.has(key)?session.get(key):null;},setItem(key,value){session.set(key,String(value));},removeItem(key){session.delete(key);}},
    localStorage:{getItem(key){return storage.get(key)||null;},setItem(key,value){storage.set(key,value);},removeItem(key){storage.delete(key);}},
    navigator:{locks:{async request(name,options,callback){(locks.calls??=[]).push({name,options});if(locks.held)return callback(null);locks.held=true;try{return await callback({name});}finally{locks.held=false;}}}},
    window:{addEventListener(){}},
    location:{hash:shared.hash||'',pathname:'/',protocol:'http:',host:'127.0.0.1:8765'},history:{replaceState(...args){replaced.push(args);}},
    URLSearchParams,console,setInterval(){},setTimeout(...args){const timer=setTimeout(...args);timer.unref();return timer;},clearTimeout,AbortController,
    fixture:[{id:A,title:'Task A'},{id:B,title:'Task B'}],
    fetch(url,options) { return new Promise((resolve,reject)=>pending.push({url,options,reject,reply(result,status=200){resolve({ok:status===200,status,json:async()=>result});}})); }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../codex_resume/static/app.js'),'utf8'),context);
  const run=code=>vm.runInContext(code,context);
  run('threads=fixture; renderThreads();'); el('task-dialog').open=true;
  return {el,pending,run,storage,session,replaced,locks,choose(id){el('thread-id').value=id;el('thread-id').emit('input');},result(id,decision='wait'){return {threadId:id,decision,title:id===A?'Task A':'Task B',reason:'test',model:'test-model',taskState:decision==='stop'?'idle':'running',canMonitor:decision!=='stop',source:'live'};}};
}

test('click selects exactly one row and synchronizes the UUID and title',async()=>{
  const h=harness(); const rows=h.el('thread-options').children;
  rows[0].emit('click');
  assert.equal(h.el('thread-id').value,A);
  assert.equal(h.el('selected-title').textContent,'Task A');
  assert.equal(rows[0].attrs['aria-pressed'],'true');
  assert.equal(rows[1].attrs['aria-pressed'],'false');
  assert.equal(rows[0].querySelector('.selection-marker').hidden,false);
  rows[1].emit('click');
  assert.equal(h.el('thread-id').value,B);
  assert.equal(h.el('selected-title').textContent,'Task B');
  assert.equal(rows[0].attrs['aria-pressed'],'false');
  assert.equal(rows[1].attrs['aria-pressed'],'true');
});

test('late failure for A cannot overwrite successful check for B',async()=>{
  const h=harness();h.choose(A);const old=h.run('checkTask()');
  h.choose(B);const current=h.run('checkTask()');
  h.pending[1].reply(h.result(B));await current;
  h.pending[0].reply({error:'old owner missing'},409);await old;
  assert.equal(h.el('start-form').hidden,false);
  assert.equal(h.el('action-error').textContent,'');
  assert.match(h.el('check-result').textContent,/Task B/);
  assert.doesNotMatch(h.el('check-result').textContent,/old owner missing/);
});

test('A to B to A ignores the first A response even though UUID matches again',async()=>{
  const h=harness();h.choose(A);const first=h.run('checkTask()');
  h.choose(B);h.choose(A);const latest=h.run('checkTask()');
  h.pending[1].reply(h.result(A,'stop'));await latest;
  h.pending[0].reply(h.result(A,'wait'));await first;
  assert.equal(h.el('start-form').hidden,true);
  assert.equal(h.run('checked.decision'),'stop');
});

test('an older finally cannot enable the button while the next check is pending',async()=>{
  const h=harness();h.choose(A);const first=h.run('checkTask()');
  h.choose(B);const next=h.run('checkTask()');
  h.pending[0].reply(h.result(A));await first;
  assert.equal(h.el('check-button').disabled,true);
  h.pending[1].reply(h.result(B));await next;
  assert.equal(h.el('check-button').disabled,false);
});

test('closing the picker invalidates the pending response',async()=>{
  const h=harness();h.choose(A);const request=h.run('checkTask()');
  h.el('task-dialog').close();
  h.pending[0].reply(h.result(A));await request;
  assert.equal(h.el('start-form').hidden,true);
  assert.equal(h.el('check-result').hidden,true);
  assert.equal(h.run('checked'),null);
});

test('failure identifies selected task and gives recovery guidance without enabling start',async()=>{
  const h=harness();h.choose(A);const request=h.run('checkTask()');
  h.pending[0].reply({error:'App owner unavailable'},409);await request;
  assert.match(h.el('check-result').textContent,/Task A/);
  assert.match(h.el('check-result').textContent,/历史/);
  assert.match(h.el('check-result').textContent,/没有开启监控或发送消息/);
  assert.equal(h.el('start-form').hidden,true);
});

test('unknown decision and mismatched thread response cannot enable start',async()=>{
  for(const result of [{threadId:A,decision:'unknown'},{threadId:B,decision:'wait'}]){
    const h=harness();h.choose(A);const request=h.run('checkTask()');
    h.pending[0].reply(result);await request;
    assert.equal(h.el('start-form').hidden,true);
  }
});

test('manual UUID edits update selection and invalidate prior successful check',async()=>{
  const h=harness();h.choose(A);const request=h.run('checkTask()');
  h.pending[0].reply(h.result(A));await request;
  h.choose(B.toUpperCase());
  assert.equal(h.el('selected-title').textContent,'Task B');
  assert.equal(h.run('selectedId()'),B);
  assert.equal(h.el('start-form').hidden,true);
  assert.equal(h.run('checked'),null);
});

test('only running or quota-limited selected tasks can be enrolled',async()=>{
  for(const taskState of ['idle','empty','needs_attention','interrupted','other_failure','unknown']){
    const h=harness();h.choose(A);const request=h.run('checkTask()');
    h.pending[0].reply({...h.result(A),taskState,canMonitor:false});await request;
    assert.equal(h.el('start-form').hidden,true,taskState);
  }
  for(const taskState of ['running','quota_limited']){
    const h=harness();h.choose(A);const request=h.run('checkTask()');
    h.pending[0].reply({...h.result(A),taskState,source:'history',connection:'waiting'});await request;
    assert.equal(h.el('start-form').hidden,false);
    assert.match(h.el('check-result').textContent,/重新核验实时状态/);
  }
});

test('decision alone without explicit eligibility cannot enable start',async()=>{
  const h=harness();h.choose(A);const request=h.run('checkTask()');
  h.pending[0].reply({threadId:A,decision:'wait',taskState:'running'});await request;
  assert.equal(h.el('start-form').hidden,true);
});

test('archived conversations remain searchable when requested',()=>{
  const h=harness();h.run('threads[1].archived=true;renderThreads();');
  assert.equal(h.el('thread-options').children.length,1);
  h.el('show-archived').checked=true;h.el('show-archived').emit('change');
  assert.equal(h.el('thread-options').children.length,2);
  h.el('search').value='Task B';h.el('search').emit('input');
  assert.equal(h.el('thread-options').children.length,1);
  assert.match(h.el('thread-options').textContent,/Task B/);
});

test('ordinary task selection and watch cards do not display internal UUIDs',()=>{
  const h=harness();
  assert.doesNotMatch(h.el('thread-options').textContent,new RegExp(A));
  h.el('thread-options').children[0].emit('click');
  assert.equal(h.el('thread-id').value,A); // Still bound internally to the exact task.
  assert.equal(h.el('selected-title').textContent,'Task A');
  h.run(`renderWatches([{thread_id:'${A}',enabled:0,status:'stopped',reason:'done',attempts:0,max_resumes:3,updated:1000}])`);
  assert.doesNotMatch(h.el('watch-list').textContent,new RegExp(A));
  assert.match(h.el('watch-list').textContent,/Task A/);
});

test('opening the picker collapses manual entry and keeps check hidden until selection',async()=>{
  const h=harness();h.el('manual-selection').open=true;
  const request=h.run('openChooser()');
  assert.equal(h.el('manual-selection').open,false);
  assert.equal(h.el('check-button').hidden,true);
  h.pending[0].reply([{id:A,title:'Task A'}]);await request;
  h.el('thread-options').children[0].emit('click');
  assert.equal(h.el('check-button').hidden,false);
});

test('check result can return directly to search instead of scrolling the entire list',async()=>{
  const h=harness();h.choose(A);const request=h.run('checkTask()');
  h.pending[0].reply(h.result(A));await request;
  h.el('check-result').querySelector('.back-to-list').emit('click');
  assert.equal(h.el('picker-browser').hidden,false);
  assert.equal(h.el('task-details').hidden,true);
  assert.equal(h.el('search').focused,true);
});

test('returning to the fixed list invalidates an in-flight check without losing scroll position',async()=>{
  const h=harness();h.el('thread-options').scrollTop=400;h.choose(A);const request=h.run('checkTask()');
  assert.equal(h.el('picker-browser').hidden,true);
  assert.equal(h.el('task-details').hidden,false);
  h.el('check-result').querySelector('.back-to-list').emit('click');
  h.pending[0].reply(h.result(A));await request;
  assert.equal(h.el('picker-browser').hidden,false);
  assert.equal(h.el('task-details').hidden,true);
  assert.equal(h.el('thread-options').scrollTop,400);
  assert.equal(h.run('checked'),null);
});


function prepareStart(h) {
  h.choose(A);h.run(`checked={threadId:'${A}',canMonitor:true,taskState:'running',decision:'wait'}`);
  h.el('max-resumes').value='1';h.el('confirm-start').checked=true;
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('continuation starts from the declared page default and resets on disconnect',()=>{
  const h=harness();
  assert.equal(h.el('continuation').value,DEFAULT_CONTINUATION);
  h.el('continuation').value='临时内容';
  h.run('forget()');
  assert.equal(h.el('continuation').value,DEFAULT_CONTINUATION);
  assert.equal(h.el('restart-continuation').value,'');
});
test('start sends the configured continuation and refuses an empty one',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);
  h.el('continuation').value='  自定义续跑：先核对进度。  ';
  const request=h.run('startTask({preventDefault(){}})');await tick();
  assert.equal(h.pending[0].url,'/api/start');
  assert.equal(JSON.parse(h.pending[0].options.body).continuation,'自定义续跑：先核对进度。');
  h.pending[0].reply({threadId:A,status:'watching'});await request;
  const empty=harness();prepareStart(empty);empty.el('continuation').value='   ';
  await empty.run('startTask({preventDefault(){}})');
  assert.equal(empty.pending.length,0);
  assert.match(empty.el('action-error').textContent,/请填写额度恢复后的续跑内容/);
});
test('POST success requires matching task and recognized start status',{timeout:2000},async()=>{
  for(const result of [{threadId:B,status:'watching'},{threadId:A,status:'invented'},{}]) {
    const h=harness();prepareStart(h);const request=h.run('startTask({preventDefault(){}})');
    await tick();h.pending[0].reply(result);await request;
    assert.doesNotMatch(h.el('notice').textContent,/任务已提交监控/);
    assert.ok(h.run('pendingMutation()'));
  }
  const h=harness();h.run(`openStop({thread_id:'${A}'})`);const request=h.run('stopTask()');
  await tick();h.pending[0].reply({});await request;
  assert.doesNotMatch(h.el('notice').textContent,/已停止后续/);
  assert.ok(h.run('pendingMutation()'));
});
test('stale login error cannot disconnect a newly authenticated session',{timeout:2000},async()=>{
  const h=harness();h.el('login-button').className='utton';h.el('login-form').append(h.el('login-button'));
  const old=h.run("token='old';login()");h.run('forget()');const newer=h.run("token='new';login()");
  h.pending[1].reply([]);await tick();assert.equal(h.run('connected'),true);
  h.pending[0].reply([]);await old;assert.equal(h.run('connected'),true);
  for(const p of h.pending.slice(2))p.reply(p.url.includes('quota')?{app:{},ready:false}:[]);
  await tick();for(const p of h.pending.slice(2))p.reply([]);await newer;
});
test('uncertain POST persists through reselect and new tab; simultaneous tabs cannot write',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);const first=h.run('startTask({preventDefault(){}})');await tick();
  const other=harness({storage:h.storage,locks:h.locks});prepareStart(other);
  await other.run('startTask({preventDefault(){}})');assert.equal(other.pending.length,0);
  h.pending[0].reject(new Error('lost connection'));await first;
  prepareStart(h);await h.run('startTask({preventDefault(){}})');assert.equal(h.pending.length,1);
  const reload=harness({storage:h.storage,locks:h.locks});prepareStart(reload);
  await reload.run('startTask({preventDefault(){}})');assert.equal(reload.pending.length,0);
});
test('confirmed rejection releases intent but storage failure prevents any POST',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);const first=h.run('startTask({preventDefault(){}})');await tick();
  h.pending[0].reply({error:'bad input'},400);await first;assert.equal(h.run('pendingMutation()'),null);
  prepareStart(h);h.run("localStorage.setItem=()=>{throw new Error('storage unavailable')}");
  await h.run('startTask({preventDefault(){}})');assert.equal(h.pending.length,1);
});
test('unlock requires fresh read and consent; cannot unlock another tab while request is active',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);const first=h.run('startTask({preventDefault(){}})');await tick();
  const other=harness({storage:h.storage,locks:h.locks});other.run('connected=true');other.el('confirm-mutation-review').checked=true;
  await other.run('acknowledgeMutation()');assert.ok(other.run('pendingMutation()'));
  other.run('lastWatchRead=Date.now()+1');await other.run('acknowledgeMutation()');assert.ok(other.run('pendingMutation()'));
  h.pending[0].reject(new Error('lost connection'));await first;
  other.el('confirm-mutation-review').checked=false;await other.run('acknowledgeMutation()');assert.ok(other.run('pendingMutation()'));
  other.el('confirm-mutation-review').checked=true;await other.run('acknowledgeMutation()');assert.equal(other.run('pendingMutation()'),null);
  assert.equal(other.pending.length,0);
});

test('same-origin mutation lock is identical across clients and always non-queueing',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);const first=h.run('startTask({preventDefault(){}})');await tick();
  const other=harness({storage:h.storage,locks:h.locks});prepareStart(other);await other.run('startTask({preventDefault(){}})');
  assert.equal(h.locks.calls.length,2);
  assert.equal(h.locks.calls[0].name,'relay-web-mutation-v1');
  for(const call of h.locks.calls){assert.equal(call.name,h.locks.calls[0].name);assert.equal(call.options.ifAvailable,true);}
  h.pending[0].reply({threadId:A,status:'watching'});await first;assert.equal(h.run('pendingMutation()'),null);
  other.run(`openStop({thread_id:'${A}'})`);const stop=other.run('stopTask()');await tick();
  other.pending[0].reply({stopped:A});await stop;assert.equal(other.run('pendingMutation()'),null);
  assert.match(other.el('notice').textContent,/已停止后续/);
});
test('unsupported Web Locks and unavailable storage visibly fail closed before any write',async()=>{
  for(const setup of ["navigator.locks=undefined","localStorage.setItem=()=>{throw new Error('unavailable')}"]){
    const h=harness();h.run(setup+';renderMutation()');
    assert.equal(h.el('mutation-warning').hidden,false);assert.match(h.el('mutation-description').textContent,/只读，不能开启或停止/);
    assert.equal(h.el('start-button').disabled,true);assert.equal(h.el('confirm-stop').disabled,true);
    prepareStart(h);await h.run('startTask({preventDefault(){}})');assert.equal(h.pending.length,0);
  }
});
test('CLI failure HTTP 409 remains uncertain and disconnect does not clear durable intent',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);const first=h.run('startTask({preventDefault(){}})');await tick();
  h.pending[0].reply({error:'watcher may have launched'},409);await first;
  assert.ok(h.run('pendingMutation()'));h.run('forget()');assert.ok(h.run('pendingMutation()'));
});
test('stale refresh failure cannot replace fresh records after reconnect',{timeout:2000},async()=>{
  const h=harness();h.run('connected=true');const old=h.run('refresh(true)');h.run('forget();connected=true');
  h.run(`renderWatches([{thread_id:'${A}',enabled:1,status:'watching'}])`);
  const fresh=h.el('watch-list').textContent;
  h.pending[0].reply({error:'old failure'},409);h.pending[1].reply({error:'old quota failure'},409);await old;
  assert.equal(h.el('watch-list').textContent,fresh);assert.equal(h.el('watch-error').textContent,'');
  assert.doesNotMatch(h.el('quota-reason').textContent,/old quota failure/);
});

test('delayed Web Lock cannot dispatch an old task under a replacement connection',{timeout:2000},async()=>{
  const h=harness();prepareStart(h);
  h.run("token='old';navigator.locks.request=(name,opts,fn)=>new Promise((resolve,reject)=>{globalThis.releaseLock=()=>Promise.resolve(fn({name})).then(resolve,reject)})");
  const request=h.run('startTask({preventDefault(){}})');
  h.run("forget();token='new';releaseLock()");await request;
  assert.equal(h.pending.length,0);assert.equal(h.run('pendingMutation()'),null);
});
test('delayed unlock rechecks connection and withdrawn consent before removing intent',{timeout:2000},async()=>{
  for(const change of ['forget()',"$('confirm-mutation-review').checked=false"]){
    const h=harness();h.run(`localStorage.setItem(MUTATION_KEY,JSON.stringify({action:'start',threadId:'${A}',createdAt:1}));connected=true;lastWatchRead=2`);
    h.el('confirm-mutation-review').checked=true;
    h.run("navigator.locks.request=(name,opts,fn)=>new Promise((resolve,reject)=>{globalThis.releaseLock=()=>Promise.resolve(fn({name})).then(resolve,reject)})");
    const request=h.run('acknowledgeMutation()');h.run(change+';releaseLock()');await request;
    assert.ok(h.run('pendingMutation()'));assert.equal(h.pending.length,0);
  }
});

test('token fragment connects automatically, keeps the token for the tab, and leaves the address',async()=>{
  const h=harness({hash:'#token=secret-value'});
  assert.equal(h.run('token'),'secret-value');
  assert.equal(h.session.get('relay-token'),'secret-value');
  assert.deepEqual(h.replaced,[[null,'','/']]);
  assert.equal(h.pending.length,1);
  assert.equal(h.pending[0].url,'/api/watches');
  assert.equal(h.pending[0].options.headers['X-Resume-Token'],'secret-value');
  h.pending[0].reply([]);await tick();
  assert.equal(h.run('connected'),true);
  assert.equal(h.el('stop-service').hidden,false);
});

test('a stored tab token connects without a fragment and does not rewrite the address',()=>{
  const h=harness({session:new Map([['relay-token','tab-token']])});
  assert.equal(h.replaced.length,0);
  assert.equal(h.run('token'),'tab-token');
  assert.equal(h.pending[0].options.headers['X-Resume-Token'],'tab-token');
});

test('stop service sends one authenticated shutdown and disconnects after confirmation',{timeout:2000},async()=>{
  const h=harness();h.run("token='live';sessionStorage.setItem('relay-token','live');connectedUI(true)");
  h.el('stop-service').emit('click');
  assert.equal(h.el('shutdown-dialog').open,true);
  const request=h.run('shutdownService()');
  assert.equal(h.el('confirm-shutdown').disabled,true);
  assert.match(h.el('confirm-shutdown').textContent,/正在停止/);
  assert.equal(h.pending[0].url,'/api/shutdown');
  assert.equal(h.pending[0].options.method,'POST');
  assert.equal(h.pending[0].options.body,JSON.stringify({stopWatches:false}));
  h.pending[0].reply({stopped:true});await request;
  assert.equal(h.el('shutdown-dialog').open,false);
  assert.equal(h.run('connected'),false);
  assert.equal(h.el('stop-service').hidden,true);
  assert.equal(h.session.has('relay-token'),false);
  assert.match(h.el('notice').textContent,/控制台服务已停止/);
  assert.equal(h.el('confirm-shutdown').disabled,false);
});

test('unconfirmed stop-service response keeps the connection and reports failure',{timeout:2000},async()=>{
  for(const reply of [{},{stopped:false}]){
    const h=harness();h.run("token='live';connectedUI(true)");
    h.el('stop-service').emit('click');
    const request=h.run('shutdownService()');
    h.pending[0].reply(reply);await request;
    assert.equal(h.run('connected'),true);
    assert.equal(h.el('shutdown-dialog').open,true);
    assert.match(h.el('shutdown-error').textContent,/不匹配/);
    assert.doesNotMatch(h.el('notice').textContent,/控制台服务已停止/);
    assert.equal(h.el('confirm-shutdown').disabled,false);
  }
});

test('stop service can also cancel every enrolled watcher',{timeout:2000},async()=>{
  const h=harness();h.run("token='live';sessionStorage.setItem('relay-token','live');connectedUI(true)");
  h.el('stop-service').emit('click');
  h.el('shutdown-watches').checked=true;
  const request=h.run('shutdownService()');
  assert.equal(h.pending[0].options.body,JSON.stringify({stopWatches:true}));
  h.pending[0].reply({stopped:true,watches:{stopped:['a','b']}});await request;
  assert.equal(h.run('connected'),false);
  assert.equal(h.el('shutdown-watches').checked,false);
  assert.match(h.el('notice').textContent,/已取消 2 个任务监控/);
});

test('a shutdown response without watcher results is not treated as success',{timeout:2000},async()=>{
  const h=harness();h.run("token='live';connectedUI(true)");
  h.el('stop-service').emit('click');
  h.el('shutdown-watches').checked=true;
  const request=h.run('shutdownService()');
  h.pending[0].reply({stopped:true});await request;
  assert.equal(h.run('connected'),true);
  assert.equal(h.el('shutdown-dialog').open,true);
  assert.match(h.el('shutdown-error').textContent,/不匹配/);
  assert.equal(h.el('shutdown-watches').checked,true);
});

test('a stale shutdown success cannot disconnect a newer connection',{timeout:2000},async()=>{
  const h=harness();h.run("token='old';connectedUI(true)");
  const request=h.run('shutdownService()');
  h.run("forget();token='new';login()");
  h.pending[0].reply({stopped:true});await request;
  h.pending[1].reply([]);await tick();
  assert.equal(h.run('connected'),true);
  assert.equal(h.el('shutdown-error').textContent,'');
  assert.doesNotMatch(h.el('notice').textContent,/控制台服务已停止/);
});

test('a stale shutdown failure cannot show an error after disconnect',{timeout:2000},async()=>{
  const h=harness();h.run("token='old';connectedUI(true)");
  const request=h.run('shutdownService()');
  h.run('forget()');
  h.pending[0].reply({error:'bad'},500);await request;
  assert.equal(h.el('shutdown-error').textContent,'');
  assert.doesNotMatch(h.el('notice').textContent,/未能确认/);
  assert.equal(h.el('confirm-shutdown').disabled,false);
});

test('disconnect hides the stop-service button and closes its dialog',()=>{
  const h=harness();h.run('connectedUI(true)');
  h.el('shutdown-dialog').open=true;
  h.run('forget()');
  assert.equal(h.el('stop-service').hidden,true);
  assert.equal(h.el('shutdown-dialog').open,false);
});

function watchActions(h,index){
  return h.el('watch-list').children[index].children[1].children[1];
}
function restartRow(h,status='stopped',attempts=0,max=3,continuation='重启续跑内容'){
  h.run(`renderWatches([{thread_id:'${A}',enabled:0,status:'${status}',reason:'test reason',attempts:${attempts},max_resumes:${max},updated:1000,continuation:'${continuation}'}])`);
}
test('disabled watch rows offer restart without the check flow; enabled rows only stop',()=>{
  const h=harness();
  h.run(`renderWatches([{thread_id:'${A}',enabled:0,status:'stopped',reason:'done',attempts:2,max_resumes:3,updated:1000},{thread_id:'${B}',enabled:1,status:'watching',reason:'running',attempts:0,max_resumes:3,updated:1000}])`);
  assert.deepEqual(watchActions(h,0).children.map(b=>b.textContent),['重新启动','检查任务']);
  assert.deepEqual(watchActions(h,1).children.map(b=>b.textContent),['停止监控']);
});
test('uncertain records cannot be restarted from the list',()=>{
  const h=harness();restartRow(h,'uncertain',1);
  assert.deepEqual(watchActions(h,0).children.map(b=>b.textContent),['检查任务']);
});
test('restart requires consent and sends one guarded start with the chosen budget',{timeout:2000},async()=>{
  const h=harness();restartRow(h,'stopped',3,3);
  watchActions(h,0).children[0].emit('click');
  assert.equal(h.el('restart-dialog').open,true);
  assert.equal(h.el('restart-task').textContent,'Task A');
  assert.equal(h.el('restart-max').value,'4');
  assert.equal(h.el('restart-continuation').value,'重启续跑内容');
  assert.equal(h.el('confirm-restart').disabled,true);
  h.el('confirm-restart-check').checked=true;h.el('confirm-restart-check').emit('change');
  assert.equal(h.el('confirm-restart').disabled,false);
  h.el('restart-max').value='5';
  h.el('restart-continuation').value='  修改后的重启续跑  ';
  const request=h.run('restartTask()');
  await tick();
  assert.equal(h.pending[0].url,'/api/start');
  assert.deepEqual(JSON.parse(h.pending[0].options.body),{threadId:A,maxResumes:5,confirmed:true,continuation:'修改后的重启续跑'});
  h.pending[0].reply({threadId:A,status:'watching'});await request;
  assert.equal(h.el('restart-dialog').open,false);
  assert.match(h.el('notice').textContent,/重新提交监控/);
  assert.equal(h.run('pendingMutation()'),null);
});
test('invalid restart budget or empty continuation never dispatches',async()=>{
  for(const setup of [h=>{h.el('restart-max').value='0';},h=>{h.el('restart-continuation').value='  ';}]){
    const h=harness();restartRow(h);
    watchActions(h,0).children[0].emit('click');
    h.el('confirm-restart-check').checked=true;h.el('confirm-restart-check').emit('change');
    setup(h);
    await h.run('restartTask()');
    assert.equal(h.pending.length,0);
    assert.match(h.el('restart-error').textContent,/1 到 100|请填写额度恢复后的续跑内容/);
  }
});
test('a closed restart dialog ignores late success or failure and refuses same-UUID reopen',{timeout:2000},async()=>{
  for(const outcome of ['success','failure']){
    const h=harness();restartRow(h);
    watchActions(h,0).children[0].emit('click');
    h.el('confirm-restart-check').checked=true;h.el('confirm-restart-check').emit('change');
    const request=h.run('restartTask()');
    await tick();
    h.el('restart-dialog').close();
    watchActions(h,0).children[0].emit('click');
    assert.equal(h.el('restart-dialog').open,false);
    if(outcome==='success') h.pending[0].reply({threadId:A,status:'watching'});
    else h.pending[0].reply({error:'late'},500);
    await request;
    assert.equal(h.el('restart-dialog').open,false);
    assert.doesNotMatch(h.el('notice').textContent,/重新提交监控|重启结果需检查/);
    assert.equal(h.el('restart-error').textContent,'');
    assert.equal(h.run('starting'),false);
  }
});
