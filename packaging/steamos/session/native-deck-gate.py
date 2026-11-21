#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Pure native Deck report policy for the private HID relay.

No device access. Layout is documented in Linux drivers/hid/hid-steam.c.
Full reports pass unchanged outside capture. Capture removes buttons, pads,
triggers and sticks, freezes accelerometer/orientation, and zeros angular rate.
Menu chord: the first button of the pair reaches the game immediately and exactly
as pressed; only the second is withheld while the first is held. If the chord is
abandoned the withheld press is delivered at once. When the menu opens, capture
neutralises the report, which releases the first button to the game.
This needs real Steam Input validation; synthetic success is not game acceptance.
"""
import struct

CHORD=0x11  # D-pad Up + View/Select, byte 9


def valid(data):
    return len(data)==64 and data[:3]==bytes((1,0,9)) and data[3] in (56,64)


def released(data, menu=False):
    # Touching a stick is not movement, but touch buttons may map to native
    # actions. Require all button/touch bits released before handback.
    buttons=bytearray(data[8:16])
    if menu:
        buttons[2]&=~0x18  # pad contact is not a menu click
        buttons[5]&=~0xc0  # fingers resting on sticks are not navigation
    return (not any(buttons) and max(struct.unpack_from('<HH',data,44))<1500
            and max(abs(n) for n in struct.unpack_from('<hhhh',data,48))<4000
            and (menu or max(struct.unpack_from('<HH',data,56))<1500))


def control_fields(data, button_start, button_size, axes, pads):
    """Independent held controls; masks never include report clocks or sensors."""
    fields = {}
    for offset in range(button_start, button_start + button_size):
        for bit in range(8):
            mask = 1 << bit
            fields[f'button-{offset-button_start}-{bit}'] = (bool(data[offset] & mask), ((offset, mask),))
    for name, offset, count, threshold, signed in axes:
        values = struct.unpack_from('<' + ('h' if signed else 'H') * count, data, offset)
        fields[name] = (max(abs(v) for v in values) >= threshold,
                        tuple((i, 255) for i in range(offset, offset + count * 2)))
    for name, offset, pressure, contact_offset, contact_mask in pads:
        active = bool(data[contact_offset] & contact_mask) or struct.unpack_from('<H', data, pressure)[0] >= 1500
        fields[name] = (active, tuple((i, 255) for i in range(offset, offset + 4)) +
                        ((pressure, 255), (pressure + 1, 255), (contact_offset, contact_mask)))
    return fields


class DeckGate:
    valid = staticmethod(valid)
    released = staticmethod(released)

    def controls(self, data):
        return control_fields(data, 8, 8,
            [('left-trigger',44,1,1500,False), ('right-trigger',46,1,1500,False),
             ('left-stick',48,2,4000,True), ('right-stick',52,2,4000,True)],
            [('left-pad',16,56,10,8), ('right-pad',20,58,10,16)])

    def chord_bits(self, data):
        return data[9] & self.chord

    def with_chord_bits(self, data, bits):
        out = bytearray(data)
        out[9] = (out[9] & ~self.chord) | bits
        return bytes(out)

    def __init__(self, shortcut="dpad_up_back", hold_seconds=1.0):
        self.chord = {"dpad_up_back": 0x11, "dpad_up_start": 0x41, "back_start": 0x50}[shortcut]
        # dpad_up_back needs Select down first. For the others either button may be first.
        self.ordered = shortcut == 'dpad_up_back'
        self.firsts = (0x10,) if self.ordered else tuple(b for b in (0x01,0x10,0x40) if b & self.chord)
        self.hold_seconds = hold_seconds
        self.phase='drain'
        self.until=None
        self.neutral_since=None
        self.reference=None
        self.first=0
        self.passed=0
        self.overlay_before=False
        self.last_time=None
        self.menu_ready=False
        self.overlay_held=set()
        self.handback=set()

    def neutral(self,data):
        reference=self.reference or data
        out=bytearray(data)
        out[8:24]=bytes(16)
        out[24:30]=reference[24:30]  # fixed gravity, not artificial freefall
        out[30:36]=bytes(6)
        out[36:44]=reference[36:44]
        out[44:64]=bytes(20)
        return bytes(out)

    def step(self,data,now,overlay=False,trusted=True):
        """Return (reports, flush queues, menu actions, menu may consume raw).

        Actions must be sent only AFTER transport acknowledges the flush.
        Unknown report types fail closed. No raw data should be logged.
        """
        if not self.valid(data):raise ValueError('unsupported native controller input report')
        if self.last_time is not None and now<self.last_time:raise ValueError('non-monotonic input time')
        self.last_time=now
        if self.reference is None:self.reference=data
        old=self.phase
        actions=[]
        if not trusted:
            self.phase='drain';self.neutral_since=None;self.first=0;self.handback.clear()
        elif overlay:
            if not self.overlay_before:
                self.phase='overlay';self.neutral_since=None;self.first=0;self.menu_ready=False
            elif self.phase!='overlay':self.phase='overlay'
        elif self.overlay_before:
            # Re-arm each control independently. A resting finger or a held
            # close button must not stall new stick/D-pad input for the account.
            self.handback=self.overlay_held.copy()
            self.phase='run';self.neutral_since=None
        if overlay:
            self.overlay_held={key for key,(active,_) in self.controls(data).items() if active}
        self.overlay_before=overlay
        if self.handback and trusted and not overlay:
            fields=self.controls(data)
            self.handback.intersection_update(key for key,(active,_) in fields.items() if active)
            if self.handback:
                out=bytearray(data)
                for key in self.handback:
                    for offset,mask in fields[key][1]:out[offset] &= ~mask
                data=bytes(out)
        if self.phase in ('drain','overlay'):
            if trusted and self.released(data,menu=self.phase=='overlay'):
                if self.neutral_since is None:self.neutral_since=now
                if self.phase=='drain' and now-self.neutral_since>=.12:self.phase='run'
            elif not overlay or not self.menu_ready:
                self.neutral_since=None
        if overlay and self.neutral_since is not None and now-self.neutral_since>=.12:self.menu_ready=True
        menu_ready=overlay and self.menu_ready
        bits=self.chord_bits(data)
        if trusted and not overlay:
            if self.phase=='run':
                if not bits:self.first=0
                elif bits in self.firsts and self.first in (0,bits):
                    self.first=bits  # first button of the pair: reaches the game as pressed
                elif bits==self.chord and (self.first in self.firsts or (not self.ordered and not self.first)):
                    self.phase='chord';self.until=now+self.hold_seconds;self.passed=self.first
                else:self.phase='ordinary';self.first=0
            elif self.phase=='ordinary' and not bits:
                self.phase='run';self.first=0
            if self.phase=='chord':
                if bits!=self.chord:
                    # Chord abandoned before it fired: the withheld second button
                    # arrives now (press, then the current state) so a quick
                    # combo still reaches the game.
                    press=self.with_chord_bits(data,self.chord)
                    self.reference=data
                    if bits and bits==self.passed:self.phase='run';self.first=bits
                    elif bits:self.phase='ordinary';self.first=0
                    else:self.phase='run';self.first=0
                    return [press,data],True,[],False
                elif now>=self.until:
                    self.phase='opening';self.until=now+1;actions=['toggle']
            elif self.phase=='opening' and now>=self.until:
                # A rejected or delayed UI request must not destroy the HID
                # endpoint. Keep controls neutral until release, then resume.
                self.phase='drain';self.neutral_since=None;self.first=0
        captured=self.phase in ('drain','opening','overlay')
        if captured:
            output=self.neutral(data)
        elif self.phase=='chord':
            # The first button passes through; only the second is withheld.
            output=self.with_chord_bits(data,self.passed)
            self.reference=output
        else:
            output=data;self.reference=data
        return [output],captured or old!=self.phase,actions,menu_ready
