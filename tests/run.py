#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Discover the DosPlex tests, run them by tier and keep evidence of the run.

Run it from a checkout or from an exported tree; it uses the Python standard library only.

    python3 tests/run.py --tier fast               hermetic tests, in parallel (the default)
    python3 tests/run.py --tier fast,integration   what ./tests/source-check.sh runs
    python3 tests/run.py --list                    every test with its tier, needs and timeout
    python3 tests/run.py auth-pam-test             only the tests whose name matches

WHAT A TEST IS
  A file named *-test.py, *-integration.py, *-test.sh or *-integration.sh (an underscore works as well as
  the dash) below tests/ and, with --include-private, below the development-only test directory of the
  development checkout. Each one declares itself in a comment near the top:

      # dosplex-test: tier=unit needs=cc timeout=60

  tier     lint, unit, integration, kernel-synthetic, hardware-synthetic, human, or 0 to 5 (required)
  needs    comma-separated list of what the test cannot run without (see NEEDS)
  timeout  seconds before the test and every process it started are killed; the default depends on the tier
  manual="why"     never part of a tier; runs only when named, together with --include-manual
  exclude="why"    not a test at all (a script that happens to be named like one); never run
  skips-ok="why"   the test may report skips of its own (see STATUS)
  serial           in tiers 0-1, do not run it alongside other tests

  A test that needs arguments, or that runs more than once, adds one line for each run:

      # dosplex-run: left {compositor} --client-side left

  {compositor} is the compositor that the runner builds once for all the tests that need it.

  A test file without a valid declaration fails the run (REGISTRY), so that a new test cannot be forgotten.
  The same check is a test of its own (tests/registry-test.py). --list-unregistered shows the offenders.

TIERS
  The numbers are test tiers. They are unrelated to the T1/T2 support tiers (TV or monitor, handheld screen).

  0 lint                 static checks and source-contract checks; no behaviour
  1 unit                 hermetic behaviour tests. The tiers 0 and 1 together are called "fast"
  2 integration          the real compositor, Xvfb, PipeWire or user services, in private temporary directories
  3 kernel-synthetic     virtual devices on this machine's kernel (uinput, CUSE); root or special rights
  4 hardware-synthetic   a Deck or a Steam Machine driven from here; "visual" is the part that reads the
                         capture card
  5 human                owner-driven acceptance

  --tier takes numbers (2), lists (0,1), ranges (0-2) and names: lint, unit, integration, kernel-synthetic,
  hardware-synthetic, human, fast (0-1), visual (the tier 4 tests that need the capture card) and all.
  Tiers 0-1 run in parallel, the others one test at a time.

NEEDS
  A test whose needs are not met is not run: it is reported INCOMPLETE and the run fails, so that a missing
  tool can never look like a pass. The needs are:

%(needs)s

  A test that has to skip something itself prints a line that starts with "SKIP:" (or lets unittest report
  skipped tests). The runner then reports it INCOMPLETE as well, unless the test declares skips-ok.
  The runner refuses to start as root without --allow-root, and a test that needs root must be manual:
  nothing is run as root unless you start the runner as root and name that test.

STATUS
  PASS        exit status 0
  FAIL        any other exit status, or killed by a signal
  TIMEOUT     the timeout passed (a failure)
  INCOMPLETE  a need was not met, the test skipped part of itself, or no test was selected
  REGISTRY    a test file without a valid declaration, or a malformed declaration

  Exit status: 0 only if every selected test passed. 1 for any other result. 2 for a usage error.

ENVIRONMENT OF THE TESTS
  Every test runs in a process group of its own, with no stdin, in its own empty working directory, with
  TMPDIR set to a private directory, PYTHONDONTWRITEBYTECODE=1, and without DOSPLEX_* settings and without
  the variables that point git at a repository. When a test ends, whatever it left running in its process
  group is killed.

EVIDENCE
  Each run writes one JSON record (UTC time, git revision, hash of the uncommitted changes, tool versions,
  the result and duration of every test) to the --evidence-dir. In the development checkout the default is
  the maintainers' evidence directory. The record does not go to /tmp unless you ask for it (only the
  scratch files of the tests go there, and they are removed after the run). --no-evidence turns it off.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ElementTree

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA = 'dosplex-gate-evidence/1'

# Where the tests are. The development-only directory is named in parts so that this published file holds
# no reference into it; the published tree has no such directory and never looks for it.
TEST_DIRECTORY = 'tests'
PRIVATE = 'private'
PRIVATE_TEST_DIRECTORY = posixpath.join(PRIVATE, 'testing')
EVIDENCE_DIRECTORY = posixpath.join(PRIVATE, 'evidence', 'gate')

HEADER_LINES = 40
CANDIDATE = re.compile(r'.+[-_](?:test|integration)\.(?:py|sh)')
NOT_SEARCHED = ('fixtures', '__pycache__')
TEST_LINE = re.compile(r'^#\s*dosplex-test:\s*(.*?)\s*$')
RUN_LINE = re.compile(r'^#\s*dosplex-run:\s*(\S+)\s*(.*?)\s*$')
PLACEHOLDER = re.compile(r'\{(\w+)\}')
LABEL = re.compile(r'[A-Za-z0-9_.-]+')
ENTRIES = ('tier', 'needs', 'timeout', 'manual', 'exclude', 'skips-ok')

TIER_NAMES = {0: 'lint', 1: 'unit', 2: 'integration', 3: 'kernel-synthetic', 4: 'hardware-synthetic', 5: 'human'}
TIER_NUMBERS = {name: number for number, name in TIER_NAMES.items()}
GROUPS = {'fast': ((0, False), (1, False)), 'visual': ((4, True),), 'all': tuple((t, False) for t in TIER_NAMES)}
DEFAULT_TIMEOUTS = {0: 60, 1: 60, 2: 300, 3: 300, 4: 900, 5: 3600}
MAX_TIMEOUT = 7200
PARALLEL_TIERS = (0, 1)

PASS, FAIL, TIMEOUT, INCOMPLETE, REGISTRY = 'PASS', 'FAIL', 'TIMEOUT', 'INCOMPLETE', 'REGISTRY'
OUTPUT_LINES = 40  # lines of a test's output that are shown and recorded when it did not pass
OUTPUT_BYTES = 12000


# --- Needs ---------------------------------------------------------------------------------------------

