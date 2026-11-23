#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=60
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The documents and the command line agree: every `dosplex` command the public documents show exists in the
program.

The documents (README.md, docs/*.md, docs/manual/*.md) show commands over months and drift: two of them named
an option the program does not have. This test reads every dosplex command in the fenced code blocks and the
inline code spans, and checks the command, its action and every option against the argparse objects the
program builds (it never runs the program; the control interface's own actions come from its list).
A placeholder (a word in capitals, a path, a number) and usage notation (`--layout [a|b]`) are not checked.
"""
import importlib.machinery
import importlib.util
import re
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = None
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/dosplex'

# (file, line number, reason) — a document that shows a command the program does not have, until it is fixed.
# `dosplex control ACTION [ARGUMENT...]` (docs/control-interface.md:8) is usage notation, not a real command;
# the ACTION is a placeholder, and the program has no such word.
KNOWN_WRONG = [
    ('docs/control-interface.md', 8, 'usage notation: control ACTION [ARGUMENT...]'),
]

DOCUMENTS = [ROOT / 'README.md'] + sorted((ROOT / 'docs').glob('*.md')) + sorted((ROOT / 'docs/manual').glob('*.md'))


def load_cli():
    loader = importlib.machinery.SourceFileLoader('dosplex_under_test', str(CLI))
    spec = importlib.util.spec_from_loader('dosplex_under_test', loader)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(module)
    return module


dosplex = load_cli()

# --- collecting the commands --------------------------------------------------------------------------------

# The program is the word `dosplex` or a path that ends in `/dosplex` (for example `./dosplex-1.0/dosplex`).
# `dosplex-VERSION.tar.gz` and `dosplex-compositor` are not the program: the name does not end in `dosplex`.
PROGRAM = re.compile(r'\A(\.?/)?([A-Za-z0-9._-]+/)*dosplex\Z')
CAPS = re.compile(r'\A[A-Z][A-Z0-9_ -]*\Z')
BRACKETS = re.compile(r'\[[^\]]*\]')


def is_program(word):
    return PROGRAM.match(word) is not None


def is_placeholder(word):
    if CAPS.match(word):
        return True
    if word.startswith('/') or word.startswith('~'):
        return True
    if re.fullmatch(r'[0-9.]+', word) or re.fullmatch(r'[0-9]+x[0-9]+', word):
        return True
    if '/' in word:
        return True
    return False


def one_command(text, where, number):
    """The dosplex command in one line of a code block or an inline span, as a list, or None.

    An optional `sudo` in front is kept as the first word, for the report."""
    words = text.split()
    if not words:
        return None
    sudo = False
    if words[0] == 'sudo':
        if len(words) < 2:
            return None
        sudo = True
        words = words[1:]
    if not is_program(words[0]):
        return None
    rest = words[1:]
    if not rest:
        return ['sudo' if sudo else words[0], words[0]] if sudo else [words[0]]
    if rest[0] == words[0] or any(c in rest[0] for c in '();&|'):
        return None
    return (['sudo'] if sudo else []) + words


def collect(text, where):
    """Every dosplex command in the text of one document: a list of (line number, command text).

    In a code block, a line that ends in a backslash is joined with the lines after it before it is looked at, so a
    command of another program that runs over two lines is never taken for one of dosplex's."""
    candidates = []                     # (line number of its first line, text)
    in_block, joined, first = False, '', 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('```'):
            in_block, joined = not in_block, ''
            continue
        if not in_block:
            candidates.extend((number, span.strip()) for span in re.findall(r'`([^`\n]+)`', line))
            continue
        stripped = line.strip()
        if not joined:
            first = number
        if stripped.endswith('\\'):
            joined += stripped[:-1].strip() + ' '
            continue
        candidates.append((first, (joined + stripped).strip()))
        joined = ''
    result = []
    for number, candidate in candidates:
        words = one_command(candidate, where, number) if candidate else None
        if words is not None:
            result.append((number, ' '.join(words)))
    return result


# --- the real parser --------------------------------------------------------------------------------------

def option_names(parser):
    names = set()
    for group in parser._mutually_exclusive_groups:
        for action in group._group_actions:
            names.update(action.option_strings)
    for action in parser._actions:
        names.update(action.option_strings)
    return names


def sub_parsers(parser):
    """The choices of the subcommand action of a parser, by name."""
    for action in parser._actions:
        if action.dest in ('command', 'action') and getattr(action, 'choices', None):
            return dict(action.choices)
    return {}


ROOT_PARSER = dosplex.build_parser()
COMMANDS = sub_parsers(ROOT_PARSER)
# commands that are not in the public help (`_control_op`, `_worker`) are not written in the documents
PUBLIC_COMMANDS = {c: p for c, p in COMMANDS.items() if not c.startswith('_')}
TOP_OPTIONS = option_names(ROOT_PARSER)


def options_of(*parsers):
    names = set()
    for parser in parsers:
        names.update(option_names(parser))
    return names


def check_command(where, number, words):
    """The problem in one dosplex command, or None."""
    if words and words[0] == 'sudo':
        words = words[1:]
    if len(words) < 1 or not words[0]:
        return None
    words = [w for w in words if w]
    if not words:
        return None
    rest = words[1:]
    if not rest:
        return None
    sub = rest[0]
    if sub.startswith('-') or is_placeholder(sub):
        return None
    if sub not in PUBLIC_COMMANDS:
        return 'unknown command: %s' % sub
    parser = PUBLIC_COMMANDS[sub]
    if sub == 'control':
        # the control interface parses its own words: the action comes from its list, and `--json` is
        # accepted anywhere. Do not check other options after `control ACTION`.
        if len(rest) < 2 or rest[1].startswith('-') or is_placeholder(rest[1]):
            return None
        return check_control(rest[1:])
    actions = sub_parsers(parser)
    options = options_of(parser)
    action = None
    i = 1
    if actions:
        while i < len(rest) and (rest[i].startswith('-') or is_placeholder(rest[i])):
            i += 1
        if i < len(rest):
            action = rest[i]
            i += 1
            if action not in actions:
                return 'unknown action: %s' % action
            options = options | options_of(actions[action])
    shown = ' '.join(words[:1] + [sub] + ([action] if action else []))
    target = actions[action] if action else parser
    # A fixed word the command takes (`autostart on|off|status`, `entry ...`, `library auto on|off`) is an
    # argparse positional with choices, not a subcommand: the first plain words after the command are held to them.
    fixed = [tuple(a.choices) for a in target._actions
             if not a.option_strings and a.choices and not isinstance(a.choices, dict)]
    with_value = {name for a in list(target._actions) + list(parser._actions) + list(ROOT_PARSER._actions)
                  for name in a.option_strings if a.nargs != 0}
    skip = False
    for w in rest[i:]:
        if skip:
            skip = False
            continue
        if not w.startswith('--'):
            if fixed and not w.startswith('-') and not is_placeholder(w) and not BRACKETS.search(w):
                allowed = fixed.pop(0)
                if '|' in w:
                    wrong = [alt for alt in w.split('|') if alt not in allowed]
                else:
                    wrong = [] if w in allowed else [w]
                if wrong:
                    return 'unknown word: %s (dosplex %s takes %s)' % (wrong[0], shown, '|'.join(allowed))
            continue
        skip = w in with_value          # `--option VALUE`: the value is not a word of the command
        clean = BRACKETS.sub('', w).strip()
        if not clean:
            continue
        alternatives = [a.strip() for a in clean.split('|')]
        for alt in alternatives:
            if not alt.startswith('--'):
                continue
            alt = alt.split('=', 1)[0]
            if alt not in options and alt not in TOP_OPTIONS:
                return 'unknown option: %s (in: dosplex %s)' % (alt, shown)
    return None


def check_control(rest):
    action = rest[0]
    if action not in dosplex.CONTROL_ACTIONS:
        return 'unknown action: control %s' % action
    for w in rest[1:]:
        if not w.startswith('--'):
            continue
        name = w.split('=', 1)[0]
        if name not in ('--json', '--timeout'):
            return 'unknown option: control %s %s' % (action, name)
    return None


# --- the test ---------------------------------------------------------------------------------------------

def all_commands():
    found = []
    for document in DOCUMENTS:
        where = str(document.relative_to(ROOT))
        for number, text_ in collect(document.read_text(encoding='utf-8'), where):
            found.append((where, number, text_))
    return found


class CollectorTest(unittest.TestCase):
    """Small tests of the collector, on made-up text."""

    def make(self, text):
        return collect(text, 'test.md')

    def test_fenced_block(self):
        found = self.make('```\nsudo dosplex start --layout single\n```')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], 'sudo dosplex start --layout single')

    def test_inline_span(self):
        found = self.make('Start it with `dosplex start` once.')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], 'dosplex start')

    def test_joined_line(self):
        # the continuation line is not itself a dosplex command, so it joins with the previous line
        text = '```\nsudo /opt/dosplex/bin/dosplex install \\\n    dosplex --release a.tar.gz\n```'
        found = self.make(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], 'sudo /opt/dosplex/bin/dosplex install dosplex --release a.tar.gz')

    def test_sudo(self):
        found = self.make('```\nsudo dosplex stop\n```')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], 'sudo dosplex stop')

    def test_path_ending_in_dosplex(self):
        found = self.make('```\n./dosplex-1.0/dosplex install --release a.tar.gz\n```')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], './dosplex-1.0/dosplex install --release a.tar.gz')

    def test_placeholder_and_usage_notation(self):
        # a placeholder is not a command, and an option inside brackets is usage notation
        self.assertEqual(self.make('```\ncp BUNDLE /opt/dosplex/bin\n```'), [])
        found = self.make('```\nsudo dosplex start --layout [a|b]\n```')
        self.assertEqual(len(found), 1)
        self.assertIsNone(check_command('t', 1, found[0][1].split()))

    def test_not_a_command(self):
        self.assertEqual(self.make('```\ndosplex-compositor --help\n```'), [])
        self.assertEqual(self.make('DosPlex starts when you start it.'), [])
        self.assertEqual(self.make('```\ndosplex-VERSION.tar.gz\n```'), [])
        self.assertEqual(self.make('```\ncp BUNDLE /opt/dosplex/bin\n```'), [])


class DocumentsTest(unittest.TestCase):
    def test_every_document_command_exists(self):
        commands = all_commands()
        problems = []
        known = []
        for where, number, text_ in commands:
            line = (ROOT / where).read_text(encoding='utf-8').splitlines()[number - 1].strip()
            if any(w == where and n == number for w, n, _ in KNOWN_WRONG):
                known.append((where, number))
                continue
            problem = check_command(where, number, text_.split())
            if problem is not None:
                problems.append('%s:%d: %s: %s' % (where, number, ' '.join(text_.split()[:4]), problem))
        self.assertGreaterEqual(
            len(commands), 40, 'only %d dosplex commands found in the documents' % len(commands))
        self.assertEqual(
            problems, [],
            'the documents show commands the program does not have:\n' + '\n'.join(problems)
            + ('\n%d known wrong (KNOWN_WRONG), not counted: %s'
               % (len(known), ', '.join('%s:%d' % (w, n) for w, n in sorted(known))) if known else ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
