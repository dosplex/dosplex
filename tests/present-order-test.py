#!/usr/bin/env python3
# dosplex-test: tier=unit needs=cc
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compile the compositor's actual present-order code and drive it with a model of wlroots' X11 backend.

Under the X11 backend the compositor learns from the presents of its output how the backend tags them: with the
sequence their commit gets (the release's patched wlroots: commit only on change) or with the one before it (the
unpatched wlroots 0.17: commit at every host frame). The model sends one present for each commit made while the
output is enabled, tagged one way or the other, and delivers them the way a host can: any number of commits in flight,
in any order, some lost or delivered twice, and the ones still on their way when the output is disabled arriving after
it is enabled again.

What is checked, for random runs and for the cases that matter most:
  - an unpatched backend is never taken for a patched one, whatever happens to its presents (a wrong answer there
    would stop the commits that presentation feedback needs);
  - a patched backend is never taken for an unpatched one;
  - an unpatched backend is recognised from the present of its first commit wherever that comes in the order and
    however many commits are in flight, and a patched one from the present that carries the output's current sequence;
  - the count of the commits that are still unanswered is right with presents out of order, and each enabling of the
    output starts a count of its own that the presents of before do not touch;
  - the frame handler's loop (a model of the flags wlroots keeps for an output, with the handler's rule as compiled)
    returns to one commit in flight after commits outside it, and does not stop while a present is in flight.
