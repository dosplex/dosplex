// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 RoyÅke (Roy-Åke Martinsson)
//
// Panel logic: every rule the help/feedback overlay uses, kept pure and testable.
// The QML (logic via `import "logic.mjs" as Logic`, see the panel's README) and
// the tests both import from this file, so a change here changes both at once.
//
// Run the tests with:
//   node --test packaging/steamos/panel/test/*.mjs

// A new copy of an object with the given keys replaced or added. Written out
// by hand (no spread) because the qmlengine script import on the target
// does not accept object spread syntax; Node and Qt both accept this.
function withReplaced(object, replacement) {
    var out = {};
    for (var key in object) {
        out[key] = object[key];
    }
    for (var key2 in replacement) {
        out[key2] = replacement[key2];
    }
    return out;
}

// The on-screen keyboard, five rows. A key is either a character key
// ({ label, value }) or a control key ({ label, action }). `value` is what
// the key appends (lower case; the `shift` action upper-cases the next
// letter). `action` is one of "shift", "space", "backspace", "done".
//
// Space is a control key ("space"), not a character key: the key grid is
// fixed and a key carries exactly one of value or action, so the space's
// effect (append a space) lives in the action, not in a value.
export const KEY_ROWS = [
    [
        { label: "1", value: "1" },
        { label: "2", value: "2" },
        { label: "3", value: "3" },
        { label: "4", value: "4" },
        { label: "5", value: "5" },
        { label: "6", value: "6" },
        { label: "7", value: "7" },
        { label: "8", value: "8" },
        { label: "9", value: "9" },
        { label: "0", value: "0" },
        { label: "-", value: "-" },
        { label: "!", value: "!" },
        { label: "'", value: "'" },
    ],
    [
        { label: "q", value: "q" },
        { label: "w", value: "w" },
        { label: "e", value: "e" },
        { label: "r", value: "r" },
        { label: "t", value: "t" },
        { label: "y", value: "y" },
        { label: "u", value: "u" },
        { label: "i", value: "i" },
        { label: "o", value: "o" },
        { label: "p", value: "p" },
    ],
    [
        { label: "a", value: "a" },
        { label: "s", value: "s" },
        { label: "d", value: "d" },
        { label: "f", value: "f" },
        { label: "g", value: "g" },
        { label: "h", value: "h" },
        { label: "j", value: "j" },
        { label: "k", value: "k" },
        { label: "l", value: "l" },
    ],
    [
        { label: "z", value: "z" },
        { label: "x", value: "x" },
        { label: "c", value: "c" },
        { label: "v", value: "v" },
        { label: "b", value: "b" },
        { label: "n", value: "n" },
        { label: "m", value: "m" },
        { label: ",", value: "," },
        { label: ".", value: "." },
        { label: "?", value: "?" },
    ],
    [
        { label: "shift", action: "shift" },
        { label: "space", action: "space" },
        { label: "back", action: "backspace" },
        { label: "done", action: "done" },
    ],
];

// Where each key of a row sits on screen, as the centre of the key in key widths: the rows are centred, a control
// key is three keys wide, and the gap between keys is a tenth of a key (KeyGrid.qml draws them this way).
const KEY_GAP = 0.1;
function keyCentres(row) {
    const widths = KEY_ROWS[row].map((k) => (k.action !== undefined ? 3 : 1));
    const total = widths.reduce((a, b) => a + b, 0) + KEY_GAP * (widths.length - 1);
    const centres = [];
    let x = -total / 2;
    for (const w of widths) {
        centres.push(x + w / 2);
        x += w + KEY_GAP;
    }
    return centres;
}

// Move the cursor inside the key grid. `pos` is { row, col }, `dir` is one of
// "up", "down", "left", "right". Left and right wrap inside the row. Up and
// down go to the key that is drawn nearest above or below (the rows have
// different lengths and are centred, so that is not the same column number).
// Edge rows stay put. Returns a new { row, col }; the argument is never changed.
export function moveKey(pos, dir) {
    const rows = KEY_ROWS.length;
    const len = (row) => KEY_ROWS[row].length;
    const row = Math.max(0, Math.min(rows - 1, pos.row));
    const col = Math.max(0, Math.min(len(row) - 1, pos.col));
    if (dir === "left") {
        return { row: row, col: (col - 1 + len(row)) % len(row) };
    }
    if (dir === "right") {
        return { row: row, col: (col + 1) % len(row) };
    }
    const target = dir === "up" ? row - 1 : dir === "down" ? row + 1 : row;
    if (target === row || target < 0 || target >= rows) {
        return { row: row, col: col };
    }
    const here = keyCentres(row)[col];
    const there = keyCentres(target);
    let best = 0;
    for (let i = 1; i < there.length; i++) {
        if (Math.abs(there[i] - here) < Math.abs(there[best] - here)) best = i;
    }
    return { row: target, col: best };
}

