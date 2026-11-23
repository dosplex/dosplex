# What DosPlex is

Two players, one SteamOS machine, one screen. DosPlex splits the screen so each player has their own side: their own Steam account, their own library and saves, their own controllers and their own sound. You start it from Steam's library and use a controller for everything.

## What alpha means here

The first public release is an alpha, and the word does real work:

- **It starts only when you start it.** Nothing runs at boot. You start DosPlex from Steam's library with a controller, and a restart of the machine always brings back plain SteamOS. If DosPlex itself breaks, a reboot puts the machine back the way it was.
- **Some things are missing, and some are rough.** The shared game library and starting at boot are in the program but off, and marked experimental. Steam's own settings pages for Wi-Fi, power, updates and storage do not work inside a side. Limits that a user will meet are listed in their own chapter of this manual.
- **Your feedback is what improves it.** DosPlex sends nothing by itself and this release has no feedback form, so you tell us on Discord or GitHub. The chapter "Tell us how it went" says where.

## What it runs on

- A Steam Machine, or a Steam Deck that is docked to a TV or monitor. A Deck can also use its own screen.
- A TV or monitor, connected and switched on.
- Two Steam accounts and two controllers. Each person uses their own account and plays their own games.
- The SteamOS, Gamescope and Steam client versions the release was tested on, listed in the release notes. A version that is not in the tested set is refused unless you say so.

An alpha that cannot outlive a reboot cannot leave your machine in a state you cannot leave. That is the point of the design.
