#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Check actual C and HUD grouping with external Steam Controller/Puck names."""
import ast
import ctypes
import glob
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ClassificationTests(unittest.TestCase):
    def test_compositor_classification_does_not_hide_external_steam_controllers(self):
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        names = ['input_name_contains_ci', 'is_deck_onboard_input_name', 'is_external_gamepad_input_name']
        functions = []
        for name in names:
            start = source.index('static bool ' + name + '(')
            end = source.index('\n}', start) + 2
            functions.append(source[start:end])
        with tempfile.TemporaryDirectory(prefix='dosplex-classification-') as directory:
            root = Path(directory)
            (root / 'policy.c').write_text('#include <stdbool.h>\n#include <string.h>\n#include <ctype.h>\n' +
                '\n'.join(functions) + '\nint deck(const char *n) {return is_deck_onboard_input_name(n);}\n'
                'int external(const char *n) {return is_external_gamepad_input_name(n);}\n')
            subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Werror', str(root / 'policy.c'),
                            '-o', str(root / 'policy.so')], check=True, timeout=15)
            policy = ctypes.CDLL(str(root / 'policy.so'))
            for function in (policy.deck, policy.external):
                function.argtypes = [ctypes.c_char_p]
                function.restype = ctypes.c_int
            for name in ('Valve Software Steam Controller', 'Valve Software Steam Controller Puck',
                         'Wireless Controller', 'DualSense Wireless Controller', 'Xbox Gamepad'):
                self.assertFalse(policy.deck(name.encode()), name)
                self.assertTrue(policy.external(name.encode()), name)
            for name in ('Steam Deck', 'Steam Deck Touchpad', 'steam deck'):
                self.assertTrue(policy.deck(name.encode()), name)
                self.assertFalse(policy.external(name.encode()), name)
            self.assertFalse(policy.deck(None))
            self.assertFalse(policy.external(None))

    def test_hud_keeps_external_rows_separate_from_deck_controls(self):
        # Load actual pure HUD functions without starting Qt or requiring bindings.
        tree = ast.parse((ROOT / 'packaging/steamos/dosplex-device-hud.py').read_text())
        names = {'is_deck_onboard_name', 'is_builtin_touchscreen_name', 'is_internal_input_name',
                 'friendly_input_name', 'input_icon', 'get_input_devices'}
        module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
        namespace = {'glob': glob}
        exec(compile(module, '<actual HUD functions>', 'exec'), namespace)
        with tempfile.TemporaryDirectory(prefix='dosplex-hud-inputs-') as directory:
            paths = []
            for i, name in enumerate(('Steam Deck', 'Steam Deck Touchpad',
                    'Valve Software Steam Controller', 'Valve Software Steam Controller Puck')):
                path = Path(directory) / str(i)
                path.write_text(name)
                paths.append(str(path))
            with patch.object(glob, 'glob', return_value=paths):
                rows = namespace['get_input_devices']({'__dosplex_deck_controls__': 'left',
                                                       'Valve Software Steam Controller': 'right'})
            rows = {row['deviceId']: row for row in rows}
            self.assertEqual(rows['__dosplex_deck_controls__']['name'], 'Deck onboard controls')
            for name in ('Valve Software Steam Controller', 'Valve Software Steam Controller Puck'):
                self.assertEqual(rows[name]['name'], name)
                self.assertEqual(rows[name]['icon'], 'gamepad')
            self.assertEqual(rows['Valve Software Steam Controller']['side'], 'right')
            self.assertEqual(len(rows), 4)  # Deck, two external models, Input Leap.


if __name__ == '__main__':
    unittest.main()
