#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The third-party notices of a release payload: generated from its SBOM, checked against it, and one rights holder.

Covered, all hermetic (no container, no network, no root):

  - tools/generate-notices.py on the SBOM of a real payload (tests/fixtures/sbom-preview3.json): every bundled
    component with version, license and source, the data compiled into files with the license as the SBOM states it
    (hwdata without a license text is an open owner decision, never an invented text), the host-provided libraries
    listed as such, libfuse with its LGPL-2.1 statement, the command line, --strict;
  - tools/check-sbom.py: a bundled library, a compiled-in component or a host library without an entry in the
    notices fails, and so does an entry the SBOM does not support;
  - packaging/release-build/assemble.py run in this process with stand-ins for the tools of the image: the payload
    it makes carries the generated notices and passes tools/check-sbom.py;
  - legal/RIGHTS_HOLDER is the one place the name is written: the notices, the headers of the sources, NOTICE,
    AUTHORS.md and README.md agree with it;
  - THIRD-PARTY-NOTICES.md of the source tree carries the provenance statement and the screenshot entry.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'sbom-preview3.json'
GENERATE = ROOT / 'tools' / 'generate-notices.py'
CHECK = ROOT / 'tools' / 'check-sbom.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notices = load('generate_notices', GENERATE)
headers = load('apply_spdx_headers', ROOT / 'tools' / 'apply-spdx-headers.py')
sbom_tests = load('sbom_check_test', ROOT / 'tests' / 'sbom-check-test.py')
check_sbom = sbom_tests.check_sbom
assemble = load('assemble', ROOT / 'packaging' / 'release-build' / 'assemble.py')

HOLDER = 'Example Rights Holder'
NOTICES_PATH = notices.NOTICES_PATH
# The template of the layer manifest in the Gamescope tree (layer/VkLayer_FROG_gamescope_wsi.json.in).
TEMPLATE = '''{
    "file_format_version" : "1.0.0",
    "layer" : {
      "name": "VK_LAYER_FROG_gamescope_wsi_@family@",
      "type": "GLOBAL",
      "api_version": "1.3.221",
      "library_path": "@lib_dir@/libVkLayer_FROG_gamescope_wsi_@family@.so",
      "implementation_version": "1",
      "description": "Gamescope WSI (XWayland Bypass) Layer (@family@)",
      "functions": {
         "vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion"
      },
      "enable_environment": {
        "ENABLE_GAMESCOPE_WSI": "1"
      },
      "disable_environment": {
        "DISABLE_GAMESCOPE_WSI": "1"
      }
    }
}
'''
LAYER = 'lib/libVkLayer_FROG_gamescope_wsi_x86_64.so'
MANIFEST = 'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json'
LAYER32 = 'lib/libVkLayer_FROG_gamescope_wsi_x86.so'
MANIFEST32 = 'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86.json'


# The versions the stand-in for pacman reports; every other package is 1.0-1. hwdata's is the one whose license
# texts the recipe keeps (assemble.py RECIPE_TEXTS).
IMAGE_VERSIONS = {'hwdata': '0.397-1'}


def fixture():
    return json.loads(FIXTURE.read_text())


def section(text, title):
    """The text under the heading `## title`, up to the next `## ` heading."""
    found = re.search(r'^## %s\n(.*?)(?=^## |\Z)' % re.escape(title), text, re.S | re.M)
    assert found, 'no section %r' % title
    return found.group(1)


def entry(text, heading):
    """The heading line and the bullets below it, up to the blank line."""
    found = re.search(r'^### %s\n((?:.+\n)*)' % re.escape(heading), text, re.M)
    assert found, 'no entry %r' % heading
    return found.group(0)


def without_entry(text, heading):
    return text.replace(entry(text, heading) + '\n', '')


