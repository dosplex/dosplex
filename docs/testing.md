# Testing Dosplex

Dosplex requires both isolated development tests and tests on the supported
hardware. A successful build or a source-contract check does not establish game
compatibility. Keep scenarios that nobody has played marked pending.

## Local tests

One runner, `tests/run.py`, finds the tests, runs them by tier, prints a summary and exits non-zero for any
failure. It uses only the Python standard library and builds the compositor from the canonical Meson project
once, for the tests that need it (`--build-dir DIR` keeps that build and `--compositor BINARY` uses one that
you built).

```bash
python3 tests/run.py --tier fast               # lint and unit tests, in parallel: about 15 seconds
python3 tests/run.py --tier fast,integration   # the portable gate, a few minutes
python3 tests/run.py --list                    # every test with its tier, needs and timeout
python3 tests/run.py headless-integration      # one test, named by its file name, a path or a glob
python3 tests/run.py --help                    # the options, tiers, needs and statuses
```

`./tests/source-check.sh` is a wrapper around the second command for the published tests. Extra arguments
go to the runner.

### Test tiers

The tiers describe what a test needs. They have nothing to do with the T1/T2 support tiers (TV or monitor,
handheld screen) that the compatibility pages use.

| Tier | Name | What it holds | `--tier` |
| --- | --- | --- | --- |
| 0 | lint | Static checks and source-contract checks. They guard against removed safety lines and are never evidence that a feature works | `fast` |
| 1 | unit | Hermetic behaviour tests: stub executables, temporary directories, private sockets | `fast` |
| 2 | integration | The real compositor, Xvfb, a private PipeWire or transient user services, each in private temporary directories | `integration` |
| 3 | kernel-synthetic | Virtual devices on this machine's kernel (uinput, CUSE). They need root or special rights | `kernel-synthetic` |
| 4 | hardware-synthetic | A Deck or a Steam Machine driven from here. `visual` is the part that checks captured frames | `hardware-synthetic`, `visual` |
| 5 | human | Acceptance by a person | `human` |

`--tier` also takes numbers (`2`), lists (`0,1`) and ranges (`0-2`). Tiers 0 and 1 run in parallel, every other
tier one test at a time. A pass in one tier never stands for another: the integration tests do not show that
a game works, and nothing here replaces the hardware acceptance below.

### How a test registers itself

A file named `*-test.py`, `*-integration.py`, `*-test.sh` or `*-integration.sh` in `tests/` is a test, and it
declares itself with a comment near the top:

```python
# dosplex-test: tier=unit needs=cc timeout=60
```

`needs` lists what the test cannot run without (a compiler, `setfacl`, PipeWire, user services, the built
compositor, and so on; `python3 tests/run.py --help` has the list). `# dosplex-run: LABEL ARGS` lines run a
test more than once or with arguments, and `{compositor}` stands for the compositor that the runner built.
`manual="why"` keeps a test out of every tier, so that it only runs when named together with
`--include-manual`. `exclude="why"` marks a script that only looks like a test. A test that skips something
itself prints a line that starts with `SKIP:` or uses unittest skips, and declares `skips-ok="why"` if that
is expected.

Choose the unit tier only for a test that finishes in a few seconds. A hermetic test that needs much longer
belongs in the integration tier, so that the fast tier, which the commit hook also runs, stays quick.

A test file without a valid declaration fails every run, and `tests/registry-test.py` fails for it too, so a
new test cannot be left out of the gate by accident. `python3 tests/run.py --list-unregistered` names the
files.

### What the runner guarantees

- A test whose needs are not met is **INCOMPLETE**, not skipped silently, and an INCOMPLETE test fails the
  run. The same goes for a test that reports a skip of its own. A tool that is missing can never look like a
  pass.
- Each test runs in a process group of its own, with no stdin, in an empty working directory, with a private
  `TMPDIR`, without `DOSPLEX_*` settings and without the variables that point git at a repository. After the
  test ends, whatever it left running is killed. A test that outlives its timeout is asked to stop, together with
  everything it started, killed if it does not, and fails.
- The runner refuses to start as root (`--allow-root` overrides that), and a test that needs root is manual.
  Nothing runs as root unless you start the runner as root and name that test.
- In a development checkout every run writes a JSON record (UTC time, git revision, a hash of the uncommitted
  changes, tool versions, and the result and duration of every test) to the maintainers' evidence directory.
  `--evidence-dir DIR` writes it elsewhere and `--no-evidence` turns it off. The record does not go to
  `/tmp` unless you ask for it; only the tests' own scratch files go there, and they are removed after the run.

Build requirements include a C compiler, Meson (0.63 or later for the wlroots subproject), Ninja, pkg-config,
wayland-scanner, Wayland protocols, Wayland client/server libraries,
xkbcommon, libxcb, libinput, Cairo and json-c >=0.15 (Debian/Ubuntu: `libjson-c-dev`;
Arch/SteamOS build hosts: `json-c`), and a wlroots. The tree carries a Meson wrap of the
wlroots 0.17.4 archive the release bundles, with the release's patches, in the compositor's `subprojects`
directory; the test runner then builds the compositor with `-Dwlroots=subproject` against it, which also
needs the libraries wlroots builds against (pixman, libdrm, GBM, EGL, GLESv2, Vulkan, libseat, xcb-util-wm,
xcb-util-renderutil, hwdata). The archive has to be in the wrap's package cache (the `packagecache`
folder next to the wrap, ignored by git): the runner fetches it once with
`meson subprojects download wlroots`, and without network you can copy it there from another checkout. When it
cannot be had, the runner builds against the system wlroots and says so in its build line. The system wlroots that Meson accepts
is 0.17.1 up to (not including) 0.18; a plain `meson setup` uses the archive when it is cached and the
system library otherwise, with a warning. That library
lacks the patches: under its X11 backend presentation feedback is answered one host commit late. The compositor
detects that from the first presents and then commits every host frame (`"frame_pacing":
"every-frame-late-presents"` in `state.json`) instead of only on change (`"on-demand"`), and
`tests/x11-presentation-test.py` fails on it. The build is an optimized release build by default
(`buildtype=release`, asserts off); pass `-Dbuildtype=debug` to `meson setup` for a debug
build. The headless test additionally needs
`wayland-info`, a C compiler and Wayland client development files to build the
small test window in its temporary directory. The nested X11 test needs Xvfb, xdotool, xwininfo and xwd. The compatibility checks need Python with PyYAML. Offline sudo-policy tests
require `visudo` and `cvtsudoers` from the sudo package.

The portable gate is `./tests/source-check.sh`. It builds the compositor from
`compositor/meson.build`; protocol headers come from installed Wayland protocols.
It runs the published behavioral tests of the fast and integration tiers without
deployment, and it ends with `PASS`, `FAIL` or `INCOMPLETE`.

The snapshot has to be self-contained. `tools/check-publication.py` fails when a
published file imports, runs, sources or links to something that is not published:
scripts and modules, systemd units, repository paths and relative Markdown links.
A reference to something deliberately left out needs an entry with a reason in
`tools/publication-waivers.json`. `python3 tests/publication-closure-test.py` checks
this rule on small fixture trees, and the maintainers run `./tests/source-check.sh`
inside the staged snapshot before it is published.

### Authentication transport and failure checks

`python3 tests/auth-transport-test.py` compiles the actual compositor auth-call
function into a small harness with logging/types stubbed. It invokes disposable
helper and sudo spies, verifies exact password bytes on stdin and no password in
arguments, checks login/auto-login/logout argv and failure propagation, and tests
the real Python CLI parser. No real PAM authentication, root helper, account or
system state is used. The real helper now accepts `login SIDE USER` with a bounded
UTF-8 password on stdin until EOF; trailing whitespace is preserved and NUL,
invalid UTF-8 and oversized input are rejected. A password argument is rejected.

These checks do not validate PAM policy, account/session authorization, runtime
permissions or privileged sudo/PAM cancellation. The C harness also checks a
silent hang, closed-output hang, excessive output, signal exit, missing helper,
verbose successful output and a forked child retaining the output pipe. A test
build uses a 700 ms deadline; production uses 30 seconds and a 64 KiB combined
output limit. Output is discarded without logging, and failure kills the owned
process group before reaping the helper. These tests never inject real passwords.

Authentication now runs as independent per-side jobs polled by Wayland timers.
The real-event-loop harness checks heartbeat progress during hung helpers, a
right-side completion before a delayed left side, duplicate rejection and scoped
shutdown cancellation. A separate actual-C completion harness checks failed
logout preserves ownership, successful login uses its captured username and
successful logout clears only its side. UI hooks/session launch are stubbed there.
The deadline does not provide transaction rollback: helper state changes
made before cancellation need a separate recovery policy. Cancellation through
real privileged sudo/PAM and uninterruptible kernel waits remains unverified.

### Authentication state publication

`python3 tests/auth-state-test.py` exercises the real helper's state functions
inside a temporary directory. It checks complete old/new reads at replacement,
write/replace failures, malformed input, other-side preservation and a writer
killed immediately before publication. Malformed state blocks login before PAM
or ACL changes. Missing state remains the unclaimed case.

The helper writes a same-directory temporary file, flushes and fsyncs its contents,
then replaces `session.json`. Ordinary failures remove the temporary file. SIGKILL
can leave an unreferenced `.auth-state-*` file; it is never read as session state.
The test fixture removes its entire private directory afterward. This does not
prove power-loss durability, secure directory ownership, concurrent claim
serialization or rollback of ACL changes made before a failed state write.

### Compositor options

The backend (`-b headless|x11|drm`) only chooses how the display is built. Every
other behaviour is an explicit option that is off until the launcher passes it;
`dosplex-compositor --help` lists them.

