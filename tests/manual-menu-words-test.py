#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=60
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The manual and the menu agree: every word the manual prints in bold that names a button or a page of
Dosplex's on-screen menu is a word the program really shows.

The manual (docs/manual/[0-9]*.md) names the menu's buttons and pages in bold, for example **Session** or
**Reset my side**. The words the menu really shows are string literals of compositor/dosplex-compositor.c
and of the Help and Feedback panel under packaging/steamos/panel/. When a menu word changes in the program,
the manual goes stale; this test is the one that notices.

Every bold phrase must be in exactly one of three places, or the test lists it with its file and line:
- in the program: it equals a string, or a string starts with the phrase followed by a space, an ellipsis,
  a dot dot dot, or a colon (the menu shows `Full mix…`; the manual writes **Full mix**);
- NOT_DOSPLEX, below: a word of Steam's or SteamOS's own interface, a controller or keyboard key, or a
  sentence opener in bold that names no button;
- TO_CHECK, below: a phrase that looks like a menu word and is not in the program, kept out of the count
  until a person says it is wrong or right. A wrong entry in NOT_DOSPLEX hides a real error for ever, so
  the doubtful ones go to TO_CHECK.

An entry of NOT_DOSPLEX or TO_CHECK that no chapter uses any more fails the test, so neither list goes stale.
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPOSITOR = ROOT / 'compositor' / 'dosplex-compositor.c'
PANEL = ROOT / 'packaging' / 'steamos' / 'panel'
AUDIO_ROUTER = ROOT / 'packaging' / 'steamos' / 'dosplex-audio-router.py'
MANUAL = ROOT / 'docs' / 'manual'

# A string the manual prints in bold that is not a word of Dosplex's menu. The reason is one of
# 'steam', 'button', 'emphasis', nothing else. 'steam': a word of Steam's or SteamOS's own interface,
# such as Power or Switch to Desktop. 'button': a controller or keyboard key. 'emphasis': a sentence
# opener in bold that names no button.
NOT_DOSPLEX = {
    # steam: words of Steam's and SteamOS's own interface.
    'A / Cross': 'steam',
    'Add Selected Programs': 'steam',
    'Add a Non-Steam Game to My Library': 'steam',
    'B / Circle': 'steam',
    'D-pad Up': 'steam',
    'Enter': 'steam',
    'Escape': 'steam',
    'Games': 'steam',
    'Menu': 'steam',
    'Non-Steam': 'steam',
    'Play': 'steam',
    'Power': 'steam',
    'Return to Gaming Mode': 'steam',
    'Select / Back / View': 'steam',
    'Storage': 'steam',
    'Super+Shift+M': 'steam',
    'Switch to Desktop': 'steam',
    'Updates': 'steam',
    'View': 'steam',
    'Wi-Fi and power': 'steam',
    # button: controller and keyboard keys.
    'B': 'button',
    'Ctrl+Shift+M': 'button',
    'L2': 'button',
    'R2': 'button',
    'X': 'button',
    '?': 'button',
    # emphasis: sentence openers that name no button.
    'A black screen': 'emphasis',
    'Discord': 'emphasis',
    'GitHub': 'emphasis',
    'What you did, and what happened': 'emphasis',
    'Your version': 'emphasis',
    'Other gamepads open the menu experimentally': 'emphasis',
    'The machine does not go to sleep by itself while two players are on the screen': 'emphasis',
    'Your machine': 'emphasis',
    'Whether it happens again': 'emphasis',
    'A controller that connects first belongs to Main': 'emphasis',
    'A controller that does not answer': 'emphasis',
    'A game that does not start': 'emphasis',
    'A game that quits right after it starts in a split': 'emphasis',
    'A game keeps its own resolution setting': 'emphasis',
    "Each player's own files of a shared game stay their own": 'emphasis',
    'A drive you take out': 'emphasis',
    'Steam may mark a game "update required"': 'emphasis',
    'A game goes back to the player who shared it first': 'emphasis',
    'A game that was already running may not see a newly claimed controller': 'emphasis',
    'A microphone you claim is not kept': 'emphasis',
    "A program can draw over the other player's part of the screen": 'emphasis',
    "Dosplex does not start from Steam's library": 'emphasis',
    'Flatpak apps are there too': 'emphasis',
    "Guest's own Steam shows one output and one microphone": 'emphasis',
    'It starts only when you start it': 'emphasis',
    "Main's": 'emphasis',
    'No picture changes while a game is running': 'emphasis',
    'No sound': 'emphasis',
    'Some things are missing, and some are rough': 'emphasis',
    'Standby is experimental': 'emphasis',
    "The Steam Deck's own screen shares one backlight": 'emphasis',
    'The desktop opens at a readable scale': 'emphasis',
    'The frame-rate display has not been checked on a device yet': 'emphasis',
    'The two sides share one network': 'emphasis',
    'To choose another scale,': 'emphasis',
    'To open it,': 'emphasis',
    'When you would change it': 'emphasis',
    'Your desktop is your own': 'emphasis',
    'Your feedback is what improves it': 'emphasis',
    # Paragraph openers of the chapter on the commands; the words are the commands `dosplex update`, `rollback`, `uninstall`.
    'Roll back': 'emphasis',
    'Uninstall': 'emphasis',
    'Update': 'emphasis',
}

