#!/usr/bin/env python3
# dosplex-test: tier=integration timeout=300
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Signed releases, `dosplex update` and `dosplex rollback` (T-702), against a fake sysroot.

The same fake sysroot, fake commands and fake bundles as tests/dosplex-cli-test.py (this file loads that one for
them). What is new is the signing. The test makes a key, pins it in a copy of the program (the way a release
build pins its key) and builds releases with tools/release.py, signed through a fake `minisign`. Every command
here runs that pinned copy. Nothing about a real key is used or needed, and the signer is TEST ONLY.

What is proved: an archive or an index that was changed, signed with another key or not signed at all is refused
before the device is touched and before anything from it runs; an update installs the newer release, keeps the
one before it, restarts a running session and records every step; rollback goes back and forth; an interrupted
update or rollback is finished or undone by `repair` after a crash at every journal point; the version gate of
install holds for update.
"""
import base64
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
import importlib.machinery
import importlib.util

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli_test = load_module('dosplex_cli_test_helpers', ROOT / 'tests' / 'dosplex-cli-test.py')
signing = load_module('release_signing_test_helpers', ROOT / 'tests' / 'release-signing-test.py')
release = load_module('release_under_test', ROOT / 'tools' / 'release.py')
dosplex = cli_test.dosplex
OnlyForTestsKey = signing.OnlyForTestsKey
sha256 = cli_test.sha256

SBOM_FILES = {'sbom/dosplex-sbom.json': b'{"format": "dosplex-sbom", "package": {"name": "dosplex"}}\n',
              'THIRD-PARTY-NOTICES.md': b'# Third-party notices\n'}
# version -> (sequence, tested set or None for the default one, text appended to the program it ships)
RECIPES = {
    'one': ('0.2.0-a', 100, None, ''),
    'two': ('0.2.0-b', 200, None, ''),
    'three': ('0.2.0-c', 300, None, ''),
    'newprogram': ('0.2.0-n', 250, None, '\n# a newer program\n'),
    'untested': ('0.2.0-x', 400, 'untested', ''),
}


class Fixtures:
    """Built once per run: a key, the program pinned to it, a signed release per recipe, and channels."""

    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='dosplex-update-fixtures-')
        self.dir = Path(self.temporary.name)
        self.key = OnlyForTestsKey()
        self.other_key = OnlyForTestsKey()
        self.secret = self.dir / 'test-only.key'
        self.secret.write_text(self.key.secret_file_text())
        self.other_secret = self.dir / 'other-test-only.key'
        self.other_secret.write_text(self.other_key.secret_file_text())
        self.public = self.dir / 'test-only.pub'
        self.public.write_text(self.key.public_file())
        self.fakebin = self.dir / 'fakebin'
        self.fakebin.mkdir()
        signing.install_fake_minisign(self.fakebin)
        original = (ROOT / 'tools' / 'dosplex').read_text()
        self.programs = {}
        for name, suffix in (('base', ''), ('newprogram', RECIPES['newprogram'][3])):
            path = self.dir / 'programs' / name / 'dosplex'
            path.parent.mkdir(parents=True)
            path.write_text(release.pin_program(original, [self.key.pin()]) + suffix)
            path.chmod(0o755)
            self.programs[name] = path
        self.program = self.programs['base']
        self.lock = threading.Lock()
        self.templates = {}
        self.releases = {}
        for name, (version, sequence, tested, suffix) in RECIPES.items():
            bundle = cli_test.Bundle(self.dir / ('bundle-' + name), version=version, extra=SBOM_FILES,
                                     tested_versions=cli_test.tested(gamescope=['9.9.9']) if tested == 'untested' else None)
            self.releases[name] = self.build(name, bundle.path, sequence, self.programs['newprogram' if suffix else 'base'])

    def program_pinned_to(self, *keys):
        """A copy of the program that trusts `keys` and nothing else."""
        path = self.dir / 'programs' / ('pinned-to-' + '-'.join(key.key_id for key in keys)) / 'dosplex'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(release.pin_program((ROOT / 'tools' / 'dosplex').read_text(), [key.pin() for key in keys]))
        path.chmod(0o755)
        return path

    def release_py(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        path = str(self.fakebin) + os.pathsep + os.environ['PATH']
        with mock.patch.dict(os.environ, PATH=path, SOURCE_DATE_EPOCH='1791531806'):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release.main([str(a) for a in argv])
        assert code == 0, stdout.getvalue() + stderr.getvalue()
        return stdout.getvalue()

    def build(self, name, bundle, sequence, program, secret=None, out=None):
        out = out or self.dir / ('release-' + name)
        self.release_py('build', bundle, '--out', out, '--sequence', sequence, '--program', program,
                        '--sign-with', secret or self.secret)
        return Release(out)

    def channel(self, directory, names):
        """A channel directory (index, its signature and the assets) holding the releases `names`, as a copy of
        a template that is made once. (release.py runs in this process and redirects its output, so it must
        not run in two threads at once.)"""
        key = tuple(names)
        with self.lock:
            if key not in self.templates:
                template = self.dir / ('channel-%d' % len(self.templates))
                template.mkdir()
                entries = []
                for name in names:
                    for item in self.releases[name].dir.iterdir():
                        shutil.copy(item, template / item.name)
                    entries.append(self.releases[name].entry)
                scratch = self.dir / ('index-%d' % len(self.templates))
                self.release_py('index', *entries, '--out', scratch, '--channel', 'preview', '--program', self.program,
                                '--sign-with', self.secret)
                for item in scratch.iterdir():
                    shutil.move(str(item), str(template / item.name))
                self.templates[key] = template
        shutil.copytree(self.templates[key], directory)
        return Path(directory)

    def cleanup(self):
        self.temporary.cleanup()


class Release:
    """The files release.py wrote for one release."""

    def __init__(self, directory):
        self.dir = Path(directory)
        (self.archive,) = self.dir.glob('*.tar.gz')
        self.base = self.archive.name[:-len('.tar.gz')]
        self.signature = self.dir / (self.base + '.manifest.json.minisig')
        self.manifest = self.dir / (self.base + '.manifest.json')
        self.entry = self.dir / (self.base + '.index-entry.json')
        self.version = self.base[len('dosplex-'):]

    @property
    def manifest_sha256(self):
        return sha256(self.manifest.read_bytes())

    def program(self):
        return dosplex.read_archive_members(self.archive.read_bytes())[self.base + '/dosplex']


FIXTURES = None


def setUpModule():
    global FIXTURES
    FIXTURES = Fixtures()


def tearDownModule():
    FIXTURES.cleanup()


def rebuild_archive(archive, change):
    """A copy of a release archive after `change(members)`, which edits the name -> bytes dict in place."""
    members = dict(dosplex.read_archive_members(Path(archive).read_bytes()))
    change(members)
    return archive_of([(name, data, tarfile.REGTYPE, None) for name, data in members.items()])


def archive_of(entries):
    """A gzip-compressed tar file of (name, data, type, link target) entries, in the order given."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data, kind, target in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = 0o755 if name.endswith(('/dosplex', '.py')) else 0o644
            if kind == tarfile.SYMTYPE or kind == tarfile.LNKTYPE:
                info.linkname = target
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class ReleaseWorld(cli_test.World):
    """The fake sysroot, running the program that has the test key pinned."""

    program = None

    def run(self, *args, input=None, root=True, timeout=180, **extra):
        command = [sys.executable, str(self.program), '--sysroot', str(self.tree)] + [str(a) for a in args]
        env = self.environment(**extra)
        if not root:
            env.pop('DOSPLEX_TEST_ROOT_UID')
        proc = subprocess.run(command, input=input, capture_output=True, text=True, env=env, timeout=timeout)
        return cli_test.Result(proc)

    def install_release(self, release_, *args, **options):
        return self.run('install', '--release', release_.archive, '--sample-seconds', '0.3', *args, **options)


class Base(cli_test.Base):
    def world(self, program=None, **options):
        previous = os.umask(0o022)      # the fake tree is built the way root would build it
        self.addCleanup(os.umask, previous)
        temp = tempfile.TemporaryDirectory(prefix='dosplex-update-test-')
        world = ReleaseWorld(temp.name, **options)
        world.program = program or FIXTURES.program
        self.addCleanup(temp.cleanup)
        self.addCleanup(world.cleanup)
        return world

    def releases(self):
        return FIXTURES.releases


