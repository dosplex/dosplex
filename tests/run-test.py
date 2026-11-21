#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=180
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The test runner (the file run.py in this directory) must fail closed.

Each case builds a small tree of fixture tests in a temporary directory and runs the real runner on it with
--root: a passing test, a failing one, a test whose needs are not met, a test file nobody registered, manual and
excluded tests, tests that need root, the parallel fast tiers, the environment of a test and the one build of the
compositor (with a stand-in for meson). run-lifecycle-test.py covers timeouts and interrupts, run-evidence-test.py
the evidence record, run-root-test.py the runner as root.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

sys.dont_write_bytecode = True
from runner_fixture import ROOT, RUNNER, run, TESTS, DECLARATION, PASSING, FAILING, rel, TreeCase  # noqa: E402


class DeclarationTests(TreeCase):
    def declared(self, name='x-test.py', text=None, **kwargs):
        if text is not None:
            self.write(rel(TESTS, name), text)
        else:
            self.add(name, **kwargs)
        return run.parse_declaration(self.root, rel(TESTS, name))

    def test_a_valid_declaration_is_read(self):
        decl = self.declared(entries='tier=unit needs=cc,git timeout=45 serial')
        self.assertEqual((decl.problems, decl.tier, decl.needs, decl.timeout, decl.serial),
                         ([], 1, ('cc', 'git'), 45, True))
        self.assertEqual(self.declared(entries='tier=2').tier, 2)
        self.assertEqual(self.declared(entries='tier=integration').timeout, run.DEFAULT_TIMEOUTS[2])
        self.assertEqual(self.declared(entries='tier=lint').timeout, run.DEFAULT_TIMEOUTS[0])

    def test_runs_and_the_compositor_placeholder(self):
        decl = self.declared(entries='tier=integration needs=compositor,ordinary-user',
                             runs=['left {compositor} --side left', 'right {compositor} --side right'])
        self.assertEqual(decl.problems, [])
        self.assertEqual([(r.label, r.args) for r in decl.runs],
                         [('left', ('{compositor}', '--side', 'left')), ('right', ('{compositor}', '--side', 'right'))])
        self.assertEqual([run.Case(decl, r).id for r in decl.runs], [rel(TESTS, 'x-test.py#left'),
                                                                     rel(TESTS, 'x-test.py#right')])

    def test_manual_and_exclude_carry_a_reason(self):
        decl = self.declared(entries='tier=unit manual="Needs a Deck. Run it by hand"')
        self.assertEqual((decl.problems, decl.manual), ([], 'Needs a Deck. Run it by hand'))
        decl = self.declared(entries='exclude="An installer, not a test"')
        self.assertEqual((decl.problems, decl.exclude, decl.tier), ([], 'An installer, not a test', None))
        decl = self.declared(entries='tier=unit skips-ok="root cases"')
        self.assertEqual((decl.problems, decl.skips_ok), ([], 'root cases'))

    def problems(self, **kwargs):
        return '\n'.join(self.declared(**kwargs).problems)

    def test_a_file_without_a_declaration_is_not_registered(self):
        self.assertIn('not registered', self.problems(entries=None))

    def test_malformed_declarations_are_reported(self):
        cases = [
            ('tier=unit tier=lint', 'tier is given twice'),
            ('tier=unit colour=red', 'unknown entry'),
            ('needs=cc', 'tier is required'),
            ('tier=sideways', 'unknown tier'),
            ('tier=unit timeout=soon', 'timeout must be'),
            ('tier=unit timeout=0', 'timeout must be'),
            ('tier=unit needs=magic', 'unknown need'),
            ('tier=unit manual=', 'needs a reason'),
            ('tier=unit manual="a', 'cannot be read'),
            ('exclude="x" tier=unit', 'exclude entry and nothing else'),
            ('tier=unit needs=root', 'must be manual'),
            ('tier=unit wibble', 'unknown entry'),
        ]
        for entries, expected in cases:
            with self.subTest(entries=entries):
                self.assertIn(expected, self.problems(entries=entries))

    def test_two_declaration_lines_are_one_too_many(self):
        text = '\n'.join([DECLARATION + 'tier=unit', DECLARATION + 'tier=lint', PASSING])
        self.assertIn('needs exactly one', self.problems(text=text))

    def test_run_lines_are_checked(self):
        self.assertIn('used twice', self.problems(entries='tier=unit', runs=['a one', 'a two']))
        self.assertIn('may hold letters', self.problems(entries='tier=unit', runs=['bad/label x']))
        self.assertIn('unknown placeholder {device}', self.problems(entries='tier=unit', runs=['a {device}']))
        self.assertIn('does not declare needs=compositor',
                      self.problems(entries='tier=integration', runs=['a {compositor}']))
        self.assertIn('no "# dosplex-run:" line passes {compositor}',
                      self.problems(entries='tier=integration needs=compositor'))

    def test_tier_selection_syntax(self):
        self.assertEqual(run.parse_tiers('fast'), {(0, False), (1, False)})
        self.assertEqual(run.parse_tiers('0-2'), {(0, False), (1, False), (2, False)})
        self.assertEqual(run.parse_tiers('1,integration,human'), {(1, False), (2, False), (5, False)})
        self.assertEqual(run.parse_tiers('hardware-synthetic'), {(4, False)})
        self.assertEqual(run.parse_tiers('visual'), {(4, True)})
        self.assertEqual(len(run.parse_tiers('all')), 6)
        for bad in ('7', '2-1', 'fastest', '', '0,,1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run.parse_tiers(bad)
        self.assertEqual(run.describe_tiers([0, 1, 2, 4]), '0-2,4')
        self.assertEqual(run.describe_tiers([3]), '3')
        self.assertEqual(run.describe_tiers(range(6)), '0-5')

    def test_the_runner_refuses_root_unless_told_otherwise(self):
        self.assertIn('refusing to run as root', run.refuse_root(0, False))
        self.assertIsNone(run.refuse_root(0, True))
        self.assertIsNone(run.refuse_root(1000, False))


class RegistryTests(TreeCase):
    def test_every_candidate_name_and_directory_is_found(self):
        for name in ('a-test.py', 'b-integration.py', 'c-test.sh', 'd-integration.sh', 'e_test.py'):
            self.add(name, 'tier=unit')
        self.add('helper.py', None)  # not named like a test
        self.add('notes-test.txt', None)
        self.add('inner-test.py', 'tier=unit', directory=rel(TESTS, 'deeper'))
        self.add('data-test.py', None, directory=rel(TESTS, 'fixtures'))  # fixtures are data
        found = [run.posixpath.basename(path) for path in run.candidate_files(self.root, TESTS)]
        self.assertEqual(sorted(found), ['a-test.py', 'b-integration.py', 'c-test.sh', 'd-integration.sh',
                                         'e_test.py', 'inner-test.py'])

    def test_an_unregistered_file_fails_the_run_and_is_listed(self):
        self.add('good-test.py', 'tier=unit')
        self.add('forgotten-test.py', None)
        listing = self.runner('--list-unregistered')
        self.assertExit(listing, 1)
        self.assertIn(rel(TESTS, 'forgotten-test.py') + ': not registered', listing.stdout)
        self.assertNotIn('good-test.py', listing.stdout)
        result = self.runner('--tier', 'fast')
        self.assertExit(result, 1)
        self.assertIn('REGISTRY', result.stdout)
        self.assertIn('registry problem', result.stdout)
        self.assertIn('PASS       1 unit', result.stdout)  # the registered test still ran

    def test_a_clean_tree_lists_nothing(self):
        self.add('good-test.py', 'tier=unit')
        listing = self.runner('--list-unregistered')
        self.assertExit(listing, 0)
        self.assertEqual(listing.stdout, '')

    def test_a_missing_private_directory_is_a_problem_when_asked_for(self):
        self.add('good-test.py', 'tier=unit')
        result = self.runner('--list-unregistered', '--include-private')
        self.assertExit(result, 1)
        self.assertIn('the test directory does not exist', result.stdout)

    def test_the_development_directory_is_searched_only_on_request(self):
        self.add('good-test.py', 'tier=unit')
        private = rel(run.PRIVATE, 'testing')
        self.add('hidden-test.py', None, directory=private)
        self.assertExit(self.runner('--list-unregistered'), 0)
        listing = self.runner('--list-unregistered', '--include-private')
        self.assertExit(listing, 1)
        self.assertIn(rel(private, 'hidden-test.py'), listing.stdout)

    def test_a_reader_that_goes_away_does_not_cause_a_traceback(self):
        self.add('a-test.py', 'tier=unit')
        process = subprocess.Popen(self.command('--list'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        process.stdout.close()  # as `| head` does once it has its lines: the first line written finds no reader
        self.assertEqual(process.wait(timeout=60), 0)
        self.assertEqual(process.stderr.read(), '')
        process.stderr.close()

    def test_list_shows_tier_needs_timeout_and_the_reasons(self):
        self.add('a-test.py', 'tier=integration needs=cc timeout=77')
        self.add('b-test.py', 'tier=kernel-synthetic needs=root manual="Run it as root"')
        self.add('c-test.py', 'exclude="Not a test"')
        listing = self.runner('--list')
        self.assertExit(listing, 0)
        self.assertRegex(listing.stdout, r'2 integration\s+77s\s+serial\s+\S+a-test\.py\s+needs cc')
        self.assertIn('manual: Run it as root', listing.stdout)
        self.assertIn('excluded: Not a test', listing.stdout)


class OutcomeTests(TreeCase):
    def test_a_passing_and_a_failing_test(self):
        self.add('ok-test.py', 'tier=unit')
        self.add('bad-test.py', 'tier=unit', FAILING)
        result = self.runner('--tier', 'unit')
        self.assertExit(result, 1)
        self.assertRegex(result.stdout, r'PASS\s+1 unit\s+[\d.]+s\s+\S+ok-test\.py')
        self.assertRegex(result.stdout, r'FAIL\s+1 unit\s+[\d.]+s\s+\S+bad-test\.py\s+exit status 3')
        self.assertIn('| this test fails', result.stdout)
        self.assertIn('FAIL: 1 of 2 tests failed', result.stdout)

    def test_all_passing_is_a_pass_and_the_exit_status_is_zero(self):
        self.add('one-test.py', 'tier=unit')
        self.add('two-test.sh', 'tier=lint', 'exit 0\n')
        result = self.runner('--tier', 'fast')
        self.assertExit(result, 0)
        self.assertIn('PASS: 2 tests passed', result.stdout)
        self.assertIn('hardware acceptance remains separate', result.stdout)

    def test_a_signal_is_a_failure(self):
        self.add('killed-test.py', 'tier=unit', 'import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n')
        result = self.runner('--tier', 'unit')
        self.assertExit(result, 1)
        self.assertIn('killed by signal 9', result.stdout)

    def test_an_unmet_need_is_incomplete_and_fails_the_run(self):
        empty = self.base / 'empty-path'
        empty.mkdir()
        self.add('compile-test.py', 'tier=unit needs=cc', 'raise SystemExit(1)  # must never run\n')
        self.add('plain-test.py', 'tier=unit')
        result = self.runner('--tier', 'unit', env={'PATH': str(empty)})
        self.assertExit(result, 1)
        self.assertRegex(result.stdout, r'INCOMPLETE\s+1 unit\s+[\d.]+s\s+\S+compile-test\.py\s+not run, needs not met: '
                                        r'cc \(not found: cc\)')
        self.assertRegex(result.stdout, r'PASS\s+1 unit\s+[\d.]+s\s+\S+plain-test\.py')
        self.assertIn('INCOMPLETE: available checks passed, but required checks were skipped', result.stdout)
        self.assertNotIn('FAIL:', result.stdout)

    def test_selecting_no_tests_is_incomplete_not_a_pass(self):
        self.add('one-test.py', 'tier=unit')
        result = self.runner('--tier', 'human')
        self.assertExit(result, 1)
        self.assertIn('no test was selected', result.stdout)

    def test_skips_make_the_run_incomplete_unless_the_test_accepts_them(self):
        self.add('line-test.py', 'tier=unit', 'print("SKIP: no sound card")\n')
        self.add('framework-test.py', 'tier=unit', textwrap.dedent('''\
            import unittest
            class Cases(unittest.TestCase):
                @unittest.skip("not here")
                def test_one(self): pass
                def test_two(self): pass
            unittest.main()
            '''))
        self.add('accepted-test.py', 'tier=unit skips-ok="the sound card is optional"', 'print("SKIP: no sound card")\n')
        result = self.runner('--tier', 'unit')
        self.assertExit(result, 1)
        self.assertRegex(result.stdout, r'INCOMPLETE\s+1 unit\s+[\d.]+s\s+\S+line-test\.py\s+skipped 1 check')
        self.assertRegex(result.stdout, r'INCOMPLETE\s+1 unit\s+[\d.]+s\s+\S+framework-test\.py\s+skipped 1 check')
        self.assertRegex(result.stdout, r'PASS\s+1 unit\s+[\d.]+s\s+\S+accepted-test\.py\s+1 skipped, accepted: '
                                        r'the sound card is optional')

    def test_fail_fast_stops_after_the_first_failing_tier(self):
        marker = self.base / 'second-tier-ran'
        self.add('first-test.py', 'tier=unit', FAILING)
        self.add('second-test.py', 'tier=integration', f'open({str(marker)!r}, "w").close()\n')
        result = self.runner('--tier', 'unit,integration', '--fail-fast')
        self.assertExit(result, 1)
        self.assertFalse(marker.exists())
        self.assertIn('stopping after the first failing tier', result.stdout)
        result = self.runner('--tier', 'unit,integration')
        self.assertExit(result, 1)
        self.assertTrue(marker.exists(), 'without --fail-fast every selected tier runs')

    def test_tests_can_be_named_and_unknown_names_are_an_error(self):
        marker = self.base / 'named-ran'
        self.add('named-test.py', 'tier=integration', f'open({str(marker)!r}, "w").close()\n')
        self.add('other-test.py', 'tier=unit', FAILING)
        for name in ('named-test', rel(TESTS, 'named-test.py'), rel(TESTS, 'named-test'), 'named-*', 'named-test.py'):
            marker.unlink(missing_ok=True)
            with self.subTest(name=name):
                self.assertExit(self.runner(name), 0)  # naming a test considers every tier
                self.assertTrue(marker.exists())
        result = self.runner('no-such-test')
        self.assertExit(result, 2)
        self.assertIn("no test matches 'no-such-test'", result.stderr)

    def test_the_jobs_option_and_a_serial_declaration(self):
        for name in ('a', 'b'):
            body = textwrap.dedent(f'''\
                import os, pathlib, time
                mark = pathlib.Path({str(self.base)!r}) / ("running-{name}")
                mark.touch()
                time.sleep(0.4)
                overlapped = [p for p in mark.parent.glob("running-*") if p != mark]
                mark.unlink()
                raise SystemExit(1 if overlapped else 0)
                ''')
            self.add(f'{name}-test.py', 'tier=unit serial', body)
        self.assertExit(self.runner('--tier', 'unit', '--jobs', '4'), 0)


class ParallelTests(TreeCase):
    def test_the_fast_tiers_run_tests_at_the_same_time(self):
        """Each test waits for the other one's marker, so both pass only if they ran together."""
        for mine, theirs in (('a', 'b'), ('b', 'a')):
            body = textwrap.dedent(f'''\
                import pathlib, sys, time
                here = pathlib.Path({str(self.base)!r})
                (here / "ready-{mine}").touch()
                end = time.monotonic() + 25
                while time.monotonic() < end:
                    if (here / "ready-{theirs}").exists():
                        raise SystemExit(0)
                    time.sleep(0.02)
                raise SystemExit("the other test never started")
                ''')
            self.add(f'{mine}-test.py', 'tier=unit timeout=40', body)
        self.assertExit(self.runner('--tier', 'unit', '--jobs', '4'), 0)

    def test_higher_tiers_run_one_test_at_a_time(self):
        for name in ('a', 'b'):
            body = textwrap.dedent(f'''\
                import pathlib, time
                mark = pathlib.Path({str(self.base)!r}) / "running-{name}"
                mark.touch()
                time.sleep(0.4)
                overlapped = [p for p in mark.parent.glob("running-*") if p != mark]
                mark.unlink()
                raise SystemExit(1 if overlapped else 0)
                ''')
            self.add(f'{name}-integration.py', 'tier=integration', body)
        self.assertExit(self.runner('--tier', 'integration', '--jobs', '4'), 0)


class SelectionTests(TreeCase):
    def test_manual_tests_are_not_part_of_a_tier_and_need_a_request_by_name(self):
        marker = self.base / 'manual-ran'
        self.add('hand-test.py', 'tier=unit manual="Run it by hand"', f'open({str(marker)!r}, "w").close()\n')
        self.add('auto-test.py', 'tier=unit')
        result = self.runner('--tier', 'unit')
        self.assertExit(result, 0)
        self.assertFalse(marker.exists())
        self.assertIn('1 manual test(s) not run', result.stdout)
        self.assertNotRegex(result.stdout, r'(PASS|FAIL|INCOMPLETE).*hand-test')
        result = self.runner('--include-manual')
        self.assertExit(result, 2)
        self.assertIn('needs the names of the manual tests', result.stderr)
        result = self.runner('hand-test')
        self.assertExit(result, 2)
        self.assertIn('is manual (Run it by hand)', result.stderr)
        self.assertFalse(marker.exists())
        self.assertExit(self.runner('--include-manual', 'hand-test'), 0)
        self.assertTrue(marker.exists())

    def test_a_test_that_needs_root_is_never_run_by_a_user_who_is_not_root(self):
        marker = self.base / 'root-ran'
        self.add('rooted-test.py', 'tier=kernel-synthetic needs=root manual="Run it as root"',
                 f'open({str(marker)!r}, "w").close()\n')
        self.add('plain-test.py', 'tier=unit')
        self.assertExit(self.runner('--tier', 'all'), 0)  # not selected by a tier
        self.assertFalse(marker.exists())
        result = self.runner('--include-manual', 'rooted-test')
        self.assertExit(result, 1)
        self.assertIn('INCOMPLETE', result.stdout)
        self.assertIn('needs not met: root', result.stdout)
        self.assertFalse(marker.exists(), 'the test ran without root')

    def test_an_excluded_file_is_never_run_not_even_by_name(self):
        marker = self.base / 'excluded-ran'
        self.add('installer-test.py', 'exclude="An installer, not a test"', f'open({str(marker)!r}, "w").close()\n')
        self.add('real-test.py', 'tier=unit')
        self.assertExit(self.runner('--tier', 'all'), 0)
        result = self.runner('--include-manual', 'installer-test')
        self.assertExit(result, 2)
        self.assertIn('is excluded and is never run: An installer, not a test', result.stderr)
        result = self.runner('installer-test')
        self.assertExit(result, 2)
        self.assertFalse(marker.exists())

    def test_the_publication_manifest_limits_a_public_run(self):
        self.add('public-test.py', 'tier=unit')
        self.add('private-only-test.py', 'tier=unit', FAILING)
        self.write('.publication-files', rel(TESTS, 'public-test.py') + '\n')
        self.assertExit(self.runner('--tier', 'unit', '--public'), 0)
        self.assertExit(self.runner('--tier', 'unit'), 1)
        (self.root / '.publication-files').unlink()
        result = self.runner('--tier', 'unit', '--public')
        self.assertExit(result, 2)
        self.assertIn('publication manifest', result.stderr)

    def test_a_public_run_does_not_see_the_development_directory(self):
        self.add('needs-test.py', 'tier=unit needs=private-tree')
        self.write('.publication-files', rel(TESTS, 'needs-test.py') + '\n')
        (self.root / run.PRIVATE).mkdir()
        self.assertExit(self.runner('--tier', 'unit'), 0)  # a development checkout meets the need
        result = self.runner('--tier', 'unit', '--public')  # the exported tree would not
        self.assertExit(result, 1)
        self.assertIn('INCOMPLETE', result.stdout)
        self.assertIn('private-tree (a --public run does not use', result.stdout)

    def test_the_visual_tier_means_the_tests_that_read_the_capture_card(self):
        self.add('card-test.py', 'tier=hardware-synthetic needs=capture manual="Needs the capture card"')
        self.add('deck-test.py', 'tier=hardware-synthetic manual="Needs a Deck"')
        self.add('unit-test.py', 'tier=unit')
        declarations, problems = run.load_registry(self.root, False)
        self.assertEqual(problems, [])
        def chosen(spec):
            cases, left_out, _ = run.select_cases(declarations, run.parse_tiers(spec), [], True, None)
            return sorted(Path(case.path).name for case in cases + left_out)
        self.assertEqual(chosen('visual'), ['card-test.py'])
        self.assertEqual(chosen('hardware-synthetic'), ['card-test.py', 'deck-test.py'])
        self.assertEqual(chosen('fast'), ['unit-test.py'])
        self.assertEqual(chosen('visual,fast'), ['card-test.py', 'unit-test.py'])

    def test_quiet_and_verbose_output(self):
        self.add('talk-test.py', 'tier=unit', 'print("the passing test speaks")\n')
        self.assertNotIn('the passing test speaks', self.runner('--tier', 'unit').stdout)
        self.assertIn('| the passing test speaks', self.runner('--tier', 'unit', '--verbose').stdout)
        quiet = self.runner('--tier', 'unit', '--quiet')
        self.assertExit(quiet, 0)
        self.assertEqual(quiet.stdout.strip().splitlines()[0][:5], 'PASS:')


class EnvironmentTests(TreeCase):
    def test_a_test_gets_a_clean_isolated_environment(self):
        record = self.base / 'environment.json'
        body = textwrap.dedent(f'''\
            import json, os, sys
            tmp = os.environ.get("TMPDIR")
            json.dump({{
                "dosplex": sorted(k for k in os.environ if k.startswith("DOSPLEX_")),
                "git": sorted(k for k in os.environ if k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")),
                "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
                "tmp": tmp, "tmp_files": os.listdir(tmp), "cwd": os.getcwd(), "cwd_files": os.listdir("."),
                "stdin": sys.stdin.read(), "sid": os.getsid(0) == os.getpid(), "pgid": os.getpgrp() == os.getpid(),
            }}, open({str(record)!r}, "w"))
            ''')
        self.add('env-test.py', 'tier=unit', body)
        result = self.runner('--tier', 'unit', env={'DOSPLEX_BUILD_DIR': '/nowhere', 'DOSPLEX_STATE_PATH': '/nowhere',
                                                    'GIT_DIR': '/nowhere', 'GIT_INDEX_FILE': '/nowhere'})
        self.assertExit(result, 0)
        seen = json.loads(record.read_text())
        self.assertEqual((seen['dosplex'], seen['git'], seen['bytecode']), ([], [], '1'))
        self.assertEqual((seen['tmp_files'], seen['cwd_files'], seen['stdin']), ([], [], ''))
        self.assertTrue(seen['sid'] and seen['pgid'], 'a test runs in a process group and session of its own')
        self.assertNotEqual(Path(seen['cwd']), self.root)
        self.assertFalse(Path(seen['tmp']).exists(), 'the private TMPDIR is removed afterwards')

    def test_every_test_gets_its_own_temporary_directory(self):
        seen = self.base / 'tmp-dirs'
        seen.mkdir()
        for name in ('a', 'b', 'c'):
            body = f'import os\nopen(os.path.join({str(seen)!r}, "{name}"), "w").write(os.environ["TMPDIR"])\n'
            self.add(f'{name}-test.py', 'tier=unit', body)
        self.assertExit(self.runner('--tier', 'unit', '--jobs', '3'), 0)
        directories = {path.read_text() for path in seen.iterdir()}
        self.assertEqual(len(directories), 3)


class CompositorTests(TreeCase):
    """The compositor is built once for every test that needs it. A stand-in for meson keeps this fast."""

    def setUp(self):
        super().setUp()
        self.tools = self.base / 'tools'
        self.tools.mkdir()
        self.calls = self.base / 'meson-calls'
        self.script('meson', textwrap.dedent(f'''\
            #!/bin/sh
            if [ "$1" = --version ]; then echo 1.0.0; exit 0; fi
            echo "$*" >> {str(self.calls)!r}
            if [ "$1" = setup ]; then
              shift
              if [ "$1" = --reconfigure ]; then shift; fi
              mkdir -p "$1"
              exit 0
            fi
            if [ "$1" = compile ]; then
              if [ -n "$FAKE_MESON_FAILS" ]; then echo "compiler said no" >&2; exit 1; fi
              printf '#!/bin/sh\\necho stand-in compositor\\n' > "$3/dosplex-compositor"
              chmod +x "$3/dosplex-compositor"
              exit 0
            fi
            exit 2
            '''))
        self.script('pkg-config', '#!/bin/sh\nexit 0\n')
        self.script('wayland-scanner', '#!/bin/sh\nexit 0\n')
        self.env = {'PATH': f'{self.tools}{os.pathsep}{os.environ["PATH"]}'}
        self.output = self.base / 'compositor-output'
        self.output.mkdir()

    def script(self, name, text):
        path = self.tools / name
        path.write_text(text)
        path.chmod(0o755)

    def add_user(self, name):
        body = (f'import subprocess, sys\n'
                f'open(sys.argv[2], "wb").write(subprocess.check_output([sys.argv[1]]))\n')
        self.add(f'{name}-integration.py', 'tier=integration needs=compositor', body,
                 runs=[f'default {{compositor}} {self.output / name}'])

    def test_it_is_built_once_and_passed_to_every_test_that_needs_it(self):
        self.add_user('one')
        self.add_user('two')
        result = self.runner('--tier', 'integration', env=self.env)
        self.assertExit(result, 0)
        self.assertEqual((self.output / 'one').read_text(), 'stand-in compositor\n')
        self.assertEqual((self.output / 'two').read_text(), 'stand-in compositor\n')
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 2, calls)  # one setup and one compile for both tests
        self.assertTrue(calls[0].startswith('setup ') and calls[1].startswith('compile -C '))
        self.assertIn('compositor built in', result.stdout)

    def test_nothing_is_built_when_no_selected_test_needs_it(self):
        self.add_user('one')
        self.add('plain-test.py', 'tier=unit')
        self.assertExit(self.runner('--tier', 'unit', env=self.env), 0)
        self.assertFalse(self.calls.exists())

    def test_a_failed_build_fails_the_run_and_leaves_its_dependents_incomplete(self):
        self.add_user('one')
        result = self.runner('--tier', 'integration', env=dict(self.env, FAKE_MESON_FAILS='1'))
        self.assertExit(result, 1)
        self.assertIn('the compositor build failed (compile)', result.stdout)
        self.assertIn('compiler said no', result.stdout)
        self.assertRegex(result.stdout, r'INCOMPLETE\s+2 integration')
        self.assertFalse((self.output / 'one').exists())
        self.assertIn('FAIL:', result.stdout)

    def test_missing_build_tools_leave_the_dependents_incomplete(self):
        self.add_user('one')
        empty = self.base / 'empty-path'
        empty.mkdir()
        result = self.runner('--tier', 'integration', env={'PATH': str(empty)})
        self.assertExit(result, 1)
        self.assertIn('not found: meson, wayland-scanner, pkg-config', result.stdout)
        self.assertIn('INCOMPLETE', result.stdout)

    def test_the_build_tools_are_a_need_of_their_own_and_checking_for_them_builds_nothing(self):
        self.add('tools-test.py', 'tier=unit needs=compositor-build')
        self.assertExit(self.runner('--tier', 'unit', env=self.env), 0)
        self.assertFalse(self.calls.exists(), 'looking for the tools must not start a build')
        empty = self.base / 'empty-path'
        empty.mkdir()
        result = self.runner('--tier', 'unit', env={'PATH': str(empty)})
        self.assertExit(result, 1)
        self.assertIn('compositor-build (not found: meson, wayland-scanner, pkg-config)', result.stdout)

    def test_a_compositor_that_was_supplied_is_used_without_a_build(self):
        self.add_user('one')
        supplied = self.base / 'supplied-compositor'
        supplied.write_text('#!/bin/sh\necho supplied compositor\n')
        supplied.chmod(0o755)
        self.assertExit(self.runner('--tier', 'integration', '--compositor', str(supplied), env=self.env), 0)
        self.assertEqual((self.output / 'one').read_text(), 'supplied compositor\n')
        self.assertFalse(self.calls.exists())
        result = self.runner('--tier', 'integration', '--compositor', str(self.base / 'missing'), env=self.env)
        self.assertExit(result, 1)
        self.assertIn('is not an executable file', result.stdout)

    def test_a_build_directory_is_kept_and_reconfigured(self):
        self.add_user('one')
        kept = self.base / 'kept'
        self.assertExit(self.runner('--tier', 'integration', '--build-dir', str(kept), env=self.env), 0)
        self.assertTrue((kept / 'dosplex-compositor').is_file())
        (kept / 'meson-private').mkdir()
        (kept / 'meson-private' / 'coredata.dat').write_text('')
        self.assertExit(self.runner('--tier', 'integration', '--build-dir', str(kept), env=self.env), 0)
        self.assertIn('setup --reconfigure', self.calls.read_text())


class DocumentationTests(unittest.TestCase):
    """The help text and the testing guide name every tier and every need that the runner knows."""

    def test_help_documents_tiers_needs_and_statuses(self):
        text = subprocess.run([sys.executable, str(RUNNER), '--help'], capture_output=True, text=True, timeout=60).stdout
        for name in run.TIER_NAMES.values():
            self.assertIn(name, text)
        for name, what, _ in run.NEED_TABLE:
            self.assertIn(name, text)
            self.assertIn(what, text)
        for word in ('PASS', 'FAIL', 'TIMEOUT', 'INCOMPLETE', 'REGISTRY', 'fast', 'visual', 'SKIP:', 'manual="why"',
                     'exclude="why"', 'skips-ok="why"', '{compositor}', 'unrelated to the T1/T2 support tiers'):
            self.assertIn(word, text)

    def test_the_testing_guide_names_every_tier(self):
        guide = (ROOT / 'docs' / 'testing.md').read_text()
        for number, name in run.TIER_NAMES.items():
            self.assertIn(f'| {number} | {name} |', guide)
        for word in ('--tier', '--list-unregistered', '--include-manual', 'INCOMPLETE', 'dosplex-test', 'skips-ok'):
            self.assertIn(word, guide)


if __name__ == '__main__':
    unittest.main()