def first_line(argv, timeout=10, env=None, digit=False):
    """The first line that a command prints (with a digit in it, if asked), or '' if it cannot be run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors='replace',
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return ''
    for line in (done.stdout + done.stderr).splitlines():
        if line.strip() and (not digit or re.search(r'\d', line)):
            return line.strip()[:120]
    return ''


def binaries(*names):
    """A need that is met when every named program is on PATH."""
    def probe(context):
        missing = [name for name in names if not shutil.which(name)]
        return (False, 'not found: ' + ', '.join(missing)) if missing else (True, ', '.join(names))
    return probe


def versioned(name, *argv):
    """A need that is met when the program is on PATH; the answer carries its version."""
    def probe(context):
        if not shutil.which(name):
            return False, 'not found: ' + name
        return True, first_line([name, *argv]) or name
    return probe


def modules(*names):
    def probe(context):
        missing = [name for name in names if importlib.util.find_spec(name) is None]
        return (False, 'Python module not found: ' + ', '.join(missing)) if missing else (True, ', '.join(names))
    return probe


def works(argv, what, timeout=20):
    """A need that is met when a small command succeeds: it shows that the feature works, not only that it exists."""
    def probe(context):
        if not shutil.which(argv[0]):
            return False, 'not found: ' + argv[0]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as problem:
            return False, f'{what}: {problem}'
        return (True, what) if done.returncode == 0 else (False, f'{what}: exit status {done.returncode}')
    return probe


def need_ordinary_user(context):
    return os.geteuid() != 0, f'uid {os.geteuid()}'


def need_root(context):
    return os.geteuid() == 0, f'uid {os.geteuid()}'


def need_private_tree(context):
    if context.public:  # a published run behaves as the exported tree does, which has no such directory
        return False, 'a --public run does not use the development-only directory'
    present = (context.root / PRIVATE).is_dir()
    return present, 'development checkout' if present else 'the development-only directory is absent'


def need_dbus_session(context):
    if not shutil.which('dbus-run-session'):
        return False, 'not found: dbus-run-session'
    return modules('dbus', 'gi')(context)


def need_wayland_client(context):
    """The probe window of the compositor tests: wayland-scanner and the client library with its protocols."""
    if not shutil.which('wayland-scanner') or not shutil.which('pkg-config'):
        return False, 'not found: wayland-scanner and pkg-config'
    if subprocess.run(['pkg-config', '--exists', 'wayland-client', 'wayland-protocols']).returncode != 0:
        return False, 'pkg-config cannot find wayland-client and wayland-protocols'
    return True, first_line(['pkg-config', '--modversion', 'wayland-client']) or 'wayland-client'


def need_pipewire_audio(context):
    names = ('pipewire', 'pipewire-pulse', 'pactl', 'paplay', 'parec', 'wireplumber', 'dbus-daemon')
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        return False, 'not found: ' + ', '.join(missing)
    try:
        shown = subprocess.run(['wireplumber', '--version'], capture_output=True, text=True, timeout=10,
                               stdin=subprocess.DEVNULL, errors='replace')
        text = shown.stdout + shown.stderr
    except (OSError, subprocess.SubprocessError):
        text = ''
    found = re.search(r'libwireplumber (\d+\.\d+\.\d+)', text)
    if not found or not found.group(1).startswith('0.5.'):
        return False, 'WirePlumber 0.5 is required, found ' + (found.group(1) if found else 'no version')
    return True, 'libwireplumber ' + found.group(1)


def need_uinput(context):
    writable = os.access('/dev/uinput', os.W_OK)
    return writable, '/dev/uinput is writable' if writable else '/dev/uinput is not writable'


def need_capture(context):
    devices = sorted(Path('/dev').glob('video*'))
    return bool(devices), str(devices[0]) if devices else 'no video capture device'


def wlroots_archive_cached(root):
    """Whether the wrap's wlroots archive is in its package cache, fetching it once if it is not (meson subprojects
    download, which needs the network). False when it cannot be had."""
    wrap = root / WLROOTS_WRAP
    cache = wrap.parent / 'packagecache' / WLROOTS_ARCHIVE
    if cache.is_file():
        return True
    if not shutil.which('meson'):
        return False
    try:
        subprocess.run(['meson', 'subprojects', 'download', 'wlroots'], cwd=wrap.parent.parent, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cache.is_file()


def wlroots_choice(root):
    """(meson option value, description) of the wlroots the compositor is built against: the patched subproject
    when the engineering tree's wrap is there and its archive is cached or can be fetched, else the system library
    (said so in the description, since it lacks the release's patches), else None."""
    note = ''
    if (root / WLROOTS_WRAP).is_file():
        if wlroots_archive_cached(root):
            return 'subproject', WLROOTS_SUBPROJECT
        note = (f' (unpatched: the wrap\'s {WLROOTS_ARCHIVE} is not in its package cache and could not be fetched; '
                'the X11 presentation test fails on it)')
    environment = dict(os.environ, PKG_CONFIG_PATH=os.environ.get('PKG_CONFIG_PATH', '') + WLROOTS_PKG_CONFIG)
    if shutil.which('pkg-config') and subprocess.run(['pkg-config', '--exists', 'wlroots'], env=environment).returncode == 0:
        return 'system', ('system wlroots ' + first_line(['pkg-config', '--modversion', 'wlroots'], env=environment)
                          + note)
    return None, 'no wlroots: neither the subproject wrap with its archive nor a system library pkg-config finds'


def build_tools(root):
    """(met, detail, environment) for the tools that build the compositor: meson, wayland-scanner, pkg-config and
    a wlroots. In the engineering tree that is the subproject wrap in the compositor's subprojects directory (the
    0.17.4 the release bundles, with the release's patches), so that the tests see the backend behaviour the
    devices get; a tree without the wrap (the published one) builds against the system wlroots."""
    missing = [name for name in ('meson', 'wayland-scanner', 'pkg-config') if not shutil.which(name)]
    environment = dict(os.environ, PKG_CONFIG_PATH=os.environ.get('PKG_CONFIG_PATH', '') + WLROOTS_PKG_CONFIG)
    if missing:
        return False, 'not found: ' + ', '.join(missing), environment
    choice, wlroots = wlroots_choice(root)
    if choice is None:
        return False, wlroots, environment
    return True, f'meson, wayland-scanner and {wlroots}', environment


def need_compositor_build(context):
    met, detail, _ = build_tools(context.root)
    return met, detail


def need_compositor(context):
    return context.compositor()


# What a test can ask for: the name, what it stands for, and a function of the run's context that returns
# (met, detail). The detail, for example a tool's version, goes into the evidence record.
NEED_TABLE = (
    ('ordinary-user', 'the runner is not root', need_ordinary_user),
    ('root', 'the runner is root; only manual tests may ask for it', need_root),
    ('private-tree', 'the development-only directory exists (never met in a --public run, as in the export)',
     need_private_tree),
    ('cc', 'a C compiler', versioned('cc', '--version')),
    ('acl', 'setfacl', versioned('setfacl', '--version')),
    ('sudo-tools', 'visudo and cvtsudoers, from the sudo package', binaries('visudo', 'cvtsudoers')),
    ('git', 'git', versioned('git', '--version')),
    ('python-yaml', 'PyYAML for the Python that runs the tests', modules('yaml')),
    ('dbus-session', 'dbus-run-session and the Python modules dbus and gi', need_dbus_session),
    ('systemd-user', 'a user service manager that runs transient services',
     works(['systemd-run', '--user', '--quiet', '--wait', '--collect', '--expand-environment=no', 'true'],
           'a transient user service runs')),
    ('userns', 'unprivileged user namespaces',
     works(['unshare', '--user', '--map-root-user', 'true'], 'an unprivileged user namespace opens')),
    ('wayland-info', 'wayland-info', binaries('wayland-info')),
    ('wayland-client', 'wayland-scanner and the Wayland client library with its protocols', need_wayland_client),
    ('xvfb', 'Xvfb, xdotool, xwininfo and xwd', binaries('Xvfb', 'xdotool', 'xwininfo', 'xwd')),
    ('pipewire-audio', 'PipeWire, PipeWire-Pulse, WirePlumber 0.5 and the Pulse tools', need_pipewire_audio),
    ('compositor-build', 'the tools that build the compositor: meson, wayland-scanner, pkg-config and a wlroots (the '
     "patched 0.17.4 subproject when the compositor's subprojects wrap is there, else the system library)",
     need_compositor_build),
    ('compositor', 'the compositor itself, built once per run from this tree (needs compositor-build)',
     need_compositor),
    ('uinput', 'write access to /dev/uinput', need_uinput),
    ('capture', 'a video capture device', need_capture),
    ('gamescope', 'gamescope', binaries('gamescope')),
    ('xephyr-tools', 'Xephyr, wmctrl and xauth', binaries('Xephyr', 'wmctrl', 'xauth')),
)
NEEDS = {name: probe for name, _, probe in NEED_TABLE}
__doc__ = __doc__ % {'needs': '\n'.join(f'    {name:<18}{what}' for name, what, _ in NEED_TABLE)}


# --- Declarations --------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Run:
    label: str
    args: tuple


@dataclasses.dataclass
class Declaration:
    path: str  # relative to the tree, with '/' separators
    tier: int | None = None
    needs: tuple = ()
    timeout: int = 0
    manual: str | None = None
    exclude: str | None = None
    skips_ok: str | None = None
    serial: bool = False
    runs: tuple = (Run('', ()),)
    problems: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Case:
    """One run of one test file."""
    declaration: Declaration
    run: Run

    @property
    def path(self):
        return self.declaration.path

    @property
    def id(self):
        return self.path + ('#' + self.run.label if self.run.label else '')

    @property
    def tier(self):
        return self.declaration.tier


def parse_tier_word(word):
    """The tier that a number or a tier name stands for, or None."""
    if word.isdigit() and int(word) in TIER_NAMES:
        return int(word)
    return TIER_NUMBERS.get(word)


def parse_tiers(spec):
    """What --tier selects, as a set of (tier, only the tests that need the capture card)."""
    selected = set()
    for item in (part.strip() for part in spec.split(',')):
        span = re.fullmatch(r'(\d)-(\d)', item)
        if parse_tier_word(item) is not None:
            selected.add((parse_tier_word(item), False))
        elif span and parse_tier_word(span.group(1)) is not None and parse_tier_word(span.group(2)) is not None \
                and int(span.group(1)) <= int(span.group(2)):
            selected.update((tier, False) for tier in range(int(span.group(1)), int(span.group(2)) + 1))
        elif item in GROUPS:
            selected.update(GROUPS[item])
        else:
            raise ValueError(f'unknown tier {item!r}; use 0-5, a range such as 0-2, or one of '
                             + ', '.join([*TIER_NUMBERS, *GROUPS]))
    return selected


def read_header(path):
    lines = []
    with path.open('r', encoding='utf-8', errors='replace') as stream:
        for _ in range(HEADER_LINES):
            line = stream.readline()
            if not line:
                break
            lines.append(line.rstrip('\r\n'))
    return lines


def parse_declaration(root, relative):
    """Read and validate the declaration of one test file. Problems are collected, never raised."""
    decl = Declaration(path=relative)
    problem = decl.problems.append
    try:
        lines = read_header(root / relative)
    except OSError as error:
        problem(f'{relative}: cannot be read: {error.strerror}')
        return decl
    declared = [match for match in map(TEST_LINE.match, lines) if match]
    if not declared:
        problem(f'{relative}: not registered. Add a "# dosplex-test:" line near the top (see tests/run.py --help), '
                'or mark the file manual="reason" or exclude="reason"')
        return decl
    if len(declared) > 1:
        problem(f'{relative}: has {len(declared)} "# dosplex-test:" lines; it needs exactly one')
        return decl
    try:
        tokens = shlex.split(declared[0].group(1))
    except ValueError as error:
        problem(f'{relative}: the "# dosplex-test:" line cannot be read: {error}')
        return decl
    seen, tier_word, timeout = set(), None, None
    for token in tokens:
        key, equals, value = token.partition('=')
        if key in seen:
            problem(f'{relative}: {key} is given twice')
        seen.add(key)
        if key == 'serial' and not equals:
            decl.serial = True
        elif not equals or key not in ENTRIES:
            problem(f'{relative}: unknown entry {token!r}; the entries are ' + ', '.join(ENTRIES) + ' and serial')
        elif key == 'tier':
            tier_word = value
        elif key == 'needs':
            decl.needs = tuple(name.strip() for name in value.split(',') if name.strip())
        elif key == 'timeout':
            timeout = value
        else:
            if not value.strip():
                problem(f'{relative}: {key} needs a reason, as in {key}="why"')
            setattr(decl, key.replace('-', '_'), value.strip())
    if decl.exclude is not None:
        if seen - {'exclude'}:
            problem(f'{relative}: an excluded file has an exclude entry and nothing else')
        return decl
    if tier_word is None:
        problem(f'{relative}: tier is required')
    elif parse_tier_word(tier_word) is None:
        problem(f'{relative}: unknown tier {tier_word!r}')
    else:
        decl.tier = parse_tier_word(tier_word)
    if timeout is not None:
        if not timeout.isdigit() or not 0 < int(timeout) <= MAX_TIMEOUT:
            problem(f'{relative}: timeout must be a whole number of seconds from 1 to {MAX_TIMEOUT}')
        else:
            decl.timeout = int(timeout)
    if not decl.timeout and decl.tier is not None:
        decl.timeout = DEFAULT_TIMEOUTS[decl.tier]
    for name in decl.needs:
        if name not in NEEDS:
            problem(f'{relative}: unknown need {name!r}; the needs are ' + ', '.join(NEEDS))
    if 'root' in decl.needs and decl.manual is None:
        problem(f'{relative}: a test that needs root must be manual="why": the runner never runs tests as root '
                'on its own')
    runs = []
    for number, line in enumerate(lines, 1):
        match = RUN_LINE.match(line)
        if not match:
            continue
        label = match.group(1)
        try:
            args = tuple(shlex.split(match.group(2)))
        except ValueError as error:
            problem(f'{relative}:{number}: the "# dosplex-run:" line cannot be read: {error}')
            continue
        if not LABEL.fullmatch(label):
            problem(f'{relative}:{number}: run label {label!r} may hold letters, digits, dots, dashes and underscores')
        if label in [run.label for run in runs]:
            problem(f'{relative}:{number}: run label {label!r} is used twice')
        for argument in args:
            for name in PLACEHOLDER.findall(argument):
                if name != 'compositor':
                    problem(f'{relative}:{number}: unknown placeholder {{{name}}}; only {{compositor}} exists')
        runs.append(Run(label, args))
    if runs:
        decl.runs = tuple(runs)
    uses = any('{compositor}' in argument for run in decl.runs for argument in run.args)
    if uses and 'compositor' not in decl.needs:
        problem(f'{relative}: passes {{compositor}} but does not declare needs=compositor')
    if 'compositor' in decl.needs and not uses:
        problem(f'{relative}: declares needs=compositor but no "# dosplex-run:" line passes {{compositor}}')
    return decl


def candidate_files(root, directory):
    """Paths, relative to the tree, of the test-like files below one test directory."""
    found = []
    for folder, subdirectories, names in os.walk(root / directory):
        subdirectories[:] = sorted(d for d in subdirectories if d not in NOT_SEARCHED and not d.startswith('.'))
        found += [posixpath.join(Path(folder).relative_to(root).as_posix(), name)
                  for name in sorted(names) if CANDIDATE.fullmatch(name)]
    return found


def load_registry(root, include_private):
    """(declarations, problems) of every test-like file of the tree."""
    directories = [TEST_DIRECTORY] + ([PRIVATE_TEST_DIRECTORY] if include_private else [])
    declarations, problems = [], []
    for directory in directories:
        if not (root / directory).is_dir():
            problems.append(f'{directory}: the test directory does not exist')
            continue
        declarations += [parse_declaration(root, relative) for relative in candidate_files(root, directory)]
    for decl in declarations:
        problems += decl.problems
    return declarations, problems


def registry_problems(root, include_private=False):
    """Everything wrong with the declarations of a tree: unregistered files and malformed declarations."""
    return load_registry(Path(root), include_private)[1]


# --- Selection -----------------------------------------------------------------------------------------

def publication_manifest(root):
    """The paths that the tree publishes, or None if it has no manifest."""
    try:
        text = (root / '.publication-files').read_text(encoding='utf-8')
    except OSError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def pattern_matches(case, pattern):
    """Whether a name given on the command line picks this case: its id, its path with or without the extension,
    the end of the path, the bare file name or a glob over any of them."""
    bare = posixpath.splitext(case.path)[0]
    stem = posixpath.basename(bare)
    return (pattern == stem or any(fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(name, '*/' + pattern)
                                   for name in (case.id, case.path, bare)))


def in_tiers(decl, selected):
    return (decl.tier, False) in selected or ((decl.tier, True) in selected and 'capture' in decl.needs)


def select_cases(declarations, selected, patterns, include_manual, manifest):
    """(cases to run, manual cases left out, excluded declarations). With patterns, only matching tests count."""
    cases, left_out, excluded = [], [], []
    for decl in declarations:
        if decl.problems:
            continue
        if decl.exclude is not None:
            excluded.append(decl)
            continue
        if manifest is not None and decl.path not in manifest:
            continue
        for run in decl.runs:
            case = Case(decl, run)
            if patterns and not any(pattern_matches(case, pattern) for pattern in patterns):
                continue
            if not in_tiers(decl, selected):
                continue
            (left_out if decl.manual is not None and not include_manual else cases).append(case)
    return cases, left_out, excluded


# --- Running -------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Result:
    id: str
    path: str
    tier: int | None
    status: str
    seconds: float = 0.0
    returncode: int | None = None
    note: str = ''
    needs: tuple = ()
    args: tuple = ()
    label: str = ''
    skipped: int = 0
    output: str = ''

    def record(self):
        entry = {'id': self.id, 'path': self.path, 'tier': self.tier, 'status': self.status,
                 'seconds': round(self.seconds, 3), 'returncode': self.returncode, 'needs': list(self.needs)}
        for key, value in (('label', self.label), ('args', list(self.args)), ('note', self.note),
                           ('skipped', self.skipped)):
            if value:
                entry[key] = value
        if self.status != PASS and self.output:
            entry['output_tail'] = self.output
        return entry


REPOSITORY_VARIABLES = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX',
                        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE',
                        'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM')


