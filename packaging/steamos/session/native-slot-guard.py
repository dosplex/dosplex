#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Retained-trial containment of one receiver slot's kernel input devices.

This is a polling containment aid, not a race-free hotplug ownership policy.
Grabs disappear when this process exits; no ACL or persistent host changes.
Never matches Steam-created virtual devices or the built-in Deck controller.
"""
import argparse
import errno
import fcntl
import os
from pathlib import Path
import stat
import time


def slot_events(interface, sysroot=Path('/sys')):
    if not interface.is_dir():
        return []
    usb = interface.parent
    try:
        identity = ((usb/'idVendor').read_text().strip(), (usb/'idProduct').read_text().strip())
    except FileNotFoundError:
        return []
    if identity not in (('28de', '1304'), ('28de', '1305')):
        raise RuntimeError('receiver identity changed')
    found = []
    for entry in (sysroot/'class/input').glob('event*'):
        device = (entry/'device').resolve()
        if interface in device.parents:
            found.append(entry.name)
    return found


def run(interface):
    held = {}
    try:
        while True:
            current = set(slot_events(interface))
            for name, (fd, identity) in list(held.items()):
                try:
                    st = Path('/dev/input', name).stat()
                    valid = name in current and (st.st_dev, st.st_ino, st.st_rdev) == identity
                except FileNotFoundError:
                    valid = False
                if not valid:
                    os.close(fd); del held[name]
            for name in current - held.keys():
                try:
                    fd = os.open('/dev/input/'+name, os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC|os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue  # Kernel removed the node between discovery/open.
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISCHR(st.st_mode):
                        raise RuntimeError('not an input device')
                    if name not in slot_events(interface):
                        os.close(fd); continue
                    fcntl.ioctl(fd, 0x40044590, 1)  # EVIOCGRAB
                    held[name] = (fd, (st.st_dev, st.st_ino, st.st_rdev))
                    print('contained '+name, flush=True)
                except OSError as error:
                    os.close(fd)
                    if error.errno not in (errno.ENODEV, errno.ENOENT):raise
                except BaseException:
                    os.close(fd); raise
            # Drain so a retained evdev queue cannot accumulate stale input.
            for fd, _ in held.values():
                try:
                    while os.read(fd, 4096):
                        pass
                except BlockingIOError:
                    pass
                except OSError:
                    pass  # Next scan drops disappeared devices.
            time.sleep(.05)
    finally:
        for fd, _ in held.values():
            os.close(fd)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface', type=Path, required=True)
    a = p.parse_args()
    if os.geteuid() or not str(a.interface).startswith('/sys/devices/') or ':' not in a.interface.name:
        raise SystemExit('requires root and an exact physical USB interface')
    run(a.interface)
