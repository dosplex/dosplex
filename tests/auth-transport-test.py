#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise the compositor's actual auth call with disposable unprivileged spies."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('side_auth', ROOT / 'guest/dosplex-side-auth.py')
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class AuthTransportTests(unittest.TestCase):
    def read(self, raw):
        with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')):
            return auth.read_password()

    def test_stdin_preserves_exact_password(self):
        self.assertEqual(self.read(' spaces ü\n'.encode()), ' spaces ü\n')
        self.assertEqual(self.read(b'a' * 4096), 'a' * 4096)

    def test_invalid_input_fails_without_echoing(self):
        for raw in (b'a' * 4097, b'public-test\0value', b'\xff'):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit):
                    self.read(raw)
            self.assertFalse(json.loads(out.getvalue())['ok'])
            self.assertNotIn('public-test', out.getvalue())

    def test_cli_rejects_password_argument(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(auth, 'SIDE_ROOT', Path(directory)), \
             patch.object(auth.os, 'geteuid', return_value=0), patch.object(auth, 'prepare_state_root'), patch.object(auth, 'login_side') as login:
            with patch.object(sys, 'argv', ['auth', 'login', 'left', 'user', 'public-test-secret']), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit):
                    auth.main()
                self.assertNotIn('public-test-secret', out.getvalue())
                login.assert_not_called()
            with patch.object(sys, 'argv', ['auth', 'login', 'left', 'user']), \
                 patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(b' exact '))):
                auth.main()
                login.assert_called_once_with('left', 'user', ' exact ')

    def test_logout_cli_preserves_expected_identity_and_rejects_partial_args(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(auth, 'SIDE_ROOT', Path(directory)), \
             patch.object(auth.os, 'geteuid', return_value=0), patch.object(auth, 'prepare_state_root'), patch.object(auth, 'logout_side') as logout:
            with patch.object(sys, 'argv', ['auth', 'logout', 'left', 'owner', 'a' * 32]):
                auth.main()
                logout.assert_called_once_with('left', 'owner', 'a' * 32)
            logout.reset_mock()
            for argv in (['auth', 'logout'], ['auth', 'logout', 'left', 'owner'],
                         ['auth', 'logout', 'left', 'owner', 'a' * 32, 'extra']):
                with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        auth.main()
            logout.assert_not_called()

    def test_actual_completion_changes_only_its_side(self):
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        function = source.split('/* === Authentication completion === */', 1)[1].split('static void auto_login_side', 1)[0]
        with tempfile.TemporaryDirectory(prefix='dosplex-auth-complete-') as directory:
            root = Path(directory)
            code = root / 'complete.c'
            code.write_text('''#define _GNU_SOURCE
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
#define SIDE_GREETER 0
#define SIDE_CLAIMED 1
struct compositor { int redraws, launches, clears; };
struct side_ctx { struct compositor *comp; bool auth_error, recovery_required, launcher_failed, launch_pending, auto_login_suppressed;
    char launch_token[33], supervisor_instance[33];
    int state, worker_pid; char owner[64], username[64], password[64]; };
static char launched[64];
static void request_redraw(struct compositor *c) { c->redraws++; }
static void clear_side_focus_on_lifecycle_change(struct compositor *c, struct side_ctx *s, const char *why) { c->clears++; }
static void launch_side_session(struct side_ctx *s, const char *username) {
    s->comp->launches++; snprintf(launched, sizeof(launched), "%s", username);
}
''' + function + '''
int main(void) {
    struct compositor c = {0};
    struct side_ctx left = {.comp=&c, .state=SIDE_CLAIMED, .worker_pid=42,
        .owner="original", .username="edited-after-start", .password="public-test"};
    struct side_ctx right = {.comp=&c, .state=SIDE_CLAIMED, .owner="other"};
    struct side_ctx before = right;
    auth_finished(&left, "logout", "original", false);
    assert(left.auth_error && left.state == SIDE_CLAIMED && left.worker_pid == 42);
    assert(!strcmp(left.owner, "original") && !c.launches && !c.clears);
    auth_finished(&left, "login", "snapshot", true);
    assert(!left.auth_error && c.launches == 1 && !strcmp(launched, "snapshot"));
    assert(left.state == SIDE_CLAIMED && !strcmp(left.owner, "snapshot") && !left.auto_login_suppressed);
    auth_finished(&left, "logout", "snapshot", true);
    assert(left.state == SIDE_GREETER && !left.worker_pid && !left.owner[0]);
    assert(!left.username[0] && !left.password[0] && c.clears == 1);
    assert(c.redraws == 3 && !memcmp(&right, &before, sizeof(right)));
    assert(left.auto_login_suppressed);
    auth_finished(&left, "auto-login", "snapshot", true);
    assert(!left.auto_login_suppressed && c.launches == 2);
}
''')
            subprocess.run(['cc', str(code), '-o', str(root/'complete')], check=True, capture_output=True, timeout=15)
            subprocess.run([str(root/'complete')], check=True, timeout=5)

    def test_automatic_claims_defer_other_side_until_admission_finishes(self):
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        function = 'static void auto_login_side' + source.split('static void auto_login_side', 1)[1].split('static int auto_login_timer_callback', 1)[0]
        with tempfile.TemporaryDirectory(prefix='dosplex-auto-admission-') as directory:
            root = Path(directory)
            code = root / 'admission.c'
            code.write_text(r'''#include <stdbool.h>
#include <assert.h>
#define SIDE_GREETER 0
#define wlr_log(...) ((void)0)
struct compositor;
struct side_ctx { struct compositor *comp; int state; bool recovery_required, auth_error, auto_login_suppressed; void *auth; };
struct compositor { struct side_ctx left, right; int requests; };
static bool call_auth_helper(struct side_ctx *ctx, const char *action, const char *user, const char *password) {
    ctx->comp->requests++; ctx->auth = ctx; return true;
}
static void request_redraw(struct compositor *comp) {}
''' + function + r'''
int main(void) {
    struct compositor c = {0}; c.left.comp = &c; c.right.comp = &c;
    auto_login_side(&c.left, "left-profile");
    auto_login_side(&c.right, "right-profile");
    assert(c.requests == 1 && c.left.auth && !c.right.auth && !c.right.auth_error);
    c.left.auth = 0; c.left.state = 1;
    auto_login_side(&c.left, "left-profile");
    auto_login_side(&c.right, "right-profile");
    assert(c.requests == 2 && c.right.auth && !c.right.auth_error);
    c.right.auth = 0; c.right.auth_error = true;
    auto_login_side(&c.right, "right-profile");
    assert(c.requests == 2); /* Genuine failures still require deliberate retry. */
    c.right.auth_error = false; c.right.auto_login_suppressed = true;
    auto_login_side(&c.right, "right-profile");
    assert(c.requests == 2); /* End Session must not immediately restart. */
}
''')
            subprocess.run(['cc', str(code), '-o', str(root / 'admission')], check=True, capture_output=True, timeout=15)
            subprocess.run([str(root / 'admission')], check=True, timeout=5)

    def test_real_c_call_uses_pipe_not_argv(self):
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        function = 'struct auth_job {' + source.split('struct auth_job {', 1)[1].split('/* === Authentication completion === */', 1)[0]
        with tempfile.TemporaryDirectory(prefix='dosplex-auth-test-') as directory:
            root = Path(directory)
            code = root / 'call.c'
            code.write_text('''#define _GNU_SOURCE
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <wayland-server-core.h>
#include <assert.h>
#include <sys/wait.h>
#include <signal.h>
#include <time.h>
#include <poll.h>
#define AUTH_HELPER_TIMEOUT_MS 700
#define wlr_log(...) ((void)0)
#define SIDE_LEFT 0
struct dosplex_config { bool allow_nested_sessions; };
struct compositor { struct dosplex_config config; struct wl_display *display; };
struct side_ctx { struct compositor *comp; int id; struct auth_job *auth; bool auth_error; char owner[64], supervisor_instance[33]; };
static int completions, success_count, ticks;
static bool right_finished_first;
static struct side_ctx *left_side;
static int heartbeat(void *data) {
    ticks++;
    wl_event_source_timer_update(*(struct wl_event_source **)data, 1);
    return 0;
}
static void auth_finished(struct side_ctx *ctx, const char *action, const char *username, bool success) {
    (void)action; (void)username;
    completions++;
    success_count += success;
    if (ctx->id == 1 && left_side->auth) right_finished_first = true;
}
static const char *auth_helper_path(void) { return getenv("DOSPLEX_TEST_HELPER"); }
/* Real definition: the extracted auth machinery calls this in every child
 * branch (T-102). The harness provides it exactly as the compositor does. */
static void spawn_child_reset(void) {
    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
    for (int sig = 1; sig < NSIG; sig++) {
        if (sig == SIGKILL || sig == SIGSTOP) continue;
        signal(sig, SIG_DFL);
    }
}
''' + function + '''
int main(int argc, char **argv) {
    struct compositor comp = {.config = {.allow_nested_sessions = true}, .display = wl_display_create()};
    struct side_ctx side = {.comp = &comp, .id = 0, .owner="test-user",
        .supervisor_instance="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"};
    struct side_ctx right = {.comp = &comp, .id = 1};
    left_side = &side;
    assert(!call_auth_helper(&right, "logout", NULL, NULL) && !right.auth && right.auth_error);
    char password[4097] = {0};
    fread(password, 1, sizeof(password)-1, stdin);
    struct wl_event_loop *loop = wl_display_get_event_loop(comp.display);
    struct wl_event_source *pulse = wl_event_loop_add_timer(loop, heartbeat, &pulse);
    wl_event_source_timer_update(pulse, 1);
    bool accepted = call_auth_helper(&side, argc > 1 ? argv[1] : "login", "test-user", password);
    assert(accepted && side.auth && !completions);
    assert(!call_auth_helper(&side, "login", "duplicate", password));
    const char *mode = getenv("DOSPLEX_TEST_MODE");
    bool parallel = mode && strcmp(mode, "parallel") == 0;
    bool cancel = mode && strcmp(mode, "cancel") == 0;
    if (parallel) assert(call_auth_helper(&right, "login", "test-user", password));
    if (cancel) {
        pid_t child = side.auth->pid;
        destroy_auth(&side);
        int status;
        waitpid(child, &status, 0);
        assert(!side.auth && !completions);
    } else {
        while (side.auth || right.auth) assert(wl_event_loop_dispatch(loop, 20) == 0);
        assert(completions == (parallel ? 2 : 1));
        if (parallel) assert(right_finished_first && ticks > 20);
        if (mode && (!strcmp(mode, "hang") || !strcmp(mode, "closed-output"))) assert(ticks > 20);
    }
    wl_event_source_remove(pulse);
    wl_display_destroy(comp.display);
    return cancel || success_count == (parallel ? 2 : 1) ? 0 : 1;
}
''')
            flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'wayland-server'], text=True).split()
            subprocess.run(['cc', '-Wall', '-Wextra', str(code), '-o', str(root / 'call'), *flags], check=True, capture_output=True, timeout=15)
            spy = root / 'helper'
            spy.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys,time,signal