def clean_environment(extra=None):
    """What tests and git queries get: no DOSPLEX_* settings, no variable that points git at a repository."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('DOSPLEX_') and key not in REPOSITORY_VARIABLES}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env.update(extra or {})
    return env


def signal_group(pid, number):
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def group_alive(pid):
    """Whether any process is left in the process group that a test started."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_tail(path):
    """The last lines of a log, bounded in lines and in bytes."""
    try:
        data = path.read_bytes()
    except OSError:
        return ''
    text = '\n'.join(data[-OUTPUT_BYTES * 4:].decode('utf-8', errors='replace').splitlines()[-OUTPUT_LINES:])
    return text[-OUTPUT_BYTES:]


def count_skips(path):
    """How many lines of a log start with SKIP:, and how many tests unittest says it skipped."""
    lines, unittest_skips, last = 0, 0, []
    try:
        with path.open('rb') as stream:
            for line in stream:
                lines += line.startswith(b'SKIP:')
                last = (last + [line])[-4:]
    except OSError:
        return 0, 0
    for line in last:
        found = re.match(rb'(?:OK|FAILED) \(.*?\bskipped=(\d+)', line)
        if found:
            unittest_skips = int(found.group(1))
    return lines, unittest_skips


class Runner:
    """Runs single tests, each in a process group and a scratch directory of its own."""

    def __init__(self, root, scratch, kill_grace, show_output):
        self.root = root
        self.scratch = scratch
        self.kill_grace = kill_grace
        self.show_output = show_output
        self.compositor_path = None
        self.lock = threading.Lock()
        self.processes = set()
        self.counter = 0

    def kill_all(self):
        with self.lock:
            alive = list(self.processes)
        for process in alive:
            signal_group(process.pid, signal.SIGKILL)

    def argv(self, case):
        arguments = [argument.replace('{compositor}', str(self.compositor_path)) for argument in case.run.args]
        program = [sys.executable] if case.path.endswith('.py') else ['bash']
        return [*program, str(self.root / case.path), *arguments]

    def execute(self, case):
        with self.lock:
            self.counter += 1
            folder = self.scratch / f't{self.counter}'
        (folder / 'tmp').mkdir(parents=True)
        folder.chmod(0o755)
        (folder / 'tmp').chmod(0o1777)  # like /tmp: a test may hand a directory of its own to another user
        (folder / 'cwd').mkdir()
        log = folder / 'output.log'
        decl = case.declaration
        result = Result(case.id, case.path, case.tier, PASS, needs=decl.needs, args=case.run.args, label=case.run.label)
        started = time.monotonic()
        timed_out = False
        with log.open('wb') as stream:
            try:
                process = subprocess.Popen(self.argv(case), cwd=folder / 'cwd', stdin=subprocess.DEVNULL,
                                           env=clean_environment({'TMPDIR': str(folder / 'tmp')}),
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                result.status, result.note = FAIL, f'could not start: {error.strerror}'
                shutil.rmtree(folder, ignore_errors=True)
                return result
            with self.lock:
                self.processes.add(process)
            try:
                process.wait(timeout=decl.timeout)
            except subprocess.TimeoutExpired:
                # Ask the whole group to stop, give it the grace period to do so, then kill what is left.
                timed_out = True
                deadline = time.monotonic() + self.kill_grace
                signal_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=self.kill_grace)
                except subprocess.TimeoutExpired:
                    pass
                while group_alive(process.pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                signal_group(process.pid, signal.SIGKILL)
                process.wait()
            finally:
                signal_group(process.pid, signal.SIGKILL)  # whatever the test left running in its group
                with self.lock:
                    self.processes.discard(process)
        result.seconds = time.monotonic() - started
        result.returncode = process.returncode
        if timed_out:
            result.status, result.note = TIMEOUT, f'killed after {decl.timeout} s'
        elif process.returncode != 0:
            result.status = FAIL
            result.note = (f'killed by signal {-process.returncode}' if process.returncode < 0
                           else f'exit status {process.returncode}')
        else:
            lines, skipped = count_skips(log)
            result.skipped = lines + skipped
            if result.skipped and decl.skips_ok is None:
                result.status = INCOMPLETE
                result.note = (f'skipped {result.skipped} check(s) of its own; if that is expected, '
                               'declare skips-ok="why"')
            elif result.skipped:
                result.note = f'{result.skipped} skipped, accepted: {decl.skips_ok}'
        if result.status != PASS or self.show_output:
            result.output = log_tail(log)
        shutil.rmtree(folder, ignore_errors=True)
        return result


# --- The compositor ------------------------------------------------------------------------------------

WLROOTS_PKG_CONFIG = ':/usr/lib/wlroots0.17/pkgconfig:/usr/lib/x86_64-linux-gnu/pkgconfig'
# What the compositor is built against (compositor/meson.build -Dwlroots): in the engineering tree the wlroots
# archive the release bundles (the wrap below, same URL and SHA-256 as the release recipe's sources.json) with the
# release's patches applied by meson, so that a test sees the backend the devices run; meson fetches the archive
# once into the wrap's package cache. The published tree has no wrap and builds against the system wlroots 0.17,
# which is unpatched: under its X11 backend presentation feedback is answered one host commit late.
WLROOTS_WRAP = Path('compositor', 'subprojects', 'wlroots.wrap')
WLROOTS_ARCHIVE = 'wlroots-0.17.4.tar.gz'  # the wrap's source_filename
WLROOTS_SUBPROJECT = 'wlroots 0.17.4 (the patched subproject)'


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


class Context:
    """What the run knows about this machine: the answers to the needs, and the one build of the compositor."""

    def __init__(self, root, scratch, supplied, build_directory, say, public=False):
        self.root = root
        self.scratch = scratch
        self.supplied = supplied
        self.build_directory = build_directory
        self.say = say
        self.public = public
        self.answers = {}
        self.build = None  # what providing the compositor did, for the record
        self.build_output = ''

    def need(self, name):
        if name not in self.answers:
            met, detail = NEEDS[name](self)
            self.answers[name] = (bool(met), detail)
        return self.answers[name]

    def compositor(self):
        """The compositor binary, provided once: the one that was supplied, or a build of this tree."""
        if self.build is None:
            self.build = self.provide_compositor()
        return (True, self.build['binary']) if self.build['ok'] else (False, self.build['detail'])

    def provide_compositor(self):
        if self.supplied is not None:
            binary = self.supplied.resolve()
            if not (binary.is_file() and os.access(binary, os.X_OK)):
                return {'ok': False, 'source': 'supplied', 'detail': f'{self.supplied} is not an executable file'}
            return {'ok': True, 'source': 'supplied', 'binary': str(binary), 'binary_sha256': file_digest(binary)}
        met, detail, environment = build_tools(self.root)
        if not met:
            return {'ok': False, 'source': 'build', 'detail': 'the build needs the build tools: ' + detail}
        choice, wlroots = wlroots_choice(self.root)
        build = self.build_directory or self.scratch / 'build'
        self.say('building the compositor')
        started = time.monotonic()
        configured = (build / 'meson-private' / 'coredata.dat').is_file()
        output = ''
        for step in (['meson', 'setup', *(['--reconfigure'] if configured else []), str(build),
                      str(self.root / 'compositor'), f'-Dwlroots={choice}'], ['meson', 'compile', '-C', str(build)]):
            done = subprocess.run(step, capture_output=True, text=True, env=environment, errors='replace',
                                  stdin=subprocess.DEVNULL)
            output += done.stdout + done.stderr
            if done.returncode != 0:
                self.build_output = output
                return {'ok': False, 'source': 'build', 'detail': f'the compositor build failed ({step[1]})',
                        'seconds': round(time.monotonic() - started, 1)}
        binary = build / 'dosplex-compositor'
        if not binary.is_file():
            self.build_output = output
            return {'ok': False, 'source': 'build', 'detail': 'the build did not produce dosplex-compositor'}
        seconds = round(time.monotonic() - started, 1)
        # The compositor's own warnings; the subproject's (wlroots) are not counted.
        warnings = sum('warning:' in line and 'subprojects/' not in line for line in output.splitlines())
        self.say(f'compositor built in {seconds} s ({warnings} compiler warnings) against {wlroots}')
        return {'ok': True, 'source': 'build', 'binary': str(binary), 'seconds': seconds, 'warnings': warnings,
                'binary_sha256': file_digest(binary), 'wlroots': wlroots}


# --- Evidence ------------------------------------------------------------------------------------------

# The tools whose versions go into every record, whether or not a selected test uses them.
TOOL_VERSIONS = (
    ('git', ['git', '--version']), ('cc', ['cc', '--version']), ('meson', ['meson', '--version']),
    ('ninja', ['ninja', '--version']), ('pkg-config', ['pkg-config', '--version']),
    ('wayland-scanner', ['wayland-scanner', '--version']), ('wlroots', ['pkg-config', '--modversion', 'wlroots']),
    ('systemd', ['systemctl', '--version']), ('pipewire', ['pipewire', '--version']),
    ('wireplumber', ['wireplumber', '--version']), ('setfacl', ['setfacl', '--version']),
    ('unshare', ['unshare', '--version']), ('xdotool', ['xdotool', 'version']), ('bash', ['bash', '--version']),
)


def tool_versions():
    """Python and the versions of the tools that the tests use, for the tools that exist on this machine."""
    environment = dict(os.environ, PKG_CONFIG_PATH=os.environ.get('PKG_CONFIG_PATH', '') + WLROOTS_PKG_CONFIG)
    versions = {'python': platform.python_version()}
    for name, argv in TOOL_VERSIONS:
        if shutil.which(argv[0]):
            line = first_line(argv, env=environment, digit=True)
            if line:
                versions[name] = line
    return versions


def git_output(root, *arguments):
    done = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True, env=clean_environment(),
                          timeout=60, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, arguments)
    return done.stdout


