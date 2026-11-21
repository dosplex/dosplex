#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Turn a built payload into a release bundle: copy its files, write manifest.json and manifest.sha256.

The payload is made by packaging/release-build/build.sh in a container. This program takes the payload
directory, the tested-versions file and the product sources, and writes a bundle directory containing
the payload files, manifest.json and manifest.sha256.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The bundle files whose source is under these directories are the product; the payload provides them.
PRODUCT_SOURCES = ('guest/', 'packaging/steamos/')
# Files that the payload builds from third-party sources (Gamescope) and that no file of this checkout can be
# compared with. A payload without them is older than the layer: it is refused and has to be built again.
BUILT_FILES = ('lib/libVkLayer_FROG_gamescope_wsi_x86_64.so',
               'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json',
               'lib/libVkLayer_FROG_gamescope_wsi_x86.so',
               'share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86.json')


TOP_LEVEL_TEXTS = {'LICENSE': 'licenses/dosplex-LICENSE', 'NOTICE': 'licenses/dosplex-NOTICE',
                   'THIRD-PARTY-NOTICES.md': 'licenses/dosplex-THIRD-PARTY-NOTICES.md'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}')
# What a release records as tested (ADR-009): the file beside this program.
DEFAULT_TESTED_VERSIONS = Path(__file__).with_name('tested-versions.json')


def release_version(commit, override=None):
    """The release name: --version as given, or the meson project version plus the start of the commit."""
    if override is None:
        project = re.search(r"^\s*version:\s*'([^']+)'", (ROOT / 'compositor/meson.build').read_text(), re.M)
        override = '%s-%s' % (project.group(1) if project else '0', commit[:9])
    if not VERSION.fullmatch(override):
        raise ValueError('the version must be letters, digits and . _ + - (at most 64), starting with a letter or digit')
    return override


def _strings(value, what):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and 0 < len(v) <= 128 and v.isprintable()
                                              for v in value):
        raise ValueError('tested versions: %s must be a list of short strings' % what)
    return sorted(set(value))


