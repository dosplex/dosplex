#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Update a retained trial after the user has saved both activities.

Does not restore the host or touch account data. Keeps old binaries and side
control metadata inside the trial root for eventual owned-tree cleanup.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

# The compositor's runtime policy is chosen by its flags, never by its backend. The product runs it as a
# nested X11 client with real side sessions (-s), host keyboards counted as menu controls (--host-keyboards) and
# machine power (--allow-power): Main's Sleep, Restart and Shut down run bundle/libexec/dosplex-power-action,
# which closes both sides and stops DosPlex before a restart or shutdown. Their confirmations say what the
# machine comes back as (--boots-into): a trial, and `dosplex start`, which runs this file, leave normal SteamOS
# behind; nothing starts DosPlex at boot. The display is never blanked (no --idle-policy).
# root-session-trial.py repeats this list (it is staged separately); a test keeps the two equal.
COMPOSITOR_FLAGS = ('-b', 'x11', '-s', '--host-keyboards', '--allow-power', '--boots-into=steamos')
# Engineering only: a session started with test hooks (the transaction's test_hooks) keeps them across an
# update: the DEV_FRAME and CHORD hooks and the stand-in for systemctl in control/power-command.
TEST_HOOK_FLAG = '--test-hooks'


def compositor_flags(tx):
    """The compositor's flags for this session: the product's, and the test hooks when the session asked for them."""
    flags = list(COMPOSITOR_FLAGS)
    if tx.get('test_hooks'): flags.insert(flags.index('-s') + 1, TEST_HOOK_FLAG)
    return tuple(flags)


# The layouts the compositor starts with; root-session-trial.py repeats this list (a test keeps the two equal).
LAYOUTS = ('single', 'vertical', 'horizontal')
# A relay that fails is started again a second later, and has no start limit (root-session-trial.py repeats this;
# a test keeps them equal).
RELAY_RESTART = ('--property=Restart=on-failure', '--property=RestartSec=1', '--property=StartLimitIntervalSec=0')
# The capture router, the output watcher and the slot guards start again after a failure (one uncaught exception would
# otherwise end routing, the menu chord or pad delivery until DosPlex is started again). Unlike the relays they have a start
# limit: one that fails ten times in two minutes stays failed, where the journal shows it, and does not spin.
SERVICE_RESTART=('--property=Restart=on-failure','--property=RestartSec=2','--property=StartLimitIntervalSec=120','--property=StartLimitBurst=10')

# What an update replaces: the staged file's name -> its place in the trial. Every file is read before
# anything stops, so a staging directory that lacks one fails without touching the trial.
# dosplex-power-action is here because COMPOSITOR_FLAGS turns on --allow-power: a trial bundled before
# the helper existed would otherwise get a compositor that refuses Sleep, Restart and Shut down.
# dosplex-sleep-guard.py is the program of the guard that start_compositor starts again after every restart of
# the compositor (only in a trial that journaled the guard when it started: sleep_guard_unit in transaction.json).
RETAINED_FILES = {
    'dosplex-desktop-environment.py': 'bundle/libexec/dosplex-desktop-environment.py',
    'session-control-proxy.py': 'bundle/libexec/session-control-proxy.py',
    'device-view-session.py': 'bundle/libexec/dosplex-device-view-session.py',
    'dosplex-side-supervisor.py': 'bundle/bin/dosplex-side-supervisor.py',
    'dosplex-side-loopback.py': 'bundle/bin/dosplex-side-loopback.py',
    'dosplex-game-store.py': 'bundle/bin/dosplex-game-store.py',
    'dosplex-game-store-tidy.py': 'bundle/bin/dosplex-game-store-tidy.py',
    'dosplex-depot.py': 'bundle/bin/dosplex-depot.py',
    'dosplex-compositor': 'bundle/bin/dosplex-compositor',
    'dosplex-user-side-session.sh': 'bundle/libexec/dosplex-user-side-session.sh',
    'dosplex-side-display-follower.py': 'bundle/libexec/dosplex-side-display-follower.py',
    'dosplex-session-manager.py': 'bundle/libexec/dosplex-session-manager.py',
    'dosplex-power-action': 'bundle/libexec/dosplex-power-action',
    'dosplex-sleep-guard.py': 'bundle/libexec/dosplex-sleep-guard.py',
    'native-output-delivery.py': 'native-output-delivery.py',
    'native-output-watch.py': 'native-output-watch.py',
}


