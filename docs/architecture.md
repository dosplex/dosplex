# Dosplex architecture

Dosplex lets two people play on one screen, each signed in to their own Steam account. This page says how it
is put together. [Session states](session-states.md) and [the limitations page](limitations.md) have the detail.

## On top of SteamOS

SteamOS's own display layer keeps driving the screen: its Gamescope in Gaming mode, or the desktop in Desktop
mode. Dosplex runs on top of it as one full-screen compositor. Both players' Steam clients and games run under
Dosplex, one per screen region, and Dosplex draws its menu over both.

While Dosplex runs, the normal single-user Steam is paused. It returns when Dosplex stops. Dosplex does not
replace the SteamOS login screen and does not start at boot: a restart of the machine brings back plain SteamOS.

## The parts

- **The compositor.** A wlroots compositor (`compositor/`). It splits the screen into two regions, keeps full
  screen, scale and picture size local to one side, and draws the menu: layout, picture size, devices, sound
  and power. One side that crashes does not take the other with it.
- **The two sides.** Main runs as the machine's own account. Guest runs as a second Unix account that the
  installer creates. Each side has its own home, its own systemd unit, its own nested Gamescope and its own
  Steam client, and signs in through Steam's own sign-in screen.
- **Controllers.** Each controller belongs to one side at a time. A relay stands between the device and Steam,
  so that a side's Steam sees only the controllers that are assigned to it, and the menu can be opened from any
  of them. The Devices page of the menu moves a controller from one side to the other.
- **Sound.** Each side has its own audio endpoint. The menu chooses where each side's sound goes.
- **The `dosplex` program.** It runs as root (`tools/dosplex`). It installs a signed release, starts and stops
  Dosplex, reports its status, updates, rolls back and uninstalls. It journals the changes it makes to the
  running system and undoes them when Dosplex stops.
  [The release process](release-process.md) describes what a release is and how it is verified.

## Isolation target

The target is not perfect hardware partitioning. The target is user and session separation within one Linux
host:

- separate Unix users
- separate homes and runtime state
- separate session processes
- visual separation enforced by the compositor
- explicit ownership of each input device
- one network namespace for both sides, with Guest on its own IPv4 loopback address (127.0.0.2): each Steam
  listens on fixed loopback ports, and without this a side's games reach the other account's Steam. IPv6
  loopback and everything that listens on all addresses stay shared

This is enough to feel like two independent machines in ordinary use. It is not a security boundary against a
player who sets out to cross it; [the limitations page](limitations.md) says exactly what is open.

## What Dosplex is not

- a custom Linux kernel
- a new graphics protocol
- a hypervisor

It is a compositor and a session layer: above the kernel and SteamOS's display layer, below the games.
