#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,wayland-client,cc,ordinary-user timeout=120
# dosplex-run: panel {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The menu's Help and Feedback start the panel, show its window over the side and give it the menu's keys.

The real compositor runs on the headless backend. A Wayland probe client stands in for the panel: the test hook
DOSPLEX_PANEL_COMMAND names a script that records how it was started and then becomes the probe. What is shown:
the two menu entries start it with the right arguments, it is handed its own connection (WAYLAND_SOCKET, no
display name), its window is sized to the side and gets the keyboard focus, a controller's menu commands arrive as
key presses and releases, the menu itself stays as it was underneath, closing the menu ends the panel, and a panel
that ends on its own gives the menu back. It does not show the real launcher, the drop to the side's account
(the test is not root) or Qt; those are a device run."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPOSITOR = None
KEY_DOWN, KEY_ENTER, KEY_ESC = 108, 28, 1


def build_probe(directory):
    protocols = subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip()
    generated = []
    for xml, header, code in [
            ('stable/xdg-shell/xdg-shell.xml', 'xdg-shell-client-protocol.h', 'xdg-shell-protocol.c'),
            ('unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
             'pointer-constraints-unstable-v1-client-protocol.h', 'pointer-constraints-protocol.c'),
            ('unstable/relative-pointer/relative-pointer-unstable-v1.xml',
             'relative-pointer-unstable-v1-client-protocol.h', 'relative-pointer-protocol.c')]:
        for mode, filename in [('client-header', header), ('private-code', code)]:
            subprocess.run(['wayland-scanner', mode, str(Path(protocols) / xml), str(directory / filename)],
                           check=True, timeout=5)
        generated.append(str(directory / code))
    flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-client'], text=True))
    subprocess.run(['cc', '-I', str(directory), str(ROOT / 'tools/dosplex-keyprobe.c'), *generated,
                    '-o', str(directory / 'probe'), *flags], check=True, timeout=15)


def release_with_the_form(directory):
    """A release folder that carries the feedback form: the menu shows Feedback only then."""
    release = directory / 'release'
    form = release / 'share/dosplex/panel/Feedback.qml'
    form.parent.mkdir(parents=True)
    form.write_text('// the form\n')
    form.chmod(0o644)
    return release


