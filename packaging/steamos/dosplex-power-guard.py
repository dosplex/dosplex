#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Let the main player's Steam restart or shut down the machine, and say so on the screen when it happens.

Inside DosPlex each side's Steam is a system service of its player's account, not a login at the screen. When a player
chooses Restart or Shut down in Steam's own power menu, Steam asks logind, logind asks polkit, and polkit answers that
a password is needed: Steam gets "interactive authentication required", shows nothing, and the button has done nothing
(seen on a Steam Deck, 2026-11-23). A person who cannot find the DosPlex menu then has no way out of DosPlex at all,
and the way back to plain SteamOS is exactly a restart of the machine.

So, while DosPlex runs, the main player's account may restart and shut down the machine: this guard writes one polkit
rule under /run (RULE_DIR; /run is empty after a boot) that answers yes to logind's reboot and power-off for that
account and for nobody else, and removes it when it ends. The second player's Steam is refused as before: one player
must not take the machine from the other. Sleep is left alone.

polkit is told to read its rules again after the rule is written and after it is removed (systemctl reload polkit).
It watches the directories of rules that exist when it starts, and /run/polkit-1/rules.d does not exist after a boot:
seen on the Steam Deck, the rule was in place after the first start following a restart and logind still answered
"challenge" until polkit was reloaded.

It also keeps a logind delay lock on shutdown. When logind announces that the machine goes down
(PrepareForShutdown(true)), whoever asked for it, the guard tells the compositor, which covers both sides with the
panel that says Steam is closing, and asks both sides' Steam to quit (the session's own helper), so that the Steam of
the player who did not ask gets to end by itself. It waits for that as long as logind waits for a delay lock, less a
margin (InhibitDelayMaxSec is 5 seconds by default, read once at start), then lets the machine go down. A guard that
is stuck costs a shutdown those seconds and no more: logind stops waiting by itself.

It runs as root beside the compositor, under a unit that is bound to it. Everything it does goes to the journal.

    dosplex-power-guard.py --root /run/dosplex-trial-ID --main-user NAME [--rule-dir DIR]
"""
import argparse
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import time

LOGIND = 'org.freedesktop.login1'
RULE_DIR = '/run/polkit-1/rules.d'
RULE_NAME = '49-dosplex-power.rules'
# The first line of the rule. A file of that name that does not begin with it is somebody else's and is left alone.
RULE_MARK = '// DosPlex power rule. It is removed when DosPlex stops, and is gone after a restart of the machine.\n'
# What the main player's account may ask logind for. Not the -ignore-inhibit actions: a program that blocks a
# shutdown (an update that must not be cut) still blocks it.
ACTIONS = ('org.freedesktop.login1.reboot', 'org.freedesktop.login1.reboot-multiple-sessions',
           'org.freedesktop.login1.power-off', 'org.freedesktop.login1.power-off-multiple-sessions')
USER_NAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
LOCK_COMMAND = ['systemd-inhibit', '--what=shutdown', '--mode=delay', '--who=DosPlex',
                '--why=Close both sides before the machine goes down', 'sh', '-c', 'echo held; exec cat']
MONITOR_COMMAND = ['gdbus', 'monitor', '--system', '--dest', LOGIND, '--object-path', '/org/freedesktop/login1']
DELAY_MAX_COMMAND = ['gdbus', 'call', '--system', '--dest', LOGIND, '--object-path', '/org/freedesktop/login1',
                     '--method', 'org.freedesktop.DBus.Properties.Get', 'org.freedesktop.login1.Manager',
                     'InhibitDelayMaxUSec']
DELAY_MAX_DEFAULT = 5.0
RELOAD_COMMAND = ['systemctl', 'reload', 'polkit.service']
DELAY_MARGIN = 1.0              # seconds of logind's wait that are left for the release of the lock
# The compositor's control request for the closing panel (CONTROL_CMD_CLOSING): what it is told the machine does.
CONTROL_SOCKET = 'runtime/control.sock'
CLOSING = {'stop': 1, 'reboot': 2, 'poweroff': 3, 'down': 4}
# logind's word for the kind of shutdown (PrepareForShutdownWithMetadata, 'type'), as the panel names it.
# logind's documentation writes 'power-off'; the logind of SteamOS sends 'poweroff' (seen on both devices: the panel
# said "The machine is going down" for a shutdown). Both are read.
KINDS = {'poweroff': 'poweroff', 'power-off': 'poweroff', 'halt': 'poweroff', 'reboot': 'reboot', 'kexec': 'reboot',
         'soft-reboot': 'reboot'}


def log(message):
    print('dosplex-power-guard: ' + message, flush=True)


def rule_text(user):
    """The polkit rule for one account. The name is checked before it is written into the rule's source."""
    if not USER_NAME.fullmatch(user):
        raise ValueError('not an account name: %r' % user)
    tests = ' ||\n            '.join('action.id == "%s"' % action for action in ACTIONS)
    return (RULE_MARK +
            '// While DosPlex runs, the main player may restart or shut down the machine from Steam\'s own power menu.\n'
            'polkit.addRule(function(action, subject) {\n'
            '    if (subject.user == "%s" && (\n            %s)) {\n'
            '        return polkit.Result.YES;\n'
            '    }\n'
            '});\n' % (user, tests))


