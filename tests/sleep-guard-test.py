#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The sleep guard freezes both sides before the machine sleeps and thaws only what it froze.

Three layers. The first runs the guard's state machine in this process against a fake manager, a fake lock and a fake
source of PrepareForSleep events: it freezes the sides that run, skips a side that is frozen already (Guest's
standby), lets go of the lock after the freezing and also when the freezing fails or runs out of time, thaws exactly
the units it froze, retries a thaw that fails and takes the lock again after every wake. Between the announcement of a
sleep and the freezing it waits a settle, holding the lock, so that the side's Steam can take the announcement: the
tests give the fake source of events a clock for that (After), and show that nothing is frozen before the settle is
over, that the lock still goes within what logind waits for it even when a freeze is stuck, the announcement was read
late or the guard was thawing with the lock held, that a wake during the settle, or a logind that leaves the bus (it
forgets the sleep, and no wake will come), freezes nothing, that one which leaves it after the freezing gets the sides
thawed once systemd has no job to sleep and not before, that a settle of 0 is the behaviour from before and how the
waits are cut when logind waits less than 5 seconds. The second runs the real lock and the real logind monitor against
small programs that behave like systemd-inhibit and gdbus monitor. The third starts the real guard as a process with
stand-ins for gdbus, systemd-inhibit and systemctl at the front of PATH, so that the commands it runs (also the one
that asks logind how long it waits for a delay lock and the one that lists the jobs of systemd), the order of what
happens across processes, the time between the announcement and the freezing and the lines it writes to the journal are
the real ones. A test announces a sleep when the guard waits for logind (fakes.guard_listens), not when the stand-in has
logged the lock, which is before the guard knows that it has it: an announcement that reaches the guard while it takes
the lock is counted from before that, and the settle is cut. tests/sleep-guard-integration.py runs the guard against
disposable units of a real user manager.
"""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import json

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'packaging/steamos/dosplex-sleep-guard.py'
spec = importlib.util.spec_from_file_location('sleep_guard', GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
spec = importlib.util.spec_from_file_location('sleep_guard_fakes', ROOT / 'tests/fixtures/sleep-guard-fakes.py')
fakes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fakes)

LEFT = 'dosplex-session-left-' + 'a' * 32 + '.service'
RIGHT = 'dosplex-session-right-' + 'b' * 32 + '.service'


# --- The state machine, in this process ------------------------------------------------------------------

class Clock:
    """Time that moves only when the guard sleeps, or when the fakes let it pass (a wait for events, a freeze that
    does not finish)."""
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Trace(list):
    """What the fakes did, in order, and when on the clock of the test."""
    def __init__(self, clock):
        super().__init__()
        self.clock, self.times = clock, []

    def append(self, item):
        super().append(item)
        self.times.append(self.clock())

    def when(self, item):
        return self.times[self.index(item)]

    def whens(self, item):
        """When each `item` in the trace happened."""
        return [when for entry, when in zip(self, self.times) if entry == item]


class FakeSystemd:
    """A manager with units in freezer states. freeze_mode says what freezing a unit does: ok, fail (the command
    fails and nothing changes), freezing (it does not finish), stuck (it runs out of time, nothing changes), unread (the
    request reaches the manager and freezes the unit, but the command runs out of time and the state cannot be read),
    unseen (the same, but the command succeeds) or stop (the request freezes the unit and a stop signal lands before
    the guard hears of it). A freeze that does not finish takes all the time there is, up to the deadline, when the fake
    has a clock."""
    def __init__(self, trace, units, clock=None):
        self.trace, self.clock = trace, clock
        self.units = {name: {'load': 'loaded', 'active': 'active', 'freezer': freezer} for name, freezer in units.items()}
        self.freeze_mode, self.thaw_failures, self.broken = {}, {}, False
        self.deadlines = []
        self.jobs, self.jobs_broken, self.job_reads = [], False, 0     # the units that have a job to sleep
        self.jobs_error = 'Connection refused'                          # what list-jobs says when it is broken
        self.job_takes, self.thaw_takes = 0.0, 0.0      # how long list-jobs takes, and a thaw that fails, on the clock

    def side_units(self, timeout=None):
        if self.broken:
            raise guard.SystemdError('systemctl list-units: Connection refused')
        return sorted(name for name, unit in self.units.items() if unit['active'] == 'active')

    def states(self, units, timeout=None):
        none = {'load': 'not-found', 'active': 'inactive', 'freezer': 'running'}
        return {unit: dict(self.units.get(unit, none)) for unit in units}

    def sleep_jobs(self, timeout=None):
        self.job_reads += 1
        if self.clock is not None:
            self.clock.sleep(self.job_takes)
        if self.jobs_broken:
            raise guard.SystemdError('systemctl list-jobs: ' + self.jobs_error)
        return sorted(self.jobs)

    def freeze(self, units, deadline):
        self.deadlines.append(deadline)
        outcomes = {}
        for unit in units:
            mode = self.freeze_mode.get(unit, 'ok')
            if mode == 'ok':
                self.units[unit]['freezer'] = 'frozen'
                self.trace.append('freeze ' + unit)
                outcomes[unit] = guard.Outcome('frozen', '')
            elif mode == 'freezing':
                self.units[unit]['freezer'] = 'freezing'
                outcomes[unit] = guard.Outcome('freezing', 'timed out')
            elif mode in ('unread', 'unseen', 'stop'):
                self.units[unit]['freezer'] = 'frozen'
                self.trace.append('freeze ' + unit)
                if mode == 'stop':
                    raise SystemExit(0)
                outcomes[unit] = guard.Outcome(None, 'timed out' if mode == 'unread' else '')
            else:
                outcomes[unit] = guard.Outcome('running', 'Connection timed out' if mode == 'fail' else 'timed out')
        if self.clock is not None and any(self.freeze_mode.get(unit, 'ok') in ('freezing', 'stuck', 'unread', 'unseen')
                                          for unit in units):
            self.clock.now = max(self.clock.now, deadline)
        return outcomes

    def thaw(self, units, deadline):
        failed = False
        for unit in units:
            if self.thaw_failures.get(unit, 0) > 0:
                self.thaw_failures[unit] -= 1
                self.trace.append('thaw-failed ' + unit)
                failed = True
            elif unit in self.units:
                self.units[unit]['freezer'] = 'running'
                self.trace.append('thaw ' + unit)
        if failed and self.clock is not None:       # a command that fails takes its time, up to its deadline
            self.clock.now = max(self.clock.now, min(self.clock.now + self.thaw_takes, deadline))
        return {}


class FakeLock:
    def __init__(self, trace):
        self.trace, self.held, self.refuse = trace, False, False

    def acquire(self):
        if self.refuse:
            return False
        self.held = True
        self.trace.append('lock-take')
        return True

    def release(self):
        if not self.held:
            return False
        self.held = False
        self.trace.append('lock-release')
        return True


class After:
    """An item of a script: an event that comes `seconds` after the event before it, on the clock of the test."""
    def __init__(self, seconds, event):
        self.seconds, self.event = seconds, event


class FakeEvents:
    """The events logind would send. An item is an event tuple, None (nothing came for a while), 'eof' (the monitor
    ended, and stays ended until it is started again), After(seconds, event), or a function that runs between two
    events. After the last item the guard's run() returns. Every item but an After comes at once: most scripts say
    what logind announces, not when. A wait for an After that is not due yet lets the clock pass and brings nothing."""
    def __init__(self, trace, *script, clock=None):
        self.trace, self.script, self.clock = trace, list(script), clock
        self.last = clock.now if clock else 0.0
        self.delivered = []         # (time, event) of every event handed out
        # When logind sent each of them: an After fell due then, before it was handed out if the guard was busy.
        self.sent = []
        self.ended = False

    def next(self, timeout):
        while self.script and callable(self.script[0]):
            self.script.pop(0)()
        if self.ended:
            raise EOFError
        if not self.script:
            raise guard.EndOfEvents
        item = self.script[0]
        due = self.clock.now if self.clock else 0.0
        if isinstance(item, After):
            due = self.last + item.seconds
            wait = due - self.clock.now
            if wait > timeout:
                self.clock.sleep(timeout)
                return None
            self.clock.sleep(max(0.0, wait))
            item = item.event
        self.script.pop(0)
        if item == 'eof':
            self.ended = True
            raise EOFError
        if item is not None:
            self.last = self.clock.now if self.clock else 0.0
            self.delivered.append((self.last, item))
            self.sent.append(due)
        return item

    def waiting(self):
        """Whether the next event is there already: one that comes at once, or an After that has fallen due. A
        function in front of it runs only when the event is read."""
        if self.ended:
            raise EOFError
        for item in self.script:
            if callable(item):
                continue
            if isinstance(item, After):
                return self.clock is not None and self.last + item.seconds <= self.clock.now
            return item not in (None, 'eof')
        return False

    def start(self):
        self.ended = False
        self.trace.append('monitor-start')


SLEEP, WAKE = ('prepare', True), ('prepare', False)
LEAVE, APPEAR = ('logind', False), ('logind', True)


class Machine:
    """Everything a guard needs, with a trace of what it did, in order. The settle is off unless a test asks for it:
    the scripts of most tests say what logind announces, not when."""
    def __init__(self, units=None, script=(), settle=0.0, freeze=guard.FREEZE_SECONDS, state_path=None):
        self.clock = Clock()
        self.trace = Trace(self.clock)
        self.systemd = FakeSystemd(self.trace, {LEFT: 'running', RIGHT: 'running'} if units is None else units,
                                   self.clock)
        self.lock = FakeLock(self.trace)
        self.events = FakeEvents(self.trace, *script, clock=self.clock)
        self.guard = guard.Guard(self.systemd, self.lock, self.events, clock=self.clock, sleep=self.clock.sleep,
                                 settle_seconds=settle, freeze_seconds=freeze, state_path=state_path)

    def run(self):
        """Run the guard through its script; returns what it said to the journal."""
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.guard.run()
        return said.getvalue()

    def idle_pass(self):
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.guard.tick()
        return said.getvalue()

    def freezer(self, unit):
        return self.systemd.units[unit]['freezer']


class RestartedGuardTests(unittest.TestCase):
    """A guard that is killed while the sides are frozen leaves them frozen; the next one thaws them."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / 'guard.json'

    def test_a_new_guard_thaws_what_the_one_before_froze_and_not_a_side_in_standby(self):
        first = Machine({LEFT: 'running', RIGHT: 'running'}, [SLEEP], state_path=self.state)
        first.run()
        self.assertEqual((first.freezer(LEFT), first.freezer(RIGHT)), ('frozen', 'frozen'))
        self.assertTrue(self.state.exists())    # the first guard ends here without a wake, as when it is killed
        self.state.write_text(json.dumps({'frozen': [LEFT]}))
        # The side on the right went into standby meanwhile: frozen on purpose, and not in the file.
        second = Machine({LEFT: 'frozen', RIGHT: 'frozen'}, [None], state_path=self.state)
        said = second.run()
        self.assertEqual((second.freezer(LEFT), second.freezer(RIGHT)), ('running', 'frozen'))
        self.assertIn('%s was frozen by an earlier guard; thawing it' % LEFT, said)
        self.assertNotIn('thaw ' + RIGHT, second.trace)
        self.assertFalse(self.state.exists())

    def test_the_file_names_what_was_frozen_before_the_freeze_and_goes_with_the_wake(self):
        machine = Machine(script=[SLEEP, WAKE], state_path=self.state)
        seen = []
        original = machine.systemd.freeze
        machine.systemd.freeze = lambda units, deadline: (seen.append(json.loads(self.state.read_text())), original(units, deadline))[1]
        machine.run()
        self.assertEqual(seen, [{'frozen': [LEFT, RIGHT]}])
        self.assertFalse(self.state.exists())

    def test_a_unit_in_the_file_that_runs_again_is_left_alone(self):
        self.state.write_text(json.dumps({'frozen': [LEFT]}))
        machine = Machine({LEFT: 'running', RIGHT: 'frozen'}, [None], state_path=self.state)
        machine.run()
        self.assertEqual(machine.trace, ['lock-take'])
        self.assertFalse(self.state.exists())


