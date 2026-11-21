#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Deterministic controller menu behavior; no evdev devices or grabs."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('controller_menu', Path(__file__).resolve().parents[1] / 'packaging/steamos/dosplex-controller-menu.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.visible = False
        self.menu = m.ControllerMenu(self.actions.append, lambda: self.visible, {0: (-32768, 32767), 1: (-32768, 32767)}, shortcut="back_start", hold_seconds=.65)

    def event(self, kind, code, value, now):
        self.menu.event(kind, code, value, now)

    def test_hold_requires_both_buttons_and_full_release(self):
        self.event(1, m.SELECT, 1, 0)
        self.menu.tick(3)
        self.assertEqual(self.actions, [])
        self.event(1, m.START, 1, 3)
        self.menu.tick(3.6)
        self.assertEqual(self.actions, [])
        self.menu.tick(3.7)
        self.menu.tick(10)
        self.assertEqual(self.actions, ['toggle'])
        self.event(1, m.START, 0, 11)
        self.event(1, m.START, 1, 12)
        self.menu.tick(13)
        self.assertEqual(self.actions, ['toggle'])
        self.event(1, m.START, 0, 14)
        self.event(1, m.SELECT, 0, 14)
        self.event(1, m.START, 1, 15)
        self.event(1, m.SELECT, 1, 15)
        self.menu.tick(16)
        self.assertEqual(self.actions, ['toggle', 'toggle'])

    def test_quick_chord_does_not_toggle(self):
        self.event(1, m.START, 1, 0)
        self.event(1, m.SELECT, 1, .1)
        self.event(1, m.SELECT, 0, .2)
        self.menu.tick(1)
        self.assertEqual(self.actions, [])

    def test_actions_require_owned_visible_menu(self):
        for code in (m.A, m.B, m.LB, m.RB):
            self.event(1, code, 1, 0)
        self.assertEqual(self.actions, [])
        self.visible = True
        for code in (m.A, m.B, m.LB, m.RB):
            self.event(1, code, 1, 1)
            self.event(1, code, 2, 1)  # kernel repeats never double activation
            self.event(1, code, 0, 1)
        self.assertEqual(self.actions, ['select', 'back', 'previous', 'next'])

    def test_stick_deadzone_hat_precedence_repeat_and_stop(self):
        self.visible = True
        self.event(3, m.ABS_X, 1000, 0)
        self.assertEqual(self.actions, [])
        self.event(3, m.ABS_X, 30000, .1)
        self.menu.tick(.2)
        self.menu.tick(.51)
        self.event(3, m.HAT_Y, -1, .6)
        self.assertEqual(self.actions, ['right', 'right', 'up'])
        self.event(3, m.ABS_X, 0, .7)
        self.event(3, m.HAT_Y, 0, .8)
        self.menu.tick(5)
        self.assertEqual(self.actions, ['right', 'right', 'up'])
        self.visible = False
        self.event(3, m.HAT_X, 1, 6)
        self.menu.tick(9)
        self.assertEqual(self.actions, ['right', 'right', 'up'])

    def test_dropped_events_clear_chord_and_repeat(self):
        self.visible = True
        self.event(1, m.START, 1, 0)
        self.event(1, m.SELECT, 1, .1)
        self.event(3, m.HAT_X, 1, .2)
        self.event(0, 3, 0, .3)
        self.event(1, m.A, 1, .4)
        self.menu.tick(5)
        self.assertEqual(self.actions, [])  # chord suppresses navigation
        self.event(0, 0, 0, 6)
        self.event(1, m.A, 1, 7)
        self.assertEqual(self.actions, ['select'])


if __name__ == '__main__':
    unittest.main()
