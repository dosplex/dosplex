#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Grab external gamepads and expose one routed virtual gamepad.

Steam keeps existing evdev/hidraw file descriptors open across chmod/chgrp
changes, so Dosplex cannot reliably move a physical controller between Main and
Guest with permissions alone. This daemon owns external joystick event devices,
grabs them, hides the physical family, and forwards events into a uinput clone
whose device node is made readable only by the selected side. Changing sides
destroys the old clone and creates a new one, which makes Steam see a real
unplug/hotplug instead of a silent permission change.
"""
import asyncio
import base64
import contextlib
import errno
import json
import importlib.util
import socket
import stat
import signal
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from evdev import InputDevice, UInput, ecodes, list_devices

_menu_spec = importlib.util.spec_from_file_location("dosplex_controller_menu", Path(__file__).with_name("dosplex-controller-menu.py"))
_menu_module = importlib.util.module_from_spec(_menu_spec)
_menu_spec.loader.exec_module(_menu_module)
ControllerMenu = _menu_module.ControllerMenu

SYS_INPUT_DIR = Path("/sys/class/input")
SYS_HIDRAW_DIR = Path("/sys/class/hidraw")
STATE_PATH = Path(os.environ.get("DOSPLEX_STATE_PATH", "/run/dosplex/state.json"))
_registry_spec = importlib.util.spec_from_file_location("controller_registry", Path(__file__).with_name("dosplex-controller-registry.py"))
_registry_module = importlib.util.module_from_spec(_registry_spec)
_registry_spec.loader.exec_module(_registry_module)
REGISTRY = None
ROUTE_DIR = Path("/run/dosplex/gamepad-routes")
IGNORE_NAMES = ("Valve Software Steam Controller", "Steam Deck", "Dosplex routed")
TARGET_GROUP = {"main": os.environ.get("DOSPLEX_MAIN_GROUP", "deck"), "guest": os.environ.get("DOSPLEX_GUEST_GROUP", "guest")}


def log(msg):
    print(f"[dosplex-gamepad-arbiter] {msg}", flush=True)


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", base_name(name or ""))


def base_name(name):
    out = name or ""
    for suffix in (" Touchpad", " Motion Sensors", " Accelerometer", " Gyro"):
        if suffix in out:
            out = out.split(suffix, 1)[0]
    return out


def read_target(name):
    state = runtime_state()
    # Only Full screen overrides the saved assignment; Guest full screen ("guest") keeps it, as a split does.
    if state is not None and state.get("layout") == "single":
        return "main"  # temporary effective target; preserve the saved assignment
    try:
        value = (ROUTE_DIR / safe_name(name)).read_text(encoding="utf-8").strip()
    except OSError:
        value = "main"
    return "guest" if value == "guest" else "main"


def udev_props(dev_path):
    result = subprocess.run(
        ["udevadm", "info", "--query=property", f"--name={dev_path}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    props = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def input_phys(path):
    event = Path(path).name
    phys_file = Path("/sys/class/input") / event / "device" / "phys"
    try:
        return phys_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_external_gamepad(path):
    if input_phys(path).startswith("dosplex-gamepad-arbiter"):
        return False
    try:
        dev = InputDevice(path)
        name = dev.name or ""
        vendor = dev.info.vendor
        product = dev.info.product
        buttons = dev.capabilities().get(ecodes.EV_KEY, [])
        dev.close()
    except OSError:
        return False
    # Steam Input creates game-facing virtual Xbox pads with Valve's vendor ID.
    # Those must stay visible to the game session; only physical external pads
    # should be owned by the Dosplex arbiter.
    if vendor == 0x28DE and product == 0x11FF:
        return False
    if any(token in name for token in IGNORE_NAMES):
        return False
    if ecodes.BTN_GAMEPAD not in buttons and ecodes.BTN_JOYSTICK not in buttons:
        return False
    props = udev_props(path)
    return props.get("ID_INPUT_JOYSTICK") == "1"



def set_owner(dev_path, target):
    group = TARGET_GROUP[target]
    # udev may still be applying generic joystick rules just after uinput
    # creation. Re-apply ownership a few times so the routed owner wins.
    for _ in range(6):
        set_path_owner(dev_path, group, "0660")
        time.sleep(0.08)
    log(f"{dev_path} -> root:{group}")


def device_name(path):
    event = Path(path).name
    name_file = Path("/sys/class/input") / event / "device" / "name"
    try:
        return name_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def physical_family(sys_device):
    """Identify a live kernel HID family, never a model-name match.

    Non-HID event devices fall back to their exact resolved input device.
    This is a live topology identity, not a persistent reconnect identifier.
    """
    try:
        device = sys_device.resolve(strict=True)
        for parent in (device, *device.parents):
            try:
                fields = (parent / "uevent").read_text().splitlines()
            except OSError:
                continue
            if any(line.startswith("HID_ID=") for line in fields):
                return parent
        return device
    except OSError:
        return None


def related_input_paths(family):
    if family is None:
        return []
    return ["/dev/input/" + entry.parent.name
            for entry in sorted(SYS_INPUT_DIR.glob("event*/device"))
            if physical_family(entry) == family]


def related_hidraw_paths(family):
    if family is None:
        return []
    return ["/dev/" + entry.parent.name
            for entry in sorted(SYS_HIDRAW_DIR.glob("hidraw*/device"))
            if physical_family(entry) == family]


def set_path_owner(dev_path, group, mode, missing_ok=False):
    if missing_ok and not Path(dev_path).exists():
        return
    subprocess.run(["setfacl", "-b", dev_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["chown", f"root:{group}", dev_path], check=True)
    subprocess.run(["chmod", mode, dev_path], check=True)


class PhysicalPermissions:
    """Keep original inode metadata/ACLs for this live routing transaction."""
    ACL = "system.posix_acl_access"

    def __init__(self):
        self.saved = {}

    def capture(self, path):
        if path in self.saved:
            current = os.stat(path, follow_symlinks=False)
            original = self.saved[path][1]
            if (current.st_dev, current.st_ino, current.st_rdev) != (original.st_dev, original.st_ino, original.st_rdev):
                raise RuntimeError("physical controller node was replaced")
            return
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISCHR(metadata.st_mode):
                raise RuntimeError("physical controller node is not a character device")
            try:
                acl = os.getxattr(fd, self.ACL)
            except OSError as error:
                if error.errno not in (errno.ENODATA, errno.EOPNOTSUPP):
                    raise
                acl = None
            journal = os.environ.get('DOSPLEX_CONTROLLER_RECOVERY')
            if journal:
                records = _registry_module.read_json(Path(journal)) if Path(journal).exists() else {}
                records[path] = dict(identity=[metadata.st_dev, metadata.st_ino, metadata.st_rdev],
                    uid=metadata.st_uid, gid=metadata.st_gid, mode=stat.S_IMODE(metadata.st_mode),
                    acl=base64.b64encode(acl).decode() if acl is not None else None)
                _registry_module.write_json(Path(journal), records)
            self.saved[path] = (fd, metadata, acl)
        except BaseException:
            os.close(fd)
            raise

    def lock(self, paths):
        # Snapshot all nodes before changing the first. Open descriptors pin
        # the original inodes across unplug and event-number reuse.
        for path in sorted(set(paths)):
            self.capture(path)
        for path in sorted(set(paths)):
            fd = self.saved[path][0]
            os.fchown(fd, 0, 0)
            try:
                os.removexattr(fd, self.ACL)
            except OSError as error:
                if error.errno not in (errno.ENODATA, errno.EOPNOTSUPP):
                    raise
            os.fchmod(fd, 0o600)

    def restore(self):
        errors = []
        for path, (fd, metadata, acl) in list(self.saved.items()):
            try:
                os.fchown(fd, metadata.st_uid, metadata.st_gid)
                os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
                if acl is None:
                    try:
                        os.removexattr(fd, self.ACL)
                    except OSError as error:
                        if error.errno not in (errno.ENODATA, errno.EOPNOTSUPP):
                            raise
                else:
                    os.setxattr(fd, self.ACL, acl)
                journal = os.environ.get('DOSPLEX_CONTROLLER_RECOVERY')
                if journal and Path(journal).exists():
                    records = _registry_module.read_json(Path(journal)); records.pop(path, None)
                    _registry_module.write_json(Path(journal), records)
            except OSError as error:
                errors.append(f"{path}: {error}")
            finally:
                os.close(fd)
                del self.saved[path]
        if errors:
            raise RuntimeError("physical permission restoration failed: " + "; ".join(errors))


def runtime_state():
    try:
        fd = os.open(STATE_PATH, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536
                    or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022
                    or metadata.st_nlink != 1):
                return None
            data = os.read(fd, 65537)
            if len(data) > 65536:
                return None
            state = json.loads(data)
        finally:
            os.close(fd)
        if (not isinstance(state, dict) or type(state.get("overlay_visible")) is not bool
                or state.get("primary_side") not in ("left", "right")
                or state.get("guest_side") not in ("left", "right")
                or state["primary_side"] == state["guest_side"]
                or state.get("overlay_owner") not in ("left", "right")
                or state.get("output_connected") is not True):
            return None
        return state
    except (OSError, ValueError, TypeError):
        return None


def overlay_state_for_target(target):
    if target not in ("main", "guest"):
        return None
    state = runtime_state()
    if state is None:
        return None
    side = state["guest_side" if target == "guest" else "primary_side"]
    owned_visible = state["overlay_visible"] and state["overlay_owner"] == side
    return side, owned_visible, not state["overlay_visible"] or owned_visible


def overlay_capture_required(target):
    state = overlay_state_for_target(target)
    return state is None or state[1]


class GameInputGate:
    """Neutralize game state during capture and require neutral hand-back."""
    def __init__(self, ui, axes, held_keys=()):
        self.ui = ui
        # code -> (current, neutral, tolerance)
        self.axes = {code: value for code, (value, _, _) in axes.items()}
        self.neutral_axes = {code: (neutral, tolerance) for code, (_, neutral, tolerance) in axes.items()}
        self.keys = set(held_keys)
        self.game_keys = set()
        self.game_axes = dict(self.axes)  # uinput inherits absolute axis values
        self.captured = True
        self.desynced = False
        self.draining = True
        self.neutralize()

    def neutralize(self):
        for code in self.game_keys:
            self.ui.write(1, code, 0)
        self.game_keys.clear()
        for code, value in list(self.game_axes.items()):
            neutral = self.neutral_axes[code][0]
            if value != neutral:
                self.ui.write(3, code, neutral)
            self.game_axes[code] = neutral
        self.ui.syn()

    def neutral(self):
        return not self.keys and all(abs(self.axes[code] - neutral) <= tolerance
                                     for code, (neutral, tolerance) in self.neutral_axes.items())

    def capture(self, active):
        active = active or self.desynced
        if active and not self.captured:
            self.neutralize()
            self.draining = True
        self.captured = active

    def finish_batch(self):
        # Only hand back after the queued batch has drained. Otherwise a quick
        # menu tap queued before closing can be replayed into gameplay.
        if not self.captured and self.neutral():
            self.draining = False

    def forward(self, event, suppress=False):
        if event.type == 0 and event.code == 3:  # SYN_DROPPED
            self.desynced = True
            self.capture(True)
            return
        if self.desynced:
            return
        if event.type == 1:
            if event.value:
                self.keys.add(event.code)
            else:
                self.keys.discard(event.code)
        elif event.type == 3 and event.code in self.axes:
            self.axes[event.code] = event.value
        if suppress:
            return
        self.emit(event)

    def emit(self, event):
        if self.captured or self.draining or self.desynced:
            return
        self.ui.write_event(event)
        if event.type == 1:
            if event.value:
                self.game_keys.add(event.code)
            else:
                self.game_keys.discard(event.code)
        elif event.type == 3 and event.code in self.axes:
            self.game_axes[event.code] = event.value


class OpeningChord:
    """Reserve the configured controller chord before gameplay sees it."""
    def __init__(self, gate, open_menu, dpad_keys=False):
        self.dpad_keys = dpad_keys
        self.gate, self.open_menu = gate, open_menu
        self.shortcut, self.hold_seconds = _menu_module.shortcut_settings()
        self.held = set()
        self.pending = None
        self.deadline = None
        self.joined_at = None
        self.waiting_since = None
        self.passthrough = False
        self.cancelled = False

    @property
    def capturing(self):
        return self.joined_at is not None or self.waiting_since is not None

    def flush(self):
        if self.pending is not None:
            self.gate.emit(self.pending)
            self.gate.ui.syn()
            self.pending = None
        self.passthrough = True

    def tick(self, now):
        if self.shortcut != 'dpad_up_back' and self.pending is not None and self.joined_at is None and now >= self.deadline:
            self.flush()
        if self.joined_at is not None and not self.cancelled and self.waiting_since is None and now - self.joined_at >= self.hold_seconds:
            self.waiting_since = now
            self.open_menu()
        if self.waiting_since is not None and now - self.waiting_since >= 1:
            raise RuntimeError("overlay did not acknowledge controller opening shortcut")

    def event(self, event, now):
        self.tick(now)
        member = _menu_module.shortcut_event(event.type, event.code, event.value, self.shortcut, self.dpad_keys)
        if member is None:
            return False
        key, pressed = member
        was_held = key in self.held
        if pressed:
            self.held.add(key)
        else:
            self.held.discard(key)
        # A downward hat event is ordinary input, not an Up release.
        if not pressed and not was_held and not self.capturing and self.pending is None:
            return False
        if self.passthrough:
            if not self.held:
                self.passthrough = False
            return False
        if pressed and not was_held and self.pending is None and not self.capturing:
            if self.shortcut == 'dpad_up_back' and key != _menu_module.START:
                self.passthrough = True
                return False
            self.pending, self.deadline = event, now + .18
        elif pressed and not was_held and len(self.held) == 2 and self.joined_at is None:
            self.joined_at = now
            self.pending = None
            self.gate.capture(True)
        elif not pressed and self.pending is not None:
            self.flush()
            if not self.held:
                self.passthrough = False
            return False
        if not pressed and self.joined_at is not None:
            self.cancelled = True
        self.gate.forward(event, suppress=True)
        if not self.held and self.waiting_since is None:
            self.joined_at = None
            self.cancelled = False
        return True



def virtual_identity(info, name, phys, normalized=False):
    # Never label an unchanged PlayStation/Nintendo layout as Xbox. Steam Input
    # must receive the identity matching its actual evdev capabilities.
    return dict(name=name, vendor=0x045e if normalized else info.vendor,
                product=0x028e if normalized else info.product,
                version=0x0114 if normalized else info.version,
                bustype=0x03 if normalized else info.bustype, phys=phys)


def neutral_axis(info, code, axis):
    sony = info.vendor == 0x054c
    sticks = (0, 1, 2, 5) if sony else (0, 1, 3, 4)
    if 16 <= code <= 23 and axis.min == -1 and axis.max == 1:
        return 0, 0
    if code in sticks:
        return (axis.min + axis.max + 1) // 2, max(1, (axis.max-axis.min)//10)
    return axis.min, max(0, axis.flat)

class SteamControllerOutput:
    """Translate the wireless Steam Controller's evdev layout to Xbox controls.

    Raw touchpad axes are not Xbox hats. Native touchpad/gyro forwarding and
    force feedback are outside this adapter's supported interface.
    """
    KEYS = (304, 305, 307, 308, 310, 311, 314, 315, 316, 317, 318)
    AXES = {0: 0, 1: 1, 3: 3, 4: 4, 20: 2, 21: 5}

    @classmethod
    def capabilities(cls, device):
        axes = []
        for code, info in device.capabilities(absinfo=True).get(3, []):
            if code in cls.AXES:
                axes.append((cls.AXES[code], info))
        axes.extend([(16, (0, -1, 1, 0, 0, 0)), (17, (0, -1, 1, 0, 0, 0))])
        return {1: list(cls.KEYS), 3: axes}

    def __init__(self, ui):
        self.ui = ui
        self.device = ui.device
        self.dpad = set()

    def write(self, kind, code, value):
        if kind == 1 and code in (544, 545, 546, 547):
            if value: self.dpad.add(code)
            else: self.dpad.discard(code)
            axis = 17 if code in (544, 545) else 16
            position = (int(545 in self.dpad)-int(544 in self.dpad)) if axis == 17 else (int(547 in self.dpad)-int(546 in self.dpad))
            self.ui.write(3, axis, position)
        elif kind == 1 and code in self.KEYS:
            self.ui.write(kind, code, value)
        elif kind == 3 and code in self.AXES:
            self.ui.write(kind, self.AXES[code], value)
        elif kind == 0:
            self.ui.write(kind, code, value)

    def write_event(self, event): self.write(event.type, event.code, event.value)
    def syn(self): self.ui.syn()
    def close(self): self.ui.close()


class RoutedGamepad:
    def __init__(self, path):
        self.path = path
        self.family = physical_family(SYS_INPUT_DIR / Path(path).name / "device")
        if self.family is None:
            raise RuntimeError("controller topology is unavailable")
        self.dev = None
        self.permissions = PhysicalPermissions()
        probe = InputDevice(path)
        self.name = probe.name or Path(path).name
        self.base = base_name(self.name)
        self.key = _registry_module.controller_id(probe.info, probe.uniq, probe.phys)
        self.last_pressed = 0
        self.input_counts = dict(back=0, up=0, stick=0)
        if REGISTRY is not None: REGISTRY.observe(self.key, self.name)
        probe.close()
        self.target = None
        self.ui = None
        self.gate = None
        self.menu = None
        self.menu_side = None
        self.menu_visible = False
        self.menu_ready = False
        self.menu_ranges = {}
        self.dpad_keys = False
        self.virtual_path = None
        self.grabbed = False
        self.sibling_grabs = {}
        self.metric_latencies = []
        self.metric_batches = []
        self.metric_last_log = time.time()
        self.reader_fd = None
        self.loop = None
        self.forward_error = None
        self.opening = None
        if REGISTRY is not None: REGISTRY.live_controls[self.key] = self

    def open_dev(self):
        if self.dev is None:
            self.dev = InputDevice(self.path)

    def close_dev(self):
        self.remove_reader()
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def close_virtual(self):
        if self.ui:
            self.ui.close()
            self.ui = None
            self.gate = None
            self.menu = None
            self.menu_visible = False
            self.opening = None
            self.virtual_path = None

    def grab_siblings(self):
        # Permission changes cannot revoke Gamescope/libinput's existing fds.
        # Hold the exact physical family's fallback mouse/keyboard endpoints too.
        for path in related_input_paths(self.family):
            if path == self.path or path in self.sibling_grabs:
                continue
            device = InputDevice(path)
            try:
                if physical_family(SYS_INPUT_DIR / Path(path).name / 'device') != self.family:
                    raise RuntimeError('controller sibling topology changed')
                device.grab()
            except BaseException:
                device.close()
                raise
            self.sibling_grabs[path] = device

    def release_siblings(self):
        for device in self.sibling_grabs.values():
            try:
                device.ungrab()
            except OSError:
                pass
            finally:
                device.close()
        self.sibling_grabs.clear()

    def restore_physical_permissions(self):
        self.permissions.restore()

    def ensure_virtual(self, target):
        if (self.ui or target == "unassigned" and self.dev is not None and self.grabbed) and self.target == target:
            return
        previous = self.target
        self.close_virtual()
        if previous and previous != target:
            # Steam can miss a rapid close/recreate when uinput reuses the same
            # event node. Give both Steam instances a clear unplug boundary.
            time.sleep(1.0)
        self.open_dev()
        if not self.grabbed:
            self.dev.grab()
            self.grabbed = True
        self.grab_siblings()
        self.permissions.lock(related_input_paths(self.family) +
                              related_hidraw_paths(self.family) + [self.path])
        if target == "unassigned":
            self.target = target
            self.ensure_reader()
            return
        virtual_name = f"Dosplex routed {target} {self.base}"
        virtual_phys = "dosplex-gamepad-arbiter/" + uuid.uuid4().hex
        wireless_steam = (self.dev.info.vendor, self.dev.info.product) in ((0x28de, 0x1304), (0x28de, 0x1305))
        identity = virtual_identity(self.dev.info, virtual_name, virtual_phys, wireless_steam)
        if wireless_steam:
            self.ui = SteamControllerOutput(UInput(SteamControllerOutput.capabilities(self.dev), **identity))
        else:
            self.ui = UInput.from_device(self.dev, **identity)
        try:
            # The UInput handle owns this node. Searching by display name can
            # select another identical controller's clone.
            node = self.ui.device
            if node is None or node.phys != virtual_phys:
                raise RuntimeError("routed controller identity was not confirmed")
            self.virtual_path = node.path
            # Ownership changes may take >500 ms. Discard queued setup input
            # only after them, then snapshot current state before forwarding.
            set_owner(self.virtual_path, target)
            self.resynchronize()
        except Exception:
            # Never leave a clone forwarding after an incomplete ownership
            # change. run() also releases the physical grab on this failure.
            self.close_virtual()
            raise
        self.target = target
        transition = f"{previous or 'none'}->{target}" if previous != target else target
        log(
            f"{self.path} {self.name} {transition} via {self.virtual_path or virtual_name} "
            f"id={self.dev.info.bustype:04x}:{self.dev.info.vendor:04x}:{self.dev.info.product:04x}"
        )
        self.ensure_reader()
        self.metric_latencies.clear()
        self.metric_batches.clear()
        self.metric_last_log = time.time()

    def resynchronize(self):
        if self.gate is not None:
            self.gate.neutralize()
        # SYN_DROPPED invalidates queued transitions. Read through the backlog
        # and rebuild from EVIOCG state; never replay it or unplug the clone.
        for _ in range(64):
            try:
                if not list(self.dev.read()): break
            except BlockingIOError:
                break
        else:
            raise RuntimeError('controller input queue did not drain')
        wireless_steam = isinstance(self.ui, SteamControllerOutput)
        capabilities = self.dev.capabilities(absinfo=True)
        axes = {}
        for code, info in capabilities.get(3, []):
            if wireless_steam and code in (16, 17, 18, 19): continue
            neutral, tolerance = neutral_axis(self.dev.info, code, info)
            axes[code] = (info.value, neutral, tolerance)
        self.gate = GameInputGate(self.ui, axes, self.dev.active_keys())
        self.dpad_keys = 544 in self.dev.capabilities().get(1, [])
        self.menu_ranges = {code: (info.min, info.max) for code, info in capabilities.get(3, []) if code in (0, 1)}
        self.opening = None
        self.menu = None
        self.menu_visible = self.menu_ready = False
        self.resync_count = getattr(self, 'resync_count', 0) + 1

    def record_metrics(self, events):
        now = time.time()
        non_syn = 0
        for event in events:
            if event.type == ecodes.EV_SYN:
                continue
            event_ts = event.sec + event.usec / 1000000.0
            latency_ms = (now - event_ts) * 1000.0
            if -1000.0 < latency_ms < 10000.0:
                self.metric_latencies.append(latency_ms)
                non_syn += 1
        if events:
            self.metric_batches.append(len(events))
        if len(self.metric_latencies) < 300 and now - self.metric_last_log < 10.0:
            return
        if not self.metric_latencies:
            self.metric_last_log = now
            return
        values = sorted(self.metric_latencies)
        batches = self.metric_batches[:] or [0]
        p95 = values[int((len(values) - 1) * 0.95)]
        avg = sum(values) / len(values)
        batch_avg = sum(batches) / len(batches)
        log(
            f"metrics {self.name} target={self.target} events={len(values)} "
            f"lat_ms min={values[0]:.3f} avg={avg:.3f} p95={p95:.3f} max={values[-1]:.3f} "
            f"batch_avg={batch_avg:.2f} batch_max={max(batches)}"
        )
        self.metric_latencies.clear()
        self.metric_batches.clear()
        self.metric_last_log = now

    def apply_route(self):
        target = REGISTRY.target(self.key) if REGISTRY is not None else read_target(self.name)
        if REGISTRY is None or REGISTRY.may_apply(self.key, self.target, target):
            self.ensure_virtual(target)


    def remove_reader(self):
        if self.loop is not None and self.reader_fd is not None:
            try:
                self.loop.remove_reader(self.reader_fd)
            except (OSError, ValueError):
                pass
        self.reader_fd = None

    def ensure_reader(self):
        if self.loop is None or self.dev is None:
            return
        fd = self.dev.fd
        if self.reader_fd == fd:
            return
        self.remove_reader()
        self.loop.add_reader(fd, self.dispatch_ready)
        self.reader_fd = fd

    def dispatch_ready(self):
        try:
            self.forward_ready()
        except Exception as error:
            self.forward_error = error
            self.remove_reader()
            if self.gate is not None:
                with contextlib.suppress(Exception):
                    self.gate.capture(True)

    def send_menu(self, action):
        if (self.menu_visible or action == "toggle") and self.menu_side is not None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
                control.setblocking(False)
                control.sendto(f"MENU {self.menu_side} {action}".encode(), str(STATE_PATH.parent / "hud-ipc.sock"))

    def update_menu_capture(self):
        state = overlay_state_for_target(self.target)
        side, visible, can_open = state if state is not None else (None, False, False)
        if side != self.menu_side:
            self.opening = None
        if visible and (not self.menu_visible or side != self.menu_side):
            self.menu = ControllerMenu(self.send_menu, lambda: self.menu_visible, self.menu_ranges, dpad_keys=getattr(self, "dpad_keys", False))
            self.menu_ready = self.gate.neutral()
        self.menu_side, self.menu_visible = side, visible
        if state is None or visible or not can_open:
            self.opening = None
        elif self.opening is None and not self.gate.draining and self.gate.neutral():
            self.opening = OpeningChord(self.gate, lambda: self.send_menu("toggle"), dpad_keys=getattr(self, "dpad_keys", False))
        self.gate.capture(state is None or visible or (self.opening is not None and self.opening.capturing))

    def forward_ready(self):
        dev = self.dev
        ui = self.ui
        if dev is None: return
        if self.gate is not None: self.update_menu_capture()
        events = []
        drained = False
        while True:
            try:
                batch = list(dev.read())
            except BlockingIOError:
                drained = True
                break
            if not batch:
                drained = True
                break
            events.extend(batch)
            if len(events) >= 256:
                break

        for event in events:
            counts = getattr(self, 'input_counts', None)
            if counts is not None:
                if event.type == 1 and event.value == 1 and event.code in (314, 544):
                    counts['back' if event.code == 314 else 'up'] += 1
                if event.type == 3 and event.code in (0, 1) and abs(event.value)>8000:
                    counts['stick'] += 1
            if event.type == ecodes.EV_KEY and event.value == 1: self.last_pressed = time.monotonic()
            if self.gate is None: continue
            consumed = not self.gate.desynced and self.opening is not None and self.opening.event(event, time.monotonic())
            if not consumed:
                self.gate.forward(event)
            if self.menu_visible and not self.gate.desynced:
                if not self.menu_ready:
                    self.menu_ready = self.gate.neutral()
                else:
                    self.menu.event(event.type, event.code, event.value, time.monotonic())
        if self.opening is not None and not self.gate.desynced:
            self.opening.tick(time.monotonic())
        if self.menu_visible and self.menu_ready and not self.gate.desynced:
            self.menu.tick(time.monotonic())
        if drained and self.gate is not None:
            self.gate.finish_batch()
        if events:
            self.record_metrics(events)

    async def route_monitor(self):
        while True:
            if self.forward_error is not None:
                raise RuntimeError(f"controller forwarding failed: {type(self.forward_error).__name__}: {self.forward_error}") from self.forward_error
            self.apply_route()
            if self.gate is not None:
                if self.gate.desynced:
                    self.resynchronize()
                self.forward_ready()
            await asyncio.sleep(0.02)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        monitor = asyncio.create_task(self.route_monitor())
        try:
            # Propagate routing failures to supervise(), which can report and
            # retry discovery. An unobserved monitor failure left this device
            # task alive indefinitely with no functioning route.
            await monitor
        finally:
            if REGISTRY is not None: REGISTRY.live_controls.pop(self.key, None)
            monitor.cancel()
            if not monitor.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            if self.grabbed and self.dev is not None:
                try:
                    self.dev.ungrab()
                except OSError:
                    pass
            try:
                self.close_virtual()
            finally:
                try:
                    self.close_dev()
                finally:
                    try:
                        self.release_siblings()
                    finally:
                        self.restore_physical_permissions()


def scan_external_gamepads():
    return {path for path in list_devices() if is_external_gamepad(path)}


async def supervise():
    ROUTE_DIR.mkdir(parents=True, exist_ok=True)
    tasks = {}
    controllers = {}
    try:
        while True:
            # Device discovery shells out to udevadm and can take hundreds of ms.
            # Keep it off the asyncio loop so evdev fd readers are never blocked.
            current = await asyncio.to_thread(scan_external_gamepads)
            for path in sorted(current - set(tasks)):
                gamepad = RoutedGamepad(path)
                if REGISTRY is not None and any(c.key == gamepad.key for c in controllers.values()):
                    raise RuntimeError("ambiguous controller identity")
                controllers[path] = gamepad
                tasks[path] = asyncio.create_task(gamepad.run())
            for path in list(set(tasks) - current):
                task = tasks.pop(path)
                controllers.pop(path, None)
                task.cancel()
                # Finish releasing the old reader/grab/clone before this event node
                # can be rediscovered and assigned to a new owner.
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log(f"{path} cleanup failed: {exc}")
            for path, task in list(tasks.items()):
                if task.done():
                    try:
                        task.result()
                    except Exception as exc:
                        log(f"{path} exited: {exc}")
                    del tasks[path]
                    controllers.pop(path, None)
            if REGISTRY is not None: REGISTRY.publish(controllers.values())
            await asyncio.sleep(.5)
    finally:
        for task in tasks.values():
            task.cancel()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise RuntimeError("gamepad shutdown cleanup failed") from failures[0]


async def serve():
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(supervise())
    stopping = False
    def stop():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    global REGISTRY
    REGISTRY = _registry_module.Registry(os.environ.get("DOSPLEX_CONTROLLER_DIRECTORY", "/var/lib/dosplex/controllers"), STATE_PATH.parent / "controllers.json")
    os.environ.setdefault("DOSPLEX_CONTROLLER_RECOVERY", str(REGISTRY.directory / "recovery.json"))
    _registry_module.restore_permissions(Path(os.environ["DOSPLEX_CONTROLLER_RECOVERY"]))
    managed_mode = os.environ.get('DOSPLEX_STEAM_MODE_POLICY') == 'managed'
    try:
        if managed_mode:
            _registry_module.acquire_steam_mode(REGISTRY.directory)
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    finally:
        if managed_mode:
            _registry_module.restore_steam_mode(REGISTRY.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
