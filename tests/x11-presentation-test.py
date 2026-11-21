#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,xvfb,wayland-client,cc,ordinary-user timeout=180
# dosplex-run: left {compositor} --client-side left
# dosplex-run: right {compositor} --client-side right
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Presentation feedback and configures for a Desktop side under the X11 backend (the host Gamescope's case).

The real compositor runs as an X11 client of an Xvfb, as it runs under the host Gamescope on the devices.
tests/fixtures/dosplex-scale-probe.c plays the nested KWin in the single layout at scale 2: it maps its toplevel and
then paces frames the way KWin 6.3 and later does, one frame in flight, the next committed only when the
wp_presentation feedback of the one before has arrived.

Three things are checked. First, the pacing: consecutive frames are one host frame apart, which the probe reads
from the host's frame counter in each feedback. The X11 backend sends the present event and then the frame event;
the compositor, once a present has shown that it carries its own commit's sequence, commits only when something
changed (state.json says "frame_pacing": "on-demand"), so the client's answer to a feedback goes out with the next
host frame. Committing at every frame event, as the compositor did before, put an empty commit in that slot and
every client frame took two host frames (30 fps at 60 Hz). The unpatched wlroots 0.17 X11 backend tags each
present with the commit sequence before the commit increments it, so a feedback was answered only by the present
of the commit after its own: three host frames apart at every-frame pacing. The compositor detects that and keeps
committing every frame ("every-frame-late-presents"), since on demand such a feedback would wait for a commit that
never comes; this test then fails on the distance. Second, the scale: the scene's own scale update of the
surface is held back, so the map (a scene output update) sends the client no preferred scale at all, where the
scene used to send 120 and the compositor 240 right after. Third, the follow-up configure the compositor sends
after a preferred scale is idempotent: a client that already shows the toplevel at the side's scale gets no
configure for a scene output update nor for any number of frames it commits.

A fourth: KWin 6.7 asks for its frame callback and its presentation feedback on its toplevel's own surface, one
single pixel, and draws its picture in a subsurface above it (tests/fixtures/dosplex-kwin-layers-probe.c is that
client). When the picture is opaque over all of the toplevel (an XR24 or a video plane, or a declared opaque region)
wlroots' scene shows the toplevel nowhere, and gives such a surface neither a frame done nor a presentation sample: the
compositor answers for it, with the output commit that shows its tree, and KWin's frames, one in flight, are still
presented one host frame apart, at scale 2 and at scale 1.
"""
import argparse
import json
import os
import pwd
import queue
import re
import select
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
from kwin_layers_client import Probe as LayersProbe, build_probe as build_layers_probe  # noqa: E402


class Probe:
    """The scale probe client; `events` collects its output lines in order."""

    def __init__(self, binary, env, extra=()):
        self.process = subprocess.Popen([str(binary), *extra], env=env, stdout=subprocess.PIPE, text=True)
        self.events = []
        self._lock = threading.Lock()
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            with self._lock:
                self.events.append(line.strip())

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def wait_for(self, text, start=0, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.snapshot()
            if any(e == text or e.startswith(text + ' ') for e in events[start:]):
                return events
            assert self.process.poll() is None, f'the probe exited: {events}'
            time.sleep(0.02)
        raise AssertionError(f'{text!r} never arrived after event {start}: {self.snapshot()}')

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=5)


def build_probe(root):
    project = Path(__file__).resolve().parents[1]
    protocols = Path(subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'],
                                             text=True).strip())
    sources = {
        'xdg-shell': protocols / 'stable/xdg-shell/xdg-shell.xml',
        'fractional-scale-v1': protocols / 'staging/fractional-scale/fractional-scale-v1.xml',
        'xdg-output-unstable-v1': protocols / 'unstable/xdg-output/xdg-output-unstable-v1.xml',
        'viewporter': protocols / 'stable/viewporter/viewporter.xml',
        'presentation-time': protocols / 'stable/presentation-time/presentation-time.xml',
    }
    generated = []
    for name, xml in sources.items():
        subprocess.run(['wayland-scanner', 'client-header', str(xml), str(root / f'{name}-client-protocol.h')],
                       check=True, timeout=5)
        subprocess.run(['wayland-scanner', 'private-code', str(xml), str(root / f'{name}-protocol.c')],
                       check=True, timeout=5)
        generated.append(str(root / f'{name}-protocol.c'))
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
    subprocess.run(['cc', '-I', str(root), str(project / 'tests/fixtures/dosplex-scale-probe.c'), *generated,
                    '-o', str(root / 'probe'), *flags], check=True, timeout=30)
    return root / 'probe'


def run(binary, client_side, frames=24):
    if os.geteuid() == 0:
        raise RuntimeError('Run this test as an ordinary user')
    with tempfile.TemporaryDirectory(prefix='dosplex-present-') as directory:
        root = Path(directory)
        probe_binary = build_probe(root)
        user = pwd.getpwuid(os.getuid()).pw_name
        read_fd, write_fd = os.pipe()
        processes = []
        try:
            with (root / 'xvfb.log').open('w') as xlog, (root / 'compositor.log').open('w+') as log:
                server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1280x720x24',
                                           '-nolisten', 'tcp', '-ac'], pass_fds=(write_fd,), stdout=xlog, stderr=xlog)
                processes.append(server)
                os.close(write_fd)
                write_fd = None
                if not select.select([read_fd], [], [], 5)[0]:
                    raise RuntimeError('Xvfb startup timed out')
                display = os.read(read_fd, 64).decode().strip()
                assert display.isdecimal()
                env = {
                    'PATH': os.environ['PATH'], 'HOME': directory, 'XDG_RUNTIME_DIR': directory,
                    'DOSPLEX_RUNTIME_DIR': directory, 'WAYLAND_DISPLAY': 'dosplex-0', 'WLR_RENDERER': 'pixman',
                    'DISPLAY': ':' + display, 'DOSPLEX_WAYLAND_DEBUG': '0', 'DOSPLEX_INPUT_DEFAULT_SIDE': 'follow',
                    # The client side is the primary in the single layout: its desktop fills the output, as on a
                    # Deck or a Machine whose Main side runs a Desktop session alone.
                    'DOSPLEX_INITIAL_LAYOUT': 'single', 'DOSPLEX_PRIMARY_SIDE': client_side,
                    'DOSPLEX_LEFT_USER': user if client_side == 'left' else 'dosplex-test-no-such-user',
                    'DOSPLEX_RIGHT_USER': user if client_side == 'right' else 'dosplex-test-no-such-user',
                    'DOSPLEX_AUTH_HELPER': '/bin/false', 'DOSPLEX_SESSION_SCRIPT': '/bin/false',
                }
                compositor = subprocess.Popen([str(binary), '-b', 'x11', '--test-hooks'], env=env, stdout=log,
                                              stderr=log, start_new_session=True)
                processes.append(compositor)
                deadline = time.monotonic() + 5
                while not (root / 'hud-ipc.sock').exists() or not (root / 'state.json').exists():
                    assert compositor.poll() is None and time.monotonic() < deadline, 'compositor did not start'
                    time.sleep(0.02)
                control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

                def send(command):
                    control.sendto(command.encode(), str(root / 'hud-ipc.sock'))

                def log_text():
                    log.flush()
                    return (root / 'compositor.log').read_text()

                send(f'DEV_SIDE_SCALE {client_side} 2000')
                time.sleep(0.3)
                # The side fills the X11 window (the backend's default size, 1024x768); the toplevel is half of it.
                state = json.loads((root / 'state.json').read_text())
                region = state[client_side]
                assert state['layout'] == 'single' and region['width'] > 0 and region['height'] > 0, state
                toplevel = f"{(region['width'] * 1000 + 1000) // 2000}x{(region['height'] * 1000 + 1000) // 2000}"
                buffer = f"{region['width']}x{region['height']}"
                # The map comes 200 ms after the first configure: the compositor's follow-up configure (80 ms after
                # it sent the scale, to a client that had no buffer yet) has run by then. What follows the map is the
                # activation configure of the first map, then nothing: not for the scene output update the map is,
                # not for the frames.
                log_mark = len(log_text())
                probe = Probe(probe_binary, env, ('--kwin', '--map-delay', '200', '--frames', str(frames)))
                probe.wait_for('frame 0', timeout=10)
                probe.wait_for(f'frames_done {frames}', timeout=15)
                time.sleep(0.5)
                events = probe.snapshot()
                mapped_at = events.index('frame 0') + 1
                before = events[:mapped_at]
                after = events[mapped_at:]
                assert 'applied_scale 240' in before and f'buffer {buffer}' in before and f'viewport {toplevel}' in before, \
                    before

                # Every frame after the mapping commit is presented, and its feedback names the host's frame counter.
                presented = {}
                for line in after:
                    found = re.fullmatch(r'presented (\d+) (\d+)', line)
                    if found:
                        presented[int(found[1])] = int(found[2])
                missing = [k for k in range(1, frames + 1) if k not in presented]
                assert not missing, f'frames without presented feedback: {missing}\n{after}'
                assert all(seq > 0 for seq in presented.values()), presented
                # On demand, the frame the client commits on a feedback is presented at the next host frame:
                # consecutive frames are one host frame apart (a busy machine may skip one now and then). Every-frame
                # pacing gives two, the unpatched backend three.
                distances = [presented[k] - presented[k - 1] for k in range(2, frames + 1)]
                assert all(d >= 1 for d in distances), distances
                assert statistics.median(distances) == 1, f'frames are not on consecutive host frames: {distances}'
                pacing = json.loads((root / 'state.json').read_text()).get('frame_pacing')
                assert pacing == 'on-demand', pacing

                # The map of the surface was a scene output update (the scene re-sent the host scale, the
                # compositor kept its own). The compositor re-sent no configure for it or for any frame: the client
                # showed the toplevel at the side's scale already. The one configure the probe may see after the
                # map is the activation of its first map, which arrives before the feedback of frame 0.
                log_after = log_text()[log_mark:]
                assert 'Frame pacing: on demand' in log_text(), log_text()[-2000:]
                assert 'Held back the scene\'s own output scale updates' in log_after, log_after[-2000:]
                kept = log_after.find('Kept preferred scale 2000/1000')
                assert kept >= 0, log_after[-2000:]
                # The log is in time order: a re-send after the map's "Kept" line is one the map or a frame caused.
                resent = re.findall(r'Re-sent configure .*', log_after[kept:])
                assert not resent, f'configures re-sent after the map: {resent}'
                first_frame = after.index('frame 1')
                configures = [e for e in after[first_frame:] if e.startswith('configure')]
                assert not configures, f'configures after the frames began: {configures}\n{after}'
                assert all(e == f'configure {toplevel}' for e in after if e.startswith('configure')), after
                # No preferred scale after the map: the scene sent none, so the compositor had none to send back.
                scales = [e for e in after if e.startswith('preferred_scale')]
                assert not scales, f'the preferred scale changed after the map: {after}'

                probe.stop()

                # KWin 6.7's layers, the picture opaque over all of the toplevel. The toplevel's own surface, which is
                # all that KWin asks for callbacks and feedback on, is covered; its frames are still done and presented,
                # one host frame apart, with the compositor committing on demand: the feedback is queued for the commit
                # that shows the picture. At scale 2, then at the scale a Game mode side has.
                layers = LayersProbe(build_layers_probe(root), env)
                processes.append(layers.process)
                layers.wait_for('ready')
                layers.wait_for(r'mapped scale=240 output=\d+x\d+ device=\d+x\d+')
                layers.frames_complete(0)
                layers.command('O')

                def main_visible_px():
                    """What the scene shows of the toplevel's own surface (DEV_SCENE)."""
                    ids = layers.surface_ids()
                    since = len(log_text())
                    send(f'DEV_SCENE {client_side}')
                    deadline = time.monotonic() + 5
                    while f'DEV_SCENE {client_side} end' not in log_text()[since:]:
                        assert time.monotonic() < deadline, 'no DEV_SCENE answer'
                        time.sleep(0.02)
                    shown = re.search(rf'DEV_SCENE {client_side} surface={ids["main"]} .* visible_px=(\d+)', log_text()[since:])
                    assert shown, log_text()[since:][-1500:]
                    return int(shown.group(1))

                def paced_frames(label, letter):
                    """24 frames, each committed on the feedback of the one before: all presented, a host frame apart."""
                    lines, presented, discarded = layers.paced(letter)
                    assert not discarded, (label, 'feedback discarded', discarded, lines)
                    assert sorted(presented) == list(range(min(presented), min(presented) + len(presented))) and len(presented) == 24, \
                        (label, presented)
                    layers.frames_complete(0)
                    distances = [presented[k] - presented[k - 1] for k in sorted(presented)[1:]]
                    assert all(d >= 1 for d in distances), (label, distances)
                    assert statistics.median(distances) == 1, f'{label}: frames are not on consecutive host frames: {distances}'
                    assert json.loads((root / 'state.json').read_text()).get('frame_pacing') == 'on-demand', label
                    print(f'{label}: 24 frames, host frames apart: {distances}')

                def covered_and_paced(label):
                    assert main_visible_px() == 0, (label, 'the toplevel is not covered')
                    paced_frames(f'{label}, the toplevel covered', 'G')
                    # A video plane that is opaque over a part of the output, all that changes in a frame: the damage meets
                    # nothing of the toplevel's visible part (and the scene would discard the feedback it is asked for).
                    layers.command('o')
                    layers.command('N')
                    layers.command('L')
                    assert 0 < main_visible_px(), (label, 'the toplevel is covered')
                    paced_frames(f'{label}, a video plane over a part of the toplevel', 'g')
                    layers.command('l')
                    layers.command('n')
                    layers.command('O')

                covered_and_paced('scale 2')
                mark = len(layers.snapshot())
                send(f'DEV_SIDE_SCALE {client_side} 1000')
                state = json.loads((root / 'state.json').read_text())
                layers.wait_for(rf'configure {state[client_side]["width"]}x{state[client_side]["height"]}', mark)
                layers.frames_complete(mark)
                time.sleep(0.3)
                layers.frames_complete(mark)
                covered_and_paced('scale 1')
                layers.stop()
                control.close()
        finally:
            for process in processes[::-1]:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
        text = (root / 'compositor.log').read_text()
        assert compositor.returncode == 0, f'compositor exit {compositor.returncode}\n{text[-2000:]}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--client-side', choices=('left', 'right'), default='left')
    parser.add_argument('--frames', type=int, default=24)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.client_side, args.frames)
    print('x11 presentation: ok')
