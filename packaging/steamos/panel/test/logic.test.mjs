// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// Tests for the panel's logic. Run with:
//   node --test packaging/steamos/panel/test/*.mjs
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  KEY_ROWS,
  MOODS,
  moveKey,
  pressKey,
  canContinue,
  buildPayload,
  summaryLines,
  outboxEntry,
  parseOutbox,
  steamIdFromLoginUsers,
  chapterForPage,
  KEY_HINT,
  keyHint,
  SCROLL_STEP,
  helpScroll,
  typeChar,
  backspace,
} from '../logic.mjs';

const keyAt = (row, col) => KEY_ROWS[row][col];
const keyIndex = (row, label) => KEY_ROWS[row].findIndex((k) => k.label === label);

test('KEY_ROWS has the five rows and the required keys', () => {
  assert.equal(KEY_ROWS.length, 5);
  assert.equal(KEY_ROWS[0].map((k) => k.label).slice(0, 10).join(''), '1234567890');
  assert.equal(KEY_ROWS[0].length, 13);
  assert.equal(KEY_ROWS[1].map((k) => k.label).join(''), 'qwertyuiop');
  assert.equal(KEY_ROWS[2].map((k) => k.label).join(''), 'asdfghjkl');
  assert.equal(KEY_ROWS[3].map((k) => k.label).join(''), 'zxcvbnm,.?');
  const last = KEY_ROWS[4];
  assert.deepEqual(
    last.map((k) => k.action),
    ['shift', 'space', 'backspace', 'done'],
  );
  // -, ' and ! are on the keyboard, each exactly once
  const labels = KEY_ROWS.flat().map((k) => k.label);
  assert.equal(labels.filter((l) => l === '-').length, 1);
  assert.equal(labels.filter((l) => l === "'").length, 1);
  assert.equal(labels.filter((l) => l === '!').length, 1);
  // every key has exactly one of value or action
  for (const key of KEY_ROWS.flat()) {
    assert.ok(
      ('value' in key) !== ('action' in key),
      'key has exactly one of value or action: ' + JSON.stringify(key),
    );
  }
});

test('moveKey wraps left and right inside a row', () => {
  assert.deepEqual(moveKey({ row: 1, col: 0 }, 'left'), { row: 1, col: 9 });
  assert.deepEqual(moveKey({ row: 1, col: 9 }, 'right'), { row: 1, col: 0 });
  assert.deepEqual(moveKey({ row: 0, col: 0 }, 'left'), { row: 0, col: 12 });
  assert.deepEqual(moveKey({ row: 0, col: 12 }, 'right'), { row: 0, col: 0 });
});

test('moveKey goes to the key drawn nearest above or below', () => {
  const label = (pos) => KEY_ROWS[pos.row][pos.col].label;
  const go = (row, col, dir) => label(moveKey({ row, col }, dir));
  // the rows are centred and of different lengths, so the column number changes on the way
  assert.equal(go(3, 2, 'down'), 'space');     // c is drawn above Space, not above Delete
  assert.equal(go(3, 0, 'down'), 'shift');
  assert.equal(go(3, 9, 'down'), 'done');
  assert.equal(go(4, 3, 'up'), '?');
  assert.equal(go(4, 1, 'up'), 'v');
  assert.equal(go(1, 0, 'up'), '2');           // q sits under 2 in the wider top row
  assert.equal(go(0, 12, 'down'), 'p');
  assert.equal(go(2, 8, 'up'), 'o');
  // the edge rows stay put
  assert.deepEqual(moveKey({ row: 0, col: 4 }, 'up'), { row: 0, col: 4 });
  assert.deepEqual(moveKey({ row: 4, col: 2 }, 'down'), { row: 4, col: 2 });
});

