#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Pulse short module lists may include multiline native PipeWire arguments."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
spec=importlib.util.spec_from_file_location('audio',Path(__file__).resolve().parents[1]/'packaging/steamos/dosplex-audio-router.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ModuleTests(unittest.TestCase):
    def test_native_multiline_and_pulse_ids(self):
        text='1\tlibpipewire-module-rt\t{\n nice.level = -11\n}\t\n536870918\tmodule-null-sink\tsink_name=dosplex_left\t\n'
        with patch.object(m,'run_pactl',return_value=SimpleNamespace(returncode=0,stdout=text)):
            modules=m.query('modules')
        self.assertEqual(len(modules),2)
        self.assertEqual(modules[1]['index'],536870918)
        self.assertEqual(m.module_args(modules[1])['sink_name'],'dosplex_left')
        self.assertIn('nice.level',modules[0]['argument'])
    def test_invalid_start_rejected(self):
        with patch.object(m,'run_pactl',return_value=SimpleNamespace(returncode=0,stdout='unrecognized\n')):
            with self.assertRaises(ValueError):m.query('modules')

if __name__=='__main__':unittest.main()
