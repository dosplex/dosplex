#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Freeze both sides before the machine sleeps, and thaw them when it wakes.

A side's Steam polls the controller nodes that DosPlex's native relays provide inside the side's sandbox. A relay is a
CUSE server: the kernel hands every poll of its node to the relay and the polling thread waits for the answer where
the freezer cannot reach it. When the machine suspends, the kernel freezes user space in no particular order. A relay
that is frozen before it has answered a pending poll leaves that Steam thread stuck in fuse_file_poll: the thread can
never freeze, the kernel gives up after 20 seconds ("tasks refusing to freeze") and the suspend fails.

So the clients stop first, while the relays still run and answer. This guard keeps a logind delay lock on sleep. When
logind announces a sleep (PrepareForSleep(true)) it waits --settle-seconds (1.5 by default) with the lock still held,
then freezes the unit of each side that runs (systemctl freeze), waits until they are frozen, and lets the sleep go on
by releasing the lock. If the sleep is called off during the settle, nothing is frozen: logind announces the wake, or
it leaves the bus (a logind that restarts has forgotten the sleep it announced, and no wake will ever come for it).
When the machine wakes (PrepareForSleep(false)) it thaws exactly the units it froze and takes the lock again. A logind
that leaves the bus after the sides were frozen has forgotten the sleep just as well, and announces no wake: the guard
asks systemd for its jobs every second and, once there is no job to sleep (logind starts it when the delay locks are
gone, and it ends when the machine is awake), handles the wake as if it had been announced.

The settle is there so that a side's Steam can take the announcement itself. On the Steam Deck (2026-10-24) the guard
froze Main's side within milliseconds of PrepareForSleep(true): Steam logged "Suspending" at the announcement but ran
OnSystemPowerStateSuspend only after the wake, in the same second as OnSystemPowerStateResume, and on 2 of 5 wakes the
client crashed in steamclient.so between the two. On the same Deck without DosPlex (stock SteamOS, same client) the
suspend is handled at suspend time and 0 of 5 wakes crashed. 1.5 seconds is what another project waits before it
freezes the same client; how little would do is not measured.

Whatever happens, the lock is released within a little more than the settle plus --freeze-seconds of the announcement,
so a stuck guard never blocks a sleep. logind stops waiting for a delay lock after InhibitDelayMaxSec (5 seconds by
default), counted from the announcement, and so are the guard's waits, not from when it gets to read the announcement:
one that waited unread while the guard was busy (a thaw that is retried) counts from when the guard last looked at the
monitor, and the settle and the freeze cap run out that much sooner. A thaw that runs with the lock held is given
2 seconds for that reason, and the idle passes retry what is left. The guard reads InhibitDelayMaxSec once at start,
from the InhibitDelayMaxUSec property of org.freedesktop.login1.Manager (5 seconds if it cannot), and cuts the two
waits so that both and a margin of 0.5 seconds fit in it: 1.5 + 3 + 0.5 = 5 with the defaults. The margin is for what
comes after the freeze cap: one last read of the units' states (0.2 seconds at most) and the release of the lock.
Where logind waits less, the freeze cap is cut first, down to 1 second (a freeze takes a fraction of that), then the
settle, and only then the freeze cap again (never below 0.2 seconds). The values used go to the journal once at start.
With no lock held logind does not wait for the guard, so there is no settle then and the sides are frozen at once.

