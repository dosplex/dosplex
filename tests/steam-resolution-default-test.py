#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""guest/dosplex-steam-resolution-default.py: Steam's Maximum Game Resolution starts at Native, once per account.

Without a choice Steam gave every game its "Recommended" size in a side, and Quake, whose saved mode was the side's
size, quit (Steam Machine, 2026-11-18). A choice the player made in Steam is kept, and so is any later change.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / 'guest' / 'dosplex-steam-resolution-default.py'
spec = importlib.util.spec_from_file_location('resolution_default', HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

WITH_DEFAULT = '"UserLocalConfigStore"\n{\n\t"Gamescope"\n\t{\n\t\t"AllowTearing"\n\t\t{\n\t\t\t"1"\t\t"0"\n\t\t}\n' \
               '\t\t"GameResolutionGlobal"\t\t"Default"\n\t}\n\t"cache"\n\t{\n\t}\n}\n'
WITHOUT_KEY = '"UserLocalConfigStore"\n{\n\t"Gamescope"\n\t{\n\t\t"AllowTearing"\n\t\t{\n\t\t}\n\t}\n}\n'
WITHOUT_BLOCK = '"UserLocalConfigStore"\n{\n\t"friends"\n\t{\n\t}\n}\n'


class Apply(unittest.TestCase):
    def test_default_becomes_native(self):
        self.assertIn('"GameResolutionGlobal"\t\t"Native"', helper.apply(WITH_DEFAULT))
        self.assertEqual(helper.apply(WITH_DEFAULT).replace('Native', 'Default'), WITH_DEFAULT)

    def test_a_missing_key_is_added_to_the_gamescope_block(self):
        changed = helper.apply(WITHOUT_KEY)
        self.assertIn('\t"Gamescope"\n\t{\n\t\t"GameResolutionGlobal"\t\t"Native"\n', changed)

    def test_a_missing_block_is_added_at_the_top(self):
        changed = helper.apply(WITHOUT_BLOCK)
        self.assertTrue(changed.startswith('"UserLocalConfigStore"\n{\n\t"Gamescope"\n\t{\n\t\t"GameResolutionGlobal"'))

    def test_a_choice_of_the_player_is_kept(self):
        for value in ('Native', '1280x720', '3840x2160'):
            self.assertIsNone(helper.apply(WITH_DEFAULT.replace('"Default"', '"%s"' % value)))

    def test_text_it_does_not_understand_is_left_alone(self):
        self.assertIsNone(helper.apply('not a config\n'))


class Once(unittest.TestCase):
    def run_helper(self, steam, markers):
        return subprocess.run([sys.executable, '-I', str(HELPER), str(steam), str(markers)], capture_output=True,
                              text=True, timeout=30)

    def test_it_is_done_once_per_account_and_a_later_default_stays(self):
        with tempfile.TemporaryDirectory() as directory:
            steam, markers = Path(directory) / 'Steam', Path(directory) / 'dosplex'
            config = steam / 'userdata/123/config/localconfig.vdf'
            config.parent.mkdir(parents=True)
            config.write_text(WITH_DEFAULT)
            os.chmod(config, 0o600)
            (steam / 'userdata/0').mkdir()
            self.assertEqual(self.run_helper(steam, markers).returncode, 0)
            self.assertIn('"Native"', config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(os.listdir(config.parent)), ['localconfig.vdf'])
            config.write_text(WITH_DEFAULT)                      # the player picks Recommended again
            self.run_helper(steam, markers)
            self.assertIn('"Default"', config.read_text())

    def test_no_steam_folder_is_no_error(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_helper(Path(directory) / 'missing', Path(directory) / 'dosplex').returncode, 0)


if __name__ == '__main__':
    unittest.main()
