#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("display_state", ROOT / "packaging/steamos/dosplex-display-state.py")
display_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(display_state)


class DisplayState(unittest.TestCase):
    def read(self, entry, layout="vertical", reader=display_state.read_configuration, **state):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"layout": layout, "left": entry, **state}))
            return reader(path, "left")

    def contract(self, entry, layout="vertical", **state):
        return self.read(entry, layout, display_state.read_contract, **state)

    def test_committed_configuration_only(self):
        self.assertEqual(self.read({"width": 960, "height": 1080, "game_width": 1280,
                                    "game_height": 720, "scale_mode": "fit", "display_revision": 4,
                                    "pending_game_width": 1920, "pending_game_height": 1080,
                                    "pending_scale_mode": "stretch"}),
                         (960, 1080, 1280, 720, "fit", 4, "vertical"))

    def test_zoom_is_a_valid_presentation_mode(self):
        self.assertEqual(self.read({"width": 960, "height": 1080, "scale_mode": "zoom", "display_revision": 2}),
                         (960, 1080, 960, 1080, "zoom", 2, "vertical"))
        self.assertEqual(self.contract({"width": 960, "height": 1080, "scale_mode": "zoom"})["live"]["scale_mode"], "zoom")

    def test_native_defaults(self):
        self.assertEqual(self.read({"width": 640, "height": 720}),
                         (640, 720, 640, 720, "fit", 0, "vertical"))

    def test_bad_state_does_not_become_a_shutdown_request(self):
        for field, value in [("width", 0), ("height", True), ("game_width", 999999),
                             ("game_height", "720"), ("scale_mode", "unknown"),
                             ("display_revision", -1)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.read({"width": 960, "height": 1080, field: value})
        with self.assertRaises(ValueError):
            self.read({"width": 960, "height": 1080}, "unknown")
        with self.assertRaises(FileNotFoundError):
            display_state.read_configuration("/does-not-exist/dosplex-state.json", "left")

    def test_contract_without_a_version_is_version_one_with_the_live_fields(self):
        self.assertEqual(self.contract({"x": 960, "y": 0, "width": 960, "height": 1080, "game_width": 1280,
                                        "game_height": 720, "scale_mode": "stretch", "display_revision": 4,
                                        "pending_game_width": 1920, "pending_game_height": 1080,
                                        "pending_scale_mode": "fit"}),
                         {"version": 1, "live": {"x": 960, "y": 0, "width": 960, "height": 1080,
                                                 "game_width": 1280, "game_height": 720,
                                                 "scale_mode": "stretch", "display_revision": 4,
                                                 "layout": "vertical"}})

    def test_contract_defaults_match_the_native_defaults(self):
        self.assertEqual(self.contract({"width": 640, "height": 720}, "single"),
                         {"version": 1, "live": {"x": 0, "y": 0, "width": 640, "height": 720,
                                                 "game_width": 640, "game_height": 720,
                                                 "scale_mode": "fit", "display_revision": 0,
                                                 "layout": "single"}})

    def test_guest_full_screen_is_a_valid_layout(self):
        entry = {"width": 1920, "height": 1080}
        for reader in (display_state.read_configuration, display_state.read_contract):
            self.read(entry, "guest", reader)
        self.assertEqual(self.contract(entry, "guest")["live"]["layout"], "guest")
        with self.assertRaises(ValueError):
            self.contract(entry, "sideways")

    def test_contract_version_two_is_declared_by_the_integer_two_only(self):
        entry = {"width": 960, "height": 1080}
        self.assertEqual(self.contract(entry, contract_version=2)["version"], 2)
        for declared in (1, 0, 3, -2, 20, "2", 2.0, True, None, [2], {"version": 2}):
            with self.subTest(declared=declared):
                self.assertEqual(self.contract(entry, contract_version=declared)["version"], 1)

    def test_contract_and_tuple_read_the_same_committed_state_under_either_version(self):
        entry = {"x": 0, "y": 540, "width": 1920, "height": 540, "game_width": 1280, "game_height": 720,
                 "scale_mode": "stretch", "display_revision": 9, "pending_game_width": 640}
        for state in ({}, {"contract_version": 2}):
            with self.subTest(state=state):
                live = self.contract(entry, "horizontal", **state)["live"]
                self.assertEqual(self.read(entry, "horizontal", **state),
                                 (live["width"], live["height"], live["game_width"], live["game_height"],
                                  live["scale_mode"], live["display_revision"], live["layout"]))

    def test_the_monitor_keys_that_the_compositor_publishes_are_not_part_of_the_contract(self):
        entry = {"x": 960, "y": 0, "width": 960, "height": 1080, "game_width": 1920, "game_height": 1080,
                 "scale_mode": "stretch", "display_revision": 3}
        plain = self.contract(entry, contract_version=2)
        for extra in (dict(monitor_width=3840, monitor_height=2160, display_scaled=True, display_result="deferred"),
                      dict(monitor_width=0, monitor_height=0, display_scaled=False, display_result=""),
                      dict(monitor_width=1920, monitor_height=1080, display_scaled=False, display_result="applied"),
                      dict(monitor_width=-1, monitor_height="x", display_scaled=None, display_result=7)):
            with self.subTest(extra=extra):
                self.assertEqual(self.contract(dict(entry, **extra), contract_version=2), plain)
                self.assertEqual(self.read(dict(entry, **extra), contract_version=2),
                                 self.read(entry, contract_version=2))

    def test_bad_contract_is_rejected_like_the_tuple_and_positions_are_checked(self):
        for field, value in [("width", 0), ("height", True), ("game_width", 999999), ("game_height", "720"),
                             ("scale_mode", "unknown"), ("display_revision", -1),
                             ("x", 99999), ("y", -99999), ("x", "0"), ("y", True), ("x", 1.5), ("y", None)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.contract({"width": 960, "height": 1080, field: value}, contract_version=2)
        with self.assertRaises(ValueError):
            self.contract({"width": 960, "height": 1080}, "unknown")
        with self.assertRaises(ValueError):
            display_state.read_contract("/does-not-exist/dosplex-state.json", "unknown")
        with self.assertRaises(FileNotFoundError):
            display_state.read_contract("/does-not-exist/dosplex-state.json", "left")


if __name__ == "__main__":
    unittest.main()
