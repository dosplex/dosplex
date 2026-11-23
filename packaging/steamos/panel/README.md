# The DosPlex panel

The panel is a small QML program for the jobs the overlay menu hands over: **Help** (the manual, one chapter at a
time) and, in this source tree, **Feedback** (a three-step form with its own on-screen keyboard; a release does not
contain it, see below). SteamOS's `/usr/bin/qml6` runs it. It
runs only while it is open: the overlay starts it, and it ends when you close it or after ten minutes without a key.

The feedback form is not part of a release. `packaging/release-build/layout.txt` leaves `Feedback.qml` out, the menu
then shows no Feedback button, and the launcher refuses the mode. What follows about the form describes the code in
this directory.

## Files

- `Panel.qml`: the window. Reads the command line, shows one page, passes every key to it.
- `Help.qml`: reads `index.json` and the chapter files that the release build makes from the manual.
- `Feedback.qml`: the form. Nothing is sent before Send.
- `KeyGrid.qml`: the on-screen keyboard.
- `logic.mjs`: every rule, as plain JavaScript without Qt, tested under Node.

## How it is started

Not by hand. `dosplex-panel-launch.py` runs as the player's own account, collects the facts the form shows (the
DosPlex version, the device, the SteamOS version, the layout, the account's Steam ID), and starts:

```sh
qml6 Panel.qml -- --feedback --context JSON --manual-dir DIR --endpoint URL
qml6 Panel.qml -- --help PAGE --context JSON --manual-dir DIR --endpoint URL
```

`PAGE` is the menu page Help was asked from (`display`, `devices`, `audio`, `session`). The launcher sets
`QML_XHR_ALLOW_FILE_READ=1`; without it Qt refuses to read the manual's files.

## Keys

The panel gets six keys and nothing else: the four arrows, Return and Escape. A controller becomes those keys in
the compositor. There is no pointer and no touch. A real keyboard does more: it types straight into the feedback
text and the name (Backspace erases), and every other key it sends is ignored. Everything else in the panel is
still arrows, Return and Escape.

## What leaves the panel

- One HTTPS POST to the endpoint, when you choose Send, with exactly what the last screen lists.
- If that fails, one line on its output, `DOSPLEX-OUTBOX {...}`. The launcher keeps it in the account's own
  `~/.local/share/dosplex/feedback-outbox.jsonl` and sends it the next time the form is opened. The launcher never
  prints that line anywhere.

## Checking it

```sh
node --test packaging/steamos/panel/test/*.mjs
python3 tests/panel-logic-test.py
python3 tests/panel-launch-test.py
```

The QML needs a machine with Qt 6's `qml6` runtime, such as a Steam Deck. `Panel.qml` has two test hooks:
`--keys up,down,return,...` presses keys by name and `--grab FILE` saves a picture of the window and ends. Qt can run
it offscreen, which is enough to walk the screens and read Qt's warnings.
