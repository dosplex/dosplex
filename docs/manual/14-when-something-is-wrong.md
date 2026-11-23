# When something is wrong

## A black screen

1. Choose **Reset my side** in your menu.
2. No menu? Restart the machine.

## A controller does not answer

1. Wake the controller.
2. Still nothing, or the machine just woke from sleep? Choose **Reset my side**.

A running game may need a restart after a controller is assigned or moved.

## No sound

1. Check that an output is claimed on the **Audio** page.
2. The second player has no output on a first run. Choose one.

## A game quits right after it starts in a split

The game remembers the size from its own video settings. If you last played it in Full screen, it may ask for that size in a split and quit.

1. Choose **Full screen** on the **Session** page.
2. Start the game.
3. In its video settings choose a window or a smaller size, then quit it.

## A game does not start

1. Steam may be idle on your side, with the game behind its screensaver. Press a button, or choose **Reset my side**.
2. Switch the TV on and close any running game before you start DosPlex.

## DosPlex does not start from Steam's library

1. A window says why.
2. If not, go to Desktop mode and run `sudo /opt/dosplex/bin/dosplex start`. It prints the reason.

Usual causes: the TV is off, or a SteamOS update the release was not tested on ([Install](03-install.md)).

## A command stopped

- A wrong hash, an untested version or a missing display changed nothing.
- Power went out during an install, update or uninstall? Run `sudo /opt/dosplex/bin/dosplex repair`. It completes or rolls back the operation.

> [!NOTE]
> A restart of the machine always returns to plain SteamOS.

More: [the user guide](../user-guide.md).
