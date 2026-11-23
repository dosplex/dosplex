#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The start service, the start client, and the unit and desktop templates they are installed from."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / 'packaging/steamos/dosplex-start-service.py'
CLIENT_PATH = ROOT / 'packaging/steamos/dosplex-start-client.py'
TEMPLATE_DIR = ROOT / 'packaging/steamos'

spec = importlib.util.spec_from_file_location('start_service', SERVICE_PATH)
start_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_service)

spec = importlib.util.spec_from_file_location('start_client', CLIENT_PATH)
start_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_client)


def make_standin(directory, body):
    program = Path(directory) / 'standin'
    program.write_text('#!/usr/bin/python3\n' + body + '\n')
    program.chmod(0o755)
    return program


def make_config(directory, uid, program):
    config = Path(directory) / 'config.json'
    config.write_text(json.dumps({'uid': uid, 'program': str(program)}))
    return config


def run_service(config, sock, timeout=30, idle=30, peer_uid_override=None):
    """Start the service as a subprocess with the listener as fd 3. Returns the Popen."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    listener.listen(8)
    env = dict(os.environ)
    env.pop('LISTEN_PID', None)
    env.pop('LISTEN_FDS', None)
    env.pop('RUNNING', None)
    env['DOSPLEX_TEST_TRUSTED'] = '1'
    if peer_uid_override is not None:
        env['DOSPLEX_TEST_PEER_UID'] = str(peer_uid_override)
    fd = listener.detach()
    if fd == 3:
        env['LISTEN_FDS'] = '1'            # what systemd sets when it hands the socket over as descriptor 3
    proc = subprocess.Popen(
        [sys.executable, '-c', _SERVICE_WITH_UID_OVERRIDE,
         '--config', str(config), '--listen-fd', str(fd),
         '--timeout', str(timeout), '--idle', str(idle)],
        pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return proc, listener


_SERVICE_WITH_UID_OVERRIDE = (
    'import os, sys, importlib.util\n'
    'spec = importlib.util.spec_from_file_location("start_service", %r)\n'
    'm = importlib.util.module_from_spec(spec)\n'
    'spec.loader.exec_module(m)\n'
    'if os.environ.get("DOSPLEX_TEST_PEER_UID") is not None:\n'
    '    m.peer_uid = lambda conn: int(os.environ["DOSPLEX_TEST_PEER_UID"])\n'
    'if os.environ.get("DOSPLEX_TEST_TRUSTED"):\n'
    '    m.trusted = lambda p, role: None\n'
    'sys.exit(m.main())\n'
) % str(SERVICE_PATH)


def client_request(sock, data=b'start\n', timeout=10):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(sock))
    if data:
        conn.sendall(data)
    conn.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, ConnectionResetError, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    conn.close()
    return b''.join(chunks)


class TrustedTests(unittest.TestCase):
    """The ownership checks, with the stat results constructed by the test (it does not run as root)."""

    def lstat_result(self, st_mode, st_uid):
        return os.stat_result((st_mode, 1, 1, 1, st_uid, 0, 0, 1, 0, 0))

    def test_regular_root_file_is_trusted(self):
        mode = stat.S_IFREG | 0o755
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNone(start_service.trusted('/opt/dosplex/bin/dosplex', 'program'))
        mode2 = stat.S_IFREG | 0o600
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode2, 0)):
            self.assertIsNone(start_service.trusted('/var/lib/dosplex/start-service.json', 'file'))

    def test_not_root_owned(self):
        mode = stat.S_IFREG | 0o755
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 1000)):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))

    def test_group_writable_program(self):
        mode = stat.S_IFREG | 0o756
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))

    def test_other_writable_program(self):
        mode = stat.S_IFREG | 0o755
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNone(start_service.trusted('/x', 'program'))
        mode_o = stat.S_IFREG | 0o757
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode_o, 0)):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))

    def test_a_config_that_group_or_others_can_write_is_refused(self):
        # Whoever can write the config chooses which account may start DosPlex as root.
        mode = stat.S_IFREG | 0o644
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNone(start_service.trusted('/x', 'file'))
        for writable in (0o664, 0o646, 0o666):
            with self.subTest(oct(writable)), \
                    unittest.mock.patch('os.lstat', return_value=self.lstat_result(stat.S_IFREG | writable, 0)):
                self.assertIn('writable', start_service.trusted('/x', 'file'))

    def test_symlink_is_never_trusted(self):
        mode = stat.S_IFLNK | 0o777
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))

    def test_not_a_regular_file(self):
        mode = stat.S_IFDIR | 0o755
        with unittest.mock.patch('os.lstat', return_value=self.lstat_result(mode, 0)):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))

    def test_unreadable(self):
        with unittest.mock.patch('os.lstat', side_effect=OSError('no such file')):
            self.assertIsNotNone(start_service.trusted('/x', 'program'))


class ServiceProcessTests(unittest.TestCase):
    """The service run as a subprocess with a real listening socket handed to it as descriptor 3."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir='/tmp')
        self.root = Path(self.tmp)
        self.allowed = 1000
        self.record = self.root / 'record'
        self.envfile = self.root / 'env'
        self.sock = self.root / 'sock'
        self.standin = make_standin(self.tmp,
            "import sys, os\n"
            "open(%r, 'w').write(repr(sys.argv[1:]))\n"
            "open(%r, 'w').write(repr(sorted(os.environ.items())))\n"
            'print(\'{"api":1,"ok":true,"code":"started","message":"ok","data":{}}\')'
            % (str(self.record), str(self.envfile)))
        self.config = make_config(self.tmp, self.allowed, self.standin)
        self.proc = None
        self.listener = None

    def tearDown(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.listener is not None:
            self.listener.close()

    def start_service(self, timeout=30, idle=30, peer_uid=None):
        self.proc, self.listener = run_service(
            self.config, self.sock, timeout=timeout, idle=idle, peer_uid_override=peer_uid)

    def wait_for_ready(self):
        time.sleep(0.5)
        rc = self.proc.poll()
        if rc is not None:
            stderr = self.proc.stderr.read() if self.proc.stderr else b''
            stdout = self.proc.stdout.read() if self.proc.stdout else b''
            self.fail('service died at once (rc=%s)\nstdout: %s\nstderr: %s' % (rc, stdout, stderr))

    def test_allowed_uid_relay(self):
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n')
        value = json.loads(raw)
        self.assertTrue(value['ok'])
        self.assertEqual(value['code'], 'started')
        self.assertEqual(self.record.read_text(), "['control', 'start']")
        env = eval(self.envfile.read_text())
        # The child is a python3 process; the C runtime adds LC_CTYPE.
        # Everything else must be exactly the clean environment the service set.
        self.assertIn(('PATH', '/usr/bin:/bin'), env)
        for key, _ in env:
            self.assertIn(key, ('PATH', 'LC_CTYPE'), 'unexpected env var: %r' % key)
        self.assertFalse(any(k in ('HOME', 'USER', 'PWD') for k, _ in env),
                         'parent env leaked into the command')

    def test_different_uid_refused(self):
        self.start_service(peer_uid=9999)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n')
        value = json.loads(raw)
        self.assertFalse(value['ok'])
        self.assertEqual(value['code'], 'refused')
        self.assertFalse(self.record.exists(), 'the stand-in must not run')

    def test_root_refused(self):
        self.start_service(peer_uid=0)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n')
        value = json.loads(raw)
        self.assertFalse(value['ok'])
        self.assertEqual(value['code'], 'refused')
        self.assertFalse(self.record.exists(), 'the stand-in must not run')

    def test_bad_requests(self):
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        for payload in (b'start --layout single\n', b'stop\n', b'', b's' * 65):
            raw = client_request(self.sock, payload)
            value = json.loads(raw)
            self.assertFalse(value['ok'])
            self.assertEqual(value['code'], 'bad-request')
            self.assertFalse(self.record.exists(), 'the stand-in must not run')
        start = time.monotonic()
        raw = client_request(self.sock, b'')
        self.assertLess(time.monotonic() - start, 6, 'a silent client must not block the service')
        value = json.loads(raw)
        self.assertEqual(value['code'], 'bad-request')

    def test_silent_client_does_not_block(self):
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        start = time.monotonic()
        raw = client_request(self.sock, b'')
        self.assertLess(time.monotonic() - start, 6, 'a client that sends nothing must not block')
        value = json.loads(raw)
        self.assertEqual(value['code'], 'bad-request')

    def test_non_json_output(self):
        standin = make_standin(self.tmp, 'print("not json at all")\n')
        self.config.write_text(json.dumps({'uid': self.allowed, 'program': str(standin)}))
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n')
        value = json.loads(raw)
        self.assertFalse(value['ok'])
        self.assertEqual(value['code'], 'failed')

    def test_nonzero_exit_no_output(self):
        standin = make_standin(self.tmp, 'import sys; sys.exit(3)\n')
        self.config.write_text(json.dumps({'uid': self.allowed, 'program': str(standin)}))
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n')
        value = json.loads(raw)
        self.assertFalse(value['ok'])
        self.assertEqual(value['code'], 'failed')

    def test_timeout(self):
        standin = make_standin(self.tmp, 'import time; time.sleep(5)\n')
        self.config.write_text(json.dumps({'uid': self.allowed, 'program': str(standin)}))
        self.start_service(timeout=1, peer_uid=self.allowed)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start\n', timeout=10)
        value = json.loads(raw)
        self.assertFalse(value['ok'])
        self.assertEqual(value['code'], 'failed')

    def test_client_data_never_reaches_standin(self):
        self.start_service(peer_uid=self.allowed)
        self.wait_for_ready()
        raw = client_request(self.sock, b'start --layout single\n')
        value = json.loads(raw)
        self.assertEqual(value['code'], 'bad-request')
        self.assertFalse(self.record.exists(), 'the stand-in must not run for a bad request')

    def test_idle_exit_zero(self):
        self.start_service(idle=1, peer_uid=self.allowed)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.1)
        self.assertIsNotNone(self.proc.poll(), 'the service must exit on its own after the idle time')
        self.assertEqual(self.proc.returncode, 0, 'the service must exit 0 when idle')


