#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Add a journaled native Ibex pair and real audio to a fresh retained trial.

Private bootstrap for a machine without built-in Deck controls. Requires a
verified bundle. Main's and Guest's receiver slots (USB 1304 or 1305) are
optional, and a slot that is given is never listened to: a controller that is
asleep joins like any other. Every other HID interface slot of the same
receiver(s) then joins as Unassigned through enroll-native-controller.py. With
no slot at all, every slot of every receiver that is plugged in joins, and
with no receiver the trial still starts (keyboard, mouse and generic pads).
Failure preserves the journal for root-session-trial.py recover; no fallback
exposes original HIDs or makes an unowned global device accessible.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid


# native-ownership.device_specs refuses more than 8 entries in native_extra_controllers, and the Main and
# Guest slots are two of them. The same limit bounds the recovery and update tools.
MAX_ENROLLED_SLOTS = 8
# A relay that fails is started again a second later, so that one failed slot does not stop the others. It has no
# start limit: with systemd's default (five starts in ten seconds) a relay that fails at once, a refused repin for
# example, would stay dead until Dosplex is stopped and started.
RELAY_RESTART = ('--property=Restart=on-failure', '--property=RestartSec=1', '--property=StartLimitIntervalSec=0')
# The capture router, the output watcher and the slot guards start again after a failure (one uncaught exception would
# otherwise end routing, the menu chord or pad delivery until Dosplex is started again). Unlike the relays they have a start
# limit: one that fails ten times in two minutes stays failed, where the journal shows it, and does not spin.
SERVICE_RESTART=('--property=Restart=on-failure','--property=RestartSec=2','--property=StartLimitIntervalSec=120','--property=StartLimitBurst=10')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def inspect_slot(hid, sysroot=Path('/sys')):
    if not re.fullmatch('/dev/hidraw[0-9]+', str(hid)):
        raise ValueError('invalid HID path')
    device = (sysroot / 'class/hidraw' / hid.name / 'device').resolve(strict=True)
    if not re.search(r'^HID_ID=0003:000028DE:0000130[45]$', (device / 'uevent').read_text(), re.M):  # 1304 and 1305 pucks are one family
        raise ValueError('only the USB 1304 and 1305 receivers are currently enrolled by this bootstrap')
    interface = device.parent
    usb = interface.parent
    identity = [(usb / 'idVendor').read_text().strip(), (usb / 'idProduct').read_text().strip(),
                (usb / 'serial').read_text().strip() if (usb / 'serial').exists() else str(usb),
                (interface / 'bInterfaceNumber').read_text().strip()]
    key = hashlib.sha256(json.dumps(['native-receiver-slot', *identity]).encode()).hexdigest()[:32]
    return interface, key


def plan_unassigned_slots(main_hid, guest_hid, enrolled=2, sysroot=Path('/sys'), all_receivers=False):
    """The slots that start Unassigned: every other HID interface of the receiver(s) of Main and Guest.

    With all_receivers, and no slot given, it is every interface of every receiver that is plugged in.

    Looks only at the kernel's device tree. A controller does not have to be awake: an idle slot is
    enrolled too, so that a controller switched on later shows up in Devices. Returns (to enroll,
    skipped as (path, reason)). `enrolled` is how many slots the trial already holds; the 8-slot limit
    counts them, so Main, Guest and six more fit. The order is fixed (receiver, interface, node).
    """
    taken = {}
    receivers = set()
    given = [h for h in (main_hid, guest_hid) if h is not None]    # a Steam Deck has no Main receiver slot
    for hid in given:
        interface, key = inspect_slot(hid, sysroot)
        taken[key[:8]] = str(hid)
        receivers.add(interface.parent.resolve())
    found = []
    for entry in sorted((sysroot / 'class/hidraw').glob('hidraw*')):
        hid = Path('/dev') / entry.name
        if str(hid) in map(str, given):
            continue
        try:
            interface, key = inspect_slot(hid, sysroot)
            node = int(entry.name[len('hidraw'):])
        except (OSError, ValueError):
            continue  # Not a 28de:1304/1305 interface, or one that vanished while looking.
        if interface.parent.resolve() in receivers or (all_receivers and not given):
            try:
                number = int((interface / 'bInterfaceNumber').read_text().strip(), 16)  # sysfs prints hex
            except ValueError:
                number = 0
            found.append((str(interface.parent.resolve()), number, node, hid, key))
    found.sort(key=lambda item: item[:3])
    selected, skipped = [], []
    room = max(0, MAX_ENROLLED_SLOTS - enrolled)
    for _, _, _, hid, key in found:
        if key[:8] in taken:
            skipped.append((hid, 'the same slot identity as ' + taken[key[:8]]))
        elif len(selected) >= room:
            skipped.append((hid, 'more than %d slots' % MAX_ENROLLED_SLOTS))
        else:
            taken[key[:8]] = str(hid)
            selected.append(hid)
    return selected, skipped


