#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Enroll an additional inspected Ibex receiver slot in the retained trial.

A new slot belongs to Main (ADR-014); one the user has set before keeps its saved owner. Recovery must be deployed before this tool. Existing
Steam clients and relays keep running; only the capture/routing service restarts.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
import uuid


# native-ownership.device_specs refuses more than 8 entries in native_extra_controllers. In an external-only
# trial the Main and Guest slots are two of them; with a built-in Deck pair they are not.
MAX_EXTRA_SLOTS=8
# A relay that fails starts again a second later, so one failed slot does not stop routing for the others. It has
# no start limit: with systemd's default (five starts in ten seconds) a relay that fails at once, a refused repin
# for example, would stay dead until DosPlex is stopped and started.
RELAY_RESTART=('--property=Restart=on-failure','--property=RestartSec=1','--property=StartLimitIntervalSec=0')
# The capture router, the output watcher and the slot guards start again after a failure (one uncaught exception would
# otherwise end routing, the menu chord or pad delivery until DosPlex is started again). Unlike the relays they have a start
# limit: one that fails ten times in two minutes stays failed, where the journal shows it, and does not spin.
SERVICE_RESTART=('--property=Restart=on-failure','--property=RestartSec=2','--property=StartLimitIntervalSec=120','--property=StartLimitBurst=10')


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


# Where the owners and numbers of the controllers are kept (native-routing.py PREFERENCES is the same place).
PREFERENCES=Path('/var/lib/dosplex/controllers')


def slot_name(specs=None):
    """A receiver slot is called 'Steam Controller'. Its number is the index the router gives it and the overlay shows."""
    return 'Steam Controller'


def saved_target(key,directory=None):
    """The owner saved for this controller, or None when there is no readable record (it then starts as Main's)."""
    try:
        meta=os.lstat((directory or PREFERENCES)/(key+'.json'))
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.geteuid() or meta.st_mode&0o022 or meta.st_size>65536:return None
        target=json.loads(((directory or PREFERENCES)/(key+'.json')).read_text()).get('target')
    except (OSError,ValueError,AttributeError):return None
    return target if target in ('main','guest','unassigned') else None


def check_capacity(tx,specs,token):
    extras=tx.get('native_extra_controllers',{})
    if token in extras or len(extras)>=MAX_EXTRA_SLOTS or len(specs)>=MAX_EXTRA_SLOTS+2:
        raise ValueError('native slot limit/collision')


def run(root,hid):
    if os.geteuid() or root.parent!=Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):
        raise RuntimeError('requires root and retained trial')
    lib=root/'bundle/libexec';runtime=root/'runtime'
    shared=load('shared',lib/'native-deck-relay.py');shared.trusted(root/'transaction.json')
    fd=os.open(root/'recovery.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        meta=os.fstat(fd)
        if meta.st_uid or meta.st_mode & 0o022 or not stat.S_ISREG(meta.st_mode):
            raise RuntimeError('untrusted trial lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return enroll_locked(root,hid)
    finally:os.close(fd)


def enroll_locked(root,hid,sysroot=Path('/sys')):
    lib=root/'bundle/libexec';runtime=root/'runtime'
    recovery=(root/'root-session-trial.py').read_text()
    if 'def recover_extra_native_controllers(' not in recovery:raise RuntimeError('install extra-slot recovery first')
    tx=json.loads((root/'transaction.json').read_text())
    if tx.get('phase')!='started' or not tx.get('keep_session'):raise RuntimeError('inactive trial')
    if not re.fullmatch('/dev/hidraw[0-9]+',str(hid)):raise ValueError('invalid HID node')
    device=(sysroot/'class/hidraw'/hid.name/'device').resolve(strict=True)
    if not re.search(r'^HID_ID=0003:000028DE:0000130[45]$', (device/'uevent').read_text(), re.M):  # 1304 and 1305 pucks are one family
        raise ValueError('expected inspected Ibex receiver')
    interface=device.parent;usb=interface.parent
    identity=[(usb/'idVendor').read_text().strip(),(usb/'idProduct').read_text().strip(),
              (usb/'serial').read_text().strip() if (usb/'serial').exists() else str(usb),
              (interface/'bInterfaceNumber').read_text().strip()]
    key=hashlib.sha256(json.dumps(['native-receiver-slot',*identity]).encode()).hexdigest()[:32]
    token=key[:8];instance='controller-'+token
    ownership=load('ownership',lib/'native-ownership.py')
    specs=ownership.device_specs(tx)
    plans=[json.loads((lib/('native-'+kind+'-plan.json')).read_text()) for kind,_,_ in specs]
    if any(any(record['path']==str(hid) for record in plan['devices']) for plan in plans):
        raise ValueError('receiver slot already enrolled')
    check_capacity(tx,specs,token)
    native=load('native',lib/'native-deck-plan.py')
    side='left';uid=tx['steam_input_owners'][side]
    plan=native.create_for_devices(side,uid,[hid])
    # The slot starts held by nobody. The router hands it to its saved owner on its first pass; a slot without one gets
    # its first owner when a controller is first awake on it (ADR-014 as amended 2026-11-23: 'auto').
    plan.update(protocol='ibex',relay_id=uuid.uuid4().hex,routing_id=key,
                routing_side='unassigned',routing_uid=0)
    entry=dict(name=slot_name(specs),default_target='auto',plan=plan,
        unit='dosplex-native-controller-'+token+'-'+tx['id']+'.service',
        guard_unit='dosplex-native-slot-guard-'+token+'-'+tx['id']+'.service')
    delivery=load('delivery',lib/'native-hid-delivery.py')
    def save():delivery.d.save_delivery_ledger(root/'transaction.json',tx)
    def start(unit,script,args,restart=()):
        subprocess.run(['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,
            '--property=KillMode=control-group','--property=BindsTo='+tx['unit'],*restart,
            '--','/usr/bin/python3',str(lib/script),*args],check=True,timeout=10)
    # All resources and original ACLs are journaled before starting containment.
    tx.setdefault('native_extra_controllers',{})[token]=entry;save()
    planpath=lib/('native-'+instance+'-plan.json');delivery.d.save_delivery_ledger(planpath,plan)
    subprocess.run(['systemctl','stop',tx['native_capture_router_unit']],check=True,timeout=10)
    try:
        start(entry['guard_unit'],'native-slot-guard.py',['--interface',str(interface)],SERVICE_RESTART)
        start(entry['unit'],'native-controller-relay.py',['--root',str(root),'--instance',instance],RELAY_RESTART)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            plan=json.loads(planpath.read_text())
            if plan.get('relay') and Path(plan['relay']['path']).exists():break
            time.sleep(.05)
        else:raise RuntimeError('extra native relay startup timed out')
        entry['plan']=plan;save()
        delivery.protect_sources(root,[*plans,plan])
    finally:
        path=runtime/'native-capture.sock'
        if path.exists():
            meta=path.lstat()
            if meta.st_uid or not stat.S_ISSOCK(meta.st_mode):raise RuntimeError('untrusted capture socket')
            path.unlink()
        start(tx['native_capture_router_unit'],'native-capture-router.py',['--root',str(root)],SERVICE_RESTART)
    print(json.dumps(dict(enrolled=instance,name=entry['name'],target=saved_target(key) or entry['default_target'],id=key)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--hid',type=Path,required=True)
    args=parser.parse_args();run(args.root,args.hid)