"""
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
@BLOCK@
int main(void) {
    static struct present_order order;
    char word[32];
    setvbuf(stdout, NULL, _IOLBF, 0);
    while (scanf("%31s", word) == 1) {
        unsigned a, b;
        if (!strcmp(word, "begin") && scanf("%u", &a) == 1) {
            present_order_begin(&order, a);
        } else if (!strcmp(word, "end")) {
            present_order_end(&order);
        } else if (!strcmp(word, "note") && scanf("%u %u", &a, &b) == 2) {
            enum present_verdict verdict = present_order_note(&order, a, b);
            printf("V %d %u\n", (int)verdict, order.seen);
        } else if (!strcmp(word, "others") && scanf("%u %u", &a, &b) == 2) {
            printf("O %d\n", present_order_others_pending(&order, a, b != 0) ? 1 : 0);
        } else {
            return 2;
        }
    }
    return 0;
}
'''

UNDECIDED, OWN, BEHIND = 0, 1, 2


def extract(source):
    begin = source.index('/* === Present order === */')
    end = source.index('/* === End of present order === */')
    return source[begin:end]


class Probe:
    """The compiled code, driven one line at a time."""

    def __init__(self, binary):
        self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def ask(self, line, prefix):
        self.process.stdin.write(line + '\n')
        self.process.stdin.flush()
        answer = self.process.stdout.readline().split()
        assert answer and answer[0] == prefix, (line, answer)
        return [int(word) for word in answer[1:]]

    def begin(self, first):
        self.process.stdin.write(f'begin {first}\n')

    def end(self):
        self.process.stdin.write('end\n')

    def note(self, serial, commit_seq):
        verdict, seen = self.ask(f'note {serial & 0xffffffff} {commit_seq & 0xffffffff}', 'V')
        return verdict, seen

    def others(self, commit_seq, committed):
        return bool(self.ask(f'others {commit_seq & 0xffffffff} {int(committed)}', 'O')[0])

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close()


class Output:
    """What the X11 backend and wlroots do with the commits of an output (types/output/output.c, backend/x11)."""

    def __init__(self, unpatched):
        self.offset = -1 if unpatched else 0
        self.commit_seq = 0
        self.enabled = False
        self.in_flight = []  # (commit index, serial), in the order the commits were made

    def commit(self, enable=None):
        """A commit; returns True if it enabled or disabled the output. Every commit of an enabled output sends one
        present, tagged with the serial read before commit_seq is incremented (the unpatched backend) or after it
        (the patched one)."""
        changes = enable is not None and enable != self.enabled
        if enable is not None:
            self.enabled = enable
        self.commit_seq += 1
        if self.enabled:
            self.in_flight.append((self.commit_seq, self.commit_seq + self.offset))
        return changes


class Compositor:
    """The compositor's side: an output, the compiled code, and the calls that note_present_sequence and
    handle_output_commit make."""

    def __init__(self, probe, unpatched):
        self.probe = probe
        self.output = Output(unpatched)
        self.verdicts = []
        self.output.commit(enable=True)
        probe.begin(1)

    def commit(self, enable=None):
        changed = self.output.commit(enable)
        if changed:
            if self.output.enabled:
                self.probe.begin(self.output.commit_seq)
            else:
                self.probe.end()
        return changed

    def deliver(self, serial):
        verdict, _ = self.probe.note(serial, self.output.commit_seq)
        self.verdicts.append(verdict)
        return verdict


class PresentOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix='dosplex-present-order-')
        cls.addClassCleanup(directory.cleanup)
        source = (ROOT / 'compositor/dosplex-compositor.c').read_text()
        code = Path(directory.name) / 'probe.c'
        code.write_text(HARNESS.replace('@BLOCK@', extract(source)))
        cls.binary = Path(directory.name) / 'probe'
        subprocess.run(['cc', '-Wall', '-Werror', str(code), '-o', str(cls.binary)],
                       check=True, capture_output=True, timeout=30)

    def setUp(self):
        self.probe = Probe(self.binary)
        self.addCleanup(self.probe.close)

    # --- the two proofs and the count, one at a time ---

    def test_the_output_s_current_sequence_proves_the_own_sequence(self):
        self.probe.begin(1)
        self.assertEqual(self.probe.note(5, 5)[0], OWN)
        self.assertEqual(self.probe.note(4, 5)[0], UNDECIDED)

    def test_it_proves_it_with_the_count_stopped_and_for_a_present_of_no_count(self):
        self.assertEqual(self.probe.note(7, 7), (OWN, 0))
        self.probe.begin(10)
        self.probe.end()
        self.assertEqual(self.probe.note(12, 12), (OWN, 0))

    def test_the_sequence_before_the_first_commit_proves_one_behind(self):
        self.probe.begin(1)
        self.assertEqual(self.probe.note(0, 3), (BEHIND, 1))
        self.probe.begin(10)
        self.assertEqual(self.probe.note(9, 12), (BEHIND, 1))

    def test_the_first_commit_s_own_sequence_proves_nothing_alone(self):
        self.probe.begin(10)
        self.assertEqual(self.probe.note(10, 12), (UNDECIDED, 1))
        self.assertEqual(self.probe.note(11, 12), (UNDECIDED, 2))

    def test_presents_from_before_the_count_are_not_counted(self):
        self.probe.begin(10)
        for serial in (8, 5, 0, 7):
            self.assertEqual(self.probe.note(serial, 12), (UNDECIDED, 0))
        self.assertEqual(self.probe.note(9, 12), (BEHIND, 1))

    def test_a_disabled_output_counts_nothing_and_a_new_count_starts_afresh(self):
        self.probe.begin(1)
        self.probe.note(0, 2)
        self.probe.end()
        self.assertEqual(self.probe.note(1, 3), (UNDECIDED, 0))
        self.assertFalse(self.probe.others(3, False))
        self.probe.begin(5)
        self.assertEqual(self.probe.note(4, 5), (BEHIND, 1))

    def test_the_unanswered_commits_are_counted_apart_from_the_one_just_made(self):
        self.probe.begin(1)
        self.assertFalse(self.probe.others(0, False))
        for commit_seq, seen, committed, expected in [
                (1, 0, False, True), (1, 0, True, False),    # the first commit, unanswered
                (3, 0, True, True), (3, 0, False, True),     # two more in flight
                (3, 2, True, False), (3, 2, False, True),    # two answered: the one just made is the only one
                (3, 3, False, False), (3, 3, True, False)]:  # all answered
            self.probe.end()
            self.probe.begin(1)
            for index in range(seen):
                self.probe.note(index, commit_seq)
            self.assertEqual(self.probe.others(commit_seq, committed), expected, (commit_seq, seen, committed))

    def test_more_presents_than_commits_never_count_as_outstanding(self):
        self.probe.begin(1)
        for serial in (0, 1, 2):
            self.probe.note(serial, 1)
        self.assertFalse(self.probe.others(1, False))
        self.assertFalse(self.probe.others(1, True))

    def test_the_sequence_may_wrap(self):
        top = 0xfffffffe
        self.probe.begin(top)
        self.assertEqual(self.probe.note(top - 1, top + 3), (BEHIND, 1))  # the unpatched tag of the first commit
        self.assertEqual(self.probe.note(top, top + 3), (UNDECIDED, 2))
        self.assertTrue(self.probe.others(top + 3, False))                 # 4 commits, 2 answered
        self.assertEqual(self.probe.note(top + 3, top + 3), (OWN, 3))

    # --- random runs against the model ---

    def run_world(self, seed, unpatched, lose=False, repeat=False, cycles=True):
        """One random run; returns the compositor and the list of (verdict, kind of the present it came with)."""
        rng = random.Random(seed)
        compositor = Compositor(self.probe, unpatched)
        output = compositor.output
        stale = []         # presents of before the output was disabled, still on their way
        delivered = []
        notes = []
        for _ in range(rng.randrange(8, 60)):
            action = rng.random()
            if action < 0.30:
                compositor.commit()
            elif action < 0.36 and cycles:
                # Disabled and enabled again, with presents still on their way: a commit that sends none in between.
                stale.extend(output.in_flight)
                output.in_flight.clear()
                compositor.commit(enable=False)
                for _ in range(rng.randrange(0, 3)):
                    if stale and rng.random() < 0.5:
                        serial = stale.pop(rng.randrange(len(stale)))[1]
                        notes.append(compositor.deliver(serial))
                compositor.commit(enable=True)
            else:
                pool = output.in_flight
                if pool and rng.random() < 0.8:
                    index = rng.randrange(len(pool)) if rng.random() < 0.5 else 0
                    commit, serial = pool.pop(index)
                    if lose and rng.random() < 0.15:
                        continue
                    notes.append(compositor.deliver(serial))
                    delivered.append(serial)
                    if repeat and rng.random() < 0.15:
                        notes.append(compositor.deliver(serial))
                elif stale and rng.random() < 0.7:
                    notes.append(compositor.deliver(stale.pop(rng.randrange(len(stale)))[1]))
        return compositor, notes

    def test_an_unpatched_backend_is_never_taken_for_a_patched_one(self):
        for seed in range(400):
            compositor, notes = self.run_world(seed, unpatched=True, lose=seed % 2 == 0, repeat=seed % 3 == 0)
            self.assertNotIn(OWN, notes, f'seed {seed}')

    def test_a_patched_backend_is_never_taken_for_an_unpatched_one(self):
        for seed in range(400):
            compositor, notes = self.run_world(seed, unpatched=False, lose=seed % 2 == 0, repeat=seed % 3 == 0)
            self.assertNotIn(BEHIND, notes, f'seed {seed}')

    def test_an_unpatched_backend_is_recognised_wherever_its_first_present_comes(self):
        for seed in range(300):
            rng = random.Random(seed)
            compositor = Compositor(self.probe, unpatched=True)
            output = compositor.output
            for _ in range(rng.randrange(0, 6)):
                compositor.commit()
            order = list(output.in_flight)
            rng.shuffle(order)
            verdicts = [compositor.deliver(serial) for _, serial in order]
            self.assertEqual(verdicts.count(BEHIND), 1, f'seed {seed}')
            self.assertEqual(verdicts.index(BEHIND), [serial for _, serial in order].index(0), f'seed {seed}')

    def test_a_patched_backend_is_recognised_once_one_commit_is_left_in_flight(self):
        for seed in range(300):
            rng = random.Random(seed)
            compositor = Compositor(self.probe, unpatched=False)
            output = compositor.output
            for _ in range(rng.randrange(0, 6)):
                compositor.commit()
            pool = list(output.in_flight)
            newest = pool.pop()
            rng.shuffle(pool)
            for _, serial in pool:
                self.assertNotEqual(compositor.deliver(serial), BEHIND)
            self.assertEqual(compositor.deliver(newest[1]), OWN, f'seed {seed}')

    def test_the_count_of_unanswered_commits_is_right_with_presents_out_of_order(self):
        for seed in range(400):
            rng = random.Random(seed)
            compositor = Compositor(self.probe, unpatched=seed % 2 == 0)
            output = compositor.output
            stale = []
            for _ in range(rng.randrange(8, 60)):
                action = rng.random()
                if action < 0.35:
                    compositor.commit()
                elif action < 0.42:
                    stale.extend(output.in_flight)
                    output.in_flight.clear()
                    compositor.commit(enable=False)
                    compositor.commit(enable=True)
                elif output.in_flight and rng.random() < 0.8:
                    _, serial = output.in_flight.pop(rng.randrange(len(output.in_flight)))
                    compositor.deliver(serial)
                elif stale:
                    compositor.deliver(stale.pop(rng.randrange(len(stale)))[1])
                outstanding = len(output.in_flight)
                self.assertEqual(self.probe.others(output.commit_seq, False), outstanding >= 1,
                                 f'seed {seed}: {outstanding} in flight')
                self.assertEqual(self.probe.others(output.commit_seq, True), outstanding >= 2,
                                 f'seed {seed}: {outstanding} in flight')

    def test_lost_presents_can_only_make_the_count_say_more_is_outstanding(self):
        for seed in range(300):
            compositor, _ = self.run_world(seed, unpatched=seed % 2 == 0, lose=True, cycles=False)
            output = compositor.output
            outstanding = len(output.in_flight)
            self.assertGreaterEqual(self.probe.others(output.commit_seq, False), outstanding >= 1, f'seed {seed}')

    # --- the frame handler's loop ---

    def simulate_loop(self, seed, unpatched, outside_commits=(1, 4), steps=80):
        """The flags wlroots keeps for an output (frame_pending, needs_frame, the idle frame) and the handler of the
        compositor: it commits when the scene has something (needs_frame), and at its end schedules the next frame
        unless the compiled rule holds it back (then a timer would, if the count were wrong). Presents come back in
        any order. Returns the in-flight count at each commit made by the handler, the commits made outside it and the
        state of the loop after each step."""
        rng = random.Random(seed)
        compositor = Compositor(self.probe, unpatched)
        output = compositor.output
        # The enabling commit is in flight, and the scene output made after it asked for a frame.
        state = {'frame_pending': True, 'needs_frame': True, 'idle': False, 'pacing': 'every-frame', 'timer': False}
        handler_commits, outside, trace = [], [], []

        def schedule_frame():
            state['needs_frame'] = True
            if not state['frame_pending']:
                state['idle'] = True

        def commit(enable=None):
            compositor.commit(enable)
            state['frame_pending'] = True
            state['needs_frame'] = False

        def handler():
            state['timer'] = False
            committed = False
            if state['needs_frame']:
                handler_commits.append(len(output.in_flight))
                commit()
                committed = True
            if state['pacing'] == 'on-demand':
                return
            if not self.probe.others(output.commit_seq, committed):
                schedule_frame()
            elif not committed:
                state['timer'] = True

        def send_frame():
            state['frame_pending'] = False
            handler()

        for step in range(steps):
            if outside_commits[0] <= step < outside_commits[1] and rng.random() < 0.5:
                commit()                      # a host resize: a commit outside the handler,
                outside.append(step)
                schedule_frame()              # and the scene asks for a frame because the mode changed
            elif state['idle']:
                state['idle'] = False
                if not state['frame_pending']:
                    send_frame()
            elif output.in_flight:
                _, serial = output.in_flight.pop(rng.randrange(len(output.in_flight)) if rng.random() < 0.5 else 0)
                verdict = compositor.deliver(serial)
                if state['pacing'] == 'every-frame':
                    state['pacing'] = {OWN: 'on-demand', BEHIND: 'late'}.get(verdict, 'every-frame')
                send_frame()
            elif state['timer']:
                state['timer'] = False
                schedule_frame()
            trace.append((bool(output.in_flight), state['idle'], state['needs_frame'], state['timer'],
                          state['pacing']))
        return handler_commits, outside, trace, state

    def test_the_loop_returns_to_one_commit_in_flight_after_commits_outside_it(self):
        for unpatched in (True, False):
            for seed in range(200):
                commits, outside, trace, state = self.simulate_loop(seed, unpatched, outside_commits=(1, 12))
                label = f"{'unpatched' if unpatched else 'patched'} seed {seed}"
                if unpatched:
                    self.assertEqual(state['pacing'], 'late', label)
                else:
                    self.assertEqual(state['pacing'], 'on-demand', label)
                    continue
                # The last commits the handler made, long after the ones outside it: none was made with a present
                # still in flight, which is one commit for each host frame.
                self.assertGreaterEqual(len(commits), 20, label)
                self.assertEqual(max(commits[-20:]), 0, f'{label}: in flight at the commits {commits[-20:]}')

    def test_the_loop_does_not_stop_while_it_is_meant_to_run(self):
        for unpatched in (True, False):
            for seed in range(200):
                commits, outside, trace, state = self.simulate_loop(seed, unpatched)
                for step, (in_flight, idle, needs_frame, timer, pacing) in enumerate(trace):
                    if pacing == 'on-demand':
                        break
                    self.assertTrue(in_flight or idle or timer,
                                    f"{'unpatched' if unpatched else 'patched'} seed {seed}: the loop stopped at step "
                                    f"{step} with nothing in flight, no frame scheduled and no timer")

    def test_a_patched_backend_settles_on_demand_with_commits_outside_the_handler(self):
        for seed in range(200):
            commits, outside, trace, state = self.simulate_loop(seed, unpatched=False, outside_commits=(1, 6))
            self.assertEqual(state['pacing'], 'on-demand', f'seed {seed}')
            self.assertFalse(trace[-1][0] or trace[-1][1], f'seed {seed}: not idle at the end: {trace[-1]}')


if __name__ == '__main__':
    unittest.main()
