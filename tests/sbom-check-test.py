#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""tools/check-sbom.py must pass a payload that agrees with its SBOM and fail when they disagree.

Every case builds a small fake payload in a temporary directory: a program and a library as hand-made ELF files,
two license texts, a Python script, the version file and the third-party notices (made by tools/generate-notices.py
from the SBOM, again whenever the payload is saved), with a matching SBOM. A case changes one thing and runs the
real checker. Nothing here needs a container, a compiler or the network.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / 'tools' / 'check-sbom.py'
spec = importlib.util.spec_from_file_location('check_sbom', CHECK)
check_sbom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_sbom)
spec = importlib.util.spec_from_file_location('generate_notices', ROOT / 'tools' / 'generate-notices.py')
generate_notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate_notices)

COMMIT = 'a' * 40
VERSION = '0.1.0-abcdef012'
WLROOTS_URL = 'https://example.org/wlroots-0.17.4.tar.gz'
RIGHTS_HOLDER = 'Example Rights Holder'


def make_elf(needed=(), soname=None, runpath=None, bits=64):
    """A minimal little-endian shared object (64-bit x86_64 or 32-bit i386) with a dynamic section: what the checker
    reads."""
    strings = b'\0'
    offsets = {}

    def add(text):
        nonlocal strings
        offsets[text] = len(strings)
        strings += text.encode() + b'\0'
    for name in list(needed) + [soname, runpath]:
        if name:
            add(name)
    wide = bits == 64
    header_size, phentsize, phnum = (64, 56, 2) if wide else (52, 32, 2)
    strtab_offset = header_size + phentsize * phnum
    dynamic_offset = strtab_offset + len(strings)
    dynamic_offset += -dynamic_offset % 8
    entries = [(1, offsets[name]) for name in needed]
    if soname:
        entries.append((14, offsets[soname]))
    if runpath:
        entries.append((29, offsets[runpath]))
    entries += [(5, strtab_offset), (10, len(strings)), (0, 0)]
    dynamic = b''.join(struct.pack('<qQ' if wide else '<iI', tag, value) for tag, value in entries)
    total = dynamic_offset + len(dynamic)
    ident = b'\x7fELF' + bytes([2 if wide else 1, 1, 1, 0]) + bytes(8)
    if wide:
        header = struct.pack('<16sHHIQQQIHHHHHH', ident, 3, 62, 1, 0, header_size, 0, 0, header_size, phentsize,
                             phnum, 64, 0, 0)
        load = struct.pack('<IIQQQQQQ', 1, 4, 0, 0, 0, total, total, 0x1000)
        dyn = struct.pack('<IIQQQQQQ', 2, 4, dynamic_offset, dynamic_offset, dynamic_offset, len(dynamic),
                          len(dynamic), 8)
    else:
        header = struct.pack('<16sHHIIIIIHHHHHH', ident, 3, 3, 1, 0, header_size, 0, 0, header_size, phentsize,
                             phnum, 40, 0, 0)
        load = struct.pack('<IIIIIIII', 1, 0, 0, 0, total, total, 4, 0x1000)
        dyn = struct.pack('<IIIIIIII', 2, dynamic_offset, dynamic_offset, dynamic_offset, len(dynamic),
                          len(dynamic), 4, 4)
    body = header + load + dyn + strings
    body += bytes(dynamic_offset - len(body))
    return body + dynamic


