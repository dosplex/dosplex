#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Assemble the release payload from the build products and write its SBOM.

Runs inside the release-build image after the private libraries, the compositor and Gamescope are built
(payload-build.sh). It copies the compositor, the private libraries, what is built from Gamescope (the Vulkan layer
and its manifest, made from the template of the source tree), the product scripts named in layout.txt and the license
texts into the payload, strips the binaries, fixes modes and timestamps, and writes sbom/dosplex-sbom.json. The
third-party notices of the payload are generated from that SBOM by tools/generate-notices.py (the rights holder
comes from legal/RIGHTS_HOLDER). The payload layout and the SBOM format are described in README.md;
tools/check-sbom.py checks the result.

Standard library only. Needs the tools of the image: strip, objdump, ldconfig and pacman.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys

OWN_LICENSE = 'AGPL-3.0-or-later'
EXECUTABLE_PREFIXES = ('bin/', 'libexec/', 'lib/')
# Data of these packages is compiled into the compositor (generated protocol headers): the license text ships.
COMPOSITOR_INCLUDES = ('wayland-protocols',)
# What the data of a package named under "embeds" in sources.json is, for the SBOM's note.
EMBEDDED_DATA = {'hwdata': 'the PNP ID table (display manufacturer names)',
                 'wayland-protocols': 'protocol code that wayland-scanner generates from its XML files'}
# The packages of the image whose license text is shipped (the file in /usr/share/licenses/<package>/).
DISTRIBUTION_TEXTS = {'wayland-protocols': 'COPYING'}
# The packages of the image that install no license text of their own. Their texts are kept in this recipe
# (texts/<package>-<upstream version>/) and are the files of that name in the package's source archive, which is
# named here so that the manifest and the notices say where the source of that exact version is. A package of
# another version than the one the texts belong to stops the build (LEG-01).
RECIPE_TEXTS = {'hwdata': {'version': '0.397', 'files': ('LICENSE', 'COPYING'),
                           'source_url': 'https://github.com/vcrhonek/hwdata/archive/refs/tags/v0.397.tar.gz',
                           'source_sha256': '09eee39e73a63ab27af651ab6afdd13d6e5c3485872f2cd406b35e4d80ffdb0b'}}
# Where the packages of the image keep their license texts.
DISTRIBUTION_LICENSES = Path('/usr/share/licenses')


def run(*argv, check=True):
    return subprocess.run(argv, check=check, capture_output=True, text=True, env=dict(os.environ, LC_ALL='C')).stdout


