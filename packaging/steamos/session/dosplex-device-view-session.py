#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Private experiment: launch a Steam side with GPU access and an empty input view.

Not a production sandbox. A root-owned optional native Deck plan can expose
HID and uinput exclusively to one side; generated device delivery is separate.
The ordinary user's HOME, session runtime and own removable drives
(/run/media/<user>) remain writable for Steam; the rest of the host is read-only.
Guest cannot reach Main's drives. Where /run/media exists when Guest starts (the
side supervisor makes it first), they are hidden at the mount level; where it
does not, only the mode of Main's folder keeps Guest out. Their mount lines
stay in Guest's mountinfo either way.
"""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import subprocess
import socket
import time


def wait_audio_socket(path, timeout=15):
    """An enrolled Unix endpoint must accept connections before Steam starts.

    A stale socket file is insufficient. Pressure-vessel drops the TCP fallback,
    so launching early can leave Proton games with no audio device.
    """
    if not path.parent.is_dir():
        return None  # This older trial has not enrolled the audio experiment.
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(.2)
            try:
                connection.connect(str(path))
                return 'unix:' + str(path)
            except (OSError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Enrolled DosPlex audio socket is not ready')
        time.sleep(.05)


def owner_audio_server(uid=None, owner=None, base=Path('/run/user'), timeout=1):
    """The shared server's own Unix socket, for the session of the server's owner.

    The socket gives the normal shared-memory transport, unlike the TCP relay.
    Returns None when this user is not the owner or the socket does not answer.
    """
    uid = os.getuid() if uid is None else uid
    owner = owner or os.environ.get('DOSPLEX_AUDIO_SERVER_USER', 'deck')
    try:
        owner_uid = pwd.getpwnam(owner).pw_uid
    except KeyError:
        return None
    if uid != owner_uid:
        return None
    try:
        return wait_audio_socket(Path(base) / str(owner_uid) / 'pulse/native', timeout)
    except RuntimeError:
        return None


def wait_guest_audio(directory, router, side, timeout=60):
    """Guest's only audio route is its own restricted endpoint.

    The router's check performs a real Pulse handshake and then insists that
    the endpoint offers exactly Guest's output and microphone and refuses
    module loading, so a Guest launched against the shared server, or against
    nothing, fails here instead of listing Main's devices. A trial that never
    enrolled the endpoint returns None: Guest then has no audio, never the
    shared server. The first launch can come before the audio router has
    started the endpoint, so the wait is longer than for Main's forwarder.
    """
    if not directory.is_dir():
        return None
    path = directory / 'pulse' / 'native'
    problems = ['the endpoint socket is missing']
    deadline = time.monotonic() + timeout
    while True:
        if path.is_socket():
            try:
                result = subprocess.run(
                    [sys.executable, str(router), 'guest-endpoint-check', '--server', 'unix:' + str(path), '--side', side],
                    capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                # A stall of the machine (a suspend that did not complete) or a busy endpoint: ask again until the deadline.
                problems = ['the check did not finish in 30 s']
            else:
                if result.returncode == 0:
                    return 'unix:' + str(path)
                problems = [line.removeprefix('dosplex audio: ') for line in result.stderr.splitlines() if line] or ['the check failed']
        if time.monotonic() >= deadline:
            raise RuntimeError('Guest audio endpoint is not ready: ' + '; '.join(problems))
        time.sleep(.25)


# Guest must not change the shared machine: SteamOS/BIOS updates and the system
# timezone belong to Main (or normal SteamOS). Steam's first-run setup still calls
# these helpers, so Guest's sandbox gets honest stand-ins instead of pkexec, which
# cannot work inside the user namespace ("pkexec must be setuid root", exit 127).
GUEST_SYSTEM_HELPERS = {
    'steamos-update': (
        'echo "No update available: SteamOS updates are managed by Main outside DosPlex." >&2\n'
        'exit 7  # steamos-update: no update available\n'),
    'steamos-set-timezone': (
        'echo "Timezone is shared by both players and managed by Main; keeping the system timezone." >&2\n'
        'exit 0\n'),
}
HOST_HELPERS = Path('/usr/bin/steamos-polkit-helpers')


def guest_system_helper_binds(runtime, host_helpers=HOST_HELPERS):
    """Write Guest's stand-in helpers under its runtime directory; return bwrap bind arguments."""
    directory = runtime / 'dosplex-guest-system-helpers'
    directory.mkdir(mode=0o700, exist_ok=True)
    binds = []
    for name, body in GUEST_SYSTEM_HELPERS.items():
        target = host_helpers / name
        if not target.is_file():
            continue  # Nothing to replace on hosts without this helper.
        # SteamOS 3.9 makes these helpers symlinks into /usr/bin/holo-polkit-helpers, and bwrap
        # cannot mount on a symlink: bind over the file it points to, which the link then reaches.
        target = target.resolve(strict=True)
        shim = directory / name
        # Replace atomically: the previous read-only copy from an earlier start (for
        # example after Reset) must not block a restart.
        temporary = directory / (name + '.new')
        temporary.unlink(missing_ok=True)
        temporary.write_text('#!/bin/sh\n# DosPlex Guest stand-in for ' + str(target) + '\n' + body)
        temporary.chmod(0o500)
        os.replace(temporary, shim)
        binds += ['--ro-bind', str(shim), str(target)]
    return binds


