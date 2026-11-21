#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The helper of a side's nested desktop (packaging/steamos/dosplex-desktop-environment.py), apart from the launcher.

  - the scale decision: the default for each kind of side size, the player's desktop-scale preference and
    what becomes of a preference that is not valid, as functions and as the command the launcher runs;
  - the edits of the settings that the launcher generates: they create a missing file, keep every other key,
    group and line of the player's file, and change nothing a second time.

tests/desktop-profile-test.py runs the launcher's own shell functions with this helper.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'packaging/steamos/dosplex-desktop-environment.py'
BASH = shutil.which('bash')

spec = importlib.util.spec_from_file_location('desktop', HELPER)
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)

KWINRC = ('[Compositing]\nBackend=OpenGL\n\n'
          '[Xwayland]\nScale=2\nXwaylandEisNoPrompt=false\n\n'
          '[Windows]\nBorderSnapZone=10\n')


class ScaleDecision(unittest.TestCase):
    def test_the_scales_a_player_may_choose(self):
        self.assertEqual(desktop.SCALES, ('1', '1.25', '1.5', '1.75', '2', '2.5', '3'))

    def test_the_default_follows_the_size_of_the_side(self):
        sizes = {
            (3840, 2160): '2',      # 4K, the whole screen
            (1920, 2160): '2',      # 4K, a side-by-side half
            (3840, 1080): '2',      # 4K, a top or bottom half
            (2160, 2160): '2',
            (1920, 1080): '1.5',    # 1080p, the whole screen
            (960, 1080): '1.5',     # 1080p, a side-by-side half
            (2560, 1440): '1.5',
            (1920, 540): '1',       # 1080p, a top or bottom half
            (1280, 800): '1',       # the Deck's own screen
            (1280, 720): '1',
            (640, 800): '1',
        }
        for (width, height), scale in sizes.items():
            with self.subTest(width=width, height=height):
                self.assertEqual(desktop.choose_scale(width, height),
                                 (scale, 'default for %dx%d' % (width, height)))

    def test_the_limits_between_the_defaults(self):
        limits = {
            (960, 1599): '1.5', (960, 1600): '2',            # 1600 pixels tall
            (2999, 999): '1', (3000, 999): '2',              # 3000 pixels wide
            (2999, 1000): '1.5', (1920, 999): '1', (1920, 1000): '1.5',    # 1000 pixels tall
        }
        for (width, height), scale in limits.items():
            with self.subTest(width=width, height=height):
                self.assertEqual(desktop.choose_scale(width, height)[0], scale)

    def test_the_size_may_come_as_text_from_the_environment(self):
        self.assertEqual(desktop.choose_scale('1920', '2160'), ('2', 'default for 1920x2160'))
        self.assertEqual(desktop.choose_scale(' 960 ', '1080\n')[0], '1.5')

    def test_a_valid_preference_wins_whatever_the_size(self):
        for size in ((3840, 2160), (1920, 1080), (1280, 800), (None, None)):
            for scale in desktop.SCALES:
                for text in (scale, scale + '\n', ' ' + scale + ' \r\n', '\t' + scale):
                    with self.subTest(size=size, text=text):
                        chosen, reason = desktop.choose_scale(*size, preference=text)
                        self.assertEqual(chosen, scale)
                        self.assertIn('desktop-scale preference', reason)
                        self.assertNotIn('ignored', reason)

    def test_anything_else_in_the_preference_falls_back_to_the_default(self):
        invalid = ['', ' ', '\n', '0', '0.5', '1.1', '1.3', '4', '10', '2.0', '1.50', '02', '+2', '-2', '2x', '200',
                   '200%', '1,5', '1.5\n2', '2 3', '1.5.', 'two', 'auto', '٢', '２', '1.5\x00', '1e0',
                   'a' * 200]
        for text in invalid:
            with self.subTest(text=text):
                scale, reason = desktop.choose_scale(1920, 2160, preference=text)
                self.assertEqual(scale, '2')
                self.assertIn('ignored desktop-scale preference', reason)
                self.assertIn('default for 1920x2160', reason)
                self.assertEqual(reason.splitlines(), [reason])        # one line of printable text
                self.assertTrue(reason.isprintable() and reason.isascii())
                self.assertLess(len(reason), 120)

    def test_an_unknown_size_gets_scale_one_and_a_valid_preference_still_applies(self):
        unknown = [(None, None), ('', ''), ('abc', '1080'), ('1920', 'abc'), ('0', '0'), ('-1', '800'),
                   ('99999', '99999'), ('16385', '1080'), ('1920.5', '1080'), (None, 2160), (1920, None),
                   ('1920 1080', '1080'), ('١٩٢٠', '1080')]
        for width, height in unknown:
            with self.subTest(width=width, height=height):
                self.assertEqual(desktop.choose_scale(width, height), ('1', 'default for an unknown output size'))
                self.assertEqual(desktop.choose_scale(width, height, '1.75')[0], '1.75')
                self.assertEqual(desktop.choose_scale(width, height, 'nonsense')[0], '1')

    def test_the_preference_file(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-scale-file-') as directory:
            root = Path(directory)
            self.assertIsNone(desktop.read_preference(root / 'missing'))
            (root / 'good').write_text('1.5\n')
            self.assertEqual(desktop.read_preference(root / 'good'), '1.5\n')
            (root / 'empty').write_text('')
            self.assertEqual(desktop.read_preference(root / 'empty'), '')
            (root / 'binary').write_bytes(b'\xff\xfe\x00\x01' * 40)
            self.assertEqual(desktop.choose_scale(1920, 2160, desktop.read_preference(root / 'binary'))[0], '2')
            (root / 'long').write_text('2' + ' ' * 5000)           # a valid value, but not all of the file
            self.assertEqual(desktop.read_preference(root / 'long'), '<more than 64 bytes>')
            self.assertEqual(desktop.choose_scale(1920, 1080, desktop.read_preference(root / 'long'))[0], '1.5')
            (root / 'limit').write_text('2' + ' ' * 63)             # exactly 64 bytes
            self.assertEqual(desktop.choose_scale(1920, 1080, desktop.read_preference(root / 'limit'))[0], '2')
            (root / 'directory').mkdir()
            unreadable = desktop.read_preference(root / 'directory')
            self.assertTrue(unreadable.startswith('<unreadable'), unreadable)
            self.assertEqual(desktop.choose_scale(1920, 1080, unreadable)[0], '1.5')
            os.mkfifo(root / 'pipe')                       # nobody writes to it: reading must not wait
            self.assertEqual(desktop.read_preference(root / 'pipe'), '')

    def test_the_command_prints_one_line_for_the_launcher(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-scale-command-') as directory:
            preference = Path(directory) / 'desktop-scale'

            def run(*arguments):
                return subprocess.run([sys.executable, str(HELPER), *arguments], capture_output=True, text=True,
                                      timeout=30)
            done = run('scale', '1920', '2160', str(preference))
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, '2 default for 1920x2160\n', ''))
            preference.write_text('1.25\n')
            done = run('scale', '1920', '2160', str(preference))
            self.assertEqual((done.returncode, done.stdout),
                             (0, '1.25 desktop-scale preference (default would be 2)\n'))
            preference.write_text('big')
            done = run('scale', '', '', str(preference))
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout,
                             "1 ignored desktop-scale preference 'big', default for an unknown output size\n")
            for arguments in ((), ('scale',), ('scale', '1', '2'), ('set-key', 'file'), ('nonsense',), ('12x',)):
                done = run(*arguments)
                self.assertEqual((done.returncode, done.stdout), (2, ''), arguments)
                self.assertIn('usage:', done.stderr)


