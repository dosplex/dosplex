#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The audio router's monitor goes on when a call outlasts its timeout.

After a stall of the whole machine (a suspend that did not complete) the clocks have run on although no process did,
so a pactl or a Guest endpoint check can raise subprocess.TimeoutExpired for no fault of its own. The monitor loop
is the one long-running part of the router; one such error must be said and the pass tried again, never end it.
"""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router', ROOT / 'packaging/steamos/dosplex-audio-router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)


class Stop(Exception):
    """Ends the monitor's endless loop from inside the fake sleep."""


class MonitorTimeouts(unittest.TestCase):
    def run_monitor(self, ensure_shared, endpoint, passes=5):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= passes:
                raise Stop

        said = io.StringIO()
        with patch.object(router, 'ensure_shared_audio', ensure_shared), \
                patch.object(router, 'guest_endpoint_from_environment', return_value=endpoint), \
                patch.object(router.time, 'sleep', sleep), patch.object(router.signal, 'signal'), \
                contextlib.redirect_stderr(said):
            with self.assertRaises(Stop):     # anything else means the loop ended on its own
                router.monitor_shared_audio()
        return said.getvalue(), len(sleeps)

    def test_a_pass_that_times_out_is_said_and_tried_again(self):
        calls = []

        def ensure_shared(monitor=None):
            calls.append(1)
            if len(calls) <= 2:
                raise subprocess.TimeoutExpired('pactl', 10)
        said, passes = self.run_monitor(ensure_shared, None)
        self.assertEqual((len(calls), passes), (5, 5))
        self.assertIn('Command \'pactl\' timed out after 10 seconds', said)
        self.assertIn('recovered', said)

    def test_a_guest_endpoint_check_that_times_out_is_said_and_tried_again(self):
        calls = []

        class Endpoint:
            def ensure(self):
                calls.append(1)
                if len(calls) <= 2:
                    raise subprocess.TimeoutExpired('pactl', 10)

            def stop(self):
                calls.append('stopped')
        said, passes = self.run_monitor(lambda monitor=None: None, Endpoint())
        self.assertEqual(calls, [1, 1, 1, 1, 1, 'stopped'])      # five passes, then the endpoint stopped with the monitor
        self.assertIn('Guest endpoint: Command \'pactl\' timed out after 10 seconds', said)


if __name__ == '__main__':
    unittest.main()