def git_state(root, evidence=None):
    """The commit of the tree and what is uncommitted, or {'available': False} outside a checkout."""
    try:
        top = Path(git_output(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        if top != root.resolve():
            return {'available': False, 'note': 'the tree is inside a different repository'}
        sha = git_output(root, 'rev-parse', 'HEAD').decode().strip()
        branch = git_output(root, 'rev-parse', '--abbrev-ref', 'HEAD').decode().strip()
        diff = git_output(root, 'diff', 'HEAD', '--binary', '--no-ext-diff', '--no-textconv', '--no-color')
        untracked = [name for name in git_output(root, 'ls-files', '--others', '--exclude-standard', '-z')
                     .decode(errors='replace').split('\0') if name]
    except (OSError, subprocess.SubprocessError):
        return {'available': False, 'note': 'git could not describe the tree'}
    if evidence is not None:  # the records of earlier runs are not part of what was tested
        untracked = [name for name in untracked if not (root / name).resolve().is_relative_to(evidence)]
    digest = hashlib.sha256(diff)
    for name in sorted(untracked):
        digest.update(b'\0untracked\0' + name.encode(errors='replace') + b'\0')
        try:
            digest.update(hashlib.sha256((root / name).read_bytes()).digest())
        except OSError:
            digest.update(b'unreadable')
    dirty = bool(diff) or bool(untracked)
    return {'available': True, 'sha': sha, 'branch': branch, 'dirty': dirty,
            'dirty_diff_sha256': digest.hexdigest() if dirty else None, 'untracked_files': len(untracked)}


def write_evidence(directory, record):
    """Write the record under a name of its own: an earlier record is never overwritten. Returns the path."""
    directory.mkdir(parents=True, exist_ok=True)
    git = record['git']
    stamp = datetime.datetime.strptime(record['started_utc'], '%Y-%m-%dT%H:%M:%SZ')
    name = '-'.join([stamp.strftime('%Y-%m-%d'), git['sha'][:12] if git.get('available') else 'no-git']
                    + (['dirty'] if git.get('dirty') else []) + [stamp.strftime('%H%M%SZ')])
    text = json.dumps(record, indent=2) + '\n'
    for attempt in range(1, 100):
        path = directory / (name + ('' if attempt == 1 else f'-{attempt}') + '.json')
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
        return path
    raise OSError('too many evidence records with the same name')


def write_junit(path, results, seconds):
    failed = (FAIL, TIMEOUT)
    suite = ElementTree.Element('testsuite', name='dosplex', tests=str(len(results)), time=f'{seconds:.3f}',
                                failures=str(sum(r.status in failed for r in results)),
                                errors=str(sum(r.status not in (PASS, *failed) for r in results)))
    for result in results:
        case = ElementTree.SubElement(suite, 'testcase', classname=TIER_NAMES.get(result.tier, 'registry'),
                                      name=result.id, time=f'{result.seconds:.3f}')
        if result.status in failed:
            ElementTree.SubElement(case, 'failure', message=f'{result.status}: {result.note}').text = result.output
        elif result.status != PASS:
            ElementTree.SubElement(case, 'error', message=f'{result.status}: {result.note}').text = result.output
    path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


# --- The run -------------------------------------------------------------------------------------------

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Report:
    """What is printed while the run goes on. One lock keeps the lines of parallel tests apart."""

    def __init__(self, quiet):
        self.quiet = quiet
        self.lock = threading.Lock()

    def line(self, text, always=False):
        if self.quiet and not always:
            return
        with self.lock:
            try:
                print(text, flush=True)
            except BrokenPipeError:
                # Whoever reads our output went away (| head). The run goes on and ends with its exit status.
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())

    def result(self, result):
        if self.quiet and result.status == PASS:
            return
        tier = f'{result.tier} {TIER_NAMES[result.tier]}' if result.tier is not None else '-'
        text = f'{result.status:<10} {tier:<20} {result.seconds:7.2f}s  {result.id}'
        if result.note:
            text += '  ' + result.note
        if result.output:
            text += '\n' + '\n'.join('    | ' + row for row in result.output.splitlines())
        self.line(text, always=True)


def order_for_speed(cases):
    """The longest allowance first, so that the slow tests of a parallel tier start together."""
    return sorted(cases, key=lambda case: (-case.declaration.timeout, case.id))


def run_tier(runner, context, cases, tier, jobs, report, results, fail_fast):
    """Check the needs of the cases of one tier, then run them: in parallel for the fast tiers."""
    if any('compositor' in case.declaration.needs for case in cases):
        context.need('compositor')
    runnable = []
    for case in cases:
        unmet = [f'{name} ({detail})' for name in case.declaration.needs
                 for met, detail in [context.need(name)] if not met]
        if unmet:
            results.append(Result(case.id, case.path, tier, INCOMPLETE, needs=case.declaration.needs,
                                  args=case.run.args, label=case.run.label,
                                  note='not run, needs not met: ' + '; '.join(unmet)))
            report.result(results[-1])
        else:
            runnable.append(case)
    runner.compositor_path = context.build['binary'] if context.build and context.build['ok'] else None
    parallel = [c for c in runnable if tier in PARALLEL_TIERS and not c.declaration.serial] if jobs > 1 else []
    if parallel:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(parallel))) as pool:
            futures = [pool.submit(runner.execute, case) for case in order_for_speed(parallel)]
            try:
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())
                    report.result(results[-1])
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                runner.kill_all()
                raise
    for case in sorted((c for c in runnable if c not in parallel), key=lambda c: c.id):
        results.append(runner.execute(case))
        report.result(results[-1])
        if fail_fast and results[-1].status in (FAIL, TIMEOUT):
            break