class SettingsEdits(unittest.TestCase):
    def test_a_missing_file_gets_just_the_generated_key(self):
        self.assertEqual(desktop.merge_ini_key('', 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         '[Xwayland]\nXwaylandEisNoPrompt=true\n')
        self.assertEqual(desktop.merge_ini_key('', 'General', 'systemdBoot', 'false'),
                         '[General]\nsystemdBoot=false\n')

    def test_the_players_other_keys_and_groups_stay(self):
        self.assertEqual(desktop.merge_ini_key(KWINRC, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         KWINRC.replace('XwaylandEisNoPrompt=false', 'XwaylandEisNoPrompt=true'))

    def test_a_key_the_group_lacks_is_added_at_the_end_of_the_group(self):
        text = '[Xwayland]\nScale=2\n\n[Windows]\nBorderSnapZone=10\n'
        self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         '[Xwayland]\nScale=2\nXwaylandEisNoPrompt=true\n\n[Windows]\nBorderSnapZone=10\n')
        self.assertEqual(desktop.merge_ini_key('[Xwayland]\n', 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         '[Xwayland]\nXwaylandEisNoPrompt=true\n')
        self.assertEqual(desktop.merge_ini_key('[Xwayland]\n\n[Windows]\nA=1\n', 'Xwayland', 'K', 'v'),
                         '[Xwayland]\nK=v\n\n[Windows]\nA=1\n')

    def test_a_group_the_file_lacks_is_appended(self):
        expected = '[Windows]\nBorderSnapZone=10\n\n[Xwayland]\nXwaylandEisNoPrompt=true\n'
        for text in ('[Windows]\nBorderSnapZone=10\n', '[Windows]\nBorderSnapZone=10'):
            with self.subTest(text=text):
                self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'), expected)

    def test_comments_and_lookalike_groups_are_not_the_key(self):
        text = ('# XwaylandEisNoPrompt=false\n[Xwayland][Sub]\nXwaylandEisNoPrompt=false\n'
                '[XwaylandX]\nXwaylandEisNoPrompt=false\n[Xwayland]\n#XwaylandEisNoPrompt=false\n')
        self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         text + 'XwaylandEisNoPrompt=true\n')

    def test_a_key_that_merely_contains_the_name_is_another_key(self):
        text = '[Xwayland]\nXwaylandEisNoPromptToo=1\nNotXwaylandEisNoPrompt=2\n XwaylandEisNoPrompt = false\n'
        self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         '[Xwayland]\nXwaylandEisNoPromptToo=1\nNotXwaylandEisNoPrompt=2\nXwaylandEisNoPrompt=true\n')
        text = '[Xwayland]\nXwaylandEisNoPromptToo=1\nNotXwaylandEisNoPrompt=2\n'
        self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         text + 'XwaylandEisNoPrompt=true\n')

    def test_the_result_ends_with_a_newline_whether_or_not_the_file_did(self):
        self.assertEqual(desktop.merge_ini_key('[Xwayland]\nScale=2', 'Xwayland', 'K', 'v'),
                         '[Xwayland]\nScale=2\nK=v\n')
        self.assertEqual(desktop.merge_ini_key('[Xwayland]\nK=old', 'Xwayland', 'K', 'v'), '[Xwayland]\nK=v\n')
        self.assertEqual(desktop.merge_assignment('A="1"\nB="2"', 'B', '3'), 'A="1"\nB="3"\n')

    def test_a_key_that_appears_in_a_repeated_group_is_set_everywhere(self):
        text = '[Xwayland]\nXwaylandEisNoPrompt=false\n[Windows]\nA=1\n[Xwayland]\nXwaylandEisNoPrompt=false\n'
        self.assertEqual(desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true'),
                         text.replace('false', 'true'))

    def test_the_edit_changes_nothing_a_second_time(self):
        for text in ('', KWINRC, '[Windows]\nA=1', '[Xwayland]\n', '# note\n\n[Xwayland]\nZ=1\n\n\n'):
            with self.subTest(text=text):
                once = desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true')
                self.assertEqual(desktop.merge_ini_key(once, 'Xwayland', 'XwaylandEisNoPrompt', 'true'), once)

                def others(value):
                    return [line for line in value.split('\n') if line and not line.startswith('XwaylandEisNoPrompt=')]
                # Every other line is still there, in its place; a missing group is the only addition.
                self.assertEqual(others(once), others(text) + ([] if '[Xwayland]' in text else ['[Xwayland]']))

    def test_names_and_values_that_would_break_the_file_are_refused(self):
        for group, key, value in (('', 'k', 'v'), ('a]b', 'k', 'v'), ('[a', 'k', 'v'), ('g', '', 'v'),
                                  ('g', 'a=b', 'v'), ('g', '#k', 'v'), ('g', 'k\nx', 'v'), ('g', 'k', 'v\nw=1'),
                                  ('g', 'k', 'v\0')):
            with self.subTest(group=group, key=key, value=value), self.assertRaises(ValueError):
                desktop.merge_ini_key('', group, key, value)

    def test_the_assignment_is_added_or_replaced_and_other_lines_stay(self):
        self.assertEqual(desktop.merge_assignment('', 'XDG_DESKTOP_DIR', '/h/data/Desktop'),
                         'XDG_DESKTOP_DIR="/h/data/Desktop"\n')
        text = '# comment\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_DESKTOP_DIR="/old"\nXDG_MUSIC_DIR="$HOME/Music"\n'
        expected = text.replace('"/old"', '"/h/data/Desktop"')
        self.assertEqual(desktop.merge_assignment(text, 'XDG_DESKTOP_DIR', '/h/data/Desktop'), expected)
        self.assertEqual(desktop.merge_assignment(expected, 'XDG_DESKTOP_DIR', '/h/data/Desktop'), expected)
        self.assertEqual(desktop.merge_assignment('XDG_MUSIC_DIR="m"', 'XDG_DESKTOP_DIR', '/d'),
                         'XDG_MUSIC_DIR="m"\nXDG_DESKTOP_DIR="/d"\n')
        commented = '# XDG_DESKTOP_DIR="/old"\n'
        self.assertEqual(desktop.merge_assignment(commented, 'XDG_DESKTOP_DIR', '/d'),
                         commented + 'XDG_DESKTOP_DIR="/d"\n')

    def test_an_assignment_reads_back_as_the_value_it_was_given(self):
        value = 'a "quoted" $HOME `date` back\\slash path'
        line = desktop.merge_assignment('', 'XDG_DESKTOP_DIR', value)
        read = subprocess.run([BASH, '-c', 'eval "$1"; printf %s "$XDG_DESKTOP_DIR"', 'bash', line],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(read.stdout, value)
        for name, bad in (('1X', 'v'), ('A-B', 'v'), ('', 'v'), ('A', 'v\nB=1')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                desktop.merge_assignment('', name, bad)

    def test_update_file_writes_in_one_step_and_only_when_something_changes(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-update-file-') as directory:
            root = Path(directory)
            path = root / 'kwinrc'

            def change(text):
                return desktop.merge_ini_key(text, 'Xwayland', 'XwaylandEisNoPrompt', 'true')
            self.assertTrue(desktop.update_file(path, change))
            self.assertEqual(path.read_text(), '[Xwayland]\nXwaylandEisNoPrompt=true\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            before = path.stat()
            self.assertFalse(desktop.update_file(path, change))
            after = path.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
            path.write_text(KWINRC)
            path.chmod(0o640)
            self.assertTrue(desktop.update_file(path, change))
            self.assertEqual(path.read_text(), KWINRC.replace('XwaylandEisNoPrompt=false', 'XwaylandEisNoPrompt=true'))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(sorted(entry.name for entry in root.iterdir()), ['kwinrc'])    # no temporary file stays

    def test_update_file_keeps_bytes_that_are_not_text_and_line_ends(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-update-bytes-') as directory:
            path = Path(directory) / 'kwinrc'
            path.write_bytes(b'[Windows]\r\nName=\xff\xfe\r\n')
            desktop.update_file(path, lambda text: desktop.merge_ini_key(text, 'Xwayland', 'K', 'v'))
            self.assertEqual(path.read_bytes(), b'[Windows]\r\nName=\xff\xfe\r\n\n[Xwayland]\nK=v\n')

    def test_a_failed_edit_leaves_the_file_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-update-fail-') as directory:
            path = Path(directory) / 'kwinrc'
            path.write_text(KWINRC)
            with self.assertRaises(ValueError):
                desktop.update_file(path, lambda text: desktop.merge_ini_key(text, 'g', 'k', 'bad\nvalue'))
            self.assertEqual(path.read_text(), KWINRC)
            self.assertEqual(sorted(entry.name for entry in path.parent.iterdir()), ['kwinrc'])
            with self.assertRaises(TypeError):                   # fails while the new file is being written
                desktop.update_file(path, lambda text: None)
            self.assertEqual(path.read_text(), KWINRC)
            self.assertEqual(sorted(entry.name for entry in path.parent.iterdir()), ['kwinrc'])

    def test_the_commands_edit_files_and_report_failures_by_status(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-edit-command-') as directory:
            root = Path(directory)

            def run(*arguments):
                return subprocess.run([sys.executable, str(HELPER), *arguments], capture_output=True, text=True,
                                      timeout=30)
            (root / 'kwinrc').write_text(KWINRC)
            done = run('set-key', str(root / 'kwinrc'), 'Xwayland', 'XwaylandEisNoPrompt', 'true')
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, '', ''))
            self.assertIn('Scale=2\nXwaylandEisNoPrompt=true\n', (root / 'kwinrc').read_text())
            done = run('set-var', str(root / 'user-dirs.dirs'), 'XDG_DESKTOP_DIR', '/d')
            self.assertEqual(done.returncode, 0)
            self.assertEqual((root / 'user-dirs.dirs').read_text(), 'XDG_DESKTOP_DIR="/d"\n')
            done = run('set-key', str(root / 'kwinrc'), 'bad]group', 'k', 'v')
            self.assertEqual((done.returncode, done.stdout), (1, ''))
            self.assertIn('invalid group or key name', done.stderr)
            done = run('set-key', str(root / 'no-directory' / 'kwinrc'), 'g', 'k', 'v')
            self.assertEqual((done.returncode, done.stdout), (1, ''))
            self.assertIn('dosplex-desktop-environment:', done.stderr)


OSK_ID = 'dosplex-steam-osk'
OTHER_RULES = ('[General]\ncount=2\nrules=aaa,bbb\n\n[aaa]\nDescription=Mine\nwmclass=konsole\nwmclassmatch=1\n\n'
               '[bbb]\nDescription=Also mine\nwmclass=dolphin\nwmclassmatch=1\n')


def group_text(text, group):
    """The lines of the [group] section of a settings text."""
    found = re.search(r'^\[' + re.escape(group) + r'\]\n(.*?)(?=^\[|\Z)', text, re.M | re.S)
    return found.group(1) if found else None


class KeyboardFit(unittest.TestCase):
    def test_the_rule_frees_the_size_hints_and_needs_no_scale(self):
        rule = group_text(desktop.merge_osk_rule(''), OSK_ID)
        self.assertEqual(desktop.merge_osk_rule(''), '[General]\nrules=%s\ncount=1\n\n[%s]\n%s' % (OSK_ID, OSK_ID, rule))
        for line in ('wmclass=steamwebhelper steam', 'wmclasscomplete=true', 'wmclassmatch=1',
                     'title=Steam Input On-screen Keyboard', 'titlematch=1',
                     'minsize=100,50', 'minsizerule=2', 'maxsize=16384,16384', 'maxsizerule=2'):
            self.assertIn(line + '\n', rule)
        # Position and size come from the script at the size of the screen, so the rule holds none.
        self.assertNotIn('positionrule', rule)
        self.assertNotIn('sizerule=', rule.replace('minsizerule', '').replace('maxsizerule', ''))

    def test_other_rules_stay_and_the_list_and_count_name_ours_once(self):
        merged = desktop.merge_osk_rule(OTHER_RULES)
        self.assertEqual(group_text(merged, 'General'), 'count=3\nrules=aaa,bbb,%s\n\n' % OSK_ID)
        for group in ('aaa', 'bbb'):
            self.assertEqual(group_text(merged, group).strip(), group_text(OTHER_RULES, group).strip())
        self.assertEqual(desktop.merge_osk_rule(merged), merged)

    def test_a_second_merge_changes_nothing_and_a_changed_rule_is_put_back(self):
        once = desktop.merge_osk_rule(OTHER_RULES)
        self.assertEqual(desktop.merge_osk_rule(once), once)
        edited = once.replace('minsize=100,50', 'minsize=1,1') + '\n[%s]\nstale=1\n' % OSK_ID
        self.assertEqual(desktop.merge_osk_rule(edited), once)

    def test_rules_that_an_older_file_numbers_instead_of_listing(self):
        text = '[General]\ncount=2\n\n[1]\nDescription=A\n\n[2]\nDescription=B\n'
        self.assertEqual(desktop.rule_ids(text), ['1', '2'])
        merged = desktop.merge_osk_rule(text)
        self.assertEqual(desktop.rule_ids(merged), ['1', '2', OSK_ID])
        self.assertEqual(group_text(merged, 'General'), 'count=3\nrules=1,2,%s\n\n' % OSK_ID)
        self.assertEqual(desktop.rule_ids(''), [])
        self.assertEqual(desktop.rule_ids('[General]\ncount=0\nrules=\n'), [])

    def test_the_script_fits_the_keyboard_from_the_screen_it_is_on(self):
        script = desktop.OSK_SCRIPT
        self.assertIn('"Steam Input On-screen Keyboard"', script)
        self.assertIn('workspace.clientArea(KWin.FullScreenArea, window)', script)
        # The height is what Steam asks for, clamped to the screen; a third is only the fallback.
        self.assertIn('height: window.frameGeometry.height', script)
        self.assertIn('keyboard.height = height', script)
        self.assertIn('Math.min(asked, area.height)', script)
        self.assertIn('Math.round(area.height / 3)', script)
        self.assertIn('area.y + area.height - height', script)
        self.assertIn('width: area.width', script)
        for signal in ('windowAdded', 'windowRemoved', 'screensChanged', 'virtualScreenGeometryChanged',
                       'frameGeometryChanged', 'captionChanged'):
            self.assertIn(signal, script)
        metadata = json.loads(desktop.OSK_METADATA)
        self.assertEqual(metadata['KPlugin']['Id'], 'dosplex-osk')
        self.assertEqual(metadata['X-Plasma-MainScript'], 'code/main.js')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_script_is_valid_javascript(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.js'
            path.write_text(desktop.OSK_SCRIPT)
            done = subprocess.run([shutil.which('node'), '--check', str(path)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_commands_write_the_rule_and_the_script_and_a_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'kwinrulesrc').write_text(OTHER_RULES)
            for _ in range(2):
                for command in (('osk-rule', str(root / 'kwinrulesrc')), ('osk-script', str(root / 'kwin/scripts'))):
                    done = subprocess.run([sys.executable, str(HELPER), *command], capture_output=True, text=True)
                    self.assertEqual((done.returncode, done.stdout, done.stderr), (0, '', ''))
                if _ == 0:
                    first = (root / 'kwinrulesrc').read_text()
                    stamp = (root / 'kwin/scripts/dosplex-osk/contents/code/main.js').stat().st_mtime_ns
            self.assertEqual((root / 'kwinrulesrc').read_text(), first)
            self.assertEqual((root / 'kwin/scripts/dosplex-osk/contents/code/main.js').stat().st_mtime_ns, stamp)
            self.assertEqual((root / 'kwin/scripts/dosplex-osk/contents/code/main.js').read_text(), desktop.OSK_SCRIPT)
            self.assertIn('rules=aaa,bbb,%s' % OSK_ID, first)


if __name__ == '__main__':
    unittest.main()
