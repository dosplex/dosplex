#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Retained-trial Ibex relay. Preserves HID metadata, feature and output reports.

Root-owned native-controller-plan.json pins one receiver slot and account.
Never exposes the original HID to Steam. Unknown input types are withheld;
counts, not input payloads, are logged. This candidate still needs live acceptance.

The relay's CUSE node asks for the device number 120:N of the /dev/hidrawN it serves (launch), so that a copy of an
earlier node of this relay in a Steam mount namespace opens the relay again after it was started again
(native-hid-delivery.py numbered). Without that number, which a helper from before the numbers cannot take, it runs on one
of the kernel's choice and says so in the journal (native_relay_number_fallback).
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


def module(name, file):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file))
    out=importlib.util.module_from_spec(spec);spec.loader.exec_module(out);return out


def clear_stale_socket(path):
    """The unit starts this relay again after a failure; the run that failed left its capture socket behind."""
    try:metadata=path.lstat()
    except FileNotFoundError:return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid!=os.geteuid():raise PermissionError('untrusted stale capture endpoint')
    path.unlink()


RELAY_START_SECONDS=5

# The input report layout depends on the controller. Its USB product decides which policy parses it.
POLICIES={'1304':'native-ibex-gate.py','1305':'native-ibex-gate.py','1205':'native-neptune-gate.py'}


def policy_file(product):
    """The policy module for a source device's USB product id (28de), or None when no parser is known for it."""
    return POLICIES.get(product)


def source_product(source,native,path):
    """The USB product id of the pinned source: the plan's slot record, else the node's own place in sysfs, else None."""
    product=(source.get('slot') or {}).get('product')
    if product is None:
        try:product=native.slot_of(path)['product']
        except (OSError,ValueError,KeyError):pass
    return product.lower() if isinstance(product,str) else None


class PassGate:
    """No chord gate: the controller's reports pass unchanged because no parser is known for it."""
    phase='passthrough'
    def neutral(self,data):return data


def start_helper(binary,name,fd,number=None):
    """Start the CUSE helper on the inherited source descriptor. number is (major, minor), the device number its node is
    to have; None leaves the choice to the kernel, as it was before the numbers. A helper that takes no number ends at once."""
    argv=[str(binary),name,str(fd)]
    if number is not None:argv.append('%d:%d'%number)
    return subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,pass_fds=(fd,),bufsize=0)


def stop_helper(proc):
    if proc.poll() is None:
        proc.terminate()
        try:proc.wait(timeout=3)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
    for pipe in (proc.stdin,proc.stdout):
        if pipe is not None:pipe.close()


def number_holder(number,claimed=Path('/sys/dev/char')):
    """The name of the device that holds this device number, or None when it is free. The kernel lists every device
    that has a number in /sys/dev/char (CUSE devices too, under the name the relay gave them)."""
    link=claimed/('%d:%d'%number)
    try:return Path(os.readlink(link)).name
    except FileNotFoundError:return None
    except OSError:return str(link)


def fell_back(number,reason):
    print(json.dumps({'native_relay_number_fallback':{'number':None if number is None else '%d:%d'%number,'reason':reason}}),flush=True)


def choose_number(native,path,devices=Path('/proc/devices'),claimed=Path('/sys/dev/char')):
    """The device number to ask for the node of the relay that serves this hidraw node, or None to let the kernel pick.

    It is 120:N for /dev/hidrawN (native-deck-plan.py relay_number), so that a copy of an earlier node of this relay,
    in any mount namespace, opens the live relay. None, with a line in the journal, when that is not possible: N is 64
    or more, or another driver holds major 120. The pad works then as it did before, and a relay that is started again
    needs 'Reset my side'. A number that another relay holds raises, naming that relay: the unit starts this one again.
    """
    if getattr(native,'RELAY_MAJOR',None) is None:return None
    try:number=native.relay_number(path)
    except ValueError as error:fell_back(None,str(error));return None
    owners=native.foreign_major_owners(devices)
    if owners:fell_back(number,'major %d is held by %s'%(number[0],', '.join(owners)));return None
    holder=number_holder(number,claimed)
    if holder is not None:raise RuntimeError('relay number %d:%d is held by %s; the unit starts again'%(*number,holder))
    return number


