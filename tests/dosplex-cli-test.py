#!/usr/bin/env python3
# dosplex-test: tier=integration timeout=300
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Hermetic tests of the `dosplex` command (T-416): install, start, stop, status, repair and uninstall.

Everything runs against a fake sysroot (--sysroot) and a PATH that holds only fake useradd, userdel,
systemctl, systemd-run, runuser, steamosctl, gamescope, loginctl and the Gamescope display tools. The
fake commands log every call, so a test can show what ran and, for a refusal, that nothing did. The
bundle is a small fake with the layout of a real one (bin/ libexec/ lib/ tools/ and a manifest); its
tools are fakes that only record what the installed release asked them to do.

No device, no root, no ssh and no network is involved. This proves the install layout, the journal,
the verification order and the policy rules. It does not prove that the real retained-trial tools work;
that needs the hardware check (the seed contract test below only pins the interface the CLI uses).

Run without arguments the tests are spread over several processes (set DOSPLEX_TEST_SERIAL=1 for one);
with test names, for example `Interrupted.test_start_interrupted_at_every_point_is_rolled_back_by_repair`,
only those run.
"""
import argparse
import concurrent.futures
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/dosplex'
UID = os.getuid()


def load_cli():
    loader = importlib.machinery.SourceFileLoader('dosplex_under_test', str(CLI))
    spec = importlib.util.spec_from_loader('dosplex_under_test', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dosplex = load_cli()

# --- the fake commands --------------------------------------------------------------------------------
# One script, installed under every command name. It keeps its world in FAKE_STATE and edits the fake
# sysroot (FAKE_SYSROOT) the way the real command would.
FAKE_COMMAND = r'''#!@PYTHON@ -S
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path

STATE = Path(os.environ['FAKE_STATE'])
TREE = Path(os.environ['FAKE_SYSROOT'])
NAME = os.path.basename(sys.argv[0])
ARGS = sys.argv[1:]


def log():
    with (STATE / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'cmd': NAME, 'argv': ARGS}) + '\n')


def load(name, default=None):
    try:
        return json.loads((STATE / name).read_text())
    except OSError:
        return default


def save(name, value):
    (STATE / name).write_text(json.dumps(value))


def cmd_useradd():
    root, shell, name, seen = TREE, '/bin/sh', None, set()
    index = 0
    while index < len(ARGS):
        arg = ARGS[index]
        if arg == '--root':
            root, index = Path(ARGS[index + 1]), index + 2
        elif arg == '--shell':
            shell, index = ARGS[index + 1], index + 2
        elif arg in ('--create-home', '--user-group'):
            seen.add(arg)
            index += 1
        elif arg.startswith('-'):
            print('useradd: unexpected option ' + arg, file=sys.stderr)
            return 2
        else:
            name, index = arg, index + 1
    if os.environ.get('FAKE_USERADD_FAIL'):
        print('useradd: cannot lock /etc/passwd', file=sys.stderr)
        return 10
    passwd, group = root / 'etc/passwd', root / 'etc/group'
    uid = max([int(line.split(':')[2]) for line in passwd.read_text().splitlines()
               if int(line.split(':')[2]) >= 1000] + [999]) + 1
    gid = max([int(line.split(':')[2]) for line in group.read_text().splitlines()
               if int(line.split(':')[2]) >= 1000] + [999]) + 1
    with group.open('a') as stream:
        stream.write('%s:x:%d:\n' % (name, gid))
    with passwd.open('a') as stream:
        stream.write('%s:x:%d:%d::/home/%s:%s\n' % (name, uid, gid, name, shell))
    home = root / 'home' / name
    home.mkdir(parents=True, mode=0o755)
    os.chmod(home, 0o755)
    (home / '.bash_logout').write_text('')
    return 0


def cmd_userdel():
    root, name = TREE, ARGS[-1]
    if ARGS[0] == '--root':
        root = Path(ARGS[1])
    passwd, group = root / 'etc/passwd', root / 'etc/group'
    passwd.write_text(''.join(l + '\n' for l in passwd.read_text().splitlines() if not l.startswith(name + ':')))
    kept = []
    for row in [l.split(':') for l in group.read_text().splitlines()]:
        if row[0] == name and not row[3]:
            continue                                     # the user's own group goes with the user
        row[3] = ','.join(m for m in row[3].split(',') if m and m != name)       # and so does every membership
        kept.append(row)
    group.write_text(''.join(':'.join(row) + '\n' for row in kept))
    return 0


def cmd_gpasswd():
    args, root = list(ARGS), TREE
    if args[:1] == ['--root']:
        root, args = Path(args[1]), args[2:]
    if len(args) != 3 or args[0] not in ('-d', '-a'):
        print('gpasswd: unexpected arguments', file=sys.stderr)
        return 2
    flag, user, name = args
    if os.environ.get('FAKE_GPASSWD_FAIL') in ('1', name):
        print('gpasswd: cannot lock /etc/group', file=sys.stderr)
        return 10
    path = root / 'etc/group'
    rows = [l.split(':') for l in path.read_text().splitlines()]
    row = next((r for r in rows if r[0] == name), None)
    if row is None:
        print("gpasswd: group '%s' does not exist" % name, file=sys.stderr)
        return 3
    members = [m for m in row[3].split(',') if m]
    if flag == '-d':
        if user not in members:
            print("gpasswd: user '%s' is not a member of '%s'" % (user, name), file=sys.stderr)
            return 3
        members.remove(user)
    elif user not in members:
        members.append(user)
    row[3] = ','.join(members)
    path.write_text(''.join(':'.join(r) + '\n' for r in rows))
    return 0


def cmd_id():
    user = ARGS[-1]
    entry = next((l.split(':') for l in (TREE / 'etc/passwd').read_text().splitlines() if l.startswith(user + ':')), None)
    if entry is None:
        print('id: %s: no such user' % user, file=sys.stderr)
        return 1
    rows = [l.split(':') for l in (TREE / 'etc/group').read_text().splitlines()]
    names = [r[0] for r in rows if r[2] == entry[3]]
    names += [r[0] for r in rows if user in r[3].split(',') and r[0] not in names]
    print(' '.join(names))
    return 0


def cmd_systemctl():
    verb = ARGS[0]
    unit = next((a for a in ARGS[1:] if not a.startswith('-')), None)
    marker = STATE / 'units' / (unit or '-')
    if verb == 'is-active':
        print('active' if marker.exists() else 'inactive')
        return 0 if marker.exists() else 3
    if verb == 'show':
        print('ActiveState=%s' % ('active' if marker.exists() else 'inactive'))
    elif verb == 'stop':
        marker.unlink(missing_ok=True)
    elif verb == 'daemon-reload' and (STATE / 'reload-fails').exists():
        print('Failed to reload the daemon', file=sys.stderr)
        return 1
    elif verb == 'daemon-reload' and (STATE / 'reload-fails-second').exists():
        # The first reload (the boot unit) works; the second one (the automatic sharing units) does not.
        count = STATE / 'reload-count'
        count.write_text(str(int(count.read_text() if count.exists() else 0) + 1))
        if int(count.read_text()) >= 2:
            print('Failed to reload the daemon', file=sys.stderr)
            return 1
    elif verb == 'start' and (STATE / 'start-fails').exists():
        print('Failed to start %s' % unit, file=sys.stderr)
        return 1
    return 0


def cmd_systemd_run():
    split = ARGS.index('--')
    options, command = ARGS[:split], ARGS[split + 1:]
    unit = next(o.split('=', 1)[1] for o in options if o.startswith('--unit='))
    (STATE / 'units').mkdir(exist_ok=True)
    (STATE / 'units' / unit).write_text('active')
    if '--wait' not in options:
        # A background unit (dosplex control): systemd-run returns at once and the service goes on without us.
        # While STATE/hold exists the service waits, so a test can look at an operation that is still running.
        script = ('import os, subprocess, sys, time\n'
                  'state, unit = sys.argv[1], sys.argv[2]\n'
                  'while os.path.exists(os.path.join(state, "hold")): time.sleep(0.05)\n'
                  'out = open(os.path.join(state, "worker.log"), "a")\n'
                  'subprocess.run(sys.argv[3:], stdout=out, stderr=out)\n'
                  'os.unlink(os.path.join(state, "units", unit))\n')
        subprocess.Popen([sys.executable, '-c', script, str(STATE), unit, *command], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    # Like the real service: what the worker prints goes to a log nobody is looking at, not to the caller.
    with (STATE / 'worker.log').open('a') as log:
        result = subprocess.run(command, stdout=log, stderr=log)
    (STATE / 'units' / unit).unlink(missing_ok=True)
    return result.returncode


def cmd_runuser():
    inner = ARGS[ARGS.index('--') + 1:]
    if inner[0] == 'env':
        inner = inner[1:]
        while inner and re.fullmatch(r'[A-Z_]+=.*', inner[0]):
            key, value = inner[0].split('=', 1)
            os.environ[key] = value
            inner = inner[1:]
    os.execvp(inner[0], inner)


def set_processes(mode):
    comm = {'desktop': 'kwin_wayland', 'gaming': 'gamescope-wl'}[mode]
    (TREE / 'proc/100/comm').write_text(comm + '\n')


def cmd_steamosctl():
    data = load('steamos.json')
    verb = ARGS[0]
    if verb == 'get-default-login-mode':
        print(data['login_mode'])
    elif verb == 'get-default-desktop-session':
        print(data['desktop_session'])
    elif verb == 'switch-to-game-mode':
        data['mode'] = 'gaming'
        set_processes('gaming')
    elif verb == 'switch-to-desktop-mode':
        data['mode'] = 'desktop'
        data['desktop_requested'] = ARGS[1]
        set_processes('desktop')
    save('steamos.json', data)
    return 0


def cmd_steamos_add_to_steam():
    # What the real tool ends in: Steam writes the shortcut a moment later. The flags in FAKE_STATE make it fail,
    # hang, or return without Steam ever writing anything.
    (STATE / 'add-env.json').write_text(json.dumps({k: os.environ.get(k) for k in
                                                    ('DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')}))
    if (STATE / 'add-fail').exists():
        print('could not talk to Steam', file=sys.stderr)
        return 1
    if (STATE / 'add-hang').exists():
        time.sleep(60)
    if not (STATE / 'add-nothing').exists():
        folder = TREE / 'home/deck/.local/share/Steam/userdata/4242/config'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'shortcuts.vdf').write_bytes(b'\x00shortcuts\x00\x00' + b'0\x00\x01AppName\x00Dosplex\x00'
                                               b'\x01Exe\x00/usr/bin/python3 /opt/dosplex/current/libexec/'
                                               b'dosplex-start-client.py\x00\x08\x08')
    return 0


def cmd_gamescope():
    print('gamescope version %s (gcc 15.2.1 20250813)' % load('gamescope.json'))
    return 0


def cmd_loginctl():
    sessions = load('logins.json', {})
    name = ARGS[1]
    if name not in sessions:
        print('User %s is not logged in or lingering.' % name, file=sys.stderr)
        return 1
    value = sessions[name]
    if isinstance(value, dict) and 'leaves_after' in value and ARGS[2] == '--property=State':
        # Guest's processes end once the program has looked this many times (a user manager needs some seconds).
        count = load('logins-looks', 0) + 1
        save('logins-looks', count)
        if count > value['leaves_after']:
            shutil.rmtree(TREE / 'proc' / value['pid'], ignore_errors=True)
            print('closing' if count == value['leaves_after'] + 1 else 'inactive')
            return 0
    prop = ARGS[2].split('=', 1)[1] if len(ARGS) > 2 and ARGS[2].startswith('--property=') else 'State'
    # A plain string is the State; a dict gives the properties one by one (Linger defaults to "no").
    print(value.get(prop, 'no' if prop == 'Linger' else '') if isinstance(value, dict) else
          (value if prop == 'State' else 'no'))
    return 0


# --- a small Gamescope: connector description, mode list, saved mode, host Xwayland size -----------------
def display():
    return load('display.json')


def apply_saved_mode():
    data = display()
    config = Path(os.environ['FAKE_SYSROOT']) / 'home/deck/.config/gamescope/modes.cfg'
    for line in config.read_text().splitlines():
        if line.startswith(data['description'] + ':'):
            width, height, rate = re.search(r':(\d+)x(\d+)@(\d+)', line).groups()
            data['physical'] = [int(width), int(height), int(rate)]
    save('display.json', data)
    state = TREE / 'sys/kernel/debug/dri/0/state'
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('crtc[41]: crtc-0\n\tactive=1\n\tmode: "%dx%d": %d 1 2 3\n' % tuple(data['physical']))


def cmd_gamescopectl():
    print('Display Make: LG Electronics\nDisplay Model: LG TV')
    return 0


def cmd_xprop():
    data = display()
    if '-set' in ARGS:
        name, value = ARGS[ARGS.index('-set') + 1], ARGS[ARGS.index('-set') + 2]
        if name == 'GAMESCOPE_DISPLAY_MODE_NUDGE' and not os.environ.get('FAKE_DISPLAY_IGNORE_NUDGE'):
            apply_saved_mode()
        elif name == 'GAMESCOPE_XWAYLAND_MODE_CONTROL':
            _, width, height, _ = [int(v) for v in value.split(',')]
            data['virtual'] = [width, height]
            save('display.json', data)
        return 0
    print('GAMESCOPE_DISPLAY_MODE_LIST_EXTERNAL(CARDINAL) = 1920x1080@60 3840x2160@60 3840x2160@30')
    return 0


def cmd_xwininfo():
    data = display()
    print('  Width: %d\n  Height: %d' % tuple(data['virtual']))
    return 0


log()
handler = globals().get('cmd_' + NAME.replace('-', '_'))
sys.exit((handler() if handler else 0) or 0)
'''

COMMANDS = ('useradd', 'userdel', 'gpasswd', 'id', 'systemctl', 'systemd-run', 'runuser', 'steamosctl', 'gamescope', 'loginctl',
            'gamescopectl', 'xprop', 'xwininfo', 'bwrap', 'socat', 'setfacl', 'steam', 'xdotool', 'xhost', 'busctl',
            'steamos-add-to-steam')

# --- the fake bundle's tools --------------------------------------------------------------------------
# The shared game library tool of a release: it records what it was asked and prints what the test put in
# FAKE_STATE (store-VERB.json); store-fail-VERB makes it refuse with that message, store-exit-VERB sets its exit
# status. `on` makes the config; a `slot` report with "left": true takes the store away.
FAKE_STORE = r'''
import json, os, shutil, sys
from pathlib import Path

STATE = Path(os.environ['FAKE_STATE'])
verb, args = sys.argv[1], sys.argv[2:]
with (STATE / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'event': 'store', 'verb': verb, 'argv': args}) + '\n')
root = Path(args[args.index('--root') + 1])
refusal = STATE / ('store-fail-' + verb)
if refusal.exists():
    print('dosplex-game-store: ' + refusal.read_text(), file=sys.stderr)
    sys.exit(1)
canned = STATE / ('store-' + verb + '.json')
out = canned.read_text() if canned.exists() else '{}'
if verb == 'on':
    root.mkdir(parents=True, exist_ok=True)
    (root / 'config.json').write_text('{}')
elif verb == 'slot' and json.loads(out).get('left'):
    shutil.rmtree(root)
elif verb == 'share':
    print('verifying 620', file=sys.stderr)
print(out)
code = STATE / ('store-exit-' + verb)
status = int(code.read_text()) if code.exists() else 0
if status < 0:
    sys.stdout.flush()
    os.kill(os.getpid(), -status)
sys.exit(status)
'''

FAKE_TRIAL = r'''
import json, os, shutil, subprocess, sys, uuid
from pathlib import Path

SYS = Path(os.environ['FAKE_SYSROOT'])
STATE = Path(os.environ['FAKE_STATE'])


def log(**event):
    with (STATE / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\n')


log(event='seed-imported', module='root-session-trial', file=__file__)


def idle_steam(user):
    return json.loads((STATE / 'steamos.json').read_text())['mode'] == 'gaming' and not (STATE / 'busy').exists()


def trusted_root(root):
    pass


def present_window(user, previous):
    log(event='present_window', user=user)
    return {'window': '0x1', 'width': 3840, 'height': 2160}


def start(bundle, manifest, seconds, main_user, guest_user, device_view=False, native_deck=None,
          native_relay=False, native_test=False, keep_session=False, layout='vertical', test_hooks=False):
    log(event='trial.start', bundle=str(bundle), manifest=manifest, seconds=seconds, main_user=main_user,
        guest_user=guest_user, device_view=device_view, keep_session=keep_session, native_deck=native_deck,
        native_relay=native_relay, layout=layout, test_hooks=test_hooks)
    failure = os.environ.get('FAKE_TRIAL_FAIL')
    if failure == 'before-root':
        raise RuntimeError('fake trial failed before it created anything')
    if failure == 'timeout':
        # The stop of the stock Steam is a command line; a TimeoutExpired of it must never be shown to the person.
        raise subprocess.TimeoutExpired(['runuser', '-u', 'deck', '--', '/usr/bin/env', '-i',
                                         'PATH=/usr/bin:/bin', 'HOME=/home/deck', 'DISPLAY=:0',
                                         'systemctl', '--user', 'stop', 'steam-launcher.service'], 150)
    ident = uuid.uuid4().hex
    root = SYS / 'run' / ('dosplex-trial-' + ident)
    root.mkdir(parents=True, mode=0o755)
    unit = 'dosplex-compositor-%s.service' % ident
    (STATE / 'units').mkdir(exist_ok=True)
    (STATE / 'units' / unit).write_text('active')
    guard = 'dosplex-sleep-guard-%s.service' % ident
    (STATE / 'units' / guard).write_text('active')
    transaction = {'id': ident, 'unit': unit, 'main_user': main_user, 'guest_user': guest_user,
                   'phase': 'started', 'keep_session': True, 'manifest': manifest, 'sleep_guard_unit': guard}
    (root / 'transaction.json').write_text(json.dumps(transaction))
    (root / 'root-session-trial.py').write_bytes(Path(__file__).read_bytes())
    os.chmod(root / 'root-session-trial.py', 0o644)
    for side, owner in (('left', main_user), ('right', guest_user)):
        (root / 'sides' / side).mkdir(parents=True)
        (root / 'sides' / side / 'session.json').write_text(json.dumps(
            {'side': side, 'owner': owner, 'lifecycle': 'running', 'restart_count': 0 if side == 'left' else 1}))
    (root / 'runtime').mkdir()
    (root / 'runtime/state.json').write_text(json.dumps(
        {'left': {'x': 0, 'y': 0, 'width': 1920, 'height': 2160},
         'right': {'x': 1920, 'y': 0, 'width': 1920, 'height': 2160}}))
    if failure == 'after-root':
        raise RuntimeError('fake trial failed after it created its directory')
    return dict(transaction, root=str(root))


def recover(root):
    root = Path(root)
    log(event='trial.recover', root=str(root))
    if os.environ.get('FAKE_RECOVER_FAIL'):
        raise SystemExit('fake recovery failure')
    transaction = json.loads((root / 'transaction.json').read_text())
    (STATE / 'units' / transaction['unit']).unlink(missing_ok=True)
    for unit in (STATE / 'units').glob('dosplex-*-' + transaction['id'] + '.service'):
        unit.unlink()
    shutil.rmtree(root)
    steam = json.loads((STATE / 'steamos.json').read_text())
    steam['steam_restored'] = steam.get('steam_restored', 0) + 1
    (STATE / 'steamos.json').write_text(json.dumps(steam))


if __name__ == '__main__':
    if sys.argv[1:2] == ['recover'] and sys.argv[2] == '--root':
        recover(sys.argv[3])
'''

FAKE_EXTERNAL = r'''
import json, os
from pathlib import Path

STATE = Path(os.environ['FAKE_STATE'])


def log(**event):
    with (STATE / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\n')


log(event='seed-imported', module='start-external-native-trial', file=__file__)


def inspect_slot(hid, sysroot=None):
    return (hid, 'key')


def run(root, main_hid, guest_hid, builtin_deck=False):
    log(event='external.run', root=str(root), main=None if main_hid is None else str(main_hid),
        guest=None if guest_hid is None else str(guest_hid), builtin_deck=builtin_deck)
    if os.environ.get('FAKE_TRIAL_FAIL') == 'external':
        raise RuntimeError('fake controller enrollment failed')
    unassigned = [slot for slot in os.environ.get('FAKE_UNASSIGNED', '').split(',') if slot]
    endpoint = Path(root) / 'audio-guest' / 'pulse'
    endpoint.mkdir(parents=True)
    (endpoint / 'native').write_text('socket')
    units = STATE / 'units'
    transaction = json.loads((Path(root) / 'transaction.json').read_text())
    for prefix in ('dosplex-native-controller-', 'dosplex-audio-'):
        (units / (prefix + transaction['id'] + '.service')).write_text('active')
    return {'unassigned': unassigned}
'''

VERSION_ID, BUILD_ID, GAMESCOPE, STEAM = '3.9.2', '20260925.101', '3.16.30', '1759461351'
COMMIT = '0123456789abcdef0123456789abcdef01234567'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tested(**changes):
    value = {'steamos': [{'version_id': VERSION_ID, 'build_id': BUILD_ID}], 'gamescope': [GAMESCOPE],
             'steam_client': [STEAM]}
    value.update(changes)
    return value


class Bundle:
    """A bundle directory with the layout of a real one and a manifest with the new version fields."""

    def __init__(self, path, version='0.1.0-test', tested_versions=None, extra=None, replace=None):
        self.path = Path(path)
        self.version = version
        contents = {}
        for name in dosplex.REQUIRED_RELEASE_FILES:
            if name == 'tools/root-session-trial.py':
                contents[name] = FAKE_TRIAL.encode()
            elif name == 'tools/start-external-native-trial.py':
                contents[name] = FAKE_EXTERNAL.encode()
            elif name.endswith('.py'):
                contents[name] = ('# fake %s %s\n' % (name, version)).encode()
            else:
                contents[name] = ('#!/bin/sh\n# fake %s %s\n' % (name, version)).encode()
        contents['lib/libfake.so.1'] = bytes(range(256)) * 8
        contents['licenses/notice'] = b'licence text\n'
        contents['LICENSE'] = b'GNU Affero General Public License v3\n'
        contents.update(extra or {})
        contents.update(replace or {})
        files = {}
        for name, data in sorted(contents.items()):
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            mode = 0o755 if name.startswith(('bin/', 'libexec/', 'tools/')) else 0o644
            target.chmod(mode)
            files[name] = {'sha256': sha256(data), 'mode': mode, 'bytes': len(data)}
        manifest = {'commit': COMMIT, 'version': version, 'scope': 'test bundle',
                    'tested_versions': tested_versions if tested_versions is not None else tested(),
                    'files': files}
        self.manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
        (self.path / 'manifest.json').write_bytes(self.manifest_bytes)
        self.sha256 = sha256(self.manifest_bytes)


class Result:
    def __init__(self, proc):
        self.code, self.out, self.err = proc.returncode, proc.stdout, proc.stderr

    @property
    def text(self):
        return self.out + self.err


class World:
    """A fake sysroot, the fake commands and a place for bundles."""

    def __init__(self, directory, version_id=VERSION_ID, build_id=BUILD_ID, gamescope=GAMESCOPE,
                 steam=STEAM, desktop=True, receivers=True):
        self.root = Path(directory)
        self.tree, self.bin, self.fake = self.root / 'tree', self.root / 'bin', self.root / 'fake'
        for path in (self.tree, self.bin, self.fake / 'units'):
            path.mkdir(parents=True)
        script = self.bin / 'fake-command'
        script.write_text(FAKE_COMMAND.replace('@PYTHON@', sys.executable))
        script.chmod(0o755)
        for name in COMMANDS:
            (self.bin / name).symlink_to(script.name)
        t = self.tree
        self.write('etc/os-release', 'ID=steamos\nNAME="SteamOS"\nVERSION_ID=%s\nBUILD_ID=%s\n' % (version_id, build_id))
        self.write('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\ndeck:x:1000:1000:Deck:/home/deck:/bin/bash\n')
        self.write('etc/group', 'root:x:0:\nwheel:x:998:deck\ndeck:x:1000:\n')
        for directory in ('etc/systemd/system', 'var/lib', 'var/tmp', 'opt', 'run', 'home/deck/.config/gamescope',
                          'sys/class/drm/card0-HDMI-A-1', 'sys/class/hidraw', 'dev', 'proc/100', 'usr/bin', 'usr/lib'):
            (t / directory).mkdir(parents=True, exist_ok=True)
        self.write('home/deck/.config/gamescope/modes.cfg', 'LG Electronics LG TV:1920x1080@60 0\n')
        package = 'home/deck/.local/share/Steam/package'
        self.write(package + '/beta', 'steamdeck_stable\n')
        self.write(package + '/steam_client_steamdeck_stable_ubuntu12.manifest',
                   '"steamdeck_stable"\n{\n\t"version"\t\t"%s"\n}\n' % steam)
        self.write('sys/class/drm/card0-HDMI-A-1/status', 'connected\n')
        for node in ('uinput', 'cuse'):
            self.write('dev/' + node, '')
        self.write('proc/100/status', 'Name:\tx\nUid:\t1000\t1000\t1000\t1000\n')
        self.write('proc/100/comm', 'kwin_wayland\n' if desktop else 'gamescope-wl\n')
        self.write('sys/kernel/debug/dri/0/state', 'crtc[41]: crtc-0\n\tactive=1\n\tmode: "1920x1080": 60 1 2 3\n')
        self.write('usr/bin/steamos-session-select', '#!/bin/sh\n')
        self.write('usr/lib/os-release-sentinel', 'read-only image\n')
        if receivers:
            usb = t / 'sys/devices/usb/1-3'
            usb.mkdir(parents=True)
            for name, value in (('idVendor', '28de'), ('idProduct', '1304'), ('serial', 'fixture')):
                (usb / name).write_text(value)
            for number, node in ((2, 'hidraw4'), (3, 'hidraw5')):
                interface = usb / ('1-3:1.%d' % number)
                device = interface / ('0003:28DE:1304.000%d' % number)
                device.mkdir(parents=True)
                (interface / 'bInterfaceNumber').write_text(str(number))
                (device / 'uevent').write_text('HID_ID=0003:000028DE:00001304\n')
                entry = t / 'sys/class/hidraw' / node
                entry.mkdir()
                (entry / 'device').symlink_to(device)
                (t / 'dev' / node).write_bytes(bytes((0x42,)) + bytes(53))
        self.save_fake('steamos.json', {'login_mode': 'gaming', 'desktop_session': 'plasma.desktop',
                                        'mode': 'desktop' if desktop else 'gaming'})
        self.save_fake('gamescope.json', gamescope)
        self.save_fake('display.json', {'description': 'LG Electronics LG TV', 'physical': [1920, 1080, 60],
                                        'virtual': [1920, 1080]})
        self.make_read_only(t / 'usr')
        self.reset_baseline()

    # --- building --------------------------------------------------------------------------------
    def write(self, relative, text):
        path = self.tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def add_builtin_deck(self, first=0):
        """The Steam Deck's own controller 28de:1205: three HID interfaces, as a Deck shows them."""
        usb = self.tree / 'sys/devices/usb/1-1'
        usb.mkdir(parents=True)
        for name, value in (('idVendor', '28de'), ('idProduct', '1205')):
            (usb / name).write_text(value)
        nodes = []
        for number in range(3):
            interface = usb / ('1-1:1.%d' % number)
            device = interface / ('0003:28DE:1205.000%d' % number)
            device.mkdir(parents=True)
            (interface / 'bInterfaceNumber').write_text('0%d' % number)
            (device / 'uevent').write_text('HID_ID=0003:000028DE:00001205\n')
            node = 'hidraw%d' % (first + number)
            entry = self.tree / 'sys/class/hidraw' / node
            entry.mkdir()
            (entry / 'device').symlink_to(device)
            (self.tree / 'dev' / node).write_bytes(bytes((1, 0, 9, 64)) + bytes(60))
            nodes.append('/dev/' + node)
        self.reset_baseline()
        return nodes

    def reset_baseline(self):
        """Take the present state of the fake sysroot as the one every test compares against."""
        self.initial = self.snapshot()
        self.initial_full = self.snapshot(exclude=())
        self.initial_users = self.users()

    def save_fake(self, name, value):
        (self.fake / name).write_text(json.dumps(value))

    @staticmethod
    def make_read_only(path):
        for directory, _, names in os.walk(path):
            for name in names:
                os.chmod(os.path.join(directory, name), 0o444)
        for directory, _, _ in os.walk(path, topdown=False):
            os.chmod(directory, 0o555)

    def cleanup(self):
        for directory, _, _ in os.walk(self.root):
            os.chmod(directory, 0o755)

    def bundle(self, name='bundle', **options):
        return Bundle(self.root / name, **options)

    # --- running ---------------------------------------------------------------------------------
    def environment(self, **extra):
        env = {'PATH': str(self.bin), 'FAKE_STATE': str(self.fake), 'FAKE_SYSROOT': str(self.tree),
               'DOSPLEX_TEST_ROOT_UID': str(UID), 'PYTHONDONTWRITEBYTECODE': '1', 'HOME': str(self.root),
               'DOSPLEX_TEST_IDLE_SECONDS': '1', 'DOSPLEX_TEST_DISPLAY_SECONDS': '1',
               'DOSPLEX_TEST_GUEST_SECONDS': '1'}
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run(self, *args, input=None, root=True, timeout=180, **extra):
        command = [sys.executable, str(CLI), '--sysroot', str(self.tree)] + [str(a) for a in args]
        env = self.environment(**extra)
        if not root:
            env.pop('DOSPLEX_TEST_ROOT_UID')
        proc = subprocess.run(command, input=input, capture_output=True, text=True, env=env, timeout=timeout)
        return Result(proc)

    def install(self, bundle, *args, **options):
        return self.run('install', bundle.path, '--manifest', bundle.sha256, '--sample-seconds', '0.3', *args, **options)

    def controllers(self):
        return ('--main-hid', '/dev/hidraw4', '--guest-hid', '/dev/hidraw5')

    # --- looking ---------------------------------------------------------------------------------
    def calls(self):
        try:
            lines = (self.fake / 'calls.jsonl').read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines]

    def commands(self, name=None):
        return [c for c in self.calls() if 'cmd' in c and (name is None or c['cmd'] == name)]

    def events(self, name=None):
        return [c for c in self.calls() if 'event' in c and (name is None or c['event'] == name)]

    def units(self):
        return sorted(p.name for p in (self.fake / 'units').iterdir())

    def users(self):
        return ((self.tree / 'etc/passwd').read_text(), (self.tree / 'etc/group').read_text())

    def snapshot(self, exclude=('var/lib/dosplex',)):
        """path -> (kind, mode, content hash or link target) for the whole tree, except what is excluded."""
        found = {}
        for directory, names, files in os.walk(self.tree):
            relative = os.path.relpath(directory, self.tree)
            if relative in exclude:
                names[:] = []
                continue
            for name in names + files:
                path = os.path.join(directory, name)
                key = os.path.normpath(os.path.join(relative, name))
                if key in exclude:
                    continue
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    found[key] = ('link', 0, os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    found[key] = ('dir', stat.S_IMODE(info.st_mode), '')
                else:
                    with open(path, 'rb') as stream:
                        found[key] = ('file', stat.S_IMODE(info.st_mode), sha256(stream.read()))
        return found

    def journal_dir(self):
        return self.tree / 'var/lib/dosplex/journal'

    def journal(self):
        """All journal records, per transaction file."""
        result = {}
        for path in sorted(self.journal_dir().glob('*.jsonl')):
            result[path.name] = [json.loads(line) for line in path.read_text().splitlines()]
        return result

    def state(self):
        return json.loads((self.tree / 'var/lib/dosplex/state.json').read_text())


class Base(unittest.TestCase):
    def world(self, **options):
        previous = os.umask(0o022)      # the fake tree is built the way root would build it
        self.addCleanup(os.umask, previous)
        temp = tempfile.TemporaryDirectory(prefix='dosplex-cli-test-')
        world = World(temp.name, **options)
        self.addCleanup(temp.cleanup)
        self.addCleanup(world.cleanup)
        return world

    def assertOk(self, result):
        self.assertEqual(result.code, 0, result.text)

    def assertFailed(self, result, text=None, code=None):
        self.assertNotEqual(result.code, 0, result.text)
        if code is not None:
            self.assertEqual(result.code, code, result.text)
        if text is not None:
            self.assertIn(text, result.text)

    def assertNothingChanged(self, world, mutating=('useradd', 'userdel', 'systemd-run')):
        self.assertEqual(world.snapshot(exclude=()), world.initial_full, 'the sysroot changed')
        self.assertEqual([c['cmd'] for c in world.commands() if c['cmd'] in mutating], [])
        self.assertEqual(world.events(), [], 'code from the bundle ran')



class CleanSysroot(Base):
    def test_status_on_a_clean_sysroot_says_not_installed_and_changes_nothing(self):
        world = self.world()
        result = world.run('status')
        self.assertOk(result)
        self.assertIn('Dosplex is not installed.', result.out)
        info = json.loads(world.run('status', '--json').out)
        self.assertFalse(info['installed'])
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        self.assertEqual(world.calls(), [])

    def test_version_works_without_root_or_install(self):
        world = self.world()
        result = world.run('version')
        self.assertOk(result)
        self.assertIn('dosplex ' + dosplex.CLI_VERSION, result.out)
        self.assertIn('release: not installed', result.out)


class RoundTrip(Base):
    def test_install_then_uninstall_leaves_an_identical_tree_user_list_and_unit_list(self):
        world = self.world()
        bundle = world.bundle()
        result = world.install(bundle, *world.controllers())
        self.assertOk(result)
        self.assertNotEqual(world.snapshot(), world.initial)
        self.assertNotEqual(world.users(), world.initial_users)
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)
        self.assertEqual(world.units(), [])


class InstallLayout(Base):
    def test_payload_goes_to_a_release_directory_with_a_relative_current_symlink(self):
        world = self.world()
        bundle = world.bundle()
        self.assertOk(world.install(bundle, '--no-start'))
        opt = world.tree / 'opt/dosplex'
        self.assertEqual(sorted(p.name for p in opt.iterdir()), ['bin', 'current', 'releases'])
        self.assertEqual(os.readlink(opt / 'current'), 'releases/0.1.0-test')
        release = opt / 'releases/0.1.0-test'
        manifest = json.loads(bundle.manifest_bytes)
        for name, item in manifest['files'].items():
            installed = release / name
            self.assertEqual(sha256(installed.read_bytes()), item['sha256'], name)
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), item['mode'], name)
        self.assertEqual((release / 'manifest.json').read_bytes(), bundle.manifest_bytes)
        self.assertEqual(sorted(p.name for p in (release).iterdir()),
                         sorted({n.split('/')[0] for n in manifest['files']} | {'manifest.json'}))
        self.assertEqual((opt / 'bin/dosplex').read_bytes(), CLI.read_bytes())
        self.assertEqual(stat.S_IMODE((opt / 'bin/dosplex').stat().st_mode), 0o755)
        state = world.state()
        self.assertEqual((state['version'], state['manifest_sha256'], state['previous']),
                         ('0.1.0-test', bundle.sha256, None))
        self.assertEqual(world.units(), [])         # install --no-start leaves nothing running

    def test_one_previous_release_is_kept(self):
        world = self.world()
        opt = world.tree / 'opt/dosplex'
        for version in ('0.1.0-a', '0.1.0-b', '0.1.0-c'):
            self.assertOk(world.install(world.bundle(name='b-' + version, version=version), '--no-start'))
        self.assertEqual(sorted(p.name for p in (opt / 'releases').iterdir()), ['0.1.0-b', '0.1.0-c'])
        self.assertEqual(os.readlink(opt / 'current'), 'releases/0.1.0-c')
        self.assertEqual(world.state()['previous'], '0.1.0-b')

    def test_installing_the_same_bundle_again_changes_nothing(self):
        world = self.world()
        bundle = world.bundle()
        self.assertOk(world.install(bundle, '--no-start'))
        before = world.snapshot()
        result = world.install(bundle, '--no-start')
        self.assertOk(result)
        self.assertEqual(world.snapshot(), before)
        self.assertEqual(len(world.commands('useradd')), 1)

    def test_a_release_name_cannot_be_reused_for_different_contents(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        other = world.bundle(name='other', replace={'LICENSE': b'changed\n'})
        result = world.install(other, '--no-start')
        self.assertFailed(result, 'already installed with different contents')
        self.assertEqual(world.state()['manifest_sha256'], world.bundle(name='same').sha256)

    def test_the_journal_is_private_and_every_step_is_intent_then_done(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        journal = world.journal_dir()
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o700)
        for path in journal.glob('*.jsonl'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        (name, records), = world.journal().items()
        self.assertEqual(name, '0001-install.jsonl')
        kinds = [(r['kind'], r.get('step')) for r in records]
        self.assertEqual(kinds[0], ('begin', None))
        self.assertEqual(kinds[-1], ('end', None))
        self.assertEqual(records[-1]['outcome'], 'complete')
        order = [step for kind, step in kinds if kind == 'intent']
        self.assertEqual(order, ['defaults', 'stage', 'release', 'guest', 'current', 'cli', 'state', 'prune'])
        for step in order:
            intent, done = kinds.index(('intent', step)), kinds.index(('done', step))
            self.assertLess(intent, done)
            self.assertEqual(kinds[intent + 1:done], [])            # nothing is recorded in between
        self.assertEqual(kinds.index(('commit', None)), kinds.index(('done', 'stage')) + 1)
        self.assertEqual([r['i'] for r in records], list(range(len(records))))

    def test_original_session_defaults_are_recorded(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        defaults = world.state()['defaults']
        self.assertEqual((defaults['login_mode'], defaults['desktop_session'], defaults['host_mode'],
                          defaults['host_desktop'], defaults['main_user'], defaults['main_uid']),
                         ('gaming', 'plasma.desktop', 'desktop', 'plasma.desktop', 'deck', 1000))
        records = world.journal()['0001-install.jsonl']
        recorded = [r for r in records if r['kind'] == 'done' and r['step'] == 'defaults'][0]['data']
        self.assertEqual(recorded, defaults)

    def test_without_controller_slots_it_installs_and_starts_like_any_other_install(self):
        world = self.world()
        result = world.install(world.bundle())
        self.assertOk(result)
        self.assertNotIn('Not started', result.out)
        self.assertIn('Dosplex is running (', result.out)
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, None, False))
        self.assertEqual(world.events('trial.start')[0]['layout'], 'vertical')       # by hand: the two splits (ADR-013)
        # The one line at the end says how to start at boot; it is off after an install.
        self.assertEqual(result.out.splitlines()[-1],
                         'Dosplex starts when you ask for it. A start at every boot is experimental: sudo %s autostart on '
                         '--experimental' % (world.tree / 'opt/dosplex/bin/dosplex'))
        self.assertEqual(world.state().get('autostart'), None)
        # --no-start still installs only.
        other = self.world()
        result = other.install(other.bundle(), '--no-start')
        self.assertOk(result)
        self.assertIn('Not started: --no-start was given.', result.out)
        self.assertEqual(other.commands('systemd-run'), [])

    def test_install_needs_root(self):
        world = self.world()
        bundle = world.bundle()
        result = world.run('install', bundle.path, '--manifest', bundle.sha256, '--no-start', root=False)
        self.assertFailed(result, 'needs root')
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        self.assertEqual(world.calls(), [])


def add_guest(world, groups=(), mode=0o755, home=True, uid=1500):
    """A guest account that was there before Dosplex, in the fake sysroot."""
    with (world.tree / 'etc/passwd').open('a') as stream:
        stream.write('guest:x:%d:%d::/home/guest:/bin/bash\n' % (uid, uid))
    rows = [line.split(':') for line in (world.tree / 'etc/group').read_text().splitlines()]
    for name in groups:
        row = next((r for r in rows if r[0] == name), None)
        if row is None:
            rows.append([name, 'x', str(900 + len(rows)), 'guest'])
        else:
            row[3] = ','.join([m for m in row[3].split(',') if m] + ['guest'])
    rows.append(['guest', 'x', str(uid), ''])
    (world.tree / 'etc/group').write_text(''.join(':'.join(row) + '\n' for row in rows))
    if home:
        (world.tree / 'home/guest').mkdir(parents=True)
        (world.tree / 'home/guest/.bash_logout').write_text('')
        (world.tree / 'home/guest').chmod(mode)
    world.reset_baseline()


class GuestAccount(Base):
    def test_guest_is_created_with_a_plain_useradd_a_private_home_and_no_groups(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        (call,) = world.commands('useradd')
        self.assertEqual(call['argv'], ['--root', str(world.tree), '--create-home', '--user-group', '--shell',
                                        '/bin/bash', 'guest'])
        self.assertEqual(stat.S_IMODE((world.tree / 'home/guest').stat().st_mode), 0o700)
        passwd_before, group_before = world.initial_users
        passwd_after, group_after = world.users()
        self.assertEqual(passwd_after.splitlines()[:-1], passwd_before.splitlines())
        self.assertEqual(group_after.splitlines()[:-1], group_before.splitlines())
        self.assertRegex(group_after.splitlines()[-1], r'^guest:x:\d+:$')       # its own group, no members
        guest = world.state()['guest']
        self.assertEqual((guest['user'], guest['origin'], guest['home'], guest['home_mode_before']),
                         ('guest', 'created', '/home/guest', None))

    def test_an_existing_guest_is_adopted_and_not_created_again(self):
        world = self.world()
        add_guest(world, mode=0o755)
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertEqual(world.commands('useradd'), [])
        self.assertEqual(stat.S_IMODE((world.tree / 'home/guest').stat().st_mode), 0o700)
        guest = world.state()['guest']
        self.assertEqual((guest['origin'], guest['uid'], guest['home_mode_before']), ('adopted-foreign', 1500, 0o755))
        # A plain uninstall puts everything back exactly as it was, including the loose mode of the home.
        self.assertOk(world.run('uninstall'))
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)
        self.assertEqual(world.commands('userdel'), [])

    def test_a_guest_that_was_there_before_dosplex_is_never_removed(self):
        world = self.world()
        add_guest(world)
        self.assertOk(world.install(world.bundle(), '--no-start'))
        before = world.snapshot(exclude=())
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, 'will not remove it')
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertEqual(world.commands('userdel'), [])

    def test_a_guest_in_a_group_that_voids_isolation_is_refused(self):
        for group in ('input', 'audio', 'wheel', 'seat'):
            with self.subTest(group=group):
                world = self.world()
                add_guest(world, groups=(group,))
                result = world.install(world.bundle(), '--no-start')
                self.assertFailed(result, 'cannot be adopted', code=3)
                self.assertIn(group, result.err)
                self.assertNothingChanged(world)

    def test_a_home_without_an_account_is_not_taken_over(self):
        world = self.world()
        (world.tree / 'home/guest').mkdir(parents=True)
        world.initial_full = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'Dosplex will not take over', code=3)
        self.assertNothingChanged(world)

    def test_plain_uninstall_keeps_guest_and_a_reinstall_adopts_it(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        guest_line = world.users()[0].splitlines()[-1]
        self.assertOk(world.run('uninstall'))
        self.assertEqual(world.commands('userdel'), [])
        self.assertTrue((world.tree / 'home/guest').is_dir())
        self.assertEqual(world.users()[0].splitlines()[-1], guest_line)
        self.assertFalse((world.tree / 'opt/dosplex').exists())
        self.assertFalse((world.tree / 'var/lib/dosplex/state.json').exists())
        result = world.run('status')
        self.assertIn('Dosplex is not installed.', result.out)
        self.assertIn('Guest was kept', result.out)
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertEqual(len(world.commands('useradd')), 1)           # not created a second time
        self.assertEqual(world.state()['guest']['origin'], 'adopted-dosplex')
        # Dosplex made this account, so the owner may still ask for it to be removed.
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)

    def test_removing_guest_needs_the_exact_path_typed_in_a_terminal(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        home = world.tree / 'home/guest'
        (home / 'precious-save').write_text('save')
        self.assertFailed(world.run('uninstall', '--remove-guest'), 'needs a terminal')
        self.assertTrue(home.is_dir())
        for answer in ('yes\n', 'y\n', '/home/guest\n', '\n', str(home) + '/x\n'):
            result = world.run('uninstall', '--remove-guest', input=answer, DOSPLEX_TEST_TTY='1')
            self.assertFailed(result, 'Not confirmed')
            self.assertTrue((home / 'precious-save').is_file(), answer)
        self.assertEqual(world.commands('userdel'), [])
        self.assertTrue((world.tree / 'opt/dosplex/current').is_symlink())      # nothing was uninstalled either
        result = world.run('uninstall', '--remove-guest', input=str(home) + '\n', DOSPLEX_TEST_TTY='1')
        self.assertOk(result)
        self.assertIn(str(home), result.out)                  # the prompt printed the exact path
        self.assertFalse(home.exists())
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)

    def test_yes_only_goes_with_remove_guest_and_never_on_a_real_system(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertFailed(world.run('uninstall', '--yes'), '--yes only goes with --remove-guest', code=2)
        self.assertTrue((world.tree / 'opt/dosplex').exists())
        real = dosplex.Env('/')
        self.assertTrue(real.real)
        args = dosplex.build_parser().parse_args(['uninstall', '--remove-guest', '--yes'])
        with mock.patch.object(dosplex.Env, 'require_root'), self.assertRaises(dosplex.UsageError) as caught:
            dosplex.cmd_uninstall(real, args)
        self.assertIn('tests only', str(caught.exception))

    def test_guest_with_a_running_process_is_not_removed(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.guest_process(world)
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, 'running processes')
        self.assertEqual(world.commands('userdel'), [])
        self.assertTrue((world.tree / 'home/guest').is_dir())

    # --- Guest goes before the program does (a Guest that cannot be removed must leave `repair` in place) -------
    STILL_THERE = 'Guest still has running processes (steam).'
    LINGER = ("Guest's account is set to keep running after sign-out (linger), so its processes do not end. "
              "Switch that off with: sudo loginctl disable-linger guest   (then Guest can be removed).")
    FINISHES = 'To finish the uninstall now and keep Guest, run: sudo '

    def guest_process(self, world, pid='200', comm='steam'):
        uid = int(world.users()[0].splitlines()[-1].split(':')[2])
        (world.tree / 'proc' / pid).mkdir()
        (world.tree / 'proc' / pid / 'comm').write_text(comm + '\n')
        (world.tree / 'proc' / pid / 'status').write_text('Uid:\t%d\t%d\t%d\t%d\n' % ((uid,) * 4))

    def logins(self, world, value):
        (world.fake / 'logins.json').write_text(json.dumps({'guest': value}))

    def test_guest_is_removed_before_the_program_and_kept_after_it(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        log = world.root / 'faults.log'
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = log.read_text().splitlines()
        self.assertLess(points.index('uninstall:done:guest'), points.index('uninstall:intent:remove-cli'))
        self.assertLess(points.index('uninstall:done:guest'), points.index('uninstall:intent:remove-current'))
        self.assertLess(points.index('uninstall:done:remove-state'), points.index('uninstall:intent:guest'))
        # Kept (the default), Guest is still dealt with last, as before.
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        log = world.root / 'faults.log'
        self.assertOk(world.run('uninstall', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = log.read_text().splitlines()
        self.assertLess(points.index('uninstall:done:remove-current'), points.index('uninstall:intent:guest'))

    def test_a_guest_that_stays_leaves_the_program_in_place_and_names_what_repair_does(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.guest_process(world)
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, self.STILL_THERE)
        self.assertIn(self.FINISHES, result.err)
        self.assertTrue((world.tree / 'opt/dosplex/bin/dosplex').is_file())
        self.assertTrue((world.tree / 'opt/dosplex/current').is_symlink())
        self.assertTrue((world.tree / 'home/guest').is_dir())
        self.assertEqual(world.commands('userdel'), [])
        # While the transaction is open, uninstall refuses and names repair.
        self.assertFailed(world.run('uninstall'), 'repair')

    def test_guest_names_at_most_five_distinct_processes(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        for number, comm in enumerate(('steam', 'steam', 'a', 'b', 'c', 'd', 'e', 'f')):
            self.guest_process(world, str(200 + number), comm)
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, 'Guest still has running processes (a, b, c, d, e).')

    def test_linger_is_named_when_it_is_why_guest_stays(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.guest_process(world)
        self.logins(world, {'State': 'lingering', 'Linger': 'yes'})
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, self.LINGER)
        self.assertNotIn('Guest still has running processes', result.text)
        self.assertIn(self.FINISHES, result.err)
        self.assertTrue((world.tree / 'opt/dosplex/bin/dosplex').is_file())
        # The only logind calls are the read-only ones.
        for call in world.commands('loginctl'):
            self.assertEqual(call['argv'][0], 'show-user')

    def test_linger_off_but_a_login_still_there_says_so(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.logins(world, {'State': 'active', 'Linger': 'no'})
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, 'Guest still has a login session (active).')
        self.assertTrue((world.tree / 'opt/dosplex/bin/dosplex').is_file())

    def test_guest_processes_that_end_after_a_moment_are_waited_for(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.guest_process(world)
        self.logins(world, {'State': 'active', 'Linger': 'no', 'leaves_after': 2, 'pid': '200'})
        result = world.run('uninstall', '--remove-guest', '--yes', DOSPLEX_TEST_GUEST_SECONDS='15')
        self.assertOk(result)
        self.assertIn("Guest's account and home were removed: /home/guest", result.out)
        self.assertEqual(len(world.commands('userdel')), 1)
        self.assertFalse((world.tree / 'opt/dosplex').exists())
        self.assertFalse((world.tree / 'home/guest').exists())

    def test_repair_after_a_guest_that_stayed_finishes_the_uninstall_and_keeps_guest(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.guest_process(world)
        self.assertFailed(world.run('uninstall', '--remove-guest', '--yes'), self.STILL_THERE)
        result = world.run('repair')
        self.assertOk(result)
        self.assertIn('Dosplex is uninstalled.', result.out)
        self.assertIn('Guest was not removed: removing it needs a new confirmation.', result.out)
        self.assertIn("Guest's account and home were kept: /home/guest", result.out)
        self.assertIn('take the dosplex program out of a release archive', result.out)
        self.assertIn('sudo ./dosplex-VERSION/dosplex uninstall --remove-guest', result.out)
        self.assertNotIn('sudo dosplex uninstall', result.out)
        self.assertFalse((world.tree / 'opt/dosplex').exists())
        self.assertEqual(world.commands('userdel'), [])
        self.assertTrue((world.tree / 'home/guest').is_dir())
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
        # Guest is kept, so the record that Dosplex made it stays, and Guest can still be removed.
        self.assertTrue((world.tree / 'var/lib/dosplex/journal').is_dir())
        shutil.rmtree(world.tree / 'proc/200')
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertFalse((world.tree / 'home/guest').exists())
        self.assertFalse((world.tree / 'var/lib/dosplex').exists())


    # --- what the closing summary says is still on the machine ---------------------------------------------------
    LIST = "Steam's library still lists Dosplex."
    FOLDER = 'The shared library folder /home/dosplex-library stays.'
    SETTINGS = 'Two Steam settings that Dosplex switched on stay as they are'
    OWN_FOLDER_KEPT = "Dosplex's settings and its record that it created Guest stay in /var/lib/dosplex."
    PERF = 'Your performance recordings stay in /var/lib/dosplex/perf.'

    def test_the_closing_summary_has_only_the_lines_that_apply(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        for words in (self.LIST, self.FOLDER, self.SETTINGS, self.PERF, "Dosplex's settings"):
            self.assertNotIn(words, result.out)
        self.assertEqual(result.out.strip().splitlines(),
                         ['Uninstalling Dosplex.', 'Dosplex is uninstalled.',
                          "Guest's account and home were removed: /home/guest"])

    def test_the_closing_summary_names_each_thing_that_is_left(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('home/deck/.local/share/Steam/userdata/4242/config/shortcuts.vdf', 'x dosplex-start-client.py')
        (world.tree / 'home/dosplex-library').mkdir()
        world.write('home/deck/.local/share/dosplex/native-external-resolution', '1\n')
        result = world.run('uninstall')
        self.assertOk(result)
        self.assertIn("Steam's library still lists Dosplex. Remove it in Steam: Library, Non-Steam, Dosplex, Manage, "
                      "Remove non-Steam game from your library.", result.out)
        self.assertIn('The shared library folder /home/dosplex-library stays. It holds no games and you may delete it.',
                      result.out)
        self.assertIn('Two Steam settings that Dosplex switched on stay as they are: Maximum Game Resolution (Native) '
                      'and Enable Native Resolution on External Displays. Change them in Steam if you want.', result.out)
        self.assertIn("Guest's account and home were kept: /home/guest", result.out)
        self.assertIn('sudo ./dosplex-VERSION/dosplex uninstall --remove-guest', result.out)
        self.assertIn(self.OWN_FOLDER_KEPT + ' They go away when you remove Guest.', result.out)

    def test_a_marker_of_a_steam_account_is_enough_for_the_settings_line(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('home/deck/.local/share/dosplex/resolution-native-4242', '1\n')
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertIn(self.SETTINGS, result.out)
        self.assertNotIn(self.LIST, result.out)

    # --- /var/lib/dosplex ----------------------------------------------------------------------------------------
    def test_dosplexs_own_folder_goes_with_the_guest_it_made_and_stays_while_guest_is_kept(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertOk(world.run('uninstall'))
        self.assertTrue((world.tree / 'var/lib/dosplex/journal').is_dir())     # the record that Dosplex made Guest
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertFalse((world.tree / 'var/lib/dosplex').exists())
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_everything_in_it_goes_but_recordings_the_player_made(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        folder = world.tree / 'var/lib/dosplex'
        for name in ('audio.json', 'display-mode.json', 'backup/x', 'legacy-duplos/y', 'sides/a/b', 'screen/c', 'perf/run.csv'):
            world.write('var/lib/dosplex/' + name, 'data\n')
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['perf'])
        self.assertEqual((folder / 'perf/run.csv').read_text(), 'data\n')
        self.assertIn(self.PERF + ' The rest of that folder is removed.', result.out)
        # An empty perf/ holds nothing of the player's: it goes too.
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        (world.tree / 'var/lib/dosplex/perf').mkdir()
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertFalse((world.tree / 'var/lib/dosplex').exists())
        self.assertNotIn(self.PERF, result.out)

    def test_the_result_is_printed_although_the_journal_it_comes_from_is_removed(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertIn('Dosplex is uninstalled.', result.out)
        self.assertIn("Guest's account and home were removed: /home/guest", result.out)
        self.assertFalse((world.tree / 'var/lib/dosplex').exists())
        self.assertIn('Dosplex is not installed.', world.run('status').out)

    def test_repair_that_finishes_an_uninstall_of_a_removed_guest_clears_the_folder_too(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('var/lib/dosplex/perf/run.csv', 'data\n')
        crashed = world.run('uninstall', '--remove-guest', '--yes', DOSPLEX_TEST_CRASH_AT='uninstall:done:guest')
        self.assertFailed(crashed)
        result = world.run('repair')
        self.assertOk(result)
        self.assertEqual(sorted(p.name for p in (world.tree / 'var/lib/dosplex').iterdir()), ['perf'])
        self.assertIn(self.PERF, result.out)
        self.assertFalse((world.tree / 'opt/dosplex').exists())

    def test_a_folder_that_cannot_be_cleared_does_not_fail_the_uninstall(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('var/lib/dosplex/stuck/file', 'x\n')
        (world.tree / 'var/lib/dosplex/stuck').chmod(0o555)         # a file in it cannot be unlinked
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertIn('Dosplex is uninstalled.', result.out)
        self.assertIn('Could not remove /var/lib/dosplex', result.out)


class VersionGate(Base):
    def test_an_unknown_os_version_refuses_without_allow_untested(self):
        world = self.world(version_id='9.9.9', build_id='20990101.1')
        bundle = world.bundle()
        result = world.install(bundle, *world.controllers())
        self.assertFailed(result, 'not in the tested set', code=3)
        self.assertIn('SteamOS 9.9.9 build 20990101.1', result.err)
        self.assertIn('--allow-untested', result.err)
        self.assertIn('Nothing was changed', result.err)
        self.assertNothingChanged(world)
        # With the flag it goes ahead, and the state and status say it was an untested configuration.
        result = world.install(bundle, '--allow-untested', '--no-start')
        self.assertOk(result)
        self.assertIn('UNTESTED', result.out)
        self.assertEqual(world.state()['untested'], ['os'])
        self.assertIn('untested configuration', world.run('status').out)
        record = world.journal()['0001-install.jsonl'][0]
        self.assertEqual((record['allow_untested'], record['untested']), (True, ['os']))
        self.assertEqual(record['observed']['steamos'], {'version_id': '9.9.9', 'build_id': '20990101.1'})

    def test_each_version_of_the_tested_set_is_checked(self):
        cases = {
            'a known SteamOS version with another build': (dict(build_id='20260101.1'), 'os'),
            'gamescope': (dict(gamescope='3.16.99'), 'gamescope'),
            'the Steam client': (dict(steam='1700000000'), 'steam-client'),
        }
        for label, (options, check) in cases.items():
            with self.subTest(label):
                world = self.world(**options)
                bundle = world.bundle()
                result = world.install(bundle, '--no-start')
                self.assertFailed(result, 'not in the tested set', code=3)
                self.assertIn(check + ': ', result.err)
                self.assertNothingChanged(world)
                self.assertOk(world.install(bundle, '--no-start', '--allow-untested'))
                self.assertEqual(world.state()['untested'], [check])

    def test_a_steam_client_version_that_cannot_be_read_counts_as_untested(self):
        world = self.world()
        shutil.rmtree(world.tree / 'home/deck/.local')
        world.initial_full = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'no Steam client manifest found', code=3)
        self.assertNothingChanged(world)

    def test_a_listed_version_without_a_build_matches_any_build(self):
        world = self.world(build_id='20990101.1')
        bundle = world.bundle(tested_versions=tested(steamos=[{'version_id': VERSION_ID}]))
        self.assertOk(world.install(bundle, '--no-start'))
        self.assertEqual(world.state()['untested'], [])

    def test_an_empty_tested_set_refuses_every_version(self):
        world = self.world()
        bundle = world.bundle(tested_versions=tested(steamos=[], gamescope=[], steam_client=[]))
        result = world.install(bundle, '--no-start')
        self.assertFailed(result, 'not in the tested set', code=3)
        for check in ('os', 'gamescope', 'steam-client'):
            self.assertIn(check + ': ', result.err)

    def test_a_device_that_is_not_steamos_is_refused_even_with_allow_untested(self):
        world = self.world()
        world.write('etc/os-release', 'ID=arch\nVERSION_ID=3.9.2\nBUILD_ID=20260925.101\n')
        world.initial_full = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start', '--allow-untested')
        self.assertFailed(result, 'this is not SteamOS', code=3)
        self.assertNothingChanged(world)

    def test_state_problems_are_refused_even_with_allow_untested(self):
        def no_display(world):
            (world.tree / 'sys/class/drm/card0-HDMI-A-1/status').write_text('disconnected\n')

        def no_tool(world):
            (world.bin / 'bwrap').unlink()

        def no_uinput(world):
            (world.tree / 'dev/uinput').unlink()

        def no_main_user(world):
            world.write('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\n')
        for name, break_it, expected in (('display', no_display, 'no connected display'),
                                         ('tools', no_tool, 'missing: bwrap'), ('devices', no_uinput, '/dev/uinput'),
                                         ('main user', no_main_user, 'does not exist')):
            with self.subTest(name):
                world = self.world()
                break_it(world)
                world.initial_full = world.snapshot(exclude=())
                result = world.install(world.bundle(), '--no-start', '--allow-untested')
                self.assertFailed(result, expected, code=3)
                self.assertNothingChanged(world)

    def test_controller_slots_that_are_given_must_be_distinct_supported_receiver_slots(self):
        def other_receiver(world):
            # 1142 is the older Steam Controller dongle: a different protocol, not supported yet.
            (world.tree / 'sys/devices/usb/1-3/1-3:1.3/0003:28DE:1304.0003/uevent').write_text(
                'HID_ID=0003:000028DE:00001142\n')
        pair = ('--main-hid', '/dev/hidraw4', '--guest-hid', '/dev/hidraw5')
        for name, change, args, expected in (
                ('the older dongle', other_receiver, pair, '28de:1142); it is not supported yet'),
                ('the same slot twice', lambda w: None, ('--main-hid', '/dev/hidraw4', '--guest-hid', '/dev/hidraw4'),
                 'two distinct controller slots'),
                ('not a hidraw path', lambda w: None, ('--main-hid', '/dev/uinput', '--guest-hid', '/dev/hidraw5'),
                 'not a /dev/hidrawN path'),
                ('one slot that is not a receiver', lambda w: None, ('--guest-hid', '/dev/hidraw9'),
                 'cannot inspect /dev/hidraw9')):
            with self.subTest(name):
                world = self.world()
                change(world)
                world.initial_full = world.snapshot(exclude=())
                result = world.install(world.bundle(), '--no-start', *args)
                self.assertFailed(result, expected)
                self.assertNothingChanged(world)

    def test_either_slot_alone_is_fine_and_a_sleeping_controller_is_an_info_line(self):
        for args, expected in ((('--main-hid', '/dev/hidraw4'), '/dev/hidraw4 (Main)'),
                               (('--guest-hid', '/dev/hidraw5'), '/dev/hidraw5 (Guest)')):
            with self.subTest(args):
                world = self.world()
                result = world.install(world.bundle(), '--no-start', *args)
                self.assertOk(result)
                self.assertIn(expected, result.out)
        world = self.world()
        (world.tree / 'dev/hidraw5').write_bytes(b'')            # asleep: it sends no report
        result = world.install(world.bundle(), '--no-start', *world.controllers())
        self.assertOk(result)
        self.assertRegex(result.out, r'\[info\s*\] controller-input .*/dev/hidraw5: the controller is asleep')
        self.assertNotIn('/dev/hidraw4: the controller is asleep', result.out)
        self.assertNotRegex(result.out, r'\[FAIL')
        # Neither the preflight report nor a start listens for longer than it is told to, and a start never does.
        result = world.run('preflight', *world.controllers(), '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn('the controller is asleep', result.out)
        self.assertNotIn('the controller is asleep', world.run('preflight', *world.controllers(), '--sample-seconds', '0').out)

    def test_a_1305_puck_is_carried_like_a_1304_puck(self):
        world = self.world()
        usb = world.tree / 'sys/devices/usb/1-3'
        (usb / 'idProduct').write_text('1305')
        for number in (2, 3):
            (usb / ('1-3:1.%d' % number) / ('0003:28DE:1304.000%d' % number) / 'uevent').write_text(
                'HID_ID=0003:000028DE:00001305\n')
        world.initial_full = world.snapshot(exclude=())
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, *world.controllers(),
                           '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn('Result: ready', result.out)
        self.assertIn('/dev/hidraw4 (28de:1305)', world.run('preflight').out)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_preflight_command_reports_without_changing_anything(self):
        world = self.world()
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, *world.controllers(),
                           '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn('Result: ready', result.out)
        info = json.loads(world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--json').out)
        self.assertFalse(info['refused'])
        self.assertEqual(info['observed'], {'steamos': {'version_id': VERSION_ID, 'build_id': BUILD_ID},
                                            'gamescope': GAMESCOPE, 'steam_client': STEAM})
        listing = world.run('preflight')
        self.assertIn('/dev/hidraw4 (28de:1304)', listing.out)          # it lists the slots to choose from
        self.assertEqual(world.commands('useradd') + world.commands('systemd-run'), [])
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        refused = self.world(gamescope='3.16.99')
        bundle = refused.bundle()
        result = refused.run('preflight', bundle.path, '--manifest', bundle.sha256)
        self.assertFailed(result, 'Result: refused', code=3)
        self.assertOk(refused.run('preflight', bundle.path, '--manifest', bundle.sha256, '--allow-untested'))
        # Without a bundle and with nothing installed there is no tested set to compare with, and it says so.
        self.assertIn('no manifest', listing.out)


def rewrite_manifest(bundle, change):
    manifest = json.loads(bundle.manifest_bytes)
    change(manifest)
    raw = (json.dumps(manifest, indent=2) + '\n').encode()
    (bundle.path / 'manifest.json').write_bytes(raw)
    return sha256(raw)


class Tamper(Base):
    """Nothing from a bundle may run before the CLI has verified every file against the approved manifest."""

    def refused(self, world, bundle, text, digest=None):
        result = world.run('install', bundle.path, '--manifest', digest or bundle.sha256, *world.controllers())
        self.assertFailed(result, text)
        self.assertEqual(world.calls(), [], 'something ran before the bundle was verified')
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        return result

    def test_a_changed_file_is_refused_before_anything_runs(self):
        world = self.world()
        bundle = world.bundle()
        original = (bundle.path / 'libexec/dosplex-audio-router.py').read_bytes()
        (bundle.path / 'libexec/dosplex-audio-router.py').write_bytes(b'#' * len(original))     # same length
        self.refused(world, bundle, 'libexec/dosplex-audio-router.py differs from the approved manifest')

    def test_a_changed_tool_with_a_canary_never_runs(self):
        world = self.world()
        bundle = world.bundle()
        tool = bundle.path / 'tools/root-session-trial.py'
        tool.write_bytes(tool.read_bytes() + b"\nopen(%r, 'w').write('ran')\n" % str(world.root / 'canary'))
        self.refused(world, bundle, 'differs from the approved manifest')
        self.assertFalse((world.root / 'canary').exists())

    def test_a_changed_manifest_is_refused(self):
        world = self.world()
        bundle = world.bundle()
        manifest = json.loads(bundle.manifest_bytes)
        manifest['tested_versions']['gamescope'].append('9.9.9')           # widen the tested set
        (bundle.path / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        self.refused(world, bundle, 'manifest.json does not match the approved SHA-256')

    def test_a_wrong_or_malformed_digest_is_refused(self):
        world = self.world()
        bundle = world.bundle()
        self.refused(world, bundle, 'does not match the approved SHA-256', digest='0' * 64)
        self.refused(world, bundle, 'must be 64 lowercase hex digits', digest=bundle.sha256.upper())
        self.refused(world, bundle, 'must be 64 lowercase hex digits', digest=bundle.sha256[:40])

    def test_files_the_manifest_does_not_list_are_refused(self):
        world = self.world()
        bundle = world.bundle()
        (bundle.path / 'tools/extra.py').write_text('print(1)\n')
        self.refused(world, bundle, '1 entry is not in the manifest: tools/extra.py')
        (bundle.path / 'tools/extra.py').unlink()
        (bundle.path / 'tools/empty-dir').mkdir()
        self.refused(world, bundle, 'tools/empty-dir/')

    def test_symlinks_hard_links_and_missing_files_are_refused(self):
        world = self.world()
        bundle = world.bundle()
        target = bundle.path / 'libexec/native-hid-relay'
        data = target.read_bytes()
        target.unlink()
        (world.root / 'outside').write_bytes(data)
        target.symlink_to(world.root / 'outside')
        self.refused(world, bundle, 'libexec/native-hid-relay is or sits behind a symlink')
        target.unlink()
        os.link(world.root / 'outside', target)
        self.refused(world, bundle, 'libexec/native-hid-relay is not a plain file')
        target.unlink()
        self.refused(world, bundle, 'libexec/native-hid-relay is missing')
        target.write_bytes(data)
        target.chmod(0o755)
        shutil.move(bundle.path / 'lib', world.root / 'lib-moved')
        (bundle.path / 'lib').symlink_to(world.root / 'lib-moved')
        self.refused(world, bundle, 'lib/libfake.so.1 is or sits behind a symlink')

    def test_a_manifest_that_asks_for_something_unsafe_is_refused(self):
        def files(change):
            return lambda manifest: change(manifest['files'])
        cases = {
            'a setuid mode': (files(lambda f: f['LICENSE'].update(mode=0o4755)), 'invalid manifest metadata'),
            'a path that leaves the bundle': (files(lambda f: f.update({'../outside': f['LICENSE']})),
                                              "invalid bundle path '../outside'"),
            'an absolute path': (files(lambda f: f.update({'/etc/passwd': f['LICENSE']})),
                                 "invalid bundle path '/etc/passwd'"),
            'a dotted path': (files(lambda f: f.update({'a/./b': f['LICENSE']})), "invalid bundle path 'a/./b'"),
            'the manifest itself': (files(lambda f: f.update({'manifest.json': f['LICENSE']})),
                                    'invalid manifest metadata'),
            'a negative size': (files(lambda f: f['LICENSE'].update(bytes=-1)), 'invalid manifest metadata'),
            'no files at all': (lambda m: m.update(files={}), 'invalid manifest file set'),
        }
        for label, (change, expected) in cases.items():
            with self.subTest(label):
                world = self.world()
                bundle = world.bundle()
                digest = rewrite_manifest(bundle, change)
                self.refused(world, bundle, expected, digest=digest)

    def test_a_bundle_without_the_new_version_fields_is_refused(self):
        for label, change, expected in (
                ('no version', lambda m: m.pop('version'), 'no valid "version"'),
                ('a version that is a path', lambda m: m.update(version='../escape'), 'no valid "version"'),
                ('no tested_versions', lambda m: m.pop('tested_versions'), 'no tested_versions object'),
                ('no commit', lambda m: m.pop('commit'), 'no valid "commit"'),
                ('a damaged tested_versions', lambda m: m['tested_versions'].update(steamos=[{'build_id': 'x'}]),
                 'tested_versions.steamos entries need a version_id')):
            with self.subTest(label):
                world = self.world()
                bundle = world.bundle()
                digest = rewrite_manifest(bundle, change)
                self.refused(world, bundle, expected, digest=digest)

    def test_a_bundle_that_lacks_a_file_start_needs_is_refused_up_front(self):
        world = self.world()
        bundle = world.bundle()

        def drop(manifest):
            del manifest['files']['tools/start-external-native-trial.py']
        digest = rewrite_manifest(bundle, drop)
        (bundle.path / 'tools/start-external-native-trial.py').unlink()
        self.refused(world, bundle, 'lacks files that start needs: tools/start-external-native-trial.py', digest=digest)

    def test_a_bundle_without_the_receiver_helper_says_why_and_what_start_enrolls(self):
        world = self.world()
        bundle = world.bundle()

        def drop(manifest):
            del manifest['files']['libexec/native-hid-relay']
        digest = rewrite_manifest(bundle, drop)
        (bundle.path / 'libexec/native-hid-relay').unlink()
        result = self.refused(world, bundle, 'the bundle lacks files that start needs: libexec/native-hid-relay.',
                              digest=digest)
        for text in ('built for a device with built-in controls', 'enrolls every receiver that is plugged in',
                     'Nothing was changed'):
            self.assertIn(text, result.err)
        # The read-only check says the same, without changing anything either.
        result = world.run('preflight', bundle.path, '--manifest', digest)
        self.assertFailed(result, 'enrolls every receiver that is plugged in', code=3)

    def test_a_valid_bundle_runs_nothing_from_itself_during_install(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        self.assertEqual(world.events(), [])

    def test_a_tampered_installed_release_is_refused_by_start(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        release = world.tree / 'opt/dosplex/releases/0.1.0-test'
        tool = release / 'tools/root-session-trial.py'
        original = tool.read_bytes()
        tool.chmod(0o755)
        tool.write_bytes(original + b'\n# changed after install\n')
        result = world.run('start')
        self.assertFailed(result, 'tools/root-session-trial.py differs from the approved manifest')
        self.assertEqual(world.commands('systemd-run'), [])
        self.assertEqual(world.events(), [])
        # An unexpected extra file (for example byte-code written into the release) is refused as well.
        tool.write_bytes(original)
        (release / 'tools/__pycache__').mkdir()
        self.assertFailed(world.run('start'), 'tools/__pycache__/')
        self.assertEqual(world.events(), [])


class JournalTrust(Base):
    def prepare(self, world):
        (world.tree / 'var/lib/dosplex').mkdir(mode=0o755)
        world.initial_full = world.snapshot(exclude=())

    def refused_everywhere(self, world, text):
        bundle = world.bundle()
        for command in (('install', bundle.path, '--manifest', bundle.sha256, '--no-start'),
                        ('status',), ('repair',), ('start',), ('stop',), ('uninstall',)):
            result = world.run(*command)
            self.assertFailed(result, text)
            self.assertEqual(world.commands('useradd') + world.commands('systemd-run'), [], command)
        self.assertFalse((world.tree / 'opt/dosplex').exists())

    def test_a_symlinked_journal_directory_is_refused(self):
        world = self.world()
        bundle = world.bundle()
        self.assertOk(world.install(bundle, *world.controllers(), '--no-start'))
        elsewhere = world.root / 'elsewhere'
        shutil.move(world.journal_dir(), elsewhere)
        world.journal_dir().symlink_to(elsewhere)
        before = sorted(p.name for p in elsewhere.iterdir())
        calls = len(world.calls())
        for command in (('install', bundle.path, '--manifest', bundle.sha256, '--no-start'), ('status',), ('repair',),
                        ('start',), ('stop',), ('uninstall',)):
            result = world.run(*command)
            self.assertFailed(result, 'is a symlink; refusing to use it')
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), before)        # nothing written through it
        self.assertEqual(len(world.calls()), calls)                                  # and nothing was run
        self.assertTrue((world.tree / 'opt/dosplex/current').is_symlink())

    def test_a_symlinked_state_directory_is_refused_by_every_command(self):
        world = self.world()
        elsewhere = world.root / 'elsewhere'
        elsewhere.mkdir(mode=0o755)
        (world.tree / 'var/lib/dosplex').symlink_to(elsewhere)
        world.initial_full = world.snapshot(exclude=())
        bundle = world.bundle()
        for command in (('install', bundle.path, '--manifest', bundle.sha256, '--no-start'), ('status',), ('repair',),
                        ('start',), ('stop',), ('uninstall',), ('version',)):
            result = world.run(*command)
            if command == ('version',):
                self.assertFailed(result, 'is a symlink; refusing to use it')
                continue
            self.assertFailed(result, 'is a symlink; refusing to use it')
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(world.calls(), [])
        self.assertFalse((world.tree / 'opt/dosplex').exists())

    def test_a_journal_directory_that_is_open_to_others_is_refused(self):
        for mode in (0o755, 0o770, 0o707, 0o500):
            with self.subTest(mode=oct(mode)):
                world = self.world()
                self.assertOk(world.install(world.bundle(), '--no-start'))
                world.journal_dir().chmod(mode)
                result = world.run('status')
                self.assertFailed(result, 'must be 0700')

    def test_a_journal_that_root_does_not_own_is_refused(self):
        world = self.world()
        self.prepare(world)
        env = dosplex.Env(world.tree)
        env.trusted_uid = UID + 1                      # as if the directories belonged to another user
        with self.assertRaises(dosplex.JournalError) as caught:
            dosplex.Journal.open(env)
        self.assertIn('not a directory that only root can change', str(caught.exception))
        env.trusted_uid = UID
        (world.tree / 'var/lib/dosplex/journal').mkdir(mode=0o700)
        env.trusted_uid = UID + 1
        with self.assertRaises(dosplex.JournalError) as caught:
            dosplex._open_checked_dir(env, env.var, 0o755, create=False)
        self.assertIn('not owned by root', str(caught.exception))
        with self.assertRaises(dosplex.JournalError):
            dosplex._open_checked_dir(env, env.var / 'journal', 0o700, create=False, exact_mode=True)

    def test_a_journal_file_that_is_a_symlink_or_open_to_others_is_refused(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        record = world.journal_dir() / '0001-install.jsonl'
        record.chmod(0o644)
        self.assertFailed(world.run('status'), 'not a private root-owned file')
        record.chmod(0o600)
        self.assertOk(world.run('status'))
        moved = world.root / 'moved.jsonl'
        record.rename(moved)
        record.symlink_to(moved)
        self.assertFailed(world.run('status'), 'cannot open journal file')

    def test_a_damaged_record_stops_everything_but_a_torn_last_record_is_dropped(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        record = world.journal_dir() / '0001-install.jsonl'
        whole = record.read_bytes()
        record.write_bytes(whole + b'{"kind":"intent","step":"x","da')        # a record torn by a crash
        self.assertOk(world.run('status'))
        lines = whole.split(b'\n')
        record.write_bytes(b'\n'.join([lines[0], b'not json'] + lines[1:]))
        result = world.run('status')
        self.assertFailed(result, 'damaged record at line 2')

    def test_two_operations_cannot_run_at_once(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        import fcntl
        fd = os.open(world.journal_dir() / '.lock', os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertFailed(world.run('uninstall'), 'another dosplex operation is running')
        self.assertFailed(world.run('start', *world.controllers()), 'another dosplex operation is running')
        self.assertTrue((world.tree / 'opt/dosplex/current').is_symlink())
        self.assertEqual(world.events(), [])

    def _open_transaction(self, world):
        (world.tree / 'var/lib/dosplex/journal').mkdir(parents=True, exist_ok=True)
        path = world.tree / 'var/lib/dosplex/journal/0001-start.jsonl'
        path.write_text('{"kind":"intent","step":"a"}\n')
        os.chown(path, UID, -1)
        os.chmod(path, 0o600)

    def test_status_shows_in_progress_while_the_lock_is_held(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self._open_transaction(world)
        import fcntl
        fd = os.open(world.journal_dir() / '.lock', os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertOk(r := world.run('status'))
        self.assertIn('In progress: ', r.text)
        self.assertNotIn('Interrupted: ', r.text)
        self.assertNotIn('dosplex repair', r.text)
        self.assertOk(r := world.run('status', '--json'))
        info = json.loads(r.out)
        self.assertIn('in_progress', info)
        self.assertEqual(info['in_progress']['op'], 'start')
        self.assertIsNone(info['interrupted'])

    def test_the_lock_is_read_from_the_kernels_list_and_never_tried(self):
        # A status that tried the lock would hold it for a moment, and a start that begins in that moment would be
        # told that another operation is running.
        with tempfile.TemporaryDirectory() as name:
            directory = os.open(name, os.O_RDONLY)
            self.addCleanup(os.close, directory)
            listing = Path(name) / 'locks'
            self.assertFalse(dosplex.journal_lock_held(directory, str(listing)))       # no lock file, no listing
            (Path(name) / '.lock').write_text('')
            info = os.stat(Path(name) / '.lock')
            mine = '%02x:%02x:%d' % (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
            other = '%02x:%02x:%d' % (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino + 1)
            listing.write_text('1: POSIX  ADVISORY  WRITE 100 %s 0 EOF\n2: FLOCK  ADVISORY  WRITE 200 %s 0 EOF\n'
                               '2: -> FLOCK  ADVISORY  WRITE 300 %s 0 EOF\nnonsense\n' % (mine, other, mine))
            self.assertFalse(dosplex.journal_lock_held(directory, str(listing)))       # another kind, another file, a waiter
            listing.write_text('1: FLOCK  ADVISORY  WRITE 200 %s 0 EOF\n' % mine)
            self.assertTrue(dosplex.journal_lock_held(directory, str(listing)))
            self.assertFalse(dosplex.journal_lock_held(directory, str(Path(name) / 'absent')))
            source = inspect.getsource(dosplex.journal_lock_held)
            self.assertNotIn('flock', source.split('"""')[2])

    def test_status_shows_interrupted_when_the_lock_is_free(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self._open_transaction(world)
        self.assertOk(r := world.run('status'))
        self.assertIn('Interrupted: ', r.text)
        self.assertIn('dosplex repair', r.text)
        self.assertOk(r := world.run('status', '--json'))
        info = json.loads(r.out)
        self.assertIn('interrupted', info)
        self.assertEqual(info['interrupted']['op'], 'start')
        self.assertIsNone(info.get('in_progress'))

    def test_status_shows_interrupted_when_there_is_no_lock_file(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self._open_transaction(world)
        (world.tree / 'var/lib/dosplex/journal/.lock').unlink()
        self.assertOk(r := world.run('status'))
        self.assertIn('Interrupted: ', r.text)
        self.assertIn('dosplex repair', r.text)
        self.assertOk(r := world.run('status', '--json'))
        info = json.loads(r.out)
        self.assertIn('interrupted', info)
        self.assertIsNone(info.get('in_progress'))


class Policy(Base):
    def full_cycle(self, world):
        bundle = world.bundle()
        self.assertOk(world.install(bundle, *world.controllers()))
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))

    def test_nothing_under_usr_is_written_or_changed(self):
        world = self.world()
        before = {k: v for k, v in world.snapshot(exclude=()).items() if k == 'usr' or k.startswith('usr/')}
        self.assertTrue(before)
        self.full_cycle(world)
        after = {k: v for k, v in world.snapshot(exclude=()).items() if k == 'usr' or k.startswith('usr/')}
        self.assertEqual(after, before)
        # The fake usr is read-only (0555/0444), so a write there would also have failed loudly.
        self.assertEqual(stat.S_IMODE((world.tree / 'usr').stat().st_mode), 0o555)

    def test_the_write_guard_refuses_usr_and_everything_outside_the_layout(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        allowed = [env.opt / 'releases/x', env.var / 'state.json', env.opt, env.var / 'journal/0001-install.jsonl']
        for path in allowed:
            self.assertEqual(env.check_write(path), path)
        for path in (world.tree / 'usr/bin/dosplex', world.tree / 'usr', world.tree / 'usr/lib/x/../y',
                     world.tree / 'etc/passwd', world.tree / 'etc/systemd/system/display-manager.service',
                     world.tree / 'opt/other', world.tree / 'var/lib/other', world.tree / 'boot/x', Path('/usr/bin/dosplex'),
                     Path('/usr/local/bin/dosplex'), world.tree / 'opt/dosplex/../../usr/x'):
            with self.assertRaises(dosplex.PolicyError, msg=str(path)):
                env.check_write(path)
        # A symlink inside the layout must not lead the guard out of it.
        (world.tree / 'opt/dosplex').mkdir()
        (world.tree / 'opt/dosplex/releases').symlink_to(world.tree / 'usr/lib')
        with self.assertRaises(dosplex.PolicyError):
            env.check_write(env.opt / 'releases/x')

    def test_a_sysroot_under_usr_is_refused(self):
        for path in ('/usr', '/usr/share', '/usr/lib/../lib'):
            with self.assertRaises(dosplex.PolicyError, msg=path):
                dosplex.Env(path)
        result = subprocess.run([sys.executable, str(CLI), '--sysroot', '/usr/share', 'status'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing under /usr', result.stderr)

    def test_a_symlinked_opt_directory_is_refused_before_it_is_followed(self):
        world = self.world()
        (world.tree / 'opt/dosplex').symlink_to(world.tree / 'usr/lib')
        world.initial_full = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'must be a real directory')
        self.assertEqual(sorted(p.name for p in (world.tree / 'usr/lib').iterdir()), ['os-release-sentinel'])

    def test_forbidden_commands_are_refused_by_the_runner(self):
        env = dosplex.Env('/tmp')
        sysroot = str(env.sysroot)
        forbidden = [
            ['pacman', '-S', 'something'], ['steamos-readonly', 'disable'], ['steamos-devmode', 'enable'],
            ['usermod', '-aG', 'input', 'guest'], ['gpasswd', '-a', 'guest', 'audio'], ['groupadd', 'x'],
            ['gpasswd', '-d', 'guest', 'wheel'], ['gpasswd', '-d', 'deck', 'audio'], ['gpasswd', '-a', 'deck', 'audio'],
            ['gpasswd', '-d', 'guest', 'audio', 'input'], ['gpasswd', '-A', 'guest', 'audio'], ['gpasswd', 'audio'],
            ['gpasswd', '--root', '/elsewhere', '-d', 'guest', 'audio'], ['id', 'deck'], ['id', '-nG', 'deck'],
            ['sudo', 'true'], ['useradd', '--root', sysroot, '-G', 'input', '--create-home', '--user-group',
                               '--shell', '/bin/bash', 'guest'],
            ['useradd', '--root', sysroot, '--create-home', '--user-group', '--groups', 'wheel', 'guest'],
            ['useradd', '--root', sysroot, '--create-home', '--user-group', '--shell', '/bin/bash', 'other'],
            ['userdel', '--root', sysroot, '--remove', 'guest'], ['userdel', '--root', sysroot, 'deck'],
            ['systemctl', 'enable', 'dosplex.service'], ['systemctl', 'disable', 'display-manager.service'],
            ['systemctl', 'mask', 'sddm.service'], ['systemctl', 'stop', 'display-manager.service'],
            ['systemctl', 'stop', 'sddm.service'], ['systemctl', 'restart', 'dosplex-x.service'],
            ['systemctl', 'daemon-reload', '--force'], ['systemctl', 'set-default', 'multi-user.target'],
            ['systemctl', 'show', 'display-manager.service'],
            ['rm', '-rf', '/usr'], ['chmod', '-R', '777', '/'], ['setfacl', '-m', 'u:guest:rw', '/dev/uinput'],
            ['runuser', '-u', 'deck', '--', 'sh', '-c', 'true'],
            ['runuser', '-u', 'deck', '--', 'env', 'A=B', 'steamos-readonly', 'disable'],
            ['runuser', '-u', 'deck', '--', 'steamosctl', 'set-default-login-mode', 'desktop'],
            ['gamescope', '--steam'], ['loginctl', 'terminate-user', 'guest'],
            ['busctl', '--system', 'call', 'org.bluez', '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'],
            ['busctl', '--user', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ReloadConfig'],
            ['busctl', 'tree'],
            ['systemd-run', '--unit=other', '--wait', '--', 'true'], ['systemd-run', '--unit=dosplex-x', '--', 'true'],
            [sys.executable, '-c', 'import os'], [sys.executable, '/usr/bin/anything'],
            # The session's helper is run for `close` only, and only the one of a session under /run.
            [sys.executable, sysroot + '/run/dosplex-trial-' + 'a' * 32 + '/bundle/libexec/dosplex-power-action', 'poweroff'],
            [sys.executable, sysroot + '/run/dosplex-trial-' + 'a' * 32 + '/bundle/libexec/dosplex-power-action', 'stop'],
            [sys.executable, sysroot + '/tmp/dosplex-trial-' + 'a' * 32 + '/bundle/libexec/dosplex-power-action', 'close'],
            [sys.executable, sysroot + '/run/elsewhere/bundle/libexec/dosplex-power-action', 'close'],
        ]
        for argv in forbidden:
            with self.subTest(argv=argv), self.assertRaises(dosplex.PolicyError):
                dosplex.check_command(env, argv)

    def test_the_commands_the_program_uses_pass_the_runner(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        account = dosplex.user_entry(env, 'deck')
        sysroot = str(env.sysroot)
        trial = world.tree / 'run' / ('dosplex-trial-' + 'a' * 32)
        used = [
            ['useradd', '--root', sysroot, '--create-home', '--user-group', '--shell', '/bin/bash', 'guest'],
            ['userdel', '--root', sysroot, 'guest'], ['gamescope', '--version'],
            ['systemctl', 'is-active', 'dosplex-compositor-x.service'], ['systemctl', 'stop', 'dosplex-x.service'],
            ['systemctl', 'daemon-reload'],
            ['loginctl', 'show-user', 'guest', '--property=State', '--value'],
            dosplex.user_command(account, 'steamosctl', 'switch-to-game-mode'),
            dosplex.user_command(account, 'steamosctl', 'get-default-login-mode'),
            ['runuser', '-u', 'deck', '--', 'env', 'DISPLAY=:0', 'xprop', '-root', '-f', 'GAMESCOPE_DISPLAY_MODE_NUDGE',
             '32c', '-set', 'GAMESCOPE_DISPLAY_MODE_NUDGE', '1'],
            ['systemd-run', '--quiet', '--collect', '--wait', '--unit=dosplex-start-1', '--', env.python,
             str(env.opt / 'bin' / 'dosplex'), '--sysroot', sysroot, '_worker', 'start', '{}'],
            [env.python, str(trial / 'root-session-trial.py'), 'recover', '--root', str(trial)],
            [env.python, str(trial / 'bundle/libexec/dosplex-power-action'), 'close', '--trial-root', str(trial)],
        ]
        for argv in used:
            with self.subTest(argv=argv):
                dosplex.check_command(env, argv)

    def test_no_group_is_granted_and_no_unit_or_display_manager_file_is_touched(self):
        world = self.world()
        units_before = world.snapshot(exclude=())
        bundle = world.bundle()
        self.assertOk(world.install(bundle, *world.controllers()))
        _, group = world.users()
        before = world.initial_users[1].splitlines()
        self.assertEqual(group.splitlines()[:len(before)], before)           # no existing group line changed
        self.assertEqual(len(group.splitlines()), len(before) + 1)           # only Guest's own group was added
        self.assertNotRegex(group, r'(input|audio|seat)[^\n]*guest')
        self.assertRegex(group, r'(?m)^wheel:x:998:deck$')
        unit_files = {k: v for k, v in world.snapshot(exclude=()).items() if k.startswith('etc/systemd')}
        self.assertEqual(unit_files, {k: v for k, v in units_before.items() if k.startswith('etc/systemd')})
        self.assertEqual(world.state()['guest']['origin'], 'created')
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.users(), world.initial_users)

    def test_only_the_expected_commands_ran_during_a_full_cycle(self):
        world = self.world()
        self.full_cycle(world)
        allowed = {'useradd', 'userdel', 'systemd-run', 'runuser', 'steamosctl', 'gamescope', 'systemctl', 'loginctl',
                   'gamescopectl', 'xprop', 'xwininfo'}
        seen = {c['cmd'] for c in world.commands()}
        self.assertLessEqual(seen, allowed)
        for call in world.commands('systemctl'):
            self.assertIn(call['argv'][0], ('is-active', 'show', 'stop'), call)
            if call['argv'][0] == 'stop':
                self.assertTrue(call['argv'][1].startswith('dosplex-'), call)
        for call in world.commands('steamosctl'):
            self.assertIn(call['argv'][0], ('get-default-login-mode', 'get-default-desktop-session',
                                            'switch-to-game-mode', 'switch-to-desktop-mode'))
        for call in world.commands('useradd') + world.commands('userdel'):
            self.assertNotIn('-G', call['argv'])
            self.assertNotIn('--remove', call['argv'])


class Session(Base):
    def started(self, **options):
        world = self.world(**options)
        bundle = world.bundle()
        self.assertOk(world.install(bundle, *world.controllers()))
        return world, bundle

    def trial_roots(self, world):
        return sorted((world.tree / 'run').glob('dosplex-trial-*'))

    def test_install_starts_the_retained_trial_the_way_the_human_test_installer_does(self):
        world, bundle = self.started()
        release = world.tree / 'opt/dosplex/releases/0.1.0-test'
        (trial,) = world.events('trial.start')
        self.assertEqual((trial['bundle'], trial['manifest'], trial['seconds'], trial['main_user'],
                          trial['guest_user'], trial['device_view'], trial['keep_session']),
                         (str(release), bundle.sha256, 120, 'deck', 'guest', True, True))
        (root,) = self.trial_roots(world)
        (external,) = world.events('external.run')
        self.assertEqual((external['root'], external['main'], external['guest']),
                         (str(root), '/dev/hidraw4', '/dev/hidraw5'))
        # The seeds were imported from the installed release, never from the bundle directory.
        for event in world.events('seed-imported'):
            self.assertTrue(event['file'].startswith(str(release / 'tools')), event)
        self.assertEqual([c['argv'] for c in world.commands('steamosctl') if c['argv'][0].startswith('switch')],
                         [['switch-to-game-mode']])
        # It ran as a transient system service that is waited for, from the installed program.
        (service,) = world.commands('systemd-run')
        argv = service['argv']
        self.assertIn('--wait', argv)
        self.assertTrue(any(a.startswith('--unit=dosplex-start-') for a in argv))
        after = argv[argv.index('--') + 1:]
        self.assertEqual(after[1:5], [str(world.tree / 'opt/dosplex/bin/dosplex'), '--sysroot', str(world.tree), '_worker'])
        state = world.state()
        self.assertEqual(state['trial']['root'], str(root))
        self.assertEqual(state['session'], {'original': 'desktop', 'desktop_session': 'plasma.desktop', 'switched': True})
        self.assertEqual(state['controllers'], {'main': '/dev/hidraw4', 'guest': '/dev/hidraw5'})
        self.assertTrue(world.units())
        steps = [r['step'] for r in world.journal()['0002-start.jsonl'] if r['kind'] == 'intent']
        self.assertEqual(steps, ['verify-release', 'preflight', 'host-mode', 'idle-steam', 'trial', 'controllers', 'running'])

    def test_the_other_receiver_slots_are_reported_and_journaled_with_the_first_two(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--sample-seconds', '0.3', FAKE_UNASSIGNED='/dev/hidraw2,/dev/hidraw3,/dev/hidraw6')
        self.assertOk(result)
        self.assertIn("Further controller slots: /dev/hidraw2, /dev/hidraw3, /dev/hidraw6. A controller you wake there is "
                      "Main's until a player moves it in Devices.", result.out)
        records = world.journal()['0002-start.jsonl']
        (intent,) = [r for r in records if r['kind'] == 'intent' and r['step'] == 'controllers']
        (done,) = [r for r in records if r['kind'] == 'done' and r['step'] == 'controllers']
        # One journaled step covers Main, Guest and the rest: the same rollback, the same recovery.
        self.assertEqual((intent['data']['main'], intent['data']['guest']), ('/dev/hidraw4', '/dev/hidraw5'))
        self.assertEqual(done['data'], {'unassigned': ['/dev/hidraw2', '/dev/hidraw3', '/dev/hidraw6']})
        self.assertIn('controllers', dosplex.Session.MUTATING)

    def test_a_receiver_without_other_slots_starts_exactly_as_before(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertNotIn('Further controller slots', result.out)
        records = world.journal()['0002-start.jsonl']
        (done,) = [r for r in records if r['kind'] == 'done' and r['step'] == 'controllers']
        self.assertEqual(done.get('data', {}), {})

    def test_the_outcome_reaches_the_person_although_the_background_service_prints_elsewhere(self):
        world = self.world()
        result = world.install(world.bundle(), *world.controllers())
        self.assertOk(result)
        self.assertIn('Switching to Gaming mode', result.out)
        self.assertIn('Dosplex is running (', result.out)
        self.assertIn('Dosplex is running (', (world.fake / 'worker.log').read_text())        # what a journal holds
        result = world.run('stop')
        self.assertIn('Dosplex stopped; Steam is back to normal (Desktop mode restored).', result.out)
        result = world.run('uninstall')
        self.assertIn('Dosplex is uninstalled.', result.out)
        self.assertIn("Guest's account and home were kept: /home/guest", result.out)
        self.assertIn('dosplex uninstall --remove-guest', result.out)
        # A failure inside the service reaches the person through the journal as well.
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--sample-seconds', '0.3', FAKE_TRIAL_FAIL='before-root')
        self.assertFailed(result, 'fake trial failed before it created anything')
        self.assertIn('Returning to stock Steam', result.out)

    def test_start_twice_does_not_start_a_second_compositor(self):
        world, _ = self.started()
        units = world.units()
        result = world.run('start')
        self.assertOk(result)
        self.assertIn('already running', result.out)
        self.assertEqual(len(world.events('trial.start')), 1)
        self.assertEqual(world.units(), units)

    def test_status_reports_version_manifest_trial_both_sides_and_guests_audio_endpoint(self):
        world, bundle = self.started()
        result = world.run('status')
        self.assertOk(result)
        for expected in ('Dosplex 0.1.0-test', bundle.sha256, 'Running: yes', 'Main  (left, deck): lifecycle running, restarts 0',
                         'Guest (right, guest): lifecycle running, restarts 1', 'created by Dosplex',
                         'Guest audio endpoint: unix:' + str(self.trial_roots(world)[0]) + '/audio-guest/pulse/native (present)'):
            self.assertIn(expected, result.out)
        info = json.loads(world.run('status', '--json').out)
        self.assertEqual((info['installed'], info['version'], info['manifest_sha256'], info['previous']),
                         (True, '0.1.0-test', bundle.sha256, None))
        self.assertTrue(info['trial']['alive'] and info['trial']['compositor_active'])
        self.assertEqual(info['trial']['sides']['Guest'], {'side': 'right', 'owner': 'guest', 'lifecycle': 'running',
                                                            'restart_count': 1, 'controller_tour_accounts': []})
        self.assertNotIn('Steam Controller detected', result.out)
        self.assertEqual(info['trial']['sides']['Main']['restart_count'], 0)
        self.assertEqual(info['trial']['guest_audio_endpoint']['present'], True)

    def test_status_says_whether_the_sleep_guard_runs(self):
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        guard = json.loads((root / 'transaction.json').read_text())['sleep_guard_unit']
        result = world.run('status')
        self.assertOk(result)
        self.assertIn('Sleep guard: active (freezes both sides before the machine sleeps)', result.out)
        self.assertNotIn('sleep guard is not active', result.out)
        self.assertTrue(json.loads(world.run('status', '--json').out)['trial']['sleep_guard_active'])
        (world.fake / 'units' / guard).unlink()
        result = world.run('status')
        self.assertIn('Warning: the sleep guard is not active. Sleep may fail until Dosplex is started again.', result.out)
        self.assertFalse(json.loads(world.run('status', '--json').out)['trial']['sleep_guard_active'])

    def test_status_tells_when_a_side_supervisor_found_steams_controller_tour_on(self):
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        path = root / 'sides' / 'right' / 'session.json'
        session = json.loads(path.read_text())
        # The supervisor writes the account ids it found; anything else in the field is not shown.
        path.write_text(json.dumps(dict(session, controller_tour_accounts=['190558236', 'x; rm', 7])))
        result = world.run('status')
        self.assertOk(result)
        self.assertIn('Steam shows its "Steam Controller detected!" tour for account 190558236 and it cannot finish inside '
                      'Dosplex.', result.out)
        self.assertIn('Settings > Developer > Show Steam Controller Tour', result.out)
        self.assertIn('"ShowIbexWelcomeTour" "0"', result.out)
        self.assertNotIn('rm', result.out.split('account ')[1].split(' and')[0])
        self.assertEqual(json.loads(world.run('status', '--json').out)['trial']['sides']['Guest']['controller_tour_accounts'],
                         ['190558236'])
        path.write_text(json.dumps(dict(session, controller_tour_accounts='not a list')))
        result = world.run('status')
        self.assertOk(result)
        self.assertNotIn('Steam Controller detected', result.out)

    def test_status_does_not_fail_where_guests_private_audio_directory_cannot_be_read(self):
        world, _ = self.started()
        env = dosplex.Env(world.tree)
        env.trusted_uid = UID                       # the fake sysroot belongs to the test user
        original = Path.exists

        def refuse(path):
            if path.name == 'native':
                raise PermissionError(13, 'Permission denied')
            return original(path)
        with mock.patch.object(Path, 'exists', refuse), mock.patch.dict(os.environ, world.environment()):
            details = dosplex.trial_status(env, dosplex.read_state(env))
            self.assertIsNone(details['guest_audio_endpoint']['present'])
            info = dosplex.collect_status(env)
        self.assertIn('run with sudo to see', dosplex.format_status(info))

    def test_stop_recovers_stock_steam_and_returns_to_the_desktop_it_found(self):
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        result = world.run('stop')
        self.assertOk(result)
        self.assertIn('Desktop mode restored', result.out)
        self.assertEqual(world.units(), [])
        self.assertFalse(root.exists())
        (recover,) = world.events('trial.recover')
        self.assertEqual(recover['root'], str(root))
        steam = json.loads((world.fake / 'steamos.json').read_text())
        self.assertEqual((steam['mode'], steam['desktop_requested']), ('desktop', 'plasma.desktop'))
        self.assertIsNone(world.state()['trial'])
        self.assertIn('Running: no', world.run('status').out)
        again = world.run('stop')
        self.assertOk(again)
        self.assertIn('not running', again.out)
        self.assertEqual(len(world.events('trial.recover')), 1)

    def test_a_reboot_returns_to_stock_and_start_brings_dosplex_back(self):
        world, _ = self.started()
        (first,) = self.trial_roots(world)
        # The restart empties /run and every transient unit; the device comes up in Gaming mode.
        shutil.rmtree(first)
        for unit in (world.fake / 'units').iterdir():
            unit.unlink()
        world.save_fake('steamos.json', dict(json.loads((world.fake / 'steamos.json').read_text()), mode='gaming'))
        (world.tree / 'proc/100/comm').write_text('gamescope-wl\n')
        result = world.run('status')
        self.assertIn('Running: no', result.out)
        self.assertIn('the previous run is gone: the device was restarted, or Dosplex ended', result.out)
        switches = len([c for c in world.commands('steamosctl') if c['argv'][0] == 'switch-to-game-mode'])
        result = world.run('start')
        self.assertOk(result)
        self.assertIn('device was restarted', result.out)
        (second,) = self.trial_roots(world)
        self.assertNotEqual(second, first)
        self.assertEqual(world.state()['trial']['root'], str(second))
        # Already in Gaming mode with Steam idle: there is nothing to switch.
        self.assertEqual(len([c for c in world.commands('steamosctl') if c['argv'][0] == 'switch-to-game-mode']), switches)
        self.assertFalse(world.state()['session']['switched'])
        self.assertOk(world.run('stop'))
        self.assertEqual(json.loads((world.fake / 'steamos.json').read_text()).get('desktop_requested'), None)

    def test_start_never_switches_away_from_a_busy_gaming_mode(self):
        world = self.world(desktop=False)
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        (world.fake / 'busy').write_text('a game is running')
        result = world.run('start')
        self.assertFailed(result, 'Steam is busy')
        self.assertEqual([c for c in world.commands('steamosctl') if c['argv'][0].startswith('switch')], [])
        self.assertEqual(world.events('trial.start'), [])
        self.assertEqual(self.trial_roots(world), [])

    def test_stop_asks_both_sides_steam_to_quit_before_it_takes_the_session_down(self):
        # TRL-04: the session's own helper is run with `close` first, then the recovery. A helper that fails, or
        # one that is not root's own file, never keeps the session from being stopped.
        helper_text = ('import json, os, sys\n'
                       'open(os.environ["FAKE_STATE"] + "/calls.jsonl", "a").write('
                       'json.dumps({"event": "helper.close", "argv": sys.argv[1:]}) + "\\n")\n'
                       'sys.exit(int(os.environ.get("FAKE_CLOSE_EXIT", "0")))\n')
        for exit_status, mode, runs in (('0', 0o755, True), ('3', 0o755, True), ('0', 0o777, False)):
            with self.subTest(exit_status=exit_status, mode=oct(mode)):
                world, _ = self.started()
                (root,) = self.trial_roots(world)
                helper = root / 'bundle' / 'libexec' / 'dosplex-power-action'
                helper.parent.mkdir(parents=True, exist_ok=True)
                helper.write_text(helper_text)
                helper.chmod(mode)
                self.assertOk(world.run('stop', FAKE_CLOSE_EXIT=exit_status))
                names = [event['event'] for event in world.events()
                         if event['event'] in ('helper.close', 'trial.recover')]
                self.assertEqual(names, ['helper.close', 'trial.recover'] if runs else ['trial.recover'])
                if runs:
                    (closed,) = world.events('helper.close')
                    self.assertEqual(closed['argv'], ['close', '--trial-root', str(root)])
                self.assertFalse(root.exists())
                self.assertIn('Running: no', world.run('status').out)

    def test_start_refuses_a_trial_that_dosplex_did_not_start_and_stop_leaves_it_alone(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        foreign = world.tree / 'run' / ('dosplex-trial-' + 'b' * 32)
        foreign.mkdir()
        result = world.run('start')
        self.assertFailed(result, 'did not start')
        self.assertEqual(world.events('trial.start'), [])
        self.assertTrue(foreign.is_dir())
        self.assertIn('Other retained trials not started by dosplex', world.run('status').out)
        self.assertIn('not running', world.run('stop').out)
        self.assertTrue(foreign.is_dir())
        self.assertEqual(world.events('trial.recover'), [])

    def test_start_needs_no_controller_slots_and_remembers_the_ones_it_was_given(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertOk(world.run('start'))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, None, False))
        self.assertIsNone(world.state()['controllers'])          # nothing was named, so nothing is remembered
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start', *world.controllers(), '--sample-seconds', '0.3'))
        self.assertEqual(world.state()['controllers'], {'main': '/dev/hidraw4', 'guest': '/dev/hidraw5'})
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start'))               # remembered from the last successful start
        self.assertEqual([(e['main'], e['guest']) for e in world.events('external.run')][1:],
                         [('/dev/hidraw4', '/dev/hidraw5')] * 2)

    def test_a_given_slot_alone_is_that_players_and_is_never_listened_to(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        (world.tree / 'dev/hidraw4').write_bytes(b'')            # asleep
        self.assertOk(world.run('start', '--main-hid', '/dev/hidraw4'))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest']), ('/dev/hidraw4', None))
        self.assertEqual(world.state()['controllers'], {'main': '/dev/hidraw4', 'guest': None})
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start', '--guest-hid', '/dev/hidraw5', '--sample-seconds', '5'))
        self.assertEqual([(e['main'], e['guest']) for e in world.events('external.run')][1:], [(None, '/dev/hidraw5')])

    def test_a_remembered_slot_that_is_no_receiver_slot_any_more_is_left_out_with_a_note(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        shutil.rmtree(world.tree / 'sys/class/hidraw/hidraw5')           # the receiver slot is gone
        result = world.run('start')
        self.assertOk(result)
        self.assertIn('The remembered Guest controller slot is not used:', result.out)
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest']), ('/dev/hidraw4', None))
        self.assertEqual(world.state()['controllers'], {'main': '/dev/hidraw4', 'guest': '/dev/hidraw5'})   # kept for later
        # Both gone: it still starts, with no slot.
        self.assertOk(world.run('stop'))
        shutil.rmtree(world.tree / 'sys/class/hidraw/hidraw4')
        result = world.run('start')
        self.assertOk(result)
        self.assertIn('The remembered Main controller slot is not used:', result.out)
        self.assertEqual([(e['main'], e['guest']) for e in world.events('external.run')][1:], [(None, None)])

    def test_a_failed_start_returns_to_stock_steam(self):
        for failure in ('before-root', 'after-root', 'external', 'timeout'):
            with self.subTest(failure):
                world = self.world()
                self.assertOk(world.install(world.bundle(), '--no-start'))
                result = world.run('start', *world.controllers(), '--sample-seconds', '0.3', FAKE_TRIAL_FAIL=failure)
                self.assertFailed(result, 'did not finish')
                self.assertIn('Returning to stock Steam', result.out)
                self.assertEqual(world.units(), [])
                self.assertEqual(self.trial_roots(world), [])
                steam = json.loads((world.fake / 'steamos.json').read_text())
                self.assertEqual(steam['mode'], 'desktop')
                self.assertEqual(steam.get('desktop_requested'), 'plasma.desktop')
                self.assertIsNone(world.state()['trial'])
                tx = world.journal()['0002-start.jsonl']
                self.assertEqual(tx[-1]['outcome'], 'rolled-back')
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
                self.assertOk(world.run('start', *world.controllers(), '--sample-seconds', '0.3'))   # and it works again

    def test_a_start_whose_stop_of_steam_times_out_is_said_in_plain_words(self):
        # Seen on the Deck: the stop of the stock Steam took the full time, and the TimeoutExpired reached the
        # person as a command line. It is a sentence that names the program now, in the notes and in the log.
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', *world.controllers(), '--sample-seconds', '0.3', FAKE_TRIAL_FAIL='timeout')
        self.assertFailed(result, 'did not finish')
        self.assertIn('systemctl did not finish in 150 seconds. Try again', result.text)
        self.assertIn('Returning to stock Steam', result.out)
        worker = (world.fake / 'worker.log').read_text()
        self.assertIn('systemctl did not finish in 150 seconds. Try again', worker)
        for part in (result.text, worker):
            self.assertNotIn('Command [', part)
            self.assertNotIn('timed out after', part)
            self.assertNotIn('runuser', part)
        self.assertEqual(world.units(), [])
        self.assertIsNone(world.state()['trial'])
        self.assertOk(world.run('start', *world.controllers(), '--sample-seconds', '0.3'))   # and it works again

    def test_a_start_has_no_test_hooks_unless_root_left_the_engineering_marker(self):
        # TRL-09: a release starts the session without the compositor's test hooks.
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertOk(world.run('start', *world.controllers(), '--sample-seconds', '0.3'))
        (trial,) = world.events('trial.start')
        self.assertIs(trial['test_hooks'], False)
        self.assertNotIn('Test hooks', world.run('status').out)

    def test_the_engineering_marker_turns_test_hooks_on_and_says_so(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        marker = world.tree / 'var/lib/dosplex/test-hooks'
        marker.write_text('')
        marker.chmod(0o644)
        result = world.run('start', *world.controllers(), '--sample-seconds', '0.3')
        self.assertOk(result)
        (trial,) = world.events('trial.start')
        self.assertIs(trial['test_hooks'], True)
        self.assertIn('Test hooks are on for this session', result.out + result.err)
        self.assertIn('Test hooks: on at the next start', world.run('status').out)
        self.assertIs(json.loads(world.run('status', '--json').out)['test_hooks'], True)

    def test_a_marker_that_others_can_write_or_that_is_a_link_is_not_a_marker(self):
        for make in (lambda marker, other: (marker.write_text(''), marker.chmod(0o666)),
                     lambda marker, other: marker.symlink_to(other),
                     lambda marker, other: marker.mkdir()):
            world = self.world()
            self.assertOk(world.install(world.bundle(), '--no-start'))
            other = world.tree / 'var/lib/dosplex/other-file'
            other.write_text('')
            make(world.tree / 'var/lib/dosplex/test-hooks', other)
            self.assertOk(world.run('start', *world.controllers(), '--sample-seconds', '0.3'))
            (trial,) = world.events('trial.start')
            self.assertIs(trial['test_hooks'], False)

    def test_a_start_says_when_the_session_still_runs_an_older_release(self):
        # An install over a session that runs leaves that session on what it was started with (the Steam Machine,
        # 2026-11-20): "already running" alone did not say so.
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        version = world.state()['version']
        again = world.run('start')
        self.assertOk(again)
        self.assertIn('Dosplex is already running', again.out)
        self.assertNotIn('It still runs', again.out)                  # the stand-in session names no release
        (root / 'bundle/share/dosplex').mkdir(parents=True)
        (root / 'bundle/share/dosplex/VERSION').write_text(version + '\n')
        self.assertNotIn('It still runs', world.run('start').out)     # the same release: nothing to say
        (root / 'bundle/share/dosplex/VERSION').write_text('0.1.0-older\n')
        again = world.run('start')
        self.assertOk(again)
        self.assertIn('It still runs 0.1.0-older, the release it was started with. To use %s: sudo' % version, again.out)
        self.assertIn('dosplex stop, then sudo', again.out)
        self.assertEqual(len(world.events('trial.start')), 1)         # and nothing was started again

    def test_a_sleeping_controller_does_not_stop_a_start(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('dev/hidraw5', '')
        result = world.run('start', *world.controllers(), '--sample-seconds', '5')
        self.assertOk(result)
        self.assertEqual(len(world.events('trial.start')), 1)
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest']), ('/dev/hidraw4', '/dev/hidraw5'))

    def test_a_start_that_preflight_refuses_does_not_touch_the_device(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        world.write('sys/class/drm/card0-HDMI-A-1/status', 'disconnected\n')
        result = world.run('start')
        self.assertFailed(result, 'no connected display', code=3)
        self.assertEqual([c for c in world.commands('steamosctl') if c['argv'][0].startswith('switch')], [])
        self.assertEqual(world.events('trial.start'), [])
        self.assertEqual(world.commands('systemd-run'), [])
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_a_failed_recovery_is_reported_and_retried_by_the_next_stop(self):
        world, _ = self.started()
        result = world.run('stop', FAKE_RECOVER_FAIL='1')
        self.assertFailed(result, 'stop did not finish')
        self.assertEqual(world.journal()['0003-stop.jsonl'][-1]['outcome'], 'failed')
        self.assertIsNotNone(world.state()['trial'])                # still recorded as running
        self.assertOk(world.run('stop'))
        self.assertEqual(world.units(), [])

    def test_a_version_installed_on_an_untested_system_starts_without_asking_again_until_it_changes(self):
        world = self.world(version_id='9.9.9')
        bundle = world.bundle()
        self.assertOk(world.install(bundle, '--allow-untested', '--no-start', *world.controllers()))
        self.assertOk(world.run('start'))
        self.assertOk(world.run('stop'))
        world.write('etc/os-release', 'ID=steamos\nVERSION_ID=9.9.10\nBUILD_ID=1\n')      # the system was updated
        result = world.run('start')
        self.assertFailed(result, 'not in the tested set', code=3)
        self.assertEqual(len(world.events('trial.start')), 1)
        self.assertOk(world.run('start', '--allow-untested'))

    def test_install_while_running_updates_the_files_but_leaves_the_session_alone(self):
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        newer = world.bundle(name='newer', version='0.2.0-test')
        result = world.install(newer, *world.controllers())
        self.assertOk(result)
        self.assertIn('already running', result.out)
        self.assertEqual(self.trial_roots(world), [root])
        state = world.state()
        self.assertEqual((state['version'], state['previous'], state['trial']['root']), ('0.2.0-test', '0.1.0-test', str(root)))
        self.assertEqual(state['session']['switched'], True)           # what start found is not forgotten
        self.assertEqual(state['defaults']['host_mode'], 'desktop')    # nor are the original defaults
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.units(), [])

    def test_uninstall_stops_the_session_first(self):
        world, _ = self.started()
        (root,) = self.trial_roots(world)
        result = world.run('uninstall')
        self.assertOk(result)
        self.assertFalse(root.exists())
        self.assertEqual(world.units(), [])
        self.assertFalse((world.tree / 'opt/dosplex').exists())
        steam = json.loads((world.fake / 'steamos.json').read_text())
        self.assertEqual(steam.get('desktop_requested'), 'plasma.desktop')
        self.assertFalse((world.tree / 'var/lib/dosplex/state.json').exists())
        kinds = [(r['kind'], r.get('step')) for r in world.journal()['0003-uninstall.jsonl']]
        self.assertEqual(kinds[0], ('begin', None))
        self.assertEqual(kinds[-1], ('end', None))
        self.assertLess(kinds.index(('done', 'stop-session')), kinds.index(('intent', 'remove-state')))
        # The journal's final record stays when everything else is gone.
        self.assertEqual(sorted(p.name for p in (world.tree / 'var/lib/dosplex').iterdir()), ['journal'])
        self.assertEqual(world.journal()['0003-uninstall.jsonl'][-1]['outcome'], 'complete')


class Interrupted(Base):
    """A crash at every journal record and every inner step: repair completes or rolls back, and the device
    ends up either fully done or exactly as it was. DOSPLEX_TEST_CRASH_AT names the point to stop at.

    Each point gets its own fake sysroot, so the points run in parallel threads."""

    def points(self, prefix, run):
        """The crash points one clean run passes, in order, found through DOSPLEX_TEST_FAULT_LOG."""
        probe = self.world()
        log = probe.root / 'faults.log'
        run(probe, DOSPLEX_TEST_FAULT_LOG=str(log))
        seen = []
        for point in log.read_text().splitlines():
            if point.startswith(prefix) and point not in seen:
                seen.append(point)
        return seen

    def each(self, items, work):
        """work(item) for every item, in threads; every failure is reported as a subtest of its own."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [(item, pool.submit(work, item)) for item in items]
            results = [(item, future.exception(), future) for item, future in futures]
        outcomes = []
        for item, error, future in results:
            with self.subTest(point=item):
                if error is not None:
                    raise error
                outcomes.append(future.result())
        return outcomes

    @staticmethod
    def stripped(state):
        return {k: v for k, v in state.items() if k != 'installed_utc'}

    @staticmethod
    def installed_part(snapshot):
        return {k: v for k, v in snapshot.items()
                if k.startswith(('opt/', 'home/guest')) or k in ('etc/passwd', 'etc/group')}

    def installed_reference(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        return self.installed_part(world.snapshot()), self.stripped(world.state())

    def assertBackToStart(self, world):
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.users(), world.initial_users)
        self.assertEqual(world.units(), [])

    def crashes(self, result, point):
        """A crash at a point stops the command, except at :end, where the work was already complete."""
        if point.endswith(':end'):
            self.assertOk(result)
        else:
            self.assertFailed(result)

    # --- install -----------------------------------------------------------------------------------------
    def test_install_interrupted_at_every_journal_point_is_completed_or_rolled_back_by_repair(self):
        points = self.points('install:', lambda w, **env: self.assertOk(w.install(w.bundle(), '--no-start', **env)))
        for expected in ('install:begin', 'install:intent:defaults', 'install:done:stage', 'install:commit',
                         'install:intent:release', 'install:applied:guest', 'install:done:guest',
                         'install:done:state', 'install:done:prune', 'install:end', 'install:inner:staged',
                         'install:inner:release-renamed', 'install:inner:guest-created'):
            self.assertIn(expected, points)
        self.assertGreaterEqual(len(points), 30)
        reference_tree, reference_state = self.installed_reference()

        def work(point):
            world = self.world()
            bundle = world.bundle()
            crashed = world.install(bundle, '--no-start', DOSPLEX_TEST_CRASH_AT=point)
            self.assertFailed(crashed)
            before = world.journal()['0001-install.jsonl']
            finished = before[-1]['kind'] == 'end'
            committed = any(r['kind'] == 'commit' for r in before)
            if not finished:
                self.assertIn('Interrupted: install', world.run('status').out)
                self.assertFailed(world.run('install', bundle.path, '--manifest', bundle.sha256, '--no-start'),
                                  'dosplex repair', code=4)         # nothing else runs until it is repaired
            self.assertOk(world.run('repair'))
            self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
            after = world.journal()['0001-install.jsonl']
            self.assertEqual(after[-1]['kind'], 'end')
            if finished or committed:
                self.assertEqual(after[-1]['outcome'], 'complete')
                self.assertEqual(self.installed_part(world.snapshot()), reference_tree)
                self.assertEqual(self.stripped(world.state()), reference_state)
                self.assertEqual(os.readlink(world.tree / 'opt/dosplex/current'), 'releases/0.1.0-test')
                self.assertEqual(world.run('status').out.splitlines()[0], 'Dosplex 0.1.0-test (commit 012345678)')
                self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
                outcome = 'completed'
            else:
                self.assertEqual(after[-1]['outcome'], 'rolled-back')
                self.assertIn('Dosplex is not installed.', world.run('status').out)
                outcome = 'rolled back'
            self.assertBackToStart(world)
            return outcome
        self.assertEqual(set(self.each(points, work)), {'completed', 'rolled back'})

    def test_the_group_removal_is_completed_or_rolled_back_at_every_journal_point(self):
        def legacy():
            world = self.world()
            add_guest(world, groups=('video', 'seat', 'input', 'audio', 'games'))
            return world
        probe = legacy()
        log = probe.root / 'faults.log'
        self.assertOk(probe.install(probe.bundle(), '--no-start', '--adopt-guest-strip-groups',
                                    DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = []
        for point in log.read_text().splitlines():
            if point.startswith('install:') and point not in points:
                points.append(point)
        for expected in ('install:intent:guest-group:audio', 'install:inner:group-removed',
                         'install:applied:guest-group:seat', 'install:done:guest-group:video'):
            self.assertIn(expected, points)
        stripped = ['audio', 'input', 'seat', 'video']

        def work(point):
            world = legacy()
            bundle = world.bundle()
            crashed = world.install(bundle, '--no-start', '--adopt-guest-strip-groups', DOSPLEX_TEST_CRASH_AT=point)
            self.assertFailed(crashed)
            before = world.journal()['0001-install.jsonl']
            completed = before[-1]['kind'] == 'end' or any(r['kind'] == 'commit' for r in before)
            self.assertOk(world.run('repair'))
            if completed:
                for group in stripped:
                    self.assertEqual(group_members(world, group), [])
                self.assertEqual(group_members(world, 'games'), ['guest'])
                self.assertEqual(world.state()['guest']['stripped_groups'], stripped)
                self.assertOk(world.run('uninstall'))                    # which puts the memberships back
            else:
                self.assertIn('Dosplex is not installed.', world.run('status').out)
            self.assertBackToStart(world)
            return 'completed' if completed else 'rolled back'
        self.assertEqual(set(self.each(points, work)), {'completed', 'rolled back'})

    def test_a_crash_while_the_groups_are_being_put_back_is_repaired(self):
        world = self.world()
        add_guest(world, groups=('audio', 'input', 'seat'))
        bundle = world.bundle()
        crashed = world.install(bundle, '--no-start', '--adopt-guest-strip-groups', FAKE_GPASSWD_FAIL='seat',
                                DOSPLEX_TEST_CRASH_AT='install:undo-intent:guest-group:input')
        self.assertFailed(crashed)
        self.assertEqual(group_members(world, 'seat'), ['guest'])            # the removal that failed
        self.assertEqual(group_members(world, 'input'), [])                  # not yet put back when it crashed
        self.assertOk(world.run('repair'))
        for group in ('audio', 'input', 'seat'):
            self.assertEqual(group_members(world, group), [])
        self.assertEqual(world.state()['guest']['stripped_groups'], ['audio', 'input', 'seat'])
        self.assertOk(world.run('uninstall'))
        self.assertBackToStart(world)

    def test_after_a_rollback_the_device_installs_cleanly_again(self):
        world = self.world()
        bundle = world.bundle()
        self.assertEqual(world.install(bundle, '--no-start', DOSPLEX_TEST_CRASH_AT='install:applied:stage').code,
                         dosplex.FAULT_EXIT)
        self.assertOk(world.run('repair'))
        self.assertBackToStart(world)
        self.assertOk(world.install(bundle, '--no-start'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertBackToStart(world)

    def test_a_crash_inside_repair_is_repaired_by_the_next_repair(self):
        for first in ('install:done:stage', 'install:done:guest'):         # one that rolls back, one that completes
            probe = self.world()
            probe.install(probe.bundle(), '--no-start', DOSPLEX_TEST_CRASH_AT=first)
            log = probe.root / 'repair-faults.log'
            self.assertOk(probe.run('repair', DOSPLEX_TEST_FAULT_LOG=str(log)))
            second_points = []
            for point in log.read_text().splitlines():
                if point.startswith('install:') and point not in second_points:
                    second_points.append(point)
            self.assertGreater(len(second_points), 3, first)

            def work(second, first=first):
                world = self.world()
                bundle = world.bundle()
                self.assertEqual(world.install(bundle, '--no-start', DOSPLEX_TEST_CRASH_AT=first).code, dosplex.FAULT_EXIT)
                self.crashes(world.run('repair', DOSPLEX_TEST_CRASH_AT=second), second)
                self.assertOk(world.run('repair'))
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
                if (world.tree / 'opt/dosplex/current').is_symlink():
                    self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
                self.assertBackToStart(world)
            self.each(second_points, work)

    def test_an_empty_journal_file_from_a_crash_before_the_first_record_is_repaired(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertOk(world.run('uninstall'))          # Guest is kept, so the journal is too
        (world.journal_dir() / '0003-install.jsonl').write_bytes(b'')
        os.chmod(world.journal_dir() / '0003-install.jsonl', 0o600)
        self.assertIn('Interrupted: install', world.run('status').out)
        self.assertOk(world.run('repair'))
        self.assertOk(world.install(world.bundle(), '--no-start'))

    # --- start and stop ------------------------------------------------------------------------------------
    def test_start_interrupted_at_every_point_is_rolled_back_by_repair(self):
        points = self.points('start:', lambda w, **env: (
            self.assertOk(w.install(w.bundle(), *w.controllers(), '--no-start')), self.assertOk(w.run('start', **env))))
        for expected in ('start:begin', 'start:intent:host-mode', 'start:applied:trial', 'start:done:trial',
                         'start:done:controllers', 'start:done:running', 'start:end'):
            self.assertIn(expected, points)

        def work(point):
            world = self.world()
            self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
            installed = world.snapshot()
            self.crashes(world.run('start', DOSPLEX_TEST_CRASH_AT=point), point)
            finished = world.journal()['0002-start.jsonl'][-1]['kind'] == 'end'
            self.assertOk(world.run('repair'))
            self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
            if finished:                                    # it had gone all the way; it is simply running
                self.assertTrue(world.units())
                self.assertOk(world.run('stop'))
            else:
                self.assertEqual(world.journal()['0002-start.jsonl'][-1]['outcome'], 'rolled-back')
                self.assertIsNone(world.state()['trial'])
            self.assertEqual(world.units(), [])
            self.assertEqual(world.snapshot(), installed)              # no trial left, nothing else changed
            self.assertEqual(json.loads((world.fake / 'steamos.json').read_text())['mode'], 'desktop')
        self.each(points, work)
        # It starts again from scratch after a rollback, and removal still leaves nothing.
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        self.crashes(world.run('start', DOSPLEX_TEST_CRASH_AT='start:done:trial'), 'start:done:trial')
        self.assertOk(world.run('repair'))
        self.assertOk(world.run('start'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertBackToStart(world)

    def test_stop_interrupted_at_every_point_is_finished_by_repair(self):
        def run(world, **env):
            self.assertOk(world.install(world.bundle(), *world.controllers()))
            self.assertOk(world.run('stop', **env))
        points = self.points('stop:', run)
        for expected in ('stop:begin', 'stop:intent:stop-session', 'stop:inner:recovered', 'stop:end'):
            self.assertIn(expected, points)

        def work(point):
            world = self.world()
            self.assertOk(world.install(world.bundle(), *world.controllers()))
            self.crashes(world.run('stop', DOSPLEX_TEST_CRASH_AT=point), point)
            self.assertOk(world.run('repair'))
            self.assertEqual(world.units(), [])
            self.assertIsNone(world.state()['trial'])
            self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])
            self.assertEqual(json.loads((world.fake / 'steamos.json').read_text()).get('desktop_requested'),
                             'plasma.desktop')
        self.each(points, work)

    # --- uninstall -----------------------------------------------------------------------------------------
    def test_uninstall_interrupted_at_every_point_is_finished_by_repair(self):
        def run(world, **env):
            self.assertOk(world.install(world.bundle(), *world.controllers()))
            self.assertOk(world.run('uninstall', '--remove-guest', '--yes', **env))
        points = self.points('uninstall:', run)
        for expected in ('uninstall:begin', 'uninstall:done:stop-session', 'uninstall:intent:remove-state',
                         'uninstall:done:remove-current', 'uninstall:intent:guest',
                         'uninstall:inner:guest-account-removed', 'uninstall:done:guest',
                         'uninstall:done:remove-release:0.1.0-test', 'uninstall:done:remove-opt', 'uninstall:end'):
            self.assertIn(expected, points)

        def work(point):
            world = self.world()
            self.assertOk(world.install(world.bundle(), *world.controllers()))
            self.crashes(world.run('uninstall', '--remove-guest', '--yes', DOSPLEX_TEST_CRASH_AT=point), point)
            self.assertOk(world.run('repair'))
            self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
            self.assertEqual(world.units(), [])
            self.assertFalse((world.tree / 'opt/dosplex').exists())
            self.assertFalse((world.tree / 'var/lib/dosplex/state.json').exists())
            # Repair never removes Guest on its own: that needs a fresh, typed confirmation.
            if (world.tree / 'home/guest').exists():
                self.assertIn('dosplex uninstall --remove-guest', world.run('status').out)
                self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
            self.assertBackToStart(world)
            self.assertIn('Dosplex is not installed.', world.run('status').out)
        self.each(points, work)

    def test_a_plain_uninstall_interrupted_at_every_point_is_finished_by_repair(self):
        def run(world, **env):
            self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
            self.assertOk(world.run('uninstall', **env))
        points = self.points('uninstall:', run)

        def work(point):
            world = self.world()
            self.assertOk(world.install(world.bundle(), '--no-start'))
            self.crashes(world.run('uninstall', DOSPLEX_TEST_CRASH_AT=point), point)
            self.assertOk(world.run('repair'))
            self.assertFalse((world.tree / 'opt/dosplex').exists())
            self.assertTrue((world.tree / 'home/guest').is_dir())             # Guest is kept
            self.assertEqual(world.commands('userdel'), [])
            self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
            self.assertBackToStart(world)
        self.each(points, work)



def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestFields(Base):
    """The bundle builder writes the version and the tested set that the CLI reads."""

    @classmethod
    def setUpClass(cls):
        cls.builder = load_script('builder', ROOT / 'packaging/release-build/make-bundle.py')
        cls.integrity = load_script('integrity', ROOT / 'packaging/steamos/session/session-bundle-integrity.py')

    def test_release_version_defaults_to_the_project_version_and_the_commit(self):
        self.assertEqual(self.builder.release_version('a' * 40), '0.1.0-aaaaaaaaa')
        self.assertEqual(self.builder.release_version('a' * 40, '0.2.0-preview.1'), '0.2.0-preview.1')
        for bad in ('', '../x', 'a/b', '-x', 'x' * 65, 'a b', '.hidden'):
            with self.assertRaises(ValueError, msg=bad):
                self.builder.release_version('a' * 40, bad)

    def test_tested_versions_are_written_sorted_and_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tested.json'
            path.write_text(json.dumps({
                'notes': 'ignored',
                'steamos': [{'version_id': '3.9.2', 'build_id': 'b'}, {'version_id': '3.8.28'},
                            {'version_id': '3.9.2', 'build_id': 'b'}, {'version_id': '3.9.2', 'build_id': 'a'}],
                'gamescope': ['3.16.30', '3.16.23', '3.16.30'], 'steam_client': ['2', '1']}))
            self.assertEqual(self.builder.load_tested_versions(path), {
                'steamos': [{'version_id': '3.8.28'}, {'version_id': '3.9.2', 'build_id': 'a'},
                            {'version_id': '3.9.2', 'build_id': 'b'}],
                'gamescope': ['3.16.23', '3.16.30'], 'steam_client': ['1', '2']})
            for bad in ({'steamos': [{'build_id': 'x'}]}, {'steamos': 'x'}, {'gamescope': [1]}, {'steamos': [{'version_id': 'a', 'x': 1}]},
                        []):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError, msg=str(bad)):
                    self.builder.load_tested_versions(path)

    def test_the_shipped_tested_set_is_valid_and_the_command_accepts_it(self):
        tested_versions = self.builder.load_tested_versions(self.builder.DEFAULT_TESTED_VERSIONS)
        self.assertIn({'version_id': '3.9.2', 'build_id': '20260925.101'}, tested_versions['steamos'])
        self.assertEqual(dosplex.normalise_tested(tested_versions), tested_versions)

    def test_a_built_manifest_is_deterministic_and_the_command_and_the_old_verifier_both_accept_it(self):
        world = self.world()
        bundle = world.bundle()
        files = json.loads(bundle.manifest_bytes)['files']

        def build():
            manifest = self.builder.build_manifest(COMMIT, '0.1.0-test', tested(), {'a': 'b'}, 'c' * 64, files)
            return (json.dumps(manifest, indent=2) + '\n').encode()
        first, second = build(), build()
        self.assertEqual(first, second)                                    # same inputs, same bytes, same digest
        manifest = json.loads(first)
        self.assertEqual(list(manifest)[:4], ['commit', 'version', 'scope', 'tested_versions'])
        parsed = dosplex.parse_manifest(first)
        self.assertEqual((parsed.version, parsed.commit, parsed.tested), ('0.1.0-test', COMMIT, tested()))
        # The bundle's own verifier (used by root-session-trial.py on the installed release) reads only "files".
        (bundle.path / 'manifest.json').write_bytes(first)
        self.assertEqual(set(self.integrity.verified_files(bundle.path, sha256(first))), set(files))

    def test_the_builder_can_produce_every_file_the_command_requires(self):
        product = self.builder.product_files()
        produced = set(product) | {'bin/dosplex-compositor'} | set(self.builder.helper_files())
        self.assertEqual(sorted(set(dosplex.REQUIRED_RELEASE_FILES) - produced), [])
        for name in dosplex.Seeds.NAMES.values():
            self.assertIn('tools/' + name, product)

    def test_a_bundle_without_the_new_fields_is_what_an_older_builder_wrote(self):
        # The previous builder wrote no version and no tested set; the command must refuse such a bundle
        # rather than guess, and say what to do.
        old = {'commit': COMMIT, 'scope': 'x', 'files': {'a': {'sha256': '0' * 64, 'mode': 0o644, 'bytes': 1}}}
        with self.assertRaises(dosplex.VerifyError) as caught:
            dosplex.parse_manifest(json.dumps(old).encode())
        self.assertIn('rebuild the bundle', str(caught.exception))


class InstalledReleaseMatchesTheRetainedTrialVerifier(Base):
    def test_the_real_verifier_the_trial_uses_accepts_the_installed_release(self):
        integrity = load_script('integrity', ROOT / 'packaging/steamos/session/session-bundle-integrity.py')
        world = self.world()
        bundle = world.bundle()
        self.assertOk(world.install(bundle, '--no-start'))
        release = world.tree / 'opt/dosplex/releases/0.1.0-test'
        # root-session-trial.start() calls exactly this with the directory it is given and the approved digest.
        files = integrity.verified_files(release, bundle.sha256)
        self.assertEqual(set(files), set(json.loads(bundle.manifest_bytes)['files']))


class SeedContract(Base):
    """The CLI calls two functions of the retained-trial tools it finds in the release. Pin their interface
    to the real tools, so that a change there cannot silently break `dosplex start`."""

    @classmethod
    def setUpClass(cls):
        cls.trial = load_script('trial', ROOT / 'packaging/steamos/session/root-session-trial.py')
        cls.external = load_script('external', ROOT / 'packaging/steamos/session/start-external-native-trial.py')

    def parameters(self, function):
        return list(inspect.signature(function).parameters)

    def test_the_functions_the_command_calls_exist_with_the_arguments_it_passes(self):
        self.assertEqual(self.parameters(self.trial.start)[:5], ['bundle', 'manifest', 'seconds', 'main_user', 'guest_user'])
        for name in ('device_view', 'keep_session'):
            self.assertIn(name, self.parameters(self.trial.start))
        self.assertEqual(self.parameters(self.trial.idle_steam), ['user'])
        self.assertEqual(self.parameters(self.trial.present_window), ['user', 'previous'])
        self.assertEqual(self.parameters(self.trial.trusted_root), ['root'])
        self.assertEqual(self.parameters(self.external.run), ['root', 'main_hid', 'guest_hid', 'builtin_deck'])
        source = (ROOT / 'tools/dosplex').read_text()
        self.assertIn("trial.start(self.release, self.state['manifest_sha256'], 120, self.state['main_user'],\n"
                      "                             GUEST_USER, device_view=True, keep_session=True, **deck)", source)

    def test_the_trial_recovers_through_its_own_command_line(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'packaging/steamos/session/root-session-trial.py'), 'recover', '--help'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--root', proc.stdout)

    def test_the_receiver_slot_check_matches_the_enrollment_it_was_lifted_from(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        for hid in ('/dev/hidraw4', '/dev/hidraw5'):
            _, key = self.external.inspect_slot(Path(hid), world.tree / 'sys')
            self.assertEqual(dosplex.hid_slot(env, hid), key)
        self.assertEqual(dosplex.report_kind(bytes((0x42,)) + bytes(53)), 'ibex-input')
        preflight = load_script('machine_preflight', ROOT / 'packaging/steamos/session/machine-preflight.py')
        for data in (bytes((0x42,)) + bytes(53), bytes((0x45,)) + bytes(45), bytes((1, 0, 1, 60)) + bytes(60),
                     bytes((1, 0, 9, 64)) + bytes(60), b'', bytes(64)):
            self.assertEqual(dosplex.report_kind(data), preflight.report_kind(data))


class DisplayMode(Base):
    ORIGINAL = b'# kept\nOther TV:1280x720@60 0\nLG Electronics LG TV:1920x1080@60 0\n'

    def started(self, *extra, **env):
        world = self.world()
        world.write('home/deck/.config/gamescope/modes.cfg', self.ORIGINAL.decode())
        world.reset_baseline()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--output-mode', '3840', '2160', '60', *extra, **env)
        return world, result

    def modes(self, world):
        return (world.tree / 'home/deck/.config/gamescope/modes.cfg').read_bytes()

    def test_replace_mode_changes_only_the_selected_connector_and_keeps_other_bytes(self):
        before = b'# user config\nOther TV:1920x1080@60 0\nLG Electronics LG TV:1920x1080@60 0\n'
        after = dosplex.replace_mode(before, 'LG Electronics LG TV', 3840, 2160, 60)
        self.assertEqual(after, before.replace(b'LG Electronics LG TV:1920x1080', b'LG Electronics LG TV:3840x2160'))
        self.assertEqual(dosplex.replace_mode(b'', 'TV', 3840, 2160, 60), b'TV:3840x2160@60 0\n')
        self.assertEqual(dosplex.replace_mode(b'# comment', 'TV', 3840, 2160, 60), b'# comment\nTV:3840x2160@60 0\n')
        for name in ('', 'TV:extra', 'TV\nOther TV', 'TV\r'):
            with self.assertRaises(ValueError):
                dosplex.replace_mode(b'', name, 3840, 2160, 60)
        with self.assertRaises(ValueError):
            dosplex.replace_mode(b'TV:1920x1080@60\nTV:1280x720@60\n', 'TV', 3840, 2160, 60)

    def test_the_output_mode_is_applied_while_dosplex_runs_and_put_back_by_stop(self):
        world, result = self.started()
        self.assertOk(result)
        self.assertEqual(self.modes(world), self.ORIGINAL.replace(b'LG TV:1920x1080', b'LG TV:3840x2160'))
        display = json.loads((world.fake / 'display.json').read_text())
        self.assertEqual((display['physical'], display['virtual']), ([3840, 2160, 60], [3840, 2160]))
        record = json.loads((world.tree / 'var/lib/dosplex/display-mode.json').read_text())
        self.assertEqual(record['phase'], 'applied')
        self.assertEqual(stat.S_IMODE((world.tree / 'var/lib/dosplex/display-mode.json').stat().st_mode), 0o600)
        self.assertEqual(len(world.events('present_window')), 1)
        self.assertEqual(world.state()['output_mode'], [3840, 2160, 60])
        self.assertOk(world.run('stop'))
        self.assertEqual(self.modes(world), self.ORIGINAL)
        display = json.loads((world.fake / 'display.json').read_text())
        self.assertEqual((display['physical'], display['virtual']), ([1920, 1080, 60], [1920, 1080]))
        self.assertEqual(json.loads((world.tree / 'var/lib/dosplex/display-mode.json').read_text())['phase'], 'restored')
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertFalse((world.tree / 'var/lib/dosplex/display-mode.json').exists())
        self.assertEqual(world.snapshot(), world.initial)

    def test_stop_does_not_fail_when_the_running_gamescope_ignores_the_restored_file(self):
        world, result = self.started()
        self.assertOk(result)
        result = world.run('stop', FAKE_DISPLAY_IGNORE_NUDGE='1')          # HW-008: it never re-reads the file
        self.assertOk(result)
        self.assertEqual(self.modes(world), self.ORIGINAL)
        self.assertEqual(world.units(), [])
        steam = json.loads((world.fake / 'steamos.json').read_text())
        self.assertEqual(steam.get('desktop_requested'), 'plasma.desktop')      # the rest of the stop still happened

    def test_a_mode_the_tv_does_not_advertise_fails_the_start_and_leaves_the_file_alone(self):
        world = self.world()
        world.write('home/deck/.config/gamescope/modes.cfg', self.ORIGINAL.decode())
        world.reset_baseline()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--output-mode', '1280', '720', '75')
        self.assertFailed(result, 'does not advertise 1280x720 at 75 Hz')
        self.assertEqual(self.modes(world), self.ORIGINAL)
        self.assertEqual(world.units(), [])
        self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])
        self.assertFalse((world.tree / 'var/lib/dosplex/display-mode.json').exists())      # nothing was prepared

    def test_a_setting_the_user_changed_since_is_not_overwritten(self):
        world, result = self.started()
        self.assertOk(result)
        (world.tree / 'home/deck/.config/gamescope/modes.cfg').write_bytes(b'my own settings\n')
        self.assertOk(world.run('stop'))
        self.assertEqual(self.modes(world), b'my own settings\n')

    def test_after_a_restart_the_original_is_still_the_one_that_comes_back(self):
        world, result = self.started()
        self.assertOk(result)
        shutil.rmtree(next((world.tree / 'run').glob('dosplex-trial-*')))        # restart: /run is gone, the file stays
        for unit in (world.fake / 'units').iterdir():
            unit.unlink()
        world.save_fake('steamos.json', dict(json.loads((world.fake / 'steamos.json').read_text()), mode='gaming'))
        (world.tree / 'proc/100/comm').write_text('gamescope-wl\n')
        self.assertEqual(self.modes(world), self.ORIGINAL.replace(b'LG TV:1920x1080', b'LG TV:3840x2160'))
        self.assertOk(world.run('start', '--output-mode', '3840', '2160', '60'))
        self.assertOk(world.run('stop'))
        self.assertEqual(self.modes(world), self.ORIGINAL)             # not the applied value that it found at start

    def test_a_crash_while_the_mode_is_being_applied_is_rolled_back_by_repair(self):
        for point in ('start:inner:display-prepared', 'start:applied:display-mode', 'start:done:display-mode'):
            with self.subTest(point=point):
                world = self.world()
                world.write('home/deck/.config/gamescope/modes.cfg', self.ORIGINAL.decode())
                world.reset_baseline()
                self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
                self.assertFailed(world.run('start', '--output-mode', '3840', '2160', '60', DOSPLEX_TEST_CRASH_AT=point))
                self.assertOk(world.run('repair'))
                self.assertEqual(self.modes(world), self.ORIGINAL)
                self.assertEqual(world.units(), [])
                self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])
                self.assertEqual(json.loads((world.fake / 'display.json').read_text())['physical'], [1920, 1080, 60])

    def test_output_mode_values_are_checked(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        for values in (('100', '100', '60'), ('3840', '2160', '500'), ('x', '2160', '60')):
            result = world.run('start', *world.controllers(), '--output-mode', *values)
            self.assertFailed(result, code=2)
        self.assertEqual(world.commands('systemd-run'), [])


class BuiltinScreen(Base):
    """A Steam Deck that is not docked starts on its own panel; docked it keeps using the TV (T-435)."""

    def deck(self):
        world = self.world(receivers=True)
        world.write('home/deck/.config/gamescope/modes.cfg', DisplayMode.ORIGINAL.decode())
        world.reset_baseline()
        return world

    def undock(self, world, panel=True):
        world.write('sys/class/drm/card0-HDMI-A-1/status', 'disconnected\n')
        if panel:
            world.write('sys/class/drm/card0-eDP-1/status', 'connected\n')
            world.write('sys/class/drm/card0-eDP-1/modes', '800x1280\n')

    def modes(self, world):
        return (world.tree / 'home/deck/.config/gamescope/modes.cfg').read_bytes()

    def test_undocked_with_a_saved_tv_mode_starts_on_the_panel_and_keeps_the_tv_mode(self):
        world = self.deck()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--output-mode', '3840', '2160', '60',
                                    '--no-start'))
        self.undock(world)
        result = world.run('start')
        self.assertOk(result)
        self.assertNotIn('TV', result.out + result.err)
        self.assertEqual(self.modes(world), DisplayMode.ORIGINAL)           # no mode was applied
        self.assertFalse((world.tree / 'var/lib/dosplex/display-mode.json').exists())
        self.assertEqual(world.state()['output_mode'], [3840, 2160, 60])     # docking later returns to the TV mode
        self.assertTrue(world.units())

    def test_docked_still_applies_the_saved_tv_mode(self):
        world = self.deck()
        world.write('sys/class/drm/card0-eDP-1/status', 'connected\n')       # the panel stays connected when docked
        world.write('sys/class/drm/card0-eDP-1/modes', '800x1280\n')
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--output-mode', '3840', '2160', '60',
                                    '--no-start'))
        self.assertOk(world.run('start'))
        self.assertEqual(self.modes(world), DisplayMode.ORIGINAL.replace(b'LG TV:1920x1080', b'LG TV:3840x2160'))
        self.assertEqual(world.state()['output_mode'], [3840, 2160, 60])

    def test_an_explicit_panel_mode_is_accepted_in_either_orientation_and_not_saved(self):
        for values in (('800', '1280', '60'), ('1280', '800', '60')):
            with self.subTest(values=values):
                world = self.deck()
                self.undock(world)
                self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
                self.assertOk(world.run('start', '--output-mode', *values))
                self.assertEqual(self.modes(world), DisplayMode.ORIGINAL)
                self.assertIsNone(world.state()['output_mode'])

    def test_install_with_the_panel_mode_does_not_save_it_as_the_tv_mode(self):
        world = self.deck()
        self.undock(world)
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--output-mode', '800', '1280', '60'))
        self.assertIsNone(world.state()['output_mode'])
        self.assertTrue(world.units())

    def test_a_mode_the_panel_does_not_have_is_refused_naming_the_screen(self):
        world = self.deck()
        self.undock(world)
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        result = world.run('start', '--output-mode', '3840', '2160', '60')
        self.assertFailed(result, 'built-in screen')
        self.assertNotIn('TV does not advertise', result.out + result.err)
        self.assertEqual(world.units(), [])

    def test_preflight_says_built_in_screen_connected(self):
        world = self.deck()
        self.undock(world)
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--sample-seconds', '0.3',
                           *world.controllers())
        self.assertOk(result)
        self.assertIn('built-in screen connected (card0-eDP-1)', result.out)

    def test_a_machine_without_a_panel_and_without_a_display_still_refuses(self):
        world = self.deck()
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        self.undock(world, panel=False)
        result = world.run('start')
        self.assertFailed(result, 'no connected display')
        self.assertEqual(world.units(), [])


class Documentation(Base):
    def test_the_installation_page_describes_the_commands_this_program_has(self):
        page = (ROOT / 'docs/steamos-installation.md').read_text()
        parser = dosplex.build_parser()
        commands = parser._subparsers._group_actions[0].choices
        for name in ('install', 'start', 'stop', 'status', 'repair', 'uninstall', 'version', 'preflight', 'update',
                     'rollback', 'library'):
            self.assertIn(name, commands)
            self.assertIn('dosplex ' + name, page, name)
        for flag in ('--manifest', '--allow-untested', '--main-hid', '--guest-hid', '--output-mode', '--remove-guest',
                     '--adopt-guest-strip-groups', '--release', '--signature', '--channel', '--check', '--no-restart'):
            self.assertIn(flag, page)
        for statement in ('passwd', 'Desktop mode', 'reboot', 'stock SteamOS', 'DOSPLEX_CHANNEL', 'dosplex.previous',
                          'sha256sum', 'pipe a download into `sudo sh`'):
            self.assertIn(statement, page)
        self.assertNotIn('install-machine-human' + '-test', page)
        self.assertNotIn('private' + '/', page)          # the page is meant to be publishable

class Probes(Base):
    def env(self, **options):
        return dosplex.Env(self.world(**options).tree)

    def test_gamescope_version_is_read_from_either_stream_in_the_usual_formats(self):
        env = self.env()
        for output, expected in (('gamescope version 3.16.30 (gcc 15.2.1 20250813)\n', '3.16.30'),
                                 ('gamescope version 3.16.23.6-1 (gcc 14)\n', '3.16.23.6-1'),
                                 ('gamescope 3.16.30\n', '3.16.30'), ('GAMESCOPE VERSION v3.14.2\n', '3.14.2'),
                                 ('gcc version 15.2.1\n', None), ('', None)):
            for stream in ('stdout', 'stderr'):
                proc = subprocess.CompletedProcess([], 0, output if stream == 'stdout' else '',
                                                   output if stream == 'stderr' else '')
                with mock.patch.object(dosplex.Env, 'run', return_value=proc):
                    self.assertEqual(dosplex.gamescope_version(env), expected, output)
        with mock.patch.object(dosplex.Env, 'run', side_effect=dosplex.CommandMissing('gamescope is not installed')):
            self.assertIsNone(dosplex.gamescope_version(env))

    def test_the_steam_client_version_comes_from_the_manifest_of_the_channel_in_use(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        account = dosplex.user_entry(env, 'deck')
        package = 'home/deck/.local/share/Steam/package/'
        self.assertEqual(dosplex.steam_client_version(env, account),
                         (STEAM, 'steam_client_steamdeck_stable_ubuntu12.manifest'))
        world.write(package + 'steam_client_ubuntu12.manifest', '"ubuntu12"\n{\n\t"version"\t"1111"\n}\n')
        self.assertEqual(dosplex.steam_client_version(env, account)[0], STEAM)        # the channel file decides
        (world.tree / package / 'beta').unlink()
        self.assertEqual(dosplex.steam_client_version(env, account), ('1111', 'steam_client_ubuntu12.manifest'))
        for name in ('steam_client_ubuntu12.manifest', 'steam_client_steamdeck_stable_ubuntu12.manifest'):
            (world.tree / package / name).unlink()
        version, reason = dosplex.steam_client_version(env, account)
        self.assertIsNone(version)
        self.assertIn('no Steam client manifest found', reason)
        world.write(package + 'beta', '../../etc\n')                          # a channel is a plain name
        world.write(package + 'steam_client_ubuntu12.manifest', '"version" "42"')
        self.assertEqual(dosplex.steam_client_version(env, account)[0], '42')

    def test_os_release_accounts_and_groups_are_read_from_the_sysroot(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        world.write('etc/os-release', '# comment\nID=steamos\nNAME="Steam OS"\nVERSION_ID="3.9.2"\nBUILD_ID=\'x\'\nbroken\n')
        self.assertEqual(dosplex.read_os_release(env),
                         {'ID': 'steamos', 'NAME': 'Steam OS', 'VERSION_ID': '3.9.2', 'BUILD_ID': 'x'})
        self.assertEqual(dosplex.user_entry(env, 'deck').uid, 1000)
        self.assertIsNone(dosplex.user_entry(env, 'nobody-here'))
        self.assertEqual(dosplex.read_groups(env)['wheel'], (998, ['deck']))

    def test_host_mode_is_read_from_the_main_users_processes(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        account = dosplex.user_entry(env, 'deck')
        self.assertEqual((dosplex.host_mode(env, account), dosplex.host_desktop_session(env, account)),
                         ('desktop', 'plasma.desktop'))
        (world.tree / 'proc/100/comm').write_text('kwin_x11\n')
        self.assertEqual(dosplex.host_desktop_session(env, account), 'plasmax11.desktop')
        (world.tree / 'proc/100/comm').write_text('gamescope-wl\n')
        self.assertEqual((dosplex.host_mode(env, account), dosplex.host_desktop_session(env, account)), ('gaming', None))
        (world.tree / 'proc/100/comm').write_text('steam\n')
        self.assertEqual(dosplex.host_mode(env, account), 'unknown')
        (world.tree / 'proc/100/status').write_text('Uid:\t2000\t2000\t2000\t2000\n')       # someone else's process
        (world.tree / 'proc/100/comm').write_text('kwin_wayland\n')
        self.assertEqual(dosplex.host_mode(env, account), 'unknown')

    def test_the_legacy_installer_leftovers_are_reported_and_left_alone(self):
        world = self.world()
        os.chmod(world.tree / 'usr/bin', 0o755)
        world.write('usr/bin/steamos-session-select.orig', 'x')
        env = dosplex.Env(world.tree)
        self.assertEqual(dosplex.legacy_leftovers(env), ['/usr/bin/steamos-session-select.orig'])


class CommandLine(Base):
    def test_every_subcommand_has_help_and_the_flags_of_the_spec(self):
        world = self.world()
        for command in ('install', 'start', 'stop', 'status', 'repair', 'uninstall', 'version', 'preflight', 'update',
                        'rollback', 'library'):
            result = world.run(command, '--help')
            self.assertOk(result)
            self.assertIn('usage: dosplex ' + command, result.out)
        install = world.run('install', '--help').out
        for flag in ('--manifest', '--allow-untested', '--main-hid', '--guest-hid', '--output-mode', '--no-start',
                     '--sysroot', '--release', '--signature'):
            self.assertIn(flag, install)
        for flag in ('--channel', '--check', '--allow-untested', '--no-restart'):
            self.assertIn(flag, world.run('update', '--help').out)
        self.assertNotIn('--sample-seconds', world.run('update', '--help').out)          # a test-only flag stays out of sight
        self.assertIn('--remove-guest', world.run('uninstall', '--help').out)
        self.assertNotIn('--yes', world.run('uninstall', '--help').out)         # a test-only flag stays out of sight
        self.assertNotIn('_worker', world.run('--help').out)

    def test_hints_name_a_command_that_works_on_the_device(self):
        # Nothing is installed yet: the hint names the file that was run.
        world = self.world()
        self.assertEqual(world.install(world.bundle(), '--no-start', DOSPLEX_TEST_CRASH_AT='install:done:stage').code,
                         dosplex.FAULT_EXIT)
        self.assertIn('Run: sudo %s repair' % CLI, world.run('status').out)
        self.assertIn('Run: sudo %s repair' % CLI, world.run('uninstall').err)
        # Once the program is installed, it names the installed program, which is where the person finds it.
        world = self.world()
        self.assertEqual(world.install(world.bundle(), '--no-start', DOSPLEX_TEST_CRASH_AT='install:done:cli').code,
                         dosplex.FAULT_EXIT)
        installed = world.tree / 'opt/dosplex/bin/dosplex'
        self.assertIn('Run: sudo %s repair' % installed, world.run('status').out)
        self.assertNotIn('sudo dosplex ', world.run('uninstall').err)
        self.assertOk(world.run('repair'))
        out = world.run('uninstall').out
        # The installed program is gone after an uninstall, so the message must not send the person to it.
        self.assertIn('sudo ./dosplex-VERSION/dosplex uninstall --remove-guest', out)
        self.assertNotIn('%s uninstall --remove-guest' % installed, out)
        self.assertFalse(installed.exists())

    def test_usage_errors_exit_with_2_and_change_nothing(self):
        world = self.world()
        for arguments in ((), ('no-such-command',), ('install',), ('install', 'somewhere'),
                          ('start', '--output-mode', '1', '2'), ('start', '--sample-seconds', '9'),
                          ('uninstall', '--no-such-flag')):
            result = world.run(*arguments)
            self.assertFailed(result, code=2)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        self.assertEqual(world.calls(), [])

    def test_a_damaged_state_file_is_refused_with_a_message_and_never_a_traceback(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        state = world.tree / 'var/lib/dosplex/state.json'
        good = json.loads(state.read_text())
        for label, damaged in (
                ('keys missing', {'schema': 1}),
                ('a release that is not the version', dict(good, release='releases/../../x')),
                ('a version that is a path', dict(good, version='../x', release='releases/../x')),
                ('a bad digest', dict(good, manifest_sha256='nope')),
                ('a main user that is a command line', dict(good, main_user='deck; rm')),
                ('a trial root outside /run', dict(good, trial={'root': '/etc'}))):
            with self.subTest(label):
                state.write_text(json.dumps(damaged))
                for command in ('status', 'version', 'start', 'stop'):
                    result = world.run(command)
                    self.assertFailed(result, 'is damaged')
                    self.assertNotIn('Traceback', result.text)
        state.write_text('{not json')
        self.assertFailed(world.run('status'), 'is damaged')
        self.assertEqual(world.commands('systemd-run'), [])


def group_members(world, group):
    for line in (world.tree / 'etc/group').read_text().splitlines():
        fields = line.split(':')
        if fields[0] == group:
            return [m for m in fields[3].split(',') if m]
    return None


class StripGroups(Base):
    """--adopt-guest-strip-groups: an older installer left Guest in audio, input and seat (PKG-012)."""
    ALL = ('audio', 'input', 'seat', 'video', 'render')

    def legacy(self, groups=('video', 'seat', 'input', 'audio'), extra=('games',)):
        world = self.world()
        add_guest(world, groups=tuple(groups) + tuple(extra))
        return world

    members = staticmethod(group_members)

    @staticmethod
    def gpasswd(world, flag):
        return [c['argv'] for c in world.commands('gpasswd') if flag in c['argv']]

    def test_without_the_flag_the_legacy_groups_still_refuse_adoption_and_the_flag_is_named(self):
        world = self.legacy(groups=('audio', 'input', 'seat'))
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'cannot be adopted', code=3)
        for text in ('audio/input/seat', '--adopt-guest-strip-groups', 'gpasswd -d', 'Nothing was changed'):
            self.assertIn(text, result.err)
        self.assertNothingChanged(world)
        self.assertEqual(world.commands('gpasswd'), [])

    def test_the_flag_removes_exactly_the_five_groups_one_journaled_step_each(self):
        world = self.legacy(groups=('render', 'video', 'seat', 'input', 'audio'))
        group_file = (world.tree / 'etc/group').read_text().splitlines()
        result = world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups')
        self.assertOk(result)
        # It said what it was going to do before it did it.
        self.assertIn('--adopt-guest-strip-groups removes exactly these memberships', result.out)
        self.assertLess(result.out.index('gpasswd -d guest audio'), result.out.index('Installed Dosplex'))
        self.assertIn('Uninstall puts them back unless it removes Guest.', result.out)
        removals = [c['argv'] for c in world.commands('gpasswd')]
        self.assertEqual(removals, [['--root', str(world.tree), '-d', 'guest', group] for group in self.ALL])
        for group in self.ALL:
            self.assertEqual(self.members(world, group), [])
        self.assertEqual(self.members(world, 'games'), ['guest'])              # nothing else was touched
        after = (world.tree / 'etc/group').read_text().splitlines()
        self.assertEqual([l for l in after if l.split(':')[0] not in self.ALL],
                         [l for l in group_file if l.split(':')[0] not in self.ALL])
        self.assertEqual(world.commands('useradd'), [])
        # Each removal is its own step, recorded before the Guest step; the state remembers them for uninstall.
        records = world.journal()['0001-install.jsonl']
        intents = [r['step'] for r in records if r['kind'] == 'intent']
        self.assertEqual(intents, ['defaults', 'stage', 'release'] + ['guest-group:' + g for g in self.ALL] +
                         ['guest', 'current', 'cli', 'state', 'prune'])
        for group in self.ALL:
            done = [r for r in records if r['kind'] == 'done' and r['step'] == 'guest-group:' + group][0]
            self.assertEqual(done['data'], {'group': group, 'removed': True})
        self.assertEqual(world.state()['guest']['stripped_groups'], list(self.ALL))
        self.assertEqual([c['argv'] for c in world.commands('id')], [['-nG', 'guest']] * len(self.ALL))
        self.assertEqual(stat.S_IMODE((world.tree / 'home/guest').stat().st_mode), 0o700)

    def test_only_the_groups_guest_is_in_are_touched(self):
        world = self.legacy(groups=('input', 'video'), extra=())
        self.assertOk(world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups'))
        self.assertEqual([a[-1] for a in self.gpasswd(world, '-d')], ['input', 'video'])
        self.assertEqual(world.state()['guest']['stripped_groups'], ['input', 'video'])

    def test_any_other_group_that_voids_isolation_still_refuses_with_the_flag(self):
        world = self.legacy(groups=('audio', 'wheel'), extra=())
        result = world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups')
        self.assertFailed(result, 'cannot be adopted', code=3)
        self.assertIn('wheel', result.err)
        self.assertNotIn('--adopt-guest-strip-groups and', result.err)       # the flag would not help
        self.assertNothingChanged(world)
        self.assertEqual(world.commands('gpasswd'), [])

    def test_the_flag_changes_nothing_for_a_guest_that_is_not_in_those_groups(self):
        for case, prepare in (('an adopted guest with other groups', lambda w: add_guest(w, groups=('games',))),
                              ('a guest that gets created', lambda w: None)):
            with self.subTest(case):
                world = self.world()
                prepare(world)
                result = world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups')
                self.assertOk(result)
                self.assertNotIn('gpasswd -d', result.out)
                self.assertEqual(world.commands('gpasswd'), [])
                self.assertEqual(world.state()['guest']['stripped_groups'], [])

    def test_video_and_render_alone_do_not_block_adoption_but_the_flag_removes_them(self):
        world = self.legacy(groups=('video', 'render'), extra=())
        self.assertOk(world.install(world.bundle(), '--no-start'))                  # not a refusal without the flag
        self.assertEqual(world.commands('gpasswd'), [])
        self.assertEqual(self.members(world, 'video'), ['guest'])
        self.assertOk(world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups'))
        self.assertEqual([a[-1] for a in self.gpasswd(world, '-d')], ['video', 'render'])
        self.assertEqual((self.members(world, 'video'), self.members(world, 'render')), ([], []))

    def test_uninstall_puts_the_memberships_back_when_guest_stays(self):
        world = self.legacy()
        self.assertOk(world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups'))
        result = world.run('uninstall')
        self.assertOk(result)
        self.assertIn('Guest was put back into the groups it was taken out of: audio, input, seat, video.', result.out)
        added = [a for a in self.gpasswd(world, '-a')]
        self.assertEqual(added, [['--root', str(world.tree), '-a', 'guest', g] for g in ('audio', 'input', 'seat', 'video')])
        self.assertEqual(world.users(), world.initial_users)                         # the group file is as it was
        self.assertEqual(world.snapshot(), world.initial)
        records = world.journal()['0002-uninstall.jsonl']
        steps = [r['step'] for r in records if r['kind'] == 'intent']
        self.assertEqual(steps[steps.index('guest'):steps.index('guest') + 5],
                         ['guest'] + ['restore-guest-group:' + g for g in ('audio', 'input', 'seat', 'video')])
        # Back where it started, so adopting it again needs the flag again.
        self.assertFailed(world.install(world.bundle(), '--no-start'), '--adopt-guest-strip-groups', code=3)

    def test_uninstall_that_removes_guest_puts_nothing_back(self):
        world = self.legacy()
        directory = world.tree / ('var/tmp/dosplex-machine-' + 'a' * 32)
        directory.mkdir(mode=0o700)
        (directory / 'host.json').write_text(json.dumps({'guest_creation_intent': True, 'guest_home': '/home/guest',
                                                          'guest_uid': 1500, 'guest_gid': 1500}))
        (directory / 'host.json').chmod(0o600)
        self.assertOk(world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups'))
        self.assertEqual(world.state()['guest']['origin'], 'adopted-dosplex')
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertOk(result)
        self.assertNotIn('put back into the groups', result.out)
        self.assertEqual(self.gpasswd(world, '-a'), [])
        self.assertFalse((world.tree / 'home/guest').exists())
        self.assertNotIn('guest', (world.tree / 'etc/group').read_text())
        self.assertEqual(self.members(world, 'games'), [])                            # userdel took the rest

    def test_a_removal_that_fails_rolls_the_install_back_to_the_original_groups(self):
        world = self.legacy(groups=('audio', 'input', 'seat'))
        result = world.install(world.bundle(), '--no-start', '--adopt-guest-strip-groups', FAKE_GPASSWD_FAIL='seat')
        self.assertFailed(result, 'gpasswd failed')
        self.assertEqual([a[-1] for a in self.gpasswd(world, '-d')], ['audio', 'input', 'seat'])
        self.assertEqual([a[-1] for a in self.gpasswd(world, '-a')], ['input', 'audio'])      # newest first
        self.assertEqual(world.users(), world.initial_users)
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(world.journal()['0001-install.jsonl'][-1]['outcome'], 'rolled-back')
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_adding_to_a_group_is_only_possible_for_what_the_journal_removed(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        sysroot = str(env.sysroot)
        for group in self.ALL:
            dosplex.check_command(env, ['gpasswd', '--root', sysroot, '-d', 'guest', group])
            with self.assertRaises(dosplex.PolicyError):
                dosplex.check_command(env, ['gpasswd', '--root', sysroot, '-a', 'guest', group])
            with env.group_restore():
                dosplex.check_command(env, ['gpasswd', '--root', sysroot, '-a', 'guest', group])
            with self.assertRaises(dosplex.PolicyError):
                dosplex.check_command(env, ['gpasswd', '--root', sysroot, '-a', 'guest', group])        # and not after
        with env.group_restore():
            for argv in (['gpasswd', '--root', sysroot, '-a', 'guest', 'wheel'], ['gpasswd', '--root', sysroot, '-a', 'deck', 'audio']):
                with self.assertRaises(dosplex.PolicyError):
                    dosplex.check_command(env, argv)
        dosplex.check_command(env, ['id', '-nG', 'guest'])

    def test_an_install_without_the_flag_never_adds_or_removes_any_membership(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers()))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertEqual(world.commands('gpasswd'), [])


class DeckControls(Base):
    """A Steam Deck: its own controller is Main's (ADR-014), so no receiver is needed to install or start."""

    def deck(self, receivers=False):
        world = self.world(receivers=receivers)
        world.add_builtin_deck()
        return world

    def test_preflight_on_a_deck_accepts_no_receiver_at_all(self):
        world = self.deck()
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn("the Steam Deck's own controls (/dev/hidraw0, /dev/hidraw1, /dev/hidraw2) are Main's", result.out)
        self.assertIn('Result: ready', result.out)

    def test_preflight_on_a_deck_takes_guests_receiver_alone_and_refuses_a_main_slot(self):
        world = self.deck(receivers=True)
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--guest-hid', '/dev/hidraw5',
                           '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn("the Steam Deck's own controls (Main) and /dev/hidraw5 (Guest)", result.out)
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, *world.controllers(),
                           '--sample-seconds', '0.3')
        self.assertFailed(result, "Main's controls are its built-in ones", code=3)

    def test_a_deck_with_a_missing_interface_is_refused(self):
        world = self.deck()
        shutil.rmtree(world.tree / 'sys/class/hidraw/hidraw2')
        bundle = world.bundle()
        self.assertFailed(world.run('preflight', bundle.path, '--manifest', bundle.sha256), 'expected three', code=3)

    def test_a_device_without_the_deck_controller_takes_one_slot_or_none(self):
        world = self.world(receivers=False)
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--guest-hid', '/dev/hidraw5')
        self.assertFailed(result, 'cannot inspect /dev/hidraw5', code=3)          # not a receiver, but not a pair either
        self.assertNotIn('go together', result.text)
        self.assertOk(world.run('preflight', bundle.path, '--manifest', bundle.sha256))
        result = world.install(bundle, '--no-start')
        self.assertOk(result)
        self.assertIn('no controller slot given; Dosplex starts without one', result.out)
        world = self.world()
        bundle = world.bundle()
        result = world.run('preflight', bundle.path, '--manifest', bundle.sha256, '--main-hid', '/dev/hidraw4',
                           '--sample-seconds', '0.3')
        self.assertOk(result)
        self.assertIn('/dev/hidraw4 (Main)', result.out)

    def test_install_on_a_deck_without_receivers_starts_with_the_built_in_controls_as_main(self):
        world = self.deck()
        self.assertOk(world.install(world.bundle()))
        (trial,) = world.events('trial.start')
        self.assertEqual((trial['native_deck'], trial['native_relay'], trial['device_view']), ('left', True, True))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, None, True))
        self.assertIsNone(world.state()['controllers'])
        steps = [r['step'] for r in world.journal()['0002-start.jsonl'] if r['kind'] == 'intent']
        self.assertEqual(steps, ['verify-release', 'preflight', 'host-mode', 'idle-steam', 'trial', 'controllers', 'running'])
        # Later starts need no slot either.
        self.assertOk(world.run('stop'))
        self.assertOk(world.run('start', '--sample-seconds', '0.3'))
        self.assertEqual([e['builtin_deck'] for e in world.events('external.run')], [True, True])

    def test_guest_slot_alone_is_remembered_on_a_deck(self):
        world = self.deck(receivers=True)
        self.assertOk(world.install(world.bundle(), '--guest-hid', '/dev/hidraw5', '--sample-seconds', '0.3'))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, '/dev/hidraw5', True))
        self.assertEqual(world.state()['controllers'], {'main': None, 'guest': '/dev/hidraw5'})

    def test_the_steam_machine_path_is_unchanged(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), *world.controllers()))
        (trial,) = world.events('trial.start')
        self.assertEqual((trial['native_deck'], trial['native_relay']), (None, False))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']),
                         ('/dev/hidraw4', '/dev/hidraw5', False))
        world = self.world()
        self.assertOk(world.install(world.bundle()))
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, None, False))


class Ownership(Base):
    """A state or journal directory that root does not own is refused, with the owner and the exact fix."""

    def in_process(self, world, *args):
        args = [str(a) for a in args]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, world.environment(), clear=True), \
                mock.patch.object(sys, 'argv', [str(CLI), *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dosplex.main(['--sysroot', str(world.tree), *args])
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def owned_by(path, uid):
        """Make the directory look as if `uid` owned it; the test user cannot really chown anything."""
        real, target = os.fstat, os.stat(path).st_ino

        def fstat(fd):
            info = real(fd)
            if info.st_ino == target:
                values = list(info)
                values[4] = uid
                return os.stat_result(values)
            return info
        return mock.patch.object(os, 'fstat', fstat)

    def world_with_somebody(self):
        world = self.world()
        passwd = (world.tree / 'etc/passwd').read_text()
        world.write('etc/passwd', passwd + 'somebody:x:4242:4242::/srv/somebody:/bin/bash\n')
        self.assertOk(world.install(world.bundle(), '--no-start'))
        return world

    def test_status_names_the_owner_and_the_fix_for_the_state_directory(self):
        world = self.world_with_somebody()
        directory = world.tree / 'var/lib/dosplex'
        with self.owned_by(directory, 4242):
            code, _, err = self.in_process(world, 'status')
        self.assertEqual(code, 1)
        self.assertIn('%s is not owned by root; refusing to use it. It is owned by somebody (uid 4242).' % directory, err)
        self.assertIn('Fix: sudo chown root:root %s && sudo chmod 0755 %s' % (directory, directory), err)
        with self.owned_by(directory, 5000):                                  # an uid nobody has a name for
            self.assertIn('It is owned by uid 5000.', self.in_process(world, 'status')[2])

    def test_the_journal_directory_gets_the_same_treatment(self):
        world = self.world_with_somebody()
        directory = world.tree / 'var/lib/dosplex/journal'
        with self.owned_by(directory, 4242):
            code, _, err = self.in_process(world, 'status')
        self.assertEqual(code, 1)
        self.assertIn('owned by somebody (uid 4242)', err)
        self.assertIn('Fix: sudo chown root:root %s && sudo chmod 0700 %s' % (directory, directory), err)

    def test_install_reports_it_before_it_verifies_or_changes_anything(self):
        world = self.world_with_somebody()
        bundle = world.bundle(name='second', version='0.2.0-test')
        calls = len(world.calls())
        directory = world.tree / 'var/lib/dosplex'
        with self.owned_by(directory, 4242):
            code, out, err = self.in_process(world, 'install', bundle.path, '--manifest', bundle.sha256, '--no-start')
        self.assertEqual(code, 1)
        self.assertIn('owned by somebody (uid 4242)', err)
        self.assertIn('Fix: sudo chown root:root %s && sudo chmod 0755 %s' % (directory, directory), err)
        self.assertNotIn('Verified', out)                      # it did not even look at the bundle
        self.assertEqual(len(world.calls()), calls)            # and ran no command
        self.assertEqual(world.state()['version'], '0.1.0-test')

    def test_a_directory_that_is_open_to_others_gets_a_chmod_fix(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        directory = world.tree / 'var/lib/dosplex'
        directory.chmod(0o775)
        result = world.run('status')
        self.assertFailed(result, '%s has mode 0775; it must be 0755.' % directory)
        self.assertIn('Fix: sudo chmod 0755 %s' % directory, result.err)
        directory.chmod(0o755)
        world.journal_dir().chmod(0o750)
        result = world.run('status')
        self.assertIn('Fix: sudo chmod 0700 %s' % world.journal_dir(), result.err)

    def test_a_journal_file_that_root_does_not_own_names_its_owner_and_fix(self):
        world = self.world_with_somebody()
        record = world.journal_dir() / '0001-install.jsonl'
        record.chmod(0o644)
        result = world.run('status')
        self.assertFailed(result, 'is not a private root-owned file')
        self.assertIn('Fix: sudo chown root:root %s && sudo chmod 0600 %s' % (record, record), result.err)

# @@TESTS@@


# --- an install under the old name (DuplOS) ------------------------------------------------------------
# The sysroot gets what the old program left: /opt/duplos, /var/lib/duplos with its state, journal and data,
# and a stand-in for the old program (it only has to understand `stop`).
OLD_PROGRAM = '''#!%s
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(args[1]) if args[:1] == ['--sysroot'] else Path('/')
with open(os.environ['FAKE_STATE'] + '/old-program.log', 'a') as log:
    log.write(json.dumps(args) + '\\n')
if os.environ.get('OLD_PROGRAM_FAIL'):
    print('stop failed: the compositor will not leave', file=sys.stderr)
    sys.exit(3)
for trial in (root / 'run').glob('duplos-trial-*'):
    shutil.rmtree(trial)
'''
OLD_TRIAL = 'duplos-trial-' + '0123456789abcdef' * 2


def old_journal_line(index, kind, **fields):
    return json.dumps(dict(fields, kind=kind, i=index, t='2026-10-01T10:00:00Z'), sort_keys=True) + '\n'


def add_old_install(world, guest_origin='created', trial=False, program=True, interrupted=False, state=True,
                    stripped=('audio',), guest=True):
    """What `duplos install` left, as of the last DuplOS release."""
    t = world.tree
    old_guest = {'user': 'guest', 'uid': 1500, 'gid': 1500, 'home': '/home/guest', 'origin': guest_origin,
                 'home_mode_before': None, 'stripped_groups': list(stripped)}
    if guest:
        add_guest(world)
    release = t / 'opt/duplos/releases/0.1.0-old'
    (release / 'bin').mkdir(parents=True)
    (release / 'bin/duplos-compositor').write_text('old compositor\n')
    (t / 'opt/duplos/current').symlink_to('releases/0.1.0-old')
    (t / 'opt/duplos/bin').mkdir()
    (t / 'opt/duplos/bin/duplos.previous').write_text('older\n')
    if program:
        old = t / 'opt/duplos/bin/duplos'
        old.write_text(OLD_PROGRAM % sys.executable)
        old.chmod(0o755)
    var = t / 'var/lib/duplos'
    for name, mode in (('', 0o755), ('journal', 0o700)):
        (var / name).mkdir(parents=True, exist_ok=True)
        (var / name).chmod(mode)
    world.write('var/lib/duplos/sides/left/mode', 'gaming\n')
    world.write('var/lib/duplos/sides/right/mode', 'desktop\n')
    world.write('var/lib/duplos/controllers/pad-1.json', '{"owner": "right"}\n')
    world.write('var/lib/duplos/input-policy.json', '{"default": "left"}\n')
    world.write('var/lib/duplos/journal/1000/wine-modelist.json', '{"restore": true}\n')
    defaults = {'login_mode': 'gaming', 'desktop_session': 'plasma.desktop', 'host_mode': 'desktop',
                'host_desktop': 'plasma.desktop', 'main_user': 'deck', 'main_uid': 1000, 'recorded': 'before-duplos'}
    lines = [old_journal_line(0, 'begin', op='install', schema=1, cli='0.2.0'),
             old_journal_line(1, 'intent', step='guest', data={'action': 'create'}),
             old_journal_line(2, 'done', step='guest', data=dict(old_guest, action='create'))]
    if not interrupted:
        lines.append(old_journal_line(3, 'end', outcome='complete'))
    journal = var / 'journal/0001-install.jsonl'
    journal.write_text(''.join(lines))
    journal.chmod(0o600)
    if state:
        record = {'schema': 1, 'version': '0.1.0-old', 'release': 'releases/0.1.0-old', 'commit': COMMIT,
                  'manifest_sha256': '1' * 64, 'main_user': 'deck', 'guest': old_guest, 'defaults': defaults,
                  'controllers': {'main': '/dev/hidraw4', 'guest': '/dev/hidraw5'}, 'output_mode': [1920, 1080, 60],
                  'previous': None, 'installed_utc': '2026-10-01T10:00:00Z', 'trial': None, 'session': None,
                  'untested': [], 'untested_observed': None}
        if trial:
            record['trial'] = {'root': str(t / 'run' / OLD_TRIAL), 'started_utc': '2026-10-01T10:05:00Z', 'tx': '0002-start'}
        (var / 'state.json').write_text(json.dumps(record))
        (var / 'state.json').chmod(0o644)
    if trial:
        world.write('run/%s/transaction.json' % OLD_TRIAL, json.dumps({'phase': 'started'}))
    world.reset_baseline()
    return defaults


class MigrationFromDuplos(Base):
    def installed(self, world, *extra):
        result = world.install(world.bundle(), '--no-start', *extra)
        self.assertOk(result)
        return result

    def test_an_old_install_is_replaced_and_its_data_moves_across(self):
        world = self.world()
        defaults = add_old_install(world)
        users = world.users()
        result = self.installed(world)
        self.assertIn('Found an install under the old name, DuplOS 0.1.0-old', result.out)
        tree = world.tree
        self.assertFalse((tree / 'opt/duplos').exists())
        self.assertFalse((tree / 'var/lib/duplos').exists())
        self.assertEqual(os.readlink(tree / 'opt/dosplex/current'), 'releases/0.1.0-test')
        var = tree / 'var/lib/dosplex'
        self.assertEqual((var / 'sides/left/mode').read_text(), 'gaming\n')
        self.assertEqual((var / 'sides/right/mode').read_text(), 'desktop\n')
        self.assertEqual((var / 'controllers/pad-1.json').read_text(), '{"owner": "right"}\n')
        self.assertEqual((var / 'input-policy.json').read_text(), '{"default": "left"}\n')
        self.assertEqual((var / 'journal/1000/wine-modelist.json').read_text(), '{"restore": true}\n')
        old_state = json.loads((var / 'legacy-duplos/state.json').read_text())
        self.assertEqual(old_state['version'], '0.1.0-old')
        self.assertTrue((var / 'legacy-duplos/journal/0001-install.jsonl').is_file())
        self.assertEqual(stat.S_IMODE((var / 'legacy-duplos').stat().st_mode), 0o700)
        self.assertEqual(world.users(), users)                       # Guest was kept, not created again
        self.assertEqual(world.commands('useradd'), [])
        state = world.state()
        self.assertEqual(state['version'], '0.1.0-test')
        self.assertEqual(state['migrated_from']['product'], 'DuplOS')
        self.assertEqual(state['migrated_from']['version'], '0.1.0-old')
        self.assertEqual(state['defaults'], defaults)                # the defaults from before DuplOS, not today's
        self.assertEqual(state['controllers'], {'main': '/dev/hidraw4', 'guest': '/dev/hidraw5'})
        self.assertEqual(state['output_mode'], [1920, 1080, 60])
        self.assertEqual(state['guest']['origin'], 'created')
        self.assertEqual(state['guest']['stripped_groups'], ['audio'])
        self.assertEqual(sorted(world.journal()), ['0001-install.jsonl', '0002-migrate.jsonl'])
        self.assertIn('migrated from DuplOS 0.1.0-old', world.run('status').out)
        self.assertEqual(world.journal()['0002-migrate.jsonl'][-1]['outcome'], 'complete')

    def test_guest_stays_removable_after_the_migration(self):
        world = self.world()
        add_old_install(world)
        self.installed(world)
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))
        self.assertNotIn('guest:', world.users()[0])

    def test_an_adopted_guest_keeps_its_origin_under_the_new_name(self):
        world = self.world()
        add_old_install(world, guest_origin='adopted-duplos')
        self.installed(world)
        self.assertEqual(world.state()['guest']['origin'], 'adopted-dosplex')

    def test_a_guest_kept_by_the_old_uninstall_is_still_dosplex_s_guest(self):
        world = self.world()
        add_old_install(world, state=False, stripped=())
        os.unlink(world.tree / 'opt/duplos/current')            # uninstalled, Guest and the journal kept
        self.installed(world)
        self.assertEqual(world.state()['guest']['origin'], 'created')
        self.assertFalse((world.tree / 'opt/duplos').exists())

    def test_a_running_session_is_stopped_with_the_old_program_before_anything_else(self):
        world = self.world()
        add_old_install(world, trial=True)
        result = self.installed(world)
        self.assertIn('A DuplOS session is running', result.out)
        calls = [json.loads(line) for line in (world.fake / 'old-program.log').read_text().splitlines()]
        self.assertEqual(calls, [['--sysroot', str(world.tree), 'stop']])
        self.assertFalse((world.tree / 'run' / OLD_TRIAL).exists())
        self.assertIsNone(world.state()['trial'])

    def test_a_session_that_will_not_stop_refuses_the_install_and_changes_nothing_else(self):
        world = self.world()
        add_old_install(world, trial=True)
        before = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start', OLD_PROGRAM_FAIL='1')
        self.assertFailed(result, 'could not stop the DuplOS session')
        self.assertIn('root-session-trial.py recover --root', result.text)
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertFalse((world.tree / 'opt/dosplex').exists())

    def test_a_session_without_its_old_program_names_the_recovery_command(self):
        world = self.world()
        add_old_install(world, trial=True, program=False)
        before = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'the old program that could stop it is not there')
        self.assertIn('sudo python3 %s/run/%s/root-session-trial.py recover' % (world.tree, OLD_TRIAL), result.text)
        self.assertEqual(world.snapshot(exclude=()), before)

    def test_an_interrupted_operation_of_the_old_program_refuses_the_install(self):
        world = self.world()
        add_old_install(world, interrupted=True)
        before = world.snapshot(exclude=())
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'was interrupted')
        self.assertIn('opt/duplos/bin/duplos repair', result.text)
        self.assertEqual(world.snapshot(exclude=()), before)

    def test_a_symlinked_old_directory_is_not_followed(self):
        world = self.world()
        add_old_install(world)
        elsewhere = world.root / 'elsewhere'
        shutil.move(str(world.tree / 'opt/duplos'), str(elsewhere))
        (world.tree / 'opt/duplos').symlink_to(elsewhere)
        result = world.install(world.bundle(), '--no-start')
        self.assertFailed(result, 'is not a real directory')
        self.assertTrue((elsewhere / 'bin/duplos').is_file())

    def test_a_failed_install_leaves_the_old_install_as_it_was(self):
        world = self.world()
        add_old_install(world)
        before = world.snapshot(exclude=())
        bundle = world.bundle(tested_versions=tested(gamescope=['9.9.9']))
        self.assertFailed(world.install(bundle, '--no-start'))
        self.assertEqual(world.snapshot(exclude=()), before)

    def test_what_the_new_name_has_already_is_not_overwritten_and_is_named(self):
        world = self.world()
        add_old_install(world)
        (world.tree / 'var/lib/dosplex/sides/left').mkdir(parents=True)
        (world.tree / 'var/lib/dosplex/sides/left/mode').write_text('desktop\n')
        (world.tree / 'var/lib/dosplex').chmod(0o755)
        (world.tree / 'var/lib/dosplex/sides').chmod(0o755)
        world.reset_baseline()
        result = self.installed(world)
        self.assertEqual((world.tree / 'var/lib/dosplex/sides/left/mode').read_text(), 'desktop\n')
        self.assertTrue((world.tree / 'var/lib/duplos/sides/left/mode').is_file())
        self.assertIn('Left in place', result.out)
        self.assertIn('/var/lib/duplos/sides', result.out)
        self.assertEqual((world.tree / 'var/lib/dosplex/input-policy.json').read_text(), '{"default": "left"}\n')

    def test_finishing_twice_is_harmless(self):
        world = self.world()
        add_old_install(world)
        self.installed(world)
        with mock.patch.dict(os.environ, {'DOSPLEX_TEST_ROOT_UID': str(UID)}):
            env = dosplex.Env(world.tree)
            legacy = {'version': '0.1.0-old', 'trials': []}
            self.assertEqual(dosplex.finish_legacy(env, legacy), {'moved': [], 'kept': []})

    def test_status_and_start_on_an_old_install_say_what_to_run(self):
        world = self.world()
        add_old_install(world)
        status = world.run('status')
        self.assertOk(status)
        self.assertIn('An install under the old name, DuplOS, is on this device', status.out)
        self.assertTrue(json.loads(world.run('status', '--json').out)['legacy'])
        start = world.run('start', *world.controllers())
        self.assertFailed(start, 'it replaces the DuplOS install that is on this device')
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_traces_of_the_older_installer_under_the_old_name_are_named_not_removed(self):
        world = self.world()
        world.write('etc/sudoers.d/zz-duplos-dev', 'deck ALL=(ALL) NOPASSWD: ALL\n')
        world.write('etc/duplos/remote-input.token', 'x\n')
        world.write('etc/systemd/system/duplos-compositor.service', '[Unit]\n')
        world.reset_baseline()
        result = world.run('preflight')
        self.assertIn('/etc/sudoers.d/zz-duplos-dev', result.text)
        self.assertIn('/etc/duplos', result.text)
        self.assertIn('/etc/systemd/system/duplos-compositor.service', result.text)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_the_old_program_is_only_ever_asked_to_stop(self):
        world = self.world()
        add_old_install(world)
        old = world.tree / 'opt/duplos/bin/duplos'
        with mock.patch.dict(os.environ, {'DOSPLEX_TEST_ROOT_UID': str(UID)}):
            env = dosplex.Env(world.tree)
            python = sys.executable
            dosplex.check_command(env, [python, str(old), '--sysroot', str(world.tree), 'stop'])
            dosplex.check_command(env, [python, str(old), 'stop'])
            for bad in (['uninstall'], ['stop', '--remove-guest'], ['start'], ['--sysroot', str(world.tree), 'repair']):
                with self.assertRaises(dosplex.PolicyError):
                    dosplex.check_command(env, [python, str(old)] + bad)


class FakeCompositor:
    """The compositor's control socket as the CLI sees it: a SOCK_SEQPACKET socket in the trial's runtime directory."""

    def __init__(self, runtime, answer=None, mode=0o600):
        import socket
        import threading
        self.requests, self.answer, self.layout = [], answer, 2
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        directory = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY)
        self.server.bind('/proc/self/fd/%d/control.sock' % directory)       # the sysroot path is too long for a socket name
        os.close(directory)
        os.chmod(runtime / 'control.sock', mode)
        self.server.listen(4)
        self.server.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        import socket
        while self.running:
            try:
                client, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with client:
                request = client.recv(64)
                self.requests.append(request)
                if self.answer is not None:
                    client.send(self.answer)
                    continue
                if request[:5] == b'DPXC\x01' and request[5] == 2 and request[6] <= 100:
                    self.brightness = max(request[6], 5)
                    client.send(b'DPXR' + bytes([1, 0, self.brightness, 0]))
                    continue
                if request[:5] == b'DPXC\x01' and request[5] == 1 and 1 <= request[6] <= 4:
                    self.layout = request[6]
                client.send(b'DPXR' + bytes([1, 0, self.layout, 0]))

    def close(self):
        self.running = False
        self.thread.join()
        self.server.close()


CONTROLLER_A, CONTROLLER_B, CONTROLLER_C = ('a' * 32, 'b' * 32, 'c' * 32)
STATUS_KEYS = {'program_version', 'installed', 'version', 'commit', 'interrupted', 'in_progress', 'running', 'layout',
               'brightness', 'sides', 'controllers', 'warnings', 'update', 'operation'}
OPERATION_KEYS = {'id', 'action', 'state', 'code', 'message', 'created_utc', 'started_utc', 'finished_utc', 'steps',
                  'notes'}


class Control(Base):
    """`dosplex control`: the versioned interface for user interfaces (docs/control-interface.md)."""

    def ctl(self, world, *words, expect=None, **extra):
        """Run an action; check the envelope and that the exit status belongs to the code."""
        result = world.run('control', *words, **extra)
        lines = result.out.splitlines()
        self.assertEqual(len(lines), 1, 'stdout must be exactly one JSON object: %r' % result.out)
        answer = json.loads(lines[0])
        self.assertEqual(set(answer), {'api', 'ok', 'code', 'message', 'data'})
        self.assertEqual(answer['api'], 1)
        self.assertIsInstance(answer['message'], str)
        self.assertIsInstance(answer['data'], dict)
        self.assertEqual(answer['ok'], answer['code'] in ('ok', 'accepted'))
        self.assertIn(answer['code'], dosplex.CONTROL_CODES)
        self.assertEqual(result.code, dosplex.CONTROL_CODES[answer['code']], result.text)
        if expect is not None:
            self.assertEqual(answer['code'], expect, result.text)
        return answer

    def installed(self, **options):
        world = self.world(**options)
        self.assertOk(world.install(world.bundle(), *world.controllers(), '--no-start'))
        return world

    def running(self, **options):
        world = self.world(**options)
        self.assertOk(world.install(world.bundle(), *world.controllers()))
        return world

    def trial(self, world):
        (root,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        return root

    def finish(self, world, answer, expect='ok', timeout='60'):
        done = self.ctl(world, 'wait', answer['data']['operation']['id'], '--timeout', timeout, expect=expect)
        return done['data']['operation']

    # --- Steam updates itself between two starts from the couch -----------------------------------
    def steam_client(self, world, version):
        world.write('home/deck/.local/share/Steam/package/steam_client_steamdeck_stable_ubuntu12.manifest',
                    '"steamdeck_stable"\n{\n\t"version"\t\t"%s"\n}\n' % version)

    def test_a_start_from_the_library_survives_a_steam_client_update_and_status_carries_a_warning(self):
        # The entry in Steam's library asks through this interface. Its user holds a controller: a refusal that
        # only --allow-untested in a terminal answers would end the product at Steam's next update of itself.
        world = self.installed()
        self.steam_client(world, '1760000000')                       # not in the tested set
        self.assertFailed(world.run('start'), 'not in the tested set', code=3)      # typed by hand: still strict
        self.finish(world, self.ctl(world, 'start', expect='accepted'))
        self.assertEqual(len(world.events('trial.start')), 1)
        status = world.run('status')
        self.assertIn('Warning: Steam client 1760000000 is not in the tested set', status.out)
        self.assertIn('Dosplex started anyway, because Steam updates itself', status.out)

    def test_a_start_from_the_library_still_refuses_another_steamos(self):
        world = self.installed()
        world.write('etc/os-release', 'ID=steamos\nVERSION_ID=9.9.10\nBUILD_ID=1\n')
        answer = self.ctl(world, 'start')
        self.assertFalse(answer['ok'], answer)
        self.assertIn('not in the tested set', answer['message'])
        self.assertEqual(world.events('trial.start'), [])

    # --- the envelope and the allow-list ---------------------------------------------------------
    def test_every_malformed_request_is_a_bad_request_in_json_and_changes_nothing(self):
        world = self.world()
        good = 'a' * 32
        for words in ((), ('nothing',), ('Status',), ('status', 'extra'), ('start', '--allow-untested'),
                      ('stop', 'now'), ('devices', '1'), ('layout',), ('layout', 'diagonal'), ('layout', 'single', 'x'),
                      ('layout', 'Vertical'), ('assign',), ('assign', good), ('assign', good, 'host'),
                      ('assign', 'A' * 32, 'main'), ('assign', 'a' * 31, 'main'), ('assign', good + '0', 'main'),
                      ('assign', '../' + 'a' * 29, 'main'), ('assign', good, 'main', 'extra'), ('forget',),
                      ('forget', 'A' * 32), ('forget', '../' + 'a' * 29), ('forget', good, good), ('eject',), ('eject', 'card'),
                      ('eject', '/run/media/../x'), ('eject', '/a', '/b'), ('eject', '/a;id\n'), ('operation',),
                      ('operation', 'x'), ('operation', good, good), ('wait',), ('wait', 'x'),
                      ('wait', good, '--timeout'), ('wait', good, '--timeout', '-1'), ('wait', good, '--timeout', 'abc'),
                      ('wait', good, '--timeout', '1801'), ('wait', good, '--timeout=1e3'), ('wait', good, '--timeout', '5', 'x'),
                      ('wait', good, '--bogus', '5'), ('status; id',), ('$(id)',), ('--help',), ('layout', 'vertical\nsingle'),
                      ('layout', '\x7f'), ('layout', 'v' * 5000), ('brightness',), ('brightness', '101'), ('brightness', '-1'),
                      ('brightness', '50.5'), ('brightness', 'half'), ('brightness', '0050'), ('brightness', '50', '60'),
                      ('brightness', ''), ('brightness', '5\n0')):
            with self.subTest(words=[w[:20] for w in words]):
                answer = self.ctl(world, *words, expect='bad-request')
                self.assertEqual(answer['data'], {})
                self.assertLess(len(answer['message']), 300)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        self.assertEqual(world.calls(), [])

    def test_the_json_flag_is_accepted_anywhere_and_changes_nothing(self):
        world = self.world()
        reference = self.ctl(world, 'status')['data']
        for words in (('status', '--json'), ('--json', 'status')):
            self.assertEqual(self.ctl(world, *words)['data'], reference)

    def test_control_needs_root(self):
        world = self.installed()
        for words in (('status',), ('start',), ('stop',), ('devices',), ('layout', 'single'), ('assign', 'a' * 32, 'main'),
                      ('forget', 'a' * 32), ('eject', '/run/media/deck/x'), ('operation', 'a' * 32), ('wait', 'a' * 32)):
            result = world.run('control', *words, root=False)
            answer = json.loads(result.out)
            self.assertEqual((answer['code'], result.code), ('permission-denied', 10), words)
            self.assertFalse(answer['ok'])

    def test_exit_statuses_are_documented_and_stable(self):
        self.assertEqual(dosplex.CONTROL_CODES, {
            'ok': 0, 'accepted': 0, 'failed': 1, 'bad-request': 2, 'refused': 3, 'not-installed': 3, 'interrupted': 4,
            'busy': 5, 'already-running': 6, 'not-running': 7, 'timeout': 8, 'not-found': 9, 'permission-denied': 10,
            'unavailable': 11})
        page = (ROOT / 'docs/control-interface.md').read_text()
        for code in dosplex.CONTROL_CODES:
            self.assertIn('`%s`' % code, page)
        for action in dosplex.CONTROL_ACTIONS:
            self.assertIn('dosplex control ' + action, page)
        self.assertNotIn('private' + '/', page)          # the page is meant to be publishable

    def test_an_unexpected_error_is_still_one_json_object(self):
        world = self.installed()
        (world.tree / 'var/lib/dosplex/state.json').write_text('{not json')
        answer = self.ctl(world, 'status', expect='failed')
        self.assertIn('damaged', answer['message'])
        self.assertNotIn('Traceback', answer['message'])

    # --- status ----------------------------------------------------------------------------------
    def test_status_before_install(self):
        world = self.world()
        data = self.ctl(world, 'status', expect='ok')['data']
        self.assertEqual(set(data), STATUS_KEYS)
        self.assertEqual((data['installed'], data['version'], data['running'], data['layout'], data['sides'],
                          data['controllers'], data['operation']), (False, None, False, None, [], [], None))
        self.assertEqual(data['program_version'], dosplex.CLI_VERSION)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_status_installed_and_stopped(self):
        world = self.installed()
        data = self.ctl(world, 'status', expect='ok')['data']
        self.assertEqual(set(data), STATUS_KEYS)
        self.assertEqual((data['installed'], data['version'], data['running'], data['sides'], data['layout']),
                         (True, '0.1.0-test', False, [], None))
        self.assertEqual(data['update'], {'checked': False, 'available': None, 'latest': None, 'previous_version': None})
        self.assertEqual(data['commit'], COMMIT)

    def test_status_of_a_running_session_reports_sides_modes_layout_and_controllers(self):
        world = self.running()
        root = self.trial(world)
        (root / 'runtime/state.json').write_text(json.dumps({'layout': 'horizontal', 'brightness': 65, 'left': {}, 'right': {}}))
        for side, mode in (('left', 'gaming'), ('right', 'desktop')):
            path = root / 'sides' / side / 'session.json'
            path.write_text(json.dumps(dict(json.loads(path.read_text()), mode=mode)))
        self.add_controllers(root)
        data = self.ctl(world, 'status', expect='ok')['data']
        self.assertEqual(set(data), STATUS_KEYS)
        self.assertTrue(data['running'])
        self.assertEqual(data['layout'], 'horizontal')
        self.assertEqual(data['brightness'], 65)
        self.assertEqual(data['sides'], [
            {'name': 'main', 'side': 'left', 'owner': 'deck', 'lifecycle': 'running', 'mode': 'gaming'},
            {'name': 'guest', 'side': 'right', 'owner': 'guest', 'lifecycle': 'running', 'mode': 'desktop'}])
        self.assertEqual(data['controllers'], [
            {'id': CONTROLLER_A, 'name': 'Pad A', 'side': 'main', 'connected': True, 'routing_error': None},
            {'id': CONTROLLER_B, 'name': 'Pad B', 'side': 'guest', 'connected': True, 'routing_error': 'not-routed'},
            {'id': CONTROLLER_C, 'name': 'Pad C', 'side': 'unassigned', 'connected': False, 'routing_error': None}])
        self.assertEqual(data['warnings'], [])
        self.assertIsNone(data['operation'])
        # An unknown mode or layout in a file never leaks through as free text.
        for bad in (-1, 0, 101, True, '70', None):                     # no panel (-1) or nonsense: null, never free text
            (root / 'runtime/state.json').write_text(json.dumps({'layout': 'horizontal', 'brightness': bad}))
            self.assertIsNone(self.ctl(world, 'status')['data']['brightness'], bad)
        (root / 'runtime/state.json').write_text(json.dumps({'layout': 'sideways'}))
        path = root / 'sides/left/session.json'
        path.write_text(json.dumps(dict(json.loads(path.read_text()), mode='<script>')))
        data = self.ctl(world, 'status')['data']
        self.assertEqual((data['layout'], data['sides'][0]['mode']), (None, None))

    def test_status_reports_a_dead_compositor_and_an_interrupted_operation(self):
        world = self.running()
        for unit in (world.fake / 'units').glob('dosplex-compositor-*'):
            unit.unlink()
        data = self.ctl(world, 'status')['data']
        # A dead compositor is not "running with a warning": status tests the unit, not the recorded phase (TRL-03).
        self.assertFalse(data['running'])
        self.assertEqual(data['warnings'], [])
        world = self.installed()
        self.assertFailed(world.run('start', '--sample-seconds', '0.3', DOSPLEX_TEST_CRASH_AT='start:done:trial'))
        data = self.ctl(world, 'status', expect='ok')['data']
        self.assertEqual(data['interrupted']['op'], 'start')

    # --- devices and assign ----------------------------------------------------------------------
    def add_controllers(self, root, age=0):
        """Three saved controllers; A is routed to Main, B was asked for Main but still sits with Guest."""
        # The owners are kept outside the trial (they outlive a session): /var/lib/dosplex/controllers of the sysroot.
        prefs = root.parents[1] / 'var/lib/dosplex/controllers'
        prefs.mkdir(mode=0o700, exist_ok=True)
        for key, name, target in ((CONTROLLER_A, 'Pad A', 'main'), (CONTROLLER_B, 'Pad B', 'main'),
                                  (CONTROLLER_C, 'Pad C', 'unassigned')):
            (prefs / (key + '.json')).write_text(json.dumps({'name': name, 'target': target}))
            (prefs / (key + '.json')).chmod(0o600)
        self.publish(root, [{'id': CONTROLLER_A, 'name': 'Pad A', 'connected': True, 'active': True, 'target': 'main'},
                            {'id': CONTROLLER_B, 'name': 'Pad B', 'connected': True, 'active': False, 'target': 'guest'},
                            {'id': CONTROLLER_C, 'name': 'Pad C', 'connected': False, 'active': False, 'target': 'unassigned'}],
                     age=age)
        return prefs

    def publish(self, root, devices, age=0, name='controllers.json'):
        path = root / 'runtime' / name
        path.write_text(json.dumps({'updated': time.time() - age, 'assignment_preview': 2, 'devices': devices}))
        path.chmod(0o600)

    def test_devices_lists_receivers_slots_and_controllers(self):
        world = self.running()
        self.add_controllers(self.trial(world))
        data = self.ctl(world, 'devices', expect='ok')['data']
        self.assertEqual(set(data), {'running', 'inventory', 'receivers', 'controllers'})
        self.assertEqual((data['running'], data['inventory']), (True, 'fresh'))
        self.assertEqual(data['receivers'], [
            {'node': '/dev/hidraw4', 'vendor': '28de', 'product': '1304', 'role': 'main'},
            {'node': '/dev/hidraw5', 'vendor': '28de', 'product': '1304', 'role': 'guest'}])
        self.assertEqual(data['controllers'][0], {
            'id': CONTROLLER_A, 'name': 'Pad A', 'side': 'main', 'requested_side': 'main', 'connected': True,
            'active': True, 'routing_error': None})
        by_id = {c['id']: c for c in data['controllers']}
        self.assertEqual((by_id[CONTROLLER_B]['side'], by_id[CONTROLLER_B]['requested_side'],
                          by_id[CONTROLLER_B]['routing_error']), ('guest', 'main', 'not-routed'))
        self.assertEqual((by_id[CONTROLLER_C]['connected'], by_id[CONTROLLER_C]['side']), (False, 'unassigned'))

    def test_a_native_row_replaces_the_generic_row_and_a_stale_list_shows_nothing_connected(self):
        world = self.running()
        root = self.trial(world)
        self.add_controllers(root)
        self.publish(root, [{'id': CONTROLLER_C, 'name': 'Native C', 'connected': True, 'target': 'guest'}],
                     name='controllers-native.json')
        by_id = {c['id']: c for c in self.ctl(world, 'devices')['data']['controllers']}
        self.assertEqual((by_id[CONTROLLER_C]['name'], by_id[CONTROLLER_C]['side'], by_id[CONTROLLER_C]['connected']),
                         ('Native C', 'guest', True))
        for name in ('controllers.json', 'controllers-native.json'):
            self.publish(root, [{'id': CONTROLLER_A, 'connected': True, 'target': 'guest'}], age=60, name=name)
        data = self.ctl(world, 'devices')['data']
        self.assertEqual(data['inventory'], 'stale')
        self.assertFalse(any(c['connected'] for c in data['controllers']))
        self.assertEqual({c['id']: c['side'] for c in data['controllers']},
                         {CONTROLLER_A: 'main', CONTROLLER_B: 'main', CONTROLLER_C: 'unassigned'})

    def test_a_generic_pad_is_listed_by_name_and_can_be_moved_like_the_others(self):
        """A Bluetooth or USB pad (backend generic-pad): its record and its row look like any controller's to the tools."""
        world = self.running()
        root = self.trial(world)
        prefs = self.add_controllers(root)
        pad = 'd5' * 16
        (prefs / (pad + '.json')).write_text(json.dumps({'name': 'DualSense Wireless Controller', 'target': 'guest',
                                                         'backend': 'generic-pad', 'transport': 'bluetooth'}))
        (prefs / (pad + '.json')).chmod(0o600)
        self.publish(root, [{'id': pad, 'name': 'DualSense Wireless Controller', 'connected': True, 'active': False,
                             'target': 'guest', 'requested': 'guest', 'backend': 'generic-pad', 'routing_error': ''}],
                     name='controllers-native.json')
        by_id = {c['id']: c for c in self.ctl(world, 'devices', expect='ok')['data']['controllers']}
        self.assertEqual((by_id[pad]['name'], by_id[pad]['side'], by_id[pad]['connected'], by_id[pad]['routing_error']),
                         ('DualSense Wireless Controller', 'guest', True, None))
        answer = self.ctl(world, 'assign', pad, 'main', expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='0.2')
        self.assertEqual(answer['data'], {'id': pad, 'side': 'main', 'previous_side': 'guest', 'applied': False})
        self.assertEqual(json.loads((prefs / 'assignments.json').read_text()), {'assignments': {pad: 'main'}})
        self.assertEqual(json.loads((prefs / (pad + '.json')).read_text())['backend'], 'generic-pad', 'the pad keeps its record')

    def test_devices_untrusted_inventory_files_are_ignored(self):
        world = self.running()
        root = self.trial(world)
        self.add_controllers(root)
        (root / 'runtime/controllers.json').chmod(0o666)               # anyone could have written it
        data = self.ctl(world, 'devices')['data']
        self.assertEqual(data['inventory'], 'missing')
        self.assertFalse(any(c['connected'] for c in data['controllers']))

    def test_devices_when_stopped_lists_receivers_only(self):
        world = self.installed()
        data = self.ctl(world, 'devices', expect='ok')['data']
        self.assertEqual((data['running'], data['inventory'], data['controllers']), (False, 'not-running', []))
        self.assertEqual(len(data['receivers']), 2)

    def test_assign_writes_the_record_the_devices_tab_writes(self):
        world = self.running()
        prefs = self.add_controllers(self.trial(world))
        # A preview lease or a handoff left by the Devices tab is replaced, as the tab itself does.
        (prefs / 'assignments.json').write_text(json.dumps({
            'assignments': {CONTROLLER_C: 'main'}, 'handoff': [CONTROLLER_A, CONTROLLER_B],
            'preview': {'assignments': {CONTROLLER_A: 'guest'}, 'expires': 1, 'boot_id': 'x'}}))
        (prefs / 'assignments.json').chmod(0o600)
        answer = self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='0.2')
        self.assertEqual(answer['data'], {'id': CONTROLLER_A, 'side': 'guest', 'previous_side': 'main', 'applied': False})
        record = json.loads((prefs / 'assignments.json').read_text())
        self.assertEqual(record, {'assignments': {CONTROLLER_C: 'main', CONTROLLER_A: 'guest'}})
        self.assertEqual(stat.S_IMODE((prefs / 'assignments.json').stat().st_mode), 0o600)
        self.assertFalse((prefs / 'assignments.request').exists())
        # The per-controller records belong to the broker and are not touched.
        self.assertEqual(json.loads((prefs / (CONTROLLER_A + '.json')).read_text()), {'name': 'Pad A', 'target': 'main'})
        by_id = {c['id']: c for c in self.ctl(world, 'devices')['data']['controllers']}
        self.assertEqual((by_id[CONTROLLER_A]['requested_side'], by_id[CONTROLLER_A]['routing_error']), ('guest', 'not-routed'))

    def test_assign_reports_applied_when_the_broker_follows(self):
        import threading
        world = self.running()
        root = self.trial(world)
        self.add_controllers(root)

        def broker():
            time.sleep(0.4)
            self.publish(root, [{'id': CONTROLLER_A, 'name': 'Pad A', 'connected': True, 'target': 'guest'}])
        thread = threading.Thread(target=broker)
        thread.start()
        answer = self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='5')
        thread.join()
        self.assertIs(answer['data']['applied'], True)
        # A controller that is asleep cannot be observed: applied is null, the request is still saved.
        answer = self.ctl(world, 'assign', CONTROLLER_C, 'main', expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='0.2')
        self.assertEqual((answer['data']['applied'], answer['data']['previous_side']), (None, 'unassigned'))

    def test_assign_to_the_side_it_already_has_writes_nothing(self):
        world = self.running()
        prefs = self.add_controllers(self.trial(world))
        answer = self.ctl(world, 'assign', CONTROLLER_A, 'main', expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='0.2')
        self.assertEqual((answer['data']['previous_side'], answer['data']['applied']), ('main', True))
        self.assertFalse((prefs / 'assignments.json').exists())

    def test_assign_refuses_unknown_controllers_a_stopped_session_and_untrusted_files(self):
        world = self.installed()
        self.ctl(world, 'assign', CONTROLLER_A, 'main', expect='not-running')
        world = self.running()
        root = self.trial(world)
        # The start made the controller directory (outside the trial: owners outlive a session), still empty.
        self.assertEqual(list((world.tree / 'var/lib/dosplex/controllers').iterdir()), [])
        self.ctl(world, 'assign', CONTROLLER_A, 'main', expect='not-found')
        prefs = self.add_controllers(root)
        self.ctl(world, 'assign', 'd' * 32, 'main', expect='not-found')
        (prefs / 'assignments.json').write_text('{}')
        (prefs / 'assignments.json').chmod(0o666)
        self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='failed')
        self.assertEqual((prefs / 'assignments.json').read_text(), '{}')
        (prefs / 'assignments.json').unlink()
        (prefs / 'assignments.json').symlink_to('/etc/passwd')
        self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='failed')
        (prefs / 'assignments.json').unlink()
        prefs.chmod(0o777)                                                           # the directory must be closed to others
        self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='unavailable')
        prefs.chmod(0o700)
        (prefs / (CONTROLLER_A + '.json')).chmod(0o666)                              # and so must the controller's record
        self.ctl(world, 'assign', CONTROLLER_A, 'guest', expect='not-found')

    # --- forget ----------------------------------------------------------------------------------
    def test_forget_asks_the_service_for_a_controller_that_is_off_and_reports_it_gone(self):
        import threading
        world = self.running()
        root = self.trial(world)
        prefs = self.add_controllers(root)

        def service():                         # (the routing service: it removes the record when it sees the request)
            for _ in range(100):
                if (prefs / (CONTROLLER_C + '.forget')).exists():
                    stat_info = (prefs / (CONTROLLER_C + '.forget')).stat()
                    self.assertEqual(stat.S_IMODE(stat_info.st_mode), 0o600)
                    (prefs / (CONTROLLER_C + '.forget')).unlink()
                    (prefs / (CONTROLLER_C + '.json')).unlink()
                    self.publish(root, [{'id': CONTROLLER_A, 'name': 'Pad A', 'connected': True, 'target': 'main'},
                                        {'id': CONTROLLER_B, 'name': 'Pad B', 'connected': True, 'target': 'guest'}])
                    return
                time.sleep(0.05)
        thread = threading.Thread(target=service)
        thread.start()
        answer = self.ctl(world, 'forget', CONTROLLER_C, expect='ok', DOSPLEX_TEST_ASSIGN_SECONDS='5')
        thread.join()
        self.assertEqual(answer['data'], {'id': CONTROLLER_C, 'name': 'Pad C'})
        self.assertEqual(sorted(p.name for p in prefs.iterdir()), [CONTROLLER_A + '.json', CONTROLLER_B + '.json'])
        self.assertNotIn(CONTROLLER_C, [c['id'] for c in self.ctl(world, 'devices')['data']['controllers']])

    def test_forget_refuses_a_connected_controller_and_asks_for_nothing(self):
        world = self.running()
        prefs = self.add_controllers(self.trial(world))
        answer = self.ctl(world, 'forget', CONTROLLER_A, expect='refused')
        self.assertIn('Turn it off or unplug it first', answer['message'])
        self.assertEqual(sorted(p.name for p in prefs.iterdir()), sorted(k + '.json' for k in (CONTROLLER_A, CONTROLLER_B, CONTROLLER_C)))

    def test_forget_when_the_service_does_not_answer_leaves_no_request_behind(self):
        world = self.running()
        prefs = self.add_controllers(self.trial(world))
        self.ctl(world, 'forget', CONTROLLER_C, expect='unavailable', DOSPLEX_TEST_ASSIGN_SECONDS='0.3')
        self.assertTrue((prefs / (CONTROLLER_C + '.json')).exists())
        self.assertFalse(list(prefs.glob('*.forget')))
        self.add_controllers(self.trial(world), age=60)               # an old inventory: nobody to ask
        self.ctl(world, 'forget', CONTROLLER_C, expect='unavailable', DOSPLEX_TEST_ASSIGN_SECONDS='0.3')
        self.assertFalse(list(prefs.glob('*.forget')))

    def test_forget_refuses_unknown_controllers_receivers_a_stopped_session_and_untrusted_files(self):
        world = self.installed()
        self.ctl(world, 'forget', CONTROLLER_C, expect='not-running')
        world = self.running()
        root = self.trial(world)
        prefs = self.add_controllers(root)
        self.ctl(world, 'forget', 'd' * 32, expect='not-found')
        (prefs / (CONTROLLER_C + '.json')).write_text(json.dumps({'name': 'Pad C', 'target': 'unassigned', 'backend': 'native'}))
        self.ctl(world, 'forget', CONTROLLER_C, expect='refused')
        (prefs / (CONTROLLER_C + '.json')).chmod(0o666)                              # a record that others can write
        self.ctl(world, 'forget', CONTROLLER_C, expect='not-found')
        (prefs / (CONTROLLER_C + '.json')).unlink()
        (prefs / (CONTROLLER_C + '.json')).symlink_to('/etc/passwd')                 # a link is not followed
        self.ctl(world, 'forget', CONTROLLER_C, expect='not-found')
        prefs.chmod(0o777)                                                           # the directory must be closed to others
        self.ctl(world, 'forget', CONTROLLER_C, expect='unavailable')
        prefs.chmod(0o700)
        self.assertFalse(list(prefs.glob('*.forget')))
        self.assertEqual(os.readlink(prefs / (CONTROLLER_C + '.json')), '/etc/passwd')

    # --- layout ----------------------------------------------------------------------------------
    def test_layout_sends_exactly_one_fixed_request_to_the_compositor(self):
        world = self.running()
        compositor = FakeCompositor(self.trial(world) / 'runtime')
        self.addCleanup(compositor.close)
        for layout, number in (('single', 1), ('vertical', 2), ('horizontal', 3), ('guest', 4)):
            answer = self.ctl(world, 'layout', layout, expect='ok')
            self.assertEqual(answer['data'], {'layout': layout})
            self.assertEqual(compositor.requests[-1], b'DPXC' + bytes([1, 1, number, 0]))
        self.assertEqual(len(compositor.requests), 4)

    def test_brightness_sends_one_fixed_request_and_reports_the_percent_in_effect(self):
        world = self.running()
        compositor = FakeCompositor(self.trial(world) / 'runtime')
        self.addCleanup(compositor.close)
        for percent, effective in (('70', 70), ('100', 100), ('0', 5), ('007', 7)):
            answer = self.ctl(world, 'brightness', percent, expect='ok')
            self.assertEqual(answer['data'], {'brightness': effective})
            self.assertEqual(compositor.requests[-1], b'DPXC' + bytes([1, 2, int(percent), 0]))
        self.assertEqual(len(compositor.requests), 4)

    def test_brightness_failures_have_their_own_codes(self):
        world = self.installed()
        self.ctl(world, 'brightness', '50', expect='not-running')
        world = self.running()
        runtime = self.trial(world) / 'runtime'
        self.ctl(world, 'brightness', '50', expect='unavailable')                    # an older compositor: no socket
        for answer, expect in ((b'DPXR' + bytes([1, 4, 0, 0]), 'unavailable'),       # a machine with no backlight
                               (b'DPXR' + bytes([1, 2, 0, 0]), 'permission-denied'),
                               (b'DPXR' + bytes([1, 1, 0, 0]), 'failed'),            # an older compositor: bad request
                               (b'DPXR' + bytes([1, 0, 0, 0]), 'failed'), (b'DPXR' + bytes([1, 0, 101, 0]), 'failed'),
                               (b'junk', 'failed')):
            compositor = FakeCompositor(runtime, answer=answer)
            try:
                self.ctl(world, 'brightness', '50', expect=expect)
            finally:
                compositor.close()
                (runtime / 'control.sock').unlink()

    def test_layout_failures_have_their_own_codes(self):
        world = self.installed()
        self.ctl(world, 'layout', 'single', expect='not-running')
        world = self.running()
        runtime = self.trial(world) / 'runtime'
        self.ctl(world, 'layout', 'single', expect='unavailable')                    # an older compositor: no socket
        for answer, expect in ((b'DPXR' + bytes([1, 2, 0, 0]), 'permission-denied'), (b'DPXR' + bytes([1, 1, 0, 0]), 'failed'),
                               (b'DPXR' + bytes([1, 3, 0, 0]), 'failed'), (b'DPXR' + bytes([1, 0, 3, 0]), 'failed'),
                               (b'junk', 'failed'), (b'DPXR' + bytes([9, 0, 1, 0]), 'failed')):
            compositor = FakeCompositor(runtime, answer=answer)
            try:
                self.ctl(world, 'layout', 'single', expect=expect)
            finally:
                compositor.close()
                (runtime / 'control.sock').unlink()
        compositor = FakeCompositor(runtime, mode=0o666)               # a socket other users can open is not trusted
        try:
            self.ctl(world, 'layout', 'single', expect='unavailable')
            self.assertEqual(compositor.requests, [])
        finally:
            compositor.close()
            (runtime / 'control.sock').unlink()
        (runtime / 'control.sock').write_text('not a socket')
        self.ctl(world, 'layout', 'single', expect='unavailable')

    # --- start, stop and the operation model -----------------------------------------------------
    def test_start_returns_at_once_with_an_operation_that_wait_can_follow(self):
        world = self.installed()
        (world.fake / 'hold').write_text('')
        answer = self.ctl(world, 'start', expect='accepted')
        operation = answer['data']['operation']
        self.assertEqual(set(operation), OPERATION_KEYS)
        self.assertRegex(operation['id'], r'^[0-9a-f]{32}$')
        self.assertEqual((operation['action'], operation['state']), ('start', 'queued'))
        # Detached: the service, not this command, does the work, as a background transient unit.
        (call,) = world.commands('systemd-run')
        self.assertNotIn('--wait', call['argv'])
        self.assertIn('--unit=dosplex-control-start-%s' % operation['id'][:8], call['argv'])
        self.assertEqual(call['argv'][call['argv'].index('--') + 1:][2:],
                         ['--sysroot', str(world.tree), '_control_op', operation['id']])
        self.assertEqual(world.events('trial.start'), [])
        got = self.ctl(world, 'operation', operation['id'], expect='ok')['data']['operation']
        self.assertEqual((got['id'], got['state']), (operation['id'], 'queued'))
        # While it waits, every other operation is busy, and status says what is going on.
        self.ctl(world, 'start', expect='busy')
        self.ctl(world, 'stop', expect='busy')
        self.assertEqual(self.ctl(world, 'status')['data']['operation'],
                         {'id': operation['id'], 'action': 'start', 'state': 'queued'})
        timed_out = self.ctl(world, 'wait', operation['id'], '--timeout', '0.5', expect='timeout')
        self.assertEqual(timed_out['data']['timeout_seconds'], 0.5)
        self.assertEqual(timed_out['data']['operation']['id'], operation['id'])
        (world.fake / 'hold').unlink()
        finished = self.finish(world, answer)
        self.assertEqual((finished['state'], finished['code']), ('succeeded', 'ok'))
        self.assertRegex(finished['message'], 'Dosplex is running')
        self.assertEqual([s['step'] for s in finished['steps']],
                         ['verify-release', 'preflight', 'host-mode', 'idle-steam', 'trial', 'controllers', 'running'])
        self.assertTrue(all(s['status'] == 'done' for s in finished['steps']))
        self.assertTrue(finished['started_utc'] and finished['finished_utc'])
        self.assertTrue(self.ctl(world, 'status')['data']['running'])
        self.assertIsNone(self.ctl(world, 'status')['data']['operation'])
        self.ctl(world, 'start', expect='already-running')
        # The records are root's and private.
        directory = world.tree / 'var/lib/dosplex/operations'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in directory.glob('*.json')}, {0o600})

    def test_stop_and_not_running(self):
        world = self.installed()
        self.ctl(world, 'stop', expect='not-running')
        self.assertEqual(world.commands('systemd-run'), [])
        world = self.running()
        finished = self.finish(world, self.ctl(world, 'stop', expect='accepted'))
        self.assertEqual((finished['action'], finished['state'], finished['code']), ('stop', 'succeeded', 'ok'))
        self.assertIn('Dosplex stopped', finished['message'])
        self.assertFalse(self.ctl(world, 'status')['data']['running'])
        self.ctl(world, 'stop', expect='not-running')

    def test_a_failed_start_is_reported_with_its_message_and_nothing_stays_busy(self):
        world = self.installed()
        answer = self.ctl(world, 'start', expect='accepted', FAKE_TRIAL_FAIL='before-root')
        finished = self.finish(world, answer, expect='failed')
        self.assertEqual((finished['state'], finished['code']), ('failed', 'failed'))
        self.assertIn('fake trial failed', finished['message'])
        self.assertFalse(self.ctl(world, 'status')['data']['running'])
        self.finish(world, self.ctl(world, 'start', expect='accepted'))

    def test_refusals_before_anything_is_launched(self):
        world = self.world()
        self.ctl(world, 'start', expect='not-installed')
        self.ctl(world, 'stop', expect='not-installed')
        world = self.installed()
        # An interrupted earlier operation has to be repaired first.
        self.assertFailed(world.run('start', '--sample-seconds', '0.3', DOSPLEX_TEST_CRASH_AT='start:done:trial'))
        calls = len(world.commands('systemd-run'))
        self.ctl(world, 'start', expect='interrupted')
        self.ctl(world, 'stop', expect='interrupted')
        self.assertEqual(len(world.commands('systemd-run')), calls)
        self.assertOk(world.run('repair'))

    def test_start_without_known_controller_slots_is_accepted(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        answer = self.ctl(world, 'start', expect='accepted')
        self.finish(world, answer)
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest']), (None, None))

    def test_a_command_line_operation_in_progress_makes_control_busy(self):
        world = self.installed()
        with mock.patch.dict(os.environ, {'DOSPLEX_TEST_ROOT_UID': str(UID)}):
            journal = dosplex.Journal.open(dosplex.Env(world.tree), lock=True)
        try:
            self.ctl(world, 'start', expect='busy')
        finally:
            journal.close()
        self.assertEqual(world.commands('systemd-run'), [])

    def test_a_service_that_dies_without_a_result_is_failed_or_interrupted(self):
        world = self.installed()
        (world.fake / 'hold').write_text('')
        answer = self.ctl(world, 'start', expect='accepted')
        op_id = answer['data']['operation']['id']
        (world.fake / 'units' / ('dosplex-control-start-' + op_id[:8])).unlink()       # the service vanished
        got = self.ctl(world, 'operation', op_id, expect='ok')['data']['operation']
        self.assertEqual((got['state'], got['code']), ('failed', 'failed'))
        self.assertIn('without a result', got['message'])
        self.ctl(world, 'wait', op_id, expect='failed')
        (world.fake / 'hold').unlink()
        # A crash in the middle of the work leaves the journal open: that is `interrupted`.
        answer = self.ctl(world, 'start', expect='accepted', DOSPLEX_TEST_CRASH_AT='start:done:trial')
        finished = self.finish(world, answer, expect='interrupted')
        self.assertEqual((finished['state'], finished['code']), ('failed', 'interrupted'))
        self.assertIn('repair', finished['message'])
        self.ctl(world, 'start', expect='interrupted')

    def test_unknown_operations_and_old_records(self):
        world = self.installed()
        self.ctl(world, 'operation', 'f' * 32, expect='not-found')
        self.ctl(world, 'wait', 'f' * 32, expect='not-found')
        world = self.running()
        directory = world.tree / 'var/lib/dosplex/operations'
        directory.mkdir(mode=0o700)
        for number in range(30):
            op_id = '%032x' % (number + 1)
            path = directory / (op_id + '.json')
            path.write_text(json.dumps({'id': op_id, 'action': 'stop', 'state': 'succeeded', 'code': 'ok', 'message': 'x',
                                        'created_utc': '2026-01-%02dT00:00:00Z' % (number + 1), 'unit': 'dosplex-control-stop-x'}))
            path.chmod(0o600)
        self.finish(world, self.ctl(world, 'stop', expect='accepted'))
        self.assertLessEqual(len(list(directory.glob('*.json'))), dosplex.OPERATIONS_KEPT + 1)
        self.assertFalse((directory / ('%032x.json' % 1)).exists())                  # the oldest went first

    def test_untrusted_records_are_not_believed(self):
        world = self.installed()
        directory = world.tree / 'var/lib/dosplex/operations'
        directory.mkdir(mode=0o700)
        op_id = 'e' * 32
        path = directory / (op_id + '.json')
        path.write_text(json.dumps({'id': op_id, 'action': 'start', 'state': 'succeeded', 'unit': 'u'}))
        path.chmod(0o666)
        self.ctl(world, 'operation', op_id, expect='not-found')
        directory.chmod(0o777)
        self.ctl(world, 'start', expect='failed')

    def test_the_runner_only_runs_a_waiting_operation(self):
        world = self.installed()
        self.assertFailed(world.run('_control_op', 'a' * 32), 'no operations directory')
        answer = self.ctl(world, 'start', expect='accepted')
        self.finish(world, answer)
        self.assertFailed(world.run('_control_op', answer['data']['operation']['id']), 'is not waiting to run')
        self.assertFailed(world.run('_control_op', 'zz'), 'not an operation id')

    def test_the_policy_allows_only_the_control_runner_in_the_background(self):
        world = self.installed()
        env = dosplex.Env(world.tree)
        program = str(world.tree / 'opt/dosplex/bin/dosplex')
        base = ['systemd-run', '--quiet', '--collect', '--unit=dosplex-control-start-12345678', '--', sys.executable]
        dosplex.check_command(env, base + [program, '--sysroot', str(world.tree), '_control_op', 'a' * 32])
        for label, argv in (
                ('another unit', ['systemd-run', '--unit=dosplex-start-12345678', '--', sys.executable, program, '_control_op']),
                ('no unit', ['systemd-run', '--', sys.executable, program, '_control_op']),
                ('the old worker in the background', base + [program, '_worker', 'start', '{}']),
                ('both', base + [program, '_control_op', '_worker']),
                ('another program', base + ['/bin/sh', '_control_op']),
                ('another python script', base + ['/tmp/x.py', '_control_op']),
                ('no marker', base + [program, 'status']),
                ('foreign unit', ['systemd-run', '--unit=evil-control-x', '--', sys.executable, program, '_control_op'])):
            with self.subTest(label):
                with self.assertRaises(dosplex.PolicyError):
                    dosplex.check_command(env, argv)
        # The waiting form is unchanged.
        dosplex.check_command(env, ['systemd-run', '--wait', '--unit=dosplex-start-12345678', '--', sys.executable,
                                    program, '_worker', 'start', '{}'])
        with self.assertRaises(dosplex.PolicyError):
            dosplex.check_command(env, ['systemd-run', '--wait', '--unit=dosplex-start-12345678', '--', sys.executable,
                                        program, '_control_op', 'a' * 32])


class SharedGameLibrary(Base):
    """`dosplex library`: the store tool of a release is a stub that records what it was asked (FAKE_STORE)."""

    PLACE = '/home/dosplex-library'
    UNIT = 'etc/systemd/system/dosplex-game-library.service'
    LINK = 'etc/systemd/system/multi-user.target.wants/dosplex-game-library.service'
    AUTO_SERVICE = 'etc/systemd/system/dosplex-game-library-auto.service'
    AUTO_TIMER = 'etc/systemd/system/dosplex-game-library-auto.timer'
    AUTO_LINK = 'etc/systemd/system/timers.target.wants/dosplex-game-library-auto.timer'
    EXPECTED_AUTO_SERVICE = '''[Unit]
Description=Dosplex shared game library: check new games for sharing
ConditionPathExists=/opt/dosplex/bin/dosplex

[Service]
Type=oneshot
ExecStart=/opt/dosplex/bin/dosplex library share --new
Nice=19
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
TimeoutStartSec=infinity
'''
    EXPECTED_AUTO_TIMER = '''[Unit]
Description=Dosplex shared game library: check new games every ten minutes

[Timer]
OnBootSec=3min
OnUnitInactiveSec=10min

[Install]
WantedBy=timers.target
'''
    EXPECTED_UNIT = '''[Unit]
Description=Dosplex shared game library
After=local-fs.target
Before=display-manager.service dosplex-compositor.service
ConditionPathExists=/opt/dosplex/bin/dosplex

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/dosplex/bin/dosplex library slot
ExecStop=/opt/dosplex/bin/dosplex library release
TimeoutStartSec=300
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
'''
    EARLIER_UNIT = '''[Unit]
Description=Dosplex shared game library
After=local-fs.target
Before=display-manager.service
ConditionPathExists=/opt/dosplex/bin/dosplex

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/dosplex/bin/dosplex library slot
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
'''
    DISK_SERVICE = 'etc/systemd/system/dosplex-game-library-disk@.service'
    DISK_RULE = 'etc/udev/rules.d/90-dosplex-game-library.rules'
    EXPECTED_DISK_SERVICE = '''[Unit]
Description=Dosplex shared game library: the mounts of %I
After=dosplex-game-library.service
ConditionPathExists=/opt/dosplex/bin/dosplex

[Service]
Type=oneshot
ExecStart=/opt/dosplex/bin/dosplex library arrive %I
TimeoutStartSec=300
'''
    EXPECTED_DISK_RULE = '''# Dosplex shared game library: a disk that appears or goes gets its shared games mounted or released.
SUBSYSTEM=="block", ACTION=="add|remove", KERNEL=="sd*|mmcblk*|nvme*|vd*", ENV{DEVTYPE}=="partition|disk", \\
    RUN+="/usr/bin/systemctl --no-block start dosplex-game-library-disk@%k.service"
'''

    def installed(self, wants=True, seed=False, tool=True, **options):
        world = self.world(**options)
        extra = {'bin/dosplex-game-store.py': FAKE_STORE.encode()} if tool else {}
        replace = {}
        if seed:
            replace['tools/root-session-trial.py'] = FAKE_TRIAL.replace(
                "layout='vertical', test_hooks=False):", "layout='vertical', test_hooks=False, game_store=None):").replace(
                '        native_relay=native_relay, layout=layout, test_hooks=test_hooks)\n    failure',
                '        native_relay=native_relay, layout=layout, test_hooks=test_hooks, game_store=game_store)\n    failure').encode()
        (world.tree / 'etc/udev/rules.d').mkdir(parents=True)
        if wants:
            (world.tree / 'etc/systemd/system/multi-user.target.wants').mkdir()
            (world.tree / 'etc/systemd/system/timers.target.wants').mkdir()
        self.assertOk(world.install(world.bundle(extra=extra, replace=replace), '--no-start'))
        world.reset_baseline()
        return world

    def on(self, world, *args, **extra):
        return world.run('library', 'on', '--experimental', *args, **extra)

    def store(self, world, verb=None):
        return [(c['verb'], c['argv']) for c in world.events('store') if verb in (None, c['verb'])]

    def canned(self, world, name, value):
        (world.fake / name).write_text(value if isinstance(value, str) else json.dumps(value))

    def turned_on(self, **options):
        world = self.installed(**options)
        self.assertOk(self.on(world))
        return world

    def place(self, world):
        return world.tree / self.PLACE.lstrip('/')

    # --- install over an install ---------------------------------------------------------------------------
    def test_an_install_over_an_install_keeps_the_library_on(self):
        world = self.turned_on()
        newer = world.bundle('newer', version='0.1.0-next', extra={'bin/dosplex-game-store.py': FAKE_STORE.encode()})
        self.assertOk(world.install(newer, '--no-start'))
        state = world.state()
        self.assertEqual(state['version'], '0.1.0-next')
        self.assertEqual(state['game_store'], self.PLACE)
        self.assertIn('Shared game library: on', world.run('status').out)

    def test_an_install_of_a_release_without_the_tool_is_refused_while_the_library_is_on(self):
        world = self.turned_on()
        older = world.bundle('older', version='0.1.0-old')
        result = world.install(older, '--no-start')
        self.assertNotEqual(result.code, 0)
        self.assertIn('library off', result.out + result.err)
        self.assertEqual(world.state()['version'], '0.1.0-test')

    # --- on --------------------------------------------------------------------------------------------
    def test_on_makes_the_store_the_unit_its_link_and_the_state_entry_as_one_journaled_operation(self):
        world = self.installed()
        result = self.on(world)
        self.assertOk(result)
        self.assertIn('The shared game library is on (%s).' % self.PLACE, result.out)
        self.assertIn('Games are shared automatically: new and updated games are checked in the background, the first '
                      'check starts in a few minutes, and they reach both accounts at the next start of Dosplex or '
                      'restart of the machine.', result.out)
        self.assertIn('To do it right away: sudo ', result.out)
        self.assertIn('library share --all', result.out)
        self.assertEqual(self.store(world), [('on', ['--root', str(self.place(world)), '--user', 'deck', '--user', 'guest'])])
        unit = world.tree / self.UNIT
        self.assertEqual(unit.read_text(), self.EXPECTED_UNIT)
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o644)
        link = world.tree / self.LINK
        self.assertEqual(os.readlink(link), '../dosplex-game-library.service')
        self.assertEqual((world.tree / self.DISK_SERVICE).read_text(), self.EXPECTED_DISK_SERVICE)
        self.assertEqual((world.tree / self.DISK_RULE).read_text(), self.EXPECTED_DISK_RULE)
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertEqual([c['argv'] for c in world.commands('systemctl')],
                         [['daemon-reload'], ['daemon-reload'], ['start', 'dosplex-game-library-auto.timer']])
        self.assertEqual((world.tree / self.AUTO_SERVICE).read_text(), self.EXPECTED_AUTO_SERVICE)
        self.assertEqual((world.tree / self.AUTO_TIMER).read_text(), self.EXPECTED_AUTO_TIMER)
        self.assertEqual(stat.S_IMODE((world.tree / self.AUTO_TIMER).stat().st_mode), 0o644)
        self.assertEqual(os.readlink(world.tree / self.AUTO_LINK), '../dosplex-game-library-auto.timer')
        self.assertNotIn('game_store_auto', world.state())
        records = world.journal()['0002-library.jsonl']
        self.assertEqual([r['step'] for r in records if r['kind'] == 'intent'], ['store', 'unit', 'auto', 'state'])
        self.assertEqual(records[-1]['kind'], 'end')
        self.assertEqual(records[-1]['outcome'], 'complete')
        status = world.run('status')
        self.assertIn('Shared game library: on (%s)' % self.PLACE, status.out)
        self.assertEqual(json.loads(world.run('status', '--json').out)['game_store'], self.PLACE)
        self.assertEqual(len(self.store(world)), 1)             # status asked the tool nothing

    def test_a_place_can_be_given_and_it_must_be_a_plain_absolute_path(self):
        world = self.installed()
        self.assertOk(self.on(world, '--root', '/srv/games'))
        self.assertEqual(world.state()['game_store'], '/srv/games')
        self.assertEqual(self.store(world)[0][1][1], str(world.tree / 'srv/games'))
        world = self.installed()
        for bad in ('relative/path', '/a/../b', '/'):
            with self.subTest(bad):
                self.assertFailed(self.on(world, '--root', bad), 'not a plain absolute path', code=2)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        self.assertEqual(self.store(world), [])

    def test_the_library_is_turned_on_only_with_the_word_experimental(self):
        # ADR-026: the shared game library stays in the program, off, and is turned on knowingly.
        world = self.installed()
        refused = world.run('library', 'on')
        self.assertFailed(refused, 'experimental', code=3)
        self.assertIn('library on --experimental', refused.text)
        self.assertIsNone(world.state().get('game_store'))
        self.assertEqual(self.store(world), [])
        self.assertOk(self.on(world))
        self.assertIn('Shared game library: on (', world.run('status').out)
        self.assertIn('), experimental', world.run('status').out)

    def test_on_refuses_what_it_cannot_do_and_changes_nothing(self):
        world = self.installed()
        self.assertOk(self.on(world))
        again = self.on(world)
        self.assertFailed(again, 'already on (%s)' % self.PLACE, code=3)
        self.assertEqual(len(self.store(world)), 1)
        # A release without the tool.
        world = self.installed(tool=False)
        result = self.on(world)
        self.assertFailed(result, 'has no shared game library', code=3)
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        # Not root, not installed.
        world = self.installed()
        self.assertFailed(world.run('library', 'on', root=False), 'needs root')
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)
        empty = self.world()
        self.assertFailed(self.on(empty), 'not installed')
        self.assertEqual(empty.snapshot(exclude=()), empty.initial_full)
        # A unit file that is not the one Dosplex writes, and one that is already the same.
        world = self.installed()
        (world.tree / self.UNIT).write_text('[Unit]\nDescription=mine\n')
        self.assertFailed(self.on(world), 'not the one Dosplex writes')
        self.assertEqual(self.store(world), [])
        (world.tree / self.UNIT).write_text(self.EXPECTED_UNIT)
        self.assertOk(self.on(world))
        self.assertEqual(world.state()['game_store'], self.PLACE)

    def test_on_refuses_while_an_earlier_operation_is_interrupted(self):
        world = self.world()
        extra = {'bin/dosplex-game-store.py': FAKE_STORE.encode()}
        self.assertEqual(world.install(world.bundle(extra=extra), '--no-start',
                                       DOSPLEX_TEST_CRASH_AT='install:done:cli').code, dosplex.FAULT_EXIT)
        result = self.on(world)
        self.assertFailed(result, 'repair', code=4)
        self.assertEqual(self.store(world), [])

    def test_a_tool_that_refuses_leaves_nothing_behind(self):
        world = self.installed()
        self.canned(world, 'store-fail-on', 'the users are not accounts of this machine')
        result = self.on(world)
        self.assertFailed(result, 'the users are not accounts of this machine')
        self.assertNotIn('game_store', world.state())
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual([r['outcome'] for r in world.journal()['0002-library.jsonl'] if r['kind'] == 'end'], ['rolled-back'])
        self.assertOk(world.run('repair'))

    def test_a_later_step_that_fails_is_undone_and_the_store_it_made_is_given_up(self):
        world = self.installed(wants=False)
        self.canned(world, 'reload-fails', '')
        self.canned(world, 'store-slot.json', {'left': True})
        result = self.on(world)
        self.assertFailed(result, 'Failed to reload the daemon')
        self.assertEqual([verb for verb, _ in self.store(world)], ['on', 'off', 'slot'])
        self.assertNotIn('game_store', world.state())
        self.assertEqual(world.snapshot(), world.initial)          # the unit, the link, the new directory and the store are gone
        self.assertEqual(world.journal()['0002-library.jsonl'][-1]['outcome'], 'rolled-back')

    def test_a_store_that_was_there_before_is_not_given_up_by_an_undo(self):
        world = self.installed()
        self.place(world).mkdir(parents=True)
        (self.place(world) / 'config.json').write_text('{}')
        self.canned(world, 'reload-fails', '')
        self.assertFailed(self.on(world), 'Failed to reload the daemon')
        self.assertEqual([verb for verb, _ in self.store(world)], ['on'])
        self.assertTrue((self.place(world) / 'config.json').exists())
        self.assertFalse((world.tree / self.UNIT).exists())

    def test_on_interrupted_at_every_journal_point_is_finished_or_undone_by_repair(self):
        probe = self.installed()
        log = probe.root / 'faults.log'
        self.assertOk(self.on(probe, DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = []
        for point in log.read_text().splitlines():
            if point.startswith('library:') and point not in points:
                points.append(point)
        for expected in ('library:begin', 'library:commit', 'library:intent:store', 'library:applied:unit',
                         'library:done:state', 'library:end'):
            self.assertIn(expected, points)
        for point in points:
            with self.subTest(point=point):
                world = self.installed()
                crashed = self.on(world, DOSPLEX_TEST_CRASH_AT=point)
                self.assertFailed(crashed)
                self.assertOk(world.run('repair'))
                done = 'game_store' in world.state()
                self.assertEqual(done, point != 'library:begin', point)       # a commit on disk is finished, else undone
                if done:
                    self.assertEqual((world.tree / self.UNIT).read_text(), self.EXPECTED_UNIT)
                    self.assertTrue((world.tree / self.LINK).is_symlink())
                    self.assertEqual((world.tree / self.DISK_SERVICE).read_text(), self.EXPECTED_DISK_SERVICE)
                    self.assertEqual((world.tree / self.DISK_RULE).read_text(), self.EXPECTED_DISK_RULE)
                else:
                    self.assertEqual(world.snapshot(), world.initial)
                self.assertOk(world.run('library', 'status'))

    # --- automatic sharing ------------------------------------------------------------------------------
    def systemctl(self, world, after=0):
        return [c['argv'] for c in world.commands('systemctl')][after:]

    def test_a_timer_that_cannot_be_started_is_said_and_does_not_stop_the_library_from_turning_on(self):
        world = self.installed()
        self.canned(world, 'start-fails', '')
        result = self.on(world)
        self.assertOk(result)
        self.assertIn('Could not start dosplex-game-library-auto.timer: Failed to start dosplex-game-library-auto.timer',
                      result.out)
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertTrue((world.tree / self.AUTO_TIMER).exists())

    def test_a_failure_while_the_automatic_units_are_written_undoes_them_and_everything_before(self):
        world = self.installed(wants=False)
        self.canned(world, 'reload-fails-second', '')
        self.canned(world, 'store-slot.json', {'left': True})
        result = self.on(world)
        self.assertFailed(result, 'Failed to reload the daemon')
        self.assertNotIn('game_store', world.state())
        self.assertEqual(world.snapshot(), world.initial)
        self.assertIn(['stop', 'dosplex-game-library-auto.timer'], self.systemctl(world))
        self.assertEqual(world.journal()['0002-library.jsonl'][-1]['outcome'], 'rolled-back')

    def test_automatic_units_that_are_not_the_ones_dosplex_writes_are_refused_and_nothing_changes(self):
        for path, make, words in (
                (self.AUTO_SERVICE, lambda p: p.write_text('[Unit]\nDescription=mine\n'), 'not the one Dosplex writes'),
                (self.AUTO_TIMER, lambda p: p.write_text('[Timer]\nOnBootSec=1\n'), 'not the one Dosplex writes'),
                (self.AUTO_TIMER, lambda p: p.symlink_to('/dev/null'), 'not the one Dosplex writes'),
                (self.AUTO_LINK, lambda p: p.symlink_to('../other.timer'), 'not the link Dosplex makes')):
            with self.subTest(path=path, words=words):
                world = self.installed()
                target = world.tree / path
                target.parent.mkdir(parents=True, exist_ok=True)
                make(target)
                self.assertFailed(self.on(world), words)
                self.assertEqual(self.store(world), [])
                self.assertNotIn('game_store', world.state())
        world = self.installed()
        (world.tree / self.AUTO_SERVICE).write_text(self.EXPECTED_AUTO_SERVICE)
        self.assertOk(self.on(world))

    def test_auto_off_stops_and_removes_the_units_and_auto_on_brings_them_back(self):
        world = self.turned_on()
        before = len(world.commands('systemctl'))
        result = world.run('library', 'auto', 'off')
        self.assertOk(result)
        self.assertIn('Automatic sharing is off', result.out)
        self.assertIs(world.state()['game_store_auto'], False)
        for path in (self.AUTO_SERVICE, self.AUTO_TIMER, self.AUTO_LINK):
            self.assertFalse(os.path.lexists(world.tree / path), path)
        self.assertTrue((world.tree / self.UNIT).exists())
        self.assertEqual(self.systemctl(world, before), [['stop', 'dosplex-game-library-auto.timer'],
                                                         ['stop', 'dosplex-game-library-auto.service'], ['daemon-reload']])
        status = world.run('library', 'status').out
        self.assertIn('Automatic sharing: off (share with: sudo ', status)
        self.assertIn(' library share APPID)', status)
        self.assertIn('Automatic sharing is already off', world.run('library', 'auto', 'off').out)
        before = len(world.commands('systemctl'))
        result = world.run('library', 'auto', 'on')
        self.assertOk(result)
        self.assertIn('Automatic sharing is on', result.out)
        self.assertIs(world.state()['game_store_auto'], True)
        self.assertEqual((world.tree / self.AUTO_TIMER).read_text(), self.EXPECTED_AUTO_TIMER)
        self.assertEqual(self.systemctl(world, before), [['daemon-reload'], ['start', 'dosplex-game-library-auto.timer']])
        self.assertIn('Automatic sharing: on (new games are checked every ten minutes)',
                      world.run('library', 'status').out)
        self.assertIn('Automatic sharing is already on', world.run('library', 'auto', 'on').out)
        self.assertEqual([r['action'] for name, records in sorted(world.journal().items())
                          if name.endswith('-library.jsonl') for r in records[:1]],
                         ['on', 'auto-off', 'auto-on'])

    def test_auto_needs_the_library_to_be_on_and_root(self):
        world = self.installed()
        self.assertFailed(world.run('library', 'auto', 'off'), 'is not on', code=3)
        self.assertFailed(world.run('library', 'auto', 'on'), 'is not on', code=3)
        world = self.turned_on()
        self.assertFailed(world.run('library', 'auto', 'off', root=False), 'needs root')
        self.assertFailed(world.run('library', 'auto', 'maybe'), code=2)

    def test_auto_off_interrupted_at_every_journal_point_is_finished_or_undone_by_repair(self):
        probe = self.turned_on()
        log = probe.root / 'faults.log'
        self.assertOk(probe.run('library', 'auto', 'off', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = []
        for point in log.read_text().splitlines():
            if point.startswith('library:') and point not in points:
                points.append(point)
        self.assertIn('library:done:auto-remove', points)
        for point in points:
            with self.subTest(point=point):
                world = self.turned_on()
                crashed = world.run('library', 'auto', 'off', DOSPLEX_TEST_CRASH_AT=point)
                self.assertFailed(crashed)
                self.assertOk(world.run('repair'))
                off = world.state().get('game_store_auto') is False
                self.assertEqual(off, point != 'library:begin', point)
                self.assertEqual(os.path.lexists(world.tree / self.AUTO_TIMER), not off, point)
                self.assertEqual(os.path.lexists(world.tree / self.AUTO_LINK), not off, point)

    def test_the_choice_of_automatic_sharing_survives_an_install_over_an_install(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'auto', 'off'))
        newer = world.bundle('newer', version='0.1.0-next', extra={'bin/dosplex-game-store.py': FAKE_STORE.encode()})
        self.assertOk(world.install(newer, '--no-start'))
        state = world.state()
        self.assertEqual((state['version'], state['game_store'], state['game_store_auto']), ('0.1.0-next', self.PLACE, False))
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_TIMER))
        self.assertOk(world.run('library', 'auto', 'on'))
        newest = world.bundle('newest', version='0.1.0-newest', extra={'bin/dosplex-game-store.py': FAKE_STORE.encode()})
        self.assertOk(world.install(newest, '--no-start'))
        state = world.state()
        self.assertEqual((state['version'], state.get('game_store_auto')), ('0.1.0-newest', True))
        self.assertTrue((world.tree / self.AUTO_TIMER).exists())

    def test_an_install_over_an_install_gives_an_old_library_its_automatic_units(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'auto', 'off'))
        state = world.state()
        state.pop('game_store_auto')                  # what an earlier release left: no choice, no automatic units
        (world.tree / 'var/lib/dosplex/state.json').write_text(json.dumps(state))
        before = len(world.commands('systemctl'))
        newer = world.bundle('newer', version='0.1.0-next', extra={'bin/dosplex-game-store.py': FAKE_STORE.encode()})
        result = world.install(newer, '--no-start')
        self.assertOk(result)
        self.assertIn('Automatic sharing of the shared game library is on', result.out)
        self.assertEqual((world.tree / self.AUTO_SERVICE).read_text(), self.EXPECTED_AUTO_SERVICE)
        self.assertIn(['start', 'dosplex-game-library-auto.timer'], self.systemctl(world, before))

    def test_a_damaged_choice_of_automatic_sharing_is_refused_like_any_damaged_field(self):
        world = self.installed()
        state = world.tree / 'var/lib/dosplex/state.json'
        good = json.loads(state.read_text())
        for value in ('yes', 1, ['on']):
            with self.subTest(value=value):
                state.write_text(json.dumps(dict(good, game_store='/srv/games', game_store_auto=value)))
                self.assertFailed(world.run('status'), 'is damaged')
        state.write_text(json.dumps(dict(good, game_store='/srv/games', game_store_auto=False)))
        self.assertOk(world.run('status'))

    def test_share_new_runs_the_tool_quietly_and_says_one_line_per_account(self):
        world = self.turned_on()
        self.canned(world, 'store-share.json', {'users': {
            'deck': {'shared': [{'appid': '620', 'name': 'Portal 2'}, {'appid': '70'}],
                     'refused': [{'appid': '400', 'name': 'Big', 'reason': 'it changed while it was read'}], 'skipped': 0},
            'guest': {'shared': [], 'refused': [], 'skipped': 2}}})
        result = world.run('library', 'share', '--new')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'share'), [('share', ['--root', str(self.place(world)), '--new'])])
        self.assertNotIn('can take a while', result.out)
        self.assertEqual(result.out.splitlines(), [
            'deck: Queued for sharing: 620 (Portal 2), 70. Not shared: 400 (Big): it changed while it was read.',
            'guest: Nothing new to share.'])
        result = world.run('library', 'share', '--new', '--user', 'guest')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'share')[1][1][2:], ['--new', '--user', 'guest'])
        self.canned(world, 'store-share.json', {'users': {}})
        self.assertEqual(world.run('library', 'share', '--new').out.strip(), 'Nothing new to share.')

    def test_share_new_follows_the_tools_failure_and_checks_its_arguments(self):
        world = self.turned_on()
        self.canned(world, 'store-share.json', '')
        self.assertFailed(world.run('library', 'share', '--new'), 'gave no readable answer', code=1)
        self.canned(world, 'store-exit-share', '1')
        self.assertFailed(world.run('library', 'share', '--new'), 'could not check for new games', code=1)
        calls = len(self.store(world, 'share'))
        for arguments in (('--new', '620'), ('--new', '--all'), ('--new', '--user', 'root')):
            with self.subTest(arguments):
                self.assertFailed(world.run('library', 'share', *arguments), code=2)
        self.assertEqual(len(self.store(world, 'share')), calls)
        self.assertFailed(self.installed().run('library', 'share', '--new'), 'is not on', code=3)

    def test_slot_stops_the_checking_service_before_it_asks_the_tool_and_reports_a_stopped_check(self):
        world = self.turned_on()
        before = len(world.calls())
        self.canned(world, 'store-slot.json', {'stopped_sharing': True})
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertEqual([(c.get('argv') if 'cmd' in c else c['verb']) for c in world.calls()[before:]],
                         [['stop', 'dosplex-game-library-auto.service'], 'slot'])
        self.assertIn('A running check of games was stopped; it starts again by itself.', result.out)
        self.canned(world, 'store-slot.json', {})
        self.assertNotIn('check of games was stopped', world.run('library', 'slot').out)

    def test_leaving_removes_the_automatic_units_and_the_choice_with_the_library(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'auto', 'off'))
        self.assertOk(world.run('library', 'auto', 'on'))
        self.assertOk(world.run('library', 'off'))
        self.canned(world, 'store-slot.json', {'left': True})
        self.assertOk(world.run('library', 'slot'))
        state = world.state()
        self.assertNotIn('game_store', state)
        self.assertNotIn('game_store_auto', state)
        for path in (self.UNIT, self.LINK, self.AUTO_SERVICE, self.AUTO_TIMER, self.AUTO_LINK, self.DISK_SERVICE,
                     self.DISK_RULE):
            self.assertFalse(os.path.lexists(world.tree / path), path)
        self.assertEqual(world.snapshot(), world.initial)

    # --- slot -------------------------------------------------------------------------------------------
    def test_slot_is_silent_when_the_library_is_not_on(self):
        world = self.installed()
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertEqual((result.out, result.err), ('', ''))
        self.assertEqual(self.store(world), [])
        self.assertEqual(world.snapshot(exclude=()), world.initial_full)

    def test_slot_runs_the_tools_slot_and_keeps_everything_while_the_store_stays(self):
        world = self.turned_on()
        self.canned(world, 'store-slot.json', {'config': {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']},
                                               'commit': {'committed': ['620'], 'dropped': [], 'removed': [],
                                                          'warnings': []}})
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertIn('Shared game library: 1 moved into the store.', result.out)
        self.assertEqual(self.store(world, 'slot'), [('slot', ['--root', str(self.place(world))])])
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertTrue((world.tree / self.UNIT).exists() and (world.tree / self.LINK).is_symlink())
        self.canned(world, 'store-slot.json', {'left': False})
        self.assertIn('nothing to do', world.run('library', 'slot').out)

    def test_a_slot_that_finds_the_store_left_removes_the_unit_its_link_and_the_state_entry(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'off'))
        self.canned(world, 'store-slot.json', {'returned': ['620'], 'left': True})
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertIn('The shared game library is off.', result.out)
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))
        self.assertNotIn('game_store', world.state())
        self.assertNotIn('Shared game library', world.run('status').out)
        self.assertEqual(world.journal()['0003-library.jsonl'][0]['action'], 'leave')
        self.assertEqual([c['argv'] for c in world.commands('systemctl')][3:], [
            ['stop', 'dosplex-game-library-auto.service'], ['daemon-reload'], ['stop', 'dosplex-game-library-auto.timer'],
            ['stop', 'dosplex-game-library-auto.service'], ['daemon-reload']])
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_SERVICE))
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_TIMER))
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_LINK))
        self.assertFalse(os.path.lexists(world.tree / self.DISK_SERVICE))
        self.assertFalse(os.path.lexists(world.tree / self.DISK_RULE))
        self.assertEqual(world.snapshot(), world.initial)
        # Once it is off, a slot has nothing to say and the library can be turned on again.
        self.assertEqual(world.run('library', 'slot').out, '')
        self.canned(world, 'store-slot.json', {})
        self.assertOk(self.on(world))
        self.assertEqual(world.state()['game_store'], self.PLACE)

    def left_at_a_start(self, world):
        """What the start of Dosplex leaves when its own slot gave the last game back: the store's folder without a
        config, the units and the state's entry as they were."""
        place = self.place(world)
        (place / 'sides').mkdir(parents=True, exist_ok=True)
        if (place / 'config.json').exists():
            (place / 'config.json').unlink()
        self.canned(world, 'store-slot.json', {'config': None})

    def test_status_says_leaving_and_then_off_while_the_units_are_still_there(self):
        world = self.turned_on()
        place = self.place(world)
        place.mkdir(parents=True, exist_ok=True)
        (place / 'config.json').write_text(json.dumps({'version': 1, 'mode': 'on', 'users': ['deck', 'guest']}))
        self.assertIn('Shared game library: on (%s), experimental' % self.PLACE, world.run('status').out)
        (place / 'config.json').write_text(json.dumps({'version': 1, 'mode': 'leaving', 'users': ['deck', 'guest']}))
        self.assertIn('Shared game library: leaving, every game goes back', world.run('status').out)
        self.left_at_a_start(world)
        out = world.run('status').out
        self.assertIn('Shared game library: off, every game is back with its account', out)
        self.assertIn('dosplex library slot', out)
        self.assertEqual(json.loads(world.run('status', '--json').out)['game_store_mode'], 'gone')

    def test_a_slot_after_a_start_that_ended_the_library_removes_the_units_and_the_state_entry(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'off'))
        self.left_at_a_start(world)
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertEqual(result.out.strip(),
                         'The shared game library is off. Every shared game is back with the account that shared it.')
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))
        self.assertFalse(os.path.lexists(world.tree / self.AUTO_TIMER))
        self.assertFalse(os.path.lexists(world.tree / self.DISK_RULE))
        self.assertNotIn('game_store', world.state())
        self.assertNotIn('Shared game library', world.run('status').out)
        self.assertEqual(world.journal()['0003-library.jsonl'][0]['action'], 'leave')
        self.assertEqual(world.run('library', 'slot').out, '')

    def test_a_store_without_a_config_whose_folder_is_not_there_keeps_the_library(self):
        # The place is on a disk that is not connected: nothing says the library left.
        world = self.turned_on()
        self.canned(world, 'store-slot.json', {'config': None})
        place = self.place(world)
        if place.exists():
            shutil.rmtree(place)
        self.assertOk(world.run('library', 'slot'))
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertTrue((world.tree / self.UNIT).exists())

    def test_library_on_after_a_start_that_ended_the_library_finishes_the_leaving_first(self):
        world = self.turned_on()
        self.assertOk(world.run('library', 'off'))
        self.left_at_a_start(world)
        self.canned(world, 'store-slot.json', {})
        self.assertOk(self.on(world))
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertEqual([name for name in world.journal() if 'library' in name][-2:],
                         ['0003-library.jsonl', '0004-library.jsonl'])
        self.assertTrue((world.tree / self.UNIT).exists())

    def test_uninstall_after_a_start_that_ended_the_library_finishes_the_leaving_and_goes_on(self):
        world = self.turned_on()
        refused = world.run('uninstall')
        self.assertNotEqual(refused.code, 0)
        self.assertIn('the shared game library is on', refused.out + refused.err)
        self.assertOk(world.run('library', 'off'))
        self.left_at_a_start(world)
        result = world.run('uninstall')
        self.assertOk(result)
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))
        self.assertFalse((world.tree / 'var/lib/dosplex/state.json').exists())

    def test_a_slot_that_ends_the_library_and_is_interrupted_is_finished_by_repair(self):
        world = self.turned_on()
        self.canned(world, 'store-slot.json', {'left': True})
        crashed = world.run('library', 'slot', DOSPLEX_TEST_CRASH_AT='library:done:unit-remove')
        self.assertFailed(crashed)
        self.assertOk(world.run('repair'))
        self.assertNotIn('game_store', world.state())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))

    def test_a_slot_the_tool_refuses_is_reported_in_plain_words_and_changes_nothing(self):
        world = self.turned_on()
        before = world.snapshot(exclude=())
        self.canned(world, 'store-fail-slot', 'a Steam is running (user deck)')
        result = world.run('library', 'slot')
        self.assertFailed(result, 'cannot change while a Steam is running', code=1)
        self.assertIn('a Steam is running (user deck)', result.err)
        self.assertEqual(world.snapshot(exclude=()), before)
        self.canned(world, 'store-fail-slot', 'the config is damaged')
        result = world.run('library', 'slot')
        self.assertFailed(result, 'the config is damaged', code=1)
        self.assertNotIn('Steam', result.err)

    # --- off --------------------------------------------------------------------------------------------
    def test_off_asks_the_tool_and_leaves_the_unit_and_the_state_to_the_slot(self):
        world = self.installed()
        result = world.run('library', 'off')
        self.assertFailed(result, 'is not on', code=3)
        self.assertEqual(self.store(world), [])
        self.assertOk(self.on(world))
        result = world.run('library', 'off')
        self.assertOk(result)
        self.assertIn('goes back to the account that shared it at the next restart of the machine', result.out)
        self.assertIn('dosplex library status', result.out)
        self.assertEqual(self.store(world, 'off'), [('off', ['--root', str(self.place(world))])])
        self.assertEqual(world.state()['game_store'], self.PLACE)
        self.assertTrue((world.tree / self.UNIT).exists())
        self.canned(world, 'store-fail-off', 'no config')
        self.assertFailed(world.run('library', 'off'), 'no config')

    # --- share ------------------------------------------------------------------------------------------
    def test_share_passes_the_games_and_the_account_and_shows_what_was_shared_and_refused(self):
        world = self.turned_on()
        self.canned(world, 'store-share.json', {'shared': [{'appid': 620, 'name': 'Portal 2'}, 70],
                                                'refused': [{'appid': 400, 'reason': 'its files are not all there'}]})
        result = world.run('library', 'share', '620', '70', '400')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'share'), [('share', ['--root', str(self.place(world)), '--user', 'deck',
                                                                 '--app', '620', '--app', '70', '--app', '400'])])
        self.assertIn('verifying 620', result.err)                     # the tool's progress comes through
        self.assertIn('Queued for sharing: 620 (Portal 2), 70.', result.out)
        self.assertIn('Not shared: 400: its files are not all there', result.out)
        self.assertIn('move at the next start of Dosplex or restart of the machine', result.out)
        result = world.run('library', 'share', '--all', '--user', 'guest')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'share')[1][1][2:], ['--user', 'guest', '--all'])

    def test_share_follows_the_tools_exit_status_and_still_shows_the_reasons(self):
        world = self.turned_on()
        self.canned(world, 'store-share.json', {'shared': [], 'refused': [{'appid': 400, 'reason': 'not installed'}]})
        self.canned(world, 'store-exit-share', '1')
        result = world.run('library', 'share', '400')
        self.assertFailed(result, code=1)
        self.assertIn('Not shared: 400: not installed', result.out)
        self.assertNotIn('move at the next start', result.out)
        self.canned(world, 'store-share.json', '')
        self.assertFailed(world.run('library', 'share', '400'), 'could not share anything', code=1)

    def test_share_checks_its_arguments_before_it_runs_anything(self):
        world = self.turned_on()
        for arguments in ((), ('620', '--all'), ('abc',), ('620', '--user', 'root'), ('620', '--user', 'other')):
            with self.subTest(arguments):
                self.assertFailed(world.run('library', 'share', *arguments), code=2)
        self.assertEqual(self.store(world, 'share'), [])
        off = self.installed()
        self.assertFailed(off.run('library', 'share', '620'), 'is not on', code=3)
        self.assertFailed(world.run('library', 'share', '620', root=False), 'needs root')

    def test_share_is_allowed_while_dosplex_runs(self):
        world = self.installed(seed=True)
        self.assertOk(self.on(world))
        self.assertOk(world.run('start', '--sample-seconds', '0.3', *world.controllers()))
        self.assertTrue(world.state()['trial'])
        self.canned(world, 'store-share.json', {'shared': [620], 'refused': []})
        self.assertOk(world.run('library', 'share', '620'))

    # --- status -----------------------------------------------------------------------------------------
    def test_status_shows_the_state_of_the_library_in_words_and_the_tools_json_unchanged(self):
        world = self.installed()
        result = world.run('library', 'status')
        self.assertOk(result)
        self.assertIn('Shared game library: off', result.out)
        self.assertEqual(self.store(world), [])
        self.assertOk(self.on(world))
        report = {'config': {'mode': 'on', 'users': ['deck', 'guest']},
                  'apps': [{'appid': 620, 'name': 'Portal 2', 'shared_by': 'deck'}],
                  'pending': [{'appid': 70}],
                  'sides': {'deck': {'library': '/home/deck/.local/share/Steam', 'mounted': True, 'registered': True,
                                     'steam_running': False, 'stash': {'journal': None, 'entries': 0}, 'conflicts': []},
                            'guest': {'library': '/home/guest/.local/share/Steam', 'mounted': False, 'registered': True,
                                      'steam_running': True, 'stash': {'journal': 'stashed', 'entries': 3},
                                      'earlier_layer': True,
                                      'conflicts': ['layer/steamapps', 'stash/appmanifest_5.acf']}}}
        self.canned(world, 'store-status.json', report)
        result = world.run('library', 'status')
        self.assertOk(result)
        for line in ('Shared game library: on (%s)' % self.PLACE,
                     'deck: its library /home/deck/.local/share/Steam, shared games shown in it\n',
                     'guest: its library /home/guest/.local/share/Steam, shared games not shown in it yet, Steam is running, '
                     '3 manifests of shared games are set aside until it is shown again; they are put back at the next '
                     'start of Dosplex or restart of the machine, its earlier private layer is still to be moved into the '
                     'library',
                     'kept aside because the library had them already (nothing was overwritten): layer/steamapps, '
                     'stash/appmanifest_5.acf',
                     'Portal 2 (620), shared by deck', 'Waiting for the next start of Dosplex or restart of the machine: 70'):
            self.assertIn(line, result.out)
        self.assertEqual(json.loads(world.run('library', 'status', '--json').out), report)
        report['config']['mode'] = 'leaving'
        self.canned(world, 'store-status.json', report)
        self.assertIn('Shared game library: leaving', world.run('library', 'status').out)

    # --- shutdown, a card that comes and goes, eject -----------------------------------------------------
    def test_the_boot_unit_releases_the_library_at_shutdown_after_the_sessions_and_before_the_disks_go(self):
        unit = self.EXPECTED_UNIT
        self.assertIn('ExecStop=/opt/dosplex/bin/dosplex library release\n', unit)
        self.assertIn('Before=display-manager.service dosplex-compositor.service\n', unit)
        self.assertIn('After=local-fs.target\n', unit)

    def test_release_runs_the_tools_release_waits_for_a_steam_and_says_what_it_did(self):
        world = self.turned_on()
        self.canned(world, 'store-release.json', {'stores': {
            '/p': {'deck': {'unmounted': ['library'], 'stashed': ['appmanifest_1.acf', 'appmanifest_2.acf']},
                   'guest': {'unmounted': ['library'], 'stashed': [], 'stash_waits': 'guest Steam is running'}}}})
        result = world.run('library', 'release')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'release'), [('release', ['--root', str(self.place(world)), '--wait', '30'])])
        self.assertIn('2 libraries released', result.out)
        self.assertIn('2 manifests set aside', result.out)
        self.assertIn('manifests stay until that Steam is closed (guest Steam is running)', result.out)
        self.canned(world, 'store-release.json', {'stores': {'/p': {'deck': {'error': 'target is busy'}}}})
        self.assertIn('not done: target is busy', world.run('library', 'release').out)

    def test_release_is_silent_when_the_library_is_off_and_follows_the_tools_failure(self):
        world = self.installed()
        result = world.run('library', 'release')
        self.assertOk(result)
        self.assertEqual((result.out, result.err, self.store(world)), ('', '', []))
        self.assertFailed(world.run('library', 'release', root=False), 'needs root')
        world = self.turned_on()
        self.canned(world, 'store-fail-release', 'the list of disks is unusable')
        self.assertFailed(world.run('library', 'release'), 'the list of disks is unusable', code=1)

    def test_arrive_gives_the_tool_the_device_and_a_minute_to_be_mounted(self):
        world = self.turned_on()
        self.canned(world, 'store-arrive.json', {'device': 'sdb1', 'disks': {self.USB_ROOT: {
            'deck': {'done': ['library']}, 'guest': {'done': ['library']}}}})
        result = world.run('library', 'arrive', 'sdb1')
        self.assertOk(result)
        self.assertEqual(self.store(world, 'arrive'),
                         [('arrive', ['--root', str(self.place(world)), '--device', 'sdb1', '--wait', '60'])])
        self.assertIn('Shared game library, disk sdb1: 2 libraries mounted.', result.out)
        self.assertOk(world.run('library', 'arrive'))
        self.assertEqual(self.store(world, 'arrive')[1], ('arrive', ['--root', str(self.place(world))]))

    def test_arrive_refuses_a_name_that_is_not_a_device_and_is_silent_when_the_library_is_off(self):
        world = self.turned_on()
        for name in ('../sda', 'a b', 'x' * 40, 'sda;id'):
            self.assertFailed(world.run('library', 'arrive', name), code=2)
        self.assertEqual(self.store(world, 'arrive'), [])
        world = self.installed()
        result = world.run('library', 'arrive', 'sdb1')
        self.assertOk(result)
        self.assertEqual((result.out, self.store(world)), ('', []))

    def test_eject_asks_the_tool_and_says_what_a_running_steam_leaves_behind(self):
        world = self.turned_on()
        self.canned(world, 'store-eject.json', {'ejected': True, 'mount': self.USB, 'device': '/dev/sdb1',
                                                'released': {'deck': {'unmounted': ['library'], 'stashed': []},
                                                             'guest': {'unmounted': ['library'], 'stash_waits': 'x'}}})
        result = world.run('library', 'eject', self.USB)
        self.assertOk(result)
        self.assertEqual(self.store(world, 'eject'), [('eject', ['--root', str(self.place(world)), '--mount', self.USB])])
        self.assertIn('%s is ejected; it is safe to pull out.' % self.USB, result.out)
        self.assertIn('The Steam of guest was running', result.out)
        self.canned(world, 'store-eject.json', {'ejected': True, 'already': True, 'mount': self.USB})
        self.assertIn('is not mounted; nothing to eject', world.run('library', 'eject', self.USB).out)

    def test_eject_that_the_tool_refuses_is_a_failure_and_a_bad_path_or_a_library_that_is_off_is_refused_before_it(self):
        world = self.turned_on()
        self.canned(world, 'store-fail-eject', '%s was not ejected, its shared games stay mounted: guest: target is busy'
                    % self.USB)
        self.assertFailed(world.run('library', 'eject', self.USB), 'was not ejected', code=1)
        calls = len(self.store(world, 'eject'))
        for path in ('relative', '/', '/run/../etc'):
            self.assertFailed(world.run('library', 'eject', path), code=2)
        self.assertEqual(len(self.store(world, 'eject')), calls)
        self.assertFailed(self.installed().run('library', 'eject', self.USB), 'is not on', code=3)

    def control(self, world, *words):
        result = world.run('control', 'eject', *words)
        return json.loads(result.out.splitlines()[0]), result

    def test_the_control_action_eject_answers_in_the_documented_codes(self):
        world = self.turned_on()
        self.canned(world, 'store-eject.json', {'ejected': True, 'mount': self.USB, 'device': '/dev/sdb1',
                                                'released': {'guest': {'stash_waits': 'x'}, 'deck': {'stashed': []}}})
        answer, result = self.control(world, self.USB)
        self.assertEqual((answer['code'], result.code, answer['ok']), ('ok', 0, True), result.text)
        self.assertEqual(answer['data'], {'mount': self.USB, 'device': '/dev/sdb1', 'already': False,
                                          'manifests_wait': ['guest']})
        self.canned(world, 'store-eject.json', {'ejected': True, 'already': True, 'mount': self.USB})
        answer, _ = self.control(world, self.USB)
        self.assertEqual((answer['code'], answer['data']['already']), ('ok', True))
        self.canned(world, 'store-fail-eject', 'no disk with a shared game store is listed at /x')
        self.assertEqual(self.control(world, '/x')[0]['code'], 'not-found')
        self.canned(world, 'store-fail-eject', '%s was not ejected, its shared games stay mounted: busy' % self.USB)
        answer, result = self.control(world, self.USB)
        self.assertEqual((answer['code'], result.code), ('refused', 3))
        self.canned(world, 'store-fail-eject', 'something broke')
        self.assertEqual(self.control(world, self.USB)[0]['code'], 'failed')
        for words in ((), ('relative',), ('/a', '/b'), ('/run/../x',), ('/a//b',), ('/',), ('/x\x01',), ('/' + 'a' * 300,)):
            answer, result = self.control(world, *words)
            self.assertEqual((answer['code'], result.code), ('bad-request', 2), words)
        self.assertEqual(self.control(self.installed(), self.USB)[0]['code'], 'refused')
        result = world.run('control', 'eject', self.USB, root=False)
        self.assertEqual(json.loads(result.out)['code'], 'permission-denied')

    def test_on_replaces_the_boot_unit_of_an_earlier_release_and_a_failure_puts_it_back(self):
        world = self.installed(wants=True)
        unit = world.tree / self.UNIT
        unit.write_text(self.EARLIER_UNIT)
        (world.tree / 'etc/systemd/system/multi-user.target.wants/dosplex-game-library.service').symlink_to(
            '../dosplex-game-library.service')
        world.reset_baseline()
        self.canned(world, 'reload-fails-second', '')
        self.canned(world, 'store-slot.json', {'left': True})
        self.assertFailed(self.on(world), 'Failed to reload the daemon')
        self.assertEqual(unit.read_text(), self.EARLIER_UNIT)                  # the earlier release's unit is back as it was
        self.assertFalse(os.path.lexists(world.tree / self.DISK_SERVICE))
        self.assertFalse(os.path.lexists(world.tree / self.DISK_RULE))
        self.assertEqual(world.snapshot(), world.initial)
        world = self.installed(wants=True)
        world.tree.joinpath(self.UNIT).write_text(self.EARLIER_UNIT)
        (world.tree / 'etc/systemd/system/multi-user.target.wants/dosplex-game-library.service').symlink_to(
            '../dosplex-game-library.service')
        self.assertOk(self.on(world))
        self.assertEqual((world.tree / self.UNIT).read_text(), self.EXPECTED_UNIT)

    def test_disk_units_that_are_not_the_ones_dosplex_writes_are_refused_and_nothing_changes(self):
        for path, make in ((self.DISK_SERVICE, lambda p: p.write_text('[Unit]\nDescription=mine\n')),
                           (self.DISK_RULE, lambda p: p.write_text('# mine\n')),
                           (self.DISK_RULE, lambda p: p.symlink_to('/dev/null'))):
            with self.subTest(path=path):
                world = self.installed()
                make(world.tree / path)
                self.assertFailed(self.on(world), 'not the one Dosplex writes')
                self.assertEqual(self.store(world), [])
                self.assertNotIn('game_store', world.state())

    def test_status_names_what_the_last_shutdown_and_the_last_disk_did(self):
        world = self.turned_on()
        self.canned(world, 'store-status.json', {
            'config': {'mode': 'on', 'users': ['deck']}, 'apps': [], 'pending': [], 'sides': {},
            'events': {'release': {'utc': '2026-11-18T10:00:00Z', 'report': {'stores': {
                '/p': {'deck': {'unmounted': ['library'], 'stashed': ['appmanifest_1.acf']}}}}},
                       'arrive': {'utc': '2026-11-18T11:00:00Z', 'report': {'disks': {'/d': {'deck': {'done': ['library']}}}}}},
            'disks': [{'root': self.USB_ROOT, 'mount': self.USB, 'present': True, 'apps': {}, 'pending': [], 'sides': {
                'deck': {'library': self.USB + '/SteamLibrary', 'mounted': False, 'steam_running': True,
                         'stash': {'journal': 'stashed', 'entries': 2}, 'stashed': ['appmanifest_1.acf', 'common/G/x'],
                         'conflicts': []}}}]})
        out = world.run('library', 'status').out
        self.assertIn('At the last shutdown or eject (2026-11-18T10:00:00Z): 1 libraries released, 1 manifests set aside', out)
        self.assertIn('At the last disk that appeared or went (2026-11-18T11:00:00Z): 1 libraries mounted', out)
        self.assertIn('not shown in the library of every account yet', out)
        self.assertIn('2 manifests of shared games are set aside until it is shown again (appmanifest_1.acf, common/G/x)', out)
        self.assertIn('they are put back at the next start of Dosplex', out)
        self.canned(world, 'store-status.json', {
            'config': {'mode': 'on'}, 'disks': [{'root': self.USB_ROOT, 'mount': self.USB, 'present': True, 'apps': {},
                                                  'sides': {'deck': {'library': '/l', 'mounted': True, 'steam_running': True,
                                                                     'stash': {}, 'conflicts': []}}}],
            'events': {'release': {'utc': 'u', 'report': {'ejected': self.USB}}}})
        out = world.run('library', 'status').out
        self.assertIn('shown in the library of every account', out)
        self.assertIn('it may show the shared games only after it looks at its library again', out)
        self.assertIn('ejected %s' % self.USB, out)

    # --- other disks ------------------------------------------------------------------------------------
    USB = '/run/media/deck/1234-abcd'
    USB_ROOT = USB + '/dosplex-library/disk'

    def test_the_slot_sums_up_every_store_names_a_disk_that_is_away_and_the_file_lists_it_asked_for(self):
        world = self.turned_on()
        moved = {'committed': ['620'], 'dropped': [], 'removed': [], 'warnings': []}
        self.canned(world, 'store-slot.json', {
            'config': {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']}, 'commit': moved,
            'lists': {'asked': 48, 'returned': 2, 'answered': 0},
            'disks': {self.USB_ROOT: {'commit': {'committed': ['70', '80'], 'dropped': [], 'removed': ['90'], 'warnings': []}},
                      '/run/media/deck/gone/dosplex-library/disk': {'absent': '/run/media/deck/gone is not mounted'}}})
        result = world.run('library', 'slot')
        self.assertOk(result)
        self.assertIn('Shared game library: 3 moved into the store, 1 removed (uninstalled by every account), '
                      'not connected: /run/media/deck/gone, asked Steam for the file lists of 48 games, '
                      '2 games still wait for their file lists (Steam was offline).', result.out)

    def test_the_slot_gives_the_disks_time_only_right_after_the_machine_started(self):
        world = self.turned_on()
        (world.tree / 'proc').mkdir(exist_ok=True)
        (world.tree / 'proc/uptime').write_text('7215.40 28000.11\n')
        self.assertOk(world.run('library', 'slot'))
        (world.tree / 'proc/uptime').write_text('14.92 40.03\n')
        self.assertOk(world.run('library', 'slot'))
        place = str(self.place(world))
        self.assertEqual(self.store(world, 'slot'), [('slot', ['--root', place]), ('slot', ['--root', place, '--wait', '20'])])

    def test_status_shows_each_disk_with_its_games_and_one_that_is_not_connected(self):
        world = self.turned_on()
        sides = {'deck': {'mounted': True, 'registered': True}, 'guest': {'mounted': True, 'registered': True}}
        self.canned(world, 'store-status.json', {
            'config': {'mode': 'on', 'users': ['deck', 'guest']}, 'apps': {}, 'pending': [], 'sides': sides,
            'disks': [{'root': self.USB_ROOT, 'mount': self.USB, 'present': True, 'reason': None, 'sides': sides,
                       'apps': {'70': {'name': 'Half-Life', 'shared_by': 'deck'}}, 'pending': [{'appid': '80'}]},
                      {'root': '/run/media/deck/gone/dosplex-library/disk', 'mount': '/run/media/deck/gone',
                       'present': False, 'reason': '/run/media/deck/gone is not mounted', 'apps': {}, 'pending': [],
                       'sides': {}}]})
        out = world.run('library', 'status').out
        for line in ('  Disk %s: shown in the library of every account, stored games: 1' % self.USB,
                     '    Half-Life (70), shared by deck',
                     '    Waiting for the next start of Dosplex or restart of the machine: 80',
                     '  Disk /run/media/deck/gone: not connected (/run/media/deck/gone is not mounted). Its games come '
                     'back when it is. If it is gone for good: sudo ', ' library forget /run/media/deck/gone'):
            self.assertIn(line, out)

    def test_status_says_why_a_game_was_not_taken_at_the_last_start(self):
        world = self.turned_on()
        sides = {'deck': {'mounted': True, 'registered': True}}
        base = {'config': {'mode': 'on', 'users': ['deck', 'guest']}, 'apps': {}, 'pending': [], 'sides': sides, 'disks': []}
        self.canned(world, 'store-status.json', dict(base, last_slot={'utc': '2026-11-16T07:31:02Z', 'report': {
            'commit': {'committed': ['620'], 'dropped': [{'appid': '550', 'reason': 'left4dead2.exe is open for writing'}],
                       'removed': [], 'warnings': []},
            'disks': {self.USB_ROOT: {'commit': {'committed': [], 'removed': [], 'warnings': [],
                                                 'dropped': [{'appid': '70', 'reason': 'is already shared (build 7)'}]}}}}}))
        out = world.run('library', 'status').out
        for line in ('  At the last start or restart (2026-11-16T07:31:02Z):',
                     '    550 not taken: left4dead2.exe is open for writing',
                     '    70 not taken: is already shared (build 7)'):
            self.assertIn(line, out)
        self.canned(world, 'store-status.json', dict(base, last_slot={'utc': 'x', 'report': {
            'commit': {'committed': ['620'], 'dropped': [], 'removed': [], 'warnings': []}, 'disks': {}}}))
        self.assertNotIn('At the last start', world.run('library', 'status').out)
        self.canned(world, 'store-status.json', dict(base, last_slot=None))
        self.assertNotIn('At the last start', world.run('library', 'status').out)

    def test_a_background_check_that_was_stopped_ends_quietly(self):
        # A start of Dosplex stops the check with a signal; the timer's service must not be left as failed.
        world = self.turned_on()
        self.canned(world, 'store-exit-share', '-15')
        result = world.run('library', 'share', '--new')
        self.assertOk(result)
        self.assertIn('The check of games was stopped; it starts again by itself.', result.out)

    def test_status_groups_the_games_that_are_not_shared_yet_by_reason(self):
        world = self.turned_on()
        waits = 'waits for its file list from Steam (%d of %d depot manifests are not cached); Steam fetches it at its next start'
        games = [{'appid': str(n), 'name': 'Game %d' % n, 'reason': waits % (1 + n % 2, 2)} for n in range(1, 8)]
        games.append({'appid': '99', 'name': 'Half', 'reason': 'is not fully installed (StateFlags 1026)'})
        self.canned(world, 'store-status.json', {
            'config': {'mode': 'on', 'users': ['deck', 'guest']}, 'apps': {}, 'pending': [], 'disks': [],
            'sides': {'deck': {'mounted': True, 'registered': True}}, 'unshared': {'deck': games, 'guest': []}})
        out = world.run('library', 'status').out
        self.assertIn('  Not shared, deck, 7: Game 1, Game 2, Game 3, Game 4, Game 5 and 2 more. Each waits for its file '
                      'list from Steam (depot manifests are not cached); Steam fetches it at its next start', out)
        self.assertIn('  Not shared, deck, 1: Half. Each is not fully installed (StateFlags 1026)', out)
        self.assertNotIn('Not shared, guest', out)
        self.canned(world, 'store-status.json', {'config': {'mode': 'on', 'users': ['deck']}, 'apps': {}, 'pending': [],
                                                 'sides': {}, 'disks': [], 'unshared': {'error': 'x'}})
        self.assertNotIn('Not shared', world.run('library', 'status').out)

    def test_share_says_which_library_is_on_a_disk_that_cannot_have_a_store(self):
        world = self.turned_on()
        self.canned(world, 'store-share.json', {'shared': [], 'refused': [], 'disks_refused': [
            {'library': '/run/media/deck/CARD', 'reason': '/run/media/deck/CARD is a exfat filesystem; a store needs '
                                                           'one of ext4, btrfs, xfs, f2fs'}]})
        result = world.run('library', 'share', '--all')
        self.assertIn('The games in /run/media/deck/CARD cannot be shared: /run/media/deck/CARD is a exfat filesystem',
                      result.out)

    def test_forget_asks_the_tool_with_the_store_root_of_the_disk_and_says_what_it_means(self):
        world = self.turned_on()
        for given in (self.USB, self.USB + '/', self.USB_ROOT):
            result = world.run('library', 'forget', given)
            self.assertOk(result)
            self.assertIn('The disk %s is forgotten' % self.USB, result.out)
        place = str(self.place(world))
        self.assertEqual(self.store(world, 'forget'), [('forget', ['--root', place, '--disk', self.USB_ROOT])] * 3)

    def test_forget_refuses_a_path_that_is_not_plain_and_passes_on_why_the_tool_refused(self):
        world = self.turned_on()
        for bad in ('usb', '/run/media/../etc'):
            self.assertFailed(world.run('library', 'forget', bad), code=2)
        self.assertEqual(self.store(world, 'forget'), [])
        self.canned(world, 'store-fail-forget', "deck's Steam is running; its library list can only be changed while it is closed")
        result = world.run('library', 'forget', self.USB)
        self.assertFailed(result)
        self.assertIn('a disk can only be forgotten while no Steam is running', result.err)
        self.canned(world, 'store-fail-forget', self.USB_ROOT + ' is connected; turn the shared game library off instead')
        self.assertIn('is connected', world.run('library', 'forget', self.USB).err)

    def test_forget_needs_the_library_on(self):
        world = self.installed()
        result = world.run('library', 'forget', self.USB)
        self.assertFailed(result)
        self.assertIn('not on', result.err)

    # --- the rest of the program ------------------------------------------------------------------------
    def test_uninstall_is_refused_while_the_library_is_on_and_works_after_it_left(self):
        world = self.turned_on()
        before = world.snapshot(exclude=())
        result = world.run('uninstall', '--remove-guest', '--yes')
        self.assertFailed(result, code=3)
        for words in ('shared game library is on', 'library off', 'restart the machine'):
            self.assertIn(words, result.err)
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertEqual(world.commands('systemd-run'), [])
        self.assertOk(world.run('library', 'off'))
        self.canned(world, 'store-slot.json', {'left': True})
        self.assertOk(world.run('library', 'slot'))
        self.assertOk(world.run('uninstall', '--remove-guest', '--yes'))

    def test_start_gives_the_store_only_to_a_seed_that_can_take_it(self):
        world = self.installed(seed=True)
        self.assertOk(self.on(world))
        result = world.run('start', '--sample-seconds', '0.3', *world.controllers())
        self.assertOk(result)
        (start,) = world.events('trial.start')
        self.assertEqual(start['game_store'], self.PLACE)
        self.assertNotIn('cannot use the shared game library', result.out)
        # A release whose seed has no such parameter starts without it, and says so once.
        old = self.installed()
        self.assertOk(self.on(old))
        result = old.run('start', '--sample-seconds', '0.3', *old.controllers())
        self.assertOk(result)
        (start,) = old.events('trial.start')
        self.assertNotIn('game_store', start)
        self.assertEqual(result.out.count('This release cannot use the shared game library'), 1)
        # Without the library the seed gets no such argument.
        plain = self.installed(seed=True)
        self.assertOk(plain.run('start', '--sample-seconds', '0.3', *plain.controllers()))
        (start,) = plain.events('trial.start')
        self.assertIsNone(start['game_store'])

    def test_a_state_entry_that_is_not_an_absolute_place_is_refused_like_any_damaged_field(self):
        world = self.installed()
        state = world.tree / 'var/lib/dosplex/state.json'
        good = json.loads(state.read_text())
        for label, value in (('relative', 'dosplex-library'), ('a number', 5), ('a list', ['/x']), ('dotdot', '/a/../b'),
                             ('nul', '/a\0b')):
            with self.subTest(label):
                state.write_text(json.dumps(dict(good, game_store=value)))
                for command in (('status',), ('start',), ('library', 'status')):
                    result = world.run(*command)
                    self.assertFailed(result, 'is damaged')
                    self.assertNotIn('Traceback', result.text)
        state.write_text(json.dumps(dict(good, game_store='/srv/games')))
        self.assertOk(world.run('status'))
        state.write_text(json.dumps(dict(good, game_store=None)))
        self.assertOk(world.run('status'))

    def test_the_tool_is_run_only_from_an_installed_release_and_only_with_its_verbs(self):
        world = self.installed()
        env = dosplex.Env(world.tree)
        tool = env.opt / 'releases/0.1.0-test/bin/dosplex-game-store.py'
        for verb in dosplex.GAME_STORE_VERBS:
            dosplex.check_command(env, [env.python, str(tool), verb, '--root', '/x'])
        dosplex.check_command(env, ['systemctl', 'daemon-reload'])
        for argv in ([env.python, str(tool), 'mount'], [env.python, str(tool)],
                     [env.python, str(env.opt / 'releases/0.1.0-test/bin/other.py'), 'on'],
                     [env.python, str(env.opt / 'releases/../x/bin/dosplex-game-store.py'), 'on'],
                     [env.python, str(world.tree / 'tmp/dosplex-game-store.py'), 'on'],
                     [env.python, str(env.opt / 'bin/dosplex-game-store.py'), 'on'],
                     ['systemctl', 'daemon-reload', 'extra'], ['systemctl', 'daemon-reexec'], ['systemctl', 'enable', 'x']):
            with self.subTest(argv=argv), self.assertRaises(dosplex.PolicyError):
                dosplex.check_command(env, argv)

    def test_help_and_the_page_describe_the_library_commands(self):
        world = self.world()
        for action in ('on', 'off', 'slot', 'share', 'status', 'auto', 'forget'):
            result = world.run('library', action, '--help')
            self.assertOk(result)
            self.assertIn('usage: dosplex library ' + action, result.out)
        self.assertFailed(world.run('library'), code=2)
        page = (ROOT / 'docs/steamos-installation.md').read_text()
        self.assertIn('## Shared game library (experimental)', page)
        for words in ('dosplex library on', 'dosplex library share', 'dosplex library slot', 'dosplex library off',
                      'dosplex library status', 'dosplex-game-library.service', 'dosplex library auto off',
                      'dosplex library forget', 'dosplex-game-library-auto.timer', 'file lists'):
            self.assertIn(words, page)


class StartLayoutAndBoot(Base):
    """`dosplex start --layout` and the start at boot (`--boot`): Main alone on the full screen, no controller."""

    def installed(self, **options):
        world = self.world(**options)
        self.assertOk(world.install(world.bundle(), '--no-start'))
        return world

    def intents(self, world, name='0002-start.jsonl'):
        return [r['step'] for r in world.journal()[name] if r['kind'] == 'intent']

    def switches(self, world):
        return [c for c in world.commands('steamosctl') if c['argv'][0].startswith('switch')]

    def test_the_layout_reaches_the_trial_and_is_journaled_for_a_repair(self):
        for layout in ('single', 'vertical', 'horizontal'):
            with self.subTest(layout):
                world = self.installed()
                self.assertOk(world.run('start', '--layout', layout))
                self.assertEqual([e['layout'] for e in world.events('trial.start')], [layout])
                records = world.journal()['0002-start.jsonl']
                self.assertEqual(records[0]['layout'], layout)
                for step in ('trial', 'controllers'):
                    (intent,) = [r for r in records if r['kind'] == 'intent' and r['step'] == step]
                    self.assertEqual(intent['data']['layout'], layout)
                self.assertEqual(world.state()['trial']['layout'], layout)
        world = self.installed()
        self.assertOk(world.run('start'))
        self.assertEqual(world.events('trial.start')[0]['layout'], 'vertical')       # ADR-013, for a start by hand
        self.assertFailed(self.installed().run('start', '--layout', 'diagonal'), code=2)

    def test_a_release_that_cannot_take_a_layout_refuses_one_that_is_not_the_default(self):
        world = self.world()
        old = FAKE_TRIAL.replace(", layout='vertical', test_hooks=False):", '):').replace(
            ', layout=layout, test_hooks=test_hooks)', ')')
        self.assertNotEqual(old, FAKE_TRIAL)
        self.assertOk(world.install(world.bundle(replace={'tools/root-session-trial.py': old.encode()}), '--no-start'))
        result = world.run('start', '--layout', 'single')
        self.assertFailed(result, 'cannot start with the single layout')
        self.assertIn('Returning to stock Steam', result.out)
        self.assertOk(world.run('start'))            # the default needs nothing from the release

    def test_a_restart_after_an_update_keeps_the_layout_the_session_had(self):
        world = self.installed()
        self.assertOk(world.run('start', '--layout', 'horizontal'))
        with mock.patch.dict(os.environ, world.environment()), mock.patch.object(dosplex, 'say'):
            dosplex_env = dosplex.Env(world.tree)
            with mock.patch.object(dosplex, 'start_client') as start, mock.patch.object(dosplex, 'launch_worker',
                                                                                         return_value=(None, mock.Mock(returncode=0))):
                dosplex.restart_session(dosplex_env, False, 0)
        self.assertEqual(start.call_args.args[1].layout, 'horizontal')
        self.assertFalse(start.call_args.args[1].boot)

    def test_boot_never_leaves_desktop_mode(self):
        world = self.installed()          # a Desktop mode device
        before = world.snapshot(exclude=())
        result = world.run('start', '--boot')
        self.assertOk(result)
        self.assertEqual(result.out.strip(), 'Dosplex does not start at boot in Desktop mode; Steam is left as it is.')
        self.assertEqual(world.events('trial.start'), [])
        self.assertEqual(world.commands('systemd-run'), [])
        self.assertEqual(self.switches(world), [])
        self.assertEqual(world.snapshot(exclude=()), before)

    def test_boot_starts_main_alone_without_a_controller_and_without_switching_modes(self):
        world = self.installed(desktop=False)
        result = world.run('start', '--boot')
        self.assertOk(result)
        self.assertIn('Dosplex is running (', result.out)
        (trial,) = world.events('trial.start')
        self.assertEqual(trial['layout'], 'single')
        (external,) = world.events('external.run')
        self.assertEqual((external['main'], external['guest'], external['builtin_deck']), (None, None, False))
        self.assertEqual(self.switches(world), [])
        self.assertEqual(self.intents(world), ['verify-release', 'preflight', 'idle-steam', 'host-mode', 'trial',
                                               'controllers', 'running'])
        state = world.state()
        self.assertEqual((state['trial']['layout'], state['session']['switched']), ('single', False))
        self.assertIsNone(state['controllers'])
        records = world.journal()['0002-start.jsonl']
        self.assertEqual((records[0]['boot'], records[0]['layout']), (True, 'single'))
        self.assertEqual(records[-1]['outcome'], 'complete')
        # --boot and another layout do not go together.
        other = self.installed(desktop=False)
        self.assertFailed(other.run('start', '--boot', '--layout', 'vertical'), 'does not go with it', code=2)
        self.assertEqual(other.commands('systemd-run'), [])
        self.assertOk(other.run('start', '--boot', '--layout', 'single'))

    def test_boot_that_never_finds_steam_idle_gives_up_quietly_and_leaves_stock_steam_running(self):
        world = self.installed(desktop=False)
        (world.fake / 'busy').write_text('')           # a game, or Steam updating itself
        result = world.run('start', '--boot')
        self.assertOk(result)
        self.assertIn('Steam was not idle after 5 minutes, so Dosplex did not start at boot; Steam is left running.',
                      result.out)
        self.assertEqual(world.events('trial.start'), [])
        self.assertEqual(world.events('trial.recover'), [])
        self.assertEqual(self.switches(world), [])
        self.assertEqual(self.intents(world), ['verify-release', 'preflight', 'idle-steam'])
        self.assertEqual(world.journal()['0002-start.jsonl'][-1]['outcome'], 'complete')
        self.assertIsNone(world.state()['trial'])
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
        (world.fake / 'busy').unlink()
        self.assertOk(world.run('start', '--boot'))      # and the next boot start works

    def test_boot_that_fails_after_it_began_returns_to_stock_steam(self):
        for failure in ('before-root', 'after-root', 'external'):
            with self.subTest(failure):
                world = self.installed(desktop=False)
                result = world.run('start', '--boot', FAKE_TRIAL_FAIL=failure)
                self.assertFailed(result, 'did not finish')
                self.assertIn('Returning to stock Steam', result.out)
                self.assertEqual(world.units(), [])
                self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])
                self.assertEqual(self.switches(world), [])
                self.assertEqual(world.journal()['0002-start.jsonl'][-1]['outcome'], 'rolled-back')
                self.assertIsNone(world.state()['trial'])
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_boot_is_refused_for_a_version_that_changed_since_the_untested_one_was_accepted(self):
        world = self.world(version_id='9.9.9', desktop=False)
        self.assertOk(world.install(world.bundle(), '--allow-untested', '--no-start'))
        self.assertOk(world.run('start', '--boot'))               # what was accepted at install is remembered
        self.assertOk(world.run('stop'))
        world.write('etc/os-release', 'ID=steamos\nVERSION_ID=9.9.10\nBUILD_ID=1\n')      # the system was updated
        calls = len(world.commands('systemd-run'))
        result = world.run('start', '--boot')
        self.assertFailed(result, 'not in the tested set', code=3)
        self.assertEqual(len(world.events('trial.start')), 1)
        self.assertEqual(len(world.commands('systemd-run')), calls)       # refused before anything was changed
        self.assertOk(world.run('start', '--boot', '--allow-untested'))

    # --- the compositor ends on its own (TRL-03) ---------------------------------------------------
    def compositor_unit(self, world):
        (root,) = sorted((world.tree / 'run').glob('dosplex-trial-*'))
        return json.loads((root / 'transaction.json').read_text())['unit']

    def test_a_dead_compositor_with_a_recorded_run_is_reported_and_start_recovers_and_starts_again(self):
        world = self.installed(desktop=False)
        self.assertOk(world.run('start'))
        (world.fake / 'units' / self.compositor_unit(world)).unlink()        # the compositor crashed
        status = world.run('status')
        self.assertIn('Running: no', status.out)
        self.assertIn('the compositor ended on its own', status.out)
        info = json.loads(world.run('status', '--json').out)['trial']
        self.assertEqual((info['alive'], info['ended'], info['compositor_active']), (False, True, False))
        result = world.run('start')
        self.assertOk(result)
        self.assertNotIn('already running', result.out)
        self.assertIn('Bringing stock Steam back first', result.out)
        self.assertEqual(len(world.events('trial.recover')), 1)
        self.assertEqual(len(world.events('trial.start')), 2)
        self.assertEqual(len(sorted((world.tree / 'run').glob('dosplex-trial-*'))), 1)
        self.assertIn('Running: yes', world.run('status').out)
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_stop_still_recovers_a_session_whose_compositor_is_dead(self):
        world = self.installed(desktop=False)
        self.assertOk(world.run('start'))
        (world.fake / 'units' / self.compositor_unit(world)).unlink()
        self.assertOk(world.run('stop'))
        self.assertEqual(len(world.events('trial.recover')), 1)
        self.assertEqual(sorted((world.tree / 'run').glob('dosplex-trial-*')), [])

    # --- a boot start that was cut short (CLI-02) ---------------------------------------------------
    def test_a_boot_start_clears_an_interrupted_start_of_its_own_kind_and_a_start_by_hand_does_not(self):
        world = self.installed(desktop=False)
        crashed = world.run('start', '--boot', DOSPLEX_TEST_CRASH_AT='start:done:trial')   # a shutdown during the start
        self.assertFailed(crashed)
        self.assertIn('Interrupted: start', world.run('status').out)
        refused = world.run('start')                                       # somebody at the keyboard repairs first
        self.assertFailed(refused, 'an earlier start was interrupted')
        result = world.run('start', '--boot')
        self.assertOk(result)
        self.assertIn('An earlier start at boot was interrupted', result.out)
        self.assertIn('Dosplex is running (', result.out)
        journal = world.journal()
        names = sorted(journal)
        self.assertEqual(journal[names[1]][-1]['outcome'], 'rolled-back')       # repair undid it
        self.assertEqual(journal[names[-1]][-1]['outcome'], 'complete')
        self.assertEqual(len(world.events('trial.recover')), 1)
        self.assertEqual(len(sorted((world.tree / 'run').glob('dosplex-trial-*'))), 1)
        self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_a_boot_start_does_not_clear_an_interrupted_operation_of_another_kind(self):
        world = self.installed(desktop=False)
        self.assertFailed(world.run('start', DOSPLEX_TEST_CRASH_AT='start:done:trial'))     # by hand, not at boot
        self.assertFailed(world.run('start', '--boot'), 'an earlier start was interrupted')
        self.assertEqual(world.events('trial.recover'), [])

    # --- a TV that is still waking up (CLI-04) -------------------------------------------------------
    def drm_status(self, world, text):
        (world.tree / 'sys/class/drm/card0-HDMI-A-1/status').write_text(text + '\n')

    def test_wait_for_display_polls_until_the_display_shows_and_is_bounded(self):
        world = self.installed(desktop=False)
        self.drm_status(world, 'disconnected')
        with mock.patch.dict(os.environ, world.environment()):
            env = dosplex.Env(world.tree)
            clock, slept, appears = [0.0], [], [True]

            def sleep(seconds):
                slept.append(seconds)
                clock[0] += seconds
                if clock[0] >= 8 and appears:
                    self.drm_status(world, 'connected')
            self.assertTrue(dosplex.wait_for_display(env, seconds=120, poll=2, sleep=sleep, clock=lambda: clock[0]))
            self.assertEqual(slept, [2, 2, 2, 2])
            self.drm_status(world, 'disconnected')
            clock[0], slept[:], appears[:] = 0.0, [], []
            self.assertFalse(dosplex.wait_for_display(env, seconds=120, poll=2, sleep=sleep, clock=lambda: clock[0]))
            self.assertEqual((len(slept), clock[0]), (60, 120))
        self.assertEqual((dosplex.BOOT_DISPLAY_SECONDS, dosplex.BOOT_DISPLAY_POLL_SECONDS), (120, 2))

    def plan_boot(self, world):
        args = argparse.Namespace(output_mode=None, main_hid=None, guest_hid=None, allow_untested=False, boot=True,
                                  layout=None, sample_seconds=0, keep_on_failure=False)
        with mock.patch.dict(os.environ, world.environment()), mock.patch.object(dosplex, 'say'):
            return dosplex.plan_start(dosplex.Env(world.tree), args)

    def test_a_boot_start_waits_for_a_display_that_appears_after_a_few_polls(self):
        world = self.installed(desktop=False)
        self.drm_status(world, 'disconnected')
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                self.drm_status(world, 'connected')
        with mock.patch.object(dosplex.time, 'sleep', sleep):
            options, probe = self.plan_boot(world)
        self.assertEqual(polls, [2, 2, 2])
        self.assertTrue(options['boot'])

    def test_a_boot_start_with_no_display_for_the_whole_wait_changes_nothing(self):
        world = self.installed(desktop=False)
        self.drm_status(world, 'disconnected')
        before = world.snapshot(exclude=())
        result = world.run('start', '--boot')                         # DOSPLEX_TEST_DISPLAY_SECONDS=1 bounds the wait
        self.assertFailed(result, 'no connected display')
        self.assertEqual(world.snapshot(exclude=()), before)
        self.assertEqual(world.events('trial.start'), [])
        by_hand = world.run('start')                                  # a start by hand does not wait
        self.assertFailed(by_hand, 'no connected display')

    # --- Steam updates itself (CLI-01) ------------------------------------------------------------
    def steam_client(self, world, version):
        world.write('home/deck/.local/share/Steam/package/steam_client_steamdeck_stable_ubuntu12.manifest',
                    '"steamdeck_stable"\n{\n\t"version"\t\t"%s"\n}\n' % version)

    def test_boot_survives_a_steam_client_update_and_status_carries_a_warning(self):
        world = self.installed(desktop=False)
        self.steam_client(world, '1760000000')                       # Steam updated itself; not in the tested set
        by_hand = world.run('start')
        self.assertFailed(by_hand, 'not in the tested set', code=3)
        self.assertEqual(world.events('trial.start'), [])
        result = world.run('start', '--boot')
        self.assertOk(result)
        self.assertIn('Steam client 1760000000 is not in the tested set', result.out)
        self.assertEqual(len(world.events('trial.start')), 1)
        status = world.run('status')
        self.assertIn('Warning: Steam client 1760000000 is not in the tested set', status.out)
        self.assertEqual(json.loads(world.run('status', '--json').out)['boot_warnings'][0][:12], 'Steam client')
        self.assertOk(world.run('stop'))
        self.steam_client(world, STEAM)                              # a later start that is fully tested drops it
        self.assertOk(world.run('start', '--boot'))
        self.assertEqual(world.run('status', '--json').out.count('is not in the tested set'), 0)

    def test_boot_still_refuses_a_different_steamos_or_gamescope_unless_that_was_accepted_at_install(self):
        world = self.installed(desktop=False)
        self.steam_client(world, '1760000000')
        world.write('etc/os-release', 'ID=steamos\nVERSION_ID=9.9.10\nBUILD_ID=1\n')       # and SteamOS changed too
        self.assertFailed(world.run('start', '--boot'), 'not in the tested set', code=3)
        self.assertEqual(world.events('trial.start'), [])
        other = self.installed(desktop=False)
        other.save_fake('gamescope.json', '3.99.0')
        self.assertFailed(other.run('start', '--boot'), 'Gamescope', code=3)
        # Accepted at install: the Gamescope it was installed with stays accepted, and Steam's client may change.
        accepted = self.world(desktop=False, gamescope='3.99.0')
        self.assertOk(accepted.install(accepted.bundle(), '--allow-untested', '--no-start'))
        self.steam_client(accepted, '1760000000')
        self.assertOk(accepted.run('start', '--boot'))
        self.assertOk(accepted.run('stop'))
        accepted.save_fake('gamescope.json', '3.99.1')                # but not a newer one
        self.assertFailed(accepted.run('start', '--boot'), 'not in the tested set', code=3)

    def test_a_start_by_hand_in_desktop_mode_still_switches_to_gaming_mode(self):
        world = self.installed()
        self.assertOk(world.run('start'))
        self.assertEqual([c['argv'] for c in self.switches(world)], [['switch-to-game-mode']])


class LibraryEntry(Base):
    """The "Dosplex" entry for Steam's library and the service behind it (ADR-026): what an install leaves so that a
    person with a controller can start Dosplex, and what takes it away again."""
    SOCKET = 'etc/systemd/system/dosplex-start.socket'
    SERVICE = 'etc/systemd/system/dosplex-start.service'
    LINK = 'etc/systemd/system/sockets.target.wants/dosplex-start.socket'
    CONFIG = 'var/lib/dosplex/start-service.json'
    DESKTOP = 'home/deck/.local/share/applications/dosplex.desktop'
    ON = {'DOSPLEX_TEST_ENTRY': '1'}

    def installed(self, **options):
        world = self.world(**options)
        self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))
        return world

    def systemctl(self, world):
        return [c['argv'] for c in world.commands('systemctl')]

    def test_an_install_sets_up_the_entry_and_starts_only_the_socket(self):
        world = self.world()
        result = world.install(world.bundle(), '--no-start', **self.ON)
        self.assertOk(result)
        self.assertIn('Add a Non-Steam Game', result.out)
        socket_unit = (world.tree / self.SOCKET).read_text()
        self.assertIn('ListenStream=/run/dosplex-start.sock', socket_unit)
        self.assertIn('SocketUser=deck', socket_unit)
        self.assertIn('SocketMode=0600', socket_unit)
        self.assertIn('Accept=no', socket_unit)
        service = (world.tree / self.SERVICE).read_text()
        self.assertIn('ExecStart=/usr/bin/python3 /opt/dosplex/current/libexec/dosplex-start-service.py '
                      '--config /var/lib/dosplex/start-service.json', service)
        self.assertNotIn('[Install]', service)                          # only the socket starts it
        self.assertEqual(os.readlink(world.tree / self.LINK), '../dosplex-start.socket')
        config = json.loads((world.tree / self.CONFIG).read_text())
        self.assertEqual(config, {'uid': UID, 'program': '/opt/dosplex/bin/dosplex'})
        self.assertEqual(stat.S_IMODE((world.tree / self.CONFIG).stat().st_mode), 0o644)
        desktop = (world.tree / self.DESKTOP).read_text()
        self.assertIn('Name=Dosplex', desktop)
        self.assertIn('Exec=/usr/bin/python3 /opt/dosplex/current/libexec/dosplex-start-client.py', desktop)
        self.assertIn(['start', 'dosplex-start.socket'], self.systemctl(world))
        self.assertNotIn(['start', 'dosplex-start.service'], self.systemctl(world))
        self.assertIs(world.state()['entry'], True)
        self.assertIn("Start from Steam's library: ready", world.run('status').out)
        self.assertRegex(world.run('entry', 'status').out.strip(), r"^Start from Steam's library: ready, not yet added "
                         r"to Steam's library \(sudo \S*dosplex entry on adds it\)$")
        # The two programs are part of every release, or the entry would point at nothing.
        for name in ('dosplex-start-service.py', 'dosplex-start-client.py'):
            self.assertTrue((world.tree / 'opt/dosplex/current/libexec' / name).is_file(), name)

    def test_a_second_install_changes_nothing_and_writes_no_second_journal(self):
        world = self.installed()
        before = sorted(world.journal())
        calls = len(self.systemctl(world))
        self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))
        self.assertEqual([name for name in sorted(world.journal()) if 'entry' in name],
                         [name for name in before if 'entry' in name])
        self.assertNotIn(['start', 'dosplex-start.socket'], self.systemctl(world)[calls:])

    def test_off_removes_everything_it_wrote_and_an_install_does_not_bring_it_back(self):
        world = self.installed()
        result = world.run('entry', 'off')
        self.assertOk(result)
        for name in (self.SOCKET, self.SERVICE, self.LINK, self.CONFIG, self.DESKTOP):
            self.assertFalse(os.path.lexists(world.tree / name), name)
        self.assertFalse((world.tree / 'home/deck/.local/share/applications').exists())   # the folder it made
        self.assertIn(['stop', 'dosplex-start.socket'], self.systemctl(world))
        self.assertIs(world.state()['entry'], False)
        self.assertIn("Start from Steam's library: off", world.run('status').out)
        self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))             # the choice is kept
        self.assertFalse(os.path.lexists(world.tree / self.SOCKET))
        self.assertOk(world.run('entry', 'on'))
        self.assertTrue((world.tree / self.DESKTOP).is_file())
        self.assertIs(world.state()['entry'], True)

    def test_a_folder_the_user_already_had_and_other_entries_in_it_stay(self):
        world = self.world()
        folder = world.tree / 'home/deck/.local/share/applications'
        folder.mkdir(parents=True)
        (folder / 'other.desktop').write_text('[Desktop Entry]\nName=Other\n')
        self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))
        self.assertTrue((world.tree / self.DESKTOP).is_file())
        self.assertOk(world.run('entry', 'off'))
        self.assertFalse((world.tree / self.DESKTOP).exists())
        self.assertEqual(sorted(e.name for e in folder.iterdir()), ['other.desktop'])

    def test_an_entry_the_user_changed_is_left_alone_when_the_rest_goes(self):
        world = self.installed()
        mine = '[Desktop Entry]\nName=Dosplex, my way\nExec=/bin/true\n'
        (world.tree / self.DESKTOP).write_text(mine)
        self.assertOk(world.run('entry', 'off'))
        self.assertEqual((world.tree / self.DESKTOP).read_text(), mine)
        self.assertFalse(os.path.lexists(world.tree / self.SOCKET))

    def test_a_unit_file_that_is_not_dosplexs_is_not_overwritten(self):
        world = self.world()
        unit = world.tree / self.SOCKET
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text('[Socket]\nListenStream=/run/somebody-else.sock\n')
        result = world.install(world.bundle(), '--no-start', **self.ON)
        self.assertOk(result)                                            # the install itself is not failed by it
        self.assertIn('not the one Dosplex writes', result.out)
        self.assertEqual(unit.read_text(), '[Socket]\nListenStream=/run/somebody-else.sock\n')
        self.assertFalse(os.path.lexists(world.tree / self.SERVICE))
        self.assertFalse((world.tree / self.DESKTOP).exists())
        refused = world.run('entry', 'on')
        self.assertFailed(refused, 'not the one Dosplex writes', code=3)

    def test_uninstall_takes_the_entry_away_and_leaves_the_home_as_it_was(self):
        world = self.world()

        def main_home_and_units():
            # Guest's account is made by an install and kept by an uninstall unless asked otherwise, so only Main's
            # home and systemd's unit folder are compared.
            return {name: value for name, value in world.snapshot().items()
                    if name.startswith('home/deck') or name.startswith('etc/systemd')}
        before = main_home_and_units()
        self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))
        self.assertTrue((world.tree / self.DESKTOP).is_file())
        self.assertNotEqual(main_home_and_units(), before)
        self.assertOk(world.run('uninstall'))
        for name in (self.SOCKET, self.SERVICE, self.LINK, self.CONFIG, self.DESKTOP):
            self.assertFalse(os.path.lexists(world.tree / name), name)
        self.assertEqual(main_home_and_units(), before)

    def test_a_change_cut_short_is_finished_or_undone_by_repair(self):
        probe = self.world()
        self.assertOk(probe.install(probe.bundle(), '--no-start'))
        log = probe.root / 'points.log'
        self.assertOk(probe.run('entry', 'on', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = [line for line in log.read_text().split() if line.startswith('entry:')]
        self.assertTrue(points, 'no fault points were recorded for the entry')
        for point in points:
            with self.subTest(point):
                world = self.world()
                self.assertOk(world.install(world.bundle(), '--no-start'))
                cut = world.run('entry', 'on', DOSPLEX_TEST_CRASH_AT=point)
                self.assertNotEqual(cut.code, 0, point)
                self.assertOk(world.run('repair'))
                done = world.state().get('entry') is True
                for name in (self.SOCKET, self.SERVICE, self.LINK, self.CONFIG, self.DESKTOP):
                    self.assertEqual(os.path.lexists(world.tree / name), done, (point, name))
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')

    def test_the_policy_lets_dosplex_start_only_its_own_socket(self):
        world = self.installed()
        started = [argv for argv in self.systemctl(world) if argv[:1] == ['start']]
        self.assertEqual(started, [['start', 'dosplex-start.socket']])


    # --- adding the entry to Steam's library (steamos-add-to-steam) ---------------------------------------------
    TOOL = 'steamos-add-to-steam'
    SHORTCUTS = 'home/deck/.local/share/Steam/userdata/4242/config/shortcuts.vdf'
    IN_LIBRARY = "Dosplex is in Steam's library: Library, Non-Steam, Dosplex."
    SLOW = {'DOSPLEX_TEST_STEAM_SECONDS': '1'}

    def steam_running(self, world):
        (world.tree / 'proc/200').mkdir(exist_ok=True)
        (world.tree / 'proc/200/comm').write_text('steam\n')
        (world.tree / 'proc/200/status').write_text('Uid:\t%d\t%d\t%d\t%d\n' % ((1000,) * 4))

    def added(self, world):
        return [c['argv'] for c in world.commands(self.TOOL)]

    def test_an_install_with_steam_running_adds_the_entry_as_the_main_user(self):
        world = self.world()
        self.steam_running(world)
        result = world.install(world.bundle(), '--no-start', **self.ON)
        self.assertOk(result)
        self.assertIn(self.IN_LIBRARY, result.out)
        self.assertNotIn('by hand', result.out)
        self.assertEqual(self.added(world), [[str(world.tree / self.DESKTOP)]])
        (runner,) = [c['argv'] for c in world.commands('runuser') if self.TOOL in c['argv']]
        self.assertEqual(runner[:3], ['-u', 'deck', '--'])
        env = json.loads((world.fake / 'add-env.json').read_text())
        self.assertEqual(env, {'DISPLAY': ':0', 'XDG_RUNTIME_DIR': '/run/user/1000',
                               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'})
        self.assertIn("Start from Steam's library: ready, in Steam's library", world.run('status').out)
        self.assertEqual(world.run('entry', 'status').out.strip(), "Start from Steam's library: ready, in Steam's library")

    def test_an_entry_that_is_in_the_library_already_is_not_added_again(self):
        world = self.world()
        self.steam_running(world)
        world.write(self.SHORTCUTS, 'x dosplex-start-client.py x')
        result = world.install(world.bundle(), '--no-start', **self.ON)
        self.assertOk(result)
        self.assertIn(self.IN_LIBRARY, result.out)
        self.assertEqual(self.added(world), [])

    def test_nothing_is_run_when_a_precondition_is_missing_and_the_message_says_which(self):
        def without_tool(world):
            (world.bin / self.TOOL).unlink()

        def with_session(world):
            self.assertOk(world.install(world.bundle(), '--no-start', **self.ON))
            self.assertOk(world.run('start'))
            (world.tree / self.SHORTCUTS).unlink()              # the install added it; this case is about a library without
        cases = (('Steam is not running', None, False),
                 ('this system has no steamos-add-to-steam', without_tool, True),
                 ('Dosplex is running', with_session, True))
        for reason, prepare, steam in cases:
            with self.subTest(reason):
                world = self.world()
                if steam:
                    self.steam_running(world)
                if prepare:
                    prepare(world)
                if reason == 'Dosplex is running':
                    result = world.run('entry', 'on', **self.ON)
                else:
                    result = world.install(world.bundle(), '--no-start', **self.ON)
                self.assertOk(result)
                self.assertIn("Dosplex could not be added to Steam's library by itself (%s)" % reason, result.out)
                self.assertIn('"Add a Non-Steam Game"', result.out)
                self.assertIn('sudo /opt/dosplex/bin/dosplex entry on', result.out)
                self.assertEqual(len(self.added(world)), 1 if reason == 'Dosplex is running' else 0)   # the install's own
                if reason != 'Dosplex is running':
                    self.assertRegex(result.out, r'Until then: sudo \S*dosplex start')

    def test_a_tool_that_fails_hangs_or_writes_nothing_does_not_fail_the_install(self):
        for flag in ('add-fail', 'add-hang', 'add-nothing'):
            with self.subTest(flag):
                world = self.world()
                self.steam_running(world)
                (world.fake / flag).write_text('')
                result = world.install(world.bundle(), '--no-start', **self.ON, **self.SLOW)
                self.assertOk(result)
                self.assertIn("Dosplex could not be added to Steam's library by itself (", result.out)
                self.assertIn('sudo /opt/dosplex/bin/dosplex entry on', result.out)
                self.assertRegex(result.out, r'Until then: sudo \S*dosplex start')
                self.assertIs(world.state()['entry'], True)
                self.assertEqual(len(self.added(world)), 1)
                self.assertIn('not yet added', world.run('entry', 'status').out)

    def test_entry_on_repairs_a_missing_library_entry_and_status_tells_both_states(self):
        world = self.installed()
        self.assertIn('not yet added', world.run('entry', 'status').out)
        self.assertEqual(self.added(world), [])
        self.steam_running(world)
        result = world.run('entry', 'on', **self.ON)
        self.assertOk(result)
        self.assertIn('already set up', result.out)
        self.assertIn(self.IN_LIBRARY, result.out)
        self.assertEqual(self.added(world), [[str(world.tree / self.DESKTOP)]])
        self.assertIn("ready, in Steam's library", world.run('entry', 'status').out)
        self.assertOk(world.run('entry', 'on'))                     # already there: not asked again
        self.assertEqual(len(self.added(world)), 1)

    def test_the_policy_runs_the_add_tool_only_for_the_one_entry_and_never_as_root(self):
        world = self.world()
        env = dosplex.Env(world.tree)
        account = dosplex.user_entry(env, 'deck')
        entry = str(world.tree / self.DESKTOP)
        dosplex.check_command(env, dosplex.user_command(account, 'DISPLAY=:0', self.TOOL, entry))
        other = str(world.tree / 'home/deck/.local/share/applications/other.desktop')
        for argv in ([self.TOOL, entry], ['/usr/bin/' + self.TOOL, entry],
                     dosplex.user_command(account, self.TOOL), dosplex.user_command(account, self.TOOL, other),
                     dosplex.user_command(account, self.TOOL, '/bin/sh'),
                     dosplex.user_command(account, self.TOOL, entry, other),
                     dosplex.user_command(account, self.TOOL, entry + '/../../x'),
                     dosplex.user_command(account, self.TOOL, '--help'),
                     dosplex.user_command(account, self.TOOL, '/home/deck/' + dosplex.ENTRY_DESKTOP)):
            with self.subTest(argv=argv), self.assertRaises(dosplex.PolicyError):
                dosplex.check_command(env, argv)


class BusPolicies(Base):
    """An uninstall removes the policy files that sessions left on the system bus (TRL-06): a session takes its own
    away at a stop, one that a hard restart wiped out cannot, and the Guest account would stay shut out for good."""
    FOLDER = 'etc/dbus-1/system.d'
    OURS_NOW = ('<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"\n'
                ' "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n<!-- Dosplex -->\n<busconfig>\n'
                '  <policy user="guest">\n    <deny own_prefix="org.bluez"/>\n    <deny send_destination="org.bluez"/>\n'
                '    <deny send_destination="com.steampowered.SteamOSManager1"/>\n  </policy>\n</busconfig>\n')

    def test_uninstall_removes_what_is_dosplexs_and_leaves_everything_else(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        folder = world.tree / self.FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        ours = [folder / ('dosplex-%s-guest-no-bluez.conf' % (letter * 32)) for letter in 'ab']
        for path in ours:
            path.write_text(self.OURS_NOW)
        kept = {
            folder / ('dosplex-%s-guest-no-bluez.conf' % ('c' * 32)): self.OURS_NOW.replace('<deny own_prefix', '<allow own_prefix'),
            folder / ('dosplex-%s-guest-no-bluez.conf' % ('d' * 32)): 'not a bus file\n',
            folder / 'org.freedesktop.NetworkManager.conf': self.OURS_NOW,           # ours in text, not in name
            folder / 'dosplex-short-guest-no-bluez.conf': self.OURS_NOW,
        }
        for path, text in kept.items():
            path.write_text(text)
        link = folder / ('dosplex-%s-guest-no-bluez.conf' % ('e' * 32))
        link.symlink_to(folder / 'org.freedesktop.NetworkManager.conf')
        self.assertOk(world.run('uninstall'))
        for path in ours:
            self.assertFalse(path.exists(), path.name)
        for path, text in kept.items():
            self.assertEqual(path.read_text(), text, path.name)
        self.assertTrue(link.is_symlink())
        reloads = [c['argv'] for c in world.commands('busctl')]
        self.assertEqual(reloads, [['--system', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                    'org.freedesktop.DBus', 'ReloadConfig']])

    def test_an_uninstall_with_nothing_left_behind_asks_the_bus_nothing(self):
        world = self.world()
        self.assertOk(world.install(world.bundle(), '--no-start'))
        self.assertOk(world.run('uninstall'))
        self.assertEqual(world.commands('busctl'), [])


class Autostart(Base):
    """`dosplex autostart on|off|status`: the one unit that runs `dosplex start --boot` at boot."""
    UNIT = 'etc/systemd/system/dosplex-autostart.service'
    LINK = 'etc/systemd/system/graphical.target.wants/dosplex-autostart.service'
    EXPECTED_UNIT = '''[Unit]
Description=Dosplex at boot
After=display-manager.service
ConditionPathExists=/opt/dosplex/bin/dosplex

[Service]
Type=oneshot
ExecStart=/opt/dosplex/bin/dosplex start --boot
TimeoutStartSec=600

[Install]
WantedBy=graphical.target
'''

    def installed(self, **options):
        world = self.world(**options)
        self.assertOk(world.install(world.bundle(), '--no-start'))
        return world

    def systemctl(self, world):
        return [c['argv'] for c in world.commands('systemctl')]

    def test_it_is_off_after_an_install_and_the_install_says_how_to_turn_it_on(self):
        world = self.world()
        result = world.install(world.bundle(), '--no-start')
        self.assertOk(result)
        self.assertIn('autostart on --experimental', result.out.splitlines()[-1])
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertNotIn('autostart', world.state())
        self.assertIn('Starts at boot: no', world.run('status').out)
        self.assertFalse(json.loads(world.run('status', '--json').out)['autostart'])
        self.assertEqual(world.run('autostart', 'status').out.strip(), 'Starts at boot: no')

    def test_on_writes_the_unit_and_the_link_and_never_starts_it(self):
        world = self.installed()
        calls = len(self.systemctl(world))
        result = world.run('autostart', 'on', '--experimental')
        self.assertOk(result)
        self.assertIn('starts at every boot', result.out)
        unit = world.tree / self.UNIT
        self.assertEqual(unit.read_text(), self.EXPECTED_UNIT)
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o644)
        self.assertEqual(os.readlink(world.tree / self.LINK), '../dosplex-autostart.service')
        self.assertIs(world.state()['autostart'], True)
        self.assertEqual(self.systemctl(world)[calls:], [['daemon-reload']])      # no start, no enable --now
        records = world.journal()['0002-autostart.jsonl']
        self.assertEqual([r['step'] for r in records if r['kind'] == 'intent'], ['unit', 'state'])
        self.assertEqual(records[-1]['outcome'], 'complete')
        self.assertIn('Starts at boot: yes', world.run('status').out)
        self.assertTrue(json.loads(world.run('status', '--json').out)['autostart'])
        self.assertEqual(world.run('autostart', 'status').out.strip(), 'Starts at boot: yes')
        again = world.run('autostart', 'on', '--experimental')
        self.assertOk(again)
        self.assertIn('already starts at boot', again.out)
        self.assertEqual(len(self.systemctl(world)[calls:]), 1)

    def test_off_removes_the_unit_and_the_link_and_the_state_key(self):
        world = self.installed()
        before = world.snapshot()
        self.assertOk(world.run('autostart', 'on', '--experimental'))
        result = world.run('autostart', 'off')
        self.assertOk(result)
        self.assertIn('no longer starts at boot', result.out)
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))
        self.assertNotIn('autostart', world.state())
        self.assertEqual(world.snapshot(), before)          # the empty directory of the link went with it
        again = world.run('autostart', 'off')
        self.assertOk(again)
        self.assertIn('does not start at boot', again.out)

    def test_a_file_or_link_that_is_not_dosplexs_is_refused_and_nothing_changes(self):
        for name, make, words in (
                ('a unit', lambda w: (w.tree / self.UNIT).write_text('[Unit]\nDescription=mine\n'), 'not the one Dosplex writes'),
                ('a linked unit', lambda w: (w.tree / self.UNIT).symlink_to('/dev/null'), 'not the one Dosplex writes'),
                ('a link', lambda w: ((w.tree / self.LINK).parent.mkdir(), (w.tree / self.LINK).symlink_to('../other.service')),
                 'not the link Dosplex makes')):
            with self.subTest(name):
                world = self.installed()
                make(world)
                before = world.snapshot()
                self.assertFailed(world.run('autostart', 'on', '--experimental'), words, code=3)
                self.assertEqual(world.snapshot(), before)
                self.assertNotIn('autostart', world.state())
                self.assertEqual(self.systemctl(world), [])
                self.assertEqual(world.run('repair').out.strip(), 'Nothing to repair.')
                # Off leaves what is not Dosplex's alone when Dosplex never turned it on.
                self.assertOk(world.run('autostart', 'off'))
                self.assertEqual(world.snapshot(), before)

    def test_turning_it_on_needs_the_word_experimental_and_an_installation_that_has_it_on_keeps_it(self):
        # ADR-026: the first release starts only when asked. Starting at boot stays in the program, off, and nobody
        # turns it on by accident. What was on before an update stays on.
        world = self.installed()
        refused = world.run('autostart', 'on')
        self.assertFailed(refused, 'experimental', code=3)
        self.assertIn('autostart on --experimental', refused.text)
        self.assertIn('Starts at boot: no', world.run('status').out)
        self.assertFalse(world.state().get('autostart'))
        self.assertOk(world.run('autostart', 'on', '--experimental'))
        self.assertIn('Starts at boot: yes, experimental', world.run('status').out)
        again = world.run('autostart', 'on')                       # already on: nothing to refuse
        self.assertOk(again)
        self.assertIn('already starts at boot', again.out)

    def test_it_needs_root_and_an_install(self):
        world = self.installed()
        self.assertFailed(world.run('autostart', 'on', root=False), 'needs root')
        self.assertFalse((world.tree / self.UNIT).exists())
        empty = self.world()
        self.assertFailed(empty.run('autostart', 'on'), 'not installed')
        self.assertFailed(empty.run('autostart', 'status'), 'not installed')
        self.assertFailed(world.run('autostart', 'sometimes'), code=2)

    def test_a_failed_reload_undoes_everything(self):
        world = self.installed()
        (world.fake / 'reload-fails').write_text('')
        result = world.run('autostart', 'on', '--experimental')
        self.assertFailed(result, 'Failed to reload the daemon')
        self.assertFalse((world.tree / self.UNIT).exists())
        self.assertFalse(os.path.lexists(world.tree / self.LINK))
        self.assertNotIn('autostart', world.state())
        self.assertEqual(world.journal()['0002-autostart.jsonl'][-1]['outcome'], 'rolled-back')

    def test_an_install_over_an_install_keeps_it_on_and_writes_a_unit_that_went_missing(self):
        world = self.installed()
        self.assertOk(world.run('autostart', 'on', '--experimental'))
        newer = world.bundle(name='newer', version='0.2.0-test')
        result = world.install(newer, '--no-start')
        self.assertOk(result)
        self.assertIs(world.state()['autostart'], True)
        self.assertEqual((world.tree / self.UNIT).read_text(), self.EXPECTED_UNIT)
        self.assertNotIn('autostart on', result.out)                # it is on already: no hint
        (world.tree / self.UNIT).unlink()
        third = world.bundle(name='third', version='0.3.0-test')
        result = world.install(third, '--no-start')
        self.assertOk(result)
        self.assertEqual((world.tree / self.UNIT).read_text(), self.EXPECTED_UNIT)
        self.assertIn('was written again', result.out)

    def test_uninstall_removes_the_unit_and_the_link_while_it_is_on(self):
        world = self.installed()
        self.assertOk(world.run('autostart', 'on', '--experimental'))
        calls = len(self.systemctl(world))
        log = world.root / 'faults.log'
        result = world.run('uninstall', '--remove-guest', '--yes', DOSPLEX_TEST_FAULT_LOG=str(log))
        self.assertOk(result)
        self.assertEqual(world.snapshot(), world.initial)
        self.assertEqual(self.systemctl(world)[calls:], [['daemon-reload']])
        # (The journal is gone with the rest of /var/lib/dosplex after this uninstall: the fault log has the order.)
        points = log.read_text().splitlines()
        self.assertLess(points.index('uninstall:done:remove-autostart'), points.index('uninstall:intent:stop-session'))

    def test_uninstall_leaves_a_file_that_is_not_dosplexs_where_it_is(self):
        world = self.installed()
        (world.tree / self.UNIT).write_text('[Unit]\nDescription=mine\n')
        self.assertOk(world.run('uninstall'))
        self.assertEqual((world.tree / self.UNIT).read_text(), '[Unit]\nDescription=mine\n')

    def test_on_and_off_interrupted_at_every_journal_point_are_finished_or_undone_by_repair(self):
        probe = self.installed()
        log = probe.root / 'faults.log'
        self.assertOk(probe.run('autostart', 'on', '--experimental', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = []
        for point in log.read_text().splitlines():
            if point.startswith('autostart:') and point not in points:
                points.append(point)
        for expected in ('autostart:begin', 'autostart:commit', 'autostart:intent:unit', 'autostart:applied:unit',
                         'autostart:done:state', 'autostart:end'):
            self.assertIn(expected, points)
        for point in points:
            with self.subTest(point=point, verb='on'):
                world = self.installed()
                crashed = world.run('autostart', 'on', '--experimental', DOSPLEX_TEST_CRASH_AT=point)
                self.assertFailed(crashed)
                self.assertOk(world.run('repair'))
                on = world.state().get('autostart') is True
                self.assertEqual(on, point != 'autostart:begin', point)       # a commit on disk is finished, else undone
                self.assertEqual((world.tree / self.UNIT).exists(), on)
                self.assertEqual(os.path.islink(world.tree / self.LINK), on)
        log = probe.root / 'faults-off.log'
        self.assertOk(probe.run('autostart', 'off', DOSPLEX_TEST_FAULT_LOG=str(log)))
        points = []
        for point in log.read_text().splitlines():
            if point.startswith('autostart:') and point not in points:
                points.append(point)
        self.assertIn('autostart:applied:unit-remove', points)
        for point in points:
            with self.subTest(point=point, verb='off'):
                world = self.installed()
                self.assertOk(world.run('autostart', 'on', '--experimental'))
                crashed = world.run('autostart', 'off', DOSPLEX_TEST_CRASH_AT=point)
                self.assertFailed(crashed)
                self.assertOk(world.run('repair'))
                on = world.state().get('autostart') is True
                self.assertEqual(on, point == 'autostart:begin', point)
                self.assertEqual((world.tree / self.UNIT).exists(), on)
                self.assertEqual(os.path.islink(world.tree / self.LINK), on)



def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id().split('.', 1)[1]


def weight(name):
    """A rough cost, so that the slow tests are spread over the workers."""
    if 'at_every' in name:
        return 10
    if 'crash_inside_repair' in name:
        return 6
    return 3 if name.startswith('Session.') else 1


def run_in_parallel(workers):
    """Run every test in `workers` processes (the tests are independent: each builds its own fake sysroot)."""
    names = sorted(test_ids(unittest.defaultTestLoader.loadTestsFromModule(sys.modules['__main__'])),
                   key=lambda n: -weight(n))
    buckets = [[] for _ in range(workers)]
    loads = [0] * workers
    for name in names:
        index = loads.index(min(loads))
        buckets[index].append(name)
        loads[index] += weight(name)
    started = time.monotonic()
    processes = [subprocess.Popen([sys.executable, __file__, *bucket], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for bucket in buckets if bucket]
    failed = False
    for process in processes:
        output, _ = process.communicate()
        failed = failed or process.returncode != 0
        if process.returncode != 0:
            print(output, end='')
    print('Ran %d tests in %d processes in %.1fs: %s' % (len(names), len(processes), time.monotonic() - started,
                                                        'FAILED' if failed else 'OK'))
    return 1 if failed else 0


if __name__ == '__main__':
    # With test names on the command line, run just those. Without, use several processes.
    if len(sys.argv) > 1 or os.environ.get('DOSPLEX_TEST_SERIAL') == '1':
        unittest.main()
    sys.exit(run_in_parallel(min(6, os.cpu_count() or 1)))
