#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Verify both privileged supervisor entry paths use shared auth admission."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class SupervisorAdmissionTests(unittest.TestCase):
    def test_request_writes_only_configured_tree(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-request-root-') as tmp:
            root = Path(tmp)
            for name in ('trial', 'unrelated'):
                (root / name / 'left').mkdir(parents=True)
                (root / name / 'left/session.json').write_text(json.dumps({'owner': 'fixture'}))
            with patch.dict(os.environ, {'DOSPLEX_SIDE_ROOT': str(root / 'trial'), 'SUDO_UID': '0'}):
                supervisor.request_change('left', 'mode', 'desktop')
            self.assertEqual((root / 'trial/left/mode').read_text(), 'desktop\n')
            self.assertFalse((root / 'unrelated/left/mode').exists())

    def test_configured_root_matches_auth_helper(self):
        with patch.dict(os.environ, {'DOSPLEX_SIDE_ROOT': '/run/dosplex-fixture/sides'}):
            spec = importlib.util.spec_from_file_location('configured_auth', ROOT / 'guest/dosplex-side-auth.py')
            auth = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(auth)
            self.assertEqual(supervisor.side_state_root(), auth.SIDE_ROOT)
            self.assertEqual(auth.SIDE_ROOT, Path('/run/dosplex-fixture/sides'))

    def test_rejection_precedes_session_and_request_side_effects(self):
        for argv in (['left', 'deck'], ['request-mode', 'right', 'gaming'],
                     ['request-restart', 'left'], ['request-stop', 'right']):
            with self.subTest(argv=argv), patch.object(sys, 'argv', ['supervisor', *argv]), \
                 patch.object(supervisor.os, 'geteuid', return_value=0), \
                 patch.object(supervisor, 'prepare_privileged_state', side_effect=PermissionError('fixture')) as admit, \
                 patch.object(supervisor, 'request_change') as request, \
                 patch.object(supervisor, 'supervise') as run:
                with self.assertRaises(PermissionError): supervisor.main()
                admit.assert_called_once_with()
                request.assert_not_called()
                run.assert_not_called()

    def test_installed_and_source_layout_use_auth_admission(self):
        for layout in ('installed', 'source'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory(prefix='dosplex-admission-layout-') as tmp:
                root = Path(tmp)
                executable = root / ('bin/dosplex-side-supervisor.py' if layout == 'installed'
                                     else 'packaging/steamos/dosplex-side-supervisor.py')
                helper = root / ('libexec/dosplex-side-auth.py' if layout == 'installed'
                                 else 'guest/dosplex-side-auth.py')
                executable.parent.mkdir(parents=True)
                helper.parent.mkdir(parents=True)
                helper.write_text('def prepare_state_root():\n    raise PermissionError("shared-admission-called")\n')
                with patch.object(supervisor, '__file__', str(executable)):
                    with self.assertRaisesRegex(PermissionError, 'shared-admission-called'):
                        supervisor.prepare_privileged_state()


if __name__ == '__main__': unittest.main()
