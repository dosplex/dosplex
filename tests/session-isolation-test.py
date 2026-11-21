#!/usr/bin/env python3
# dosplex-test: tier=integration needs=systemd-user,ordinary-user timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Real unprivileged systemd services: cgroup cleanup and separate runtimes."""
import importlib.util
import contextlib
import io
from unittest.mock import patch
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import threading
import socket
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

auth_spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(auth_spec)
auth_spec.loader.exec_module(auth)


def wait_for(predicate, label, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out: ' + label)


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


def supervisor_flow(root, probe):
    host = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = root / 'host.sock'
    host.bind(str(socket_path))
    state_path = root / 'display.json'
    entry = dict(width=960, height=1080, game_width=1280, game_height=720,
                 scale_mode='fit', display_revision=0)
    display = dict(layout='vertical', left=dict(entry), right=dict(entry))
    module.atomic_json(state_path, display)
    sessions, workers, errors = [], [], []
    audio_keys = {}
    def factory(side, owner, command, environment):
        audio_keys.setdefault(side, set()).update(key for key in environment if key.endswith('AUDIO_SERVER'))
        environment.update(CAPTURE=str(root / (side + '-flow.json')), LITERAL='flow')
        session = module.Session(side, owner, ['/usr/bin/python3', str(probe), 'flow'],
                                 environment, user_manager=True)
        sessions.append(session)
        return session
    def record(side):
        try:
            return json.loads((root / (side + '-flow.json')).read_text())
        except (OSError, ValueError):
            return {}
    def changed(side, previous):
        current = record(side).get('pid')
        return current is not None and current != previous
    def start(side):
        stop = threading.Event()
        def run():
            try:
                module.supervise(side, pwd.getpwuid(os.getuid()).pw_name,
                    root / side, state_path, socket_path, root, stop,
                    session_factory=factory, role='primary' if side == 'left' else 'guest',
                    launch_token=('a' if side == 'left' else 'b') * 32)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        workers.append((stop, thread))
        thread.start()
    try:
        start('left')
        start('right')
        wait_for(lambda: record('left') and record('right'), 'both supervisor launches')
        left, right = record('left'), record('right')
        for side, token in (('left', 'a' * 32), ('right', 'b' * 32)):
            assert json.loads((root / side / 'session.json').read_text())['launch_token'] == token
        # Corrupt display state must preserve both existing activities.
        state_path.write_text('{')
        time.sleep(.8)
        assert alive(left['pid']) and alive(right['pid'])
        display['left']['display_revision'] = 1
        module.atomic_json(state_path, display)
        wait_for(lambda: changed('left', left['pid']), 'left display apply')
        assert not alive(left['pid']) and not alive(left['child'])
        assert alive(right['pid']) and alive(right['child'])
        assert not Path(left['runtime']).exists()
        left = record('left')
        left_instance = json.loads((root / 'left/session.json').read_text())['instance']
        # Contract version 2 (ADR-012): the same kind of change leaves both real services alone. A
        # side launched under version 1 keeps restarting, so the left side is launched again first.
        restarts = json.loads((root / 'left/session.json').read_text())['restart_count']
        def left_revision():
            try:
                return module.client_display_get(str(root / 'left/control.sock'), left_instance,
                                                 server_uid=os.getuid())['display_revision']
            except (OSError, RuntimeError):
                return None
        display['contract_version'] = 2
        module.atomic_json(state_path, display)
        module.request_change('left', 'restart', side_root=root)
        wait_for(lambda: changed('left', left['pid']), 'left launched under version 2')
        left = record('left')
        assert alive(left['pid']) and alive(right['pid'])
        display['left'].update(display_revision=2, width=1280, scale_mode='stretch')
        module.atomic_json(state_path, display)
        wait_for(lambda: left_revision() == 2, 'left supervisor reads the version 2 change')
        time.sleep(.8)
        assert alive(left['pid']) and alive(left['child']), 'a version 2 display change stopped the left service'
        assert record('left')['pid'] == left['pid'], 'a version 2 display change restarted the left service'
        assert alive(right['pid']) and alive(right['child'])
        assert json.loads((root / 'left/session.json').read_text())['restart_count'] == restarts + 1
        module.client_mode('desktop', str(root / 'left/control.sock'), left_instance,
                           server_uid=os.getuid())
        wait_for(lambda: changed('left', left['pid']), 'left activity switch')
        assert alive(right['pid'])
        left = record('left')
        module.request_change('right', 'restart', side_root=root)
        wait_for(lambda: changed('right', right['pid']), 'right restart request')
        assert alive(left['pid'])
        right = record('right')
        # Verify side-local stop before global layout transitions, which may
        # restart the primary or be coalesced before it observes an intermediate mode.
        module.request_change('right', 'stop', side_root=root)
        workers[1][1].join(timeout=10)
        assert not workers[1][1].is_alive(), 'stop request did not exit supervisor'
        assert not alive(right['pid']) and not alive(right['child'])
        assert not Path(right['runtime']).exists()
        assert alive(left['pid']) and alive(left['child'])
        stopped = json.loads((root / 'right/session.json').read_text())
        assert stopped['lifecycle'] == 'stopped' and not stopped['active']
        assert not (root / 'right/control.sock').exists()
        # A stop left on disk must never stop a future session on the same side.
        start('right')
        wait_for(lambda: changed('right', right['pid']), 'new session ignores stale stop')
        right = record('right')
        time.sleep(.5)
        assert alive(right['pid']) and alive(left['pid'])
        current = json.loads((root / 'right/session.json').read_text())
        assert current['instance'] != stopped['instance']
        display['layout'] = 'single'
        module.atomic_json(state_path, display)
        wait_for(lambda: json.loads((root / 'right/session.json').read_text())['lifecycle'] == 'standby', 'guest frozen in single layout')
        assert alive(right['pid']) and alive(right['child'])
        display['layout'] = 'vertical'
        module.atomic_json(state_path, display)
        wait_for(lambda: json.loads((root / 'right/session.json').read_text())['lifecycle'] == 'running', 'guest thawed in split layout')
        assert record('right')['pid'] == right['pid']
        right = record('right')
        # Stop must also work while the guest is waiting in single-screen mode.
        display['layout'] = 'single'
        module.atomic_json(state_path, display)
        wait_for(lambda: json.loads((root / 'right/session.json').read_text())['lifecycle'] == 'standby', 'guest standby before stop')
        assert alive(right['pid'])
        with patch.object(auth, 'SIDE_ROOT', root), \
             patch.object(auth, 'revoke_wayland_access') as revoke, \
             contextlib.redirect_stdout(io.StringIO()):
            observed = json.loads((root / 'right/session.json').read_text())
            auth.logout_side('right', observed['owner'], observed['instance'])
        revoke.assert_called_once_with(pwd.getpwuid(os.getuid()).pw_name)
        assert not (root / 'right/session.json').exists()
        workers[2][1].join(timeout=10)
        assert not workers[2][1].is_alive(), 'sleeping supervisor ignored stop'
        # This second global layout transition can also restart the primary.
        wait_for(lambda: alive(record('left').get('pid', 0)), 'primary active after single layout')

        # A login may finish after Full was selected. Its new supervisor must
        # retain the claim without launching a hidden guest activity.
        right_session_count = sum('session-right-' in session.unit for session in sessions)
        start('right')
        def sleeping_guest():
            try:
                return json.loads((root / 'right/session.json').read_text()).get('lifecycle') == 'sleeping'
            except (OSError, ValueError):
                return False
        wait_for(sleeping_guest, 'late guest login waits in full layout')
        waiting = json.loads((root / 'right/session.json').read_text())
        time.sleep(.6)
        assert workers[3][1].is_alive()
        assert not alive(right['pid'])
        assert not waiting['active'] and not waiting['unit']
        assert sum('session-right-' in session.unit for session in sessions) == right_session_count
        display['layout'] = 'vertical'
        module.atomic_json(state_path, display)
        wait_for(lambda: changed('right', right['pid']), 'late guest starts after split restored')
        resumed = json.loads((root / 'right/session.json').read_text())
        assert resumed['instance'] == waiting['instance'] and resumed['owner'] == waiting['owner']
        assert alive(record('right')['pid'])
        # Main is offered the shared audio server; Guest is only ever offered its own endpoint.
        assert audio_keys == {'left': {'DOSPLEX_AUDIO_SERVER'}, 'right': {'DOSPLEX_GUEST_AUDIO_SERVER'}}, audio_keys
        assert not errors, errors

    finally:
        for stop, thread in workers:
            stop.set()
        for stop, thread in workers:
            thread.join(timeout=20)
            assert not thread.is_alive()
        host.close()
        for session in sessions:
            session.stop()
            assert not session.runtime.exists()
    assert not errors, errors


def launcher_flow(root):
    binaries = root / 'bin'
    binaries.mkdir()
    steam = binaries / 'steam'
    steam.write_text('#!/bin/sh\nexit 91\n')
    steam.chmod(0o755)
    gamescope = binaries / 'gamescope'
    gamescope.write_text('''#!/usr/bin/python3
import json, os, pathlib, socket, time
runtime = pathlib.Path(os.environ['XDG_RUNTIME_DIR'])
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.bind(str(runtime / os.environ['GAMESCOPE_WAYLAND_DISPLAY']))
for attempt in range(100):
    if (runtime / 'wayland-0').is_socket(): break
    time.sleep(.05)
else: raise RuntimeError('session compatibility alias missing')
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(dict(os.environ)))
while True: time.sleep(1)
''')
    gamescope.chmod(0o755)
    sessions = []
    try:
        for side in ('left', 'right'):
            home = root / (side + '-home')
            home.mkdir()
            capture = root / (side + '-launcher.json')
            session = module.Session(side, pwd.getpwuid(os.getuid()).pw_name,
                ['bash', str(ROOT / 'guest/dosplex-user-side-session.sh'), side, 'tester'],
                {'PATH': str(binaries) + ':/usr/bin:/bin', 'HOME': str(home),
                 'CAPTURE': str(capture), 'DOSPLEX_SESSION_PAYLOAD': 'steam',
                 **({'DOSPLEX_AUDIO_SERVER': 'unix:' + str(root / 'audio.sock')} if side == 'left' else {}),
                 'DOSPLEX_SIDE': side,
                 'DOSPLEX_SESSION_CONTROL': str(root / (side + '-control.sock')),
                 'DOSPLEX_SESSION_INSTANCE': ('a' if side == 'left' else 'b') * 32,
                 'DOSPLEX_SESSION_CONTROL_CLIENT': str(ROOT / 'packaging/steamos/dosplex-side-supervisor.py'),
                 'DOSPLEX_SHIM_SCRIPT': str(ROOT / 'packaging/steamos/steamos-session-select-shim.sh'),
                 'WAYLAND_DISPLAY': str(root / 'host.sock')}, user_manager=True)
            sessions.append(session)
            session.start()
            wait_for(capture.exists, 'actual session launcher ' + side)
            env = json.loads(capture.read_text())
            assert env['XDG_RUNTIME_DIR'] == str(session.runtime)
            assert env['XAUTHORITY'] == str(session.runtime / 'Xauthority')
            assert env['DBUS_SESSION_BUS_ADDRESS']
            assert env['PULSE_SERVER'] == ('unix:' + str(root / 'audio.sock') if side == 'left' else '127.0.0.1')
            assert env['PULSE_SINK'] == 'dosplex_' + side
            assert env['PULSE_SOURCE'] == 'dosplex_' + side + '_mic.monitor'
            assert (session.runtime / 'wayland-0').readlink() == Path('wayland-gamescope-' + side)
            assert (session.runtime / 'shims/steamos-session-select').resolve() == ROOT / 'packaging/steamos/steamos-session-select-shim.sh'
            assert not (home / '.Xauthority').exists()
    finally:
        for session in sessions:
            session.stop()
            assert not session.runtime.exists()


def main():
    if os.geteuid() == 0:
        raise RuntimeError('Run this isolation test as an ordinary user')
    subprocess.run(['systemctl', '--user', 'show-environment'], check=True, stdout=subprocess.DEVNULL)
    sessions = []
    outsider = subprocess.Popen(['sleep', '120'])
    with tempfile.TemporaryDirectory(prefix='dosplex-service-test-') as directory:
        root = Path(directory)
        probe = root / 'probe.py'
        probe.write_text('''import json, os, pathlib, sys, time
child = os.fork()
if child == 0:
    os.setsid()  # Escape process-group cleanup; remain in the owning cgroup.
    while True: time.sleep(1)
runtime = pathlib.Path(os.environ['XDG_RUNTIME_DIR'])
(runtime / 'sentinel').write_text(sys.argv[1])
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
    'pid': os.getpid(), 'child': child, 'runtime': str(runtime),
    'literal': os.environ['LITERAL'], 'argument': sys.argv[1],
    'bus': os.environ.get('DBUS_SESSION_BUS_ADDRESS'),
}))
while True: time.sleep(1)
''')
        records = []
        try:
            literal = 'spaces $HOME $(touch should-not-exist) `false` %h %u'
            for side in ('left', 'right'):
                capture = root / (side + '.json')
                session = module.Session(side, pwd.getpwuid(os.getuid()).pw_name,
                    ['/usr/bin/python3', str(probe), literal],
                    {'PATH': '/usr/bin:/bin', 'CAPTURE': str(capture), 'LITERAL': literal},
                    user_manager=True)
                sessions.append(session)
                session.start()
                wait_for(capture.exists, side + ' startup')
                record = json.loads(capture.read_text())
                records.append(record)
                assert record['literal'] == literal and record['argument'] == literal
                assert record['bus'] is None
                assert Path(record['runtime']).stat().st_mode & 0o777 == 0o700
            assert records[0]['runtime'] != records[1]['runtime']
            sessions[0].stop()
            wait_for(lambda: not alive(records[0]['pid']) and not alive(records[0]['child']), 'left cgroup cleanup')
            assert not Path(records[0]['runtime']).exists()
            assert alive(records[1]['pid']) and alive(records[1]['child'])
            assert (Path(records[1]['runtime']) / 'sentinel').read_text() == literal
            assert outsider.poll() is None
            sessions[1].stop()
            wait_for(lambda: not alive(records[1]['pid']) and not alive(records[1]['child']), 'right cgroup cleanup')
            assert not Path(records[1]['runtime']).exists()
            assert outsider.poll() is None
            supervisor_flow(root, probe)
            launcher_flow(root)
        finally:
            for session in sessions:
                session.stop()
            outsider.terminate()
            outsider.wait(timeout=5)
    print('PASS: real systemd isolation, owned cleanup, private runtimes, literal argv, display apply and activity/restart flows')


if __name__ == '__main__':
    main()
