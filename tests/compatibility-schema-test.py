#!/usr/bin/env python3
# dosplex-test: tier=unit needs=python-yaml,git
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Check the compatibility database: scenario schema, canonical hashing, the acceptance lock,
result records, the release gate and the generated public page.

The cumulative rule of ADR-008 is the system under test. A scenario is pending until the owner locks it;
a locked scenario is frozen; a candidate passes the gate only when every locked scenario has a pass on it,
at the right tier, device, setup and evidence level. The fixtures here build small scenario documents and
result records, so none of this needs the private result store.
"""
import copy
import datetime as dt
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compat_results', ROOT / 'tools/compat_results.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)

CANDIDATE = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
OTHER_CANDIDATE = '0123456789abcdef0123456789abcdef01234567'
BUNDLE = 'f0e1d2c3b4a5968778695a4b3c2d1e0f' * 2
OTHER_BUNDLE = '1f2e3d4c5b6a79887766554433221100' * 2
NOW = dt.datetime(2026, 10, 8, 16, 0, tzinfo=dt.timezone.utc)
STOCK = {'root_readonly': True, 'extra_users': [], 'decky': False, 'sudoers_dropin': False}

# The 18 fields the previous evidence contract required, and where a result record carries each now.
LEGACY_METADATA = {
    'run_id': 'run_id', 'timestamp': 'timestamp', 'source_revision': 'source_revision', 'candidate_sha256': 'candidate_sha256',
    'os_version': 'os_version', 'gpu_driver': 'gpu_driver', 'gamescope_version': 'gamescope_version',
    'steam_version': 'steam_version', 'game_build': 'game_build', 'proton_version_or_native': 'proton_version_or_native',
    'tv_mode': 'setup.tv_mode', 'controller_identities': 'controller_identities',
    'observed_game_resolution': 'observed_game_resolution', 'dosplex_picture_choice': 'setup.preset and setup.presentation',
    'steam_game_resolution_setting': 'steam_game_resolution_setting', 'scenario': 'scenario', 'result': 'result',
    'observations': 'observations',
}
# What a published file must not contain, in the page and in the scenario file alike.
PRIVATE_PATTERNS = {
    'IPv4 address': r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    'home path': r'/(?:home|Users)/[A-Za-z0-9_.-]+',
    'private directory': r'(?<![\w.~-])' + 'private' + '/',  # In two parts: a published file may not spell out the reference.
    'controller identifier': r'\b[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\b|\bhidraw[0-9]*\b|/dev/(?:input|hidraw)|\bevent[0-9]+\b',
    'host name': r'\b[A-Za-z0-9-]+\.(?:local|lan|home|internal)\b|@[A-Za-z0-9-]+\.',
    'scenario hash': r'\b[0-9a-f]{64}\b',
}
RUN_TOGETHER = re.compile(r'\b(?:on|at|over|after|sending|build|Build|Proton|native|candidate)[0-9]')


def make_scenario(sid='QUAKE-ONE-001', level='human', **setup):
    """A valid scenario of the Quake game in the mini document."""
    base = {'device': 'steam_machine', 'backend': 'x11_nested', 'tier': 'T1', 'tv_mode': '2160p60', 'layout': 'side_by_side',
            'side': 'main', 'preset': 'native', 'presentation': 'fit', 'window_mode': 'default', 'steam_input': 'default',
            'steam_game_resolution': 'default', 'controller': 'any'}
    base.update(setup)
    if level == 'human':
        criteria = {'automated': [{'kind': 'process_alive', 'min_seconds': 40}], 'human': ['The game starts and reaches its menu.']}
    else:
        criteria = {'automated': [{'kind': 'process_alive', 'min_seconds': 40}], 'human': []}
    return {'id': sid, 'game': 'quake', 'title': 'A test scenario', 'introduced': dt.date(2026, 10, 8), 'setup': base,
            'profile': None, 'pass_criteria': criteria, 'level_required': level}


def mini(*scenarios, waivers=()):
    """A small, valid scenario document with the real vocabulary and criteria kinds."""
    real = REAL_DOC
    return {'version': 1, 'acceptance_policy': {'rule': real['acceptance_policy']['rule'], 'requirements': ['Keep every pass.']},
            'schema': copy.deepcopy(real['schema']),
            'games': [{'id': 'quake', 'code': 'QUAKE', 'name': 'Quake', 'steam_app_id': 2310, 'platform': 'steam'}],
            'scenarios': list(scenarios) or [make_scenario()], 'waivers': list(waivers)}


def concrete(setup):
    out = dict(setup)
    if out['device'] == 'any':
        out['device'] = 'steam_machine'
    if out['controller'] == 'any':
        out['controller'] = 'steam_controller'
    return out


def make_record(target, run_id='run-0001', **changes):
    """A valid, passing result record for the scenario `target` on the test candidate."""
    record = {
        'schema': 1, 'run_id': run_id, 'timestamp': '2026-10-07T12:00:00Z', 'scenario': target['id'],
        'criteria_sha256': db.scenario_hash(target), 'level': target['level_required'], 'result': 'pass',
        'observations': 'The game reached its menu and filled the half.',
        'steps': ['Launched the game from Steam', 'Waited 40 seconds', 'Checked the frame'],
        'cleanup': 'Closed the game; nothing else was changed.', 'evidence_ref': 'captures/example/run-0001',
        'source_revision': CANDIDATE, 'candidate_sha256': BUNDLE, 'os_version': '3.9.2 (20260925.101)',
        'gpu_driver': 'Mesa 25.2', 'gamescope_version': '3.16.30', 'steam_version': '1760000000', 'game_build': '25148042',
        'proton_version_or_native': 'Proton 11.0-100', 'observed_game_resolution': '1920x2160',
        'steam_game_resolution_setting': 'default', 'steam_input_layout': 'Steam default template',
        'controller_identities': ['Steam Controller, wireless'], 'setup': concrete(target['setup']),
        'baseline': dict(STOCK),
    }
    record.update(changes)
    return record


def lock_entry(scenario, level=None, date='2026-10-08'):
    return db.LockEntry(scenario['id'], db.scenario_hash(scenario), date, level or scenario['level_required'])


def gate(scenarios, records, entries=None, candidate=CANDIDATE, tier=None, waivers=()):
    doc = mini(*scenarios, waivers=waivers)
    if entries is None:
        entries = [lock_entry(s) for s in scenarios]
    return db.evaluate_gate(doc, entries, records, candidate, tier)


REAL_CONTRACTS = db.load_contracts()
REAL_DOC = db.load_scenarios(contracts=REAL_CONTRACTS)


class RealDataTests(unittest.TestCase):
    """The files in the repository."""

    def test_scenario_file_is_valid(self):
        self.assertEqual(db.validate_scenarios(REAL_DOC, REAL_CONTRACTS), [])

    def test_ids_are_unique_and_every_game_has_a_scenario(self):
        ids = [s['id'] for s in REAL_DOC['scenarios']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({g['id'] for g in REAL_DOC['games']}, {s['game'] for s in REAL_DOC['scenarios']})
        codes = {g['id']: g['code'] for g in REAL_DOC['games']}
        for scenario in REAL_DOC['scenarios']:
            self.assertTrue(scenario['id'].startswith(codes[scenario['game']] + '-'), scenario['id'])

    def test_no_scenario_records_an_outcome(self):
        allowed = set(db.SCENARIO_KEYS)
        for scenario in REAL_DOC['scenarios']:
            self.assertLessEqual(set(scenario), allowed, scenario['id'])
            for forbidden in ('status', 'result', 'outcome', 'pass', 'passed'):
                self.assertNotIn(forbidden, scenario)

    def test_presets_are_text_not_numbers(self):
        for scenario in REAL_DOC['scenarios']:
            self.assertIsInstance(scenario['setup']['preset'], str, scenario['id'])
        self.assertEqual(set(db.vocabulary(REAL_DOC)['preset']), {'native', '1:1', '4:3', '16:9'})

    def test_the_window_sizing_pair_is_defined(self):
        pair = REAL_DOC['acceptance_policy']['first_window_sizing_pair']
        self.assertEqual(len(pair), 2)
        self.assertTrue(set(pair) <= {s['id'] for s in REAL_DOC['scenarios']})

    def test_native_builds_name_the_window_class_that_was_measured(self):
        classes = {game['id']: db.window_classes(game) for game in REAL_DOC['games'] if 'window_class' in game}
        self.assertEqual(classes, {'crimsonland': ['crimsonland'], 'valheim': ['valheim.x86_64']})

    def test_every_invariant_a_game_points_at_exists(self):
        for game in REAL_DOC['games']:
            for link in game.get('invariants', []):
                self.assertIn(link['contract'], REAL_CONTRACTS, game['id'])

    def test_the_scenario_file_is_free_of_private_data_and_glued_words(self):
        text = (ROOT / 'compatibility/scenarios.yaml').read_text(encoding='utf-8')
        for label, pattern in PRIVATE_PATTERNS.items():
            self.assertIsNone(re.search(pattern, text), label)
        self.assertEqual([l for l in text.splitlines() if RUN_TOGETHER.search(l)], [])

    def test_the_sd_card_scenarios_fail_on_every_run_when_the_card_cannot_be_written(self):
        # Steam runs a game's first-run setup once, and afterwards the game also starts from a card that Steam cannot
        # write to. So the setup criterion cannot fail a rerun, and the schema has no criterion kind for a log: a human
        # criterion has to carry the failed writes, which Steam logs for as long as the card is read-only.
        cards = [s for s in REAL_DOC['scenarios'] if s['id'].startswith('ALIENBREED-SDCARD-')]
        self.assertEqual(len(cards), 2)
        for scenario in cards:
            human = scenario['pass_criteria']['human']
            logs = [c for c in human if 'Failed to write app state file' in c
                    and 'BFilesystemAllowsExecution: failed to write' in c]
            self.assertEqual(len(logs), 1, scenario['id'])
            self.assertTrue(logs[0].startswith('On every run'), scenario['id'])
            self.assertIn('freshly installed', human[0], scenario['id'])        # what a first run starts from
            self.assertTrue(any('On a rerun' in c for c in human), scenario['id'])

    def test_the_lock_matches_the_scenarios(self):
        entries = db.load_lock()
        self.assertEqual(db.lock_problems(entries, REAL_DOC), [])

    def test_the_lock_has_lost_nothing_that_an_earlier_commit_had(self):
        problems, note = db.lock_history_problems(db.load_lock())
        self.assertEqual(problems, [])
        if note:
            print(f'NOTE: {note}')

    def test_a_result_record_still_carries_every_field_of_the_old_evidence_contract(self):
        template = db.record_template(REAL_DOC['scenarios'][0])
        for old, new in LEGACY_METADATA.items():
            if '.' not in new:
                self.assertIn(new, template, old)
            else:
                self.assertIn('setup', template, old)
        self.assertEqual({'preset', 'presentation', 'tv_mode'} - set(template['setup']), set())

    def test_the_data_check_command_passes(self):
        self.assertEqual(db.main(), 0)


class ValidationTests(unittest.TestCase):
    def problems(self, doc):
        return db.validate_scenarios(doc)

    def assertProblem(self, doc, text):
        found = self.problems(doc)
        self.assertTrue(any(text in p for p in found), f'{text!r} not in {found}')

    def test_a_minimal_document_is_valid(self):
        self.assertEqual(self.problems(mini()), [])

    def test_every_setup_field_must_come_from_the_vocabulary(self):
        for field in db.SETUP_FIELDS:
            doc = mini(make_scenario(**{field: 'nonsense'}))
            self.assertProblem(doc, f'setup.{field}')
        doc = mini(make_scenario())
        del doc['scenarios'][0]['setup']['layout']
        self.assertProblem(doc, 'missing layout')

    def test_an_unquoted_preset_is_caught(self):
        doc = db.load_yaml_text('preset: 4:3\n')
        self.assertEqual(doc['preset'], 243)  # YAML reads it as a base-60 number
        self.assertProblem(mini(make_scenario(preset=243)), 'quote')

    def test_an_outcome_cannot_be_recorded_in_the_scenario_file(self):
        for key in ('status', 'result', 'pass'):
            scenario = make_scenario()
            scenario[key] = 'pass'
            self.assertProblem(mini(scenario), 'never in this file')

    def test_scenario_ids(self):
        self.assertProblem(mini(make_scenario(), make_scenario()), 'duplicate scenario id')
        self.assertProblem(mini(make_scenario('quake-one-001')), 'id must look like')
        self.assertProblem(mini(make_scenario('QUAKE-ONE-1')), 'id must look like')
        self.assertProblem(mini(make_scenario('ALIEN-ONE-001')), 'game code QUAKE-')
        scenario = make_scenario()
        scenario['game'] = 'no_such_game'
        self.assertProblem(mini(scenario), 'unknown game')

    def test_a_game_without_a_scenario_is_refused(self):
        doc = mini()
        doc['games'].append({'id': 'other', 'code': 'OTHER', 'name': 'Other', 'steam_app_id': None, 'platform': 'steam'})
        self.assertProblem(doc, 'has no scenario')

    def test_a_games_window_class_is_a_class_or_a_list_of_them(self):
        def with_class(value):
            doc = mini()
            doc['games'][0]['window_class'] = value
            return doc

        for good in ('quake', 'valheim.x86_64', 'Left 4 Dead 2', 'hl2_linux', ['quake', 'Quake.bin'], ['x']):
            self.assertEqual(self.problems(with_class(good)), [], good)
        for bad in ('', ' quake', 'quake ', 'qu  ake', 'a"b', "it's", 'tab\tclass', 'new\nline', 'back\\slash', 'x' * 81, [], ['a', 'a'],
                    ['a', ''], ['a', 7], [['a']], 7, True, None, {'a': 1}):
            self.assertProblem(with_class(bad), 'window_class')
        self.assertEqual(self.problems(mini()), [])  # the key is optional
        doc = mini()
        doc['games'][0]['window-class'] = 'quake'
        self.assertProblem(doc, "unknown key 'window-class'")

    def test_window_classes_reads_a_string_or_a_list(self):
        self.assertEqual(db.window_classes({'window_class': 'quake'}), ['quake'])
        self.assertEqual(db.window_classes({'window_class': ['a', 'b']}), ['a', 'b'])
        self.assertEqual(db.window_classes({}), [])
        self.assertEqual(db.window_classes({'window_class': None}), [])

    def test_tier_rules(self):
        t2 = make_scenario(tier='T2', device='steam_deck', tv_mode='panel')
        self.assertEqual(self.problems(mini(t2)), [])
        self.assertProblem(mini(make_scenario(tier='T2')), 'tier T2')
        self.assertProblem(mini(make_scenario(tier='T2', device='steam_deck')), 'tier T2')
        self.assertProblem(mini(make_scenario(tv_mode='panel')), 'tier T1 drives an external display')
        self.assertProblem(mini(make_scenario(layout='single', side='guest')), 'only the Main side')

    def test_evidence_level_rules(self):
        self.assertProblem(mini(make_scenario(level='visual') | {'level_required': 'moderate'}), 'level_required')
        no_human = make_scenario()
        no_human['pass_criteria']['human'] = []
        self.assertProblem(mini(no_human), 'at least one human criterion')
        visual = make_scenario(level='visual')
        visual['pass_criteria']['human'] = ['A person looks.']
        self.assertProblem(mini(visual), 'human criteria need the human level')
        synthetic = make_scenario(level='synthetic')
        synthetic['pass_criteria']['automated'] = []
        self.assertProblem(mini(synthetic), 'at least one automated criterion')

    def test_automated_criteria_follow_their_kind(self):
        scenario = make_scenario()
        scenario['pass_criteria']['automated'] = [{'kind': 'telepathy'}]
        self.assertProblem(mini(scenario), 'is not defined in schema.criteria_kinds')
        scenario['pass_criteria']['automated'] = [{'kind': 'process_alive'}]
        self.assertProblem(mini(scenario), 'process_alive needs min_seconds')
        scenario['pass_criteria']['automated'] = [{'kind': 'process_alive', 'min_seconds': 'forty'}]
        self.assertProblem(mini(scenario), 'min_seconds must be a positive integer')
        scenario['pass_criteria']['automated'] = [{'kind': 'process_alive', 'min_seconds': 40, 'extra': 1}]
        self.assertProblem(mini(scenario), "no parameter 'extra'")
        scenario['pass_criteria']['automated'] = [{'kind': 'window_size', 'expect': 'anywhere'}]
        self.assertProblem(mini(scenario), 'expect must be one of')
        scenario['pass_criteria']['automated'] = [{'kind': 'process_alive', 'min_seconds': 40}] * 2
        self.assertProblem(mini(scenario), 'duplicate automated criterion')

    def test_what_the_lock_freezes_has_to_be_hashable(self):
        scenario = make_scenario()
        scenario['profile'] = {'id': 'p', 'version': 1, 'settings': {'ratio': float('inf')}}
        self.assertProblem(mini(scenario), 'cannot be hashed')
        scenario = make_scenario()
        scenario['pass_criteria']['automated'] = [{'kind': 'process_alive', 'min_seconds': 40}, b'bytes']
        self.assertProblem(mini(scenario), 'is not defined in schema.criteria_kinds')  # Reported, not a crash.

    def test_profiles(self):
        scenario = make_scenario()
        scenario['profile'] = {'id': 'quake-modelist', 'version': 1, 'settings': {'emulate_modelist': 'Y'}, 'reason': 'Last resort.'}
        self.assertEqual(self.problems(mini(scenario)), [])
        scenario['profile'] = {'id': 'Quake Profile', 'version': 0, 'settings': {}}
        found = self.problems(mini(scenario))
        self.assertTrue(any('id must be' in p for p in found) and any('version' in p for p in found)
                        and any('settings' in p for p in found), found)

    def test_waivers_must_cite_an_adr(self):
        scenario = make_scenario()
        good = {'scenario': scenario['id'], 'adr': 'ADR-012', 'reason': 'Retired by the decision.', 'date': '2026-10-08'}
        self.assertEqual(self.problems(mini(scenario, waivers=[good])), [])
        for bad, text in (({**good, 'adr': 'ADR-12'}, 'must cite the ADR'), ({**good, 'adr': 'because I said so'}, 'must cite the ADR'),
                          ({k: v for k, v in good.items() if k != 'adr'}, 'must cite the ADR'),
                          ({**good, 'reason': ''}, 'needs a reason'), ({**good, 'scenario': 'QUAKE-NOPE-001'}, 'unknown scenario'),
                          ({**good, 'candidates': ['abc']}, 'candidates must be'), ({**good, 'date': 'yesterday'}, 'needs a date')):
            self.assertProblem(mini(scenario, waivers=[bad]), text)

    def test_waiver_adrs_can_be_checked_against_the_decision_record(self):
        scenario = make_scenario()
        waiver = {'scenario': scenario['id'], 'adr': 'ADR-099', 'reason': 'Retired.', 'date': '2026-10-08'}
        doc = mini(scenario, waivers=[waiver])
        self.assertEqual(db.waiver_adr_problems(doc, {'ADR-012'}), ['waiver for QUAKE-ONE-001: ADR-099 is not in the decision record'])
        self.assertEqual(db.waiver_adr_problems(doc, {'ADR-099'}), [])

    def test_a_repeated_yaml_key_is_an_error_not_a_silent_loss(self):
        with self.assertRaises(db.SchemaError) as caught:
            db.load_yaml_text('human:\n  - one\nhuman:\n  - two\n')
        self.assertIn('duplicate key', str(caught.exception))

    def test_invariant_links_must_resolve(self):
        contracts = {'steam_session': {'file': 'x', 'required': [
            {'kind': 'env', 'name': 'PROTON_USE_WAYLAND', 'value': '0'},
            {'kind': 'command-contains', 'command': 'gamescope', 'contains': '--borderless'},
            {'kind': 'key-value', 'key': 'Nice', 'value': '-10'}, {'kind': 'text', 'contains': 'some text'}]}}
        doc = mini()
        doc['games'][0]['invariants'] = [{'contract': 'steam_session', 'requirements': ['PROTON_USE_WAYLAND', 'gamescope:--borderless',
                                                                                           'Nice', 'some text']}]
        self.assertEqual(db.validate_scenarios(doc, contracts), [])
        doc['games'][0]['invariants'] = [{'contract': 'steam_session', 'requirements': ['NOT_THERE']}]
        self.assertTrue(any('NOT_THERE' in p for p in db.validate_scenarios(doc, contracts)))
        doc['games'][0]['invariants'] = [{'contract': 'no_such_contract'}]
        self.assertTrue(any('unknown configuration invariant' in p for p in db.validate_scenarios(doc, contracts)))

    def test_the_vocabulary_has_to_offer_what_the_rules_use(self):
        doc = mini()
        del doc['schema']['vocabulary']['tv_mode']['panel']
        self.assertProblem(doc, "must offer 'panel'")
        doc = mini()
        doc['schema']['vocabulary']['tier'] = {'T1': 'T1'}
        self.assertProblem(doc, 'fixed by ADR-002')


class HashTests(unittest.TestCase):
    YAML_A = """