class FakePayload:
    """A payload directory with a matching SBOM; `sbom` can be edited before save().

    save() also writes the notices again, as tools/generate-notices.py makes them from the SBOM as it stands, so that
    a case which changes the SBOM keeps notices that agree with it. A case about the notices themselves sets
    `keep_notices` and writes the file and its record by hand."""

    def __init__(self, root):
        self.root = Path(root)
        self.keep_notices = False
        self.files = {}
        self.add('licenses/dosplex-LICENSE', b'AGPL text\n', 0o644, kind='license', name='dosplex',
                 origin='repository', source_path='LICENSE', license='AGPL-3.0-or-later')
        self.add('licenses/wlroots-0.17.4-LICENSE', b'MIT License text\n', 0o644, kind='license', name='wlroots',
                 version='0.17.4', origin='upstream', source_url=WLROOTS_URL, source_sha256='b' * 64, license='MIT')
        self.add('bin/dosplex-compositor', make_elf(['libwlroots.so.12', 'libseat.so.1', 'libc.so.6'],
                                                   runpath='$ORIGIN/../lib'), 0o755,
                 kind='executable', name='dosplex', origin='repository', source_path='compositor/',
                 license='AGPL-3.0-or-later', license_files=['licenses/dosplex-LICENSE'])
        self.add('lib/libwlroots.so.12', make_elf(['libseat.so.1', 'libc.so.6'], soname='libwlroots.so.12',
                                                  runpath='$ORIGIN'), 0o755,
                 kind='library', name='wlroots', version='0.17.4', origin='upstream', source_url=WLROOTS_URL,
                 source_sha256='b' * 64, license='MIT', license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.add('libexec/helper.py', b'import json\nimport evdev\nfrom os import path\n', 0o755,
                 kind='script', name='dosplex', origin='repository', source_path='guest/helper.py',
                 license='AGPL-3.0-or-later', license_files=['licenses/dosplex-LICENSE'])
        self.add('share/dosplex/VERSION', (VERSION + '\n').encode(), 0o644, kind='data', name='dosplex',
                 origin='repository', source_path='packaging/release-build/assemble.py', license='AGPL-3.0-or-later',
                 license_files=['licenses/dosplex-LICENSE'])
        self.sbom = {
            'format': 'dosplex-sbom', 'format_version': 1,
            'package': {'name': 'dosplex', 'version': VERSION, 'commit': COMMIT, 'source_date_epoch': 1700000000},
            'build': {'glibc_floor': '2.41'},
            'files': [],
            'host_provided': {
                'libraries': [
                    {'soname': 'libc.so.6', 'needed_by': ['bin/dosplex-compositor', 'lib/libwlroots.so.12'],
                     'provided_by': 'host', 'package': 'glibc', 'version': '2.41', 'license': 'LGPL-2.1-or-later'},
                    {'soname': 'libseat.so.1', 'needed_by': ['bin/dosplex-compositor', 'lib/libwlroots.so.12'],
                     'provided_by': 'host', 'package': 'seatd', 'version': '0.9.1', 'license': 'MIT'},
                ],
                'python': [
                    {'name': 'python3', 'kind': 'interpreter', 'used_by': ['libexec/helper.py'],
                     'provided_by': 'host', 'version': None},
                    {'name': 'evdev', 'kind': 'module', 'used_by': ['libexec/helper.py'], 'provided_by': 'host',
                     'version': None},
                ],
            },
        }
        self.sbom['files'] = [self.record(path) for path in sorted(self.files)]
        self.save()

    def add(self, path, data, mode, **record):
        record.setdefault('version', VERSION)
        record.setdefault('license_files', [])
        self.files[path] = (data, mode, record)
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)

    def record(self, path):
        data, mode, record = self.files[path]
        return dict(record, path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                    mode='%04o' % mode)

    def entry(self, path):
        return next(item for item in self.sbom['files'] if item['path'] == path)

    def drop_entry(self, path):
        self.sbom['files'] = [item for item in self.sbom['files'] if item['path'] != path]

    def write_notices(self):
        """Write the notices that the SBOM implies, and their record (the notices state nothing about it)."""
        try:
            text = generate_notices.generate(self.sbom, RIGHTS_HOLDER).encode()
        except generate_notices.NoticeError:
            return      # an SBOM the generator refuses: the notices stay as they were
        self.add(generate_notices.NOTICES_PATH, text, 0o644, kind='license', name='dosplex', origin='repository',
                 source_path='tools/generate-notices.py', license='AGPL-3.0-or-later',
                 license_files=['licenses/dosplex-LICENSE'])
        self.sbom['files'] = [item for item in self.sbom['files'] if item['path'] != generate_notices.NOTICES_PATH]
        self.sbom['files'].append(self.record(generate_notices.NOTICES_PATH))

    def save(self):
        if not self.keep_notices:
            self.write_notices()
        target = self.root / 'sbom' / 'dosplex-sbom.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.sbom, indent=2, sort_keys=True) + '\n')

    def problems(self):
        self.save()
        return check_sbom.check(self.root)

    def cli(self):
        self.save()
        return subprocess.run([sys.executable, str(CHECK), str(self.root)], capture_output=True, text=True)


class CheckSbomTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-sbom-test-')
        self.addCleanup(temporary.cleanup)
        self.payload = FakePayload(Path(temporary.name) / 'payload')

    def assertProblem(self, *fragments):
        problems = self.payload.problems()
        self.assertTrue(any(all(fragment in text for fragment in fragments) for text in problems),
                        'expected a problem mentioning %r, got %r' % (fragments, problems))

    def test_a_complete_payload_passes(self):
        self.assertEqual(self.payload.problems(), [])
        result = self.payload.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('check-sbom: OK', result.stdout)

    def test_the_patches_of_an_upstream_file_name_the_patch_and_its_digest(self):
        entry = self.payload.entry('lib/libwlroots.so.12')
        entry['patches'] = [{'path': 'packaging/release-build/patches/x.patch', 'sha256': 'c' * 64}]
        self.assertEqual(self.payload.problems(), [])
        for broken in ([], 'x.patch', [{'path': 'x.patch'}], [{'path': 'x.patch', 'sha256': 'nothex'}],
                       [{'sha256': 'c' * 64}]):
            entry['patches'] = broken
            self.assertProblem('lib/libwlroots.so.12', 'patches')
        del entry['patches']
        included = self.payload.entry('bin/dosplex-compositor')
        included['includes'] = [{'name': 'part', 'version': '1', 'license': 'MIT', 'license_files': [],
                                 'source_url': WLROOTS_URL, 'source_sha256': 'b' * 64,
                                 'patches': [{'path': 'p.patch', 'sha256': 'bad'}]}]
        self.assertProblem('bin/dosplex-compositor', 'includes part', 'patches')

    def test_the_command_exits_1_and_lists_the_problem_when_they_disagree(self):
        self.payload.drop_entry('lib/libwlroots.so.12')
        result = self.payload.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn('lib/libwlroots.so.12', result.stderr)

    def test_the_command_exits_2_for_a_directory_that_does_not_exist(self):
        result = subprocess.run([sys.executable, str(CHECK), str(self.payload.root / 'nowhere')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_a_bundled_library_without_an_sbom_entry_fails(self):
        self.payload.drop_entry('lib/libwlroots.so.12')
        self.assertProblem('lib/libwlroots.so.12', 'no SBOM entry')

    def test_a_library_that_names_no_license_text_fails(self):
        self.payload.entry('lib/libwlroots.so.12')['license_files'] = []
        self.assertProblem('lib/libwlroots.so.12', 'license text')

    def test_a_license_text_named_by_the_sbom_but_missing_fails(self):
        (self.payload.root / 'licenses/wlroots-0.17.4-LICENSE').unlink()
        self.payload.drop_entry('licenses/wlroots-0.17.4-LICENSE')
        self.assertProblem('licenses/wlroots-0.17.4-LICENSE', 'missing from the payload')

    def test_a_license_text_that_is_empty_fails(self):
        (self.payload.root / 'licenses/wlroots-0.17.4-LICENSE').write_bytes(b'')
        self.payload.entry('licenses/wlroots-0.17.4-LICENSE').update(size=0, sha256=hashlib.sha256(b'').hexdigest())
        self.assertProblem('licenses/wlroots-0.17.4-LICENSE', 'empty')

    def test_a_license_text_outside_licenses_fails(self):
        self.payload.entry('lib/libwlroots.so.12')['license_files'] = ['share/dosplex/VERSION']
        self.assertProblem('not under licenses/')

    def test_a_license_text_that_has_no_sbom_entry_fails(self):
        self.payload.drop_entry('licenses/wlroots-0.17.4-LICENSE')
        self.assertProblem('licenses/wlroots-0.17.4-LICENSE', 'no SBOM entry')

    def test_a_changed_file_fails(self):
        (self.payload.root / 'libexec/helper.py').write_bytes(b'import json\nimport evdev\nprint(1)\n')
        self.assertProblem('libexec/helper.py', 'differs')

    def test_a_file_with_another_mode_fails(self):
        (self.payload.root / 'bin/dosplex-compositor').chmod(0o644)
        self.assertProblem('bin/dosplex-compositor', 'mode')

    def test_a_file_that_is_not_listed_fails(self):
        (self.payload.root / 'bin/extra').write_bytes(b'x')
        self.assertProblem('bin/extra', 'no SBOM entry')

    def test_a_listed_file_that_is_missing_fails(self):
        (self.payload.root / 'libexec/helper.py').unlink()
        self.assertProblem('libexec/helper.py', 'not in the payload')

    def test_a_file_listed_twice_fails(self):
        self.payload.sbom['files'].append(dict(self.payload.entry('bin/dosplex-compositor')))
        self.assertProblem('bin/dosplex-compositor', 'twice')

    def test_a_symbolic_link_fails(self):
        os.symlink('libwlroots.so.12', self.payload.root / 'lib/libwlroots.so')
        self.assertProblem('lib/libwlroots.so', 'symbolic link')

    def test_an_incomplete_record_fails(self):
        for key in ('license', 'version', 'name'):
            with self.subTest(missing=key):
                self.payload = FakePayload(self.payload.root.parent / ('again-' + key))
                del self.payload.entry('bin/dosplex-compositor')[key]
                self.assertProblem('bin/dosplex-compositor', key)

    def test_an_upstream_record_needs_its_source(self):
        del self.payload.entry('lib/libwlroots.so.12')['source_url']
        self.assertProblem('lib/libwlroots.so.12', 'source_url')
        self.payload.entry('lib/libwlroots.so.12').update(source_url=WLROOTS_URL, source_sha256='nothex')
        self.assertProblem('lib/libwlroots.so.12', 'source_sha256')

    def test_a_record_from_the_repository_needs_a_source_path(self):
        del self.payload.entry('libexec/helper.py')['source_path']
        self.assertProblem('libexec/helper.py', 'source_path')

    def test_a_needed_library_that_is_not_declared_fails(self):
        self.payload.sbom['host_provided']['libraries'] = [
            item for item in self.payload.sbom['host_provided']['libraries'] if item['soname'] != 'libseat.so.1']
        self.assertProblem('libseat.so.1', 'neither bundled')

    def test_a_declared_host_library_that_nothing_needs_fails(self):
        self.payload.sbom['host_provided']['libraries'].append(
            {'soname': 'libgone.so.1', 'needed_by': ['bin/dosplex-compositor'], 'provided_by': 'host'})
        self.assertProblem('libgone.so.1', 'no file in the payload needs')

    def test_a_bundled_library_declared_host_provided_fails(self):
        self.payload.sbom['host_provided']['libraries'].append(
            {'soname': 'libwlroots.so.12', 'needed_by': ['bin/dosplex-compositor'], 'provided_by': 'host'})
        self.assertProblem('libwlroots.so.12', 'bundles it')

    def test_a_host_library_entry_needs_users_that_exist(self):
        self.payload.sbom['host_provided']['libraries'][1]['needed_by'] = ['bin/nothing']
        self.assertProblem('libseat.so.1', 'bin/nothing')

    def test_a_third_party_python_module_that_is_not_declared_fails(self):
        self.payload.sbom['host_provided']['python'] = [
            item for item in self.payload.sbom['host_provided']['python'] if item['name'] != 'evdev']
        self.assertProblem('evdev', 'host_provided.python')

    def test_the_release_name_has_to_agree_everywhere(self):
        self.payload.sbom['package']['version'] = '9.9.9'
        self.assertProblem('VERSION', '9.9.9')

    def test_a_missing_or_unknown_sbom_fails(self):
        (self.payload.root / 'sbom/dosplex-sbom.json').unlink()
        self.assertTrue(check_sbom.check(self.payload.root))
        self.payload.sbom['format_version'] = 2
        self.assertProblem('version 1')

    def test_an_sbom_in_another_place_can_be_named(self):
        elsewhere = self.payload.root.parent / 'elsewhere.json'
        self.payload.save()
        shutil.move(self.payload.root / 'sbom/dosplex-sbom.json', elsewhere)
        self.assertEqual(check_sbom.check(self.payload.root, elsewhere), [])


class AbiTests(unittest.TestCase):
    """A 32-bit file needs the 32-bit library of a soname; the library of the 64-bit files does not do."""

    LAYER = 'lib/libexample_x86.so'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-abi-test-')
        self.addCleanup(temporary.cleanup)
        self.payload = FakePayload(Path(temporary.name) / 'payload')
        self.payload.add(self.LAYER, make_elf(['libc.so.6', 'libxcb.so.1'], soname='libexample_x86.so', bits=32),
                         0o755, kind='library', name='example', version='1.0', origin='upstream',
                         source_url=WLROOTS_URL, source_sha256='b' * 64, license='BSD-2-Clause',
                         license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.payload.sbom['host_provided']['libraries'] += [
            {'soname': 'libc.so.6', 'needed_by': [self.LAYER], 'provided_by': 'host', 'abi': 'i386',
             'package': 'lib32-glibc', 'version': '2.41', 'license': 'LGPL-2.1-or-later'},
            {'soname': 'libxcb.so.1', 'needed_by': [self.LAYER], 'provided_by': 'host', 'abi': 'i386',
             'package': 'lib32-libxcb', 'version': '1.17.0', 'license': 'MIT'}]
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]

    def assertProblem(self, *fragments):
        problems = self.payload.problems()
        self.assertTrue(any(all(fragment in text for fragment in fragments) for text in problems),
                        'expected a problem mentioning %r, got %r' % (fragments, problems))

    def test_a_payload_with_a_32_bit_library_and_its_32_bit_host_libraries_passes(self):
        self.assertEqual(self.payload.problems(), [])
        text = (self.payload.root / 'licenses/dosplex-THIRD-PARTY-NOTICES.md').read_text(encoding='utf-8')
        self.assertIn('| `libc.so.6` (i386) | lib32-glibc | 2.41 |', text)
        self.assertIn('| `libc.so.6` | glibc | 2.41 |', text)

    def test_the_libraries_of_the_64_bit_files_do_not_satisfy_a_32_bit_file(self):
        # libc.so.6 is declared for the 64-bit files as well; that entry is not the 32-bit library.
        self.payload.sbom['host_provided']['libraries'] = [
            item for item in self.payload.sbom['host_provided']['libraries']
            if not (item['soname'] == 'libc.so.6' and item.get('abi') == 'i386')]
        self.assertProblem(self.LAYER, 'libc.so.6 (i386)', 'neither bundled')

    def test_a_32_bit_library_that_no_32_bit_file_needs_fails(self):
        self.payload.sbom['host_provided']['libraries'].append(
            {'soname': 'libseat.so.1', 'needed_by': [self.LAYER], 'provided_by': 'host', 'abi': 'i386'})
        self.assertProblem('libseat.so.1 (i386)', 'no file in the payload needs')

    def test_a_64_bit_bundled_library_does_not_satisfy_a_32_bit_file(self):
        self.payload.add('lib/libneeded.so.1', make_elf(['libc.so.6'], soname='libneeded.so.1'), 0o755, kind='library',
                         name='example', version='1.0', origin='upstream', source_url=WLROOTS_URL,
                         source_sha256='b' * 64, license='MIT', license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.payload.add(self.LAYER, make_elf(['libc.so.6', 'libneeded.so.1'], soname='libexample_x86.so', bits=32),
                         0o755, kind='library', name='example', version='1.0', origin='upstream',
                         source_url=WLROOTS_URL, source_sha256='b' * 64, license='BSD-2-Clause',
                         license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]
        self.assertProblem(self.LAYER, 'libneeded.so.1 (i386)', 'neither bundled')

    def test_an_abi_the_checker_does_not_know_fails(self):
        self.payload.sbom['host_provided']['libraries'][-1]['abi'] = 'armhf'
        self.assertProblem('libxcb.so.1', "abi 'armhf'")


class VulkanManifestTests(unittest.TestCase):
    """A Vulkan layer manifest of the payload leads to a library of the payload, by a path relative to itself."""

    LAYER = 'lib/libVkLayer_example_x86_64.so'
    MANIFEST = 'share/vulkan/implicit_layer.d/VkLayer_example.x86_64.json'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-vulkan-test-')
        self.addCleanup(temporary.cleanup)
        self.payload = FakePayload(Path(temporary.name) / 'payload')
        self.payload.add(self.LAYER, make_elf(['libc.so.6'], soname='libVkLayer_example_x86_64.so'), 0o755,
                         kind='library', name='example', version='1.0', origin='upstream', source_url=WLROOTS_URL,
                         source_sha256='b' * 64, license='BSD-2-Clause',
                         license_files=['licenses/wlroots-0.17.4-LICENSE'])
        for item in self.payload.sbom['host_provided']['libraries']:
            if item['soname'] == 'libc.so.6':
                item['needed_by'].append(self.LAYER)
        self.manifest('../../../lib/libVkLayer_example_x86_64.so')

    def manifest(self, library_path, data=None):
        if data is None:
            data = {'file_format_version': '1.0.0', 'layer': {'name': 'VK_LAYER_example', 'type': 'GLOBAL',
                                                              'library_path': library_path}}
        raw = data if isinstance(data, bytes) else json.dumps(data, indent=2).encode()
        self.payload.add(self.MANIFEST, raw, 0o644, kind='data', name='example', version='1.0', origin='upstream',
                         source_url=WLROOTS_URL, source_sha256='b' * 64, license='BSD-2-Clause',
                         license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]

    def assertProblem(self, *fragments):
        problems = self.payload.problems()
        self.assertTrue(any(all(fragment in text for fragment in fragments) for text in problems),
                        'expected a problem mentioning %r, got %r' % (fragments, problems))

    def test_a_relative_path_to_a_library_of_the_payload_passes(self):
        self.assertEqual(self.payload.problems(), [])

    def test_the_path_may_climb_and_descend_as_long_as_it_stays_in_the_payload(self):
        self.manifest('../../../lib/../lib/libVkLayer_example_x86_64.so')
        self.assertEqual(self.payload.problems(), [])

    def test_an_absolute_path_fails_because_the_payload_moves(self):
        self.manifest('/opt/dosplex/lib/libVkLayer_example_x86_64.so')
        self.assertProblem(self.MANIFEST, 'is absolute')

    def test_a_bare_library_name_fails_because_the_loader_would_search_the_system(self):
        self.manifest('libVkLayer_example_x86_64.so')
        self.assertProblem(self.MANIFEST, 'bare name')

    def test_a_path_that_leaves_the_payload_fails(self):
        self.manifest('../../../../../usr/lib/libVkLayer_example_x86_64.so')
        self.assertProblem(self.MANIFEST, 'leaves the payload')

    def test_a_path_to_a_file_that_is_not_in_the_payload_fails(self):
        self.manifest('../../../lib/libVkLayer_other_x86_64.so')
        self.assertProblem(self.MANIFEST, 'lib/libVkLayer_other_x86_64.so', 'not in the payload')

    def test_a_path_that_stops_in_the_manifest_directory_fails(self):
        self.manifest('./libVkLayer_example_x86_64.so')
        self.assertProblem(self.MANIFEST, 'share/vulkan/implicit_layer.d/libVkLayer_example_x86_64.so')

    def test_every_layer_of_a_manifest_with_several_is_checked(self):
        self.manifest(None, {'file_format_version': '1.0.1', 'layers': [
            {'name': 'VK_LAYER_one', 'library_path': '../../../lib/libVkLayer_example_x86_64.so'},
            {'name': 'VK_LAYER_two', 'library_path': '/usr/lib/libtwo.so'}]})
        self.assertProblem(self.MANIFEST, '/usr/lib/libtwo.so', 'absolute')

    def test_a_driver_manifest_is_checked_like_a_layer(self):
        self.manifest(None, {'file_format_version': '1.0.0', 'ICD': {'library_path': '/usr/lib/libdriver.so',
                                                                     'api_version': '1.3.0'}})
        self.assertProblem(self.MANIFEST, 'absolute')

    def test_a_manifest_that_is_not_json_or_describes_nothing_fails(self):
        self.manifest(None, b'{not json')
        self.assertProblem(self.MANIFEST, 'cannot read the manifest')
        self.manifest(None, {'file_format_version': '1.0.0'})
        self.assertProblem(self.MANIFEST, 'describes no layer')
        self.manifest(None, {'layer': {'name': 'VK_LAYER_example'}})
        self.assertProblem(self.MANIFEST, 'without a library_path')

    def test_other_json_files_are_not_vulkan_manifests(self):
        self.payload.add('share/dosplex/settings.json', b'{"library_path": "/absolute"}', 0o644, kind='data',
                         name='dosplex', origin='repository', source_path='x', license='AGPL-3.0-or-later',
                         license_files=['licenses/dosplex-LICENSE'])
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]
        self.assertEqual(self.payload.problems(), [])


class IncludedSourcesTests(unittest.TestCase):
    """An "includes" entry for code compiled from a source archive names the archive and its SHA-256."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-includes-test-')
        self.addCleanup(temporary.cleanup)
        self.payload = FakePayload(Path(temporary.name) / 'payload')
        self.include = {'name': 'vkroots', 'version': '0.1.0+5106d8a', 'license': 'MIT',
                        'license_files': ['licenses/wlroots-0.17.4-LICENSE'], 'note': 'a header library',
                        'source_url': 'https://example.org/vkroots.tar.gz', 'source_sha256': 'c' * 64}
        self.payload.entry('lib/libwlroots.so.12')['includes'] = [self.include]

    def assertProblem(self, *fragments):
        problems = self.payload.problems()
        self.assertTrue(any(all(fragment in text for fragment in fragments) for text in problems),
                        'expected a problem mentioning %r, got %r' % (fragments, problems))

    def test_an_entry_with_its_source_passes_and_reaches_the_notices(self):
        self.assertEqual(self.payload.problems(), [])
        text = (self.payload.root / 'licenses/dosplex-THIRD-PARTY-NOTICES.md').read_text(encoding='utf-8')
        self.assertIn('- Source: https://example.org/vkroots.tar.gz\n', text)

    def test_a_url_without_a_hash_or_a_hash_without_a_url_fails(self):
        del self.include['source_sha256']
        self.assertProblem('lib/libwlroots.so.12', 'vkroots', 'both source_url and source_sha256')
        self.include['source_sha256'] = 'c' * 64
        del self.include['source_url']
        self.assertProblem('lib/libwlroots.so.12', 'vkroots', 'both source_url and source_sha256')

    def test_a_source_that_is_not_https_or_a_hash_that_is_not_hex_fails(self):
        self.include['source_url'] = 'http://example.org/vkroots.tar.gz'
        self.assertProblem('lib/libwlroots.so.12', 'vkroots', 'https source_url')
        self.include['source_url'] = 'https://example.org/vkroots.tar.gz'
        self.include['source_sha256'] = 'not-a-hash'
        self.assertProblem('lib/libwlroots.so.12', 'vkroots', '64 hex digits')


class ElfAndImportTests(unittest.TestCase):
    def test_read_elf_reads_the_dynamic_section_of_a_hand_made_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.so'
            path.write_bytes(make_elf(['liba.so.1', 'libc.so.6'], soname='libx.so.2', runpath='$ORIGIN/../lib'))
            self.assertEqual(check_sbom.read_elf(path),
                             {'needed': ['liba.so.1', 'libc.so.6'], 'soname': 'libx.so.2',
                              'runpath': '$ORIGIN/../lib', 'rpath': None, 'abi': 'x86_64'})
            path.write_text('not an ELF file')
            self.assertIsNone(check_sbom.read_elf(path))

    def test_read_elf_reads_a_32_bit_file_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.so'
            path.write_bytes(make_elf(['libxcb.so.1', 'ld-linux.so.2'], soname='libx_x86.so', runpath='$ORIGIN',
                                      bits=32))
            self.assertEqual(check_sbom.read_elf(path),
                             {'needed': ['libxcb.so.1', 'ld-linux.so.2'], 'soname': 'libx_x86.so',
                              'runpath': '$ORIGIN', 'rpath': None, 'abi': 'i386'})
            path.write_bytes(make_elf(bits=32))
            self.assertEqual(check_sbom.read_elf(path)['needed'], [])

    def test_read_elf_refuses_a_big_endian_or_cut_off_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.so'
            for label, data in (('big endian', b'\x7fELF\x02\x02\x01' + bytes(100)),
                                ('cut off', b'\x7fELF\x01\x01\x01' + bytes(10))):
                path.write_bytes(data)
                with self.subTest(label), self.assertRaises(ValueError):
                    check_sbom.read_elf(path)

    @unittest.skipUnless(shutil.which('readelf'), 'readelf is not installed')
    def test_read_elf_agrees_with_readelf_on_real_programs(self):
        for program in ('/bin/ls', '/bin/sh', sys.executable):
            program = os.path.realpath(program)
            with self.subTest(program=program):
                info = check_sbom.read_elf(program)
                shown = subprocess.run(['readelf', '-d', program], capture_output=True, text=True).stdout
                expected = [line.split('[')[1].rstrip(']') for line in shown.splitlines() if '(NEEDED)' in line]
                self.assertEqual(info['needed'], expected)

    def test_third_party_imports_leaves_out_the_standard_library_and_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'script.py'
            path.write_text('from __future__ import annotations\nimport os.path, evdev.ecodes\n'
                            'from dbus import SystemBus\nfrom . import peer\nimport sibling_module\n'
                            'try:\n    import PAM\nexcept ImportError:\n    import pam\n')
            self.assertEqual(check_sbom.third_party_imports(path, {'sibling_module'}), {'evdev', 'dbus', 'PAM', 'pam'})

    def test_only_scripts_in_bin_and_libexec_count(self):
        self.assertEqual(check_sbom.python_files(['bin/a.py', 'libexec/b.py', 'share/c.py', 'bin/d.sh']),
                         ['bin/a.py', 'libexec/b.py'])


if __name__ == '__main__':
    unittest.main()
