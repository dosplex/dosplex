#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compile the compositor's actual picture-choice code; check sizes, order, labels and the default per layout."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Every modal action that existed before 1:1 was added, in declaration order.
# state.json publishes these as overlay_confirmation_action codes, so a new
# action may only be appended after them.
PUBLISHED_MODAL_ACTIONS = [
    'NONE', 'CLOSE', 'SINGLE', 'SPLIT_VERTICAL', 'SPLIT_HORIZONTAL', 'GUEST_LEFT', 'GUEST_RIGHT',
    'SCALE_FIT', 'SCALE_STRETCH', 'SCALE_NATIVE', 'RESOLUTION_16_9', 'RESOLUTION_4_3',
    'DISPLAY_APPLY', 'DISPLAY_CANCEL', 'TAB_DISPLAY', 'TAB_DEVICES', 'TAB_SESSION', 'TAB_AUDIO',
    'AUDIO_OUTPUT', 'AUDIO_INPUT', 'AUDIO_SELECT', 'AUDIO_MUTE', 'AUDIO_PREV', 'AUDIO_NEXT',
    'AUDIO_MIX_OFF', 'AUDIO_MIX_FULL', 'AUDIO_MIX_SEPARATED', 'AUDIO_GENTLE', 'AUDIO_BALANCED',
    'AUDIO_STRONG', 'AUDIO_JOIN', 'AUDIO_LEAVE', 'DEVICE_PREV', 'DEVICE_NEXT', 'CONFIRM',
    'CANCEL_ACTION', 'DEVICE_FOLLOW', 'DEVICE_LEFT', 'DEVICE_BOTH', 'DEVICE_RIGHT', 'POWER_SLEEP',
    'POWER_RESTART', 'POWER_SHUTDOWN', 'POWER_EXIT', 'RESET_SIDE', 'RESET_ALL', 'RETIRED_ACTIVE',
    'RETIRED_STANDBY', 'RETIRED_OFF', 'RETIRED_GUEST_ACTIVE', 'RETIRED_GUEST_STANDBY',
    'RETIRED_GUEST_OFF', 'SINGLE_STANDBY', 'SINGLE_OFF', 'RETIRED_SESSION_STATES', 'KEEP_DEVICE',
    'SWAP_DEVICE',
]

# The ratio choices in tie-break order: 16:9 beats 4:3 beats 1:1 when two of
# them fill a region equally.
RATIOS = {'16:9': (16, 9), '4:3': (4, 3), '1:1': (1, 1)}

HARNESS = r'''
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
%(enums)s
%(name)s
%(block)s
int main(void) {
    static const enum game_resolution_choice choices[] = {GAME_RESOLUTION_NATIVE,
        GAME_RESOLUTION_16_9, GAME_RESOLUTION_4_3, GAME_RESOLUTION_1_1, GAME_RESOLUTION_CUSTOM};
    int region_width, region_height;
    for (size_t i = 0; i < sizeof(choices) / sizeof(choices[0]); i++)
        printf("N %%s\n", game_resolution_choice_name(choices[i]));
    printf("D single %%s\n", game_resolution_choice_name(default_game_resolution_choice(LAYOUT_SINGLE)));
    printf("D vertical %%s\n", game_resolution_choice_name(default_game_resolution_choice(LAYOUT_SPLIT_VERTICAL)));
    printf("D horizontal %%s\n", game_resolution_choice_name(default_game_resolution_choice(LAYOUT_SPLIT_HORIZONTAL)));
    printf("X 16:9 %%d\n", (int)MODAL_ACTION_RESOLUTION_16_9);
    printf("X 4:3 %%d\n", (int)MODAL_ACTION_RESOLUTION_4_3);
    printf("X 1:1 %%d\n", (int)MODAL_ACTION_RESOLUTION_1_1);
    while (scanf("%%d %%d", &region_width, &region_height) == 2) {
        printf("R %%d %%d\n", region_width, region_height);
        for (size_t i = 0; i < sizeof(choices) / sizeof(choices[0]); i++) {
            int width = -1, height = -1;
            bool fits = game_resolution_for_choice(region_width, region_height, choices[i], &width, &height);
            printf("S %%s %%d %%d %%d\n", game_resolution_choice_name(choices[i]), fits, width, height);
        }
        const struct game_ratio *order[GAME_RATIO_COUNT];
        game_ratios_by_fill(region_width, region_height, order);
        for (int i = 0; i < GAME_RATIO_COUNT; i++) {
            char label[32];
            game_ratio_label(order[i], region_width, region_height, label, sizeof(label));
            printf("O %%s|%%s|%%d\n", game_resolution_choice_name(order[i]->choice), label, (int)order[i]->action);
        }
    }
    return 0;
}
'''


