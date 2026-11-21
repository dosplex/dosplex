#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Root-only capture routing for the retained Deck and external native relays.

Installer moves the original Deck socket to native-deck-capture.sock before
starting this router. A missing selected relay fails closed; no fallback READY.
A relay that is down or restarting holds back only the side it may belong to:
the other side's menu, and an Unassigned slot's restart, never wait for it.
If its device node is gone as well, nothing can read through it: it holds back no side.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time
import threading
import importlib.util
from concurrent.futures import ThreadPoolExecutor


def forward(endpoint,request):
    metadata=endpoint.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode&0o077:
        raise PermissionError('untrusted capture endpoint')
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as client:
        client.settimeout(.15);client.connect(str(endpoint))
        if struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]!=0:
            raise PermissionError('untrusted capture peer')
        client.sendall(request)
        reply=client.recv(16)
        return reply if reply in (b'READY',b'WAIT',b'INVALID') else b'WAIT'


def select_endpoint(runtime,request,deck_side,external_side):
    if request==('CAPTURE '+deck_side).encode():return runtime/'native-deck-capture.sock'
    if request==('CAPTURE '+external_side).encode():return runtime/'native-controller-capture.sock'
    if request in (b'MODE physical',b'MODE synthetic') or request.startswith(b'TEST '):
        return runtime/'native-deck-capture.sock'
    raise ValueError('invalid capture request')


def read_status(path,now):
    """A slot's status and whether it is fresh. A relay that restarts leaves a stale, missing or unreadable one."""
    try:value=json.loads(path.read_text())
    except (OSError,ValueError):return None,False
    if not isinstance(value,dict):return None,False
    updated=value.get('updated')
    fresh=(value.get('routing_version')==1 and value.get('side') in ('left','right','unassigned')
           and isinstance(updated,(int,float)) and 0<=now-updated<2)
    return value,fresh


def possible_sides(runtime,kind,value):
    """Where a slot may belong: its last status and the applied owner that its relay persisted in the plan."""
    sides={value.get('side')} if value else set()
    try:
        plan=json.loads((runtime.parent/'bundle/libexec'/('native-'+kind+'-plan.json')).read_text())
        sides.add(plan.get('routing_side',plan.get('side')))
    except (OSError,ValueError,AttributeError):pass
    return {side for side in sides if side in ('left','right','unassigned')}


RELAY_NODE=re.compile('/dev/dosplex-hid-[a-f0-9]{32}')


def relay_present(runtime,kind,dev=Path('/dev')):
    """Whether the slot's relay still has its device node. A relay without one has no readers left.

    The relay's plan names the node. A plan that is missing, unreadable or without a valid relay entry counts as
    present, so that the caller fails closed: the relay may not have published its entry yet."""
    try:path=json.loads((runtime.parent/'bundle/libexec'/('native-'+kind+'-plan.json')).read_text())['relay']['path']
    except (OSError,ValueError,KeyError,TypeError):return True
    if not isinstance(path,str) or not RELAY_NODE.fullmatch(path):return True
    try:(dev/Path(path).name).lstat()
    except FileNotFoundError:return False
    except OSError:pass
    return True


