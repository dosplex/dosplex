#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Deliver a proven Steam-created evdev node into its private trial namespace.

Development helper for Steam and its existing nested game runtimes. Replacing
device mounts does not guarantee that a game reopens them. Keyboard/mouse output
and overlay capture are separate paths. Private mounts disappear with their
session namespaces; Steam owns the lifetime of its generated devices.

Steam's 64-bit client runs as a chain of two processes in two mount namespaces (steam_family). The node goes into each
namespace of the chain, and into a game's namespace when one is named.
"""
import argparse
from contextlib import ExitStack
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import pwd
import select
import stat
import subprocess
import sys

LIBC = ctypes.CDLL(None, use_errno=True)


def syscall(number, *args):
    value = LIBC.syscall(number, *args)
    if value < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return value


def process_identity(pid, unit, uid, steam=True):
    root = Path('/proc')/str(pid)
    if root.stat().st_uid != uid or (steam and (root/'comm').read_text().strip() != 'steam'):
        raise RuntimeError('not the assigned Steam process')
    if (root/'cgroup').read_text().strip() != '0::/system.slice/'+unit:
        raise RuntimeError('Steam process outside exact side unit')
    start = (root/'stat').read_text().rsplit(')',1)[1].split()[19]
    namespace = os.readlink(root/'ns/mnt')
    if namespace == os.readlink('/proc/self/ns/mnt'):
        raise RuntimeError('refusing host mount namespace')
    return (start, namespace)


PROC = Path('/proc')
SYS = Path('/sys')


def parent_pid(pid):
    """The parent of a process: field 4 of /proc/<pid>/stat, parsed as process_identity parses the start time."""
    return int((PROC/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[1])


def unit_main_pid(unit):
    """The main process of a side unit as systemd reports it; 0 when the unit has none (or systemctl cannot say)."""
    try:
        said = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '--value'], text=True, timeout=3)
        return int(said.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def unit_tree(unit):
    """The pids of the unit's main process and everything below it, by parent links. Empty when the unit has no main process.

    A process the account started that is not below the main process (reparented to init, or in the cgroup by another way)
    is not in it. The side's Steam is looked for here and nowhere else.
    """
    main = unit_main_pid(unit)
    if main < 1:
        return set()
    children = {}
    for path in PROC.iterdir():
        if path.name.isdecimal():
            try:
                children.setdefault(parent_pid(int(path.name)), []).append(int(path.name))
            except (OSError, ValueError, IndexError):
                pass
    tree, todo = {main}, [main]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in tree:
                tree.add(child)
                todo.append(child)
    return tree


def steam_processes(unit, uid):
    """Every process of the unit's main process tree that process_identity accepts as the Steam of this side: [(pid, identity)] by pid.

    Only the tree of the unit's MainPID is looked at (unit_tree): a process of the account that is named steam and is not
    in it is not the side's Steam and neither picks the namespace root enters nor stops delivery. More than one is normal
    for Steam's 64-bit client (steamrt64): it runs as two processes named steam. steam_chain decides whether what was found
    is one client.
    """
    found = []
    for pid in unit_tree(unit):
        try:
            found.append((pid, process_identity(pid, unit, uid)))
        except (OSError, RuntimeError):
            pass
    return sorted(found)


def steam_chain(pids, parent_of=None):
    """The Steam processes of one side unit as one chain, outermost first.

    One process is a chain by itself. The 64-bit client runs as two: a launcher and, below it and below
    pressure-vessel's helpers, the client proper that opens the devices. Several processes are one client only when each
    is a descendant of the one before it (parents are read with parent_of, default parent_pid). Two that are unrelated, or
    two below the same process, are refused with what was found. The outermost is the process to enter and to read the
    environment of; the innermost, the one with no Steam below it, is the client. game-scenario-probe.py carries a copy
    of this rule (it loads nothing else), and its tests run both over the same cases.
    """
    parent_of = parent_of or parent_pid
    pids = sorted(set(pids))
    if not pids:
        raise RuntimeError('expected one Steam process or one chain of them, found none')
    if len(pids) == 1:
        return pids
    above = {}
    for pid in pids:
        chain, walk = [], pid
        while len(chain) < 4096:
            try:
                walk = parent_of(walk)
            except (OSError, ValueError, IndexError):
                break
            if walk < 1 or walk in chain:
                break
            chain.append(walk)
            if walk == 1:
                break
        above[pid] = chain
    ordered = sorted(pids, key=lambda pid: sum(other in above[pid] for other in pids))
    if all(outer in above[inner] for outer, inner in zip(ordered, ordered[1:])):
        return ordered
    raise RuntimeError('expected one Steam process or one chain of them (each below the one before), found '
                       + ', '.join(f"{pid} (parent {above[pid][0] if above[pid] else 'unknown'})" for pid in pids))


def steam_family(unit, uid):
    """The live Steam of a side unit: [(pid, identity)], outermost first. Raises unless it is one process or one chain."""
    found = dict(steam_processes(unit, uid))
    return [(pid, found[pid]) for pid in steam_chain(found, parent_pid)]


def mount_namespaces(family):
    """(pid, identity) of the first process of each mount namespace of a Steam family, outermost first.

    A mount is made in one mount namespace only, and Steam's 64-bit client runs its launcher and its client in two (the HID
    delivery serves them one by one, native-hid-delivery.py namespaces). A family of one process, or of several that share a
    namespace, has one.
    """
    seen, found = set(), []
    for pid, identity in family:
        if identity[1] not in seen:
            seen.add(identity[1])
            found.append((pid, identity))
    return found


def steam_changed(family, unit, uid):
    """Whether any process of a Steam family is no longer the one steam_family found (another start time or namespace)."""
    return any(process_identity(pid, unit, uid) != identity for pid, identity in family)


INPUT_NODE = r'/dev/input/(event|js)[0-9]+'
# The nodes of a generic game pad (native-pads.py): its evdev and js nodes and its hidraw node, mounted at their own paths.
PAD_NODE = r'/dev/(input/(event|js)|hidraw)[0-9]+'


def attach(fd, node, uid, gid, pattern=INPUT_NODE):
    if not re.fullmatch(pattern, node):
        raise ValueError('unexpected mountpoint')
    previous_gid = LIBC.setfsgid(gid)
    previous_uid = LIBC.setfsuid(uid)
    try:
        target = os.open(node, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(target)
    finally:
        LIBC.setfsuid(previous_uid); LIBC.setfsgid(previous_gid)
    syscall(429, fd, b'', -100, os.fsencode(node), 4)


def replace_deleted_mount(source, old, node, uid, gid, pattern=INPUT_NODE):
    """Replace a proven broker mount whose devtmpfs inode was removed on unplug."""
    if not re.fullmatch(pattern, node):
        raise ValueError('unexpected replacement path')
    pinned = os.fstat(old)
    current = os.stat(node, follow_symlinks=False)
    if (not stat.S_ISCHR(pinned.st_mode) or pinned.st_nlink != 0
            or device_identity(current) != device_identity(pinned)):
        raise RuntimeError('replacement target is not the pinned deleted device')
    # move_mount onto the unlinked devtmpfs dentry returns ENOENT. Detach only
    # this verified mount, exposing the private placeholder made by attach().
    syscall(166, os.fsencode(node), 2)  # umount2(MNT_DETACH), x86_64
    try:
        target = os.open(node, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        attach(source, node, uid, gid, pattern)
        return
    try:
        meta = os.fstat(target)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != uid or meta.st_nlink != 1:
            raise RuntimeError('unexpected controller placeholder after detach')
        syscall(429, source, b'', target, b'', 4 | 0x40)
    finally:
        os.close(target)


PLACEHOLDER_WAIT = 10  # seconds a helper or its parent waits for the other; covers the watcher's pass over several games


def detach_deleted_mount(old, node, pattern=INPUT_NODE):
    """Detach, in this mount namespace, the mount at node if it is the pinned deleted device; nothing else.

    A dentry that is still a mountpoint in another namespace cannot be unlinked (EBUSY), so replacing a deleted node starts
    by detaching its stale mount in every namespace that has one. The checks are those of replace_deleted_mount.
    """
    if not re.fullmatch(pattern, node):
        raise ValueError('unexpected replacement path')
    pinned = os.fstat(old)
    current = os.stat(node, follow_symlinks=False)
    if (not stat.S_ISCHR(pinned.st_mode) or pinned.st_nlink != 0
            or device_identity(current) != device_identity(pinned)):
        raise RuntimeError('detach target is not the pinned deleted device')
    syscall(166, os.fsencode(node), 2)  # umount2(MNT_DETACH), x86_64


def wait_for_release(fd, timeout):
    """Whether a byte (or the end of the pipe) came on fd within timeout seconds."""
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return False
    os.read(fd, 1)
    return True


def replace_placeholder(source, node, uid, gid, wait_fd, timeout=PLACEHOLDER_WAIT, out=None):
    """In the outermost namespace: unlink the placeholder, make it again, mount source onto it, and change its mode last.

    The unlink and the create are an IN_DELETE and an IN_CREATE on the directory every namespace of the side shares and a
    game's inotify watch sits on (a bind mount or a covering tmpfs makes none). The file is made as attach() makes it:
    host root is unmapped in the side's user namespace, so a create as root fails with EOVERFLOW. Once the mount is made this
    says "ready" on out and waits for a byte on wait_fd (at most timeout seconds) while the caller serves the other
    namespaces and the game; then fchmod on the descriptor made before the mount gives IN_ATTRIB with the entry name, which
    SDL takes as "try this path again". A parent that is gone or stuck costs only the timeout.
    """
    out = out or sys.stdout
    if not re.fullmatch(INPUT_NODE, node):
        raise ValueError('unexpected replacement path')
    target = os.open(node, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        meta = os.fstat(target)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != uid or meta.st_nlink != 1:
            raise RuntimeError('unexpected controller placeholder before replacement')
    finally:
        os.close(target)
    os.unlink(node)
    previous_gid = LIBC.setfsgid(gid)
    previous_uid = LIBC.setfsuid(uid)
    try:
        fd = os.open(node, os.O_CREAT | os.O_EXCL | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    finally:
        LIBC.setfsuid(previous_uid); LIBC.setfsgid(previous_gid)
    # The descriptor stays the underlying file after the mount covers it: the fchmod below reaches the tmpfs inode.
    syscall(429, source, b'', fd, b'', 4 | 0x40)
    out.write('ready\n')
    out.flush()
    wait_for_release(wait_fd, timeout)  # on a timeout the mode is changed all the same
    previous_gid = LIBC.setfsgid(gid)
    previous_uid = LIBC.setfsuid(uid)
    try:
        os.fchmod(fd, 0o600)
    finally:
        LIBC.setfsuid(previous_uid); LIBC.setfsgid(previous_gid)


class PlaceholderReplacement:
    """The helper left waiting in the outermost namespace: release() lets it change the mode and end, abort() just closes."""
    def __init__(self, process, write_fd, pid, timeout=PLACEHOLDER_WAIT):
        self.process, self.write_fd, self.pid, self.timeout = process, write_fd, pid, timeout
        self.done = False

    def release(self):
        if self.done:
            return
        self.done = True
        try:
            os.write(self.write_fd, b'x')
        finally:
            os.close(self.write_fd)
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError(f'placeholder helper in process {self.pid} timed out')
        if self.process.returncode:
            raise RuntimeError(f'placeholder helper in process {self.pid} failed (exit status {self.process.returncode})')

    def abort(self):
        if self.done:
            return
        self.done = True
        os.close(self.write_fd)  # the helper sees the end of the pipe, changes the mode and ends by itself
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def start_placeholder_replacement(namespace, clone, node, uid, gid, keep, popen=subprocess.Popen):
    """Run replace_placeholder in the namespace of the first process of the family and wait until its mount is made."""
    pid, identity, mount_fd, root_fd = namespace
    read_fd, write_fd = os.pipe()
    try:
        process = popen([sys.executable, str(Path(__file__).resolve()), '--namespace', str(mount_fd), str(root_fd),
                         '--replace-placeholder', str(clone), node, str(uid), str(gid), str(read_fd)],
                        pass_fds=(mount_fd, root_fd, clone, read_fd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True)
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    handle = PlaceholderReplacement(process, write_fd, pid)
    ready, _, _ = select.select([process.stdout], [], [], PLACEHOLDER_WAIT)
    line = process.stdout.readline() if ready else None
    if line != 'ready\n':
        process.kill()
        _, stderr = process.communicate()
        handle.done = True
        os.close(write_fd)
        reason = 'timed out' if line is None else helper_failure(
            subprocess.CalledProcessError(process.returncode, 'helper', stderr=stderr))
        raise RuntimeError(f'native output delivery failed in {identity[1]} (process {pid}): ' + reason)
    return handle


def device_identity(meta):
    return [meta.st_dev, meta.st_ino, meta.st_rdev]


def replacement_allowed(record, process, node, current):
    """Only replace the exact mount this helper previously delivered."""
    return (isinstance(record, dict) and record.get('process') == list(process)
            and record.get('node') == node and stat.S_ISCHR(current.st_mode)
            and record.get('identity') == device_identity(current))


def owned_placeholder(meta, uid):
    """Whether meta is the placeholder attach() makes before a mount: the account's empty, private, regular file."""
    return (stat.S_ISREG(meta.st_mode) and meta.st_uid == uid and stat.S_IMODE(meta.st_mode) == 0o600
            and meta.st_size == 0 and meta.st_nlink == 1)


