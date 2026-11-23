#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,ordinary-user timeout=240
# dosplex-run: default {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Controllers in the Devices tab: Guest claims and releases its own, and Swap copes with several (T-422).

The real compositor runs headless, as an ordinary user, in a private runtime directory, with a fixture
controller inventory in place of the broker (the same files a broker writes: controllers.json, one preference
record per controller and assignments.json). Clicks go through the real pointer path. What this shows is the
menu's decisions and the requests it writes; it does not show that a controller reaches a game. That is the
hardware check.

Side by side on the 1280x720 output: Main's region is x 0..639, Guest's x 640..1279, a 616x540 panel in each.
"""
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

BINARY = None  # set from the command line at the bottom

# The panel of a 640x720 region at scale 1, and the hit targets inside it (logical pixels = screen pixels).
PANEL_Y, PANEL_H = 90, 540
CONTENT_X, CONTENT_Y, CONTENT_W = 28, 202, 584
TAB_W, THIRD, HALF = (CONTENT_W - 24) // 4, (CONTENT_W - 16) // 3, (CONTENT_W - 8) // 2
GUEST_X = 640
ROWS_Y = CONTENT_Y + 22           # the first controller row, below the one-line message


def devices_tab(origin):
    return origin + CONTENT_X + TAB_W + 8 + TAB_W // 2, PANEL_Y + 70 + 15


def row_button(origin, row, button=0, wide=False):
    """Centre of a button in a controller row: Main's rows have three (Main, Guest, Unassigned), Guest's one."""
    y = ROWS_Y + row * 50 + 20 + 13
    if wide:
        return origin + CONTENT_X + HALF // 2, y
    return origin + CONTENT_X + button * (THIRD + 8) + THIRD // 2, y


def forget_button(origin, row):
    """The Forget button sits at the right end of the name line of a row, above the three owner buttons."""
    return origin + CONTENT_X + CONTENT_W - 40, ROWS_Y + row * 50 - 2 + 11


CANCEL = (CONTENT_X + HALF // 2, CONTENT_Y + 88 + 17)
CONTINUE = (CONTENT_X + HALF + 8 + HALF // 2, CONTENT_Y + 88 + 17)
SWAP = (CONTENT_X + CONTENT_W // 2, PANEL_Y + PANEL_H - 104 + 14)
PREVIOUS = (CONTENT_X + HALF // 2, PANEL_Y + PANEL_H - 68 + 14)
NEXT = (CONTENT_X + HALF + 8 + HALF // 2, PANEL_Y + PANEL_H - 68 + 14)
UNDO = (CONTENT_X + HALF // 2, CONTENT_Y + 112 + 17)
KEEP = (CONTENT_X + HALF + 8 + HALF // 2, CONTENT_Y + 112 + 17)

MAIN_PAD, GUEST_PAD, FREE_PAD, ASLEEP_PAD, MAIN_PAD_2, GUEST_PAD_2 = (c * 32 for c in 'abcdef')


class Inventory:
    """What the broker would publish. A thread republishes it, because the compositor ignores a list older than 5 s."""

    def __init__(self, root):
        self.root = root
        self.directory = root / 'controller-preferences'
        self.directory.mkdir(mode=0o700)
        self.devices = {}
        self.preview = 2     # assignment_preview: 2 is a broker that can recover from a crashed preview
        self.lock = threading.RLock()
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def add(self, key, name, owner, connected=True, active=False, index=None, backend=None):
        """A controller the broker knows. `owner` is where it is: main, guest or unassigned. `index` is the
        number the routing service gives it; an older service has none, and the row then has no such field."""
        record = self.directory / (key + '.json')
        record.write_text(json.dumps({'name': name, 'target': owner}))
        record.chmod(0o600)
        with self.lock:
            self.devices[key] = {'id': key, 'name': name, 'connected': connected, 'active': active,
                                 'target': owner, 'requested': owner, 'input_status': {'shortcut_ready': connected}}
            if index is not None:
                self.devices[key]['index'] = index
            if backend is not None:
                self.devices[key]['backend'] = backend
            self.publish()

    def change(self, key, **fields):
        with self.lock:
            self.devices[key].update(fields)
            self.publish()

    def publish(self):
        with self.lock:
            text = json.dumps({'updated': time.time(), 'assignment_preview': self.preview,
                               'devices': list(self.devices.values())})
            temporary = self.root / 'controllers.new'
            temporary.write_text(text)
            temporary.chmod(0o600)
            os.replace(temporary, self.root / 'controllers.json')

    def loop(self):
        while self.running:
            self.publish()
            time.sleep(.25)

    def assignments(self):
        path = self.directory / 'assignments.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def owner(self, key):
        saved = self.assignments().get('assignments', {})
        return saved.get(key) or json.loads((self.directory / (key + '.json')).read_text())['target']

    def set_saved_owner(self, key, owner):
        """Another player's request, saved behind the menu's back."""
        record = self.assignments()
        record.setdefault('assignments', {})[key] = owner
        temporary = self.directory / 'assignments.request'
        temporary.write_text(json.dumps(record))
        temporary.chmod(0o600)
        os.replace(temporary, self.directory / 'assignments.json')


class Run:
    """One disposable headless compositor, side by side (or the layout given), with a fixture inventory."""

    def __init__(self, layout='vertical'):
        self.layout = layout

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dosplex-claim-test-')
        self.root = Path(self.directory.name)
        user = pwd.getpwuid(os.getuid()).pw_name
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.root), 'XDG_RUNTIME_DIR': str(self.root),
                    'DOSPLEX_RUNTIME_DIR': str(self.root),
                    'DOSPLEX_CONTROLLER_DIRECTORY': str(self.root / 'controller-preferences'),
                    'WAYLAND_DISPLAY': 'dosplex-0', 'WLR_RENDERER': 'pixman', 'DOSPLEX_WAYLAND_DEBUG': '0',
                    'DOSPLEX_INITIAL_LAYOUT': self.layout, 'DOSPLEX_LEFT_USER': user, 'DOSPLEX_RIGHT_USER': user,
                    'DOSPLEX_AUTH_HELPER': '/bin/false', 'DOSPLEX_SESSION_SCRIPT': '/bin/false'}
        self.log = (self.root / 'compositor.log').open('w+')
        self.inventory = Inventory(self.root)
        self.proc = subprocess.Popen([BINARY, '-b', 'headless'], env=self.env, stdout=self.log, stderr=self.log,
                                     start_new_session=True)
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.pointer = None
        try:
            state = self.wait_for(lambda s: s is not None and s['layout'] == self.layout and s['left']['width'] > 0,
                                  self.layout)
            if self.layout == 'vertical':
                assert (state['left']['x'], state['left']['width'], state['left']['height']) == (0, 640, 720), state
                assert (state['right']['x'], state['right']['width']) == (GUEST_X, 640), state
            assert (state['left']['role'], state['right']['role']) == ('primary', 'guest'), state
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        self.inventory.running = False
        self.control.close()
        if self.pointer:
            self.pointer.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.inventory.thread.join(timeout=2)
        self.log.close()
        self.directory.cleanup()

    def log_text(self):
        self.log.flush()
        return (self.root / 'compositor.log').read_text()

    def state(self):
        if self.proc.poll() is not None:
            raise AssertionError(f'the compositor exited with {self.proc.returncode}:\n{self.log_text()[-3000:]}')
        try:
            return json.loads((self.root / 'state.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def report(self, label):
        return f'{label}\nassignments: {self.inventory.assignments()}\n{self.log_text()[-3000:]}'

    def wait_for(self, predicate, label, timeout=4):
        deadline = time.monotonic() + timeout
        while True:
            current = self.state()
            if predicate(current):
                return current
            if time.monotonic() > deadline:
                raise AssertionError(f'{label}: {current}\n{self.log_text()[-3000:]}')
            time.sleep(.02)

    def wait_until(self, predicate, label, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.state()  # fails fast when the compositor died
            if time.monotonic() > deadline:
                raise AssertionError(self.report(label))
            time.sleep(.02)

    def logged(self, text, timeout=3):
        self.wait_until(lambda: text in self.log_text(), f'the log never said {text!r}', timeout)

    def stays(self, predicate, label, seconds=.6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert predicate(), self.report(label)
            time.sleep(.05)

    def ipc(self, command):
        self.control.sendto(command.encode(), str(self.root / 'hud-ipc.sock'))

    def redraw(self):
        """Make the compositor read the inventory again: a route command for a name that no device has changes
        nothing, redraws, and is logged when it has been handled, which is the point to wait for."""
        self.syncs = getattr(self, 'syncs', 0) + 1
        name = f'sync-{self.syncs}'
        self.ipc(f'ROUTE {name} main')
        self.logged(f'Added dynamic routes for all devices named {name} to')
        time.sleep(.15)  # The frame that follows reads the inventory.

    def click(self, point):
        if self.pointer is None:
            self.pointer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.pointer.connect(str(self.root / 'dosplex-primary-input.sock'))
        self.pointer.sendall(f'M {point[0]} {point[1]}\nB 272 1\nB 272 0\n'.encode())
        time.sleep(.15)

    def open_devices(self, side):
        owner = 'left' if side == 'main' else 'right'
        state = self.state()
        if state['overlay_visible'] and state['overlay_owner'] != owner:
            # There is one overlay and Guest's never takes Main's: what is open closes before the other opens.
            self.ipc('OVERLAY close')
            self.wait_for(lambda s: not s['overlay_visible'], 'the open menu closes')
        self.ipc(f'OVERLAY {owner}')
        self.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == owner, f'{side} overlay opens')
        time.sleep(.2)  # The first frame builds the hit regions.
        origin = 0 if side == 'main' else GUEST_X
        if self.layout == 'single':
            origin = 320  # Main's panel is centred in the whole 1280 pixel output
        for _ in range(6):
            self.click(devices_tab(origin))
            if self.state()['overlay_tab'] == 1:
                break
        else:
            raise AssertionError(f'the Devices tab never opened\n{self.log_text()[-2000:]}')
        time.sleep(.2)  # Its rows are drawn from the inventory.
        return origin


class GuestClaimTests(unittest.TestCase):
    def inventory(self, run):
        """Main's pad and Guest's pad, a free pad that is awake and one that is asleep, in this order."""
        run.inventory.add(MAIN_PAD, 'Main controller', 'main')
        run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')
        run.inventory.add(FREE_PAD, 'Free controller', 'unassigned')
        run.inventory.add(ASLEEP_PAD, 'Asleep controller', 'unassigned', connected=False)

    def test_guest_lists_its_own_and_the_free_awake_controllers_and_nothing_else(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            # The rows are Guest's own controller (Release), then the free awake one (Claim). Main's pad is
            # not listed, or Guest's would be the second row; the sleeping one is not listed, or it would be
            # a third row with a Claim button that says to wake it.
            for row in (2, 3):
                run.click(row_button(origin, row, wide=True))
            run.stays(lambda: run.inventory.assignments() == {}, 'a click below the list wrote a request')
            self.assertNotIn('Wake the controller', run.log_text())
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            self.assertEqual([run.inventory.owner(k) for k in (MAIN_PAD, GUEST_PAD, ASLEEP_PAD)],
                             ['main', 'guest', 'unassigned'])

    def test_a_claim_is_written_at_once_with_no_preview_to_confirm(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            record = run.inventory.assignments()
            self.assertEqual(record['assignments'], {FREE_PAD: 'guest'})
            self.assertNotIn('preview', record)                    # No 15 second preview,
            self.assertNotIn('handoff', record)                    # no paired swap,
            self.assertFalse(run.state()['overlay_confirmation'])  # and nothing to confirm.
            self.assertTrue(run.state()['overlay_visible'])
            run.logged('Devices: Claimed. A game that is already running may need a restart to see it.')
            run.click(row_button(origin, 1, wide=True))            # Claiming again says so and changes nothing.
            run.logged('Devices: You already have this controller.')
            self.assertEqual(run.inventory.assignments()['assignments'], {FREE_PAD: 'guest'})

    def test_a_release_gives_the_controller_back_at_once(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            # Guest holds a second controller, so the menu stays in Guest's hands.
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            run.inventory.change(FREE_PAD, target='guest', requested='guest')
            run.redraw()
            run.click(row_button(origin, 0, wide=True))            # Release the first one
            run.wait_until(lambda: run.inventory.owner(GUEST_PAD) == 'unassigned', 'Release wrote nothing')
            record = run.inventory.assignments()
            self.assertEqual(record['assignments'], {FREE_PAD: 'guest', GUEST_PAD: 'unassigned'})
            self.assertNotIn('preview', record)
            self.assertFalse(run.state()['overlay_confirmation'])
            run.logged('Devices: Released.')
            self.assertTrue(run.state()['overlay_visible'])

    def test_releasing_the_last_control_closes_the_menu_nothing_could_operate(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.click(row_button(origin, 0, wide=True))            # Guest's only controller
            run.wait_until(lambda: run.inventory.owner(GUEST_PAD) == 'unassigned', 'Release wrote nothing')
            run.wait_for(lambda s: not s['overlay_visible'], "Guest's menu stayed open with nothing to operate it")
            run.logged("Devices: Released. That was Guest's last control, so the menu closes.")
            # Main's menu can open at once (an open menu is never taken over by the other player's controller).
            run.ipc('MENU left toggle')
            run.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'left', "Main's menu did not open")

    def test_a_claimed_controller_is_one_more_for_guest_and_main_keeps_its_own(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            # Guest now holds two controllers; nothing was taken from anyone.
            self.assertEqual([run.inventory.owner(k) for k in (MAIN_PAD, GUEST_PAD, FREE_PAD)],
                             ['main', 'guest', 'guest'])
            # The broker applies it; the next frame lists both as Guest's, each with a Release button.
            run.inventory.change(FREE_PAD, target='guest', requested='guest')
            run.redraw()
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'unassigned', 'the second row did not release')
            self.assertEqual(run.inventory.owner(GUEST_PAD), 'guest')

    def test_a_button_cannot_take_a_controller_another_player_now_has(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.inventory.set_saved_owner(FREE_PAD, 'main')        # Main claims it while Guest's menu shows it free.
            run.click(row_button(origin, 1, wide=True))
            run.logged('Devices: Another player has this controller.')
            self.assertEqual(run.inventory.owner(FREE_PAD), 'main')

    def test_guest_cannot_release_a_controller_that_is_not_its_own(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.inventory.set_saved_owner(GUEST_PAD, 'main')       # Main took it; the row still says Guest's.
            run.click(row_button(origin, 0, wide=True))
            run.logged('Devices: That controller is not yours to release.')
            self.assertEqual(run.inventory.owner(GUEST_PAD), 'main')

    def test_a_controller_that_fell_asleep_cannot_be_claimed(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            run.inventory.change(FREE_PAD, connected=False)        # Between the frame and the click.
            run.click(row_button(origin, 1, wide=True))
            run.logged('Devices: Wake the controller, then claim it.')
            self.assertEqual(run.inventory.owner(FREE_PAD), 'unassigned')

    def test_an_old_broker_that_cannot_recover_a_request_gets_none(self):
        with Run() as run:
            self.inventory(run)
            run.inventory.preview = 0                              # No assignment_preview 2.
            run.inventory.publish()
            origin = run.open_devices('guest')
            for row in (0, 1):
                run.click(row_button(origin, row, wide=True))
            run.stays(lambda: run.inventory.assignments() == {}, 'a request was written for an old broker')

    def test_guest_has_no_swap_and_no_way_to_move_main_s_controller(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            # Where Main's Swap button and Main's row buttons are, Guest's menu has nothing.
            for point in ((origin + SWAP[0], SWAP[1]), row_button(origin, 0, 1), row_button(origin, 0, 2)):
                run.click(point)
            run.stays(lambda: not run.state()['overlay_confirmation'], 'Guest started a swap or a move')
            self.assertNotIn('Swap needs', run.log_text())         # The button was never there to click.
            self.assertEqual(run.inventory.owner(MAIN_PAD), 'main')


class MainFlowsStayTests(unittest.TestCase):
    """Main keeps its three buttons per row, the confirmation and Undo."""

    def test_main_moves_a_free_controller_with_a_preview_that_it_must_confirm(self):
        with Run() as run:
            run.inventory.add(MAIN_PAD, 'Main controller', 'main')
            run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')
            run.inventory.add(FREE_PAD, 'Free controller', 'unassigned')
            origin = run.open_devices('main')
            run.click(row_button(origin, 2, button=1))             # Guest, on the free controller's row
            run.wait_for(lambda s: s['overlay_confirmation'], "Main's move asks for confirmation")
            record = run.inventory.assignments()
            self.assertEqual(record['preview']['assignments'], {FREE_PAD: 'guest'})
            self.assertEqual(run.inventory.owner(FREE_PAD), 'unassigned')       # Only a preview so far.
            run.click(UNDO)
            run.wait_for(lambda s: not s['overlay_confirmation'], 'Undo closes the preview')
            self.assertNotIn('preview', run.inventory.assignments())
            self.assertEqual(run.inventory.owner(FREE_PAD), 'unassigned')

    def test_main_still_lists_every_controller_including_the_sleeping_one(self):
        with Run() as run:
            run.inventory.add(MAIN_PAD, 'Main controller', 'main')
            run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')
            run.inventory.add(ASLEEP_PAD, 'Asleep controller', 'unassigned', connected=False)
            origin = run.open_devices('main')
            run.click(row_button(origin, 2, button=0))             # The sleeping controller's row, "Main"
            run.wait_for(lambda s: s['overlay_confirmation'], 'Main can still address a sleeping controller')

    def test_main_keeps_a_control_for_itself(self):
        with Run() as run:
            run.inventory.add(MAIN_PAD, 'Main controller', 'main')
            run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')
            origin = run.open_devices('main')
            run.click(row_button(origin, 0, button=1))             # Hand Main's only control to Guest
            run.logged('Devices: Keep a control for Main. Connect another or use Swap controls.')
            self.assertFalse(run.state()['overlay_confirmation'])
            self.assertEqual(run.inventory.assignments(), {})


class ControllerNumberTests(unittest.TestCase):
    """A controller is named by the number the routing service gave it (1-based), wherever the player sees it;
    a row without a usable number reads as it always did, and the menu never shows 0 or garbage."""

    def numbered(self, run):
        run.inventory.add(MAIN_PAD, 'Main controller', 'main', index=1)
        run.inventory.add(GUEST_PAD, 'Guest controller', 'guest', index=2)
        run.inventory.add(FREE_PAD, 'Free controller', 'unassigned', index=12)

    def test_claim_and_release_name_the_controller_by_its_number(self):
        with Run() as run:
            self.numbered(run)
            origin = run.open_devices('guest')
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            run.logged('Devices: Controller 12 claimed. A game that is already running may need a restart to see it.')
            run.click(row_button(origin, 1, wide=True))
            run.logged('Devices: Controller 12 is already yours.')
            run.inventory.change(FREE_PAD, target='guest', requested='guest')
            run.redraw()
            run.click(row_button(origin, 0, wide=True))
            run.wait_until(lambda: run.inventory.owner(GUEST_PAD) == 'unassigned', 'Release wrote nothing')
            run.logged('Devices: Controller 2 released.')

    def test_main_s_move_asks_about_the_controller_by_its_number(self):
        with Run() as run:
            self.numbered(run)
            origin = run.open_devices('main')
            run.click(row_button(origin, 2, button=1))
            run.wait_for(lambda s: s['overlay_confirmation'], "Main's move asks for confirmation")
            run.logged('Devices: Controller 12 → Guest')

    def test_a_row_without_a_usable_number_keeps_the_name_and_never_shows_zero(self):
        with Run() as run:
            run.inventory.add(MAIN_PAD, 'Main controller', 'main', index=1)
            run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')                 # an older service: no field
            run.inventory.add(FREE_PAD, 'Free controller', 'unassigned', index=0)     # not a number
            run.inventory.add(ASLEEP_PAD, 'Asleep controller', 'unassigned', index='x')
            run.inventory.change(ASLEEP_PAD, connected=True)
            origin = run.open_devices('guest')
            run.click(row_button(origin, 1, wide=True))
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'Claim wrote nothing')
            run.logged('Devices: Claimed. A game that is already running may need a restart to see it.')
            run.click(row_button(origin, 1, wide=True))
            run.logged('Devices: You already have this controller.')
            self.assertNotIn('Controller 0', run.log_text())


class ForgetTests(unittest.TestCase):
    """Main forgets a controller that is off: a confirmation, then a request file for the routing service. A gamepad and
    a Steam Controller on the session's receiver are offered it while they are not connected; a connected controller
    never is. What this does not show: the service carrying the request out (the registry and routing unit tests do) or
    a real controller being turned off."""

    def pads(self, run):
        run.inventory.add(MAIN_PAD, 'Main controller', 'main', index=1, backend='generic-pad')
        run.inventory.add(ASLEEP_PAD, 'Asleep controller', 'unassigned', connected=False, index=3, backend='generic-pad')
        run.inventory.add(FREE_PAD, 'Receiver controller', 'unassigned', connected=False, index=2, backend='native')

    def request(self, run, key):
        return run.inventory.directory / (key + '.forget')

    def test_forget_asks_first_and_cancel_changes_nothing(self):
        with Run() as run:
            self.pads(run)
            origin = run.open_devices('main')
            run.click(forget_button(origin, 1))
            run.wait_for(lambda s: s['overlay_confirmation'], 'Forget asks for confirmation')
            run.click(CANCEL)
            run.wait_for(lambda s: not s['overlay_confirmation'], 'Cancel closes the question')
            self.assertFalse(self.request(run, ASLEEP_PAD).exists())

    def test_forget_writes_one_private_request_after_the_confirmation(self):
        with Run() as run:
            self.pads(run)
            origin = run.open_devices('main')
            run.click(forget_button(origin, 1))
            run.wait_for(lambda s: s['overlay_confirmation'], 'Forget asks for confirmation')
            self.assertFalse(self.request(run, ASLEEP_PAD).exists(), 'nothing is asked before the confirmation')
            run.click(CONTINUE)
            run.wait_until(lambda: self.request(run, ASLEEP_PAD).exists(), 'Forget wrote no request')
            self.assertEqual(self.request(run, ASLEEP_PAD).stat().st_mode & 0o777, 0o600)
            run.logged('Devices: Forgetting Controller 3.')
            self.assertEqual([p.name for p in run.inventory.directory.glob('*.forget')], [ASLEEP_PAD + '.forget'])
            self.assertTrue((run.inventory.directory / (ASLEEP_PAD + '.json')).exists(), 'the service removes the record')

    def test_a_controller_that_turned_on_during_the_question_is_not_forgotten(self):
        with Run() as run:
            self.pads(run)
            origin = run.open_devices('main')
            run.click(forget_button(origin, 1))
            run.wait_for(lambda s: s['overlay_confirmation'], 'Forget asks for confirmation')
            run.inventory.change(ASLEEP_PAD, connected=True)
            time.sleep(.3)
            run.click(CONTINUE)
            run.logged('Devices: Turn it off or unplug it first.')
            self.assertFalse(self.request(run, ASLEEP_PAD).exists())

    def test_a_connected_controller_has_no_forget_button(self):
        with Run() as run:
            self.pads(run)
            run.inventory.change(FREE_PAD, connected=True)
            origin = run.open_devices('main')
            for row in (0, 2):
                run.click(forget_button(origin, row))
                self.assertFalse(run.state()['overlay_confirmation'], f'row {row} offered Forget')
            self.assertEqual(list(run.inventory.directory.glob('*.forget')), [])

    def test_a_steam_controller_that_is_asleep_is_forgotten_the_same_way(self):
        # The owner asked for it: the list keeps the Steam Controllers that woke once, and only gamepads could be dropped.
        with Run() as run:
            self.pads(run)
            origin = run.open_devices('main')
            run.click(forget_button(origin, 2))
            run.wait_for(lambda s: s['overlay_confirmation'], 'Forget asks for confirmation on a receiver row')
            run.click(CONTINUE)
            run.wait_until(lambda: self.request(run, FREE_PAD).exists(), 'Forget wrote no request')
            self.assertEqual([p.name for p in run.inventory.directory.glob('*.forget')], [FREE_PAD + '.forget'])

    def test_guest_is_never_offered_forget(self):
        with Run() as run:
            run.inventory.add(GUEST_PAD, 'Guest controller', 'guest', index=1, backend='generic-pad')
            origin = run.open_devices('guest')
            run.click(forget_button(origin, 0))
            self.assertFalse(run.state()['overlay_confirmation'])


class NothingSplitsByItselfTests(unittest.TestCase):
    """With Main alone on the full screen, a controller appearing, being claimed, released or moved leaves the
    layout as it is: only the overlay's layout buttons split the view."""

    def stays_single(self, run, label):
        run.stays(lambda: run.state()['layout'] == 'single', label, seconds=.5)

    def test_controllers_coming_going_and_moving_never_split_the_view(self):
        with Run(layout='single') as run:
            self.assertEqual(run.state()['layout'], 'single')
            run.inventory.add(MAIN_PAD, 'Main controller', 'main', index=1)
            self.stays_single(run, 'a controller appeared and the view split')
            run.inventory.add(FREE_PAD, 'Free controller', 'unassigned', index=2)
            run.inventory.add(GUEST_PAD, 'Guest controller', 'unassigned', index=3)
            self.stays_single(run, 'more controllers appeared and the view split')
            run.inventory.change(GUEST_PAD, connected=False)
            self.stays_single(run, 'a controller left and the view split')
            # Claimed and released, as Guest's menu or the broker would write it.
            run.inventory.set_saved_owner(FREE_PAD, 'guest')
            run.inventory.change(FREE_PAD, target='guest', requested='guest')
            run.redraw()
            self.stays_single(run, 'a claim split the view')
            run.inventory.set_saved_owner(FREE_PAD, 'unassigned')
            run.inventory.change(FREE_PAD, target='unassigned', requested='unassigned')
            run.redraw()
            self.stays_single(run, 'a release split the view')
            # Moved by Main from the overlay, kept and then moved back.
            origin = run.open_devices('main')
            run.click(row_button(origin, 1, button=1))
            run.wait_for(lambda s: s['overlay_confirmation'], 'the move asks for confirmation')
            run.logged('Devices: Controller 2 → Guest')
            run.click((origin + KEEP[0], KEEP[1]))
            run.wait_for(lambda s: not s['overlay_confirmation'], 'Keep closes the preview')
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest', 'the move was not saved')
            self.assertEqual(run.state()['layout'], 'single')
            self.stays_single(run, 'a move split the view')
            self.assertNotIn('Applied layout=vertical', run.log_text())
            self.assertNotIn('Applied layout=horizontal', run.log_text())


