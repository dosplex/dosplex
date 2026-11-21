#!/usr/bin/env python3
# dosplex-test: tier=unit needs=acl
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Arbiter task failure/cancellation fixtures; never open physical input devices."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import os
import signal
import time
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('arbiter', ROOT / 'packaging/steamos/dosplex-gamepad-arbiter.py')
m = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'evdev': SimpleNamespace(InputDevice=Mock(), UInput=Mock(), ecodes=Mock(), list_devices=Mock())}):
    spec.loader.exec_module(m)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def device(self):
        device = m.RoutedGamepad.__new__(m.RoutedGamepad)
        device.dev = Mock()
        device.grabbed = True
        device.sibling_grabs = {}
        device.close_virtual = Mock()
        device.close_dev = Mock()
        device.restore_physical_permissions = Mock()
        return device

    def cleaned(self, device):
        device.dev.ungrab.assert_called_once()
        device.close_virtual.assert_called_once()
        device.close_dev.assert_called_once()
        device.restore_physical_permissions.assert_called_once()

    async def test_route_failure_reaches_supervisor_and_releases_device(self):
        device = self.device()
        async def monitor():
            raise RuntimeError('injected route failure')
        device.route_monitor = monitor
        with self.assertRaisesRegex(RuntimeError, 'injected route failure'):
            await asyncio.wait_for(device.run(), .5)
        self.cleaned(device)

    async def test_cancel_finishes_monitor_before_device_cleanup(self):
        device = self.device()
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def monitor():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                stopped.set()
        device.route_monitor = monitor
        device.close_virtual.side_effect = lambda: self.assertTrue(stopped.is_set())
        task = asyncio.create_task(device.run())
        await asyncio.wait_for(entered.wait(), .5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        self.cleaned(device)

    async def test_removed_device_finishes_cleanup_before_event_node_reuse(self):
        events = []
        children = []
        snapshots = iter([{'/dev/fake'}, set(), {'/dev/fake'}])
        real_sleep = asyncio.sleep
        class EndScan(Exception):
            pass
        class FakeDevice:
            def __init__(self, path):
                if events:
                    self_test.assertIn('stopped', events)
                events.append('created')
            async def run(self):
                children.append(asyncio.current_task())
                try:
                    await asyncio.Future()
                finally:
                    await real_sleep(.01)
                    events.append('stopped')
        self_test = self
        def scan():
            try:
                return next(snapshots)
            except StopIteration:
                raise EndScan()
        async def tick(_):
            await real_sleep(.001)
        try:
            with patch.object(m, 'ROUTE_DIR') as directory, patch.object(m, 'RoutedGamepad', FakeDevice), patch.object(m, 'scan_external_gamepads', side_effect=scan), patch.object(m.asyncio, 'sleep', tick):
                with self.assertRaises(EndScan):
                    await asyncio.wait_for(m.supervise(), 1)
        finally:
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        self.assertEqual(events, ['created', 'stopped', 'created', 'stopped'])


class OwnershipTests(unittest.TestCase):
    def test_each_permission_failure_stops_before_later_commands(self):
        for fail_at in range(3):
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                self.assertTrue(kwargs['check'])
                if len(calls) == fail_at + 1:
                    raise subprocess.CalledProcessError(1, command)
            with self.subTest(fail_at=fail_at), patch.object(m.subprocess, 'run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    m.set_path_owner('/dev/fake-pad', 'guest', '0660')
            self.assertEqual(len(calls), fail_at + 1)

    def test_missing_cleanup_path_is_optional_but_grant_is_not(self):
        with patch.object(m.Path, 'exists', return_value=False), patch.object(m.subprocess, 'run', side_effect=FileNotFoundError) as run:
            m.set_path_owner('/dev/fake-pad', 'deck', '0660', missing_ok=True)
            run.assert_not_called()
            with self.assertRaises(FileNotFoundError):
                m.set_path_owner('/dev/fake-pad', 'guest', '0660')

    def test_clone_is_destroyed_when_node_discovery_or_owner_change_fails(self):
        for node in (None, '/dev/fake-clone'):
            probe = SimpleNamespace(name='Fixture Pad', close=Mock(), info=SimpleNamespace(bustype=3,vendor=1,product=2), uniq='fixture', phys='usb/1')
            with self.subTest(node=node), patch.object(m, 'InputDevice', return_value=probe), patch.object(m, 'physical_family', return_value=Path('/sys/fixture')):
                device = m.RoutedGamepad('/dev/fake-pad')
            device.dev = Mock()
            device.dev.capabilities.return_value = {}
            device.dev.active_keys.return_value = []
            clone = Mock()
            clone.device = None if node is None else SimpleNamespace(path=node, phys='dosplex-gamepad-arbiter/fixture')
            device.ensure_reader = Mock()
            with patch.object(m.PhysicalPermissions, 'lock'), patch.object(m.UInput, 'from_device', return_value=clone), patch.object(m.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixture')), patch.object(m, 'set_owner', side_effect=PermissionError('injected owner failure')) as owner:
                with self.assertRaises((RuntimeError, PermissionError)):
                    device.ensure_virtual('guest')
            clone.close.assert_called_once()
            self.assertIsNone(device.ui)
            self.assertIsNone(device.virtual_path)
            self.assertIsNone(device.target)
            device.ensure_reader.assert_not_called()
            if node is None:
                owner.assert_not_called()


class SiblingGrabTests(unittest.TestCase):
    def test_existing_mouse_handle_is_excluded_until_release(self):
        d=m.RoutedGamepad.__new__(m.RoutedGamepad)
        d.path='/dev/input/event1';d.family=Path('/sys/fixture');d.sibling_grabs={}
        mouse=Mock()
        with patch.object(m,'related_input_paths',return_value=[d.path,'/dev/input/event2']), patch.object(m,'physical_family',return_value=d.family), patch.object(m,'InputDevice',return_value=mouse) as opened:
            d.grab_siblings();d.grab_siblings()
            opened.assert_called_once_with('/dev/input/event2');mouse.grab.assert_called_once()
            d.release_siblings();mouse.ungrab.assert_called_once();mouse.close.assert_called_once()
            self.assertEqual(d.sibling_grabs,{})
    def test_failed_sibling_grab_closes_descriptor(self):
        d=m.RoutedGamepad.__new__(m.RoutedGamepad)
        d.path='/dev/input/event1';d.family=Path('/sys/fixture');d.sibling_grabs={}
        mouse=Mock();mouse.grab.side_effect=OSError('busy')
        with patch.object(m,'related_input_paths',return_value=['/dev/input/event2']), patch.object(m,'physical_family',return_value=d.family), patch.object(m,'InputDevice',return_value=mouse):
            with self.assertRaises(OSError):d.grab_siblings()
        mouse.close.assert_called_once();self.assertEqual(d.sibling_grabs,{})


class TopologyTests(unittest.TestCase):
    def test_owned_virtual_identity_is_excluded_from_rediscovery(self):
        for identity in ('dosplex-gamepad-arbiter', 'dosplex-gamepad-arbiter/unique-clone'):
            with patch.object(m, 'input_phys', return_value=identity), patch.object(m, 'InputDevice') as probe:
                self.assertFalse(m.is_external_gamepad('/dev/fake-clone'))
                probe.assert_not_called()

    def test_identical_models_keep_their_own_event_and_hidraw_family(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-topology-') as directory:
            root = Path(directory)
            inputs, hidraw = root / 'input', root / 'hidraw'
            families = []
            for index, label in enumerate(('a', 'b')):
                family = root / 'devices' / label
                family.mkdir(parents=True)
                (family / 'uevent').write_text('HID_ID=0003:0000054C:00000CE6\nHID_NAME=Wireless Controller\n')
                families.append(family)
                for event in (index*2, index*2+1):
                    node = family / 'input' / ('input'+str(event))
                    node.mkdir(parents=True)
                    (node / 'name').write_text('Wireless Controller')
                    entry = inputs / ('event'+str(event))
                    entry.mkdir(parents=True)
                    (entry / 'device').symlink_to(node, target_is_directory=True)
                entry = hidraw / ('hidraw'+str(index))
                entry.mkdir(parents=True)
                (entry / 'device').symlink_to(family, target_is_directory=True)
            with patch.object(m, 'SYS_INPUT_DIR', inputs), patch.object(m, 'SYS_HIDRAW_DIR', hidraw):
                self.assertEqual(m.related_input_paths(families[0]), ['/dev/input/event0', '/dev/input/event1'])
                self.assertEqual(m.related_hidraw_paths(families[0]), ['/dev/hidraw0'])
                self.assertEqual(m.related_input_paths(families[1]), ['/dev/input/event2', '/dev/input/event3'])
                self.assertEqual(m.related_hidraw_paths(families[1]), ['/dev/hidraw1'])
                # An event number reused by another family must not be captured
                # by teardown of the original family.
                (inputs / 'event0/device').unlink()
                (inputs / 'event0/device').symlink_to(families[1] / 'input/input2', target_is_directory=True)
                self.assertEqual(m.related_input_paths(families[0]), ['/dev/input/event1'])
                self.assertEqual(m.related_input_paths(None), [])

    def test_non_hid_fallback_and_missing_topology(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-topology-') as directory:
            root = Path(directory)
            node = root / 'plain-input'
            node.mkdir()
            self.assertEqual(m.physical_family(node), node)
            self.assertIsNone(m.physical_family(root / 'missing'))

    def test_wrong_clone_identity_is_closed_without_changing_permissions(self):
        with patch.object(m, 'physical_family', return_value=Path('/sys/fixture')), patch.object(m, 'InputDevice', return_value=SimpleNamespace(name='Identical Pad', close=Mock(), info=SimpleNamespace(bustype=3,vendor=1,product=2), uniq='fixture', phys='usb/1')):
            device = m.RoutedGamepad('/dev/fake-pad')
        device.dev = Mock()
        clone = Mock(device=SimpleNamespace(path='/dev/another-pad', phys='not-our-clone'))
        with patch.object(m.PhysicalPermissions, 'lock'), patch.object(m.UInput, 'from_device', return_value=clone), patch.object(m, 'set_owner') as owner:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                device.ensure_virtual('guest')
        owner.assert_not_called()
        clone.close.assert_called_once()
        self.assertIsNone(device.target)


class PermissionSnapshotTests(unittest.TestCase):
    def test_actual_acl_restore_targets_original_inode_after_path_replacement(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-permission-') as directory:
            path = Path(directory) / 'node'
            path.write_text('fixture')
            path.chmod(0o640)
            subprocess.run(['setfacl', '-m', 'u:65534:r--', str(path)], check=True)
            original = path.stat()
            acl = os.getxattr(path, m.PhysicalPermissions.ACL)
            permissions = m.PhysicalPermissions()
            # Real ACL and mode operations on a private regular-file fixture;
            # only the device-type check and privileged ownership change mocked.
            with patch.object(m.stat, 'S_ISCHR', return_value=True), patch.object(m.os, 'fchown') as owner:
                permissions.lock([str(path)])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                renamed = path.with_name('original-node')
                path.rename(renamed)
                path.write_text('replacement')
                path.chmod(0o604)
                with self.assertRaisesRegex(RuntimeError, 'replaced'):
                    permissions.capture(str(path))
                permissions.restore()
                self.assertEqual(owner.call_args.args[1:], (original.st_uid, original.st_gid))
            self.assertEqual(os.getxattr(renamed, m.PhysicalPermissions.ACL), acl)
            self.assertEqual(stat.S_IMODE(renamed.stat().st_mode), stat.S_IMODE(original.st_mode))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o604)
            self.assertEqual(permissions.saved, {})

    def test_restore_attempts_other_nodes_and_reports_failure(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-permission-') as directory:
            paths = [Path(directory) / name for name in ('a', 'b')]
            for path in paths:
                path.write_text('fixture')
            permissions = m.PhysicalPermissions()
            with patch.object(m.stat, 'S_ISCHR', return_value=True):
                for path in paths:
                    permissions.capture(str(path))
            fds = [record[0] for record in permissions.saved.values()]
            with patch.object(m.os, 'fchown', side_effect=[PermissionError('injected'), None]) as owner:
                with self.assertRaisesRegex(RuntimeError, 'restoration failed'):
                    permissions.restore()
            self.assertEqual(owner.call_count, 2)
            self.assertEqual(permissions.saved, {})
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_real_sigterm_awaits_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-signal-') as directory:
            root = Path(directory)
            source = ROOT / 'packaging/steamos/dosplex-gamepad-arbiter.py'
            code = f"""
import asyncio, runpy, sys
from pathlib import Path
from types import SimpleNamespace
sys.modules['evdev'] = SimpleNamespace(InputDevice=None,UInput=None,ecodes=None,list_devices=None)
module = runpy.run_path({str(source)!r})
root = Path({directory!r})
async def fixture():
    (root/'ready').touch()
    try:
        await asyncio.Future()
    finally:
        await asyncio.sleep(.1)
        (root/'cleaned').touch()
module['serve'].__globals__['supervise'] = fixture
asyncio.run(module['serve']())
"""
            child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 3
                while not (root/'ready').exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root/'ready').exists())
                child.send_signal(signal.SIGTERM)
                time.sleep(.02)
                child.send_signal(signal.SIGTERM)
                out, err = child.communicate(timeout=3)
                self.assertEqual(child.returncode, 0, out+err)
                self.assertTrue((root/'cleaned').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
