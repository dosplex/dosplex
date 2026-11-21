#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""tools/release.py (T-702): the files it writes, what it refuses and the checks it makes before it publishes.

Hermetic and fast. The bundle is a small stand-in with the layout of a real one. Signing goes through a fake
`minisign` on PATH that signs with a key made by the test (tests/release-signing-test.py has the TEST ONLY
signer): release.py runs the command line it would run for the real tool, and the real tool is not needed.
What it writes is then read back with the verifier of `dosplex` itself.
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


signing = load_module('release_signing_test_helpers', ROOT / 'tests' / 'release-signing-test.py')
release = load_module('release_under_test', ROOT / 'tools' / 'release.py')
dosplex = signing.dosplex
OnlyForTestsKey = signing.OnlyForTestsKey

COMMIT = '0123456789abcdef0123456789abcdef01234567'
EPOCH = 1791531806
TESTED = {'steamos': [{'version_id': '3.9.2', 'build_id': '20260925.101'}], 'gamescope': ['3.16.30'],
          'steam_client': ['1759461351']}


def make_bundle(directory, version='0.2.0-test', sbom=True, notices=True, extra=None, epoch=EPOCH):
    """A small prepared bundle with the layout of a real one: every file `dosplex start` needs, and a manifest."""
    directory = Path(directory)
    contents = {}
    for name in dosplex.REQUIRED_RELEASE_FILES:
        contents[name] = ('#!/bin/sh\n# stand-in for %s %s\n' % (name, version)).encode()
    contents['lib/libstandin.so.1'] = bytes(range(256)) * 4
    contents['LICENSE'] = b'GNU Affero General Public License v3\n'
    contents['NOTICE'] = b'Notice text\n'
    contents['licenses/standin-LICENSE'] = b'licence of a library\n'
    if notices:
        contents['THIRD-PARTY-NOTICES.md'] = b'# Third-party notices\n'
    if sbom:
        contents['sbom/dosplex-sbom.json'] = json.dumps(
            {'format': 'dosplex-sbom', 'package': {'name': 'dosplex', 'version': version, 'commit': COMMIT,
                                                  'source_date_epoch': epoch}}, sort_keys=True).encode()
    contents.update(extra or {})
    files = {}
    for name, data in sorted(contents.items()):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        mode = 0o755 if name.startswith(('bin/', 'libexec/', 'tools/')) else 0o644
        target.chmod(mode)
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode, 'bytes': len(data)}
    manifest = {'commit': COMMIT, 'version': version, 'scope': 'test bundle', 'tested_versions': TESTED,
                'files': files}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return directory


class Workspace(unittest.TestCase):
    """A temporary directory with a test key pair, a bundle and a `minisign` that signs with the test key."""

    NAME = 'dosplex-0.2.0-test'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-release-test-')
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.key = OnlyForTestsKey()
        self.secret = self.dir / 'test-only.key'
        self.secret.write_text(self.key.secret_file_text())
        self.public = self.dir / 'test-only.pub'
        self.public.write_text(self.key.public_file())
        self.fakebin = self.dir / 'fakebin'
        self.fakebin.mkdir()
        signing.install_fake_minisign(self.fakebin)
        self.log = self.dir / 'minisign.log'
        self.bundle = make_bundle(self.dir / 'bundle')

    def run_release(self, *argv, minisign=True, **environment):
        """release.main(argv) in this process, with the fake minisign first on PATH. Returns (code, out, err)."""
        path = (str(self.fakebin) + os.pathsep if minisign else '') + os.environ['PATH']
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, dict(PATH=path, FAKE_MINISIGN_LOG=str(self.log), **environment)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = release.main([str(a) for a in argv])
                except SystemExit as stop:
                    code = stop.code
        return code, stdout.getvalue(), stderr.getvalue()

    def unpinned_program(self):
        """A copy of tools/dosplex with no release key pinned (the checkout pins the owner's key)."""
        path = self.dir / 'dosplex-unpinned'
        if not path.exists():
            path.write_text(release.pin_program((ROOT / 'tools/dosplex').read_text(encoding='utf-8'), []),
                            encoding='utf-8')
        return path

    def build(self, *extra, out='release', sign=True, minisign=True, **environment):
        argv = ['build', self.bundle, '--out', self.dir / out, '--pin-key', self.public, *extra]
        if sign:
            argv += ['--sign-with', self.secret]
        return self.run_release(*argv, minisign=minisign, **environment)

    def assertBuilt(self, result):
        self.assertEqual(result[0], 0, result[1] + result[2])
        return result

    def files(self, out='release'):
        return sorted(p.name for p in (self.dir / out).iterdir())

    def trusted(self, key=None):
        key = key or self.key
        return [dosplex.ReleaseKey(key.keynum, key.public)]