// Press one key of the grid. `state` is the text-editing state
// { text, shift, maxLength } (maxLength defaults to 600), `key` is
// { row, col }. Returns a new state; the argument is never changed.
// Rules: a character key appends its value (upper-cased when shift is on and
// the value is a letter; shift turns itself off after one letter), a non-letter
// value is not affected by shift but still turns it off. "space" appends a
// space. "backspace" removes the last character. "shift" toggles. "done" sets
// done: true. The text never grows past maxLength.
export function pressKey(state, key) {
    const k = KEY_ROWS[key.row][key.col];
    const maxLength = state.maxLength !== undefined ? state.maxLength : 600;
    const out = {
        text: state.text,
        shift: state.shift,
        done: false,
        maxLength: maxLength,
    };
    const upper = (s) => s.toUpperCase();
    if (k.action === "shift") {
        out.shift = !state.shift;
        return out;
    }
    if (k.action === "backspace") {
        out.text = out.text.slice(0, -1);
        return out;
    }
    if (k.action === "done") {
        out.done = true;
        return out;
    }
    if (k.action === "space") {
        if (out.text.length < maxLength) out.text = out.text + " ";
        out.shift = false;
        return out;
    }
    // Character key: a letter is upper-cased while shift is on; shift turns
    // itself off after exactly one letter. A non-letter value is not affected
    // by shift but still turns it off.
    let value = k.value;
    if (state.shift && /[a-z]/.test(value)) {
        value = upper(value);
    }
    out.shift = false;
    if (out.text.length < maxLength) out.text = out.text + value;
    return out;
}

// A typed character from a real keyboard: appended as it comes (the keyboard already sends the right
// letter), at most one character at a time, nothing past maxLength. Never changes its argument.
export function typeChar(state, ch) {
    const maxLength = state.maxLength !== undefined ? state.maxLength : 600;
    let text = state.text;
    if (typeof ch === "string" && ch.length === 1) {
        const code = ch.charCodeAt(0);
        // control characters (below 32) and the delete character (127) do not type
        if (code >= 32 && code !== 127 && text.length < maxLength) {
            text = text + ch;
        }
    }
    return { text, shift: false, done: false, maxLength };
}

// Removes the last character; empty text stays empty. Never changes its argument.
export function backspace(state) {
    const maxLength = state.maxLength !== undefined ? state.maxLength : 600;
    return { text: state.text.slice(0, -1), shift: false, done: false, maxLength };
}

// The three mood choices, in on-screen order.
export const MOODS = [
    { id: "good", label: "Works well" },
    { id: "problems", label: "Some problems" },
    { id: "broken", label: "Broken" },
];

// Can the user go on from `state` = { step, mood, text, name, steamIdOn }?
// Step 1 needs a mood. Step 2 needs non-blank text unless the mood is "good"
// (no text is fine when nothing is wrong). Step 3 always passes.
export function canContinue(state) {
    if (state.step === 1) return state.mood !== null && state.mood !== undefined;
    if (state.step === 2) {
        if (state.mood === "good") return true;
        return state.text.trim().length > 0;
    }
    if (state.step === 3) return true;
    return false;
}

// Build the feedback payload exactly as the server contract allows. `state` is
// the form state at step 3, `context` is the context the overlay started with
// (side, dosplex, device, steamos, layout, steamId). Only allowed fields are
// emitted, never an unknown key. Text is trimmed and cut to 600 characters,
// name to 40. `steam_id` is included only when the switch is on AND the steam
// id from the context looks like a 17-digit Steam ID (starts with "7656").
// Empty context fields are dropped.
export function buildPayload(state, context) {
    const ctx = context || {};
    const payload = { source: "alpha-overlay", mood: state.mood };
    const text = String(state.text || "").trim().slice(0, 600);
    payload.text = text;
    const name = String(state.name || "").trim().slice(0, 40);
    if (name !== "") payload.name = name;
    const steamId = String(ctx.steam_id || ctx.steamId || "");      // the launcher writes steam_id
    if (state.steamIdOn && steamId.length === 17 && steamId.startsWith("7656")) {
        payload.steam_id = steamId;
    }
    const optional = {
        side: String(ctx.side || ""),
        dosplex: String(ctx.dosplex || ""),
        device: String(ctx.device || "").slice(0, 60),
        steamos: String(ctx.steamos || "").slice(0, 60),
        layout: String(ctx.layout || ""),
    };
    for (const key in optional) {
        if (optional[key] !== "") payload[key] = optional[key];
    }
    return payload;
}

