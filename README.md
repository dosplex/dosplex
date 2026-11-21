# Dosplex

**Two Steam accounts. One screen. Two independent activities.**

Dosplex lets two people share one SteamOS machine and one screen while each has
their own Steam account, session, controls and audio. Its core uses are couch co-op with
separate game instances and playing a game while someone else watches a movie.
Each person can use their own headset, or one can use the TV speakers.

The goal is to bring couch co-op to games without built-in local co-op and make
a shared TV useful for different activities at once. Compatibility is verified
per game, media application and device.

**Dosplex 0.1.0 is an alpha.** It works on the devices and versions it was tested on, some things are
missing, and your feedback is what improves it. See [what alpha means](docs/manual/01-what-dosplex-is.md).

## What you get

- **Two sides on one screen.** Each player has their own Steam account, library, saves, controllers and sound.
- **Layouts.** Side by side, one above the other, or one player on the whole screen while the other waits.
- **A menu for each player.** Layout, picture size, controllers and sound, with a controller.
- **Started by hand.** You start Dosplex from Steam's library. Nothing runs at boot, and a restart of the
  machine always brings back plain SteamOS.
- **Easy to leave.** **Turn off Dosplex** in the menu brings stock Steam back. Uninstalling removes it.
- **Help in the menu.** The manual is one button away, with a controller.
- **Nothing leaves your machine.** No account, no telemetry, no analytics. [PRIVACY.md](PRIVACY.md) says so in full.

## What you need

- A Steam Machine, or a Steam Deck docked to a TV or monitor. A Deck's own screen works too, with less room
  for each player.
- Two Steam accounts and two controllers.
- The SteamOS, Gamescope and Steam client versions the release was tested on. The release notes list them,
  and the installer refuses other versions unless you say so.

## Install

Read [the manual](docs/manual/README.md) first; it is short. In brief, in Desktop mode:

