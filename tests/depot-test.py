#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Unit tests for dosplex-depot: manifest parsing, VDF text and tree verification.

The tests build their own synthetic manifests with a small protobuf encoder and make their own folders, so they
need no Steam data. Symlink and fifo cases prove that the verifier never reads through a link.
"""
import hashlib
import importlib.util
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / 'packaging/steamos/dosplex-depot.py'
_spec = importlib.util.spec_from_file_location('dosplex_depot', MODULE_PATH)
depot = importlib.util.module_from_spec(_spec)
sys.modules['dosplex_depot'] = depot
_spec.loader.exec_module(depot)

DepotError = depot.DepotError


# A tiny protobuf and section encoder.

def varint(number):
    out = bytearray()
    while True:
        low = number & 0x7F
        number >>= 7
        if number:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def f_varint(number, value):
    return varint(number << 3) + varint(value)


def f_bytes(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def sha(data):
    return hashlib.sha1(data).digest()


def entry(name, size=0, flags=0, digest=None, link=None, chunks=1):
    if isinstance(name, str):
        name = name.encode()
    out = f_bytes(1, name) + f_varint(2, size) + f_varint(3, flags)
    if digest is not None:
        out += f_bytes(4, b'\x11' * 20) + f_bytes(5, digest)
    for _ in range(chunks):
        out += f_bytes(6, f_bytes(1, b'\x22' * 20) + f_varint(3, 5))
    if link is not None:
        out += f_bytes(7, link if isinstance(link, bytes) else link.encode())
    return out


def file_entry(name, data, flags=0):
    return entry(name, len(data), flags, sha(data))


def section(magic, payload):
    return struct.pack('<II', magic, len(payload)) + payload


def metadata(depot_id=262831, gid=5318254292612917484, created=1527594793, encrypted=False):
    return (f_varint(1, depot_id) + f_varint(2, gid) + f_varint(3, created) + f_varint(4, int(encrypted))
            + f_varint(5, 1000) + f_varint(6, 500))


def build(entries, zipped=False, **meta):
    payload = b''.join(f_bytes(1, item) for item in entries)
    data = (section(depot.PAYLOAD_MAGIC, payload) + section(depot.METADATA_MAGIC, metadata(**meta))
            + section(depot.SIGNATURE_MAGIC, b'') + struct.pack('<I', depot.END_MAGIC))
    return zip_it(data) if zipped else data


def zip_it(data, members=1):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index in range(members):
            archive.writestr(f'member{index}', data)
    return buffer.getvalue()


def manifest_of(entries, **meta):
    return depot.parse_manifest(build(entries, **meta))


class ManifestTest(unittest.TestCase):
    def sample(self):
        return [
            file_entry('bin\\game', b'binary', depot.EXECUTABLE),
            entry('bin', 0, depot.DIRECTORY, chunks=0),
            entry('lib/current', 0, depot.SYMLINK, link='libreal.so.1', chunks=0),
            file_entry('Data/Config.ini', b'cfg', depot.USER_CONFIG),
            file_entry('Data/Saved.ini', b'saved', depot.VERSIONED_USER_CONFIG),
            file_entry('readme.txt', b''),
        ]

    def check_sample(self, manifest):
        self.assertEqual((manifest.depot_id, manifest.gid, manifest.creation_time),
                         (262831, 5318254292612917484, 1527594793))
        names = [f.name for f in manifest.files]
        self.assertEqual(names, ['bin/game', 'bin', 'lib/current', 'Data/Config.ini', 'Data/Saved.ini',
                                 'readme.txt'])
        game, directory, link, config, versioned, empty = manifest.files
        self.assertTrue(game.is_executable)
        self.assertFalse(game.is_directory or game.is_symlink or game.is_user_config)
        self.assertEqual((game.size, game.sha1), (6, hashlib.sha1(b'binary').hexdigest()))
        self.assertTrue(directory.is_directory)
        self.assertIsNone(directory.sha1)
        self.assertTrue(link.is_symlink)
        self.assertEqual((link.link, link.sha1), ('libreal.so.1', None))
        self.assertTrue(config.is_user_config)
        self.assertTrue(versioned.is_user_config)
        self.assertEqual(empty.sha1, hashlib.sha1(b'').hexdigest())

    def test_a_manifest_round_trips_with_every_kind_of_entry(self):
        self.check_sample(depot.parse_manifest(build(self.sample())))

    def test_the_zipped_form_reads_the_same(self):
        plain = depot.parse_manifest(build(self.sample()))
        zipped = depot.parse_manifest(build(self.sample(), zipped=True))
        self.check_sample(zipped)
        self.assertEqual(plain, zipped)

    def test_names_keep_their_case_and_backslashes_become_slashes(self):
        manifest = manifest_of([file_entry('A\\B\\c.TXT', b'x')])
        self.assertEqual(manifest.files[0].name, 'A/B/c.TXT')

    def test_an_exact_duplicate_entry_is_kept_once(self):
        item = file_entry('same', b'x')
        self.assertEqual(len(manifest_of([item, item, file_entry('other', b'y')]).files), 2)

    def test_a_manifest_with_no_files_is_accepted(self):
        self.assertEqual(manifest_of([]).files, [])

    def test_every_refusal_raises_a_depot_error(self):
        good = build([file_entry('a', b'x')])
        payload_only = section(depot.PAYLOAD_MAGIC, b'') + struct.pack('<I', depot.END_MAGIC)
        no_payload = section(depot.METADATA_MAGIC, metadata()) + struct.pack('<I', depot.END_MAGIC)

        def raw_payload(body):
            return (section(depot.PAYLOAD_MAGIC, body) + section(depot.METADATA_MAGIC, metadata())
                    + struct.pack('<I', depot.END_MAGIC))

        cases = {
            'empty input': b'',
            'not sections': b'this is not a manifest at all',
            'no payload section': no_payload,
            'no metadata section': payload_only,
            'no end marker': good[:-4],
            'data after the end marker': good + b'x',
            'unknown section magic': struct.pack('<II', 0xDEADBEEF, 0) + struct.pack('<I', depot.END_MAGIC),
            'truncated section': good[:20],
            'truncated section header': good[:6],
            'truncated varint': raw_payload(b'\x0a\x03\x08\x80'),
            'truncated length-delimited field': raw_payload(f_bytes(1, entry('a', 1, 0, sha(b'x')))[:-3]),
            'length beyond the buffer': raw_payload(b'\x0a\x7f' + b'abc'),
            'unknown wire type 3': raw_payload(f_bytes(1, f_varint(2, 1) + varint(9 << 3 | 3))),
            'unknown wire type 7': raw_payload(f_bytes(1, varint(9 << 3 | 7) + b'\x00')),
            'encrypted file names': build([file_entry('a', b'x')], encrypted=True),
            'empty name': build([entry('', 1, 0, sha(b'x'))]),
            'missing name': build([f_varint(2, 1) + f_bytes(5, sha(b'x'))]),
            'name not UTF-8': build([entry(b'bad\xff\xfename', 1, 0, sha(b'x'))]),
            'absolute name': build([file_entry('/etc/passwd', b'x')]),
            'absolute name with backslash': build([file_entry('\\windows\\x', b'x')]),
            'NUL in name': build([file_entry('a\0b', b'x')]),
            'dot component': build([file_entry('a/./b', b'x')]),
            'dot-dot component': build([file_entry('a/../b', b'x')]),
            'dot-dot with backslashes': build([file_entry('a\\..\\b', b'x')]),
            'empty component': build([file_entry('a//b', b'x')]),
            'trailing slash': build([file_entry('a/', b'x')]),
            'lone dot-dot': build([file_entry('..', b'x')]),
            'SHA-1 too short': build([entry('a', 1, 0, b'\x00' * 19)]),
            'SHA-1 too long': build([entry('a', 1, 0, b'\x00' * 21)]),
            'SHA-1 missing': build([entry('a', 1, 0, None)]),
            'same name twice with different content': build([file_entry('a', b'x'), file_entry('a', b'y')]),
            'same name twice with different flags': build([file_entry('a', b'x'),
                                                                   file_entry('a', b'x', depot.EXECUTABLE)]),
            'varint longer than 10 bytes': raw_payload(b'\x0a\x05' + b'\x80' * 11 + b'\x00'),
            'varint over 64 bits': raw_payload(b'\x0a\x0c\x10' + b'\xff' * 9 + b'\x7f'),
            'symlink with an empty target': build([entry('l', 0, depot.SYMLINK)]),
            'symlink target not UTF-8': build([entry('l', 0, depot.SYMLINK, link=b'\xff\xfe')]),
            'directory and symlink together': build([entry('d', 0, depot.DIRECTORY | depot.SYMLINK, link='x')]),
            'zip with no members': zip_it(b'', members=0),
            'zip with several members': zip_it(good, members=2),
            'broken zip': b'PK\x03\x04 and then nonsense',
        }
        for label, data in cases.items():
            with self.subTest(label):
                with self.assertRaises(DepotError):
                    depot.parse_manifest(data)

    def test_a_zip_member_larger_than_the_limit_is_refused_before_reading(self):
        data = build([file_entry('a', b'x')], zipped=True)
        with self.assertRaises(DepotError):
            depot.parse_manifest(data, max_bytes=10)
        self.assertEqual(len(depot.parse_manifest(data).files), 1)

    def test_depot_error_is_a_value_error(self):
        self.assertTrue(issubclass(DepotError, ValueError))


class ReadManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_regular_file_is_read(self):
        path = self.dir / '1_2.manifest'
        path.write_bytes(build([file_entry('a', b'x')]))
        self.assertEqual(depot.read_manifest(path).files[0].name, 'a')

    def test_a_directory_is_refused(self):
        with self.assertRaises(DepotError):
            depot.read_manifest(self.dir)

    def test_a_symlink_to_a_file_is_refused(self):
        real = self.dir / 'real.manifest'
        real.write_bytes(build([file_entry('a', b'x')]))
        link = self.dir / 'link.manifest'
        link.symlink_to(real)
        with self.assertRaises(DepotError):
            depot.read_manifest(link)

    def test_a_fifo_is_refused_without_blocking(self):
        fifo = self.dir / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaises(DepotError):
            depot.read_manifest(fifo)

    def test_an_oversized_file_is_refused(self):
        path = self.dir / 'big.manifest'
        path.write_bytes(build([file_entry('a', b'x')]))
        with self.assertRaises(DepotError):
            depot.read_manifest(path, max_bytes=20)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(DepotError):
            depot.read_manifest(self.dir / 'nothing')


class TreeTest(unittest.TestCase):
    FILES = {'game': b'game binary', 'data/a.pak': b'pak a', 'data/deep/b.pak': b'pak b', 'readme.txt': b'hello'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.other = Path(self.tmp.name) / 'other'
        self.root.mkdir()
        self.other.mkdir()
        for name, data in self.FILES.items():
            self.put(name, data)

    def put(self, name, data, base=None):
        path = (base or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def manifest(self, files=None, extra=()):
        files = self.FILES if files is None else files
        return manifest_of([file_entry(n, d) for n, d in files.items()] + list(extra))

    def verify(self, manifests=None, **kwargs):
        if manifests is None:
            manifests = [self.manifest()]
        return depot.verify_tree(manifests, self.root, **kwargs)

    def test_a_tree_that_matches_is_all_pristine(self):
        verdict = self.verify()
        self.assertEqual(verdict.pristine, sorted(self.FILES))
        self.assertEqual((verdict.changed, verdict.missing, verdict.extra), ([], [], []))
        self.assertTrue(verdict.ok)

    def test_a_changed_byte_with_the_same_size_is_changed(self):
        self.put('game', b'game binarY')
        verdict = self.verify()
        self.assertEqual(verdict.changed, ['game'])
        self.assertNotIn('game', verdict.pristine)
        self.assertFalse(verdict.ok)

    def test_a_size_difference_is_changed(self):
        self.put('readme.txt', b'hello!')
        self.assertEqual(self.verify().changed, ['readme.txt'])

    def test_a_missing_file_is_missing_and_not_ok(self):
        (self.root / 'data/a.pak').unlink()
        verdict = self.verify()
        self.assertEqual(verdict.missing, ['data/a.pak'])
        self.assertFalse(verdict.ok)

    def test_a_missing_parent_directory_makes_its_files_missing(self):
        for path in [self.root / 'data/deep/b.pak', self.root / 'data/deep']:
            path.rmdir() if path.is_dir() else path.unlink()
        self.assertEqual(self.verify().missing, ['data/deep/b.pak'])

    def test_extra_files_symlinks_and_fifos_are_extra_but_still_ok(self):
        self.put('new.txt', b'mine')
        self.put('data/save/slot1', b'save')
        (self.root / 'link').symlink_to('readme.txt')
        os.mkfifo(self.root / 'pipe')
        verdict = self.verify()
        self.assertEqual(verdict.extra, ['data/save/slot1', 'link', 'new.txt', 'pipe'])
        self.assertTrue(verdict.ok)

    def test_a_symlinked_directory_is_one_extra_object_and_is_not_walked(self):
        self.put('inside.txt', b'x', base=self.other)
        (self.root / 'linked').symlink_to(self.other)
        self.assertEqual(self.verify().extra, ['linked'])

    def test_empty_directories_are_never_extra(self):
        (self.root / 'emptydir').mkdir()
        self.assertEqual(self.verify().extra, [])

    def test_a_file_replaced_by_a_symlink_to_identical_content_is_changed_and_never_opened(self):
        target = self.other / 'copy'
        os.mkfifo(target)
        (self.root / 'readme.txt').unlink()
        (self.root / 'readme.txt').symlink_to(target)
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            if os.fspath(path) in (str(target), str(self.root / 'readme.txt')):
                raise AssertionError('the symlink or its target was opened')
            return real_open(path, *args, **kwargs)

        with mock.patch('os.open', side_effect=spy):
            verdict = self.verify()
        self.assertEqual(verdict.changed, ['readme.txt'])
        self.assertNotIn('readme.txt', verdict.pristine)
        self.assertNotIn(str(target), opened)

    def test_a_symlink_to_a_file_with_identical_content_is_changed(self):
        copy = self.put('copy', b'hello', base=self.other)
        (self.root / 'readme.txt').unlink()
        (self.root / 'readme.txt').symlink_to(copy)
        self.assertEqual(self.verify().changed, ['readme.txt'])

    def test_a_parent_directory_replaced_by_a_symlink_to_the_right_files_is_changed(self):
        for name in ('data/a.pak', 'data/deep/b.pak'):
            self.put(name, self.FILES[name], base=self.other)
        for name in ('data/a.pak', 'data/deep/b.pak'):
            (self.root / name).unlink()
        (self.root / 'data/deep').rmdir()
        (self.root / 'data').rmdir()
        (self.root / 'data').symlink_to(self.other / 'data')
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        with mock.patch('os.open', side_effect=spy):
            verdict = self.verify()
        self.assertEqual(verdict.changed, ['data/a.pak', 'data/deep/b.pak'])
        self.assertEqual(verdict.pristine, ['game', 'readme.txt'])
        self.assertFalse(any('data' in p for p in opened))
        self.assertFalse(verdict.ok)

    def test_a_symlink_entry_that_is_right_is_pristine(self):
        (self.root / 'lib').symlink_to('real.so')
        item = entry('lib', 0, depot.SYMLINK, link='real.so', chunks=0)
        verdict = self.verify([self.manifest(extra=[item])])
        self.assertIn('lib', verdict.pristine)
        self.assertEqual(verdict.extra, [])

    def test_a_symlink_entry_with_a_wrong_target_is_changed(self):
        (self.root / 'lib').symlink_to('other.so')
        item = entry('lib', 0, depot.SYMLINK, link='real.so', chunks=0)
        self.assertEqual(self.verify([self.manifest(extra=[item])]).changed, ['lib'])

    def test_a_symlink_entry_where_a_file_is_is_changed_and_where_nothing_is_is_missing(self):
        item = entry('lib', 0, depot.SYMLINK, link='real.so', chunks=0)
        self.put('lib', b'real.so')
        self.assertEqual(self.verify([self.manifest(extra=[item])]).changed, ['lib'])
        (self.root / 'lib').unlink()
        self.assertEqual(self.verify([self.manifest(extra=[item])]).missing, ['lib'])

    def test_a_directory_entry_matches_a_real_directory_and_is_not_listed(self):
        item = entry('data/deep', 0, depot.DIRECTORY, chunks=0)
        verdict = self.verify([self.manifest(extra=[item])])
        self.assertNotIn('data/deep', verdict.pristine)
        self.assertEqual((verdict.changed, verdict.missing), ([], []))

    def test_a_missing_directory_entry_is_not_reported(self):
        item = entry('nodir', 0, depot.DIRECTORY, chunks=0)
        verdict = self.verify([self.manifest(extra=[item])])
        self.assertEqual((verdict.changed, verdict.missing, verdict.extra), ([], [], []))

    def test_a_directory_entry_where_a_file_is_is_changed(self):
        item = entry('readme.txt', 0, depot.DIRECTORY, chunks=0)
        manifest = manifest_of([file_entry(n, d) for n, d in self.FILES.items() if n != 'readme.txt'] + [item])
        self.assertEqual(self.verify([manifest]).changed, ['readme.txt'])

    def test_a_file_entry_where_a_directory_is_is_changed(self):
        (self.root / 'readme.txt').unlink()
        (self.root / 'readme.txt').mkdir()
        self.assertEqual(self.verify().changed, ['readme.txt'])

    def test_two_manifests_listing_a_path_pass_when_the_tree_matches_the_second(self):
        first = manifest_of([file_entry('game', b'old build')], depot_id=1, gid=1)
        rest = {n: d for n, d in self.FILES.items() if n != 'game'}
        second = manifest_of([file_entry('game', b'game binary')] + [file_entry(n, d) for n, d in rest.items()],
                             depot_id=2, gid=2)
        verdict = self.verify([first, second])
        self.assertEqual(verdict.pristine, sorted(self.FILES))
        self.assertTrue(verdict.ok)

    def test_a_path_matching_neither_of_two_manifests_is_changed(self):
        first = manifest_of([file_entry('game', b'old build')], depot_id=1, gid=1)
        second = self.manifest()
        self.put('game', b'a third thing')
        self.assertEqual(self.verify([first, second]).changed, ['game'])

    def test_several_manifests_together_cover_one_game(self):
        left = manifest_of([file_entry('game', b'game binary'), file_entry('readme.txt', b'hello')], depot_id=1)
        right = manifest_of([file_entry(n, d) for n, d in self.FILES.items() if n.startswith('data')], depot_id=2)
        verdict = self.verify([left, right])
        self.assertEqual((verdict.pristine, verdict.extra), (sorted(self.FILES), []))

    def test_skipping_content_accepts_a_changed_byte_but_not_a_changed_size(self):
        self.put('game', b'game binarY')
        self.put('readme.txt', b'hello!')
        verdict = self.verify(check_content=False)
        self.assertIn('game', verdict.pristine)
        self.assertEqual(verdict.changed, ['readme.txt'])

    @unittest.skipIf(os.geteuid() == 0, 'root can read any file')
    def test_an_unreadable_file_is_changed_not_an_exception(self):
        path = self.root / 'game'
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        verdict = self.verify()
        self.assertEqual(verdict.changed, ['game'])

    @unittest.skipIf(os.geteuid() == 0, 'root can read any file')
    def test_an_unlistable_directory_is_changed(self):
        path = self.root / 'data/deep'
        path.chmod(0)
        self.addCleanup(path.chmod, 0o755)
        verdict = self.verify()
        self.assertIn('data/deep', verdict.changed)
        self.assertIn('data/deep/b.pak', verdict.changed)

    def test_on_file_is_called_once_per_regular_file_with_its_size(self):
        calls = []
        self.verify(on_file=lambda name, size: calls.append((name, size)))
        self.assertEqual(sorted(calls), sorted((n, len(d)) for n, d in self.FILES.items()))

    def test_names_that_differ_only_in_case_are_different_files(self):
        self.put('Readme.txt', b'hello')
        verdict = self.verify()
        self.assertIn('readme.txt', verdict.pristine)
        self.assertEqual(verdict.extra, ['Readme.txt'])
        manifest = manifest_of([file_entry('README.TXT', b'hello')])
        verdict = depot.verify_tree([manifest], self.root)
        self.assertEqual(verdict.missing, ['README.TXT'])

    def test_every_list_is_sorted(self):
        for name in ('zz', 'aa', 'mm'):
            self.put(name, b'x')
        self.assertEqual(self.verify().extra, ['aa', 'mm', 'zz'])

    def test_a_large_file_is_hashed_in_blocks(self):
        data = os.urandom(2_500_000)
        self.put('big.bin', data)
        manifest = self.manifest(extra=[file_entry('big.bin', data)])
        self.assertIn('big.bin', self.verify([manifest]).pristine)

    def test_a_root_that_is_not_a_real_directory_is_refused(self):
        link = Path(self.tmp.name) / 'rootlink'
        link.symlink_to(self.root)
        plain = self.root / 'game'
        missing = Path(self.tmp.name) / 'absent'
        for label, root in (('symlink', link), ('file', plain), ('missing', missing)):
            with self.subTest(label):
                with self.assertRaises(DepotError):
                    depot.verify_tree([self.manifest()], root)


APP_MANIFEST = '''"AppState"
{
\t"appid"\t\t"4242"
\t"Universe"\t\t"1"
\t"name"\t\t"Test Game"
\t"installdir"\t\t"Test Game"
\t"SizeOnDisk"\t\t"1234"
\t"InstalledDepots"
\t{
\t\t"4243"
\t\t{
\t\t\t"manifest"\t\t"1111111111111111111"
\t\t\t"size"\t\t"1000"
\t\t}
\t\t"4244"
\t\t{
\t\t\t"manifest"\t\t"2222222222222222222"
\t\t\t"size"\t\t"234"
\t\t}
\t}
\t"InstallScripts"
\t{
\t\t"4243"\t\t"installscript.vdf"
\t}
\t"UserConfig"
\t{
\t\t"language"\t\t"english"
\t}
}
'''

LIBRARY_FOLDERS = '''"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"/srv/steam/someone"
\t\t"label"\t\t""
\t\t"contentid"\t\t"1234567890"
\t\t"apps"
\t\t{
\t\t\t"4242"\t\t"1234"
\t\t}
\t}
}
'''


class AsInstalledTest(unittest.TestCase):
    """Names take the letter case they have on disk, as Steam on Linux keeps it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-depot-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def put(self, name, data=b'x'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def manifest(self, *names, folders=()):
        entries = [file_entry(name, b'x') for name in names] + [entry(name, 0, depot.DIRECTORY) for name in folders]
        return depot.parse_manifest(build(entries, depot_id=1, gid=2))

    def names(self, manifest):
        return [item.name for item in depot.as_installed([manifest], self.root)[0].files]

    def test_a_name_that_is_on_disk_as_listed_stays(self):
        self.put('game_Data/level1')
        self.put('Game.exe')
        self.assertEqual(self.names(self.manifest('game_Data/level1', 'Game.exe')), ['game_Data/level1', 'Game.exe'])

    def test_a_folder_that_kept_its_old_case_gives_its_spelling_to_everything_below(self):
        self.put('Game_Data/level1')
        self.put('Game_Data/Sub/a')
        self.assertEqual(self.names(self.manifest('game_Data/level1', 'game_data/sub/A', folders=('game_Data',))),
                         ['Game_Data/level1', 'Game_Data/Sub/a', 'Game_Data'])

    def test_the_spelling_that_holds_the_file_wins_over_an_empty_folder_spelled_as_listed(self):
        # The state a torn game was left in: the listed spelling exists and is empty, the files are in the other.
        self.put('Game_Data/level1')
        (self.root / 'game_Data').mkdir()
        self.assertEqual(self.names(self.manifest('game_Data/level1')), ['Game_Data/level1'])

    def test_a_file_that_is_nowhere_keeps_its_name_below_the_folder_that_was_found(self):
        self.put('Game_Data/level1')
        self.assertEqual(self.names(self.manifest('game_Data/level2', 'other/new')), ['Game_Data/level2', 'other/new'])

    def test_the_same_file_in_two_spellings_is_refused_unless_one_is_as_listed(self):
        self.put('Game_Data/level1')
        self.put('GAME_DATA/level1')
        with self.assertRaisesRegex(depot.DepotError, 'more than one spelling'):
            self.names(self.manifest('game_Data/level1'))
        self.put('game_Data/level1')
        self.assertEqual(self.names(self.manifest('game_Data/level1')), ['game_Data/level1'])

    def test_a_link_to_a_folder_is_not_looked_into(self):
        self.put('real/level1')
        os.symlink(self.root / 'real', self.root / 'Game_Data')
        self.assertEqual(self.names(self.manifest('game_data/level1')), ['Game_Data/level1'])
        self.assertEqual(self.names(self.manifest('game_data/LEVEL1')), ['Game_Data/LEVEL1'])

    def test_a_tree_in_another_case_verifies_as_pristine_once_named_as_installed(self):
        self.put('Game_Data/level1', b'one')
        listed = depot.parse_manifest(build([file_entry('game_Data/level1', b'one')], depot_id=1, gid=2))
        self.assertEqual(depot.verify_tree([listed], self.root).pristine, [])
        verdict = depot.verify_tree(depot.as_installed([listed], self.root), self.root)
        self.assertEqual((verdict.pristine, verdict.extra, verdict.missing), (['Game_Data/level1'], [], []))


class VdfTest(unittest.TestCase):
    def test_an_app_manifest_round_trips_byte_for_byte(self):
        name, value = depot.vdf_loads(APP_MANIFEST)
        self.assertEqual(name, 'AppState')
        self.assertEqual(depot.vdf_dumps(name, value), APP_MANIFEST)

    def test_library_folders_round_trips_byte_for_byte(self):
        name, value = depot.vdf_loads(LIBRARY_FOLDERS)
        self.assertEqual(name, 'libraryfolders')
        self.assertEqual(value['0']['label'], '')
        self.assertEqual(depot.vdf_dumps(*depot.vdf_loads(LIBRARY_FOLDERS)), LIBRARY_FOLDERS)

    def test_parsed_values_are_strings_and_keep_their_order_and_case(self):
        _, value = depot.vdf_loads(APP_MANIFEST)
        self.assertEqual(list(value)[:4], ['appid', 'Universe', 'name', 'installdir'])
        self.assertEqual(value['appid'], '4242')
        self.assertIsInstance(value['InstalledDepots'], dict)

    def test_escapes_are_read_and_written_back_escaped(self):
        text = '"S"\n{\n\t"k"\t\t"a\\\\b \\"q\\" line\\nnext\\ttab"\n}\n'
        name, value = depot.vdf_loads(text)
        self.assertEqual(value['k'], 'a\\b "q" line\nnext\ttab')
        self.assertEqual(depot.vdf_dumps(name, value), text)

    def test_comments_and_unquoted_tokens_and_loose_whitespace_are_read(self):
        text = ('// header\n"S" // trailing\n  {\r\n bare   value  // note\n "quoted"\t"x"\n'
                ' sub { inner 1 }\n}\n// end\n')
        name, value = depot.vdf_loads(text)
        self.assertEqual(name, 'S')
        self.assertEqual(value, {'bare': 'value', 'quoted': 'x', 'sub': {'inner': '1'}})

    def test_a_quoted_brace_is_a_value_not_structure(self):
        _, value = depot.vdf_loads('"S" { "k" "{" "j" "}" }')
        self.assertEqual(value, {'k': '{', 'j': '}'})

    def test_an_empty_section_round_trips(self):
        text = '"S"\n{\n\t"empty"\n\t{\n\t}\n}\n'
        self.assertEqual(depot.vdf_dumps(*depot.vdf_loads(text)), text)

    def test_every_vdf_refusal_raises_a_depot_error(self):
        deep = '"S"{' + '"k"{' * 64 + '}' * 65
        fine = '"S"{' + '"k"{' * 62 + '}' * 63
        cases = {
            'empty': '',
            'only whitespace and comments': '  // nothing\n',
            'no opening brace': '"S"',
            'unclosed top level': '"S"\n{\n"k" "v"\n',
            'unclosed subsection': '"S" { "a" { "k" "v" }',
            'extra closing brace': '"S" { "k" "v" } }',
            'missing value at end of section': '"S" { "k" }',
            'missing value at end of input': '"S" { "k"',
            'text after the top-level section': '"S" { } "k"',
            'second top-level section': '"A" { } "B" { }',
            'duplicate key': '"S" { "k" "1" "k" "2" }',
            'duplicate key across kinds': '"S" { "k" "1" "k" { } }',
            'duplicate key with capitals': '"S" { "K" "1" "K" "2" }',
            'nesting too deep': deep,
            'unterminated string': '"S" { "k" "v }',
            'unknown escape': '"S" { "k" "a\\qb" }',
            'line break inside a string': '"S" { "k" "a\nb" }',
            'opening brace where a key belongs': '"S" { { } }',
            'section name is a brace': '{ } ',
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(DepotError):
                    depot.vdf_loads(text)
        with self.subTest('depth just inside the limit'):
            depot.vdf_loads(fine)

    def test_keys_that_differ_only_in_case_are_kept_apart(self):
        _, value = depot.vdf_loads('"S" { "Key" "1" "key" "2" }')
        self.assertEqual(value, {'Key': '1', 'key': '2'})

    def test_vdf_dumps_refuses_what_it_cannot_write(self):
        for label, value in (('number', {'k': 5}), ('carriage return', {'k': 'a\rb'}), ('list', {'k': ['x']})):
            with self.subTest(label):
                with self.assertRaises(DepotError):
                    depot.vdf_dumps('S', value)

    def test_read_vdf_reads_a_file_and_refuses_bad_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'appmanifest_4242.acf'
            path.write_text(APP_MANIFEST)
            self.assertEqual(depot.vdf_dumps(*depot.read_vdf(path)), APP_MANIFEST)
            link = Path(tmp) / 'link.acf'
            link.symlink_to(path)
            bad = Path(tmp) / 'bad.acf'
            bad.write_bytes(b'"S" { "k" "\xff" }')
            for label, target in (('symlink', link), ('directory', Path(tmp)), ('not UTF-8', bad)):
                with self.subTest(label):
                    with self.assertRaises(DepotError):
                        depot.read_vdf(target)
            with self.subTest('oversized'):
                with self.assertRaises(DepotError):
                    depot.read_vdf(path, max_bytes=10)

    def test_installed_depots_maps_depot_ids_to_manifest_gids(self):
        _, state = depot.vdf_loads(APP_MANIFEST)
        self.assertEqual(depot.installed_depots(state), {4243: 1111111111111111111, 4244: 2222222222222222222})

    def test_installed_depots_is_empty_when_absent(self):
        self.assertEqual(depot.installed_depots({'appid': '1'}), {})

    def test_installed_depots_refuses_what_it_does_not_understand(self):
        cases = {
            'not a section': {'InstalledDepots': 'x'},
            'non-numeric depot': {'InstalledDepots': {'abc': {'manifest': '1'}}},
            'depot is a leaf': {'InstalledDepots': {'1': '2'}},
            'no manifest key': {'InstalledDepots': {'1': {'size': '2'}}},
            'non-numeric gid': {'InstalledDepots': {'1': {'manifest': 'zz'}}},
            'negative gid': {'InstalledDepots': {'1': {'manifest': '-5'}}},
        }
        for label, state in cases.items():
            with self.subTest(label):
                with self.assertRaises(DepotError):
                    depot.installed_depots(state)

    def test_manifest_name_joins_depot_and_gid(self):
        self.assertEqual(depot.manifest_name(262831, 5318254292612917484), '262831_5318254292612917484.manifest')

    def test_manifest_name_refuses_what_is_not_a_number(self):
        for depot_id, gid in (('1', 2), (1, '2'), (-1, 2), (1, None), (True, 2)):
            with self.subTest((depot_id, gid)):
                with self.assertRaises(DepotError):
                    depot.manifest_name(depot_id, gid)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.root = self.dir / 'game'
        self.root.mkdir()
        (self.root / 'a.bin').write_bytes(b'aaa')
        (self.root / 'sub').mkdir()
        (self.root / 'sub/b.bin').write_bytes(b'bbbb')
        entries = [file_entry('a.bin', b'aaa', depot.EXECUTABLE), file_entry('sub\\b.bin', b'bbbb'),
                   entry('sub', 0, depot.DIRECTORY, chunks=0),
                   entry('ln', 0, depot.SYMLINK, link='a.bin', chunks=0)]
        self.manifest = self.dir / '77_88.manifest'
        self.manifest.write_bytes(build(entries, depot_id=77, gid=88))
        (self.root / 'ln').symlink_to('a.bin')

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-I', str(MODULE_PATH), *map(str, args)],
                              capture_output=True, text=True, timeout=60)

    def test_the_manifest_command_prints_the_header_and_one_line_per_entry(self):
        result = self.run_cli('manifest', self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:4], ['depot 77', 'gid 88', 'files 4', 'size 7'])
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[4].split(), ['32', '3', hashlib.sha1(b'aaa').hexdigest(), 'a.bin'])
        self.assertTrue(lines[5].endswith(' sub/b.bin'))
        self.assertIn(' sub', lines[6])
        self.assertTrue(lines[7].endswith(' ln -> a.bin'))

    def test_the_verify_command_exits_zero_for_a_pristine_tree(self):
        result = self.run_cli('verify', self.root, self.manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['pristine 3', 'changed 0', 'missing 0', 'extra 0'])

    def test_the_verify_command_lists_names_and_exits_one_when_not_ok(self):
        (self.root / 'a.bin').write_bytes(b'aab')
        (self.root / 'sub/b.bin').unlink()
        (self.root / 'extra.txt').write_bytes(b'x')
        result = self.run_cli('verify', self.root, self.manifest)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ['pristine 1', 'changed 1', '  a.bin', 'missing 1',
                                                      '  sub/b.bin', 'extra 1', '  extra.txt'])

    def test_the_verify_command_prints_at_most_twenty_names(self):
        for index in range(25):
            (self.root / f'x{index:02d}').write_bytes(b'x')
        result = self.run_cli('verify', self.root, self.manifest)
        listed = [line for line in result.stdout.splitlines() if line.startswith('  x')]
        self.assertEqual(len(listed), 20)
        self.assertIn('extra 25', result.stdout)

    def test_the_vdf_command_prints_json(self):
        path = self.dir / 'appmanifest_4242.acf'
        path.write_text(APP_MANIFEST)
        result = self.run_cli('vdf', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['AppState']['InstalledDepots']['4243']['manifest'], '1111111111111111111')

    def test_errors_print_a_prefixed_message_and_exit_two(self):
        bad = self.dir / 'bad.manifest'
        bad.write_bytes(b'junk')
        cases = (('manifest', bad), ('manifest', self.dir / 'absent'), ('vdf', bad),
                 ('verify', self.dir / 'absent', self.manifest), ('verify', self.root, bad))
        for args in cases:
            with self.subTest(args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.startswith('dosplex-depot: '), result.stderr)
                self.assertEqual(result.stdout, '')

    def test_a_missing_command_exits_two(self):
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == '__main__':
    unittest.main()
