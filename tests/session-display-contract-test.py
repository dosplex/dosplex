#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A side never restarts for a display change under contract version 2 (ADR-012).

Runs the real supervise() loop with fake sessions and no systemd. With version-2 state, region, game
monitor, scaler, revision and layout changes keep the very same session; with version-1 state every
committed change restarts it as before. A mode change and a restart request restart the side under
both versions, Off stops it and a service that exits is started again. The supervisor serves the
contract it last read to its owner and keeps the follower's report in session.json. With a Steam Controller
routed to the side and Steam's controller tour on for one of the owner's accounts, it records the accounts in
session.json and one journal line, and writes nothing of Steam's.
"""
import contextlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

SLEEP = time.sleep
BASE = dict(role='primary', x=0, y=0, width=960, height=1080, game_width=960, game_height=1080,
            scale_mode='fit', display_revision=1, activity='active')
BASE_LIVE = {key: BASE[key] for key in ('x', 'y', 'width', 'height', 'game_width', 'game_height',
                                        'scale_mode', 'display_revision')} | {'layout': 'vertical'}
LAUNCH_FIELDS = {'display_applied_revision': 1, 'display_applied_size': [960, 1080],
                 'display_result': 'applied', 'display_detail': ''}
LAUNCH_ENVIRONMENT = {'width': 'DOSPLEX_OUTPUT_WIDTH', 'height': 'DOSPLEX_OUTPUT_HEIGHT',
                      'game_width': 'DOSPLEX_GAME_WIDTH', 'game_height': 'DOSPLEX_GAME_HEIGHT',
                      'scale_mode': 'DOSPLEX_SCALE_MODE'}
DISPLAY_KEYS = ('display_applied_revision', 'display_applied_size', 'display_result', 'display_detail')
serial = itertools.count()


class FakeSession:
    """Stands in for the owned systemd service: running until it is stopped or exits."""
    def __init__(self, side, owner, command, environment):
        self.environment = dict(environment)
        self.unit = f'fake-{next(serial)}.service'
        self.started = self.frozen = self.exited = False
        self.stops = 0

    def start(self):
        self.started = True

    def is_running(self):
        return self.started and not self.exited

    def stop(self):
        self.stops += 1
        self.started = False

    def set_standby(self, enabled):
        self.frozen = enabled


class Supervised:
    """One supervise() loop for the left side, polling every few milliseconds."""
    def __init__(self, test, version=2, owner_uid=None, **entry):
        self.test, self.version, self.layout = test, version, 'vertical'
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self.entry = dict(BASE, **entry)
        self.sessions, self.errors, self.cached_instance = [], [], None
        self.stop_event = threading.Event()
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-display-contract-')
        self.root = Path(self.temp.name)
        self.directory, self.state_path = self.root / 'left', self.root / 'state.json'
        self.host_path = self.root / 'host.sock'
        self.host = socket.socket(socket.AF_UNIX)
        self.host.bind(str(self.host_path))
        self.write()

    def __enter__(self):
        user = SimpleNamespace(pw_uid=self.owner_uid, pw_name='tester', pw_dir=str(self.root), pw_shell='/bin/sh')
        self.patches = [patch.object(m.pwd, 'getpwnam', return_value=user),
                        patch.object(m.time, 'sleep', lambda seconds: SLEEP(min(seconds, .005)))]
        for patcher in self.patches:
            patcher.start()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
        try:
            self.wait(lambda: self.sessions and self.snapshot().get('lifecycle') == 'running', 'the first session')
        except BaseException:
            self.shutdown()
            raise
        return self

    def __exit__(self, *failure):
        self.shutdown()
        if failure[0] is None:
            self.test.assertFalse(self.thread.is_alive(), 'the supervisor did not stop')
            self.test.assertEqual(self.errors, [])

    def shutdown(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        for patcher in reversed(self.patches):
            patcher.stop()
        self.host.close()
        self.temp.cleanup()

    def run(self):
        try:
            m.supervise('left', 'tester', self.directory, self.state_path, self.host_path, self.root,
                        self.stop_event, session_factory=self.factory)
        except BaseException as error:
            self.errors.append(error)

    def factory(self, side, owner, command, environment):
        self.sessions.append(FakeSession(side, owner, command, environment))
        return self.sessions[-1]

    def write(self, layout=None, **changes):
        """Publish new state.json, as the compositor does."""
        self.layout = layout or self.layout
        self.entry.update(changes)
        state = {'layout': self.layout, 'left': dict(self.entry), 'right': dict(self.entry, x=960)}
        if self.version is not None:
            state['contract_version'] = self.version
        m.atomic_json(self.state_path, state)

    def wait(self, predicate, label, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError('timed out waiting for ' + label)
            SLEEP(.002)

    def started(self, count):
        """Whether the count-th session exists and is running."""
        return len(self.sessions) == count and self.sessions[-1].started

    def snapshot(self):
        try:
            return json.loads((self.directory / 'session.json').read_text())
        except (OSError, ValueError):
            return {}

    @property
    def endpoint(self):
        return str(self.directory / 'control.sock')

    @property
    def instance(self):
        self.cached_instance = self.cached_instance or self.snapshot()['instance']
        return self.cached_instance

    def live(self):
        return m.client_display_get(self.endpoint, self.instance, server_uid=os.getuid())

    def read_back(self, **expected):
        """Wait until the supervisor has read the new state: it serves display get from what it last read."""
        def reported():
            try:
                live = self.live()
            except (OSError, RuntimeError):
                return False
            return all(live[key] == value for key, value in expected.items())
        self.wait(reported, f'the supervisor to read {expected}')

    def settle(self, rounds=25):
        """Let the loop run on. Every display get is served by one iteration of the loop, so a restart
        that was going to follow (a few iterations of stopping and starting) has happened by now."""
        for _ in range(rounds):
            self.live()


class DisplayContract(unittest.TestCase):
    def setUp(self):
        if os.getuid() < 1000:
            self.skipTest('supervise() only runs sessions for an ordinary user')

    def test_version_2_display_changes_never_restart_the_side(self):
        with Supervised(self, version=2) as run:
            first = run.sessions[0]
            self.assertEqual(run.live(), BASE_LIVE)
            for change in (
                    # Full screen: the region and Native follow the output.
                    dict(layout='single', width=1920, height=1080, game_width=1920, game_height=1080),
                    # Top and bottom, then the Main position swap.
                    dict(layout='horizontal', x=0, y=540, width=1920, height=540, game_width=1920, game_height=540),
                    dict(y=0),
                    dict(layout='vertical', x=960, y=0, width=960, height=1080, game_width=960, game_height=1080),
                    dict(scale_mode='stretch'),
                    dict(scale_mode='fit', display_revision=2),
                    # Apply of a picture choice, and a host output change.
                    dict(game_width=1280, game_height=720, display_revision=3),
                    dict(width=1280, height=720, game_width=1280, game_height=720, display_revision=4)):
                with self.subTest(change=change):
                    run.write(**change)
                    run.read_back(**change)
                    run.settle()
                    self.assertEqual(run.sessions, [first])
                    self.assertEqual(first.stops, 0)
                    self.assertTrue(first.is_running())
            snapshot = run.snapshot()
            self.assertEqual((snapshot['lifecycle'], snapshot['restart_count'], snapshot['unit']),
                             ('running', 0, first.unit))

    def test_version_1_display_changes_restart_the_side_as_before(self):
        for declared in (None, 1):
            for field, value in dict(width=1024, height=768, game_width=1280, game_height=720,
                                     scale_mode='stretch', display_revision=2).items():
                with self.subTest(contract_version=declared, field=field), Supervised(self, version=declared) as run:
                    run.write(**{field: value})
                    run.wait(lambda: run.started(2), 'the restart')
                    first, second = run.sessions
                    self.assertEqual(first.stops, 1)
                    if field in LAUNCH_ENVIRONMENT:
                        self.assertEqual(second.environment[LAUNCH_ENVIRONMENT[field]], str(value))
                    else:  # The revision is not launch argv; the new session starts at it.
                        run.wait(lambda: run.snapshot().get('display_applied_revision') == value, 'the new revision')
                    self.assertEqual(run.snapshot()['restart_count'], 1)
        # Layout and position never restarted a side, and still do not.
        for change in (dict(layout='horizontal'), dict(x=480), dict(y=270), dict(layout='single', x=0)):
            with self.subTest(unchanged=change), Supervised(self, version=None) as run:
                run.write(**change)
                run.read_back(**change)
                run.settle()
                self.assertEqual(len(run.sessions), 1)
                self.assertEqual(run.sessions[0].stops, 0)

    def test_mode_change_and_restart_request_restart_under_both_versions(self):
        for version in (None, 2):
            with self.subTest(version=version, request='mode'), Supervised(self, version=version) as run:
                m.client_mode('desktop', run.endpoint, run.instance, server_uid=os.getuid())
                run.wait(lambda: run.started(2), 'the restart for a mode change')
                first, second = run.sessions
                self.assertEqual(first.stops, 1)
                self.assertEqual(first.environment['DOSPLEX_SESSION_PAYLOAD'], 'steam')
                self.assertEqual(second.environment['DOSPLEX_SESSION_PAYLOAD'], 'desktop')
            with self.subTest(version=version, request='restart token'), Supervised(self, version=version) as run:
                m.request_change('left', 'restart', side_root=run.root)
                run.wait(lambda: run.started(2), 'the restart for a restart request')
                first, second = run.sessions
                self.assertEqual(first.stops, 1)
                self.assertTrue(second.is_running())
                run.settle()
                self.assertEqual(len(run.sessions), 2)  # One request, one restart.

    def test_a_restart_for_another_reason_starts_from_the_contract_read_at_that_moment(self):
        with Supervised(self, version=2) as run:
            run.write(layout='single', width=1920, height=1080, game_width=1280, game_height=720,
                      scale_mode='stretch', display_revision=7)
            run.read_back(display_revision=7)
            run.settle()
            self.assertEqual(len(run.sessions), 1)
            m.client_mode('desktop', run.endpoint, run.instance, server_uid=os.getuid())
            run.wait(lambda: run.started(2), 'the restart for a mode change')
            second = run.sessions[1]
            self.assertEqual({name: second.environment[name] for name in LAUNCH_ENVIRONMENT.values()},
                             {'DOSPLEX_OUTPUT_WIDTH': '1920', 'DOSPLEX_OUTPUT_HEIGHT': '1080',
                              'DOSPLEX_GAME_WIDTH': '1280', 'DOSPLEX_GAME_HEIGHT': '720',
                              'DOSPLEX_SCALE_MODE': 'stretch'})
            # What the new session was launched with counts as applied.
            run.wait(lambda: run.snapshot().get('display_applied_revision') == 7, 'the new launch contract')
            snapshot = run.snapshot()
            self.assertEqual((snapshot['display_applied_size'], snapshot['display_result'],
                              snapshot['display_detail']), ([1280, 720], 'applied', ''))

    def test_a_desktop_profile_root_given_to_the_supervisor_reaches_the_launcher_of_every_session(self):
        root = '/run/dosplex-trial/desktop'
        with patch.dict(os.environ, {'DOSPLEX_DESKTOP_CONFIG_ROOT': root}):
            self.assertEqual(m.desktop_environment(), {'DOSPLEX_DESKTOP_CONFIG_ROOT': root})
            with Supervised(self) as run:
                m.client_mode('desktop', run.endpoint, run.instance, server_uid=os.getuid())
                run.wait(lambda: run.started(2), 'the desktop session')
                self.assertEqual([session.environment['DOSPLEX_DESKTOP_CONFIG_ROOT'] for session in run.sessions],
                                 [root, root])
        for given in (None, ''):
            with patch.dict(os.environ):
                os.environ.pop('DOSPLEX_DESKTOP_CONFIG_ROOT', None)
                if given is not None:
                    os.environ['DOSPLEX_DESKTOP_CONFIG_ROOT'] = given
                self.assertEqual(m.desktop_environment(), {})
                with Supervised(self) as run:
                    self.assertNotIn('DOSPLEX_DESKTOP_CONFIG_ROOT', run.sessions[0].environment)

    def test_off_and_a_service_that_exits_stop_or_restart_the_side_under_version_2(self):
        with Supervised(self, version=2) as run:
            first = run.sessions[0]
            run.write(activity='off')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'off', 'Off')
            self.assertEqual((len(run.sessions), first.stops), (1, 1))
            self.assertFalse(any(key in run.snapshot() for key in DISPLAY_KEYS))
            run.write(width=1280, height=720, display_revision=2)  # Off still reads the state.
            run.read_back(width=1280, height=720, display_revision=2)
            self.assertEqual(len(run.sessions), 1)
            run.write(activity='active')
            run.wait(lambda: run.started(2) and run.snapshot().get('lifecycle') == 'running', 'On again')
            second = run.sessions[1]
            second.exited = True  # The service ends by itself.
            run.wait(lambda: run.started(3) and run.snapshot().get('lifecycle') == 'running', 'a new service')
            self.assertEqual(second.stops, 1)
            self.assertEqual(run.snapshot()['restart_count'], 2)

    def test_a_guest_side_started_in_full_screen_reports_sleeping_and_starts_nothing_until_the_split(self):
        # A boot start logs Guest's side in while the layout is Full screen: its session.json must appear, the
        # side must sleep with no service (so no Steam), and a split must start it as it does after Full screen.
        class GuestRun(Supervised):
            def __init__(self, test):
                super().__init__(test, version=2, role='guest')
                self.directory = self.root / 'right'
                self.write(layout='single')

            def run(self):
                try:
                    m.supervise('right', 'tester', self.directory, self.state_path, self.host_path, self.root,
                                self.stop_event, session_factory=self.factory, role='guest')
                except BaseException as error:
                    self.errors.append(error)

            def __enter__(self):
                user = SimpleNamespace(pw_uid=self.owner_uid, pw_name='tester', pw_dir=str(self.root),
                                       pw_shell='/bin/sh')
                self.patches = [patch.object(m.pwd, 'getpwnam', return_value=user),
                                patch.object(m.time, 'sleep', lambda seconds: SLEEP(min(seconds, .005)))]
                for patcher in self.patches:
                    patcher.start()
                self.thread = threading.Thread(target=self.run)
                self.thread.start()
                try:
                    self.wait(lambda: self.snapshot().get('lifecycle') == 'sleeping', 'the sleeping side')
                except BaseException:
                    self.shutdown()
                    raise
                return self

        with GuestRun(self) as run:
            SLEEP(.3)  # A side that was going to start a service would have done so by now.
            snapshot = run.snapshot()
            self.assertEqual((snapshot['side'], snapshot['lifecycle'], snapshot['active']), ('right', 'sleeping', False))
            self.assertEqual(run.sessions, [])
            run.write(layout='vertical', display_revision=2)
            run.wait(lambda: run.started(1) and run.snapshot().get('lifecycle') == 'running', 'the split wakes Guest')

    def test_standby_and_resuming_do_not_restart_the_side_under_version_2(self):
        with Supervised(self, version=2) as run:
            first = run.sessions[0]
            run.write(activity='standby', layout='single', width=1920, height=1080, display_revision=2)
            run.wait(lambda: run.snapshot().get('lifecycle') == 'standby', 'standby')
            self.assertTrue(first.frozen)
            run.write(activity='active', display_revision=3)
            run.wait(lambda: run.snapshot().get('lifecycle') == 'running' and not first.frozen, 'resume')
            run.settle()
            self.assertEqual((run.sessions, first.stops), ([first], 0))

    def test_display_get_serves_what_the_supervisor_read_and_only_to_its_owner(self):
        with Supervised(self, version=2) as run:
            self.assertEqual(run.live(), BASE_LIVE)
            run.write(layout='horizontal', x=0, y=540, width=1920, height=540, scale_mode='stretch', display_revision=2)
            run.read_back(display_revision=2)
            self.assertEqual(run.live(), {'x': 0, 'y': 540, 'width': 1920, 'height': 540, 'game_width': 960,
                                          'game_height': 1080, 'scale_mode': 'stretch', 'display_revision': 2,
                                          'layout': 'horizontal'})
            # State that cannot be read keeps the last contract and never costs the session.
            run.state_path.write_text('{')
            run.settle()
            self.assertEqual(run.live()['display_revision'], 2)
            self.assertEqual(len(run.sessions), 1)
        with Supervised(self, version=2, owner_uid=os.getuid() + 1) as run:
            with self.assertRaises((OSError, RuntimeError)):
                run.live()
            with self.assertRaises((OSError, RuntimeError)):
                m.client_display_report(run.endpoint, run.instance, 9, 1280, 720, 'failed', 'not mine',
                                        server_uid=os.getuid())
            self.assertEqual({key: run.snapshot()[key] for key in DISPLAY_KEYS}, LAUNCH_FIELDS)

    def test_display_report_lands_in_session_json_and_survives_the_loops_own_writes(self):
        with Supervised(self, version=2) as run:
            snapshot = run.snapshot()
            self.assertEqual({key: snapshot[key] for key in DISPLAY_KEYS}, LAUNCH_FIELDS)
            m.client_display_report(run.endpoint, run.instance, 2, 1280, 720, 'failed', 'xrandr timed out',
                                    server_uid=os.getuid())
            reported = {'display_applied_revision': 2, 'display_applied_size': [1280, 720],
                        'display_result': 'failed', 'display_detail': 'xrandr timed out'}
            self.assertEqual({key: run.snapshot()[key] for key in DISPLAY_KEYS}, reported)
            # The loop rewrites session.json for standby and for resuming; the report stays.
            run.write(activity='standby')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'standby', 'standby')
            self.assertEqual({key: run.snapshot()[key] for key in DISPLAY_KEYS}, reported)
            run.write(activity='active')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'running', 'resume')
            self.assertEqual({key: run.snapshot()[key] for key in DISPLAY_KEYS}, reported)
            # A new session replaces it with its own launch contract.
            run.write(display_revision=5)
            m.client_mode('desktop', run.endpoint, run.instance, server_uid=os.getuid())
            run.wait(lambda: run.started(2) and run.snapshot().get('lifecycle') == 'running', 'the restart')
            self.assertEqual({key: run.snapshot()[key] for key in DISPLAY_KEYS},
                             {'display_applied_revision': 5, 'display_applied_size': [960, 1080],
                              'display_result': 'applied', 'display_detail': ''})
            # With no session there is nothing to report on, and nothing of the old report is left.
            run.write(activity='off')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'off', 'Off')
            self.assertFalse(any(key in run.snapshot() for key in DISPLAY_KEYS))
            before = (run.directory / 'session.json').read_bytes()
            with self.assertRaises(RuntimeError):
                m.client_display_report(run.endpoint, run.instance, 6, 1280, 720, 'applied', server_uid=os.getuid())
            self.assertEqual((run.directory / 'session.json').read_bytes(), before)

    def test_a_display_report_says_applied_failed_or_deferred_and_nothing_else(self):
        detail = 'game 892970 running; monitor stays 960x1080 (target 1280x720)'

        def report(result, **changes):
            return dict({'instance': 'a' * 32, 'display': 'report', 'revision': 4, 'game_width': 960,
                         'game_height': 1080, 'result': result, 'detail': detail}, **changes)
        self.assertEqual(m.DISPLAY_RESULTS, ('applied', 'failed', 'deferred'))
        for result in m.DISPLAY_RESULTS:
            with self.subTest(result=result):
                self.assertEqual(m.display_report_fields(report(result)),
                                 {'display_applied_revision': 4, 'display_applied_size': [960, 1080],
                                  'display_result': result, 'display_detail': detail})
                self.assertEqual(m.validate_control_request(report(result), 'a' * 32), 'report')
        for result in ('Deferred', 'DEFERRED', 'deferred ', ' deferred', 'defer', 'deferral', 'done', 'pending', '',
                       None, 1, True, ['deferred'], {'deferred': True}):
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    m.display_report_fields(report(result))
                with self.assertRaises(ValueError):
                    m.validate_control_request(report(result), 'a' * 32)
        # A deferred report has to be as well formed as any other: the size, the revision and the detail.
        for changes in (dict(game_width=0), dict(game_height=16385), dict(revision=-1), dict(revision=2 ** 32),
                        dict(detail='x' * 201), dict(detail='two\nlines'), dict(detail=None), dict(extra=1)):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    m.display_report_fields(report('deferred', **changes))
        self.assertEqual(m.display_report_fields(report('deferred', detail='x' * 200))['display_detail'], 'x' * 200)

    def test_a_deferred_report_is_kept_and_a_later_report_for_the_same_revision_replaces_it(self):
        def stored(run):
            return {key: run.snapshot()[key] for key in DISPLAY_KEYS}
        detail = 'game 892970 running; monitor stays 960x1080 (target 1280x720)'
        with Supervised(self, version=2) as run:
            run.write(game_width=1280, game_height=720, display_revision=2)
            run.read_back(display_revision=2)
            m.client_display_report(run.endpoint, run.instance, 2, 960, 1080, 'deferred', detail,
                                    server_uid=os.getuid())
            deferred = {'display_applied_revision': 2, 'display_applied_size': [960, 1080],
                        'display_result': 'deferred', 'display_detail': detail}
            self.assertEqual(stored(run), deferred)
            # The loop rewrites session.json for standby and for resuming; the deferred report stays.
            run.write(activity='standby')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'standby', 'standby')
            self.assertEqual(stored(run), deferred)
            run.write(activity='active')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'running', 'resume')
            self.assertEqual(stored(run), deferred)
            # Another game starts: the same revision is reported deferred again, and the new report is the one kept.
            again = 'games 262830, 892970 running; monitor stays 960x1080 (target 1280x720)'
            m.client_display_report(run.endpoint, run.instance, 2, 960, 1080, 'deferred', again,
                                    server_uid=os.getuid())
            self.assertEqual(stored(run), dict(deferred, display_detail=again))
            # The games have ended and the follower has applied the revision: applied replaces deferred.
            m.client_display_report(run.endpoint, run.instance, 2, 1280, 720, 'applied', '1280x720 fit',
                                    server_uid=os.getuid())
            applied = {'display_applied_revision': 2, 'display_applied_size': [1280, 720],
                       'display_result': 'applied', 'display_detail': '1280x720 fit'}
            self.assertEqual(stored(run), applied)
            # A later deferral of a newer revision replaces that in turn, and failed replaces a deferred one too.
            m.client_display_report(run.endpoint, run.instance, 3, 1280, 720, 'deferred', 'game 892970 running',
                                    server_uid=os.getuid())
            self.assertEqual((stored(run)['display_applied_revision'], stored(run)['display_result']),
                             (3, 'deferred'))
            m.client_display_report(run.endpoint, run.instance, 3, 1920, 1080, 'failed', 'xrandr timed out',
                                    server_uid=os.getuid())
            self.assertEqual(stored(run), {'display_applied_revision': 3, 'display_applied_size': [1920, 1080],
                                           'display_result': 'failed', 'display_detail': 'xrandr timed out'})

    def test_a_deferred_revision_restarts_nothing_before_or_after_it_is_applied(self):
        detail = 'game 892970 running; monitor stays 960x1080 (target 1920x1080)'
        with Supervised(self, version=2) as run:
            first = run.sessions[0]
            # The compositor commits a new game monitor for a layout change; a game runs, so the follower defers it.
            run.write(layout='single', width=1920, height=1080, game_width=1920, game_height=1080, display_revision=2)
            run.read_back(display_revision=2)
            m.client_display_report(run.endpoint, run.instance, 2, 960, 1080, 'deferred', detail,
                                    server_uid=os.getuid())
            run.settle()
            self.assertEqual((run.sessions, first.stops), ([first], 0))
            self.assertEqual(run.snapshot()['display_result'], 'deferred')
            # Further changes while it waits restart nothing, and the follower is still served the latest contract.
            run.write(game_width=1280, game_height=720, display_revision=3)
            run.read_back(display_revision=3, game_width=1280)
            run.settle()
            self.assertEqual((run.sessions, first.stops), ([first], 0))
            # The game ends, the follower applies the latest revision and reports it.
            m.client_display_report(run.endpoint, run.instance, 3, 1280, 720, 'applied', '1280x720 fit',
                                    server_uid=os.getuid())
            run.settle()
            self.assertEqual((run.sessions, first.stops), ([first], 0))
            snapshot = run.snapshot()
            self.assertEqual((snapshot['lifecycle'], snapshot['restart_count'], snapshot['unit']),
                             ('running', 0, first.unit))
            self.assertEqual((snapshot['display_applied_revision'], snapshot['display_result']), (3, 'applied'))
        # A state file of version 1 restarts the side for a committed change, as before; a report never does.
        for declared in (None, 1):
            with self.subTest(contract_version=declared), Supervised(self, version=declared) as run:
                first = run.sessions[0]
                for result, text in (('deferred', detail), ('applied', '960x1080 fit')):
                    m.client_display_report(run.endpoint, run.instance, 1, 960, 1080, result, text,
                                            server_uid=os.getuid())
                    run.settle()
                self.assertEqual((run.sessions, first.stops), ([first], 0))

    def test_desktop_scale_survives_the_loops_writes_and_ends_with_its_session(self):
        with Supervised(self, version=2) as run:
            self.assertNotIn('desktop_scale_milli', run.snapshot())
            m.client_desktop_scale(run.endpoint, run.instance, '2', server_uid=os.getuid())
            self.assertEqual(run.snapshot()['desktop_scale_milli'], 2000)
            run.write(activity='standby')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'standby', 'standby')
            self.assertEqual(run.snapshot()['desktop_scale_milli'], 2000)
            run.write(activity='active')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'running', 'resume')
            self.assertEqual(run.snapshot()['desktop_scale_milli'], 2000)
            # A new session starts without it; its launcher reports again.
            m.client_mode('desktop', run.endpoint, run.instance, server_uid=os.getuid())
            run.wait(lambda: run.started(2) and run.snapshot().get('lifecycle') == 'running', 'the restart')
            self.assertNotIn('desktop_scale_milli', run.snapshot())
            run.write(activity='off')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'off', 'Off')
            with self.assertRaises(RuntimeError):
                m.client_desktop_scale(run.endpoint, run.instance, '2', server_uid=os.getuid())
            self.assertNotIn('desktop_scale_milli', run.snapshot())

    def test_the_wine_policy_follows_the_live_game_monitor_without_a_restart(self):
        calls = []
        def reconcile(owner, home, portrait):
            calls.append(portrait)
            return {'results': {'prefix': 'applied'}}
        # supervise() skips the registry policy unless it runs as root; stand in for both.
        with patch.object(m.wine_modelist, 'reconcile', reconcile), patch.object(m.os, 'geteuid', return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            with Supervised(self, version=2) as run:
                first = run.sessions[0]
                self.assertEqual(calls, [True])  # Launched with a portrait game monitor.
                run.write(game_width=1920, game_height=1080, display_revision=2)
                run.wait(lambda: calls[-1] is False and run.snapshot().get('wine_notice_seq', 0) >= 1,
                         'the landscape policy')
                run.write(game_width=960, game_height=1080, display_revision=3)
                run.wait(lambda: calls[-1] is True and run.snapshot().get('wine_notice_seq', 0) >= 2,
                         'the portrait policy')
                run.settle()
                self.assertEqual((run.sessions, first.stops), ([first], 0))
                self.assertEqual([value for value, _ in itertools.groupby(calls)], [True, False, True])
                self.assertIn('Reopen it now', run.snapshot()['wine_notice'])

    def test_the_control_socket_is_answered_while_the_wine_policy_runs(self):
        # The policy edits every Proton prefix of the account (a /proc scan, a fork and several fsyncs for each)
        # whenever the game monitor turns from portrait to landscape or back. The side's display follower asks
        # every 0.5 s and gives up on a reply after 3 s, so a policy run that keeps the loop from serving the
        # socket leaves the new layout unapplied for as long as it lasts (about 20 s on a large Steam library).
        entered, release = threading.Event(), threading.Event()
        def reconcile(owner, home, portrait):
            if portrait is False:
                entered.set()
                release.wait(20)
            return {'results': {}}
        with patch.object(m.wine_modelist, 'reconcile', reconcile), patch.object(m.os, 'geteuid', return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            with Supervised(self, version=2) as run:
                first = run.sessions[0]
                run.write(game_width=1920, game_height=1080, display_revision=2)
                self.assertTrue(entered.wait(5), 'the policy did not run for the landscape game monitor')
                try:
                    asked = time.monotonic()
                    live = run.live()    # raises when the supervisor does not answer within the client's 3 s
                    answered = time.monotonic() - asked
                    # What the follower reports after it applied the revision is accepted and kept as well.
                    m.client_display_report(run.endpoint, run.instance, 2, 1920, 1080, 'applied', '1920x1080 fit',
                                            server_uid=os.getuid())
                    run.wait(lambda: run.snapshot().get('display_applied_revision') == 2,
                             'the report in session.json')
                finally:
                    release.set()
                self.assertEqual((live['display_revision'], live['game_width'], live['game_height']),
                                 (2, 1920, 1080))
                self.assertLess(answered, 1.0)
                run.settle()
                self.assertEqual((run.sessions, first.stops), ([first], 0))
                self.assertEqual(run.snapshot()['display_applied_size'], [1920, 1080])

    def test_a_wine_policy_that_fails_is_logged_and_the_loop_goes_on(self):
        def reconcile(owner, home, portrait):
            if portrait is False:
                raise RuntimeError('cannot inspect processes for active Wine prefixes')
            return {'results': {}}
        log = io.StringIO()
        with patch.object(m.wine_modelist, 'reconcile', reconcile), patch.object(m.os, 'geteuid', return_value=0), \
                contextlib.redirect_stdout(log):
            with Supervised(self, version=2) as run:
                first = run.sessions[0]
                run.write(game_width=1920, game_height=1080, display_revision=2)
                run.wait(lambda: 'skipped for left: cannot inspect processes' in log.getvalue(),
                         'the failure in the log')
                run.read_back(display_revision=2)
                run.settle()
                self.assertEqual((run.sessions, first.stops), ([first], 0))

    def test_only_two_version_2_contracts_are_exempt_from_restarting(self):
        def contract(version):
            return {'version': version, 'live': dict(BASE_LIVE)}
        base = contract(2)
        for changes in (dict(width=1), dict(height=1), dict(game_width=1), dict(game_height=1),
                        dict(scale_mode='stretch'), dict(display_revision=2), dict(x=1), dict(y=1),
                        dict(layout='single')):
            changed = dict(base['live'], **changes)
            with self.subTest(changes=changes):
                self.assertFalse(m.display_change_restarts(base, {'version': 2, 'live': changed}))
                restarts = any(key in m.V1_RESTART_FIELDS for key in changes)
                for started, current in ((1, 1), (1, 2), (2, 1)):
                    self.assertEqual(m.display_change_restarts(contract(started), {'version': current, 'live': changed}),
                                     restarts)
        self.assertEqual(m.V1_RESTART_FIELDS, ('width', 'height', 'game_width', 'game_height',
                                               'scale_mode', 'display_revision'))


SHARED_CONFIG = '\n'.join([
    '"UserRoamingConfigStore"', '{', '\t"Software"', '\t{', '\t\t"Valve"', '\t\t{', '\t\t\t"Steam"', '\t\t\t{',
    '\t\t\t\t"Apps" { "22610" { "LastPlayed" "1" } }', '%s', '\t\t\t}', '\t\t}', '\t}', '}', ''])


class ControllerTour(unittest.TestCase):
    """Steam's Steam Controller tour (T-504): read the setting, tell, never write."""
    def setUp(self):
        if os.getuid() < 1000:
            self.skipTest('supervise() only runs sessions for an ordinary user')
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-controller-tour-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def account(self, name, setting=None, text=None, home=None):
        """Write userdata/<name>/7/remote/sharedconfig.vdf with the tour setting (None: no such key)."""
        directory = (home or self.home) / '.local/share/Steam/userdata' / name / '7/remote'
        directory.mkdir(parents=True, exist_ok=True)
        line = '' if setting is None else '\t\t\t\t"ShowIbexWelcomeTour"\t\t"%s"' % setting
        (directory / 'sharedconfig.vdf').write_text(SHARED_CONFIG % line if text is None else text)
        return directory / 'sharedconfig.vdf'

    def test_the_setting_is_read_at_any_depth_and_in_any_case(self):
        for text, expected in (
                ('"a" { "ShowIbexWelcomeTour" "0" }', '0'),
                ('"a" { "x" { "y" { "showibexwelcometour" "1" } } }', '1'),
                ('"a" { "SHOWIBEXWELCOMETOUR" "" }', ''),
                ('﻿"a" // comment\n{ "ShowIbexWelcomeTour"\t"0" }', '0'),
                ('"a" { "k" "v" "ShowIbexWelcomeTour" "0" }', '0'),
                ('"a" { "k" "ShowIbexWelcomeTour" "other" "1" }', None),     # as a value it is no key
                ('"a" { "k" { "ShowIbexWelcomeTour" { "b" "c" } } }', None),  # an object is no setting
                ('"a" { "Collection { \\"x\\"" "ShowIbexWelcomeTour" }', None),
                ('"a" { }', None), ('', None)):
            with self.subTest(text=text):
                self.assertEqual(m.tour_setting(text.encode()), expected)

    def test_a_file_that_is_not_well_formed_is_refused(self):
        for data in (b'"a" { "ShowIbexWelcomeTour" "0"', b'"a" } "ShowIbexWelcomeTour" "0" {', b'"a" { "b" "c }',
                     b'"a" { b "c" }', b'\xff\xfe"a" { }', b'"a" { "ShowIbexWelcomeTour" "0" } }',
                     b'<html>nope</html>'):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    m.tour_setting(data)

    def test_the_accounts_are_sorted_into_on_off_and_unreadable_and_nothing_is_written(self):
        files = [self.account('111'), self.account('222', '0'), self.account('333', '1'),
                 self.account('444', text='"a" { "ShowIbexWelcomeTour" "0"'), self.account('0', '1')]
        userdata = self.home / '.local/share/Steam/userdata'
        (userdata / '555/7/remote').mkdir(parents=True)     # no file yet
        (userdata / 'config').mkdir()                       # not an account
        outside = self.home / 'outside'
        target = self.account('666', '1', home=outside).parents[2]
        (userdata / '666').symlink_to(target)               # an account that is a link is not followed
        link = self.account('777', '0')
        link.rename(link.with_name('real.vdf'))
        link.symlink_to('real.vdf')                         # nor is a file that is a link
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        self.assertEqual(m.steam_tour_state(self.home, os.getuid()),
                         {'on': ['111', '333'], 'off': ['222'], 'unreadable': ['444', '777']})
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})
        self.assertEqual(sorted(path.name for path in (userdata / '111/7/remote').iterdir()), ['sharedconfig.vdf'])

    def test_a_missing_or_foreign_steam_directory_says_nothing(self):
        nothing = {'on': [], 'off': [], 'unreadable': []}
        self.assertEqual(m.steam_tour_state(self.home, os.getuid()), nothing)
        self.assertEqual(m.steam_tour_state(self.home / 'no-such-home', os.getuid()), nothing)
        self.account('111')
        self.assertEqual(m.steam_tour_state(self.home, os.getuid() + 1), nothing)     # not this owner's directory

    def test_an_oversized_file_is_unreadable_not_read(self):
        self.account('111', text='"a" { "x" "' + 'y' * (m.TOUR_MAX_BYTES + 1) + '" }')
        self.assertEqual(m.steam_tour_state(self.home, os.getuid())['unreadable'], ['111'])

    def test_the_sides_that_hold_a_steam_controller_come_from_the_routing_plans(self):
        libexec = self.home / 'libexec'
        self.assertEqual(m.steam_controller_sides(self.home), set())
        libexec.mkdir()
        for name, plan in (('native-controller-aaaaaaaa-plan.json', {'protocol': 'ibex', 'side': 'left', 'routing_side': 'right'}),
                           ('native-controller-bbbbbbbb-plan.json', {'protocol': 'ibex', 'side': 'left'}),
                           ('native-controller-cccccccc-plan.json', {'protocol': 'ibex', 'side': 'left', 'routing_side': 'unassigned'}),
                           ('native-controller-dddddddd-plan.json', {'protocol': 'other', 'side': 'right'}),
                           ('native-deck-plan.json', {'protocol': 'ibex', 'side': 'right'})):
            (libexec / name).write_text(json.dumps(plan))
        (libexec / 'native-controller-eeeeeeee-plan.json').write_text('{broken')
        (libexec / 'native-controller-ffffffff-plan.json').write_text('[1]')
        self.assertEqual(m.steam_controller_sides(self.home), {'left', 'right'})
        (libexec / 'native-controller-bbbbbbbb-plan.json').unlink()
        self.assertEqual(m.steam_controller_sides(self.home), {'right'})

    def supervised(self, routed='left', **accounts):
        """A supervisor of the left side, with a Steam Controller routed to `routed` (None: none) and the accounts
        given as name=setting (name starts with a, as the keyword cannot be a number)."""
        run = Supervised(self, version=2)
        self.addCleanup(run.temp.cleanup)
        if routed:
            (run.root / 'libexec').mkdir()
            (run.root / 'libexec/native-controller-aaaaaaaa-plan.json').write_text(
                json.dumps({'protocol': 'ibex', 'side': 'left', 'routing_side': routed}))
        for name, setting in accounts.items():
            self.account(name[1:], setting, home=run.root)
        return run

    def restarted(self, run, count, mode):
        m.client_mode(mode, run.endpoint, run.instance, server_uid=os.getuid())
        run.wait(lambda: run.started(count) and run.snapshot().get('lifecycle') == 'running', 'the restart')

    def test_a_side_with_a_steam_controller_and_the_tour_on_records_it_once(self):
        run = self.supervised(a111=None, a222='0', a333='1')
        config = run.root / '.local/share/Steam/userdata/222/7/remote/sharedconfig.vdf'
        before = config.read_bytes()
        log = io.StringIO()
        with contextlib.redirect_stdout(log), run:
            snapshot = run.snapshot()
            self.assertEqual(snapshot['controller_tour_accounts'], ['111', '333'])
            self.assertIn('Settings > Developer > Show Steam Controller Tour', snapshot['controller_tour_notice'])
            self.assertIn('"ShowIbexWelcomeTour" "0"', snapshot['controller_tour_notice'])
            # The field survives the loop's own writes and a restart of the side's session.
            run.write(activity='standby')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'standby', 'standby')
            self.assertEqual(run.snapshot()['controller_tour_accounts'], ['111', '333'])
            run.write(activity='active')
            run.wait(lambda: run.snapshot().get('lifecycle') == 'running', 'resume')
            self.restarted(run, 2, 'desktop')
            self.assertEqual(run.snapshot()['controller_tour_accounts'], ['111', '333'])
            self.assertEqual(config.read_bytes(), before)
        lines = [line for line in log.getvalue().splitlines() if 'Steam Controller tour is on' in line]
        self.assertEqual(len(lines), 1, log.getvalue())
        self.assertIn('left: the Steam Controller tour is on for tester account(s) 111, 333.', lines[0])
        self.assertIn('Show Steam Controller Tour', lines[0])

    def test_the_notice_follows_the_setting_when_the_side_starts_again(self):
        run = self.supervised(a111=None)
        log = io.StringIO()
        with contextlib.redirect_stdout(log), run:
            self.assertEqual(run.snapshot()['controller_tour_accounts'], ['111'])
            self.account('111', '0', home=run.root)       # the player switched the tour off
            self.restarted(run, 2, 'desktop')
            snapshot = run.snapshot()
            self.assertEqual((snapshot['controller_tour_accounts'], snapshot['controller_tour_notice']), ([], ''))
            self.account('111', '1', home=run.root)       # and on again
            self.restarted(run, 3, 'gaming')
            self.assertEqual(run.snapshot()['controller_tour_accounts'], ['111'])
        self.assertEqual(log.getvalue().count('Steam Controller tour is on'), 2)

    def test_a_side_without_a_steam_controller_or_with_the_tour_off_says_nothing(self):
        for label, run in (('the controller is routed to the other side', self.supervised(routed='right', a111=None)),
                           ('the tour is off', self.supervised(a111='0')),
                           ('no controller is routed', self.supervised(routed=None, a111=None)),
                           ('no Steam directory', self.supervised())):
            log = io.StringIO()
            with self.subTest(label), contextlib.redirect_stdout(log), run:
                snapshot = run.snapshot()
                self.assertEqual((snapshot['controller_tour_accounts'], snapshot['controller_tour_notice']), ([], ''))
                self.assertNotIn('tour', log.getvalue())

    def test_a_broken_file_or_a_failing_check_never_stops_the_side(self):
        run = self.supervised(a111=None)
        self.account('222', text='"a" { "ShowIbexWelcomeTour" "0"', home=run.root)
        with contextlib.redirect_stdout(io.StringIO()), run:
            self.assertEqual(run.snapshot()['controller_tour_accounts'], ['111'])
        run = self.supervised()
        self.account('333', text='\x00\x01', home=run.root)
        with contextlib.redirect_stdout(io.StringIO()), run:
            self.assertEqual(run.snapshot()['controller_tour_accounts'], [])
        log = io.StringIO()
        with patch.object(m, 'steam_tour_state', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(log), \
                self.supervised() as run:
            self.assertEqual(run.snapshot()['controller_tour_accounts'], [])
            self.assertIn('Steam Controller tour check skipped: boom', log.getvalue())


if __name__ == '__main__':
    unittest.main()