def launch(binary,node,fd,path,native,wait=RELAY_START_SECONDS,devices=Path('/proc/devices'),claimed=Path('/sys/dev/char')):
    """Start the helper on the source descriptor and wait for its node; returns (process, the node's metadata).

    The helper is asked for the number 120:N of the hidrawN it serves. If it ends before its node is there (an older
    helper takes no number; the kernel may refuse one), it is started once more, on the source that the caller still
    holds open, with a number of the kernel's choice, and the journal says so. A relay that holds the number by then
    raises instead, naming it. With a number asked for, the node has to carry it.
    """
    number=choose_number(native,path,devices,claimed)
    proc=start_helper(binary,node.name,fd,number)
    try:
        deadline=time.monotonic()+wait
        while not node.exists():
            if proc.poll() is not None:
                if number is None:raise RuntimeError('relay startup failed')
                holder=number_holder(number,claimed)
                if holder is not None and holder.startswith('dosplex-hid-'):
                    raise RuntimeError('relay number %d:%d is held by %s; the unit starts again'%(*number,holder))
                fell_back(number,'the helper ended with status %s before its node was there'%proc.returncode)
                stop_helper(proc);number=None;proc=start_helper(binary,node.name,fd);deadline=time.monotonic()+wait
            elif time.monotonic()>deadline:raise RuntimeError('relay startup failed')
            time.sleep(.01)
        metadata=node.lstat()
        if not stat.S_ISCHR(metadata.st_mode):raise RuntimeError('invalid relay endpoint')
        if number is not None and metadata.st_rdev!=os.makedev(*number):
            raise RuntimeError('relay endpoint has the number %d:%d, not the asked %d:%d'%(
                os.major(metadata.st_rdev),os.minor(metadata.st_rdev),*number))
        return proc,metadata
    except BaseException:
        stop_helper(proc);raise