class InstallSignedRelease(Base):
    def test_a_signed_release_installs_like_a_bundle_and_records_who_signed_it(self):
        world = self.world()
        one = self.releases()['one']
        result = world.install_release(one, '--no-start')
        self.assertOk(result)
        self.assertIn('Verified the signature of release 0.2.0-a (key %s, sequence 100)' % FIXTURES.key.key_id, result.out)
        opt = world.tree / 'opt/dosplex'
        self.assertEqual(os.readlink(opt / 'current'), 'releases/0.2.0-a')
        release_dir = opt / 'releases/0.2.0-a'
        manifest = json.loads(one.manifest.read_text())
        for name, item in manifest['files'].items():
            self.assertEqual(sha256((release_dir / name).read_bytes()), item['sha256'], name)
            self.assertEqual(stat.S_IMODE((release_dir / name).stat().st_mode), item['mode'], name)
        self.assertEqual((release_dir / 'manifest.json').read_bytes(), one.manifest.read_bytes())
        self.assertEqual((opt / 'bin/dosplex').read_bytes(), one.program())
        state = world.state()
        self.assertEqual((state['version'], state['manifest_sha256'], state['previous'], state['previous_info']),
                         ('0.2.0-a', one.manifest_sha256, None, None))
        self.assertEqual(state['signed'], {'key_id': FIXTURES.key.key_id, 'sequence': 100})
        self.assertEqual(state['cli'], {'version': dosplex.CLI_VERSION, 'sha256': sha256(one.program())})
        begin = world.journal()['0001-install.jsonl'][0]
        self.assertEqual((begin['release'], begin['signed_by']), (str(one.archive), FIXTURES.key.key_id))
        self.assertNotIn('bundle', begin)

    def test_the_signature_is_looked_for_beside_the_archive_and_can_be_named(self):
        world = self.world()
        one = self.releases()['one']
        elsewhere = world.root / 'elsewhere'
        elsewhere.mkdir()
        shutil.copy(one.archive, elsewhere)
        result = world.run('install', '--release', elsewhere / one.archive.name, '--no-start')
        self.assertFailed(result, 'cannot read the signature file')
        self.assertIn(one.signature.name, result.err)
        self.assertNothingChanged(world)
        shutil.copy(one.signature, elsewhere / 'any-name.minisig')
        self.assertOk(world.run('install', '--release', elsewhere / one.archive.name, '--signature',
                                elsewhere / 'any-name.minisig', '--no-start'))

    def test_a_digest_that_is_also_given_has_to_be_the_one_in_the_signed_manifest(self):
        world = self.world()
        one = self.releases()['one']
        result = world.run('install', '--release', one.archive, '--manifest', '0' * 64, '--no-start')
        self.assertFailed(result, 'which is not the digest you gave')
        self.assertNothingChanged(world)
        self.assertOk(world.run('install', '--release', one.archive, '--manifest', one.manifest_sha256, '--no-start'))

    def test_usage_mistakes_are_exit_2_and_change_nothing(self):
        world = self.world()
        one = self.releases()['one']
        for arguments in (('install',), ('install', '--release', one.archive, 'somewhere'),
                          ('install', 'somewhere', '--manifest', 'a' * 64, '--signature', one.signature),
                          ('install', 'somewhere'), ('preflight', 'somewhere', '--release', one.archive)):
            self.assertFailed(world.run(*arguments), code=2)
        self.assertNothingChanged(world)

    def refused(self, world, archive, text, signature=None, program=None):
        command = ['install', '--release', archive, '--no-start']
        if signature is not None:
            command += ['--signature', signature]
        result = world.run(*command)
        self.assertFailed(result, text)
        self.assertNothingChanged(world)
        return result

    def tampered(self, name, change):
        """A copy of release `name` with its archive changed by `change(members)`; returns (archive, signature)."""
        one = self.releases()[name]
        directory = Path(tempfile.mkdtemp(prefix='tampered-', dir=FIXTURES.dir))
        self.addCleanup(shutil.rmtree, directory, True)
        archive = directory / one.archive.name
        archive.write_bytes(rebuild_archive(one.archive, change))
        shutil.copy(one.signature, directory / one.signature.name)
        return archive

    def test_a_changed_file_is_refused_before_anything_runs(self):
        world = self.world()
        name = self.releases()['one'].base + '/bundle/libexec/dosplex-audio-router.py'
        archive = self.tampered('one', lambda members: members.update({name: members[name] + b'#'}))
        self.refused(world, archive, 'libexec/dosplex-audio-router.py differs from the signed manifest')

    def test_a_changed_manifest_is_refused_whatever_else_matches(self):
        world = self.world()
        one = self.releases()['one']
        name = one.base + '/bundle/manifest.json'

        def widen(members):               # the tested set widened, the file hashes left right
            manifest = json.loads(members[name])
            manifest['tested_versions']['gamescope'].append('9.9.9')
            members[name] = (json.dumps(manifest, indent=2) + '\n').encode()
        self.refused(world, self.tampered('one', widen), 'signature of the manifest is not valid')

    def test_files_that_are_missing_or_extra_or_duplicated_or_not_plain_are_refused(self):
        world = self.world()
        one = self.releases()['one']
        base = one.base
        members = dosplex.read_archive_members(one.archive.read_bytes())
        entries = [(name, data, tarfile.REGTYPE, None) for name, data in members.items()]
        directory = Path(tempfile.mkdtemp(prefix='shapes-', dir=FIXTURES.dir))
        self.addCleanup(shutil.rmtree, directory, True)
        shutil.copy(one.signature, directory / one.signature.name)
        cases = {
            'a missing file': ([e for e in entries if not e[0].endswith('LICENSE')], 'LICENSE is missing from the archive'),
            'an extra file': (entries + [(base + '/bundle/extra.py', b'print(1)\n', tarfile.REGTYPE, None)],
                              '1 entry is not in the signed manifest: %s/bundle/extra.py' % base),
            'a duplicate name': (entries + [entries[3]], 'duplicate name'),
            'a symlink': (entries + [(base + '/bundle/link', b'', tarfile.SYMTYPE, '/etc/passwd')], 'not a plain file'),
            'a hard link': (entries + [(base + '/bundle/hard', b'', tarfile.LNKTYPE, base + '/dosplex')], 'not a plain file'),
            'a name that leaves the directory': (entries + [(base + '/../escape', b'x', tarfile.REGTYPE, None)],
                                                 'unacceptable name'),
            'an absolute name': (entries + [('/etc/cron.d/evil', b'x', tarfile.REGTYPE, None)], 'unacceptable name'),
            'a second top directory': (entries + [('other/file', b'x', tarfile.REGTYPE, None)], 'one directory'),
            'a device': (entries + [(base + '/bundle/dev', b'', tarfile.CHRTYPE, None)], 'not a plain file'),
        }
        for label, (changed, needle) in cases.items():
            with self.subTest(label):
                archive = directory / one.archive.name
                archive.write_bytes(archive_of(changed))
                self.refused(world, archive, needle)

    def test_a_program_that_is_not_the_one_the_manifest_names_is_refused(self):
        world = self.world()
        name = self.releases()['one'].base + '/dosplex'
        archive = self.tampered('one', lambda members: members.update({name: members[name] + b'\n# swapped\n'}))
        self.refused(world, archive, 'dosplex program in the archive differs from the signed manifest')

    def test_a_release_signed_with_another_key_is_refused_with_the_ids_of_both(self):
        world = self.world()
        bundle = cli_test.Bundle(FIXTURES.dir / 'bundle-other', version='0.2.0-o', extra=SBOM_FILES)
        other = FIXTURES.build('other', bundle.path, 150, FIXTURES.program_pinned_to(FIXTURES.other_key),
                               secret=FIXTURES.other_secret)
        result = self.refused(world, other.archive, 'does not trust')
        self.assertIn(FIXTURES.other_key.key_id, result.err)
        self.assertIn(FIXTURES.key.key_id, result.err)

    def test_a_signature_that_belongs_to_another_release_or_is_not_there_or_is_damaged_is_refused(self):
        world = self.world()
        one, two = self.releases()['one'], self.releases()['two']
        self.refused(world, one.archive, 'signature of the manifest is not valid', signature=two.signature)
        self.refused(world, one.archive, 'cannot read the signature file', signature=world.root / 'no-such.minisig')
        damaged = world.root / 'damaged.minisig'
        text = one.signature.read_text().splitlines()
        damaged.write_text('\n'.join(text[:3] + [base64.b64encode(os.urandom(64)).decode()]) + '\n')
        self.refused(world, one.archive, 'signature comment', signature=damaged)
        damaged.write_text('not a signature\n')
        self.refused(world, one.archive, 'not a minisign signature', signature=damaged)

    def test_something_that_is_not_an_archive_is_refused(self):
        world = self.world()
        one = self.releases()['one']
        for label, data in (('garbage', b'not an archive at all'), ('empty', b''), ('a truncated archive', one.archive.read_bytes()[:200]),
                            ('an archive of nothing', archive_of([]))):
            with self.subTest(label):
                path = world.root / 'broken.tar.gz'
                path.write_bytes(data)
                self.refused(world, path, 'archive', signature=one.signature)

    def test_a_decompression_bomb_is_stopped(self):
        world = self.world()
        one = self.releases()['one']
        bomb = archive_of([(one.base + '/bundle/huge%d' % number, bytes(40 * 1024 * 1024), tarfile.REGTYPE, None)
                           for number in range(4)])
        path = world.root / 'bomb.tar.gz'
        path.write_bytes(bomb)
        self.assertLess(len(bomb), 1 << 20)
        self.refused(world, path, 'more data than a release may', signature=one.signature)

    def test_a_signed_manifest_that_is_not_a_release_manifest_is_refused(self):
        world = self.world()
        bundle = cli_test.Bundle(FIXTURES.dir / 'bundle-plain', version='0.2.0-p', extra=SBOM_FILES)
        members = {'dosplex-0.2.0-p/bundle/' + name: (bundle.path / name).read_bytes()
                   for name in json.loads(bundle.manifest_bytes)['files']}
        members['dosplex-0.2.0-p/bundle/manifest.json'] = bundle.manifest_bytes
        members['dosplex-0.2.0-p/dosplex'] = FIXTURES.program.read_bytes()
        path = world.root / 'plain.tar.gz'
        path.write_bytes(archive_of([(name, data, tarfile.REGTYPE, None) for name, data in members.items()]))
        signature = world.root / 'plain.minisig'
        signature.write_text(FIXTURES.key.sign_text(bundle.manifest_bytes, 'dosplex manifest 0.2.0-p'))
        self.refused(world, path, 'not a release manifest', signature=signature)
        signature.write_text(FIXTURES.key.sign_text(bundle.manifest_bytes, 'dosplex manifest 9.9.9'))
        self.refused(world, path, 'signature is for release 9.9.9', signature=signature)

    def test_a_program_without_a_pinned_key_cannot_install_a_signed_release(self):
        world = self.world(program=FIXTURES.program_pinned_to())
        one = self.releases()['one']
        result = world.run('install', '--release', one.archive, '--no-start')
        self.assertFailed(result, 'pins no release signing key')
        self.assertIn('manifest digest', result.err)
        self.assertNothingChanged(world)

    def test_preflight_checks_a_release_and_changes_nothing(self):
        world = self.world()
        one = self.releases()['one']
        result = world.run('preflight', '--release', one.archive)
        self.assertOk(result)
        self.assertIn('Result: ready', result.out)
        self.assertIn('version 0.2.0-a', result.out)
        self.assertNothingChanged(world)
        archive = self.tampered('one', lambda members: members.update({one.base + '/dosplex': b'#'}))
        self.assertFailed(world.run('preflight', '--release', archive), 'differs from the signed manifest')
        self.assertNothingChanged(world)

    def test_a_directory_bundle_still_installs_by_its_digest_and_a_release_can_follow_it(self):
        world = self.world()
        bundle = world.bundle(version='0.1.0-dir', extra=SBOM_FILES)
        self.assertOk(world.install(bundle, '--no-start'))
        self.assertIsNone(world.state()['signed'])
        one = self.releases()['one']
        self.assertOk(world.install_release(one, '--no-start'))
        state = world.state()
        self.assertEqual((state['version'], state['previous']), ('0.2.0-a', '0.1.0-dir'))
        self.assertEqual(state['previous_info']['manifest_sha256'], bundle.sha256)
        self.assertIsNone(state['previous_info']['signed'])
        self.assertEqual(sorted(p.name for p in (world.tree / 'opt/dosplex/releases').iterdir()), ['0.1.0-dir', '0.2.0-a'])

    def test_a_release_brings_its_own_program_and_that_is_what_gets_installed(self):
        world = self.world()
        newprogram = self.releases()['newprogram']
        self.assertNotEqual(newprogram.program(), FIXTURES.program.read_bytes())
        self.assertOk(world.install_release(newprogram, '--no-start'))
        installed = world.tree / 'opt/dosplex/bin/dosplex'
        self.assertEqual(installed.read_bytes(), newprogram.program())
        self.assertEqual(world.state()['cli']['sha256'], sha256(newprogram.program()))
        # Installing the same release again, from the program it ships, changes nothing.
        before = world.snapshot()
        self.assertOk(world.install_release(newprogram, '--no-start'))
        self.assertEqual(world.snapshot(), before)

    def test_uninstall_leaves_the_device_as_it_was(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], '--no-start'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)


