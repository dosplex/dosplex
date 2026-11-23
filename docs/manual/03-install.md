# Install

Work in Desktop mode. Open a terminal and follow these steps.

1. Switch to Desktop mode by pressing the Steam button, choosing **Power**, then **Switch to Desktop**.
2. Set a password for the `deck` user if you have not already: run `passwd` in the terminal. The installer needs `sudo`.
3. Put the release files in a folder. A signed release is an archive (`dosplex-VERSION.tar.gz`) and its signature file. Take the `dosplex` program out of the archive (`tar -xzf dosplex-VERSION.tar.gz dosplex-VERSION/dosplex`) and compare its SHA-256 (`sha256sum dosplex-VERSION/dosplex`) with the one on the release page. That hash is the one thing you check by hand; the program checks everything else itself.
4. Install. Run `sudo ./dosplex-VERSION/dosplex install --release dosplex-VERSION.tar.gz`. It checks your device first (SteamOS, Gamescope and Steam client versions, free space) and changes nothing when a check fails. Then it checks the signature and every file, creates the `guest` account and copies the release to `/opt/dosplex/`. It prints one line for the checks that passed and the whole line for each one that did not.
5. Nothing starts by itself. Start DosPlex in Gaming mode from Steam's library (**Library**, **Non-Steam**, **DosPlex**), or from the DosPlex icon on the desktop. Add `--start` to the install command if you want it to start right after the install. Starting switches the device to Gaming mode, so save any desktop work first.

No controller has to be connected or awake for the install. The first controller is Main's. While two players share the screen and the second player has no controller, the next controller that wakes goes to the second player by itself. On a Steam Deck the built-in controls are Main's, so the first other controller is the second player's. You can change who has which controller at any time in the menu, under **Devices**, and DosPlex remembers what you chose.

If you want to look at your device without installing, `sudo ./dosplex-VERSION/dosplex preflight --release dosplex-VERSION.tar.gz` lists every check and changes nothing.

## If the installer says a version is not in the tested set

A release lists the SteamOS, Gamescope and Steam client versions it was tested on, and the installer stops when yours is another one. Steam updates its own client often, so this is likely for the Steam client, and it is the least risky of the three. If only the Steam client is named, install with `--allow-untested` added to the command:

```bash
sudo ./dosplex-VERSION/dosplex install --release dosplex-VERSION.tar.gz --allow-untested
```

If SteamOS or Gamescope is named, a newer release of DosPlex may list your version: look at the release page first. You can go on with `--allow-untested` there too, at your own risk; a restart of the machine brings plain SteamOS back whatever happens.

Once DosPlex is installed, a later update of the Steam client does not stop it from starting from Steam's library. `dosplex status` then shows a warning with the version. After an update of SteamOS itself, DosPlex refuses to start until you have installed a release that lists the new version, or have started it once in Desktop mode with `sudo /opt/dosplex/bin/dosplex start --allow-untested`.

## Add DosPlex to Steam's library

The installer leaves an application called **DosPlex** in your account and, while Steam is running, adds it to Steam's library by itself. You then find it in Gaming mode under **Non-Steam**, and you start DosPlex with a controller. The installer says "DosPlex is in Steam's library" when it worked.

If it says it could not add DosPlex (Steam was not running, for example), start Steam and run `sudo /opt/dosplex/bin/dosplex entry on`: it tries again. Or add it by hand, once:

1. Stay in Desktop mode and open Steam.
2. In Steam's window choose **Games**, then **Add a Non-Steam Game to My Library**.
3. Tick **DosPlex** in the list and choose **Add Selected Programs**.
4. Go back to Gaming mode. DosPlex is in your library under **Non-Steam**.

You can check that the entry is ready, and whether Steam's library has it, with `sudo /opt/dosplex/bin/dosplex entry status`. If you do not want it, `sudo /opt/dosplex/bin/dosplex entry off` removes the application and the small service behind it; remove the shortcut from Steam yourself.

The installer records what it changes in a journal under `/var/lib/dosplex/journal/`. It keeps one earlier release alongside the current one, so you can roll back.
