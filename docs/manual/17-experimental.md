# Experimental

The shared game library and starting at boot are off after an install. Each has run on few machines, and a mistake is hard to undo. Each needs `--experimental` to turn on. `dosplex status` shows whether each is on.

Every command starts with `sudo /opt/dosplex/bin/dosplex`.

## The shared game library

Installed games move into one store. Both players see them in their own library and play one copy. Each still needs to own the game.

> [!WARNING]
> It moves your installed games. A fault can make Steam ask to download a game again. It has open defects (below): turn it on only when you trust both accounts.

| You want to | Run |
|---|---|
| Turn it on | `library on --experimental` |
| See what it holds | `library status` |
| Turn it off | `library off` |
| Share one game | `library share APPID` |
| Eject a card with shared games | `library eject MOUNT` |
| Forget a disk that is gone | `library forget DISK` |

- `library on` moves nothing yet. Games move at the next start of DosPlex.
- `library off` gives every game back to the player who shared it, at the next restart or start of DosPlex. `library status` shows on, leaving or off.
- Turn it off and restart the machine before `dosplex uninstall`, which refuses while it is on.
- Close games on an SD card or USB drive before you remove it.
- `library auto on` shares new games by themselves; `auto off` leaves it to you.

Open defects:

- A program in a player's account can name a foreign folder as its Steam library, and the tidy-up then renames files in it.
- A program in a player's account can redirect a library that is being put in place.
- A game on an SD card or USB drive that arrives while Steam runs can be played before Steam checks its files.
- A file list that one account forges can remove the other player's copy of a game.

## Starting at boot

The machine starts DosPlex by itself with Main alone on the full screen. No controller is needed. If DosPlex cannot start, you get plain SteamOS.

Turn it on with `autostart on --experimental` and off with `autostart off`.

## The menu from other gamepads

A DualShock 4, DualSense, Xbox or Switch Pro controller opens the menu with the Steam Controller chord. The D-pad or left stick moves, the bottom button selects and the right button goes back or closes. A pad drives only a menu it opened. The warning in [The menu](05-the-menu.md) applies.

To turn it off, run `sudo touch /var/lib/dosplex/gamepad-menu-off` and restart DosPlex. Delete the file and restart to turn it on again.

More: [the installation guide](../steamos-installation.md#shared-game-library-experimental).
