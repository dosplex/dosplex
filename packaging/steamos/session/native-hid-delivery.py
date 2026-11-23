#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Bind only a pinned mediated HID into the mount namespaces of an enrolled Steam.

HID mounts stay private to the side unit. A journaled runtime udev rule keeps
enrolled physical HIDs root-only during discovery; recovery removes that rule.

A controller relay's node has the device number 120:N, where N is the number of the /dev/hidrawN it serves (native-deck-plan.py
relay_number). The kernel opens a character device by its number, not by its inode. A mount in a Steam namespace of the node of
an earlier relay of that hidrawN (a relay that was started again, a receiver that was plugged in again) therefore opens the
live relay, or fails with ENXIO when there is none. Such a node counts as delivered (numbered): nothing is unmounted or moved.

A Steam client in a container does not learn of that node from the udev add that notify() sends. rescan_poke() (below) wakes the
watcher it does use: after delivery has made the relay's node usable in a mount namespace or found that the relay was started
again since it last looked there (served), or the router has handed the controller to the side whose namespace it is
(recovered_family), it creates one empty file in that namespace's /dev as the side's account, and removes it again.
"""
import ctypes
import importlib.util
import errno
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager


def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


d = module('output_delivery', 'native-output-delivery.py')
PROC = Path('/proc')
_NUMBERING = []


def numbering():
    """native-deck-plan.py, which says what number the node of a relay has: loaded when it is first needed, then kept."""
    if not _NUMBERING:_NUMBERING.append(module('native_plan','native-deck-plan.py'))
    return _NUMBERING[0]


def relay_numbered(relay):
    """Whether the live relay's node has the number 120:N of the hidrawN it serves. A relay on a dynamic number does not
    (the wrapper falls back to one when 120:N is not to be had), and then nothing counts as numbered for it."""
    try:return relay['identity'][2]==os.makedev(*numbering().relay_number(relay['target']))
    except (ValueError,LookupError,TypeError,AttributeError):return False     # (a plan module from before the numbers has none)


def numbered(meta, relay):
    """Whether meta, the metadata of a node in a Steam namespace, is a node of an earlier relay of this hidraw node.

    An unlinked character device (the relay that made it is gone) on the live relay's filesystem, with the live relay's
    number, and that number is 120:N for the hidrawN the relay serves. Opening it reaches the live relay; a node with
    another number, another filesystem or a link, or any node of a relay on a dynamic number, is not this.
    """
    return (stat.S_ISCHR(meta.st_mode) and meta.st_nlink==0 and relay_numbered(relay)
            and meta.st_dev==relay['identity'][0] and meta.st_rdev==relay['identity'][2])


class SteamFamily(tuple):
    """The Steam of one side: a single process, or the chain Steam's 64-bit client (steamrt64) runs as.

    It unpacks as (pid, identity, unit, uid) of the OUTERMOST process, as the one process always did, so the tools that
    enter Steam's namespaces or read its environment keep working. members lists (pid, identity) of the whole chain,
    outermost first; the last one is the client that opens the devices. Two families are equal only when they are the same
    processes: a replaced inner client is a replaced Steam.
    """
    def __new__(cls, unit, uid, members):
        members = tuple((int(pid), tuple(identity)) for pid, identity in members)
        if not members:
            raise ValueError('a Steam family has at least one process')
        family = super().__new__(cls, (*members[0], unit, uid))
        family.members = members
        return family

    unit = property(lambda self: self[2])
    uid = property(lambda self: self[3])
    pids = property(lambda self: tuple(pid for pid, _ in self.members))

    def __eq__(self, other):
        if isinstance(other, SteamFamily):
            return tuple.__eq__(self, other) and self.members == other.members
        return tuple.__eq__(self, other)

    def __ne__(self, other):
        equal = self.__eq__(other)
        return equal if equal is NotImplemented else not equal

    __hash__ = tuple.__hash__

    def __reduce__(self):
        return SteamFamily, (self.unit, self.uid, self.members)

    def __repr__(self):
        return f'SteamFamily(unit={self.unit!r}, uid={self.uid!r}, members={self.members!r})'


def family_members(process):
    """(pid, identity) of each process of a steam() result; a plain (pid, identity, unit, uid) tuple is one process."""
    return getattr(process, 'members', None) or ((process[0], process[1]),)


def unchanged(process, message):
    """Raise unless every process of the family is still the one steam() saw (same start time and mount namespace)."""
    for pid, identity in family_members(process):
        if d.process_identity(pid, process[2], process[3]) != identity:
            raise RuntimeError(message)


class SystemdUnresponsive(RuntimeError):
    """systemctl did not answer within its limit. systemd is one process: when it does not answer for the unit of one side it
    does not for the unit of the other either, which is why a caller that asks for more than one side can stop asking."""


def steam(root, side):
    tx=json.loads((root/'transaction.json').read_text())
    session_path=root/'sides'/side/'session.json'
    for path in (root,root/'transaction.json',session_path,*session_path.parents):
        st=path.lstat()
        if st.st_uid or st.st_mode&0o022 or stat.S_ISLNK(st.st_mode):
            raise PermissionError('untrusted native delivery metadata')
    session=json.loads(session_path.read_text());uid=d.output_owner(tx,side,session)
    # A side whose session has not been started yet (the first seconds of a start, Guest's a little after Main's) has
    # no unit: the controller waits for it, and the row must not say a bare 'unit'.
    unit=session.get('unit') if isinstance(session,dict) else None
    if not unit:raise RuntimeError("the side's session has not started yet")
    if not isinstance(unit,str) or not re.fullmatch('dosplex-session-'+side+r'-[a-f0-9]{32}\.service',unit):
        raise ValueError('invalid native delivery unit')
    # This runs on the routing thread, which also keeps every other controller's rows fresh: it must not wait for a stuck systemd.
    try:values=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','-p','BindsTo'],text=True,timeout=3)
    except subprocess.TimeoutExpired as error:raise SystemdUnresponsive(f'systemctl show {unit} did not answer in {error.timeout:g} s') from error
    if 'ActiveState=active\n' not in values or 'BindsTo='+tx['unit']+'\n' not in values:
        raise RuntimeError('side is not bound to retained compositor')
    # One process, or the chain of them Steam's 64-bit client runs as; anything else raises with what was found.
    return SteamFamily(unit,uid,d.steam_family(unit,uid))


def holds(pid, uid, relay):
    """Whether this process has the relay's device open and a working reader (an inode match alone proves nothing).

    A relay with a number counts a handle on any node with its device number and filesystem, an earlier node of the relay
    included: Steam opens the path of the stale mount and reaches the live relay. The ioctl below still has to prove it,
    for a handle on the stale node that was opened before the relay was started again is dead.
    """
    counted=relay_numbered(relay)
    for path in (PROC/str(pid)/'fd').iterdir():
        try:
            meta=path.stat()
            if d.device_identity(meta)!=relay['identity'] and not (
                    counted and stat.S_ISCHR(meta.st_mode) and meta.st_dev==relay['identity'][0] and meta.st_rdev==relay['identity'][2]):
                continue
            # Steam can retain a revoked metadata handle. Its inode alone is
            # not proof of a working reader. Check the harmless metadata ioctl
            # under the owning account through a borrowed, pinned descriptor.
            pidfd=os.pidfd_open(pid)
            try:borrowed=d.syscall(438,pidfd,int(path.name),0)
            finally:os.close(pidfd)
            try:
                result=subprocess.run([sys.executable,'-I','-c',
                    'import fcntl,sys; fcntl.ioctl(int(sys.argv[1]),0x80084803,bytearray(8))',str(borrowed)],
                    user=uid,group=pwd.getpwuid(uid).pw_gid,extra_groups=[],pass_fds=(borrowed,),
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
                if result.returncode==0:return True
            finally:os.close(borrowed)
        except OSError as error:
            # Steam closes revoked descriptors concurrently with this check.
            if error.errno not in (errno.ENOENT,errno.EBADF,errno.ENODEV):raise
    return False


def opened(process, relay):
    unchanged(process,'Steam replaced')
    # The client that opens devices is the innermost process of the family; any of them may hold the handle.
    return any(holds(pid,process[3],relay) for pid,_ in reversed(family_members(process)))


def namespaces(process):
    """(pid, identity) of the first process of each mount namespace of the family, outermost first.

    Steam's 64-bit client runs its launcher and its client in two mount namespaces. On the Deck (2026-10-18) a placeholder
    made in the launcher's namespace showed in the client's as an empty file, but the mount over it did not: a mount is
    made in one namespace only. A family of one process, or of several that share a namespace, has one.
    """
    seen, found = set(), []
    for pid, identity in family_members(process):
        if identity[1] not in seen:
            seen.add(identity[1])
            found.append((pid, identity))
    return found


def placeholder_problem(meta, uid):
    """Why a file is not the placeholder the relay is mounted over, or None if it is one.

    That is the empty regular file of the account that the helper makes before it mounts (native-output-delivery.py
    accepts the same file for a consumer). A namespace the helper did not enter holds it until it is mounted over.
    """
    shown = stat.filemode(meta.st_mode)
    if stat.S_ISCHR(meta.st_mode):return f'a device node that is not the relay ({shown})'
    if not stat.S_ISREG(meta.st_mode):return f'not a regular file ({shown})'
    if meta.st_uid!=uid:return f'owned by uid {meta.st_uid}, not by the account {uid}'
    if stat.S_IMODE(meta.st_mode)!=0o600:return f'mode {stat.S_IMODE(meta.st_mode):04o}, not 0600'
    if meta.st_size:return f'{meta.st_size} bytes, not empty'
    if meta.st_nlink!=1:return f'{meta.st_nlink} links, not 1'
    return None


def mountpoint_state(pid, uid, relay, keep):
    """What the HID's path holds in the mount namespace of one process, as (state, descriptor).

    mounted      the relay's device: there is nothing to deliver (no descriptor)
    absent       nothing: the helper makes the placeholder and mounts over it (no descriptor)
    placeholder  the empty file of the account; the descriptor pins that file (O_PATH), so the mount goes onto it and no other
    numbered     the node of an earlier relay of this hidraw node, with the live relay's number (numbered()); the descriptor pins it
    occupied     anything else, another device included; the descriptor pins that file for the message

    keep(fd) registers a descriptor for the caller to close.
    """
    view=PROC/str(pid)/'root'/relay['target'].lstrip('/')
    try:meta=view.stat()
    except FileNotFoundError:return 'absent',None
    if d.device_identity(meta)==relay['identity']:return 'mounted',None
    fd=keep(os.open(view,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
    pinned=os.fstat(fd)
    if numbered(pinned,relay):return 'numbered',fd
    return ('occupied' if placeholder_problem(pinned,uid) else 'placeholder'),fd


def helper_failure(error):
    """What a failed namespace helper said: the last line of its stderr, or how it ended."""
    if isinstance(error,subprocess.TimeoutExpired):return 'timed out'
    text=error.stderr if isinstance(error.stderr,str) else (error.stderr or b'').decode(errors='replace')
    lines=text.strip().splitlines()
    return lines[-1] if lines else f'exit status {error.returncode}'


def journal_mount(root, pid, target, entry):
    """Record what was delivered in the mount namespace of a process, under pid:target in native-routing-mounts.json."""
    ledger_path=root/'native-routing-mounts.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    ledger[str(pid)+':'+target]=entry
    d.save_delivery_ledger(ledger_path,ledger)


def journaled(root, pid, target):
    """What native-routing-mounts.json holds for the mount namespace of a process, or None (also when it cannot be read)."""
    try:entry=json.loads((root/'native-routing-mounts.json').read_text()).get(str(pid)+':'+target)
    except (OSError,ValueError,AttributeError):return None
    return entry if isinstance(entry,dict) else None


def serve_numbered(root, side, process, pid, identity, relay, pinned):
    """A numbered node counts as delivered: it stays mounted, and nothing is unmounted or moved.

    Its owner and mode follow the account of the side, as the live relay's own node does (native-ownership.py transfer): the
    account, the account's group and 0600. If they differ, that is all that is changed, through the descriptor that pins the
    node, after the whole family has been verified again and the pin has been looked at again. The result is checked in the
    namespace. The relay's own owner check still decides who may use the node.

    The change is journaled before it is made, as pending, and logged as soon as it has been made, with what the node was and
    what it is by then: the account owns the node from the chown on and can change its mode at once, so the check of the result
    can fail, and a router that stops half way leaves no more than the journal. The entry becomes final when the result is
    right: when this pass sees it through, or a later one finds the node as it should be. What the node was before DosPlex first
    changed it stays what the entry says, however many passes it takes.

    True if this pass made the node usable (it changed it, or it made an entry final that an earlier pass had left pending),
    False if it found the node as it should be.
    """
    uid=process[3];wanted=(uid,uid,0o600);target=relay['target']
    before=os.fstat(pinned);found=(before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode));node=d.device_identity(before)
    kept=journaled(root,pid,target);earlier=kept.get('numbered') if kept else None
    if not isinstance(earlier,dict) or earlier.get('node')!=node:earlier=None      # (what it says of another node here is not this one's)
    if found==wanted:
        if earlier and earlier.get('pending'):
            journal_mount(root,pid,target,dict(kept,numbered={key:value for key,value in earlier.items() if key!='pending'}))
            return True
        return False
    unchanged(process,'Steam changed')
    now=os.fstat(pinned)
    if not numbered(now,relay) or (now.st_uid,now.st_gid,stat.S_IMODE(now.st_mode))!=found:
        raise RuntimeError(f'numbered HID node {target} in {identity[1]} (process {pid}) changed while it was looked at')
    was=dict(uid=found[0],gid=found[1],mode='%04o'%found[2])
    entry=dict(side=side,process=list(identity),relay=relay,
               numbered=dict(node=node,was=earlier['was'] if earlier and earlier.get('was') else was,owner=uid,mode='0600'))
    journal_mount(root,pid,target,dict(entry,numbered=dict(entry['numbered'],pending=True)))
    link=f'/proc/self/fd/{pinned}'
    os.chown(link,uid,uid);os.chmod(link,0o600)
    after=os.fstat(pinned)
    print(json.dumps({'native_numbered_node':dict(process=pid,namespace=identity[1],target=target,node=node,was=was,owner=uid,mode='0600',
                                                  now=dict(uid=after.st_uid,gid=after.st_gid,mode='%04o'%stat.S_IMODE(after.st_mode)))}),flush=True)
    view=(PROC/str(pid)/'root'/target.lstrip('/')).stat()
    if (d.device_identity(view)!=d.device_identity(now) or (view.st_uid,view.st_gid,stat.S_IMODE(view.st_mode))!=wanted
            or not numbered(view,relay)):
        raise RuntimeError(f'numbered HID node {target} in {identity[1]} (process {pid}) is not as it was set')
    journal_mount(root,pid,target,entry)
    return True


def serve(root, side, process, pid, identity, relay):
    """Mount the relay at the HID's path in the mount namespace of one process of the family, unless it is already there.

    The checks of a single Steam process, per namespace: the relay is pinned and its identity checked, the whole family is
    verified again before the namespace is entered, and the mount is checked afterwards and journaled in
    native-routing-mounts.json under the process whose namespace it is. Nothing here is unmounted later: every process of
    the family was checked to be in the exact side unit, and the mount ends with the namespace when that unit stops.

    What it found and did: 'mounted' (the relay's node was there), 'numbered' (an earlier node of the relay that needed
    nothing), 'repaired' (an earlier node of the relay that this pass made usable: serve_numbered changed it, or made a
    pending entry of it final) or 'delivered' (it mounted the relay now).
    """
    uid=process[3];source=Path(relay['path']);target=relay['target']
    with ExitStack() as stack:
        def keep(fd):stack.callback(os.close,fd);return fd
        state,pinned=mountpoint_state(pid,uid,relay,keep)
        if state=='mounted':return 'mounted'
        if state=='numbered':return 'repaired' if serve_numbered(root,side,process,pid,identity,relay,pinned) else 'numbered'
        if state=='occupied':
            why=placeholder_problem(os.fstat(pinned),uid) or 'it changed while it was looked at'
            raise RuntimeError(f'occupied native HID mountpoint {target} in {identity[1]} (process {pid}): {why}')
        fd=keep(os.open(source,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC))
        if d.device_identity(os.fstat(fd))!=relay['identity']:raise RuntimeError('relay changed')
        # A detached mount can be attached once: each namespace gets a clone of its own.
        clone=keep(d.syscall(428,fd,b'',0x1000|1|os.O_CLOEXEC))
        mount=keep(os.open(f'/proc/{pid}/ns/mnt',os.O_RDONLY))
        rootfd=keep(os.open(f'/proc/{pid}/root',os.O_PATH|os.O_DIRECTORY))
        unchanged(process,'Steam changed')
        try:
            if state=='placeholder':
                d.mount_in_namespace(mount,rootfd,['--attach-existing',clone,pinned],(clone,pinned))
            else:
                subprocess.run([sys.executable,str(Path(__file__).resolve()),'--attach',
                    str(mount),str(rootfd),str(clone),target,str(uid),str(pwd.getpwuid(uid).pw_gid)],
                    pass_fds=(mount,rootfd,clone),check=True,timeout=5,capture_output=True)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'native HID delivery failed in {identity[1]} (process {pid}): '
                               +helper_failure(error)) from error
    view=PROC/str(pid)/'root'/target.lstrip('/')
    if d.device_identity(view.stat())!=relay['identity']:raise RuntimeError('HID delivery mismatch')
    journal_mount(root,pid,target,dict(side=side,process=list(identity),relay=relay))
    return 'delivered'


def deliver(root, side, relay):
    """Put the relay at its HID path in every mount namespace of the side's Steam, outermost first; returns the family.

    Steam's 64-bit client is two processes in two namespaces, and a mount made after the client started reaches only the
    namespace it is made in. Each namespace is served on its own, so a transfer that stopped halfway continues where it
    stopped. The relay goes where there is nothing or the account's empty placeholder; a numbered node (an earlier node of
    the relay, which opens the live relay) stays where it is; whatever else is at the path, a device that is not the relay
    included, is refused.

    What it found in each namespace is noted for rescan_poke (served): a namespace in which it made the node usable, and one
    that holds an earlier node of a relay that was started again since delivery last looked there. Delivery does not poke: the
    router does, where it announces the controller, which for a transfer is after the route has been switched. A namespace
    that holds the live relay already is not noted here; the router notes the side that a controller is handed to
    (recovered_family).
    """
    process=steam(root,side)
    source=Path(relay['path']);target=relay['target']
    if (not re.fullmatch('/dev/dosplex-hid-[a-f0-9]{32}',str(source)) or
            not re.fullmatch('/dev/hidraw[0-9]+',target)):
        raise ValueError('invalid native HID binding')
    st=source.lstat()
    if not stat.S_ISCHR(st.st_mode) or d.device_identity(st)!=relay['identity']:
        raise RuntimeError('mediated HID was replaced')
    for pid,identity in namespaces(process):served(pid,identity,relay,serve(root,side,process,pid,identity,relay))
    return process


STATES={'mounted':'mounted','absent':'missing','placeholder':'empty placeholder','occupied':'occupied by something else',
        'numbered':'earlier node of the relay'}


def describe(process, relay):
    """Where the relay stands in each mount namespace of the family, for the message of a transfer Steam did not take.

    'PID NAMESPACE STATE' for each namespace, separated by commas. It does not raise: a namespace it cannot read says so.
    """
    found=[]
    with ExitStack() as stack:
        def keep(fd):stack.callback(os.close,fd);return fd
        for pid,identity in namespaces(process):
            try:state=STATES[mountpoint_state(pid,process[3],relay,keep)[0]]
            except OSError as error:state='unreadable ('+(error.strerror or type(error).__name__)+')'
            found.append(f'{pid} {identity[1]} {state}')
    return ', '.join(found)


def rule_path(root):
    if not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):
        raise ValueError('invalid native rule owner')
    return Path('/run/udev/rules.d')/('72-'+root.name+'-native.rules')


def checked_rule(root, missing=False, pending=False):
    journal=root/'native-hid-udev.json'
    st=journal.lstat()
    if st.st_uid or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode):
        raise PermissionError('untrusted native rule journal')
    record=json.loads(journal.read_text());path=rule_path(root)
    if record['path']!=str(path):raise ValueError('unexpected native rule path')
    try:st=path.lstat()
    except FileNotFoundError:
        if missing:return path,record
        raise
    if st.st_uid or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode):
        raise PermissionError('untrusted native rule')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=record['sha256'] and not (pending and digest==record.get('previous_sha256')):
        raise RuntimeError('native rule changed; preserve for inspection')
    return path,record


def rule_line(native, record):
    """The rule line that keeps the node of one enrolled physical HID root-only. Raises if the node is not the pinned one."""
    path=native.checked_path(record)
    syspath=(Path('/sys/class/hidraw')/path.name).resolve(strict=True)
    devpath='/'+str(syspath.relative_to('/sys'))
    if not re.fullmatch('/devices/[-a-zA-Z0-9_./:+]+',devpath):
        raise ValueError('unexpected physical HID sysfs path')
    return ('SUBSYSTEM=="hidraw", DEVPATH=="'+devpath+'", '
            'MODE:="0600", OWNER:="root", GROUP:="root", TAG-="uaccess"')


def protect_sources(root, plans):
    """Keep normal uaccess rules from exposing enrolled physical HIDs.

    Journal before installing one volatile rule. The original ACL snapshots
    stay in the native plans; recovery removes this rule before restoring them.
    """
    native=module('native_plan','native-deck-plan.py')
    records={record['path']:record for plan in plans for record in plan['devices']
             if record['path'].startswith('/dev/hidraw')}
    lines=['# Owned by '+root.name+'; remove through trial recovery.']
    for target,record in sorted(records.items()):
        lines.append(rule_line(native,record))
    content=('\n'.join(lines)+'\n').encode();path=rule_path(root)
    journal=root/'native-hid-udev.json'
    record=dict(path=str(path),sha256=hashlib.sha256(content).hexdigest(),devices=records)
    if journal.exists():
        _,saved=checked_rule(root,missing=True,pending=True)
        # Enrollment may add slots, but cannot silently replace or drop any
        # physical snapshot still owned by this trial.
        if any(records.get(key)!=value for key,value in saved['devices'].items()):
            raise RuntimeError('native rule enrollment changed existing devices')
    else:
        if path.exists() or path.is_symlink():raise RuntimeError('native rule already exists without journal')
    path.parent.mkdir(mode=0o755,exist_ok=True)
    if not path.exists() or path.read_bytes()!=content:
        pending=dict(record)
        if path.exists():pending['previous_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        d.save_delivery_ledger(journal,pending)
        temporary=root/'native-hid-udev.rules.next'
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
        with os.fdopen(fd,'wb') as output:output.write(content);output.flush();os.fsync(output.fileno())
        temporary.replace(path)
    d.save_delivery_ledger(journal,record)
    checked_rule(root)
    subprocess.run(['udevadm','control','--reload-rules'],check=True,timeout=3,capture_output=True)
    # Also undo permissions reapplied by earlier rescans. Pinned plan records
    # prevent changing a replacement device. uinput is deliberately excluded.
    for plan in plans:
        physical=dict(plan,devices=[r for r in plan['devices'] if r['path'] in records])
        native.grant(physical)
    # Update udev's saved tags on every protected interface, so a later seat
    # activation cannot restore uaccess from its pre-trial database entry.
    for record in records.values():
        target=native.checked_path(record)
        subprocess.run(['udevadm','trigger','--action=change','--settle',
                        '/sys/class/hidraw/'+target.name],check=True,timeout=3,capture_output=True)


# --- A device that was enumerated again -------------------------------------------------------------------
# Every pin of a node lives in four places: the plan file the relay and the side wrappers read, the copy of the plan
# in transaction.json that recovery restores from, the journal of the discovery rule and the rule itself (its
# DEVPATH names the node). When a suspend makes a device come back as another node, a relay moves the pin of that
# slot (native-deck-plan.py moved) in all four before it serves the new node, so that every later check, enrolling
# another slot, a restarted side, routing and recovery included, sees one answer. Each step can be done again.

INSTANCE=re.compile(r'deck|controller|controller-[a-f0-9]{8}')


@contextmanager
def recovery_lock(root, timeout=20):
    """recovery.lock, the lock every operation that rewrites the trial's ledgers takes; waits at most `timeout` seconds."""
    fd=os.open(root/'recovery.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        end=time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=end:raise RuntimeError('another operation holds the trial lock; the pins stay as they are')
                time.sleep(.1)
        yield
    finally:os.close(fd)


def replace_records(devices, pairs):
    """Put the new record in place of each old one in the list `devices`. True if anything changed."""
    changed=False
    for index,record in enumerate(devices):
        for old,new in pairs:
            if record['path']==old['path'] and record['identity']==old['identity'] and record!=new:
                devices[index]=new;changed=True
    return changed


def write_json(path, value, indent=None):
    temporary=path.with_name('.'+path.name+'.repin')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=indent)
        if indent:stream.write('\n')
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def trusted_file(path):
    st=path.lstat()
    if st.st_uid or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
        raise PermissionError('untrusted native pin file '+path.name)


