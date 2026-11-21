#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Bounded development-only two-user session trial, with independent recovery.

Requires explicit root execution. Never installs services, sudoers, accounts or
changes the production compositor mask. Recovery archives a small result in the
main user's home and removes only this trial's /run tree after Steam is restored.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

# The compositor's runtime policy is chosen by its flags, never by its backend. The product runs it as a
# nested X11 client with real side sessions (-s), host keyboards counted as menu controls (--host-keyboards) and
# machine power (--allow-power): Main's Sleep, Restart and Shut down run bundle/libexec/dosplex-power-action,
# which closes both sides and stops Dosplex before a restart or shutdown. Their confirmations say what the
# machine comes back as (--boots-into): a trial, and `dosplex start`, which runs this file, leave normal SteamOS
# behind; nothing starts Dosplex at boot. The display is never blanked (no --idle-policy).
# update-retained-session.py repeats this list (it is staged separately); a test keeps the two equal.
COMPOSITOR_FLAGS = ('-b', 'x11', '-s', '--host-keyboards', '--allow-power', '--boots-into=steamos')
# Engineering only (start(test_hooks=True), kept in the transaction): the DEV_FRAME and CHORD hooks the
# harnesses use, the stand-in for systemctl in control/power-command, and the relays' synthetic controls.
# A release starts without them (TRL-09); `dosplex start` turns them on only where root has put the
# engineering marker (see docs/testing.md).
TEST_HOOK_FLAG = '--test-hooks'


def compositor_flags(transaction):
    """The compositor's flags for this session: the product's, and the test hooks when the session asked for them."""
    flags = list(COMPOSITOR_FLAGS)
    if transaction.get('test_hooks'): flags.insert(flags.index('-s') + 1, TEST_HOOK_FLAG)
    return tuple(flags)


def compositor_test_environment(root, transaction):
    """What a session with test hooks adds to the compositor's environment; nothing without them."""
    if not transaction.get('test_hooks'): return {}
    # A root-owned program put here later runs instead of systemctl (see dosplex-power-action).
    return {'DOSPLEX_POWER_COMMAND': str(Path(root) / 'control/power-command')}


# A relay that fails (its controller went away in a suspend, say) is started again a second later, and then follows
# its slot to the node it has now. It has no start limit: with systemd's default (five starts in ten seconds) a relay
# that fails at once would stay dead. The launchers of the other relays repeat this policy; a test keeps them equal.
RELAY_RESTART = ('--property=Restart=on-failure', '--property=RestartSec=1', '--property=StartLimitIntervalSec=0')


# The layouts the compositor starts with; update-retained-session.py repeats this list (a test keeps the two equal).
LAYOUTS = ('single', 'vertical', 'horizontal')
# How long stopping the stock Steam service may take. The unit stops with TimeoutStopSec=1min and KillMode=mixed:
# a Steam that does not exit on SIGTERM gets the final SIGTERM after 60 s and SIGKILL after that, so the worst
# case is about 90 s; 150 keeps our wait past the worst case, with margin.
STEAM_STOP_SECONDS = 150
# How long recovery waits for the stock Steam to show its home screen. Steam may update itself first, and the stock
# Gamescope answers nothing while it comes up; 45 seconds was too short for that (TRL-07).
STEAM_RETURN_SECONDS = 180
# How long a start waits for the stock session to be up before it says to try again.
STEAM_READY_SECONDS = 60
# One look at the stock Gamescope: a short wait per try and a few tries. The stock Gamescope does not answer while the
# stock session goes down or comes back, and a 30 second wait that raised made the stop and the start fail.
IDLE_PROBE_SECONDS = 5
IDLE_PROBE_TRIES = 3
IDLE_PROBE_PAUSE = .5
# Where systemd reads units that live only until the next boot (what systemd-run writes its transient units to).
RUNTIME_UNIT_DIR = Path('/run/systemd/system')


def run(args, *, check=True, timeout=30, input=None):
    return subprocess.run([str(a) for a in args], check=check, timeout=timeout,
                          input=input, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def as_user(user, *args):
    account = pwd.getpwnam(user)
    return ['runuser', '-u', user, '--', '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin',
            'HOME='+account.pw_dir, 'DISPLAY=:0', 'XDG_RUNTIME_DIR=/run/user/'+str(account.pw_uid),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(account.pw_uid)+'/bus', *args]


def idle_steam(user):
    """True when the stock Steam shows its home screen and nothing else. A stock Gamescope that does not answer (xprop
    hangs or fails: the session is going down or coming up) is not idle: this looks a few times, briefly, then says no."""
    for attempt in range(IDLE_PROBE_TRIES):
        try:
            text = run(as_user(user, 'xprop', '-display', ':0', '-root',
                               'GAMESCOPE_FOCUSED_APP', 'GAMESCOPE_FOCUSABLE_APPS'), check=False, timeout=IDLE_PROBE_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            text = None
        if text is not None and text.returncode == 0:
            lines = text.stdout.splitlines()
            return len(lines) == 2 and all('=' in line and
                re.findall(r'\d+', line.split('=',1)[1]) == ['769'] for line in lines)
        if attempt+1 < IDLE_PROBE_TRIES:time.sleep(IDLE_PROBE_PAUSE)
    return False


def wait_idle_steam(user, seconds):
    """Wait up to `seconds` for idle_steam(user). Returns whether it came."""
    deadline=time.monotonic()+seconds
    while True:
        if idle_steam(user):return True
        if time.monotonic()>=deadline:return False
        time.sleep(1)


def windows(user):
    tree=run(as_user(user,'xwininfo','-display',':0','-root','-tree')).stdout
    return set(re.findall(r'^\s+(0x[0-9a-f]+) "Dosplex":',tree,re.MULTILINE))


def present_window(user, previous):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        created=windows(user)-previous
        if len(created)>1:raise RuntimeError('ambiguous trial window')
        if created:break
        time.sleep(.1)
    else:raise RuntimeError('trial window did not appear')
    window=created.pop()
    info=run(as_user(user,'xwininfo','-display',':0','-root')).stdout
    dimensions=[re.search(r'^\s*'+name+r': (\d+)$',info,re.MULTILINE) for name in ('Width','Height')]
    if not all(dimensions):raise RuntimeError('cannot determine host display size')
    width,height=(int(match.group(1)) for match in dimensions)
    if not (640<=width<=16384 and 480<=height<=16384):raise RuntimeError('unexpected host display size')
    run(as_user(user,'xdotool','windowsize',window,str(width),str(height)))
    # Host Steam was selected before handoff. Tag only our newly created window
    # with that app ID; no host root-window or saved Steam settings are changed.
    run(as_user(user,'xprop','-display',':0','-id',window,'-f','STEAM_GAME','32c','-set','STEAM_GAME','769'))
    return {'window':window,'width':width,'height':height}


def grant_x11_access(root, transaction):
    access=run(as_user(transaction['main_user'],'xhost')).stdout
    lines={line.strip() for line in access.splitlines()}
    if 'SI:localuser:root' in lines or access.startswith('access control disabled'):
        return
    if not access.startswith('access control enabled'):
        raise RuntimeError('cannot determine X11 access control state')
    # Record restoration intent before mutation, including an ambiguous failure.
    transaction['x11_root_access_added']=True
    save(root,transaction)
    run(as_user(transaction['main_user'],'xhost','+SI:localuser:root'))


def restore_trial_audio(root, transaction):
    if transaction.get('audio_trial_unit'):
        expected_audio='dosplex-audio-'+transaction['id']+'.service'
        if transaction['audio_trial_unit'] != expected_audio:
            raise ValueError('unexpected audio trial unit')
        # The bundle keeps the helper under bundle/tools; older trials copied it next to this file.
        helper=root/'retained-audio-trial.py'
        if not helper.is_file(): helper=root/'bundle/tools/retained-audio-trial.py'
        run(['/usr/bin/python3', helper, '--root', root, 'cleanup'])


def restore_x11_access(transaction):
    if not transaction.get('x11_root_access_added'):return
    user=transaction['main_user']
    run(as_user(user,'xhost','-SI:localuser:root'))
    access=run(as_user(user,'xhost')).stdout
    if 'SI:localuser:root' in {line.strip() for line in access.splitlines()}:
        raise RuntimeError('temporary X11 root access was not removed')


def save(root, value):
    temporary = root / '.transaction-new'
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), 0o644)
        json.dump(value, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, root / 'transaction.json')


