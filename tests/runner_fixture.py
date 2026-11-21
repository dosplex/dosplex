# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Shared fixtures of the runner tests: a temporary tree with a tests directory and the real runner pointed at it.

Not a test. The runner tests (run-test.py, run-lifecycle-test.py, run-evidence-test.py) import it. Fixture paths
are assembled from parts so that no file spells out a reference to a file that does not exist.
"""
import importlib.util
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'tests' / 'run.py'
spec = importlib.util.spec_from_file_location('dosplex_run', RUNNER)
run = importlib.util.module_from_spec(spec)
sys.modules['dosplex_run'] = run  # dataclasses look their module up here
spec.loader.exec_module(run)

TESTS = 'tests'
DECLARATION = '# ' + 'dosplex-test: '
RUN_LINE = '# ' + 'dosplex-run: '
PASSING = 'raise SystemExit(0)\n'
FAILING = 'print("this test fails")\nraise SystemExit(3)\n'


def rel(*parts):
    return posixpath.join(*parts)


def alive(pid):
    """Whether a process exists and is not a zombie."""
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != 'Z'


def wait_until(condition, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class TreeCase(unittest.TestCase):
    """A temporary tree with a tests directory, and the real runner pointed at it."""

    def setUp(self):
        if os.geteuid() == 0:
            self.fail('run these tests as an ordinary user: the runner refuses to start as root')
        temp = tempfile.TemporaryDirectory(prefix='dosplex-runner-test-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'tree'
        (self.root / TESTS).mkdir(parents=True)

    def write(self, relative, text, mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def add(self, name, entries, body=PASSING, runs=(), directory=TESTS):
        """Write a test file with its declaration. `entries` is the text of the dosplex-test line."""
        lines = [DECLARATION + entries] if entries is not None else []
        lines += [RUN_LINE + one for one in runs]
        return self.write(rel(directory, name), '\n'.join(lines + [body]))

    def command(self, *arguments):
        return [sys.executable, str(RUNNER), '--root', str(self.root), *arguments]

    def runner(self, *arguments, env=None, timeout=120):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.update(env or {})
        return subprocess.run(self.command('--no-evidence', *arguments), capture_output=True, text=True,
                              env=environment, timeout=timeout)

    def assertExit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
