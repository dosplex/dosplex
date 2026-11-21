#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=360
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The display follower applies live display changes to the nested Gamescope and the launcher starts it.

Process-level tests run the real follower against a fake control server (a SOCK_SEQPACKET socket in a
temporary directory, served by this process as the current user and made trusted through
DOSPLEX_SESSION_CONTROL_SERVER_UID) and a fake xprop/xrandr on PATH that behave like Gamescope's nested
Xwayland: a GAMESCOPE_XWAYLAND_MODE_CONTROL request is consumed after a delay, which resizes the screen and
deletes the property. Launcher tests run the real launcher with stub Gamescope and Steam executables.

The window fit (a one-shot GAMESCOPE_FORCE_WINDOWS_FULLSCREEN for the focused window, decided from the app Gamescope
names for its focus window, its WM_NORMAL_HINTS, its size and the screen's, and switched off again inside the same poll,
as soon as the window has the screen size and at the latest after a quarter of a second) has three kinds of case. The
decision and the parsing of the tools' output are pure functions with their own tests. Scripted tools, on a stand-in
clock that makes the quarter of a second cost nothing, show the calls one poll makes, in which order a window is read
and the property written, and what a failing tool or a stop leaves behind; they also act as Gamescope's focus pass does
(the focus window with an app id gets the screen size while the property is on, Steam's own window always), keep the
input focus apart from the focus window, and replay the Steam Machine's launch sequences of 2026-10-20 (Valheim,
Absolum). And the real follower runs against a fake xprop and xdotool that also hold windows with their hints, app id
and size, publish GAMESCOPE_FOCUSED_WINDOW the way Gamescope does (an empty property when nothing is focused), and run
the same focus pass; they can make the reads of a window fail or take long while the property is on, and keep an xprop
that has set the property running, so that a failure or a stop meets the one-shot at the worst moment.

No real X server, Gamescope or Steam is involved, so this does not show that Gamescope accepts the properties or
resizes the window; that needs the hardware check.
"""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FOLLOWER = ROOT / 'guest/dosplex-side-display-follower.py'
SUPERVISOR = ROOT / 'packaging/steamos/dosplex-side-supervisor.py'
LAUNCHER = ROOT / 'guest/dosplex-user-side-session.sh'
INSTANCE = 'a1' * 16
DISPLAY = ':99'
MODE = 'GAMESCOPE_XWAYLAND_MODE_CONTROL'
SCALER = 'GAMESCOPE_NEW_SCALING_SCALER'
FILTER = 'GAMESCOPE_NEW_SCALING_FILTER'
FOCUS = 'GAMESCOPE_FOCUSED_WINDOW'
FORCE = 'GAMESCOPE_FORCE_WINDOWS_FULLSCREEN'
FOCUSABLE = 'GAMESCOPE_FOCUSABLE_APPS'
FOCUSED_APP = 'GAMESCOPE_FOCUSED_APP'
FOCUS_APP = 'GAMESCOPE_FOCUSED_APP_GFX'
GAME, FIXED, RANGED = 0x600003, 0x600005, 0x600007   # X window ids
STEAM, VALHEIM, ABSOLUM = 0x2000035, 0x4800001, 0x4a00001   # Steam's own window and two game windows (the Machine, 2026-10-20)
STEAM_UI, GAME_APP, OTHER_APP = 769, 892970, 262830  # Gamescope app ids: Steam's own interface and two games
FAST = ['--poll-interval', '0.05', '--apply-timeout', '5']

spec = importlib.util.spec_from_file_location('display_follower', FOLLOWER)
follower = importlib.util.module_from_spec(spec)
spec.loader.exec_module(follower)

# One script installed as xprop, xrandr and xdotool. It keeps a tiny X server in a JSON file: a mode-control
# request is consumed after `delay` seconds (screen resized, property deleted), at once if the size is
# unchanged, or never when `gamescope` is 'ignore'. It prints what the real tools print. A key press of xdotool
# only shows in the log of calls. A `budget` makes it behave like a Steam that watches the screen: a size above that many
# pixels is put back to 1920x1080 `undo_after` seconds after the screen reached it. Three switches let a test model what a stop or a failure meets in the one-shot of the
# window fit: the window reads fail (or are slow) only while the forced fullscreen is on, and an xprop that sets the
# forced fullscreen to 1 stays running for a while after the property is set, as a slow xprop does.
FAKE_X = r'''#!@PYTHON@
import fcntl, json, os, sys, time

MODE = 'GAMESCOPE_XWAYLAND_MODE_CONTROL'
FORCE = 'GAMESCOPE_FORCE_WINDOWS_FULLSCREEN'
LINGER = [0]    # seconds the tool keeps running once it has done its work and let go of the state


def fit_focused(state):
    """Gamescope's focus pass: the focus window with an app id gets the screen size while the forced fullscreen is
    on, and Steam's own window (app 769) gets it always, unless the test made it one that does not take it (stubborn).
    Its app id is GAMESCOPE_FOCUSED_APP_GFX when a test set that, else its STEAM_GAME."""
    focused = state['props'].get('GAMESCOPE_FOCUSED_WINDOW') or []
    held = state['windows'].get(str(focused[0])) if focused else None
    if not held or held['size'] is None or held.get('stubborn'):
        return
    app = (state['props'].get('GAMESCOPE_FOCUSED_APP_GFX') or [held['app']])[0]
    if app and ((state['props'].get(FORCE) or [0])[0] or app == 769):
        held['size'] = list(state['screen'])


def settle(state):
    undo = state.get('undo')
    if undo and time.time() >= undo['due']:
        state['screen'] = [1920, 1080]    # Steam puts a screen that is too large back
        state['undo'] = None
    pending = state['pending']
    if pending and time.time() >= pending['due']:
        state['screen'] = pending['size']
        state['props'].pop(MODE, None)
        state['pending'] = None
        state['applied_at'] = time.time()
        budget = state.get('budget')
        if budget and pending['size'][0] * pending['size'][1] > budget:
            state['undo'] = {'due': pending['due'] + state.get('undo_after', 0.3)}
        fit_focused(state)    # a root resize marks the focus dirty


def xrandr(state):
    width, height = state['screen']
    print('Screen 0: minimum 16 x 16, current %d x %d, maximum 32767 x 32767' % (width, height))
    print('gamescope connected %dx%d+0+0 (normal left inverted right x axis y axis) 0mm x 0mm' % (width, height))
    print('   %dx%d     59.98*+' % (width, height))
    return 0


def forced_now(state):
    return bool((state['props'].get(FORCE) or [0])[0])


def reads_fail(state):
    """Whether the reads of a window fail: a test made them fail, or made them fail while the forced fullscreen is on."""
    return state['hint_error'] or (state.get('fail_reads_while_forced') and forced_now(state))


def xprop_window(state, args):
    """xprop -id WINDOW NAME...: what the test stored for the window, one property to a line in the order asked, or the
    tool's own BadWindow error."""
    if len(args) < 3 or any(name not in ('WM_NORMAL_HINTS', 'STEAM_GAME') for name in args[2:]):
        return 1
    window = str(int(args[1], 0))
    if reads_fail(state) or window not in state['windows']:
        sys.stderr.write('X Error of failed request:  BadWindow (invalid Window parameter)\n'
                         '  Major opcode of failed request:  20 (X_GetProperty)\n'
                         '  Resource id in failed request:  %s\n' % args[1])
        return 1
    held = state['windows'][window]
    for name in args[2:]:
        if name == 'WM_NORMAL_HINTS':
            sys.stdout.write(held['hints'] or 'WM_NORMAL_HINTS:  not found.\n')
        elif held['app'] is None:
            sys.stdout.write('STEAM_GAME:  not found.\n')
        else:
            sys.stdout.write('STEAM_GAME(CARDINAL) = %d\n' % held['app'])
    return 0


def xprop(state, args):
    if args[:1] == ['-id']:
        return xprop_window(state, args)
    if args[:1] != ['-root']:
        return 1
    args = args[1:]
    if args and not any(arg.startswith('-') for arg in args):
        if any(name in state['fail_props'] for name in args):
            sys.stderr.write('xprop: error: cannot read %s\n' % args[0])
            return 1
        for name in args:
            values = state['props'].get(name)
            if values is None:
                print('%s:  not found.' % name)
            else:
                print('%s(CARDINAL) = %s' % (name, ', '.join(str(v) for v in values)))
        return 0
    if len(args) == 6 and args[0] == '-f' and args[2] == '32c' and args[3] == '-set' and args[1] == args[4]:
        name, values = args[1], [int(v) for v in args[5].split(',')]
        if name == 'GAMESCOPE_FORCE_WINDOWS_FULLSCREEN' and state['fail_force_set']:
            sys.stderr.write('xprop: error: cannot change %s\n' % name)
            return 1
        state['props'][name] = values
        if name == MODE:
            state['undo'] = None
        if name == FORCE:
            fit_focused(state)    # the property change marks the focus dirty
            if values[0]:
                LINGER[0] = state.get('linger_set', 0)    # the property is set; xprop takes its time to end
        if name == MODE and state['gamescope'] == 'apply' and len(values) == 4:
            if values[1:3] == state['screen']:
                state['props'].pop(MODE)
            else:
                state['pending'] = {'size': values[1:3], 'due': time.time() + state['delay']}
        return 0
    sys.stderr.write('xprop: unexpected arguments %r\n' % (args,))
    return 1


def xdotool(state, args):
    """xdotool getwindowgeometry --shell WINDOW: the size the test stored for the window, as the real tool prints it.
    xdotool key NAME: nothing to change in the fake X, the call is in the log; a test can make it fail."""
    if len(args) == 3 and args[:2] == ['getwindowgeometry', '--shell']:
        held = state['windows'].get(str(int(args[2], 0)))
        if reads_fail(state) or held is None or held['size'] is None:
            sys.stderr.write('XGetWindowAttributes[%s] failed (code=1)\n' % args[2])
            return 1
        if forced_now(state):
            time.sleep(state.get('slow_reads_while_forced', 0))    # a slow read: the lock is held meanwhile
        print('WINDOW=%d\nX=0\nY=0\nWIDTH=%d\nHEIGHT=%d\nSCREEN=0' % (int(args[2], 0), *held['size']))
        return 0
    if len(args) == 2 and args[0] == 'windowfocus':
        if state['windows'].get(str(int(args[1], 0))) is None and int(args[1], 0) != 1023:
            sys.stderr.write('XSetInputFocus failed: BadWindow (invalid Window parameter)\n')
            return 1
        return 0    # only the log of calls shows the focus cycle
    if len(args) != 2 or args[0] != 'key':
        sys.stderr.write('xdotool: unexpected arguments %r\n' % (args,))
        return 1
    if state['fail_key']:
        sys.stderr.write('Error: cannot send the key %s\n' % args[1])
        return 1
    return 0


def main():
    tool = os.path.basename(sys.argv[0])
    path = os.environ['FAKE_X_STATE']
    with open(path + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path) as stream:
            state = json.load(stream)
        state['calls'].append({'t': time.time(), 'argv': [tool] + sys.argv[1:]})
        if state['fail_next'] > 0 or state['gone'] or os.environ.get('DISPLAY') != state['display']:
            state['fail_next'] = max(0, state['fail_next'] - 1)
            if tool == 'xprop':
                sys.stderr.write('xprop:  unable to open display \'%s\'\n' % os.environ.get('DISPLAY'))
            else:
                sys.stderr.write("Can't open display %s\n" % os.environ.get('DISPLAY'))
            status = 1
        else:
            settle(state)
            if tool == 'xrandr':
                status = xrandr(state)
            elif tool == 'xwininfo':
                print('\nxwininfo: Window id: 1023 (the root window) (has no name)')
                status = 0
            elif tool == 'xdotool':
                status = xdotool(state, sys.argv[1:])
            else:
                status = xprop(state, sys.argv[1:])
        with open(path + '.tmp', 'w') as stream:
            json.dump(state, stream)
        os.replace(path + '.tmp', path)
    if LINGER[0]:
        time.sleep(LINGER[0])
    sys.exit(status)


