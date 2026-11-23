#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Watch Steam Deck hardware controls and toggle the DosPlex overlay.

The Steam Deck exposes the hardware buttons through more than one input path.
The controller event node carries BTN_MODE and BTN_TRIGGER_HAPPY* buttons, while
SteamOS also exposes a small steamos-manager keyboard-like node for system
shortcuts. DosPlex watches both paths. The manager node is grabbed and re-emitted
so the DosPlex chord can be swallowed before Steam opens its own menu.

The Deck's own controls are Main's: the chord opens Main's overlay (taking Guest's when that is the one that is
open) and closes it again; it never opens the overlay of the side that had it last.
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import socket
from pathlib import Path

from evdev import InputDevice, UInput, ecodes, list_devices

LOG = logging.getLogger("dosplex-deck-control-shortcut")
DEFAULT_HUD_IPC_SOCK = "/run/dosplex/hud-ipc.sock"
DEFAULT_STATE_PATH = "/run/dosplex/state.json"
STEAM_BUTTON = ecodes.ecodes.get("BTN_MODE", 316)
QUICK_ACCESS_CANDIDATES = {
    ecodes.ecodes.get("BTN_TRIGGER_HAPPY1", 704),
    ecodes.ecodes.get("BTN_TRIGGER_HAPPY2", 705),
    ecodes.ecodes.get("BTN_TRIGGER_HAPPY3", 706),
    ecodes.ecodes.get("BTN_TRIGGER_HAPPY4", 707),
}
MANAGER_TRIGGER_CANDIDATES = {
    ecodes.ecodes.get("KEY_INSERT", 110),
}
CTRL_KEYS = {ecodes.ecodes.get("KEY_LEFTCTRL", 29), ecodes.ecodes.get("KEY_RIGHTCTRL", 97)}
SHIFT_KEYS = {ecodes.ecodes.get("KEY_LEFTSHIFT", 42), ecodes.ecodes.get("KEY_RIGHTSHIFT", 54)}
M_KEYS = {ecodes.ecodes.get("KEY_M", 50)}
WATCH_DEVICE_NAMES = {"Steam Deck", "Valve Software Steam Controller", "steamos-manager"}
GRAB_DEVICE_NAMES = {"steamos-manager"}


def evdev_name(code: int) -> str:
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code) or str(code)
    if isinstance(name, (list, tuple)):
        return str(name[0])
    return str(name)


def parse_code_set(value: str | None, defaults: set[int]) -> set[int]:
    if not value:
        return defaults
    out: set[int] = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        if token.isdigit():
            out.add(int(token))
            continue
        code = ecodes.ecodes.get(token)
        if code is None:
            LOG.warning("Unknown evdev code in config: %s", token)
            continue
        out.add(code)
    return out or defaults


class OverlayToggle:
    def __init__(self, ipc_path: str, state_path: str) -> None:
        self.ipc_path = ipc_path
        self.state_path = state_path

    def _state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _main_side(self, state: dict) -> str:
        """The side that is Main's, wherever it sits: the Deck's own controls are Main's."""
        return "right" if state.get("primary_side") == "right" else "left"

    def toggle(self, reason: str) -> None:
        state = self._state()
        main = self._main_side(state)
        # Main's chord closes Main's overlay and otherwise opens it, taking Guest's when that is the one that is
        # open (the compositor decides by role: Main's overlay always takes precedence over Guest's). It
        # never reopens the side that had the overlay last, which after Guest used it would be Guest's.
        main_is_open = bool(state.get("overlay_visible")) and state.get("overlay_owner") == main
        cmd = "OVERLAY close" if main_is_open else f"OVERLAY {main}"
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.sendto(cmd.encode("ascii"), self.ipc_path)
            sock.close()
            LOG.info("%s: sent %s", reason, cmd)
        except OSError as exc:
            LOG.warning("%s: cannot send overlay IPC to %s: %s", reason, self.ipc_path, exc)


class ShortcutState:
    def __init__(self, toggle: OverlayToggle, qam_codes: set[int], manager_codes: set[int]) -> None:
        self.toggle = toggle
        self.qam_codes = qam_codes
        self.manager_codes = manager_codes
        self.steam_down = False
        self.ctrl_down = False
        self.shift_down = False
        self.armed = True

    def key(self, device_name: str, code: int, value: int) -> bool:
        pressed = value != 0
        if code == STEAM_BUTTON:
            self.steam_down = pressed
            if not pressed:
                self.armed = True
            return False
        if code in CTRL_KEYS:
            self.ctrl_down = pressed
            if not pressed:
                self.armed = True
            return False
        if code in SHIFT_KEYS:
            self.shift_down = pressed
            if not pressed:
                self.armed = True
            return False

        if not pressed or not self.armed:
            return False

        if self.steam_down and code in self.qam_codes:
            self.armed = False
            self.toggle.toggle(f"{device_name}: BTN_MODE+{evdev_name(code)}")
            return True

        if device_name == "steamos-manager" and code in self.manager_codes:
            self.armed = False
            self.toggle.toggle(f"{device_name}: {evdev_name(code)}")
            return True

        if self.ctrl_down and self.shift_down and code in M_KEYS:
            self.armed = False
            self.toggle.toggle(f"{device_name}: Ctrl+Shift+M")
            return True

        return False


