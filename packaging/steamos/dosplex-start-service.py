#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The socket service behind the Steam library entry: the one place a controller can start Dosplex.

Steam's Gaming mode has no terminal and no password prompt, so the library entry runs the start client
as the main player, and the client talks to this service over a Unix socket. systemd starts the service
for each connection (socket activation, Accept=no) and hands over the listening socket as descriptor 3.
The service trusts exactly one account, named in its config file: anything else, anything but the exact
request "start\n", is refused before a command runs. The command itself is fixed:

    <program from the config> control start

Nothing the client sent is ever passed to it; it runs with no shell, stdin closed and an environment of
its own (PATH only). Its stdout comes back over the socket unchanged when it is one JSON object with the
control interface's keys; everything else is answered with the failure.

    dosplex-start-service.py [--config PATH] [--idle SECONDS] [--listen-fd N] [--timeout SECONDS]
"""
import argparse
import json
import os
import select
import socket
import stat
import struct
import subprocess
import sys
import time

DEFAULT_CONFIG = '/var/lib/dosplex/start-service.json'
DEFAULT_IDLE = 30
DEFAULT_TIMEOUT = 90
REQUEST_LIMIT = 64
READ_TIMEOUT = 5
ENVIRON = {'PATH': '/usr/bin:/bin'}


def fail(message):
    print('dosplex-start-service: ' + message, file=sys.stderr, flush=True)


def trusted(path, expect):
    """Why this file is safe for root to read or run, or None when it is.

    Both the config and the command to run must be regular files that root owns and that neither the
    group nor others can write: whoever can write the config chooses the account that may start Dosplex,
    and whoever can write the program chooses what root runs. A link is never trusted: it could be
    swapped while the check and the use are apart. expect names the kind of file for the caller.
    """
    try:
        info = os.lstat(path)
    except OSError as error:
        return '%s cannot be read: %s' % (path, error.strerror)
    if stat.S_ISLNK(info.st_mode):
        return '%s is a link' % path
    if not stat.S_ISREG(info.st_mode):
        return '%s is not a regular file' % path
    if info.st_uid != 0:
        return '%s is not owned by root' % path
    if info.st_mode & 0o022:
        return '%s is writable by group or others' % path
    return None


def peer_uid(connection):
    """The uid of the connected peer, from the kernel, not from anything it said."""
    _, uid, _ = struct.unpack('3i', connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    return uid


def peer_uid_safe(connection):
    """The peer's uid for the log line; -1 when the kernel no longer says (the socket is closed)."""
    try:
        return peer_uid(connection)
    except OSError:
        return -1


def answer(code, message, data=None, ok=False):
    return json.dumps({'api': 1, 'ok': ok, 'code': code, 'message': message,
                       'data': data if data is not None else {}})


def refusal(message):
    return answer('refused', message)


def bad_request():
    return answer('bad-request', 'This service only starts Dosplex.')


def failure(message):
    return answer('failed', message)


def is_control_answer(raw):
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    keys = {'api', 'ok', 'code', 'message'}
    if any(key not in value for key in keys) or keys - set(value):
        return False
    return isinstance(value['api'], int) and isinstance(value['ok'], bool) and \
        isinstance(value['code'], str) and isinstance(value['message'], str)


class Service:
    def __init__(self, config, idle, timeout):
        self.config = config
        self.uid = None
        self.program = None
        self.idle = idle
        self.timeout = timeout

    def check_config(self):
        problem = trusted(self.config, 'file')
        if problem:
            fail('config %s' % problem)
            return False
        try:
            with open(self.config) as handle:
                value = json.loads(handle.read())
        except (OSError, ValueError) as error:
            fail('config %s is not a JSON object: %s' % (self.config, error))
            return False
        if not isinstance(value, dict) or not isinstance(value.get('uid'), int) or \
                not isinstance(value.get('program'), str):
            fail('config needs an integer "uid" and a string "program"')
            return False
        if isinstance(value['uid'], bool) or value['uid'] < 1000:
            # Root and the system accounts have other ways; the service is for one player's account.
            fail('config "uid" must be an ordinary account (1000 or above)')
            return False
        if not os.path.isabs(value['program']):
            fail('config "program" must be an absolute path')
            return False
        self.uid = value['uid']
        self.program = value['program']
        return True

    def run_program(self):
        """Run the fixed start command and return the bytes to send back, unchanged or as a failure."""
        problem = trusted(self.program, 'program')
        if problem:
            fail('program %s' % problem)
            return failure('The start command cannot be run.').encode()
        try:
            done = subprocess.run([str(self.program), 'control', 'start'], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                  env=ENVIRON, timeout=self.timeout, check=False)
        except (OSError, ValueError):
            return failure('The start command could not be run.').encode()
        except subprocess.TimeoutExpired:
            return failure('The start command did not answer in time.').encode()
        if done.returncode != 0 and not done.stdout:
            return failure('The start command could not be run.').encode()
        if is_control_answer(done.stdout):
            return done.stdout
        return failure('The start command\'s answer could not be read.').encode()

    def connection(self, connection):
        """One connection, end to end. Returns the answer's code for the log line."""
        uid = peer_uid(connection)
        # Read what was sent before answering, also when the answer is a refusal: closing a socket that still holds
        # unread bytes resets the connection, and the client would see an error instead of the reason.
        connection.settimeout(READ_TIMEOUT if uid == self.uid else 1)
        try:
            raw = connection.recv(REQUEST_LIMIT)
        except socket.timeout:
            raw = b''
        if uid != self.uid:
            connection.sendall(refusal('Only the main player\'s account can start Dosplex.').encode())
            connection.shutdown(socket.SHUT_WR)
            return 'refused'
        if raw != b'start\n':
            connection.sendall(bad_request().encode())
            connection.shutdown(socket.SHUT_WR)
            return 'bad-request'
        text = self.run_program()
        connection.sendall(text)
        connection.shutdown(socket.SHUT_WR)
        return json.loads(text.decode())['code']

    def serve(self, listener):
        """Wait for connections until the idle time passes with none in flight, then exit 0."""
        deadline = time.monotonic() + self.idle
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                print('dosplex-start-service: idle, closing for systemd', file=sys.stderr, flush=True)
                return 0
            ready, _, _ = select.select([listener], [], [], remain)
            if not ready:
                continue
            connection, _ = listener.accept()
            with connection:
                uid = peer_uid_safe(connection)        # before the connection closes, or the kernel says nothing
                try:
                    code = self.connection(connection)
                except OSError as error:
                    # A client that went away mid-answer must not end the service: systemd would count that
                    # as a failure of the unit, and enough of them close the socket for everybody.
                    code = 'dropped (%s)' % error.strerror
            print('dosplex-start-service: peer uid %d, answered %s' % (uid, code), file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description='answer the Steam library entry\'s start request, as root')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--idle', type=float, default=DEFAULT_IDLE)
    parser.add_argument('--listen-fd', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    arguments = parser.parse_args()
    if os.environ.get('LISTEN_PID') and int(os.environ['LISTEN_PID']) != os.getpid():
        fail('not the process systemd gave the socket to')
        return 78
    if arguments.listen_fd == 3 and os.environ.get('LISTEN_FDS') != '1':
        fail('systemd handed over no listening socket (this program is started by dosplex-start.socket)')
        return 78
    listener = socket.fromfd(arguments.listen_fd, socket.AF_UNIX, socket.SOCK_STREAM)
    service = Service(arguments.config, arguments.idle, arguments.timeout)
    if not service.check_config():
        return 78
    try:
        return service.serve(listener)
    finally:
        listener.close()


if __name__ == '__main__':
    sys.exit(main())