class ReleaseBuild(Workspace):
    def test_it_writes_the_archive_the_manifest_its_signature_the_entry_and_the_copies(self):
        self.assertBuilt(self.build())
        self.assertEqual(self.files(), sorted(self.NAME + suffix for suffix in (
            '.tar.gz', '.manifest.json', '.manifest.json.minisig', '.index-entry.json', '.sbom.json',
            '.THIRD-PARTY-NOTICES.md', '.NOTICE')))
        out = self.dir / 'release'
        self.assertEqual((out / (self.NAME + '.sbom.json')).read_bytes(), (self.bundle / 'sbom/dosplex-sbom.json').read_bytes())
        self.assertEqual((out / (self.NAME + '.NOTICE')).read_bytes(), (self.bundle / 'NOTICE').read_bytes())
        for item in out.iterdir():
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o755)

    def test_the_manifest_is_the_bundle_manifest_plus_a_release_entry_and_is_the_one_in_the_archive(self):
        self.assertBuilt(self.build())
        out = self.dir / 'release'
        manifest_raw = (out / (self.NAME + '.manifest.json')).read_bytes()
        original = json.loads((self.bundle / 'manifest.json').read_text())
        released = json.loads(manifest_raw)
        members = dosplex.read_archive_members((out / (self.NAME + '.tar.gz')).read_bytes())
        self.assertEqual(members[self.NAME + '/bundle/manifest.json'], manifest_raw)
        self.assertEqual({key: value for key, value in released.items() if key != 'release'}, original)
        self.assertEqual(list(released)[-2:], ['release', 'files'])
        program = members[self.NAME + '/dosplex']
        self.assertEqual(released['release'], {'format': 1, 'sequence': EPOCH, 'cli': {
            'version': dosplex.CLI_VERSION, 'sha256': hashlib.sha256(program).hexdigest(), 'bytes': len(program)}})
        self.assertEqual(dosplex.parse_manifest(manifest_raw).release['sequence'], EPOCH)

    def test_the_archive_has_one_directory_the_manifest_first_and_plain_reproducible_members(self):
        self.assertBuilt(self.build('--source-date-epoch', '1000'))
        data = (self.dir / 'release' / (self.NAME + '.tar.gz')).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            infos = archive.getmembers()
        names = [info.name for info in infos]
        self.assertEqual(names[0], self.NAME + '/bundle/manifest.json')
        self.assertEqual(names[-1], self.NAME + '/dosplex')
        self.assertEqual(names[1:-1], sorted(names[1:-1]))
        self.assertEqual({name.split('/')[0] for name in names}, {self.NAME})
        for info in infos:
            self.assertEqual((info.type, info.uid, info.gid, info.uname, info.gname, info.mtime),
                             (tarfile.REGTYPE, 0, 0, '', '', 1000), info.name)
        self.assertEqual(stat.S_IMODE(infos[-1].mode), 0o755)
        self.assertEqual(stat.S_IMODE(infos[0].mode), 0o644)
        self.assertEqual(data[:4], b'\x1f\x8b\x08\x00')                 # gzip, without a file name
        self.assertEqual(data[4:8], bytes(4))                            # and without a time

    def test_the_same_inputs_give_the_same_bytes_including_the_signature(self):
        self.assertBuilt(self.build(out='one'))
        self.assertBuilt(self.build(out='two'))
        self.assertEqual(self.files('one'), self.files('two'))
        for name in self.files('one'):
            self.assertEqual((self.dir / 'one' / name).read_bytes(), (self.dir / 'two' / name).read_bytes(), name)

    def test_the_signature_is_the_detached_minisign_signature_of_the_manifest_file(self):
        self.assertBuilt(self.build())
        out = self.dir / 'release'
        manifest = (out / (self.NAME + '.manifest.json')).read_bytes()
        signature = (out / (self.NAME + '.manifest.json.minisig')).read_text()
        signed = dosplex.verify_signature(manifest, signature, self.trusted(), 'manifest')
        self.assertEqual((signed.key_id, signed.subject), (self.key.key_id, '0.2.0-test'))
        command = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(command[0], '-S')
        self.assertEqual(command[command.index('-s') + 1], str(self.secret))
        self.assertEqual(command[command.index('-t') + 1], 'dosplex manifest 0.2.0-test')
        self.assertTrue(command[command.index('-m') + 1].endswith(self.NAME + '.manifest.json'))

    def test_the_program_in_the_archive_pins_the_key_and_accepts_the_release(self):
        result = self.assertBuilt(self.build())
        archive = (self.dir / 'release' / (self.NAME + '.tar.gz')).read_bytes()
        members = dosplex.read_archive_members(archive)
        shipped = members[self.NAME + '/dosplex'].decode()
        original = (ROOT / 'tools/dosplex').read_text()
        self.assertIn("{'id': %r, 'public': %r}," % (self.key.key_id, self.key.public.hex()), shipped)
        # Only the RELEASE KEYS block differs from the repository's program.
        self.assertEqual(release.KEY_BLOCK.sub('', shipped), release.KEY_BLOCK.sub('', original))
        module, _ = release.load_program(ROOT / 'tools/dosplex', [self.key.pin()])
        self.assertEqual([key.id for key in module.pinned_keys()], [self.key.key_id])
        signature = (self.dir / 'release' / (self.NAME + '.manifest.json.minisig')).read_text()
        module.verify_release_archive(archive, signature, module.pinned_keys())     # the shipped program accepts it
        self.assertIn('the installer hash for the release page', result[1])
        self.assertIn(hashlib.sha256(shipped.encode()).hexdigest(), result[1])

    def test_an_unsigned_build_says_how_to_sign_and_never_looks_for_minisign(self):
        code, out, err = self.assertBuilt(self.build(sign=False, minisign=False))
        self.assertIn('unsigned', out)
        self.assertIn('minisign -S -s SECRET_KEY -m %s.manifest.json' % self.NAME, out)
        self.assertIn('-t "dosplex manifest 0.2.0-test"', out)
        self.assertNotIn(self.NAME + '.manifest.json.minisig', self.files())
        self.assertFalse(self.log.exists())

    def test_two_pinned_keys_describe_the_first_release_of_a_rotation(self):
        other = OnlyForTestsKey()
        (self.dir / 'other.pub').write_text(other.public_file())
        self.assertBuilt(self.run_release('build', self.bundle, '--out', self.dir / 'rotation', '--pin-key', self.public,
                                          '--pin-key', self.dir / 'other.pub', '--sign-with', self.secret))
        members = dosplex.read_archive_members((self.dir / 'rotation' / (self.NAME + '.tar.gz')).read_bytes())
        shipped = members[self.NAME + '/dosplex'].decode()
        self.assertLess(shipped.index(self.key.key_id), shipped.index(other.key_id))
        module, _ = release.load_program(ROOT / 'tools/dosplex', [self.key.pin(), other.pin()])
        self.assertEqual([key.id for key in module.pinned_keys()], [self.key.key_id, other.key_id])

    def test_the_index_entry_describes_the_archive_for_the_channel(self):
        self.assertBuilt(self.build('--base-url', 'https://example.invalid/download/v0.2.0-test'))
        out = self.dir / 'release'
        entry = json.loads((out / (self.NAME + '.index-entry.json')).read_text())
        archive, manifest = (out / (self.NAME + '.tar.gz')).read_bytes(), (out / (self.NAME + '.manifest.json')).read_bytes()
        base = 'https://example.invalid/download/v0.2.0-test/' + self.NAME
        self.assertEqual(entry, {
            'version': '0.2.0-test', 'sequence': EPOCH, 'commit': COMMIT, 'tested_versions': TESTED,
            'manifest': {'url': base + '.manifest.json', 'sha256': hashlib.sha256(manifest).hexdigest(), 'bytes': len(manifest)},
            'signature': {'url': base + '.manifest.json.minisig'},
            'tarball': {'url': base + '.tar.gz', 'sha256': hashlib.sha256(archive).hexdigest(), 'bytes': len(archive)}})

    def test_without_a_base_url_the_entry_names_are_relative_to_the_index(self):
        self.assertBuilt(self.build())
        entry = json.loads((self.dir / 'release' / (self.NAME + '.index-entry.json')).read_text())
        self.assertEqual(entry['tarball']['url'], self.NAME + '.tar.gz')

    def test_the_sequence_comes_from_the_argument_the_sbom_or_nowhere(self):
        self.assertBuilt(self.build('--sequence', '77', out='given'))
        entry = json.loads((self.dir / 'given' / (self.NAME + '.index-entry.json')).read_text())
        self.assertEqual(entry['sequence'], 77)
        bare = make_bundle(self.dir / 'bare', sbom=False)
        argv = ('build', bare, '--out', self.dir / 'bare-out', '--pin-key', self.public, '--allow-incomplete')
        code, out, err = self.run_release(*argv)
        self.assertEqual(code, 1)
        self.assertIn('needs a sequence number', err)
        self.assertFalse((self.dir / 'bare-out').exists())
        self.assertEqual(self.run_release(*argv, '--sequence', '5')[0], 0)

    def test_it_refuses_a_bundle_that_does_not_agree_with_its_own_manifest(self):
        (self.bundle / 'LICENSE').write_bytes(b'changed\n')
        code, out, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn('does not agree with its own manifest', err)
        self.assertFalse((self.dir / 'release').exists())
        self.assertEqual([p.name for p in self.dir.glob('.release-*')], [])

    def test_it_refuses_a_bundle_without_sbom_or_notices_unless_told_it_is_a_trial(self):
        for label, kwargs, needle in (('no-sbom', dict(sbom=False), 'no sbom/dosplex-sbom.json'),
                                      ('no-notices', dict(notices=False), 'no THIRD-PARTY-NOTICES.md')):
            with self.subTest(label):
                bundle = make_bundle(self.dir / label, **kwargs)
                argv = ('build', bundle, '--out', self.dir / (label + '-out'), '--pin-key', self.public, '--sequence', '3')
                code, out, err = self.run_release(*argv)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
                self.assertEqual(self.run_release(*argv, '--allow-incomplete')[0], 0)

    def test_it_refuses_a_release_whose_notices_hold_an_open_owner_decision(self):
        bundle = make_bundle(self.dir / 'decision')
        (bundle / 'THIRD-PARTY-NOTICES.md').write_bytes(
            b'# Third-party notices\n'
            b'- **OWNER DECISION REQUIRED:** The SBOM records no license for standin 1.0.\n'
            b'- **OWNER DECISION REQUIRED:** No license text ships for other 2.0.\n')
        manifest = json.loads((bundle / 'manifest.json').read_text())
        data = (bundle / 'THIRD-PARTY-NOTICES.md').read_bytes()
        manifest['files']['THIRD-PARTY-NOTICES.md'] = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o644,
                                                       'bytes': len(data)}
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        code, out, err = self.run_release('build', bundle, '--out', self.dir / 'decision-out', '--pin-key', self.public)
        self.assertEqual(code, 1)
        self.assertIn('2 line(s)', err)
        self.assertIn(release.DECISION_MARKER, err)
        self.assertIn('OWNER DECISION REQUIRED:** The SBOM records no license for standin', err)
        self.assertIn('--allow-incomplete', err)
        self.assertFalse((self.dir / 'decision-out').exists())
        code, out, warn = self.run_release('build', bundle, '--out', self.dir / 'decision-out', '--pin-key', self.public,
                                           '--allow-incomplete')
        self.assertEqual(code, 0)
        self.assertIn('2 open owner decision(s)', warn)
        self.assertIn('must not be published', warn)

    def test_notices_without_the_marker_build_as_before(self):
        bundle = make_bundle(self.dir / 'quiet')
        (bundle / 'THIRD-PARTY-NOTICES.md').write_bytes(b'# Third-party notices\n'
                                                       b'- The SBOM records standin 1.0 under AGPL-3.0.\n')
        manifest = json.loads((bundle / 'manifest.json').read_text())
        data = (bundle / 'THIRD-PARTY-NOTICES.md').read_bytes()
        manifest['files']['THIRD-PARTY-NOTICES.md'] = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o644,
                                                       'bytes': len(data)}
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        code, out, err = self.run_release('build', bundle, '--out', self.dir / 'quiet-out', '--pin-key', self.public)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn('decision', err)

    def test_it_refuses_a_bundle_that_lacks_what_start_needs(self):
        manifest = json.loads((self.bundle / 'manifest.json').read_text())
        victim = 'tools/start-external-native-trial.py'
        (self.bundle / victim).unlink()
        del manifest['files'][victim]
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        code, out, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn(victim, err)

    def test_it_says_what_to_do_with_a_release_payload_that_is_not_a_bundle(self):
        payload = self.dir / 'payload'
        (payload / 'share/dosplex').mkdir(parents=True)
        (payload / 'share/dosplex/VERSION').write_text('0.2.0-test\n')
        (payload / 'bin').mkdir()
        (payload / 'bin/dosplex-compositor').write_bytes(b'\x7fELF')
        code, out, err = self.run_release('build', payload, '--out', self.dir / 'from-payload', '--pin-key', self.public)
        self.assertEqual(code, 1)
        self.assertIn('is a release payload', err)
        self.assertIn('prepare-session-bundle.py --payload', err)
        self.assertFalse((self.dir / 'from-payload').exists())

    def test_it_refuses_a_bundle_that_is_already_a_release(self):
        self.assertBuilt(self.build())
        members = dosplex.read_archive_members((self.dir / 'release' / (self.NAME + '.tar.gz')).read_bytes())
        again = self.dir / 'again'
        for name, data in members.items():
            if name.startswith(self.NAME + '/bundle/'):
                target = again / name[len(self.NAME + '/bundle/'):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        code, out, err = self.run_release('build', again, '--out', self.dir / 'again-out', '--pin-key', self.public)
        self.assertEqual(code, 1)
        self.assertIn('already has a release entry', err)

    def test_an_output_directory_that_is_not_empty_is_left_alone(self):
        (self.dir / 'release').mkdir()
        (self.dir / 'release' / 'keep').write_text('x')
        code, out, err = self.build()
        self.assertEqual(code, 1)
        self.assertIn('is not empty', err)
        self.assertEqual(self.files(), ['keep'])
        (self.dir / 'release' / 'keep').unlink()
        self.assertBuilt(self.build())                      # an empty one is fine

    def test_signing_needs_minisign_a_key_file_and_a_pinned_key_and_leaves_nothing_behind(self):
        argv = ('build', self.bundle, '--pin-key', self.public)
        code, out, err = self.run_release(*argv, '--out', self.dir / 'r1', '--sign-with', self.secret, '--minisign', 'no-such-minisign-here')
        self.assertEqual(code, 1)
        self.assertIn('no-such-minisign-here is not on PATH', err)
        code, out, err = self.run_release(*argv, '--out', self.dir / 'r2', '--sign-with', self.dir / 'nothing.key')
        self.assertEqual(code, 1)
        self.assertIn('does not exist', err)
        code, out, err = self.run_release('build', self.bundle, '--out', self.dir / 'r3', '--sign-with', self.secret,
                                          '--program', self.unpinned_program())
        self.assertEqual(code, 1)
        self.assertIn('pins no release key', err)
        code, out, err = self.build(out='r4', FAKE_MINISIGN_FAIL='1')
        self.assertEqual(code, 1)
        self.assertIn('did not sign', err)
        for name in ('r1', 'r2', 'r3', 'r4'):
            self.assertFalse((self.dir / name).exists(), name)
        self.assertEqual([p.name for p in self.dir.glob('.release-*')], [])

    def test_a_signature_by_a_key_the_program_does_not_pin_is_caught_before_anything_is_published(self):
        other = OnlyForTestsKey()
        (self.dir / 'other.key').write_text(other.secret_file_text())
        code, out, err = self.run_release('build', self.bundle, '--out', self.dir / 'wrong', '--pin-key', self.public,
                                          '--sign-with', self.dir / 'other.key')
        self.assertEqual(code, 1)
        self.assertIn('does not verify with the program that ships in the release', err)
        self.assertIn(other.key_id, err)
        self.assertIn(self.key.key_id, err)
        self.assertFalse((self.dir / 'wrong').exists())
        self.assertEqual([p.name for p in self.dir.glob('.release-*')], [])


class ReleaseIndex(Workspace):
    def entries(self):
        entries = []
        for version, epoch in (('0.2.0-test', 100), ('0.3.0-test', 300), ('0.2.5-test', 200)):
            bundle = make_bundle(self.dir / ('bundle-' + version), version=version, epoch=epoch)
            self.assertBuilt(self.run_release('build', bundle, '--out', self.dir / ('out-' + version), '--pin-key',
                                              self.public, '--sign-with', self.secret))
            entries.append(self.dir / ('out-' + version) / ('dosplex-%s.index-entry.json' % version))
        return entries

    def test_the_index_lists_the_newest_release_first_and_is_signed_for_its_channel(self):
        entries = self.entries()
        code, out, err = self.run_release('index', *entries, '--out', self.dir / 'channel', '--channel', 'preview',
                                          '--pin-key', self.public, '--sign-with', self.secret, SOURCE_DATE_EPOCH=str(EPOCH))
        self.assertEqual(code, 0, out + err)
        raw = (self.dir / 'channel/dosplex-index.json').read_bytes()
        index = dosplex.parse_index(raw, 'file:///channel/dosplex-index.json')
        self.assertEqual((index.channel, [r['version'] for r in index.releases]),
                         ('preview', ['0.3.0-test', '0.2.5-test', '0.2.0-test']))
        self.assertEqual({r['signed_by'] for r in index.releases}, {self.key.key_id})        # read from the signatures
        self.assertEqual(json.loads(raw)['updated'], time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(EPOCH)))
        signed = dosplex.verify_signature(raw, (self.dir / 'channel/dosplex-index.json.minisig').read_text(), self.trusted(), 'index')
        self.assertEqual((signed.key_id, signed.subject), (self.key.key_id, 'preview'))
        self.assertEqual(self.files('channel'), ['dosplex-index.json', 'dosplex-index.json.minisig'])
        command = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(command[command.index('-t') + 1], 'dosplex index preview')

    def test_an_entry_that_dosplex_would_not_accept_is_refused_at_once(self):
        entries = self.entries()
        broken = json.loads(entries[0].read_text())
        broken['tarball']['sha256'] = 'nope'
        (self.dir / 'broken.json').write_text(json.dumps(broken))
        argv = ('--out', self.dir / 'c', '--channel', 'preview', '--pin-key', self.public)
        code, out, err = self.run_release('index', self.dir / 'broken.json', *argv)
        self.assertEqual(code, 1)
        self.assertIn('would not be acceptable to dosplex', err)
        self.assertEqual(self.run_release('index', entries[0], entries[0], *argv)[0], 1)         # one version twice
        self.assertEqual(self.run_release('index', entries[0], '--out', self.dir / 'c', '--channel', 'Not A Name',
                                          '--pin-key', self.public)[0], 1)
        self.assertEqual(self.run_release('index', self.dir / 'missing.json', *argv)[0], 1)
        self.assertFalse((self.dir / 'c').exists())

    def test_unsigned_it_says_how_to_sign_and_signing_needs_a_pinned_key(self):
        entries = self.entries()
        code, out, err = self.run_release('index', entries[0], '--out', self.dir / 'u', '--channel', 'preview',
                                          '--pin-key', self.public)
        self.assertEqual(code, 0, err)
        self.assertIn('minisign -S -s SECRET_KEY -m dosplex-index.json', out)
        self.assertIn('-t "dosplex index preview"', out)
        code, out, err = self.run_release('index', entries[0], '--out', self.dir / 's', '--channel', 'preview',
                                          '--sign-with', self.secret, '--program', self.unpinned_program())
        self.assertEqual(code, 1)
        self.assertIn('pins no release key', err)