test('moveKey stays on the edge rows and an unknown direction moves nowhere', () => {
  assert.deepEqual(moveKey({ row: 0, col: 3 }, 'up'), { row: 0, col: 3 });
  assert.deepEqual(moveKey({ row: 4, col: 2 }, 'down'), { row: 4, col: 2 });
  assert.deepEqual(moveKey({ row: 1, col: 2 }, 'sideways'), { row: 1, col: 2 });
});

test('pressKey types digits, punctuation and letters', () => {
  const s = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 0, col: 0 });
  assert.equal(s.text, '1');
  const comma = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 3, col: 7 });
  assert.equal(comma.text, ',');
  const bang = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 0, col: 11 });
  assert.equal(bang.text, '!');
  const q = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 1, col: 0 });
  assert.equal(q.text, 'q');
  const dash = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 0, col: 10 });
  assert.equal(dash.text, '-');
  const quote = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 0, col: 12 });
  assert.equal(quote.text, "'");
});

test('pressKey applies shift to one letter and turns itself off', () => {
  let s = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 4, col: 0 });
  assert.equal(s.shift, true);
  s = pressKey(s, { row: 1, col: 0 });
  assert.equal(s.text, 'Q');
  assert.equal(s.shift, false);
  s = pressKey(s, { row: 1, col: 1 });
  assert.equal(s.text, 'Qw');
});

test('pressKey shift toggles twice back to off', () => {
  let s = pressKey({ text: '', shift: false, maxLength: 600 }, { row: 4, col: 0 });
  assert.equal(s.shift, true);
  s = pressKey(s, { row: 4, col: 0 });
  assert.equal(s.shift, false);
});

test('pressKey space and backspace', () => {
  let s = pressKey({ text: 'ab', shift: false, maxLength: 600 }, { row: 4, col: 1 });
  assert.equal(s.text, 'ab ');
  s = pressKey(s, { row: 4, col: 2 });
  assert.equal(s.text, 'ab');
  s = pressKey(s, { row: 4, col: 2 });
  assert.equal(s.text, 'a');
  s = pressKey(s, { row: 4, col: 2 });
  assert.equal(s.text, '');
});

test('pressKey done sets done and keeps the text', () => {
  const s = pressKey({ text: 'hi', shift: false, maxLength: 600 }, { row: 4, col: 3 });
  assert.equal(s.done, true);
  assert.equal(s.text, 'hi');
});

test('pressKey never grows the text past maxLength', () => {
  let s = { text: 'abc', shift: false, maxLength: 3 };
  s = pressKey(s, { row: 1, col: 0 });
  assert.equal(s.text, 'abc');
  s = pressKey(s, { row: 4, col: 1 });
  assert.equal(s.text, 'abc');
  s = pressKey(s, { row: 4, col: 0 });
  assert.equal(s.text, 'abc');
  assert.equal(s.shift, true);
  // a backspace makes room again
  s = pressKey(s, { row: 4, col: 2 });
  assert.equal(s.text, 'ab');
  s = pressKey(s, { row: 1, col: 0 });
  assert.equal(s.text, 'abQ');
});

test('pressKey and moveKey return new objects and never change their arguments', () => {
  const before = { text: 'x', shift: false, maxLength: 5 };
  const frozen = Object.freeze(before);
  const after = pressKey(frozen, { row: 1, col: 0 });
  assert.notEqual(after, before);
  assert.equal(before.text, 'x');
  assert.equal(after.text, 'xq');

  const cur = { row: 1, col: 0 };
  const moved = moveKey(cur, 'right');
  assert.notEqual(moved, cur);
  assert.equal(cur.col, 0);
});

test('MOODS is the three choices', () => {
  assert.deepEqual(MOODS, [
    { id: 'good', label: 'Works well' },
    { id: 'problems', label: 'Some problems' },
    { id: 'broken', label: 'Broken' },
  ]);
});

