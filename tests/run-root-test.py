#!/usr/bin/env python3
# dosplex-test: tier=unit needs=userns timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The test runner must not run anything as root on its own.

The runner is started as "root" inside a user namespace (unshare --user --map-root-user), where the process has
user id 0 and no privilege at all, so nothing here can touch the machine. It must refuse to start without
--allow-root, and with it a test that needs root runs only when it is named together with --include-manual. A
test that needs an ordinary user is not run as root: it is INCOMPLETE.
"""
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parent / 'run.py'
TESTS = 'tests'
DECLARATION = '# ' + 'dosplex-test: '


class RootTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-runner-root-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'tree'
        (self.root / TESTS).mkdir(parents=True)
        self.marks = self.base / 'marks'
        self.marks.mkdir()
        self.add('plain-test.py', 'tier=unit')
        self.add('ordinary-test.py', 'tier=unit needs=ordinary-user')
        self.add('rooted-test.py', 'tier=kernel-synthetic needs=root manual="Run it as root"')

    def add(self, name, entries):
        marker = self.marks / name
        body = f'import os\nopen({str(marker)!r}, "w").write(str(os.geteuid()))\n'
        (self.root / posixpath.join(TESTS, name)).write_text(f'{DECLARATION}{entries}\n{body}')

    def as_root(self, *arguments):
        command = ['unshare', '--user', '--map-root-user', sys.executable, str(RUNNER), '--root', str(self.root),
                   '--no-evidence', *arguments]
        return subprocess.run(command, capture_output=True, text=True, timeout=90,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))

    def ran(self):
        return sorted(path.name for path in self.marks.iterdir())

    def test_the_runner_refuses_to_start_as_root(self):
        result = self.as_root('--tier', 'all')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('refusing to run as root', result.stderr)
        self.assertEqual(self.ran(), [], 'nothing may run when the runner refuses')

    def test_a_namespace_root_really_is_uid_zero(self):
        """Guards the other cases: if the namespace did not make the process root, they would prove nothing."""
        result = subprocess.run(['unshare', '--user', '--map-root-user', 'id', '-u'], capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.stdout.strip(), '0')

    def test_with_allow_root_a_test_for_an_ordinary_user_is_incomplete_and_root_tests_stay_manual(self):
        result = self.as_root('--allow-root', '--tier', 'all')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('INCOMPLETE', result.stdout)
        self.assertIn('needs not met: ordinary-user', result.stdout)
        self.assertEqual(self.ran(), ['plain-test.py'], 'only the test that has no need ran, and neither of the others')
        self.assertIn('1 manual test(s) not run', result.stdout)

    def test_a_test_that_needs_root_runs_only_when_named_with_include_manual_by_root(self):
        result = self.as_root('--allow-root', '--include-manual', 'rooted-test')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran(), ['rooted-test.py'])
        self.assertEqual((self.marks / 'rooted-test.py').read_text(), '0', 'it ran as user id 0')
        result = self.as_root('--allow-root', '--include-manual')
        self.assertEqual(result.returncode, 2, 'naming no test is refused')
        self.assertEqual(self.ran(), ['rooted-test.py'])


if __name__ == '__main__':
    unittest.main()