def old_in_transaction(root, instance, old):
    """Whether the plan of the instance in transaction.json still holds this record, exactly."""
    path=root/'transaction.json'
    trusted_file(path)
    plan=plan_in_transaction(json.loads(path.read_text()),instance)
    return isinstance(plan,dict) and old in plan.get('devices',[])


def same_node(first, second):
    """Whether two records name the same node of the same slot: the same path, identity and slot. The rest of a record (its mode,
    owner and ACL) is what the node had when the record was made, and the discovery rule changes that."""
    return all(first.get(key)==second.get(key) for key in ('path','identity','slot'))


def repin_rule(root, pairs, instance):
    """Point the discovery rule and its journal at the new nodes. True if it wrote the rule or its journal.

    The journal keeps one record for each slot, under the path of its node. Two slots can come back with each other's
    numbers (receivers that are enumerated again in the other order), or one can take a number that another still has
    on record (a shift by one), so a move must not depend on what the new path held. A slot's move is accepted when
    - the record of the slot is the old one;
    - the record of the slot is the new node already: an attempt that did not get to the end left it there (below);
    - the slot has no record, because the move of another slot took the path of its record, and the plan of the instance
      in transaction.json still holds the old record exactly.
    Another slot's record at the new path is replaced if its node is not there any more, and refused if it is. A record of
    the same slot with any other identity stops everything, as it always did.

    An attempt that did not get to the end (udevadm was slower than its limit, the unit was stopped) leaves the move in the
    journal and the old pin in the plan and in transaction.json. The next attempt finds the node of the slot again and
    makes its record from the node as it is by then: locked down by the rule, so with another mode, owner and ACL than it had
    before. The journaled record has the ones from before, which recovery restores, so that is the record the plan and the
    transaction get: it replaces `new`, in place. The rest of the attempt is done again: the rule is written if it is not
    what the journal says, udev reloads it and the new nodes are triggered.
    """
    journal=root/'native-hid-udev.json'
    if not journal.exists():return False        # this trial never installed the rule
    native=module('native_plan','native-deck-plan.py')
    path,saved=checked_rule(root,missing=True,pending=True)
    devices=dict(saved['devices']);resumed=False
    for old,new in pairs:
        slot=old.get('slot')
        mine={key:record for key,record in devices.items() if (record.get('slot')==slot if slot else key==old['path'])}
        if any(key!=record['path'] or not (record==old or same_node(record,new)) for key,record in mine.items()):
            raise RuntimeError('the discovery rule does not hold the enrolled node '+old['path'])
        recorded=mine.get(new['path'])
        if recorded is not None and same_node(recorded,new):
            new.clear();new.update(recorded);resumed=True        # moved already
        elif not mine and not old_in_transaction(root,instance,old):
            raise RuntimeError('the discovery rule does not hold the enrolled node '+old['path'])
        if devices.get(old['path'])==old:del devices[old['path']]
        other=devices.get(new['path'])
        if other is not None and other!=new and native.node_state(other)=='ok':
            raise RuntimeError('the discovery rule holds another enrolled node at '+new['path'])
        devices[new['path']]=new
    lines=['# Owned by '+root.name+'; remove through trial recovery.']
    for target,record in sorted(devices.items()):
        try:lines.append(rule_line(native,record))
        except (FileNotFoundError,RuntimeError):continue    # a node that is gone has no line to keep
    content=('\n'.join(lines)+'\n').encode()
    record=dict(path=str(path),sha256=hashlib.sha256(content).hexdigest(),devices=devices)
    complete=saved==record and path.exists() and path.read_bytes()==content
    if complete and not resumed:return False
    if not complete:
        pending=dict(record)
        if path.exists():pending['previous_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        d.save_delivery_ledger(journal,pending)
        temporary=root/'native-hid-udev.rules.next'
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
        with os.fdopen(fd,'wb') as output:output.write(content);output.flush();os.fsync(output.fileno())
        temporary.replace(path)
        d.save_delivery_ledger(journal,record)
        checked_rule(root)
    subprocess.run(['udevadm','control','--reload-rules'],check=True,timeout=3,capture_output=True)
    for _,new in pairs:
        subprocess.run(['udevadm','trigger','--action=change','--settle','/sys/class/hidraw/'+Path(new['path']).name],
                       check=True,timeout=3,capture_output=True)
    return not complete


def plan_in_transaction(transaction, instance):
    """The copy of the instance's plan in transaction.json, or None."""
    if instance=='deck':return transaction.get('native_deck_plan')
    if instance=='controller':return transaction.get('native_controller_plan')
    entry=transaction.get('native_extra_controllers',{}).get(instance.removeprefix('controller-'))
    return entry.get('plan') if isinstance(entry,dict) else None


def repin_transaction(root, pairs, instance):
    path=root/'transaction.json'
    trusted_file(path)
    transaction=json.loads(path.read_text())
    plan=plan_in_transaction(transaction,instance)
    if not plan or not replace_records(plan['devices'],pairs):return False
    write_json(path,transaction,indent=2)
    return True


def repin_plan_file(root, pairs, instance):
    path=root/'bundle/libexec'/('native-'+instance+'-plan.json')
    trusted_file(path)
    plan=json.loads(path.read_text())
    if not replace_records(plan['devices'],pairs):return False
    write_json(path,plan)
    return True


def repin(root, instance, pairs, timeout=20):
    """Make the new nodes of re-enumerated slots the pinned ones, in all four places and in this order: the rule and
    its journal, the plan in transaction.json, the plan file. `pairs` is [(old record, new record)] from moved().
    Returns what it changed. Nothing is served from a node that this has not made the pinned one.

    A new record that the journal of the rule holds already (an attempt that did not get to the end journaled it) is replaced,
    in place, by the journaled one (repin_rule), so that the caller goes on with the record the plan and the transaction got."""
    if not pairs:return {}
    if not INSTANCE.fullmatch(instance):raise ValueError('invalid native instance')
    for old,new in pairs:
        if not re.fullmatch(r'/dev/hidraw[0-9]+',old['path']) or not re.fullmatch(r'/dev/hidraw[0-9]+',new['path']):
            raise ValueError('only hidraw nodes are moved')
        if not old.get('slot') or old.get('slot')!=new.get('slot'):
            raise ValueError('the new node does not fill the slot of the old one')
    with recovery_lock(root,timeout):
        return {'rule':repin_rule(root,pairs,instance),'transaction':repin_transaction(root,pairs,instance),
                'plan':repin_plan_file(root,pairs,instance)}


def remove_source_rule(root):
    if not (root/'native-hid-udev.json').exists():return
    path,record=checked_rule(root,missing=True,pending=True)
    path.unlink(missing_ok=True)
    subprocess.run(['udevadm','control','--reload-rules'],check=True,timeout=3,capture_output=True)
    native=module('native_plan','native-deck-plan.py')
    # Restore udev's normal tags before the recovery caller restores the exact
    # saved ACLs. Never replay rules on a reused node after unplug/reconnect.
    for device in record['devices'].values():
        try:target=native.checked_path(device)
        except (FileNotFoundError,RuntimeError):continue
        subprocess.run(['udevadm','trigger','--action=change','--settle',
                        '/sys/class/hidraw/'+target.name],check=True,timeout=3,capture_output=True)


def notify(root, relay, settle=True):
    """Announce the enrolled HID node again (udev add), so that Steam looks at its path. settle=False does not wait for udev
    to finish with the event: a router that repeats this every two seconds for a controller Steam has not opened yet must
    not stand still for it."""
    target=relay['target']
    if not re.fullmatch('/dev/hidraw[0-9]+',target):raise ValueError('invalid HID notification')
    _,record=checked_rule(root)
    module('native_plan','native-deck-plan.py').checked_path(record['devices'][target])
    # HID discovery reacts to add/remove; change alone can leave a released
    # controller absent forever. This reannounces only the enrolled HID node.
    subprocess.run(['udevadm','trigger','--action=add',*(['--settle'] if settle else []),'/sys/class/hidraw/'+Path(target).name],
                   check=True,timeout=3,capture_output=True)


# --- The rescan poke -----------------------------------------------------------------------------------------------------
# Steam's 64-bit beta client (steamdeck_publicbeta) runs in a pressure-vessel container. In a container SDL does not use udev
# for its HIDAPI devices (its sandbox detection finds /.flatpak-info, the SNAP variables or /run/host/container-manager): it
# watches /dev with inotify (IN_CREATE | IN_DELETE | IN_MOVE | IN_ATTRIB), and an event counts as a change of the hidraw
# devices only when the name is "hidraw" and decimal digits and nothing else (StrHasPrefix "hidraw", StrIsInteger). That is
# src/hidapi/SDL_hidapi.c of SDL release-2.30.5, release-2.32.10, release-3.2.30 and release-3.4.18, which all agree.
#
# So the udev add that notify() sends does not reach that client, and neither does a chown or a mount of the node: neither
# makes an entry in the directory it watches. After a relay was started again it never looked at /dev/hidrawN again (Deck,
# 2026-10-22, preview23). It did when a file named hidraw63 was made in its /dev and removed. The stable client uses udev and
# needs nothing, but a poke does it no harm.
#
# The poke is such a file: made in the /dev of the consumer's mount namespace as the side's account, empty, mode 0600, with
# O_CREAT|O_EXCL|O_NOFOLLOW, and removed at once. The filter takes only "hidraw" and digits, so the name cannot differ from
# a node's in its shape. It differs in its number: a hidraw node has a minor from 0 to 63 (the kernel's HIDRAW_MAX_DEVICES is
# 64, and native-deck-plan.py RELAY_MINORS agrees), and the poke's name has nine digits, the first not 0, which is 100000000
# or more. No node of the kernel has such a name.
#
# A container's /dev is a tmpfs of the container's user namespace, and a file can be made in it only with ids that map there:
# host root's do not (EOVERFLOW, seen on the Deck). The helper therefore takes the account's ids for files first, as they are
# on the host, which is what the kernel compares. That is enough where the account owns /dev, as it does in a container the
# account made itself (native-hid-poke-namespace-test.py). If the mode of /dev refuses the create all the same (EACCES, EPERM) and
# the process has a user namespace of its own, the helper joins it, after the ids, and tries once more: it has the capabilities
# there that a /dev the account cannot write by its mode needs. In the other order the ids would be read in the container's
# namespace, where the account's number may be another one or none. EOVERFLOW is not a refusal that a join cures: the kernel
# checks the ids of a new file against the user namespace of the filesystem, whichever namespace the caller is in, so the retry
# would end the same way. It ends the poke. The helper enters the mount namespace and root of the process first and refuses the
# host's, so it cannot make a file in the host's /dev.
#
# Delivery notes the namespaces in which Steam has to open the relay's node again (served): where it made the node usable (a
# mount, a repair, a pending entry made final), and where it finds an earlier node of the relay while the live relay is not the
# one it found there before. That is every restart of a numbered relay, not only the first: devtmpfs resets the node it removes,
# which is the live relay's, so the node in Steam's namespace needs a repair after the first restart and none after the next
# ones, and Steam's handle on it is dead each time. The router notes the namespaces of the side that a controller is handed to
# as well (recovered_family), whatever the relay's number: each change of owner revokes the handles of the relay and refuses an
# open by anyone else, so a Steam that held the mount already has lost its handle, and gets from delivery nothing but the udev
# add. The router calls rescan_poke where it announces the controller, while Steam has not opened it: once for each recovery,
# the first time it is asked after it (a pad that was asleep when its relay came back is announced when it wakes, which may be
# long after); once more if it is asked again 1.5 s later and within 10 s of that poke. One consumer is poked at most three times
# a minute for restarts and repairs (a relay that keeps failing, an account that keeps changing its node) and at most twelve
# times a minute for handovers (each one an owner change that the router made). A first poke that the limit holds back waits
# for room; a retry that it holds back is not made. A poke whose helper did not finish (it ran out of time, a signal killed it, or
# it could not be started for want of memory) decided nothing about the namespace: it is due again, POKE_RETRY_AFTER seconds later.
# It counts against the limit like any other poke, and no consumer has more than POKE_LIMIT of them in POKE_PERIOD seconds
# whatever the kind of its recovery, so a helper that never finishes costs this thread POKE_TIMEOUT seconds three times a minute
# for each consumer at most. Any other failure is final: what the helper itself reports (EOVERFLOW, the ids refused, every name
# taken) and Steam having changed. It is journaled and nothing else. A consumer is remembered until its process is gone. Delivery
# does not depend on any of this. A helper that is killed between the create and the unlink leaves the empty file in the
# consumer's /dev; no node of the kernel has its name, and later pokes use other names.

POKE_NAME=re.compile('hidraw[1-9][0-9]{8}')
POKE_NAMES=3            # names handed to the helper for one poke, in case a name is taken
POKE_RETRY_AFTER=1.5    # seconds after a poke before the one retry may follow (the router asks every 2 s)
POKE_WINDOW=10.0        # seconds after the first poke of a recovery in which the one retry may follow
POKE_LIMIT=3            # pokes of one consumer in POKE_PERIOD seconds for its relay's restarts and repairs, retries included
POKE_HANDOVER_LIMIT=12  # pokes of one consumer in POKE_PERIOD seconds for controllers handed to it, retries included
POKE_PERIOD=60.0
POKE_TIMEOUT=2          # seconds the helper may take: the thread that asks also keeps the row of every other controller fresh
POKE_REFUSED=(errno.EACCES,errno.EPERM)       # what joining the user namespace may cure: the mode of /dev, not an id that does not map
CLONE_NEWUSER=0x10000000
_POKES={}               # (pid, start time, mount namespace, target) of a consumer -> its last recovery and its pokes
_SEEN={}                # the same key -> the identity of the live relay when delivery last served that namespace
_POKE_NUMBERS=itertools.count(int.from_bytes(os.urandom(4),'big'))


def poke_names(count=POKE_NAMES):
    """Names for the file of one poke, none of them used before by this process: 'hidraw' and nine digits, the first not 0."""
    return ['hidraw%d'%(100_000_000+next(_POKE_NUMBERS)%900_000_000) for _ in range(count)]


def poke_create(dirfd, names):
    """Make the first of the names that is free in the directory dirfd as an empty regular file with mode 0600, and remove it.

    Returns its name. Nothing that is there is followed or touched: a name that is taken (by a file, a link, a directory, anything;
    O_EXCL refuses a link whatever it points at, and does not open what it finds) is passed over for the next, and the file is
    removed by its name only while that name still is the file that was made. FileExistsError when every name is taken,
    ValueError for a name that is not one of poke_names' shape (nothing is made then). Between the look at the name and the
    unlink there is a gap that only a process that can write in this directory can use, and it can lose no more than a name it
    put there itself.
    """
    if not names or not all(POKE_NAME.fullmatch(name) for name in names):raise ValueError('invalid poke name')
    for name in names:
        try:fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=dirfd)
        except FileExistsError:continue
        try:
            made=os.fstat(fd);now=os.stat(name,dir_fd=dirfd,follow_symlinks=False)
            if (now.st_dev,now.st_ino)!=(made.st_dev,made.st_ino):raise RuntimeError(f'{name} was replaced; left alone')
            os.unlink(name,dir_fd=dirfd)
        finally:os.close(fd)
        return name
    raise FileExistsError(errno.EEXIST,'every name of the poke is taken')


