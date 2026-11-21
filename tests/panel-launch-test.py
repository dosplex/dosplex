#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Tests for dosplex-panel-launch.py: the launcher for the QML Help and Feedback panel.

Each test builds a temporary home directory, a fake qml6 script, and (where needed)
a local HTTP server, then runs the launcher as a subprocess.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve()
PROGRAM = HERE.parent.parent / 'packaging' / 'steamos' / 'dosplex-panel-launch.py'
PYTHON = sys.executable or '/usr/bin/python3'


# --- HTTP server helper ----------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Record each request body; respond with the configured status."""
    received = []  # list of (path, body_bytes)
    status = 200

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        self.received.append((self.path, body))
        self.send_response(self.status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


def _start_server(status=200):
    _Handler.received = []
    _Handler.status = status
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f'http://127.0.0.1:{port}/v1/feedback'


# --- Base test case --------------------------------------------------------------

class PanelLaunchBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='dosplex-panel-test-')
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / 'home'
        self.home.mkdir(parents=True)
        self.panel_dir = self.tmp / 'panel'
        self.panel_dir.mkdir()
        (self.panel_dir / 'Panel.qml').write_text('// fake Panel.qml\n', encoding='utf-8')
        (self.panel_dir / 'Feedback.qml').write_text('// fake Feedback.qml\n', encoding='utf-8')
        self.manual_dir = self.tmp / 'manual'
        self.manual_dir.mkdir()
        self.qml = self.tmp / 'qml6'
        self.env_file = self.tmp / 'qml-env.json'
        self.outbox = self.home / '.local' / 'share' / 'dosplex' / 'feedback-outbox.jsonl'

    def tearDown(self):
        self._tmp.cleanup()

    def _write_manifest(self, version='1.2.3'):
        path = self.tmp / 'manifest.json'
        if version is None:
            path.write_text('not json', encoding='utf-8')
        else:
            path.write_text(json.dumps({'version': version}), encoding='utf-8')
        return path

    def _write_os_release(self, version_id='3.2'):
        path = self.tmp / 'os-release'
        if version_id is None:
            path.write_text('NAME=SteamOS\n', encoding='utf-8')
        else:
            path.write_text(f'NAME=SteamOS\nVERSION_ID="{version_id}"\n', encoding='utf-8')
        return path

    def _write_product_name(self, name=None):
        path = self.tmp / 'product_name'
        if name is not None:
            path.write_text(name + '\n', encoding='utf-8')
        return path

    def _write_loginusers(self, content):
        if content is None:
            return None
        path = self.home / '.local' / 'share' / 'Steam' / 'config'
        path.mkdir(parents=True, exist_ok=True)
        vdf = path / 'loginusers.vdf'
        vdf.write_text(content, encoding='utf-8')
        return vdf

    def _write_fake_qml(self, path, lines=None, exit_code=0, sleep_seconds=None, env_file=None):
        """Write a fake qml6 script that records argv and env, prints lines, exits."""
        env_file_repr = repr(env_file) if env_file else 'None'
        lines_repr = repr(lines or [])
        sleep_code = f"import time; time.sleep({sleep_seconds})" if sleep_seconds else "pass"
        code = (
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'argv = sys.argv\n'
            'env_vars = {\n'
            "    'QML_XHR_ALLOW_FILE_READ': os.environ.get('QML_XHR_ALLOW_FILE_READ', ''),\n"
            "    'QT_QUICK_CONTROLS_STYLE': os.environ.get('QT_QUICK_CONTROLS_STYLE', ''),\n"
            "    'QT_FORCE_STDERR_LOGGING': os.environ.get('QT_FORCE_STDERR_LOGGING', ''),\n"
            '}\n'
            f'if {env_file_repr}:\n'
            f'    with open({env_file_repr}, \'w\') as f:\n'
            '        json.dump({\'argv\': argv, \'env\': env_vars}, f)\n'
            f'for line in {lines_repr}:\n'
            '    sys.stdout.write(line + \'\\n\')\n'
            '    sys.stdout.flush()\n'
            f'{sleep_code}\n'
            f'sys.exit({exit_code})\n'
        )
        path.write_text(code, encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, mode='feedback', side='main', page='menu', layout='unknown',
             extra_args=None, env_extra=None, timeout=15):
        args = [
            PYTHON, str(PROGRAM),
            '--mode', mode, '--side', side,
            '--page', page, '--layout', layout,
            '--panel-dir', str(self.panel_dir),
            '--manual-dir', str(self.manual_dir),
            '--qml', str(self.qml),
            '--home', str(self.home),
        ]
        if extra_args:
            args.extend(extra_args)
        if not any(str(a).startswith('--endpoint') for a in args):
            args += ['--endpoint', 'http://127.0.0.1:1/unused']     # no test may ever reach the real address
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        done = subprocess.run(args, capture_output=True, text=True, env=env,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        return done

    def _qml_env(self):
        if self.env_file.is_file():
            return json.loads(self.env_file.read_text(encoding='utf-8'))
        return None


# --- Tests -----------------------------------------------------------------------

class RootRefusedTest(PanelLaunchBase):
    def test_root_refused(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=0, env_file=str(self.env_file))
        self._write_manifest('1.0.0')
        done = self._run(mode='feedback', env_extra={'DOSPLEX_TEST_PANEL_UID': '0'})
        self.assertEqual(done.returncode, 2)
        self.assertIn('The panel runs as a player\'s account, never as root.', done.stdout)

    def test_non_root_ok(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=0, env_file=str(self.env_file))
        self._write_manifest('1.0.0')
        done = self._run(mode='feedback', env_extra={'DOSPLEX_TEST_PANEL_UID': '1000'})
        self.assertNotEqual(done.returncode, 2)


class PrintContextTest(PanelLaunchBase):
    def test_all_files_present(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        manifest = self._write_manifest('2.5.1')
        os_release = self._write_os_release('4.1')
        product = self._write_product_name('Jupiter')
        self._write_loginusers(
            ' "users" {\n\t"76561198000000001" {\n\t\t"AccountName" "player1"\n\t\t"MostRecent" "1"\n\t\t"timestamp" "1700000000"\n\t}\n\t}\n'
        )
        # Pre-seed outbox with 3 entries
        outbox_dir = self.home / '.local' / 'share' / 'dosplex'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        self.outbox.write_text(
            json.dumps({'queued': 'a', 'payload': {}}) + '\n'
            + json.dumps({'queued': 'b', 'payload': {}}) + '\n'
            + json.dumps({'queued': 'c', 'payload': {}}) + '\n',
            encoding='utf-8',
        )
        done = self._run(
            mode='feedback', side='guest', page='settings', layout='handheld',
            extra_args=['--manifest', str(manifest), '--os-release', str(os_release),
                        '--product-name', str(product), '--print-context'],
        )
        self.assertEqual(done.returncode, 0)
        ctx = json.loads(done.stdout.strip())
        self.assertEqual(ctx['side'], 'guest')
        self.assertEqual(ctx['layout'], 'handheld')
        self.assertEqual(ctx['dosplex'], '2.5.1')
        self.assertEqual(ctx['steamos'], '4.1')
        self.assertEqual(ctx['device'], 'Steam Deck')
        self.assertEqual(ctx['steam_id'], '76561198000000001')
        self.assertEqual(ctx['outbox'], 3)

    def test_all_files_missing(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        # Point at non-existent paths
        missing = self.tmp / 'missing'
        done = self._run(
            mode='feedback', side='main',
            extra_args=[
                '--manifest', str(missing / 'manifest.json'),
                '--os-release', str(missing / 'os-release'),
                '--product-name', str(missing / 'product_name'),
                '--print-context',
            ],
        )
        self.assertEqual(done.returncode, 0)
        ctx = json.loads(done.stdout.strip())
        self.assertEqual(ctx['side'], 'main')
        self.assertEqual(ctx['layout'], 'unknown')
        self.assertEqual(ctx['dosplex'], 'unknown')
        self.assertEqual(ctx['steamos'], 'unknown')
        self.assertEqual(ctx['device'], 'unknown')
        self.assertEqual(ctx['steam_id'], '')
        self.assertEqual(ctx['outbox'], 0)


class DeviceNameTest(PanelLaunchBase):
    def _check_device(self, name, expected):
        product = self._write_product_name(name)
        done = self._run(
            mode='help',
            extra_args=['--product-name', str(product), '--print-context'],
        )
        self.assertEqual(done.returncode, 0)
        ctx = json.loads(done.stdout.strip())
        self.assertEqual(ctx['device'], expected, f'device for {name!r}')

    def test_jupiter(self):
        self._check_device('Jupiter', 'Steam Deck')

    def test_galileo(self):
        self._check_device('Galileo', 'Steam Deck OLED')

    def test_other_name(self):
        self._check_device('Custom Device', 'Custom Device')

    def test_empty_product(self):
        product = self._write_product_name('')
        done = self._run(
            mode='help',
            extra_args=['--product-name', str(product), '--print-context'],
        )
        self.assertEqual(done.returncode, 0)
        ctx = json.loads(done.stdout.strip())
        self.assertEqual(ctx['device'], 'unknown')


class SteamIdTest(PanelLaunchBase):
    def _get_steam_id(self, vdf_content):
        self._write_loginusers(vdf_content)
        done = self._run(
            mode='help',
            extra_args=['--print-context'],
        )
        self.assertEqual(done.returncode, 0)
        return json.loads(done.stdout.strip())['steam_id']

    def test_with_mostrecent(self):
        vdf = (
            ' "users" {\n'
            '\t"76561198000000001" {\n\t\t"AccountName" "a"\n\t\t"MostRecent" "1"\n\t\t"timestamp" "1700000000"\n\t}\n'
            '\t"76561198000000002" {\n\t\t"AccountName" "b"\n\t\t"MostRecent" "0"\n\t\t"timestamp" "1700000100"\n\t}\n'
            '\t}\n'
        )
        self.assertEqual(self._get_steam_id(vdf), '76561198000000001')

    def test_without_mostrecent_highest_timestamp(self):
        vdf = (
            ' "users" {\n'
            '\t"76561198000000001" {\n\t\t"AccountName" "a"\n\t\t"timestamp" "1700000000"\n\t}\n'
            '\t"76561198000000002" {\n\t\t"AccountName" "b"\n\t\t"timestamp" "1700000500"\n\t}\n'
            '\t}\n'
        )
        self.assertEqual(self._get_steam_id(vdf), '76561198000000002')

    def test_no_17_digit_key(self):
        vdf = ' "users" {\n\t"12345" {\n\t\t"AccountName" "a"\n\t\t"timestamp" "1700000000"\n\t}\n\t}\n'
        self.assertEqual(self._get_steam_id(vdf), '')

    def test_no_file(self):
        self.assertEqual(self._get_steam_id(None), '')


class CommandLineTest(PanelLaunchBase):
    def _check_cmd(self, mode, page='menu'):
        self._write_fake_qml(self.qml, lines=[], exit_code=0, env_file=str(self.env_file))
        manifest = self._write_manifest('1.0.0')
        done = self._run(
            mode=mode, page=page,
            extra_args=['--manifest', str(manifest)],
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        env = self._qml_env()
        self.assertIsNotNone(env, 'fake qml6 did not record env')
        argv = env['argv']
        # argv[0] is the fake qml path, argv[1] is Panel.qml, argv[2] is '--'
        self.assertEqual(argv[1], str(self.panel_dir / 'Panel.qml'))
        self.assertEqual(argv[2], '--')
        mode_flag = '--feedback' if mode == 'feedback' else '--help'
        self.assertEqual(argv[3], mode_flag)
        # Only Help takes a page; the form has none, and a stray word there would be read as an argument.
        self.assertEqual(argv[4], page if mode == 'help' else '--context')
        self.assertIn('--context', argv)
        self.assertIn('--manual-dir', argv)
        self.assertIn('--endpoint', argv)
        # Env vars
        self.assertEqual(env['env']['QML_XHR_ALLOW_FILE_READ'], '1')
        self.assertEqual(env['env']['QT_QUICK_CONTROLS_STYLE'], 'Basic')
        # Without this Qt sends the panel's one outbox line to the system journal and the launcher never sees it.
        self.assertEqual(env['env']['QT_FORCE_STDERR_LOGGING'], '1')

    def test_feedback_cmd(self):
        self._check_cmd('feedback')

    def _endpoint_seen(self, file_text=None, mode=0o644, owner=None, extra=()):
        self._write_fake_qml(self.qml, lines=[], exit_code=0, env_file=str(self.env_file))
        path = self.tmp / 'feedback-endpoint'
        if file_text is not None:
            path.write_text(file_text, encoding='utf-8')
            path.chmod(mode)
        done = self._run(mode='feedback', extra_args=['--endpoint-file', str(path), *extra],
                         env_extra={'DOSPLEX_TEST_ENDPOINT_OWNER': str(os.getuid() if owner is None else owner)})
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = self._qml_env()['argv']
        return argv[argv.index('--endpoint') + 1]

    def test_the_address_is_the_real_one_unless_root_wrote_another(self):
        real = 'https://feedback.dosplex.org/v1/feedback'
        self.assertEqual(self._endpoint_seen(), real)                                   # no file
        self.assertEqual(self._endpoint_seen('http://127.0.0.1:8787/v1/feedback\n'), 'http://127.0.0.1:8787/v1/feedback')
        self.assertEqual(self._endpoint_seen('http://127.0.0.1:8787/x\n', mode=0o666), real)   # others can write it
        self.assertEqual(self._endpoint_seen('http://127.0.0.1:8787/x\n', owner=os.getuid() + 1), real)  # not root's
        self.assertEqual(self._endpoint_seen('file:///etc/passwd\n'), real)
        self.assertEqual(self._endpoint_seen('http://a b\n'), real)
        self.assertEqual(self._endpoint_seen('http://127.0.0.1:1/x\n', extra=['--endpoint', 'http://127.0.0.1:2/y']),
                         'http://127.0.0.1:2/y')                                         # said outright wins

    def test_help_cmd(self):
        self._check_cmd('help')

    def test_feedback_cmd_page(self):
        self._check_cmd('feedback', page='settings')


class OutboxWriteTest(PanelLaunchBase):
    def test_outbox_line_written(self):
        outbox_json = json.dumps({'queued': 'my feedback text', 'payload': {'msg': 'hi'}})
        self._write_fake_qml(
            self.qml,
            lines=[f'DOSPLEX-OUTBOX {outbox_json}'],
            exit_code=0,
            env_file=str(self.env_file),
        )
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        # Check the file
        self.assertTrue(self.outbox.is_file(), 'outbox file not created')
        content = self.outbox.read_text(encoding='utf-8').strip()
        lines = [l for l in content.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry['queued'], 'my feedback text')
        self.assertEqual(entry['payload'], {'msg': 'hi'})
        # Privacy: outbox line must NOT appear in stdout or stderr
        self.assertNotIn('my feedback text', done.stdout)
        self.assertNotIn('my feedback text', done.stderr)

    def test_outbox_file_mode(self):
        outbox_json = json.dumps({'queued': 'x', 'payload': {}})
        self._write_fake_qml(self.qml, lines=[f'DOSPLEX-OUTBOX {outbox_json}'], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        if self.outbox.is_file():
            st = self.outbox.stat()
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            folder = self.outbox.parent
            fst = folder.stat()
            self.assertEqual(stat.S_IMODE(fst.st_mode), 0o700)

    def test_ordinary_line_goes_to_stderr(self):
        self._write_fake_qml(self.qml, lines=['hello from panel'], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('hello from panel', done.stderr)

    def test_malformed_outbox_line_dropped(self):
        # Not valid JSON after the marker
        self._write_fake_qml(self.qml, lines=['DOSPLEX-OUTBOX not-json'], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('not-json', done.stderr + done.stdout)       # broken or not, it is what a person wrote
        if self.outbox.is_file():
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 0)

    def test_overlong_outbox_line_dropped(self):
        # Make a line over 4096 bytes
        long_queued = 'x' * 4200
        outbox_json = json.dumps({'queued': long_queued, 'payload': {}})
        self._write_fake_qml(self.qml, lines=[f'DOSPLEX-OUTBOX {outbox_json}'], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('xxxxxxxx', done.stderr + done.stdout)
        if self.outbox.is_file():
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 0)

    def test_a_dosplex_folder_that_is_there_already_keeps_its_mode(self):
        # On a device ~/.local/share/dosplex exists (the desktop's files live in it) and is not this program's to close.
        folder = self.outbox.parent
        folder.mkdir(parents=True)
        folder.chmod(0o755)
        (folder / 'desktop').mkdir()
        line = 'DOSPLEX-OUTBOX ' + json.dumps({'queued': 'now', 'payload': {'text': 'hello'}})
        self._write_fake_qml(self.qml, lines=[line], exit_code=0)
        done = self._run(mode='feedback')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.outbox.stat().st_mode), 0o600)

    def test_a_link_on_the_way_to_the_outbox_is_not_written_through(self):
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        (self.home / '.local').mkdir()
        (self.home / '.local' / 'share').symlink_to(elsewhere)
        line = 'DOSPLEX-OUTBOX ' + json.dumps({'queued': 'now', 'payload': {'text': 'hello'}})
        self._write_fake_qml(self.qml, lines=[line], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_endless_or_huge_file_in_the_home_does_not_hang_the_launcher(self):
        folder = self.outbox.parent
        folder.mkdir(parents=True)
        self.outbox.symlink_to('/dev/zero')
        steam = self.home / '.local' / 'share' / 'Steam' / 'config'
        steam.mkdir(parents=True)
        (steam / 'loginusers.vdf').symlink_to('/dev/zero')
        done = self._run(mode='feedback', extra_args=['--print-context'], timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        context = json.loads(done.stdout)
        self.assertEqual((context['steam_id'], context['outbox']), ('', 0))
        self.outbox.unlink()
        with open(self.outbox, 'wb') as big:
            big.truncate(8 << 20)                                     # sparse: eight megabytes of nothing
        done = self._run(mode='feedback', extra_args=['--print-context'], timeout=10)
        self.assertEqual(json.loads(done.stdout)['outbox'], 0)

    def test_the_wayland_connection_the_compositor_hands_over_reaches_the_panel(self):
        import socket
        ours, theirs = socket.socketpair()
        os.set_inheritable(theirs.fileno(), True)
        fake = ('#!/usr/bin/python3\nimport os, socket\n'
                's = socket.socket(fileno=int(os.environ["WAYLAND_SOCKET"]))\ns.sendall(b"here")\n')
        self.qml.write_text(fake, encoding='utf-8')
        self.qml.chmod(0o755)
        args = [PYTHON, str(PROGRAM), '--mode', 'help', '--side', 'main', '--panel-dir', str(self.panel_dir),
                '--manual-dir', str(self.manual_dir), '--qml', str(self.qml), '--home', str(self.home)]
        done = subprocess.run(args, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, WAYLAND_SOCKET=str(theirs.fileno())), pass_fds=(theirs.fileno(),))
        theirs.close()
        self.assertEqual(done.returncode, 0, done.stderr)
        ours.settimeout(5)
        self.assertEqual(ours.recv(16), b'here')
        ours.close()

    def test_outbox_symlink_not_read_or_written(self):
        # Create a real file elsewhere, then symlink to it
        real_file = self.tmp / 'real_outbox.jsonl'
        real_file.write_text(json.dumps({'queued': 'old', 'payload': {}}) + '\n', encoding='utf-8')
        outbox_dir = self.home / '.local' / 'share' / 'dosplex'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real_file), str(self.outbox))
        outbox_json = json.dumps({'queued': 'new', 'payload': {}})
        self._write_fake_qml(self.qml, lines=[f'DOSPLEX-OUTBOX {outbox_json}'], exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        # The symlink should not have been replaced; the real file should be unchanged
        self.assertTrue(self.outbox.is_symlink(), 'symlink was replaced')
        self.assertEqual(
            real_file.read_text(encoding='utf-8').strip(),
            json.dumps({'queued': 'old', 'payload': {}}),
        )


class OutboxLimitTest(PanelLaunchBase):
    def test_more_than_20_keeps_newest(self):
        # Pre-seed 25 entries, then add 2 more via the panel
        outbox_dir = self.home / '.local' / 'share' / 'dosplex'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        existing = [json.dumps({'queued': f'old-{i}', 'payload': {}}) for i in range(25)]
        self.outbox.write_text('\n'.join(existing) + '\n', encoding='utf-8')
        # Panel prints 2 new entries
        lines = [
            f'DOSPLEX-OUTBOX {json.dumps({"queued": "new-0", "payload": {}})}',
            f'DOSPLEX-OUTBOX {json.dumps({"queued": "new-1", "payload": {}})}',
        ]
        self._write_fake_qml(self.qml, lines=lines, exit_code=0)
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 0, done.stderr)
        content = self.outbox.read_text(encoding='utf-8').strip()
        result_lines = [l for l in content.splitlines() if l.strip()]
        self.assertEqual(len(result_lines), 20)
        # The last two should be the new ones
        last = json.loads(result_lines[-1])
        self.assertEqual(last['queued'], 'new-1')
        second_last = json.loads(result_lines[-2])
        self.assertEqual(second_last['queued'], 'new-0')


class SendingTest(PanelLaunchBase):
    def _seed_outbox(self, n, prefix='wait'):
        outbox_dir = self.home / '.local' / 'share' / 'dosplex'
        outbox_dir.mkdir(parents=True, exist_ok=True)
        entries = [json.dumps({'queued': f'{prefix}-{i}', 'payload': {'n': i}}) for i in range(n)]
        self.outbox.write_text('\n'.join(entries) + '\n', encoding='utf-8')

    def _run_and_check(self, server, endpoint, mode='feedback', extra=None):
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        args = ['--endpoint', endpoint]
        if extra:
            args.extend(extra)
        done = self._run(mode=mode, extra_args=args, timeout=20)
        return done, server

    def test_200_removes_entry(self):
        self._seed_outbox(1, 'ok')
        server, endpoint = _start_server(status=200)
        try:
            done, server = self._run_and_check(server, endpoint)
            self.assertEqual(done.returncode, 0, done.stderr)
            if self.outbox.is_file():
                lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
                self.assertEqual(len(lines), 0, 'entry should be removed after 200')
            self.assertEqual(len(_Handler.received), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_400_removes_entry(self):
        self._seed_outbox(1, 'bad')
        server, endpoint = _start_server(status=400)
        try:
            done, server = self._run_and_check(server, endpoint)
            self.assertEqual(done.returncode, 0, done.stderr)
            if self.outbox.is_file():
                lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
                self.assertEqual(len(lines), 0, 'entry should be removed after 400')
        finally:
            server.shutdown()
            server.server_close()

    def test_429_keeps_entry(self):
        self._seed_outbox(1, 'retry')
        server, endpoint = _start_server(status=429)
        try:
            done, server = self._run_and_check(server, endpoint)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 1, 'entry should be kept after 429')
        finally:
            server.shutdown()
            server.server_close()

    def test_500_keeps_entry_and_later(self):
        self._seed_outbox(3, 'err')
        server, endpoint = _start_server(status=500)
        try:
            done, server = self._run_and_check(server, endpoint)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 3, 'all 3 entries should be kept after 500')
            # Only the first entry should have been tried
            self.assertEqual(len(_Handler.received), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_no_server_keeps_all(self):
        self._seed_outbox(2, 'offline')
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        # Use an address that nothing is listening on
        done = self._run(mode='feedback',
                         extra_args=['--endpoint', 'http://127.0.0.1:1/unused'],
                         timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
        self.assertEqual(len(lines), 2, 'all entries kept when no server')

    def test_help_mode_sends_nothing(self):
        self._seed_outbox(2, 'help')
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        server, endpoint = _start_server(status=200)
        try:
            done = self._run(mode='help', extra_args=['--endpoint', endpoint], timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(_Handler.received), 0, 'help mode should not send')
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 2, 'help mode should not remove entries')
        finally:
            server.shutdown()
            server.server_close()


class FormNotInTheReleaseTest(PanelLaunchBase):
    """A release without Feedback.qml: feedback mode opens nothing and sends nothing that waits; Help works."""

    def setUp(self):
        super().setUp()
        (self.panel_dir / 'Feedback.qml').unlink()
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        self.outbox.write_text(''.join(json.dumps({'queued': f'old-{i}', 'payload': {'n': i}}) + '\n'
                                       for i in range(2)), encoding='utf-8')
        self._write_fake_qml(self.qml, lines=[], exit_code=0, env_file=str(self.env_file))

    def test_feedback_mode_is_refused_and_the_outbox_stays(self):
        server, endpoint = _start_server(status=200)
        try:
            done = self._run(mode='feedback', extra_args=['--endpoint', endpoint], timeout=20)
            self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
            self.assertIn('The feedback form is not part of this release.', done.stdout)
            self.assertEqual(len(_Handler.received), 0, 'something was sent without the form')
            self.assertIsNone(self._qml_env(), 'the panel was started')
            self.assertEqual(len(self.outbox.read_text(encoding='utf-8').splitlines()), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_help_still_opens_and_sends_nothing(self):
        server, endpoint = _start_server(status=200)
        try:
            done = self._run(mode='help', extra_args=['--endpoint', endpoint], timeout=20)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn('--help', self._qml_env()['argv'])
            self.assertEqual(len(_Handler.received), 0)
            self.assertEqual(len(self.outbox.read_text(encoding='utf-8').splitlines()), 2)
        finally:
            server.shutdown()
            server.server_close()


class QmlMissingTest(PanelLaunchBase):
    def test_missing_qml(self):
        # Don't create the fake qml
        done = self._run(mode='feedback',
                         extra_args=['--qml', str(self.tmp / 'nonexistent-qml6')])
        self.assertEqual(done.returncode, 3)

    def test_missing_panel_qml(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=0)
        # Remove Panel.qml
        (self.panel_dir / 'Panel.qml').unlink()
        done = self._run(mode='feedback')
        self.assertEqual(done.returncode, 3)

    def test_fake_exit_status_7(self):
        self._write_fake_qml(self.qml, lines=[], exit_code=7, env_file=str(self.env_file))
        done = self._run(mode='feedback', extra_args=['--endpoint', 'http://127.0.0.1:1/unused'])
        self.assertEqual(done.returncode, 7, done.stderr)


class TimeLimitTest(PanelLaunchBase):
    def test_timeout_kills_sleeping_fake(self):
        # Fake qml6 prints an outbox line, then sleeps for 30 seconds
        outbox_json = json.dumps({'queued': 'before-sleep', 'payload': {}})
        self._write_fake_qml(
            self.qml,
            lines=[f'DOSPLEX-OUTBOX {outbox_json}'],
            exit_code=0,
            sleep_seconds=30,
        )
        start = time.monotonic()
        done = self._run(
            mode='feedback',
            extra_args=['--endpoint', 'http://127.0.0.1:1/unused'],
            env_extra={'DOSPLEX_TEST_PANEL_SECONDS': '1'},
            timeout=15,
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10, f'took too long: {elapsed:.1f}s')
        # The outbox line printed before the sleep should be saved
        if self.outbox.is_file():
            lines = [l for l in self.outbox.read_text(encoding='utf-8').splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry['queued'], 'before-sleep')
        else:
            self.fail('outbox file not created after timeout')


if __name__ == '__main__':
    unittest.main()
