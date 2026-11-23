#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Move games into the shared game store, and out of it again (ADR-023, T-606 step 2).

The store (dosplex-game-store.py) holds each game once. This module is the one thing that changes it:

    share      slow, safe while Steam runs: verify a game of one account against Steam's depot manifests and write
               a root-owned record of what was seen (<root>/pending/<appid>.json)
    commit     quick, only in a slot (no Steam runs, libraries unmounted): move what the record names into the
               store, hand the other accounts a manifest that asks Steam to validate the game, remove games that
               every account has uninstalled
    restore    leaving: give every stored game back to the account that shared it

Only files that match the depot manifests go to the store. Saves, settings and anything changed stay with the
account. Between share and commit nothing is trusted: every path of an account is walked with directory
descriptors that never follow a link, every file is compared with what was seen (type, link count, device, inode,
size, times) while it is held under a read lease, and a game that changed is rolled back untouched.

    dosplex-game-store-tidy.py share --root R --user U --users a,b --app ID
    dosplex-game-store-tidy.py commit --root R --users a,b
    dosplex-game-store-tidy.py restore --root R --users a,b
    dosplex-game-store-tidy.py pending --root R
    dosplex-game-store-tidy.py shareable --root R --user U --users a,b

Standard library only.
"""
import argparse
import contextlib
import datetime
import errno
import fcntl
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time


def _load_store():
    spec = importlib.util.spec_from_file_location('dosplex_game_store', Path(__file__).with_name('dosplex-game-store.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load_store()
depot = store.depot_module()

RECORD_VERSION = 1
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
APPID = re.compile(r'[0-9]{1,10}')
# Keys Steam keeps while it works on a game; a stored game is finished and idle.
TRANSIENT_KEYS = ('FullValidateBeforeNextUpdate', 'UpdateResult', 'BytesToDownload', 'BytesDownloaded',
                  'BytesToStage', 'BytesStaged')


class TidyError(RuntimeError):
    """A refusal with a message for the person."""


class _Changed(TidyError):
    """Something is not as it was when the game was read; the game is rolled back."""


# Small helpers that never follow a link.

def _appid(value):
    value = str(value)
    if not APPID.fullmatch(value):
        raise TidyError(f'{value!r} is not an app id')
    return value


def _installdir(value):
    """The install folder must be one plain path component."""
    if not isinstance(value, str) or not value or '/' in value or '\0' in value or value in ('.', '..'):
        raise TidyError(f'has an install folder ({value!r}) that is not a plain folder name')
    return value


def _parts(name):
    parts = name.split('/') if isinstance(name, str) else ['']
    if any(part in ('', '.', '..') or '\0' in part for part in parts):
        raise TidyError(f'{name!r} is not a plain relative name')
    return parts


def _is_whiteout(info):
    """An overlay whiteout: a character device 0/0 that hides what is below it."""
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def _device(path):
    """The device of the filesystem a path is on (a link at the end is followed: it is a folder that is asked about)."""
    return os.stat(path).st_dev


def _same_disk(path, other):
    return _device(path) == _device(other)


def _chown(fd, uid, gid, name=None):
    """Give a file or folder to uid:gid. With a name, fd is the folder that holds it (a link is not followed).

    Does nothing unless the process is root, so that the module can be exercised without privileges.
    """
    if os.geteuid() != 0:
        return
    if name is None:
        os.fchown(fd, uid, gid)
    else:
        os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)


def _strip_overlay(target):
    """Remove the overlay's own attributes (trusted.overlay.*) from an open file or folder."""
    try:
        names = os.listxattr(target)
    except OSError:
        return
    for name in names:
        if name.startswith('trusted.overlay.'):
            try:
                os.removexattr(target, name)
            except OSError as error:
                if error.errno not in (errno.ENOTSUP, errno.EPERM, errno.ENODATA):
                    raise


def _enter(fd, name, create=False, mode=0o755, owner=None, replace=False):
    """Open the folder name below fd without following a link. With create, a missing folder is made.

    A file or link where the folder should be is an error, unless replace says it may be removed.
    """
    if create:
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            if not replace:
                raise _Changed(f'{name} is not a folder')
            os.unlink(name, dir_fd=fd)
            info = None
        if info is None:
            os.mkdir(name, mode, dir_fd=fd)
            new = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.fchmod(new, mode)
            if owner is not None:
                _chown(new, *owner)
            return new
    try:
        return os.open(name, DIR_FLAGS, dir_fd=fd)
    except OSError as error:
        raise _Changed(f'{name} is missing, a link or not a folder ({error.strerror})') from None


def _walk(fd, parts, *, create=False, mode=0o755, owner=None, replace=False):
    """Open the folder at parts below fd, one component at a time. Returns a new descriptor; fd stays open."""
    current = os.dup(fd)
    try:
        for part in parts:
            following = _enter(current, part, create, mode, owner, replace)
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    return current


def _open_dir(path):
    """Open an absolute path with every component opened without following a link."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise TidyError(f'{path} is not an absolute path without ".."')
    top = os.open('/', DIR_FLAGS)
    try:
        return _walk(top, path.parts[1:])
    finally:
        os.close(top)


def _stat_at(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _lstat_below(base, parts):
    """lstat of base/parts without following a link; None when it is not there or a link is in the way."""
    try:
        top = _open_dir(base)
    except (OSError, TidyError):
        return None
    try:
        parent = _walk(top, parts[:-1])
        try:
            return _stat_at(parent, parts[-1])
        finally:
            os.close(parent)
    except (OSError, TidyError):
        return None
    finally:
        os.close(top)


def _is_regular(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not _is_whiteout(info)


def _lstat_below_fd(fd, parts):
    """lstat of parts below an open folder without following a link; None when it is not there."""
    try:
        parent = _walk(fd, parts[:-1])
    except (OSError, TidyError):
        return None
    try:
        return _stat_at(parent, parts[-1])
    finally:
        os.close(parent)


def _read_at(fd, name, limit=16 * 1024 * 1024):
    """The bytes of a regular file below fd, or None when it is not one."""
    info = _stat_at(fd, name)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return None
    try:
        handle = os.open(name, FILE_FLAGS, dir_fd=fd)
    except OSError:
        return None
    with os.fdopen(handle, 'rb') as stream:
        return stream.read(limit + 1)


def _put_file(fd, name, data, mode, owner=None):
    """Write a file below fd through a temporary name and a rename, so a reader sees all or nothing."""
    temporary = f'.{name}.{os.getpid()}.tmp'
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if owner is not None:
                _chown(stream.fileno(), *owner)
            os.fsync(stream.fileno())
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=fd)
        raise


def _remove_entry(fd, name):
    """Delete a file, link or folder tree below fd. Nothing is followed."""
    info = _stat_at(fd, name)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=fd)
        return
    child = os.open(name, DIR_FLAGS, dir_fd=fd)
    try:
        for entry in os.listdir(child):
            _remove_entry(child, entry)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=fd)


def _remove_empty_tree(fd, name):
    """Remove a folder tree that holds no file; refuse (and leave the files) when there is one."""
    child = os.open(name, DIR_FLAGS, dir_fd=fd)
    try:
        for entry in os.listdir(child):
            info = _stat_at(child, entry)
            if info is None:
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise TidyError(f'{name}/{entry} is still there')
            _remove_empty_tree(child, entry)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=fd)


def _names_below(fd, prefix=''):
    """Relative names of every non-folder below fd, sorted. Nothing is followed."""
    found = []
    for entry in sorted(os.listdir(fd)):
        info = _stat_at(fd, entry)
        if info is None:
            continue
        name = f'{prefix}{entry}'
        if stat.S_ISDIR(info.st_mode):
            child = os.open(entry, DIR_FLAGS, dir_fd=fd)
            try:
                found.extend(_names_below(child, name + '/'))
            finally:
                os.close(child)
        else:
            found.append(name)
    return found


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# A share record names every file of a game with what was seen of it: Left 4 Dead 2 has 59233 files and its record
# was larger than 16 MiB, so the commit could not read it and the game was never taken (seen on the Deck, p57, p59).
RECORD_LIMIT = 512 * 1024 * 1024


def _json_at(fd, name):
    data = _read_at(fd, name, RECORD_LIMIT)
    if data is None:
        raise TidyError(f'{name} is not a readable file of at most {RECORD_LIMIT >> 20} MiB')
    try:
        value = json.loads(data)
    except ValueError:
        raise TidyError(f'{name} is not valid JSON') from None
    if not isinstance(value, dict):
        raise TidyError(f'{name} is not a JSON object')
    return value


def _as_user(entry, work):
    """Run work() with the account's identity. A refusal inside comes back as a TidyError."""
    def guarded():
        try:
            return {'ok': work()}
        except (TidyError, OSError, ValueError) as error:
            return {'refused': str(error)}
    try:
        reply = store.as_user(entry, guarded)
    except store.StoreError as error:
        raise TidyError(str(error)) from None
    if 'refused' in reply:
        raise TidyError(reply['refused'])
    return reply['ok']


def _layout_dir(layout, name, create=False, mode=0o755):
    """A folder of the store's own root (apps, pending, staging); returns an open descriptor."""
    top = _open_dir(layout.root)
    try:
        return _walk(top, [name], create=create, mode=mode)
    finally:
        os.close(top)


def _check_user(user, users):
    if user not in users:
        raise TidyError(f'{user} is not an account of the store')


# Reading what an account has installed.

def _libraries(layout, entry, user):
    """The account's own Steam library folders, the default one first. The folders of the earlier layout (DosPlex's own
    library overlays) are none of them, for as long as they are still on the list."""
    steam = store.steam_root_of(entry)
    retired = (layout.primary or layout).retired_libraries(user)
    paths = [steam]
    try:
        _name, folders = depot.read_vdf(steam / 'steamapps' / 'libraryfolders.vdf')
    except (OSError, ValueError):
        folders = {}
    for value in folders.values():
        raw = value.get('path') if isinstance(value, dict) else None
        if isinstance(raw, str) and '\0' not in raw:
            path = Path(raw)
            if path.is_absolute() and '..' not in path.parts:
                paths.append(path)
    found = []
    for path in paths:
        if path not in retired and path not in found:
            found.append(path)
    return found


def _folder_of(layout, user, library):
    """The steamapps folder of one of the account's libraries as its own files are seen: the overlay covers the
    steamapps folder of the account's library on this disk, and its real folder is bound at the side's reach while it
    does. A share that read the merged folder would take the store's files for the account's own."""
    side = layout.side(user)
    if side.library is not None and Path(library) == side.library and store.is_mount_point(side.reach):
        return side.reach
    return Path(library) / 'steamapps'


def _steam_uses(steam):
    """{appid: library path} from the "apps" list of each library folder in the account's libraryfolders.vdf: the
    copy of a game that Steam itself uses. An app Steam lists nowhere is not in it."""
    try:
        _name, folders = depot.read_vdf(steam / 'steamapps' / 'libraryfolders.vdf')
    except (OSError, ValueError, depot.DepotError):
        return {}
    uses = {}
    for value in folders.values():
        if not isinstance(value, dict) or not isinstance(value.get('path'), str) or '\0' in value['path']:
            continue
        apps = value.get('apps')
        if isinstance(apps, dict):
            for appid in apps:
                uses.setdefault(str(appid), Path(value['path']))
    return uses


