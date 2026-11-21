#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Real TCP/Unix-socket bridge tests; no devices, desktop or root required."""
import importlib.util
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bridge', ROOT / 'packaging/steamos/dosplex-primary-input-bridge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TOKEN = b'a' * 64


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-input-test-')
        self.path = str(Path(self.temp.name) / 'compositor.sock')
        self.messages = queue.Queue()
        self.done = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(4)
        self.listener.settimeout(.1)
        def receive():
            while not self.done.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(.1)
                    buffer = b''
                    while not self.done.is_set():
                        try:
                            chunk = conn.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            self.messages.put(line)
        self.receiver = threading.Thread(target=receive)
        self.receiver.start()
        self.bridge = module.Bridge('127.0.0.1', 0, self.path, TOKEN)
        self.errors = []
        def serve():
            try:
                self.bridge.start()
            except Exception as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        deadline = time.monotonic() + 2
        while self.bridge.server_sock is None or self.bridge.server_sock.getsockname()[1] == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.port = self.bridge.server_sock.getsockname()[1]
        self.clients = []

    def connect(self):
        # The port is bound before listen() runs, so retry a refused connection briefly.
        deadline = time.monotonic() + 2
        while True:
            try:
                client = socket.create_connection(('127.0.0.1', self.port), timeout=1)
                break
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.01)
        self.clients.append(client)
        return client

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.bridge.stop()
        self.thread.join(timeout=2)
        self.done.set()
        self.listener.close()
        self.receiver.join(timeout=2)
        self.temp.cleanup()
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.receiver.is_alive())
        self.assertEqual(self.errors, [])

    def test_reject_unauthenticated_and_wrong_token(self):
        client = self.connect()
        client.sendall(b'K 30 1\n')
        self.assertEqual(client.recv(1), b'')
        wrong = self.connect()
        wrong.sendall(b'AUTH ' + b'b' * 64 + b'\nK 30 1\n')
        self.assertEqual(wrong.recv(1), b'')
        self.assertTrue(self.messages.empty())

    def test_fragmented_auth_and_disconnect_releases(self):
        client = self.connect()
        client.sendall(b'AUTH ' + TOKEN[:20])
        time.sleep(.03)
        self.assertTrue(self.messages.empty())
        client.sendall(TOKEN[20:] + b'\nK 30 1\nB 272 1\nM 30 40\n')
        self.assertEqual([self.messages.get(timeout=1) for _ in range(3)], [b'K 30 1', b'B 272 1', b'M 30 40'])
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertEqual({self.messages.get(timeout=1) for _ in range(2)}, {b'K 30 0', b'B 272 0'})

    def test_bounded_partial_command_and_invalid_ranges(self):
        for command in (b'X' * 129, b'K -1 1\n', b'B 272 5\n', b'W 999999 0\n'):
            client = self.connect()
            client.sendall(b'AUTH ' + TOKEN + b'\nK 30 1\n' + command)
            self.assertEqual(self.messages.get(timeout=1), b'K 30 1')
            self.assertEqual(self.messages.get(timeout=1), b'K 30 0')
            self.assertEqual(client.recv(1), b'')
            self.assertLessEqual(len(self.bridge.client_buffer), 128)

    def test_second_connection_does_not_evict_sender(self):
        first = self.connect()
        first.sendall(b'AUTH ' + TOKEN + b'\nK 30 1\n')
        self.assertEqual(self.messages.get(timeout=1), b'K 30 1')
        second = self.connect()
        self.assertEqual(second.recv(1), b'')
        first.sendall(b'K 30 0\n')
        self.assertEqual(self.messages.get(timeout=1), b'K 30 0')

    def test_auth_deadline(self):
        client = self.connect()
        client.sendall(b'AUTH ')
        time.sleep(.05)
        self.bridge.auth_deadline = time.monotonic() - 1
        self.assertEqual(client.recv(1), b'')

    def test_bind_and_secret_permissions(self):
        with self.assertRaises(ValueError):
            module.Bridge('0.0.0.0', 0, self.path, TOKEN)
        token_file = Path(self.temp.name) / 'token'
        token_file.write_bytes(TOKEN)
        token_file.chmod(0o600)
        self.assertEqual(module.read_token(token_file), TOKEN)
        token_file.chmod(0o644)
        with self.assertRaises(ValueError):
            module.read_token(token_file)
        link = Path(self.temp.name) / 'link'
        link.symlink_to(token_file)
        with self.assertRaises(OSError):
            module.read_token(link)


if __name__ == '__main__':
    unittest.main()