id: QUAKE-ONE-001
game: quake
title: A
setup: {device: steam_machine, tier: T1, preset: native}
pass_criteria:
  automated:
    - {kind: process_alive, min_seconds: 40}
  human: ["The game   starts and reaches its menu."]
profile: null
"""
    YAML_B = """
profile: null
pass_criteria:
  human:
    - >-
      The game starts
      and reaches its menu.
  automated:
    - {min_seconds: 40.0, kind: process_alive}
setup: {preset: native, tier: T1, device: steam_machine}
title: B, which is not part of the definition
game: quake
id: QUAKE-ONE-001
"""

    def test_yaml_key_order_wrapping_and_number_style_do_not_change_the_hash(self):
        a, b = db.load_yaml_text(self.YAML_A), db.load_yaml_text(self.YAML_B)
        self.assertNotEqual(list(a), list(b))
        self.assertEqual(db.scenario_hash(a), db.scenario_hash(b))

    def test_canonical_form_in_memory(self):
        one = {'b': [{'y': 1, 'x': 2.0}], 'a': 'two  words'}
        two = {'a': 'two words', 'b': [{'x': 2, 'y': 1}]}
        self.assertEqual(db.canonical_json(one), db.canonical_json(two))
        self.assertEqual(db.canonical_json({'d': dt.date(2026, 10, 8)}), '{"d":"2026-10-08"}')
        with self.assertRaises(ValueError):
            db.canonicalize(float('nan'))

    def test_list_order_is_part_of_the_definition(self):
        scenario = make_scenario()
        scenario['pass_criteria']['automated'].append({'kind': 'geometry_stable', 'samples': 5, 'interval_s': 1})
        flipped = copy.deepcopy(scenario)
        flipped['pass_criteria']['automated'].reverse()
        self.assertNotEqual(db.scenario_hash(scenario), db.scenario_hash(flipped))

    def test_everything_that_decides_acceptance_changes_the_hash(self):
        base = make_scenario()
        digest = db.scenario_hash(base)
        for mutate in (
                lambda s: s['setup'].update(layout='single'), lambda s: s['setup'].update(tier='T2'),
                lambda s: s['setup'].update(preset='4:3'), lambda s: s['setup'].update(device='steam_deck'),
                lambda s: s['pass_criteria']['automated'][0].update(min_seconds=10),
                lambda s: s['pass_criteria']['automated'].append({'kind': 'mode_listed', 'mode': 'native'}),
                lambda s: s['pass_criteria']['human'].__setitem__(0, 'The game starts.'),
                lambda s: s['pass_criteria']['human'].clear(),
                lambda s: s.update(profile={'id': 'p', 'version': 1, 'settings': {'a': 1}}),
                lambda s: s.update(game='alien_breed')):
            changed = copy.deepcopy(base)
            mutate(changed)
            self.assertNotEqual(db.scenario_hash(changed), digest)

    def test_descriptions_do_not_change_the_hash(self):
        base = make_scenario()
        digest = db.scenario_hash(base)
        for mutate in (lambda s: s.update(title='Another title'), lambda s: s.update(introduced=dt.date(2026, 1, 1)),
                       lambda s: s.update(known_issue='Something.'), lambda s: s.update(source='Elsewhere.'),
                       lambda s: s.update(notes=['A note.']), lambda s: s.update(level_required='visual')):
            changed = copy.deepcopy(base)
            mutate(changed)
            self.assertEqual(db.scenario_hash(changed), digest)

    def test_the_hash_of_a_fixed_scenario_never_moves(self):
        # Locked hashes outlive this code. Changing the canonical form would silently unlock every scenario,
        # so a change that moves this value has to be a deliberate, reviewed decision with a migration.
        self.assertEqual(db.scenario_hash(make_scenario()),
                         '6f28ca0111352fd964409c8e4a67d67e39115b71191a0b39278fcdc8d0413b4d')


class LockTests(unittest.TestCase):
    def test_an_empty_lock_is_valid_and_comments_are_ignored(self):
        entries, problems = db.parse_lock('# header\n\n   # indented comment\n')
        self.assertEqual((entries, problems), ([], []))
        self.assertEqual(db.lock_problems([], mini()), [])

    def test_malformed_lines(self):
        digest = 'a' * 64
        for line in ('QUAKE-ONE-001 abc 2026-10-08 human', f'QUAKE-ONE-001 {digest} 2026-13-45 human', f'QUAKE-ONE-001 {digest} 2026-10-08 godlike',
                     f'quake {digest} 2026-10-08 human', f'QUAKE-ONE-001 {digest} 2026-10-08', f'QUAKE-ONE-001 {digest} 2026-10-08 human # note'):
            entries, problems = db.parse_lock(line + '\n')
            self.assertEqual(entries, [], line)
            self.assertEqual(len(problems), 1, line)

    def test_a_matching_entry_is_valid_and_the_helper_line_round_trips(self):
        scenario = make_scenario()
        line = db.format_lock_line(scenario, date='2026-10-08')
        entries, problems = db.parse_lock('# header\n' + line + '\n')
        self.assertEqual(problems, [])
        self.assertEqual(db.lock_problems(entries, mini(scenario)), [])
        entry = entries[0]
        self.assertEqual((entry.scenario, entry.sha256, entry.date, entry.level),
                         (scenario['id'], db.scenario_hash(scenario), '2026-10-08', 'human'))

    def test_weakening_a_locked_scenario_fails(self):
        scenario = make_scenario()
        entries = [lock_entry(scenario)]
        weaker = copy.deepcopy(scenario)
        weaker['pass_criteria']['human'] = []
        weaker['pass_criteria']['automated'][0]['min_seconds'] = 1
        self.assertTrue(any('changed after the scenario was locked' in p for p in db.lock_problems(entries, mini(weaker))))
        for mutate in (lambda s: s['setup'].update(layout='single'), lambda s: s['setup'].update(tier='T2'),
                       lambda s: s['pass_criteria']['human'].append('One more thing.')):
            changed = copy.deepcopy(scenario)
            mutate(changed)
            self.assertNotEqual(db.lock_problems(entries, mini(changed)), [])

    def test_a_title_or_note_can_still_change(self):
        scenario = make_scenario()
        entries = [lock_entry(scenario)]
        scenario['title'] = 'A clearer title'
        scenario['notes'] = ['More context.']
        self.assertEqual(db.lock_problems(entries, mini(scenario)), [])

    def test_removing_a_locked_scenario_fails(self):
        scenario = make_scenario()
        other = make_scenario('QUAKE-TWO-001')
        problems = db.lock_problems([lock_entry(scenario)], mini(other))
        self.assertTrue(any('was removed' in p for p in problems), problems)

    def test_locking_twice_fails(self):
        scenario = make_scenario()
        problems = db.lock_problems([lock_entry(scenario), lock_entry(scenario, date='2026-10-09')], mini(scenario))
        self.assertTrue(any('locked twice' in p for p in problems), problems)

    def test_the_required_level_can_rise_but_not_fall(self):
        # The level is not part of the hash, so a level change is the only edit that the hash cannot see.
        visual = make_scenario(level='visual')
        raised = copy.deepcopy(visual)
        raised['level_required'] = 'human'
        self.assertEqual(db.lock_problems([lock_entry(visual)], mini(raised)), [])
        human = make_scenario()
        lowered = copy.deepcopy(human)
        lowered['level_required'] = 'visual'
        problems = db.lock_problems([lock_entry(human)], mini(lowered))
        self.assertTrue(any('lowered from human to visual' in p for p in problems), problems)


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class LockHistoryTests(unittest.TestCase):
    """The lock never shrinks: every line an earlier commit had must still be there."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-lock-')
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.repo = self.parent / 'repo'
        (self.repo / 'compatibility').mkdir(parents=True)
        # A hermetic git: the surrounding checkout (hooks may export GIT_DIR) and the user's config stay out of it.
        patch = mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.parent), 'GIT_CONFIG_GLOBAL': os.devnull,
                                             'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'tester@localhost',
                                             'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'tester@localhost'})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            os.environ.pop(name, None)
        self.git('init', '-q')
        self.lock = self.repo / 'compatibility/accepted.lock'
        self.one, self.two = make_scenario('QUAKE-ONE-001'), make_scenario('QUAKE-TWO-001')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                       check=True, capture_output=True)

    def commit(self, text):
        self.lock.write_text(text)
        self.git('add', 'compatibility/accepted.lock')
        self.git('commit', '-q', '-m', 'lock')

    def check(self):
        return db.lock_history_problems(db.parse_lock(self.lock.read_text())[0], self.repo)

    def lines(self):
        return db.format_lock_line(self.one, date='2026-10-08') + '\n', db.format_lock_line(self.two, date='2026-10-09') + '\n'

    def test_appending_is_fine(self):
        first, second = self.lines()
        self.commit('# header\n' + first)
        self.lock.write_text('# header\n' + first + second)
        self.assertEqual(self.check(), ([], None))
        self.commit('# header\n' + first + second)
        self.assertEqual(self.check(), ([], None))

    def test_removing_a_line_fails_even_before_it_is_committed(self):
        first, second = self.lines()
        self.commit('# header\n' + first + second)
        self.lock.write_text('# header\n' + first)
        problems, note = self.check()
        self.assertIsNone(note)
        self.assertTrue(any('QUAKE-TWO-001' in p and 'append-only' in p for p in problems), problems)

    def test_removing_a_line_in_a_later_commit_still_fails(self):
        first, second = self.lines()
        self.commit('# header\n' + first + second)
        self.commit('# header\n' + first)
        problems, _ = self.check()
        self.assertTrue(any('QUAKE-TWO-001' in p for p in problems), problems)

    def test_editing_a_line_fails(self):
        first, _ = self.lines()
        self.commit('# header\n' + first)
        weakened = first.replace('human', 'visual')
        self.lock.write_text('# header\n' + weakened)
        problems, _ = self.check()
        self.assertTrue(any('QUAKE-ONE-001' in p for p in problems), problems)

    def test_a_tree_without_history_reports_that_it_could_not_check(self):
        bare = self.parent / 'staged'
        (bare / 'compatibility').mkdir(parents=True)
        (bare / 'compatibility/accepted.lock').write_text('# header\n')
        problems, note = db.lock_history_problems([], bare)
        self.assertEqual(problems, [])
        self.assertIn('no git history', note)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.scenario = make_scenario()
        self.doc = mini(self.scenario)

    def problems(self, record, **kw):
        return db.validate_record(record, self.doc, NOW, **kw)

    def assertProblem(self, record, text, **kw):
        found = self.problems(record, **kw)
        self.assertTrue(any(text in p for p in found), f'{text!r} not in {found}')

    def test_a_complete_record_is_valid(self):
        self.assertEqual(self.problems(make_record(self.scenario)), [])

    def test_every_required_field_is_required(self):
        record = make_record(self.scenario)
        for field in list(record):
            incomplete = {k: v for k, v in record.items() if k != field}
            self.assertProblem(incomplete, field)

    def test_unknown_fields_are_refused(self):
        self.assertProblem(make_record(self.scenario, hostname='steamdeck'), 'is not a known field')

    def test_notes_are_optional_text(self):
        self.assertEqual(self.problems(make_record(self.scenario, notes='The cable was loose in run one.')), [])
        self.assertProblem(make_record(self.scenario, notes=''), 'notes: must be a text')

    def test_blocked_and_not_applicable_results_need_a_reason(self):
        for result in ('blocked', 'not_applicable'):
            self.assertProblem(make_record(self.scenario, result=result), 'reason: is required')
            self.assertProblem(make_record(self.scenario, result=result, reason='  '), 'reason: is required')
            self.assertEqual(self.problems(make_record(self.scenario, result=result, reason='No Steam Machine was available.')), [])
        for result in ('pass', 'fail'):
            self.assertEqual(self.problems(make_record(self.scenario, result=result)), [])
        self.assertProblem(make_record(self.scenario, result='maybe'), 'result')

    def test_unfilled_template_values_are_refused(self):
        self.assertProblem(make_record(self.scenario, observations='FILL-IN'), 'observations')
        template = db.record_template(self.scenario)
        self.assertTrue(len(self.problems(template)) > 10)

    def test_candidate_identifiers_must_be_complete(self):
        self.assertProblem(make_record(self.scenario, source_revision=CANDIDATE[:8]), 'full 40-digit')
        self.assertProblem(make_record(self.scenario, candidate_sha256=BUNDLE[:12]), 'full 64-digit')
        self.assertProblem(make_record(self.scenario, candidate_sha256=BUNDLE.upper()), 'full 64-digit')

    def test_timestamps(self):
        self.assertProblem(make_record(self.scenario, timestamp='2026-10-07 12:00'), 'must be UTC')
        self.assertProblem(make_record(self.scenario, timestamp='2026-10-7T12:00:00Z'), 'must be UTC')  # The file name comes from it.
        self.assertProblem(make_record(self.scenario, timestamp='2026-02-30T12:00:00Z'), 'must be UTC')
        future = make_record(self.scenario, timestamp='2026-10-09T12:00:00Z')
        self.assertProblem(future, 'in the future')
        self.assertEqual(self.problems(future, current=False), [])  # A stored record is not judged by today's clock.

    def test_the_device_baseline_is_mandatory_and_typed(self):
        self.assertProblem(make_record(self.scenario, baseline=None), 'baseline')
        for field in ('root_readonly', 'decky', 'sudoers_dropin'):
            broken = dict(STOCK)
            del broken[field]
            self.assertProblem(make_record(self.scenario, baseline=broken), f'baseline.{field}')
            self.assertProblem(make_record(self.scenario, baseline={**STOCK, field: 'yes'}), f'baseline.{field}')
        broken = dict(STOCK)
        del broken['extra_users']
        self.assertProblem(make_record(self.scenario, baseline=broken), 'baseline.extra_users')
        self.assertProblem(make_record(self.scenario, baseline={**STOCK, 'extra_users': 'guest'}), 'baseline.extra_users')
        self.assertProblem(make_record(self.scenario, baseline={**STOCK, 'wifi': True}), 'unknown field')

    def test_the_engineering_deck_is_not_a_stock_device(self):
        engineering = {'root_readonly': False, 'extra_users': ['user1', 'user2', 'guest'], 'decky': True, 'sudoers_dropin': True}
        self.assertEqual(self.problems(make_record(self.scenario, baseline=engineering)), [])
        self.assertEqual(db.baseline_class(engineering), 'engineering')
        self.assertEqual(db.baseline_class(STOCK), 'stock')
        for field, value in (('root_readonly', False), ('extra_users', ['guest']), ('decky', True), ('sudoers_dropin', True)):
            self.assertEqual(db.baseline_class({**STOCK, field: value}), 'engineering', field)

    def test_the_setup_names_what_was_run(self):
        record = make_record(self.scenario)
        self.assertProblem({**record, 'setup': {**record['setup'], 'device': 'any'}}, 'never "any"')
        self.assertProblem({**record, 'setup': {**record['setup'], 'controller': 'any'}}, 'never "any"')
        self.assertProblem({**record, 'setup': {**record['setup'], 'layout': 'diagonal'}}, 'setup.layout')
        self.assertProblem({**record, 'setup': {**record['setup'], 'tier': 'T2'}}, 'tier T2')
        self.assertProblem({**record, 'setup': {**record['setup'], 'colour': 'red'}}, 'unknown field')
        self.assertProblem({**record, 'steam_game_resolution_setting': 'custom:1280x720'}, 'disagrees with setup')
        custom = {**record, 'steam_game_resolution_setting': 'custom:1280x720', 'setup': {**record['setup'], 'steam_game_resolution': 'custom'}}
        self.assertEqual(self.problems(custom), [])

    def test_the_evidence_reference_stays_inside_the_private_evidence(self):
        for ref in ('/srv/evidence/frame.png', '../elsewhere/frame.png', 'https://example.org/frame.png'):
            self.assertProblem(make_record(self.scenario, evidence_ref=ref), 'evidence_ref')

    def test_controllers_must_be_named_unless_none_was_used(self):
        self.assertProblem(make_record(self.scenario, controller_identities=[]), 'controller_identities')
        none = make_scenario(controller='none')
        self.assertEqual(db.validate_record(make_record(none, controller_identities=[]), mini(none), NOW), [])

    def test_a_record_must_belong_to_the_scenario_as_it_is_now(self):
        stale = make_record(self.scenario, criteria_sha256='0' * 64)
        self.assertProblem(stale, 'not the current hash')
        self.assertEqual(self.problems(stale, current=False), [])
        self.assertProblem(make_record(self.scenario, scenario='QUAKE-NOPE-001'), 'not defined')
        self.assertEqual(self.problems(make_record(self.scenario, scenario='QUAKE-NOPE-001'), current=False), [])

    def test_only_a_pass_with_a_reason_can_acknowledge_failures(self):
        self.assertProblem(make_record(self.scenario, reruns=['run-0000']), 'only a pass with a reason')
        self.assertEqual(self.problems(make_record(self.scenario, reruns=['run-0000'], reason='The first run had a loose cable.')), [])
        self.assertProblem(make_record(self.scenario, result='fail', reason='x', reruns=['run-0000']), 'only a pass')

    def test_the_template_carries_the_known_fields(self):
        scenario = make_scenario(device='any')
        template = db.record_template(scenario)
        self.assertEqual(template['scenario'], scenario['id'])
        self.assertEqual(template['criteria_sha256'], db.scenario_hash(scenario))
        self.assertEqual(template['level'], 'human')
        self.assertEqual(template['setup']['device'], db.PLACEHOLDER)
        self.assertEqual(template['setup']['preset'], 'native')


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-results-')
        self.addCleanup(temp.cleanup)
        self.store = Path(temp.name) / 'results'
        self.scenario = make_scenario()
        self.doc = mini(self.scenario)

    def append(self, record, **kw):
        return db.append_result(record, self.store, scenarios=self.doc, now=NOW, **kw)

    def stored(self):
        return db.read_results(self.store)

    def test_a_record_is_appended_as_one_line_and_read_back(self):
        record = make_record(self.scenario)
        path = self.append(record)
        self.assertEqual(path.name, '2026-10-07.jsonl')
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.assertEqual(self.stored(), ([record], []))

    def test_nothing_is_written_for_an_invalid_record(self):
        with self.assertRaises(db.RecordError) as caught:
            self.append(make_record(self.scenario, result='blocked'))
        self.assertIn('reason', str(caught.exception))
        self.assertFalse(list(self.store.glob('*.jsonl')) if self.store.exists() else [])

    def test_a_run_id_can_only_be_used_once(self):
        self.append(make_record(self.scenario))
        with self.assertRaises(db.RecordError) as caught:
            self.append(make_record(self.scenario, observations='A different story.'))
        self.assertIn('already stored', str(caught.exception))
        self.assertEqual(len(self.stored()[0]), 1)

    def test_appending_never_rewrites_what_is_there(self):
        first = self.append(make_record(self.scenario, run_id='run-0001'))
        before = first.read_bytes()
        self.append(make_record(self.scenario, run_id='run-0002', result='fail', observations='It crashed.'))
        self.assertEqual(first.read_bytes()[:len(before)], before)
        self.assertEqual(len(first.read_text().splitlines()), 2)

    def test_a_file_without_a_final_newline_does_not_merge_two_records(self):
        path = self.append(make_record(self.scenario, run_id='run-0001'))
        path.write_text(path.read_text().rstrip('\n'))
        self.append(make_record(self.scenario, run_id='run-0002'))
        self.assertEqual(len(self.stored()[0]), 2)

    def test_the_store_defaults_to_the_environment(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DOSPLEX_COMPAT_RESULTS', None)
            with self.assertRaises(db.RecordError):
                db.append_result(make_record(self.scenario), scenarios=self.doc, now=NOW)
        with mock.patch.dict(os.environ, {'DOSPLEX_COMPAT_RESULTS': str(self.store)}):
            db.append_result(make_record(self.scenario), scenarios=self.doc, now=NOW)
        self.assertEqual(len(self.stored()[0]), 1)

    def test_a_failure_is_kept_and_a_rerun_has_to_name_it(self):
        self.append(make_record(self.scenario, run_id='run-fail', result='fail', observations='It crashed at the menu.'))
        with self.assertRaises(db.RecordError):
            self.append(make_record(self.scenario, run_id='run-ok', reruns=['run-missing'], reason='Cable fixed.'))
        self.append(make_record(self.scenario, run_id='run-ok', reruns=['run-fail'], reason='A loose cable caused the crash.'))
        records, _ = self.stored()
        self.assertEqual([r['run_id'] for r in records], ['run-fail', 'run-ok'])

    def test_a_rerun_cannot_name_a_failure_of_another_candidate(self):
        self.append(make_record(self.scenario, run_id='run-fail', result='fail', observations='It crashed.',
                                source_revision=OTHER_CANDIDATE, candidate_sha256=OTHER_BUNDLE))
        with self.assertRaises(db.RecordError):
            self.append(make_record(self.scenario, run_id='run-ok', reruns=['run-fail'], reason='Fixed.'))

    def test_damaged_lines_are_reported_and_block_appending(self):
        path = self.append(make_record(self.scenario))
        with open(path, 'a') as out:
            out.write('{not json\n[1, 2]\n')
        records, problems = self.stored()
        self.assertEqual(len(records), 1)
        self.assertEqual(len(problems), 2)
        with self.assertRaises(db.RecordError):
            self.append(make_record(self.scenario, run_id='run-0002'))

    def test_the_whole_store_can_be_checked(self):
        self.append(make_record(self.scenario, run_id='run-0001'))
        records, _ = self.stored()
        self.assertEqual(db.results_problems(records, self.doc, NOW), [])
        self.assertTrue(any('used twice' in p for p in db.results_problems(records + records, self.doc, NOW)))
        broken = copy.deepcopy(records[0])
        broken['run_id'] = 'run-0002'
        del broken['baseline']
        self.assertTrue(any('baseline' in p for p in db.results_problems(records + [broken], self.doc, NOW)))

    def test_editing_a_scenario_outdates_old_results_without_corrupting_them(self):
        self.append(make_record(self.scenario))
        edited = copy.deepcopy(self.scenario)
        edited['pass_criteria']['human'].append('A new requirement.')
        records, _ = self.stored()
        self.assertEqual(db.results_problems(records, mini(edited), NOW), [])


class GateTests(unittest.TestCase):
    """The gate on fixtures: pass, missing, wrong candidate, wrong tier, wrong level and all failing."""

    def row(self, report, scenario):
        return next(r for r in report.rows if r.scenario == scenario['id'])

    def test_a_pass_on_the_candidate_passes(self):
        scenario = make_scenario()
        report = gate([scenario], [make_record(scenario)])
        self.assertTrue(report.ok)
        self.assertEqual(self.row(report, scenario).status, 'pass')
        self.assertEqual(self.row(report, scenario).run_id, 'run-0001')

    def test_a_missing_result_fails(self):
        scenario = make_scenario()
        report = gate([scenario], [])
        self.assertFalse(report.ok)
        self.assertEqual(self.row(report, scenario).status, 'missing')

    def test_a_pass_on_another_candidate_does_not_count(self):
        scenario = make_scenario()
        other = make_record(scenario, source_revision=OTHER_CANDIDATE, candidate_sha256=OTHER_BUNDLE)
        report = gate([scenario], [other])
        self.assertFalse(report.ok)
        self.assertEqual(self.row(report, scenario).status, 'missing')
        self.assertTrue(gate([scenario], [other], candidate=OTHER_CANDIDATE).ok)

    def test_a_t2_result_never_satisfies_a_t1_scenario(self):
        scenario = make_scenario()
        t2_setup = {**concrete(scenario['setup']), 'tier': 'T2', 'device': 'steam_deck', 'tv_mode': 'panel'}
        report = gate([scenario], [make_record(scenario, setup=t2_setup)])
        self.assertFalse(report.ok)
        self.assertIn('T2 result cannot satisfy a T1 scenario', self.row(report, scenario).detail)

    def test_a_t1_result_never_satisfies_a_t2_scenario(self):
        scenario = make_scenario(tier='T2', device='steam_deck', tv_mode='panel')
        t1_setup = {**concrete(scenario['setup']), 'tier': 'T1', 'tv_mode': '2160p60'}
        report = gate([scenario], [make_record(scenario, setup=t1_setup)])
        self.assertFalse(report.ok)
        self.assertIn('T1 result cannot satisfy a T2 scenario', self.row(report, scenario).detail)
        self.assertTrue(gate([scenario], [make_record(scenario)]).ok)

    def test_a_lower_evidence_level_never_satisfies_a_higher_one(self):
        human = make_scenario()
        for level in ('synthetic', 'visual'):
            report = gate([human], [make_record(human, level=level)])
            self.assertFalse(report.ok, level)
            self.assertIn(f'{level} evidence is below the required human level', self.row(report, human).detail)
        visual = make_scenario('QUAKE-VIS-001', level='visual')
        self.assertFalse(gate([visual], [make_record(visual, level='synthetic')]).ok)

    def test_a_higher_evidence_level_satisfies_a_lower_one(self):
        visual = make_scenario('QUAKE-VIS-001', level='visual')
        self.assertTrue(gate([visual], [make_record(visual, level='human')]).ok)
        synthetic = make_scenario('QUAKE-SYN-001', level='synthetic')
        self.assertTrue(gate([synthetic], [make_record(synthetic, level='visual')]).ok)

    def test_blocked_and_not_applicable_are_not_passes(self):
        scenario = make_scenario()
        for result in ('blocked', 'not_applicable'):
            report = gate([scenario], [make_record(scenario, result=result, reason='The Steam Machine was not available.')])
            self.assertFalse(report.ok)
            self.assertEqual(self.row(report, scenario).status, 'missing')
            self.assertIn('not available', self.row(report, scenario).detail)

    def test_all_failing(self):
        scenarios = [make_scenario('QUAKE-ONE-001'), make_scenario('QUAKE-TWO-001', layout='single'),
                     make_scenario('QUAKE-THREE-001', preset='4:3')]
        records = [make_record(scenarios[0], result='fail', observations='It crashed.'), make_record(scenarios[1], run_id='run-0002',
                   level='synthetic')]
        report = gate(scenarios, records)
        self.assertFalse(report.ok)
        self.assertEqual([r.status for r in report.rows], ['fail', 'missing', 'missing'])
        self.assertEqual(report.count('pass'), 0)

    def test_a_failure_on_the_candidate_counts_against_it(self):
        scenario = make_scenario()
        report = gate([scenario], [make_record(scenario, result='fail', observations='It crashed.')])
        self.assertEqual(self.row(report, scenario).status, 'fail')

    def test_a_later_pass_does_not_erase_a_failure(self):
        scenario = make_scenario()
        failed = make_record(scenario, run_id='run-0001', result='fail', observations='It crashed.', timestamp='2026-10-07T10:00:00Z')
        passed = make_record(scenario, run_id='run-0002', timestamp='2026-10-07T11:00:00Z')
        report = gate([scenario], [failed, passed])
        self.assertEqual(self.row(report, scenario).status, 'fail')
        self.assertIn('must list it under reruns', self.row(report, scenario).detail)

    def test_a_pass_that_names_the_failure_explains_it(self):
        scenario = make_scenario()
        failed = make_record(scenario, run_id='run-0001', result='fail', observations='It crashed.', timestamp='2026-10-07T10:00:00Z')
        passed = make_record(scenario, run_id='run-0002', timestamp='2026-10-07T11:00:00Z', reruns=['run-0001'],
                             reason='The first run had a loose cable.')
        self.assertTrue(gate([scenario], [failed, passed]).ok)
        earlier_pass = make_record(scenario, run_id='run-0003', timestamp='2026-10-07T09:00:00Z', reruns=['run-0001'], reason='x')
        self.assertFalse(gate([scenario], [failed, earlier_pass]).ok)  # A pass cannot explain a failure that came after it.

    def test_a_failure_after_a_pass_still_fails(self):
        scenario = make_scenario()
        passed = make_record(scenario, run_id='run-0001', timestamp='2026-10-07T10:00:00Z')
        failed = make_record(scenario, run_id='run-0002', result='fail', observations='It crashed.', timestamp='2026-10-07T11:00:00Z')
        self.assertFalse(gate([scenario], [passed, failed]).ok)

    def test_a_failure_in_another_setup_is_not_a_failure_of_this_scenario(self):
        scenario = make_scenario()
        elsewhere = make_record(scenario, run_id='run-0001', result='fail', observations='It crashed.',
                                setup={**concrete(scenario['setup']), 'layout': 'single'})
        self.assertTrue(gate([scenario], [elsewhere, make_record(scenario, run_id='run-0002')]).ok)

    def test_a_result_for_other_pass_criteria_does_not_count(self):
        scenario = make_scenario()
        old = make_record(scenario, criteria_sha256='0' * 64)
        report = gate([scenario], [old])
        self.assertFalse(report.ok)
        self.assertIn('other pass criteria', self.row(report, scenario).detail)

    def test_the_setup_must_match(self):
        scenario = make_scenario()
        for field, value in (('layout', 'single'), ('preset', '4:3'), ('tv_mode', '1080p60'), ('side', 'guest'), ('backend', 'drm'),
                             ('presentation', 'stretch'), ('window_mode', 'borderless'), ('steam_input', 'enabled')):
            record = make_record(scenario, setup={**concrete(scenario['setup']), field: value})
            report = gate([scenario], [record])
            self.assertFalse(report.ok, field)
            self.assertIn(f'{field} was {value}', self.row(report, scenario).detail)

    def test_the_device_must_match_unless_the_scenario_accepts_any(self):
        machine = make_scenario(device='steam_machine')
        on_deck = make_record(machine, setup={**concrete(machine['setup']), 'device': 'steam_deck'})
        self.assertFalse(gate([machine], [on_deck]).ok)
        anywhere = make_scenario('QUAKE-ANY-001', device='any')
        self.assertTrue(gate([anywhere], [make_record(anywhere)]).ok)
        self.assertTrue(gate([anywhere], [make_record(anywhere, setup={**concrete(anywhere['setup']), 'device': 'steam_deck'})]).ok)

    def test_any_controller_accepts_a_named_controller(self):
        scenario = make_scenario(controller='any')
        record = make_record(scenario, setup={**concrete(scenario['setup']), 'controller': 'dualsense'})
        self.assertTrue(gate([scenario], [record]).ok)
        strict = make_scenario('QUAKE-STRICT-001', controller='steam_controller')
        self.assertFalse(gate([strict], [make_record(strict, setup={**concrete(strict['setup']), 'controller': 'dualsense'})]).ok)

    def test_a_pass_that_is_not_sound_cannot_open_the_gate(self):
        scenario = make_scenario()
        unsound = make_record(scenario)
        del unsound['baseline']
        report = gate([scenario], [unsound])
        self.assertFalse(report.ok)
        self.assertIn('invalid record', self.row(report, scenario).detail)

    def test_a_waiver_with_an_adr_excuses_a_scenario(self):
        scenario = make_scenario()
        waiver = {'scenario': scenario['id'], 'adr': 'ADR-012', 'reason': 'Retired by the decision.', 'date': '2026-10-08'}
        report = gate([scenario], [], waivers=[waiver])
        self.assertTrue(report.ok)
        self.assertEqual(self.row(report, scenario).status, 'waived')
        self.assertIn('ADR-012', self.row(report, scenario).detail)

    def test_a_waiver_can_be_limited_to_a_candidate(self):
        scenario = make_scenario()
        waiver = {'scenario': scenario['id'], 'adr': 'ADR-012', 'reason': 'Skipped for this candidate.', 'date': '2026-10-08',
                  'candidates': [CANDIDATE[:10]]}
        self.assertTrue(gate([scenario], [], waivers=[waiver]).ok)
        self.assertTrue(gate([scenario], [], waivers=[waiver], candidate=CANDIDATE[:8]).ok)
        self.assertFalse(gate([scenario], [], waivers=[waiver], candidate=OTHER_CANDIDATE).ok)
        by_bundle = {**waiver, 'candidates': [BUNDLE[:12]]}
        self.assertTrue(gate([scenario], [make_record(scenario, result='fail', observations='x')], waivers=[by_bundle]).ok)

    def test_a_waiver_for_another_scenario_excuses_nothing(self):
        one, two = make_scenario('QUAKE-ONE-001'), make_scenario('QUAKE-TWO-001', layout='single')
        waiver = {'scenario': one['id'], 'adr': 'ADR-012', 'reason': 'Retired.', 'date': '2026-10-08'}
        report = gate([one, two], [], waivers=[waiver])
        self.assertEqual([r.status for r in report.rows], ['waived', 'missing'])

    def test_a_locked_definition_that_was_edited_fails(self):
        scenario = make_scenario()
        entry = lock_entry(scenario)
        weaker = copy.deepcopy(scenario)
        weaker['pass_criteria']['human'] = ['The game starts.']
        report = gate([weaker], [make_record(weaker)], entries=[entry])
        self.assertFalse(report.ok)
        self.assertIn('edited', report.rows[0].detail)

    def test_a_locked_scenario_that_is_gone_fails(self):
        gone, other = make_scenario(), make_scenario('QUAKE-TWO-001')
        report = gate([other], [make_record(other)], entries=[lock_entry(gone), lock_entry(other)])
        self.assertEqual([r.status for r in report.rows], ['fail', 'pass'])

    def test_lowering_the_required_level_fails(self):
        human = make_scenario()
        entry = lock_entry(human)
        lowered = copy.deepcopy(human)
        lowered['level_required'] = 'visual'
        report = gate([lowered], [make_record(lowered, level='visual')], entries=[entry])
        self.assertFalse(report.ok)
        self.assertIn('lowered', report.rows[0].detail)

    def test_an_empty_lock_gates_nothing_and_says_so(self):
        report = gate([make_scenario()], [], entries=[])
        self.assertTrue(report.ok)
        self.assertEqual(report.rows, [])
        self.assertIn('nothing is gated', report.notes[0])

    def test_the_gate_can_be_limited_to_one_tier(self):
        t1, t2 = make_scenario('QUAKE-ONE-001'), make_scenario('QUAKE-TWO-001', tier='T2', device='steam_deck', tv_mode='panel')
        records = [make_record(t1)]
        self.assertEqual([r.scenario for r in gate([t1, t2], records, tier='T1').rows], ['QUAKE-ONE-001'])
        self.assertTrue(gate([t1, t2], records, tier='T1').ok)
        self.assertFalse(gate([t1, t2], records, tier='T2').ok)
        self.assertFalse(gate([t1, t2], records).ok)

    def test_a_candidate_is_a_revision_or_bundle_prefix(self):
        scenario = make_scenario()
        record = make_record(scenario)
        for candidate in (CANDIDATE, CANDIDATE[:7], CANDIDATE.upper(), BUNDLE, BUNDLE[:9]):
            self.assertTrue(gate([scenario], [record], candidate=candidate).ok, candidate)
        for candidate in (OTHER_CANDIDATE, CANDIDATE[:6][::-1] + 'ffff'):
            self.assertFalse(gate([scenario], [record], candidate=candidate).ok, candidate)
        for short in ('abc123', 'a1b2c3', 'not-hex!', ''):
            with self.assertRaises(ValueError):
                db.candidate_matches(record, short)

    def test_an_ambiguous_prefix_is_detected(self):
        scenario = make_scenario()
        one = make_record(scenario, run_id='run-0001', source_revision='abcdef1' + '0' * 33, candidate_sha256='1' * 64)
        two = make_record(scenario, run_id='run-0002', source_revision='abcdef1' + '9' * 33, candidate_sha256='2' * 64)
        self.assertEqual(len(db.ambiguous_candidate([one, two], 'abcdef1')), 2)
        self.assertEqual(db.ambiguous_candidate([one, two], 'abcdef10'), [])
        self.assertEqual(db.ambiguous_candidate([one, one], 'abcdef1'), [])

    def test_the_real_lock_with_real_scenarios_and_no_results(self):
        report = db.evaluate_gate(REAL_DOC, db.load_lock(), [], CANDIDATE)
        self.assertEqual(report.count('pass'), 0)
        self.assertEqual(len(report.rows), len(db.load_lock()))


class PageTests(unittest.TestCase):
    """docs/compatibility.md is generated from private results too, so the published tree cannot rebuild it. It can still
    check the page against the published data: the scenarios, the lock and the rules for what a page may contain."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / 'docs/compatibility.md').read_text(encoding='utf-8')
        cls.rows = {}
        for line in cls.text.splitlines():
            found = re.search(r'\(`([A-Z0-9-]+-[0-9]{3})`\)', line)
            if found:
                cls.rows[found.group(1)] = [cell.strip() for cell in line.strip().strip('|').split(' | ')]
        cls.locked = {entry.scenario for entry in db.load_lock()}

    def test_every_scenario_is_listed_once_and_nothing_else(self):
        ids = re.findall(r'\(`([A-Z0-9-]+-[0-9]{3})`\)', self.text)
        self.assertEqual(sorted(ids), sorted(s['id'] for s in REAL_DOC['scenarios']))

    def test_every_game_has_a_section(self):
        for game in REAL_DOC['games']:
            self.assertIn(f"### {game['name']}\n", self.text)

    def test_only_locked_scenarios_are_accepted(self):
        for scenario_id, cells in self.rows.items():
            status = cells[3]
            self.assertIn(status, ('Accepted', 'Verified', 'Failing', 'Pending'), scenario_id)
            if scenario_id in self.locked:
                self.assertIn(status, ('Accepted', 'Failing'), scenario_id)
            else:
                self.assertNotEqual(status, 'Accepted', scenario_id)

    def test_the_rows_describe_the_scenarios_in_the_file(self):
        vocab = db.vocabulary(REAL_DOC)
        for scenario in REAL_DOC['scenarios']:
            cells = self.rows[scenario['id']]
            self.assertTrue(cells[0].startswith(scenario['title']), scenario['id'])
            self.assertTrue(cells[1].startswith(f"{scenario['setup']['tier']}, "), scenario['id'])
            self.assertIn(vocab['layout'][scenario['setup']['layout']], cells[1], scenario['id'])
            self.assertEqual(cells[2].lower(), scenario['level_required'], scenario['id'])
            if scenario.get('known_issue'):
                self.assertIn(scenario['known_issue'], cells[5], scenario['id'])

    def test_the_summary_counts_the_rows(self):
        tiers = {s['id']: s['setup']['tier'] for s in REAL_DOC['scenarios']}
        for tier in db.TIERS:
            statuses = [cells[3] for scenario_id, cells in self.rows.items() if tiers[scenario_id] == tier]
            expected = [str(len(statuses))] + [str(statuses.count(s)) for s in ('Accepted', 'Verified', 'Failing', 'Pending')]
            line = next(l for l in self.text.splitlines() if l.startswith(f'| {tier} | '))
            self.assertEqual([c.strip() for c in line.strip().strip('|').split('|')][1:], expected, tier)

    def test_the_page_is_free_of_private_data_and_glued_words(self):
        for label, pattern in PRIVATE_PATTERNS.items():
            self.assertIsNone(re.search(pattern, self.text), label)
        self.assertEqual([l for l in self.text.splitlines() if RUN_TOGETHER.search(l)], [])
        self.assertNotIn('tools/', self.text)  # The generator is not part of the published tree.

    def test_relative_links_resolve(self):
        for target in re.findall(r'\]\(([^)#]+)\)', self.text):
            if '://' not in target:
                self.assertTrue((ROOT / 'docs' / target).resolve().exists(), target)


if __name__ == '__main__':
    unittest.main()
