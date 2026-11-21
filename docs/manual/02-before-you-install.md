# Before you install

Dosplex runs on a Steam Machine, or on a Steam Deck docked to a TV or monitor. You need two controllers and two Steam accounts. Each person uses their own account and plays their own games. The shared game library is experimental and off unless you turn it on (see the chapter on experimental features): both sides then see the same library, but each person must own the games they play.

You also need the administrator password you set in Desktop mode. The `deck` user has no password by default, and the installer needs it. Switch to Desktop mode (Steam button, **Power**, **Switch to Desktop**) and run `passwd` in a terminal to set one.

Dosplex leaves your SteamOS installation intact. It writes files only under `/opt/dosplex` and `/var/lib/dosplex`. It does not write under `/usr`, does not disable the read-only system, and does not change the display manager. It creates a `guest` account for the second player with a private home directory.

A restart of the machine always brings back plain SteamOS. Dosplex is hand-started: nothing runs at boot until you choose otherwise. Starting at boot is experimental and covered in a later chapter. If something goes wrong, a reboot returns the device to what it was before you installed.