test('canContinue per step', () => {
  assert.equal(canContinue({ step: 1, mood: null, text: '', name: '', steamIdOn: false }), false);
  assert.equal(canContinue({ step: 1, mood: 'broken', text: '', name: '', steamIdOn: false }), true);
  // step 2 needs text unless the mood is good
  assert.equal(canContinue({ step: 2, mood: 'broken', text: '', name: '', steamIdOn: false }), false);
  assert.equal(canContinue({ step: 2, mood: 'broken', text: '   ', name: '', steamIdOn: false }), false);
  assert.equal(canContinue({ step: 2, mood: 'broken', text: 'lag ', name: '', steamIdOn: false }), true);
  assert.equal(canContinue({ step: 2, mood: 'good', text: '', name: '', steamIdOn: false }), true);
  assert.equal(canContinue({ step: 3, mood: 'good', text: '', name: '', steamIdOn: false }), true);
});

const CONTEXT = {
  side: 'main',
  dosplex: '0.1.0-alpha.1',
  device: 'Steam Deck OLED',
  steamos: '3.9.5',
  layout: 'side-by-side',
  steamId: '76561198012345678',
};

test('buildPayload is exactly the allowed fields', () => {
  const payload = buildPayload(
    { step: 3, mood: 'problems', text: 'lag ', name: '  Sam ', steamIdOn: true },
    CONTEXT,
  );
  assert.deepEqual(
    Object.keys(payload).sort(),
    ['device', 'dosplex', 'layout', 'mood', 'name', 'side', 'source', 'steam_id', 'steamos', 'text'].sort(),
  );
  assert.equal(payload.source, 'alpha-overlay');
  assert.equal(payload.mood, 'problems');
  assert.equal(payload.text, 'lag');
  assert.equal(payload.name, 'Sam');
  assert.equal(payload.steam_id, '76561198012345678');
  assert.equal(payload.side, 'main');
});

test('buildPayload trims text to 600 and context fields to 60', () => {
  const long = 'a'.repeat(700);
  const payload = buildPayload({ step: 3, mood: 'good', text: '  ' + long + '  ', name: '', steamIdOn: false }, CONTEXT);
  assert.equal(payload.text.length, 600);
  const ctx = { ...CONTEXT, device: 'd'.repeat(80) };
  const p2 = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: false }, ctx);
  assert.equal(p2.device.length, 60);
});

test('buildPayload name is trimmed to 40 and only when not blank', () => {
  const longName = 'x'.repeat(50);
  const payload = buildPayload({ step: 3, mood: 'good', text: '', name: longName, steamIdOn: false }, CONTEXT);
  assert.equal(payload.name, 'x'.repeat(40));
  const blank = buildPayload({ step: 3, mood: 'good', text: '', name: '   ', steamIdOn: false }, CONTEXT);
  assert.ok(!('name' in blank));
  const absent = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: false }, CONTEXT);
  assert.ok(!('name' in absent));
});

test('buildPayload never has steam_id when the switch is off or the id is malformed', () => {
  const off = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: false }, CONTEXT);
  assert.ok(!('steam_id' in off));
  const shortId = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: true }, { ...CONTEXT, steamId: '12345' });
  assert.ok(!('steam_id' in shortId));
  const letters = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: true }, { ...CONTEXT, steamId: 'abc61198012345678' });
  assert.ok(!('steam_id' in letters));
  const missing = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: true }, { ...CONTEXT, steamId: '' });
  assert.ok(!('steam_id' in missing));
  const wrongLength = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: true }, { ...CONTEXT, steamId: '765611980123456789' });
  assert.ok(!('steam_id' in wrongLength));
});

test('buildPayload drops empty context fields', () => {
  const ctx = { side: '', dosplex: '0.1.0', device: '', steamos: '', layout: 'side-by-side', steamId: '' };
  const payload = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: true }, ctx);
  assert.ok(!('side' in payload));
  assert.ok(!('device' in payload));
  assert.ok(!('steamos' in payload));
  assert.ok(payload.dosplex === '0.1.0');
  assert.ok(payload.layout === 'side-by-side');
});

