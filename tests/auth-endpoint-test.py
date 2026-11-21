#!/usr/bin/env python3
# dosplex-test: tier=unit needs=acl
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Reject missing/wrong display endpoints before ACL changes or claim publication."""
import contextlib
import importlib.util
import io
import json
import os
import pwd
import subprocess
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class EndpointTests(unittest.TestCase):
    def test_invalid_endpoints_do_not_grant_or_publish(self):
        for kind in ('missing-runtime', 'missing-socket', 'regular-file', 'directory',
                     'socket-symlink', 'runtime-symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='dosplex-endpoint-') as directory:
                root = Path(directory)
                runtime = root / 'runtime'
                runtime.mkdir()
                endpoint = runtime / 'dosplex-0'
                with socket.socket(socket.AF_UNIX) as server:
                    if kind == 'missing-runtime':
                        runtime.rmdir()
                    elif kind == 'regular-file':
                        endpoint.write_text('not a socket')
                    elif kind == 'directory':
                        endpoint.mkdir()
                    elif kind == 'socket-symlink':
                        server.bind(str(runtime / 'real'))
                        endpoint.symlink_to('real')
                    elif kind == 'runtime-symlink':
                        server.bind(str(endpoint))
                        (root / 'alias').symlink_to(runtime, target_is_directory=True)
                        runtime = root / 'alias'
                    user = SimpleNamespace(pw_name='player', pw_uid=1001, pw_shell='/bin/bash', pw_dir='/fixtures/player')
                    with patch.object(auth, 'SIDE_ROOT', root / 'state'), \
                         patch.object(auth, 'SOCKET_RUNTIME_DIR', runtime), \
                         patch.object(auth, 'SOCKET_NAME', 'dosplex-0'), \
                         patch.object(auth.pwd, 'getpwnam', return_value=user), \
                         patch.object(auth, 'authenticate', return_value=True), \
                         patch.object(auth.subprocess, 'run') as run:
                        for operation in (lambda: auth.login_side('left', 'player', 'public-fixture'),
                                          lambda: auth.auto_login_side('left', 'player')):
                            with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
                                operation()
                            self.assertFalse(json.loads(output.getvalue())['ok'])
                            self.assertFalse((root / 'state/left/session.json').exists())
                        run.assert_not_called()

    def test_socket_endpoint_requests_both_access_grants(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-endpoint-') as directory, socket.socket(socket.AF_UNIX) as server:
            runtime = Path(directory)
            endpoint=runtime/'dosplex-0';server.bind(str(endpoint))
            username=pwd.getpwuid(os.getuid()).pw_name
            with patch.object(auth, 'SOCKET_RUNTIME_DIR', runtime), patch.object(auth, 'SOCKET_NAME', 'dosplex-0'):
                with auth.grant_wayland_access(username):
                    for path,permission in ((runtime,'--x'),(endpoint,'rw-')):
                        acl=subprocess.check_output(['getfacl','-cpn',str(path)],text=True)
                        self.assertIn(f'user:{os.getuid()}:{permission}',acl)

    def test_partial_grant_and_publication_failure_restore_exact_acls(self):
        for boundary in ('grant','publication'):
            for login in (False,True):
                with self.subTest(boundary=boundary,login=login), tempfile.TemporaryDirectory(prefix='dosplex-acl-') as directory, socket.socket(socket.AF_UNIX) as server:
                    root=Path(directory);runtime=root/'runtime';runtime.mkdir(mode=0o700)
                    endpoint=runtime/'dosplex-0';server.bind(str(endpoint))
                    username=pwd.getpwuid(os.getuid()).pw_name
                    paths=(runtime,endpoint)
                    def acls():
                        return [subprocess.check_output(['getfacl','-cpn',str(p)],text=True) for p in paths]
                    before=acls();original=subprocess.run
                    def injected(args,**kwargs):
                        if boundary=='grant' and args[:3]==['setfacl','-m',f'u:{username}:rw']:
                            raise subprocess.CalledProcessError(1,args)
                        return original(args,**kwargs)
                    with patch.object(auth,'SIDE_ROOT',root/'state'), \
                         patch.object(auth,'SOCKET_RUNTIME_DIR',runtime),patch.object(auth,'SOCKET_NAME','dosplex-0'), \
                         patch.object(auth,'authenticate',return_value=True), \
                         patch.object(auth,'save_state',side_effect=OSError('injected publication failure')) as publish, \
                         patch.object(auth.subprocess,'run',side_effect=injected):
                        with self.assertRaises((OSError,subprocess.CalledProcessError)):
                            if login:auth.login_side('left',username,'public-fixture')
                            else:auth.auto_login_side('left',username)
                        self.assertEqual(publish.call_count,int(boundary=='publication'))
                    self.assertEqual(acls(),before)
                    self.assertFalse((root/'state/left/session.json').exists())

    def test_replacement_socket_is_not_modified_by_rollback(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-acl-replace-') as directory, \
             socket.socket(socket.AF_UNIX) as old, socket.socket(socket.AF_UNIX) as replacement:
            runtime=Path(directory);endpoint=runtime/'dosplex-0';old.bind(str(endpoint))
            def acl(path):return subprocess.check_output(['getfacl','-cpn',str(path)],text=True)
            baseline=acl(runtime)
            with patch.object(auth,'SOCKET_RUNTIME_DIR',runtime),patch.object(auth,'SOCKET_NAME','dosplex-0'):
                with self.assertRaises(OSError):
                    with auth.grant_wayland_access(pwd.getpwuid(os.getuid()).pw_name):
                        endpoint.unlink();replacement.bind(str(endpoint))
                        replacement_acl=acl(endpoint)
                        raise OSError('injected claim failure after endpoint replacement')
            self.assertEqual(acl(endpoint),replacement_acl)
            self.assertEqual(acl(runtime),baseline)


if __name__ == '__main__':
    unittest.main()
