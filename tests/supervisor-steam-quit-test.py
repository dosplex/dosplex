#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""TRL-04: the supervisor asks its side's Steam to quit before it stops the unit.

A Reset, a mode change or a display change that needs a restart, and the final stop (Guest off) all stop the
side's systemd unit. systemd sends SIGTERM and kills what is left after ten seconds, so a Steam there is killed
with saves it has not uploaded. The supervisor now asks that Steam to quit first, through the power helper's
`close --steam-only` action (the same path `dosplex stop` uses), and waits at most a fixed number of seconds.
A helper that times out or fails never keeps the unit from being stopped: the stop goes on and the failure is
said once.

The stand-in helper gets the log file path from the environment variable DOSPLEX_TEST_HELPER_LOG (set by the
test and inherited by the subprocess the supervisor starts). The helper's wait is short by patching the two
module constants; no test-only option is added to the supervisor.
"""
import contextlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import socket
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

# The stand-in helper: appends its argv (minus the interpreter and script path) as a JSON line to the file
# named by DOSPLEX_TEST_HELPER_LOG. If that variable is missing, it writes to /dev/null.
HELPER_SOURCE = (
    "import json, os, sys\n"
    "log = os.environ.get('DOSPLEX_TEST_HELPER_LOG', '/dev/null')\n"
    "try:\n"
    "    with open(log, 'a') as f:\n"
    "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    "except OSError:\n"
    "    pass\n"
)

# A helper that logs its argv and then exits with status 3.
HELPER_EXIT3_SOURCE = HELPER_SOURCE + "import sys\nsys.exit(3)\n"


class FakeSession:
    """The owned service, as a recording stand-in."""
    created = []

    def __init__(self, side, owner, command, environment):
        self.unit = f'fake-{next(serial)}.service'
        self.started = self.frozen = False
        self.calls = {'is_running': 0, 'stop': 0, 'start': 0}
        FakeSession.created.append(self)

    def start(self):
        self.started = True
        self.calls['start'] += 1

    def is_running(self):
        self.calls['is_running'] += 1
        return self.started

    def stop(self):
        self.calls['stop'] += 1
        self.started = False
        # In the same file as the helper's lines, so that the order of the two is on record.
        with open(os.environ['DOSPLEX_TEST_HELPER_LOG'], 'a') as stream:
            stream.write(json.dumps(['session.stop']) + '\n')

    def set_standby(self, enabled):
        self.frozen = enabled


class Supervised:
    def __init__(self, test, helper_source=None, helper_mode=0o755, payload='steam'):
        if os.getuid() < 1000:
            test.skipTest('supervise() only runs sessions for an ordinary user')
        self.test = test
        FakeSession.created = []
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-steam-quit-test-')
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

        # Create the stand-in helper if requested.
        self.helper_log = self.root / 'helper-log.jsonl'
        self.helper_path = self.root / 'libexec' / 'dosplex-power-action'
        if helper_source is not None:
            self.helper_path.parent.mkdir(parents=True, exist_ok=True)
            self.helper_path.write_text(helper_source)
            self.helper_path.chmod(helper_mode)

        # Shorten the helper's wait so the tests finish quickly.
        self._restart_secs = m.RESTART_STEAM_QUIT_SECONDS
        self._stop_secs = m.STOP_STEAM_QUIT_SECONDS
        m.RESTART_STEAM_QUIT_SECONDS = 7        # two values that cannot be mistaken for each other
        m.STOP_STEAM_QUIT_SECONDS = 9
        test.addCleanup(self._restore_constants)

        # Tell the stand-in helper where to log.
        self._old_env = os.environ.get('DOSPLEX_TEST_HELPER_LOG')
        os.environ['DOSPLEX_TEST_HELPER_LOG'] = str(self.helper_log)
        test.addCleanup(self._restore_env)

        patches = [patch.object(m.pwd, 'getpwnam', return_value=user),
                   patch.object(m.time, 'sleep', lambda seconds: SLEEP(min(seconds, .005)))]
        for patcher in patches:
            patcher.start()
            test.addCleanup(patcher.stop)

        self.thread = threading.Thread(target=self.run, args=(payload,))
        self.redirect = contextlib.redirect_stdout(self.said)
        self.redirect.__enter__()
        test.addCleanup(self.finish)
        self.thread.start()

    def _restore_constants(self):
        m.RESTART_STEAM_QUIT_SECONDS = self._restart_secs
        m.STOP_STEAM_QUIT_SECONDS = self._stop_secs

    def _restore_env(self):
        if self._old_env is not None:
            os.environ['DOSPLEX_TEST_HELPER_LOG'] = self._old_env
        else:
            os.environ.pop('DOSPLEX_TEST_HELPER_LOG', None)

    def run(self, payload):
        try:
            m.supervise('left', 'tester', self.directory, self.state_path, self.host_path, self.root,
                        self.stop_event, session_factory=FakeSession, payload=payload)
        except BaseException as error:
            self.errors.append(error)

    def finish(self):
        self.stop_event.set()

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

    def request_stop(self):
        """Ask for the stop the way `request-stop` does: the stop file with this supervisor's instance."""
        state = self.snapshot()
        (self.directory / 'stop').write_text(json.dumps(
            {'instance': state['instance'], 'side': 'left', 'owner': state['owner']}) + '\n')
        self.thread.join(timeout=15)
        return not self.thread.is_alive() and not self.errors

    def snapshot(self):
        try:
            return json.loads((self.directory / 'session.json').read_text())
        except (OSError, ValueError):
            return {}

    def helper_entries(self):
        entries = []
        if self.helper_log.exists():
            for line in self.helper_log.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries


class SideSupervisorSteamQuitTests(unittest.TestCase):
    """The real supervise() loop, with a fake session and a stand-in helper. The helper and the fake session's
    stop() write into one log, so what ran and in which order is on record."""

    def running(self, **options):
        run = Supervised(self, **options)
        run.wait(lambda: FakeSession.created and FakeSession.created[0].started
                 and run.snapshot().get('lifecycle') == 'running', 'the side to run')
        return run

    def ask(self, run, limit):
        return ['close', '--steam-only', '--side', 'left', '--side-root', str(run.directory.parent),
                '--steam-timeout', str(limit)]

    def test_a_restart_asks_steam_first_with_the_short_wait(self):
        run = self.running(helper_source=HELPER_SOURCE)
        (run.directory / 'restart').write_text('a restart request\n')
        run.wait(lambda: len(FakeSession.created) >= 2 and FakeSession.created[1].started, 'the side to start again')
        self.assertEqual(run.helper_entries(), [self.ask(run, 7), ['session.stop']])

    def test_a_stop_that_was_asked_for_asks_steam_first_with_the_long_wait(self):
        run = self.running(helper_source=HELPER_SOURCE)
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [self.ask(run, 9), ['session.stop']])

    def test_a_signal_stops_at_once_and_asks_nobody(self):
        # systemd is taking the unit down (a stop, a recovery, a compositor that is gone): nothing waits for Steam.
        run = self.running(helper_source=HELPER_SOURCE)
        self.assertTrue(run.end())
        self.assertEqual(run.helper_entries(), [['session.stop']])

    def test_a_helper_that_fails_never_keeps_the_side_from_stopping(self):
        run = self.running(helper_source=HELPER_EXIT3_SOURCE)
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [self.ask(run, 9), ['session.stop']])
        self.assertEqual(run.said.getvalue().count('ended with status 3'), 1)

    def test_a_helper_that_others_can_write_is_not_run(self):
        run = self.running(helper_source=HELPER_SOURCE, helper_mode=0o775)
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [['session.stop']])

    def test_a_link_in_the_helpers_place_is_not_run(self):
        run = self.running(helper_source=None)
        elsewhere = run.root / 'elsewhere.py'
        elsewhere.write_text(HELPER_SOURCE)
        run.helper_path.parent.mkdir(parents=True, exist_ok=True)
        run.helper_path.symlink_to(elsewhere)
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [['session.stop']])

    def test_without_a_helper_nothing_is_run_and_nothing_is_said_about_it(self):
        run = self.running(helper_source=None)
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [['session.stop']])
        self.assertNotIn('quit', run.said.getvalue())

    def test_a_side_that_is_not_steam_is_not_asked(self):
        run = self.running(helper_source=HELPER_SOURCE, payload='desktop')
        self.assertTrue(run.request_stop())
        self.assertEqual(run.helper_entries(), [['session.stop']])


if __name__ == '__main__':
    unittest.main()