def _candidates(layout, user, entry):
    """(appid, source, library) of every game the account has a manifest for in its own libraries on this disk. The
    source is always 'library': what the account's Steam wrote into its own library, an update of a stored game
    included, is its own; a game that only the store holds is not listed."""
    found = {}
    # The library the store is merged into comes last: a manifest in it can be only what the account's Steam wrote over
    # the store's copy, and a whole copy in another library of the account is the one to share.
    own = layout.side(user).library
    libraries = _libraries(layout, entry, user)
    for lib in [item for item in libraries if item != own] + [item for item in libraries if item == own]:
        # The store takes games by rename: only from the filesystem it is on. Another disk has a store of its own.
        try:
            if not _same_disk(lib, layout.root):
                continue
        except OSError:
            continue
        folder = _folder_of(layout, user, lib)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            match = re.fullmatch(r'appmanifest_([0-9]{1,10})\.acf', name)
            if match and _is_regular(folder / name) and match.group(1) not in found:
                found[match.group(1)] = ('library', lib)
    return [(appid, source, library) for appid, (source, library) in sorted(found.items(), key=lambda item: int(item[0]))]


def account_libraries(layout, user, users):
    """The account's own Steam libraries (none of the earlier layout's), as [{'path', 'dev'}]; dev is the device of
    the library folder, and a library without a steamapps folder is left out. Read as the account."""
    _check_user(user, users)
    entry = store.account(user)

    def look():
        found = []
        for path in _libraries(layout, entry, user):
            try:
                if (path / 'steamapps').is_dir():
                    found.append({'path': str(path), 'dev': _device(path)})
            except OSError:
                continue
        return found
    return _as_user(entry, look)


def _stored_one(layout, appid):
    try:
        fd = _layout_dir(layout, 'apps')
    except (OSError, TidyError):
        return None
    try:
        if _stat_at(fd, f'{appid}.json') is None:
            return None
        return _json_at(fd, f'{appid}.json')
    finally:
        os.close(fd)


def _depot_map(value):
    return {str(key): str(item) for key, item in dict(value or {}).items()}


def _kind(stored_app, buildid, depots):
    """new, join or update, or a refusal, from what the store holds for this app (its record, or None).

    The account's own library holds files that the store's copy shows through, so one folder can be a whole copy of
    the game (a join: the files that match are dropped) or only what the account's Steam wrote over the store's copy
    (an update to take over, or nothing). The same build is a join, and shares only if there is a file to join.

    The record is read by root and handed in: on a disk store the account cannot walk to the store's own folders,
    and a step run as the account would take every game for new (seen on the Deck, p57)."""
    if stored_app is None:
        return 'new'
    if 'error' in stored_app:
        raise TidyError(f'cannot be compared with the store: {stored_app["error"]}')
    held = str(stored_app.get('buildid', ''))
    if held == buildid:
        if _depot_map(stored_app.get('depots')) == _depot_map(depots):
            return 'join'
        raise TidyError(f'does not match the store: it holds build {held} and this copy is build {buildid}')
    if held.isdigit() and int(buildid) > int(held):
        return 'update'
    raise TidyError(f'does not match the store: it holds build {held} and this copy is build {buildid}')


def _folder_key(installdir):
    """Two install folders are the same folder when they differ only in letter case: SteamOS game folders can be
    casefolded, and the store must not hold two apps in what the disk sees as one folder."""
    return installdir.casefold()


def _sharers(records, appid, installdir):
    """The app ids, other than `appid`, whose stored records (appid -> record) name the same install folder."""
    key = _folder_key(installdir)
    return sorted((other for other, record in records.items()
                   if other != appid and isinstance(record, dict) and 'error' not in record
                   and isinstance(record.get('installdir'), str) and _folder_key(record['installdir']) == key),
                  key=int)


def _named(records, appid):
    record = records.get(appid)
    name = record.get('name') if isinstance(record, dict) else None
    return f'{name} ({appid})' if isinstance(name, str) and name else f'game {appid}'


def _require_alone(records, appid, installdir):
    """Refuse when another stored app has the same install folder: what is done to the folder would be done to both."""
    others = _sharers(records, appid, installdir)
    if others:
        raise TidyError(f'shares its install folder with {_named(records, others[0])}, which is stored already, '
                        f'and is left alone')


def _account_folders(layout, user, candidates):
    """appid -> install folder named by each app manifest of the account, read as the account. A manifest that cannot
    be read names nothing: it is refused on its own account when its game is looked at."""
    found = {}
    for appid, _source, library in candidates:
        try:
            raw = depot._read_regular(_folder_of(layout, user, library) / f'appmanifest_{appid}.acf', 16 * 1024 * 1024,
                                      'app manifest')
            _section, state = depot.vdf_loads(raw.decode('utf-8'))
        except (OSError, ValueError, depot.DepotError):
            continue
        if isinstance(state.get('installdir'), str) and state['installdir']:
            found[appid] = state['installdir']
    return found


def _inspect(layout, user, entry, appid, source, library, facts, held, folders=None):
    """Everything that is cheap to check about one game: manifest, depots, place. Raises TidyError to refuse.
    `folders` is appid -> install folder for all of the account's manifests (see _account_folders)."""
    steam = store.steam_root_of(entry)
    if not _same_disk(library, layout.root):
        raise TidyError('is on another disk; the store can only take games from the disk it is on')
    steamapps = _folder_of(layout, user, library)
    manifest_path = steamapps / f'appmanifest_{appid}.acf'
    # One game in two of the account's libraries (an old copy that a move to another disk left behind, with its app
    # manifest): Steam lists the one it uses, and only that one is shared. Each disk's store looks only at its own
    # disk, and on the Steam Machine both stores took Crimsonland, so that the account's Steam found it in two of its
    # libraries (2026-11-17).
    used = _steam_uses(steam).get(appid)
    if used is not None and used != library:
        raise TidyError(f'is a second copy: Steam uses the one in {used}, and this one is left alone')
    raw = depot._read_regular(manifest_path, 16 * 1024 * 1024, 'app manifest')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise TidyError('has an app manifest that is not valid text') from None
    try:
        section, state = depot.vdf_loads(text)
    except depot.DepotError as error:
        raise TidyError(f'has an app manifest that cannot be read ({error})') from None
    facts['name'] = state.get('name', '') if isinstance(state.get('name'), str) else ''
    if section.lower() != 'appstate' or state.get('appid') != appid:
        raise TidyError('has an app manifest that does not belong to it')
    if state.get('StateFlags') != '4':
        raise TidyError(f'is not fully installed (StateFlags {state.get("StateFlags")})')
    installdir = _installdir(state.get('installdir'))
    # Steam lets several apps use one folder (a game and its add-ons). The store keeps one app to a folder: a second
    # one would overwrite the first's files, and an update or a delete of one would take the other's files with it.
    _require_alone(held, appid, installdir)
    mates = sorted((other for other, name in (folders or {}).items()
                    if other != appid and _folder_key(name) == _folder_key(installdir)), key=int)
    if mates:
        raise TidyError(f'shares its install folder with game {mates[0]}, which is installed for this account too; '
                        f'neither is taken, and both are left alone')
    buildid = state.get('buildid')
    if not isinstance(buildid, str) or not buildid.isascii() or not buildid.isdigit():
        raise TidyError('has no build number')
    try:
        depots = depot.installed_depots(state)
    except depot.DepotError as error:
        raise TidyError(f'has damaged depot information ({error})') from None
    if not depots:
        raise TidyError('has no installed depots')
    paths = [steam / 'depotcache' / depot.manifest_name(number, gid) for number, gid in sorted(depots.items())]
    missing = [path for path in paths if not _is_regular(path)]
    if missing:
        raise TidyError(f'waits for its file list from Steam ({len(missing)} of {len(paths)} depot manifests are not '
                        f'cached); Steam fetches it at its next start after DosPlex started')
    if held.get(appid) is not None and not _overlay_mounted(layout, user, library):
        raise TidyError('is in a store and the account\'s library is not merged with it right now')
    folder = Path(library) / 'steamapps' / 'common' / installdir
    base = steamapps / 'common' / installdir
    kind = _kind(held.get(appid), buildid, depots)
    if kind == 'join' and not _has_files(base):
        raise TidyError(f'is already shared (build {buildid})')
    return {'source': source, 'library': str(library), 'folder': folder, 'base': base, 'acf': text,
            'name': facts['name'] or installdir, 'installdir': installdir, 'buildid': buildid, 'depots': depots,
            'manifest_paths': paths, 'kind': kind}


def _has_files(folder):
    """Whether a folder holds a file or link of its own anywhere below it (a whiteout is not one)."""
    for top, _folders, names in os.walk(folder):
        for name in names:
            try:
                if not _is_whiteout(os.lstat(os.path.join(top, name))):
                    return True
            except OSError:
                continue
    return False


def _overlay_mounted(layout, user, library):
    """Whether the overlay is on the steamapps folder of this library of the account."""
    side = layout.side(user)
    if side.library is None or Path(library) != side.library:
        return True         # a library the store is not merged into: nothing of it is missing
    return store.is_mounted(side.steamapps)


def _link_inside(name, link):
    """A link is shared only if it is relative and, resolved lexically from its own folder, stays in the game."""
    if not link or link.startswith('/') or '\0' in link:
        return False
    depth = len(name.split('/')) - 1
    for part in link.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _identity(info):
    return (stat.S_IFMT(info.st_mode), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _prepare(layout, user, entry, appid, progress, held):
    """The slow part of share, run as the account: read the game, hash it, describe what moves."""
    facts = {}
    candidates = _candidates(layout, user, entry)
    side = layout.side(user)
    hits = [item for item in candidates if item[0] == appid]
    source = library = None
    if hits:
        _app, source, library = hits[0]
    else:
        raise TidyError('is not installed for this account')
    plan = _inspect(layout, user, entry, appid, source, library, facts, held,
                    _account_folders(layout, user, candidates))
    try:
        manifests = [depot.read_manifest(path) for path in plan['manifest_paths']]
    except depot.DepotError as error:
        raise TidyError(f'has a depot manifest that cannot be read ({error})') from None
    for path in (plan['folder'], plan['base']):
        probe = _open_dir(path)   # a link in the way, or a missing folder, is a refusal
        os.close(probe)
    # From here on a name is the one on disk. Steam keeps the letter case a folder already has, so a game can live in
    # `Game_Data` while its list says `game_Data`. Taken by the listed names, such a game was torn in two: the store
    # got an empty `game_Data`, the files stayed with the account, and the other account downloaded them again
    # (seen on the Deck, p57: Thronefall, Project Zomboid).
    try:
        manifests = depot.as_installed(manifests, plan['folder'])
    except depot.DepotError as error:
        raise TidyError(f'cannot be compared with its depot manifests ({error})') from None
    names = sorted({item.name for manifest in manifests for item in manifest.files if not item.is_directory})
    folders = sorted({item.name for manifest in manifests for item in manifest.files if item.is_directory})
    before = {}
    for name in names:
        try:
            before[name] = os.lstat(os.path.join(plan['base'], name))
        except OSError:
            continue
    try:
        verdict = depot.verify_tree(manifests, plan['folder'], on_file=progress)
    except depot.DepotError as error:
        raise TidyError(f'cannot be compared with its depot manifests ({error})') from None
    if not verdict.pristine:
        raise TidyError('has no file that matches its depot manifests')
    files, unshared, size = [], 0, 0
    for name in verdict.pristine:
        first = before.get(name)
        if first is None:
            continue            # in the store only (a layer game): nothing to move
        try:
            second = os.lstat(os.path.join(plan['base'], name))
        except OSError:
            unshared += 1
            continue
        link = None
        if stat.S_ISLNK(first.st_mode):
            link = os.readlink(os.path.join(plan['base'], name))
            if not _link_inside(name, link):
                unshared += 1
                continue
        elif not stat.S_ISREG(first.st_mode) or first.st_nlink != 1:
            unshared += 1
            continue
        if _identity(first) != _identity(second) or first.st_uid != entry.pw_uid:
            unshared += 1
            continue
        item = {'name': name, 'size': first.st_size, 'mtime_ns': first.st_mtime_ns, 'ctime_ns': first.st_ctime_ns,
                'ino': first.st_ino, 'dev': first.st_dev, 'mode': stat.S_IMODE(first.st_mode),
                'executable': bool(first.st_mode & 0o100)}
        if link is not None:
            item['link'] = link
        else:
            size += first.st_size
        files.append(item)
    stored_here = len(verdict.pristine) - len(files) - unshared
    if not files and not stored_here:
        raise TidyError('has no file that can be shared')
    if plan['kind'] == 'join' and not files:
        # The same build as the store's, and nothing of the account's own to take: the files it shows are the store's.
        raise TidyError(f'is already shared (build {plan["buildid"]})')
    if plan['kind'] == 'join':
        # A second copy only joins a stored one that spells every name the same way. Otherwise the account would
        # see the game under two spellings at once, and a Windows game picks one of them.
        have = set((held.get(appid) or {}).get('files') or [])
        other = {name.lower() for name in have}
        if any(item['name'] not in have and item['name'].lower() in other for item in files):
            raise TidyError('is spelled differently on disk than the stored copy (letter case of a folder or file); '
                            'this account keeps its own copy')
    record = {
        'version': RECORD_VERSION, 'appid': appid, 'name': plan['name'], 'user': user, 'source': source,
        'kind': plan['kind'], 'library': plan['library'], 'installdir': plan['installdir'],
        'buildid': plan['buildid'], 'depots': _depot_map(plan['depots']), 'acf': plan['acf'], 'listed': names,
        'folders': folders, 'files': files, 'prepared_utc': _now()}
    summary = {'appid': appid, 'name': plan['name'], 'source': source, 'kind': plan['kind'], 'files': len(files),
               'bytes': size, 'private': len(verdict.changed) + len(verdict.extra) + unshared}
    return {'record': record, 'summary': summary}


# File lists. Steam drops the depot manifests of old installs from depotcache, and without them a game cannot be
# compared. A closed Steam can be asked for them: with StateFlags 2 in the app manifest it runs an update at its next
# start that fetches the manifests, finds nothing to change and sets the game back to installed, within a second
# and without touching a file (seen on client 1794536281). A Steam that is offline cannot, and the game would not
# start until it is online again: so what was asked and never answered is taken back at the next slot.

LISTS_NAME = 'lists.json'
LISTS_LIMIT = 1024 * 1024
LISTS_WAIT = 24 * 3600
LISTS_AT_ONCE = 300


def _read_lists(layout):
    try:
        top = _open_dir(layout.root)
    except (OSError, TidyError):
        return {}
    try:
        if _stat_at(top, LISTS_NAME) is None:
            return {}
        memory = json.loads(_read_at(top, LISTS_NAME, LISTS_LIMIT) or b'')
    except (OSError, TidyError, ValueError):
        return {}
    finally:
        os.close(top)
    asked = memory.get('asked') if isinstance(memory, dict) and memory.get('version') == 1 else None
    if not isinstance(asked, dict):
        return {}
    good = {}
    for key, item in asked.items():
        if (isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('utc'), (int, float))
                and isinstance(item.get('identity'), list) and item.get('state') in ('asked', 'returned')):
            good[key] = item
        elif (isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('utc'), (int, float))
              and item.get('state') == 'asking' and isinstance(item.get('before'), list)
              and isinstance(item.get('intended'), str)):
            good[key] = item
    return good


