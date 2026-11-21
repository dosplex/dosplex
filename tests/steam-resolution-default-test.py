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


CLIENT_OFF = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t}\n\t"Gamescope"\n\t{\n\t\t"HDREnabled"\n\t\t{\n\t\t}\n' \
             '\t\t"EnableSteamNativeExternalResolution"\t\t"0"\n\t}\n}\n'
CLIENT_WITHOUT_KEY = '"InstallConfigStore"\n{\n\t"Gamescope"\n\t{\n\t\t"HDREnabled"\n\t\t{\n\t\t}\n\t}\n}\n'
CLIENT_WITHOUT_BLOCK = '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t}\n}\n'
ON = '"EnableSteamNativeExternalResolution"\t\t"1"'


class NativeOnExternalDisplays(unittest.TestCase):
    """The client's own switch, without which a newer Steam puts a tall side back to 1920x1080."""

    def test_off_becomes_on_and_a_missing_key_or_block_is_added(self):
        for text in (CLIENT_OFF, CLIENT_WITHOUT_KEY, CLIENT_WITHOUT_BLOCK):
            changed = helper.apply_external(text)
            self.assertEqual(changed.count(ON), 1, changed)
            self.assertEqual(changed.count('{'), changed.count('}'))
            self.assertEqual(changed.count('"Gamescope"'), 1)
            self.assertIsNone(helper.apply_external(changed), 'a second pass has nothing to do')
        self.assertIn('"HDREnabled"', helper.apply_external(CLIENT_WITHOUT_KEY))

    def test_text_it_does_not_understand_is_left_alone(self):
        self.assertIsNone(helper.apply_external('not a config file'))
        self.assertIsNone(helper.apply_external('"UserLocalConfigStore"\n{\n}\n'))

    def run_helper(self, steam, markers):
        return subprocess.run([sys.executable, '-I', str(HELPER), str(steam), str(markers)], capture_output=True,
                              text=True, timeout=30)

    def test_it_is_done_once_and_a_player_who_switches_it_off_keeps_that(self):
        with tempfile.TemporaryDirectory() as directory:
            steam, markers = Path(directory) / 'Steam', Path(directory) / 'dosplex'
            config = steam / 'config/config.vdf'
            config.parent.mkdir(parents=True)
            config.write_text(CLIENT_WITHOUT_KEY)
            os.chmod(config, 0o600)
            result = self.run_helper(steam, markers)             # (no userdata folder: the client setting is done all the same)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(ON, config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(os.listdir(config.parent)), ['config.vdf'])
            config.write_text(CLIENT_OFF)                        # the player switches it off in Steam
            self.run_helper(steam, markers)
            self.assertIn('"0"', config.read_text())

    def test_a_steam_that_has_never_run_is_left_for_the_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            steam, markers = Path(directory) / 'Steam', Path(directory) / 'dosplex'
            steam.mkdir()
            self.assertEqual(self.run_helper(steam, markers).returncode, 0)
            self.assertEqual(os.listdir(markers), [])
            (steam / 'config').mkdir()
            (steam / 'config/config.vdf').write_text('something else')
            self.run_helper(steam, markers)
            self.assertEqual(os.listdir(markers), [], 'no marker for a file that was not understood')
            (steam / 'config/config.vdf').write_text(CLIENT_WITHOUT_BLOCK)
            self.run_helper(steam, markers)
            self.assertIn(ON, (steam / 'config/config.vdf').read_text())
            self.assertEqual(os.listdir(markers), ['native-external-resolution'])


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
