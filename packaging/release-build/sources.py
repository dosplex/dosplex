#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Fetch and unpack the pinned third-party sources of the release build (sources.json).

  sources.py fetch  --dest DIR              download every source and keep it only if its SHA-256 matches
  sources.py unpack --from DIR --dest DIR   check every file in DIR again and unpack it into DEST/<directory>
  sources.py libraries                      print the sonames of the private libraries, one per line

All take --sources FILE to read another list than the sources.json next to this script (the tests do).

A source is unpacked into DEST/<directory>, the one top-level directory of its archive. A source with "into" is a
snapshot of a subproject (a git submodule that the host archive leaves out): its content goes to DEST/<into>
instead, which has to lie inside the directory of a source listed before it, and a file of the host is never
overwritten. A source with "overlay" gets the files of that directory copied over it afterwards, which is what a
meson wrap's patch_directory does (meson does not apply it to a subproject that is already in place). A source
with "patches" gets each listed patch file (a path relative to the directory of sources.json) applied to its
unpacked tree with `patch -p1`, in order and with no fuzz; a patch that does not apply cleanly stops the build.

Standard library only; `unpack` runs GNU patch for a source with patches. `fetch` is the one step of the build
that uses the network: the image build runs it once, and the payload build runs `unpack` with no network at all.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
SOURCES = HERE / 'sources.json'
FILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]*\.(tar\.gz|tar\.xz|tgz)')
COMMIT = re.compile(r'[0-9a-f]{40}')
PAYLOAD_DIRECTORIES = ('bin', 'lib', 'libexec', 'share')
PRODUCT_KINDS = ('executable', 'library', 'data')
# Payload files that are not built from a source of this list but may contain code of one (the compositor).
FIXED_FILES = ('bin/dosplex-compositor',)


def relative_path(value, what, name):
    """`value` as a normalised relative path of at least one component, else SystemExit."""
    path = PurePosixPath(value) if isinstance(value, str) else None
    if (path is None or not path.parts or path.is_absolute() or str(path) != value
            or any(part in ('.', '..') for part in path.parts)):
        raise SystemExit('%s: %s must be a relative path without . or .. (got %r)' % (name, what, value))
    return path


def directory(source):
    """The one top-level directory of the archive. "{commit}" in the name stands for the commit of the source:
    a GitHub archive of a commit is unpacked into <repository>-<commit>, and the commit is written once."""
    return source['directory'].replace('{commit}', str(source.get('commit', '{commit}')))


def filename(source):
    """The name the download is kept under: "filename" when the source has one, else the end of its URL."""
    return source.get('filename') or Path(urllib.parse.urlparse(source['url']).path).name


def payload_path(value, what, name):
    """`value` as a path inside the payload: a file below bin/, lib/, libexec/ or share/."""
    path = relative_path(value, what, name)
    if len(path.parts) < 2 or path.parts[0] not in PAYLOAD_DIRECTORIES:
        raise SystemExit('%s: %s must be a file below %s (got %r)' % (name, what, ', '.join(PAYLOAD_DIRECTORIES), value))
    return path


def patch_files(source, sources_path=None):
    """The patch files of a source, as paths next to the sources list (sources.json's directory)."""
    base = Path(sources_path or SOURCES).resolve().parent
    return [base / item for item in source.get('patches', [])]