def _write_lists(layout, asked):
    top = _open_dir(layout.root)
    try:
        _put_file(top, LISTS_NAME, json.dumps({'version': 1, 'asked': asked}, indent=1).encode(), 0o600, (0, 0))
    finally:
        os.close(top)


def _manifest_state(path):
    """(text, state, identity) of an app manifest that is a regular file, or None."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        text = depot._read_regular(path, 16 * 1024 * 1024, 'app manifest').decode('utf-8')
        section, state = depot.vdf_loads(text)
    except (OSError, UnicodeDecodeError, depot.DepotError):
        return None
    if section.lower() != 'appstate':
        return None
    return section, state, [info.st_size, info.st_mtime_ns, info.st_ino]


def _text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _intended_flags(path, flags, expect):
    """As the account: what _set_flags would write, without writing it: (the manifest's identity now, the digest of the
    text it would have afterwards), or None when the manifest does not say `expect`."""
    found = _manifest_state(path)
    if found is None or found[1].get('StateFlags') != expect:
        return None
    section, state, identity = found
    return identity, _text_digest(depot.vdf_dumps(section, dict(state, StateFlags=flags)))


def _unfinished_question(path, before, intended):
    """As the account: what became of a question that was written down and never recorded as put. 'returned': the
    manifest is exactly what the question would have written, and it is put back; 'unput': the manifest is as it
    was before, nothing to do; 'answered': somebody (Steam) wrote it since, and it is left alone."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return 'answered'
        if [info.st_size, info.st_mtime_ns, info.st_ino] == before:
            return 'unput'
        text = depot._read_regular(path, 16 * 1024 * 1024, 'app manifest').decode('utf-8')
    except (OSError, UnicodeDecodeError, depot.DepotError):
        return 'answered'
    if _text_digest(text) != intended:
        return 'answered'
    return 'returned' if _set_flags(path, '4', '2') is not None else 'answered'


def _set_flags(path, flags, expect):
    """As the account: set StateFlags of its app manifest, if it still says `expect`. Returns the new identity."""
    found = _manifest_state(path)
    if found is None or found[1].get('StateFlags') != expect:
        return None
    section, state, _identity = found
    state['StateFlags'] = flags
    folder = _open_dir(Path(path).parent)
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        _put_file(folder, Path(path).name, depot.vdf_dumps(section, state).encode(), mode)
    finally:
        os.close(folder)
    info = os.lstat(path)
    return [info.st_size, info.st_mtime_ns, info.st_ino]


def _games_without_lists(layout, user, entry, held=frozenset()):
    """As the account: (appid, manifest path) of its own installed games whose depot manifests are not all cached.
    A game a store holds is not its own to ask for: its overlay shows the store's manifest."""
    steam = store.steam_root_of(entry)
    found = []
    for library in _libraries(layout, entry, user):
        folder = library / 'steamapps'
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            match = re.fullmatch(r'appmanifest_([0-9]{1,10})\.acf', name)
            state = _manifest_state(folder / name) if match else None
            if state is None or state[1].get('StateFlags') != '4' or state[1].get('appid') != match.group(1):
                continue
            if match.group(1) in held:
                continue
            if str(state[1].get('name', '')).startswith(store.STEAM_TOOLS):
                continue
            try:
                depots = depot.installed_depots(state[1])
            except depot.DepotError:
                continue
            if depots and not all(_is_regular(steam / 'depotcache' / depot.manifest_name(number, gid))
                                  for number, gid in depots.items()):
                found.append([match.group(1), str(folder / name)])
    return found


def asked_for_lists(layout):
    """The `user/appid` keys of the games whose file list was asked for and not answered yet."""
    return {key for key, item in _read_lists(layout).items() if item.get('state') == 'asked'}


def ask_for_lists(layout, users, *, online, now=None):
    """Only in a slot (no Steam runs). Takes back what was asked and never answered, then, when the device is
    online, asks each account's Steam for the file lists it dropped. Returns {'asked': n, 'returned': n, 'answered': n}."""
    now = time.time() if now is None else now
    result = {'asked': 0, 'returned': 0, 'answered': 0}
    held = set()
    with contextlib.suppress(store.StoreError, OSError):
        for place in store.listed_layouts(layout):
            held |= set(stored(place))
    with store.locked(layout):
        memory = _read_lists(layout)
        for key, item in list(memory.items()):
            user = key.split('/', 1)[0]
            if user not in users:
                del memory[key]
                continue
            entry = store.account(user)
            if item['state'] == 'returned':
                if now - item['utc'] >= LISTS_WAIT or now < item['utc']:
                    del memory[key]
                continue
            if item['state'] == 'asking':
                # Written down before the manifest was touched, and never recorded as put: the run stopped in between.
                try:
                    outcome = _as_user(entry, lambda item=item: _unfinished_question(
                        item['path'], item['before'], item['intended']))
                except TidyError:
                    outcome = 'answered'
                if outcome == 'returned':
                    result['returned'] += 1
                    memory[key] = {'path': item['path'], 'identity': item['before'], 'state': 'returned', 'utc': now}
                else:
                    if outcome == 'answered':
                        result['answered'] += 1
                    del memory[key]
                continue

            def take_back(path=item['path'], identity=item['identity']):
                found = _manifest_state(path)
                if found is None or found[2] != identity:
                    return 'answered'
                return 'returned' if _set_flags(path, '4', '2') is not None else 'answered'
            try:
                outcome = _as_user(entry, take_back)
            except TidyError:
                outcome = 'answered'
            result[outcome] += 1
            if outcome == 'returned':
                memory[key] = dict(item, state='returned', utc=now)
            else:
                del memory[key]
        if online:
            # Every question is written down before a manifest is touched (state "asking": what the manifest is now
            # and what it will be), so that a run that stops anywhere after this can still tell its own edit from
            # Steam's and take it back. A manifest that says "update required" with nothing remembering why stayed
            # that way for good.
            wanted = []
            for user in users:
                entry = store.account(user)
                try:
                    games = _as_user(entry, lambda: _games_without_lists(layout, user, entry, held))
                except TidyError:
                    continue
                for appid, path in games:
                    key = f'{user}/{appid}'
                    if key in memory or len(wanted) >= LISTS_AT_ONCE:
                        continue
                    try:
                        plan = _as_user(entry, lambda: _intended_flags(path, '2', '4'))
                    except TidyError:
                        continue
                    if plan is not None:
                        memory[key] = {'path': path, 'state': 'asking', 'before': plan[0], 'intended': plan[1], 'utc': now}
                        wanted.append((key, entry, path))
            if wanted:
                _write_lists(layout, memory)
            for key, entry, path in wanted:
                try:
                    identity = _as_user(entry, lambda: _set_flags(path, '2', '4'))
                except TidyError:
                    identity = None
                if identity is not None:
                    memory[key] = {'path': path, 'identity': identity, 'state': 'asked', 'utc': now}
                    result['asked'] += 1
                else:
                    del memory[key]
        _write_lists(layout, memory)
    return result


# The public listing and the slow step.

def shareable(layout, user, users):
    """Cheap listing, no hashing: every fully or partly installed game of `user` that could be shared or is refused,
    as a list of {'appid', 'name', 'source': 'library'|'layer', 'library': path, 'reason': None or text}."""
    _check_user(user, users)
    entry = store.account(user)
    store.resolve_libraries(layout, users)
    held = stored(layout)

    def look():
        listing = []
        candidates = _candidates(layout, user, entry)
        folders = _account_folders(layout, user, candidates)
        for appid, source, library in candidates:
            facts = {}
            try:
                _inspect(layout, user, entry, appid, source, library, facts, held, folders)
                reason = None
            except (TidyError, OSError, ValueError) as error:
                reason = str(error)
            listing.append({'appid': appid, 'name': facts.get('name', ''), 'source': source, 'library': str(library),
                            'reason': reason})
        return listing
    return _as_user(entry, look)


def _ensure_root_dir(layout, name, mode):
    fd = _layout_dir(layout, name, create=True, mode=mode)
    os.close(fd)


def share(layout, user, appid, users, *, progress=None):
    """The slow step, safe while Steam runs. Verifies the game and writes <root>/pending/<appid>.json.
    Returns {'appid','name','source','kind': 'new'|'join'|'update','files': n,'bytes': n,'private': n}.
    Raises TidyError with the reason when the game is refused. progress(name, size) is called per hashed file."""
    appid = _appid(appid)
    _check_user(user, users)
    entry = store.account(user)
    store.resolve_libraries(layout, users)
    held = stored(layout)
    try:
        result = _as_user(entry, lambda: _prepare(layout, user, entry, appid, progress, held))
    except TidyError as error:
        raise TidyError(f'game {appid} {error}') from None
    _ensure_root_dir(layout, 'pending', 0o700)
    fd = _layout_dir(layout, 'pending')
    try:
        _put_file(fd, f'{appid}.json', json.dumps(result['record'], separators=(',', ':')).encode(), 0o600)
    finally:
        os.close(fd)
    return result['summary']