def poke_in_dev(names):
    """poke_create in the /dev of the root this process has; the name that was made."""
    dirfd=os.open('/dev',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:return poke_create(dirfd,names)
    finally:os.close(dirfd)


class NoImports:
    """A finder that refuses every import, for as long as poke_main runs the helper, and in the process of the entry until it
    ends: the helper enters the root of the consumer, whose files the account can change. What it needs is loaded with this
    module, so it is in sys.modules and needs no finder; anything else would come from there."""
    @staticmethod
    def find_spec(name, path=None, target=None):raise ImportError(f'no import in the consumer\'s root: {name}')


def poke_helper(argv):
    """Make and remove the file of a poke in the /dev of a consumer's mount namespace as the side's account; (name, how).

    argv is MOUNT ROOT USER UID GID NAME...: descriptors of the mount namespace and the root of a process of the consumer and of
    its user namespace (-1 for none), the account, and the names of poke_names. All of it is checked before anything is entered:
    the descriptors are numbers from 3 up and differ, the account is a user (not root's, not a system account's) with a group
    that is not root's, and there are one to POKE_NAMES different names of the shape poke_create makes. ValueError otherwise.

    It enters the namespace and the root as the --attach helper does, drops every supplementary group, and takes the account's
    ids for files as that does; it checks that it has them. A filesystem that belongs to a user namespace takes a new file only
    if the ids it is made with map there, and host root's do not (EOVERFLOW). The ids are set first, as they are on the host,
    which is what the kernel compares, and the file is made with them: 'mount' says that this was enough. If the mode of /dev
    refuses that (POKE_REFUSED: EACCES, EPERM) and the process is in a user namespace other than the helper's, the helper joins
    that, after the ids and not before, and tries once more: 'mount+user'. In the user namespace it has the capabilities that a
    /dev the account cannot write by its mode needs. EOVERFLOW is not in POKE_REFUSED: the kernel compares the ids of the new file
    with the user namespace of the filesystem, not with the caller's, so a join cannot change it and it ends the poke as it is.
    A join that fails ends the poke with that error and the refusal that led to it. Everything it does after the root was entered
    needs no module that is not loaded already: the root is the container's, which may have no Python and whose files the
    account can change.
    """
    try:mount,rootfd,userfd,uid,gid=map(int,argv[:5])
    except ValueError:raise ValueError('invalid poke') from None
    names=list(argv[5:])
    held=[mount,rootfd,*([userfd] if userfd>=0 else [])]
    if (min(held)<3 or userfd<-1 or len(set(held))!=len(held) or uid<1000 or gid<1 or not names or len(names)>POKE_NAMES
            or len(set(names))!=len(names) or not all(POKE_NAME.fullmatch(name) for name in names)):
        raise ValueError('invalid poke')
    joining=False
    if userfd>=0:
        there=os.fstat(userfd);ours=os.stat('/proc/self/ns/user')
        joining=(there.st_dev,there.st_ino)!=(ours.st_dev,ours.st_ino)
    d.enter_mount_namespace(mount,rootfd)
    os.umask(0o077);os.setgroups([])
    d.LIBC.setfsgid(gid);d.LIBC.setfsuid(uid)
    if d.LIBC.setfsgid(-1)!=gid or d.LIBC.setfsuid(-1)!=uid:raise PermissionError(f'could not take the ids {uid}:{gid}')
    try:return poke_in_dev(names),'mount'
    except OSError as refused:
        if not joining or refused.errno not in POKE_REFUSED:raise
        if d.LIBC.setns(userfd,CLONE_NEWUSER)!=0:
            error=ctypes.get_errno()
            raise OSError(error,f'joining the user namespace: {os.strerror(error)} (after: {refused})') from refused
    return poke_in_dev(names),'mount+user'


def poke_main(argv, keep=False):
    """The --poke entry: 'NAME HOW' on stdout and status 0, or one line on stderr and status 1 (the router journals that line).
    No module is loaded while the helper runs (NoImports).

    keep leaves the finder in place when the helper is done: the process of the entry (below) ends with this call, still in the
    root of the consumer, and what it does until then (the answer, the report of an exception that is not an Exception, the end
    of the interpreter) must not load anything either. Without keep the finder is taken out again, for a caller that goes on.
    """
    sys.meta_path.insert(0,NoImports)
    try:name,how=poke_helper(argv)
    except Exception as error:
        print(f'{type(error).__name__}: {error}',file=sys.stderr);return 1
    finally:
        if not keep:sys.meta_path.remove(NoImports)
    print(name,how);return 0


def journal_poke(record):
    """Write the record of a poke to the journal and return it. One that cannot be written is lost and nothing else happens."""
    try:print(json.dumps({'native_rescan_poke':record}),flush=True)
    except Exception:pass
    return record


def unfinished(error):
    """Whether the helper did not get to do its work or to say how it went: it ran out of time (POKE_TIMEOUT), a signal killed it
    (the system is short of memory), or it could not be started for want of memory or processes. That says nothing about the
    namespace. What the helper itself reports (EOVERFLOW, the ids refused, every name taken) and what stops the poke before the
    helper starts (Steam changed, a process that is gone) are final."""
    if isinstance(error,subprocess.TimeoutExpired):return True
    if isinstance(error,subprocess.CalledProcessError):return error.returncode<0
    return isinstance(error,OSError) and error.errno in (errno.ENOMEM,errno.EAGAIN)


def poke_namespace(process, pid, identity, relay, names, attempt):
    """One poke in the mount namespace of pid, the first process of that namespace; the record it journaled. Never raises.

    The descriptors of the namespace, the root and the user namespace of pid are opened first, and then the whole family is
    verified again (a pid can be given to another process, and these descriptors are the ones of whoever had it when they were
    opened: the check after them says that it was Steam). Only then is the helper started, with the descriptors and nothing else.

    The record has process, namespace, target, name (the one that was made, else the first that was to be tried), result ('ok',
    'failed: why') and attempt (1 or 2), and on success how the helper made the file ('mount': with the account's ids in the
    mount namespace; 'mount+user': it had to join the user namespace as well). A failure that says nothing about the namespace
    (unfinished) has again: True, which tells rescan_poke that the poke is still due.
    """
    record=dict(process=pid,namespace=identity[1],target=relay['target'],name=names[0],result='',attempt=attempt)
    try:
        uid=process[3];gid=pwd.getpwuid(uid).pw_gid
        with ExitStack() as stack:
            def keep(fd):stack.callback(os.close,fd);return fd
            mount=keep(os.open(f'/proc/{pid}/ns/mnt',os.O_RDONLY))
            rootfd=keep(os.open(f'/proc/{pid}/root',os.O_PATH|os.O_DIRECTORY))
            try:user=keep(os.open(f'/proc/{pid}/ns/user',os.O_RDONLY))
            except FileNotFoundError:user=-1       # (a kernel without user namespaces: there is none to join)
            unchanged(process,'Steam changed')
            done=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--poke',str(mount),str(rootfd),str(user),str(uid),
                                 str(gid),*names],pass_fds=tuple(fd for fd in (mount,rootfd,user) if fd>=0),check=True,
                                timeout=POKE_TIMEOUT,capture_output=True,text=True)
        said=done.stdout.split()
        if len(said)!=2 or said[0] not in names or said[1] not in ('mount','mount+user'):
            raise RuntimeError(f'the poke helper answered {done.stdout.strip()[:80]!r}')
        record.update(name=said[0],result='ok',how=said[1])
    except Exception as error:      # (a poke never stops delivery)
        helper=isinstance(error,(subprocess.CalledProcessError,subprocess.TimeoutExpired))
        record['result']='failed: '+(helper_failure(error) if helper else f'{type(error).__name__}: {error}')
        if unfinished(error):record['again']=True
    return journal_poke(record)


