#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""packaging/release-build/make-bundle.py: the public bundle builder.

Covers product_files(), write_bundle(), the scope default of build_manifest(),
and the CLI error paths. The fake payload for
read_payload comes from tests/sbom-check-test.py (loaded via importlib).
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKE_BUNDLE = ROOT / 'packaging' / 'release-build' / 'make-bundle.py'
SBOM_TEST = ROOT / 'tests' / 'sbom-check-test.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load the sbom-check-test module to reuse FakePayload and make_elf.
sbom_mod = load('sbom_check_test', SBOM_TEST)
FakePayload = sbom_mod.FakePayload
make_elf = sbom_mod.make_elf
COMMIT = sbom_mod.COMMIT
VERSION = sbom_mod.VERSION

make_bundle = load('make_bundle_public', MAKE_BUNDLE)


def fake_payload_dir(temp_root):
    """Build a fake payload that read_payload can check, using the sbom-check-test
    FakePayload plus the files read_payload needs from the repository."""
    import shutil
    import hashlib

    payload = FakePayload(Path(temp_root) / 'payload')
    root = Path(temp_root)
    # Add the compositor source file that read_payload digests.
    compositor_src = 'compositor/dosplex-compositor.c'
    (root / compositor_src).parent.mkdir(parents=True, exist_ok=True)
    (root / compositor_src).write_text('int main(void){return 0}\n')
    # Add product source files listed in PRODUCT_SOURCES.
    for source_dir in make_bundle.PRODUCT_SOURCES:
        src = root / source_dir
        src.mkdir(parents=True, exist_ok=True)
        (src / 'dummy').write_text('x\n')
    # Add files that product_files() references so they exist.
    for dest, src_rel in make_bundle.product_files().items():
        src = root / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            src.write_text('x\n')
    return payload


class ProductFilesTests(unittest.TestCase):
    def test_product_files_matches_layout(self):
        """product_files() returns exactly the bin/, libexec/, tools/, sources/ lines of layout.txt."""
        layout = ROOT / 'packaging' / 'release-build' / 'layout.txt'
        expected = {}
        for line in layout.read_text(encoding='utf-8').splitlines():
            parts = line.split()
            if len(parts) == 2 and not line.startswith('#') \
               and parts[0].startswith(('bin/', 'libexec/', 'tools/', 'sources/')):
                expected[parts[0]] = parts[1]
        self.assertEqual(make_bundle.product_files(), expected)

    def test_product_files_sources_exist(self):
        """Every source in product_files() is an existing file."""
        for dest, src in make_bundle.product_files().items():
            self.assertTrue((ROOT / src).is_file(),
                            'source for %s does not exist: %s' % (dest, src))


class WriteBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dosplex-make-bundle-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Build a fake repository root with the files write_bundle needs.
        for subdir in ('compositor', 'guest', 'packaging/steamos'):
            (self.root / subdir).mkdir(parents=True, exist_ok=True)
        (self.root / 'compositor' / 'dosplex-compositor.c').write_text('int main(void){return 0}\n')
        (self.root / 'packaging' / 'steamos' / 'dosplex-start-client.py').write_text('#!/usr/bin/python3\n')
        # A small set of sources.
        self.sources = {
            'bin/dosplex-gamepad-arbiter.py': 'packaging/steamos/dosplex-gamepad-arbiter.py',
            'libexec/dosplex-session-manager.py': 'packaging/steamos/dosplex-session-manager.py',
            'LICENSE': 'LICENSE',
        }
        for name, src in self.sources.items():
            (self.root / src).parent.mkdir(parents=True, exist_ok=True)
            (self.root / src).write_text('file %s\n' % name)
        (self.root / 'LICENSE').write_text('AGPL\n')
        self.source_specs = dict(self.sources)

    def test_write_bundle_writes_files_with_modes(self):
        output = self.root / 'output'
        output.mkdir()
        manifest = make_bundle.write_bundle(
            output, {n: self.root / s for n, s in self.sources.items()},
            self.source_specs, 'a' * 40, '1.0-abcdef012',
            {'steamos': []}, 'release bundle')
        bundle = output / 'bundle'
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o700)
        for name in self.sources:
            target = bundle / name
            self.assertTrue(target.is_file(), 'missing %s' % name)
            expected_mode = 0o755 if name.startswith(('bin/', 'libexec/', 'tools/')) else 0o644
            self.assertEqual(target.stat().st_mode & 0o777, expected_mode,
                             'wrong mode for %s' % name)

    def test_write_bundle_manifest_digests_match(self):
        output = self.root / 'output'
        output.mkdir()
        manifest = make_bundle.write_bundle(
            output, {n: self.root / s for n, s in self.sources.items()},
            self.source_specs, 'a' * 40, '1.0-abcdef012',
            {'steamos': []}, 'release bundle')
        bundle = output / 'bundle'
        for name, info in manifest['files'].items():
            path = bundle / name
            self.assertTrue(path.is_file())
            import hashlib
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(info['sha256'], actual, 'digest mismatch for %s' % name)
            self.assertEqual(info['bytes'], path.stat().st_size, 'size mismatch for %s' % name)
            self.assertEqual(info['mode'], path.stat().st_mode & 0o777, 'mode mismatch for %s' % name)

    def test_write_bundle_scope(self):
        output = self.root / 'output'
        output.mkdir()
        manifest = make_bundle.write_bundle(
            output, {n: self.root / s for n, s in self.sources.items()},
            self.source_specs, 'a' * 40, '1.0-abcdef012',
            {'steamos': []}, 'custom scope text')
        self.assertEqual(manifest['scope'], 'custom scope text')

    def test_manifest_sha256_matches(self):
        output = self.root / 'output'
        output.mkdir()
        make_bundle.write_bundle(
            output, {n: self.root / s for n, s in self.sources.items()},
            self.source_specs, 'a' * 40, '1.0-abcdef012',
            {'steamos': []}, 'release bundle')
        import hashlib
        manifest_bytes = (output / 'bundle' / 'manifest.json').read_bytes()
        sha_file = (output / 'manifest.sha256').read_text(encoding='utf-8').strip()
        self.assertEqual(sha_file, hashlib.sha256(manifest_bytes).hexdigest())


class BuildManifestScopeTests(unittest.TestCase):
    def test_build_manifest_default_scope(self):
        manifest = make_bundle.build_manifest(
            'a' * 40, '1.0-abcdef012', {}, {}, '', {})
        self.assertEqual(manifest['scope'], 'release bundle')

    def test_build_manifest_custom_scope(self):
        manifest = make_bundle.build_manifest(
            'a' * 40, '1.0-abcdef012', {}, {}, '', {},
            scope='another scope')
        self.assertEqual(manifest['scope'], 'another scope')


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(MAKE_BUNDLE)] + list(args),
                              capture_output=True, text=True)

    def test_help_exits_0(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_extra_name_cannot_leave_the_bundle_folder(self):
        # The name becomes a path below the bundle; an absolute one, an empty part or a dot would write elsewhere.
        with tempfile.TemporaryDirectory(prefix='dosplex-make-bundle-') as name:
            extra = Path(name) / 'file'
            extra.write_text('x')
            for bad in ('/etc/dosplex-test', 'a//b', './a', 'a/./b', '', 'a/../b'):
                done = subprocess.run([sys.executable, str(MAKE_BUNDLE), '--payload', name, '--output',
                                       str(Path(name) / 'out'), '--tested-versions', str(extra),
                                       '--extra', '%s=%s' % (bad, extra)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 2, (bad, done.stderr))
                self.assertFalse((Path(name) / 'out').exists(), bad)

    def test_extra_with_dotdot_exits_2(self):
        result = self.run_cli('--payload', '/tmp/nonexistent', '--output', '/tmp/out',
                              '--tested-versions', '/tmp/tv.json',
                              '--extra', 'a/../b=/tmp/somefile')
        self.assertEqual(result.returncode, 2)

    def test_extra_missing_file_exits_2(self):
        result = self.run_cli('--payload', '/tmp/nonexistent', '--output', '/tmp/out',
                              '--tested-versions', '/tmp/tv.json',
                              '--extra', 'tools/extra=/nonexistent/file')
        self.assertEqual(result.returncode, 2)

    def test_commit_not_40_hex_exits_2(self):
        result = self.run_cli('--payload', '/tmp/nonexistent', '--output', '/tmp/out',
                              '--tested-versions', '/tmp/tv.json',
                              '--commit', 'abc123')
        self.assertEqual(result.returncode, 2)

    def test_output_exists_exits_2(self):
        import os
        outdir = Path(tempfile.mkdtemp(prefix='dosplex-bundle-exists-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(outdir))
        result = self.run_cli('--payload', '/tmp/nonexistent', '--output', str(outdir),
                              '--tested-versions', '/tmp/tv.json')
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
