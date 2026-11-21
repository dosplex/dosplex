#!/usr/bin/env python3
# dosplex-test: tier=lint
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The meta-test: every test file of this tree is registered, or marked manual or excluded with a reason.

A file named like a test (*-test.py, *-integration.py, *-test.sh, *-integration.sh) that has no valid
"dosplex-test" declaration would be a test that no gate ever runs, so it fails here and in every run of the
runner. The check covers the published test directory, and in a development checkout the development-only
test directory too. The runner itself has its own tests; this file only looks at the real tree.
"""
import importlib.util
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dosplex_run', ROOT / 'tests' / 'run.py')
run = importlib.util.module_from_spec(spec)
sys.modules['dosplex_run'] = run  # dataclasses look their module up here
spec.loader.exec_module(run)

DEVELOPMENT = (ROOT / run.PRIVATE_TEST_DIRECTORY).is_dir()


class RegistryTests(unittest.TestCase):
    def test_every_test_file_is_registered_or_explicitly_marked(self):
        problems = run.registry_problems(ROOT, include_private=DEVELOPMENT)
        self.assertEqual(problems, [], 'unregistered or malformed test declarations:\n' + '\n'.join(problems))

    def test_the_search_covers_the_files_it_is_meant_to(self):
        declarations, _ = run.load_registry(ROOT, include_private=DEVELOPMENT)
        paths = {decl.path for decl in declarations}
        self.assertIn(run.posixpath.join('tests', 'registry-test.py'), paths, 'this file registers itself')
        self.assertIn(run.posixpath.join('tests', 'headless-integration.py'), paths)
        self.assertEqual(DEVELOPMENT, any(path.startswith(run.PRIVATE) for path in paths))

    def test_every_test_that_needs_root_is_manual_and_every_reason_is_given(self):
        declarations, _ = run.load_registry(ROOT, include_private=DEVELOPMENT)
        for decl in declarations:
            if 'root' in decl.needs:
                self.assertTrue(decl.manual, f'{decl.path} needs root but is not manual')
            for kind in ('manual', 'exclude', 'skips_ok'):
                if getattr(decl, kind) is not None:
                    self.assertGreater(len(getattr(decl, kind).strip()), 10, f'{decl.path}: the {kind} reason is empty')

    def test_no_excluded_file_and_no_manual_file_is_ever_scheduled_by_a_tier(self):
        declarations, _ = run.load_registry(ROOT, include_private=DEVELOPMENT)
        everything = frozenset((tier, False) for tier in run.TIER_NAMES)
        cases, left_out, excluded = run.select_cases(declarations, everything, [], False, None)
        scheduled = {case.path for case in cases}
        for decl in declarations:
            if decl.exclude is not None:
                self.assertIn(decl, excluded)
                self.assertNotIn(decl.path, scheduled | {case.path for case in left_out})
            if decl.manual is not None:
                self.assertNotIn(decl.path, scheduled, f'{decl.path} is manual but a tier would run it')


if __name__ == '__main__':
    unittest.main()
