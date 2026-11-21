# Current limitations

This page lists what Dosplex 0.1.0 alpha does not do, or does not do well yet, in engineering detail. The
manual has the short form for users: [Limits of the alpha](manual/15-limits-of-the-alpha.md).

## Input ownership and controller support

The compositor routes connected keyboards/mice independently and now protects
Main's last menu control. Generic controller requests have stable identities,
atomic swaps and expiring previews. Native Deck/Steam Controller mediation and
per-user device views use acknowledged assignment. Automatic native discovery is
not complete.
Do not start both native and generic owners for one device.

The Steam Controller puck 28de:1305 is carried the same as the 1304 puck, including the firmware-update
guard. The older Steam Controller dongle 28de:1142 is not supported.

A controller with no saved owner belongs to Main, and a
controller that appears never changes the layout. `dosplex start` enrolls every other
slot of the receivers of Main's and Guest's controllers, awake or not, up to eight
slots in all; each starts as Main's. The user can release one (Unassigned) or give it
to Guest, and the owner is kept in `/var/lib/dosplex/controllers` across a stop and
a reboot. Each controller has a stable number that the Devices page shows. Which
interfaces of a real receiver are controller slots, and that a second pad reaches a
game, are not checked on the hardware. Deck plus two identical wireless
controllers are enumerated, with independent routes. Slot identity follows the
receiver interface; physical-controller identity after a reconnect into a different
slot is not established. A running game may need restarting after assignment:
Valheim did not reopen the added pad until restart, even after its stale mounts
were repaired. A layout moves no controller; in Full screen Main's menu chord
answers from every owned controller, Guest's included.

A Steam Controller's trackpad mouse and keyboard emulation reach the game
through that player's own Steam and nested Gamescope, so the two players'
emulated pointers and keyboards are independent. Each nested Gamescope now
draws its own cursor. Physical USB or Bluetooth keyboards and mice are a
separate path. Nested X11 merges host keyboard/mouse identities. The private uinput fixture
proves the compositor handlers and UI, not independent physical routing through
X11. Keyboard/mouse reconnect persistence, composite-device grouping, physical
hotplug during a move and broader controller families remain open. Swap
exchanges the control each player pressed last, on a backend both players have,
and asks for a press when it cannot tell. Guest claims and releases controllers
from its own menu; keyboards, mice and other input devices are still assigned by
Main, which can also share a mouse or keyboard between both sides (Shared). That
choice has been run in a nested X11 session, not with physical devices.

Only one Dosplex menu is shown at a time. Native overlay capture and immediate
handback have tests and limited human acceptance, but simultaneous physical
players, game input, optional gyro/pad/haptics features and controller sleep/wake
need broader coverage.

**Steam's "Steam Controller detected!" tour.** Steam shows this tour per account, and
it cannot finish inside a Dosplex side: Steam's firmware check fails there, so Steam
never saves that the tour is done. It comes back at every Steam start and every
controller wake, and while it is up it takes the input from games and installers.
Escape or B only hides it until the next start. Dosplex does not change Steam's files.
When a Steam Controller is routed to a side and the tour is on for one of the side
owner's accounts, `dosplex status` lists the account and the journal says so once.
Switch the tour off once per account, in that side's Steam: Settings → Developer →
Miscellaneous → Show Steam Controller Tour. Developer appears after Settings →
System → Enable Developer Mode; if it does not show in a side, do it in stock
SteamOS signed in as that account. The setting is stored with the account
(`ShowIbexWelcomeTour` in `userdata/<account>/7/remote/sharedconfig.vdf`) and Steam
Cloud carries it to other devices.

**Controller firmware updates.** Update the firmware of a Steam Controller and its
Puck receiver in stock SteamOS, not while Dosplex runs. Steam can offer an update
inside a side, but its firmware check fails there, so an update could be left half
done. The controller relay therefore refuses the command that starts one (the
reboot into the bootloader), for the receiver and for a pad behind it, so an
update cannot start inside a side. Decline the prompt. The refusal has been
tested with synthetic input only, not with a real controller. The Deck's built-in
controller is not covered.

## Sessions, rendering and applications

Two accounts, independent Steam sessions, desktop round trips, side resets,
Standby and Off have development evidence. Full screen offers Guest Standby
(in memory) or Off (closes the activity). Main can also give Guest the whole screen (Guest full screen): Main goes to Standby, and there is no Off for Main.
Guest currently needs a tested official Steam beta/launch-flag combination.

