#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Undo only journaled, still-owned Wine mode-list values after sides stop.

Development recovery only. Never restore an entire registry or alter another
prefix. The per-account write runs without root privileges. Later user changes
are preserved. The caller must stop all trial side services before invoking it.
"""
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile
import importlib.util

SECTION = b'[Software\\\\Wine\\\\X11 Driver]'
VALUE = b'"EmulateModelist"'
OWNED = VALUE + b'="Y"'
PRODUCT_CANDIDATES = (
    Path(__file__).resolve().parents[1] / 'dosplex-wine-modelist.py',      # a source checkout: one folder up
    Path(__file__).resolve().parents[1] / 'bin/dosplex-wine-modelist.py',
)
PRODUCT_PATH = next((path for path in PRODUCT_CANDIDATES if path.is_file()), None)
if PRODUCT_PATH is None:
    raise FileNotFoundError('DosPlex Wine mode-list helper is missing')
PRODUCT_SPEC = importlib.util.spec_from_file_location('wine_modelist', PRODUCT_PATH)
PRODUCT = importlib.util.module_from_spec(PRODUCT_SPEC)
PRODUCT_SPEC.loader.exec_module(PRODUCT)


def without_owned_value(data):
    if not data.startswith(b'WINE REGISTRY Version 2') or b'\0' in data:
        raise ValueError('unexpected Wine registry format')
    lines = data.splitlines(keepends=True)
    active = False
    sections = 0
    indices = []
    for i, line in enumerate(lines):
        if line.startswith(b'['):
            active = line.startswith(SECTION) and line[len(SECTION):len(SECTION)+1] in (b' ', b'\n', b'\r', b'')
            sections += int(active)
        elif active and line.startswith(VALUE + b'='):
            indices.append(i)
    if sections > 1 or len(indices) > 1:
        raise ValueError('ambiguous Wine registry value')
    if not indices or lines[indices[0]].rstrip(b'\r\n') != OWNED:
        return data  # Already absent or independently edited: preserve it.
    del lines[indices[0]]
    return b''.join(lines)


def restore_file(target, uid):
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1:
            raise PermissionError('invalid Wine registry file')
        data = stream.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('oversized Wine registry file')
    result = without_owned_value(data)
    if result == data:
        return
    fd, name = tempfile.mkstemp(prefix='.dosplex-modelist-restore-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode))
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        now = target.lstat()
        if (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size) != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size):
            raise RuntimeError('Wine registry changed during recovery')
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)


def locate_legacy_prefix(home, uid, app_id, expected_library=None):
    """Resolve old app-only records across libraries; refuse ambiguous copies."""
    libraries = PRODUCT.discover_libraries(Path(home), uid)
    if not libraries:
        raise FileNotFoundError('no mounted Steam library is available')
    if expected_library is not None and Path(expected_library) not in libraries:
        raise FileNotFoundError('journaled Steam library is not mounted')
    prefixes = PRODUCT.enumerate_prefixes(libraries, uid)
    matches = [item for item in prefixes.values() if item['app_id'] == app_id and
               (expected_library is None or item['library'] == expected_library)]
    if len(matches) > 1:
        raise ValueError('ambiguous compatdata app in multiple Steam libraries')
    return (Path(matches[0]['path']), matches[0]['library']) if matches else (None, None)


def restore(root):
    journal = root / 'wine-modelist-recovery.json'
    for path in (root, journal, root / 'transaction.json'):
        info = path.lstat()
        if info.st_uid or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise PermissionError('untrusted Wine recovery metadata')
    tx = json.loads((root / 'transaction.json').read_text())
    records = json.loads(journal.read_text())
    if not isinstance(records, list) or len(records) > 32:
        raise ValueError('invalid Wine recovery records')
    for record in records:
        if (record['owner'] not in (tx['main_user'], tx['guest_user']) or
                type(record['app']) is not int or not 0 < record['app'] < 2**32 or
                record['key'] != 'HKCU\\Software\\Wine\\X11 Driver' or
                record['value'] != 'EmulateModelist' or record['before'] != 'absent' or
                record['candidate'] != 'Y' or record['type'] != 'REG_SZ'):
            raise ValueError('unsupported Wine recovery record')
        account = pwd.getpwnam(record['owner'])
        registry, library = locate_legacy_prefix(account.pw_dir, account.pw_uid,
                                                  record['app'], record.get('library'))
        if registry is None:
            record['status'] = 'not-found'
            continue
        prefix = registry.parent
        # Wine writes user.reg on exit. Refuse to race a surviving prefix user.
        for process in Path('/proc').iterdir():
            if not process.name.isdigit(): continue
            try:
                if process.stat().st_uid != account.pw_uid: continue
                env = (process / 'environ').read_bytes().split(b'\0')
                if b'WINEPREFIX=' + os.fsencode(prefix) in env:
                    raise RuntimeError('Wine prefix still in use; retry recovery after exit')
            except (FileNotFoundError, ProcessLookupError): pass
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups(os.getgrouplist(account.pw_name, account.pw_gid))
                os.setgid(account.pw_gid)
                os.setuid(account.pw_uid)
                restore_file(registry, account.pw_uid)
                os._exit(0)
            except BaseException:
                os._exit(1)
        if os.waitpid(pid, 0)[1] != 0:
            raise RuntimeError('Wine mode-list restoration failed; retained journal')
        record['status'] = 'owned value removed or later change preserved'
    journal.write_text(json.dumps(records, indent=2) + '\n')