def trusted_root(root):
    if root.parent != Path('/run') or not re.fullmatch(r'dosplex-trial-[a-f0-9]{32}', root.name):
        raise ValueError('unexpected trial directory')
    for path in (Path('/run'), root):
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('untrusted trial directory')


def unit_dead(unit):
    """True when the unit runs nothing any more: it is gone, or it stopped, or it failed. A unit that crashed or
    was killed ends in ActiveState failed, with no main and no control process; systemd is done with it then, as
    with one that stopped cleanly. Counting only "inactive" as dead made every recovery after a compositor crash
    refuse ("trial services not confirmed stopped"), which is the one moment the recovery is for (Deck, p74)."""
    result=run(['systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState',
                '-p','MainPID','-p','ControlPID'],check=False)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if fields.get('LoadState') == 'not-found':return True
    state=(fields.get('ActiveState'),fields.get('SubState'))
    return state in (('inactive','dead'),('failed','failed')) and fields.get('MainPID') == '0' and \
        fields.get('ControlPID') == '0'


def unit_restarts(unit):
    """How often systemd has started the unit again after a failure (NRestarts), or None when it does not say."""
    result=run(['systemctl','show',unit,'-p','NRestarts'],check=False)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    try:return int(fields['NRestarts'])
    except (KeyError,ValueError):return None


def bound_children(parent, allowed=()):
    result=run(['systemctl','show',parent,'-p','LoadState','-p','BoundBy'],check=False)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if fields.get('LoadState') == 'not-found':
        return []
    if result.returncode or 'BoundBy' not in fields or fields.get('LoadState') not in ('loaded','masked'):
        raise RuntimeError('cannot observe trial dependencies')
    children=fields['BoundBy'].split()
    if any(unit not in allowed and not re.fullmatch(r'dosplex-session-(left|right)-[a-f0-9]{32}\.service',unit) for unit in children):
        raise RuntimeError('unexpected trial dependency; preserve for inspection')
    return children


def journaled_native_units(transaction):
    names=[]
    fields={'native_relay_unit':'native-relay','native_output_unit':'native-output',
            'native_controller_unit':'native-controller','native_capture_router_unit':'native-capture',
            'native_slot_guard_unit':'native-slot-guard','controller_broker_unit':'controller-broker',
            'controller_output_unit':'controller-output','audio_trial_unit':'audio','audio_socket_unit':'audio-socket',
            'sleep_guard_unit':'sleep-guard'}
    for key,prefix in fields.items():
        if not transaction.get(key):continue
        expected='dosplex-'+prefix+'-'+transaction['id']+'.service'
        if transaction[key]!=expected:raise ValueError('unexpected journaled unit')
        names.append(expected)
    extras=transaction.get('native_extra_controllers',{})
    if not isinstance(extras,dict) or len(extras)>8:raise ValueError('invalid extra native slots')
    for token,entry in extras.items():
        if not re.fullmatch('[a-f0-9]{8}',token):raise ValueError('invalid native slot')
        for key,prefix in (('unit','dosplex-native-controller-'),('guard_unit','dosplex-native-slot-guard-')):
            expected=prefix+token+'-'+transaction['id']+'.service'
            if entry[key]!=expected:raise ValueError('unexpected extra native unit')
            names.append(expected)
    return names


SLEEP_GUARD = 'libexec/dosplex-sleep-guard.py'


def sleep_guard_command(root, transaction):
    """(unit, systemd-run argv) of the sleep guard, a root service bound to the compositor like the audio router.

    update-retained-session.py repeats this (it is staged separately); a test keeps the two equal. The guard freezes
    the sides before the machine sleeps, so that no Steam is stuck waiting for a relay the kernel froze first
    (packaging/steamos/dosplex-sleep-guard.py). Its lines in the journal carry the name dosplex-sleep-guard.
    """
    unit='dosplex-sleep-guard-'+transaction['id']+'.service'
    return unit,['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,
        '--property=BindsTo='+transaction['unit'],'--property=KillMode=control-group','--property=TimeoutStopSec=20s',
        '--property=Restart=on-failure','--property=RestartSec=2','--property=SyslogIdentifier=dosplex-sleep-guard',
        '--','/usr/bin/python3',str(root/'bundle'/SLEEP_GUARD)]