class Channeled(Base):
    """Tests that update from a channel directory on the fake device's disk (a file:// address)."""

    def channel(self, world, names):
        directory = FIXTURES.channel(world.root / 'channel', names)
        return directory, (directory / 'dosplex-index.json').as_uri()

    def installed(self, name='one', *args):
        world = self.world()
        self.assertOk(world.install_release(self.releases()[name], '--no-start', *args))
        return world

    def update(self, world, url, *args, **extra):
        return world.run('update', '--channel', url, '--sample-seconds', '0.3', *args, **extra)

    def state_of(self, world):
        state = world.state()
        return state['version'], state['previous'], sorted(p.name for p in (world.tree / 'opt/dosplex/releases').iterdir())

    def assertUnchanged(self, world, before):
        self.assertEqual(world.snapshot(exclude=()), before, 'the sysroot changed')
        self.assertEqual(world.events(), [], 'code from the release ran')


class Update(Channeled):
    def test_update_installs_the_newest_release_and_keeps_the_one_before_it(self):
        world = self.installed('one')
        directory, url = self.channel(world, ['one', 'two'])
        result = self.update(world, url, '--check')
        self.assertOk(result)
        self.assertIn('Update available: 0.2.0-b (sequence 200)', result.out)
        self.assertEqual(self.state_of(world)[0], '0.2.0-a')                      # --check changes nothing
        result = self.update(world, url)
        self.assertOk(result)
        for expected in ('signed by key %s' % FIXTURES.key.key_id, 'Verified the signature of release 0.2.0-b',
                         'Updated Dosplex 0.2.0-a -> 0.2.0-b.'):
            self.assertIn(expected, result.out)
        self.assertEqual(self.state_of(world), ('0.2.0-b', '0.2.0-a', ['0.2.0-a', '0.2.0-b']))
        self.assertEqual(os.readlink(world.tree / 'opt/dosplex/current'), 'releases/0.2.0-b')
        one, two = self.releases()['one'], self.releases()['two']
        state = world.state()
        self.assertEqual((state['manifest_sha256'], state['signed']), (two.manifest_sha256, {'key_id': FIXTURES.key.key_id, 'sequence': 200}))
        self.assertEqual(state['previous_info'], {'manifest_sha256': one.manifest_sha256, 'commit': cli_test.COMMIT,
                                                  'scope': 'test bundle', 'installed_utc': state['previous_info']['installed_utc'],
                                                  'signed': {'key_id': FIXTURES.key.key_id, 'sequence': 100}})
        for name, item in json.loads(two.manifest.read_text())['files'].items():
            self.assertEqual(sha256((world.tree / 'opt/dosplex/releases/0.2.0-b' / name).read_bytes()), item['sha256'], name)
        self.assertEqual(world.run('status').out.splitlines()[0], 'Dosplex 0.2.0-b (commit 012345678)')
        self.assertIn('signature: signed by key %s, sequence 200' % FIXTURES.key.key_id, world.run('status').out)
        again = self.update(world, url)
        self.assertOk(again)
        self.assertIn('Dosplex is up to date.', again.out)
        self.assertEqual(self.state_of(world)[0], '0.2.0-b')

    def test_the_journal_has_the_steps_of_an_update_with_the_commit_after_staging(self):
        world = self.installed('one')
        directory, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        records = world.journal()['0002-update.jsonl']
        kinds = [(r['kind'], r.get('step')) for r in records]
        self.assertEqual(kinds[0], ('begin', None))
        self.assertEqual(kinds[-1], ('end', None))
        self.assertEqual(records[-1]['outcome'], 'complete')
        self.assertEqual([step for kind, step in kinds if kind == 'intent'], ['stage', 'release', 'current', 'state', 'prune'])
        self.assertEqual(kinds.index(('commit', None)), kinds.index(('done', 'stage')) + 1)
        begin = records[0]
        self.assertEqual((begin['from_version'], begin['version'], begin['sequence'], begin['signed_by'], begin['cli_changes']),
                         ('0.2.0-a', '0.2.0-b', 200, FIXTURES.key.key_id, False))
        self.assertEqual(begin['channel'], url)

    def test_the_third_release_pushes_the_first_out_and_two_are_kept(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        shutil.rmtree(world.root / 'channel')
        _, url = self.channel(world, ['one', 'two', 'three'])
        self.assertOk(self.update(world, url))
        self.assertEqual(self.state_of(world), ('0.2.0-c', '0.2.0-b', ['0.2.0-b', '0.2.0-c']))
        self.assertEqual(world.state()['previous_info']['signed']['sequence'], 200)

    def test_the_newest_release_wins_when_several_are_newer(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two', 'three'])
        self.assertOk(self.update(world, url))
        self.assertEqual(self.state_of(world), ('0.2.0-c', '0.2.0-a', ['0.2.0-a', '0.2.0-c']))

    def test_the_newest_release_this_device_is_tested_for_is_chosen(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two', 'untested'])
        result = self.update(world, url)
        self.assertOk(result)
        self.assertEqual(self.state_of(world)[0], '0.2.0-b')
        self.assertIn('Skipped 0.2.0-x (sequence 400)', result.out)
        self.assertIn('gamescope', result.out)

    def test_a_release_this_device_is_not_tested_for_is_refused_unless_allowed(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'untested'])
        before = world.snapshot(exclude=())
        result = self.update(world, url)
        self.assertFailed(result, 'is not tested on this device', code=3)
        self.assertIn('0.2.0-x', result.err)
        self.assertIn('Nothing was changed', result.err)
        self.assertIn('--allow-untested', result.err)
        self.assertUnchanged(world, before)
        result = self.update(world, url, '--allow-untested')
        self.assertOk(result)
        self.assertEqual(self.state_of(world)[0], '0.2.0-x')
        state = world.state()
        self.assertEqual(state['untested'], ['gamescope'])
        self.assertEqual(state['untested_observed']['gamescope'], cli_test.GAMESCOPE)
        self.assertIn('untested configuration', world.run('status').out)

    def test_a_release_that_is_not_newer_is_never_installed(self):
        world = self.installed('two')
        _, url = self.channel(world, ['one', 'two'])
        before = world.snapshot(exclude=())
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('Dosplex is up to date.', result.out)
        self.assertUnchanged(world, before)

    def test_a_device_installed_from_a_bundle_takes_the_newest_signed_release(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(version='0.1.0-dir', extra=SBOM_FILES), '--no-start'))
        _, url = self.channel(world, ['one'])
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('not from a signed release', result.out)
        self.assertEqual(self.state_of(world), ('0.2.0-a', '0.1.0-dir', ['0.1.0-dir', '0.2.0-a']))

    def test_the_program_is_replaced_when_the_release_ships_another_and_the_old_one_is_kept(self):
        world = self.installed('one')
        base = FIXTURES.program.read_bytes()
        _, url = self.channel(world, ['one', 'newprogram'])
        self.assertOk(self.update(world, url))
        newprogram = self.releases()['newprogram']
        bin_dir = world.tree / 'opt/dosplex/bin'
        self.assertEqual((bin_dir / 'dosplex').read_bytes(), newprogram.program())
        self.assertEqual((bin_dir / 'dosplex.previous').read_bytes(), base)
        self.assertEqual(sorted(p.name for p in bin_dir.iterdir()), ['dosplex', 'dosplex.previous'])
        for name in ('dosplex', 'dosplex.previous'):
            self.assertEqual(stat.S_IMODE((bin_dir / name).stat().st_mode), 0o755)
        self.assertEqual(world.state()['cli']['sha256'], sha256(newprogram.program()))
        steps = [r['step'] for r in world.journal()['0002-update.jsonl'] if r['kind'] == 'intent']
        self.assertEqual(steps, ['stage', 'release', 'current', 'cli', 'state', 'prune'])
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)

    def test_an_update_that_keeps_the_program_leaves_the_kept_one_alone(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'newprogram', 'three'])
        self.assertOk(self.update(world, url))                      # newest is three: same program, nothing replaced
        self.assertEqual(sorted(p.name for p in (world.tree / 'opt/dosplex/bin').iterdir()), ['dosplex'])

    def test_a_running_session_is_restarted_on_the_new_release(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *world.controllers()))
        (first,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        _, url = self.channel(world, ['one', 'two'])
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('Restarting Dosplex on 0.2.0-b.', result.out)
        (second,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        self.assertNotEqual(first, second)
        self.assertEqual(world.state()['trial']['root'], str(second))
        starts = world.events('trial.start')
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[-1]['bundle'].endswith('/releases/0.2.0-b'), starts[-1])
        self.assertEqual(starts[-1]['manifest'], self.releases()['two'].manifest_sha256)
        self.assertIn('Running: yes', world.run('status').out)
        self.assertEqual(sorted(world.journal()), ['0001-install.jsonl', '0002-start.jsonl', '0003-update.jsonl',
                                                   '0004-stop.jsonl', '0005-start.jsonl'])
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)

    def test_with_no_restart_a_running_session_is_left_on_the_old_release(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *world.controllers()))
        (first,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        _, url = self.channel(world, ['one', 'two'])
        result = self.update(world, url, '--no-restart')
        self.assertOk(result)
        self.assertIn('keeps running the release it started with', result.out)
        self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [first])
        self.assertEqual(len(world.events('trial.start')), 1)
        self.assertEqual(self.state_of(world)[0], '0.2.0-b')

    def test_a_session_that_was_stopped_stays_stopped(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *world.controllers()))
        self.assertOk(world.run('stop'))
        _, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        self.assertEqual(len(world.events('trial.start')), 1)
        self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])

    def test_a_failed_restart_is_reported_and_the_update_stays(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *world.controllers()))
        _, url = self.channel(world, ['one', 'two'])
        result = self.update(world, url, FAKE_TRIAL_FAIL='before-root')
        self.assertFailed(result, 'Dosplex could not be restarted')
        self.assertIn('sudo', result.err)
        self.assertEqual(self.state_of(world)[0], '0.2.0-b')
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_staging_needs_neither_a_display_nor_controllers(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two'])
        (world.tree / 'sys/class/drm/card0-HDMI-A-1/status').write_text('disconnected\n')
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('[warn', result.out)
        self.assertEqual(self.state_of(world)[0], '0.2.0-b')

    def test_it_needs_root_and_an_installed_release_and_a_key(self):
        world = self.world()
        _, url = self.channel(world, ['one'])
        self.assertFailed(self.update(world, url, root=False), 'needs root')
        self.assertFailed(self.update(world, url), 'Dosplex is not installed')
        self.assertOk(world.install_release(self.releases()['one'], '--no-start'))
        unpinned = self.world(program=FIXTURES.program_pinned_to())
        self.assertOk(unpinned.install(unpinned.bundle(), '--no-start'))
        result = unpinned.run('update', '--channel', (world.root / 'nothing-here/dosplex-index.json').as_uri())
        self.assertFailed(result, 'pins no release signing key')
        self.assertNotIn('cannot fetch', result.err)                    # it did not even try the network

    def test_update_to_a_release_without_the_game_store_tool_is_refused_while_the_library_is_on(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two'])
        state = world.tree / 'var/lib/dosplex/state.json'
        state.write_text(json.dumps(dict(world.state(), game_store='/home/dosplex-library')))
        before = world.snapshot(exclude=())
        result = self.update(world, url)
        self.assertFailed(result, 'the shared game library is on', code=3)
        self.assertNotIn('Downloading', result.out)
        self.assertIn('bin/dosplex-game-store.py', result.err)
        self.assertIn('library off', result.err)
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertEqual(world.state()['version'], '0.2.0-a')
        state.write_text(json.dumps({k: v for k, v in world.state().items() if k != 'game_store'}))
        self.assertOk(self.update(world, url))


class UpdateRefusals(Channeled):
    """Whatever is wrong with the channel, the device is left as it was and nothing from it has run."""

    def setUp(self):
        super().setUp()
        self.world_ = self.installed('one')
        self.directory, self.url = self.channel(self.world_, ['one', 'two'])
        self.before = self.world_.snapshot(exclude=())

    def assertRefuses(self, text, *args, code=None):
        result = self.update(self.world_, self.url, *args)
        self.assertFailed(result, text, code=code)
        self.assertUnchanged(self.world_, self.before)
        self.assertEqual(self.world_.state()['version'], '0.2.0-a')
        return result

    def resign(self, index_bytes, key_secret=None, comment='dosplex index preview'):
        (self.directory / 'dosplex-index.json').write_bytes(index_bytes)
        key = FIXTURES.key if key_secret is None else key_secret
        (self.directory / 'dosplex-index.json.minisig').write_text(key.sign_text(index_bytes, comment))

    def test_an_index_that_was_changed_after_it_was_signed(self):
        index = self.directory / 'dosplex-index.json'
        index.write_bytes(index.read_bytes().replace(b'0.2.0-b', b'0.2.0-z'))
        self.assertRefuses('signature of the index is not valid')

    def test_an_index_signed_by_another_key(self):
        self.resign((self.directory / 'dosplex-index.json').read_bytes(), key_secret=FIXTURES.other_key)
        result = self.assertRefuses('does not trust')
        self.assertIn(FIXTURES.other_key.key_id, result.err)

    def test_an_index_without_a_signature_or_with_a_damaged_one(self):
        (self.directory / 'dosplex-index.json.minisig').write_text('nothing\n')
        self.assertRefuses('not a minisign signature')
        (self.directory / 'dosplex-index.json.minisig').unlink()
        self.assertRefuses('cannot fetch the signature of the index')

    def test_a_signature_made_for_a_manifest_is_not_an_index_signature(self):
        index = (self.directory / 'dosplex-index.json').read_bytes()
        self.resign(index, comment='dosplex manifest 0.2.0-b')
        self.assertRefuses('does not say that it is for a dosplex index')

    def test_an_index_signed_for_another_channel(self):
        self.resign((self.directory / 'dosplex-index.json').read_bytes(), comment='dosplex index stable')
        self.assertRefuses('signed for channel stable but says it is channel preview')

    def test_an_index_that_is_not_an_index(self):
        for label, content in (('not json', b'{'), ('a list', b'[]'), ('wrong format', b'{"format": "other"}'),
                               ('a bad release', b'{"format": "dosplex-index", "format_version": 1, "channel": "preview", '
                                                 b'"releases": [{"version": "../x"}]}')):
            with self.subTest(label):
                self.resign(content)
                self.assertRefuses('index')

    def test_a_manifest_that_is_not_the_one_the_signed_index_names(self):
        manifest = self.directory / 'dosplex-0.2.0-b.manifest.json'
        manifest.write_bytes(self.releases()['one'].manifest.read_bytes())
        self.assertRefuses('manifest of 0.2.0-b is not the one the signed index names')

    def test_a_signature_of_another_release(self):
        signature = self.directory / 'dosplex-0.2.0-b.manifest.json.minisig'
        signature.write_text(self.releases()['one'].signature.read_text())
        self.assertRefuses('signature of the manifest is not valid')

    def test_an_archive_that_is_not_the_one_the_signed_index_names(self):
        archive = self.directory / 'dosplex-0.2.0-b.tar.gz'
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0x01
        archive.write_bytes(bytes(data))
        self.assertRefuses('archive of 0.2.0-b is not the one the signed index names')
        archive.write_bytes(self.releases()['one'].archive.read_bytes())            # another release, and larger
        result = self.update(self.world_, self.url)              # another release's archive: too large, or not the named one
        self.assertFailed(result)
        self.assertTrue('is larger than' in result.err or 'is not the one the signed index names' in result.err, result.err)
        self.assertUnchanged(self.world_, self.before)

    def test_a_missing_asset(self):
        (self.directory / 'dosplex-0.2.0-b.tar.gz').unlink()
        self.assertRefuses('cannot fetch the release archive')

    def test_an_archive_that_does_not_match_its_manifest_even_though_the_index_agrees(self):
        # Someone with the signing key (an operator's mistake) lists the hash of a tampered archive.
        name = 'dosplex-0.2.0-b/bundle/LICENSE'
        archive = self.directory / 'dosplex-0.2.0-b.tar.gz'
        changed = rebuild_archive(archive, lambda members: members.update({name: b'changed\n'}))
        archive.write_bytes(changed)
        index = json.loads((self.directory / 'dosplex-index.json').read_text())
        for entry in index['releases']:
            if entry['version'] == '0.2.0-b':
                entry['tarball'].update(sha256=sha256(changed), bytes=len(changed))
        self.resign((json.dumps(index, indent=2) + '\n').encode())
        self.assertRefuses('LICENSE differs from the signed manifest')

    def test_an_index_and_a_manifest_that_disagree(self):
        index = json.loads((self.directory / 'dosplex-index.json').read_text())
        for entry in index['releases']:
            if entry['version'] == '0.2.0-b':
                entry['sequence'] = 201
        self.resign((json.dumps(index, indent=2) + '\n').encode())
        self.assertRefuses('disagree')

    def test_a_release_that_is_already_on_the_device_with_other_contents(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(version='0.2.0-b', extra=SBOM_FILES), '--no-start'))
        directory, url = self.channel(world, ['two'])
        before = world.snapshot(exclude=())
        result = self.update(world, url)
        self.assertFailed(result, 'already on this device with other contents')
        self.assertUnchanged(world, before)

    def test_addresses_that_are_not_https_or_file(self):
        for address in ('http://example.invalid/dosplex-index.json', 'ftp://example.invalid/x', 'relative/path.json',
                        'example.invalid/index.json'):
            with self.subTest(address):
                result = self.world_.run('update', '--channel', address)
                self.assertFailed(result, 'must be an https:// or file:// address', code=2)
                self.assertUnchanged(self.world_, self.before)

    def test_assets_must_stay_on_the_kind_of_address_the_index_came_from(self):
        index = json.loads((self.directory / 'dosplex-index.json').read_text())
        for entry in index['releases']:
            entry['tarball']['url'] = 'https://example.invalid/' + entry['tarball']['url']
        self.resign((json.dumps(index, indent=2) + '\n').encode())
        self.assertRefuses('not on the same kind of address')


class Rollback(Channeled):
    def updated(self, *names, controllers=False):
        """A device that has release one and was updated from it to the newest of `names`."""
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *(world.controllers() if controllers else ('--no-start',))))
        _, url = self.channel(world, ['one', *names])
        self.assertOk(self.update(world, url))
        return world, url

    def rollback(self, world, *args, **extra):
        return world.run('rollback', '--sample-seconds', '0.3', *args, **extra)

    def test_rollback_goes_back_and_a_second_one_goes_forward_again(self):
        world, _ = self.updated('two')
        one, two = self.releases()['one'], self.releases()['two']
        program = (world.tree / 'opt/dosplex/bin/dosplex').read_bytes()
        result = self.rollback(world)
        self.assertOk(result)
        self.assertIn('Release 0.2.0-a is intact: ', result.out)
        self.assertIn('Rolled back Dosplex 0.2.0-b -> 0.2.0-a.', result.out)
        self.assertEqual(self.state_of(world), ('0.2.0-a', '0.2.0-b', ['0.2.0-a', '0.2.0-b']))
        self.assertEqual(os.readlink(world.tree / 'opt/dosplex/current'), 'releases/0.2.0-a')
        state = world.state()
        self.assertEqual((state['manifest_sha256'], state['signed']['sequence'], state['previous_info']['manifest_sha256'],
                          state['previous_info']['signed']['sequence']), (one.manifest_sha256, 100, two.manifest_sha256, 200))
        self.assertEqual(world.run('status').out.splitlines()[0], 'Dosplex 0.2.0-a (commit 012345678)')
        self.assertEqual((world.tree / 'opt/dosplex/bin/dosplex').read_bytes(), program)
        kinds = [(r['kind'], r.get('step')) for r in world.journal()['0003-rollback.jsonl']]
        self.assertEqual([step for kind, step in kinds if kind == 'intent'], ['verify-previous', 'current', 'state'])
        self.assertEqual(kinds.index(('commit', None)), kinds.index(('done', 'verify-previous')) + 1)
        self.assertEqual(kinds[-1], ('end', None))
        again = self.rollback(world)
        self.assertOk(again)
        self.assertEqual(self.state_of(world), ('0.2.0-b', '0.2.0-a', ['0.2.0-a', '0.2.0-b']))
        self.assertEqual(world.state()['signed']['sequence'], 200)
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)

    def test_a_running_session_is_restarted_on_the_release_it_goes_back_to(self):
        world, _ = self.updated('two', controllers=True)
        self.assertEqual(len(world.events('trial.start')), 2)
        result = self.rollback(world)
        self.assertOk(result)
        self.assertIn('Restarting Dosplex on 0.2.0-a.', result.out)
        starts = world.events('trial.start')
        self.assertEqual(len(starts), 3)
        self.assertTrue(starts[-1]['bundle'].endswith('/releases/0.2.0-a'))
        self.assertEqual(starts[-1]['manifest'], self.releases()['one'].manifest_sha256)
        self.assertIn('Running: yes', world.run('status').out)
        result = self.rollback(world, '--no-restart')
        self.assertOk(result)
        self.assertEqual(len(world.events('trial.start')), 3)
        self.assertIn('keeps running the release it started with', result.out)

    def test_an_update_after_a_rollback_uses_the_release_that_is_still_there(self):
        world, url = self.updated('two')
        self.assertOk(self.rollback(world))
        self.assertOk(self.update(world, url))
        self.assertEqual(self.state_of(world), ('0.2.0-b', '0.2.0-a', ['0.2.0-a', '0.2.0-b']))
        records = world.journal()['0004-update.jsonl']
        (done,) = [r for r in records if r['kind'] == 'done' and r['step'] == 'release']
        self.assertEqual(done['data'], {'created': False})

    def test_the_program_is_left_as_it_is(self):
        world, _ = self.updated('newprogram')
        bin_dir = world.tree / 'opt/dosplex/bin'
        before = {name: (bin_dir / name).read_bytes() for name in ('dosplex', 'dosplex.previous')}
        self.assertOk(self.rollback(world))
        self.assertEqual({name: (bin_dir / name).read_bytes() for name in before}, before)

    def test_there_has_to_be_an_earlier_release_with_a_complete_record(self):
        world = self.installed('one')
        before = world.snapshot(exclude=())
        result = world.run('rollback')
        self.assertFailed(result, 'no earlier release to go back to')
        self.assertUnchanged(world, before)
        self.assertFailed(self.world().run('rollback'), 'nothing to roll back')
        world, _ = self.updated('two')
        state = json.loads((world.tree / 'var/lib/dosplex/state.json').read_text())
        del state['previous_info']
        (world.tree / 'var/lib/dosplex/state.json').write_text(json.dumps(state))
        before = world.snapshot(exclude=())
        result = world.run('rollback')
        self.assertFailed(result, 'record of release 0.2.0-a is incomplete')
        self.assertUnchanged(world, before)

    def test_a_kept_release_that_was_changed_is_refused(self):
        world, _ = self.updated('two')
        kept = world.tree / 'opt/dosplex/releases/0.2.0-a'
        victim = kept / 'LICENSE'
        victim.chmod(0o644)
        original = victim.read_bytes()
        victim.write_bytes(original + b'changed')
        before = world.snapshot(exclude=())
        self.assertFailed(world.run('rollback'), 'LICENSE differs from the approved manifest')
        self.assertUnchanged(world, before)
        victim.write_bytes(original)
        (kept / 'extra').write_text('x')
        before = world.snapshot(exclude=())
        self.assertFailed(world.run('rollback'), '1 entry is not in the manifest')
        self.assertUnchanged(world, before)
        (kept / 'extra').unlink()
        self.assertOk(world.run('rollback'))

    def test_the_release_it_goes_back_to_has_to_be_tested_for_this_device_too(self):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['untested'], '--no-start', '--allow-untested'))
        self.assertEqual(self.state_of(world)[0], '0.2.0-x')
        # Its sequence, 400, is the newest, so there is nothing to update to: install two over it as a release.
        self.assertOk(world.install_release(self.releases()['two'], '--no-start'))
        self.assertEqual(self.state_of(world)[:2], ('0.2.0-b', '0.2.0-x'))
        before = world.snapshot(exclude=())
        result = world.run('rollback')
        self.assertFailed(result, 'not in the tested set', code=3)
        self.assertIn('Nothing was changed', result.err)
        self.assertUnchanged(world, before)
        result = world.run('rollback', '--allow-untested')
        self.assertOk(result)
        state = world.state()
        self.assertEqual((state['version'], state['untested']), ('0.2.0-x', ['gamescope']))

    def test_a_kept_release_that_this_program_could_not_start_is_not_rolled_back_to(self):
        world, _ = self.updated('two')
        pinned = load_module('dosplex_pinned_requirements', FIXTURES.program)
        out, err = io.StringIO(), io.StringIO()
        before = world.snapshot(exclude=())
        with mock.patch.dict(os.environ, world.environment(), clear=True), \
                mock.patch.object(sys, 'argv', [str(FIXTURES.program), 'rollback']), \
                mock.patch.object(pinned, 'REQUIRED_RELEASE_FILES', pinned.REQUIRED_RELEASE_FILES + ('bin/a-newer-need',)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pinned.main(['--sysroot', str(world.tree), 'rollback'])
        self.assertEqual(code, 1)
        self.assertIn('release 0.2.0-a lacks files that start needs: bin/a-newer-need', err.getvalue())
        self.assertUnchanged(world, before)

    def test_rollback_to_a_release_without_the_game_store_tool_is_refused_while_the_library_is_on(self):
        world, _ = self.updated('two')
        state = world.tree / 'var/lib/dosplex/state.json'
        state.write_text(json.dumps(dict(world.state(), game_store='/home/dosplex-library')))
        before = world.snapshot(exclude=())
        result = self.rollback(world)
        self.assertFailed(result, 'the shared game library is on', code=3)
        self.assertIn('library off', result.err)
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertEqual(world.state()['version'], '0.2.0-b')

    def test_it_needs_root_and_a_clean_journal(self):
        world, _ = self.updated('two')
        self.assertFailed(world.run('rollback', root=False), 'needs root')
        crashed = world.run('rollback', DOSPLEX_TEST_CRASH_AT='rollback:begin')
        self.assertEqual(crashed.code, dosplex.FAULT_EXIT)
        result = world.run('rollback')
        self.assertFailed(result, 'dosplex repair', code=4)


class LibraryUnits(Channeled):
    """Releases that carry the shared game library tool: what an update and a rollback do with the library units."""
    PLACE = '/home/dosplex-library'
    AUTO_TIMER = 'etc/systemd/system/dosplex-game-library-auto.timer'
    AUTO_SERVICE = 'etc/systemd/system/dosplex-game-library-auto.service'
    AUTO_LINK = 'etc/systemd/system/timers.target.wants/dosplex-game-library-auto.timer'
    BOOT_UNIT = 'etc/systemd/system/dosplex-game-library.service'

    def library_releases(self):
        with FIXTURES.lock:
            if 'library-one' not in FIXTURES.releases:
                for name, version, sequence in (('library-one', '0.5.0-a', 5000), ('library-two', '0.5.0-b', 6000)):
                    bundle = cli_test.Bundle(FIXTURES.dir / ('bundle-' + name), version=version,
                                             extra=dict(SBOM_FILES, **{'bin/dosplex-game-store.py': cli_test.FAKE_STORE.encode()}))
                    FIXTURES.releases[name] = FIXTURES.build(name, bundle.path, sequence, FIXTURES.program)
        return FIXTURES.releases

    def old_library(self, **state):
        """A device with release one and the library turned on by an earlier release: the state entry, no units."""
        self.library_releases()
        world = self.installed('library-one')
        path = world.tree / 'var/lib/dosplex/state.json'
        path.write_text(json.dumps(dict(world.state(), game_store=self.PLACE, **state)))
        _, url = self.channel(world, ['library-one', 'library-two'])
        return world, url

    def systemctl(self, world):
        return [c['argv'] for c in world.commands('systemctl')]

    def test_an_update_gives_a_library_from_an_earlier_release_its_units_and_starts_the_timer(self):
        world, url = self.old_library()
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('Automatic sharing of the shared game library is on', result.out)
        for path in (self.BOOT_UNIT, self.AUTO_SERVICE, self.AUTO_TIMER):
            self.assertTrue((world.tree / path).is_file(), path)
        self.assertEqual(os.readlink(world.tree / self.AUTO_LINK), '../dosplex-game-library-auto.timer')
        self.assertIn(['start', 'dosplex-game-library-auto.timer'], self.systemctl(world))
        state = world.state()
        self.assertEqual((state['version'], state['game_store']), ('0.5.0-b', self.PLACE))
        self.assertNotIn('game_store_auto', state)
        (name,) = [name for name in world.journal() if name.endswith('-library.jsonl')]
        self.assertEqual(world.journal()[name][0]['action'], 'units')

    def test_an_update_leaves_a_library_with_automatic_sharing_off_alone_and_keeps_the_choice(self):
        world, url = self.old_library(game_store_auto=False)
        self.assertOk(self.update(world, url))
        for path in (self.AUTO_SERVICE, self.AUTO_TIMER, self.AUTO_LINK):
            self.assertFalse(os.path.lexists(world.tree / path), path)
        self.assertIs(world.state()['game_store_auto'], False)
        self.assertNotIn(['start', 'dosplex-game-library-auto.timer'], self.systemctl(world))
        result = world.run('rollback', '--sample-seconds', '0.3')
        self.assertOk(result)
        state = world.state()
        self.assertEqual((state['version'], state['game_store'], state['game_store_auto']), ('0.5.0-a', self.PLACE, False))

    def test_a_library_that_has_its_units_is_not_touched_again_by_a_rollback(self):
        world, url = self.old_library()
        self.assertOk(self.update(world, url))
        self.assertOk(world.run('rollback', '--sample-seconds', '0.3'))
        self.assertEqual(world.state()['version'], '0.5.0-a')
        self.assertIn('game_store', world.state())
        timer = world.tree / self.AUTO_TIMER
        self.assertTrue(timer.is_file())
        starts = self.systemctl(world).count(['start', 'dosplex-game-library-auto.timer'])
        self.assertEqual(starts, 1)

    def earlier_boot_unit(self, world):
        """The boot unit and the automatic units an earlier release wrote: no stop step, no disk units."""
        (world.tree / 'etc/systemd/system/multi-user.target.wants').mkdir(parents=True, exist_ok=True)
        (world.tree / self.BOOT_UNIT).write_text(cli_test.SharedGameLibrary.EARLIER_UNIT)
        os.symlink('../dosplex-game-library.service', world.tree / 'etc/systemd/system/multi-user.target.wants'
                   / 'dosplex-game-library.service')

    def test_an_update_replaces_the_boot_unit_of_an_earlier_release_and_adds_the_disk_units(self):
        world, url = self.old_library()
        self.earlier_boot_unit(world)
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('The boot unit and the disk units of the shared game library are up to date', result.out)
        self.assertEqual((world.tree / self.BOOT_UNIT).read_text(), cli_test.SharedGameLibrary.EXPECTED_UNIT)
        self.assertEqual((world.tree / 'etc/systemd/system/dosplex-game-library-disk@.service').read_text(),
                         cli_test.SharedGameLibrary.EXPECTED_DISK_SERVICE)
        self.assertEqual((world.tree / 'etc/udev/rules.d/90-dosplex-game-library.rules').read_text(),
                         cli_test.SharedGameLibrary.EXPECTED_DISK_RULE)
        self.assertIn(['daemon-reload'], self.systemctl(world))

    def test_an_update_brings_the_units_up_to_date_even_with_automatic_sharing_off(self):
        world, url = self.old_library(game_store_auto=False)
        self.earlier_boot_unit(world)
        self.assertOk(self.update(world, url))
        self.assertEqual((world.tree / self.BOOT_UNIT).read_text(), cli_test.SharedGameLibrary.EXPECTED_UNIT)
        self.assertTrue((world.tree / 'etc/udev/rules.d/90-dosplex-game-library.rules').is_file())
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_TIMER))

    def test_an_update_that_is_interrupted_while_the_units_are_written_is_finished_by_repair(self):
        world, url = self.old_library()
        self.earlier_boot_unit(world)
        crashed = self.update(world, url, DOSPLEX_TEST_CRASH_AT='library:applied:unit')
        self.assertEqual(crashed.code, dosplex.FAULT_EXIT, crashed.text)
        self.assertOk(world.run('repair'))
        self.assertEqual((world.tree / self.BOOT_UNIT).read_text(), cli_test.SharedGameLibrary.EXPECTED_UNIT)
        self.assertTrue((world.tree / 'etc/udev/rules.d/90-dosplex-game-library.rules').is_file())

    def test_a_boot_unit_that_is_neither_this_releases_nor_an_earlier_ones_is_left_alone(self):
        world, url = self.old_library()
        (world.tree / self.BOOT_UNIT).write_text('[Unit]\nDescription=mine\n')
        result = self.update(world, url)
        self.assertOk(result)
        self.assertEqual((world.tree / self.BOOT_UNIT).read_text(), '[Unit]\nDescription=mine\n')
        self.assertFalse(os.path.lexists(world.tree / 'etc/udev/rules.d/90-dosplex-game-library.rules'))

    def test_a_foreign_unit_file_is_said_and_never_stops_an_update(self):
        world, url = self.old_library()
        (world.tree / self.AUTO_SERVICE).write_text('[Unit]\nDescription=mine\n')
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('could not be set up', result.out)
        self.assertIn('not the one Dosplex writes', result.out)
        self.assertEqual(world.state()['version'], '0.5.0-b')
        self.assertEqual((world.tree / self.AUTO_SERVICE).read_text(), '[Unit]\nDescription=mine\n')
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_TIMER))


