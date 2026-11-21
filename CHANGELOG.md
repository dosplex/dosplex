# Changelog

What changed in each release of Dosplex, newest first. Versions follow `MAJOR.MINOR.PATCH`, with `-alpha.N`
while a release is an alpha.

## 0.1.0-alpha.2

The first public release. An alpha for SteamOS on a Steam Deck and a Steam Machine. (0.1.0-alpha.1 was built and
signed, and not published. This release adds two things to it: the menu offers screen brightness only while the
Steam Deck's own screen is on, and the manual says which controllers open the menu.)

### What is in it

- Two players on one screen, each with their own Steam account, library, saves, controllers and sound.
- Layouts: side by side, one above the other, Main alone, Guest alone. Picture sizes per player: Native, 1:1,
  4:3 and 16:9.
- A menu for each player, driven with a controller: layout, picture size, devices, sound, session.
- Desktop mode for each side.
- Install, update, roll back and uninstall from a signed release, with the `dosplex` program.
- Start from Steam's library with a controller. Nothing runs at boot; a restart brings stock SteamOS back.
- **Turn off Dosplex**, Sleep, Restart and Shut down from Main's menu.
- **Help** in the menu opens the manual.
- Nothing is sent from your machine: no account, no telemetry, no analytics, and no feedback form in this release.

### Experimental, off unless you turn it on

- The shared game library (`dosplex library on --experimental`).
- Starting at boot (`dosplex autostart on --experimental`).

### Known limits

- Steam's own pages for Wi-Fi, power, updates and storage do not work inside a side.
- No HDR, no VRR, no refresh rate per side.
- Only a Steam Controller or the Steam Deck's own controls open the menu. Other gamepads play games but cannot
  open it; a keyboard can.
- SteamOS 3.10 with Gamescope 3.17 (the preview channel in November 2026) is not in the tested set: there, a
  picture size chosen in the menu is undone at once. The installer stops on it.
- The two players are kept apart in ordinary use, not against a player who sets out to cross the line.
  [The limitations page](docs/limitations.md) says exactly what is open.
- [Limits of the alpha](docs/manual/15-limits-of-the-alpha.md) has the full list for users.
