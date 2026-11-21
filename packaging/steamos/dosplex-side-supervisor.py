#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Own exactly one side's transient service; never clean another user session."""
import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

spec = importlib.util.spec_from_file_location("display_state", Path(__file__).with_name("dosplex-display-state.py"))
display_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(display_state)
wine_spec = importlib.util.spec_from_file_location("wine_modelist", Path(__file__).with_name("dosplex-wine-modelist.py"))
wine_modelist = importlib.util.module_from_spec(wine_spec)
wine_spec.loader.exec_module(wine_modelist)
loopback_spec = importlib.util.spec_from_file_location("side_loopback", Path(__file__).with_name("dosplex-side-loopback.py"))
side_loopback = importlib.util.module_from_spec(loopback_spec)
loopback_spec.loader.exec_module(side_loopback)


def prepare_privileged_state():
    # Share the auth boundary rather than letting request/supervisor entry
    # points trust state that authentication would reject. Installed helpers
    # live in libexec; the second location supports the source checkout.
    here = Path(__file__).resolve()
    helper = here.parent.parent / 'libexec/dosplex-side-auth.py'
    if not helper.is_file():
        helper = here.parents[2] / 'guest/dosplex-side-auth.py'
    spec = importlib.util.spec_from_file_location('dosplex_state_admission', helper)
    admission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admission)
    admission.prepare_state_root()


# Where the audio service provides Guest's restricted endpoint (see
# dosplex-audio-router.py, DOSPLEX_GUEST_AUDIO_DIR). Guest never defaults to the
# shared server: with no endpoint it has no audio.
DEFAULT_GUEST_AUDIO_SERVER = 'unix:/run/dosplex/audio/guest/pulse/native'


def audio_environment(role):
    if role == 'guest':
        return {'DOSPLEX_GUEST_AUDIO_SERVER': os.environ.get('DOSPLEX_GUEST_AUDIO_SERVER', DEFAULT_GUEST_AUDIO_SERVER)}
    return {'DOSPLEX_AUDIO_SERVER': os.environ.get('DOSPLEX_AUDIO_SERVER', '127.0.0.1')}


def desktop_environment():
    """DOSPLEX_DESKTOP_CONFIG_ROOT, when a trial or retained experiment moves the Desktop mode profile of its sides.

    The side's service starts with an empty environment, so the launcher only learns of it from here. Without
    it the launcher keeps each profile in the home of the side user.
    """
    root = os.environ.get('DOSPLEX_DESKTOP_CONFIG_ROOT', '')
    return {'DOSPLEX_DESKTOP_CONFIG_ROOT': root} if root else {}


def say(message):
    try:
        print('Dosplex supervisor: ' + message, flush=True)
    except (OSError, ValueError):
        pass   # a journal that cannot be written to must not stop the supervisor


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.session-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# Where udisks mounts a user's removable drives: the SD card and the USB drives of SteamOS's automount.
MEDIA_ROOT = Path('/run/media')


def prepare_media_root(root=MEDIA_ROOT):
    """Make /run/media if no drive has been mounted since boot; return whether this made it.

    Guest's launcher covers /run/media with an empty tmpfs, so that Main's drives are not in Guest's view
    whatever the mode of their folder is. Bubblewrap cannot make that mount point on the launcher's read-only
    root: without /run/media, Guest starts with nothing covered, and a drive that is mounted later is kept from
    it by the mode of its folder alone. The directory is empty, root's and mode 0755, as udisks makes it for the
    first drive, and it lies on /run, so a reboot removes it. Whatever is there already (a directory of any
    owner or mode, a link, a file) is left for the launcher to judge. A failure is said and never stops a side.
    """
    try:
        os.mkdir(root, 0o755)
        # The umask cut mkdir's mode. Open without following a link, in case the name changed in between.
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, 0o755)
        finally:
            os.close(fd)
    except FileExistsError:
        return False
    except OSError as error:
        say(f'could not make {root}: {error}; Guest starts without it')
        return False
    say(f'made {root} (empty, root, 0755) so that Guest can cover it; a reboot removes it')
    return True


# Where the programs that give Guest its own loopback address are pinned (dosplex-side-loopback.py, T-452). A bpf
# filesystem keeps nothing over a reboot.
LOOPBACK_ROOT = Path('/sys/fs/bpf/dosplex')


def prepare_loopback(side, root=LOOPBACK_ROOT, address=None):
    """Pin the programs that give this side its own IPv4 loopback address; return the systemd-run arguments that
    attach them to its session unit.

    Both sides share the host's network namespace and so 127.0.0.1, where each Steam wants the same fixed ports.
    Without this the Steam that starts second loses them, and that side's games reach the other account's Steam
    and exit. DOSPLEX_GUEST_LOOPBACK names another address in 127.0.0.0/8, or `shared` to leave the side on the
    host's loopback. A set left behind by a supervisor that was killed is replaced. A failure is said and never
    stops a side: it then starts on the shared loopback, as it did before, and returns [].
    """
    address = address or os.environ.get('DOSPLEX_GUEST_LOOPBACK') or side_loopback.DEFAULT_ADDRESS
    if address == 'shared':
        say(f'{side} keeps the shared loopback (DOSPLEX_GUEST_LOOPBACK=shared)')
        return []
    directory = Path(root) / side
    try:
        address = side_loopback.side_address(address)
        side_loopback.unpin(directory)
        Path(root).mkdir(mode=0o700, exist_ok=True)
        side_loopback.pin(directory, address)
    except (OSError, ValueError) as error:
        say(f'{side} starts on the shared loopback: {error}')
        return []
    say(f'{side} has its own loopback address {address} (programs pinned in {directory})')
    return side_loopback.systemd_properties(directory)


def release_loopback(side, root=LOOPBACK_ROOT):
    """Remove this side's pinned programs once its last session unit is stopped, and the folder when it is empty."""
    try:
        side_loopback.unpin(Path(root) / side)
        Path(root).rmdir()
    except OSError:
        pass   # the other side's set is still there, or nothing was pinned


