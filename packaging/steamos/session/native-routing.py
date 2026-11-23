#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Retained-trial native controller adapter for the shared ownership registry.

Runs beside the capture router. Preferences retain Main/Guest ownership; a single
screen moves nothing. A paired move prepares incoming Main first and
rolls back on failure. No physical device is bound into a Steam namespace.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import time


def module(name,filename):
    path=Path(__file__).with_name(filename)
    if not path.exists() and filename=='dosplex-controller-registry.py':
        path=Path(__file__).resolve().parents[1]/filename       # a source checkout: packaging/steamos, one folder up
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


registry=module('registry','dosplex-controller-registry.py')
PREFERENCES=Path('/var/lib/dosplex/controllers')
delivery=module('hid_delivery','native-hid-delivery.py')


def preferences(root):
    """The directory of the saved owners and numbers of the controllers.

    It lives outside the trial (which is deleted at every stop), so that an owner the user chose is still there after a stop
    and after a reboot. Only root writes it (mode 0700). The session hands the same path to the compositor as
    DOSPLEX_CONTROLLER_DIRECTORY (update-retained-session.py), and it is the arbiter's own default."""
    return PREFERENCES


def effective_side(target, state):
    primary=state['primary_side']
    if primary not in ('left','right'):raise ValueError('invalid Main side')
    if target=='unassigned':return 'unassigned'
    if target not in ('main','guest'):raise ValueError('invalid controller target')
    # The layout does not move a controller. A single screen used to hand Guest's controllers to Main, so Main's Steam met a new
    # controller (and was poked for it) when Guest went to standby, rebuilt its virtual gamepad and the game kept the old one;
    # the split back took it away again. Guest's controllers stay with Guest: its session is frozen or hidden with its
    # ownership retained, and Main's Steam is left alone. Main's menu still hears every controller (the relay's overlay_target).
    if target=='main':return primary
    return 'right' if primary=='left' else 'left'


def transfer_order(current, desired, primary):
    changed=[key for key in desired if current[key]!=desired[key]]
    return sorted(changed,key=lambda key: (desired[key]!=primary,current[key]==primary,key))


def stable_key(plan, kind):
    if plan.get('routing_id'):return plan['routing_id']
    path=(Path('/sys/class/hidraw')/Path(plan['relay']['target']).name/'device').resolve(strict=True)
    for parent in (path,*path.parents):
        if (parent/'idVendor').exists():
            identity=[kind,(parent/'idVendor').read_text().strip(),(parent/'idProduct').read_text().strip(),
                      (parent/'serial').read_text().strip() if (parent/'serial').exists() else str(parent)]
            return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    raise RuntimeError('native physical USB identity unavailable')


def route(endpoint, side):
    st=endpoint.lstat()
    import stat
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid or st.st_mode&0o077:
        raise PermissionError('untrusted native routing endpoint')
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as client:
        client.settimeout(2);client.connect(str(endpoint))
        if struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]!=0:
            raise PermissionError('untrusted native routing peer')
        client.sendall(('ROUTE '+side).encode())
        if client.recv(16)!=b'READY':raise RuntimeError('native routing barrier not ready')


def mount_namespaces(process,relay):
    """' (mount namespaces: PID NAMESPACE STATE, ...)' for the error of a controller Steam did not open, '' if unknown.

    Steam's 64-bit client runs in two mount namespaces: what the relay's path holds in each tells whether the delivery
    reached the client, which is the next thing to look at when a transfer fails.
    """
    try:found=delivery.describe(process,relay)
    except Exception:return ''
    return ' (mount namespaces: '+found+')' if found else ''


class Asleep(RuntimeError):
    """A wanted transfer waits: these controllers are asleep, or their relays are restarting."""
    def __init__(self,keys):
        super().__init__('Wake the controller before transferring controls');self.keys=frozenset(keys)


