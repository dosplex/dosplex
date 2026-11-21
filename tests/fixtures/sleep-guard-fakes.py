# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Stand-ins for the programs the sleep guard runs: gdbus, systemd-inhibit and, for some tests, systemctl.

Not a test. sleep-guard-test.py and sleep-guard-integration.py put the scripts into a directory at the front of PATH.
Every script appends what it does to $FAKE_LOG, one line each, with the monotonic clock in nanoseconds in front, so that
the order of events across processes is what the file says.

  gdbus           `gdbus monitor` (the default) prints what the real one prints on its first lines, then each line
                  that is appended to $FAKE_SIGNALS after it started. The line EXIT ends it. It also ends when its
                  parent does. `gdbus call` answers the one question the guard asks, logind's InhibitDelayMaxUSec
                  (log line gdbus-call): $FAKE_DELAY_MAX is the answer in microseconds, 5000000 if unset, or "fail"
                  (an error exit), "hang" (no answer) or "garbage" (an answer that is not a number).
  systemd-inhibit takes a "lock" (log line lock-taken), runs the command it is given like the real one, and logs
                  lock-released when that command ends. $FAKE_INHIBIT_FAIL makes it fail like a refused lock.
                  $FAKE_INHIBIT_DELAY is the seconds it waits after the log line and before it runs the command, as
                  a machine does that is too busy to start the shell at once. The guard knows that it has the lock
                  only after that, so a test that goes by the log line is early every time, not only on a busy machine.
  systemctl       keeps a table of units in the JSON file $FAKE_STATE and answers list-units, show, freeze, thaw
                  and list-jobs. A unit's "freeze" entry says what freezing it does: ok (the default), fail, hang
                  (the command does not return and nothing changes) or freezing (the unit stays in the freezing
                  state); "thaw_failures" is how many thaw commands fail before one works. The "jobs" list of the
                  file holds the names of the units that have a job, each a start job that is running (log line
                  list-jobs for every question).