class ReleaseVerify(Workspace):
    def setUp(self):
        super().setUp()
        self.assertBuilt(self.build())
        self.out = self.dir / 'release'

    def verify(self, directory=None):
        return self.run_release('verify', directory or self.out, '--pin-key', self.public)

    def test_a_good_release_directory_verifies_and_says_what_it_checked(self):
        code, out, err = self.verify()
        self.assertEqual(code, 0, out + err)
        self.assertIn('signed by key %s' % self.key.key_id, out)
        self.assertIn(self.NAME + '.tar.gz', out)

    def test_the_published_copies_have_to_match_the_archive(self):
        for suffix in ('.manifest.json', '.sbom.json', '.NOTICE'):
            with self.subTest(suffix):
                path = self.out / (self.NAME + suffix)
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                code, out, err = self.verify()
                self.assertEqual(code, 1, suffix)
                self.assertIn('differs', err)
                path.write_bytes(original)
        entry = self.out / (self.NAME + '.index-entry.json')
        original = entry.read_text()
        entry.write_text(original.replace('"sequence": %d' % EPOCH, '"sequence": 5'))
        code, out, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn('does not describe this archive', err)
        entry.write_text(original)
        self.assertEqual(self.verify()[0], 0)

    def test_a_changed_archive_a_missing_signature_and_another_key_fail(self):
        archive = self.out / (self.NAME + '.tar.gz')
        original = archive.read_bytes()
        members = dosplex.read_archive_members(original)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tampered:
            for name, data in members.items():
                data = data + b'#' if name.endswith('bin/dosplex-side-supervisor.py') else data
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tampered.addfile(info, io.BytesIO(data))
        archive.write_bytes(buffer.getvalue())
        code, out, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn('differs from the signed manifest', err)
        archive.write_bytes(original)
        signature = self.out / (self.NAME + '.manifest.json.minisig')
        text = signature.read_text()
        signature.unlink()
        self.assertEqual(self.verify()[0], 1)
        signature.write_text(text)
        other = OnlyForTestsKey()
        (self.dir / 'other.pub').write_text(other.public_file())
        code, out, err = self.run_release('verify', self.out, '--pin-key', self.dir / 'other.pub')
        self.assertEqual(code, 1)
        self.assertIn('does not trust', err)
        self.assertEqual(self.verify(self.dir / 'bundle')[0], 1)                  # nothing there to verify

    def test_an_index_in_the_directory_is_checked_too(self):
        entry = self.out / (self.NAME + '.index-entry.json')
        code, out, err = self.run_release('index', entry, '--out', self.dir / 'with-index', '--channel', 'preview',
                                          '--pin-key', self.public, '--sign-with', self.secret)
        self.assertEqual(code, 0, err)
        code, out, err = self.verify(self.dir / 'with-index')
        self.assertEqual(code, 0, out + err)
        self.assertIn('channel preview, 1 release(s)', out)
        index = self.dir / 'with-index/dosplex-index.json'
        index.write_bytes(index.read_bytes() + b' ')
        code, out, err = self.verify(self.dir / 'with-index')
        self.assertEqual(code, 1)
        self.assertIn('not valid', err)