MEDIA_ROOT = Path('/run/media')


def own_media_binds(user, uid, root=MEDIA_ROOT, root_uid=0):
    """The bwrap arguments that give a side its owner's removable drives back read-write, or [].

    udisks mounts a user's drives (the SD card and USB drives of SteamOS's automount) under /run/media/<user>, a
    folder that root owns and only that user may enter. --ro-bind / / makes the drives that are mounted when the
    side starts read-only. Binding that folder over them restores their host flags, and drives mounted later
    arrive with the host's flags anyway, through /run. Only the owner's own folder is bound and bwrap runs as the
    owner, so nothing becomes writable that the owner could not already change on the host. A folder that is
    not the one udisks makes is left alone and said so, and the side starts either way. No folder (no drive
    since boot) means no bind.
    """
    if not user or '/' in user or user in ('.', '..'):
        return []
    folder = root / user
    try:
        base, own = root.lstat(), folder.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        print(f'DosPlex: {folder} stays read-only in this side: {error}', file=sys.stderr, flush=True)
        return []
    shared = stat.S_IWGRP | stat.S_IWOTH  # write for group or other
    usual = (stat.S_ISDIR(base.st_mode) and base.st_uid == root_uid and not base.st_mode & shared and
             stat.S_ISDIR(own.st_mode) and own.st_uid in (root_uid, uid) and not own.st_mode & shared)
    if not usual or not os.access(folder, os.R_OK | os.X_OK):
        print(f'DosPlex: {folder} stays read-only in this side: not a folder that only root or its user may change, '
              'or its user cannot enter it', file=sys.stderr, flush=True)
        return []
    # --bind-try: a folder that goes away before bwrap binds it must not stop the side from starting.
    return ['--bind-try', str(folder), str(folder)]


def media_view(role, user, uid, root=MEDIA_ROOT, root_uid=0):
    """The drive arguments of one side: its own folder (own_media_binds), and for Guest an empty /run/media below it.

    Main's drives are mounted below /run/media/<Main's user>. Like every mount they are in Guest's view, and only
    the mode of that folder keeps Guest out of them. A tmpfs over /run/media takes them out of Guest's directory
    tree, whatever that mode is and whenever a drive is mounted; their mount lines (path and device) stay in its
    /proc/self/mountinfo. The folder bound after the tmpfs is Guest's own, if it exists when Guest's side starts.
    udisks makes it for Guest's first drive since boot, and a folder made after the side started lies under the
    tmpfs: it and its drives stay hidden until Guest's side restarts. The tmpfs belongs to Guest's uid, so it is
    remounted read-only once that folder is bound, or Guest could make files and folders in it, a new writable
    place in the host's view. The remount does not reach the folder, which stays writable, and a drive that is
    mounted in it later. bwrap cannot make the mount point on the read-only root, and then the side would not
    start, so where /run/media does not exist when the side starts nothing is hidden and the mode of the folder is
    the only barrier, as it was. The side supervisor makes /run/media before it starts Guest (prepare_media_root in
    dosplex-side-supervisor.py); only a launcher started any other way meets that case.
    """
    binds = own_media_binds(user, uid, root, root_uid)
    if role != 'guest':
        return binds
    try:
        exists = stat.S_ISDIR(root.lstat().st_mode)
    except OSError:
        exists = False
    if not exists:
        return binds
    # The remount comes after the bind: the folder needs the writable tmpfs to take its mount point.
    return ['--tmpfs', str(root)] + binds + ['--remount-ro', str(root)]


def game_library_binds(path=None):
    """The bwrap arguments that keep the account's shared game libraries writable in the side, or [].

    The shared store is an overlay mounted on the steamapps folder of the account's own library (dosplex-game-store.py;
    the supervisor mounts them and names the mount points, separated by colons, in DOSPLEX_GAME_LIBRARY: one for the
    primary store and one for every other disk that has one). --ro-bind / / makes every mount that is there when the side
    starts read-only, and Steam must write to its library: installs, updates and its own manifests land in the account's
    real folder below the overlay. Binding the mount point over itself restores the flags it has on the host. Only a
    folder that is a mount point is bound, so a library that is not merged with the store stays as it is.
    """
    path = path if path is not None else os.environ.get('DOSPLEX_GAME_LIBRARY', '')
    binds = []
    for item in path.split(':'):
        if item and os.path.isabs(item) and os.path.ismount(item):
            binds += ['--bind-try', item, item]
    return binds


