#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise game-facing capture behavior and actual arbiter forwarding code."""
import importlib.util
import json
import os
import socket
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('arbiter', ROOT / 'packaging/steamos/dosplex-gamepad-arbiter.py')
m = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'evdev': SimpleNamespace(InputDevice=Mock(), UInput=Mock(), ecodes=Mock(), list_devices=Mock())}):
    spec.loader.exec_module(m)


def event(kind, code, value):
    return SimpleNamespace(type=kind, code=code, value=value)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        settings = patch.object(m._menu_module, "shortcut_settings", return_value=("back_start", .65))
        settings.start()
        self.addCleanup(settings.stop)

    def test_overflow_resync_preserves_clone_and_waits_for_current_release(self):
        d=self.routed_device();original_ui=d.ui
        d.gate.capture(False);d.gate.finish_batch();d.gate.forward(event(1,305,1))
        d.ui.reset_mock();d.gate.forward(event(0,3,0))
        d.dev.info=SimpleNamespace(vendor=0x045e)
        d.dev.capabilities.return_value={3:[(0,SimpleNamespace(value=0,min=-32768,max=32767,flat=0))],1:[304,305]}
        d.dev.active_keys.return_value=[304]
        d.dev.read.side_effect=[[event(1,304,0)],BlockingIOError()]
        d.resynchronize()
        self.assertIs(d.ui,original_ui);d.ui.close.assert_not_called()
        d.ui.write.assert_any_call(1,305,0)
        self.assertFalse(d.gate.desynced)
        d.gate.capture(False);d.gate.finish_batch();self.assertTrue(d.gate.draining)
        d.gate.forward(event(1,304,0));d.gate.finish_batch()
        self.assertFalse(d.gate.draining)
        d.ui.reset_mock();press=event(1,304,1);d.gate.forward(press)
        d.ui.write_event.assert_called_once_with(press)

    def test_full_screen_temporarily_routes_to_main_without_erasing_assignment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'ROUTE_DIR', Path(tmp)):
            route = Path(tmp) / m.safe_name('test pad')
            route.write_text('guest\n')
            for layout, expected in [('vertical', 'guest'), ('single', 'main'), ('horizontal', 'guest'), ('guest', 'guest')]:
                with patch.object(m, 'runtime_state', return_value={'layout': layout}):
                    self.assertEqual(m.read_target('test pad'), expected)
                    self.assertEqual(route.read_text(), 'guest\n')

    def test_releases_held_game_controls_and_swallows_menu_until_neutral(self):
        ui = Mock()
        gate = m.GameInputGate(ui, {0: (0, 0, 100), 2: (0, 0, 0)})
        gate.capture(False)
        gate.finish_batch()
        gate.forward(event(1, 304, 1))
        gate.forward(event(3, 0, 12000))
        ui.reset_mock()
        gate.capture(True)
        self.assertEqual([c.args for c in ui.write.call_args_list], [(1, 304, 0), (3, 0, 0)])
        ui.syn.assert_called_once()
        gate.forward(event(1, 305, 1))
        gate.forward(event(3, 2, 255))
        gate.capture(False)
        for e in [event(1, 304, 0), event(1, 305, 0), event(3, 0, 0), event(3, 2, 0)]:
            gate.forward(e)
        ui.write_event.assert_not_called()
        gate.finish_batch()
        next_press = event(1, 304, 1)
        gate.forward(next_press)
        ui.write_event.assert_called_once_with(next_press)

    def test_startup_held_controls_do_not_appear_as_game_presses(self):
        ui = Mock()
        gate = m.GameInputGate(ui, {0: (12000, 0, 100)}, [304])
        ui.write.assert_called_once_with(3, 0, 0)
        gate.capture(False)
        gate.forward(event(1, 304, 0))
        gate.forward(event(3, 0, 0))
        ui.write_event.assert_not_called()
        gate.finish_batch()
        gate.forward(event(1, 304, 1))
        ui.write_event.assert_called_once()

    def test_lost_events_neutralize_and_cannot_reenable_old_route(self):
        ui = Mock()
        gate = m.GameInputGate(ui, {})
        gate.capture(False)
        gate.finish_batch()
        gate.forward(event(1, 304, 1))
        ui.reset_mock()
        gate.forward(event(0, 3, 0))
        gate.capture(False)
        gate.forward(event(1, 304, 0))
        gate.forward(event(0, 0, 0))
        gate.forward(event(1, 305, 1))
        ui.write.assert_called_once_with(1, 304, 0)
        ui.write_event.assert_not_called()
        self.assertTrue(gate.desynced)

    def test_queued_menu_tap_is_not_replayed_after_close(self):
        ui = Mock()
        gate = m.GameInputGate(ui, {})
        gate.capture(False)
        gate.finish_batch()
        gate.capture(True)
        gate.capture(False)
        gate.forward(event(1, 304, 1))
        gate.forward(event(1, 304, 0))
        gate.forward(event(0, 0, 0))
        ui.write_event.assert_not_called()
        gate.finish_batch()
        gate.forward(event(1, 305, 1))
        ui.write_event.assert_called_once()

    def test_actual_forward_callback_routes_through_capture_gate(self):
        device = m.RoutedGamepad.__new__(m.RoutedGamepad)
        device.dev = Mock()
        device.ui = Mock()
        device.target = 'guest'
        device.opening = None
        device.menu = None
        device.menu_side = None
        device.menu_visible = False
        device.menu_ready = False
        device.menu_ranges = {}
        device.send_menu = Mock()
        device.gate = m.GameInputGate(device.ui, {})
        device.record_metrics = Mock()
        device.dev.read.side_effect = [[event(1, 304, 1)], BlockingIOError()]
        with patch.object(m, 'overlay_state_for_target', return_value=('left', True, True)) as state:
            device.forward_ready()
        state.assert_called_once_with('guest')
        device.ui.write_event.assert_not_called()
        self.assertEqual(device.gate.keys, {304})

    def routed_device(self):
        device = m.RoutedGamepad.__new__(m.RoutedGamepad)
        device.dev, device.ui = Mock(), Mock()
        device.target = 'guest'
        device.opening = None
        device.menu = None
        device.menu_side = None
        device.menu_visible = device.menu_ready = False
        device.menu_ranges = {0: (-32768, 32767)}
        device.gate = m.GameInputGate(device.ui, {0: (0, 0, 100)})
        device.record_metrics = Mock()
        device.remove_reader = Mock()
        device.forward_error = None
        return device

    def feed(self, device, events):
        device.dev.read.side_effect = [events, BlockingIOError()]
        device.forward_ready()

    def test_real_menu_datagrams_and_game_capture_on_both_sides(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-menu-') as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(str(Path(directory) / 'hud-ipc.sock'))
                receiver.settimeout(.1)
                for side in ('left', 'right'):
                    device = self.routed_device()
                    with patch.object(m, 'STATE_PATH', Path(directory) / 'state.json'), patch.object(
                            m, 'overlay_state_for_target', return_value=(side, True, True)), patch.object(m.time, 'monotonic', return_value=10):
                        self.feed(device, [event(3, 0, 30000), event(1, 304, 1),
                                           event(1, 304, 0), event(1, 305, 1)])
                        for action in ('right', 'select', 'back'):
                            self.assertEqual(receiver.recv(128), f'MENU {side} {action}'.encode())
                        with patch.object(m.time, 'monotonic', return_value=10.5):
                            self.feed(device, [])
                        self.assertEqual(receiver.recv(128), f'MENU {side} right'.encode())
                        device.ui.write_event.assert_not_called()
                        with self.assertRaises(socket.timeout):
                            receiver.recv(128)

    def test_opening_with_held_axis_waits_for_neutral(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, True)):
            self.feed(device, [])
            self.feed(device, [event(3, 0, 30000)])
        device.ui.reset_mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', True, True)):
            self.feed(device, [event(1, 304, 1), event(1, 304, 0), event(3, 0, 0)])
            device.send_menu.assert_not_called()
            self.feed(device, [event(1, 304, 1)])
        device.send_menu.assert_called_once_with('select')
        device.ui.write_event.assert_not_called()
        device.ui.write.assert_called_once_with(3, 0, 0)

    def test_other_side_menu_leaves_game_route_active(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('right', False, True)):
            self.feed(device, [])
            press = event(1, 304, 1)
            self.feed(device, [press])
        device.ui.write_event.assert_called_once_with(press)
        device.send_menu.assert_not_called()

    def test_opening_chord_reaches_socket_without_game_buttons(self):
        device = self.routed_device()
        with tempfile.TemporaryDirectory(prefix='dosplex-opening-') as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(str(Path(directory) / 'hud-ipc.sock'))
                receiver.settimeout(.1)
                with patch.object(m, 'STATE_PATH', Path(directory) / 'state.json'), patch.object(
                        m, 'overlay_state_for_target', return_value=('right', False, True)) as state, patch.object(m.time, 'monotonic') as now:
                    now.return_value = 0
                    self.feed(device, [])
                    self.feed(device, [event(1, 314, 1)])
                    now.return_value = .1
                    self.feed(device, [event(1, 315, 1)])
                    now.return_value = .76
                    self.feed(device, [])
                    self.assertEqual(receiver.recv(128), b'MENU right toggle')
                    device.ui.write_event.assert_not_called()
                    # While waiting for compositor state, A must not leak.
                    self.feed(device, [event(1, 304, 1), event(1, 304, 0)])
                    device.ui.write_event.assert_not_called()
                    state.return_value = ('right', True, True)
                    self.feed(device, [event(1, 314, 0), event(1, 315, 0)])
                    self.feed(device, [event(1, 304, 1)])
                    self.assertEqual(receiver.recv(128), b'MENU right select')
                    device.ui.write_event.assert_not_called()

    def test_shortcut_single_buttons_and_late_pair_preserve_game_holds(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, True)), patch.object(m.time, 'monotonic') as now:
            now.return_value = 0
            self.feed(device, [])
            press = event(1, 315, 1)
            self.feed(device, [press])
            device.ui.write_event.assert_not_called()
            now.return_value = .19
            self.feed(device, [])
            device.ui.write_event.assert_called_once_with(press)
            second, release1, release2 = event(1, 314, 1), event(1, 315, 0), event(1, 314, 0)
            self.feed(device, [second])
            now.return_value = 2
            self.feed(device, [release1, release2])
            self.assertEqual([c.args[0] for c in device.ui.write_event.call_args_list], [press, second, release1, release2])
            device.send_menu.assert_not_called()
            device.ui.reset_mock()
            # A short individual tap is emitted as a paired press/release.
            now.return_value = 3
            self.feed(device, [press])
            now.return_value = 3.05
            self.feed(device, [release1])
            self.assertEqual([c.args[0] for c in device.ui.write_event.call_args_list], [press, release1])

    def test_interrupted_chord_is_not_replayed_or_rearmed_until_release(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, True)), patch.object(m.time, 'monotonic') as now:
            now.return_value = 0
            self.feed(device, [])
            self.feed(device, [event(1, 314, 1), event(1, 315, 1)])
            now.return_value = .2
            self.feed(device, [event(1, 315, 0)])
            now.return_value = 2
            self.feed(device, [event(1, 315, 1)])
            device.send_menu.assert_not_called()
            device.ui.write_event.assert_not_called()
            self.feed(device, [event(1, 314, 0), event(1, 315, 0)])
            self.feed(device, [])
            press = event(1, 304, 1)
            self.feed(device, [press])
            device.ui.write_event.assert_called_once_with(press)

    def test_other_owner_menu_does_not_start_an_opening_attempt(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, False)):
            self.feed(device, [])
            presses = [event(1, 314, 1), event(1, 315, 1)]
            self.feed(device, presses)
        device.send_menu.assert_not_called()
        self.assertEqual([c.args[0] for c in device.ui.write_event.call_args_list], presses)

    def test_opening_without_acknowledgment_fails_captured(self):
        device = self.routed_device()
        device.send_menu = Mock()
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, True)), patch.object(m.time, 'monotonic') as now:
            now.return_value = 0
            self.feed(device, [])
            self.feed(device, [event(1, 314, 1), event(1, 315, 1)])
            now.return_value = .7
            self.feed(device, [])
            device.send_menu.assert_called_once_with('toggle')
            now.return_value = 1.8
            with self.assertRaisesRegex(RuntimeError, 'did not acknowledge'):
                self.feed(device, [])
            self.assertTrue(device.gate.captured)
            device.ui.write_event.assert_not_called()

    def test_missing_menu_socket_stops_forwarding(self):
        device = self.routed_device()
        device.dev.read.side_effect = [[event(1, 304, 1)], BlockingIOError()]
        with tempfile.TemporaryDirectory(prefix='dosplex-missing-menu-') as directory:
            with patch.object(m, 'STATE_PATH', Path(directory) / 'state.json'), patch.object(
                    m, 'overlay_state_for_target', return_value=('left', True, True)):
                device.dispatch_ready()
        self.assertIsInstance(device.forward_error, OSError)
        device.remove_reader.assert_called_once()
        device.ui.write_event.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'controller forwarding failed'):
            asyncio.run(device.route_monitor())

    def test_callback_error_is_captured_and_reaches_monitor(self):
        device = self.routed_device()
        device.dev.read.side_effect = OSError('disconnected')
        with patch.object(m, 'overlay_state_for_target', return_value=('left', False, True)):
            device.dispatch_ready()
        self.assertIsInstance(device.forward_error, OSError)
        device.remove_reader.assert_called_once()
        self.assertTrue(device.gate.captured)
        with self.assertRaisesRegex(RuntimeError, 'controller forwarding failed'):
            asyncio.run(device.route_monitor())

    def test_state_owner_mapping_and_invalid_state_capture(self):
        state = dict(primary_side='right', guest_side='left', overlay_owner='left',
                     overlay_visible=True, output_connected=True)
        with tempfile.TemporaryDirectory(prefix='dosplex-capture-state-') as directory:
            path = Path(directory) / 'state.json'
            with patch.object(m, 'STATE_PATH', path):
                self.assertTrue(m.overlay_capture_required('main'))
                path.write_text(json.dumps(state))
                path.chmod(0o600)
                self.assertFalse(m.overlay_capture_required('main'))
                self.assertTrue(m.overlay_capture_required('guest'))
                self.assertTrue(m.overlay_capture_required('invalid'))
                state['overlay_visible'] = False
                path.write_text(json.dumps(state))
                path.chmod(0o600)
                self.assertFalse(m.overlay_capture_required('guest'))
                for raw in ('{', '[]', 'x'*65537, json.dumps(dict(state, overlay_visible='false')),
                            json.dumps(dict(state, output_connected=False))):
                    path.write_text(raw)
                    self.assertTrue(m.overlay_capture_required('guest'))
                path.write_text(json.dumps(state));path.chmod(0o666)
                self.assertTrue(m.overlay_capture_required('main'))
                path.unlink();os.mkfifo(path)
                self.assertTrue(m.overlay_capture_required('main'))
                path.unlink();path.symlink_to(Path(directory)/'missing')
                self.assertTrue(m.overlay_capture_required('main'))


