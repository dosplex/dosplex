#!/usr/bin/env python3
# dosplex-test: tier=integration needs=git,compositor-build timeout=180 skips-ok="one check asks git whether the recipe is ignored; an exported tree is not a git checkout"
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The release payload recipe (packaging/release-build) and the pieces around it must stay consistent.

Fast and hermetic: no container is started and nothing is downloaded. The container build itself is a manual
check (packaging/release-build/README.md). Covered here:

  - the bundle builder reads its product files from layout.txt, under the same names;
  - sources.json and the Containerfile pin what the README says they pin;
  - the recipe is not in a git-ignored directory (.gitignore ignores anything called build/);
  - the bundle builder (make-bundle.py) accepts a payload that agrees with its SBOM, was built from this commit
    and carries the repository's scripts, and refuses every other one;
  - compositor/meson.build builds a release by default, accepts a debug build, and generates the version header.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / 'packaging' / 'release-build'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('make_bundle', ROOT / 'packaging' / 'release-build' / 'make-bundle.py')
PRODUCT = builder.product_files()
sbom_tests = load('sbom_check_test', ROOT / 'tests' / 'sbom-check-test.py')
check_sbom = sbom_tests.check_sbom


def layout():
    entries = []
    for line in (RECIPE / 'layout.txt').read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            destination, source = line.split()
            entries.append((destination, source))
    return entries


class LayoutTests(unittest.TestCase):
    def test_layout_names_the_product_files_the_bundle_builder_uses_under_the_same_names(self):
        product = dict(PRODUCT)
        self.assertTrue(all(source.startswith(builder.PRODUCT_SOURCES) for source in product.values()), product)
        in_layout = {destination: source for destination, source in layout()
                     if destination.startswith(('bin/', 'libexec/', 'tools/', 'sources/'))}
        self.assertEqual(in_layout, product)

    def test_every_layout_source_is_a_file_and_every_destination_is_listed_once(self):
        destinations = [destination for destination, _ in layout()]
        self.assertEqual(len(destinations), len(set(destinations)))
        for destination, source in layout():
            self.assertTrue((ROOT / source).is_file(), source)

    def test_the_project_license_texts_are_in_the_layout(self):
        self.assertIn(('licenses/dosplex-LICENSE', 'LICENSE'), layout())
        self.assertIn(('licenses/dosplex-NOTICE', 'NOTICE'), layout())

    def test_every_listed_c_helper_has_a_destination_a_source_in_the_export_and_a_name_the_stage_can_hold(self):
        for line in (RECIPE / 'helpers.txt').read_text().splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            destination, source, *_modules = line.split()
            self.assertTrue(destination.startswith(('bin/', 'libexec/')), destination)
            self.assertTrue((ROOT / source).is_file(), source)
            # payload-build.sh only exports these directories into the container.
            self.assertTrue(source.startswith(('compositor/', 'guest/', 'packaging/')), source)


