# Experimental

Two features are in the program and off after an install: the shared game library and starting at boot. They are marked experimental. They have run on few machines, and a mistake in either is harder to undo than a mistake anywhere else in DosPlex. Each is turned on with a command in Desktop mode, and the command needs the word `--experimental`, so that nobody turns one on by accident.

`dosplex status` shows whether each is on.

A third experimental feature, the menu from other gamepads, is on after an install and has its own switch: see the last section of this chapter.

## The shared game library

Without it, each player installs their own copy of a game. With it, installed games are moved into one store on the disk, and both players see them in their own Steam library and play from the one copy. Each player still needs to own the game.

It is experimental because it moves your installed games. A fault here can make Steam ask to download a game again.

Turn it on:

```bash
sudo /opt/dosplex/bin/dosplex library on --experimental
```

See what it holds and what it left alone, and why:

```bash
sudo /opt/dosplex/bin/dosplex library status
```

Turn it off:

```bash
sudo /opt/dosplex/bin/dosplex library off
```

Turning it off gives every shared game back at the next restart of the machine, or at the next start of DosPlex. Each game goes back, as whole files, into the library of the player who shared it. `library status` shows when that is done. While the library is on, `dosplex uninstall` refuses: turn the library off, restart the machine, then uninstall.

### What each setting means

`library status` begins with one of three words:

| It says | What it means |
| --- | --- |
| on | Shared games are in the store and both players play from the one copy. |
| leaving | You turned it off and the games have not gone back yet. They go back at the next restart of the machine or start of DosPlex. Until then both players still see them. |
| off | Every game is back with the player who shared it. Each player has their own games again, as before the library. |

These are all its settings:

| Setting | Command | What it does |
| --- | --- | --- |
| The library, on | `library on --experimental` | Creates the store. Nothing moves yet: games are checked first, file by file, and move at the next start of DosPlex. |
| The library, off | `library off` | Gives every game back, as the paragraph above says. Nothing is deleted and nothing is downloaded again. Do this before you uninstall DosPlex, and before you move a card with shared games to another machine for good. |
| Automatic sharing, on | `library auto on` | The setting after `library on`. Every ten minutes DosPlex looks for games that were installed or updated, checks them, and shares them at the next start of DosPlex. |
| Automatic sharing, off | `library auto off` | Sharing by hand. No game joins the store by itself: a game you install stays yours alone until you share it. Games that are shared already stay shared. |
| Share by hand | `library share APPID` | Checks one game and shares it at the next start of DosPlex. The number is the game's Steam app id. `--all` takes every game of the account, `--user NAME` takes the games of the other player. |
| Eject a card | `library eject MOUNT` | Lets go of the shared games on an SD card and ejects it. Steam's own eject fails while DosPlex holds the card. |
| Forget a disk | `library forget DISK` | For a disk with shared games that is gone for good. DosPlex stops waiting for it. The games on it are not touched. Without this, a library that is leaving waits for that disk for ever. |

Every command begins with `sudo /opt/dosplex/bin/dosplex`. `MOUNT` and `DISK` are the folder that `library status` shows for the card or disk.

A card with shared games shows those games only on the machine that shared them. In another machine, Steam sees the card without them until the card is back.

### Known defects in the alpha

The library's tidy-up runs as the administrator and trusts things that a program in a player's account can change. These are open in this release:

- A program in a player's account can name a folder that is not that player's as one of their Steam libraries. The tidy-up then moves and renames files in it.
- While a library is being put in place, a program in a player's account can redirect it to another folder.
- A game on an SD card or a USB drive that arrives while Steam runs can be played before Steam has checked its files.
- A file list that one account forges can remove the other player's copy of a game, or stop it from being updated.

So turn the library on only when both accounts on the machine belong to people you trust, and not on a machine that others can log in to.

Other things to know before you turn it on:

- **Each player's own files of a shared game stay their own.** Saves, settings and anything a game writes into its folder are kept per player, not shared.
- **A game goes back to the player who shared it first** when you turn the library off. What the other player had written into that game's folder is not deleted: it is moved to that player's `conflicts` folder in the store, which only the administrator can read.
- **A drive you take out** while a side still uses it may stay in use by that side. Close the games on it, or stop DosPlex, before you remove an SD card or a USB drive.
- **Steam may mark a game "update required"** when DosPlex asks it for a file list it no longer has. The mark goes away when Steam has answered or after the next start of DosPlex.

## Starting at boot

After an install nothing of DosPlex runs when the machine starts: you get plain SteamOS and start DosPlex yourself. With starting at boot on, the machine starts DosPlex by itself, with Main alone on the full screen. No controller is needed for that. If DosPlex cannot start, the machine stays on plain SteamOS.

It is experimental because it changes what your machine does when it starts.

Turn it on:

```bash
sudo /opt/dosplex/bin/dosplex autostart on --experimental
```

Turn it off:

```bash
sudo /opt/dosplex/bin/dosplex autostart off
```

After you turn it off, a restart of the machine is plain SteamOS again, and you start DosPlex yourself.

## The menu from other gamepads

A DualShock 4, a DualSense, an Xbox or a Switch Pro controller (any gamepad that is not a Steam Controller or the Deck's own controls) can open and drive the menu of the player it belongs to. The chord is the same as on a Steam Controller: hold Select / Back / View, press D-pad Up, and hold both for about one second. Then the D-pad or the left stick moves, the bottom button selects, and the right button goes back or closes. When the menu is already open because something else opened it (a touch, a keyboard, another controller), the gamepad's right button or the same chord closes it. The gamepad does not move in a menu it did not open, and the game gets that button press too, because the side is not paused then.

It is experimental because DosPlex cannot filter these controllers. Two things follow:

- The chord itself also reaches the game.
- While the menu is open because of such a controller, that player's side is paused. The picture and the sound stand still, and an online game may drop its connection. The side continues when the menu has closed and you have let go of the controller.

A pad drives only a menu that it opened itself. If the menu was opened some other way, the pad does nothing in it.

Turn it off:

```bash
sudo touch /var/lib/dosplex/gamepad-menu-off
```

Then restart DosPlex. Delete the file and restart DosPlex to turn it on again.
