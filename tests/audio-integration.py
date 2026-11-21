#!/usr/bin/env python3
# dosplex-test: tier=integration needs=pipewire-audio timeout=300
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise real Pulse routing in a disposable PipeWire server, with no hardware."""
import importlib.util
import array
import contextlib
import io
import math
import wave
import json
import os
from pathlib import Path
import pwd
import shutil
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RATE = 48000


def write_tone(path, frequency, seconds=10, amplitude=5000):
    samples = array.array('h', (int(amplitude * math.sin(2 * math.pi * frequency * i / RATE))
                                for i in range(RATE * seconds)))
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(samples.tobytes())
    return str(path)


def tone_levels(process, frequencies):
    """Amplitude of each frequency in the last second of a two second mono s16le capture."""
    want = RATE * 2 * 2
    data = bytearray()
    deadline = time.monotonic() + 10
    while len(data) < want:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise AssertionError('guest audio capture timed out')
        chunk = os.read(process.stdout.fileno(), min(65536, want - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    process.terminate()
    process.wait(timeout=3)
    values = array.array('h')
    values.frombytes(bytes(data[:len(data) // 2 * 2]))
    assert len(values) >= RATE, 'insufficient audio samples'
    values = values[-RATE:]
    levels = {}
    for frequency in frequencies:
        real = sum(v * math.cos(2 * math.pi * frequency * i / RATE) for i, v in enumerate(values))
        imag = sum(v * math.sin(2 * math.pi * frequency * i / RATE) for i, v in enumerate(values))
        levels[frequency] = 2 * math.hypot(real, imag) / len(values)
    return levels


def recreated_output_checks(router, root, start, pactl):
    """The TV's HDMI handshake removes the Steam Machine's HDMI output and creates it again under the same name,
    with a new index. A loopback that was loaded against the old sink keeps its arguments but is linked to
    nothing, so its route is silent. The monitor has to notice and load the loopback again. A null sink stands
    in for the output. The monitor is the real loop: its pause between two passes is where this check touches
    the graph, so the sink goes and comes back between two passes, as it did on the Machine."""
    hdmi = 'alsa_output.pci-0000_03_00.1.hdmi-stereo'
    saved = router.load_routes()
    tone = write_tone(root / 'handshake-440.wav', 440)
    modules = {}  # what this check loads, to unload it at the end

    def index_of(kind, name):
        return next(n['index'] for n in router.query(kind) if n['name'] == name)

    def left_loopback():
        """The module that carries Main's sound, and the two streams that module owns."""
        module = next(m['index'] for m in router.query('modules') if router.managed_route(m) == ('left', 'output'))
        [playback] = [s for s in router.query('sink-inputs') if str(s.get('owner_module')) == str(module)]
        [capture] = [s for s in router.query('source-outputs') if str(s.get('owner_module')) == str(module)]
        return module, playback, capture

    def attached():
        _, playback, capture = left_loopback()
        return (playback['sink'] == index_of('sinks', hdmi)
                and capture['source'] == index_of('sources', 'dosplex_left.monitor'))

    def level(sink):
        """How loud the 440 Hz tone that plays into Main's bus is on the monitor of `sink`, and whether the
        streams of Main's loopback are (corked, muted) while it plays."""
        player = start(['paplay', '--device=dosplex_left', tone])
        try:
            recorder = start(['parec', '--raw', '--format=s16le', '--channels=1', '--rate=48000',
                              f'--device={sink}.monitor'], stdout=subprocess.PIPE)
            loudness = tone_levels(recorder, (440,))[440]
            return loudness, [(s['corked'], s['mute']) for s in left_loopback()[1:]]
        finally:
            player.terminate()
            player.wait(timeout=3)

    class Done(BaseException):
        pass

    def run_monitor(after_pass):
        """Run the real monitor loop. `after_pass(n, journal)` runs in the pause after the nth pass and ends the
        loop by returning True. That is where this check changes the graph or looks at it. Returns the journal."""
        journal, passes, sleep = io.StringIO(), [], time.sleep

        def pause(seconds):
            if seconds < 1:
                return sleep(seconds)  # the router's own short waits
            passes.append(seconds)
            if after_pass(len(passes), journal.getvalue().splitlines()):
                raise Done
            sleep(seconds)
        # The shared server's TCP listener and the user's audio services are not what this check is about:
        # the listener is loaded below on a free port, and the services must not be started from a test.
        with patch.dict(os.environ), patch.object(router, 'ensure_user_audio_server'), \
                patch.object(time, 'sleep', pause), contextlib.redirect_stderr(journal):
            os.environ.pop('DOSPLEX_GUEST_AUDIO_DIR', None)
            try:
                router.monitor_shared_audio()
            except Done:
                pass
        return journal.getvalue().splitlines()

    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    modules['tcp'] = pactl('load-module', 'module-native-protocol-tcp', f'port={port}', 'listen=127.0.0.1')
    modules['hdmi'] = pactl('load-module', 'module-null-sink', f'sink_name={hdmi}')
    try:
        # Main moves from test_tv to the stand-in. The loopback is replaced, and the journal says why.
        journal = io.StringIO()
        with contextlib.redirect_stderr(journal):
            router.assign_audio('left', hdmi)
        lines = journal.getvalue().splitlines()
        assert [line.split(': ', 1)[1].split(' ')[0] for line in lines] == ['unloaded', 'loaded'], lines
        assert '-> test_tv (module ' in lines[0] and lines[0].endswith('reason: arguments changed (source or sink)'), lines
        assert f'-> {hdmi} (module ' in lines[1] and lines[1].endswith('reason: arguments changed'), lines
        assert attached()
        # What a loopback that works looks like with PipeWire: it runs whether or not anything plays, so
        # neither of its streams is corked or muted. Only that is reported when it lasts.
        _, playback, capture = left_loopback()
        assert [(s['corked'], s['mute']) for s in (playback, capture)] == [(False, False)] * 2, (playback, capture)
        loudness, states = level(hdmi)
        assert loudness > 500 and states == [(False, False)] * 2, (loudness, states)

        # The handshake: the sink goes and comes back under its name between two passes.
        before = {'old': index_of('sinks', hdmi), 'module': left_loopback()[0]}

        def handshake(n, journal):
            if n == 1:
                assert journal == [], journal  # a pass that changed nothing said nothing
                pactl('unload-module', modules['hdmi'])
                modules['hdmi'] = pactl('load-module', 'module-null-sink', f'sink_name={hdmi}')
                before['new'] = index_of('sinks', hdmi)
                assert before['new'] != before['old']
                # The defect: the arguments of the loopback still match its route, its streams exist, and
                # the playback stream is linked to nothing, so the tone does not come out of the new sink.
                _, playback, _ = left_loopback()
                assert playback['sink'] == router.UNATTACHED, playback
                assert level(hdmi)[0] < 100
                return False
            if attached():
                before['passes'] = n - 1
                return True
            assert n < 4, 'the loopback is not linked to the new sink within 3 monitor passes'
            return False
        lines = run_monitor(handshake)
        module, playback, capture = left_loopback()
        assert playback['sink'] == before['new'] and capture['source'] == index_of('sources', 'dosplex_left.monitor')
        why = f'stream detached (playback stream is on no sink, not on {hdmi} (index {before["new"]}))'
        assert lines == [
            f'dosplex audio: sink {hdmi} was re-created (index {before["old"]} -> {before["new"]})',
            f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {hdmi} (module {before["module"]}), reason: {why}',
            f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {hdmi} (module {module}), '
            'reason: stream detached'], lines
        assert before['passes'] <= 3, before
        loudness, states = level(hdmi)
        assert loudness > 500 and states == [(False, False)] * 2, (loudness, states)

        # The output is gone for one pass and comes back: while it is gone the side plays on the shared default
        # (AUD-N-04), and the claim wins again when the output is back.
        gone = {'index': index_of('sinks', hdmi), 'module': module}

        def returns(n, journal):
            if n == 1:
                pactl('unload-module', modules['hdmi'])
            elif n == 2:
                # The claimed output is gone: the loopback is not dropped, it plays on the shared default.
                default = router.pactl_checked('get-default-sink')
                gone['fallback'], playback, _ = left_loopback()
                assert playback['sink'] == index_of('sinks', default), (default, playback)
                modules['hdmi'] = pactl('load-module', 'module-null-sink', f'sink_name={hdmi}')
                gone['new'] = index_of('sinks', hdmi)
            elif n >= 3:
                assert n < 6, 'the loopback is not linked to the output that came back'
                return attached()
            return False
        lines = run_monitor(returns)
        default = router.pactl_checked('get-default-sink')
        module = left_loopback()[0]
        assert lines == [
            f'dosplex audio: sink {hdmi} disappeared (was index {gone["index"]})',
            f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {hdmi} (module {gone["module"]}), '
            'reason: arguments changed (source or sink)',
            f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {default} (module {gone["fallback"]}), '
            'reason: arguments changed',
            f'dosplex audio: sink {hdmi} appeared (index {gone["new"]})',
            f'dosplex audio: unloaded loopback left output dosplex_left.monitor -> {default} (module {gone["fallback"]}), '
            'reason: arguments changed (source or sink)',
            f'dosplex audio: loaded loopback left output dosplex_left.monitor -> {hdmi} (module {module}), '
            'reason: arguments changed'], lines
        assert level(hdmi)[0] > 500

        # A stream that stays muted for more than five seconds is unmuted where it is, not loaded again: a
        # new capture stream would get the mute of the one before from WirePlumber. The clock is a stand-in.
        now = [0.0]
        memory = router.Monitor(lambda: now[0])
        module, playback, capture = left_loopback()
        pactl('set-sink-input-mute', str(playback['index']), '1')
        pactl('set-source-output-mute', str(capture['index']), '1')

        def look(seconds):
            now[0] += seconds
            journal = io.StringIO()
            with contextlib.redirect_stderr(journal):
                router.ensure_loopbacks(monitor=memory)
            return journal.getvalue().splitlines(), left_loopback()
        for seconds in (0, 4):
            lines, (same, playback, capture) = look(seconds)
            assert lines == [] and same == module and playback['mute'] and capture['mute'], (seconds, lines)
        lines, (same, playback, capture) = look(2)
        assert same == module, 'the loopback was loaded again'
        assert not playback['mute'] and not capture['mute'], (playback, capture)
        assert sorted(lines) == sorted(
            f'dosplex audio: unmuted the {role} stream of the left output loopback (module {module}), '
            'muted for more than 5 seconds' for role in ('playback', 'capture')), lines
        assert look(10)[0] == []
    finally:
        with contextlib.redirect_stderr(io.StringIO()):
            router.assign_audio('left', 'test_tv')
        for name in ('hdmi', 'tcp'):
            pactl('unload-module', modules[name])
    assert router.load_routes() == saved