class RecipeTests(unittest.TestCase):
    def test_sources_are_pinned_by_https_url_and_sha256(self):
        data = json.loads((RECIPE / 'sources.json').read_text())
        self.assertEqual(data['format'], 1)
        names = [source['name'] for source in data['sources']]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({'wlroots', 'libdisplay-info', 'libliftoff', 'gamescope', 'vkroots', 'glm', 'stb'}, set(names))
        sonames = []
        for source in data['sources']:
            with self.subTest(source=source['name']):
                self.assertTrue(source['url'].startswith('https://'))
                self.assertRegex(source['sha256'], r'[0-9a-f]{64}')
                self.assertTrue(source['license'] and source['license_files'])
                if 'commit' in source:
                    # A snapshot of a git commit (a submodule of Gamescope, or its release tag): the archive is
                    # the commit's, and the directory of a GitHub archive carries the commit.
                    self.assertRegex(source['commit'], r'[0-9a-f]{40}')
                    if 'into' in source:
                        self.assertIn(source['commit'], source['url'])
                        self.assertEqual(source['directory'], '%s-{commit}' % source['url'].split('/')[4])
                else:
                    self.assertEqual(source['directory'], '%s-%s' % (source['name'], source['version']))
                    self.assertIn(source['directory'], source['url'])
                    self.assertTrue(source['libraries'])
                sonames += source.get('libraries', [])
        self.assertEqual(len(sonames), len(set(sonames)))
        gamescope = next(source for source in data['sources'] if source['name'] == 'gamescope')
        # The layer comes from this tag; its subprojects are the commits the tag pins (.gitmodules and the wraps).
        self.assertEqual(gamescope['version'], '3.16.30')
        self.assertIn('/refs/tags/%s.tar.gz' % gamescope['version'], gamescope['url'])
        wlroots = next(source for source in data['sources'] if source['name'] == 'wlroots')
        self.assertRegex(wlroots['version'], r'0\.17\.\d+')   # compositor/meson.build requires >=0.17.1, <0.18

    def test_the_image_is_pinned_by_digest_to_a_dated_snapshot(self):
        text = (RECIPE / 'Containerfile').read_text()
        self.assertRegex(text, r'(?m)^ARG BASE_IMAGE=\S+@sha256:[0-9a-f]{64}$')
        self.assertRegex(text, r'(?m)^ARG SNAPSHOT=\d{4}/\d{2}/\d{2}$')
        self.assertIn('FROM ${BASE_IMAGE}', text)
        self.assertNotRegex(text, r'(?m)^FROM .*:latest')
        self.assertIn('archive.archlinux.org/repos/%s/', text)

    def test_the_readme_states_the_pins_the_recipe_uses(self):
        readme = (RECIPE / 'README.md').read_text()
        for source in json.loads((RECIPE / 'sources.json').read_text())['sources']:
            self.assertTrue(source['sha256'] in readme, 'the README does not state the SHA-256 of ' + source['name'])
        containerfile = (RECIPE / 'Containerfile').read_text()
        self.assertIn(re.search(r'@(sha256:[0-9a-f]{64})', containerfile).group(1), readme)
        self.assertIn('repos/' + re.search(r'ARG SNAPSHOT=(\S+)', containerfile).group(1), readme)

    def test_the_32_bit_layer_build_has_the_files_and_packages_it_names(self):
        script = (RECIPE / 'payload-build.sh').read_text()
        for name in ('cross-x86.txt', 'open-layer.c'):
            self.assertIn(name, script)
            self.assertTrue((RECIPE / name).is_file(), name)
        cross = (RECIPE / 'cross-x86.txt').read_text()
        self.assertIn("cpu_family = 'x86'", cross)
        self.assertEqual(cross.count("'-m32'"), 2)
        containerfile = (RECIPE / 'Containerfile').read_text()
        self.assertIn('[multilib]', containerfile)
        for package in ('lib32-glibc', 'lib32-gcc-libs', 'lib32-libx11', 'lib32-libxcb', 'lib32-wayland',
                        'lib32-vulkan-icd-loader'):
            self.assertIn(package, containerfile)
        products = next(source for source in json.loads((RECIPE / 'sources.json').read_text())['sources']
                        if source['name'] == 'gamescope')['products']
        self.assertEqual(sorted(products), sorted(path for path in builder.BUILT_FILES))

    def test_the_recipe_is_not_in_an_ignored_directory(self):
        if shutil.which('git') is None or not (ROOT / '.git').exists():
            self.skipTest('not a git checkout')
        for item in sorted(RECIPE.iterdir()):
            if item.name == '__pycache__':
                continue
            result = subprocess.run(['git', 'check-ignore', '-q', str(item.relative_to(ROOT))], cwd=ROOT)
            self.assertEqual(result.returncode, 1, '%s is ignored by .gitignore' % item.relative_to(ROOT))

    def test_the_scripts_are_executable_and_parse(self):
        for name in ('build.sh', 'payload-build.sh', 'payload-digest.sh', 'sources.py', 'assemble.py',
                     'check-steamos-parity.py'):
            self.assertTrue((RECIPE / name).stat().st_mode & 0o111, name)
        for name in ('build.sh', 'payload-build.sh', 'payload-digest.sh'):
            self.assertEqual(subprocess.run(['bash', '-n', str(RECIPE / name)]).returncode, 0, name)
        self.assertEqual(subprocess.run(['sh', '-n', str(RECIPE / 'entrypoint.sh')]).returncode, 0)
        for name in ('sources.py', 'assemble.py', 'check-steamos-parity.py'):
            compile((RECIPE / name).read_text(), name, 'exec')


COMMIT = 'c' * 40