main()
'''


def run_focus_pass(state):
    """FAKE_X's fit_focused, for a change the test makes to the fake X's state."""
    namespace = {}
    exec(FAKE_X.split('\ndef settle', 1)[0].split('\n', 1)[1], namespace)
    namespace['fit_focused'](state)


def hints_text(minimum=None, maximum=None):
    """What `xprop -id WINDOW WM_NORMAL_HINTS` prints for a window that states these sizes, in the real tool's words."""
    lines = ['WM_NORMAL_HINTS(WM_SIZE_HINTS):', '\t\tprogram specified location: 0, 0',
             '\t\tprogram specified size: 1920 by 2160']
    if minimum:
        lines.append('\t\tprogram specified minimum size: %d by %d' % minimum)
    if maximum:
        lines.append('\t\tprogram specified maximum size: %d by %d' % maximum)
    lines += ['\t\tprogram specified resize increment: 1 by 1', '\t\twindow gravity: NorthWest']
    return '\n'.join(lines) + '\n'


NO_SIZES = hints_text()   # WM_NORMAL_HINTS published without a minimum or a maximum size


def held(hints=NO_SIZES, app=GAME_APP, size=(1280, 720), stubborn=False):
    """A window as the fake X holds it: its WM_NORMAL_HINTS as xprop prints them (None: not published), its STEAM_GAME
    app id (None: not published), its size for xdotool (None: unknown), and whether it keeps its size when Gamescope
    fits it (stubborn)."""
    return {'hints': hints, 'app': app, 'size': list(size) if size else None, 'stubborn': stubborn}


def wait_for(predicate, timeout=10.0, step=0.02):
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= end:
            return value
        time.sleep(step)


class FakeX:
    """The state file the fake xprop, xrandr and xdotool share with the test."""
    def __init__(self, path):
        self.path = path
        self.reset()

    def locked(self, change):
        with open(str(self.path) + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text())
            change(state)
            temporary = Path(str(self.path) + '.new')
            temporary.write_text(json.dumps(state))
            os.replace(temporary, self.path)

    def reset(self, **changes):
        state = {'display': DISPLAY, 'screen': [1280, 720], 'props': {}, 'pending': None, 'gamescope': 'apply',
                 'delay': 0.1, 'gone': False, 'fail_next': 0, 'applied_at': None, 'calls': [],
                 'windows': {}, 'hint_error': False, 'fail_force_set': False, 'fail_props': [], 'fail_key': False,
                 'fail_reads_while_forced': False, 'slow_reads_while_forced': 0, 'linger_set': 0,
                 'budget': None, 'undo_after': 0.3, 'undo': None}
        state.update(changes)
        self.path.write_text(json.dumps(state))

    def update(self, **changes):
        self.locked(lambda state: state.update(changes))

    def read(self):
        return json.loads(self.path.read_text())

    def calls(self):
        return self.read()['calls']

    def sets(self):
        """The property writes other than the scaling filter, which filter_sets() lists."""
        return [call['argv'] for call in self.calls() if '-set' in call['argv'] and FILTER not in call['argv']]

    def filter_sets(self):
        return [call['argv'][-1] for call in self.calls() if '-set' in call['argv'] and FILTER in call['argv']]

    def tools(self):
        return {call['argv'][0] for call in self.calls()}

    def focus(self, window):
        """What Gamescope publishes: the focused window's id, or an empty property when no window is focused. Its
        focus pass runs at once (Steam's own window gets the screen size)."""
        def change(state):
            state['props'][FOCUS] = [] if window is None else [window]
            run_focus_pass(state)
        self.locked(change)

    def window(self, window, **fields):
        """A window the fake X holds (see held); again, to change what it publishes."""
        self.locked(lambda state: state['windows'].__setitem__(str(window), held(**fields)))

    def hints(self, window, text):
        """Change a held window's WM_NORMAL_HINTS as xprop prints them (None: not published)."""
        self.locked(lambda state: state['windows'][str(window)].__setitem__('hints', text))

    def window_reads(self, window):
        """How often the window's own properties were read."""
        return len([call for call in self.calls() if call['argv'][:3] == ['xprop', '-id', '0x%x' % window]])

    def force_writes(self):
        """The values the follower wrote to the forced-fullscreen property, in order, whether the write took or not."""
        return [argv[-1] for argv in self.sets() if argv[-2] == FORCE]

    def force_times(self):
        """When each write of the forced fullscreen reached the fake X, as (time, value). The property is on from a 1
        to the next 0."""
        return [(call['t'], call['argv'][-1]) for call in self.calls()
                if '-set' in call['argv'] and call['argv'][-2] == FORCE]

    def forced(self):
        """The value the property holds now, None while it is not set."""
        return self.read()['props'].get(FORCE)

    def games(self, *apps):
        """What Gamescope lists as focusable: Steam's own interface, always, and the games that run."""
        self.locked(lambda state: state['props'].__setitem__(FOCUSABLE, [STEAM_UI, *apps]))

    def focused_app(self, app):
        """The app Gamescope presents: Steam's interface on a side that is idle, the game once it has the focus."""
        self.locked(lambda state: state['props'].__setitem__(FOCUSED_APP, [app]))

    def keys(self):
        """The key presses sent with xdotool, in order, as (time, argv)."""
        return [(call['t'], call['argv']) for call in self.calls()
                if call['argv'][0] == 'xdotool' and call['argv'][1] == 'key']

    def focus_cycles(self):
        """The windows the follower gave the X input focus, in order (xdotool windowfocus)."""
        return [int(call['argv'][2], 0) for call in self.calls() if call['argv'][:2] == ['xdotool', 'windowfocus']]

    def reads(self, name):
        """How often the property was read."""
        return len([call for call in self.calls() if call['argv'] == ['xprop', '-root', name]])

    def size(self, window):
        """The held window's size now, as [width, height]."""
        return self.read()['windows'][str(window)]['size']

    def screen_reads(self):
        return len([call for call in self.calls() if call['argv'][0] == 'xrandr'])


class FakeControl:
    """The supervisor's control socket as the follower sees it: one request, one reply."""
    def __init__(self, path, instance=INSTANCE):
        self.path, self.instance = path, instance
        self.lock = threading.Lock()
        self.live = {'x': 0, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 1920, 'game_height': 2160,
                     'scale_mode': 'fit', 'display_revision': 1, 'layout': 'vertical'}
        self.modes = {'get': 'ok', 'report': 'ok'}   # ok | drop (hang up) | stall (stay silent) | refuse | garbage
        self.drop_reports = 0                         # drop this many report attempts, then acknowledge
        self.stall_gets = 0                           # stall this many get requests, then answer
        self.messages = []
        self.reports = []
        self.gets = 0
        self.get_times = []                           # when each get request was served, time.monotonic()
        self.listener = None
        self.thread = None
        self.stopping = threading.Event()

    def start(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(str(self.path))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.listener:
            self.listener.close()

    def set_live(self, **fields):
        with self.lock:
            self.live.update(fields)

    def snapshot(self):
        with self.lock:
            return list(self.messages), list(self.reports), self.gets

    def times(self):
        with self.lock:
            return list(self.get_times)

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    self.handle(connection)
                except OSError:
                    pass

    def handle(self, connection):
        raw = connection.recv(65536)
        if not raw:
            return
        try:
            request = json.loads(raw)
        except ValueError:
            request = None
        kind = request.get('display') if isinstance(request, dict) else None
        with self.lock:
            self.messages.append(request)
            valid = isinstance(request, dict) and request.get('instance') == self.instance and kind in ('get', 'report')
            mode = self.modes.get(kind, 'ok')
            if kind == 'get':
                self.gets += 1
                self.get_times.append(time.monotonic())
                if self.stall_gets > 0:
                    self.stall_gets -= 1
                    mode = 'stall'
            if kind == 'report':
                self.reports.append(request)
                if self.drop_reports > 0:
                    self.drop_reports -= 1
                    mode = 'drop'
            live = dict(self.live)
        if mode == 'drop':
            return
        if mode == 'stall':
            # A busy supervisor: the request is taken and left without a reply, and every other connection waits
            # behind it, until the follower gives up and hangs up (recv then returns b'').
            connection.settimeout(5)
            connection.recv(1)
            return
        if mode == 'garbage':
            connection.sendall(b'this is not json')
        elif not valid or mode == 'refuse':
            connection.sendall(b'{"ok":false}')
        elif kind == 'get':
            connection.sendall(json.dumps({'ok': True, 'live': live}).encode())
        else:
            connection.sendall(b'{"ok":true}')


class FollowerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-follower-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        fake = self.bin / 'fake-x'
        fake.write_text(FAKE_X.replace('@PYTHON@', sys.executable))
        fake.chmod(0o755)
        (self.bin / 'xprop').symlink_to('fake-x')
        (self.bin / 'xrandr').symlink_to('fake-x')
        (self.bin / 'xdotool').symlink_to('fake-x')
        (self.bin / 'xwininfo').symlink_to('fake-x')
        self.x = FakeX(self.root / 'x.json')
        self.control = FakeControl(self.root / 'control.sock')
        self.addCleanup(self.control.stop)
        self.process = None
        self.addCleanup(self.stop_follower)

    def start_follower(self, *args, server_uid='self', environment=None):
        env = {'PATH': str(self.bin), 'DISPLAY': DISPLAY, 'FAKE_X_STATE': str(self.x.path),
               'DOSPLEX_SESSION_CONTROL': str(self.control.path), 'DOSPLEX_SESSION_INSTANCE': INSTANCE}
        if server_uid == 'self':
            env['DOSPLEX_SESSION_CONTROL_SERVER_UID'] = str(os.getuid())
        elif server_uid is not None:
            env['DOSPLEX_SESSION_CONTROL_SERVER_UID'] = str(server_uid)
        env.update(environment or {})
        self.log = (self.root / 'follower.log').open('w')
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([sys.executable, str(FOLLOWER), *(args or FAST)], env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log)
        return self.process

    def stop_follower(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def log_lines(self):
        return (self.root / 'follower.log').read_text().splitlines()

    def wait_for_report(self, revision):
        def found():
            return [r for r in self.control.snapshot()[1] if r['revision'] == revision]
        reports = wait_for(found)
        self.assertTrue(reports, 'no report for revision %d; follower log: %s' % (revision, self.log_lines()))
        return reports[0]

    def wait_for_result(self, revision, result):
        def found():
            return [r for r in self.control.snapshot()[1] if r['revision'] == revision and r['result'] == result]
        reports = wait_for(found)
        self.assertTrue(reports, 'no "%s" report for revision %d; reports: %s; follower log: %s'
                        % (result, revision, self.results(), self.log_lines()))
        return reports[0]

    def results(self, revision=None):
        """What the follower reported, in order, as (revision, result); one revision's results with an argument."""
        return [(r['revision'], r['result']) for r in self.control.snapshot()[1]
                if revision in (None, r['revision'])]

    def wait_for_gets(self, more):
        target = self.control.snapshot()[2] + more
        self.assertTrue(wait_for(lambda: self.control.snapshot()[2] >= target), 'the follower stopped polling')

    def alive(self):
        return self.process.poll() is None


class ApplyTests(FollowerCase):
    def test_new_revision_sets_the_mode_waits_for_gamescope_then_sets_the_scaler_and_reports(self):
        self.control.start()
        self.control.set_live(display_revision=7, game_width=1920, game_height=2160, scale_mode='fit')
        self.start_follower()
        report = self.wait_for_report(7)
        self.assertEqual(report, {'instance': INSTANCE, 'display': 'report', 'revision': 7, 'game_width': 1920,
                                  'game_height': 2160, 'result': 'applied', 'detail': '1920x2160 fit'})
        self.assertEqual(list(report), ['instance', 'display', 'revision', 'game_width', 'game_height',
                                        'result', 'detail'])
        messages = self.control.snapshot()[0]
        self.assertEqual(messages[0], {'instance': INSTANCE, 'display': 'get'})
        state = self.x.read()
        self.assertEqual(state['screen'], [1920, 2160])
        self.assertNotIn(MODE, state['props'])
        self.assertEqual(state['props'][SCALER], [2])
        mode_set = ['xprop', '-root', '-f', MODE, '32c', '-set', MODE, '0, 1920, 2160, 1']
        scaler_set = ['xprop', '-root', '-f', SCALER, '32c', '-set', SCALER, '2']
        self.assertEqual(self.x.sets(), [mode_set, scaler_set])
        # The scaler is only written after Gamescope consumed the mode request.
        scaler_time = next(call['t'] for call in self.x.calls() if call['argv'] == scaler_set)
        self.assertGreaterEqual(scaler_time, state['applied_at'])
        self.assertTrue(any(call['argv'][0] == 'xrandr' for call in self.x.calls()))
        # One log line for the apply.
        self.assertEqual(len([line for line in self.log_lines() if 'revision 7' in line]), 1, self.log_lines())

    def test_a_revision_it_already_settled_is_a_no_op_even_when_other_fields_differ(self):
        self.control.start()
        self.control.set_live(display_revision=4)
        self.start_follower()
        self.wait_for_report(4)
        sets_before = self.x.sets()
        self.control.set_live(game_width=1280, game_height=720, scale_mode='stretch')   # same revision
        self.wait_for_gets(8)
        self.assertEqual(self.x.sets(), sets_before)
        self.assertEqual(self.x.read()['screen'], [1920, 2160])
        self.assertEqual(len(self.control.snapshot()[1]), 1)
        # A new revision applies again.
        self.control.set_live(display_revision=5)
        report = self.wait_for_report(5)
        self.assertEqual((report['result'], report['game_width'], report['game_height']), ('applied', 1280, 720))
        self.assertEqual(self.x.read()['screen'], [1280, 720])
        self.assertEqual(self.x.read()['props'][SCALER], [4])

    def test_scaler_only_change_leaves_the_screen_alone_and_maps_fit_and_stretch(self):
        self.control.start()
        self.start_follower()
        self.wait_for_report(1)
        self.x.update(calls=[])
        self.control.set_live(display_revision=2, scale_mode='stretch')
        report = self.wait_for_report(2)
        self.assertEqual(report['result'], 'applied')
        self.assertEqual(self.x.sets(), [['xprop', '-root', '-f', SCALER, '32c', '-set', SCALER, '4']])
        self.assertEqual(self.x.read()['props'][SCALER], [4])
        self.control.set_live(display_revision=3, scale_mode='fit')
        self.wait_for_report(3)
        self.assertEqual(self.x.read()['props'][SCALER], [2])
        self.assertEqual(len(self.x.sets()), 2)
        self.assertEqual(self.x.read()['screen'], [1920, 2160])

    def test_zoom_writes_the_fit_scaler(self):
        self.control.start()
        self.start_follower()
        self.wait_for_report(1)
        self.control.set_live(display_revision=2, scale_mode='stretch')
        self.wait_for_report(2)
        self.assertEqual(self.x.read()['props'][SCALER], [4])
        self.control.set_live(display_revision=3, scale_mode='zoom')
        report = self.wait_for_report(3)
        self.assertEqual(report['result'], 'applied')
        self.assertEqual(self.x.read()['props'][SCALER], [2])
        self.assertEqual(self.x.read()['screen'], [1920, 2160])

    def test_the_filter_is_written_after_the_scaler_and_logged(self):
        self.control.start()
        self.start_follower()
        self.wait_for_report(1)
        self.assertEqual(self.x.filter_sets(), [str(follower.scaling_filter('fit', (1920, 2160), (960, 1080)))])
        self.assertTrue(any('filter' in line and 'scaler fit (2)' in line for line in self.log_lines()), self.log_lines())

    def test_the_first_poll_applies_even_when_the_screen_already_has_the_right_size(self):
        self.x.reset(screen=[1920, 2160])
        self.control.start()
        self.start_follower()
        report = self.wait_for_report(1)
        self.assertEqual(report['result'], 'applied')
        # Nothing asks Gamescope to change the mode; only the scaler is written.
        self.assertEqual(self.x.sets(), [['xprop', '-root', '-f', SCALER, '32c', '-set', SCALER, '2']])

    def test_a_stale_mode_request_is_replaced_rather_than_trusted(self):
        self.x.reset(screen=[1920, 2160], props={MODE: [0, 1, 1, 1]})
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.assertEqual(self.x.sets()[0][-1], '0, 1920, 2160, 1')
        self.assertNotIn(MODE, self.x.read()['props'])


class FailureTests(FollowerCase):
    def test_a_request_gamescope_never_consumes_is_reported_failed_then_a_later_revision_applies(self):
        self.x.reset(gamescope='ignore')
        self.control.start()
        self.start_follower('--poll-interval', '0.05', '--apply-timeout', '0.5')
        report = self.wait_for_report(1)
        self.assertEqual(report['result'], 'failed')
        self.assertEqual((report['game_width'], report['game_height']), (1920, 2160))
        self.assertIn('timed out', report['detail'])
        self.assertIn('pending', report['detail'])
        self.assertLessEqual(len(report['detail']), 160)
        self.assertNotIn(SCALER, self.x.read()['props'])      # nothing after the failed step
        self.assertEqual(len(self.x.sets()), 1)
        # It keeps polling, does not retry the same revision, and recovers on the next one.
        self.wait_for_gets(6)
        self.assertTrue(self.alive())
        self.assertEqual(len(self.control.snapshot()[1]), 1)
        self.assertEqual(len(self.x.sets()), 1)
        self.x.update(gamescope='apply')
        self.control.set_live(display_revision=2)
        report = self.wait_for_report(2)
        self.assertEqual(report['result'], 'applied')
        self.assertEqual(self.x.read()['screen'], [1920, 2160])
        self.assertTrue(self.alive())

    def test_a_failing_tool_is_reported_failed_with_its_message(self):
        self.control.start()
        self.bin.joinpath('xrandr').unlink()   # no xrandr on PATH
        self.start_follower()
        report = self.wait_for_report(1)
        self.assertEqual(report['result'], 'failed')
        self.assertIn('xrandr', report['detail'])
        self.assertTrue(self.alive())

    def test_an_unusable_contract_is_ignored_without_touching_x_or_reporting(self):
        self.control.start()
        for fields in ({'scale_mode': 'fill'}, {'game_width': 0}, {'game_height': 16385},
                       {'display_revision': True}, {'display_revision': -1}, {'game_width': '1920'}):
            with self.subTest(fields=fields):
                self.control.set_live(**{'scale_mode': 'fit', 'game_width': 1920, 'game_height': 2160,
                                         'display_revision': 1, **fields})
                self.start_follower()
                self.wait_for_gets(3)
                self.assertTrue(self.alive())
                self.stop_follower()
                self.assertEqual(self.x.sets(), [])
                self.assertEqual(self.control.snapshot()[1], [])
                self.assertTrue(any('control:' in line for line in self.log_lines()), self.log_lines())


class DeferTests(FollowerCase):
    """A game that runs keeps the monitor it started with: a change of the monitor waits, Gamescope scales the
    picture meanwhile, and the latest revision is applied when the game has ended."""

    def start_with_revision_one(self):
        """Revision 1 (1920x2160, fit) is applied while no game runs; the calls so far are forgotten."""
        self.x.reset(screen=[1920, 2160])
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.x.update(calls=[])

    def defer_revision_two(self):
        """A game starts, then revision 2 asks for 1280x720 and the stretch scaler; returns what is reported."""
        self.start_with_revision_one()
        self.x.games(GAME_APP)
        self.control.set_live(display_revision=2, game_width=1280, game_height=720, scale_mode='stretch')
        return self.wait_for_report(2)

    def writes(self, name):
        return [argv[-1] for argv in self.x.sets() if argv[-2] == name]

    def deferrals(self):
        return [line for line in self.log_lines() if ': deferred ' in line]

    def test_the_monitor_is_not_changed_while_a_game_runs_and_the_report_says_deferred(self):
        report = self.defer_revision_two()
        self.assertEqual(report, {'instance': INSTANCE, 'display': 'report', 'revision': 2, 'game_width': 1920,
                                  'game_height': 2160, 'result': 'deferred',
                                  'detail': 'game 892970 running; monitor stays 1920x2160 (target 1280x720)'})
        self.assertEqual(list(report), ['instance', 'display', 'revision', 'game_width', 'game_height',
                                        'result', 'detail'])
        # The game keeps its monitor: no request to Gamescope to resize the screen, and the screen is as it was.
        self.assertEqual(self.writes(MODE), [])
        self.assertNotIn(MODE, self.x.read()['props'])
        self.assertEqual(self.x.read()['screen'], [1920, 2160])
        self.assertEqual(self.results(), [(1, 'applied'), (2, 'deferred')])
        self.assertEqual([line.split('dosplex-display-follower: ', 1)[1] for line in self.deferrals()],
                         ['revision 2: deferred 1280x720 stretch: game 892970 running, monitor stays 1920x2160'])

    def test_the_scaler_is_still_set_while_the_monitor_is_deferred(self):
        self.defer_revision_two()
        self.assertEqual(self.x.read()['props'][SCALER], [4])
        self.assertEqual(self.writes(SCALER), ['4'])
        self.assertEqual([argv[-2] for argv in self.x.sets()], [SCALER])

    def test_the_deferral_is_reported_once_and_not_at_every_poll(self):
        self.defer_revision_two()
        screen_reads = self.x.screen_reads()
        self.wait_for_gets(6)
        self.assertEqual(self.results(2), [(2, 'deferred')])
        self.assertEqual(len(self.deferrals()), 1, self.log_lines())
        # Nor is anything written again, and the screen is not looked at at every poll while nothing changes.
        self.assertEqual(self.writes(SCALER), ['4'])
        self.assertEqual(self.writes(MODE), [])
        self.assertEqual(self.x.screen_reads(), screen_reads)
        self.assertTrue(self.alive())

    def test_the_same_revision_is_applied_and_reported_applied_when_the_game_has_ended(self):
        self.defer_revision_two()
        self.x.games()
        report = self.wait_for_result(2, 'applied')
        self.assertEqual(report, {'instance': INSTANCE, 'display': 'report', 'revision': 2, 'game_width': 1280,
                                  'game_height': 720, 'result': 'applied', 'detail': '1280x720 stretch'})
        self.assertEqual(self.results(2), [(2, 'deferred'), (2, 'applied')])
        self.assertEqual(self.x.read()['screen'], [1280, 720])
        self.assertEqual(self.writes(MODE), ['0, 1280, 720, 1'])
        self.assertEqual(self.writes(SCALER), ['4', '4'])      # for the deferral, and with the apply
        self.assertEqual(len([line for line in self.log_lines() if 'revision 2: applied 1280x720 stretch' in line]), 1,
                         self.log_lines())
        # It is settled: nothing is applied or reported again.
        self.wait_for_gets(6)
        self.assertEqual(self.results(2), [(2, 'deferred'), (2, 'applied')])
        self.assertEqual(self.writes(MODE), ['0, 1280, 720, 1'])

    def test_a_target_that_is_the_size_of_the_screen_is_applied_even_while_a_game_runs(self):
        self.start_with_revision_one()
        self.x.games(GAME_APP)
        self.control.set_live(display_revision=2, scale_mode='stretch')    # still 1920x2160: only the scaler changes
        report = self.wait_for_report(2)
        self.assertEqual((report['result'], report['game_width'], report['game_height'], report['detail']),
                         ('applied', 1920, 2160, '1920x2160 stretch'))
        self.assertEqual(self.writes(MODE), [])
        self.assertEqual(self.writes(SCALER), ['4'])
        self.assertEqual(self.results(2), [(2, 'applied')])
        self.assertEqual(self.deferrals(), [])

    def test_a_newer_revision_is_reported_in_its_turn_and_the_latest_is_the_one_applied_after_the_game(self):
        self.defer_revision_two()
        self.control.set_live(display_revision=3, game_width=960, game_height=1080, scale_mode='fit')
        report = self.wait_for_result(3, 'deferred')
        self.assertEqual(report['detail'], 'game 892970 running; monitor stays 1920x2160 (target 960x1080)')
        self.assertEqual(self.x.read()['props'][SCALER], [2])
        self.assertEqual(self.writes(MODE), [])
        self.x.games()
        self.wait_for_result(3, 'applied')
        self.assertEqual(self.x.read()['screen'], [960, 1080])
        self.assertEqual(self.writes(MODE), ['0, 960, 1080, 1'])           # the size of revision 2 was never asked for
        self.assertEqual(self.results(), [(1, 'applied'), (2, 'deferred'), (3, 'deferred'), (3, 'applied')])

    def test_a_change_in_the_games_that_run_is_reported_again_for_the_same_revision(self):
        self.defer_revision_two()
        self.x.games(GAME_APP, OTHER_APP)
        self.assertTrue(wait_for(lambda: len(self.results(2)) >= 2), self.results())
        self.x.games(OTHER_APP)                                            # the first game ends, the other goes on
        self.assertTrue(wait_for(lambda: len(self.results(2)) >= 3), self.results())
        self.wait_for_gets(6)
        self.assertEqual(self.results(2), [(2, 'deferred')] * 3)
        target = 'monitor stays 1920x2160 (target 1280x720)'
        self.assertEqual([r['detail'] for r in self.control.snapshot()[1] if r['revision'] == 2],
                         ['game 892970 running; ' + target, 'games 262830, 892970 running; ' + target,
                          'game 262830 running; ' + target])
        self.assertEqual(self.writes(MODE), [])
        self.assertEqual(self.x.read()['screen'], [1920, 2160])

    def test_steams_own_interface_is_no_game(self):
        self.start_with_revision_one()
        self.x.games()                          # the list holds 769 and nothing else
        self.control.set_live(display_revision=2, game_width=1280, game_height=720)
        self.assertEqual(self.wait_for_report(2)['result'], 'applied')
        self.assertEqual(self.x.read()['screen'], [1280, 720])
        self.assertEqual(self.deferrals(), [])

    def test_a_revision_waits_while_the_games_cannot_be_read_and_is_applied_when_they_can(self):
        self.start_with_revision_one()
        self.x.update(fail_props=[FOCUSABLE])
        self.control.set_live(display_revision=2, game_width=1280, game_height=720)
        self.assertTrue(wait_for(lambda: any('games:' in line for line in self.log_lines())), self.log_lines())
        self.wait_for_gets(6)
        # Nothing is known about the games, so the screen is not touched and nothing is reported.
        self.assertEqual(self.results(2), [])
        self.assertEqual(self.writes(MODE), [])
        self.assertEqual(self.x.read()['screen'], [1920, 2160])
        self.assertTrue(self.alive())
        self.assertEqual(len([line for line in self.log_lines() if 'games:' in line]), 1, self.log_lines())
        self.x.update(fail_props=[])
        self.assertEqual(self.wait_for_report(2)['result'], 'applied')
        self.assertTrue(any(line.endswith('games: recovered') for line in self.log_lines()), self.log_lines())

    def test_the_checks_that_the_display_is_still_there_go_on_while_a_revision_is_deferred(self):
        self.defer_revision_two()
        probes = self.x.reads(MODE)
        self.assertTrue(wait_for(lambda: self.x.reads(MODE) >= probes + 3), self.log_lines())
        self.x.update(gone=True)
        self.assertEqual(self.process.wait(timeout=10), 0, self.log_lines())
        self.assertTrue(any('is gone' in line for line in self.log_lines()), self.log_lines())


class HoldTests(FollowerCase):
    """Steam puts a screen that is too large back; the follower looks at the screen after the change and falls back."""
    BUDGET = 2304000

    def mode_writes(self):
        return [argv[-1] for argv in self.x.sets() if argv[-2] == MODE]

    def start_capped(self, budget=BUDGET, **live):
        self.x.reset(budget=budget)
        self.control.start()
        self.control.set_live(**live)
        self.start_follower()

    def settle_polls(self):
        """Polls that go by after the last report: the follower may not write the mode again."""
        before = len(self.mode_writes())
        self.wait_for_gets(8)
        self.assertEqual(len(self.mode_writes()), before)

    def test_a_size_above_the_budget_is_reported_as_the_capped_size_that_held(self):
        self.start_capped(display_revision=8)
        report = self.wait_for_report(8)
        self.assertEqual((report['result'], report['game_width'], report['game_height']), ('applied', 1430, 1608))
        self.assertEqual(report['detail'], 'Steam keeps this screen within 1920x1200 pixels: '
                                           '1430x1608 is scaled to fill 1920x2160')
        self.assertLessEqual(len(report['detail']), 200)
        self.assertEqual(self.x.read()['screen'], [1430, 1608])
        self.assertEqual(self.mode_writes(), ['0, 1920, 2160, 1', '0, 1430, 1608, 1'])
        self.assertEqual(self.x.read()['props'][SCALER], [2])
        self.assertEqual(len(self.x.filter_sets()), 1)
        lines = [line.split('dosplex-display-follower: ', 1)[1] for line in self.log_lines() if 'revision 8' in line]
        self.assertEqual(lines[0], 'revision 8: Steam set 1920x2160 back to 1920x1080; trying 1430x1608')
        self.assertRegex(lines[1], r'^revision 8: applied 1430x1608 fit \(scaled to 1920x2160\) in \d+\.\d\ds$')
        self.settle_polls()
        self.assertEqual(self.results(8), [(8, 'applied')])
        self.assertEqual(self.x.read()['screen'], [1430, 1608])

    def test_the_hold_time_is_waited_for_before_a_size_within_the_budget_is_reported(self):
        for budget in (self.BUDGET, None):
            with self.subTest(budget=budget):
                self.x.reset(budget=budget)
                self.control, self.process = FakeControl(self.root / ('control-%s.sock' % budget)), None
                self.addCleanup(self.control.stop)
                self.control.start()
                self.control.set_live(game_width=1600, game_height=900, display_revision=3)
                self.start_follower()
                report = self.wait_for_report(3)
                self.assertEqual((report['result'], report['game_width'], report['game_height'], report['detail']),
                                 ('applied', 1600, 900, '1600x900 fit'))
                state = self.x.read()
                reached = state['applied_at']
                reported = next(call['t'] for call in state['calls'] if call['argv'][-2:] == [SCALER, '2'])
                self.assertGreaterEqual(reported - reached, follower.HOLD_SECONDS - 0.1)
                self.assertEqual(self.mode_writes(), ['0, 1600, 900, 1'])
                self.stop_follower()

    def test_a_screen_that_already_has_the_size_is_not_looked_at_for_the_hold_time(self):
        self.x.reset(screen=[1600, 900], budget=self.BUDGET)
        self.control.start()
        self.control.set_live(game_width=1600, game_height=900)
        began = time.monotonic()
        self.start_follower()
        self.wait_for_report(1)
        self.assertLess(time.monotonic() - began, follower.HOLD_SECONDS)
        self.assertEqual(self.mode_writes(), [])

    def test_nothing_that_holds_is_reported_failed_with_the_screen_and_is_not_applied_again(self):
        self.start_capped(budget=1000, display_revision=5)
        report = self.wait_for_report(5)
        self.assertEqual((report['result'], report['game_width'], report['game_height']), ('failed', 1920, 1080))
        self.assertEqual(report['detail'], 'Steam set the screen back to 1920x1080')
        self.assertEqual(self.mode_writes(), ['0, 1920, 2160, 1', '0, 1430, 1608, 1', '0, 1318, 1482, 1',
                                              '0, 1216, 1368, 1', '0, 1120, 1260, 1'])
        self.assertNotIn(SCALER, self.x.read()['props'])
        self.settle_polls()
        self.assertEqual(self.results(5), [(5, 'failed')])
        self.assertTrue(self.alive())

    def test_a_later_request_above_the_learned_budget_goes_straight_to_its_capped_size(self):
        self.start_capped(display_revision=1)
        self.wait_for_report(1)
        self.x.update(calls=[])
        self.control.set_live(display_revision=2, game_width=1920, game_height=1920)
        report = self.wait_for_report(2)
        self.assertEqual((report['result'], report['game_width'], report['game_height']), ('applied', 1516, 1516))
        self.assertEqual(self.mode_writes(), ['0, 1516, 1516, 1'])      # 1920x1920 itself was never asked for
        self.assertIn('1516x1516 is scaled to fill 1920x1920', report['detail'])
        self.settle_polls()
        # A request within the budget is applied as it is.
        self.control.set_live(display_revision=3, game_width=1600, game_height=900)
        report = self.wait_for_report(3)
        self.assertEqual((report['result'], report['game_width'], report['game_height'], report['detail']),
                         ('applied', 1600, 900, '1600x900 fit'))
        self.assertEqual(self.mode_writes(), ['0, 1516, 1516, 1', '0, 1600, 900, 1'])
        self.assertEqual(self.x.read()['screen'], [1600, 900])

    def test_the_same_request_again_under_a_new_revision_keeps_the_screen_and_writes_nothing(self):
        self.start_capped(display_revision=1)
        self.wait_for_report(1)
        before = len(self.mode_writes())
        self.control.set_live(display_revision=2, scale_mode='stretch')
        report = self.wait_for_report(2)
        self.assertEqual((report['result'], report['game_width'], report['game_height']), ('applied', 1430, 1608))
        self.assertEqual(len(self.mode_writes()), before)
        self.assertEqual(self.x.read()['props'][SCALER], [4])

    def test_a_deferred_revision_that_is_applied_later_gets_the_hold_and_the_fallback(self):
        self.x.reset(screen=[1600, 900], budget=self.BUDGET)
        self.control.start()
        self.control.set_live(game_width=1600, game_height=900)
        self.start_follower()
        self.wait_for_report(1)
        self.x.games(GAME_APP)
        self.control.set_live(display_revision=2, game_width=1920, game_height=2160)
        self.assertEqual(self.wait_for_report(2)['result'], 'deferred')
        self.x.games()
        report = self.wait_for_result(2, 'applied')
        self.assertEqual((report['game_width'], report['game_height']), (1430, 1608))
        self.assertEqual(self.mode_writes(), ['0, 1920, 2160, 1', '0, 1430, 1608, 1'])


class SupervisorChainTests(FollowerCase):
    """The real follower against the supervisor's own SideControl, served from this process, instead of the fake.

    What the follower reports for a deferral has to pass the supervisor's validation, land in session.json as the
    fixed fields, and be replaced by the report of the same revision when it has been applied."""
    DISPLAY_KEYS = ('display_applied_revision', 'display_applied_size', 'display_result', 'display_detail')

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location('dosplex_side_supervisor', SUPERVISOR)
        self.supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)
        self.directory = self.root / 'left'
        self.directory.mkdir()
        self.side = self.supervisor.SideControl(self.directory, os.getuid(), INSTANCE)
        self.addCleanup(self.side.close)
        self.live = {'x': 0, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 1920, 'game_height': 2160,
                     'scale_mode': 'fit', 'display_revision': 1, 'layout': 'vertical'}
        self.side.live = dict(self.live)
        self.side.display_applied = self.supervisor.launch_display_fields(self.live)
        self.supervisor.atomic_json(self.directory / 'session.json', {
            'side': 'left', 'owner': 'tester', 'pid': 1, 'instance': INSTANCE, 'active': True,
            'lifecycle': 'running', 'mode': 'gaming', **self.side.display_applied})
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.stopping.set)

    def serve(self):
        while not self.stopping.is_set():
            self.side.poll()
            time.sleep(0.005)

    def stored(self):
        snapshot = json.loads((self.directory / 'session.json').read_text())
        return {key: snapshot[key] for key in self.DISPLAY_KEYS}

    def wait_for_polls(self, more):
        """The follower reads the games at every poll; this waits for that many more of them."""
        target = self.x.reads(FOCUSABLE) + more
        self.assertTrue(wait_for(lambda: self.x.reads(FOCUSABLE) >= target), 'the follower stopped polling')

    def test_a_deferred_revision_is_kept_in_session_json_and_replaced_by_its_applied_report(self):
        self.x.reset(screen=[1920, 2160])
        self.start_follower(environment={'DOSPLEX_SESSION_CONTROL': str(self.side.path)})
        # Revision 1 was applied when the side was launched; the follower reports it in its own words.
        self.assertTrue(wait_for(lambda: self.stored()['display_detail'] == '1920x2160 fit'), self.log_lines())
        # A game runs, and a layout change asks for another size: the monitor stays and the picture is scaled.
        self.x.games(GAME_APP)
        self.side.live = dict(self.live, display_revision=2, game_width=1280, game_height=720, scale_mode='stretch')
        deferred = {'display_applied_revision': 2, 'display_applied_size': [1920, 2160],
                    'display_result': 'deferred',
                    'display_detail': 'game 892970 running; monitor stays 1920x2160 (target 1280x720)'}
        self.assertTrue(wait_for(lambda: self.stored() == deferred), (self.stored(), self.log_lines()))
        self.assertEqual(self.side.display_applied, deferred)
        self.assertEqual(self.x.read()['screen'], [1920, 2160])
        self.wait_for_polls(6)
        self.assertEqual(self.stored(), deferred)
        self.assertFalse([line for line in self.log_lines() if 'rejected' in line], self.log_lines())
        # The game ends: the same revision is applied, and its report replaces the deferred one.
        self.x.games()
        applied = {'display_applied_revision': 2, 'display_applied_size': [1280, 720],
                   'display_result': 'applied', 'display_detail': '1280x720 stretch'}
        self.assertTrue(wait_for(lambda: self.stored() == applied), (self.stored(), self.log_lines()))
        self.assertEqual(self.side.display_applied, applied)
        self.assertEqual(self.x.read()['screen'], [1280, 720])
        self.assertFalse([line for line in self.log_lines() if 'rejected' in line], self.log_lines())


class WakeTests(FollowerCase):
    """A game that starts into a side whose Steam is idle stays behind Steam's window; one key press wakes Steam.

    These run on the real clock, so the three seconds are waited for. WakeStepTests has the other cases of it."""

    def test_steam_is_woken_once_for_a_game_that_stays_behind_it_and_not_for_one_that_took_the_focus(self):
        self.x.reset(screen=[1920, 2160], props={FOCUSABLE: [STEAM_UI], FOCUSED_APP: [STEAM_UI]})
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        # A game starts and is presented at once, as on a side that is awake: Steam is not woken for it.
        self.x.focused_app(OTHER_APP)
        self.x.games(OTHER_APP)
        self.wait_for_gets(3)
        # The player goes back to Steam's interface, and a second game starts behind it, as on an idle side.
        self.x.focused_app(STEAM_UI)
        before = time.time()
        self.x.games(OTHER_APP, GAME_APP)
        keys = wait_for(self.x.keys, timeout=15)
        self.assertTrue(keys, self.log_lines())
        pressed, argv = keys[0]
        self.assertEqual(argv, ['xdotool', 'key', 'Escape'])
        self.assertGreaterEqual(pressed - before, 3.0)      # not before the three seconds are over
        self.assertTrue(wait_for(lambda: any(line.endswith('woke Steam for app 892970') for line in self.log_lines())),
                        self.log_lines())
        # One key press, for the game that stayed behind Steam, and no other while it stays in the list.
        self.wait_for_gets(8)
        self.assertEqual(len(self.x.keys()), 1)
        self.assertEqual([line for line in self.log_lines() if 'woke' in line],
                         ['dosplex-display-follower: woke Steam for app 892970'])
        # The key press changed no display setting.
        self.assertEqual(self.results(), [(1, 'applied')])
        self.assertTrue(self.alive())


class ControlChannelTests(FollowerCase):
    def test_survives_a_missing_socket_and_logs_it_once(self):
        self.start_follower()      # the control server is not there yet
        self.assertTrue(wait_for(lambda: any('control:' in line for line in self.log_lines())))
        time.sleep(0.4)            # many polls
        self.assertTrue(self.alive())
        self.assertEqual(len([line for line in self.log_lines() if 'control:' in line]), 1, self.log_lines())
        self.control.start()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.assertTrue(any('recovered' in line for line in self.log_lines()))

    def test_survives_dropped_refused_and_garbled_replies(self):
        self.control.start()
        for mode in ('drop', 'refuse', 'garbage'):
            with self.subTest(mode=mode):
                self.control.modes['get'] = mode
                gets = self.control.snapshot()[2]
                self.start_follower()
                self.assertTrue(wait_for(lambda: self.control.snapshot()[2] >= gets + 5))
                self.assertTrue(self.alive())
                self.stop_follower()
        self.assertEqual(self.x.sets(), [])
        self.control.modes['get'] = 'ok'
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')

    def test_a_get_that_times_out_is_asked_again_within_the_retry_interval_not_at_the_next_poll(self):
        # The supervisor is busy and leaves the first get without a reply. The next normal poll is 30 s away.
        self.control.start()
        self.control.stall_gets = 1
        self.start_follower('--poll-interval', '30', '--control-timeout', '0.3', '--retry-interval', '0.2',
                            '--apply-timeout', '5')
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')    # wait_for gives up after 10 s
        first, second = self.control.times()
        self.assertLess(second - first, 3)
        self.assertEqual([line.split('control: ', 1)[1] for line in self.log_lines() if 'control:' in line],
                         ['TimeoutError: timed out', 'recovered'])

    def test_a_supervisor_that_stays_silent_is_asked_quickly_a_few_times_and_then_at_the_normal_poll(self):
        self.control.start()
        self.control.modes['get'] = 'stall'
        self.start_follower('--poll-interval', '1', '--control-timeout', '0.2', '--retry-interval', '0.25',
                            '--apply-timeout', '5')
        self.assertTrue(wait_for(lambda: len(self.control.times()) >= 6, timeout=20), self.log_lines())
        times = self.control.times()
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        # The first try and three quick retries follow each other; from the fifth the poll interval applies again.
        self.assertTrue(all(gap >= 0.8 for gap in gaps[3:]), gaps)
        self.assertEqual(self.x.sets(), [])
        # One log line for the whole stretch, not one per try.
        self.assertEqual([line for line in self.log_lines() if 'control:' in line],
                         ['dosplex-display-follower: control: TimeoutError: timed out'])
        # The supervisor answers again: the follower says so once and applies the revision.
        self.control.modes['get'] = 'ok'
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.assertEqual(len([line for line in self.log_lines() if 'recovered' in line]), 1, self.log_lines())

    def test_a_server_with_another_uid_never_sees_a_request(self):
        self.control.start()
        self.start_follower(server_uid=os.getuid() + 1)
        self.assertTrue(wait_for(lambda: any('not trusted' in line for line in self.log_lines())), self.log_lines())
        time.sleep(0.5)
        self.assertTrue(self.alive())
        self.assertEqual(self.control.snapshot()[0], [])
        self.assertEqual(self.x.sets(), [])

    @unittest.skipIf(os.getuid() == 0, 'the default trusted server uid is root')
    def test_the_trusted_server_uid_defaults_to_root(self):
        self.control.start()
        self.start_follower(server_uid=None)
        self.assertTrue(wait_for(lambda: any('not trusted' in line for line in self.log_lines())), self.log_lines())
        time.sleep(0.3)
        self.assertEqual(self.control.snapshot()[0], [])

    def test_an_unacknowledged_report_is_sent_again_until_the_server_answers(self):
        self.control.start()
        self.control.drop_reports = 2
        self.start_follower()
        reports = wait_for(lambda: self.control.snapshot()[1] if len(self.control.snapshot()[1]) >= 3 else None)
        self.assertTrue(reports)
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[1], reports[2])
        self.wait_for_gets(8)
        self.assertEqual(len(self.control.snapshot()[1]), 3)       # acknowledged: no more copies
        self.assertEqual(len(self.x.sets()), 2)                    # and the apply itself ran once

    def test_a_rejected_report_is_not_repeated(self):
        self.control.start()
        self.control.modes['report'] = 'refuse'
        self.start_follower()
        self.wait_for_report(1)
        self.wait_for_gets(8)
        self.assertEqual(len(self.control.snapshot()[1]), 1)
        self.assertTrue(any('rejected' in line for line in self.log_lines()), self.log_lines())


class LifetimeTests(FollowerCase):
    def test_ends_cleanly_on_sigterm(self):
        self.control.start()
        self.start_follower()
        self.wait_for_gets(2)
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=5), 0)

    def test_ends_when_the_display_stays_unreachable_but_not_for_a_short_glitch(self):
        self.control.start()
        self.start_follower()
        self.wait_for_report(1)
        self.x.update(fail_next=2)       # two failed tool calls, then the display answers again
        self.assertTrue(wait_for(lambda: self.x.read()['fail_next'] == 0))
        self.wait_for_gets(10)      # more than one further display check
        self.assertTrue(self.alive(), self.log_lines())
        self.x.update(gone=True)
        self.assertEqual(self.process.wait(timeout=10), 0, self.log_lines())
        self.assertTrue(any('is gone' in line for line in self.log_lines()), self.log_lines())

    def test_missing_or_invalid_configuration_is_a_usage_error(self):
        cases = {
            'no display': {'DISPLAY': ''},
            'no control socket': {'DOSPLEX_SESSION_CONTROL': ''},
            'relative control socket': {'DOSPLEX_SESSION_CONTROL': 'control.sock'},
            'short instance': {'DOSPLEX_SESSION_INSTANCE': 'abc'},
            'uid not a number': {'DOSPLEX_SESSION_CONTROL_SERVER_UID': 'root'},
        }
        self.control.start()
        for name, environment in cases.items():
            with self.subTest(name):
                process = self.start_follower(environment=environment)
                self.assertEqual(process.wait(timeout=5), 2)
                self.assertTrue(self.log_lines())
        self.assertEqual(self.control.snapshot()[0], [])

    def test_the_control_timing_options_are_checked(self):
        for option, value in (('--control-timeout', '0'), ('--control-timeout', '61'), ('--control-timeout', 'long'),
                              ('--retry-interval', '0'), ('--retry-interval', '61'), ('--retry-interval', 'soon')):
            with self.subTest(option=option, value=value):
                self.assertEqual(self.start_follower(option, value).wait(timeout=5), 2)
                self.assertTrue(any('error:' in line and option in line for line in self.log_lines()),
                                self.log_lines())

    def test_it_touches_nothing_but_the_x_tools_and_the_control_socket(self):
        # Only the fake xprop and xrandr are on PATH, so any other command would fail and be logged.
        self.control.start()
        self.start_follower()
        self.wait_for_report(1)
        self.wait_for_gets(5)
        self.assertEqual(self.x.tools(), {'xprop', 'xrandr'})
        source = FOLLOWER.read_text()
        for forbidden in ('os.kill', 'killpg', '.kill(', '.terminate(', 'pkill', 'systemctl', 'os.system',
                          'raise_signal', 'os.fork', 'os.exec', 'shell=True'):
            self.assertNotIn(forbidden, source)


class FocusRefreshTests(FollowerCase):
    """The focus refresh end to end: the real follower and the fake X, on the real clock (the settle is 0.75 s)."""

    def start_idle(self):
        self.x.reset(screen=[1920, 2160], props={FOCUSABLE: [STEAM_UI], FOCUSED_APP: [STEAM_UI]})
        self.x.window(STEAM, app=STEAM_UI, hints=None, size=(1920, 2160))
        self.x.window(GAME, app=GAME_APP)
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')

    def test_steams_window_gets_one_focus_cycle_and_a_game_none(self):
        self.start_idle()
        self.x.focus(STEAM)
        self.assertTrue(wait_for(lambda: len(self.x.focus_cycles()) == 2), self.log_lines())
        self.wait_for_gets(10)
        self.assertEqual(self.x.focus_cycles(), [1023, STEAM])      # the root, then the window; no repeat
        self.assertEqual(self.x.keys(), [])
        # A game takes the focus: no cycle, however long it stays.
        self.x.focused_app(GAME_APP)
        self.x.focus(GAME)
        self.wait_for_gets(10)
        self.assertEqual(self.x.focus_cycles(), [1023, STEAM])
        # The game ends and Steam's window is the focus again: one more cycle.
        self.x.focused_app(STEAM_UI)
        self.x.focus(STEAM)
        self.assertTrue(wait_for(lambda: len(self.x.focus_cycles()) == 4), self.log_lines())
        self.assertEqual(self.x.focus_cycles(), [1023, STEAM, 1023, STEAM])
        self.assertTrue(self.alive())

    def test_a_window_that_fails_the_cycle_is_logged_once_and_does_not_end_the_follower(self):
        self.start_idle()
        self.x.focus(0x7777777)                                      # a window the X server does not hold
        self.assertTrue(wait_for(lambda: any('focus refresh:' in line for line in self.log_lines())))
        self.wait_for_gets(6)
        self.assertEqual(len([line for line in self.log_lines() if 'focus refresh:' in line]), 1)
        self.assertTrue(self.alive())


class FitTests(FollowerCase):
    """The window fit end to end: the real follower, the fake control server and a fake X that holds windows.

    Revision 1 of the fake control server makes the screen 1920x2160 at the first poll, before the first fit. The fake
    X does what Gamescope's focus pass does: it gives the focus window with an app id the screen size while the forced
    fullscreen is on, and Steam's own window always. GAMESCOPE_FOCUSED_APP_GFX is not set on its root unless a test
    sets it, so the window's own STEAM_GAME names the app.
    """
    FIXED_HINTS = hints_text((640, 480), (640, 480))

    def wait_for_writes(self, expected):
        self.assertTrue(wait_for(lambda: self.x.force_writes() == expected),
                        'writes %s, expected %s; follower log: %s' % (self.x.force_writes(), expected, self.log_lines()))

    def fit_errors(self):
        return [line for line in self.log_lines() if line.startswith('dosplex-display-follower: fit:')]

    def fit_lines(self):
        return [line for line in self.log_lines() if 'window fit' in line]

    def wait_for_fit_line(self, text):
        self.assertTrue(wait_for(lambda: any(text in line for line in self.fit_lines())),
                        'no "%s"; follower log: %s' % (text, self.log_lines()))

    def test_a_game_window_smaller_than_the_screen_is_fitted_with_a_one_shot_after_the_display_is_applied(self):
        self.x.reset(windows={str(GAME): held(size=(1280, 720))}, props={FOCUS: [GAME]})   # focused, no sizes in its hints
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.wait_for_writes(['1', '0'])
        self.wait_for_gets(8)
        self.assertEqual(self.x.force_writes(), ['1', '0'])   # on once, off again: not at every poll
        self.assertEqual(self.x.forced(), [0])
        self.assertEqual(self.x.size(GAME), [1920, 2160])
        # The mode and the scaler of the revision first, then the one-shot.
        self.assertEqual([argv[-2] for argv in self.x.sets()], [MODE, SCALER, FORCE, FORCE])
        self.assertEqual(len(self.fit_lines()), 2, self.log_lines())
        self.assertIn('%s 0 -> 1 (window 0x600003, app 892970, no size hints, 1280x720, screen 1920x2160)' % FORCE,
                      self.fit_lines()[0])
        self.assertRegex(self.fit_lines()[1], r'%s 1 -> 0 \(window 0x600003 has the screen size 1920x2160 after \d+ ms\)$'
                         % FORCE)
        self.assertEqual(self.x.reads(FOCUSED_APP), 0)        # the input focus app is not what Gamescope resizes

    def test_steams_window_is_left_alone_and_a_game_window_is_decided_once_its_app_is_known(self):
        self.x.reset(windows={str(STEAM): held(hints=None, app=STEAM_UI, size=(1280, 720)),
                              str(GAME): held(hints=None, app=None, size=(1920, 1080))}, props={FOCUS: [STEAM]})
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.wait_for_fit_line("left Steam's own window 0x2000035 alone (Gamescope sizes it itself)")
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(self.x.size(STEAM), [1920, 2160])  # Gamescope's focus pass sized it, not the property
        self.assertEqual(self.x.window_reads(STEAM), 1)     # decided once, not read again
        # The game window takes the focus before anything names its app: the follower waits and writes nothing.
        self.x.focus(GAME)
        self.wait_for_fit_line('window 0x600003: waiting for its app id')
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(len(self.fit_lines()), 2, self.log_lines())   # the wait is logged once
        # Its app id appears; it has no WM_NORMAL_HINTS at all, so it is resizable.
        self.x.window(GAME, hints=None, app=GAME_APP, size=(1920, 1080))
        self.wait_for_writes(['1', '0'])
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), ['1', '0'])
        self.assertEqual(self.x.size(GAME), [1920, 2160])
        self.assertIn('%s 0 -> 1 (window 0x600003, app 892970, no WM_NORMAL_HINTS, 1920x1080, screen 1920x2160)' % FORCE,
                      self.fit_lines()[-2])
        # Steam's window takes the focus again: the property is already off, so nothing is written.
        self.x.focus(STEAM)
        self.assertTrue(wait_for(lambda: len([line for line in self.fit_lines() if "left Steam's own window" in line]) == 2),
                        self.log_lines())
        self.wait_for_gets(4)
        self.assertEqual(self.x.force_writes(), ['1', '0'])
        self.assertEqual(self.x.forced(), [0])
        self.assertTrue(self.alive())

    def test_gamescopes_focus_app_names_the_app_and_steam_game_is_read_only_without_it(self):
        self.x.reset(windows={str(GAME): held(app=None, size=(1280, 720))}, props={FOCUS: [GAME], FOCUS_APP: [GAME_APP]})
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['1', '0'])                    # the window has no STEAM_GAME; the root names its app
        self.assertIn('window 0x600003, app 892970, no size hints', self.fit_lines()[0])

    def test_a_game_window_behind_a_steam_overlay_with_the_input_is_fitted_as_the_game_it_is(self):
        # GAMESCOPE_FOCUSED_APP is the input focus window's app: Steam's while its overlay has the input. The window
        # Gamescope resizes is the focus window, whose app is GAMESCOPE_FOCUSED_APP_GFX.
        self.x.reset(windows={str(GAME): held(size=(1280, 720))},
                     props={FOCUS: [GAME], FOCUS_APP: [GAME_APP], FOCUSED_APP: [STEAM_UI]})
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['1', '0'])
        self.assertIn('window 0x600003, app 892970, no size hints, 1280x720, screen 1920x2160', self.fit_lines()[0])
        self.assertNotIn("Steam's own window", '\n'.join(self.fit_lines()))

    def test_a_game_window_that_states_one_size_is_left_alone(self):
        self.x.reset(windows={str(STEAM): held(hints=None, app=STEAM_UI, size=(1920, 2160)),
                              str(FIXED): held(hints=hints_text((1920, 2160), (1920, 2160)), size=(1920, 2160))},
                     props={FOCUS: [STEAM]})
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.wait_for_fit_line("left Steam's own window 0x2000035")
        self.x.focus(FIXED)
        self.wait_for_fit_line('left window 0x600005, app 892970, minimum 1920x2160, maximum 1920x2160 alone')
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(self.x.forced(), None)
        self.assertEqual(self.x.window_reads(FIXED), 1)
        self.assertEqual([call for call in self.x.calls() if call['argv'][0] == 'xdotool'], [])   # no size asked

    def test_a_game_window_that_already_has_the_screens_size_is_left_alone(self):
        self.x.reset(windows={str(RANGED): held(hints=hints_text((640, 480), (3840, 2160)), size=(1920, 2160))},
                     props={FOCUS: [RANGED]})
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.wait_for_fit_line('left window 0x600007, app 892970, minimum 640x480, maximum 3840x2160, '
                               '1920x2160, screen 1920x2160 alone')
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(self.x.forced(), None)
        self.assertEqual(self.x.window_reads(RANGED), 1)
        self.assertEqual(len(self.fit_lines()), 1, self.log_lines())

    def test_guest_full_screen_gives_the_monitor_the_whole_screen(self):
        self.x.reset(windows={str(GAME): held(size=(1280, 720))}, props={FOCUS: [GAME]})
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['1', '0'])
        self.assertEqual(self.x.size(GAME), [1920, 2160])
        self.wait_for_gets(4)
        # Main's choice published for Guest: layout "guest", its region the whole output.
        self.control.set_live(display_revision=2, x=0, y=0, width=3840, height=2160,
                              game_width=3840, game_height=2160, layout='guest')
        self.assertEqual(self.wait_for_report(2)['result'], 'applied')
        self.wait_for_writes(['1', '0', '1', '0'])
        self.assertEqual(self.x.size(GAME), [3840, 2160])

    def test_a_layout_change_fits_the_focused_window_again_with_another_one_shot(self):
        self.x.reset(windows={str(GAME): held(size=(1280, 720))}, props={FOCUS: [GAME]})
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['1', '0'])
        self.assertEqual(self.x.size(GAME), [1920, 2160])
        self.wait_for_gets(4)
        self.control.set_live(display_revision=2, game_width=3840, game_height=2160)   # Full screen
        self.assertEqual(self.wait_for_report(2)['result'], 'applied')
        self.wait_for_writes(['1', '0', '1', '0'])
        self.assertEqual(self.x.size(GAME), [3840, 2160])
        self.assertEqual(self.x.forced(), [0])
        self.wait_for_fit_line('the screen is 3840x2160 now, was 1920x2160: window 0x600003 is fitted again')
        # A revision that leaves the screen size alone (the scaler only) fits nothing again.
        self.control.set_live(display_revision=3, scale_mode='stretch')
        self.assertEqual(self.wait_for_report(3)['result'], 'applied')
        self.wait_for_gets(6)
        self.assertEqual(self.x.force_writes(), ['1', '0', '1', '0'])

    def test_a_layout_change_with_steam_in_front_never_switches_the_property_on(self):
        # The 16:9 split start and the native 4K relaunch apply a revision with Steam in front. Gamescope sizes
        # Steam's window itself, so no 1 is written that could reach the window focused next.
        self.x.reset(windows={str(STEAM): held(hints=None, app=STEAM_UI, size=(1920, 2160))}, props={FOCUS: [STEAM]})
        self.control.start()
        self.start_follower()
        self.wait_for_fit_line("left Steam's own window 0x2000035 alone")
        self.control.set_live(display_revision=2, game_width=3840, game_height=2160)
        self.assertEqual(self.wait_for_report(2)['result'], 'applied')
        self.wait_for_gets(8)
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(self.x.size(STEAM), [3840, 2160])

    def test_a_window_that_does_not_take_the_size_gets_the_property_off_after_a_quarter_of_a_second(self):
        self.x.reset(windows={str(GAME): held(size=(1280, 720), stubborn=True)}, props={FOCUS: [GAME]})
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['1', '0'])
        self.wait_for_fit_line('%s 1 -> 0 (window 0x600003 is 1280x720, not 1920x2160, after ' % FORCE)
        line = self.fit_lines()[-1]
        self.assertRegex(line, r'%s 1 -> 0 \(window 0x600003 is 1280x720, not 1920x2160, after \d+ ms: left at that size\)$'
                         % FORCE)
        self.assertGreaterEqual(int(re.search(r'after (\d+) ms', line).group(1)), 250)   # the whole time it is given
        self.wait_for_gets(8)
        self.assertEqual(self.x.force_writes(), ['1', '0'])   # not tried again for this window
        self.assertEqual(self.x.forced(), [0])

    def test_the_property_is_off_within_the_poll_even_when_every_read_fails_while_it_is_on(self):
        # The reads that tell when the property can go off all fail from the moment it is on, and the window keeps the
        # focus. The property used to stay on then, for as long as the failure lasted, and the next window to take the
        # focus was resized in Gamescope's focus pass. Now the 0 follows the 1 inside the same poll. The poll is long (8 s)
        # to show that the 0 does not wait for the next one.
        self.x.reset(windows={str(GAME): held(size=(1280, 720), stubborn=True),
                              str(FIXED): held(hints=self.FIXED_HINTS, size=(640, 480))},
                     props={FOCUS: [GAME]}, fail_reads_while_forced=True)
        self.control.start()
        self.start_follower('--poll-interval', '8', '--apply-timeout', '5')
        self.assertTrue(wait_for(lambda: self.x.force_writes() == ['1', '0'], timeout=5), self.log_lines())
        (on, _), (off, _) = self.x.force_times()
        self.assertLess(off - on, 2.0)                       # a few of the fake's tools, not a poll
        self.assertEqual(self.x.forced(), [0])
        self.wait_for_fit_line('%s 1 -> 0 (a tool failed: ' % FORCE)
        self.assertTrue(wait_for(self.fit_errors), self.log_lines())   # and the failure is logged, once
        # A fixed-size window takes the focus at once: it keeps its size.
        self.x.focus(FIXED)
        self.assertEqual(self.x.size(FIXED), [640, 480])
        self.assertEqual(self.x.force_writes(), ['1', '0'])

    def test_a_fixed_size_window_that_takes_the_focus_right_after_a_fit_is_never_resized(self):
        # The poll is long (8 s). The property used to stay on until the next poll, and a window that took the focus in
        # between was resized in Gamescope's focus pass: a fixed-size one to the screen size, 1920x2160 in this harness.
        self.x.reset(windows={str(GAME): held(size=(1280, 720)),
                              str(FIXED): held(hints=self.FIXED_HINTS, size=(640, 480))}, props={FOCUS: [GAME]})
        self.control.start()
        self.start_follower('--poll-interval', '8', '--apply-timeout', '5')
        self.assertTrue(wait_for(lambda: self.x.force_writes() == ['1', '0'], timeout=5), self.log_lines())
        self.assertEqual(self.x.size(GAME), [1920, 2160])    # fitted
        self.x.focus(FIXED)                                  # the next window takes the focus before the next poll
        self.assertEqual(self.x.size(FIXED), [640, 480])
        self.assertEqual(self.x.forced(), [0])
        self.assertEqual(self.x.force_writes(), ['1', '0'])

    def test_a_property_left_on_by_an_earlier_follower_is_switched_off_at_the_start(self):
        self.x.reset(windows={str(FIXED): held(hints=self.FIXED_HINTS, size=(640, 480))},
                     props={FOCUS: [FIXED], FORCE: [1]})        # a follower ended during a one-shot
        self.control.start()
        self.start_follower()
        self.wait_for_writes(['0'])
        self.wait_for_fit_line('%s 1 -> 0 (on when the follower started)' % FORCE)
        self.assertEqual(self.x.forced(), [0])
        self.stop_follower()
        self.assertEqual(self.x.force_writes(), ['0'])         # nothing on, so nothing to switch off at the end

    def test_a_stop_during_the_one_shot_leaves_the_property_off(self):
        # The follower is stopped while the property is on and the window is being looked at. The reads of the window are
        # slow while the property is on, so the look cannot end before the signal arrives: the 0 is written on the way out.
        self.x.reset(windows={str(GAME): held(size=(1280, 720), stubborn=True)}, props={FOCUS: [GAME]},
                     slow_reads_while_forced=5)
        self.control.start()
        self.start_follower()
        self.assertTrue(wait_for(lambda: self.x.forced() == [1]), self.log_lines())
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.x.force_writes(), ['1', '0'])
        self.assertEqual(self.x.forced(), [0])
        self.assertIn('%s 1 -> 0 (the follower is stopping)' % FORCE, self.fit_lines()[-1])

    def test_a_stop_during_the_write_of_the_1_leaves_the_property_off(self):
        # xprop has set the property and is still running when the stop comes. The follower unwinds that call without
        # having seen it answer, and the property is on all the same: the 0 is written on the way out.
        self.x.reset(windows={str(GAME): held(size=(1280, 720), stubborn=True)}, props={FOCUS: [GAME]}, linger_set=5)
        self.control.start()
        self.start_follower()
        self.assertTrue(wait_for(lambda: self.x.forced() == [1]), self.log_lines())
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.x.forced(), [0])
        self.assertEqual(self.x.force_writes(), ['1', '0'])
        self.assertIn('%s 1 -> 0 (the follower is stopping)' % FORCE, self.fit_lines()[-1])

    def test_a_failing_xprop_leaves_the_window_undecided_and_does_not_end_the_follower(self):
        self.x.reset(windows={str(FIXED): held(hints=self.FIXED_HINTS, size=(640, 480))}, props={FOCUS: [FIXED]},
                     hint_error=True)                       # xprop -id fails (BadWindow)
        self.control.start()
        self.start_follower()
        self.assertTrue(wait_for(self.fit_errors), self.log_lines())
        self.wait_for_gets(8)
        self.assertTrue(self.alive())
        self.assertEqual(self.x.force_writes(), [])
        self.assertEqual(len(self.fit_errors()), 1, self.fit_errors())   # a failure that persists is logged once
        self.assertFalse(self.fit_lines(), self.log_lines())             # not decided yet
        self.x.update(hint_error=False)
        self.wait_for_fit_line('left window 0x600005, app 892970, minimum 640x480, maximum 640x480 alone')
        self.assertTrue(wait_for(lambda: any(line.endswith('fit: recovered') for line in self.log_lines())),
                        self.log_lines())
        self.assertTrue(self.alive())

    def changes(self):
        """The fit lines that report a change of the property (the ones with an arrow)."""
        return [line for line in self.fit_lines() if ' -> ' in line]

    def test_a_write_that_fails_is_tried_again_at_the_next_poll_and_logs_no_change(self):
        # Every write of the property fails. The 1 is tried once and a 0 follows at once, since the property may be on, and
        # again at every poll after that, before anything else: the window is not switched on again while a 0 is owed.
        # No change is logged, because none took place; the one line about it says the 0 could not be written.
        self.x.reset(windows={str(GAME): held(size=(1280, 720))}, props={FOCUS: [GAME]}, fail_force_set=True)
        self.control.start()
        self.start_follower()
        self.assertEqual(self.wait_for_report(1)['result'], 'applied')
        self.assertTrue(wait_for(lambda: len(self.x.force_writes()) >= 4), self.log_lines())
        self.assertTrue(self.alive())
        self.assertIsNone(self.x.forced())                  # it never took
        self.assertEqual(self.x.force_writes()[:2], ['1', '0'])
        self.assertEqual(self.x.force_writes().count('1'), 1)
        self.assertFalse(self.changes(), self.log_lines())
        self.assertEqual(len([line for line in self.fit_lines() if 'could not be switched off' in line]), 1)
        self.x.update(fail_force_set=False)
        self.assertTrue(wait_for(lambda: self.x.forced() == [0] and len(self.changes()) == 3), self.log_lines())
        self.assertIn('%s 1 -> 0 (it may have been left on by the last call)' % FORCE, self.changes()[0])
        self.assertIn('%s 0 -> 1 (' % FORCE, self.changes()[1])
        self.assertEqual(self.x.size(GAME), [1920, 2160])


class FitStepTests(unittest.TestCase):
    """Follower.fit_windows with scripted X tools: the calls one poll makes, one poll at a time and without a process.

    The scripted tools act as Gamescope does. The focus window (GAMESCOPE_FOCUSED_WINDOW) and the input focus window
    can differ: a Steam overlay with inputFocusMode takes the input. GAMESCOPE_FOCUSED_APP_GFX names the focus window's
    app and GAMESCOPE_FOCUSED_APP the input focus window's (unless a test makes the first absent or empty). The focus
    pass, which runs when the focus moves, when the property changes and when the screen is resized, gives the focus
    window with an app id the screen size while the property is on, and Steam's own window always, under
    GAMESCOPE_STEAM_MAX_HEIGHT the screen scaled to that height.
    """

    def setUp(self):
        self.focus = None              # the window GAMESCOPE_FOCUSED_WINDOW names; None: the property is empty
        self.input_focus = None        # the input focus window when it is not the focus window (a Steam overlay)
        self.root_app = 'focused'      # GAMESCOPE_FOCUSED_APP_GFX: 'focused' (the window's app), 'absent', 'empty' or an id
        self.steam_cap = None          # GAMESCOPE_STEAM_MAX_HEIGHT, None: not set
        self.property = 0              # the forced fullscreen as Gamescope holds it
        self.screen = (1920, 2160)
        self.windows = {STEAM: held(hints=None, app=STEAM_UI, size=(1920, 2160)),
                        GAME: held(size=(1280, 720)),
                        FIXED: held(hints=hints_text((640, 480), (640, 480)), size=(640, 480))}
        self.failing = {}              # 'focus' (a root read), 'window', 'hints', 'geometry', 'screen' or 'set' -> error
        self.failing_while_on = {}     # the same kinds, but the error comes only while the property is 1
        self.on_set = None             # called with the value once the scripted Gamescope has taken a write of the property
        self.set_times = []            # (time on the clock, value) of each write that was applied
        self.tool_seconds = 0.01       # what one call of a tool costs on the clock
        self.calls = []
        self.sleeps = []               # each pause of the follower
        self.clock = FakeClock()       # the follower's time: pausing costs nothing, a tool costs tool_seconds
        pause = self.clock.sleep
        self.clock.sleep = lambda seconds: (self.sleeps.append(seconds), pause(seconds))[1]
        for patcher in (mock.patch.object(follower, 'run_tool', self.tool), mock.patch.object(follower, 'time', self.clock)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()    # the follower's log lines
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.follower = self.new_follower()

    def new_follower(self):
        return follower.Follower('/run/dosplex/control.sock', INSTANCE, 0, environment={'DISPLAY': DISPLAY})

    def focus_app(self):
        """The focus window's app as Gamescope holds it (from its process, or its STEAM_GAME)."""
        window = self.windows.get(self.focus)
        return window['app'] if window else None

    def gamescope_fit(self):
        """Gamescope's focus pass (DetermineAndApplyFocus): the focus window with an app id gets the screen size while
        the property is on, and Steam's own window always (window_is_fullscreen), capped by GAMESCOPE_STEAM_MAX_HEIGHT."""
        window = self.windows.get(self.focus)
        app = self.focus_app()
        if not window or not app or window['stubborn'] or not (self.property or app == STEAM_UI):
            return
        width, height = self.screen
        if app == STEAM_UI and self.steam_cap and height > self.steam_cap:
            width, height = int(width * (self.steam_cap / height)), self.steam_cap
        window['size'] = [width, height]

    def move_focus(self, window):
        """The focus moves; Gamescope's focus pass runs at once, before the follower polls."""
        self.focus = window
        self.gamescope_fit()

    def resize_screen(self, screen):
        """Gamescope resizes the nested screen; configure_win marks the focus dirty, so the focus pass runs."""
        self.screen = screen
        self.gamescope_fit()

    def root_text(self, names):
        lines = []
        for name in names:
            if name == FOCUS:
                lines.append('GAMESCOPE_FOCUSED_WINDOW(CARDINAL) = %s' % (self.focus or ''))
            elif name == FOCUS_APP:
                if self.root_app == 'absent':
                    lines.append(FOCUS_APP + ':  not found.')
                else:
                    app = self.focus_app() if self.root_app == 'focused' else self.root_app
                    lines.append('%s(CARDINAL) = %s' % (FOCUS_APP, '' if app in (None, 'empty') else app))
            elif name == FOCUSED_APP:          # the input focus window's app; the fit must not need it
                window = self.windows.get(self.input_focus or self.focus)
                lines.append('%s(CARDINAL) = %s' % (FOCUSED_APP, window['app'] if window and window['app'] else ''))
            elif name == FORCE:
                lines.append('%s(CARDINAL) = %d' % (FORCE, self.property))
            else:
                self.fail('unexpected root property %s' % name)
        return '\n'.join(lines) + '\n'

    def tool(self, argv, environment):
        self.calls.append(argv)
        self.clock.now += self.tool_seconds
        if argv[:2] == ['xprop', '-root'] and '-f' not in argv:
            kind = 'focus'                 # any read of the root window
        elif argv[:2] == ['xprop', '-id'] and argv[3:] == ['WM_NORMAL_HINTS']:
            kind = 'hints'
        elif argv[:2] == ['xprop', '-id']:
            kind = 'window'
        elif argv[:2] == ['xdotool', 'getwindowgeometry']:
            kind = 'geometry'
        elif argv == ['xrandr', '--current']:
            kind = 'screen'
        else:
            self.assertEqual(argv[:5], ['xprop', '-root', '-f', FORCE, '32c'])
            kind = 'set'
        if kind in self.failing:
            raise self.failing[kind]
        if kind in self.failing_while_on and self.property:
            raise self.failing_while_on[kind]
        if kind == 'focus':
            return self.root_text(argv[2:])
        if kind in ('window', 'hints'):
            self.assertIn(argv[3:], (['WM_NORMAL_HINTS', 'STEAM_GAME'], ['WM_NORMAL_HINTS']))
            window = self.windows[int(argv[2], 16)]
            text = window['hints'] or 'WM_NORMAL_HINTS:  not found.\n'
            if kind == 'window':
                text += ('STEAM_GAME(CARDINAL) = %d\n' % window['app'] if window['app'] is not None
                         else 'STEAM_GAME:  not found.\n')
            return text
        if kind == 'geometry':
            self.assertEqual(argv[2], '--shell')
            window = self.windows[int(argv[3], 16)]
            return 'WINDOW=%d\nX=0\nY=0\nWIDTH=%d\nHEIGHT=%d\nSCREEN=0\n' % (int(argv[3], 16), *window['size'])
        if kind == 'screen':
            return 'Screen 0: minimum 16 x 16, current %d x %d, maximum 32767 x 32767\n' % self.screen
        self.property = int(argv[-1])
        self.set_times.append((self.clock.now, self.property))
        self.gamescope_fit()
        if self.on_set:
            self.on_set(self.property)
        return ''

    def poll(self, times=1):
        for _ in range(times):
            self.follower.fit_windows()

    def writes(self):
        return [argv[-1] for argv in self.calls if '-set' in argv]

    def log_text(self):
        return self.stderr.getvalue()

    def fit_lines(self):
        return [line for line in self.log_text().splitlines() if 'window fit' in line]

    def read_focus(self):
        return follower.xprop_get_argv(FOCUS, FOCUS_APP)

    def read_focus_first(self):
        """The first poll reads the property as well."""
        return follower.xprop_get_argv(FOCUS, FOCUS_APP, FORCE)

    def read_window(self, window):
        return follower.xprop_window_argv(window, 'WM_NORMAL_HINTS', 'STEAM_GAME')

    def read_hints(self, window):
        return follower.xprop_window_argv(window, 'WM_NORMAL_HINTS')

    def read_size(self, window):
        return follower.xdotool_geometry_argv(window)

    def decide(self, window):
        """The calls that look at a resizable window that is not a fixed size: its hints and app, its size, the screen."""
        return [self.read_window(window), self.read_size(window), ['xrandr', '--current']]

    def look(self, window):
        """The calls of one look at a window while the property is on: the focus, its hints, its size."""
        return [self.read_focus(), self.read_hints(window), self.read_size(window)]

    def test_a_game_window_smaller_than_the_screen_gets_the_property_until_it_has_the_screen_size(self):
        self.focus = GAME
        self.poll()
        # One poll does it all: the focus (and the property, at the first poll), the window's hints and app, its size, the
        # screen, the focus once more, the write of the 1; then a look (the focus, the hints, the size) at which the
        # window has the screen size, and the write of the 0. The screen is not read again.
        self.assertEqual(self.calls, [self.read_focus_first(), *self.decide(GAME), self.read_focus(),
                                      follower.xprop_set_argv(FORCE, [1]), *self.look(GAME),
                                      follower.xprop_set_argv(FORCE, [0])])
        self.assertEqual((self.follower.forced, self.property), (0, 0))      # off when the poll returns
        self.assertEqual(self.windows[GAME]['size'], [1920, 2160])
        self.assertTrue(self.follower.fit_decided)
        self.calls.clear()
        self.poll(4)
        self.assertEqual(self.calls, [self.read_focus()] * 4)               # decided: only the focus is read
        self.assertEqual(self.fit_lines(), [
            'dosplex-display-follower: window fit: %s 0 -> 1 (window 0x600003, app 892970, no size hints, 1280x720, '
            'screen 1920x2160)' % FORCE,
            'dosplex-display-follower: window fit: %s 1 -> 0 (window 0x600003 has the screen size 1920x2160 after 40 '
            'ms)' % FORCE])

    def test_the_valheim_launch_on_the_steam_machine_gets_one_resize_and_leaves_the_property_off(self):
        # The Steam Machine, 2026-10-20 17:02:52: Steam's window 0x2000035 (no size hints) was focused; Valheim's
        # resizable 3840x1080 window then took the focus with the 1 chosen for Steam's window still on, and Gamescope
        # resized it in that focus pass (T-417). Now Steam's window, which Gamescope sizes itself, is left alone with
        # the property off, so Valheim's window takes the focus with it off. Valheim is still fitted once (the owner
        # expects Full screen to fill the TV), and the property is off again when that poll returns.
        self.screen = (3840, 2160)
        self.windows[STEAM]['size'] = [3840, 2160]
        self.windows[VALHEIM] = held(hints=hints_text(), app=GAME_APP, size=(3840, 1080))
        self.focus = STEAM
        self.poll(3)
        self.assertEqual(self.writes(), [])
        self.move_focus(VALHEIM)
        self.assertEqual(self.windows[VALHEIM]['size'], [3840, 1080])     # not resized in the focus pass
        self.calls.clear()
        self.poll()
        self.assertEqual(self.calls, [self.read_focus(), *self.decide(VALHEIM), self.read_focus(),
                                      follower.xprop_set_argv(FORCE, [1]), *self.look(VALHEIM),
                                      follower.xprop_set_argv(FORCE, [0])])
        self.assertEqual(self.windows[VALHEIM]['size'], [3840, 2160])     # the one resize
        self.assertEqual(self.property, 0)
        self.poll(5)
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertEqual(self.property, 0)
        # The game ends and Steam's window takes the focus with the property off: nothing to switch off, and
        # nothing but its hints and app is read.
        self.calls.clear()
        self.move_focus(STEAM)
        self.poll()
        self.assertEqual(self.calls, [self.read_focus(), self.read_window(STEAM)])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fit_lines()[-1], "dosplex-display-follower: window fit: left Steam's own window 0x2000035 "
                                               'alone (Gamescope sizes it itself)')

    def test_the_absolum_launch_on_the_steam_machine_is_never_forced(self):
        # The Steam Machine, 2026-10-20 17:03:52: Absolum's window 0x4a00001 states 3840x2160 as minimum and maximum.
        # It took the focus with the 1 chosen for Steam's window still on and was resized in that focus pass.
        self.screen = (3840, 2160)
        self.windows[STEAM]['size'] = [3840, 2160]
        self.windows[ABSOLUM] = held(hints=hints_text((3840, 2160), (3840, 2160)), app=GAME_APP, size=(3840, 2160))
        self.focus = STEAM
        self.poll(2)
        self.calls.clear()
        self.move_focus(ABSOLUM)
        self.poll(5)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.property, 0)
        self.assertEqual(self.calls, [self.read_focus(), self.read_window(ABSOLUM)] + [self.read_focus()] * 4)
        self.assertEqual(self.fit_lines()[-1], 'dosplex-display-follower: window fit: left window 0x4a00001, app 892970, '
                                               'minimum 3840x2160, maximum 3840x2160 alone')

    def test_steams_own_window_never_gets_the_property_whatever_its_size(self):
        # Gamescope sizes Steam's window in every focus pass, with the property on or off; a 1 written for it would
        # add nothing and could reach the window focused next. Its size is not even asked.
        for screen, cap, size in (((1920, 2160), None, (1280, 720)),       # read before Gamescope's pass ran
                                  ((3840, 2160), 1080, (1920, 1080)),       # under GAMESCOPE_STEAM_MAX_HEIGHT
                                  ((3840, 2160), None, (1920, 2160))):      # a refit read right after a layout change
            with self.subTest(screen=screen, cap=cap, size=size):
                self.follower, self.calls = self.new_follower(), []
                self.screen, self.steam_cap, self.windows[STEAM]['size'] = screen, cap, list(size)
                self.focus = STEAM
                self.poll(3)
                self.follower.refit = True
                self.poll(2)
                self.assertEqual(self.writes(), [])
                self.assertFalse(any(argv[0] == 'xdotool' for argv in self.calls))
                self.assertEqual(self.fit_lines()[-1], "dosplex-display-follower: window fit: left Steam's own window "
                                                       '0x2000035 alone (Gamescope sizes it itself)')
        # Steam in front while a revision changes the screen: Gamescope sizes Steam's window, the follower writes nothing.
        self.steam_cap = None
        self.resize_screen((3840, 2160))
        self.assertEqual(self.windows[STEAM]['size'], [3840, 2160])
        self.assertEqual(self.writes(), [])

    def test_a_steam_overlay_with_the_input_does_not_make_the_game_window_steams(self):
        # Gamescope writes GAMESCOPE_FOCUSED_APP from the input focus window, a Steam overlay here, and resizes the
        # focus window, whose app is GAMESCOPE_FOCUSED_APP_GFX. The follower reads the latter and never the former.
        self.screen, self.steam_cap = (3840, 2160), 1080
        self.windows[GAME]['size'] = [3840, 2160]
        self.input_focus, self.focus = STEAM, GAME
        self.poll(4)
        self.assertEqual(self.writes(), [])                                # it already has the screen size
        self.assertEqual(self.fit_lines(), ['dosplex-display-follower: window fit: left window 0x600003, app 892970, '
                                            'no size hints, 3840x2160, screen 3840x2160 alone'])
        self.assertNotIn(follower.xprop_get_argv(FOCUSED_APP), self.calls)
        # And a game window smaller than the screen behind the overlay is fitted as a game, then switched off.
        self.follower, self.calls = self.new_follower(), []
        self.windows[GAME]['size'] = [1920, 1080]
        self.poll(2)
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertEqual(self.windows[GAME]['size'], [3840, 2160])

    def test_a_window_without_size_hints_is_resizable(self):
        self.windows[GAME] = held(hints=None, size=(1280, 720))
        self.focus = GAME
        self.poll(2)
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertIn('window 0x600003, app 892970, no WM_NORMAL_HINTS, 1280x720, screen 1920x2160', self.fit_lines()[0])

    def test_a_window_that_states_one_size_once_the_property_is_on_gets_it_off_at_the_next_look(self):
        # A window that publishes fixed hints only after it was decided: the next look finds them, the property goes
        # off at once and Gamescope sizes the window back to its hints; it is not fitted again.
        self.windows[GAME] = held(hints=None, size=(1280, 720), stubborn=True)
        self.focus = GAME
        fixed = hints_text((1280, 720), (1280, 720))
        self.on_set = lambda value: self.windows[GAME].update(hints=fixed) if value else None
        self.poll()
        self.assertEqual(self.calls[-4:], [follower.xprop_set_argv(FORCE, [1]), self.read_focus(), self.read_hints(GAME),
                                           follower.xprop_set_argv(FORCE, [0])])
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertEqual(self.fit_lines()[-1], 'dosplex-display-follower: window fit: %s 1 -> 0 (window 0x600003 states '
                                               'one size now (minimum 1280x720, maximum 1280x720))' % FORCE)
        self.assertEqual(self.sleeps, [])                                  # the first look found it
        self.calls.clear()
        self.follower.refit = True
        self.poll(3)
        self.assertEqual(self.calls, [self.read_focus()] * 3)

    def test_a_game_window_that_already_has_the_screens_size_is_left_alone(self):
        self.windows[GAME] = held(hints=hints_text((640, 480), (3840, 2160)), size=(1920, 2160))
        self.focus = GAME
        self.poll(5)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls, [self.read_focus_first(), *self.decide(GAME)] + [self.read_focus()] * 4)
        self.assertEqual(self.fit_lines(), ['dosplex-display-follower: window fit: left window 0x600003, app 892970, '
                                            'minimum 640x480, maximum 3840x2160, 1920x2160, screen 1920x2160 alone'])

    def test_a_window_whose_app_is_not_named_yet_is_waited_for_and_decided_once_it_is(self):
        self.root_app, self.focus = 'empty', GAME          # Gamescope names no app for the focus window yet
        self.poll(3)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls, [self.read_focus_first(), self.read_window(GAME),
                                      self.read_focus(), self.read_window(GAME),
                                      self.read_focus(), self.read_window(GAME)])
        self.assertEqual(self.fit_lines(), ['dosplex-display-follower: window fit: window 0x600003: waiting for its app id '
                                            '(GAMESCOPE_FOCUSED_APP_GFX and its STEAM_GAME name none)'])
        self.root_app = 'focused'
        self.poll(2)
        self.assertEqual(self.writes(), ['1', '0'])

    def test_steam_game_is_read_only_while_the_root_has_no_focus_app(self):
        self.root_app = 'absent'
        self.windows[GAME]['app'] = None
        self.focus = GAME
        self.poll(2)
        self.assertEqual(self.writes(), [])                                # neither names an app
        self.assertIn('waiting for its app id', self.fit_lines()[0])
        self.windows[GAME]['app'] = GAME_APP
        self.poll(2)
        self.assertEqual(self.writes(), ['1', '0'])
        # The root names the app: the window's STEAM_GAME does not matter.
        self.follower, self.calls = self.new_follower(), []
        self.root_app, self.windows[RANGED] = OTHER_APP, held(app=None, size=(1280, 720))
        self.focus = RANGED
        self.poll()
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertIn('window 0x600007, app 262830', self.fit_lines()[-2])

    def test_a_window_that_keeps_its_size_gets_the_property_off_after_a_quarter_of_a_second(self):
        self.windows[GAME]['stubborn'] = True
        self.focus = GAME
        self.poll()
        # All inside one poll: the 1, a look about every 20 ms, and the 0 once the quarter of a second is over. A look
        # that has begun is finished (three tools of 10 ms here), so the 1 is on for 250 ms and at most that look more.
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertEqual((self.follower.forced, self.property), (0, 0))
        (on, _), (off, _) = self.set_times
        self.assertGreaterEqual(off - on, follower.FIT_SECONDS)
        self.assertLessEqual(off - on, follower.FIT_SECONDS + 3 * self.tool_seconds + 1e-9)
        self.assertEqual(self.sleeps[0], follower.FIT_STEP_SECONDS)
        self.assertLessEqual(max(self.sleeps), follower.FIT_STEP_SECONDS)
        self.assertEqual(self.fit_lines()[-1], 'dosplex-display-follower: window fit: %s 1 -> 0 (window 0x600003 is 1280x720, '
                                               'not 1920x2160, after 280 ms: left at that size)' % FORCE)
        self.assertTrue(self.follower.fit_decided)
        self.calls.clear()
        self.poll(3)
        self.assertEqual(self.calls, [self.read_focus()] * 3)             # not tried again for this window

    def test_a_look_that_finds_no_window_or_another_app_for_it_switches_the_property_off(self):
        # The look compares the whole focus, the window and the app Gamescope names for it, with what was decided.
        for name, change, reason in (
                ('another app', lambda: setattr(self, 'root_app', OTHER_APP), 'window 0x600003 is named for another app now'),
                ('no window', lambda: setattr(self, 'focus', None), 'no window is focused')):
            with self.subTest(name):
                self.follower, self.calls, self.property = self.new_follower(), [], 0
                self.root_app, self.focus = 'focused', GAME
                self.windows[GAME] = held(size=(1280, 720), stubborn=True)
                self.on_set = lambda value, change=change: change() if value else None   # right after the 1 was written
                self.poll()
                self.on_set = None
                self.assertEqual(self.calls[-3:], [follower.xprop_set_argv(FORCE, [1]), self.read_focus(),
                                                   follower.xprop_set_argv(FORCE, [0])])
                self.assertIn('%s 1 -> 0 (%s)' % (FORCE, reason), self.fit_lines()[-1])
                self.assertEqual((self.follower.forced, self.property), (0, 0))
                self.assertFalse(self.follower.fit_decided)

    def test_a_window_that_takes_the_focus_while_the_property_is_on_is_resized_and_the_next_look_switches_it_off(self):
        # What is left of the risk: Gamescope resizes a window that takes the focus while the property is 1, in that
        # focus pass, before the follower looks. That lasts for one look, not for a poll: the next look finds the focus
        # moved and writes the 0, and the next poll decides the window that has the focus.
        self.windows[GAME]['stubborn'] = True
        self.windows[RANGED] = held(hints=hints_text((640, 480), (3840, 2160)), size=(1280, 720))
        self.focus = GAME
        self.on_set = lambda value: self.move_focus(RANGED) if value else None   # right after the 1 was written
        self.poll()
        self.assertEqual(self.windows[RANGED]['size'], [1920, 2160])           # resized in the focus pass
        self.assertEqual(self.calls[-3:], [follower.xprop_set_argv(FORCE, [1]), self.read_focus(),
                                           follower.xprop_set_argv(FORCE, [0])])
        self.assertEqual(self.fit_lines()[-1], 'dosplex-display-follower: window fit: %s 1 -> 0 (the focus moved to window '
                                               '0x600007)' % FORCE)
        self.assertEqual((self.follower.forced, self.property), (0, 0))
        self.assertFalse(self.follower.fit_decided)                            # a window that lost the focus is not decided
        self.on_set = None
        self.calls.clear()
        self.poll()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fit_lines()[-1], 'dosplex-display-follower: window fit: left window 0x600007, app 892970, '
                                               'minimum 640x480, maximum 3840x2160, 1920x2160, screen 1920x2160 alone')

    def test_a_focus_change_while_a_window_is_looked_at_writes_nothing_for_it(self):
        # The focus is read once more right before the write: a window that took the focus while the one before was
        # looked at is not resized for that one, and is decided itself at the next poll.
        self.windows[GAME]['size'] = [1280, 720]
        self.focus = GAME
        real_tool = self.tool

        def steam_takes_the_focus(argv, environment):
            if argv == ['xrandr', '--current'] and self.focus == GAME:
                self.move_focus(STEAM)
            return real_tool(argv, environment)
        with mock.patch.object(follower, 'run_tool', steam_takes_the_focus):
            self.poll()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.property, 0)
        self.assertEqual(self.fit_lines(), ['dosplex-display-follower: window fit: the focus moved while window 0x600003 '
                                            'was looked at; nothing written'])
        self.poll()
        self.assertEqual(self.writes(), [])
        self.assertIn("left Steam's own window 0x2000035 alone", self.fit_lines()[-1])

    def test_a_new_screen_size_after_an_applied_revision_fits_the_window_again(self):
        self.focus = GAME
        self.poll(2)
        self.assertEqual(self.writes(), ['1', '0'])
        self.calls.clear()
        self.follower.refit = True                                         # a revision was applied, same screen
        self.poll(2)
        self.assertEqual(self.calls, [self.read_focus(), ['xrandr', '--current'], self.read_focus()])
        self.assertEqual(self.writes(), [])
        self.resize_screen((3840, 2160))                                   # Gamescope leaves a resizable window as it is
        self.assertEqual(self.windows[GAME]['size'], [1920, 2160])
        self.follower.refit = True
        self.poll(3)
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertEqual(self.windows[GAME]['size'], [3840, 2160])
        self.assertIn('the screen is 3840x2160 now, was 1920x2160: window 0x600003 is fitted again', self.log_text())

    def test_an_applied_revision_asks_for_a_refit_and_a_deferred_one_does_not(self):
        self.follower.apply = lambda contract, games=(): None
        self.follower.deliver_report = lambda: None
        self.follower.apply_and_report(follower.Contract(1, 1920, 2160, 'fit'))
        self.assertTrue(self.follower.refit)
        self.follower.refit = False
        self.follower.apply = lambda contract, games=(): (1920, 2160)
        self.follower.apply_and_report(follower.Contract(2, 3840, 2160, 'fit'), [GAME_APP])
        self.assertFalse(self.follower.refit)

    def test_a_game_window_that_states_one_size_is_never_forced_and_its_size_is_not_asked(self):
        self.focus = FIXED
        self.poll(3)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls, [self.read_focus_first(), self.read_window(FIXED)] + [self.read_focus()] * 2)

    def test_the_first_poll_reads_the_property_and_switches_off_one_left_on(self):
        for focus in (None, FIXED, GAME):
            with self.subTest(focus=focus):
                self.follower, self.focus, self.calls = self.new_follower(), focus, []
                self.windows[GAME] = held(size=(1920, 2160))                 # already has the screen size
                self.property = 0
                self.poll(3)
                self.assertEqual(self.writes(), [])                          # off: nothing to write
                self.assertEqual(self.follower.forced, 0)
                self.assertEqual(self.calls[0], self.read_focus_first())
                self.assertNotIn(self.read_focus_first(), self.calls[1:])    # read once
                self.follower, self.calls = self.new_follower(), []
                self.property = 1                                            # left on by a follower that ended
                self.poll()
                self.assertEqual(self.calls[:2], [self.read_focus_first(), follower.xprop_set_argv(FORCE, [0])])
                self.assertEqual(self.property, 0)
                self.assertIn('%s 1 -> 0 (on when the follower started)' % FORCE, self.log_text())

    def test_a_switch_off_at_the_start_that_fails_is_tried_again(self):
        self.property, self.focus = 1, FIXED
        self.failing['set'] = follower.ToolError('xprop failed: broken')
        self.poll(2)
        self.assertEqual(self.property, 1)
        self.assertIsNone(self.follower.forced)
        self.assertEqual([argv for argv in self.calls if argv == self.read_focus_first()], [self.read_focus_first()] * 2)
        del self.failing['set']
        self.poll()
        self.assertEqual(self.property, 0)
        self.assertEqual(self.follower.forced, 0)

    def test_release_switches_off_a_property_that_a_failed_switch_off_left_on_and_nothing_else(self):
        self.focus = FIXED
        self.poll()
        self.follower.release()
        self.assertEqual(self.writes(), [])                                # off: nothing to switch off
        # A 0 that fails at the end of a one-shot leaves the property on, and `forced` says so.
        self.windows[GAME]['stubborn'] = True
        self.move_focus(GAME)
        self.on_set = lambda value: self.failing.update(set=follower.ToolError('xprop failed: broken')) if value else None
        self.poll()
        self.assertEqual((self.property, self.follower.forced), (1, 1))
        self.assertIn('could not be switched off', self.log_text())
        self.calls.clear()
        del self.failing['set']
        self.on_set = None
        self.follower.release()
        self.assertEqual(self.writes(), ['0'])
        self.assertEqual((self.property, self.follower.forced), (0, 0))
        self.assertIn('%s 1 -> 0 (the follower is stopping)' % FORCE, self.fit_lines()[-1])
        self.follower.release()
        self.assertEqual(self.writes(), ['0'])                             # off again: nothing more
        # A write that fails on the way out is logged and does not raise.
        self.stderr.seek(0)
        self.stderr.truncate()
        self.follower.forced, self.failing['set'] = 1, follower.ToolError('xprop failed: broken')
        self.follower.release()
        self.assertIn('could not be switched off', self.log_text())

    def leave_the_property_on(self):
        """A poll for a window that keeps its size and whose 0 fails: the one-shot ends with the property still on and
        `forced` saying so, as after a tool that failed at the wrong moment. The failure is gone again when this returns,
        and so is the log of the poll."""
        self.windows[GAME]['stubborn'] = True
        self.focus = GAME
        self.on_set = lambda value: self.failing.update(set=follower.ToolError('xprop failed: first')) if value else None
        self.poll()
        self.on_set = None
        del self.failing['set']
        self.assertEqual((self.property, self.follower.forced), (1, 1))
        self.stderr.seek(0)
        self.stderr.truncate()

    def test_no_focused_window_turns_a_property_that_is_owed_off_without_reading_any_window(self):
        self.leave_the_property_on()
        self.calls.clear()
        self.focus = None
        self.poll(3)
        self.assertEqual(self.calls, [self.read_focus(), follower.xprop_set_argv(FORCE, [0])] + [self.read_focus()] * 2)
        self.assertEqual((self.follower.forced, self.property), (0, 0))
        self.assertIn('%s 1 -> 0 (it may have been left on by the last call)' % FORCE, self.log_text())

    def test_a_window_decided_by_a_one_shot_whose_0_failed_is_not_fitted_again_when_the_0_is_written(self):
        self.leave_the_property_on()                                       # decided (left at that size); a 0 is owed
        self.assertTrue(self.follower.fit_decided)
        self.calls.clear()
        self.poll(2)
        self.assertEqual(self.calls, [self.read_focus(), follower.xprop_set_argv(FORCE, [0]), self.read_focus()])
        self.assertEqual((self.follower.forced, self.property), (0, 0))

    def test_a_switch_off_that_is_owed_is_tried_again_before_anything_else_and_a_failing_tool_is_logged_once(self):
        for tool in ('focus', 'set'):
            for error in (follower.ToolError('xprop failed: broken'),
                          follower.DisplayUnreachable("xprop: unable to open display ':99'")):
                with self.subTest(tool=tool, error=type(error).__name__):
                    self.follower, self.failing, self.property = self.new_follower(), {}, 0
                    self.leave_the_property_on()
                    self.focus, self.failing[tool] = FIXED, error      # the focus has moved on: nothing is decided
                    self.poll(3)                                       # and none of this raises
                    self.assertEqual((self.follower.forced, self.property), (1, 1))   # the property stays as it is
                    failures = [line for line in self.log_text().splitlines()
                                if line.startswith('dosplex-display-follower: fit:')]
                    self.assertEqual(len(failures), 1, self.log_text())
                    self.assertNotIn('window fit', self.log_text())    # no change took place
                    del self.failing[tool]
                    self.poll()
                    self.assertEqual((self.follower.forced, self.property), (0, 0))   # tried again at the next poll
                    self.assertIn('fit: recovered', self.log_text())
                    self.assertEqual(self.follower.errors, {})

    def test_the_property_is_off_within_a_quarter_of_a_second_even_when_reads_fail_and_is_tried_again_at_the_next_poll(self):
        # The reads that tell when the property can go off fail from the moment it is on, at every poll, while the window
        # keeps the focus. The property used to stay on then, for as long as the failure lasted, and the next window to take
        # the focus was resized in Gamescope's focus pass.
        for tool in ('focus', 'hints', 'geometry'):
            with self.subTest(tool=tool):
                self.follower, self.failing_while_on, self.set_times = self.new_follower(), {}, []
                self.property, self.calls = 0, []
                self.windows[GAME] = held(size=(1280, 720), stubborn=True)
                self.focus = GAME
                self.failing_while_on[tool] = follower.ToolError('%s failed: broken' % tool)
                self.stderr.seek(0)
                self.stderr.truncate()
                for _ in range(5):
                    self.poll()
                    self.assertEqual((self.follower.forced, self.property), (0, 0))   # off again when the poll returns
                self.assertEqual(self.writes(), ['1', '0'] * 5)
                ons, offs = ([at for at, value in self.set_times if value == wanted] for wanted in (1, 0))
                for on, off in zip(ons, offs):
                    self.assertLessEqual(off - on, follower.FIT_SECONDS)       # within a quarter of a second ...
                    self.assertLess(off - on, 5 * self.tool_seconds)           # ... and in fact at the first look
                self.assertFalse(self.follower.fit_decided)                    # nothing is decided: tried again
                failures = [line for line in self.log_text().splitlines()
                            if line.startswith('dosplex-display-follower: fit:')]
                self.assertEqual(len(failures), 1, self.log_text())            # a failure that lasts is logged once
                self.assertIn('%s 1 -> 0 (a tool failed: %s failed: broken)' % (FORCE, tool), self.log_text())
                del self.failing_while_on[tool]
                self.poll()
                self.assertTrue(self.follower.fit_decided)                     # decided at the next poll
                self.assertEqual((self.follower.forced, self.property), (0, 0))
                self.assertIn('fit: recovered', self.log_text())

    def test_a_tool_that_fails_while_a_window_is_looked_at_leaves_it_undecided_and_is_logged_once(self):
        for tool in ('window', 'geometry', 'screen'):
            for error in (follower.ToolError('%s failed: broken' % tool),
                          follower.DisplayUnreachable("xprop: unable to open display ':99'")):
                with self.subTest(tool=tool, error=type(error).__name__):
                    self.follower, self.failing, self.calls = self.new_follower(), {}, []
                    self.windows[GAME] = held(size=(1280, 720))
                    self.focus, self.property = None, 0
                    self.poll()                                        # the start check, with nothing focused
                    self.failing[tool] = error
                    self.stderr.seek(0)
                    self.stderr.truncate()
                    self.focus = GAME                                  # would be forced
                    self.poll(3)
                    self.assertEqual(self.follower.forced, 0)          # nothing was written
                    self.assertEqual(self.writes(), [])
                    self.assertFalse(self.follower.fit_decided)
                    failures = [line for line in self.log_text().splitlines()
                                if line.startswith('dosplex-display-follower: fit:')]
                    self.assertEqual(len(failures), 1, self.log_text())
                    self.assertNotIn('window fit', self.log_text())
                    del self.failing[tool]
                    self.poll()
                    self.assertTrue(self.follower.fit_decided)         # decided at the next poll
                    self.assertEqual(self.writes(), ['1', '0'])        # and fitted, with the property off again
                    self.assertEqual((self.follower.forced, self.property), (0, 0))
                    self.assertIn('fit: recovered', self.log_text())

    def test_a_write_of_the_1_that_fails_leaves_a_0_owed_until_it_is_written(self):
        # The 1 did not reach Gamescope, but xprop may have set it before it failed: a 0 is tried at once and at every poll
        # after that, before anything else, and the window is not switched on again meanwhile.
        for error in (follower.ToolError('set failed: broken'),
                      follower.DisplayUnreachable("xprop: unable to open display ':99'")):
            with self.subTest(error=type(error).__name__):
                self.follower, self.failing, self.calls = self.new_follower(), {}, []
                self.windows[GAME] = held(size=(1280, 720))
                self.focus, self.property = None, 0
                self.poll()                                            # the start check, with nothing focused
                self.failing['set'] = error
                self.stderr.seek(0)
                self.stderr.truncate()
                self.focus = GAME
                self.poll(3)
                self.assertEqual((self.follower.forced, self.property), (1, 0))
                self.assertEqual(self.writes(), ['1', '0', '0', '0'])
                self.assertFalse(self.follower.fit_decided)
                failures = [line for line in self.log_text().splitlines()
                            if line.startswith('dosplex-display-follower: fit:')]
                self.assertEqual(len(failures), 1, self.log_text())
                self.assertEqual(self.log_text().count('could not be switched off'), 1)
                self.assertNotIn(' -> ', self.log_text())              # no change took place
                del self.failing['set']
                self.poll()
                self.assertEqual((self.follower.forced, self.property), (0, 0))
                self.assertTrue(self.follower.fit_decided)
                self.assertEqual(self.writes(), ['1', '0', '0', '0', '0', '1', '0'])
                self.assertIn('%s 1 -> 0 (it may have been left on by the last call)' % FORCE, self.log_text())
                self.assertIn('fit: recovered', self.log_text())

    def test_a_fixed_size_window_that_takes_the_focus_right_after_a_fit_is_never_resized(self):
        # The one-shot is over when the poll returns. The property used to stay on until the next poll, and a window that
        # took the focus in between was resized in Gamescope's focus pass: a fixed-size one to the screen size.
        self.focus = GAME
        self.poll()
        self.assertEqual(self.windows[GAME]['size'], [1920, 2160])           # fitted
        self.assertEqual((self.follower.forced, self.property), (0, 0))
        self.move_focus(FIXED)                                               # the next window takes the focus
        self.assertEqual(self.windows[FIXED]['size'], [640, 480])            # Gamescope's focus pass left it alone
        self.calls.clear()
        self.poll(3)
        self.assertEqual(self.calls, [self.read_focus(), self.read_window(FIXED)] + [self.read_focus()] * 2)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.windows[FIXED]['size'], [640, 480])

    def test_a_stop_during_the_write_of_the_1_leaves_the_property_off(self):
        # The stop unwinds the xprop call after xprop has set the property, so the follower never saw the write answer.
        # The write counts as made from the moment it is sent: the 0 is written on the way out.
        self.windows[GAME]['stubborn'] = True
        self.focus = GAME

        def stop(value):
            if value:
                raise SystemExit(0)            # what the signal handler raises inside subprocess.run
        self.on_set = stop
        with self.assertRaises(SystemExit):
            self.poll()
        self.assertEqual((self.property, self.follower.forced), (0, 0))
        self.assertEqual(self.writes(), ['1', '0'])
        self.assertIn('%s 1 -> 0 (the follower is stopping)' % FORCE, self.fit_lines()[-1])
        self.calls.clear()
        self.follower.release()
        self.assertEqual(self.calls, [])                                     # nothing left for the switch-off at the end

    def test_a_second_stop_during_the_0_leaves_it_to_the_switch_off_on_the_way_out(self):
        # Another signal arrives while the 0 is written, before xprop has done anything: the property is still on, and
        # `forced` still says so, so that release() writes the 0 (main() ignores signals by then).
        self.windows[GAME]['stubborn'] = True
        self.focus = GAME
        real_tool = self.tool
        writes = []

        def stops(argv, environment):
            if '-set' in argv:
                writes.append(argv[-1])
                if writes == ['1', '0']:
                    raise SystemExit(0)        # the second signal, before xprop set anything
            result = real_tool(argv, environment)
            if '-set' in argv and writes == ['1']:
                raise SystemExit(0)            # the first one, after xprop set the property
            return result
        with mock.patch.object(follower, 'run_tool', stops):
            with self.assertRaises(SystemExit):
                self.poll()
        self.assertEqual(writes, ['1', '0'])
        self.assertEqual((self.property, self.follower.forced), (1, 1))
        self.follower.release()
        self.assertEqual((self.property, self.follower.forced), (0, 0))
        self.assertIn('%s 1 -> 0 (the follower is stopping)' % FORCE, self.fit_lines()[-1])

    def test_the_scripted_gamescopes_focus_pass_resizes_the_game_window_while_the_property_is_1(self):
        # The tests around this one lean on the focus pass of the scripted Gamescope: it resizes the focus window while
        # the property is 1 (Steam's own window always), and at no other time. First one poll, followed call by call: what
        # each call left as the property and as the size of the window.
        trace = []
        real_tool = self.tool

        def traced(argv, environment):
            result = real_tool(argv, environment)
            trace.append((argv[-1] if '-set' in argv else argv[0], self.property, tuple(self.windows[GAME]['size'])))
            return result
        self.focus = GAME
        with mock.patch.object(follower, 'run_tool', traced):
            self.poll()
        on = [index for index, (what, _, _) in enumerate(trace) if what == '1']
        off = [index for index, (what, _, _) in enumerate(trace) if what == '0']
        self.assertEqual((len(on), len(off)), (1, 1))
        small, big = (1280, 720), (1920, 2160)
        self.assertEqual({(prop, size) for _, prop, size in trace[:on[0]]}, {(0, small)})   # off: the window is not touched
        self.assertEqual({(prop, size) for _, prop, size in trace[on[0]:off[0]]}, {(1, big)})  # on: resized in that pass
        self.assertEqual(trace[off[0]][1:], (0, big))                        # off again: the window keeps the size
        # The same pass for a window that takes the focus: resized while the property is 1, and not while it is 0.
        self.windows[RANGED] = held(hints=hints_text((640, 480), (3840, 2160)), size=(1280, 720))
        self.property = 0
        self.move_focus(RANGED)
        self.assertEqual(self.windows[RANGED]['size'], [1280, 720])
        self.property = 1
        self.move_focus(RANGED)
        self.assertEqual(self.windows[RANGED]['size'], [1920, 2160])
        # Steam's own window gets it with the property off as well.
        self.property = 0
        self.windows[STEAM]['size'] = [1280, 720]
        self.move_focus(STEAM)
        self.assertEqual(self.windows[STEAM]['size'], [1920, 2160])

    def test_a_failing_fit_does_not_count_toward_the_end_of_the_follower_and_an_answer_ends_the_streak(self):
        self.failing['focus'] = follower.DisplayUnreachable("xprop: unable to open display ':99'")
        self.poll(5)
        self.assertEqual(self.follower.display_failures, 0)
        self.assertFalse(self.follower.display_lost)
        del self.failing['focus']
        self.follower.display_failures = 2          # two unreachable checks so far
        self.poll()
        self.assertEqual(self.follower.display_failures, 0)

    def test_it_runs_after_every_apply_and_every_poll_until_the_display_is_lost(self):
        events, revisions = [], iter([1, 1, 2, 2, 2, 2, 2, 2])

        def poll_contract():
            events.append('poll')
            return follower.Contract(next(revisions), 1920, 2160, 'fit')

        def watch_games():
            events.append('games')
            return []

        def apply_and_report(contract, games):
            events.append('apply %d' % contract.revision)
            self.follower.settled = contract.revision

        def probe_display():
            events.append('probe')
            self.follower.display_lost = self.follower.cycles == 8

        self.follower.poll_contract = poll_contract
        self.follower.watch_games = watch_games
        self.follower.apply_and_report = apply_and_report
        self.follower.probe_display = probe_display
        self.follower.fit_windows = lambda: events.append('fit')
        for _ in range(8):
            self.follower.cycle()
        # The games are read at every poll, after the contract and before anything is applied.
        self.assertEqual(events, ['poll', 'games', 'apply 1', 'fit', 'poll', 'games', 'fit',
                                  'poll', 'games', 'apply 2', 'fit', 'poll', 'games', 'probe', 'fit',
                                  'poll', 'games', 'fit', 'poll', 'games', 'fit', 'poll', 'games', 'fit',
                                  'poll', 'games', 'probe'])
        self.assertTrue(self.follower.display_lost)


class FakeClock:
    """The follower's time.monotonic and time.sleep: sleeping costs nothing and only moves the clock."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptOver(BaseException):
    """A scripted supervisor was asked for more exchanges than the test gave it. Not an Exception: that would loop."""


class ControlRetryTests(unittest.TestCase):
    """Follower.run against a scripted supervisor and a clock that costs nothing: when a failed exchange is asked again.

    A script lists what each control exchange does, in order: 'timeout' (the follower waits its control timeout and
    gets no reply), 'refused' (the connection is refused at once), ('live', N) (the supervisor serves revision N) and
    'ack' (it acknowledges a report). The loop ends after the last step. Times are seconds since the loop started.
    """
    LIVE = {'x': 0, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 1920, 'game_height': 2160,
            'scale_mode': 'fit', 'layout': 'vertical'}

    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(follower, 'time', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def run_script(self, script, poll, control_timeout=3.0, retry_interval=1.0):
        script, began = list(script), self.clock.now
        self.follower = follower.Follower('/run/dosplex/control.sock', INSTANCE, 0, poll,
                                          environment={'DISPLAY': DISPLAY}, control_timeout=control_timeout,
                                          retry_interval=retry_interval)
        self.attempts = []      # (seconds since the start, 'get' or 'report'), one per control exchange

        def exchange(payload):
            self.attempts.append((self.clock.now - began, payload['display']))
            if not script:
                raise ScriptOver()
            step = script.pop(0)
            if not script:
                self.follower.display_lost = True      # the last step: the loop ends after this cycle
            if step == 'timeout':
                self.clock.now += control_timeout
                raise TimeoutError('timed out')
            if step == 'refused':
                raise ConnectionRefusedError('Connection refused')
            if step == 'ack':
                return {'ok': True}
            return {'ok': True, 'live': dict(self.LIVE, display_revision=step[1])}

        self.follower.exchange = exchange
        self.follower.apply = lambda contract, games=(): None    # no X tools: only the control exchanges matter here
        self.follower.watch_games = lambda: []
        self.follower.fit_windows = lambda: None
        self.follower.probe_display = lambda: None
        self.assertEqual(self.follower.run(), 0)

    def gets(self):
        return [round(at, 3) for at, kind in self.attempts if kind == 'get']

    def reports(self):
        return [round(at, 3) for at, kind in self.attempts if kind == 'report']

    def episodes(self, kind='control'):
        """The follower's log lines about one kind of error, without the program name."""
        return [line.split(kind + ': ', 1)[1] for line in self.stderr.getvalue().splitlines() if (kind + ': ') in line]

    def test_a_get_that_times_out_after_a_revision_change_is_asked_again_inside_the_retry_interval(self):
        # Revision 1 is applied. The next poll, 10 s later, finds revision 2 and its get times out after 0.4 s.
        self.run_script([('live', 1), 'ack', 'timeout', ('live', 2), 'ack'], poll=10, control_timeout=0.4)
        # The second try comes 1 s after the failed poll began, not 10 s: revision 2 is applied at 11 s, not at 20 s.
        self.assertEqual(self.gets(), [0, 10, 11])
        self.assertEqual(self.reports(), [0, 11])
        self.assertEqual(self.follower.settled, 2)
        self.assertEqual(self.episodes(), ['TimeoutError: timed out', 'recovered'])

    def test_a_timeout_that_took_longer_than_the_retry_interval_is_asked_again_at_once(self):
        self.run_script([('live', 1), 'ack', 'timeout', ('live', 2), 'ack'], poll=10, control_timeout=3.0)
        self.assertEqual(self.gets(), [0, 10, 13])      # no wait between the timeout at 13 s and the second try
        self.assertEqual(self.follower.settled, 2)

    def test_a_supervisor_that_stays_silent_is_asked_quickly_three_times_and_then_at_the_normal_poll(self):
        # 1 s of retry interval and 0.5 s of timeout. The failures of 10, 11, 12 and 13 s are the first try and
        # its three quick retries; 23 and 33 s are normal polls (10 s after the start of the last try).
        silence = ['timeout'] * 6
        self.run_script([('live', 1), 'ack', *silence, ('live', 2), 'ack'], poll=10, control_timeout=0.5)
        self.assertEqual(self.gets(), [0, 10, 11, 12, 13, 23, 33, 43])
        self.assertEqual(self.reports(), [0, 43])
        self.assertEqual(self.follower.settled, 2)
        # One log line for the whole stretch and one when the supervisor answers again, not one per try.
        self.assertEqual(self.episodes(), ['TimeoutError: timed out', 'recovered'])

    def test_a_successful_exchange_gives_the_quick_retries_back(self):
        self.run_script([('live', 1), 'ack', *['timeout'] * 4, ('live', 1), 'timeout', ('live', 1)],
                        poll=10, control_timeout=0.5)
        # 10 to 13 s: first try and three quick retries; 23 s: normal poll, which succeeds. Then 33 s fails
        # and has its quick retry again, at 34 s.
        self.assertEqual(self.gets(), [0, 10, 11, 12, 13, 23, 33, 34])
        self.assertEqual(self.episodes(),
                         ['TimeoutError: timed out', 'recovered', 'TimeoutError: timed out', 'recovered'])

    def test_with_the_default_timing_nothing_waits_longer_than_before(self):
        # Poll 0.5 s, timeout 3 s, retry interval 1 s. A get that times out is asked again at once, as it always was.
        self.run_script(['timeout'] * 3 + [('live', 1), 'ack'], poll=follower.POLL_SECONDS,
                        control_timeout=follower.CONTROL_TIMEOUT_SECONDS, retry_interval=follower.CONTROL_RETRY_SECONDS)
        self.assertEqual(self.gets(), [0, 3, 6, 9])
        # One that is refused at once is asked again at the next poll, 0.5 s later: the retry interval is longer.
        self.stderr.seek(0)
        self.stderr.truncate()
        self.run_script(['refused'] * 4 + [('live', 1), 'ack'], poll=follower.POLL_SECONDS)
        self.assertEqual(self.gets(), [0, 0.5, 1.0, 1.5, 2.0])
        self.assertEqual(self.episodes(), ['ConnectionRefusedError: Connection refused', 'recovered'])

    def test_a_report_without_a_reply_is_sent_again_inside_the_retry_interval(self):
        self.run_script([('live', 1), 'timeout', 'ack', ('live', 1)], poll=10, control_timeout=0.4)
        self.assertEqual(self.reports(), [0, 1])
        self.assertEqual(self.gets(), [0, 1])
        self.assertIsNone(self.follower.pending_report)
        self.assertEqual(self.episodes('report'), ['TimeoutError: timed out', 'recovered'])

    def test_a_poll_interval_below_the_retry_interval_is_never_stretched(self):
        self.run_script(['refused'] * 3 + [('live', 1), 'ack'], poll=0.2, control_timeout=0.4, retry_interval=5)
        self.assertEqual(self.gets(), [0, 0.2, 0.4, 0.6])


class FocusStepTests(unittest.TestCase):
    """Follower.refresh_focus with scripted tools and a clock that costs nothing: when the focus cycle is made.

    Each poll is 0.5 s after the one before it, the first at 0 s. A window is first seen by the poll that follows the
    change; the cycle is made at the first poll 0.75 s or more after that, if it is still the focus then."""
    CYCLE = [['xdotool', 'windowfocus', '1023'], ['xdotool', 'windowfocus', str(STEAM)]]

    def setUp(self):
        self.clock = FakeClock()
        for patcher in (mock.patch.object(follower, 'time', self.clock),
                        mock.patch.object(follower, 'run_tool', self.tool)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.window, self.app = STEAM, STEAM_UI     # GAMESCOPE_FOCUSED_WINDOW (None: empty) and GAMESCOPE_FOCUSED_APP
        self.failing = {}                           # 'read', 'root' or 'focus' -> the error that tool raises
        self.calls = []
        self.follower = follower.Follower('/run/dosplex/control.sock', INSTANCE, 0, environment={'DISPLAY': DISPLAY})

    def tool(self, argv, environment):
        self.calls.append(argv)
        kind = 'read' if argv[:2] == ['xprop', '-root'] else 'root' if argv[0] == 'xwininfo' else 'focus'
        if kind in self.failing:
            raise self.failing[kind]
        if kind == 'read':
            self.assertEqual(argv[2:], [FOCUS, FOCUSED_APP])
            return ('%s(CARDINAL) = %s\n%s(CARDINAL) = %s\n'
                    % (FOCUS, self.window or '', FOCUSED_APP, self.app or ''))
        if kind == 'root':
            return '\nxwininfo: Window id: 1023 (the root window) (has no name)\n'
        self.assertEqual(argv[:2], ['xdotool', 'windowfocus'])
        return ''

    def poll(self, times=1):
        for _ in range(times):
            self.follower.refresh_focus()
            self.clock.sleep(0.5)

    def cycles(self):
        return [argv for argv in self.calls if argv[:2] == ['xdotool', 'windowfocus']]

    def test_steam_gets_one_cycle_after_the_settle_root_first_and_none_while_it_stays_the_focus(self):
        self.poll(2)                                # 0 s and 0.5 s: seen, 0.5 s is short of the settle
        self.assertEqual(self.cycles(), [])
        self.poll(1)                                # 1 s
        self.assertEqual(self.cycles(), self.CYCLE)
        self.poll(10)                               # the property notifies again and again: no repeat
        self.assertEqual(self.cycles(), self.CYCLE)
        self.assertEqual(self.stderr.getvalue().count('focus refresh: window 0x%x' % STEAM), 1)

    def test_a_game_in_front_gets_no_cycle_even_when_its_window_is_the_focus_for_long(self):
        self.window, self.app = GAME, GAME_APP
        self.poll(8)
        self.assertEqual(self.cycles(), [])
        self.assertNotIn('xwininfo', [argv[0] for argv in self.calls])

    def test_a_focus_that_moves_away_before_the_settle_gets_no_cycle(self):
        self.poll(1)                                # Steam's window is seen at 0 s
        self.window, self.app = GAME, GAME_APP      # a game takes the focus at 0.5 s
        self.poll(1)
        self.window, self.app = None, None          # and nothing is focused
        self.poll(4)
        self.assertEqual(self.cycles(), [])

    def test_the_window_that_returns_after_a_game_gets_a_new_cycle_but_not_within_a_second(self):
        self.poll(3)
        self.assertEqual(len(self.cycles()), 2)
        self.window, self.app = GAME, GAME_APP
        self.poll(1)                                # 1.5 s
        self.window, self.app = STEAM, STEAM_UI     # back at 2 s; the last cycle was at 1 s
        self.poll(2)                                # 2 s, 2.5 s: short of the settle
        self.assertEqual(len(self.cycles()), 2)
        self.poll(1)                                # 3 s
        self.assertEqual(self.cycles(), self.CYCLE * 2)

    def test_the_rate_limit_holds_a_cycle_back_and_does_not_lose_it(self):
        self.follower.refresh_last = self.clock.now + 1.5    # a cycle is due to have run at 1.5 s
        self.poll(3)                                # settled at 1 s, but 1 s after the last is 2.5 s
        self.assertEqual(self.cycles(), [])
        self.poll(1)                                # 1.5 s: still short
        self.poll(2)                                # 2 s, 2.5 s
        self.assertEqual(self.cycles(), self.CYCLE)

    def test_an_empty_focus_is_not_a_change_so_the_same_window_back_is_not_refreshed_again(self):
        self.poll(3)
        self.assertEqual(len(self.cycles()), 2)
        self.window = None
        self.poll(1)
        self.window = STEAM
        self.poll(6)
        self.assertEqual(len(self.cycles()), 2)

    def test_a_failing_tool_is_logged_once_and_the_cycle_is_not_repeated(self):
        self.failing['focus'] = follower.ToolError('xdotool failed: XSetInputFocus failed: BadWindow')
        self.poll(8)
        self.assertEqual(len(self.cycles()), 1)     # the root, which failed; nothing after it
        self.assertEqual(self.stderr.getvalue().count('focus refresh:'), 1)

    def test_the_root_window_is_read_once(self):
        self.poll(3)
        self.window = GAME
        self.poll(1)
        self.window = STEAM
        self.poll(4)
        self.assertEqual(len([argv for argv in self.calls if argv[0] == 'xwininfo']), 1)


class WakeStepTests(unittest.TestCase):
    """Follower.watch_games with a scripted xprop and xdotool and a clock that costs nothing: when Steam is woken.

    Each poll is 0.5 s after the one before it, the first at 0 s; times are seconds since the start of the test.
    A game that appears is first seen by the poll that follows the change, and Steam is woken for it at the first
    poll three seconds or more after that, if Gamescope has presented Steam (app 769) at every poll in between."""
    ESCAPE = ['xdotool', 'key', 'Escape']

    def setUp(self):
        self.clock = FakeClock()
        for patcher in (mock.patch.object(follower, 'time', self.clock),
                        mock.patch.object(follower, 'run_tool', self.tool)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.start()

    def start(self):
        """A new follower, with Steam's interface alone on the nested display and in front, at time 0."""
        self.began = self.clock.now
        self.apps = [STEAM_UI]      # what GAMESCOPE_FOCUSABLE_APPS lists; None: the property is not set
        self.focused = STEAM_UI     # the app GAMESCOPE_FOCUSED_APP names; None: it is not set
        self.failing = {}           # 'games', 'focus' or 'key' -> the error that tool raises
        self.calls = []             # (seconds since the start, argv) of every tool call
        self.stderr.seek(0)
        self.stderr.truncate()
        self.follower = follower.Follower('/run/dosplex/control.sock', INSTANCE, 0, environment={'DISPLAY': DISPLAY})

    def tool(self, argv, environment):
        self.calls.append((self.clock.now - self.began, argv))
        kinds = {tuple(follower.xprop_get_argv(FOCUSABLE)): 'games',
                 tuple(follower.xprop_get_argv(FOCUSED_APP)): 'focus', tuple(self.ESCAPE): 'key'}
        kind = kinds.get(tuple(argv))
        self.assertIsNotNone(kind, 'a tool that the wake has no business with: %r' % (argv,))
        if kind in self.failing:
            raise self.failing[kind]
        if kind == 'key':
            return ''
        name, value = (FOCUSABLE, self.apps) if kind == 'games' else (FOCUSED_APP, self.focused)
        if value is None:
            return name + ':  not found.\n'
        items = value if isinstance(value, list) else [value]
        return '%s(CARDINAL) = %s\n' % (name, ', '.join(str(item) for item in items))

    def poll(self, times=1):
        """Poll `times` times, 0.5 s apart; returns what the last poll made of the games."""
        for _ in range(times):
            games = self.follower.watch_games()
            self.clock.sleep(0.5)
        return games

    def keys(self):
        return [round(at, 3) for at, argv in self.calls if argv == self.ESCAPE]

    def reads(self, name):
        return [round(at, 3) for at, argv in self.calls if argv == follower.xprop_get_argv(name)]

    def woken(self):
        return [line.rsplit(' ', 1)[1] for line in self.stderr.getvalue().splitlines() if 'woke Steam for app' in line]

    def logged(self, kind):
        return [line.split('dosplex-display-follower: ', 1)[1] for line in self.stderr.getvalue().splitlines()
                if line.startswith('dosplex-display-follower: ' + kind + ':')]

    def test_steam_is_woken_when_it_has_stayed_in_front_of_a_new_game_for_three_seconds(self):
        self.poll(2)                                # 0 s and 0.5 s: Steam's interface alone
        self.assertEqual(self.keys(), [])
        self.apps = [STEAM_UI, GAME_APP]            # the game appears; the poll at 1 s is the first to see it
        self.poll(12)
        self.assertEqual([argv for _, argv in self.calls if argv[0] == 'xdotool'], [self.ESCAPE])
        self.assertEqual(self.keys(), [4.0])        # three seconds after 1 s, not before
        self.assertEqual(self.woken(), ['892970'])
        # The presented app is read at every poll from the one that saw the game until the key press, and not else.
        self.assertEqual(self.reads(FOCUSED_APP), [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0])
        self.assertEqual(self.follower.watching, {})
        self.assertEqual(follower.WAKE_AFTER_SECONDS, 3.0)
        self.assertEqual(follower.WAKE_KEY, 'Escape')

    def test_steam_is_not_woken_for_a_game_that_takes_the_focus(self):
        for name, presented in (('the game', GAME_APP), ('another game', OTHER_APP), ('nothing', None)):
            with self.subTest(presented=name):
                self.start()
                self.poll(1)
                self.apps = [STEAM_UI, GAME_APP]
                self.poll(2)                         # first seen at 0.5 s, still behind Steam at 1 s
                self.focused = presented             # at 1.5 s the game is presented (or Steam shows nothing)
                self.poll(14)
                self.assertEqual(self.keys(), [])
                # Steam comes back to the front with the game still running: the wake was decided, and is not made.
                self.focused = STEAM_UI
                self.poll(14)
                self.assertEqual(self.keys(), [])
                self.assertEqual(self.woken(), [])
                self.assertEqual(self.reads(FOCUSED_APP), [0.5, 1.0, 1.5])     # and nothing is read for it any more

    def test_a_game_gets_one_key_press_while_it_stays_in_the_list_and_another_when_it_comes_back(self):
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(120)                               # a minute: Steam stays in front of the game that runs
        self.assertEqual(self.keys(), [3.5])
        self.assertEqual(self.woken(), ['892970'])
        # The game ends and is started again: a new game, and Steam is woken for it once more.
        self.apps = [STEAM_UI]
        self.poll(2)
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(14)
        self.assertEqual(self.keys(), [3.5, 64.5])
        self.assertEqual(self.woken(), ['892970', '892970'])

    def test_a_game_that_runs_when_the_follower_starts_is_not_woken_for_but_a_later_one_is(self):
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(30)
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.reads(FOCUSED_APP), [])
        self.apps = [STEAM_UI, GAME_APP, OTHER_APP]
        self.poll(10)
        self.assertEqual(self.keys(), [18.0])        # first seen at 15 s
        self.assertEqual(self.woken(), ['262830'])

    def test_a_game_that_leaves_the_list_before_the_wake_is_forgotten_and_is_new_when_it_returns(self):
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(3)                                 # seen at 0.5 s, 1 s and 1.5 s
        self.apps = [STEAM_UI]
        self.poll(2)
        self.assertEqual(self.follower.watching, {})
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(14)                                # back at 3 s: three seconds from there, not from 0.5 s
        self.assertEqual(self.keys(), [6.0])

    def test_games_that_appear_together_get_one_key_press_and_ones_that_appear_later_their_own(self):
        self.poll(1)
        self.apps = [STEAM_UI, OTHER_APP, GAME_APP]
        self.poll(10)
        self.assertEqual(self.keys(), [3.5])
        self.assertEqual(self.woken(), ['262830', '892970'])
        self.start()
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]             # seen at 0.5 s
        self.poll(2)
        self.apps = [STEAM_UI, GAME_APP, OTHER_APP]  # seen at 1.5 s
        self.poll(14)
        self.assertEqual(self.keys(), [3.5, 4.5])
        self.assertEqual(self.woken(), ['892970', '262830'])

    def test_a_key_press_that_cannot_be_made_is_logged_once_and_not_repeated(self):
        for error in (follower.ToolError('xdotool unavailable: No such file or directory'),
                      follower.ToolError('xdotool failed: Error: cannot send the key Escape'),
                      follower.DisplayUnreachable("Error: Can't open display: :99")):
            with self.subTest(error=str(error)):
                self.start()
                self.failing['key'] = error
                self.poll(1)
                self.apps = [STEAM_UI, GAME_APP]
                self.poll(40)
                self.assertEqual(self.keys(), [3.5])             # tried once, not at every poll
                self.assertEqual(self.woken(), [])               # and no claim that Steam was woken
                self.assertEqual(self.logged('wake'), ['wake: ' + type(error).__name__ + ': ' + str(error)])
                # Another game gets its own try; the same failure is not logged again.
                self.apps = [STEAM_UI, GAME_APP, OTHER_APP]
                self.poll(10)
                self.assertEqual(len(self.keys()), 2)
                self.assertEqual(len(self.logged('wake')), 1)
                # xdotool works again: the next game gets its key press and the recovery is logged.
                del self.failing['key']
                self.apps = [STEAM_UI, GAME_APP, OTHER_APP, 4711]
                self.poll(10)
                self.assertEqual(len(self.keys()), 3)
                self.assertEqual(self.woken(), ['4711'])
                self.assertEqual(self.logged('wake')[1:], ['wake: recovered'])

    def test_a_presented_app_that_cannot_be_read_waits_the_wake_and_is_logged_once(self):
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]
        self.failing['focus'] = follower.ToolError('xprop failed: broken')
        self.poll(12)                                # Steam may or may not be in front: no key press
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.logged('focus'), ['focus: ToolError: xprop failed: broken'])
        del self.failing['focus']
        self.poll(1)                                 # Steam is in front, long after the game appeared
        self.assertEqual(self.keys(), [6.5])
        self.assertEqual(self.logged('focus')[1:], ['focus: recovered'])
        self.assertEqual(self.woken(), ['892970'])

    def test_games_that_cannot_be_read_tell_nothing_and_change_nothing(self):
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(2)                                 # seen at 0.5 s
        self.failing['games'] = follower.ToolError('xprop failed: broken')
        self.assertIsNone(self.poll(4))              # 1.5 s to 3 s: unknown, so nothing is read or sent
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.logged('games'), ['games: ToolError: xprop failed: broken'])
        self.assertEqual(self.reads(FOCUSED_APP), [0.5, 1.0])
        del self.failing['games']
        self.assertEqual(self.poll(1), [GAME_APP])   # 3.5 s: the game is still the one seen at 0.5 s, now due
        self.assertEqual(self.keys(), [3.5])
        self.assertEqual(self.logged('games')[1:], ['games: recovered'])

    def test_the_games_are_the_sorted_apps_other_than_steam_or_none_when_they_cannot_be_read(self):
        self.apps = [GAME_APP, STEAM_UI, OTHER_APP]
        self.assertEqual(self.poll(1), [OTHER_APP, GAME_APP])
        self.apps = None                             # the property is not set: no game
        self.assertEqual(self.poll(1), [])
        self.failing['games'] = follower.DisplayUnreachable("xprop: unable to open display ':99'")
        self.assertIsNone(self.poll(1))
        self.assertEqual(self.follower.display_failures, 0)    # it is no check of the display, like the window fit

    def test_the_wake_reads_only_the_games_and_the_presented_app_and_sends_only_the_key(self):
        self.poll(1)
        self.apps = [STEAM_UI, GAME_APP]
        self.poll(10)
        self.assertEqual({tuple(argv) for _, argv in self.calls},
                         {tuple(follower.xprop_get_argv(FOCUSABLE)), tuple(follower.xprop_get_argv(FOCUSED_APP)),
                          tuple(self.ESCAPE)})


class UnitTests(unittest.TestCase):
    def test_gamescope_values(self):
        # GamescopeUpscaleScaler in Gamescope 3.16.30: AUTO 0, INTEGER 1, FIT 2, FILL 3, STRETCH 4.
        self.assertEqual(follower.SCALER_VALUES, {'fit': 2, 'stretch': 4, 'zoom': 2})   # Zoom keeps Gamescope at Fit
        self.assertEqual(follower.MODE_PROPERTY, 'GAMESCOPE_XWAYLAND_MODE_CONTROL')
        self.assertEqual(follower.SCALER_PROPERTY, 'GAMESCOPE_NEW_SCALING_SCALER')
        self.assertEqual(follower.mode_control_values(1920, 2160), [0, 1920, 2160, 1])
        self.assertEqual(follower.xprop_set_argv(MODE, [0, 1920, 2160, 1]),
                         ['xprop', '-root', '-f', MODE, '32c', '-set', MODE, '0, 1920, 2160, 1'])
        self.assertEqual(follower.xprop_get_argv(MODE), ['xprop', '-root', MODE])

    def test_timing_defaults(self):
        self.assertEqual(follower.POLL_SECONDS, 0.5)
        self.assertEqual(follower.APPLY_TIMEOUT_SECONDS, 10.0)
        self.assertEqual(follower.CONTROL_TIMEOUT_SECONDS, 3.0)
        self.assertEqual((follower.CONTROL_RETRIES, follower.CONTROL_RETRY_SECONDS), (3, 1.0))
        # The one-shot of the window fit: a look about every 20 ms, for at most a quarter of a second from the 1.
        self.assertEqual((follower.FIT_STEP_SECONDS, follower.FIT_SECONDS), (0.02, 0.25))
        self.assertLess(follower.FIT_SECONDS, follower.POLL_SECONDS)       # it is over long before the next poll

    def test_xrandr_and_xprop_output_as_the_real_tools_print_it(self):
        sample = ('Screen 0: minimum 16 x 16, current 3840 x 2160, maximum 32767 x 32767\n'
                  'gamescope connected 3840x2160+0+0 (normal left inverted right x axis y axis) 0mm x 0mm\n'
                  '   3840x2160     59.98*+\n')
        self.assertEqual(follower.parse_screen_size(sample), (3840, 2160))
        self.assertIsNone(follower.parse_screen_size('Can\'t open display :0\n'))
        self.assertTrue(follower.property_absent(MODE + ':  not found.\n'))
        self.assertTrue(follower.property_absent(MODE + ':  no such atom on any window.\n'))
        self.assertFalse(follower.property_absent(MODE + '(CARDINAL) = 0, 1920, 2160, 1\n'))

    def test_the_scaling_filter_is_nearest_only_for_whole_number_scales(self):
        decide = follower.scaling_filter
        self.assertEqual(decide('fit', (1920, 1080), (3840, 2160)), 1)      # Keep ratio, 2x
        self.assertEqual(decide('fit', (1920, 1080), (2160, 2160 * 9 // 16 * 2)), 0)
        self.assertEqual(decide('fit', (1920, 1080), (2160, 1215)), 0)       # 1.125x
        self.assertEqual(decide('fit', (1920, 1080), (1920, 1080)), 1)       # 1x
        self.assertEqual(decide('fit', (1920, 1080), (3840, 4320)), 1)       # 2x, bars above and below
        self.assertEqual(decide('fit', (1920, 1080), (3840, 2000)), 0)       # fit scale is 1.85
        self.assertEqual(decide('fit', (3840, 2160), (1920, 1080)), 0)       # a downscale
        self.assertEqual(decide('stretch', (1920, 1080), (3840, 2160)), 1)   # 2x / 2x
        self.assertEqual(decide('stretch', (1920, 1080), (3840, 1080)), 1)   # 2x / 1x
        self.assertEqual(decide('stretch', (1920, 1080), (3840, 1620)), 0)   # 2x / 1.5x
        self.assertEqual(decide('zoom', (1920, 1080), (3840, 2160)), 0)      # Dosplex scales a Fit
        self.assertEqual(decide('fit', (1920, 1080), (0, 0)), 0)             # no region known
        self.assertEqual(decide('stretch', (1920, 1080), (0, 2160)), 0)

    def test_live_contract_validation(self):
        good = {'x': 0, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 960, 'game_height': 1080,
                'scale_mode': 'stretch', 'display_revision': 9, 'layout': 'vertical'}
        self.assertEqual(follower.parse_live(good), follower.Contract(9, 960, 1080, 'stretch', 960, 1080))
        for field, value in (('display_revision', True), ('display_revision', 2 ** 32), ('display_revision', 1.0),
                             ('game_width', 0), ('game_width', 16385), ('game_height', None),
                             ('scale_mode', 'Fit'), ('scale_mode', ['fit']), ('scale_mode', 'fill')):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    follower.parse_live(dict(good, **{field: value}))
        for live in (None, [], 'x'):
            with self.assertRaises(ValueError):
                follower.parse_live(live)
        self.assertEqual(follower.parse_live(dict(good, game_width=16384)).width, 16384)

    def test_window_fit_properties_and_their_commands(self):
        self.assertEqual(follower.FOCUS_PROPERTY, 'GAMESCOPE_FOCUSED_WINDOW')
        self.assertEqual(follower.FORCE_FULLSCREEN_PROPERTY, 'GAMESCOPE_FORCE_WINDOWS_FULLSCREEN')
        self.assertEqual(follower.HINTS_PROPERTY, 'WM_NORMAL_HINTS')
        self.assertEqual(follower.APP_PROPERTY, 'STEAM_GAME')
        self.assertEqual(follower.FOCUS_APP_PROPERTY, 'GAMESCOPE_FOCUSED_APP_GFX')
        self.assertEqual(follower.xprop_set_argv(FORCE, [1]), ['xprop', '-root', '-f', FORCE, '32c', '-set', FORCE, '1'])
        self.assertEqual(follower.xprop_get_argv(FOCUS), ['xprop', '-root', FOCUS])
        self.assertEqual(follower.xprop_get_argv(FOCUS, FOCUS_APP), ['xprop', '-root', FOCUS, FOCUS_APP])
        self.assertEqual(follower.xprop_window_argv(GAME, 'WM_NORMAL_HINTS'),
                         ['xprop', '-id', '0x600003', 'WM_NORMAL_HINTS'])
        self.assertEqual(follower.xprop_window_argv(GAME, 'WM_NORMAL_HINTS', 'STEAM_GAME'),
                         ['xprop', '-id', '0x600003', 'WM_NORMAL_HINTS', 'STEAM_GAME'])
        self.assertEqual(follower.xdotool_geometry_argv(GAME), ['xdotool', 'getwindowgeometry', '--shell', '0x600003'])

    def test_which_windows_state_one_size(self):
        hints = follower.SizeHints
        for name, stated, expected in (
                ('no size in its hints', follower.NO_HINTS, False),
                ('no WM_NORMAL_HINTS at all (resizable, as in Gamescope get_size_hints)', None, False),
                ('a minimum equal to the maximum', hints((640, 480), (640, 480)), True),
                ('a minimum below the maximum', hints((640, 480), (1920, 1080)), False),
                ('only a minimum', hints((640, 480), None), False),
                ('only a maximum', hints(None, (640, 480)), False),
                ('one dimension fixed and the other free', hints((640, 480), (640, 1080)), False),
                ('a zero size, which Gamescope does not count', hints((0, 0), (0, 0)), False)):
            with self.subTest(name):
                self.assertIs(follower.is_fixed(stated), expected)

    def test_the_app_gamescope_names_for_its_focus_window(self):
        parse = follower.parse_focus_app
        self.assertEqual(parse(FOCUS_APP + '(CARDINAL) = 769\n'), (True, STEAM_UI))
        self.assertEqual(parse(FOCUS_APP + '(CARDINAL) = 892970\n'), (True, GAME_APP))
        self.assertEqual(parse(FOCUS_APP + '(CARDINAL) = \n'), (True, None))   # Gamescope names no app
        self.assertEqual(parse(FOCUS_APP + '(CARDINAL) = 0\n'), (True, None))
        # One xprop call prints the focus window, then its app; the input focus app is another property.
        self.assertEqual(parse(FOCUS + '(CARDINAL) = 6291459\n' + FOCUS_APP + '(CARDINAL) = 892970\n'), (True, GAME_APP))
        self.assertEqual(parse(FOCUSED_APP + '(CARDINAL) = 769\n' + FOCUS_APP + '(CARDINAL) = 892970\n'), (True, GAME_APP))
        for text in (FOCUS_APP + ':  not found.\n', FOCUS_APP + '(CARDINAL) = 1, 2\n', FOCUS_APP + '(STRING) = "x"\n',
                     FOCUSED_APP + '(CARDINAL) = 769\n', ''):
            with self.subTest(text=text):
                self.assertEqual(parse(text), (False, None))                     # absent: the window's STEAM_GAME is read

    def test_the_forced_fullscreen_as_gamescope_reads_it(self):
        parse = follower.parse_force
        self.assertEqual(parse(FORCE + '(CARDINAL) = 1\n'), 1)
        self.assertEqual(parse(FORCE + '(CARDINAL) = 7\n'), 1)                  # !!get_prop: any non-zero first value
        self.assertEqual(parse(FORCE + '(CARDINAL) = 0\n'), 0)
        self.assertEqual(parse(FORCE + '(CARDINAL) = 0, 1\n'), 0)               # only the first value counts
        self.assertEqual(parse(FORCE + '(CARDINAL) = \n'), 0)
        self.assertEqual(parse(FORCE + ':  not found.\n'), 0)

    def test_what_the_fit_looked_at_as_the_log_says_it(self):
        hints = follower.SizeHints
        self.assertEqual(follower.describe_window(GAME, GAME_APP, None), 'window 0x600003, app 892970, no WM_NORMAL_HINTS')
        self.assertEqual(follower.describe_window(GAME, GAME_APP, follower.NO_HINTS), 'window 0x600003, app 892970, no size hints')
        self.assertEqual(follower.describe_window(GAME, GAME_APP, hints((640, 480), (640, 480))),
                         'window 0x600003, app 892970, minimum 640x480, maximum 640x480')
        self.assertEqual(follower.describe_window(GAME, GAME_APP, hints(None, (1920, 1080)), (1280, 720), (1920, 2160)),
                         'window 0x600003, app 892970, maximum 1920x1080, 1280x720, screen 1920x2160')

    def test_a_windows_app_id_and_size_as_the_tools_print_them(self):
        parse = follower.parse_window_app
        self.assertEqual(parse('STEAM_GAME(CARDINAL) = 769\n'), STEAM_UI)
        self.assertEqual(parse(hints_text((640, 480), (640, 480)) + 'STEAM_GAME(CARDINAL) = 892970\n'), GAME_APP)
        for text in ('STEAM_GAME:  not found.\n', 'STEAM_GAME(CARDINAL) = \n', 'STEAM_GAME(CARDINAL) = 1, 2\n',
                     hints_text(), '', 'garbage'):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))
        geometry = follower.parse_window_geometry
        self.assertEqual(geometry('WINDOW=6291459\nX=0\nY=0\nWIDTH=1920\nHEIGHT=2160\nSCREEN=0\n'), (1920, 2160))
        for text in ('WINDOW=6291459\nX=0\nY=0\nWIDTH=1920\nSCREEN=0\n', 'WIDTH=1920 HEIGHT=2160\n', '', 'garbage'):
            with self.subTest(text=text):
                self.assertIsNone(geometry(text))

    def test_size_hints_as_xprop_prints_them(self):
        # The wording of the real tool (xprop 1.2.6): one line for each size that the window states.
        fixed = ('WM_NORMAL_HINTS(WM_SIZE_HINTS):\n\t\tprogram specified minimum size: 640 by 480\n'
                 '\t\tprogram specified maximum size: 640 by 480\n')
        self.assertEqual(follower.parse_size_hints(fixed), follower.SizeHints((640, 480), (640, 480)))
        self.assertEqual(follower.parse_size_hints(hints_text((640, 480), (1920, 1080))),
                         follower.SizeHints((640, 480), (1920, 1080)))
        self.assertEqual(follower.parse_size_hints(hints_text((640, 480), None)), follower.SizeHints((640, 480), None))
        self.assertEqual(follower.parse_size_hints(hints_text(None, (800, 600))), follower.SizeHints(None, (800, 600)))
        # Size, increment, aspect, base size and gravity say nothing about a range.
        other = ('WM_NORMAL_HINTS(WM_SIZE_HINTS):\n\t\tprogram specified location: 10, 20\n'
                 '\t\tuser specified size: 1920 by 2160\n\t\tprogram specified size: 1920 by 2160\n'
                 '\t\tprogram specified resize increment: 7 by 13\n\t\tprogram specified base size: 4 by 4\n'
                 '\t\tprogram specified minimum aspect ratio: 4/3\n\t\twindow gravity: NorthWest\n')
        self.assertEqual(follower.parse_size_hints(other), follower.NO_HINTS)
        # Other properties on the same output are not this one's.
        self.assertEqual(follower.parse_size_hints(fixed + 'STEAM_GAME(CARDINAL) = 892970\n'), follower.SizeHints((640, 480), (640, 480)))
        # A window that has not published the property states nothing yet: that is not the same as no sizes.
        for text in ('WM_NORMAL_HINTS:  not found.\n', 'WM_NORMAL_HINTS:  no such atom on any window.\n',
                     'STEAM_GAME(CARDINAL) = 892970\n', '', 'garbage'):
            with self.subTest(text=text):
                self.assertIsNone(follower.parse_size_hints(text))

    def test_the_focused_window_as_xprop_prints_it(self):
        parse = follower.parse_focused_window
        self.assertEqual(parse(FOCUS + '(CARDINAL) = 6291459\n'), GAME)
        self.assertEqual(parse(FOCUS + '(WINDOW): window id # 0x600003\n'), GAME)
        self.assertEqual(parse(FOCUS + '(CARDINAL) = 4294967295\n'), 4294967295)
        # Nothing focused: Gamescope leaves the property with no items, or it was never set.
        for text in (FOCUS + '(CARDINAL) = \n', FOCUS + ':  not found.\n', FOCUS + ':  no such atom on any window.\n',
                     FOCUS + '(CARDINAL) = 0\n', FOCUS + '(CARDINAL) = 4294967296\n', FOCUS + '(CARDINAL) = 1, 2\n',
                     FOCUS + '(CARDINAL) = -5\n', 'GAMESCOPE_FOCUSED_APP(CARDINAL) = 769\n', '', 'garbage'):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_the_games_as_xprop_prints_them(self):
        games = follower.running_games
        # Steam's own interface (769) is no game; any other app id is one. The list comes out sorted and each id once.
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 769, 892970\n'), [GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 892970\n'), [GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 892970, 769, 262830\n'), [OTHER_APP, GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 892970, 892970\n'), [GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 769,892970\n'), [GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 4294967295\n'), [4294967295])
        # xprop prints the properties it is asked for one to a line; the others on the output are not this one's.
        self.assertEqual(games(FOCUSED_APP + '(CARDINAL) = 769\n' + FOCUSABLE + '(CARDINAL) = 769, 892970\n'), [GAME_APP])
        self.assertEqual(games(FOCUSABLE + '(CARDINAL) = 769, 892970\n' + FOCUSED_APP + '(CARDINAL) = 262830\n'), [GAME_APP])
        # Only Steam, no items and no property at all: no game.
        for text in (FOCUSABLE + '(CARDINAL) = 769\n', FOCUSABLE + '(CARDINAL) = \n', FOCUSABLE + '(CARDINAL) =\n',
                     FOCUSABLE + ':  not found.\n', FOCUSABLE + ':  no such atom on any window.\n', '',
                     FOCUSED_APP + '(CARDINAL) = 892970\n'):
            with self.subTest(text=text):
                self.assertEqual(games(text), [])
        # A property that is malformed names no game, whatever else its line holds.
        for text in (FOCUSABLE + '(CARDINAL) = 769, soon\n', FOCUSABLE + '(CARDINAL) = 769 892970\n',
                     FOCUSABLE + '(CARDINAL) = 769, 892970,\n', FOCUSABLE + '(CARDINAL) = ,892970\n',
                     FOCUSABLE + '(CARDINAL) = -5\n', FOCUSABLE + '(CARDINAL) = 4294967296\n',
                     FOCUSABLE + '(CARDINAL) = 769, 4294967296\n', FOCUSABLE + '(CARDINAL) = 0x10\n',
                     FOCUSABLE + '(CARDINAL) = 1.5\n', FOCUSABLE + '(STRING) = "892970"\n',
                     FOCUSABLE + '(WINDOW): window id # 0x600003\n', ' ' + FOCUSABLE + '(CARDINAL) = 892970\n',
                     'XGAMESCOPE_FOCUSABLE_APPS(CARDINAL) = 892970\n', 'garbage'):
            with self.subTest(text=text):
                self.assertEqual(games(text), [])

    def test_the_focused_app_as_xprop_prints_it(self):
        parse = follower.parse_focused_app
        self.assertEqual(parse(FOCUSED_APP + '(CARDINAL) = 769\n'), STEAM_UI)
        self.assertEqual(parse(FOCUSED_APP + '(CARDINAL) = 892970\n'), GAME_APP)
        self.assertEqual(parse(FOCUSED_APP + '(CARDINAL) = 4294967295\n'), 4294967295)
        self.assertEqual(parse(FOCUSABLE + '(CARDINAL) = 892970\n' + FOCUSED_APP + '(CARDINAL) = 769\n'), STEAM_UI)
        # Nothing presented: the property is absent or empty. Several apps, a number out of range and other types
        # name no app either.
        for text in (FOCUSED_APP + ':  not found.\n', FOCUSED_APP + ':  no such atom on any window.\n',
                     FOCUSED_APP + '(CARDINAL) = \n', FOCUSED_APP + '(CARDINAL) = 769, 892970\n',
                     FOCUSED_APP + '(CARDINAL) = 4294967296\n', FOCUSED_APP + '(CARDINAL) = -1\n',
                     FOCUSED_APP + '(STRING) = "769"\n', FOCUSABLE + '(CARDINAL) = 769\n', '', 'garbage'):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_cardinal_lists_as_xprop_prints_them(self):
        parse = follower.parse_cardinals
        self.assertEqual(parse('NAME(CARDINAL) = 1, 22, 333\n', 'NAME'), [1, 22, 333])
        self.assertEqual(parse('NAME(CARDINAL) = \n', 'NAME'), [])
        self.assertEqual(parse('OTHER(CARDINAL) = 5\nNAME(CARDINAL) = 7\n', 'NAME'), [7])
        # The name is matched as text, not as a pattern.
        self.assertIsNone(parse('NAMES(CARDINAL) = 7\n', 'NAME'))
        self.assertIsNone(parse('NAME(CARDINAL) = 7\n', 'NA.E'))
        self.assertIsNone(parse('NAME:  not found.\n', 'NAME'))

    def test_games_are_named_for_a_log_line_and_a_report(self):
        self.assertEqual(follower.describe_games([GAME_APP]), 'game 892970')
        self.assertEqual(follower.describe_games([OTHER_APP, GAME_APP]), 'games 262830, 892970')
        # With many games the first few are named, so a report's detail still ends with the sizes.
        self.assertEqual(follower.MAX_LISTED_GAMES, 4)
        self.assertEqual(follower.describe_games([1, 2, 3, 4]), 'games 1, 2, 3, 4')
        self.assertEqual(follower.describe_games([1, 2, 3, 4, 5, 6]), 'games 1, 2, 3, 4, ...')

    def test_game_detection_names(self):
        self.assertEqual(follower.FOCUSABLE_APPS_PROPERTY, 'GAMESCOPE_FOCUSABLE_APPS')
        self.assertEqual(follower.FOCUSED_APP_PROPERTY, 'GAMESCOPE_FOCUSED_APP')
        self.assertEqual(follower.STEAM_APP_ID, 769)
        self.assertEqual(follower.xprop_get_argv(FOCUSABLE), ['xprop', '-root', 'GAMESCOPE_FOCUSABLE_APPS'])


class LauncherTests(unittest.TestCase):
    """The real launcher, with Gamescope and Steam replaced by stubs that record what they are given."""
    CHILD = ['steam', '-gamepadui', '-nocrashdialog', '-nodumpreport', '-steamos3', '-steampal', '-steamdeck']

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-follower-launch-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        python = '#!' + sys.executable + '\n'
        stubs = {
            # Gamescope runs its child with its own DISPLAY and, like gamescopereaper, ends whatever the child left behind.
            'gamescope': python + '''import json, os, signal, subprocess, sys
