#!/usr/bin/env python3
# dosplex-test: tier=integration needs=systemd-user
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Real systemd freeze/thaw: preserve state, pause work, leave peer running."""
import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('supervisor',ROOT/'packaging/steamos/dosplex-side-supervisor.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def wait(predicate):
    deadline=time.monotonic()+5
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('state transition timed out')
        time.sleep(.02)


def main():
    with tempfile.TemporaryDirectory(prefix='dosplex-standby-') as d:
        root=Path(d);script=root/'counter.py'
        script.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nn=0\nwhile True:\n n+=1\n p.write_text(str(n))\n time.sleep(.02)\n')
        sessions=[]
        try:
            for side in ('left','right'):
                session=m.Session(side,pwd.getpwuid(os.getuid()).pw_name,
                    ['/usr/bin/python3',str(script),str(root/side)],{},user_manager=True)
                sessions.append(session);session.start();wait(lambda:(root/side).exists())
            peer,guest=sessions
            guest.set_standby(True)
            assert guest.frozen and guest.is_running()
            saved=(root/'right').read_text();left=(root/'left').read_text()
            time.sleep(.3)
            assert (root/'right').read_text()==saved,'frozen session advanced'
            assert (root/'left').read_text()!=left,'peer stopped'
            guest.set_standby(False)
            wait(lambda:(root/'right').read_text()!=saved)
            assert guest.is_running()
            # Freeze may succeed while its verification times out. Preserve
            # the uncertain flag so recovery/cleanup still attempts thaw.
            with patch.object(m.subprocess, 'check_output', side_effect=subprocess.TimeoutExpired('show', 5)):
                try:
                    guest.set_standby(True)
                    raise AssertionError('verification timeout was swallowed')
                except subprocess.TimeoutExpired:
                    pass
            assert guest.frozen
            guest.set_standby(False)
            saved=(root/'right').read_text()
            wait(lambda:(root/'right').read_text()!=saved)
            # Off/cleanup must work from standby without leaving frozen units.
            guest.set_standby(True);guest.stop()
            assert not guest.is_running() and not guest.runtime.exists()
            assert peer.is_running()
        finally:
            for session in sessions:session.stop()
    print('PASS: standby pauses work, preserves live session, resumes progress, leaves peer running, and stops cleanly')


if __name__=='__main__':main()