def extract(source):
    """The enums, the name lookup and the marked block, exactly as compiled in the compositor."""
    enums = ''.join(re.search(pattern, source, re.S).group(0) + '\n' for pattern in (
        r'enum game_resolution_choice \{.*?\n\};', r'enum modal_action \{.*?\n\};',
        r'enum layout_mode \{.*?\n\};'))
    start = source.index('static const char *game_resolution_choice_name(')
    name = source[start:source.index('\n}', start) + 2]
    begin = source.index('/* === Game picture choices === */')
    end = source.index('/* === End of game picture choices === */')
    return enums, name, source[begin:end]


class PictureChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enums, name, block = extract((ROOT / 'compositor/dosplex-compositor.c').read_text())
        directory = tempfile.TemporaryDirectory(prefix='dosplex-picture-choice-')
        cls.addClassCleanup(directory.cleanup)
        code = Path(directory.name) / 'probe.c'
        code.write_text(HARNESS % dict(enums=enums, name=name, block=block))
        cls.binary = Path(directory.name) / 'probe'
        subprocess.run(['cc', '-Wall', '-Werror', str(code), '-o', str(cls.binary)],
                       check=True, capture_output=True, timeout=30)
        cls.modal_enum = re.search(r'enum modal_action \{.*?\n\};', enums, re.S).group(0)

    def run_probe(self, regions):
        text = ''.join(f'{width} {height}\n' for width, height in regions)
        return subprocess.run([str(self.binary)], input=text, check=True, capture_output=True,
                              text=True, timeout=30).stdout.splitlines()

    def defaults(self):
        """The choice that each layout uses until its player makes one."""
        return dict(line[2:].split() for line in self.run_probe([]) if line.startswith('D '))

    def probe(self, regions):
        """Run the compiled code on regions; returns the choice names, the action codes and one record per region."""
        output = self.run_probe(regions)
        names, actions, results, current = [], {}, {}, None
        for line in output:
            kind, _, rest = line.partition(' ')
            if kind == 'N':
                names.append(rest)
            elif kind == 'X':
                choice, action = rest.split()
                actions[choice] = int(action)
            elif kind == 'R':
                current = tuple(int(value) for value in rest.split())
                results[current] = dict(sizes={}, order=[], labels=[], actions=[])
            elif kind == 'S':
                choice, fits, width, height = rest.split()
                results[current]['sizes'][choice] = (int(fits), int(width), int(height))
            elif kind == 'O':
                choice, label, action = rest.split('|')
                results[current]['order'].append(choice)
                results[current]['labels'].append(label)
                results[current]['actions'].append(int(action))
        return names, actions, results

    def check(self, region, sizes, order):
        _, actions, results = self.probe([region])
        found = results[region]
        for choice, size in sizes.items():
            self.assertEqual(found['sizes'][choice], (1, *size), f'{choice} in {region}')
        self.assertEqual(found['order'], order, f'order in {region}')
        self.assertEqual(found['labels'], [f'{choice} {sizes[choice][0]}x{sizes[choice][1]}' for choice in order])
        self.assertEqual(found['actions'], [actions[choice] for choice in order], 'button actions')

    def test_the_four_half_sizes(self):
        # Side by side: 1:1 keeps 89% of the pixels, 4:3 67%, 16:9 50%.
        self.check((1920, 2160), {'1:1': (1920, 1920), '4:3': (1920, 1440), '16:9': (1920, 1080)},
                   ['1:1', '4:3', '16:9'])
        self.check((960, 1080), {'1:1': (960, 960), '4:3': (960, 720), '16:9': (960, 540)},
                   ['1:1', '4:3', '16:9'])
        # Top/bottom: 16:9 keeps 50%, 4:3 38%, 1:1 only 28%.
        self.check((3840, 1080), {'1:1': (1080, 1080), '4:3': (1440, 1080), '16:9': (1920, 1080)},
                   ['16:9', '4:3', '1:1'])
        self.check((1920, 540), {'1:1': (540, 540), '4:3': (720, 540), '16:9': (960, 540)},
                   ['16:9', '4:3', '1:1'])

    def test_full_screen_and_small_screens(self):
        self.check((3840, 2160), {'1:1': (2160, 2160), '4:3': (2880, 2160), '16:9': (3840, 2160)},
                   ['16:9', '4:3', '1:1'])
        self.check((1920, 1080), {'1:1': (1080, 1080), '4:3': (1440, 1080), '16:9': (1920, 1080)},
                   ['16:9', '4:3', '1:1'])
        # Deck panel: full screen, then the side by side and top/bottom halves.
        self.check((1280, 800), {'1:1': (800, 800), '4:3': (1064, 798), '16:9': (1280, 720)},
                   ['16:9', '4:3', '1:1'])
        self.check((640, 800), {'1:1': (640, 640), '4:3': (640, 480), '16:9': (640, 360)},
                   ['1:1', '4:3', '16:9'])
        self.check((1280, 400), {'1:1': (400, 400), '4:3': (532, 399), '16:9': (704, 396)},
                   ['16:9', '4:3', '1:1'])
        # The 1280x720 output used by the headless tests.
        self.check((640, 720), {'1:1': (640, 640), '4:3': (640, 480), '16:9': (640, 360)},
                   ['1:1', '4:3', '16:9'])
        self.check((1280, 360), {'1:1': (360, 360), '4:3': (480, 360), '16:9': (640, 360)},
                   ['16:9', '4:3', '1:1'])

    def test_a_square_region_prefers_the_square(self):
        self.check((1000, 1000), {'1:1': (1000, 1000), '4:3': (1000, 750), '16:9': (992, 558)},
                   ['1:1', '4:3', '16:9'])

    def test_equal_fill_keeps_the_table_order(self):
        # 16:9 gives 160x90 and 1:1 gives 120x120: both 14400 pixels. 4:3 gives
        # 160x120 and wins; between the tied two, 16:9 comes before 1:1.
        self.check((160, 120), {'1:1': (120, 120), '4:3': (160, 120), '16:9': (160, 90)},
                   ['4:3', '16:9', '1:1'])

    def test_native_custom_and_choices_that_do_not_fit(self):
        _, _, results = self.probe([(10, 20), (0, 0), (-5, 100)])
        small = results[(10, 20)]
        self.assertEqual(small['sizes']['native'], (1, 0, 0))  # Native advertises the region itself.
        self.assertEqual(small['sizes']['custom'][0], 0)
        self.assertEqual(small['sizes']['16:9'][0], 0)  # Narrower than 16 pixels.
        self.assertEqual(small['sizes']['1:1'][1:], (10, 10))
        self.assertEqual(small['order'], ['1:1', '4:3', '16:9'])
        self.assertEqual(small['labels'], ['1:1 10x10', '4:3 8x6', '16:9'])
        for region in ((0, 0), (-5, 100)):
            for choice in ('native', '16:9', '4:3', '1:1', 'custom'):
                self.assertEqual(results[region]['sizes'][choice][0], 0, (region, choice))
            self.assertEqual(results[region]['order'], ['16:9', '4:3', '1:1'])
            self.assertEqual(results[region]['labels'], ['16:9', '4:3', '1:1'])

    def test_every_size_is_the_largest_exact_ratio_that_fits_and_orders_by_fill(self):
        values = [1, 3, 9, 15, 16, 17, 100, 120, 160, 175, 320, 360, 400, 540, 640, 720, 800, 960,
                  1080, 1280, 1440, 1920, 2160, 2560, 3840, 4320, 7680]
        regions = [(width, height) for width in values for height in values]
        _, _, results = self.probe(regions)
        for region in regions:
            width, height = region
            found = results[region]
            areas = {}
            for choice, (ratio_width, ratio_height) in RATIOS.items():
                fits, size_width, size_height = found['sizes'][choice]
                if width < ratio_width or height < ratio_height:
                    self.assertEqual(fits, 0, (region, choice))
                    areas[choice] = 0
                    continue
                self.assertEqual(fits, 1, (region, choice))
                self.assertLessEqual(size_width, width, (region, choice))
                self.assertLessEqual(size_height, height, (region, choice))
                self.assertEqual(size_width * ratio_height, size_height * ratio_width, (region, choice))
                self.assertTrue(size_width + ratio_width > width or size_height + ratio_height > height,
                                f'a larger {choice} fits {region}')
                areas[choice] = size_width * size_height
            # Python's sort is stable, so equal fills keep the table order.
            self.assertEqual(found['order'], sorted(RATIOS, key=lambda choice: -areas[choice]), region)
            # 1:1 is the largest square.
            self.assertEqual(found['sizes']['1:1'][1:], (min(region),) * 2, region)

    def test_the_default_is_native_in_every_layout(self):
        self.assertEqual(self.defaults(), {'single': 'native', 'vertical': 'native', 'horizontal': 'native'})

    def test_the_default_monitor_per_layout_on_the_tv_and_the_headless_output(self):
        # Native gives a game every pixel of the region in every layout: the monitor is the region itself
        # (advertised as 0x0). A side that chose 16:9 gets the largest 16:9 that fits, which on 4K is
        # 1920x1080 in either split and the whole screen in Full screen.
        names = self.defaults()
        for split, full in ((((1920, 2160), (3840, 1080)), (3840, 2160)),
                            (((960, 1080), (1920, 540)), (1920, 1080)),
                            (((640, 720), (1280, 360)), (1280, 720))):
            _, _, results = self.probe([*split, full])
            self.assertEqual(results[split[0]]['sizes'][names['vertical']], (1, 0, 0), 'Native: the region itself')
            self.assertEqual(results[split[1]]['sizes'][names['horizontal']], (1, 0, 0), 'Native: the region itself')
            self.assertEqual(results[full]['sizes'][names['single']], (1, 0, 0), 'Native: the region itself')
            vertical = results[split[0]]['sizes']['16:9']
            horizontal = results[split[1]]['sizes']['16:9']
            self.assertEqual(vertical[1:], horizontal[1:], f'one 16:9 monitor for both splits of {full}')
            self.assertEqual(vertical[1] * 9, vertical[2] * 16, 'the TV ratio')
            self.assertEqual(results[full]['sizes']['16:9'][1:], full, 'Native and 16:9 coincide on a 16:9 screen')
        _, _, results = self.probe([(1920, 2160), (3840, 1080)])
        self.assertEqual(results[(1920, 2160)]['sizes']['16:9'], (1, 1920, 1080))
        self.assertEqual(results[(3840, 1080)]['sizes']['16:9'], (1, 1920, 1080))

    def test_state_and_overlay_names(self):
        names, actions, _ = self.probe([])
        # state.json game_aspect: 1:1 is added and the other values are unchanged.
        self.assertEqual(names, ['native', '16:9', '4:3', '1:1', 'custom'])
        self.assertEqual(len(set(actions.values())), 3, 'each ratio button has its own action')

    def test_published_modal_action_codes_do_not_move(self):
        members = re.findall(r'^\s+MODAL_ACTION_([A-Z0-9_]+)', self.modal_enum, re.M)
        self.assertEqual(members[:len(PUBLISHED_MODAL_ACTIONS)], PUBLISHED_MODAL_ACTIONS)
        self.assertIn('RESOLUTION_1_1', members[len(PUBLISHED_MODAL_ACTIONS):])


if __name__ == '__main__':
    unittest.main()