def sleep_guard_command(root, transaction):
    """(unit, systemd-run argv) of the sleep guard. root-session-trial.py has the same function (it is staged
    separately); a test keeps the two equal."""
    unit='dosplex-sleep-guard-'+transaction['id']+'.service'
    return unit,['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,
        '--property=BindsTo='+transaction['unit'],'--property=KillMode=control-group','--property=TimeoutStopSec=20s',
        '--property=Restart=on-failure','--property=RestartSec=2','--property=SyslogIdentifier=dosplex-sleep-guard',
        '--','/usr/bin/python3',str(root/'bundle/libexec/dosplex-sleep-guard.py')]


def recovery_hook_properties(transaction):
    """Return the OnFailure/OnSuccess properties that point the compositor unit at the trial's recovery hook.
    A transaction without recovery_hook (started by an older tool) gets none. A hook name that does not match
    the expected pattern for this transaction's id is a journal error, raised before anything starts."""
    hook = transaction.get('recovery_hook')
    if hook is None:
        return []
    if hook != 'dosplex-trial-recovery-' + transaction['id'] + '.service':
        raise RuntimeError('unexpected recovery hook unit: ' + hook)
    return ['--property=OnFailure=' + hook, '--property=OnSuccess=' + hook]

def start_compositor(root, tx, *, session_script=None, seconds=None):
    """Start the compositor with the trial's policy: its layout, hook environment, power helper and the same
    flags root-session-trial.py launches it with. A retained trial's journal carries a sleep guard unit: when it
    does, start that guard too (bound to the compositor), because the guard dies with the compositor and an
    update is exactly that. A journal without it (a trial started before the guard existed) starts no guard.
    A journal with a recovery hook re-arms OnFailure/OnSuccess so a crash after this start still recovers."""
    runtime = root / 'runtime'
    layout = tx.get('initial_layout', 'vertical')
    if layout not in LAYOUTS:
        raise RuntimeError('the trial journal names an unknown layout: %r' % (layout,))
    hook_props = recovery_hook_properties(tx)
    env = {'PATH':'/usr/bin:/bin', 'HOME':str(root/'home'), 'DISPLAY':':0',
        'XAUTHORITY':str(Path(pwd.getpwnam(tx['main_user']).pw_dir)/'.Xauthority'),
        'XDG_RUNTIME_DIR':str(runtime), 'LD_LIBRARY_PATH':str(root/'bundle/lib'),
        'DOSPLEX_CONTROLLER_DIRECTORY':'/var/lib/dosplex/controllers',
        'DOSPLEX_ROOT':str(root/'bundle'), 'DOSPLEX_SIDE_ROOT':str(root/'sides'),
        'DOSPLEX_COMPOSITOR_UNIT':tx['unit'], 'DOSPLEX_RUNTIME_DIR':str(runtime),
        'DOSPLEX_WAYLAND_DISPLAY':'dosplex-0', 'DOSPLEX_STATE_PATH':str(runtime/'state.json'),
        'DOSPLEX_LEFT_USER':tx['main_user'], 'DOSPLEX_RIGHT_USER':tx['guest_user'],
        'DOSPLEX_SESSION_PAYLOAD':'steam', 'DOSPLEX_INITIAL_LAYOUT':layout,
        'DOSPLEX_AUTH_HELPER':str(root/'bundle/libexec/dosplex-side-auth.py'),
        'DOSPLEX_SESSION_SCRIPT':str(root/'bundle/bin/dosplex-side-supervisor.sh'),
        'DOSPLEX_REAL_SESSION_SCRIPT':str(root/'bundle/libexec'/('dosplex-device-view-session.py'
            if tx.get('device_view_experiment') else 'dosplex-user-side-session.sh'))}
    if tx.get('test_hooks'):
        # A root-owned program put here later runs instead of systemctl (see dosplex-power-action).
        env['DOSPLEX_POWER_COMMAND'] = str(root/'control/power-command')
    if layout == 'single':
        env['DOSPLEX_GUEST_PRELOGIN'] = '1'      # Guest's side starts asleep, so that its session file exists
    if tx.get('native_relay_unit') or tx.get('native_capture_router_unit'):
        env['DOSPLEX_NATIVE_CAPTURE_SOCKET'] = str(runtime/'native-capture.sock')
    if tx.get('audio_trial_unit'):
        env.update(DOSPLEX_AUDIO_ROUTER=str(root/'bundle/libexec/dosplex-audio-router.py'),
                   DOSPLEX_AUDIO_STATE=str(root/'audio-routes.json'),
                   DOSPLEX_AUDIO_SERVER_USER=tx['main_user'])
    if session_script is not None:
        env['DOSPLEX_REAL_SESSION_SCRIPT'] = str(session_script)
    # The shared game store (ADR-023), when the trial has one (root-session-trial.py start --game-store, or
    # --game-store here): the supervisors mount and list each account's library.
    if tx.get('game_store'):
        env['DOSPLEX_GAME_STORE'] = tx['game_store']
    subprocess.run(['systemd-run','--quiet','--collect','--service-type=exec','--expand-environment=no',
        '--unit='+tx['unit'],'--property=RuntimeMaxSec='+str(seconds or 'infinity'),'--property=KillMode=control-group',
        '--property=TimeoutStopSec=15s',*hook_props,'--','/usr/bin/env','-i',*[k+'='+v for k,v in env.items()],
        str(root/'bundle/bin/dosplex-compositor'),*compositor_flags(tx)],check=True,timeout=15)
    # The proxy is bound to this compositor, so start it immediately after the
    # parent becomes active. Side wrappers wait for a listening socket before
    # Steam starts; controller enrollment must not delay container audio.
    if tx.get('audio_socket_unit'):
        subprocess.run(['/usr/bin/python3', str(root/'retained-audio-trial.py'),
                        '--root', str(root), 'socket'], check=True, timeout=15)
    # The sleep guard is bound to the compositor too, so every restart of the compositor needs it started again.
    if tx.get('sleep_guard_unit'):
        unit, command = sleep_guard_command(root, tx)
        if tx['sleep_guard_unit'] != unit:
            raise RuntimeError('unexpected sleep guard unit')
        try:
            subprocess.run(command, check=True, timeout=15, capture_output=True, text=True)
        except (subprocess.SubprocessError, OSError) as error:
            # Sleep goes unguarded, as it did before, and `dosplex status` says so; the compositor is up already.
            print('could not start the sleep guard ' + unit + ': ' + ((getattr(error, 'stderr', None) or '').strip() or str(error)),
                  flush=True)