test('buildPayload never contains an unknown key', () => {
  const allowed = new Set(['source', 'mood', 'text', 'side', 'dosplex', 'device', 'steamos', 'layout', 'name', 'steam_id']);
  const payload = buildPayload(
    { step: 3, mood: 'broken', text: 'no sound', name: 'A', steamIdOn: true },
    { ...CONTEXT, extra: 'no', other: 7 },
  );
  for (const key of Object.keys(payload)) {
    assert.ok(allowed.has(key), 'unknown key in payload: ' + key);
  }
});

test('summaryLines names exactly what the payload carries', () => {
  const payload = buildPayload(
    { step: 3, mood: 'problems', text: 'lag on the left', name: 'Sam', steamIdOn: true },
    CONTEXT,
  );
  const lines = summaryLines(payload);
  assert.equal(lines.length, 5);
  assert.ok(lines[0].includes('problems'));
  assert.ok(lines[1].includes('lag on the left'));
  assert.ok(lines[2].includes('Sam'));
  assert.ok(lines[3].includes('Steam ID is included'));
  assert.ok(
    lines[4].includes('0.1.0-alpha.1') &&
      lines[4].includes('Steam Deck OLED') &&
      lines[4].includes('3.9.5') &&
      lines[4].includes('side-by-side'),
  );
});

test('summaryLines covers the unsigned, no-text, switch-off case', () => {
  const payload = buildPayload({ step: 3, mood: 'good', text: '', name: '', steamIdOn: false }, CONTEXT);
  const lines = summaryLines(payload);
  assert.equal(lines.length, 5);
  assert.ok(lines[1].includes('No text'));
  assert.ok(lines[2].includes('Not signed'));
  assert.ok(lines[3].includes('Steam ID is not included'));
});

test('summaryLines covers a partial context', () => {
  const payload = buildPayload({ step: 3, mood: 'good', text: 'fine', name: '', steamIdOn: false }, { dosplex: '0.1.0' });
  const lines = summaryLines(payload);
  assert.equal(lines.length, 5);
  assert.ok(lines[4].includes('0.1.0'));
});

test('outboxEntry and parseOutbox round-trip', () => {
  const payload = buildPayload({ step: 3, mood: 'good', text: 'ok', name: '', steamIdOn: false }, CONTEXT);
  const line = outboxEntry(payload, '2026-11-19T12:00:00Z');
  const parsed = parseOutbox(line);
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].queued, '2026-11-19T12:00:00Z');
  assert.deepEqual(parsed[0].payload, payload);
});

test('parseOutbox skips damaged lines and never throws', () => {
  const good = outboxEntry({ source: 'alpha-overlay', mood: 'good', text: 'a' }, '2026-11-19T12:00:00Z');
  const text = [
    good,
    '{"queued": "2026-11-19T12:01:00Z", "payload": {',
    'not json at all',
    '',
    '{"queued": 5, "payload": {}}',
    '{"queued": "2026-11-19T12:02:00Z"}',
    '{"queued": "2026-11-19T12:03:00Z", "payload": [1,2]}',
  ].join('\n');
  const parsed = parseOutbox(text);
  assert.equal(parsed.length, 1);
  assert.deepEqual(parsed[0].payload, { source: 'alpha-overlay', mood: 'good', text: 'a' });
});

test('steamIdFromLoginUsers: two users, one marked most recent', () => {
  const vdf = [
    '    "users"',
    '    {',
    '        "76561198000000001"',
    '        {',
    '            "AccountName" "first"',
    '            "PersonaName" "First"',
    '            "MostRecent" "0"',
    '        }',
    '        "76561198000000002"',
    '        {',
    '            "AccountName" "second"',
    '            "PersonaName" "Second"',
    '            "MostRecent" "1"',
    '        }',
    '    }',
  ].join('\n');
  assert.equal(steamIdFromLoginUsers(vdf), '76561198000000002');
});

