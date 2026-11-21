# Limits of the alpha

These are the limits a user will meet, in plain words. Each says what does not work, and what to do instead where the installation documentation gives a way.

## The picture

- **No picture changes while a game is running.** Changing the layout or the picture size while a game runs does not move it: Gamescope scales the running picture into your area. Quit the game and start it again to make it fill the new area.
- **A game keeps its own resolution setting.** Dosplex sets the size of the monitor a game sees, and Steam's own resolution settings work inside that. What a game saved in its own video settings is the game's: one that saved the full screen's size may quit at once when you start it in a split. [When something is wrong](14-when-something-is-wrong.md) says what to do.
- **The Steam Deck's own screen shares one backlight.** If you run Dosplex on the Deck's built-in screen, both players share that screen's brightness. Main's menu is the only brightness control, and it is not there on a Steam Machine or an external monitor.
- **The frame-rate display has not been checked on a device yet.** Dosplex's own frame-rate counter and Steam's Performance Overlay have so far only been checked with stand-in programs, not on a Steam Deck or a Steam Machine.
- **Standby is experimental.** When a side goes to Standby it keeps using memory, does not survive a power loss, and online games can disconnect. Save before you rely on it.

## Controllers and sound

- **Only a Steam Controller or the Deck's own controls open the menu.** A DualShock 4, a DualSense, an Xbox or a Switch Pro controller plays games on its side but cannot open the Dosplex menu. A player who has only such a controller needs a keyboard, or the other player, to change their settings.
- **A controller that connects first belongs to Main.** Every controller you connect is Main's until someone assigns it. Main can give a controller to Guest; Guest can claim and release their own.
- **A game that was already running may not see a newly claimed controller.** Start the game after the controller is in place, or restart it once it is claimed.
- **Guest's own Steam shows one output and one microphone.** The real devices are chosen in the Dosplex Audio menu; Steam's own audio settings cannot change them.
- **A microphone you claim is not kept.** Output claims stay across restarts, but microphone claims do not. Claim the microphone again after a restart.

## Steam's own settings pages

Steam's own settings pages for Wi-Fi, power, updates and storage do not work inside a side. The installation documentation says what to do for each:

- **Wi-Fi and power.** Open Steam's Quick Access Menu on the side and use it there.
- **Updates.** Update the machine itself before you update Dosplex. Run the update from a plain shell:

  ```bash
  sudo /opt/dosplex/bin/dosplex update
  ```

- **Storage.** Use Desktop mode for Steam's storage page.

## Between the two players

Dosplex keeps the two players apart in ordinary use: each has their own account, Steam, games, saves, controllers and sound. It is not yet a wall against a player who sets out to cross it. Use the alpha only with people you trust at the same screen.

What is open today:

- **A program can draw over the other player's part of the screen.** A program started from one player's account can place a window over the other player's part and take that player's pointer. It does not get the other player's key presses.
- **The two sides share one network.** A program on one side can reach network services that a program on the other side opens on the machine itself.

What Main decides for both of you, by design:

- The layout, and who has the whole screen.
- Which controller, mouse or keyboard belongs to whom. Guest can claim and release their own controllers.
- The shared speakers and their mix. **Use own output** takes a player out of the shared mix.
- Turning Dosplex off, and Sleep, Restart and Shut down.
