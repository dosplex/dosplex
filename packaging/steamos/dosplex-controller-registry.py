#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Persistent controller preferences and an atomic, bounded live inventory."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
TARGETS = ('main', 'guest', 'unassigned')
# ADR-014 (amended 2026-11-16): a controller with no saved owner belongs to Main. Unassigned is a state the user chooses.
NEW_OWNER = 'main'
# ADR-014 (amended 2026-11-23): the first owner is decided when a controller is first awake, not when its receiver slot
# is first seen, and the second player gets the next one while they have none (first_owner). A record that still waits
# for that moment carries first=FIRST_PENDING and belongs to nobody; `default` AUTO_OWNER asks observe() for such a record.
AUTO_OWNER = 'auto'
FIRST_PENDING = 'pending'
MAX_INDEX = 999
INDEX_LOCK = 'index.lock'
FORGET_SUFFIX = '.forget'


def requested_target(record):
    """A preview never replaces the last confirmed owner on disk.

    The bounded lease also expires if the compositor crashes. A reboot or a
    malformed lease uses the confirmed assignment immediately.
    """
    target = record.get('target')
    if target not in TARGETS:
        raise ValueError('invalid controller target')
    preview = record.get('preview')
    if isinstance(preview, dict) and preview.get('target') in TARGETS:
        try:
            remaining = float(preview['expires']) - time.monotonic()
            if 0 < remaining <= 15 and preview['boot_id'] == BOOT_ID.read_text().strip():
                return preview['target']
        except (OSError, KeyError, TypeError, ValueError):
            pass
    return target


def first_owner(state, main_has, guest_has):
    """Who gets a controller that nobody has given an owner yet, at the moment it is first awake.

    The first one is Main's. While the second player is on the screen (every layout but Main alone) and has no
    controller that is awake, and Main has one, it is the second player's: a person who sits down with a second
    controller can sign in without anyone opening a menu. main_has and guest_has say whether a controller (or the Steam
    Deck's own controls) that is awake belongs to that player. What the user chooses in Devices is kept and always wins;
    this only answers for a controller that has no owner at all."""
    return 'guest' if state.get('layout') != 'single' and main_has and not guest_has else NEW_OWNER


def controller_id(info, uniq, phys):
    identity = [info.bustype, info.vendor, info.product, uniq or phys]
    if not identity[-1]:
        raise ValueError('controller has no stable identity')
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022 or st.st_size > 65536:
            raise ValueError('untrusted controller state')
        return json.loads(os.read(fd, 65537))
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out); out.flush(); os.fsync(out.fileno())
    os.replace(temp, path)


def valid_index(value):
    return type(value) is int and 1 <= value <= MAX_INDEX