class ConfigCheckTests(unittest.TestCase):
    """The config checks, with trusted() patched so the test does not need root."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir='/tmp')
        self.root = Path(self.tmp)
        self.standin = make_standin(self.tmp, 'print(1)\n')
        self.config = make_config(self.tmp, 1000, self.standin)

    def make_service(self):
        return start_service.Service(str(self.config), 5, 5)

    def test_check_config_rejects_bad_uid(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value=None):
            self.config.write_text(json.dumps({'uid': 'not-an-int', 'program': '/x'}))
            self.assertFalse(service.check_config())

    def test_check_config_rejects_bad_program(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value=None):
            self.config.write_text(json.dumps({'uid': 1000, 'program': 42}))
            self.assertFalse(service.check_config())

    def test_check_config_rejects_root_system_accounts_and_a_relative_program(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value=None):
            for uid in (0, 999, True):
                with self.subTest(uid=uid):
                    self.config.write_text(json.dumps({'uid': uid, 'program': '/opt/dosplex/bin/dosplex'}))
                    self.assertFalse(service.check_config())
            self.config.write_text(json.dumps({'uid': 1000, 'program': 'bin/dosplex'}))
            self.assertFalse(service.check_config())
            self.config.write_text(json.dumps({'uid': 1000, 'program': '/opt/dosplex/bin/dosplex'}))
            self.assertTrue(service.check_config())

    def test_a_client_that_hangs_up_does_not_end_the_service_and_the_log_names_the_real_peer(self):
        import io
        service = self.make_service()
        service.uid, service.program = 1000, str(self.standin)
        left, right = socket.socketpair()

        class Listener:
            def __init__(self):
                self.given = False

            def fileno(self):
                return left.fileno()

            def accept(self):
                self.given = True
                return right, None

        def broken(connection):
            raise BrokenPipeError(32, 'Broken pipe')
        service.idle = 0.6
        log = io.StringIO()
        right.sendall(b'x')                                  # makes the "listener" readable for select
        with unittest.mock.patch.object(start_service, 'peer_uid', return_value=1234), \
                unittest.mock.patch.object(service, 'connection', side_effect=broken), \
                unittest.mock.patch('sys.stderr', log):
            self.assertEqual(service.serve(Listener()), 0)   # it went on to its idle exit
        self.assertIn('peer uid 1234, answered dropped (Broken pipe)', log.getvalue())
        left.close()

    def test_check_config_rejects_non_dict(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value=None):
            self.config.write_text('[1, 2]')
            self.assertFalse(service.check_config())

    def test_check_config_accepts_good(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value=None):
            self.assertTrue(service.check_config())
            self.assertEqual(service.uid, 1000)
            self.assertEqual(service.program, str(self.standin))

    def test_check_config_rejects_untrusted(self):
        service = self.make_service()
        with unittest.mock.patch.object(start_service, 'trusted', return_value='/x is not owned by root'):
            self.assertFalse(service.check_config())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir='/tmp')
        self.root = Path(self.tmp)
        self.sock = self.root / 'sock'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.sock))
        self.server.listen(1)

    def tearDown(self):
        self.server.close()

    def run_client(self, socket_path):
        return subprocess.run(
            [sys.executable, str(CLIENT_PATH), '--no-window', '--socket', str(socket_path)],   # never a window in a test
            capture_output=True, text=True, timeout=15)

    def respond(self, answer_text):
        conn, _ = self.server.accept()
        with conn:
            conn.recv(64)
            conn.sendall(answer_text.encode())

    def test_ok(self):
        answer = json.dumps({'api': 1, 'ok': True, 'code': 'started', 'message': 'started', 'data': {}})
        thread = threading.Thread(target=self.respond, args=(answer,), daemon=True)
        thread.start()
        proc = self.run_client(self.sock)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('started', proc.stdout)
        thread.join(timeout=5)

    def test_already_running(self):
        answer = json.dumps({'api': 1, 'ok': False, 'code': 'already-running',
                             'message': 'already running', 'data': {}})
        thread = threading.Thread(target=self.respond, args=(answer,), daemon=True)
        thread.start()
        proc = self.run_client(self.sock)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('already running', proc.stdout)
        thread.join(timeout=5)

    def test_refused(self):
        answer = json.dumps({'api': 1, 'ok': False, 'code': 'refused',
                             'message': "Only the main player's account can start DosPlex.", 'data': {}})
        thread = threading.Thread(target=self.respond, args=(answer,), daemon=True)
        thread.start()
        proc = self.run_client(self.sock)
        self.assertEqual(proc.returncode, 1)
        thread.join(timeout=5)

    def test_missing_socket(self):
        proc = self.run_client(self.root / 'no-such-sock')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not installed', proc.stdout)

    def test_non_json_answer(self):
        thread = threading.Thread(target=self.respond, args=('garbage',), daemon=True)
        thread.start()
        proc = self.run_client(self.sock)
        self.assertEqual(proc.returncode, 1)
        thread.join(timeout=5)


    def test_an_accepted_start_is_said_for_a_person(self):
        answer = json.dumps({'api': 1, 'ok': True, 'code': 'accepted',
                             'message': 'The start was started in the background.', 'data': {}})
        thread = threading.Thread(target=self.respond, args=(answer,))
        thread.start()
        proc = self.run_client(self.sock)
        thread.join()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), 'DosPlex is starting. The screen changes in a moment.')

    def test_a_service_that_hangs_up_is_a_sentence_and_not_a_traceback(self):
        def hang_up():
            conn, _ = self.server.accept()
            conn.close()                       # nothing read, nothing sent: the client's read is reset or empty
        thread = threading.Thread(target=hang_up)
        thread.start()
        proc = self.run_client(self.sock)
        thread.join()
        self.assertEqual(proc.returncode, 1)
        self.assertIn('did not answer with a message', proc.stdout)
        self.assertNotIn('Traceback', proc.stderr)

if __name__ == '__main__':
    unittest.main()