def alive(pid, identity):
    """Whether pid is still the process of this identity (its start time and mount namespace), read from /proc. Never raises."""
    try:
        start=(PROC/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]
        return (start,os.readlink(PROC/str(pid)/'ns'/'mnt'))==(identity[0],identity[1])
    except (OSError,IndexError,ValueError):return False


def forget_gone():
    """Forget the consumers whose process is gone (alive), in _SEEN and _POKES: what they hold is bounded by the live ones."""
    for table in (_SEEN,_POKES):
        for key in [key for key in table if not alive(key[0],key[1:3])]:del table[key]


def served(pid, identity, relay, found):
    """Note for rescan_poke what serve() found in the mount namespace of pid (recovered), and remember the live relay there.

    'delivered' and 'repaired' made the node usable now: a recovery. So is 'numbered', an earlier node of the relay that needed
    nothing, when the live relay is not the one delivery found when it last served this namespace, or delivery has not served it
    before: the relay was started again since, and whatever handle Steam had on the node is dead. devtmpfs resets only the node
    it removes, which is the live relay's, so the node in Steam's namespace needs a repair after the first restart of its relay
    and none after the next ones. 'mounted' is the live relay's own node. It never raises.
    """
    try:
        key=(pid,identity[0],identity[1],relay['target']);live=tuple(relay['identity'])
        if key not in _SEEN:forget_gone()
        seen=_SEEN.get(key);_SEEN[key]=live
        if found in ('delivered','repaired') or (found=='numbered' and seen!=live):recovered(pid,identity,relay)
    except Exception as error:
        journal_poke(dict(process=None,namespace=None,target=None,name=None,result=f'failed: {type(error).__name__}: {error}'))