class AutostartUnit(Channeled):
    """The start at boot (`dosplex autostart on`) across an update and a rollback."""
    UNIT = 'etc/systemd/system/dosplex-autostart.service'
    LINK = 'etc/systemd/system/graphical.target.wants/dosplex-autostart.service'

    def on(self):
        world = self.installed('one')
        self.assertOk(world.run('autostart', 'on', '--experimental'))
        _, url = self.channel(world, ['one', 'two'])
        return world, url

    def test_an_update_and_a_rollback_keep_the_start_at_boot_on_with_its_unit_untouched(self):
        world, url = self.on()
        unit = (world.tree / self.UNIT).read_text()
        reloads = [c['argv'] for c in world.commands('systemctl')]
        self.assertOk(self.update(world, url))
        self.assertEqual(world.state()['version'], '0.2.0-b')
        self.assertIs(world.state()['autostart'], True)
        self.assertEqual((world.tree / self.UNIT).read_text(), unit)
        self.assertEqual(os.readlink(world.tree / self.LINK), '../dosplex-autostart.service')
        self.assertIn('Starts at boot: yes', world.run('status').out)
        self.assertOk(world.run('rollback', '--sample-seconds', '0.3'))
        self.assertEqual(world.state()['version'], '0.2.0-a')
        self.assertIs(world.state()['autostart'], True)
        self.assertEqual((world.tree / self.UNIT).read_text(), unit)
        self.assertEqual([c['argv'] for c in world.commands('systemctl')], reloads)       # nothing was written again

    def test_an_update_writes_a_unit_that_went_missing_while_the_start_at_boot_is_on(self):
        world, url = self.on()
        (world.tree / self.UNIT).unlink()
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('was written again', result.out)
        self.assertTrue((world.tree / self.UNIT).is_file())
        (name,) = [name for name in world.journal() if name.endswith('-autostart.jsonl')][-1:]
        self.assertEqual(world.journal()[name][0]['action'], 'units')

    def test_an_update_never_turns_the_start_at_boot_on(self):
        world = self.installed('one')
        _, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        self.assertNotIn('autostart', world.state())
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertIn('Starts at boot: no', world.run('status').out)

    def test_a_foreign_unit_is_said_and_never_stops_an_update(self):
        world, url = self.on()
        (world.tree / self.UNIT).write_text('[Unit]\nDescription=mine\n')
        result = self.update(world, url)
        self.assertOk(result)
        self.assertIn('not the one Dosplex writes', result.out)
        self.assertEqual(world.state()['version'], '0.2.0-b')
        self.assertEqual((world.tree / self.UNIT).read_text(), '[Unit]\nDescription=mine\n')

    def test_the_restart_after_an_update_keeps_the_layout_of_the_running_session(self):
        world = self.installed('one')
        self.assertOk(world.run('start', '--layout', 'single'))
        _, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        self.assertEqual([e['layout'] for e in world.events('trial.start')], ['single', 'single'])
        self.assertEqual(world.state()['trial']['layout'], 'single')