argv = sys.argv[1:]
child = argv[argv.index('--') + 1:]
process = subprocess.Popen(child, env=dict(os.environ, DISPLAY=':77'), start_new_session=True)
with open(os.environ['CAPTURE'], 'w') as stream:
    json.dump({'argv': argv, 'child': child, 'child_pid': process.pid}, stream)
status = process.wait(timeout=30)
try:
    os.killpg(process.pid, signal.SIGTERM)
except ProcessLookupError:
    pass
sys.exit(status)
''',
            'steam': python + '''import json, os, sys, time
if os.environ.get('EXPECT_FOLLOWER'):
    end = time.time() + 15
    while time.time() < end and not os.path.exists(os.environ['FOLLOWER_LOG']):
        time.sleep(0.02)
with open(os.environ['STEAM_LOG'], 'w') as stream:
    json.dump({'pid': os.getpid(), 'argv': sys.argv[1:], 'display': os.environ.get('DISPLAY')}, stream)
''',
            'dbus-run-session': '#!/bin/sh\nshift\nexec "$@"\n',
        }
        for name, body in stubs.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        for name in ('mkdir', 'touch', 'date', 'id', 'bash', 'dirname'):
            (self.bin / name).symlink_to('/usr/bin/' + name)
        # A libexec directory like the installed one: the launcher finds its helpers next to the shim script.
        self.libexec = self.root / 'libexec'
        self.libexec.mkdir()
        (self.libexec / 'steamos-session-select-shim.sh').write_text('#!/bin/sh\n')
        self.follower_stub = self.libexec / 'dosplex-side-display-follower.py'
        self.follower_stub.write_text('''import json, os, signal, sys, time