| Option | Effect | Default |
| --- | --- | --- |
| `-s` | Real side sessions: sign-in, session-state polling, auto-login. Needs root. Without it the two sides are placeholders. | off |
| `--test-hooks` | The `DEV_FRAME`, `DEV_OUTPUT_*`, `DEV_SIDE_SCALE`, `DEV_SIDE_DISPLAY`, `DEV_STATS`, `DEV_SCENE` and `CHORD` control commands, the screen-copy protocol, and `DOSPLEX_POWER_COMMAND` (a program the power helper runs instead of `systemctl`). The screen-copy protocol is never offered together with `-s`. | off |
| `--allow-power` | Sleep, Restart and Shut down act, through `$DOSPLEX_ROOT/libexec/dosplex-power-action`. Restart and Shut down first ask each side's Steam to quit (`steam -shutdown`, up to 45 s per side), then stop both sides and Dosplex; without the helper they do nothing and the menu says so. Otherwise the menu says power is not available. | off |
| `--boots-into=steamos\|dosplex` | What the machine boots into after Restart or Shut down, which the confirmations say: `steamos` (normal SteamOS; a retained trial and the installed `dosplex start` pass it, nothing starts Dosplex at boot) or `dosplex` (the display-manager installation, `start-dosplex.sh`, passes it). Without it the confirmations say nothing about the next boot. | unsaid |
| `--idle-policy=none\|dim` | `dim` switches the output off after 5 minutes without input (`DOSPLEX_IDLE_DIM_TIMEOUT_MS` changes the time) and lowers the panel backlight where the machine has one. | `none` |
| `--host-keyboards` | Keyboards that are neither libinput nor X11 devices count as menu controls. | off |
| `--audio-router=PATH` | The audio helper. `DOSPLEX_AUDIO_ROUTER` sets it too; the option wins and an empty value turns audio off. Without a helper, audio is unavailable. | none |

A control command that needs `--test-hooks` is logged and ignored without it, so a
tool that lost the flag fails with a message instead of waiting for a token.

An installed release starts its session without `--test-hooks`. To run a device check that needs
them, create the engineering marker as root before the start:

```bash
sudo touch /var/lib/dosplex/test-hooks    # root's file, not writable by others
sudo dosplex start                        # says that test hooks are on for this session
sudo rm /var/lib/dosplex/test-hooks       # the next start is a normal one again
```

`dosplex status` shows the marker while it exists. A release never creates it. With it, the
session's compositor gets `--test-hooks`, the stand-in for `systemctl` and the relays' synthetic
controls; all of them answer only to root.

### What the tests prove

