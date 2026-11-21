#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Apply and undo Dosplex's portrait Wine mode-list setting.

The side supervisor runs this module as root. Wine registry files are always
read and atomically replaced by the account that owns them; the durable
write-ahead journal stays root-owned under /var/lib/dosplex/journal/<uid>.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile
from datetime import datetime, timezone

SECTION = b'[Software\\\\Wine\\\\X11 Driver]'
KEY = b'"EmulateModelist"'
HEADER = b'WINE REGISTRY Version 2'
MAX_REGISTRY = 32 * 1024 * 1024
MAX_VDF = 2 * 1024 * 1024
SCHEMA = 'dosplex.wine-modelist.v1'
# Journals written before the rename (2026-10-29) carry this name; they are read as the current schema and
# carry SCHEMA from their next write on. Without this the policy was skipped for every side on a device that
# had the earlier version, and a fresh prefix never got its mode list (AvP Classic on Guest, 2026-11-10).
LEGACY_SCHEMA = 'duplos.wine-modelist.v1'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _line_section(line):
    if not line[:len(SECTION)].lower() == SECTION.lower():
        return False
    tail = line[len(SECTION):]
    return tail[:1] in (b'', b' ', b'\t', b'\r', b'\n')


def inspect_registry(data):
    """Return safe, unambiguous EmulateModelist state and line locations."""
    if not data.startswith(HEADER) or b'\0' in data or len(data) > MAX_REGISTRY:
        raise ValueError('unsupported Wine registry file')
    lines = data.splitlines(keepends=True)
    sections = []
    keys = []
    active = False
    for index, line in enumerate(lines):
        if line.startswith(b'['):
            active = _line_section(line)
            if active:
                sections.append(index)
        elif active and line[:len(KEY)].lower() == KEY.lower() and line[len(KEY):len(KEY) + 1] == b'=':
            keys.append(index)
    if len(sections) > 1 or len(keys) > 1:
        raise ValueError('ambiguous Wine registry section or value')
    if not keys:
        return {'value': 'absent', 'lines': lines, 'section': sections[0] if sections else None,
                'key': None}
    line = lines[keys[0]]
    body = line.rstrip(b'\r\n')
    match = re.fullmatch(re.escape(KEY) + rb'="((?:[^"\\]|\\.)*)"', body, flags=re.IGNORECASE)
    if not match:
        raise ValueError('unsupported EmulateModelist value')
    raw_value = match.group(1)
    if raw_value == b'Y':
        value = 'Y'
    elif raw_value == b'N':
        value = 'N'
    else:
        value = 'other:' + raw_value.hex()
    return {'value': value, 'lines': lines, 'section': sections[0], 'key': keys[0]}


def _eol(data):
    return b'\r\n' if b'\r\n' in data else b'\n'


def set_registry_value(data):
    state = inspect_registry(data)
    if state['value'] == 'Y':
        return data, state['value'], False, 0
    if state['value'] not in ('absent', 'N'):
        raise ValueError('EmulateModelist has a user-defined value')
    lines = state['lines']
    ending = _eol(data)
    created_section = state['section'] is None
    separator_added = 0
    if state['value'] == 'N':
        index = state['key']
        line = lines[index]
        ending_part = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
        name = line.rstrip(b'\r\n').split(b'=', 1)[0]
        lines[index] = name + b'="Y"' + ending_part
    elif not created_section:
        section = state['section']
        end = section + 1
        while end < len(lines) and not lines[end].startswith(b'['):
            end += 1
        insert = end
        while insert > section + 1 and lines[insert - 1].strip(b'\r\n') == b'':
            insert -= 1
        lines.insert(insert, KEY + b'="Y"' + ending)
    else:
        before_separator = b''.join(lines)
        if data and not before_separator.endswith((b'\n', b'\r')):
            lines.append(ending)
            separator_added += 1
        if data and not b''.join(lines).endswith(ending + ending):
            lines.append(ending)
            separator_added += 1
        lines.extend((SECTION + ending, KEY + b'="Y"' + ending))
    return b''.join(lines), state['value'], created_section, separator_added