def sandbox_command(home, runtime, extra, inner):
    """The bwrap command that runs inner as the side: the host read-only, a private /dev, /proc and /tmp, GPU
    access, and writable HOME, session runtime and extra binds."""
    return ['bwrap', '--unshare-user', '--unshare-pid', '--unshare-ipc', '--die-with-parent',
            '--new-session', '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc',
            '--tmpfs', '/tmp', '--dir', '/dev/input', '--dev-bind', '/dev/dri', '/dev/dri',
            '--bind', str(home), str(home), '--bind', str(runtime), str(runtime),
            *extra, '--cap-drop', 'ALL', '--setenv', 'DOSPLEX_INSIDE_DEVICE_VIEW', '1', '--', *inner]


def select_audio(side, role):
    """Point the session at its audio endpoint."""
    here = Path(__file__).resolve()
    root = here.parents[2]
    if role == 'guest':
        # Guest is never handed the shared server, whatever the caller exported.
        os.environ.pop('DOSPLEX_AUDIO_SERVER', None)
        server = wait_guest_audio(root / 'audio-guest', here.with_name('dosplex-audio-router.py'), side)
        if server:
            os.environ['DOSPLEX_GUEST_AUDIO_SERVER'] = server
        else:
            os.environ.pop('DOSPLEX_GUEST_AUDIO_SERVER', None)
        return
    # Steam pressure-vessel forwards a Unix Pulse socket into its container.
    # A TCP-only server is discarded, leaving games without an audio endpoint.
    # The server owner's own socket keeps shared memory; the relay is the fallback.
    server = owner_audio_server()
    if not server:
        server = wait_audio_socket(root / 'audio/native')
    if server:
        os.environ['DOSPLEX_AUDIO_SERVER'] = server
        print('dosplex audio: Main endpoint ' + server, file=sys.stderr)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('left', 'right') or os.geteuid() == 0:
        raise SystemExit('requires an ordinary side user, side and owner')
    home = Path(os.environ['HOME'])
    runtime = Path(os.environ['XDG_RUNTIME_DIR'])
    for directory in (home, runtime):
        info = directory.lstat()
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir() or info.st_uid != os.getuid():
            raise RuntimeError('expected owned home and runtime')
    real = Path(__file__).with_name('dosplex-user-side-session.sh')

    if os.environ.pop('DOSPLEX_INSIDE_DEVICE_VIEW', None) == '1':
        # Namespace identity is diagnostic, not an authorization token.
        (runtime / 'device-view.json').write_text(json.dumps({
            'mount': os.readlink('/proc/self/ns/mnt'), 'pid': os.readlink('/proc/self/ns/pid'),
            'devices': os.listdir('/dev/input'), 'uid': os.getuid(),
        }) + '\n')
        os.execv(real, [str(real), *sys.argv[1:]])
    # Chosen once, outside the sandbox; the sandboxed copy inherits the result.
    select_audio(sys.argv[1], os.environ.get('DOSPLEX_SIDE_ROLE'))
    # A root UID is unmapped inside bwrap's user namespace. Keep root-peer
    # validation outside, then authenticate the same-owner proxy inside.
    proxy_path = runtime / 'side-control-proxy.sock'
    proxy = subprocess.Popen([sys.executable, str(Path(__file__).with_name('session-control-proxy.py'))])
    deadline = time.monotonic() + 3
    while not proxy_path.exists():
        if proxy.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('session control proxy failed')
        time.sleep(.02)
    os.environ['DOSPLEX_SESSION_CONTROL'] = str(proxy_path)
    os.environ['DOSPLEX_SESSION_CONTROL_SERVER_UID'] = str(os.getuid())
    # The folder is named after the uid, never after USER or another variable. This stays above the native
    # binds on purpose: retained-synthetic-session.py rewrites the lines from them up to the command.
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = ''  # no account entry for this uid, so no folder to name
    media = media_view(os.environ.get('DOSPLEX_SIDE_ROLE'), user, os.getuid()) + game_library_binds()
    spec = importlib.util.spec_from_file_location('native_plan', Path(__file__).with_name('native-deck-plan.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    native = module.bindings(Path(__file__).with_name('native-deck-plan.json'), sys.argv[1])
    native += module.bindings(Path(__file__).with_name('native-controller-plan.json'), sys.argv[1])
    # External-only trials grant uinput independently of any built-in Deck HID.
    # Controller HIDs are delivered by the native router after relay startup.
    native += module.bindings(Path(__file__).with_name('native-'+sys.argv[1]+'-uinput-plan.json'), sys.argv[1])
    if os.environ.get('DOSPLEX_SIDE_ROLE') == 'guest':
        native += guest_system_helper_binds(runtime)
    command = sandbox_command(home, runtime, media + native,
                              [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
