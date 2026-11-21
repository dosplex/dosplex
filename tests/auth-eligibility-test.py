#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Discovery/direct-login policy parity without real accounts or PAM."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


def user(name='player', uid=1001, shell='/bin/bash'):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_shell=shell, pw_dir='/home/' + name)


class EligibilityTests(unittest.TestCase):
    def test_discovery_and_direct_calls_reject_the_same_disabled_accounts(self):
        rejected = [user(uid=0), user(uid=999), user(uid=65534), user(name='dosplex'), user(name='nobody')]
        rejected += [user(shell=shell) for shell in ('/bin/false', '/usr/bin/false',
                                                   '/sbin/nologin', '/usr/sbin/nologin', '/bin/nologin')]
        with tempfile.TemporaryDirectory(prefix='dosplex-eligibility-') as directory, \
             patch.object(auth, 'SIDE_ROOT', Path(directory)), \
             patch.object(auth, 'authenticate') as authenticate, \
             patch.object(auth, 'grant_wayland_access') as grant:
            for entry in rejected:
                with self.subTest(entry=entry), patch.object(auth.pwd, 'getpwall', return_value=[entry]), \
                     patch.object(auth.pwd, 'getpwnam', return_value=entry):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        auth.list_login_users()
                    self.assertEqual(json.loads(output.getvalue())['users'], [])
                    for operation in (lambda: auth.login_side('left', entry.pw_name, 'public-fake'),
                                      lambda: auth.auto_login_side('left', entry.pw_name)):
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                            operation()
                    self.assertFalse((Path(directory) / 'left/session.json').exists())
            authenticate.assert_not_called()
            grant.assert_not_called()

    def test_eligible_user_still_requires_password_authentication(self):
        entry = user()
        with tempfile.TemporaryDirectory(prefix='dosplex-eligible-') as directory, \
             patch.object(auth, 'SIDE_ROOT', Path(directory)), \
             patch.object(auth.pwd, 'getpwnam', return_value=entry), \
             patch.object(auth, 'authenticate', return_value=False) as authenticate, \
             patch.object(auth, 'grant_wayland_access') as grant:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                auth.login_side('left', entry.pw_name, 'public-fake')
            authenticate.assert_called_once_with(entry.pw_name, 'public-fake')
            grant.assert_not_called()
            self.assertFalse((Path(directory) / 'left/session.json').exists())
            authenticate.return_value = True
            with contextlib.redirect_stdout(io.StringIO()):
                auth.login_side('left', entry.pw_name, 'public-fake')
            self.assertEqual(auth.load_state('left')['owner'], entry.pw_name)
            grant.assert_called_once_with(entry.pw_name)

    def test_auto_login_cannot_report_another_live_legacy_owner_as_success(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-claim-owner-') as directory, \
             patch.object(auth, 'SIDE_ROOT', Path(directory)), \
             patch.object(auth.pwd, 'getpwnam', return_value=user()), \
             patch.object(auth, 'grant_wayland_access') as grant:
            existing = dict(side='left', owner='other-player', pid=os.getpid(), active=True)
            auth.save_state('left', existing)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                auth.auto_login_side('left', 'player')
            self.assertEqual(auth.load_state('left'), existing)
            grant.assert_not_called()

    def test_interactive_users_remain_discoverable_and_sorted(self):
        entries = [user('zoe', shell='/bin/zsh'), user('amy', shell='/usr/bin/fish')]
        with patch.object(auth.pwd, 'getpwall', return_value=entries), contextlib.redirect_stdout(io.StringIO()) as output:
            auth.list_login_users()
        self.assertEqual([item['name'] for item in json.loads(output.getvalue())['users']], ['amy', 'zoe'])


if __name__ == '__main__':
    unittest.main()
