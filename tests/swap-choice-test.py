#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compile the compositor's actual swap-choice code; check which two controls Swap exchanges.

A player may hold several controls (T-422). With one on each side Swap exchanges those two, as before; with
several it exchanges the one pressed last, and when that cannot be told it says so and keeps the button.
"""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
@BLOCK@
int main(void) {
    static struct activity_table table;
    char word[64];
    while (scanf("%63s", word) == 1) {
        if (!strcmp(word, "note")) {
            char id[128];
            double at;
            if (scanf("%127s %lf", id, &at) == 2) activity_note(&table, id, at);
        } else if (!strcmp(word, "last")) {
            char id[128];
            if (scanf("%127s", id) == 1) printf("L %.3f\n", activity_last(&table, id));
        } else if (!strcmp(word, "count")) {
            printf("C %d\n", table.count);
        } else if (!strcmp(word, "swap")) {
            double now;
            int count;
            struct swap_control controls[64];
            static char keys[64][64];
            if (scanf("%lf %d", &now, &count) != 2 || count > 64) return 2;
            for (int i = 0; i < count; i++) {
                int guest;
                double at;
                if (scanf("%63s %d %lf", keys[i], &guest, &at) != 3) return 2;
                controls[i] = (struct swap_control){keys[i], guest != 0, at};
            }
            int main_index = -1, guest_index = -1;
            enum swap_choice choice = choose_swap_pair(controls, count, now, &main_index, &guest_index);
            printf("R %d %d %d\n", (int)choice, main_index, guest_index);
        }
    }
    printf("W %.1f\n", (double)SWAP_ACTIVE_SECONDS);
    return 0;
}
'''

UNAVAILABLE, NEEDS_PRESS, READY = 0, 1, 2


def extract(source):
    begin = source.index('/* === Swap choice === */')
    end = source.index('/* === End of swap choice === */')
    return source[begin:end]


class SwapChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix='dosplex-swap-choice-')
        cls.addClassCleanup(directory.cleanup)
        cls.source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        block = extract(cls.source)
        code = Path(directory.name) / 'probe.c'
        code.write_text(HARNESS.replace('@BLOCK@', block))
        cls.binary = Path(directory.name) / 'probe'
        subprocess.run(['cc', '-Wall', '-Werror', str(code), '-o', str(cls.binary)],
                       check=True, capture_output=True, timeout=30)

    def run_probe(self, text):
        return subprocess.run([str(self.binary)], input=text, check=True, capture_output=True,
                              text=True, timeout=30).stdout.splitlines()

    def swap(self, controls, now=100.0):
        """controls: (key, 'main'|'guest', seconds ago or None). Returns (choice, main key, guest key)."""
        lines = ['swap %.3f %d' % (now, len(controls))]
        for key, side, ago in controls:
            lines.append('%s %d %.3f' % (key, side == 'guest', 0 if ago is None else now - ago))
        output = self.run_probe('\n'.join(lines) + '\n')
        choice, main_index, guest_index = (int(value) for value in output[0].split()[1:])
        names = [key for key, _, _ in controls]
        return choice, names[main_index] if main_index >= 0 else None, names[guest_index] if guest_index >= 0 else None

    # --- one control on each side: what Swap always did ----------------------------------------------------

    def test_one_control_each_is_the_pair_whether_or_not_it_was_pressed(self):
        for ago in (None, 0.5, 3600):
            self.assertEqual(self.swap([('controller:a', 'main', ago), ('controller:b', 'guest', None)]),
                             (READY, 'controller:a', 'controller:b'), ago)
            self.assertEqual(self.swap([('input:3', 'main', ago), ('input:4', 'guest', ago)]),
                             (READY, 'input:3', 'input:4'), ago)

    def test_no_control_on_a_side_or_two_backends_leave_no_button(self):
        self.assertEqual(self.swap([])[0], UNAVAILABLE)
        self.assertEqual(self.swap([('controller:a', 'main', 1)])[0], UNAVAILABLE)
        self.assertEqual(self.swap([('controller:a', 'guest', 1), ('controller:b', 'guest', 2)])[0], UNAVAILABLE)
        # A keyboard against a controller cannot be exchanged; no amount of pressing changes that.
        self.assertEqual(self.swap([('input:3', 'main', 1), ('controller:b', 'guest', 1)])[0], UNAVAILABLE)
        self.assertEqual(self.swap([('input:3', 'main', 1), ('input:4', 'main', 2), ('controller:b', 'guest', 1),
                                    ('controller:c', 'guest', 3)])[0], UNAVAILABLE)

    # --- several controls: the one pressed last -------------------------------------------------------------

    def test_a_player_with_two_controllers_swaps_the_one_pressed_last(self):
        controls = [('controller:m1', 'main', 4), ('controller:m2', 'main', 1), ('controller:g1', 'guest', 2)]
        self.assertEqual(self.swap(controls), (READY, 'controller:m2', 'controller:g1'))
        controls[0] = ('controller:m1', 'main', 0.2)
        self.assertEqual(self.swap(controls), (READY, 'controller:m1', 'controller:g1'))

    def test_both_players_with_several_swap_what_each_used_last(self):
        controls = [('controller:m1', 'main', 6), ('controller:m2', 'main', 2), ('controller:g1', 'guest', 1),
                    ('controller:g2', 'guest', 8)]
        self.assertEqual(self.swap(controls), (READY, 'controller:m2', 'controller:g1'))

    def test_when_nobody_was_pressed_the_button_asks_for_a_press(self):
        self.assertEqual(self.swap([('controller:m1', 'main', None), ('controller:m2', 'main', None),
                                    ('controller:g1', 'guest', None)]), (NEEDS_PRESS, None, None))
        # One side being clear does not help: Guest holds two and neither was pressed.
        self.assertEqual(self.swap([('controller:m1', 'main', 1), ('controller:g1', 'guest', None),
                                    ('controller:g2', 'guest', None)])[0], NEEDS_PRESS)

    def test_a_press_counts_for_ten_seconds(self):
        stale = [('controller:m1', 'main', 10.5), ('controller:m2', 'main', None), ('controller:g1', 'guest', 0.1)]
        self.assertEqual(self.swap(stale)[0], NEEDS_PRESS)
        fresh = [('controller:m1', 'main', 9.5), ('controller:m2', 'main', None), ('controller:g1', 'guest', 0.1)]
        self.assertEqual(self.swap(fresh), (READY, 'controller:m1', 'controller:g1'))
        self.assertEqual(self.run_probe('')[-1], 'W 10.0')

    def test_two_controls_pressed_at_the_same_moment_cannot_be_told_apart(self):
        tied = [('controller:m1', 'main', 1), ('controller:m2', 'main', 1), ('controller:g1', 'guest', 1)]
        self.assertEqual(self.swap(tied)[0], NEEDS_PRESS)
        tied[1] = ('controller:m2', 'main', 0.5)                     # The next press settles it.
        self.assertEqual(self.swap(tied), (READY, 'controller:m2', 'controller:g1'))
        # A tie that is older than the latest press does not matter.
        older = [('controller:m1', 'main', 3), ('controller:m2', 'main', 3), ('controller:m3', 'main', 1),
                 ('controller:g1', 'guest', 1)]
        self.assertEqual(self.swap(older), (READY, 'controller:m3', 'controller:g1'))

    def test_a_player_who_used_a_keyboard_can_still_swap_controllers(self):
        # Main has a keyboard and a controller, Guest only a controller: the only pair is controller to
        # controller, even if the keyboard was used last (it was used to open the menu).
        controls = [('input:2', 'main', 0.1), ('controller:m1', 'main', 5), ('controller:g1', 'guest', None)]
        self.assertEqual(self.swap(controls), (READY, 'controller:m1', 'controller:g1'))

    def test_with_both_backends_on_both_sides_the_last_presses_must_agree(self):
        controls = [('input:2', 'main', 0.5), ('controller:m1', 'main', 5),
                    ('input:3', 'guest', 0.3), ('controller:g1', 'guest', 4)]
        self.assertEqual(self.swap(controls), (READY, 'input:2', 'input:3'))
        controls[2] = ('input:3', 'guest', 20)                       # Guest last used its controller.
        self.assertEqual(self.swap(controls), (NEEDS_PRESS, None, None))
        controls[0] = ('input:2', 'main', 20)                        # Main did too: the controllers.
        self.assertEqual(self.swap(controls), (READY, 'controller:m1', 'controller:g1'))

    def test_a_player_with_a_controller_per_receiver_slot_swaps_the_slot_in_use(self):
        controls = [('controller:m%d' % n, 'main', None) for n in range(3)] + [
            ('controller:g%d' % n, 'guest', None) for n in range(3)]
        controls[1] = ('controller:m1', 'main', 2)
        controls[5] = ('controller:g2', 'guest', 7)
        self.assertEqual(self.swap(controls), (READY, 'controller:m1', 'controller:g2'))

    def test_the_composition_of_the_list_does_not_decide(self):
        controls = [('controller:m1', 'main', 4), ('controller:m2', 'main', 1), ('controller:g1', 'guest', 2),
                    ('controller:g2', 'guest', 3)]
        expected = self.swap(controls)
        self.assertEqual(expected, (READY, 'controller:m2', 'controller:g1'))
        self.assertEqual(self.swap(list(reversed(controls))), expected)

    # --- the memory of presses -----------------------------------------------------------------------------

    def test_the_last_press_of_each_controller_is_kept(self):
        output = self.run_probe('last a\nnote a 5\nnote b 7\nlast a\nnote a 9\nlast a\nlast b\ncount\n')
        self.assertEqual(output[:5], ['L 0.000', 'L 5.000', 'L 9.000', 'L 7.000', 'C 2'])

    def test_the_table_forgets_the_oldest_when_it_is_full(self):
        notes = ''.join('note id%02d %d\n' % (n, n + 1) for n in range(32))
        output = self.run_probe(notes + 'count\nnote extra 100\ncount\nlast id00\nlast id01\nlast extra\n')
        self.assertEqual(output[:5], ['C 32', 'C 32', 'L 0.000', 'L 2.000', 'L 100.000'])

    def test_what_is_too_long_for_a_controller_id_is_not_stored(self):
        self.assertEqual(self.run_probe('note %s 5\ncount\nlast %s\n' % ('z' * 60, 'z' * 60))[:2], ['C 0', 'L 0.000'])

    # --- the source around the block -----------------------------------------------------------------------

    def test_a_published_action_code_is_never_reused(self):
        enum = re.search(r'enum modal_action \{.*?\n\};', self.source, re.S).group(0)
        members = re.findall(r'^\s+MODAL_ACTION_([A-Z0-9_]+)', enum, re.M)
        # Guest's two moves are appended after everything that was published before them.
        self.assertEqual(members[members.index('CLAIM_DEVICE'):][:2], ['CLAIM_DEVICE', 'RELEASE_DEVICE'])
        # The FPS counter's four (T-434) come after those, and nothing is inserted before them.
        self.assertEqual(members[-11:-7], ['FPS_OFF', 'FPS_ON', 'FPS_FRAMETIME', 'PERF_RECORD'])
        # Main's two brightness steps (T-436) are appended after those.
        self.assertEqual(members[-7:-5], ['BRIGHTNESS_DOWN', 'BRIGHTNESS_UP'])
        # The Zoom button is appended after those, and Guest full screen after it.
        self.assertEqual(members[-5:-3], ['SCALE_ZOOM', 'SINGLE_GUEST'])
        # Forgetting a controller that is off is appended after those.
        self.assertEqual(members[-3], 'FORGET_DEVICE')
        # Help and Feedback, which open the panel, are appended after that.
        self.assertEqual(members[-2:], ['HELP', 'FEEDBACK'])
        self.assertEqual(members.index('FPS_OFF'), members.index('RELEASE_DEVICE') + 1)
        self.assertLess(members.index('RESOLUTION_1_1'), members.index('CLAIM_DEVICE'))


if __name__ == '__main__':
    unittest.main()
