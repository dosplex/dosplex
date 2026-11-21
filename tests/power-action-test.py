#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=300
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The power helper closes both sides and stops Dosplex before it restarts or shuts down.

packaging/steamos/dosplex-power-action runs as it would inside a retained trial (its own copy in
<trial>/bundle/libexec), with stand-ins for systemd-run, systemctl, nsenter, runuser, steam, the side
supervisor, the installed dosplex program and the trial's recovery tool. Every stand-in appends one line to
calls.log, so the order of the steps is what the file says.

Each side's Steam is a fake process table (a /proc of its own, passed as --proc-root): a process called
steam, of the side's user, in the cgroup of the side's session unit. The stand-in for steam acts on it
the way a real client acts on `steam -shutdown`: it exits (its entry disappears), it ignores the request,
or the command fails.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'packaging/steamos/dosplex-power-action'
TRIAL_ID = 'c' * 32
OWNER = pwd.getpwuid(os.getuid()).pw_name
UNITS = {'left': 'dosplex-session-left-' + 'a' * 32 + '.service', 'right': 'dosplex-session-right-' + 'b' * 32 + '.service'}
STEAM_PIDS = {'left': 4343, 'right': 4242}
SIDE_OF_PID = {str(pid): side for side, pid in STEAM_PIDS.items()}

# Runs the command after `--` in place, as the escaped unit would run it, and ends with its exit status
# (systemd-run --wait does both). REMOVE_BEFORE_ESCAPE names a file that disappears between the first
# stage and the escaped copy; NO_ESCAPE records the call and runs nothing.
SYSTEMD_RUN = '''#!/bin/sh
echo "systemd-run $*" >> "$CALLS"
[ -z "$NO_ESCAPE" ] || exit 0
[ -z "$REMOVE_BEFORE_ESCAPE" ] || rm -f "$REMOVE_BEFORE_ESCAPE"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
'''
# `show` answers FreezerState (FREEZER_STATE, running otherwise); the power actions also say whether the
# trial directory was still there when they ran.
SYSTEMCTL = '''#!/bin/sh
case "$1" in
  show) echo "systemctl $*" >> "$CALLS"; echo "${FREEZER_STATE:-running}"; exit 0 ;;
  is-active) echo "systemctl $*" >> "$CALLS"; exit "${GUARD_STATUS:-0}" ;;
  reboot|poweroff|suspend) echo "systemctl $* trial=$([ -d "$TRIAL_DIR" ] && echo present || echo gone)" >> "$CALLS" ;;
  *) echo "systemctl $*" >> "$CALLS" ;;
esac
exit "${SYSTEMCTL_STATUS:-0}"
'''
# nsenter and runuser: the command after their own `--`, run in place.
PASS_THROUGH = '''#!/bin/sh
echo "%(name)s $*" >> "$CALLS"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
'''
# The steam command of the sides. It is run by `env -i`, so it finds everything it needs next to itself:
# its own directory holds calls.log, the fake /proc and the mode file(s). The client of the side is named by
# the HOME that the request was given (.../home-<pid>). mode: "exit SECONDS" (the client quits after that
# long), "hang" (nothing happens) or "fail" (the command fails, as when there is no client to tell).
STEAM = '''#!/bin/sh
here=$(dirname "$0")
echo "steam $* HOME=$HOME XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR DISPLAY=$DISPLAY USER=${USER-unset} SECRET=${SECRET-unset}" >> "$here/calls.log"
pid=${HOME##*-}
mode=hang; delay=0
for file in "$here/steam-mode-$pid" "$here/steam-mode"; do
  if [ -f "$file" ]; then read -r mode delay < "$file"; break; fi
done
case "$mode" in
  exit) ( sleep "$delay"; rm -rf "$here/proc/$pid" ) >/dev/null 2>&1 & ;;
  fail) echo "steam: no running client" >&2; exit 1 ;;
esac
exit 0
'''
# request-stop SIDE: what the real supervisor's loop does with the stop file, at once. A side named in
# STUCK_SIDES never stops.
SUPERVISOR = '''import json, os, sys
from pathlib import Path
with open(os.environ['CALLS'], 'a') as calls:
    calls.write('supervisor %s side_root=%s\\n' % (' '.join(sys.argv[1:]), os.environ.get('DOSPLEX_SIDE_ROOT')))
side = sys.argv[2]
if side not in os.environ.get('STUCK_SIDES', '').split():
    path = Path(os.environ['DOSPLEX_SIDE_ROOT']) / side / 'session.json'
    state = json.loads(path.read_text())
    state.update(lifecycle='stopped', active=False)
    path.write_text(json.dumps(state))
'''
# REMOVE_TRIAL: what root-session-trial.py recover ends with, shutil.rmtree of the trial directory.
# RECOVERY_BUSY: what it says when another recovery holds the trial's lock (and then does nothing).
DOSPLEX = '''#!/bin/sh
echo "dosplex $*" >> "$CALLS"
[ -z "$REMOVE_TRIAL" ] || rm -rf "$REMOVE_TRIAL"
[ -z "$RECOVERY_BUSY" ] || { echo "dosplex: python3 failed (exit 1): RuntimeError: recovery already running; retry later"; exit 1; }
exit "${DOSPLEX_STATUS:-0}"
'''
RECOVER = '''import os, sys
with open(os.environ['CALLS'], 'a') as calls:
    calls.write('trial %s\\n' % ' '.join(sys.argv[1:]))
if os.environ.get('RECOVERY_BUSY'):
    sys.exit('RuntimeError: recovery already running; retry later')
sys.exit(int(os.environ.get('RECOVER_STATUS', '0')))
'''
RECORDER = '''#!/bin/sh
echo "power-command $*" >> "$CALLS"
'''


