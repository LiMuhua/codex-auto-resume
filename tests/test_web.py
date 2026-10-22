"""HTTP boundary tests use a fake backend, never a real account or business task."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_resume.web import APIError, Backend, CompanionServer, serve

ROOT = Path(__file__).resolve().parent.parent

THREAD = '11111111-1111-4111-8111-111111111111'
THREAD2 = '22222222-2222-4222-8222-222222222222'
THREAD3 = '33333333-3333-4333-8333-333333333333'


class FakeBackend:
    def __init__(self):
        self.calls = []
    def watches(self): return []
    def threads(self): return [{'id': THREAD, 'title': '<script>not markup</script>'}]
    def quota(self): return {'ready': False}
    def check(self, value):
        self.calls.append(('check', value))
        return {'threadId': value, 'decision': 'stop'}
    def mutate(self, action, data):
        self.calls.append((action, data))
        return {'ok': True}
    def stop_all(self):
        self.calls.append(('stop_all', {}))
        return {'stopped': [THREAD]}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = FakeBackend()
        cls.server = CompanionServer(('127.0.0.1', 0), cls.backend, token='test-credential')
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.worker.join()
    def setUp(self): self.backend.calls.clear()
    def request(self, method, path, body=None, headers=None, auth=True):
        c = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        h = {'X-Resume-Token':'test-credential'} if auth else {}
        if headers: h.update(headers)
        c.request(method, path, body=body, headers=h)
        response = c.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        c.close()
        return result
    def test_static_shell_has_no_secret_and_does_not_read_tasks(self):
        status, headers, body = self.request('GET', '/', auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b'test-credential', body)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
    def test_all_private_gets_require_authentication(self):
        for path in ['/api/watches','/api/threads','/api/quota','/api/check?threadId='+THREAD]:
            self.assertEqual(self.request('GET',path,auth=False)[0],401)
        self.assertEqual(self.backend.calls, [])
    def test_bad_token_and_cross_site_requests_never_mutate(self):
        for headers, expected in [({'X-Resume-Token':'wrong'},401),
                                  ({'Origin':'https://other.example'},403),
                                  ({'Host':'attacker.example'},403),
                                  ({'Sec-Fetch-Site':'cross-site'},403)]:
            headers['Content-Type']='application/json'
            self.assertEqual(self.request('POST','/api/start',json.dumps({'threadId':THREAD}),headers)[0],expected)
        self.assertEqual(self.backend.calls, [])
    def test_same_origin_json_post(self):
        body={'threadId':THREAD,'confirmed':True,'maxResumes':1}
        code, _, _ = self.request('POST','/api/start',json.dumps(body),{'Content-Type':'application/json','Origin':self.server.origin})
        self.assertEqual(code,200)
        self.assertEqual(self.backend.calls,[('start',body)])
    def test_get_cannot_start_and_unknown_paths_do_not_escape_static_root(self):
        for path in ['/api/start','/../web.py','/%2e%2e/web.py','/app.js?secret=123']:
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.backend.calls, [])
    def test_check_requires_exactly_one_id(self):
        for path in ['/api/check','/api/check?threadId='+THREAD+'&threadId='+THREAD]:
            self.assertEqual(self.request('GET',path)[0],400)
        self.assertEqual(self.request('GET','/api/check?threadId='+THREAD)[0],200)
    def test_malformed_large_and_non_json_requests_rejected(self):
        for body, headers, expected in [('{}', {'Content-Type':'text/plain'},415),
                                        ('{', {'Content-Type':'application/json'},400),
                                        ('x'*2049, {'Content-Type':'application/json'},413)]:
            self.assertEqual(self.request('POST','/api/start',body,headers)[0],expected)
        self.assertEqual(self.backend.calls, [])
    def test_no_cors_or_reflected_private_error(self):
        with patch.object(self.backend,'quota',side_effect=Exception('private auth dump')):
            code, headers, body=self.request('GET','/api/quota')
        self.assertEqual(code,500)
        self.assertNotIn(b'private auth dump',body)
        self.assertNotIn('Access-Control-Allow-Origin',headers)
    def test_assets_are_served(self):
        for path in ['/app.js','/style.css','/icon.svg']:
            code, _, body=self.request('GET',path,auth=False)
            self.assertEqual(code,200)
    def test_shutdown_option_requires_a_boolean_and_reports_stopped_watches(self):
        backend=FakeBackend()
        server=CompanionServer(('127.0.0.1',0),backend,token='shutdown-credential')
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            def shutdown(body):
                c=http.client.HTTPConnection(*server.server_address,timeout=3)
                c.request('POST','/api/shutdown',body=json.dumps(body),
                          headers={'X-Resume-Token':'shutdown-credential','Content-Type':'application/json'})
                response=c.getresponse(); result=(response.status,json.loads(response.read())); c.close()
                return result
            self.assertEqual(shutdown({'stopWatches':'yes'})[0],400)
            self.assertEqual(shutdown({'other':True})[0],400)
            self.assertEqual(backend.calls,[])
            self.assertEqual(shutdown({'stopWatches':True}),
                             (200,{'stopped':True,'watches':{'stopped':[THREAD]}}))
            self.assertEqual(backend.calls,[('stop_all',{})])
            worker.join(3); self.assertFalse(worker.is_alive())
        finally:
            server.shutdown(); server.server_close()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend=Backend(SimpleNamespace(home=Path('/tmp/home'),app=Path('/tmp/app'),state_dir=Path('/tmp/state')))
    def test_start_requires_confirmation_uuid_budget_and_only_supported_fields(self):
        good={'threadId':THREAD,'confirmed':True,'maxResumes':1}
        cases=[{}, [], dict(good,confirmed=False),dict(good,threadId='; rm file'),
               dict(good,maxResumes=True),dict(good,maxResumes=0),dict(good,maxResumes=101),
               dict(good,maxResumes='3'),dict(good,prompt='run anything'),dict(good,limitId='--arg')]
        with patch.object(self.backend,'command') as command:
            for body in cases:
                with self.subTest(body=body), self.assertRaises(APIError): self.backend.mutate('start',body)
            command.assert_not_called()
    def test_start_calls_guarded_cli_and_does_not_expose_arbitrary_prompt(self):
        with patch.object(self.backend,'command',return_value={'status':'starting'}) as command:
            self.backend.mutate('start',{'threadId':THREAD,'confirmed':True,'limitId':'codex'})
            command.assert_called_once_with('start',THREAD,'--max-resumes','3','--limit-id','codex')
    def test_concurrent_start_refused_and_stop_runs_after_pending_start(self):
        self.backend.mutation_lock.acquire()
        entered = threading.Event()
        done = threading.Event()
        def stop():
            entered.set()
            self.backend.mutate('stop', {'threadId': THREAD})
            done.set()
        with patch.object(self.backend, 'command') as command:
            try:
                with self.assertRaises(APIError): self.backend.mutate('start', {'threadId': THREAD, 'confirmed': True})
                worker = threading.Thread(target=stop)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(done.wait(.05))
                command.assert_not_called()
            finally:
                self.backend.mutation_lock.release()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(done.is_set())
            command.assert_called_once_with('stop', THREAD)
    def test_stop_all_cancels_only_enabled_watches_under_the_mutation_lock(self):
        rows=[{'thread_id':THREAD,'enabled':1},{'thread_id':THREAD2,'enabled':0},
              {'thread_id':THREAD3,'enabled':1}]
        with patch.object(self.backend,'command',side_effect=[rows,{},{}]) as command:
            self.assertEqual(self.backend.stop_all()['stopped'],[THREAD,THREAD3])
        self.assertEqual([call.args for call in command.call_args_list],
                         [('status',),('stop',THREAD),('stop',THREAD3)])
        self.assertFalse(self.backend.mutation_lock.locked())
    def test_stop_all_reports_partial_failure_without_claiming_success(self):
        rows=[{'thread_id':THREAD,'enabled':1},{'thread_id':THREAD3,'enabled':1}]
        with patch.object(self.backend,'command',
                          side_effect=[rows,{},APIError('db busy',409)]):
            with self.assertRaises(APIError) as caught:
                self.backend.stop_all()
        self.assertIn('已停止 1 个',str(caught.exception))
        self.assertFalse(self.backend.mutation_lock.locked())
    def test_quota_cached_without_exposing_credits(self):
        raw={'app':{'version':'test'},'ipc':'connected','ready':True,'reason':'ok',
             'rateLimits':{'primary':{'usedPercent':10},'credits':{'balance':'secret'}}}
        with patch.object(self.backend,'command',return_value=raw) as command:
            first=self.backend.quota(); second=self.backend.quota()
            self.assertEqual(first,second); self.assertEqual(command.call_count,1)
            self.assertNotIn('credits',first)
    def test_command_timeout_no_retry(self):
        import subprocess
        with patch('codex_resume.web.subprocess.run',side_effect=subprocess.TimeoutExpired('test',65)) as run:
            with self.assertRaises(APIError): self.backend.command('start',THREAD)
            self.assertEqual(run.call_count,1)
    def test_commands_hide_console_windows_on_windows(self):
        done=SimpleNamespace(returncode=0,stdout='[]',stderr='')
        with patch('codex_resume.runtime.platform_name',return_value='windows'), \
                patch('codex_resume.web.subprocess.run',return_value=done) as run:
            self.assertEqual(self.backend.watches(),[])
            self.assertTrue(run.call_args.kwargs['creationflags'] & 0x08000000)
    def test_command_forces_utf8_so_gbk_titles_do_not_fail(self):
        done=SimpleNamespace(returncode=0,stdout=json.dumps({'title':'a\u00a0b'},ensure_ascii=False),stderr='')
        with patch('codex_resume.web.subprocess.run',return_value=done) as run:
            self.assertEqual(self.backend.watches(),{'title':'a\u00a0b'})
        kwargs=run.call_args.kwargs
        self.assertEqual(kwargs['encoding'],'utf-8')
        self.assertEqual(kwargs['errors'],'replace')
        self.assertEqual(kwargs['env']['PYTHONIOENCODING'],'utf-8')
        self.assertEqual(kwargs['env']['PYTHONUTF8'],'1')
    def test_serve_always_binds_loopback(self):
        args=SimpleNamespace(port=0,home=Path('/tmp/home'),app=Path('/tmp/app'),state_dir=Path('/tmp/state'))
        with patch('codex_resume.web.CompanionServer') as companion:
            server=companion.return_value.__enter__.return_value
            server.origin='http://127.0.0.1:1234'
            server.token='test-token'
            server.serve_forever.side_effect=KeyboardInterrupt
            serve(args)
        self.assertEqual(companion.call_args.args[0],('127.0.0.1',0))
    def test_serve_uses_provided_credential_and_reports_readiness(self):
        args=SimpleNamespace(port=0,home=Path('/tmp/home'),app=Path('/tmp/app'),state_dir=Path('/tmp/state'))
        ready=[]
        with patch('codex_resume.web.CompanionServer') as companion:
            server=companion.return_value.__enter__.return_value
            server.origin='http://127.0.0.1:1234';server.token='fixed-credential'
            server.serve_forever.side_effect=KeyboardInterrupt
            serve(args,token='fixed-credential',ready=lambda origin,token: ready.append((origin,token)))
        self.assertEqual(companion.call_args.kwargs['token'],'fixed-credential')
        self.assertEqual(ready,[('http://127.0.0.1:1234','fixed-credential')])
    def test_shutdown_requires_credential_and_stops_serve_loop(self):
        server=CompanionServer(('127.0.0.1',0),FakeBackend(),token='shutdown-credential')
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            for headers,expected in (({},401),({'X-Resume-Token':'shutdown-credential'},200)):
                connection=http.client.HTTPConnection(*server.server_address,timeout=3)
                try:
                    connection.request('POST','/api/shutdown',body='{}',
                                       headers={'Content-Type':'application/json',**headers})
                    response=connection.getresponse()
                    body=json.loads(response.read())
                    self.assertEqual(response.status,expected)
                    if expected==200: self.assertEqual(body,{'stopped':True})
                finally:
                    connection.close()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        finally:
            server.shutdown();server.server_close()


class ServerLimitTests(unittest.TestCase):
    def test_worker_pool_rejects_excess_connections_and_releases_slots(self):
        server=CompanionServer(('127.0.0.1',0),FakeBackend(),token='pool-test')
        # Use one worker to exercise the same bounded admission path without
        # opening dozens of sockets. No account, TLS trust, or network changes.
        server.worker_slots=threading.BoundedSemaphore(1)
        entered=threading.Event();release=threading.Event()
        class SlowHandler:
            def __init__(self,*args): entered.set();release.wait(2)
        server.RequestHandlerClass=SlowHandler
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        first=socket.create_connection(server.server_address,timeout=1)
        try:
            self.assertTrue(entered.wait(1))
            rejected=socket.create_connection(server.server_address,timeout=1)
            try:self.assertEqual(rejected.recv(1),b'')
            finally:rejected.close()
            release.set()
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                if server.worker_slots.acquire(blocking=False):
                    server.worker_slots.release();break
                time.sleep(.01)
            else:self.fail('worker slot leaked after request completion')
        finally:
            release.set();first.close();server.shutdown();server.server_close();worker.join()

class CliOutputEncodingTests(unittest.TestCase):
    def run_code(self, body, encoding='gbk'):
        env=os.environ.copy();env['PYTHONIOENCODING']=encoding;env.pop('PYTHONUTF8',None)
        return subprocess.run([sys.executable,'-c',body],cwd=ROOT,env=env,
                              capture_output=True,timeout=60)
    def test_json_output_and_errors_are_utf8_when_stream_encoding_is_gbk(self):
        value='a'+chr(0xa0)+'b'
        code=("from codex_resume.__main__ import output, emit_error; "
              "output({'title': 'a"+chr(92)+"u00a0b'}); "
              "emit_error('bad a"+chr(92)+"u00a0b')")
        result=self.run_code(code)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
        self.assertEqual(json.loads(result.stdout.decode('utf-8')),{'title':value})
        self.assertEqual(result.stderr.decode('utf-8'),'bad '+value+'\n')

if __name__=='__main__': unittest.main()