mode = os.environ.get('DOSPLEX_TEST_MODE', '')
pathlib.Path(os.environ['DOSPLEX_TEST_PID']).write_text(str(os.getpid()))
if mode in ('hang', 'cancel'): time.sleep(60)
if mode == 'parallel' and sys.argv[2] == 'left': time.sleep(.35)
if mode == 'closed-output':
    os.close(1); os.close(2); time.sleep(60)
if mode == 'flood':
    while True: os.write(1, b'x' * 4096)
if mode == 'descendant':
    if os.fork() == 0:
        time.sleep(1.2)
        pathlib.Path(os.environ['DOSPLEX_TEST_LEAK']).write_text('survived')
        os._exit(0)
    os._exit(0)
if mode == 'signal': os.kill(os.getpid(), signal.SIGTERM)
if mode == 'verbose': os.write(1, b'x' * 32768)
pathlib.Path(os.environ['DOSPLEX_TEST_RECORD']).write_text(json.dumps({'argv':sys.argv[1:], 'stdin':sys.stdin.read()}))
print('{"ok": true}')
raise SystemExit(int(os.environ.get('DOSPLEX_TEST_EXIT', '0')))
''')
            spy.chmod(0o700)
            sudo = root / 'sudo'
            sudo.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys
pathlib.Path(os.environ['DOSPLEX_TEST_SUDO']).write_text(json.dumps(sys.argv[1:]))
assert sys.argv[1] == '-n'
os.execv(sys.argv[2], sys.argv[2:])
''')
            sudo.chmod(0o700)
            env = dict(os.environ, PATH=str(root)+':'+os.defpath, DOSPLEX_TEST_HELPER=str(spy),
                       DOSPLEX_TEST_LEAK=str(root/'leaked'), DOSPLEX_TEST_PID=str(root/'pid'), DOSPLEX_TEST_RECORD=str(root/'record'), DOSPLEX_TEST_SUDO=str(root/'sudo-args'))
            password = ' public-test-ü\n'
            for action, expected in [('login', password), ('auto-login', ''), ('logout', '')]:
                result = subprocess.run([str(root/'call'), action], input=password, text=True,
                                        capture_output=True, env=env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads((root/'record').read_text())
                expected_argv = [action, 'left', 'test-user'] + (['a' * 32] if action == 'logout' else [])
                self.assertEqual(record, {'argv': expected_argv, 'stdin': expected})
                self.assertNotIn(password, result.stdout + result.stderr)
                if os.geteuid() != 0:
                    self.assertNotIn(password, (root/'sudo-args').read_text())
            result = subprocess.run([str(root/'call')], input=password, text=True, capture_output=True,
                                    env=dict(env, DOSPLEX_TEST_EXIT='1'), timeout=5)
            self.assertEqual(result.returncode, 1)
            for mode, expected in [('verbose', 0), ('hang', 1), ('closed-output', 1),
                                   ('flood', 1), ('signal', 1), ('descendant', 1), ('parallel', 0), ('cancel', 0)]:
                with self.subTest(mode=mode):
                    started = time.monotonic()
                    result = subprocess.run([str(root/'call')], input=password, text=True,
                                            capture_output=True, env=dict(env, DOSPLEX_TEST_MODE=mode), timeout=5)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(result.stdout + result.stderr, '')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root/'pid').read_text()), 0)
            # A forked child that inherits stdout must not outlive cancellation.
            time.sleep(0.6)
            self.assertFalse((root/'leaked').exists())
            result = subprocess.run([str(root/'call')], input=password, text=True, capture_output=True,
                                    env=dict(env, DOSPLEX_TEST_HELPER=str(root/'missing')), timeout=5)
            self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