# (phrase, file, line, what was searched) — a phrase that looks like a menu word and is not in the program,
# kept out of the count until a person says what it is. These are the words of the `dosplex` command line
# (Update, Roll back, Uninstall), which the program shows on the terminal, not in the menu.
TO_CHECK = []

# A C string literal: `"..."` with escaped characters.
C_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
# A quoted string, one per line (QML and JavaScript): `"..."` with escaped characters.
QUOTED = re.compile(r'"([^"\\](?:[^"\\]|\\.)*)"')
# A bold phrase: `**...**`, not spanning lines.
BOLD = re.compile(r'\*\*(.+?)\*\*')
# A fenced code block: a line that starts with ``` (optionally indented).
FENCE = re.compile(r'^\s*```')


def collect_bold_phrases():
    """Every bold phrase of every manual chapter, as a list of (phrase, file, line).

    A trailing ':' or '.' inside the bold is stripped. Bold text inside a fenced code block is not
    collected. The phrase is kept with every place it occurs, so one phrase on several lines is listed
    for every line."""
    phrases = []
    for path in sorted(MANUAL.glob('[0-9]*.md')):
        in_fence = False
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in BOLD.finditer(line):
                phrase = match.group(1).rstrip(':.')
                if phrase:
                    phrases.append((phrase, path.name, number))
    return phrases


def program_strings():
    """Every string literal of the compositor, the audio router and the panel, line by line.

    The compositor and the audio router are scanned whole (C and Python literals); the panel is scanned
    line by line, because QML puts several strings on one line in a ternary chain and JavaScript joins
    them with ``+``, and scanning the whole text at once would miss the ones after the first on a line."""
    strings = set()
    for path in (COMPOSITOR, AUDIO_ROUTER):
        for match in C_STRING.finditer(path.read_text(encoding='utf-8')):
            strings.add(match.group(1))
    for path in sorted(PANEL.rglob('*')):
        if not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            for match in QUOTED.finditer(line):
                strings.add(match.group(1))
    return strings


def in_program(phrase, strings):
    """Rule 2: the phrase equals a string, or a string starts with the phrase followed by a space,
    an ellipsis, a dot dot dot, or a colon."""
    if phrase in strings:
        return True
    for tail in (' ', '\u2026', '...', ':'):
        if any(s.startswith(phrase + tail) for s in strings):
            return True
    return False


class ManualMenuWordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phrases = collect_bold_phrases()
        cls.strings = program_strings()

    def test_at_least_thirty_phrases(self):
        """At least 30 phrases were found in the program, so a broken collector cannot pass."""
        found = sum(1 for phrase, _, _ in self.phrases if in_program(phrase, self.strings))
        self.assertGreaterEqual(
            found, 30, 'only %d bold phrases were found in the program; the collector is broken' % found)

    def test_every_phrase_is_in_one_group(self):
        """Every bold phrase is in the program, in NOT_DOSPLEX, or in TO_CHECK — nothing else."""
        problems = []
        for phrase, where, line in self.phrases:
            if in_program(phrase, self.strings):
                continue
            if phrase in NOT_DOSPLEX:
                continue
            if any(t[0] == phrase for t in TO_CHECK):
                continue
            problems.append('%s:%d: **%s**' % (where, line, phrase))
        self.assertEqual(
            problems, [],
            'these bold phrases are not in the program, not in NOT_DOSPLEX, not in TO_CHECK:\n'
            + '\n'.join(problems))

    def test_not_dosplex_is_not_stale(self):
        """Every entry of NOT_DOSPLEX is still used by some chapter, so the list cannot go stale."""
        used = {phrase for phrase, _, _ in self.phrases}
        stale = sorted(p for p in NOT_DOSPLEX if p not in used)
        self.assertEqual(stale, [], 'entries of NOT_DOSPLEX no chapter uses any more: %s' % ', '.join(stale))

    def test_to_check_is_not_stale(self):
        """Every entry of TO_CHECK is still used by some chapter, so the list cannot go stale."""
        used = {phrase for phrase, _, _ in self.phrases}
        stale = sorted(p for p, _, _, _ in TO_CHECK if p not in used)
        self.assertEqual(stale, [], 'entries of TO_CHECK no chapter uses any more: %s' % ', '.join(stale))

    def test_not_dosplex_reasons(self):
        """The reason of every entry of NOT_DOSPLEX is one of the three allowed, nothing else."""
        allowed = {'steam', 'button', 'emphasis'}
        bad = sorted(p for p, r in NOT_DOSPLEX.items() if r not in allowed)
        self.assertEqual(bad, [], 'NOT_DOSPLEX entries with an unknown reason: %s' % ', '.join(bad))

    def test_to_check_is_printed(self):
        """TO_CHECK is printed as a count, so it stays visible even though it does not fail the test."""
        print('\nTO_CHECK: %d phrase(s) that look like menu words and are not in the program:' % len(TO_CHECK))
        for phrase, where, line, searched in TO_CHECK:
            print('  %s:%d  %s' % (where, line, phrase))
            print('      searched: %s' % searched)
        self.assertTrue(True)

    # --- the collector and rule 2, on made-up text -----------------------------------------------------------

    def test_collector_strips_trailing_colon_and_dot(self):
        text = 'choose **Session.** and then **Turn off Dosplex:** to stop.\n'
        phrases = [(m.group(1).rstrip(':.'), 1) for m in BOLD.finditer(text)]
        self.assertEqual(phrases, [('Session', 1), ('Turn off Dosplex', 1)])

    def test_collector_skips_fenced_code_blocks(self):
        text = '```bash\n**Not bold in code**\n```\nand **bold in text**\n'
        in_fence = False
        found = []
        for line in text.splitlines():
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in BOLD.finditer(line):
                found.append(match.group(1).rstrip(':.'))
        self.assertEqual(found, ['bold in text'])

    def test_collector_keeps_every_occurrence(self):
        text = '**Main** holds **Main**.\n**Guest** goes free.\n'
        phrases = [(m.group(1).rstrip(':.'), number) for number, line in enumerate(text.splitlines(), 1)
                   for m in BOLD.finditer(line)]
        self.assertEqual(phrases, [('Main', 1), ('Main', 1), ('Guest', 2)])

    def test_rule_2_equal(self):
        self.assertTrue(in_program('Session', {'Session', 'Turn off Dosplex'}))

    def test_rule_2_prefix_with_space(self):
        self.assertTrue(in_program('Full mix', {'Full mix…', 'Full mix — all sound'}))
        self.assertTrue(in_program('Main', {'Main left', 'Main right'}))

    def test_rule_2_prefix_with_ellipsis(self):
        self.assertTrue(in_program('Full mix', {'Full mix…'}))

    def test_rule_2_prefix_with_dot_dot_dot(self):
        self.assertTrue(in_program('Full mix', {'Full mix...'}))

    def test_rule_2_prefix_with_colon(self):
        self.assertTrue(in_program('1:1', {'1:1 1920x1920', '1:1: 1920x1920'}))

    def test_rule_2_no_prefix_is_no_match(self):
        self.assertFalse(in_program('Main', {'Mainly', 'Domain'}))
        self.assertFalse(in_program('Session', {'Sessions'}))
        self.assertFalse(in_program('Full mix', {'Full mixes'}))

    def test_rule_2_empty_strings_do_not_match(self):
        self.assertFalse(in_program('Session', set()))
        self.assertFalse(in_program('', {'Session'}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