def describe_tiers(tiers):
    """Tier numbers as ranges: [0, 1, 2, 4] becomes '0-2,4'."""
    parts, tiers = [], sorted(tiers)
    while tiers:
        end = 0
        while end + 1 < len(tiers) and tiers[end + 1] == tiers[end] + 1:
            end += 1
        parts.append(str(tiers[0]) if end == 0 else f'{tiers[0]}-{tiers[end]}')
        tiers = tiers[end + 1:]
    return ','.join(parts)


def summarise(results, other_failures, seconds, tiers, report):
    """Print the table of counts and the verdict. Returns the verdict word.

    other_failures are causes of failure that are not a failed test: registry problems, a build that failed.
    """
    rows = {}
    for result in results:
        rows.setdefault(result.tier, []).append(result)
    report.line('')
    report.line(f'{"tier":<22}{"tests":>6}{"pass":>6}{"fail":>6}{"incomplete":>11}{"seconds":>9}')
    for tier in sorted(rows):
        group = rows[tier]
        report.line(f'{str(tier) + " " + TIER_NAMES[tier]:<22}{len(group):>6}'
                    f'{sum(r.status == PASS for r in group):>6}{sum(r.status in (FAIL, TIMEOUT) for r in group):>6}'
                    f'{sum(r.status == INCOMPLETE for r in group):>11}{sum(r.seconds for r in group):>9.1f}')
    if len(results) > 3:
        slowest = sorted(results, key=lambda r: -r.seconds)[:3]
        report.line('slowest: ' + ', '.join(f'{r.id} {r.seconds:.1f}s' for r in slowest))
    failed = [r for r in results if r.status in (FAIL, TIMEOUT)]
    incomplete = [r for r in results if r.status == INCOMPLETE]
    if failed or other_failures:
        causes = ([f'{len(failed)} of {len(results)} tests failed'] if failed else []) + list(other_failures) \
                 + ([f'{len(incomplete)} incomplete'] if incomplete else [])
        report.line(f'FAIL: {", ".join(causes)} ({seconds:.1f} s wall time)', always=True)
        return FAIL
    if incomplete or not results:
        report.line('INCOMPLETE: available checks passed, but required checks were skipped '
                    f'({len(incomplete) or "no test was selected"}; {seconds:.1f} s wall time)', always=True)
        return INCOMPLETE
    report.line(f'PASS: {len(results)} tests passed (tiers {describe_tiers(tiers)}; {seconds:.1f} s wall time); '
                'hardware acceptance remains separate', always=True)
    return PASS


