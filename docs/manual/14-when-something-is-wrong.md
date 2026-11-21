# When something is wrong

**A black screen.** Use **Reset my side** from your menu. It closes and reopens your session. If you cannot reach the menu, restart the machine.

**A controller that does not answer.** Wake the controller first. If it still does not work, use **Reset my side**. A running game may need restarting after a controller is assigned or moved between players. If the machine just woke from sleep and a controller does not answer, **Reset my side** helps.

**No sound.** Check that an output is claimed on the Audio page of your menu. An output you claim stays yours across restarts and reboots until you release it. The second player has no default output on a first run — choose one from the menu.

**A game that quits right after it starts in a split.** A game remembers the resolution you chose in its own video settings. If you last played it in Full screen, it may ask for that size again in a split, where your part of the screen is smaller, and some games then quit at once without a message. Choose **Full screen** on the Session page, start the game, set its own video settings to a window or to a smaller size, and quit it. It then starts in a split too.

**A game that does not start.** Make sure Steam is not idle on your side. If Steam shows its screensaver, a newly started game can stay behind it. Press a button to wake Steam, or use **Reset my side**. Switch the TV on and close any running game before starting Dosplex.

**Dosplex does not start from Steam's library.** A window says why. If nothing is shown, switch to Desktop mode and run `sudo /opt/dosplex/bin/dosplex start` in a terminal: it prints the reason. The usual ones are a TV that is off, and a SteamOS update that the installed release was not tested on (see the end of [Install](03-install.md)).

If a command stops because of a wrong hash, an untested version, or a missing display, it has changed nothing. If the power went out during an install, update, or uninstall, run `sudo /opt/dosplex/bin/dosplex repair` to complete or roll back the operation. An uninstall that stopped because Guest still had processes is finished by the same command, and Guest is kept.

A restart of the machine always returns to plain SteamOS.