Layout, position, proportion and picture-choice changes no longer restart a
side. Steam's interface follows them by a live change of the game monitor, but a
game that is running keeps the monitor it started with, because not every game
survives a monitor that changes under it. Its picture is scaled into the new
area, the overlay says Scaled, and the game fills that area only after you change
its resolution in its own video settings or relaunch it. The monitor changes once
no game runs in that session. The live monitor change is verified with Steam's
interface and one native game, from before the monitor was held while a game
runs. Holding it has tests with stand-in X tools and no real game yet; the scaled
picture and the resizing of game windows to the monitor have not had their
hardware check. Each game still needs its own Native, 1:1, 4:3 and 16:9 and
proportion/stretch checks.

Fit is a third way to fill your area (Zoom inside Dosplex), drawn by the Dosplex compositor and not by
Gamescope: it crops the game's picture to the shape of your area and fills it,
so nothing is stretched and no bars are left. You pick it on the Display page, in
every layout, and Dosplex never picks it for you, because it can crop a game that
really uses its whole monitor. It is meant for a game that draws black bars inside
its monitor, such as a 16:9 picture letterboxed in a 1920x2160 monitor that has
become a full 3840x2160 screen. While a game keeps its monitor, your menu then says
"Fitted: game at 1920x2160. Keep ratio shows the whole picture;
relaunch the game to use the whole area." and, for Keep ratio or Stretch,
"Scaled: game at 1920x2160. Fit fills the screen if the game shows black
bars; relaunch the game to use the whole area." Fit hides the part of the picture outside the crop, so a game's edge-of-screen
controls can be cut. It has had a hardware check on one game only.
TV hotplug, suspend, latency, thermal load and long play sessions require hardware
acceptance. Sleep pauses both sessions first and wakes them after (the sleep guard,
see [testing](testing.md#sleep-and-the-sides)); that has tests with stand-in
programs and with a real service manager, and no run through a real suspend yet.
A controller or receiver that the machine enumerates again after a sleep comes back
as a new device node; whether the player gets the controller back without
**Reset my side** is not established.

A game that starts while Steam on its side is idle (the screensaver is up and the
account shows as Away) can stay behind Steam's window. When Steam is still in
front of a new game three seconds later, Dosplex sends one Escape key press to wake
it. That needs `xdotool` in the player's session, which SteamOS ships; without it
nothing is sent and the log says so. One Escape wakes an idle Steam on the Deck;
Dosplex's own sending of it has tests with stand-in X tools and no hardware run yet.

Alien Breed Impact's fixed window renders without forced resizing at 1080p with
proportions preserved. Quake's portrait-mode enumeration
failure has a scoped app2310 experiment: two native exclusive launches pass at
1920×2160. A general portrait policy journals the Wine setting per prefix, applies it before Steam starts, and
repairs first-run prefixes after Wine exits. The Deck's disposable Proton
11.0-100 probe reproduced the transposition at 1920×2160 and confirmed that
`EmulateModelist=Y` exposes the native mode. It changed no installed prefix.
Fixtures cover multiple libraries, crash recovery, LF/CRLF registries,
active-prefix deferral, fail-closed process inspection and user edits; the
compositor carries a brief first-run notice. In a Deck trial the
supervisor applied the setting to every prefix across two libraries before
Steam started, repaired a prefix created during the session after its process
exited, showed the notice, and reverted every value on stop. Real-game checks
with the policy live remain open, so portrait Native compatibility is not yet
guaranteed. The policy does not force 16:9 or change
Steam's per-game Game Resolution. Top/bottom may be more compatible for some
titles; record the result per game and layout. Deck-panel 640×800 split is
experimental, outside MVP acceptance.

Game+movie, two simultaneous different games, multiplayer, cloud/save isolation
and concurrent load are not certified by menu or launch tests.

Gamescope before 3.16.30 (the Steam Machine's 3.16.23.6) hides its nested window
by attaching an empty buffer whenever it has nothing to show, and attaches a buffer
again without the configure that xdg-shell asks for after that. Stock wlroots
disconnects such a client, and Gamescope aborts; a game installer that closes one
dialog and opens the next showed it on the Machine, where a side's Gamescope
stopped twice. The compositor now takes a buffer from a toplevel that has mapped
before and is unmapped as the remap, and logs that it did. This has tests with a
probe client and has not been run with the Machine's Gamescope or on the Machine
yet. Gamescope 3.16.30, the Deck's, does not do it.

KDE's own display scale setting (System Settings, Display) has no effect in a side's
nested Desktop mode: Dosplex sets the scale when the desktop starts, and the
`desktop-scale` file described in the user guide is the way to change it.

**Storage.** In Main's side, Main's own removable drives are writable, as on stock
SteamOS: the SD card and other removable drives that SteamOS mounts for Main's
account under `/run/media/deck`. The side's sandbox leaves them writable; that is
checked with stand-in drives, and a game run from a real SD card is not checked
yet. A drive that is mounted anywhere else before a side starts, for example by
`/etc/fstab` at `/mnt`, is read-only in that side. Steam cannot write to a library
on it, and a game on it can fail at its first start; keep such a library in Main's
home folder or on a drive that SteamOS mounts. A drive mounted anywhere else after a
side has started keeps the settings it was mounted with, in both sides, until that
side restarts, and only its file permissions decide who can use it. Guest cannot
reach Main's drives under `/run/media/deck`. A side that was already running when
Dosplex was updated keeps its earlier view until its next restart. Each account keeps its own
game library unless you turn on the experimental shared library (see [Experimental features](manual/17-experimental.md)).

## Audio and trust

Claimed outputs and the shared-speaker settings are kept in `/var/lib/dosplex/audio.json`
and survive restarts and reboots; microphone claims are not kept. An unreadable file is
renamed to `audio.json.corrupt-<time>` and Dosplex starts from the defaults. The
stable device identity (so a renamed device keeps its claim) is not done yet: the claim
is the device's node name. Guest has no default output on a first run.

Output/microphone claim, release, mute, new-stream routing and synthetic signal
separation are tested. Main Crimsonland audio has a human pass. Physical HDMI and
headset pairs, two microphones, voice chat, unplug fallback and Guest-game handoff
remain incomplete. Guest's Steam is offered one output and one microphone through
its own endpoint, which adds about 60 ms of latency to Guest's audio; its isolation
is tested with synthetic audio only. The shared server's local TCP listener and
Main's use of the shared Pulse transport are not a hostile-client boundary.

Full mix and Positioned stereo mix shared output modes pass four-channel signal checks through
both Steam namespaces and the installed overlay. Positioned stereo mix changes stereo
balance according to screen position; it does not create private sound zones.
Physical listening remains separate acceptance. Startup now waits for the
enrolled Unix audio endpoint so Steam containers do not lose game audio during
a full Dosplex update.

Separate Linux users and Wayland seat/output filtering do not make this a security
sandbox. Privileged authentication, helper write paths, admission policy, partial
failure rollback and hostile-client audio/input access need a focused review.
Do not capture passwords, QR tokens, microphone audio or raw physical input in
routine diagnostics.

## Security and isolation

Dosplex keeps the two players apart in ordinary use: each has their own Unix account, Steam, games, saves,
controllers and sound. It is not yet a wall against a player who sets out to cross it. The alpha is
published with the gaps below stated, not fixed. Run it only with people you trust at the same screen.

- **A window over the other player's part of the screen.** The compositor does not clip a side's windows to
  that side's part of the screen. A program run from one player's account can place a window over the other
  player's part and take that player's pointer. The compositor offers each player's input seat only to that
  player's account, so such a window does not get the other player's key presses.
- **An old menu program is still accepted without a check of who runs it.** The compositor's Wayland socket
  can be reached from both players' accounts, and it still accepts the application id of an earlier
  menu program, `dosplex-hud`, from either. In a test, a program run as the Guest account had such a
  window accepted. The effect is the one above: a window over the other player's part.
- **The two sides share one network.** Both players' programs run in the machine's one network namespace. A
  program on one side can reach a network service that a program on the other side opens on the machine
  itself (`127.0.0.1`).

The shared game library and the start at boot are off unless you turn them on, and are experimental. The
library has known open defects in code that runs as root; [Experimental features](manual/17-experimental.md)
lists them. Do not turn the library on where an account on the machine is not trusted.

The repository also holds systemd units of an earlier, never released way to run Dosplex as a system service
(`packaging/steamos/usr/lib/systemd/system/`). A release does not install them. One of them creates a runtime
directory that every account can write to; do not install these units by hand.

## Performance overlay

The Performance Overlay and the CSV logs of each side (see the user guide) have
only been checked with stand-in programs so far, not on a Steam Deck or a Steam
Machine. Known limits:

- **A log needs the overlay on.** Gamescope 3.16.30 starts mangoapp only while
  the overlay is shown, and stops it ten seconds after you set level 0. No
  mangoapp, no log.
- **File names and one file per run.** MangoHud names the CSV files and decides
  when a file ends. Logging starts when mangoapp starts, so one run may give one
  file for several games, not one per game. Dosplex does not rename the files.
- **Which columns exist** (power, temperatures) depends on what MangoHud reads
  on the machine.
- **If mangoapp stops, it is not restarted** by Dosplex. Gamescope starts it
  again only on 3.16.30 and later, when the overlay is shown after a hide.
- **The queue that carries the frame data is separated by directory, not by
  namespace.** The two sides and SteamOS itself would otherwise share one System V
  message queue. Each side uses its own directory for the key. Two sides collide
  only if the inode numbers of their `mangoapp` files agree in the low 16 bits,
  which does not happen on a fresh machine but is not checked.
- **MANGOHUD_CONFIG reaches Steam and its games** when logging is on, because
  Gamescope hands its environment to everything it starts.

## Dosplex's own FPS counter

The counter and the CSV recording in Dosplex's menu (see the user guide) have
only been checked with test programs so far, not on a Steam Deck or a Steam
Machine. What the number means:

- **It counts frames the side's Gamescope delivers to Dosplex.** Each commit
  with a new buffer on the side's window is one frame, timed when Dosplex
  handles it. That is the picture the player sees. It is not the game's own
  frame rate. Gamescope repeats a frame when the game is slower than Gamescope's
  output, and drops frames when the game is faster. A game that renders 30 FPS
  inside a Gamescope that outputs 60 shows 60 here, and the other way round.
  Read the number as "what reaches the screen", and use the game's own counter
  for what the game renders.
- **A still picture reads low.** Gamescope may not commit when nothing changes.
  The counter then shows 0 or a low number, and the first frame after a pause
  carries the pause in its frame time (and in `frametime_ms_p99` of that second).
- **It is the time Dosplex saw the commit, not the time the frame reached the
  glass.** It leaves out Dosplex's own composition and the display's refresh. If
  Dosplex itself runs late, commits are handled late, and the numbers show it
  only as bunched frames.
- **Windows of the numbers.** FPS and the average frame time cover the last
  second. The p99 on screen covers the last five seconds, and the p99 in the CSV
  the second of its row. With 60 frames in a second, that p99 is the
  second-slowest frame. Above about 400 FPS the five-second window holds fewer
  than five seconds of frames.
- **Desktop mode is not counted well.** A Desktop session's nested KWin draws in
  sub-windows of its window, and only the main window's commits are counted. The
  number there is lower than the real rate and may be near zero.
- **Host sensors differ by machine.** The columns come from the AMD GPU driver,
  the CPU temperature driver and the battery in `/sys`. A sensor the machine
  does not have is left empty. A Steam Machine has no battery. The power on a
  Deck is the chip's average, not the whole device.
- **Only root writes the files.** They go to `/var/lib/dosplex/perf/`, so a
  side's account cannot write there. Dosplex keeps the newest 20 files and
  deletes older ones without asking.
- **Steam's own overlay is not replaced.** Steam's Performance panel in a side
  does not work yet; see the section above.

## Distribution and recovery

- **One way to install.** A release is a signed archive, installed with `dosplex install --release`. There is
  no package for a package manager, and Dosplex is not in any store.
- **Tested versions only.** A release lists the SteamOS, Gamescope and Steam client versions it was tested
  on, and the installer refuses others unless told to go on. A SteamOS update can therefore make Dosplex
  refuse to start until a release has been tested on it.
- **One earlier release is kept.** `dosplex rollback` returns to it. Older ones are removed.
- **Recovery is a reboot.** Dosplex starts only when asked, so a restart of the machine brings stock SteamOS
  back whatever state a session was in. `dosplex repair` finishes or undoes an install, update or uninstall
  that was interrupted.
- **Devices.** The alpha was tested on a Steam Deck and on a Steam Machine. Other SteamOS devices have not
  been tried.
