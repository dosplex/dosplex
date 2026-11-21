#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""packaging/release-build/sources.py: what it accepts in sources.json and how it unpacks the pinned archives.

Hermetic: every case builds small archives and a sources.json of its own in a temporary directory, runs the real
sources.py on them and looks at the tree it makes. Nothing is downloaded.
"""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / 'packaging' / 'release-build'
SOURCES = RECIPE / 'sources.py'
WRAP = ROOT / 'compositor' / 'subprojects' / 'wlroots.wrap'

PATCH = '''a patch of plain's a.c
--- a/src/a.c
+++ b/src/a.c
@@ -1,1 +1,1 @@
-int a;
+int a = 1;
'''


def archive(path, top, files, empty_dirs=(), extra=()):
    """A .tar.gz with `files` (name -> text) and `empty_dirs` under the top-level directory `top`."""
    with tarfile.open(path, 'w:gz') as tar:
        for name in [top, *('%s/%s' % (top, item) for item in empty_dirs)]:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo('%s/%s' % (top, name))
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        for info in extra:
            tar.addfile(info)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-sources-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.origin = self.directory / 'origin'
        self.origin.mkdir()
        self.dest = self.directory / 'unpacked'
        self.list = []

    def add(self, name, top, files, empty_dirs=(), extra=(), **fields):
        """Make the archive for a source and list it in sources.json."""
        sha = archive(self.origin / ('%s.tar.gz' % name), top, files, empty_dirs, extra)
        source = {'name': name, 'version': '1.0', 'url': 'https://example.org/%s.tar.gz' % name, 'sha256': sha,
                  'directory': top, 'license': 'MIT', 'license_files': ['LICENSE']}
        source.update(fields)
        self.list.append(source)
        return source

    def write(self):
        path = self.directory / 'sources.json'
        path.write_text(json.dumps({'format': 1, 'sources': self.list}))
        return path

    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, '-I', str(SOURCES), *map(str, arguments)], capture_output=True,
                              text=True)

    def unpack(self):
        return self.run_tool('unpack', '--sources', self.write(), '--from', self.origin, '--dest', self.dest)

    def assertRefused(self, result, *fragments):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        for fragment in fragments:
            self.assertIn(fragment, result.stderr)

    def test_a_source_is_unpacked_into_its_directory(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n', 'src/a.c': 'int a;\n'})
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'plain-1.0/src/a.c').read_text(), 'int a;\n')

    def test_a_snapshot_goes_into_the_directory_the_host_archive_left_empty(self):
        self.add('host', 'host-1.0', {'LICENSE': 'host\n', 'subprojects/glue.wrap': 'x\n'},
                 empty_dirs=['subprojects/part'])
        self.add('part', 'part-0123456789abcdef0123456789abcdef01234567', {'LICENSE': 'part license\n', 'p.h': 'p\n'},
                 into='host-1.0/subprojects/part', commit='0123456789abcdef0123456789abcdef01234567',
                 directory='part-{commit}')
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'host-1.0/subprojects/part/LICENSE').read_text(), 'part license\n')
        self.assertEqual((self.dest / 'host-1.0/subprojects/glue.wrap').read_text(), 'x\n')
        self.assertFalse((self.dest / 'part-0123456789abcdef0123456789abcdef01234567').exists())

    def test_the_overlay_of_a_wrap_is_copied_over_the_snapshot(self):
        self.add('host', 'host-1.0', {'LICENSE': 'host\n', 'subprojects/packagefiles/part/meson.build': 'project()\n'},
                 empty_dirs=['subprojects/part'])
        self.add('part', 'part-snapshot', {'LICENSE': 'part\n', 'include/p.h': 'p\n'},
                 into='host-1.0/subprojects/part', overlay='host-1.0/subprojects/packagefiles/part')
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'host-1.0/subprojects/part/meson.build').read_text(), 'project()\n')
        self.assertEqual((self.dest / 'host-1.0/subprojects/part/include/p.h').read_text(), 'p\n')

    def test_a_snapshot_never_overwrites_a_file_of_the_host(self):
        self.add('host', 'host-1.0', {'LICENSE': 'host\n', 'subprojects/part/clash.h': 'host\n'})
        self.add('part', 'part-1', {'LICENSE': 'part\n', 'clash.h': 'part\n'}, into='host-1.0/subprojects/part')
        self.assertRefused(self.unpack(), 'would overwrite')
        self.assertEqual((self.dest / 'host-1.0/subprojects/part/clash.h').read_text(), 'host\n')

    def test_an_archive_that_does_not_match_its_pin_is_refused(self):
        source = self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'})
        source['sha256'] = '0' * 64
        self.assertRefused(self.unpack(), 'does not match the pinned SHA-256')
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_a_missing_archive_is_refused(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'})
        (self.origin / 'plain.tar.gz').unlink()
        self.assertRefused(self.unpack(), 'missing or does not match')

    def test_members_outside_the_top_level_directory_are_refused(self):
        stray = tarfile.TarInfo('elsewhere/evil.txt')
        stray.size = 0
        self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'}, extra=[stray])
        self.assertRefused(self.unpack(), 'unexpected member')

    def test_links_and_devices_are_refused(self):
        link = tarfile.TarInfo('plain-1.0/link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'}, extra=[link])
        self.assertRefused(self.unpack(), 'unexpected member')

    def test_a_license_file_that_the_archive_lacks_is_refused(self):
        self.add('plain', 'plain-1.0', {'README': 'no license here\n'})
        self.assertRefused(self.unpack(), 'license file LICENSE is not in the archive')

    def test_the_download_can_be_kept_under_another_name(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'}, filename='plain-renamed.tar.gz',
                 url='https://example.org/archive/0123456789abcdef.tar.gz')
        (self.origin / 'plain.tar.gz').rename(self.origin / 'plain-renamed.tar.gz')
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sources_json_is_checked_when_it_is_read(self):
        cases = {
            'a url that is not https': ({'url': 'http://example.org/plain.tar.gz'}, 'must be https'),
            'a sha256 that is not hex': ({'sha256': 'x' * 64}, 'sha256 must be 64 lowercase hex'),
            'no license files': ({'license_files': []}, 'license_files'),
            'a license file outside the tree': ({'license_files': ['../LICENSE']}, 'relative path'),
            'a directory that is a path': ({'directory': 'a/b'}, 'top-level directory'),
            'an odd file name': ({'filename': 'x y.tar.gz'}, 'usable archive file name'),
            'a commit that is not a commit': ({'commit': 'abc'}, 'commit must be 40'),
            'a directory that uses {commit} without one': ({'directory': 'other-{commit}'}, 'has no commit'),
            'an into outside any earlier source': ({'into': 'other-1.0/sub'}, 'into must lie inside'),
            'an into that is the whole directory of another source': ({'into': 'plain-1.0'}, 'into must lie inside'),
            'an absolute into': ({'into': '/etc/x'}, 'relative path'),
            'an into with ..': ({'into': 'plain-1.0/../x'}, 'relative path'),
            'an overlay without into': ({'overlay': 'plain-1.0/sub'}, 'overlay needs into'),
            'a product outside the payload directories': (
                {'products': {'etc/x.json': {'kind': 'data', 'license': 'MIT'}}}, 'must be a file below'),
            'a product that names no kind': ({'products': {'lib/x.so': {'license': 'MIT'}}}, 'needs a kind'),
            'a product that names no license': (
                {'products': {'lib/x.so': {'kind': 'library'}}}, 'needs a kind'),
            'a product that another source builds': (
                {'products': {'lib/p.so': {'kind': 'library', 'license': 'MIT'}}}, 'also built from another source'),
            'compiled into a file that nothing builds': (
                {'embedded_in': ['lib/nothing.so'], 'note': 'x'}, 'no source listed before it builds'),
            'compiled into a file without a note for the SBOM': (
                {'embedded_in': ['bin/dosplex-compositor']}, 'needs a note'),
            'a library given as a path': ({'libraries': ['lib/x.so']}, 'sonames, not paths'),
            'a template without a library': (
                {'products': {'share/m.json': {'kind': 'data', 'license': 'MIT', 'template': 'm.json.in',
                                                'family': 'x86_64'}}}, 'needs the family and the library'),
            'a template whose library is not a product of the source': (
                {'products': {'share/m.json': {'kind': 'data', 'license': 'MIT', 'template': 'm.json.in',
                                                'family': 'x86_64', 'library': 'lib/other.so'}}},
                 'needs the family and the library'),
            'a layer name that is not a text': (
                {'products': {'lib/l.so': {'kind': 'library', 'license': 'MIT'},
                              'share/m.json': {'kind': 'data', 'license': 'MIT', 'template': 'm.json.in',
                                               'family': 'x86_64', 'library': 'lib/l.so', 'layer_name': ''}}},
                 'layer_name must be a text'),
            'a disable variable that is not a text': (
                {'products': {'lib/l.so': {'kind': 'library', 'license': 'MIT'},
                              'share/m.json': {'kind': 'data', 'license': 'MIT', 'template': 'm.json.in',
                                               'family': 'x86_64', 'library': 'lib/l.so', 'disable_variable': 1}}},
                 'disable_variable must be a text'),
        }
        for label, (change, fragment) in cases.items():
            with self.subTest(label):
                self.list = []
                self.add('plain', 'plain-1.0', {'LICENSE': 'the text\n'},
                         products={'lib/p.so': {'kind': 'library', 'license': 'MIT'}})
                source = self.add('other', 'other-1.0', {'LICENSE': 'x\n'})
                source.update(change)
                self.assertRefused(self.unpack(), fragment)

    def test_a_source_can_be_compiled_into_the_product_of_an_earlier_one(self):
        self.add('host', 'host-1.0', {'LICENSE': 'host\n'},
                 products={'lib/libhost.so': {'kind': 'library', 'license': 'BSD-2-Clause'}})
        self.add('part', 'part-1', {'LICENSE': 'part\n'}, embedded_in=['lib/libhost.so', 'bin/dosplex-compositor'],
                 note='a header library')
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_sources_cannot_share_a_name_or_a_download(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'x\n'})
        self.add('plain', 'plain-2.0', {'LICENSE': 'x\n'})
        self.assertRefused(self.unpack(), 'listed twice')
        self.list = []
        self.add('one', 'one-1.0', {'LICENSE': 'x\n'}, filename='same.tar.gz')
        self.add('two', 'two-1.0', {'LICENSE': 'x\n'}, filename='same.tar.gz')
        self.assertRefused(self.unpack(), 'another source is downloaded under the name same.tar.gz')

    def write_patch(self, name, text=PATCH):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return name

    def test_a_patch_is_applied_to_the_unpacked_source(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'x\n', 'src/a.c': 'int a;\n'},
                 patches=[self.write_patch('patches/plain-a.patch')])
        result = self.unpack()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'plain-1.0/src/a.c').read_text(), 'int a = 1;\n')
        self.assertIn('plain: applied plain-a.patch', result.stderr)
        self.assertFalse(list(self.dest.rglob('*.orig')) + list(self.dest.rglob('*.rej')))

    def test_a_patch_that_does_not_apply_stops_the_unpack(self):
        self.add('plain', 'plain-1.0', {'LICENSE': 'x\n', 'src/a.c': 'int b;\n'},
                 patches=[self.write_patch('patches/plain-a.patch')])
        self.assertRefused(self.unpack(), 'plain-a.patch: does not apply to plain-1.0')
        self.list = []
        shutil.rmtree(self.dest)
        # Applied already (the tree is not the pristine archive): refused as well, not silently skipped.
        self.add('plain', 'plain-1.0', {'LICENSE': 'x\n', 'src/a.c': 'int a = 1;\n'},
                 patches=[self.write_patch('patches/plain-a.patch')])
        self.assertRefused(self.unpack(), 'plain-a.patch: does not apply to plain-1.0')

    def test_a_patch_is_a_relative_patch_file_that_exists(self):
        cases = {
            'a missing file': (['patches/none.patch'], 'the patch file does not exist'),
            'an absolute path': (['/etc/passwd.patch'], 'a patch must be a relative path'),
            'a parent directory': (['../x.patch'], 'a patch must be a relative path'),
            'another suffix': (['patches/a.diff'], 'a patch file is named *.patch'),
            'not a list': ('patches/a.patch', 'patches must be a list of distinct patch files'),
            'the same patch twice': (['patches/a.patch', 'patches/a.patch'], 'patches must be a list of distinct'),
        }
        for label, (patches, fragment) in cases.items():
            with self.subTest(label):
                self.list = []
                self.add('plain', 'plain-1.0', {'LICENSE': 'x\n'}, patches=patches)
                self.assertRefused(self.unpack(), fragment)

    def test_the_patches_of_the_release_build_exist_and_apply_cleanly_in_form(self):
        """Every patch sources.json names is in the recipe and is a unified diff with a/ and b/ prefixes."""
        for source in json.loads((RECIPE / 'sources.json').read_text())['sources']:
            for item in source.get('patches', []):
                with self.subTest(item):
                    text = (RECIPE / item).read_text()
                    self.assertRegex(text, r'(?m)^--- a/')
                    self.assertRegex(text, r'(?m)^\+\+\+ b/')

    def test_the_development_wrap_is_the_bundled_wlroots_with_the_same_patches(self):
        """compositor/subprojects/wlroots.wrap builds the development compositor and the tests against the same
        archive the release bundles, with the same patches: meson applies the copies under packagefiles/, which have
        to stay equal to the recipe's."""
        wlroots = next(source for source in json.loads((RECIPE / 'sources.json').read_text())['sources']
                       if source['name'] == 'wlroots')
        wrap = dict(line.split(' = ', 1) for line in WRAP.read_text().splitlines() if ' = ' in line)
        self.assertEqual(wrap['source_url'], wlroots['url'])
        self.assertEqual(wrap['source_hash'], wlroots['sha256'])
        self.assertEqual(wrap['source_filename'], Path(wlroots['url']).name)
        self.assertEqual(wrap['directory'], wlroots['directory'])
        self.assertEqual(wrap['diff_files'].split(', '), [Path(item).name for item in wlroots['patches']])
        for item in wlroots['patches']:
            with self.subTest(item):
                copy = WRAP.parent / 'packagefiles' / Path(item).name
                self.assertEqual(copy.read_bytes(), (RECIPE / item).read_bytes(),
                                 '%s differs from %s' % (copy, RECIPE / item))

    def test_the_sources_of_the_release_build_are_valid(self):
        result = subprocess.run([sys.executable, '-I', '-c',
                                 'import importlib.util, sys; spec = importlib.util.spec_from_file_location("s", sys.argv[1]);'
                                 ' m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);'
                                 ' print(len(m.load()))', str(SOURCES)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(int(result.stdout), 3)


if __name__ == '__main__':
    unittest.main()