"""
from pathlib import Path

GDBUS = r'''#!/usr/bin/env python3
import os, sys, time
if sys.argv[1:2] == ['call']:
    arguments, options, values = sys.argv[2:], {}, []
    while arguments:
        argument = arguments.pop(0)
        if argument in ('--dest', '--object-path', '--method', '--timeout'):
            options[argument] = arguments.pop(0) if arguments else ''
        elif argument.startswith('-'):
            options[argument] = ''
        else:
            values.append(argument)
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write('%d gdbus-call %s\n' % (time.monotonic_ns(), ' '.join(sys.argv[2:])))
    asked = (options.get('--dest'), options.get('--object-path'), options.get('--method'), values)
    if '--system' not in options or asked != ('org.freedesktop.login1', '/org/freedesktop/login1',
            'org.freedesktop.DBus.Properties.Get', ['org.freedesktop.login1.Manager', 'InhibitDelayMaxUSec']):
        print('Error: GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: the fake gdbus answers only the '
              'question about InhibitDelayMaxUSec, not ' + ' '.join(sys.argv[2:]), file=sys.stderr)
        sys.exit(1)
    answer = os.environ.get('FAKE_DELAY_MAX', '5000000')
    if answer == 'fail':
        print('Error: GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name org.freedesktop.login1 was '
              'not provided by any .service files', file=sys.stderr)
        sys.exit(1)
    if answer == 'hang':
        time.sleep(60)
    print('(<string "soon">,)' if answer == 'garbage' else '(<uint64 %s>,)' % answer)
    sys.exit(0)
signals = os.environ['FAKE_SIGNALS']
parent = os.getppid()
print('Monitoring signals on object /org/freedesktop/login1 owned by org.freedesktop.login1', flush=True)
print('The name org.freedesktop.login1 is owned by :1.5', flush=True)
try:
    offset = os.path.getsize(signals)
except FileNotFoundError:
    offset = 0
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write('%d gdbus-started\n' % time.monotonic_ns())
while os.getppid() == parent:
    try:
        with open(signals, 'rb') as stream:
            stream.seek(offset)
            data = stream.read()
    except FileNotFoundError:
        data = b''
    complete, newline, _ = data.rpartition(b'\n')
    if newline:
        offset += len(complete) + 1
        for line in complete.decode().split('\n'):
            if line == 'EXIT':
                sys.exit(0)
            print(line, flush=True)
    time.sleep(0.01)
'''

INHIBIT = r'''#!/usr/bin/env python3
import os, subprocess, sys, time
arguments = sys.argv[1:]
count = 0
while count < len(arguments) and arguments[count].startswith('--'):
    count += 1
command = arguments[count:]
def note(text):
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write('%d %s\n' % (time.monotonic_ns(), text))
if os.environ.get('FAKE_INHIBIT_FAIL'):
    print('Failed to inhibit: Access denied', file=sys.stderr)
    sys.exit(1)
note('lock-taken ' + ' '.join(arguments[:count]))
time.sleep(float(os.environ.get('FAKE_INHIBIT_DELAY') or 0))
status = subprocess.call(command)
note('lock-released')
sys.exit(status)
'''

SYSTEMCTL = r'''#!/usr/bin/env python3
import fcntl, fnmatch, json, os, sys, time
arguments = [a for a in sys.argv[1:] if a != '--user']
verb = arguments[0]
state_path = os.environ['FAKE_STATE']
def note(text):
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write('%d %s\n' % (time.monotonic_ns(), text))
def transact(change):
    with open(state_path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(state_path) as stream:
            state = json.load(stream)
        result = change(state)
        with open(state_path + '.new', 'w') as stream:
            json.dump(state, stream)
        os.replace(state_path + '.new', state_path)
        return result
if verb == 'list-units':
    patterns = [a for a in arguments[1:] if not a.startswith('-')]
    state = transact(lambda state: state)
    for name, unit in sorted(state['units'].items()):
        if unit['active'] == 'active' and any(fnmatch.fnmatch(name, p) for p in patterns):
            print('%s loaded active running fake side' % name)
elif verb == 'show':
    names = []
    skip = False
    for argument in arguments[1:]:
        if skip:
            skip = False
        elif argument == '-p':
            skip = True
        elif not argument.startswith('-'):
            names.append(argument)
    state = transact(lambda state: state)
    blocks = []
    for name in names:
        unit = state['units'].get(name)
        blocks.append('Id=%s\nLoadState=%s\nActiveState=%s\nFreezerState=%s' % (
            name, unit['load'] if unit else 'not-found', unit['active'] if unit else 'inactive',
            unit['freezer'] if unit else 'running'))
    print('\n\n'.join(blocks))
elif verb == 'list-jobs':
    state = transact(lambda state: state)
    note('list-jobs')
    for number, name in enumerate(state.get('jobs', []), 1):
        print('%d %s start running' % (1000 + number, name))
elif verb == 'freeze':
    name = arguments[1]
    def freeze(state):
        unit = state['units'].get(name)
        if unit is None:
            return 'missing'
        mode = unit.get('freeze', 'ok')
        if mode == 'ok':
            unit['freezer'] = 'frozen'
        elif mode == 'freezing':
            unit['freezer'] = 'freezing'
        return mode
    mode = transact(freeze)
    note('freeze %s %s' % (name, mode))
    if mode == 'missing':
        print('Failed to freeze unit: Unit %s not found.' % name, file=sys.stderr)
        sys.exit(1)
    if mode == 'fail':
        print('Failed to freeze unit %s: Connection timed out' % name, file=sys.stderr)
        sys.exit(1)
    if mode == 'hang':
        time.sleep(60)
elif verb == 'thaw':
    name = arguments[1]
    def thaw(state):
        unit = state['units'].get(name)
        if unit is None:
            return 'missing'
        if unit.get('thaw_failures', 0) > 0:
            unit['thaw_failures'] -= 1
            return 'fail'
        unit['freezer'] = 'running'
        return 'ok'
    result = transact(thaw)
    note('thaw %s %s' % (name, result))
    if result != 'ok':
        print('Failed to thaw unit %s: %s' % (name, result), file=sys.stderr)
        sys.exit(1)
else:
    print('fake systemctl: unexpected verb ' + verb, file=sys.stderr)
    sys.exit(2)
'''

SCRIPTS = {'gdbus': GDBUS, 'systemd-inhibit': INHIBIT, 'systemctl': SYSTEMCTL}


def guard_listens(process, said, locks=1):
    """Whether the guard (a subprocess.Popen) has taken the sleep delay lock `locks` times, as its journal (`said`, the
    text) tells, and now waits for logind: it is asleep in select() on the monitor, which /proc shows as the state S.

    The log line lock-taken is no sign of that. It comes before the guard knows that it has the lock, and an
    announcement sent then reaches the guard while it takes the lock. All the guard can tell of when that one came is
    that it was after the guard last looked, before the lock: it counts the waits from there, and the settle is cut by
    the time the lock took. The guard is right to do that, as logind counts from the announcement, and a test that
    times the settle must not provoke it. Nor is the line in the journal enough: a process that has just written it
    still runs, and one that sleeps in a system call after it waits for logind. Linux only."""
    if said.count('took the sleep delay lock') != locks:
        return False
    try:
        with open('/proc/%d/stat' % process.pid) as stat:
            return stat.read().rpartition(')')[2].split()[0] == 'S'
    except (OSError, IndexError):
        return False


def install(directory, names=('gdbus', 'systemd-inhibit', 'systemctl')):
    """Write the scripts named into `directory` (executable); returns the directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = directory / name
        path.write_text(SCRIPTS[name])
        path.chmod(0o755)
    return directory