class StateMachineTests(unittest.TestCase):
    def test_a_sleep_freezes_the_sides_before_the_lock_goes_and_a_wake_thaws_them_and_takes_it_again(self):
        machine = Machine(script=[SLEEP, WAKE])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())
        self.assertTrue(machine.lock.held)
        for line in ('froze ' + LEFT, 'froze ' + RIGHT, 'released the sleep delay lock', 'thawed ' + LEFT,
                     'thawed ' + RIGHT, 'took the sleep delay lock again'):
            self.assertIn('dosplex-sleep-guard: ' + line, said)

    def test_a_side_that_is_frozen_already_is_left_alone_and_never_thawed(self):
        # Guest's standby freezes its unit; the guard must neither freeze it again nor thaw it after the wake.
        machine = Machine({LEFT: 'running', RIGHT: 'frozen'}, [SLEEP, WAKE])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'lock-release', 'thaw ' + LEFT, 'lock-take'])
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'frozen'))
        self.assertIn('left %s alone: it is frozen already' % RIGHT, said)
        self.assertNotIn('thawed ' + RIGHT, said)

    def test_a_unit_in_the_middle_of_freezing_or_thawing_is_left_alone_too(self):
        for state in ('freezing', 'thawing'):
            with self.subTest(state=state):
                machine = Machine({LEFT: 'running', RIGHT: state}, [SLEEP, WAKE])
                machine.run()
                self.assertNotIn('freeze ' + RIGHT, machine.trace)
                self.assertNotIn('thaw ' + RIGHT, machine.trace)
                self.assertEqual(machine.freezer(RIGHT), state)

    def test_a_unit_whose_freezer_state_systemd_does_not_report_is_left_alone(self):
        # A systemd older than 246 has no FreezerState: there is no telling whether the supervisor froze the unit.
        machine = Machine({LEFT: 'running', RIGHT: ''}, [SLEEP, WAKE])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'lock-release', 'thaw ' + LEFT, 'lock-take'])
        self.assertIn('left %s alone: systemd did not say what state it is in' % RIGHT, said)

    def test_a_failed_freeze_still_releases_the_lock_and_only_what_was_frozen_is_thawed(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.freeze_mode[RIGHT] = 'fail'
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'lock-release', 'thaw ' + LEFT, 'lock-take'])
        self.assertIn('could not freeze %s: Connection timed out' % RIGHT, said)
        self.assertIn('released the sleep delay lock after', said)
        self.assertIn('1 of 2 sides did not freeze', said)
        self.assertEqual(machine.guard.ours, set())

    def test_a_freeze_that_does_not_finish_in_time_still_releases_the_lock_and_is_thawed_later(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.freeze_mode[LEFT] = 'freezing'
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + RIGHT, 'lock-release', 'thaw ' + LEFT,
                                         'thaw ' + RIGHT, 'lock-take'])
        self.assertIn('could not finish freezing %s: it is freezing' % LEFT, said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))

    def test_a_freeze_that_ran_out_of_time_and_changed_nothing_is_not_thawed(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.freeze_mode[LEFT] = 'stuck'
        said = machine.run()
        self.assertNotIn('thaw ' + LEFT, machine.trace)
        self.assertIn('could not freeze %s: timed out' % LEFT, said)
        self.assertIn('lock-release', machine.trace)

    def test_a_freeze_whose_command_ran_out_of_time_and_whose_state_could_not_be_read_is_thawed_later(self):
        # systemd is busy: the command is killed at the deadline and the one read after it fails, and then systemd
        # carries out the freeze it was asked for. The unit is frozen though nothing the guard saw says so. So is the
        # other one, whose command succeeded and whose state could not be read either: both are ours.
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.freeze_mode[LEFT] = 'unread'
        machine.systemd.freeze_mode[RIGHT] = 'unseen'
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertIn('could not finish freezing %s: it is in an unknown state (timed out)\n' % LEFT, said)
        self.assertIn('could not finish freezing %s: it is in an unknown state\n' % RIGHT, said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())

    def test_a_stop_while_the_freezing_is_waited_for_still_thaws_the_unit_that_was_asked(self):
        # The stop lands after the request reached systemd and before the guard heard that it was done.
        machine = Machine(script=[SLEEP])
        machine.systemd.freeze_mode[LEFT] = 'stop'
        said = io.StringIO()
        with contextlib.redirect_stderr(said), self.assertRaises(SystemExit):
            machine.guard.run()
        self.assertEqual(machine.freezer(LEFT), 'frozen')
        self.assertFalse(machine.lock.held, 'the lock goes, whatever stops the guard')
        with contextlib.redirect_stderr(said):
            machine.guard.shutdown()
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())

    def test_a_freeze_that_failed_and_was_seen_running_is_not_ours_but_one_that_could_not_be_read_is(self):
        # The only evidence that a unit was not frozen is a read that finds it running.
        for outcome, ours in ((guard.Outcome('running', 'Failed to freeze unit: refused'), False),
                              (guard.Outcome('running', 'timed out'), False),
                              (guard.Outcome('running', ''), False),
                              (guard.Outcome(None, 'timed out'), True),
                              (guard.Outcome(None, 'Failed to freeze unit: Connection timed out'), True),
                              (guard.Outcome(None, ''), True),
                              (guard.Outcome('freezing', 'timed out'), True),
                              (guard.Outcome('thawing', ''), True),
                              (guard.Outcome('', ''), True)):
            with self.subTest(state=outcome.state, error=outcome.error):
                machine = Machine(units={LEFT: 'running'}, script=[SLEEP])
                machine.systemd.freeze = lambda units, deadline: {unit: outcome for unit in units}
                said = machine.run()
                self.assertEqual(machine.guard.ours, {LEFT} if ours else set())
                self.assertIn('could not %s %s' % ('finish freezing' if ours else 'freeze', LEFT), said)

    def test_a_freeze_that_raises_leaves_the_units_ours_and_the_lock_is_released(self):
        machine = Machine(script=[SLEEP, WAKE])

        def broken(units, deadline):
            raise RuntimeError('unexpected')
        machine.systemd.freeze = broken
        said = machine.run()
        self.assertIn('freezing failed: RuntimeError: unexpected', said)
        self.assertEqual(said.count('it is in an unknown state'), 2)
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])

    def test_the_lock_is_released_when_systemd_cannot_be_read(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.broken = True
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        self.assertIn('could not read the units of the sides: systemctl list-units: Connection refused', said)

    def test_the_lock_is_released_after_an_error_nobody_expected(self):
        machine = Machine(script=[SLEEP, WAKE])

        def broken(units, timeout=None):
            raise RuntimeError('unexpected')
        machine.systemd.states = broken
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        self.assertIn('could not freeze the sides: RuntimeError: unexpected', said)
        self.assertIn('released the sleep delay lock after an error', said)

    def test_the_wait_for_the_freezing_ends_well_before_logind_stops_waiting(self):
        # logind lets a delay lock hold a sleep for InhibitDelayMaxSec, 5 seconds unless configured otherwise.
        machine = Machine(script=[SLEEP])
        start = machine.clock()
        machine.run()
        self.assertLess(guard.FREEZE_SECONDS, 5)
        self.assertLess(guard.MAX_FREEZE_SECONDS, 5)
        self.assertEqual(machine.systemd.deadlines, [start + guard.FREEZE_SECONDS])

    def test_a_second_sleep_before_a_wake_neither_freezes_again_nor_forgets_what_it_froze(self):
        machine = Machine(script=[SLEEP, SLEEP, WAKE])
        said = machine.run()
        self.assertEqual(machine.trace.count('freeze ' + LEFT), 1)
        self.assertEqual(machine.trace.count('thaw ' + LEFT), 1)
        self.assertIn('%s is still frozen from the sleep before' % LEFT, said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))

    def test_a_unit_of_ours_that_runs_again_by_the_next_sleep_is_frozen_for_it(self):
        # After a wake LEFT's thaw is not confirmed in the whole retry (systemd is busy), so LEFT stays ours. Then
        # systemd carries out the thaw it was asked for, unseen, and the next sleep comes. LEFT is read as running and
        # has to be frozen for it, whatever the guard has in `ours`.
        def systemd_catches_up():
            machine.systemd.thaw_failures[LEFT] = 0
            machine.systemd.units[LEFT]['freezer'] = 'running'
        for settle, script in ((0, [SLEEP, WAKE, systemd_catches_up, SLEEP, After(60, WAKE)]),
                               (1.5, [SLEEP, After(3, WAKE), After(11, SLEEP), systemd_catches_up, After(60, WAKE)])):
            with self.subTest(settle=settle):
                machine = Machine(settle=settle, script=script)
                machine.systemd.thaw_failures[LEFT] = 1000
                said = machine.run()
                freezes = [i for i, item in enumerate(machine.trace) if item == 'freeze ' + LEFT]
                releases = [i for i, item in enumerate(machine.trace) if item == 'lock-release']
                self.assertEqual(len(freezes), 2, 'once for each sleep')
                self.assertLess(freezes[1], releases[-1], 'LEFT was frozen before the lock went')
                self.assertNotIn('%s is still frozen from the sleep before' % LEFT, said)
                self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
                self.assertEqual(machine.guard.ours, set())

    def test_a_unit_of_ours_that_is_thawing_is_not_frozen_again(self):
        # A thaw is going on: the unit is not running yet, so it is not frozen again, and it stays ours until it is.
        def systemd_is_thawing():
            machine.systemd.thaw_failures[LEFT] = 0
            machine.systemd.units[LEFT]['freezer'] = 'thawing'
        machine = Machine(script=[SLEEP, WAKE, systemd_is_thawing, SLEEP, WAKE])
        machine.systemd.thaw_failures[LEFT] = 1000
        said = machine.run()
        self.assertEqual(machine.trace.count('freeze ' + LEFT), 1)
        self.assertIn('%s is still thawing from the sleep before' % LEFT, said)
        self.assertNotIn('%s is still frozen' % LEFT, said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())

    def test_a_wake_that_follows_no_sleep_thaws_nothing_and_takes_the_lock(self):
        machine = Machine({LEFT: 'running', RIGHT: 'frozen'}, [WAKE])
        machine.run()
        self.assertEqual(machine.trace, ['lock-take'])
        self.assertEqual(machine.freezer(RIGHT), 'frozen')

    def test_every_wake_arms_the_next_sleep(self):
        machine = Machine(script=[SLEEP, WAKE, SLEEP, WAKE, SLEEP])
        machine.run()
        self.assertEqual(machine.trace.count('lock-take'), 3)
        self.assertEqual(machine.trace.count('lock-release'), 3)
        self.assertEqual(machine.trace.count('freeze ' + LEFT), 3)
        self.assertFalse(machine.lock.held)

    def test_a_thaw_that_fails_is_tried_again_and_reported_only_when_it_worked(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.systemd.thaw_failures[LEFT] = 2
        said = machine.run()
        self.assertEqual(machine.trace.count('thaw-failed ' + LEFT), 2)
        self.assertEqual(machine.trace.count('thaw ' + LEFT), 1)
        self.assertEqual(said.count('thawed ' + LEFT), 1)
        self.assertEqual(machine.freezer(LEFT), 'running')

    def test_a_unit_that_stays_frozen_is_kept_and_tried_again_when_the_guard_is_idle(self):
        machine = Machine(script=[SLEEP, WAKE, None])
        machine.systemd.thaw_failures[LEFT] = 1000
        said = machine.run()
        self.assertEqual(machine.guard.ours, {LEFT})
        self.assertIn('could not thaw %s yet; trying again' % LEFT, said)
        self.assertNotIn('thawed ' + LEFT, said)
        machine.systemd.thaw_failures[LEFT] = 0
        machine.events = FakeEvents(machine.trace, None, clock=machine.clock)
        machine.guard.events = machine.events
        said = machine.run()
        self.assertIn('thawed ' + LEFT, said)
        self.assertEqual(machine.guard.ours, set())

    def test_an_idle_pass_does_not_thaw_while_the_machine_is_going_to_sleep(self):
        # Between PrepareForSleep(true) and the kernel's freezing, other delay locks can hold the sleep for seconds.
        machine = Machine(script=[SLEEP, None, None])
        machine.run()
        self.assertEqual(machine.guard.ours, {LEFT, RIGHT})
        self.assertNotIn('thaw ' + LEFT, machine.trace)

    def test_a_unit_that_is_gone_after_the_wake_is_dropped_without_a_thaw(self):
        machine = Machine(script=[SLEEP, lambda: machine.systemd.units.pop(RIGHT), WAKE])
        said = machine.run()
        self.assertIn('%s is gone; nothing to thaw' % RIGHT, said)
        self.assertNotIn('thaw ' + RIGHT, machine.trace)
        self.assertEqual(machine.guard.ours, set())

    def test_a_sleep_is_prepared_even_without_the_lock_and_the_lock_is_taken_again_when_idle(self):
        machine = Machine(script=[SLEEP, WAKE])
        machine.lock.refuse = True
        said = machine.run()
        self.assertEqual(machine.trace, ['freeze ' + LEFT, 'freeze ' + RIGHT, 'thaw ' + LEFT, 'thaw ' + RIGHT])
        self.assertIn('the sleep delay lock was not held, so logind does not wait for this guard', said)
        machine.lock.refuse = False
        machine.clock.now += guard.LOCK_RETRY_SECONDS + 1
        machine.idle_pass()
        self.assertTrue(machine.lock.held)

    def test_the_lock_is_not_asked_for_again_before_the_retry_time(self):
        machine = Machine(script=[None])
        machine.lock.refuse = True
        machine.run()
        machine.lock.refuse = False
        machine.idle_pass()
        self.assertFalse(machine.lock.held)
        machine.clock.now += guard.LOCK_RETRY_SECONDS + 1
        machine.idle_pass()
        self.assertTrue(machine.lock.held)

    def test_when_the_guard_ends_it_thaws_what_it_froze_and_lets_the_lock_go(self):
        machine = Machine(script=[SLEEP])
        machine.run()
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('frozen', 'frozen'))
        machine.lock.held = True
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            machine.guard.shutdown()
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertFalse(machine.lock.held)
        self.assertIn('released the sleep delay lock (the guard ends)', said.getvalue())

    def test_an_error_in_one_event_does_not_end_the_guard(self):
        machine = Machine(script=[('logind', True), SLEEP, WAKE])
        original = machine.lock.release
        calls = []

        def broken():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            return original()
        machine.lock.release = broken
        said = machine.run()
        self.assertIn('error in event (\'logind\', True): RuntimeError: boom', said)
        self.assertIn('froze ' + LEFT, said)

    def test_a_monitor_that_ends_is_started_again_and_what_was_frozen_is_thawed(self):
        machine = Machine(script=[SLEEP, 'eof'])
        said = machine.run()
        self.assertEqual(machine.trace[-4:], ['monitor-start', 'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertIn('the logind monitor ended; starting it again in 1 s', said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertTrue(machine.lock.held)

    def test_a_logind_that_comes_back_gets_a_new_lock(self):
        machine = Machine(script=[('logind', False), ('logind', True)])
        machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])