def restore_registry_value(data, before, created_section=False, separator_added=0):
    """Undo only a current Y. Return unchanged when a later value took over."""
    state = inspect_registry(data)
    if state['value'] != 'Y':
        return data, 'preserved-later-change'
    lines = state['lines']
    index = state['key']
    if before == 'N':
        line = lines[index]
        ending_part = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
        name = line.rstrip(b'\r\n').split(b'=', 1)[0]
        lines[index] = name + b'="N"' + ending_part
    elif before == 'absent':
        del lines[index]
        if created_section:
            # Remove only the section Dosplex created, and only if it remains
            # empty. Any later Wine/user values keep the section in place.
            section = state['section']
            if section is not None:
                end = section + 1
                while end < len(lines) and not lines[end].startswith(b'['):
                    end += 1
                if all(not line.strip(b'\r\n \t') for line in lines[section + 1:end]):
                    del lines[section]
                    tail = b''.join(lines)
                    if separator_added:
                        ending = _eol(data)
                        suffix = ending * separator_added
                        if not tail.endswith(suffix):
                            raise ValueError('journaled Wine section separator no longer matches')
                        tail = tail[:-len(suffix)]
                        return tail, 'restored'
    else:
        raise ValueError('invalid journaled original value')
    return b''.join(lines), 'restored'


def _tokens_vdf(text):
    tokens = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text[i] in '{}':
            tokens.append(text[i])
            i += 1
            continue
        if text[i] != '"':
            raise ValueError('invalid Steam library VDF token')
        i += 1
        value = []
        while i < len(text):
            char = text[i]
            i += 1
            if char == '"':
                break
            if char == '\\' and i < len(text):
                escaped = text[i]
                i += 1
                value.append('\\' if escaped == '\\' else '"' if escaped == '"' else '\\' + escaped)
            else:
                value.append(char)
        else:
            raise ValueError('unterminated Steam library VDF string')
        tokens.append(''.join(value))
    return tokens


def parse_libraryfolders(data):
    text = data.decode('utf-8-sig')
    tokens = _tokens_vdf(text)
    cursor = 0

    def parse_object():
        nonlocal cursor
        result = {}
        while cursor < len(tokens) and tokens[cursor] != '}':
            key = tokens[cursor]
            cursor += 1
            if cursor >= len(tokens):
                raise ValueError('truncated Steam library VDF')
            if tokens[cursor] == '{':
                cursor += 1
                value = parse_object()
                if cursor >= len(tokens) or tokens[cursor] != '}':
                    raise ValueError('unclosed Steam library VDF object')
                cursor += 1
            elif tokens[cursor] != '}':
                value = tokens[cursor]
                cursor += 1
            else:
                raise ValueError('missing Steam library VDF value')
            if key in result:
                raise ValueError('duplicate Steam library VDF key')
            result[key] = value
        return result

    if not tokens:
        raise ValueError('empty Steam library VDF')
    root = {}
    while cursor < len(tokens):
        key = tokens[cursor]
        cursor += 1
        if cursor >= len(tokens) or tokens[cursor] != '{':
            raise ValueError('invalid Steam library VDF root')
        cursor += 1
        value = parse_object()
        if cursor >= len(tokens) or tokens[cursor] != '}':
            raise ValueError('unclosed Steam library VDF root')
        cursor += 1
        if key in root:
            raise ValueError('duplicate Steam library VDF root')
        root[key] = value
    libraries = root.get('libraryfolders')
    if not isinstance(libraries, dict):
        raise ValueError('Steam library VDF has no libraryfolders object')
    paths = []
    for key, entry in libraries.items():
        if not key.isdigit():
            continue
        value = entry if isinstance(entry, str) else entry.get('path') if isinstance(entry, dict) else None
        if value is not None:
            if not isinstance(value, str) or not value.startswith('/'):
                raise ValueError('invalid Steam library path')
            paths.append(value)
    return paths


