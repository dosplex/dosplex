#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Keep root-peer validation outside the experiment's user namespace.

One fixed side endpoint and instance; never accepts caller-selected destinations.
The supervisor still authenticates the real side UID on every forwarded call.

It forwards the supervisor's three control requests and nothing else: a mode change, display get
and display report (validate_control_request in the supervisor module, which the supervisor applies
again). The supervisor's reply goes back as it was sent, so display get returns the live contract.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct


def main():
    endpoint=os.environ['DOSPLEX_SESSION_CONTROL'];instance=os.environ['DOSPLEX_SESSION_INSTANCE']
    runtime=Path(os.environ['XDG_RUNTIME_DIR'])
    spec=importlib.util.spec_from_file_location('supervisor',os.environ['DOSPLEX_SESSION_CONTROL_CLIENT'])
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    path=runtime/'side-control-proxy.sock'
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as server:
        server.bind(str(path));path.chmod(0o600);server.listen(4)
        while True:
            connection,_=server.accept()
            with connection:
                connection.settimeout(.2)
                try:
                    _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid!=os.getuid():continue
                    raw,_,flags,_=connection.recvmsg(1024)
                    if flags&socket.MSG_TRUNC:raise ValueError('oversized request')
                    request=json.loads(raw)
                    reply=module.client_request(endpoint,instance,request)
                    connection.sendall(json.dumps(reply,separators=(',',':')).encode())
                except (OSError,ValueError,RuntimeError):
                    try:connection.sendall(b'{"ok":false}')
                    except OSError:pass

if __name__=='__main__':main()