def is_ours(path):
    """Whether the file at path is a rule this program wrote: a regular file of root's that begins with RULE_MARK."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return False
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            return stream.read(len(RULE_MARK)) == RULE_MARK
    except OSError:
        return False


def write_rule(directory, user):
    """Write the rule; True when it is in place. A file of that name that is not ours is left as it is."""
    directory = Path(directory)
    path = directory / RULE_NAME
    text = rule_text(user)
    try:
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        if directory.is_symlink():
            log('%s is a link; the rule is not written' % directory)
            return False
        if os.path.lexists(path) and not is_ours(path):
            log('%s is already there and is not the one DosPlex writes; it is left alone' % path)
            return False
        temporary = directory / ('.' + RULE_NAME + '.new')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o644)       # polkit runs as its own user and has to read it
            stream.write(text)
        os.replace(temporary, path)
        return True
    except OSError as error:
        log('could not write %s: %s' % (path, error.strerror or error))
        return False


def remove_rule(directory):
    """Remove the rule when it is ours; True when a file was removed."""
    path = Path(directory) / RULE_NAME
    if not is_ours(path):
        return False
    try:
        os.unlink(path)
        return True
    except OSError as error:
        log('could not remove %s: %s' % (path, error.strerror or error))
        return False


def reload_polkit(command=RELOAD_COMMAND, timeout=10.0):
    """Have polkit read its rules again; True when it did. A polkit that is not running reads them when it starts."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        log('could not reload polkit: %s' % error)
        return False
    if done.returncode != 0:
        log('could not reload polkit: %s' % ((done.stderr or '').strip().splitlines() or ['exit %d' % done.returncode])[-1])
    return done.returncode == 0


def parse_event(line):
    """What a line of `gdbus monitor` says about a shutdown: ('down', kind) when logind announces one (kind is
    'reboot', 'poweroff' or 'down' when it does not say), ('called-off', None) when it takes the announcement back,
    or None."""
    found = re.search(r'\.PrepareForShutdownWithMetadata \((true|false),\s*(.*)\)\s*$', line)
    if found:
        if found.group(1) == 'false':
            return ('called-off', None)
        kind = re.search(r"'type':\s*<'([a-z-]+)'>", found.group(2))
        return ('down', KINDS.get(kind.group(1), 'down') if kind else 'down')
    found = re.search(r'\.PrepareForShutdown \((true|false),?\)', line)
    if found:
        return ('down', 'down') if found.group(1) == 'true' else ('called-off', None)
    return None


