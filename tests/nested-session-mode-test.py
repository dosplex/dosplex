#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,userns,wayland-info,ordinary-user timeout=120
# dosplex-run: default {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Verify root opt-in and screen-copy exclusion in a disposable user namespace.

The screen-copy protocol is a test hook: it is offered only with --test-hooks, and
never together with -s, whatever the other options say.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

binary = Path(sys.argv[1]).resolve()
if '--namespace-worker' not in sys.argv:
    if os.geteuid() == 0:
        raise RuntimeError('run this fixture as an ordinary user')
    denied = subprocess.run([str(binary), '-s', '-b', 'headless'], capture_output=True, timeout=5)
    assert denied.returncode != 0 and b'require root' in denied.stderr
    subprocess.run(['unshare', '--user', '--map-root-user', sys.executable, str(Path(__file__).resolve()),
                    str(binary), '--namespace-worker'], check=True, timeout=30)
    print('PASS: nested real-session opt-in requires root and removes screen-copy protocol')
else:
    assert os.geteuid() == 0
    # (-s, --test-hooks, watch the auth helper for 3.5 s). The helper checks need the
    # auto-login timer to have fired, so only one run of each kind pays for them.
    for real_sessions, test_hooks, check_auth in ((False, True, True), (True, True, True),
                                                  (False, False, False), (True, False, False)):
        with tempfile.TemporaryDirectory(prefix='dosplex-nested-session-') as tmp:
            root = Path(tmp)
            spy = root / 'auth-spy'
            spy.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$XDG_RUNTIME_DIR/auth.called"\nexit 1\n')
            spy.chmod(0o755)
            env = {'PATH': '/usr/bin:/bin', 'HOME': tmp, 'XDG_RUNTIME_DIR': tmp,
                   'WAYLAND_DISPLAY': 'dosplex-0', 'WLR_RENDERER': 'pixman',
                   'DOSPLEX_AUTH_HELPER': str(spy), 'DOSPLEX_SESSION_SCRIPT': '/bin/false',
                   'DOSPLEX_LEFT_USER': 'fixture-user',
                   'DOSPLEX_SIDE_ROOT': str(root / 'state')}
            argv = [str(binary), '-b', 'headless', *(['-s'] if real_sessions else []),
                    *(['--test-hooks'] if test_hooks else [])]
            with (root / 'log').open('w+') as log:
                proc = subprocess.Popen(argv, env=env, stdout=log, stderr=log)
                try:
                    deadline=time.monotonic()+5
                    while not (root / 'dosplex-0').is_socket():
                        if proc.poll() is not None or time.monotonic()>deadline:
                            log.seek(0); raise RuntimeError(log.read())
                        time.sleep(.02)
                    info=subprocess.check_output(['wayland-info'],env=env,text=True,stderr=subprocess.DEVNULL,timeout=5)
                    offered = 'zwlr_screencopy_manager_v1' in info
                    assert offered is (test_hooks and not real_sessions), (argv, info)
                    if check_auth:
                        deadline = time.monotonic()+3.5
                        while time.monotonic()<deadline and not (root / 'auth.called').exists():
                            assert proc.poll() is None
                            time.sleep(.05)
                        assert (root / 'auth.called').exists() is real_sessions
                        if real_sessions:
                            assert (root / 'auth.called').read_text() == 'auto-login\n'
                finally:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait(timeout=5)