def load(path=None):
    data = json.loads(Path(path or SOURCES).read_text())
    if data.get('format') != 1:
        raise SystemExit('sources.json: unknown format')
    sources = data['sources']
    names, files, directories, products = set(), set(), [], set()
    for source in sources:
        name = source.get('name', '?')
        for key in ('name', 'version', 'url', 'sha256', 'directory', 'license'):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise SystemExit('%s: %s is missing' % (name, key))
        parsed = urllib.parse.urlparse(source['url'])
        if parsed.scheme != 'https':
            raise SystemExit('%s: the url must be https' % name)
        if len(source['sha256']) != 64 or any(c not in '0123456789abcdef' for c in source['sha256']):
            raise SystemExit('%s: sha256 must be 64 lowercase hex digits' % name)
        if name in names:
            raise SystemExit('%s: listed twice' % name)
        names.add(name)
        if not FILE_NAME.fullmatch(filename(source)):
            raise SystemExit('%s: %r is not a usable archive file name (.tar.gz, .tar.xz or .tgz)' % (name, filename(source)))
        if filename(source) in files:
            raise SystemExit('%s: another source is downloaded under the name %s' % (name, filename(source)))
        files.add(filename(source))
        if '{commit}' in source['directory'] and 'commit' not in source:
            raise SystemExit('%s: directory uses {commit}, but the source has no commit' % name)
        if len(relative_path(directory(source), 'directory', name).parts) != 1:
            raise SystemExit('%s: directory is the one top-level directory of the archive, not a path' % name)
        licenses = source.get('license_files')
        if not isinstance(licenses, list) or not licenses or len(set(licenses)) != len(licenses):
            raise SystemExit('%s: license_files must be a list of distinct files of the source tree' % name)
        for item in licenses:
            relative_path(item, 'a license file', name)
        if len({PurePosixPath(item).name for item in licenses}) != len(licenses):
            raise SystemExit('%s: two license files have the same name (they are kept under licenses/ by name)' % name)
        if 'commit' in source and not COMMIT.fullmatch(str(source['commit'])):
            raise SystemExit('%s: commit must be 40 lowercase hex digits' % name)
        for key in ('libraries', 'embeds', 'embedded_in'):
            value = source.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise SystemExit('%s: %s must be a list of names' % (name, key))
        for soname in source.get('libraries', []):
            if '/' in soname:
                raise SystemExit('%s: libraries lists sonames, not paths (%r)' % (name, soname))
        patches = source.get('patches', [])
        if not isinstance(patches, list) or len(set(map(str, patches))) != len(patches):
            raise SystemExit('%s: patches must be a list of distinct patch files' % name)
        for item in patches:
            if relative_path(item, 'a patch', name).suffix != '.patch':
                raise SystemExit('%s: a patch file is named *.patch (got %r)' % (name, item))
        listed = source.get('products', {})
        if not isinstance(listed, dict):
            raise SystemExit('%s: products must map payload paths to a kind and a license' % name)
        for product, facts in listed.items():
            payload_path(product, 'a product', name)
            if (not isinstance(facts, dict) or facts.get('kind') not in PRODUCT_KINDS
                    or not isinstance(facts.get('license'), str) or not facts['license'].strip()):
                raise SystemExit('%s: product %s needs a kind (%s) and a license' % (
                    name, product, ', '.join(PRODUCT_KINDS)))
            if product in products:
                raise SystemExit('%s: product %s is also built from another source' % (name, product))
            products.add(product)
            for key in ('template', 'library', 'family', 'layer_name', 'disable_variable'):
                if key in facts and not (isinstance(facts[key], str) and facts[key].strip()):
                    raise SystemExit('%s: product %s: %s must be a text' % (name, product, key))
            if 'template' in facts:
                relative_path(facts['template'], 'a template', name)
                if facts.get('library') not in listed or 'family' not in facts:
                    raise SystemExit('%s: product %s is made from a template, so it needs the family and the '
                                     'library (another product of this source) that it names' % (name, product))
        for target in source.get('embedded_in', []):
            payload_path(target, 'embedded_in', name)
            if target not in products and target not in FIXED_FILES:
                raise SystemExit('%s: embedded_in names %s, which no source listed before it builds' % (name, target))
        if source.get('embedded_in') and not str(source.get('note', '')).strip():
            raise SystemExit('%s: a source that is compiled into payload files needs a note for the SBOM' % name)
        if 'into' in source:
            target = relative_path(source['into'], 'into', name)
            if len(target.parts) < 2 or target.parts[0] not in directories:
                raise SystemExit('%s: into must lie inside the directory of a source listed before it' % name)
        if 'overlay' in source:
            if 'into' not in source:
                raise SystemExit('%s: overlay needs into' % name)
            overlay = relative_path(source['overlay'], 'overlay', name)
            if overlay.parts[0] not in directories:
                raise SystemExit('%s: overlay must lie inside the directory of a source listed before it' % name)
        directories.append(directory(source))
    return sources


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url, target):
    last = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'dosplex-release-build'})
            with urllib.request.urlopen(request, timeout=60) as reply, target.open('wb') as out:
                while True:
                    block = reply.read(1 << 16)
                    if not block:
                        return
                    out.write(block)
        except OSError as error:
            last = error
            time.sleep(2 * (attempt + 1))
    raise SystemExit('download failed: %s: %s' % (url, last))


