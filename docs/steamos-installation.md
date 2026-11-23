# Installing DosPlex

DosPlex 0.1.0 is an alpha. It installs on a Steam Deck or a Steam Machine that runs
stock SteamOS with the read-only system partition left on. It writes nothing
under `/usr`, never runs the package manager, does not touch the display manager
or the SteamOS updater, and grants no groups to anyone. A reboot returns the
device to normal SteamOS unless you turned on [starting at boot](#start-at-boot);
one command brings DosPlex back either way.

After setup, follow the [user guide](user-guide.md) for the menu, picture size,
device assignment and sound, and the [game notes template](game-test-notes.md)
for compatibility reports. Read the [known limitations](limitations.md) first.

## What you need

- A SteamOS version, Gamescope version and Steam client version from the
  [tested set](#tested-versions). Anything else is refused unless you say so.
- A TV or monitor, connected and switched on. A Steam Deck can also use its own
  screen when it is not docked.
- No controller is needed to start. DosPlex starts with Main alone on the screen
  and a keyboard, a mouse or a generic gamepad works. Steam Controllers are
  optional; they are the controllers the alpha was tested with, each on its
  USB wireless receiver (28de:1304 or 28de:1305). Other combinations are not
  supported yet. A controller does not have to be awake when you start.
- Main's Steam account on the normal `deck` user. Guest gets a separate local
  account and signs in through Steam itself; Guest may stay signed out while you
  test Main. Never give passwords or Steam Guard codes to anyone or put them
  into a script.
- The release you were given. That is either a [signed release](#install-a-signed-release),
  the archive `dosplex-VERSION.tar.gz` with its signature file, or a release
  folder with the `dosplex` program, the `bundle` folder and the manifest SHA-256
  from the release notes. The bundle has to be one built for Steam Controller
  receivers: a bundle built for a device with built-in controls lacks the
  receiver helper, and `install` says so and stops, because `start` enrols every
  receiver that is plugged in.

## Install

1. Switch to Desktop mode (Steam button, Power, Switch to Desktop) and open a
   terminal.
2. The `deck` user has no password until you set one, and the installer needs
   `sudo`. Set it once:

   ```bash
   passwd
   ```

3. In the release folder, check the device. This changes nothing. It lists the
   receiver slots it finds, for example `/dev/hidraw4 (28de:1304)` or `(28de:1305)`; the numbers
   can change whenever you unplug a receiver:

   ```bash
   cd ~/dosplex-release
   sudo ./dosplex preflight ./bundle --manifest MANIFEST_SHA256
   ```

4. Install. `--output-mode` is optional: a mode your TV advertises, which
   DosPlex uses while it runs.

   ```bash
   sudo ./dosplex install ./bundle --manifest MANIFEST_SHA256 --output-mode 3840 2160 60
   ```

   Controller slots are optional too. To say which controller is Main's and
   which is Guest's from the start, pass the slot of each (either one alone is
   fine). The slots have to be Steam Controller receiver slots and two different
   ones, but the controllers do not have to be awake:

   ```bash
   sudo ./dosplex install ./bundle --manifest MANIFEST_SHA256 \
     --main-hid /dev/hidraw4 --guest-hid /dev/hidraw5
   ```

   On a Steam Deck, Main's controller is the Deck's own controls (preflight
   lists them as `28de:1205`), so you pass no `--main-hid`. To give Guest a
   receiver slot from the start, pass only `--guest-hid /dev/hidrawY`.

Installing:

- hashes every file in the bundle against the manifest whose SHA-256 you gave,
  before anything from the bundle runs. A changed file, a changed manifest or a
  file the manifest does not list stops it;
- checks the SteamOS, Gamescope and Steam client versions, the display and the
  controllers;
- records what it changes in a journal under `/var/lib/dosplex/journal/` before
  it changes it, and notes your original session settings;
- copies the payload to `/opt/dosplex/releases/<version>/`, points
  `/opt/dosplex/current` at it and keeps one earlier release;
- creates the `guest` account with a private home, or adopts one that is
  already there and safe to use;
- puts DosPlex in Steam's library and on the desktop, where you start it (`--start` starts it right after the install).

The install prints one line for the checks that passed and the whole line for
each check that did not. `preflight` prints every check. The last lines say how
to start DosPlex: from Steam's library (Library, Non-Steam, DosPlex), from the
DosPlex icon on the desktop, or with `sudo /opt/dosplex/bin/dosplex start` in a
terminal. With `--start`, DosPlex starts right after the install. Starting
switches the device to Gaming mode, so save desktop work first. It takes up to
two minutes. It runs as a system service, so closing the terminal or the ssh
connection does not stop it. `--no-start` is accepted and does nothing, because
not starting is the default. A line before those says how to
[start DosPlex at every boot](#start-at-boot); that is off until you turn it on.

Starting also lists every other controller slot of the receivers that you gave
in Devices, awake or not, up to eight slots in all. Without a slot it lists
every slot of every receiver that is plugged in, and without a receiver it
starts with the keyboard, mouse and generic gamepads only.
The first controller is Main's. While two players share the screen and the second player has no controller, the next controller that wakes goes to the second player by itself. On a Steam Deck the built-in controls are Main's, so the first other controller is the second player's. You can change who has which controller at any time in the menu, under Devices, and DosPlex remembers what you chose. See
[Choose your controls](user-guide.md#choose-your-controls).

### A guest account that is already there

`install` adopts an existing `guest` account only when that is safe. An older
DosPlex installer put Guest into the `audio`, `input` and `seat` groups, which
would let Guest reach the other player's devices, so `install` stops and names
the groups. If the account is that leftover, run the same command again with
`--adopt-guest-strip-groups`. It prints what it will do, then removes exactly
Guest's membership of `audio`, `input`, `seat`, `video` and `render` and nothing
else, one journaled step for each group. Any other group that gives Guest too
much, such as `wheel`, still stops the install. `uninstall` puts the memberships
back unless you also remove Guest.

## Install a signed release

A signed release is two files that belong together, both on the release page:
the archive `dosplex-VERSION.tar.gz` and its signature
`dosplex-VERSION.manifest.json.minisig`. Put them in one folder, take the
`dosplex` program out of the archive and check its SHA-256 against the one on the
release page before you run it:

```bash
cd ~/dosplex-release
tar -xzf dosplex-VERSION.tar.gz dosplex-VERSION/dosplex
sha256sum dosplex-VERSION/dosplex
```

That hash is the one thing you check by hand. The program has the release
signing key built in and checks everything else itself. Never run an installer
whose hash you did not compare, and never pipe a download into `sudo sh`.

```bash
sudo ./dosplex-VERSION/dosplex install --release dosplex-VERSION.tar.gz \
  --main-hid /dev/hidraw4 --guest-hid /dev/hidraw5
```

`--release` takes the place of the bundle folder and of `--manifest`. The
signature is looked for beside the archive; `--signature FILE` names another.
Before it changes anything the program:

- checks the signature of the release manifest with its built-in key. A
  missing signature, a signature from another key and a changed manifest all
  stop it, and so does a program that has no release key built in;
- checks every file of the archive by size and SHA-256 against that manifest,
  and that the archive holds nothing else. Nothing is unpacked to disk and
  nothing from the archive runs;
- checks the SteamOS, Gamescope and Steam client versions, as for any install.

It then installs the files like any other install, together with the `dosplex`
program that comes in the archive. `dosplex preflight --release dosplex-VERSION.tar.gz`
does the checks without installing.

## Day to day

The installer puts the program at `/opt/dosplex/bin/dosplex`. Every command needs
`sudo`:

```bash
sudo /opt/dosplex/bin/dosplex status
sudo /opt/dosplex/bin/dosplex stop
sudo /opt/dosplex/bin/dosplex start
```

To type less, add `alias dosplex='/opt/dosplex/bin/dosplex'` and
`alias sudo='sudo '` to `~/.bashrc`; then `sudo dosplex status` works.

- `dosplex status` shows the installed version and manifest, whether DosPlex runs,
  each side's state and restart count, and Guest's audio endpoint.
- `dosplex stop` gives Steam back and returns to Desktop mode if that is where
  you started.
- `dosplex start` starts DosPlex again with the two players side by side. Controller slots you
  gave are remembered, but only while each still is a receiver slot; one that is
  not is left out with a note and never stops the start. Pass `--main-hid` and
  `--guest-hid` if a receiver moved. `--layout single` starts with Main alone on
  the full screen, `--layout horizontal` with one player above the other
  (Top / bottom in the menu); `vertical`, the default, is Side by side.
- `dosplex autostart on|off|status` turns [starting at boot](#start-at-boot)
  on and off.
- `dosplex entry on|off|status` writes or removes the
  [entry for Steam's library](#start-from-the-steam-library). An install writes it.
- `dosplex version` shows the version of the program and of the installed release.

## Start from the Steam library

You do not need a terminal to start DosPlex. An install leaves an application
called **DosPlex** in the main user's account, with the DosPlex icon, and the same
launcher on the desktop (`~/Desktop/DosPlex.desktop`, when that folder exists). While Steam is running in the
main user's session, asks Steam to add it to the library. It says "DosPlex is in
Steam's library" when that worked. If it says it could not (Steam was not
running, there is no `steamos-add-to-steam` on the system, or DosPlex itself was
running), start Steam and run `sudo /opt/dosplex/bin/dosplex entry on` to try
again, or add it by hand once:

1. In Desktop mode, open Steam.
2. Choose **Games**, then **Add a Non-Steam Game to My Library**.
3. Tick **DosPlex** and choose **Add Selected Programs**.

From then on, open **DosPlex** under **Non-Steam** in Gaming mode and press
**Play**. Steam closes, and after about half a minute both sides are there.

Behind the entry is a small service that systemd starts only when the entry is
used. It accepts one request, "start", and only from the main user's account:
Guest's account and every other account are refused. Nothing of DosPlex runs
before you press Play.

Steam updates its own client often. A start from the library does not stop for
that: a Steam client that is not in the [tested set](#tested-versions) is shown
as a warning in `dosplex status`, and DosPlex starts. Another SteamOS or
Gamescope version still stops it, with a window that says so; see
[Start at boot](#start-at-boot) for what to do then. A start typed in a terminal
stops for all three, because there you can add `--allow-untested`.

`sudo /opt/dosplex/bin/dosplex entry status` says whether the entry is ready and whether Steam's library has it; `entry on` adds it to the library again if it is missing.
`sudo /opt/dosplex/bin/dosplex entry off` removes the application and the
service (remove the shortcut from Steam yourself); `entry on` writes them again.
An update keeps your choice, and an uninstall removes both.

## After a reboot

A reboot returns the device to stock SteamOS, unless you turned on
[starting at boot](#start-at-boot). Press **Play** on DosPlex in Steam's
library, or run `sudo /opt/dosplex/bin/dosplex start`, to bring it back.

On a Steam Deck that is not docked (no external display connected), DosPlex
starts on the built-in screen at the mode it already has: the panel is 800x1280
and is shown turned, as 1280x800. A saved TV mode is ignored while undocked and
kept, so the next docked start uses it again. `--output-mode` then accepts only
the panel's own mode (`800 1280 60` or `1280 800 60`) and changes nothing. A
Steam Machine has no built-in screen, so without a display `start` still stops
with "no connected display".

If you used `--output-mode` on a TV, run `dosplex stop` before you reboot. Otherwise the
TV stays at the mode DosPlex saved until the next start and stop put the old one
back. A running Gaming mode may apply the restored mode only the next time it
restarts.

## Start at boot

Starting at boot is experimental in this release. After an install it is off: DosPlex
starts only when you start it, and a restart of the device is plain SteamOS. To turn it
on, say that you know it is experimental:

```bash
sudo /opt/dosplex/bin/dosplex autostart on --experimental
```

Without `--experimental` the command refuses and changes nothing. An installation that
already starts at boot keeps doing so after an update.

It writes one unit, `/etc/systemd/system/dosplex-autostart.service`, and its
link in `graphical.target.wants`; it does not start anything now. At every boot
the unit runs `dosplex start --boot` once the display manager is up. That starts
DosPlex with Main alone on the full screen, as the Steam you know, and it needs
no controller. Guest is not signed in until a player opens a split from the
menu. `dosplex autostart status` and `dosplex status` say whether it is on
("Starts at boot"); `sudo /opt/dosplex/bin/dosplex autostart off` removes the
unit and the link, and `uninstall` does too. An update keeps it on and writes
the unit again if it went missing. It is off after an install.

A start at boot never makes things worse than stock SteamOS. When it cannot run
it leaves the device on stock Steam and exits without an error:

- In Desktop mode it does nothing. It never switches the device to Gaming mode.
- It waits up to five minutes for Steam to be idle, because Steam may update
  itself at boot. If Steam is not idle by then, DosPlex does not start and Steam
  stays as it is.
- If something goes wrong after it began, it undoes what it did and returns to
  stock Steam. `systemctl status dosplex-autostart.service` and
  `journalctl -u dosplex-autostart.service` say why.
- On a SteamOS or Gamescope version that is not in the
  [tested set](#tested-versions) it is refused, unless you gave `--allow-untested`
  at the install or the last start and the versions are still the same. After a
  SteamOS update, run `sudo /opt/dosplex/bin/dosplex start --allow-untested` once
  (or `update`) to accept the new versions, or leave DosPlex off until a tested
  release comes. A Steam client that is not in the tested set does not stop it,
  because Steam updates itself: `dosplex status` shows a warning with the version.

A boot that did not start DosPlex does not need a repair; run
`sudo /opt/dosplex/bin/dosplex start` when you want it.

## Update

`dosplex update` looks for a newer signed release, installs it and keeps the
release it replaces:

```bash
sudo /opt/dosplex/bin/dosplex update
```

- It reads the signed release index of the channel and checks its signature
  with the key built into the program. By default the channel is the list of
  DosPlex releases on GitHub. `--channel ADDRESS` (an `https://` or `file://`
  address) or the environment variable `DOSPLEX_CHANNEL` points it at another
  one, for example a private copy.
- It takes the newest release that is newer than the one installed and tested
  for your SteamOS, Gamescope and Steam client, and says which newer releases it
  skipped and why. If none is tested for your device it stops and changes
  nothing; `--allow-untested` takes the newest anyway, at your own risk.
- It downloads the release, checks the signature of its manifest and every file
  (as for [a signed release](#install-a-signed-release)), puts the files in
  `/opt/dosplex/releases/<version>/` and switches `/opt/dosplex/current` to them
  in one step. The release it replaced is kept; the one before that is deleted.
- If the release comes with another `dosplex` program, that program is
  installed and the one it replaces is kept as `/opt/dosplex/bin/dosplex.previous`.
- If DosPlex was running, it is stopped and started again on the new release,
  with the layout it had. `--no-restart` leaves it on the release it started with until you run
  `dosplex stop` and `dosplex start`.

`dosplex update --check` only says whether a newer release is available. An
update never goes to an older release: a release is installed only if its
sequence number is higher than the installed one. The release files cannot be
changed on the way, because the signature covers them; the download itself uses
the network, and only `dosplex update` does. Every step is recorded in the
journal like those of an install.

Installing again from a newer bundle or release archive works as well. It
replaces the files, keeps the old release, and leaves a running session alone
until you stop and start it.

## Roll back

If the new release misbehaves, go back to the one before it:

```bash
sudo /opt/dosplex/bin/dosplex rollback
```

Nothing is downloaded. `rollback` checks the kept release against the digest
recorded when it was installed, switches `/opt/dosplex/current` back and
restarts DosPlex if it was running (`--no-restart` as for `update`). The release
you left is kept, so a second `rollback` goes forward again. It refuses, and
changes nothing, if the kept release was changed or if your SteamOS is not in
its tested set (`--allow-untested`). It leaves the `dosplex` program alone: if
the newer program is the problem, run the one `update` kept:

```bash
sudo /opt/dosplex/bin/dosplex.previous rollback
```

`dosplex update` can offer the same release again later; rolling back does not
mark it as bad.

## Shared game library (experimental)

By default Main and Guest each keep their own Steam library, so a game that both
want is downloaded twice. The shared game library keeps one copy of a game in a
store that both accounts read. Once it is on you do nothing more: a game either
account installs shows up installed for the other one too. It is experimental: you
turn it on yourself.

There is one drive in Steam for each disk, as on a stock SteamOS. The shared
games are shown inside each account's own library on that disk: Steam's Storage
page lists the SteamOS drive and the SD card, with nothing extra. Everything an
account writes (its own installs and updates, saves inside a game folder,
Proton prefixes) goes to its own library, where it is on a stock SteamOS.

The shared game library is experimental in this release and is off after an install. It
moves installed games into one store that both accounts play from. To turn it on, say
that you know it is experimental:

```bash
sudo /opt/dosplex/bin/dosplex library on --experimental
```

Without `--experimental` the command refuses and changes nothing.

This needs an installed DosPlex whose release ships the store tool. It makes the
store (by default in `/home/dosplex-library`, or where `--root PATH` says),
writes its units and one udev rule and records the library in
`/var/lib/dosplex/state.json`:

- `dosplex-game-library.service` runs `dosplex library slot` before the display
  manager starts (enabled by a link in `multi-user.target.wants`). At shutdown it
  runs `dosplex library release`, while the disks are still mounted and after the
  sessions have stopped.
- `dosplex-game-library-auto.timer` and `dosplex-game-library-auto.service` run
  `dosplex library share --new` three minutes after the start and then every ten
  minutes, at idle priority.
- `dosplex-game-library-disk@.service` and the udev rule
  `/etc/udev/rules.d/90-dosplex-game-library.rules`: when a disk is put in or
  pulled out, one instance of the unit runs `dosplex library arrive` for it.

It is one journaled operation: if a step fails, or the power goes out and you
run `repair`, what it added is removed again. It refuses if the library is
already on, if the release has no store tool, or if an earlier operation was
interrupted.

### How a game gets shared

1. In the background, DosPlex reads every file of an installed game and compares
   it with Steam's own file list. Only files that match go in: saves, settings,
   mods and Proton prefixes stay with the account. You can play meanwhile.
2. At the next start of DosPlex, or the next restart of the machine, the game
   moves into the store. That takes a moment, not a copy. Both accounts then
   see it installed.
3. The other account's Steam checks every file of the game once before its
   first play.

A new install and an update that one account's Steam downloaded take the same
way. So a new game reaches the other account after the next start of DosPlex,
and at the latest after the second one.

Steam drops its file lists for games it installed long ago. DosPlex asks each
account's Steam for them again when DosPlex starts and the device is online.
Steam fetches them at its start, within about a second for each game and without
downloading or changing a game file. Those games are shared one start later.
If Steam could not answer (the device went offline), DosPlex takes the request
back at the next start and tries again a day later, so no game stays in
"update required".

To share at once instead of waiting for the background check:

```bash
sudo /opt/dosplex/bin/dosplex library share 620 70
sudo /opt/dosplex/bin/dosplex library share --all --user guest
```

`share` reads the games of one account (`--user` is Main by default, or
`guest`), shows its progress, and prints which games it queued and which it
refused, with the reason.

`sudo /opt/dosplex/bin/dosplex library auto off` stops the background check and
removes its two units; `auto on` brings them back. The choice survives updates.

### Games on an SD card or a USB drive

A game is never copied to another disk. Each disk that holds a Steam library
gets a store of its own, in `dosplex-library` at the top of that disk, the first
time a game on it is shared. The disk needs an ext4, btrfs, xfs or f2fs
filesystem (SteamOS formats cards as ext4). The shared games of that disk are
shown inside each account's library on it.

SteamOS opens a card to the account that is logged in (Main) only. Guest gets a
library of its own on the card, so that its Steam shows the card as a drive too:
the folder `dosplex-library/disk/accounts/guest/SteamLibrary` on the card,
owned by Guest, and reachable for Guest at `/run/media/guest/NAME/SteamLibrary`
(a bind of its folder, made at every start). It is an ordinary library in
Guest's Steam list. Guest's own installs onto the card go there.

If such a disk is missing when DosPlex or the machine starts, its games are
not shown for either account until it is back, and Steam shows the drive as
missing, as it does for any card that is out; nothing is lost. If it will not
come back:

```bash
sudo /opt/dosplex/bin/dosplex library forget /run/media/deck/NAME
```

#### Shutdown, a card that is put in, a card that is pulled out

Stock Steam must never find an app manifest for a shared game whose files it
cannot see: it would offer to download the game again. So DosPlex makes the
disks right at the moments the disks change, not only at its own start.

- **Shutdown and restart.** The boot unit's stop step, `dosplex library release`,
  takes every account's overlays and binds off every disk and sets the manifests
  of shared games aside (renamed into a stash on the same disk), as an orderly
  stop of DosPlex does. It waits up to 30 seconds for a Steam that still runs.
  If a Steam still runs it keeps its manifests where they are for now; the next
  start of the machine puts it right. It is safe to run twice.
- **At boot.** The slot mounts the libraries before the display manager starts.
  A card that SteamOS mounts after that is handled by the next point.
- **A card that is put in.** SteamOS mounts it a moment after the system sees
  it. The udev rule starts `dosplex library arrive DEVICE`, which waits up to a
  minute for the device to be mounted, and then mounts the overlays for each
  account that has a library on it. It does this while a Steam runs too:
  mounting over a folder adds nothing to the folder. What would change an
  account's real library (putting its set-aside manifests back) waits until that
  account's Steam is closed. `library status` says what waits. If an overlay
  cannot be mounted, the manifests of shared games are set aside at once when
  that account's Steam is closed.
- **A card that is pulled out.** The same rule runs `arrive`, which sees that
  the device is gone and unmounts what is left of it lazily. Nothing can be set
  aside on a card that is not there; the next time it is put in, `arrive`
  mounts the overlays before anything else has to look at the card. A card
  pulled while a game from it runs ends that game, as it does without DosPlex.
- **Eject.** Steam's Storage page ejects a card through UDisks2. That fails for a
  card that holds shared games ("target is busy"), because DosPlex's overlays
  and binds are mounted below and beside the card. Nothing is lost when it
  fails. Use DosPlex's own eject instead:

  ```bash
  sudo /opt/dosplex/bin/dosplex library eject /run/media/deck/NAME
  ```

  It releases the card (and sets the manifests aside for an account whose Steam
  is closed), then asks `udisksctl` to unmount it. If a game from the card runs
  or udisks says no, everything is mounted again and nothing changed. If a Steam
  was running, its manifests stay on the card until that Steam is closed, so
  close it first if you take the card to another machine. The same action is
  `dosplex control eject MOUNT` for a user interface.

A Steam that is already running when an overlay is mounted over its library may
show the shared games only after it looks at the library again, for example
after a restart. Whether Steam notices a library that is mounted over while it
runs has not been shown on a device yet; restart that Steam if the games are
missing.

### Looking at it and turning it off

`sudo /opt/dosplex/bin/dosplex library status` shows whether the library is on,
leaving or off, its place, each account's own library on each disk and whether
the shared games are shown in it (whether its overlay is up), the manifests that
are set aside for it, the games in each store with the account that
shared each, the games waiting for the next start, what the last shutdown and
the last disk that was put in or pulled out did, what is kept aside (see
below), and whether the background check is on (`--json` prints the tool's own
answer unchanged).

`sudo /opt/dosplex/bin/dosplex library slot` is the quick step the boot unit
runs while no Steam runs: it moves queued games in, removes a game that every
account uninstalled and, if the library is leaving, finishes that. You can run
it by hand, but it refuses while a Steam of either account is running. While
the library is off it does nothing.

To turn it off:

```bash
sudo /opt/dosplex/bin/dosplex library off
```

`off` marks the library as leaving. At the next restart of the machine (or the
next start of DosPlex) every shared game goes back, as whole files, into the
library of the account that shared it, and the slot then removes the units, the udev rule, their
links and the state entry. No whiteout of DosPlex and nothing of the stash is
left in any library. What the other account had written for a game it did not
get back (its manifest, files in the game folder) is not deleted: it is moved to
the `conflicts` folder of the store (see below).
`library status` shows when that is done. A disk that is not connected at that
moment keeps the library in "leaving" until it is back, or until you `forget`
it.

While the library is on, `dosplex uninstall` refuses: turn it off, restart the
machine, then uninstall, so that no game is left in the store. `dosplex update`
and `dosplex rollback` refuse to move to a release that has no store tool,
because the boot unit would find nothing to run; turn the library off and
restart first.

### What to know

- A game needs a licence on the account that plays it. The shared copy saves
  disk space, not licences; the same game on both accounts at once still needs
  two licences, or Steam Families.
- Saves that a game keeps inside its own folder are not shared: each account
  has its own. That is on purpose; two players writing one save would break it.
- A file that differs from Steam's list (a modified or patched file) stays with
  the account that has it. The other account's Steam downloads just that file
  when it checks the game.
- The other account gets the branch and language the sharing account chose for
  a game.
- Steam's own tools (Steam Linux Runtime, Proton, the common redistributables)
  stay with each account unless you name them in `share`.
- An account that has a half-finished install of a game in its own library sees
  that one, not the shared copy. Finish or remove the install.
- A game that every account uninstalled is deleted from the store at the next
  start.
- If both accounts had the same game, the second copy is dropped as a
  duplicate. When you turn the library off, the game goes back to the account
  that shared it first, and the other account has to download it again. On a
  disk that only one account has a Steam library on, every game of that disk
  goes back to that account.
- While DosPlex is not running and the shared games are not shown, a stock Steam
  must not offer to download them again. So when a library stays unmounted (after
  a failed mount, or when the library is not permanent and DosPlex stops), the
  account's app manifests for the shared games, and the whiteouts for them, are
  moved by rename to `stash/USER` in the store, and moved back before the next
  mount. Both steps run with that account's Steam closed. `library status`
  counts what waits in a stash.
- Anything that cannot be put where it belongs because that name exists already
  (a manifest a stock Steam made while the stash was out, a file in the
  migration below) is never overwritten: it is moved to `conflicts/USER` in the
  store (on the same disk), and `library status` names it. Look at it, keep what
  you need and delete the rest yourself.

### Updating from the earlier layout

Before this layout each account had a private layer for the shared games and a
library of DosPlex's own, which Steam listed next to the account's real
libraries (so Steam's Storage page showed four drives for two). The first start
of DosPlex, or restart of the machine, after the update moves everything in each
private layer (saves and settings inside game folders, downloaded updates, app
manifests, Proton files) into the account's real library on the same disk, by
rename, and takes DosPlex's own library off both accounts' Steam lists. Nothing
is overwritten: an item that exists in the real library already goes to
`conflicts/USER` in the store and `library status` names it. The move is
journaled and can be interrupted at any point; the next start finishes it.
Until it is finished the shared games are not shown for that account, and
`library status` says so.

## If something goes wrong

- A command that refuses before it starts, for a wrong hash, an untested version,
  a missing display or a controller slot that is not a receiver slot, has changed
  nothing.
- If a command says `/var/lib/dosplex` (or its `journal` directory) is not owned
  by root, it names the owner it found and prints the `chown` and `chmod` that
  fix it. DosPlex keeps refusing until root owns it again.
- If the power went out or the connection dropped during an install, update,
  rollback, start, stop or uninstall, the next command tells you to run
  `sudo /opt/dosplex/bin/dosplex repair`. It completes the operation or rolls it
  back from the journal. An update that stopped before it switched to the new
  release is undone and one that had switched is finished, so the device is on
  one release or the other, never between them.
- A command that stops because of a signature, for a changed file, a signature
  from another key or a release key that this program lacks, has changed
  nothing. If the release key was changed since you installed, install the
  newest release by hand as above: the program inside it has the new key.
- `start` fails cleanly: it returns the device to stock Steam and says why. Switch
  the TV on and close any running game first. A controller that is asleep never
  stops a start.

## Upgrading from DuplOS

DosPlex was called DuplOS until October 2026. The old name used `/opt/duplos`,
`/var/lib/duplos` and the `duplos` command. A device never keeps both: install
DosPlex as in [Install](#install) or [Install a signed release](#install-a-signed-release)
and `dosplex install` replaces the old install.

What `dosplex install` does when it finds a DuplOS install:

- It reads the old install without changing it. If an operation of the old
  program was interrupted, it stops and tells you to run
  `sudo /opt/duplos/bin/duplos repair` first. If the old directories are
  symlinks, it refuses.
- If DuplOS is running, it stops it with the old program (`duplos stop`), which
  puts the TV mode and the Steam session back. If that fails, or the old program
  is gone, it refuses and prints the recovery command. It changes nothing else in
  that case. Save and quit your games first: the running session ends.
- It installs DosPlex exactly as on a clean device, and keeps what only the old
  install knew: the main user, the Guest record (a Guest that DuplOS created can
  still be removed with `--remove-guest`, and one it adopted stays adopted), the
  original SteamOS defaults, the controller slots and the output mode. If the
  install fails, the old install is still there; only its session was stopped.
- It then moves `/var/lib/duplos` into `/var/lib/dosplex` (the side modes, the
  controller registry, the input policy and the per-user Wine journals), keeps
  the old state file and transaction journal as a record in
  `/var/lib/dosplex/legacy-duplos`, and removes `/opt/duplos`. Whatever it cannot
  place, because the new name has it already or because DosPlex did not put it
  there, stays where it is and is listed. Run the install again and it picks up
  where it stopped. `dosplex status` shows `migrated_from` in the JSON output
  after a migration.
- It does not remove anything the old installer left outside those two
  directories (units in `/etc/systemd/system`, udev rules, sudoers drop-ins,
  `/etc/duplos`). `dosplex preflight` names them with their old names; remove
  them yourself.

Your own files move the first time each player's side starts: the Desktop
profile (`~/.local/share/duplos`, with the desktop scale you chose, becomes
`~/.local/share/dosplex`) and the workspace folder (`~/DuplOS` becomes `~/Dosplex`).
A folder moves only if the new name does not exist yet; nothing is merged or
overwritten, and the launch log says what moved.

`dosplex update` of an old DuplOS install does not see DosPlex releases, because
the release index has a new name (`dosplex-index.json`). Install the first
DosPlex release by hand; later updates work as described above.

## Uninstall

```bash
sudo /opt/dosplex/bin/dosplex uninstall
```

This stops DosPlex, removes the unit that starts it at boot if you turned that
on, undoes the install from the journal and removes `/opt/dosplex`. Guest's account and files are kept, because they hold Guest's
Steam library and saves; group memberships that `--adopt-guest-strip-groups`
removed are put back. To remove them as well, run
`sudo /opt/dosplex/bin/dosplex uninstall --remove-guest`. It prints Guest's home
path and asks you to type it; nothing is deleted otherwise. After a plain uninstall
the installed program is gone: the `dosplex` program of the release archive does
it, `sudo ./dosplex-VERSION/dosplex uninstall --remove-guest`. DosPlex never removes
a guest account that was already on the device, and never touches Main's files.

With `--remove-guest`, Guest is removed before the program is. The uninstall waits up to 20 seconds
for Guest's processes to end. If they do not, it stops with `Guest still has running processes`,
or, when `loginctl enable-linger` is set for Guest, says so and names
`sudo loginctl disable-linger guest`. The program is still installed then, and
`sudo /opt/dosplex/bin/dosplex repair` finishes the uninstall and keeps Guest.

A finished uninstall prints what is still on the machine: DosPlex's entry in Steam's
library, the shared library folder `/home/dosplex-library`, the two Steam settings DosPlex
switched on (Maximum Game Resolution, Enable Native Resolution on External Displays) and
`/var/lib/dosplex`. That folder stays while Guest is kept, because its journal is the record
that DosPlex created Guest. When Guest is removed, the folder is removed too, except `perf/`
with your recordings.

## What it changes

| Does | Does not |
| --- | --- |
| Writes `/opt/dosplex` and `/var/lib/dosplex` | Write under `/usr` or disable the read-only system |
| With the shared game library on: keeps the games in `/home/dosplex-library` and in `dosplex-library` on each other disk that has a Steam library, shows them inside each account's own library with an overlay on its `steamapps` folder, makes a Steam library for Guest on a card it cannot enter and adds it to Guest's Steam list, moves an account's manifests for shared games into a stash while that library is not shown, and asks Steam for a game's file list through its app manifest | Share a save, a setting or a file that is not on Steam's own list of the game |
| Fetches the signed release index and the release it names, when you run `dosplex update` | Use the network at any other time, or send more than a plain request for those files |
| Creates the `guest` account, home mode 0700 | Add anyone to a group; uninstall only puts back what `--adopt-guest-strip-groups` removed |
| Takes an existing Guest out of `audio`, `input`, `seat`, `video` and `render`, only if you pass `--adopt-guest-strip-groups` | Change any other group |
| Starts temporary system services while it runs | Install a service that starts at boot, unless you turn on starting at boot (then `dosplex-autostart.service`, until you turn it off or uninstall) or the shared game library (then `dosplex-game-library.service`, the timer `dosplex-game-library-auto.timer` with its service, the template `dosplex-game-library-disk@.service` and a udev rule in `/etc/udev/rules.d`, until you turn it off) |
| Changes the TV mode while it runs, if you asked | Change the default session or the display manager |
| Keeps its journal after an uninstall that keeps Guest, and your `perf/` recordings after any | Delete a Steam profile or a save |

## Tested versions

The release notes list the versions the bundle was tested on, and the manifest
carries them. `install`, `update`, `rollback` and `start` compare the device with
that list and stop if the SteamOS version and build, the Gamescope version or
the Steam client version is not on it. `update` skips a release that is not
tested for your device and takes the newest one that is. The message names what differs and that nothing was
changed. `--allow-untested` goes on at your own risk. The result is recorded and
shown by `dosplex status`, and `start` asks again if the versions change later.
The one exception is the Steam client at a start from Steam's library or at boot, which Steam's own update of
itself does not stop ([Start from the Steam library](#start-from-the-steam-library)).
This release carries the Steam Controller wireless receivers 28de:1304 and 28de:1305 the same way. It is not
tested on other controllers, other receivers or other operating systems. The older Steam Controller dongle
(28de:1142) is not supported.
