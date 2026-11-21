#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Native input policy for the Steam Deck's own controller (28de:1205, "Neptune"), preserving its HID identity.

Interface 02 sends 64-byte reports: 01 00 09 40, a little-endian sequence counter in bytes 4..7, then the buttons.
The chord, capture and hand-back logic is native-deck-gate.py's DeckGate; this module adds what the controller relay
needs besides it (the Ibex relay gets the same from native-ibex-gate.py): valid, fixture_report and menu_values.

Bit map (Linux drivers/hid/hid-steam.c steam_do_deck_input_event; RECORDED = seen on a real Deck on 2026-11-02):
  byte 8:  bit 0 R2, 1 L2, 2 R1, 3 L1, 4 Y, 5 B, 6 X, 7 A         (A seen pressed; the rest from hid-steam.c)
  byte 9:  bit 0 D-pad up (RECORDED), 1 right, 2 left, 3 down, 4 View/Select (RECORDED), 5 Steam, 6 Menu/Start
           (right, left, down, Steam and Menu/Start are from hid-steam.c, not recorded)
  byte 10: bit 3 left pad touch, bit 4 right pad touch (bit 4 seen); bytes 48..51 left stick x/y, 52..55 right stick.
"""
import importlib.util
from pathlib import Path
import struct

spec=importlib.util.spec_from_file_location('native_gate',Path(__file__).with_name('native-deck-gate.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

valid=base.valid
released=base.released
TEMPLATE=bytes((1,0,9,64))+bytes(60)  # a neutral report, for the relay's test controls

# The same bounded engineering inputs as the Ibex fixture (face A/B, View, D-pad), as Neptune bits.
FIXTURE_BITS=((0x1,8,0x80),(0x2,8,0x20),(0x400,9,0x08),(0x800,9,0x02),(0x1000,9,0x04),(0x2000,9,0x01),(0x4000,9,0x10))


def fixture_report(data, buttons, x, y):
    if not valid(data) or buttons < 0 or buttons & ~0x7c03:
        raise ValueError('invalid fixture buttons or report')
    if not all(-32768 <= v <= 32767 for v in (x, y)):
        raise ValueError('invalid fixture axes')
    out = bytearray(NeptuneGate().neutral(data))
    for bit,offset,mask in FIXTURE_BITS:
        if buttons & bit:out[offset]|=mask
    struct.pack_into('<hh', out, 48, x, y)  # as the Ibex fixture: menu_values reports -y
    return bytes(out)


class NeptuneGate(base.DeckGate):
    """DeckGate: View and D-pad Up are byte 9 bits 4 and 0, Menu/Start is bit 6 (dpad_up_start 0x41, back_start 0x50)."""
    valid=staticmethod(valid)
    released=staticmethod(released)


def menu_values(data):
    """Evdev-style values for the shared menu, the same keys and axes as the Ibex ones."""
    if not valid(data):raise ValueError('unsupported Neptune input report')
    keys={304:(8,0x80),305:(8,0x20),310:(8,0x08),311:(8,0x04),314:(9,0x10),315:(9,0x40)}
    values={(1,key):int(bool(data[offset]&mask)) for key,(offset,mask) in keys.items()}
    values.update({(3,16):int(bool(data[9]&2))-int(bool(data[9]&4)),
                   (3,17):int(bool(data[9]&8))-int(bool(data[9]&1)),
                   (3,0):struct.unpack_from('<h',data,48)[0],
                   (3,1):-struct.unpack_from('<h',data,50)[0]})
    return values