def read_delay_max(command=DELAY_MAX_COMMAND, timeout=3.0):
    """What logind waits for a delay lock, in seconds; the default when it cannot be read."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        found = re.search(r'uint64 (\d+)', done.stdout)
        if done.returncode == 0 and found:
            return int(found.group(1)) / 1e6
    except (OSError, subprocess.SubprocessError):
        pass
    return DELAY_MAX_DEFAULT


def tell_compositor(root, kind):
    """Ask the compositor for the closing panel. One fixed request on its root-only control socket; True if it took it."""
    path = Path(root) / CONTROL_SOCKET
    try:
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
            sock.settimeout(1.0)
            sock.connect(str(path))
            sock.send(b'DPXC' + bytes([1, 4, CLOSING[kind], 0]))
            reply = sock.recv(64)
        return len(reply) == 8 and reply[:4] == b'DPXR' and reply[5] == 0
    except (OSError, KeyError):
        return False


class Lock:
    """A logind delay lock on shutdown, kept by `systemd-inhibit` around a `cat` that is fed nothing. Closing cat's
    input ends it, and the lock with it; that also happens when this program dies."""

    def __init__(self, command=LOCK_COMMAND):
        self.command, self.process = command, None

    def acquire(self, timeout=3.0):
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            log('could not take the shutdown delay lock: %s' % (error.strerror or error))
            return False
        deadline, said = time.monotonic() + timeout, b''
        while b'held\n' not in said and process.poll() is None and time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                chunk = os.read(process.stdout.fileno(), 64)
                if not chunk:
                    break
                said += chunk
        if b'held\n' in said and process.poll() is None:
            self.process = process
            return True
        self.process = process
        self.release()
        log('could not take the shutdown delay lock')
        return False

    def release(self):
        process, self.process = self.process, None
        if process is None:
            return False
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
        return True


def close_sides(root, seconds):
    """Ask both sides' Steam to quit with the session's own helper, and wait for it at most `seconds`. The helper
    goes on by itself after that; the machine going down ends it."""
    helper = Path(root) / 'bundle/libexec/dosplex-power-action'
    try:
        info = os.lstat(helper)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            log('the close helper is not a file of root\'s; Steam is not asked to quit')
            return
        process = subprocess.Popen([sys.executable, str(helper), 'close', '--trial-root', str(root)],
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        log('could not ask Steam to quit: %s' % (error.strerror or error))
        return
    try:
        process.wait(timeout=max(0.1, seconds))
        log('both sides answered the request to quit')
    except subprocess.TimeoutExpired:
        log('the sides had %.1f s to quit; the machine goes down now' % seconds)


def going_down(root, kind, lock, wait_seconds):
    log('logind announces that the machine goes down (%s)' % {'reboot': 'a restart', 'poweroff': 'a shutdown'}.get(
        kind, 'it does not say how'))
    if tell_compositor(root, kind):
        log('the closing panel is up on both sides')
    if lock.process is not None:
        close_sides(root, wait_seconds)
    else:
        close_sides(root, 0.1)          # no lock: logind does not wait for this guard
    if lock.release():
        log('released the shutdown delay lock')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, help='the session directory, /run/dosplex-trial-ID')
    parser.add_argument('--main-user', required=True, help='the account of the main player')
    parser.add_argument('--rule-dir', default=RULE_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not USER_NAME.fullmatch(args.main_user):
        log('not an account name: %r' % args.main_user)
        return 2

    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    lock = Lock()
    monitor = None
    wrote = False
    down = False        # logind has announced that the machine goes down, and the sides were asked to close
    try:
        wrote = write_rule(args.rule_dir, args.main_user)
        if wrote:
            reload_polkit()
            log('%s may restart and shut down the machine while DosPlex runs (%s)'
                % (args.main_user, Path(args.rule_dir) / RULE_NAME))
        delay_max = read_delay_max()
        wait_seconds = max(0.5, delay_max - DELAY_MARGIN)
        if lock.acquire():
            log('holding a shutdown delay lock; the sides get %.1f s to quit when the machine goes down' % wait_seconds)
        monitor = subprocess.Popen(MONITOR_COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        for line in monitor.stdout:
            event = parse_event(line)
            if event is None:
                continue
            if event[0] == 'down':
                # logind says it twice, with the kind of shutdown and without: the first one is acted on.
                if not down:
                    down = True
                    going_down(args.root, event[1], lock, wait_seconds)
            elif event[0] == 'called-off':
                down = False
                # The machine stays up after all: the panel goes, and the lock is taken again for the next time.
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                        sock.settimeout(1.0)
                        sock.connect(str(Path(args.root) / CONTROL_SOCKET))
                        sock.send(b'DPXC' + bytes([1, 4, 0, 0]))
                        sock.recv(64)
                except OSError:
                    pass
                if lock.process is None and lock.acquire():
                    log('the shutdown was called off; holding the delay lock again')
        log('the monitor of logind ended')
        return 1
    finally:
        if monitor is not None and monitor.poll() is None:
            monitor.terminate()
        lock.release()
        if wrote and remove_rule(args.rule_dir):
            log('removed %s' % (Path(args.rule_dir) / RULE_NAME))
            if not down:
                reload_polkit(timeout=5.0)      # (a machine that goes down has no use for it, and may not answer)


if __name__ == '__main__':
    sys.exit(main())