def routing_ready(root, seconds=30, clock=time.monotonic, sleep=time.sleep, wall=time.time, expected=()):
    """Wait until the routing service publishes a device list, one made after this call began.

    Enrolling a slot restarts that service. Doing it while the service still starts would stop it half
    way through taking over the slots' source nodes, so wait for one that has settled. The list that the
    service left before it was stopped must not count, however recent it is: only a newer one does.
    `expected` names the controllers the list has to hold; the failure says which of them it lacked.
    """
    path = Path(root) / 'runtime/controllers-native.json'
    began = wall()
    deadline = clock() + seconds
    while True:
        try:
            published = json.loads(path.read_text())
            if began <= published['updated'] and 0 <= wall() - published['updated'] < 2:
                names = [row.get('name') for row in published.get('devices', []) if isinstance(row, dict)]
                if all(name in names for name in expected):
                    return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if clock() >= deadline:
            raise RuntimeError('native routing did not come up within %d seconds%s' % (seconds, waiting_for(path, expected)))
        sleep(.2)


def waiting_for(path, expected):
    """' (still waiting for: ...)': which expected controllers the routing list lacks, or that there is no list."""
    if not expected:
        return ''
    try:
        published = json.loads(Path(path).read_text())
        names = {row.get('name') for row in published.get('devices', []) if isinstance(row, dict)}
        missing = [name for name in expected if name not in names]
        return ' (still waiting for: %s)' % ', '.join(missing) if missing else ' (every expected controller is listed, but the list is not fresh: the routing service stopped publishing)'
    except (OSError, ValueError, AttributeError, TypeError):
        return ' (still waiting for: %s; the routing service published no list at all)' % ', '.join(expected)


def move_deck_capture_socket(runtime):
    """The capture router listens at native-capture.sock, where the Deck relay listens until it is moved aside.

    A Steam Deck's relay starts with the trial and holds that name, so the router (started later) would fail to bind
    and the routing thread inside it would die with it. The relay keeps its listening socket across a rename. Returns
    True when it was moved, False when it already was.
    """
    original, moved = Path(runtime) / 'native-capture.sock', Path(runtime) / 'native-deck-capture.sock'
    if moved.exists():
        return False
    metadata = original.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise RuntimeError('unexpected Deck capture socket')   # the relay is root's, as is this bootstrap
    original.rename(moved)
    return True


def enroll_unassigned_slots(root, load_enroll, main_hid, guest_hid, ready, sysroot=Path('/sys'), all_receivers=False):
    """Enroll the other slots of the receiver(s) as Unassigned, through enroll-native-controller.py.

    Called with the recovery lock held, after Main's and Guest's slots run. Every slot is journaled in
    transaction.json by enroll_locked before anything starts, so a failure here is rolled back by the
    trial's own recovery together with the first two slots. No input report is needed from these slots.
    Returns the enrolled /dev/hidraw paths.
    """
    transaction = json.loads((root / 'transaction.json').read_text())
    selected, skipped = plan_unassigned_slots(
        main_hid, guest_hid, len(transaction.get('native_extra_controllers', {})), sysroot, all_receivers)
    for hid, reason in skipped:
        print(json.dumps(dict(unassigned_slot_skipped=str(hid), reason=reason)), flush=True)
    if not selected:
        return []
    enroll_locked = load_enroll()
    enrolled = []
    for hid in selected:
        ready()
        enroll_locked(root, hid)
        enrolled.append(str(hid))
    ready()
    return enrolled


def publish_user_plan(delivery, path, plan):
    # Plans are root-write-only metadata, read by the unprivileged side wrapper.
    # The private recovery ledger itself stays0600.
    delivery.save_delivery_ledger(path, plan)
    path.chmod(0o644)


def builtin_deck_uinput_plan(deck_plan):
    """The /dev/uinput-only plan of a Steam Deck trial, with the record the Deck plan took before it granted anything.

    Only the device record is carried over. The Deck plan's 'relay' names a hidraw target of its own; copied into a
    plan that holds just /dev/uinput it fails native-deck-plan.py grant() ("relay target is not in the saved device
    plan") and restore() would look for a target the plan does not hold.
    """
    records = [r for r in deck_plan['devices'] if r['path'] == '/dev/uinput']
    if len(records) != 1:
        raise ValueError('the Steam Deck plan lacks the original /dev/uinput record')
    return {key: value for key, value in dict(deck_plan, devices=records).items() if key != 'relay'}


