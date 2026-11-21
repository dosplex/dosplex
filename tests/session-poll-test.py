#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Run the compositor's actual state poller against disposable session files."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

DECLARATIONS = r'''#define _GNU_SOURCE
#include <stdio.h>
#include <limits.h>
#include <stdint.h>
#include <time.h>
#include <sys/stat.h>
#include <sys/random.h>
#include <fcntl.h>
#include <json-c/json.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <signal.h>
#include <unistd.h>
enum side_state {SIDE_GREETER, SIDE_CLAIMED};
#define SIDE_LEFT 0
@DISPLAY_RESULT@
struct dosplex_config { bool allow_nested_sessions; };
struct compositor { struct dosplex_config config; };
struct side_ctx {struct compositor *comp; int desktop_scale_milli; enum side_state state; int id; pid_t worker_pid;
    bool recovery_required, launcher_failed, launch_pending, desktop_cursor, desktop_payload; void *auth;
    char launch_token[33], supervisor_instance[33]; char owner[64], username[64], password[64];
    char wine_notice_instance[33], wine_notice[160]; uint32_t wine_notice_seq;
    struct timespec wine_notice_started; bool wine_notice_timer_started;
    bool wine_notice_is_scaled, scaled_notice_given; unsigned scaled_notice_revision;
    int width, height, game_width, game_height; void *app_surface;
    unsigned display_revision; bool display_applied_known; unsigned display_applied_revision;
    int display_applied_width, display_applied_height; enum display_result display_result;
    char display_detail[160];};
static void clear_side_focus_on_lifecycle_change(struct compositor *c, struct side_ctx *s, const char *r) {}
static void side_set_desktop_scale(struct side_ctx *s, int milli) { s->desktop_scale_milli = milli; }
static void side_zoom_refresh(struct side_ctx *s) { (void)s; }
static bool side_zoom_in_effect(const struct side_ctx *s) { (void)s; return getenv("DOSPLEX_TEST_ZOOMED") != NULL; }
'''

# The poller, run once per process (or once per line of standard input with DOSPLEX_TEST_STEPS), then one line of
# what the environment variables ask to see. The region is 960x1080 and the game monitor Native unless a test says
# otherwise (DOSPLEX_TEST_REGION, DOSPLEX_TEST_GAME).
POLL_MAIN = r'''
static void report(const struct side_ctx *s, const char *token) {
    printf("%d %d %s", s->state, s->recovery_required, s->owner);
    if (token) printf(" %d %s", s->launch_pending, s->supervisor_instance);
    if (getenv("DOSPLEX_TEST_CURSOR")) printf(" cursor=%d", s->desktop_cursor);
    if (getenv("DOSPLEX_TEST_SCALE")) printf(" scale=%d", s->desktop_scale_milli);
    if (getenv("DOSPLEX_TEST_NOTICE")) printf(" notice=%u %s %s", s->wine_notice_seq,
        s->wine_notice_instance, s->wine_notice);
    if (getenv("DOSPLEX_TEST_DISPLAY")) printf(" display=%d %u %s|%s", s->display_applied_known,
        s->display_applied_revision, display_result_name(s->display_result), s->display_detail);
    if (getenv("DOSPLEX_TEST_SIZE")) printf(" size=%dx%d", s->display_applied_width, s->display_applied_height);
    if (getenv("DOSPLEX_TEST_STATUS")) {
        char line[160];
        const char *characters = getenv("DOSPLEX_TEST_CHARS");
        enum display_status status = side_display_status(s, characters ? strtoul(characters, NULL, 10) : 80,
            line, sizeof(line));
        printf(" status=%d %s", status, line);
    }
    if (getenv("DOSPLEX_TEST_SCALED_NOTICE")) printf(" scalednotice=%d|%s",
        s->scaled_notice_given ? (int)s->scaled_notice_revision : -1, s->wine_notice);
    if (getenv("DOSPLEX_TEST_SCALED")) printf(" scaled=%d", side_is_scaled(s));
    if (getenv("DOSPLEX_TEST_PICTURE")) {
        char header[128], label[128];
        const char *word = side_picture_word(s);
        side_region_line(s, "Full screen", header, sizeof(header));
        side_debug_label(s, 'L', "CLAIMED", label, sizeof(label));
        printf(" word=%s|%s|%s", word ? word : "-", header, label);
    }
    puts("");
    fflush(stdout);
}

int main(void) {
    struct compositor c = {.config = {.allow_nested_sessions = getenv("DOSPLEX_TEST_PLACEHOLDER_SIDES") == NULL}};
    struct side_ctx s = {.state=SIDE_CLAIMED, .owner="previous-owner", .width=960, .height=1080};
    const char *token = getenv("DOSPLEX_TEST_LAUNCH_TOKEN");
    if (token) { snprintf(s.launch_token, sizeof(s.launch_token), "%s", token); s.launch_pending = true; }
    const char *instance = getenv("DOSPLEX_TEST_INSTANCE");
    if (instance) snprintf(s.supervisor_instance, sizeof(s.supervisor_instance), "%s", instance);
    if (getenv("DOSPLEX_TEST_DISPLAY_SEED")) {  /* an earlier report that a new snapshot must replace */
        s.display_applied_known = true;
        s.display_applied_revision = 9;
        s.display_applied_width = 123;
        s.display_applied_height = 456;
        s.display_result = DISPLAY_RESULT_FAILED;
        snprintf(s.display_detail, sizeof(s.display_detail), "stale");
    }
    const char *revision = getenv("DOSPLEX_TEST_REVISION");
    if (revision) s.display_revision = (unsigned)strtoul(revision, NULL, 10);
    const char *region = getenv("DOSPLEX_TEST_REGION");
    if (region) sscanf(region, "%dx%d", &s.width, &s.height);
    const char *game = getenv("DOSPLEX_TEST_GAME");
    if (game) sscanf(game, "%dx%d", &s.game_width, &s.game_height);
    if (getenv("DOSPLEX_TEST_APP")) s.app_surface = &s;
    if (getenv("DOSPLEX_TEST_STEPS")) {
        /* One poll for each line "REVISION", "REVISION consume" or "REVISION relogin": the compositor's revision at
         * that moment and, for "consume", the overlay having shown the notice for its 8 seconds, for "relogin" a
         * new login having cleared the supervisor instance that the compositor adopted. */
        char line[64];
        while (fgets(line, sizeof(line), stdin)) {
            char *rest;
            s.display_revision = (unsigned)strtoul(line, &rest, 10);
            if (strstr(rest, "consume")) { s.wine_notice[0] = '\0'; s.wine_notice_timer_started = false; }
            if (strstr(rest, "relogin")) s.supervisor_instance[0] = '\0';
            poll_side_state(&c, &s);
            report(&s, token);
        }
        return 0;
    }
    poll_side_state(&c, &s);
    report(&s, token);
    return 0;
}
'''

