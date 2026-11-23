#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""One shared store of installed games for both accounts (ADR-023).

Each account has its own Steam, so a game both play is downloaded and kept twice. The store holds every game once,
owned by root, and only the files Steam shipped. An account sees it inside its own Steam library, as one drive per
disk (step 4):

    <store root>/                           root, 0755: dosplex-library below /home, or what DOSPLEX_GAME_STORE names
        config.json                         root, 0644: {"version": 1, "mode": "on" | "leaving", "users": [...]}
        pending/  apps/  staging/           share records, what the store holds, files on their way (tidy module)
        store/steamapps/                    the one shared copy: appmanifest_<id>.acf, common/<installdir>/
        sides/<user>/                       root:<user's group>, 0750
            overlay-work/                   the overlay's work directory (root, 0700, on the library's filesystem)
        stash/<user>/  conflicts/<user>/    manifests that wait while the overlay is away; what was never overwritten
        migrating-<user>.json               the journal of the move of the earlier layout's private layer (tidy module)
        disks.json                              root, 0644: {"version": 1, "disks": [{"root": ..., "mount": ...}]}, at most 8

    store/steamapps --read-only bind, root shown as <user>-->  /run/dosplex-library/<user>  (the view)
    overlay: lowerdir=view, upperdir=<account's library>/steamapps, mounted on <account's library>/steamapps

The account's library is the first library of its Steam list that is on the store's filesystem. The overlay sits on its
own upper directory, so everything the account writes lands in its real folder, as on a stock SteamOS; the real folder
is bound at sides/<user>/real while the overlay covers it. The view is only needed while the overlay is being mounted
and is unmounted again right after.

A game enters the store by rename, so a store only takes games from the filesystem it is on. The store above is the
primary. Every other filesystem that holds a Steam library of an account gets a disk store of its own, listed in
disks.json:

    <mount>/dosplex-library/                root, 0755, immutable: the owner of the top folder cannot swap it
        disk/                               the store root of that disk: config.json, store/, sides/<user>/, accounts/
        disk/accounts/<user>/SteamLibrary   the library of an account that has none on that disk (Guest on a card Main
                                            owns), bound at <media>/<user>/<mount name> for the account
    <primary root>/sides/<user>/real-<key>  where the account's real steamapps of that disk is bound (key: 12 hex
                                            digits of the sha256 of the disk's store root)

A disk that is not connected is skipped; the account's library there stays on its Steam list like any drive that is gone.

Nothing writes to the store through a library: writes land in the account's real folder. Only `tidy`, which runs while
no Steam uses the store, changes it (see dosplex-game-store-tidy.py).

    dosplex-game-store.py mount --user USER        put the account's manifests back and mount the overlay (root)
    dosplex-game-store.py unmount --user USER      unmount it; its manifests of shared games go to the stash
    dosplex-game-store.py register --user USER     add the account's own library to its Steam library list
    dosplex-game-store.py unregister --user USER   remove it from the list
    dosplex-game-store.py status [--user USER]...  what is mounted, registered and stored, as JSON
    dosplex-game-store.py on --user USER [--user USER]...   make the store permanent for these accounts (config)
    dosplex-game-store.py off                      set the config to "leaving"; the next slot gives the games back
    dosplex-game-store.py slot [--wait SECONDS]    the moment no Steam of the accounts runs: commit or leave, mount;
                                                   with --wait, first give the listed disks that long to be mounted
    dosplex-game-store.py share --user USER (--app ID... | --all)   record games of that account for the next slot;
                                                   a disk that holds a library of the account gets its store here
    dosplex-game-store.py forget --disk ROOT       take a disk that is not connected off the list for good
    dosplex-game-store.py release [--wait SECONDS] shutdown: unmount every overlay on every disk and stash (root)
    dosplex-game-store.py arrive [--device NAME] [--wait SECONDS]   a disk appeared or went: detach what is left of a
                                                   gone one, mount the overlays on every listed disk that is mounted
    dosplex-game-store.py eject --mount PATH       release a card and have udisks unmount it (the DosPlex-side eject)
    dosplex-game-store.py share --new [--user USER]...   check new or changed games of the accounts and record them

register and unregister refuse while that account's Steam runs: Steam writes the list when it exits. They change
the list as the account's user, never as root, and only an entry whose path is exactly the library folder.
Standard library only; needs util-linux 2.39 or later (X-mount.idmap) and a filesystem that supports idmapped mounts.
"""
import argparse
import array
import contextlib
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

# Below /home: the one large filesystem of a SteamOS device that survives system updates.
DEFAULT_ROOT = str(Path('/home') / 'dosplex-library')
VIEW_ROOT = Path('/run/dosplex-library')
USER_NAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
# A disk store: <mount>/dosplex-library/disk, and the file of the primary that lists them.
WRAPPER, DISK_ROOT, DISKS_NAME = 'dosplex-library', 'disk', 'disks.json'
# What an account that has no library on a disk gets there, and where the drives of an account are mounted.
ACCOUNTS, LIBRARY_FOLDER, MEDIA_ROOT = 'accounts', 'SteamLibrary', Path('/run/media')
DISK_FILESYSTEMS = ('ext4', 'btrfs', 'xfs', 'f2fs')
MAX_DISKS = 8
ROOT_UID = 0
# The inode flag that makes a folder impossible to rename or remove, even for the owner of its parent.
FS_IOC_GETFLAGS, FS_IOC_SETFLAGS, FS_IMMUTABLE_FL = 0x80086601, 0x40086602, 0x10
# The two copies of the library list that Steam keeps, below the account's Steam folder.
LIBRARY_LISTS = ('steamapps/libraryfolders.vdf', 'config/libraryfolders.vdf')


def tidy_module():
    spec = importlib.util.spec_from_file_location('dosplex_game_store_tidy', Path(__file__).with_name('dosplex-game-store-tidy.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def depot_module():
    spec = importlib.util.spec_from_file_location('dosplex_depot', Path(__file__).with_name('dosplex-depot.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreError(RuntimeError):
    """A refusal or a failed step, with a message for the log."""


class Layout:
    """Where everything is, for one store root: the primary store, or the store of one other disk.

    A side's own library (the real Steam library folder of the account on this store's disk) is found by reading the
    account's library list (resolve_libraries); until then a side has none, and the paths that need one are None.
    """

    def __init__(self, root=None, view_root=VIEW_ROOT, *, primary=None, mount_point=None, media_root=MEDIA_ROOT):
        root = Path(root or os.environ.get('DOSPLEX_GAME_STORE') or DEFAULT_ROOT)
        if not root.is_absolute() or '..' in root.parts:
            raise StoreError('the store must be an absolute path')
        self.root = root
        self.store = root / 'store'
        self.steamapps = self.store / 'steamapps'
        self.common = self.steamapps / 'common'
        self.sides = root / 'sides'
        self.lock = root / '.lock'
        self.config = root / 'config.json'
        self.view_root = Path(view_root)
        self.media_root = Path(media_root)
        self.primary = primary
        self.mount_point = Path(mount_point) if mount_point is not None else None
        self.key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        self.disks = root / DISKS_NAME
        # name -> (library as the account reaches it, library as root reaches it): filled by resolve_libraries.
        self.libraries = {}

    @classmethod
    def for_disk(cls, primary, mount):
        """The store of the disk mounted at mount: <mount>/dosplex-library/disk, listed by the primary."""
        mount = Path(mount)
        if not mount.is_absolute() or '..' in mount.parts:
            raise StoreError('the mount point must be an absolute path')
        return cls(mount / WRAPPER / DISK_ROOT, primary.view_root, primary=primary, mount_point=mount,
                   media_root=primary.media_root)

    @property
    def wrapper(self):
        """The folder that holds a disk store's root; the primary has none."""
        return self.root.parent if self.primary is not None else None

    @property
    def view_folders(self):
        """The folders, outermost first, that must exist before a view folder can be made."""
        if self.primary is None:
            return [self.view_root]
        outer = Path(str(self.view_root) + '-disks')
        return [outer, outer / self.key]

    def provisioned(self, name):
        """(visible, real, media bind) of the library a disk store gives an account that has none on that disk: the
        folder the account sees below its own drives folder, the folder root works in, and the folder on the disk
        that is bound to the first one's parent. None for the primary."""
        if self.primary is None or self.mount_point is None:
            return None
        if not USER_NAME.fullmatch(name):
            raise StoreError(f'invalid user name {name!r}')
        home = self.root / ACCOUNTS / name
        bind = self.media_root / name / self.mount_point.name
        return bind / LIBRARY_FOLDER, home / LIBRARY_FOLDER, (home, bind)

    def side(self, name, library=None, real=None):
        if not USER_NAME.fullmatch(name):
            raise StoreError(f'invalid user name {name!r}')
        if library is None and name in self.libraries:
            library, real = self.libraries[name]
        base = self.sides / name
        own = (self.primary or self).sides / name
        if self.primary is None:
            view, tail = self.view_root / name, ''
        else:
            # The account may not be able to walk to a removable disk's mount point (SteamOS opens /run/media/<user>
            # to that one user only), so its views and its way to the real folder are below the primary.
            view, tail = self.view_folders[-1] / name, f'-{self.key}'
        # Folders of the layout before one drive per disk: the account's private layer and the library folder that
        # was an overlay of its own. They are emptied into the account's real library (the migration) and removed.
        retired = Retired(base / 'upper', base / 'work', own / ('library' + tail), own / ('layer' + tail) if tail else None)
        if library is not None:
            library = Path(library)
            real = Path(real) if real is not None else library
            for path in (library, real):
                if ':' in str(path):
                    raise StoreError(f'{path} cannot be a library folder: it has a colon')
        return Side(name, base, base / 'overlay-work', view, own / ('real' + tail), library, real,
                    self.root / 'stash' / name, self.root / 'conflicts' / name, retired,
                    self.provisioned(name))

    def retired_libraries(self, name):
        """The library folders that were DosPlex's own overlays (the primary's and every disk's), present or not.
        They are taken off the account's Steam list by the migration, and are never an account's real library."""
        primary = self.primary or self
        found = {primary.side(name).retired.library}
        for disk in read_disks(primary, owners=(0, os.geteuid())):
            found.add(Layout.for_disk(primary, disk['mount']).side(name).retired.library)
        return found


class Retired:
    """The places of the earlier layout (two libraries per account, a private layer under a root-owned folder)."""
    def __init__(self, upper, work, library, bound):
        self.upper, self.work, self.library, self.bound = upper, work, library, bound


class Side:
    """The folders of one account in one store.

    library is the account's real Steam library folder on this store's disk, as the account reaches it (what its
    library list names); real is the same folder as root reaches it. They differ only for a library that DosPlex
    gave an account on a disk it cannot enter: there the account's folder on the disk is bound below the account's own
    drives folder, and a rename between two mounts is refused, so root works at `real`.

    The overlay is mounted on library/steamapps, over its own upper directory. reach is where the real
    library/steamapps is bound while the overlay covers it: the account's slow checks read it there, so that they only
    ever see the account's own files. work is the overlay's work directory; view is the read-only bind of the store
    that is the overlay's lower layer.
    """
    def __init__(self, name, base, work, view, reach, library, real, stash, conflicts, retired, provisioned):
        self.name, self.base, self.work, self.view, self.reach = name, base, work, view, reach
        self.library, self.real_library = library, real
        self.stash, self.conflicts, self.retired = stash, conflicts, retired
        self.provisioned = provisioned

    @property
    def steamapps(self):
        return None if self.library is None else self.library / 'steamapps'

    @property
    def real_steamapps(self):
        return None if self.real_library is None else self.real_library / 'steamapps'

    def root_path(self, path):
        """The path as root reaches it, for a path below the account's library."""
        path = Path(path)
        if self.library is not None and self.real_library != self.library:
            try:
                return self.real_library / path.relative_to(self.library)
            except ValueError:
                pass
        return path


def account(name):
    """The passwd entry of an ordinary user."""
    if not USER_NAME.fullmatch(name):
        raise StoreError(f'invalid user name {name!r}')
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise StoreError(f'no account {name}') from None
    if entry.pw_uid < 1000 or entry.pw_gid < 1000:
        raise StoreError(f'{name} is not an ordinary user')
    return entry


def mount_points(mountinfo='/proc/self/mountinfo'):
    """mount point -> (filesystem type, mount options, super options) of the mount that is on top there."""
    found = {}
    with open(mountinfo) as stream:
        for line in stream:
            fields = line.split()
            separator = fields.index('-')
            point = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
            found[point] = (fields[separator + 1], fields[5], fields[separator + 3] if len(fields) > separator + 3 else '')
    return found


def mount_roots(mountinfo='/proc/self/mountinfo'):
    """mount point -> the folder of its filesystem that is mounted there ("/" for a whole filesystem)."""
    found = {}
    with open(mountinfo) as stream:
        for line in stream:
            fields = line.split()
            if len(fields) > 4:
                point = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
                found[point] = fields[3]
    return found


