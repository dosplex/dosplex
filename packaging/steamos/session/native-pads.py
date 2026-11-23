#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Generic game pads (Bluetooth and USB) that are not Steam Controller receivers or the Deck's own controls.

A DualSense, DualShock 4, Xbox or Switch Pro pad (or any pad with an evdev gamepad node) is found through sysfs, grouped by
the physical device it belongs to, given a stable id and carried into the side that owns it. Nothing here mediates reports:
the real hidraw and evdev nodes are delivered (ADR-006 keeps CUSE mediation for the Valve devices that need it; native Steam
Input reads these pads from their hidraw node itself, and an evdev node serves SDL and games that do not use Steam Input).

What the nodes of one pad go through:

  discovery   sysfs: every hidraw and input (event, js) node is anchored to its HID device (Bluetooth or USB) or, for a driver
              that has no HID device (xpad), to its USB device. The nodes of one anchor are one pad, with its touchpad and
              motion sub-devices. A group counts as a pad when it is a known family (Sony, Microsoft, Nintendo ids below) or has
              an evdev node with EV_ABS and BTN_GAMEPAD. Valve (28de) devices and DosPlex's own virtual pads never count.
  identity    sha256 of [vendor, product, uniq] when the device has a uniq (the Bluetooth address, which a DualSense and a DS4
              report over USB as well, so one pad keeps its side across both); else [bus, vendor, product, phys].
  host        a runtime udev rule keeps the nodes of the known families root-only and out of uaccess/seat (and out of libinput,
              so a DualShock 4 touchpad is no mouse for the host Gamescope); every node found is also locked down at once
              (ACL cleared, root, 0600), the way native-deck-plan.py lock_down() does it.
  grant       the owning side's account gets `setfacl -m u:UID:rw` on each real node.
  delivery    the nodes are mounted at their own paths in the mount namespaces of the side's Steam (the code that mounts a
              relay's node: native-hid-delivery.py steam()/namespaces() and native-output-delivery.py mount_in_namespace(),
              with that file's --attach-pad and --replace-deleted-pad, which accept the pads' path shapes and no others).
  revoke      an ACL change does not close a handle Steam already holds. A pad that moves is therefore re-enumerated: its
              driver is unbound and bound again (sysfs), the old nodes die (ENODEV for every reader) and the new ones are
              locked down by the rule before anyone can open them. The link of a Bluetooth pad stays up.

A pad that has no saved owner belongs to Main (ADR-014, amended 2026-11-16); the user can release it or give it to Guest, and
that is kept by stable id, with the pad's number, in the controller directory (/var/lib/dosplex/controllers, outside the trial)
that the Devices tab and `dosplex control devices/assign` read.

Experimental: a pad that belongs to Main or Guest can open and drive its player's menu (the same chord as every controller).
Nothing filters these pads, so the chord also reaches the game, and so would every press made in the menu. The side is
therefore frozen (its systemd unit, as Standby does) while the menu is open because of such a pad. A reader thread of the
router process reads the pad's evdev node (never grabbed), feeds the shared menu helper and keeps that hold; see MenuHolds.
`/var/lib/dosplex/gamepad-menu-off` switches it off.
"""
import contextlib
import dataclasses
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import importlib.util
from xml.sax.saxutils import quoteattr

SYS = Path('/sys')
DEV = Path('/dev')

BUS_USB, BUS_BLUETOOTH = 0x0003, 0x0005
EV_KEY, EV_ABS = 0x01, 0x03
BTN_GAMEPAD = 0x130          # BTN_SOUTH
VALVE = 0x28de

# vendor -> product ids of the pads that count without looking at evdev (nothing else of these vendors counts that way).
FAMILIES = {
    0x054c: {0x0ce6, 0x0df2, 0x05c4, 0x09cc, 0x0268},                                  # Sony: DualSense, Edge, DS4 v1/v2, DS3
    0x045e: {0x0b13, 0x0b20, 0x0b05, 0x02e0, 0x02fd, 0x0b12, 0x02ea, 0x0b00, 0x028e},  # Microsoft: Xbox wireless/BT, Elite, 360
    0x057e: {0x2009},                                                                   # Nintendo: Switch Pro
}
MAX_PADS = 16
RULE_NAME = '73-dosplex-pads.rules'
# A file only root can make: while it exists, the devices that appear below /devices/virtual/input stay visible to the seat.
# The engineering Deck's test mouse and keyboard make it around creating their devices; that tool keeps a copy of this
# path and text, and its test checks that they agree.
TEST_SEAT_FILE = '/run/dosplex-test-input-seat'
TEST_SEAT_TEXT = 'DOSPLEX_TEST_SEAT=1\n'
HID_NAME = re.compile(r'[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}')
SUFFIXES = (' Touchpad', ' Motion Sensors', ' Accelerometer', ' Gyro')
USER_NAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}')


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def read(path, default=''):
    try:
        return Path(path).read_text(errors='replace').strip()
    except OSError:
        return default


def uevent(path):
    found = {}
    for line in read(Path(path) / 'uevent').splitlines():
        key, _, value = line.partition('=')
        found[key] = value
    return found


def bitmap(text):
    """The integer of a sysfs capability bitmap: hex words, the most significant first, one unsigned long each."""
    bits = 0
    width = 8 * struct_long()
    for word in text.split():
        bits = bits << width | int(word, 16)
    return bits


def struct_long():
    import struct
    return struct.calcsize('L')


def natural(path):
    match = re.search(r'(\d+)$', Path(path).name)
    return int(match.group(1)) if match else 0


def node_identity(path):
    """[st_dev, st_ino, st_rdev] of a node that is there, else None."""
    try:
        st = Path(path).lstat()
    except OSError:
        return None
    return [st.st_dev, st.st_ino, st.st_rdev]


@dataclasses.dataclass
class Pad:
    id: str
    name: str
    bus: int
    vendor: int
    product: int
    uniq: str
    anchor: str                       # sysfs path of the HID or USB device the nodes belong to
    kind: str                         # 'hid' or 'usb'
    hidraw: list
    events: list
    js: list
    gamepad: bool                     # an evdev node says gamepad (EV_ABS and BTN_GAMEPAD)
    family: bool                      # a known vendor/product

    @property
    def nodes(self):
        return [*sorted(self.hidraw, key=natural), *sorted(self.events, key=natural), *sorted(self.js, key=natural)]

    @property
    def transport(self):
        return 'bluetooth' if self.bus == BUS_BLUETOOTH else 'usb' if self.bus == BUS_USB else 'other'

    def signature(self):
        """What a change of the node set shows in: the paths with their identities."""
        return tuple((str(node), tuple(node_identity(node) or ())) for node in self.nodes)

    def describe(self):
        return dict(id=self.id, name=self.name, transport=self.transport, vendor='%04x' % self.vendor,
                    product='%04x' % self.product, uniq=self.uniq, nodes=[str(node) for node in self.nodes],
                    family=self.family, gamepad=self.gamepad)


def pad_id(vendor, product, uniq, bus, phys):
    """32 hex digits. The same pad over USB and Bluetooth is one id when the device reports its address as uniq."""
    if uniq and uniq.replace(':', '').strip('0'):
        identity = ['pad', vendor, product, uniq.lower()]
    elif phys:
        identity = ['pad', bus, vendor, product, phys]
    else:
        return None
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def anchor_of(device, base):
    """('hid', path) of the HID device above a node's device, else ('usb', path) of its USB device, else None."""
    parents = [parent for parent in (device, *device.parents) if parent != base and base in parent.parents]
    for parent in parents:
        if HID_NAME.fullmatch(parent.name):
            return 'hid', parent
    for parent in parents:
        if (parent / 'idVendor').is_file():
            return 'usb', parent
    return None


def emulated(device, found):
    """Whether a node belongs to a pad that software made up (Steam's virtual pads, DosPlex's routed ones: uinput
    devices below /devices/virtual/input) and not to a real one.

    A real Bluetooth pad can be below /devices/virtual as well: BlueZ hands its reports to the kernel through uhid
    (its default since 5.7x), and the HID device is then /devices/virtual/misc/uhid/0005:VVVV:PPPP.NNNN. Taking all of
    /devices/virtual for emulated hid a DualSense from DosPlex while the udev rule had already taken it from Steam
    (seen on the Steam Machine, SteamOS 3.9.5). Such a device counts as real when its HID bus is Bluetooth (0005)."""
    if '/devices/virtual/' not in str(device):
        return False
    kind, anchor = found
    return not (kind == 'hid' and '/devices/virtual/misc/uhid/' in str(anchor) + '/' and anchor.name.upper().startswith('0005:'))


