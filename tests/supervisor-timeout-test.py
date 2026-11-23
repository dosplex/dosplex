#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A slow systemctl never ends the side supervisor.

On the Deck a suspend that did not complete froze every process for 20 seconds while the clocks ran on. The supervisor's
`systemctl is-active` (timeout 5 s) then raised subprocess.TimeoutExpired in its loop, the supervisor died with it, its
session unit stopped and nothing started it again. This runs the real supervise() loop with fake sessions whose calls
time out the way systemctl does, and checks that the supervisor goes on: it keeps the very same session when only the
question was slow, asks again when the stop was slow, and starts the side again when the start was slow.
"""
import contextlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SLEEP = time.sleep
ENTRY = dict(role='primary', x=0, y=0, width=960, height=1080, game_width=960, game_height=1080,
             scale_mode='fit', display_revision=1, activity='active')
serial = itertools.count()


def timeout(call):
    return subprocess.TimeoutExpired(['systemctl', call], 5)


class FakeSession:
    """Stands in for the owned service. `slow` says how the calls behave, per call: a number of TimeoutExpired errors
    to raise before the call works."""
    slow = {}                     # set by the test: {'is_running': 2, 'stop': 1, 'start': 1}
    created = []

    def __init__(self, side, owner, command, environment):
        self.unit = f'fake-{next(serial)}.service'
        self.started = self.frozen = False
        self.calls = {'is_running': 0, 'stop': 0, 'start': 0}
        FakeSession.created.append(self)

    def attempt(self, call):
        self.calls[call] += 1
        left = FakeSession.slow.get(call, 0)
        if left > 0:
            FakeSession.slow[call] = left - 1
            raise timeout(call)

    def start(self):
        self.started = True               # like the real one: the handle says started before systemd-run answers
        self.attempt('start')

    def is_running(self):
        self.attempt('is_running')
        return self.started

    def stop(self):
        self.attempt('stop')
        self.started = False

    def set_standby(self, enabled):
        self.frozen = enabled


class Supervised:
    def __init__(self, test, **slow):
        if os.getuid() < 1000:
            test.skipTest('supervise() only runs sessions for an ordinary user')
        self.test = test
        FakeSession.slow, FakeSession.created = dict(slow), []
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-supervisor-timeout-')
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory, self.state_path, self.host_path = self.root / 'left', self.root / 'state.json', self.root / 'host'
        self.host = socket.socket(socket.AF_UNIX)
        self.host.bind(str(self.host_path))
        test.addCleanup(self.host.close)
        m.atomic_json(self.state_path, {'layout': 'vertical', 'contract_version': 2, 'left': dict(ENTRY),
                                        'right': dict(ENTRY, x=960)})
        self.stop_event, self.errors, self.said = threading.Event(), [], io.StringIO()
        user = SimpleNamespace(pw_uid=os.getuid(), pw_name='tester', pw_dir=str(self.root), pw_shell='/bin/sh')
        patches = [patch.object(m.pwd, 'getpwnam', return_value=user),
                   patch.object(m.time, 'sleep', lambda seconds: SLEEP(min(seconds, .005)))]
        for patcher in patches:
            patcher.start()
            test.addCleanup(patcher.stop)
        self.thread = threading.Thread(target=self.run)
        self.redirect = contextlib.redirect_stdout(self.said)
        self.redirect.__enter__()
        test.addCleanup(self.finish)
        self.thread.start()

    def run(self):
        try:
            m.supervise('left', 'tester', self.directory, self.state_path, self.host_path, self.root, self.stop_event,
                        session_factory=FakeSession)
        except BaseException as error:
            self.errors.append(error)

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=15)
        self.redirect.__exit__(None, None, None)

    def snapshot(self):
        try:
            return json.loads((self.directory / 'session.json').read_text())
        except (OSError, ValueError):
            return {}

    def wait(self, condition, what, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            if not self.thread.is_alive():
                self.test.fail('the supervisor ended (%r) while waiting for %s' % (self.errors, what))
            SLEEP(.002)
        self.test.fail('timed out waiting for ' + what)

    def end(self):
        """Ask it to stop; True when it ended without an error."""
        self.stop_event.set()
        self.thread.join(timeout=15)
        return not self.thread.is_alive() and not self.errors


class SlowSystemd(unittest.TestCase):
    def test_a_question_that_times_out_is_asked_again_and_the_session_stays(self):
        run = Supervised(self, is_running=3)
        run.wait(lambda: FakeSession.created and FakeSession.created[0].calls['is_running'] > 6, 'the loop to go on')
        self.assertTrue(run.thread.is_alive())
        self.assertEqual(len(FakeSession.created), 1)
        self.assertTrue(FakeSession.created[0].started)
        self.assertEqual(run.snapshot()['lifecycle'], 'running')
        self.assertEqual(run.snapshot()['restart_count'], 0)
        self.assertTrue(run.end())
        said = run.said.getvalue()
        self.assertIn('DosPlex supervisor: left: systemctl is-active timed out after 5 s (1 in a row); asking again', said)
        self.assertIn('systemd answers again after 3 timeout(s)', said)

    def test_a_long_stall_is_said_once_and_the_supervisor_waits_it_out(self):
        run = Supervised(self, is_running=60)
        run.wait(lambda: FakeSession.created and FakeSession.created[0].calls['is_running'] > 80, 'the stall to pass')
        self.assertEqual(len(FakeSession.created), 1)
        self.assertEqual(run.snapshot()['lifecycle'], 'running')
        self.assertTrue(run.end())
        self.assertEqual(run.said.getvalue().count('timed out after'), 1)

    def test_a_service_that_really_stopped_is_still_noticed_after_a_slow_answer(self):
        run = Supervised(self, is_running=2)
        run.wait(lambda: FakeSession.created and FakeSession.created[0].calls['is_running'] > 3, 'the first answers')
        FakeSession.created[0].started = False          # the unit died
        run.wait(lambda: len(FakeSession.created) == 2 and FakeSession.created[1].started, 'the side to start again')
        self.assertEqual(run.snapshot()['restart_count'], 1)
        self.assertTrue(run.end())

    def test_a_stop_that_times_out_is_asked_again(self):
        run = Supervised(self, stop=2)
        run.wait(lambda: FakeSession.created and run.snapshot().get('lifecycle') == 'running', 'the side to run')
        (run.directory / 'restart').write_text('another restart request\n')
        run.wait(lambda: len(FakeSession.created) == 2 and FakeSession.created[1].started, 'the side to start again')
        self.assertEqual(FakeSession.created[0].calls['stop'], 3)
        self.assertFalse(FakeSession.created[0].started)
        self.assertTrue(run.thread.is_alive())
        self.assertTrue(run.end())
        self.assertIn('systemctl stop timed out', run.said.getvalue())

    def test_a_start_that_times_out_stops_what_may_have_started_and_starts_again(self):
        run = Supervised(self, start=1)
        run.wait(lambda: len(FakeSession.created) == 2 and FakeSession.created[1].started
                 and run.snapshot().get('lifecycle') == 'running', 'the second start')
        first = FakeSession.created[0]
        self.assertEqual((first.calls['start'], first.calls['stop'], first.started), (1, 1, False))
        self.assertEqual(run.snapshot()['restart_count'], 1)
        self.assertTrue(run.end())
        self.assertIn('systemd-run timed out', run.said.getvalue())

    def test_a_stop_that_keeps_timing_out_ends_with_the_identity_kept_for_recovery(self):
        # Four timeouts in a row are more than a stall: the unit stays named in session.json and the error is raised.
        run = Supervised(self, stop=100)
        run.wait(lambda: run.snapshot().get('lifecycle') == 'running', 'the side to run')
        run.stop_event.set()
        run.thread.join(timeout=15)
        self.assertFalse(run.thread.is_alive())
        self.assertEqual(len(run.errors), 1)
        self.assertIsInstance(run.errors[0], subprocess.TimeoutExpired)
        state = run.snapshot()
        self.assertEqual((state['lifecycle'], state['active'], state['owner']), ('cleanup_failed', True, 'tester'))
        self.assertEqual(FakeSession.created[0].calls['stop'], 2 * m.STOP_ATTEMPTS)    # the loop, then the final stop

    def test_other_errors_still_end_the_supervisor(self):
        # Only a timeout is transient: a failed systemctl is an error as it always was.
        class Failing(FakeSession):
            def is_running(self):
                raise subprocess.CalledProcessError(1, 'systemctl')
        with patch.object(FakeSession, 'is_running', Failing.is_running):
            run = Supervised(self)
            run.thread.join(timeout=15)
        self.assertFalse(run.thread.is_alive())
        self.assertEqual(len(run.errors), 1)
        self.assertIsInstance(run.errors[0], subprocess.CalledProcessError)


class RealSessionCalls(unittest.TestCase):
    def test_the_real_session_still_raises_the_timeout_for_its_caller_to_handle(self):
        # The loop in supervise() is what makes the timeout transient; Session itself reports it, as it always did.
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_RUNTIME_DIR=directory):
            session = m.Session('left', 'tester', ['true'], {}, user_manager=True)
        session.started = True
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                session.is_running()


if __name__ == '__main__':
    unittest.main()
