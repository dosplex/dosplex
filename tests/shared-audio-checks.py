# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Shared speaker checks, run inside audio-integration's hardware-free server."""
import array
import json
import math
import os
import select
import statistics
import subprocess
import time
import wave
from unittest.mock import patch


def run(router, root, start, pactl):
    display = {'primary_side':'left', 'layout':'vertical', 'left':{'x':0}, 'right':{'x':1920}}
    router.display_state = lambda: display
    router.change_mix('left', 'off')
    router.assign_audio('right', 'none')
    router.assign_audio('left', 'test_tv')
    router.assign_audio('right', 'test_headset')
    router.assign_audio('right', 'test_mic', 'input') if router.load_routes()['left']['input'] is None else None
    mic_before = {s:router.load_routes()[s]['input'] for s in router.SIDES}
    def graph():
        return {router.managed_route(m): router.module_args(m)
                for m in router.query('modules') if router.managed_route(m)}
    def denied(fn):
        saved=router.STATE_PATH.read_bytes()
        try:fn()
        except ValueError:pass
        else:raise AssertionError('unauthorized change accepted')
        assert router.STATE_PATH.read_bytes()==saved
    denied(lambda:router.change_mix('right','full'))
    router.change_mix('left','full')
    assert graph()['right','output']['sink']=='test_headset', 'Guest was silently redirected'
    denied(lambda:router.assign_audio('right','test_tv'))
    router.change_mix('right','join')
    assert graph()['right','output']['sink']=='test_tv'
    assert {s:router.load_routes()[s]['input'] for s in router.SIDES}==mic_before
    assert 'M\tfull\t11\t' in router.overlay_snapshot()
    assert 'D\toutput\tshared\t' in router.overlay_snapshot()
    rate=48000;frequencies=(440,660,880,1100)
    def tone(name, fs, amplitude=5000):
        path=root/(name+'.wav');samples=array.array('h')
        for i in range(rate*4):
            samples.extend(int(amplitude*math.sin(2*math.pi*f*i/rate)) for f in fs)
        with wave.open(str(path),'wb') as w:
            w.setnchannels(2);w.setsampwidth(2);w.setframerate(rate);w.writeframes(samples.tobytes())
        return str(path)
    paths=[tone('shared-left',frequencies[:2]),tone('shared-right',frequencies[2:])]
    def measure(paths=paths):
        recorder=start(['parec','--raw','--format=s16le','--channels=2','--rate=48000',
                        '--device=test_tv.monitor'],stdout=subprocess.PIPE)
        players=[start(['paplay','--device=dosplex_'+s,p]) for s,p in zip(router.SIDES,paths)]
        try:
            data=bytearray();deadline=time.monotonic()+6;size=rate*4*2
            while len(data)<size:
                remain=deadline-time.monotonic()
                assert remain>0 and select.select([recorder.stdout],[],[],remain)[0], 'signal timeout'
                chunk=os.read(recorder.stdout.fileno(),min(65536,size-len(data)))
                assert chunk,'capture closed';data.extend(chunk)
            values=array.array('h');values.frombytes(data);values=values[-rate*2:]
            channels=[values[c::2] for c in range(2)]
            def amp(v,f):
                # Independent streams can slip a sample under CI load. Measure
                # amplitude in 100 ms blocks so a phase jump cannot cancel an
                # otherwise present tone across the entire capture.
                n=rate//10
                return statistics.median(2*math.hypot(
                    sum(x*math.cos(2*math.pi*f*i/rate) for i,x in enumerate(v[j:j+n])),
                    sum(x*math.sin(2*math.pi*f*i/rate) for i,x in enumerate(v[j:j+n])))/n
                    for j in range(0,len(v)-n+1,n))
            return [[amp(v,f) for f in frequencies] for v in channels],max(map(abs,values))
        finally:
            for p in [recorder,*players]:
                if p.poll() is None:p.terminate()
                p.wait(timeout=3)
    def check(expected):
        amplitudes,peak=measure()
        print('shared signal', expected, amplitudes, flush=True)
        for c in range(2):
            for f in range(4):
                wanted=5000*expected[c][f]
                assert abs(amplitudes[c][f]-wanted)<max(100,wanted*.18),(expected,amplitudes)
        return [[round(v) for v in channel] for channel in amplitudes]
    results={'full':check([[.5,0,.5,0],[0,.5,0,.5]])}
    router.change_mix('left','separated')
    results['separated']=check([[.5,0,.125,0],[0,.125,0,.5]])
    for strength,quieter in (('gentle',.25),('strong',.0625),('balanced',.125)):
        denied(lambda:router.change_mix('right',strength))
        router.change_mix('left',strength)
        assert router.load_routes()['shared']['separation']==strength
        assert 'P\t'+strength+'\t' in router.overlay_snapshot()
        results[strength]=check([[.5,0,quieter,0],[0,quieter,0,.5]])
    legacy=router.load_routes();del legacy['shared']['separation'];router.STATE_PATH.write_text(json.dumps(legacy))
    assert router.load_routes()['shared']['separation']=='balanced'
    display['left']['x'],display['right']['x']=1920,0
    router.ensure_loopbacks()
    results['swapped']=check([[.125,0,.5,0],[0,.5,0,.125]])
    display['layout']='horizontal';router.ensure_loopbacks()
    results['horizontal_fallback']=check([[.5,0,.5,0],[0,.5,0,.5]])
    router.change_mix('right','leave')
    assert graph()['right','output']['sink']=='test_headset'
    results['guest_left']=check([[.5,0,0,0],[0,.5,0,0]])
    router.change_mix('right','join')
    # Sleeping participants retain claims but have no playback/capture path.
    statefile=router.SIDE_ROOT/'right/session.json';record=json.loads(statefile.read_text())
    statefile.write_text(json.dumps(dict(record,lifecycle='standby')));router.ensure_loopbacks()
    assert ('right','output') not in graph() and ('right','input') not in graph()
    results['standby']=check([[.5,0,0,0],[0,.5,0,0]])
    statefile.write_text(json.dumps(dict(record,lifecycle='running')));router.ensure_loopbacks()
    # Both graph failure and publication failure restore old routes and gain.
    before=router.STATE_PATH.read_bytes();previous=graph()
    with patch.object(router,'save_routes',side_effect=OSError('injected shared publication failure')):
        try:router.change_mix('left','off')
        except OSError:pass
        else:raise AssertionError('publication fault swallowed')
    assert router.STATE_PATH.read_bytes()==before and graph()==previous
    tv=next(m for m in router.query('modules') if m['name']=='module-null-sink' and router.module_args(m).get('sink_name')=='test_tv')
    pactl('unload-module',str(tv['index']));router.ensure_loopbacks()
    assert all((s,'output') not in graph() for s in router.SIDES)
    assert all(router.status()['sides'][s]['output']['state']=='disconnected' for s in router.SIDES)
    pactl('load-module','module-null-sink','sink_name=test_tv');router.ensure_loopbacks()
    results['reconnected']=check([[.5,0,.5,0],[0,.5,0,.5]])
    # Near-full-scale, same-frequency sources bound the combined peak even if
    # their independent playback clocks happen to align coherently.
    hot=tone('shared-headroom',(997,997),30000)
    _,peak=measure([hot,hot]);assert 1000<peak<31000,peak
    results['headroom_peak']=peak
    router.change_mix('left','off')
    assert graph()['left','output']['sink']=='test_tv'
    assert graph()['right','output']['sink']=='test_headset'
    # A private claim after a positioned mix starts at unity. WirePlumber gives a new stream the
    # volume of the last one with the same name, and a loopback is named after its module id,
    # which is reused: without the explicit volume Main's claim came up with the mix's gains.
    display['layout']='vertical';display['left']['x'],display['right']['x']=0,1920
    router.change_mix('left','separated');router.change_mix('right','join')
    assert loopback_volume(router,('left','output'))!=loopback_volume(router,('right','output'))
    time.sleep(1.2)  # WirePlumber records the stream volumes in the background
    router.change_mix('left','off')
    for route in (('left','output'),('right','output')):
        assert loopback_volume(router,route)==[65536,65536],(route,loopback_volume(router,route))
        assert not {'node.stream.restore-props=false','state.restore-props=false'}-set(graph()[route]['sink_input_properties'].split())
    results['private_after_mix']=check([[1,0,0,0],[0,1,0,0]])
    join_checks(router,graph)
    print('PASS: shared audio full/separated, four channel tones, swap, horizontal fallback, join/leave, standby, rollback, reconnect, headroom, auto-join and opt-out '+json.dumps(results))