def refuse_root(effective_uid, allow_root):
    """The reason the runner must not start, or None."""
    if effective_uid == 0 and not allow_root:
        return ('refusing to run as root. The tests are written for an ordinary user, and a test that needs root '
                'is manual and has to be named: sudo python3 tests/run.py --allow-root --include-manual TEST')
    return None


def print_listing(declarations, problems, report):
    report.line(f'{"tier":<22}{"timeout":>8}  {"mode":<9} test')
    for decl in declarations:
        if decl.problems or decl.exclude is not None:
            continue
        mode = ('manual' if decl.manual is not None
                else 'serial' if decl.tier not in PARALLEL_TIERS or decl.serial else 'parallel')
        for run in decl.runs:
            needs = f'  needs {",".join(decl.needs)}' if decl.needs else ''
            report.line(f'{str(decl.tier) + " " + TIER_NAMES[decl.tier]:<22}{decl.timeout:>7}s  {mode:<9} '
                        f'{Case(decl, run).id}{needs}')
            if decl.manual is not None:
                report.line(f'{"":<42}manual: {decl.manual}')
    for decl in declarations:
        if decl.exclude is not None:
            report.line(f'{"-":<22}{"":>8}  {"excluded":<9} {decl.path}')
            report.line(f'{"":<42}excluded: {decl.exclude}')
    for problem in problems:
        report.line(f'{REGISTRY}: {problem}')


