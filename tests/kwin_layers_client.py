# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The KWin 6.7 layers client for the tests that run it: building it and driving it.

Not a test. tests/fixtures/dosplex-kwin-layers-probe.c plays a nested KWin 6.7's Wayland backend (a toplevel of one
single-pixel buffer with the frame callback and the presentation feedback KWin asks for, and a subsurface for the
picture and for each overlay plane); its header comment lists the commands it takes on stdin and the lines it prints.
desktop-subsurface-scale-integration.py (headless) and x11-presentation-test.py (under Xvfb) import this.
"""
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

TIMED = re.compile(r'(frame|frame_done|presented|discarded) (\d+)(?: \d+)? t=([0-9.]+)$')


def build_probe(root):
    """Compile the client into the directory `root` and return the binary's path."""
    project = Path(__file__).resolve().parents[1]
    protocols = Path(subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip())
    sources = {
        'xdg-shell': protocols / 'stable/xdg-shell/xdg-shell.xml',
        'viewporter': protocols / 'stable/viewporter/viewporter.xml',
        'fractional-scale-v1': protocols / 'staging/fractional-scale/fractional-scale-v1.xml',
        'single-pixel-buffer-v1': protocols / 'staging/single-pixel-buffer/single-pixel-buffer-v1.xml',
        'presentation-time': protocols / 'stable/presentation-time/presentation-time.xml',
    }
    generated = []
    for name, xml in sources.items():
        subprocess.run(['wayland-scanner', 'client-header', str(xml), str(root / f'{name}-client-protocol.h')], check=True, timeout=5)
        subprocess.run(['wayland-scanner', 'private-code', str(xml), str(root / f'{name}-protocol.c')], check=True, timeout=5)
        generated.append(str(root / f'{name}-protocol.c'))
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
    binary = root / 'kwin-layers-probe'
    subprocess.run(['cc', '-I', str(root), str(project / 'tests/fixtures/dosplex-kwin-layers-probe.c'), *generated,
                    '-o', str(binary), *flags], check=True, timeout=30)
    return binary


class Probe:
    """The KWin layers client; `events` collects its output lines in order, and it takes its commands on stdin."""

    def __init__(self, binary, env):
        self.process = subprocess.Popen([str(binary)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.events = []
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            with self._lock:
                self.events.append(line.strip())

    def snapshot(self):
        return self.since(0)

    def since(self, mark):
        with self._lock:
            return list(self.events[mark:])

    def wait_for(self, pattern, mark=0, timeout=10):
        """The first line after `mark` that matches the regular expression."""
        deadline = time.monotonic() + timeout
        while True:
            for line in self.since(mark):
                found = re.fullmatch(pattern, line)
                if found:
                    return found
            assert self.process.poll() is None, f'the client exited: {self.snapshot()}'
            assert time.monotonic() < deadline, f'{pattern!r} never came after line {mark}: {self.since(mark)}'
            time.sleep(0.01)

    def command(self, letter, wait=True):
        """One command; with `wait` every frame it committed is done and presented (or discarded) before this returns."""
        mark = len(self.snapshot())
        self.process.stdin.write(letter)
        self.process.stdin.flush()
        self.wait_for(rf'ok {letter}', mark)
        if wait:
            self.frames_complete(mark)
        return mark

    def frames_complete(self, mark, timeout=10):
        """Every frame committed after `mark` has had its frame callback and its feedback."""
        deadline = time.monotonic() + timeout
        while True:
            lines = self.since(mark)
            frames = [int(line.split()[1]) for line in lines if line.startswith('frame ')]
            done = {int(line.split()[1]) for line in lines if line.startswith('frame_done ')}
            finished = {int(line.split()[1]) for line in lines if line.startswith(('presented ', 'discarded '))}
            if all(frame in done and frame in finished for frame in frames):
                return frames
            assert time.monotonic() < deadline, f'frames {frames} are not complete after {timeout} s: {lines}'
            time.sleep(0.01)

    def timings(self, mark):
        """For each frame committed after `mark`: how long its frame callback and its feedback took (ms), and whether the
        feedback was a presented or a discarded."""
        stamps = {}
        for line in self.since(mark):
            found = TIMED.match(line)
            if found:
                stamps.setdefault(int(found.group(2)), {})[found.group(1)] = float(found.group(3))
        return [{'done': got['frame_done'] - got['frame'],
                 'feedback': (got['presented'] if 'presented' in got else got['discarded']) - got['frame'],
                 'presented': 'presented' in got}
                for _, got in sorted(stamps.items()) if 'frame' in got and 'frame_done' in got]

    def paced(self, letter='G', timeout=30):
        """The G command (g: the frames of a video on a plane): frames committed one at a time, each on the feedback of
        the one before. Returns the lines it produced, the frames presented (frame -> the output's frame counter in the
        feedback) and the frames discarded."""
        mark = len(self.snapshot())
        self.command(letter, wait=False)
        self.wait_for('paced_done', mark, timeout)
        lines = self.since(mark)
        presented = {int(words[1]): int(words[2]) for words in (line.split() for line in lines) if words[0] == 'presented'}
        discarded = [int(words[1]) for words in (line.split() for line in lines) if words[0] == 'discarded']
        return lines, presented, discarded

    def surface_ids(self):
        """name -> the wl_surface id the client printed for it."""
        return {m.group(1): int(m.group(2)) for line in self.snapshot() if (m := re.fullmatch(r'layer (\w+) id=(\d+)', line))}

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