test('steamIdFromLoginUsers: one user with no MostRecent', () => {
  const vdf = [
    '    "users"',
    '    {',
    '        "76561198000000042"',
    '        {',
    '            "AccountName" "only"',
    '            "PersonaName" "Only"',
    '        }',
    '    }',
  ].join('\n');
  assert.equal(steamIdFromLoginUsers(vdf), '76561198000000042');
});

test('steamIdFromLoginUsers: two users, no MostRecent, gives up', () => {
  const vdf = [
    '    "users"',
    '    {',
    '        "76561198000000001" { "AccountName" "a" }',
    '        "76561198000000002" { "AccountName" "b" }',
    '    }',
  ].join('\n');
  assert.equal(steamIdFromLoginUsers(vdf), '');
});

test('steamIdFromLoginUsers: garbage and nothing return empty', () => {
  assert.equal(steamIdFromLoginUsers('not a vdf file'), '');
  assert.equal(steamIdFromLoginUsers(''), '');
  assert.equal(steamIdFromLoginUsers(null), '');
});

test('chapterForPage maps each page and defaults to the menu', () => {
  assert.equal(chapterForPage('layout'), '06-layout');
  assert.equal(chapterForPage('picture'), '07-picture-size');
  assert.equal(chapterForPage('devices'), '08-controllers');
  assert.equal(chapterForPage('audio'), '09-sound');
  assert.equal(chapterForPage('display'), '10-display');
  assert.equal(chapterForPage('session'), '12-turn-off-sleep-restart');
  assert.equal(chapterForPage('first'), '05-the-menu');
  assert.equal(chapterForPage('whatever'), '05-the-menu');
  assert.equal(chapterForPage(undefined), '05-the-menu');
});

test('KEY_HINT is the unified hint string and keyHint returns it', () => {
  assert.equal(KEY_HINT, 'D-pad to move, A to choose, B to go back');
  assert.equal(keyHint(), KEY_HINT);
});

test('SCROLL_STEP is 120 pixels', () => {
  assert.equal(SCROLL_STEP, 120);
});

test('helpScroll: down scrolls by SCROLL_STEP and clamps at the bottom', () => {
  const s1 = helpScroll({ contentY: 0, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'down');
  assert.equal(s1.contentY, 120);
  assert.equal(s1.chapterChanged, false);
  const s2 = helpScroll({ contentY: 500, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'down');
  assert.equal(s2.contentY, 600); // 500+120=620, limit=600, clamped
  assert.equal(s2.chapterChanged, false);
});

test('helpScroll: up scrolls back and clamps at 0', () => {
  const s1 = helpScroll({ contentY: 200, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'up');
  assert.equal(s1.contentY, 80);
  const s2 = helpScroll({ contentY: 50, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'up');
  assert.equal(s2.contentY, 0); // 50-120=-70, clamped to 0
});

test('helpScroll: right goes to the next chapter and stops at the last', () => {
  const s = helpScroll({ contentY: 300, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'right');
  assert.equal(s.chapterChanged, true);
  assert.equal(s.chapterIndex, 1);
  const last = helpScroll({ contentY: 0, contentHeight: 1000, height: 400, chapterIndex: 6, chapterCount: 7 }, 'right');
  assert.equal(last.chapterChanged, false);
  assert.equal(last.chapterIndex, 6);
});

test('buildPayload reads the Steam ID under the name the launcher writes', () => {
  const state = { step: 3, mood: 'good', text: '', name: '', steamIdOn: true };
  assert.equal(buildPayload(state, { steam_id: '76561198012345678' }).steam_id, '76561198012345678');
  assert.ok(!('steam_id' in buildPayload({ ...state, steamIdOn: false }, { steam_id: '76561198012345678' })));
});

test('helpScroll: left goes back one chapter and is a no-op at the first', () => {
  const s1 = helpScroll({ contentY: 0, contentHeight: 1000, height: 400, chapterIndex: 3, chapterCount: 7 }, 'left');
  assert.equal(s1.chapterIndex, 2);
  assert.equal(s1.chapterChanged, true);
  const s2 = helpScroll({ contentY: 0, contentHeight: 1000, height: 400, chapterIndex: 0, chapterCount: 7 }, 'left');
  assert.equal(s2.chapterIndex, 0);
  assert.equal(s2.chapterChanged, false);
});

test('helpScroll: does not change the input object', () => {
  const orig = { contentY: 100, contentHeight: 1000, height: 400, chapterIndex: 2, chapterCount: 7 };
  const copy = { ...orig };
  helpScroll(orig, 'down');
  assert.deepEqual(orig, copy);
});

test('typeChar appends a letter, capital, digit, space and sign as it is', () => {
  assert.equal(typeChar({ text: '', shift: false, maxLength: 600 }, 'h').text, 'h');
  assert.equal(typeChar({ text: '', shift: false, maxLength: 600 }, 'K').text, 'K');
  assert.equal(typeChar({ text: '', shift: false, maxLength: 600 }, '7').text, '7');
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, ' ').text, 'a ');
  assert.equal(typeChar({ text: '', shift: false, maxLength: 600 }, '&').text, '&');
  assert.equal(typeChar({ text: 'h', shift: false, maxLength: 600 }, 'ä').text, 'hä');
  // the result carries no shift, done is false, maxLength passes through
  const s = typeChar({ text: '', shift: true, maxLength: 40 }, 'x');
  assert.equal(s.shift, false);
  assert.equal(s.done, false);
  assert.equal(s.maxLength, 40);
  assert.equal(s.text, 'x');
});

test('typeChar refuses an emoji of two code units: it is not one character', () => {
  // a plain emoji is two UTF-16 code units (a surrogate pair); its length is 2, so the
  // form counts it as more than one simple character and refuses it
  assert.equal('😀'.length, 2);
  assert.equal(typeChar({ text: '', shift: false, maxLength: 600 }, '😀').text, '');
});

test('typeChar refuses a control character and the delete character', () => {
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, '\n').text, 'a');
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, '\t').text, 'a');
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, '\u007f').text, 'a');
});