def stop_the_guard():
    raise SystemExit(0)


def break_the_wait():
    raise RuntimeError('boom')


class SettleTests(unittest.TestCase):
    """The wait between the announcement of a sleep and the freezing, in which the sides' Steam takes the announcement.
    The scripts say when logind speaks (After); the clock moves only as the guard waits."""

    def test_nothing_is_frozen_before_the_settle_is_over_and_the_lock_is_held_meanwhile(self):
        seen = []
        machine = Machine(settle=1.5, script=[
            SLEEP, lambda: seen.append((machine.clock(), list(machine.trace), machine.lock.held)), After(3, WAKE)])
        said = machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(seen, [(announced, ['lock-take'], True)],
                         'the settle began with the lock held and nothing frozen')
        for unit in (LEFT, RIGHT):
            self.assertEqual(machine.trace.when('freeze ' + unit), announced + 1.5)
        self.assertGreaterEqual(machine.trace.when('lock-release'), announced + 1.5)
        self.assertEqual(machine.systemd.deadlines, [announced + 1.5 + guard.FREEZE_SECONDS])
        positions = [said.index('dosplex-sleep-guard: ' + text) for text in (
            'PrepareForSleep(true)', 'waiting 1.5 s for the sides to take the sleep announcement', 'froze ' + LEFT,
            'released the sleep delay lock (froze 2 units in 0.00 s)')]
        self.assertEqual(positions, sorted(positions))

    def test_a_stuck_freeze_still_lets_the_lock_go_within_what_logind_waits(self):
        for mode in ('stuck', 'freezing'):
            with self.subTest(mode=mode):
                machine = Machine(settle=guard.SETTLE_SECONDS, script=[SLEEP, After(10, WAKE)])
                machine.systemd.freeze_mode[LEFT] = mode
                machine.run()
                announced = machine.events.delivered[0][0]
                held = machine.trace.when('lock-release') - announced
                self.assertEqual(held, guard.SETTLE_SECONDS + guard.FREEZE_SECONDS,
                                 'the stuck unit gets all the time there is')
                self.assertLessEqual(held, 5.0 - guard.DELAY_MARGIN_SECONDS,
                                     'logind waits 5 s unless it is told otherwise')
                self.assertLess(machine.trace.index('freeze ' + RIGHT), machine.trace.index('lock-release'))

    def test_a_settle_that_ran_long_does_not_move_the_freeze_deadline(self):
        # The clock runs a second on inside the settle, as when the guard comes back to its wait late. The freeze cap
        # still counts from the announcement: the lock goes when the settle and the cap are over, not a second later.
        machine = Machine(settle=1.5, script=[SLEEP, lambda: machine.clock.sleep(1.0), After(10, WAKE)])
        machine.systemd.freeze_mode[LEFT] = 'stuck'
        machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(machine.systemd.deadlines, [announced + 1.5 + guard.FREEZE_SECONDS])
        self.assertLessEqual(machine.trace.when('lock-release') - announced, 1.5 + guard.FREEZE_SECONDS)

    def test_a_wake_during_the_settle_freezes_nothing_and_the_lock_is_taken_again(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(0.4, WAKE)])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        self.assertEqual(machine.systemd.deadlines, [], 'nothing was asked to freeze')
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())
        self.assertTrue(machine.lock.held)
        self.assertFalse(machine.guard.asleep)
        (announced, _), (woke, _) = machine.events.delivered
        self.assertAlmostEqual(woke - announced, 0.4)
        for line in ('waiting 1.5 s for the sides to take the sleep announcement',
                     'released the sleep delay lock (logind announced the wake after 0.4 s; nothing was frozen)',
                     'PrepareForSleep(false): the system woke up', 'took the sleep delay lock again'):
            self.assertIn('dosplex-sleep-guard: ' + line, said)
        for unit in (LEFT, RIGHT):
            self.assertNotIn('froze ' + unit, said)

    def test_a_side_that_is_frozen_already_is_still_left_alone(self):
        for wake_after in (3, 0.4):
            with self.subTest(wake_after=wake_after):
                machine = Machine({LEFT: 'running', RIGHT: 'frozen'}, [SLEEP, After(wake_after, WAKE)], settle=1.5)
                machine.run()
                self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'frozen'))
                self.assertEqual([item for item in machine.trace if item.endswith(RIGHT)], [])

    def test_a_wake_that_came_in_while_the_guard_was_stalled_is_seen_before_anything_is_frozen(self):
        # The clock ran on by 5 s in the middle of the wait, as after a stall of the whole machine, and the wake is
        # in the queue when the time is up.
        machine = Machine(settle=1.5, script=[SLEEP, lambda: machine.clock.sleep(5), None, WAKE])
        machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        self.assertEqual(machine.systemd.deadlines, [])

    def test_a_settle_of_0_is_the_behaviour_from_before(self):
        seen = []
        machine = Machine(settle=0, script=[SLEEP, lambda: seen.append(list(machine.trace)), WAKE])
        said = machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(seen, [['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release']],
                         'the events were not read before the sides were frozen and the lock let go')
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual(machine.trace.when('freeze ' + LEFT), announced)
        self.assertEqual(machine.systemd.deadlines, [announced + guard.FREEZE_SECONDS])
        self.assertNotIn('waiting', said)
        self.assertIn('released the sleep delay lock (froze 2 units in 0.00 s)', said)

    def test_without_the_lock_there_is_no_settle(self):
        # logind does not wait for a guard that holds no lock: every moment counts then.
        machine = Machine(settle=1.5, script=[SLEEP, WAKE])
        machine.lock.refuse = True
        said = machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(machine.trace, ['freeze ' + LEFT, 'freeze ' + RIGHT, 'thaw ' + LEFT, 'thaw ' + RIGHT])
        self.assertEqual(machine.trace.when('freeze ' + LEFT), announced)
        self.assertNotIn('waiting', said)

    def test_a_logind_that_leaves_the_bus_during_the_settle_calls_the_sleep_off(self):
        # A logind that restarts has forgotten the sleep it announced, so no wake will ever come. Freezing at the end of
        # the settle would leave both sides frozen with the machine awake, and nothing would thaw them.
        machine = Machine(settle=1.5, script=[SLEEP, After(0.3, ('logind', False)), After(0.2, ('logind', True)), None])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        self.assertEqual(machine.systemd.deadlines, [], 'nothing was asked to freeze')
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())
        self.assertFalse(machine.guard.asleep, 'no wake will come to end the sleep')
        self.assertTrue(machine.lock.held)
        for _ in range(3):
            self.assertEqual(machine.idle_pass(), '', 'the idle passes after it have nothing to do')
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        positions = [said.index('dosplex-sleep-guard: ' + text) for text in (
            'waiting 1.5 s for the sides to take the sleep announcement',
            'released the sleep delay lock (logind left the bus after 0.3 s, which calls the sleep off; nothing was '
            'frozen)',
            'logind left the bus\n', 'took the sleep delay lock again')]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('froze ', said)

    def test_a_logind_that_appears_on_the_bus_does_not_call_the_sleep_off(self):
        # Its arrival alone says nothing about the sleep: a monitor that was slow to start says it late. A restart
        # shows in what comes first, logind leaving.
        machine = Machine(settle=1.5, script=[SLEEP, After(0.3, ('logind', True)), After(5, WAKE)])
        machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release', 'lock-take',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT])
        self.assertEqual(machine.trace.when('freeze ' + LEFT), announced + 1.5)

    def test_the_events_that_come_after_the_call_off_are_handled_in_order(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(0.2, ('logind', False)), After(0.2, ('logind', True)),
                                              After(0.2, WAKE)])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])
        positions = [said.index('dosplex-sleep-guard: ' + text) for text in (
            'released the sleep delay lock (logind left', 'logind left the bus\n', 'took the sleep delay lock again',
            'PrepareForSleep(false): the system woke up')]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(said.count('took the sleep delay lock again'), 1, 'the wake finds the lock held')

    def test_the_announcements_that_come_during_the_settle_are_kept_and_handled_in_order_after_the_freezing(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(0.2, SLEEP), After(0.2, SLEEP), After(5, WAKE)])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual(said.count('waiting 1.5 s'), 1)
        self.assertEqual(said.count('is still frozen from the sleep before'), 4, 'two announcements, two sides each')
        self.assertLess(said.rindex('is still frozen from the sleep before'), said.index('PrepareForSleep(false)'))

    def test_a_second_announcement_during_the_settle_neither_waits_again_nor_freezes_twice(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(0.2, SLEEP), After(5, WAKE)])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual(said.count('waiting 1.5 s'), 1)
        self.assertIn('%s is still frozen from the sleep before' % LEFT, said)

    def test_a_monitor_that_ends_during_the_settle_does_not_cut_the_settle_short(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(0.3, 'eof')])
        said = machine.run()
        announced = machine.events.delivered[0][0]
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'monitor-start', 'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual(machine.trace.when('freeze ' + LEFT), announced + 1.5)
        self.assertIn('the logind monitor ended; starting it again', said)

    def test_a_stop_during_the_settle_lets_the_lock_go_and_freezes_nothing(self):
        machine = Machine(settle=1.5, script=[SLEEP, stop_the_guard])
        said = io.StringIO()
        with contextlib.redirect_stderr(said), self.assertRaises(SystemExit):
            machine.guard.run()
        self.assertEqual(machine.trace, ['lock-take', 'lock-release'])
        self.assertEqual(machine.systemd.deadlines, [])
        self.assertIn('released the sleep delay lock (stopped during the settle; nothing was frozen)', said.getvalue())
        self.assertNotIn('after an error', said.getvalue(), 'a stop is not an error')

    def test_a_wait_that_goes_wrong_does_not_keep_the_sides_from_being_frozen(self):
        machine = Machine(settle=1.5, script=[SLEEP, break_the_wait, After(5, WAKE)])
        said = machine.run()
        announced = machine.events.delivered[0][0]
        self.assertIn('could not wait for the sides: RuntimeError: boom', said)
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT, 'lock-take'])
        self.assertEqual(machine.trace.when('freeze ' + LEFT), announced)