// The human summary lines shown before sending. One line per field the
// payload actually carries, in a fixed order: mood (in the words the person chose), text, name, Steam ID,
// context. Missing fields are named ("No text", "Not signed"), never left
// out silently.
export function summaryLines(payload, moodLabel) {
    const lines = [];
    lines.push("How it was: " + (moodLabel || payload.mood || ""));
    lines.push(payload.text ? "Text: " + payload.text : "No text");
    lines.push(payload.name ? "Signed: " + payload.name : "Not signed");
    lines.push(payload.steam_id ? "Steam ID is included" : "Steam ID is not included");
    const bits = [];
    if (payload.dosplex) bits.push("Dosplex " + payload.dosplex);
    if (payload.device) bits.push(payload.device);
    if (payload.steamos) bits.push("SteamOS " + payload.steamos);
    if (payload.layout) bits.push(payload.layout);
    lines.push(bits.length ? bits.join(", ") : "No context");
    return lines;
}

// A JSON line for the outbox file: {"queued": <ISO time>, "payload": <payload>}.
export function outboxEntry(payload, queued) {
    return JSON.stringify({ queued: queued, payload: payload });
}

// Parse the outbox file. Returns a list of { queued, payload }. A damaged
// line is skipped, never thrown. A line counts only when `queued` is a string
// and `payload` is a plain object.
export function parseOutbox(text) {
    const out = [];
    if (typeof text !== "string") return out;
    for (const line of text.split("\n")) {
        const trimmed = line.trim();
        if (trimmed === "") continue;
        let obj;
        try {
            obj = JSON.parse(trimmed);
        } catch (e) {
            continue;
        }
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) continue;
        if (typeof obj.queued !== "string") continue;
        if (!obj.payload || typeof obj.payload !== "object" || Array.isArray(obj.payload)) continue;
        out.push({ queued: obj.queued, payload: obj.payload });
    }
    return out;
}

// Read the steam id out of ~/.steam/steam/steam.cfg. Finds the first user id
// marked MostRecent; if none is marked, the only user when there is exactly
// one. Returns "" when it cannot decide (or on garbage input).
export function steamIdFromLoginUsers(text) {
    if (typeof text !== "string") return "";
    const users = [];
    // User ids: lines like "76561198000000001" followed by "{" (possibly on the
    // same line), inside the "users" block.
    const re = /"(\d{17})"\s*\{([^}]*?)\}/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        const id = m[1];
        const body = m[2];
        const mostRecent = /"MostRecent"\s*"1"/.test(body);
        users.push({ id: id, mostRecent: mostRecent });
    }
    if (users.length === 0) return "";
    const recent = users.filter((u) => u.mostRecent);
    if (recent.length === 1) return recent[0].id;
    if (users.length === 1) return users[0].id;
    return "";
}

// The manual chapter for an overlay page. `page` comes from the overlay (the
// --help argument). Unknown pages go to the menu chapter.
const CHAPTERS = {
    layout: "06-layout",
    picture: "07-picture-size",
    devices: "08-controllers",
    audio: "09-sound",
    display: "10-display",
    session: "12-turn-off-sleep-restart",
};
export function chapterForPage(page) {
    return CHAPTERS[page] || "05-the-menu";
}

// The one line of help text shown under every feedback step's heading,
// saying what the keys do.
export const KEY_HINT = "D-pad to move, A to choose, B to go back";

export function keyHint() {
    return KEY_HINT;
}

// Help scrolling. `flick` is the current state of the help page:
// { contentY, contentHeight, height, chapterIndex, chapterCount }.
// dir is "up", "down", "left", "right" or "escape". Up and down scroll by
// SCROLL_STEP pixels. Left and right change to the chapter before and after;
// at the first and the last chapter they do nothing. Returns a new state; the
// argument is never changed.
export const SCROLL_STEP = 120;

export function helpScroll(flick, dir) {
    const out = {
        contentY: flick.contentY,
        contentHeight: flick.contentHeight,
        height: flick.height,
        chapterIndex: flick.chapterIndex,
        chapterCount: flick.chapterCount,
        chapterChanged: false,
    };
    const limit = Math.max(0, flick.contentHeight - flick.height);
    if (dir === "up") {
        out.contentY = Math.max(0, flick.contentY - SCROLL_STEP);
    } else if (dir === "down") {
        out.contentY = Math.min(limit, flick.contentY + SCROLL_STEP);
    } else if (dir === "right") {
        if (flick.chapterIndex < flick.chapterCount - 1) {
            out.chapterIndex = flick.chapterIndex + 1;
            out.chapterChanged = true;
        }
    } else if (dir === "left") {
        if (flick.chapterIndex > 0) {
            out.chapterIndex = flick.chapterIndex - 1;
            out.chapterChanged = true;
        }
    }
    return out;
}
