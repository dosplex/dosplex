#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Execute selector routing with disposable host/sudo spies and a real socket."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SelectorTests(unittest.TestCase):
    def test_routing_and_no_host_fallback_inside_dosplex(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-selector-') as tmp:
            root = Path(tmp)
            capture = root / 'capture.json'
            host = root / 'host'
            helper = root / 'helper'
            sudo = root / 'sudo'
            for name, path in [('host', host), ('sudo', sudo), ('client', root / 'python3')]:
                path.write_text('#!/usr/bin/python3\nimport os,json,sys\n'
                                'open(os.environ["CAPTURE"],"w").write(json.dumps([' + repr(name) + ',*sys.argv[1:]]))\n'
                                'sys.exit(int(os.environ.get("SPY_EXIT","0")))\n')
                path.chmod(0o755)
            helper.write_text('#!/bin/sh\nexit 99\n'); helper.chmod(0o755)
            source = (ROOT / 'packaging/steamos/steamos-session-select-shim.sh').read_text()
            # Relocate only fixed installed executable paths; execute all routing
            # logic unchanged. No host binaries or sudo policy are modified.
            shim = root / 'shim'
            shim.write_text(source.replace('/usr/bin/steamos-session-select.orig', str(host))
                                  .replace('/usr/local/bin/dosplex-switch-mode.sh', str(helper)))
            env = {'PATH': str(root), 'CAPTURE': str(capture)}

            def run(args, extra):
                capture.unlink(missing_ok=True)
                result = subprocess.run(['/bin/bash', str(shim), *args], env=dict(env, **extra),
                                        capture_output=True, text=True, timeout=3)
                return result, json.loads(capture.read_text()) if capture.exists() else None

            with socket.socket(socket.AF_UNIX) as endpoint:
                endpoint.bind(str(root / 'display'))
                inside = {'DOSPLEX_SIDE': 'left', 'DOSPLEX_SESSION_RUNTIME': '1',
                          'DOSPLEX_HOST_SOCKET': str(root / 'display')}
                for side in ('left', 'right'):
                    for target, mode in [('desktop', 'desktop'), ('plasma-wayland', 'desktop'),
                                         ('steam', 'gaming'), ('gamescope', 'gaming')]:
                        result, seen = run([target], dict(inside, DOSPLEX_SIDE=side))
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(seen, ['sudo', '-n', str(helper), side, mode])
                for args, extra in [(['unknown'], inside), ([], inside), (['steam', 'extra'], inside),
                                    (['steam'], dict(inside, DOSPLEX_SIDE='')),
                                    (['steam'], dict(inside, DOSPLEX_SIDE='bogus')),
                                    (['steam'], dict(inside, DOSPLEX_HOST_SOCKET=str(root / 'missing'))),
                                    (['steam'], dict(inside, DOSPLEX_SIDE_ROOT='/run/dosplex-trial/sides')),
                                    (['steam'], {'DOSPLEX_SESSION_RUNTIME': '1'})]:
                    with self.subTest(args=args, extra=extra):
                        result, seen = run(args, extra)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIsNone(seen)
                result, seen = run(['steam'], dict(inside, SPY_EXIT='37'))
                self.assertEqual(result.returncode, 37)
                self.assertEqual(seen[0], 'sudo')
                control_env = dict(inside, DOSPLEX_SESSION_CONTROL=str(root / 'control.sock'),
                                   DOSPLEX_SESSION_CONTROL_CLIENT=str(helper),
                                   DOSPLEX_SIDE_ROOT='/run/dosplex-trial/sides')
                result, seen = run(['desktop'], control_env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(seen, ['client', str(helper), 'client-mode', 'desktop'])
                result, seen = run(['desktop'], dict(control_env, SPY_EXIT='39'))
                self.assertEqual(result.returncode, 39)
                self.assertEqual(seen[0], 'client')
                result, seen = run(['desktop'], dict(control_env, DOSPLEX_SESSION_CONTROL_CLIENT=''))
                self.assertNotEqual(result.returncode, 0); self.assertIsNone(seen)
                helper.unlink()
                result, seen = run(['steam'], inside)
                self.assertNotEqual(result.returncode, 0); self.assertIsNone(seen)
                result, seen = run(['desktop', 'host-option'], {})
                self.assertEqual(result.returncode, 0)
                self.assertEqual(seen, ['host', 'desktop', 'host-option'])


if __name__ == '__main__': unittest.main()
