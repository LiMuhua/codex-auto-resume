#!/usr/bin/env python3
"""Double-click launcher for the local web console.

Saved as .pyw so Windows opens it with pythonw.exe and no console window
appears at all. The first invocation relaunches itself as a detached process;
the detached process serves the dashboard, writes a private session file for
re-open/stop, and opens the browser with the one-time credential in the URL
fragment (never sent to the server or referrer). Start-up failures are shown in
a message box because there is no console to print to.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_resume.runtime import default_app_path, process_exists  # noqa: E402
from codex_resume.web import serve  # noqa: E402

SESSION_NAME = 'web-session.json'
LOG_NAME = 'web.log'
SESSION_GRACE = 3.0  # Seconds a just-written session may take to accept connections.
LOCAL_OPENER = build_opener(ProxyHandler({}))  # Never send loopback checks through a proxy.


def parser():
    p = argparse.ArgumentParser(description='静默启动 Codex Relay 本机控制台并自动打开浏览器')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--home', type=Path,
                   default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    p.add_argument('--app', type=Path, default=default_app_path())
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.codex-auto-resume')
    p.add_argument('--no-browser', action='store_true', help='启动服务但不打开浏览器')
    p.add_argument('--foreground', action='store_true', help='在当前终端运行，便于查看输出')
    p.add_argument('--background', action='store_true', help=argparse.SUPPRESS)
    return p


def private_directory(path):
    """Create the state directory with the same privacy rules as Store."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
        raise RuntimeError('状态目录不是普通目录')
    if os.name != 'nt' and (st.st_uid != os.getuid() or st.st_mode & 0o077):
        raise RuntimeError('状态目录必须属于当前用户，且只有当前用户可访问')


def session_path(args):
    return args.state_dir / SESSION_NAME


def lock_path(args):
    # Scope the lock to the port so a second dashboard on another port is allowed.
    return args.state_dir / f'server-{args.port}.lock'


def acquire_instance(args):
    """Serialize startup for this port, returning a release callable or None.

    Windows with SO_REUSEADDR lets two servers bind the same loopback address,
    so a rapid second double-click must be refused before binding rather than
    detected afterwards. A lock left by a crashed process is reclaimed by PID.
    """
    path = lock_path(args)
    deadline = time.monotonic() + SESSION_GRACE
    reclaimed = False
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pid = None
            try:
                pid = int(path.read_text(encoding='ascii').strip())
            except (OSError, ValueError):
                pass
            if pid is None and time.monotonic() < deadline:
                time.sleep(0.05)  # Owner may still be writing its PID.
                continue
            if pid is not None and process_exists(pid):
                return None
            try:
                path.unlink()
                reclaimed = True
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)
            continue
        else:
            with os.fdopen(fd, 'w', encoding='ascii') as stream:
                stream.write(str(os.getpid()))
            return (lambda: path.unlink(missing_ok=True)), reclaimed


def read_session(args, grace=SESSION_GRACE):
    """Return only a session that is still serving; stale files are ignored."""
    path = session_path(args)
    try:
        if path.is_symlink():
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        origin, token = data.get('origin'), data.get('token')
        if (not isinstance(origin, str) or not isinstance(token, str) or not token
                or data.get('port') != args.port
                or origin != f'http://127.0.0.1:{args.port}'):
            return None
    except (OSError, TypeError, ValueError):
        return None
    deadline = time.monotonic() + grace
    while True:
        try:
            with LOCAL_OPENER.open(origin + '/', timeout=2) as response:
                # A stale file must not hand the browser to whatever now owns
                # the port. Only the dashboard's own server counts as running.
                if (response.status == 200
                        and response.headers.get('Server', '').startswith('CodexRelay')):
                    return data
                return None
        except (OSError, ValueError):
            # The session file is written just before serve_forever() starts
            # accepting. Wait briefly so a fast second double-click cannot
            # start a duplicate service on a platform that reuses addresses.
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)


def write_session(args, origin, token):
    path = session_path(args)
    temporary = path.with_name(f'.{SESSION_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    data = json.dumps({'origin': origin, 'token': token, 'port': args.port},
                      ensure_ascii=False).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def remove_session(args):
    try:
        session_path(args).unlink()
    except OSError:
        pass


def browser_url(origin, token):
    return f'{origin}/#token={quote(token, safe="")}'


def open_dashboard(origin, token):
    try:
        webbrowser.open(browser_url(origin, token))
    except Exception:
        pass


def launch_background(args):
    """Hand off to a detached interpreter; returns as soon as it is started."""
    private_directory(args.state_dir)
    log = args.state_dir / LOG_NAME
    command = [sys.executable, str(Path(__file__).resolve()), '--background',
               '--port', str(args.port), '--home', str(args.home), '--app', str(args.app),
               '--state-dir', str(args.state_dir)]
    if args.no_browser:
        command.append('--no-browser')
    options = {'cwd': str(ROOT), 'stdin': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        if pythonw.is_file():
            command[0] = str(pythonw)
        options['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
                                    | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200))
    else:
        options['start_new_session'] = True
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(log, flags, 0o600), 'ab') as stream:
        subprocess.Popen(command, stdout=stream, stderr=stream, **options)


def alert(message):
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Codex Relay', 0x00000010)
            return
        except Exception:
            pass
    stream = sys.stderr or sys.stdout
    if stream is not None:
        print(message, file=stream, flush=True)


def run(args):
    private_directory(args.state_dir)
    acquired = acquire_instance(args)
    if acquired is None:
        # A live instance owns the port. Re-open its ready dashboard; during a
        # simultaneous first start the owner opens the browser itself.
        existing = read_session(args, grace=0)
        if existing and not args.no_browser:
            open_dashboard(existing['origin'], existing['token'])
        return
    release, reclaimed = acquired
    writing = []

    def ready(origin, actual_token):
        write_session(args, origin, actual_token)
        writing.append(origin)
        if not args.no_browser:
            open_dashboard(origin, actual_token)

    try:
        # A reclaimed lock proves the previous owner is gone, so its session
        # cannot become live; skip the startup grace in that case.
        existing = read_session(args, grace=0 if reclaimed else SESSION_GRACE)
        if existing:
            # A second launch must not start a duplicate hidden service.
            if not args.no_browser:
                open_dashboard(existing['origin'], existing['token'])
            return
        remove_session(args)
        token = secrets.token_urlsafe(32)
        serve(SimpleNamespace(port=args.port, home=args.home, app=args.app,
                              state_dir=args.state_dir), token=token, ready=ready)
    finally:
        if writing:
            remove_session(args)
        release()


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error('--port 必须在 1 到 65535 之间')
    args.home, args.app, args.state_dir = [
        p.expanduser().absolute() for p in (args.home, args.app, args.state_dir)]
    try:
        if not args.foreground and not args.background:
            launch_background(args)
            return
        run(args)
    except Exception as exc:
        alert(f'启动失败：{exc}\n\n日志：{args.state_dir / LOG_NAME}')
        raise


if __name__ == '__main__':
    main()