def main(root, staging, update_native=False, game_store=None):
    if os.geteuid() != 0 or root.parent != Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}', root.name):
        raise RuntimeError('requires exact root trial')
    txpath = root / 'transaction.json'
    meta = txpath.lstat()
    if meta.st_uid or meta.st_mode & 0o022 or not stat.S_ISREG(meta.st_mode):
        raise RuntimeError('untrusted transaction')
    tx = json.loads(txpath.read_text())
    if tx.get('keep_session') is not True or tx.get('phase') != 'started':
        raise RuntimeError('requires active retained trial')
    if tx['unit'] != 'dosplex-compositor-' + tx['id'] + '.service':
        raise RuntimeError('unexpected compositor unit')
    changes = dict(RETAINED_FILES)
    if game_store is not None:
        if not Path(game_store).is_absolute() or '..' in Path(game_store).parts:
            raise RuntimeError('the game store must be an absolute path')
        tx['game_store'] = str(game_store)
    if update_native:
        if tx.get('native_relay_unit') != 'dosplex-native-relay-' + tx['id'] + '.service':
            raise RuntimeError('unexpected native relay unit')
        for name in ('native-deck-relay.py', 'native-deck-gate.py', 'dosplex-controller-menu.py'):
            changes[name] = 'bundle/libexec/' + name
    payloads = {target: (staging / source).read_bytes() for source, target in changes.items()}
    timestamp = str(time.time_ns())
    archive = root / ('update-' + timestamp)
    archive.mkdir(mode=0o700)
    for target in payloads:
        original = root / target
        if original.exists():
            backup = archive / target
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, backup)
    def save():
        temp = root / '.transaction-update'
        temp.write_text(json.dumps(tx, indent=2) + '\n'); temp.chmod(0o644)
        os.replace(temp, txpath)
    tx.setdefault('retained_updates', []).append({'archive': archive.name, 'status': 'stopping',
        'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}})
    save()
    units = []
    pids = []
    for side in ('left', 'right'):
        path = root / 'sides' / side / 'session.json'
        if not path.exists(): continue
        session = json.loads(path.read_text())
        unit = session.get('unit', '')
        if unit:
            if not re.fullmatch('dosplex-session-' + side + r'-[a-f0-9]{32}\.service', unit):
                raise RuntimeError('unexpected child unit')
            units.append(unit)
        pids.append(session.get('pid', 0))
    subprocess.run(['systemctl', 'stop', tx['unit']], check=False, timeout=45)
    for unit in units:
        subprocess.run(['systemctl', 'stop', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
            raise RuntimeError('child still active')
    for pid in pids:
        if pid and Path('/proc', str(pid)).exists(): raise RuntimeError('supervisor still alive')
    # Preserve trusted stale lifecycle records; profiles live outside this tree.
    if (root / 'sides').exists():
        (root / 'sides').rename(archive / 'sides')
    for target, data in payloads.items():
        temp = (root / target).with_suffix('.update')
        temp.write_bytes(data); temp.chmod(0o755); os.replace(temp, root / target)
    tx['retained_updates'][-1]['status'] = 'installed'
    save()
    if update_native:
        # Existing Steam processes are stopped before replacing their HID endpoint.
        subprocess.run(['systemctl', 'stop', tx['native_relay_unit']], check=True, timeout=15)
        socket_path = root/'runtime/native-capture.sock'
        if socket_path.exists():
            metadata = socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid:
                raise RuntimeError('unexpected native capture socket')
            socket_path.unlink()
        planpath = root/'bundle/libexec/native-deck-plan.json'
        plan = json.loads(planpath.read_text()); plan.pop('relay', None)
        planpath.write_text(json.dumps(plan))
        subprocess.run(['systemd-run','--quiet','--collect','--service-type=exec',
            '--unit='+tx['native_relay_unit'],'--property=RuntimeMaxSec=infinity',
            '--property=TimeoutStopSec=5s','--property=KillMode=control-group',*RELAY_RESTART,
            '--','/usr/bin/python3',str(root/'bundle/libexec/native-deck-relay.py'),
            '--root',str(root)],check=True,timeout=15)
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            plan = json.loads(planpath.read_text())
            if plan.get('relay'): break
            time.sleep(.05)
        else: raise RuntimeError('native relay did not publish new endpoint')
        tx['native_deck_plan'] = plan
        save()
    start_compositor(root, tx)
    spec = importlib.util.spec_from_file_location('trial', root / 'root-session-trial.py')
    trial = importlib.util.module_from_spec(spec); spec.loader.exec_module(trial)
    tx['presentation'] = trial.present_window(tx['main_user'], set())
    watcher = tx.get('native_output_unit')
    if watcher:
        if watcher != 'dosplex-native-output-' + tx['id'] + '.service':
            raise RuntimeError('unexpected output watcher')
        subprocess.run(['systemd-run','--quiet','--collect','--service-type=exec','--unit='+watcher,
            '--property=BindsTo='+tx['unit'],'--property=KillMode=control-group',*SERVICE_RESTART,
            '--','/usr/bin/python3',str(root/'native-output-watch.py'),'--root',str(root)],check=True,timeout=15)
    if tx.get('controller_broker_unit'):
        subprocess.run(['/usr/bin/python3',str(root/'controller-tools/start-controller-trial.py'),
            '--root',str(root),'--staging',str(root/'controller-tools')],check=True,timeout=20)
    if tx.get('controller_broker_unit'):
        tx['controller_tools_sha256'] = json.loads(txpath.read_text())['controller_tools_sha256']
    tx['retained_updates'][-1]['status'] = 'started'
    save()
    print(json.dumps({'updated': True, 'root': str(root), 'archive': archive.name}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--staging', type=Path, required=True)
    p.add_argument('--sessions-saved', action='store_true', required=True)
    p.add_argument("--update-native-relay", action="store_true")
    p.add_argument('--game-store', type=Path, help='turn on the shared game store at this place (ADR-023)')
    a = p.parse_args(); main(a.root, a.staging, a.update_native_relay, a.game_store)