class Interrupted(Channeled):
    """A crash at every journal record and inner step of an update or a rollback, then `repair`.

    Each point gets a fake sysroot of its own, so the points run in parallel threads (as the CLI tests do)."""

    def points(self, prefix, run):
        probe = self.world()
        log = probe.root / 'faults.log'
        run(probe, DOSPLEX_TEST_FAULT_LOG=str(log))
        seen = []
        for point in log.read_text().splitlines():
            if point.startswith(prefix) and point not in seen:
                seen.append(point)
        return seen

    def each(self, items, work):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(item, pool.submit(work, item)) for item in items]
            results = [(item, future.exception(), future) for item, future in futures]
        outcomes = []
        for item, error, future in results:
            with self.subTest(point=item):
                if error is not None:
                    raise error
                outcomes.append(future.result())
        return outcomes

    @staticmethod
    def stripped(value):
        """A state without the times, which differ from device to device."""
        if isinstance(value, dict):
            return {k: Interrupted.stripped(v) for k, v in value.items() if k != 'installed_utc'}
        return value

    @staticmethod
    def opt_part(snapshot):
        return {k: v for k, v in snapshot.items() if k.startswith('opt/')}

    def prepared(self, names=('two',), controllers=False):
        world = self.world()
        self.assertOk(world.install_release(self.releases()['one'], *(world.controllers() if controllers else ('--no-start',))))
        directory, url = self.channel(world, ['one', *names])
        return world, url

    def crashes(self, result, point):
        self.assertEqual(result.code, dosplex.FAULT_EXIT, result.text)       # update and rollback run in the foreground

    def repair_crashes(self, result, point):
        """Repair runs in a background service: its crash is a failed command, except at :end, where it was done."""
        if point.endswith(':end'):
            self.assertOk(result)
        else:
            self.assertFailed(result)

    def check_repair(self, world, kind):
        """Repair finishes the job once, then has nothing to do; returns the outcome of the interrupted tx."""
        text = world.run('status').out
        records = world.journal()
        name = sorted(records)[-1]
        finished = records[name][-1]['kind'] == 'end'
        if not finished:
            self.assertIn('Interrupted: %s' % kind, text)
            self.assertFailed(world.run(kind), 'dosplex repair', code=4)          # nothing else runs until it is repaired
            self.assertFailed(world.run('start'), 'dosplex repair', code=4)
        self.assertOk(world.run('repair'))
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
        after = world.journal()[name]
        self.assertEqual(after[-1]['kind'], 'end')
        return after[-1]['outcome'], any(r['kind'] == 'commit' for r in after)

    def assertWhole(self, world):
        """No staging directory, no half-written file; state, current and the kept release agree."""
        opt = world.tree / 'opt/dosplex'
        state = world.state()
        self.assertEqual(os.readlink(opt / 'current'), state['release'])
        names = sorted(p.name for p in (opt / 'releases').iterdir())
        self.assertEqual(names, sorted({state['version'], state['previous']} - {None}))
        self.assertEqual([p.name for p in (opt / 'bin').iterdir() if p.name.startswith('.')], [])
        self.assertEqual([p.name for p in (world.tree / 'var/lib/dosplex').iterdir() if p.name.startswith('.')], [])
        self.assertEqual(world.run('status').out.splitlines()[0].split()[1], state['version'])

    def test_update_interrupted_at_every_point_is_finished_or_undone_by_repair(self):
        def run(world, **env):
            world.install_release(self.releases()['one'], '--no-start')
            directory, url = self.channel(world, ['one', 'two'])
            self.assertOk(self.update(world, url, **env))
        points = self.points('update:', run)
        for expected in ('update:begin', 'update:intent:stage', 'update:inner:staged', 'update:done:stage', 'update:commit',
                         'update:intent:release', 'update:inner:release-renamed', 'update:done:release',
                         'update:intent:current', 'update:applied:current', 'update:done:current', 'update:intent:state',
                         'update:done:state', 'update:intent:prune', 'update:done:prune', 'update:end'):
            self.assertIn(expected, points)
        reference, url = self.prepared()
        self.assertOk(self.update(reference, url))
        finished_tree, finished_state = self.opt_part(reference.snapshot()), self.stripped(reference.state())

        def work(point):
            world, url = self.prepared()
            started = self.opt_part(world.snapshot())
            self.crashes(self.update(world, url, DOSPLEX_TEST_CRASH_AT=point), point)
            outcome, committed = self.check_repair(world, 'update')
            if committed:
                self.assertEqual(outcome, 'complete')
                self.assertEqual(self.opt_part(world.snapshot()), finished_tree)
                self.assertEqual(self.stripped(world.state()), finished_state)
                self.assertEqual(self.state_of(world), ('0.2.0-b', '0.2.0-a', ['0.2.0-a', '0.2.0-b']))
                self.assertIn('Dosplex is up to date.', self.update(world, url).out)
                self.assertOk(world.run('rollback', '--sample-seconds', '0.3'))
            else:
                self.assertEqual(outcome, 'rolled-back')
                self.assertEqual(self.opt_part(world.snapshot()), started)         # exactly as it was before the update
                self.assertEqual(self.state_of(world), ('0.2.0-a', None, ['0.2.0-a']))
                self.assertOk(self.update(world, url))                               # and it can be done again
                self.assertEqual(self.state_of(world)[0], '0.2.0-b')
            self.assertWhole(world)
            self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
            self.assertEqual(world.snapshot(), world.initial)
            return outcome
        self.assertEqual(set(self.each(points, work)), {'complete', 'rolled-back'})

    def test_update_that_replaces_the_program_is_finished_or_undone_at_every_point(self):
        def run(world, **env):
            world.install_release(self.releases()['one'], '--no-start')
            directory, url = self.channel(world, ['one', 'newprogram'])
            self.assertOk(self.update(world, url, **env))
        points = self.points('update:', run)
        for expected in ('update:intent:cli', 'update:inner:program-kept', 'update:done:cli'):
            self.assertIn(expected, points)
        base, new = FIXTURES.program.read_bytes(), self.releases()['newprogram'].program()

        def work(point):
            world, url = self.prepared(('newprogram',))
            self.crashes(self.update(world, url, DOSPLEX_TEST_CRASH_AT=point), point)
            outcome, committed = self.check_repair(world, 'update')
            bin_dir = world.tree / 'opt/dosplex/bin'
            if committed:
                self.assertEqual((bin_dir / 'dosplex').read_bytes(), new)
                self.assertEqual((bin_dir / 'dosplex.previous').read_bytes(), base)
                self.assertEqual(world.state()['cli']['sha256'], sha256(new))
            else:
                self.assertEqual((bin_dir / 'dosplex').read_bytes(), base)
                self.assertFalse((bin_dir / 'dosplex.previous').exists())
            self.assertWhole(world)
            self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
            self.assertEqual(world.snapshot(), world.initial)
            return outcome
        self.assertEqual(set(self.each(points, work)), {'complete', 'rolled-back'})

    def test_rollback_interrupted_at_every_point_is_finished_or_undone_by_repair(self):
        def updated():
            world, url = self.prepared()
            self.assertOk(self.update(world, url))
            return world

        def run(world, **env):
            world.install_release(self.releases()['one'], '--no-start')
            directory, url = self.channel(world, ['one', 'two'])
            self.assertOk(self.update(world, url))
            self.assertOk(world.run('rollback', '--sample-seconds', '0.3', **env))
        points = self.points('rollback:', run)
        for expected in ('rollback:begin', 'rollback:done:verify-previous', 'rollback:commit', 'rollback:intent:current',
                         'rollback:done:current', 'rollback:intent:state', 'rollback:done:state', 'rollback:end'):
            self.assertIn(expected, points)

        def work(point):
            world = updated()
            started = self.opt_part(world.snapshot())
            crashed = world.run('rollback', '--sample-seconds', '0.3', DOSPLEX_TEST_CRASH_AT=point)
            self.crashes(crashed, point)
            outcome, committed = self.check_repair(world, 'rollback')
            if committed:
                self.assertEqual(outcome, 'complete')
                self.assertEqual(self.state_of(world), ('0.2.0-a', '0.2.0-b', ['0.2.0-a', '0.2.0-b']))
                self.assertEqual(world.state()['signed']['sequence'], 100)
            else:
                self.assertEqual(outcome, 'rolled-back')
                self.assertEqual(self.opt_part(world.snapshot()), started)
                self.assertEqual(self.state_of(world), ('0.2.0-b', '0.2.0-a', ['0.2.0-a', '0.2.0-b']))
            self.assertWhole(world)
            self.assertOk(world.run('rollback', '--sample-seconds', '0.3'))             # it still works afterwards
            self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
            self.assertEqual(world.snapshot(), world.initial)
            return outcome
        self.assertEqual(set(self.each(points, work)), {'complete', 'rolled-back'})

    def test_a_crash_inside_repair_is_repaired_by_the_next_repair(self):
        for first in ('update:done:stage', 'update:done:current'):             # one that rolls back, one that completes
            probe, url = self.prepared()
            probe_crash = self.update(probe, url, DOSPLEX_TEST_CRASH_AT=first)
            self.assertEqual(probe_crash.code, dosplex.FAULT_EXIT)
            log = probe.root / 'repair-faults.log'
            self.assertOk(probe.run('repair', DOSPLEX_TEST_FAULT_LOG=str(log)))
            second_points = []
            for point in log.read_text().splitlines():
                if point.startswith('update:') and point not in second_points:
                    second_points.append(point)
            self.assertGreater(len(second_points), 3, first)

            def work(second, first=first):
                world, url = self.prepared()
                self.assertEqual(self.update(world, url, DOSPLEX_TEST_CRASH_AT=first).code, dosplex.FAULT_EXIT)
                self.repair_crashes(world.run('repair', DOSPLEX_TEST_CRASH_AT=second), second)
                self.assertOk(world.run('repair'))
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
                self.assertWhole(world)
                self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
                self.assertEqual(world.snapshot(), world.initial)
            self.each(second_points, work)

    def test_a_running_session_survives_an_interrupted_update_and_repair(self):
        world, url = self.prepared(controllers=True)
        (trial,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        self.assertEqual(self.update(world, url, DOSPLEX_TEST_CRASH_AT='update:done:current').code, dosplex.FAULT_EXIT)
        result = world.run('repair')
        self.assertOk(result)
        self.assertIn('The update was completed.', result.out)
        self.assertIn('Dosplex keeps running the release it started with', result.out)
        self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [trial])
        self.assertIn('Running: yes', world.run('status').out)
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start'))
        self.assertTrue(world.events('trial.start')[-1]['bundle'].endswith('/releases/0.2.0-b'))