class LostWakeTests(unittest.TestCase):
    """logind leaves the bus after the sides were frozen and the lock let go. A logind that restarts has forgotten the
    sleep it announced, so it never announces the wake. The sides are thawed once systemd has no job to sleep: that is
    when the machine is awake, and a sleep that is going on has one."""

    def assertAwake(self, machine):
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())
        self.assertFalse(machine.guard.asleep)
        self.assertTrue(machine.lock.held)

    def test_a_logind_that_leaves_the_bus_after_the_settle_does_not_leave_the_sides_frozen(self):
        # Where the leave falls decides whether the sleep is called off or the sides are thawed after it, not whether
        # they stay frozen: 1.4 s is in the settle, 1.6 s and the rest are after the freezing.
        for leaves_after in (1.4, 1.6, 2.0, 4.0, 10.0):
            with self.subTest(leaves_after=leaves_after):
                machine = Machine(settle=1.5, script=[SLEEP, After(leaves_after, LEAVE), After(0.2, APPEAR), None])
                machine.run()
                self.assertAwake(machine)
                for _ in range(3):
                    self.assertEqual(machine.idle_pass(), '', 'the idle passes after it have nothing to do')

    def test_the_sides_are_thawed_when_systemd_has_no_job_to_sleep_and_the_lock_is_taken_again(self):
        machine = Machine(settle=1.5, script=[SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), None])
        said = machine.run()
        self.assertEqual(machine.trace, ['lock-take', 'freeze ' + LEFT, 'freeze ' + RIGHT, 'lock-release', 'lock-take',
                                         'thaw ' + LEFT, 'thaw ' + RIGHT])
        positions = [said.index('dosplex-sleep-guard: ' + text) for text in (
            'released the sleep delay lock (froze 2 units', 'logind left the bus after announcing a sleep',
            'took the sleep delay lock again',
            'no PrepareForSleep(false) will come, and systemd has no job to sleep', 'thawed ' + LEFT,
            'thawed ' + RIGHT)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(said.count('took the sleep delay lock again'), 1)

    def test_a_logind_that_leaves_while_the_freezing_is_stuck_does_not_leave_the_sides_frozen(self):
        # The leave comes at 2 s, while the guard waits for LEFT, and is read at 4.5 s, after the lock went.
        machine = Machine(settle=1.5, script=[SLEEP, After(2.0, LEAVE), After(0.2, APPEAR), None])
        machine.systemd.freeze_mode[LEFT] = 'stuck'
        said = machine.run()
        (_, _), (left, _), _ = machine.events.delivered
        self.assertGreaterEqual(left, machine.trace.when('lock-release'), 'the guard read the leave after the release')
        self.assertAwake(machine)
        self.assertIn('thawed ' + RIGHT, said)

    def test_the_sides_stay_frozen_while_systemd_has_a_job_to_sleep(self):
        # logind began the sleep as it saw the lock go and is gone since: the machine is asleep, or about to be.
        seen = []
        machine = Machine(settle=1.5, script=[
            SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), None, None,
            lambda: seen.append((machine.freezer(LEFT), machine.freezer(RIGHT), list(machine.trace))),
            lambda: machine.systemd.jobs.clear(), None])
        machine.systemd.jobs = ['suspend.target', 'systemd-suspend.service']
        machine.run()
        self.assertEqual(machine.systemd.job_reads, 3, 'asked at every idle pass')
        (left, right, trace), = seen
        self.assertEqual((left, right), ('frozen', 'frozen'))
        self.assertNotIn('thaw ' + LEFT, trace)
        self.assertAwake(machine)

    def test_jobs_that_cannot_be_read_are_asked_for_again_at_every_idle_pass_and_said_once(self):
        # A systemd that keeps failing would fill the journal with a line a second, and the line says nothing new.
        machine = Machine(settle=1.5, script=[
            SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), None, None, None, None, None,
            lambda: setattr(machine.systemd, 'jobs_broken', False), None])
        machine.systemd.jobs_broken = True
        said = machine.run()
        self.assertEqual(machine.systemd.job_reads, 6, 'five that failed and one that worked')
        self.assertEqual(said.count('could not read the jobs of systemd: systemctl list-jobs: Connection refused'), 1)
        self.assertEqual(machine.trace.count('thaw ' + LEFT), 1)
        self.assertAwake(machine)

    def test_jobs_that_cannot_be_read_are_said_again_when_the_words_change_or_a_read_worked_between(self):
        def breaks(error=None):
            def step():
                machine.systemd.jobs_broken = True
                machine.systemd.jobs_error = error or machine.systemd.jobs_error
            return step

        def works():
            machine.systemd.jobs_broken = False
        machine = Machine(settle=1.5, script=[
            SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), breaks('Connection refused'), None, None,
            breaks('Connection timed out'), None, None,
            works, lambda: machine.systemd.jobs.append('suspend.target'), None,
            breaks(), None, None,
            works, lambda: machine.systemd.jobs.clear(), None])
        said = machine.run()
        line = 'could not read the jobs of systemd: systemctl list-jobs: '
        self.assertEqual(said.count(line + 'Connection refused'), 1)
        self.assertEqual(said.count(line + 'Connection timed out'), 2, 'new words, and the same again after a read')
        self.assertLess(said.index(line + 'Connection refused'), said.index(line + 'Connection timed out'))
        self.assertAwake(machine)

    def test_jobs_that_cannot_be_read_are_said_again_in_the_next_loss_of_a_wake(self):
        # The new logind announces a sleep and its wake, which ends the loss. The words of the last loss do not silence
        # the next: it is another logind that left.
        machine = Machine(settle=1.5, script=[
            SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), None, None, After(0.5, SLEEP), After(5, WAKE),
            SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), None, None,
            lambda: setattr(machine.systemd, 'jobs_broken', False), None])
        machine.systemd.jobs_broken = True
        said = machine.run()
        self.assertEqual(said.count('could not read the jobs of systemd: systemctl list-jobs: Connection refused'), 2)
        self.assertAwake(machine)

    def test_a_new_announcement_after_the_leave_is_a_sleep_whose_wake_will_come(self):
        # The new logind announces the sleep and its wake, so the idle passes in between thaw nothing.
        machine = Machine(settle=1.5, script=[SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), After(0.3, SLEEP),
                                              After(5, WAKE)])
        machine.run()
        woke = machine.events.delivered[-1][0]
        self.assertEqual(machine.trace.whens('thaw ' + LEFT), [woke])
        self.assertEqual(machine.systemd.job_reads, 0)
        self.assertAwake(machine)

    def test_a_logind_that_leaves_while_the_system_is_awake_asks_nothing_of_systemd(self):
        machine = Machine(script=[LEAVE, APPEAR, None])
        machine.run()
        self.assertEqual(machine.systemd.job_reads, 0)
        self.assertEqual(machine.trace, ['lock-take', 'lock-release', 'lock-take'])

    def restarted_logind(self, failures):
        """A sleep, then a logind that leaves 1.6 s later and is back 0.2 s after that: the guard takes the lock again
        while the sides are frozen. The first idle pass after that is at 1 s, and the new logind announces a second
        sleep 0.05 s later (the fourth event), as the guard begins to thaw. LEFT's thaw fails `failures` times."""
        machine = Machine(settle=1.5, script=[SLEEP, After(1.6, LEAVE), After(0.2, APPEAR), After(1.05, SLEEP),
                                              After(60, WAKE)])
        machine.systemd.thaw_failures[LEFT] = failures
        return machine

    def second_lock_went_after(self, machine):
        """How long after the second announcement the guard let the lock go. logind stops waiting for it 5 s after."""
        return machine.trace.whens('lock-release')[-1] - machine.events.sent[3]

    def test_a_sleep_announced_while_the_sides_are_thawed_with_the_lock_held_gets_its_lock_in_time(self):
        # The guard holds the lock it took when logind came back, so the new logind waits for it, and stops waiting
        # after 5 s. The guard reads the announcement only when the thaw lets it: the thaw is cut short, the idle passes
        # retry what is left, and the announcement counts from when the guard last looked.
        for failures in (0, 3, 6, 9):
            with self.subTest(failures=failures):
                machine = self.restarted_logind(failures)
                machine.run()
                self.assertLessEqual(self.second_lock_went_after(machine), guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
                freezes = [i for i, item in enumerate(machine.trace) if item == 'freeze ' + RIGHT]
                releases = [i for i, item in enumerate(machine.trace) if item == 'lock-release']
                self.assertEqual(len(freezes), 2, 'RIGHT was thawed on the way and goes into the second sleep frozen')
                self.assertLess(freezes[1], releases[1])
                self.assertAwake(machine)

    def test_a_thaw_with_the_lock_held_is_cut_by_the_time_the_job_list_took_too(self):
        # systemd is slow to answer: the job list takes 1.9 s, and every thaw that fails takes 1 s. The guard has been
        # away from the monitor for 1.9 s when the thaw begins, and the thaw may take what is left of its 2 s.
        machine = self.restarted_logind(failures=6)
        machine.systemd.job_takes, machine.systemd.thaw_takes = 1.9, 1.0
        machine.run()
        self.assertLessEqual(self.second_lock_went_after(machine), guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertAwake(machine)

    def test_a_job_list_that_took_nearly_all_of_the_2_seconds_leaves_one_try_of_the_thaw(self):
        # The job list takes 1.95 s and LEFT's thaw fails in 0.1 s: the 2 s are over after one try.
        machine = self.restarted_logind(failures=6)
        machine.systemd.job_takes, machine.systemd.thaw_takes = 1.95, 0.1
        machine.run()
        read = machine.events.delivered[3][0]
        tries = [when for item, when in zip(machine.trace, machine.trace.times) if item == 'thaw-failed ' + LEFT]
        self.assertEqual(len([when for when in tries if when <= read]), 1)
        self.assertAlmostEqual(read - machine.events.sent[3], 2.0)
        self.assertAwake(machine)

    def test_a_thaw_whose_commands_run_into_their_deadline_does_not_hold_up_the_next_sleep(self):
        # systemd does not answer: every thaw command is killed at its deadline, which is 5 s away at most.
        machine = self.restarted_logind(failures=2)
        machine.systemd.thaw_takes = 60.0
        machine.run()
        self.assertLessEqual(self.second_lock_went_after(machine), guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertAwake(machine)


class LateAnnouncementTests(unittest.TestCase):
    """logind waits for a delay lock from the announcement, not from when the guard gets to read it. A guard that is
    busy when the announcement comes (a thaw that is tried again after a wake) reads it late, and the settle and the
    freeze cap run out that much sooner."""

    def machine(self, busy, *, freeze_mode='ok', lock=True, second=After(0.2, SLEEP)):
        """A sleep and its wake, then a second sleep `second` says when, 0.2 s after the wake unless said otherwise.
        The thaw of LEFT fails `busy` times and is tried again a second after each, so that the guard is busy that many
        seconds after the wake. From the wake on, freezing RIGHT does what `freeze_mode` says."""
        machine = Machine(settle=guard.SETTLE_SECONDS, script=[
            SLEEP, After(3, WAKE), lambda: machine.systemd.freeze_mode.update({RIGHT: freeze_mode}), second,
            After(60, WAKE)])
        machine.systemd.thaw_failures[LEFT] = busy
        machine.lock.refuse = not lock
        return machine

    def test_the_lock_goes_within_what_logind_waits_for_it_though_the_announcement_was_read_late(self):
        machine = self.machine(busy=1, freeze_mode='stuck')
        said = machine.run()
        sent, read = machine.events.sent[2], machine.events.delivered[2][0]
        self.assertAlmostEqual(read - sent, 0.8, msg='the guard was busy when the announcement came')
        released = machine.trace.whens('lock-release')[-1]
        self.assertLessEqual(released - sent, guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertIn('the announcement came up to 1.0 s ago; counting the waits from then', said)

    def test_the_settle_is_counted_from_when_the_guard_last_looked(self):
        machine = self.machine(busy=1)
        said = machine.run()
        woke, read = machine.events.delivered[1][0], machine.events.delivered[2][0]
        self.assertAlmostEqual(read - woke, 1.0)
        self.assertAlmostEqual(machine.trace.whens('freeze ' + LEFT)[-1], woke + guard.SETTLE_SECONDS)
        self.assertIn('waiting 0.5 s for the sides to take the sleep announcement', said)
        self.assertEqual(said.count('waiting 1.5 s'), 1, 'only the first sleep had the whole settle')

    def test_a_guard_that_is_later_than_the_waits_allow_freezes_at_once_for_the_least_it_can_use(self):
        machine = self.machine(busy=6, freeze_mode='stuck')
        said = machine.run()
        woke, read = machine.events.delivered[1][0], machine.events.delivered[2][0]
        self.assertAlmostEqual(read - woke, 6.0)
        self.assertEqual(machine.trace.whens('freeze ' + LEFT)[-1], read, 'no settle: it is over')
        self.assertEqual(machine.systemd.deadlines[-1], read + guard.MIN_FREEZE_SECONDS)
        self.assertAlmostEqual(machine.trace.whens('lock-release')[-1] - read, guard.MIN_FREEZE_SECONDS)
        self.assertEqual(said.count('waiting'), 1, 'only the first sleep waited')

    def test_an_announcement_that_comes_while_the_guard_listens_is_not_late(self):
        machine = self.machine(busy=1, second=After(5, SLEEP))
        said = machine.run()
        read = machine.events.delivered[2][0]
        self.assertAlmostEqual(machine.trace.whens('freeze ' + LEFT)[-1], read + guard.SETTLE_SECONDS)
        self.assertEqual(said.count('waiting 1.5 s'), 2)
        self.assertNotIn('came up to', said)

    def test_an_announcement_that_comes_just_after_the_guard_began_to_listen_was_not_waiting(self):
        # The guard was busy for 3 s (the retried thaw of the wake), takes the lock and listens; 5 ms later logind
        # announces the sleep. It was not waiting: the settle counts from when it came, not from before the busy time.
        for later in (0.005, 0.02):
            with self.subTest(later=later):
                machine = self.machine(busy=3, second=After(3 + later, SLEEP))
                said = machine.run()
                woke, read = machine.events.delivered[1][0], machine.events.delivered[2][0]
                self.assertAlmostEqual(read - woke, 3 + later)
                self.assertAlmostEqual(machine.trace.whens('freeze ' + LEFT)[-1], read + guard.SETTLE_SECONDS)
                self.assertEqual(said.count('waiting 1.5 s'), 2, 'both sleeps had the whole settle')
                self.assertNotIn('came up to', said)

    @staticmethod
    def lock_held_at(machine, event):
        """Whether the lock was held when `event` first happened, from the takes and releases in the trace before it."""
        before = machine.trace[:machine.trace.index(event)]
        return before.count('lock-take') > before.count('lock-release')

    def test_a_thaw_after_a_wake_with_the_lock_held_does_not_hold_up_the_next_sleep(self):
        # logind appears on the bus during the settle and the guard takes the lock while the machine sleeps, so the
        # lock is held when the wake comes: a sleep announced while the sides are thawed waits for the thaw.
        machine = Machine(settle=guard.SETTLE_SECONDS, script=[SLEEP, After(0.3, APPEAR), After(5, WAKE),
                                                              After(0.5, SLEEP), After(60, WAKE)])
        machine.systemd.thaw_failures[LEFT] = 6
        said = machine.run()
        sent = machine.events.sent[3]
        self.assertTrue(self.lock_held_at(machine, 'thaw-failed ' + LEFT), 'the lock was held when the thaw began')
        released = machine.trace.whens('lock-release')[-1]
        self.assertLessEqual(released - sent, guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertIn('the announcement came up to 2.0 s ago', said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())

    def test_a_thaw_after_a_monitor_that_was_started_again_does_not_hold_up_the_next_sleep(self):
        # The wake leaves LEFT frozen: its thaw is not confirmed in the 10 s it gets (the lock is not held then). The
        # monitor ends, and when it is back systemd confirms LEFT's thaw only after 7 more tries. The lock is held then,
        # and logind announces a sleep half a second after the monitor is back.
        def monitor_ends():
            machine.systemd.thaw_failures[LEFT] = 7
            machine.events.last = machine.clock() + guard.MONITOR_RETRY_SECONDS[0]    # the sleep counts from its return
        machine = Machine(settle=guard.SETTLE_SECONDS, script=[SLEEP, After(3, WAKE), monitor_ends, 'eof',
                                                              After(0.5, SLEEP), After(60, WAKE)])
        machine.systemd.thaw_failures[LEFT] = 1000
        said = machine.run()
        self.assertTrue(self.lock_held_at(machine, 'monitor-start'), 'the lock was held when the monitor came back')
        sent = machine.events.sent[2]
        released = machine.trace.whens('lock-release')[-1]
        self.assertLessEqual(released - sent, guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertIn('the announcement came up to 2.0 s ago', said)
        self.assertEqual((machine.freezer(LEFT), machine.freezer(RIGHT)), ('running', 'running'))
        self.assertEqual(machine.guard.ours, set())

    def test_the_time_before_the_guard_runs_is_not_time_it_was_busy(self):
        # The guard is made, then its monitor is started (seconds, when that is slow), then it runs.
        machine = Machine(settle=guard.SETTLE_SECONDS, script=[SLEEP, After(10, WAKE)])
        machine.clock.sleep(5)
        said = machine.run()
        read = machine.events.delivered[0][0]
        self.assertEqual(machine.trace.whens('freeze ' + LEFT), [read + guard.SETTLE_SECONDS])
        self.assertNotIn('came up to', said)

    def test_a_monitor_that_was_started_again_has_lost_what_was_sent_before_it(self):
        # The guard looked last before the monitor ended and was away for a second starting it again: what it reads at
        # once from the new monitor cannot be that old.
        machine = Machine(settle=guard.SETTLE_SECONDS, script=[WAKE, 'eof', SLEEP, After(10, WAKE)])
        said = machine.run()
        read = machine.events.delivered[1][0]
        self.assertEqual(machine.trace.whens('freeze ' + LEFT), [read + guard.SETTLE_SECONDS])
        self.assertIn('waiting 1.5 s', said)
        self.assertNotIn('came up to', said)

    def test_without_the_lock_a_late_announcement_keeps_its_whole_time_to_freeze(self):
        # logind does not wait for a guard that holds no lock, so there is no bound to keep.
        machine = self.machine(busy=1, lock=False)
        said = machine.run()
        read = machine.events.delivered[2][0]
        self.assertEqual(machine.systemd.deadlines[-1], read + guard.FREEZE_SECONDS)
        self.assertNotIn('came up to', said)


class ListenTests(unittest.TestCase):
    """When logind sent an event, as far as the guard can tell: an event that was waiting when the guard looked again
    came while it was busy, any time since it last looked."""

    class Source:
        """A source that takes `block` seconds on the clock of the test to bring its event. The event was there before
        the read began (waiting) when it takes no time, unless the test says otherwise."""
        def __init__(self, clock, block, event, waiting=None):
            self.clock, self.block, self.event = clock, block, event
            self.there = block == 0 if waiting is None else waiting

        def waiting(self):
            return self.there and self.event is not None

        def next(self, timeout):
            self.clock.sleep(self.block)
            return self.event

    def listen(self, busy, block, event=SLEEP, waiting=None):
        """The guard looks, is busy for `busy` seconds, then listens. Returns (when it looked, what listen says, the
        guard, the time now)."""
        clock = Clock()
        watching = guard.Guard(None, None, self.Source(clock, block, event, waiting), clock=clock, sleep=clock.sleep)
        looked = clock()
        clock.sleep(busy)
        return looked, watching.listen(30), watching, clock()

    def test_an_event_that_was_waiting_came_after_the_guard_last_looked(self):
        looked, (event, sent), watching, now = self.listen(busy=2.0, block=0.0)
        self.assertEqual((event, sent), (SLEEP, looked))
        self.assertEqual(watching.looked_at, now)

    def test_an_event_that_the_guard_waited_for_came_when_it_was_read(self):
        looked, (event, sent), watching, now = self.listen(busy=2.0, block=0.5)
        self.assertEqual((event, sent), (SLEEP, now))
        self.assertEqual(watching.looked_at, now)

    def test_an_event_that_came_in_the_first_moments_of_the_wait_came_when_it_was_read(self):
        # It was not there when the wait began: how soon after that it came does not make it late.
        for block in (0.001, 0.005, 0.0099, 0.01, 0.5):
            with self.subTest(block=block):
                looked, (event, sent), watching, now = self.listen(busy=3.0, block=block)
                self.assertEqual((event, sent), (SLEEP, now))

    def test_an_event_that_was_waiting_came_after_the_guard_last_looked_however_long_the_read_takes(self):
        # A read that is preempted for a while does not make what was waiting any younger.
        for block in (0.0, 0.005, 0.02, 0.3):
            with self.subTest(block=block):
                looked, (event, sent), watching, now = self.listen(busy=3.0, block=block, waiting=True)
                self.assertEqual((event, sent), (SLEEP, looked))
                self.assertEqual(watching.looked_at, now)

    def test_when_nothing_came_the_guard_has_looked_now(self):
        looked, (event, sent), watching, now = self.listen(busy=2.0, block=0.0, event=None)
        self.assertEqual((event, sent), (None, now))
        self.assertEqual(watching.looked_at, now)


class FreezeReadTests(unittest.TestCase):
    """What Systemd.freeze reads, and when. What it reads after the deadline is time that the sleep delay lock is held
    for, and logind stops waiting for the lock after InhibitDelayMaxSec."""

    def setUp(self):
        self.clock = Clock()
        self.systemd = guard.Systemd('user', clock=self.clock, sleep=self.clock.sleep)
        self.systemd.change, self.systemd.states = self.change, self.states
        self.errors = {}            # what the freeze command of each unit says; 'timed out' for one that hangs
        self.freezers = {}          # the freezer states `systemctl show` reports for each unit, in turn; the last stays
        self.show_seconds = 0.0     # how long `systemctl show` takes; it is cut off at its timeout
        self.reads = []             # (when, units, timeout) of every read

    def change(self, verb, units, deadline):
        if any(self.errors.get(unit) == 'timed out' for unit in units):
            self.clock.now = max(self.clock.now, deadline)       # a command that hangs runs into the deadline
        return {unit: self.errors.get(unit, '') for unit in units}

    def states(self, units, timeout=1.0):
        self.reads.append((self.clock(), list(units), timeout))
        self.clock.sleep(min(timeout, self.show_seconds))
        if self.show_seconds > timeout:
            raise guard.SystemdError('systemctl show: systemctl timed out after %.1f s' % timeout)
        turns = {unit: self.freezers.get(unit, ['running']) for unit in units}
        return {unit: {'load': 'loaded', 'active': 'active', 'freezer': turn.pop(0) if len(turn) > 1 else turn[0]}
                for unit, turn in turns.items()}

    def test_a_unit_that_hangs_and_one_that_fails_are_read_together_once_after_the_deadline(self):
        self.errors = {LEFT: 'timed out', RIGHT: 'Failed to freeze unit: Connection timed out'}
        self.show_seconds = 0.4         # systemd is busy: every read runs into its timeout
        deadline = self.clock() + 4.5
        outcomes = self.systemd.freeze([LEFT, RIGHT], deadline)
        self.assertEqual([units for _, units, _ in self.reads], [[LEFT, RIGHT]], 'one read, of both')
        self.assertLessEqual(self.clock() - deadline, 0.2 + 1e-9, 'what is read after the deadline takes 0.2 s at most')
        self.assertEqual({unit: outcome.state for unit, outcome in outcomes.items()}, {LEFT: None, RIGHT: None})
        self.assertEqual({unit: outcome.error for unit, outcome in outcomes.items()},
                         {LEFT: 'timed out', RIGHT: 'Failed to freeze unit: Connection timed out'},
                         'what the guard has to take for a freeze that may have reached systemd')

    def test_units_that_froze_are_read_once(self):
        self.freezers = {LEFT: ['frozen'], RIGHT: ['frozen']}
        outcomes = self.systemd.freeze([LEFT, RIGHT], self.clock() + 3)
        self.assertEqual(len(self.reads), 1)
        self.assertEqual({unit: outcome.state for unit, outcome in outcomes.items()}, {LEFT: 'frozen', RIGHT: 'frozen'})

    def test_a_unit_that_is_still_freezing_is_read_again_and_one_that_failed_is_not(self):
        self.errors = {RIGHT: 'Failed to freeze unit: boom'}
        self.freezers = {LEFT: ['freezing', 'freezing', 'frozen'], RIGHT: ['running']}
        outcomes = self.systemd.freeze([LEFT, RIGHT], self.clock() + 3)
        self.assertEqual([units for _, units, _ in self.reads], [[LEFT, RIGHT], [LEFT], [LEFT]])
        self.assertEqual(outcomes[LEFT].state, 'frozen')
        self.assertEqual((outcomes[RIGHT].state, outcomes[RIGHT].error), ('running', 'Failed to freeze unit: boom'))

    def test_a_unit_that_never_freezes_is_read_until_the_deadline_and_no_further(self):
        self.freezers = {LEFT: ['freezing']}
        deadline = self.clock() + 1.0
        outcomes = self.systemd.freeze([LEFT], deadline)
        self.assertEqual(outcomes[LEFT].state, 'freezing')
        self.assertGreaterEqual(self.clock(), deadline)
        self.assertLessEqual(self.clock() - deadline, 0.1)


class SleepJobTests(unittest.TestCase):
    """The jobs to sleep that Systemd.sleep_jobs finds in what `systemctl list-jobs --no-legend` prints."""

    def jobs(self, out, status=0, err=''):
        with patch.object(guard, 'run_command', return_value=(status, out, err)) as run:
            return guard.Systemd('user').sleep_jobs(), run.call_args.args[0]

    def test_a_system_with_no_jobs_is_awake(self):
        found, command = self.jobs('')
        self.assertEqual(found, [])
        self.assertEqual(command, ['systemctl', '--user', 'list-jobs', '--no-legend', '--no-pager'])

    def test_the_jobs_to_sleep_are_found_by_the_name_of_the_unit(self):
        out = ('1238405 systemd-suspend.service start running\n'
               '1238404 suspend.target           start waiting\n'
               '1238403 sleep.target             stop  waiting\n'
               '    407 systemd-hibernate.service start running\n'
               '     12 hybrid-sleep.target      start waiting\n'
               '     13 hibernate.target         start waiting\n'
               '     14 suspend-then-hibernate.target start waiting\n'
               '     15 systemd-hybrid-sleep.service start running\n'
               '     16 systemd-suspend-then-hibernate.service start running\n')
        self.assertEqual(self.jobs(out)[0], sorted([
            'systemd-suspend.service', 'suspend.target', 'sleep.target', 'systemd-hibernate.service',
            'hybrid-sleep.target', 'hibernate.target', 'suspend-then-hibernate.target', 'systemd-hybrid-sleep.service',
            'systemd-suspend-then-hibernate.service']))

    def test_other_jobs_are_no_sign_of_a_sleep(self):
        out = ('1238405 %s start running\n'
               '1238404 graphical.target start waiting\n'
               '1238403 my-suspend.target start waiting\n'
               '1238402 suspend.target.wants start waiting\n'
               '1238401 systemd-suspend.service.bak start running\n'
               '1238400 sleep-guard.service restart running\n' % LEFT)
        self.assertEqual(self.jobs(out)[0], [])

    def test_a_legend_and_a_blank_line_are_no_jobs(self):
        out = 'JOB     UNIT                     TYPE  STATE\n1238405 suspend.target start waiting\n\n2 jobs listed.\n'
        self.assertEqual(self.jobs(out)[0], ['suspend.target'])

    def test_a_job_that_is_listed_twice_is_one_unit(self):
        self.assertEqual(self.jobs('1 suspend.target start waiting\n2 suspend.target stop waiting\n')[0],
                         ['suspend.target'])

    def test_an_error_is_said_with_its_own_words(self):
        with self.assertRaisesRegex(guard.SystemdError, 'systemctl list-jobs: Failed to connect to bus'):
            self.jobs('', status=1, err='Failed to connect to bus: No such file or directory\n')
        with self.assertRaisesRegex(guard.SystemdError, 'systemctl list-jobs: exit status 3'):
            self.jobs('', status=3)
        with self.assertRaisesRegex(guard.SystemdError, 'systemctl list-jobs: systemctl timed out after 2.0 s'):
            self.jobs('', status=None, err='systemctl timed out after 2.0 s')


class WaitsTests(unittest.TestCase):
    """The settle and the freeze cap against what logind waits for a delay lock (InhibitDelayMaxSec)."""

    def test_the_defaults_and_the_margin_fit_in_the_five_seconds_logind_waits_by_default(self):
        self.assertEqual((guard.SETTLE_SECONDS, guard.FREEZE_SECONDS, guard.DELAY_MARGIN_SECONDS), (1.5, 3.0, 0.5))
        self.assertEqual(guard.DELAY_MAX_FALLBACK_SECONDS, 5.0)
        self.assertLessEqual(guard.SETTLE_SECONDS + guard.FREEZE_SECONDS + guard.DELAY_MARGIN_SECONDS, 5.0)
        self.assertEqual(guard.fit_waits(guard.SETTLE_SECONDS, guard.FREEZE_SECONDS, 5.0), (1.5, 3.0))

    def test_what_fits_is_kept_as_it_is(self):
        for settle, freeze, delay_max in ((0, 3.0, 5.0), (0.5, 0.6, 5.0), (1.5, 3.0, 5.000001), (1.5, 3.0, 60.0),
                                          (1.5, 3.0, 2 ** 64 / 1e6), (3.5, 1.0, 5.0), (0.3, 0.6, 2.0)):
            self.assertEqual(guard.fit_waits(settle, freeze, delay_max), (settle, freeze), (settle, freeze, delay_max))

    def test_where_logind_waits_less_the_freeze_cap_gives_way_first_then_the_settle(self):
        # The freeze cap is cut to a second before the settle loses anything; a freeze takes a fraction of that.
        for delay_max, expected in ((4.5, (1.5, 2.5)), (3.5, (1.5, 1.5)), (3.0, (1.5, 1.0)), (2.5, (1.0, 1.0)),
                                    (2.0, (0.5, 1.0)), (1.5, (0.0, 1.0)), (1.0, (0.0, 0.5)), (0.7, (0.0, 0.2))):
            got = guard.fit_waits(1.5, 3.0, delay_max)
            self.assertAlmostEqual(got[0], expected[0], msg=delay_max)
            self.assertAlmostEqual(got[1], expected[1], msg=delay_max)

    def test_a_freeze_cap_that_was_short_to_begin_with_is_not_cut_for_the_settle(self):
        settle, freeze = guard.fit_waits(1.5, 0.6, 2.0)
        self.assertAlmostEqual(settle, 0.9)
        self.assertEqual(freeze, 0.6)

    def test_whatever_logind_waits_the_total_and_the_margin_stay_below_it(self):
        for delay_max in (0.7, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 7, 30, 1e9):
            for settle in (0, 0.5, 1.5, guard.MAX_SETTLE_SECONDS):
                for freeze in (guard.MIN_FREEZE_SECONDS, 1, 3, guard.MAX_FREEZE_SECONDS):
                    waits = guard.fit_waits(settle, freeze, delay_max)
                    context = (delay_max, settle, freeze, waits)
                    self.assertLessEqual(sum(waits) + guard.DELAY_MARGIN_SECONDS, delay_max + 1e-9, context)
                    self.assertGreaterEqual(waits[0], 0, context)
                    self.assertLessEqual(waits[0], settle, context)
                    self.assertGreaterEqual(waits[1], guard.MIN_FREEZE_SECONDS, context)
                    self.assertLessEqual(waits[1], freeze, context)

    def test_a_logind_that_waits_hardly_at_all_gets_no_settle_and_the_least_freeze_cap(self):
        for delay_max in (0, 0.3, 0.6):
            self.assertEqual(guard.fit_waits(1.5, 3.0, delay_max), (0.0, guard.MIN_FREEZE_SECONDS), delay_max)

    def test_the_options_cannot_ask_for_more_than_logind_could_give(self):
        self.assertLess(guard.MAX_SETTLE_SECONDS, 5)
        self.assertLess(guard.MAX_FREEZE_SECONDS, 5)


class DelayMaxTests(unittest.TestCase):
    """What logind says it waits for a delay lock, asked with programs that behave like `gdbus call`."""

    def ask(self, script, timeout=5.0):
        return guard.read_delay_max([sys.executable, '-c', script], timeout)

    def test_logind_answers_in_microseconds(self):
        self.assertEqual(self.ask('print("(<uint64 5000000>,)")'), (5.0, ''))
        self.assertEqual(self.ask('print("(<uint64 2500000>,)")'), (2.5, ''))
        self.assertEqual(self.ask('print("(<uint64 0>,)")'), (0.0, ''))
        self.assertGreater(self.ask('print("(<uint64 18446744073709551615>,)")')[0], 1e12)   # infinity

    def test_an_error_is_said_with_its_own_words(self):
        seconds, reason = self.ask('import sys; sys.stderr.write("Error: GDBus.Error:org.freedesktop.DBus.Error.'
                                   'UnknownProperty: no such property\\nmore\\n"); sys.exit(1)')
        self.assertIsNone(seconds)
        self.assertEqual(reason, 'Error: GDBus.Error:org.freedesktop.DBus.Error.UnknownProperty: no such property more')
        self.assertEqual(self.ask('import sys; sys.exit(3)'), (None, 'exit status 3'))

    def test_an_answer_that_is_not_a_number_is_not_taken_for_one(self):
        for text in ('(<string "soon">,)', '(<uint32 5000000>,)', '5000000', '(<uint64 five>,)', ''):
            seconds, reason = self.ask('print(%r)' % text)
            self.assertIsNone(seconds, text)
            self.assertIn('unexpected answer', reason)

    def test_no_answer_in_time_and_a_missing_program_are_said(self):
        seconds, reason = self.ask('import time; time.sleep(60)', timeout=.3)
        self.assertIsNone(seconds)
        self.assertIn('timed out after 0.3 s', reason)
        seconds, reason = guard.read_delay_max(['/nonexistent/gdbus'], 1)
        self.assertIsNone(seconds)
        self.assertIn('could not start /nonexistent/gdbus', reason)

    def test_the_question_is_the_property_of_the_manager_object_asked_on_the_system_bus(self):
        command = guard.DELAY_MAX_COMMAND
        self.assertEqual(command[:3], ['gdbus', 'call', '--system'])
        self.assertEqual(command[-3:], ['org.freedesktop.DBus.Properties.Get', 'org.freedesktop.login1.Manager',
                                        'InhibitDelayMaxUSec'])
        self.assertIn('/org/freedesktop/login1', command)
        self.assertLess(guard.DELAY_MAX_TIMEOUT, 5)      # a short wait: the guard has to be up before the next sleep


class ChooseWaitsTests(unittest.TestCase):
    """What the guard picks at start and says about it."""

    def choose(self, answer, settle=1.5, freeze=3.0):
        said = io.StringIO()
        with patch.object(guard, 'read_delay_max', return_value=answer), contextlib.redirect_stderr(said):
            chosen = guard.choose_waits(settle, freeze)
        return chosen, said.getvalue()

    def test_five_seconds_leave_the_defaults_alone_and_say_nothing(self):
        self.assertEqual(self.choose((5.0, '')), ((1.5, 3.0, 5.0), ''))

    def test_less_is_cut_and_said(self):
        chosen, said = self.choose((2.0, ''))
        self.assertEqual(chosen, (0.5, 1.0, 2.0))
        self.assertEqual(said, 'dosplex-sleep-guard: logind waits 2 s for a delay lock: cutting the settle from 1.5 s '
                               'to 0.5 s and the freeze cap from 3 s to 1 s\n')

    def test_only_what_is_cut_is_said(self):
        chosen, said = self.choose((3.0, ''))
        self.assertEqual(chosen, (1.5, 1.0, 3.0))
        self.assertEqual(said, 'dosplex-sleep-guard: logind waits 3 s for a delay lock: cutting the freeze cap from 3 s '
                               'to 1 s\n')

    def test_a_settle_of_0_stays_0_and_the_freeze_cap_is_still_kept_below_logind(self):
        chosen, said = self.choose((2.0, ''), settle=0)
        self.assertEqual(chosen, (0.0, 1.5, 2.0))
        self.assertIn('cutting the freeze cap from 3 s to 1.5 s', said)

    def test_a_logind_that_cannot_be_asked_is_taken_to_wait_five_seconds(self):
        chosen, said = self.choose((None, 'gdbus timed out after 2.0 s'))
        self.assertEqual(chosen, (1.5, 3.0, 5.0))
        self.assertEqual(said, 'dosplex-sleep-guard: could not read InhibitDelayMaxUSec from logind '
                               '(gdbus timed out after 2.0 s); assuming 5 s\n')

    def test_a_logind_that_waits_hardly_at_all_is_said_to_be_too_short_for_the_freeze(self):
        chosen, said = self.choose((0.0, ''))
        self.assertEqual(chosen, (0.0, guard.MIN_FREEZE_SECONDS, 0.0))
        self.assertIn('logind waits only 0 s for a delay lock: less than the freeze cap and a margin of 0.5 s need',
                      said)


# What logind writes on the Deck (systemd 261, GLib 2.88) each time a delay lock is taken or let go, as `gdbus monitor`
# prints it. The guard's own lock causes it, at start and after every wake, so it is in the pipe when the guard listens.
PROPERTIES_CHANGED = ("/org/freedesktop/login1: org.freedesktop.DBus.Properties.PropertiesChanged "
                      "('org.freedesktop.login1.Manager', {'DelayInhibited': <'sleep'>, 'NCurrentInhibitors': "
                      "<uint64 6>}, @as [])")


class EventLineTests(unittest.TestCase):
    """The lines of `gdbus monitor --system --dest org.freedesktop.login1`, as it prints them."""

    def test_prepare_for_sleep(self):
        line = '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)'
        self.assertEqual(guard.parse_event(line % 'true'), ('prepare', True))
        self.assertEqual(guard.parse_event(line % 'false'), ('prepare', False))

    def test_the_owner_of_the_name(self):
        self.assertEqual(guard.parse_event('The name org.freedesktop.login1 is owned by :1.5'), ('logind', True))
        self.assertEqual(guard.parse_event('The name org.freedesktop.login1 does not have an owner'), ('logind', False))

    def test_everything_else_is_ignored(self):
        for line in ('Monitoring signals on object /org/freedesktop/login1 owned by org.freedesktop.login1',
                     '/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionNew (\'4\', objectpath \'/x\')',
                     '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (true,)',
                     PROPERTIES_CHANGED, 'The name org.example.Other is owned by :1.9', ''):
            self.assertIsNone(guard.parse_event(line), line)


# --- The lock and the monitor, with programs of their own -------------------------------------------------

HOLD = ('import sys; print("held", flush=True); sys.stdin.read()')


class SleepLockTests(unittest.TestCase):
    """SleepLock against small programs that behave like `systemd-inhibit ... sh -c 'echo held; exec cat'`."""

    def lock(self, script):
        return guard.SleepLock(command=[sys.executable, '-c', script])

    def test_the_lock_is_held_once_the_program_says_so_and_ends_with_its_input(self):
        lock = self.lock(HOLD)
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.held)
        process = lock.process
        self.assertTrue(lock.acquire(), 'asking again while it is held changes nothing')
        self.assertIs(lock.process, process)
        self.assertTrue(lock.release())
        self.assertFalse(lock.held)
        self.assertIsNotNone(process.poll(), 'the program ended when its input closed')
        self.assertFalse(lock.release(), 'nothing left to release')

    def test_a_program_that_fails_is_said_with_its_own_words(self):
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertFalse(self.lock('import sys; print("Failed to inhibit: Access denied", file=sys.stderr); sys.exit(1)')
                             .acquire())
        self.assertIn('could not take the sleep delay lock: Failed to inhibit: Access denied', said.getvalue())

    def test_a_program_that_does_not_answer_is_given_up_on_and_stopped(self):
        lock = self.lock('import time; time.sleep(60)')
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertFalse(lock.acquire(timeout=.4))
        self.assertIn('could not take the sleep delay lock: no answer in 0.4 s', said.getvalue())
        self.assertFalse(lock.held)

    def test_a_program_that_is_not_there_is_said(self):
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertFalse(guard.SleepLock(command=['/nonexistent/systemd-inhibit']).acquire())
        self.assertIn('could not take the sleep delay lock: could not start /nonexistent/systemd-inhibit', said.getvalue())

    def test_a_stop_signal_while_the_lock_is_taken_leaves_nothing_running(self):
        started = []
        real = guard.subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]
        lock = self.lock('import time; time.sleep(60)')
        with patch.object(guard.subprocess, 'Popen', popen), patch.object(guard.select, 'select', side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                lock.acquire()
        self.assertIsNotNone(started[0].poll(), 'the program was ended')
        self.assertFalse(lock.held)


class LogindMonitorTests(unittest.TestCase):
    """LogindMonitor against a program that prints what `gdbus monitor` prints."""

    def monitor(self, lines, tail=''):
        script = ('import sys, time\nfor line in %r:\n    print(line, flush=True)\n%s' % (lines, tail or 'time.sleep(60)'))
        return guard.LogindMonitor(command=[sys.executable, '-c', script])

    def test_it_starts_after_the_name_of_logind_is_reported_and_then_yields_the_events_in_order(self):
        monitor = self.monitor(['Monitoring signals on object /org/freedesktop/login1 owned by org.freedesktop.login1',
                                'The name org.freedesktop.login1 is owned by :1.9',
                                '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)',
                                '/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionNew (\'1\', objectpath \'/x\')',
                                '/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)'])
        self.addCleanup(monitor.close)
        monitor.start()
        self.assertEqual(monitor.next(5), ('prepare', True))        # queued behind the banner in the same read
        self.assertEqual(monitor.next(5), ('prepare', False))
        self.assertIsNone(monitor.next(.2), 'nothing else came')

    def test_a_line_that_arrives_in_two_pieces_is_one_event(self):
        script = ('import sys, time\n'
                  'print("The name org.freedesktop.login1 is owned by :1.9", flush=True)\n'
                  'sys.stdout.write("/org/freedesktop/login1: org.freedesktop.login1.Manager.Prepare"); sys.stdout.flush()\n'
                  'time.sleep(.3)\n'
                  'sys.stdout.write("ForSleep (true,)\\n"); sys.stdout.flush()\n'
                  'time.sleep(60)\n')
        monitor = guard.LogindMonitor(command=[sys.executable, '-c', script])
        self.addCleanup(monitor.close)
        monitor.start()
        self.assertEqual(monitor.next(5), ('prepare', True))

    def waiting_monitor(self, then):
        """A monitor that says who owns logind's name, and once start() returned the line that logind writes when a lock
        is taken (PROPERTIES_CHANGED), then waits for the file `go` and runs `then` (source lines), then writes
        `written`. Returns (monitor, go, written), the line unread in the pipe."""
        directory = tempfile.TemporaryDirectory(prefix='dosplex-sleep-guard-')
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        noise, noisy, go, written = (base / name for name in ('noise', 'noisy', 'go', 'written'))
        script = '\n'.join([
            'import pathlib, sys, time',
            'def wait(path):',
            '    while not pathlib.Path(path).exists():',
            '        time.sleep(0.01)',
            'print("The name org.freedesktop.login1 is owned by :1.9", flush=True)',
            'wait(%r)' % str(noise),
            'print(%r, flush=True)' % PROPERTIES_CHANGED,
            'pathlib.Path(%r).write_text("x")' % str(noisy),
            'wait(%r)' % str(go),
            then,
            'pathlib.Path(%r).write_text("x")' % str(written),
            'time.sleep(60)'])
        monitor = guard.LogindMonitor(command=[sys.executable, '-c', script])
        self.addCleanup(monitor.close)
        monitor.start()
        noise.write_text('x')
        self.wait_for(noisy)
        return monitor, go, written

    def wait_for(self, path):
        end = time.monotonic() + 10
        while not path.exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(path.exists(), 'the monitor wrote what it was to write')

    def go(self, go, written):
        go.write_text('x')
        self.wait_for(written)

    ANNOUNCE = ('print("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)", flush=True)')

    def test_waiting_tells_that_an_event_is_there_and_leaves_it_to_be_read(self):
        monitor, go, written = self.waiting_monitor(self.ANNOUNCE)
        self.assertTrue(select.select([monitor.process.stdout], [], [], 5)[0], 'the line is there to be read')
        self.assertFalse(monitor.waiting(), 'a line that says nothing about logind is no event')
        self.assertFalse(select.select([monitor.process.stdout], [], [], 0)[0], 'and it was read, not left behind')
        self.assertFalse(monitor.waiting())
        self.go(go, written)
        self.assertTrue(monitor.waiting())
        self.assertTrue(monitor.waiting(), 'looking does not use the event up')
        self.assertEqual(monitor.next(5), ('prepare', True))
        self.assertFalse(monitor.waiting(), 'and then there is none')

    def test_an_announcement_that_comes_while_the_guard_listens_is_not_waiting_because_of_a_line_before_it(self):
        # After a wake the line about the guard's own lock is in the pipe when the guard listens, and the announcement
        # comes later. It counts from when it was read, not from when the guard last looked (3 s ago): the settle must
        # not be over before it began. The announcement is written after the guard has asked, whatever the timing.
        monitor, go, written = self.waiting_monitor(self.ANNOUNCE)
        asked = monitor.waiting

        def waiting():
            answer = asked()
            go.write_text('x')
            return answer
        monitor.waiting = waiting
        watching = guard.Guard(None, None, monitor)
        watching.looked_at = time.monotonic() - 3.0
        began = time.monotonic()
        event, sent = watching.listen(10)
        self.assertEqual(event, ('prepare', True))
        self.assertGreaterEqual(sent, began, 'it came while the guard listened')
        self.assertEqual(sent, watching.looked_at, 'dated from when it was read')

    def test_waiting_looks_past_more_lines_that_say_nothing_than_one_read_takes(self):
        noise = ('sys.stdout.write(("/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionRemoved (\'1\',)\\n")'
                 ' * 300)\nsys.stdout.flush()\n' + self.ANNOUNCE)
        monitor, go, written = self.waiting_monitor(noise)
        self.go(go, written)
        self.assertTrue(monitor.waiting(), 'the announcement is behind 25 kB of other lines')
        self.assertEqual(monitor.next(5), ('prepare', True))

    def test_waiting_has_nothing_to_tell_before_the_monitor_writes(self):
        monitor, go, written = self.waiting_monitor(self.ANNOUNCE)
        self.assertFalse(monitor.waiting())
        self.assertIsNone(monitor.next(.1))

    def test_waiting_on_a_monitor_that_ended_is_an_end_of_file_after_the_events_it_left(self):
        monitor = guard.LogindMonitor(command=[sys.executable, '-c',
            'print("The name org.freedesktop.login1 is owned by :1.9", flush=True)\n'
            'print("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)", flush=True)'])
        self.addCleanup(monitor.close)
        monitor.start()
        monitor.process.wait(timeout=10)    # start() read the first line, perhaps not the second: it is written when it ends
        self.assertTrue(monitor.waiting(), 'what it wrote before it ended is still to be read')
        self.assertEqual(monitor.next(5), ('prepare', True))
        for _ in range(1000):
            try:
                monitor.waiting()
            except EOFError:
                break
            time.sleep(0.01)
        else:
            self.fail('the end of the monitor was not seen')

    def test_a_monitor_that_ends_is_an_end_of_file(self):
        monitor = self.monitor(['The name org.freedesktop.login1 is owned by :1.9'], tail='')
        monitor.command = [sys.executable, '-c', 'print("The name org.freedesktop.login1 is owned by :1.9", flush=True)']
        monitor.start()
        with self.assertRaises(EOFError):
            monitor.next(5)
        monitor.close()

    def test_a_monitor_that_ends_at_once_cannot_be_started(self):
        monitor = guard.LogindMonitor(command=[sys.executable, '-c', 'pass'])
        with self.assertRaises(OSError):
            monitor.start()

    def test_a_monitor_that_never_names_logind_is_waited_for_only_so_long(self):
        monitor = self.monitor(['Monitoring signals on object /org/freedesktop/login1 owned by org.freedesktop.login1'])
        self.addCleanup(monitor.close)
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            monitor.start(timeout=.4)
        self.assertIn('the logind monitor said nothing about logind in 0.4 s; going on', said.getvalue())

    def test_closing_it_ends_the_program(self):
        monitor = self.monitor(['The name org.freedesktop.login1 is owned by :1.9'])
        monitor.start()
        process = monitor.process
        monitor.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(EOFError):
            monitor.next(.1)


# --- The guard as a process ------------------------------------------------------------------------------

class World:
    """A temporary directory with the stand-ins at the front of PATH, a table of fake units and the guard's journal."""

    def __init__(self, test, units=None):
        self.test = test
        self.directory = tempfile.TemporaryDirectory(prefix='dosplex-sleep-guard-')
        test.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.bin = fakes.install(base / 'bin')
        self.state, self.log, self.signals = base / 'state.json', base / 'events.log', base / 'signals'
        self.journal = base / 'journal.log'
        self.table = {LEFT: self.unit(), RIGHT: self.unit()} if units is None else units
        self.jobs = []
        self.write_state()
        self.signals.write_text('')
        self.process = None
        test.addCleanup(self.stop)

    @staticmethod
    def unit(freezer='running', **options):
        return dict(load='loaded', active='active', freezer=freezer, **options)

    def write_state(self):
        self.state.write_text(json.dumps({'units': self.table, 'jobs': self.jobs}))

    def set_jobs(self, jobs):
        """Change the jobs while the guard runs, as the fake systemctl does for the units: under its lock."""
        with open(str(self.state) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.state.read_text())
            state['jobs'] = list(jobs)
            self.state.write_text(json.dumps(state))

    def units(self):
        return json.loads(self.state.read_text())['units']

    def environment(self, **extra):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], FAKE_STATE=str(self.state),
                           FAKE_LOG=str(self.log), FAKE_SIGNALS=str(self.signals), PYTHONDONTWRITEBYTECODE='1')
        environment.pop('FAKE_INHIBIT_FAIL', None)
        environment.pop('FAKE_INHIBIT_DELAY', None)
        environment.pop('FAKE_DELAY_MAX', None)
        environment.update(extra)
        return environment

    def start(self, *arguments, **environment):
        self.log.touch()
        with self.journal.open('w') as journal:
            self.process = subprocess.Popen([sys.executable, str(GUARD), '--manager', 'user',
                                             '--state-path', str(self.state.with_name('guard-state.json')), *arguments],
                                            env=self.environment(**environment), stdout=journal, stderr=journal,
                                            stdin=subprocess.DEVNULL)
        return self.process

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def send(self, line):
        with self.signals.open('a') as stream:
            stream.write(line + '\n')

    def sleep_signal(self, active):
        self.send('/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)'
                  % ('true' if active else 'false'))

    def events(self):
        found = []
        for line in self.log.read_text().splitlines():
            when, _, what = line.partition(' ')
            found.append((int(when), what))
        return found

    def said(self):
        return self.journal.read_text()

    def wait(self, condition, what, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            if self.process is not None and self.process.poll() is not None and not condition():
                self.test.fail('the guard ended (%s) while waiting for %s\n%s' % (self.process.returncode, what, self.said()))
            time.sleep(0.02)
        self.test.fail('timed out waiting for %s\n%s\n%s' % (what, self.log.read_text(), self.said()))

    def count(self, prefix):
        return sum(1 for _, what in self.events() if what.startswith(prefix))

    def wait_until_listening(self, locks=1):
        """Wait until the guard has taken the lock `locks` times and waits for logind (see fakes.guard_listens): the
        lock-taken of the stand-in comes too soon for a test that times the settle to send the announcement at it."""
        self.wait(lambda: fakes.guard_listens(self.process, self.said(), locks), 'the guard to wait for logind')


class ListeningTests(unittest.TestCase):
    """fakes.guard_listens, which the tests that time the settle wait for before they announce a sleep: the journal
    says that the guard took the lock as often as asked, and the process sleeps. Small processes stand in for the
    guard: one that sleeps, one that runs and one that has ended."""
    SAID = 'dosplex-sleep-guard: took the sleep delay lock\n'

    def start(self, code):
        """A process that says "ready" and then runs `code`. Returns when it has said so."""
        process = subprocess.Popen([sys.executable, '-c', 'print("ready", flush=True)\n' + code],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.assertEqual(process.stdout.readline(), 'ready\n')
        return process

    def test_a_guard_that_sleeps_after_it_said_it_took_the_lock_listens_and_one_that_runs_does_not(self):
        sleeping, running = self.start('import time; time.sleep(60)'), self.start('while True: pass')
        end = time.monotonic() + 15
        while not fakes.guard_listens(sleeping, self.SAID) and time.monotonic() < end:
            time.sleep(0.02)         # the process is still on its way to sleep
        self.assertTrue(fakes.guard_listens(sleeping, self.SAID))
        self.assertFalse(fakes.guard_listens(running, self.SAID), 'a process that runs is not waiting for logind')

    def test_a_guard_that_did_not_say_it_took_the_lock_as_often_as_asked_does_not_listen(self):
        sleeping = self.start('import time; time.sleep(60)')
        end = time.monotonic() + 15
        while not fakes.guard_listens(sleeping, self.SAID) and time.monotonic() < end:
            time.sleep(0.02)
        again = self.SAID + 'dosplex-sleep-guard: took the sleep delay lock again\n'
        self.assertFalse(fakes.guard_listens(sleeping, ''))
        self.assertFalse(fakes.guard_listens(sleeping, self.SAID, locks=2))
        self.assertFalse(fakes.guard_listens(sleeping, again, locks=1))
        self.assertTrue(fakes.guard_listens(sleeping, again, locks=2))

    def test_a_guard_that_ended_does_not_listen(self):
        ended = self.start('pass')
        ended.wait(timeout=15)
        self.assertFalse(fakes.guard_listens(ended, self.SAID))


class ProcessTests(unittest.TestCase):
    def test_the_sides_are_frozen_before_the_lock_goes_and_thawed_after_the_wake(self):
        world = World(self)
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        order = [what for _, what in world.events()]
        taken = [what for what in order if what.startswith('lock-taken')]
        self.assertEqual(taken[0], 'lock-taken --what=sleep --mode=delay --who=Dosplex --why=Freeze both sides before '
                                   'the machine sleeps')
        frozen = [i for i, what in enumerate(order) if what.startswith('freeze ') and what.endswith(' ok')]
        released = order.index('lock-released')
        self.assertEqual(len(frozen), 2)
        self.assertTrue(all(i < released for i in frozen), order)
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'frozen'})
        # Gone: the guard does not wait for the wake to take the lock again.
        world.sleep_signal(False)
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        world.wait(lambda: 'took the sleep delay lock again' in world.said(), 'the guard to say it has the lock again')
        world.stop()
        said = world.said()
        for expected in ('froze ' + LEFT, 'froze ' + RIGHT, 'released the sleep delay lock', 'thawed ' + LEFT,
                         'thawed ' + RIGHT, 'took the sleep delay lock again'):
            self.assertIn(expected, said)
        self.assertEqual(world.process.returncode, 0)

    def test_a_side_in_standby_stays_frozen_through_the_sleep_and_the_wake(self):
        world = World(self)
        world.table[RIGHT]['freezer'] = 'frozen'
        world.write_state()
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        world.sleep_signal(False)
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        self.assertEqual(world.units()[RIGHT]['freezer'], 'frozen')
        self.assertEqual(world.units()[LEFT]['freezer'], 'running')
        self.assertEqual(world.count('freeze ' + RIGHT), 0)
        self.assertEqual(world.count('thaw ' + RIGHT), 0)
        self.assertIn('left %s alone: it is frozen already' % RIGHT, world.said())

    def test_a_freeze_that_hangs_cannot_hold_the_sleep_longer_than_the_waits(self):
        world = World(self)
        world.table[LEFT]['freeze'] = 'hang'
        world.write_state()
        world.start('--settle-seconds', '0.3', '--freeze-seconds', '0.6')
        world.wait_until_listening()
        began = time.monotonic_ns()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        released = [when for when, what in world.events() if what == 'lock-released'][0]
        elapsed = (released - began) / 1e9
        self.assertGreaterEqual(elapsed, 0.3 + 0.6)       # the settle, then all the time the freeze has
        self.assertLess(elapsed, 3)                       # the freeze that hangs would hold it for 60 s
        self.assertEqual(world.units()[RIGHT]['freezer'], 'frozen')
        world.wait(lambda: 'released the sleep delay lock after' in world.said(), 'the release to be said')
        self.assertIn('could not freeze %s: timed out' % LEFT, world.said())
        world.sleep_signal(False)
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        self.assertEqual(world.count('thaw ' + LEFT), 0)

    def test_the_sides_are_frozen_no_earlier_than_the_settle_after_the_announcement(self):
        world = World(self)
        world.start()
        world.wait_until_listening()
        began = time.monotonic_ns()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        world.wait(lambda: 'released the sleep delay lock' in world.said(), 'the release to be said')
        froze = [when for when, what in world.events() if what.startswith('freeze ') and what.endswith(' ok')]
        self.assertEqual(len(froze), 2)
        self.assertGreaterEqual((min(froze) - began) / 1e9, guard.SETTLE_SECONDS)
        said = world.said()
        self.assertIn('watching logind for PrepareForSleep (user manager, 1.5 s to settle, 3 s to freeze, logind waits '
                      '5 s for a delay lock)', said)
        positions = [said.index('dosplex-sleep-guard: ' + text) for text in (
            'PrepareForSleep(true)', 'waiting 1.5 s for the sides to take the sleep announcement', 'froze ' + LEFT,
            'released the sleep delay lock')]
        self.assertEqual(positions, sorted(positions))

    def test_an_announcement_sent_to_a_guard_that_listens_gets_the_whole_settle_though_the_lock_was_slow(self):
        # The stand-in logs the lock 0.4 s before the guard hears that it has it, as a busy machine does. An announcement
        # sent at the log line reaches the guard while it takes the lock, and is counted from before that: the settle
        # is cut by the 0.4 s. One sent when the guard listens counts from when it is read.
        world = World(self)
        world.start('--settle-seconds', '0.5', FAKE_INHIBIT_DELAY='0.4')
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        said = world.said()
        self.assertIn('waiting 0.5 s for the sides to take the sleep announcement', said)
        self.assertNotIn('came up to', said)

    def test_a_freeze_that_hangs_lets_the_lock_go_before_logind_stops_waiting(self):
        # The defaults: 1.5 s of settle and 3 s for the freeze, and logind waits 5 s for a delay lock.
        world = World(self)
        world.table[LEFT]['freeze'] = 'hang'
        world.write_state()
        world.start()
        world.wait_until_listening()
        began = time.monotonic_ns()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        released = [when for when, what in world.events() if what == 'lock-released'][0]
        elapsed = (released - began) / 1e9
        self.assertGreaterEqual(elapsed, guard.SETTLE_SECONDS + guard.FREEZE_SECONDS)
        self.assertLess(elapsed, 5)
        self.assertEqual(world.units()[RIGHT]['freezer'], 'frozen')

    def test_a_wake_during_the_settle_freezes_nothing_and_the_lock_is_taken_again(self):
        world = World(self)
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: 'waiting 1.5 s for the sides to take the sleep announcement' in world.said(), 'the settle')
        world.sleep_signal(False)
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        world.wait(lambda: 'took the sleep delay lock again' in world.said(), 'the guard to say it has the lock again')
        time.sleep(guard.SETTLE_SECONDS + 0.5)            # long enough for a freeze that was only put off
        self.assertEqual(world.count('freeze '), 0)
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        self.assertEqual(world.count('lock-released'), 1)
        self.assertIn('logind announced the wake after', world.said())
        for unit in (LEFT, RIGHT):
            self.assertNotIn('froze ' + unit, world.said())

    def test_a_logind_that_restarts_during_the_settle_freezes_nothing_and_the_lock_is_taken_again(self):
        # The lines are the ones `gdbus monitor` prints when the owner of logind's name goes and another one comes.
        world = World(self)
        world.start('--settle-seconds', '0.8')
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: 'waiting 0.8 s for the sides to take the sleep announcement' in world.said(), 'the settle')
        world.send('The name org.freedesktop.login1 does not have an owner')
        world.send('The name org.freedesktop.login1 is owned by :1.9')
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        world.wait(lambda: 'took the sleep delay lock again' in world.said(), 'the guard to say it has the lock again')
        time.sleep(0.8 + 0.5)           # long enough for a freeze that was only put off
        self.assertEqual(world.count('freeze '), 0)
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        self.assertEqual(world.count('lock-released'), 1)
        said = world.said()
        self.assertIn('(logind left the bus after', said)
        self.assertIn('which calls the sleep off; nothing was frozen)', said)
        for unit in (LEFT, RIGHT):
            self.assertNotIn('froze ' + unit, said)

    def test_a_logind_that_restarts_after_the_freezing_does_not_leave_the_sides_frozen(self):
        # logind has begun the sleep and restarts: it announces no wake. The sides stay frozen while systemd has a job to
        # sleep (the machine is asleep, or about to be) and are thawed when it has none.
        world = World(self)
        world.jobs.append('suspend.target')
        world.write_state()
        world.start('--settle-seconds', '0.2')
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'frozen'})
        world.send('The name org.freedesktop.login1 does not have an owner')
        world.send('The name org.freedesktop.login1 is owned by :1.9')
        world.wait(lambda: world.count('lock-taken') == 2, 'the lock again')
        world.wait(lambda: world.count('list-jobs') >= 2, 'the guard to ask systemd for its jobs')
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'frozen'}, 'a job to sleep: it is not over')
        self.assertEqual(world.count('thaw '), 0)
        world.set_jobs([])
        world.wait(lambda: 'thawed ' + RIGHT in world.said(), 'the sides to be thawed')
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        self.assertEqual(world.count('lock-taken'), 2, 'the lock was taken again when logind came back, once')
        said = world.said()
        self.assertIn('logind left the bus after announcing a sleep', said)
        self.assertIn('no PrepareForSleep(false) will come, and systemd has no job to sleep', said)
        self.assertIn('thawed ' + LEFT, said)

    def test_a_stop_during_the_settle_releases_the_lock_and_freezes_nothing(self):
        world = World(self)
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: 'waiting 1.5 s for the sides to take the sleep announcement' in world.said(), 'the settle')
        world.process.send_signal(signal.SIGTERM)
        self.assertEqual(world.process.wait(timeout=15), 0)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual(world.count('freeze '), 0)
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        self.assertIn('released the sleep delay lock (stopped during the settle; nothing was frozen)', world.said())
        self.assertNotIn('after an error', world.said(), 'a stop is not an error')

    def test_a_settle_of_0_freezes_at_once_as_before(self):
        world = World(self)
        world.start('--settle-seconds', '0')
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        world.wait(lambda: 'released the sleep delay lock (froze 2 units' in world.said(), 'the release to be said')
        order = [what for _, what in world.events()]
        frozen = [i for i, what in enumerate(order) if what.startswith('freeze ') and what.endswith(' ok')]
        self.assertEqual(len(frozen), 2)
        self.assertTrue(all(i < order.index('lock-released') for i in frozen), order)
        self.assertIn('(user manager, 0 s to settle, 3 s to freeze, logind waits 5 s for a delay lock)', world.said())
        self.assertNotIn('waiting', world.said())

    def test_a_logind_that_waits_less_gets_a_shorter_settle_and_freeze_cap(self):
        world = World(self)
        world.table[LEFT]['freeze'] = 'hang'
        world.write_state()
        world.start(FAKE_DELAY_MAX='2000000')
        world.wait_until_listening()
        said = world.said()
        self.assertIn('logind waits 2 s for a delay lock: cutting the settle from 1.5 s to 0.5 s and the freeze cap '
                      'from 3 s to 1 s', said)
        self.assertIn('(user manager, 0.5 s to settle, 1 s to freeze, logind waits 2 s for a delay lock)', said)
        began = time.monotonic_ns()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        released = [when for when, what in world.events() if what == 'lock-released'][0]
        elapsed = (released - began) / 1e9
        self.assertGreaterEqual(elapsed, 0.5 + 1.0)       # the settle, then all the time the freeze has
        self.assertLess(elapsed, 2)                       # what logind waits
        self.assertEqual(world.units()[RIGHT]['freezer'], 'frozen')

    def test_a_delay_max_that_cannot_be_read_is_taken_as_five_seconds(self):
        for answer, reason in (('fail', 'ServiceUnknown'), ('garbage', 'unexpected answer'),
                               ('hang', 'timed out after 2.0 s')):
            with self.subTest(answer=answer):
                world = World(self)
                world.start(FAKE_DELAY_MAX=answer)
                world.wait(lambda: world.count('lock-taken') == 1, 'the lock', seconds=20)
                said = world.said()
                self.assertRegex(said, r'could not read InhibitDelayMaxUSec from logind \(.*%s.*\); assuming 5 s'
                                 % reason)
                self.assertIn('(user manager, 1.5 s to settle, 3 s to freeze, logind waits 5 s for a delay lock)', said)
                world.stop()

    def test_logind_is_asked_once_how_long_it_waits_and_never_again(self):
        world = World(self)
        world.start('--settle-seconds', '0.2')
        world.wait_until_listening()
        for round_ in (1, 2):
            world.sleep_signal(True)
            world.wait(lambda: world.count('lock-released') == round_, 'the lock to be released')
            world.sleep_signal(False)
            world.wait_until_listening(round_ + 1)
        self.assertEqual(world.count('gdbus-call'), 1)
        self.assertIn('--method org.freedesktop.DBus.Properties.Get org.freedesktop.login1.Manager InhibitDelayMaxUSec',
                      [what for _, what in world.events() if what.startswith('gdbus-call')][0])

    def test_a_failing_freeze_is_said_and_the_other_side_is_still_frozen(self):
        world = World(self)
        world.table[RIGHT]['freeze'] = 'fail'
        world.write_state()
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual(world.units()[LEFT]['freezer'], 'frozen')
        self.assertEqual(world.units()[RIGHT]['freezer'], 'running')
        self.assertIn('could not freeze %s: Failed to freeze unit %s: Connection timed out' % (RIGHT, RIGHT),
                      world.said())

    def test_a_refused_lock_is_said_and_the_sides_are_still_prepared(self):
        world = World(self)
        world.start(FAKE_INHIBIT_FAIL='1')
        world.wait(lambda: 'could not take the sleep delay lock' in world.said(), 'the refusal to be said')
        # The reason is the program's own words; on a machine too busy to start it in 3 s it is the missing answer.
        self.assertRegex(world.said(), 'could not take the sleep delay lock: (Failed to inhibit: Access denied|no answer in 3.0 s)')
        world.sleep_signal(True)
        world.wait(lambda: world.count('freeze ' + RIGHT) == 1, 'the freezing')
        world.wait(lambda: 'the sleep delay lock was not held' in world.said(), 'the missing lock to be said')
        self.assertEqual(world.count('lock-taken'), 0)

    def test_a_stop_while_the_sides_are_frozen_thaws_them_and_releases_the_lock(self):
        world = World(self)
        world.start()
        world.wait_until_listening()
        world.sleep_signal(True)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'frozen'})
        world.process.send_signal(signal.SIGTERM)
        self.assertEqual(world.process.wait(timeout=15), 0)
        self.assertEqual({u['freezer'] for u in world.units().values()}, {'running'})
        self.assertIn('thawed ' + LEFT, world.said())

    def test_a_stop_while_the_lock_is_held_releases_it(self):
        world = World(self)
        world.start()
        world.wait(lambda: world.count('lock-taken') == 1, 'the lock')
        world.wait(lambda: 'took the sleep delay lock' in world.said(), 'the guard to say it has the lock')
        world.process.send_signal(signal.SIGTERM)
        self.assertEqual(world.process.wait(timeout=15), 0)
        world.wait(lambda: world.count('lock-released') == 1, 'the lock to be released')
        self.assertIn('released the sleep delay lock (the guard ends)', world.said())

    def test_a_monitor_that_ends_is_started_again(self):
        world = World(self)
        world.start()
        world.wait(lambda: world.count('gdbus-started') == 1, 'the monitor')
        world.send('EXIT')
        world.wait(lambda: world.count('gdbus-started') == 2, 'the monitor again', seconds=20)
        self.assertIn('the logind monitor ended; starting it again', world.said())
        world.sleep_signal(True)
        world.wait(lambda: world.count('freeze ' + RIGHT) == 1, 'the freezing after the restart')

    def test_the_guard_refuses_a_wait_that_would_outlast_logind(self):
        for value in ('5', '4.5', '0.01'):
            done = subprocess.run([sys.executable, str(GUARD), '--manager', 'user', '--freeze-seconds', value],
                                  capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 2, value)
            self.assertIn('--freeze-seconds must be between', done.stderr)

    def test_the_guard_refuses_a_settle_that_cannot_be_right(self):
        for value in ('-0.1', '3.6', '10', 'nan'):
            done = subprocess.run([sys.executable, str(GUARD), '--manager', 'user', '--settle-seconds', value],
                                  capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(done.returncode, 2, value)
            self.assertIn('--settle-seconds must be between 0 and 3.5', done.stderr)

    def test_the_system_manager_needs_root(self):
        if os.geteuid() == 0:
            self.skipTest('this check is for an ordinary user')
        done = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 2)
        self.assertIn('must run as root', done.stderr)


if __name__ == '__main__':
    unittest.main()
