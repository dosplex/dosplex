#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Read Steam depot manifests and compare an install folder against them.

A tool that shares installed Steam games between two Unix accounts must know, file by file, whether a game folder
holds exactly what Steam shipped. This module reads the depot manifests (depotcache/<depot>_<gid>.manifest), the
app manifests (appmanifest_<appid>.acf) and libraryfolders.vdf, and sorts the files of a folder into pristine,
changed, missing and extra.

Wrong answers lose data, so the code is strict. It never follows a symlink, it refuses any input it does not
understand with a DepotError, and it treats anything unreadable as changed. It uses the standard library only.
"""
import argparse
import dataclasses
import hashlib
import io
import json
import os
import stat
import struct
import sys
import zipfile
import zlib

PAYLOAD_MAGIC = 0x71F617D0
METADATA_MAGIC = 0x1F4812BE
SIGNATURE_MAGIC = 0x1B81B817
END_MAGIC = 0x32C415AB

USER_CONFIG = 1
VERSIONED_USER_CONFIG = 2
EXECUTABLE = 32
DIRECTORY = 64
SYMLINK = 512

MAX_VDF_DEPTH = 64
BLOCK = 1 << 20


class DepotError(ValueError):
    """Raised for every refusal of input."""


@dataclasses.dataclass(frozen=True)
class DepotFile:
    """One entry of a depot manifest. The name uses forward slashes and keeps the manifest's case."""
    name: str
    size: int
    flags: int
    sha1: object = None
    link: object = None

    @property
    def is_directory(self):
        return bool(self.flags & DIRECTORY)

    @property
    def is_symlink(self):
        return bool(self.flags & SYMLINK)

    @property
    def is_executable(self):
        return bool(self.flags & EXECUTABLE)

    @property
    def is_user_config(self):
        return bool(self.flags & (USER_CONFIG | VERSIONED_USER_CONFIG))


@dataclasses.dataclass
class Manifest:
    depot_id: int
    gid: int
    creation_time: int
    files: list


# Depot manifest parsing.

def _varint(buf, pos):
    value = 0
    for index in range(10):
        if pos >= len(buf):
            raise DepotError('truncated varint')
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if value >= 1 << 64:
                raise DepotError('varint does not fit in 64 bits')
            return value, pos
    raise DepotError('varint longer than 10 bytes')