def recovered(pid, identity, relay, handover=False):
    """Note that the node of the relay has just been made usable in the mount namespace of pid, or that its relay was started
    again (served), or with handover that the router hands the controller to the side whose namespace it is (recovered_family):
    rescan_poke may poke it.

    A recovery takes the place of the one before, poked or not; its kind decides which limit its pokes count against
    (rescan_poke). A consumer is forgotten only when its process is gone (forget_gone). It never raises: delivery has done its
    work when this is called.
    """
    try:
        key=(pid,identity[0],identity[1],relay['target'])
        state=_POKES.get(key)
        if state is None:
            forget_gone()
            state=_POKES[key]=dict(times=[],handed=[],lost=[],held=False)
        state.update(first=None,last=None,retry=True,handover=handover)
    except Exception as error:
        journal_poke(dict(process=None,namespace=None,target=None,name=None,result=f'failed: {type(error).__name__}: {error}'))


def recovered_family(process, relay):
    """recovered() for each mount namespace of the family, as a handover: the router hands the controller to this side.

    Delivery notes what it repaired or mounted, and a side that holds the relay's mount already gets nothing from it. Its Steam
    can need a poke all the same. Each change of owner (route) revokes every handle of the relay, and the relay refuses an open
    by anyone but the owner (EACCES), whatever its number: the Deck's own relay and a relay on a number of the kernel's choice
    as well. A controller that goes from Main to Guest and back finds Main's mount in place, its Steam without a handle, and
    no event for a Steam in a container to look at again. So the router notes the side it hands a controller to, before it
    switches the route: an owner change that the relay makes after route() stopped waiting for it is noted then too. It never
    raises.
    """
    try:
        for pid,identity in namespaces(process):recovered(pid,identity,relay,handover=True)
    except Exception as error:
        journal_poke(dict(process=None,namespace=None,target=None,name=None,result=f'failed: {type(error).__name__}: {error}'))