def _json_files(layout, folder):
    """name -> parsed JSON of every <digits>.json in a folder of the root; unreadable ones give {'error': text}."""
    try:
        fd = _layout_dir(layout, folder)
    except (OSError, TidyError):
        return {}
    found = {}
    try:
        for name in sorted(os.listdir(fd)):
            match = re.fullmatch(r'([0-9]{1,10})\.json', name)
            if not match:
                continue
            try:
                found[match.group(1)] = _json_at(fd, name)
            except TidyError as error:
                found[match.group(1)] = {'error': str(error)}
    finally:
        os.close(fd)
    return found


def pending(layout):
    """Summaries of the records waiting for a slot."""
    summaries = []
    for appid, record in sorted(_json_files(layout, 'pending').items(), key=lambda item: int(item[0])):
        if 'error' in record:
            summaries.append({'appid': appid, 'error': record['error']})
            continue
        files = record.get('files', [])
        summaries.append({
            'appid': appid, 'name': record.get('name', ''), 'user': record.get('user'), 'source': record.get('source'),
            'kind': record.get('kind'), 'files': len(files),
            'bytes': sum(item.get('size', 0) for item in files if 'link' not in item),
            'prepared_utc': record.get('prepared_utc')})
    return summaries


def stored(layout):
    """appid -> the contents of <root>/apps/<appid>.json for every game the store holds."""
    return _json_files(layout, 'apps')


# Commit.

@contextlib.contextmanager
def _quiet_lease_signals():
    """A lease broken by another process signals the holder; the default action would end the process."""
    try:
        previous = signal.signal(signal.SIGIO, lambda *_args: None)
    except ValueError:
        yield           # not the main thread
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGIO, previous)


def _lease(fd, name):
    """A read lease: it fails while anyone has the file open for writing."""
    try:
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EACCES):
            raise _Changed(f'{name} is open for writing') from None
        raise _Changed(f'{name} cannot be held ({error.strerror})') from None


def _unlease(fd):
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)


def _load_record(layout, appid, users):
    fd = _layout_dir(layout, 'pending')
    try:
        record = _json_at(fd, f'{appid}.json')
    finally:
        os.close(fd)
    if record.get('version') != RECORD_VERSION or record.get('appid') != appid:
        raise TidyError('the record is not one this version understands')
    _check_user(record.get('user'), users)
    _installdir(record.get('installdir'))
    if record.get('source') not in ('library', 'layer') or record.get('kind') not in ('new', 'join', 'update'):
        raise TidyError('the record names an unknown source or kind')
    if not isinstance(record.get('files'), list) or not isinstance(record.get('listed'), list):
        raise TidyError('the record has no file list')
    if record['source'] == 'library':
        library = Path(record.get('library', ''))
        if not library.is_absolute() or '..' in library.parts:
            raise TidyError('the record names a library that is not an absolute path')
    return record


def _drop_record(layout, appid):
    with contextlib.suppress(OSError, TidyError):
        fd = _layout_dir(layout, 'pending')
        try:
            os.unlink(f'{appid}.json', dir_fd=fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)


def _source_folder(layout, record):
    side = layout.side(record['user'])
    return side.root_path(Path(record['library'])) / 'steamapps' / 'common' / record['installdir']


def _source_manifest_dir(layout, record):
    side = layout.side(record['user'])
    return side.root_path(Path(record['library'])) / 'steamapps'


def _owned_dir(path, uid, what):
    """Open a folder of an account and refuse one that is not the account's own.

    Root moves and deletes below these folders, so a path that an account only named (in its library list) but does
    not own must never be worked on.
    """
    fd = _open_dir(path)
    if os.fstat(fd).st_uid != uid:
        os.close(fd)
        raise _Changed(f'{what} does not belong to the account')
    return fd


def _check_manifest_unchanged(layout, record, uid):
    folder = _source_manifest_dir(layout, record)
    name = f'appmanifest_{record["appid"]}.acf'
    try:
        fd = _owned_dir(folder, uid, 'the app manifest folder')
    except (OSError, TidyError) as error:
        raise _Changed(f'the app manifest folder cannot be used ({error})') from None
    try:
        info = _stat_at(fd, name)
        if info is None or info.st_uid != uid:
            raise _Changed('the app manifest is gone or does not belong to the account')
        data = _read_at(fd, name)
    finally:
        os.close(fd)
    if data is None or data.decode('utf-8', 'replace') != record['acf']:
        raise _Changed('the app manifest changed since the game was read')


def _check_file(name, info, item, uid, *, with_ctime):
    wanted = stat.S_IFLNK if 'link' in item else stat.S_IFREG
    if stat.S_IFMT(info.st_mode) != wanted:
        raise _Changed(f'{name} is not what it was')
    pairs = [(info.st_dev, item['dev']), (info.st_ino, item['ino']), (info.st_size, item['size']),
             (info.st_mtime_ns, item['mtime_ns'])]
    if with_ctime:
        pairs.append((info.st_ctime_ns, item['ctime_ns']))
    if any(have != want for have, want in pairs):
        raise _Changed(f'{name} changed since the game was read')
    if with_ctime and (info.st_uid != uid or (wanted == stat.S_IFREG and info.st_nlink != 1)):
        raise _Changed(f'{name} changed owner or got another hard link')


def _journal(stage_fd, phase, record):
    _put_file(stage_fd, 'journal.json', json.dumps({'phase': phase, 'record': record}).encode(), 0o600)


def _open_stage(layout, appid):
    _ensure_root_dir(layout, 'staging', 0o700)
    top = _layout_dir(layout, 'staging')
    try:
        return _walk(top, [appid], create=True, mode=0o700)
    finally:
        os.close(top)


def _move_in(parts, source_parent, item, uid, files_fd):
    """Phase 1 for one file: compare it with what was seen, hold a lease, rename it into staging."""
    name = '/'.join(parts)
    leaf = parts[-1]
    info = _stat_at(source_parent, leaf)
    if info is None:
        raise _Changed(f'{name} is gone')
    _check_file(name, info, item, uid, with_ctime=True)
    handle = None
    try:
        if 'link' not in item:
            handle = os.open(leaf, FILE_FLAGS, dir_fd=source_parent)
            _lease(handle, name)
            _check_file(name, os.fstat(handle), item, uid, with_ctime=True)
        elif os.readlink(leaf, dir_fd=source_parent) != item['link']:
            raise _Changed(f'{name} points somewhere else')
        target = _walk(files_fd, parts[:-1], create=True, mode=0o700)
        try:
            os.rename(leaf, leaf, src_dir_fd=source_parent, dst_dir_fd=target)
        finally:
            os.close(target)
    finally:
        if handle is not None:
            _unlease(handle)
            os.close(handle)


def _collect(layout, record, uid, files_fd):
    """Phase 1: every file the record names moves into staging, or the game is not taken."""
    root = _owned_dir(_source_folder(layout, record), uid, 'the game folder')
    try:
        for item in record['files']:
            parts = _parts(item['name'])
            parent = _walk(root, parts[:-1])
            try:
                _move_in(parts, parent, item, uid, files_fd)
            finally:
                os.close(parent)
    finally:
        os.close(root)


def _prepare_staged(record, entry, files_fd):
    """Phase 2: owner root, plain mode, no overlay attributes; then the file must still be what was seen."""
    for item in record['files']:
        parts = _parts(item['name'])
        parent = _walk(files_fd, parts[:-1])
        try:
            leaf = parts[-1]
            if 'link' in item:
                info = _stat_at(parent, leaf)
                if info is None or not stat.S_ISLNK(info.st_mode) or info.st_ino != item['ino']:
                    raise _Changed(f'{item["name"]} is not what it was')
                _chown(parent, 0, 0, leaf)
                continue
            handle = os.open(leaf, FILE_FLAGS, dir_fd=parent)
            try:
                _chown(handle, 0, 0)
                os.fchmod(handle, 0o755 if item['executable'] else 0o644)
                _strip_overlay(handle)
                _lease(handle, item['name'])
                info = os.fstat(handle)
                _check_file(item['name'], info, item, entry.pw_uid, with_ctime=False)
            finally:
                _unlease(handle)
                os.close(handle)
        finally:
            os.close(parent)


def _rollback(layout, record, entry, stage_fd):
    """Put every staged file back where it came from, with its owner and mode. Never overwrites anything."""
    left = []
    try:
        files_fd = _walk(stage_fd, ['files'])
    except (OSError, TidyError):
        return
    try:
        try:
            root = _open_dir(_source_folder(layout, record))
        except (OSError, TidyError) as error:
            raise TidyError(f'the game folder cannot be opened to put the files back ({error})') from None
        try:
            for item in record['files']:
                parts = _parts(item['name'])
                try:
                    staged = _walk(files_fd, parts[:-1])
                except (OSError, TidyError):
                    continue
                try:
                    leaf = parts[-1]
                    if _stat_at(staged, leaf) is None:
                        continue
                    if 'link' in item:
                        _chown(staged, entry.pw_uid, entry.pw_gid, leaf)
                    else:
                        handle = os.open(leaf, FILE_FLAGS, dir_fd=staged)
                        try:
                            _chown(handle, entry.pw_uid, entry.pw_gid)
                            os.fchmod(handle, item['mode'])
                        finally:
                            os.close(handle)
                    try:
                        target = _walk(root, parts[:-1])
                        try:
                            if _stat_at(target, leaf) is not None:
                                left.append(item['name'])
                                continue
                            os.rename(leaf, leaf, src_dir_fd=staged, dst_dir_fd=target)
                        finally:
                            os.close(target)
                    except (OSError, TidyError):
                        left.append(item['name'])
                finally:
                    os.close(staged)
        finally:
            os.close(root)
    finally:
        os.close(files_fd)
    if left:
        raise TidyError(f'{len(left)} files could not be put back and stay in staging/{record["appid"]}: '
                        + ', '.join(left[:5]))
    with contextlib.suppress(OSError, TidyError):
        _remove_empty_tree(stage_fd, 'files')


def _publish_join(layout, record, files_fd, names):
    """Phase 3 of a join, file by file. Returns warnings.

    Every staged file is one the joining account's copy had exactly as the depot lists it. What happens to it depends
    on the store, never on the name alone:
    - the store has the same bytes: the staged file is a duplicate and goes;
    - the store has no such file (the first sharer's own copy was changed, so it stayed private and the store never
      got it): the staged file enters the store. It used to be deleted here as a "duplicate", and then neither the
      account nor the store had the file;
    - the store has something else under that name: nothing is deleted and nothing is overwritten. The staged file
      stays in staging, where the next commit and `status` find it, and a warning says so.
    Can run again after an interruption: a file that was dealt with is no longer in staging.
    """
    warnings, added = [], False
    common = _open_dir(layout.common)
    try:
        for name in names:
            parts = _parts(name)
            source = _walk(files_fd, parts[:-1])
            try:
                try:
                    held = _walk(common, [record['installdir']] + parts[:-1])
                except (OSError, TidyError):
                    held = None
                try:
                    existing = _stat_at(held, parts[-1]) if held is not None else None
                    staged = _stat_at(source, parts[-1])
                    if existing is None:
                        if held is None:
                            held = _walk(common, [record['installdir']] + parts[:-1], create=True, mode=0o755,
                                         owner=(0, 0))
                        os.rename(parts[-1], parts[-1], src_dir_fd=source, dst_dir_fd=held)
                        added = True
                    elif (stat.S_ISREG(existing.st_mode) and staged is not None and stat.S_ISREG(staged.st_mode)
                          and existing.st_size == staged.st_size and _same_file_bytes(source, held, parts[-1])):
                        os.unlink(parts[-1], dir_fd=source)
                    elif (stat.S_ISLNK(existing.st_mode) and staged is not None and stat.S_ISLNK(staged.st_mode)
                          and os.readlink(parts[-1], dir_fd=held) == os.readlink(parts[-1], dir_fd=source)):
                        os.unlink(parts[-1], dir_fd=source)
                    else:
                        warnings.append(f'{record["appid"]}: {name} of {record["user"]} differs from the stored copy; '
                                        'it was kept in staging and nothing was overwritten')
                finally:
                    if held is not None:
                        os.close(held)
            finally:
                os.close(source)
    finally:
        os.close(common)
    if added:
        _note_store_names(layout, record)
    return warnings


