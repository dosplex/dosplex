#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A slow recording helper for testing compositor IPC without desktop audio."""
import json
from pathlib import Path
import sys
import time

root = Path(__file__).parent
(root / 'audio-helper.pid').write_text(str(__import__('os').getpid()))
calls = root / 'audio-calls.jsonl'
with calls.open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\n')
time.sleep(.3)
# IDs deliberately contain spaces: the compositor must pass an exact argv item.
state_path=root/'audio-mix.json'
state=json.loads(state_path.read_text()) if state_path.exists() else {'mode':'off','members':[False,False]}
if len(sys.argv)==5 and sys.argv[3]=='mix':
    side=0 if sys.argv[2]=='left' else 1
    action=sys.argv[4]
    if action in ('off','full','separated'):
        state['mode']=action
        state['members']=[False,False] if action=='off' else state['members']
        if action!='off':state['members'][side]=True
    elif action in ('gentle','balanced','strong'):
        state['mode']='separated';state['separation']=action;state['members'][side]=True
    else:state['members'][side]=action=='join'
    state_path.write_text(json.dumps(state))
print('DOSPLEX_AUDIO_3' if 'separation' in state else 'DOSPLEX_AUDIO_2')
print('M\t'+state['mode']+'\t'+''.join('1' if v else '0' for v in state['members'])+'\t'+('TV speakers'.encode().hex() if state['mode']!='off' else '-')+'\tfull')
if 'separation' in state:print('P\t'+state['separation']+'\t-\t-\t-')
for side in ('left', 'right'):
    for kind in ('output', 'input'):
        print(f'S\t{side}\t{kind}\tmuted\t-')
for kind, name in [('output', 'TV speakers'), ('output', 'USB headphones'), ('input', 'Headset microphone')]:
    print(f'D\t{kind}\tfree\t{name.encode().hex()}\t{name.encode().hex()}')
print('END')