class GenericControllerPolicyTests(unittest.TestCase):
    def test_native_identity_preserved_for_usb_and_bluetooth(self):
        for vendor,product in ((0x054c,0x0ce6),(0x054c,0x09cc),(0x057e,0x2009),(0x045e,0x02ea)):
            for bus in (3,5):
                info=SimpleNamespace(vendor=vendor,product=product,bustype=bus,version=256)
                identity=m.virtual_identity(info,'Dosplex routed fixture','fixture')
                self.assertEqual((identity['vendor'],identity['product'],identity['bustype'],identity['version']),(vendor,product,bus,256))
    def test_playstation_unsigned_sticks_and_triggers_have_distinct_neutral(self):
        info=SimpleNamespace(vendor=0x054c)
        axis=SimpleNamespace(min=0,max=255,flat=0)
        for code in (0,1,2,5):self.assertEqual(m.neutral_axis(info,code,axis),(128,25))
        for code in (3,4):self.assertEqual(m.neutral_axis(info,code,axis),(0,0))
    def test_xbox_axes_and_hat_neutral(self):
        info=SimpleNamespace(vendor=0x045e)
        for code in (0,1,3,4):self.assertEqual(m.neutral_axis(info,code,SimpleNamespace(min=-32768,max=32767,flat=0)),(0,6553))
        for code in (2,5):self.assertEqual(m.neutral_axis(info,code,SimpleNamespace(min=0,max=255,flat=0)),(0,0))
        self.assertEqual(m.neutral_axis(info,17,SimpleNamespace(min=-1,max=1,flat=0)),(0,0))


