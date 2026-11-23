# Before you install

You need:

- A Steam Machine, or a Steam Deck docked to a TV or monitor.
- Two controllers and two Steam accounts. Each person plays their own games.
- An administrator password. The `deck` user has none by default: in Desktop mode, run `passwd`.

The install writes only under `/opt/dosplex` and `/var/lib/dosplex`. It does not touch `/usr`, the read-only system or the display manager. It creates a `guest` account for the second player.

> [!NOTE]
> Nothing runs at boot unless you turn on starting at boot, which is experimental. A restart brings back plain SteamOS.

The shared game library is experimental and off: see [Experimental](17-experimental.md).

More: [the installation guide](../steamos-installation.md#what-you-need).
