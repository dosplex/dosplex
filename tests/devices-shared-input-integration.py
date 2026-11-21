#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,xvfb,wayland-client,cc,ordinary-user timeout=120
# dosplex-run: default {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Main shares the mouse and the keyboard between both sides from the second page of Devices.

The real compositor runs on the X11 backend in a private Xvfb server, side by side on a 1280x720 output, as an
ordinary user. The X11 backend merges every host mouse, keyboard and touchscreen into one device each, so
Devices lists a Touchscreen, a Mouse and a Keyboard row. A fixture controller inventory (the files a broker
writes) gives Main a controller: a merged X11 device is never a menu control, and Main may move a device only
while it keeps another one. The menu is clicked through the remote pointer socket, which has nothing to do with
the mouse being assigned; `xdotool` is the mouse and the keyboard under test. A Wayland probe client on Guest's
side stands in for Guest's Steam: what it is sent is what a Guest application would get.

Shown here: Mouse and Keyboard have Main, Guest and Shared (a third of the row each) and Touchscreen only Main
and Guest, in a frame captured after the page was drawn; Shared starts the Keep/Undo preview, which names the
device and Shared; a shared mouse crosses the middle and reaches Guest's client at the place under it, where
Main's mouse moved to the same place does not; going back and forth, it leaves Guest's client each time it goes
over Main and enters it afresh, at the place under it, each time it returns, and a button held across the
crossing keeps the client until the release; a shared keyboard types in the side where the mouse is; Undo and
Main give the device back; and Main still must keep a control for itself. It does not show that a real mouse
or keyboard reaches a game: the X11 backend merges the host's devices, and a physical run is the owner's. The
probe is no Gamescope either, so it cannot show the warp of a Gamescope's cursor on each entry; the compositor's
log names the entries.
"""
import json
import os
from pathlib import Path
import pwd
import re
import select
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BINARY = None  # set from the command line at the bottom
ROOT = Path(__file__).resolve().parents[1]
PROBE = None   # the compiled probe client, built once by setUpModule
_probe_directory = None

# Side by side on the 1280x720 output: Main's region is x 0..639 and the menu a 616x540 panel in it. These are
# the hit targets inside it (logical pixels are screen pixels at this size).
PANEL_Y, PANEL_H = 90, 540
CONTENT_X, CONTENT_Y, CONTENT_W = 28, 202, 584
TAB_W, THIRD, HALF = (CONTENT_W - 24) // 4, (CONTENT_W - 16) // 3, (CONTENT_W - 8) // 2
ROWS_Y, ROW_PITCH, ROWS_PER_PAGE = CONTENT_Y + 22, 50, 5
DEVICES_TAB = (CONTENT_X + TAB_W + 8 + TAB_W // 2, PANEL_Y + 70 + 15)
PREVIOUS = (CONTENT_X + HALF // 2, PANEL_Y + PANEL_H - 68 + 14)
NEXT = (CONTENT_X + HALF + 8 + HALF // 2, PANEL_Y + PANEL_H - 68 + 14)
UNDO = (CONTENT_X + HALF // 2, CONTENT_Y + 112 + 17)
KEEP = (CONTENT_X + HALF + 8 + HALF // 2, CONTENT_Y + 112 + 17)
BUTTONS = ('Main', 'Guest', 'Shared')

# What a button is drawn with when it is selected and when it is not, and the panel behind the buttons.
ACCENT, BOX, PANEL = (51, 117, 220), (30, 33, 43), (21, 24, 31)

# The X11 backend's three devices, as Devices names them.
FRIENDLY = {'x11-keyboard': 'Keyboard', 'x11-pointer': 'Mouse', 'x11-touch': 'Touchscreen'}

PADS = {'a': 'main', 'b': 'guest', 'c': 'unassigned', 'd': 'unassigned', 'e': 'unassigned'}
KEY_A, KEY_B = 30, 48  # evdev codes that the probe reports for the letters a and b


def setUpModule():
    """Build the probe client that stands in for Guest's Steam (the same tool the X11 test maps)."""
    global PROBE, _probe_directory
    _probe_directory = tempfile.TemporaryDirectory(prefix='dosplex-shared-probe-')
    directory = Path(_probe_directory.name)
    protocols = Path(subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'],
                                             text=True).strip())
    generated = []
    for xml, header, code in [
            ('stable/xdg-shell/xdg-shell.xml', 'xdg-shell-client-protocol.h', 'xdg-shell-protocol.c'),
            ('unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
             'pointer-constraints-unstable-v1-client-protocol.h', 'pointer-constraints-protocol.c'),
            ('unstable/relative-pointer/relative-pointer-unstable-v1.xml',
             'relative-pointer-unstable-v1-client-protocol.h', 'relative-pointer-protocol.c')]:
        for mode, filename in [('client-header', header), ('private-code', code)]:
            subprocess.run(['wayland-scanner', mode, str(protocols / xml), str(directory / filename)],
                           check=True, timeout=10)
        generated.append(str(directory / code))
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
    PROBE = directory / 'probe'
    subprocess.run(['cc', '-I', str(directory), str(ROOT / 'tools/dosplex-keyprobe.c'), *generated,
                    '-o', str(PROBE), *flags], check=True, timeout=60)


