#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Private native Deck HID experiment. Not a hot-reassignment broker.

Plans pin live device identities and preserve ACLs. No protocol translation or
controller reports are recorded. Session wrappers expose HID only to one side.

A pin names a node (its inode), and a device that is enumerated again after a suspend gets another one. Each hidraw
record therefore also names its slot: the USB interface it sits on (port path and interface number), the USB ids
and the serial number, none of which depends on the node. moved() finds the new node of a slot, and a relay makes
that node the pinned one, durably, before it serves it (native-hid-delivery.py repin).

A controller relay's CUSE node has the device number 120:N, where N is the minor of the /dev/hidrawN it serves
(relay_number). The kernel opens a character device by its number, so a copy of an earlier relay's node, in any mount
namespace, opens the live relay that serves hidrawN, or fails when there is none (native-hid-delivery.py numbered).
"""
import base64
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

ACL = 'system.posix_acl_access'
# The character major of the relay numbers: one that the kernel never hands out on its own (dynamic majors are 234-254
# and 384-511). None turns the numbers off: relays then take a dynamic number and nothing counts as a numbered node.
RELAY_MAJOR = 120
RELAY_MINORS = 64     # N of /dev/hidrawN is the minor, and a relay number has the minors 0-63 (the helper checks it too)
RELAY_PREFIX = 'dosplex-hid-'
# A device that was enumerated again needs a few seconds to be there as a node. A relay waits this long for the node
# of its slot; its unit then starts it again.
SLOT_WAIT_SECONDS = 30


def relay_number(path):
    """(major, minor) of the node of the relay that serves this hidraw node: (RELAY_MAJOR, N) for /dev/hidrawN.

    ValueError for anything else: a path that is no hidraw node, an N of 64 or more, and every path when the numbers are
    off (RELAY_MAJOR is None). The caller then starts the relay on a dynamic number, and nothing counts as numbered."""
    if RELAY_MAJOR is None:
        raise ValueError('relay numbers are not in use')
    found = re.fullmatch(r'/dev/hidraw(0|[1-9][0-9]*)', str(path))
    if not found or int(found.group(1)) >= RELAY_MINORS:
        raise ValueError('no relay number for ' + str(path))
    return RELAY_MAJOR, int(found.group(1))


def foreign_major_owners(devices=Path('/proc/devices')):
    """The names under which a driver other than a relay holds the character major of the relay numbers; [] if none
    does (and when the numbers are off). Only the 'Character devices' part of /proc/devices counts: a block driver
    may hold the same number, and it does not matter here."""
    if RELAY_MAJOR is None:
        return []
    owners, characters = [], False
    for line in Path(devices).read_text().splitlines():
        if line.startswith('Character devices'):
            characters = True
        elif line.startswith('Block devices'):
            characters = False
        elif characters:
            number, _, name = line.strip().partition(' ')
            name = name.strip()
            if number.isdecimal() and int(number) == RELAY_MAJOR and name and not name.startswith(RELAY_PREFIX):
                owners.append(name)
    return owners


def identity(path):
    st = path.lstat()
    if not stat.S_ISCHR(st.st_mode):
        raise ValueError('expected character device: ' + str(path))
    return [st.st_dev, st.st_ino, st.st_rdev]


def deck_hids(sys_root=Path('/sys'), dev_root=Path('/dev')):
    groups = {}
    for entry in sorted((sys_root / 'class/hidraw').glob('hidraw*')):
        device = (entry / 'device').resolve(strict=True)
        # Match the complete physical USB device, not its display name. Composite
        # HID interface suffixes can change when hid_steam rebinds the device.
        for parent in (device, *device.parents):
            try:
                vendor = (parent / 'idVendor').read_text().strip()
                product = (parent / 'idProduct').read_text().strip()
            except FileNotFoundError:
                continue
            if (vendor, product) == ('28de', '1205'):
                groups.setdefault(str(parent), []).append(dev_root / entry.name)
            break
    if len(groups) != 1:
        raise RuntimeError('expected exactly one physical USB Deck controller')
    paths = next(iter(groups.values()))
    if len(paths) != 3:
        raise RuntimeError('expected three Deck HID interfaces; inspect this hardware first')
    return paths


def create(side, uid):
    return create_for_devices(side, uid, [*deck_hids(), Path('/dev/uinput')])


def slot_of(path, sys_root=Path('/sys')):
    """What names the device behind a hidraw node and stays the same when it is enumerated again: the USB interface
    it sits on (its place in the device tree, which holds the port, and its number), the USB ids and the serial
    number. Raises OSError or ValueError for a node that is not a USB interface (nothing to follow then)."""
    name = Path(path).name
    base = Path(sys_root).resolve()
    device = (base / 'class/hidraw' / name / 'device').resolve(strict=True)
    for interface in (device, *device.parents):
        if (interface / 'bInterfaceNumber').is_file():
            break
    else:
        raise ValueError('no USB interface behind ' + name)

    def read(directory, file):
        try:
            return (directory / file).read_text().strip() or None
        except OSError:
            return None
    usb = interface.parent
    return {'interface': str(interface.relative_to(base)), 'number': read(interface, 'bInterfaceNumber'),
            'vendor': read(usb, 'idVendor'), 'product': read(usb, 'idProduct'), 'serial': read(usb, 'serial')}


def find_slot(slot, sys_root=Path('/sys'), dev_root=Path('/dev')):
    """The hidraw node of the device that fills this slot now, or None. RuntimeError if several nodes claim it."""
    found = []
    for entry in sorted((Path(sys_root) / 'class/hidraw').glob('hidraw*')):
        node = Path(dev_root) / entry.name
        try:
            if slot_of(node, sys_root) == slot:
                found.append(node)
        except (OSError, ValueError):
            continue
    if len(found) > 1:
        raise RuntimeError('several native HID nodes fill the slot ' + str(slot.get('interface')) + ': '
                           + ', '.join(map(str, found)))
    return found[0] if found else None


def device_record(path):
    """The plan's record of one node: its identity, its permissions as they are now and, for a hidraw node on a USB
    interface, its slot."""
    path = Path(path)
    if not re.fullmatch(r'/dev/(hidraw[0-9]+|uinput)', str(path)):
        raise ValueError('unexpected native device path')
    st = path.lstat()
    pinned = identity(path)
    try:
        acl = base64.b64encode(os.getxattr(path, ACL)).decode('ascii')
    except OSError as error:
        if error.errno != errno.ENODATA:
            raise
        acl = None
    if identity(path) != pinned:
        raise RuntimeError('device changed while making plan')
    record = {'path': str(path), 'identity': pinned, 'mode': stat.S_IMODE(st.st_mode),
              'uid': st.st_uid, 'gid': st.st_gid, 'acl': acl}
    if path.name.startswith('hidraw'):
        try:
            record['slot'] = slot_of(path)
        except (OSError, ValueError):
            pass   # not a USB interface: there is no slot to follow
    return record


def create_for_devices(side, uid, paths):
    if side not in ('left', 'right') or uid < 1000:
        raise ValueError('invalid side owner')
    return {'schema': 1, 'side': side, 'uid': uid, 'devices': [device_record(path) for path in paths]}


def checked_path(record):
    path = Path(record['path'])
    if not re.fullmatch(r'/dev/(hidraw[0-9]+|uinput)', str(path)):
        raise ValueError('unexpected native device path')
    if identity(path) != record['identity']:
        raise RuntimeError('native controller node replaced: ' + str(path))
    return path


def node_state(record):
    """'ok' when the pinned node is there and is the pinned device, 'gone' when it is not there, 'other' when the
    path holds another node now."""
    try:
        current = identity(Path(record['path']))
    except FileNotFoundError:
        return 'gone'
    except (OSError, ValueError):
        return 'other'
    return 'ok' if current == record['identity'] else 'other'


def udev_settle():
    """Wait (5 s at most) until udev has finished with the node that just appeared. The record made of a node holds
    the permissions it has at that moment, and recovery restores them: a node taken before udev gave it its ACL
    would be restored without it."""
    try:
        subprocess.run(['udevadm', 'settle', '--timeout=5'], check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def moved(plan, *, wait=SLOT_WAIT_SECONDS, clock=time.monotonic, sleep=time.sleep, settle=udev_settle,
          sys_root=Path('/sys'), dev_root=Path('/dev')):
    """The hidraw records of the plan whose device was enumerated again, as [(old record, new record)].

    A record qualifies when its pinned node is gone or holds another node and the record names its slot. Then the
    node of that slot is waited for, at most `wait` seconds in all (a device that resumes needs a few seconds), and
    the new record is made from it. The old records are the plan's own objects. A record that is fine, has no slot,
    or whose slot has no node by the deadline is left out: checked_path then says what is wrong with it, as ever.
    Nothing is changed here; repin() makes the new pins durable.
    """
    waiting = {id(record): record for record in plan['devices']
               if re.fullmatch(r'/dev/hidraw[0-9]+', record['path']) and record.get('slot')}
    pairs = []
    deadline = clock() + wait
    while waiting:
        for key, record in list(waiting.items()):
            state = node_state(record)
            if state == 'ok':
                del waiting[key]
                continue
            node = find_slot(record['slot'], sys_root, dev_root)
            if node is None:
                continue
            settle()
            new = device_record(node)
            if new.get('slot') != record['slot']:
                continue
            pairs.append((record, new))
            del waiting[key]
        if not waiting or clock() >= deadline:
            break
        sleep(.25)
    return pairs


def lock_down(records):
    """Keep Steam off the physical nodes of re-pinned records the way grant() does for an enrolled relay: the
    ACL cleared and the mode 0600, root's. The node is opened first and checked against its pin."""
    for record in records:
        path = checked_path(record)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if [st.st_dev, st.st_ino, st.st_rdev] != record['identity']:
                raise RuntimeError('device changed during lock down')
            subprocess.run(['setfacl', '-b', '/proc/self/fd/' + str(fd)], pass_fds=(fd,), check=True, timeout=5)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def grant(plan):
    # Caller must durably save the complete plan before the first ACL mutation.
    relay_target = plan.get('relay', {}).get('target')
    if plan.get('relay') and relay_target not in {
            record['path'] for record in plan['devices'] if record['path'].startswith('/dev/hidraw')
    }:
        raise ValueError('native relay target %s is not in the saved device plan (hidraw paths in it: %s)' % (
            relay_target, ', '.join(sorted(record['path'] for record in plan['devices']
                                           if record['path'].startswith('/dev/hidraw'))) or 'none'))
    for record in plan['devices']:
        path = checked_path(record)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if [st.st_dev, st.st_ino, st.st_rdev] != record['identity']:
                raise RuntimeError('device changed during grant')
            if plan.get('relay') and str(path) != relay_target and path.name.startswith('hidraw'):
                # Steam sees only the mediated endpoint. Restore exact original
                # ACLs from the saved plan after all trial clients have stopped.
                subprocess.run(['setfacl', '-b', '/proc/self/fd/' + str(fd)],
                               pass_fds=(fd,), check=True, timeout=5)
                os.fchmod(fd, 0o600)
            elif plan.get('relay') and str(path) == relay_target:
                # The relay owns the physical source through its inherited root
                # descriptor. No Steam client may retain a direct HID handle.
                subprocess.run(['setfacl', '-b', '/proc/self/fd/' + str(fd)],
                               pass_fds=(fd,), check=True, timeout=5)
                os.fchmod(fd, 0o600)
            else:
                subprocess.run(['setfacl', '-m', 'u:' + str(plan['uid']) + ':rw', '/proc/self/fd/' + str(fd)],
                               pass_fds=(fd,), check=True, timeout=5)
        finally:
            os.close(fd)