def run(root, main_hid, guest_hid, builtin_deck=False):
    if os.geteuid() or root.parent != Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}', root.name):
        raise ValueError('requires exact root retained trial')
    lib = root / 'bundle/libexec'
    tools = root / 'bundle/tools'
    shared = load('shared', lib / 'native-deck-relay.py')
    shared.trusted(root / 'transaction.json')
    for path in (lib, tools, tools / 'root-session-trial.py', lib / 'native-hid-relay'):
        shared.trusted(path)
    with (root / 'recovery.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tx = json.loads((root / 'transaction.json').read_text())
        if tx.get('phase') != 'started' or tx.get('keep_session') is not True or not tx.get('device_view_experiment'):
            raise ValueError('requires active retained device-view trial')
        # A Steam Deck starts with its own controls already carried (native-deck-relay.py, started with the trial);
        # they are Main's (ADR-014) and Guest's receiver slot, if any, joins them.
        fresh = ('native_controller_plan', 'native_extra_controllers', 'audio_trial_unit') + (
            () if builtin_deck else ('native_deck_plan',))
        if any(tx.get(k) for k in fresh):
            raise ValueError('only a fresh trial can use this bootstrap')
        if builtin_deck:
            if main_hid is not None:
                raise ValueError("on a Steam Deck Main's controls are the built-in ones; only Guest has a receiver slot")
            if not tx.get('native_deck_plan'):
                raise ValueError('the Steam Deck trial carries no plan for the built-in controls')
        elif main_hid is not None and main_hid == guest_hid:
            raise ValueError('each player needs a distinct receiver slot')
        slots = [h for h in (main_hid, guest_hid) if h is not None]
        inspected = [inspect_slot(h) for h in slots]
        if len({key for _, key in inspected}) != len(inspected):
            raise ValueError('duplicate receiver identity')
        # A slot that is given is never listened to: a controller that is asleep joins and works once it wakes.
        for name in ('root-session-trial.py', 'retained-audio-trial.py', 'restore-wine-modelist.py'):
            shutil.copyfile(tools / name, root / name)
            (root / name).chmod(0o700)
        trial = load('trial', root / 'root-session-trial.py')
        native = load('native', lib / 'native-deck-plan.py')
        delivery = load('delivery', lib / 'native-output-delivery.py')
        import pwd
        owners = dict(left=pwd.getpwnam(tx['main_user']).pw_uid, right=pwd.getpwnam(tx['guest_user']).pw_uid)
        if len(set(owners.values())) != 2 or any(uid < 1000 for uid in owners.values()):
            raise ValueError('two ordinary separate owners required')
        # The relays take root's synthetic controls only in a session that was started with test hooks (TRL-09).
        tx.update(native_external_only=True, native_builtin_deck=bool(builtin_deck),
                  native_test_controls=bool(tx.get('native_test_controls') or tx.get('test_hooks')),
                  steam_input_owners=owners,
                  native_capture_router_unit='dosplex-native-capture-' + tx['id'] + '.service',
                  native_output_unit='dosplex-native-output-' + tx['id'] + '.service', native_extra_controllers={})
        roles = [(side, hid, role) for side, hid, role in
                 zip(('left', 'right'), (main_hid, guest_hid), ('main', 'guest')) if hid is not None]
        for (side, hid, role), (interface, key) in zip(roles, inspected):
            token = key[:8]
            plan = native.create_for_devices(side, owners[side], [hid])
            plan.update(protocol='ibex', relay_id=uuid.uuid4().hex, routing_id=key,
                        routing_side=side, routing_uid=owners[side])
            tx['native_extra_controllers'][token] = dict(
                name='Steam Controller ' + ('1' if role == 'main' or builtin_deck else '2'), default_target=role,
                interface=str(interface), plan=plan,
                unit='dosplex-native-controller-' + token + '-' + tx['id'] + '.service',
                guard_unit='dosplex-native-slot-guard-' + token + '-' + tx['id'] + '.service')
        # Snapshot uinput once, before either account's ACL is granted.
        if builtin_deck:
            # The Deck's own plan took /dev/uinput with its original permissions when the trial started, and has
            # granted it since: a fresh snapshot would record the granted state as the one to restore.
            tx['native_uinput_plan'] = builtin_deck_uinput_plan(tx['native_deck_plan'])
        else:
            tx['native_uinput_plan'] = native.create_for_devices('left', owners['left'], [Path('/dev/uinput')])
        save = lambda: delivery.save_delivery_ledger(root / 'transaction.json', tx)
        save()  # Recovery owns every resource before any service/ACL mutation.
        # The journaled helpers (the sleep guard among them) are bound to the compositor too; they stop with it here
        # and start_compositor starts them again.
        children = trial.bound_children(tx['unit'], trial.journaled_native_units(tx))
        subprocess.run(['systemctl', 'stop', tx['unit']], check=True, timeout=45)
        for unit in children:
            subprocess.run(['systemctl', 'stop', unit], capture_output=True, timeout=30)
        if not all(trial.unit_dead(unit) for unit in [tx['unit'], *children]):
            raise RuntimeError('initial trial sessions did not stop')
        if (root / 'sides').exists():
            (root / 'sides').rename(root / ('before-native-sides-' + str(time.time_ns())))
        for side, uid in owners.items():
            plan = dict(tx['native_uinput_plan'], side=side, uid=uid)
            publish_user_plan(delivery, lib / ('native-' + side + '-uinput-plan.json'), plan)
            native.grant(plan)
        for token, entry in tx['native_extra_controllers'].items():
            delivery.save_delivery_ledger(lib / ('native-controller-' + token + '-plan.json'), entry['plan'])
        # Audio enrollment must precede session launch; its Unix socket starts
        # immediately after the compositor, while side wrappers wait for it.
        audio = load('audio_trial', root / 'retained-audio-trial.py')
        audio.run(root, 'prepare', simulated=False)
        tx = json.loads((root / 'transaction.json').read_text())
        tx['audio_socket_unit'] = 'dosplex-audio-socket-' + tx['id'] + '.service'
        save()
        updater = load('updater', tools / 'update-retained-session.py')
        updater.start_compositor(root, tx)
        def start(unit, script, args, restart=()):
            subprocess.run(['systemd-run', '--quiet', '--collect', '--service-type=exec', '--unit=' + unit,
                            '--property=BindsTo=' + tx['unit'], '--property=KillMode=control-group', *restart,
                            '--', '/usr/bin/python3', str(lib / script), *args], check=True, timeout=15)
        until = time.monotonic() + 20
        while not all((root / 'sides' / s / 'session.json').exists() for s in owners):
            if time.monotonic() >= until:
                raise RuntimeError('side session metadata missing')
            time.sleep(.1)
        for token, entry in tx['native_extra_controllers'].items():
            instance = 'controller-' + token
            start(entry['guard_unit'], 'native-slot-guard.py', ['--interface', entry['interface']], SERVICE_RESTART)
            start(entry['unit'], 'native-controller-relay.py', ['--root', str(root), '--instance', instance],
                  RELAY_RESTART)
            planpath = lib / ('native-' + instance + '-plan.json')
            until = time.monotonic() + 8
            while True:
                plan = json.loads(planpath.read_text())
                if plan.get('relay') and Path(plan['relay']['path']).exists():
                    break
                if time.monotonic() >= until:
                    raise RuntimeError('native relay did not publish endpoint')
                time.sleep(.05)
            entry['plan'] = plan
            save()
            native.grant(plan)
        start(tx['native_output_unit'], 'native-output-watch.py', ['--root', str(root)], SERVICE_RESTART)
        if builtin_deck:
            move_deck_capture_socket(root / 'runtime')
        start(tx['native_capture_router_unit'], 'native-capture-router.py', ['--root', str(root)], SERVICE_RESTART)
        if builtin_deck:
            # The Deck's own controls are Main's routed controller; every enrolled controller has to be listed.
            wanted = [name for _, name, _ in load('ownership', lib / 'native-ownership.py').device_specs(tx)]
            routing_ready(root, expected=wanted)

        def load_enroll():
            tool = tools / 'enroll-native-controller.py'
            shared.trusted(tool)
            return load('enroll', tool).enroll_locked
        # The other slots of the receiver(s) start Unassigned. enroll_locked writes transaction.json itself,
        # so the copy read below is the one that holds them.
        # With no slot given (and no Deck), every slot of every receiver that is plugged in joins. No receiver is fine.
        unassigned = enroll_unassigned_slots(root, load_enroll, main_hid, guest_hid, lambda: routing_ready(root),
                                             all_receivers=not slots and not builtin_deck)
        audio.run(root, 'start')
        # Audio socket startup writes its endpoint into the same transaction.
        tx = json.loads((root / 'transaction.json').read_text())
        tx['presentation'] = trial.present_window(tx['main_user'], set())
        save()
        print(json.dumps(dict(started=True, root=str(root), hardware_acceptance=False,
                              unassigned_slots=unassigned)))
        return dict(unassigned=unassigned)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--main-hid', type=Path)
    p.add_argument('--guest-hid', type=Path)
    p.add_argument('--builtin-deck', action='store_true', help="Main's controls are the Steam Deck's own")
    a = p.parse_args()
    run(a.root, a.main_hid, a.guest_hid, a.builtin_deck)