@contextlib.contextmanager
def index_lock(directory):
    """Exclusive lock of the numbering in one directory, shared by every process that writes records there.

    The lock file is opened without following a link and must be a regular file of this user that no one else can write."""
    fd = os.open(Path(directory) / INDEX_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            raise ValueError('untrusted controller lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def used_indexes(directory, skip=None):
    """{number: key} of every controller record in the directory that has one (a record that cannot be read has none)."""
    used = {}
    for p in sorted(Path(directory).glob('*.json'))[:256]:
        if not re.fullmatch('[a-f0-9]{32}', p.stem) or p.stem == skip: continue
        try:
            number = read_json(p).get('index')
        except (OSError, ValueError, TypeError, AttributeError): continue
        if valid_index(number): used.setdefault(number, p.stem)
    return used


def save_record(directory, key, fields):
    """Write the record of a controller with its number and return the number.

    The number is the one the record already has; else the smallest free one. A record is removed only by forget_record,
    so a number is not given to another controller until its owner was forgotten, and the lock makes two processes that
    number at once agree. An existing record that cannot be read counts as no record. fields may not carry the number."""
    directory = Path(directory)
    path = directory / (key + '.json')
    with index_lock(directory):
        try:
            number = read_json(path).get('index')
        except (OSError, ValueError, TypeError, AttributeError):
            number = None
        if not valid_index(number):
            taken = used_indexes(directory, skip=key)
            number = next(n for n in range(1, MAX_INDEX + 2) if n not in taken)
            if number > MAX_INDEX: raise RuntimeError('controller numbers are used up')
        write_json(path, dict(fields, index=number))
    return number


def forget_record(directory, key):
    """Remove the record of a controller, which frees its number for the next new one; True if there was a record.

    Under the same lock as save_record, so that a record is either whole or gone. The record must be a regular file of
    this user that no one else can write (read_json says so): anything else is refused with ValueError and left alone.
    The owner the user chose is dropped from assignments.json as well; left there it would give the owner back to a
    controller that comes again as a new one."""
    if not re.fullmatch('[a-f0-9]{32}', key): raise ValueError('invalid controller id')
    directory = Path(directory)
    path = directory / (key + '.json')
    with index_lock(directory):
        try:
            read_json(path)
        except FileNotFoundError:
            return False
        except OSError as error:                 # (a link, or a file that cannot be opened: not ours to remove)
            raise ValueError('untrusted controller state') from error
        path.unlink()
        batch = directory / 'assignments.json'
        try:
            value = read_json(batch)
            if isinstance(value, dict) and isinstance(value.get('assignments'), dict) and key in value['assignments']:
                del value['assignments'][key]
                write_json(batch, value)
        except (OSError, ValueError, TypeError): pass
    return True


def wait_again_record(directory, key):
    """Make the record of a controller wait for its first owner again; its number, or None if there was no record.

    This is what forgetting means for a receiver slot of the running session: the session goes on holding the slot, so
    its record cannot go (a missing record reads as Main's). Under the lock, in one step: the owner the user chose is
    dropped from assignments.json, and the record is written anew as nobody's and waiting (FIRST_PENDING), with the
    smallest number that is free. The controller that next wakes on the slot gets its first owner like a new one.
    The record must be trusted as for forget_record; anything else is refused with ValueError and left alone."""
    if not re.fullmatch('[a-f0-9]{32}', key): raise ValueError('invalid controller id')
    directory = Path(directory)
    path = directory / (key + '.json')
    with index_lock(directory):
        try:
            record = read_json(path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ValueError('untrusted controller state') from error
        if not isinstance(record, dict): raise ValueError('untrusted controller state')
        batch = directory / 'assignments.json'
        try:
            value = read_json(batch)
            if isinstance(value, dict) and isinstance(value.get('assignments'), dict) and key in value['assignments']:
                del value['assignments'][key]
                write_json(batch, value)
        except (OSError, ValueError, TypeError): pass
        taken = used_indexes(directory, skip=key)
        number = next(n for n in range(1, MAX_INDEX + 2) if n not in taken)
        if number > MAX_INDEX: raise RuntimeError('controller numbers are used up')
        kept = {k: v for k, v in record.items() if k not in ('index', 'target', 'first', 'preview')}
        write_json(path, dict(kept, target='unassigned', first=FIRST_PENDING, index=number))
    return number


class Registry:
    def __init__(self, directory, inventory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            raise ValueError('untrusted controller directory')
        self.inventory = Path(inventory)
        self.devices = {}
        self.live_controls = {}
        # A preview or a handoff in assignments.json that was written before this process started belongs to an earlier
        # session (the confirmed owners in it are kept): see batch().
        self.started = time.time() - 2          # (the file clock runs a little behind this one)
        for p in sorted(self.directory.glob('*.json'))[:32]:
            if not re.fullmatch('[a-f0-9]{32}', p.stem): continue
            try:
                v = read_json(p)
                if v.get('target') not in TARGETS: continue
                number = v.get('index')
                if not valid_index(number):            # (a record from before the numbers: give it one now)
                    number = save_record(self.directory, p.stem,
                                         {k: x for k, x in v.items() if k != 'index'})
                self.devices[p.stem] = dict(id=p.stem, name=str(v.get('name', 'Controller'))[:120], index=number,
                    requested=requested_target(v), target='unassigned', connected=False, active=False, node='')
            except (OSError, ValueError, TypeError, AttributeError, RuntimeError): continue

    def observe(self, key, name, default=NEW_OWNER, **fields):
        """Note a controller that is connected. One with no saved record (or an unreadable one) gets one, owned by default.

        With default AUTO_OWNER the new record belongs to nobody and waits for its first owner (pending, decide)."""
        if key not in self.devices:
            if len(self.devices) >= 32: raise RuntimeError('controller inventory is full')
            if default == AUTO_OWNER:
                default, fields = 'unassigned', dict(fields, first=FIRST_PENDING)
            number = save_record(self.directory, key, dict(fields, name=name[:120], target=default))
            self.devices[key] = dict(id=key, name=name[:120], index=number, requested=default,
                target='unassigned', connected=True, active=False, node='')
        self.devices[key]['connected'] = True

    def pending(self, key):
        """Whether the controller still waits for its first owner: its record says so, and the user has chosen none."""
        try:
            if read_json(self.directory / (key + '.json')).get('first') != FIRST_PENDING: return False
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        try:
            return key not in self.batch().get('assignments', {})
        except (OSError, ValueError, TypeError, AttributeError):
            return True         # (no usable file is no choices)

    def decide(self, key, owner):
        """Give a controller that waited for it its first owner. The record keeps its number and no longer waits."""
        if owner not in TARGETS: raise ValueError('invalid controller target')
        record = read_json(self.directory / (key + '.json'))
        save_record(self.directory, key, dict({k: v for k, v in record.items() if k not in ('index', 'first')}, target=owner))
        if key in self.devices: self.devices[key]['requested'] = owner

    def forget_requests(self, connected, protected=(), session=()):
        """Forget the controllers that a `<id>.forget` file in the directory asks for; the ids that were forgotten.

        Whoever is root (the overlay, `dosplex control forget`) makes the file, so the one place that removes a record is
        this process, under the lock. A request is removed whatever happens to it. A controller that is in connected is
        refused, and so is one in protected: the user turns the controller off first. One in session (a receiver slot
        of the running session, which goes on holding it) keeps a record: it is made nobody's and waits for its first
        owner again (wait_again_record), so the controller that next wakes on it comes back as a new one. A request
        that is not a regular file of this user is ignored and removed."""
        done = []
        for p in sorted(self.directory.glob('*' + FORGET_SUFFIX))[:32]:
            key = p.name[:-len(FORGET_SUFFIX)]
            try:
                st = p.lstat()
                p.unlink()
                if (not re.fullmatch('[a-f0-9]{32}', key) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                        or st.st_mode & 0o022 or key in connected or key in protected): continue
                if key in session:
                    number = wait_again_record(self.directory, key)
                    if number is None: continue
                    if key in self.devices: self.devices[key].update(index=number, requested='unassigned')
                    done.append(key)
                    continue
                forget_record(self.directory, key)
                self.devices.pop(key, None)
                done.append(key)
            except (OSError, ValueError): continue
        return done

    def batch(self):
        """assignments.json without what an earlier session left in it.

        The confirmed owners stay (that is the memory of the user's choices). A preview and a handoff are short lived: one
        that the file got before this process started is dropped, and a preview expires by itself after 15 seconds."""
        path = self.directory / 'assignments.json'
        value = read_json(path)
        if not isinstance(value, dict): raise ValueError('invalid assignments')
        if os.stat(path, follow_symlinks=False).st_mtime < self.started:
            value = {k: x for k, x in value.items() if k not in ('preview', 'handoff')}
        return value

    def target(self, key):
        try:
            record = read_json(self.directory / (key+'.json'))
            try:
                batch = self.batch()
            except (OSError, ValueError, TypeError, AttributeError):
                batch = {}          # (no usable file is no choices: the record's owner stands)
            confirmed = batch.get('assignments', {}).get(key, record.get('target'))
            effective = dict(target=confirmed)
            preview = batch.get('preview')
            if isinstance(preview, dict) and key in preview.get('assignments', {}):
                effective['preview'] = dict(preview, target=preview['assignments'][key])
            elif 'preview' in record and not batch:
                effective['preview'] = record['preview']
            target = requested_target(effective)
        except (OSError, ValueError, TypeError, AttributeError):
            target = NEW_OWNER      # (no usable record is no record: Main's)
        self.devices[key]['requested'] = target
        return target

    def may_apply(self, key, current, target):
        """During a swap, make the incoming Main endpoint before removing the old.

        References are the live broker objects, not a published status snapshot.
        If creation of the incoming endpoint fails, Main keeps its old controller.
        """
        if current != 'main' or target == 'main':
            return True
        try:
            pair = self.batch().get('handoff', [])
            if key not in pair:
                return True
            return any(other != key and other in self.live_controls and
                       self.live_controls[other].target == 'main' and
                       self.live_controls[other].ui is not None for other in pair)
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def publish(self, controllers):
        # A controller that is off and whose record was forgotten (by another process, see forget_requests) is not listed.
        live = {c.key for c in controllers}
        for key in [k for k in self.devices if k not in live and not (self.directory / (k + '.json')).exists()]:
            del self.devices[key]
        for v in self.devices.values():
            v.update(connected=False, target='unassigned', active=False, node='')
        for c in controllers:
            v = self.devices[c.key]
            v.update(connected=True, target=c.target or 'unassigned', node=c.virtual_path or '',
                     active=time.monotonic()-c.last_pressed < 2)
            v['input_counts'] = getattr(c, 'input_counts', {})
            v['resync_count'] = getattr(c, 'resync_count', 0)
            gate = getattr(c, 'gate', None)
            opening = getattr(c, 'opening', None)
            v['input_status'] = dict(
                captured=bool(gate and gate.captured),
                draining=bool(gate and gate.draining),
                neutral=bool(gate and gate.neutral()),
                held_buttons=len(gate.keys) if gate else 0,
                off_center_axes=[code for code, (neutral, tolerance) in gate.neutral_axes.items()
                    if abs(gate.axes[code]-neutral)>tolerance] if gate else [],
                shortcut_ready=opening is not None,
                shortcut_pending=bool(opening and opening.pending is not None),
                shortcut_captured=bool(opening and opening.capturing),
                dpad_buttons=bool(getattr(c, 'dpad_keys', False)))
            if c.virtual_path:
                st = os.stat(c.virtual_path, follow_symlinks=False)
                v['identity'] = [st.st_dev, st.st_ino, st.st_rdev]
        write_json(self.inventory, dict(updated=time.time(), assignment_preview=2,
                                       devices=list(self.devices.values())))


def restore_permissions(path):
    import base64
    path = Path(path)
    if not path.exists(): return
    records = read_json(path)
    for name, record in list(records.items()):
        if not re.fullmatch(r'/dev/(input/event[0-9]+|hidraw[0-9]+)', name):
            raise ValueError('invalid recovery node')
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            st = os.fstat(fd)
            if [st.st_dev, st.st_ino, st.st_rdev] != record['identity']: continue
            os.fchown(fd, record['uid'], record['gid']); os.fchmod(fd, record['mode'])
            if record['acl'] is None:
                try: os.removexattr(fd, 'system.posix_acl_access')
                except OSError as e:
                    import errno
                    if e.errno not in (errno.ENODATA, errno.EOPNOTSUPP): raise
            else: os.setxattr(fd, 'system.posix_acl_access', base64.b64decode(record['acl']))
        finally: os.close(fd)
    path.unlink()


STEAM_MODE_PARAMETER = Path('/sys/module/hid_steam/parameters/lizard_mode')


def acquire_steam_mode(directory):
    """Opt-in lease for a session that owns all Steam-controller input paths."""
    journal = Path(directory) / 'steam-mode-recovery.json'
    if not STEAM_MODE_PARAMETER.exists():
        return False
    if not journal.exists():
        original = STEAM_MODE_PARAMETER.read_text().strip()
        if original not in ('Y', 'N'):
            raise ValueError('unknown Steam driver mode')
        write_json(journal, {'original': original})
    else:
        record = read_json(journal)
        if not isinstance(record, dict) or record.get('original') not in ('Y', 'N'):
            raise ValueError('invalid Steam mode recovery record')
    STEAM_MODE_PARAMETER.write_text('N\n')
    return True


def restore_steam_mode(directory):
    journal = Path(directory) / 'steam-mode-recovery.json'
    if not journal.exists():
        return
    record = read_json(journal)
    if not isinstance(record, dict) or record.get('original') not in ('Y', 'N'):
        raise ValueError('invalid Steam mode recovery record')
    if STEAM_MODE_PARAMETER.exists():
        STEAM_MODE_PARAMETER.write_text(record['original'] + '\n')
    journal.unlink()