def start_sleep_guard(root, transaction):
    """Run the sleep guard. The unit is journaled before it starts: recovery stops what the journal names, and
    refuses a dependency of the compositor that it does not know. A guard that cannot start costs the trial nothing
    it had: Dosplex starts, Sleep goes unguarded as it did before, and `dosplex status` says so."""
    unit,command=sleep_guard_command(root,transaction)
    transaction['sleep_guard_unit']=unit
    save(root,transaction)
    try:run(command)
    except (subprocess.SubprocessError,OSError) as error:
        print('could not start the sleep guard '+unit+': '+((getattr(error,'stderr',None) or '').strip() or str(error)),flush=True)


ARCHIVE_SCRIPT = r"""
import json, os, pathlib, re, stat, sys, tempfile
path=pathlib.Path(sys.argv[1]); data=sys.stdin.buffer.read(65537)
if len(data)>65536: raise ValueError('result too large')
incoming=json.loads(data)
if not re.fullmatch(r'[a-f0-9]{32}',incoming['id']): raise ValueError('invalid trial identity')
staging=path.parent/('.dosplex-result-'+incoming['id'])
try: staging.mkdir(mode=0o700)
except FileExistsError: pass
info=staging.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
    raise ValueError('untrusted result staging directory')
fd, temporary=tempfile.mkstemp(prefix='part-',dir=staging)
try:
    with os.fdopen(fd,'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    try:
        os.link(temporary,path)
    except FileExistsError:
        old=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(old,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():
                raise ValueError('unexpected existing result')
            prior=json.loads(stream.read(65537))
        if (any(prior.get(key)!=incoming.get(key) for key in ('id','unit','main_user','manifest')) or
                prior.get('phase')!='restored' or prior.get('steam_foreground_only') is not True):
            raise ValueError('existing result belongs to another or incomplete trial')
finally:
    os.unlink(temporary)
# Only this trial's private publication directory is eligible for orphan cleanup.
# A killed writer may leave a partial file or an extra link to the final result.
for part in staging.iterdir():
    info=part.lstat()
    if not part.name.startswith('part-') or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():
        raise ValueError('unexpected result staging entry')
    part.unlink()
staging.rmdir()
"""