def discover(sys_root=SYS, dev_root=DEV):
    """The pads that are connected now, as a list of Pad sorted by id. Only nodes that exist in dev_root count."""
    base = Path(sys_root).resolve()
    dev_root = Path(dev_root)
    groups = {}

    def group(kind, anchor):
        return groups.setdefault(str(anchor), dict(kind=kind, anchor=anchor, hidraw=[], events=[], js=[], inputs=[]))

    for entry in sorted((base / 'class/hidraw').glob('hidraw*')):
        try:
            device = (entry / 'device').resolve(strict=True)
        except OSError:
            continue
        found = anchor_of(device, base)
        if found and not emulated(device, found) and (dev_root / entry.name).exists():
            group(*found)['hidraw'].append(dev_root / entry.name)
    for entry in sorted((base / 'class/input').iterdir()) if (base / 'class/input').is_dir() else ():
        if not re.fullmatch(r'(event|js)[0-9]+', entry.name):
            continue
        try:
            device = (entry / 'device').resolve(strict=True)
        except OSError:
            continue
        found = anchor_of(device, base)
        node = dev_root / 'input' / entry.name
        if not found or emulated(device, found) or not node.exists():
            continue
        item = group(*found)
        item['events' if entry.name.startswith('event') else 'js'].append(node)
        if device not in item['inputs']:
            item['inputs'].append(device)

    pads = []
    for item in groups.values():
        pad = assemble(item)
        if pad is not None:
            pads.append(pad)
    return sorted(pads, key=lambda pad: pad.id)[:MAX_PADS]


def assemble(item):
    anchor, kind = item['anchor'], item['kind']
    info = uevent(anchor) if kind == 'hid' else {}
    inputs = item['inputs']
    if kind == 'hid':
        match = re.fullmatch(r'([0-9A-F]{4}):([0-9A-F]{8}):([0-9A-F]{8})', info.get('HID_ID', ''), re.I)
        if not match:
            return None
        bus, vendor, product = (int(group, 16) for group in match.groups())
        uniq, phys, title = info.get('HID_UNIQ', ''), info.get('HID_PHYS', ''), info.get('HID_NAME', '')
    else:
        try:
            vendor, product = int(read(anchor / 'idVendor'), 16), int(read(anchor / 'idProduct'), 16)
        except ValueError:
            return None
        bus, uniq, title = BUS_USB, read(anchor / 'serial'), read(anchor / 'product')
        phys = read(inputs[0] / 'phys') if inputs else ''
    gamepad, names = False, []
    for device in inputs:
        caps_ev, caps_key = bitmap(read(device / 'capabilities/ev')), bitmap(read(device / 'capabilities/key'))
        name = read(device / 'name')
        if name.startswith('DosPlex routed') or read(device / 'phys').startswith('dosplex-gamepad-arbiter'):
            return None
        if caps_ev >> EV_ABS & 1 and caps_key >> BTN_GAMEPAD & 1:
            gamepad = True
            names.insert(0, name)
        elif name:
            names.append(name)
        uniq = uniq or read(device / 'uniq')
    if vendor == VALVE:
        return None
    family = product in FAMILIES.get(vendor, ()) and bus in (BUS_USB, BUS_BLUETOOTH)
    if not (family or gamepad):
        return None
    name = next((n for n in [*names, title] if n), 'Game controller')
    for suffix in SUFFIXES:
        name = name.split(suffix, 1)[0]
    identity = pad_id(vendor, product, uniq, bus, phys)
    if identity is None or not (item['hidraw'] or item['events'] or item['js']):
        return None
    return Pad(id=identity, name=name[:120], bus=bus, vendor=vendor, product=product, uniq=uniq.lower(), anchor=str(anchor),
               kind=kind, hidraw=item['hidraw'], events=item['events'], js=item['js'], gamepad=gamepad, family=family)


# --- the host: runtime udev rule, ACLs, re-enumeration -------------------------------------------------------------------

def rule_text(owner='dosplex'):
    """The runtime udev rule: every input device made through uinput is invisible to the seat and to libinput, the nodes of
    the known families, and any joystick input node, are root-only and invisible to uaccess, the seat and libinput.

    The first part matches the place of the device (/devices/virtual/input, where the kernel puts what /dev/uinput makes) and
    nothing the creator chooses: not its name, vendor or product. It comes before the lines that leave Valve's and DosPlex's
    own pads alone, which are name and vendor matches that a Guest process could imitate. Bluetooth pads handed over through
    uhid are below /devices/virtual/misc/uhid and are not matched. The one exception is TEST_SEAT_FILE, which only root can make."""
    virtual = 'SUBSYSTEM=="input", DEVPATH=="/devices/virtual/input/*"'
    lines = ['# Owned by ' + owner + '; removed by recovery (native-pads.py recover).',
             'ACTION=="remove", GOTO="dosplex_pads_end"',
             '%s, IMPORT{file}="%s"' % (virtual, TEST_SEAT_FILE),
             '%s, ENV{DOSPLEX_TEST_SEAT}!="1", TAG-="uaccess", TAG-="seat", ENV{LIBINPUT_IGNORE_DEVICE}="1"' % virtual,
             'SUBSYSTEM=="input", ATTRS{id/vendor}=="28de", GOTO="dosplex_pads_end"',
             'SUBSYSTEM=="input", ATTRS{name}=="DosPlex routed*", GOTO="dosplex_pads_end"']
    lock = 'OWNER:="root", GROUP:="root", MODE:="0600", TAG-="uaccess", TAG-="seat"'
    for vendor, products in sorted(FAMILIES.items()):
        for bus in (BUS_BLUETOOTH, BUS_USB):
            patterns = '|'.join('%04X:%04X:%04X.*' % (bus, vendor, product) for product in sorted(products))
            lines.append('SUBSYSTEM=="hidraw", KERNELS=="%s", %s' % (patterns, lock))
            lines.append('SUBSYSTEM=="input", KERNEL=="event[0-9]*|js[0-9]*", KERNELS=="%s", %s, ENV{LIBINPUT_IGNORE_DEVICE}="1"'
                         % (patterns, lock))
    lines.append('SUBSYSTEM=="input", KERNEL=="event[0-9]*|js[0-9]*", ENV{ID_INPUT_JOYSTICK}=="1", %s, '
                 'ENV{LIBINPUT_IGNORE_DEVICE}="1"' % lock)
    lines.append('LABEL="dosplex_pads_end"')
    return '\n'.join(lines) + '\n'


class HostOps:
    """The changes to the host's nodes. strict=False accepts regular files (for the ACL tests)."""

    def __init__(self, strict=True, setfacl='setfacl', udevadm='udevadm'):
        self.strict, self.setfacl, self.udevadm = strict, setfacl, udevadm

    @contextlib.contextmanager
    def pinned(self, node, identity=None):
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if self.strict and not stat.S_ISCHR(st.st_mode):
                raise ValueError('expected a character device: ' + str(node))
            if identity is not None and [st.st_dev, st.st_ino, st.st_rdev] != list(identity):
                raise RuntimeError('pad node replaced: ' + str(node))
            yield fd
        finally:
            os.close(fd)

    def lock(self, node, identity=None):
        """Root's, mode 0600, ACL cleared; through a descriptor, checked against the identity it was found with."""
        with self.pinned(node, identity) as fd:
            subprocess.run([self.setfacl, '-b', '/proc/self/fd/%d' % fd], pass_fds=(fd,), check=True, timeout=5)
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)

    def grant(self, node, uid, identity=None):
        with self.pinned(node, identity) as fd:
            subprocess.run([self.setfacl, '-m', 'u:%d:rw' % uid, '/proc/self/fd/%d' % fd], pass_fds=(fd,), check=True,
                           timeout=5)

    def notify(self, node):
        """udev add for a node, so that a Steam which uses udev looks at its path again, and wait for udev to finish: udev gives
        the node its mode again (0600), which sets the ACL mask to nothing, so the caller grants the account again after this."""
        name = Path(node).name
        if re.fullmatch(r'hidraw[0-9]+', name):
            path = '/sys/class/hidraw/' + name
        elif re.fullmatch(r'(event|js)[0-9]+', name):
            path = '/sys/class/input/' + name
        else:
            return
        subprocess.run([self.udevadm, 'trigger', '--action=add', '--settle', path], check=False, timeout=8, capture_output=True)

    def unbind(self, pad, sys_root=SYS):
        """Make the driver let go of the device: every handle on its nodes dies and the nodes are gone until bind() of
        what this returns. The transport (the Bluetooth link) stays up."""
        target = Path(pad.anchor)
        if pad.kind == 'usb':
            for parent in (target / name for name in os.listdir(target) if (target / name / 'bInterfaceNumber').is_file()):
                if (parent / 'driver').exists() and any((parent / 'input').glob('input*')):
                    target = parent
                    break
        driver = (target / 'driver').resolve(strict=True)
        bus = (target / 'subsystem').resolve(strict=True).name
        base = Path(sys_root) / 'bus' / bus / 'drivers' / driver.name
        (base / 'unbind').write_text(target.name)
        return base, target.name

    def bind(self, binding):
        """The driver takes the device again: new nodes, enumerated afresh (and locked down by the rule)."""
        base, name = binding
        (Path(base) / 'bind').write_text(name)

    def rebind(self, pad, sys_root=SYS):
        self.bind(self.unbind(pad, sys_root))


def trigger_change(names, udevadm='udevadm'):
    """Let udev apply its normal rules to these sysfs paths again (after the DosPlex rule was removed)."""
    for name in names:
        subprocess.run([udevadm, 'trigger', '--action=change', name], check=False, timeout=5, capture_output=True)