class PinningKeys(Workspace):
    def test_pin_prints_the_lines_that_make_a_program_trust_the_key(self):
        code, out, err = self.run_release('pin', self.public)
        self.assertEqual(code, 0, err)
        original = (ROOT / 'tools/dosplex').read_text()
        block = out.split('\n', 1)[1]                                      # after the comment line
        pasted = release.KEY_BLOCK.sub(lambda match: match.group(1) + block + match.group(3), original)
        namespace = {'__name__': 'pinned_copy', '__file__': 'dosplex'}
        exec(compile(pasted, 'dosplex', 'exec'), namespace)
        self.assertEqual([key.id for key in namespace['pinned_keys']()], [self.key.key_id])

    def test_a_file_that_is_not_a_public_key_is_refused(self):
        (self.dir / 'junk').write_text('hello\n')
        code, out, err = self.run_release('pin', self.dir / 'junk')
        self.assertEqual(code, 1)
        self.assertIn('not a minisign public key', err)

    def test_the_repository_program_keeps_its_key_block_where_release_py_can_rewrite_it(self):
        text = (ROOT / 'tools/dosplex').read_text()
        self.assertEqual(len(release.KEY_BLOCK.findall(text)), 1)
        for pins, count in (([], 0), ([self.key.pin()], 1), ([self.key.pin(), OnlyForTestsKey().pin()], 2)):
            module, _ = release.load_program(ROOT / 'tools/dosplex', pins)
            self.assertEqual(len(module.pinned_keys()), count)
        for entry in dosplex.RELEASE_KEYS:                                   # whatever the owner pinned is well formed
            self.assertEqual(len(bytes.fromhex(entry['public'])), 32)
        dosplex.pinned_keys()


