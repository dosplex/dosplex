#!/usr/bin/env python3
# dosplex-test: tier=integration needs=systemd-user
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Verify owned service dependencies and real cleanup when its parent stops."""
import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class ParentTests(unittest.TestCase):
    def test_root_trial_binding_and_rejection(self):
        parent = 'dosplex-compositor-' + 'a'*32 + '.service'
        with patch.dict(os.environ, {'DOSPLEX_COMPOSITOR_UNIT': parent}):
            session = m.Session('left', 'deck', ['/usr/bin/true'], {})
            with patch.object(m.subprocess, 'run') as run:
                session.start()
            args = run.call_args.args[0]
            self.assertIn('--property=BindsTo=' + parent, args)
            self.assertIn('--property=After=' + parent, args)
            self.assertIn('--property=User=deck', args)
        for invalid in ('', 'steam-launcher.service', '../other.service', 'dosplex-compositor-*.service'):
            with patch.dict(os.environ, {'DOSPLEX_COMPOSITOR_UNIT': invalid}):
                with self.assertRaises(ValueError):
                    m.Session('left', 'deck', ['true'], {})

    def test_actual_parent_stop_removes_child_runtime(self):
        parent = 'dosplex-compositor-' + uuid.uuid4().hex + '.service'
        session = m.Session('left', pwd.getpwuid(os.getuid()).pw_name,
                            ['/usr/bin/sleep', '60'], {}, user_manager=True, parent_unit=parent)
        def run(*args, check=True):
            return subprocess.run(args, check=check, capture_output=True, text=True, timeout=15)
        try:
            run('systemd-run', '--user', '--quiet', '--collect', '--unit=' + parent,
                '--property=RuntimeMaxSec=60', '/usr/bin/sleep', '60')
            session.start()
            self.assertTrue(session.is_running())
            self.assertTrue(session.runtime.is_dir())
            run('systemctl', '--user', 'stop', parent)
            deadline = time.monotonic()+5
            while time.monotonic()<deadline and (session.is_running() or session.runtime.exists()):
                time.sleep(.05)
            self.assertFalse(session.is_running())
            self.assertFalse(session.runtime.exists())
        finally:
            session.stop()
            run('systemctl', '--user', 'stop', parent, check=False)


if __name__ == '__main__': unittest.main()