def device_caps(dev: InputDevice) -> list[int]:
    try:
        return dev.capabilities().get(ecodes.EV_KEY, [])
    except OSError:
        return []


def matching_devices(qam_codes: set[int], manager_codes: set[int]) -> list[InputDevice]:
    devices: list[InputDevice] = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            name = dev.name or ""
            caps = device_caps(dev)
        except OSError:
            continue
        if name in WATCH_DEVICE_NAMES:
            devices.append(dev)
            continue
        # Do not watch external gamepads just because they expose BTN_MODE.
        # The gamepad arbiter owns those devices, and the DosPlex overlay
        # shortcut is only intended for Deck hardware/steamos-manager paths.
    return devices


def should_grab(dev: InputDevice, args: argparse.Namespace) -> bool:
    if not args.grab_manager:
        return False
    return (dev.name or "") in GRAB_DEVICE_NAMES


def make_passthrough(dev: InputDevice) -> UInput | None:
    try:
        return UInput.from_device(dev, name=f"DosPlex passthrough {dev.name or Path(dev.path).name}")
    except Exception as exc:
        LOG.warning("Cannot create passthrough device for %s (%s): %s", dev.name, dev.path, exc)
        return None


async def watch_device(dev: InputDevice, state: ShortcutState, args: argparse.Namespace) -> None:
    grabbed = should_grab(dev, args)
    passthrough = None
    if grabbed:
        passthrough = make_passthrough(dev)
        try:
            dev.grab()
            LOG.info("Grabbed %s (%s) with passthrough=%s", dev.name, dev.path, bool(passthrough))
        except OSError as exc:
            grabbed = False
            LOG.warning("Cannot grab %s (%s): %s", dev.name, dev.path, exc)
    else:
        LOG.info("Watching %s (%s)", dev.name, dev.path)

    try:
        async for event in dev.async_read_loop():
            swallowed = False
            if event.type == ecodes.EV_KEY:
                swallowed = state.key(dev.name or dev.path, event.code, event.value)
                if event.value != 2:
                    LOG.debug("%s %s=%s swallowed=%s", dev.name, evdev_name(event.code), event.value, swallowed)
            if grabbed and passthrough and not swallowed:
                passthrough.write_event(event)
                if event.type != ecodes.EV_SYN:
                    passthrough.syn()
    finally:
        if grabbed:
            with contextlib.suppress(Exception):
                dev.ungrab()
        if passthrough:
            passthrough.close()


async def main_async(args: argparse.Namespace) -> int:
    qam_codes = parse_code_set(args.quick_access_codes, QUICK_ACCESS_CANDIDATES)
    manager_codes = parse_code_set(args.manager_trigger_codes, MANAGER_TRIGGER_CANDIDATES)
    LOG.info("Quick Access candidate codes: %s", ", ".join(evdev_name(c) for c in sorted(qam_codes)))
    LOG.info("Manager trigger codes: %s", ", ".join(evdev_name(c) for c in sorted(manager_codes)))
    toggle = OverlayToggle(args.ipc_socket, args.state_path)
    state = ShortcutState(toggle, qam_codes, manager_codes)

    while True:
        devices = matching_devices(qam_codes, manager_codes)
        if devices:
            tasks = [asyncio.create_task(watch_device(dev, state, args)) for dev in devices]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    LOG.warning("Device watcher exited: %s", exc)
            for task in pending:
                task.cancel()
        else:
            LOG.warning("No Steam Deck hardware input devices found")
        await asyncio.sleep(2.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Toggle DosPlex overlay from Steam Deck hardware controls")
    parser.add_argument("--ipc-socket", default=os.environ.get("DOSPLEX_HUD_IPC_SOCK", DEFAULT_HUD_IPC_SOCK))
    parser.add_argument("--state-path", default=os.environ.get("DOSPLEX_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument(
        "--quick-access-codes",
        default=os.environ.get("DOSPLEX_DECK_QAM_CODES"),
        help="Comma-separated evdev names or integers accepted as the Quick Access button while BTN_MODE is held",
    )
    parser.add_argument(
        "--manager-trigger-codes",
        default=os.environ.get("DOSPLEX_DECK_MANAGER_TRIGGER_CODES"),
        help="Comma-separated steamos-manager evdev names or integers that toggle DosPlex and are swallowed",
    )
    parser.add_argument(
        "--grab-manager",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("DOSPLEX_DECK_GRAB_MANAGER", "1") != "0",
        help="Grab/re-emit the steamos-manager shortcut device so DosPlex overlay triggers do not reach Steam",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[dosplex-deck-control-shortcut] %(message)s",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