def _fields(buf):
    """Yield (number, wire type, value) for each protobuf field. Only wire types 0, 1, 2 and 5 are understood."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise DepotError('protobuf field number 0')
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            size, pos = _varint(buf, pos)
            if size > len(buf) - pos:
                raise DepotError('truncated length-delimited field')
            value = bytes(buf[pos:pos + size])
            pos += size
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if width > len(buf) - pos:
                raise DepotError('truncated fixed-width field')
            value = bytes(buf[pos:pos + width])
            pos += width
        else:
            raise DepotError(f'unknown protobuf wire type {wire}')
        yield number, wire, value


def _clean_name(raw, what='file name'):
    if not raw:
        raise DepotError(f'empty {what}')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise DepotError(f'{what} is not valid UTF-8: {raw!r}') from None
    if '\0' in text:
        raise DepotError(f'{what} holds a NUL byte: {text!r}')
    text = text.replace('\\', '/')
    if text.startswith('/'):
        raise DepotError(f'{what} is absolute: {text!r}')
    for part in text.split('/'):
        if part in ('', '.', '..'):
            raise DepotError(f'{what} has an empty, "." or ".." component: {text!r}')
    return text


def _parse_entry(buf):
    seen = {}
    for number, wire, value in _fields(buf):
        if number in (1, 2, 3, 4, 5, 7):
            expected = 0 if number in (2, 3) else 2
            if wire != expected:
                raise DepotError(f'file entry field {number} has wire type {wire}')
            if number in seen:
                raise DepotError(f'file entry field {number} appears twice')
            seen[number] = value
    name = _clean_name(seen.get(1, b''))
    size = seen.get(2, 0)
    flags = seen.get(3, 0)
    if flags & DIRECTORY and flags & SYMLINK:
        raise DepotError(f'entry is both a directory and a symlink: {name!r}')
    if flags & DIRECTORY:
        return DepotFile(name, size, flags)
    if flags & SYMLINK:
        target = seen.get(7, b'')
        if not target:
            raise DepotError(f'symlink with an empty target: {name!r}')
        try:
            link = target.decode('utf-8')
        except UnicodeDecodeError:
            raise DepotError(f'symlink target is not valid UTF-8: {name!r}') from None
        if '\0' in link:
            raise DepotError(f'symlink target holds a NUL byte: {name!r}')
        return DepotFile(name, size, flags, None, link)
    sha = seen.get(5)
    if sha is None or len(sha) != 20:
        raise DepotError(f'SHA-1 is not 20 bytes for {name!r}')
    return DepotFile(name, size, flags, sha.hex())


def _unzip(data, max_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise DepotError(f'zip holds {len(members)} members, expected 1')
            member = members[0]
            if member.is_dir():
                raise DepotError('zip member is a directory')
            if member.file_size > max_bytes:
                raise DepotError(f'zip member is larger than {max_bytes} bytes')
            with archive.open(member) as handle:
                out = handle.read(max_bytes + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError, OSError) as error:
        raise DepotError(f'bad zip archive: {error}') from None
    if len(out) > max_bytes:
        raise DepotError(f'zip member is larger than {max_bytes} bytes')
    return out


def _sections(data):
    pos, out = 0, {}
    while True:
        if pos + 4 > len(data):
            raise DepotError('manifest ends without an end marker')
        magic, = struct.unpack_from('<I', data, pos)
        pos += 4
        if magic == END_MAGIC:
            if pos != len(data):
                raise DepotError('data after the end marker')
            return out
        if magic not in (PAYLOAD_MAGIC, METADATA_MAGIC, SIGNATURE_MAGIC):
            raise DepotError(f'unknown section magic 0x{magic:08X}')
        if pos + 4 > len(data):
            raise DepotError('truncated section header')
        size, = struct.unpack_from('<I', data, pos)
        pos += 4
        if size > len(data) - pos:
            raise DepotError('truncated section')
        if magic in out:
            raise DepotError(f'section 0x{magic:08X} appears twice')
        out[magic] = data[pos:pos + size]
        pos += size


def parse_manifest(data, max_bytes=256 * 1024 * 1024):
    """Parse the bytes of a depot manifest, plain or zipped, into a Manifest."""
    data = bytes(data)
    if data[:4] == b'PK\x03\x04':
        data = _unzip(data, max_bytes)
    parts = _sections(data)
    if PAYLOAD_MAGIC not in parts:
        raise DepotError('manifest has no payload section')
    if METADATA_MAGIC not in parts:
        raise DepotError('manifest has no metadata section')
    meta = {}
    for number, wire, value in _fields(parts[METADATA_MAGIC]):
        if number in (1, 2, 3, 4, 5, 6):
            if wire != 0:
                raise DepotError(f'metadata field {number} has wire type {wire}')
            meta[number] = value
    if meta.get(4):
        raise DepotError('manifest file names are encrypted')
    if 1 not in meta or 2 not in meta:
        raise DepotError('metadata lacks the depot id or the manifest gid')
    files, by_name = [], {}
    for number, wire, value in _fields(parts[PAYLOAD_MAGIC]):
        if number != 1:
            continue
        if wire != 2:
            raise DepotError(f'file mapping has wire type {wire}')
        entry = _parse_entry(value)
        old = by_name.get(entry.name)
        if old is None:
            by_name[entry.name] = entry
            files.append(entry)
        elif old != entry:
            raise DepotError(f'name listed twice with different content: {entry.name!r}')
    return Manifest(meta[1], meta[2], meta.get(3, 0), files)


def _read_regular(path, max_bytes, what):
    try:
        info = os.lstat(path)
    except OSError as error:
        raise DepotError(f'cannot read {what} {path}: {error.strerror}') from None
    if not stat.S_ISREG(info.st_mode):
        raise DepotError(f'{what} is not a regular file (symlinks are refused): {path}')
    if info.st_size > max_bytes:
        raise DepotError(f'{what} is larger than {max_bytes} bytes: {path}')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise DepotError(f'cannot open {what} {path}: {error.strerror}') from None
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise DepotError(f'{what} is not a regular file of acceptable size: {path}')
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise DepotError(f'{what} is larger than {max_bytes} bytes: {path}')
    return data


def read_manifest(path, max_bytes=256 * 1024 * 1024):
    """Read a depot manifest file. A symlink, a non-regular file or an oversized file is refused."""
    return parse_manifest(_read_regular(path, max_bytes, 'manifest'), max_bytes)


# VDF text.

def _vdf_tokens(text):
    """Yield (kind, text) with kind 'open', 'close' or 'str'. Quoted and bare words are both 'str'."""
    pos, end = 0, len(text)
    while pos < end:
        char = text[pos]
        if char in ' \t\r\n':
            pos += 1
        elif char == '/' and text.startswith('//', pos):
            newline = text.find('\n', pos)
            pos = end if newline < 0 else newline + 1
        elif char == '{':
            yield 'open', char
            pos += 1
        elif char == '}':
            yield 'close', char
            pos += 1
        elif char == '"':
            pos += 1
            out = []
            while True:
                if pos >= end:
                    raise DepotError('unterminated quoted string')
                char = text[pos]
                if char == '"':
                    pos += 1
                    break
                if char in '\r\n':
                    raise DepotError('line break inside a quoted string')
                if char == '\\':
                    pos += 1
                    escape = text[pos:pos + 1]
                    mapped = {'\\': '\\', '"': '"', 'n': '\n', 't': '\t'}.get(escape)
                    if mapped is None:
                        raise DepotError(f'unknown escape \\{escape}')
                    out.append(mapped)
                else:
                    out.append(char)
                pos += 1
            yield 'str', ''.join(out)
        else:
            start = pos
            while pos < end and text[pos] not in ' \t\r\n{}"':
                pos += 1
            yield 'str', text[start:pos]


def vdf_loads(text):
    """Parse VDF text with one top-level section into (name, dict). Every leaf is a str."""
    tokens = _vdf_tokens(text)
    first = next(tokens, None)
    if first is None:
        raise DepotError('empty VDF text')
    if first[0] != 'str':
        raise DepotError('VDF text does not start with a name')
    opener = next(tokens, None)
    if opener is None or opener[0] != 'open':
        raise DepotError(f'top-level section {first[1]!r} has no opening brace')
    # An explicit stack keeps deep input from reaching the interpreter's recursion limit.
    top = {}
    stack = [top]
    pending = None
    for kind, value in tokens:
        if not stack:
            raise DepotError('text after the top-level section')
        current = stack[-1]
        if pending is None:
            if kind == 'close':
                stack.pop()
            elif kind == 'open':
                raise DepotError('unexpected opening brace')
            else:
                if value in current:
                    raise DepotError(f'duplicate key {value!r}')
                pending = value
        elif kind == 'open':
            if len(stack) >= MAX_VDF_DEPTH:
                raise DepotError(f'nesting deeper than {MAX_VDF_DEPTH}')
            child = {}
            current[pending] = child
            stack.append(child)
            pending = None
        elif kind == 'close':
            raise DepotError(f'missing value for key {pending!r}')
        else:
            current[pending] = value
            pending = None
    if stack or pending is not None:
        raise DepotError('unbalanced braces or a missing value')
    return first[1], top


def _vdf_quote(text):
    if '\r' in text:
        raise DepotError('a carriage return cannot be written to VDF')
    text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')
    return f'"{text}"'


def vdf_dumps(name, value):
    """Write (name, dict) in the layout Steam uses: tabs, braces on their own lines, two tabs before a value."""
    out = []

    def section(key, body, depth):
        indent = '\t' * depth
        out.append(f'{indent}{_vdf_quote(key)}\n{indent}{{\n')
        for sub_key, sub_value in body.items():
            if isinstance(sub_value, dict):
                section(sub_key, sub_value, depth + 1)
            elif isinstance(sub_value, str):
                out.append(f'{indent}\t{_vdf_quote(sub_key)}\t\t{_vdf_quote(sub_value)}\n')
            else:
                raise DepotError(f'VDF value for {sub_key!r} is neither str nor dict')
        out.append(f'{indent}}}\n')

    if not isinstance(value, dict):
        raise DepotError('VDF top-level value must be a dict')
    section(name, value, 0)
    return ''.join(out)


def read_vdf(path, max_bytes=16 * 1024 * 1024):
    """Read a VDF file (appmanifest, libraryfolders) and return (name, dict)."""
    raw = _read_regular(path, max_bytes, 'VDF file')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise DepotError(f'VDF file is not valid UTF-8: {path}') from None
    return vdf_loads(text)


def _number(text, what):
    if not (isinstance(text, str) and text.isascii() and text.isdigit()):
        raise DepotError(f'{what} is not a number: {text!r}')
    return int(text)


def installed_depots(app_state):
    """Return {depot_id: manifest gid} from an AppState's InstalledDepots, or {} when it has none."""
    depots = app_state.get('InstalledDepots')
    if depots is None:
        return {}
    if not isinstance(depots, dict):
        raise DepotError('InstalledDepots is not a section')
    out = {}
    for key, entry in depots.items():
        depot = _number(key, 'depot id')
        if not isinstance(entry, dict) or 'manifest' not in entry:
            raise DepotError(f'depot {key} has no manifest entry')
        out[depot] = _number(entry['manifest'], f'manifest gid of depot {key}')
    return out


