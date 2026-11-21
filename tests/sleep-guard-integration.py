#!/usr/bin/env python3
# dosplex-test: tier=integration needs=systemd-user timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The sleep guard against a real service manager: real units are frozen and thawed, and nothing else is.

Two disposable user services are named like the units of the sides (dosplex-session-left-<id>.service and
dosplex-session-right-<id>.service) and count in a loop, so that a frozen unit can be seen to stand still. The guard
runs with --manager user. Only logind's side is replaced: a stand-in for gdbus sends PrepareForSleep, and a stand-in
for systemd-inhibit stands for the lock. systemctl is the real one, so the output formats of list-units and show,
the cgroup freezer and the "frozen" state are the real ones. The sides run on while the settle after the announcement
lasts and are frozen after it. A unit that is frozen before the sleep (Guest in standby) must still be frozen after
the wake, and a unit that is not a side must not be touched.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'packaging/steamos/dosplex-sleep-guard.py'
spec = importlib.util.spec_from_file_location('sleep_guard_fakes', ROOT / 'tests/fixtures/sleep-guard-fakes.py')
fakes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fakes)

COUNTER = ('import pathlib, sys, time\n'
           'path, n = pathlib.Path(sys.argv[1]), 0\n'
           'while True:\n'
           '    n += 1\n'
           '    path.write_text(str(n))\n'
           '    time.sleep(0.02)\n')


def systemctl(*arguments, check=True):
    return subprocess.run(['systemctl', '--user', *arguments], capture_output=True, text=True, timeout=30, check=check)


def freezer(unit):
    return systemctl('show', '-p', 'FreezerState', '--value', unit).stdout.strip()


