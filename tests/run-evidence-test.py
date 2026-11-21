#!/usr/bin/env python3
# dosplex-test: tier=unit needs=git timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Every run of the test runner keeps a JSON record: the revision, the uncommitted changes, the tools, each result.

Fixture trees (some of them git repositories) are tested with the real runner. Also JUnit output.
"""
import json
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ElementTree

sys.dont_write_bytecode = True
from runner_fixture import run, TESTS, DECLARATION, FAILING, rel, TreeCase  # noqa: E402


class EvidenceTests(TreeCase):
    def git(self, *arguments):
        identity = 'user.email=' + '@'.join(('test', 'example.invalid'))  # not spelled out: the publication check reads this file
        return subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', identity,
                               '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *arguments],
                              capture_output=True, text=True, check=True, env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null'})

    def records(self, directory):
        return sorted(directory.glob('*.json'))

    def evidence_run(self, directory, *arguments, **kwargs):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run(self.command('--evidence-dir', str(directory), *arguments), capture_output=True,
                              text=True, env=environment, **kwargs)

    def test_a_record_names_the_run_the_revision_the_tools_and_every_result(self):
        self.add('ok-test.py', 'tier=unit needs=cc')
        self.add('bad-test.py', 'tier=unit', FAILING)
        self.add('hand-test.py', 'tier=unit manual="Run it by hand"')
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        sha = self.git('rev-parse', 'HEAD').stdout.strip()
        directory = self.base / 'evidence'
        result = self.evidence_run(directory, '--tier', 'unit')
        self.assertExit(result, 1)
        files = self.records(directory)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, rf'^\d{{4}}-\d\d-\d\d-{sha[:12]}-\d{{6}}Z\.json$')
        self.assertIn(f'evidence: {files[0]}', result.stdout)
        record = json.loads(files[0].read_text())
        self.assertEqual(record['schema'], 'dosplex-gate-evidence/1')
        self.assertRegex(record['started_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertRegex(record['finished_utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual((record['git']['available'], record['git']['sha'], record['git']['dirty'],
                          record['git']['dirty_diff_sha256']), (True, sha, False, None))
        self.assertEqual(record['verdict'], 'FAIL')
        self.assertEqual(record['summary']['PASS'], 1)
        self.assertEqual(record['summary']['FAIL'], 1)
        self.assertEqual(record['selection']['tiers'], [1])
        self.assertIn('python', record['tools'])
        self.assertIn('cc', record['tools'])
        self.assertTrue(record['needs']['cc']['met'])
        by_id = {entry['id']: entry for entry in record['results']}
        ok, bad = by_id[rel(TESTS, 'ok-test.py')], by_id[rel(TESTS, 'bad-test.py')]
        self.assertEqual((ok['status'], ok['returncode'], ok['tier'], ok['needs']), ('PASS', 0, 1, ['cc']))
        self.assertGreaterEqual(ok['seconds'], 0)
        self.assertEqual((bad['status'], bad['returncode']), ('FAIL', 3))
        self.assertIn('this test fails', bad['output_tail'])
        self.assertEqual(record['not_run']['manual'], [rel(TESTS, 'hand-test.py')])

    def test_uncommitted_changes_are_hashed_and_earlier_records_are_kept(self):
        self.add('ok-test.py', 'tier=unit')
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        directory = self.root / 'evidence-inside-the-tree'  # not ignored: it must still not count as a change
        self.assertExit(self.evidence_run(directory, '--tier', 'unit'), 0)
        clean = json.loads(self.records(directory)[0].read_text())['git']
        self.assertEqual((clean['dirty'], clean['dirty_diff_sha256']), (False, None))
        self.write(rel(TESTS, 'ok-test.py'), (self.root / TESTS / 'ok-test.py').read_text() + '# edited\n')
        for _ in range(2):
            self.assertExit(self.evidence_run(directory, '--tier', 'unit'), 0)
        records = self.records(directory)
        self.assertEqual(len(records), 3, 'every run keeps its own record')
        dirty = [json.loads(path.read_text())['git'] for path in records if 'dirty' in path.name]
        self.assertEqual(len(dirty), 2)
        self.assertTrue(all(g['dirty'] for g in dirty))
        self.assertRegex(dirty[0]['dirty_diff_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(dirty[0]['dirty_diff_sha256'], dirty[1]['dirty_diff_sha256'])
        self.write(rel(TESTS, 'new-test.py'), DECLARATION + 'tier=unit\n')
        self.assertExit(self.evidence_run(directory, '--tier', 'unit'), 0)
        newest = [json.loads(path.read_text())['git'] for path in self.records(directory)]
        self.assertEqual(len({g['dirty_diff_sha256'] for g in newest if g['dirty']}), 2,
                         'a new untracked file changes the hash')

    def test_a_tree_that_is_not_a_checkout_is_recorded_as_such(self):
        self.add('ok-test.py', 'tier=unit')
        directory = self.base / 'evidence'
        self.assertExit(self.evidence_run(directory, '--tier', 'unit'), 0)
        (record_file,) = self.records(directory)
        self.assertIn('no-git', record_file.name)
        self.assertFalse(json.loads(record_file.read_text())['git']['available'])

    def test_a_tree_inside_another_repository_does_not_borrow_its_revision(self):
        outer = self.base / 'outer'
        self.root = outer / 'inner'
        (self.root / TESTS).mkdir(parents=True)
        self.add('ok-test.py', 'tier=unit')
        subprocess.run(['git', '-C', str(outer), 'init', '-q'], check=True)
        directory = self.base / 'evidence'
        self.assertExit(self.evidence_run(directory, '--tier', 'unit'), 0)
        git = json.loads(self.records(directory)[0].read_text())['git']
        self.assertFalse(git['available'])

    def test_a_development_checkout_has_a_default_evidence_directory_and_a_published_tree_none(self):
        self.add('ok-test.py', 'tier=unit')
        result = self.runner('--tier', 'unit')  # the helper adds --no-evidence
        self.assertIn('evidence: not written', result.stdout)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        plain = subprocess.run(self.command('--tier', 'unit'), capture_output=True, text=True, env=environment)
        self.assertExit(plain, 0)
        self.assertIn('evidence: not written', plain.stdout)
        self.assertEqual(list(self.root.rglob('*.json')), [])
        (self.root / run.PRIVATE).mkdir()
        development = subprocess.run(self.command('--tier', 'unit'), capture_output=True, text=True, env=environment)
        self.assertExit(development, 0)
        written = list((self.root / run.PRIVATE / 'evidence' / 'gate').glob('*.json'))
        self.assertEqual(len(written), 1, development.stdout)

    def test_a_record_that_cannot_be_written_fails_the_run(self):
        self.add('ok-test.py', 'tier=unit')
        blocked = self.base / 'blocked'
        blocked.write_text('a file where the directory should be')
        result = self.evidence_run(blocked, '--tier', 'unit')
        self.assertExit(result, 1)
        self.assertIn('the evidence record could not be written', result.stdout)

    def test_junit_output(self):
        self.add('ok-test.py', 'tier=unit')
        self.add('bad-test.py', 'tier=unit', FAILING)
        self.add('skip-test.py', 'tier=unit', 'print("SKIP: nothing to see")\n')
        report = self.base / 'out' / 'junit.xml'
        self.assertExit(self.runner('--tier', 'unit', '--junit', str(report)), 1)
        suite = ElementTree.parse(report).getroot()
        self.assertEqual((suite.get('tests'), suite.get('failures'), suite.get('errors')), ('3', '1', '1'))
        failed = [case for case in suite if case.find('failure') is not None]
        self.assertEqual(len(failed), 1)
        self.assertIn('this test fails', failed[0].find('failure').text)


if __name__ == '__main__':
    unittest.main()