test('typeChar refuses an empty string and a two-letter string', () => {
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, '').text, 'a');
  assert.equal(typeChar({ text: 'a', shift: false, maxLength: 600 }, 'bc').text, 'a');
});

test('typeChar never grows the text past maxLength (default 600)', () => {
  const three = typeChar({ text: 'abc', shift: false, maxLength: 3 }, 'd');
  assert.equal(three.text, 'abc');
  // default maxLength is 600
  const at = typeChar({ text: 'a'.repeat(600), shift: false }, 'b');
  assert.equal(at.text, 'a'.repeat(600));
  const below = typeChar({ text: 'a'.repeat(599), shift: false }, 'b');
  assert.equal(below.text, 'a'.repeat(599) + 'b');
});

test('typeChar never changes its argument', () => {
  const before = { text: 'x', shift: true, maxLength: 5 };
  const frozen = Object.freeze(before);
  const after = typeChar(frozen, 'q');
  assert.notEqual(after, frozen);
  assert.equal(before.text, 'x');
  assert.equal(after.text, 'xq');
});

test('backspace removes the last character; empty text stays empty', () => {
  assert.equal(backspace({ text: 'abc', shift: false, maxLength: 600 }).text, 'ab');
  assert.equal(backspace({ text: 'a', shift: false, maxLength: 600 }).text, '');
  assert.equal(backspace({ text: '', shift: false, maxLength: 600 }).text, '');
  const s = backspace({ text: 'abc', shift: true, maxLength: 3 });
  assert.equal(s.shift, false);
  assert.equal(s.done, false);
  assert.equal(s.maxLength, 3);
});

test('backspace never changes its argument', () => {
  const before = { text: 'ab', shift: false, maxLength: 5 };
  const frozen = Object.freeze(before);
  const after = backspace(frozen);
  assert.notEqual(after, frozen);
  assert.equal(before.text, 'ab');
  assert.equal(after.text, 'a');
});
