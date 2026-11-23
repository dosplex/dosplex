# Target platforms

## Product scope: SteamOS devices

DosPlex is tested on a Steam Deck and a Steam Machine. Other SteamOS devices have
not been tried.
Support is described in two tiers:

- **TV or monitor (core):** a Steam machine, or a Steam Deck docked to a TV or monitor, at
  1080p and 4K. This is where full acceptance testing happens.
- **Steam Deck built-in screen:** supported. Each player gets about 640×800, so many games
  will not fit well; compatibility is listed separately for this mode.

Device-family support must be backed by recorded validation rather than inferred from a
shared operating system.

## Current test bed: SteamOS PC connected to a TV

DosPlex is for two people sharing one TV: two game sessions for
couch co-op, or a game alongside movie playback in a separate session. Each session owns a screen region, controller input, audio output and
microphone selection. The same requirements apply when each player uses a
headset. Screen placement and audio-device selection are independent.

The active implementation is the C/wlroots compositor, a side supervisor and
nested Gamescope sessions. The older Python/Xephyr VM prototype is historical;
it is not the release architecture or the main acceptance path.

| Environment | Purpose | Current evidence |
| --- | --- | --- |
| Ordinary Linux user, headless wlroots/pixman | Fast isolated display/control tests | Local build and behavioral suites pass; see testing guide |
| Private PipeWire/Pulse and WirePlumber policy | Audio routing and signal separation | Local synthetic tests pass; no hardware is opened |
| SteamOS PC, AMD GPU, HDMI TV | Primary end-user acceptance | Tested with the installed release and a set of games |
| Steam Deck, docked | TV/monitor tier | Two-account sessions, display presets and native Deck controls tested; games are listed on the compatibility page |
| Steam Deck, built-in screen | Supported with limited game compatibility | Separate, reduced acceptance matrix |
| Generic Linux desktop/X11 | Development and portability | Backend exists; release support is not established |

## Display contract

The compositor uses one physical output. `DOSPLEX_OUTPUT` selects an exact
connector name, such as `HDMI-A-1`; with no selection, the first available
physical output is used. Additional outputs are ignored. This is not currently
a multi-monitor placement implementation. DRM connector selection and hotplug
still require hardware validation.

Each side exposes one virtual monitor to its assigned account. Its advertised
mode matches that side's screen region and its desktop origin is `(0,0)`.
Nested Gamescope separately presents Native, 1:1, 4:3 or 16:9 to the game. The
ratio choices use the largest exact ratio that fits the side region; Fit keeps
proportions and Stretch fills the region. Pending menu changes require Apply;
changing one player's game resolution must not change the other player. Steam's
per-game Game Resolution is a separate override and is left unchanged by DosPlex.

The existing automatic portrait-panel rotation is restricted to an eDP output
with an 800×1280 mode. External portrait displays must not inherit that rule.
Other panel orientations and user-selectable rotation are not validated.

## Required hardware coverage

The checks below apply to the TV-connected Steam machine release target.
Deck built-in-screen results are recorded separately from TV results. Do not
advertise every SteamOS device as verified on the strength of this one test bed.

- TV at 1920×1080 and supported 3840×2160 modes, both split orientations.
- Native, 1:1, 4:3 and 16:9 side resolutions, Keep ratio and Stretch.
- Steam Controller + DualSense with Steam Input, then DualShock 4 and two
  identical-model controllers. USB/Bluetooth and reconnect coverage are separate.
- TV output + headset, two headsets, independent microphones, mute and unplug.
- Game + movie playback, including local video and browser streaming, captions,
  fullscreen within the assigned region, seek/pause and independent volume.
- Game launch/exit, overlay during gameplay, side restart/crash, suspend/resume,
  display disconnect/reconnect, shutdown and complete rollback.

Do not infer support from a successful build, connected-device inventory or an
old game result. The compatibility scenarios record the setups and the evidence
each one needs. A hardware case stays pending until it has been executed with a
named build.

See [Testing](testing.md) for the local methodology and its limits.
