#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=60
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The power guard: the polkit rule for the main player, what it reads from logind, and what it tells the compositor."""
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load('power_guard', ROOT / 'packaging/steamos/dosplex-power-guard.py')


class RuleTests(unittest.TestCase):
    def test_the_rule_answers_yes_to_restart_and_shutdown_for_the_main_player_only(self):
        text = guard.rule_text('deck')
        self.assertTrue(text.startswith(guard.RULE_MARK))
        self.assertIn('subject.user == "deck"', text)
        for action in ('org.freedesktop.login1.reboot', 'org.freedesktop.login1.reboot-multiple-sessions',
                       'org.freedesktop.login1.power-off', 'org.freedesktop.login1.power-off-multiple-sessions'):
            self.assertIn('action.id == "%s"' % action, text)
        self.assertEqual(text.count('polkit.Result.YES'), 1)
        # Sleep is not its business, and neither is going past a program that blocks a shutdown.
        for word in ('suspend', 'hibernate', 'ignore-inhibit', 'polkit.Result.NO', 'guest'):
            self.assertNotIn(word, text)

    def test_a_name_that_is_not_an_account_name_never_reaches_the_rule(self):
        for name in ('', 'Deck', 'deck"', 'a b', 'x' * 40, 'deck\n', '"; return polkit.Result.YES; //'):
            with self.assertRaises(ValueError):
                guard.rule_text(name)

    def test_it_writes_its_rule_replaces_its_own_and_removes_only_its_own(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / 'polkit-1/rules.d'
            self.assertTrue(guard.write_rule(rules, 'deck'))
            path = rules / guard.RULE_NAME
            self.assertEqual(path.read_text(), guard.rule_text('deck'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertTrue(guard.write_rule(rules, 'main'))                 # its own file is written again
            self.assertIn('subject.user == "main"', path.read_text())
            self.assertTrue(guard.remove_rule(rules))
            self.assertFalse(path.exists())
            self.assertFalse(guard.remove_rule(rules))                      # nothing there: nothing to do

    def test_a_file_of_that_name_that_is_somebody_elses_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory)
            path = rules / guard.RULE_NAME
            path.write_text('// mine\n')
            with patch.object(guard, 'log'):
                self.assertFalse(guard.write_rule(rules, 'deck'))
            self.assertEqual(path.read_text(), '// mine\n')
            self.assertFalse(guard.remove_rule(rules))
            self.assertEqual(path.read_text(), '// mine\n')
            path.unlink()
            path.symlink_to('/dev/null')                                    # a link is nobody's rule
            with patch.object(guard, 'log'):
                self.assertFalse(guard.write_rule(rules, 'deck'))
            self.assertTrue(path.is_symlink())


class LogindTests(unittest.TestCase):
    PREFIX = '/org/freedesktop/login1: org.freedesktop.login1.Manager'

    def test_it_reads_the_announcement_of_a_shutdown_and_its_kind(self):
        cases = {
            ".PrepareForShutdownWithMetadata (true, {'type': <'reboot'>})": ('down', 'reboot'),
            ".PrepareForShutdownWithMetadata (true, {'type': <'power-off'>})": ('down', 'poweroff'),
            # What the logind of SteamOS sends for a shutdown; unread, the panel said "The machine is going down".
            ".PrepareForShutdownWithMetadata (true, {'type': <'poweroff'>})": ('down', 'poweroff'),
            ".PrepareForShutdownWithMetadata (true, {'type': <'halt'>})": ('down', 'poweroff'),
            ".PrepareForShutdownWithMetadata (true, {'type': <'kexec'>})": ('down', 'reboot'),
            ".PrepareForShutdownWithMetadata (true, {'type': <'something-new'>})": ('down', 'down'),
            ".PrepareForShutdownWithMetadata (true, @a{sv} {})": ('down', 'down'),
            ".PrepareForShutdownWithMetadata (false, {'type': <'reboot'>})": ('called-off', None),
            '.PrepareForShutdown (true,)': ('down', 'down'),
            '.PrepareForShutdown (false,)': ('called-off', None),
            '.PrepareForSleep (true,)': None,
            ".SessionNew ('3', objectpath '/org/freedesktop/login1/session/_33')": None,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(guard.parse_event(self.PREFIX + line + '\n'), expected)

    def test_the_wait_of_logind_is_read_and_has_a_default(self):
        ok = type('Done', (), {'returncode': 0, 'stdout': '(<uint64 5000000>,)\n'})()
        with patch.object(guard.subprocess, 'run', return_value=ok):
            self.assertEqual(guard.read_delay_max(), 5.0)
        with patch.object(guard.subprocess, 'run', side_effect=OSError('no gdbus')):
            self.assertEqual(guard.read_delay_max(), guard.DELAY_MAX_DEFAULT)

    def test_polkit_is_told_to_read_its_rules_again(self):
        # Seen on the Steam Deck: after a boot the rule was in place and logind still said "challenge", because polkit
        # watches only the directories of rules that existed when it started.
        self.assertEqual(guard.RELOAD_COMMAND, ['systemctl', 'reload', 'polkit.service'])
        ok = type('Done', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        bad = type('Done', (), {'returncode': 1, 'stdout': '', 'stderr': 'Unit polkit.service not found.\n'})()
        with patch.object(guard.subprocess, 'run', return_value=ok) as run:
            self.assertTrue(guard.reload_polkit())
            self.assertEqual(run.call_args.args[0], guard.RELOAD_COMMAND)
        with patch.object(guard.subprocess, 'run', return_value=bad), patch.object(guard, 'log') as said:
            self.assertFalse(guard.reload_polkit())
            self.assertIn('polkit.service not found', said.call_args.args[0])
        with patch.object(guard.subprocess, 'run', side_effect=OSError('no systemctl')), patch.object(guard, 'log'):
            self.assertFalse(guard.reload_polkit())

    def test_the_lock_is_a_delay_lock_on_shutdown(self):
        self.assertIn('--what=shutdown', guard.LOCK_COMMAND)
        self.assertIn('--mode=delay', guard.LOCK_COMMAND)


class CompositorTests(unittest.TestCase):
    def serve(self, root):
        """A stand-in for the compositor's control socket: answers one request, keeps what it got."""
        runtime = Path(root) / 'runtime'
        runtime.mkdir()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        server.bind(str(runtime / 'control.sock'))
        os.chmod(runtime / 'control.sock', 0o600)
        server.listen(1)
        got = []

        def answer():
            connection, _ = server.accept()
            with connection:
                got.append(connection.recv(64))
                connection.send(b'DPXR' + bytes([1, 0, 2, 0]))
            server.close()
        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        return got, thread

    def test_it_asks_for_the_closing_panel_with_the_kind_of_shutdown(self):
        for kind, number in (('reboot', 2), ('poweroff', 3), ('down', 4)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                got, thread = self.serve(root)
                self.assertTrue(guard.tell_compositor(root, kind))
                thread.join(2)
                self.assertEqual(got, [b'DPXC' + bytes([1, 4, number, 0])])

    def test_a_socket_that_is_not_root_only_or_is_missing_is_not_used(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(guard.tell_compositor(root, 'reboot'))         # no compositor
            runtime = Path(root) / 'runtime'
            runtime.mkdir()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            server.bind(str(runtime / 'control.sock'))
            os.chmod(runtime / 'control.sock', 0o666)
            self.assertFalse(guard.tell_compositor(root, 'reboot'))
            server.close()


class SessionTests(unittest.TestCase):
    """The session starts the guard beside the sleep guard, for the main player, and takes its rule away at the end."""

    def test_the_two_programs_that_start_the_guard_agree_on_its_unit_and_command(self):
        trial = load('trial', ROOT / 'packaging/steamos/session/root-session-trial.py')
        update = load('update', ROOT / 'packaging/steamos/session/update-retained-session.py')
        transaction = dict(id='0' * 32, unit='dosplex-compositor-' + '0' * 32 + '.service', main_user='deck')
        root = Path('/run/dosplex-trial-' + '0' * 32)
        first, second = trial.power_guard_command(root, transaction), update.power_guard_command(root, transaction)
        self.assertEqual(first, second)
        unit, command = first
        self.assertEqual(unit, 'dosplex-power-guard-' + '0' * 32 + '.service')
        self.assertIn('--property=BindsTo=' + transaction['unit'], command)
        self.assertEqual(command[-5:], [str(root / 'bundle/libexec/dosplex-power-guard.py'), '--root', str(root),
                                        '--main-user', 'deck'])
        with self.assertRaises(ValueError):
            trial.power_guard_command(root, dict(transaction, main_user='deck"; x'))
        self.assertIn('dosplex-power-guard-' + '0' * 32 + '.service',
                      trial.journaled_native_units(dict(transaction, power_guard_unit=unit)))

    def test_recovery_removes_a_rule_the_guard_left_and_nothing_else(self):
        trial = load('trial', ROOT / 'packaging/steamos/session/root-session-trial.py')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / guard.RULE_NAME
            self.assertFalse(trial.remove_power_rule(path))
            path.write_text(guard.rule_text('deck'))
            self.assertTrue(guard.rule_text('deck').encode().startswith(trial.POWER_RULE_MARK))
            with patch.object(trial.subprocess, 'run') as run:
                self.assertTrue(trial.remove_power_rule(path))
            self.assertEqual(run.call_args.args[0], ['systemctl', 'reload', 'polkit.service'])
            self.assertFalse(path.exists())
            path.write_text('// somebody else\n')
            self.assertFalse(trial.remove_power_rule(path))
            self.assertTrue(path.exists())
        self.assertEqual(str(trial.POWER_RULE), str(Path(guard.RULE_DIR) / guard.RULE_NAME))


if __name__ == '__main__':
    unittest.main()
