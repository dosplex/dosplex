#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A loopback whose output was removed and created again is loaded again, and the monitor says what it does.

The TV's HDMI handshake removes the Steam Machine's HDMI output and creates it again under the same name.
The new sink has a new index. The loopback that was loaded against the old one keeps its arguments, but it
is linked to nothing (it is loaded with sink_dont_move=true, so nothing links it again), and its route is
silent until something loads it again. These tests run the router against a fake Pulse server: the JSON of
`pactl list sinks|sources|sink-inputs|source-outputs`, the module list and the commands that change them.
tests/audio-integration.py does the same with a real PipeWire server.
"""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('audio', Path(__file__).resolve().parents[1] / 'packaging/steamos/dosplex-audio-router.py')
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

NOTHING = router.UNATTACHED
HDMI = 'alsa_output.pci-0000_03_00.1.hdmi-stereo'
HEADSET = 'alsa_output.usb-Logitech_G935-00.analog-stereo'
MICROPHONE = 'alsa_input.usb-Logitech_G935-00.mono-fallback'


class Server:
    """Enough of a Pulse server for the router: sinks with their monitors, sources, loopback modules with the
    two streams each has, and the commands that change them."""

    def __init__(self):
        self.serial = 6900
        self.sinks, self.sources = {}, {}
        self.modules = []
        self.sink_inputs, self.source_outputs = [], []
        self.next_module = 536870912
        self.links = True    # whether the streams of a new loopback are linked at once
        self.commands = []   # the commands that changed something
        self.default = None  # what get-default-sink answers; None is an error

    def index(self):
        self.serial += 1
        return self.serial

    def add_sink(self, name):
        self.sinks[name] = self.sources[name + '.monitor'] = self.index()
        return self.sinks[name]

    def add_source(self, name):
        self.sources[name] = self.index()
        return self.sources[name]

    def remove_sink(self, name):
        index = self.sinks.pop(name)
        del self.sources[name + '.monitor']
        self.unlink(index)

    def remove_source(self, name):
        self.unlink(self.sources.pop(name))

    def unlink(self, index):
        # What PipeWire does with a stream whose node went: it is linked to nothing, and stays so.
        for stream in self.sink_inputs:
            if stream['sink'] == index:
                stream['sink'] = NOTHING
        for stream in self.source_outputs:
            if stream['source'] == index:
                stream['source'] = NOTHING

    def loopback(self, route):
        """The module, its sink input and its source output of the loopback that carries `route`."""
        module = next(m for m in self.modules if router.managed_route(m) == route)
        return (module, next(s for s in self.sink_inputs if s['owner_module'] == str(module['index'])),
                next(s for s in self.source_outputs if s['owner_module'] == str(module['index'])))

    def add_application(self, kind, name, node, **properties):
        stream = {'index': self.index(), 'owner_module': 4294967295, 'corked': False, 'mute': False,
                  'properties': {'application.name': name, **properties}}
        if kind == 'output':
            stream['sink'] = self.sinks[node]
            self.sink_inputs.append(stream)
        else:
            stream['source'] = self.sources[node]
            self.source_outputs.append(stream)
        return stream

    def load(self, name, arguments):
        index, self.next_module = self.next_module, self.next_module + 1
        module = {'index': index, 'name': name, 'argument': ' '.join(arguments)}
        self.modules.append(module)
        options = router.module_args(module)
        if name == 'module-null-sink':
            self.add_sink(options['sink_name'])
        elif name == 'module-loopback':
            linked = self.links
            self.sink_inputs.append({
                'index': self.index(), 'owner_module': str(index), 'corked': False, 'mute': False,
                'sink': self.sinks[options['sink']] if linked else NOTHING,
                'volume': {'front-left': {'value': 65536}, 'front-right': {'value': 65536}},
                'properties': {'pulse.module.id': str(index)}})
            self.source_outputs.append({
                'index': self.index(), 'owner_module': str(index), 'corked': False, 'mute': False,
                'source': self.sources[options['source']] if linked else NOTHING,
                'properties': {'pulse.module.id': str(index)}})
        return index

    def unload(self, index):
        module = next(m for m in self.modules if m['index'] == index)
        self.modules.remove(module)
        self.sink_inputs[:] = [s for s in self.sink_inputs if s['owner_module'] != str(index)]
        self.source_outputs[:] = [s for s in self.source_outputs if s['owner_module'] != str(index)]
        if module['name'] == 'module-null-sink':
            self.remove_sink(router.module_args(module)['sink_name'])

    def listing(self, kind):
        if kind == 'sinks':
            return [{'index': i, 'name': n, 'description': n, 'properties': {}} for n, i in self.sinks.items()]
        if kind == 'sources':
            return [dict({'index': i, 'name': n, 'description': n, 'properties': {}},
                         **({'monitor_of_sink_name': n[:-len('.monitor')]} if n.endswith('.monitor') else {}))
                    for n, i in self.sources.items()]
        return copy.deepcopy({'sink-inputs': self.sink_inputs, 'source-outputs': self.source_outputs}[kind])

    def pactl(self, *args, **_unused):
        def done(text=''):
            return SimpleNamespace(returncode=0, stdout=text, stderr='')
        if args[:3] == ('list', 'short', 'modules'):
            return done(''.join(f"{m['index']}\t{m['name']}\t{m['argument']}\t\n" for m in self.modules))
        if args[:3] == ('-f', 'json', 'list'):
            return done(json.dumps(self.listing(args[3])))
        command, rest = args[0], args[1:]
        self.commands.append(args)
        if command == 'get-default-sink':
            if self.default is None:
                return SimpleNamespace(returncode=1, stdout='', stderr='no default')
            return done(self.default + '\n')
        if command == 'load-module':
            return done(str(self.load(rest[0], rest[1:])))
        if command == 'unload-module':
            self.unload(int(rest[0]))
            return done()
        if command in ('set-sink-input-mute', 'set-source-output-mute'):
            streams = self.sink_inputs if command == 'set-sink-input-mute' else self.source_outputs
            next(s for s in streams if s['index'] == int(rest[0]))['mute'] = rest[1] == '1'
            return done()
        if command == 'set-sink-input-volume':
            stream = next(s for s in self.sink_inputs if s['index'] == int(rest[0]))
            for channel, value in zip(stream['volume'], rest[1:]):
                stream['volume'][channel] = {'value': int(value)}
            return done()
        if command in ('move-sink-input', 'move-source-output'):
            output = command == 'move-sink-input'
            stream = next((s for s in (self.sink_inputs if output else self.source_outputs) if s['index'] == int(rest[0])), None)
            if stream is None or stream.get('ended'):   # gone between the listing and the move, as pactl reports it
                return SimpleNamespace(returncode=1, stdout='', stderr='Failure: No such entity')
            stream['sink' if output else 'source'] = (self.sinks if output else self.sources)[rest[1]]
            return done()
        raise AssertionError(f'the fake server does not know {args}')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RouterCase(unittest.TestCase):
    """A fake server with the buses of both sides, an HDMI output, a headset and a microphone."""

    def setUp(self):
        self.server = server = Server()
        for bus in ('dosplex_left', 'dosplex_left_mic', 'dosplex_right', 'dosplex_right_mic', HDMI, HEADSET):
            server.add_sink(bus)
        server.add_source(MICROPHONE)
        directory = tempfile.TemporaryDirectory(prefix='dosplex-liveness-')
        self.addCleanup(directory.cleanup)
        self.sides = Path(directory.name)
        for side in router.SIDES:
            self.session(side)
        for target, name, value in ((router, 'run_pactl', server.pactl), (router, 'SIDE_ROOT', self.sides),
                                    (router, 'display_state', lambda: {}), (router, 'ATTACH_SECONDS', .05)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.routes = router.empty_routes()
        self.clock = Clock()
        self.monitor = router.Monitor(self.clock)

    def session(self, side, **fields):
        (self.sides / side).mkdir(exist_ok=True)
        (self.sides / side / 'session.json').write_text(json.dumps({'owner': 'deck', **fields}))

    def said(self, function, *args, **kwargs):
        """What `function` writes to the journal, as lines."""
        journal = io.StringIO()
        with contextlib.redirect_stderr(journal):
            function(*args, **kwargs)
        return journal.getvalue().splitlines()

    def passes(self, monitor=True):
        """One pass of the monitor over the loopbacks, a second after the one before."""
        self.clock.advance(1)
        return self.said(router.ensure_loopbacks, self.routes, monitor=self.monitor if monitor else None)

    def route(self, side='left', kind='output', device=HDMI):
        self.routes[side][kind] = device

    def assertQuiet(self, lines):
        self.assertEqual(lines, [])


class LoopbackProblem(unittest.TestCase):
    """The decision itself, from the JSON a server gives."""

    nodes = {'sinks': {'dosplex_left': 11, HDMI: 12, HEADSET: 13},
             'sources': {'dosplex_left.monitor': 11, HDMI + '.monitor': 12, MICROPHONE: 14}}

    def streams(self, sink=12, source=11, module='7', **extra):
        sink_input = dict({'index': 31, 'owner_module': module, 'sink': sink, 'corked': False, 'mute': False}, **extra)
        source_output = dict({'index': 32, 'owner_module': module, 'source': source, 'corked': False, 'mute': False}, **extra)
        return [sink_input], [source_output]

    def problem(self, sink_inputs, source_outputs, module=7, source='dosplex_left.monitor', sink=HDMI):
        return router.loopback_problem(module, source, sink, self.nodes, sink_inputs, source_outputs)

    def test_both_streams_on_their_nodes_are_fine(self):
        self.assertIsNone(self.problem(*self.streams()))
        # An input route: the microphone is the source and the side's microphone bus the sink.
        nodes = {'sinks': {'dosplex_left_mic': 21}, 'sources': {MICROPHONE: 14}}
        sink_inputs, source_outputs = self.streams(sink=21, source=14)
        self.assertIsNone(router.loopback_problem(7, MICROPHONE, 'dosplex_left_mic', nodes, sink_inputs, source_outputs))

    def test_a_stream_on_nothing_is_detached(self):
        sink_inputs, source_outputs = self.streams(sink=NOTHING)
        state, text = self.problem(sink_inputs, source_outputs)
        self.assertEqual(state, 'detached')
        self.assertEqual(text, f'playback stream is on no sink, not on {HDMI} (index 12)')
        sink_inputs, source_outputs = self.streams(source=NOTHING)
        state, text = self.problem(sink_inputs, source_outputs)
        self.assertEqual((state, text), ('detached', 'capture stream is on no source, not on dosplex_left.monitor (index 11)'))

    def test_a_stream_on_the_old_index_of_a_sink_with_the_same_name_is_detached(self):
        # The sink was created again: the name is the same, the index is not.
        state, text = self.problem(*self.streams(sink=6933))
        self.assertEqual((state, text), ('detached', f'playback stream is on sink index 6933, not on {HDMI} (index 12)'))

    def test_a_stream_on_another_node_is_detached(self):
        state, text = self.problem(*self.streams(sink=13))
        self.assertEqual((state, text), ('detached', f'playback stream is on {HEADSET} (index 13), not on {HDMI} (index 12)'))

    def test_a_stream_that_is_not_there_is_missing(self):
        sink_inputs, source_outputs = self.streams()
        self.assertEqual(self.problem([], source_outputs), ('missing', 'no playback stream'))
        self.assertEqual(self.problem(sink_inputs, []), ('missing', 'no capture stream'))

    def test_streams_are_found_by_owner_module_or_by_module_property(self):
        sink_inputs, source_outputs = self.streams(sink=NOTHING)
        # Another loopback's streams do not count, however well they are linked.
        others = self.streams(module='8')
        self.assertEqual(self.problem(sink_inputs + others[0], source_outputs + others[1])[0], 'detached')
        # A stream with no owner_module that carries the module id as a property is the loopback's.
        for stream in sink_inputs + source_outputs:
            stream.update(owner_module=None, properties={'pulse.module.id': '7'})
        self.assertEqual(self.problem(sink_inputs, source_outputs)[0], 'detached')
        self.assertEqual(self.problem(others[0], others[1]), ('missing', 'no playback stream'))

    def test_a_node_that_is_not_there_is_not_judged(self):
        # Loading the loopback again cannot help while its sink is gone; the caller unloads it then.
        nodes = {'sinks': {'dosplex_left': 11}, 'sources': {'dosplex_left.monitor': 11}}
        sink_inputs, source_outputs = self.streams(sink=NOTHING)
        self.assertIsNone(router.loopback_problem(7, 'dosplex_left.monitor', HDMI, nodes, sink_inputs, source_outputs))

    def test_text_from_the_server_cannot_break_the_line(self):
        self.assertEqual(router.plain('Steam\ndosplex audio: fake line\x1b[0m'), 'Steam?dosplex audio: fake line?[0m')
        self.assertEqual(len(router.plain('x' * 500)), 120)
        self.assertTrue(router.plain('x' * 500).endswith('...'))
        nodes = {'sinks': {'a\nb': 12}, 'sources': {'dosplex_left.monitor': 11}}
        sink_inputs, source_outputs = self.streams(sink=NOTHING)
        self.assertNotIn('\n', router.loopback_problem(7, 'dosplex_left.monitor', 'a\nb', nodes, sink_inputs, source_outputs)[1])


class Loopbacks(RouterCase):
    def module(self, route):
        return self.server.loopback(route)[0]['index']

    def test_a_new_route_is_loaded_once_and_then_left_alone(self):
        self.route()
        lines = self.passes()
        module = self.module(('left', 'output'))
        self.assertEqual(lines, [f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {HDMI} '
                                 f'(module {module}), reason: new route'])
        before = list(self.server.commands)
        for _ in range(3):
            self.assertQuiet(self.passes())
        self.assertEqual(self.server.commands, before, 'a loopback that was right was touched')

    def test_a_sink_that_was_created_again_gets_its_loopback_linked_again(self):
        self.route()
        self.passes()
        old = self.server.sinks[HDMI]
        module, playback, capture = self.server.loopback(('left', 'output'))
        self.server.remove_sink(HDMI)
        new = self.server.add_sink(HDMI)  # between two passes, so that no pass sees the sink missing
        self.assertNotEqual(old, new)
        # The defect: the loopback still has the arguments of its route, and its playback stream is on nothing.
        self.assertEqual(router.module_args(module)['sink'], HDMI)
        self.assertEqual(playback['sink'], NOTHING)
        lines = self.passes()
        replacement = self.server.loopback(('left', 'output'))
        self.assertEqual(replacement[1]['sink'], new)
        self.assertEqual(replacement[2]['source'], self.server.sources['dosplex_left.monitor'])
        why = f'stream detached (playback stream is on no sink, not on {HDMI} (index {new}))'
        self.assertEqual(lines, [
            f'dosplex audio: sink {HDMI} was re-created (index {old} -> {new})',
            f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} (module {module["index"]}), reason: {why}',
            f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {HDMI} (module {replacement[0]["index"]}), '
            'reason: stream detached'])
        self.assertQuiet(self.passes())

    def test_a_pass_that_runs_once_repairs_it_too(self):
        # assign and ensure-shared have no monitor: the same repair, and no report of devices.
        self.route()
        self.passes()
        self.server.remove_sink(HDMI)
        new = self.server.add_sink(HDMI)
        lines = self.passes(monitor=False)
        self.assertEqual(self.server.loopback(('left', 'output'))[1]['sink'], new)
        self.assertEqual([line.split(': ', 1)[1].split(' ')[0] for line in lines], ['unloaded', 'loaded'])

    def test_a_loopback_that_is_still_starting_is_waited_for(self):
        # Linking takes a few tens of milliseconds. The pass that loads waits for it, so that no pass takes
        # a loopback that has not been linked yet for a broken one.
        self.route()
        self.server.links = False

        def link(_seconds):
            # The streams get linked while the router waits.
            _, playback, capture = self.server.loopback(('left', 'output'))
            playback['sink'] = self.server.sinks[HDMI]
            capture['source'] = self.server.sources['dosplex_left.monitor']
        with patch.object(time, 'sleep', link):
            lines = self.passes()
        self.assertEqual(len(lines), 1, lines)  # it was loaded, and not said to have failed to link
        self.assertQuiet(self.passes())

    def test_a_sink_that_goes_for_a_pass_and_comes_back(self):
        self.route()
        self.passes()
        module = self.module(('left', 'output'))
        old = self.server.sinks[HDMI]
        self.server.remove_sink(HDMI)
        lines = self.passes()
        self.assertEqual(lines, [
            f'dosplex audio: sink {HDMI} disappeared (was index {old})',
            f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} (module {module}), '
            f'reason: {HDMI} is not available'])
        self.assertQuiet(self.passes())
        new = self.server.add_sink(HDMI)
        lines = self.passes()
        self.assertEqual(lines[0], f'dosplex audio: sink {HDMI} appeared (index {new})')
        self.assertTrue(lines[1].endswith('reason: new route'), lines)
        self.assertEqual(self.server.loopback(('left', 'output'))[1]['sink'], new)

    def test_a_claimed_output_that_goes_falls_back_to_the_default_and_the_claim_wins_when_it_is_back(self):
        self.server.default = HEADSET
        self.route()
        self.passes()
        self.server.remove_sink(HDMI)
        self.passes()
        self.assertEqual(self.routes['left']['output'], HDMI)  # the claim is kept
        _, playback, _ = self.server.loopback(('left', 'output'))
        self.assertEqual(playback['sink'], self.server.sinks[HEADSET])
        self.assertQuiet(self.passes())
        new = self.server.add_sink(HDMI)
        self.passes()
        self.assertEqual(self.server.loopback(('left', 'output'))[1]['sink'], new)

    def test_without_a_usable_default_a_gone_output_stays_silent(self):
        self.route()
        self.passes()
        self.server.remove_sink(HDMI)
        for default in (None, 'dosplex_right', 'not-there'):
            self.server.default = default
            lines = self.passes()
            self.assertTrue(all('loaded' not in line or 'unloaded' in line for line in lines), lines)
        self.assertEqual([m for m in self.server.modules if m['name'] == 'module-loopback'], [])

    def test_a_stream_that_is_missing_loads_the_loopback_again(self):
        self.route()
        self.passes()
        module = self.module(('left', 'output'))
        self.server.sink_inputs.clear()
        lines = self.passes()
        self.assertEqual(lines[0], f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} '
                                   f'(module {module}), reason: stream missing (no playback stream)')
        self.assertTrue(lines[1].endswith('reason: stream missing'), lines)
        self.assertEqual(self.server.loopback(('left', 'output'))[1]['sink'], self.server.sinks[HDMI])

    def test_the_capture_side_follows_the_side_bus(self):
        self.route()
        self.passes()
        self.server.remove_sink('dosplex_left')
        bus = self.server.add_sink('dosplex_left')
        lines = self.passes()
        self.assertIn('reason: stream detached (capture stream is on no source, not on dosplex_left.monitor '
                      f'(index {bus}))', lines[0])
        self.assertEqual(self.server.loopback(('left', 'output'))[2]['source'], bus)

    def test_an_input_route_follows_its_microphone(self):
        self.route(kind='input', device=MICROPHONE)
        self.passes()
        old = self.server.sources[MICROPHONE]
        self.server.remove_source(MICROPHONE)
        new = self.server.add_source(MICROPHONE)
        lines = self.passes()
        self.assertEqual(lines[0], f'dosplex audio: source {MICROPHONE} was re-created (index {old} -> {new})')
        self.assertIn(f'unloaded loopback left input {MICROPHONE} -> dosplex_left_mic', lines[1])
        self.assertIn(f'capture stream is on no source, not on {MICROPHONE} (index {new})', lines[1])
        self.assertEqual(self.server.loopback(('left', 'input'))[2]['source'], new)

    def test_standby_silences_a_side_and_waking_it_loads_the_loopback(self):
        self.route()
        self.passes()
        module = self.module(('left', 'output'))
        self.session('left', lifecycle='standby')
        self.assertEqual(self.passes(), [f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} '
                                         f'(module {module}), reason: side is standby'])
        self.assertQuiet(self.passes())
        self.session('left', lifecycle='running')
        self.assertTrue(self.passes()[0].endswith('reason: new route'))

    def test_a_changed_route_replaces_the_loopback(self):
        self.route()
        self.passes()
        module = self.module(('left', 'output'))
        self.route(device=HEADSET)
        lines = self.passes()
        self.assertEqual(lines[0], f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} '
                                   f'(module {module}), reason: arguments changed (source or sink)')
        self.assertTrue(lines[1].startswith(f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {HEADSET} '))
        self.assertTrue(lines[1].endswith('reason: arguments changed'))

    def test_a_released_route_goes(self):
        self.route()
        self.passes()
        module = self.module(('left', 'output'))
        self.route(device=None)
        self.assertEqual(self.passes(), [f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {HDMI} '
                                         f'(module {module}), reason: route released'])

    def test_the_shared_mix_is_named(self):
        self.route()
        self.passes()
        self.routes['shared'].update(mode='full', output=HDMI)
        self.routes['left']['shared'] = True
        lines = self.passes()
        self.assertIn('reason: arguments changed (shared mix on)', lines[0])
        self.assertIn('loaded loopback left output (shared mix) dosplex_left.monitor', lines[1])
        self.routes['shared'].update(mode='off', output=None)
        self.routes['left']['shared'] = False
        self.assertIn('reason: arguments changed (shared mix off)', self.passes()[0])

    arguments = ['source=dosplex_left.monitor', f'sink={HDMI}', 'source_dont_move=true', 'sink_dont_move=true',
                 'latency_msec=30']

    def test_a_loopback_from_before_the_stream_flags_is_replaced(self):
        self.route()
        self.server.load('module-loopback', self.arguments)
        lines = self.passes()
        self.assertIn('reason: arguments changed (stream flags)', lines[0])
        self.assertTrue(lines[1].endswith('reason: arguments changed'))

    def test_a_second_loopback_for_a_route_is_unloaded(self):
        self.route()
        self.passes()
        duplicate = self.server.load('module-loopback', [*self.arguments, 'sink_input_properties="'
                                                         + router.loopback_properties(False) + '"'])
        lines = self.passes()
        self.assertEqual(len(lines), 1)
        self.assertIn(f'(module {duplicate}), reason: duplicate loopback', lines[0])

    def test_a_loopback_that_will_not_link_is_left_alone_for_a_while(self):
        self.route()
        self.server.links = False
        lines = self.passes()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].endswith('reason: new route'))
        self.assertIn(f'the left output loopback (module {self.module(("left", "output"))}) was not linked within '
                      '0.05 seconds of loading: playback stream is on no sink', lines[1])
        self.assertTrue(lines[1].endswith('; leaving it for 30 seconds'))
        for _ in range(5):
            self.assertQuiet(self.passes())  # no loading again every second
        self.clock.advance(30)
        lines = self.passes()
        self.assertEqual([line.split(': ', 1)[1].split(' ')[0] for line in lines[:2]], ['unloaded', 'loaded'])
        self.assertIn('reason: stream detached', lines[0])
        # That the new one did not link either was said 37 seconds ago, and is not said again yet.
        self.assertEqual(len(lines), 2)
        self.assertQuiet(self.passes())

    def test_a_loopback_that_links_ends_the_wait_for_its_route(self):
        self.route()
        self.server.links = False
        self.passes()  # loaded and not linked: left alone for 30 seconds
        self.server.links = True
        self.route(device=HEADSET)
        self.passes()  # a changed route loads it again at once, and this one links
        self.server.remove_sink(HEADSET)
        new = self.server.add_sink(HEADSET)
        self.passes()  # so a later fault is not held back by the wait of the one before
        self.assertEqual(self.server.loopback(('left', 'output'))[1]['sink'], new)

    def test_a_stream_that_was_muted_for_more_than_five_seconds_is_unmuted(self):
        self.route()
        self.passes()
        module, playback, capture = self.server.loopback(('left', 'output'))
        playback['mute'] = capture['mute'] = True
        self.assertQuiet(self.passes())
        self.clock.advance(3.9)
        self.assertQuiet(self.passes())
        self.assertTrue(playback['mute'] and capture['mute'])
        self.clock.advance(1.5)  # more than five seconds since it was first seen
        lines = self.passes()
        self.assertEqual(sorted(lines), sorted([
            f'dosplex audio: unmuted the playback stream of the left output loopback (module {module["index"]}), muted for more than 5 seconds',
            f'dosplex audio: unmuted the capture stream of the left output loopback (module {module["index"]}), muted for more than 5 seconds']))
        self.assertFalse(playback['mute'] or capture['mute'])
        self.assertEqual(self.server.loopback(('left', 'output'))[0], module, 'the loopback was loaded again')
        self.assertQuiet(self.passes())

    def test_five_seconds_exactly_is_not_yet_more_than_five(self):
        self.route()
        self.passes()
        playback = self.server.loopback(('left', 'output'))[1]
        playback['mute'] = True
        self.assertQuiet(self.passes())
        first_seen = self.clock.now
        for seconds, unmuted in ((5.0, False), (5.01, True)):
            self.clock.now = first_seen + seconds
            lines = self.said(router.ensure_loopbacks, self.routes, monitor=self.monitor)
            self.assertEqual((len(lines), playback['mute']), (1 if unmuted else 0, not unmuted))

    def test_a_mute_that_does_not_last_is_left_alone(self):
        self.route()
        self.passes()
        playback = self.server.loopback(('left', 'output'))[1]
        playback['mute'] = True
        self.assertQuiet(self.passes())
        self.clock.advance(3)
        playback['mute'] = False  # gone before five seconds
        self.assertQuiet(self.passes())
        self.clock.advance(3)
        playback['mute'] = True  # the time starts again
        self.assertQuiet(self.passes())
        self.assertNotIn(('set-sink-input-mute', str(playback['index']), '0'), self.server.commands)

    def test_a_stream_that_is_normal_again_starts_its_time_afresh(self):
        # Even if the passes in between never got to the end: the time is kept per stream, not per pass.
        playback = {'index': 31, 'owner_module': '7', 'mute': True, 'corked': False}
        def watch():
            self.monitor.watch_streams(('left', 'output'), 'left output', 7, [playback], [], set())
        self.assertQuiet(self.said(watch))
        self.clock.advance(4)
        playback['mute'] = False
        self.assertQuiet(self.said(watch))
        self.clock.advance(4)
        playback['mute'] = True
        self.assertQuiet(self.said(watch))
        self.clock.advance(4)  # eight seconds since the first sight of a mute, but only four of this one
        self.assertQuiet(self.said(watch))
        self.assertTrue(playback['mute'])

    def test_a_stream_that_keeps_being_muted_is_said_once_a_minute(self):
        self.route()
        self.passes()
        playback = self.server.loopback(('left', 'output'))[1]
        lines = []
        for _ in range(140):
            playback['mute'] = True  # whatever unmuted it, something mutes it again
            lines += self.passes()
        # It is unmuted every seventh second, and said the first time and then once a minute.
        self.assertEqual(len(lines), 3, lines)
        self.assertNotIn('repeated', lines[0])
        for line in lines[1:]:
            self.assertTrue(line.endswith('muted for more than 5 seconds (repeated 8 times since it was last said)'), line)

    def test_a_corked_stream_is_reported_once_and_left_as_it_is(self):
        self.route()
        self.passes()
        module, playback, capture = self.server.loopback(('left', 'output'))
        commands = list(self.server.commands)
        capture['corked'] = True
        self.assertQuiet(self.passes())
        self.clock.advance(5)
        lines = self.passes()
        self.assertEqual(lines, [f'dosplex audio: the capture stream of the left output loopback (module {module["index"]}) '
                                 'has been corked for more than 5 seconds; left as it is'])
        for _ in range(10):
            self.assertQuiet(self.passes())
        self.assertEqual(self.server.commands, commands)
        capture['corked'] = False
        self.assertQuiet(self.passes())
        capture['corked'] = True  # a new episode is reported again
        self.passes()
        self.clock.advance(6)
        self.assertEqual(len(self.passes()), 1)

    def test_idle_and_playing_loopbacks_are_normal(self):
        # tests/audio-integration.py shows it with PipeWire: a loopback runs, neither corked nor muted,
        # whether or not anything plays. Nothing is reported for that, however long it lasts.
        self.route()
        self.route(kind='input', device=MICROPHONE)
        self.passes()
        for _ in range(30):
            self.clock.advance(1)
            self.assertQuiet(self.passes())

    def test_devices_that_come_and_go_are_said_and_the_first_pass_says_nothing(self):
        self.assertQuiet(self.passes())
        self.server.add_sink('bluez_output.00_11_22_33_44_55.1')
        self.server.add_source('alsa_input.pci-0000_00_1f.3.analog-stereo')
        lines = self.passes()
        self.assertEqual(lines, [f'dosplex audio: sink bluez_output.00_11_22_33_44_55.1 appeared (index {self.server.serial - 1})',
                                 f'dosplex audio: source alsa_input.pci-0000_00_1f.3.analog-stereo appeared (index {self.server.serial})'])
        self.server.remove_sink(HEADSET)
        self.assertEqual(len(self.passes()), 1)
        # The router's own buses and the monitors of sinks are not devices a player can choose.
        self.server.add_sink('dosplex_right_extra')
        self.assertQuiet(self.passes())

    def test_buses_are_said_when_they_are_created(self):
        self.server.remove_sink('dosplex_right')
        lines = self.said(router.ensure_per_side_sinks)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^dosplex audio: created bus dosplex_right \(module \d+\)$')
        self.assertQuiet(self.said(router.ensure_per_side_sinks))


class SharedAudio(RouterCase):
    def test_a_pass_of_the_monitor_says_what_it_changes_and_nothing_when_it_changes_nothing(self):
        with tempfile.TemporaryDirectory(prefix='dosplex-liveness-state-') as state, \
                patch.object(router, 'STATE_PATH', Path(state) / 'audio-routes.json'), \
                patch.object(router, 'PERSIST_PATH', Path(state) / 'audio.json'), \
                patch.object(router, 'ensure_user_audio_server'):
            self.route()
            router.save_routes(self.routes)
            self.server.add_application('output', 'Steam', HEADSET, **{'dosplex.side': 'left'})
            steam = self.server.sink_inputs[0]
            lines = self.said(router.ensure_shared_audio, self.monitor)
            tcp = next(m['index'] for m in self.server.modules if m['name'] == router.TCP_MODULE)
            module = self.module_of(('left', 'output'))
            self.assertEqual(lines, [
                f'dosplex audio: loaded {router.TCP_MODULE} (module {tcp})',
                f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {HDMI} (module {module}), reason: new route',
                f'dosplex audio: moved left output stream "Steam" (index {steam["index"]}): {HEADSET} -> dosplex_left'])
            for _ in range(3):
                self.assertQuiet(self.said(router.ensure_shared_audio, self.monitor))

    def module_of(self, route):
        return self.server.loopback(route)[0]['index']


class Streams(RouterCase):
    def test_a_stream_that_is_moved_is_said(self):
        self.server.add_application('output', 'Steam', HDMI, **{'dosplex.side': 'left'})
        self.server.add_application('input', 'Discord', MICROPHONE, **{'dosplex.side': 'right'})
        stream = self.server.sink_inputs[0]
        capture = self.server.source_outputs[0]
        lines = self.said(router.route_streams)
        self.assertEqual(lines, [
            f'dosplex audio: moved left output stream "Steam" (index {stream["index"]}): {HDMI} -> dosplex_left',
            f'dosplex audio: moved right input stream "Discord" (index {capture["index"]}): {MICROPHONE} '
            '-> dosplex_right_mic.monitor'])
        self.assertEqual((stream['sink'], capture['source']),
                         (self.server.sinks['dosplex_left'], self.server.sources['dosplex_right_mic.monitor']))
        self.assertQuiet(self.said(router.route_streams))

    def test_the_name_of_an_application_cannot_forge_a_line(self):
        self.server.add_application('output', 'Game"\ndosplex audio: sink x disappeared\x07', HDMI, **{'dosplex.side': 'left'})
        [line] = self.said(router.route_streams)
        self.assertIn('moved left output stream "Game"?dosplex audio: sink x disappeared?"', line)

    def test_a_stream_with_no_name_and_one_that_was_linked_to_nothing(self):
        stream = self.server.add_application('output', '', HDMI, **{'dosplex.side': 'left', 'media.name': 'Playback'})
        stream['properties'].pop('application.name')
        stream['sink'] = NOTHING
        [line] = self.said(router.route_streams)
        self.assertEqual(line, f'dosplex audio: moved left output stream "Playback" (index {stream["index"]}): no sink -> dosplex_left')

    def test_a_stream_that_ended_before_its_move_is_skipped(self):
        # Stock Steam's streams close while a start routes them; the move answers "No such entity".
        gone = self.server.add_application('output', 'Steam', HDMI, **{'dosplex.side': 'left'})
        gone['ended'] = True
        kept = self.server.add_application('output', 'Game', HDMI, **{'dosplex.side': 'left'})
        lines = self.said(router.route_streams)
        self.assertEqual(lines, [
            f'dosplex audio: skipped left output stream "Steam" (index {gone["index"]}): it ended before it could be moved',
            f'dosplex audio: moved left output stream "Game" (index {kept["index"]}): {HDMI} -> dosplex_left'])
        self.assertEqual(kept['sink'], self.server.sinks['dosplex_left'])

    def test_streams_of_loopbacks_and_unknown_streams_are_left_alone(self):
        self.route()
        self.passes()
        self.server.add_application('output', 'Some game', HDMI)  # no side: not ours to move
        self.assertQuiet(self.said(router.route_streams))
        self.assertNotIn('move-sink-input', [command[0] for command in self.server.commands])


class Journal(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.monitor = router.Monitor(self.clock)

    def said(self, function, *args):
        journal = io.StringIO()
        with contextlib.redirect_stderr(journal):
            function(*args)
        return journal.getvalue().splitlines()

    def test_a_failure_that_goes_on_is_said_once_a_minute(self):
        lines = []
        for _ in range(130):
            lines += self.said(self.monitor.failure, 'audio', 'Cannot query sinks')
            self.clock.advance(1)
        self.assertEqual(lines, ['dosplex audio: Cannot query sinks',
                                 'dosplex audio: Cannot query sinks (repeated 59 times since it was last said)',
                                 'dosplex audio: Cannot query sinks (repeated 59 times since it was last said)'])

    def test_a_different_message_is_said_at_once_and_so_is_the_end_of_a_failure(self):
        self.assertEqual(self.said(self.monitor.failure, 'audio', 'Cannot query sinks'), ['dosplex audio: Cannot query sinks'])
        self.clock.advance(1)
        self.assertEqual(self.said(self.monitor.failure, 'audio', 'Cannot query sinks'), [])
        self.assertEqual(self.said(self.monitor.failure, 'audio', 'Audio volume did not settle'),
                         ['dosplex audio: Audio volume did not settle'])
        self.assertEqual(self.said(self.monitor.failure, 'guest endpoint', 'Guest endpoint: pipewire did not appear'),
                         ['dosplex audio: Guest endpoint: pipewire did not appear'])
        self.assertEqual(self.said(self.monitor.recovered, 'audio'), ['dosplex audio: recovered: Audio volume did not settle'])
        self.assertEqual(self.said(self.monitor.recovered, 'audio'), [])  # said once
        self.assertEqual(self.said(self.monitor.recovered, 'never failed'), [])

    def test_a_journal_that_cannot_be_written_to_does_not_stop_the_routing(self):
        closed = io.StringIO()
        closed.close()
        with contextlib.redirect_stderr(closed):
            router.say('nobody reads this')
            self.monitor.failure('audio', 'nor this')

    def test_a_message_cannot_run_over_lines(self):
        [line] = self.said(self.monitor.failure, 'audio', 'Failure: Connection refused\ndosplex audio: fake\n')
        self.assertEqual(line, 'dosplex audio: Failure: Connection refused dosplex audio: fake')

    def run_monitor(self, failures):
        """Run the real loop for `failures` seconds of a pass that fails, then for ten that work."""
        cycles = []

        class Stop(BaseException):
            pass

        def ensure(monitor):
            cycles.append(monitor)
            if len(cycles) <= failures:
                raise RuntimeError('Audio command failed: load-module')

        def sleep(seconds):
            self.clock.advance(seconds)
            if len(cycles) >= failures + 10:
                raise Stop

        journal = io.StringIO()
        with patch.object(router, 'ensure_shared_audio', ensure), patch.object(time, 'sleep', sleep), \
                patch.object(router, 'guest_endpoint_from_environment', lambda: None), contextlib.redirect_stderr(journal):
            with self.assertRaises(Stop):
                router.monitor_shared_audio(self.monitor)
        self.assertEqual({id(monitor) for monitor in cycles}, {id(self.monitor)})
        return journal.getvalue().splitlines()

    def test_the_monitor_loop_says_a_failure_once_a_minute_and_when_it_ends(self):
        self.assertEqual(self.run_monitor(130), [
            'dosplex audio: Audio command failed: load-module',
            'dosplex audio: Audio command failed: load-module (repeated 59 times since it was last said)',
            'dosplex audio: Audio command failed: load-module (repeated 59 times since it was last said)',
            'dosplex audio: recovered: Audio command failed: load-module'])

    def test_the_monitor_loop_says_nothing_while_all_is_well(self):
        self.assertEqual(self.run_monitor(0), [])


if __name__ == '__main__':
    unittest.main()