class DevicesPageTests(unittest.TestCase):
    """The Devices tab pages through its list, and each menu owner has its own page.

    Main's list also holds the host's mouse, keyboard and touchscreen, so it is longer than Guest's, and Main can
    be on its second page while Guest's second page is empty. A page that both menus shared left Guest looking at
    "No devices on this page" when it opened its menu after Main had been to the second page.
    """
    SIX = [letter * 32 for letter in '123456']

    def inventory(self, run):
        """Main holds six controllers, so five fill its first page and one is on the second. Guest lists its own
        and the free one that is awake: two rows, which is its first page and nothing more."""
        for key in self.SIX:
            run.inventory.add(key, 'Main pad ' + key[0], 'main')
        run.inventory.add(GUEST_PAD, 'Guest controller', 'guest')
        run.inventory.add(FREE_PAD, 'Free controller', 'unassigned')

    def page(self, run, owner):
        state = run.state()
        self.assertEqual(state['overlay_owner'], owner)
        return state['device_page']

    def turn(self, run, origin, button, owner, page):
        run.click((origin + button[0], button[1]))
        run.wait_for(lambda s: s['overlay_owner'] == owner and s['device_page'] == page,
                     f'{owner} did not turn to page {page}')

    def test_guest_opens_on_its_first_page_when_main_left_its_menu_on_the_second(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('main')
            self.assertEqual(self.page(run, 'left'), 0)
            self.turn(run, origin, NEXT, 'left', 1)
            # Guest opens its own menu on the Devices tab. Its page is its own: the first, with its two rows.
            origin = run.open_devices('guest')
            self.assertEqual(self.page(run, 'right'), 0, "Main's page followed Guest into its menu")
            run.click(row_button(origin, 1, wide=True))            # The free controller's row, on the first page
            run.wait_until(lambda: run.inventory.owner(FREE_PAD) == 'guest',
                           "Guest's first page showed no rows: it was on Main's page")
            # Main opens its menu again and is back on its own page, the second.
            run.open_devices('main')
            self.assertEqual(self.page(run, 'left'), 1, "Guest's page replaced Main's")

    def test_turning_the_page_in_one_menu_leaves_the_page_of_the_other(self):
        with Run() as run:
            self.inventory(run)
            origin = run.open_devices('guest')
            self.turn(run, origin, NEXT, 'right', 1)               # Guest's second page has nothing on it
            origin = run.open_devices('main')
            self.assertEqual(self.page(run, 'left'), 0, "Guest's page followed Main into its menu")
            self.turn(run, origin, NEXT, 'left', 1)
            self.turn(run, origin, PREVIOUS, 'left', 0)
            origin = run.open_devices('guest')
            self.assertEqual(self.page(run, 'right'), 1, "Main's Previous turned Guest's page")
            self.turn(run, origin, PREVIOUS, 'right', 0)
            self.assertEqual(self.page(run, 'right'), 0)


class SwapTests(unittest.TestCase):
    def pads(self, run, held=((MAIN_PAD, MAIN_PAD_2), (GUEST_PAD, GUEST_PAD_2))):
        for key, owner in [(k, 'main') for k in held[0]] + [(k, 'guest') for k in held[1]]:
            run.inventory.add(key, 'Pad ' + key[:2], owner)

    def press(self, run, *keys):
        """What the broker shows for the half second after a press: the pads marked active."""
        for key in keys:
            run.inventory.change(key, active=True)
        run.redraw()
        time.sleep(.2)
        for key in keys:
            run.inventory.change(key, active=False)
        run.redraw()

    def swap_request(self, run):
        run.click(SWAP)
        return run.wait_for(lambda s: s['overlay_confirmation'], 'Swap controls asks for confirmation')

    def test_one_controller_each_swaps_as_before(self):
        with Run() as run:
            self.pads(run, ((MAIN_PAD,), (GUEST_PAD,)))
            run.open_devices('main')
            self.swap_request(run)
            record = run.inventory.assignments()
            self.assertEqual(record['preview']['assignments'], {MAIN_PAD: 'guest', GUEST_PAD: 'main'})
            self.assertEqual(sorted(record['handoff']), sorted([MAIN_PAD, GUEST_PAD]))
            self.assertEqual(run.inventory.owner(MAIN_PAD), 'main')            # Only a preview until Keep.
            run.click(KEEP)
            run.wait_until(lambda: run.inventory.owner(MAIN_PAD) == 'guest', 'Keep did not save the swap')
            self.assertEqual(run.inventory.assignments()['assignments'], {MAIN_PAD: 'guest', GUEST_PAD: 'main'})

    def test_with_several_controllers_and_none_pressed_the_button_stays_and_asks_for_a_press(self):
        with Run() as run:
            self.pads(run)
            run.open_devices('main')
            run.click(SWAP)
            run.logged('Devices: Press a button on the controller to swap.')
            run.stays(lambda: not run.state()['overlay_confirmation'], 'a swap started although nothing was pressed')
            self.assertEqual(run.inventory.assignments(), {})
            self.assertTrue(run.state()['overlay_visible'])
            # The button is still there: pressing, then asking again, works.
            self.press(run, MAIN_PAD_2, GUEST_PAD)
            self.swap_request(run)

    def test_the_controllers_pressed_last_are_swapped_and_the_others_stay(self):
        with Run() as run:
            self.pads(run)
            run.open_devices('main')
            self.press(run, MAIN_PAD_2, GUEST_PAD)
            self.swap_request(run)
            record = run.inventory.assignments()
            self.assertEqual(record['preview']['assignments'], {MAIN_PAD_2: 'guest', GUEST_PAD: 'main'})
            self.assertEqual(sorted(record['handoff']), sorted([MAIN_PAD_2, GUEST_PAD]))
            run.click(KEEP)
            run.wait_until(lambda: run.inventory.owner(MAIN_PAD_2) == 'guest', 'Keep did not save the swap')
            self.assertEqual(run.inventory.assignments()['assignments'], {MAIN_PAD_2: 'guest', GUEST_PAD: 'main'})
            self.assertEqual([run.inventory.owner(k) for k in (MAIN_PAD, GUEST_PAD_2)], ['main', 'guest'])

    def test_a_later_press_wins_over_an_earlier_one(self):
        with Run() as run:
            self.pads(run)
            run.open_devices('main')
            self.press(run, MAIN_PAD, GUEST_PAD_2)
            self.press(run, MAIN_PAD_2)
            self.swap_request(run)
            self.assertEqual(run.inventory.assignments()['preview']['assignments'],
                             {MAIN_PAD_2: 'guest', GUEST_PAD_2: 'main'})

    def test_two_controllers_pressed_together_are_not_guessed(self):
        with Run() as run:
            self.pads(run)
            run.open_devices('main')
            self.press(run, MAIN_PAD, MAIN_PAD_2, GUEST_PAD)
            run.click(SWAP)
            run.logged('Devices: Press a button on the controller to swap.')
            self.assertEqual(run.inventory.assignments(), {})
            self.press(run, MAIN_PAD)                              # One of them again settles it.
            self.swap_request(run)
            self.assertEqual(run.inventory.assignments()['preview']['assignments'],
                             {MAIN_PAD: 'guest', GUEST_PAD: 'main'})

    def test_a_sleeping_controller_is_not_a_candidate(self):
        with Run() as run:
            self.pads(run, ((MAIN_PAD, MAIN_PAD_2), (GUEST_PAD,)))
            run.inventory.change(MAIN_PAD_2, connected=False, input_status={'shortcut_ready': False})
            run.open_devices('main')
            self.swap_request(run)                                 # Main has one awake control: no guessing.
            self.assertEqual(run.inventory.assignments()['preview']['assignments'],
                             {MAIN_PAD: 'guest', GUEST_PAD: 'main'})

    def test_the_button_is_gone_when_a_player_has_no_control_to_swap(self):
        with Run() as run:
            self.pads(run, ((MAIN_PAD, MAIN_PAD_2), ()))
            run.open_devices('main')
            run.click(SWAP)
            run.stays(lambda: not run.state()['overlay_confirmation'], 'a swap started with nobody to swap with')
            # Clicking a button that is there would have said so; clicking nothing says nothing.
            self.assertNotIn('Devices: Swap needs', run.log_text())
            self.assertNotIn('Press a button on the controller to swap', run.log_text())


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: controller-claim-integration.py COMPOSITOR [unittest options]')
    if os.geteuid() == 0:
        raise RuntimeError('run this test as an ordinary user')
    BINARY = str(Path(sys.argv.pop(1)).resolve(strict=True))
    unittest.main()
