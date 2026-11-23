# Changelog

What changed in each release of DosPlex, newest first. Versions follow `MAJOR.MINOR.PATCH`, with `-alpha.N`
while a release is an alpha.

## 0.1.0-alpha.6

### Fixed

- After a new install on a machine with a Steam Controller receiver, a controller could not be given to Guest: its
  row said "Transfer failed; controls returned". The empty slots of the receiver counted as sleeping controllers
  that Main was waiting for, and nothing was allowed to leave Main. A controller now moves at once when Main keeps
  another controller that is awake.
- A stop or an uninstall typed in a terminal looked hung: both halves said "Waiting for Steam..." while Steam was
  being closed. The screen now says "Closing Steam on both sides, then returning to SteamOS…".
- The line that asks to keep a controller move showed an empty box instead of its arrow on a Steam Deck. It is
  written in words now.

### Changed

- The name is written DosPlex: in the menu, the messages, the manual and the documents. Commands, paths and file
  names stay lowercase (`dosplex`). An update from 0.1.0-alpha.5 keeps the entry in Steam's library, which Steam
  goes on listing under the name it was added with.
- Controllers find their player by themselves. The first controller you wake is Main's. While the second player is
  on the screen and has no controller that is awake, the next one is theirs; on a Steam Deck the built-in controls
  are Main's, so the first other controller is the second player's. What you choose under **Devices** is remembered
  and always wins. An empty slot of a Steam Controller receiver belongs to nobody until a controller wakes on it.
- A Steam Deck with a Steam Controller receiver plugged in uses it without any option at the start.
- While a side starts, it shows "Starting Steam…" with a progress bar. A new account's first start takes a few
  minutes and the screen says so.
- `install` no longer starts DosPlex by itself. Add `--start` to start it right after the install; `--no-start` is
  still accepted and does nothing. The last lines of the install say how to start it: from Steam's library, from
  the DosPlex icon on the desktop, or in a terminal.
- The entry in Steam's library has the DosPlex icon, and the same launcher is put on the main user's desktop
  (`~/Desktop/DosPlex.desktop`) when that folder exists. `entry off` and `uninstall` remove it.
- The precheck inside `install` prints one line for the checks that passed and the whole line only for a check that
  did not. `preflight` still prints every check.

## 0.1.0-alpha.5

The first public release. An alpha for SteamOS on a Steam Deck and a Steam Machine. (0.1.0-alpha.1 to 0.1.0-alpha.4
were built and signed, and not published.)

### What is in it

- Two players on one screen, each with their own Steam account, library, saves, controllers and sound.
- Layouts: side by side, one above the other, Main alone, Guest alone. Picture sizes per player: Native (the
  default), 1:1, 4:3 and 16:9.
- A menu for each player, driven with a controller: layout, picture size, devices, sound, session.
- Desktop mode for each side.
- Install, update, roll back and uninstall from a signed release, with the `dosplex` program.
- Start from Steam's library with a controller; the installer adds DosPlex to the library by itself while Steam is running. Nothing runs at boot; a restart brings stock SteamOS back.
- **Turn off DosPlex**, Sleep, Restart and Shut down from Main's menu.
- **Help** in the menu opens the manual.
- Nothing is sent from your machine: no account, no telemetry, no analytics, and no feedback form in this release.

### Experimental, off unless you turn it on

- The shared game library (`dosplex library on --experimental`).
- Starting at boot (`dosplex autostart on --experimental`).

### Experimental, on unless you turn it off

- The menu from other gamepads: a DualShock 4, a DualSense, an Xbox or a Switch Pro controller opens and drives its
  player's menu with the same chord. That player's side is paused while the menu is open because of it.
  `sudo touch /var/lib/dosplex/gamepad-menu-off` and a restart of DosPlex switch it off.

### Known limits

- Steam's own pages for Wi-Fi, power, updates and storage do not work inside a side.
- No HDR, no VRR, no refresh rate per side.
- With two players on the screen the machine does not go to sleep by itself: choose Sleep in Main's menu or press
  the power button. Sleep in Guest's own Steam menu does not work. A session that was in Standby for more than
  an hour asks for sleep when it comes back: Guest's part of the screen then stays black until Reset my side,
  and Main's puts the machine to sleep.
- A Steam Controller, the Steam Deck's own controls and a keyboard open the menu without side effects. Other
  gamepads open it experimentally (see above). The 2015 Steam Controller with its dongle is not supported.
- DosPlex switches on Steam's setting "Enable Native Resolution on External Displays" for each account's Steam, once.
  Newer Steam clients need it for a picture size above 1920x1200 pixels; a side that was just created gets it at its
  second start.
- The two players are kept apart in ordinary use, not against a player who sets out to cross the line.
  [The limitations page](docs/limitations.md) says exactly what is open.
- [Limits of the alpha](docs/manual/15-limits-of-the-alpha.md) has the full list for users.
