#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""PAM adapter contracts without loading PAM libraries or changing accounts."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class PAMError(Exception):
    pass


class PAMContracts(unittest.TestCase):
    def test_combined_backend_result_is_authoritative_without_fallback(self):
        # python-pam combines authenticate + acct_mgmt internally. This fixture
        # verifies our adapter, not the external library's account enforcement.
        for result in (True, False):
            with self.subTest(result=result):
                instance = Mock()
                instance.authenticate.return_value = result
                combined = SimpleNamespace(pam=Mock(return_value=instance))
                fallback = Mock()
                with patch.object(auth, 'pam_module', combined), patch.object(auth, 'PAM', fallback):
                    self.assertIs(auth.authenticate('player', ' public-fake '), result)
                instance.authenticate.assert_called_once_with('player', ' public-fake ', service='login')
                fallback.pam.assert_not_called()

    def test_legacy_authentication_then_account_check_and_conversation(self):
        for failure in (None, 'authenticate', 'acct_mgmt'):
            with self.subTest(failure=failure):
                instance = Mock()
                if failure:
                    getattr(instance, failure).side_effect = PAMError('public-fixture-denial')
                backend = SimpleNamespace(pam=Mock(return_value=instance), error=PAMError,
                                          PAM_USER=1, PAM_CONV=2,
                                          PAM_PROMPT_ECHO_OFF=3, PAM_PROMPT_ECHO_ON=4)
                with patch.object(auth, 'pam_module', None), patch.object(auth, 'PAM', backend):
                    self.assertIs(auth.authenticate('player', ' public-fake '), failure is None)
                instance.start.assert_called_once_with('login')
                items = dict(call.args for call in instance.set_item.call_args_list)
                self.assertEqual(items[backend.PAM_USER], 'player')
                conversation = items[backend.PAM_CONV]
                with patch.object(auth, 'PAM', backend):
                    self.assertEqual(conversation(None, [('password', 3), ('user', 4), ('info', 5)], None),
                                     [(' public-fake ', 0), ('player', 0), ('', 0)])
                names = [call[0] for call in instance.mock_calls]
                self.assertEqual(names[:4], ['start', 'set_item', 'set_item', 'authenticate'])
                if failure == 'authenticate':
                    instance.acct_mgmt.assert_not_called()
                else:
                    self.assertEqual(names[4:], ['acct_mgmt'])

    def test_missing_backend_fails_closed_with_structured_error(self):
        with patch.object(auth, 'pam_module', None), patch.object(auth, 'PAM', None), \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
            auth.authenticate('player', 'public-fake')
        self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {'ok': False, 'error': 'python PAM module is not installed'})


if __name__ == '__main__':
    unittest.main()