def manifest_name(depot_id, gid):
    """Return the depotcache file name for a depot and manifest gid."""
    for value in (depot_id, gid):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DepotError(f'not a depot or manifest number: {value!r}')
    return f'{depot_id}_{gid}.manifest'


# Verification.

class Verdict:
    """The result of verify_tree. Every list holds sorted relative names."""

    def __init__(self, pristine, changed, missing, extra):
        self.pristine = pristine
        self.changed = changed
        self.missing = missing
        self.extra = extra

    @property
    def ok(self):
        return not self.changed and not self.missing


def _hash_file(path, size):
    """Hash a file through a no-follow descriptor. Raise OSError or DepotError if it is not a plain file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise DepotError('file changed type or size while reading')
        digest = hashlib.sha1()
        total = 0
        while True:
            block = handle.read(BLOCK)
            if not block:
                break
            total += len(block)
            digest.update(block)
        after = os.fstat(handle.fileno())
        if not stat.S_ISREG(after.st_mode) or after.st_size != size or total != size:
            raise DepotError('file changed type or size while reading')
    return digest.hexdigest()


def _parent_state(root, name, cache):
    """Return 'ok', 'missing' or 'unsafe' for the parent directories of a relative name, using lstat only."""
    parts = name.split('/')[:-1]
    state = 'ok'
    prefix = ''
    for part in parts:
        prefix = f'{prefix}/{part}' if prefix else part
        if prefix in cache:
            state = cache[prefix]
        else:
            try:
                info = os.lstat(os.path.join(root, prefix))
            except FileNotFoundError:
                state = 'missing'
            except OSError:
                state = 'unsafe'
            else:
                state = 'ok' if stat.S_ISDIR(info.st_mode) else 'unsafe'
            cache[prefix] = state
        if state != 'ok':
            return state
    return state


def _check_name(root, name, entries, cache, check_content, on_file):
    """Return 'pristine', 'changed', 'missing' or 'ok-dir' (a matching directory) for one listed name."""
    all_dirs = all(entry.is_directory for entry in entries)
    parent = _parent_state(root, name, cache)
    if parent == 'unsafe':
        return 'changed'
    if parent == 'missing':
        return 'ok-dir-missing' if all_dirs else 'missing'
    path = os.path.join(root, name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return 'ok-dir-missing' if all_dirs else 'missing'
    except OSError:
        return 'changed'
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError:
            return 'changed'
        return 'pristine' if any(e.is_symlink and e.link == target for e in entries) else 'changed'
    if stat.S_ISDIR(mode):
        return 'ok-dir' if any(e.is_directory for e in entries) else 'changed'
    if not stat.S_ISREG(mode):
        return 'changed'
    candidates = [e for e in entries if not e.is_directory and not e.is_symlink and e.size == info.st_size]
    if not candidates:
        return 'changed'
    if not check_content:
        return 'pristine'
    if on_file is not None:
        on_file(name, info.st_size)
    try:
        digest = _hash_file(path, info.st_size)
    except (OSError, DepotError):
        return 'changed'
    return 'pristine' if any(e.sha1 == digest for e in candidates) else 'changed'


def _walk_objects(root, changed):
    """Yield the relative name of every non-directory object under root. Never enters a symlink."""
    stack = ['']
    while stack:
        prefix = stack.pop()
        directory = os.path.join(root, prefix) if prefix else root
        try:
            with os.scandir(directory) as scan:
                children = list(scan)
        except OSError:
            if prefix:
                changed.add(prefix)
                continue
            raise DepotError(f'cannot list {root}') from None
        for child in children:
            rel = f'{prefix}/{child.name}' if prefix else child.name
            try:
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append(rel)
            else:
                yield rel


def as_installed(manifests, root):
    """The manifests with every name spelled the way it is on disk below root.

    Steam on Linux keeps the letter case that a folder or file already has: a game whose developer renamed
    `Game_Data` to `game_Data` goes on living in `Game_Data`, and Steam finds its files there. A name that is on disk
    exactly as listed stays. One that is not, and whose path exists in exactly one other spelling, takes that
    spelling. One that is nowhere keeps the listed spelling below the deepest folder that was found. A name found in
    more than one spelling (and not exactly as listed) is refused: nothing says which one Steam would use.
    Folders are only listed, never entered through a link.
    """
    root = os.fspath(root)
    listings = {}

    def listing(prefix):
        if prefix not in listings:
            exact, folded = set(), {}
            path = os.path.join(root, prefix) if prefix else root
            try:
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    with os.scandir(path) as scan:
                        for child in scan:
                            exact.add(child.name)
                            folded.setdefault(child.name.lower(), []).append(child.name)
            except OSError:
                pass
            listings[prefix] = (exact, folded)
        return listings[prefix]

    def spellings(parts, index, prefix, found):
        """Every path on disk that is parts[index:] below prefix in some letter case; the exact one first."""
        if index == len(parts):
            found.append(prefix)
            return
        exact, folded = listing(prefix)
        part = parts[index]
        for option in ([part] if part in exact else []) + sorted(c for c in folded.get(part.lower(), ()) if c != part):
            spellings(parts, index + 1, f'{prefix}/{option}' if prefix else option, found)
            if len(found) > 1:
                return

    known = {}

    def actual(name):
        if name in known:
            return known[name]
        parts = name.split('/')
        for depth in range(len(parts), 0, -1):
            found = []
            spellings(parts[:depth], 0, '', found)
            if found and found[0] == '/'.join(parts[:depth]):
                found = found[:1]           # on disk exactly as listed: that is the one
            if len(found) > 1:
                raise DepotError(f'{"/".join(parts[:depth])} is on disk in more than one spelling (letter case)')
            if found:
                known[name] = '/'.join([found[0]] + parts[depth:])
                return known[name]
        known[name] = name
        return name

    return [Manifest(manifest.depot_id, manifest.gid, manifest.creation_time,
                     [dataclasses.replace(entry, name=actual(entry.name)) for entry in manifest.files])
            for manifest in manifests]


def verify_tree(manifests, root, *, check_content=True, on_file=None):
    """Compare the folder root against the manifests of the depots a game is installed from."""
    root = os.fspath(root)
    try:
        info = os.lstat(root)
    except OSError as error:
        raise DepotError(f'cannot stat {root}: {error.strerror}') from None
    if not stat.S_ISDIR(info.st_mode):
        raise DepotError(f'root is not a real directory: {root}')
    index = {}
    for manifest in manifests:
        for entry in manifest.files:
            known = index.setdefault(entry.name, [])
            if entry not in known:
                known.append(entry)
    listed = {name for name, entries in index.items() if any(not e.is_directory for e in entries)}
    pristine, changed, missing = [], set(), []
    cache = {}
    for name in sorted(index):
        result = _check_name(root, name, index[name], cache, check_content, on_file)
        if result == 'pristine':
            pristine.append(name)
        elif result == 'changed':
            changed.add(name)
        elif result == 'missing':
            missing.append(name)
    extra = [rel for rel in _walk_objects(root, changed) if rel not in listed]
    return Verdict(sorted(pristine), sorted(changed), sorted(missing), sorted(extra))


# Command line.

def _cmd_manifest(args):
    manifest = read_manifest(args.file)
    total = sum(f.size for f in manifest.files if not f.is_directory and not f.is_symlink)
    print(f'depot {manifest.depot_id}')
    print(f'gid {manifest.gid}')
    print(f'files {len(manifest.files)}')
    print(f'size {total}')
    for entry in manifest.files:
        line = f'{entry.flags:4d} {entry.size:12d} {entry.sha1 or "-" * 40} {entry.name}'
        if entry.link is not None:
            line += f' -> {entry.link}'
        print(line)
    return 0


def _cmd_verify(args):
    manifests = [read_manifest(path) for path in args.files]
    verdict = verify_tree(manifests, args.root)
    print(f'pristine {len(verdict.pristine)}')
    for label in ('changed', 'missing', 'extra'):
        names = getattr(verdict, label)
        print(f'{label} {len(names)}')
        for name in names[:20]:
            print(f'  {name}')
        if len(names) > 20:
            print(f'  ... {len(names) - 20} more')
    return 0 if verdict.ok else 1


def _cmd_vdf(args):
    name, value = read_vdf(args.file)
    print(json.dumps({name: value}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='dosplex-depot', description='Read Steam depot manifests and VDF files.')
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('manifest', help='print the entries of a depot manifest')
    one.add_argument('file')
    one.set_defaults(run=_cmd_manifest)
    two = sub.add_parser('verify', help='compare a game folder with depot manifests')
    two.add_argument('root')
    two.add_argument('files', nargs='+', metavar='FILE')
    two.set_defaults(run=_cmd_verify)
    three = sub.add_parser('vdf', help='print a VDF file as JSON')
    three.add_argument('file')
    three.set_defaults(run=_cmd_vdf)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (DepotError, OSError) as error:
        print(f'dosplex-depot: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
