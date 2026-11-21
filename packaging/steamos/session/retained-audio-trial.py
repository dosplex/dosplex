#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Reversible audio buses and two simulated headsets for a retained trial.

Only synthetic microphone feeds are created. Real microphones are never opened
by preparation. Shared Pulse transport remains an engineering routing mechanism,
not a security boundary between untrusted applications. Main's Steam uses it.
Guest's Steam does not: `socket` enrolls <root>/audio-guest, and the router unit
that `start` runs then provides Guest's restricted endpoint there (one output, one
microphone). Guest's first launch waits for it, so run `start` soon after `socket`.
"""
import argparse
import importlib.util
import json
import os
import pwd
from pathlib import Path
import re
import stat
import subprocess
import time


def load(path):
    spec=importlib.util.spec_from_file_location('audio',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def save(path,value):
    p=path.with_suffix('.new');p.write_text(json.dumps(value,indent=2));p.chmod(0o600);p.replace(path)


def owned(module,router):
    args=router.module_args(module)
    if router.managed_route(module):return True
    names={router.bus(s,k) for s in router.SIDES for k in router.KINDS}
    names.update('dosplex-test-headset-'+s for s in ('a','b'))
    names.update('dosplex_sim_'+s+'_feed' for s in ('a','b'))
    return (module['name']=='module-null-sink' and args.get('sink_name') in names or
            module['name']=='module-remap-source' and args.get('source_name') in {'dosplex-test-mic-a','dosplex-test-mic-b'} or
            module['name']==router.TCP_MODULE and args.get('listen')=='127.0.0.1' and args.get('auth-ip-acl')=='127.0.0.1')


def default_device(router,kind):
    """The server's default sink or source, or None when it has none. A device without a microphone (the
    Steam Machine with nothing plugged in) answers get-default-source with the placeholder @DEFAULT_SOURCE@,
    and set-default-source refuses that name with "No such entity"; a None is simply not restored."""
    name=router.pactl_checked('get-default-'+kind)
    return None if not name or name.startswith('@') else name


def restore_defaults(router,snapshot):
    """Put the defaults back that preparation found, so the buses it loaded do not become the default."""
    for kind in ('sink','source'):
        name=snapshot.get('default_'+kind)
        if name:router.pactl_checked('set-default-'+kind,name)


def enroll_guest(root,tx,router):
    """Create Guest's endpoint directory and configuration, and say how the router is to run the endpoint.

    Guest's wrapper treats the directory as enrollment: it waits for the endpoint inside it and fails
    visibly if it never comes up, whereas a trial without the directory gives Guest no audio. So this
    happens as soon as the audio socket is enrolled, before the router (which runs the endpoint) starts.
    """
    if not tx.get('guest_user'):return []
    directory=root/'audio-guest'
    router.GuestEndpoint(directory,owner=pwd.getpwnam(tx['guest_user'])).prepare()
    return ['DOSPLEX_RIGHT_USER='+tx['guest_user'],'DOSPLEX_GUEST_AUDIO_DIR='+str(directory)]


def socket_command(tx,endpoint,proxy_unit):
    """The unit that forwards Main's relay socket to the shared server.

    Mode 0600 under Main's own account: Main's session is the only client (Guest has its own endpoint, and
    root ignores the mode), so no other local account reaches Main's audio server, microphone or module loading.
    """
    return ['systemd-run','--quiet','--collect','--service-type=exec','--unit='+proxy_unit,
        '--property=BindsTo='+tx['unit'],'--property=KillMode=control-group',
        '--property=User='+tx['main_user'],'--','/usr/bin/socat',
        'UNIX-LISTEN:'+endpoint+',fork,mode=0600','TCP:127.0.0.1:4713']


def router_command(root,tx,unit,guest_env):
    """The audio router unit. It restarts if it dies, or Guest stays silent until the next launch."""
    return ['systemd-run','--quiet','--collect','--service-type=exec','--unit='+unit,
        '--property=BindsTo='+tx['unit'],'--property=KillMode=control-group',
        '--property=Restart=on-failure','--property=RestartSec=2',
        '--property=StartLimitIntervalSec=60','--property=StartLimitBurst=5','--','/usr/bin/env',
        'DOSPLEX_SIDE_ROOT='+str(root/'sides'),'DOSPLEX_AUDIO_STATE='+str(root/'audio-routes.json'),
        'DOSPLEX_STATE_PATH='+str(root/'runtime/state.json'),
        'DOSPLEX_AUDIO_SERVER_USER='+tx['main_user'],*guest_env,'/usr/bin/python3',
        str(root/'bundle/libexec/dosplex-audio-router.py'),'monitor-shared']


def run(root,action,simulated=True):
    if os.geteuid() or root.parent!=Path('/run') or not re.fullmatch('dosplex-trial-[a-f0-9]{32}',root.name):raise ValueError('requires root retained trial')
    for p in (root,root/'transaction.json',root/'bundle/libexec'):
        st=p.lstat()
        if st.st_uid or st.st_mode&0o022 or p.is_symlink():raise PermissionError('untrusted trial')
    txpath=root/'transaction.json';tx=json.loads(txpath.read_text())
    router=load(root/'bundle/libexec/dosplex-audio-router.py')
    router.SERVER_USER=tx['main_user'];router.SIDE_ROOT=root/'sides';router.STATE_PATH=root/'audio-routes.json'
    journal=root/'audio-trial.json';unit='dosplex-audio-'+tx['id']+'.service'
    if action=='prepare':
        if journal.exists():
            if json.loads(journal.read_text()).get('phase') != 'cleaned':
                raise RuntimeError('audio already prepared')
            journal.rename(root/('audio-cleaned-'+str(time.time_ns())+'.json'))
        modules=router.query('modules')
        if any(owned(m,router) for m in modules):raise RuntimeError('pre-existing Dosplex modules; inspect before enrolling')
        snapshot={'baseline':modules,'default_sink':default_device(router,'sink'),
                  'default_source':default_device(router,'source'),'phase':'preparing'}
        save(journal,snapshot)
        tx['audio_trial_unit']=unit;save(txpath,tx)
        router.ensure_per_side_sinks()
        for suffix in (('a','b') if simulated else ()):
            router.pactl_checked('load-module','module-null-sink','sink_name=dosplex-test-headset-'+suffix,
                'sink_properties=device.description=Simulated_Headset_'+suffix.upper())
            router.pactl_checked('load-module','module-null-sink','sink_name=dosplex_sim_'+suffix+'_feed')
            router.pactl_checked('load-module','module-remap-source','master=dosplex_sim_'+suffix+'_feed.monitor',
                'source_name=dosplex-test-mic-'+suffix,'source_properties=device.description=Simulated_Microphone_'+suffix.upper())
        if not any(m['name']==router.TCP_MODULE for m in modules):
            router.pactl_checked('load-module',router.TCP_MODULE,*router.TCP_ARGS.split())
        restore_defaults(router,snapshot)
        router.initial_routes()  # persisted claims, or Main's default output on a first run (T-302)
        snapshot['phase']='prepared';save(journal,snapshot)
    elif action=='socket':
        if not journal.exists():raise RuntimeError('prepare first')
        enroll_guest(root,tx,router)
        directory=root/'audio';uid=pwd.getpwnam(tx['main_user']).pw_uid
        if not directory.exists():
            directory.mkdir(mode=0o700);os.chown(directory,uid,uid)
        st=directory.lstat()
        if directory.is_symlink() or st.st_uid!=uid or st.st_mode&0o022:
            raise PermissionError('untrusted audio socket directory')
        endpoint=str(directory/'native')
        proxy_unit='dosplex-audio-socket-'+tx['id']+'.service'
        tx['audio_socket_unit']=proxy_unit;tx['audio_unix_socket']=endpoint;save(txpath,tx)
        active=subprocess.run(['systemctl','is-active','--quiet',proxy_unit]).returncode==0
        if not active:
            path=Path(endpoint)
            if path.exists():
                st=path.lstat()
                if not stat.S_ISSOCK(st.st_mode) or st.st_uid!=uid:raise PermissionError('unexpected audio endpoint')
                path.unlink()
            subprocess.run(socket_command(tx,endpoint,proxy_unit),check=True)
        deadline=time.monotonic()+5
        while not Path(endpoint).is_socket():
            if time.monotonic()>deadline:raise RuntimeError('audio socket did not start')
            time.sleep(.05)
    elif action=='start':
        if tx.get('audio_socket_unit'):run(root,'socket')
        if not journal.exists():raise RuntimeError('prepare first')
        guest_env=enroll_guest(root,tx,router)
        subprocess.run(router_command(root,tx,unit,guest_env),check=True)
    elif action=='cleanup':
        if not journal.exists():return
        if tx.get('audio_socket_unit'):
            expected='dosplex-audio-socket-'+tx['id']+'.service'
            if tx['audio_socket_unit']!=expected:raise ValueError('unexpected audio socket unit')
            subprocess.run(['systemctl','stop',expected],capture_output=True)  # already-collected units are fine; state is verified next
            observed=subprocess.check_output(['systemctl','show',expected,'-p','ActiveState','--value'],text=True).strip()
            if observed not in ('inactive','failed'):raise RuntimeError('audio socket still running')
        subprocess.run(['systemctl','stop',unit],capture_output=True)
        state=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','--value'],text=True).strip()
        if state not in ('inactive','failed'):raise RuntimeError('audio watcher still active')
        snapshot=json.loads(journal.read_text());baseline={m['index'] for m in snapshot['baseline']}
        modules=[m for m in router.query('modules') if m['index'] not in baseline and owned(m,router)]
        # Detach routes first, then remapped microphones, then null sinks/TCP.
        modules.sort(key=lambda m:0 if m['name']=='module-loopback' else 1 if m['name']=='module-remap-source' else 2)
        for module in modules:router.pactl_checked('unload-module',str(module['index']))
        snapshot['phase']='cleaned';save(journal,snapshot)
    print(json.dumps({'audio_action':action,'unit':unit}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--no-simulated-headsets',action='store_true',help='human trial: expose real devices only')
    p.add_argument('action',choices=('prepare','socket','start','cleanup'));a=p.parse_args();run(a.root,a.action,not a.no_simulated_headsets)