def loopback_volume(router, route):
    """The Pulse volume of each channel of the sink input that the router's loopback for `route` plays."""
    module = next(m['index'] for m in router.query('modules') if router.managed_route(m) == route)
    [stream] = [s for s in router.query('sink-inputs') if str(s.get('owner_module')) == str(module)]
    return [channel['value'] for channel in stream['volume'].values()]


def join_checks(router, graph):
    """Who is in a shared mix: a player without an output of their own joins when Main starts or
    changes a mix, "Use own output" keeps them out while the mix stays on, and old route files load.
    Starts and ends with the mix off, Main on test_tv and Guest on test_headset."""
    def routes():
        return router.load_routes()
    def members():
        return next(line.split('\t')[2] for line in router.overlay_snapshot().splitlines() if line.startswith('M\t'))
    def guest():
        return routes()['right']
    def refuses(text):
        router.STATE_PATH.write_text(json.dumps(text))
        try:router.load_routes()
        except ValueError:pass
        else:raise AssertionError('accepted '+repr(text))
    assert routes()['shared']['mode']=='off' and routes()['left']['output']=='test_tv' and guest()['output']=='test_headset'
    # A player who has an output of their own keeps it when Main starts a mix, and may still join.
    router.change_mix('left','full')
    assert guest()['shared'] is False and members()=='10', routes()
    assert graph()['right','output']['sink']=='test_headset', 'Guest was silently redirected'
    router.change_mix('left','off')
    # A player without one is in the mix at once, however Main starts it or sets its strength.
    router.assign_audio('right','none')
    for action in ('full','separated','strong'):
        router.change_mix('left','off')
        assert guest()['shared'] is False and ('right','output') not in graph()
        router.change_mix('left',action)
        assert guest()['shared'] is True and guest()['opted_out'] is False and members()=='11', action
        assert graph()['right','output']['sink']=='test_tv' and 'dosplex.shared=true' in graph()['right','output']['sink_input_properties']
    # Changing a mix that is already on brings in a player who has become free since.
    router.assign_audio('right','test_headset')
    assert guest()['shared'] is False and members()=='10'
    router.assign_audio('right','none')
    assert guest()['shared'] is False and ('right','output') not in graph()
    router.change_mix('left','strong')
    assert guest()['shared'] is True and members()=='11'
    # "Use own output" is the opt-out: no mix change brings the player back while the mix is on.
    router.change_mix('right','leave')
    assert guest()['opted_out'] is True and guest()['shared'] is False and ('right','output') not in graph()
    for action in ('balanced','full'):
        router.change_mix('left',action)
        assert guest()['shared'] is False and guest()['opted_out'] is True and members()=='10', action
    router.change_mix('right','join')
    assert guest()['shared'] is True and guest()['opted_out'] is False and members()=='11'
    # Stopping the mix ends the opt-out, so the next mix includes the player again.
    router.change_mix('right','leave')
    assert guest()['opted_out'] is True
    router.change_mix('left','off')
    assert all(routes()[s]['opted_out'] is False and routes()[s]['shared'] is False for s in router.SIDES)
    router.change_mix('left','full')
    assert guest()['shared'] is True and guest()['opted_out'] is False
    router.change_mix('left','off')
    # Leaving when no mix is on is not an opt-out from the next one.
    router.change_mix('right','leave')
    assert guest()['opted_out'] is False
    router.change_mix('left','full')
    assert guest()['shared'] is True
    # An opt-out ends when the player claims an output and releases it again, not before. Releasing
    # nothing changes no claim.
    router.change_mix('right','leave')
    router.assign_audio('right','none')
    assert guest()['opted_out'] is True
    router.change_mix('left','gentle')
    assert guest()['shared'] is False
    router.assign_audio('right','test_headset')
    router.assign_audio('right','none')
    assert guest()['opted_out'] is False and guest()['shared'] is False
    router.change_mix('left','balanced')
    assert guest()['shared'] is True
    # Freeing the device (the owner's tool, not the player) releases a claim in the same way.
    router.assign_audio('right','test_headset')
    router.change_mix('right','leave')
    assert guest()['opted_out'] is True and guest()['output']=='test_headset'
    router.assign_audio('free','test_headset')
    assert guest()['opted_out'] is False and guest()['output'] is None
    router.change_mix('left','strong')
    assert guest()['shared'] is True
    router.change_mix('left','off')
    router.assign_audio('right','test_headset')
    # Route files from before auto-join have no opt-out, and a bad one is refused like any other bad key.
    good=json.loads(router.STATE_PATH.read_text())
    assert all(good[s]['opted_out'] is False for s in router.SIDES)
    old=json.loads(json.dumps(good))
    for side in router.SIDES:del old[side]['opted_out']
    router.STATE_PATH.write_text(json.dumps(old))
    assert all(routes()[s]['opted_out'] is False for s in router.SIDES) and guest()['output']=='test_headset'
    router.STATE_PATH.write_text(json.dumps({'left':'test_tv','right':'test_headset'}))  # the first, output-only format
    assert routes()['left']['output']=='test_tv' and guest()['opted_out'] is False
    for bad in ('yes','true',1,0,None,[],{}):
        refuses(dict(good,right=dict(good['right'],opted_out=bad)))
    router.STATE_PATH.write_text(json.dumps(good))
    assert routes()==json.loads(json.dumps(good))
