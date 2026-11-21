#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Private native Deck policy process. Requires an armed root-session trial.

Reports stay in memory; stdout logs only lifecycle/errors. The CUSE helper holds
one pinned HID descriptor. Original HID nodes are NOT bound into Steam's view.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import subprocess
import time


# The report stream of a controller is never silent for long while the machine is awake: it fails the relay after
# GAP_SECONDS. A machine that sleeps, or that stalls (the kernel's freezer holds every process for seconds before a
# suspend gives up), makes a gap that is no fault of the controller. The monotonic clock stops in a suspend and
# BOOTTIME does not; a wait that overran by far was held. After either, a silent stream gets RESUME_GRACE seconds.
GAP_SECONDS=.5
RESUME_GRACE=60.0
SUSPEND_SECONDS=.25     # BOOTTIME ahead of MONOTONIC by this much more than at the last look: the machine slept
STALL_SECONDS=1.5       # a wait of GAP_SECONDS that took this long: this process did not run


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class Silence:
    """Judges a gap in the report stream: a failure, or one that a suspend or a stall explains."""
    def __init__(self,clock=time.monotonic,boot=boottime,grace=RESUME_GRACE):
        self.clock,self.boot,self.grace=clock,boot,grace
        self.heard()

    def heard(self):
        """A report came: the next gap is judged strictly again."""
        self.until=0.0
        self.offset=self.boot()-self.clock()

    def excused(self,waited):
        """select() waited `waited` seconds and nothing came. True when that is no failure yet."""
        now=self.clock();offset=self.boot()-now
        if offset-self.offset>=SUSPEND_SECONDS or waited>=STALL_SECONDS:
            self.until=now+self.grace
        self.offset=offset
        return now<self.until


def clear_stale_socket(path):
    """The unit starts this relay again after a failure; the run that failed left its capture socket behind."""
    try:metadata=path.lstat()
    except FileNotFoundError:return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid!=os.geteuid():raise PermissionError('untrusted stale capture endpoint')
    path.unlink()


def capture_path(runtime):
    """Where this relay listens for capture requests. The capture router moves the Deck's socket from
    native-capture.sock to native-deck-capture.sock and listens at the first name itself, so a relay that is started
    again once the router runs has to listen at the second name, where the router looks for it."""
    moved=runtime/'native-deck-capture.sock'
    try:
        if stat.S_ISSOCK(moved.lstat().st_mode):return moved
    except FileNotFoundError:pass
    return runtime/'native-capture.sock'


def follow_moved_slots(root,instance,plan,native):
    """A suspend can enumerate a controller's USB device again: its hidraw node comes back as another node, with
    another inode, and no pin names it. For each record of the plan whose slot has a new node, that node becomes the
    pinned one, durably and before any of it is served (native-hid-delivery.py repin), and is locked down like the
    enrolled one. The records of `plan` are changed in place. Returns [[old path, new path]]."""
    pairs=native.moved(plan)
    if not pairs:return []
    changes=[[old['path'],new['path']] for old,new in pairs]
    print(json.dumps({'native_slot_moved':changes}),flush=True)
    module('hid_delivery','native-hid-delivery.py').repin(root,instance,pairs)
    native.lock_down([new for _,new in pairs])
    for old,new in pairs:
        old.clear();old.update(new)
    return changes


def module(name,file):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def trusted(path):
    for item in (path,*path.parents):
        s=item.lstat()
        if s.st_uid or s.st_mode&0o022 or stat.S_ISLNK(s.st_mode):raise PermissionError('untrusted relay path')


def state(runtime,side):
    path=runtime/'state.json'
    try:
        trusted(path)
        with path.open() as f:s=json.loads(f.read(65537))
        if not isinstance(s,dict) or s.get('primary_side') not in ('left','right'):return False,False
        if s.get('layout')=='single' and side!=s['primary_side']:return False,False
        if not isinstance(s.get('overlay_visible'),bool):return False,False
        overlay=s['overlay_visible'] and s.get('overlay_owner')==side
        # An inactive session must still be able to open its own menu.
        # Suppress application reports separately from shortcut recognition.
        return True,overlay
    except (OSError,ValueError):return False,False


def app_active(runtime, side):
    try:
        path=runtime/'state.json';trusted(path)
        data=json.loads(path.read_text())
        return (data.get(side,{}).get('activity','active')=='active' and
                (data.get('layout')!='single' or data.get('primary_side')==side))
    except (OSError,ValueError):
        return False


def overlay_target(runtime, assigned):
    """The sole visible session owns menus in single-screen mode."""
    try:
        path=runtime/'state.json';trusted(path)
        data=json.loads(path.read_text())
        if data.get('layout')=='single' and data.get('primary_side') in ('left','right'):
            return data['primary_side']
    except (OSError,ValueError):
        pass
    return assigned