class RealUnits(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='dosplex-sleep-guard-it-')
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        (self.base / 'counter.py').write_text(COUNTER)
        self.units = {}
        for name in ('left', 'right'):
            self.start_unit(name, 'dosplex-session-%s-%s.service' % (name, uuid.uuid4().hex))
        # A neighbour that is not a side, whose name starts the same way.
        self.start_unit('other', 'dosplex-session-other-%s.service' % uuid.uuid4().hex)
        self.bin = fakes.install(self.base / 'bin', ('gdbus', 'systemd-inhibit'))
        self.log, self.signals, self.journal = self.base / 'events.log', self.base / 'signals', self.base / 'journal'
        self.signals.write_text('')
        self.log.touch()
        self.process = None
        self.addCleanup(self.stop_guard)

    def start_unit(self, label, name):
        self.addCleanup(self.remove_unit, name)
        subprocess.run(['systemd-run', '--user', '--quiet', '--collect', '--service-type=exec', '--unit=' + name,
                        sys.executable, str(self.base / 'counter.py'), str(self.base / label)],
                       check=True, timeout=30)
        self.units[label] = name
        self.wait(lambda: (self.base / label).exists(), 'the counter of ' + label)

    @staticmethod
    def remove_unit(name):
        systemctl('thaw', name, check=False)
        systemctl('stop', name, check=False)

    def wait(self, condition, what, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for ' + what)

    def counter(self, label):
        return int((self.base / label).read_text() or 0)

    def start_guard(self):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], FAKE_LOG=str(self.log),
                           FAKE_SIGNALS=str(self.signals), PYTHONDONTWRITEBYTECODE='1')
        with self.journal.open('w') as journal:
            self.process = subprocess.Popen([sys.executable, str(GUARD), '--manager', 'user', '--state-path', str(self.base / 'guard-state.json')],
                                            env=environment,
                                            stdout=journal, stderr=journal, stdin=subprocess.DEVNULL)
        # Not at the lock-taken of the stand-in, which comes before the guard knows that it has the lock: an announcement
        # sent then would be counted from before the lock, and the settle would be cut (see fakes.guard_listens).
        self.wait(lambda: fakes.guard_listens(self.process, self.journal.read_text()), 'the guard to wait for logind')

    def stop_guard(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def send(self, active):
        with self.signals.open('a') as stream:
            stream.write('/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (%s,)\n'
                         % ('true' if active else 'false'))

    def count(self, text):
        return self.log.read_text().count(text)

    def stands_still(self, label):
        first = self.counter(label)
        time.sleep(0.3)
        return self.counter(label) == first

    def runs(self, label):
        first = self.counter(label)
        self.wait(lambda: self.counter(label) > first, label + ' to count again')

    def test_a_sleep_freezes_both_sides_and_a_wake_thaws_them(self):
        self.start_guard()
        self.send(True)
        self.wait(lambda: self.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual([freezer(self.units['left']), freezer(self.units['right'])], ['frozen', 'frozen'])
        self.assertTrue(self.stands_still('left') and self.stands_still('right'), 'a frozen side went on counting')
        self.assertEqual(freezer(self.units['other']), 'running')
        self.runs('other')
        self.send(False)
        self.wait(lambda: self.count('lock-taken') == 2, 'the lock again')
        self.assertEqual([freezer(self.units['left']), freezer(self.units['right'])], ['running', 'running'])
        self.runs('left')
        self.runs('right')
        self.send(True)
        self.wait(lambda: self.count('lock-released') == 2, 'the second release')
        self.assertEqual(freezer(self.units['left']), 'frozen', 'the second sleep was not prepared')

    def test_the_sides_run_on_while_the_settle_lasts_and_are_frozen_after_it(self):
        self.start_guard()
        began = time.monotonic()
        self.send(True)
        self.wait(lambda: 'waiting 1.5 s for the sides to take the sleep announcement' in self.journal.read_text(),
                  'the settle to begin')
        time.sleep(0.5)
        self.assertEqual([freezer(self.units['left']), freezer(self.units['right'])], ['running', 'running'])
        self.runs('left')
        self.runs('right')
        self.assertEqual(self.count('lock-released'), 0, 'the lock is held through the settle')
        self.wait(lambda: self.count('lock-released') == 1, 'the lock to be released')
        self.assertGreaterEqual(time.monotonic() - began, 1.5)
        self.assertEqual([freezer(self.units['left']), freezer(self.units['right'])], ['frozen', 'frozen'])

    def test_a_side_in_standby_is_still_frozen_after_the_wake(self):
        systemctl('freeze', self.units['right'])
        self.assertTrue(self.stands_still('right'))
        self.start_guard()
        self.send(True)
        self.wait(lambda: self.count('lock-released') == 1, 'the lock to be released')
        self.assertEqual([freezer(self.units['left']), freezer(self.units['right'])], ['frozen', 'frozen'])
        self.send(False)
        self.wait(lambda: self.count('lock-taken') == 2, 'the lock again')
        self.assertEqual(freezer(self.units['left']), 'running')
        self.assertEqual(freezer(self.units['right']), 'frozen', 'the guard thawed what it did not freeze')
        self.assertTrue(self.stands_still('right'))
        self.runs('left')
        journal = self.journal.read_text()
        self.assertIn('froze ' + self.units['left'], journal)
        self.assertIn('left %s alone: it is frozen already' % self.units['right'], journal)
        self.assertNotIn('thawed ' + self.units['right'], journal)
        # What the supervisor froze, the supervisor thaws.
        systemctl('thaw', self.units['right'])
        self.runs('right')

    def test_a_unit_that_stopped_while_the_machine_slept_is_not_an_error(self):
        self.start_guard()
        self.send(True)
        self.wait(lambda: self.count('lock-released') == 1, 'the lock to be released')
        systemctl('thaw', self.units['right'])
        systemctl('stop', self.units['right'])
        self.send(False)
        self.wait(lambda: self.count('lock-taken') == 2, 'the lock again')
        self.assertEqual(freezer(self.units['left']), 'running')
        self.wait(lambda: 'thawed ' + self.units['left'] in self.journal.read_text(), 'the thaw to be said')
        self.assertNotIn('could not', self.journal.read_text())

    def test_the_real_list_and_show_output_is_understood(self):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('sleep_guard', GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        manager = guard.Systemd('user')
        units = manager.side_units()
        self.assertEqual(units, sorted(units))
        self.assertTrue({self.units['left'], self.units['right']} <= set(units))     # others of the manager may be there
        self.assertNotIn(self.units['other'], units)
        states = manager.states([self.units['left'], self.units['right'], self.units['other'],
                                 'dosplex-session-left-' + '0' * 32 + '.service'])
        self.assertEqual(states[self.units['left']], {'load': 'loaded', 'active': 'active', 'freezer': 'running'})
        self.assertEqual(states['dosplex-session-left-' + '0' * 32 + '.service']['load'], 'not-found')
        outcome = manager.freeze([self.units['left']], time.monotonic() + 3)
        self.assertEqual((outcome[self.units['left']].state, outcome[self.units['left']].error), ('frozen', ''))
        self.assertEqual(manager.thaw([self.units['left']], time.monotonic() + 3), {self.units['left']: ''})
        self.assertEqual(freezer(self.units['left']), 'running')
        missing = manager.freeze(['dosplex-session-left-' + '1' * 32 + '.service'], time.monotonic() + 3)
        self.assertNotEqual(next(iter(missing.values())).error, '')


if __name__ == '__main__':
    unittest.main()