def load_tool(source, filename):
    """A script of tools/ in the repository export as a module (their names have dashes, so no import)."""
    spec = importlib.util.spec_from_file_location(filename[:-3].replace('-', '_'), source / 'tools' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_table(path, maximum):
    """The lines of layout.txt or helpers.txt: lists of whitespace-separated fields, comments left out."""
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if not 2 <= len(fields) <= maximum:
            raise SystemExit('%s:%d: expected "<payload path> <repository path>"%s' % (
                path, number, ' and pkg-config modules' if maximum > 2 else ''))
        rows.append(fields)
    return rows


def sha256_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Packages:
    """What the image's package manager knows about the build host: versions, licenses, who owns a library."""

    def __init__(self):
        self.cache = {}
        # soname -> path, per kind of file that needs it: ldconfig marks the 64-bit libraries "x86-64" and the
        # 32-bit ones (in /usr/lib32) only "libc6".
        self.libraries = {'x86_64': {}, 'i386': {}}
        for line in run('ldconfig', '-p').splitlines():
            found = re.match(r'\s*(\S+) \(([^)]*)\) => (\S+)', line)
            if found and 'x86-64' in found.group(2):
                self.libraries['x86_64'].setdefault(found.group(1), found.group(3))
            elif found and 'libc6' in found.group(2):
                self.libraries['i386'].setdefault(found.group(1), found.group(3))

    def info(self, name):
        """{'name', 'version', 'license'} of an installed package."""
        if name not in self.cache:
            fields = {}
            for line in run('pacman', '-Qi', name).splitlines():
                found = re.match(r'([A-Za-z][A-Za-z ]*?)\s*:\s(.*)', line)
                if found:
                    fields[found.group(1)] = found.group(2).strip()
            licenses = [item for item in re.split(r'\s{2,}', fields.get('Licenses', '')) if item]
            self.cache[name] = {'name': name, 'version': fields['Version'],
                                'license': ' AND '.join(licenses) if licenses else 'NOASSERTION'}
        return self.cache[name]

    def owner_of_library(self, soname, abi='x86_64'):
        path = self.libraries.get(abi, {}).get(soname)
        if path is None:
            raise SystemExit('%s is needed (by a %s file) but the image has no such library' % (soname, abi))
        found = re.search(r'is owned by (\S+) ', run('pacman', '-Qo', path))
        if not found:
            raise SystemExit('%s (%s) belongs to no package of the image' % (soname, path))
        return self.info(found.group(1))


def symbol_versions(path, prefix):
    """The versions of the symbols that `path` needs from a library with versioned symbols (GLIBC_, GLIBCXX_)."""
    output = run('objdump', '-T', str(path), check=False)
    return {tuple(int(part) for part in version.split('.'))
            for version in re.findall(prefix + r'_(\d+(?:\.\d+)*)', output)}


def glibc_versions(path):
    return symbol_versions(path, 'GLIBC')


def glibcxx_versions(path):
    return symbol_versions(path, 'GLIBCXX')


def version_text(version):
    return '.'.join(map(str, version))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--source', type=Path, required=True, help='the repository export')
    parser.add_argument('--unpacked', type=Path, required=True, help='the unpacked third-party sources')
    parser.add_argument('--deps', type=Path, required=True, help='prefix the private libraries are installed in')
    parser.add_argument('--stage', type=Path, required=True,
                        help='destdir of the compositor install; stage/gamescope/<payload path> holds what is built '
                        'from the Gamescope tree')
    parser.add_argument('--payload', type=Path, required=True, help='the payload directory to create')
    parser.add_argument('--version', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--glibc-floor', required=True, help='newest GLIBC_ symbol version allowed, e.g. 2.41')
    parser.add_argument('--glibcxx-floor', help='newest GLIBCXX_ symbol version allowed (libstdc++), e.g. 3.4.34; '
                        'given when the payload has C++ programs, and recorded in the SBOM')
    args = parser.parse_args()
    epoch = int(os.environ['SOURCE_DATE_EPOCH'])
    floor = tuple(int(part) for part in args.glibc_floor.split('.'))
    cxx_floor = tuple(int(part) for part in args.glibcxx_floor.split('.')) if args.glibcxx_floor else None

    checker = load_tool(args.source, 'check-sbom.py')
    notices = load_tool(args.source, 'generate-notices.py')
    recipe = args.source / 'packaging' / 'release-build'
    sources = json.loads((recipe / 'sources.json').read_text())['sources']
    layout = read_table(recipe / 'layout.txt', 2)
    helpers = read_table(recipe / 'helpers.txt', 16)
    packages = Packages()
    payload = args.payload
    if payload.exists() and any(payload.iterdir()):
        raise SystemExit('%s is not empty' % payload)

    records = {}        # payload path -> record without sha256, size, mode

    def place(relative, origin_path=None, data=None, **fields):
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin_path is not None:
            shutil.copyfile(origin_path, target)
        else:
            target.write_bytes(data)
        target.chmod(0o755 if relative.startswith(EXECUTABLE_PREFIXES) else 0o644)
        records[relative] = dict(path=relative, **fields)
        return target

    def patch_facts(source):
        """The patches applied to the source's tree before the build, each with its repository path and SHA-256,
        so that the manifest says what was built when that is not the archive as released."""
        return [{'path': 'packaging/release-build/' + item, 'sha256': sha256_of(recipe / item)}
                for item in source.get('patches', [])]

    def upstream_facts(source):
        """The fields of every record of a file (or license text) that comes from the source archive `source`."""
        facts = dict(name=source['name'], version=source['version'], origin='upstream', license=source['license'],
                     source_url=source['url'], source_sha256=source['sha256'])
        if source.get('patches'):
            facts['patches'] = patch_facts(source)
        return facts

    def source_root(source):
        """Where sources.py unpacked the source: its place in the tree of the host, else its own directory."""
        return args.unpacked / source.get('into', source['directory'].replace('{commit}', source.get('commit', '')))

    def place_license_texts(source):
        """Copy the license files of an unpacked source into licenses/ and return their payload paths."""
        names = []
        root = source_root(source)
        for item in source['license_files']:
            relative = 'licenses/%s-%s-%s' % (source['name'], source['version'], Path(item).name)
            if relative in records:
                raise SystemExit('%s: two license files of %s are called %s' % (relative, source['name'], Path(item).name))
            place(relative, root / item, kind='license', license_files=[], **upstream_facts(source))
            names.append(relative)
        return names

    def owned(source_path, kind, **extra):
        return dict(kind=kind, name='dosplex', version=args.version, origin='repository', source_path=source_path,
                    license=OWN_LICENSE, license_files=['licenses/dosplex-LICENSE'], **extra)

    def embedded_package(package, note):
        """An "includes" entry for data of a package of the image that is compiled into a file. The license text
        of the package is shipped, once, when DISTRIBUTION_TEXTS knows it."""
        info = packages.info(package)
        texts = []
        if package in DISTRIBUTION_TEXTS:
            relative = 'licenses/%s-%s-%s' % (package, info['version'], DISTRIBUTION_TEXTS[package])
            if relative not in records:
                place(relative, DISTRIBUTION_LICENSES / package / DISTRIBUTION_TEXTS[package], kind='license',
                      name=package, version=info['version'], origin='distribution',
                      package='%s %s' % (package, info['version']), license=info['license'], license_files=[])
            texts = [relative]
        source = {}
        if package in RECIPE_TEXTS:
            kept = RECIPE_TEXTS[package]
            if info['version'].rsplit('-', 1)[0] != kept['version']:
                raise SystemExit('the image has %s %s, and the license texts kept in texts/%s-%s belong to version %s: '
                                 'take the texts and the source archive of the new version (RECIPE_TEXTS)'
                                 % (package, info['version'], package, kept['version'], kept['version']))
            for name in kept['files']:
                relative = 'licenses/%s-%s-%s' % (package, info['version'], name)
                kept_path = 'texts/%s-%s/%s' % (package, kept['version'], name)
                if relative not in records:
                    place(relative, recipe / kept_path, kind='license', name=package, version=info['version'],
                          origin='repository', source_path='packaging/release-build/' + kept_path,
                          license=info['license'], license_files=[])
                texts.append(relative)
            source = {'source_url': kept['source_url'], 'source_sha256': kept['source_sha256']}
        return {'name': package, 'version': info['version'], 'license': info['license'], 'license_files': texts,
                'note': note, **source}

    def compiled_source(source):
        """An "includes" entry for code of a source archive that is compiled into a file of the payload."""
        entry = {'name': source['name'], 'version': source['version'], 'license': source['license'],
                 'license_files': texts_of[source['name']], 'note': source['note'],
                 'source_url': source['url'], 'source_sha256': source['sha256']}
        if source.get('patches'):
            entry['patches'] = patch_facts(source)
        return entry

    # The license texts of every source that has something in the payload, and which source is compiled into which
    # file (sources.json: "libraries", "products", "embedded_in").
    texts_of = {source['name']: place_license_texts(source) for source in sources
                if source.get('libraries') or source.get('products') or source.get('embedded_in')}
    products = {path: (source, facts) for source in sources for path, facts in source.get('products', {}).items()}
    compiled_in = {}
    for source in sources:
        for path in source.get('embedded_in', []):
            compiled_in.setdefault(path, []).append(source)

    # The compositor, built and stripped by meson install.
    includes = [embedded_package(name, 'generated protocol headers compiled into the binary')
                for name in COMPOSITOR_INCLUDES]
    includes += [compiled_source(source) for source in compiled_in.get('bin/dosplex-compositor', [])]
    place('bin/dosplex-compositor', args.stage / 'bin' / 'dosplex-compositor',
          **owned('compositor/', 'executable', includes=includes))

    # The private libraries and their license texts.
    for source in sources:
        if not source.get('libraries'):
            continue
        name, version = source['name'], source['version']
        upstream = upstream_facts(source)
        texts = texts_of[name]
        embedded = [embedded_package(package, '%s compiled into the library'
                                     % EMBEDDED_DATA.get(package, 'data of the package'))
                    for package in source.get('embeds', [])]
        for soname in source['libraries']:
            target = place('lib/' + soname, (args.deps / 'lib' / soname).resolve(), kind='library',
                           license_files=texts, includes=embedded, **upstream)
            subprocess.run(['strip', '--strip-unneeded', str(target)], check=True)

    def manifest_from_template(source, path, facts):
        """The Vulkan manifest of a layer: the template of the source tree with the family filled in, a
        library_path that is relative to the manifest, so that the payload can be installed anywhere, and the name
        and the disable variable that sources.json gives it."""
        library = posixpath.relpath(facts['library'], posixpath.dirname(path))
        text = ((source_root(source) / facts['template']).read_text()
                .replace('@family@', facts['family']).replace('@lib_dir@', posixpath.dirname(library)))
        if '@' in text:
            raise SystemExit('%s still has a placeholder after the substitution' % facts['template'])
        try:
            layer = json.loads(text)['layer']
        except (ValueError, KeyError, TypeError):
            raise SystemExit('%s does not make a layer manifest' % facts['template'])
        if layer.get('library_path') != library:
            raise SystemExit('%s: library_path is %r, expected %r' % (facts['template'], layer.get('library_path'),
                                                                      library))
        # A name and an off switch of Dosplex's own (sources.json "layer_name", "disable_variable"), so that the
        # side launcher can turn off the layer of the installed Gamescope without turning off this one (README.md,
        # "Launcher"). Each is the one JSON string of the template that it replaces, so the text keeps its form.
        replacements = []
        if 'layer_name' in facts:
            replacements.append((layer.get('name'), facts['layer_name']))
        if 'disable_variable' in facts:
            disable = layer.get('disable_environment')
            if not isinstance(disable, dict) or len(disable) != 1:
                raise SystemExit('%s: expected one disable_environment variable, found %r' % (facts['template'],
                                                                                               disable))
            replacements.append((next(iter(disable)), facts['disable_variable']))
        for old, new in replacements:
            quoted = json.dumps(old) if isinstance(old, str) else None
            if quoted is None or text.count(quoted) != 1:
                raise SystemExit('%s: %r is not a string that occurs once, so it cannot be replaced by %r' % (
                    facts['template'], old, new))
            text = text.replace(quoted, json.dumps(new))
        if replacements:
            layer = json.loads(text)['layer']
            if 'layer_name' in facts and layer.get('name') != facts['layer_name']:
                raise SystemExit('%s: the layer name did not become %r' % (facts['template'], facts['layer_name']))
            if 'disable_variable' in facts and list(layer['disable_environment']) != [facts['disable_variable']]:
                raise SystemExit('%s: the disable variable did not become %r' % (facts['template'],
                                                                                 facts['disable_variable']))
        return text.encode()

    # What is built from the Gamescope tree: the layer (binary, stripped) and its manifest (made from a template).
    for path, (source, facts) in sorted(products.items()):
        fields = dict(upstream_facts(source), license=facts['license'], license_files=texts_of[source['name']])
        if 'template' in facts:
            place(path, data=manifest_from_template(source, path, facts), kind=facts['kind'], **fields)
            continue
        noun = 'library' if facts['kind'] == 'library' else 'binary'
        embedded = [compiled_source(item) for item in compiled_in.get(path, [])]
        embedded += [embedded_package(package, '%s compiled into the %s'
                                      % (EMBEDDED_DATA.get(package, 'data of the package'), noun))
                     for package in source.get('embeds', [])]
        target = place(path, args.stage / 'gamescope' / path, kind=facts['kind'], includes=embedded, **fields)
        subprocess.run(['strip', '--strip-unneeded', str(target)], check=True)

    # C programs built by payload-build.sh from helpers.txt.
    for destination, repository_path, *_modules in helpers:
        built = args.stage / 'helpers' / destination.replace('/', '__')
        target = place(destination, built, **owned(repository_path, 'executable'))
        subprocess.run(['strip', '--strip-unneeded', str(target)], check=True)

    # The product scripts and the project's own license texts.
    for destination, repository_path in layout:
        origin = args.source / repository_path
        if not origin.is_file():
            raise SystemExit('layout.txt names %s, which the repository export does not have' % repository_path)
        kind = 'license' if destination.startswith('licenses/') else 'script'
        place(destination, origin, **owned(repository_path, kind))
    # The manual the panel's Help shows: one page per chapter of docs/manual, built here so that the payload holds
    # the manual of exactly this source tree.
    manual = args.source / 'docs/manual'
    if manual.is_dir():
        built_manual = args.stage / 'manual'
        subprocess.run([sys.executable, str(args.source / 'tools/build-manual.py'), '--source', str(manual),
                        '--out', str(built_manual)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for item in sorted(built_manual.iterdir()):
            if item.is_file():
                place('share/dosplex/manual/' + item.name, item, **owned('docs/manual', 'data'))
    # The release name, written once and read back by everything that needs it (prepare-session-bundle.py).
    place('share/dosplex/VERSION', data=(args.version + '\n').encode(),
          **owned('packaging/release-build/assemble.py', 'data'))

    def describe(relative):
        """The hash, size and mode of a placed file, into its record."""
        path = payload / relative
        record = records[relative]
        record['sha256'] = sha256_of(path)
        record['size'] = path.stat().st_size
        record['mode'] = '%04o' % (path.stat().st_mode & 0o777)

    # Facts about every file.
    glibc_max = (0,)
    glibcxx_max = (0,)
    elf = {}
    on_disk = sorted(records)
    for relative in on_disk:
        path = payload / relative
        info = checker.read_elf(path)
        if info is not None:
            elf[relative] = info
            versions = glibc_versions(path)
            if versions:
                glibc_max = max(glibc_max, max(versions))
            if versions and max(versions) > floor:
                raise SystemExit('%s needs GLIBC_%s, above the floor GLIBC_%s' % (
                    relative, version_text(max(versions)), args.glibc_floor))
            if cxx_floor:
                versions = glibcxx_versions(path)
                if versions:
                    glibcxx_max = max(glibcxx_max, max(versions))
                if versions and max(versions) > cxx_floor:
                    raise SystemExit('%s needs GLIBCXX_%s, above the floor GLIBCXX_%s' % (
                        relative, version_text(max(versions)), args.glibcxx_floor))
        describe(relative)

    bundled = set()         # (file name or soname, abi) of the libraries in lib/
    for relative, info in elf.items():
        if relative.startswith('lib/'):
            bundled |= {(relative.split('/', 1)[1], info['abi']), (info['soname'], info['abi'])}
    needed = {}             # (soname, abi) -> the files that need it
    for relative, info in elf.items():
        for soname in info['needed']:
            if (soname, info['abi']) not in bundled:
                needed.setdefault((soname, info['abi']), set()).add(relative)
    libraries = []
    for soname, abi in sorted(needed):
        owner = packages.owner_of_library(soname, abi)
        record = {'soname': soname, 'needed_by': sorted(needed[(soname, abi)]), 'provided_by': 'host',
                  'package': owner['name'], 'version': owner['version'], 'license': owner['license'],
                  'observed': 'build image'}
        if abi != checker.DEFAULT_ABI:
            record['abi'] = abi         # the libraries of 32-bit files are other files than those of 64-bit ones
        libraries.append(record)

    scripts = checker.python_files(on_disk)
    local = checker.local_module_names(scripts)
    modules = {}
    for relative in scripts:
        for name in checker.third_party_imports(payload / relative, local):
            modules.setdefault(name, set()).add(relative)
    python = [{'name': 'python3', 'kind': 'interpreter', 'used_by': scripts, 'provided_by': 'host', 'version': None}]
    python += [{'name': name, 'kind': 'module', 'used_by': sorted(users), 'provided_by': 'host', 'version': None}
               for name, users in sorted(modules.items())]

    installed = sorted(run('pacman', '-Q').splitlines())
    sbom = {
        'format': 'dosplex-sbom',
        'format_version': 1,
        'package': {'name': 'dosplex', 'version': args.version, 'commit': args.commit, 'source_date_epoch': epoch},
        'build': {
            'base_image': os.environ['DOSPLEX_BASE_IMAGE'],
            'package_snapshot': 'https://archive.archlinux.org/repos/' + os.environ['DOSPLEX_PACKAGE_SNAPSHOT'],
            'glibc_floor': args.glibc_floor,
            'glibc_max_required': version_text(glibc_max),
            'toolchain': {name: packages.info(name)['version'] for name in
                          ('gcc', 'binutils', 'glibc', 'linux-api-headers', 'meson', 'ninja', 'pkgconf', 'python')},
            'environment_sha256': hashlib.sha256(('\n'.join(installed) + '\n').encode()).hexdigest(),
        },
        'files': [records[relative] for relative in on_disk],
        'host_provided': {'libraries': libraries, 'python': python},
    }
    if cxx_floor:
        sbom['build'].update(glibcxx_floor=args.glibcxx_floor, glibcxx_max_required=version_text(glibcxx_max))
    # THIRD-PARTY-NOTICES is generated from the SBOM and is itself a file of the payload. It says nothing about its
    # own record, so it is made from the SBOM as it stands and then added to it with its own hash.
    try:
        rights_holder = notices.read_rights_holder(args.source / 'legal' / 'RIGHTS_HOLDER')
        place(notices.NOTICES_PATH, data=notices.generate(sbom, rights_holder).encode('utf-8'),
              **owned('tools/generate-notices.py', 'license'))
    except notices.NoticeError as error:
        raise SystemExit('the third-party notices cannot be generated: %s' % error)
    describe(notices.NOTICES_PATH)
    on_disk = sorted(records)
    sbom['files'] = [records[relative] for relative in on_disk]

    sbom_file = payload / checker.SBOM_PATH
    sbom_file.parent.mkdir(parents=True, exist_ok=True)
    sbom_file.write_text(json.dumps(sbom, indent=2, sort_keys=True) + '\n')
    sbom_file.chmod(0o644)

    # Directory modes and timestamps, so the tree does not depend on when or by whom it was made.
    for directory, dirnames, filenames in os.walk(payload):
        for name in dirnames:
            (Path(directory) / name).chmod(0o755)
    payload.chmod(0o755)
    for directory, dirnames, filenames in os.walk(payload):
        for name in dirnames + filenames:
            os.utime(Path(directory) / name, (epoch, epoch), follow_symlinks=False)
    os.utime(payload, (epoch, epoch))
    print('payload: %d files, %d host-provided libraries, highest GLIBC_%s%s' % (
        len(records), len(libraries), version_text(glibc_max),
        ', highest GLIBCXX_%s' % version_text(glibcxx_max) if cxx_floor else ''), file=sys.stderr)


if __name__ == '__main__':
    main()