def tearDownModule():
    _probe_directory.cleanup()


class Inventory:
    """What the broker would publish. A thread republishes it, because the compositor ignores a list older than 5 s."""

    def __init__(self, root, pads):
        self.root = root
        self.directory = root / 'controller-preferences'
        self.directory.mkdir(mode=0o700)
        self.devices = {}
        self.lock = threading.RLock()
        for letter, owner in pads.items():
            self.add(letter * 32, f'Pad {letter}', owner)
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def add(self, key, name, owner):
        record = self.directory / (key + '.json')
        record.write_text(json.dumps({'name': name, 'target': owner}))
        record.chmod(0o600)
        with self.lock:
            self.devices[key] = {'id': key, 'name': name, 'connected': True, 'active': False, 'target': owner,
                                 'requested': owner, 'input_status': {'shortcut_ready': True}}
            self.publish()

    def publish(self):
        with self.lock:
            text = json.dumps({'updated': time.time(), 'assignment_preview': 2, 'devices': list(self.devices.values())})
            temporary = self.root / 'controllers.new'
            temporary.write_text(text)
            temporary.chmod(0o600)
            os.replace(temporary, self.root / 'controllers.json')

    def loop(self):
        while self.running:
            self.publish()
            time.sleep(.25)


class Frame:
    """A capture of the nested output (xwd): what was really drawn, not what the compositor believes it drew."""

    def __init__(self, raw):
        header = struct.unpack('>25I', raw[:100])
        assert header[1:3] == (7, 2) and header[11] == 32, header  # xwd version 7, ZPixmap, 32 bits a pixel
        self.raw, self.stride, self.offset = raw, header[12], header[0] + header[19] * 12
        self.order = 'little' if header[7] == 0 else 'big'
        self.masks = header[14:17]
        self.size = (header[4], header[5])

    def rgb(self, x, y):
        start = self.offset + y * self.stride + x * 4
        value = int.from_bytes(self.raw[start:start + 4], self.order)
        return tuple((value & mask) >> ((mask & -mask).bit_length() - 1) for mask in self.masks)

    def kind(self, x, y):
        """accent (a selected button), box (a button), panel (nothing), or the colour itself."""
        colour = self.rgb(x, y)
        for name, wanted in (('accent', ACCENT), ('box', BOX), ('panel', PANEL)):
            if all(abs(a - b) <= 6 for a, b in zip(colour, wanted)):
                return name
        return colour


def row_y(row):
    return ROWS_Y + row * ROW_PITCH + 20 + 13  # the middle of the buttons of a row