def run(root):
    if os.geteuid()!=0 or root.parent!=Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):
        raise RuntimeError('requires exact root trial')
    trusted(root/'transaction.json')
    transaction=json.loads((root/'transaction.json').read_text())
    if transaction.get('keep_session') is not True:
        subprocess.run(['systemctl','is-active','--quiet',transaction['recovery']+'.timer'],check=True)
    planpath=root/'bundle/libexec/native-deck-plan.json';trusted(planpath)
    plan=json.loads(planpath.read_text());native=module('native','native-deck-plan.py')
    follow_moved_slots(root,'deck',plan,native)
    targets=[]
    for record in plan['devices']:
        path=native.checked_path(record)
        if path.name.startswith('hidraw'):
            physical=(Path('/sys/class/hidraw')/path.name/'device').resolve()
            if any(p.name.endswith(':1.2') for p in physical.parents):targets.append(record)
    if len(targets)!=1:raise RuntimeError('ambiguous native Deck interface')
    source=targets[0];fd=os.open(source['path'],os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
    st=os.fstat(fd)
    if [st.st_dev,st.st_ino,st.st_rdev]!=source['identity']:os.close(fd);raise RuntimeError('native HID replaced')
    menu_module=module('menu','dosplex-controller-menu.py')
    shortcut, hold_seconds=menu_module.shortcut_settings()
    gate=module('gate','native-deck-gate.py').DeckGate(shortcut, hold_seconds)
    ControllerMenu=menu_module.ControllerMenu
    name='dosplex-hid-'+transaction['id'];node=Path('/dev')/name
    binary=Path(__file__).with_name('native-hid-relay');trusted(binary)
    # The node asks for the number 120:N of the hidrawN it serves, as the receivers' relays do (native-controller-relay.py launch),
    # so that Steam's copy of an earlier node of this relay opens the relay again after it was started again.
    launcher=module('controller_relay','native-controller-relay.py')
    try:proc,metadata=launcher.launch(binary,node,fd,source['path'],native)
    finally:os.close(fd)
    capture=None
    try:
        capture=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        listening=capture_path(root/'runtime');clear_stale_socket(listening)
        capture.bind(str(listening))
        os.chmod(listening,0o600);capture.listen(4);capture.setblocking(False)
        plan['relay']={'path':str(node),'target':source['path'],'identity':[metadata.st_dev,metadata.st_ino,metadata.st_rdev]}
        temporary=planpath.with_suffix('.new');temporary.write_text(json.dumps(plan));temporary.chmod(0o644);os.replace(temporary,planpath)
        owner=module('ownership','native-ownership.py').Ownership(root,planpath,plan,node,proc,transaction)
        print('native relay ready; input held until compositor state and neutral controls',flush=True)
        pending={};barriers={};sequence=0;buffer=b'';previous={};menu=None;last_overlay=False
        last_report=None;capture_until=0;report_count=0;last_log=0
        test_until=0;test_values=(0,0,0,0,0)
        # Permission to run fixtures must never enable synthetic input on a
        # service restart. Only an explicit root MODE synthetic request may.
        test_mode=False
        last_state=0;available=False;overlay=False;runtime=root/'runtime';side=owner.side;published_at=0;menu_side=overlay_target(runtime,side)
        silence=Silence()
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as ipc:
            ipc.setblocking(False)
            def send(actions):
                for action in actions:ipc.sendto(f'MENU {menu_side} {action}'.encode(),str(runtime/'hud-ipc.sock'))
            while proc.poll() is None:
                now=time.monotonic()
                if now-published_at>=.25:
                    status=owner.status(last_report is not None,gate.phase)
                    temporary=runtime/'native-deck-status.new'
                    temporary.write_text(json.dumps(status));temporary.chmod(0o600)
                    temporary.replace(runtime/'native-deck-status.json');published_at=now
                waited_from=time.monotonic()
                readable=select.select([proc.stdout,capture],[],[],GAP_SECONDS)[0]
                if not readable:
                    # A gap is a failure unless the machine slept or stalled in it (Silence), and not for long then.
                    if silence.excused(time.monotonic()-waited_from):continue
                    raise RuntimeError('native report stream stopped')
                if proc.stdout in readable:silence.heard()
                if capture in readable:
                    connection,_=capture.accept();connection.settimeout(.05)
                    credentials=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    try:
                        request=connection.recv(64)
                        if credentials[1]==0 and transaction.get('native_test_controls') is True and request in (b'MODE physical',b'MODE synthetic'):
                            test_mode=request==b'MODE synthetic'
                            test_until=0;test_values=(0,0,0,0,0)
                            gate.phase='drain';gate.neutral_since=None
                            connection.send(b'READY');connection.close()
                            print('native control mode='+('synthetic' if test_mode else 'physical'),flush=True)
                        elif credentials[1]==0 and test_mode and request.startswith(b'TEST '):
                            try:
                                values=tuple(map(int,request[5:].split()))
                                if len(values)!=5 or not 0<=values[0]<2**64 or any(not -32768<=v<=32767 for v in values[1:]):raise ValueError('invalid synthetic controls')
                                test_values=values;test_until=time.monotonic()+.2
                                connection.send(b'READY')
                            except ValueError:connection.send(b'INVALID')
                            connection.close()
                        elif credentials[1]==0 and request.startswith(b'ROUTE '):
                            sequence+=1
                            if owner.request(request,sequence,connection):
                                side=owner.side;menu_side=overlay_target(runtime,side)
                                gate.phase='drain';gate.neutral_since=None
                                pending.clear();menu=None;previous={};last_overlay=False
                                test_until=0;test_values=(0,0,0,0,0)
                        elif credentials[1]!=0 or request not in (b'CAPTURE left',b'CAPTURE right'):
                            connection.close()
                        elif request!=('CAPTURE '+side).encode():
                            connection.send(b'WAIT');connection.close()
                        elif last_report is None:
                            connection.send(b'WAIT');connection.close()
                        else:
                            capture_until=time.monotonic()+1
                            sequence+=1;barriers[sequence]=connection
                            packet=gate.neutral(last_report)
                            frame=struct.pack('<III',3,sequence,len(packet))+packet
                            if os.write(proc.stdin.fileno(),frame)!=len(frame):raise RuntimeError('short capture barrier write')
                    except (OSError,ValueError):
                        connection.close()
                if proc.stdout not in readable:continue
                chunk=os.read(proc.stdout.fileno(),65536)
                if not chunk:raise RuntimeError('native relay disconnected: its controller went away; the unit starts the relay again, which follows the slot')
                buffer+=chunk
                while len(buffer)>=12:
                    kind,seq,size=struct.unpack_from('<III',buffer)
                    if size>4096:raise RuntimeError('oversized relay frame')
                    if len(buffer)<12+size:break
                    data=buffer[12:12+size];buffer=buffer[12+size:]
                    if kind==2:
                        if owner.acknowledge(seq):
                            published_at=0
                            continue
                        send(pending.pop(seq,[]))
                        connection=barriers.pop(seq,None)
                        if connection is not None:
                            with contextlib.suppress(OSError):connection.send(b'READY')
                            connection.close()
                        continue
                    if kind!=1:raise RuntimeError('unexpected relay frame')
                    now=time.monotonic()
                    report_count+=1
                    if now-last_log>=10:
                        print(f'native relay reports={report_count} phase={gate.phase}',flush=True);last_log=now
                    if now-last_state>=.008:
                        menu_side=overlay_target(runtime,side)
                        available,overlay=state(runtime,menu_side) if side != 'unassigned' else (False,False)
                        last_state=now
                    if test_mode:
                        # Explicit engineering mode only: physical controls stay
                        # neutral when the short synthetic lease expires.
                        values=test_values if now<test_until else (0,0,0,0,0)
                        packet=bytearray(data)
                        if len(packet)!=64:raise ValueError('unexpected synthetic source report length')
                        packet[8:24]=bytes(16);packet[30:36]=bytes(6);packet[44:64]=bytes(20)
                        struct.pack_into('<Q',packet,8,values[0]);struct.pack_into('<hhhh',packet,48,*values[1:])
                        data=bytes(packet)
                    last_report=data
                    if overlay:capture_until=0
                    packets,flush,actions,ready=gate.step(data,now,overlay,available and now>=capture_until)
                    if overlay!=last_overlay:menu=None;previous={};last_overlay=overlay
                    if ready:
                        if menu is None:menu=ControllerMenu(actions.append,lambda:True,{0:(-32768,32767),1:(-32768,32767)})
                        else:menu.send=actions.append
                        values={(1,304):int(bool(data[8]&128)),(1,305):int(bool(data[8]&32)),
                                (1,310):int(bool(data[8]&8)),(1,311):int(bool(data[8]&4)),
                                (1,314):int(bool(data[9]&16)),(1,315):int(bool(data[9]&64)),
                                (3,16):int(bool(data[9]&2))-int(bool(data[9]&4)),
                                (3,17):int(bool(data[9]&8))-int(bool(data[9]&1)),
                                (3,0):struct.unpack_from('<h',data,48)[0],
                                (3,1):-struct.unpack_from('<h',data,50)[0]}
                        for key,value in values.items():
                            if previous.get(key,0)!=value:menu.event(*key,value,now)
                        previous=values;menu.tick(now)
                    active=app_active(runtime,side)
                    for i,packet in enumerate(packets):
                        if not active or menu_side!=side:
                            packet=gate.neutral(packet)
                            flush=True
                        sequence+=1;ack=bool(actions) and i==len(packets)-1
                        if ack:pending[sequence]=actions[:]
                        flags=int(flush and i==0)|2*ack
                        frame=struct.pack('<III',flags,sequence,len(packet))+packet
                        # Each frame is below PIPE_BUF. A short pipe write is an
                        # error; never continue with a desynchronized stream.
                        if os.write(proc.stdin.fileno(),frame)!=len(frame):raise RuntimeError('short relay write')
    finally:
        if 'owner' in locals():owner.close()
        if capture is not None:capture.close()
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        if node.exists():raise RuntimeError('relay node was not removed')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True,type=Path)
    run(p.parse_args().root)