SENTENCE = 'Scaled: game at %s. Fit fills the screen if the game shows black bars; relaunch the game to use the whole area.'
UNSIZED_SENTENCE = 'Scaled. Fit fills the screen if the game shows black bars; relaunch the game to use the whole area.'


def declarations():
    """The stand-ins for the rest of the compositor, with the enum that the compositor really has."""
    source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
    result = re.search(r'enum display_result \{.*?\n\};', source, re.S).group(0)
    shape = re.search(r'^static bool zoom_same_shape\([^;{]*\)\s*\{.*?\n\}\n', source, re.S | re.M).group(0)
    return DECLARATIONS.replace('@DISPLAY_RESULT@', result) + shape


def decoding_function():
    source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
    return source.split('/* === Session state decoding === */', 1)[1].split('/* === Scene rendering === */', 1)[0]


class Poller:
    """The compositor's poller compiled against a temporary side root, with the left side's session.json."""

    def __init__(self):
        self.previous_umask = os.umask(0o022)
        self.temporary = tempfile.TemporaryDirectory(prefix='dosplex-poll-test-')
        try:
            self.directory = self.temporary.name
            self.root = Path(self.directory)
            (self.root / 'left').mkdir()
            self.code = self.root / 'poll.c'
            self.code.write_text(declarations() + decoding_function() + POLL_MAIN)
            self.flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'json-c'], text=True).split()
            self.binary = self.build('poll', 'getuid()')
        except BaseException:
            self.close()
            raise
        self.path = self.root / 'left/session.json'
        self.valid = dict(side='left', owner='tester', pid=os.getpid(), active=True, lifecycle='running')

    def close(self):
        self.temporary.cleanup()
        os.umask(self.previous_umask)

    def __enter__(self):
        return self

    def __exit__(self, *problem):
        self.close()

    def build(self, name, uid):
        binary = self.root / name
        subprocess.run(['cc', '-DSESSION_STATE_UID=' + uid, '-DSIDE_STATE_ROOT=' + json.dumps(self.directory),
                        str(self.code), '-o', str(binary), *self.flags],
                       check=True, capture_output=True, timeout=15)
        return binary

    def result(self, extra=None):
        return subprocess.check_output([str(self.binary)], text=True, timeout=5,
                                       env=dict(os.environ, **(extra or {}))).strip()

    def write(self, **fields):
        self.path.write_text(json.dumps(dict(self.valid, **fields)))

    def steps(self, extra, script):
        """Poll once for each step: (session fields, or None to remove the file, line for the poller); returns the lines."""
        process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                   env=dict(os.environ, DOSPLEX_TEST_STEPS='1', **extra))
        lines = []
        try:
            for fields, line in script:
                if fields is None:
                    self.path.unlink()
                else:
                    self.write(**fields)
                process.stdin.write(line + '\n')
                process.stdin.flush()
                lines.append(process.stdout.readline().strip())
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
        return lines


