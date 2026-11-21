#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The publication check must fail on a reference to anything that is not published.

Each case builds a small published tree in a temporary directory and runs the real
tools/check-publication.py on it. Fixture paths are assembled from parts so that this
file never spells out a dangling reference itself.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / 'tools/check-publication.py'


def path(*parts):
    return '/'.join(parts)


WAIVERS = path('tools', 'publication-waivers.json')
GUIDE = path('docs', 'guide.md')
ABSENT_PAGE = path('docs', 'absent.md')
HELPER, PEER = path('tools', 'helper.py'), path('tools', 'peer.py')
LOADER, MODULE = path('tools', 'loader.py'), path('tools', 'module.py')
RUN, PEER_SCRIPT = path('tests', 'run.sh'), path('tests', 'peer.sh')
ABSENT_TOOL = path('tests', 'absent.py')
UNIT_FILE = path('tools', 'x.service')
PRIVATE_NOTES = path('private', 'notes.md')
ABSENT_UNIT = 'dosplex-' + 'absent' + '.service'
ABSENT_SCRIPT = 'absent' + '.sh'
SIBLING_CALL = 'exec "$(dirname "$0")' + '/'  # The script name follows, to be completed per case.


class ClosureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-closure-')
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / 'tree'

    def check(self, files, waivers=None):
        """Publish exactly `files` (path -> text) and return the checker's result."""
        files = dict(files)
        files['.publication-images.json'] = '{}\n'
        if waivers is not None:
            files[WAIVERS] = json.dumps(waivers)
        files['.publication-files'] = '\n'.join([*files, '.publication-files']) + '\n'
        for name, text in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        # Keep git from finding an enclosing repository; the checker then lists the files itself.
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.parent))
        return subprocess.run([sys.executable, str(CHECK), '--root', str(self.root)],
                              capture_output=True, text=True, env=env)

    def assertFails(self, result, *diagnostics):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for text in diagnostics:
            self.assertIn(text, result.stderr)

    def assertPasses(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resolving_references_pass(self):
        self.assertPasses(self.check({
            'README.md': f'Read [the guide]({GUIDE}#top), [the site](https://example.org/x.md) and `{HELPER}`.\n',
            GUIDE: 'Back to [the readme](../README.md).\n',
            HELPER: 'from pathlib import Path\nsibling = Path(__file__).with_name("peer.py")\n',
            PEER: 'import json\n',
            RUN: SIBLING_CALL + 'peer.sh"\n',
            PEER_SCRIPT: '#!/bin/sh\n',
        }))

    def test_dangling_markdown_link_is_reported_with_file_and_line(self):
        self.assertFails(self.check({'README.md': f'# Title\n\nSee [the page]({ABSENT_PAGE}).\n'}),
                         f'README.md:3: unresolved link reference: {ABSENT_PAGE}')

    def test_dangling_path_in_a_script_is_reported(self):
        self.assertFails(self.check({RUN: f'#!/bin/sh\npython3 {ABSENT_TOOL}\n'}),
                         f'{RUN}:2: unresolved path reference: {ABSENT_TOOL}')

    def test_dangling_sibling_script_is_reported(self):
        self.assertFails(self.check({LOADER: 'from pathlib import Path\nPath(__file__).with_name("absent.py")\n'}),
                         f'{LOADER}:2: unresolved sibling reference: {path("tools", "absent.py")}')
        self.assertFails(self.check({RUN: SIBLING_CALL + ABSENT_SCRIPT + '"\n'}),
                         f'{RUN}:1: unresolved script reference: {ABSENT_SCRIPT}')
        self.assertFails(self.check({RUN: f'#!/bin/sh\nsource {ABSENT_SCRIPT}\n'}),
                         f'{RUN}:2: unresolved script reference: {ABSENT_SCRIPT}')

    def test_a_path_inside_an_installed_release_resolves_through_the_release_layout(self):
        layout = path('packaging', 'release-build', 'layout.txt')
        source = path('packaging', 'steamos', 'session', 'start.py')
        installed = path('tools', 'start.py')
        page = {'README.md': f'After installing, `{installed}` starts it.\n'}
        # The layout says where the published file lies in a release: the reference resolves.
        self.assertPasses(self.check({**page, layout: f'# comment\n{installed}   {source}\n', source: 'import json\n'}))
        # Without the layout, or with a layout whose source is not published, it does not.
        shutil.rmtree(self.root)
        self.assertFails(self.check({**page, source: 'import json\n'}),
                         f'README.md:1: unresolved path reference: {installed}')
        shutil.rmtree(self.root)
        self.assertFails(self.check({**page, layout: f'{installed}   {source}\n'}),
                         f'README.md:1: unresolved path reference: {installed}')

    def test_reference_into_the_private_directory_cannot_be_waived(self):
        waivers = {'references': [{'target': PRIVATE_NOTES, 'reason': 'not accepted'}]}
        self.assertFails(self.check({'README.md': f'See {PRIVATE_NOTES}.\n'}, waivers),
                         'README.md:1: reference into the private directory')

    def test_a_path_into_the_private_directory_built_from_parts_is_found_too(self):
        # Found by a trial export on 2026-11-19: a published test loaded a development-only script through
        # ROOT / 'private' / ..., which the plain-text rule does not see. The exported tree then failed its own gate.
        private = 'priv' + 'ate'
        for expression in ("ROOT / '%s' / 'testing' / 'tool.py'" % private,
                           "ROOT.joinpath('%s', 'notes.md')" % private,
                           "os.path.join(ROOT, '%s', 'x')" % private):
            with self.subTest(expression):
                shutil.rmtree(self.root, ignore_errors=True)
                source = 'import os\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nX = %s\n' % expression
                self.assertFails(self.check({LOADER: source}),
                                 f'{LOADER}:4: reference into the private directory is not allowed in a published file')

    def test_dangling_unit_is_reported(self):
        self.assertFails(self.check({UNIT_FILE: f'[Unit]\nRequires={ABSENT_UNIT}\n'}),
                         f'{UNIT_FILE}:2: unresolved unit reference: {ABSENT_UNIT}')

    def test_unlisted_import_is_reported_and_a_listed_one_passes(self):
        source = {MODULE: 'import notinstalled\n'}
        self.assertFails(self.check(source), f'{MODULE}:1: unresolved import reference: notinstalled')
        self.assertPasses(self.check(source, {'third_party_modules': {'notinstalled': 'optional package'}}))

    def test_python_that_does_not_parse_cannot_be_checked_and_fails(self):
        self.assertFails(self.check({MODULE: 'def (:\n'}), f'{MODULE}: not valid Python')

    def test_waiver_needs_a_reason_and_cannot_go_stale(self):
        readme = {'README.md': f'See `{ABSENT_PAGE}`.\n'}
        self.assertPasses(self.check(readme, {'references': [
            {'file': 'README.md', 'target': ABSENT_PAGE, 'reason': 'documented as intentionally unpublished'}]}))
        self.assertFails(self.check(readme, {'references': [{'target': ABSENT_PAGE}]}),
                         'needs a target and a reason', f'unresolved path reference: {ABSENT_PAGE}')
        self.assertFails(self.check({'README.md': 'Nothing is referenced here.\n'}, {'references': [
            {'target': ABSENT_PAGE, 'reason': 'was needed once'}]}), 'stale waiver')
        self.assertFails(self.check({'README.md': 'Nothing is imported here.\n'},
                                    {'third_party_modules': {'gone': 'was needed once'}}),
                         'stale third-party module')


if __name__ == '__main__':
    unittest.main()
