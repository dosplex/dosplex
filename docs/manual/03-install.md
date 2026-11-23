# Install

Work in Desktop mode, in a terminal.

1. Press <kbd>Steam</kbd>, choose **Power**, then **Switch to Desktop**.
2. Set a password for the `deck` user if it has none: run `passwd`.
3. Put the release archive and its signature file in one folder.
4. Take the program out of the archive: `tar -xzf dosplex-VERSION.tar.gz dosplex-VERSION/dosplex`.
5. Run `sha256sum dosplex-VERSION/dosplex` and compare the result with the SHA-256 on the release page.
6. Install: `sudo ./dosplex-VERSION/dosplex install --release dosplex-VERSION.tar.gz`.
7. In Gaming mode, start DosPlex from Steam's library (**Library**, **Non-Steam**, **DosPlex**), or from the DosPlex icon on the desktop.

The installer checks your device first and changes nothing when a check fails.

> [!WARNING]
> Starting DosPlex switches the device to Gaming mode. Save your desktop work first. With `--start` on the install command it starts right after the install.

To check your device without installing, run `sudo ./dosplex-VERSION/dosplex preflight --release dosplex-VERSION.tar.gz`.

## A version is not in the tested set

- Only the Steam client named: add `--allow-untested` to the install command.
- SteamOS or Gamescope named: look for a newer DosPlex release first. `--allow-untested` is at your own risk.

> [!NOTE]
> After a SteamOS update, DosPlex refuses to start until you install a release that lists the new version. Or start it once in Desktop mode with `sudo /opt/dosplex/bin/dosplex start --allow-untested`.

## Add DosPlex to Steam's library

The installer adds **DosPlex** to Steam's library while Steam runs. If it could not:

- Start Steam, then run `sudo /opt/dosplex/bin/dosplex entry on`.
- Or in Desktop mode, in Steam, choose **Games**, then **Add a Non-Steam Game to My Library**, tick **DosPlex** and choose **Add Selected Programs**.

`entry status` checks the entry. `entry off` removes it.

More: [the installation guide](../steamos-installation.md#install).