def game_store_module():
    spec = importlib.util.spec_from_file_location('game_store', Path(__file__).with_name('dosplex-game-store.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_game_library(owner, store=None, module=None):
    """Merge the shared game store into the account's own libraries (an overlay on the steamapps folder of its library on
    the primary's disk and on every disk that has a store) and return the folders the overlays are mounted on, joined
    with ':', or None.

    Off unless DOSPLEX_GAME_STORE names the store (ADR-023). Called before every start of the side, while its
    Steam is closed; a folder that is mounted already is left as it is. The account's Steam list is not touched: the
    shared games sit in the libraries it already has (an account that has none on a disk gets one there, and that one is
    listed). A failure is said and never stops a side: it starts with its own libraries, and the manifests of the
    shared games stay out of them (stashed) so that Steam does not offer to download them again.
    """
    store = store if store is not None else os.environ.get('DOSPLEX_GAME_STORE', '')
    if not store:
        return None
    try:
        module = module or game_store_module()
        layout = module.Layout(store)
    except Exception as error:
        say(f'{owner} starts without the shared game library: {error}')
        return None
    try:
        paths, problems = module.mount_all(layout, owner)
    except Exception as error:
        say(f'{owner} starts without the shared game library: {error}')
        return None
    for problem in problems:
        say(f'{owner} starts without the shared game library: {problem}')
    return ':'.join(paths) or None


_PERMANENT_SAID = []


def release_game_library(owner, store=None, module=None):
    """Take the shared store out of the account's libraries when its last session unit is stopped, unless the store is
    permanent.

    With a permanent store (a config in mode on) the overlays stay mounted: the stock session needs them, and the
    store's slot is what unmounts them. This is said once at most. Without a config, or when the config is broken
    (said, then the old way), nothing mounts the overlays at boot, and the account's Steam also runs without Dosplex:
    the overlays are unmounted, and the manifests of the shared games (and the whiteouts for them) are moved out of its
    real libraries into the store's stash, so that Steam does not find games it cannot see. The next start puts them back.
    Whatever the account's Steam and games wrote stays in its libraries.
    """
    store = store if store is not None else os.environ.get('DOSPLEX_GAME_STORE', '')
    if not store:
        return
    try:
        module = module or game_store_module()
        layout = module.Layout(store)
    except Exception as error:
        say(f'{owner}: the shared game library was not put away: {error}')
        return
    try:
        if module.permanent(layout):
            if not _PERMANENT_SAID:
                _PERMANENT_SAID.append(True)
                say('the shared game library is permanent: it stays mounted')
            return
    except Exception as error:
        say(f'{owner}: the shared game store config is unusable ({error}); putting the library away')
    try:
        stores = module.listed_layouts(layout)
    except Exception as error:
        say(f'{owner}: the list of disks is unusable ({error}); only the main library is put away')
        stores = [layout]
    for item in stores:
        try:
            module.unmount(item, owner)
        except Exception as error:
            say(f'{owner}: the shared game library was not put away: {error}')


class Session:
    """A unique systemd unit is the ownership handle, never a recovered PID."""
    def __init__(self, side, owner, command, environment, *, user_manager=False, parent_unit=None, properties=()):
        if side not in ('left', 'right'):
            raise ValueError('invalid side')
        self.properties = list(properties)
        if any(not isinstance(item, str) or not item.startswith('--property=BPFProgram=') for item in self.properties):
            raise ValueError('invalid session property')
        if self.properties and user_manager:
            raise ValueError('a user manager cannot attach programs to a session unit')
        self.name = f'dosplex-session-{side}-{uuid.uuid4().hex}'
        self.unit = self.name + '.service'
        self.manager = ['--user'] if user_manager else []
        self.parent_unit = parent_unit if user_manager else (parent_unit or
            os.environ.get('DOSPLEX_COMPOSITOR_UNIT', 'dosplex-compositor.service'))
        if self.parent_unit is not None and not re.fullmatch(
                r'dosplex-compositor(?:-[a-f0-9]{32})?\.service', self.parent_unit):
            raise ValueError('invalid owning compositor unit')
        self.runtime = Path(os.environ['XDG_RUNTIME_DIR'] if user_manager else '/run') / self.name
        self.environment = dict(environment, XDG_RUNTIME_DIR=str(self.runtime), DOSPLEX_SESSION_RUNTIME='1')
        self.owner = owner
        self.command = list(command)
        self.started = False
        self.frozen = False

    def start(self):
        if self.started:
            raise RuntimeError('session already started')
        args = ['systemd-run', *self.manager, '--quiet', '--collect',
                '--service-type=exec', '--expand-environment=no', '--unit=' + self.unit,
                '--property=KillMode=control-group', '--property=TimeoutStopSec=10s',
                '--property=SendSIGKILL=yes', '--property=RuntimeDirectory=' + self.name,
                '--property=RuntimeDirectoryMode=0700']
        if not self.manager:
            args += ['--property=User=' + self.owner]
        if self.parent_unit:
            args += ['--property=BindsTo=' + self.parent_unit,
                     '--property=After=' + self.parent_unit]
        # systemd attaches these before the unit's first process runs.
        args += self.properties
        # env -i avoids inheriting the service manager's desktop/Wayland/D-Bus
        # environment. Explicit argv also avoids shell evaluation of user data.
        args += ['--', '/usr/bin/env', '-i']
        args += [f'{key}={value}' for key, value in self.environment.items()]
        args += self.command
        self.started = True
        # Without --no-block, systemd-run waits for the Exec start job. This
        # closes the race where stop could run before the unit was registered.
        subprocess.run(args, check=True, timeout=15)

    def is_running(self):
        return self.started and subprocess.run(
            ['systemctl', *self.manager, 'is-active', '--quiet', self.unit],
            timeout=5, check=False).returncode == 0

    def stop(self):
        if not self.started:
            return
        if self.frozen:
            self.set_standby(False)
        # An exited main process may have descendants: always ask systemd to
        # stop the whole owned cgroup. A collected unit is already gone.
        stopped = subprocess.run(['systemctl', *self.manager, 'stop', self.unit],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=20, check=False)
        observed = subprocess.run(
            ['systemctl', *self.manager, 'show', self.unit,
             '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
             '-p', 'MainPID', '-p', 'ControlPID'],
            capture_output=True, text=True, timeout=5, check=False)
        properties = dict(line.split('=', 1) for line in observed.stdout.splitlines() if '=' in line)
        gone = properties.get('LoadState') == 'not-found'
        inactive = (properties.get('LoadState') in ('loaded', 'not-found') and
                    properties.get('ActiveState') == 'inactive' and
                    properties.get('SubState') == 'dead' and
                    properties.get('MainPID', '0' if gone else '') == '0' and
                    properties.get('ControlPID', '0' if gone else '') == '0')
        if observed.returncode != 0 or not inactive or (stopped.returncode != 0 and not gone):
            raise RuntimeError('owned session stop was not confirmed: ' + self.unit)
        if self.runtime.exists():
            raise RuntimeError('owned session runtime remains after stop: ' + self.unit)
        self.started = False

    def set_standby(self, enabled):
        if not self.started:
            raise RuntimeError('cannot freeze an unstarted session')
        # A timeout may leave a successfully frozen cgroup. Require a verified
        # thaw before clearing this flag, including during cleanup.
        if enabled:
            self.frozen = True
        subprocess.run(['systemctl', *self.manager, 'freeze' if enabled else 'thaw', self.unit],
                       check=True, capture_output=True, timeout=10)
        observed = subprocess.check_output(['systemctl', *self.manager, 'show', self.unit,
            '-p', 'FreezerState', '--value'], text=True, timeout=5).strip()
        if observed != ('frozen' if enabled else 'running'):
            raise RuntimeError('session freezer state was not confirmed: '+observed)
        self.frozen = enabled


def desired_activity(path, side, role):
    data = json.loads(Path(path).read_text())
    activity = data[side].get('activity', 'active')
    if activity not in ('active', 'standby', 'off'):
        raise ValueError('invalid session activity state')
    if activity == 'active' and role == 'guest' and data['layout'] == 'single':
        return 'standby'
    return activity


def requested_mode(directory):
    try:
        mode = (directory / 'mode').read_text().strip()
    except FileNotFoundError:
        return 'gaming'
    if mode not in ('gaming', 'desktop'):
        raise ValueError('invalid requested activity')
    return mode


def restart_request(directory):
    try:
        return (directory / 'restart').read_text()[:128]
    except FileNotFoundError:
        return ''


def side_state_root():
    return Path(os.environ.get('DOSPLEX_SIDE_ROOT', '/var/lib/dosplex/sides'))


# Steam's "Steam Controller detected!" tour (T-504). It is a per-account Steam setting, ShowIbexWelcomeTour in
# userdata/<account>/7/remote/sharedconfig.vdf: "0" switches it off and anything else, a missing key included,
# leaves it on. Inside a side the tour cannot be finished (Steam's firmware check fails there), so it returns at
# every Steam start and controller wake and takes the input focus from games and installers. Dosplex reads the
# setting and says so; it never writes Steam's files.
TOUR_KEY = 'showibexwelcometour'
TOUR_MAX_ACCOUNTS = 64
TOUR_MAX_BYTES = 16 * 1024 * 1024
TOUR_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\s+|(.)', re.DOTALL)
TOUR_NOTICE = ('Steam shows "Steam Controller detected!" for this account and it cannot finish inside Dosplex. '
               'Switch it off in this side\'s Steam: Settings > Developer > Show Steam Controller Tour '
               '(Developer appears after Settings > System > Enable Developer Mode). Or close Steam and set '
               '"ShowIbexWelcomeTour" "0" in userdata/<account>/7/remote/sharedconfig.vdf.')


def tour_setting(data):
    """The value of ShowIbexWelcomeTour in a sharedconfig.vdf (bytes), None when the key is missing.

    The key may sit at any depth and its case is ignored, as Steam's is. ValueError for a file that is not
    well-formed VDF: text that is not UTF-8, a character outside strings and braces, an unterminated string or
    unbalanced braces.
    """
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('not UTF-8 text') from None
    depth, found, key = 0, None, None   # key: the string just read as a key, until its value or object
    for token in TOUR_TOKEN.finditer(text):
        string, brace, stray = token.groups()
        if stray is not None:
            raise ValueError('unexpected character or unterminated string')
        if string is not None:
            if key is None:
                key = string
            else:
                if key.lower() == TOUR_KEY and found is None:
                    found = string
                key = None
        elif brace == '{':
            depth += 1
            key = None
        elif brace == '}':
            depth -= 1
            key = None
            if depth < 0:
                raise ValueError('unbalanced braces')
    if depth:
        raise ValueError('unbalanced braces')
    return found


def steam_controller_sides(root):
    """The sides that hold a Steam Controller now, as a set of 'left' and 'right'.

    Read from the routing plans of the enrolled Steam Controller slots (`root`/libexec/native-controller-*-plan.json),
    whose routing_side is the live owner. A missing, unreadable or malformed plan counts for nothing.
    """
    sides = set()
    for path in sorted(Path(root, 'libexec').glob('native-controller-*-plan.json'))[:16]:
        try:
            plan = json.loads(path.read_text())
            side = plan.get('routing_side', plan.get('side'))
            if plan.get('protocol') == 'ibex' and side in ('left', 'right'):
                sides.add(side)
        except (OSError, ValueError, AttributeError):
            continue
    return sides


def steam_tour_state(home, uid):
    """Read the owner's Steam accounts: {'on': [...], 'off': [...], 'unreadable': [...]} with account ids.

    Read only; nothing is created or changed. An account without a sharedconfig.vdf yet is left out. One whose
    file cannot be read, is not a private regular file or is malformed goes to 'unreadable' and counts as
    unknown. A missing home or Steam directory gives empty lists.
    """
    state = {'on': [], 'off': [], 'unreadable': []}
    userdata = Path(home) / '.local/share/Steam/userdata'
    try:
        for directory in (Path(home), userdata.parent, userdata):
            wine_modelist._check_directory(directory, uid)
        names = sorted(entry.name for entry in os.scandir(userdata)
                       if entry.name.isdigit() and entry.name != '0' and entry.is_dir(follow_symlinks=False))
    except OSError:
        return state
    for account in names[:TOUR_MAX_ACCOUNTS]:
        try:
            data, _ = wine_modelist._safe_regular(userdata / account / '7/remote/sharedconfig.vdf', uid, TOUR_MAX_BYTES)
            value = tour_setting(data)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            state['unreadable'].append(account)
            continue
        state['off' if value == '0' else 'on'].append(account)
    return state


# Display contract (ADR-012). A version-1 state file restarts a side when any of these
# committed fields changes (layout and position never did). A version-2 state file restarts
# it for none of them: the follower inside the side applies live changes and reports back.
V1_RESTART_FIELDS = ('width', 'height', 'game_width', 'game_height', 'scale_mode', 'display_revision')
DESKTOP_SCALE_KEYS = {'instance', 'desktop_scale_milli'}
DISPLAY_REPORT_KEYS = {'instance', 'display', 'revision', 'game_width', 'game_height', 'result', 'detail'}
# What a display report says became of a revision. "deferred" is a change that waits because a game runs: the
# side keeps its game monitor (the size in the report) and Gamescope scales the picture. A later report for the
# same revision replaces it with "applied" or "failed".
DISPLAY_RESULTS = ('applied', 'failed', 'deferred')
# While the loop waits for a slow call (serve_control_during in supervise) it looks at the control socket this often.
CONTROL_SERVICE_SECONDS = 0.02
# A systemctl that outlasts its timeout is no reason to end the supervisor: the machine may have stalled as a whole
# (a suspend that did not complete freezes every process for as long as it takes the kernel to give up, and the
# clocks go on), or systemd is busy. Stopping the service is asked again this many times before the error counts.
STOP_ATTEMPTS = 4
# At most one line in this many seconds about calls that keep timing out.
SLOW_CALL_SAY_SECONDS = 30.0
# A Reset or a mode change is often asked for because the side is stuck, so its Steam is not waited for long.
RESTART_STEAM_QUIT_SECONDS = 15
# A stop may wait as long as `dosplex stop` waits for a Steam to quit.
STOP_STEAM_QUIT_SECONDS = 45


def display_change_restarts(started, current):
    """Whether a change of the display contract must restart a running side.

    `started` is the contract the side was launched with and `current` the one just read,
    both from display_state.read_contract. Only two version-2 contracts are exempt, so a
    state file of an older or mixed version keeps restarting exactly as before. What the
    follower reports (applied, failed or deferred) is no part of either contract.
    """
    if started['version'] == 2 and current['version'] == 2:
        return False
    return any(started['live'][field] != current['live'][field] for field in V1_RESTART_FIELDS)


def launch_display_fields(live):
    """session.json fields for a side launched with `live`: the launcher passes this game
    monitor and scaler to Gamescope, so they count as applied from the start."""
    return {'display_applied_revision': live['display_revision'],
            'display_applied_size': [live['game_width'], live['game_height']],
            'display_result': 'applied', 'display_detail': ''}


def display_report_fields(request):
    """Validate a display report and return its session.json fields; ValueError otherwise.

    The report carries exactly DISPLAY_REPORT_KEYS and one of DISPLAY_RESULTS as its result.
    The detail ends up in session.json for the compositor: one line of printable text of at
    most 200 characters.
    """
    if (not isinstance(request, dict) or set(request) != DISPLAY_REPORT_KEYS or
            request['display'] != 'report'):
        raise ValueError('invalid display report')
    revision, width, height = request['revision'], request['game_width'], request['game_height']
    result, detail = request['result'], request['detail']
    if (any(type(value) is not int for value in (revision, width, height)) or
            not 0 <= revision <= 4294967295 or not 1 <= width <= 16384 or not 1 <= height <= 16384):
        raise ValueError('invalid display report')
    if (result not in DISPLAY_RESULTS or type(detail) is not str or len(detail) > 200 or
            not detail.isprintable()):
        raise ValueError('invalid display report')
    return {'display_applied_revision': revision, 'display_applied_size': [width, height],
            'display_result': result, 'display_detail': detail}


def desktop_scale_fields(request):
    """The session.json field for a Desktop scale report (thousandths, 1000 to 3000); ValueError otherwise."""
    milli = request['desktop_scale_milli']
    if type(milli) is not int or not 1000 <= milli <= 3000:
        raise ValueError('invalid desktop scale')
    return {'desktop_scale_milli': milli}


def desktop_scale_milli(text):
    """The thousandths for the scale text the Desktop launcher chose ('1', '1.25', '2'); ValueError otherwise."""
    try:
        value = Decimal(text) * 1000
        valid = value.is_finite() and value == value.to_integral_value() and 1000 <= value <= 3000
    except (InvalidOperation, TypeError):
        valid = False
    if not valid:
        raise ValueError('invalid desktop scale')
    return int(value)


def validate_control_request(request, instance):
    """Classify a request to the control socket of `instance`: 'mode', 'get', 'report' or 'scale'.

    Exactly the messages of the protocol and nothing else; ValueError for anything else.
    The supervisor, its clients and the private-namespace proxy all decide with this function.
    """
    if not isinstance(request, dict) or request.get('instance') != instance:
        raise ValueError('invalid request')
    keys = set(request)
    if keys == {'instance', 'mode'} and request['mode'] in ('gaming', 'desktop'):
        return 'mode'
    if keys == {'instance', 'display'} and request['display'] == 'get':
        return 'get'
    if keys == DISPLAY_REPORT_KEYS:
        display_report_fields(request)
        return 'report'
    if keys == DESKTOP_SCALE_KEYS:
        desktop_scale_fields(request)
        return 'scale'
    raise ValueError('invalid request')


class SideControl:
    """One supervisor instance answers its owner's UID only, over one socket.

    The owner asks for a mode change. From inside the side, its display follower reads the
    live display contract and reports what it applied (ADR-012). The supervise loop keeps
    `live` (the live fields it last read from state.json, None while unknown) and
    `display_applied` (the display_applied_* fields of session.json while a session
    exists, None otherwise; a report is accepted only then). poll() serves one waiting
    connection; the loop calls it once per iteration and, while a slow call runs, every
    CONTROL_SERVICE_SECONDS (serve_control_during).
    """
    def __init__(self, directory, owner_uid, instance):
        self.path = directory / 'control.sock'
        self.directory, self.owner_uid, self.instance = directory, owner_uid, instance
        self.live = None
        self.display_applied = None
        self.desktop_scale = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.identity = None
        try:
            try:
                previous = self.path.lstat()
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
                    raise PermissionError('unexpected session control endpoint')
                # Caller holds supervisor.lock; an earlier instance has exited.
                self.path.unlink()
            self.listener.bind(str(self.path))
            info = self.path.lstat()
            self.identity = (info.st_dev, info.st_ino)
            self.path.chmod(0o666)
            self.listener.listen(8)
            self.listener.setblocking(False)
        except BaseException:
            self.close()
            raise

    def poll(self):
        try:
            connection, _ = self.listener.accept()
        except BlockingIOError:
            return
        with connection:
            connection.settimeout(.05)
            try:
                _, uid, _ = struct.unpack('3i', connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid != self.owner_uid:
                    raise PermissionError('wrong owner')
                raw, _, flags, _ = connection.recvmsg(1024)
                if flags & socket.MSG_TRUNC:
                    raise ValueError('oversized request')
                request = json.loads(raw)
                kind = validate_control_request(request, self.instance)
                # No caller-supplied side or state path: this endpoint belongs
                # to one locked supervisor and writes only its own directory.
                if kind == 'mode':
                    reply = self.request_mode(request['mode'])
                elif kind == 'get':
                    reply = self.display_get()
                elif kind == 'scale':
                    reply = self.desktop_scale_report(request)
                else:
                    reply = self.display_report(request)
                connection.sendall(reply)
            except (OSError, ValueError, TypeError):
                try:
                    connection.sendall(b'{"ok":false}')
                except OSError:
                    pass

    def request_mode(self, mode):
        if mode not in ('gaming', 'desktop'):
            raise ValueError('invalid request')
        fd, temporary = tempfile.mkstemp(prefix='.mode-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(mode + '\n')
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / 'mode')
        finally:
            Path(temporary).unlink(missing_ok=True)
        return b'{"ok":true}'

    def display_get(self):
        if self.live is None:
            raise ValueError('display contract unknown')
        return json.dumps({'ok': True, 'live': self.live}, separators=(',', ':')).encode()

    def display_report(self, request):
        fields = display_report_fields(request)
        if self.display_applied is None:
            raise ValueError('no session to report on')
        # Merge into the supervisor's own snapshot, never create it: a missing or foreign
        # file means the side was logged out or replaced, and a file written here would
        # make the compositor treat the side as needing recovery.
        path = self.directory / 'session.json'
        snapshot = json.loads(path.read_text())
        if not isinstance(snapshot, dict) or snapshot.get('instance') != self.instance:
            raise ValueError('no session snapshot for this instance')
        # The report replaces the earlier one whole, also for the same revision: a revision that was
        # deferred while a game ran is reported again as applied (or failed) once the follower has done it.
        atomic_json(path, {**snapshot, **fields})
        self.display_applied = fields
        return b'{"ok":true}'

    def desktop_scale_report(self, request):
        """The Desktop launcher says which scale it starts the nested KWin with; the compositor
        reads it from session.json and sends it to that KWin as its preferred scale (T-424)."""
        fields = desktop_scale_fields(request)
        if self.display_applied is None:
            raise ValueError('no session to report on')
        path = self.directory / 'session.json'
        snapshot = json.loads(path.read_text())
        if not isinstance(snapshot, dict) or snapshot.get('instance') != self.instance:
            raise ValueError('no session snapshot for this instance')
        atomic_json(path, {**snapshot, **fields})
        self.desktop_scale = fields
        return b'{"ok":true}'

    def close(self):
        self.listener.close()
        if self.identity is not None:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass


def _control_exchange(endpoint, request, reply_limit, server_uid, timeout):
    """Send one request to a side's control socket and return the raw reply.

    The server must run as DOSPLEX_SESSION_CONTROL_SERVER_UID (root unless told otherwise):
    a process that merely owns the path is never trusted with the request.
    """
    if server_uid is None:
        server_uid = int(os.environ.get("DOSPLEX_SESSION_CONTROL_SERVER_UID", "0"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(timeout)
        connection.connect(endpoint)
        _, uid, _ = struct.unpack('3i', connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != server_uid:
            raise PermissionError('session control server is not trusted')
        connection.sendall(json.dumps(request, separators=(',', ':')).encode())
        return connection.recv(reply_limit)


def client_request(endpoint, instance, request, *, server_uid=None, timeout=3):
    """Send one request to the control socket of a side's supervisor; return its accepted reply.

    `request` has to be one of the protocol's messages for `instance` (validate_control_request):
    a mode change, display get or a display report. Nothing is sent otherwise, and nothing is
    sent to a server that is not the trusted uid. The reply is the supervisor's decoded JSON
    object with "ok": true, which display get extends with "live".

    ValueError for a request that is not valid, PermissionError for an untrusted server, OSError
    for a socket that fails or times out, RuntimeError when the supervisor refuses the request
    or answers with anything but a JSON object.
    """
    if type(instance) is not str or len(instance) != 32 or not Path(endpoint).is_absolute():
        raise ValueError('invalid session control request')
    validate_control_request(request, instance)
    raw = _control_exchange(endpoint, request, 1024, server_uid, timeout)
    try:
        reply = json.loads(raw)
    except ValueError:
        raise RuntimeError('session control reply was malformed') from None
    if not isinstance(reply, dict):
        raise RuntimeError('session control reply was malformed')
    if reply.get('ok') is not True:
        raise RuntimeError('session control request was rejected')
    return reply


def client_mode(mode, endpoint, instance, *, server_uid=None):
    if mode not in ('gaming', 'desktop') or len(instance) != 32 or not Path(endpoint).is_absolute():
        raise ValueError('invalid session mode request')
    client_request(endpoint, instance, {'instance': instance, 'mode': mode}, server_uid=server_uid)


def client_desktop_scale(endpoint, instance, scale, *, server_uid=None):
    """Tell the side's supervisor the Desktop scale the launcher chose, as the text KWin's --scale takes
    ('1.5'). Raises like client_request, ValueError for a scale outside 1 to 3.
    """
    client_request(endpoint, instance, {'instance': instance, 'desktop_scale_milli': desktop_scale_milli(scale)},
                   server_uid=server_uid)


def client_display_get(endpoint, instance, *, server_uid=None):
    """The live display contract the side's supervisor last read: a dict of x, y, width,
    height, game_width, game_height, scale_mode, display_revision and layout.

    Raises like client_request, RuntimeError also while the supervisor knows no contract yet.
    """
    reply = client_request(endpoint, instance, {'instance': instance, 'display': 'get'}, server_uid=server_uid)
    if not isinstance(reply.get('live'), dict):
        raise RuntimeError('session control reply was malformed')
    return reply['live']


def client_display_report(endpoint, instance, revision, game_width, game_height, result, detail='',
                          *, server_uid=None):
    """Tell the side's supervisor what the side did for display `revision`: `result` is
    'applied', 'failed' or 'deferred' (a game runs, so the game monitor stays at game_width x
    game_height), `detail` printable text of at most 200 characters.

    Raises like client_request; the report is validated like the supervisor validates it.
    """
    client_request(endpoint, instance, {'instance': instance, 'display': 'report', 'revision': revision,
                                        'game_width': game_width, 'game_height': game_height,
                                        'result': result, 'detail': detail}, server_uid=server_uid)


def request_change(side, operation, value=None, *, side_root=None):
    if side not in ('left', 'right') or operation not in ('mode', 'restart', 'stop'):
        raise ValueError('invalid side request')
    if operation == 'mode' and value not in ('gaming', 'desktop'):
        raise ValueError('invalid activity')
    directory = (side_state_root() if side_root is None else side_root) / side
    state = json.loads((directory / 'session.json').read_text())
    # A sudo-invoked user may control only their own side. Root/compositor can
    # control both. Do not trust a caller-supplied owner or PID.
    caller_uid = int(os.environ.get('SUDO_UID', '0'))
    if caller_uid and pwd.getpwnam(state['owner']).pw_uid != caller_uid:
        raise PermissionError('this side belongs to another user')
    if operation == 'stop':
        instance = state.get('instance')
        if not isinstance(instance, str) or len(instance) != 32:
            raise ValueError('side has no supervisor instance; recovery is required')
        value = json.dumps({'instance': instance, 'side': side, 'owner': state['owner']})
    fd, temporary = tempfile.mkstemp(prefix='.request-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write((value if operation in ('mode', 'stop') else uuid.uuid4().hex) + '\n')
        os.chmod(temporary, 0o644)
        os.replace(temporary, directory / operation)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'client-mode':
        if len(sys.argv) != 3:
            raise ValueError('client-mode requires one activity')
        client_mode(sys.argv[2], os.environ.get('DOSPLEX_SESSION_CONTROL', ''),
                    os.environ.get('DOSPLEX_SESSION_INSTANCE', ''))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'client-desktop-scale':
        if len(sys.argv) != 3:
            raise ValueError('client-desktop-scale requires one scale')
        client_desktop_scale(os.environ.get('DOSPLEX_SESSION_CONTROL', ''),
                             os.environ.get('DOSPLEX_SESSION_INSTANCE', ''), sys.argv[2])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] in ('request-mode', 'request-restart', 'request-stop'):
        if os.geteuid() != 0:
            raise PermissionError('must run as root')
        operation = sys.argv[1].removeprefix('request-')
        if len(sys.argv) != (4 if operation == 'mode' else 3):
            raise ValueError('invalid request arguments')
        prepare_privileged_state()
        request_change(sys.argv[2], operation, sys.argv[3] if operation == 'mode' else None)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('side', choices=('left', 'right'))
    parser.add_argument('owner')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('must run as root')
    prepare_privileged_state()
    root = Path(os.environ.get('DOSPLEX_ROOT', '/usr/local'))
    stop_event = threading.Event()
    def stop(_signum, _frame):
        stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervise(
        args.side, args.owner, side_state_root() / args.side,
        Path(os.environ.get('DOSPLEX_STATE_PATH', '/run/dosplex/state.json')),
        Path(os.environ.get('DOSPLEX_RUNTIME_DIR', '/run/dosplex')) / os.environ.get('DOSPLEX_WAYLAND_DISPLAY', 'dosplex-0'),
        root, stop_event,
        role=os.environ.get('DOSPLEX_SIDE_ROLE', 'guest' if args.side == 'right' else 'primary'),
        payload=os.environ.get('DOSPLEX_SESSION_PAYLOAD', 'steam'),
        script=os.environ.get('DOSPLEX_REAL_SESSION_SCRIPT') or None,
        launch_token=os.environ.get('DOSPLEX_LAUNCH_TOKEN', ''),
        media_root=MEDIA_ROOT, loopback_root=LOOPBACK_ROOT)


def supervise(side, owner, directory, state_path, host_socket, root, stop_event,
              *, session_factory=Session, role='primary', payload='steam', script=None, launch_token='',
              media_root=None, loopback_root=None):
    if side not in ('left', 'right'):
        raise ValueError('invalid side')
    if not isinstance(launch_token, str) or (launch_token and
            (len(launch_token) != 32 or any(c not in '0123456789abcdef' for c in launch_token))):
        raise ValueError('invalid launch token')
    user = pwd.getpwnam(owner)
    if user.pw_uid < 1000:
        raise ValueError('session owner must be an ordinary user')
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    lock = os.fdopen(os.open(directory / 'supervisor.lock',
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600), 'r+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return 0
    script = script or str(root / 'libexec/dosplex-user-side-session.sh')
    session = None
    restarts = 0
    mode = "gaming"
    instance = uuid.uuid4().hex
    control = None
    last_idle_wine_scan = 0.0
    wine_notice_seq = 0
    wine_notice = ''
    last_wine_log = None
    tour_accounts = []
    # Guest gets its own loopback address for as long as this supervisor runs; Main keeps the host's.
    own_loopback = loopback_root is not None and role == 'guest'
    game_store = bool(os.environ.get('DOSPLEX_GAME_STORE'))
    session_properties = prepare_loopback(side, loopback_root) if own_loopback else []
    last_tour_log = None
    def serve_control_during(work):
        """Return work(), or raise its error, while this thread keeps answering the control socket.

        The supervise loop is the only place that serves the socket, so a slow call in the loop leaves the
        side's display follower without a reply for as long as the call lasts, and the follower gives up on
        a reply after 3 s. The Wine policy is such a call: whenever the game monitor turns between portrait
        and landscape it edits every Proton prefix of the account, each with a /proc scan, a fork and several
        fsyncs, so it runs longer the more prefixes there are.

        `work` runs on a helper thread while this thread serves the socket, so the supervisor's state, the
        socket and session.json are still touched by this thread alone. The policy forks to edit a registry
        as its owner; the child makes a few system calls and exits.
        """
        outcome = []

        def run():
            try:
                outcome.append((True, work()))
            except BaseException as error:   # raised again on the calling thread
                outcome.append((False, error))
        helper = threading.Thread(target=run, name='side-slow-call', daemon=True)
        helper.start()
        while helper.is_alive():
            if control:
                try:
                    control.poll()
                except OSError:
                    pass   # stopping() in the loop meets it again
            helper.join(CONTROL_SERVICE_SECONDS)
        succeeded, value = outcome[0]
        if not succeeded:
            raise value
        return value

    def reconcile_wine_modelist(portrait, announce=False):
        nonlocal wine_notice_seq, wine_notice, last_wine_log
        # The service entry point requires root. Some session lifecycle tests
        # call supervise() directly as an ordinary user with fake sessions;
        # those tests do not exercise registry policy or journal writes.
        if os.geteuid() != 0:
            return None

        def log_once(line):
            # Periodic scans repeat the same outcome; log it only when it changes.
            nonlocal last_wine_log
            if line != last_wine_log:
                print(line, flush=True)
                last_wine_log = line
        try:
            result = serve_control_during(lambda: wine_modelist.reconcile(owner, user.pw_dir, portrait))
        except Exception as error:
            # The policy is a compatibility improvement. Fail closed on its
            # registry edits, but keep Steam available and make the issue
            # visible in this side's systemd journal.
            log_once(f'Dosplex Wine mode-list policy skipped for {side}: {error}')
            return None
        if result.get('error') or result.get('results'):
            log_once('Dosplex Wine mode-list policy: ' + json.dumps({
                'side': side, 'owner': owner, **result}, sort_keys=True))
        if announce and any(value in ('applied', 'recovered-applied')
                            for value in result.get('results', {}).values()):
            wine_notice_seq += 1
            wine_notice = 'A first-run game was updated for Native. Reopen it now.'
            if session and session.started:
                state('running')
        return result

    def check_controller_tour():
        """Note the accounts whose Steam Controller tour is on, once a Steam Controller is routed to this side.

        Only reads (see steam_tour_state). The accounts go to session.json, and the way to switch the tour off to
        the journal when the accounts change; it is looked at again whenever the side's session starts.
        """
        nonlocal tour_accounts, last_tour_log
        try:
            if side in steam_controller_sides(root):
                found = serve_control_during(lambda: steam_tour_state(user.pw_dir, user.pw_uid))
                tour_accounts = found['on']
            else:
                tour_accounts = []
        except Exception as error:
            # A notice is no reason to stop a side.
            say(f'{side}: Steam Controller tour check skipped: {error}')
            return
        if tour_accounts and tour_accounts != last_tour_log:
            say(f'{side}: the Steam Controller tour is on for {owner} account(s) {", ".join(tour_accounts)}. ' + TOUR_NOTICE)
        last_tour_log = tour_accounts or None

    def stopping():
        if stop_event.is_set():
            return True
        if control:
            control.poll()
        try:
            request = json.loads((directory / 'stop').read_text())
        except (OSError, ValueError):
            return False
        return request == {'instance': instance, 'side': side, 'owner': owner}

    def state(lifecycle, active=True):
        atomic_json(directory / 'session.json', {
            'side': side, 'owner': owner, 'pid': os.getpid(), 'instance': instance,
            'active': active, 'lifecycle': lifecycle, 'restart_count': restarts,
            'launch_token': launch_token, 'mode': mode,
            'unit': session.unit if session else '',
            'wine_notice_seq': wine_notice_seq, 'wine_notice': wine_notice,
            'controller_tour_accounts': tour_accounts, 'controller_tour_notice': TOUR_NOTICE if tour_accounts else '',
            # What the side's display follower last reported, while its session exists.
            **(control.display_applied if control and session and control.display_applied else {}),
            # The Desktop launcher's scale, while its session exists (a restart reports it again).
            **(control.desktop_scale if control and session and control.desktop_scale else {}),
        })

    def end_session():
        # The owned service is confirmed stopped: nothing is left to report on.
        nonlocal session
        session = None
        if control:
            control.display_applied = None
            control.desktop_scale = None

    slow = {'in_a_row': 0, 'said_at': None}

    def timed_out(call, error):
        """A systemctl call outlasted its timeout. Say so now and then, and let the caller try again."""
        slow['in_a_row'] += 1
        now = time.monotonic()
        if slow['said_at'] is None or now - slow['said_at'] >= SLOW_CALL_SAY_SECONDS:
            slow['said_at'] = now
            say(f'{side}: {call} timed out after {error.timeout:g} s ({slow["in_a_row"]} in a row); asking again')

    def answered():
        if slow['in_a_row']:
            say(f'{side}: systemd answers again after {slow["in_a_row"]} timeout(s)')
            slow['in_a_row'] = 0
            slow['said_at'] = None

    def running():
        """Whether the owned service runs. A systemctl that is too slow to say tells nothing about the service, so
        the answer is yes for now and the next pass asks again; the loop never ends because systemd was slow."""
        try:
            result = session.is_running()
        except subprocess.TimeoutExpired as error:
            timed_out('systemctl is-active', error)
            return True
        answered()
        return result

    def stop_session():
        """Stop the owned service for good, asking again when systemctl is too slow. The last attempt may raise: a
        stop that is not confirmed keeps the owner and unit in session.json for recovery."""
        for _ in range(STOP_ATTEMPTS - 1):
            try:
                session.stop()
            except subprocess.TimeoutExpired as error:
                timed_out('systemctl stop', error)
                continue
            answered()
            return
        session.stop()
        answered()

    def quit_steam(limit):
        """Before this side's unit is stopped: ask its Steam to quit and wait at most `limit` seconds (TRL-04).

        The ask goes through the power helper's `close --steam-only` (the same wait `dosplex stop` uses). It never
        raises and never keeps the unit from being stopped: a timeout, a missing interpreter or a non-zero exit is
        said once and the stop goes on. The helper runs on a thread while the control socket is answered, and its
        lines land in the supervisor's log (its stdout and stderr are not captured)."""
        if payload != 'steam' or session is None or not getattr(session, 'started', False):
            return
        helper = Path(root) / 'libexec/dosplex-power-action'
        try:
            info = helper.lstat()
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode) or stat.S_IWGRP & info.st_mode or stat.S_IWOTH & info.st_mode or \
                not info.st_uid in (0, os.getuid()):
            return
        argv = [sys.executable, str(helper), 'close', '--steam-only', '--side', side,
                '--side-root', str(directory.parent), '--steam-timeout', str(limit)]

        def run():
            with open(os.devnull, 'rb') as devnull:
                return subprocess.run(argv, stdin=devnull, timeout=limit + 20)

        try:
            code = serve_control_during(run)
        except subprocess.TimeoutExpired:
            say(f'{side}: Steam had not quit after {limit + 20:g} s; stopping the unit anyway')
            return
        except (OSError, subprocess.SubprocessError) as error:
            say(f'{side}: the ask of Steam to quit did not run ({error})')
            return
        if code.returncode not in (None, 0):
            say(f'{side}: the ask of Steam to quit ended with status {code.returncode}; stopping the unit anyway')

    def pause():
        # Bound crash loops and remain responsive to termination.
        for _ in range(8):
            if stopping():
                break
            time.sleep(.25)
    try:
        control = SideControl(directory, user.pw_uid, instance)
        while not stopping():
            try:
                contract = display_state.read_contract(state_path, side)
                mode = requested_mode(directory)
                activity = desired_activity(state_path, side, role)
            except (OSError, ValueError, KeyError, TypeError):
                state('waiting_display', False)
                time.sleep(.25)
                continue
            live = contract['live']
            control.live = dict(live)
            if activity != 'active' or not host_socket.is_socket():
                if time.monotonic() - last_idle_wine_scan >= 5.0:
                    reconcile_wine_modelist(False)
                    last_idle_wine_scan = time.monotonic()
                state('off' if activity == 'off' else 'sleeping', False)
                time.sleep(.25)
                continue
            token = restart_request(directory)
            environment = {
                'HOME': user.pw_dir, 'USER': user.pw_name, 'LOGNAME': user.pw_name,
                'SHELL': user.pw_shell, 'PATH': '/usr/local/bin:/usr/bin:/bin',
                'WAYLAND_DISPLAY': str(host_socket), 'DOSPLEX_HOST_SOCKET': str(host_socket),
                'DOSPLEX_SHIM_SCRIPT': str(root / 'libexec/steamos-session-select-shim.sh'),
                'DOSPLEX_SIDE': side, 'DOSPLEX_OWNER': user.pw_name,
                'DOSPLEX_SIDE_ROOT': str(directory.parent),
                'DOSPLEX_SESSION_CONTROL': str(control.path),
                'DOSPLEX_SESSION_INSTANCE': instance,
                'DOSPLEX_SESSION_CONTROL_CLIENT': str(Path(__file__).resolve()),
                'DOSPLEX_SIDE_ROLE': role,
                **audio_environment(role),
                **desktop_environment(),
                'DOSPLEX_SESSION_PAYLOAD': payload if mode == 'gaming' else 'desktop',
                'DOSPLEX_OUTPUT_WIDTH': str(live['width']), 'DOSPLEX_OUTPUT_HEIGHT': str(live['height']),
                'DOSPLEX_GAME_WIDTH': str(live['game_width']), 'DOSPLEX_GAME_HEIGHT': str(live['game_height']),
                'DOSPLEX_SCALE_MODE': live['scale_mode'],
            }
            # The shared game library, when the store is on: mounted and listed before the side's Steam starts.
            library = prepare_game_library(user.pw_name) if game_store else None
            if library:
                environment['DOSPLEX_GAME_LIBRARY'] = library
            session = session_factory(side, user.pw_name, [script, side, user.pw_name], environment,
                                      **({'properties': session_properties} if session_properties else {}))
            control.display_applied = launch_display_fields(live)
            # Existing prefixes are fixed before the side's Steam client can
            # launch a game. A title played for the first time creates its
            # prefix while Wine is active; periodic reconciliation picks that
            # prefix up only after the game has exited.
            reconcile_wine_modelist(live['game_height'] > live['game_width'])
            check_controller_tour()
            state('starting')
            # Guest's launcher needs the directory to exist when it starts (see prepare_media_root).
            if media_root is not None and role == 'guest':
                prepare_media_root(media_root)
            try:
                session.start()
            except subprocess.TimeoutExpired as error:
                # systemd-run waits for the start job. It may or may not have made the unit; the handle says it
                # started, so the stop below covers both. Then the side is started again.
                timed_out('systemd-run', error)
                state('stopping')
                stop_session()
                end_session()
                restarts += 1
                state('restarting')
                pause()
                continue
            state('running')
            frozen = False
            last_wine_scan = time.monotonic()
            last_portrait = live['game_height'] > live['game_width']
            while not stopping() and running():
                time.sleep(.25)
                try:
                    current = display_state.read_contract(state_path, side)
                    mode_changed = requested_mode(directory) != mode
                    restart_changed = restart_request(directory) != token
                    activity = desired_activity(state_path, side, role)
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # incomplete state never stops a working activity
                control.live = dict(current['live'])
                if activity == 'off':
                    break
                portrait = current['live']['game_height'] > current['live']['game_width']
                if portrait != last_portrait or time.monotonic() - last_wine_scan >= 5.0:
                    reconcile_wine_modelist(portrait, announce=True)
                    last_portrait = portrait
                    last_wine_scan = time.monotonic()
                if activity == 'standby':
                    if not frozen:
                        try:
                            session.set_standby(True)
                        except (subprocess.SubprocessError, RuntimeError):
                            # Keep the owned activity alive when the kernel
                            # rejects freezing; never turn standby into Off.
                            state('standby_failed')
                            time.sleep(1)
                            continue
                        frozen = True
                        state('standby')
                    continue
                if frozen or session.frozen:
                    try:
                        session.set_standby(False)
                    except (subprocess.SubprocessError, RuntimeError):
                        state('standby_failed')
                        time.sleep(1)
                        continue
                    frozen = False
                    state('running')
                # Version-2 state never restarts a side for the display: the follower
                # inside the side applies live changes (ADR-012), or defers one while a
                # game runs, and says which in its report. Only the two contracts read
                # from state.json are compared here, never a report: a deferred revision
                # restarts nothing. Version-1 state keeps restarting on a committed
                # change. A mode change or a restart request restarts under both, and a
                # fresh session starts from the contract read at that moment.
                if display_change_restarts(contract, current) or mode_changed or restart_changed:
                    break
            state('stopping')
            # This is where a side ends for a restart and for a stop that was asked for. A signal asks nobody:
            # systemd is already taking the units down (the session is being stopped or recovered, or the
            # compositor is gone), `dosplex stop` has asked Steam before it got here, and a recovery must not
            # be held up.
            if not stop_event.is_set():
                quit_steam(STOP_STEAM_QUIT_SECONDS if stopping() else RESTART_STEAM_QUIT_SECONDS)
            stop_session()
            end_session()
            # The transient service is confirmed stopped before restoring any
            # prefix values. A still-running stock Steam game causes a safe
            # deferral recorded in the persistent journal.
            reconcile_wine_modelist(False)
            restarts += 1
            state('restarting')
            pause()
    finally:
        try:
            if session:
                # Reached with a session only when the loop ended by an error; the same rule as above.
                if not stop_event.is_set():
                    quit_steam(STOP_STEAM_QUIT_SECONDS)
                stop_session()
                end_session()
            reconcile_wine_modelist(False)
            if own_loopback:
                release_loopback(side, loopback_root)
            if game_store:
                release_game_library(user.pw_name)
            state('stopped', False)
        except Exception:
            # Preserve owner/unit identity for recovery; never advertise an
            # unclaimed side while cleanup is unconfirmed.
            state('cleanup_failed', True)
            raise
        finally:
            try:
                if control:
                    control.close()
            finally:
                lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
