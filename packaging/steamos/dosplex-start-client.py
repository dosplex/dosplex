#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The program Steam's library entry runs: asks the root service to start Dosplex, and tells a person.

The main player presses the entry; this runs as that player, not as root. It connects to the socket the
service listens on, sends the one request the service accepts ("start\n") and prints the service's
message. Exit status 0 means Dosplex is starting, already running, or was already refused by a
deliberate choice; 1 means it is not there, or it said no.

    dosplex-start-client.py [--socket PATH]
"""
import argparse
import json
import shutil
import socket
import subprocess
import sys

DEFAULT_SOCKET = '/run/dosplex-start.sock'
NOT_THERE = ('Dosplex is not installed on this machine, or its start service is not there. '
             'In Desktop mode, run: sudo dosplex repair')
ANSWER_LIMIT = 64 * 1024
ANSWER_TIMEOUT = 100


def read_answer(connection):
    """The whole answer: it is one small object and the service closes the socket when it is done."""
    connection.settimeout(ANSWER_TIMEOUT)
    chunks = []
    size = 0
    while size < ANSWER_LIMIT:
        chunk = connection.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)


def tell(message, window):
    """Say it where a person holding a controller sees it. Steam's Gaming mode shows nothing a program prints,
    so a start that did not happen would look like nothing happened: show a window when one can be shown."""
    print(message)
    if window and shutil.which('kdialog'):
        try:
            subprocess.run(['kdialog', '--title', 'Dosplex', '--error', message], timeout=120, check=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def main():
    parser = argparse.ArgumentParser(description='ask Dosplex\'s start service to start it')
    parser.add_argument('--no-window', action='store_true', help='print only; never show a message window')
    parser.add_argument('--socket', default=DEFAULT_SOCKET)
    arguments = parser.parse_args()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(arguments.socket)
    except OSError:
        tell(NOT_THERE, not arguments.no_window)
        return 1
    with connection:
        try:
            connection.sendall(b'start\n')
            raw = read_answer(connection)
        except OSError:
            raw = b''               # the service went away mid-answer: said below, like an answer that cannot be read
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, ValueError):
        value = None
    if not isinstance(value, dict) or not isinstance(value.get('message'), str):
        tell('Dosplex did not answer with a message. In Desktop mode, run: sudo dosplex repair',
             not arguments.no_window)
        return 1
    started = bool(value.get('ok')) or value.get('code') == 'already-running'
    # "accepted" is the control interface's word for a start that now runs in the background; say it for a person.
    message = 'Dosplex is starting. The screen changes in a moment.' if value.get('code') == 'accepted' else value['message']
    tell(message, not started and not arguments.no_window)
    return 0 if started else 1


if __name__ == '__main__':
    sys.exit(main())
