# Game compatibility

*This page is generated from the compatibility scenarios, the list of accepted scenarios and the project's reviewed test results. Do not edit it by hand.*

Dosplex runs two Steam accounts side by side, and a game that works full screen can still fail in half a screen. This page lists every game scenario the project tracks, what it needs before it counts as working, and where it stands.

Nothing here is a promise that a game works for you. A scenario is **Pending** until a person has played it on a named build and the result has been reviewed. Pending means unverified, not broken.

## How to read this page

| Status | Meaning |
| --- | --- |
| Accepted | The project owner confirmed that the scenario passed. Every release candidate has to pass it again before it ships. |
| Verified | The scenario passed on a build. The project owner has not accepted it yet. |
| Failing | The latest recorded result failed. |
| Pending | There is no usable result yet. |

The evidence a scenario needs is one of three kinds, and a stronger kind counts for a weaker one:

- **Synthetic:** virtual controllers on real hardware. It shows that a path works.
- **Visual:** frames captured after the action and checked by measurement. It shows that the picture is right.
- **Human:** a person plays with real controllers and audio. It shows that the game is good to play.

**T1** is a TV or monitor driven by a Steam Machine or a docked Steam Deck, at 1080p or 4K. It is the core use case. **T2** is the Steam Deck's own screen, where support is limited and is tracked separately. A result in one tier never counts for the other.

*Main* and *Guest* are the two players. *Side by side* is the default layout, and *Top / bottom* is supported and measured as well. *Native* gives a game every pixel of the player's area. *1:1*, *4:3* and *16:9* force a shape for games that fail at Native. The [user guide](user-guide.md) explains how to choose.

## Summary

| Tier | Scenarios | Accepted | Verified | Failing | Pending |
| --- | --- | --- | --- | --- | --- |
| T1 | 36 | 0 | 0 | 5 | 31 |
| T2 | 0 | 0 | 0 | 0 | 0 |

## Games

### Alien Breed: Impact

