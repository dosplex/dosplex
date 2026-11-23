#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import argparse
import hmac
import ipaddress
import os
import stat
import time
import logging
import selectors
import signal
import socket
import sys
from typing import Optional


LOG = logging.getLogger("dosplex-primary-input-bridge")
VALID_PREFIXES = {"M", "B", "K", "W"}


class Bridge:
    def __init__(self, listen_host: str, listen_port: int, unix_socket_path: str, token: bytes) -> None:
        if not ipaddress.ip_address(listen_host).is_loopback:
            raise ValueError("Remote input must listen on loopback; use an authenticated SSH tunnel")
        if len(token) != 64 or any(c not in b"0123456789abcdefABCDEF" for c in token):
            raise ValueError("Input token must be 32 random bytes encoded as 64 hex characters")
        self.token = token
        self.authenticated = False
        self.auth_deadline = 0.0
        self.pressed_keys = set()
        self.pressed_buttons = set()
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.unix_socket_path = unix_socket_path
        self.selector = selectors.DefaultSelector()
        self.server_sock: Optional[socket.socket] = None
        self.client_sock: Optional[socket.socket] = None
        self.client_addr: str = ""
        self.client_buffer = b""
        self.unix_sock: Optional[socket.socket] = None
        self.running = True
        self.cmd_count = 0

    def start(self) -> None:
        family = socket.AF_INET6 if ":" in self.listen_host else socket.AF_INET
        self.server_sock = socket.socket(family, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.listen_host, self.listen_port))
        self.server_sock.listen(4)
        self.server_sock.setblocking(False)
        self.selector.register(self.server_sock, selectors.EVENT_READ, self._accept_client)
        LOG.info("Listening on %s:%d", self.listen_host, self.listen_port)

        try:
            while self.running:
                if self.client_sock is not None and not self.authenticated and time.monotonic() > self.auth_deadline:
                    self._drop_client()
                for key, _ in self.selector.select(timeout=0.1):
                    if not self.running:
                        break
                    callback = key.data
                    callback(key.fileobj)
        finally:
            self._close()

    def stop(self) -> None:
        # Signals and test threads only request shutdown. The event-loop owner
        # closes sockets so a read/disconnect cannot race with their cleanup.
        self.running = False

    def _close(self) -> None:
        self._drop_client()
        if self.server_sock is not None:
            try:
                self.selector.unregister(self.server_sock)
            except Exception:
                pass
            self.server_sock.close()
            self.server_sock = None
        if self.unix_sock is not None:
            self.unix_sock.close()
            self.unix_sock = None

        self.selector.close()

    def _accept_client(self, server_sock: socket.socket) -> None:
        conn, addr = server_sock.accept()
        conn.setblocking(False)
        LOG.info("Accepted client from %s:%d", addr[0], addr[1])
        if self.client_sock is not None:
            conn.close()  # A second connection cannot evict an active sender.
            return
        self.authenticated = False
        self.auth_deadline = time.monotonic() + 3
        self.client_sock = conn
        self.client_addr = f"{addr[0]}:{addr[1]}"
        self.client_buffer = b""
        self.selector.register(conn, selectors.EVENT_READ, self._read_client)
        # Try an immediate non-blocking read in case data already arrived.
        # This handles the fast connect-send-disconnect pattern used by
        # one-shot test tools where the FIN may arrive before select()
        # fires the read callback for the newly registered socket.
        try:
            chunk = conn.recv(4096)
            if chunk:
                self._process_chunk(chunk)
            if not chunk:
                LOG.info("TCP client %s disconnected immediately", self.client_addr)
                self._drop_client()
        except BlockingIOError:
            pass  # No data yet, normal path
        except OSError as exc:
            LOG.warning("TCP early read failed: %s", exc)
            self._drop_client()

    def _drop_client(self) -> None:
        if self.client_sock is None:
            return
        for code in sorted(self.pressed_keys):
            self._send_to_unix(f"K {code} 0".encode())
        for code in sorted(self.pressed_buttons):
            self._send_to_unix(f"B {code} 0".encode())
        self.pressed_keys.clear()
        self.pressed_buttons.clear()
        self.authenticated = False
        if self.unix_sock is not None:
            self.unix_sock.close()
            self.unix_sock = None
        try:
            self.selector.unregister(self.client_sock)
        except Exception:
            pass
        conn = self.client_sock
        self.client_sock = None
        self.client_addr = ""
        self.client_buffer = b""
        conn.close()

    def _connect_unix(self) -> bool:
        if self.unix_sock is not None:
            return True

        unix_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_sock.settimeout(.2)
        try:
            unix_sock.connect(self.unix_socket_path)
        except OSError as exc:
            unix_sock.close()
            LOG.warning("Cannot connect to compositor input socket %s: %s",
                        self.unix_socket_path, exc)
            return False

        self.unix_sock = unix_sock
        LOG.info("Connected to compositor input socket %s", self.unix_socket_path)
        return True

    def _send_to_unix(self, line: bytes) -> bool:
        if not self._connect_unix():
            return False

        try:
            assert self.unix_sock is not None
            self.unix_sock.sendall(line + b"\n")
            return True
        except OSError as exc:
            LOG.warning("Lost compositor input socket connection: %s", exc)
            if self.unix_sock is not None:
                self.unix_sock.close()
                self.unix_sock = None
            return False

    def _process_chunk(self, chunk: bytes) -> None:
        self.client_buffer += chunk
        while b"\n" in self.client_buffer:
            raw_line, self.client_buffer = self.client_buffer.split(b"\n", 1)
            if len(raw_line) > 128:
                self._drop_client()
                return
            line = raw_line.strip()
            if not self.authenticated:
                if not hmac.compare_digest(line, b"AUTH " + self.token):
                    self._drop_client()
                    return
                self.authenticated = True
                continue
            if not self._valid_command(line):
                LOG.warning("Rejected malformed input command")
                self._drop_client()
                return
            if not self._send_to_unix(line):
                self._drop_client()
                return
            kind, code, value = line.split()
            if kind in (b"K", b"B"):
                held = self.pressed_keys if kind == b"K" else self.pressed_buttons
                if int(value):
                    held.add(int(code))
                else:
                    held.discard(int(code))
            self.cmd_count += 1
        if len(self.client_buffer) > 128:
            self._drop_client()

    def _read_client(self, conn: socket.socket) -> None:
        try:
            chunk = conn.recv(4096)
        except BlockingIOError:
            return
        except OSError as exc:
            LOG.warning("TCP read failed: %s", exc)
            self._drop_client()
            return

        if not chunk:
            LOG.info("TCP client %s disconnected", self.client_addr)
            self._drop_client()
            return

        self._process_chunk(chunk)

    @staticmethod
    def _valid_command(line: bytes) -> bool:
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            return False

        parts = text.split()
        if len(parts) != 3 or parts[0] not in VALID_PREFIXES:
            return False

        try:
            a, b = int(parts[1]), int(parts[2])
        except ValueError:
            return False
        if parts[0] == "K":
            return 0 <= a <= 767 and b in (0, 1)
        if parts[0] == "B":
            return 272 <= a <= 279 and b in (0, 1)
        if parts[0] == "M":
            return -131072 <= a <= 131072 and -131072 <= b <= 131072
        return -1200 <= a <= 1200 and -1200 <= b <= 1200


def read_token(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Input token file must be private and owned by the service user")
        token = os.read(fd, 130).strip()
        if len(token) != 64 or any(c not in b"0123456789abcdefABCDEF" for c in token):
            raise ValueError("Invalid input token format")
        return token
    finally:
        os.close(fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge TCP remote input commands into the DosPlex primary input socket."
    )
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=24802)
    parser.add_argument("--socket", default="/run/dosplex/dosplex-primary-input.sock")
    parser.add_argument("--token-file", default="/etc/dosplex/remote-input.token")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[dosplex-primary-input-bridge] %(message)s",
    )

    bridge = Bridge(args.listen_host, args.listen_port, args.socket, read_token(args.token_file))

    def handle_signal(signum, _frame) -> None:
        LOG.info("Stopping on signal %s", signum)
        bridge.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        bridge.start()
    except KeyboardInterrupt:
        bridge.stop()
    except Exception as exc:
        LOG.exception("Bridge failed: %s", exc)
        bridge.stop()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