def mount_sources(mountinfo='/proc/self/mountinfo'):
    """mount point -> the source of the mount that is on top there (a device node such as /dev/sdb1, or a name)."""
    found = {}
    with open(mountinfo) as stream:
        for line in stream:
            fields = line.split()
            if '-' not in fields:
                continue
            separator = fields.index('-')
            if len(fields) > separator + 2:
                point = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
                found[point] = fields[separator + 2].replace('\\040', ' ')
    return found


def is_mounted(path, mountinfo='/proc/self/mountinfo'):
    return str(path) in mount_points(mountinfo)


def view_command(layout, side, entry):
    """Read-only bind of the store's steamapps whose root-owned files appear as the user's."""
    options = f'ro,nosuid,nodev,X-mount.idmap=u:0:{entry.pw_uid}:1 g:0:{entry.pw_gid}:1'
    return ['mount', '--bind', '-o', options, str(layout.steamapps), str(side.view)]


def overlay_command(side):
    """The overlay that merges the store into the account's own library: the store's steamapps below, the account's
    real steamapps on top and as the mount point, and a work directory of root's on the same filesystem."""
    for path in (side.view, side.steamapps, side.real_steamapps, side.work):
        if path is None or any(character in str(path) for character in ',:\\'):
            raise StoreError(f'{path} cannot be an overlay layer: it has a comma, a colon or a backslash')
    # SteamOS mounts overlays with index, metacopy and redirect_dir on by default. With any of them the lower layer must
    # not change while the overlay is unmounted, and a file in the private layer can hold metadata only. Off, the
    # account's folder holds whole files and the store may change between mounts (the tidy-up does exactly that).
    # uuid=off: the overlay sits on the filesystem of its own upper directory and must not take over its identity.
    # The upper directory is named as root reaches it (real_steamapps): the kernel wants it under the same mount as the
    # work directory. For a library DosPlex gave an account on a card the account sees it through a bind of its own,
    # another mount of the same filesystem, and the overlay was refused there ("workdir and upperdir must reside under
    # the same mount", Deck, 2026-11-18). The overlay is still mounted where the account sees the folder.
    options = (f'nosuid,nodev,index=off,metacopy=off,redirect_dir=off,xino=off,uuid=off,'
               f'lowerdir={side.view},upperdir={side.real_steamapps},workdir={side.work}')
    return ['mount', '-t', 'overlay', 'dosplex-library', '-o', options, str(side.steamapps)]


def reach_commands(side):
    """Bind the account's real steamapps to its reach, made private so that the overlay mounted on the real folder
    later does not show up in the bind too."""
    return [['mount', '--bind', '-o', 'nosuid,nodev', str(side.real_steamapps), str(side.reach)],
            ['mount', '--make-private', str(side.reach)]]


def prefix_command(side):
    """Bind the account's real compatdata over the overlay's: the Proton prefixes never go through the overlay.

    SteamOS makes each new prefix (compatdata/<appid>/pfx) case-insensitive (ext4 casefold), and overlayfs refuses a
    casefolded directory in a layer: every lookup in the prefix failed ("failed lookup in upper ... child wrong
    casefold", EREMOTE) and a Proton game could not open its own prefix (Deck, 2026-11-18). Nothing below compatdata is
    ever the store's, so the account's own folder is mounted there as it is, from the reach (the real steamapps)."""
    return ['mount', '--bind', '-o', 'nosuid,nodev', str(side.reach / 'compatdata'), str(side.steamapps / 'compatdata')]


def ensure_prefix_folder(side, entry):
    """The account's compatdata in its real steamapps (through the reach), made as the account's if it is missing."""
    folder = side.reach / 'compatdata'
    try:
        os.mkdir(folder, 0o755)
    except FileExistsError:
        pass
    else:
        os.chown(folder, entry.pw_uid, entry.pw_gid, follow_symlinks=False)
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid:
        raise StoreError(f'{folder} is not a folder of the account')


def is_whiteout(info):
    """An overlay whiteout: a character device 0/0 that hides what is below it. Tests replace this function."""
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def real_directory(path, *, uid=None, gid=None, mode=None):
    """Make path a directory with this owner and mode; refuse a link or anything else that is there."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f'{path} is not a directory')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if uid is not None and (info.st_uid, info.st_gid) != (uid, gid):
            os.fchown(descriptor, uid, gid)
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def get_immutable(descriptor):
    """Whether the inode flag "immutable" is set on the open file or folder. Tests replace this function."""
    flags = array.array('l', [0])
    try:
        fcntl.ioctl(descriptor, FS_IOC_GETFLAGS, flags, True)
    except OSError as error:
        raise StoreError(f'this filesystem cannot tell the immutable flag ({error.strerror})') from None
    return bool(flags[0] & FS_IMMUTABLE_FL)


def set_immutable(descriptor, value=True):
    """Set or clear the inode flag "immutable" on an open folder. Tests replace this function."""
    flags = array.array('l', [0])
    try:
        fcntl.ioctl(descriptor, FS_IOC_GETFLAGS, flags, True)
        flags[0] = (flags[0] | FS_IMMUTABLE_FL) if value else (flags[0] & ~FS_IMMUTABLE_FL)
        fcntl.ioctl(descriptor, FS_IOC_SETFLAGS, flags)
    except OSError as error:
        raise StoreError(f'this filesystem cannot take the immutable flag ({error.strerror})') from None


def check_wrapper(layout):
    """A disk store's wrapper folder must be root's, closed to others and immutable. Raises StoreError."""
    wrapper = layout.wrapper
    try:
        info = os.lstat(wrapper)
        descriptor = os.open(wrapper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise StoreError(f'{wrapper} cannot be opened as a folder: {error.strerror}') from None
    try:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise StoreError(f'{wrapper} must be a folder of root that others cannot write')
        if not get_immutable(descriptor):
            raise StoreError(f'{wrapper} is not immutable')
    finally:
        os.close(descriptor)


def prepare_store(layout):
    """The store's own folders: root's, readable by everyone, writable by nobody else."""
    parent = os.lstat(layout.root.parent)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != ROOT_UID or parent.st_mode & 0o022:
        raise StoreError(f'{layout.root.parent} must be a directory that only root can change')
    if layout.primary is not None:
        # An immutable wrapper takes no new entry: the store root was made before the flag, and must be there.
        check_wrapper(layout)
        if not stat.S_ISDIR(os.lstat(layout.root).st_mode):
            raise StoreError(f'{layout.root} is not a folder')
    for path in (layout.root, layout.store, layout.steamapps, layout.common, layout.sides):
        real_directory(path, uid=0, gid=0, mode=0o755)


def prepare_side(layout, side, entry):
    """The account's folders of root's: its place below the root, the overlay's work directory and the mount point of
    the reach. Their parent is root's, so the account cannot swap one for a link."""
    if layout.primary is not None:
        # The account reaches the view and the real folder through its own folder below the primary.
        real_directory(layout.primary.sides / side.name, uid=0, gid=entry.pw_gid, mode=0o750)
    real_directory(side.base, uid=0, gid=entry.pw_gid, mode=0o750)
    real_directory(side.work, uid=0, gid=0, mode=0o700)
    if not is_mount_point(side.reach):
        real_directory(side.reach, uid=0, gid=0, mode=0o755)
    for folder in layout.view_folders:
        folder.mkdir(mode=0o755, exist_ok=True)
    real_directory(side.view, uid=0, gid=0, mode=0o755)


def is_mount_point(path):
    """Whether something is mounted on the folder now, asked of the kernel's own list. Tests replace this function."""
    try:
        return is_mounted(path)
    except OSError:
        return os.path.ismount(path)


def run(command, runner=subprocess.run):
    result = runner(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise StoreError(f'{" ".join(command[:3])} failed: {(result.stderr or result.stdout).strip()}')


@contextlib.contextmanager
def locked(layout):
    """One changing command at a time for a store."""
    descriptor = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def overlay_mounts(side, mountinfo='/proc/self/mountinfo'):
    """The mount points of the account's overlay, found by its work directory: that is how the mount is told from any
    other, whatever library the account has by now."""
    wanted = f'workdir={side.work}'
    return [point for point, (kind, _options, super_options) in mount_points(mountinfo).items()
            if kind == 'overlay' and wanted in _unescaped(super_options).split(',')]


def _unescaped(text):
    """A mount table field with the octal escapes of space, tab and backslash undone."""
    return text.replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def plain_folder(path, uid):
    """Refuse a library folder that the account could point somewhere else: root mounts on it.

    Every component must be a real folder; a link is accepted only where root made it (/home pointing to /var/home).
    The folder itself must be the account's. Nothing here closes the window between this check and the mount for a
    process of the account that runs meanwhile; the mount is made while the account's Steam is closed.
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise StoreError(f'{path} is not an absolute path without ".."')
    current = Path('/')
    for part in path.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise StoreError(f'{current} cannot be looked at: {error.strerror}') from None
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != ROOT_UID:
                raise StoreError(f'{current} is a link that is not root\'s; a library is never mounted over a link')
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise StoreError(f'{current} is not a folder')
    if os.stat(path).st_uid != uid:
        raise StoreError(f'{path} does not belong to the account')


def ensure_steamapps(side, entry):
    """The account's steamapps folder in its real library, made (as the account's) when the library is there and it
    is not. The library itself is never made here."""
    if side.real_library is None:
        raise StoreError(f'{side.name} has no Steam library on this disk')
    try:
        top = os.open(side.real_library, DIR_FLAGS)
    except OSError as error:
        raise StoreError(f'{side.real_library} is not a folder: {error.strerror}') from None
    try:
        try:
            os.mkdir('steamapps', 0o755, dir_fd=top)
        except FileExistsError:
            pass
        else:
            if os.geteuid() == 0:
                inner = os.open('steamapps', DIR_FLAGS, dir_fd=top)
                try:
                    os.fchown(inner, entry.pw_uid, entry.pw_gid)
                finally:
                    os.close(inner)
        os.close(os.open('steamapps', DIR_FLAGS, dir_fd=top))
    except OSError as error:
        raise StoreError(f'{side.real_steamapps} is not a folder: {error.strerror}') from None
    finally:
        os.close(top)


def library_paths(entry, vdf):
    """The library folders in the account's list, the default Steam folder first (run as the account)."""
    steam = steam_root_of(entry)
    found = [str(steam)]
    try:
        _name, folders = vdf.read_vdf(steam / 'steamapps' / 'libraryfolders.vdf')
    except (OSError, ValueError):
        folders = {}
    for value in folders.values():
        raw = value.get('path') if isinstance(value, dict) else None
        if (isinstance(raw, str) and '\0' not in raw and raw.startswith('/') and '..' not in Path(raw).parts
                and raw not in found):
            found.append(raw)
    return found


def library_list(entry, name):
    """The library paths the account's list names (both copies together), read as the account."""
    vdf = depot_module()

    def look():
        found = set()
        for relative in LIBRARY_LISTS:
            try:
                _name, folders = vdf.read_vdf(steam_root_of(entry) / relative)
            except (OSError, ValueError):
                continue
            found |= {value.get('path') for value in folders.values() if isinstance(value, dict)}
        return sorted(item for item in found if isinstance(item, str))
    return as_user(entry, look)


def change_library(entry, library, add):
    """Add a library folder to the account's list, or take it off; returns the copies changed. Steam must be closed."""
    vdf = depot_module()
    root = steam_root_of(entry)
    return as_user(entry, lambda: change_lists(root, library, add, vdf))


def resolve_library(layout, name, entry=None):
    """(visible, real) of the account's own Steam library on this store's disk, or None.

    The first library of the account's list that is on the store's filesystem; none of DosPlex's earlier library
    folders counts. A disk where the list has none gives the library DosPlex made there (provisioned), if it exists.
    """
    entry = entry or account(name)
    vdf = depot_module()
    paths = as_user(entry, lambda: library_paths(entry, vdf))
    retired = (layout.primary or layout).retired_libraries(name)
    device = device_of(layout.root)
    given = layout.provisioned(name)
    for text in paths:
        path = Path(text)
        if path in retired:
            continue
        try:
            if device_of(path) != device or not (path / 'steamapps').is_dir():
                continue
        except OSError:
            continue
        return path, (given[1] if given is not None and path == given[0] else path)
    if given is not None and given[1].is_dir():
        return given[0], given[1]
    return None


def resolve_libraries(layout, users):
    """Fill layout.libraries for every account that has none yet; an account without a library stays out of it."""
    for name in users:
        if name in layout.libraries:
            continue
        try:
            found = resolve_library(layout, name)
        except (StoreError, OSError):
            found = None
        if found is not None:
            layout.libraries[name] = found