class PayloadTests(unittest.TestCase):
    """prepare-session-bundle.py --payload, through read_payload(): what it accepts and what it refuses."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-payload-test-')
        self.addCleanup(temporary.cleanup)
        self.payload = sbom_tests.FakePayload(Path(temporary.name) / 'payload')
        self.scripts = {}
        for name, source in PRODUCT.items():
            if source.startswith(builder.PRODUCT_SOURCES):
                self.scripts[name] = source
                self.payload.add(name, (ROOT / source).read_bytes(), 0o755, kind='script', name='dosplex',
                                 origin='repository', source_path=source, license='AGPL-3.0-or-later',
                                 license_files=['licenses/dosplex-LICENSE'])
        # What the payload builds from Gamescope: the layers (64-bit and 32-bit) and their manifests, which no file
        # of the repository equals.
        layer, manifest, layer32, manifest32 = builder.BUILT_FILES
        upstream = dict(name='gamescope', version='3.16.30', origin='upstream', source_url=sbom_tests.WLROOTS_URL,
                        source_sha256='b' * 64, license='BSD-2-Clause',
                        license_files=['licenses/wlroots-0.17.4-LICENSE'])
        self.payload.add(layer, sbom_tests.make_elf(['libc.so.6'], soname='libVkLayer_FROG_gamescope_wsi_x86_64.so'),
                         0o755, kind='library', **upstream)
        self.payload.add(layer32, sbom_tests.make_elf([], soname='libVkLayer_FROG_gamescope_wsi_x86.so', bits=32),
                         0o755, kind='library', **upstream)
        for path, name, library in ((manifest, 'x86_64', layer), (manifest32, 'x86', layer32)):
            self.payload.add(path, json.dumps({'file_format_version': '1.0.0', 'layer': {
                'name': 'VK_LAYER_DOSPLEX_gamescope_wsi_' + name, 'library_path': '../../../' + library,
                'disable_environment': {'DOSPLEX_DISABLE_GAMESCOPE_WSI': '1'}}}).encode(),
                0o644, kind='data', **upstream)
        # What the payload compiles from helpers.txt (the controller relay): a program with no file of the repository
        # to compare it with.
        self.helpers = []
        for line in (RECIPE / 'helpers.txt').read_text().splitlines():
            fields = line.split()
            if fields and not line.startswith('#'):
                self.helpers.append(fields[0])
                self.payload.add(fields[0], sbom_tests.make_elf(['libc.so.6']), 0o755, kind='executable',
                                 name='dosplex', origin='repository', source_path=fields[1],
                                 license='AGPL-3.0-or-later', license_files=['licenses/dosplex-LICENSE'])
        for item in self.payload.sbom['host_provided']['libraries']:
            if item['soname'] == 'libc.so.6':
                item['needed_by'] += [layer, *self.helpers]
        self.payload.sbom['package']['commit'] = COMMIT
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]
        # The scripts import host modules; the SBOM has to list them like the real one does.
        imports = {}
        for path in check_sbom.python_files(self.payload.files):
            for module in check_sbom.third_party_imports(self.payload.root / path,
                                                         check_sbom.local_module_names(
                                                             check_sbom.python_files(self.payload.files))):
                imports.setdefault(module, set()).add(path)
        python = self.payload.sbom['host_provided']['python']
        python[:] = [item for item in python if item['name'] == 'python3']
        python[0]['used_by'] = check_sbom.python_files(self.payload.files)
        python += [{'name': module, 'kind': 'module', 'used_by': sorted(users), 'provided_by': 'host',
                    'version': None} for module, users in sorted(imports.items())]
        self.payload.save()

    def read(self, commit=COMMIT):
        return builder.read_payload(self.payload.root, commit, PRODUCT)

    def test_a_payload_without_the_programs_it_should_have_built_is_refused(self):
        self.assertTrue(self.helpers, 'helpers.txt names no program; the controller relay should be there')
        (self.payload.root / self.helpers[0]).unlink()
        with self.assertRaises(ValueError):
            self.read()

    def test_a_payload_that_agrees_with_everything_is_accepted(self):
        version, taken = self.read()
        self.assertEqual(version, sbom_tests.VERSION)
        for name in ('bin/dosplex-compositor', 'lib/libwlroots.so.12', 'licenses/wlroots-0.17.4-LICENSE',
                     'sbom/dosplex-sbom.json', 'share/dosplex/VERSION', *builder.BUILT_FILES, *self.scripts,
                     *self.helpers):
            self.assertIn(name, taken)
            self.assertTrue(taken[name].is_file(), name)
        self.assertEqual(taken['bin/dosplex-compositor'], (self.payload.root / 'bin/dosplex-compositor').resolve())

    def test_the_gamescope_layers_and_their_manifests_are_in_the_bundle_by_the_names_the_launcher_looks_for(self):
        layer, manifest, layer32, manifest32 = builder.BUILT_FILES
        self.assertEqual(layer, 'lib/libVkLayer_FROG_gamescope_wsi_x86_64.so')
        self.assertEqual(manifest, 'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json')
        self.assertEqual(layer32, 'lib/libVkLayer_FROG_gamescope_wsi_x86.so')
        self.assertEqual(manifest32, 'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86.json')
        launcher = (ROOT / 'guest' / 'dosplex-user-side-session.sh').read_text()
        for name in (layer, manifest, layer32, manifest32):
            self.assertIn(name, launcher)
        # Both manifests are in the one directory that the launcher puts in front.
        self.assertEqual(Path(manifest).parent, Path(manifest32).parent)
        self.assertEqual(PRODUCT['libexec/dosplex-user-side-session.sh'], 'guest/dosplex-user-side-session.sh')
        version, taken = self.read()
        self.assertEqual(taken[layer], (self.payload.root / layer).resolve())

    def test_the_launcher_looks_for_each_manifest_with_its_library_and_the_name_the_build_gives_it(self):
        # The launcher turns off the installed Gamescope's layer only when every bundled manifest is there with its
        # library and answers to DosPlex's name and switch, so its table has to be what sources.json builds.
        products = next(source for source in json.loads((RECIPE / 'sources.json').read_text())['sources']
                        if source['name'] == 'gamescope')['products']
        built = sorted((path, facts['library'], facts['layer_name']) for path, facts in products.items()
                       if 'template' in facts)
        self.assertEqual(len(built), 2)
        for path, library, name in built:
            self.assertIn(library, products, 'the manifest %s names a library the payload does not build' % path)
            self.assertEqual(products[library]['kind'], 'library')
        self.assertEqual({facts['disable_variable'] for facts in products.values() if 'template' in facts},
                         {'DOSPLEX_DISABLE_GAMESCOPE_WSI'})
        launcher = (ROOT / 'guest' / 'dosplex-user-side-session.sh').read_text()
        table = re.search(r'local -a layers=\(\n(.*?)\n  \)', launcher, re.S).group(1)
        rows = sorted(tuple(line.strip().strip('"').split('|')) for line in table.splitlines())
        self.assertEqual(rows, built)
        self.assertIn("'\"DOSPLEX_DISABLE_GAMESCOPE_WSI\"'", launcher)
        # Every library the payload builds has its manifest, and the other way round.
        libraries = sorted(path for path, facts in products.items() if facts['kind'] == 'library')
        self.assertEqual(libraries, sorted(library for _, library, _ in built))

    def test_a_payload_without_the_layer_is_refused_and_told_to_be_built_again(self):
        for name in builder.BUILT_FILES:
            with self.subTest(missing=name):
                payload = sbom_tests.FakePayload(self.payload.root.parent / ('without-' + name.replace('/', '-')))
                payload.sbom = json.loads(json.dumps(self.payload.sbom))
                for other, (data, mode, record) in self.payload.files.items():
                    if other != name and other not in payload.files:
                        payload.add(other, data, mode, **record)
                payload.sbom['files'] = [payload.record(path) for path in sorted(payload.files)]
                for item in payload.sbom['host_provided']['libraries']:
                    item['needed_by'] = [path for path in item['needed_by'] if path != name]
                payload.save()
                # Without the library the SBOM check already refuses the manifest that leads to it; without the
                # manifest the builder says that the payload is older than the layer.
                with self.assertRaisesRegex(ValueError, '(lacks|leads to) %s' % re.escape(name)):
                    builder.read_payload(payload.root, COMMIT, PRODUCT)

    def test_a_payload_built_from_another_commit_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'built from commit c+ but this checkout is at d+'):
            self.read('d' * 40)

    def test_a_payload_that_disagrees_with_its_sbom_is_refused(self):
        (self.payload.root / 'lib/libwlroots.so.12').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'does not agree with its SBOM'):
            self.read()

    def test_a_script_that_differs_from_the_repository_is_refused(self):
        name = sorted(self.scripts)[0]
        # Rewrite the file and its SBOM record together, so that the payload still agrees with its SBOM.
        self.payload.add(name, b'# changed\n', 0o755, **self.payload.files[name][2])
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]
        self.payload.save()
        self.assertEqual(check_sbom.check(self.payload.root), [])
        with self.assertRaisesRegex(ValueError, 'differs from'):
            self.read()

    def test_a_payload_without_a_product_script_is_refused(self):
        name = sorted(self.scripts)[0]
        (self.payload.root / name).unlink()
        self.payload.sbom['files'] = [item for item in self.payload.sbom['files'] if item['path'] != name]
        self.payload.save()
        with self.assertRaises(ValueError):
            self.read()

    def test_the_licence_and_the_notices_of_the_payload_are_also_at_the_top_of_the_bundle(self):
        self.payload.add('licenses/dosplex-THIRD-PARTY-NOTICES.md', b'# Third-party notices of this payload\n', 0o644,
                         kind='license', name='dosplex', origin='repository', source_path='tools/generate-notices.py',
                         license='AGPL-3.0-or-later', license_files=['licenses/dosplex-LICENSE'])
        self.payload.sbom['files'] = [self.payload.record(path) for path in sorted(self.payload.files)]
        self.payload.save()
        _, taken = self.read()
        self.assertEqual(taken['THIRD-PARTY-NOTICES.md'], taken['licenses/dosplex-THIRD-PARTY-NOTICES.md'])
        for name, inside in builder.TOP_LEVEL_TEXTS.items():
            if inside in taken:
                self.assertEqual(taken[name], taken[inside], name)

    def test_a_directory_that_is_not_a_payload_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'not a directory'):
            builder.read_payload(self.payload.root / 'nowhere', COMMIT, PRODUCT)
        empty = self.payload.root.parent / 'empty'
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, 'not a release payload'):
            builder.read_payload(empty, COMMIT, PRODUCT)

    def test_a_release_name_the_installer_would_refuse_is_refused(self):
        (self.payload.root / 'share/dosplex/VERSION').write_text('../x\n')
        with self.assertRaises(ValueError):
            self.read()

    def test_the_builder_says_how_it_is_used(self):
        result = subprocess.run([sys.executable, str(ROOT / 'packaging/release-build/make-bundle.py'), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ' '.join(result.stdout.split())
        for option in ('--payload DIR', '--output DIR', '--tested-versions FILE', '--extra NAME=PATH', '--commit HASH'):
            self.assertIn(option, text)


@unittest.skipUnless(shutil.which('meson') and subprocess.run(['pkg-config', '--exists', 'wlroots']).returncode == 0,
                     'meson and wlroots are needed to configure the compositor')
class MesonTests(unittest.TestCase):
    def configure(self, *options, source=None):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-meson-test-')
        self.addCleanup(temporary.cleanup)
        build = Path(temporary.name) / 'build'
        result = subprocess.run(['meson', 'setup', str(build), str(source or ROOT / 'compositor'),
                                 '--wrap-mode=nodownload', *options], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return build

    def option(self, build, name):
        result = subprocess.run(['meson', 'introspect', str(build), '--buildoptions'], capture_output=True, text=True)
        return next(item['value'] for item in json.loads(result.stdout) if item['name'] == name)

    def header(self, build):
        text = (build / 'dosplex-version.h').read_text()
        return dict(re.findall(r'#define (\w+) "([^"]*)"', text))

    def test_the_default_is_a_release_build_without_asserts(self):
        build = self.configure()
        self.assertEqual(self.option(build, 'buildtype'), 'release')
        self.assertEqual(self.option(build, 'b_ndebug'), 'if-release')

    def test_a_debug_build_is_still_one_option_away(self):
        build = self.configure('-Dbuildtype=debug')
        self.assertEqual(self.option(build, 'buildtype'), 'debug')

    def test_the_release_name_and_revision_can_be_given(self):
        header = self.header(self.configure('-Drelease_version=9.9.9-preview.1', '-Dgit_revision=abcdef012'))
        self.assertEqual(header, {'DOSPLEX_VERSION': '9.9.9-preview.1', 'DOSPLEX_PROJECT_VERSION': '0.1.0',
                                  'DOSPLEX_GIT_REVISION': 'abcdef012'})

    def test_the_default_release_name_is_the_project_version_and_the_revision(self):
        header = self.header(self.configure('-Dgit_revision=abcdef012'))
        self.assertEqual(header['DOSPLEX_VERSION'], '0.1.0-abcdef012')

    def test_a_tree_without_git_history_says_unknown(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-meson-copy-') as copy:
            for name in ('meson.build', 'meson_options.txt', 'dosplex-compositor.c'):
                shutil.copy(ROOT / 'compositor' / name, copy)
            header = self.header(self.configure(source=copy))
        self.assertEqual(header['DOSPLEX_GIT_REVISION'], 'unknown')
        self.assertEqual(header['DOSPLEX_VERSION'], '0.1.0-unknown')


if __name__ == '__main__':
    unittest.main()