def _same_file_bytes(first_dir, second_dir, name):
    first = os.open(name, FILE_FLAGS, dir_fd=first_dir)
    try:
        second = os.open(name, FILE_FLAGS, dir_fd=second_dir)
        try:
            return _same_bytes(first, second)
        finally:
            os.close(second)
    finally:
        os.close(first)


def _note_store_names(layout, record):
    """The store's record of a game gets the names the store holds now; everything else in it stays as it is."""
    apps = _layout_dir(layout, 'apps')
    try:
        name = f'{record["appid"]}.json'
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=apps)
        except OSError:
            return
        try:
            with os.fdopen(fd, 'rb') as source:
                app = json.loads(source.read())
        except ValueError:
            return
        if not isinstance(app, dict):
            return
        app['files'] = _store_names(layout, record['installdir'])
        _put_file(apps, name, json.dumps(app, indent=1).encode(), 0o644, (0, 0))
    finally:
        os.close(apps)


def _publish_files(layout, record, files_fd):
    """Phase 3, files: move what is staged into the store (a join only drops its duplicates)."""
    names = _names_below(files_fd)
    if record['kind'] == 'join':
        return _publish_join(layout, record, files_fd, names)
    os.makedirs(layout.common, exist_ok=True)
    common = _open_dir(layout.common)
    try:
        for name in names:
            parts = _parts(name)
            source = _walk(files_fd, parts[:-1])
            try:
                target = _walk(common, [record['installdir']] + parts[:-1], create=True, mode=0o755, owner=(0, 0),
                               replace=True)
                try:
                    existing = _stat_at(target, parts[-1])
                    if existing is not None and stat.S_ISDIR(existing.st_mode):
                        _remove_entry(target, parts[-1])
                    os.rename(parts[-1], parts[-1], src_dir_fd=source, dst_dir_fd=target)
                finally:
                    os.close(target)
            finally:
                os.close(source)
    finally:
        os.close(common)


def _prune_store(fd, prefix, listed):
    """Delete files of a stored game that the new build does not list, and folders that emptied. True if any went."""
    removed = False
    for entry in sorted(os.listdir(fd)):
        info = _stat_at(fd, entry)
        if info is None:
            continue
        name = f'{prefix}{entry}'
        if stat.S_ISDIR(info.st_mode):
            child = os.open(entry, DIR_FLAGS, dir_fd=fd)
            try:
                inner = _prune_store(child, name + '/', listed)
                empty = not os.listdir(child)
            finally:
                os.close(child)
            if inner and empty:
                os.rmdir(entry, dir_fd=fd)
            removed = removed or inner
        elif name not in listed:
            os.unlink(entry, dir_fd=fd)
            removed = True
    return removed


def _store_folders(layout, record):
    """The folders the depot manifests list exist in the store even when they are empty: Steam ships some empty."""
    common = _open_dir(layout.common)
    try:
        for name in [''] + [item for item in record.get('folders', []) if isinstance(item, str)]:
            try:
                parts = [record['installdir']] + (_parts(name) if name else [])
                os.close(_walk(common, parts, create=True, mode=0o755, owner=(0, 0)))
            except (TidyError, OSError):
                continue        # a file is where the folder would be: the files win
    finally:
        os.close(common)


def _canonical_text(acf, flags, validate):
    section, state = depot.vdf_loads(acf)
    state['StateFlags'] = flags
    for key in TRANSIENT_KEYS:
        state.pop(key, None)
    if validate:
        state['FullValidateBeforeNextUpdate'] = '1'
    return depot.vdf_dumps(section, state)


def _store_names(layout, installdir):
    try:
        folder = _open_dir(layout.common / installdir)
    except (OSError, TidyError):
        return []
    try:
        return _names_below(folder)
    finally:
        os.close(folder)


def _write_store_records(layout, record):
    steamapps = _open_dir(layout.steamapps)
    try:
        _put_file(steamapps, f'appmanifest_{record["appid"]}.acf',
                  _canonical_text(record['acf'], '4', False).encode(), 0o644, (0, 0))
    finally:
        os.close(steamapps)
    _ensure_root_dir(layout, 'apps', 0o755)
    apps = _layout_dir(layout, 'apps')
    try:
        # Who shared a game first is who gets it back when the library is turned off (the installation guide says
        # so). An update that the other account's Steam made must not change that: it is noted beside it.
        earlier = (stored(layout).get(record['appid']) or {}) if record['kind'] == 'update' else {}
        first = earlier.get('shared_by') if isinstance(earlier.get('shared_by'), str) else None
        app = {'version': RECORD_VERSION, 'appid': record['appid'], 'name': record['name'],
               'shared_by': first or record['user'], 'installdir': record['installdir'],
               'buildid': record['buildid'], 'depots': record['depots'],
               'files': _store_names(layout, record['installdir']),
               'shared_utc': earlier.get('shared_utc') if first and isinstance(earlier.get('shared_utc'), str) else _now()}
        if first:
            app.update(updated_by=record['user'], updated_utc=_now())
        _put_file(apps, f'{record["appid"]}.json', json.dumps(app, indent=1).encode(), 0o644, (0, 0))
    finally:
        os.close(apps)


def _relocate(source, target, owner, shared_folder=None, prefix=''):
    """Move everything below source into target at the same relative path; folders are made for the account.

    An empty folder moves too, because a game may expect a folder it once made (logs, saves) to be there, unless
    shared_folder(relative name) says the store has that folder anyway.
    """
    for name in sorted(os.listdir(source)):
        info = _stat_at(source, name)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR_FLAGS, dir_fd=source)
            try:
                relative = f'{prefix}{name}'
                if os.listdir(child) or shared_folder is None or not shared_folder(relative):
                    into = _walk(target, [name], create=True, mode=stat.S_IMODE(info.st_mode), owner=owner,
                                 replace=True)
                    try:
                        _relocate(child, into, owner, shared_folder, relative + '/')
                    finally:
                        os.close(into)
            finally:
                os.close(child)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=source)
        else:
            existing = _stat_at(target, name)
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                _remove_entry(target, name)
            os.rename(name, name, src_dir_fd=source, dst_dir_fd=target)


def _remove_whiteouts_not_listed(fd, prefix, listed):
    for name in sorted(os.listdir(fd)):
        info = _stat_at(fd, name)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            try:
                _remove_whiteouts_not_listed(child, f'{prefix}{name}/', listed)
            finally:
                os.close(child)
        elif _is_whiteout(info) and f'{prefix}{name}' not in listed:
            os.unlink(name, dir_fd=fd)


def _prune_empty(fd):
    """Remove folders that hold nothing, bottom-up. A folder with anything in it stays."""
    for name in sorted(os.listdir(fd)):
        info = _stat_at(fd, name)
        if info is None or not stat.S_ISDIR(info.st_mode):
            continue
        child = os.open(name, DIR_FLAGS, dir_fd=fd)
        try:
            _prune_empty(child)
        finally:
            os.close(child)
        with contextlib.suppress(OSError):
            os.rmdir(name, dir_fd=fd)


def _prune_held_empty(fd, held, prefix=''):
    """Remove folders that hold nothing and that the store has as well: they were part of the copy that moved. A folder
    the store does not have (a game's own folder for its saves, made empty) stays."""
    for name in sorted(os.listdir(fd)):
        info = _stat_at(fd, name)
        if info is None or not stat.S_ISDIR(info.st_mode):
            continue
        child = os.open(name, DIR_FLAGS, dir_fd=fd)
        try:
            _prune_held_empty(child, held, f'{prefix}{name}/')
        finally:
            os.close(child)
        below = _lstat_below(held, _parts(f'{prefix}{name}'))
        if below is not None and stat.S_ISDIR(below.st_mode):
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=fd)


def _cleanup_source(layout, record, entry):
    """What is left of the game at its old place: the old manifest goes (the store's shows through the overlay),
    whiteouts for files the new build no longer has go, and the folders the move emptied go. What the account keeps
    there (saves, changed settings) stays where it is when that is the library its overlay is on: that folder is the
    layer. From another library of the account on this disk it goes by rename into the folder of the game in that
    library, where the game, started from the store, finds it."""
    appid, installdir = record['appid'], record['installdir']
    manifest = f'appmanifest_{appid}.acf'
    side = layout.side(record['user'])
    owner = (entry.pw_uid, entry.pw_gid)
    steamapps = _owned_dir(_source_manifest_dir(layout, record), entry.pw_uid, 'the library')
    try:
        info = _stat_at(steamapps, manifest)
        if info is not None and not _is_whiteout(info):
            os.unlink(manifest, dir_fd=steamapps)
        try:
            common = _walk(steamapps, ['common'])
        except (OSError, TidyError):
            return
        try:
            if _stat_at(common, installdir) is None:
                return
            game = _walk(common, [installdir])
            try:
                if os.fstat(game).st_uid != entry.pw_uid:
                    raise TidyError('the old game folder does not belong to the account; it is left as it is')
                held = layout.common / installdir
                elsewhere = side.real_library is not None and side.root_path(Path(record['library'])) != side.real_library
                if record['kind'] in ('update', 'join'):
                    _remove_whiteouts_not_listed(game, '', set(record['listed']))
                if elsewhere and os.listdir(game):
                    top = _open_dir(side.real_library)
                    try:
                        target = _walk(top, ['steamapps', 'common', installdir], create=True, owner=owner)
                        try:
                            _relocate(game, target, owner, lambda relative: stat.S_ISDIR(getattr(
                                _lstat_below(held, _parts(relative)), 'st_mode', 0)))
                        finally:
                            os.close(target)
                    finally:
                        os.close(top)
                _prune_held_empty(game, held)
                if elsewhere:
                    _prune_empty(game)
            finally:
                os.close(game)
            with contextlib.suppress(OSError):
                os.rmdir(installdir, dir_fd=common)
        finally:
            os.close(common)
    finally:
        os.close(steamapps)


def _has_whiteout(layout, user, appid):
    side = layout.side(user)
    if side.real_library is None:
        return False
    info = _lstat_below(side.real_library, ['steamapps', f'appmanifest_{appid}.acf'])
    return info is not None and _is_whiteout(info)


def _validation_for(layout, user, entry, record):
    """As the account: put the validation manifest into its library unless it uninstalled the game or has it itself."""
    appid = record['appid']
    side = layout.side(user)
    name = f'appmanifest_{appid}.acf'
    if side.library is None:
        return 'no library'
    info = _lstat_below(side.library, ['steamapps', name])
    if info is not None and _is_whiteout(info):
        return 'uninstalled'
    for library in _libraries(layout, entry, user):
        # What the account's Steam wrote in the library its overlay is on (an update of its own, or its own copy of the
        # build) is replaced by the request: its Steam then checks every file before the game starts. A copy in any
        # other library of the account is its own, and the game is not offered from the store.
        if library != side.library and _is_regular(library / 'steamapps' / name):
            return 'own copy'
    top = _open_dir(side.library)
    try:
        steamapps = _walk(top, ['steamapps'], create=True)
        try:
            _put_file(steamapps, name, _canonical_text(record['acf'], '2', True).encode(), 0o644)
        finally:
            os.close(steamapps)
    finally:
        os.close(top)
    return 'written'