# --- the namespaces of the side's Steam ----------------------------------------------------------------------------------

LAUNCHER_WAIT_SECONDS = 120.0              # how long the launcher of Steam's 64-bit client is waited for to start its client
CLIENT_FOLDERS = ('.local/share/Steam', '.steam/steam', '.steam/root')   # where an account's Steam is, below its home


def has_64bit_client(home):
    """Whether the Steam of the account whose home this is has the 64-bit client (a program steamrt64/steam)."""
    return any(os.path.exists(os.path.join(home, folder, 'steamrt64', 'steam')) for folder in CLIENT_FOLDERS)


def launcher_alone(members, has_client):
    """Whether what was found of a side's Steam is the launcher of the 64-bit client with no client below it yet.

    members: [(pid, (start, mount namespace))] of the family; has_client: the account's Steam has the 64-bit client.
    That client is complete when it runs in two mount namespaces: the launcher in the side's, the client proper in its
    container. Before that there is the launcher alone, or the launcher and a child it has forked and that is still
    named steam: one namespace either way. An older client is one process in one namespace, and complete."""
    return bool(has_client) and len({identity[1] for _, identity in members}) < 2


class NamespaceDelivery:
    """Mount real nodes at their own paths in the mount namespaces of a side's Steam.

    It reuses the helpers that deliver a relay's node (native-hid-delivery.py: steam(), namespaces(), unchanged();
    native-output-delivery.py: syscall(), mount_in_namespace(), replacement_arguments()). The checks are the same: the side is
    bound to the retained compositor, every process of the Steam family is verified before its namespace is entered, and the
    mount is checked afterwards. A mount of a node that was removed (a pad that came back, or was re-enumerated) is replaced.
    This is the part that only a real side proves (private /dev, bwrap).
    """

    def __init__(self, root, delivery=None, output=None, clock=time.monotonic, has_client=None):
        self.root = Path(root)
        self.delivery = delivery or module('hid_delivery', 'native-hid-delivery.py')
        self.output = output or module('output_delivery', 'native-output-delivery.py')
        self.clock = clock
        self.has_client = has_client or (lambda uid: has_64bit_client(pwd.getpwuid(uid).pw_dir))
        self.alone = {}                       # side -> (the launcher that was found alone, when it was first)

    def steam(self, side):
        return self.delivery.steam(self.root, side)

    def deliver(self, side, nodes):
        """nodes: [(path, identity)]. Returns the Steam family; raises if a namespace refuses a node."""
        process = self.steam(side)
        self.wait_for_client(side, process)
        # Innermost first. The namespaces of one Steam share one /dev folder (the launcher's tmpfs, which the client's
        # container takes over), and the client inside watches it: the empty file a node is mounted over shows up
        # in every namespace the moment it is made, and Steam's controller list (SDL) looks at once. Made in the
        # launcher's namespace first, the client found an empty file where its own mount came a tenth of a second
        # later, failed to open it, and SDL never tries a device again that it has listed (Deck and Steam Machine,
        # 2026-11-16: a pad given to a running Steam was delivered and never opened). Made in the client's namespace
        # first, the mount follows the file within the same helper.
        for pid, identity in reversed(list(self.delivery.namespaces(process))):
            for path, node_id in nodes:
                self.serve(process, pid, identity, str(path), node_id)
        return process

    def wait_for_client(self, side, process):
        """Raise while the 64-bit client of the side's Steam has not started below its launcher, for
        LAUNCHER_WAIT_SECONDS at most.

        A node mounted in the launcher's namespace before the client's container exists is copied into that container
        when it is made, and a copied mount is locked there: nothing can take it away again. It works until the pad's
        node is removed (the pad reconnects, or goes to the other player and comes back); then the replacement fails
        with EINVAL and the pad never reaches that Steam again until DosPlex is started again. Seen on a Steam Deck: a
        DualShock 4 that was connected when DosPlex started could be given to Guest and not back to Main; and, with a
        first form of this wait that looked for one process, three of its nodes were mounted in the half second in
        which the launcher and a child it had just forked were the family. The pad is tried again with the next pass.
        After LAUNCHER_WAIT_SECONDS the pad is delivered anyway: a Steam that stays in one namespace must not be left
        without controllers. A client that Steam starts again later in the same session still gets copies; that is
        written down in docs/limitations.md.
        """
        members = list(self.delivery.family_members(process))
        if not launcher_alone(members, self.has_client(process[3])):
            self.alone.pop(side, None)
            return
        first = self.alone.get(side)
        if first is None or first[0] != members[0]:
            first = self.alone[side] = (members[0], self.clock())
        if self.clock() - first[1] < LAUNCHER_WAIT_SECONDS:
            raise RuntimeError("Steam's client has not started yet: only its launcher runs")

    def opened(self, side, nodes):
        """Whether a process of the side's Steam holds one of these nodes open. nodes: [(path, identity)]."""
        wanted = {tuple(identity)[2] for _, identity in nodes if identity}
        for pid, _ in self.delivery.family_members(self.steam(side)):
            try:
                names = os.listdir('/proc/%d/fd' % pid)
            except OSError:
                continue
            for name in names:
                try:
                    meta = os.stat('/proc/%d/fd/%s' % (pid, name))
                except OSError:
                    continue
                if stat.S_ISCHR(meta.st_mode) and meta.st_rdev in wanted:
                    return True
        return False

    def serve(self, process, pid, identity, path, node_id):
        d, h = self.output, self.delivery
        uid = process[3]
        gid = pwd.getpwuid(uid).pw_gid
        with contextlib.ExitStack() as stack:
            def keep(fd):
                stack.callback(os.close, fd)
                return fd
            view = Path('/proc') / str(pid) / 'root' / path.lstrip('/')
            try:
                meta = view.stat()
            except FileNotFoundError:
                state, pinned, current = 'absent', None, None
            else:
                if d.device_identity(meta) == node_id:
                    return 'mounted'
                pinned = keep(os.open(view, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC))
                current = os.fstat(pinned)
                if stat.S_ISCHR(current.st_mode) and current.st_nlink == 0:
                    state = 'stale'                 # the mount of a node that was removed
                elif h.placeholder_problem(current, uid) is None:
                    state = 'placeholder'
                else:
                    raise RuntimeError('occupied pad mountpoint %s in %s (process %d): %s'
                                       % (path, identity[1], pid, h.placeholder_problem(current, uid)))
            source = keep(os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC))
            if d.device_identity(os.fstat(source)) != node_id:
                raise RuntimeError('pad node replaced: ' + path)
            clone = keep(d.syscall(428, source, b'', 0x1000 | 1 | os.O_CLOEXEC))
            mount = keep(os.open('/proc/%d/ns/mnt' % pid, os.O_RDONLY))
            rootfd = keep(os.open('/proc/%d/root' % pid, os.O_PATH | os.O_DIRECTORY))
            h.unchanged(process, 'Steam changed')
            try:
                if state == 'absent':
                    d.mount_in_namespace(mount, rootfd, ['--attach-pad', clone, path, uid, gid], (clone,))
                elif state == 'stale':
                    d.mount_in_namespace(mount, rootfd, ['--replace-deleted-pad', clone, pinned, path, uid, gid], (clone, pinned))
                else:
                    d.mount_in_namespace(mount, rootfd, ['--attach-existing', clone, pinned], (clone, pinned))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise RuntimeError('pad delivery failed in %s (process %d): %s' % (identity[1], pid, d.helper_failure(error)))
        if d.device_identity(view.stat()) != node_id:
            raise RuntimeError('pad delivery mismatch: ' + path)
        return 'delivered'

    def announce(self, process, nodes):
        """The poke that wakes a Steam in a container (native-hid-delivery.py rescan_poke) for the hidraw nodes just delivered."""
        for path, node_id in nodes:
            if re.fullmatch(r'/dev/hidraw[0-9]+', str(path)):
                relay = dict(target=str(path), identity=node_id)
                self.delivery.recovered_family(process, relay)
                self.delivery.rescan_poke(process, relay)


# --- udev events ---------------------------------------------------------------------------------------------------------