def guest_endpoint_checks(router, root, env, start, pactl, log):
    """Guest's endpoint offers one output and one microphone and cannot touch the shared server."""
    shared = env['PULSE_SERVER']

    def on(server, *args, **extra):
        return subprocess.run(['pactl', *args], env=dict(env, PULSE_SERVER=server, **extra),
                              capture_output=True, text=True, timeout=10)

    def names(server, kind, **extra):
        result = on(server, '-f', 'json', 'list', kind, **extra)
        assert result.returncode == 0, result.stderr
        return sorted(item['name'] for item in json.loads(result.stdout))

    def record(server, device):
        return start(['parec', '--server=' + server, '--raw', '--format=s16le', '--channels=1',
                      '--rate=48000', '--device=' + device], stdout=subprocess.PIPE)

    def measure(recorders, frequencies):
        with ThreadPoolExecutor(max_workers=len(recorders)) as pool:
            levels = list(pool.map(lambda process: tone_levels(process, frequencies), recorders.values()))
        return dict(zip(recorders, levels))

    tones = (440, 880, 1320, 1760)  # Guest's sound, Main's sound, Guest's microphone, Main's microphone

    def only(levels, name, wanted):
        assert levels[name][wanted] > 500, (name, levels[name])
        assert all(levels[name][f] < 100 for f in tones if f != wanted), (name, levels[name])

    # Everything the endpoint is built from is validated before anything starts.
    base = dict(upstream='tcp:127.0.0.1:4713', side='right', latency_ms=60)
    config = router.guest_pipewire_config(**base)
    for wanted in ('"dosplex_right"', '"dosplex_right_mic"', '"dosplex_right_mic.monitor"', '"Dosplex sound"', '"Dosplex microphone"',
                   'tunnel.mode = sink', 'tunnel.mode = source', 'reconnect.interval.ms = 1000'):
        assert wanted in config, wanted
    assert 'dosplex_left' not in config
    assert 'pulse.allow-module-loading = false' in router.GUEST_PULSE_CONFIG
    assert 'module-always-sink' not in router.GUEST_PULSE_CONFIG
    for bad in (dict(upstream='tcp:127.0.0.1:4713; evil'), dict(upstream='unix:relative'), dict(upstream='unix:/a b'),
                dict(upstream='tcp:host:99999999'), dict(upstream=None), dict(side='top'),
                dict(latency_ms=5), dict(latency_ms=5000), dict(latency_ms='60')):
        try:
            router.guest_pipewire_config(**{**base, **bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f'accepted {bad}')
    if os.geteuid() == 0:
        print('SKIP: the endpoint never runs as root, so only the privileged checks below run as root')
        return False
    # The audio service switches the endpoint on with DOSPLEX_GUEST_AUDIO_DIR and tunes it with two more variables.
    with patch.dict(os.environ):
        os.environ.pop('DOSPLEX_GUEST_AUDIO_DIR', None)
        assert router.guest_endpoint_from_environment() is None
        os.environ.update(DOSPLEX_GUEST_AUDIO_DIR=str(root / 'from-env'), DOSPLEX_GUEST_AUDIO_LATENCY_MS='80',
                          DOSPLEX_GUEST_AUDIO_UPSTREAM='unix:/run/example/native')
        configured = router.guest_endpoint_from_environment()
        assert configured.directory == root / 'from-env' and configured.config_directory == root / 'from-env.conf'
        assert 'pulse.latency = 80' in configured.files['pipewire.conf']
        assert '"unix:/run/example/native"' in configured.files['pipewire.conf']
        os.environ['DOSPLEX_GUEST_AUDIO_LATENCY_MS'] = 'fast'
        try:
            router.guest_endpoint_from_environment()
        except ValueError:
            pass
        else:
            raise AssertionError('accepted a latency that is not a number')
        os.environ.update(DOSPLEX_GUEST_AUDIO_LATENCY_MS='60', DOSPLEX_GUEST_AUDIO_DIR='relative/audio')
        try:
            router.guest_endpoint_from_environment()
        except ValueError:
            pass
        else:
            raise AssertionError('accepted a relative directory')
    # A WirePlumber that cannot start fails the start visibly, and is not retried at once.
    fake = root / 'fake-bin'
    fake.mkdir()
    for name in ('pipewire', 'pipewire-pulse'):
        (fake / name).symlink_to(shutil.which(name))
    (fake / 'wireplumber').write_text('#!/bin/sh\nexit 3\n')
    (fake / 'wireplumber').chmod(0o755)
    broken = router.GuestEndpoint(root / 'broken-audio', upstream=env['PULSE_SERVER'], log=log)
    with patch.object(router, 'CHILD_PATH', str(fake)):
        try:
            broken.start()
        except RuntimeError as error:
            assert 'wireplumber exited with status 3' in str(error), error
        else:
            raise AssertionError('a WirePlumber that exits at once did not fail the start')
        assert broken.children == [] and not (broken.directory / 'pulse' / 'native').is_socket()
        assert broken.ensure() is False  # and it is not restarted in a tight loop
    # As root the endpoint runs only as an ordinary Guest account, never as root.
    with patch.object(os, 'geteuid', return_value=0):
        for owner in (None, SimpleNamespace(pw_uid=0, pw_gid=0), SimpleNamespace(pw_uid=999, pw_gid=999)):
            try:
                router.GuestEndpoint(root / 'unused', owner=owner).identity()
            except RuntimeError:
                pass
            else:
                raise AssertionError(f'would run the Guest endpoint as {owner}')
        assert router.GuestEndpoint(root / 'unused', owner=SimpleNamespace(pw_uid=1001, pw_gid=1002)).identity() == (1001, 1002)
    # The directories are checked and fixed through open handles, never followed through links.
    loose = root / 'loose'
    loose.mkdir(mode=0o775)
    loose.chmod(0o775)
    try:
        router.GuestEndpoint(loose / 'audio').prepare()
    except PermissionError:
        pass
    else:
        raise AssertionError('accepted a parent directory that others can write to')
    elsewhere = root / 'elsewhere'
    elsewhere.mkdir()
    (root / 'linked').symlink_to(elsewhere)
    try:
        router.GuestEndpoint(root / 'linked').prepare()
    except OSError:
        pass
    else:
        raise AssertionError('followed a symbolic link')
    assert list(elsewhere.iterdir()) == []

    modules = pactl('list', 'short', 'modules')
    unclaimed = [(m['name'], m['argument']) for m in router.query('modules') if not router.managed_route(m)]
    shared_sinks = names(shared, 'sinks')
    defaults = (pactl('get-default-sink'), pactl('get-default-source'))
    dosplex_left_volume = pactl('get-sink-volume', 'dosplex_left')
    # Control: the shared server, which Guest's Steam used until now, fails the same check.
    shared_problems = router.guest_endpoint_problems(shared)
    assert any('outputs are' in p and 'dosplex_left' in p for p in shared_problems), shared_problems
    assert any('inputs are' in p for p in shared_problems), shared_problems
    assert 'module loading is not refused' in shared_problems, shared_problems
    assert pactl('list', 'short', 'modules') == modules, 'the check left a module behind'
    # The command the session wrapper runs says no to the shared server, and refuses a server it cannot trust.
    checker = [sys.executable, str(ROOT / 'packaging/steamos/dosplex-audio-router.py'), 'guest-endpoint-check', '--server']
    refused = subprocess.run([*checker, shared], capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1 and 'module loading is not refused' in refused.stderr, refused
    for server in ('127.0.0.1', 'tcp:127.0.0.1:4713'):
        invalid = subprocess.run([*checker, server], capture_output=True, text=True, timeout=30)
        assert invalid.returncode == 2 and 'unix:/path' in invalid.stderr, invalid

    endpoint = router.GuestEndpoint(root / 'guest-audio', upstream=shared, log=log)
    players = []
    try:
        endpoint.start()
        guest = endpoint.server
        deadline = time.monotonic() + 15
        while router.guest_endpoint_problems(guest):
            assert time.monotonic() < deadline, router.guest_endpoint_problems(guest)
            time.sleep(.2)
        assert stat.S_IMODE(endpoint.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint.config_directory.stat().st_mode) == 0o755
        assert {stat.S_IMODE(p.stat().st_mode) for p in endpoint.config_directory.iterdir()} == {0o644}
        assert stat.S_IMODE((endpoint.directory / 'pulse').stat().st_mode) == 0o700

        # What Guest's Steam is offered: its sound and its microphone, with friendly names.
        assert names(guest, 'sinks') == ['dosplex_right']
        assert names(guest, 'sources') == ['dosplex_right.monitor', 'dosplex_right_mic']
        described = {item['name']: item['description'] for kind in ('sinks', 'sources')
                     for item in json.loads(on(guest, '-f', 'json', 'list', kind).stdout)}
        assert described['dosplex_right'] == 'Dosplex sound', described
        assert described['dosplex_right_mic'] == 'Dosplex microphone', described
        info = on(guest, 'info').stdout
        assert 'Default Sink: dosplex_right' in info and 'Default Source: dosplex_right_mic\n' in info, info
        # Nothing of the shared server shows anywhere in what Guest can list.
        listing = on(guest, 'list').stdout
        for hidden in ('dosplex_left', 'test_tv', 'test_headset', 'test_mic', 'dosplex_guest_probe'):
            assert hidden not in listing, hidden

        # What Guest cannot do. A client's own property list cannot lift this either.
        spoof = {'PULSE_PROP': 'pipewire.client.access=unrestricted'}
        assert names(guest, 'sinks', **spoof) == ['dosplex_right']
        assert names(guest, 'sources', **spoof) == ['dosplex_right.monitor', 'dosplex_right_mic']
        for command in (('set-default-sink', 'dosplex_left'), ('set-default-sink', 'test_tv'),
                        ('set-default-source', 'test_mic'), ('set-default-source', 'dosplex_left_mic.monitor'),
                        ('set-sink-volume', 'dosplex_left', '10%'), ('set-sink-mute', 'test_tv', '1'),
                        ('suspend-sink', 'test_tv', '1')):
            for extra in ({}, spoof):
                result = on(guest, *command, **extra)
                assert result.returncode != 0, (command, result.stdout)
        victim = next(m for m in router.query('modules') if m['name'] == 'module-null-sink')
        for command in (('load-module', 'module-null-sink', 'sink_name=dosplex_evil'),
                        ('load-module', 'module-loopback', 'source=test_mic', 'sink=dosplex_left'),
                        ('unload-module', str(victim['index']))):
            result = on(guest, *command, **spoof)
            assert result.returncode != 0 and 'Access denied' in result.stderr, (command, result.stderr)
        assert on(guest, 'set-default-sink', 'dosplex_right').returncode == 0  # a no-op on Guest's own server
        assert (pactl('get-default-sink'), pactl('get-default-source')) == defaults
        assert pactl('list', 'short', 'modules') == modules
        assert names(shared, 'sinks') == shared_sinks
        assert pactl('get-sink-volume', 'dosplex_left') == dosplex_left_volume
        # The shared server's own streams are invisible to Guest.
        players.append(start(['paplay', '--device=test_tv', '--raw',
                              '--property=application.id=dosplex.audio.main.stream', '/dev/zero']))
        deadline = time.monotonic() + 5
        while 'dosplex.audio.main.stream' not in pactl('list', 'sink-inputs'):
            assert time.monotonic() < deadline, 'main stream did not appear'
            time.sleep(.05)
        assert 'dosplex.audio.main.stream' not in on(guest, 'list', 'sink-inputs').stdout
        players.pop().terminate()

        # What Guest can do: play to its own bus, and record the microphone claimed for it.
        guest_tone = write_tone(root / 'guest-440.wav', 440)
        main_tone = write_tone(root / 'main-880.wav', 880)
        mic_tone = write_tone(root / 'mic-1320.wav', 1320)
        left_mic_tone = write_tone(root / 'left-mic-1760.wav', 1760)

        def stop_players():
            for player in players:
                player.terminate()
                player.wait(timeout=3)
            players.clear()

        # Guest plays through its endpoint; the tone arrives on its own bus and on no other.
        recorders = {'shared right bus': record(shared, 'dosplex_right.monitor'),
                     'shared left bus': record(shared, 'dosplex_left.monitor'),
                     'guest sound monitor': record(guest, 'dosplex_right.monitor')}
        players.append(start(['paplay', '--server=' + guest, guest_tone]))
        players.append(start(['paplay', '--device=dosplex_left', main_tone]))
        levels = measure(recorders, tones)
        only(levels, 'shared right bus', 440)
        only(levels, 'shared left bus', 880)
        only(levels, 'guest sound monitor', 440)
        stop_players()
        # Guest's own volume and mute act on Guest's sound alone: the shared buses keep their volume.
        full = levels['shared right bus'][440]
        for command, expected in ((('set-sink-volume', 'dosplex_right', '50%'), lambda level: .05 * full < level < .3 * full),
                                  (('set-sink-mute', 'dosplex_right', '1'), lambda level: level < 100)):
            assert on(guest, *command).returncode == 0, command
            players.append(start(['paplay', '--server=' + guest, guest_tone]))
            level = measure({'shared right bus': record(shared, 'dosplex_right.monitor')}, tones)['shared right bus'][440]
            assert expected(level), (command, level, full)
            stop_players()
        assert on(guest, 'set-sink-mute', 'dosplex_right', '0').returncode == 0
        assert on(guest, 'set-sink-volume', 'dosplex_right', '100%').returncode == 0
        assert pactl('get-sink-volume', 'dosplex_right') == pactl('get-sink-volume', 'dosplex_left') == dosplex_left_volume
        # Guest records the microphone claimed for it. Asking for Main's buses by name gets
        # something of Guest's own: those names do not exist on its server, and neither does their sound.
        router.assign_audio('left', 'none', 'input')
        router.assign_audio('right', 'test_mic', 'input')
        recorders = {'guest microphone': record(guest, 'dosplex_right_mic'),
                     'guest asking for the left bus': record(guest, 'dosplex_left.monitor'),
                     'guest asking for the left microphone': record(guest, 'dosplex_left_mic.monitor'),
                     'shared left microphone': record(shared, 'dosplex_left_mic.monitor')}
        players.append(start(['paplay', '--device=test_mic_feed', mic_tone]))
        players.append(start(['paplay', '--server=' + guest, guest_tone]))
        players.append(start(['paplay', '--device=dosplex_left', main_tone]))
        players.append(start(['paplay', '--device=dosplex_left_mic', left_mic_tone]))
        levels = measure(recorders, tones)
        only(levels, 'guest microphone', 1320)
        for name in ('guest asking for the left bus', 'guest asking for the left microphone'):
            assert levels[name][880] < 100 and levels[name][1760] < 100, (name, levels[name])
        only(levels, 'shared left microphone', 1760)  # control: Main's microphone really carries that tone
        stop_players()

        # A stopped process is replaced, and the replacement is as restricted as the first.
        name, process = endpoint.children[1]
        process.kill()
        process.wait(timeout=3)
        assert not endpoint.running()
        endpoint.retry_at = time.monotonic() + 60
        assert endpoint.ensure() is False  # restarts are paced
        endpoint.retry_at = 0
        assert endpoint.ensure() is True
        deadline = time.monotonic() + 15
        while router.guest_endpoint_problems(guest):
            assert time.monotonic() < deadline, router.guest_endpoint_problems(guest)
            time.sleep(.2)
    finally:
        for player in players:
            player.terminate()
        endpoint.stop()
    assert endpoint.children == [] and on(endpoint.server, 'info').returncode != 0
    # The router's own claims changed the loopbacks; nothing else on the shared server may differ.
    assert [(m['name'], m['argument']) for m in router.query('modules') if not router.managed_route(m)] == unclaimed, \
        'the shared server gained or lost modules'
    return True


def run_guest_privileged_checks(pactl):
    # In the product the router is root and the endpoint belongs to Guest's account. Root itself runs the
    # checks. Anyone else emulates it in a user namespace where this account is root and uid 1001 is Guest.
    namespace = ['unshare', '--map-auto', '--map-root-user', '--']
    can_become_guest = 'import os;os.setgroups([]);os.setgid(1002);os.setuid(1002)'
    if os.geteuid() != 0 and subprocess.run([*namespace, sys.executable, '-c', can_become_guest],
                                           capture_output=True).returncode:
        print('SKIP: the privileged Guest endpoint checks need root or user namespaces with subordinate ids')
        return
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    # The shared server is reached over TCP: Guest's account cannot enter the test's private directories.
    tcp = pactl('load-module', 'module-native-protocol-tcp', f'port={port}', 'listen=127.0.0.1')
    try:
        if os.geteuid() == 0:
            guest_privileged_checks(port)
            return
        result = subprocess.run([*namespace, sys.executable, '-I', str(Path(__file__).resolve()),
                                 '--guest-privileged', str(port)], capture_output=True, text=True, timeout=120)
    finally:
        pactl('unload-module', tcp)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


def guest_privileged_checks(port):
    """Run as root of a user namespace in which uid 1001 is Guest and uid 1002 is Main."""
    assert os.geteuid() == 0
    spec = importlib.util.spec_from_file_location('audio_router', ROOT / 'packaging/steamos/dosplex-audio-router.py')
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    guest, main_uid = SimpleNamespace(pw_uid=1001, pw_gid=1001), 1002

    def as_user(uid, code):
        result = subprocess.run([sys.executable, '-I', '-c', code], user=uid, group=uid, extra_groups=[],
                                capture_output=True, text=True, timeout=20)
        return result.returncode, (result.stdout + result.stderr).strip()

    def mode(path):
        return stat.S_IMODE(path.stat().st_mode)

    os.environ['DOSPLEX_ROUTER_SECRET'] = 'must-not-reach-guest'
    with tempfile.TemporaryDirectory(prefix='dosplex-guest-ns-') as directory:
        root = Path(directory)
        root.chmod(0o755)  # a trial root that every account can traverse
        endpoint = router.GuestEndpoint(root / 'audio-guest', upstream=f'tcp:127.0.0.1:{port}', owner=guest)
        try:
            endpoint.start()
            deadline = time.monotonic() + 15
            while router.guest_endpoint_problems(endpoint.server):
                assert time.monotonic() < deadline, router.guest_endpoint_problems(endpoint.server)
                time.sleep(.2)
            # Guest owns the runtime directory and nothing else; the configuration stays with the router.
            runtime = endpoint.directory.stat()
            assert (runtime.st_uid, runtime.st_gid, mode(endpoint.directory)) == (1001, 1001, 0o700)
            assert (endpoint.config_directory.stat().st_uid, mode(endpoint.config_directory)) == (0, 0o755)
            for path in endpoint.config_directory.iterdir():
                assert (path.stat().st_uid, mode(path)) == (0, 0o644), path
            # The processes are Guest, with no groups of the router's and none of its environment.
            for name, process in endpoint.children:
                status = dict(line.split(':\t', 1) for line in Path(f'/proc/{process.pid}/status').read_text().splitlines()
                              if ':\t' in line)
                assert status['Uid'].split() == ['1001'] * 4 and status['Gid'].split() == ['1001'] * 4, (name, status)
                assert status['Groups'].strip() == '', (name, status['Groups'])
                environment = Path(f'/proc/{process.pid}/environ').read_bytes()
                assert b'DOSPLEX_ROUTER_SECRET' not in environment, name
                assert {e.split(b'=')[0] for e in environment.split(b'\0') if e} == \
                    {b'HOME', b'PATH', b'PIPEWIRE_RUNTIME_DIR', b'XDG_CACHE_HOME', b'XDG_CONFIG_HOME',
                     b'XDG_DATA_HOME', b'XDG_RUNTIME_DIR', b'XDG_STATE_HOME'}, name
            # Only Guest reaches the endpoint, and Guest cannot loosen it.
            native = str(endpoint.directory / 'pulse' / 'native')
            connect = ('import socket,sys\ns=socket.socket(socket.AF_UNIX)\ntry:\n s.connect(%r)\n'
                       'except OSError as e:\n print(e.errno)\n sys.exit(1)\nprint("connected")' % native)
            assert as_user(1001, connect) == (0, 'connected')
            assert as_user(main_uid, connect) == (1, str(13)), as_user(main_uid, connect)  # EACCES
            assert as_user(main_uid, 'import os;os.listdir(%r)' % str(endpoint.directory))[0] != 0
            config = str(endpoint.config_directory / 'pipewire-pulse.conf')
            assert as_user(1001, 'open(%r,"a").write("pulse.allow-module-loading = true")' % config)[0] != 0
            assert as_user(1001, 'import os;os.replace(%r,%r)' % (config, config + '.x'))[0] != 0
            checker = [sys.executable, str(ROOT / 'packaging/steamos/dosplex-audio-router.py'), 'guest-endpoint-check',
                       '--server', endpoint.server]
            as_guest = subprocess.run(checker, user=1001, group=1001, extra_groups=[], capture_output=True, text=True, timeout=30)
            assert as_guest.returncode == 0, as_guest.stderr
            as_main = subprocess.run(checker, user=main_uid, group=main_uid, extra_groups=[], capture_output=True, text=True, timeout=30)
            assert as_main.returncode != 0 and 'does not answer' in as_main.stderr, as_main.stderr
        finally:
            endpoint.stop()
        assert endpoint.children == [] and as_user(1001, connect)[0] != 0
    print('PASS: Guest endpoint, started by a root router, runs as Guest alone with a clean environment, '
          'is reachable only by Guest, and keeps its configuration out of Guest\'s hands')


def main():
    for binary in ('pipewire', 'pipewire-pulse', 'pactl', 'paplay', 'parec', 'wireplumber', 'dbus-daemon'):
        if not shutil.which(binary):
            raise RuntimeError(f'Missing test dependency: {binary}')
    processes = []
    with tempfile.TemporaryDirectory(prefix='dosplex-audio-test-') as directory:
        root = Path(directory)
        env = os.environ.copy()
        env.update(GIO_USE_VFS="local", HOME=directory, XDG_RUNTIME_DIR=directory,
                   PIPEWIRE_RUNTIME_DIR=directory, PULSE_SERVER=f'unix:{directory}/pulse/native',
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'))
        # WirePlumber's policy-only profile has no hardware monitors.
        version = subprocess.check_output(['wireplumber', '--version'], text=True)
        if 'libwireplumber 0.5.' not in version:
            raise RuntimeError('Audio fixture requires WirePlumber 0.5')
        config = root / 'pipewire.conf'
        config.write_text('''context.properties = { core.daemon = true core.name = pipewire-0 }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
 { name = libpipewire-module-protocol-native }
 { name = libpipewire-module-metadata }
 { name = libpipewire-module-spa-node-factory }
 { name = libpipewire-module-client-node }
 { name = libpipewire-module-adapter }
 { name = libpipewire-module-link-factory }
 { name = libpipewire-module-session-manager }
 { name = libpipewire-module-access args = { access.force = unrestricted } }
]
context.objects = [ { factory = spa-node-factory args = { factory.name = support.node.driver node.name = Dummy-Driver priority.driver = 20000 } } ]
''')
        log = open(root / 'server.log', 'w')
        def start(args, **kwargs):
            process = subprocess.Popen(args, env=env, stderr=log,
                                       stdout=kwargs.pop('stdout', log), **kwargs)
            processes.append(process)
            return process
        def pactl(*args):
            return subprocess.run(['pactl', *args], env=env, check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        try:
            dbus = start(['dbus-daemon', '--session', '--nofork', '--print-address=1'], stdout=subprocess.PIPE)
            env['DBUS_SESSION_BUS_ADDRESS'] = dbus.stdout.readline().decode().strip()
            start(['pipewire', '-c', str(config)])
            start(['pipewire-pulse'])
            deadline = time.monotonic() + 5
            while True:
                try:
                    pactl('info')
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() > deadline:
                        raise RuntimeError((root / 'server.log').read_text())
                    time.sleep(.05)
            manager = start(['wireplumber', '--profile', 'policy'])
            time.sleep(.4)
            assert manager.poll() is None, (root / "server.log").read_text()
            spec = importlib.util.spec_from_file_location('audio_router', ROOT / 'packaging/steamos/dosplex-audio-router.py')
            router = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(router)
            router.STATE_PATH = root / 'routes.json'
            router.PERSIST_PATH = root / 'persist/audio.json'
            router.SIDE_ROOT = root / 'sides'
            router.SERVER_USER = pwd.getpwuid(os.getuid()).pw_name
            # Exercise equal-owner ambiguity independent of the machine user.
            # Deck's normal fallback owner is literally "deck", which otherwise
            # makes an untagged fixture paplay stream look left-owned on Deck.
            for side in ('left', 'right'):
                side_dir = router.SIDE_ROOT / side
                side_dir.mkdir(parents=True, exist_ok=True)
                (side_dir / 'session.json').write_text(json.dumps({'owner': router.SERVER_USER}))
            # Keep every pactl request on our private socket, including root CI.
            def isolated_pactl(*args, **kwargs):
                return subprocess.run(['pactl', *args], env=env, capture_output=True,
                                      text=True, timeout=5)
            router.run_pactl = isolated_pactl
            tv_module = pactl('load-module', 'module-null-sink', 'sink_name=test_tv')
            pactl('load-module', 'module-null-sink', 'sink_name=test_headset')
            pactl('load-module', 'module-null-sink', 'sink_name=test_mic_feed')
            pactl('load-module', 'module-remap-source', 'master=test_mic_feed.monitor', 'source_name=test_mic')
            router.ensure_per_side_sinks()
            assert {d['id'] for d in router.list_sinks()} == {'test_tv', 'test_headset', 'test_mic_feed'}
            assert {d['id'] for d in router.list_devices('sources')} == {'test_mic'}
            router.assign_audio('left', 'test_tv')
            router.assign_audio('right', 'test_headset')
            router.assign_audio('right', 'test_mic', 'input')
            snapshot = router.overlay_snapshot()
            assert snapshot.startswith('DOSPLEX_AUDIO_3\n') and snapshot.endswith('\nEND\n')
            assert 'S\tright\tinput\tselected\t' in snapshot
            assert 'D\toutput\tleft\t' in snapshot
            conflict = router.overlay_snapshot('left', 'output', 'test_headset')
            assert '\nE\t' in conflict
            routes = router.load_routes()
            assert routes['left']['input'] is None
            assert routes['right']['input'] == 'test_mic'
            try:
                router.assign_audio('left', 'test_headset')
                raise AssertionError('exclusive ownership was not enforced')
            except ValueError:
                pass
            def graph():
                return {router.managed_route(m): router.module_args(m)
                        for m in router.query('modules') if router.managed_route(m)}
            first = graph()
            assert len(first) == 3, first
            def loopback_modules():
                return {router.managed_route(m): m['index'] for m in router.query('modules') if router.managed_route(m)}
            loaded = loopback_modules()
            for _ in range(3):
                router.ensure_loopbacks()
                router.route_streams()
            assert graph() == first
            assert loopback_modules() == loaded, 'a loopback that was already right was loaded again'
            # Every loopback carries the restore flags, and a private one plays at unity (T-420).
            def loopback_volume(route):
                [stream] = [s for s in router.query('sink-inputs') if str(s.get('owner_module')) == str(loopback_modules()[route])]
                return [channel['value'] for channel in stream['volume'].values()]
            restore_flags = {'node.stream.restore-props=false', 'state.restore-props=false'}
            for route, loopback in first.items():
                properties = set(loopback['sink_input_properties'].split())
                assert restore_flags <= properties and 'dosplex.shared=true' not in properties, (route, properties)
                assert loopback_volume(route) == [65536, 65536], (route, loopback_volume(route))
            # WirePlumber gives a new stream the volume of the last one with the same name, and a
            # loopback is named after its module id, which PipeWire reuses. A loopback that was left
            # at another volume must not pass it on to the one that replaces it, and a loopback from
            # before the flags existed is replaced by one that has them.
            def old_loopback():
                module = pactl('load-module', 'module-loopback', 'source=dosplex_left.monitor', 'sink=test_tv',
                               'source_dont_move=true', 'sink_dont_move=true', 'latency_msec=30')
                deadline = time.monotonic() + 5
                while not [s for s in router.query('sink-inputs') if str(s.get('owner_module')) == module]:
                    assert time.monotonic() < deadline, 'loopback stream did not appear'
                    time.sleep(.05)
                return module
            pactl('unload-module', str(loopback_modules()['left', 'output']))
            module = old_loopback()
            [stream] = [s for s in router.query('sink-inputs') if str(s.get('owner_module')) == module]
            pactl('set-sink-input-volume', str(stream['index']), '30000', '20000')
            time.sleep(1.5)  # WirePlumber records the volume in the background
            pactl('unload-module', module)
            router.ensure_loopbacks()
            assert loopback_volume(('left', 'output')) == [65536, 65536], loopback_volume(('left', 'output'))
            pactl('unload-module', str(loopback_modules()['left', 'output']))
            old_loopback()
            router.ensure_loopbacks()
            assert restore_flags <= set(graph()['left', 'output']['sink_input_properties'].split()), graph()
            assert loopback_volume(('left', 'output')) == [65536, 65536]
            assert graph() == first
            # Fail after removing the old loopback, then fail state publication
            # after installing the new graph. Both must restore the prior graph
            # and leave the saved route untouched.
            saved=router.STATE_PATH.read_bytes()
            original_pactl=router.pactl_checked
            def fail_new_loopback(*args):
                if args[:2]==('load-module','module-loopback') and 'sink=test_mic_feed' in args:
                    raise RuntimeError('injected loopback creation failure')
                return original_pactl(*args)
            for boundary in ('loopback','publication'):
                fault=(patch.object(router,'pactl_checked',side_effect=fail_new_loopback) if boundary=='loopback'
                       else patch.object(router,'save_routes',side_effect=OSError('injected publication failure')))
                with fault:
                    try:
                        router.assign_audio('left','test_mic_feed')
                        raise AssertionError('injected failure was swallowed')
                    except (RuntimeError,OSError):pass
                assert router.STATE_PATH.read_bytes()==saved,boundary
                assert graph()==first,(boundary,graph())
            # Unknown/unowned streams must never be swept onto either side.
            untagged_id = 'dosplex.audio.integration.untagged'
            playback = start(['paplay', '--raw', '--device=test_tv',
                              f'--property=application.id={untagged_id}', '/dev/zero'])
            deadline = time.monotonic() + 5
            while True:
                before = router.query('sink-inputs')
                untagged_candidates = [s for s in before
                                       if s.get('properties', {}).get('application.id') == untagged_id]
                if untagged_candidates:
                    break
                assert time.monotonic() < deadline, 'untagged fixture stream did not appear'
                time.sleep(.02)
            assert len(untagged_candidates) == 1, untagged_candidates
            untagged_index = untagged_candidates[0]['index']
            sinks_before = {n['name']: n['index'] for n in router.query('sinks')}
            assert untagged_candidates[0]['sink'] == sinks_before['test_tv']
            # A unique owner match routes an untagged stream to that side.
            (router.SIDE_ROOT / 'right' / 'session.json').write_text(
                json.dumps({'owner': router.SERVER_USER + '-other'}))
            router.route_streams()
            side_sinks = {n['name']: n['index'] for n in router.query('sinks')}
            # The move request can complete before PipeWire publishes its new
            # graph. Observe that one request settling; do not issue retries.
            deadline = time.monotonic() + 5
            while True:
                single_owner_stream = next(s for s in router.query('sink-inputs')
                                           if s['index'] == untagged_index)
                if single_owner_stream['sink'] == side_sinks['dosplex_left']:
                    break
                assert time.monotonic() < deadline, single_owner_stream
                time.sleep(.02)
            # Equal owners make identity alone ambiguous; restore the fixture
            # stream's original destination before checking pass-through.
            (router.SIDE_ROOT / 'right' / 'session.json').write_text(
                json.dumps({'owner': router.SERVER_USER}))
            router.pactl_checked('move-sink-input', str(untagged_index), 'test_tv')
            deadline = time.monotonic() + 5
            while True:
                ambiguous_stream = next(s for s in router.query('sink-inputs')
                                        if s['index'] == untagged_index)
                if ambiguous_stream['sink'] == sinks_before['test_tv']:
                    break
                assert time.monotonic() < deadline, {
                    'stream': ambiguous_stream,
                    'expected_sink': sinks_before['test_tv'],
                    'sinks': {n['name']: n['index'] for n in router.query('sinks')},
                }
                time.sleep(.02)
            # Session property tags work even if both sides have the same owner.
            tagged = start(['paplay', '--raw', '--device=test_tv',
                            '--property=dosplex.side=right', '/dev/zero'])
            record = start(['parec', '--raw', '--device=test_mic',
                            '--property=dosplex.side=left'], stdout=subprocess.DEVNULL)
            # Stream creation is asynchronous. A fixed sleep can invoke the
            # one-shot router before the capture stream exists under load.
            deadline = time.monotonic() + 5
            while True:
                plays = router.query('sink-inputs')
                captures = router.query('source-outputs')
                if (any(s.get('properties', {}).get('dosplex.side') == 'right' for s in plays) and
                        any(s.get('properties', {}).get('dosplex.side') == 'left' for s in captures)):
                    break
                assert time.monotonic() < deadline, 'tagged streams did not appear'
                time.sleep(.02)
            moves = io.StringIO()
            with contextlib.redirect_stderr(moves):
                router.route_streams()
            sinks = {n['name']: n['index'] for n in router.query('sinks')}
            sources = {n['name']: n['index'] for n in router.query('sources')}
            streams = router.query('sink-inputs')
            untagged = next(s for s in streams if s['index'] == untagged_index)
            assert untagged['sink'] == sinks['test_tv'], untagged
            tagged_stream = next(s for s in streams if s.get('properties', {}).get('dosplex.side') == 'right')
            assert tagged_stream['sink'] == sinks['dosplex_right'], tagged_stream
            capture = next(s for s in router.query('source-outputs') if s.get('properties', {}).get('dosplex.side') == 'left')
            assert capture['source'] == sources['dosplex_left_mic.monitor'], capture
            # The journal has one line for each move: the side, the application, and from where to where.
            assert moves.getvalue().splitlines() == [
                f'dosplex audio: moved right output stream "paplay" (index {tagged_stream["index"]}): test_tv -> dosplex_right',
                f'dosplex audio: moved left input stream "parec" (index {capture["index"]}): test_mic -> dosplex_left_mic.monitor'
            ], moves.getvalue()
            quiet = io.StringIO()
            with contextlib.redirect_stderr(quiet):
                router.route_streams()
            assert quiet.getvalue() == '', quiet.getvalue()  # nothing moved, nothing said
            pactl('unload-module', tv_module)
            router.ensure_loopbacks()
            # The claimed output is gone: the side plays on the shared default until it is back (AUD-N-04).
            default = router.pactl_checked('get-default-sink')
            assert default and not default.startswith('dosplex_') and default != 'test_tv', default
            assert graph()[('left', 'output')]['sink'] == default, graph()
            assert router.status()['sides']['left']['output']['state'] == 'disconnected'
            assert router.load_routes()['left']['output'] == 'test_tv'
            pactl('load-module', 'module-null-sink', 'sink_name=test_tv')
            router.ensure_loopbacks()
            assert graph()[('left', 'output')]['sink'] == 'test_tv'
            # Prove actual signal separation with synthetic tones, never speech.
            for process in (playback, tagged, record):
                process.terminate()
                process.wait(timeout=3)
            rate = 48000
            def tone(name, frequency):
                path = root / f'{name}.wav'
                samples = array.array('h', (int(5000 * math.sin(2 * math.pi * frequency * i / rate))
                                            for i in range(rate * 5)))
                with wave.open(str(path), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(rate)
                    output.writeframes(samples.tobytes())
                return str(path)
            recordings = {}
            for name, source in {'tv': 'test_tv.monitor', 'headset': 'test_headset.monitor',
                                 'left_mic': 'dosplex_left_mic.monitor',
                                 'right_mic': 'dosplex_right_mic.monitor'}.items():
                recordings[name] = start(['parec', '--raw', '--format=s16le', '--channels=1',
                                          '--rate=48000', f'--device={source}'], stdout=subprocess.PIPE)
            for name, frequency, sink in [('left', 440, 'dosplex_left'),
                                           ('right', 880, 'dosplex_right'),
                                           ('mic', 1320, 'test_mic_feed')]:
                start(['paplay', f'--device={sink}', tone(name, frequency)])
            # communicate drains each pipe; at 2 s each, all generated tones
            # overlap. Use concurrent reads to avoid pausing the other captures.
            from concurrent.futures import ThreadPoolExecutor
            def sample(process):
                data = bytearray()
                deadline = time.monotonic() + 8
                while len(data) < rate * 2 * 2:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                        raise AssertionError('audio capture timed out')
                    chunk = os.read(process.stdout.fileno(), min(65536, rate * 2 * 2 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                process.terminate()
                process.wait(timeout=3)
                values = array.array('h')
                values.frombytes(data)
                assert len(values) >= rate, 'insufficient audio samples'
                values = values[-rate:]
                def amplitude(frequency):
                    real = sum(v * math.cos(2 * math.pi * frequency * i / rate) for i, v in enumerate(values))
                    imag = sum(v * math.sin(2 * math.pi * frequency * i / rate) for i, v in enumerate(values))
                    return 2 * math.hypot(real, imag) / len(values)
                return [amplitude(f) for f in (440, 880, 1320)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                signals = dict(zip(recordings, pool.map(sample, recordings.values())))
            for name, desired in [('tv', 0), ('headset', 1), ('right_mic', 2)]:
                amplitudes = signals[name]
                assert amplitudes[desired] > 500, (name, amplitudes)
                assert all(a < 100 for i, a in enumerate(amplitudes) if i != desired), (name, amplitudes)
            assert max(signals['left_mic']) < 100, signals['left_mic']
            # Standby/off silence both directions without releasing claims.
            saved_routes = router.load_routes()
            state_file = router.SIDE_ROOT / 'right/session.json'
            side_record = json.loads(state_file.read_text())
            for lifecycle in ('standby', 'off'):
                state_file.write_text(json.dumps(dict(side_record, lifecycle=lifecycle)))
                router.ensure_loopbacks()
                assert ('right', 'output') not in graph() and ('right', 'input') not in graph()
                assert router.load_routes() == saved_routes
                assert ('left', 'output') in graph()
            state_file.write_text(json.dumps(dict(side_record, lifecycle='running')))
            router.ensure_loopbacks()
            assert ('right', 'output') in graph() and ('right', 'input') in graph()
            # Transfer claimed endpoints while the side buses remain stable.
            # Other-player claims fail; release then claim transfers ownership.
            for process in processes:
                if process.args[0] == 'paplay' and process.poll() is None:
                    process.terminate();process.wait(timeout=3)
            saved = router.STATE_PATH.read_bytes()
            try:
                router.assign_audio('left', 'test_mic', 'input')
                raise AssertionError('claimed microphone was stolen')
            except ValueError:
                assert router.STATE_PATH.read_bytes() == saved
            router.assign_audio('right', 'none', 'output')
            router.assign_audio('left', 'test_headset', 'output')
            router.assign_audio('right', 'test_tv', 'output')
            router.assign_audio('right', 'none', 'input')
            router.assign_audio('left', 'test_mic', 'input')
            assert graph()[('left', 'output')]['sink'] == 'test_headset'
            assert graph()[('right', 'output')]['sink'] == 'test_tv'
            recordings = {}
            for name, source in {'tv': 'test_tv.monitor', 'headset': 'test_headset.monitor',
                                 'left_mic': 'dosplex_left_mic.monitor',
                                 'right_mic': 'dosplex_right_mic.monitor'}.items():
                recordings[name] = start(['parec', '--raw', '--format=s16le', '--channels=1',
                                          '--rate=48000', f'--device={source}'], stdout=subprocess.PIPE)
            for name, frequency, sink in [('left', 440, 'dosplex_left'),
                                           ('right', 880, 'dosplex_right'),
                                           ('mic', 1320, 'test_mic_feed')]:
                start(['paplay', f'--device={sink}', tone(name, frequency)])
            with ThreadPoolExecutor(max_workers=4) as pool:
                transferred = dict(zip(recordings, pool.map(sample, recordings.values())))
            for name, desired in [('tv', 1), ('headset', 0), ('left_mic', 2)]:
                amplitudes = transferred[name]
                assert amplitudes[desired] > 500, (name, amplitudes)
                assert all(a < 100 for i, a in enumerate(amplitudes) if i != desired), (name, amplitudes)
            assert max(transferred['right_mic']) < 100, transferred['right_mic']
            router.assign_audio('right', 'none', 'input')
            assert ('right', 'input') not in graph()
            assert router.status()['sides']['right']['input']['state'] == 'muted'
            for process in processes:
                if process.args[0] == 'paplay' and process.poll() is None:
                    process.terminate(); process.wait(timeout=3)
            spec = importlib.util.spec_from_file_location('shared_checks', ROOT / 'tests/shared-audio-checks.py')
            shared_checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(shared_checks)
            shared_checks.run(router, root, start, pactl)
            recreated_output_checks(router, root, start, pactl)
            print('PASS: a sink that is created again under its name gets its loopback linked again, within the first monitor pass, and the journal says what the monitor changed, and nothing on a pass that changed nothing')
            print('PASS: isolated PipeWire output/input ownership, release/claim transfer, new streams, maintenance, mute, reconnect and synthetic signal separation')
            if guest_endpoint_checks(router, root, env, start, pactl, log):
                print('PASS: Guest endpoint offers one output and one microphone, refuses shared defaults, volumes and modules, scales only its own sound, and plays and records only its own buses')
            run_guest_privileged_checks(pactl)
        except Exception:
            print((root / 'server.log').read_text())
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--guest-privileged':
        guest_privileged_checks(int(sys.argv[2]))
    else:
        main()
