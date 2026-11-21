#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Guest gets its own IPv4 loopback address, so that its games reach its own Steam (T-452).

Both sides share the host's network namespace. Each Steam wants 127.0.0.1:57343; the one that starts second cannot
have it, and that side's games and web helper then talk to the other account's Steam. On the Deck every game on
Guest exited within seconds. dosplex-side-loopback.py makes four cgroup socket-address programs that turn 127.0.0.1
into the side's own address, and the supervisor has systemd attach them to Guest's session unit.

This checks, without the kernel: what the programs do (they are run here by a small interpreter of the six
instructions they use), which addresses a side may be given, the systemd arguments, the pin and unpin rules that need
no bpf filesystem, and that the supervisor hands the arguments to Guest's unit and never to Main's, starts Guest
without them when they cannot be made, and removes the pins when it ends. What the kernel and systemd do with them
is side-loopback-check.py in the development tree, run as root on a device.
"""
import contextlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loopback = load('side_loopback', 'packaging/steamos/dosplex-side-loopback.py')
m = load('supervisor', 'packaging/steamos/dosplex-side-supervisor.py')
SLEEP = time.sleep


def run_program(code, address, family_port=(2, 0x1234)):
    """Run a program on a context whose user_ip4 is address; return (verdict, the address afterwards, other fields).

    Only the instructions the programs use: a 32-bit load from and store to the context, a 32-bit compare, 32- and
    64-bit moves of an immediate, and exit. Anything else fails the test.
    """
    context = bytearray(64)
    struct.pack_into('<I', context, 0, family_port[0])
    context[4:8] = ipaddress.IPv4Address(address).packed
    struct.pack_into('<I', context, 24, family_port[1])
    registers = {1: 'context'}
    counter = 0
    for _ in range(64):
        operation, pair, offset, immediate = struct.unpack_from('<BBhi', code, counter * 8)
        destination, source = pair & 15, pair >> 4
        counter += 1
        if operation == 0x61:      # load 32 bits from the context
            assert registers[source] == 'context'
            registers[destination] = struct.unpack_from('<I', context, offset)[0]
        elif operation == 0x63:    # store 32 bits to the context
            assert registers[destination] == 'context'
            struct.pack_into('<I', context, offset, registers[source] & 0xFFFFFFFF)
        elif operation == 0x56:    # 32-bit "not equal" jump
            if registers[destination] & 0xFFFFFFFF != immediate & 0xFFFFFFFF:
                counter += offset
        elif operation == 0xb4:    # 32-bit move
            registers[destination] = immediate & 0xFFFFFFFF
        elif operation == 0xb7:    # 64-bit move
            registers[destination] = immediate
        elif operation == 0x95:    # exit
            rest = bytes(context[:4]) + bytes(context[8:])
            return registers[0], str(ipaddress.IPv4Address(bytes(context[4:8]))), rest
        else:
            raise AssertionError(f'unexpected instruction {operation:#x}')
    raise AssertionError('the program did not end')


class Programs(unittest.TestCase):
    def test_there_is_one_for_bind_connect_send_and_receive(self):
        made = loopback.programs('127.0.0.2')
        self.assertEqual({name: attach for name, (attach, _code) in made.items()},
                         {'bind4': 8, 'connect4': 10, 'sendmsg4': 14, 'recvmsg4': 19})
        for _attach, code in made.values():
            self.assertEqual(len(code), 6 * 8)

    def test_what_a_side_binds_connects_or_sends_to_on_loopback_goes_to_its_own_address(self):
        for address in ('127.0.0.2', '127.0.0.3', '127.0.0.200', '127.9.8.7'):
            made = loopback.programs(address)
            for name in ('bind4', 'connect4', 'sendmsg4'):
                verdict, after, _rest = run_program(made[name][1], '127.0.0.1')
                self.assertEqual((verdict, after), (1, address), (name, address))

    def test_every_other_address_is_left_alone_and_the_call_goes_on(self):
        made = loopback.programs('127.0.0.2')
        others = ('0.0.0.0', '127.0.0.2', '127.0.0.53', '127.0.1.1', '192.0.2.7', '1.0.0.127', '255.255.255.255')
        for name, (_attach, code) in made.items():
            for address in others:
                if name == 'recvmsg4' and address == '127.0.0.2':
                    continue
                verdict, after, _rest = run_program(code, address)
                self.assertEqual((verdict, after), (1, address), (name, address))

    def test_a_received_datagram_from_the_sides_address_says_loopback(self):
        code = loopback.programs('127.0.0.200')['recvmsg4'][1]
        self.assertEqual(run_program(code, '127.0.0.200')[:2], (1, '127.0.0.1'))
        self.assertEqual(run_program(code, '127.0.0.1')[:2], (1, '127.0.0.1'))

    def test_nothing_but_the_address_is_written(self):
        blank = run_program(loopback.programs('127.0.0.2')['connect4'][1], '8.8.8.8')[2]
        for name, (_attach, code) in loopback.programs('127.0.0.2').items():
            self.assertEqual(run_program(code, '127.0.0.1')[2], blank, name)
            self.assertEqual(run_program(code, '127.0.0.2')[2], blank, name)


class Addresses(unittest.TestCase):
    def test_a_side_may_have_any_loopback_address_that_is_free(self):
        for address in ('127.0.0.2', '127.0.0.52', '127.0.0.55', '127.1.2.3', '127.255.255.254'):
            self.assertEqual(loopback.side_address(address), address)

    def test_the_hosts_the_resolvers_and_anything_off_loopback_are_refused(self):
        for address in ('127.0.0.1', '127.0.0.0', '127.255.255.255', '127.0.0.53', '127.0.0.54', '198.51.100.2',
                        '0.0.0.0', '::1', 'localhost', '', '127.0.0', '127.0.0.2/8', 'shared'):
            with self.assertRaises(ValueError, msg=address):
                loopback.side_address(address)
            with self.assertRaises(ValueError, msg=address):
                loopback.programs(address)


class Pins(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-loopback-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_the_systemd_arguments_name_each_pinned_program_by_its_attach_point(self):
        self.assertEqual(loopback.systemd_properties('/sys/fs/bpf/dosplex/right'), [
            '--property=BPFProgram=bind4:/sys/fs/bpf/dosplex/right/bind4',
            '--property=BPFProgram=connect4:/sys/fs/bpf/dosplex/right/connect4',
            '--property=BPFProgram=sendmsg4:/sys/fs/bpf/dosplex/right/sendmsg4',
            '--property=BPFProgram=recvmsg4:/sys/fs/bpf/dosplex/right/recvmsg4'])

    def test_pinning_outside_a_bpf_filesystem_is_refused_and_makes_nothing(self):
        with self.assertRaisesRegex(ValueError, 'not on a bpf filesystem'):
            loopback.pin(self.root / 'right', '127.0.0.2')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            loopback.pin(Path('relative/right'), '127.0.0.2')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unpin_removes_the_four_names_and_the_folder(self):
        folder = self.root / 'right'
        folder.mkdir()
        for name in loopback.PROGRAMS:
            (folder / name).write_text('')
        self.assertTrue(loopback.unpin(folder))
        self.assertFalse(folder.exists())
        self.assertFalse(loopback.unpin(folder))                               # nothing there: no error

    def test_unpin_does_not_follow_a_link_and_leaves_a_folder_with_anything_else_in_it(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'bind4').write_text('')
        (self.root / 'link').symlink_to(real)
        self.assertFalse(loopback.unpin(self.root / 'link'))
        self.assertTrue((real / 'bind4').exists())
        (real / 'other').write_text('keep')
        with self.assertRaises(OSError):
            loopback.unpin(real)
        self.assertEqual((real / 'other').read_text(), 'keep')


class SessionUnit(unittest.TestCase):
    def arguments(self, **options):
        session = m.Session('right', 'guest', ['/bin/true'], {'A': 'b'}, parent_unit='dosplex-compositor.service', **options)
        with patch.object(m.subprocess, 'run') as run:
            session.start()
        return run.call_args.args[0]

    def test_the_properties_come_before_the_command(self):
        properties = loopback.systemd_properties('/sys/fs/bpf/dosplex/right')
        arguments = self.arguments(properties=properties)
        separator = arguments.index('--')
        for item in properties:
            self.assertIn(item, arguments[:separator])
        self.assertEqual(arguments[separator + 1:separator + 3], ['/usr/bin/env', '-i'])
        self.assertFalse(any('BPFProgram' in item for item in self.arguments()))

    def test_anything_but_a_program_attachment_is_refused(self):
        for bad in (['--property=User=root'], ['--uid=0'], [b'--property=BPFProgram=x'], ['--property=ExecStartPre=/x']):
            with self.assertRaises(ValueError, msg=bad):
                m.Session('right', 'guest', ['/bin/true'], {}, properties=bad)
        with self.assertRaises(ValueError):
            m.Session('right', 'guest', ['/bin/true'], {}, user_manager=True,
                      properties=['--property=BPFProgram=bind4:/sys/fs/bpf/x'])


def said_by(call, *arguments, **options):
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        return call(*arguments, **options), said.getvalue()


class Prepare(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-loopback-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'dosplex'
        self.pinned = []

        def pin(directory, address):
            Path(directory).mkdir(mode=0o700)
            for name in loopback.PROGRAMS:
                (Path(directory) / name).write_text(address)
            self.pinned.append((Path(directory), address))
        patcher = patch.object(m.side_loopback, 'pin', pin)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('DOSPLEX_GUEST_LOOPBACK', None)
        self.addCleanup(os.environ.pop, 'DOSPLEX_GUEST_LOOPBACK', None)

    def test_it_pins_the_sides_set_and_returns_the_arguments(self):
        arguments, said = said_by(m.prepare_loopback, 'right', self.root)
        self.assertEqual(self.pinned, [(self.root / 'right', '127.0.0.2')])
        self.assertEqual(arguments, loopback.systemd_properties(self.root / 'right'))
        self.assertIn('right has its own loopback address 127.0.0.2', said)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)

    def test_a_set_left_by_a_killed_supervisor_is_replaced(self):
        said_by(m.prepare_loopback, 'right', self.root)
        arguments, _said = said_by(m.prepare_loopback, 'right', self.root, '127.0.0.9')
        self.assertEqual(self.pinned[-1], (self.root / 'right', '127.0.0.9'))
        self.assertEqual((self.root / 'right/bind4').read_text(), '127.0.0.9')
        self.assertEqual(len(arguments), 4)

    def test_the_environment_can_name_the_address_or_keep_the_shared_loopback(self):
        os.environ['DOSPLEX_GUEST_LOOPBACK'] = '127.0.0.77'
        said_by(m.prepare_loopback, 'right', self.root)
        self.assertEqual(self.pinned, [(self.root / 'right', '127.0.0.77')])
        os.environ['DOSPLEX_GUEST_LOOPBACK'] = 'shared'
        arguments, said = said_by(m.prepare_loopback, 'right', self.root)
        self.assertEqual(arguments, [])
        self.assertIn('keeps the shared loopback', said)
        self.assertEqual(len(self.pinned), 1)

    def test_a_failure_is_said_and_the_side_starts_without(self):
        for address, problem in (('127.0.0.1', 'cannot be a side'), ('203.0.113.1', 'cannot be a side')):
            arguments, said = said_by(m.prepare_loopback, 'right', self.root, address)
            self.assertEqual(arguments, [])
            self.assertIn('right starts on the shared loopback', said)
            self.assertIn(problem, said)
        with patch.object(m.side_loopback, 'pin', side_effect=PermissionError(1, 'Operation not permitted')):
            arguments, said = said_by(m.prepare_loopback, 'right', self.root)
        self.assertEqual(arguments, [])
        self.assertIn('Operation not permitted', said)

    def test_release_removes_the_set_and_the_folder_only_when_it_is_empty(self):
        said_by(m.prepare_loopback, 'right', self.root)
        said_by(m.prepare_loopback, 'left', self.root)
        m.release_loopback('right', self.root)
        self.assertFalse((self.root / 'right').exists())
        self.assertTrue((self.root / 'left/bind4').exists())
        m.release_loopback('left', self.root)
        self.assertFalse(self.root.exists())
        m.release_loopback('left', self.root)                                  # nothing there: no error


ENTRY = dict(role='primary', x=0, y=0, width=960, height=1080, game_width=960, game_height=1080,
             scale_mode='fit', display_revision=1, activity='active')


class FakeSession:
    """Stands in for the owned service; keeps what each unit was started with."""
    started_with = []

    def __init__(self, side, owner, command, environment, **options):
        self.unit = 'fake.service'
        self.started = self.frozen = False
        self.options = options

    def start(self):
        self.started = True
        FakeSession.started_with.append(self.options)

    def is_running(self):
        return self.started

    def stop(self):
        self.started = False

    def set_standby(self, enabled):
        self.frozen = enabled


class Supervised(unittest.TestCase):
    """The real supervise() loop with fake sessions and a pin that writes ordinary files."""

    def supervise(self, side, role, *, pin_fails=False, with_root=True):
        if os.getuid() < 1000:
            self.skipTest('supervise() only runs sessions for an ordinary user')
        temp = tempfile.TemporaryDirectory(prefix='dosplex-supervisor-loopback-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.pins = root / 'bpf' / 'dosplex'
        (root / 'bpf').mkdir()
        FakeSession.started_with = []
        directory, state, host_path = root / side, root / 'state.json', root / 'host'
        host = socket.socket(socket.AF_UNIX)
        host.bind(str(host_path))
        self.addCleanup(host.close)
        m.atomic_json(state, {'layout': 'vertical', 'contract_version': 2, 'left': dict(ENTRY), 'right': dict(ENTRY, x=960)})

        def pin(folder, address):
            if pin_fails:
                raise OSError(1, 'Operation not permitted')
            Path(folder).mkdir(mode=0o700)
            for name in loopback.PROGRAMS:
                (Path(folder) / name).write_text(address)
        user = SimpleNamespace(pw_uid=os.getuid(), pw_name='tester', pw_dir=str(root), pw_shell='/bin/sh')
        for patcher in (patch.object(m.pwd, 'getpwnam', return_value=user), patch.object(m.side_loopback, 'pin', pin),
                        patch.object(m.time, 'sleep', lambda seconds: SLEEP(min(seconds, .005)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        stop, errors, said = threading.Event(), [], io.StringIO()
        options = dict(loopback_root=self.pins) if with_root else {}

        def run():
            try:
                m.supervise(side, 'tester', directory, state, host_path, root, stop, session_factory=FakeSession,
                            role=role, **options)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        redirect = contextlib.redirect_stdout(said)
        redirect.__enter__()
        try:
            thread.start()

            def lifecycle():
                try:
                    return json.loads((directory / 'session.json').read_text()).get('lifecycle')
                except (OSError, ValueError):
                    return None

            def running(starts):
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    if len(FakeSession.started_with) >= starts and lifecycle() == 'running':
                        return
                    if not thread.is_alive():
                        self.fail(f'the supervisor ended: {errors!r}')
                    SLEEP(.002)
                self.fail('timed out waiting for the side to run')
            running(1)
            pinned_while_running = (self.pins / side / 'connect4').exists()
            (directory / 'restart').write_text('again\n')
            running(2)
        finally:
            stop.set()
            thread.join(timeout=15)
            redirect.__exit__(None, None, None)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return pinned_while_running, said.getvalue()

    def test_guests_units_get_the_programs_and_the_pins_go_when_the_supervisor_ends(self):
        pinned, said = self.supervise('right', 'guest')
        expected = {'properties': loopback.systemd_properties(self.pins / 'right')}
        self.assertEqual(FakeSession.started_with, [expected, expected])       # the restart reuses the one set
        self.assertTrue(pinned)
        self.assertEqual(said.count('has its own loopback address 127.0.0.2'), 1)
        self.assertFalse(self.pins.exists())

    def test_main_keeps_the_hosts_loopback(self):
        pinned, said = self.supervise('left', 'primary')
        self.assertEqual(FakeSession.started_with, [{}, {}])
        self.assertFalse(pinned)
        self.assertNotIn('loopback', said)

    def test_guest_starts_on_the_shared_loopback_when_the_programs_cannot_be_made(self):
        _pinned, said = self.supervise('right', 'guest', pin_fails=True)
        self.assertEqual(FakeSession.started_with, [{}, {}])
        self.assertEqual(said.count('right starts on the shared loopback: [Errno 1] Operation not permitted'), 1)

    def test_a_caller_that_names_no_folder_changes_nothing(self):
        _pinned, said = self.supervise('right', 'guest', with_root=False)
        self.assertEqual(FakeSession.started_with, [{}, {}])
        self.assertNotIn('loopback', said)


if __name__ == '__main__':
    unittest.main()