class UdevMonitor:
    """`udevadm monitor` for hidraw and input; changed() says whether any event came in since it was last asked."""

    def __init__(self, spawn=None):
        self.spawn = spawn or (lambda: subprocess.Popen(
            ['udevadm', 'monitor', '--udev', '--subsystem-match=hidraw', '--subsystem-match=input'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        self.process = None

    def changed(self):
        if self.process is None or self.process.poll() is not None:
            self.process = self.spawn()
            return True                                  # (a restarted monitor may have missed events)
        fd = self.process.stdout.fileno()
        seen = False
        while select.select([fd], [], [], 0)[0]:
            data = os.read(fd, 65536)
            if not data:
                return True
            seen = seen or re.search(rb'^UDEV\s+\[', data, re.M) is not None
        return seen

    def close(self):
        if self.process is not None:
            with contextlib.suppress(OSError):
                self.process.terminate()
                self.process.wait(2)
            self.process = None


# --- the manager ---------------------------------------------------------------------------------------------------------

# --- the menu for pads that are not mediated (experimental) --------------------------------------------------------------

MENU_OFF_FILE = Path('/var/lib/dosplex/gamepad-menu-off')
HAT_X, HAT_Y, ABS_X, ABS_Y = 16, 17, 0, 1
DPAD_KEYS = (544, 545, 546, 547)           # BTN_DPAD_UP, DOWN, LEFT, RIGHT
NEUTRAL_SECONDS = 0.3                      # no button, hat or stick for this long before a side is thawed
OPEN_WAIT_SECONDS = 3.0                    # how long the menu may take to show after its open action
LEFT_WAIT_SECONDS = 2.0                    # how long the menu may take to close after the pads of its hold left the side
LEFT_GRACE_SECONDS = 0.3                   # the pads have to be gone this long before the menu is looked at for a question
QUESTION_WAIT_SECONDS = 20.0               # how long a question in the menu is waited for (a move expires after 15 s)
STICK_DEAD_ZONE = 0.3                      # of the half range
EVENT = struct.Struct('llHHi')             # struct input_event
LEFT_RIGHT = ('left', 'right')


def evdev_ioc(direction, number, size):
    return direction << 30 | size << 16 | ord('E') << 8 | number


def menu_module():
    """The shared menu helper: beside this file in a release, one folder up in the source tree."""
    for folder in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        if (folder / 'dosplex-controller-menu.py').is_file():
            spec = importlib.util.spec_from_file_location('menu', folder / 'dosplex-controller-menu.py')
            value = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(value)
            return value
    raise FileNotFoundError('dosplex-controller-menu.py')


class EvdevSource:
    """One evdev node, read-only and non-blocking, never grabbed. wait() and read() raise OSError when the pad is gone."""

    def __init__(self, path):
        import fcntl
        self.path = str(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            def bits(kind, size):
                buffer = bytearray(size)
                fcntl.ioctl(self.fd, evdev_ioc(2, 0x20 + kind, size), buffer)
                return int.from_bytes(buffer, 'little')
            keys, absolute = bits(EV_KEY, 96), bits(EV_ABS, 8)
            self.gamepad = bool(keys >> BTN_GAMEPAD & 1) and bool(absolute & 1)
            self.hat = bool(absolute >> HAT_X & 1 and absolute >> HAT_Y & 1)
            self.dpad_keys = all(keys >> code & 1 for code in DPAD_KEYS)
            self.ranges = {}
            for code in (ABS_X, ABS_Y):
                if absolute >> code & 1:
                    buffer = bytearray(24)
                    fcntl.ioctl(self.fd, evdev_ioc(2, 0x40 + code, 24), buffer)
                    _, low, high, _, _, _ = struct.unpack('6i', buffer)
                    self.ranges[code] = (low, high)
        except BaseException:
            os.close(self.fd)
            raise

    def wait(self, timeout):
        return bool(select.select([self.fd], [], [], timeout)[0])

    def read(self):
        try:
            data = os.read(self.fd, EVENT.size * 64)
        except BlockingIOError:
            return []
        return [EVENT.unpack_from(data, at)[2:] for at in range(0, len(data) - EVENT.size + 1, EVENT.size)]

    def close(self):
        with contextlib.suppress(OSError):
            os.close(self.fd)


def open_pad_source(pad):
    """The evdev node of a pad that carries its buttons (a DualSense has three nodes: pad, touchpad, motion)."""
    problem = 'the pad has no evdev node'
    for node in sorted(pad.events, key=natural):
        try:
            source = EvdevSource(node)
        except OSError as error:
            problem = '%s: %s' % (node, error.strerror or error)
            continue
        if source.gamepad:
            return source
        source.close()
        problem = 'no evdev node of the pad has gamepad buttons and axes'
    raise RuntimeError(problem)


class SystemdFreezer:
    """Freeze and thaw the session unit of a side, the way the side supervisor does for Standby (set_standby).

    The unit name is in <trial>/sides/<side>/session.json, which the supervisor writes and the delivery code of this process
    reads the same way (native-hid-delivery.py steam()). The sides are system units (no --user)."""

    def __init__(self, root, run=subprocess.run, check_output=subprocess.check_output):
        self.root, self.run, self.check_output = Path(root), run, check_output

    def unit(self, side):
        path = self.root / 'sides' / side / 'session.json'
        for item in (path, *path.parents):
            info = item.lstat()
            if info.st_uid or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                raise PermissionError('untrusted session metadata')
            if item == self.root:
                break
        unit = json.loads(path.read_text()).get('unit', '')
        if not re.fullmatch('dosplex-session-' + side + r'-[a-f0-9]{32}\.service', str(unit)):
            raise ValueError('the side has no session unit')
        return unit

    def change(self, side, verb, wanted):
        unit = self.unit(side)
        self.run(['systemctl', verb, unit], check=True, capture_output=True, timeout=10)
        seen = self.check_output(['systemctl', 'show', unit, '-p', 'FreezerState', '--value'], text=True, timeout=5).strip()
        if seen != wanted:
            raise RuntimeError('the freezer state was %r, not %r' % (seen, wanted))

    def freeze(self, side):
        self.change(side, 'freeze', 'frozen')

    def thaw(self, side):
        self.change(side, 'thaw', 'running')


def read_state_file(runtime):
    """state.json of the compositor as a dict, or None when it cannot be read."""
    try:
        data = json.loads((Path(runtime) / 'state.json').read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class MenuHolds:
    """The pauses that keep a side still while its menu is open because of a pad that nobody filters.

    One hold per side. It starts when a reader's chord completes (the side is frozen and the menu opened), lasts while the
    menu is open and until the pads of the hold have been neutral for NEUTRAL_SECONDS (a queue of reports holds only the
    last ones: nothing of the last press may be replayed to the game), and ends with a thaw. A side that is in Standby for
    its own reasons is never thawed here. Every operation that the tests have to fake is passed in.

    A hold whose pads have all left the side (given to the other player from the Devices page, switched off, out of
    range) has nobody to close its menu, and no pad whose presses could reach the game: the menu is closed once, and the
    side is thawed when it has closed, or LEFT_WAIT_SECONDS later with the menu still open. Seen on a Steam Deck: a
    DualShock 4 opened Main's menu and was given to Guest in it, and Main stayed frozen for seven minutes.

    Not while the menu asks a question, though. Giving a pad to the other player is a move that Main keeps or undoes
    within 15 seconds, with the controls Main has left, and closing the menu undoes it: the pad would come straight
    back (seen on a Steam Deck with the first form of this rule). So the menu is left alone while its question is open
    (state.json's overlay_confirmation), for QUESTION_WAIT_SECONDS at most, and closed after the answer."""

    def __init__(self, root, *, freezer=None, state=None, sender=None, clock=time.monotonic, wall=time.time, log=None):
        self.root = Path(root)
        self.runtime = self.root / 'runtime'
        self.freezer = freezer or SystemdFreezer(self.root)
        self.read_state = state or (lambda: read_state_file(self.runtime))
        self.sender = sender or self.send_to_compositor
        self.clock, self.wall = clock, wall
        self.log = log or (lambda record: print(json.dumps(record), flush=True))
        self.locks = {side: threading.RLock() for side in LEFT_RIGHT}
        self.holds = {}                       # side -> dict(pad, pads, opened_at, seen, ending)
        self.data = None                      # the last state.json that was read
        self.socket = None

    def hold_file(self, side):
        return self.runtime / ('pad-menu-hold-%s.json' % side)

    def send_to_compositor(self, side, action):
        if self.socket is None:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.sendto(('MENU %s %s' % (side, action)).encode(), str(self.runtime / 'hud-ipc.sock'))

    # -- what state.json says

    def refresh(self):
        self.data = self.read_state()
        return self.data

    def activity(self, side):
        return self.data.get(side, {}).get('activity', 'active') if self.data else None

    def shown(self, side):
        """The side is active and, on a single screen, the one that is shown (the relays' app_active)."""
        data = self.data
        if not data:
            return False
        return (data.get(side, {}).get('activity', 'active') == 'active'
                and (data.get('layout') != 'single' or data.get('primary_side') == side))

    def target(self, side):
        """The side whose menu a pad of `side` drives: the shown one on a single screen (the relays' overlay_target)."""
        data = self.data or {}
        if data.get('layout') == 'single' and data.get('primary_side') in LEFT_RIGHT:
            return data['primary_side']
        return side

    def menu_open(self, side):
        data = self.data or {}
        return data.get('overlay_visible') is True and data.get('overlay_owner') == self.target(side)

    # -- the hold

    def driving(self, side):
        """Whether the pads of the hold of `side` may move in the menu now: a hold of theirs is there and the menu shows."""
        hold = self.holds.get(side)
        return bool(hold) and not hold['ending'] and self.menu_open(side)

    def refuse(self, reader, reason):
        self.log({'native_pad_menu_refused': reader.pad.name, 'id': reader.pad.id, 'side': reader.side, 'reason': reason})

    def chord(self, reader):
        """The chord of a pad completed. With a hold of the side there: the pad joins it, and the chord closes the menu.
        Without one and with the menu open (something else opened it): the chord closes the menu. Otherwise: freeze, and
        only when that is confirmed, open."""
        side = reader.side
        with self.locks[side]:
            self.refresh()
            hold = self.holds.get(side)
            if hold:
                hold['pads'][reader.pad.id] = reader       # (also a pad that had left and is back)
                if self.menu_open(side) and not hold['ending']:
                    self.send(side, 'toggle')
                return
            if self.data is None:
                return self.refuse(reader, 'state.json cannot be read')
            if not self.shown(side):
                return self.refuse(reader, 'the side is not active and shown (activity %s)' % self.activity(side))
            if self.menu_open(side):
                # Opened by something else (a Steam Controller, the Deck's controls, a keyboard, a touch): the chord
                # closes it, as every controller's chord does. No hold: this pad pressed nothing in the menu.
                with contextlib.suppress(OSError):
                    self.send(side, 'toggle')
                self.log({'native_pad_menu_closed': reader.pad.name, 'id': reader.pad.id, 'side': side})
                return
            try:
                self.freezer.freeze(side)
            except Exception as error:
                with contextlib.suppress(Exception):
                    self.freezer.thaw(side)        # (a freeze that was made but not confirmed)
                return self.refuse(reader, 'the side could not be frozen: ' + str(error)[:160])
            now = self.clock()
            record = dict(pad=reader.pad.name, pad_id=reader.pad.id, side=side, since=self.wall())
            try:
                self.runtime.mkdir(parents=True, exist_ok=True)
                temporary = self.hold_file(side).with_suffix('.new')
                temporary.write_text(json.dumps(record))
                temporary.chmod(0o600)
                os.replace(temporary, self.hold_file(side))
                self.send(side, 'toggle')
            except Exception as error:
                self.thaw(side)
                return self.refuse(reader, 'the menu could not be opened: ' + str(error)[:160])
            self.holds[side] = dict(pad=reader.pad.name, pads={reader.pad.id: reader}, opened_at=now, seen=False, ending=False)
            self.log({'native_pad_menu_hold': reader.pad.name, 'id': reader.pad.id, 'side': side})

    def send(self, side, action):
        self.sender(self.target(side), action)

    def watching(self, side):
        """Whether a pad of `side` has a menu to act on: one its hold drives, or one that something else opened."""
        return self.driving(side) or (side not in self.holds and self.menu_open(side))

    def drive(self, reader, action):
        """A press of a pad. In the menu of its hold every action goes through. In a menu that something else opened
        (no hold: the side runs, and the game gets the same press) only `back` does: the pad can close that menu or
        leave its page, and does not move in it."""
        with self.locks[reader.side]:
            hold = self.holds.get(reader.side)
            if hold:
                allowed = reader.pad.id in hold['pads'] and self.driving(reader.side)
            else:
                allowed = action == 'back' and self.menu_open(reader.side)
            if allowed:
                with contextlib.suppress(OSError):
                    self.send(reader.side, action)

    def thaw(self, side):
        """Thaw `side` if its activity is active, and remove the hold file. False when the thaw failed (the file stays)."""
        if self.activity(side) == 'active':
            try:
                self.freezer.thaw(side)
            except Exception as error:
                self.log({'native_pad_menu_error': side, 'reason': 'the side could not be thawed: ' + str(error)[:160]})
                return False
        else:
            self.log({'native_pad_menu_not_thawed': side, 'reason': 'the side is not active (%s): it is left as it is' % self.activity(side)})
        with contextlib.suppress(OSError):
            self.hold_file(side).unlink()
        return True

    def service(self, now):
        """Look at every hold: end the ones whose menu is closed and whose pads are still, or that cannot go on."""
        self.refresh()
        for side in LEFT_RIGHT:
            with self.locks[side]:
                hold = self.holds.get(side)
                if not hold or self.data is None:
                    continue
                if self.activity(side) != 'active':
                    self.end(side, hold, 'the side went to Standby')
                elif hold['ending']:
                    self.end(side, hold, 'the thaw is tried again')
                elif self.menu_open(side):
                    hold['seen'] = True
                    gone = self.gone_since(hold)
                    if gone is None:
                        hold.pop('left_at', None)             # (a pad of the hold is back: it joined with its chord)
                    elif 'left_at' not in hold:
                        if now - gone < LEFT_GRACE_SECONDS:
                            continue
                        if self.question_open() and now - gone < QUESTION_WAIT_SECONDS:
                            continue                          # (closing the menu now would answer its question)
                        hold['left_at'] = now
                        with contextlib.suppress(OSError):
                            self.send(side, 'toggle')         # once: a second one would open it again
                    elif now - hold['left_at'] >= LEFT_WAIT_SECONDS:
                        self.end(side, hold, 'the pads of the menu left the side and the menu stayed open')
                elif not hold['seen']:
                    if now - hold['opened_at'] >= OPEN_WAIT_SECONDS:
                        self.end(side, hold, 'the menu did not show')
                elif self.neutral(hold, now):
                    self.end(side, hold, 'the menu closed')

    @staticmethod
    def gone_since(hold):
        """When the last pad of the hold left the side (its reader ended), or None while one of them is still there."""
        times = list(hold['pads'].values())
        return max(times) if all(isinstance(item, float) for item in times) else None

    def question_open(self):
        """Whether the menu asks a question now (keep a device move, a change for both players)."""
        return (self.data or {}).get('overlay_confirmation') is True

    def neutral(self, hold, now):
        for item in hold['pads'].values():
            since = item if isinstance(item, float) else item.neutral_since
            if since is None or now - since < NEUTRAL_SECONDS:
                return False
        return True

    def end(self, side, hold, why):
        hold['ending'] = True
        if self.thaw(side):
            self.holds.pop(side, None)
            self.log({'native_pad_menu_released': hold['pad'], 'side': side, 'reason': why})

    def reader_ended(self, reader, now):
        """A reader is gone (the pad went away): it counts as still from now on."""
        with self.locks[reader.side]:
            hold = self.holds.get(reader.side)
            if hold and hold['pads'].get(reader.pad.id) is reader:
                hold['pads'][reader.pad.id] = float(now)

    def release_all(self, why):
        """Every side that has a hold file (a router that died, or this one closing) is thawed if it is active, and the file
        goes. A file whose thaw failed stays."""
        self.refresh()
        for side in LEFT_RIGHT:
            with self.locks[side]:
                if not self.hold_file(side).exists() and side not in self.holds:
                    continue
                if self.thaw(side):
                    self.holds.pop(side, None)
                    self.log({'native_pad_menu_released': side, 'side': side, 'reason': why})


class PadReader:
    """Reads one pad's evdev node in a thread and feeds the menu helper like a relay does. Its logic is in feed() and step(),
    which the tests call with their own events and clock."""

    def __init__(self, pad, side, source, holds, menu, clock=time.monotonic, log=None):
        self.pad, self.side, self.source, self.holds, self.clock = pad, side, source, holds, clock
        self.log = log or (lambda record: print(json.dumps(record), flush=True))
        self.down, self.axes = set(), {}
        self.dpad_keys = source.dpad_keys and not source.hat      # a pad that has a hat keeps it; its keys are only noted
        self.ranges = dict(source.ranges)
        self.neutral_since = clock()
        self.stopping = threading.Event()
        self.thread = None
        self.menu = menu.ControllerMenu(self.action, lambda: self.holds.watching(self.side), self.ranges)

    def action(self, action):
        if action == 'toggle':
            self.holds.chord(self)
        else:
            self.holds.drive(self, action)

    def feed(self, events, now):
        for kind, code, value in events:
            if kind == EV_KEY:
                if value:
                    self.down.add(code)
                else:
                    self.down.discard(code)
                if code in DPAD_KEYS:
                    if self.dpad_keys:      # the menu helper reads a hat: tell it one
                        pressed = lambda key: int(key in self.down)
                        self.menu.event(EV_ABS, HAT_Y, pressed(545) - pressed(544), now)
                        self.menu.event(EV_ABS, HAT_X, pressed(547) - pressed(546), now)
                    continue
            elif kind == EV_ABS and code in (HAT_X, HAT_Y, ABS_X, ABS_Y):
                self.axes[code] = value
            elif kind != 0:
                continue
            self.menu.event(kind, code, value, now)

    def centred(self):
        for code in (HAT_X, HAT_Y):
            if self.axes.get(code, 0):
                return False
        for code in (ABS_X, ABS_Y):
            if code in self.axes and code in self.ranges:
                low, high = self.ranges[code]
                extent = (high - low) / 2
                if extent > 0 and abs((self.axes[code] - (low + high) / 2) / extent) > STICK_DEAD_ZONE:
                    return False
        return True

    def step(self, now):
        """After events (or none): time-driven menu actions, the neutral clock, and a look at the holds."""
        self.menu.tick(now)
        if self.down or not self.centred():
            self.neutral_since = None
        elif self.neutral_since is None:
            self.neutral_since = now
        self.holds.service(now)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='pad-menu-' + self.pad.id[:8], daemon=True)
        self.thread.start()
        self.log({'native_pad_menu_reader': self.pad.name, 'id': self.pad.id, 'side': self.side, 'event': 'started'})

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def stop(self):
        self.stopping.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(1.0)

    def run(self):
        reason = 'stopped'
        try:
            while not self.stopping.is_set():
                ready = self.source.wait(0.05)
                now = self.clock()
                if ready:
                    self.feed(self.source.read(), now)
                self.step(now)
        except OSError as error:
            reason = 'the pad went away' if error.errno in (errno.ENODEV, errno.EIO, errno.ENOENT, errno.EBADF) else str(error)
        except Exception as error:
            reason = 'error: ' + str(error)[:160]
        finally:
            self.source.close()
            self.holds.reader_ended(self, self.clock())
            self.log({'native_pad_menu_reader': self.pad.name, 'id': self.pad.id, 'side': self.side, 'event': 'ended',
                      'reason': reason})


class PadManager:
    """Keeps the generic pads where their preferences say. The router calls tick() and rows() with its own state."""

    SCAN_SECONDS = 5.0
    DELIVERY_SECONDS = 2.0
    RETRY_SECONDS = 3.0
    REBIND_SECONDS = 10.0
    VANISH_SECONDS = 1.0     # how long a pad stays without its driver, so that every Steam that was told has dropped it
    OPEN_SECONDS = 5.0       # how long Steam may take to open a pad it was told about
    CURES = 2                # times a pad that Steam did not open is taken away and given again
    CURE_MEMORY = 60.0       # seconds the count of those is kept (it has to outlive the pad's own absence)
    UNOPENED = 'Steam has not opened this controller'

    def __init__(self, root, registry, registry_module, owners, effective_side, *, sys_root=SYS, dev_root=DEV, host=None,
                 delivery=None, monitor=None, clock=time.monotonic, log=None, holds=None, source_factory=open_pad_source,
                 menu_factory=menu_module, menu_off=MENU_OFF_FILE):
        self.root, self.registry, self.reg = Path(root), registry, registry_module
        self.owners = dict(owners)                         # side ('left'/'right') -> uid
        self.effective_side, self.sys_root, self.dev_root = effective_side, sys_root, dev_root
        self.host = host or HostOps()
        self.delivery = delivery or NamespaceDelivery(root)
        self.monitor, self.clock = monitor, clock
        self.log = log or (lambda record: print(json.dumps(record), flush=True))
        self.names = {}                      # id -> last known name of every pad that has a preference
        self.pads = {}                       # id -> Pad connected now
        self.locked = {}                     # id -> node signature locked down
        self.applied, self.delivered = {}, {}   # id -> side ('left', 'right', 'unassigned'); id -> signature delivered
        self.errors, self.retry_at, self.rebinding = {}, {}, {}
        self.vanished = {}                   # id -> the driver binding to restore and when (see vanish)
        self.watch, self.cures = {}, {}      # id -> when to look whether Steam opened it; id -> cures made and when
        self.scan_due = self.delivery_due = 0.0
        self.active = {}
        # The menu for pads nobody mediates (experimental): one reader thread per pad that has an owner.
        self.readers, self.reader_failed = {}, {}   # id -> PadReader; id -> node signature that could not be read
        self.holds = holds or MenuHolds(self.root, clock=clock, log=self.log)
        self.source_factory, self.menu_factory, self.menu = source_factory, menu_factory, None
        self.menu_on = not Path(menu_off).exists()
        self.log({'native_pad_menu': 'on (experimental)' if self.menu_on else 'off: ' + str(menu_off) + ' exists'})
        self.holds.release_all('a hold was left behind by an earlier run')
        for path in sorted(Path(self.registry.directory).glob('*.json'))[:64]:
            if not re.fullmatch('[a-f0-9]{32}', path.stem):
                continue
            try:
                record = self.reg.read_json(path)
            except (OSError, ValueError):
                continue
            if record.get('backend') == 'generic-pad' and record.get('target') in self.reg.TARGETS:
                self.names[path.stem] = str(record.get('name', 'Game controller'))[:120]
                self.registry.devices.setdefault(path.stem, dict(id=path.stem, name=self.names[path.stem], requested='unassigned',
                                                                 target='unassigned', connected=False, active=False, node=''))

    # -- assignment

    def awake_owners(self):
        """The owners of the pads that are connected and recorded ('main', 'guest' or 'unassigned' each)."""
        owners = []
        for key in self.pads:
            if key in self.names and key in self.registry.devices:
                try:
                    owners.append(self.registry.target(key))
                except Exception:
                    pass
        return owners

    def default_target(self, state=None, others=()):
        """ADR-014 (amended 2026-11-23) for a pad this machine has never seen: the first controller is Main's, and while
        the second player is on the screen with none, the next one is theirs (registry first_owner). others are the rows
        of the router's own controllers (the receivers' and the Steam Deck's controls). The user can release the pad or
        give it to the other player, and the choice is kept."""
        owners = [row.get('requested', row.get('target')) for row in others if isinstance(row, dict) and row.get('connected')]
        owners += self.awake_owners()
        return self.reg.first_owner(state or {'layout': 'single'}, 'main' in owners, 'guest' in owners)

    def remember(self, pad, target):
        number = self.reg.save_record(self.registry.directory, pad.id,
                                      dict(name=pad.name, target=target, backend='generic-pad', transport=pad.transport))
        self.names[pad.id] = pad.name
        self.registry.devices.setdefault(pad.id, dict(id=pad.id, name=pad.name, index=number, requested=target,
                                                      target='unassigned', connected=True, active=False, node=''))

    def forget(self, key):
        """Drop what is kept of a pad whose record was removed (Registry.forget_requests). One that is connected is not
        forgotten (the router refuses it); a pad that was found again is recorded afresh, with a new number."""
        self.names.pop(key, None)
        for table in (self.applied, self.delivered, self.locked, self.rebinding, self.errors, self.watch,
                      self.retry_at, self.cures):
            table.pop(key, None)

    # -- one look

    def scan(self):
        found = {pad.id: pad for pad in discover(self.sys_root, self.dev_root)}
        for key in set(self.pads) - set(found):                         # gone: what was delivered goes with it
            for table in (self.applied, self.delivered, self.locked, self.rebinding, self.errors, self.watch):
                table.pop(key, None)
        self.pads = found

    def new_pads(self, state=None, others=()):
        """Give every pad without a preference its first owner (default_target) and a number."""
        for key, pad in sorted(self.pads.items()):
            if key in self.names:
                continue
            if len(self.names) >= 28:
                self.errors[key] = 'controller inventory is full'
                continue
            target = self.default_target(state, others)
            self.remember(pad, target)
            self.log({'native_pad_new': pad.name, 'id': key, 'target': target})

    def tick(self, state, others=()):
        """One pass: look for pads when udev said so (or now and then), lock them down, move and deliver them.

        others: the rows of the router's own controllers (they count for a new pad's first owner). Never raises."""
        now = self.clock()
        try:
            self.bind_due(now)
            changed = self.monitor.changed() if self.monitor else False
            if changed or now >= self.scan_due:
                self.scan()
                self.scan_due = now + self.SCAN_SECONDS
            self.new_pads(state, others)
            for key, pad in sorted(self.pads.items()):
                self.settle(key, pad, state, now)
            if self.menu_on:
                self.sync_readers(now)
                self.holds.service(now)
        except Exception as error:
            self.log({'native_pads_wait': str(error)[:250]})

    def sync_readers(self, now):
        """A reader for every pad that belongs to Main or Guest (as applied); none for a pad that left, moved or whose node
        was made again (a reader whose thread ended is replaced, from the node that is there now)."""
        for key in sorted(self.readers):
            reader, pad = self.readers[key], self.pads.get(key)
            if reader.alive() and pad and self.applied.get(key) == reader.side and pad.signature() == reader.signature:
                continue
            reader.stop()
            self.readers.pop(key)
        for key, pad in sorted(self.pads.items()):
            side = self.applied.get(key)
            if key in self.readers or side not in self.owners or self.delivered.get(key) != pad.signature():
                continue
            if self.reader_failed.get(key) == pad.signature():
                continue
            try:
                if self.menu is None:
                    self.menu = self.menu_factory()
                source = self.source_factory(pad)
            except Exception as error:
                self.reader_failed[key] = pad.signature()
                self.log({'native_pad_menu_refused': pad.name, 'id': key, 'side': side,
                          'reason': 'no reader: ' + str(error)[:160]})
                continue
            reader = PadReader(pad, side, source, self.holds, self.menu, self.clock, self.log)
            reader.signature = pad.signature()
            self.readers[key] = reader
            self.reader_failed.pop(key, None)
            reader.start()

    def lock_down(self, key, pad):
        signature = pad.signature()
        if self.locked.get(key) == signature:
            return
        for node in pad.nodes:
            self.host.lock(node, node_identity(node))
        self.locked[key] = signature

    def settle(self, key, pad, state, now):
        if key not in self.registry.devices or key in self.vanished:
            return
        try:
            wait = self.rebinding.get(key)
            if wait:
                if pad.signature() == wait['before']:
                    if now < wait['deadline']:
                        return
                    raise RuntimeError('the controller did not come back after being released')
                self.rebinding.pop(key)
            self.lock_down(key, pad)
            if now < self.retry_at.get(key, 0):
                return
            want = self.effective_side(self.registry.target(key), state)
            have, signature = self.applied.get(key), pad.signature()
            if have in self.owners and have != want:
                self.release(key, pad, have, now)
                return
            if have == want and self.delivered.get(key) == signature:
                seen = self.check_opened(key, pad, want, state, now) if want in self.owners else None
                if seen == 'cure':
                    return
                if (want in self.owners and now >= self.delivery_due
                        and state.get(want, {}).get('activity', 'active') == 'active'):
                    self.deliver(key, pad, want, announce=False)      # a restarted Steam needs its pads again
                    if seen != 'unopened':
                        self.errors.pop(key, None)
                return
            if want in self.owners:
                self.deliver(key, pad, want, announce=True)
                self.watch[key] = dict(check_at=now + self.OPEN_SECONDS)
                self.log({'native_pad_delivered': pad.name, 'id': key, 'side': want,
                          'nodes': [str(node) for node in pad.nodes]})
            self.applied[key], self.delivered[key] = want, signature
            self.errors.pop(key, None)
        except Exception as error:
            message = str(error)[:160]
            if self.errors.get(key) != message:
                self.log({'native_pad_error': pad.name, 'id': key, 'reason': message})
            self.errors[key] = message
            self.retry_at[key] = now + self.RETRY_SECONDS

    def deliver(self, key, pad, side, announce):
        nodes = [(node, node_identity(node)) for node in pad.nodes]
        for node, identity in nodes:
            self.host.grant(node, self.owners[side], identity)
        try:
            process = self.delivery.deliver(side, nodes)
        except Exception:
            # Not delivered: the side's account must not keep the access it was just given. A pad whose first owner
            # was a side that is off was left readable by that account after the user gave it to the other side (seen
            # on the Steam Machine: a DualSense meant for Guest, then moved to Main, readable by both).
            for node, identity in nodes:
                with contextlib.suppress(Exception):
                    self.host.lock(node, identity)
            self.locked.pop(key, None)
            raise
        if announce:
            for node, _ in nodes:
                self.host.notify(node)
            for node, identity in nodes:                    # (udev set the modes again: the ACL mask is gone with them)
                self.host.grant(node, self.owners[side], identity)
            self.delivery.announce(process, nodes[:len(pad.hidraw)][:1])
        self.delivery_due = self.clock() + self.DELIVERY_SECONDS

    def release(self, key, pad, side, now):
        """The pad leaves the side it was delivered to: ACLs off, then the driver lets go so no old handle works."""
        for node in pad.nodes:
            self.host.lock(node, node_identity(node))
        before = pad.signature()
        self.vanish(key, pad, now)
        self.rebinding[key] = dict(before=before, deadline=now + self.VANISH_SECONDS + self.REBIND_SECONDS)
        for table in (self.applied, self.delivered, self.locked, self.watch):
            table.pop(key, None)
        self.log({'native_pad_released': pad.name, 'id': key, 'side': side})

    def vanish(self, key, pad, now):
        """The driver lets go of the pad, every running Steam is told to look at its controllers, and the driver takes
        the pad again VANISH_SECONDS later (bind_due).

        Steam's controller list (SDL) reads the hidraw devices from sysfs, which a side sees whole, and keeps a device
        it could not open for as long as the device is listed: it is not tried again, whatever is mounted or allowed
        later. A Steam that looked while the pad was the other side's, or nobody's, holds such an entry. Looking while
        the pad is gone is what drops it, so the pad that comes back is a new one to every Steam. A pad without a
        hidraw node has no such list to clear: its driver takes it again at once."""
        hid = [(node, node_identity(node)) for node in pad.hidraw[:1]]
        binding = self.host.unbind(pad, self.sys_root)
        if not hid:
            self.host.bind(binding)
            return
        self.vanished[key] = dict(binding=binding, bind_at=now + self.VANISH_SECONDS, name=pad.name)
        for side in sorted(self.owners):
            with contextlib.suppress(Exception):        # (a side that is off has no Steam to tell)
                self.delivery.announce(self.delivery.steam(side), hid)

    def bind_due(self, now=None):
        """Give the pads that vanish() took away back to their drivers; now=None: all of them (the router is closing)."""
        for key, item in sorted(self.vanished.items()):
            if now is not None and now < item['bind_at']:
                continue
            self.vanished.pop(key)
            self.scan_due = 0.0
            try:
                self.host.bind(item['binding'])
            except Exception as error:
                self.log({'native_pad_error': item['name'], 'id': key,
                          'reason': ('the driver did not take the controller again: ' + str(error))[:160]})

    def check_opened(self, key, pad, side, state, now):
        """Look whether the side's Steam opened a pad it was told about, OPEN_SECONDS after the delivery.

        None: nothing to say (not due, the side is standing by, or Steam has it open). 'cure': it had not, and the pad
        was taken away to be given again (vanish), at most CURES times. 'unopened': it still has not; the row says so
        until it does."""
        watch = self.watch.get(key)
        if not watch:
            return None
        if now < watch['check_at']:
            return 'unopened' if watch.get('gave_up') else None
        if state.get(side, {}).get('activity', 'active') != 'active':
            watch['check_at'] = now + self.OPEN_SECONDS         # a Steam that stands by opens nothing
            return 'unopened' if watch.get('gave_up') else None
        if self.delivery.opened(side, [(node, node_identity(node)) for node in pad.nodes]):
            self.watch.pop(key)
            self.cures.pop(key, None)
            if self.errors.get(key) == self.UNOPENED:
                self.errors.pop(key)
            self.log({'native_pad_opened': pad.name, 'id': key, 'side': side})
            return None
        cure = self.cures.get(key)
        count = cure['count'] if cure and now - cure['at'] < self.CURE_MEMORY else 0
        if count >= self.CURES:
            if not watch.get('gave_up'):
                self.log({'native_pad_error': pad.name, 'id': key, 'reason': self.UNOPENED})
            watch.update(gave_up=True, check_at=now + 2 * self.OPEN_SECONDS)
            self.errors[key] = self.UNOPENED
            return 'unopened'
        self.cures[key] = dict(count=count + 1, at=now)
        self.log({'native_pad_cure': pad.name, 'id': key, 'side': side, 'attempt': count + 1})
        for node in pad.nodes:
            self.host.lock(node, node_identity(node))
        self.vanish(key, pad, now)
        for table in (self.applied, self.delivered, self.locked, self.watch):
            table.pop(key, None)
        return 'cure'

    # -- what the Devices tab and `dosplex control devices` show

    def rows(self, state):
        primary = state.get('primary_side', 'left')
        rows = []
        for key in sorted(self.names):
            pad = self.pads.get(key)
            requested = self.registry.target(key) if key in self.registry.devices else 'unassigned'
            side = self.applied.get(key) if pad else None
            target = ('unassigned' if side in (None, 'unassigned') else 'main' if side == primary else 'guest')
            rows.append(dict(id=key, name=pad.name if pad else self.names[key],
                             index=self.registry.devices.get(key, {}).get('index'), connected=bool(pad), target=target,
                             requested=requested, active=False, node='', backend='generic-pad',
                             transport=pad.transport if pad else '',
                             input_status=dict(shortcut_ready=bool(key in self.readers and self.readers[key].alive()),
                                               captured=False),
                             routing_error=self.errors.get(key, '') if pad else ''))
        return rows

    def close(self):
        for reader in list(self.readers.values()):
            reader.stop()
        self.readers.clear()
        self.holds.release_all('the router is closing')       # no side is left frozen
        self.bind_due()                      # no pad is left without its driver
        if self.monitor:
            self.monitor.close()


# --- host installation and recovery (the runtime rule, the Guest bluez policy) -----------------------------------------

JOURNAL = 'native-pads.json'
BLUEZ_DIR = Path('/etc/dbus-1/system.d')


def bluez_policy(guest_user):
    """A D-Bus system bus policy that gives the Guest account no access to Bluetooth, system updates and SteamOS Manager.

    Bluetooth is managed by Main's Steam only (BlueZ is reached over D-Bus; the side has no CAP_NET_RAW, so it has no
    other way in). Guest's Steam runs with SteamOS's own flags (-steamos3) for the resolution settings, and so shows
    the System page too: Atomupd1 (updates, branch) and SteamOSManager1 (its system service takes calls from anyone on
    the bus: power profile, fan, formatting a card) are closed to Guest as well. NetworkManager, UDisks2 and logind
    stay as they are: their changes need the wheel group or an active local session, which Guest does not have."""
    if not USER_NAME.fullmatch(guest_user or ''):
        raise ValueError('invalid Guest account name')
    user = quoteattr(guest_user)
    return ('<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"\n'
            ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n'
            '<!-- Owned by DosPlex; removed by recovery. Guest\'s Steam must not pair, forget or configure Bluetooth devices, '
            'update the system or use SteamOS Manager. -->\n'
            '<busconfig>\n  <policy user=%s>\n    <deny own_prefix="org.bluez"/>\n    <deny send_destination="org.bluez"/>\n'
            '    <deny receive_sender="org.bluez"/>\n'
            '    <deny send_destination="com.steampowered.Atomupd1"/>\n'
            '    <deny send_destination="com.steampowered.SteamOSManager1"/>\n'
            '  </policy>\n</busconfig>\n' % user)


def write_file(path, content):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'wb') as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def reload_system(run=subprocess.run, reload_dbus=True, reload_udev=True):
    """Tell udev and the system bus to read their configuration again. Returns what failed, as a list of names."""
    failed = []
    for name, command in (('udev', ['udevadm', 'control', '--reload-rules']),
                          ('dbus', ['busctl', '--system', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                    'org.freedesktop.DBus', 'ReloadConfig'])):
        if (name == 'udev' and not reload_udev) or (name == 'dbus' and not reload_dbus):
            continue
        try:
            if run(command, timeout=5, capture_output=True).returncode:
                failed.append(name)
        except (OSError, subprocess.SubprocessError):
            failed.append(name)
    return failed


POLICY_NAME = re.compile(r'dosplex-([a-f0-9]{32})-guest-no-bluez\.conf$')
STALE_POLICY_TEXT = re.compile(
    r'\s*<!DOCTYPE busconfig [^>]*>\s*(?:<!--.*?-->\s*)*<busconfig>\s*<policy user="[a-z_][a-z0-9_-]{0,31}">\s*'
    r'(?:(?:<deny [^<>]*/>|<!--.*?-->)\s*)+</policy>\s*</busconfig>\s*', re.S)


def journal_add(root, files):
    """Record files in the session journal, keeping entries already there. Returns the record."""
    path = Path(root) / JOURNAL
    record = json.loads(path.read_text()) if path.exists() else dict(files={})
    record['files'].update({path: hashlib.sha256(content).hexdigest() for path, content in files.items()})
    output = module('output_delivery', 'native-output-delivery.py')
    output.save_delivery_ledger(path, record)
    return record


def sweep_stale(dbus_dir=BLUEZ_DIR, run_dir=Path('/run'), keep=None, run=subprocess.run):
    """Remove the policy files of sessions whose folder is gone (a hard restart wiped it). Returns {'removed': [...], 'kept': [...]}."""
    removed, kept = [], []
    try:
        entries = sorted(Path(dbus_dir).iterdir())
    except OSError:
        return dict(removed=removed, kept=kept)
    for entry in entries:
        match = POLICY_NAME.fullmatch(entry.name)
        if not match:
            continue
        session_id = match.group(1)
        if session_id == keep:
            continue
        if (Path(run_dir) / ('dosplex-trial-' + session_id)).is_dir():
            continue
        try:
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode):
                kept.append(str(entry))
                continue
            text = entry.read_text()
        except OSError:
            kept.append(str(entry))
            continue
        # Ours by its name, in a folder only root writes, and by what it is: one policy for one account that only
        # denies. Earlier releases wrote the same thing with other wording (two such files were on a Steam Deck on
        # 2026-11-19, keeping Guest from Bluetooth with no session running). Removing a deny-only file can take
        # nothing away from anyone; a file that grants something, or holds anything else, is not ours and stays.
        if info.st_size > 4096 or not STALE_POLICY_TEXT.fullmatch(text):
            kept.append(str(entry))
            continue
        try:
            entry.unlink()
            removed.append(str(entry))
        except OSError:
            kept.append(str(entry))
    if removed:
        reload_system(run, reload_udev=False)
    return dict(removed=removed, kept=kept)


def policy_in_place(root, guest_user, dbus_dir=BLUEZ_DIR):
    """Whether this session's Guest policy file is there with exactly the text it should have (install_policy wrote it
    and the bus took it, before the compositor started). True too when there is no Guest account to keep out."""
    if guest_user is None:
        return True
    policy = Path(dbus_dir) / ('dosplex-' + Path(root).name.removeprefix('dosplex-trial-') + '-guest-no-bluez.conf')
    try:
        return stat.S_ISREG(policy.lstat().st_mode) and policy.read_bytes() == bluez_policy(guest_user).encode()
    except OSError:
        return False


def install_policy(root, guest_user, dbus_dir=BLUEZ_DIR, run_dir=Path('/run'), run=subprocess.run):
    """Write the Guest bluez policy before the compositor starts: sweep stale policies, journal first, write, reload.

    Returns the policy path as a string; None when guest_user is None (a session without a Guest account)."""
    root = Path(root)
    if not re.fullmatch('dosplex-trial-[a-f0-9]{32}', root.name):
        raise ValueError('invalid pads owner')
    if guest_user is None:
        return None
    session_id = root.name.removeprefix('dosplex-trial-')
    sweep_stale(dbus_dir=dbus_dir, run_dir=run_dir, keep=session_id, run=run)
    policy = Path(dbus_dir) / ('dosplex-' + session_id + '-guest-no-bluez.conf')
    journal_add(root, {str(policy): bluez_policy(guest_user).encode()})
    content = bluez_policy(guest_user).encode()
    try:
        Path(policy).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_file(policy, content)
    except OSError as err:
        raise RuntimeError('the policy that keeps Bluetooth from Guest could not be written: ' + str(err))
    if 'dbus' in reload_system(run, reload_udev=False):
        time.sleep(0.5)
        if 'dbus' in reload_system(run, reload_udev=False):
            try:
                policy.unlink()
            except OSError:
                pass
            raise RuntimeError('the system bus did not take the policy that keeps Bluetooth from Guest')
    return str(policy)


def install(root, guest_user, rules_dir=Path('/run/udev/rules.d'), dbus_dir=BLUEZ_DIR, run=subprocess.run):
    """Write the runtime udev rule and the Guest bluez policy, journaled first so that recovery can remove them.

    Returns {'rule': path, 'policy': path, 'reload_failed': [...]}. The rule's name carries the trial id."""
    root = Path(root)
    if not re.fullmatch('dosplex-trial-[a-f0-9]{32}', root.name):
        raise ValueError('invalid pads owner')
    rule = Path(rules_dir) / ('73-' + root.name + '-pads.rules')
    policy = Path(dbus_dir) / ('dosplex-' + root.name.removeprefix('dosplex-trial-') + '-guest-no-bluez.conf')
    files = {str(rule): rule_text(root.name).encode()}
    if guest_user is not None:                      # (a transaction without a Guest account has no one to keep from Bluetooth)
        files[str(policy)] = bluez_policy(guest_user).encode()
    journal_add(root, files)
    for path, content in files.items():
        target = Path(path)
        if target.is_file():
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_file(target, content)
    return dict(rule=str(rule), policy=str(policy) if guest_user is not None else None, reload_failed=reload_system(run))


def recover(root, run=subprocess.run, trigger=trigger_change):
    """Remove what install() wrote (a file that someone changed is kept and reported), reload, and let udev re-apply its own
    rules to the pad nodes that are there. Returns the list of files kept."""
    root = Path(root)
    path = root / JOURNAL
    if not path.exists():
        return []
    info = path.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise PermissionError('untrusted pads journal')
    record = json.loads(path.read_text())
    kept = []
    for name, digest in record['files'].items():
        file = Path(name)
        if not file.exists():
            continue
        if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            kept.append(name)
            continue
        file.unlink()
    reload_system(run)
    trigger([str(entry) for entry in sorted((SYS / 'class/hidraw').glob('hidraw*'))]
            + [str(entry) for entry in sorted((SYS / 'class/input').glob('event*'))])
    if not kept:
        path.unlink()
    return kept


def probe_bluez(users, run=subprocess.run):
    """On the Machine, as root: ask BlueZ as each account. {user: 'ok' | 'denied' | 'error: ...'}. Main's must be ok, Guest's denied."""
    import pwd
    results = {}
    for user in users:
        account = pwd.getpwnam(user)
        result = run(['busctl', '--system', 'call', 'org.bluez', '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'],
                     user=account.pw_uid, group=account.pw_gid, extra_groups=[], capture_output=True, text=True, timeout=10)
        text = (result.stderr or '') + (result.stdout or '')
        results[user] = ('ok' if result.returncode == 0 else
                         'denied' if 'AccessDenied' in text or 'Access denied' in text or 'not allowed' in text.lower() else
                         'error: ' + text.strip()[:160])
    return results


def main(argv):
    if argv[:1] == ['--list']:
        print(json.dumps([pad.describe() for pad in discover()], indent=2))
    elif argv[:1] == ['--probe-bluez'] and len(argv) >= 2 and os.geteuid() == 0:
        print(json.dumps(probe_bluez(argv[1:]), indent=2))
    elif argv[:1] == ['--rule']:
        print(rule_text(), end='')
    else:
        raise SystemExit('usage: native-pads.py --list | --rule | --probe-bluez USER...   (--probe-bluez needs root)')


if __name__ == '__main__':
    main(sys.argv[1:])