def provision(layout, name, entry, *, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc')):
    """Give an account that has no library on a disk store's disk one: <disk store>/accounts/<name>/SteamLibrary,
    owned by the account, reachable for it at <media>/<name>/<mount name>/SteamLibrary (a bind of the account's
    folder), and on its library list like any other library. Doing it again changes nothing. Returns what it did."""
    if layout.primary is None:
        raise StoreError('only a disk store gives an account a library')
    visible, real, (home, bind) = layout.provisioned(name)
    if Path(bind) == layout.mount_point:
        raise StoreError(f'{name} reaches this disk at its own mount point; it needs no library made for it')
    done = []
    real_directory(layout.root / ACCOUNTS, uid=0, gid=0, mode=0o755)
    real_directory(home, uid=entry.pw_uid, gid=entry.pw_gid, mode=0o700)
    real_directory(real, uid=entry.pw_uid, gid=entry.pw_gid, mode=0o755)
    # The account owns the library folder and could swap it for a link: steamapps is made through a descriptor of it.
    ensure_steamapps(SimpleNamespace(name=name, real_library=real, real_steamapps=real / 'steamapps'), entry)
    if not is_mounted(bind, mountinfo):
        real_directory(layout.media_root / name, uid=0, gid=entry.pw_gid, mode=0o750)
        real_directory(bind, uid=0, gid=0, mode=0o755)
        run(['mount', '--bind', '-o', 'nosuid,nodev', str(home), str(bind)], runner)
        done.append('media')
    if str(visible) not in library_list(entry, name):
        if steam_running(entry.pw_uid, proc):
            raise StoreError(f"{name}'s Steam is running; its library list can only be changed while it is closed")
        change_library(entry, visible, True)
        done.append('listed')
    return done


def resolve_side(layout, name, entry, *, provisioning=False, runner=subprocess.run, mountinfo='/proc/self/mountinfo',
                 proc=Path('/proc'), done=None):
    """The account's side with its real library found; with provisioning a disk store gives an account its own."""
    found = layout.libraries.get(name) or resolve_library(layout, name, entry)
    given = layout.provisioned(name)
    if found is None and provisioning and given is not None:
        steps = provision(layout, name, entry, runner=runner, mountinfo=mountinfo, proc=proc)
        found = resolve_library(layout, name, entry)
    elif found is not None and provisioning and given is not None and found[0] == given[0]:
        # The library DosPlex made: its bind is gone after a reboot, and the list may have lost the entry.
        steps = provision(layout, name, entry, runner=runner, mountinfo=mountinfo, proc=proc)
    else:
        steps = []
    if done is not None:
        done.extend(steps)
    if found is None:
        raise StoreError(f'{name} has no Steam library on this disk' if layout.primary is not None
                         else f'{name} has no Steam folder yet (Steam makes it when it first starts)')
    layout.libraries[name] = found
    return layout.side(name)


def migration_pending(layout, side):
    """Whether the earlier layout (a private layer of this account) still has to be emptied into its library."""
    return os.path.lexists(side.retired.upper) or os.path.lexists(layout.root / f'migrating-{side.name}.json')


def mount(layout, name, *, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc'),
          while_running=False):
    """Merge the store into the account's own library: the overlay on its steamapps folder. Returns what it did;
    doing it again changes nothing. The account's Steam must be closed (its manifests come back first).

    With while_running (a card that arrives while a Steam runs) the overlay is mounted even if the account's Steam runs:
    mounting over a folder adds nothing to the real folder. What changes the real folder (putting the stashed manifests
    back) is left for the next time the account's Steam is closed; that is named in the result ('manifests wait').
    """
    entry = account(name)
    prepare_store(layout)
    done = []
    with locked(layout):
        side = resolve_side(layout, name, entry, provisioning=True, runner=runner, mountinfo=mountinfo, proc=proc,
                            done=done)
        if migration_pending(layout, side):
            raise StoreError('the private layer of the earlier layout has not been moved into the library yet; '
                             'the next slot does that')
        prepare_side(layout, side, entry)
        plain_folder(side.real_library, entry.pw_uid)
        ensure_steamapps(side, entry)
        if overlay_mounts(side, mountinfo):
            return done
        bound = False
        try:
            if while_running and steam_running(entry.pw_uid, proc):
                if os.path.lexists(side.stash / 'steamapps'):
                    done.append('manifests wait')
            else:
                restore_manifests(layout, side, entry, proc=proc)
            if not is_mounted(side.reach, mountinfo):
                for command in reach_commands(side):
                    run(command, runner)
                bound = True
                done.append('real')
            if not is_mounted(side.view, mountinfo):
                run(view_command(layout, side, entry), runner)
            try:
                run(overlay_command(side), runner)
                done.append('library')
                ensure_prefix_folder(side, entry)
                if not is_mounted(side.steamapps / 'compatdata', mountinfo):
                    run(prefix_command(side), runner)
                done.append('prefixes')
            finally:
                # The overlay holds the view by itself from here on (it works on with the view's mount point empty), so
                # the view goes: one mount per account stays, and nothing of it is left in /run.
                gone = runner(['umount', str(side.view)], capture_output=True, text=True, timeout=60)
                if gone.returncode != 0:
                    done.append('view left mounted')
        except BaseException:
            # No overlay: the account's Steam must not find a manifest of a shared game whose files it cannot see.
            for point in overlay_mounts(side, mountinfo) if 'library' in done else ():
                with contextlib.suppress(StoreError, OSError):
                    if is_mounted(Path(point) / 'compatdata', mountinfo):
                        run(['umount', str(Path(point) / 'compatdata')], runner)
                with contextlib.suppress(StoreError, OSError):
                    run(['umount', point], runner)
            if bound:
                with contextlib.suppress(StoreError, OSError):
                    run(['umount', str(side.reach)], runner)
            with contextlib.suppress(StoreError, OSError):
                stash_manifests(layout, side, entry, proc=proc)
            raise
    return done


def unmount(layout, name, *, runner=subprocess.run, mountinfo='/proc/self/mountinfo', layer=False, stash=True,
            proc=Path('/proc')):
    """Take the overlay, the view and the reach away. A folder that is in use stays, and that is an error.

    With stash, the account's app manifests for the games the store holds, and the whiteouts for them, go out of its
    real library into a stash of root's on the same filesystem (stock Steam would offer to download those games
    again); the next mount puts them back. The slot does not stash: it commits and mounts again with no Steam between.
    The account's media bind of a library DosPlex made stays, because the tidy work between an unmount and the next
    mount needs it; layer=True takes it away too (the store is leaving).
    """
    side = layout.side(name)
    done = []
    if not layout.root.is_dir():
        return done
    places = []
    for point in overlay_mounts(side, mountinfo):
        places += [('prefixes', Path(point) / 'compatdata'), ('library', Path(point))]
    places += [('view', side.view), ('real', side.reach), ('earlier', side.retired.library)]
    if side.retired.bound is not None:
        places.append(('earlier layer', side.retired.bound))
    # The media bind is DosPlex's only for an account SteamOS does not mount the disk for. For the account it mounts
    # the disk for (Main) the same path is the disk's own mount point: unmounting it there would eject the card, and
    # at shutdown it was tried and refused as busy, which ended the release before the rest (Deck, 2026-11-18).
    if layer and side.provisioned is not None and Path(side.provisioned[2][1]) != layout.mount_point:
        places.append(('media', side.provisioned[2][1]))
    with locked(layout):
        for label, path in places:
            if is_mounted(path, mountinfo):
                try:
                    run(['umount', str(path)], runner)
                except StoreError:
                    # The prefix bind sits on top of the overlay and has to go first. An overlay that is busy then stays
                    # mounted, and without the bind every prefix lookup in it fails (EREMOTE) until a full remount: the
                    # bind goes back, so that a failed unmount leaves the mounts as they were.
                    if label == 'library' and 'prefixes' in done:
                        with contextlib.suppress(StoreError, OSError):
                            run(prefix_command(side), runner)
                    raise
                done.append(label)
        if stash:
            stash_account(layout, name, proc=proc)
    return done


def stash_account(layout, name, *, proc=Path('/proc')):
    """Stash the manifests of the stored games out of the account's real library (stash_manifests), finding the
    library first. Returns the entries moved; none when the account has no library on this disk."""
    entry = account(name)
    try:
        side = resolve_side(layout, name, entry)
    except StoreError:
        return []
    return stash_manifests(layout, side, entry, proc=proc)


def restore_account(layout, name, *, proc=Path('/proc')):
    """Put the stashed manifests back into the account's real library (restore_manifests), finding the library first.
    Returns the entries put back; none when the account has no library on this disk."""
    entry = account(name)
    try:
        side = resolve_side(layout, name, entry)
    except StoreError:
        return []
    with locked(layout):
        return restore_manifests(layout, side, entry, proc=proc)


# The stash. Stock Steam, started without the overlay, must not find an app manifest of a shared game whose files it
# cannot see: it would offer to download the whole game again. Whenever the account's library stays unmounted, its
# manifests for the games the store holds (and the whiteouts for those games) are moved, by rename and nothing else,
# out of its real library into <root>/stash/<user>/steamapps on the same filesystem, and moved back before the next
# mount. Both steps run with the account's Steam closed. The journal says which step was under way; each step is made of
# renames that are right whenever they stop, and running the other step finishes what was begun.

STASH_JOURNAL = 'journal.json'


def held_apps(layout):
    """appid -> install folder of every game the store holds, from <root>/apps/<appid>.json."""
    found = {}
    folder = layout.root / 'apps'
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        match = re.fullmatch(r'([0-9]{1,10})\.json', name)
        if not match:
            continue
        try:
            data = json.loads((folder / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        installdir = data.get('installdir') if isinstance(data, dict) else None
        if isinstance(installdir, str) and installdir and '/' not in installdir and installdir not in ('.', '..'):
            found[match.group(1)] = installdir
    return found


def _stat_at(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _write_json(folder, name, value):
    handle, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=folder)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, Path(folder) / name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _dir_below(fd, parts, *, create=False, owner=None):
    """Open the folder at parts below fd, one component at a time and never through a link. Made when missing (mode
    0700, or the account's mode 0755 and owner when an owner is given). Returns a new descriptor."""
    current = os.dup(fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755 if owner else 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                else:
                    if owner is not None and os.geteuid() == 0:
                        made = os.open(part, DIR_FLAGS, dir_fd=current)
                        try:
                            os.fchown(made, *owner)
                        finally:
                            os.close(made)
            following = os.open(part, DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    return current


def _root_folder(layout, *parts):
    """An open descriptor of a folder below the store root that root makes (0700) when it is missing."""
    top = os.open(layout.root, DIR_FLAGS)
    try:
        return _dir_below(top, parts, create=True)
    finally:
        os.close(top)


def set_aside(layout, side, label, parts, source_fd, name):
    """Move an entry of the account that cannot go where it was meant to (something is there already) to
    <root>/conflicts/<user>/<label>/<parts>/<name>, by rename. A name that is taken there gets a number. Returns the
    relative name it has there."""
    folder = _root_folder(layout, 'conflicts', side.name, label, *parts)
    try:
        target, number = name, 0
        while _stat_at(folder, target) is not None:
            number += 1
            target = f'{name}.{number}'
        os.rename(name, target, src_dir_fd=source_fd, dst_dir_fd=folder)
    finally:
        os.close(folder)
    return '/'.join([label, *parts, target])


def conflicts_of(side, limit=50):
    """The entries kept in the account's conflicts folder, as relative names (at most limit, then a count)."""
    found = []
    try:
        top = os.open(side.conflicts, DIR_FLAGS)
    except OSError:
        return found

    def walk(fd, prefix):
        for name in sorted(os.listdir(fd)):
            if len(found) >= limit:
                return
            info = _stat_at(fd, name)
            if info is None:
                continue
            found.append(prefix + name)
    try:
        for label in sorted(os.listdir(top)):
            inner = os.open(label, DIR_FLAGS, dir_fd=top)
            try:
                walk(inner, label + '/')
            finally:
                os.close(inner)
    finally:
        os.close(top)
    return found


def _stashable(fd, apps):
    """The entries of a real steamapps folder that belong in the stash, as lists of path parts: the app manifest of every
    game the store holds (a file or a whiteout), and every whiteout below the game's folder."""
    found = []
    for appid, installdir in sorted(apps.items(), key=lambda item: int(item[0])):
        name = f'appmanifest_{appid}.acf'
        info = _stat_at(fd, name)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            found.append([name])
        try:
            common = os.open('common', DIR_FLAGS, dir_fd=fd)
        except OSError:
            continue
        try:
            info = _stat_at(common, installdir)
            if info is None:
                continue
            if is_whiteout(info):
                found.append(['common', installdir])
            elif stat.S_ISDIR(info.st_mode):
                game = os.open(installdir, DIR_FLAGS, dir_fd=common)
                try:
                    found += [['common', installdir, *parts] for parts in _whiteouts_below(game)]
                finally:
                    os.close(game)
        finally:
            os.close(common)
    return found


def _whiteouts_below(fd, prefix=()):
    found = []
    for name in sorted(os.listdir(fd)):
        info = _stat_at(fd, name)
        if info is None:
            continue
        if is_whiteout(info):
            found.append([*prefix, name])
        elif stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            try:
                found += _whiteouts_below(child, (*prefix, name))
            finally:
                os.close(child)
    return found


def _stash_state(layout, side, phase, apps):
    folder = _root_folder(layout, 'stash', side.name)
    try:
        _write_json(Path(layout.root) / 'stash' / side.name, STASH_JOURNAL, {'version': 1, 'phase': phase, 'apps': sorted(apps)})
    finally:
        os.close(folder)


def stash_manifests(layout, side, entry, *, proc=Path('/proc')):
    """Move the account's manifests of stored games, and the whiteouts for them, out of its real library. Returns the
    entries moved (relative to steamapps). The account's Steam must be closed and the overlay must not be mounted."""
    apps = held_apps(layout)
    if not apps or side.real_library is None:
        return []
    try:
        top = os.open(side.real_steamapps, DIR_FLAGS)
    except OSError:
        return []
    moved = []
    try:
        wanted = _stashable(top, apps)
        if not wanted:
            return []
        if steam_running(entry.pw_uid, proc):
            raise StoreError(f"{side.name}'s Steam is running; its manifests can only be moved while it is closed")
        plain_folder(side.real_steamapps, entry.pw_uid)
        _stash_state(layout, side, 'stashing', apps)
        stash = _root_folder(layout, 'stash', side.name, 'steamapps')
        try:
            for parts in wanted:
                source = _dir_below(top, parts[:-1])
                try:
                    target = _dir_below(stash, parts[:-1], create=True)
                    try:
                        if _stat_at(target, parts[-1]) is not None:
                            if len(parts) == 1 and _newer(_buildid_below(source, parts[-1]), _buildid_below(target, parts[-1])):
                                # The library's manifest is the newer one: it goes to the stash, and the stashed one
                                # is kept in the account's conflicts (LIB-07).
                                set_aside(layout, side, 'stash', parts[:-1], target, parts[-1])
                                os.rename(parts[-1], parts[-1], src_dir_fd=source, dst_dir_fd=target)
                            else:
                                set_aside(layout, side, 'stash', parts[:-1], source, parts[-1])
                        else:
                            os.rename(parts[-1], parts[-1], src_dir_fd=source, dst_dir_fd=target)
                        moved.append('/'.join(parts))
                    finally:
                        os.close(target)
                finally:
                    os.close(source)
        finally:
            os.close(stash)
        _stash_state(layout, side, 'stashed', apps)
    finally:
        os.close(top)
    return sorted(moved)


def _buildid_below(fd, name):
    """The build number of the app manifest at name below fd, as an integer, read on the descriptor the stash work
    holds: None when the file is not there or cannot be read (LIB-07)."""
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except OSError:
        return None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            return None
        data = b''
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(handle, min(65536, remaining))
            if not chunk:
                break
            data += chunk
            remaining -= len(chunk)
        try:
            _section, state = depot_module().vdf_loads(data.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return None
        buildid = state.get('buildid')
        if isinstance(buildid, str) and buildid.isascii() and buildid.isdigit():
            return int(buildid)
        return None
    finally:
        os.close(handle)


def _newer(build, other):
    """Whether the manifest with build number build goes where it is meant to instead of the one with other
    (LIB-07): the larger number, the way the tidy work compares builds; a readable number beats one that cannot be
    read, and two that are equal or cannot be read are not newer."""
    if build is None:
        return False
    if other is None:
        return True
    return build > other


def _put_back(stash_fd, real_fd, parts, layout, side, owner, moved):
    for name in sorted(os.listdir(stash_fd)):
        info = _stat_at(stash_fd, name)
        if info is None:
            continue
        there = _stat_at(real_fd, name)
        if stat.S_ISDIR(info.st_mode):
            if there is not None and not stat.S_ISDIR(there.st_mode):
                set_aside(layout, side, 'stash', parts, stash_fd, name)
                continue
            inner = os.open(name, DIR_FLAGS, dir_fd=stash_fd)
            try:
                target = _dir_below(real_fd, [name], create=True, owner=owner)
                try:
                    _put_back(inner, target, (*parts, name), layout, side, owner, moved)
                finally:
                    os.close(target)
            finally:
                os.close(inner)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=stash_fd)
        elif there is not None:
            if parts == () and _newer(_buildid_below(stash_fd, name), _buildid_below(real_fd, name)):
                # The stashed manifest is the newer one: it goes in, and the library's one is kept in the account's
                # conflicts (LIB-07).
                set_aside(layout, side, 'stash', parts, real_fd, name)
                os.rename(name, name, src_dir_fd=stash_fd, dst_dir_fd=real_fd)
                moved.append('/'.join((*parts, name)))
            else:
                set_aside(layout, side, 'stash', parts, stash_fd, name)
        else:
            os.rename(name, name, src_dir_fd=stash_fd, dst_dir_fd=real_fd)
            moved.append('/'.join((*parts, name)))


def restore_manifests(layout, side, entry, *, proc=Path('/proc')):
    """Put back what stash_manifests moved. A name that exists in the library again (stock Steam made it) is kept in
    the account's conflicts folder instead. The account's Steam must be closed. Returns the entries put back."""
    stash = side.stash / 'steamapps'
    if not os.path.lexists(stash):
        with contextlib.suppress(OSError):
            os.unlink(side.stash / STASH_JOURNAL)
        return []
    if side.real_library is None:
        raise StoreError(f"{side.name}'s library is not known; its stashed manifests stay where they are")
    if steam_running(entry.pw_uid, proc):
        raise StoreError(f"{side.name}'s Steam is running; its manifests can only be put back while it is closed")
    plain_folder(side.real_steamapps, entry.pw_uid)
    moved = []
    _stash_state(layout, side, 'restoring', held_apps(layout))
    stash_fd = os.open(stash, DIR_FLAGS)
    try:
        real_fd = os.open(side.real_steamapps, DIR_FLAGS)
        try:
            _put_back(stash_fd, real_fd, (), layout, side, (entry.pw_uid, entry.pw_gid), moved)
        finally:
            os.close(real_fd)
    finally:
        os.close(stash_fd)
    with contextlib.suppress(OSError):
        os.rmdir(stash)
    with contextlib.suppress(OSError):
        os.unlink(side.stash / STASH_JOURNAL)
    with contextlib.suppress(OSError):
        os.rmdir(side.stash)
    return sorted(moved)


def stash_state(side):
    """What the account's stash holds, for status: {'journal': phase or None, 'entries': n}."""
    phase = None
    try:
        phase = json.loads((side.stash / STASH_JOURNAL).read_text(encoding='utf-8')).get('phase')
    except (OSError, ValueError, AttributeError):
        pass
    count = 0
    try:
        top = os.open(side.stash / 'steamapps', DIR_FLAGS)
    except OSError:
        return {'journal': phase, 'entries': 0}
    try:
        def walk(fd):
            total = 0
            for name in os.listdir(fd):
                info = _stat_at(fd, name)
                if info is not None and stat.S_ISDIR(info.st_mode):
                    child = os.open(name, DIR_FLAGS, dir_fd=fd)
                    try:
                        total += walk(child)
                    finally:
                        os.close(child)
                else:
                    total += 1
            return total
        count = walk(top)
    finally:
        os.close(top)
    return {'journal': phase, 'entries': count}


def stash_names(side, limit=20):
    """The entries the account's stash holds, as names relative to steamapps (at most limit, sorted)."""
    found = []
    try:
        top = os.open(side.stash / 'steamapps', DIR_FLAGS)
    except OSError:
        return found

    def walk(fd, prefix):
        for name in sorted(os.listdir(fd)):
            if len(found) >= limit:
                return
            info = _stat_at(fd, name)
            if info is None:
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                try:
                    walk(child, prefix + name + '/')
                finally:
                    os.close(child)
            else:
                found.append(prefix + name)
    try:
        walk(top, '')
    finally:
        os.close(top)
    return found


def steam_running(uid, proc=Path('/proc')):
    """Whether a process called steam runs as this user (the client, in a side or in the stock session)."""
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            if item.stat().st_uid == uid and (item / 'comm').read_text().strip() == 'steam':
                return True
        except OSError:
            continue
    return False


def edited_list(text, library, add, vdf):
    """The library list with the folder added or removed; None when nothing changes.

    Steam numbers its folders "0", "1", ... and fills in a new entry that has only a path (label, content id and
    apps) when it starts. Removing an entry renumbers the ones after it, in their order.
    """
    name, folders = vdf.vdf_loads(text)
    if name.lower() != 'libraryfolders':
        raise StoreError('not a Steam library list')
    entries = [(key, value) for key, value in folders.items() if isinstance(value, dict)]
    others = [(key, value) for key, value in folders.items() if not isinstance(value, dict)]
    if any(not key.isdigit() for key, _value in entries):
        raise StoreError('the library list has a folder that is not numbered')
    present = [value for _key, value in entries if value.get('path') == str(library)]
    if add:
        if present:
            return None
        entries.append((None, {'path': str(library)}))
    else:
        if not present:
            return None
        entries = [(key, value) for key, value in entries if value.get('path') != str(library)]
    rebuilt = dict(others)
    for number, (_key, value) in enumerate(entries):
        rebuilt[str(number)] = value
    return vdf.vdf_dumps(name, rebuilt)


def as_user(entry, work):
    """Run work() in a child with the user's identity and nothing of root's; return what it returned (JSON)."""
    if os.geteuid() != 0:
        return work()   # tests, and a user changing their own list
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        status = 1
        try:
            os.close(reader)
            os.setgroups([])
            os.setresgid(entry.pw_gid, entry.pw_gid, entry.pw_gid)
            os.setresuid(entry.pw_uid, entry.pw_uid, entry.pw_uid)
            try:
                reply = {'result': work()}
            except Exception as error:      # every failure comes back with its reason, never as a bare exit
                reply = {'error': str(error) or type(error).__name__}
            os.write(writer, json.dumps(reply).encode())
            status = 0
        finally:
            os._exit(status)
    os.close(writer)
    with os.fdopen(reader, 'rb') as stream:
        data = stream.read()
    _pid, status = os.waitpid(child, 0)
    if status != 0 or not data:
        raise StoreError('the step run as the user failed')
    reply = json.loads(data)
    if 'error' in reply:
        raise StoreError(reply['error'])
    return reply['result']


def change_lists(steam_root, library, add, vdf):
    """Add or remove the folder in every copy of the list that exists; return the copies that changed."""
    changed = []
    for relative in LIBRARY_LISTS:
        path = Path(steam_root) / relative
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise StoreError(f'{path} is not a library list')
            text = stream.read()
        new = edited_list(text, library, add, vdf)
        if new is None:
            continue
        handle, temporary = tempfile.mkstemp(prefix='.libraryfolders-', dir=path.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                stream.write(new)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(info.st_mode))
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        changed.append(relative)
    return changed


def steam_root_of(entry):
    return Path(entry.pw_dir) / '.local/share/Steam'


def register(layout, name, add=True, *, proc=Path('/proc'), path=None):
    """Put a library folder (default: the account's own library on this store's disk) on the account's Steam library
    list, or take it off. Returns the copies changed. Only while the account's Steam is closed."""
    entry = account(name)
    library = path if path is not None else layout.side(name).library
    if library is None:
        raise StoreError(f'{name} has no Steam library on this disk')
    if steam_running(entry.pw_uid, proc):
        raise StoreError(f"{name}'s Steam is running; its library list can only be changed while it is closed")
    return change_library(entry, library, add)


def registered(layout, name, path=None):
    """The copies of the account's list that name the library folder (default: its own library on this disk)."""
    entry = account(name)
    library = path if path is not None else layout.side(name).library
    if library is None:
        return []
    vdf = depot_module()

    def look():
        found = []
        for relative in LIBRARY_LISTS:
            try:
                _name, folders = vdf.read_vdf(steam_root_of(entry) / relative)
            except (OSError, ValueError):
                continue
            if any(isinstance(value, dict) and value.get('path') == str(library) for value in folders.values()):
                found.append(relative)
        return found
    return as_user(entry, look)


CONFIG_LIMIT = 64 * 1024
# How Steam names the tools it installs next to the games.
STEAM_TOOLS = ('Steam Linux Runtime', 'Proton ', 'Steamworks Common Redistributables')


def read_config(layout):
    """The store's config, or None when there is none. Anything that is not exactly what `on` writes is refused."""
    try:
        descriptor = os.open(layout.config, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StoreError(f'{layout.config} cannot be read: {error}') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_LIMIT:
            raise StoreError(f'{layout.config} is not a regular file of at most 64 KiB')
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise StoreError(f'{layout.config} must belong to root and not be writable by others')
        data = os.read(descriptor, CONFIG_LIMIT + 1)
    finally:
        os.close(descriptor)
    try:
        config = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise StoreError(f'{layout.config} is not valid JSON: {error}') from None
    if not isinstance(config, dict) or set(config) != {'version', 'mode', 'users'}:
        raise StoreError(f'{layout.config} must hold exactly version, mode and users')
    if type(config['version']) is not int or config['version'] != 1:
        raise StoreError(f'{layout.config} has an unknown version')
    if config['mode'] not in ('on', 'leaving'):
        raise StoreError(f'{layout.config} has an unknown mode')
    users = config['users']
    if (not isinstance(users, list) or not 1 <= len(users) <= 4 or
            any(not isinstance(name, str) or not USER_NAME.fullmatch(name) for name in users) or
            len(set(users)) != len(users)):
        raise StoreError(f'{layout.config} must name one to four distinct valid users')
    return config


def write_config(layout, config):
    """Write the config atomically: a temporary file next to it, synced, then renamed. Mode 0644."""
    handle, temporary = tempfile.mkstemp(prefix='.config-', dir=layout.root)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'mode': config['mode'], 'users': list(config['users'])}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, layout.config)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def permanent(layout):
    """Whether the libraries are permanent: a config exists. A broken config raises.

    That holds in mode leaving too: until a slot has given every game back, the libraries stay mounted and on the
    Steam lists, and nothing but the slot may put them away.
    """
    return read_config(layout) is not None


def turn_on(layout, users):
    """Make the store permanent for these accounts. The same again changes nothing; a different one is refused."""
    users = list(users)
    if not users:
        raise StoreError('on needs at least one user')
    for name in users:
        account(name)
    prepare_store(layout)
    with locked(layout):
        current = read_config(layout)
        if current is not None:
            if current['mode'] == 'on' and current['users'] == users:
                return current
            raise StoreError(f'the store is configured already: mode {current["mode"]}, '
                             f'users {", ".join(current["users"])}')
        config = {'version': 1, 'mode': 'on', 'users': users}
        write_config(layout, config)
    return config


def turn_off(layout, *, mountinfo='/proc/self/mountinfo'):
    """Set the mode to leaving; the next slot gives the games back and removes the config. The disks follow."""
    with locked(layout):
        current = read_config(layout)
        if current is None:
            raise StoreError('the store is not on')
        current['mode'] = 'leaving'
        write_config(layout, current)
    present, _absent = layouts(layout, mountinfo=mountinfo)
    for disk in present[1:]:
        with locked(disk):
            write_config(disk, current)
    return current


def _valid_disks(disks, what):
    """The list of disks as disks.json holds it, or a StoreError that says what is wrong with it."""
    if not isinstance(disks, list) or len(disks) > MAX_DISKS:
        raise StoreError(f'{what} must hold a list of at most {MAX_DISKS} disks')
    roots = set()
    for item in disks:
        if not isinstance(item, dict) or set(item) != {'root', 'mount'}:
            raise StoreError(f'{what} has a disk that is not exactly a root and a mount point')
        root, mount = item['root'], item['mount']
        if (not isinstance(root, str) or not isinstance(mount, str) or '\0' in root or '\0' in mount or
                not mount.startswith('/') or '..' in Path(mount).parts or str(Path(mount)) != mount):
            raise StoreError(f'{what} has a disk with a path that is not a plain absolute path')
        if root != str(Path(mount) / WRAPPER / DISK_ROOT):
            raise StoreError(f'{what} has a disk whose root is not {WRAPPER}/{DISK_ROOT} below its mount point')
        if root in roots:
            raise StoreError(f'{what} lists {root} twice')
        roots.add(root)
    return [{'root': item['root'], 'mount': item['mount']} for item in disks]


def read_disks(primary, *, owners=None):
    """The disks the primary store lists; none when there is no file. Anything but exactly what write_disks makes is refused.

    owners is who may own the file (default: the process; a step run as an account also accepts root).
    """
    path = primary.disks
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise StoreError(f'{path} cannot be read: {error}') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_LIMIT:
            raise StoreError(f'{path} is not a regular file of at most 64 KiB')
        if info.st_uid not in (owners or (os.geteuid(),)) or info.st_mode & 0o022:
            raise StoreError(f'{path} must belong to root and not be writable by others')
        data = os.read(descriptor, CONFIG_LIMIT + 1)
    finally:
        os.close(descriptor)
    try:
        content = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as error:
        raise StoreError(f'{path} is not valid JSON: {error}') from None
    if not isinstance(content, dict) or set(content) != {'version', 'disks'}:
        raise StoreError(f'{path} must hold exactly version and disks')
    if type(content['version']) is not int or content['version'] != 1:
        raise StoreError(f'{path} has an unknown version')
    return _valid_disks(content['disks'], str(path))


def write_disks(primary, disks):
    """Write the list atomically: a temporary file next to it, synced, then renamed. Mode 0644."""
    disks = _valid_disks(list(disks), 'the list of disks')
    handle, temporary = tempfile.mkstemp(prefix='.disks-', dir=primary.root)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'disks': disks}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, primary.disks)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def device_of(path):
    """The device number of the filesystem a path is on. Tests replace this function."""
    return os.stat(path).st_dev


def add_disk(primary, mount, *, mountinfo='/proc/self/mountinfo'):
    """Give the filesystem mounted at mount a store of its own and list it. Doing it again changes nothing.

    The top folder of such a filesystem belongs to an ordinary user, who could rename the store away and put
    something else there while root works below it. So the store root is made inside a wrapper folder of root's and
    the wrapper is then made immutable: an immutable folder cannot be renamed or removed, even by the owner of its
    parent. Returns {'root', 'mount', 'added': bool}.
    """
    mount = Path(mount)
    if not mount.is_absolute() or '..' in mount.parts or str(mount) != os.path.normpath(mount):
        raise StoreError(f'{mount} is not a plain absolute path')
    if any(character in str(mount) for character in ',:\\'):
        raise StoreError(f'{mount} cannot hold a store: it has a comma, a colon or a backslash')
    config = read_config(primary)
    if config is None or config['mode'] != 'on':
        raise StoreError('the shared game library is not on; turn it on first')
    points = mount_points(mountinfo)
    if str(mount) not in points:
        raise StoreError(f'{mount} is not a mount point')
    kind, options, super_options = points[str(mount)]
    if kind not in DISK_FILESYSTEMS:
        raise StoreError(f'{mount} is a {kind} filesystem; a store needs one of {", ".join(DISK_FILESYSTEMS)}')
    if 'ro' in options.split(',') or 'ro' in super_options.split(','):
        raise StoreError(f'{mount} is mounted read-only')
    if device_of(mount) == device_of(primary.root):
        raise StoreError(f'{mount} is on the same filesystem as the primary store; it needs no store of its own')
    layout = Layout.for_disk(primary, mount)
    wrapper = layout.wrapper
    try:
        os.mkdir(wrapper, 0o755)
    except FileExistsError:
        pass
    except OSError as error:
        raise StoreError(f'{wrapper} cannot be made: {error.strerror}') from None
    try:
        descriptor = os.open(wrapper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise StoreError(f'{wrapper} is not a real folder: {error.strerror}') from None
    try:
        info = os.fstat(descriptor)
        if info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise StoreError(f'{wrapper} is there already and is not a folder of root that others cannot write')
        if stat.S_IMODE(info.st_mode) != 0o755 and os.geteuid() == 0:
            os.fchmod(descriptor, 0o755)
        try:
            os.mkdir(DISK_ROOT, 0o755, dir_fd=descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise StoreError(f'{layout.root} cannot be made: {error.strerror}') from None
        inner = os.open(DISK_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
        try:
            if os.geteuid() == 0:
                os.fchown(inner, 0, 0)
                os.fchmod(inner, 0o755)
        finally:
            os.close(inner)
        if not get_immutable(descriptor):
            set_immutable(descriptor, True)
        if not get_immutable(descriptor):
            raise StoreError(f'{wrapper} did not take the immutable flag')
        after = os.lstat(wrapper)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise StoreError(f'{wrapper} was replaced while it was made immutable')
    finally:
        os.close(descriptor)
    prepare_store(layout)
    with locked(primary):
        disks = read_disks(primary)
        with locked(layout):
            try:
                current = read_config(layout)
            except StoreError:
                current = None
            if current != {'version': 1, 'mode': config['mode'], 'users': config['users']}:
                write_config(layout, config)
        entry = {'root': str(layout.root), 'mount': str(mount)}
        if entry in disks:
            return dict(entry, added=False)
        if len(disks) >= MAX_DISKS:
            raise StoreError(f'at most {MAX_DISKS} disks can have a store')
        write_disks(primary, disks + [entry])
    return dict(entry, added=True)


def listed_layouts(primary):
    """The primary and the store of every listed disk, connected or not."""
    return [primary] + [Layout.for_disk(primary, disk['mount']) for disk in read_disks(primary)]


def layouts(primary, *, mountinfo='/proc/self/mountinfo'):
    """(present, absent): the primary first, then each listed disk that is mounted and whole; the rest with a reason."""
    disks = read_disks(primary)
    present, absent = [primary], []
    if not disks:
        return present, absent
    points = mount_points(mountinfo)
    for disk in disks:
        layout = Layout.for_disk(primary, disk['mount'])
        point = points.get(disk['mount'])
        reason = None
        if point is None:
            reason = f'{disk["mount"]} is not mounted'
        elif 'ro' in point[1].split(',') or 'ro' in point[2].split(','):
            reason = f'{disk["mount"]} is mounted read-only'
        else:
            try:
                check_wrapper(layout)
                if not stat.S_ISDIR(os.lstat(layout.root).st_mode):
                    raise StoreError(f'{layout.root} is not a folder')
            except (StoreError, OSError) as error:
                reason = str(error)
        if reason is None:
            present.append(layout)
        else:
            absent.append({'root': disk['root'], 'mount': disk['mount'], 'reason': reason})
    return present, absent


def mount_all(primary, name, *, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc')):
    """Merge the stores into the account's libraries: one overlay on the steamapps folder of its library on the
    primary's disk and on every connected disk that has a store. Returns (paths, problems).

    paths are the folders the overlays are mounted on, the primary's first. A store that cannot be mounted is skipped
    and said. A disk that is not connected is skipped: the library the account has there stays on its Steam list like
    any library of a drive that is gone.
    """
    problems = []
    try:
        present, _absent = layouts(primary, mountinfo=mountinfo)
    except StoreError as error:
        problems.append(f'the list of disks is unusable: {error}')
        present = [primary]
    paths = []
    for layout in present:
        label = '' if layout.primary is None else f'disk {layout.mount_point}: '
        try:
            mount(layout, name, runner=runner, mountinfo=mountinfo, proc=proc)
            paths.append(str(layout.side(name).steamapps))
        except (StoreError, OSError) as error:
            problems.append(f'{label}{error}')
    return paths, problems


def forget(primary, root, *, mountinfo='/proc/self/mountinfo', proc=Path('/proc')):
    """Take a disk that is not connected off the list for good, and the library DosPlex gave an account there off its
    Steam list. What is on the disk is not touched."""
    root = str(root)
    entry = next((disk for disk in read_disks(primary) if disk['root'] == root), None)
    if entry is None:
        raise StoreError(f'{root} is not a listed disk')
    present, _absent = layouts(primary, mountinfo=mountinfo)
    if any(str(layout.root) == root for layout in present[1:]):
        raise StoreError(f'{root} is connected; turn the shared game library off instead')
    config = read_config(primary)
    users = config['users'] if config else []
    layout = Layout.for_disk(primary, entry['mount'])
    for name in users:
        register(layout, name, False, proc=proc, path=layout.provisioned(name)[0])
    with locked(primary):
        write_disks(primary, [disk for disk in read_disks(primary) if disk['root'] != root])
    return {'forgot': root, 'users': users}


# Shutdown, a card that arrives, a card that leaves. Stock Steam must never find an app manifest for a shared game whose
# files it cannot see (it would offer to download the game again), and a card must be free of DosPlex's mounts before
# it is ejected. The pieces are the ones the slot uses (mount, unmount, the stash), run at other moments:
#
#   release  at shutdown (the boot unit's ExecStop): every account's overlays on every disk come off and the manifests
#            of stored games are stashed, as an orderly unmount does. A Steam that still runs keeps its manifests where
#            they are (the stash is made later); the next slot or arrival puts things right.
#   arrive   when a disk appears or goes (a udev rule starts it): what is left of a disk that is gone is detached
#            (lazy), and every listed disk that is mounted gets its overlays, with a running Steam too (mounting over a
#            folder adds nothing to it); what changes the real folder waits for that Steam to close.
#   eject    the DosPlex-side eject of a card: release the card, then ask udisks to unmount it.
#
# One of them, and the slot, at a time (cycling). Each result is kept in events.json for status.

EVENTS_NAME = 'events.json'
DEVICE_NAME = re.compile(r'[A-Za-z0-9._-]{1,32}')
UDISKS_UNMOUNT = ('udisksctl', 'unmount', '--no-user-interaction', '--block-device')


def keep_event(primary, kind, report, now=None):
    """Keep what the last release or arrival did (root, 0644), next to the one of the other kind. Never fatal."""
    if not primary.root.is_dir():
        return
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    kept = last_events(primary)
    kept[kind] = {'utc': stamp.strftime(UTC_FORMAT), 'report': report}
    try:
        handle, temporary = tempfile.mkstemp(prefix='.events-', dir=primary.root)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, **kept}, stream)
                stream.write('\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, primary.root / EVENTS_NAME)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    except (OSError, TypeError, ValueError):
        pass


def last_events(primary):
    """What keep_event kept: {'release': {'utc', 'report'}, 'arrive': {...}}; a kind that never ran is missing."""
    try:
        descriptor = os.open(primary.root / EVENTS_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            return {}
        data = json.loads(os.read(descriptor, 4 * 1024 * 1024 + 1).decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    finally:
        os.close(descriptor)
    found = {}
    for kind in ('release', 'arrive'):
        item = data.get(kind) if isinstance(data, dict) else None
        if isinstance(item, dict) and isinstance(item.get('report'), dict) and isinstance(item.get('utc'), str):
            found[kind] = {'utc': item['utc'], 'report': item['report']}
    return found


def device_gone(source):
    """Whether the block device a mount came from has no node any more (the disk was pulled). Tests replace this."""
    return source.startswith('/dev/') and not os.path.exists(source)


def filesystem_of(device):
    """The filesystem type on /dev/<device> read from the device itself, or None. Tests replace this function."""
    try:
        done = subprocess.run(['blkid', '-p', '-o', 'value', '-s', 'TYPE', f'/dev/{device}'], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def _listed(primary, mountinfo, gone):
    """(present, absent) as layouts says, with a disk whose device node is gone counted as absent (a pulled card's mount
    stays in the table until somebody unmounts it, and its filesystem answers with errors)."""
    present, absent = layouts(primary, mountinfo=mountinfo)
    sources = mount_sources(mountinfo)
    kept = present[:1]
    for layout in present[1:]:
        source = sources.get(str(layout.mount_point), '')
        if gone(source):
            absent.append({'root': str(layout.root), 'mount': str(layout.mount_point), 'reason': f'{source} is gone'})
        else:
            kept.append(layout)
    return kept, absent


def _arrive_account(layout, name, *, runner, mountinfo, proc):
    """The overlay of one account on one disk, mounted with its Steam running or not. With no overlay (it could not be
    made) the stored games' manifests must not stay in the real library: they are stashed, which needs the account's Steam
    closed; if it runs, that is said and the next slot does it."""
    result = {}
    try:
        result['done'] = mount(layout, name, runner=runner, mountinfo=mountinfo, proc=proc, while_running=True)
        if steam_running(account(name).pw_uid, proc):
            result['steam_running'] = True
    except (StoreError, OSError) as error:
        result['error'] = str(error)
        try:
            result['stashed'] = stash_account(layout, name, proc=proc)
        except (StoreError, OSError) as stash_error:
            result['stash_waits'] = str(stash_error)
    return result


def arrive(primary, *, device=None, wait=0, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc'),
           sleep=time.sleep, clock=time.monotonic, gone=None, probe=None):
    """A disk appeared or went (device is the kernel name from udev, such as sdb1). Before anything else a disk that is
    gone has what is still mounted of it detached; then every listed disk that is mounted gets its overlays for every
    account (a store's disk is listed in disks.json; any other disk is none of ours). With wait, a device that holds a
    filesystem is given that long to be mounted first: SteamOS mounts a card a moment after udev saw it.

    Mounting over a folder adds nothing to it, so it is done with an account's Steam running as well; putting the
    stashed manifests back is not, and waits for the next time that Steam is closed (the result says so). A Steam that
    runs may only see the shared games after it looks at the library again; that is not shown here."""
    config = read_config(primary)
    if config is None:
        return {'config': None}
    users = config['users']
    report = {'device': device}
    if device is not None:
        if not DEVICE_NAME.fullmatch(device):
            raise StoreError(f'{device!r} is not a device name')
        if wait > 0 and (probe or filesystem_of)(device) in DISK_FILESYSTEMS:
            deadline = clock() + wait
            while f'/dev/{device}' not in mount_sources(mountinfo).values():
                if clock() >= deadline:
                    report['not_mounted'] = f'/dev/{device} was not mounted after {wait} seconds'
                    break
                sleep(1)
    with cycling(primary):
        try:
            present, absent = _listed(primary, mountinfo, gone or device_gone)
        except (StoreError, OSError) as error:
            report['disks_error'] = f'the list of disks is unusable: {error}'
            keep_event(primary, 'arrive', report)
            return report
        disks = {}
        for item in absent:
            disks[item['root']] = _slot_absent(primary, item, users, proc, runner, mountinfo)
        for layout in present[1:]:
            disks[str(layout.root)] = {name: _arrive_account(layout, name, runner=runner, mountinfo=mountinfo, proc=proc)
                                       for name in users}
        report['disks'] = disks
    keep_event(primary, 'arrive', report)
    return report


def _release_account(layout, name, *, runner, mountinfo, proc):
    """Take one account's overlays (and the binds that pin the disk) off one store, then stash its manifests of stored
    games. An overlay that is in use stays mounted and is an error. The stash needs the account's Steam closed: when it
    runs the manifests stay and the result says so."""
    result = {}
    try:
        result['unmounted'] = unmount(layout, name, runner=runner, mountinfo=mountinfo, layer=True, stash=False, proc=proc)
    except (StoreError, OSError) as error:
        result['error'] = str(error)
        return result
    try:
        result['stashed'] = stash_account(layout, name, proc=proc)
    except (StoreError, OSError) as error:
        result['stash_waits'] = str(error)
    return result


def release(primary, *, wait=0, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc'),
            sleep=time.sleep, clock=time.monotonic, gone=None):
    """Shutdown: unmount every account's overlays on every disk and stash, the same as an orderly unmount. With wait,
    an account's Steam that still runs is given that many seconds to end first. Doing it again changes nothing."""
    config = read_config(primary)
    if config is None:
        return {'config': None}
    users = config['users']
    if wait > 0:
        deadline = clock() + wait
        while any(steam_running(account(name).pw_uid, proc) for name in users) and clock() < deadline:
            sleep(1)
    report = {'stores': {}}
    with cycling(primary):
        try:
            present, absent = _listed(primary, mountinfo, gone or device_gone)
        except (StoreError, OSError) as error:
            report['disks_error'] = f'the list of disks is unusable: {error}'
            present, absent = [primary], []
        if absent:
            report['absent'] = {item['root']: _slot_absent(primary, item, users, proc, runner, mountinfo)
                                for item in absent}
        for layout in present[1:] + present[:1]:
            report['stores'][str(layout.root)] = {
                name: _release_account(layout, name, runner=runner, mountinfo=mountinfo, proc=proc) for name in users}
    keep_event(primary, 'release', report)
    return report


def eject(primary, mount_text, *, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc')):
    """The DosPlex-side eject of a card: release it (every account's overlays and binds off, the stash made for an
    account whose Steam is closed), then have udisks unmount it, as Steam's own eject would. If anything of the card is
    still in use (a game from it runs) or udisks refuses, what was released is mounted again and that is an error: the
    card stays as it was. Returns what was done."""
    config = read_config(primary)
    if config is None:
        raise StoreError('the shared game library is not on')
    users = config['users']
    mount_text = str(mount_text)
    if not mount_text.startswith('/') or '\0' in mount_text or '..' in Path(mount_text).parts:
        raise StoreError(f'{mount_text!r} is not a plain absolute path')
    place = os.path.normpath(mount_text)
    if not any(disk['mount'] == place for disk in read_disks(primary)):
        raise StoreError(f'no disk with a shared game store is listed at {place}')
    layout = Layout.for_disk(primary, place)
    with cycling(primary):
        device = mount_sources(mountinfo).get(place)
        if device is None:
            return {'ejected': True, 'already': True, 'mount': place}
        if not device.startswith('/dev/'):
            raise StoreError(f'{place} is not the mount of a block device ({device})')
        released = {name: _release_account(layout, name, runner=runner, mountinfo=mountinfo, proc=proc) for name in users}
        errors = [f'{name}: {item["error"]}' for name, item in released.items() if 'error' in item]
        if not errors:
            held = sorted(point for point, source in mount_sources(mountinfo).items() if source == device and point != place)
            if held:
                errors.append('still mounted elsewhere: ' + ', '.join(held))
        if not errors:
            try:
                run([*UDISKS_UNMOUNT, device], runner)
            except FileNotFoundError:
                try:
                    run(['umount', place], runner)
                except StoreError as error:
                    errors.append(str(error))
            except StoreError as error:
                errors.append(str(error))
        if errors:
            for name in users:
                if released[name].get('unmounted'):
                    _arrive_account(layout, name, runner=runner, mountinfo=mountinfo, proc=proc)
            raise StoreError(f'{place} was not ejected, its shared games stay mounted: ' + '; '.join(errors))
    keep_event(primary, 'release', {'ejected': place, 'released': released})
    return {'ejected': True, 'mount': place, 'device': device, 'released': released}


def online(routes='/proc/net/route'):
    """Whether the device has a default route now: the cheap sign that a Steam started next can reach its servers."""
    try:
        with open(routes) as stream:
            for line in stream.readlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == '00000000' and int(fields[3], 16) & 3 == 3:
                    return True
    except (OSError, ValueError):
        pass
    return False


def prepare_sides(layout, users, *, provisioning=True, runner=subprocess.run, mountinfo='/proc/self/mountinfo',
                  proc=Path('/proc')):
    """The store's folders and every account's folders of root's, made if they are missing; on a disk store an
    account that has no library there is given one (provisioning). Returns {user: reason} for an account that has no
    library on this disk (it is left out of what follows)."""
    prepare_store(layout)
    problems = {}
    with locked(layout):
        for name in users:
            try:
                entry = account(name)
                side = resolve_side(layout, name, entry, provisioning=provisioning, runner=runner, mountinfo=mountinfo,
                                    proc=proc)
                prepare_side(layout, side, entry)
            except (StoreError, OSError) as error:
                problems[name] = str(error)
    return problems


CYCLE_LOCK = '.cycle.lock'


@contextlib.contextmanager
def cycling(primary):
    """One of slot, release, arrive and eject at a time. A card that arrives must not be mounted into while the slot has
    the overlays unmounted for a commit, and a shutdown must not unmount under an arrival. Taken before any store's own
    lock, never inside one."""
    descriptor = os.open(primary.root / CYCLE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def slot(primary, **options):
    """The slot of every store, one at a time with release, arrive and eject (cycling). See _slot."""
    if read_config(primary) is None:
        return {'config': None}
    with cycling(primary):
        return _slot(primary, **options)


def _slot(primary, *, wait=0, tidy=None, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc'),
          sleep=time.sleep, clock=time.monotonic, routes='/proc/net/route'):
    """The one entry for "no Steam of the store's accounts runs now". Returns a report; raises only before it did anything.

    Every store has its slot (_slot_layout): each connected disk first, then the primary. The primary is the authority:
    a disk's config is rewritten to its mode and users first. A disk that is not connected has what is still mounted of
    it detached and is reported (the libraries of the accounts on it stay on their lists). Leaving: a disk that gave everything back is cleaned up and
    unlisted; the primary leaves only when no disk stays listed. With wait, a listed disk gets that many seconds to be
    mounted first (at boot SteamOS mounts drives after the system has started). The report is the primary's, with the
    other stores' under 'disks'.
    """
    config = read_config(primary)
    if config is None:
        return {'config': None}
    users = config['users']
    for name in users:
        if steam_running(account(name).pw_uid, proc):
            raise StoreError(f"{name}'s Steam is running; the slot needs every account's Steam closed")
    stopped = stop_sharing(primary)
    if wait > 0:
        deadline = clock() + wait
        while True:
            points = mount_points(mountinfo)
            if all(disk['mount'] in points for disk in read_disks(primary)) or clock() >= deadline:
                break
            sleep(1)
    present, absent = layouts(primary, mountinfo=mountinfo)
    disks = {}
    for layout in present[1:]:
        disks[str(layout.root)] = _slot_disk(primary, layout, config, tidy, runner, mountinfo, proc)
    for item in absent:
        disks[item['root']] = _slot_absent(primary, item, users, proc, runner, mountinfo)
    stays = any(not report.get('left') for report in disks.values())
    report = _slot_layout(primary, config, tidy=tidy, runner=runner, mountinfo=mountinfo, proc=proc, may_leave=not stays)
    if config['mode'] == 'on':
        # No Steam runs now: the moment to ask each account's Steam for the file lists it dropped (tidy module).
        try:
            report['lists'] = (tidy or tidy_module()).ask_for_lists(primary, users, online=online(routes))
        except Exception as error:
            report['lists_error'] = str(error)
    if report.get('left') is False and stays and config['mode'] == 'leaving':
        report['disks_remain'] = [root for root, other in disks.items() if not other.get('left')]
    report['disks'] = disks
    if stopped:
        report['stopped_sharing'] = True
    keep_slot_report(primary, report)
    return report


SLOT_NAME = 'slot.json'


def keep_slot_report(primary, report, now=None):
    """Keep what the last slot did (root, 0644), so that status can say why a game was not taken. A start of DosPlex
    runs the slot where nobody reads its answer. A store that left has no place for it; a failure is not one."""
    if not primary.root.is_dir() or report.get('left') is True:
        return
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    try:
        handle, temporary = tempfile.mkstemp(prefix='.slot-', dir=primary.root)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'utc': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'), 'report': report}, stream)
                stream.write('\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, primary.root / SLOT_NAME)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
    except (OSError, TypeError, ValueError):
        pass


def last_slot(primary):
    """What keep_slot_report kept: {'utc', 'report'}, or None."""
    try:
        descriptor = os.open(primary.root / SLOT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            return None
        kept = json.loads(os.read(descriptor, 4 * 1024 * 1024 + 1).decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    finally:
        os.close(descriptor)
    if not isinstance(kept, dict) or not isinstance(kept.get('report'), dict) or not isinstance(kept.get('utc'), str):
        return None
    return {'utc': kept['utc'], 'report': kept['report']}


def _slot_absent(primary, item, users, proc, runner=subprocess.run, mountinfo='/proc/self/mountinfo'):
    """A listed disk that is not connected (it was pulled out): what is still mounted of it is detached. The library an
    account has on it stays on its Steam list, as any library of a drive that is gone does."""
    report = {'absent': item['reason']}
    layout = Layout.for_disk(primary, item['mount'])
    for name in users:
        side = layout.side(name)
        paths = [Path(point) for point in overlay_mounts(side, mountinfo)] + [side.reach, side.view, side.retired.library]
        if side.retired.bound is not None:
            paths.append(side.retired.bound)
        if side.provisioned is not None:
            paths.append(side.provisioned[2][1])
        for path in paths:
            if is_mounted(path, mountinfo):
                gone = runner(['umount', '-l', str(path)], capture_output=True, text=True, timeout=60)
                if gone.returncode != 0:
                    report.setdefault('failed', {})[name] = f'{path} stays mounted: {(gone.stderr or gone.stdout).strip()}'
    return report


def _slot_disk(primary, layout, config, tidy, runner, mountinfo, proc):
    """The slot of one connected disk, with the primary's mode and users; a disk that left is unlisted and freed."""
    try:
        with locked(layout):
            try:
                current = read_config(layout)
            except StoreError:
                current = None
            if current != {'version': 1, 'mode': config['mode'], 'users': config['users']}:
                write_config(layout, config)
    except (StoreError, OSError) as error:
        return {'config_error': str(error), 'left': False}
    report = _slot_layout(layout, config, tidy=tidy, runner=runner, mountinfo=mountinfo, proc=proc, may_leave=True)
    if report.get('left') is True:
        try:
            for name in config['users']:
                unmount(layout, name, runner=runner, mountinfo=mountinfo, layer=True, stash=False, proc=proc)
                _retire_provision(layout, name, report, proc)
            descriptor = os.open(layout.wrapper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                set_immutable(descriptor, False)
            finally:
                os.close(descriptor)
            with locked(primary):
                write_disks(primary, [disk for disk in read_disks(primary) if disk['root'] != str(layout.root)])
            report['unlisted'] = True
        except (StoreError, OSError) as error:
            report['left'] = False
            report['unlist_error'] = str(error)
    return report


def _retire_provision(layout, name, report, proc):
    """A disk that is left: the library DosPlex gave the account there goes off its Steam list (its folder and what it
    holds stay on the disk, and are named in the report when it holds games)."""
    given = layout.provisioned(name)
    if given is None:
        return
    register(layout, name, False, proc=proc, path=given[0])
    try:
        held = [item for item in os.listdir(given[1] / 'steamapps') if item.startswith('appmanifest_')]
    except OSError:
        held = []
    if held:
        report.setdefault('kept_libraries', {})[name] = str(given[1])


def _slot_layout(layout, config, *, tidy=None, runner=subprocess.run, mountinfo='/proc/self/mountinfo', proc=Path('/proc'),
                 may_leave=True):
    """The slot of one store.

    Mode on: unmount, move the private layers of the earlier layout into the libraries (migrate), commit what the share
    records hold, mount again. Mode leaving: unmount, migrate, give the games back, remove the config; when a game
    could not be given back (or may_leave is false: a disk of the primary stays) the config stays and the overlays come
    back so that the game stays playable. mount and unmount take the store lock themselves, as do the tidy functions, so
    it is not held here. No manifests are stashed here: the commit needs them, and no stock Steam runs between the
    unmount and the mount; what a failed mount leaves unmounted is stashed.
    """
    users, leaving = config['users'], config['mode'] == 'leaving'
    report = {'config': config}
    busy = []
    for name in users:
        try:
            unmount(layout, name, runner=runner, mountinfo=mountinfo, stash=False, proc=proc)
        except (StoreError, OSError) as error:
            busy.append(name)
            report.setdefault('busy_errors', {})[name] = str(error)
    if not busy:
        # The tidy work puts files into every account's real library, so the folders must be there, and an account that
        # has no library on a disk is given one (not when the store is leaving).
        try:
            problems = prepare_sides(layout, users, provisioning=not leaving, runner=runner, mountinfo=mountinfo, proc=proc)
        except (StoreError, OSError) as error:
            report['prepare_error'] = str(error)
            busy = list(users)
        else:
            if problems:
                report['no_library'] = problems
    if busy:
        report.setdefault('busy', busy)
    else:
        tidy = tidy or tidy_module()
        # After a clean shutdown the manifests of stored games are in the stash, and the commit reads the account's own
        # manifest of a game it updates: it drops the record when the manifest is not there. They come back first (the
        # mount does it again, finding nothing to do). Leaving does it itself, in restore_all.
        for name in () if leaving else users:
            try:
                restore_account(layout, name, proc=proc)
            except (StoreError, OSError) as error:
                report.setdefault('stash_errors', {})[name] = str(error)
        try:
            report['migrate'] = tidy.migrate(layout, users, proc=proc)
        except Exception as error:
            report['migrate_error'] = str(error)
        if leaving:
            try:
                report['restore'] = tidy.restore_all(layout, users)
            except Exception as error:
                report['restore_error'] = str(error)
            restore = report.get('restore')
            if isinstance(restore, dict) and not restore.get('kept') and 'restore_error' not in report and may_leave \
                    and not report.get('migrate_error') and not _unfinished(report.get('migrate')):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(layout.config)
                report['left'] = True
                return report
        else:
            try:
                report['commit'] = tidy.commit(layout, users)
            except Exception as error:
                report['commit_error'] = str(error)
    if leaving:
        report['left'] = False
    for name in users:
        try:
            mount(layout, name, runner=runner, mountinfo=mountinfo, proc=proc)
        except (StoreError, OSError) as error:
            report.setdefault('failed', {})[name] = str(error)
            # Stock Steam must not find manifests of games whose files it cannot see.
            with contextlib.suppress(StoreError, OSError):
                unmount(layout, name, runner=runner, mountinfo=mountinfo, stash=True, proc=proc)
    return report


def _unfinished(migrate):
    """Whether a migration report says an account's earlier layout is not emptied yet."""
    return isinstance(migrate, dict) and any(isinstance(item, dict) and item.get('error') for item in migrate.values())


def stored_apps(layout):
    """appid -> name, install folder and build of every game the store holds, from its manifests."""
    vdf = depot_module()
    apps = {}
    try:
        names = sorted(os.listdir(layout.steamapps))
    except OSError:
        return apps
    for name in names:
        match = re.fullmatch(r'appmanifest_(\d+)\.acf', name)
        if not match:
            continue
        try:
            _section, state = vdf.read_vdf(layout.steamapps / name)
        except (OSError, ValueError) as error:
            apps[match.group(1)] = {'error': str(error)}
            continue
        apps[match.group(1)] = {key: state.get(key, '') for key in ('name', 'installdir', 'buildid', 'SizeOnDisk')}
    return apps


def _layout_status(layout, names, tidy, mountinfo='/proc/self/mountinfo'):
    """What one store holds: its games, the share records waiting and each account's library on this disk."""
    report = {'apps': stored_apps(layout), 'sides': {}}
    try:
        tidy = tidy or tidy_module()
        report['pending'] = tidy.pending(layout)
        shared = tidy.stored(layout)
        for appid, entry in report['apps'].items():
            entry['shared_by'] = shared.get(appid, {}).get('shared_by')
    except (OSError, ImportError):
        report['tidy'] = 'missing'
    resolve_libraries(layout, names)
    for name in names:
        side = layout.side(name)
        report['sides'][name] = {
            'library': None if side.library is None else str(side.library),
            'mounted': bool(overlay_mounts(side, mountinfo)), 'registered': registered(layout, name),
            'steam_running': steam_running(account(name).pw_uid),
            'stash': stash_state(side), 'stashed': stash_names(side), 'conflicts': conflicts_of(side),
            'earlier_layer': migration_pending(layout, side)}
    return report


def status(layout, names, *, tidy=None, mountinfo='/proc/self/mountinfo'):
    config = read_config(layout)
    if not names and config is not None:
        names = config['users']
    report = {'root': str(layout.root), 'exists': layout.store.is_dir(), 'config': config}
    report.update(_layout_status(layout, names, tidy, mountinfo))
    present, absent = layouts(layout, mountinfo=mountinfo)
    report['last_slot'] = last_slot(layout)
    report['events'] = last_events(layout)
    report['unshared'] = unshared(layout, names, present, tidy) if config is not None and config['mode'] == 'on' else {}
    report['disks'] = []
    for disk in present[1:]:
        item = {'root': str(disk.root), 'mount': str(disk.mount_point), 'present': True, 'reason': None}
        item.update(_layout_status(disk, names, tidy, mountinfo))
        report['disks'].append(item)
    for item in absent:
        report['disks'].append({'root': item['root'], 'mount': item['mount'], 'present': False, 'reason': item['reason'],
                                'apps': {}, 'pending': [], 'sides': {}})
    return report


def unshared(primary, names, present, tidy=None):
    """{user: [{'appid', 'name', 'reason'}]}: the installed games of each account that are not in a store and cannot
    be shared as they are, with the reason. Steam's tools and what is shared already are left out. Never fatal."""
    found = {}
    try:
        tidy = tidy or tidy_module()
        config = read_config(primary)
        users = config['users'] if config else []
        for name in names:
            if name not in users:
                continue
            games = {}
            asked = tidy.asked_for_lists(primary) if hasattr(tidy, 'asked_for_lists') else set()
            for place in present:
                held = stored_apps(place)       # a stored game the account has still to validate is shared
                for item in tidy.shareable(place, name, users):
                    reason = item.get('reason')
                    if reason and f"{name}/{item['appid']}" in asked:
                        reason = 'waits for its file list from Steam (asked at the last start; Steam fetches it when it starts)'
                    if (reason and not reason.startswith('is already shared') and str(item['appid']) not in held
                            and not str(item.get('name', '')).startswith(STEAM_TOOLS)):
                        games[str(item['appid'])] = {'appid': str(item['appid']), 'name': item.get('name', ''), 'reason': reason}
            found[name] = [games[appid] for appid in sorted(games, key=int)]
    except Exception as error:
        return {'error': str(error)}
    return found


def _mount_of(path, device, points):
    """The longest mount point that holds the path and is on that device; None when there is none."""
    path = str(path)
    for point in sorted(points, key=len, reverse=True):
        if (path == point or path.startswith(point.rstrip('/') + '/')) and device_of_point(point) == device:
            return point
    return None


def device_of_point(point):
    try:
        return device_of(point)
    except OSError:
        return None


def _find_disks(primary, name, users, tidy, mountinfo, summary):
    """Give a store to every filesystem that holds an own Steam library of the account and has none. Refusals are noted."""
    present, _absent = layouts(primary, mountinfo=mountinfo)
    known = {device_of(layout.root) for layout in present}
    points = mount_points(mountinfo)
    roots = mount_roots(mountinfo)
    tried = {}
    for library in tidy.account_libraries(primary, name, users):
        if library['dev'] in known:
            continue
        if library['dev'] in tried:
            continue
        point = _mount_of(library['path'], library['dev'], points)
        if point is not None and f'/{WRAPPER}/{DISK_ROOT}/{ACCOUNTS}/' in roots.get(point, '') + '/':
            continue        # a library DosPlex gave the account on a disk: its bind is not the disk's mount point
        try:
            if point is None:
                raise StoreError(f'no mount point of {library["path"]} was found')
            tried[library['dev']] = add_disk(primary, point, mountinfo=mountinfo)
        except (StoreError, OSError) as error:
            tried[library['dev']] = None
            summary['disks_refused'].append({'library': library['path'], 'reason': str(error)})


def share(layout, name, apps, every, *, tidy=None, progress=None, mountinfo='/proc/self/mountinfo'):
    """Record games of the account for the next slot. Returns the summary: what was shared and what was refused.

    A game goes to the store of the disk it is on. Before the games of the account are listed, a disk that holds
    one of its own libraries and has no store yet is given one (add_disk); one that cannot be is named under
    disks_refused and the other games are shared all the same.
    """
    config = read_config(layout)
    if config is None or name not in config['users']:
        raise StoreError(f'{name} is not an account of the store; turn it on first')
    users = config['users']
    tidy = tidy or tidy_module()
    summary = {'user': name, 'shared': [], 'refused': [], 'disks_refused': []}
    routes = []
    if every:
        # --all means games. Steam's own tools (runtimes, Proton) are apps too, but every account needs them at all
        # times; they are shared only when they are named.
        _find_disks(layout, name, users, tidy, mountinfo, summary)
        summary['skipped_tools'] = []
        for place in layouts(layout, mountinfo=mountinfo)[0]:
            listing = [entry for entry in tidy.shareable(place, name, users) if not entry.get('reason')]
            games = [entry['appid'] for entry in listing if not str(entry.get('name', '')).startswith(STEAM_TOOLS)]
            routes += [(place, appid) for appid in games]
            summary['skipped_tools'] += [entry['appid'] for entry in listing if entry['appid'] not in games]
    else:
        places = layouts(layout, mountinfo=mountinfo)[0]
        found = {appid: _where(places, tidy, name, users, appid) for appid in apps}
        if any(place is None for place in found.values()):
            _find_disks(layout, name, users, tidy, mountinfo, summary)
            places = layouts(layout, mountinfo=mountinfo)[0]
            found = {appid: found[appid] or _where(places, tidy, name, users, appid) for appid in apps}
        for appid in apps:
            if found[appid] is None:
                summary['refused'].append({'appid': appid, 'reason': f'game {appid} is not installed for this account'})
            else:
                routes.append((found[appid], appid))
    for place, appid in routes:
        if progress:
            progress(f'sharing {appid}')
        try:
            result = tidy.share(place, name, appid, users, progress=_per_gibibyte(progress))
            summary['shared'].append(result if place is layout else dict(result, disk=str(place.root)))
        except tidy.TidyError as error:
            summary['refused'].append({'appid': appid, 'reason': str(error)})
    return summary


def _where(places, tidy, name, users, appid):
    """The first store whose listing of the account's games has this app; None when none has."""
    for place in places:
        if any(entry['appid'] == appid for entry in tidy.shareable(place, name, users)):
            return place
    return None


# Automatic sharing: the games of the accounts are checked in the background and queued like a named share.

AUTO_LIMIT = 1024 * 1024
AUTO_AGE = 24 * 3600
UTC_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


def _seconds(text):
    """Seconds since the epoch of a stamp this module wrote, or None."""
    try:
        return datetime.datetime.strptime(text, UTC_FORMAT).replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _manifest_identity(layout, name, entry):
    """[size, mtime_ns] of the account's app manifest of that game, from lstat, or None when it cannot be told."""
    try:
        info = os.lstat(Path(entry['library']) / 'steamapps' / f"appmanifest_{entry['appid']}.acf")
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def read_auto(layout):
    """The memory of refused games: {"user/appid": {...}}. Anything unreadable or malformed is an empty memory."""
    try:
        descriptor = os.open(layout.root / 'auto.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > AUTO_LIMIT:
            return {}
        memory = json.loads(os.read(descriptor, AUTO_LIMIT + 1).decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    finally:
        os.close(descriptor)
    if not isinstance(memory, dict) or memory.get('version') != 1 or not isinstance(memory.get('refused'), dict):
        return {}
    for key, entry in memory['refused'].items():
        identity = entry.get('manifest') if isinstance(entry, dict) else None
        if (not isinstance(identity, list) or len(identity) != 2 or any(type(number) is not int for number in identity)
                or not isinstance(entry.get('reason'), str) or _seconds(entry.get('utc')) is None):
            return {}
    return memory['refused']


def write_auto(layout, refused):
    """Write the memory atomically: a temporary file next to it, synced, then renamed. Mode 0600."""
    handle, temporary = tempfile.mkstemp(prefix='.auto-', dir=layout.root)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'refused': refused}, stream, indent=1)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, layout.root / 'auto.json')
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def share_new(layout, names=None, *, tidy=None, progress=None, now=None, mountinfo='/proc/self/mountinfo'):
    """Check and queue the games of the accounts that are new or changed. Returns {'users': {name: summary}}.

    Every store is looked at: the primary and each connected disk, and a disk that holds a library of an account and
    has no store yet gets one first. Steam's own tools, games that wait for a slot already and games refused lately
    (and unchanged since) are left out, so a refused game is not read again every few minutes. A store without a
    config, or one that is leaving, shares nothing."""
    config = read_config(layout)
    if config is None or config['mode'] != 'on':
        return {'users': {}}
    names = list(names) if names else list(config['users'])
    for name in names:
        if name not in config['users']:
            raise StoreError(f'{name} is not an account of the store')
    users = config['users']
    tidy = tidy or tidy_module()
    now = time.time() if now is None else now
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(UTC_FORMAT)
    memory = read_auto(layout)
    before = json.dumps(memory, sort_keys=True)
    result = {'users': {}}
    waiting = {}
    for name in names:
        summary = result['users'][name] = {'shared': [], 'refused': [], 'skipped': 0, 'disks_refused': []}
        try:
            _find_disks(layout, name, users, tidy, mountinfo, summary)
            places = layouts(layout, mountinfo=mountinfo)[0]
        except (StoreError, OSError) as error:
            summary['disks_refused'].append({'library': '', 'reason': str(error)})
            places = [layout]
        listed = set()
        for place in places:
            if str(place.root) not in waiting:
                waiting[str(place.root)] = {str(item['appid']) for item in tidy.pending(place)}
            queued = waiting[str(place.root)]
            listing = tidy.shareable(place, name, users)
            listed |= {f"{name}/{item['appid']}" for item in listing}
            for item in listing:
                appid, key = str(item['appid']), f"{name}/{item['appid']}"
                if item.get('reason') or str(item.get('name', '')).startswith(STEAM_TOOLS) or appid in queued:
                    continue
                identity = _manifest_identity(place, name, item)
                known = memory.get(key)
                if known and identity is not None and known['manifest'] == identity and \
                        0 <= now - _seconds(known['utc']) < AUTO_AGE:
                    summary['skipped'] += 1
                    continue
                memory.pop(key, None)
                if progress:
                    progress(f'sharing {appid}')
                try:
                    shared = tidy.share(place, name, appid, users, progress=_per_gibibyte(progress))
                    summary['shared'].append(shared if place is layout else dict(shared, disk=str(place.root)))
                    queued.add(appid)
                except tidy.TidyError as error:
                    summary['refused'].append({'appid': appid, 'name': item.get('name', ''), 'reason': str(error)})
                    if identity is not None:
                        memory[key] = {'manifest': identity, 'reason': str(error), 'utc': stamp}
                except (OSError, ValueError) as error:
                    # Not a refusal of the game: tried again at the next run.
                    summary['refused'].append({'appid': appid, 'name': item.get('name', ''), 'reason': str(error)})
        for key in [key for key in memory if key.startswith(name + '/') and key not in listed]:
            del memory[key]
    if json.dumps(memory, sort_keys=True) != before:
        write_auto(layout, memory)
    return result


@contextlib.contextmanager
def sharing(layout, kind='named'):
    """One check of games for sharing at a time, for the whole run. The process id goes into the lock file, and
    what kind of check it is: `new` for the background one, `named` for one somebody asked for."""
    descriptor = os.open(layout.root / '.share-lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoreError('games are being checked for sharing already') from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f'{os.getpid()} {kind}\n'.encode())
        yield
    finally:
        os.close(descriptor)


def _signal_group(pid, number):
    """Signal the process group of pid; the process alone when the group cannot be signalled."""
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        pass
    except OSError:
        os.kill(pid, number)


def stop_sharing(layout, *, patience=10, signaller=None, only=None):
    """Stop a running check of games so that a slot can go first. Returns whether something was stopped.
    With only='new' just the background check is stopped (a share somebody asked for goes before it; seen on the
    Steam Machine, p60: the first command after `library on` was refused because the timer had fired).

    The check runs in a process group of its own (its slow hash is a forked child that holds the lock too), so
    the whole group is asked to end, and killed when it does not."""
    signaller = signaller or _signal_group
    try:
        descriptor = os.open(layout.root / '.share-lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        def free():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return True

        def wait(seconds):
            end = time.monotonic() + seconds
            while not free():
                if time.monotonic() >= end:
                    return False
                time.sleep(0.05)
            return True

        if free():
            return False
        words = os.pread(descriptor, 64, 0).split()
        try:
            pid = int(words[0])
        except (IndexError, ValueError):
            pid = 0
        if only is not None and (len(words) < 2 or words[1].decode('ascii', 'replace') != only):
            return False
        if pid <= 1 or pid == os.getpid():
            raise StoreError('a check of games for sharing could not be stopped')
        for number, seconds in ((signal.SIGTERM, patience), (signal.SIGKILL, 5)):
            try:
                signaller(pid, number)
            except OSError:
                pass
            if wait(seconds):
                return True
        raise StoreError('a check of games for sharing could not be stopped')
    finally:
        os.close(descriptor)


def _per_gibibyte(say):
    """The tidy module reports every file it hashes (name, size); pass on one line per GiB read."""
    if say is None:
        return None
    seen = [0, 0]

    def per_file(_name, size):
        seen[0] += size
        if seen[0] >> 30 > seen[1]:
            seen[1] = seen[0] >> 30
            say(f'  {seen[1]} GiB checked')
    return per_file


def _share_command(layout, arguments):
    """The share verb, run while the share lock is held. Returns the exit status."""
    if arguments.new:
        if arguments.app or arguments.all:
            raise StoreError('share --new takes no --app and no --all')
        print(json.dumps(share_new(layout, arguments.user, progress=lambda line: print(line, file=sys.stderr))))
        return 0
    if len(arguments.user) != 1:
        raise StoreError('share takes exactly one --user')
    if bool(arguments.app) == arguments.all:
        raise StoreError('share needs --app ID (repeated) or --all')
    if not all(item.isdigit() for item in arguments.app):
        raise StoreError('--app takes a numeric app id')
    summary = share(layout, arguments.user[0], arguments.app, arguments.all,
                    progress=lambda line: print(line, file=sys.stderr))
    print(json.dumps(summary, indent=2))
    return 0 if summary['shared'] or not summary['refused'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=('mount', 'unmount', 'register', 'unregister', 'status',
                                            'on', 'off', 'slot', 'share', 'forget', 'release', 'arrive', 'eject'))
    parser.add_argument('--user', action='append', default=[])
    parser.add_argument('--app', action='append', default=[])
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--wait', type=int, default=0, help='slot: seconds to wait for the listed disks to be mounted; '
                        'arrive: for the device to be mounted; release: for a Steam to end')
    parser.add_argument('--device', default=None, help='arrive: the kernel name of the device that appeared or went')
    parser.add_argument('--mount', default=None, help='eject: where the disk is mounted')
    parser.add_argument('--disk', default=None, help='forget: the store root of the disk (from status)')
    parser.add_argument('--new', action='store_true', help='share: check new or changed games of the accounts')
    parser.add_argument('--root', default=None, help='the store (default: DOSPLEX_GAME_STORE or ' + DEFAULT_ROOT + ')')
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise StoreError('must run as root')
        layout = Layout(arguments.root)
        if arguments.command == 'status':
            print(json.dumps(status(layout, arguments.user), indent=2))
            return 0
        if arguments.command == 'on':
            print(json.dumps(turn_on(layout, arguments.user)))
            return 0
        if arguments.command == 'off':
            print(json.dumps(turn_off(layout)))
            return 0
        if arguments.command == 'slot':
            if not 0 <= arguments.wait <= 600:
                raise StoreError('--wait takes 0 to 600 seconds')
            print(json.dumps(slot(layout, wait=arguments.wait)))
            return 0
        if arguments.command in ('release', 'arrive', 'eject'):
            if not 0 <= arguments.wait <= 600:
                raise StoreError('--wait takes 0 to 600 seconds')
            if arguments.command == 'release':
                print(json.dumps(release(layout, wait=arguments.wait)))
            elif arguments.command == 'arrive':
                print(json.dumps(arrive(layout, device=arguments.device, wait=arguments.wait)))
            else:
                if not arguments.mount:
                    raise StoreError('eject needs --mount PATH')
                print(json.dumps(eject(layout, arguments.mount)))
            return 0
        if arguments.command == 'forget':
            if not arguments.disk:
                raise StoreError('forget needs --disk ROOT')
            print(json.dumps(forget(layout, arguments.disk)))
            return 0
        if arguments.command == 'share':
            with contextlib.suppress(OSError):
                os.setpgrp()
            if not arguments.new:
                stop_sharing(layout, only='new')
            with sharing(layout, 'new' if arguments.new else 'named'):
                return _share_command(layout, arguments)
        if len(arguments.user) != 1:
            raise StoreError(f'{arguments.command} takes exactly one --user')
        name = arguments.user[0]
        if arguments.command != 'mount':
            resolve_libraries(layout, [name])
        if arguments.command == 'mount':
            done = mount(layout, name)
        elif arguments.command == 'unmount':
            done = unmount(layout, name)
        else:
            done = register(layout, name, arguments.command == 'register')
        print(json.dumps({arguments.command: done, 'user': name, 'library': str(layout.side(name).library)}))
        return 0
    except (StoreError, OSError, ValueError) as error:
        print(f'dosplex-game-store: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
