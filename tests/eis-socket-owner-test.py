#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Execute the compositor's eis_open_owned(): a side's input socket is used only when the side's account listens.

The root compositor finds a nested Gamescope's EIS socket through the client's own environment (CMP-B-01). That
environment is written by the client's account, so a Guest program could name Main's Gamescope. eis_open_owned()
connects and then asks the kernel who listens on the socket it reached. Checked here with the function taken out
of the compositor's source: a socket this account listens on is taken, the same socket is refused when another
account is expected, a path that is no socket and a missing path are refused, a link to a socket is judged by who
listens behind it, and a listener that does not take connections never blocks the caller. It does not show two
accounts on one machine (the test has one); the kernel's answer is the account's uid in either case."""
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'compositor/dosplex-compositor.c').read_text()
match = re.search(r'^static int __attribute__\(\(unused\)\) eis_open_owned\([^{]*\{', source, re.M)
assert match, 'eis_open_owned not found'
function = source[match.start():source.index('\n}', match.start()) + 2]
# The one caller hands it the uid of the Wayland client, not a constant and not the compositor's own.
caller = source[source.index('static struct eis_input *eis_connect('):]
caller = caller[:caller.index('\n}\n')]
assert 'wl_client_get_credentials(wl_resource_get_client(surface->resource), &pid, &uid, &gid);' in caller
assert 'eis_open_owned(eis->path, uid)' in caller
assert 'ei_setup_backend_socket' not in source, 'a path is connected without asking who listens there'

HARNESS = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

%s

static int listener(const char *path, int backlog) {
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    strcpy(addr.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(fd, backlog) != 0) return -1;
    return fd;
}

static int check(const char *what, int ok) {
    printf("%%s %%s\n", ok ? "ok  " : "FAIL", what);
    return ok ? 0 : 1;
}

int main(int argc, char **argv) {
    char path[256], other[256];
    int bad = 0;
    uid_t me = geteuid();
    if (argc != 2 || chdir(argv[1]) != 0) return 2;

    snprintf(path, sizeof(path), "%%s/mine-ei", argv[1]);
    int server = listener(path, 8);
    if (server < 0) return 2;
    int fd = eis_open_owned(path, me);
    bad += check("a socket this account listens on is taken", fd >= 0);
    if (fd >= 0) close(fd);
    bad += check("the same socket is refused when another account is expected", eis_open_owned(path, me + 1) < 0);
    bad += check("and when root is expected", me == 0 || eis_open_owned(path, 0) < 0);

    snprintf(other, sizeof(other), "%%s/link-ei", argv[1]);
    if (symlink(path, other) != 0) return 2;
    fd = eis_open_owned(other, me);
    bad += check("a link is judged by who listens behind it: taken for the listener's account", fd >= 0);
    if (fd >= 0) close(fd);
    bad += check("a link is judged by who listens behind it: refused for another", eis_open_owned(other, me + 1) < 0);

    snprintf(other, sizeof(other), "%%s/file-ei", argv[1]);
    FILE *file = fopen(other, "w");
    if (file) fclose(file);
    bad += check("a path that is no socket is refused", eis_open_owned(other, me) < 0);
    snprintf(other, sizeof(other), "%%s/none-ei", argv[1]);
    bad += check("a missing path is refused", eis_open_owned(other, me) < 0);
    bad += check("no path is refused", eis_open_owned(NULL, me) < 0);
    char longpath[400];
    memset(longpath, 'a', sizeof(longpath) - 1);
    longpath[sizeof(longpath) - 1] = 0;
    bad += check("a path too long for a socket is refused", eis_open_owned(longpath, me) < 0);

    /* A listener that never takes its connections: fill what it queues, then the next connect must come back at
     * once and not hold the compositor's event loop. */
    snprintf(other, sizeof(other), "%%s/stuck-ei", argv[1]);
    int stuck = listener(other, 0);
    if (stuck < 0) return 2;
    int held[64], count = 0;
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);
    for (; count < 64; count++) {
        held[count] = eis_open_owned(other, me);
        if (held[count] < 0) break;
    }
    clock_gettime(CLOCK_MONOTONIC, &end);
    double seconds = (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
    bad += check("a listener that takes no connections is given up on", count < 64);
    bad += check("without waiting", seconds < 1.0);
    for (int i = 0; i < count; i++) close(held[i]);
    close(stuck);
    close(server);
    return bad ? 1 : 0;
}
'''

with tempfile.TemporaryDirectory(prefix='dosplex-eis-owner-') as name:
    work = Path(name)
    (work / 'harness.c').write_text(HARNESS % function)
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-o', str(work / 'harness'), str(work / 'harness.c')],
                   check=True, timeout=30)
    sockets = work / 's'
    sockets.mkdir()
    done = subprocess.run([str(work / 'harness'), str(sockets)], capture_output=True, text=True, timeout=20)
    print(done.stdout, end='')
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count('ok  ') == 11, done.stdout
print('PASS: eis_open_owned takes only a socket its side\'s account listens on')