def _validation(layout, users, record):
    """Every account that should check the game before first play gets a manifest that asks Steam to."""
    if record['kind'] == 'join':
        targets = [record['user']]
    else:
        targets = [name for name in users if name != record['user']]
    warnings = []
    for name in targets:
        try:
            entry = store.account(name)
            _as_user(entry, lambda: _validation_for(layout, name, entry, record))
        except (TidyError, store.StoreError, OSError) as error:
            warnings.append(f'{record["appid"]}: no validation manifest for {name}: {error}')
    return warnings


def _finish(layout, users, record, entry, stage_fd):
    """Phase 3 and what follows. Every step can run again, so an interrupted commit can be finished."""
    # Checked again here for a commit that was interrupted: files are not published into, or pruned from, a folder
    # that the store's records give to another app too. A join counts: it can bring the store a file it lacks.
    _require_alone(stored(layout), record['appid'], record['installdir'])
    files_fd = _walk(stage_fd, ['files'], create=True, mode=0o700)
    try:
        unpublished = _publish_files(layout, record, files_fd) or []
    finally:
        os.close(files_fd)
    if record['kind'] == 'update':
        common = _open_dir(layout.common)
        try:
            game = _walk(common, [record['installdir']], create=True, mode=0o755, owner=(0, 0))
            try:
                _prune_store(game, '', set(record['listed']))
            finally:
                os.close(game)
        finally:
            os.close(common)
    if record['kind'] != 'join':
        _store_folders(layout, record)
        _write_store_records(layout, record)
    _cleanup_source(layout, record, entry)
    warnings = unpublished + _drop_duplicates(layout, users, record)
    warnings += _validation(layout, users, record)
    _drop_record(layout, record['appid'])
    return warnings


def _same_bytes(first, second):
    while True:
        one, other = os.read(first, 1 << 20), os.read(second, 1 << 20)
        if one != other:
            return False
        if not one:
            return True


def _drop_same_files(layer, held):
    """Below an account's layer folder of a game: remove every file whose bytes are exactly the store's file of that
    name, so that the account reads the store's. A folder the overlay marks opaque hides what is below it and is left
    alone. A file of no bytes is not worth the look. Returns the bytes freed."""
    freed = 0
    if _opaque(layer):
        return freed
    for name in sorted(os.listdir(layer)):
        info, below = _stat_at(layer, name), _stat_at(held, name)
        if info is None or below is None or _is_whiteout(info):
            continue
        if stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(below.st_mode):
            child, under = os.open(name, DIR_FLAGS, dir_fd=layer), os.open(name, DIR_FLAGS, dir_fd=held)
            try:
                gone = _drop_same_files(child, under)
                emptied = gone > 0 and not os.listdir(child)
            finally:
                os.close(child)
                os.close(under)
            freed += gone
            if emptied:         # only a folder this emptied: one the account or a game made empty is theirs to keep
                with contextlib.suppress(OSError):
                    os.rmdir(name, dir_fd=layer)
        elif (stat.S_ISREG(info.st_mode) and stat.S_ISREG(below.st_mode) and info.st_size == below.st_size
              and info.st_size > 0 and info.st_nlink == 1):
            mine, theirs = os.open(name, FILE_FLAGS, dir_fd=layer), os.open(name, FILE_FLAGS, dir_fd=held)
            try:
                same = _same_bytes(mine, theirs)
            finally:
                os.close(mine)
                os.close(theirs)
            if same:
                os.unlink(name, dir_fd=layer)
                freed += info.st_size
    return freed


def _drop_duplicates(layout, users, record):
    """After the store took a game or an update: what an account still keeps of it in its own library and is the same
    as the store's goes. Both accounts' Steam download an update while both run (seen on the Deck: Thronefall, 1.4 GB
    in each layer); one copy enters the store and the other would stay for good. Whiteouts for files the new build
    no longer has go too. Never fails a commit: what could not be done is a warning."""
    warnings = []
    installdir = record['installdir']
    try:
        held = _open_dir(layout.common / installdir)
    except (OSError, TidyError):
        return warnings
    try:
        for name in users:
            side = layout.side(name)
            if side.real_library is None:
                continue
            try:
                top = _open_dir(side.real_library)
                try:
                    common = _walk(top, ['steamapps', 'common'])
                except (OSError, TidyError):
                    continue
                finally:
                    os.close(top)
                try:
                    info = _stat_at(common, installdir)
                    if info is None or not stat.S_ISDIR(info.st_mode):
                        continue
                    game = _walk(common, [installdir])
                    try:
                        _remove_whiteouts_not_listed(game, '', set(record['listed']))
                        emptied = _drop_same_files(game, held) > 0 and not os.listdir(game)
                    finally:
                        os.close(game)
                    if emptied:
                        with contextlib.suppress(OSError):
                            os.rmdir(installdir, dir_fd=common)
                finally:
                    os.close(common)
            except (OSError, TidyError) as error:
                warnings.append(f'{record["appid"]}: the copy in the library of {name} was not compared: {error}')
    finally:
        os.close(held)
    return warnings


def _end_stage(layout, appid):
    """Remove the staging folder of a finished game. Anything that is not empty stays."""
    top = _layout_dir(layout, 'staging')
    try:
        stage = _walk(top, [appid])
        try:
            if _stat_at(stage, 'files') is not None:
                _remove_empty_tree(stage, 'files')      # refuses while a file is left; the journal stays then
            with contextlib.suppress(FileNotFoundError):
                os.unlink('journal.json', dir_fd=stage)
        finally:
            os.close(stage)
        os.rmdir(appid, dir_fd=top)
    finally:
        os.close(top)


def _commit_one(layout, users, appid, result, waiting=None):
    """`waiting` is the pending records as they were when the run began (commit passes it): a record dropped earlier
    in the run still counts, so that neither of two apps in one folder is taken."""
    record = None
    try:
        record = _load_record(layout, appid, users)
        _require_alone(stored(layout), appid, record['installdir'])
        rivals = _sharers(_json_files(layout, 'pending') if waiting is None else waiting, appid, record['installdir'])
        if rivals:
            raise TidyError(f'shares its install folder with game {rivals[0]}, which waits to be shared too; '
                            f'neither is taken, and both are left alone')
        entry = store.account(record['user'])
        if _kind(_stored_one(layout, appid), str(record.get('buildid')), record.get('depots')) != record['kind']:
            raise TidyError('the store changed since the game was checked')
        _check_manifest_unchanged(layout, record, entry.pw_uid)
        stage = _open_stage(layout, appid)
    except (TidyError, store.StoreError, OSError) as error:
        _drop_record(layout, appid)
        result['dropped'].append({'appid': appid, 'reason': str(error)})
        return
    try:
        _journal(stage, 'collecting', record)
        files_fd = _walk(stage, ['files'], create=True, mode=0o700)
        try:
            try:
                _collect(layout, record, entry.pw_uid, files_fd)
                _journal(stage, 'preparing', record)
                _prepare_staged(record, entry, files_fd)
            except (TidyError, OSError, ValueError) as error:
                reason = str(error)
                try:
                    _rollback(layout, record, entry, stage)
                except TidyError as trouble:
                    reason = f'{reason}; {trouble}'
                _drop_record(layout, appid)
                result['dropped'].append({'appid': appid, 'reason': reason})
                with contextlib.suppress(TidyError, OSError):
                    _end_stage(layout, appid)
                return
        finally:
            os.close(files_fd)
        _journal(stage, 'publishing', record)
        result['warnings'].extend(_finish(layout, users, record, entry, stage))
    except (TidyError, store.StoreError, OSError, ValueError) as error:
        result['warnings'].append(f'{appid}: the commit stopped after the point of no return and the next commit '
                                  f'finishes it: {error}')
        return
    finally:
        os.close(stage)
    try:
        _end_stage(layout, appid)
    except (TidyError, OSError) as error:
        result['warnings'].append(f'{appid}: staging was not emptied: {error}')
    result['committed'].append(appid)


def _recover(layout, users, appid, result):
    """A staging folder found at the start: finish a commit that had published, roll back any other.

    Returns False when it could not be settled; that game is then left alone.
    """
    top = _layout_dir(layout, 'staging')
    try:
        stage = _walk(top, [appid])
    except (OSError, TidyError):
        return True
    finally:
        os.close(top)
    try:
        try:
            journal = _json_at(stage, 'journal.json')
            record, phase = journal['record'], journal['phase']
            entry = store.account(record['user'])
            _installdir(record['installdir'])
        except (TidyError, KeyError, store.StoreError):
            try:
                _end_stage(layout, appid)
            except (TidyError, OSError):
                result['warnings'].append(f'{appid}: staging holds files and no journal; left as it is')
                return False
            return True
        try:
            if phase == 'publishing':
                result['warnings'].extend(_finish(layout, users, record, entry, stage))
                result['committed'].append(appid)
            else:
                _rollback(layout, record, entry, stage)
                _drop_record(layout, appid)
                result['dropped'].append({'appid': appid, 'reason': 'an earlier commit was interrupted and was rolled back'})
        except (TidyError, store.StoreError, OSError, ValueError) as error:
            result['warnings'].append(f'{appid}: an interrupted commit could not be finished: {error}')
            return False
    finally:
        os.close(stage)
    try:
        _end_stage(layout, appid)
    except (TidyError, OSError) as error:
        result['warnings'].append(f'{appid}: staging was not emptied: {error}')
        return False
    return True


def _unlink_whiteout(layout, user, parts):
    base = layout.side(user).real_library
    if base is None:
        return
    info = _lstat_below(base, parts)
    if info is None or not _is_whiteout(info):
        return
    top = _open_dir(base)
    try:
        parent = _walk(top, parts[:-1])
        try:
            os.unlink(parts[-1], dir_fd=parent)
        finally:
            os.close(parent)
    finally:
        os.close(top)


def _delete_game(layout, users, appid, installdir):
    """Delete a stored game: its files, canonical manifest and record, and the accounts' whiteouts for it.
    Refused, and nothing touched, while the store's records give the folder to another app as well."""
    _require_alone(stored(layout), appid, installdir)
    common = _open_dir(layout.common)
    try:
        _remove_entry(common, installdir)
    finally:
        os.close(common)
    steamapps = _open_dir(layout.steamapps)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f'appmanifest_{appid}.acf', dir_fd=steamapps)
    finally:
        os.close(steamapps)
    apps = _layout_dir(layout, 'apps')
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f'{appid}.json', dir_fd=apps)
    finally:
        os.close(apps)
    for name in users:
        _unlink_whiteout(layout, name, ['steamapps', f'appmanifest_{appid}.acf'])
        _unlink_whiteout(layout, name, ['steamapps', 'common', installdir])
        _drop_whiteouts(layout, name, installdir)


def _drop_whiteouts(layout, user, installdir):
    """An account that uninstalled the game while it had files of its own in the folder keeps a real folder with
    one whiteout per shipped file. With the game gone from the store they hide nothing, and they would hide the
    files if the game is ever shared again: they go. What the account wrote there stays."""
    base = layout.side(user).real_library
    if base is None:
        return
    try:
        top = _open_dir(base)
    except (OSError, TidyError):
        return
    try:
        common = _walk(top, ['steamapps', 'common'])
    except (OSError, TidyError):
        return
    finally:
        os.close(top)
    try:
        info = _stat_at(common, installdir)
        if info is None or not stat.S_ISDIR(info.st_mode):
            return
        game = _walk(common, [installdir])
        try:
            _remove_whiteouts_not_listed(game, '', set())
            _prune_empty(game)
        finally:
            os.close(game)
        with contextlib.suppress(OSError):
            os.rmdir(installdir, dir_fd=common)
    finally:
        os.close(common)


