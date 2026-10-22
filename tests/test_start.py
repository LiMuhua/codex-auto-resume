"""Double-click launcher tests; never open a real browser or bind a real port."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_resume.web import CompanionServer

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'start' / 'start.pyw'
SPEC = importlib.util.spec_from_file_location(
    'codex_start', LAUNCHER, loader=importlib.machinery.SourceFileLoader('codex_start', str(LAUNCHER)))
start = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start)


class FakeResponse:
    def __init__(self, status=200, server='CodexRelay Test'):
        self.status = status
        self.headers = {'Server': server}
    def __enter__(self):
        return self
    def __exit__(self, *_exc):
        return False


class StartLauncherTests(unittest.TestCase):
    def args(self, directory, **extra):
        values = dict(port=8765, home=Path(directory) / 'home', app=Path(directory) / 'app',
                      state_dir=Path(directory) / 'state', no_browser=True)
        values.update(extra)
        return SimpleNamespace(**values)

    def test_credential_is_passed_in_the_fragment_not_the_query(self):
        url = start.browser_url('http://127.0.0.1:8765', 'abc-_123')
        self.assertEqual(url, 'http://127.0.0.1:8765/#token=abc-_123')
        self.assertNotIn('?', url)

    def test_stale_or_mismatched_session_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            start.private_directory(args.state_dir)
            path = start.session_path(args)
            path.write_text('{not json', encoding='utf-8')
            self.assertIsNone(start.read_session(args))
            path.write_text(json.dumps(
                {'origin': 'http://127.0.0.1:9999', 'token': 'x', 'port': 8765}), encoding='utf-8')
            self.assertIsNone(start.read_session(args))

    def test_non_relay_page_does_not_count_as_a_live_session(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            start.private_directory(args.state_dir)
            start.session_path(args).write_text(json.dumps(
                {'origin': 'http://127.0.0.1:8765', 'token': 'old', 'port': 8765}),
                encoding='utf-8')
            with patch.object(start.LOCAL_OPENER, 'open',
                              return_value=FakeResponse(server='SomeOtherServer/1.0')):
                self.assertIsNone(start.read_session(args))

    def test_a_just_starting_relay_server_is_waited_for(self):
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            start.private_directory(args.state_dir)
            start.session_path(args).write_text(json.dumps(
                {'origin': 'http://127.0.0.1:8765', 'token': 'old', 'port': 8765}),
                encoding='utf-8')
            with patch.object(start.LOCAL_OPENER, 'open',
                              side_effect=[URLError('still starting'), FakeResponse()]), \
                    patch.object(start.time, 'sleep') as sleep:
                self.assertEqual(start.read_session(args)['token'], 'old')
            sleep.assert_called_once()

    def test_live_relay_server_is_accepted_as_an_existing_session(self):
        server = CompanionServer(('127.0.0.1', 0), None, token='live-token')
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                args = self.args(directory, port=server.server_address[1])
                start.private_directory(args.state_dir)
                start.session_path(args).write_text(json.dumps(
                    {'origin': server.origin, 'token': 'live-token', 'port': args.port}),
                    encoding='utf-8')
                self.assertEqual(start.read_session(args)['token'], 'live-token')
        finally:
            server.shutdown(); server.server_close(); worker.join()

    @unittest.skipIf(os.name == 'nt', 'POSIX permission check')
    def test_private_state_directory_rejects_group_access(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            state.mkdir(mode=0o755)
            with self.assertRaises(RuntimeError):
                start.private_directory(state)

    def test_second_launch_reuses_the_live_session(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory, no_browser=False)
            start.private_directory(args.state_dir)
            session = {'origin': 'http://127.0.0.1:8765', 'token': 'live', 'port': 8765}
            with patch.object(start, 'read_session', return_value=session), \
                    patch.object(start, 'serve', side_effect=AssertionError('must not start twice')), \
                    patch.object(start.webbrowser, 'open') as opened:
                start.run(args)
            opened.assert_called_once_with('http://127.0.0.1:8765/#token=live')
            self.assertFalse(start.lock_path(args).exists())

    def test_live_instance_lock_blocks_a_duplicate_start(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory, no_browser=False)
            start.private_directory(args.state_dir)
            start.lock_path(args).write_text(str(os.getpid()), encoding='ascii')
            with patch.object(start, 'serve', side_effect=AssertionError('must not start twice')), \
                    patch.object(start.webbrowser, 'open') as opened:
                start.run(args)
            opened.assert_not_called()

    def test_live_instance_reopens_its_ready_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory, no_browser=False)
            start.private_directory(args.state_dir)
            start.lock_path(args).write_text(str(os.getpid()), encoding='ascii')
            session = {'origin': 'http://127.0.0.1:8765', 'token': 'live', 'port': 8765}
            with patch.object(start, 'read_session', return_value=session) as read, \
                    patch.object(start, 'serve', side_effect=AssertionError('must not start twice')), \
                    patch.object(start.webbrowser, 'open') as opened:
                start.run(args)
            self.assertEqual(read.call_args.kwargs['grace'], 0)
            opened.assert_called_once_with('http://127.0.0.1:8765/#token=live')
            self.assertTrue(start.lock_path(args).exists())  # The owner's lock is untouched.

    def test_stale_instance_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            start.private_directory(args.state_dir)
            start.lock_path(args).write_text('999999', encoding='ascii')
            served = []
            with patch.object(start, 'process_exists', return_value=False), \
                    patch.object(start, 'read_session', return_value=None), \
                    patch.object(start, 'serve', side_effect=lambda *a, **k: served.append(1)):
                start.run(args)
            self.assertEqual(served, [1])
            self.assertFalse(start.lock_path(args).exists())

    def test_reclaimed_lock_does_not_wait_for_a_dead_session(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            start.private_directory(args.state_dir)
            start.lock_path(args).write_text('999999', encoding='ascii')
            start.session_path(args).write_text(json.dumps(
                {'origin': 'http://127.0.0.1:8765', 'token': 'dead', 'port': 8765}),
                encoding='utf-8')
            served = []
            with patch.object(start, 'process_exists', return_value=False), \
                    patch.object(start.LOCAL_OPENER, 'open',
                                 side_effect=OSError('refused')) as opened, \
                    patch.object(start, 'serve', side_effect=lambda *a, **k: served.append(1)):
                start.run(args)
            self.assertEqual(served, [1])
            self.assertEqual(opened.call_count, 1)
            self.assertFalse(start.session_path(args).exists())

    def test_session_exists_only_while_serve_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory, no_browser=False)
            seen = {}

            def fake_serve(_args, token=None, ready=None):
                seen['token'] = token
                seen['lock'] = start.lock_path(args).exists()
                ready('http://127.0.0.1:8765', token)
                seen['session'] = json.loads(
                    start.session_path(args).read_text(encoding='utf-8'))

            with patch.object(start, 'serve', side_effect=fake_serve), \
                    patch.object(start.webbrowser, 'open') as opened:
                start.run(args)
            self.assertEqual(seen['session'],
                             {'origin': 'http://127.0.0.1:8765', 'token': seen['token'], 'port': 8765})
            self.assertTrue(seen['lock'])
            self.assertFalse(start.session_path(args).exists())
            self.assertFalse(start.lock_path(args).exists())
            opened.assert_called_once_with(
                f'http://127.0.0.1:8765/#token={seen["token"]}')

    def test_launch_failure_is_reported_in_a_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            with patch.object(start, 'launch_background', side_effect=OSError('boom')), \
                    patch.object(start, 'alert') as alert:
                with self.assertRaises(OSError):
                    start.main(['--state-dir', str(args.state_dir)])
            alert.assert_called_once()
            self.assertIn('启动失败', alert.call_args.args[0])

    def test_background_launch_is_detached_and_logs_to_the_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            with patch.object(start.subprocess, 'Popen') as popen:
                start.launch_background(args)
            command = popen.call_args.args[0]
            self.assertEqual(command[1], str(LAUNCHER))
            self.assertIn('--background', command)
            self.assertTrue((args.state_dir / 'web.log').exists())
            if os.name == 'nt':
                self.assertTrue(popen.call_args.kwargs['creationflags'] & 0x00000008)
                self.assertNotIn('start_new_session', popen.call_args.kwargs)
            else:
                self.assertTrue(popen.call_args.kwargs['start_new_session'])


if __name__ == '__main__':
    unittest.main()