class SteamControllerMappingTests(unittest.TestCase):
    def test_sticks_triggers_and_dpad_use_xbox_axes(self):
        ui=Mock();out=m.SteamControllerOutput(ui)
        for e in (event(3,0,16000),event(3,1,-16000),event(3,3,9000),event(3,4,-9000),
                  event(3,20,12000),event(3,21,14000),event(1,544,1),event(1,547,1),
                  event(1,544,0),event(1,547,0)):
            out.write_event(e)
        self.assertEqual([c.args for c in ui.write.call_args_list],
            [(3,0,16000),(3,1,-16000),(3,3,9000),(3,4,-9000),(3,2,12000),(3,5,14000),
             (3,17,-1),(3,16,1),(3,17,0),(3,16,0)])
    def test_touchpads_and_touch_keys_do_not_become_game_controls(self):
        ui=Mock();out=m.SteamControllerOutput(ui)
        for e in (event(3,16,-25000),event(3,17,25000),event(3,18,20000),event(3,19,10000),event(1,289,1)):
            out.write_event(e)
        ui.write.assert_not_called()
    def test_capture_releases_translated_hat_and_trigger(self):
        ui=Mock();out=m.SteamControllerOutput(ui)
        gate=m.GameInputGate(out,{20:(0,0,0)})
        gate.capture(False);gate.finish_batch()
        gate.forward(event(1,544,1));gate.forward(event(3,20,12000));ui.reset_mock()
        gate.capture(True)
        self.assertEqual([c.args for c in ui.write.call_args_list],[(3,17,0),(3,2,0)])
    def test_advertised_layout_does_not_include_touchpad_axes_or_raw_dpad_keys(self):
        dev=Mock();dev.capabilities.return_value={3:[(code,(0,-32767,32767,0,0,0)) for code in (0,1,3,4,16,17,18,19,20,21)]}
        caps=m.SteamControllerOutput.capabilities(dev)
        self.assertEqual(sorted(c for c,_ in caps[3]),[0,1,2,3,4,5,16,17])
        self.assertNotIn(544,caps[1]);self.assertEqual(dict(caps[3])[17],(0,-1,1,0,0,0))


if __name__ == '__main__':
    unittest.main()