def save_delivery_ledger(path, ledger):
    temporary = path.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(ledger, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def output_owner(transaction, side, session):
    """Authorize only explicitly enrolled account outputs, never arbitrary UIDs."""
    if side not in ('left', 'right') or transaction.get('phase') != 'started':
        raise RuntimeError('side is not active')
    owners = transaction.get('steam_input_owners')
    if owners is None:
        plan = transaction['native_deck_plan']
        owners = {plan['side']: plan['uid']}
    if not isinstance(owners, dict):
        raise ValueError('invalid Steam Input owners')
    uid = owners.get(side)
    if type(uid) is not int or uid < 1000:
        raise RuntimeError('side is not enrolled for Steam Input')
    if pwd.getpwnam(session['owner']).pw_uid != uid:
        raise RuntimeError('Steam Input owner differs from side account')
    return uid


def replacement_arguments(metadata, source_fd, target_fd, node, uid, gid):
    if metadata.st_nlink == 0:
        return ['--replace-deleted',source_fd,target_fd,node,uid,gid]
    return ['--attach-existing',source_fd,target_fd]


def mount_in_namespace(mount_fd, root_fd, arguments, device_fds):
    """Load Python on the host before entering a runtime without Python."""
    subprocess.run([sys.executable, str(Path(__file__).resolve()),
        '--namespace', str(mount_fd), str(root_fd), *map(str, arguments)],
        pass_fds=(mount_fd, root_fd, *device_fds), check=True, timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def helper_failure(error):
    """What a failed namespace helper said: the last line of its stderr, or how it ended."""
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timed out'
    text = error.stderr if isinstance(error.stderr, str) else (error.stderr or b'').decode(errors='replace')
    lines = text.strip().splitlines()
    return lines[-1] if lines else f'exit status {error.returncode}'


def mount_steam_side(pid, identity, mount_fd, root_fd, arguments, device_fds):
    """mount_in_namespace in the namespace of one process of Steam's family; a failure names that namespace and process."""
    try:
        mount_in_namespace(mount_fd, root_fd, arguments, device_fds)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'native output delivery failed in {identity[1]} (process {pid}): '
                           + helper_failure(error)) from error


def input_view_snapshot(directory, authorized, require_live, allow_empty=False, skip_deleted=False):
    """A replacement view may contain only proven outputs of this Steam side."""
    snapshot={}
    for entry in directory.iterdir():
        meta=entry.lstat()
        if (not re.fullmatch(r'(event|js)[0-9]+',entry.name)
                or not stat.S_ISCHR(meta.st_mode)
                or device_identity(meta) not in authorized):
            raise RuntimeError('input view contains an unproven or stale device')
        if meta.st_nlink == 0:
            if skip_deleted:continue
            if require_live:raise RuntimeError('input view contains an unproven or stale device')
        snapshot[entry.name]=device_identity(meta)
    if not snapshot and not allow_empty:raise RuntimeError('empty controller view')
    return snapshot


def fresh_input_view(devices, keep):
    """Build a detached view from live pinned nodes, without stale submounts.

    A recursive clone of Steam's existing tree can fail EINVAL after deleted
    controller mounts have accumulated underneath newer replacements.
    """
    fs=keep(syscall(430,b'tmpfs',1))  # fsopen(FSOPEN_CLOEXEC)
    syscall(431,fs,1,b'mode',b'0755',0)  # FSCONFIG_SET_STRING
    syscall(431,fs,6,0,0,0)  # FSCONFIG_CMD_CREATE
    tree=keep(syscall(432,fs,1,0))  # fsmount(FSMOUNT_CLOEXEC)
    for name,identity in devices.items():
        if not re.fullmatch('(event|js)[0-9]+',name):raise ValueError('invalid input view entry')
        source=keep(os.open('/dev/input/'+name,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
        meta=os.fstat(source)
        if not stat.S_ISCHR(meta.st_mode) or meta.st_nlink==0 or device_identity(meta)!=identity:
            raise RuntimeError('input source changed while rebuilding view')
        clone=keep(syscall(428,source,b'',0x1000|1|os.O_CLOEXEC))
        placeholder=os.open(name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600,dir_fd=tree)
        os.close(placeholder)
        syscall(429,clone,b'',tree,os.fsencode(name),4)
    return tree


def replace_consumer_input_view(family, consumer_pid, unit, uid, consumer_identity, ledger):
    """Overmount a locked inherited subtree using only this account's outputs.

    Nested user namespaces can lock inherited deleted device mounts against
    individual unmount. The directory itself remains a valid mount target.
    The view is taken from the first process of the Steam family, the namespace this
    was proven in when Steam was one process (a guess that the client's would list
    the same: deliver has served both); every process of the family is checked again
    before the mount.
    """
    pid=family[0][0]
    authorized=ledger.get('history:'+unit,[])
    source_path=PROC/str(pid)/'root/dev/input'
    target_path=PROC/str(consumer_pid)/'root/dev/input'
    # Steam can retain names of removed outputs when replacement outputs get
    # different event numbers. Only skip deleted nodes already in this side's
    # delivery history; an unknown device still fails validation.
    before=input_view_snapshot(source_path,authorized,True,skip_deleted=True)
    old=input_view_snapshot(target_path,authorized,False,allow_empty=True)
    key='view:'+consumer_identity[1]
    count=ledger.get(key,{}).get('replacements',0)
    if count>=64:raise RuntimeError('input view replacement limit reached; restart side')
    with ExitStack() as stack:
        def keep(fd):stack.callback(os.close,fd);return fd
        target=keep(os.open(target_path,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW))
        clone=fresh_input_view(before,keep)
        mount=keep(os.open(f'/proc/{consumer_pid}/ns/mnt',os.O_RDONLY))
        root=keep(os.open(f'/proc/{consumer_pid}/root',os.O_PATH|os.O_DIRECTORY))
        if (steam_changed(family,unit,uid)
                or process_identity(consumer_pid,unit,uid,steam=False)!=consumer_identity
                or input_view_snapshot(source_path,authorized,True,skip_deleted=True)!=before
                or input_view_snapshot(target_path,authorized,False,allow_empty=True)!=old):
            raise RuntimeError('input view changed before replacement')
        mount_in_namespace(mount,root,['--attach-existing',clone,target],(clone,target))
        if input_view_snapshot(target_path,authorized,True)!=before:
            raise RuntimeError('replacement view verification failed')
    ledger[key]={'replacements':count+1,'process':list(consumer_identity),'devices':before}


class NoImports:
    """A finder that refuses every import. Root enters the mount namespace of an account, whose files the account can change:
    whatever Python would load after that (traceback, a codec, a module a function imports late) would come from there and
    run as root. What the helper needs is imported before the entry; nothing else can be."""
    @staticmethod
    def find_spec(name, path=None, target=None):
        raise ImportError(f"no import in the account's root: {name}")


def enter_mount_namespace(mount_fd, root_fd):
    if os.geteuid() != 0:
        raise PermissionError('mount helper requires root')
    if os.readlink(f'/proc/self/fd/{mount_fd}') == os.readlink('/proc/self/ns/mnt'):
        raise RuntimeError('refusing host mount namespace')
    # Before the entry, so that nothing after it can import (a caller that is not namespace_main gets it too).
    if NoImports not in sys.meta_path:
        sys.meta_path.insert(0, NoImports)
    if LIBC.setns(mount_fd, 0x00020000) != 0:
        error=ctypes.get_errno();raise OSError(error,os.strerror(error))
    os.fchdir(root_fd)
    os.chroot('.')
    os.chdir('/')


def namespace_main(mount_fd, root_fd, work):
    """Enter the namespace, run work(), and end the process with os._exit: 0, or 1 with one line on stderr that the parent reports.

    Nothing runs in the account's root but raw calls: the imports are closed before the entry (NoImports), what an error
    needs is loaded before it, and no exception leaves this function, so no traceback is formatted, no atexit handler runs
    and no codec or site code is looked for there. The parent writes the log line from the status and the stderr line.
    """
    for pattern in (INPUT_NODE, PAD_NODE):
        re.fullmatch(pattern, '/dev/null')  # the pattern cache is filled here, not after the entry
    str(OSError(2, 'x', 'y')), 'x'.encode('utf-8', 'replace')
    status = 1
    try:
        enter_mount_namespace(mount_fd, root_fd)
        work()
        sys.stdout.flush()
        status = 0
    except BaseException as error:
        try:
            os.write(2, f'{type(error).__name__}: {error}\n'.encode('utf-8', 'replace'))
        except BaseException:
            pass
    os._exit(status)


def output_records(namespaces, uid, unit, sysname, node, major, minor, consumer, replaced=False):
    """What deliver returns for one output node: a record for each mount namespace of Steam it was served in."""
    return [{'pid':pid,'uid':uid,'unit':unit,'mount_namespace':identity[1],'sysname':sysname,'node':node,
             'major':major,'minor':minor,'consumer':consumer, **({'replaced':True} if replaced else {})}
            for pid,identity,_,_ in namespaces]


def uinput_outputs(family, pidfds, keep):
    """(sysname, sysfs directory) of every virtual input output open in a process of the Steam family, each once.

    Which process of the 64-bit client's chain holds them has not been established on a device, so the descriptors of every
    process are read. pidfds are the pidfds of the family in order; keep(fd) registers a descriptor for the caller to close.
    """
    seen = set()
    for (pid, _), pidfd in zip(family, pidfds):
        for entry in sorted((PROC/str(pid)/'fd').iterdir()):
            try:
                if os.readlink(entry) != '/dev/uinput':
                    continue
            except FileNotFoundError:
                continue
            borrowed = keep(syscall(438, pidfd, int(entry.name), 0))
            data = bytearray(128)
            fcntl.ioctl(borrowed, (2<<30)|(128<<16)|(ord('U')<<8)|44, data, True)
            sysname = data.split(b'\0', 1)[0].decode('ascii')
            if not re.fullmatch('input[0-9]+', sysname):
                raise RuntimeError('invalid uinput identity')
            if sysname in seen:  # a descriptor that one process of the chain inherited from the other
                continue
            seen.add(sysname)
            sysdev = SYS/'class/input'/sysname
            if sysdev.resolve().parent != SYS/'devices/virtual/input':
                raise RuntimeError('output is not virtual input')
            yield sysname, sysdev


def announce(uid, sysnames, run=subprocess.run, sys_root=SYS, dev_root=Path('/dev')):
    """Announce Steam's virtual outputs again (udev add) once deliver has put their nodes where a game can open them.

    Steam destroys its virtual pad when the controller sleeps and makes a new one when it wakes (same event name, new device).
    The kernel's remove and add reach every libudev monitor at that moment, long before delivery has mounted the new node
    into the side's /dev (the watcher polls once a second, and a game's namespace is served after Steam's). A monitor that
    opens the node on that add finds the old, deleted mount or nothing, and never looks again (Proton's winebus, Main, 2026-11-02,
    preview40). This is the second add, with the node in place, as notify() does for a relayed controller.

    Only the outputs named are announced (sysnames are the 'inputN' of virtual outputs open in this side's Steam, from
    uinput_outputs), so the other side's pads are never touched. The generic joystick rule runs again on an add and strips the
    ACL that deliver gave the account, so the ACL is given again before this returns. Returns the sysfs paths announced.
    """
    paths, nodes = [], []
    for sysname in sysnames:
        if not re.fullmatch('input[0-9]+', sysname):
            raise ValueError('invalid uinput identity')
        sysdev = sys_root/'class/input'/sysname
        if sysdev.resolve().parent != sys_root/'devices/virtual/input':
            raise RuntimeError('output is not virtual input')
        paths.append(str(sysdev))  # the input device first, then its nodes, as the kernel announced them
        for child in sorted(sysdev.iterdir()):
            if re.fullmatch(r'(event|js)[0-9]+', child.name):
                paths.append(str(sys_root/'class/input'/child.name))
                nodes.append(child.name)
    if not paths:
        return []
    run(['udevadm', 'trigger', '--action=add', '--settle', *paths], check=True, timeout=5, capture_output=True)
    for name in nodes:
        fd = os.open(dev_root/'input'/name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                raise RuntimeError('output not a device')
            run(['setfacl', '-m', f'u:{uid}:rw', f'/proc/self/fd/{fd}'], pass_fds=(fd,), check=True, timeout=5)
        finally:
            os.close(fd)
    return paths


def serve_output(namespace, node, source, meta, ledger, uid, keep, check, placeholder_ok):
    """Put the pinned output at node in one mount namespace of Steam's family, unless it is already there.

    namespace is (pid, identity, mount namespace descriptor, root descriptor) of the first process of that namespace. The
    path holds nothing (the helper makes the placeholder and mounts over it), the output itself (there is nothing to do), an
    older device this helper delivered to this very process (mounted over, as ever) or, where placeholder_ok, the empty file
    of the account that the helper makes before it mounts: a namespace that shares /dev/input with the one entered before it
    holds that file until it is mounted over. Anything else is refused. check() raises unless every process of the family
    is still the one that was vouched for; it runs before the mount.
    """
    pid, identity, mount, rootfd = namespace
    target = PROC/str(pid)/'root'/node.lstrip('/')
    if target.exists():
        current = target.stat()
        if device_identity(current) != device_identity(meta):
            allowed = replacement_allowed(ledger.get(str(pid) + ':' + node), identity, node, current)
            if not allowed and not placeholder_ok:
                raise RuntimeError('occupied output mountpoint without matching delivery ledger')
            target_fd = keep(os.open(target, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC))
            pinned = os.fstat(target_fd)
            if device_identity(pinned) != device_identity(current):
                raise RuntimeError('output target changed before replacement')
            if not allowed and not owned_placeholder(pinned, uid):
                raise RuntimeError('occupied output mountpoint without matching delivery ledger')
            check()
            # Stack the replacement on the pinned old mount, or mount over the pinned placeholder. No unmount gap, host
            # namespace change, or permission to replace unknown nodes. A detached mount attaches once: one clone each.
            clone = keep(syscall(428, source, b'', 0x1000|1|os.O_CLOEXEC))
            mount_steam_side(pid, identity, mount, rootfd, replacement_arguments(
                current, clone, target_fd, node, uid, pwd.getpwuid(uid).pw_gid), (clone, target_fd))
    else:
        check()
        clone = keep(syscall(428, source, b'', 0x1000|1|os.O_CLOEXEC))
        mount_steam_side(pid, identity, mount, rootfd, ['--attach', clone, node, uid, pwd.getpwuid(uid).pw_gid], (clone,))
    current = target.stat()
    if device_identity(current) != device_identity(meta) or not stat.S_ISCHR(current.st_mode):
        raise RuntimeError('output delivery verification failed')


def stale_pad_node(namespace, node, meta, ledger):
    """Whether the first namespace shows a deleted device this helper delivered at node, other than the output (meta)."""
    pid, identity = namespace[0], namespace[1]
    try:
        current = (PROC/str(pid)/'root'/node.lstrip('/')).stat()
    except FileNotFoundError:
        return False
    return (stat.S_ISCHR(current.st_mode) and current.st_nlink == 0
            and device_identity(current) != device_identity(meta)
            and replacement_allowed(ledger.get(str(pid) + ':' + node), identity, node, current))


def replace_deleted_output(namespaces, node, source, meta, uid, keep, check, start=start_placeholder_replacement):
    """Replace a deleted output at node in the outermost namespace so the side's /dev/input sees IN_DELETE and IN_CREATE.

    First the stale mount is detached in every namespace of the family that still shows it (outermost first: a dentry that is
    a mountpoint anywhere cannot be unlinked). Then the outermost namespace's helper unlinks and makes the placeholder again
    and mounts a clone of the output onto it, and waits. Returns the handle that holds that helper: the caller serves the
    other namespaces (they find the new placeholder, the same dentry) and the game, then calls release() for the IN_ATTRIB.
    """
    gid = pwd.getpwuid(uid).pw_gid
    outer = namespaces[0]
    outer_target = PROC/str(outer[0])/'root'/node.lstrip('/')
    stale = []
    for namespace in namespaces:
        target = PROC/str(namespace[0])/'root'/node.lstrip('/')
        try:
            fd = keep(os.open(target, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC))
        except FileNotFoundError:
            continue
        pinned = os.fstat(fd)
        if namespace is outer:
            if not stat.S_ISCHR(pinned.st_mode) or pinned.st_nlink != 0:
                raise RuntimeError('replacement target is not a deleted device')
            first = pinned
        elif not stat.S_ISCHR(pinned.st_mode):
            continue  # no stale mount here: a placeholder, which is what the new one will be
        elif pinned.st_nlink != 0 or device_identity(pinned) != device_identity(first):
            raise RuntimeError('namespace shows a device other than the deleted one')
        stale.append((namespace, fd))
    check()
    for namespace, fd in stale:
        mount_steam_side(namespace[0], namespace[1], namespace[2], namespace[3], ['--detach-deleted', fd, node], (fd,))
    check()
    clone = keep(syscall(428, source, b'', 0x1000|1|os.O_CLOEXEC))
    handle = start(outer, clone, node, uid, gid, keep)
    if device_identity(outer_target.stat()) != device_identity(meta):
        handle.abort()
        raise RuntimeError('output delivery verification failed')
    return handle


def deliver(root, side, consumer_pid=None, launch_app=None, carried=None):
    """Deliver Steam's generated event/js nodes into every mount namespace of the side's Steam, a record for each of them.

    Steam is one process or the chain its 64-bit client runs as (steam_family), and each distinct mount namespace of it is
    served on its own. With consumer_pid, the nodes also go into that game process's namespace. With launch_app, the app is
    started through the outermost Steam process and nothing is delivered.

    A deleted output replaced at its node (replace_deleted_output) leaves a helper waiting in the outermost namespace; it is
    released (IN_ATTRIB on the directory) once the node's game namespace, if any, was served. With a consumer, the call that
    serves Steam first runs with carried (a dict, filled with the waiting helpers by node) and leaves them to this one.
    """
    if os.geteuid() != 0 or sys.platform != 'linux' or os.uname().machine != 'x86_64':
        raise RuntimeError('requires root on Linux x86_64')
    if root.parent != Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}', root.name):
        raise ValueError('unexpected trial path')
    if side not in ('left', 'right'):
        raise ValueError('invalid side')
    for p in (root, root/'transaction.json', root/'sides', root/'sides'/side, root/'sides'/side/'session.json'):
        st = p.lstat()
        if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
            raise PermissionError('untrusted trial metadata')
    transaction=json.loads((root/'transaction.json').read_text())
    if transaction.get('keep_session') is not True and subprocess.run(['systemctl','is-active','--quiet',transaction['recovery']+'.timer']).returncode:
        raise RuntimeError('independent recovery is not armed')
    session=json.loads((root/'sides'/side/'session.json').read_text())
    uid=output_owner(transaction, side, session)
    defer = carried is not None
    pending = carried if defer else {}
    if consumer_pid is not None:
        # Complete every Steam-side event/js replacement before cloning its view.
        pending = {}
        deliver(root,side,carried=pending)
    unit=session['unit']
    if not re.fullmatch('dosplex-session-'+side+r'-[a-f0-9]{32}\.service',unit):
        raise ValueError('unexpected side unit')
    properties=subprocess.check_output(['systemctl','show',unit,'-p','BindsTo','-p','ActiveState'],text=True)
    if 'ActiveState=active\n' not in properties or 'BindsTo='+transaction['unit']+'\n' not in properties:
        raise RuntimeError('side is not bound to active trial')
    # One process, or the chain Steam's 64-bit client runs as; anything else raises with what was found. The outermost
    # process is the one to enter and to read the environment of.
    family=steam_family(unit,uid)
    pid=family[0][0]
    if launch_app is not None:
        if consumer_pid is not None or not 1 <= launch_app <= 0xffffffff:
            raise ValueError('invalid app launch request')
        keep={'HOME','USER','LOGNAME','PATH','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS',
              'DISPLAY','XAUTHORITY','WAYLAND_DISPLAY','LD_LIBRARY_PATH','STEAM_RUNTIME',
              'STEAM_COMPAT_CLIENT_INSTALL_PATH'}
        values=[v for v in (PROC/str(pid)/'environ').read_bytes().split(b'\0')
                if v.split(b'=',1)[0].decode(errors='ignore') in keep]
        if steam_changed(family,unit,uid):raise RuntimeError('Steam changed before launch')
        subprocess.run(['nsenter','-t',str(pid),'-m','-p','--','runuser','-u',pwd.getpwuid(uid).pw_name,
            '--','env','-i',*[v.decode() for v in values],'/usr/bin/steam','-applaunch',str(launch_app)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=15)
        return {'launch_requested':launch_app,'uid':uid,'unit':unit}
    consumer_identity = process_identity(consumer_pid, unit, uid, steam=False) if consumer_pid else None
    ledger_path = root / 'native-output-mounts.json'
    ledger = {}
    if ledger_path.exists():
        metadata = ledger_path.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o077 or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError('untrusted output mount ledger')
        ledger = json.loads(ledger_path.read_text())
    history_key = 'history:' + unit
    results=[]
    with ExitStack() as stack:
        def keep(fd):stack.callback(os.close,fd);return fd
        def check():
            if steam_changed(family,unit,uid):raise RuntimeError('Steam changed')
        if not defer:  # a caller that passed carried owns the handles in it
            for waiting in pending.values():stack.callback(waiting.abort)  # a no-op once released
        pidfds=[keep(os.pidfd_open(member)) for member,_ in family]
        # A mount is made in one mount namespace only: enter the first process of each one.
        namespaces=[(member,identity,keep(os.open(f'/proc/{member}/ns/mnt',os.O_RDONLY)),
                     keep(os.open(f'/proc/{member}/root',os.O_PATH|os.O_DIRECTORY)))
                    for member,identity in mount_namespaces(family)]
        pidns=keep(os.open(f'/proc/{pid}/ns/pid',os.O_RDONLY))
        for sysname,sysdev in uinput_outputs(family,pidfds,keep):
            for event in sorted(sysdev.iterdir()):
                if not re.fullmatch(r'(event|js)[0-9]+',event.name):continue
                node='/dev/input/'+event.name
                source=keep(os.open(node,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
                meta=os.fstat(source)
                if not stat.S_ISCHR(meta.st_mode):raise RuntimeError('output not a device')
                major,minor=map(int,(event/'dev').read_text().strip().split(':'))
                if meta.st_rdev!=os.makedev(major,minor):raise RuntimeError('event node reused')
                check()
                # This node is owned by the temporary Steam uinput creator; its
                # lifetime ends with that process. Never change physical ACLs here.
                subprocess.run(['setfacl','-m',f'u:{uid}:rw',f'/proc/self/fd/{source}'],
                               pass_fds=(source,),check=True,timeout=5)
                if node not in pending and stale_pad_node(namespaces[0],node,meta,ledger):
                    # Steam made a new pad under the name of a deleted one: replace it so the directory gets events.
                    pending[node]=replace_deleted_output(namespaces,node,source,meta,uid,keep,check)
                    if not defer:stack.callback(pending[node].abort)
                    served_first=True
                else:served_first=False
                def finish(node=node):
                    waiting=pending.get(node)
                    if waiting is not None and not defer:
                        waiting.release()
                        del pending[node]
                for index,namespace in enumerate(namespaces):
                    # Outermost first. Each namespace is journaled under the process whose namespace it is, as soon as it is
                    # served, so a delivery that stopped halfway continues where it stopped.
                    if not (index==0 and served_first):
                        serve_output(namespace,node,source,meta,ledger,uid,keep,check,index>0)
                    history = ledger.setdefault(history_key, [])
                    if device_identity(meta) not in history:
                        history.append(device_identity(meta))
                    ledger[str(namespace[0]) + ':' + node] = {'process': list(namespace[1]), 'node': node,
                        'identity': device_identity(meta)}
                    save_delivery_ledger(ledger_path, ledger)
                consumer_result = None
                if consumer_pid:
                    if process_identity(consumer_pid,unit,uid,steam=False)!=consumer_identity:
                        raise RuntimeError('consumer changed before delivery')
                    consumer_target=PROC/str(consumer_pid)/'root'/node.lstrip('/')
                    try:target_fd=keep(os.open(consumer_target,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
                    except FileNotFoundError:
                        replace_consumer_input_view(family,consumer_pid,unit,uid,consumer_identity,ledger)
                        save_delivery_ledger(ledger_path,ledger)
                        target_fd=keep(os.open(consumer_target,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
                    target_meta=os.fstat(target_fd)
                    if stat.S_ISCHR(target_meta.st_mode):
                        if (target_meta.st_dev,target_meta.st_ino,target_meta.st_rdev)!=(meta.st_dev,meta.st_ino,meta.st_rdev):
                            if device_identity(target_meta) not in ledger.get(history_key, []):
                                raise RuntimeError('consumer has an unproven different device')
                            if target_meta.st_nlink == 0:
                                replace_consumer_input_view(family,consumer_pid,unit,uid,consumer_identity,ledger)
                                save_delivery_ledger(ledger_path,ledger)
                                target_meta=consumer_target.stat()
                                if device_identity(target_meta)!=device_identity(meta):
                                    raise RuntimeError('fresh consumer view differs from output')
                                consumer_result={'pid':consumer_pid,'mount_namespace':consumer_identity[1], 'device_visible':True}
                                replaced=node in pending
                                finish()
                                results.extend(output_records(namespaces,uid,unit,sysname,node,major,minor,consumer_result,replaced))
                                continue
                            consumer_mnt=keep(os.open(f'/proc/{consumer_pid}/ns/mnt',os.O_RDONLY))
                            consumer_root=keep(os.open(f'/proc/{consumer_pid}/root',os.O_PATH|os.O_DIRECTORY))
                            clone=keep(syscall(428,source,b'',0x1000|1|os.O_CLOEXEC))
                            if process_identity(consumer_pid,unit,uid,steam=False)!=consumer_identity:
                                raise RuntimeError('consumer changed while pinning replacement')
                            mount_in_namespace(consumer_mnt,consumer_root,
                                replacement_arguments(target_meta,clone,target_fd,node,uid,pwd.getpwuid(uid).pw_gid),(clone,target_fd))
                    else:
                        if not owned_placeholder(target_meta,uid):
                            raise RuntimeError('consumer target is not an owned empty placeholder')
                        consumer_mnt=keep(os.open(f'/proc/{consumer_pid}/ns/mnt',os.O_RDONLY))
                        consumer_root=keep(os.open(f'/proc/{consumer_pid}/root',os.O_PATH|os.O_DIRECTORY))
                        clone=keep(syscall(428,source,b'',0x1000|1|os.O_CLOEXEC))
                        if process_identity(consumer_pid,unit,uid,steam=False)!=consumer_identity:
                            raise RuntimeError('consumer changed while pinning namespace')
                        mount_in_namespace(consumer_mnt,consumer_root,
                            replacement_arguments(target_meta,clone,target_fd,node,uid,pwd.getpwuid(uid).pw_gid),(clone,target_fd))
                    observed=consumer_target.stat()
                    if (observed.st_dev,observed.st_ino,observed.st_rdev)!=(meta.st_dev,meta.st_ino,meta.st_rdev):
                        raise RuntimeError('consumer delivery did not reach the pinned device')
                    consumer_result={'pid':consumer_pid,'mount_namespace':consumer_identity[1],
                                     'device_visible':True}
                replaced=node in pending
                finish()
                results.extend(output_records(namespaces,uid,unit,sysname,node,major,minor,consumer_result,replaced))
    if not results:raise RuntimeError('Steam has no initialized uinput output yet')
    return results


def helper_operation(argv):
    """Run one of the private helper operations (argv[0] is its name); False when argv is none of them."""
    if len(argv)==5 and argv[0]=='--attach':
        attach(int(argv[1]),argv[2],int(argv[3]),int(argv[4]))
    elif len(argv)==5 and argv[0]=='--attach-pad':
        attach(int(argv[1]),argv[2],int(argv[3]),int(argv[4]),PAD_NODE)
    elif len(argv)==6 and argv[0]=='--replace-deleted-pad':
        replace_deleted_mount(int(argv[1]), int(argv[2]), argv[3], int(argv[4]), int(argv[5]), PAD_NODE)
    elif len(argv)==6 and argv[0]=='--replace-deleted':
        replace_deleted_mount(int(argv[1]), int(argv[2]), argv[3], int(argv[4]), int(argv[5]))
    elif len(argv)==3 and argv[0]=='--detach-deleted':
        detach_deleted_mount(int(argv[1]),argv[2])
    elif len(argv)==6 and argv[0]=='--replace-placeholder':
        replace_placeholder(int(argv[1]),argv[2],int(argv[3]),int(argv[4]),int(argv[5]))
    elif len(argv)==3 and argv[0]=='--attach-existing':
        syscall(429,int(argv[1]),b'',int(argv[2]),b'',4|0x40)
    else:
        return False
    return True


def namespace_operation(argv):
    if not helper_operation(argv):
        raise ValueError('unknown namespace operation')


if __name__=='__main__':
    if len(sys.argv)>=6 and sys.argv[1]=='--namespace':
        namespace_main(int(sys.argv[2]),int(sys.argv[3]),lambda:namespace_operation(sys.argv[4:]))
    elif not helper_operation(sys.argv[1:]):
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--root',type=Path,required=True)
        parser.add_argument('--side',choices=('left','right'),required=True)
        parser.add_argument('--consumer-pid',type=int,help='exact existing game process in the assigned side unit')
        parser.add_argument('--launch-app',type=int,help='request game launch in the validated side; no input delivery in this call')
        args=parser.parse_args()
        print(json.dumps(deliver(args.root,args.side,args.consumer_pid,args.launch_app),indent=2))
