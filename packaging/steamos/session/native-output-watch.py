#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Maintain Steam-created input delivery in an explicitly retained dev session."""
import argparse
import importlib.util
import json
import os
import select
import socket
import struct
import subprocess
from pathlib import Path
import time

NETLINK_KOBJECT_UEVENT=15
UDEV_GROUP=2  # events after systemd-udevd applied its rules (group 1 is the raw kernel event, before udev strips ACLs)
UDEV_MAGIC=0xfeedcafe


def open_uevent_socket():
    """A non-blocking netlink socket on the udev multicast group, or None (printed once) when it cannot be opened."""
    try:
        sock=socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,NETLINK_KOBJECT_UEVENT)
        try:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,512*1024)
            sock.bind((0,UDEV_GROUP))
            sock.setblocking(False)
        except OSError:
            sock.close();raise
        return sock
    except OSError as error:
        print(json.dumps({'uevent_listener':'unavailable','error':str(error)}),flush=True)
        return None


def parse_properties(blob):
    properties={}
    for item in blob.split(b'\0'):
        key,separator,value=item.partition(b'=')
        if separator and key:properties[key.decode(errors='replace')]=value.decode(errors='replace')
    return properties


def parse_uevent(data):
    """The KEY=VALUE properties of a udev-group ('libudev\\0' header) or kernel-group ('action@devpath\\0...') message; {} if malformed."""
    try:
        if data.startswith(b'libudev\0'):
            if len(data)<24:return {}
            magic,header_size,offset,length=struct.unpack_from('<IIII',data,8)
            if UDEV_MAGIC not in (magic,struct.unpack('>I',struct.pack('<I',magic))[0]):return {}  # libudev writes the magic big-endian
            if header_size<24 or offset<header_size or offset+length>len(data):return {}
            return parse_properties(data[offset:offset+length])
        head,separator,rest=data.partition(b'\0')
        if b'@' not in head or not separator:return {}
        return parse_properties(rest)
    except (struct.error,ValueError):
        return {}


def input_event(properties):
    """Every input add/remove counts: the kernel announces inputN and then its eventN/jsN children as separate events."""
    return properties.get('SUBSYSTEM')=='input' and properties.get('ACTION') in ('add','remove')


def wait_for_input_event(sock):
    """Wait up to a second; return early (the matching properties) at the first input add/remove, draining what is queued."""
    deadline=time.monotonic()+1.0
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:return None
        if not select.select([sock],[],[],remaining)[0]:return None
        woke=None
        while True:
            try:data=sock.recv(65536)
            except (BlockingIOError,InterruptedError):break
            except OSError:break  # e.g. ENOBUFS: the poll catches up
            properties=parse_uevent(data)
            if woke is None and input_event(properties):woke=properties
        if woke is not None:return woke


def release_all(pending,side):
    """Release the helpers a pass left waiting in the outermost namespace; a failure is printed, never raised."""
    for node,handle in list(pending.items()):
        try:handle.release()
        except (OSError,RuntimeError,subprocess.SubprocessError) as error:
            print(json.dumps({'side':side,'release_error':str(error)[-300:],'node':node}),flush=True)
        finally:pending.pop(node,None)


def main(root):
    """Deliver Steam's outputs until root is gone. A pass runs at once on a udev input add/remove, else every second.

    Tests replace the module attribute open_uevent_socket (None means poll only)."""
    sock=open_uevent_socket()
    spec=importlib.util.spec_from_file_location('delivery',Path(__file__).with_name('native-output-delivery.py'))
    delivery=importlib.util.module_from_spec(spec);spec.loader.exec_module(delivery)
    if os.geteuid()!=0: raise RuntimeError('requires root')
    tx=json.loads((root/'transaction.json').read_text())
    if tx.get('keep_session') is not True: raise RuntimeError('requires retained development session')
    owners=tx.get('steam_input_owners')
    sides=list(owners) if isinstance(owners,dict) else [tx['native_deck_plan']['side']]
    if not sides or any(side not in ('left','right') for side in sides):
        raise RuntimeError('invalid Steam Input sides')
    previous={}; consumer_errors={}; served={}
    while root.exists():
        for side in sides:
            try:
                # A deleted pad replaced in Steam's namespaces leaves a helper waiting in pending; it is released (the
                # IN_ATTRIB on the directory) only after every game's namespace was served, and before the pass ends.
                pending={}
                try:
                    records=delivery.deliver(root,side,carried=pending)
                    reached={(r['mount_namespace'],r['sysname']) for r in records}
                    # Existing game containers keep private mounts across Steam output
                    # recreation. Update only namespaces belonging to this exact side.
                    steam = records[0]
                    # deliver served every mount namespace of Steam (two for its 64-bit client): none of them is a game's.
                    namespaces = {record['mount_namespace'] for record in records}
                    for process in Path('/proc').iterdir():
                        if not process.name.isdecimal(): continue
                        try:
                            identity = delivery.process_identity(int(process.name), steam['unit'], steam['uid'], steam=False)
                            if identity[1] in namespaces: continue
                            if not (process / 'root/dev/input').is_dir(): continue
                            namespaces.add(identity[1])
                            for r in delivery.deliver(root, side, consumer_pid=int(process.name)) or ():
                                reached.add(((r.get('consumer') or r)['mount_namespace'],r['sysname']))
                            consumer_errors.pop(identity[1],None)
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                            if isinstance(error,subprocess.CalledProcessError):
                                detail=(error.stderr or str(error))[-300:]
                                key=identity[1]
                                if consumer_errors.get(key)!=detail:
                                    print(json.dumps({'side':side,'consumer_namespace':key,'delivery_error':detail}),flush=True)
                                    if len(consumer_errors)<256:consumer_errors[key]=detail
                            continue  # Namespace may disappear while a game starts/exits.
                finally:
                    release_all(pending,side)
                # Steam made an output again (a pad woke) or a game's namespace got it: tell the monitors in the side now that
                # the node is there. Only this side's own outputs, and each namespace once.
                fresh=reached-served.get(side,set())
                served[side]=reached
                if fresh:
                    try:delivery.announce(steam['uid'],sorted({sysname for _,sysname in fresh}))
                    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
                        print(json.dumps({'side':side,'announce_error':str(error)[-300:]}),flush=True)
                        served[side]=reached-fresh  # tried again on the next pass
                signature=tuple((r['pid'],r['sysname'],r['node']) for r in records)
                if signature!=previous.get(side):
                    print(json.dumps({'delivered':records}),flush=True);previous[side]=signature
            except (OSError,RuntimeError,ValueError,KeyError,subprocess.SubprocessError) as error:
                signature=(type(error).__name__,str(error))
                if signature!=previous.get(side):
                    print(json.dumps({'waiting':signature}),flush=True);previous[side]=signature
        if sock is None:
            time.sleep(1);continue
        woke=wait_for_input_event(sock)
        if woke is not None:
            print(json.dumps({'uevent':{k:woke[k] for k in ('ACTION','DEVNAME','DEVPATH') if k in woke}}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    main(p.parse_args().root)
