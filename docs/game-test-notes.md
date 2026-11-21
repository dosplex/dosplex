# Game testing notes

Use a new entry for each game, account side and display setting. A game reaching
its menu is a launch pass; mark gameplay separately. Keep failed attempts too.
Do not include account passwords, Steam Guard codes or QR screenshots.

## Quick sequence

1. Launch the game on Main. Check menus, movement, camera and the usual buttons.
2. Open Dosplex: hold **Select/Back first**, add **D-pad Up**, hold about one second,
   then release both. Check that the game stops receiving input while the menu
   is open and responds immediately after closing it.
3. Set Steam Properties → General → Game Resolution to Default. Try Dosplex
   Native, 1:1, 4:3 and 16:9 with Keep ratio. Check the game's own resolution
   setting; it may retain an earlier setting or need a relaunch. If you also
   test a custom Steam resolution, record it as a separate run.
4. Repeat on Guest. Run two games together and confirm each controller and
   audio claim reaches its own account. Test both overlays during activity.
5. Move Main to the other position. Try Full mix, then Positioned stereo mix
   at Gentle/Balanced/Strong. Confirm the speaker bias follows screen position.
6. After saving progress, check single screen with Guest in Standby, then split
   again. Test Reset separately; it closes that session's running applications.

## Copy for each result

```text
Date / Dosplex build:
Device / SteamOS version:
TV output resolution / refresh rate:
Steam Properties → General → Game Resolution: Default / custom (value):
Game / Steam app ID / game build:
Native Linux or Proton version:
Main or Guest / other activity running:
Dosplex layout, picture choice (Native / 1:1 / 4:3 / 16:9) and virtual resolution:
Keep ratio, Stretch or Fit (Fit crops the picture to your area's shape, for a
game that shows black bars; you pick it yourself on the Display page and Dosplex never chooses it, and the overlay
then says Fitted):
Game resolution / windowed, borderless or exclusive fullscreen:
Controller model / USB, receiver or Bluetooth / Steam Input template:
Audio device / Exclusive, Full mix or Positioned stereo mix strength:

Launch: pass / fail / untested
Menus and gameplay: pass / fail / untested; minutes played:
Overlay opening, navigation and immediate return: pass / fail / untested
Other player's input and audio stay independent: pass / fail / untested
Resolution change and side swap: pass / fail / untested
Standby and resume: pass / fail / untested
Symptoms / exact reproduction steps:
Workaround / whether game relaunch was necessary:
```

Native means the pixels allocated to that screen region: a vertical split of a
4K output gives each player 1920×2160; a 1080p output gives 960×1080; a
horizontal (top/bottom) split gives 3840×1080 or 1920×540. The 1:1, 4:3 and
16:9 choices select the largest square, 4:3 or 16:9 size that fits the region;
in a 4K vertical split they are 1920×1920, 1920×1440 and 1920×1080. Some games
choose a different internal resolution. Steam's per-game Game Resolution is a
separate variable and can limit the modes presented to a game. Record the
Dosplex choice, Steam setting, virtual monitor and in-game setting separately.
