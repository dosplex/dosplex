#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Failure injection for owned service stop confirmation; no real services."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

DEAD = 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n'
GONE = 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'


class StopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-stop-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.root)):
            self.session = module.Session('left', 'tester', ['true'], {}, user_manager=True)
        self.session.started = True

    def run_stop(self, state=DEAD, stop_code=0, show_code=0):
        responses = [subprocess.CompletedProcess([], stop_code),
                     subprocess.CompletedProcess([], show_code, stdout=state)]
        with patch.object(module.subprocess, 'run', side_effect=responses) as run:
            self.session.stop()
        for call in run.call_args_list:
            self.assertIn(self.session.unit, call.args[0])

    def test_confirmed_stop_and_collected_unit(self):
        for state, code in ((DEAD, 0), (GONE, 5)):
            self.session.started = True
            self.run_stop(state, code)
            self.assertFalse(self.session.started)

    def test_failed_or_ambiguous_observation_preserves_ownership(self):
        cases = [(DEAD, 1, 0), (DEAD, 0, 1), ('', 0, 0),
                 (DEAD.replace('inactive', 'active'), 0, 0),
                 (DEAD.replace('LoadState=loaded\n', ''), 0, 0),
                 (DEAD.replace('SubState=dead', 'SubState=stop-sigterm'), 0, 0),
                 (DEAD.replace('MainPID=0', 'MainPID=123'), 0, 0),
                 (DEAD.replace('ControlPID=0', 'ControlPID=123'), 0, 0)]
        for state, stop_code, show_code in cases:
            with self.subTest(state=state, stop=stop_code, show=show_code):
                with self.assertRaises(RuntimeError):
                    self.run_stop(state, stop_code, show_code)
                self.assertTrue(self.session.started)

    def test_remaining_runtime_and_timeout_are_not_success(self):
        self.session.runtime.mkdir()
        with self.assertRaises(RuntimeError):
            self.run_stop()
        self.assertTrue(self.session.started)
        self.session.runtime.rmdir()
        with patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 20)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.session.stop()
        self.assertTrue(self.session.started)

    def test_invalid_launch_token_has_no_side_effects(self):
        with patch.object(module.pwd, 'getpwnam') as lookup:
            for token in ('short', 'g' * 32, 'A' * 32, None):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    module.supervise('left', 'tester', self.root / 'left', self.root / 'display',
                                     self.root / 'host', self.root, threading.Event(), launch_token=token)
            lookup.assert_not_called()
        self.assertFalse((self.root / 'left').exists())

    def test_supervisor_keeps_failed_cleanup_identity(self):
        stop = threading.Event()
        class FailedSession:
            unit = 'owned-test.service'
            def __init__(self, *args): pass
            def start(self): stop.set()
            def stop(self): raise RuntimeError('injected stop failure')
        user = type('User', (), dict(pw_uid=1001, pw_name='tester', pw_dir='/tmp', pw_shell='/bin/sh'))()
        host = socket.socket(socket.AF_UNIX)
        self.addCleanup(host.close)
        host_path = self.root / 'host.sock'
        host.bind(str(host_path))
        contract = {'version': 1, 'live': {'x': 0, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 1280,
                                           'game_height': 720, 'scale_mode': 'fit', 'display_revision': 0,
                                           'layout': 'vertical'}}
        (self.root / 'display').write_text(json.dumps({'layout': 'vertical', 'left': {'activity': 'active'}}))
        with patch.object(module.pwd, 'getpwnam', return_value=user), \
             patch.object(module.display_state, 'read_contract', return_value=contract):
            with self.assertRaisesRegex(RuntimeError, 'injected stop failure'):
                module.supervise('left', 'tester', self.root / 'left', self.root / 'display',
                                 host_path, self.root, stop, session_factory=FailedSession)
        state = json.loads((self.root / 'left/session.json').read_text())
        self.assertEqual(state['lifecycle'], 'cleanup_failed')
        self.assertEqual(state['owner'], 'tester')
        self.assertEqual(state['unit'], FailedSession.unit)
        self.assertTrue(state['active'])


if __name__ == '__main__':
    unittest.main()