def terminated(*_):
    open(os.environ['FOLLOWER_LOG'] + '.terminated', 'w').close()
    os._exit(0)

signal.signal(signal.SIGTERM, terminated)
with open(os.environ['FOLLOWER_LOG'], 'a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'ppid': os.getppid(), 'display': os.environ.get('DISPLAY'),
                             'argv': sys.argv[1:]}) + '\\n')
time.sleep(30)
''')

    def launch(self, *, payload='steam', side='left', role='primary', control=True, follower=True):
        if not follower:
            self.follower_stub.unlink()
        env = {
            'PATH': str(self.bin), 'HOME': str(self.root), 'CAPTURE': str(self.root / 'gamescope.json'),
            'STEAM_LOG': str(self.root / 'steam.json'), 'FOLLOWER_LOG': str(self.root / 'follower.log'),
            'DOSPLEX_LAUNCH_LOG': str(self.root / 'launch.log'), 'DOSPLEX_SESSION_PAYLOAD': payload,
            'DOSPLEX_SIDE_ROLE': role, 'DOSPLEX_OUTPUT_WIDTH': '960', 'DOSPLEX_OUTPUT_HEIGHT': '1080',
            'DOSPLEX_GAME_WIDTH': '960', 'DOSPLEX_GAME_HEIGHT': '1080', 'DOSPLEX_SCALE_MODE': 'fit',
        }
        if control:
            env.update(EXPECT_FOLLOWER='1' if follower else '',
                       DOSPLEX_SHIM_SCRIPT=str(self.libexec / 'steamos-session-select-shim.sh'),
                       DOSPLEX_SESSION_CONTROL=str(self.root / 'control.sock'), DOSPLEX_SESSION_INSTANCE=INSTANCE)
        result = subprocess.run(['bash', str(LAUNCHER), side, 'tester'], env=env, text=True, capture_output=True,
                                timeout=60)
        return result

    def read(self, name):
        path = self.root / name
        return json.loads(path.read_text()) if path.exists() else None

    def follower_starts(self):
        path = self.root / 'follower.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_the_gaming_command_starts_the_follower_once_beside_steam(self):
        for side, role in (('left', 'primary'), ('right', 'guest')):
            with self.subTest(side=side, role=role):
                self.clear_records()
                result = self.launch(side=side, role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                capture, steam, starts = self.read('gamescope.json'), self.read('steam.json'), self.follower_starts()
                # Gamescope's child is a shell that runs the follower and then becomes Steam.
                self.assertEqual(capture['child'][:2], ['/bin/sh', '-c'])
                self.assertEqual(capture['child'][3:5], ['dosplex-display-follower', str(self.follower_stub)])
                steam_args = ['-gamepadui', '-nocrashdialog', '-nodumpreport', '-steamos3', '-steampal', '-steamdeck']
                self.assertEqual(capture['child'][5:], ['steam'] + steam_args)
                self.assertEqual(steam['argv'], steam_args)
                self.assertEqual(capture['argv'].count('--'), 1)
                self.assertEqual(capture['argv'][capture['argv'].index('--') - 1], '--force-composition')
                # Started exactly once, with Gamescope's DISPLAY, as a child of Steam, which is still the primary child.
                self.assertEqual(len(starts), 1, starts)
                self.assertEqual(starts[0]['display'], ':77')
                self.assertEqual(starts[0]['argv'], [])
                self.assertEqual(steam['pid'], capture['child_pid'])
                self.assertEqual(starts[0]['ppid'], steam['pid'])
                self.assertEqual(steam['display'], ':77')
                # It goes away with the session.
                self.assertTrue(wait_for(lambda: (self.root / 'follower.log.terminated').exists()))

    def test_the_nested_gamescope_hides_its_cursor_after_three_seconds(self):
        for side, role in (('left', 'primary'), ('right', 'guest')):
            with self.subTest(side=side, role=role):
                self.clear_records()
                result = self.launch(side=side, role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = self.read('gamescope.json')['argv'][:self.read('gamescope.json')['argv'].index('--')]
                self.assertEqual(argv.count('--hide-cursor-delay'), 1, argv)
                self.assertEqual(argv[argv.index('--hide-cursor-delay') + 1], '3000')

    def clear_records(self):
        for name in ('gamescope.json', 'steam.json', 'follower.log', 'follower.log.terminated', 'launch.log'):
            (self.root / name).unlink(missing_ok=True)

    def test_without_a_control_socket_or_without_the_follower_the_command_is_unchanged(self):
        result = self.launch(control=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read('gamescope.json')['child'], self.CHILD)
        self.assertEqual(self.follower_starts(), [])
        self.clear_records()
        result = self.launch(follower=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read('gamescope.json')['child'], self.CHILD)
        self.assertEqual(self.follower_starts(), [])
        self.assertIn('display follower missing', result.stderr)

    def test_the_desktop_payload_has_no_follower(self):
        result = self.launch(payload='desktop')
        self.assertNotEqual(result.returncode, 0)          # no nested Plasma on a test host
        self.assertIsNone(self.read('gamescope.json'))
        self.assertEqual(self.follower_starts(), [])
        text = LAUNCHER.read_text()

        def body(name):
            match = re.search(r'^' + name + r'\(\) \{\n(.*?)^\}\n', text, re.M | re.S)
            self.assertTrue(match, name)
            return match.group(1)
        self.assertIn('dosplex-side-display-follower.py', body('launch_steam_session'))
        for name in ('launch_desktop_session', 'launch_wayland_terminal', 'launch_xterm_terminal'):
            self.assertNotIn('follower', body(name), name)
        # One place starts it, in the gaming command only.
        self.assertEqual(text.count('dosplex-side-display-follower.py'), 1)
        self.assertEqual(text.count('dosplex-display-follower'), 1)


if __name__ == '__main__':
    unittest.main()
