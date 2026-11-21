#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Shared controller menu navigation and unprivileged Steam Input preview adapter."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import time

# Linux input event codes; kept independent of evdev for deterministic tests.
EV_KEY, EV_ABS = 1, 3
SELECT, START = 314, 315
A, B, LB, RB = 304, 305, 310, 311
ABS_X, ABS_Y, HAT_X, HAT_Y = 0, 1, 16, 17


def shortcut_settings():
    """Read the administrator-owned shortcut file once per controller instance."""
    path = Path(os.environ.get('DOSPLEX_CONTROLLER_CONFIG', '/etc/dosplex/controller.json'))
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('controller configuration must be an object')
    chord = data.get('overlay_shortcut', 'dpad_up_back')
    hold = data.get('overlay_hold_seconds', 1.0)
    if chord not in ('dpad_up_back', 'dpad_up_start', 'back_start'):
        raise ValueError('unsupported overlay_shortcut')
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not .5 <= hold <= 3:
        raise ValueError('overlay_hold_seconds must be between 0.5 and 3')
    return chord, float(hold)


def shortcut_event(kind, code, value, chord, dpad_keys=False):
    """Return logical member and held state, preserving original event for replay."""
    button = SELECT if chord == 'dpad_up_back' else START
    if kind == EV_KEY and code == button:
        return START, value != 0
    if chord == 'back_start' and kind == EV_KEY and code == SELECT:
        return SELECT, value != 0
    if chord in ('dpad_up_start', 'dpad_up_back'):
        if not dpad_keys and kind == EV_ABS and code == HAT_Y:
            return SELECT, value < 0
        if kind == EV_KEY and code == 544:  # BTN_DPAD_UP
            return SELECT, value != 0
    return None


class ControllerMenu:
    def __init__(self, send, visible, ranges=None, hold_seconds=None, shortcut=None, dpad_keys=False):
        self.send = send
        self.visible = visible
        self.ranges = ranges or {}
        configured, duration = shortcut_settings()
        self.shortcut = shortcut or configured
        self.hold_seconds = duration if hold_seconds is None else hold_seconds
        self.shortcut_buttons = set()
        self.shortcut_ordered = False
        self.dpad_keys = dpad_keys
        self.buttons = set()
        self.chord_since = None
        self.chord_fired = False
        self.directions = {}
        self.repeat_at = None
        self.last_direction = None
        self.desynced = False

    def direction(self):
        # A single direction at a time avoids double motion from devices that
        # publish both stick and hat state. D-pad takes precedence.
        for code in (HAT_Y, HAT_X, ABS_Y, ABS_X):
            value = self.directions.get(code, 0)
            if value:
                return ('up' if value < 0 else 'down') if code in (HAT_Y, ABS_Y) else ('left' if value < 0 else 'right')
        return None

    def event(self, kind, code, value, now):
        if kind == 0 and code == 3:  # SYN_DROPPED: discard stale held state.
            self.buttons.clear()
            self.shortcut_buttons.clear()
            self.shortcut_ordered = False
            self.directions.clear()
            self.chord_since = None
            self.chord_fired = False
            self.last_direction = None
            self.repeat_at = None
            self.desynced = True
            return
        if self.desynced:
            if kind == 0 and code == 0:
                self.desynced = False
            return
        if kind == EV_KEY:
            if value == 1:
                self.buttons.add(code)
            elif value == 0:
                self.buttons.discard(code)
            if self.dpad_keys and code in (544, 545, 546, 547):
                self.directions[HAT_Y] = int(545 in self.buttons) - int(544 in self.buttons)
                self.directions[HAT_X] = int(547 in self.buttons) - int(546 in self.buttons)
            if value == 1 and self.visible():
                action = {A: 'select', B: 'back', LB: 'previous', RB: 'next'}.get(code)
                if action:
                    self.send(action)
        elif kind == EV_ABS and code in (ABS_X, ABS_Y, HAT_X, HAT_Y) and not (self.dpad_keys and code in (HAT_X, HAT_Y)):
            if code in (HAT_X, HAT_Y):
                direction = -1 if value < 0 else 1 if value > 0 else 0
            elif code in self.ranges:
                low, high = self.ranges[code]
                center = (low + high) / 2
                extent = (high - low) / 2
                normalized = (value - center) / extent if extent > 0 else 0
                # Hysteresis avoids repeating navigation from center noise.
                threshold = .25 if self.directions.get(code) else .55
                direction = -1 if normalized < -threshold else 1 if normalized > threshold else 0
            else:
                direction = 0
            self.directions[code] = direction
        member = shortcut_event(kind, code, value, self.shortcut, self.dpad_keys)
        if member is not None:
            key, held = member
            if held:
                if not self.shortcut_buttons:
                    self.shortcut_ordered = key == START
                self.shortcut_buttons.add(key)
            else:
                self.shortcut_buttons.discard(key)
            if len(self.shortcut_buttons) == 2 and (self.shortcut != 'dpad_up_back' or self.shortcut_ordered):
                if self.chord_since is None:
                    self.chord_since = now
            else:
                self.chord_since = None
                if not self.shortcut_buttons:
                    self.chord_fired = False
        self.tick(now)

    def tick(self, now):
        if self.chord_since is not None and not self.chord_fired and now - self.chord_since >= self.hold_seconds:
            self.send('toggle')
            self.chord_fired = True
        direction = self.direction() if self.visible() and not (self.chord_since is not None or self.chord_fired) else None
        if direction != self.last_direction:
            self.last_direction = direction
            self.repeat_at = now + .4 if direction else None
            if direction:
                self.send(direction)
        elif direction and self.repeat_at is not None and now >= self.repeat_at:
            self.send(direction)
            self.repeat_at = now + .15


def main():
    from evdev import InputDevice
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True)
    parser.add_argument('--side', choices=('left', 'right'), required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('preview adapter must run as the desktop user')
    # Explicit selection avoids assigning another identical device by name.
    with closing(InputDevice(args.device)) as device:
        if (device.info.vendor, device.info.product) != (0x28de, 0x11ff):
            parser.error('preview adapter accepts only a Steam Input virtual gamepad')
        ranges = {}
        for code in (ABS_X, ABS_Y):
            info = device.absinfo(code)
            if info:
                ranges[code] = (info.min, info.max)
        state = {}
        def visible():
            return state.get('overlay_visible') and state.get('overlay_owner') == args.side
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control, selectors.DefaultSelector() as selector:
            def send(action):
                control.sendto(f'MENU {args.side} {action}'.encode(), str(args.runtime / 'hud-ipc.sock'))
            menu = ControllerMenu(send, visible, ranges)
            running = True
            def stop(*_):
                nonlocal running
                running = False
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            selector.register(device.fd, selectors.EVENT_READ)
            print('Controller menu adapter ready for ' + args.side, flush=True)
            while running:
                try:
                    state = json.loads((args.runtime / 'state.json').read_text())
                except (OSError, ValueError):
                    state = {}
                for _, _ in selector.select(timeout=.02):
                    try:
                        events = list(device.read())
                    except BlockingIOError:
                        events = []
                    for event in events:
                        menu.event(event.type, event.code, event.value, time.monotonic())
                menu.tick(time.monotonic())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