class Rehearsal(Channeled):
    """T-702's rehearsal, v(N-1) -> vN -> rollback -> vN, with a running session, against a fake sysroot. It is
    synthetic: the hardware rehearsal on a clean device belongs to T-704."""

    def step(self, world, expected_version, expected_previous, trials):
        state = world.state()
        self.assertEqual((state['version'], state['previous']), (expected_version, expected_previous))
        self.assertEqual(os.readlink(world.tree / 'opt/dosplex/current'), 'releases/' + expected_version)
        self.assertEqual(len(world.events('trial.start')), trials)
        starts = world.events('trial.start')
        self.assertTrue(starts[-1]['bundle'].endswith('/releases/' + expected_version), starts[-1])
        self.assertEqual(starts[-1]['manifest'], state['manifest_sha256'])
        (root,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        self.assertEqual(state['trial']['root'], str(root))
        status = world.run('status').out
        self.assertIn('Running: yes', status)
        self.assertEqual(status.splitlines()[0].split()[1], expected_version)

    def test_install_update_rollback_update_with_a_running_session(self):
        world = self.world()
        one, two = self.releases()['one'], self.releases()['two']
        self.assertOk(world.install_release(one, *world.controllers()))
        self.step(world, '0.2.0-a', None, 1)
        _, url = self.channel(world, ['one', 'two'])
        self.assertOk(self.update(world, url))
        self.step(world, '0.2.0-b', '0.2.0-a', 2)
        self.assertOk(world.run('rollback', '--sample-seconds', '0.3'))
        self.step(world, '0.2.0-a', '0.2.0-b', 3)
        self.assertOk(self.update(world, url))
        self.step(world, '0.2.0-b', '0.2.0-a', 4)
        self.assertEqual(sorted(p.name for p in (world.tree / 'opt/dosplex/releases').iterdir()), ['0.2.0-a', '0.2.0-b'])
        self.assertEqual(world.state()['manifest_sha256'], two.manifest_sha256)
        self.assertEqual(world.state()['previous_info']['manifest_sha256'], one.manifest_sha256)
        self.assertEqual(sorted(world.journal()), ['0001-install.jsonl', '0002-start.jsonl', '0003-update.jsonl',
                                                   '0004-stop.jsonl', '0005-start.jsonl', '0006-rollback.jsonl',
                                                   '0007-stop.jsonl', '0008-start.jsonl', '0009-update.jsonl',
                                                   '0010-stop.jsonl', '0011-start.jsonl'])
        for name, records in world.journal().items():
            self.assertEqual(records[-1]['kind'], 'end', name)
            self.assertEqual(records[-1]['outcome'], 'complete', name)
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)


