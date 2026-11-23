#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Actual compositor launcher and child monitoring with disposable executables."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_launch_arguments_exit_and_nonblocking_owned_reaping(self):
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        functions = 'static bool monitor_session_launcher(' + source.split('static bool monitor_session_launcher(', 1)[1].split('/* === Session state decoding === */', 1)[0]
        with tempfile.TemporaryDirectory(prefix='dosplex-launcher-test-') as directory:
            root = Path(directory)
            code = root / 'launcher.c'
            code.write_text('''#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/wait.h>
#include <sys/random.h>
#include <time.h>
#define SESSION_START_TIMEOUT_MS 30000
#define SIDE_LEFT 0
#define wlr_log(...) ((void)0)
struct side_ctx {
    int id, role, width, height; pid_t launcher_pid, worker_pid;
    bool launcher_failed, recovery_required, auth_error, launch_pending;
    char launch_token[33], supervisor_instance[33]; struct timespec launch_started;
    void *auth; char owner[64];
    struct {char description[64]; int scale_milli, logical_x, logical_y;} logical_output;
    struct {char *display_name;} *xwayland;
};
static const char *side_role_name(int role) { return "primary"; }
static const char *side_output_name(struct side_ctx *s) { return "dosplex-left"; }
static int side_logical_width(struct side_ctx *s) { return 960; }
static int side_logical_height(struct side_ctx *s) { return 1080; }
static int side_physical_origin_x(struct side_ctx *s) { return 0; }
static int side_physical_origin_y(struct side_ctx *s) { return 0; }
static const char *session_script_path(void) { return getenv("DOSPLEX_TEST_SCRIPT"); }
/* Real definition: launch_side_session calls this in its child branch (T-102),
 * and the harness provides it exactly as the compositor does. */
static void spawn_child_reset(void) {
    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
    for (int sig = 1; sig < NSIG; sig++) {
        if (sig == SIGKILL || sig == SIGSTOP) continue;
        signal(sig, SIG_DFL);
    }
}
''' + functions + '''
static void ready(pid_t pid) {
    siginfo_t info;
    assert(waitid(P_PID, pid, &info, WEXITED | WNOWAIT) == 0);
}
int main(void) {
    struct side_ctx side = {.owner="test-user"};
    launch_side_session(&side, "literal user $HOME");
    pid_t launched = side.launcher_pid;
    assert(launched > 0 && side.worker_pid == launched && !side.launcher_failed && side.launch_pending);
    assert(strlen(side.launch_token) == 32);
    ready(launched);
    // JSON polling can replace worker_pid; it must not replace child identity.
    side.worker_pid = getpid();
    assert(monitor_session_launcher(&side));
    assert(!side.launcher_pid && side.launcher_failed && side.recovery_required && side.auth_error);
    assert(waitpid(launched, NULL, WNOHANG) == -1 && errno == ECHILD);

    // A live child blocks on a pipe. Monitoring must return before we release it.
    int gate[2]; assert(pipe(gate) == 0);
    pid_t live = fork(); assert(live >= 0);
    if (!live) { close(gate[1]); char byte; read(gate[0], &byte, 1); _exit(0); }
    close(gate[0]);
    side.launcher_pid = live;
    assert(!monitor_session_launcher(&side));
    assert(side.launcher_pid == live && kill(live, 0) == 0);
    // An exited second side must not reap or signal the live first side.
    struct side_ctx other = {.owner="other"};
    other.launcher_pid = fork(); assert(other.launcher_pid >= 0);
    if (!other.launcher_pid) _exit(0);
    ready(other.launcher_pid);
    assert(monitor_session_launcher(&other) && other.launcher_failed);
    assert(kill(live, 0) == 0);
    close(gate[1]); ready(live);
    side.owner[0] = 0; side.launcher_failed = false;
    assert(monitor_session_launcher(&side) && !side.launcher_failed);
    assert(!monitor_session_launcher(&side));
    assert(pipe(gate) == 0);
    side.launcher_pid = fork(); assert(side.launcher_pid >= 0);
    if (!side.launcher_pid) { close(gate[1]); char byte; read(gate[0], &byte, 1); _exit(0); }
    close(gate[0]);
    strcpy(side.owner, "test-user");
    side.launch_pending = true;
    side.launch_started = (struct timespec){0};
    assert(monitor_session_launcher(&side));
    assert(side.launcher_failed && !side.launch_pending && side.recovery_required);
    close(gate[1]);
    if (side.launcher_pid > 0) { ready(side.launcher_pid); monitor_session_launcher(&side); }
}
''')
            subprocess.run(['cc', str(code), '-o', str(root/'launcher')], check=True, capture_output=True, timeout=15)
            script = root / 'session'
            script.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys
pathlib.Path(os.environ['DOSPLEX_TEST_RECORD']).write_text(json.dumps({
    'argv':sys.argv[1:], 'width':os.environ['DOSPLEX_OUTPUT_WIDTH'],
    'role':os.environ['DOSPLEX_SIDE_ROLE'], 'token':os.environ['DOSPLEX_LAUNCH_TOKEN']}))
raise SystemExit(7)
''')
            script.chmod(0o700)
            sudo = root / 'sudo'
            sudo.write_text('''#!/usr/bin/python3
import os,sys
assert sys.argv[1] == '-n'
args = sys.argv[2:]
while '=' in args[0]:
    key,value = args.pop(0).split('=',1)
    os.environ[key] = value
os.execv(args[0], args)
''')
            sudo.chmod(0o700)
            env = dict(os.environ, PATH=str(root)+':'+os.defpath,
                       DOSPLEX_TEST_SCRIPT=str(script), DOSPLEX_TEST_RECORD=str(root/'record'))
            subprocess.run([str(root/'launcher')], env=env, check=True, capture_output=True, timeout=5)
            record = json.loads((root/'record').read_text())
            self.assertRegex(record.pop('token'), r'^[0-9a-f]{32}$')
            self.assertEqual(record,
                             dict(argv=['left', 'literal user $HOME'], width='960', role='primary'))


if __name__ == '__main__':
    unittest.main()
