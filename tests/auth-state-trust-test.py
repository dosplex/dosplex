#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Check root admission with real file types and simulated root ownership."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class StateTrustTests(unittest.TestCase):
    def test_trusted_tree_and_rejected_boundary_objects(self):
        cases = ('valid', 'parent-owner', 'parent-writable', 'parent-symlink',
                 'side-owner', 'side-writable', 'side-symlink', 'state-symlink',
                 'state-hardlink', 'state-owner', 'state-writable', 'lock-fifo', 'mode-fifo', 'restart-symlink', 'stop-symlink')
        real_lstat = Path.lstat
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='dosplex-state-trust-') as tmp:
                base = Path(tmp)
                parent = base / 'dosplex'
                sides = parent / 'sides'
                left = sides / 'left'
                left.mkdir(parents=True)
                for directory in (parent, sides, left): directory.chmod(0o755)
                state = left / 'session.json'
                state.write_text('original')
                state.chmod(0o644)
                foreign = base / 'foreign'
                foreign.mkdir()
                sentinel = foreign / 'sentinel'
                sentinel.write_text('untouched')
                bad_owner = None
                if case == 'parent-owner': bad_owner = parent
                if case == 'side-owner': bad_owner = left
                if case == 'state-owner': bad_owner = state
                if case == 'parent-writable': parent.chmod(0o777)
                if case == 'side-writable': left.chmod(0o777)
                if case == 'state-writable': state.chmod(0o666)
                if case == 'parent-symlink':
                    parent.rename(base / 'original')
                    parent.symlink_to(foreign, target_is_directory=True)
                if case == 'side-symlink':
                    left.rename(sides / 'original')
                    left.symlink_to(foreign, target_is_directory=True)
                if case == 'state-symlink':
                    state.unlink(); state.symlink_to(sentinel)
                if case == 'state-hardlink': os.link(state, base / 'alias')
                if case == 'lock-fifo': os.mkfifo(left / 'auth.lock')
                if case == 'mode-fifo': os.mkfifo(left / 'mode')
                if case == 'restart-symlink': (left / 'restart').symlink_to(sentinel)
                if case == 'stop-symlink': (left / 'stop').symlink_to(sentinel)

                def root_metadata(path):
                    value = real_lstat(path)
                    # Ordinary-user fixtures cannot own root files. Simulate
                    # ownership and a protected ancestor above the fixture;
                    # retain actual leaf type/mode/link count for each case.
                    mode = value.st_mode
                    if path in base.parents:
                        mode = stat.S_IFDIR | 0o755
                    return SimpleNamespace(st_mode=mode, st_uid=1000 if path == bad_owner else 0,
                                           st_nlink=value.st_nlink)

                with patch.object(auth, 'SIDE_ROOT', sides), patch.object(Path, 'lstat', root_metadata):
                    if case == 'valid':
                        auth.prepare_state_root()
                        self.assertTrue((sides / 'right').is_dir())
                    else:
                        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                            auth.prepare_state_root()
                self.assertEqual(sentinel.read_text(), 'untouched')
                self.assertEqual(list(foreign.iterdir()), [sentinel])

    def test_actual_unprivileged_directory_is_rejected_without_adoption(self):
        if os.getuid() == 0:
            self.skipTest('requires ordinary user')
        with tempfile.TemporaryDirectory(prefix='dosplex-untrusted-') as directory:
            target = Path(directory) / 'not-created'
            with patch.object(auth, 'SIDE_ROOT', target), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    auth.prepare_state_root()
            self.assertFalse(target.exists())
            with patch.object(auth, 'SIDE_ROOT', target), \
                 patch.object(auth.os, 'geteuid', return_value=0), \
                 patch.object(sys, 'argv', ['auth', 'status', 'left']), \
                 patch.object(auth, 'side_status') as status, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    auth.main()
                status.assert_not_called()
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