def run_tool(script, *arguments):
    return subprocess.run([sys.executable, str(script), *map(str, arguments)], capture_output=True, text=True,
                          encoding='utf-8')


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.sbom = fixture()
        self.text = notices.generate(self.sbom, HOLDER)

    def test_every_bundled_component_is_listed_with_version_license_and_source(self):
        bundled = section(self.text, 'Bundled components')
        libraries = [record for record in self.sbom['files'] if record['path'].startswith('lib/')]
        self.assertEqual(len(libraries), 3)
        for record in libraries:
            with self.subTest(library=record['path']):
                block = entry(bundled, '%s %s' % (record['name'], record['version']))
                self.assertIn('- Files: `%s`\n' % record['path'], block)
                self.assertIn('- License: %s\n' % record['license'], block)
                self.assertIn('- License text: `%s`\n' % record['license_files'][0], block)
                self.assertIn('- Source: %s\n' % record['source_url'], block)
                self.assertIn('- Source SHA-256: `%s`\n' % record['source_sha256'], block)
                self.assertNotIn('OWNER DECISION', block)

    def test_the_data_compiled_into_files_is_listed_with_the_license_the_sbom_states(self):
        compiled = section(self.text, 'Data compiled into bundled files')
        hwdata = entry(compiled, 'hwdata 0.397-1')
        self.assertIn('- Compiled into: `lib/libdisplay-info.so.2`, `lib/libwlroots.so.12`\n', hwdata)
        self.assertIn('- License (as recorded in the SBOM): GPL-2.0-or-later\n', hwdata)
        self.assertIn('- License text: none ships with this release\n', hwdata)
        self.assertIn('- **OWNER DECISION REQUIRED:** The SBOM records hwdata 0.397-1 under GPL-2.0-or-later', hwdata)
        # The text is never made up: the entry cites the record and nothing else.
        self.assertNotIn('Permission is hereby granted', hwdata)
        self.assertNotIn('General Public License', hwdata)
        wayland = entry(compiled, 'wayland-protocols 1.45-1')
        self.assertIn('- Compiled into: `bin/dosplex-compositor`\n', wayland)
        self.assertIn('- License text: `licenses/wayland-protocols-1.45-1-COPYING`\n', wayland)
        self.assertNotIn('OWNER DECISION', wayland)

    def test_code_compiled_from_a_source_archive_is_listed_with_its_source_not_as_a_package(self):
        sbom = fixture()
        record = next(item for item in sbom['files'] if item['path'] == 'lib/libliftoff.so.0')
        record['includes'] = [{'name': 'vkroots', 'version': '0.1.0+5106d8a', 'license': 'Apache-2.0 OR MIT',
                               'license_files': ['licenses/libliftoff-0.5.0-LICENSE'], 'note': 'a header library',
                               'source_url': 'https://example.org/vkroots-5106d8a.tar.gz',
                               'source_sha256': 'c' * 64}]
        text = notices.generate(sbom, HOLDER)
        block = entry(section(text, 'Data compiled into bundled files'), 'vkroots 0.1.0+5106d8a')
        self.assertIn('- Compiled into: `lib/libliftoff.so.0`\n', block)
        self.assertIn('- SBOM note: a header library\n', block)
        self.assertIn('- License (as recorded in the SBOM): Apache-2.0 OR MIT\n', block)
        self.assertIn('- License text: `licenses/libliftoff-0.5.0-LICENSE`\n', block)
        self.assertIn('- Source: https://example.org/vkroots-5106d8a.tar.gz\n', block)
        self.assertIn('- Source SHA-256: `%s`\n' % ('c' * 64), block)
        self.assertNotIn('- Package:', block)
        self.assertNotIn('OWNER DECISION', block)
        self.assertIn('- Also compiled in: vkroots 0.1.0+5106d8a', entry(section(text, 'Bundled components'),
                                                                        'libliftoff 0.5.0'))
        # The data of the image's packages keep their package line.
        self.assertIn('- Package: hwdata 0.397-1 of the build image', entry(
            section(text, 'Data compiled into bundled files'), 'hwdata 0.397-1'))
        # The check wants the source lines word for word.
        self.assertEqual(notices.notice_problems(sbom, text), [])
        edited = text.replace('- Source SHA-256: `%s`\n' % ('c' * 64), '- Source SHA-256: `%s`\n' % ('d' * 64))
        self.assertTrue(any('vkroots 0.1.0+5106d8a' in problem for problem in notices.notice_problems(sbom, edited)))

    def test_the_libraries_that_include_data_point_at_it(self):
        bundled = section(self.text, 'Bundled components')
        for name in ('libdisplay-info 0.2.0', 'wlroots 0.17.4'):
            self.assertIn('- Also compiled in: hwdata 0.397-1', entry(bundled, name))
        self.assertNotIn('Also compiled in', entry(bundled, 'libliftoff 0.5.0'))

    def test_host_provided_libraries_are_listed_as_such(self):
        host = section(self.text, 'Libraries provided by the host system')
        bundled = section(self.text, 'Bundled components')
        libraries = self.sbom['host_provided']['libraries']
        self.assertEqual(len(libraries), 28)
        self.assertIn('not part of the release', host)
        self.assertIn('(observed: build image)', host)
        for item in libraries:
            with self.subTest(soname=item['soname']):
                row = '| `%s` | %s | %s | %s | ' % (item['soname'], item['package'], item['version'], item['license'])
                self.assertIn(row, host)
                for user in item['needed_by']:
                    self.assertIn('`%s`' % user, host.split(row, 1)[1].split('\n', 1)[0])
                self.assertNotIn(item['soname'], bundled)

    def test_the_python_interpreter_and_modules_are_listed_as_host_provided(self):
        python = section(self.text, 'Python provided by the host system')
        self.assertIn('- `python3` (interpreter)', python)
        for item in self.sbom['host_provided']['python']:
            self.assertIn('`%s`' % item['name'], python)
        self.assertIn('- `PAM`: used by `libexec/dosplex-side-auth.py`\n', python)

    def test_the_rights_holder_is_the_name_it_is_given(self):
        self.assertIn('The copyright holder is %s ' % HOLDER, self.text)
        self.assertIn('The copyright holder is Someone Else ', notices.generate(self.sbom, 'Someone Else'))
        with self.assertRaises(notices.NoticeError):
            notices.generate(self.sbom, '  ')

    def test_the_command_takes_the_rights_holder_from_the_file(self):
        result = run_tool(GENERATE, FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('The copyright holder is %s ' % (ROOT / 'legal' / 'RIGHTS_HOLDER').read_text(
            encoding='utf-8').strip(), result.stdout)

    def test_the_same_sbom_always_gives_the_same_text_in_any_record_order(self):
        shuffled = fixture()
        shuffled['files'].reverse()
        shuffled['host_provided']['libraries'].reverse()
        shuffled['host_provided']['python'].reverse()
        self.assertEqual(notices.generate(shuffled, HOLDER), self.text)
        self.assertEqual(notices.generate(fixture(), HOLDER), self.text)

    def test_the_text_does_not_depend_on_the_record_of_the_notices_themselves(self):
        with_record = fixture()
        with_record['files'].append({
            'path': NOTICES_PATH, 'kind': 'license', 'name': 'dosplex', 'version': '0.1.0-preview3',
            'origin': 'repository', 'source_path': 'tools/generate-notices.py', 'license': 'AGPL-3.0-or-later',
            'license_files': ['licenses/dosplex-LICENSE'], 'sha256': '0' * 64, 'size': 1, 'mode': '0644'})
        self.assertEqual(notices.generate(with_record, HOLDER), self.text)

    def test_the_text_ends_with_one_newline_and_has_no_trailing_spaces(self):
        self.assertTrue(self.text.endswith('.\n') or self.text.endswith('\n'))
        self.assertFalse(self.text.endswith('\n\n'))
        self.assertEqual([line for line in self.text.split('\n') if line != line.rstrip()], [])

    def test_the_controller_report_layout_statement_is_there(self):
        self.assertIn(notices.CONTROLLER_LAYOUT_NOTE, section(self.text, 'Provenance'))
        self.assertIn('public Linux kernel documentation', notices.CONTROLLER_LAYOUT_NOTE)
        self.assertIn('No code was copied', notices.CONTROLLER_LAYOUT_NOTE)

    def test_the_open_decisions_of_the_current_payload_are_only_hwdata(self):
        self.assertEqual([label for label, _ in notices.open_decisions(self.sbom)], ['hwdata 0.397-1'])
        self.assertEqual(section(self.text, 'Open decisions').strip().split('\n')[-1], '- hwdata 0.397-1')

    def test_libfuse_is_not_in_a_payload_that_does_not_have_it(self):
        self.assertNotIn('libfuse', self.text)
        self.assertNotIn('## License obligations', self.text)

    def add_libfuse_host_library(self):
        self.sbom['host_provided']['libraries'].append({
            'soname': 'libfuse3.so.3', 'needed_by': ['libexec/native-hid-relay'], 'provided_by': 'host',
            'package': 'fuse3', 'version': '3.17.2-1', 'license': 'GPL-2.0-only AND LGPL-2.1-only',
            'observed': 'build image'})

    def test_a_host_provided_libfuse_comes_with_its_lgpl_statement(self):
        self.add_libfuse_host_library()
        text = notices.generate(self.sbom, HOLDER)
        self.assertIn('| `libfuse3.so.3` | fuse3 | 3.17.2-1 | GPL-2.0-only AND LGPL-2.1-only (the package; the library is '
                      'LGPL-2.1, see "libfuse" below) | '
                      '`libexec/native-hid-relay` |', section(text, 'Libraries provided by the host system'))
        block = entry(section(text, 'License obligations'), 'libfuse (LGPL-2.1)')
        self.assertIn('host-provided `libfuse3.so.3` (package fuse3 3.17.2-1), needed by `libexec/native-hid-relay`',
                      block)
        self.assertIn('GNU Lesser General Public License, version 2.1 (LGPL-2.1)', block)
        self.assertIn('- Dynamic linking:', block)
        self.assertIn('can be replaced', block)
        self.assertNotIn('Static linking', block)
        self.assertNotIn('OWNER DECISION', block)
        self.assertEqual([label for label, _ in notices.open_decisions(self.sbom)], ['hwdata 0.397-1'])

    def test_a_libfuse_that_is_part_of_a_file_needs_a_relink_path_and_says_so(self):
        compositor = next(record for record in self.sbom['files'] if record['path'] == 'bin/dosplex-compositor')
        compositor['includes'].append({'name': 'libfuse', 'version': '3.17.2', 'license': 'LGPL-2.1-only',
                                       'license_files': [], 'note': 'linked in statically'})
        text = notices.generate(self.sbom, HOLDER)
        block = entry(section(text, 'License obligations'), 'libfuse (LGPL-2.1)')
        self.assertIn('- Static linking:', block)
        self.assertIn('relink', block)
        self.assertIn('- **OWNER DECISION REQUIRED:** The SBOM records no way to relink', block)
        self.assertIn('libfuse', [label for label, _ in notices.open_decisions(self.sbom)])

    def test_a_missing_license_text_or_source_is_an_open_decision_not_a_crash(self):
        library = next(record for record in self.sbom['files'] if record['path'] == 'lib/libliftoff.so.0')
        library['license_files'] = []
        del library['source_url']
        block = entry(notices.generate(self.sbom, HOLDER), 'libliftoff 0.5.0')
        self.assertIn('- License text: none ships with this release\n', block)
        self.assertIn('- Source: not recorded in the SBOM\n', block)
        self.assertEqual(block.count('OWNER DECISION REQUIRED'), 2)
        labels = [label for label, _ in notices.open_decisions(self.sbom)]
        self.assertEqual(labels.count('libliftoff 0.5.0 (lib/libliftoff.so.0)'), 2)

    def test_an_sbom_that_is_not_a_dosplex_sbom_is_refused(self):
        for broken in ({}, {'format': 'dosplex-sbom', 'format_version': 2}, [], 'text'):
            with self.subTest(broken=broken), self.assertRaises(notices.NoticeError):
                notices.generate(broken, HOLDER)
        incomplete = fixture()
        del incomplete['host_provided']['python']
        with self.assertRaises(notices.NoticeError):
            notices.generate(incomplete, HOLDER)

    def test_the_rights_holder_file_must_hold_exactly_one_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'RIGHTS_HOLDER'
            path.write_text('Name Surname\n', encoding='utf-8')
            self.assertEqual(notices.read_rights_holder(path), 'Name Surname')
            for text in ('', '\n\n', 'One\nTwo\n'):
                path.write_text(text, encoding='utf-8')
                with self.subTest(text=text), self.assertRaises(notices.NoticeError):
                    notices.read_rights_holder(path)
            with self.assertRaises(notices.NoticeError):
                notices.read_rights_holder(Path(directory) / 'missing')


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-notices-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_the_command_writes_the_file_and_the_check_accepts_it(self):
        target = self.directory / 'notices.md'
        result = run_tool(GENERATE, FIXTURE, '-o', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         notices.generate(fixture(), notices.read_rights_holder()))
        result = run_tool(GENERATE, FIXTURE, '--check', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('matches the SBOM', result.stdout)

    def test_the_check_names_a_missing_entry(self):
        target = self.directory / 'notices.md'
        target.write_text(without_entry(notices.generate(fixture(), HOLDER), 'wlroots 0.17.4'), encoding='utf-8')
        result = run_tool(GENERATE, FIXTURE, '--check', target)
        self.assertEqual(result.returncode, 1)
        self.assertIn('wlroots 0.17.4 (lib/libwlroots.so.12) has no entry', result.stderr)

    def test_strict_refuses_while_a_decision_is_open_and_writes_nothing(self):
        target = self.directory / 'notices.md'
        result = run_tool(GENERATE, FIXTURE, '--strict', '-o', target)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(target.exists())
        self.assertIn('hwdata 0.397-1', result.stderr)

    def test_strict_writes_when_nothing_is_open(self):
        sbom = fixture()
        for record in sbom['files']:
            if 'includes' in record:
                record['includes'] = [item for item in record['includes'] if item['name'] != 'hwdata']
        sbom_file = self.directory / 'sbom.json'
        sbom_file.write_text(json.dumps(sbom), encoding='utf-8')
        target = self.directory / 'notices.md'
        result = run_tool(GENERATE, sbom_file, '--strict', '-o', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('OWNER DECISION', target.read_text(encoding='utf-8'))

    def test_the_rights_holder_file_can_be_named(self):
        holder = self.directory / 'holder'
        holder.write_text('Named On The Command Line\n', encoding='utf-8')
        result = run_tool(GENERATE, FIXTURE, '--rights-holder-file', holder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Named On The Command Line', result.stdout)
        holder.write_text('two\nlines\n', encoding='utf-8')
        result = run_tool(GENERATE, FIXTURE, '--rights-holder-file', holder)
        self.assertEqual(result.returncode, 1)
        self.assertIn('exactly one line', result.stderr)

    def test_an_unreadable_sbom_is_reported_not_raised(self):
        for content in (None, 'not json', '{}'):
            sbom_file = self.directory / 'broken.json'
            if content is not None:
                sbom_file.write_text(content, encoding='utf-8')
            with self.subTest(content=content):
                result = run_tool(GENERATE, sbom_file)
                self.assertEqual(result.returncode, 1)
                self.assertIn('generate-notices:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)


class CompletenessTests(unittest.TestCase):
    """tools/check-sbom.py on a fake payload whose notices are changed after they were generated."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-notices-check-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.payload = sbom_tests.FakePayload(self.directory / 'payload')

    def rewrite(self, edit):
        """Replace the notices by edit(text), keep the SBOM record in step, and stop save() regenerating them."""
        self.payload.save()
        target = self.payload.root / NOTICES_PATH
        data = edit(target.read_text(encoding='utf-8')).encode('utf-8')
        target.write_bytes(data)
        self.payload.keep_notices = True
        self.payload.entry(NOTICES_PATH).update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def assertProblem(self, *fragments):
        problems = self.payload.problems()
        self.assertTrue(any(all(fragment in text for fragment in fragments) for text in problems),
                        'expected a problem mentioning %r, got %r' % (fragments, problems))

    def test_a_payload_whose_notices_are_generated_from_its_sbom_passes(self):
        self.assertEqual(self.payload.problems(), [])
        text = (self.payload.root / NOTICES_PATH).read_text(encoding='utf-8')
        self.assertIn('### wlroots 0.17.4', text)
        self.assertIn('| `libseat.so.1` | seatd | 0.9.1 | MIT |', text)
        result = self.payload.cli()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_bundled_library_without_an_entry_in_the_notices_fails(self):
        self.rewrite(lambda text: without_entry(text, 'wlroots 0.17.4'))
        self.assertProblem(NOTICES_PATH, 'wlroots 0.17.4', 'lib/libwlroots.so.12', 'has no entry')
        result = self.payload.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn('wlroots 0.17.4', result.stderr)

    def test_compiled_in_data_without_an_entry_fails(self):
        compositor = self.payload.entry('bin/dosplex-compositor')
        compositor['includes'] = [{'name': 'wayland-protocols', 'version': '1.45-1', 'license': 'MIT',
                                   'license_files': [], 'note': 'generated headers'}]
        self.payload.save()
        self.assertEqual(self.payload.problems(), [])
        self.rewrite(lambda text: without_entry(text, 'wayland-protocols 1.45-1'))
        self.assertProblem(NOTICES_PATH, 'wayland-protocols 1.45-1', 'has no entry')

    def test_a_host_library_without_a_row_fails(self):
        self.rewrite(lambda text: re.sub(r'^\| `libseat\.so\.1`.*\n', '', text, flags=re.M))
        self.assertProblem(NOTICES_PATH, 'host library libseat.so.1', 'has no entry')

    def test_a_python_module_without_a_line_fails(self):
        self.rewrite(lambda text: re.sub(r'^- `evdev`.*\n', '', text, flags=re.M))
        self.assertProblem(NOTICES_PATH, 'Python evdev', 'has no entry')

    def test_an_entry_that_was_edited_by_hand_fails(self):
        self.rewrite(lambda text: text.replace('- License: MIT\n', '- License: GPL-3.0-only\n', 1))
        self.assertProblem(NOTICES_PATH, 'wlroots 0.17.4', 'differs from the SBOM', 'GPL-3.0-only')

    def test_an_entry_with_a_line_added_by_hand_fails(self):
        self.rewrite(lambda text: text.replace('- Files: `lib/libwlroots.so.12`\n',
                                               '- Files: `lib/libwlroots.so.12`\n- Remark: approved\n', 1))
        self.assertProblem(NOTICES_PATH, 'wlroots 0.17.4', 'differs from the SBOM', 'Remark')

    def test_an_entry_the_sbom_does_not_support_fails(self):
        self.rewrite(lambda text: text.replace('## Libraries provided by the host system',
                                               '### invented 1.0\n- License: MIT\n\n'
                                               '## Libraries provided by the host system'))
        self.assertProblem(NOTICES_PATH, 'invented 1.0', 'does not support')

    def test_a_host_library_the_sbom_does_not_have_fails(self):
        self.rewrite(lambda text: text.replace(
            '| --- | --- | --- | --- | --- |\n',
            '| --- | --- | --- | --- | --- |\n| `libinvented.so.1` | invented | 1 | MIT | `bin/x` |\n'))
        self.assertProblem(NOTICES_PATH, 'libinvented.so.1')

    def test_notices_made_for_another_sbom_fail(self):
        other = notices.generate(fixture(), HOLDER)
        self.rewrite(lambda text: other)
        self.assertProblem(NOTICES_PATH, 'libdisplay-info')
        self.assertProblem(NOTICES_PATH, 'wlroots 0.17.4', 'differs from the SBOM')

    def test_a_payload_without_notices_fails(self):
        self.payload.save()
        (self.payload.root / NOTICES_PATH).unlink()
        self.payload.keep_notices = True
        self.payload.drop_entry(NOTICES_PATH)
        self.assertProblem(NOTICES_PATH, 'is missing', 'has to list every bundled library')

    def test_notices_that_are_not_text_fail_with_a_message(self):
        self.rewrite(lambda text: text)
        data = b'\xff\xfe not utf-8'
        (self.payload.root / NOTICES_PATH).write_bytes(data)
        self.payload.entry(NOTICES_PATH).update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        self.assertProblem('cannot check', NOTICES_PATH)

    def test_the_checker_does_not_pass_a_payload_it_cannot_check(self):
        lonely = self.directory / 'tools'
        lonely.mkdir()
        shutil.copy(CHECK, lonely / 'check-sbom.py')       # without generate-notices.py next to it
        self.payload.save()
        result = run_tool(lonely / 'check-sbom.py', self.payload.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('generate-notices.py is not usable', result.stderr)

    def test_the_paths_agree_between_the_tools(self):
        self.assertEqual(check_sbom.NOTICES_PATH, NOTICES_PATH)
        self.assertEqual(check_sbom.NOTICES_TOOL, GENERATE.name)


class AssembleTests(unittest.TestCase):
    """assemble.py in this process, with stand-ins for strip, objdump, ldconfig and pacman and for the image's
    license texts: the payload it makes has the generated notices, and tools/check-sbom.py accepts it."""

    COMMIT = 'd' * 40

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-assemble-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.sources = json.loads((ROOT / 'packaging' / 'release-build' / 'sources.json').read_text())['sources']
        sonames = [soname for source in self.sources for soname in source.get('libraries', [])]
        self.unpacked, self.deps, self.stage = (self.directory / name for name in ('unpacked', 'deps', 'stage'))
        (self.deps / 'lib').mkdir(parents=True)
        (self.stage / 'bin').mkdir(parents=True)
        (self.stage / 'helpers').mkdir()
        for source in self.sources:
            root = self.unpacked / source.get('into', source['directory'])
            for item in source['license_files']:
                (root / item).parent.mkdir(parents=True, exist_ok=True)
                (root / item).write_text('Licensed under %s: %s (%s)\n' % (source['license'], source['name'], item))
            for soname in source.get('libraries', []):
                (self.deps / 'lib' / soname).write_bytes(
                    sbom_tests.make_elf(['libc.so.6'], soname=soname, runpath='$ORIGIN'))
        (self.stage / 'bin' / 'dosplex-compositor').write_bytes(
            sbom_tests.make_elf(sonames + ['libc.so.6', 'libjson-c.so.5'], runpath='$ORIGIN/../lib'))
        # What payload-build.sh compiles from helpers.txt: one program per line, linked against the image's libraries.
        for line in (ROOT / 'packaging' / 'release-build' / 'helpers.txt').read_text().splitlines():
            fields = line.split()
            if fields and not line.startswith('#'):
                (self.stage / 'helpers' / fields[0].replace('/', '__')).write_bytes(
                    sbom_tests.make_elf(['libc.so.6'] + ['lib%s.so.4' % module for module in fields[2:]]))
        # What payload-build.sh stages from the Gamescope tree, and the template that tree has for the manifest.
        layer = 'lib/libVkLayer_FROG_gamescope_wsi_x86_64.so'
        (self.stage / 'gamescope' / 'lib').mkdir(parents=True)
        (self.stage / 'gamescope' / layer).write_bytes(
            sbom_tests.make_elf(['libc.so.6'], soname='libVkLayer_FROG_gamescope_wsi_x86_64.so'))
        (self.stage / 'gamescope' / LAYER32).write_bytes(
            sbom_tests.make_elf(['libc.so.6'], soname='libVkLayer_FROG_gamescope_wsi_x86.so', bits=32))
        (self.unpacked / 'gamescope-3.16.30' / 'layer').mkdir(parents=True)
        self.template = self.unpacked / 'gamescope-3.16.30' / 'layer' / 'VkLayer_FROG_gamescope_wsi.json.in'
        self.template.write_text(TEMPLATE)
        self.distribution = self.directory / 'licenses'
        (self.distribution / 'wayland-protocols').mkdir(parents=True)
        (self.distribution / 'wayland-protocols' / 'COPYING').write_text('wayland-protocols license text\n')
        self.payload = self.directory / 'payload'

    @staticmethod
    def fake_run(*argv, check=True):
        """What ldconfig, pacman and objdump would print in the image."""
        licenses = {'hwdata': 'GPL-2.0-or-later', 'glibc': 'LGPL-2.1-or-later', 'lib32-glibc': 'LGPL-2.1-or-later',
                    'fuse3': 'LGPL-2.1-only'}
        owners = {'libc.so.6': 'glibc', 'libjson-c.so.5': 'json-c', 'libfuse3.so.4': 'fuse3'}
        owners32 = {'libc.so.6': 'lib32-glibc'}
        if argv[:2] == ('ldconfig', '-p'):
            return (''.join('\t%s (libc6,x86-64, OS ABI: Linux 3.2.0) => /usr/lib/%s\n' % (name, name)
                            for name in owners)
                    + ''.join('\t%s (libc6, OS ABI: Linux 3.2.0) => /usr/lib32/%s\n' % (name, name)
                              for name in owners32))
        if argv[:2] == ('pacman', '-Qi'):
            return ('Name            : %s\nVersion         : %s\nLicenses        : %s\n'
                    % (argv[2], IMAGE_VERSIONS.get(argv[2], '1.0-1'), licenses.get(argv[2], 'MIT')))
        if argv[:2] == ('pacman', '-Qo'):
            table = owners32 if argv[2].startswith('/usr/lib32/') else owners
            return '%s is owned by %s 1.0-1\n' % (argv[2], table[Path(argv[2]).name])
        if argv[:2] == ('pacman', '-Q'):
            return 'fuse3 1.0-1\nglibc 1.0-1\njson-c 1.0-1\nlib32-glibc 1.0-1\n'
        if argv[0] == 'objdump':
            return ('DYNAMIC SYMBOL TABLE:\n0000 g DF .text 0000 GLIBC_2.17 symbol\n'
                    '0000 g DF .text 0000 GLIBCXX_3.4.30 other\n')
        raise AssertionError('unexpected command %r' % (argv,))

    def assemble(self, *extra):
        real_run = subprocess.run

        def fake_subprocess_run(argv, *args, **kwargs):
            if argv[0] == 'strip':
                return subprocess.CompletedProcess(argv, 0)
            return real_run(argv, *args, **kwargs)
        arguments = ['assemble.py', '--source', str(ROOT), '--unpacked', str(self.unpacked), '--deps', str(self.deps),
                     '--stage', str(self.stage), '--payload', str(self.payload), '--version', '0.0.0-test',
                     '--commit', self.COMMIT, '--glibc-floor', '2.41', *extra]
        environment = {'SOURCE_DATE_EPOCH': '1700000000', 'DOSPLEX_BASE_IMAGE': 'example.org/base@sha256:' + '0' * 64,
                       'DOSPLEX_PACKAGE_SNAPSHOT': '2025/07/30'}
        with mock.patch.dict(os.environ, environment), mock.patch.object(sys, 'argv', arguments), \
                mock.patch.object(assemble, 'run', self.fake_run), \
                mock.patch.object(assemble, 'DISTRIBUTION_LICENSES', self.distribution), \
                mock.patch.object(assemble.subprocess, 'run', fake_subprocess_run), \
                mock.patch.object(sys, 'stderr', io.StringIO()):
            assemble.main()
        return json.loads((self.payload / 'sbom' / 'dosplex-sbom.json').read_text())

    def test_the_payload_carries_the_notices_generated_from_its_sbom(self):
        sbom = self.assemble()
        target = self.payload / NOTICES_PATH
        self.assertTrue(target.is_file())
        record = next(item for item in sbom['files'] if item['path'] == NOTICES_PATH)
        self.assertEqual(record['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual((record['kind'], record['origin'], record['source_path'], record['mode']),
                         ('license', 'repository', 'tools/generate-notices.py', '0644'))
        self.assertEqual(record['license_files'], ['licenses/dosplex-LICENSE'])
        # It is what the generator makes from the SBOM without the notices' own record and the repository's name.
        before = dict(sbom, files=[item for item in sbom['files'] if item['path'] != NOTICES_PATH])
        self.assertEqual(target.read_text(encoding='utf-8'),
                         notices.generate(before, notices.read_rights_holder()))
        self.assertEqual(target.read_text(encoding='utf-8'), notices.generate(sbom, notices.read_rights_holder()))

    def test_it_lists_every_component_of_the_payload(self):
        sbom = self.assemble()
        text = (self.payload / NOTICES_PATH).read_text(encoding='utf-8')
        for source in self.sources:
            if not (source.get('libraries') or source.get('products')):
                continue
            block = entry(section(text, 'Bundled components'), '%s %s' % (source['name'], source['version']))
            self.assertIn('- Source: %s\n' % source['url'], block)
            self.assertIn('- Source SHA-256: `%s`\n' % source['sha256'], block)
            if source.get('libraries'):
                self.assertIn('- License: %s\n' % source['license'], block)
        hwdata = entry(section(text, 'Data compiled into bundled files'), 'hwdata 0.397-1')
        self.assertIn('- SBOM note: the PNP ID table (display manufacturer names) compiled into the library\n', hwdata)
        self.assertIn('- License (as recorded in the SBOM): GPL-2.0-or-later\n', hwdata)
        # LEG-01: hwdata installs no license text, so the recipe keeps the two files of its source archive. They
        # ship, the entry names the archive of that version, and no decision is left open.
        self.assertIn('- License text: `licenses/hwdata-0.397-1-COPYING`, `licenses/hwdata-0.397-1-LICENSE`\n', hwdata)
        self.assertIn('- Source: https://github.com/vcrhonek/hwdata/archive/refs/tags/v0.397.tar.gz\n', hwdata)
        self.assertIn('- Source SHA-256: `%s`\n' % assemble.RECIPE_TEXTS['hwdata']['source_sha256'], hwdata)
        self.assertNotIn('OWNER DECISION REQUIRED', text)
        self.assertEqual(notices.open_decisions(sbom), [])
        kept = ROOT / 'packaging/release-build/texts/hwdata-0.397'
        for name in ('LICENSE', 'COPYING'):
            self.assertEqual((self.payload / 'licenses' / ('hwdata-0.397-1-' + name)).read_bytes(),
                             (kept / name).read_bytes())
        self.assertIn(b'GNU General Public License, version 2 or later', (kept / 'LICENSE').read_bytes())
        self.assertIn(b'GNU GENERAL PUBLIC LICENSE', (kept / 'COPYING').read_bytes())
        self.assertIn('wayland-protocols 1.0-1', text)
        for item in sbom['host_provided']['libraries']:
            self.assertIn('| `%s` |' % item['soname'], section(text, 'Libraries provided by the host system'))

    def test_another_hwdata_than_the_one_whose_texts_are_kept_stops_the_assembly(self):
        with mock.patch.dict(IMAGE_VERSIONS, {'hwdata': '0.398-1'}):
            with self.assertRaisesRegex(SystemExit, 'the image has hwdata 0.398-1.*belong to version 0.397'):
                self.assemble()

    def test_the_gamescope_layer_and_its_manifest_are_in_the_payload_with_what_is_compiled_into_the_layer(self):
        sbom = self.assemble()
        records = {item['path']: item for item in sbom['files']}
        layer, manifest = records[LAYER], records[MANIFEST]
        self.assertEqual((layer['kind'], layer['origin'], layer['name'], layer['version'], layer['mode'],
                          layer['license']), ('library', 'upstream', 'gamescope', '3.16.30', '0755', 'BSD-2-Clause'))
        self.assertEqual((manifest['kind'], manifest['origin'], manifest['mode']), ('data', 'upstream', '0644'))
        gamescope = next(source for source in self.sources if source['name'] == 'gamescope')
        for record in (layer, manifest):
            self.assertEqual((record['source_url'], record['source_sha256']), (gamescope['url'], gamescope['sha256']))
            self.assertEqual(record['license_files'], ['licenses/gamescope-3.16.30-LICENSE'])
        compiled = {item['name']: item for item in layer['includes']}
        self.assertEqual(sorted(compiled), ['glm', 'vkroots', 'wayland-protocols'])
        for name in ('glm', 'vkroots'):
            source = next(item for item in self.sources if item['name'] == name)
            self.assertEqual((compiled[name]['source_url'], compiled[name]['source_sha256']),
                             (source['url'], source['sha256']))
            self.assertEqual(compiled[name]['license_files'], [
                'licenses/%s-%s-%s' % (name, source['version'], Path(item).name) for item in source['license_files']])
            for path in compiled[name]['license_files']:
                self.assertTrue((self.payload / path).is_file(), path)
        self.assertNotIn('includes', manifest)
        # stb is an input of the build that nothing in the payload contains: no text, no record.
        self.assertFalse([path for path in records if '/stb-' in path or path.startswith('licenses/stb')])

    def test_the_32_bit_layer_and_its_manifest_are_in_the_payload_like_the_64_bit_ones(self):
        sbom = self.assemble()
        records = {item['path']: item for item in sbom['files']}
        layer, manifest = records[LAYER32], records[MANIFEST32]
        self.assertEqual((layer['kind'], layer['origin'], layer['name'], layer['version'], layer['mode'],
                          layer['license']), ('library', 'upstream', 'gamescope', '3.16.30', '0755', 'BSD-2-Clause'))
        self.assertEqual((manifest['kind'], manifest['mode']), ('data', '0644'))
        self.assertEqual(sorted(item['name'] for item in layer['includes']), ['glm', 'vkroots', 'wayland-protocols'])
        data = json.loads((self.payload / MANIFEST32).read_text())['layer']
        self.assertEqual(data['name'], 'VK_LAYER_DOSPLEX_gamescope_wsi_x86')
        self.assertEqual(data['disable_environment'], {'DOSPLEX_DISABLE_GAMESCOPE_WSI': '1'})
        self.assertEqual(data['library_path'], '../../../lib/libVkLayer_FROG_gamescope_wsi_x86.so')
        self.assertEqual((self.payload / MANIFEST32).parent.joinpath(data['library_path']).resolve(),
                         (self.payload / LAYER32).resolve())
        # Its libc is the 32-bit one, which is another package of the image than the libc of the 64-bit files.
        hosts = {(item['soname'], item.get('abi')): item for item in sbom['host_provided']['libraries']}
        self.assertEqual(hosts[('libc.so.6', 'i386')]['package'], 'lib32-glibc')
        self.assertEqual(hosts[('libc.so.6', 'i386')]['needed_by'], [LAYER32])
        self.assertEqual(hosts[('libc.so.6', None)]['package'], 'glibc')
        self.assertNotIn(LAYER32, hosts[('libc.so.6', None)]['needed_by'])
        text = (self.payload / NOTICES_PATH).read_text(encoding='utf-8')
        self.assertIn('| `libc.so.6` (i386) | lib32-glibc | 1.0-1 |', text)
        self.assertIn('`%s`' % LAYER32, entry(section(text, 'Bundled components'), 'gamescope 3.16.30'))

    def test_a_32_bit_file_whose_library_the_image_lacks_stops_the_assembly(self):
        (self.stage / 'gamescope' / LAYER32).write_bytes(
            sbom_tests.make_elf(['libnothing.so.1'], soname='libVkLayer_FROG_gamescope_wsi_x86.so', bits=32))
        with self.assertRaisesRegex(SystemExit, r'libnothing.so.1 is needed \(by a i386 file\)'):
            self.assemble()

    def test_the_manifest_of_the_layer_finds_the_layer_relative_to_itself(self):
        self.assemble()
        text = (self.payload / MANIFEST).read_text()
        manifest = json.loads(text)['layer']
        self.assertEqual(manifest['library_path'], '../../../lib/libVkLayer_FROG_gamescope_wsi_x86_64.so')
        self.assertEqual(manifest['enable_environment'], {'ENABLE_GAMESCOPE_WSI': '1'})
        self.assertEqual((self.payload / MANIFEST).parent.joinpath(manifest['library_path']).resolve(),
                         (self.payload / LAYER).resolve())
        self.assertNotIn('@', text)

    def test_the_bundled_layers_have_a_name_and_an_off_switch_of_their_own(self):
        # The side launcher turns off the installed Gamescope's layer with DISABLE_GAMESCOPE_WSI=1; the bundled one
        # has to answer to another variable and, so that the loader keeps both apart, to another name. Nothing else
        # of the template changes.
        self.assemble()
        for path, family in ((MANIFEST, 'x86_64'), (MANIFEST32, 'x86')):
            with self.subTest(family):
                text = (self.payload / path).read_text()
                layer = json.loads(text)['layer']
                self.assertEqual(layer['name'], 'VK_LAYER_DOSPLEX_gamescope_wsi_' + family)
                self.assertEqual(layer['disable_environment'], {'DOSPLEX_DISABLE_GAMESCOPE_WSI': '1'})
                self.assertEqual(layer['enable_environment'], {'ENABLE_GAMESCOPE_WSI': '1'})
                self.assertEqual(text, TEMPLATE.replace('@family@', family).replace('@lib_dir@', '../../../lib')
                                 .replace('"VK_LAYER_FROG_gamescope_wsi_%s"' % family,
                                          '"VK_LAYER_DOSPLEX_gamescope_wsi_%s"' % family)
                                 .replace('"DISABLE_GAMESCOPE_WSI"', '"DOSPLEX_DISABLE_GAMESCOPE_WSI"'))

    def test_the_payload_has_a_manifest_only_for_a_layer_whose_library_it_has(self):
        sbom = self.assemble()
        manifests = sorted(item['path'] for item in sbom['files']
                           if item['path'].startswith('share/vulkan/implicit_layer.d/'))
        self.assertEqual(manifests, sorted([MANIFEST, MANIFEST32]))
        for path in manifests:
            with self.subTest(path):
                library = (self.payload / path).parent / json.loads((self.payload / path).read_text())['layer'][
                    'library_path']
                self.assertTrue(library.resolve().is_file(), library)
                self.assertTrue(library.resolve().is_relative_to(self.payload.resolve()), library)

    def test_a_template_that_does_not_make_a_manifest_stops_the_assembly(self):
        for label, change in (('a placeholder that is left', lambda text: text.replace('@family@', '@arch@', 1)),
                              ('no library_path', lambda text: text.replace('library_path', 'path')),
                              ('a library that is not the layer', lambda text: text.replace('.so"', '.so.1"', 1)),
                              ('text that is not json', lambda text: text + '{'),
                              ('no disable variable', lambda text: text.replace('"DISABLE_GAMESCOPE_WSI": "1"', '')),
                              ('two disable variables', lambda text: text.replace(
                                  '"DISABLE_GAMESCOPE_WSI": "1"', '"DISABLE_GAMESCOPE_WSI": "1", "OTHER": "1"')),
                              ('the name twice', lambda text: text.replace(
                                  '"description": "Gamescope WSI', '"description": "VK_LAYER_FROG_gamescope_wsi_@family@",'
                                  ' "summary": "Gamescope WSI'))):
            with self.subTest(label), self.assertRaises(SystemExit):
                self.template.write_text(change(TEMPLATE))
                shutil.rmtree(self.payload, ignore_errors=True)
                self.assemble()

    def test_the_symbol_versions_of_libstdcxx_are_recorded_and_held_to_a_floor(self):
        sbom = self.assemble('--glibcxx-floor', '3.4.34')
        self.assertEqual((sbom['build']['glibcxx_floor'], sbom['build']['glibcxx_max_required']), ('3.4.34', '3.4.30'))
        shutil.rmtree(self.payload)
        with self.assertRaisesRegex(SystemExit, 'needs GLIBCXX_3.4.30, above the floor GLIBCXX_3.4.20'):
            self.assemble('--glibcxx-floor', '3.4.20')
        shutil.rmtree(self.payload, ignore_errors=True)
        self.assertNotIn('glibcxx_floor', self.assemble()['build'])

    def test_the_payload_passes_the_sbom_check_and_the_notices_check(self):
        self.assemble()
        self.assertEqual(check_sbom.check(self.payload), [])
        result = run_tool(CHECK, self.payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_tool(GENERATE, self.payload / 'sbom' / 'dosplex-sbom.json', '--check',
                          self.payload / NOTICES_PATH)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_repository_notices_are_not_what_the_payload_ships(self):
        self.assemble()
        self.assertNotEqual((self.payload / NOTICES_PATH).read_bytes(),
                            (ROOT / 'THIRD-PARTY-NOTICES.md').read_bytes())
        layout = (ROOT / 'packaging' / 'release-build' / 'layout.txt').read_text()
        self.assertNotRegex(re.sub(r'(?m)^#.*$', '', layout), r'THIRD-PARTY-NOTICES')

    def test_a_missing_rights_holder_file_stops_the_assembly(self):
        shutil.copytree(ROOT / 'packaging', self.directory / 'export' / 'packaging')
        shutil.copytree(ROOT / 'tools', self.directory / 'export' / 'tools',
                        ignore=shutil.ignore_patterns('__pycache__', 'build'))
        for name in ('LICENSE', 'NOTICE'):
            shutil.copy(ROOT / name, self.directory / 'export' / name)
        for name in ('guest',):
            shutil.copytree(ROOT / name, self.directory / 'export' / name)
        arguments = ['assemble.py', '--source', str(self.directory / 'export'), '--unpacked', str(self.unpacked),
                     '--deps', str(self.deps), '--stage', str(self.stage), '--payload', str(self.payload),
                     '--version', '0.0.0-test', '--commit', self.COMMIT, '--glibc-floor', '2.41']
        environment = {'SOURCE_DATE_EPOCH': '1700000000', 'DOSPLEX_BASE_IMAGE': 'x', 'DOSPLEX_PACKAGE_SNAPSHOT': 'y'}
        real_run = subprocess.run
        with mock.patch.dict(os.environ, environment), mock.patch.object(sys, 'argv', arguments), \
                mock.patch.object(assemble, 'run', self.fake_run), \
                mock.patch.object(assemble, 'DISTRIBUTION_LICENSES', self.distribution), \
                mock.patch.object(assemble.subprocess, 'run',
                                  lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0)
                                  if argv[0] == 'strip' else real_run(argv, *a, **k)):
            with self.assertRaises(SystemExit) as stopped:
                assemble.main()
        self.assertIn('third-party notices cannot be generated', str(stopped.exception))
        self.assertIn('RIGHTS_HOLDER', str(stopped.exception))


class RecipeWiringTests(unittest.TestCase):
    """The container build exports what the notices need, and nothing copies the hand-written file."""

    def test_the_build_exports_the_generator_and_the_rights_holder(self):
        text = (ROOT / 'packaging' / 'release-build' / 'build.sh').read_text()
        archive = re.search(r'git -C "\$repo" archive[^\n]*\\\n[^\n]*', text).group(0)
        for needed in ('tools/generate-notices.py', 'tools/check-sbom.py', 'legal', 'LICENSE', 'NOTICE'):
            self.assertRegex(archive, r'(?<![\w/.-])%s(?![\w/.-])' % re.escape(needed), needed)
        self.assertNotIn('THIRD-PARTY-NOTICES.md', archive)

    def test_the_final_check_on_the_host_has_the_generator_next_to_the_checker(self):
        text = (ROOT / 'packaging' / 'release-build' / 'build.sh').read_text()
        self.assertIn('$commit:tools/generate-notices.py" > "$tmp/generate-notices.py"', text)
        self.assertIn('python3 "$tmp/check-sbom.py" "$out"', text)

    def test_the_payload_build_checks_the_payload_with_the_exported_tools(self):
        text = (ROOT / 'packaging' / 'release-build' / 'payload-build.sh').read_text()
        self.assertIn('python3 "$src/tools/check-sbom.py" "$payload"', text)


class RightsHolderTests(unittest.TestCase):
    """The name is written once, in legal/RIGHTS_HOLDER; everything else agrees with it."""

    def setUp(self):
        self.holder = notices.read_rights_holder()

    def test_both_tools_read_the_same_file(self):
        self.assertEqual(headers.read_rights_holder(ROOT), self.holder)
        self.assertTrue(self.holder)
        self.assertEqual((ROOT / 'legal' / 'RIGHTS_HOLDER').read_text(encoding='utf-8'), self.holder + '\n')

    def test_notice_authors_and_readme_name_the_rights_holder(self):
        self.assertEqual(headers.identity_problems(ROOT), [])
        notice = (ROOT / 'NOTICE').read_text(encoding='utf-8')
        self.assertIn('Copyright %s %s\n' % (headers.COPYRIGHT_YEAR, self.holder), notice)
        authors = (ROOT / 'AUTHORS.md').read_text(encoding='utf-8')
        self.assertIn('**%s** — project author and maintainer' % self.holder, authors)
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('Created and maintained by **%s**' % self.holder, readme)
        self.assertIn('Copyright © %s %s.' % (headers.COPYRIGHT_YEAR, self.holder), readme)

    def test_no_other_form_of_the_name_is_left_in_the_public_files(self):
        for name in ('NOTICE', 'AUTHORS.md', 'README.md', 'THIRD-PARTY-NOTICES.md'):
            text = (ROOT / name).read_text(encoding='utf-8')
            with self.subTest(file=name):
                self.assertNotIn('Roy Martinsson', text.replace(self.holder, ''))

    def test_the_headers_of_the_sources_name_the_rights_holder(self):
        self.assertEqual(headers.header_problems(ROOT), [])
        first = (ROOT / 'tools' / 'check-publication.py').read_text(encoding='utf-8').split('\n')[:4]
        self.assertIn('# SPDX-License-Identifier: AGPL-3.0-or-later', first)
        self.assertIn('# Copyright %s %s' % (headers.COPYRIGHT_YEAR, self.holder), first)

    def test_the_notices_of_a_payload_name_the_rights_holder_of_the_file(self):
        result = run_tool(GENERATE, FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('The copyright holder is %s (' % self.holder, result.stdout)


class SourceTreeNoticesTests(unittest.TestCase):
    """THIRD-PARTY-NOTICES.md of the repository."""

    def setUp(self):
        self.text = (ROOT / 'THIRD-PARTY-NOTICES.md').read_text(encoding='utf-8')

    def test_it_carries_the_statement_about_the_controller_report_layouts(self):
        self.assertIn(notices.CONTROLLER_LAYOUT_NOTE, self.text)

    def test_no_game_screenshot_is_published(self):
        # No picture of anybody's game is in the repository (owner, 2026-11-18): the folder of pictures is empty or gone.
        self.assertEqual(sorted(p.name for p in (ROOT / 'docs').glob('images/*')), [])
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('docs/images/', readme)
        self.assertNotIn('## Screenshots', self.text)

    def test_it_says_which_libraries_a_binary_release_lists_and_where(self):
        self.assertIn('licenses/dosplex-THIRD-PARTY-NOTICES.md', self.text)
        self.assertIn('libfuse', self.text)
        self.assertIn('LGPL-2.1', self.text)

    def test_it_says_what_the_tracked_protocol_header_is(self):
        self.assertIn('compositor/xdg-shell-protocol.h', self.text)
        self.assertIn('wayland-scanner', self.text)

    def test_the_json_c_entry_is_part_of_the_dependency_list_not_a_stray_section(self):
        self.assertNotIn('### json-c', self.text)
        self.assertIn('json-c', self.text)


if __name__ == '__main__':
    unittest.main()