def restore(plan):
    for record in plan['devices']:
        path = Path(record['path'])
        try:
            checked_path(record)
        except (FileNotFoundError, RuntimeError):
            # The original device disappeared. Never change a reused node.
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if [st.st_dev, st.st_ino, st.st_rdev] != record['identity']:
                raise RuntimeError('device changed during restore')
            os.fchown(fd, record['uid'], record['gid'])
            os.fchmod(fd, record['mode'])
            if record['acl'] is None:
                import errno
                try:
                    os.removexattr(fd, ACL)
                except OSError as error:
                    if error.errno != errno.ENODATA:
                        raise
            else:
                os.setxattr(fd, ACL, base64.b64decode(record['acl'], validate=True))
        finally:
            os.close(fd)


def bindings(plan_path, side):
    if not plan_path.exists():
        return []
    for parent in (plan_path, *plan_path.parents):
        st = parent.lstat()
        if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
            raise PermissionError('native input plan requires root-owned ancestors')
    plan = json.loads(plan_path.read_text())
    if plan['schema'] != 1 or plan['side'] not in ('left', 'right'):
        raise ValueError('invalid native input plan')
    if side != plan.get('routing_side',plan['side']):
        return []
    owner_uid=plan.get('routing_uid',plan['uid'])
    if owner_uid != os.getuid():
        raise PermissionError('native input plan owner mismatch')
    args = []
    for record in plan['devices']:
        path = checked_path(record)
        target = path
        if plan.get('relay') and path.name.startswith('hidraw'):
            relay = plan['relay']
            if str(path) != relay['target']:
                continue
            path = Path(relay['path'])
            if not re.fullmatch(r'/dev/dosplex-hid-[a-f0-9]{32}', str(path)) or identity(path) != relay['identity']:
                raise RuntimeError('native relay endpoint replaced')
            st = path.lstat()
            if st.st_uid != owner_uid or stat.S_IMODE(st.st_mode) != 0o600:
                raise PermissionError('native relay ownership changed')
        if not os.access(path, os.R_OK | os.W_OK):
            raise PermissionError('native input node inaccessible')
        args += ['--dev-bind', str(path), str(target)]
    return args