def load_tested_versions(path):
    """The tested set in the canonical form the manifest carries (sorted, no duplicates)."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('tested versions: expected an object')
    entries = data.get('steamos') or []
    if not isinstance(entries, list):
        raise ValueError('tested versions: steamos must be a list')
    steamos = set()
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('version_id'), str) or not entry['version_id']
                or not (entry.get('build_id') is None or (isinstance(entry['build_id'], str) and entry['build_id']))
                or set(entry) - {'version_id', 'build_id'}):
            raise ValueError('tested versions: each steamos entry needs a version_id and may have a build_id')
        steamos.add((entry['version_id'], entry.get('build_id')))
    return {'steamos': [dict(version_id=v, **({'build_id': b} if b else {})) for v, b in
                        sorted(steamos, key=lambda item: (item[0], item[1] or ''))],
            'gamescope': _strings(data.get('gamescope'), 'gamescope'),
            'steam_client': _strings(data.get('steam_client'), 'steam_client')}


def build_manifest(commit, version, tested_versions, source_hashes, compositor_source_sha256, files,
                   scope='release bundle'):
    """The manifest as a dict, in the order it is written. `files` is name -> {sha256, mode, bytes}."""
    return {'commit': commit, 'version': version, 'scope': scope,
            'tested_versions': tested_versions,
            'source_sha256': source_hashes,
            'compositor_source_sha256': compositor_source_sha256, 'files': files}


def read_payload(payload, commit, source_specs):
    """Check a release payload and say what the bundle takes from it: (release name, {bundle name: path}).

    The payload (packaging/release-build/build.sh) has to agree with its own SBOM, to have been built from
    `commit`, and its product scripts have to equal the repository's. Otherwise ValueError says what to do."""
    payload = Path(payload).resolve()
    if not payload.is_dir():
        raise ValueError('the payload %s is not a directory' % payload)
    try:
        version = (payload / 'share/dosplex/VERSION').read_text().strip()
    except OSError as error:
        raise ValueError('%s is not a release payload: %s (build one with packaging/release-build/build.sh)'
                         % (payload, error))
    release_version('0' * 40, version)
    spec = importlib.util.spec_from_file_location('check_sbom', ROOT / 'tools/check-sbom.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    problems = checker.check(payload)
    if problems:
        raise ValueError('the payload does not agree with its SBOM (python3 tools/check-sbom.py %s): %s%s'
                         % (payload, '; '.join(problems[:3]), ' ...' if len(problems) > 3 else ''))
    built_from = json.loads((payload / 'sbom/dosplex-sbom.json').read_text())['package']['commit']
    if built_from != commit:
        raise ValueError('the payload was built from commit %s but this checkout is at %s; rebuild the payload '
                         '(packaging/release-build/build.sh) or check out that commit' % (built_from, commit))
    taken = {'bin/dosplex-compositor': payload / 'bin/dosplex-compositor',
             'sbom/dosplex-sbom.json': payload / 'sbom/dosplex-sbom.json'}
    for directory in ('lib', 'licenses', 'share'):
        for item in sorted((payload / directory).rglob('*')):
            if item.is_file():
                taken[item.relative_to(payload).as_posix()] = item
    # The three texts a person looks for at the top of a release, and that the release tool publishes beside the
    # archive: the project's licence and notice, and the third-party notices generated from this payload's SBOM
    # (they describe exactly what this release holds, which a file of the repository cannot).
    for name, inside in TOP_LEVEL_TEXTS.items():
        if inside in taken:
            taken[name] = taken[inside]
    for name in BUILT_FILES:
        if name not in taken:
            raise ValueError('the payload lacks %s; it was built before the Gamescope layer was part of it '
                             '(build it again with packaging/release-build/build.sh)' % name)
    # The programs the payload compiles from helpers.txt (the controller relay): there is no file of the checkout
    # to compare them with, their record in the SBOM (checked above) names their source.
    for name in helper_files():
        if not (payload / name).is_file():
            raise ValueError('the payload lacks %s; it was built before helpers.txt named it (build it again with '
                             'packaging/release-build/build.sh)' % name)
        taken[name] = payload / name
    for name, source in source_specs.items():
        if not source.startswith(PRODUCT_SOURCES):
            continue
        item = payload / name
        if not item.is_file():
            raise ValueError('the payload lacks %s' % name)
        if digest(item) != digest(ROOT / source):
            raise ValueError('%s in the payload differs from %s in this checkout' % (name, source))
        taken[name] = item
    for name, item in taken.items():
        if not item.is_file():
            raise ValueError('the payload lacks %s' % name)
    return version, taken


def helper_files(root=ROOT):
    """The destinations of the programs helpers.txt has the payload build: the first field of each line."""
    names = []
    for line in (root / 'packaging' / 'release-build' / 'helpers.txt').read_text(encoding='utf-8').splitlines():
        fields = line.split()
        if fields and not line.lstrip().startswith('#'):
            names.append(fields[0])
    return names


def product_files(root=ROOT):
    """Read layout.txt and return {destination: source} for lines with exactly two fields
    whose destination starts with bin/, libexec/, tools/ or sources/."""
    layout = (root / 'packaging' / 'release-build' / 'layout.txt').read_text(encoding='utf-8')
    result = {}
    for line in layout.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        dest, src = parts
        if dest.startswith(('bin/', 'libexec/', 'tools/', 'sources/')):
            result[dest] = src
    return result


def write_bundle(output, sources, source_specs, commit, version, tested_versions, scope):
    """Copy the payload files into output/bundle and write manifest.json and manifest.sha256.

    Returns the manifest dict. Does not print.
    """
    output = Path(output)
    bundle = output / 'bundle'
    bundle.mkdir(mode=0o700)

    # Copy each source file
    for name, source in sources.items():
        target = bundle / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if name.startswith(('bin/', 'libexec/', 'tools/')) else 0o644)

    # Build the files dict for the manifest
    files = {str(p.relative_to(bundle)): {'sha256': digest(p),
                                          'mode': p.stat().st_mode & 0o777, 'bytes': p.stat().st_size}
             for p in sorted(bundle.rglob('*')) if p.is_file()}

    # Build source_sha256: digest of ROOT/source for every (name, source) in source_specs
    source_sha256 = {name: digest(ROOT / source) for name, source in source_specs.items()}

    compositor_source_sha256 = digest(ROOT / 'compositor/dosplex-compositor.c')

    manifest = build_manifest(commit, version, tested_versions, source_sha256, compositor_source_sha256,
                              files, scope=scope)

    (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'manifest.sha256').write_text(digest(bundle / 'manifest.json') + '\n')

    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='make-bundle.py',
        description='Turn a built payload into a release bundle.')
    parser.add_argument('--payload', required=True, metavar='DIR', help='the payload directory')
    parser.add_argument('--output', required=True, metavar='DIR', help='the output directory (must not exist yet)')
    parser.add_argument('--tested-versions', default=DEFAULT_TESTED_VERSIONS, metavar='FILE',
                        help='the tested SteamOS, Gamescope and Steam client versions (default: tested-versions.json '
                        'beside this program)')
    parser.add_argument('--extra', action='append', default=[], metavar='NAME=PATH',
                        help='an extra file to add to the bundle (NAME is a relative path, PATH an existing file)')
    parser.add_argument('--commit', metavar='HASH',
                        help='the commit hash (40 hex digits); required when the tree has no git')
    args = parser.parse_args(argv)

    # Validate --extra arguments first (before anything else) so that bad
    # --extra args exit 2 via parser.error
    extra_map = {}
    for extra in args.extra:
        if '=' not in extra:
            parser.error('--extra must be NAME=PATH: %s' % extra)
        name, path = extra.split('=', 1)
        parts = name.split('/')
        if not name or name.startswith('/') or any(part in ('', '.', '..') for part in parts):
            parser.error('--extra name must be a plain relative path inside the bundle: %s' % name)
        extra_path = Path(path)
        if not extra_path.is_file():
            parser.error('--extra file does not exist: %s' % path)
        extra_map[name] = extra_path

    # Check --output does not exist yet
    output = Path(args.output)
    if output.exists():
        parser.error('--output directory already exists: %s' % args.output)

    # Validate --commit format first (before git checks) so that a bad
    # --commit exits 2 via parser.error
    if args.commit and not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('--commit must be 40 hex digits')

    # Get the commit
    commit = None
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(ROOT),
                                capture_output=True, text=True)
        if result.returncode == 0:
            commit = result.stdout.strip()
            # Check for uncommitted changes under compositor, guest, packaging
            dirty = subprocess.run(['git', 'status', '--porcelain', 'compositor', 'guest', 'packaging'],
                                   cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
            if dirty:
                print('make-bundle: uncommitted changes under compositor, guest or packaging', file=sys.stderr)
                return 1
    except FileNotFoundError:
        pass

    if commit is None:
        # No git: --commit is required
        if not args.commit:
            parser.error('--commit is required when the tree has no git')
        commit = args.commit
    else:
        # Git present: if --commit was given, it must agree
        if args.commit and args.commit != commit:
            parser.error('--commit %s does not agree with git HEAD %s' % (args.commit, commit))

    # Read tested versions
    try:
        tested_versions = load_tested_versions(args.tested_versions)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print('make-bundle: %s' % exc, file=sys.stderr)
        return 1

    # Read the payload
    try:
        payload_version, payload_files = read_payload(args.payload, commit, product_files())
    except ValueError as exc:
        print('make-bundle: %s' % exc, file=sys.stderr)
        return 1

    missing = sorted(name for name in TOP_LEVEL_TEXTS if name not in payload_files)
    if missing:
        print('make-bundle: the payload lacks %s; a release is not made without its licence and notices'
              % ', '.join(TOP_LEVEL_TEXTS[name] for name in missing), file=sys.stderr)
        return 1

    # Merge --extra into payload_files (checking against bundle file names)
    for name, path in extra_map.items():
        if name in payload_files:
            parser.error('--extra name is already a bundle file: %s' % name)
        payload_files[name] = path

    # Determine the scope
    scope = 'release bundle with extra files' if args.extra else 'release bundle'

    # Write the bundle
    output.mkdir(parents=True)
    manifest = write_bundle(output, payload_files, product_files(), commit, payload_version,
                            tested_versions, scope)

    # Write build.log
    (output / 'build.log').write_text(
        'nothing was built here: release payload %s, version %s, commit %s\n'
        % (Path(args.payload).resolve(), payload_version, commit), encoding='utf-8')

    # Print the one-line JSON (same as the private script)
    print(json.dumps({'bundle': str(output / 'bundle'), 'commit': commit, 'version': payload_version,
                      'manifest_sha256': digest(output / 'bundle' / 'manifest.json'), 'deployed': False}))

    return 0


if __name__ == '__main__':
    sys.exit(main())