def poke_due(state, now):
    """Which poke of a recovery is due: 1 (the first), 2 (the one retry) or 0 (none).

    The first is due whenever it is asked for and there has been none, however long after the recovery: the router asks only
    while the controller is connected and Steam has not opened it, and a pad that was asleep when its relay came back is
    connected a long time later. A first poke that the limit holds back stays due, and so does one whose helper did not finish
    (rescan_poke leaves first at None), POKE_RETRY_AFTER seconds after it. The retry is due POKE_RETRY_AFTER seconds after the
    first poke, and no later than POKE_WINDOW seconds after it.
    """
    if state['first'] is None:return 1 if state['last'] is None or now-state['last']>=POKE_RETRY_AFTER else 0
    if state['retry'] and now-state['first']<=POKE_WINDOW and now-state['last']>=POKE_RETRY_AFTER:return 2
    return 0


def rescan_poke(process, relay):
    """Poke the consumers of this relay that have a recovery, as far as a poke is due; the records journaled.

    The router calls it where it announces the controller while Steam has not opened it. A consumer is poked once for each
    recovery (recovered), when this is first called after it, however long after (poke_due says why); once more if this is called
    again POKE_RETRY_AFTER seconds later, within POKE_WINDOW of that first poke. A consumer is poked at most POKE_LIMIT times in
    POKE_PERIOD seconds for restarts and repairs, and at most POKE_HANDOVER_LIMIT times for handovers, retries included. A
    first poke that the limit holds back waits: it is made when this is called once the limit has room, and journaled as held
    back once. A retry that the limit holds back is not made (journaled as skipped). A poke whose helper did not finish (its
    record says again) has decided nothing: a first poke is due again, and so is a retry within its window, after
    POKE_RETRY_AFTER seconds. It counts like any other, and no consumer has more than POKE_LIMIT of them in POKE_PERIOD seconds
    whatever the kind of its recovery (journaled as held back or skipped). Without a recovery it does nothing. It never raises.
    """
    if not _POKES:return []
    records=[]
    try:
        now=time.monotonic()
        for pid,identity in namespaces(process):
            state=_POKES.get((pid,identity[0],identity[1],relay['target']))
            attempt=poke_due(state,now) if state else 0
            if not attempt:continue
            window,limit=('handed',POKE_HANDOVER_LIMIT) if state['handover'] else ('times',POKE_LIMIT)
            for name in (window,'lost'):state[name]=[t for t in state[name] if now-t<POKE_PERIOD]
            full=(f'{limit} pokes' if len(state[window])>=limit else
                  f'{POKE_LIMIT} pokes that did not finish' if len(state['lost'])>=POKE_LIMIT else None)
            if full:
                if attempt==2:state['retry']=False
                elif state['held']:continue
                else:state['held']=True
                records.append(journal_poke(dict(process=pid,namespace=identity[1],target=relay['target'],name=None,attempt=attempt,
                    result=f'{"skipped" if attempt==2 else "held back"}: {full} in the last {POKE_PERIOD:g} s')))
                continue
            state[window].append(now);state['last']=now
            if attempt==1:state.update(first=now,held=False)
            else:state['retry']=False
            record=poke_namespace(process,pid,identity,relay,poke_names(),attempt)
            if record.get('again'):         # (the helper did not finish: nothing was decided in the namespace, so the poke is still due)
                state['lost'].append(now)
                if attempt==1:state['first']=None
                else:state['retry']=True
            records.append(record)
    except Exception as error:      # (whatever is wrong with what it was given, delivery goes on)
        records.append(journal_poke(dict(process=None,namespace=None,target=None,name=None,result=f'failed: {type(error).__name__}: {error}')))
    return records


if __name__=='__main__':
    if sys.argv[1:2]==['--poke']:raise SystemExit(poke_main(sys.argv[2:],keep=True))
    if len(sys.argv)!=8 or sys.argv[1]!='--attach':raise ValueError('private namespace helper only')
    mount,rootfd,source=map(int,sys.argv[2:5]);target=sys.argv[5];uid,gid=map(int,sys.argv[6:8])
    if not re.fullmatch('/dev/hidraw[0-9]+',target) or uid<1000:raise ValueError('invalid HID target')
    def attach():
        oldgid=d.LIBC.setfsgid(gid);olduid=d.LIBC.setfsuid(uid)
        try:
            fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600);os.close(fd)
        finally:d.LIBC.setfsuid(olduid);d.LIBC.setfsgid(oldgid)
        d.syscall(429,source,b'',-100,os.fsencode(target),4)
    d.namespace_main(mount,rootfd,attach)   # (ends the process; no import, no traceback in the account's root)