Steam app 22610.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Keeps its fixed window size (`ALIENBREED-SPLIT-MAIN-169-001`) | T1, Steam Deck, 4K display, Side by side, Main, 16:9, Keep proportions | Human | Pending | - |  |
| Keeps its fixed window size (`ALIENBREED-SPLIT-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native | Human | Pending | - |  |
| Runs from the SD card (`ALIENBREED-SDCARD-MAIN-001`) | T1, Steam Machine, 4K display, Side by side, Main, 16:9, Keep proportions | Human | Pending | - | With the game on the SD card, Steam could not write to the card in Main's side, so it skipped the game's first-run setup and the game closed after a few seconds. The fix, which keeps Main's own SD card writable in its side, is still being verified. |
| Runs from the SD card (`ALIENBREED-SDCARD-SINGLE-001`) | T1, Steam Machine, 4K display, Full screen, Main, 16:9, Keep proportions | Human | Pending | - | With the game on the SD card, Steam could not write to the card in Main's side, so it skipped the game's first-run setup and the game closed after a few seconds. The fix, which keeps Main's own SD card writable in its side, is still being verified. |

### Aliens versus Predator Classic 2000

Steam app 3730.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`AVP-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native, Fullscreen | Human | Pending | - | At Native the game stopped with a Direct3D initialisation error until Wine's mode-list setting was applied to its prefix. The general fix, which applies that setting automatically, is still being verified. |
| Starts and plays (`AVP-SPLIT-MAIN-169-001`) | T1, Steam Machine, 4K display, Side by side, Main, 16:9, Keep proportions, Fullscreen | Human | Pending | - |  |
| Starts and plays (`AVP-SPLIT-GUEST-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Guest, Native, Fullscreen | Human | Pending | - |  |

### Carrier Command 2

Steam app 1489630.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`CC2-SPLIT-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Crimsonland

Steam app 262830.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`CRIMSON-SINGLE-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Full screen, Main, Native | Human | Pending | - |  |
| Starts and plays (`CRIMSON-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |
| Starts and plays (`CRIMSON-SPLIT-GUEST-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native | Human | Failing | - |  |
| Keeps running when its controller moves to the other side (`CRIMSON-CONTROLLER-SWITCH-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Dead Cells

Steam app 588650.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`DEADCELLS-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### FlatOut 2

Steam app 2990.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`FLATOUT2-SPLIT-GUEST-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native | Human | Failing | - |  |
| Starts and plays (`FLATOUT2-SPLIT-GUEST-169-001`) | T1, Steam Deck, 4K display, Side by side, Guest, 16:9, Keep proportions | Human | Failing | - |  |
| Keeps running when its controller moves to the other side (`FLATOUT2-CONTROLLER-SWITCH-001`) | T1, Steam Machine, 4K display, Side by side, Guest, Native | Human | Pending | - |  |
| Steam's gamepad output reaches the game's menu (`FLATOUT2-GAMEPAD-OUTPUT-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native, Steam Input enabled, Virtual test controller | Synthetic | Pending | - |  |
| The title screen accepts Start with the per-game binding (`FLATOUT2-START-BUTTON-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native, Steam Input enabled, Steam Controller | Human | Pending | - | The title screen did not accept the controller's Start button until Start was bound to Enter in Steam's controller settings for this game. Needs a per-game setting. |
| Every menu works with the controller alone (`FLATOUT2-MENUS-CONTROLLER-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native, Steam Input enabled, Steam Controller | Human | Pending | - |  |
| A race can be driven with the controller (`FLATOUT2-GAMEPAD-GAMEPLAY-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native, Steam Input enabled, Steam Controller | Human | Pending | - |  |

### Left 4 Dead 2

Steam app 550.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`L4D2-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native, Fullscreen | Human | Pending | - |  |

### Magicka

Steam app 42910.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Stays running (`MAGICKA-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - | The game exits within 40 seconds of launch at Native, with and without Wine's mode-list setting. The cause is not yet investigated. |

### Project Zomboid

Steam app 108600.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`ZOMBOID-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Quake

Steam app 2310.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`QUAKE-SINGLE-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Full screen, Main, Native, Fullscreen | Human | Pending | - |  |
| Starts and plays (`QUAKE-SPLIT-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native, Fullscreen | Human | Pending | - | At Native the game crashed during display setup until Wine's mode-list setting was applied to its prefix. The general fix, which applies that setting automatically, is still being verified. |
| Starts and plays (`QUAKE-SPLIT-GUEST-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Guest, Native, Fullscreen | Human | Pending | - |  |
| Keeps running when its controller moves to the other side (`QUAKE-CONTROLLER-SWITCH-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native, Fullscreen | Human | Pending | - |  |

### Risk of Rain (2013)

Steam app 248820.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`ROR-SINGLE-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Full screen, Main, Native | Human | Pending | - |  |
| Starts and plays (`ROR-SPLIT-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Risk of Rain 2

Steam app 632360.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`ROR2-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Risk of Rain Returns

Steam app 1337520.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`RORR-SPLIT-MAIN-NATIVE-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - |  |

### Valheim

Steam app 892970.

| Scenario | Setup | Needs | Status | Last verified | Notes |
| --- | --- | --- | --- | --- | --- |
| Starts and plays (`VALHEIM-SPLIT-MAIN-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native | Human | Failing | - |  |
| Starts and plays (`VALHEIM-SPLIT-GUEST-NATIVE-001`) | T1, Steam Deck, 4K display, Side by side, Guest, Native | Human | Failing | - |  |
| Keeps running when its controller moves to the other side (`VALHEIM-CONTROLLER-SWITCH-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native | Human | Pending | - | A running game keeps the gamepad handles it had before a controller moved to the other side, so the game has to be restarted after a move. |
| A second wireless controller navigates the game's menu (`VALHEIM-SECOND-WIRELESS-MENU-001`) | T1, Steam Deck, 4K display, Side by side, Main, Native, Virtual test controller | Synthetic | Pending | - |  |
| Fills the half after a normal launch (`VALHEIM-NATIVE-FILL-001`) | T1, Steam Machine, 4K display, Side by side, Main, Native | Human | Pending | - | A normal launch restores the game's saved window size and leaves bars; resizing the window by hand fills the half. Automatic sizing is not implemented yet. |

## Where this comes from

The scenarios and their pass criteria are in [compatibility/scenarios.yaml](../compatibility/scenarios.yaml), and the accepted ones are listed in [compatibility/accepted.lock](../compatibility/accepted.lock). Raw logs, controller identifiers and device details stay private; only reviewed summaries reach this page.
