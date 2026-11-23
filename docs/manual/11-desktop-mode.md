# Desktop mode

Steam's own **Power** menu offers Desktop mode. It opens a KDE Plasma desktop in your part of the screen while the other person's session keeps running, and **Return to Gaming Mode** on the desktop brings your Steam back.

**Your desktop is your own.** Main and Guest each have a desktop of their own. Its settings stay in the folder `~/.local/share/dosplex/desktop` in that player's own home folder, and they are there again the next time Desktop mode opens.

**The desktop opens at a readable scale.** DosPlex chooses it from the size of your part of the screen when Desktop mode starts: 200 % when your part is at least 1600 pixels tall or 3000 pixels wide (a half or the whole of a 4K screen), 150 % when it is at least 1000 pixels tall (the whole of a 1080p screen, or its left or right half) and 100 % on anything smaller. Changing the layout while the desktop is open does not change the scale.

**To choose another scale,** write one of 1, 1.25, 1.5, 1.75, 2, 2.5 or 3 (1 is 100 %, 2 is 200 %) into the file `~/.local/share/dosplex/desktop/desktop-scale`, for example from a terminal in Desktop mode, and open Desktop mode again. The choice is kept for your side with the rest of your desktop. Anything else in the file is ignored, and deleting the file returns to the scale DosPlex chooses from the size. The overlay does not offer this setting, and KDE's own display scale setting has no effect in this desktop.

**Flatpak apps are there too.** Apps installed as Flatpaks, for the whole machine or for your own account, appear in the application menu and on the command line, as on SteamOS's own desktop.

**To open it,** press the **Steam** button on your side, choose **Power**, then **Switch to Desktop**, as on SteamOS without DosPlex. DosPlex's menu stays available in Desktop mode.
