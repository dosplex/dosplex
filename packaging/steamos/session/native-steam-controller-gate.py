#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Native wired/dongle Steam Controller report policy; no Xbox translation.

The report layout follows Linux drivers/hid/hid-steam.c steam_do_input_event.
Bluetooth compact reports require a separate decoder and are not accepted here.
Outside overlay capture, all native feature fields pass through unchanged.
"""
import importlib.util
from pathlib import Path
import struct

_spec = importlib.util.spec_from_file_location('deck_gate', Path(__file__).with_name('native-deck-gate.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def valid(data):
    return len(data) == 64 and data[:4] == bytes((1, 0, 1, 60))


def released(data, menu=False):
    buttons = bytearray(data[8:11])
    # Bit7 also implies left-pad contact in an interleaved stick sample.
    if buttons[2] & 0x80:
        buttons[2] |= 8
    buttons[2] &= ~0x80
    if menu:
        buttons[2] &= ~0x18  # contact is not a click
    # Coordinates16..19 describe the pad when touched, otherwise the stick.
    # Bit3 selects the coordinate source even when bit7 marks interleaving.
    is_stick = not (data[10] & 8)
    stick_idle = not is_stick or max(abs(n) for n in struct.unpack_from('<hh', data, 16)) < 4000
    return not any(buttons) and max(data[11:13]) < 6 and stick_idle


class SteamControllerGate(_base.DeckGate):
    valid = staticmethod(valid)
    released = staticmethod(released)

    def neutral(self, data):
        reference = self.reference or data
        out = bytearray(data)
        out[8:28] = bytes(20)  # buttons, triggers, multiplexed stick and pads
        out[28:34] = reference[28:34]  # preserve gravity
        out[34:40] = bytes(6)  # angular velocity
        out[40:48] = reference[40:48]  # freeze orientation
        out[48:62] = bytes(14)  # raw trigger/stick/pad channels
        return bytes(out)  # sequence and battery telemetry remain intact