def run(root, instance='controller'):
    if os.geteuid()!=0 or root.parent!=Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):
        raise RuntimeError('requires exact root trial')
    shared=module('deck_relay','native-deck-relay.py')
    if not re.fullmatch('controller(?:-[a-f0-9]{8})?',instance):raise ValueError('invalid controller instance')
    planpath=root/'bundle/libexec'/('native-'+instance+'-plan.json')
    shared.trusted(planpath);shared.trusted(root/'transaction.json')
    tx=json.loads((root/'transaction.json').read_text());plan=json.loads(planpath.read_text())
    if instance not in {kind for kind,_,_ in module('ownership','native-ownership.py').device_specs(tx)}:
        raise ValueError('controller instance is not enrolled')
    if tx.get('keep_session') is not True or tx.get('phase')!='started':raise RuntimeError('requires active retained trial')
    if plan.get('protocol')!='ibex' or plan.get('side') not in ('left','right'):raise ValueError('invalid native controller plan')
    delivery=module('delivery','native-output-delivery.py')
    sessionpath=root/'sides'/plan['side']/'session.json';shared.trusted(sessionpath)
    session=json.loads(sessionpath.read_text())
    if delivery.output_owner(tx,plan['side'],session)!=plan['uid']:
        raise RuntimeError('native controller owner mismatch')
    native=module('native_plan','native-deck-plan.py')
    sources=[record for record in plan['devices'] if record['path'].startswith('/dev/hidraw')]
    if len(sources)!=1:raise ValueError('expected one pinned receiver slot')
    # A suspend can enumerate the receiver again; its node is then another one. The slot's new node becomes the pinned
    # one, durably, before anything is served from it.
    shared.follow_moved_slots(root,instance,plan,native)
    source=sources[0];path=native.checked_path(source)
    if not re.fullmatch('[a-f0-9]{32}',plan['relay_id']):raise ValueError('invalid relay identity')
    fd=os.open(path,os.O_RDWR|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC)
    st=os.fstat(fd)
    if [st.st_dev,st.st_ino,st.st_rdev]!=source['identity']:
        os.close(fd);raise RuntimeError('physical node changed')
    binary=Path(__file__).with_name('native-hid-relay');shared.trusted(binary)
    node=Path('/dev/dosplex-hid-'+plan['relay_id'])
    # The source stays open until the node is there: a helper that is refused its number is started again on it.
    try:proc,metadata=launch(binary,node,fd,path,native)
    finally:os.close(fd)
    capture=None
    barriers={}
    try:
        product=source_product(source,native,path);name=policy_file(product);menu_module=module('menu','dosplex-controller-menu.py')
        shortcut,hold=menu_module.shortcut_settings()
        if name is None:
            print(json.dumps({'native_controller_without_chord_gate':{'product':product,'path':str(path),
                'reason':'no input report parser for this USB product; the menu chord is off for it, reports pass unchanged'}}),flush=True)
            policy=None;make_gate=PassGate;gate=PassGate();protocol='unknown'
        else:
            policy=module('policy',name)
            make_gate=lambda:getattr(policy,'NeptuneGate' if name=='native-neptune-gate.py' else 'IbexGate')(shortcut,hold)
            gate=make_gate();protocol='neptune' if name=='native-neptune-gate.py' else 'ibex'
        health=module('health','native-controller-health.py').ControllerHealth()
        plan['relay']={'path':str(node),'target':str(path),'identity':[metadata.st_dev,metadata.st_ino,metadata.st_rdev]}
        temporary=planpath.with_suffix('.new');temporary.write_text(json.dumps(plan));temporary.chmod(0o644);temporary.replace(planpath)
        owner=module('ownership','native-ownership.py').Ownership(root,planpath,plan,node,proc,tx)
        clear_stale_socket(root/'runtime'/('native-'+instance+'-capture.sock'))
        capture=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        capture.bind(str(root/'runtime'/('native-'+instance+'-capture.sock')))
        os.chmod(root/'runtime'/('native-'+instance+'-capture.sock'),0o600)
        capture.listen(4);capture.setblocking(False)
        last_report=None;capture_until=0
        test_until=0;test_values=(0,0,0)
        buffer=b'';pending={};sequence=0;previous={};menu=None;overlay_before=False;unknown=set()
        side=owner.side;runtime=root/'runtime';menu_side=shared.overlay_target(runtime,side)
        published_at=0;active_until=0
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as ipc:
            ipc.setblocking(False)
            def send(actions):
                for action in actions:ipc.sendto(f'MENU {menu_side} {action}'.encode(),str(runtime/'hud-ipc.sock'))
            def reset_input():
                nonlocal gate,last_report,menu,previous,sequence
                if last_report is not None:
                    packet=gate.neutral(last_report);sequence+=1
                    frame=struct.pack('<III',1,sequence,len(packet))+packet
                    if os.write(proc.stdin.fileno(),frame)!=len(frame):raise RuntimeError('short disconnect neutralization')
                gate=make_gate();last_report=None;menu=None;previous={};pending.clear()
            while proc.poll() is None:
                now=time.monotonic()
                if health.expired(now):reset_input()
                if now-published_at>=.5:
                    status=dict(health.snapshot(now),protocol=protocol,active=now<active_until,withheld_reports=sorted(unknown,key=repr))
                    status.update(owner.status(status['connected'],gate.phase))
                    temporary=runtime/('native-'+instance+'-status.new')
                    temporary.write_text(json.dumps(status));temporary.chmod(0o600)
                    temporary.replace(runtime/('native-'+instance+'-status.json'));published_at=now
                readable=select.select([proc.stdout,capture],[],[],.5)[0]
                if capture in readable:
                    connection,_=capture.accept();connection.settimeout(.05)
                    try:
                        credentials=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                        request=connection.recv(64)
                        if credentials[1]==0 and tx.get('native_test_controls') is True and request.startswith(b'TEST '):
                            try:
                                values=tuple(map(int,request[5:].split()))
                                if len(values)!=3:raise ValueError('expected buttons and two axes')
                                if policy is None:raise ValueError('no parser for this controller')
                                policy.fixture_report(getattr(policy,'TEMPLATE',bytes([0x42])+bytes(53)),*values)
                                test_values=values;test_until=time.monotonic()+.2
                                connection.send(b'READY')
                            except ValueError:connection.send(b'INVALID')
                            connection.close()
                        elif credentials[1]==0 and request.startswith(b'ROUTE '):
                            sequence+=1
                            if owner.request(request,sequence,connection):
                                side=owner.side;menu_side=shared.overlay_target(runtime,side)
                                reset_input();overlay_before=False;test_until=0
                        elif credentials[1]!=0 or request!=('CAPTURE '+side).encode():
                            connection.send(b'WAIT');connection.close()
                        else:
                            capture_until=time.monotonic()+1
                            sequence+=1;barriers[sequence]=connection
                            # An asleep controller has no report to neutralize.
                            # Still flush the CUSE queue and await its barrier;
                            # the capture lease gates any newly arriving input.
                            packet=gate.neutral(last_report) if last_report is not None else b''
                            frame=struct.pack('<III',3,sequence,len(packet))+packet
                            if os.write(proc.stdin.fileno(),frame)!=len(frame):raise RuntimeError('short capture write')
                    except (OSError,ValueError):
                        connection.close()
                if proc.stdout not in readable:continue
                chunk=os.read(proc.stdout.fileno(),65536)
                if not chunk:raise RuntimeError('native relay disconnected: its controller went away; the unit starts the relay again, which follows the slot')
                buffer+=chunk
                while len(buffer)>=12:
                    kind,seq,size=struct.unpack_from('<III',buffer)
                    if size>4096:raise RuntimeError('oversized report')
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
                    if kind!=1:raise RuntimeError('invalid relay frame')
                    now=time.monotonic()
                    target=shared.overlay_target(runtime,side)
                    if target!=menu_side:
                        reset_input();menu_side=target;overlay_before=False
                    available,overlay=shared.state(runtime,menu_side) if side!='unassigned' else (False,False)
                    if policy is None:
                        # No parser: nothing is gated, nothing is decoded.
                        packets=[data];flush=False;actions=[]
                    elif not policy.valid(data):
                        # Known non-control telemetry may pass without decoding.
                        if data and (data[0],len(data)) in ((0x43,15),(0x79,2)):
                            if data[0]==0x79 and data[1]==1 and health.disconnect():reset_input()
                            packets=[data];flush=False;actions=[]
                        else:
                            marker=(data[0] if data else None,len(data))
                            if marker not in unknown:
                                print(json.dumps({'withheld_report':marker}),flush=True);unknown.add(marker)
                            continue
                    else:
                        health.input(now)
                        if not policy.released(data,menu=True):active_until=now+.5
                        if now<test_until:data=policy.fixture_report(data,*test_values)
                        last_report=data
                        if overlay:capture_until=0
                        packets,flush,actions,ready=gate.step(data,now,overlay,available and now>=capture_until)
                        if overlay!=overlay_before:menu=None;previous={};overlay_before=overlay
                        if ready:
                            if menu is None:menu=menu_module.ControllerMenu(actions.append,lambda:True,{0:(-32768,32767),1:(-32768,32767)})
                            else:menu.send=actions.append
                            values=policy.menu_values(data)
                            for key,value in values.items():
                                if previous.get(key,0)!=value:menu.event(*key,value,now)
                            previous=values;menu.tick(now)
                    active=shared.app_active(runtime,side)
                    for index,packet in enumerate(packets):
                        if (menu_side != side or not active) and policy is not None and policy.valid(packet):
                            # Menu control may follow the full screen, but do
                            # not queue controls into a hidden/frozen client.
                            packet=gate.neutral(packet)
                            flush=True
                        sequence+=1;ack=bool(actions) and index==len(packets)-1
                        if ack:pending[sequence]=actions[:]
                        flags=int(flush and index==0)|2*ack
                        frame=struct.pack('<III',flags,sequence,len(packet))+packet
                        if os.write(proc.stdin.fileno(),frame)!=len(frame):raise RuntimeError('short relay write')
            raise RuntimeError('native transport exited')
    finally:
        if 'owner' in locals():owner.close()
        if capture is not None:capture.close()
        for connection in barriers.values():connection.close()
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        if node.exists():raise RuntimeError('relay endpoint remains after exit')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--instance',default='controller')
    args=parser.parse_args();run(args.root,args.instance)
