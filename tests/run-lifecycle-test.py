#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A test that ends, runs too long or is interrupted must not leave processes behind.

The runner is run on fixture tests that start children: a timeout asks the whole process group to stop, waits
for the grace period and kills what is left; what a finished test left running is killed; terminating the
runner ends the tests it started.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest

sys.dont_write_bytecode = True
from runner_fixture import alive, wait_until, TreeCase  # noqa: E402


class LifecycleTests(TreeCase):
    """What happens to a test and to the processes it started when it ends, is too slow or is interrupted."""

    def test_a_timeout_asks_the_whole_group_to_stop_and_then_kills_what_is_left(self):
        ready, terminated = self.base / 'child.ready', self.base / 'child.terminated'
        polite = textwrap.dedent(f'''\
            import pathlib, signal, sys, time
            def goodbye(number, frame):
                time.sleep(0.5)  # cleaning up takes a moment, which the grace period is for
                pathlib.Path({str(terminated)!r}).write_text("terminated")
                sys.exit(0)
            signal.signal(signal.SIGTERM, goodbye)
            pathlib.Path({str(ready)!r}).write_text("ready")
            time.sleep(120)
            ''')
        body = textwrap.dedent(f'''\
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", {polite!r}])
            time.sleep(120)
            ''')
        self.add('slow-test.py', 'tier=unit timeout=5', body)
        stubborn = 'import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n'
        self.add('stubborn-test.py', 'tier=unit timeout=1', stubborn)
        started = time.monotonic()
        result = self.runner('--tier', 'unit', '--kill-grace', '2')
        self.assertLess(time.monotonic() - started, 30)
        self.assertExit(result, 1)
        self.assertRegex(result.stdout, r'TIMEOUT\s+1 unit\s+[\d.]+s\s+\S+slow-test\.py\s+killed after 5 s')
        self.assertRegex(result.stdout, r'TIMEOUT\s+1 unit\s+[\d.]+s\s+\S+stubborn-test\.py\s+killed after 1 s')
        self.assertTrue(ready.exists(), 'the child never got ready, so this case proved nothing')
        self.assertEqual(terminated.read_text(), 'terminated',
                         'what the timed-out test started was killed without being asked to stop first')

    def test_what_a_finished_test_left_running_is_killed(self):
        pidfile = self.base / 'left.pid'
        body = textwrap.dedent(f'''\
            import subprocess, sys
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            open({str(pidfile)!r}, "w").write(str(child.pid))
            ''')
        self.add('leaky-test.py', 'tier=unit', body)
        self.assertExit(self.runner('--tier', 'unit'), 0)
        pid = int(pidfile.read_text())
        self.assertTrue(wait_until(lambda: not alive(pid)), 'a process of a finished test is still running')

    def test_terminating_the_runner_ends_the_tests_it_started(self):
        pidfile = self.base / 'running.pid'
        body = textwrap.dedent(f'''\
            import os, time
            open({str(pidfile)!r}, "w").write(str(os.getpid()))
            time.sleep(120)
            ''')
        self.add('sleeper-test.py', 'tier=unit timeout=100', body)
        process = subprocess.Popen(self.command('--no-evidence', '--tier', 'unit'), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        try:
            self.assertTrue(wait_until(pidfile.exists), 'the test never started')
            pid = int(pidfile.read_text())
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('interrupted', output)
        self.assertTrue(wait_until(lambda: not alive(pid)), 'the test outlived the runner')


if __name__ == '__main__':
    unittest.main()