def _remove_abandoned(layout, users, result):
    """A stored game that every account has uninstalled (a whiteout for its manifest) is deleted."""
    if not users:
        return
    for appid, app in sorted(stored(layout).items(), key=lambda item: int(item[0])):
        try:
            installdir = _installdir(app.get('installdir'))
            if all(_has_whiteout(layout, name, appid) for name in users):
                _delete_game(layout, users, appid, installdir)
                result['removed'].append(appid)
        except (TidyError, OSError) as error:
            result['warnings'].append(f'{appid}: could not be removed: {error}')


def commit(layout, users):
    """The quick step, only called in a slot (no Steam runs, libraries unmounted). Commits every pending record,
    writes validation manifests for the other accounts, removes games every account has uninstalled.
    Returns {'committed': [...], 'dropped': [{'appid','reason'}], 'removed': [...]} and never raises for one game.
    A fourth key, 'warnings', lists what went wrong after a game was already taken."""
    users = list(users)
    result = {'committed': [], 'dropped': [], 'removed': [], 'warnings': []}
    store.resolve_libraries(layout, users)
    with _quiet_lease_signals(), store.locked(layout):
        try:
            top = _layout_dir(layout, 'staging')
        except (OSError, TidyError):
            leftovers = []
        else:
            try:
                leftovers = [name for name in sorted(os.listdir(top)) if APPID.fullmatch(name)]
            finally:
                os.close(top)
        blocked = {appid for appid in leftovers if not _recover(layout, users, appid, result)}
        waiting = _json_files(layout, 'pending')
        for appid in sorted(waiting, key=int):
            if appid not in blocked:
                _commit_one(layout, users, appid, result, waiting)
        _remove_abandoned(layout, users, result)
    return result


# Leaving.

def _chown_tree(fd, uid, gid):
    """Give every folder and file below fd to the account, one by one."""
    _chown(fd, uid, gid)
    for name in os.listdir(fd):
        info = _stat_at(fd, name)
        if info is None:
            continue
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            try:
                _chown_tree(child, uid, gid)
            finally:
                os.close(child)
        else:
            _chown(fd, uid, gid, name)


def _opaque(fd):
    try:
        return os.getxattr(fd, 'trusted.overlay.opaque') == b'y'
    except OSError:
        return False


def _give_back(staged, real, owner):
    """Move the store's files of a game (staged, already the account's) into the account's own game folder. What the
    account has there wins: a file of its own, a folder it made again after deleting it (opaque) and a whiteout (it
    deleted the file) all stand, and the store's copy of that name is dropped. A whiteout that has done its work goes."""
    for name in sorted(os.listdir(staged)):
        info, there = _stat_at(staged, name), _stat_at(real, name)
        if info is None:
            continue
        if there is None:
            os.rename(name, name, src_dir_fd=staged, dst_dir_fd=real)
        elif _is_whiteout(there):
            _remove_entry(staged, name)
            os.unlink(name, dir_fd=real)
        elif stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(there.st_mode):
            inner, target = os.open(name, DIR_FLAGS, dir_fd=staged), os.open(name, DIR_FLAGS, dir_fd=real)
            try:
                if _opaque(target):
                    _remove_entry(staged, name)
                    continue
                _give_back(inner, target, owner)
            finally:
                os.close(inner)
                os.close(target)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=staged)
        else:
            _remove_entry(staged, name)


def _restore_target(layout, users, appid, app):
    """The account that gets the game back: who shared it, else the first account that still has it."""
    shared_by = app.get('shared_by')
    if shared_by in users and not _has_whiteout(layout, shared_by, appid):
        return shared_by
    for name in users:
        if not _has_whiteout(layout, name, appid):
            return name
    return None


def _giving_account(layout, users, target):
    """The account a game of this store goes back to: the one _restore_target names if it has a library on this
    disk, else the first account that has one, even if that account uninstalled the game (nothing is lost that way)."""
    for name in [target] + [other for other in users if other != target]:
        if layout.side(name).real_library is not None:
            return name
    raise TidyError('no account has a Steam library on this disk to give it back to')


def _set_aside_leftovers(layout, users, target, appid, installdir, result):
    """The other accounts' own files of a game that went back to one account: whiteouts go (nothing is hidden any
    more), and what the account itself wrote (its manifest, files in the game folder) is set aside in its conflicts folder
    by rename, so that its stock Steam does not find a game that is not whole."""
    name = f'appmanifest_{appid}.acf'
    for other in users:
        side = layout.side(other)
        if other == target or side.real_library is None:
            continue
        try:
            top = _open_dir(side.real_library)
            try:
                steamapps = _walk(top, ['steamapps'])
            finally:
                os.close(top)
        except (OSError, TidyError):
            continue
        try:
            info = _stat_at(steamapps, name)
            if info is not None and _is_whiteout(info):
                os.unlink(name, dir_fd=steamapps)
            elif info is not None:
                result.setdefault('set_aside', []).append(
                    {'user': other, 'name': store.set_aside(layout, side, 'left-over', ['steamapps'], steamapps, name)})
            _drop_whiteouts(layout, other, installdir)
            try:
                common = _walk(steamapps, ['common'])
            except (OSError, TidyError):
                continue
            try:
                if _stat_at(common, installdir) is not None:
                    result.setdefault('set_aside', []).append({'user': other, 'name': store.set_aside(
                        layout, side, 'left-over', ['steamapps', 'common'], common, installdir)})
            finally:
                os.close(common)
        finally:
            os.close(steamapps)


# A restore that began is written down first: <root>/restoring/<appid>.json says which account the game goes back to.
# Its files move by rename and are then in that account's library, no longer in the store and no longer in staging;
# a restore that stopped there (a power cut, a kill) used to find "no files for it" at the next run and keep the game
# for ever. The note is the last thing a restore removes, after the store's own record of the game.

def _restoring(layout, appid):
    """The note of a restore that began for this game: {'to': account, 'installdir': folder}, or None."""
    try:
        fd = _layout_dir(layout, 'restoring')
    except (OSError, TidyError):
        return None
    try:
        raw = _read_at(fd, f'{appid}.json', 4096)
    finally:
        os.close(fd)
    try:
        note = json.loads(raw) if raw else None
    except ValueError:
        return None
    if isinstance(note, dict) and isinstance(note.get('to'), str) and isinstance(note.get('installdir'), str):
        return note
    return None


def _note_restoring(layout, appid, target, installdir):
    _ensure_root_dir(layout, 'restoring', 0o700)
    fd = _layout_dir(layout, 'restoring')
    try:
        _put_file(fd, f'{appid}.json', json.dumps({'version': 1, 'to': target, 'installdir': installdir}).encode(),
                  0o600, (0, 0))
    finally:
        os.close(fd)


def _end_restoring(layout, appid):
    with contextlib.suppress(OSError, TidyError):
        fd = _layout_dir(layout, 'restoring')
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f'{appid}.json', dir_fd=fd)
            if not os.listdir(fd):
                top = _open_dir(layout.root)
                try:
                    with contextlib.suppress(OSError):
                        os.rmdir('restoring', dir_fd=top)
                finally:
                    os.close(top)
        finally:
            os.close(fd)


def _drop_restoring_without_a_game(layout, held):
    """A restore that stopped after the store's record went and before its own note did: only the note is left."""
    try:
        fd = _layout_dir(layout, 'restoring')
    except (OSError, TidyError):
        return
    try:
        names = [name for name in os.listdir(fd) if name.endswith('.json') and name[:-5] not in held]
    finally:
        os.close(fd)
    for name in names:
        _end_restoring(layout, name[:-5])


def _restore_one(layout, users, appid, app, result):
    if 'error' in app:
        raise TidyError(app['error'])
    installdir = _installdir(app.get('installdir'))
    name = f'appmanifest_{appid}.acf'
    begun = _restoring(layout, appid)
    if begun is not None and (begun['to'] not in users or begun['installdir'] != installdir):
        begun = None
    if begun is not None:
        # An earlier run began to give it to this account and stopped: it goes on to the same one.
        target, given = begun['to'], None
    else:
        target = _restore_target(layout, users, appid, app)
        if target is None:
            _delete_game(layout, users, appid, installdir)
            result['removed'].append(appid)
            return
        wanted = target
        target = _giving_account(layout, users, target)
        given = {'appid': appid, 'to': target, 'instead_of': wanted} if target != wanted else None
    entry = store.account(target)
    side = layout.side(target)
    owner = (entry.pw_uid, entry.pw_gid)
    library = side.real_steamapps
    try:
        lib_fd = _open_dir(library)
    except (OSError, TidyError):
        raise TidyError(f'{target} has no Steam folder to give the game back to') from None
    try:
        if not _same_disk(library, layout.root):
            raise TidyError(f'the Steam folder of {target} is on another disk')
        if os.fstat(lib_fd).st_uid != entry.pw_uid:
            raise TidyError(f'the Steam library of {target} on this disk does not belong to the account')
        own = _stat_at(lib_fd, name)
        if own is not None and _is_whiteout(own):
            os.unlink(name, dir_fd=lib_fd)      # the account uninstalled it, and is given the game back anyway
            own = None
        if own is not None and not stat.S_ISREG(own.st_mode):
            raise TidyError(f'{target} has something that is not a file where the app manifest goes')
        common_info = _lstat_below(library, ['common', installdir])
        if common_info is not None and not stat.S_ISDIR(common_info.st_mode):
            raise TidyError(f'{target} has a file where the game folder {installdir} goes')
        manifest = None
        if own is None:
            canonical = _open_dir(layout.steamapps)
            try:
                manifest = _read_at(canonical, name)
            finally:
                os.close(canonical)
            if manifest is None:
                raise TidyError('the store has no app manifest for it')
        if begun is None:
            _note_restoring(layout, appid, target, installdir)
        _ensure_root_dir(layout, 'staging', 0o700)
        top = _layout_dir(layout, 'staging')
        try:
            stage = _walk(top, [appid], create=True, mode=0o700)
        finally:
            os.close(top)
        try:
            _restore_files(layout, installdir, stage, lib_fd, owner, resumed=begun is not None)
        finally:
            os.close(stage)
        # Where the account has a manifest of its own (its Steam wrote it: an update) that one stays.
        if manifest is not None:
            _as_user(entry, lambda: _write_manifest_as_user(side.steamapps, name, manifest))
    finally:
        os.close(lib_fd)
    # Every step from here can run again. The store's record of the game goes after the other accounts' own files
    # were set aside, so that a restore that stops in between is found and finished; the note goes last of all.
    _set_aside_leftovers(layout, users, target, appid, installdir, result)
    steamapps = _open_dir(layout.steamapps)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=steamapps)
    finally:
        os.close(steamapps)
    with contextlib.suppress(OSError, TidyError):
        top = _layout_dir(layout, 'staging')
        try:
            _remove_empty_tree(top, appid)
        finally:
            os.close(top)
    apps = _layout_dir(layout, 'apps')
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f'{appid}.json', dir_fd=apps)
    finally:
        os.close(apps)
    _end_restoring(layout, appid)
    result['restored'].append(appid)
    if given is not None:
        result.setdefault('given', []).append(given)


def _write_manifest_as_user(library, name, data):
    fd = _open_dir(library)
    try:
        _put_file(fd, name, data, 0o644)
    finally:
        os.close(fd)
    return True


