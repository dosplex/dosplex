#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,xvfb,wayland-client,cc,ordinary-user timeout=120
# dosplex-run: left {compositor} left
# dosplex-run: right {compositor} right
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise actual nested X11 output setup without touching the desktop."""
import json
import os
import pwd
import shlex
import re
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time


def fit_16_9(width, height):
    """The largest 16:9 size that fits a region: the default picture of a side in either split."""
    scale = min(width // 16, height // 9)
    return 16 * scale, 9 * scale


def main():
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    side = sys.argv[2] if len(sys.argv) > 2 else 'left'
    assert side in ('left', 'right')
    with tempfile.TemporaryDirectory(prefix='dosplex-x11-test-') as directory:
        root = Path(directory)
        project = Path(__file__).resolve().parents[1]
        protocols = subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip()
        # The window needs xdg-shell; the locked pointer needs pointer constraints and relative pointer.
        generated = []
        for xml, header, code in [
                ('stable/xdg-shell/xdg-shell.xml', 'xdg-shell-client-protocol.h', 'xdg-shell-protocol.c'),
                ('unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
                 'pointer-constraints-unstable-v1-client-protocol.h', 'pointer-constraints-protocol.c'),
                ('unstable/relative-pointer/relative-pointer-unstable-v1.xml',
                 'relative-pointer-unstable-v1-client-protocol.h', 'relative-pointer-protocol.c')]:
            for mode, filename in [('client-header', header), ('private-code', code)]:
                subprocess.run(['wayland-scanner', mode, str(Path(protocols) / xml), str(root / filename)],
                               check=True, timeout=5)
            generated.append(str(root / code))
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
        subprocess.run(['cc', '-I', directory, str(project / 'tools/dosplex-keyprobe.c'), *generated,
            '-o', str(root / 'probe'), *flags], check=True, timeout=15)
        read_fd, write_fd = os.pipe()
        processes = []
        try:
            with (root / 'xvfb.log').open('w') as xlog, (root / 'compositor.log').open('w') as log:
                server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
                    '1280x720x24', '-nolisten', 'tcp', '-ac'], pass_fds=(write_fd,),
                    stdout=xlog, stderr=xlog)
                processes.append(server)
                os.close(write_fd)
                write_fd = None
                if not select.select([read_fd], [], [], 5)[0]:
                    raise RuntimeError('Xvfb startup timed out')
                display = os.read(read_fd, 64).decode().strip()
                assert display.isdecimal()
                env = {'PATH': '/usr/bin:/bin', 'HOME': directory, 'XDG_RUNTIME_DIR': directory,
                       'DOSPLEX_RUNTIME_DIR': directory, 'WLR_RENDERER': 'pixman',
                       'DISPLAY': ':' + display, 'DOSPLEX_INITIAL_LAYOUT': 'vertical',
                       'WAYLAND_DISPLAY': 'dosplex-0', 'DOSPLEX_INPUT_DEFAULT_SIDE': 'follow',
                       'DOSPLEX_LEFT_USER': pwd.getpwuid(os.getuid()).pw_name if side == 'left' else 'dosplex-unassigned',
                       'DOSPLEX_RIGHT_USER': pwd.getpwuid(os.getuid()).pw_name if side == 'right' else 'dosplex-unassigned'}
                # The nested product's policy: test hooks for the DEV_FRAME barrier, host keyboards as menu controls.
                compositor = subprocess.Popen([binary, '-b', 'x11', '--test-hooks', '--host-keyboards'],
                                              env=env, stdout=log, stderr=log)
                processes.append(compositor)
                deadline = time.monotonic() + 5
                state_path = root / 'state.json'
                while not state_path.exists():
                    if compositor.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError((root / 'compositor.log').read_text())
                    time.sleep(.05)
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                    control.sendto(b'OVERLAY left', str(root / 'hud-ipc.sock'))
                time.sleep(.5)  # include asynchronous output/input events and first overlay frames
                assert compositor.poll() is None, (root / 'compositor.log').read_text()
                state = json.loads(state_path.read_text())
                assert state['output_connected']
                assert state['left']['width'] == state['right']['width'] > 0
                # Contract version 2: the version leads the file, and the
                # pending flag has its new name (a display change never restarts).
                assert next(iter(state)) == 'contract_version' and state['contract_version'] == 2, state
                for name in ('left', 'right'):
                    assert state[name]['display_pending'] is False, state
                    assert 'scale_restart_required' not in state[name], state

                def read_state():
                    return json.loads(state_path.read_text())

                def ipc(command):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                        control.sendto(command.encode(), str(root / 'hud-ipc.sock'))

                def wait_state(predicate, label):
                    deadline = time.monotonic() + 3
                    while True:
                        current = read_state()
                        if predicate(current):
                            return current
                        assert time.monotonic() < deadline, f'{label}: {current}'
                        time.sleep(.02)
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                    control.sendto(b'DEV_FRAME 42', str(root / 'hud-ipc.sock'))
                deadline = time.monotonic() + 3
                while json.loads(state_path.read_text()).get('capture_ready') != 42:
                    assert time.monotonic() < deadline, 'presentation barrier did not complete'
                    time.sleep(.02)
                tree = subprocess.check_output(['xwininfo', '-root', '-tree'],
                    env=env, text=True, timeout=3)
                # With no window manager, Xvfb has exactly our one child. EWMH
                # window naming is not available, so identify that child here.
                windows = re.findall(r'^\s+(0x[0-9a-f]+) .*: .*\d+x\d+', tree, re.MULTILINE)
                assert len(windows) == 1
                for width, height in ((960, 540), (1280, 720)):
                    previous = read_state()
                    subprocess.run(['xdotool', 'windowsize', windows[0], str(width), str(height)],
                                   env=env, check=True, timeout=3)
                    deadline = time.monotonic() + 3
                    while True:
                        state = json.loads(state_path.read_text())
                        if (state['left']['width'], state['left']['height']) == (width // 2, height):
                            break
                        assert time.monotonic() < deadline, state
                        time.sleep(.05)
                    # A split's default is the largest 16:9 that fits the region, so an output change
                    # moves each side's game monitor and advances its display revision.
                    for name in ('left', 'right'):
                        assert (state[name]['game_aspect'], state[name]['native_resolution']) == ('16:9', False), state
                        assert (state[name]['game_width'], state[name]['game_height']) == fit_16_9(width // 2, height), state
                        assert state[name]['display_revision'] > previous[name]['display_revision'], (previous, state)
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                    control.sendto(b'OVERLAY close', str(root / 'hud-ipc.sock'))
                with (root / 'probe.log').open('w') as probe_log:
                    probe = subprocess.Popen([str(root / 'probe'), '--test-control'], env=env,
                                             stdin=subprocess.PIPE, stdout=probe_log, stderr=probe_log)
                    processes.append(probe)
                    def wait_probe(text):
                        deadline = time.monotonic() + 5
                        while text not in (root / 'probe.log').read_text():
                            assert probe.poll() is None, (root / 'probe.log').read_text()
                            assert time.monotonic() < deadline, (root / 'probe.log').read_text()
                            time.sleep(.02)
                    wait_probe('CONFIGURED')
                    def mouse(x, y):
                        subprocess.run(['xdotool', 'mousemove', '--window', windows[0], str(x), str(y)],
                            env=env, check=True, timeout=3)
                    capture_token = 100
                    def cursor_patches():
                        nonlocal capture_token
                        capture_token += 1
                        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                            control.sendto(f'DEV_FRAME {capture_token}'.encode(), str(root / 'hud-ipc.sock'))
                        deadline = time.monotonic() + 3
                        while json.loads(state_path.read_text())['capture_ready'] != capture_token:
                            assert time.monotonic() < deadline
                            time.sleep(.02)
                        shot = root / 'cursor.xwd'
                        subprocess.run(['xwd', '-display', env['DISPLAY'], '-id', windows[0], '-silent', '-out', str(shot)],
                            env=env, check=True, timeout=3)
                        raw = shot.read_bytes()
                        header = struct.unpack('>25I', raw[:100])
                        assert header[1:3] == (7, 2) and header[11] == 32
                        offset = header[0] + header[19] * 12
                        def patch(x):
                            return b''.join(raw[offset + y * header[12] + x * 4:
                                offset + y * header[12] + (x + 48) * 4] for y in range(390, 438))
                        return patch(190), patch(830)
                    baseline = cursor_patches()
                    # An actual rendered frame must return after repeated
                    # null-buffer attachments, not merely has_app metadata.
                    def wait_remap(predicate):
                        deadline = time.monotonic() + 5
                        while not predicate():
                            assert probe.poll() is None, (root / 'probe.log').read_text()
                            assert time.monotonic() < deadline, 'remap did not converge'
                            time.sleep(.02)
                    for _ in range(2):
                        probe.stdin.write(b'U'); probe.stdin.flush()
                        wait_remap(lambda: not json.loads(state_path.read_text())[side]['has_app'])
                        assert cursor_patches() != baseline, 'unmapped image remained visible'
                        count = (root / 'probe.log').read_text().count('TOPLEVEL_CONFIGURE ')
                        probe.stdin.write(b'N'); probe.stdin.flush()
                        wait_remap(lambda: (root / 'probe.log').read_text().count('TOPLEVEL_CONFIGURE ') > count)
                        time.sleep(.1)
                        probe.stdin.write(b'C'); probe.stdin.flush()
                        wait_remap(lambda: json.loads(state_path.read_text())[side]['has_app'])
                        assert cursor_patches() == baseline, 'remapped image was not restored'
                    mouse(200, 400)
                    left_cursor = cursor_patches()
                    mouse(840, 400)
                    both_cursors = cursor_patches()
                    # A side with a mapped application draws its own cursor (nested
                    # Gamescope or KWin), so Dosplex's marker stays hidden there. The
                    # empty side gets the marker and keeps it when the pointer leaves.
                    if side == 'left':
                        assert left_cursor[0] == baseline[0], 'marker drawn over the mapped application'
                        assert both_cursors[0] == baseline[0], 'marker appeared over the mapped application'
                        assert both_cursors[1] != baseline[1], 'right cursor not drawn'
                    else:
                        assert left_cursor[0] != baseline[0], 'left cursor not drawn'
                        assert both_cursors[0] == left_cursor[0], 'moving right erased or moved left cursor'
                        assert both_cursors[1] == baseline[1], 'marker drawn over the mapped application'
                    x = 100 if side == 'left' else 740
                    mouse(x, 100)
                    wait_probe('POINTER_ENTER')
                    mouse(x + 20, 120)
                    wait_probe('POINTER_MOTION x=120.00 y=120.00')
                    # Pin the backend mouse to this side, move an independent
                    # remote pointer elsewhere, then click without mouse motion.
                    # A side's buffer is never handed to the X11 output (the host would keep it until a later present,
                    # and with frames on demand none comes: the Steam Machine's games stood still while loading).
                    assert 'Direct scan-out of a side\'s buffer: off (X11 output)' in (root / 'compositor.log').read_text()
                    device = re.search(r'Attached routed pointer device=(.*?) role=', (root / 'compositor.log').read_text())
                    assert device, (root / 'compositor.log').read_text()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                        control.sendto(f'ROUTE {device[1]} {side}'.encode(), str(root / 'hud-ipc.sock'))
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote:
                        remote.connect(str(root / 'dosplex-primary-input.sock'))
                        remote.sendall(f'M {740 if side == "left" else 100} 200\n'.encode())
                        time.sleep(.05)
                        start = len((root / 'probe.log').read_text())
                        subprocess.run(['xdotool', 'click', '1'], env=env, check=True, timeout=3)
                        wait_probe('POINTER_BUTTON button=272 state=0')
                        clicks = (root / 'probe.log').read_text()[start:]
                        positions = re.findall(r'POINTER_BUTTON button=272 state=[01] x=([0-9.]+) y=([0-9.]+)', clicks)
                        assert positions == [('120.00', '120.00'), ('120.00', '120.00')], clicks
                        # Reverse the two input paths: a remote click must not
                        # borrow the local mouse's position on the other side.
                        remote.sendall(f'M {500 if side == "left" else 1140} 600\n'.encode())
                        wait_probe('POINTER_MOTION x=500.00 y=600.00')
                        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                            control.sendto(f'ROUTE {device[1]} follow'.encode(), str(root / 'hud-ipc.sock'))
                        mouse(740 if side == 'left' else 100, 200)
                        remote.sendall(b'B 272 1\nB 272 0\n')
                        wait_probe('POINTER_BUTTON button=272 state=0 x=500.00 y=600.00')
                        # A smaller region clamps the saved local position.
                        subprocess.run(['xdotool', 'windowsize', windows[0], '960', '540'],
                            env=env, check=True, timeout=3)
                        deadline = time.monotonic() + 3
                        while json.loads(state_path.read_text())[side]['width'] != 480:
                            assert time.monotonic() < deadline
                            time.sleep(.02)
                        cursor_patches()  # wait for resized scene presentation
                        remote.sendall(b'B 272 1\nB 272 0\n')
                        wait_probe('POINTER_BUTTON button=272 state=0 x=479.00 y=539.00')
                        subprocess.run(['xdotool', 'windowsize', windows[0], '1280', '720'],
                            env=env, check=True, timeout=3)
                        deadline = time.monotonic() + 3
                        while json.loads(state_path.read_text())[side]['width'] != 640:
                            assert time.monotonic() < deadline
                            time.sleep(.02)
                        cursor_patches()  # state geometry precedes rendered scene
                        remote.sendall(f'M {123 if side == "left" else 763} 123\n'.encode())
                        wait_probe('POINTER_MOTION x=123.00 y=123.00')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                        control.sendto(f'ROUTE {device[1]} follow'.encode(), str(root / 'hud-ipc.sock'))
                    mouse(x + 20, 120)
                    subprocess.run(['xdotool', 'click', '5'], env=env, check=True, timeout=3)
                    wait_probe('POINTER_AXIS axis=0 value=15.00')
                    count = (root / 'probe.log').read_text().count('POINTER_AXIS')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                        control.sendto(('OVERLAY ' + side).encode(), str(root / 'hud-ipc.sock'))
                    deadline = time.monotonic() + 3
                    while not json.loads(state_path.read_text())['overlay_visible']:
                        assert time.monotonic() < deadline
                        time.sleep(.02)
                    subprocess.run(['xdotool', 'click', '5'], env=env, check=True, timeout=3)
                    time.sleep(.1)
                    assert (root / 'probe.log').read_text().count('POINTER_AXIS') == count, 'overlay leaked scrolling'
                    mouse(740 if side == 'left' else 100, 100)
                    subprocess.run(['xdotool', 'click', '5'], env=env, check=True, timeout=3)
                    time.sleep(.1)
                    assert (root / 'probe.log').read_text().count('POINTER_AXIS') == count, 'other side leaked scrolling'
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                        control.sendto(b'OVERLAY close', str(root / 'hud-ipc.sock'))
                    mouse(x, 100)
                    subprocess.run(['xdotool', 'key', 'a'], env=env, check=True, timeout=3)
                    wait_probe('key=30 state=1')
                    before_switch = len((root / 'probe.log').read_text())
                    mouse(740 if side == 'left' else 100, 100)
                    subprocess.run(['xdotool', 'key', 'Shift_L+z'], env=env, check=True, timeout=3)
                    wait_probe('KEYBOARD_LEAVE')
                    after_leave = (root / 'probe.log').read_text()[before_switch:]
                    assert not re.search(r'KEY serial=.*key=(42|44) ', after_leave), after_leave
                    assert not re.search(r'MODIFIERS .*depressed=[1-9]', after_leave), after_leave
                    mouse(x, 100)
                    subprocess.run(['xdotool', 'key', 'b'], env=env, check=True, timeout=3)
                    wait_probe('key=48 state=1')
                    # A key held on the other side cannot enter this client
                    # when the keyboard follows the pointer back here.
                    mouse(740 if side == 'left' else 100, 100)
                    subprocess.run(['xdotool', 'keydown', 'Shift_L'], env=env, check=True, timeout=3)
                    boundary = len((root / 'probe.log').read_text())
                    mouse(x, 100)
                    subprocess.run(['xdotool', 'key', 'c'], env=env, check=True, timeout=3)
                    subprocess.run(['xdotool', 'keyup', 'Shift_L'], env=env, check=True, timeout=3)
                    subprocess.run(['xdotool', 'key', 'd'], env=env, check=True, timeout=3)
                    wait_probe('key=32 state=1')
                    held = (root / 'probe.log').read_text()[boundary:]
                    assert not re.search(r'KEY serial=.*key=46 ', held), held
                    assert not re.search(r'MODIFIERS .*depressed=[1-9]', held), held
                    subprocess.run(['xdotool', 'keydown', 'w'], env=env, check=True, timeout=3)
                    wait_probe('key=17 state=1')
                    boundary = len((root / 'probe.log').read_text())
                    subprocess.run(['xdotool', 'key', 'ctrl+shift+m'], env=env, check=True, timeout=3)
                    deadline = time.monotonic() + 3
                    while not json.loads(state_path.read_text())['overlay_visible']:
                        assert time.monotonic() < deadline, 'keyboard shortcut did not open overlay'
                        time.sleep(.02)
                    subprocess.run(['xdotool', 'key', 'Escape'], env=env, check=True, timeout=3)
                    deadline = time.monotonic() + 3
                    while json.loads(state_path.read_text())['overlay_visible']:
                        assert time.monotonic() < deadline, 'Escape did not close overlay'
                        time.sleep(.02)
                    subprocess.run(['xdotool', 'key', 'c'], env=env, check=True, timeout=3)
                    subprocess.run(['xdotool', 'keyup', 'w'], env=env, check=True, timeout=3)
                    subprocess.run(['xdotool', 'key', 'f'], env=env, check=True, timeout=3)
                    wait_probe('key=33 state=1')
                    menu_events = (root / 'probe.log').read_text()[boundary:]
                    assert 'KEYBOARD_LEAVE' in menu_events, menu_events
                    assert not re.search(r'KEY serial=.*key=(1|46|50) ', menu_events), menu_events
                    assert not re.search(r'KEY serial=.*key=17 state=0', menu_events), menu_events
                    # Pointer lock, the grab across sides and the layout re-home, with the backend mouse. The
                    # backend only reports positions: a locked client gets the motion worked out from them.
                    def probe_text():
                        return (root / 'probe.log').read_text()

                    def probe_do(letter):
                        probe.stdin.write(letter.encode())
                        probe.stdin.flush()

                    def wait_since(position, pattern, timeout=5):
                        deadline = time.monotonic() + timeout
                        while True:
                            found = re.search(pattern, probe_text()[position:], re.M)
                            if found:
                                return found
                            assert probe.poll() is None, probe_text()
                            assert time.monotonic() < deadline, f'never saw {pattern!r}:\n{probe_text()[position:][-3000:]}'
                            time.sleep(.02)

                    def never_since(position, pattern, settle=.3):
                        time.sleep(settle)
                        found = re.search(pattern, probe_text()[position:], re.M)
                        assert not found, f'saw {pattern!r}:\n{probe_text()[position:][-3000:]}'

                    # Where the host pointer is, xdotool being the host mouse, and the middle of the 1280x720 output, where
                    # the compositor brings it as a lock begins and whenever it is more than a quarter of the output away.
                    # Every xdotool call here names the private display: the desktop's is not ours.
                    state = read_state()
                    assert state['layout'] == 'vertical' and (state['left']['width'] + state['right']['width'],
                        state['left']['height']) == (1280, 720), state
                    middle_x, middle_y = 640, 360

                    def host_pointer():
                        shell = subprocess.run(['xdotool', 'getmouselocation', '--shell'], env=env, check=True,
                                               timeout=3, capture_output=True, text=True).stdout
                        found = dict(part.split('=', 1) for part in shell.split())
                        return int(found['X']), int(found['Y'])

                    def wait_host_pointer(position, timeout=1.0):
                        # Whether the host pointer is at `position`, to within the two pixels that a jump is told by,
                        # before the time is up.
                        deadline = time.monotonic() + timeout
                        while True:
                            here = host_pointer()
                            if abs(here[0] - position[0]) <= 2 and abs(here[1] - position[1]) <= 2:
                                return True
                            if time.monotonic() > deadline:
                                return False
                            time.sleep(.01)

                    other_x = 740 if side == 'left' else 100
                    mouse(x, 100)
                    at = len(probe_text())
                    mouse(x + 10, 110)
                    wait_since(at, r'^POINTER_MOTION x=110\.00 y=110\.00$')
                    probe_do('L')
                    wait_since(at, r'^POINTER_LOCKED$')
                    # The compositor brings the host pointer to the middle of the output as the lock begins; whether it
                    # does is for the steps at the edge below to show. Here the host mouse moves from wherever the
                    # pointer is once that jump has passed: the motion is the position it reports, less the one before.
                    wait_host_pointer((middle_x, middle_y))
                    here = host_pointer()
                    at = len(probe_text())
                    mouse(here[0] + 30, here[1] + 20)
                    wait_since(at, r'^RELATIVE dx=30\.00 dy=20\.00 dx_unaccel=30\.00 dy_unaccel=20\.00$')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as injected:
                        injected.connect(str(root / 'dosplex-primary-input.sock'))
                        injected.sendall(b'R 4 -2\n')
                        wait_since(at, r'^RELATIVE dx=4\.00 dy=-2\.00 dx_unaccel=4\.00 dy_unaccel=-2\.00$')
                    subprocess.run(['xdotool', 'click', '5'], env=env, check=True, timeout=3)
                    wait_since(at, r'^POINTER_AXIS axis=0 value=15\.00$')
                    assert 'POINTER_MOTION' not in probe_text()[at:], 'a locked pointer got a position:\n' + probe_text()[at:]
                    probe_do('l')
                    wait_since(at, r'^LOCK_ENDED$')
                    at = len(probe_text())
                    mouse(x + 50, 140)
                    wait_since(at, r'^POINTER_MOTION x=150\.00 y=140\.00$')

                    # The host pointer under a lock. The backend only reports where the host pointer is, so a locked
                    # client's motion is worked out from consecutive positions, and a host pointer that reaches the edge
                    # of the screen stops reporting any: the camera of a game stops turning that way. While the client
                    # holds a lock the compositor brings the host pointer back to the middle of the output whenever it
                    # is more than a quarter of the output away from it, and the jump that makes is a resync, not motion.
                    # xdotool's mousemove_relative moves the host mouse by steps, as a mouse does, and the screen's edge
                    # stops it.
                    def near_middle(position, step):
                        # Up to a quarter of the output away is left alone; the move that crosses it may carry one step
                        # further before the pointer is brought back.
                        return (abs(position[0] - middle_x) <= 320 + abs(step[0]) and
                                abs(position[1] - middle_y) <= 180 + abs(step[1]))

                    def appears(since, pattern, timeout=.6):
                        deadline = time.monotonic() + timeout
                        while not re.search(pattern, probe_text()[since:], re.M):
                            if time.monotonic() > deadline:
                                return False
                            time.sleep(.005)
                        return True

                    def drive(name, step, count):
                        # The host mouse moves by `step` (dx, dy) `count` times, one move after the other. Every move has
                        # to reach the locked client as relative motion of that size, whatever the host pointer did
                        # before, and the host pointer has to be back near the middle of the output within a moment.
                        first = len(probe_text())
                        wanted = f'dx={step[0]:.2f} dy={step[1]:.2f}'
                        missing, away = [], []
                        for number in range(1, count + 1):
                            at = len(probe_text())
                            subprocess.run(['xdotool', 'mousemove_relative', '--', str(step[0]), str(step[1])],
                                           env=env, check=True, timeout=3)
                            if not appears(at, rf'^RELATIVE {re.escape(wanted)} dx_unaccel=\S+ dy_unaccel=\S+$'):
                                missing.append(number)
                            deadline = time.monotonic() + .6
                            position = host_pointer()
                            while not near_middle(position, step) and time.monotonic() < deadline:
                                time.sleep(.01)
                                position = host_pointer()
                            if not near_middle(position, step):
                                away.append((number, position))
                        time.sleep(.15)  # a late line of the last move
                        problems = []
                        if missing:
                            problems.append(f'{len(missing)} of {count} moves gave the locked client no relative motion of '
                                            f'{wanted} (moves {missing[0]} to {missing[-1]})')
                        if away:
                            problems.append(f'the host pointer was not brought back near the middle after {len(away)} '
                                            f'moves: the first at move {away[0][0]} {away[0][1]}, the last at move '
                                            f'{away[-1][0]} {away[-1][1]}')
                        assert not problems, f'{name}: ' + '; '.join(problems) + f'; the host pointer is at {host_pointer()}'
                        # The jump of a pull back is no motion: the client got a move for each move of the mouse and
                        # nothing else, and no position.
                        moves = re.findall(r'^RELATIVE (dx=\S+ dy=\S+) ', probe_text()[first:], re.M)
                        assert moves == [wanted] * count, f'{name}: {len(moves)} relative motions for {count} moves: ' \
                            + ', '.join(sorted(set(moves)))
                        assert not re.search(r'^POINTER_(MOTION|ENTER|LEAVE)', probe_text()[first:], re.M), \
                            f'{name}: a locked client got a position or lost its pointer:\n' + probe_text()[first:][-1500:]

                    # A free pointer and a confined one need the real position of the host pointer: nothing brings it back.
                    at = len(probe_text())
                    mouse(x + 300, 600)
                    wait_since(at, r'^POINTER_MOTION x=400\.00 y=600\.00$')
                    time.sleep(.3)
                    assert host_pointer() == (x + 300, 600), ('a free pointer was moved', host_pointer())
                    at = len(probe_text())
                    probe_do('F')
                    wait_since(at, r'^POINTER_CONFINED$')
                    mouse(x + 310, 610)
                    time.sleep(.3)
                    assert host_pointer() == (x + 310, 610), ('a confined pointer was moved', host_pointer())
                    probe_do('f')
                    wait_since(at, r'^CONFINE_ENDED$')

                    mouse(x, 100)
                    at = len(probe_text())
                    mouse(x + 10, 110)
                    wait_since(at, r'^POINTER_MOTION x=110\.00 y=110\.00$')
                    probe_do('L')
                    wait_since(at, r'^POINTER_LOCKED$')
                    time.sleep(.2)
                    # Right to well past where the edge of the screen is, then back left past the other one, then down and up.
                    drive('to the right', (40, 0), 36)
                    drive('to the left', (-40, 0), 36)
                    drive('down', (0, 40), 22)
                    drive('up', (0, -40), 22)

                    def burst(name, step, count, pause='0.0005'):
                        # `count` moves in one go, with no wait for the compositor between them: positions that were
                        # already on their way when it brought the host pointer back are still motion, and the jump of
                        # the pull back is not. The client gets the sum of the moves, exactly.
                        first = len(probe_text())
                        chain = []
                        for _ in range(count):
                            chain += ['mousemove_relative', '--', str(step[0]), str(step[1])]
                            if pause:
                                chain += ['sleep', pause]
                        subprocess.run(['xdotool', *chain], env=env, check=True, timeout=10)
                        wanted = (float(step[0] * count), float(step[1] * count))
                        deadline = time.monotonic() + 3
                        while True:
                            seen = [(float(a), float(b)) for a, b in
                                    re.findall(r'^RELATIVE dx=(\S+) dy=(\S+) ', probe_text()[first:], re.M)]
                            total = (sum(a for a, _ in seen), sum(b for _, b in seen))
                            if total == wanted or time.monotonic() > deadline:
                                break
                            time.sleep(.02)
                        assert total == wanted, f'{name}: {len(seen)} relative motions add up to {total}, not {wanted}'
                        deadline = time.monotonic() + 1
                        while not near_middle(host_pointer(), (0, 0)) and time.monotonic() < deadline:
                            time.sleep(.01)
                        assert near_middle(host_pointer(), (0, 0)), (name, host_pointer())

                    burst('a burst to the right', (7, 0), 200)
                    burst('a burst down and to the left', (-5, 6), 150)
                    # No pause at all, and just enough moves to cross a quarter of the output and carry on for 14 more:
                    # those are on their way, and already count, when the compositor brings the host pointer back.
                    moves_to_quarter = (middle_x + 320 - host_pointer()[0]) // 7 + 1
                    burst('a burst with no pause', (7, 0), moves_to_quarter + 14, pause=None)
                    probe_do('l')
                    wait_since(at, r'^LOCK_ENDED$')
                    # The compositor's own cursor stayed where the lock held it, however far the host pointer was
                    # pulled: a click reaches the client, which still has the pointer, at the place where the lock was
                    # asked for (a seat sends no motion to the place it was at), though the host pointer is somewhere else.
                    at = len(probe_text())
                    subprocess.run(['xdotool', 'click', '1'], env=env, check=True, timeout=3)
                    wait_since(at, r'^POINTER_BUTTON button=272 state=1 x=110\.00 y=110\.00$')
                    wait_since(at, r'^POINTER_BUTTON button=272 state=0 x=110\.00 y=110\.00$')
                    clicked = probe_text()[at:]
                    assert not re.search(r'^POINTER_(ENTER|LEAVE)', clicked, re.M), clicked
                    assert not re.search(r'^POINTER_MOTION x=(?!110\.00 y=110\.00$)', clicked, re.M), clicked

                    # A lock that begins while the host mouse rests at the edge of the screen. Pushed further, the host
                    # pointer does not move, so the motion worked out from its positions would be nothing and nothing
                    # would bring it back: the first motion in that direction would be lost, until the mouse went another
                    # way (on the Deck every step was; in the private Xvfb the first one is).
                    # The compositor brings the host pointer to the middle as the lock begins, wherever it is. The edge is
                    # the one of the screen that belongs to this side: the right edge for the right side, and the left
                    # edge for the left side, whose client the right edge is not over (a lock needs the pointer over it).
                    # Every step has to arrive as relative motion of its size from the first one, and the jump that the
                    # host pointer makes at the start is no motion. The second time the lock is taken up again.
                    edge_x = 0 if side == 'left' else 1279
                    push = (-40, 0) if side == 'left' else (40, 0)
                    # A seat sends no motion to the place it was at, so the second time the mouse rests a little lower.
                    for label, edge_y in (('a lock that begins with the host pointer at the edge of the screen', 300),
                                          ('a lock that is taken up again with the host pointer at the edge of the '
                                           'screen', 320)):
                        edge = (edge_x, edge_y)
                        at = len(probe_text())
                        mouse(*edge)
                        assert host_pointer() == edge, (label, host_pointer())
                        wait_since(at, rf'^POINTER_(ENTER|MOTION) x={edge_x - (0 if side == "left" else 640)}\.00 '
                                       rf'y={edge_y}\.00$')
                        began = len(probe_text())
                        probe_do('L')
                        wait_since(began, r'^POINTER_LOCKED$')
                        # Whether the host pointer is brought to the middle is for the steps to show, with where it is
                        # when they fail: here the jump of that is given the time to arrive, and is no motion.
                        wait_host_pointer((middle_x, middle_y))
                        time.sleep(.1)
                        assert 'RELATIVE' not in probe_text()[began:], \
                            f'{label}: the jump of the host pointer was sent as motion:\n' + probe_text()[began:]
                        drive(label, push, 12)
                        probe_do('l')
                        wait_since(began, r'^LOCK_ENDED$')

                    # A press stays with the seat that got it: the release follows it over the other side.
                    mouse(x, 100)
                    at = len(probe_text())
                    subprocess.run(['xdotool', 'mousedown', '1'], env=env, check=True, timeout=3)
                    wait_since(at, r'^POINTER_BUTTON button=272 state=1 ')
                    mouse(other_x, 100)
                    never_since(at, r'^POINTER_BUTTON button=272 state=0')
                    subprocess.run(['xdotool', 'mouseup', '1'], env=env, check=True, timeout=3)
                    wait_since(at, r'^POINTER_BUTTON button=272 state=0 ')

                    # A layout change re-homes the pointer: the cursor goes into Main's region and its click
                    # reaches Main's application; Guest's client loses its pointer focus. The mouse starts over
                    # the client (a client that the pointer is not over was told so when it left).
                    at = len(probe_text())
                    ipc('LAYOUT horizontal')
                    wait_state(lambda s: s['layout'] == 'horizontal', 'top/bottom layout')
                    if side == 'left':
                        # The mouse is over the other side: the re-home brings it into Main's region and enters
                        # Main's client there.
                        wait_since(at, rf'^POINTER_ENTER x={other_x}\.00 y=100\.00$')
                    at = len(probe_text())
                    if side == 'right':
                        mouse(300, 500)
                        wait_since(at, r'^POINTER_(ENTER|MOTION) x=300\.00 y=140\.00$')  # the bottom half is its region
                    else:
                        mouse(300, 100)
                        wait_since(at, r'^POINTER_(ENTER|MOTION) x=300\.00 y=100\.00$')  # the top half is its region
                    at = len(probe_text())
                    ipc('LAYOUT vertical')
                    wait_state(lambda s: s['layout'] == 'vertical' and s[side]['width'] == 640, 'side by side again')
                    wait_since(at, r'^POINTER_LEAVE$')
                    if side == 'left':
                        wait_since(at, r'^POINTER_ENTER x=300\.00 y=100\.00$')
                        subprocess.run(['xdotool', 'click', '1'], env=env, check=True, timeout=3)
                        wait_since(at, r'^POINTER_BUTTON button=272 state=0 x=300\.00 y=100\.00$')
                    else:
                        subprocess.run(['xdotool', 'click', '1'], env=env, check=True, timeout=3)
                        never_since(at, r'^POINTER_(ENTER|BUTTON)')
                    mouse(x, 100)

                    # The display contract with a client mapped: display_revision
                    # advances once for each change of a side's effective game
                    # monitor size or applied scaler, and for nothing else.
                    other = 'right' if side == 'left' else 'left'
                    state = read_state()
                    assert state['layout'] == 'vertical' and (state[side]['width'], state[side]['height']) == (640, 720), state
                    mine, theirs = state[side]['display_revision'], state[other]['display_revision']

                    def expect(current, mine_delta, theirs_delta, label):
                        nonlocal mine, theirs
                        mine, theirs = mine + mine_delta, theirs + theirs_delta
                        assert (current[side]['display_revision'], current[other]['display_revision']) == \
                            (mine, theirs), (label, current)

                    def wait_configure(since, width, height):
                        # The mapped client is resized to the region and keeps running.
                        needle = f'TOPLEVEL_CONFIGURE width={width} height={height}'
                        deadline = time.monotonic() + 3
                        while needle not in (root / 'probe.log').read_text()[since:]:
                            assert probe.poll() is None, 'a layout change stopped the client'
                            assert time.monotonic() < deadline, f'the client never got {needle}'
                            time.sleep(.02)

                    # A layout change moves both game monitors, once (Native in Full screen, the
                    # default 16:9 in the split), and resizes the mapped client's toplevel to the new
                    # region.
                    since = len((root / 'probe.log').read_text())
                    ipc('LAYOUT single')
                    state = wait_state(lambda s: s['layout'] == 'single' and s[side]['width'] == 1280, 'single layout')
                    for name in (side, other):
                        assert (state[name]['game_width'], state[name]['game_height']) == (1280, 720), state
                    expect(state, 1, 1, 'vertical to single')
                    wait_configure(since, 1280, 720)
                    since = len((root / 'probe.log').read_text())
                    ipc('LAYOUT vertical')
                    state = wait_state(lambda s: s['layout'] == 'vertical' and s[side]['width'] == 640, 'vertical layout')
                    for name in (side, other):
                        assert (state[name]['game_width'], state[name]['game_height']) == fit_16_9(640, 720), state
                    expect(state, 1, 1, 'single to vertical')
                    wait_configure(since, 640, 720)
                    # Swapping places keeps both regions the size they were: no change.
                    swap = 'left' if state['guest_physical_side'] == 'right' else 'right'
                    ipc(f'GUEST {swap}')
                    wait_state(lambda s: s['guest_physical_side'] == swap, 'guest placement')
                    time.sleep(.15)
                    expect(read_state(), 0, 0, 'position swap')
                    ipc(f'GUEST {"right" if swap == "left" else "left"}')
                    wait_state(lambda s: s['guest_physical_side'] != swap, 'guest placement restored')
                    time.sleep(.15)
                    expect(read_state(), 0, 0, 'position swap back')
                    # A pending choice is not a change; Cancel discards it.
                    ipc(f'RESOLUTION {side} 720p')
                    state = wait_state(lambda s: s[side]['pending_game_width'] == 1280, 'pending 720p')
                    assert state[side]['display_pending'] is True and state[other]['display_pending'] is False, state
                    assert (state[side]['game_width'], state[side]['game_height']) == fit_16_9(640, 720), state
                    expect(state, 0, 0, 'pending choice')
                    ipc(f'DISPLAY_CANCEL {side}')
                    state = wait_state(lambda s: not s[side]['display_pending'], 'Cancel')
                    assert (state[side]['pending_game_width'], state[side]['pending_game_height']) == fit_16_9(640, 720), state
                    time.sleep(.15)
                    expect(read_state(), 0, 0, 'Cancel')
                    # Apply advances the revision once and moves only this side.
                    ipc(f'RESOLUTION {side} 720p')
                    wait_state(lambda s: s[side]['display_pending'], 'pending 720p again')
                    ipc(f'DISPLAY_APPLY {side}')
                    state = wait_state(lambda s: not s[side]['display_pending'] and s[side]['game_width'] == 1280, 'Apply')
                    assert (state[side]['game_width'], state[side]['game_height']) == (1280, 720), state
                    expect(state, 1, 0, 'Apply')
                    ipc(f'DISPLAY_APPLY {side}')  # nothing is pending
                    time.sleep(.15)
                    expect(read_state(), 0, 0, 'Apply without a pending choice')
                    # The fixed 1280x720 does not move with the region: only the side on its default does.
                    ipc('LAYOUT single')
                    state = wait_state(lambda s: s['layout'] == 'single' and s[other]['width'] == 1280, 'single layout again')
                    expect(state, 0, 1, 'fixed size, vertical to single')
                    ipc('LAYOUT vertical')
                    state = wait_state(lambda s: s['layout'] == 'vertical' and s[other]['width'] == 640, 'vertical layout again')
                    expect(state, 0, 1, 'fixed size, single to vertical')
                    # An applied scaler change advances the revision; a pending one does not.
                    ipc(f'SCALE {side} stretch')
                    state = wait_state(lambda s: s[side]['pending_scale_mode'] == 'stretch', 'pending stretch')
                    assert state[side]['display_pending'] is True and state[side]['scale_mode'] == 'fit', state
                    expect(state, 0, 0, 'pending scaler')
                    ipc(f'DISPLAY_APPLY {side}')
                    state = wait_state(lambda s: s[side]['scale_mode'] == 'stretch' and not s[side]['display_pending'], 'scaler Apply')
                    expect(state, 1, 0, 'scaler Apply')
                    # One Apply that changes both the size and the scaler is one change.
                    ipc(f'ASPECT {side} native')
                    ipc(f'SCALE {side} fit')
                    wait_state(lambda s: s[side]['pending_game_aspect'] == 'native' and s[side]['pending_scale_mode'] == 'fit', 'pending restore')
                    ipc(f'DISPLAY_APPLY {side}')
                    state = wait_state(lambda s: not s[side]['display_pending'] and s[side]['scale_mode'] == 'fit', 'restoring Apply')
                    assert (state[side]['game_width'], state[side]['game_height']) == (640, 720), state
                    assert state[side]['native_resolution'] is True, state
                    expect(state, 1, 0, 'size and scaler in one Apply')
                    # A choice that the player applied stays through a layout change, Native included, which
                    # is not the split's default; the side that made none follows the default, and the same
                    # 16:9 monitor fits both splits, so it moves nothing.
                    ipc('LAYOUT horizontal')
                    state = wait_state(lambda s: s['layout'] == 'horizontal' and s[side]['width'] == 1280, 'top/bottom layout')
                    assert (state[side]['game_aspect'], state[side]['game_width'], state[side]['game_height']) == ('native', 1280, 360), state
                    assert (state[other]['game_aspect'], state[other]['game_width'], state[other]['game_height']) == \
                        ('16:9',) + fit_16_9(1280, 360), state
                    expect(state, 1, 0, 'Native follows its region, the default 16:9 stays')
                    ipc('LAYOUT vertical')
                    state = wait_state(lambda s: s['layout'] == 'vertical' and s[side]['width'] == 640, 'side by side once more')
                    assert (state[side]['game_aspect'], state[side]['game_width'], state[side]['game_height']) == ('native', 640, 720), state
                    expect(state, 1, 0, 'Native follows its region back')
                    # The menu says whether the picture is scaled, and draws it again from the supervisor's report
                    # (DEV_SIDE_DISPLAY stands in for session.json, which only root may write). A frame of the real
                    # output after each step, compared line by line, shows the header and the Display page's line
                    # change with the report and come back, whatever the font: the notice that the first deferred
                    # report for a revision raises, then the header's own Scaled for the same revision and no second
                    # notice, then Native again once the monitor is applied.
                    region = read_state()[side]
                    assert (region['width'], region['height']) == (640, 720), region  # scale 1: a 616x540 panel
                    revision = region['display_revision']
                    panel_x, panel_y = region['x'] + (640 - 616) // 2, region['y'] + (720 - 540) // 2
                    captures = [200]

                    def lines():
                        """The pixels of the header line and of the line under the buttons of the Display page."""
                        captures[0] += 1
                        ipc(f'DEV_FRAME {captures[0]}')
                        wait_state(lambda s: s['capture_ready'] == captures[0], 'a frame for the capture')
                        shot = root / 'frame.xwd'
                        subprocess.run(['xwd', '-display', env['DISPLAY'], '-id', windows[0], '-silent', '-out', str(shot)],
                                       env=env, check=True, timeout=5)
                        raw = shot.read_bytes()
                        header = struct.unpack('>25I', raw[:100])
                        assert header[1:3] == (7, 2) and header[11] == 32, header  # xwd version 7, ZPixmap, 32 bits
                        offset, stride = header[0] + header[19] * 12, header[12]
                        def rows(top, bottom):
                            return b''.join(raw[offset + y * stride + panel_x * 4:offset + y * stride + (panel_x + 616) * 4]
                                            for y in range(panel_y + top, panel_y + bottom))
                        return rows(44, 62), rows(282, 300)

                    ipc(f'OVERLAY {side}')
                    wait_state(lambda s: s['overlay_visible'] and s['overlay_owner'] == side, 'the menu opens')
                    native_header, standing_line = lines()
                    ipc(f'DEV_SIDE_DISPLAY {side} {revision} 3840 2160 deferred')
                    wait_state(lambda s: s[side]['display_scaled'], 'a deferred report scales the side')
                    notice_header, scaled_line = lines()
                    assert notice_header != native_header and scaled_line != standing_line, 'the report did not change the menu'
                    ipc(f'DEV_SIDE_DISPLAY {side} {revision} 640 720 applied')
                    wait_state(lambda s: not s[side]['display_scaled'], 'the applied report ends it')
                    assert lines() == (native_header, standing_line), 'the menu did not return to Native'
                    ipc(f'DEV_SIDE_DISPLAY {side} {revision} 3840 2160 deferred')
                    wait_state(lambda s: s[side]['display_scaled'], 'a deferred report scales the side again')
                    word_header, repeated_line = lines()
                    assert repeated_line == scaled_line, 'the Display page says another thing for the same report'
                    assert word_header not in (native_header, notice_header), \
                        'the header says Scaled once, and the notice is not raised twice for a revision'
                    # Zoom is never chosen for the player: a layout change under a deferred report leaves the
                    # published mode alone. The player's own Apply of Zoom publishes it with a new revision.
                    ipc('LAYOUT horizontal')
                    state = wait_state(lambda s: s['layout'] == 'horizontal' and s[side]['width'] == 1280, 'top/bottom layout')
                    assert state[side]['scale_mode'] == 'fit', state
                    revision = state[side]['display_revision']
                    ipc(f'DEV_SIDE_DISPLAY {side} {revision} 640 720 deferred')
                    wait_state(lambda s: s[side]['display_scaled'], 'a deferred report after a layout change')
                    time.sleep(0.3)
                    state = read_state()
                    assert state[side]['scale_mode'] == 'fit' and state[side]['display_revision'] == revision, state
                    ipc(f'SCALE {side} zoom')
                    wait_state(lambda s: s[side]['pending_scale_mode'] == 'zoom', 'pending Zoom')
                    ipc(f'DISPLAY_APPLY {side}')
                    state = wait_state(lambda s: s[side]['scale_mode'] == 'zoom', 'Zoom applied')
                    assert state[side]['display_revision'] == revision + 1, state
                    ipc(f'SCALE {side} fit')
                    ipc(f'DISPLAY_APPLY {side}')
                    state = wait_state(lambda s: s[side]['scale_mode'] == 'fit', 'Fit applied again')
                    assert state[side]['display_revision'] == revision + 2, state
                    ipc('OVERLAY close')
                    probe.terminate()
                    probe.wait(timeout=5)
                compositor.send_signal(signal.SIGTERM)
                assert compositor.wait(timeout=5) == 0
        except Exception:
            print((root / 'compositor.log').read_text()[-12000:])
            raise
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print('PASS: real nested X11 output, overlay startup, resize, absolute pointer motion, owner-scoped scroll, a locked '
          'pointer\'s host pointer brought to the middle as the lock begins, also at the edge of the screen, and kept near it, '
          'and graceful teardown in private Xvfb')


if __name__ == '__main__':
    main()
