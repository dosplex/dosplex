#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""release.py: build, sign and check DosPlex release archives (T-702, PKG-011).

A release is made from a prepared bundle: the directory with a manifest.json that the bundle builder writes
from the payload of packaging/release-build/build.sh. Standard library only; signing needs the `minisign`
program on PATH and an operator-held secret key. Nothing here ever creates a key.

    release.py build BUNDLE --out DIR [--sign-with SECRET_KEY] [--pin-key PUBLIC_KEY ...] [--sequence N]
                     [--base-url URL] [--program PATH] [--minisign PATH] [--allow-incomplete]
    release.py index --out DIR --channel NAME [--sign-with SECRET_KEY] [--program PATH] ENTRY.json ...
    release.py verify DIR [--program PATH] [--pin-key PUBLIC_KEY ...]
    release.py pin PUBLIC_KEY_FILE ...

build  checks the bundle against its own manifest, adds a "release" entry to the manifest (a sequence number and
       the SHA-256 of the dosplex program that ships with the release), packs bundle/ and the program into
       dosplex-VERSION.tar.gz, and writes next to it:
           dosplex-VERSION.manifest.json           the manifest, byte for byte as it is in the archive
           dosplex-VERSION.manifest.json.minisig   its detached signature (with --sign-with)
           dosplex-VERSION.index-entry.json        this release's entry for the channel index
           dosplex-VERSION.sbom.json, dosplex-VERSION.THIRD-PARTY-NOTICES.md, dosplex-VERSION.NOTICE
       It then checks the result the way a device does, with the verifier and the pinned keys of the program
       that it just packed, and prints the SHA-256 of that program for the release page.
index  merges the entries of the releases into dosplex-index.json, the file `dosplex update` reads, and signs it.
verify checks a directory of release files the same way, with the program given by --program.
pin    prints the RELEASE_KEYS lines for a minisign public key file (what tools/dosplex pins).

The signature is minisign's: `minisign -V -p PUBLIC_KEY -m dosplex-VERSION.manifest.json` checks it too. The
archive is reproducible: the same bundle, program, sequence and date give the same bytes.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types

sys.dont_write_bytecode = True
DEFAULT_PROGRAM = Path(__file__).resolve().with_name('dosplex')
KEY_BLOCK = re.compile(r'(# BEGIN RELEASE KEYS[^\n]*\n)(.*?)(# END RELEASE KEYS)', re.S)
DECISION_MARKER = 'OWNER DECISION REQUIRED'    # written by tools/generate-notices.py (its DECISION constant)


