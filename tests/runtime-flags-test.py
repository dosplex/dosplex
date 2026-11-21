#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,wayland-info,wayland-client,cc,ordinary-user timeout=240
# dosplex-run: default {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The compositor's runtime flags gate exactly what they claim.

The backend never implies a policy: every behaviour below is off until the launcher
passes its flag. Each case starts the real compositor (headless, as an ordinary
user, in a private runtime directory) with and without the flag and watches the
effect from outside:

  --test-hooks      DEV_FRAME, DEV_OUTPUT_*, DEV_SIDE_SCALE, DEV_SIDE_DISPLAY, DEV_STATS and DEV_SCENE commands, the R
                    relative motion command of the input socket, and the screen-copy protocol
  --allow-power     the Sleep, Restart and Shut down confirmations can be continued (a
                    recording stand-in for systemctl is the only program on PATH); Restart
                    and Shut down run only through the bundled power helper
  --boots-into      what the confirmations say the machine boots into (steamos or dosplex);
                    unsaid, they say nothing about it
  DOSPLEX_POWER_COMMAND  with --test-hooks, the power helper runs it instead of systemctl;
                    without --test-hooks it is ignored
  DOSPLEX_POWER_PANEL_TIMEOUT_MS  with --test-hooks, shortens the time after which the Restart and
                    Shut down progress panel comes down when Dosplex was not stopped
  --idle-policy     none never blanks the output; dim does, and restores it on input
  --audio-router    audio exists only where a router is configured; the option wins
                    over DOSPLEX_AUDIO_ROUTER and an empty value turns audio off
  argument errors   rejected before anything starts