def write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class PowerActionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dosplex-power-test-')
        base = self.base = Path(self.directory.name)
        self.trial = base / ('dosplex-trial-' + TRIAL_ID)
        self.helper = self.trial / 'bundle/libexec/dosplex-power-action'
        write(self.helper, HELPER.read_text())
        write(self.trial / 'bundle/bin/dosplex-side-supervisor.py', SUPERVISOR)
        write(self.trial / 'root-session-trial.py', RECOVER)
        write(self.trial / 'transaction.json', '{}', 0o644)
        for side in ('left', 'right'):
            write(self.trial / 'sides' / side / 'session.json',
                  json.dumps({'side': side, 'owner': OWNER, 'unit': UNITS[side], 'lifecycle': 'running',
                              'active': True, 'pid': os.getpid(), 'instance': side[0] * 32}), 0o644)
        self.bin = base / 'bin'
        write(self.bin / 'systemd-run', SYSTEMD_RUN)
        write(self.bin / 'systemctl', SYSTEMCTL)
        for name in ('nsenter', 'runuser'):
            write(self.bin / name, PASS_THROUGH % {'name': name})
        self.steam = base / 'steam'
        write(self.steam, STEAM)
        self.proc = base / 'proc'
        self.proc.mkdir()
        self.dosplex = base / 'opt/dosplex'
        write(self.dosplex, DOSPLEX)
        self.state = base / 'state.json'
        write(self.state, json.dumps({'trial': {'root': str(self.trial)}}), 0o644)
        self.calls = base / 'calls.log'
        self.command = base / 'power-command'

    def tearDown(self):
        self.directory.cleanup()

    def add_steam(self, side, uid=None, unit=None, comm='steam', state='S', pid=None):
        """A Steam client of `side` in the fake process table: the process, the account and the cgroup of the
        side's session unit, with an environment that has more in it than a request may take along."""
        pid = pid or STEAM_PIDS[side]
        uid = os.getuid() if uid is None else uid
        unit = unit or UNITS[side]
        home = self.base / ('home-%d' % pid)
        write(self.proc / str(pid) / 'comm', comm + '\n', 0o644)
        write(self.proc / str(pid) / 'stat', '%d (%s) %s 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %d 0 0\n'
              % (pid, comm, state, 1000 + pid), 0o644)
        write(self.proc / str(pid) / 'status', 'Name:\t%s\nUid:\t%d\t%d\t%d\t%d\n' % ((comm,) + (uid,) * 4), 0o644)
        write(self.proc / str(pid) / 'cgroup', '0::/system.slice/%s\n' % unit, 0o644)
        write(self.proc / str(pid) / 'environ',
              'HOME=%s\0XDG_RUNTIME_DIR=/run/%s\0DISPLAY=:7\0SECRET=do-not-pass-on\0'
              % (home, unit.removesuffix('.service')), 0o644)
        return pid

    def steam_mode(self, text, pid=None):
        write(self.base / ('steam-mode-%d' % pid if pid else 'steam-mode'), text + '\n', 0o644)

    def run_helper(self, action, *extra, **env):
        environment = {'PATH': '%s:/usr/bin:/bin' % self.bin, 'CALLS': str(self.calls), 'TRIAL_DIR': str(self.trial),
                       'DOSPLEX_SIDE_ROOT': str(self.trial / 'sides'), **env}
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(self.helper), '--state-file', str(self.state),
                                 '--dosplex', str(self.dosplex), '--steam', str(self.steam),
                                 '--proc-root', str(self.proc), *extra, action],
                                env=environment, capture_output=True, text=True, timeout=60)
        self.elapsed = time.monotonic() - started
        return result

    def recorded(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def steps(self):
        """The calls without their arguments' details: what ran, in order. A request that Steam quits is
        `quit SIDE` (the stand-ins for runuser and steam behind it are not listed)."""
        names = []
        for line in self.recorded():
            words = line.split()
            if words[0] == 'supervisor':
                names.append('request-stop ' + words[2])
            elif words[0] in ('systemctl', 'dosplex', 'power-command'):
                names.append(' '.join(words[:2]))
            elif words[0] == 'trial':
                names.append('trial ' + words[1])
            elif words[0] == 'nsenter':
                names.append('quit ' + SIDE_OF_PID[words[2]])
            elif words[0] in ('runuser', 'steam'):
                continue
            else:
                names.append(words[0])
        return names

    def test_restart_closes_both_sides_then_stops_dosplex_then_restarts(self):
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop',
                                        'systemctl reboot'])
        unit = self.recorded()[0]
        self.assertRegex(unit, r'--unit=dosplex-power-reboot-[0-9a-f]{8} ')
        self.assertIn('--escaped --trial-root %s ' % self.trial, unit)
        self.assertIn('side_root=%s' % (self.trial / 'sides'), self.recorded()[1])
        self.assertIn('right: stopped after', result.stderr)
        self.assertIn('left: stopped after', result.stderr)

    def test_the_escape_waits_for_its_unit_so_that_a_failing_copy_fails_the_first_stage(self):
        # systemd-run --wait ends with the exit status of the unit: the compositor, which reaps this first
        # stage, learns of an escaped copy that fails and takes its progress panel down. The stand-in does
        # what --wait does, so what is checked here is the flag and the status that comes back.
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.recorded()[0], r'^systemd-run --quiet --collect --wait --service-type=exec ')
        self.calls.unlink()
        refused = self.run_helper('reboot', SYSTEMCTL_STATUS='5')
        self.assertEqual(refused.returncode, 5, refused.stderr)
        self.assertEqual(self.steps()[-1], 'systemctl reboot')
        self.assertIn('exit status 5', refused.stderr)

    def test_shut_down_recovers_the_trial_itself_when_dosplex_did_not_start_it(self):
        write(self.state, json.dumps({'trial': {'root': '/run/dosplex-trial-' + 'd' * 32}}), 0o644)
        result = self.run_helper('poweroff')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'trial recover',
                                        'systemctl poweroff'])
        self.assertIn('trial recover --root %s' % self.trial, self.recorded())

    def test_turn_off_dosplex_closes_both_sides_and_stops_dosplex_and_runs_no_power_command(self):
        # "Turn off Dosplex" on Main's Session page: the same orderly close as Restart, then nothing: the machine
        # stays on with the stock Steam. No systemctl reboot, poweroff or suspend may follow.
        result = self.run_helper('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop'])
        self.assertFalse([line for line in self.recorded() if line.startswith('systemctl ')], self.recorded())
        self.assertIn('stop: done; the stock Steam is back', result.stderr)

    def test_close_asks_both_sides_and_stops_nothing_else(self):
        # What `dosplex stop` runs first: Steam quits on both sides and the supervisors are asked to stop. No unit
        # of its own, no `dosplex stop` (that is the caller), no recovery, no power command.
        for side in ('left', 'right'):
            self.add_steam(side)
        self.steam_mode('exit 0.2')          # the stand-in Steam quits when asked
        result = self.run_helper('close')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.steps()), ['quit left', 'quit right', 'request-stop left', 'request-stop right'])
        self.assertIn('close: done', result.stderr)
        self.assertTrue(self.trial.is_dir())

    def test_close_finds_its_sides_from_its_own_place_when_nothing_is_said(self):
        result = subprocess.run([sys.executable, str(self.helper), '--steam', str(self.steam), '--proc-root',
                                 str(self.proc), 'close'], capture_output=True, text=True, timeout=60,
                                env={'PATH': '%s:/usr/bin:/bin' % self.bin, 'CALLS': str(self.calls),
                                     'TRIAL_DIR': str(self.trial)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.steps()), ['request-stop left', 'request-stop right'])
        self.assertIn('side_root=%s' % (self.trial / 'sides'), self.recorded()[0])

    def test_close_with_no_side_running_does_nothing(self):
        for side in ('left', 'right'):
            write(self.trial / 'sides' / side / 'session.json', json.dumps({'side': side, 'lifecycle': 'stopped'}), 0o644)
        result = self.run_helper('close')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), [])

    def test_close_steam_only_asks_only_the_named_side(self):
        # The supervisor's own stop asks its own Steam and waits; it does not ask the other side and it does not
        # request-stop either (it stops the unit itself afterwards, TRL-04).
        self.add_steam('left')
        self.add_steam('right')
        self.steam_mode('exit 0.2')          # the stand-in Steam quits when asked
        result = self.run_helper('close', '--steam-only', '--side', 'right')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['quit right'])
        self.assertNotIn('request-stop', ' '.join(self.steps()))
        self.assertIn('close: asked right Steam to quit (steam only)', result.stderr)
        self.assertIn('close: done', result.stderr)

    def test_close_steam_only_asks_a_side_that_is_already_stopping(self):
        # The supervisor has just written `stopping` before it stops its unit; a Steam there is still asked,
        # because it is the one whose saves are at risk. `finished()` would say the side is done, but not here.
        self.add_steam('left')
        self.add_steam('right')
        session = self.trial / 'sides' / 'left' / 'session.json'
        state = json.loads(session.read_text())
        state['lifecycle'] = 'stopping'
        write(session, json.dumps(state), 0o644)
        self.steam_mode('exit 0.2')          # the stand-in Steam quits when asked
        result = self.run_helper('close', '--steam-only', '--side', 'left')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['quit left'])
        self.assertIn('close: asked left Steam to quit (steam only)', result.stderr)

    def test_close_with_a_side_only_asks_and_stops_that_side(self):
        # `close --side` is the ordinary close limited to one side: Steam quits and the supervisor is asked,
        # and the other side is left alone.
        self.add_steam('left')
        self.add_steam('right')
        self.steam_mode('exit 0.2')          # the stand-in Steam quits when asked
        result = self.run_helper('close', '--side', 'left')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.steps()), ['quit left', 'request-stop left'])
        self.assertIn('close: done', result.stderr)

    def test_close_steam_only_asks_both_sides_without_requesting_stop(self):
        self.add_steam('left')
        self.add_steam('right')
        self.steam_mode('exit 0.2')          # the stand-in Steam quits when asked
        result = self.run_helper('close', '--steam-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(self.steps()), ['quit left', 'quit right'])
        self.assertNotIn('request-stop', ' '.join(self.steps()))
        self.assertIn('close: done', result.stderr)

    def test_close_steam_only_is_bounded_and_says_so(self):
        self.add_steam('left')
        self.add_steam('right')
        self.steam_mode('hang')
        result = self.run_helper('close', '--steam-only', '--side', 'left', '--steam-timeout', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['quit left'])
        self.assertIn('left: Steam did not exit', result.stderr)
        self.assertIn('close: done', result.stderr)
        self.assertLess(self.elapsed, 10)

    def test_turn_off_dosplex_fails_when_dosplex_did_not_stop(self):
        # The compositor still runs then, reaps the helper's failure, takes its progress panel down and says so.
        result = self.run_helper('stop', '--recovery-wait', '1', RECOVERY_BUSY='1')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('stop: Dosplex did not stop', result.stderr)
        self.assertFalse([line for line in self.recorded() if line.startswith('systemctl ')], self.recorded())

    def test_turn_off_dosplex_needs_no_power_command(self):
        # A missing or untrusted power command refuses Restart; it has no say in an action that runs none.
        result = self.run_helper('stop', '--power-command', '/nonexistent/power-command')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('refused', result.stderr)

    def test_the_action_runs_even_when_stopping_dosplex_fails(self):
        result = self.run_helper('reboot', DOSPLEX_STATUS='3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps()[-2:], ['dosplex stop', 'systemctl reboot'])
        self.assertIn('stop failed; running reboot anyway', result.stderr)
        # A stop that failed for another reason than a recovery that runs does not wait for anything.
        self.assertNotIn('waiting up to', result.stderr)
        self.assertLess(self.elapsed, 10)

    def test_the_wait_for_a_side_is_bounded(self):
        result = self.run_helper('reboot', '--side-timeout', '1', STUCK_SIDES='right')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop',
                                        'systemctl reboot'])
        self.assertIn('right: still running after 1 s; continuing', result.stderr)
        self.assertLess(self.elapsed, 20)

    def test_a_side_without_a_running_supervisor_is_not_asked(self):
        (self.trial / 'sides/right/session.json').unlink()
        state = json.loads((self.trial / 'sides/left/session.json').read_text())
        write(self.trial / 'sides/left/session.json', json.dumps(dict(state, lifecycle='stopped')), 0o644)
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'dosplex stop', 'systemctl reboot'])

    def test_sleep_stops_nothing(self):
        self.add_steam('right')
        self.add_steam('left')
        result = self.run_helper('suspend')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemctl suspend'])
        state = json.loads((self.trial / 'sides/left/session.json').read_text())
        self.assertEqual(state['lifecycle'], 'running')
        self.assertNotIn('Steam', result.stderr)

    def name_a_guard(self):
        unit = 'dosplex-sleep-guard-' + 'd' * 32 + '.service'
        write(self.trial / 'transaction.json', json.dumps({'sleep_guard_unit': unit}), 0o644)
        return unit

    def test_sleep_goes_ahead_when_the_trials_guard_is_active(self):
        unit = self.name_a_guard()
        result = self.run_helper('suspend')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded()[0], 'systemctl is-active --quiet ' + unit)
        self.assertEqual(self.steps(), ['systemctl is-active', 'systemctl suspend'])

    def test_sleep_is_refused_when_the_trials_guard_is_not_running(self):
        unit = self.name_a_guard()
        result = self.run_helper('suspend', GUARD_STATUS='3')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('refused suspend: the sleep guard (%s) is not running' % unit, result.stderr)
        self.assertEqual(self.steps(), ['systemctl is-active'])

    # -- Steam is asked to quit before anything is stopped ----------------------------------------------

    def test_steam_is_asked_to_quit_on_both_sides_at_once_before_either_unit_is_stopped(self):
        self.add_steam('right')
        self.add_steam('left')
        self.steam_mode('exit 0.5')
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.steps()
        self.assertEqual(names[0], 'systemd-run')
        # Both sides are asked first (neither has quit yet when the second is asked), each side's supervisor
        # only after its own Steam is gone, and Dosplex and the action after both.
        self.assertCountEqual(names[1:3], ['quit right', 'quit left'])
        self.assertCountEqual(names[3:5], ['request-stop right', 'request-stop left'])
        self.assertEqual(names[5:], ['dosplex stop', 'systemctl reboot'])
        for side in ('right', 'left'):
            self.assertIn('%s: Steam shutdown requested (steam pid %d in %s)' % (side, STEAM_PIDS[side], UNITS[side]),
                          result.stderr)
            self.assertRegex(result.stderr, r'%s: Steam exited after \d+\.\d s' % side)
            self.assertNotIn('%s: Steam did not exit' % side, result.stderr)
            self.assertIn('%s: stopped after' % side, result.stderr)
        text = result.stderr
        # Both requests were made before either client had quit, and a side's stop was asked for only after
        # that side's own Steam had gone.
        first_exit = min(text.index('right: Steam exited'), text.index('left: Steam exited'))
        self.assertLess(text.index('right: Steam shutdown requested'), first_exit)
        self.assertLess(text.index('left: Steam shutdown requested'), first_exit)
        for side in ('right', 'left'):
            self.assertLess(text.index('%s: Steam exited' % side), text.index('%s request-stop: ' % side))

    def test_the_request_reaches_the_client_as_its_user_with_its_home_and_runtime(self):
        self.add_steam('right')
        self.steam_mode('exit 0.2')
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        home = self.base / 'home-4242'
        nsenter = next(line for line in self.recorded() if line.startswith('nsenter '))
        # The namespaces of Steam's own process, then the account, a clean environment with only what the
        # client has for HOME and the runtime, and steam -shutdown.
        self.assertEqual(nsenter, 'nsenter -t 4242 -m -p -n -- runuser -u %s -- env -i PATH=/usr/bin:/bin '
                         'DISPLAY=:7 HOME=%s XDG_RUNTIME_DIR=/run/%s %s -shutdown'
                         % (OWNER, home, UNITS['right'].removesuffix('.service'), self.steam))
        steam = next(line for line in self.recorded() if line.startswith('steam '))
        self.assertEqual(steam, 'steam -shutdown HOME=%s XDG_RUNTIME_DIR=/run/%s DISPLAY=:7 USER=unset SECRET=unset'
                         % (home, UNITS['right'].removesuffix('.service')))

    def test_a_steam_that_does_not_quit_is_waited_for_at_most_the_timeout_and_then_the_unit_is_stopped(self):
        self.add_steam('right')
        self.add_steam('left')
        self.steam_mode('hang')
        result = self.run_helper('reboot', '--steam-timeout', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.steps()
        self.assertEqual(names[0], 'systemd-run')
        self.assertCountEqual(names[1:3], ['quit right', 'quit left'])
        self.assertCountEqual(names[3:5], ['request-stop right', 'request-stop left'])
        self.assertEqual(names[5:], ['dosplex stop', 'systemctl reboot'])
        text = result.stderr
        for side in ('right', 'left'):
            self.assertIn('%s: Steam did not exit; stopping the unit (still running after 1 s)' % side, text)
            self.assertNotIn('%s: Steam exited' % side, text)
        # Both waits run side by side: both requests were made before either wait ended.
        first_giving_up = min(text.index('right: Steam did not exit'), text.index('left: Steam did not exit'))
        self.assertLess(text.index('right: Steam shutdown requested'), first_giving_up)
        self.assertLess(text.index('left: Steam shutdown requested'), first_giving_up)
        self.assertLess(self.elapsed, 20)

    def test_a_steam_that_only_just_started_gets_a_short_wait(self):
        # Seen on a Steam Deck: a Steam asked to quit about a minute after its start was still there after 45 s,
        # one asked after three minutes was gone in 3 s. The wait is cut for a young one, by its age in /proc.
        ticks = os.sysconf('SC_CLK_TCK')
        for side in ('right', 'left'):
            self.add_steam(side)
        self.steam_mode('hang')
        young = (1000 + STEAM_PIDS['right']) / ticks + 40          # the fake clients started (1000 + pid) ticks in
        write(self.proc / 'uptime', '%.2f 0.00\n' % young, 0o644)
        result = self.run_helper('close', '--steam-timeout', '30', '--young-steam-timeout', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        for side in ('right', 'left'):
            self.assertRegex(result.stderr, r'%s: Steam started (39|40|41) s ago and may not answer yet; '
                             r'waiting at most 1 s for it' % side)
            self.assertIn('%s: Steam did not exit; stopping the unit (still running after 1 s)' % side, result.stderr)
        self.assertLess(self.elapsed, 15)

    def test_a_steam_that_has_run_for_a_while_gets_the_full_wait(self):
        ticks = os.sysconf('SC_CLK_TCK')
        self.add_steam('right')
        self.steam_mode('hang')
        write(self.proc / 'uptime', '%.2f 0.00\n' % ((1000 + STEAM_PIDS['right']) / ticks + 600), 0o644)
        result = self.run_helper('close', '--steam-timeout', '2', '--young-steam-timeout', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('may not answer yet', result.stderr)
        self.assertIn('right: Steam did not exit; stopping the unit (still running after 2 s)', result.stderr)

    def test_a_side_whose_steam_quit_is_stopped_without_waiting_for_the_other_side(self):
        # The supervisor starts a side again whose Steam exited, so the stop request must come at once.
        self.add_steam('right')
        self.add_steam('left')
        self.steam_mode('exit 0.2', pid=STEAM_PIDS['right'])
        self.steam_mode('hang', pid=STEAM_PIDS['left'])
        result = self.run_helper('reboot', '--steam-timeout', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stderr
        self.assertLess(text.index('right: Steam exited'), text.index('right request-stop: exit status 0'))
        self.assertLess(text.index('right request-stop: exit status 0'), text.index('left: Steam did not exit'))
        self.assertEqual(self.steps()[-4:], ['request-stop right', 'request-stop left', 'dosplex stop',
                                             'systemctl reboot'])

    def test_a_failing_request_does_not_wait_for_a_steam_that_was_never_told(self):
        self.add_steam('right')
        self.steam_mode('fail')
        result = self.run_helper('reboot', '--steam-timeout', '30')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('right: Steam did not exit; stopping the unit (steam -shutdown exited with status 1)',
                      result.stderr)
        self.assertIn('right steam | steam: no running client', result.stderr)
        # Left has no Steam to ask and is handed over at once; right follows when its request has failed.
        self.assertEqual(self.steps(), ['systemd-run', 'quit right', 'request-stop left', 'request-stop right',
                                        'dosplex stop', 'systemctl reboot'])
        self.assertLess(self.elapsed, 15)

    def test_no_steam_is_started_for_a_side_that_runs_none(self):
        # Told to quit, a Steam that is not running can be started instead: nothing is run for a side
        # whose unit has no Steam, nor for processes that only look like one.
        self.add_steam('right', uid=os.getuid() + 1)                       # another user's
        self.add_steam('right', state='Z', pid=4401)                       # a zombie
        self.add_steam('left', unit='dosplex-session-left-' + 'd' * 32 + '.service')  # an earlier session's
        self.add_steam('left', comm='steamwebhelper', pid=4400)            # another program
        self.steam_mode('exit 0')
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop',
                                        'systemctl reboot'])
        self.assertEqual([line for line in self.recorded() if line.split()[0] in ('nsenter', 'runuser', 'steam')], [])
        self.assertIn('right: no Steam process in %s; nothing to quit' % UNITS['right'], result.stderr)
        self.assertIn('left: no Steam process in %s; nothing to quit' % UNITS['left'], result.stderr)

    def test_a_session_without_a_usable_unit_or_owner_is_not_asked(self):
        self.add_steam('right')
        for side, fields in (('right', {'unit': '../../etc'}), ('left', {'owner': 'no such user'})):
            state = json.loads((self.trial / 'sides' / side / 'session.json').read_text())
            write(self.trial / 'sides' / side / 'session.json', json.dumps(dict(state, **fields)), 0o644)
        result = self.run_helper('reboot')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop',
                                        'systemctl reboot'])
        self.assertEqual(result.stderr.count('session.json names no usable session unit or owner'), 2)

    def test_a_frozen_side_is_thawed_so_that_steam_can_quit(self):
        # Guest in Full screen is on standby: its unit is frozen and a frozen Steam acts on nothing.
        self.add_steam('right')
        self.steam_mode('exit 0.2')
        state = json.loads((self.trial / 'sides/right/session.json').read_text())
        write(self.trial / 'sides/right/session.json', json.dumps(dict(state, lifecycle='standby')), 0o644)
        result = self.run_helper('reboot', FREEZER_STATE='frozen')
        self.assertEqual(result.returncode, 0, result.stderr)
        names = self.recorded()
        show = 'systemctl show -p FreezerState --value %s' % UNITS['right']
        thaw = 'systemctl thaw %s' % UNITS['right']
        self.assertEqual([line for line in names if line.startswith('systemctl')][:2], [show, thaw])
        self.assertLess(names.index(thaw), next(i for i, line in enumerate(names) if line.startswith('nsenter')))
        self.assertIn('right: Steam exited after', result.stderr)
        # A side on standby whose unit is not frozen is not thawed.
        self.calls.unlink()
        self.add_steam('right')
        write(self.trial / 'sides/right/session.json', json.dumps(dict(state, lifecycle='standby')), 0o644)
        result = self.run_helper('reboot', FREEZER_STATE='running')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('systemctl thaw', '\n'.join(self.recorded()))

    def test_the_side_directory_falls_back_to_the_supervisors_default(self):
        # The display-manager installation sets no DOSPLEX_SIDE_ROOT and has no trial: the sides are in
        # /var/lib/dosplex/sides, where a supervisor puts them (side_state_root() of the supervisor).
        plain = self.base / 'plain'
        write(plain / 'libexec/dosplex-power-action', HELPER.read_text())
        write(plain / 'bin/dosplex-side-supervisor.py', SUPERVISOR)
        environment = {'PATH': '%s:/usr/bin:/bin' % self.bin, 'CALLS': str(self.calls), 'NO_ESCAPE': '1'}
        result = subprocess.run([sys.executable, str(plain / 'libexec/dosplex-power-action'),
                                 '--proc-root', str(self.proc), 'reboot'],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = self.recorded()[0]
        self.assertIn(' --trial-root  --side-root /var/lib/dosplex/sides --supervisor %s ' % (plain / 'bin/dosplex-side-supervisor.py'), line)
        # The supervisor's own default is the same path.
        source = (HELPER.parents[0] / 'dosplex-side-supervisor.py').read_text()
        self.assertIn("os.environ.get('DOSPLEX_SIDE_ROOT', '/var/lib/dosplex/sides')", source)
        # In a trial without DOSPLEX_SIDE_ROOT the trial's own sides are used, as before.
        self.calls.unlink()
        environment.pop('DOSPLEX_SIDE_ROOT', None)
        subprocess.run([sys.executable, str(self.helper), '--proc-root', str(self.proc), 'reboot'],
                       env=environment, capture_output=True, text=True, timeout=30, check=True)
        self.assertIn(' --side-root %s ' % (self.trial / 'sides'), self.recorded()[0])

    def test_without_a_side_root_variable_the_default_sides_are_closed(self):
        # The same, end to end: sessions in a directory of their own that stands in for /var/lib/dosplex/sides.
        plain = self.base / 'plain'
        write(plain / 'libexec/dosplex-power-action',
              HELPER.read_text().replace("SIDE_ROOT = '/var/lib/dosplex/sides'", "SIDE_ROOT = %r" % str(self.trial / 'sides')))
        write(plain / 'bin/dosplex-side-supervisor.py', SUPERVISOR)
        self.add_steam('right')
        self.steam_mode('exit 0.2')
        environment = {'PATH': '%s:/usr/bin:/bin' % self.bin, 'CALLS': str(self.calls), 'TRIAL_DIR': str(self.trial)}
        result = subprocess.run([sys.executable, str(plain / 'libexec/dosplex-power-action'),
                                 '--state-file', str(self.state), '--dosplex', str(self.dosplex),
                                 '--steam', str(self.steam), '--proc-root', str(self.proc), 'reboot'],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'quit right', 'request-stop left', 'request-stop right',
                                        'systemctl reboot'])
        self.assertIn('stop: no trial root; nothing to stop', result.stderr)

    # -- a recovery that already runs is waited for -------------------------------------------------------

    def delete_the_trial_after_the_stop(self, seconds):
        """The other recovery finishes `seconds` after the helper's attempt at the stop (which says one runs): what
        is waited for is the stop's own call, so that a slow machine does not change what the test shows."""
        def later():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                lines = self.calls.read_text().splitlines() if self.calls.exists() else []
                if any(line.startswith(('dosplex stop', 'trial recover')) for line in lines):
                    break
                time.sleep(.02)
            time.sleep(seconds)
            shutil.rmtree(self.trial, ignore_errors=True)
        thread = threading.Thread(target=later)
        thread.start()
        return thread

    def test_a_recovery_that_already_runs_is_waited_for_before_the_action(self):
        # The trial's watcher holds the lock and is restoring: `dosplex stop` says so and does nothing. The
        # action waits until the trial's directory is gone, so that it does not cut that recovery short.
        remover = self.delete_the_trial_after_the_stop(1.5)
        try:
            result = self.run_helper('reboot', '--recovery-wait', '30', RECOVERY_BUSY='1')
        finally:
            remover.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps()[-2:], ['dosplex stop', 'systemctl reboot'])
        self.assertIn('systemctl reboot trial=gone', self.recorded())
        self.assertIn('stop: a recovery of %s already runs; waiting up to 30 s for it to finish' % self.trial,
                      result.stderr)
        waited = re.search(r'stop: %s is gone after (\d+\.\d) s; the other recovery finished' % self.trial,
                           result.stderr)
        self.assertTrue(waited, result.stderr)
        self.assertGreaterEqual(float(waited.group(1)), 1.2)
        self.assertNotIn('stop failed', result.stderr)

    def test_the_wait_for_a_recovery_is_bounded(self):
        result = self.run_helper('poweroff', '--recovery-wait', '1', RECOVERY_BUSY='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stop: %s still exists after 1 s; going on without it' % self.trial, result.stderr)
        self.assertIn('stop failed; running poweroff anyway', result.stderr)
        self.assertIn('systemctl poweroff trial=present', self.recorded())
        self.assertLess(self.elapsed, 15)

    def test_the_trials_own_recovery_tool_is_waited_for_too(self):
        write(self.state, json.dumps({'trial': {'root': '/run/dosplex-trial-' + 'd' * 32}}), 0o644)
        remover = self.delete_the_trial_after_the_stop(1.0)
        try:
            result = self.run_helper('reboot', '--recovery-wait', '30', RECOVERY_BUSY='1')
        finally:
            remover.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('trial recover --root %s' % self.trial, self.recorded())
        self.assertIn('systemctl reboot trial=gone', self.recorded())
        self.assertIn('the other recovery finished', result.stderr)

    def test_the_wait_for_a_recovery_never_leaves_the_action_without_its_time(self):
        # The wait shares the sequence's budget: a stop that took long leaves the wait the rest of it only.
        loader = importlib.machinery.SourceFileLoader('power_action', str(HELPER))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader('power_action', loader))
        loader.exec_module(module)
        self.assertEqual(module.SEQUENCE_BUDGET + module.ACTION_TIMEOUT + 30, module.UNIT_RUNTIME)
        waits = []
        module.wait_for_recovery = lambda root, wait: waits.append(round(wait)) or False
        module.run_captured = lambda *args, **kwargs: (1, 'recovery already running; retry later')
        module.stop_dosplex(self.trial, str(self.state), str(self.dosplex), 300.0, time.monotonic() + 40)
        module.stop_dosplex(self.trial, str(self.state), str(self.dosplex), 300.0, time.monotonic() - 5)
        module.stop_dosplex(self.trial, str(self.state), str(self.dosplex), 300.0, None)
        self.assertEqual(waits, [40, 0, 300])

    # -- the power command ----------------------------------------------------------------------------------

    def test_a_power_command_runs_instead_of_systemctl(self):
        write(self.command, RECORDER)
        result = self.run_helper('reboot', '--power-command', str(self.command))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left', 'dosplex stop',
                                        'power-command reboot'])
        self.calls.unlink()
        self.assertEqual(self.run_helper('suspend', '--power-command', str(self.command)).returncode, 0)
        self.assertEqual(self.steps(), ['power-command suspend'])

    def dry_run_in_the_trial(self, action):
        # The trial's command lives in <trial>/control, and stopping Dosplex removes the whole trial. A dry
        # run of Restart or Shut down must still end in the stand-in, never in the real systemctl.
        command = self.trial / 'control/power-command'
        write(command, RECORDER)
        result = self.run_helper(action, '--power-command', str(command), REMOVE_TRIAL=str(self.trial))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.trial.exists(), 'the stub stop did not remove the trial')
        self.assertEqual(self.steps(), ['systemd-run', 'request-stop right', 'request-stop left',
                                        'dosplex stop', 'power-command ' + action])
        self.assertIn('--power-command-required', self.recorded()[0])
        self.assertNotIn('using systemctl', result.stderr)

    def test_the_trial_power_command_still_restarts_after_the_stop_deleted_the_trial(self):
        self.dry_run_in_the_trial('reboot')

    def test_the_trial_power_command_still_shuts_down_after_the_stop_deleted_the_trial(self):
        self.dry_run_in_the_trial('poweroff')

    def test_a_power_command_that_vanishes_before_the_escape_refuses_the_action(self):
        command = self.trial / 'control/power-command'
        write(command, RECORDER)
        result = self.run_helper('reboot', '--power-command', str(command), REMOVE_BEFORE_ESCAPE=str(command))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.steps(), ['systemd-run'])
        self.assertIn('not falling back to systemctl', result.stderr)

    def test_a_power_command_that_is_not_installed_leaves_systemctl(self):
        result = self.run_helper('suspend', '--power-command', str(self.command))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.steps(), ['systemctl suspend'])
        self.assertIn('is not installed; using systemctl', result.stderr)

    def test_an_untrusted_power_command_is_refused_before_anything_stops(self):
        write(self.command, RECORDER, 0o775)
        link = self.command.with_name('power-link')
        os.symlink(self.command, link)
        plain = self.command.with_name('power-plain')
        write(plain, RECORDER, 0o644)
        for path, reason in ((self.command, 'writable by others'), (link, 'not a regular file'),
                             (plain, 'not executable')):
            for action in ('suspend', 'reboot'):
                with self.subTest(path=path.name, action=action):
                    result = self.run_helper(action, '--power-command', str(path))
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(reason, result.stderr)
                    self.assertEqual(self.recorded(), [], 'something ran although the command was refused')

    def test_unknown_actions_are_refused(self):
        for action in ('halt', 'restart', ''):
            with self.subTest(action=action):
                self.assertEqual(self.run_helper(action).returncode, 64)
        self.assertEqual(self.recorded(), [])

    def test_the_timeouts_are_checked(self):
        for option in ('--side-timeout', '--steam-timeout'):
            for value in ('0', '601', '-1'):
                with self.subTest(option=option, value=value):
                    self.assertEqual(self.run_helper('reboot', option, value).returncode, 64)
        self.assertEqual(self.run_helper('reboot', '--recovery-wait', '601').returncode, 64)
        self.assertEqual(self.recorded(), [])


if __name__ == '__main__':
    unittest.main()