def _other_user_home(path, uid):
    for account in pwd.getpwall():
        if account.pw_uid == uid or not account.pw_dir or account.pw_dir == '/':
            continue
        home = Path(account.pw_dir)
        try:
            if path == home or home in path.parents:
                return True
        except (OSError, RuntimeError):
            return True
    return False


def _check_directory(path, uid=None):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError('Steam path component is not a real directory')
    if uid is not None and info.st_uid != uid:
        raise PermissionError('Steam directory has the wrong owner')
    return info


def _safe_regular(path, uid, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1:
            raise PermissionError('Steam metadata file is not a private regular file')
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ValueError('Steam metadata file is too large')
        return bytes(data), info
    finally:
        os.close(fd)


def discover_libraries(home, uid, unavailable=None):
    """Return the account's mounted Steam libraries.

    A library that libraryfolders.vdf lists but that is not present (an SD card
    that is removed) is skipped and reported through `unavailable`, never fatal:
    its prefixes are handled when it returns, and journal entries for it stay
    'library-unavailable' until then. A malformed VDF still fails closed.
    """
    home = Path(home)
    _check_directory(home, uid)
    found = {}
    for candidate in (home / '.local/share/Steam', home / '.steam/steam', home / '.steam/root'):
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if not resolved.is_absolute() or _other_user_home(resolved, uid):
            raise PermissionError('Steam path resolves into another account home')
        if resolved in found:
            continue
        _check_directory(resolved)
        found[resolved] = True
    if not found:
        return []
    libraries = set(found)
    # libraryfolders.vdf is read only. A broken VDF fails the scan closed so
    # a hidden SD-card prefix is never silently omitted from a change.
    for steam_root in tuple(found):
        steamapps = steam_root / 'steamapps'
        try:
            _check_directory(steamapps)
        except FileNotFoundError:
            continue
        vdf = steamapps / 'libraryfolders.vdf'
        try:
            raw, _ = _safe_regular(vdf, uid, MAX_VDF)
        except FileNotFoundError:
            continue
        for raw_path in parse_libraryfolders(raw):
            candidate = Path(raw_path)
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError:
                if unavailable is not None:
                    unavailable.append(str(candidate))
                continue
            if _other_user_home(resolved, uid):
                raise PermissionError('Steam library resolves into another account home')
            _check_directory(resolved)
            libraries.add(resolved)
    return sorted(libraries, key=lambda p: str(p))


def _open_directory_chain(path, create_mode=None):
    """Open an absolute directory without following symlinks in any component.

    With create_mode, a component that does not exist is created with that mode
    instead of failing. Creation is as link-safe as opening: a symlink in the
    way still fails, and components that already exist are never changed.
    """
    path = Path(os.path.normpath(str(path)))
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionError('invalid absolute path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if create_mode is None:
                    raise
                try:
                    os.mkdir(part, create_mode, dir_fd=fd)
                except FileExistsError:
                    pass  # the other side's supervisor created it first
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_user_reg(path, uid):
    path = Path(path)
    parent_fd = _open_directory_chain(path.parent)
    try:
        parent_info = os.fstat(parent_fd)
        if parent_info.st_uid != uid or parent_info.st_mode & 0o022:
            raise PermissionError('Proton prefix directory has unsafe ownership or permissions')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1 or
                    before.st_mode & 0o022):
                raise PermissionError('Wine registry file has unsafe ownership or links')
            chunks = bytearray()
            while len(chunks) <= MAX_REGISTRY:
                chunk = os.read(fd, min(1024 * 1024, MAX_REGISTRY + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > MAX_REGISTRY:
                raise ValueError('Wine registry file is too large')
            os.close(fd)
            return bytes(chunks), before, parent_fd
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        os.close(parent_fd)
        raise


class PrefixActive(RuntimeError):
    """The prefix gained a Wine user between the scan and the replace."""


def _atomic_replace_as_owner(path, uid, before_data, after_data, *, proc_root=Path('/proc')):
    """Replace user.reg under its owner UID, after checking it has not changed.

    The final active-prefix check runs here, in the caller's privilege context.
    The owner-scoped child cannot read the environment of sandboxed processes
    such as a running Steam game, so it must never be the one to decide that a
    prefix is idle.
    """
    path = Path(path)
    if os.path.normpath(str(path.parent)) in active_prefixes(uid, proc_root):
        raise PrefixActive('Wine prefix became active during update')
    if os.geteuid() == uid:
        return _atomic_replace_user_file(path, uid, before_data, after_data)
    if os.geteuid() != 0:
        raise PermissionError('cannot edit a Proton prefix for another user')
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            account = pwd.getpwuid(uid)
            os.setgroups(os.getgrouplist(account.pw_name, account.pw_gid))
            os.setgid(account.pw_gid)
            os.setuid(uid)
            _atomic_replace_user_file(path, uid, before_data, after_data)
            os.write(write_fd, b'OK')
            os._exit(0)
        except BaseException as exc:
            try:
                os.write(write_fd, ('ERR:' + repr(exc)).encode()[:2048])
            finally:
                os._exit(1)
    os.close(write_fd)
    try:
        result = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if status != 0 or result != b'OK':
        raise RuntimeError('owner-scoped registry edit failed: ' + result.decode('utf-8', 'replace'))


def _atomic_replace_user_file(path, uid, before_data, after_data):
    data, before, parent_fd = _read_user_reg(path, uid)
    try:
        if data != before_data:
            raise RuntimeError('Wine registry changed before update')
        temporary = '.dosplex-modelist-' + os.urandom(12).hex()
        temp_exists = False
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     stat.S_IMODE(before.st_mode), dir_fd=parent_fd)
        temp_exists = True
        try:
            os.fchmod(fd, stat.S_IMODE(before.st_mode))
            os.fchown(fd, before.st_uid, before.st_gid)
            view = memoryview(after_data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        current_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            current = os.fstat(current_fd)
            current_data = bytearray()
            while len(current_data) <= MAX_REGISTRY:
                chunk = os.read(current_fd, min(1024 * 1024, MAX_REGISTRY + 1 - len(current_data)))
                if not chunk: break
                current_data.extend(chunk)
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                    before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) or bytes(current_data) != before_data:
                raise RuntimeError('Wine registry changed during update')
        finally:
            os.close(current_fd)
        try:
            os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temp_exists = False
            os.fsync(parent_fd)
        finally:
            if temp_exists:
                try: os.unlink(temporary, dir_fd=parent_fd)
                except OSError: pass
    finally:
        if 'temporary' in locals() and temp_exists:
            try: os.unlink(temporary, dir_fd=parent_fd)
            except OSError: pass
        os.close(parent_fd)


def _version_for(compatdata, uid):
    try:
        raw, _ = _safe_regular(compatdata / 'version', uid, 4096)
    except FileNotFoundError:
        return 'unknown'
    try:
        value = raw.decode('ascii').splitlines()[0].strip()
    except (UnicodeError, IndexError):
        return 'unknown'
    return value if re.fullmatch(r'[A-Za-z0-9._+-]{1,96}', value) else 'unknown'


def enumerate_prefixes(libraries, uid):
    result = {}
    for library in libraries:
        steamapps = library / 'steamapps'
        try:
            _check_directory(steamapps)
            compatdata = steamapps / 'compatdata'
            _check_directory(compatdata, uid)
            compat_fd = _open_directory_chain(compatdata)
        except FileNotFoundError:
            continue
        try:
            with os.scandir(compat_fd) as entries:
                for entry in entries:
                    if not entry.name.isdigit() or not 0 < int(entry.name) < 2**32:
                        continue
                    compat_path = compatdata / entry.name
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        app_info = entry.stat(follow_symlinks=False)
                        if app_info.st_uid != uid:
                            continue
                        pfx = compat_path / 'pfx'
                        _check_directory(pfx, uid)
                        user_reg = pfx / 'user.reg'
                        # A pfx under construction has no user.reg yet. It is
                        # picked up on the next post-exit scan.
                        try:
                            st = user_reg.lstat()
                        except FileNotFoundError:
                            continue
                        if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or
                                st.st_mode & 0o022):
                            raise PermissionError('Wine registry file has unsafe ownership or links: ' + str(user_reg))
                        result[str(user_reg)] = {
                            'path': user_reg, 'app_id': int(entry.name),
                            'library': str(library), 'proton_version': _version_for(compat_path, uid),
                        }
                    except (FileNotFoundError, NotADirectoryError):
                        continue
        finally:
            os.close(compat_fd)
    return result


def _normal_prefix(value):
    if not value:
        return None
    value = os.path.normpath(value)
    if value.endswith('/pfx'):
        return value
    if '/steamapps/compatdata/' in value:
        return os.path.join(value, 'pfx')
    return None


def active_prefixes(uid, proc_root=Path('/proc')):
    active = set()
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError as error:
        raise RuntimeError('cannot inspect processes for active Wine prefixes') from error
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            info = process.stat()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise RuntimeError('cannot inspect process ownership for active Wine prefixes') from error
        if info.st_uid != uid:
            continue
        try:
            raw = (process / 'environ').read_bytes()[:2 * 1024 * 1024]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            # Guessing that a process is idle could corrupt a live prefix.
            raise RuntimeError('cannot inspect an account process for active Wine prefixes') from error
        for item in raw.split(b'\0'):
            if item.startswith((b'WINEPREFIX=', b'STEAM_COMPAT_DATA_PATH=')):
                key, value = item.split(b'=', 1)
                normalized = _normal_prefix(os.fsdecode(value))
                if normalized:
                    active.add(normalized)
    return active


def _ensure_journal_dir(journal_base, uid):
    if os.geteuid() != 0:
        raise PermissionError('Dosplex Wine journal requires the root supervisor')
    base = Path(journal_base)
    # Do not traverse a symlink in any persistent journal path component. A
    # fresh host has no journal directory yet, so create what is missing,
    # private to root; the checks below still refuse an unsafe existing one.
    base_fd = _open_directory_chain(base, create_mode=0o700)
    try:
        info = os.fstat(base_fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('unsafe Dosplex journal directory')
    finally:
        os.close(base_fd)
    owner_dir = base / str(uid)
    try:
        owner_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = owner_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise PermissionError('unsafe account journal directory')
    return owner_dir


def _read_journal(path, uid, account):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'schema': SCHEMA, 'uid': uid, 'account': account, 'updated_at': now(), 'entries': []}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
        raise PermissionError('unsafe Dosplex Wine journal')
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Dosplex Wine journal is too large')
    journal = json.loads(raw)
    if journal.get('schema') == LEGACY_SCHEMA:
        journal['schema'] = SCHEMA
    if (journal.get('schema') != SCHEMA or journal.get('uid') != uid or
            journal.get('account') != account or not isinstance(journal.get('entries'), list) or
            len(journal['entries']) > 4096):
        raise ValueError('invalid Dosplex Wine journal schema')
    for entry in journal['entries']:
        if (not isinstance(entry, dict) or entry.get('uid') != uid or entry.get('account') != account or
                entry.get('key') != 'HKCU\\Software\\Wine\\X11 Driver' or
                entry.get('value_name') != 'EmulateModelist' or entry.get('after') != 'Y' or
                entry.get('before') not in ('absent', 'N') or
                type(entry.get('created_section')) is not bool or
                type(entry.get('separator_added', 0)) is not int or
                entry.get('separator_added', 0) not in (0, 1, 2) or
                entry.get('status') not in ('pending-apply', 'applied', 'pending-restore', 'restored',
                                            'preserved-later-change', 'not-found', 'conflict', 'failed') or
                type(entry.get('app_id')) is not int or not 0 < entry['app_id'] < 2**32 or
                not isinstance(entry.get('path'), str) or not Path(entry['path']).is_absolute() or
                not isinstance(entry.get('library'), str) or not Path(entry['library']).is_absolute() or
                not isinstance(entry.get('proton_version'), str) or len(entry['proton_version']) > 96):
            raise ValueError('invalid Dosplex Wine journal entry')
    return journal


def _save_journal(path, journal):
    journal['updated_at'] = now()
    fd, temporary = tempfile.mkstemp(prefix='.wine-modelist-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(journal, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = _open_directory_chain(path.parent)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _journal_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise PermissionError('unsafe Dosplex Wine journal lock')
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _find_entry(entries, path):
    for entry in reversed(entries):
        if entry['path'] == str(path) and entry['status'] in ('pending-apply', 'applied', 'pending-restore'):
            return entry
    return None


FAILURE_RETRY_LIMIT = 3
FAILURE_WINDOW_SECONDS = 24 * 3600
TERMINAL_STATUSES = ('restored', 'not-found', 'failed', 'conflict', 'preserved-later-change')
JOURNAL_KEEP_PER_PATH = 6


def _recent_failures(entries, key):
    cutoff = datetime.now(timezone.utc).timestamp() - FAILURE_WINDOW_SECONDS
    count = 0
    for entry in entries:
        if entry['path'] != key or entry['status'] != 'failed':
            continue
        try:
            failed_at = datetime.fromisoformat(entry.get('failed_at') or '').timestamp()
        except ValueError:
            failed_at = cutoff + 1
        if failed_at >= cutoff:
            count += 1
    return count


def _prune_journal(journal):
    """Keep the journal bounded: only the latest terminal records per prefix."""
    seen = {}
    kept = []
    for entry in reversed(journal['entries']):
        if entry['status'] in TERMINAL_STATUSES:
            count = seen.get(entry['path'], 0)
            if count >= JOURNAL_KEEP_PER_PATH:
                continue
            seen[entry['path']] = count + 1
        kept.append(entry)
    kept.reverse()
    if len(kept) != len(journal['entries']):
        journal['entries'] = kept
        return True
    return False


def _safe_library_record(entry, libraries):
    library = Path(entry['library'])
    if library not in libraries:
        return False
    expected = library / 'steamapps' / 'compatdata' / str(entry['app_id']) / 'pfx' / 'user.reg'
    return os.path.normpath(str(expected)) == os.path.normpath(entry['path'])


def _apply_one(item, account, uid, entries, journal, journal_path, live_prefixes,
               *, proc_root=Path('/proc')):
    path = item['path']
    key = str(path)
    existing = _find_entry(entries, path)
    if existing and existing['status'] == 'applied' and existing.get('applied_stat'):
        # Periodic scans must not re-read every registry file. An applied file
        # whose inode, size and mtime are unchanged still carries the value.
        try:
            current = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return 'not-found'
        if [current.st_ino, current.st_size, current.st_mtime_ns] == existing['applied_stat']:
            return 'already-applied'
    prefix = os.path.normpath(str(path.parent))
    if prefix in live_prefixes:
        return 'active-prefix-deferred'
    try:
        data, before_info, parent_fd = _read_user_reg(path, uid)
        os.close(parent_fd)
        state = inspect_registry(data)
    except (FileNotFoundError, NotADirectoryError):
        return 'not-found'
    if existing:
        if existing['status'] == 'pending-apply':
            if state['value'] == 'Y':
                existing['status'] = 'applied'
                existing['file_hash_after'] = sha256(data)
                existing['applied_at'] = now()
                _save_journal(journal_path, journal)
                return 'recovered-applied'
            if state['value'] != existing['before']:
                existing['status'] = 'conflict'
                _save_journal(journal_path, journal)
                return 'conflict'
            existing['status'] = 'restored'
            existing['restored_at'] = now()
            _save_journal(journal_path, journal)
            existing = None
        elif existing['status'] == 'pending-restore':
            if state['value'] == 'Y':
                existing['status'] = 'applied'
                existing['applied_at'] = now()
                _save_journal(journal_path, journal)
                return 'already-applied'
            if state['value'] == existing['before']:
                existing['status'] = 'restored'
                existing['restored_at'] = now()
                _save_journal(journal_path, journal)
                existing = None
            else:
                existing['status'] = 'preserved-later-change'
                existing['observed_value'] = state['value']
                existing['restored_at'] = now()
                _save_journal(journal_path, journal)
                return 'preserved-later-change'
        elif state['value'] == 'Y':
            return 'already-applied'
        else:
            existing['status'] = 'preserved-later-change'
            existing['observed_value'] = state['value']
            existing['restored_at'] = now()
            _save_journal(journal_path, journal)
            return 'preserved-later-change'
    if state['value'] == 'Y':
        return 'already-user-enabled'
    if state['value'] not in ('absent', 'N'):
        return 'unsupported-user-value'
    for previous in reversed(entries):
        if previous['path'] == key and previous['status'] == 'preserved-later-change':
            # A later explicit setting owns this value. Do not fight it on
            # subsequent scans; changing/removing that value lets Dosplex
            # manage the prefix again.
            if state['value'] == previous.get('observed_value'):
                return 'preserved-user-setting'
            break
    if _recent_failures(entries, key) >= FAILURE_RETRY_LIMIT:
        return 'failed-retry-limit'
    changed, before_value, created_section, separator_added = set_registry_value(data)
    entry = {
        'id': hashlib.sha256((key + now()).encode()).hexdigest(),
        'account': account, 'uid': uid, 'app_id': item['app_id'],
        'proton_version': item['proton_version'], 'library': item['library'],
        'path': key, 'key': 'HKCU\\Software\\Wine\\X11 Driver',
        'value_name': 'EmulateModelist', 'type': 'REG_SZ',
        'before': before_value, 'after': 'Y', 'created_section': created_section,
        'separator_added': separator_added,
        'file_hash_before': sha256(data), 'file_hash_after': None,
        'timestamp': now(), 'status': 'pending-apply',
    }
    entries.append(entry)
    _save_journal(journal_path, journal)  # write-ahead: durable before touching user.reg
    try:
        _atomic_replace_as_owner(path, uid, data, changed, proc_root=proc_root)
    except PrefixActive:
        # Nothing was written. The prefix is in use; the next scan defers it.
        entry['status'] = 'restored'
        entry['restored_at'] = now()
        _save_journal(journal_path, journal)
        return 'active-prefix-deferred'
    except Exception as error:
        # Record the failure durably, so the next scan neither mistakes it for
        # a crash before write nor retries it without bound.
        entry['status'] = 'failed'
        entry['error'] = str(error)[:300]
        entry['failed_at'] = now()
        _save_journal(journal_path, journal)
        return 'failed'
    after_data, after_info, parent_fd = _read_user_reg(path, uid)
    os.close(parent_fd)
    entry['file_hash_after'] = sha256(after_data)
    entry['applied_stat'] = [after_info.st_ino, after_info.st_size, after_info.st_mtime_ns]
    entry['status'] = 'applied'
    entry['applied_at'] = now()
    _save_journal(journal_path, journal)
    return 'applied'


def _restore_one(entry, account, uid, journal, journal_path, live_prefixes,
                 discovered_libraries, discovered_paths, *, proc_root=Path('/proc')):
    if entry['status'] not in ('pending-apply', 'applied', 'pending-restore'):
        return entry['status']
    if not _safe_library_record(entry, discovered_libraries):
        return 'library-unavailable'
    path = Path(entry['path'])
    if os.path.normpath(str(path.parent)) in live_prefixes:
        return 'active-prefix-deferred'
    if str(path) not in discovered_paths:
        # A mounted library with no matching prefix means it was removed. An
        # unavailable library is handled above and remains recoverable.
        entry['status'] = 'not-found'
        entry['restored_at'] = now()
        _save_journal(journal_path, journal)
        return 'not-found'
    try:
        data, _, parent_fd = _read_user_reg(path, uid)
        os.close(parent_fd)
        state = inspect_registry(data)
    except (FileNotFoundError, NotADirectoryError):
        entry['status'] = 'not-found'
        entry['restored_at'] = now()
        _save_journal(journal_path, journal)
        return 'not-found'
    if entry['status'] in ('pending-restore', 'pending-apply') and state['value'] == entry['before']:
        # A pending apply that never reached the file, or a pending restore
        # that completed, leaves the original value in place: nothing to undo.
        entry['status'] = 'restored'
        entry['restored_at'] = now()
        _save_journal(journal_path, journal)
        return 'restored'
    if state['value'] != 'Y':
        entry['status'] = 'preserved-later-change'
        entry['observed_value'] = state['value']
        entry['restored_at'] = now()
        _save_journal(journal_path, journal)
        return 'preserved-later-change'
    restored, outcome = restore_registry_value(data, entry['before'], entry['created_section'],
                                                entry.get('separator_added', 0))
    entry['status'] = 'pending-restore'
    entry['restore_hash_before'] = sha256(data)
    entry['restore_started_at'] = now()
    _save_journal(journal_path, journal)  # write-ahead restore intent
    _atomic_replace_as_owner(path, uid, data, restored, proc_root=proc_root)
    entry['status'] = outcome
    entry['file_hash_restored'] = sha256(restored)
    entry['restored_at'] = now()
    _save_journal(journal_path, journal)
    return outcome


def reconcile(owner, home, portrait, *, journal_base=Path('/var/lib/dosplex/journal'),
              proc_root=Path('/proc')):
    """Reconcile all of one side account's prefixes with the current monitor shape."""
    if os.geteuid() != 0:
        raise PermissionError('Wine mode-list reconciliation must run in the root supervisor')
    account = pwd.getpwnam(owner)
    uid = account.pw_uid
    if uid < 1000 or Path(home) != Path(account.pw_dir):
        raise PermissionError('unexpected Steam account home')
    owner_dir = _ensure_journal_dir(journal_base, uid)
    journal_path = owner_dir / 'wine-modelist.json'
    lock_fd = _journal_lock(owner_dir / 'wine-modelist.lock')
    try:
        journal = _read_journal(journal_path, uid, owner)
        if _prune_journal(journal):
            _save_journal(journal_path, journal)
        unavailable = []
        try:
            libraries = discover_libraries(Path(home), uid, unavailable)
            prefixes = enumerate_prefixes(libraries, uid)
        except (OSError, ValueError, UnicodeError) as error:
            # Discovery errors fail closed: no prefix is edited on a partial scan.
            return {'portrait': bool(portrait), 'libraries': 0, 'prefixes': 0,
                    'error': str(error), 'results': {}, 'unavailable_libraries': unavailable}
        live = active_prefixes(uid, proc_root)
        results = {}
        entries = journal['entries']
        if portrait:
            for item in prefixes.values():
                result = _apply_one(item, owner, uid, entries, journal, journal_path, live,
                                    proc_root=proc_root)
                if result != 'already-applied' and result != 'already-user-enabled':
                    results[str(item['path'])] = result
        else:
            for entry in entries:
                result = _restore_one(entry, owner, uid, journal, journal_path, live,
                                      set(libraries), set(prefixes), proc_root=proc_root)
                if result not in ('restored', 'preserved-later-change', 'not-found'):
                    results[entry['path']] = result
        return {'portrait': bool(portrait), 'libraries': len(libraries),
                'prefixes': len(prefixes), 'error': None, 'results': results,
                'unavailable_libraries': unavailable}
    finally:
        os.close(lock_fd)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('owner')
    parser.add_argument('home')
    parser.add_argument('--portrait', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(reconcile(args.owner, args.home, args.portrait), sort_keys=True))
    except Exception as error:
        print(json.dumps({'error': str(error)}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
