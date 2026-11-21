#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Read-only target inventory before a private Dosplex human-test deployment.

No Steam profile, login token, controller serial or HID payload is retained.
Optional root HID sampling counts protocol/length pairs for at most five seconds;
it neither grabs input nor writes controller feature reports.
This inventory does not authorize deployment or constitute a rollback snapshot.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import pwd
import select
import shutil
import subprocess
import time


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=8,
                                env=dict(os.environ, SYSTEMD_PAGER='cat', PAGER='cat'))
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def displays(sysroot=Path('/sys')):
    return [dict(connector=p.parent.name, status=read(p),
                 modes=(read(p.parent / 'modes') or '').splitlines())
            for p in sorted((sysroot / 'class/drm').glob('card*-*/status'))]


def hids(sysroot=Path('/sys')):
    result = []
    for p in sorted((sysroot / 'class/hidraw').glob('hidraw*')):
        fields = dict(line.split('=', 1) for line in
                      (read(p / 'device/uevent') or '').splitlines() if '=' in line)
        if ':000028DE:' not in fields.get('HID_ID', ''):
            continue
        result.append(dict(node='/dev/' + p.name, hid_id=fields['HID_ID'],
                           name=fields.get('HID_NAME'), driver=fields.get('DRIVER')))
    return result


def report_kind(data):
    if data and (data[0], len(data)) in ((0x42, 54), (0x45, 46), (0x47, 46)):
        return 'ibex-input'
    if len(data) == 64 and data[:4] == bytes((1, 0, 1, 60)):
        return 'steam-controller-input'
    if len(data) == 64 and data[:4] == bytes((1, 0, 9, 64)):
        return 'deck-input'
    return 'other'


def sample(devices, seconds):
    if not 0 < seconds <= 5:
        raise ValueError('sample duration must be greater than zero and at most five seconds')
    opened = {}
    counts = {d['node']: Counter() for d in devices}
    errors = {}
    try:
        for d in devices:
            try:
                fd = os.open(d['node'], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
                opened[fd] = d['node']
            except OSError as error:
                errors[d['node']] = error.errno
        until = time.monotonic() + seconds
        while opened and time.monotonic() < until:
            for fd in select.select(list(opened), [], [], min(.1, max(0, until - time.monotonic())))[0]:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        os.close(fd)
                        del opened[fd]
                        continue
                    counts[opened[fd]][(report_kind(data), len(data))] += 1
                except BlockingIOError:
                    continue
                except OSError as error:
                    errors[opened[fd]] = error.errno
                    os.close(fd)
                    del opened[fd]
    finally:
        for fd in opened:
            os.close(fd)
    return {node: dict(reports=[dict(kind=key[0], length=key[1], count=value)
                                for key, value in sorted(count.items())],
                       error_errno=errors.get(node)) for node, count in counts.items()}


def user_commands(user):
    uid = pwd.getpwnam(user).pw_uid
    env = ['env', 'XDG_RUNTIME_DIR=/run/user/' + str(uid),
           'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(uid) + '/bus']
    return (['runuser', '-u', user, '--'] if os.geteuid() == 0 else []) + env


def inventory(user='deck', seconds=0):
    prefix = user_commands(user)
    nodes = hids()
    output = dict(schema=1, collected_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  hardware=read(Path('/sys/class/dmi/id/product_name')),
                  kernel=os.uname().release, displays=displays(), controllers=nodes,
                  runtime_nodes={n: Path('/dev', n).exists() for n in ('uinput', 'cuse')},
                  tools={n: shutil.which(n) for n in ('bwrap', 'gamescope', 'socat', 'setfacl', 'steam')},
                  steam_launcher=command(prefix + ['systemctl', '--user', 'show',
                                                  'steam-launcher.service', '-p', 'ActiveState', '--value']))
    os_fields = dict(line.split('=', 1) for line in (read(Path('/etc/os-release')) or '').splitlines()
                     if '=' in line and not line.startswith('#'))
    output['os'] = {k: os_fields[k].strip('"') for k in ('ID', 'VERSION_ID', 'BUILD_ID') if k in os_fields}
    for kind in ('sinks', 'sources'):
        raw = command(prefix + ['pactl', '-f', 'json', 'list', kind])
        try:
            values = json.loads(raw) if raw is not None else None
            output[kind] = None if values is None else [
                dict(description=v.get('description'), channels=v.get('channel_map'),
                     active_port=v.get('active_port'), state=v.get('state')) for v in values]
        except (TypeError, ValueError):
            output[kind] = None
    output['side_users'] = {}
    for name in (user, 'guest'):
        try:
            entry = pwd.getpwnam(name)
            output['side_users'][name] = dict(uid=entry.pw_uid, home_mode=oct(Path(entry.pw_dir).stat().st_mode & 0o777))
        except KeyError:
            output['side_users'][name] = None
    if seconds:
        output['controller_sample'] = sample(nodes, seconds)
    blockers = []
    if not any(d['status'] == 'connected' for d in output['displays']):
        blockers.append('No connected display: turn on/check the TV and HDMI/DisplayPort connection.')
    if output['steam_launcher'] != 'active':
        blockers.append('Current trial launcher requires normal Steam Gaming mode; preserve original session before switching.')
    if output['side_users']['guest'] is None:
        blockers.append('Create and journal an independent Guest OS account before a two-account trial.')
    if not any(d['hid_id'].endswith(':00001205') for d in nodes):
        blockers.append('No built-in Deck controls: use the external-only native installer after identifying two live supported receiver slots.')
    if seconds and not any(r['kind'].endswith('-input') for v in output['controller_sample'].values() for r in v['reports']):
        blockers.append('No live controller input reports observed; wake controllers before verifying their protocol.')
    if not all(output['runtime_nodes'].values()) or not all(output['tools'].values()):
        blockers.append('Required runtime nodes or tools are missing; inspect before installing.')
    output['blockers'] = blockers
    output['ready_for_current_trial_launcher'] = not blockers
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default='deck')
    parser.add_argument('--sample-seconds', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.sample_seconds <= 5:
        parser.error('sample duration must be between zero and five seconds')
    print(json.dumps(inventory(args.user, args.sample_seconds), indent=2))