class ReleaseConstants(unittest.TestCase):
    def test_the_decision_marker_is_the_one_the_notices_generator_writes(self):
        self.assertIn(release.DECISION_MARKER, (ROOT / 'tools' / 'generate-notices.py').read_text(encoding='utf-8'))


class ReleaseProcessDocument(unittest.TestCase):
    """docs/release-process.md names commands and options that exist, links to files that exist and keeps the
    promises the program makes."""

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / 'docs' / 'release-process.md').read_text()

    def test_the_commands_and_options_it_names_exist(self):
        parser = release.build_parser()
        commands = parser._subparsers._group_actions[0].choices
        for name in ('build', 'index', 'verify', 'pin'):
            self.assertIn('release.py ' + name, self.page)
            self.assertIn(name, commands)
        build = commands['build'].format_help() + commands['index'].format_help()
        for option in ('--sign-with', '--pin-key', '--sequence', '--base-url', '--channel', '--out'):
            self.assertIn(option, self.page)
            self.assertIn(option, build)
        for option in ('--channel', 'DOSPLEX_CHANNEL'):
            self.assertIn(option, self.page)

    def test_the_signing_command_it_prints_is_the_one_release_py_prints(self):
        self.assertIn('minisign -S -s release.key -m dosplex-VERSION.manifest.json', self.page)
        self.assertIn('-t "dosplex manifest VERSION"', self.page)

    def test_it_covers_build_sign_publish_and_key_rotation(self):
        for heading in ('## Build', '## Sign offline', '## Publish', '## Keys', 'Replacing the key'):
            self.assertIn(heading, self.page)
        for statement in ('RELEASE_KEYS', 'minisign -G', 'bridge', 'no revocation list', 'sudo sh', 'SHA-256 of the `dosplex` program'):
            self.assertIn(statement, self.page)

    def test_its_links_resolve_and_it_names_nothing_private(self):
        for target in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', self.page):
            if not target.startswith('http'):
                self.assertTrue((ROOT / 'docs' / target).exists(), target)
        self.assertNotIn('private' + '/', self.page)

    def test_the_installation_page_links_the_sections_it_points_to(self):
        page = (ROOT / 'docs' / 'steamos-installation.md').read_text()
        for anchor in re.findall(r'\]\(#([^)]+)\)', page):
            heading = anchor.replace('-', ' ')
            self.assertTrue(any(line.lstrip('#').strip().lower() == heading for line in page.splitlines() if line.startswith('#')), anchor)


if __name__ == '__main__':
    unittest.main()
