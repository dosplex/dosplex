#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Native HID ownership protocol shared by enrolled retained-trial relays.

Only root policy peers can request a route. The CUSE acknowledgement means all
old readers have been revoked, including pending reads and feature writes.
Account IDs come from the enrolled trial, never from an IPC caller.
"""
import contextlib
import json
import os
import re
import stat
import struct
import time


def device_specs(transaction):
    """Only explicitly journaled controller slots join capture and routing."""
    external_only=transaction.get('native_external_only',False)
    if type(external_only) is not bool:raise ValueError('invalid native platform enrollment')
    builtin=transaction.get('native_builtin_deck',False)
    if type(builtin) is not bool:raise ValueError('invalid native platform enrollment')
    # A Steam Deck has its own controls (kind 'deck', always Main's at first, ADR-014). Without a receiver pair the
    # second built-in kind ('controller') does not exist: Guest's pads are enrolled extras. A start with no controller at all
    # is legal (Main alone needs none): the result is then empty.
    if external_only:result=[('deck','Steam Deck controls','main')] if builtin else []
    else:result=[('deck','Steam Deck controls','main'),('controller','Steam Controller','guest')]
    extras=transaction.get('native_extra_controllers',{})
    if not isinstance(extras,dict) or len(extras)>8:raise ValueError('invalid native slot enrollment')
    for token,entry in sorted(extras.items()):
        if not re.fullmatch('[a-f0-9]{8}',token) or not isinstance(entry,dict):
            raise ValueError('invalid native slot identity')
        default=entry.get('default_target','main')  # (ADR-014: a controller nobody named belongs to Main)
        # 'auto' (ADR-014 as amended 2026-11-23): the owner is decided when the controller is first awake.
        if default not in ('main','guest','unassigned','auto'):raise ValueError('invalid native default owner')
        result.append(('controller-'+token,entry.get('name','Steam Controller'), default))
    return result


class Ownership:
    def __init__(self, root, planpath, plan, node, proc, transaction):
        self.root, self.planpath, self.plan = root, planpath, plan
        self.node, self.proc = node, proc
        self.owners = transaction['steam_input_owners']
        if set(self.owners) != {'left', 'right'} or any(
                type(uid) is not int or uid < 1000 for uid in self.owners.values()):
            raise ValueError('invalid enrolled native owners')
        if len(set(self.owners.values())) != 2:
            raise ValueError('native owners must be separate accounts')
        self.side = plan.get('routing_side', plan['side'])
        self.uid_for(self.side)
        self.pending = None
        self.generation = 0
        self.transfer(self.side, 0, acknowledge=False)

    def uid_for(self, side):
        if side == 'unassigned': return 0
        if side not in self.owners: raise ValueError('invalid native route')
        return self.owners[side]

    def transfer(self, side, sequence, acknowledge=True):
        uid = self.uid_for(side)
        meta = self.node.lstat()
        if (not stat.S_ISCHR(meta.st_mode) or
                [meta.st_dev, meta.st_ino, meta.st_rdev] != self.plan['relay']['identity']):
            raise RuntimeError('native relay identity changed')
        frame = struct.pack('<IIII', 6 if acknowledge else 4, sequence, 4, uid)
        if os.write(self.proc.stdin.fileno(), frame) != len(frame):
            raise RuntimeError('short native ownership command')
        os.chown(self.node, uid, uid)
        os.chmod(self.node, 0o600)
        self.side = side

    def request(self, request, sequence, connection):
        side = request.decode('ascii').removeprefix('ROUTE ')
        self.uid_for(side)
        if self.pending is not None:
            connection.send(b'WAIT'); connection.close(); return False
        self.transfer(side, sequence)
        self.pending = sequence, connection
        return True

    def acknowledge(self, sequence):
        if self.pending is None or self.pending[0] != sequence: return False
        _, connection = self.pending
        self.pending = None
        self.generation += 1
        self.plan.update(routing_side=self.side, routing_uid=self.uid_for(self.side))
        temporary = self.planpath.with_suffix('.new')
        temporary.write_text(json.dumps(self.plan)); temporary.chmod(0o644)
        temporary.replace(self.planpath)
        with contextlib.suppress(OSError): connection.send(b'READY')
        connection.close()
        return True

    def status(self, connected, phase):
        return dict(updated=time.time(), side=self.side,
                    routing_version=1, routing_generation=self.generation,
                    connected=connected, phase=phase,
                    captured=phase in ('drain','chord','opening','overlay'))

    def close(self):
        if self.pending is not None: self.pending[1].close()
