#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compatibility database v2: scenarios, the acceptance lock, result records and the release gate.

ADR-008 accepts a compatibility change only when the new case and every previously accepted scenario
pass on the same candidate. This module makes that rule executable.

* compatibility/scenarios.yaml defines the scenarios: setup, optional profile, pass criteria and the
  evidence level they need. It never records an outcome. A scenario is pending until it is locked.
* compatibility/accepted.lock is append-only. One line freezes one scenario: its id, the SHA-256 of its
  canonicalised definition, the date and the evidence level. A locked definition cannot change.
* A result record is one JSON object per line. It carries the ADR-009 metadata and the device baseline
  (HW-003). Records are private and this module only ever receives the directory as an argument.
* evaluate_gate() answers "what is accepted on candidate X": every locked scenario needs a pass on that
  candidate at its tier, device, setup and evidence level (or higher), or an ADR-backed waiver.

The module is published, so it works on the data files alone. Command-line wrappers that bind it to the
private result store are development-only. Run on its own it checks the data files:
python3 tools/compat_results.py
"""
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import unicodedata

try:
    import yaml
except ImportError:  # The checks fail loudly instead of silently skipping the data.
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = ROOT / 'compatibility' / 'scenarios.yaml'
LOCK_PATH = ROOT / 'compatibility' / 'accepted.lock'
INVARIANTS_PATH = ROOT / 'compatibility' / 'game-compatibility.yaml'

SCHEMA_VERSION = 1
HASH_SCHEME = 'compat-definition-v1'
# Evidence levels (ADR-009), weakest first. A result satisfies a scenario at its own level or above.
LEVELS = ('synthetic', 'visual', 'human')
TIERS = ('T1', 'T2')  # ADR-002. A result of one tier never satisfies a scenario of the other.
RESULTS = ('pass', 'fail', 'blocked', 'not_applicable')
SETUP_FIELDS = ('device', 'backend', 'tier', 'tv_mode', 'layout', 'side', 'preset', 'presentation',
                'window_mode', 'steam_input', 'steam_game_resolution', 'controller')
WILDCARD = 'any'
WILDCARD_FIELDS = ('device', 'controller')  # A scenario may accept any device or controller; a record never says "any".
# Values the validator's own rules refer to. The vocabulary in the scenario file has to offer them.
REQUIRED_VOCABULARY = {'device': ('steam_machine', 'steam_deck', WILDCARD), 'controller': (WILDCARD, 'none'),
                       'tv_mode': ('panel',), 'layout': ('single',), 'side': ('main',)}
BASELINE_FIELDS = ('root_readonly', 'extra_users', 'decky', 'sudoers_dropin')

TOP_KEYS = ('version', 'acceptance_policy', 'schema', 'games', 'scenarios', 'waivers')
GAME_KEYS = ('id', 'code', 'name', 'steam_app_id', 'platform', 'window_class', 'invariants', 'notes')
SCENARIO_KEYS = ('id', 'game', 'title', 'introduced', 'setup', 'profile', 'pass_criteria', 'level_required',
                 'known_issue', 'source', 'notes')
WAIVER_KEYS = ('scenario', 'adr', 'reason', 'date', 'candidates')
PROFILE_KEYS = ('id', 'version', 'settings', 'reason')

ID = re.compile(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]{3}')
GAME_ID = re.compile(r'[a-z][a-z0-9_]*')
GAME_CODE = re.compile(r'[A-Z][A-Z0-9]*')
ADR_ID = re.compile(r'ADR-[0-9]{3}')
HEX_PREFIX = re.compile(r'[0-9a-f]{7,64}')
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{5,63}')
TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
RESOLUTION = re.compile(r'[1-9][0-9]*x[1-9][0-9]*')
# An X11 window class as xwininfo prints it: words of letters, digits and . _ + : @ - with single spaces between them.
WINDOW_CLASS = re.compile(r'[A-Za-z0-9_.+:@-]+(?: [A-Za-z0-9_.+:@-]+)*')
PLACEHOLDER = 'FILL-IN'
CLOCK_SKEW = dt.timedelta(minutes=5)


class CompatError(Exception):
    """The compatibility data is not valid; `problems` lists what is wrong."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__('; '.join(self.problems))


class SchemaError(CompatError):
    """The scenario file or the game-matrix file is not valid."""


class LockError(CompatError):
    """accepted.lock is malformed, weakened or shrunk."""


class RecordError(CompatError):
    """A result record is not valid, so it was not stored."""


# --- YAML ----------------------------------------------------------------------------------------

_LOADER = None


def _loader():
    """A safe loader that refuses duplicate keys: a repeated `human:` key would silently drop criteria."""
    global _LOADER
    if yaml is None:
        raise CompatError('PyYAML is required to read the compatibility files')
    if _LOADER is None:
        class Loader(yaml.SafeLoader):
            pass

        def construct_mapping(loader, node, deep=False):
            loader.flatten_mapping(node)
            seen = set()
            for key_node, _ in node.value:
                key = loader.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:
                    raise yaml.constructor.ConstructorError(
                        'while constructing a mapping', node.start_mark, 'found an unhashable key', key_node.start_mark) from None
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        'while constructing a mapping', node.start_mark, f'found duplicate key {key!r}', key_node.start_mark)
                seen.add(key)
            return yaml.SafeLoader.construct_mapping(loader, node, deep)

        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
        _LOADER = Loader
    return _LOADER


def load_yaml_text(text, label='YAML'):
    try:
        return yaml.load(text, Loader=_loader())  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as error:
        raise SchemaError(f'{label}: {error}') from error


def load_yaml_file(path):
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        raise SchemaError(f'{path.name}: cannot read: {error.strerror or error}') from error
    return load_yaml_text(text, path.name)


def load_scenarios(path=SCENARIOS_PATH, contracts=None, check=True):
    """The parsed scenario file. With check=True it raises SchemaError listing every problem."""
    doc = load_yaml_file(path)
    if check:
        problems = validate_scenarios(doc, contracts)
        if problems:
            raise SchemaError(problems)
    return doc


def load_contracts(path=INVARIANTS_PATH):
    """The configuration invariants (formerly runtime contracts), keyed by name."""
    doc = load_yaml_file(path)
    contracts = doc.get('configuration_invariants') if isinstance(doc, dict) else None
    if not isinstance(contracts, dict) or not contracts:
        raise SchemaError(f'{Path(path).name}: no configuration_invariants')
    return contracts