def button_point(row, button, buttons=3):
    width = THIRD if buttons == 3 else HALF
    return CONTENT_X + button * (width + 8) + width // 2, row_y(row)


class Probe:
    """The client on Guest's side. Its output is what its seat was sent."""

    def __init__(self, run):
        self.path = run.root / 'probe.log'
        self.log = self.path.open('w')
        self.proc = subprocess.Popen([str(PROBE)], env=run.env, stdout=self.log, stderr=self.log)
        run.wait_until(lambda: 'CONFIGURED' in self.text(), 'the probe never mapped')
        run.wait_for(lambda s: s['right']['has_app'], 'Guest has the probe window')

    def text(self):
        return self.path.read_text()

    def mark(self):
        return len(self.text())

    def since(self, mark):
        return self.text()[mark:]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.close()


class Run:
    """One disposable X11 compositor in a private Xvfb server, side by side, with a fixture controller inventory."""

    def __init__(self, pads=PADS, probe=False):
        self.pads, self.with_probe, self.probe, self.captures = pads, probe, None, 0

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dosplex-shared-input-')
        self.root = Path(self.directory.name)
        self.processes = []
        self.inventory = self.remote = self.compositor = None
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def start_xvfb(self):
        """A private Xvfb of 1280x720 that answers: its display, or None when the one it named is not ours or does not
        answer (a busy machine runs many servers at once, and a display can be taken by another between the choice and
        the first connection: "Failed to open xcb connection" or a window tree that is not ours, once each in full
        gate runs)."""
        read_fd, write_fd = os.pipe()
        try:
            server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1280x720x24',
                                       '-nolisten', 'tcp', '-ac'], pass_fds=(write_fd,), stdout=self.xlog, stderr=self.xlog)
            self.processes.append(server)
            os.close(write_fd)
            write_fd = None
            if not select.select([read_fd], [], [], 10)[0]:
                return None
            display = os.read(read_fd, 64).decode().strip()
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        if not display.isdecimal():
            return None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            answer = subprocess.run(['xwininfo', '-root', '-display', ':' + display], capture_output=True, text=True,
                                    timeout=10)
            if answer.returncode == 0:
                return display if re.search(r'^\s*Width: 1280\s*$', answer.stdout, re.M) and \
                    re.search(r'^\s*Height: 720\s*$', answer.stdout, re.M) else None
            time.sleep(.1)
        return None

    def start(self):
        self.xlog = (self.root / 'xvfb.log').open('w')
        self.log = (self.root / 'compositor.log').open('w')
        display = None
        for attempt in range(3):
            display = self.start_xvfb()
            if display:
                break
            self.processes.pop().terminate()
        if not display:
            raise RuntimeError('no Xvfb of its own answered in three tries:\n' + (self.root / 'xvfb.log').read_text()[-2000:])
        me = pwd.getpwuid(os.getuid()).pw_name
        # Guest's client is the one this user runs; Main's side has none, so Main's region is the empty placeholder.
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'XDG_RUNTIME_DIR': str(self.root),
                    'DOSPLEX_RUNTIME_DIR': str(self.root), 'DOSPLEX_CONTROLLER_DIRECTORY':
                    str(self.root / 'controller-preferences'), 'WLR_RENDERER': 'pixman', 'DISPLAY': ':' + display,
                    'WAYLAND_DISPLAY': 'dosplex-0', 'DOSPLEX_WAYLAND_DEBUG': '0', 'DOSPLEX_INITIAL_LAYOUT': 'vertical',
                    'DOSPLEX_LEFT_USER': 'dosplex-unassigned', 'DOSPLEX_RIGHT_USER': me}
        self.inventory = Inventory(self.root, self.pads)
        self.compositor = subprocess.Popen([BINARY, '-b', 'x11', '--test-hooks'], env=self.env, stdout=self.log,
                                           stderr=self.log)
        self.processes.append(self.compositor)
        self.wait_until(lambda: (self.root / 'state.json').exists(), 'the compositor wrote no state')
        tree = subprocess.check_output(['xwininfo', '-root', '-tree'], env=self.env, text=True, timeout=5)
        windows = re.findall(r'^\s+(0x[0-9a-f]+) .*: .*\d+x\d+', tree, re.MULTILINE)
        assert len(windows) == 1, tree  # with no window manager the output is the server's only window
        self.window = windows[0]
        subprocess.run(['xdotool', 'windowsize', self.window, '1280', '720'], env=self.env, check=True, timeout=5)
        state = self.wait_for(lambda s: s['layout'] == 'vertical' and s['left']['width'] == 640 and
                              s['right']['x'] == 640, 'side by side at 1280x720')
        assert (state['left']['role'], state['right']['role']) == ('primary', 'guest'), state
        # The menu lists the devices newest first; the order they attach in tells which row is which.
        attached = re.findall(r'New input device type=\d+ name=(x11-[a-z]+)', self.log_text())
        assert sorted(attached) == sorted(FRIENDLY), attached
        self.devices = [FRIENDLY[name] for name in reversed(attached)]
        self.remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.remote.connect(str(self.root / 'dosplex-primary-input.sock'))
        if self.with_probe:
            self.probe = Probe(self)

    def __exit__(self, *exc):
        if self.probe:
            self.probe.stop()
        if self.inventory:
            self.inventory.running = False
            self.inventory.thread.join(timeout=2)
        self.control.close()
        if self.remote:
            self.remote.close()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in (getattr(self, 'xlog', None), getattr(self, 'log', None)):
            if log:
                log.close()
        self.directory.cleanup()

    # --- looking -----------------------------------------------------------------------------------------

    def log_text(self):
        return (self.root / 'compositor.log').read_text(encoding='utf-8', errors='replace')

    def state(self):
        if self.compositor and self.compositor.poll() is not None:
            raise AssertionError(f'the compositor exited with {self.compositor.returncode}:\n{self.log_text()[-3000:]}')
        try:
            return json.loads((self.root / 'state.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def wait_until(self, predicate, label, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.state()  # fails fast when the compositor died
            if time.monotonic() > deadline:
                raise AssertionError(f'{label}\n{self.log_text()[-3000:]}')
            time.sleep(.02)

    def wait_for(self, predicate, label, timeout=4):
        self.wait_until(lambda: (lambda s: s is not None and predicate(s))(self.state()), label, timeout)
        return self.state()

    def logged(self, text, timeout=3):
        self.wait_until(lambda: text in self.log_text(), f'the log never said {text!r}', timeout)

    def frame(self):
        """The output as drawn after everything done so far: the compositor presents a frame, then it is captured."""
        self.captures += 1
        self.ipc(f'DEV_FRAME {self.captures}')
        self.wait_for(lambda s: s['capture_ready'] == self.captures, 'no frame was presented for the capture')
        shot = self.root / 'frame.xwd'
        subprocess.run(['xwd', '-display', self.env['DISPLAY'], '-id', self.window, '-silent', '-out', str(shot)],
                       env=self.env, check=True, timeout=5)
        frame = Frame(shot.read_bytes())
        assert frame.size == (1280, 720), frame.size
        return frame

    def drawn(self, frame, device, buttons):
        """How a row looks if it has `buttons` buttons: what is at the start of each, and in the gaps between."""
        _, row = self.where(device)
        width = THIRD if buttons == 3 else HALF
        starts = [CONTENT_X + j * (width + 8) for j in range(buttons)]
        return ([frame.kind(x + 6, row_y(row)) for x in starts], [frame.kind(x - 4, row_y(row)) for x in starts[1:]])

    def selected(self, frame, device):
        """The buttons of a three-button row that are drawn as selected."""
        _, row = self.where(device)
        return [name for j, name in enumerate(BUTTONS)
                if frame.kind(CONTENT_X + j * (THIRD + 8) + 6, row_y(row)) == 'accent']

    def where(self, device):
        """The page of Devices and the row on it: the controllers come first, then the devices of this machine."""
        index = len(self.pads) + self.devices.index(device)
        return index // ROWS_PER_PAGE, index % ROWS_PER_PAGE

    # --- acting ------------------------------------------------------------------------------------------

    def ipc(self, command):
        self.control.sendto(command.encode(), str(self.root / 'hud-ipc.sock'))

    def click(self, point):
        """A click in the menu, through the remote pointer: it stays out of the way of the mouse under test."""
        self.remote.sendall(f'M {point[0]} {point[1]}\nB 272 1\nB 272 0\n'.encode())
        time.sleep(.15)

    def move_mouse(self, x, y):
        """Move the mouse (window pixels) to x, y. It is first taken to a spot in Main's half, since a mouse that
        is moved to where it already is says nothing."""
        for point in ((320, 700), (x, y)):
            subprocess.run(['xdotool', 'mousemove', '--window', self.window, str(point[0]), str(point[1])],
                           env=self.env, check=True, timeout=5)
        time.sleep(.1)

    def key(self, key):
        subprocess.run(['xdotool', 'key', key], env=self.env, check=True, timeout=5)

    def open_devices(self, device=None):
        """Main's menu on the Devices tab, on the page that has `device`'s row (the first page without one)."""
        self.ipc('OVERLAY left')
        self.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'left', "Main's menu opens")
        time.sleep(.2)  # The first frame builds the hit regions.
        # The menu opens on the tab and page it was closed on, so only what is not there yet is clicked.
        page = self.where(device)[0] if device else 0
        for _ in range(8):
            state = self.state()
            if state['overlay_tab'] == 1 and state['device_page'] == page:
                break
            self.click(DEVICES_TAB if state['overlay_tab'] != 1 else NEXT if state['device_page'] < page else PREVIOUS)
        else:
            raise AssertionError(f'Devices page {page} never opened: {state}\n{self.log_text()[-2000:]}')
        time.sleep(.2)  # Its rows are drawn from the inventory.

    def press(self, device, button):
        """Press one of the three buttons of a device's row. The row is looked at first: a button that is not
        drawn is not clicked on the chance that something else lies there."""
        kinds, gaps = self.drawn(self.frame(), device, 3)
        assert gaps == ['panel', 'panel'] and sorted(kinds) == ['accent', 'box', 'box'], \
            f'the {device} row does not have Main, Guest and Shared buttons (one selected, a gap between each): {kinds} {gaps}'
        self.click(button_point(self.where(device)[1], BUTTONS.index(button)))

    def keep(self):
        self.click(KEEP)
        self.wait_for(lambda s: not s['overlay_confirmation'], 'Keep ends the preview')

    def undo(self):
        self.click(UNDO)
        self.wait_for(lambda s: not s['overlay_confirmation'], 'Undo ends the preview')

    def menu(self, action):
        """What a controller does: one D-pad step or button of the menu helper (up, down, left, right, select, back)."""
        self.ipc(f'MENU left {action}')
        time.sleep(.15)  # The next step moves from the buttons of the frame this one drew.

    def close_menu(self):
        self.ipc('OVERLAY close')
        self.wait_for(lambda s: not s['overlay_visible'], 'the menu closes')

    def share(self, device):
        """Open Devices, choose Shared for `device`, see that the menu asks about it, and keep it."""
        self.open_devices(device)
        self.press(device, 'Shared')
        self.wait_for(lambda s: s['overlay_confirmation'], f'Shared on {device} asks for confirmation')
        self.logged(f'Devices: {device} → Shared')
        self.keep()
        self.close_menu()


def probe_gets(run, text, since, why, timeout=4):
    """The probe is sent `text` after `since`. A failure shows everything it was sent since then."""
    deadline = time.monotonic() + timeout
    while text not in run.probe.since(since):
        run.state()  # fails fast when the compositor died
        if time.monotonic() > deadline:
            raise AssertionError(f'{why}\nthe probe was sent:\n{run.probe.since(since)}\n{run.log_text()[-2000:]}')
        time.sleep(.02)


def quiet(test, run, text, since, why, seconds=.4):
    """Nothing the probe was sent since `since` contains `text`. Absence needs time to be believed, so every test
    that uses this also shows the same move or key being delivered when it should be."""
    time.sleep(seconds)
    test.assertNotIn(text, run.probe.since(since), why)


class SharedChoiceTests(unittest.TestCase):
    def test_mouse_and_keyboard_rows_have_main_guest_and_shared_and_touch_only_main_and_guest(self):
        with Run() as run:
            run.open_devices('Mouse')
            self.assertEqual(run.state()['device_page'], 1, 'five controllers fill the first page')
            frame = run.frame()
            for device in ('Mouse', 'Keyboard'):
                self.assertEqual(run.drawn(frame, device, 3), (['accent', 'box', 'box'], ['panel', 'panel']), device)
                self.assertEqual(run.selected(frame, device), ['Main'], device)
            self.assertEqual(run.drawn(frame, 'Touchscreen', 2), (['accent', 'box'], ['panel']), 'Touchscreen')

    def test_shared_names_the_device_and_a_shared_mouse_crosses_the_middle(self):
        with Run(probe=True) as run:
            # Main's mouse covers Main's region and no more: moving it to the right edge of the screen
            # does not reach Guest.
            mark = run.probe.mark()
            run.move_mouse(1000, 300)
            quiet(self, run, 'POINTER_ENTER', mark, "Main's mouse reached Guest")
            run.open_devices('Mouse')
            run.press('Mouse', 'Shared')
            run.wait_for(lambda s: s['overlay_confirmation'], 'Shared on the Mouse row asks for confirmation')
            run.logged('Devices: Mouse → Shared')
            run.keep()
            frame = run.frame()
            self.assertEqual(run.selected(frame, 'Mouse'), ['Shared'])
            self.assertEqual(run.selected(frame, 'Keyboard'), ['Main'])
            run.close_menu()
            # Now the pointer is where it is moved to: over Guest's client, 360 pixels into it. Were the mouse
            # Guest's instead, the whole window would be Guest's region and this would be 500.
            mark = run.probe.mark()
            run.move_mouse(1000, 300)
            run.wait_until(lambda: 'POINTER_ENTER x=360.00 y=300.00' in run.probe.since(mark),
                           'a shared mouse did not reach Guest where it was moved\n' + run.probe.since(mark))
            # A click goes to the side under the mouse: Guest's client gets the one over it, and not the one
            # that follows in Main's half.
            mark = run.probe.mark()
            subprocess.run(['xdotool', 'click', '1'], env=run.env, check=True, timeout=5)
            run.wait_until(lambda: 'POINTER_BUTTON button=272 state=1 x=360.00 y=300.00' in run.probe.since(mark),
                           "a click over Guest's side did not reach its client\n" + run.probe.since(mark))
            run.move_mouse(200, 300)
            mark = run.probe.mark()
            subprocess.run(['xdotool', 'click', '1'], env=run.env, check=True, timeout=5)
            quiet(self, run, 'POINTER_BUTTON', mark, "a click in Main's half reached Guest's client")
            # Main takes the mouse back with Main, and it is Main's alone again.
            run.open_devices('Mouse')
            run.press('Mouse', 'Main')
            run.wait_for(lambda s: s['overlay_confirmation'], 'Main on the Mouse row asks for confirmation')
            run.logged('Devices: Mouse → Main')
            run.keep()
            self.assertEqual(run.selected(run.frame(), 'Mouse'), ['Main'])
            run.close_menu()
            mark = run.probe.mark()
            run.move_mouse(1000, 300)
            quiet(self, run, 'POINTER_ENTER', mark, 'a mouse given back to Main still reached Guest')

    def test_a_shared_mouse_leaves_and_enters_guest_each_time_it_crosses(self):
        # Guest has a seat of its own, and the shared mouse is over one side at a time. While it is over Main,
        # Guest's client has to be told that it left, and when it returns it has to be entered afresh at the
        # place under the mouse, every time and not only the first: a seat that keeps its pointer focus gets
        # plain motion on the way back, and a nested Gamescope behind it is never warped to the new place. The
        # probe is no Gamescope, so what it shows is the Wayland half; the compositor's log names every entry.
        with Run(probe=True) as run:
            run.share('Mouse')
            visits = [(1000, 300), (900, 500), (700, 100)]
            for visit, (x, y) in enumerate(visits, start=1):
                mark = run.probe.mark()
                run.move_mouse(x, y)
                probe_gets(run, f'POINTER_ENTER x={x - 640:.2f} y={y:.2f}', mark,
                           f'visit {visit}: the shared mouse did not enter Guest where it was moved')
                mark = run.probe.mark()
                run.move_mouse(200, 300)
                probe_gets(run, 'POINTER_LEAVE', mark,
                           f"visit {visit}: Guest's client was never told that the shared mouse went over Main")
            self.assertEqual(re.findall(r'^POINTER_(ENTER|LEAVE)', run.probe.text(), re.M), ['ENTER', 'LEAVE'] * 3)
            entered = re.findall(r'Pointer entered app: x=(\d+) y=(\d+) side=right', run.log_text())
            self.assertEqual(entered, [('1000', '300'), ('900', '500'), ('700', '100')],
                             'the compositor did not enter Guest again on each return')

    def test_a_press_held_across_the_crossing_keeps_guests_client_until_the_release(self):
        # The release belongs to the seat that got the press, so that seat is not left while the button is down:
        # the client gets the release first and then the leave, and the next visit is entered afresh.
        with Run(probe=True) as run:
            run.share('Mouse')
            run.move_mouse(1000, 300)
            probe_gets(run, 'POINTER_ENTER x=360.00 y=300.00', 0, 'the shared mouse did not reach Guest')
            mark = run.probe.mark()
            subprocess.run(['xdotool', 'mousedown', '1'], env=run.env, check=True, timeout=5)
            probe_gets(run, 'POINTER_BUTTON button=272 state=1 x=360.00 y=300.00', mark,
                       "the press did not reach Guest's client")
            run.move_mouse(200, 300)
            quiet(self, run, 'POINTER_BUTTON button=272 state=0', mark, "the press ended when the mouse left Guest")
            self.assertNotIn('POINTER_LEAVE', run.probe.since(mark), "Guest's client was left with its button down")
            subprocess.run(['xdotool', 'mouseup', '1'], env=run.env, check=True, timeout=5)
            probe_gets(run, 'POINTER_LEAVE', mark, "Guest's client was never told that the mouse left once the button was up")
            self.assertEqual(re.findall(r'^POINTER_(BUTTON button=272 state=\d|LEAVE)', run.probe.since(mark), re.M),
                             ['BUTTON button=272 state=1', 'BUTTON button=272 state=0', 'LEAVE'])
            mark = run.probe.mark()
            run.move_mouse(900, 500)
            probe_gets(run, 'POINTER_ENTER x=260.00 y=500.00', mark, 'the shared mouse was not entered afresh after the release')

    def test_a_shared_keyboard_types_in_the_side_where_the_mouse_is(self):
        with Run(probe=True) as run:
            run.share('Mouse')  # the mouse has to get to Guest's side for the keyboard to follow it there
            mark = run.probe.mark()
            run.move_mouse(1000, 300)
            run.wait_until(lambda: 'POINTER_ENTER' in run.probe.since(mark), 'the shared mouse did not reach Guest')
            # The keyboard is still Main's: whatever the mouse is over, it types in Main.
            mark = run.probe.mark()
            run.key('a')
            quiet(self, run, f'key={KEY_A} ', mark, "Main's keyboard typed into Guest's client")
            run.share('Keyboard')
            run.move_mouse(1000, 300)
            mark = run.probe.mark()
            run.key('a')
            run.wait_until(lambda: f'key={KEY_A} state=1' in run.probe.since(mark),
                           'a shared keyboard did not type where the mouse is\n' + run.probe.since(mark))
            # With the mouse back over Main's half the same keyboard types there, not in Guest's client.
            run.move_mouse(200, 300)
            mark = run.probe.mark()
            run.key('b')
            quiet(self, run, f'key={KEY_B} ', mark, 'a shared keyboard typed into Guest with the mouse over Main')

    def test_a_controller_reaches_shared_on_the_second_page_with_the_dpad(self):
        with Run() as run:
            # The menu opens with the focus on the Display tab, and each step goes to the nearest button in that
            # direction, preferring one that lies across the step. So: right to Devices, A to open it; five
            # controller rows, Swap controls and Previous are seven steps down, then right to Next and A.
            run.menu('toggle')
            run.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'left', "Main's menu opens")
            time.sleep(.3)
            run.menu('right')
            run.menu('select')
            run.wait_for(lambda s: s['overlay_tab'] == 1, 'A on the Devices tab opens it')
            for _ in range(7):
                run.menu('down')
            run.menu('right')
            run.menu('select')
            run.wait_for(lambda s: s['device_page'] == 1, 'A on Next shows the second page')
            time.sleep(.3)
            # A new page starts the focus at the close button. Down goes to the Session tab, to Touchscreen's
            # Guest button and to Mouse's Shared button, which A presses.
            for _ in range(3):
                run.menu('down')
            run.menu('select')
            run.wait_for(lambda s: s['overlay_confirmation'], 'A on the Shared button of the Mouse row asks for confirmation')
            run.logged('Devices: Mouse → Shared')
            run.menu('right')  # Undo has the focus; right is Keep assignment.
            run.menu('select')
            run.wait_for(lambda s: not s['overlay_confirmation'], 'Keep ends the preview')
            self.assertEqual(run.selected(run.frame(), 'Mouse'), ['Shared'])

    def test_undo_gives_a_shared_mouse_back_to_main(self):
        with Run(probe=True) as run:
            run.open_devices('Mouse')
            run.press('Mouse', 'Shared')
            run.wait_for(lambda s: s['overlay_confirmation'], 'Shared on the Mouse row asks for confirmation')
            run.logged('Devices: Mouse → Shared')
            run.undo()
            self.assertEqual(run.selected(run.frame(), 'Mouse'), ['Main'])
            run.close_menu()
            mark = run.probe.mark()
            run.move_mouse(1000, 300)
            quiet(self, run, 'POINTER_ENTER', mark, 'a mouse that Undo gave back to Main reached Guest')
            # The same move does reach Guest once Shared is kept: the check above could see it.
            run.share('Mouse')
            run.move_mouse(1000, 300)
            run.wait_until(lambda: 'POINTER_ENTER x=360.00 y=300.00' in run.probe.since(mark),
                           'a kept Shared did not reach Guest\n' + run.probe.since(mark))

    def test_main_keeps_a_control_for_itself_when_it_shares_a_device(self):
        # Guest has the only controller, so Main would have nothing to open the menu with but the merged
        # X11 keyboard, which does not count. Shared moves a device away from Main like Guest does.
        with Run(pads={'b': 'guest'}) as run:
            run.open_devices('Keyboard')
            run.press('Keyboard', 'Shared')
            run.logged('Devices: Keep a control for Main. Connect another or use Swap controls.')
            self.assertFalse(run.state()['overlay_confirmation'])
            self.assertNotIn('Devices: Keyboard → Shared', run.log_text())


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: devices-shared-input-integration.py COMPOSITOR [unittest options]')
    if os.geteuid() == 0:
        raise RuntimeError('run this test as an ordinary user')
    BINARY = str(Path(sys.argv.pop(1)).resolve(strict=True))
    unittest.main()