class KeyRotation(Channeled):
    """The documented rotation, end to end: a release that trusts both keys bridges the old key to the new one."""

    def releases_for_rotation(self):
        with FIXTURES.lock:
            if not hasattr(FIXTURES, 'rotation'):
                FIXTURES.rotation = self.build_rotation_releases()
        return FIXTURES.rotation

    def build_rotation_releases(self):
        old, new = FIXTURES.key, FIXTURES.other_key
        trusts_old, trusts_both, trusts_new = (FIXTURES.program_pinned_to(*keys) for keys in ((old,), (old, new), (new,)))
        self.assertEqual(trusts_old.read_bytes(), FIXTURES.program.read_bytes())
        made = {}
        for name, version, sequence, program, secret in (('first', '0.3.0-a', 1000, trusts_old, FIXTURES.secret),
                                                         ('bridge', '0.3.0-b', 2000, trusts_both, FIXTURES.secret),
                                                         ('last', '0.3.0-c', 3000, trusts_new, FIXTURES.other_secret)):
            bundle = cli_test.Bundle(FIXTURES.dir / ('bundle-rotation-' + name), version=version, extra=SBOM_FILES)
            made[name] = FIXTURES.build('rotation-' + name, bundle.path, sequence, program, secret=secret)
        return made, {'old': trusts_old, 'both': trusts_both, 'new': trusts_new}

    def channel_of(self, world, made, names, program, secret, directory='rotation-channel'):
        directory = world.root / directory
        directory.mkdir()
        entries = []
        for name in names:
            for item in made[name].dir.iterdir():
                shutil.copy(item, directory / item.name)
            entries.append(made[name].entry)
        scratch = world.root / (directory.name + '-scratch')
        FIXTURES.release_py('index', *entries, '--out', scratch, '--channel', 'preview', '--program', program,
                            '--sign-with', secret)
        for item in scratch.iterdir():
            shutil.move(str(item), str(directory / item.name))
        return (directory / 'dosplex-index.json').as_uri()

    def test_old_devices_go_through_the_bridge_release_to_the_new_key(self):
        made, programs = self.releases_for_rotation()
        world = self.world()                                                       # runs the program that trusts the old key
        self.assertOk(world.install_release(made['first'], '--no-start'))
        window = self.channel_of(world, made, ['first', 'bridge', 'last'], programs['both'], FIXTURES.secret)
        result = self.update(world, window)
        self.assertOk(result)
        self.assertIn('Skipped 0.3.0-c (sequence 3000): signed with key %s, which this dosplex does not trust'
                      % FIXTURES.other_key.key_id, result.out)
        self.assertEqual(self.state_of(world)[0], '0.3.0-b')
        installed = world.tree / 'opt/dosplex/bin/dosplex'
        self.assertEqual(installed.read_bytes(), programs['both'].read_bytes())          # now it trusts both
        world.program = installed
        result = self.update(world, window)
        self.assertOk(result)
        self.assertEqual(self.state_of(world)[0], '0.3.0-c')
        self.assertEqual(installed.read_bytes(), programs['new'].read_bytes())           # and now only the new key
        self.assertEqual((world.tree / 'opt/dosplex/bin/dosplex.previous').read_bytes(), programs['both'].read_bytes())
        self.assertEqual(world.state()['signed']['key_id'], FIXTURES.other_key.key_id)
        # The window is closed: an index signed with the new key is read, one signed with the old key is not.
        closed = self.channel_of(world, made, ['first', 'bridge', 'last'], programs['new'], FIXTURES.other_secret, 'closed')
        self.assertIn('Dosplex is up to date.', self.update(world, closed).out)
        before = world.snapshot(exclude=())
        result = self.update(world, window)
        self.assertFailed(result, 'does not trust')
        self.assertIn(FIXTURES.key.key_id, result.err)
        self.assertUnchanged(world, before)

    def test_a_device_that_skipped_the_bridge_is_told_what_to_do(self):
        made, programs = self.releases_for_rotation()
        world = self.world()
        self.assertOk(world.install_release(made['first'], '--no-start'))
        only_last = self.channel_of(world, made, ['first', 'last'], programs['both'], FIXTURES.secret)
        before = world.snapshot(exclude=())
        result = self.update(world, only_last)
        self.assertFailed(result, 'cannot check any newer release')
        self.assertIn('0.3.0-c is signed with key %s' % FIXTURES.other_key.key_id, result.err)
        self.assertIn('Install the newest release by hand', result.err)
        self.assertUnchanged(world, before)
        # And one signed with the new key altogether names both keys and says the same.
        new_signed = self.channel_of(world, made, ['first', 'last'], programs['new'], FIXTURES.other_secret, 'new-signed')
        result = self.update(world, new_signed)
        self.assertFailed(result, 'does not trust')
        self.assertIn('install the newest release by hand', result.err)
        self.assertUnchanged(world, before)