class Router:
    # A transfer that keeps failing is tried again after 2, 4, 8, 16 and then every 30 seconds (INP-N-08): each
    # try takes the controller from its owner for a moment. A new choice, or a controller that wakes, is tried at once.
    RETRY_WAITS=(2,4,8,16,30)
    AWAKE_GRACE=30  # seconds a controller still counts as its player's after it was last seen awake (first_owners)
    recently_awake=frozenset();waiting=frozenset()
    ERROR_LINE_EVERY=60  # seconds between two equal error lines in the log
    failures=0;failed_desired=None;failed_connected=None;error_said=None;error_said_at=0
    held_said=None  # the last hold of Main's controllers that the log named (said once, and again when it changes or ends)
    # The controllers whose Devices rows show self.error. None: every row, for an error that is the adapter's own.
    error_keys=None
    # What the systemctl that did not answer in this tick said, or None. systemd is one process: when it does not answer for the
    # unit of one side it does not for the other's either, and each wait is 3 s of the thread that keeps every row fresh (the
    # compositor drops them when they are 5 s old). So systemd is asked once per tick: the rest of the tick, the restore of a
    # transfer and the check of the other controllers included, fails at once with that answer, and the next tick asks again.
    systemd_error=None

    def __init__(self,root):
        self.root=root;self.runtime=root/'runtime';self.devices={}
        self.registry=registry.Registry(preferences(root),self.runtime/'controllers-native.json')
        self.error='';self.error_keys=None;self.retry_at=0;self.delivery_due=0;self.delivery_errors={}
        tx=registry.read_json(root/'transaction.json')
        for kind,name,default in module('ownership','native-ownership.py').device_specs(tx):
            path=root/'bundle/libexec'/('native-'+kind+'-plan.json')
            plan=registry.read_json(path);key=stable_key(plan,kind)
            # A controller with a saved record keeps its owner and its number; one without (or whose record cannot be read) is
            # recorded now with the owner it was handed over with, which is Main's unless the start named another.
            self.registry.observe(key,name,default,backend='native')
            self.devices[key]=dict(kind=kind,name=name,plan=plan,path=path,
                                   endpoint=self.runtime/('native-'+kind+'-capture.sock'))
        delivery.protect_sources(root,[device['plan'] for device in self.devices.values()])
        self.current={key:self.plan(key).get('routing_side',device['plan']['side'])
                      for key,device in self.devices.items()}
        self.pads=self.start_pads(root,tx)

    def start_pads(self,root,tx):
        """The manager of the generic (Bluetooth and USB) pads, or None. A failure here never stops the receivers' routing.

        Its host changes are the runtime udev rule and the D-Bus policy that keeps org.bluez from Guest (native-pads.py install);
        recovery removes them. A session whose Guest policy is not in place raises: Guest must not reach Bluetooth."""
        if not tx.get('native_generic_pads',True) or not isinstance(tx.get('steam_input_owners'),dict):return None
        try:pads=module('pads','native-pads.py')
        except Exception as error:
            print(json.dumps({'native_pads_off':str(error)[:200]}),flush=True);return None
        # The policy is on the bus since before the compositor started (install_policy in the session's start). A
        # failure here leaves it there, and the receivers' routing goes on; only a session whose policy is NOT in place
        # must not go on with a Guest who could reach Bluetooth.
        guest_user=tx.get('guest_user')
        try:print(json.dumps({'native_pads_installed':pads.install(root,guest_user)}),flush=True)
        except Exception as error:
            print(json.dumps({'native_pads_install_failed':str(error)[:200]}),flush=True)
        if not pads.policy_in_place(root,guest_user):
            raise RuntimeError('Guest is not kept from Bluetooth: its policy is not in place')
        try:
            return pads.PadManager(root,self.registry,registry,tx['steam_input_owners'],effective_side,monitor=pads.UdevMonitor())
        except Exception as error:
            print(json.dumps({'native_pads_off':str(error)[:200]}),flush=True);return None

    def index(self,key):
        """The number of the controller, the one the overlay shows (the registry gives it with the record)."""
        return getattr(self.registry,'devices',{}).get(key,{}).get('index')

    def plan(self,key):return registry.read_json(self.devices[key]['path'])

    def deliver(self,side,relay):
        """delivery.deliver(), unless systemd did not answer a moment ago in this tick (systemd_error)."""
        if self.systemd_error is not None:raise delivery.SystemdUnresponsive(self.systemd_error)
        try:return delivery.deliver(self.root,side,relay)
        except delivery.SystemdUnresponsive as error:
            self.systemd_error=str(error);raise

    def status(self,key):
        device=self.devices[key]
        value=registry.read_json(self.runtime/('native-'+device['kind']+'-status.json'))
        if value.get('routing_version')!=1 or not 0<=time.time()-value['updated']<2:
            raise RuntimeError('native routing status is stale')
        return value

    def fresh_status(self,key):
        """The relay's status, or None while that relay is down or restarting (its status is stale or missing).

        One such relay must not stop routing for the others: it counts as not connected and is skipped.
        """
        try:return self.status(key)
        except Exception:return None

    def move(self,key,side,wait=True):
        device=self.devices[key];relay=self.plan(key)['relay']
        was=self.current.get(key)
        process=self.deliver(side,relay) if side!='unassigned' else None
        # A side that holds the mount already (a controller that goes back to it) has no recovery of delivery's, but each change of
        # owner revoked its Steam's handle and its next open was refused, so the side a controller is handed to is noted, in each of
        # its namespaces. That is before the route is switched: an owner change that the relay makes after route() stopped waiting
        # for its READY (a slow feature report holds the relay's ioctl lock) is noted then too, and the check pokes it once the plan
        # says so. A note is no poke. It does not raise.
        if process and side!=was:delivery.recovered_family(process,relay)
        route(device['endpoint'],side)
        self.current[key]=side
        # A Steam in a container does not take the udev add that follows (delivery.rescan_poke says why): it is poked, once for each
        # recovery. That is after the route was switched, so that what Steam then opens answers for the new side, and before the
        # announcement, which waits for udev to settle (up to 3 s): the Steam that needs the poke would wait for that, to spare the
        # one that does not the start of a helper for each of its namespaces. The check (reconcile_consumers) announces first,
        # because its announcement does not wait. It does not raise.
        if process:delivery.rescan_poke(process,relay)
        delivery.notify(self.root,relay)
        if process and wait:
            deadline=time.monotonic()+4
            notify_at=time.monotonic()+1
            while not delivery.opened(process,relay):
                if time.monotonic()>deadline:
                    raise RuntimeError(device['name']+': Steam did not open transferred controller'
                                       +mount_namespaces(process,relay))
                # A rapid Undo can race Steam removing its revoked device.
                # Repeat the same scoped announcement after removal settles;
                # never restart Steam or unbind the physical device.
                if time.monotonic()>=notify_at:
                    # (the poke asks again with it, but it is made once more at most, and not before 1.5 s after the first)
                    delivery.rescan_poke(process,relay)
                    delivery.notify(self.root,relay);notify_at=time.monotonic()+1
                time.sleep(.05)
        # Steam has the controller, or there is nothing to deliver. What an earlier attempt said about this
        # controller no longer holds; the Devices row must not go on saying that the transfer failed.
        if wait or not process:self.delivery_errors.pop(key,None)

    def first_owners(self,state,statuses):
        """Give each controller that is awake for the first time its first owner (registry.first_owner).

        A receiver slot that was enrolled without a named owner belongs to nobody until a controller wakes on it: an
        empty slot is nobody's controller and nothing waits for it. The first one that wakes is Main's; the next is the
        second player's while they are on the screen with none. Counted are the controllers that are awake and have an
        owner: the receivers' own, the Steam Deck's controls and the other gamepads."""
        awake=lambda key:statuses[key] is not None and statuses[key]['connected']
        # A controller counts for the others while it is awake and for AWAKE_GRACE seconds after it was last seen awake.
        # Seen on the Steam Machine with 0.1.0-alpha.6: a gamepad plugged into USB was given to Main although Main's
        # Steam Controller was in use; at that instant the Steam Controller's status was missing or said asleep (a USB
        # device that arrives can hold up the others on the bus for a moment), so Main seemed to have nothing.
        now=time.monotonic();seen=self.__dict__.setdefault('awake_at',{})
        for key in self.devices:
            if awake(key):seen[key]=now
        self.recently_awake=frozenset(key for key in self.devices if now-seen.get(key,-1e9)<self.AWAKE_GRACE)
        if not hasattr(self.registry,'pending'):
            self.waiting=frozenset();return
        pads=getattr(self,'pads',None)
        for key in sorted(self.devices,key=lambda key:(self.index(key) or 0,key)):
            if not awake(key) or not self.registry.pending(key):continue
            owners=[self.registry.target(other) for other in self.devices
                    if other!=key and other in self.recently_awake and not self.registry.pending(other)]
            if pads is not None and hasattr(pads,'awake_owners'):owners+=pads.awake_owners()
            owner=registry.first_owner(state,'main' in owners,'guest' in owners)
            self.registry.decide(key,owner)
            # What the decision saw goes into the log with it: a first owner that surprises somebody can be explained.
            print(json.dumps({'native_first_owner':{'id':key[:8],'number':self.index(key),
                'name':self.devices[key].get('name'),'owner':owner,'layout':state.get('layout'),
                'main_has':'main' in owners,'guest_has':'guest' in owners}}),flush=True)
        # The receiver slots that still wait for a controller: Devices lists them after the real controllers (tick).
        self.waiting=frozenset(key for key in self.devices if self.registry.pending(key))

    def tick(self):
        self.systemd_error=None
        state=registry.read_json(self.runtime/'state.json')
        self.forget_requests()
        statuses={key:self.fresh_status(key) for key in self.devices}
        try:self.first_owners(state,statuses)
        except Exception as error:  # (a record that cannot be written: the controller waits, the others are routed)
            print(json.dumps({'native_first_owner_wait':str(error)[:160]}),flush=True)
        requested={key:self.registry.target(key) for key in self.devices}
        desired={key:effective_side(target,state) for key,target in requested.items()}
        self.current={key:self.plan(key).get('routing_side',self.devices[key]['plan']['side']) for key in self.devices}
        order=transfer_order(self.current,desired,state['primary_side'])
        # Every controller counts, not only those that are to move: one of Main's that wakes lets a held one leave (below).
        connected=frozenset(key for key in self.devices if statuses[key] is not None and statuses[key]['connected'])
        if not order:self.error='';self.error_keys=None;self.failures=0;self.error_said=None;self.retry_at=0
        changed=bool(self.failures) and (desired!=self.failed_desired or connected!=self.failed_connected)
        if order and (changed or time.monotonic()>=self.retry_at):
            before=dict(self.current)
            failing=None  # the controller whose move raised; none for an error that is no controller's
            try:
                # A controller that is asleep, or whose relay restarts, moves when it wakes. While one of them is on its
                # way to Main, no controller leaves Main: do not exchange recovery controls for a device that is asleep,
                # its controls may be the ones Main gets for those it gives away. Everything else moves. Seen on the
                # Deck and on the Steam Machine: sleeping spare slots, which are Main's by default, kept every other
                # controller where it was.
                # That hold is for a Main that would be left with nothing: when another controller that is awake is Main's
                # and stays (or comes to Main in this same tick, before anything leaves), a controller leaves Main at once.
                # Seen on the Steam Machine with 0.1.0-alpha.5, first start after a new install: six empty receiver slots,
                # unassigned and Main's by default, never wake, and no controller could be given to Guest ("Transfer
                # failed; controls returned") although Main kept another one.
                asleep={key for key in order if statuses[key] is None or not statuses[key]['connected']}
                held=set(asleep)
                main_keeps_one=any(desired[key]==state['primary_side'] and statuses[key] is not None
                                   and statuses[key]['connected'] for key in self.devices)
                if not main_keeps_one and any(desired[key]==state['primary_side'] for key in asleep):
                    held.update(key for key in order if self.current[key]==state['primary_side'])
                self.say_held(sorted(held-asleep),sorted(key for key in asleep if desired[key]==state['primary_side']))
                for key in order:
                    if key in held:continue
                    failing=key
                    self.move(key,desired[key])
                if held:before=dict(self.current);failing=None;raise Asleep(held)  # what moved stays; the rest waits
                self.error='';self.error_keys=None;self.failures=0;self.error_said=None
            except Exception as error:
                self.error=str(error)[:180]
                # A move that failed concerns that controller: the rows of the others must not say that their transfer failed.
                # Waiting for a controller that is asleep is no failure of the controllers that are awake: only its own row
                # carries it (and that row says Disconnected).
                self.error_keys=set(error.keys) if isinstance(error,Asleep) else None if failing is None else {failing}
                for key in transfer_order(self.current,before,state['primary_side']):
                    try:self.move(key,before[key])
                    except Exception as restore_error:
                        self.error+='; restore: '+str(restore_error)[:120]
                        if self.error_keys is not None:self.error_keys.add(key)
                self.failures+=1;self.failed_desired=dict(desired);self.failed_connected=connected
                now=time.monotonic();self.retry_at=now+self.RETRY_WAITS[min(self.failures,len(self.RETRY_WAITS))-1]
                if self.error!=self.error_said or now-self.error_said_at>=self.ERROR_LINE_EVERY:
                    print(json.dumps({'native_routing_error':self.error}),flush=True)
                    self.error_said=self.error;self.error_said_at=now
        self.reconcile_consumers(state)
        rows=[]
        for key,device in self.devices.items():
            status=self.fresh_status(key) or dict(connected=False,captured=False);target=self.current[key]
            # The row says where the controller is delivered now (a layout moves nothing, effective_side): the owner it asked for
            # while that is in place, else the side that holds it.
            target=requested[key] if target==desired[key] else (
                'unassigned' if target=='unassigned' else 'main' if target==state['primary_side'] else 'guest')
            rows.append(dict(id=key,name=device['name'],index=self.index(key),connected=status['connected'],
                awake_recently=bool(status['connected'] or key in self.recently_awake),
                waiting=bool(key in self.waiting and not status['connected']),
                target=target,requested=requested[key],active=status.get('active',False),node='',backend='native',
                input_status=dict(shortcut_ready=status['connected'],captured=status['captured']),
                routing_error=self.row_error(key)))
        pads=getattr(self,'pads',None)
        if pads is not None:
            # Generic pads: they take a first owner by ADR-014 counting the rows above, and are listed beside them.
            pads.tick(state,rows);rows.extend(pads.rows(state))
        # Devices shows the rows in this order, five to a page: the controllers that are connected first, then the ones
        # DosPlex knows that are asleep or off, then the receiver slots no controller has woken on yet; by their number
        # within each. Seen on the Steam Machine with 0.1.0-alpha.6: seven empty slots filled the first page and the
        # gamepad that had just been plugged in was on the second, where its owner did not look (the owner: "can we
        # show connected controllers first in the list"). A row moves when its controller wakes or sleeps; a move in
        # Devices names the controller in its question, so a press that lands on a row that just moved can be undone.
        rows.sort(key=lambda row:(0 if row.get('connected') else 2 if row.get('waiting') else 1,
                                  row.get('index') or 10**6))
        registry.write_json(self.runtime/'controllers-native.json',
                            dict(updated=time.time(),assignment_preview=2,devices=rows))

    def say_held(self,kept,waited_for):
        """One log line when controllers that are awake are kept on Main, naming them and the sleeping ones Main waits for.

        The row of such a controller says only that its transfer failed; the log must say which controllers the router
        kept and why. Said once for each distinct hold, and once when it ends."""
        name=lambda key:dict(id=key[:8],number=self.index(key),name=self.devices[key].get('name'))
        note=(tuple(kept),tuple(waited_for)) if kept else None
        if note==self.held_said:return
        self.held_said=note
        if note is None:print(json.dumps({'native_routing_hold_ended':True}),flush=True)
        else:print(json.dumps({'native_routing_hold':{
            'kept_on_main':[name(key) for key in kept],
            'reason':'Main has no other controller that is awake, and these are asleep and wanted by Main',
            'asleep_wanted_by_main':[name(key) for key in waited_for]}}),flush=True)

    def forget_requests(self):
        """Carry out the forget requests in the controller directory, for controllers that are off or asleep. A gamepad loses
        its record (its number is free again) and its row. A slot of this session's receiver cannot lose its record while the
        session holds it: it becomes nobody's and waits for its first owner again, so its row leaves the page like an empty
        slot's and the controller that next wakes on it comes back as a new one (the owner asked for a way to drop a Steam
        Controller from the list, and what happens when it is turned on again)."""
        if not hasattr(self.registry,'forget_requests'):return
        pads=getattr(self,'pads',None)
        connected={key for key in self.devices if (self.fresh_status(key) or {}).get('connected')}
        connected.update(getattr(pads,'pads',()))
        for key in self.registry.forget_requests(connected,session=self.devices):
            if key in self.devices:
                print(json.dumps({'native_controller_waits_again':key,'name':self.devices[key].get('name')}),flush=True)
                continue
            if hasattr(pads,'forget'):pads.forget(key)
            print(json.dumps({'native_controller_forgotten':key}),flush=True)

    def row_error(self,key):
        """What the Devices row of a controller says went wrong: a failed transfer that concerns it, else its delivery check."""
        if self.error and (self.error_keys is None or key in self.error_keys):return self.error
        return self.delivery_errors.get(key,'')

    def reconcile_consumers(self,state):
        """A restarted Steam needs its claimed HIDs even without a route change.

        Re-delivery never changes UID ownership or revokes existing readers.
        Validate every destination afresh; one restarting side must not prevent
        the other side's devices from being checked.
        """
        if time.monotonic()<self.delivery_due:return
        self.delivery_due=time.monotonic()+2
        for key,device in self.devices.items():
            side=self.current[key]
            if side=='unassigned' or state.get(side,{}).get('activity','active')!='active':
                self.delivery_errors.pop(key,None);continue
            try:
                if self.fresh_status(key) is None:
                    # The relay is restarting: nothing to deliver until its endpoint is back.
                    self.delivery_errors.pop(key,None);continue
                relay=self.plan(key)['relay']
                process=self.deliver(side,relay)
                if not self.status(key)['connected']:
                    self.delivery_errors.pop(key,None);continue
                if delivery.opened(process,relay):
                    self.delivery_errors.pop(key,None)
                else:
                    # Repeated every two seconds until Steam has it: do not hold this thread for udev to settle.
                    # The udev add goes first: a Steam that uses udev hears it at once and has no use for the poke, which starts a
                    # helper for each of its namespaces. A Steam in a container does not hear it and is poked next, with nothing in
                    # between that waits (settle=False). The poke cannot fail, and a notify that raises must not keep it from its
                    # rescan, so it is in the finally. It is made once for each recovery, and once more; a first poke that the limit
                    # held back, or whose helper did not finish, is made here when it is due again.
                    try:delivery.notify(self.root,relay,settle=False)
                    finally:delivery.rescan_poke(process,relay)
                    self.delivery_errors[key]='Waiting for Steam to open this controller'
            except Exception as error:
                message=str(error)[:160]
                if self.delivery_errors.get(key)!=message:
                    print(json.dumps({'native_delivery_wait':device['name'],'reason':message}),flush=True)
                self.delivery_errors[key]=message


def run(root):
    router=None
    while root.exists():
        try:
            if router is None:router=Router(root)
            router.tick()
        except Exception as error:
            print(json.dumps({'native_routing_wait':str(error)[:250]}),flush=True)
        time.sleep(.25)