1. Download the release archive and its signature file from the [releases page](https://github.com/dosplex/dosplex/releases).
2. Take the `dosplex` program out of it and compare its SHA-256 with the one on the release page:
   `tar -xzf dosplex-VERSION.tar.gz dosplex-VERSION/dosplex && sha256sum dosplex-VERSION/dosplex`.
3. Check your device: `sudo ./dosplex-VERSION/dosplex preflight --release dosplex-VERSION.tar.gz`.
4. Install: `sudo ./dosplex-VERSION/dosplex install --release dosplex-VERSION.tar.gz`.
5. Add **Dosplex** to Steam's library once, then start it from there with a controller.

[Install](docs/manual/03-install.md) has every step, and
[Update, roll back, uninstall](docs/manual/13-update-roll-back-uninstall.md) has the way back.

## Know before you try it

- [Limits of the alpha](docs/manual/15-limits-of-the-alpha.md) lists what does not work yet.
- Dosplex keeps two players apart in ordinary use. It is not yet a wall against a player who sets out to
  cross it: use the alpha with people you trust at the same screen. [The limitations page](docs/limitations.md)
  says exactly what is open.
- The shared game library and the start at boot are in the program, off, and
  [experimental](docs/manual/17-experimental.md).
- Game compatibility is checked game by game. [The compatibility page](docs/compatibility.md) shows what has
  been played and accepted so far.

## Tell us how it went

Dosplex sends nothing by itself, so you tell us in your own words:

- the channel `#alpha-testers` on [the Dosplex Discord](https://discord.gg/HSBJhQPA6A), for how it went;
- the [issue tracker](https://github.com/dosplex/dosplex/issues), for a defect you want to follow until it is fixed.

[Tell us how it went](docs/manual/16-tell-us-how-it-went.md) says what helps us. Never include passwords, Steam
Guard codes or sign-in QR codes. Report security problems privately, as [SECURITY.md](SECURITY.md) describes.

## How it works

A wlroots compositor runs on top of SteamOS's Gaming mode and splits the screen. Each side runs as its own
Unix user with its own nested Gamescope and Steam, its own controllers and its own audio. A menu built into
the compositor handles layout, picture size, devices and sound. [The architecture page](docs/architecture.md)
and [session states](docs/session-states.md) have the detail.

## Development

Read [the testing guide](docs/testing.md) for requirements, commands and the
limits of each test. To build without deploying:

```bash
meson setup build compositor
meson compile -C build
```

The build uses `compositor/meson.build`. The wlroots it links is chosen by
`-Dwlroots`: `subproject` builds the 0.17.4 that the release bundles, with the
release's patches, from the Meson wrap in the compositor's `subprojects`
directory (the test runner builds this way when
the wrap is present and its archive is cached or can be fetched); `system` links the system's wlroots, which Meson accepts
from 0.17.1 up to (not including) 0.18, and Meson warns that it lacks the
release's patches; the default `auto` takes the subproject when its archive is
cached and Meson is 0.63 or later. The build is an optimized release build by default; add
`-Dbuildtype=debug` to the `meson setup` command for a debug build. A binary built
for one distribution must not be assumed compatible with another distribution's
libraries.

Run the local gate as an ordinary user:

```bash
./tests/source-check.sh
```

The gate includes source-contract checks, launcher tests, a compositor build
and real headless integration. Missing required checks produce an incomplete
result. Physical displays, controllers and games require separate evidence.

## Repository layout

- `compositor/`: wlroots compositor and built-in management overlay.
- `guest/`: authentication and per-user payload launchers.
- `packaging/steamos/`: the session's programs, the supervisors, the input and audio helpers and the panel.
- `packaging/release-build/`: the container build of a release and the bundle builder.
- `tools/`: the `dosplex` program (install, start, update, uninstall) and the release tools.
- `tests/`: local checks and historical hardware feedback scripts.
- `compatibility/`: `scenarios.yaml` defines the game scenarios and what counts as a
  pass, `accepted.lock` lists the accepted ones, and `game-compatibility.yaml` holds
  the configuration invariants and the coverage requirements. `docs/compatibility.md`
  is generated from them.
- `docs/testing.md`: repeatable local testing and hardware evidence requirements.
- `prototype/`: earlier prototypes.

When a document and the code disagree, the code is right.

## Support development

Dosplex is free and open source under the GNU Affero General Public License v3.0 or later. RoyÅke
also offers commercial licenses, integration and paid development; see [LICENSING.md](LICENSING.md).
If it's useful to you, you can help support development and hardware testing.

**[☕ Buy me a coffee](https://buymeacoffee.com/royake)**

Testing, bug reports and contributions are equally welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## People and license

Created and maintained by **RoyÅke (Roy-Åke Martinsson)** · [royake.com](https://royake.com)

Contact: <roy@dosplex.org>

See [AUTHORS.md](AUTHORS.md) for project credits and
[third-party notices](THIRD-PARTY-NOTICES.md) for upstream acknowledgements.

The code is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE) (AGPL-3.0-or-later).
RoyÅke also offers commercial licenses; [LICENSING.md](LICENSING.md) explains both. The documentation
(`docs/`, this README and the website text) is licensed under
[CC BY 4.0](LICENSE-DOCS.md); code samples inside the documentation stay under
AGPL-3.0-or-later. Contributions are accepted under the Developer Certificate of Origin and the
[contributor license agreement](CLA.md) (see [CONTRIBUTING.md](CONTRIBUTING.md)). Please follow the
[code of conduct](CODE_OF_CONDUCT.md); [MAINTAINERS.md](MAINTAINERS.md) says who decides.

Dosplex™ is a trademark of RoyÅke (Roy-Åke Martinsson). You may fork and
modify Dosplex freely, but a modified version must not use the name or logo as if
it were the official project. See [TRADEMARKS.md](TRADEMARKS.md).

Steam, SteamOS, Steam Deck and Steam Machine are trademarks or registered trademarks of Valve
Corporation. Dosplex is an independent project. It is not made, endorsed or supported by Valve.

Copyright © 2026 RoyÅke (Roy-Åke Martinsson).