def wait_until(predicate, why, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(why() if callable(why) else why)


class PanelTest(unittest.TestCase):
    def test_the_menu_starts_the_panel_and_feeds_it_keys(self):
        if os.geteuid() == 0:
            self.skipTest('run as an ordinary user')
        with tempfile.TemporaryDirectory(prefix='dosplex-panel-') as name:
            root = Path(name)
            build_probe(root)
            stand_in = root / 'panel-stand-in'
            stand_in.write_text('#!/bin/sh\n'
                                'echo "ARGS $*" > "$PANEL_TEST/started"\n'
                                'echo "SOCKET ${WAYLAND_SOCKET:-none} DISPLAY ${WAYLAND_DISPLAY-unset}" >> "$PANEL_TEST/started"\n'
                                'echo $$ > "$PANEL_TEST/pid"\n'
                                'exec "$PANEL_TEST/probe" > "$PANEL_TEST/panel.log" 2>&1\n')
            stand_in.chmod(0o755)
            env = {'PATH': os.environ['PATH'], 'HOME': name, 'XDG_RUNTIME_DIR': name, 'WAYLAND_DISPLAY': 'dosplex-0',
                   'WLR_RENDERER': 'pixman', 'DOSPLEX_WAYLAND_DEBUG': '0',
                   # Neither side belongs to the account the test runs as: on a device the compositor is root and
                   # the panel's connection carries root's credentials, so the panel must get its side's seat
                   # because it is the panel, not because of who it is.
                   'DOSPLEX_LEFT_USER': 'dosplex-test-no-such-user',
                   'DOSPLEX_RIGHT_USER': 'dosplex-test-no-such-user', 'DOSPLEX_AUTH_HELPER': '/bin/false',
                   'DOSPLEX_SESSION_SCRIPT': '/bin/false', 'DOSPLEX_PANEL_COMMAND': str(stand_in),
                   'PANEL_TEST': name, 'DOSPLEX_ROOT': str(release_with_the_form(root))}
            log = (root / 'compositor.log').open('w+')
            compositor = subprocess.Popen([COMPOSITOR, '-b', 'headless', '--test-hooks'], env=env,
                                          stdout=log, stderr=log, start_new_session=True)
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

            def send(command):
                control.sendto(command.encode(), str(root / 'hud-ipc.sock'))

            def state():
                try:
                    return json.loads((root / 'state.json').read_text())
                except (ValueError, OSError):
                    return {}

            def panel_log():
                path = root / 'panel.log'
                return path.read_text() if path.exists() else ''

            def started():
                path = root / 'started'
                return path.read_text() if path.exists() else ''

            def diagnostics():
                return '%s\n--- panel\n%s\n--- started\n%s\n--- compositor\n%s' % (
                    json.dumps({k: state().get(k) for k in ('overlay_visible', 'overlay_tab', 'panel', 'panel_shown')}),
                    panel_log(), started(), (root / 'compositor.log').read_text()[-3000:])

            def keys(code):
                return [line for line in panel_log().splitlines() if line.startswith('KEY ') and f' key={code} ' in line]

            def panel_alive():
                try:
                    os.kill(int((root / 'pid').read_text()), 0)
                    return True
                except (OSError, ValueError):
                    return False
            try:
                wait_until(lambda: (root / 'state.json').exists(), 'the compositor never wrote its state')
                self.assertEqual((state().get('panel'), state().get('panel_shown')), ('', False))
                self.assertIs(state().get('panel_feedback'), True, 'the release carries the form')
                send('OVERLAY left')
                wait_until(lambda: state().get('overlay_visible') is True, diagnostics)
                tab = state()['overlay_tab']

                # The menu opens on its second entry (the first page; Close is the first). Help is the last entry
                # and Feedback the one before it, so "previous" twice reaches Help and three times Feedback.
                def menu(*commands):
                    for command in commands:
                        send('MENU left ' + command)
                        time.sleep(0.15)             # one drawn menu per command, as a person's presses are
                time.sleep(0.3)
                menu('previous', 'previous', 'select')
                wait_until(lambda: state().get('panel') == 'help', diagnostics)
                wait_until(lambda: 'SOCKET' in started(), diagnostics)
                self.assertIn('ARGS --mode help --side main --page display --layout ', started())
                socket_line = [line for line in started().splitlines() if line.startswith('SOCKET')][0]
                self.assertRegex(socket_line, r'^SOCKET \d+ DISPLAY unset$')      # its own connection, by descriptor
                side = state()['left']
                wait_until(lambda: f"TOPLEVEL_CONFIGURE width={side['width']} height={side['height']}" in panel_log(),
                           diagnostics)
                wait_until(lambda: state().get('panel_shown') is True, diagnostics)
                wait_until(lambda: 'KEYBOARD_ENTER' in panel_log(), diagnostics)
                self.assertIn('SEAT name=seat0', panel_log())                 # Main's seat, and only that one
                self.assertEqual(panel_log().count('SEAT name='), 1)

                # A controller's menu commands are the panel's keys now: press and release, and the menu stays put.
                send('MENU left down')
                wait_until(lambda: len(keys(KEY_DOWN)) == 2, diagnostics)
                self.assertEqual([line.split('state=')[1] for line in keys(KEY_DOWN)], ['1', '0'])
                send('MENU left select')
                wait_until(lambda: len(keys(KEY_ENTER)) == 2, diagnostics)
                send('MENU left back')
                wait_until(lambda: len(keys(KEY_ESC)) == 2, diagnostics)
                now = state()
                self.assertTrue(now['overlay_visible'], 'B closed the menu under the panel')
                self.assertEqual((now['overlay_tab'], now['panel']), (tab, 'help'))
                # The other player's controller is not the panel's.
                send('MENU right down')
                time.sleep(0.3)
                self.assertEqual(len(keys(KEY_DOWN)), 2)
                # A second panel is not started while one is open.
                first = (root / 'pid').read_text()
                send('MENU left select')
                time.sleep(0.3)
                self.assertEqual((root / 'pid').read_text(), first)

                # The menu closes: the panel is told to end, and is gone.
                send('MENU left toggle')
                wait_until(lambda: state().get('overlay_visible') is False, diagnostics)
                wait_until(lambda: state().get('panel') == '' and not panel_alive(), diagnostics)
                self.assertFalse(state()['panel_shown'])

                # Feedback, and a panel that ends on its own: the menu is there again and takes its keys.
                (root / 'started').unlink()
                send('OVERLAY left')
                wait_until(lambda: state().get('overlay_visible') is True, diagnostics)
                time.sleep(0.3)
                menu('previous', 'previous', 'previous', 'select')
                wait_until(lambda: state().get('panel') == 'feedback', diagnostics)
                wait_until(lambda: 'ARGS --mode feedback --side main --page menu' in started(), diagnostics)
                wait_until(lambda: state().get('panel_shown') is True, diagnostics)
                os.kill(int((root / 'pid').read_text()), signal.SIGTERM)
                wait_until(lambda: state().get('panel') == '', diagnostics)
                self.assertTrue(state()['overlay_visible'])
                send('MENU left back')
                wait_until(lambda: state().get('overlay_visible') is False, diagnostics)
                self.assertIsNone(compositor.poll(), 'the compositor exited')
            finally:
                control.close()
                try:
                    os.kill(int((root / 'pid').read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
                compositor.terminate()
                try:
                    compositor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                log.close()

    @contextlib.contextmanager
    def help_open(self):
        """The headless compositor with Main's menu open and Help running in it: (send, state, panel_alive, why)."""
        if os.geteuid() == 0:
            self.skipTest('run as an ordinary user')
        with tempfile.TemporaryDirectory(prefix='dosplex-panel-') as name:
            root = Path(name)
            build_probe(root)
            stand_in = root / 'panel-stand-in'
            stand_in.write_text('#!/bin/sh\n'
                                'echo $$ > "$PANEL_TEST/pid"\n'
                                'exec "$PANEL_TEST/probe" > "$PANEL_TEST/panel.log" 2>&1\n')
            stand_in.chmod(0o755)
            env = {'PATH': os.environ['PATH'], 'HOME': name, 'XDG_RUNTIME_DIR': name, 'WAYLAND_DISPLAY': 'dosplex-0',
                   'WLR_RENDERER': 'pixman', 'DOSPLEX_WAYLAND_DEBUG': '0',
                   'DOSPLEX_LEFT_USER': 'dosplex-test-no-such-user',
                   'DOSPLEX_RIGHT_USER': 'dosplex-test-no-such-user', 'DOSPLEX_AUTH_HELPER': '/bin/false',
                   'DOSPLEX_SESSION_SCRIPT': '/bin/false', 'DOSPLEX_PANEL_COMMAND': str(stand_in),
                   # A release that does not carry the feedback form, as the alpha is built.
                   'PANEL_TEST': name, 'DOSPLEX_ROOT': str(root / 'release-without-the-form')}
            log = (root / 'compositor.log').open('w+')
            compositor = subprocess.Popen([COMPOSITOR, '-b', 'headless', '--test-hooks'], env=env,
                                          stdout=log, stderr=log, start_new_session=True)
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

            def send(command):
                control.sendto(command.encode(), str(root / 'hud-ipc.sock'))

            def state():
                try:
                    return json.loads((root / 'state.json').read_text())
                except (ValueError, OSError):
                    return {}

            def why():
                return '%s\n--- compositor\n%s' % (
                    json.dumps({k: state().get(k) for k in ('layout', 'overlay_visible', 'panel', 'panel_shown')}),
                    (root / 'compositor.log').read_text()[-3000:])

            def panel_alive():
                try:
                    os.kill(int((root / 'pid').read_text()), 0)
                    return True
                except (OSError, ValueError):
                    return False
            try:
                wait_until(lambda: (root / 'state.json').exists(), 'the compositor never wrote its state')
                send('OVERLAY left')
                wait_until(lambda: state().get('overlay_visible') is True, why)
                time.sleep(0.3)
                for command in ('previous', 'previous', 'select'):       # the question mark in the top row
                    send('MENU left ' + command)
                    time.sleep(0.15)
                wait_until(lambda: state().get('panel') == 'help' and state().get('panel_shown') is True, why)
                self.assertTrue(panel_alive(), 'the panel was not running')
                yield send, state, panel_alive, why
                self.assertIsNone(compositor.poll(), 'the compositor exited')
            finally:
                control.close()
                try:
                    os.kill(int((root / 'pid').read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
                compositor.terminate()
                try:
                    compositor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                log.close()

    def test_a_release_without_the_form_has_no_feedback_and_help_is_where_it_was(self):
        # help_open reached Help with the same two presses: Help is still the last entry of the menu.
        with self.help_open() as (send, state, panel_alive, why):
            self.assertIs(state().get('panel_feedback'), False, why())
            send('OVERLAY close')
            wait_until(lambda: state().get('panel') == '' and not panel_alive(), why)
            # The entry before Help is no longer Feedback: three presses back and a choice start no feedback panel.
            send('OVERLAY left')
            wait_until(lambda: state().get('overlay_visible') is True, why)
            time.sleep(0.3)
            for command in ('previous', 'previous', 'previous'):
                send('MENU left ' + command)
                time.sleep(0.15)
            self.assertEqual(state().get('panel'), '', why())

    def test_the_panel_ends_when_its_menu_is_closed_from_outside(self):
        # Not by the person's Back: the menu is taken away under the panel (another player's menu, a power action).
        with self.help_open() as (send, state, panel_alive, why):
            send('OVERLAY close')
            wait_until(lambda: state().get('overlay_visible') is False, why)
            wait_until(lambda: state().get('panel') == '' and not panel_alive(),
                       'the menu closed but the panel still runs', timeout=5)
            self.assertFalse(state()['panel_shown'])

    def test_the_panel_stays_with_its_side_through_a_layout_change(self):
        # A layout change moves the side and the panel with it (panel_place); it does not end what the person reads.
        with self.help_open() as (send, state, panel_alive, why):
            before = state().get('layout')
            send('LAYOUT ' + ('single' if before == 'vertical' else 'vertical'))
            wait_until(lambda: state().get('layout') not in (None, before), why)
            time.sleep(1)
            self.assertTrue(panel_alive(), 'the panel ended when the layout changed')
            self.assertEqual((state().get('panel'), state().get('panel_shown')), ('help', True), why())


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('usage: panel-integration.py COMPOSITOR')
    COMPOSITOR = sys.argv.pop(1)
    unittest.main()