A side that is frozen already (Guest's standby) is left alone, then and after the wake: the guard thaws only what it
froze or asked to freeze. It cannot tell who froze a unit, so a side that the supervisor puts in standby after the
guard froze it, and before the wake, is thawed by the wake. The units it froze are also written to a small
state file (STATE_PATH, under /run), so that a guard that starts again (it is restarted when it fails) thaws what
the one before it froze and never got to thaw, and only that: a side in standby is not in the file. It runs as root next to the compositor, under a unit that
is bound to it. Everything it does goes to the journal, one line each: "waiting N s for the sides to take the sleep
announcement", "froze UNIT", "thawed UNIT", "released the sleep delay lock".

    dosplex-sleep-guard.py [--manager system|user] [--settle-seconds N] [--freeze-seconds N] [--thaw-seconds N]

--settle-seconds 0 freezes at once, as the guard did before it had a settle. --manager user watches a user manager
instead of the system manager, so that the tests can run the guard on disposable units without root.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

LOGIND = 'org.freedesktop.login1'
# The units of the sides: one for each of Main and Guest, named by the supervisor that runs them.
SIDE_UNIT = re.compile(r'dosplex-session-(?:left|right)-[0-9a-f]{32}\.service')
SIDE_PATTERNS = ('dosplex-session-left-*.service', 'dosplex-session-right-*.service')
# The jobs that systemd has while the system goes to sleep, from the moment logind starts the sleep until it is awake.
SLEEP_JOB = re.compile(r'(?:sleep|suspend|hibernate|hybrid-sleep|suspend-then-hibernate)\.target'
                       r'|systemd-(?:suspend|hibernate|hybrid-sleep|suspend-then-hibernate)\.service')
# What the guard froze, for a guard that starts again. /run is empty after a boot, when no unit is frozen either.
STATE_PATH = '/run/dosplex-sleep-guard.json'
LOCK_WHO = 'DosPlex'
LOCK_WHY = 'Freeze both sides before the machine sleeps'
# Held by a shell that says so once logind has the lock, then by cat, which ends when its input closes.
LOCK_COMMAND = ['systemd-inhibit', '--what=sleep', '--mode=delay', '--who=' + LOCK_WHO, '--why=' + LOCK_WHY,
                'sh', '-c', 'echo held; exec cat']
MONITOR_COMMAND = ['gdbus', 'monitor', '--system', '--dest', LOGIND, '--object-path', '/org/freedesktop/login1']
# logind waits for a delay lock at most InhibitDelayMaxSec (5 s by default). The guard reads it once at start (a
# property of the Manager object, asked with gdbus) and keeps the settle and the freeze cap, and a margin, below it.
DELAY_MAX_COMMAND = ['gdbus', 'call', '--system', '--dest', LOGIND, '--object-path', '/org/freedesktop/login1',
                     '--method', 'org.freedesktop.DBus.Properties.Get', 'org.freedesktop.login1.Manager',
                     'InhibitDelayMaxUSec']
DELAY_MAX_TIMEOUT = 2.0
DELAY_MAX_FALLBACK_SECONDS = 5.0     # used when logind does not say
DELAY_MARGIN_SECONDS = 0.5           # settle + freeze cap + this fit in what logind waits
SETTLE_SECONDS = 1.5                 # after PrepareForSleep(true), with the lock held, before the sides are frozen
MAX_SETTLE_SECONDS = 3.5
FREEZE_SECONDS = 3.0
MIN_FREEZE_SECONDS = 0.2
MAX_FREEZE_SECONDS = 3.5
FREEZE_FLOOR_SECONDS = 1.0           # what the freeze cap keeps, as long as it can, when logind waits less
THAW_SECONDS = 10.0
THAW_RETRY_SECONDS = 1.0
THAW_HELD_SECONDS = 2.0  # what a thaw is given with the lock held, while the guard reads nothing (see thaw_budget)
LOCK_RETRY_SECONDS = 5.0
TICK_SECONDS = 1.0       # the pace of the loop while there is something to retry
IDLE_SECONDS = 30.0      # and while there is not
MONITOR_RETRY_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
LATE_SECONDS = 0.1       # an announcement that waited unread for less than this is counted from when it was read
# What calls off a sleep that is announced and has not begun, as (what the journal says, and what it adds). logind keeps
# a pending sleep in memory only: one that restarts has forgotten it, and no wake will ever come for it. It leaves the
# bus first, also when another takes its name over (gdbus monitor says so before it names the new owner). Its arrival
# alone says nothing of the kind: a monitor that was slow to start says it late.
CALLED_OFF_BY = {('prepare', False): ('logind announced the wake', ''),
                 ('logind', False): ('logind left the bus', ', which calls the sleep off')}


def log(message):
    try:
        print('dosplex-sleep-guard: ' + message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass   # a journal that cannot be written to must not stop the guard


def run_command(argv, timeout):
    """Run argv and wait at most `timeout` seconds. Returns (exit status, stdout, stderr). The status is None, with
    the reason in stderr, when the command could not start or had not finished in time (it is killed then)."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors='replace')
    except OSError as error:
        return None, '', 'could not start %s: %s' % (argv[0], error.strerror or error)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None, '', '%s timed out after %.1f s' % (argv[0], timeout)
    return process.returncode, out, err


def parse_event(line):
    """What a line of `gdbus monitor` says about logind: ('prepare', True|False) for PrepareForSleep, ('logind',
    True|False) when logind appears on or leaves the bus, None for anything else."""
    found = re.search(r'\.PrepareForSleep \((true|false),?\)', line)
    if found:
        return 'prepare', found.group(1) == 'true'
    if 'The name ' + LOGIND + ' is owned by' in line:
        return 'logind', True
    if 'The name ' + LOGIND + ' does not have an owner' in line:
        return 'logind', False
    return None


def read_delay_max(command=DELAY_MAX_COMMAND, timeout=DELAY_MAX_TIMEOUT):
    """What logind waits for a delay lock, from its InhibitDelayMaxUSec property: (seconds, '') or (None, why not).
    `gdbus call` prints the property as "(<uint64 5000000>,)"."""
    status, out, err = run_command(command, timeout)
    if status != 0:
        return None, ' '.join((err or out).split())[:200] or 'exit status %s' % status
    found = re.fullmatch(r'\(<uint64 (\d+)>,?\)', out.strip())
    if not found:
        return None, 'unexpected answer: ' + (' '.join(out.split())[:80] or 'none')
    return int(found.group(1)) / 1e6, ''


def fit_waits(settle, freeze, delay_max):
    """The settle and the freeze cap to use when logind waits `delay_max` seconds for a delay lock: both and the margin
    fit in it. The freeze is the job and the settle a courtesy, but a freeze takes a fraction of a second, so the
    freeze cap is cut first, no further than FREEZE_FLOOR_SECONDS, then the settle, and only when that is not enough
    the freeze cap again, never below MIN_FREEZE_SECONDS."""
    room = delay_max - DELAY_MARGIN_SECONDS
    settle = max(0.0, min(settle, room - min(freeze, FREEZE_FLOOR_SECONDS)))
    freeze = max(MIN_FREEZE_SECONDS, min(freeze, room - settle))
    return settle, freeze


def choose_waits(settle, freeze):
    """The settle and the freeze cap to use: those asked for, cut to fit what logind waits for a delay lock. It reads
    that from logind (once, at start) and says what it found and what it cut. Returns (settle, freeze, delay_max)."""
    delay_max, why_not = read_delay_max()
    if delay_max is None:
        log('could not read InhibitDelayMaxUSec from logind (%s); assuming %g s'
            % (why_not, DELAY_MAX_FALLBACK_SECONDS))
        delay_max = DELAY_MAX_FALLBACK_SECONDS
    fitted_settle, fitted_freeze = fit_waits(settle, freeze, delay_max)
    cuts = []
    if fitted_settle < settle:
        cuts.append('the settle from %g s to %g s' % (settle, fitted_settle))
    if fitted_freeze < freeze:
        cuts.append('the freeze cap from %g s to %g s' % (freeze, fitted_freeze))
    if cuts:
        log('logind waits %g s for a delay lock: cutting %s' % (delay_max, ' and '.join(cuts)))
    if fitted_settle + fitted_freeze + DELAY_MARGIN_SECONDS > delay_max + 1e-9:
        log('logind waits only %g s for a delay lock: less than the freeze cap and a margin of %g s need, so the sleep '
            'may go on before the sides are frozen' % (delay_max, DELAY_MARGIN_SECONDS))
    return fitted_settle, fitted_freeze, delay_max


class SystemdError(Exception):
    pass


class Outcome:
    """What became of one unit that was asked to freeze: its freezer state as last read (None if it could not be
    read) and the error text of the systemctl command ('' when it succeeded)."""
    def __init__(self, state=None, error=''):
        self.state, self.error = state, error


class Systemd:
    """The manager that runs the sides, as far as the guard needs it."""

    def __init__(self, manager='system', clock=time.monotonic, sleep=time.sleep):
        self.prefix = ['systemctl'] + (['--user'] if manager == 'user' else [])
        self.clock, self.sleep = clock, sleep

    def side_units(self, timeout=2.0):
        """The units of the sides that are active now, sorted."""
        status, out, err = run_command([*self.prefix, 'list-units', '--plain', '--no-legend', '--no-pager',
                                        '--state=active', *SIDE_PATTERNS], timeout)
        if status != 0:
            raise SystemdError('systemctl list-units: ' + (err.strip() or 'exit status %s' % status))
        found = (line.split() for line in out.splitlines())
        return sorted({fields[0] for fields in found if fields and SIDE_UNIT.fullmatch(fields[0])})

    def sleep_jobs(self, timeout=2.0):
        """The units with a job to sleep that systemd has now, queued or running, sorted. Empty when the system is
        awake: logind starts the job when the delay locks are gone, and it ends when the machine has woken up."""
        status, out, err = run_command([*self.prefix, 'list-jobs', '--no-legend', '--no-pager'], timeout)
        if status != 0:
            raise SystemdError('systemctl list-jobs: ' + (err.strip() or 'exit status %s' % status))
        found = (line.split() for line in out.splitlines())
        return sorted({fields[1] for fields in found if len(fields) > 1 and SLEEP_JOB.fullmatch(fields[1])})

    def states(self, units, timeout=1.0):
        """{unit: {'load', 'active', 'freezer'}} as systemd says now. A unit it does not know is loaded as
        not-found."""
        if not units:
            return {}
        status, out, err = run_command([*self.prefix, 'show', '-p', 'Id', '-p', 'LoadState', '-p', 'ActiveState',
                                        '-p', 'FreezerState', *units], timeout)
        if status != 0:
            raise SystemdError('systemctl show: ' + (err.strip() or 'exit status %s' % status))
        result = {}
        for block in out.split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            if fields.get('Id'):
                result[fields['Id']] = {'load': fields.get('LoadState', ''), 'active': fields.get('ActiveState', ''),
                                        'freezer': fields.get('FreezerState', '')}
        return result

    def change(self, verb, units, deadline):
        """Run `systemctl freeze|thaw UNIT` for all the units at once. Returns {unit: error text}, '' for a command
        that succeeded. A command that has not finished at the deadline is killed."""
        jobs, results = {}, {}
        for unit in units:
            try:
                jobs[unit] = subprocess.Popen([*self.prefix, verb, unit], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                              errors='replace')
            except OSError as error:
                results[unit] = 'could not start systemctl: %s' % (error.strerror or error)
        while jobs:
            for unit, job in list(jobs.items()):
                if job.poll() is not None:
                    out, err = job.communicate()
                    results[unit] = '' if job.returncode == 0 else (
                        err.strip() or out.strip() or 'exit status %d' % job.returncode)
                    del jobs[unit]
            if not jobs or self.clock() >= deadline:
                break
            self.sleep(0.01)
        for unit, job in jobs.items():
            job.kill()
            job.communicate()
            results[unit] = 'timed out'
        return results

    def freeze(self, units, deadline):
        """Freeze the units at once and wait until each is frozen or the deadline passes. Returns {unit: Outcome}.
        What is read after the deadline costs the sleep delay lock that much time, so all the units are read together
        when the commands are done, and a read that comes after the deadline is the only one. A unit whose command
        failed is read just that once. One whose command succeeded, or ran out of time, is read again until it is
        frozen: the command is meant to return only then, but the state is what counts."""
        errors = self.change('freeze', units, deadline)
        outcomes = {unit: Outcome(None, errors.get(unit, '')) for unit in units}
        reading = list(units)
        while reading:
            self.read_into(outcomes, reading, deadline)
            reading = [unit for unit in reading
                       if outcomes[unit].error in ('', 'timed out') and outcomes[unit].state != 'frozen']
            if not reading or self.clock() >= deadline:
                break
            self.sleep(0.05)
        return outcomes

    def read_into(self, outcomes, units, deadline):
        try:
            states = self.states(units, timeout=max(0.2, min(1.0, deadline - self.clock())))
        except SystemdError:
            return
        for unit in units:
            if unit in states:
                outcomes[unit].state = states[unit]['freezer']

    def thaw(self, units, deadline):
        """Thaw the units at once. Returns {unit: error text}; the state, read afterwards, decides what became of them."""
        return self.change('thaw', units, deadline)


class SleepLock:
    """A logind delay lock on sleep, kept by `systemd-inhibit` around a `cat` that this guard feeds nothing. Closing
    cat's input ends cat, then systemd-inhibit, and with them the lock; that also happens when the guard dies."""

    def __init__(self, command=LOCK_COMMAND, clock=time.monotonic):
        self.command, self.clock = command, clock
        self.process = None

    @property
    def held(self):
        return self.process is not None and self.process.poll() is None

    def acquire(self, timeout=3.0):
        """Take the lock; True once logind has it. The shell prints "held" after systemd-inhibit took the lock."""
        if self.held:
            return True
        self.process = None
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            log('could not take the sleep delay lock: could not start %s: %s' % (self.command[0], error.strerror or error))
            return False
        try:
            deadline = self.clock() + timeout
            said = b''
            while b'held\n' not in said and process.poll() is None and self.clock() < deadline:
                if select.select([process.stdout], [], [], 0.05)[0]:
                    chunk = os.read(process.stdout.fileno(), 64)
                    if not chunk:
                        # Its output is closed: it is ending, and about to be reaped.
                        try:
                            process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    said += chunk
            if b'held\n' in said and process.poll() is None:
                self.process = process
                return True
            reason = 'no answer in %.1f s' % timeout
            if process.poll() is not None:
                said = b''
                if select.select([process.stderr], [], [], 0)[0]:
                    said = os.read(process.stderr.fileno(), 400)
                reason = said.decode(errors='replace').strip() or 'exit status %d' % process.returncode
        except BaseException:
            self.end(process)       # a stop signal arrived while the lock was being taken: leave nothing behind
            raise
        self.end(process)
        log('could not take the sleep delay lock: ' + reason)
        return False

    def release(self):
        """Let the lock go. True if it was held."""
        process, self.process = self.process, None
        if process is None:
            return False
        self.end(process)
        return True

    @staticmethod
    def end(process):
        """Close the process's input and wait for it; ask harder if it does not end."""
        try:
            process.stdin.close()
        except OSError:
            pass
        for sign in (None, signal.SIGTERM, signal.SIGKILL):
            if sign is not None:
                try:
                    os.killpg(process.pid, sign)
                except OSError:
                    pass
            try:
                process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                continue
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass


class EndOfEvents(Exception):
    """The source of events has no more (the tests' sources end like this)."""


class LogindMonitor:
    """The events of logind that matter, read from `gdbus monitor`."""

    def __init__(self, command=MONITOR_COMMAND, clock=time.monotonic):
        self.command, self.clock = command, clock
        self.process = None
        self.pending = []
        self.partial = b''

    def start(self, timeout=5.0):
        """Start watching. The monitor's first line about the owner of logind's name tells that its match rule is in
        place at the bus, so no signal is missed after this returns."""
        self.close()
        self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            try:
                event = self.next(max(0.0, deadline - self.clock()))
            except EOFError:
                self.close()
                raise OSError('%s ended at once' % self.command[0]) from None
            if event is not None and event[0] == 'logind':
                return
            if event is not None:
                self.pending.append(event)
        log('the logind monitor said nothing about logind in %.1f s; going on' % timeout)

    def next(self, timeout):
        """The next event, ('prepare', bool) or ('logind', bool); None when none came in `timeout` seconds. EOFError
        when the monitor has ended."""
        if self.pending:
            return self.pending.pop(0)
        deadline = self.clock() + timeout
        while True:
            remaining = max(0.0, deadline - self.clock())
            if self.process is None:
                raise EOFError
            if select.select([self.process.stdout], [], [], remaining)[0]:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise EOFError
                *lines, self.partial = (self.partial + chunk).split(b'\n')
                for line in lines:
                    event = parse_event(line.decode(errors='replace'))
                    if event is not None:
                        self.pending.append(event)
                if self.pending:
                    return self.pending.pop(0)
            if self.clock() >= deadline:
                return None

    def waiting(self):
        """Whether an event is there to be read now: one that came before this call. Whatever the monitor has written
        is read for that, without waiting, and the event stays for next(). A line that says nothing about logind is no
        event, and neither is what the monitor has not written yet. EOFError when the monitor has ended."""
        while True:
            event = self.next(0.0)
            if event is not None:
                self.pending.insert(0, event)
                return True
            if self.process is None or not select.select([self.process.stdout], [], [], 0)[0]:
                return False

    def close(self):
        process, self.process = self.process, None
        self.pending, self.partial = [], b''
        if process is None:
            return
        for sign in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sign)
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            process.stdout.close()
        except OSError:
            pass


class Guard:
    """The state machine. It holds the sleep lock while the machine is awake; on a sleep it waits a moment for the
    sides to take the announcement, freezes the sides that run and lets go of the lock; on a wake it thaws what it
    froze and takes the lock again. `ours` is the set of units it froze, or asked to freeze and may have frozen, and has
    not seen running again: only those are ever thawed. `deferred` holds the events that came while it waited, until
    run() has them. `looked_at` is when the guard last read the source of events. `wake_lost` is set when logind left
    the bus after the sides were frozen: it will not announce the wake. `jobs_unread` is what the journal last said
    about a job list that could not be read, so that the same words are said once, not at every try."""

    def __init__(self, systemd, lock, events, *, clock=time.monotonic, sleep=time.sleep,
                 settle_seconds=SETTLE_SECONDS, freeze_seconds=FREEZE_SECONDS, thaw_seconds=THAW_SECONDS,
                 state_path=None):
        self.systemd, self.lock, self.events = systemd, lock, events
        self.state_path = state_path
        self.clock, self.sleep = clock, sleep
        self.settle_seconds, self.freeze_seconds, self.thaw_seconds = settle_seconds, freeze_seconds, thaw_seconds
        self.ours = set()
        self.deferred = []
        self.asleep = False
        self.wake_lost = False
        self.jobs_unread = ''
        self.lock_retry_at = 0.0
        self.monitor_failures = 0
        self.looked_at = self.clock()

    # --- what an earlier guard froze -----------------------------------------------------------------
    def remember(self):
        """Write `ours` to the state file, when it changed. A file that cannot be written is said, nothing more:
        the guard still thaws what it knows of itself."""
        if not self.state_path:
            return
        wanted = sorted(self.ours)
        try:
            path = Path(self.state_path)
            if not wanted:
                path.unlink(missing_ok=True)
                return
            temporary = path.with_name(path.name + '.new')
            temporary.write_text(json.dumps({'frozen': wanted}))
            temporary.chmod(0o600)
            temporary.replace(path)
        except OSError as error:
            log('could not write %s: %s' % (self.state_path, error))

    def recover(self):
        """Thaw what a guard before this one froze and did not thaw (it was killed between the freeze and the wake).
        Only units in the state file that are frozen now: a side that is frozen for its standby is not in it."""
        if not self.state_path:
            return
        try:
            listed = json.loads(Path(self.state_path).read_text()).get('frozen', [])
            units = sorted(unit for unit in listed if isinstance(unit, str) and SIDE_UNIT.fullmatch(unit))
        except (OSError, ValueError, AttributeError):
            return
        try:
            states = self.systemd.states(units, timeout=2.0) if units else {}
        except SystemdError as error:
            log('could not read the units an earlier guard froze: %s' % error)
            return
        for unit in units:
            if states.get(unit, {}).get('freezer') == 'frozen':
                log('%s was frozen by an earlier guard; thawing it' % unit)
                self.ours.add(unit)
        self.remember()
        self.thaw_ours(self.thaw_seconds)

    # --- the lock ------------------------------------------------------------------------------------
    def arm(self, again=False):
        if self.lock.held:
            return True
        if self.lock.acquire():
            log('took the sleep delay lock' + (' again' if again else ''))
            return True
        self.lock_retry_at = self.clock() + LOCK_RETRY_SECONDS
        return False

    def release_lock(self, detail):
        if self.lock.release():
            log('released the sleep delay lock' + detail)
        else:
            log('the sleep delay lock was not held, so logind does not wait for this guard' + detail)

    # --- a sleep ---------------------------------------------------------------------------------------
    def listen(self, timeout):
        """The next event from logind and when it was sent, as (event, sent); (None, now) when none came in `timeout`
        seconds. An event that was there to be read when this began was waiting while the guard was busy with
        something else, and all that is known of when it came is that it was after the guard last looked: that is
        `sent` then, however long the read takes. One that came during the wait was sent when it was read, however
        soon after the wait began. One that the monitor had read already, behind another event, came before that look:
        its lateness is counted short by up to the lateness of the event before it. logind counts what it waits from
        the announcement, not from when this process gets to read it."""
        waiting = self.events.waiting()
        event = self.events.next(timeout)
        now = self.clock()
        sent = self.looked_at if event is not None and waiting else now
        self.looked_at = now
        return event, sent

    def prepare_for_sleep(self, sent=None):
        """A sleep is announced. `sent` is when logind sent the announcement, or the earliest it can have been (listen
        says). With the lock held logind waits for the guard from then on, so the waits are counted from then: an
        announcement that waited while the guard was busy does not get the settle and the freeze cap on top of that
        time."""
        self.asleep = True
        self.wake_lost = False      # a logind that announces a sleep announces its wake
        now = self.clock()
        log('PrepareForSleep(true): freezing the sides before the system sleeps')
        detail = ' after an error'
        try:
            # Without the lock logind does not wait for the guard, and every moment counts: no settle then, and no
            # bound to keep.
            held = self.lock.held
            settle = self.settle_seconds if held else 0.0
            started, late = now, 0.0
            if held and sent is not None and now - sent >= LATE_SECONDS:
                started, late = sent, now - sent
                log('the announcement came up to %.1f s ago; counting the waits from then' % late)
            if settle > 0:
                # A stop that comes during the settle is not an error: the lock goes and nothing was frozen.
                detail = ' (stopped during the settle; nothing was frozen)'
                ended_by = self.called_off(started, settle, late)
                detail = ' after an error'
                if ended_by:
                    # No wake will follow a logind that left the bus: the sleep is off for good, and so is `asleep`.
                    self.asleep = False
                    what, note = CALLED_OFF_BY[ended_by]
                    detail = ' (%s after %.1f s%s; nothing was frozen)' % (what, self.clock() - started, note)
                    return      # run() handles the event next: a wake, or logind back on the bus, takes the lock again
            # The freeze cap counts from the end of the settle, but the whole is anchored at the announcement. A guard
            # that is later than that still gives the freezing the least it can use.
            deadline = max(started + settle + self.freeze_seconds, self.clock() + MIN_FREEZE_SECONDS)
            detail = self.freeze_sides(started, deadline)
        except Exception as error:
            log('could not freeze the sides: %s: %s' % (type(error).__name__, error))
        finally:
            # Whatever happened, the sleep must not wait for this guard any longer.
            self.release_lock(detail)

    def called_off(self, started, settle, late=0.0):
        """Wait until `settle` seconds after `started`, with the lock held, for the sides to take the announcement
        (`late` of that has gone by already). Returns the event that called the sleep off, if one came meanwhile:
        nothing is to be frozen then. None when the time ran out."""
        if settle > late:
            log('waiting %g s for the sides to take the sleep announcement' % (settle - late))
        try:
            return self.wait_for_call_off(started + settle)
        except Exception as error:
            # A wait that goes wrong must not keep the sides from being frozen.
            log('could not wait for the sides: %s: %s' % (type(error).__name__, error))
            return None

    def wait_for_call_off(self, until):
        """Wait until `until` for the sleep to be called off: logind announces the wake, or leaves the bus (see
        CALLED_OFF_BY). Returns that event, or None when the time is up. Every event that comes is kept in
        `deferred`, in order, for run(): none is lost or handled out of turn. The source is read once more when the
        time is up, so that an event that came in while this process was stalled is seen before anything is frozen."""
        while True:
            left = until - self.clock()
            try:
                event, _ = self.listen(max(0.0, left))
            except EOFError:
                # The monitor ended, so nothing more can be seen. run() finds that out again at its next read.
                self.sleep(max(0.0, until - self.clock()))
                return None
            if event is None:
                if left <= 0:
                    return None
                continue
            self.deferred.append(event)
            if event in CALLED_OFF_BY:
                return event

    def freeze_sides(self, started, deadline):
        """Freeze the sides that run and are not frozen yet, within `deadline`. Returns what to add to the line about
        the lock. `started` is when the sleep was announced."""
        began = self.clock()
        todo, states = [], {}
        try:
            units = self.systemd.side_units(timeout=self.within(deadline, 2.0))
            states = self.systemd.states(units, timeout=self.within(deadline, 1.0))
        except SystemdError as error:
            log('could not read the units of the sides: %s' % error)
            units = []
        for unit in units:
            state = states.get(unit)
            running = state is not None and state['freezer'] == 'running'
            # A unit of ours that is read running was thawed meanwhile (a thaw that systemd was too busy for lands
            # unseen): it goes into this sleep like any other, and it stays ours.
            if unit in self.ours and not running:
                log('%s is still %s from the sleep before' % (unit, (state and state['freezer']) or 'frozen'))
            elif state is None or not state['freezer']:
                log('left %s alone: systemd did not say what state it is in' % unit)
            elif running:
                todo.append(unit)
            else:
                log('left %s alone: it is %s already' % (unit, state['freezer']))
        froze, failed = [], []
        if todo:
            # A request that reached systemd freezes the unit though this process never hears that it did: a command
            # that runs out of time is killed and may have sent its request, the read that follows can time out, and a
            # stop can land while the commands are waited for. So the units are ours from the moment they are asked,
            # and only a read that finds one running says that it is not.
            self.ours.update(todo)
            self.remember()   # before the freeze: a guard that dies in it leaves the file that says what to thaw
            try:
                outcomes = self.systemd.freeze(todo, deadline)
            except Exception as error:   # whatever went wrong, the lock still has to go
                log('freezing failed: %s: %s' % (type(error).__name__, error))
                outcomes = {}
            for unit in todo:
                outcome = outcomes.get(unit, Outcome())
                if outcome.state == 'frozen':
                    froze.append(unit)
                    log('froze ' + unit)
                elif outcome.state == 'running':
                    self.ours.discard(unit)
                    failed.append(unit)
                    log('could not freeze %s: %s' % (unit, outcome.error or 'it is still running'))
                else:
                    # Begun and not known to be finished: still ours to thaw.
                    failed.append(unit)
                    state = outcome.state
                    if not state:
                        state = 'in an unknown state' + (' (%s)' % outcome.error if outcome.error else '')
                    log('could not finish freezing %s: it is %s' % (unit, state))
        self.remember()
        if failed:
            return ' after %.1f s, although %d of %d sides did not freeze' % (self.clock() - started, len(failed),
                                                                              len(todo))
        return ' (froze %d unit%s in %.2f s)' % (len(froze), '' if len(froze) == 1 else 's', self.clock() - began)

    def within(self, deadline, limit):
        """A timeout for one call: at most `limit`, at least a little, and not past the deadline."""
        return max(0.2, min(limit, deadline - self.clock()))

    # --- a wake ----------------------------------------------------------------------------------------
    def wake(self, why='PrepareForSleep(false): the system woke up'):
        self.asleep = self.wake_lost = False
        log(why)
        self.thaw_ours(self.thaw_budget())
        self.arm(again=True)

    def thaw_budget(self):
        """How long a thaw may take now. With the lock held a sleep that is announced meanwhile waits for this guard,
        and the guard reads nothing while it thaws. That is so when logind came back while the sides were frozen (the
        guard took the lock again at once) and when a wake finds the lock held. The thaw is given what an idle pass's
        retry gets, 2 seconds counted from when the guard last looked as the waits of an announcement are (see listen),
        and the idle passes retry what is left. A try that has begun is not cut off: it ends at its own deadline, a
        second away at least. When the job list used up the 2 seconds, that is the one try, and the idle passes retry:
        nothing is added for it. Without the lock nothing waits for the guard."""
        if not self.lock.held:
            return self.thaw_seconds
        return min(self.thaw_seconds, max(0.0, THAW_HELD_SECONDS - (self.clock() - self.looked_at)))

    def wake_unannounced(self):
        """logind left the bus after the sides were frozen. A logind that restarts has forgotten the sleep it announced
        (see CALLED_OFF_BY) and will not announce its wake. The system is awake when systemd has no job to sleep, queued
        or running, and the wake is handled then as if it had been announced. Before that the sleep may be going on
        (logind starts it when the delay locks are gone), and thawing the sides in the middle of it would make it
        fail."""
        try:
            jobs = self.systemd.sleep_jobs()
        except SystemdError as error:
            # Asked again at every idle pass, said once: the same words again would only fill the journal.
            if str(error) != self.jobs_unread:
                log('could not read the jobs of systemd: %s' % error)
                self.jobs_unread = str(error)
            return
        self.jobs_unread = ''
        if not jobs:
            self.wake('no PrepareForSleep(false) will come, and systemd has no job to sleep: the system is awake')

    def thaw_ours(self, seconds):
        """Thaw the units in `ours` until each is running again or gone, or `seconds` have passed."""
        if not self.ours:
            return
        deadline = self.clock() + seconds
        while self.ours:
            units = sorted(self.ours)
            try:
                self.systemd.thaw(units, self.clock() + min(5.0, max(1.0, deadline - self.clock())))
                states = self.systemd.states(units, timeout=self.within(deadline, 2.0))
            except SystemdError as error:
                log('could not read the units to thaw: %s' % error)
                states = {}
            for unit in units:
                state = states.get(unit)
                if state is None:
                    continue
                if state['load'] == 'not-found' or state['active'] in ('inactive', 'failed'):
                    log('%s is gone; nothing to thaw' % unit)
                    self.ours.discard(unit)
                elif state['freezer'] == 'running':
                    log('thawed ' + unit)
                    self.ours.discard(unit)
            if not self.ours or self.clock() >= deadline:
                break
            self.sleep(THAW_RETRY_SECONDS)
        self.remember()
        for unit in sorted(self.ours):
            log('could not thaw %s yet; trying again' % unit)

    # --- the loop --------------------------------------------------------------------------------------
    def tick(self):
        """Nothing happened for a while: retry what is left over."""
        if self.asleep:
            if self.wake_lost:
                self.wake_unannounced()
            return
        if self.ours:
            self.thaw_ours(2.0)
        if not self.lock.held and self.clock() >= self.lock_retry_at:
            self.arm(again=True)

    def handle(self, event, sent=None):
        kind, value = event
        if kind == 'prepare':
            self.prepare_for_sleep(sent) if value else self.wake()
        elif kind == 'logind' and value:
            # logind came back on the bus (it was restarted): a lock from before it is gone with it.
            self.lock.release()
            self.arm(again=True)
        elif self.asleep:
            # The sides are frozen and the lock is gone, so logind may have begun the sleep already, and a thaw now
            # could break it: whether it did is for systemd to say, which tick() asks until the sleep is over.
            self.wake_lost, self.jobs_unread = True, ''
            log('logind left the bus after announcing a sleep; its wake will not be announced, so the sides are thawed '
                'once systemd has no job to sleep')
        else:
            log('logind left the bus')

    def lost_monitor(self):
        """The monitor ended. Start it again, after a pause that grows while it keeps failing. A wake may have gone
        by unseen, so what was frozen is thawed (with the lock held, for as long as thaw_budget says)."""
        delay = MONITOR_RETRY_SECONDS[min(self.monitor_failures, len(MONITOR_RETRY_SECONDS) - 1)]
        self.monitor_failures += 1
        log('the logind monitor ended; starting it again in %.0f s' % delay)
        self.sleep(delay)
        try:
            self.events.start()
        except OSError as error:
            log('could not start the logind monitor: %s' % error)
            return
        self.monitor_failures = 0
        self.looked_at = self.clock()       # a new monitor: what was sent while there was none is lost
        if self.asleep or self.ours:
            self.asleep = self.wake_lost = False
            self.thaw_ours(self.thaw_budget())
        self.arm(again=True)

    def run(self):
        """Serve events until the source has no more (EndOfEvents, the tests) or the process is told to stop."""
        self.looked_at = self.clock()
        self.guarded(self.recover, 'the recovery of an earlier freeze')
        self.arm()
        while True:
            try:
                if self.deferred:       # seen while the guard waited for the settle: older than anything still to come
                    event, sent = self.deferred.pop(0), None    # the lock is not held then: nothing to count from
                else:
                    event, sent = self.listen(TICK_SECONDS if (self.ours or self.wake_lost or not self.lock.held)
                                              else IDLE_SECONDS)
            except EndOfEvents:
                return
            except EOFError:
                self.guarded(self.lost_monitor, 'the lost monitor')
                continue
            if event is None:
                self.guarded(self.tick, 'the idle pass')
            else:
                self.guarded(lambda: self.handle(event, sent), 'event %r' % (event,))

    @staticmethod
    def guarded(step, what):
        """Run one step. An error in it is said and ends nothing: the guard has to be there at the next sleep."""
        try:
            step()
        except Exception as error:
            log('error in %s: %s: %s' % (what, type(error).__name__, error))

    def shutdown(self):
        """The guard ends: never leave a side frozen behind, and let the lock go."""
        self.guarded(lambda: self.thaw_ours(self.thaw_seconds), 'the final thaw')
        if self.lock.release():
            log('released the sleep delay lock (the guard ends)')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Freeze both DosPlex sides before the machine sleeps.')
    parser.add_argument('--manager', choices=('system', 'user'), default='system',
                        help='which systemd manager runs the sides (user: for the tests, without root)')
    parser.add_argument('--settle-seconds', type=float, default=SETTLE_SECONDS,
                        help='wait this long after a sleep is announced, holding the lock, for the sides to take the '
                             'announcement before they are frozen; 0 freezes at once (default %(default)s)')
    parser.add_argument('--freeze-seconds', type=float, default=FREEZE_SECONDS,
                        help='longest wait for the sides to freeze before the sleep goes on (default %(default)s)')
    parser.add_argument('--thaw-seconds', type=float, default=THAW_SECONDS,
                        help='longest wait for the sides to thaw after a wake (default %(default)s)')
    parser.add_argument('--state-path', default=STATE_PATH, help='where the units this guard froze are kept '
                        '(default %(default)s)')
    args = parser.parse_args(argv)
    if not 0 <= args.settle_seconds <= MAX_SETTLE_SECONDS:
        parser.error('--settle-seconds must be between 0 and %g: logind waits 5 s for a delay lock at most'
                     % MAX_SETTLE_SECONDS)
    if not MIN_FREEZE_SECONDS <= args.freeze_seconds <= MAX_FREEZE_SECONDS:
        parser.error('--freeze-seconds must be between %g and %g: logind waits 5 s for a delay lock at most'
                     % (MIN_FREEZE_SECONDS, MAX_FREEZE_SECONDS))
    if not 0.2 <= args.thaw_seconds <= 120:
        parser.error('--thaw-seconds must be between 0.2 and 120')
    if args.manager == 'system' and os.geteuid() != 0:
        parser.error('must run as root')

    def stop(_signum, _frame):
        raise SystemExit(0)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop)
    settle, freeze, delay_max = choose_waits(args.settle_seconds, args.freeze_seconds)
    events = LogindMonitor()
    guard = Guard(Systemd(args.manager), SleepLock(), events, settle_seconds=settle, freeze_seconds=freeze,
                  thaw_seconds=args.thaw_seconds,
                  state_path=args.state_path)
    log('watching logind for PrepareForSleep (%s manager, %g s to settle, %g s to freeze, logind waits %g s for a '
        'delay lock)' % (args.manager, settle, freeze, delay_max))
    try:
        # The monitor first: a sleep announced between taking the lock and watching would go unseen.
        while True:
            try:
                events.start()
                break
            except OSError as error:
                log('could not start the logind monitor: %s' % error)
                time.sleep(MONITOR_RETRY_SECONDS[min(guard.monitor_failures, len(MONITOR_RETRY_SECONDS) - 1)])
                guard.monitor_failures += 1
        guard.monitor_failures = 0
        guard.run()
    except SystemExit:
        pass
    finally:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)   # the final thaw is not to be cut short
        guard.shutdown()
        events.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
