#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Auth state publication and interrupted-write checks; no accounts or PAM."""
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class AuthStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-auth-state-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(auth, 'SIDE_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.old = {'side': 'left', 'owner': 'old-user', 'pid': 0}
        self.new = {'side': 'left', 'owner': 'new-user', 'pid': 0}
        auth.save_state('left', self.old)
        self.path = self.root / 'left/session.json'

    def assert_no_temporary(self):
        self.assertEqual(list(self.path.parent.glob('.auth-state-*')), [])

    def test_reader_sees_old_until_replace_then_complete_new(self):
        replace = os.replace

        def observe(source, destination):
            self.assertEqual(auth.load_state('left'), self.old)
            self.assertEqual(json.loads(Path(source).read_text()), self.new)
            replace(source, destination)

        with patch.object(auth.os, 'replace', side_effect=observe):
            auth.save_state('left', self.new)
        self.assertEqual(auth.load_state('left'), self.new)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assert_no_temporary()

    def test_failure_preserves_old_state_and_cleans_temporary(self):
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                with patch.object(auth.os, operation, side_effect=OSError('injected write failure')):
                    with self.assertRaises(OSError):
                        auth.save_state('left', self.new)
                self.assertEqual(auth.load_state('left'), self.old)
                self.assert_no_temporary()
        with self.assertRaises(TypeError):
            auth.save_state('left', {'unsupported': object()})
        self.assertEqual(auth.load_state('left'), self.old)
        self.assert_no_temporary()

    def test_missing_is_unclaimed_but_malformed_is_an_error(self):
        self.assertIsNone(auth.load_state('right'))
        for raw in (b'{', b'[]', b'null', b'{}', b'\xff', b'x' * 65537,
                    b'{"side":"right","owner":"other"}',
                    b'{"side":"left","owner":42}'):
            with self.subTest(raw=raw[:40]):
                self.path.write_bytes(raw)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    with self.assertRaises(SystemExit):
                        auth.load_state('left')
                self.assertFalse(json.loads(out.getvalue())['ok'])
                self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_state_blocks_login_before_auth_or_access_changes(self):
        self.path.write_text('{')
        user = type('User', (), {'pw_uid': 1001, 'pw_name': 'test-user', 'pw_shell': '/bin/bash'})()
        with patch.object(auth.pwd, 'getpwnam', return_value=user), \
             patch.object(auth, 'authenticate') as authenticate, \
             patch.object(auth, 'grant_wayland_access') as grant:
            for call in (lambda: auth.login_side('left', 'test-user', 'public-fake'),
                         lambda: auth.auto_login_side('left', 'test-user')):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    call()
            authenticate.assert_not_called()
            grant.assert_not_called()

    def test_other_side_is_unchanged(self):
        right = {'side': 'right', 'owner': 'other', 'pid': 0}
        auth.save_state('right', right)
        before = (self.root / 'right/session.json').read_bytes()
        auth.save_state('left', self.new)
        self.assertEqual((self.root / 'right/session.json').read_bytes(), before)

    def test_logout_requires_matching_confirmed_cleanup(self):
        original = dict(self.old, instance='a' * 32, lifecycle='running', active=True, unit='owned.service')
        cases = [dict(original, lifecycle='cleanup_failed'),
                 dict(original, instance='b' * 32), original]
        for current in cases:
            with self.subTest(current=current):
                auth.save_state('left', original)
                with patch.object(auth, 'LOGOUT_TIMEOUT_SECONDS', .001), \
                     patch.object(auth, 'load_state', side_effect=[original, current]), \
                     patch.object(auth, 'revoke_wayland_access') as revoke, \
                     patch.object(auth.os, 'kill') as kill, \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        auth.logout_side('left')
                    revoke.assert_not_called()
                    kill.assert_not_called()
                self.assertEqual(auth.load_state('left'), original)

    def test_guarded_logout_rejects_stale_or_invalid_identity_before_stop(self):
        current = dict(self.old, instance='a' * 32, lifecycle='running', active=True, unit='owned')
        auth.save_state('left', current)
        before = self.path.read_bytes()
        with patch.object(auth, 'revoke_wayland_access') as revoke:
            for owner, instance in [('old-user', 'b' * 32), ('another-user', 'a' * 32),
                                    ('old-user', 'bad'), ('old-user', None), (None, 'a' * 32)]:
                with self.subTest(owner=owner, instance=instance), \
                     contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    auth.logout_side('left', owner, instance)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse((self.root / 'left/stop').exists())
            auth.remove_state('left')
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                auth.logout_side('left', 'old-user', 'a' * 32)
            revoke.assert_not_called()
            self.assertFalse((self.root / 'left/stop').exists())

    def test_logout_ack_removes_only_target_state(self):
        stopped = dict(self.old, instance='a' * 32, lifecycle='stopped', active=False, unit='')
        auth.save_state('left', stopped)
        right = dict(self.old, side='right', owner='other')
        auth.save_state('right', right)
        with patch.object(auth, 'revoke_wayland_access') as revoke, \
             patch.object(auth.os, 'kill') as kill, contextlib.redirect_stdout(io.StringIO()):
            auth.logout_side('left', 'old-user', 'a' * 32)
        revoke.assert_called_once_with('old-user')
        kill.assert_not_called()
        self.assertIsNone(auth.load_state('left'))
        self.assertEqual(auth.load_state('right'), right)

    def test_revocation_failure_keeps_confirmed_state_for_retry(self):
        stopped = dict(self.old, instance='a' * 32, lifecycle='stopped', active=False, unit='')
        auth.save_state('left', stopped)
        with patch.object(auth, 'revoke_wayland_access', side_effect=OSError('ACL failure')):
            with self.assertRaises(OSError):
                auth.logout_side('left')
        self.assertEqual(auth.load_state('left'), stopped)

    def test_legacy_pid_is_never_signalled(self):
        auth.save_state('left', dict(self.old, pid=os.getpid()))
        with patch.object(auth.os, 'kill') as kill, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                auth.logout_side('left')
            kill.assert_not_called()
        self.assertTrue(self.path.exists())

    def test_parallel_auth_operation_is_rejected(self):
        with (self.root / 'left/auth.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                auth.logout_side('left')
        self.assertEqual(auth.load_state('left'), self.old)

    def test_cross_side_allocation_is_serialized(self):
        with (self.root / 'allocation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                auth.auto_login_side('right', 'player')
        self.assertIsNone(auth.load_state('right'))

    def test_other_side_identity_cannot_be_reused_even_when_stopped(self):
        from types import SimpleNamespace
        for name, uid in [('player', 1001), ('alias', 1001)]:
            with self.subTest(name=name):
                auth.save_state('right', {'side': 'right', 'owner': name,
                                         'pid': 0, 'active': False})
                def lookup(username):
                    return SimpleNamespace(pw_name=username, pw_uid=uid,
                                           pw_shell='/bin/bash', pw_dir='/home/' + username)
                with patch.object(auth.pwd, 'getpwnam', side_effect=lookup), \
                     patch.object(auth, 'authenticate') as authenticate, \
                     patch.object(auth, 'grant_wayland_access') as grant:
                    for operation in (lambda: auth.login_side('left', 'player', 'fixture'),
                                      lambda: auth.auto_login_side('left', 'player')):
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                            operation()
                    authenticate.assert_not_called()
                    grant.assert_not_called()
                self.assertEqual(auth.load_state('left'), self.old)

    def test_distinct_side_identity_is_allowed(self):
        from types import SimpleNamespace
        auth.save_state('right', {'side': 'right', 'owner': 'other', 'pid': 0})
        with patch.object(auth.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1002)):
            auth.require_exclusive_identity('left', SimpleNamespace(pw_name='player', pw_uid=1001))

    def test_killed_writer_does_not_publish_partial_state(self):
        script = '''
import importlib.util, os, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('auth', sys.argv[1])
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)
auth.SIDE_ROOT = pathlib.Path(sys.argv[2])
def interrupted_replace(source, destination):
    os.kill(os.getpid(), signal.SIGKILL)
auth.os.replace = interrupted_replace
auth.save_state('left', {'side': 'left', 'owner': 'interrupted', 'pid': 0})
'''
        result = subprocess.run([sys.executable, '-c', script,
                                 str(ROOT / 'guest/dosplex-side-auth.py'), str(self.root)],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, -9)
        self.assertEqual(auth.load_state('left'), self.old)
        # SIGKILL skips finally; the abandoned private candidate is not state.
        self.assertEqual(len(list(self.path.parent.glob('.auth-state-*'))), 1)
        auth.save_state('left', self.new)
        self.assertEqual(auth.load_state('left'), self.new)


if __name__ == '__main__':
    unittest.main()