# --- canonical form and hashing -------------------------------------------------------------------

def canonicalize(value):
    """The value in a form that does not depend on how the YAML was written.

    Mapping keys are sorted, strings are NFC-normalised with runs of whitespace collapsed (so re-wrapping
    a long line changes nothing), whole-number floats become integers and dates become ISO strings. List
    order is kept: it is part of the definition.
    """
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, str):
        return ' '.join(unicodedata.normalize('NFC', value).split())
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('a non-finite number has no canonical form')
        return int(value) if value.is_integer() else value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise ValueError(f'{type(value).__name__} has no canonical form')


def canonical_json(value):
    return json.dumps(canonicalize(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def definition(scenario):
    """What the lock freezes: everything that decides whether a result accepts the scenario.

    The evidence level is not part of it (the lock stores it as its own column) and neither are the title,
    dates and notes, which only describe the scenario.
    """
    return {'scheme': HASH_SCHEME, 'id': scenario['id'], 'game': scenario['game'], 'setup': scenario['setup'],
            'profile': scenario.get('profile'), 'pass_criteria': scenario['pass_criteria']}


def scenario_hash(scenario):
    return sha256_hex(canonical_json(definition(scenario)))


def level_rank(level):
    return LEVELS.index(level)


def scenarios_by_id(doc):
    return {scenario['id']: scenario for scenario in doc['scenarios']}


# --- scenario file validation -----------------------------------------------------------------------

def _iso_date(value):
    """A date from YAML (a date object) or text, as an ISO string, or None when it is not a date."""
    if isinstance(value, dt.datetime):
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        try:
            return dt.date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    return None


def _text(value, limit=400):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and '\n' not in value.strip()


def _type_error(spec, value):
    """Why `value` does not fit a criterion parameter spec, or None."""
    if isinstance(spec, list):
        return None if value in spec and isinstance(value, str) else f'must be one of {", ".join(spec)}'
    if spec == 'int':
        return None if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 'must be a positive integer'
    if spec == 'number':
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
        return None if ok else 'must be a positive number'
    if spec == 'str':
        return None if _text(value) else 'must be a short single-line text'
    return f'unknown parameter type {spec!r} in the schema'


def _dedup_key(item):
    """A key that is equal for two criteria that mean the same, whatever odd value the YAML held."""
    try:
        return canonical_json(item)
    except ValueError:
        return repr(item)


def _unknown_keys(mapping, allowed, where, bad):
    for key in mapping:
        if key not in allowed:
            hint = ''
            if key in ('status', 'result', 'outcome', 'pass', 'passed'):
                hint = ' (outcomes are recorded as results and acceptance as accepted.lock, never in this file)'
            bad(where, f'unknown key {key!r}{hint}')


def window_classes(game):
    """The X11 window classes that a game entry names for the game's own window, as a list (empty when it names none).

    A Proton game's window has the class steam_app_<id>, which needs no entry. A native build uses a class of its
    own, which the entry gives as one string or as a list.
    """
    value = game.get('window_class')
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def vocabulary(doc):
    """{field: {value: label}} from the schema block; the tier is fixed by ADR-002 and lives in the code."""
    schema = doc.get('schema') if isinstance(doc, dict) else None
    vocab = schema.get('vocabulary') if isinstance(schema, dict) else None
    return vocab if isinstance(vocab, dict) else {}


def _validate_schema_block(doc, bad):
    schema = doc.get('schema')
    if not isinstance(schema, dict):
        bad('schema', 'expected a mapping with vocabulary and criteria_kinds')
        return
    _unknown_keys(schema, ('vocabulary', 'criteria_kinds'), 'schema', bad)
    vocab = schema.get('vocabulary')
    if not isinstance(vocab, dict):
        bad('schema.vocabulary', 'expected a mapping of setup fields to {value: label}')
        vocab = {}
    for field in SETUP_FIELDS:
        if field == 'tier':
            if 'tier' in vocab:
                bad('schema.vocabulary.tier', 'the tiers are fixed by ADR-002 and live in the validator')
            continue
        values = vocab.get(field)
        if not isinstance(values, dict) or not values:
            bad(f'schema.vocabulary.{field}', 'expected a non-empty mapping of value to label')
            continue
        for value, label in values.items():
            if not isinstance(value, str) or not value:
                bad(f'schema.vocabulary.{field}', f'value {value!r} must be a string (quote values such as "1:1", '
                    'which YAML would read as a number)')
            if not _text(label, 80):
                bad(f'schema.vocabulary.{field}.{value}', 'needs a short label')
        for required in REQUIRED_VOCABULARY.get(field, ()):
            if required not in values:
                bad(f'schema.vocabulary.{field}', f'must offer {required!r}, which the validator rules use')
    for field in vocab:
        if field not in SETUP_FIELDS:
            bad('schema.vocabulary', f'unknown setup field {field!r}')
    kinds = schema.get('criteria_kinds')
    if not isinstance(kinds, dict) or not kinds:
        bad('schema.criteria_kinds', 'expected a mapping of criterion kind to its parameters')
        return
    for kind, params in kinds.items():
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', kind) or not isinstance(params, dict):
            bad(f'schema.criteria_kinds.{kind}', 'expected a snake_case name with a mapping of parameters')
            continue
        for name, spec in params.items():
            if not (spec in ('int', 'number', 'str') or (isinstance(spec, list) and spec and all(isinstance(s, str) for s in spec))):
                bad(f'schema.criteria_kinds.{kind}.{name}', 'expected int, number, str or a list of allowed values')


def _validate_policy(doc, ids, bad):
    policy = doc.get('acceptance_policy')
    if not isinstance(policy, dict):
        bad('acceptance_policy', 'expected a mapping')
        return
    _unknown_keys(policy, ('rule', 'requirements', 'first_window_sizing_pair'), 'acceptance_policy', bad)
    if not _text(policy.get('rule'), 120):
        bad('acceptance_policy.rule', 'expected a rule name')
    requirements = policy.get('requirements')
    if not isinstance(requirements, list) or not requirements or not all(isinstance(r, str) and r.strip() for r in requirements):
        bad('acceptance_policy.requirements', 'expected a list of requirement texts')
    pair = policy.get('first_window_sizing_pair', [])
    if not isinstance(pair, list) or not all(isinstance(p, str) for p in pair):
        bad('acceptance_policy.first_window_sizing_pair', 'expected a list of scenario ids')
    else:
        for scenario_id in pair:
            if scenario_id not in ids:
                bad('acceptance_policy.first_window_sizing_pair', f'unknown scenario {scenario_id}')


def requirement_tokens(requirement):
    """The names a game may use to point at one requirement of a configuration invariant."""
    if not isinstance(requirement, dict):
        return set()
    kind = requirement.get('kind')
    if kind in ('env', 'unset'):
        return {requirement.get('name')}
    if kind == 'key-value':
        return {requirement.get('key')}
    if kind == 'command-contains':
        return {f"{requirement.get('command')}:{requirement.get('contains')}"}
    if kind in ('text', 'absent'):
        return {requirement.get('contains')}
    return set()


def _validate_games(doc, contracts, bad):
    games = doc.get('games')
    if not isinstance(games, list) or not games:
        bad('games', 'expected a non-empty list')
        return {}
    by_id, codes = {}, set()
    for index, game in enumerate(games):
        where = f'games[{index}]'
        if not isinstance(game, dict):
            bad(where, 'expected a mapping')
            continue
        where = f"games[{index}] {game.get('id')}"
        _unknown_keys(game, GAME_KEYS, where, bad)
        if not isinstance(game.get('id'), str) or not GAME_ID.fullmatch(game['id']):
            bad(where, 'id must be lowercase letters, digits and underscores')
        elif game['id'] in by_id:
            bad(where, 'duplicate game id')
        else:
            by_id[game['id']] = game
        code = game.get('code')
        if not isinstance(code, str) or not GAME_CODE.fullmatch(code):
            bad(where, 'code must be upper-case letters and digits (it starts every scenario id)')
        elif code in codes:
            bad(where, f'duplicate game code {code}')
        else:
            codes.add(code)
        if not _text(game.get('name'), 120):
            bad(where, 'needs a name')
        app = game.get('steam_app_id')
        if app is not None and not (isinstance(app, int) and not isinstance(app, bool) and app > 0):
            bad(where, 'steam_app_id must be a positive integer or null')
        if game.get('platform') != 'steam':
            bad(where, 'platform must be steam')
        if 'window_class' in game:
            classes = window_classes(game)
            if not classes or not all(isinstance(c, str) and len(c) <= 80 and WINDOW_CLASS.fullmatch(c) for c in classes):
                bad(where, 'window_class must be a window class or a non-empty list of them (letters, digits and . _ + : @ - '
                    'with single spaces)')
            elif len(set(classes)) != len(classes):
                bad(where, 'window_class names a class twice')
        notes = game.get('notes', [])
        if not isinstance(notes, list) or not all(isinstance(n, str) and n.strip() for n in notes):
            bad(where, 'notes must be a list of texts')
        links = game.get('invariants', [])
        if not isinstance(links, list):
            bad(where, 'invariants must be a list')
            continue
        for link in links:
            if not isinstance(link, dict) or set(link) - {'contract', 'requirements'} or 'contract' not in link:
                bad(where, 'each invariant is {contract, requirements?}')
                continue
            if contracts is None:
                continue
            contract = contracts.get(link['contract'])
            if not isinstance(contract, dict):
                bad(where, f"references unknown configuration invariant {link['contract']!r}")
                continue
            known = set()
            for requirement in contract.get('required', []):
                known |= requirement_tokens(requirement)
            for token in link.get('requirements', []):
                if token not in known:
                    bad(where, f"requirement {token!r} is not part of configuration invariant {link['contract']!r}")
    return by_id


def _validate_criteria(criteria, kinds, level, where, bad):
    if not isinstance(criteria, dict):
        bad(where, 'pass_criteria must be a mapping with automated and human lists')
        return
    _unknown_keys(criteria, ('automated', 'human'), f'{where}.pass_criteria', bad)
    automated, human = criteria.get('automated'), criteria.get('human')
    if not isinstance(automated, list) or not isinstance(human, list):
        bad(where, 'pass_criteria needs both an automated and a human list (either may be empty)')
        return
    for index, item in enumerate(automated):
        label = f'{where}.pass_criteria.automated[{index}]'
        kind = item.get('kind') if isinstance(item, dict) else None
        if kind not in kinds:
            bad(label, f'kind {kind!r} is not defined in schema.criteria_kinds')
            continue
        params = kinds[kind]
        for name in item:
            if name != 'kind' and name not in params:
                bad(label, f'{kind} has no parameter {name!r}')
        for name, spec in params.items():
            if name not in item:
                bad(label, f'{kind} needs {name}')
            else:
                problem = _type_error(spec, item[name])
                if problem:
                    bad(label, f'{name} {problem}')
    if len({_dedup_key(item) for item in automated}) != len(automated):
        bad(where, 'duplicate automated criterion')
    for index, item in enumerate(human):
        if not _text(item, 300):
            bad(f'{where}.pass_criteria.human[{index}]', 'expected a short single-line text')
    if len({canonicalize(item) for item in human if isinstance(item, str)}) != len([i for i in human if isinstance(i, str)]):
        bad(where, 'duplicate human criterion')
    if level == 'human' and not human:
        bad(where, 'a human-level scenario needs at least one human criterion')
    if level in ('synthetic', 'visual') and not automated:
        bad(where, f'a {level}-level scenario needs at least one automated criterion')
    if level in ('synthetic', 'visual') and human:
        bad(where, f'human criteria need the human level, not {level}')


def _validate_setup(setup, vocab, where, bad):
    if not isinstance(setup, dict):
        bad(where, 'setup must be a mapping')
        return
    _unknown_keys(setup, SETUP_FIELDS, f'{where}.setup', bad)
    for field in SETUP_FIELDS:
        value = setup.get(field)
        allowed = TIERS if field == 'tier' else tuple(vocab.get(field, {}))
        if field not in setup:
            bad(f'{where}.setup', f'missing {field}')
        elif not isinstance(value, str) or value not in allowed:
            bad(f'{where}.setup.{field}', f'{value!r} is not one of {", ".join(allowed)}'
                + (' (quote values such as "1:1": YAML reads them as numbers)' if field == 'preset' else ''))
    if all(isinstance(setup.get(f), str) for f in ('tier', 'device', 'tv_mode')):
        if setup['tier'] == 'T2' and (setup['device'] != 'steam_deck' or setup['tv_mode'] != 'panel'):
            bad(f'{where}.setup', 'tier T2 is the Steam Deck built-in screen: device steam_deck and tv_mode panel')
        if setup['tier'] == 'T1' and setup['tv_mode'] == 'panel':
            bad(f'{where}.setup', 'tier T1 drives an external display; tv_mode panel belongs to tier T2')
    if setup.get('layout') == 'single' and setup.get('side') != 'main':
        bad(f'{where}.setup', 'a full-screen layout has only the Main side')


def _validate_profile(profile, where, bad):
    if profile is None:
        return
    if not isinstance(profile, dict):
        bad(where, 'profile must be null or a mapping {id, version, settings}')
        return
    _unknown_keys(profile, PROFILE_KEYS, f'{where}.profile', bad)
    if not isinstance(profile.get('id'), str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', profile['id']):
        bad(f'{where}.profile', 'id must be lowercase letters, digits, dots, dashes or underscores')
    version = profile.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        bad(f'{where}.profile', 'version must be a positive integer')
    settings = profile.get('settings')
    if not isinstance(settings, dict) or not settings or not all(isinstance(k, str) for k in settings):
        bad(f'{where}.profile', 'settings must be a non-empty mapping')
    if 'reason' in profile and not _text(profile['reason']):
        bad(f'{where}.profile', 'reason must be a short text')


def _validate_scenarios(doc, games, vocab, bad):
    scenarios = doc.get('scenarios')
    if not isinstance(scenarios, list) or not scenarios:
        bad('scenarios', 'expected a non-empty list')
        return {}
    kinds = doc.get('schema', {}).get('criteria_kinds') if isinstance(doc.get('schema'), dict) else None
    kinds = kinds if isinstance(kinds, dict) else {}
    seen, used_games = {}, set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            bad(f'scenarios[{index}]', 'expected a mapping')
            continue
        sid = scenario.get('id')
        where = f'scenarios[{index}] {sid}'
        _unknown_keys(scenario, SCENARIO_KEYS, where, bad)
        if not isinstance(sid, str) or not ID.fullmatch(sid):
            bad(where, 'id must look like GAME-TOPIC-001')
        elif sid in seen:
            bad(where, 'duplicate scenario id')
        else:
            seen[sid] = scenario
        game = games.get(scenario['game']) if isinstance(scenario.get('game'), str) else None
        if game is None:
            bad(where, f"unknown game {scenario.get('game')!r}")
        else:
            used_games.add(scenario['game'])
            code = game.get('code')
            if isinstance(sid, str) and isinstance(code, str) and not sid.startswith(code + '-'):
                bad(where, f'id must start with the game code {code}-')
        if not _text(scenario.get('title'), 100):
            bad(where, 'needs a short title')
        if _iso_date(scenario.get('introduced')) is None:
            bad(where, 'introduced must be a date (YYYY-MM-DD)')
        level = scenario.get('level_required')
        if level not in LEVELS:
            bad(where, f'level_required must be one of {", ".join(LEVELS)}')
        _validate_setup(scenario.get('setup'), vocab, where, bad)
        _validate_profile(scenario.get('profile'), where, bad)
        _validate_criteria(scenario.get('pass_criteria'), kinds, level, where, bad)
        try:  # Whatever the lock freezes has to have a canonical form, or every later hash would fail.
            canonical_json({key: scenario.get(key) for key in ('id', 'game', 'setup', 'profile', 'pass_criteria')})
        except ValueError as error:
            bad(where, f'cannot be hashed: {error}')
        for key in ('known_issue', 'source'):
            if key in scenario and not _text(scenario[key], 600):
                bad(where, f'{key} must be a short single-line text')
        notes = scenario.get('notes', [])
        if not isinstance(notes, list) or not all(isinstance(n, str) and n.strip() for n in notes):
            bad(where, 'notes must be a list of texts')
    for game_id in games:
        if game_id not in used_games:
            bad(f'games {game_id}', 'has no scenario; add at least one (a pending one for an untested game)')
    return seen


def _validate_waivers(doc, ids, bad):
    waivers = doc.get('waivers')
    if not isinstance(waivers, list):
        bad('waivers', 'expected a list (empty when there are none)')
        return
    for index, waiver in enumerate(waivers):
        where = f'waivers[{index}]'
        if not isinstance(waiver, dict):
            bad(where, 'expected a mapping')
            continue
        _unknown_keys(waiver, WAIVER_KEYS, where, bad)
        if waiver.get('scenario') not in ids:
            bad(where, f"unknown scenario {waiver.get('scenario')!r}")
        if not isinstance(waiver.get('adr'), str) or not ADR_ID.fullmatch(waiver['adr']):
            bad(where, 'a waiver must cite the ADR that allows it, for example ADR-012')
        if not _text(waiver.get('reason')):
            bad(where, 'a waiver needs a reason')
        if _iso_date(waiver.get('date')) is None:
            bad(where, 'a waiver needs a date')
        candidates = waiver.get('candidates', [])
        if not isinstance(candidates, list) or not all(isinstance(c, str) and HEX_PREFIX.fullmatch(c) for c in candidates):
            bad(where, 'candidates must be lowercase hex revisions of 7 to 64 characters (leave it out to waive every candidate)')


def validate_scenarios(doc, contracts=None):
    """Every problem in the parsed scenario file, as a list of strings (empty when it is valid).

    `contracts` is the configuration-invariants mapping; with it, each game's invariant links are resolved.
    """
    problems = []

    def bad(where, message):
        problems.append(f'{where}: {message}')

    if not isinstance(doc, dict):
        return ['scenario file: expected a mapping at the top level']
    _unknown_keys(doc, TOP_KEYS, 'scenario file', bad)
    if doc.get('version') != SCHEMA_VERSION:
        bad('version', f'expected {SCHEMA_VERSION}')
    _validate_schema_block(doc, bad)
    games = _validate_games(doc, contracts, bad)
    scenarios = _validate_scenarios(doc, games, vocabulary(doc), bad)
    _validate_policy(doc, scenarios, bad)
    _validate_waivers(doc, scenarios, bad)
    return problems


def waiver_adr_problems(doc, adr_ids):
    """Waivers that cite an ADR which does not exist in `adr_ids` (the decision record is private)."""
    return [f"waiver for {w['scenario']}: {w['adr']} is not in the decision record"
            for w in doc.get('waivers', []) if w['adr'] not in adr_ids]


# --- accepted.lock -----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class LockEntry:
    scenario: str
    sha256: str
    date: str
    level: str
    line: int = 0

    def text(self):
        return f'{self.scenario} {self.sha256} {self.date} {self.level}'


def format_lock_line(scenario, level=None, date=None):
    """The line that locks `scenario`. The owner appends it to accepted.lock after reviewing the evidence."""
    level = level or scenario['level_required']
    if level not in LEVELS:
        raise LockError(f'unknown evidence level {level}')
    date = date or dt.datetime.now(dt.timezone.utc).date().isoformat()
    return LockEntry(scenario['id'], scenario_hash(scenario), date, level).text()


def parse_lock(text):
    """(entries, problems) from the text of accepted.lock. Only whole-line comments are allowed."""
    entries, problems = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 4:
            problems.append(f'accepted.lock:{number}: expected "scenario-id sha256 date level"')
            continue
        scenario, digest, date, level = parts
        if not ID.fullmatch(scenario):
            problems.append(f'accepted.lock:{number}: {scenario!r} is not a scenario id')
        elif not re.fullmatch(r'[0-9a-f]{64}', digest):
            problems.append(f'accepted.lock:{number}: the hash must be 64 lowercase hex digits')
        elif _iso_date(date) is None:
            problems.append(f'accepted.lock:{number}: {date!r} is not a date')
        elif level not in LEVELS:
            problems.append(f'accepted.lock:{number}: level must be one of {", ".join(LEVELS)}')
        else:
            entries.append(LockEntry(scenario, digest, date, level, number))
    return entries, problems


def load_lock(path=LOCK_PATH):
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as error:
        raise LockError(f'accepted.lock: cannot read: {error.strerror or error}') from error
    entries, problems = parse_lock(text)
    if problems:
        raise LockError(problems)
    return entries


def lock_problems(entries, doc):
    """Why the lock does not match the scenario file: removed, edited (weakened) or lowered scenarios."""
    problems, seen = [], set()
    by_id = scenarios_by_id(doc)
    for entry in entries:
        where = f'accepted.lock:{entry.line} {entry.scenario}'
        if entry.scenario in seen:
            problems.append(f'{where}: locked twice; a locked scenario is frozen, add a new scenario instead')
        seen.add(entry.scenario)
        scenario = by_id.get(entry.scenario)
        if scenario is None:
            problems.append(f'{where}: the scenario was removed from scenarios.yaml; locked scenarios are permanent')
            continue
        if scenario_hash(scenario) != entry.sha256:
            problems.append(f'{where}: the setup, profile or pass criteria changed after the scenario was locked '
                            '(never weaken an accepted scenario; add a new scenario to tighten it)')
        if level_rank(scenario['level_required']) < level_rank(entry.level):
            problems.append(f"{where}: the required level was lowered from {entry.level} to {scenario['level_required']}")
    return problems


def lock_history_problems(entries, root=ROOT, rel='compatibility/accepted.lock'):
    """(problems, note): lock lines that earlier commits had and the current file lost or changed.

    Without a git history (a staged export, no git) the check cannot run: it returns no problems and a note.
    """
    root = Path(root)
    try:
        revisions = subprocess.run(['git', '-C', str(root), 'log', '--full-history', '--format=%H', '--', rel],
                                   capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return [], 'no git history; the lock was not compared with earlier commits'
    current = {entry.text() for entry in entries}
    problems = []
    for revision in revisions:
        shown = subprocess.run(['git', '-C', str(root), 'show', f'{revision}:./{rel}'], capture_output=True, text=True)
        if shown.returncode != 0:
            continue  # The file did not exist in that commit (it was deleted there, or not yet added).
        for old in parse_lock(shown.stdout)[0]:
            if old.text() not in current:
                problems.append(f'accepted.lock: the line for {old.scenario} from commit {revision[:10]} was removed or '
                                'changed; the lock is append-only')
    return sorted(set(problems)), None


# --- result records -----------------------------------------------------------------------------------

def _str_ok(value):
    return isinstance(value, str) and bool(value.strip()) and PLACEHOLDER not in value


def _record_setup_problems(setup, vocab, bad):
    if not isinstance(setup, dict):
        bad('setup', 'must record the setup that was actually run')
        return
    for field in SETUP_FIELDS:
        value = setup.get(field)
        allowed = TIERS if field == 'tier' else tuple(vocab.get(field, {}))
        if field in WILDCARD_FIELDS:
            allowed = tuple(v for v in allowed if v != WILDCARD)
        if field not in setup:
            bad('setup', f'missing {field}')
        elif vocab or field == 'tier':
            if value not in allowed:
                bad(f'setup.{field}', f'{value!r} is not one of {", ".join(allowed)} (a record names what was run, never "any")')
    for field in setup:
        if field not in SETUP_FIELDS:
            bad('setup', f'unknown field {field!r}')
    if setup.get('tier') == 'T2' and (setup.get('device') != 'steam_deck' or setup.get('tv_mode') != 'panel'):
        bad('setup', 'tier T2 is the Steam Deck built-in screen: device steam_deck and tv_mode panel')
    if setup.get('tier') == 'T1' and setup.get('tv_mode') == 'panel':
        bad('setup', 'tier T1 drives an external display; tv_mode panel belongs to tier T2')
    if setup.get('layout') == 'single' and setup.get('side') != 'main':
        bad('setup', 'a full-screen layout has only the Main side')


def _baseline_problems(baseline, bad):
    if not isinstance(baseline, dict):
        bad('baseline', 'the device baseline is required (read-only root, extra users, Decky, sudoers drop-in)')
        return
    for field in baseline:
        if field not in BASELINE_FIELDS:
            bad('baseline', f'unknown field {field!r}')
    for field in ('root_readonly', 'decky', 'sudoers_dropin'):
        if not isinstance(baseline.get(field), bool):
            bad(f'baseline.{field}', 'must be true or false')
    users = baseline.get('extra_users')
    if not isinstance(users, list) or not all(_str_ok(u) for u in users) or len(set(users)) != len(users):
        bad('baseline.extra_users', 'must be a list of the local users beyond the intended accounts (empty on a stock device)')


def baseline_class(baseline):
    """"stock" when the device matches a clean SteamOS install, otherwise "engineering" (HW-003)."""
    stock = (baseline.get('root_readonly') is True and baseline.get('extra_users') == []
             and baseline.get('decky') is False and baseline.get('sudoers_dropin') is False)
    return 'stock' if stock else 'engineering'


RECORD_REQUIRED = ('schema', 'run_id', 'timestamp', 'scenario', 'criteria_sha256', 'level', 'result', 'observations', 'steps',
                   'cleanup', 'evidence_ref', 'source_revision', 'candidate_sha256', 'os_version', 'gpu_driver',
                   'gamescope_version', 'steam_version', 'game_build', 'proton_version_or_native', 'observed_game_resolution',
                   'steam_game_resolution_setting', 'steam_input_layout', 'controller_identities', 'setup', 'baseline')
RECORD_OPTIONAL = ('reason', 'reruns', 'notes')
RECORD_TEXTS = ('observations', 'cleanup', 'evidence_ref', 'os_version', 'gpu_driver', 'gamescope_version', 'steam_version',
                'game_build', 'proton_version_or_native', 'steam_input_layout')


def _record_identity_problems(record, latest, bad):
    """The run, its time (no later than `latest`, when given) and the exact candidate and criteria it speaks for."""
    for field in RECORD_REQUIRED:
        if field not in record:
            bad(field, 'is required')
    for field in record:
        if field not in RECORD_REQUIRED + RECORD_OPTIONAL:
            bad(field, 'is not a known field')
    if record.get('schema') != SCHEMA_VERSION:
        bad('schema', f'expected {SCHEMA_VERSION}')
    if 'run_id' in record and not (isinstance(record['run_id'], str) and RUN_ID.fullmatch(record['run_id'])):
        bad('run_id', 'must be 6 to 64 letters, digits, dots, dashes or underscores')
    if 'timestamp' in record:
        stamp = record['timestamp']
        when = None
        if isinstance(stamp, str) and TIMESTAMP.fullmatch(stamp):
            try:
                when = dt.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        if when is None:
            bad('timestamp', 'must be UTC, like 2026-10-08T14:30:00Z')
        elif latest is not None and when > latest:
            bad('timestamp', 'is in the future')
    for field, length in (('source_revision', 40), ('candidate_sha256', 64), ('criteria_sha256', 64)):
        if field in record and not (isinstance(record[field], str) and re.fullmatch(f'[0-9a-f]{{{length}}}', record[field])):
            bad(field, f'must be the full {length}-digit lowercase hex value, not an abbreviation')


def _record_outcome_problems(record, bad):
    """The level, the result and what has to accompany it."""
    if 'level' in record and record['level'] not in LEVELS:
        bad('level', f'must be one of {", ".join(LEVELS)}')
    if 'result' in record and record['result'] not in RESULTS:
        bad('result', f'must be one of {", ".join(RESULTS)}')
    if record.get('result') in ('blocked', 'not_applicable') and not _str_ok(record.get('reason')):
        bad('reason', f"is required when the result is {record['result']}: neither counts as a pass, so say why")
    for field in ('reason', 'notes'):
        if field in record and not _str_ok(record[field]):
            bad(field, 'must be a text')
    if 'reruns' in record:
        reruns = record['reruns']
        if record.get('result') != 'pass' or not _str_ok(record.get('reason')):
            bad('reruns', 'only a pass with a reason can acknowledge earlier failures')
        if not isinstance(reruns, list) or not reruns or not all(isinstance(r, str) and RUN_ID.fullmatch(r) for r in reruns):
            bad('reruns', 'must list the run ids of the failures this pass explains')


def _record_text_problems(record, bad):
    """What was observed, run and found on the device; free text stays private."""
    for field in RECORD_TEXTS:
        if field in record and not _str_ok(record[field]):
            bad(field, 'must be a non-empty text (write the real value, or "none" or "n/a" where nothing applies)')
    ref = record.get('evidence_ref')
    if isinstance(ref, str) and (ref.startswith('/') or '..' in Path(ref).parts or '://' in ref):
        bad('evidence_ref', 'must be a relative reference into the private evidence, not an absolute path or a URL')
    steps = record.get('steps')
    if 'steps' in record and (not isinstance(steps, list) or not steps or not all(_str_ok(s) for s in steps)):
        bad('steps', 'must be a non-empty list of the steps that were run')
    resolution = record.get('observed_game_resolution')
    if 'observed_game_resolution' in record and not (resolution == 'n/a' or (isinstance(resolution, str) and RESOLUTION.fullmatch(resolution))):
        bad('observed_game_resolution', 'must be WIDTHxHEIGHT as the game reported it, or n/a')


def _record_setup_agreement_problems(record, bad):
    """Fields that restate a part of the setup have to agree with it."""
    setup = record.get('setup') if isinstance(record.get('setup'), dict) else {}
    if 'steam_game_resolution_setting' in record:
        value = record['steam_game_resolution_setting']
        if not (value == 'default' or (isinstance(value, str) and re.fullmatch(r'custom:[1-9][0-9]*x[1-9][0-9]*', value))):
            bad('steam_game_resolution_setting', 'must be "default" or "custom:WIDTHxHEIGHT"')
        elif setup.get('steam_game_resolution') != value.split(':')[0]:
            bad('steam_game_resolution_setting', f"disagrees with setup.steam_game_resolution {setup.get('steam_game_resolution')!r}")
    if 'controller_identities' in record:
        identities = record['controller_identities']
        if not isinstance(identities, list) or not all(_str_ok(i) for i in identities):
            bad('controller_identities', 'must be a list of the controllers used (private; never published)')
        elif not identities and setup.get('controller') != 'none':
            bad('controller_identities', 'is empty although the setup used a controller')


def validate_record(record, doc=None, now=None, current=True):
    """Every problem in one result record, as a list of strings (empty when it is valid).

    `doc` is the scenario file; it supplies the setup vocabulary. With `current=True` the record also has
    to belong to the scenario as it is now: the scenario exists and the criteria hash is its current hash.
    That is what a new record must satisfy, and it must not be dated in the future. A stored record that an edit
    of the scenario has outdated is not corrupt, it just no longer counts, and a stored record is not judged by
    today's clock either; read it with `current=False`.
    """
    problems = []

    def bad(where, message):
        problems.append(f'{where}: {message}')

    if not isinstance(record, dict):
        return ['record: expected a JSON object']
    latest = (now or dt.datetime.now(dt.timezone.utc)) + CLOCK_SKEW if current else None
    _record_identity_problems(record, latest, bad)
    _record_outcome_problems(record, bad)
    _record_text_problems(record, bad)
    _record_setup_agreement_problems(record, bad)
    _record_setup_problems(record.get('setup'), vocabulary(doc) if doc else {}, bad)
    _baseline_problems(record.get('baseline'), bad)
    if current and doc and isinstance(record.get('scenario'), str):
        scenario = scenarios_by_id(doc).get(record['scenario'])
        if scenario is None:
            bad('scenario', f"{record['scenario']} is not defined in the scenario file")
        elif record.get('criteria_sha256') != scenario_hash(scenario):
            bad('criteria_sha256', 'is not the current hash of the scenario; the scenario changed after this was prepared')
    return problems


def record_template(scenario):
    """A record for `scenario` with the known fields filled in; the owner replaces every FILL-IN."""
    fill = PLACEHOLDER
    setup = {field: scenario['setup'][field] for field in SETUP_FIELDS}
    for field in WILDCARD_FIELDS:
        if setup[field] == WILDCARD:
            setup[field] = fill
    return {
        'schema': SCHEMA_VERSION, 'run_id': fill, 'timestamp': fill, 'scenario': scenario['id'],
        'criteria_sha256': scenario_hash(scenario), 'level': scenario['level_required'], 'result': fill,
        'observations': fill, 'steps': [fill], 'cleanup': fill, 'evidence_ref': fill,
        'source_revision': fill, 'candidate_sha256': fill, 'os_version': fill, 'gpu_driver': fill,
        'gamescope_version': fill, 'steam_version': fill, 'game_build': fill, 'proton_version_or_native': fill,
        'observed_game_resolution': fill, 'steam_game_resolution_setting': 'default' if setup['steam_game_resolution'] == 'default' else fill,
        'steam_input_layout': fill, 'controller_identities': [fill], 'setup': setup,
        'baseline': {'root_readonly': fill, 'extra_users': [fill], 'decky': fill, 'sudoers_dropin': fill},
    }


def read_results(results_dir):
    """(records, problems) from every *.jsonl file in the directory, oldest file first."""
    records, problems = [], []
    directory = Path(results_dir)
    for path in sorted(directory.glob('*.jsonl')) if directory.is_dir() else []:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f'{path.name}: cannot be read: {error}')
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                problems.append(f'{path.name}:{number}: not valid JSON: {error}')
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                problems.append(f'{path.name}:{number}: expected a JSON object')
    return records, problems


def _rerun_problems(record, earlier_by_run):
    """Reruns of `record` that do not name an earlier failure of the same scenario on the same candidate."""
    problems = []
    for run_id in record.get('reruns', []) if isinstance(record.get('reruns'), list) else []:
        target = earlier_by_run.get(run_id)
        if (target is None or target.get('result') != 'fail' or target.get('scenario') != record.get('scenario')
                or target.get('candidate_sha256') != record.get('candidate_sha256')):
            problems.append(f'reruns {run_id}: not an earlier failure of the same scenario on the same candidate')
    return problems


def results_problems(records, doc, now=None):
    """Problems across a whole result store, in file order.

    Each record has to be well formed, run ids have to be unique and every rerun has to point back at a failure.
    Records are judged as history (current=False): editing a scenario outdates its old results without corrupting them.
    """
    problems, earlier = [], {}
    for index, record in enumerate(records, 1):
        run_id = record.get('run_id')
        where = f'record {index} ({run_id})'
        problems += [f'{where}: {problem}' for problem in validate_record(record, doc, now, current=False)]
        problems += [f'{where}: {problem}' for problem in _rerun_problems(record, earlier)]
        if run_id in earlier:
            problems.append(f'{where}: run id used twice; records are append-only, give a rerun its own id')
        elif isinstance(run_id, str):
            earlier[run_id] = record
    return problems


def append_result(record, results_dir=None, scenarios=None, now=None):
    """Validate `record` and append it to the result store as one JSON line. Returns the file written.

    Nothing is written when the record is invalid (RecordError lists the problems) or its run id is already
    used. Records are never edited or removed: a rerun is a new record with its own run id. `results_dir`
    defaults to the DOSPLEX_COMPAT_RESULTS environment variable, so a harness can call this with one argument.
    """
    directory = results_dir or os.environ.get('DOSPLEX_COMPAT_RESULTS')
    if not directory:
        raise RecordError('no result store: pass results_dir or set DOSPLEX_COMPAT_RESULTS')
    directory = Path(directory)
    doc = scenarios if scenarios is not None else load_scenarios()
    problems = validate_record(record, doc, now)
    if problems:
        raise RecordError(problems)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['timestamp'][:10]}.jsonl"
    with open(directory / '.append.lock', 'a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)  # Two writers must not interleave their checks and appends.
        stored, damaged = read_results(directory)
        if damaged:
            raise RecordError(['the result store has damaged lines; repair them before appending', *damaged])
        by_run = {r.get('run_id'): r for r in stored}
        if record['run_id'] in by_run:
            raise RecordError(f"run id {record['run_id']} is already stored; give a rerun its own id")
        problems = _rerun_problems(record, by_run)
        if problems:
            raise RecordError(problems)
        with open(path, 'a+b') as out:
            out.seek(0, os.SEEK_END)
            if out.tell():
                out.seek(-1, os.SEEK_END)
                if out.read(1) != b'\n':  # A hand-edited file may lack its final newline.
                    out.write(b'\n')
            out.write(line.encode('utf-8'))
            out.flush()
            os.fsync(out.fileno())
    return path


# --- the release gate -------------------------------------------------------------------------------------

def candidate_matches(record, candidate):
    """True when `candidate` (a lowercase hex prefix of 7 or more digits) names the record's revision or bundle hash."""
    if not HEX_PREFIX.fullmatch(candidate):
        raise ValueError('a candidate is the source revision or the bundle hash, as at least 7 lowercase hex digits')
    return any(str(record.get(field, '')).startswith(candidate) for field in ('source_revision', 'candidate_sha256'))


def ambiguous_candidate(records, candidate):
    """The distinct (revision, bundle hash) pairs `candidate` names when it names more than one, else []."""
    pairs = sorted({(str(r.get('source_revision')), str(r.get('candidate_sha256'))) for r in records
                    if isinstance(r, dict) and candidate_matches(r, candidate)})
    return pairs if len(pairs) > 1 else []


def setup_difference(record_setup, scenario_setup):
    """Why a record's setup does not satisfy the scenario's, or None when it does."""
    if not isinstance(record_setup, dict):
        return 'no setup recorded'
    if record_setup.get('tier') != scenario_setup['tier']:
        return f"a {record_setup.get('tier')} result cannot satisfy a {scenario_setup['tier']} scenario (ADR-002)"
    for field in SETUP_FIELDS:
        want, got = scenario_setup[field], record_setup.get(field)
        if want == WILDCARD and field in WILDCARD_FIELDS:
            continue
        if got != want:
            return f'{field} was {got}, the scenario needs {want}'
    return None


def _disqualification(record, scenario, digest, doc):
    """(reason, relevant): why a record does not satisfy the scenario, and whether it still describes this scenario's run.

    A record is relevant when it was recorded against the same definition in the same setup. A relevant failure
    counts against the candidate even when its evidence level was below the one the scenario needs.
    """
    if record.get('criteria_sha256') != digest:
        return 'recorded against other pass criteria than the current ones', False
    difference = setup_difference(record.get('setup'), scenario['setup'])
    if difference:
        return difference, False
    if record.get('result') == 'fail':
        return 'failed', True
    if record.get('result') != 'pass':
        return f"{record.get('result')}: {record.get('reason')}", True
    invalid = validate_record(record, doc, current=False)  # Only a pass can open the gate, so only a pass has to be sound.
    if invalid:
        return f'invalid record ({invalid[0]})', False
    if level_rank(record['level']) < level_rank(scenario['level_required']):
        return f"{record['level']} evidence is below the required {scenario['level_required']} level", True
    return None, True


@dataclasses.dataclass
class GateRow:
    scenario: str
    level: str
    tier: str
    status: str  # pass, waived, fail or missing
    detail: str
    run_id: str = ''

    @property
    def satisfied(self):
        return self.status in ('pass', 'waived')


@dataclasses.dataclass
class GateReport:
    candidate: str
    rows: list
    notes: list

    @property
    def ok(self):
        return all(row.satisfied for row in self.rows)

    def count(self, status):
        return sum(1 for row in self.rows if row.status == status)


def _waiver_for(doc, scenario_id, candidate, identities):
    """The waiver that excuses `scenario_id` on this candidate. One without candidates covers every candidate."""
    for waiver in doc.get('waivers', []):
        if waiver['scenario'] != scenario_id:
            continue
        scope = waiver.get('candidates', [])
        if not scope:
            return waiver
        for prefix in scope:
            if candidate.startswith(prefix) or prefix.startswith(candidate) or any(i.startswith(prefix) for i in identities):
                return waiver
    return None


def _stamp(record):
    return (str(record.get('timestamp', '')), str(record.get('run_id', '')))


def _judge(entry, scenario, doc, candidate, records, identities):
    """The gate row for one locked scenario."""
    level, digest = scenario['level_required'], scenario_hash(scenario)

    def row(status, detail, run_id=''):
        return GateRow(entry.scenario, level, scenario['setup']['tier'], status, detail, run_id)

    if digest != entry.sha256:
        return row('fail', 'the locked definition was edited (weakened or changed)')
    if level_rank(level) < level_rank(entry.level):
        return row('fail', f'the required level was lowered from {entry.level}')
    waiver = _waiver_for(doc, entry.scenario, candidate, identities)
    if waiver:
        return row('waived', f"waived by {waiver['adr']}: {waiver['reason']}")
    own = [r for r in records if r.get('scenario') == entry.scenario]
    if not own:
        return row('missing', 'no result recorded for this candidate')
    passes, failures, unusable = [], [], []
    for record in own:
        reason, relevant = _disqualification(record, scenario, digest, doc)
        if reason is None:
            passes.append(record)
        elif relevant and record.get('result') == 'fail':
            failures.append(record)
        else:
            unusable.append((record, reason))
    unexplained = [f for f in failures
                   if not any(f.get('run_id') in p.get('reruns', []) and _stamp(p) >= _stamp(f) for p in passes)]
    if unexplained:
        latest = max(unexplained, key=_stamp)
        follow = 'a later pass must list it under reruns' if passes else 'no pass since'
        return row('fail', f"failed on this candidate (run {latest.get('run_id')}); {follow}", str(latest.get('run_id', '')))
    if passes:
        best = max(passes, key=_stamp)
        return row('pass', f"{best['level']} pass on {best['timestamp'][:10]}", best['run_id'])
    newest_first = sorted(unusable, key=lambda pair: _stamp(pair[0]), reverse=True)
    return row('missing', '; '.join(f"run {r.get('run_id')}: {reason}" for r, reason in newest_first[:3]))


def evaluate_gate(doc, entries, records, candidate, tier=None):
    """Check every locked scenario against the result records of one candidate.

    A locked scenario is satisfied by an ADR-backed waiver or by a pass on this candidate whose tier, device and
    setup match and whose evidence level is the required one or higher. A failure on the candidate counts against
    it until a later pass names that failure under `reruns` (ADR-009: failures are kept next to the reruns). Bad
    data never opens the gate: a record that is not sound cannot be a pass. `tier` limits the check to one tier.
    """
    candidate = candidate.lower()
    by_id = scenarios_by_id(doc)
    mine = [r for r in records if isinstance(r, dict) and candidate_matches(r, candidate)]
    identities = {str(r.get(field)) for r in mine for field in ('source_revision', 'candidate_sha256')}
    rows, notes = [], []
    if not entries:
        notes.append('accepted.lock has no entries, so nothing is gated')
    for entry in entries:
        scenario = by_id.get(entry.scenario)
        if scenario is None:
            rows.append(GateRow(entry.scenario, entry.level, '?', 'fail', 'locked scenario is no longer defined'))
        elif not tier or scenario['setup']['tier'] == tier:
            rows.append(_judge(entry, scenario, doc, candidate, mine, identities))
    return GateReport(candidate, rows, notes)


def main():
    """Check the published data files: the scenarios, the configuration invariants they point at, and the lock."""
    try:
        doc = load_scenarios(contracts=load_contracts())
        entries = load_lock()
        problems = lock_problems(entries, doc)
        history, note = lock_history_problems(entries)
        problems += history
    except CompatError as error:
        problems, doc, entries, note = error.problems, None, [], None
    for problem in problems:
        print(f'FAIL: {problem}')
    if problems:
        return 1
    print(f"PASS: {len(doc['scenarios'])} scenarios for {len(doc['games'])} games, {len(entries)} locked"
          + (f' ({note})' if note else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