class PollTests(unittest.TestCase):
    def test_reset_writes_a_fresh_restart_token_only_into_a_trusted_side_directory(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        function = decoding_function()
        with tempfile.TemporaryDirectory(prefix='dosplex-reset-test-') as directory:
            root = Path(directory)
            (root / 'left').mkdir()
            (root / 'right').mkdir()
            code = root / 'reset.c'
            code.write_text(declarations() + function + '''
int main(int argc, char **argv) {
    return request_side_restart(argc > 1 ? argv[1] : "left") ? 0 : 3;
}
''')
            flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'json-c'], text=True).split()
            subprocess.run(['cc', '-DSESSION_STATE_UID=getuid()', '-DSIDE_STATE_ROOT=' + json.dumps(directory), str(code),
                            '-o', str(root / 'reset'), *flags], check=True, capture_output=True, timeout=15)
            def run(side, extra=None):
                return subprocess.run([str(root / 'reset'), side], timeout=5,
                                      env=dict(os.environ, **(extra or {}))).returncode
            self.assertEqual(run('left'), 0)
            first = (root / 'left/restart').read_text()
            self.assertRegex(first, r'^[0-9a-f]{32}\n$')
            self.assertEqual(run('left'), 0)
            second = (root / 'left/restart').read_text()
            self.assertRegex(second, r'^[0-9a-f]{32}\n$')
            self.assertNotEqual(first, second)
            self.assertEqual(sorted(p.name for p in (root / 'left').iterdir()), ['restart'])
            self.assertFalse((root / 'right/restart').exists())
            # The side root comes from the environment, exactly as for session.json.
            with tempfile.TemporaryDirectory(prefix='dosplex-reset-alt-') as alternate:
                (Path(alternate) / 'right').mkdir()
                self.assertEqual(run('right', {'DOSPLEX_SIDE_ROOT': alternate}), 0)
                self.assertRegex((Path(alternate) / 'right/restart').read_text(), r'^[0-9a-f]{32}\n$')
            # A side directory that others could write is not the supervisor's; nothing is written.
            (root / 'right').chmod(0o777)
            self.assertEqual(run('right'), 3)
            self.assertEqual(list((root / 'right').iterdir()), [])
            self.assertEqual(run('missing'), 3)

    def test_ownership_survives_session_failure_and_waiting(self):
        with Poller() as poller:
            root, path, result = poller.root, poller.path, poller.result
            self.assertEqual(result(), '0 0')
            # An acknowledged logout also drops the last display report.
            self.assertEqual(result({'DOSPLEX_TEST_DISPLAY': '1', 'DOSPLEX_TEST_DISPLAY_SEED': '1',
                                     'DOSPLEX_TEST_SIZE': '1'}),
                             '0 0  display=0 0 | size=0x0')
            for lifecycle, active, recovery in [('running', True, False), ('starting', True, False),
                    ('sleeping', False, False), ('waiting_display', False, False),
                    ('stopping', True, False), ('cleanup_failed', True, True),
                    ('stopped', False, True), ('exited', False, True)]:
                with self.subTest(lifecycle=lifecycle):
                    state = dict(side='left', owner='tester', pid=os.getpid(),
                                 lifecycle=lifecycle, active=active)
                    path.write_text(json.dumps(state))
                    self.assertEqual(result(), f'1 {int(recovery)} tester')
                    state['pid'] = 0
                    path.write_text(json.dumps(state))
                    self.assertEqual(result(), '1 1 tester')
            valid = dict(side='left', owner='tester', pid=os.getpid(), active=True, lifecycle='running')
            for mode, active, expected in [('desktop', True, 1), ('gaming', True, 0), ('desktop', False, 0)]:
                path.write_text(json.dumps(dict(valid, mode=mode, active=active)))
                self.assertTrue(result({'DOSPLEX_TEST_CURSOR': '1'}).endswith(f'cursor={expected}'))
            # The Desktop scale the launcher reported (thousandths) reaches the side only for an active Desktop
            # session; every other payload, a stopped session, a missing or an out-of-range value is scale 1.
            for extra, expected in [(dict(mode='desktop', desktop_scale_milli=2000), 2000),
                                    (dict(mode='desktop', desktop_scale_milli=1500), 1500),
                                    (dict(mode='desktop'), 1000),
                                    (dict(mode='gaming', desktop_scale_milli=2000), 1000),
                                    (dict(desktop_scale_milli=2000), 1000),
                                    (dict(mode='desktop', active=False, desktop_scale_milli=2000), 1000),
                                    (dict(mode='desktop', desktop_scale_milli=999), 1000),
                                    (dict(mode='desktop', desktop_scale_milli=3001), 1000),
                                    (dict(mode='desktop', desktop_scale_milli='2000'), 1000),
                                    (dict(mode='desktop', desktop_scale_milli=2000.5), 1000)]:
                path.write_text(json.dumps(dict(valid, **extra)))
                self.assertTrue(result({'DOSPLEX_TEST_SCALE': '1'}).endswith(f'scale={expected}'), extra)
            path.write_text(json.dumps(dict(valid, mode='unexpected')))
            self.assertEqual(result(), '1 1 previous-owner')
            alternate = root / 'trial-state'
            (alternate / 'left').mkdir(parents=True)
            (alternate / 'left/session.json').write_text(json.dumps(dict(valid, owner='trial-owner')))
            self.assertEqual(result({'DOSPLEX_SIDE_ROOT': str(alternate)}), '1 0 trial-owner')
            # Without -s the sides are placeholders: the state files are never read.
            self.assertEqual(result({'DOSPLEX_SIDE_ROOT': str(alternate), 'DOSPLEX_TEST_PLACEHOLDER_SIDES': '1'}),
                             '1 0 previous-owner')
            for invalid in ('', 'relative/state'):
                self.assertEqual(result({'DOSPLEX_SIDE_ROOT': invalid}), '1 1 previous-owner')
            nested = dict(valid, unrelated={'owner': 'intruder', 'pid': 0, 'active': False},
                          text='"owner": "intruder", "pid": 0')
            path.write_text('  ' + json.dumps(nested) + '\n\t')
            self.assertEqual(result(), '1 0 tester')
            path.write_text(json.dumps(dict(valid, owner='tést', instance='a' * 32)))
            self.assertEqual(result(), '1 0 tést')
            notice = 'A first-run game was updated for Native. Reopen it now.'
            path.write_text(json.dumps(dict(valid, instance='d' * 32,
                                            wine_notice_seq=4, wine_notice=notice)))
            self.assertEqual(result({'DOSPLEX_TEST_NOTICE': '1'}),
                             f'1 0 tester notice=4 {"d" * 32} {notice}')
            path.write_text(json.dumps(dict(valid, instance='d' * 32,
                                            wine_notice_seq='4', wine_notice=notice)))
            self.assertEqual(result({'DOSPLEX_TEST_NOTICE': '1'}),
                             '1 1 previous-owner notice=0')
            # The supervisor's report on applying the display contract is
            # cosmetic: read when well formed, left out (never a recovery) when not.
            display = {'DOSPLEX_TEST_DISPLAY': '1'}
            path.write_text(json.dumps(dict(valid, display_applied_revision=7,
                                            display_result='failed', display_detail='xrandr timed out')))
            self.assertEqual(result(display), '1 0 tester display=1 7 failed|xrandr timed out')
            path.write_text(json.dumps(valid))  # a snapshot without a report replaces an earlier one
            self.assertEqual(result(dict(display, DOSPLEX_TEST_DISPLAY_SEED='1', DOSPLEX_TEST_SIZE='1')),
                             '1 0 tester display=0 0 | size=0x0')
            path.write_text(json.dumps(dict(valid, display_applied_revision=2**32 - 1, display_result='applied')))
            self.assertEqual(result(display), f'1 0 tester display=1 {2**32 - 1} applied|')
            for report, expected in [
                    (dict(display_applied_revision='7', display_result='failed', display_detail='kept'), '0 0 failed|kept'),
                    (dict(display_applied_revision=7.5), '0 0 |'),
                    (dict(display_applied_revision=-1), '0 0 |'),
                    (dict(display_applied_revision=2**32), '0 0 |'),
                    (dict(display_applied_revision=True), '0 0 |'),
                    (dict(display_applied_revision=3, display_result='unknown'), '1 3 |'),
                    (dict(display_applied_revision=3, display_result=42, display_detail=['x']), '1 3 |'),
                    (dict(display_detail='first\nsecond\tthird'), '0 0 |first second third'),
                    (dict(display_detail='a' * 300), '0 0 |' + 'a' * 159),
                    (dict(display_detail='é' * 200), '0 0 |' + 'é' * 79),  # never cut inside a character
                    (dict(display_detail='€' * 100), '0 0 |' + '€' * 53)]:
                with self.subTest(report=report):
                    path.write_text(json.dumps(dict(valid, **report)))
                    self.assertEqual(result(display), '1 0 tester display=' + expected)
            # The Display page's line: "Applying…" while the report lags the
            # revision, the failure detail when the current revision failed.
            def status_line(report, revision, characters=None):
                path.write_text(json.dumps(dict(valid, **report)))
                extra = {'DOSPLEX_TEST_STATUS': '1', 'DOSPLEX_TEST_REVISION': str(revision)}
                if characters:
                    extra['DOSPLEX_TEST_CHARS'] = str(characters)
                return result(extra).partition(' status=')[2]
            failed = dict(display_applied_revision=4, display_result='failed', display_detail='xrandr timed out')
            for report, revision, characters, expected in [
                    ({}, 5, None, '0'),  # no revision in the snapshot: say nothing
                    (dict(display_result='failed', display_detail='boom'), 5, None, '0'),
                    (dict(display_applied_revision=0), 0, None, '0'),
                    (dict(display_applied_revision=0), 1, None, '1 Applying…'),
                    (dict(display_applied_revision=3), 4, None, '1 Applying…'),
                    (dict(failed, display_applied_revision=3), 4, None, '1 Applying…'),  # an older failure is stale
                    (dict(display_applied_revision=4, display_result='applied'), 4, None, '0'),
                    (dict(display_applied_revision=4), 4, None, '0'),
                    (dict(failed, display_applied_revision=9), 4, None, '0'),  # the report is ahead of this compositor
                    (failed, 4, None, '2 xrandr timed out'),
                    (dict(failed, display_detail=''), 4, None, '2 failed'),
                    (dict(failed, display_detail='   spaced out'), 4, None, '2 spaced out'),
                    (dict(failed, display_detail='x' * 100), 4, 20, '2 ' + 'x' * 20 + '…'),
                    (dict(failed, display_detail='x' * 100), 4, None, '2 ' + 'x' * 80 + '…'),
                    (dict(failed, display_detail='é' * 7), 4, 5, '2 ' + 'é' * 5 + '…'),
                    (dict(failed, display_detail='abc def'), 4, 4, '2 abc…'),  # no space before the ellipsis
                    (dict(failed, display_detail='abcde'), 4, 5, '2 abcde')]:
                with self.subTest(report=report, revision=revision, characters=characters):
                    self.assertEqual(status_line(report, revision, characters), expected)
            requested = {'DOSPLEX_TEST_LAUNCH_TOKEN': 'a' * 32}
            acknowledged = dict(valid, owner='previous-owner', launch_token='a' * 32, instance='b' * 32)
            for changes, expected in [({}, '1 0 previous-owner 0 ' + 'b' * 32),
                    ({'launch_token': 'c' * 32}, '1 0 previous-owner 1'),
                    ({'owner': 'wrong-owner'}, '1 0 previous-owner 1'),
                    ({'lifecycle': 'starting'}, '1 0 previous-owner 1 ' + 'b' * 32),
                    ({'pid': 0}, '1 0 previous-owner 1 ' + 'b' * 32),
                    ({'lifecycle': 'sleeping', 'active': False}, '1 0 previous-owner 0 ' + 'b' * 32),
                    ({'lifecycle': 'cleanup_failed'}, '1 1 previous-owner 0 ' + 'b' * 32)]:
                with self.subTest(ack=changes):
                    path.write_text(json.dumps(dict(acknowledged, **changes)))
                    self.assertEqual(result(requested), expected)
            path.write_text(json.dumps(dict(acknowledged, instance='c' * 32)))
            self.assertEqual(result(dict(requested, DOSPLEX_TEST_INSTANCE='b' * 32)),
                             '1 1 previous-owner 0 ' + 'b' * 32)
            # A compositor adopting existing state must also keep its observed
            # owner/instance when a later snapshot changes, without a launch token.
            path.write_text(json.dumps(dict(acknowledged, instance='c' * 32)))
            self.assertEqual(result({'DOSPLEX_TEST_INSTANCE': 'b' * 32}), '1 1 previous-owner')
            malformed = [b'{', b'x' * 65537, b'[]', b'null', b'"text"', b'{}',
                         json.dumps(valid).encode() + b' {}',
                         b'/* comment */' + json.dumps(valid).encode(),
                         json.dumps(valid).encode() + b'\0garbage',
                         json.dumps(valid).replace('tester', '\xff').encode('latin1')]
            for key, values in {'side': ['right', None], 'owner': ['', 42, 'a' * 64, 'a\0b'],
                                'pid': [-1, 2**63, 1.5, '1', True], 'active': [1, 'true', None],
                                'lifecycle': ['unknown', [], 'a' * 32],
                                'instance': ['', 'z' * 32, 'a' * 31, 42]}.items():
                malformed += [json.dumps(dict(valid, **{key: value})).encode() for value in values]
            for key in ('side', 'owner', 'pid', 'active'):
                malformed.append(json.dumps({k: v for k, v in valid.items() if k != key}).encode())
            malformed.append(json.dumps(dict(valid, extra=[[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]])).encode())
            for raw in malformed:
                with self.subTest(raw=raw[:90]):
                    path.write_bytes(raw)
                    self.assertEqual(result(), '1 1 previous-owner')
            path.unlink()
            path.mkdir()  # read error, not an absent claim
            self.assertEqual(result(), '1 1 previous-owner')
            path.rmdir()
            os.mkfifo(path)
            self.assertEqual(result(), '1 1 previous-owner')
            path.unlink()
            target = root / 'elsewhere.json'
            target.write_text(json.dumps(valid))
            path.symlink_to(target)
            self.assertEqual(result(), '1 1 previous-owner')
            path.unlink()
            os.link(target, path)
            self.assertEqual(result(), '1 1 previous-owner')
            path.unlink()
            path.write_text(json.dumps(valid))
            path.chmod(0o666)
            self.assertEqual(result(), '1 1 previous-owner')
            path.chmod(0o644)
            path.parent.chmod(0o777)
            self.assertEqual(result(), '1 1 previous-owner')
            path.parent.chmod(0o755)
            root.chmod(0o777)
            self.assertEqual(result(), '1 1 previous-owner')
            root.chmod(0o700)
            self.assertEqual(result(), '1 0 tester')
            real_side = root / 'real-left'
            path.parent.rename(real_side)
            (root / 'left').symlink_to(real_side, target_is_directory=True)
            self.assertEqual(result(), '1 1 previous-owner')
            (root / 'left').unlink()
            real_side.rename(root / 'left')
            poller.build('wrong-owner', str(os.getuid() + 1))
            self.assertEqual(subprocess.check_output([str(root / 'wrong-owner')], text=True, timeout=5).strip(),
                             '1 1 previous-owner')

    def test_a_deferred_report_and_the_monitor_size_are_decoded(self):
        with Poller() as poller:
            detail = 'game 892970 running; monitor stays 3840x2160 (target 1920x2160)'
            everything = {'DOSPLEX_TEST_DISPLAY': '1', 'DOSPLEX_TEST_SIZE': '1'}
            # "deferred" is a result like "applied" and "failed": a game runs, so the monitor kept its size.
            poller.write(display_applied_revision=7, display_applied_size=[3840, 2160], display_result='deferred',
                         display_detail=detail)
            self.assertEqual(poller.result(everything), f'1 0 tester display=1 7 deferred|{detail} size=3840x2160')
            # A later report for the same revision can say applied once the game has exited.
            poller.write(display_applied_revision=7, display_applied_size=[1920, 2160], display_result='applied')
            self.assertEqual(poller.result(everything), '1 0 tester display=1 7 applied| size=1920x2160')
            # The monitor size is two integers from 1 to 16384; anything else is left out and the rest stands.
            for size, expected in [([1, 1], '1x1'), ([16384, 16384], '16384x16384'), ([3840, 2160], '3840x2160'),
                                   ([0, 1080], '0x0'), ([1920, 0], '0x0'), ([16385, 1080], '0x0'), ([1920, -1], '0x0'),
                                   ([3840], '0x0'), ([3840, 2160, 1], '0x0'), ([], '0x0'),
                                   ([3840.0, 2160], '0x0'), ([True, 2160], '0x0'), (['3840', 2160], '0x0'),
                                   ([None, None], '0x0'), ('3840x2160', '0x0'), ({'width': 3840, 'height': 2160}, '0x0'),
                                   (3840, '0x0'), (None, '0x0'), ([[3840], [2160]], '0x0'), ([2**63, 1], '0x0')]:
                with self.subTest(size=size):
                    poller.write(display_applied_revision=3, display_applied_size=size, display_result='applied')
                    self.assertEqual(poller.result(everything), f'1 0 tester display=1 3 applied| size={expected}')
            poller.write(display_applied_revision=3, display_result='applied')  # no size at all
            self.assertEqual(poller.result(everything), '1 0 tester display=1 3 applied| size=0x0')
            poller.write(display_applied_size=[1920, 1080])  # a size without a revision is read, and says nothing
            self.assertEqual(poller.result(everything), '1 0 tester display=0 0 | size=1920x1080')
            # An unknown word is still no result.
            for word in ('Deferred', 'DEFERRED', 'scaled', '', 'applied ', 7):
                with self.subTest(word=word):
                    poller.write(display_applied_revision=3, display_applied_size=[800, 600], display_result=word)
                    self.assertEqual(poller.result(everything), '1 0 tester display=1 3 | size=800x600')

    def test_a_side_is_scaled_when_the_report_defers_or_the_monitor_is_not_the_size_asked_for(self):
        with Poller() as poller:
            def scaled(revision, region=None, game=None, **report):
                poller.write(**report)
                extra = {'DOSPLEX_TEST_SCALED': '1', 'DOSPLEX_TEST_REVISION': str(revision)}
                if region:
                    extra['DOSPLEX_TEST_REGION'] = region
                if game:
                    extra['DOSPLEX_TEST_GAME'] = game
                return poller.result(extra).partition(' scaled=')[2]
            deferred = dict(display_applied_revision=5, display_result='deferred', display_applied_size=[3840, 2160])
            applied = dict(display_applied_revision=5, display_result='applied')
            failed = dict(display_applied_revision=5, display_result='failed')
            for label, revision, kwargs, expected in [
                    # A deferred report for the current revision: scaled, whatever the sizes say.
                    ('deferred', 5, dict(deferred), '1'),
                    ('deferred, the size equal to the region', 5, dict(deferred, display_applied_size=[960, 1080]), '1'),
                    ('deferred, no size', 5, dict(display_applied_revision=5, display_result='deferred'), '1'),
                    # Another revision is not the current one: only the sizes decide.
                    ('deferred for an older revision, the sizes differ', 6, dict(deferred), '1'),
                    ('deferred for an older revision, the sizes equal', 6,
                     dict(deferred, display_applied_size=[960, 1080]), '0'),
                    ('deferred for a newer revision, the sizes equal', 4,
                     dict(deferred, display_applied_size=[960, 1080]), '0'),
                    # Applied or failed: the monitor in effect against the size asked for (Native is the region).
                    ('applied, Native, the monitor is the region', 5, dict(applied, display_applied_size=[960, 1080]), '0'),
                    ('applied, Native, the monitor is another size', 5, dict(applied, display_applied_size=[1920, 1080]), '1'),
                    ('applied, one dimension differs', 5, dict(applied, display_applied_size=[960, 1081]), '1'),
                    ('applied, no size', 5, dict(applied), '0'),
                    ('applied, an invalid size', 5, dict(applied, display_applied_size=[0, 0]), '0'),
                    ('failed, the monitor is not the size asked for', 5, dict(failed, display_applied_size=[3840, 2160]), '1'),
                    ('failed, the monitor is the size asked for', 5, dict(failed, display_applied_size=[960, 1080]), '0'),
                    # No report: nothing is known, so nothing is claimed.
                    ('no revision', 5, dict(display_applied_size=[3840, 2160], display_result='deferred'), '0'),
                    ('no report at all', 5, {}, '0'),
                    # A Desktop payload has no game monitor.
                    ('Desktop, deferred', 5, dict(deferred, mode='desktop'), '0'),
                    ('Desktop, the sizes differ', 5, dict(applied, display_applied_size=[3840, 2160], mode='desktop'), '0'),
                    ('gaming, the sizes differ', 5, dict(applied, display_applied_size=[3840, 2160], mode='gaming'), '1')]:
                with self.subTest(label):
                    self.assertEqual(scaled(revision, **kwargs), expected)
            # A game monitor of its own is what the monitor in effect is compared with.
            for label, game, size, expected in [('the monitor is the 16:9 asked for', '960x540', [960, 540], '0'),
                                                ('the monitor is not', '960x540', [960, 1080], '1'),
                                                ('Native: the region again', '0x0', [960, 1080], '0')]:
                with self.subTest(label):
                    self.assertEqual(scaled(5, game=game, **dict(applied, display_applied_size=size)), expected)
            # Without a region the size asked for is unknown.
            self.assertEqual(scaled(5, region='0x0', **dict(applied, display_applied_size=[3840, 2160])), '0')
            self.assertEqual(scaled(5, region='1920x1080', **dict(applied, display_applied_size=[1920, 1080])), '0')
            self.assertEqual(scaled(5, region='1920x1080', **dict(applied, display_applied_size=[960, 1080])), '1')

    def test_the_display_page_says_scaled_for_a_deferred_report(self):
        with Poller() as poller:
            def status(revision, characters=None, **report):
                poller.write(**report)
                extra = {'DOSPLEX_TEST_STATUS': '1', 'DOSPLEX_TEST_REVISION': str(revision)}
                if characters:
                    extra['DOSPLEX_TEST_CHARS'] = str(characters)
                return poller.result(extra).partition(' status=')[2]
            deferred = dict(display_applied_revision=4, display_result='deferred', display_applied_size=[3840, 2160],
                            display_detail='game 892970 running; monitor stays 3840x2160 (target 1920x2160)')
            # The report that deferred the revision has acknowledged it: no "Applying…", the Scaled sentence.
            self.assertEqual(status(4, **deferred), '3 ' + SENTENCE % '3840x2160')
            self.assertEqual(status(4, 20, **deferred), '3 ' + SENTENCE % '3840x2160')  # a sentence is never cut
            self.assertEqual(status(4, **dict(deferred, display_applied_size=[1280, 720])), '3 ' + SENTENCE % '1280x720')
            self.assertEqual(status(4, **dict(deferred, display_applied_size=[0, 5])), '3 ' + UNSIZED_SENTENCE)
            self.assertEqual(status(4, **dict(deferred, display_applied_size=[960, 1080])), '3 ' + SENTENCE % '960x1080')
            # A report for an older revision has not acknowledged this one, and one for a newer says nothing.
            self.assertEqual(status(5, **deferred), '1 Applying…')
            self.assertEqual(status(3, **deferred), '0')
            # Applied after the deferral, for the same revision: scaled no more when the monitor is the size asked for.
            applied = dict(deferred, display_result='applied', display_detail='')
            self.assertEqual(status(4, **dict(applied, display_applied_size=[960, 1080])), '0')
            self.assertEqual(status(4, **dict(applied, display_applied_size=[1920, 2160])), '3 ' + SENTENCE % '1920x2160')
            self.assertEqual(status(4, **dict(applied, display_applied_size=None)), '0')
            # A failure keeps its own text, and a Desktop payload has no game monitor to scale.
            self.assertEqual(status(4, **dict(deferred, display_result='failed', display_detail='xrandr timed out')),
                             '2 xrandr timed out')
            self.assertEqual(status(4, **dict(deferred, mode='desktop')), '0')

    def test_the_overlay_header_and_the_labels_say_native_or_scaled(self):
        with Poller() as poller:
            def picture(app=True, region='960x1080', game=None, revision=5, **report):
                poller.write(**report)
                extra = {'DOSPLEX_TEST_PICTURE': '1', 'DOSPLEX_TEST_REVISION': str(revision),
                         'DOSPLEX_TEST_REGION': region}
                if app:
                    extra['DOSPLEX_TEST_APP'] = '1'
                if game:
                    extra['DOSPLEX_TEST_GAME'] = game
                return poller.result(extra).partition(' word=')[2]
            def expected(word, region='960x1080', size=None):
                header = f'{region} screen region \u00b7 Full screen'
                label = 'L: CLAIMED / tester'
                if word == 'native':
                    header += ' \u00b7 Native'
                elif word == 'scaled':
                    header += f' \u00b7 Scaled (game at {size})' if size else ' \u00b7 Scaled'
                return f'{word}|{header}|{label}' + (f' / {word}' if word != '-' else '')
            deferred = dict(display_applied_revision=5, display_result='deferred', display_applied_size=[3840, 2160])
            applied = dict(display_applied_revision=5, display_result='applied')
            # A side with an app surface says native when the monitor is the size asked for, scaled when it is not.
            self.assertEqual(picture(**dict(applied, display_applied_size=[960, 1080])), expected('native'))
            self.assertEqual(picture(**deferred), expected('scaled', size='3840x2160'))
            self.assertEqual(picture(**dict(applied, display_applied_size=[1920, 1080])), expected('scaled', size='1920x1080'))
            self.assertEqual(picture(**dict(deferred, display_applied_size=None)), expected('scaled'))
            self.assertEqual(picture(region='1920x2160', **dict(deferred, display_applied_size=[3840, 2160])),
                             expected('scaled', '1920x2160', '3840x2160'))
            self.assertEqual(picture(game='960x540', **dict(applied, display_applied_size=[960, 540])), expected('native'))
            self.assertEqual(picture(game='960x540', **dict(applied, display_applied_size=[960, 1080])),
                             expected('scaled', size='960x1080'))
            # Applied after the deferral, it is native again; without a report, nothing says otherwise.
            self.assertEqual(picture(**dict(deferred, display_result='applied', display_applied_size=[960, 1080])),
                             expected('native'))
            self.assertEqual(picture(), expected('native'))
            # A side without an app surface shows no picture, and a Desktop payload has no game monitor.
            self.assertEqual(picture(app=False, **deferred), expected('-'))
            self.assertEqual(picture(app=False, **dict(applied, display_applied_size=[960, 1080])), expected('-'))
            self.assertEqual(picture(**dict(deferred, mode='desktop')), expected('-'))

    def test_the_first_deferred_report_for_a_revision_raises_the_scaled_notice_once(self):
        with Poller() as poller:
            sentence = SENTENCE % '3840x2160'
            deferred = dict(display_applied_revision=5, display_result='deferred', display_applied_size=[3840, 2160])
            applied = dict(display_applied_revision=5, display_result='applied', display_applied_size=[1920, 1080])
            extra = {'DOSPLEX_TEST_REGION': '1920x1080', 'DOSPLEX_TEST_SCALED_NOTICE': '1', 'DOSPLEX_TEST_SCALED': '1'}
            def notices(script):
                return [line.partition(' scalednotice=')[2] for line in poller.steps(extra, script)]
            # Once for the revision: the same report again, a report that only changes its detail, and the overlay
            # having shown it raise nothing; the next revision raises its own, and the monitor being applied
            # withdraws a notice that nobody has seen. A report for another revision than the current raises none.
            self.assertEqual(notices([
                (deferred, '5'),
                (deferred, '5 consume'),
                (dict(deferred, display_detail='game 892970 running'), '5'),
                (applied, '5'),
                (dict(deferred, display_applied_revision=6, display_applied_size=[1280, 720]), '6'),
                (dict(applied, display_applied_revision=6), '6'),
                (dict(deferred, display_applied_revision=7), '8'),
                (dict(deferred, display_applied_revision=8), '8'),
            ]), [f'5|{sentence} scaled=1', '5| scaled=1', '5| scaled=1', '5| scaled=0',
                 f'6|{SENTENCE % "1280x720"} scaled=1', '6| scaled=0', '6| scaled=1',
                 f'8|{sentence} scaled=1'])
            # The supervisor's own notice is not overwritten; the Scaled notice waits for its turn and then comes.
            wine = dict(instance='d' * 32, wine_notice_seq=1, wine_notice='Reopen the game.')
            again = dict(wine, wine_notice_seq=2, wine_notice='Reopen it again.')
            self.assertEqual(notices([
                (dict(deferred, **wine), '5'),
                (dict(deferred, **wine), '5 consume'),
                (dict(deferred, **again), '5'),
                (dict(applied, **again), '5'),
            ]), ['-1|Reopen the game. scaled=1', f'5|{sentence} scaled=1', '5|Reopen it again. scaled=1',
                 '5|Reopen it again. scaled=0'])
            # A new supervisor instance is a new session: the same revision is noticed again. So is a login after a logout.
            self.assertEqual(notices([
                (dict(deferred, instance='a' * 32), '5'),
                (dict(deferred, instance='a' * 32), '5 consume'),
                (dict(deferred, instance='b' * 32), '5 relogin'),
                (dict(deferred, instance='b' * 32), '5 consume'),
                (None, '5'),
                (dict(deferred, instance='c' * 32), '5 relogin'),
            ]), [f'5|{sentence} scaled=1', '5| scaled=1', f'5|{sentence} scaled=1', '5| scaled=1', '-1| scaled=0',
                 f'5|{sentence} scaled=1'])
            # A Desktop payload has no game monitor to notice.
            self.assertEqual(notices([(dict(deferred, mode='desktop'), '5')]), ['-1| scaled=0'])


class ZoomSentenceTests(unittest.TestCase):
    def test_the_scaled_sentence_names_zoom_and_a_zoomed_side_names_fit(self):
        deferred = dict(display_applied_revision=2, display_applied_size=[1920, 2160], display_result='deferred')
        with Poller() as poller:
            poller.write(**deferred)
            extra = {'DOSPLEX_TEST_REGION': '3840x2160', 'DOSPLEX_TEST_REVISION': '2',
                     'DOSPLEX_TEST_SCALED_NOTICE': '1'}
            self.assertIn(' scalednotice=2|Scaled: game at 1920x2160. Fit fills the screen if the game shows black '
                          'bars; relaunch the game to use the whole area.', poller.result(extra))
            self.assertIn(' scalednotice=2|Fitted: game at 1920x2160. Keep ratio shows the whole picture; relaunch the game '
                          'to use the whole area.', poller.result(dict(extra, DOSPLEX_TEST_ZOOMED='1')))
            poller.write(display_applied_revision=2, display_result='deferred')
            self.assertIn('|Fitted. Keep ratio shows the whole picture; relaunch the game to use the whole area.',
                          poller.result(dict(extra, DOSPLEX_TEST_ZOOMED='1')))


if __name__ == '__main__':
    unittest.main()