| Test | Evidence | Limit |
| --- | --- | --- |
| Session launch | Executes the real session launcher with stub executables; checks independent game/output dimensions, Fit/Stretch and rejected values | Does not run Steam or render a game |
| Display state | Validates committed launch configuration and rejects corrupt state; pending selections cannot change running settings; the per-side monitor keys that the compositor publishes (`monitor_width`, `monitor_height`, `display_scaled`, `display_result`) are not part of the contract, whatever they hold | Does not run the privileged supervisor |
| Display follower (`display-follower-test.py`) | Runs the real follower against a fake supervisor socket and a fake `xprop`, `xrandr` and `xdotool` that behave like the nested Xwayland: one apply for each revision (mode request, wait for Gamescope, scaler), a report of `applied` or `failed`, the window fit that follows the focused window (a one-shot inside one poll: the property goes on, the window is looked at for at most 250 ms, and the property goes off again, also when a read or a write fails and when the follower is stopped), the control socket's failures and the end with the display. While a game runs (an app other than Steam's 769 in `GAMESCOPE_FOCUSABLE_APPS`) it sets no mode request, still sets the scaler, reports `deferred` with the size the monitor keeps once for each revision and set of games, applies the latest revision and reports it `applied` when the game has ended, and applies at once a target that is the screen's size; Steam's interface alone, a missing property and a malformed one are no game, and a property that cannot be read holds the revision. It wakes an idle Steam with one `xdotool key Escape` when `GAMESCOPE_FOCUSED_APP` stays 769 for three seconds after a game appeared, once for each app while it stays in the list, and not for a game that takes the focus, that ran before the follower started or that leaves; a missing `xdotool` is logged once. The three seconds run on a stand-in clock, and on the real one in a single process test. One test runs the follower against the supervisor's own control socket: the supervisor accepts the `deferred` report, keeps it in `session.json` as `display_applied_revision`, `display_applied_size`, `display_result` and `display_detail`, and the `applied` report of the same revision replaces it | Does not run Gamescope, Xwayland, Steam or a game: it does not show that Gamescope scales the picture of a game that keeps its monitor, that a game copes with that, or that the Escape wakes a real Steam (the hardware check) |
| Display contract (`session-display-contract-test.py`) | Runs the real `supervise()` loop with fake sessions: a version-2 state file restarts a side for no display change and a version-1 file for each committed one, as before; the supervisor serves the live contract and keeps the follower's report (`applied`, `failed` or `deferred`) in `session.json` through its own writes; a later report for the same revision replaces a deferred one, and a deferred revision restarts nothing under either version; with a Steam Controller routed to the side and the account's controller tour on (`ShowIbexWelcomeTour` not `"0"` in `sharedconfig.vdf`), `session.json` lists the accounts and the journal says once how to switch it off, and the file is never written | Does not run systemd, Steam or the follower |
| Session poll and display report | Compiles the compositor's actual session-state poller (`session-poll-test.py`) against disposable session files. For the supervisor's display report: `deferred` is a result next to `applied` and `failed`; `display_applied_size` is two integers from 1 to 16384 and is left out otherwise, without touching the rest of the report; a report for the same revision that says applied after a deferral; when a side counts as scaled (the report on the current revision says deferred, or the monitor in effect is not the effective game size; never for a Desktop payload, and not without a report); the Display page's line ("Applying…" until the revision is reported, the failure detail, and "Scaled: game at WxH. Change its resolution in the game or relaunch to fill the area." for a deferred report or a monitor of another size, never cut); the overlay's header line ending in Native or Scaled (game at WxH) and the third word of the split labels, for a side with an app surface only; and the notice, which the first deferred report for a revision raises once for each side (not again for the same report, a changed detail or after it was shown; again for the next revision, a new supervisor instance or a login after a logout; withdrawn when the monitor is applied before anyone saw it; never over the supervisor's own notice) | Does not draw anything: the strings are checked, not pixels, and the labels are drawn below the app surfaces, so a client that fills its side covers them. Does not run a supervisor: the files are the test user's, where the compositor wants root's |
| Picture choices | Compiles the compositor's actual picture-choice code; checks the largest exact-ratio size of 1:1 (the largest square), 4:3 and 16:9 for side by side, top/bottom, full screen and Deck regions, the fill order of the ratio buttons with ties kept as 16:9, 4:3, 1:1, their labels, the `game_aspect` names, that published overlay action codes do not move, and the default choice of each layout (Native in full screen, 16:9 in either split) with the monitor it gives: the same 1920x1080 in both splits of a 4K output | Does not draw or click the overlay; the headless integration test drives the buttons and the layout changes |
| Desktop scale | Runs the real compositor with a probe client that behaves like the nested KWin (fractional-scale request, wl_output and xdg_output); a Desktop side receives the preferred scale before its first configure, the wl_output scale and the region divided by the scale as logical size and toplevel size, re-sent on a layout change and when the scale changes; the follow-up configure goes to a client that got the scale only after the configures of its map (`--late-fractional`) and takes it to scale 2, and not to one that applied the scale already; a side without a scale, and the other side, stay at 1; the supervisor, launcher and session.json parts have their own unit tests | Does not run KWin, render a frame at the larger scale or use the state file the real compositor reads (that file must be root's) |
| Headless integration | Runs the real compositor with software rendering; queries each assigned client’s sole seat and virtual output, mode and zero desktop origin; exercises display Apply/Cancel, the supervisor's report fed through `DEV_SIDE_DISPLAY` (`state.json` carries `monitor_width`, `monitor_height`, `display_scaled` and `display_result` for each side; a deferred report for the current revision is scaled, any other report is decided by the monitor size against the size asked for; a layout change leaves the old monitor scaled until the new revision is reported; a command that is not well formed changes nothing), the default picture of a side that has made no choice (Native in full screen, the largest 16:9 in either split, one revision step when the monitor changes size and none between the splits, whose 16:9 is the same size), that a choice the player applied (Native included) is kept in every layout, that choosing what a side already has changes nothing, that a selection not yet applied does not move with the layout and Cancel returns to the default, that a toplevel which attaches a buffer again after an unmap without a new configure (what Gamescope before 3.16.30 does) is mapped and not disconnected, pointer and keyboard controls, owner-scoped Escape, atomic state and output reconnection with a mapped client | Does not validate DRM, controllers or two-user isolation |
| Scene damage | Runs the real compositor with software rendering and the probe client as a side's application; reports the scene counters through `DEV_STATS` (frames, overlay rebuilds, output pixels damaged, commit time, the visible area of the overlay's background). Checks that a stream of client commits rebuilds the overlay 0 times and damages only the client's block (128x128 output pixels plus a pixel of filter bleed at Desktop scale 2, 64x64 at scale 1), that an opaque side at scale 2 hides the background under it in full screen and in a split, that the commit handling returns to the scene at scale 1, and that an unmap and a remap still rebuild the overlay | Software rendering only; the X11 backend's presentation is not measured |
| Audio integration | Runs private PipeWire/Pulse servers and WirePlumber policy; checks output/microphone ownership, new streams, disconnect/reconnect and measured separation of synthetic tones; starts Guest's restricted endpoint and checks that it offers one output and one microphone, refuses shared defaults, volumes and modules, and plays and records only Guest's buses (the shared server fails the same check), then repeats the ownership, privilege and access checks under a root router in a user namespace where one is available. The re-created output case runs the real monitor loop with a null sink as the HDMI output: the sink is removed and created again under its name (a new index) between two passes, the loopback is still loaded with the same arguments and is linked to nothing, and the case checks that the tone from Main's bus does not reach the new sink before the monitor's next pass and does within it (never later than the third), that an output that is gone for a pass takes its loopback with it and gets one again, that a loopback that works is neither corked nor muted, idle or playing, and that a stream that stays muted for more than five seconds is unmuted in place; it also reads the journal: one line for each loopback loaded or unloaded (with the reason), sink that came, went or was created again and stream moved, and none for a pass that changed nothing | Does not validate physical HDMI (a null sink is not a card whose profile, port and handshake the TV drives), USB/Bluetooth profiles, game voice chat, Guest's latency on a Deck or hostile-client isolation (the shared server's TCP listener remains). Nothing here produces a corked loopback stream, so what the monitor does for one (report it after five seconds and leave it) is checked against a fake server only |
| Audio loopback liveness (`audio-loopback-liveness-test.py`) | Runs the router's loopback pass against a fake Pulse server (the JSON of sinks, sources, sink inputs and source outputs, the module list and the commands that change them): a loopback is kept only while its capture stream is on the live index of the source and its playback stream on the live index of the sink (not judged by name), is loaded again when a stream is missing or on nothing or on another node (a sink created again under its name, a bus, a microphone) and is left alone for 30 seconds when it will not link; the reasons it says (new route, arguments changed, stream missing, stream detached, route released, side in standby), the lines for devices that come and go, buses created and streams moved, a stream muted for more than five seconds (unmuted, once a minute at most) or corked (reported once), and that a failure that goes on is said once a minute and its end once; text from the server cannot add a line to the journal | Does not run PipeWire or WirePlumber: the fake server links streams the way the real one was seen to in `audio-integration.py`, which is where that is shown |
| Runtime flags | Starts the real compositor with and without each option: `DEV_FRAME`, `DEV_OUTPUT_*`, `DEV_SIDE_SCALE`, `DEV_SIDE_DISPLAY` and the screen-copy protocol need `--test-hooks`; the Sleep confirmation can continue only with `--allow-power` (a recording stand-in for `systemctl`); with `--test-hooks` Sleep and Restart run `DOSPLEX_POWER_COMMAND` through the power helper, Restart leaves through `systemd-run` and sets `power_action` in `state.json` (the progress panel itself is not captured), and without `--test-hooks` the variable is ignored; while `power_action` is set the overlay does not open and a real client on Main's side gets no key, pointer, scroll or relative motion; when nothing stopped the compositor the panel comes down at its deadline (`DOSPLEX_POWER_PANEL_TIMEOUT_MS`, test hooks only), and a first stage that fails takes it down with "Shut down failed", also when it fails only some seconds after Continue (the helper waits for its unit), and then Main's overlay is open again on the Session tab with the message; a probe on Guest's side that holds a key while Main continues Restart gets a keyboard leave as the panel goes up (the key ends there, the release is dropped under the panel), gets the focus back at once when the panel comes down, and no key is replayed; a probe on Main's side gets its focus back when Main closes the reopened overlay; `--boots-into` is read and a bad value is refused; Restart and Shut down never run without the helper, and a helper that is a symlink, not executable, or writable by group or others is not run (its owner is not checked: that needs root); Guest's Session page has no power buttons; `--idle-policy=none` never blanks the output and `dim` does and restores it on input; audio exists only with a configured router, the option beats `DOSPLEX_AUDIO_ROUTER` and an empty value turns audio off; bad arguments end the process before anything starts. `nested-session-mode-test.py` adds that `-s` needs root and removes the screen-copy protocol even with `--test-hooks` | Does not exercise `--host-keyboards` (stock inputs cannot create such a keyboard), a real backlight, or a real power action |
| Power helper (`power-action-test.py`) | Runs `dosplex-power-action` from a fake trial with stand-ins for `systemd-run` (which, as with `--wait`, ends with the exit status of the copy it runs), `systemctl`, `nsenter`, `runuser`, `steam`, the side supervisor and `dosplex`, and a fake process table for each side's Steam: Restart and Shut down leave through `systemd-run --wait`, ask both sides' Steam to quit at the same time (the request runs as the side's user in the namespaces of its Steam process with its HOME and runtime variables and nothing else, a side on standby is thawed first, and no Steam is asked when none runs in the side's unit), wait at most the Steam timeout for each, hand a side to its supervisor for the stop as soon as its Steam is gone (not after the other side's), stop Dosplex (or recover the trial when the installed state names another; when that says a recovery already runs, wait up to the bound for the trial directory to disappear before the action), then run the action, also when the stop failed; the side directory falls back to the supervisor's `/var/lib/dosplex/sides`; Sleep stops nothing; the trial's own power command (inside the trial directory) still runs after the stop deleted the trial, and one that vanished before the escape refuses the action instead of running `systemctl`; an untrusted power command is refused before anything stops | Does not run real systemd units, supervisors, a real Steam client (that `steam -shutdown` reaches the client of a side through its pipe and starts no other is for a device to show), `dosplex stop`, suspend, reboot or poweroff |
| Power helper, real process table (`power-action-process-test.py`) | Runs the helper's discovery and wait for a side's Steam against `/proc` and a cgroup as the kernel writes them: a real process called `steam` (a link to `sleep`) in a real transient unit of the user's service manager that is named like a side's session unit is found by its name, its user and its unit's cgroup and by nothing else, the variables a request takes along are read from its real environment, a request that stops the unit ends the wait with "Steam exited" and the process gone, and one that does nothing ends it at the timeout with the process still there | Entering a namespace needs root, so `nsenter` and `runuser` are pass-through stand-ins; that a request reaches a client in a side's namespaces is for a device to show |
| Overlay text layout (`overlay-text-test.py`) | Compiles the compositor's own text layout (word wrap, shrink to fit, ellipsis) and its power confirmation texts and runs them with real Cairo and the machine's Sans font, measuring every line again with the scaled font's extents: no line is wider than its box less the padding (but a single character), a word wider than a line is broken between characters, nothing is lost or reordered, a line ends early only because the next word did not fit, and the boxes of the overlay's messages (three lines for a confirmation, two for the power failure message) hold every message at every panel width from 240 to 720 logical pixels at 1x to 3x, showing all of the text at every real layout (a panel of 376 or more); Restart and Shut down say normal SteamOS and `dosplex start`, Dosplex, or nothing, as `--boots-into` says | Does not draw: the pixels are `overlay-text-render-integration.py`'s; it uses the machine's own Sans, so the wrapped lines differ between fonts while the checks do not |
| Overlay text drawn (`overlay-text-render-integration.py`) | Runs the real compositor on the X11 backend in a private Xvfb, opens Main's menu on the Session page, asks for Restart and captures the frame (xwd) after it was drawn, Side by side at 1280 and at 1000 pixels wide and Top / bottom: no light pixel in the last six of the message box (where a clipped line ended), at least two lines one below the other where the panel is 476 wide, at most three, and the heading and the buttons drawn | Reads no text back from the pixels; the lines' wording is the layout test's |
| Deck shortcut (`deck-shortcut-test.py`) | Runs the Steam Deck chord watcher's sender against a datagram socket and the state file: the chord opens Main's overlay (the side that `primary_side` names) whoever had the overlay last, takes Guest's open overlay for Main, and closes Main's | Does not read the Deck's input devices |
| Sleep guard (`sleep-guard-test.py`, `sleep-guard-integration.py`) | The state machine against a fake manager, lock and logind events: it freezes the sides that run, leaves a side that is frozen already (Guest's standby) alone and never thaws it, lets go of the lock after the freezing and also when the freezing fails, runs out of time, cannot read systemd or breaks, thaws exactly the units it froze or may have frozen (one whose freeze command ran out of time with its state unread, or that was asked when the guard was stopped, but not one that was read running), freezes again a unit of its own that it finds running at the next sleep (a thaw that was queued behind a busy systemd landed unseen), tries a thaw that fails again and takes the lock again after every wake, also after a second sleep before a wake and after a monitor that ended. Between the announcement of a sleep and the freezing it waits the settle (1.5 seconds) with the lock held: nothing is frozen before the settle is over, a freeze that is stuck still lets the lock go within what logind waits for it (settle, freezing limit and a margin of half a second under `InhibitDelayMaxSec`), also when the guard read the announcement late because it was busy (the waits count from when it last looked, but not for one that came a few milliseconds after it began to listen) and when `systemctl show` is slow (one read after the freezing limit, 0.2 seconds at most), a wake during the settle freezes nothing and the lock is taken again, and so does a logind that leaves the bus (it has forgotten the sleep, no wake will follow, and nothing may stay frozen), where one that only appears on it is no sign of that, one that leaves it after the freezing (also while a freeze is stuck) gets the sides thawed once systemd has no job to sleep, not before, and a job list that cannot be read is asked for again at every idle pass but said once (again when the words change, a read worked between, or another logind left), and that thaw is cut short while the lock is held (to what an idle pass's retry takes, counted from when the guard last looked, so the time systemd took for the job list counts too; the same for a wake that finds the lock held and for a monitor that was started again with units left to thaw), so that a sleep which the new logind announces meanwhile still gets its lock released in time, announcements that come during it are kept and handled in order afterwards, a settle of 0 is the behaviour from before, there is no settle without the lock, and both waits are cut when logind says it waits less than 5 seconds. The lock and the logind monitor against small programs that behave like `systemd-inhibit` and `gdbus monitor` (a program that fails, that does not answer, that is not there, a stop signal while the lock is being taken, a monitor that ends, and whether an event is waiting: the line that logind writes on the Deck whenever a lock is taken, in the pipe when the guard listens, is no event, so an announcement that comes after it is dated from when it was read and gets the whole settle). The real guard as a process, with stand-ins for `gdbus`, `systemd-inhibit` and `systemctl` at the front of `PATH`: the order across processes (every freeze before the lock goes), a freeze that hangs (the lock goes at the end of the settle and the freezing limit, before logind stops waiting), the time from the announcement to the first freeze, a wake, a logind that restarts or a stop during the settle (a stop is said as a stop, not as an error), a logind that restarts after the freezing (the sides stay frozen while `systemctl list-jobs` lists a job to sleep, and are thawed when it lists none), a refused lock, a stop while the sides are frozen, the question to logind (`gdbus call` for `InhibitDelayMaxUSec`) answered, refused, hung and answered with nonsense, the lines in the journal. And against disposable services of a real user manager (`systemctl --user`): the real output of `list-units` and `show`, the cgroup freezer and a standby unit that stays frozen | Does not run logind (so not a restart of it either: that it forgets a sleep it announced is read from its source, not shown, and so is that the job of a sleep it began is in systemd's list by the time the guard hears it leave), a real delay lock held through a suspend, a suspend, Steam's threads on a relay, or Steam handling the announcement during the settle (whether 1.5 seconds is enough) |
| Slow systemd (`supervisor-timeout-test.py`, `audio-router-timeout-test.py`) | The real `supervise()` loop with fake sessions whose calls time out like `systemctl` (`subprocess.TimeoutExpired`): a question that times out is asked again and the session stays the very same one, a long stall is said once, a stop that times out is asked again, a start that times out stops what may have started and starts the side again, a stop that keeps timing out ends with the owner and unit kept for recovery, and any other error still ends the supervisor. The audio router's monitor goes on when a pass or the Guest endpoint check times out. These fail on the supervisor and router as they were | Does not stall a real machine |
| Guest's own loopback address (`side-loopback-test.py`) | Both sides share the host's network namespace, and each Steam wants the same fixed ports on 127.0.0.1; the one that starts second went without, and that side's games then reached the other account's Steam and exited. The four programs that `dosplex-side-loopback.py` makes are run by a small interpreter of their six instructions: a bind, connect or send to 127.0.0.1 goes to the side's address, a received datagram from the side's address says 127.0.0.1, every other address and every other field is left alone and the call always goes on. Which addresses a side may have (in 127.0.0.0/8, not the host's, not the resolver's), the `BPFProgram=` arguments for systemd, pinning refused outside a bpf filesystem, what unpinning removes and leaves. The session unit takes the arguments before its command and refuses any other property. The real `supervise()` loop with fake sessions: Guest's units get the arguments, a restart reuses the one pinned set, the pins go when the supervisor ends, Main's units get none, and a set that cannot be made is said once and Guest starts without it | Does not load a program into a kernel or start a unit: on a device, `side-loopback-check.py` of the development tree does (root). Does not run Steam or a game |
| Shared game store (`game-store-test.py`) | The folders of the store and of each account (the overlay's work directory, the bind of the account's real folder, the stash and conflicts folders), the mount commands (a bind of the real folder, a read-only bind that shows root's files as the account's, and the overlay mounted on the account's own steamapps folder over its own upper directory), that the view is taken away once the overlay is up, the manifests of shared games that leave the account's library by rename when it stays unmounted and come back before the next mount (also when the machine dies after any rename), the library that an account without one on a disk is given, the move of the earlier layout's private layers into the real libraries (renames only, nothing overwritten, whiteouts kept, dying at any journal point, a second run changing nothing), leaving giving every game back with no whiteout or stash left, that mounting twice does nothing and a failed overlay leaves nothing behind, the names and places that are refused, a link or a file where a folder belongs. The edits to Steam's library list (used for the library given to an account and to take the earlier library off): the folder is added once with only its path, removed with the later folders renumbered and everything else as it was, in both copies, with the file's mode kept, never through a link, and refused while that account's Steam runs. The supervisor merges the store into the account's libraries before a side starts only when the store is on, starts the side without it when that fails, and unmounts (and stashes) when it ends and the store is not permanent | Does not mount anything, run Steam or need root: what the kernel, Bubblewrap and Steam do was checked on a Deck by hand. |
| Steam's manifests (`depot-test.py`) | Depot manifests built by the test: files, folders, links, flags, the zipped form; every refusal (truncated, unknown, encrypted names, names that leave the folder, a wrong hash length). A game folder sorted into pristine, changed, missing and extra against one or several manifests without following a link: a changed byte, a size, a file replaced by a link, a folder replaced by a link, a fifo, an unreadable file. Steam's text format read and written back byte for byte, with its refusals. The command line | Does not read a manifest that Steam wrote and cannot tell whether a manifest is Valve's |
| X11 integration | Starts a private Xvfb server and the real X11 compositor; verifies output creation, two cursor images, input delivery/isolation, overlay handoff, pointer-position restoration, resize, the state contract version, one `display_revision` step for each change of a side's game monitor size or scaler (output, layout, Apply, scaler) and none for Cancel, a pending choice or an unchanged size, that the default 16:9 and a kept Native follow the layouts with a client mapped, that the menu draws the supervisor's report (a frame of the output after each step, compared line by line: the notice that the first deferred report for a revision raises, the header's Scaled for the same revision and no second notice, Native again once the monitor is applied; the text itself is checked by the poll test, not by pixels), that a client with a pointer lock gets every move of the host mouse as relative motion of its size, one move at a time and in bursts, to the right, left, down and up past where the screen's edge would have stopped the host pointer, while the compositor brings the host pointer back to the middle of the output (a free or a confined pointer is left where it is, and the jump of the pull back is not motion, also for moves that were already on their way), that a lock which begins while the host mouse rests at the edge of the screen (the right edge for the right side, the left edge for the left side) gets every step as relative motion from the first one, the first time and when the lock is taken up again, the compositor having brought the host pointer to the middle as the lock began, and that the compositor's cursor stays where the lock held it, and graceful shutdown | Does not establish TV visibility or host Gamescope focus, or that Gamescope's Xwayland keeps a host pointer that the compositor has moved (the Deck's check) |
| X11 presentation (`x11-presentation-test.py`) | Runs the real compositor as an X11 client of a private Xvfb with a probe that paces frames by `wp_presentation` feedback, one frame in flight, as a nested KWin does: `state.json` reports `"frame_pacing": "on-demand"`, consecutive frames are one host frame apart (median), the map of the scaled surface sends no preferred scale (the scene's own scale update is held back), and no configure is re-sent after the map or for the frames. It fails on an unpatched wlroots 0.17 (frames three host frames apart) | Xvfb, not the host Gamescope; it models KWin's pacing and does not run KWin, Gamescope or a game |
| Present order (`present-order-test.py`) | Compiles the compositor's actual present-order code and drives it with a model of the X11 backend's presents, which carry the sequence of their commit or the one before it. An unpatched backend is never taken for a patched one, whatever happens to its presents (any number of commits in flight, presents out of order, lost or delivered twice, the ones still on their way when the output is switched off arriving after it is on again), and a patched one is never taken for an unpatched one. A patched backend is recognised from a present that carries the output's current sequence, an unpatched one from the present of the output's first commit wherever that comes in the order. The count of the commits still unanswered is right with presents out of order, and each time the output is enabled a count of its own starts. A model of the frame handler's loop, with its rule as compiled, goes back to one commit in flight after commits outside it and does not stop while a present is in flight | Does not run wlroots or an X server; the model follows what Xvfb and the X11 backend were seen to do |
| Host pointer under a pointer lock (`host-pointer-warp-test.py` and the re-centring cases of `pointer-routing-test.py`) | Compile the compositor's actual absolute-motion handler, the decisions of the re-centring and its connection to the display against stand-ins. The X11 backend's pointer is brought back to the middle of the output when it is more than a quarter of the output away from it, and only while the side that owns the motion holds a lock (not for a free or a confined pointer, a lock on the other side, a tablet or another backend); the jump of the pull back is no motion and the positions that were already on their way are, also across the wrap of the display's clock and after the lock has ended; a jump that does not come, a different one and a display that cannot be moved leave the motion as it was; the real activation of a lock brings that pointer to the middle at once, at an edge or a corner, also when the lock is taken up again and when it begins with the position that enters the application, whatever the time between the last position and the jump, and the next real motion is sent (not for a confinement, a lock on a side that does not own the motion, a pointer that has not reported a position or is in the middle already, a device that is not the X11 pointer, a move that is awaited already, or a display that cannot be moved); the connection is opened by the first warp and once, the warp is by an offset with no window, an unreachable display, a broken connection and an error the display answers with are each said once | Does not move a real pointer (the X11 integration test does, in Xvfb) or show that Gamescope's Xwayland keeps a host pointer that the compositor has moved |
| Session isolation | Starts real temporary user services; verifies cgroup cleanup, private runtime removal, literal argv/environment, display apply, activity switching and guest sleep/resume | Does not validate system-manager permissions, two Linux users, real Steam or hostile apps |
| Controller menu | Deterministic hold/repeat/deadzone/dropped-event tests plus real compositor menu selection and other-side rejection | Does not prove physical controller ownership, Steam Input profiles or game-event suppression |
| Swap choice | Compiles the compositor's actual swap-choice code; checks that one control per side is the pair, that with several the one pressed last within ten seconds is meant, that equal presses, old presses and presses on different backends are not guessed (the button then asks for a press), that only a backend both players have counts, and the memory of the last press per controller | Does not click anything or read a real controller; the controller claim test drives the button |
| Controller claim and swap | Runs the real compositor headless, side by side, with a fixture controller inventory and clicks through the real pointer path: Guest lists its own and the free awake controllers, claims and releases at once with no preview, cannot take what another player now has or release what is not its own, has no Swap and no way to move Main's controller; Main keeps its preview, Undo and last-control rule; Swap with several controls exchanges the ones pressed last and otherwise asks for a press while the button stays | Does not show that a controller reaches a game or that Steam sees a second pad; that is the hardware check |
| Shared mouse and keyboard (`devices-shared-input-integration.py`) | Starts a private Xvfb server and the real X11 compositor, side by side, with a fixture controller inventory and a Wayland probe client on Guest's side, and clicks through the menu. A captured frame shows the Mouse and Keyboard rows with Main, Guest and Shared and the Touchscreen row with only Main and Guest, and Shared selected after Keep. Shared asks for Keep or Undo and names the device and Shared; Undo gives the device back to Main. Moving Main's mouse to the right edge of the screen does not reach Guest's client, but a shared mouse does, where it is moved, and a click goes to the side under it; a shared mouse that crosses back and forth leaves Guest's client each time it goes over Main and enters it afresh, where it is moved, each time it returns, and a button held across the crossing keeps the client until the release; a shared keyboard types in the side where the mouse is; and Main still has to keep a control for itself | Reads the preview's label from the compositor's log, not from a frame. Does not show that a physical mouse or keyboard reaches a game: the X11 backend merges the host's devices, and that check needs real hardware. The probe is no Gamescope, so the warp of its cursor on each entry is not shown |
| Input bridge | Real loopback TCP and Unix sockets test authentication, framing bounds, invalid commands, disconnect releases and connection ownership | Does not exercise physical controllers or Input Leap TLS |
| Configuration invariants (`compatibility-regression-test.py`) | Checks that the settings games rely on are still in the repository's own text | Does not prove that a game launches or works |
| Compatibility schema | Validates the scenario file (setup, pass criteria, evidence levels, tiers, waivers), the canonical hash and the append-only acceptance lock, the result record format with the device baseline, the release gate on fixtures (pass, missing, wrong candidate, wrong tier, wrong evidence level, failures) and the generated compatibility page | Does not run a game. It shows how a pass is judged, not that any game passes |
| Packaging checks | Checks selected packaging and policy expectations | Does not install/uninstall a package |

The headless integration test runs as an ordinary user in a newly created
temporary directory. It sets a private HOME and runtime directory, uses the
pixman renderer, disables authentication/session launch, and terminates its own
process in `finally`. Temporary files are removed on completion, including test
failure. The backend never decides what the compositor may do; its options do
(see "Compositor options"). Without `-s` it does not auto-login or poll installed
session state, without `--idle-policy=dim` it never blanks the output or changes
the panel backlight, and without `--allow-power` host power actions do nothing.
The tests that use the `DEV_FRAME`, `DEV_OUTPUT_*`, `DEV_SIDE_SCALE`, `DEV_SIDE_DISPLAY`, `DEV_STATS` or `DEV_SCENE`
commands start the compositor with `--test-hooks`.

The session-launch test similarly uses a temporary HOME, command directory and
log. It never launches real Steam, Gamescope or D-Bus. User-session launch logs
default to the side's workspace instead of a predictable shared `/tmp` file.

### Session lifecycle isolation

The session isolation test requires an ordinary user with a running systemd
user manager and systemd-run with `--expand-environment=no` support (verified
with systemd 255). It creates uniquely named transient services and stops each
exact unit in `finally`. It does not enable services or change the desktop.

One probe forks a child into a separate process group. Stopping its service
must remove both processes and its private runtime while the second service
and an unrelated process remain alive. The supervisor flow test then exercises
invalid display state, Apply, activity changes, explicit restart and guest
sleep/resume. A separate fixture executes the real user launcher with substitute
Steam/Gamescope programs, checking its runtime, private D-Bus, Xauthority and
nested Wayland alias. No real Steam process or game save is opened.

Production supervision now creates a unique system service for each activity,
with the selected Linux user, a mode-0700 RuntimeDirectory and control-group
cleanup. Environment and arguments are passed literally. The user launcher
creates its shims and nested Wayland alias inside that runtime. Mode/restart
helpers write requests; the supervisor stops its own unit. The root supervisor
uses a per-side lock and atomic state files. System services depend on the
compositor service so its shutdown also stops their activities.

This replaces the active supervisor's user-wide process kills, persistent
lingering changes and deletion of shared user runtime files. Legacy launch
scripts and authentication/logout still need review. System-manager startup,
Steam containers, desktop activation and abrupt supervisor death require
hardware acceptance before this path can be called production-ready.

### Sleep and the sides

A side's Steam polls the controller nodes that the native relays provide inside its sandbox. A relay is a CUSE server
that runs as root: the kernel hands each poll to it and the polling thread waits for the answer. When the machine
suspends, the kernel freezes user space in no particular order. A relay frozen before it answered a pending poll leaves
that Steam thread stuck in `fuse_file_poll`; it can never freeze, the kernel gives up after 20 seconds ("tasks refusing
to freeze") and the machine stays awake. The sides must stop first, while the relays still answer.

`packaging/steamos/dosplex-sleep-guard.py` does that. A retained trial (and so `dosplex start`) runs it as the root unit
`dosplex-sleep-guard-<trial id>.service`, bound to the compositor like the audio router and started again whenever the
compositor is. It holds a logind delay lock on sleep (`systemd-inhibit --what=sleep --mode=delay`). When logind says
`PrepareForSleep(true)` it first waits 1.5 seconds with the lock still held (`--settle-seconds`), then freezes the
active `dosplex-session-left-*` and `dosplex-session-right-*` units with `systemctl freeze`, waits until they are frozen
(3 seconds at most) and releases the lock. When logind says `PrepareForSleep(false)` it thaws exactly the units it froze
and takes the lock again; when it says so during the 1.5 seconds, nothing is frozen. Nothing is frozen either when
logind leaves the bus during the 1.5 seconds: a logind that restarts has forgotten the sleep it announced and sends no
wake, so the lock is released and taken again when logind is back. A logind that leaves the bus after the sides were
frozen has forgotten the sleep as well and sends no wake. The guard then asks systemd for its jobs every second, and
thaws the sides, as for a wake, once systemd has no job to sleep (`sleep.target`, `suspend.target`, the
`systemd-suspend.service` family): the sleep may be going on, and a thaw in the middle of it would make it fail. A job
list that cannot be read is asked for again every second and said once. The guard takes the lock again as soon as logind
is back, so a sleep that this logind announces while the guard thaws waits for the lock, and the guard reads nothing
meanwhile. A thaw that runs with the lock held is therefore given 2 seconds, counted from when the guard last looked at
the monitor (the time that systemd took to list its jobs counts), and the idle passes retry what is left. A unit that
was frozen already, such as Guest's standby, is left alone and never thawed. The guard cannot tell who froze a unit,
though: a side that the supervisor puts in standby after the guard froze it (or asked to), and before the wake, is
thawed by the wake, and the supervisor, which does not read the freezer state again, does not freeze it again until the
side's activity changes. Only a change of layout or of a side's activity puts a side in standby, so this takes one
during the sleep itself. The thaw is shown with the fake manager; what the supervisor does is read from its loop. The
recovery of the trial stops the guard, and `dosplex status` says whether it runs.

The wait gives the sides' Steam time to take the announcement itself. Frozen within milliseconds of it, Main's Steam on
the Deck handled the suspend only after the wake, and its client crashed on some wakes (the evidence is in the
docstring of the guard). logind stops waiting for a delay lock after `InhibitDelayMaxSec` (5 seconds by default). The
guard reads that once at start (`InhibitDelayMaxUSec` of the login manager; 5 if it cannot) and keeps the wait, the
freezing limit and a margin of half a second within it, so a stuck guard never blocks a sleep: where logind waits less,
the freezing limit is cut first, down to a second, then the wait. logind counts its time from the announcement, and so
does the guard: an announcement that waited unread while the guard was busy (a thaw that is tried again after a wake)
counts from when the guard last looked, and the wait and the limit run out sooner; one that comes while the guard
listens counts from when it came, also a few milliseconds after the guard began to listen. The margin is for the last
read of the units' states after the limit (0.2 seconds at most) and the release of the lock. `--settle-seconds 0`
freezes at once, as the guard did before the wait. With no lock held there is no wait, because logind does not wait for
the guard then. The wait has tests with stand-in programs and with a real service manager and has not been run through a
real suspend yet.

For one sleep with both sides running, `journalctl -u 'dosplex-sleep-guard-*'` shows:

```text
dosplex-sleep-guard: took the sleep delay lock
dosplex-sleep-guard: PrepareForSleep(true): freezing the sides before the system sleeps
dosplex-sleep-guard: waiting 1.5 s for the sides to take the sleep announcement
dosplex-sleep-guard: froze dosplex-session-left-<id>.service
dosplex-sleep-guard: froze dosplex-session-right-<id>.service
dosplex-sleep-guard: released the sleep delay lock (froze 2 units in 0.08 s)
dosplex-sleep-guard: PrepareForSleep(false): the system woke up
dosplex-sleep-guard: thawed dosplex-session-left-<id>.service
dosplex-sleep-guard: thawed dosplex-session-right-<id>.service
dosplex-sleep-guard: took the sleep delay lock again
```

A freeze that fails is said (`could not freeze UNIT: ...`), and one that does not finish, or whose state could not be
read, is said otherwise (`could not finish freezing UNIT: it is freezing`, or `it is in an unknown state` with the error
of the command when there was one). The lock is released anyway and the sleep goes on as it would without a guard: a
stuck guard never blocks a sleep. A unit that was asked to freeze is the guard's to thaw from then on, also when it
never heard that the freeze was done (the command ran out of time and systemd was too busy to answer the read after it,
or the guard was stopped while it waited): only a read that finds the unit running says that it was not frozen, and that
unit alone is said as `could not freeze`; the others are thawed after the wake. A thaw that fails is tried again until
it works or the unit is gone; one that stays unconfirmed through the retries leaves the unit in the guard's hands, and
if systemd carries it out unseen, the next sleep finds the unit running and freezes it like any other. After a stall of
the whole machine, or a suspend that did not complete, the clocks have run on although no process did, so a call with a
timeout can raise `subprocess.TimeoutExpired` for no fault of its own. The side supervisor, the check of Guest's audio
endpoint and the audio router take that as a slow answer and ask again; the supervisor used to end on it, its session
unit stopped with it and nothing started the side again.

The relays have the same exposure after the sleep. The Deck relay's report-gap watchdog (half a second) does not count
a gap in which the machine slept or stalled, and waits for reports for a minute more. A relay unit that fails is
started again a second later, and a relay whose controller was enumerated again follows its USB slot to the new node
before it serves it (the pin moves in the plan file, the plan in `transaction.json`, the rule journal and the rule,
under the trial lock). The trial watcher ends a trial for a dead relay only after 15 seconds, and waits out a
`systemctl` that is too slow to answer. Hardware acceptance of all of this is still to do.

### Remote input test isolation

The input bridge suite uses temporary Unix sockets and an ephemeral loopback
TCP port. It never opens input devices. It checks missing/wrong credentials,
fragmented messages, invalid ranges, oversized commands, authentication timeout,
rejection of a second sender and release of held keys/buttons on disconnect.
The headless compositor test additionally verifies that a real Wayland client
receives a key release when its input connection closes. Test threads and sockets
are closed before the temporary directory is removed.

The optional network bridge now requires a private, service-owned file containing
64 hexadecimal characters and binds only to loopback. Its system service is
inactive without that token file. Existing unauthenticated remote senders must
be migrated; remote connections require an authenticated tunnel. The compositor's
control sockets are mode 0600; its input stream also checks the peer's UID.
These restrictions cover control endpoints only. Wayland client admission,
separate seats now route application focus and remote events by side. Physical
device isolation, independent cursors and simultaneous two-user acceptance
still require further work.
The legacy Input Leap upstream connection still disables TLS certificate
verification and must not be treated as a secure remote-input option.

### Physical pointer event path in an isolated X server

The X11 suite starts a private Xvfb server and the real compositor, then maps the
Wayland probe on each side in separate runs. `xdotool` sends movement and wheel
input through the X11 backend. The probe checks the resulting Wayland pointer
coordinates and scroll events. Scrolling over the owner's overlay or over the
other side must not reach that client. The same backend keyboard then follows
the pointer to the other side: the old client must receive keyboard leave and
no letter or pressed-modifier events from the new side. Typing resumes when the
pointer returns. A keyboard is attached to only one seat at a time.
If it changes sides with keys held, input is ignored until every key is released.
The test holds Shift across a side change, verifies that neither the held
modifier nor an intervening letter reaches the new side, then verifies normal
typing after release. It waits for that final key before checking the whole event
sequence, so delayed delivery cannot pass an early absence check. Every process is stopped on success or
failure; the real desktop and physical devices are untouched.

These checks cover absolute-motion and wheel listener registration and follow
routing. They do not establish independent cursor positions, exclusive physical
device ownership, relative-motion acceleration or simultaneous two-mouse behavior.

### Per-side pointer position

The X11 test parks the client-side pointer at (120,120), assigns the backend
mouse to that side, and moves a separate remote pointer into the other region.
Clicking the assigned mouse without moving it must still press and release at
(120,120). The client records coordinates with each button event, so the check
does not depend on redundant motion notifications. Both side assignments run.

Assigned relative motion, buttons and wheel events restore that side's saved
position; remote clicks restore their remote target's position. Coordinates are
stored relative to the side and clamped to its current dimensions on restoration.
Each side now has its own cursor image, shown after that side first receives
pointer motion. Full-screen mode hides the inactive side's cursor; layout changes
reposition images from their saved local coordinates. Cursor image buffers are
cached per side rather than recreated on every motion.

The X11 suite uses xwd captures and compares image regions to verify that moving
the right cursor leaves the left image intact. It also tests remote clicks after
local movement and clamps saved positions after shrinking the output. Resize
input checks wait for scene presentation, since state geometry is written before
the resized scene is drawn. Failed scene commits retry on a short timer so
backend buffer-release events can run before another render attempt.
Pointer grabs, application-defined cursor images,
relative acceleration and physical two-mouse acceptance remain incomplete.

### Overlay keyboard handoff

The X11 client test holds W, opens the menu with Ctrl+Shift+M, closes it with
Escape, and attempts another letter before releasing W. Opening must send a
keyboard leave to the application. The menu shortcut, Escape and the attempted
letter must not arrive as game keys. A fresh key after releasing W must work.
The checks run for each side and wait for that final key before inspecting the
whole event sequence. The headless suite exercises the same held-W/shortcut/Escape flow through the
real remote input socket and checks the receiving Wayland client. Controller
game capture still needs separate acceptance.

### Reserved keyboard shortcuts

Ctrl+Shift+M and Super+Shift+M toggle the current owner's overlay on both backend
and remote keyboards. They follow the same rule as the controller chord: Main's
takes the overlay from Guest, Guest's never takes Main's, and in full screen
Guest's does nothing. The hud-ipc command `OVERLAY <side>` decides the same way
(it opens and never closes, and Guest's pending confirmation ends when Main takes
the overlay), and the Deck's own Steam+QAM chord, which is Main's, sends it for
Main's side whoever had the overlay last. The headless suite checks this with the
`CHORD` test hook, `OVERLAY` and the remote keyboard; `deck-shortcut-test.py` checks
the Deck's chord. The older remote-only L/R/S shortcuts have been removed:
Ctrl/Super+Shift+L, R or S pass through to the application. Resolution changes
use the overlay's explicit Apply action. The headless client checks full press
and release pairs for all six former shortcuts. This reduces reserved bindings;
it does not establish that the remaining M shortcut is conflict-free in games.

### Seat and focus checks

The headless suite runs with the current Linux UID assigned first to the left
side and then to the right. `wayland-info` must expose exactly one seat: seat0
for the primary role or seat1 for the guest. The real Wayland key probe receives
keys and disconnect releases on that seat. Moving the remote pointer to the
other side must release held keys without sending a keyboard leave to the first
client; subsequent keys for the other side must not reach that client. Returning
to its side must resume delivery. Remote scrolling must reach that client with
the menu closed and be consumed while its overlay is open.

The compositor uses separate synthetic keyboard objects for the two sides.
Seat visibility follows Linux UID ownership. These tests do not run two Linux
users simultaneously, and do not establish isolation for physical controllers,
shared cursor state, touch cancellation or device reassignment. Those require
additional protocol and hardware tests before release.

### Audio test isolation

The audio suite requires PipeWire, pipewire-pulse, WirePlumber 0.5, pactl,
paplay, parec and dbus-daemon. It runs in a temporary HOME and runtime with a
private Pulse socket and D-Bus. WirePlumber uses its `policy` profile, which
does not enumerate hardware. It creates substitute outputs and a synthetic
microphone; it never opens a physical speaker or records ambient speech.

440 Hz and 880 Hz represent the two players, and 1320 Hz represents the
selected microphone. A one-second sample must show the intended tone above
500 PCM units and unwanted tones below 100. The unassigned microphone must
remain below 100 for all three tones. Captures stay in memory. Every launched
test process is terminated and waited for; temporary server state is removed
in `finally`, including failures. Capture reads have deadlines.

Guest's endpoint runs in the same disposable server as a second private
PipeWire, pipewire-pulse and WirePlumber whose only devices tunnel to Guest's
buses. The test lists what it offers, tries to change defaults, volumes and
modules (also with a property list that claims unrestricted access), plays a
tone through it and records the microphone bus claimed for Guest while other
tones play on Main's buses. Where `unshare --map-auto --map-root-user` works,
a second run emulates the product, a root router with uid 1001 as Guest: the
runtime directory belongs to Guest, the configuration stays with the router,
the processes run as Guest with no groups and a clean environment, and only
Guest can reach the socket. Without user namespaces that part is skipped.

The router keeps stable per-side playback buses and microphone buses. Device
selection changes the connections to those buses. A missing device retains
its saved assignment and has no active route; it must not fall back to another
player's device. An unassigned microphone is silent. Current stream tags and
account names establish routing, not a security boundary. The shared
local TCP server is not an access boundary between the two players (see the security section of
`limitations.md`).

A loopback is loaded with `sink_dont_move=true`, so that a missing output never
leaks onto another device. The price is that one linked to a sink that was
removed and created again (the TV's HDMI handshake does that, under the same
name and with a new index) stays linked to nothing while its arguments still
match its route. The monitor therefore judges a kept loopback by its streams
too: both must be on the live indices of the nodes the route names, or it is
loaded again. `monitor-shared` writes one journal line for each change it makes
and none for a pass that changes nothing, and says a failure that goes on once a
minute. `audio-integration.py` runs that loop for real, with the start of the
user's audio services stubbed out and the shared TCP listener loaded on a free
port, so that it touches neither systemd nor port 4713, and reads those lines.

The built-in overlay has an Audio tab with separate Output and Microphone
choices, mute and disconnected-device feedback. Requests run in a child process
with a bounded response and timeout, while the compositor continues handling
input. Headless tests use a slow recording helper to verify the active controls
for both owners and preserve device IDs containing spaces. The real PipeWire
suite separately exercises the router and measures signal separation. These
are complementary tests; a physical headset/game trial is still required.

The experimental router CLI supports `assign DEVICE left|right --kind output|input`, `assign none SIDE
--kind input`, `list-sinks`, `list-sources`, and `status`. These mutate the live
audio graph; use the isolated test instead of running them against desktop audio.

## Game evidence

Every game scenario is defined in `compatibility/scenarios.yaml`: its setup
(device, tier, TV mode, layout, side, picture choice, presentation, window mode,
Steam Input and controller), an optional per-game profile, automated and human
pass criteria, and the evidence level it needs (synthetic, visual or human).
Add the scenario before fixing a failure, and prefer a general fix over a
profile. A scenario stays pending until it has been played; a source check or a
synthetic fixture never stands in for it.

Record a result only after executing the scenario, and keep the failures next
to the reruns that passed. Every hardware result identifies the candidate build,
OS, GPU/driver, Gamescope and Steam versions, TV mode, layout, controller
identities, Steam Input layout, game build and Proton version, the steps, the
result and the cleanup. It also records the device baseline: whether the root
file system is read-only, which extra users exist, and whether a sudoers
drop-in is present. Record the observed game resolution separately from its
screen region, and include both users and both split orientations. A result for
the Steam Deck's own screen (tier T2) never counts for a TV or monitor scenario
(tier T1), and a lower evidence level never counts for a higher one. Results and
raw evidence stay private.

The maintainer accepts a scenario by adding a line to `compatibility/accepted.lock`.
From then on its definition is frozen, and every release candidate has to pass
it again, or carry a waiver that cites an accepted decision. The generated
[compatibility page](compatibility.md) shows where every scenario stands.

Test overlay open/close during gameplay, native and widescreen display modes,
controller reassignment/reconnect, pointer confinement, audio routing, side
restart/crash, suspend/resume and recovery. Run simultaneous games long enough
to observe latency, memory use and frame pacing. Historical evidence remains
historical until reproduced with the candidate.

## Mixed game and movie workloads

Test two instances of the same game joining a match, different games, a game
alongside a local movie, and a game alongside browser streaming. Run each mixed
case with the activities swapped between sides. Record the media application,
codec/resolution or streaming service/browser version, actual playback result,
frame drops and A/V sync while the game is under load. Verify pause, seek,
captions, fullscreen confinement, independent audio and that either side can
open its overlay or restart without interrupting the other. Having a desktop
launcher or passing an audio tone test does not establish media compatibility.

## Reversibility

Before a hardware trial, record original files, service states, permissions,
ACLs, device assignments and output state. Use versioned candidate files and a
temporary override with an independent timed rollback. Restore the original
state after each failed trial and after testing. Verify cleanup against the
recorded baseline. Preserve game saves, user accounts and unrelated processes.

### Supervisor stop requests

The real user-systemd isolation test also exercises `request-stop`. Each
supervisor publishes a fresh instance identifier. A stop request names that
instance, side and owner; the supervisor stops its owned service and exits.
A request from a previous instance is ignored by a later supervisor. The test
checks detached-child cleanup, runtime removal, the other running side, and
stop while a guest is waiting in single-screen mode.

`dosplex-side-control.sh stop SIDE` queues a request; its successful exit does
not acknowledge completed cleanup. Check matching supervisor state and service
cleanup. This is the supervisor mechanism only: compositor and auth logout still
needs integration and reliable acknowledgment before production acceptance.


### Failed service cleanup

`python3 tests/session-stop-test.py` injects stop errors, observation errors,
active/stopping services, remaining process IDs, leftover runtimes and timeouts
into the real Session.stop method. None may clear its ownership handle. A
collected unit is accepted only when systemd confirms not-found/inactive/dead
and the runtime is absent. The supervisor failure case retains owner, instance
and unit in `cleanup_failed` state. Real user-systemd tests separately exercise
successful stop and collected services.

This confirms supervisor behavior, not UI recovery. The compositor's legacy
PID-based polling/logout paths still need to respect failed cleanup and avoid
reclaiming a side or auto-starting another session after supervisor exit.


### Authentication logout acknowledgment

Auth logout publishes an atomic stop request for the recorded supervisor instance
and waits up to 25 seconds for matching stopped/inactive state. It acquires the
supervisor lock and rechecks state before revoking access and removing ownership.
Authentication operations on each side use an exclusive auth lock. Recorded
supervisor instances cannot be replaced by login merely because their PID exited.

Auth state tests inject timeout, cleanup failure, instance change and concurrent
auth operations; failed logout must preserve state and never signal a PID. The
real user-systemd isolation test also runs auth logout against a sleeping guest
supervisor, checking its acknowledgment and state removal. ACL revocation is
mocked in that test. Physical accounts/ACLs and the root service manager remain
untested. Sign Out now calls this helper without a direct PID kill, and clears
its immediate UI state only on helper success.

These tests do not cover hardware validation of busy/error panels, layout changes
during pending login, legacy state migration, compositor recovery or cross-side shared-user
access policy, and do not establish full two-account logout acceptance.


### Compositor recovery state

`python3 tests/session-poll-test.py` compiles the actual C state polling function
with minimal compositor and focus stubs and a temporary state root. It checks that
running/waiting/stopping ownership remains claimed, cleanup failure and missing
supervisor PIDs require recovery, and only an absent state file returns to the
sign-in state. Read errors and oversized state remain claimed. The production
root stays fixed; the test overrides its compile-time constant.

The compositor no longer launches a recovery worker based on missing frame
commits or PID liveness. Restart belongs to the supervisor. A recovery panel is
rendered even when an app surface remains. The harness tests state decisions;
it does not visually verify that panel or establish trusted process identity.
Structured decoding is covered below; a usable recovery action remains needed.


### Asynchronous authentication UI

Each side accepts at most one helper job. Submission returns immediately; the
completion callback alone launches a session or clears a successful logout.
Pending jobs disable that side's sign-in regions and text entry, skip state-file
polling until completion and display Signing in/Signing out. Failures retain
ownership and display a generic error without helper diagnostics. Automatic
login stops retrying after a failed attempt; manual retry remains available.

Output draining is capped per event dispatch and per operation. Timeout/flood
cancellation kills the owned helper group, then polls child exit without blocking
the compositor. An uninterruptible or privileged helper that cannot exit can
remain pending; privileged cancellation still needs target testing. Shutdown
cancels and detaches pending jobs without executing completion actions, uses
nonblocking reaping and leaves any final orphan reaping to init when Dosplex exits.
No user-facing cancel action or transaction rollback is claimed.

The full local gate exercises existing rendering/input paths, while auth-specific
tests use the real Wayland event loop with disposable helpers. Production PAM,
root manager, busy/error visual acceptance, simultaneous physical input during
auth and changes to layout while a login is pending remain separate checks.


### Guest login completing after a layout change

Layout changes no longer call authentication logout. Full layout delegates guest
activity stopping/waiting to its supervisor and retains the account claim; only
explicit Sign Out removes access and ownership. A login finishing while Full is
selected may start a supervisor, which waits without launching an activity.
Restoring split launches the guest activity under the same supervisor instance.
This is an activity restart, not preservation of game progress. The Session menu
states that Full closes the guest activity and keeps sign-in.

The real user-systemd test starts a new guest supervisor while already in Full,
waits for its sleeping state, checks no guest service was created, restores split
and verifies an activity starts with unchanged instance/owner. Existing tests
cover closing an already-running guest and returning to split. PAM completion,
UI input during that transition and actual game progress are not tested by these
controlled payloads; retain those hardware acceptance cases.


### Session launcher child monitoring

`python3 tests/session-launcher-test.py` compiles the actual C launcher and
monitor with small environment/geometry stubs. Disposable script/sudo spies
check literal username arguments, side display environment and noninteractive
sudo. A failing launcher is reaped using its separately recorded child PID even
if JSON polling has replaced worker_pid. Pipe-blocked child and second-side exit
cases verify nonblocking monitoring and that unrelated children remain alive.
A normal exit after ownership has been cleared does not create a recovery error.

The compositor records fork/exit failures and retains ownership for recovery;
it does not automatically relaunch. This is child-lifecycle evidence, not a
successful-session acknowledgment. Launch acknowledgment and deadlines are covered below. Production Steam/game
readiness and privileged recovery still need acceptance.


### Structured session-state decoding

The C poller now uses json-c with strict and UTF-8 validation flags, a depth limit
of16 and a bounded read below64KiB. It requires a top-level object with matching
side, nonempty bounded owner, integer PID in0..INT_MAX and boolean active. Optional
lifecycle and instance fields are checked against known states and32 lowercase
hex digits. Only a fully decoded snapshot updates owner/PID; bad data preserves
the previous values and requires recovery. Unknown fields are ignored and nested
fields cannot override the top-level claim. Duplicate keys follow json-c's object
semantics; canonical producers do not emit duplicates.

The actual-C test covers wrong types/ranges, missing fields, wrong side, bad UTF-8,
embedded NUL, excess depth/size, comments, trailing objects, malformed strings,
misleading nested fields and valid escaped Unicode/whitespace. It does not prove
state-file ownership, safe path permissions or authentic supervisor identity.

The optional display report (`display_applied_revision`, `display_applied_size`,
`display_result`, `display_detail`) is cosmetic: it feeds the Display page's status
line, the Native and Scaled words of the overlay and `state.json`. A field that is
missing, mistyped or out of range is left out and never invalidates the snapshot
or requires recovery; the detail loses control characters and is cut at a UTF-8
boundary. `display_result` is `applied`, `failed` or `deferred` (a game is running,
so the monitor kept its old size and Gamescope scales the game's picture); any other
word is no result. `display_applied_size` is the monitor size in effect, two
integers from 1 to 16384. The same test covers the line itself: "Applying…" while
the report lags the compositor's `display_revision`, the failure detail for the
current revision, the Scaled sentence for a deferred report (which has acknowledged
its revision) or a monitor that is not the size asked for, and nothing when the
snapshot has no revision. The notice that the first deferred report for a revision
raises shares the Wine notice's slot and 8-second timer; the test steps the poller
through reports, consumed notices, logins and instances to show that it comes once.

The required parser APIs and flags exist in the
[json-c 0.15 header](https://raw.githubusercontent.com/json-c/json-c/json-c-0.15-20200726/json_tokener.h);
[upstream API documentation](https://json-c.github.io/json-c/json-c-current-release/doc/html/json__tokener_8h.html)
describes strict parsing and explicit consumed-input checks. The implementation
checks consumed length as well as parser success and field types.


### State-file trust and file-type checks

The compositor opens the state root, side directory and session file through
file descriptors, rejects symlinks at those entries, and checks each opened
inode. Production requires root ownership and no group/other write bits. The
session file must be a regular file with one hard link and size below64KiB.
It is opened nonblocking so a substituted FIFO cannot stall the event loop.
Rejected entries preserve the prior claim and request recovery; permissions are
not modified by the reader. Newly created helper/supervisor state directories
request0755; preexisting unsafe directories require deliberate recovery.

The actual-C poller harness uses the test user's UID at compile time and exercises
FIFO, directory, symlinked file/side directory, hard-linked file, writable file/
directories and mismatched owner rejection. Its five-second subprocess deadline
would catch a blocking FIFO open. These checks do not validate every ancestor
of the configured state root, secure the helper's write paths or authenticate a
supervisor instance. Those remain separate trust-boundary work.


### Supervisor launch acknowledgment

Each compositor launch generates a128-bit random correlation token and passes
it as DOSPLEX_LAUNCH_TOKEN. The supervisor validates and publishes that token with
its own instance ID. The compositor accepts startup only from a matching owner,
side, token and instance with a live supervisor reporting running/active or an
intentional sleeping state. Starting/waiting states remain pending. Once bound,
a different instance cannot replace the claim through polling.

The side displays Starting session until acknowledgment, with its sign-in controls
disabled. A30-second monotonic deadline requests SIGTERM of the compositor's own
unreaped launcher child and retains ownership/error state; it never signals a PID
learned from JSON or automatically launches another worker. Cleanup remains
asynchronous and must still be confirmed. Explicit successful logout clears the
correlation state. The default launcher is the installed supervisor wrapper;
custom launchers must implement the same acknowledgment protocol.

Actual-C tests verify generated token argv/environment, stale token/wrong owner/
dead PID rejection, pending startup, running/sleeping acknowledgment, unexpected
instance rejection and timeout of an owned child. Real user-systemd integration
verifies supervisor token publication. These are complementary component tests,
not a full PAM-to-Steam startup trial. The token correlates requests; it is not a
secret or an authorization boundary. Root write-path trust, recovery after abrupt
supervisor exit, visible startup UX and usable game-window acceptance remain open.


### Sign Out identity checks

The compositor captures the displayed owner and supervisor instance when it
queues logout. It sends `logout SIDE OWNER INSTANCE`; the helper rejects missing,
invalid or changed identities before publishing any stop request. It then keeps
checking that same identity through stop acknowledgment and access removal.
Polling also binds an observed instance for sessions adopted without a new
compositor launch token, so later snapshots cannot silently retarget Sign Out.

The helper retains `logout SIDE` for explicit privileged administration/legacy
callers, which intentionally acts on the currently observed claim. The compositor
never uses that form. This distinction does not replace a review of sudo/PAM
policy or root write-path trust.

Tests exercise exact C helper arguments, CLI argument-count handling, rejection
before stop/ACL/state mutation, absent state and a matching guarded logout against
a real disposable supervisor. Global layout transitions run after the side-local
stop-isolation assertion, because an intermediate layout can legitimately be
coalesced without restarting the primary. Guest close/resume checks are retained.


### Legacy authentication delegation policy

`python3 tests/auth-policy-test.py` extracts the policy generated by the legacy
guest installer, parses it with visudo and converts it with cvtsudoers. It checks
that this rule delegates only login/logout/status/list-users, has NOSETENV and
contains no unrestricted helper or auto-login grant. Tests never install sudoers
files, run sudo with the generated policy or authenticate an account.

The installer validates a temporary policy before installing it. The compositor's
ordinary-user fallback uses sudo -n without -E; the supported production root
service invokes the helper directly with its configured runtime. Legacy non-root
runtime/configuration behavior remains unvalidated. Every helper subcommand now
rejects unexpected argument counts before creating state directories.

This tests the generated rule, not the complete effective policy on a machine.
Other sudo grants can authorize more. Existing installed policies are not changed
by a source update and need a reviewed migration. PAM/account policy, owner checks
for administrative logout and privileged path handling remain separate work.


### Account eligibility regression

`python3 tests/auth-eligibility-test.py` checks that user discovery, password
login and auto-login share the same eligibility filter. Accounts below UID1000,
UID65534, the dosplex/nobody accounts and shells named false/nologin are excluded.
Direct helper calls cannot bypass that filter. A live legacy claim belonging to
another user cannot be reported as a successful auto-login.

Fixtures use temporary state directories, fake password-database entries and
mocked authentication/ACL operations. They verify rejection before authentication,
access grants or claim publication, and confirm eligible password login still
requires successful authentication. No system users or permissions are changed.
These checks do not establish PAM account-policy parity, expired/locked-account
handling, configurable system UID thresholds or safe production auto-login policy.
The complete pre-deploy gate includes this test.


### PAM adapter verification

`python3 tests/auth-pam-test.py` exercises both helper adapters using disposable
Python fixtures. Combined python-pam success/denial is authoritative and never
retried through the fallback. Legacy PyPAM must authenticate before acct_mgmt;
either denial returns false. Its conversation preserves the provided password
and username. Missing both modules produces a structured failure.

These are adapter contract tests, not real expired/locked-account acceptance.
The reviewed [python-pam source](https://github.com/FirefighterBlu3/python-pam/blob/120d13418540af2eb5eb79d1f59f51ab765f5c58/src/pam/__internals.py)
calls pam_acct_mgmt internally after successful authentication. Its default also
reinitializes credentials. Thus the lack of a separate account-check call in our
combined adapter is not itself a missing check. A supported distribution binding
version, effective login PAM stack and real account-policy cases still need
verification. Do not add a second independent PAM transaction to compensate.

The legacy installer comment claiming that the helper needs no PAM configuration
is insufficient: the helper uses the host's login service, whose policy and
Python binding availability must be established before a production trial.



### Display endpoint availability during sign-in

`python3 tests/auth-endpoint-test.py` verifies both login entry points fail before
ACL commands or claim publication when the runtime/socket is missing, the endpoint
has the wrong file type, or either final component is a symlink. A real disposable
Unix socket exercises the valid path with mocked ACL commands. The helper no
longer silently succeeds when the compositor socket is absent.

This is an availability/type check, not full privileged-path validation. Ancestor
trust, replacement races, stale unlistened sockets, ACL rollback after partial
failure and interrupted publication remain open. No real account ACLs change in
these tests. The complete local gate includes the endpoint test.


### Production gamepad task lifecycle

`python3 tests/gamepad-lifecycle-test.py` injects a route-monitor failure and
cancellation into the actual arbiter task code. It verifies error propagation,
cleanup calls and completion of the old task before a reused event node is
assigned again. Fixtures substitute evdev and permission operations; they do
not prove real grabs, ACL restoration, hidraw isolation or controller reconnect.
The full local gate includes this test.


The gamepad lifecycle fixtures also inject failure at each ACL/ownership/mode
command. Required grants now fail on command errors; missing paths are tolerated
only during cleanup. A virtual clone is closed if its event node cannot be found
or owner setup fails, before installing its event reader or publishing the route.
These checks do not prove ACL rollback, stable hardware identity, exclusion of
already-open handles, or absence of races with udev.


Gamepad topology fixtures now create two identically named HID families, each with
its own event and hidraw links. They check family membership, event-number reuse,
missing topology, non-HID fallback and exclusion of owned virtual clones. Clone
permissions use the UInput handle's node plus a unique physical-path token; a
mismatched node is closed without changing its permissions.

The compositor now only requests a gamepad route; the arbiter alone manages its
physical permissions and clones. This removes the competing name-based permission
sweep. Route keys and UI selection still use model names, so independent assignment
of identical controllers remains incomplete. Live HID ancestry is not a persistent
reconnect identity, and multi-interface devices need explicit hardware coverage.


### Physical controller permission restoration

The arbiter captures the original UID/GID, mode and POSIX access ACL before
locking each physical node. It keeps an open descriptor so restoration targets
the original inode even if its event path is reused. Ordinary teardown restores
that snapshot instead of granting access to a hardcoded main-user group.
Required device opens reject final symlinks and non-character nodes. Repeated
capture rejects a replaced node. All saved descriptors are closed after attempted
restoration, and failures are reported. This snapshot is currently in memory;
SIGKILL, power loss and durable recovery are not covered.

The lifecycle suite tests real ACL/mode restoration on private temporary files
with only the character-device check and privileged chown mocked. A replacement
file at the original pathname remains unchanged. It also injects a restore failure
to check other nodes are attempted and descriptors closed. A disposable process
receives repeated SIGTERM and must finish cleanup before exiting. No physical
input devices or system ACLs are changed. These fixtures do not establish real
udev coordination, crash recovery or isolation from already-open handles.


### Overlay capture in the production gamepad forwarding path

`python3 tests/gamepad-capture-test.py` checks the arbiter's gameplay gate and
actual forwarding callback. Opening the owning side's overlay neutralizes already
forwarded buttons/axes. Menu-period events are withheld from the virtual gamepad;
closing waits for neutral controls and a drained input queue before forwarding
again. This prevents a queued menu tap from being replayed into gameplay.
The other side is selected using primary/guest role fields, including swapped
physical sides. Missing, invalid or disconnected display state blocks forwarding.
SYN_DROPPED neutralizes and holds the old route until its supervisor rebuilds it.

This is forwarding capture only: controller menu commands and chord handling are not
covered here, Steam Controller/Steam Input devices
remain excluded there, and hardware/Steam background isolation is unverified.
State is checked before input batches and on a20ms timer; there is no compositor
capture acknowledgment. The tests use fixtures, not physical devices. They
include lost events, held controls at startup/open/close, queued taps, wrong-side
capture and malformed/unsafe state files including a FIFO that must not block.


### Configurable controller shortcut regression

Run `python3 tests/controller-shortcut-test.py`, the shared controller-menu and
broker capture suites, and the development-only native Deck gate test. The new
suite covers JSON defaults/invalid values, Up+Back in either order, one-second
hold, release before rearming, normal Up tap replay, and native report suppression
for all three supported combinations. Steam Controller key-based D-pad detection
must take precedence over its analog touchpad HAT axes.

`overlay-menu-flows.py` also checks all three Main controller assignment buttons
against a stable identity fixture and rejects Guest reassigning Main's controller
(Guest's own Claim and Release are covered by `tests/controller-claim-integration.py`). Write inventory
fixtures with mode0600: the compositor intentionally rejects group-writable data.
Hardware delivery, reconnect and consumption by the selected Steam process still
require separate checks.


For physical debugging, controllers.json exposes capture/drain/neutral state,
off-center axis codes and aggregate Back/Up/stick counters. It does not retain
raw reports or screenshots. Read these before restarting the broker. A fresh
external-controller capture must also grab its exact sibling mouse/keyboard
endpoints: chmod alone cannot revoke Gamescope's existing open descriptor.

### Controller identity, driver mode and Steam's saved mappings

Do not label an unchanged physical evdev layout as Xbox. The broker preserves
native VID/PID/version/bus for ordinary controllers. The explicit wireless Steam
Controller adapter exports a normalized Xbox layout. Tests cover Sony unsigned
stick centers versus trigger minima, Xbox axes, and USB/Bluetooth identities.
These fixtures establish policy; they do not establish physical compatibility.

The retained trial opts into `DOSPLEX_STEAM_MODE_POLICY=managed` only with its native
Deck relay active. This temporarily sets the module-wide `hid_steam/lizard_mode`
parameter to N, preventing long Start from switching the wireless controller out
of gamepad mode. The original value is written first to the trial's
`controller-preferences/steam-mode-recovery.json`. Broker exit and final root
recovery restore it; crash restart preserves the original journal. No modprobe
configuration is changed. Production does not opt in by default: it needs a
lifecycle owner covering every Steam-controller input path before using this
module-wide policy. The behavior follows the upstream
[hid-steam driver](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-steam.c).

`steam-controller-mapping-check.py --root <retained-root>` adds a disposable
wireless-layout source through the live broker and Guest device delivery, then
checks the actual SDL mapping logged by Guest Steam. It injects no button events.
It verifies that the Steam consumer agrees with the exposed layout, a boundary
not covered by a uinput-only test. It removes the source and preference and
restarts the broker to clear its temporary inventory entry. Run while Guest is
idle. The corrected version0114 mapping passed.

Earlier experiments left incorrect Xbox mappings in Guest's `SDL_GamepadBind`.
`repair-controller-mappings.py` repairs only entries matching those known bad
mappings. It pauses the validated side supervisor, stops that side, journals only
the affected mapping strings, writes atomically, and resumes the supervisor in
finally. Final recovery reverses only unchanged repaired strings, preserving
other profile changes. Never replace the whole Steam configuration to undo this.


### Input queue overflow regression

A wireless controller fixture stalls its reader while sending 4096 reports,
forcing SYN_DROPPED. It verifies recovery with the same virtual controller, then
checks normal controls and overlay isolation. Startup must drain stale events
after slow permission setup, then read current kernel state. Runtime recovery
neutralizes the game and waits for release without unplugging the clone.
The physical wake loop was reproduced and fixed.

## Ordered controller shortcut acceptance

Default `dpad_up_back`: hold Select/Back first, then press D-pad Up and keep
both held for the configured duration (default one second). Release before
navigating. Up alone must have no reservation delay. Up-first must not open the
overlay; release both before retrying. Select is delivered to the game immediately and exactly as pressed (tap, hold,
release); only Up is withheld, and only while Select is down. A Select + Up
released before the hold delivers Up at once. The game sees Select released when
the menu opens. Verify on Deck and native Steam Controller in Steam and a running game, including
no input behind overlay, close/reopen and held-button release. Other configured
chords follow the same rule: the first button passes through, the second is withheld. Synthetic passes do not prove latency at
the display or hardware acceptance.