def _restore_files(layout, installdir, stage, lib_fd, owner, resumed=False):
    """Move the stored game folder to the account's library through staging. A game folder the account has already (its
    files over the store's copy) keeps what it has and gets the rest; an interrupted restore goes on from staging, and
    one that stopped after the folder had reached the account (resumed: the restore's note says it began) has nothing
    left to move."""
    try:
        common = _open_dir(layout.common)
    except (OSError, TidyError):
        common = None
    try:
        if common is not None and _stat_at(common, installdir) is not None:
            os.rename(installdir, installdir, src_dir_fd=common, dst_dir_fd=stage)
        elif _stat_at(stage, installdir) is None:      # else: an earlier restore stopped half-way; go on from staging
            back = _lstat_below_fd(lib_fd, ['common', installdir]) if resumed else None
            if back is not None and stat.S_ISDIR(back.st_mode) and back.st_uid == owner[0]:
                return                                  # the files went back before the earlier run stopped
            raise TidyError('the store has no files for it')
        try:
            game = os.open(installdir, DIR_FLAGS, dir_fd=stage)
            try:
                _chown_tree(game, *owner)
            finally:
                os.close(game)
            target_common = _walk(lib_fd, ['common'], create=True, owner=owner)
            try:
                if _stat_at(target_common, installdir) is None:
                    os.rename(installdir, installdir, src_dir_fd=stage, dst_dir_fd=target_common)
                else:
                    staged = os.open(installdir, DIR_FLAGS, dir_fd=stage)
                    real = os.open(installdir, DIR_FLAGS, dir_fd=target_common)
                    try:
                        _give_back(staged, real, owner)
                    finally:
                        os.close(staged)
                        os.close(real)
                    os.rmdir(installdir, dir_fd=stage)
            finally:
                os.close(target_common)
        except BaseException:
            if _stat_at(stage, installdir) is not None:
                game = os.open(installdir, DIR_FLAGS, dir_fd=stage)
                try:
                    _chown_tree(game, 0, 0)
                finally:
                    os.close(game)
                if common is not None and _stat_at(common, installdir) is None:
                    os.rename(installdir, installdir, src_dir_fd=stage, dst_dir_fd=common)
            raise
    finally:
        if common is not None:
            os.close(common)


def _sweep_whiteouts(layout, users, result):
    """Leaving is done: no whiteout of the design is left in any account's real library. The top of steamapps and the
    game folders below common are looked at; Proton prefixes and caches are not the design's."""
    for name in users:
        side = layout.side(name)
        if side.real_library is None:
            continue
        try:
            top = _open_dir(side.real_library)
            try:
                steamapps = _walk(top, ['steamapps'])
            finally:
                os.close(top)
        except (OSError, TidyError):
            continue
        try:
            for entry_name in sorted(os.listdir(steamapps)):
                info = _stat_at(steamapps, entry_name)
                if info is not None and _is_whiteout(info):
                    os.unlink(entry_name, dir_fd=steamapps)
                    result.setdefault('whiteouts_dropped', []).append(f'{name}: steamapps/{entry_name}')
            try:
                common = _walk(steamapps, ['common'])
            except (OSError, TidyError):
                continue
            try:
                for game_name in sorted(os.listdir(common)):
                    info = _stat_at(common, game_name)
                    if info is None:
                        continue
                    if _is_whiteout(info):
                        os.unlink(game_name, dir_fd=common)
                    elif stat.S_ISDIR(info.st_mode):
                        game = os.open(game_name, DIR_FLAGS, dir_fd=common)
                        try:
                            _remove_whiteouts_not_listed(game, '', set())
                        finally:
                            os.close(game)
            finally:
                os.close(common)
        finally:
            os.close(steamapps)


def restore_all(layout, users):
    """Leaving: give every stored game back to the account that shared it.
    Returns {'restored': [...], 'kept': [{'appid','reason'}]}. A game that every account has uninstalled is
    deleted instead and listed under 'removed'. The accounts' stashed manifests come back first, and when every game
    is back no whiteout of the design and nothing of the stash is left in any library."""
    users = list(users)
    result = {'restored': [], 'kept': [], 'removed': []}
    store.resolve_libraries(layout, users)
    with store.locked(layout):
        for name in users:
            side = layout.side(name)
            if side.library is None:
                continue
            try:
                store.restore_manifests(layout, side, store.account(name))
            except (store.StoreError, OSError) as error:
                result['kept'].append({'appid': None, 'reason': f'the stashed manifests of {name} stay: {error}'})
        if result['kept']:
            return result
        _drop_restoring_without_a_game(layout, stored(layout))
        for appid, app in sorted(stored(layout).items(), key=lambda item: int(item[0])):
            try:
                _restore_one(layout, users, appid, app, result)
            except (TidyError, store.StoreError, OSError, ValueError) as error:
                result['kept'].append({'appid': appid, 'reason': str(error)})
        if not result['kept']:
            _sweep_whiteouts(layout, users, result)
    for name in users:
        left = store.conflicts_of(layout.side(name))
        if left:
            result.setdefault('conflicts', {})[name] = left
    return result


# Migration from the layout before one drive per disk. Each account then had a private layer per disk (an upper
# directory below a root-owned folder, bound for the account on a removable disk) under an overlay library of DosPlex's
# own that was on its Steam list next to its real libraries. Now the store is merged into the account's real library,
# and that library is the layer. In the first slot after the update (every Steam closed) the content of each layer moves
# into the real library of the same disk by rename, nothing is overwritten, the old library leaves the account's Steam
# list, and the emptied folders go.
#
# The journal is <root>/migrating-<user>.json. Every step is rename-only and right at whatever point it stops: what has
# moved is in the library, what has not is in the layer, and a second run goes on with the rest. So an interrupted
# migration needs no repair but the next slot, and a finished one does nothing.

def _checkpoint(label):
    """Where the migration may be interrupted in a test; replaced there."""


def _merge_layer(layout, side, source, target, parts, result):
    """Move everything below source (a folder of the old layer) into target (the same folder of the account's library).
    A folder that is in both is merged. A file inside a game folder that is in both: the newer stays in the library and the
    older goes to the account's conflicts folder (label "library" for the library's, "layer" for the layer's), named in the result.
    Anything else that exists in the library already stays, and the layer's item goes aside. Whiteouts move as
    whiteouts."""
    for name in sorted(os.listdir(source)):
        info = _stat_at(source, name)
        if info is None:
            continue
        there = _stat_at(target, name)
        if not parts and name == 'libraryfolder.vdf':
            # What Steam kept about the old library (its content id and label): it is not the real library's, so it
            # is kept aside, and not named as a conflict.
            store.set_aside(layout, side, 'layer', parts, source, name)
        elif there is None:
            os.rename(name, name, src_dir_fd=source, dst_dir_fd=target)
            result['moved'] += 1
        elif stat.S_ISDIR(info.st_mode) and stat.S_ISDIR(there.st_mode):
            inner, below = os.open(name, DIR_FLAGS, dir_fd=source), os.open(name, DIR_FLAGS, dir_fd=target)
            try:
                _merge_layer(layout, side, inner, below, [*parts, name], result)
            finally:
                os.close(inner)
                os.close(below)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=source)
        elif (len(parts) >= 3 and parts[:2] == ['steamapps', 'common']
              and stat.S_ISREG(info.st_mode) and stat.S_ISREG(there.st_mode)
              and info.st_mtime_ns > there.st_mtime_ns):
            # Both have a file inside a game folder (Steam's own files, the library list and the app manifests, never
            # take this way), and the layer's is newer. Under the earlier layout the account's Steam saw the
            # layer's copy (the overlay hid the library's), so that is the one its games last wrote: the save the
            # player knows. The library's older copy, from before the game was shared, goes aside instead (Deck,
            # 2026-11-18: FlatOut 2's saves of 2026 were set aside and those of 2024 kept). Set aside first, then
            # moved: an interruption between the two leaves the name free, and the next run moves the layer's in.
            result['conflicts'].append(store.set_aside(layout, side, 'library', parts, target, name))
            os.rename(name, name, src_dir_fd=source, dst_dir_fd=target)
            result['moved'] += 1
        else:
            result['conflicts'].append(store.set_aside(layout, side, 'layer', parts, source, name))


def _migrate_user(layout, user, proc):
    side = layout.side(user)
    old = side.retired.upper
    journal = layout.root / f'migrating-{user}.json'
    listed = bool(store.registered(layout, user, path=side.retired.library)) if side.retired.library else False
    if not (os.path.lexists(old) or os.path.lexists(journal) or listed):
        return None
    entry = store.account(user)
    result = {'moved': 0, 'conflicts': [], 'listed_off': False}
    if side.real_library is None:
        raise TidyError('has no Steam library on this disk to move its earlier layer into')
    if store.steam_running(entry.pw_uid, proc):
        raise TidyError("Steam is running; the earlier layer is moved only while it is closed")
    for place in (side.retired.library, side.retired.bound):
        if place is not None and store.is_mounted(place):
            raise TidyError(f'{place} is still mounted')
    store.ensure_steamapps(side, entry)
    store._write_json(layout.root, journal.name, {'version': 1, 'phase': 'layers'})
    _checkpoint('begin')
    if os.path.lexists(old):
        source = _open_dir(old)
        try:
            library = _owned_dir(side.real_library, entry.pw_uid, 'the library')
            try:
                _merge_layer(layout, side, source, library, [], result)
            finally:
                os.close(library)
            _prune_empty(source)
        finally:
            os.close(source)
    _checkpoint('layers')
    store._write_json(layout.root, journal.name, {'version': 1, 'phase': 'lists'})
    result['listed_off'] = bool(store.register(layout, user, False, proc=proc, path=side.retired.library))
    _checkpoint('lists')
    store._write_json(layout.root, journal.name, {'version': 1, 'phase': 'cleanup'})
    if os.path.lexists(old):
        try:
            os.rmdir(old)
        except OSError:
            raise TidyError(f'{old} still holds something after the move') from None
    for place in (side.retired.work / 'work', side.retired.work, side.retired.library, side.retired.bound):
        if place is not None:
            with contextlib.suppress(OSError):
                os.rmdir(place)
    _checkpoint('cleanup')
    with contextlib.suppress(FileNotFoundError):
        os.unlink(journal)
    return result


def migrate(layout, users, *, proc=Path('/proc')):
    """Only in a slot: move the earlier layout's private layer of each account into its real library (see above).
    Returns {user: {'moved': n, 'conflicts': [names], 'listed_off': bool}} for the accounts that had work, or
    {'error': reason} for one whose earlier layer could not be emptied (its overlay is not mounted until it is)."""
    users = list(users)
    store.resolve_libraries(layout, users)
    report = {}
    with store.locked(layout):
        for user in users:
            try:
                item = _migrate_user(layout, user, proc)
            except (TidyError, store.StoreError, OSError, ValueError) as error:
                item = {'error': str(error)}
            if item is not None:
                report[user] = item
    return report


# Command line.

def _users(text):
    names = [name for name in (text or '').split(',') if name]
    for name in names:
        if not store.USER_NAME.fullmatch(name):
            raise TidyError(f'invalid user name {name!r}')
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('share', 'commit', 'restore', 'pending', 'shareable'):
        sub = commands.add_parser(name)
        sub.add_argument('--root', required=True)
        if name != 'pending':
            sub.add_argument('--users', required=True, help='the accounts of the store, separated by commas')
        if name in ('share', 'shareable'):
            sub.add_argument('--user', required=True)
        if name == 'share':
            sub.add_argument('--app', required=True)
    arguments = parser.parse_args(argv)
    try:
        layout = store.Layout(arguments.root)
        if arguments.command == 'pending':
            result = pending(layout)
        else:
            users = _users(arguments.users)
            if arguments.command == 'share':
                result = share(layout, arguments.user, arguments.app, users)
            elif arguments.command == 'shareable':
                result = shareable(layout, arguments.user, users)
            elif arguments.command == 'commit':
                result = commit(layout, users)
            else:
                result = restore_all(layout, users)
        print(json.dumps(result, indent=2))
        return 0
    except (TidyError, store.StoreError, OSError, ValueError) as error:
        print(f'dosplex-game-store-tidy: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