def recover(root):
    trusted_root(root)
    lock_fd=os.open(root/'recovery.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    with os.fdopen(lock_fd,'r+') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('recovery already running; retry later')
        return recover_locked(root)


def recover_locked(root):
    info=(root/'transaction.json').lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise PermissionError('untrusted trial transaction')
    transaction=json.loads((root/'transaction.json').read_text())
    expected='dosplex-compositor-'+root.name.removeprefix('dosplex-trial-')+'.service'
    if (transaction['unit'] != expected or transaction['id'] != root.name.removeprefix('dosplex-trial-') or
            transaction['recovery'] != 'dosplex-trial-recovery-'+transaction['id']):
        raise ValueError('trial unit mismatch')
    allowed=journaled_native_units(transaction)
    children=set(bound_children(transaction['unit'],allowed))
    run(['systemctl','stop',transaction['unit']],check=False)
    # A child can register after the first observation but before the parent
    # finishes stopping. Discover systemd's reverse dependencies again.
    children.update(bound_children(transaction['unit'],allowed))
    for child in sorted(children):
        run(['systemctl','stop',child],check=False)
    children.update(bound_children(transaction['unit'],allowed))
    if not all(unit_dead(unit) for unit in [transaction['unit'],*sorted(children)]):
        raise RuntimeError('trial services not confirmed stopped; refusing competing Steam client')
    children=sorted(children)
    user=transaction['main_user']
    if transaction.get('sleep_guard_unit'):
        expected='dosplex-sleep-guard-'+transaction['id']+'.service'
        if transaction['sleep_guard_unit']!=expected:raise ValueError('unexpected sleep guard unit')
        run(['systemctl','stop',expected],check=False)
        if not unit_dead(expected):raise RuntimeError('sleep guard remains active')
    for field, prefix in (('controller_broker_unit', 'dosplex-controller-broker-'),
                          ('native_capture_router_unit', 'dosplex-native-capture-'),
                          ('controller_output_unit', 'dosplex-controller-output-')):
        if transaction.get(field):
            expected=prefix+transaction['id']+'.service'
            if transaction[field]!=expected: raise ValueError('unexpected controller unit')
            run(['systemctl','stop',expected],check=False)
            if not unit_dead(expected): raise RuntimeError('controller service remains active')
    if transaction.get('controller_broker_unit'):
        spec=importlib.util.spec_from_file_location('registry',root/'controller-tools/dosplex-controller-registry.py')
        registry=importlib.util.module_from_spec(spec);spec.loader.exec_module(registry)
        registry.restore_permissions(root/'controller-preferences/recovery.json')
        registry.restore_steam_mode(root/'controller-preferences')
    if transaction.get('native_output_unit'):
        expected='dosplex-native-output-'+transaction['id']+'.service'
        if transaction['native_output_unit']!=expected:raise ValueError('unexpected output watcher unit')
        run(['systemctl','stop',expected],check=False)
        if not unit_dead(expected):raise RuntimeError('native output watcher remains active')
    if transaction.get('keep_session'):
        run(as_user(user,'systemctl','--user','stop','dosplex-retained-dev-awake.service'),check=False)
    if transaction.get('native_relay_unit'):
        expected_relay='dosplex-native-relay-'+transaction['id']+'.service'
        if transaction['native_relay_unit'] != expected_relay:
            raise ValueError('unexpected native relay unit')
        run(['systemctl','stop',expected_relay],check=False)
        if not unit_dead(expected_relay):raise RuntimeError('native relay still owns physical HID')
        relay_path = transaction.get('native_deck_plan', {}).get('relay', {}).get('path')
        if relay_path and Path(relay_path).exists():
            raise RuntimeError('native relay endpoint still exists; refusing Steam handback')
    if transaction.get('native_slot_guard_unit'):
        expected_guard='dosplex-native-slot-guard-'+transaction['id']+'.service'
        if transaction['native_slot_guard_unit'] != expected_guard:
            raise ValueError('unexpected native slot guard unit')
        run(['systemctl','stop',expected_guard],check=False)
        if not unit_dead(expected_guard):raise RuntimeError('native slot guard remains active')
    if transaction.get('native_controller_unit'):
        expected_controller='dosplex-native-controller-'+transaction['id']+'.service'
        if transaction['native_controller_unit'] != expected_controller:
            raise ValueError('unexpected native controller unit')
        run(['systemctl','stop',expected_controller],check=False)
        if not unit_dead(expected_controller):raise RuntimeError('native controller relay remains active')
        relay_path=transaction.get('native_controller_plan',{}).get('relay',{}).get('path')
        if relay_path and Path(relay_path).exists():
            raise RuntimeError('external native endpoint still exists; refusing Steam handback')
    recover_extra_native_controllers(root,transaction)
    # Routing mounts exist only in the exact side namespaces stopped above.
    # native-routing-mounts.json is evidence, not a list of host unmount targets.
    if (root/'native-hid-udev.json').exists():
        spec=importlib.util.spec_from_file_location('hid_delivery',root/'bundle/libexec/native-hid-delivery.py')
        delivery=importlib.util.module_from_spec(spec);spec.loader.exec_module(delivery)
        delivery.remove_source_rule(root)
    if (root/'native-pads.json').exists():
        # The generic pads' udev rule and the Guest D-Bus policy (no org.bluez). A file someone changed is kept and named.
        spec=importlib.util.spec_from_file_location('native_pads',root/'bundle/libexec/native-pads.py')
        pads=importlib.util.module_from_spec(spec);spec.loader.exec_module(pads)
        kept=pads.recover(root)
        if kept:print('pads files changed since Dosplex wrote them; kept for inspection: '+', '.join(kept),file=sys.stderr)
    for entry in transaction.get('native_extra_controllers',{}).values():
        native_module(root).restore(entry['plan'])
    if transaction.get('native_controller_plan'):
        # Restore the later external-controller lease first. Its uinput snapshot
        # may include the Deck lease; the original Deck snapshot restores last.
        native_module(root).restore(transaction['native_controller_plan'])
    if transaction.get('native_deck_plan'):
        native_module(root).restore(transaction['native_deck_plan'])
    if transaction.get('native_uinput_plan'):
        native_module(root).restore(transaction['native_uinput_plan'])
    if (root/'controller-mapping-recovery.json').exists():
        spec=importlib.util.spec_from_file_location('mapping_repair',root/'repair-controller-mappings.py')
        repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
        repair.restore(root)
    if (root/'guest-client-channel-experiment.json').exists():
        spec=importlib.util.spec_from_file_location('client_channel',root/'restore-client-channel.py')
        channel=importlib.util.module_from_spec(spec);spec.loader.exec_module(channel)
        channel.restore(root)
    if (root/'wine-modelist-recovery.json').exists():
        spec=importlib.util.spec_from_file_location('wine_modelist',root/'restore-wine-modelist.py')
        modelist=importlib.util.module_from_spec(spec);spec.loader.exec_module(modelist)
        modelist.restore(root)
    # Guest's loopback programs (dosplex-side-loopback.py): the supervisor removes its pins when it stops; one that
    # was killed leaves them. Every session unit is confirmed stopped above, so nothing is attached any more.
    loopback=root/'bundle/bin/dosplex-side-loopback.py'
    if loopback.is_file():
        spec=importlib.util.spec_from_file_location('side_loopback',loopback)
        pins=importlib.util.module_from_spec(spec);spec.loader.exec_module(pins)
        for side in ('left','right'):
            try: pins.unpin(Path('/sys/fs/bpf/dosplex')/side)
            except OSError as error: print('Dosplex recovery: loopback programs of '+side+' remain: '+str(error),file=sys.stderr)
        try: Path('/sys/fs/bpf/dosplex').rmdir()
        except OSError: pass
    put_game_libraries_away(root,transaction)
    restore_trial_audio(root,transaction)
    restore_x11_access(transaction)
    if transaction.get('steam_runtime_mask_added'):
        run(as_user(user,'systemctl','--user','unmask','--runtime','steam-launcher.service'))
    run(as_user(user,'systemctl','--user','start','steam-launcher.service'))
    foreground=wait_idle_steam(user,STEAM_RETURN_SECONDS)
    if not foreground:
        # Steam was started and the sides are gone, so the session is the stock one again; it only has not shown its
        # home screen yet (it may be updating itself). Failing here left the trial retained and blocked the next start
        # (TRL-07). Carry on, and do not claim the home screen was seen.
        if run(as_user(user,'systemctl','--user','is-active','steam-launcher.service'),check=False).returncode:
            raise RuntimeError('normal Steam did not start again; trial evidence retained. Try again in a minute')
        print('Dosplex recovery: stock Steam was started but did not show its home screen within '
              +str(STEAM_RETURN_SECONDS)+' seconds; it is probably still coming up.',file=sys.stderr,flush=True)
    transaction.update(phase='restored', steam_foreground_only=foreground, stopped_children=children)
    save(root,transaction)
    # Write as the desktop UID, never root into a user-controlled home path.
    archive='dosplex-session-trial-'+transaction['id']+'-result.json'
    run(as_user(user,'python3','-c',ARCHIVE_SCRIPT,str(Path(pwd.getpwnam(user).pw_dir)/archive)),
        input=json.dumps(transaction,indent=2)+'\n')
    # Stop only the timer, never kill a recovery service executing this code.
    run(['systemctl','stop',transaction['recovery']+'.timer'],check=False)
    disarm_recovery_hook(transaction)
    shutil.rmtree(root)
    return transaction


# A retained session (keep_session, what `dosplex start` runs) has no watcher and no timer: nothing brought stock Steam
# back when the compositor ended on its own, and the screen stayed black (TRL-03). The compositor unit now names a
# recovery unit in OnFailure= and in OnSuccess=. Both, because a compositor that exits with status 0 without anybody
# asking (its display went away, say) is as dead to the player as one that crashed; OnFailure= alone does not see it.
# A stop that recovery or `dosplex stop` asks for also ends in OnSuccess=, so the unit first looks whether anybody is
# already recovering (the lock) or the trial is gone (its condition), and then does nothing. The unit is a file under
# /run/systemd/system, the place systemd-run writes its units to, because OnFailure= needs the named unit to exist
# when the compositor starts and systemd-run would start it at once. Recovery removes it with the rest of the trial.
def recovery_hook_name(transaction):
    return transaction['recovery']+'.service'


def recovery_hook_text(root, transaction):
    return (
        '[Unit]\nDescription=Dosplex: bring stock Steam back after the compositor ended\n'
        'ConditionPathExists='+str(root/'transaction.json')+'\n\n'
        '[Service]\nType=oneshot\nSyslogIdentifier=dosplex-recovery\n'
        'ExecStart=/usr/bin/python3 '+str(root/'root-session-trial.py')+' ended --root '+str(root)+'\n'
        # Like the recovery of a trial with a timer: a recovery that did not finish is tried again.
        'Restart=on-failure\nRestartSec=5s\nTimeoutStartSec=600\n')


def arm_recovery_hook(root, transaction, directory=None):
    """Write the recovery unit the compositor names. Journaled first: recovery removes what the journal says."""
    directory=directory or RUNTIME_UNIT_DIR
    transaction['recovery_hook']=recovery_hook_name(transaction)
    save(root,transaction)
    (directory/transaction['recovery_hook']).write_text(recovery_hook_text(root,transaction))
    run(['systemctl','daemon-reload'])


def disarm_recovery_hook(transaction, directory=None):
    """Remove the recovery unit. It never stops a recovery: a failure here is said and recovery goes on."""
    name=transaction.get('recovery_hook')
    if not name:return
    if name!='dosplex-trial-recovery-'+transaction['id']+'.service':raise ValueError('unexpected recovery unit')
    try:
        (( directory or RUNTIME_UNIT_DIR)/name).unlink(missing_ok=True)
        run(['systemctl','daemon-reload'],check=False)
    except (OSError,subprocess.SubprocessError) as error:
        print('Dosplex recovery: could not remove the unit '+name+': '+str(error),file=sys.stderr)


def unit_running(unit):
    """True while the unit runs or is starting or stopping; a failed or inactive one (or none) is not running."""
    result=run(['systemctl','show',unit,'-p','ActiveState'],check=False)
    fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    return fields.get('ActiveState') in ('active','activating','reloading','deactivating')


def ended(root):
    """What the compositor's OnFailure= and OnSuccess= run: bring stock Steam back if the compositor is gone."""
    trusted_root(root)
    try:transaction=json.loads((root/'transaction.json').read_text())
    except FileNotFoundError:return {'phase':'already-restored'}
    if unit_running(transaction['unit']):return {'phase':'compositor-running'}
    try:return recover(root)
    except FileNotFoundError:
        if not root.exists():return {'phase':'already-restored'}
        raise
    except RuntimeError as error:
        # Somebody else is recovering (dosplex stop, the recovery of a stop, the timer): they bring Steam back.
        if 'recovery already running' in str(error):return {'phase':'recovering-elsewhere'}
        raise


def stage_and_arm(root, files, transaction):
    root.mkdir(mode=0o755)
    trusted_root(root)
    attempted_arm = False
    try:
        for name, (data, mode) in files.items():
            target = root/'bundle'/name
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
        for name in ('root-session-trial.py', 'session-bundle-integrity.py'):
            target = root/name
            target.write_bytes(Path(__file__).with_name(name).read_bytes())
            target.chmod(0o644)
        (root/'runtime').mkdir(mode=0o700)
        (root/'home').mkdir(mode=0o700)
        save(root, transaction)
        if transaction.get('keep_session'):
            # Explicit development ownership: keep the restoration ledger but
            # leave recovery to the operator across repeated test iterations.
            return
        attempted_arm = True
        run(['systemd-run','--quiet','--collect','--service-type=exec',
             '--property=Restart=on-failure','--property=RestartSec=5s',
             '--property=ConditionPathExists='+str(root/'transaction.json'),
             '--unit='+transaction['recovery'],
             '--on-active='+str(transaction['seconds']+15)+'s','--timer-property=AccuracySec=1s',
             '/usr/bin/python3',root/'root-session-trial.py','recover','--root',root])
        if run(['systemctl','is-active',transaction['recovery']+'.timer'],check=False).returncode:
            raise RuntimeError('independent recovery timer did not arm')
    except BaseException:
        if attempted_arm:
            units = [transaction['recovery']+suffix for suffix in ('.timer','.service')]
            run(['systemctl','stop',*units],check=False)
            if not all(unit_dead(unit) for unit in units):
                raise RuntimeError('staging failed; recovery units not confirmed stopped; retained '+str(root))
        shutil.rmtree(root)
        raise


# The relay's unit is started again by systemd when the relay fails (RELAY_RESTART), for example after a suspend that
# made its controller come back as another node. The watcher ends the trial for a relay only when the relay stays dead
# for this long: a unit that is between its failure and its restart is not dead, and one that was missed for a
# moment must not cost the trial.
RELAY_DEAD_SECONDS=15
# RELAY_RESTART has no start limit, so a relay that fails at once on every start is never dead: its unit goes round
# between running and auto-restart for ever, and systemd only counts the restarts (NRestarts). The watcher takes a
# relay that was started again RELAY_LOOP_RESTARTS times within RELAY_LOOP_SECONDS for dead while that goes on, and the
# grace above applies as it does to a dead unit. One restart after a suspend, or a few before a controller is back, make
# no loop. The relay's status file cannot tell a loop from a wait, and the watcher does not use it: a relay publishes
# nothing for up to half a minute (native-deck-plan.py SLOT_WAIT_SECONDS) while it waits for the new node of its
# controller. If the node does not come, it fails and is started again about every 31 s: too rarely to make a loop, and
# the trial runs to its deadline as it always did.
RELAY_LOOP_RESTARTS=3
RELAY_LOOP_SECONDS=10


class RestartLoop:
    """Tells whether a unit that systemd keeps starting again is in a crash loop (see RELAY_LOOP_RESTARTS)."""

    def __init__(self):
        self.count=None;self.times=[]

    def looping(self,count,now):
        """`count` is the unit's NRestarts (None: systemd does not say) and `now` the monotonic time of this look.
        True while the unit was started again RELAY_LOOP_RESTARTS times within the last RELAY_LOOP_SECONDS."""
        if count is None:return False
        # The restarts from before the first look are not counted. A count that went down means the unit was started
        # by hand: it counts from zero again.
        if self.count is not None and count>self.count:self.times+=[now]*(count-self.count)
        self.count=count
        self.times=[t for t in self.times if now-t<RELAY_LOOP_SECONDS]
        return len(self.times)>=RELAY_LOOP_RESTARTS


def watch(root):
    """Restore on early compositor exit, a relay that stays dead or keeps failing, or an owner-created stop marker.

    The independent deadline timer remains the fallback if this observer dies. A systemctl that is too slow to answer
    (the machine stalled, a suspend did not complete) is no answer: the watcher asks again a second later.
    """
    trusted_root(root)
    transaction=json.loads((root/'transaction.json').read_text())
    if transaction.get('keep_session') is True:
        return {'phase':'retained', 'root':str(root)}
    deadline=time.monotonic()+transaction['seconds']+15
    relay_dead_since=None
    loop=RestartLoop()
    told=False
    while root.exists():
        requested=False
        try:
            marker=(root/'control/stop').lstat()
            requested=stat.S_ISREG(marker.st_mode) and marker.st_uid==pwd.getpwnam(transaction['main_user']).pw_uid
        except FileNotFoundError:
            pass
        relay_failed=compositor_dead=False
        try:
            relay_state=None
            relay=transaction.get('native_relay_unit')
            if relay:
                if unit_dead(relay):relay_state='dead'
                elif loop.looping(unit_restarts(relay),time.monotonic()):relay_state='started again and again'
            compositor_dead=unit_dead(transaction['unit'])
            if relay_state:
                if relay_dead_since is None:relay_dead_since=time.monotonic()
                relay_failed=time.monotonic()-relay_dead_since>=RELAY_DEAD_SECONDS
            else:relay_dead_since=None
        except subprocess.TimeoutExpired:
            pass
        if relay_failed and not told:
            told=True;print('the native relay unit '+relay+' is '+relay_state+'; ending the trial',flush=True)
        if requested or relay_failed or compositor_dead or time.monotonic()>=deadline:
            try:
                return recover(root)
            except FileNotFoundError:
                if not root.exists():return {'phase':'already-restored'}
                raise
            except RuntimeError:
                # A concurrent recovery owns the lock, or restoration needs a
                # retry. Keep the independent timer armed in either case.
                if not root.exists():return {'phase':'already-restored'}
        time.sleep(1)
    return {'phase':'already-restored'}


def recover_extra_native_controllers(root,transaction):
    extras=transaction.get('native_extra_controllers',{})
    if not isinstance(extras,dict) or len(extras)>8:raise ValueError('invalid extra native slots')
    for token,entry in extras.items():
        if not re.fullmatch('[a-f0-9]{8}',token):raise ValueError('invalid native slot')
        for key,prefix in (('unit','dosplex-native-controller-'),('guard_unit','dosplex-native-slot-guard-')):
            expected=prefix+token+'-'+transaction['id']+'.service'
            if entry[key]!=expected:raise ValueError('unexpected extra native unit')
            run(['systemctl','stop',expected],check=False)
            if not unit_dead(expected):raise RuntimeError('extra native slot remains active')
        plan=entry['plan'];relay_id=plan['relay_id']
        if not re.fullmatch('[a-f0-9]{32}',relay_id):raise ValueError('invalid extra native relay')
        # Derive this path even if startup failed before publishing its inode.
        if Path('/dev/dosplex-hid-'+relay_id).exists():
            raise RuntimeError('extra native endpoint remains; refusing Steam handback')


def native_module(root):
    spec = importlib.util.spec_from_file_location('native_deck_plan', root/'bundle/libexec/native-deck-plan.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def game_store_module(root):
    spec=importlib.util.spec_from_file_location('game_store',root/'bundle/bin/dosplex-game-store.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def game_store_slot(root,transaction):
    """The shared game store's slot (ADR-023): with a permanent store, no Steam of its accounts runs now, so commit what
    was shared and mount every library again. Returns True when the store is permanent (whether the slot ran, reported
    failures or could not run at all), else False. Never stops a start or a recovery."""
    store=transaction.get('game_store')
    if not store:return False
    try:
        module=game_store_module(root);layout=module.Layout(store)
        if not module.permanent(layout):return False
    except Exception as error:
        print('Dosplex: shared game store slot failed: '+str(error),file=sys.stderr);return False
    try:report=module.slot(layout)
    except Exception as error:
        # A permanent store that could not have its slot now (a Steam of an account still runs: a start that failed
        # while it stopped the stock Steam) keeps its libraries mounted and listed as they are. Returning False here
        # had the recovery put them away: Main's library stayed on its Steam list and was unmounted, and every
        # shared game looked uninstalled (seen on the Steam Machine, p60).
        print('Dosplex: shared game store slot failed, the libraries stay as they are: '+str(error),file=sys.stderr);return True
    try:print('Dosplex: shared game store: '+json.dumps(report),file=sys.stderr)
    except Exception as error:print('Dosplex: shared game store: slot ran, report not printable: '+str(error),file=sys.stderr)
    # The slot finished leaving: there is no store any more, so the sides must not be told about one.
    if isinstance(report,dict) and report.get('left') is True:transaction.pop('game_store',None)
    return True


def put_game_libraries_away(root,transaction):
    """The shared game store (ADR-023): unmount each account's overlay and stash its manifests of the shared games.

    A permanent store (config in mode on) is not put away: its libraries stay mounted and listed across recovery, and
    recovery is a slot. Otherwise: the supervisors do this when they stop; one that was killed leaves it. It must be
    done before the stock Steam starts, which would otherwise find a listed folder that is not mounted. Every session
    unit is confirmed stopped by now. A failure is said and recovery goes on: nothing of the user's is in danger.
    """
    store=transaction.get('game_store')
    if not store:return
    if game_store_slot(root,transaction):return
    try:
        module=game_store_module(root);layout=module.Layout(store)
    except Exception as error:
        print('Dosplex recovery: shared game store not put away: '+str(error),file=sys.stderr);return
    try:stores=module.listed_layouts(layout)
    except Exception as error:
        print('Dosplex recovery: shared game store disks not listed, the main store only: '+str(error),file=sys.stderr);stores=[layout]
    for store_layout in stores:
        for user in (transaction['main_user'],transaction['guest_user']):
            try:module.unmount(store_layout,user)
            except Exception as error:print('Dosplex recovery: shared game library of '+user+' ('+str(store_layout.root)+'): '+str(error),file=sys.stderr)


def start(bundle, manifest, seconds, main_user, guest_user, device_view=False, native_deck=None, native_relay=False, native_test=False, keep_session=False, game_store=None, layout='vertical', test_hooks=False):
    if layout not in LAYOUTS:
        raise ValueError('the layout is one of: ' + ', '.join(LAYOUTS))
    if not 30 <= seconds <= 1800:
        raise ValueError('duration must be30..1800 seconds')
    if native_deck not in (None, 'left', 'right') or (native_deck and not device_view):
        raise ValueError('native Deck experiment requires a side and private device view')
    if native_test and not native_relay:raise ValueError('synthetic native controls require native relay')
    if native_relay and not native_deck:raise ValueError('native relay requires native Deck side')
    users=[pwd.getpwnam(name) for name in (main_user,guest_user)]
    if users[0].pw_uid == users[1].pw_uid or any(u.pw_uid < 1000 for u in users):
        raise ValueError('two distinct ordinary user identities are required')
    for user in users:
        home=Path(user.pw_dir).lstat()
        if not stat.S_ISDIR(home.st_mode) or home.st_uid != user.pw_uid or home.st_mode & 0o077:
            raise PermissionError('side home must be private and owned by its user')
    if not wait_idle_steam(main_user,STEAM_READY_SECONDS):
        raise RuntimeError('Steam is not ready: its home screen did not show within '+str(STEAM_READY_SECONDS)+
                           ' seconds. Return to the Steam home screen, wait a minute and try again')
    if run(as_user(main_user,'systemctl','--user','is-active','steam-launcher.service'),check=False).returncode:
        raise RuntimeError('normal Steam service is not active')
    if run(['pgrep','-u',str(users[1].pw_uid),'-x','steam'],check=False).returncode == 0:
        raise RuntimeError('guest already has Steam running')
    spec=importlib.util.spec_from_file_location('integrity',Path(__file__).with_name('session-bundle-integrity.py'))
    integrity=importlib.util.module_from_spec(spec);spec.loader.exec_module(integrity)
    files=integrity.verified_files(bundle,manifest)
    if device_view and 'libexec/dosplex-device-view-session.py' not in files:
        raise RuntimeError('candidate lacks private device-view launcher')
    if native_relay and 'libexec/native-hid-relay' not in files:raise RuntimeError('candidate lacks native HID relay')
    if game_store is not None:
        if not Path(game_store).is_absolute() or '..' in Path(game_store).parts:raise ValueError('the game store must be an absolute path')
        if 'bin/dosplex-game-store.py' not in files:raise RuntimeError('candidate lacks the game store tool')
    ident=uuid.uuid4().hex; root=Path('/run')/('dosplex-trial-'+ident)
    transaction={'id':ident,'unit':'dosplex-compositor-'+ident+'.service',
        'recovery':'dosplex-trial-recovery-'+ident,'main_user':main_user,'guest_user':guest_user,
        'manifest':manifest,'seconds':seconds,'phase':'staged','device_view_experiment':device_view,
        'native_test_controls':bool(native_test), 'keep_session':bool(keep_session), 'initial_layout':layout,
        'test_hooks':bool(test_hooks)}
    if game_store is not None:transaction['game_store']=str(game_store)
    stage_and_arm(root, files, transaction)
    runtime=root/'runtime'
    # Serialize handoff against the already armed deadline recovery service.
    lock_fd=os.open(root/'recovery.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:
        os.close(lock_fd)
        raise
    try:
        (root/'control').mkdir(mode=0o700)
        os.chown(root/'control',users[0].pw_uid,users[0].pw_gid)
        grant_x11_access(root,transaction)
        previous_windows=windows(main_user)
        if not idle_steam(main_user): raise RuntimeError('foreground changed before handoff')
        # SteamOS can reactivate its launcher while we own the same profile.
        # A runtime mask is removed by both watcher and independent recovery.
        unit_state=run(as_user(main_user,'systemctl','--user','show','steam-launcher.service','-p','UnitFileState','--value')).stdout.strip()
        if unit_state.startswith('masked'):
            raise RuntimeError('normal Steam launcher was already masked')
        transaction['steam_runtime_mask_added']=True
        save(root,transaction)
        run(as_user(main_user,'systemctl','--user','mask','--runtime','steam-launcher.service'))
        # Steam can take a while to quit (it may be updating itself at boot), so this stop waits longer than the default.
        # A Steam that has just come up is the slow case, and this stop can take the full time above.
        try:
            run(as_user(main_user,'systemctl','--user','stop','steam-launcher.service'),timeout=STEAM_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            # systemd is still finishing the stop, and will kill Steam only after our wait; say to wait and try again.
            raise RuntimeError('Steam did not stop within '+str(STEAM_STOP_SECONDS)+
                               ' seconds. Wait until Steam shows its home screen, then try again') from None
        if run(['pgrep','-u',str(users[0].pw_uid),'-x','steam'],check=False).returncode == 0:
            raise RuntimeError('main Steam process remains after service stop')
        game_store_slot(root,transaction)
        if native_deck:
            module = native_module(root)
            plan = module.create(native_deck, users[0 if native_deck == 'left' else 1].pw_uid)
            transaction['native_deck_plan'] = plan
            transaction['steam_input_owners'] = {'left':users[0].pw_uid,'right':users[1].pw_uid}
            save(root, transaction)
            (root/'bundle/libexec/native-deck-plan.json').write_text(json.dumps(plan))
            if native_relay:
                transaction['native_relay_unit']='dosplex-native-relay-'+ident+'.service'
                save(root,transaction)
                run(['systemd-run','--quiet','--collect','--service-type=exec',
                     '--unit='+transaction['native_relay_unit'],
                     '--property=RuntimeMaxSec='+('infinity' if keep_session else str(seconds)),'--property=TimeoutStopSec=5s',
                     '--property=KillMode=control-group',*RELAY_RESTART,
                     '--','/usr/bin/python3',root/'bundle/libexec/native-deck-relay.py','--root',root])
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    plan=json.loads((root/'bundle/libexec/native-deck-plan.json').read_text())
                    if plan.get('relay'):break
                    time.sleep(.05)
                else:raise RuntimeError('native HID relay did not publish endpoint')
                transaction['native_deck_plan']=plan;save(root,transaction)
            module.grant(plan)
        env={'PATH':'/usr/bin:/bin','HOME':str(root/'home'),'DISPLAY':':0',
            'XAUTHORITY':str(Path(users[0].pw_dir)/'.Xauthority'),'XDG_RUNTIME_DIR':str(runtime),
            'LD_LIBRARY_PATH':str(root/'bundle/lib'),'DOSPLEX_ROOT':str(root/'bundle'),
            'DOSPLEX_SIDE_ROOT':str(root/'sides'),'DOSPLEX_COMPOSITOR_UNIT':transaction['unit'],
            'DOSPLEX_RUNTIME_DIR':str(runtime),'DOSPLEX_WAYLAND_DISPLAY':'dosplex-0',
            'DOSPLEX_STATE_PATH':str(runtime/'state.json'),'DOSPLEX_LEFT_USER':main_user,
            'DOSPLEX_RIGHT_USER':guest_user,'DOSPLEX_SESSION_PAYLOAD':'steam',
            'DOSPLEX_INITIAL_LAYOUT':layout,'DOSPLEX_AUTH_HELPER':str(root/'bundle/libexec/dosplex-side-auth.py'),
            'DOSPLEX_SESSION_SCRIPT':str(root/'bundle/bin/dosplex-side-supervisor.sh'),
            'DOSPLEX_REAL_SESSION_SCRIPT':str(root/'bundle/libexec'/('dosplex-device-view-session.py' if device_view else 'dosplex-user-side-session.sh'))}
        env.update(compositor_test_environment(root,transaction))
        # With Main alone on the screen Guest is not logged in; its side still starts, asleep, so that its session file exists.
        if layout=='single':env['DOSPLEX_GUEST_PRELOGIN']='1'
        if native_relay:env['DOSPLEX_NATIVE_CAPTURE_SOCKET']=str(runtime/'native-capture.sock')
        # The shared game store (ADR-023), off unless asked for: the supervisors mount and list each account's library.
        if transaction.get('game_store'):env['DOSPLEX_GAME_STORE']=transaction['game_store']
        hook=[]
        if keep_session:
            arm_recovery_hook(root,transaction)
            hook=['--property=OnFailure='+transaction['recovery_hook'],'--property=OnSuccess='+transaction['recovery_hook']]
        # The Guest Bluetooth policy must be on the bus before the compositor starts Guest's Steam.
        if guest_user and (root/'bundle/libexec/native-pads.py').exists():
            spec=importlib.util.spec_from_file_location('native_pads',root/'bundle/libexec/native-pads.py')
            pads=importlib.util.module_from_spec(spec);spec.loader.exec_module(pads)
            pads.install_policy(root,guest_user)
        run(['systemd-run','--quiet','--collect','--service-type=exec','--expand-environment=no',
             '--unit='+transaction['unit'],'--property=RuntimeMaxSec='+('infinity' if keep_session else str(seconds)),
             '--property=KillMode=control-group','--property=TimeoutStopSec=15s',*hook,
             '--','/usr/bin/env','-i',*[k+'='+v for k,v in env.items()],
             root/'bundle/bin/dosplex-compositor',*compositor_flags(transaction)])
        transaction['phase']='started';save(root,transaction)
        # A bundle from before the guard has no such file; the product's release must (the installer checks).
        if SLEEP_GUARD in files:start_sleep_guard(root,transaction)
        if not keep_session: run(['systemd-run','--quiet','--collect','--service-type=exec',
             '--unit=dosplex-trial-watch-'+ident,
             '--property=RuntimeMaxSec='+str(seconds+120),
             '--','/usr/bin/python3',root/'root-session-trial.py','watch','--root',root])
        transaction['presentation']=present_window(main_user,previous_windows)
        save(root,transaction)
    except BaseException:
        os.close(lock_fd)
        lock_fd=None
        if keep_session:
            transaction['phase']='startup_failed';save(root,transaction)
        else:
            recover(root)
        raise
    finally:
        if lock_fd is not None:os.close(lock_fd)
    return dict(transaction,root=str(root))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    begin=sub.add_parser('start');begin.add_argument('--bundle',type=Path,required=True)
    begin.add_argument('--manifest',required=True);begin.add_argument('--seconds',type=int,default=120)
    begin.add_argument('--main-user',default='deck');begin.add_argument('--guest-user',default='guest')
    begin.add_argument('--native-deck-experiment', choices=('left', 'right'), help='private native HID exposure; no seamless reassignment or overlay isolation acceptance')
    begin.add_argument('--native-test-controls',action='store_true',help='engineering only: replace physical controls with bounded root synthetic input')
    begin.add_argument('--test-hooks',action='store_true',help='engineering only: start the compositor with --test-hooks, the power stand-in and the relays\' synthetic controls')
    begin.add_argument('--keep-session',action='store_true',help='explicit development session: retain setup until manual recover; no automatic timer or watcher')
    begin.add_argument('--native-deck-relay',action='store_true',help='private native HID capture candidate; requires native Deck experiment')
    begin.add_argument('--device-view-experiment',action='store_true',help='private GPU-only device view; no controller support yet')
    begin.add_argument('--layout',choices=LAYOUTS,default='vertical',help='how the compositor starts: single is Main on the full screen')
    begin.add_argument('--game-store',type=Path,help='turn on the shared game store at this root-owned place (ADR-023); the installer passes its library folder')
    for command in ('recover','watch','ended'):
        end=sub.add_parser(command);end.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('explicit root execution required')
    try:value=_dispatch(args)
    except subprocess.TimeoutExpired as error:
        # No raw exception text: say which program did not answer and what to do.
        program=error.cmd[0] if isinstance(error.cmd,(list,tuple)) and error.cmd else str(error.cmd)
        print('A command did not answer in time ('+os.path.basename(str(program))+'). Wait a minute and try again.',file=sys.stderr)
        sys.exit(1)
    print(json.dumps(value))


def _dispatch(args):
    return (start(args.bundle,args.manifest,args.seconds,args.main_user,args.guest_user,args.device_view_experiment,args.native_deck_experiment,args.native_deck_relay,args.native_test_controls,args.keep_session,args.game_store,args.layout,args.test_hooks)
            if args.command=='start' else (watch(args.root) if args.command=='watch' else
                  (ended(args.root) if args.command=='ended' else recover(args.root))))


if __name__=='__main__':main()