class ReleaseError(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# --- the dosplex program: its verifier is the one that decides ----------------------------------------------------

def pin_program(text, entries):
    """The program text with RELEASE_KEYS set to exactly `entries` (dicts with "id" and "public")."""
    lines = ['RELEASE_KEYS = (\n'] + ["    {'id': %r, 'public': %r},\n" % (e['id'], e['public']) for e in entries] + [')\n']
    if not KEY_BLOCK.search(text):
        raise ReleaseError('the dosplex program has no RELEASE KEYS block to pin a key into')
    return KEY_BLOCK.sub(lambda match: match.group(1) + ''.join(lines) + match.group(3), text, count=1)


def load_program(path, pins=None):
    """(module, bytes) of the dosplex program, with its RELEASE_KEYS replaced when `pins` is given."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as error:
        raise ReleaseError('cannot read the dosplex program %s: %s' % (path, error.strerror or error)) from None
    if pins is not None:
        text = pin_program(text, pins)
    module = types.ModuleType('dosplex_program')
    module.__file__ = str(path)
    exec(compile(text, str(path), 'exec'), module.__dict__)
    return module, text.encode('utf-8')


def read_key_entries(program, files):
    """RELEASE_KEYS entries for minisign public key files."""
    entries = []
    for path in files:
        try:
            keynum, public = program.read_minisign_public_key(Path(path).read_text())
        except OSError as error:
            raise ReleaseError('cannot read the public key %s: %s' % (path, error.strerror or error)) from None
        except program.VerifyError as error:
            raise ReleaseError('%s: %s' % (path, error)) from None
        entries.append({'id': program.key_id_text(keynum), 'public': public.hex()})
    return entries


def program_with_keys(args):
    """(module, bytes) of the program the release ships, with --pin-key applied."""
    module, _ = load_program(args.program)
    pins = read_key_entries(module, args.pin_key) if args.pin_key else None
    return load_program(args.program, pins)


# --- the bundle and the release manifest ------------------------------------------------------------------------

def read_bundle(program, bundle):
    """(manifest dict, manifest bytes, Verified) of a prepared bundle, checked against its own manifest."""
    path = Path(bundle) / 'manifest.json'
    try:
        raw = path.read_bytes()
    except OSError as error:
        if (Path(bundle) / 'share/dosplex/VERSION').is_file():
            raise ReleaseError('%s is a release payload (the output of packaging/release-build/build.sh), not a bundle: it '
                               'lacks the retained-trial tools and has no manifest. Make the bundle from it with the bundle '
                               'builder (prepare-session-bundle.py --payload %s) and give release.py its bundle directory'
                               % (bundle, bundle)) from None
        raise ReleaseError('%s has no manifest.json (%s); give the directory that the bundle builder wrote'
                           % (bundle, error.strerror or error)) from None
    try:
        verified = program.verify_directory(bundle, sha256(raw))
        manifest = json.loads(raw)
    except (program.VerifyError, ValueError) as error:
        raise ReleaseError('the bundle does not agree with its own manifest: %s' % error) from None
    if 'release' in manifest:
        raise ReleaseError('the bundle manifest already has a release entry; build from the prepared bundle, not from a release')
    missing = program.missing_required(verified.manifest)
    if missing:
        raise ReleaseError('the bundle lacks files that dosplex needs to start: %s' % ', '.join(missing))
    return manifest, raw, verified


def source_date(args, verified):
    """The SOURCE_DATE_EPOCH of the release: --source-date-epoch, the environment, the SBOM, else 0."""
    if args.source_date_epoch is not None:
        return args.source_date_epoch
    if os.environ.get('SOURCE_DATE_EPOCH', '').isdigit():
        return int(os.environ['SOURCE_DATE_EPOCH'])
    return sbom_epoch(verified) or 0


def sbom_epoch(verified):
    entry = verified.files.get('sbom/dosplex-sbom.json')
    if entry is None:
        return None
    try:
        value = json.loads(entry[0])['package']['source_date_epoch']
    except (ValueError, KeyError, TypeError):
        return None
    return value if type(value) is int and value > 0 else None


def release_manifest(program, manifest, sequence, shipped):
    """The bundle manifest with a release entry before "files", as the bytes that are signed."""
    entry = {'format': 1, 'sequence': sequence,
             'cli': {'version': program.CLI_VERSION, 'sha256': sha256(shipped), 'bytes': len(shipped)}}
    result = {}
    for key, value in manifest.items():
        if key == 'files':
            result['release'] = entry
        result[key] = value
    if 'release' not in result:
        result['release'] = entry
    raw = (json.dumps(result, indent=2) + '\n').encode()
    try:
        if len(raw) > program.MAX_MANIFEST:
            raise program.VerifyError('it is %d bytes; dosplex reads at most %d' % (len(raw), program.MAX_MANIFEST))
        program.parse_manifest(raw)
    except program.VerifyError as error:
        raise ReleaseError('the release manifest is not acceptable to dosplex: %s' % error) from None
    return raw


def make_archive(version, manifest_raw, verified, shipped, mtime):
    """The release archive: dosplex-VERSION/bundle/... and dosplex-VERSION/dosplex, the same bytes every time."""
    root = 'dosplex-' + version
    out = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=out, mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            members = [('bundle/manifest.json', manifest_raw, 0o644)]
            members += [('bundle/' + name, data, mode) for name, (data, mode) in sorted(verified.files.items())]
            members += [('dosplex', shipped, 0o755)]
            for name, data, mode in members:
                info = tarfile.TarInfo('%s/%s' % (root, name))
                info.size, info.mode, info.mtime = len(data), mode, mtime
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


# --- signing ---------------------------------------------------------------------------------------------------------

def find_minisign(command):
    path = shutil.which(command)
    if path is None:
        raise ReleaseError('%s is not on PATH. Signing needs minisign (https://jedisct1.github.io/minisign/); install it '
                           'on the machine that holds the secret key, or build without --sign-with and sign '
                           'dosplex-VERSION.manifest.json yourself' % command)
    return path


def sign_file(minisign, secret_key, path, comment, signature):
    """Run `minisign -S`. It asks for the key's password on the terminal, so nothing is captured."""
    command = [minisign, '-S', '-s', str(secret_key), '-m', str(path), '-x', str(signature), '-t', comment]
    try:
        done = subprocess.run(command)
    except OSError as error:
        raise ReleaseError('cannot run %s: %s' % (minisign, error.strerror or error)) from None
    if done.returncode != 0 or not Path(signature).is_file():
        raise ReleaseError('minisign did not sign %s (exit status %d); nothing was published' % (path, done.returncode))


# --- build -----------------------------------------------------------------------------------------------------------

def check_output_directory(out):
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ReleaseError('%s exists and is not empty' % out)
    return out


def index_entry(program, manifest_raw, archive, base_url):
    manifest = program.parse_manifest(manifest_raw)
    name = 'dosplex-' + manifest.version
    return {'version': manifest.version, 'sequence': manifest.release['sequence'], 'commit': manifest.commit,
            'tested_versions': manifest.tested,
            'manifest': {'url': base_url + name + '.manifest.json', 'sha256': sha256(manifest_raw),
                         'bytes': len(manifest_raw)},
            'signature': {'url': base_url + name + '.manifest.json.minisig'},
            'tarball': {'url': base_url + name + '.tar.gz', 'sha256': sha256(archive), 'bytes': len(archive)}}


def cmd_build(args):
    out = check_output_directory(args.out)
    minisign = find_minisign(args.minisign) if args.sign_with else None
    if args.sign_with and not Path(args.sign_with).is_file():
        raise ReleaseError('the secret key file %s does not exist' % args.sign_with)
    program, shipped = program_with_keys(args)
    manifest, _, verified = read_bundle(program, args.bundle)
    version = manifest['version']
    if not args.allow_incomplete:
        absent = [name for name in ('sbom/dosplex-sbom.json', 'THIRD-PARTY-NOTICES.md') if name not in verified.files]
        if absent:
            raise ReleaseError('the bundle has no %s; a release is not signed without its SBOM and notices (T-701). '
                               'Build the bundle from the release payload, or pass --allow-incomplete for a trial'
                               % ' and no '.join(absent))
    if 'THIRD-PARTY-NOTICES.md' in verified.files:
        lines = [line for line in verified.files['THIRD-PARTY-NOTICES.md'][0].decode('utf-8', 'replace').splitlines()
                 if DECISION_MARKER in line]
        if lines:
            if not args.allow_incomplete:
                raise ReleaseError('the notices hold %d line(s) marked %s, first: %s. A release is not built while a '
                                   'decision is open; pass --allow-incomplete to build a trial anyway'
                                   % (len(lines), DECISION_MARKER, lines[0][:200]))
            print('warning   the notices hold %d open owner decision(s); this archive must not be published' % len(lines),
                  file=sys.stderr)
    sequence = args.sequence if args.sequence is not None else sbom_epoch(verified)
    if sequence is None:
        raise ReleaseError('the release needs a sequence number that grows with every release; give --sequence N '
                           '(the bundle has no SBOM to take the commit date from)')
    if args.sign_with and not program.pinned_keys():
        raise ReleaseError('the dosplex program pins no release key, so it could not verify what you sign. Pin the '
                           'public key of the signing key with --pin-key PUBLIC_KEY_FILE, or put it in RELEASE_KEYS '
                           '(release.py pin PUBLIC_KEY_FILE)')
    manifest_raw = release_manifest(program, manifest, sequence, shipped)
    archive = make_archive(version, manifest_raw, verified, shipped, source_date(args, verified))
    base_url = args.base_url if not args.base_url or args.base_url.endswith('/') else args.base_url + '/'
    name = 'dosplex-' + version
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.release-', dir=out.parent.resolve()))
    signed = None
    try:
        files = {name + '.tar.gz': archive, name + '.manifest.json': manifest_raw}
        for asset, suffix in (('sbom/dosplex-sbom.json', '.sbom.json'), ('THIRD-PARTY-NOTICES.md', '.THIRD-PARTY-NOTICES.md'),
                              ('NOTICE', '.NOTICE')):
            if asset in verified.files:
                files[name + suffix] = verified.files[asset][0]
        entry = index_entry(program, manifest_raw, archive, base_url or '')
        files[name + '.index-entry.json'] = (json.dumps(entry, indent=2) + '\n').encode()
        for asset, data in files.items():
            (staging / asset).write_bytes(data)
        signature = staging / (name + '.manifest.json.minisig')
        if minisign:
            sign_file(minisign, args.sign_with, staging / (name + '.manifest.json'), 'dosplex manifest ' + version, signature)
            try:    # what a device does, with the verifier and the keys of the program that ships in the archive
                signed = program.verify_release_archive(archive, signature.read_text(), program.pinned_keys())[2]
            except program.VerifyError as error:
                raise ReleaseError('the signature does not verify with the program that ships in the release (%s). '
                                   'Sign with the secret key whose public key the program pins (%s)'
                                   % (error, ', '.join(key.id for key in program.pinned_keys()))) from None
        staging.chmod(0o755)
        for item in staging.iterdir():
            item.chmod(0o644)
        if out.exists():
            out.rmdir()
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print('release   %s (sequence %d, commit %s)' % (version, sequence, manifest['commit']))
    print('written   %s' % out)
    print('manifest  %s  %s.manifest.json' % (sha256(manifest_raw), name))
    print('archive   %s  %s.tar.gz (%d bytes)' % (sha256(archive), name, len(archive)))
    print('program   %s  dosplex %s  <- the installer hash for the release page' % (sha256(shipped), program.CLI_VERSION))
    if signed is not None:
        print('signed    by key %s: %s.manifest.json.minisig, checked with the program in the archive' % (signed.key_id, name))
    else:
        print('unsigned  sign it offline, on the machine that holds the key:')
        print('          minisign -S -s SECRET_KEY -m %s.manifest.json -x %s.manifest.json.minisig -t "dosplex manifest %s"'
              % (name, name, version))
    if program.pinned_keys():
        print('pinned    the program trusts: %s' % ', '.join(key.id for key in program.pinned_keys()))
    else:
        print('pinned    the program pins no release key; devices cannot verify this release until one is pinned')
    return 0


# --- index -----------------------------------------------------------------------------------------------------------

def cmd_index(args):
    out = check_output_directory(args.out)
    minisign = find_minisign(args.minisign) if args.sign_with else None
    program, _ = program_with_keys(args)
    releases = []
    for path in args.entry:
        try:
            entry = json.loads(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise ReleaseError('cannot read the index entry %s: %s' % (path, getattr(error, 'strerror', None) or error)) from None
        signature = Path(path).with_name(Path(path).name[:-len('.index-entry.json')] + '.manifest.json.minisig')
        if Path(path).name.endswith('.index-entry.json') and signature.is_file():
            try:     # the key that signed the release, so that a device that does not trust it yet can skip the entry
                entry['signed_by'] = program.key_id_text(program.parse_minisign_signature(signature.read_text())[1])
            except (OSError, program.VerifyError) as error:
                raise ReleaseError('cannot read the signature %s: %s' % (signature, error)) from None
        releases.append(entry)
    if not releases:
        raise ReleaseError('give at least one index entry (dosplex-VERSION.index-entry.json)')
    epoch = int(os.environ['SOURCE_DATE_EPOCH']) if os.environ.get('SOURCE_DATE_EPOCH', '').isdigit() else int(time.time())
    raw = (json.dumps({'format': 'dosplex-index', 'format_version': 1, 'channel': args.channel,
                       'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch)),
                       'releases': sorted(releases, key=lambda release: -release.get('sequence', 0))},
                      indent=2) + '\n').encode()
    try:
        program.parse_index(raw, 'file:///index')
    except program.VerifyError as error:
        raise ReleaseError('the index would not be acceptable to dosplex: %s' % error) from None
    if minisign and not program.pinned_keys():
        raise ReleaseError('the dosplex program pins no release key, so it could not verify what you sign; use --pin-key')
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.index-', dir=out.parent.resolve()))
    signed = None
    try:
        (staging / 'dosplex-index.json').write_bytes(raw)
        if minisign:
            sign_file(minisign, args.sign_with, staging / 'dosplex-index.json', 'dosplex index ' + args.channel,
                      staging / 'dosplex-index.json.minisig')
            try:
                signed = program.verify_signature(raw, (staging / 'dosplex-index.json.minisig').read_text(),
                                                  program.pinned_keys(), 'index')
            except program.VerifyError as error:
                raise ReleaseError('the signature does not verify with the keys the program pins: %s' % error) from None
        staging.chmod(0o755)
        for item in staging.iterdir():
            item.chmod(0o644)
        if out.exists():
            out.rmdir()
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print('index     %s: %d release(s), newest %s' % (out / 'dosplex-index.json', len(releases),
                                                      max(releases, key=lambda release: release['sequence'])['version']))
    if signed is not None:
        print('signed    by key %s' % signed.key_id)
    else:
        print('unsigned  sign it offline: minisign -S -s SECRET_KEY -m dosplex-index.json -x dosplex-index.json.minisig '
              '-t "dosplex index %s"' % args.channel)
    return 0


# --- verify and pin --------------------------------------------------------------------------------------------------

def cmd_verify(args):
    program, program_bytes = program_with_keys(args)
    directory = Path(args.directory)
    archives = sorted(directory.glob('dosplex-*.tar.gz'))
    indexes = sorted(directory.glob('dosplex-index.json'))
    if not archives and not indexes:
        raise ReleaseError('%s holds no dosplex-VERSION.tar.gz and no dosplex-index.json' % directory)
    keys = program.pinned_keys()
    for archive in archives:
        base = archive.name[:-len('.tar.gz')]
        try:
            data = archive.read_bytes()
            signature = (directory / (base + '.manifest.json.minisig')).read_text()
            verified, shipped, signed = program.verify_release_archive(data, signature, keys)
        except OSError as error:
            raise ReleaseError('%s: %s' % (error.filename or archive, error.strerror or error)) from None
        except program.VerifyError as error:
            raise ReleaseError('%s: %s' % (archive.name, error)) from None
        problems = []
        if (directory / (base + '.manifest.json')).read_bytes() != verified.manifest.raw:
            problems.append('%s.manifest.json differs from the manifest in the archive' % base)
        for asset, suffix in (('sbom/dosplex-sbom.json', '.sbom.json'), ('THIRD-PARTY-NOTICES.md', '.THIRD-PARTY-NOTICES.md'),
                              ('NOTICE', '.NOTICE')):
            published = directory / (base + suffix)
            if published.exists() and asset in verified.files and published.read_bytes() != verified.files[asset][0]:
                problems.append('%s differs from %s in the archive' % (published.name, asset))
        entry_path = directory / (base + '.index-entry.json')
        if entry_path.exists():
            try:
                entry = json.loads(entry_path.read_text())
                described = ((entry['tarball']['sha256'], entry['tarball']['bytes']) == (sha256(data), len(data))
                             and entry['manifest']['sha256'] == verified.manifest.sha256
                             and entry['sequence'] == verified.manifest.release['sequence'])
            except (OSError, ValueError, KeyError, TypeError):
                described = False
            if not described:
                problems.append('%s does not describe this archive' % entry_path.name)
        if problems:
            raise ReleaseError('; '.join(problems))
        print('ok  %s: signed by key %s, %d files match manifest %s, dosplex program %s (%s)' % (
            archive.name, signed.key_id, len(verified.files), verified.manifest.sha256[:12], sha256(shipped)[:12],
            'the program given with --program' if shipped == program_bytes else 'not the program given with --program'))
    for index in indexes:
        try:
            raw = index.read_bytes()
            signature = (directory / (index.name + '.minisig')).read_text()
            signed = program.verify_signature(raw, signature, keys, 'index')
            parsed = program.parse_index(raw, 'file:///index')
        except OSError as error:
            raise ReleaseError('%s: %s' % (error.filename or index, error.strerror or error)) from None
        except program.VerifyError as error:
            raise ReleaseError('%s: %s' % (index.name, error)) from None
        if signed.subject != parsed.channel:
            raise ReleaseError('%s is signed for channel %s but is the index of channel %s' % (index.name, signed.subject, parsed.channel))
        print('ok  %s: signed by key %s, channel %s, %d release(s)' % (index.name, signed.key_id, parsed.channel, len(parsed.releases)))
    return 0


def cmd_pin(args):
    program, _ = load_program(args.program)
    entries = read_key_entries(program, args.public_key)
    print('# Replace the lines between the RELEASE KEYS markers of tools/dosplex with:')
    print('RELEASE_KEYS = (')
    for entry in entries:
        print("    {'id': %r, 'public': %r}," % (entry['id'], entry['public']))
    print(')')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='release.py', description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)

    def common(command, keys=True):
        command.add_argument('--program', type=Path, default=DEFAULT_PROGRAM, metavar='PATH',
                             help='the dosplex program to ship and to verify with (default: tools/dosplex)')
        command.add_argument('--minisign', default='minisign', metavar='COMMAND', help='the minisign program (default: minisign)')
        command.add_argument('--sign-with', metavar='SECRET_KEY', help='sign with this minisign secret key (asks for its password)')
        if keys:
            command.add_argument('--pin-key', action='append', default=[], metavar='PUBLIC_KEY',
                                 help='ship the program with RELEASE_KEYS set to this minisign public key (repeat for '
                                 'more; the first release of a rotation lists the old and the new key)')

    build = sub.add_parser('build', help='build and sign a release archive from a prepared bundle')
    build.add_argument('bundle', type=Path, metavar='BUNDLE', help='the prepared bundle directory (with manifest.json)')
    build.add_argument('--out', type=Path, required=True, metavar='DIR', help='where the release files go (new or empty)')
    build.add_argument('--sequence', type=int, metavar='N', help='the release sequence number (default: the commit date '
                       'from the bundle SBOM)')
    build.add_argument('--source-date-epoch', type=int, metavar='SECONDS', help='file times in the archive (default: '
                       'SOURCE_DATE_EPOCH, else the SBOM commit date, else 0)')
    build.add_argument('--base-url', default='', metavar='URL', help='where the release files will be published; goes '
                       'into the index entry (default: names relative to the index)')
    build.add_argument('--allow-incomplete', action='store_true', help='build a release without SBOM and notices (trials only)')
    common(build)
    index = sub.add_parser('index', help='merge index entries into a signed release index')
    index.add_argument('entry', nargs='+', metavar='ENTRY.json', help='dosplex-VERSION.index-entry.json files')
    index.add_argument('--out', type=Path, required=True, metavar='DIR')
    index.add_argument('--channel', required=True, metavar='NAME', help='the channel name, for example preview')
    common(index)
    verify = sub.add_parser('verify', help='check release files the way a device does')
    verify.add_argument('directory', type=Path, metavar='DIR')
    verify.add_argument('--program', type=Path, default=DEFAULT_PROGRAM, metavar='PATH')
    verify.add_argument('--pin-key', action='append', default=[], metavar='PUBLIC_KEY')
    pin = sub.add_parser('pin', help='print the RELEASE_KEYS lines for minisign public keys')
    pin.add_argument('public_key', nargs='+', metavar='PUBLIC_KEY_FILE')
    pin.add_argument('--program', type=Path, default=DEFAULT_PROGRAM, metavar='PATH')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return {'build': cmd_build, 'index': cmd_index, 'verify': cmd_verify, 'pin': cmd_pin}[args.command](args)
    except ReleaseError as error:
        print('release.py: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