def build_parser():
    parser = argparse.ArgumentParser(prog='run.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('tests', nargs='*', metavar='TEST',
                        help='run only the tests that match: a path, the end of a path, a file name without its '
                             'extension, or a glob. Without --tier, every tier is considered')
    parser.add_argument('--tier', metavar='TIERS', help='which tiers to run (default: fast)')
    parser.add_argument('--include-private', action='store_true',
                        help='also discover the development-only tests of the development checkout')
    parser.add_argument('--public', action='store_true',
                        help='run only the tests that the publication manifest lists, as in the exported tree')
    parser.add_argument('--include-manual', action='store_true',
                        help='run the manual tests that TEST names (they are never part of a tier)')
    parser.add_argument('--allow-root', action='store_true', help='let the runner start as root')
    parser.add_argument('-j', '--jobs', type=int, metavar='N',
                        help='tests to run at once in tiers 0-1 (default: the number of processors, at most 8)')
    parser.add_argument('-x', '--fail-fast', action='store_true', help='stop after the first tier that has a failure')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT, help='the tree to test (default: this one)')
    parser.add_argument('--compositor', type=Path, metavar='BINARY',
                        help='use this compositor instead of building one')
    parser.add_argument('--build-dir', type=Path, metavar='DIR',
                        help='build the compositor in DIR/build and keep it (default: a temporary directory; '
                             'DOSPLEX_BUILD_DIR does the same)')
    parser.add_argument('--evidence-dir', type=Path, metavar='DIR',
                        help="write the JSON record of the run here (default: the maintainers' evidence "
                             'directory of a development checkout, otherwise nowhere)')
    parser.add_argument('--no-evidence', action='store_true', help='write no JSON record')
    parser.add_argument('--junit', type=Path, metavar='FILE', help='also write the results as JUnit XML')
    parser.add_argument('--list', action='store_true', help='list the tests with tier, needs and timeout; run nothing')
    parser.add_argument('--list-unregistered', action='store_true',
                        help='list the test files without a valid declaration: prints nothing and exits 0 if there '
                             'are none, exits 1 otherwise')
    parser.add_argument('-q', '--quiet', action='store_true', help='print only what did not pass, and the verdict')
    parser.add_argument('-v', '--verbose', action='store_true', help='show the output of passing tests too')
    parser.add_argument('--kill-grace', type=float, default=5.0, metavar='SECONDS',
                        help='time between SIGTERM and SIGKILL for a test that timed out (default: 5)')
    return parser