def capture_requests(runtime,request,deck_side,external_side,kinds=('deck','controller'),dev=Path('/dev')):
    # New relays publish their applied owner. Multiple controllers may belong
    # to one side; capture every one before revealing its overlay.
    statuses=[runtime/('native-'+kind+'-status.json') for kind in kinds]
    explicit=len(kinds)>2 or tuple(kinds)!=('deck','controller')
    # Main alone needs no controller: with none enrolled there is nothing to capture, and the request is answered at once.
    if not kinds and request.startswith(b'CAPTURE '):return []
    if not kinds:raise ValueError('no enrolled native controls')
    if request.startswith(b'CAPTURE ') and (explicit or all(path.exists() for path in statuses)):
        reads=[read_status(path,time.time()) for path in statuses]
        if explicit or all(value is not None and value.get('routing_version')==1 for value,_ in reads):
            side=request.decode().removeprefix('CAPTURE ')
            if side not in ('left','right'):raise ValueError('invalid capture side')
            state=json.loads((runtime/'state.json').read_text())
            single=state.get('layout')=='single' and side==state.get('primary_side')
            result=[]
            for kind,(value,fresh) in zip(kinds,reads):
                if fresh:
                    if value['side']==side or (single and value['side'] in ('left','right')):
                        result.append((runtime/('native-'+kind+'-capture.sock'),('CAPTURE '+value['side']).encode()))
                    continue
                # No fresh status: that relay is down or restarting. If its node is gone too, nobody can read through
                # it: there is nothing to capture, and it holds back no side. Otherwise it holds the request back
                # (fail closed) only if it may belong to this side, or if its side is unknown.
                if not relay_present(runtime,kind,dev):continue
                sides=possible_sides(runtime,kind,value)
                if not sides or side in sides or (single and sides&{'left','right'}):
                    raise ValueError('native routing status is stale')
            return result
    if explicit:raise ValueError('enrolled native slots need valid capture routing status')
    endpoint=select_endpoint(runtime,request,deck_side,external_side)
    if request.startswith(b'CAPTURE '):
        data=json.loads((runtime/'state.json').read_text())
        if data.get('layout')=='single' and request==('CAPTURE '+data.get('primary_side','')).encode():
            return [(runtime/'native-deck-capture.sock',('CAPTURE '+deck_side).encode()),
                    (runtime/'native-controller-capture.sock',('CAPTURE '+external_side).encode())]
    return [(endpoint,request)]


def clear_stale_socket(path):
    """Remove the socket a router that is gone left behind; one that still answers, and anything that is no socket, stays.

    A router that was stopped leaves its socket file, and the next one could not bind: "Address already in use", and
    with its unit gone no controller was routed until the session was restarted (seen on the Steam Machine)."""
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):return
    except FileNotFoundError:return
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as probe:
        probe.settimeout(.5)
        try:probe.connect(str(path))
        except (ConnectionRefusedError,FileNotFoundError):pass
        except OSError:return
        else:return
    with contextlib.suppress(FileNotFoundError):os.unlink(path)


def run(root):
    if os.geteuid()!=0 or root.parent!=Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):raise RuntimeError('invalid trial')
    metadata=(root/'transaction.json').lstat()
    if metadata.st_uid!=0 or metadata.st_mode&0o022 or not stat.S_ISREG(metadata.st_mode):raise PermissionError('untrusted transaction')
    tx=json.loads((root/'transaction.json').read_text())
    if tx.get('keep_session') is not True or tx.get('phase')!='started':raise RuntimeError('inactive trial')
    if tx.get('native_external_only'):
        deck=external=None  # Explicit status routing never uses the legacy pair fallback.
    else:
        deck=tx['native_deck_plan']['side'];external=tx['native_controller_plan']['side']
        if {deck,external}!={'left','right'}:raise ValueError('invalid capture owners')
    runtime=root/'runtime'
    spec=importlib.util.spec_from_file_location('native_ownership',Path(__file__).with_name('native-ownership.py'))
    ownership=importlib.util.module_from_spec(spec);spec.loader.exec_module(ownership)
    kinds=tuple(kind for kind,_,_ in ownership.device_specs(tx))
    routing=Path(__file__).with_name('native-routing.py')
    if routing.exists():
        spec=importlib.util.spec_from_file_location('native_routing',routing)
        adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
        threading.Thread(target=adapter.run,args=(root,),daemon=True).start()
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as server:
        clear_stale_socket(runtime/'native-capture.sock')
        server.bind(str(runtime/'native-capture.sock'));os.chmod(runtime/'native-capture.sock',0o600);server.listen(8)
        while True:
            connection,_=server.accept()
            with connection:
                connection.settimeout(.02)
                try:
                    if struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]!=0:continue
                    request=connection.recv(64)
                    requests=capture_requests(runtime,request,deck,external,kinds)
                    with ThreadPoolExecutor(max_workers=max(1,len(kinds))) as pool:
                        replies=list(pool.map(lambda item:forward(*item),requests))
                    connection.sendall(b'READY' if all(r==b'READY' for r in replies) else b'WAIT')
                except (OSError,ValueError):
                    try:connection.sendall(b'WAIT')
                    except OSError:pass


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    run(parser.parse_args().root)