def fetch(dest, sources):
    dest.mkdir(parents=True, exist_ok=True)
    for source in sources:
        target = dest / filename(source)
        temporary = dest / (target.name + '.part')
        download(source['url'], temporary)
        got = digest(temporary)
        if got != source['sha256']:
            temporary.unlink()
            raise SystemExit('%s: SHA-256 mismatch\n  expected %s\n  got      %s' % (source['url'], source['sha256'], got))
        temporary.rename(target)
        print('%s %s  %s' % (got, source['name'], source['version']), file=sys.stderr)


def apply_patch(tree, patch):
    """Apply `patch` (a unified diff with a/ and b/ prefixes) to the source tree at `tree`: exactly, or not at all."""
    if not patch.is_file():
        raise SystemExit('%s: the patch file does not exist' % patch)
    done = subprocess.run(['patch', '--forward', '--strip=1', '--fuzz=0', '--no-backup-if-mismatch',
                           '--reject-file=-', '--directory', str(tree), '--input', str(patch)],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit('%s: does not apply to %s:\n%s%s' % (patch.name, tree.name, done.stdout, done.stderr))


def unpack(origin, dest, sources, sources_path=None):
    dest.mkdir(parents=True, exist_ok=True)
    for source in sources:
        archive = origin / filename(source)
        if not archive.is_file() or digest(archive) != source['sha256']:
            raise SystemExit('%s: missing or does not match the pinned SHA-256 (rebuild the image)' % archive)
        top = directory(source)
        placed = source.get('into', top)
        with tarfile.open(archive) as tar:
            # Only regular files and directories under the one expected top-level directory.
            members = tar.getmembers()
            for member in members:
                parts = Path(member.name).parts
                if (not parts or parts[0] != top or '..' in parts or Path(member.name).is_absolute()
                        or not (member.isfile() or member.isdir())):
                    raise SystemExit('%s: unexpected member %r' % (archive, member.name))
                member.name = '/'.join((placed,) + parts[1:])
                if member.isfile() and (dest / member.name).exists():
                    raise SystemExit('%s: %s would overwrite a file that is already there' % (archive, member.name))
            tar.extractall(dest, members=members, filter='data')
        if not (dest / placed).is_dir():
            raise SystemExit('%s: no directory %s' % (archive, placed))
        for item in source['license_files']:
            if not (dest / placed / item).is_file():
                raise SystemExit('%s: the license file %s is not in the archive' % (archive, item))
        for patch in patch_files(source, sources_path):
            apply_patch(dest / placed, patch)
            print('%s: applied %s' % (source['name'], patch.name), file=sys.stderr)
    for source in sources:
        if 'overlay' in source:
            origin_dir = dest / source['overlay']
            if not origin_dir.is_dir():
                raise SystemExit('%s: the overlay directory %s does not exist' % (source['name'], source['overlay']))
            shutil.copytree(origin_dir, dest / source['into'], dirs_exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('fetch')
    two = sub.add_parser('unpack')
    three = sub.add_parser('libraries')
    for command in (one, two):
        command.add_argument('--dest', type=Path, required=True)
    for command in (one, two, three):
        command.add_argument('--sources', type=Path,
                             help='the list of sources (default: sources.json next to this script)')
    two.add_argument('--from', dest='origin', type=Path, required=True)
    args = parser.parse_args()
    sources = load(args.sources)
    if args.command == 'fetch':
        fetch(args.dest, sources)
    elif args.command == 'unpack':
        unpack(args.origin, args.dest, sources, args.sources)
    else:
        for source in sources:
            for soname in source.get('libraries', []):
                print(soname)


if __name__ == '__main__':
    main()