-s (real side sessions require root; the screen-copy protocol is never offered with
them) needs a user namespace and is covered by nested-session-mode-test.py.
--host-keyboards only changes how keyboards that are neither libinput nor X11
devices count as menu controls. Stock inputs cannot create such a keyboard, so this
test only checks that the option is read.
"""
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = None  # set from the command line at the bottom

# The compositor runs $DOSPLEX_ROOT/libexec/dosplex-power-action. Every run points DOSPLEX_ROOT at its
# own bundle directory, which holds this helper only when the case asks for it.
HELPER = Path(__file__).resolve().parents[1] / 'packaging/steamos/dosplex-power-action'


# --idle-policy=dim also lowers this panel backlight; keep it away from a machine that has one.
BACKLIGHT = Path('/sys/class/backlight/amdgpu_bl0/brightness')

# Overlay geometry for the single 1280x720 region (scale 1): a 720x540 panel centred in
# the region, content starting 112 below its top, buttons at fixed rows. These are the
# same hit targets the other headless tests click.
PANEL_X, PANEL_Y, PANEL_W = 280, 90, 720
CONTENT_X, CONTENT_Y, CONTENT_W = PANEL_X + 16, PANEL_Y + 112, PANEL_W - 32
TAB_W, THIRD, HALF = (CONTENT_W - 24) // 4, (CONTENT_W - 16) // 3, (CONTENT_W - 8) // 2


def centre(x, y, w, h):
    return x + w // 2, y + h // 2


AUDIO_TAB = centre(CONTENT_X + 2 * (TAB_W + 8), PANEL_Y + 70, TAB_W, 30)
SESSION_TAB = centre(CONTENT_X + 3 * (TAB_W + 8), PANEL_Y + 70, TAB_W, 30)
SLEEP = centre(CONTENT_X, CONTENT_Y + 136, THIRD, 32)
RESTART = centre(CONTENT_X + THIRD + 8, CONTENT_Y + 136, THIRD, 32)
SHUT_DOWN = centre(CONTENT_X + 2 * (THIRD + 8), CONTENT_Y + 136, THIRD, 32)
TURN_OFF = centre(CONTENT_X, CONTENT_Y + 210, CONTENT_W, 30)    # Turn off Dosplex: a row of its own below Reset
CONTINUE = centre(CONTENT_X + HALF + 8, CONTENT_Y + 88, HALF, 34)

POLICY_DEFAULTS = {'backend': 'headless', 'real_sessions': '0', 'test_hooks': '0', 'allow_power': '0',
                   'idle_policy': 'none', 'host_keyboards': '0', 'power_command': 'none', 'boots_into': 'unknown',
                   'audio_router': 'none'}
RECORD_POWER = {'systemctl': 'echo "$@" >> "$POWER_LOG"\n'}
# The helper leaves the compositor's service through systemd-run before a restart; this stand-in records
# the call and runs the command in place.
FAKE_SYSTEMD_RUN = {'systemd-run': 'echo "$@" >> "$UNIT_LOG"\nwhile [ "$1" != "--" ]; do shift; done\nshift\n'
                                   'exec "$@"\n'}
RECORD_AUDIO = {'python3': 'echo "$@" >> "$AUDIO_LOG"\nexit 1\n'}
# The escaped unit of Restart that fails some time after it started, without having stopped anything. The
# helper waits for its unit (systemd-run --wait), so this is what the first stage then exits with.
FAILING_SYSTEMD_RUN = {'systemd-run': 'echo "$@" >> "$UNIT_LOG"\nsleep 3\nexit 1\n',
                       'sleep': 'exec /usr/bin/sleep "$@"\n'}


def build_probe(directory):
    """tools/dosplex-keyprobe.c, the Wayland client that prints every input event it gets (as in
    headless-integration.py)."""
    directory = Path(directory)
    project = Path(__file__).resolve().parents[1]
    protocols = subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip()
    generated = []
    for xml, header, code in [
            ('stable/xdg-shell/xdg-shell.xml', 'xdg-shell-client-protocol.h', 'xdg-shell-protocol.c'),
            ('unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
             'pointer-constraints-unstable-v1-client-protocol.h', 'pointer-constraints-protocol.c'),
            ('unstable/relative-pointer/relative-pointer-unstable-v1.xml',
             'relative-pointer-unstable-v1-client-protocol.h', 'relative-pointer-protocol.c')]:
        for mode, filename in [('client-header', header), ('private-code', code)]:
            subprocess.run(['wayland-scanner', mode, str(Path(protocols) / xml), str(directory / filename)],
                           check=True, timeout=5)
        generated.append(str(directory / code))
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
    subprocess.run(['cc', '-I', str(directory), str(project / 'tools/dosplex-keyprobe.c'), *generated,
                    '-o', str(directory / 'probe'), *flags], check=True, timeout=30)
    return directory / 'probe'


class KeyProbe:
    """tools/dosplex-keyprobe.c as the client of one of the compositor's sides. Its log is what its seat was sent:
    KEYBOARD_ENTER and KEYBOARD_LEAVE for the keyboard focus, KEY for every key it is given."""

    def __init__(self, run, binary):
        self.run = run
        self.output = (run.root / 'probe.log').open('w+')
        env = {'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': str(run.root), 'WAYLAND_DISPLAY': 'dosplex-0'}
        self.process = subprocess.Popen([str(binary)], env=env, stdout=self.output, stderr=self.output)

    def events(self, since=0):
        self.output.flush()
        return (self.run.root / 'probe.log').read_text()[since:]

    def count(self, kind, since=0):
        return len(re.findall('^' + kind, self.events(since), re.M))

    def saw(self, since, pattern, timeout=3):
        deadline = time.monotonic() + timeout
        while not re.search(pattern, self.events(since), re.M):
            assert self.process.poll() is None, 'the probe exited'
            if time.monotonic() > deadline:
                raise AssertionError(f'the probe never saw {pattern!r}:\n{self.events(since)[-2000:]}')
            time.sleep(.02)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.output.close()


class Run:
    """One disposable headless compositor with its own runtime directory."""

    def __init__(self, flags=(), env=None, tools=None, helper=False, recorder=False):
        self.flags = list(flags)
        self.extra_env = dict(env or {})
        self.tools = dict(tools or {})  # name -> shell script text; the only programs on PATH
        self.helper = helper            # install the power helper in this run's bundle
        self.recorder = recorder        # point DOSPLEX_POWER_COMMAND at a recorder (command.log)

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dosplex-flags-test-')
        self.root = Path(self.directory.name)
        bin_dir = self.root / 'fakebin'
        bin_dir.mkdir()
        for name, text in self.tools.items():
            (bin_dir / name).write_text('#!/bin/sh\n' + text)
            (bin_dir / name).chmod(0o755)
        bundle = self.root / 'bundle'
        (bundle / 'libexec').mkdir(parents=True)
        if self.helper:
            shutil.copyfile(HELPER, bundle / 'libexec/dosplex-power-action')
            (bundle / 'libexec/dosplex-power-action').chmod(0o755)
        power_command = {}
        if self.recorder:
            (self.root / 'power-command').write_text('#!/bin/sh\necho "$@" >> "$COMMAND_LOG"\n')
            (self.root / 'power-command').chmod(0o755)
            power_command = {'DOSPLEX_POWER_COMMAND': str(self.root / 'power-command')}
        self.env = {'PATH': str(bin_dir), 'HOME': str(self.root), 'XDG_RUNTIME_DIR': str(self.root),
                    'WAYLAND_DISPLAY': 'dosplex-0', 'WLR_RENDERER': 'pixman', 'DOSPLEX_WAYLAND_DEBUG': '0',
                    'DOSPLEX_LEFT_USER': pwd.getpwuid(os.getuid()).pw_name,
                    'DOSPLEX_RIGHT_USER': 'dosplex-flags-test-absent',
                    'DOSPLEX_AUTH_HELPER': '/bin/false', 'DOSPLEX_SESSION_SCRIPT': '/bin/false',
                    'POWER_LOG': str(self.root / 'power.log'), 'AUDIO_LOG': str(self.root / 'audio.log'),
                    'COMMAND_LOG': str(self.root / 'command.log'), 'UNIT_LOG': str(self.root / 'unit.log'),
                    'DOSPLEX_ROOT': str(bundle), **power_command, **self.extra_env}
        self.log = (self.root / 'compositor.log').open('w+')
        self.started = time.monotonic()
        self.proc = subprocess.Popen([BINARY, '-b', 'headless', *self.flags], env=self.env,
                                     stdout=self.log, stderr=self.log, start_new_session=True)
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.pointer = None
        try:
            self.wait_for(lambda s: s is not None, 'the first state file', timeout=5)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
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

    def wait_for(self, predicate, label, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            current = self.state()
            if predicate(current):
                return current
            if time.monotonic() > deadline:
                raise AssertionError(f'{label}: {current}\n{self.log_text()[-3000:]}')
            time.sleep(.02)

    def wait_for_log(self, text, timeout=4):
        deadline = time.monotonic() + timeout
        while text not in self.log_text():
            self.state()  # fails fast when the compositor died
            if time.monotonic() > deadline:
                raise AssertionError(f'the log never said {text!r}:\n{self.log_text()[-3000:]}')
            time.sleep(.02)

    def stays(self, predicate, label, seconds=1.0):
        """The state satisfies `predicate` for the whole window (a hook that did nothing)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            current = self.state()
            assert current is not None and predicate(current), f'{label}: {current}\n{self.log_text()[-3000:]}'
            time.sleep(.05)

    def ipc(self, command):
        self.control.sendto(command.encode(), str(self.root / 'hud-ipc.sock'))

    def send_pointer(self, text):
        if self.pointer is None:
            self.pointer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.pointer.connect(str(self.root / 'dosplex-primary-input.sock'))
        self.pointer.sendall(text.encode())
        time.sleep(.12)

    def click(self, point):
        self.send_pointer(f'M {point[0]} {point[1]}\nB 272 1\nB 272 0\n')

    def click_until(self, point, predicate, label, attempts=6):
        """Click until the state shows the effect; a click before the first frame lands on nothing."""
        for _ in range(attempts):
            self.click(point)
            try:
                return self.wait_for(predicate, label, timeout=.4)
            except AssertionError:
                pass
        raise AssertionError(f'{label}: {self.state()}\n{self.log_text()[-3000:]}')

    def policy(self):
        match = re.search(r'Runtime policy: (.*)$', self.log_text(), re.MULTILINE)
        assert match, self.log_text()
        # The audio router path comes last, so it may contain spaces.
        return dict(item.split('=', 1) for item in match.group(1).split(' ', 8))

    def globals(self):
        env = {'PATH': os.environ['PATH'], 'XDG_RUNTIME_DIR': str(self.root), 'WAYLAND_DISPLAY': 'dosplex-0'}
        return subprocess.check_output(['wayland-info'], env=env, text=True,
                                       stderr=subprocess.DEVNULL, timeout=5)

    def recorded(self, name):
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def open_overlay_page(self, tab_point, tab):
        self.ipc('OVERLAY left')
        self.wait_for(lambda s: s['overlay_visible'], 'the Main overlay opens')
        self.click_until(tab_point, lambda s: s['overlay_tab'] == tab, f'overlay tab {tab} opens')

    def open_sleep_confirmation(self):
        self.open_power_confirmation(SLEEP, 'Sleep')

    def open_power_confirmation(self, button, label):
        self.open_overlay_page(SESSION_TAB, 2)
        self.click_until(button, lambda s: s['overlay_confirmation'], f'{label} asks for confirmation')
        time.sleep(.4)  # let the confirmation dialog draw its buttons