class FailureAfterTheCommit(Channeled):
    """An error (not a crash) in a step after the commit record: the update is undone and the journal says so."""

    def in_process(self, world, *args, apply_table=None):
        pinned = load_module('dosplex_pinned_in_process', FIXTURES.program)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, world.environment(), clear=True), \
                mock.patch.object(sys, 'argv', [str(FIXTURES.program), *map(str, args)]), \
                mock.patch.dict(pinned.UPDATE_APPLY, apply_table(pinned) if apply_table else {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pinned.main(['--sysroot', str(world.tree), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_a_step_that_fails_after_the_commit_undoes_the_whole_update(self):
        for failing in ('prune', 'state', 'current'):
            with self.subTest(step=failing):
                world = self.installed('one')
                _, url = self.channel(world, ['one', 'newprogram'])
                started, state = Interrupted.opt_part(world.snapshot()), world.state()

                def broken(pinned, failing=failing):
                    def fail(env, tx, data):
                        raise pinned.DosplexError('the disk is full')
                    return {failing: fail}
                code, out, err = self.in_process(world, 'update', '--channel', url, '--sample-seconds', '0.3', apply_table=broken)
                self.assertEqual(code, 1)
                self.assertIn('the disk is full', err)
                self.assertEqual(Interrupted.opt_part(world.snapshot()), started)     # program, releases and link as before
                self.assertEqual(world.state(), state)
                records = world.journal()['0002-update.jsonl']
                self.assertEqual((records[-1]['kind'], records[-1]['outcome']), ('end', 'rolled-back'))
                self.assertIn('the disk is full', records[-1]['error'])
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
                self.assertOk(self.update(world, url))                               # and the next try works
                self.assertEqual(self.state_of(world)[0], '0.2.0-n')


class ChannelAddress(unittest.TestCase):
    """The address handling, in this process, with a program that has the test key pinned."""

    @classmethod
    def setUpClass(cls):
        cls.pinned = load_module('dosplex_pinned_copy', FIXTURES.program)

    def test_the_default_is_the_release_index_of_the_github_releases(self):
        self.assertTrue(self.pinned.DEFAULT_CHANNEL.startswith('https://github.com/'))
        self.assertTrue(self.pinned.DEFAULT_CHANNEL.endswith('/releases/latest/download/dosplex-index.json'))
        self.assertEqual(self.pinned.CHANNEL_ENV, 'DOSPLEX_CHANNEL')

    def test_the_flag_beats_the_environment_beats_the_default(self):
        module = self.pinned
        asked = []

        def fetch(url, limit, what, timeout=60):
            asked.append(url)
            raise module.DosplexError('stop here')
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            for name in ('etc', 'var/lib', 'opt', 'run', 'home'):
                (tree / name).mkdir(parents=True)
            state = {'schema': 1, 'version': 'x', 'release': 'releases/x', 'manifest_sha256': 'a' * 64, 'commit': 'c' * 40,
                     'main_user': 'deck'}
            env = module.Env(tree)
            with mock.patch.object(module, 'read_state', return_value=state), mock.patch.object(module, 'check_current'), \
                    mock.patch.object(module.Env, 'require_root'), mock.patch.object(module, 'fetch_url', fetch), \
                    mock.patch.object(module.Journal, 'open', return_value=None):
                for flag, environment, expected in (
                        (None, None, module.DEFAULT_CHANNEL), (None, 'file:///from/env/index.json', 'file:///from/env/index.json'),
                        ('file:///from/flag/index.json', 'file:///from/env/index.json', 'file:///from/flag/index.json'),
                        ('/an/absolute/path/index.json', None, 'file:///an/absolute/path/index.json')):
                    del asked[:]
                    args = module.build_parser().parse_args(['update'] + (['--channel', flag] if flag else []))
                    with mock.patch.dict(os.environ, {module.CHANNEL_ENV: environment} if environment else {}, clear=False), \
                            contextlib.redirect_stdout(io.StringIO()):
                        if not environment:
                            os.environ.pop(module.CHANNEL_ENV, None)
                        with self.assertRaises(module.DosplexError):
                            module.cmd_update(env, args)
                    self.assertEqual(asked, [expected])

    def test_addresses(self):
        module = self.pinned
        self.assertEqual(module.channel_address('https://example.invalid/i.json'), 'https://example.invalid/i.json')
        self.assertEqual(module.channel_address('file:///tmp/i.json'), 'file:///tmp/i.json')
        self.assertEqual(module.channel_address('/tmp/with space/i.json'), 'file:///tmp/with%20space/i.json')
        for bad in ('http://example.invalid/i.json', 'ftp://x/y', 'i.json', 'host/i.json', ''):
            with self.assertRaises(module.UsageError, msg=bad):
                module.channel_address(bad)

    def test_credentials_in_an_address_never_reach_a_message(self):
        module = self.pinned
        word = 'not-a-real-word'                                  # built here so that no URL in the source holds one
        self.assertEqual(module.redact_url('https://user:%s@example.invalid/x' % word), 'https://example.invalid/x')
        with self.assertRaises(module.DosplexError) as caught:
            module.fetch_url('ftp://user:%s@example.invalid/x' % word, 10, 'thing')
        self.assertNotIn(word, str(caught.exception))

    def test_a_fetch_is_limited_in_size_and_reports_what_went_wrong(self):
        module = self.pinned
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'big'
            path.write_bytes(bytes(1000))
            self.assertEqual(module.fetch_url(path.as_uri(), 1000, 'thing'), bytes(1000))
            with self.assertRaises(module.VerifyError) as caught:
                module.fetch_url(path.as_uri(), 999, 'thing')
            self.assertIn('larger than 999 bytes', str(caught.exception))
            with self.assertRaises(module.DosplexError) as caught:
                module.fetch_url((Path(directory) / 'absent').as_uri(), 10, 'release index')
            self.assertIn('cannot fetch the release index', str(caught.exception))

    def test_a_redirect_may_only_stay_on_https(self):
        module = self.pinned
        handler = module._HttpsOnly()
        request = module.urllib.request.Request('https://example.invalid/a')
        with self.assertRaises(module.urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'http://example.invalid/b')
        with self.assertRaises(module.urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, 'Found', {}, 'file:///etc/passwd')
        followed = handler.redirect_request(request, io.BytesIO(), 302, 'Found', {}, 'https://objects.example.invalid/b')
        self.assertEqual(followed.full_url, 'https://objects.example.invalid/b')

    def test_an_https_fetch_sends_the_program_name_and_reports_what_went_wrong(self):
        module = self.pinned
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opening(opener, request, data=None, timeout=None):
            seen.append((request.full_url, request.get_header('User-agent'), timeout))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)

        outcomes = [b'the index', module.urllib.error.HTTPError('https://example.invalid/i', 404, 'Not Found', {}, None),
                    module.urllib.error.URLError('name or service not known'), TimeoutError('timed out'),
                    module.http.client.IncompleteRead(b'x'), b'x' * 11]
        with mock.patch.object(module.urllib.request.OpenerDirector, 'open', opening):
            self.assertEqual(module.fetch_url('https://example.invalid/i', 100, 'index'), b'the index')
            for needle in ('HTTP 404 Not Found', 'name or service not known', 'timed out', 'IncompleteRead'):
                with self.assertRaises(module.DosplexError, msg=needle) as caught:
                    module.fetch_url('https://example.invalid/i', 100, 'index')
                self.assertIn(needle, str(caught.exception))
                self.assertIn('cannot fetch the index from https://example.invalid/i', str(caught.exception))
            with self.assertRaises(module.VerifyError):
                module.fetch_url('https://example.invalid/i', 10, 'index')
        self.assertEqual({entry[1] for entry in seen}, {'dosplex/' + module.CLI_VERSION})
        self.assertEqual({entry[2] for entry in seen}, {60})

    def test_other_schemes_are_never_fetched(self):
        for url in ('http://example.invalid/x', 'ftp://example.invalid/x', '/etc/passwd', 'data:text/plain,hi'):
            with self.assertRaises(self.pinned.DosplexError, msg=url):
                self.pinned.fetch_url(url, 10, 'thing')

    def test_the_index_is_validated(self):
        module = self.pinned
        good = {'format': 'dosplex-index', 'format_version': 1, 'channel': 'preview', 'releases': []}
        self.assertEqual(module.parse_index(json.dumps(good).encode(), 'file:///i.json').releases, [])
        for change in ({'format': 'x'}, {'format_version': 2}, {'channel': 'Bad Name'}, {'channel': ''}, {'releases': {}},
                       {'releases': [1]}, {'releases': [{}] * 65}):
            with self.assertRaises(module.VerifyError, msg=str(change)):
                module.parse_index(json.dumps(dict(good, **change)).encode(), 'file:///i.json')


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id().split('.', 1)[1]


def weight(name):
    """A rough cost, so that the slow tests are spread over the workers."""
    if 'at_every' in name:
        return 10
    if 'crash_inside_repair' in name:
        return 6
    return 3 if name.startswith(('KeyRotation.', 'Interrupted.')) else 2


def run_in_parallel(workers):
    """Run every test in `workers` processes (the tests are independent: each builds its own fake sysroot)."""
    names = sorted(test_ids(unittest.defaultTestLoader.loadTestsFromModule(sys.modules['__main__'])),
                   key=lambda n: -weight(n))
    buckets = [[] for _ in range(workers)]
    loads = [0] * workers
    for name in names:
        index = loads.index(min(loads))
        buckets[index].append(name)
        loads[index] += weight(name)
    started = time.monotonic()
    processes = [subprocess.Popen([sys.executable, __file__, *bucket], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for bucket in buckets if bucket]
    failed = False
    for process in processes:
        output, _ = process.communicate()
        failed = failed or process.returncode != 0
        if process.returncode != 0:
            print(output, end='')
    print('Ran %d tests in %d processes in %.1fs: %s' % (len(names), len(processes), time.monotonic() - started,
                                                        'FAILED' if failed else 'OK'))
    return 1 if failed else 0


if __name__ == '__main__':
    # With test names on the command line, run just those. Without, use several processes.
    if len(sys.argv) > 1 or os.environ.get('DOSPLEX_TEST_SERIAL') == '1':
        unittest.main()
    sys.exit(run_in_parallel(min(6, os.cpu_count() or 1)))