def interrupt(number, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    message = refuse_root(os.geteuid(), args.allow_root)
    if message:
        parser.error(message)
    root = args.root.resolve()
    try:
        selected = parse_tiers(args.tier or ('all' if args.tests else 'fast'))
    except ValueError as error:
        parser.error(str(error))
    if args.include_manual and not args.tests:
        parser.error('--include-manual needs the names of the manual tests to run')
    if args.jobs is not None and args.jobs < 1:
        parser.error('--jobs must be at least 1')
    declarations, problems = load_registry(root, args.include_private)
    report = Report(args.quiet)
    if args.list_unregistered:
        for problem in problems:
            report.line(problem, always=True)
        return 1 if problems else 0
    if args.list:
        print_listing(declarations, problems, report)
        return 1 if problems else 0
    manifest = publication_manifest(root) if args.public else None
    if args.public and manifest is None:
        parser.error('--public needs the publication manifest, which this tree does not have')
    cases, left_out, excluded = select_cases(declarations, selected, args.tests, args.include_manual, manifest)
    for pattern in args.tests:
        if any(pattern_matches(case, pattern) for case in cases):
            continue
        for case in left_out:
            if pattern_matches(case, pattern):
                parser.error(f'{case.path} is manual ({case.declaration.manual}); name it together with '
                             '--include-manual to run it')
        for decl in excluded:
            if pattern_matches(Case(decl, decl.runs[0]), pattern):
                parser.error(f'{decl.path} is excluded and is never run: {decl.exclude}')
        parser.error(f'no test matches {pattern!r} in the selected tiers (see --list)')

    jobs = args.jobs or min(8, os.cpu_count() or 2)
    build_directory = args.build_dir or (Path(os.environ['DOSPLEX_BUILD_DIR']) / 'build'
                                         if os.environ.get('DOSPLEX_BUILD_DIR') else None)
    evidence = None if args.no_evidence else args.evidence_dir or (
        root / EVIDENCE_DIRECTORY if (root / PRIVATE).is_dir() else None)
    if evidence is not None:
        evidence = evidence.resolve()
    report.line(f'run.py: {len(cases)} tests in tiers {describe_tiers({t for t, _ in selected})}'
                + (f'; {len(left_out)} manual test(s) not run' if left_out else '')
                + (f'; {len(problems)} registry problem(s)' if problems else ''))
    registry = []
    for problem in problems:
        registry.append(Result(problem.split(':')[0], problem.split(':')[0], None, REGISTRY, note=problem))
        report.line(f'{REGISTRY:<10} {problem}', always=True)

    scratch = Path(tempfile.mkdtemp(prefix='dr-'))
    scratch.chmod(0o755)  # other uids that a test emulates must be able to walk down to their files
    context = Context(root, scratch, args.compositor, build_directory.resolve() if build_directory else None,
                      report.line, public=args.public)
    runner = Runner(root, scratch, args.kill_grace, args.verbose)
    results, interrupted = [], False
    started_utc, started = utc_now(), time.monotonic()
    state = git_state(root, evidence) if evidence is not None else None  # taken before the tests run
    signal.signal(signal.SIGTERM, interrupt)
    try:
        for tier in sorted({case.tier for case in cases}):
            run_tier(runner, context, [case for case in cases if case.tier == tier], tier, jobs, report, results,
                     args.fail_fast)
            if context.build_output:
                report.line('compositor build output (tail):\n' + '\n'.join(
                    '    | ' + row for row in context.build_output.splitlines()[-OUTPUT_LINES:]), always=True)
                context.build_output = ''
            if args.fail_fast and any(r.status in (FAIL, TIMEOUT) for r in results):
                report.line('stopping after the first failing tier (--fail-fast)', always=True)
                break
    except KeyboardInterrupt:
        interrupted = True
        runner.kill_all()
        report.line('interrupted: the tests that were running have been killed', always=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    seconds = time.monotonic() - started
    results.sort(key=lambda r: (r.tier, r.id))
    other_failures = ([f'{len(problems)} registry problem(s)'] if problems else []) \
        + (['the compositor could not be provided'] if context.build is not None and not context.build['ok'] else []) \
        + (['the run was interrupted, so it proves nothing'] if interrupted else [])
    verdict = summarise(results, other_failures, seconds, sorted({t for t, _ in selected}), report)
    everything = results + registry
    if evidence is not None:
        record = {
            'schema': EVIDENCE_SCHEMA, 'started_utc': started_utc, 'finished_utc': utc_now(),
            'duration_seconds': round(seconds, 2),
            'command': [Path(sys.argv[0]).name, *(argv if argv is not None else sys.argv[1:])],
            'selection': {'tiers': sorted({t for t, _ in selected}), 'include_private': args.include_private,
                          'public_only': args.public, 'include_manual': args.include_manual,
                          'patterns': args.tests, 'jobs': jobs},
            'git': state,
            'host': {'platform': platform.platform(), 'machine': platform.machine(),
                     'effective_uid': os.geteuid(), 'cpus': os.cpu_count()},
            'tools': tool_versions(),
            'needs': {name: {'met': met, 'detail': detail} for name, (met, detail) in sorted(context.answers.items())},
            'build': context.build,
            'results': [r.record() for r in everything],
            'summary': {status: sum(r.status == status for r in everything)
                        for status in (PASS, FAIL, TIMEOUT, INCOMPLETE, REGISTRY)},
            'not_run': {'manual': [case.id for case in left_out], 'excluded': [d.path for d in excluded]},
            'interrupted': interrupted,
            'verdict': verdict,
        }
        try:
            report.line(f'evidence: {write_evidence(evidence, record)}', always=True)
        except OSError as error:
            report.line(f'FAIL: the evidence record could not be written: {error}', always=True)
            verdict = FAIL
    else:
        report.line('evidence: not written (see --evidence-dir)')
    if args.junit:
        write_junit(args.junit, everything, seconds)
    return 0 if verdict == PASS else 1


if __name__ == '__main__':
    sys.exit(main())