def run_binary(*args):
    """Run the compositor with arguments that must end it at once; report what it left behind."""
    with tempfile.TemporaryDirectory(prefix='dosplex-flags-args-') as directory:
        result = subprocess.run([BINARY, *args], capture_output=True, text=True, timeout=10,
                                env={'PATH': '/usr/bin:/bin', 'HOME': directory, 'XDG_RUNTIME_DIR': directory})
        leftovers = sorted(os.listdir(directory))
    return result, leftovers


class RuntimeFlagTests(unittest.TestCase):
    def test_help_lists_every_flag_with_its_default(self):
        result, _ = run_binary('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ('-b backend', '-s ', '--test-hooks', '--allow-power', '--boots-into=BOOT', 'steamos', 'dosplex',
                     '--idle-policy=POLICY', 'none (default)',
                     '--host-keyboards', '--audio-router=PATH', 'DOSPLEX_AUDIO_ROUTER', '(default off', '--help'):
            self.assertIn(text, result.stdout)
        self.assertEqual(result.stderr, '')

    def test_bad_arguments_end_the_process_before_anything_starts(self):
        for args, message in ((['--idle-policy=sometimes'], 'idle-policy'), (['--idle-policy'], 'idle-policy'),
                              (['--boots-into=windows'], 'boots-into'), (['--boots-into'], 'boots-into'),
                              (['--boots-into='], 'boots-into'),
                              (['--audio-router'], 'audio-router'), (['-b'], "'b'"),
                              (['--no-such-flag'], 'no-such-flag'), (['-x'], "'x'")):
            with self.subTest(args=args):
                result, leftovers = run_binary(*args)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(leftovers, [], 'a refused start must not create sockets or files')
        result, _ = run_binary('-b', 'no-such-backend')
        self.assertEqual(result.returncode, 1)
        self.assertIn('no-such-backend', result.stderr)

    def test_every_flag_is_read(self):
        flags = ['--test-hooks', '--allow-power', '--host-keyboards', '--boots-into=steamos',
                 '--audio-router=/opt/audio router']
        expected = dict(POLICY_DEFAULTS, test_hooks='1', allow_power='1', host_keyboards='1', boots_into='steamos',
                        audio_router='/opt/audio router')
        if not BACKLIGHT.exists():
            flags.append('--idle-policy=dim')
            expected['idle_policy'] = 'dim'
        with Run(flags) as run:
            self.assertEqual(run.policy(), expected)
        with Run(env={'DOSPLEX_AUDIO_ROUTER': '/opt/router-from-environment'}) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, audio_router='/opt/router-from-environment'))
        with Run(['--boots-into=dosplex']) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, boots_into='dosplex'))

    def test_without_flags_every_policy_is_off(self):
        # DOSPLEX_IDLE_DIM_TIMEOUT_MS only makes the idle check short; the policy decides.
        with Run(env={'DOSPLEX_IDLE_DIM_TIMEOUT_MS': '1000'}, tools={**RECORD_POWER, **RECORD_AUDIO}) as run:
            self.assertEqual(run.policy(), POLICY_DEFAULTS)

            # Test hooks are refused, and the log says why instead of leaving a tool to time out.
            run.ipc('DEV_FRAME 42')
            run.ipc('DEV_OUTPUT_REMOVE')
            run.ipc('DEV_SIDE_DISPLAY left 0 3840 2160 deferred')
            run.ipc('DEV_STATS')
            run.ipc('DEV_SCENE left')
            run.stays(lambda s: s['capture_ready'] == 0 and s['output_connected'] and
                      s['left']['display_result'] == '' and not s['left']['display_scaled'],
                      'a test hook acted without --test-hooks')
            for command in ('DEV_FRAME', 'DEV_OUTPUT_REMOVE', 'DEV_SIDE_DISPLAY', 'DEV_STATS', 'DEV_SCENE'):
                self.assertIn(f'Ignoring {command}: the compositor was not started with --test-hooks', run.log_text())
            self.assertNotIn('DEV_STATS frames=', run.log_text())
            self.assertNotIn('DEV_SCENE left end', run.log_text())
            # So is the input socket's relative motion: refused with a reason, and the connection stays.
            run.send_pointer('R 5 5\n')
            self.assertIn('Ignoring R: the compositor was not started with --test-hooks', run.log_text())
            run.send_pointer('M 10 10\n')
            self.assertNotIn('input connection closed', run.log_text())
            if shutil.which('wayland-info'):
                self.assertNotIn('zwlr_screencopy_manager_v1', run.globals())

            # Audio has no router to run.
            run.open_overlay_page(AUDIO_TAB, 3)
            run.stays(lambda s: s['audio_revision'] == 0 and not s['audio_busy'], 'audio ran without a router')
            self.assertEqual(run.recorded('audio.log'), [], 'an audio helper was started without a router')
            run.ipc('OVERLAY close')

            # Machine power: the confirmation opens, but there is no Continue to press.
            run.open_sleep_confirmation()
            run.click(CONTINUE)
            time.sleep(.3)
            run.click(CONTINUE)
            run.stays(lambda s: s['overlay_visible'] and s['overlay_confirmation'],
                      'Sleep went through without --allow-power')
            self.assertEqual(run.recorded('power.log'), [], 'a power action ran without --allow-power')
            run.ipc('OVERLAY close')
            run.wait_for(lambda s: not s['overlay_visible'], 'the overlay closes')
            # CHORD is a test hook as well.
            run.ipc('CHORD left')
            run.stays(lambda s: not s['overlay_visible'], 'CHORD acted without --test-hooks', seconds=.5)
            self.assertIn('Ignoring CHORD: the compositor was not started with --test-hooks', run.log_text())

            # The idle timer would have fired by now under the dim policy.
            time.sleep(max(0, 3.2 - (time.monotonic() - run.started)))
            run.stays(lambda s: s['output_connected'], 'the output went away while idle')
            for text in ('Display dimmed after inactivity', 'Display disabled due to idle', 'acklight'):
                self.assertNotIn(text, run.log_text())

    def test_test_hooks_enable_the_hook_commands_and_screen_capture(self):
        with Run(['--test-hooks']) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, test_hooks='1'))
            run.ipc('DEV_FRAME 42')
            run.wait_for(lambda s: s['capture_ready'] == 42, 'DEV_FRAME did not complete')
            run.ipc('DEV_OUTPUT_REMOVE')
            run.wait_for(lambda s: not s['output_connected'], 'DEV_OUTPUT_REMOVE did nothing')
            run.ipc('DEV_OUTPUT_ADD 1280 720')
            run.wait_for(lambda s: s['output_connected'], 'DEV_OUTPUT_ADD did nothing')
            run.ipc('DEV_SIDE_DISPLAY left 0 3840 2160 deferred')
            run.wait_for(lambda s: (s['left']['display_result'], s['left']['display_scaled'],
                                    s['left']['monitor_width'], s['left']['monitor_height']) == ('deferred', True, 3840, 2160),
                         'DEV_SIDE_DISPLAY did nothing')
            run.send_pointer('R 5 5\n')
            run.ipc('DEV_SCENE left')
            run.wait_for_log('DEV_SCENE left end surfaces=0')
            self.assertNotIn('Ignoring', run.log_text())
            if shutil.which('wayland-info'):
                self.assertIn('zwlr_screencopy_manager_v1', run.globals())

    def test_allow_power_lets_the_confirmation_continue(self):
        # No helper in the bundle: Sleep (and only Sleep) falls back to systemctl.
        with Run(['--allow-power'], tools=RECORD_POWER) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, allow_power='1'))
            run.open_sleep_confirmation()
            run.click_until(CONTINUE, lambda s: not s['overlay_confirmation'] and not s['overlay_visible'],
                            'Continue did not run the action')
            deadline = time.monotonic() + 3
            while not run.recorded('power.log') and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(run.recorded('power.log'), ['suspend'])

    def test_power_command_stands_in_for_systemctl_with_test_hooks(self):
        tools = {**RECORD_POWER, **FAKE_SYSTEMD_RUN}
        with Run(['--test-hooks', '--allow-power'], tools=tools, helper=True, recorder=True,
                 env={'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '4000'}) as run:
            command = str(run.root / 'power-command')
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, test_hooks='1', allow_power='1',
                                                power_command=command))
            # Sleep: the helper runs the recorder with "suspend"; nothing is stopped, systemctl never runs.
            run.open_sleep_confirmation()
            run.click_until(CONTINUE, lambda s: not s['overlay_confirmation'] and not s['overlay_visible'],
                            'Continue did not run Sleep')
            run.wait_for_log('Power helper for suspend finished: exit status 0')
            self.assertEqual(run.recorded('command.log'), ['suspend'])
            self.assertRegex(run.log_text(), r'\[INFO\].*Power action suspend uses DOSPLEX_POWER_COMMAND ' + re.escape(command))
            self.assertEqual(run.state()['power_action'], '')
            self.assertEqual(run.recorded('unit.log'), [], 'Sleep left the compositor service')

            # Restart: through the helper, which leaves the service in a unit of its own, and the
            # progress panel replaces the overlay until that unit stops the compositor.
            run.open_power_confirmation(RESTART, 'Restart')
            run.click_until(CONTINUE, lambda s: s['power_action'] == 'reboot' and not s['overlay_visible'],
                            'Continue did not start Restart')
            run.wait_for_log('Power helper for reboot finished: exit status 0')
            self.assertEqual(run.recorded('command.log'), ['suspend', 'reboot'])
            self.assertRegex(run.log_text(), r'\[INFO\].*Power action reboot uses DOSPLEX_POWER_COMMAND ')
            units = run.recorded('unit.log')
            self.assertEqual(len(units), 1, units)
            self.assertRegex(units[0], r'--unit=dosplex-power-reboot-[0-9a-f]{8} ')
            self.assertEqual(run.recorded('power.log'), [], 'systemctl ran although a power command was given')
            # While the panel shows, nothing opens the overlay.
            for request in ('MENU left toggle', 'CHORD left', 'OVERLAY left'):
                run.ipc(request)
            run.stays(lambda s: not s['overlay_visible'] and s['power_action'] == 'reboot',
                      'the overlay opened over the power panel')
            self.assertIn("Ignoring Main's overlay request (controller): reboot is in progress", run.log_text())

            # Nothing stopped this compositor (the stand-in does not), so at the deadline the panel comes
            # down, says so, and Main's overlay is open again on the Session tab with the message.
            state = run.wait_for(lambda s: s['power_action'] == '', 'the progress panel never came down', timeout=6)
            self.assertEqual(state['power_message'], 'Restart did not finish. See the log.')
            self.assertIn('Restart did not stop Dosplex within', run.log_text())
            self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and state['overlay_tab'] == 2,
                            state)
            # The overlay is Main's, so Main's own chord closes it.
            run.ipc('CHORD left')
            run.wait_for(lambda s: not s['overlay_visible'], 'the overlay did not close after the panel came down')
            run.ipc('CHORD left')
            run.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'left', 'the overlay opens again')

    def test_no_input_reaches_the_sides_while_the_power_panel_shows(self):
        # A real client on Main's side: what it is sent before the panel, nothing while the panel covers
        # it, and again once the panel came down (so the client was there all along).
        with tempfile.TemporaryDirectory(prefix='dosplex-flags-probe-') as build:
            probe_binary = build_probe(build)
            with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAKE_SYSTEMD_RUN},
                     helper=True, recorder=True, env={'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '5000'}) as run:
                env = {'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': str(run.root), 'WAYLAND_DISPLAY': 'dosplex-0'}
                output = (run.root / 'probe.log').open('w+')
                probe = subprocess.Popen([str(probe_binary)], env=env, stdout=output, stderr=output)
                try:
                    run.wait_for(lambda s: s['left']['has_app'], 'the probe never mapped')

                    def events(since=0):
                        output.flush()
                        return (run.root / 'probe.log').read_text()[since:]

                    def probe_saw(since, pattern, timeout=3):
                        deadline = time.monotonic() + timeout
                        while not re.search(pattern, events(since), re.M):
                            assert probe.poll() is None, 'the probe exited'
                            if time.monotonic() > deadline:
                                raise AssertionError(f'the probe never saw {pattern!r}:\n{events(since)[-2000:]}')
                            time.sleep(.02)

                    since = len(events())
                    for attempt in range(8):
                        run.send_pointer(f'M {300 + attempt} 300\n')
                        if 'POINTER_MOTION' in events(since):
                            break
                    run.send_pointer('K 30 1\nK 30 0\n')
                    probe_saw(since, r'^KEY serial=.*key=30 state=0')

                    run.open_power_confirmation(RESTART, 'Restart')
                    run.click_until(CONTINUE, lambda s: s['power_action'] == 'reboot', 'Continue did not start Restart')
                    self.assertIn('Power action reboot: input to both sides is held until Dosplex stops', run.log_text())
                    since = len(events())
                    run.send_pointer('M 310 310\nM 320 320\nB 272 1\nB 272 0\nW 0 1\nR 4 4\nK 30 1\nK 30 0\n')
                    time.sleep(.5)
                    held = events(since)
                    for kind in ('POINTER_ENTER', 'POINTER_MOTION', 'POINTER_BUTTON', 'POINTER_AXIS', 'RELATIVE',
                                 'KEY serial', 'KEYBOARD_ENTER'):
                        self.assertNotIn(kind, held, f'{kind} reached the side under the power panel:\n{held}')
                    self.assertEqual(run.state()['power_action'], 'reboot')

                    state = run.wait_for(lambda s: s['power_action'] == '', 'the progress panel never came down',
                                         timeout=8)
                    # Main's overlay is open again on the Session tab, so it is the overlay that has Main's input
                    # now; closing it gives the client's back.
                    self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and
                                    state['overlay_tab'] == 2, state)
                    run.ipc('CHORD left')
                    run.wait_for(lambda s: not s['overlay_visible'], 'the overlay did not close')
                    since = len(events())
                    for attempt in range(8):
                        run.send_pointer(f'M {330 + attempt} 330\n')
                        if 'POINTER_MOTION' in events(since):
                            break
                    run.send_pointer('K 31 1\nK 31 0\n')
                    probe_saw(since, r'^KEY serial=.*key=31 state=0')
                    self.assertIn('POINTER_MOTION', events(since))
                finally:
                    probe.terminate()
                    try:
                        probe.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        probe.kill()
                        probe.wait(timeout=5)
                    output.close()

    def test_turn_off_dosplex_asks_first_and_runs_the_helper_with_stop(self):
        # Main's Session tab has "Turn off Dosplex": both sessions close and the stock Steam comes back, with no
        # restart. It asks first, then runs the power helper with the action `stop`, which runs no power command.
        # Here the helper cannot leave the compositor's service (no systemd-run), so its first stage fails: that
        # shows which action was started and that a failure is said under the action's own name.
        with Run(['--test-hooks', '--allow-power'], tools=RECORD_POWER, helper=True, recorder=True) as run:
            run.open_power_confirmation(TURN_OFF, 'Turn off Dosplex')
            self.assertEqual(run.state()['power_action'], '')           # nothing runs before Continue
            self.assertNotIn('Power helper for stop', run.log_text())
            run.click(CONTINUE)
            state = run.wait_for(lambda s: s['power_message'] == 'Turn off Dosplex failed. See the log.',
                                 'the failed first stage of Turn off Dosplex was not reported', timeout=5)
            self.assertEqual(state['power_action'], '')
            self.assertIn('Power helper for stop finished: exit status 1', run.log_text())
            self.assertEqual(run.recorded('command.log'), [], 'Turn off Dosplex ran a power command')
            self.assertEqual(run.recorded('power.log'), [], 'Turn off Dosplex ran systemctl')
            self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and state['overlay_tab'] == 2,
                            state)

    def test_a_failed_first_stage_takes_the_panel_down(self):
        # The helper is there but cannot leave the compositor's service (no systemd-run): its first stage
        # fails, so the progress panel goes away again and the Session tab says so.
        with Run(['--test-hooks', '--allow-power'], tools=RECORD_POWER, helper=True, recorder=True) as run:
            run.open_power_confirmation(SHUT_DOWN, 'Shut down')
            run.click(CONTINUE)
            state = run.wait_for(lambda s: s['power_message'] == 'Shut down failed. See the log.',
                                 'the failed first stage was not reported', timeout=5)
            self.assertEqual(state['power_action'], '')
            self.assertIn('Power helper for poweroff finished: exit status 1', run.log_text())
            self.assertEqual(run.recorded('command.log'), [], 'the power command ran after a failed escape')
            self.assertEqual(run.recorded('power.log'), [])
            # Main's overlay is open again on the Session tab, where the message is.
            self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and state['overlay_tab'] == 2,
                            state)
            run.ipc('CHORD left')
            run.wait_for(lambda s: not s['overlay_visible'], 'the overlay did not close after the failure')

    def test_an_escaped_stage_that_fails_later_takes_the_panel_down_when_it_fails(self):
        # The helper waits for the unit it started: when that unit fails, with nothing stopped yet, the
        # compositor learns of it then (not at the deadline, which is far off here), takes the panel down,
        # and Main's overlay says so on the Session tab.
        with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAILING_SYSTEMD_RUN}, helper=True,
                 recorder=True, env={'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '60000'}) as run:
            run.open_power_confirmation(RESTART, 'Restart')
            run.click_until(CONTINUE, lambda s: s['power_action'] == 'reboot' and not s['overlay_visible'],
                            'Continue did not start Restart')
            went_up = time.monotonic()
            run.stays(lambda s: s['power_action'] == 'reboot' and not s['overlay_visible'],
                      'the panel came down before the unit failed', seconds=1.2)
            self.assertEqual(run.recorded('command.log'), [], 'the power command ran')
            state = run.wait_for(lambda s: s['power_action'] == '', 'the failed unit did not take the panel down',
                                 timeout=6)
            self.assertLess(time.monotonic() - went_up, 8)
            self.assertEqual(state['power_message'], 'Restart failed. See the log.')
            self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and state['overlay_tab'] == 2,
                            state)
            self.assertIn('Power helper for reboot finished: exit status 1', run.log_text())
            self.assertNotIn('did not stop Dosplex within', run.log_text())
            self.assertEqual(run.recorded('command.log'), [])
            self.assertEqual(run.recorded('power.log'), [])

    def test_guests_key_ends_when_the_panel_goes_up_and_its_input_comes_back_when_it_goes_down(self):
        # Guest holds a key while Main continues a Restart. Under the panel nothing reaches Guest and the
        # key's release is dropped, so the key has to end when the panel goes up: Guest's application loses
        # the keyboard focus then (a leave, which releases what a client holds). When the unit fails and the
        # panel comes down the focus is back at once, with no key press to bring it, and keys flow again.
        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory(prefix='dosplex-flags-probe-') as build:
            probe_binary = build_probe(build)
            with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAILING_SYSTEMD_RUN}, helper=True,
                     recorder=True, env={'DOSPLEX_LEFT_USER': 'dosplex-flags-test-absent', 'DOSPLEX_RIGHT_USER': me,
                                         'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '60000'}) as run:
                run.ipc('LAYOUT vertical')
                run.wait_for(lambda s: s['layout'] == 'vertical', 'the split layout')
                probe = KeyProbe(run, probe_binary)
                try:
                    run.wait_for(lambda s: s['right']['has_app'], "Guest's probe never mapped")
                    # The remote pointer rests on Guest's side, so the remote keyboard is Guest's. Main's overlay is
                    # worked from the controller verbs, which leave the pointer where it is.
                    run.send_pointer('M 900 300\n')
                    run.send_pointer('K 30 1\n')
                    probe.saw(0, r'^KEY serial=.*key=30 state=1')
                    self.assertEqual((probe.count('KEYBOARD_ENTER'), probe.count('KEYBOARD_LEAVE')), (1, 0))

                    def menu(*verbs):
                        for verb in verbs:
                            run.ipc(f'MENU left {verb}')
                            time.sleep(.3)  # the next frame builds the hit regions the next verb walks

                    run.ipc('OVERLAY left')
                    run.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'left', "Main's overlay")
                    time.sleep(.4)
                    menu('next', 'next', 'next', 'select')                  # Display, Devices, Audio, Session
                    run.wait_for(lambda s: s['overlay_tab'] == 2, 'the Session tab opens')
                    menu(*['next'] * 8, 'select')                           # past the four layouts and the placement to Restart
                    run.wait_for(lambda s: s['overlay_confirmation'], 'Restart asks for confirmation')
                    time.sleep(.3)
                    since = len(probe.events())
                    menu('right', 'select')                                 # Cancel is the default; Continue is beside it
                    state = run.wait_for(lambda s: s['power_action'] == 'reboot', 'Continue did not start Restart')
                    self.assertFalse(state['overlay_visible'], state)
                    # The focus is cleared as the panel goes up: the key Guest holds ends with it.
                    probe.saw(since, r'^KEYBOARD_LEAVE')
                    run.send_pointer('K 30 0\n')
                    run.send_pointer('K 31 1\nK 31 0\n')
                    time.sleep(.3)
                    self.assertNotIn('KEY serial', probe.events(since), 'a key reached Guest under the power panel')
                    self.assertEqual(run.state()['power_action'], 'reboot')

                    state = run.wait_for(lambda s: s['power_action'] == '', 'the failed unit did not take the panel down',
                                         timeout=6)
                    self.assertEqual(state['power_message'], 'Restart failed. See the log.')
                    self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and
                                    state['overlay_tab'] == 2, state)
                    # Guest has its keyboard focus again with no key pressed since, and its keys flow.
                    probe.saw(since, r'^KEYBOARD_ENTER', timeout=2)
                    entered_at = probe.events().rindex('KEYBOARD_ENTER')
                    after = len(probe.events())
                    run.send_pointer('K 32 1\nK 32 0\n')
                    probe.saw(after, r'^KEY serial=.*key=32 state=0')
                    # Nothing is stuck: the key Guest held ended with the leave and is not replayed, the keys
                    # pressed under the panel never arrived, and what came after the focus is what was sent after.
                    log = probe.events()
                    self.assertEqual(re.findall(r'key=(\d+) state=([01])', log[log.index('KEYBOARD_LEAVE'):]),
                                     [('32', '1'), ('32', '0')])
                    self.assertEqual((probe.count('KEYBOARD_ENTER'), probe.count('KEYBOARD_LEAVE')), (2, 1))
                    self.assertGreater(entered_at, log.index('KEYBOARD_LEAVE'))
                finally:
                    probe.stop()

    def test_main_gets_its_keyboard_back_when_it_closes_the_overlay_after_a_failed_power_action(self):
        # Main's application lost the keyboard focus when its overlay opened and never got it back: the overlay
        # was closed with the panel up, which holds the focus, and nothing handed the focus back when the panel
        # came down. Now Main's overlay is open again after the failure, and closing it hands the focus back as
        # it does anywhere.
        with tempfile.TemporaryDirectory(prefix='dosplex-flags-probe-') as build:
            probe_binary = build_probe(build)
            with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAILING_SYSTEMD_RUN}, helper=True,
                     recorder=True, env={'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '60000'}) as run:
                probe = KeyProbe(run, probe_binary)
                try:
                    run.wait_for(lambda s: s['left']['has_app'], "Main's probe never mapped")
                    for attempt in range(8):
                        run.send_pointer(f'M {300 + attempt} 300\n')
                        if probe.count('POINTER_MOTION'):
                            break
                    run.send_pointer('K 30 1\nK 30 0\n')
                    probe.saw(0, r'^KEY serial=.*key=30 state=0')
                    run.open_power_confirmation(RESTART, 'Restart')
                    probe.saw(0, r'^KEYBOARD_LEAVE')
                    run.click_until(CONTINUE, lambda s: s['power_action'] == 'reboot', 'Continue did not start Restart')
                    state = run.wait_for(lambda s: s['power_action'] == '', 'the failed unit did not take the panel down',
                                         timeout=6)
                    self.assertTrue(state['overlay_visible'] and state['overlay_owner'] == 'left' and
                                    state['overlay_tab'] == 2, state)
                    time.sleep(.3)
                    # The overlay owns the keyboard, so the application is without focus as with any overlay, and
                    # nothing that is typed reaches it.
                    self.assertEqual(probe.count('KEYBOARD_ENTER'), probe.count('KEYBOARD_LEAVE'))
                    typed = len(probe.events())
                    run.send_pointer('K 31 1\nK 31 0\n')
                    time.sleep(.3)
                    self.assertNotIn('KEY serial', probe.events(typed))
                    enters = probe.count('KEYBOARD_ENTER')
                    run.ipc('CHORD left')
                    run.wait_for(lambda s: not s['overlay_visible'], 'the overlay did not close')
                    deadline = time.monotonic() + 3
                    while probe.count('KEYBOARD_ENTER') == enters and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(probe.count('KEYBOARD_ENTER'), enters + 1, 'closing the overlay did not give Main its focus back')
                    after = len(probe.events())
                    run.send_pointer('K 32 1\nK 32 0\n')
                    probe.saw(after, r'^KEY serial=.*key=32 state=0')
                finally:
                    probe.stop()

    def test_an_untrusted_helper_is_not_run(self):
        # The compositor checks the helper each time: a regular, executable file that nobody but its
        # owner (root, or the user running the compositor) can write. Ownership needs root to test.
        with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAKE_SYSTEMD_RUN},
                 helper=True, recorder=True) as run:
            helper = run.root / 'bundle/libexec/dosplex-power-action'
            real = run.root / 'bundle/libexec/real-power-action'
            helper.rename(real)
            cases = (('group-writable', 0o775, 'writable by others'), ('other-writable', 0o757, 'writable by others'),
                     ('not executable', 0o644, 'not executable'), ('symlink', None, 'not a regular file'))
            for label, mode, reason in cases:
                with self.subTest(label):
                    if helper.is_symlink() or helper.exists():
                        helper.unlink()
                    if mode is None:
                        helper.symlink_to(real)
                    else:
                        shutil.copyfile(real, helper)
                        helper.chmod(mode)
                    for button, name in ((SLEEP, 'Sleep'), (RESTART, 'Restart')):
                        run.open_power_confirmation(button, name)
                        run.click_until(CONTINUE, lambda s: s['power_message'] == 'Power helper missing' and
                                        not s['overlay_confirmation'], f'{name} ran an untrusted helper ({label})')
                        self.assertIn(f'is not usable ({reason})', run.log_text())
                        run.ipc('OVERLAY close')
                        run.wait_for(lambda s: not s['overlay_visible'], 'the overlay closes')
            time.sleep(.3)
            for log in ('power.log', 'command.log', 'unit.log'):
                self.assertEqual(run.recorded(log), [], log)

    def test_power_command_is_ignored_without_test_hooks(self):
        with Run(['--allow-power'], tools=RECORD_POWER, helper=True, recorder=True,
                 env={'DOSPLEX_POWER_PANEL_TIMEOUT_MS': '1000'}) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, allow_power='1'))
            self.assertIn('Ignoring DOSPLEX_POWER_COMMAND: the compositor was not started with --test-hooks',
                          run.log_text())
            self.assertIn('Ignoring DOSPLEX_POWER_PANEL_TIMEOUT_MS: the compositor was not started with --test-hooks',
                          run.log_text())
            run.open_sleep_confirmation()
            run.click_until(CONTINUE, lambda s: not s['overlay_confirmation'] and not s['overlay_visible'],
                            'Continue did not run Sleep')
            run.wait_for_log('Power helper for suspend finished: exit status 0')
            self.assertEqual(run.recorded('power.log'), ['suspend'])
            self.assertEqual(run.recorded('command.log'), [])
            self.assertNotIn('uses DOSPLEX_POWER_COMMAND', run.log_text())

    def test_restart_and_shut_down_never_run_a_bare_systemctl(self):
        # Without the helper there is nothing that closes the sides first, so nothing runs at all.
        with Run(['--allow-power'], tools={**RECORD_POWER, **FAKE_SYSTEMD_RUN}) as run:
            for button, label in ((RESTART, 'Restart'), (SHUT_DOWN, 'Shut down'), (TURN_OFF, 'Turn off Dosplex')):
                run.open_power_confirmation(button, label)
                run.click_until(CONTINUE, lambda s: s['power_message'] == 'Power helper missing' and
                                not s['overlay_confirmation'], f'{label} did not say the helper is missing')
                state = run.state()
                self.assertTrue(state['overlay_visible'] and state['overlay_tab'] == 2, state)
                self.assertEqual(state['power_action'], '')
                run.ipc('OVERLAY close')
                run.wait_for(lambda s: not s['overlay_visible'], 'the overlay closes')
            time.sleep(.3)
            self.assertEqual(run.recorded('power.log'), [])
            self.assertEqual(run.recorded('unit.log'), [])

    def test_guest_has_no_power_buttons(self):
        with Run(['--test-hooks', '--allow-power'], tools={**RECORD_POWER, **FAKE_SYSTEMD_RUN},
                 helper=True, recorder=True) as run:
            run.ipc('LAYOUT vertical')
            state = run.wait_for(lambda s: s['layout'] == 'vertical', 'the split layout')
            right = state['right']
            run.ipc('OVERLAY right')
            run.wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == 'right', "Guest's overlay opens")
            # Guest's 640x720 region at scale 1: a 616x540 panel 12 from its left and 90 from its top.
            x, y, width = right['x'] + 28, right['y'] + 202, 616 - 32
            tab, third = (width - 24) // 4, (width - 16) // 3
            run.click_until(centre(x + 3 * (tab + 8), right['y'] + 160, tab, 30),
                            lambda s: s['overlay_tab'] == 2, "Guest's Session tab opens")
            time.sleep(.3)
            # Where Main's tab has Sleep, Restart and Shut down there is nothing to press.
            for column in range(3):
                run.click(centre(x + column * (third + 8), y + 136, third, 32))
            run.click(centre(x, y + 210, width, 30))        # and nothing where Main has Turn off Dosplex
            run.stays(lambda s: s['overlay_visible'] and not s['overlay_confirmation'],
                      "Guest's Session tab offered a power action")
            for log in ('power.log', 'command.log', 'unit.log'):
                self.assertEqual(run.recorded(log), [], log)

    def test_audio_exists_only_where_a_router_is_configured(self):
        for flags, router, expected in (([], None, None),
                                        ([], '/opt/router-from-environment', '/opt/router-from-environment'),
                                        (['--audio-router=/opt/router-from-option'], '/opt/router-from-environment', '/opt/router-from-option'),
                                        (['--audio-router='], '/opt/router-from-environment', None)):
            with self.subTest(flags=flags, environment=router), \
                    Run(flags, env={'DOSPLEX_AUDIO_ROUTER': router} if router else {}, tools=RECORD_AUDIO) as run:
                run.open_overlay_page(AUDIO_TAB, 3)
                if expected:
                    run.wait_for(lambda s: s['audio_revision'] > 0, 'the audio helper never answered')
                    self.assertEqual(run.recorded('audio.log'), [f'{expected} overlay'])
                else:
                    run.stays(lambda s: s['audio_revision'] == 0 and not s['audio_busy'], 'audio ran without a router')
                    self.assertEqual(run.recorded('audio.log'), [])

    @unittest.skipIf(BACKLIGHT.exists(), f'{BACKLIGHT} exists here and dim would lower it')
    def test_idle_policy_dim_blanks_the_output_and_input_restores_it(self):
        with Run(['--idle-policy=dim'], env={'DOSPLEX_IDLE_DIM_TIMEOUT_MS': '1000'}) as run:
            self.assertEqual(run.policy(), dict(POLICY_DEFAULTS, idle_policy='dim'))
            run.wait_for_log('Display disabled due to idle', timeout=4)
            # Without a panel backlight the output is all that idles; the sysfs files are not touched.
            self.assertNotIn('acklight', run.log_text())
            run.send_pointer('M 10 10\n')
            run.wait_for_log('Display restored after activity')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: runtime-flags-test.py COMPOSITOR [unittest options]')
    if os.geteuid() == 0:
        raise RuntimeError('run this test as an ordinary user')
    BINARY = str(Path(sys.argv.pop(1)).resolve(strict=True))
    unittest.main()
