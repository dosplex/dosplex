#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Native Ibex Steam Controller input policy, preserving its HID identity.

Layout: Linux drivers/hid/hid-steam.c steam_do_ibex_input_event.
Full input0x42, compact input0x45 and timestamp input0x47 are decoded. Receiver status
and feature replies must be classified by the transport separately.
"""
import importlib.util
from pathlib import Path
import struct

spec=importlib.util.spec_from_file_location('native_gate',Path(__file__).with_name('native-deck-gate.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)


def valid(data):
    return bool(data) and (data[0],len(data)) in ((0x42,54),(0x45,46),(0x47,46))


def fixture_report(data, buttons, x, y):
    """Bounded engineering inputs: face A/B, Back, D-pad and left stick only."""
    if not valid(data) or buttons < 0 or buttons & ~0x7c03:
        raise ValueError('invalid fixture buttons or report')
    if not all(-32768 <= v <= 32767 for v in (x, y)):
        raise ValueError('invalid fixture axes')
    out = bytearray(IbexGate().neutral(data))
    struct.pack_into('<I', out, 2, buttons)
    struct.pack_into('<hh', out, 10, x, y)
    return bytes(out)


def released(data, menu=False):
    pressure_offsets=(24,30) if data[0]==0x47 else (22,28)
    buttons=bytearray(data[2:6])
    if menu:
        buttons[2]&=~0x30  # right stick/pad contact
        buttons[3]&=~0x33  # left stick/pad and grip contact
    return (not any(buttons)
            and max(abs(x) for x in struct.unpack_from('<hh',data,6))<1500
            and max(abs(x) for x in struct.unpack_from('<hhhh',data,10))<4000
            and (menu or max(struct.unpack_from('<H',data,offset)[0] for offset in pressure_offsets)<1500))


class IbexGate(base.DeckGate):
    valid=staticmethod(valid)
    released=staticmethod(released)
    # Canonical chord bits match the common policy, physical offsets do not.
    MEMBERS=((1,3,0x20),(0x10,3,0x40),(0x40,2,0x40))

    def controls(self,data):
        shift=2 if data[0]==0x47 else 0
        return base.control_fields(data,2,4,
            [('left-trigger',6,1,1500,True), ('right-trigger',8,1,1500,True),
             ('left-stick',10,2,4000,True), ('right-stick',14,2,4000,True)],
            [('left-pad',18+shift,22+shift,5,2), ('right-pad',24+shift,28+shift,4,32)])

    def chord_bits(self,data):
        return sum(bit for bit,offset,mask in self.MEMBERS if bit & self.chord and data[offset] & mask)

    def with_chord_bits(self,data,bits):
        out=bytearray(data)
        for bit,offset,mask in self.MEMBERS:
            if bit & self.chord:
                out[offset]=(out[offset]&~mask)|(mask if bits & bit else 0)
        return bytes(out)

    def neutral(self,data):
        reference=self.reference or data
        out=bytearray(data)
        if data[0]==0x47:
            out[2:18]=bytes(16)
            out[20:32]=bytes(12)  # preserve trackpad and IMU timestamps
        else:
            out[2:30]=bytes(28)
        out[34:40]=reference[34:40]
        out[40:46]=bytes(6)
        if len(data)==54:
            out[46:54]=reference[46:54] if len(reference)==54 else bytes(8)
        return bytes(out)


def menu_values(data):
    """Evdev-style values for the shared menu, independent of Steam mappings."""
    if not valid(data):raise ValueError('unsupported Ibex input report')
    keys={304:(2,1),305:(2,2),310:(4,8),311:(3,2),314:(3,0x40),315:(2,0x40)}
    values={(1,key):int(bool(data[offset]&mask)) for key,(offset,mask) in keys.items()}
    values.update({(3,16):int(bool(data[3]&8))-int(bool(data[3]&16)),
                   (3,17):int(bool(data[3]&4))-int(bool(data[3]&32)),
                   (3,0):struct.unpack_from('<h',data,10)[0],
                   (3,1):-struct.unpack_from('<h',data,12)[0]})
    return values
