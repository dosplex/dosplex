# Using DosPlex

DosPlex gives two people their own Steam session on one screen. Play together,
play different games, or let one person watch a movie while the other plays.
This guide is the long form. [The manual](manual/README.md) is the short one, and the
same pages open from **Help** in the DosPlex menu. DosPlex 0.1.0 is an alpha: read
[the limits](manual/15-limits-of-the-alpha.md) first.

## Start and sign in

Connect your TV or monitor. Controllers are optional: DosPlex starts without
one, and a keyboard, a mouse or a generic gamepad works for Main. On a Steam Deck
you can also play undocked: with no external display connected, DosPlex starts on
the built-in screen (shown as 1280x800) and uses the TV mode you saved only when
the Deck is docked again. Started by hand with `dosplex start`, Main uses one
half and Guest uses the other; `dosplex start --layout single` gives Main the
full screen instead. Each session
uses Steam's own interface and sign-in, including QR code and Steam Guard.
Steam may ask for language or other first-run setup before showing sign-in.
You can leave Guest signed out while using Main.

Starting at boot is experimental. To have DosPlex start by itself, run
`sudo dosplex autostart on --experimental` once. From then
on the device starts DosPlex at every boot with Main alone on the full screen,
and you open a split for Guest from the menu when you want one. If it cannot
start (the device is in Desktop mode, Steam is busy for five minutes, or
something fails), you get stock Steam as before. The installation guide has
the details under "Start at boot".

Each person needs their own Steam account and access to the game they want to
play. DosPlex does not change Steam's game-ownership or online-play requirements.

To start DosPlex with a controller, open **DosPlex** in Steam's library and
press **Play**. You can also start it from the DosPlex icon on the desktop. An install does not start it by itself unless you add `--start`. The installer adds it to Steam's library by itself while Steam is
running. If it says it could not, run `sudo /opt/dosplex/bin/dosplex entry on`
with Steam running, or add it once by hand: in Desktop mode, in Steam, choose
**Games**, then **Add a Non-Steam Game to My Library**, and tick **DosPlex**.

## Open your menu

On a Steam Controller:

1. Hold **Select / Back / View** first.
2. While holding it, press **D-pad Up**. Hold both for about one second.
3. Release both buttons, then use the D-pad or stick to move, **A / Cross** to
   select, and **B / Circle** to go back or close.

The chord works on the Steam Deck's own controls too: hold **View** (the left
button of the pair under the screen), press **D-pad Up**, and hold both for
about one second. If you set the chord to use **Menu** instead of View, hold
Menu there. A hold that ends sooner than the configured time (one second by
default) does not open the menu.

Other gamepads, such as a DualShock 4, a DualSense, an Xbox or a Switch Pro
controller, can open and drive the menu of the player they belong to with the
same chord. This is experimental. DosPlex cannot filter these controllers, so
the chord also reaches the game, and the player's side is paused while the menu
is open because of one of them (picture and sound stand still, an online game
may drop its connection). It continues when the menu closes.
`sudo touch /var/lib/dosplex/gamepad-menu-off` and a restart of DosPlex switch
this off. A Steam Controller, the Deck's own controls and a keyboard open the
menu without any of that.

Your game always gets **Select / Back / View** straight away, exactly as you press
it, so a game that uses it for its own menu works as usual. DosPlex holds back
only the second button of the chord, and only while the first is down. If you let
go before the hold ends, the game gets that button right then, so a quick
Select + D-pad Up still reaches it. D-pad Up on its own is never delayed. When
the menu opens, the game has already seen Select; DosPlex then releases it for
you so the game does not think it is stuck. The other chords work the same way:
with **D-pad Up + Menu** the first button you press reaches the game and the
second is held back, and the same for **Select + Menu**.

On an assigned keyboard, use **Ctrl+Shift+M** or **Super+Shift+M**, then arrows,
Enter and Escape. Each person's menu controls their own session. Main also has
shared layout and session controls; actions affecting both people warn first.

Only one menu is open at a time. When Main opens the menu while Guest's is open,
Main's replaces it and anything Guest was about to confirm is cancelled. Guest
cannot open the menu while Main's is open. In full screen only Main has one, and in Guest full screen both of you do (Main's opens over Guest's picture).

## Help in the menu

The top row of your menu has **?** beside **X**.

**?** opens the manual at the chapter for the page you are on. Up and down
read, left and right change the chapter, and **B** closes it. It covers only
your side of the screen and runs only while it is open.

This release has no feedback form: DosPlex sends nothing by itself. The manual's
chapter [Tell us how it went](manual/16-tell-us-how-it-went.md) says where to
write to us.

## Choose a picture size

Open **Display** in your menu.

| Setting | What it does |
| --- | --- |
| Native | Gives your game the pixels in your allocated part of the screen. |
| 1:1 | Gives the game the largest square resolution that fits your part of the screen. |
| 4:3 | Gives the game the largest 4:3 resolution that fits your part of the screen. |
| 16:9 | Gives the game the largest 16:9 resolution that fits your part of the screen. |
| Keep ratio | Fits that picture into your space, adding bars when needed. |
| Stretch | Fills your space with the game's picture, changing its proportions. Steam's own menus always keep their proportions. |
| Fit | Crops the picture to the screen's shape and fills it. For games that show black bars. Nothing is stretched, and the edges of the picture that do not fit are hidden. DosPlex never picks it for you. |

Native is always the first button. The 1:1, 4:3 and 16:9 buttons follow it,
from the one that fills your part of the screen most to the one that fills it
least in your current layout. Each shows the size it gives, for example
**1:1 1920x1920**. These are the sizes in the two split layouts, Side by side
(a vertical split) and Top / bottom (a horizontal split), on 4K and 1080p
outputs:

| Setting | Side by side, 4K | Side by side, 1080p | Top / bottom, 4K | Top / bottom, 1080p |
| --- | --- | --- | --- | --- |
| Native | 1920×2160 | 960×1080 | 3840×1080 | 1920×540 |
| 1:1 | 1920×1920 | 960×960 | 1080×1080 | 540×540 |
| 4:3 | 1920×1440 | 960×720 | 1440×1080 | 720×540 |
| 16:9 | 1920×1080 | 960×540 | 1920×1080 | 960×540 |

In full screen your part is the whole output. On 4K, Native and 16:9 are
3840×2160, 4:3 is 2880×2160 and 1:1 is 2160×2160. DosPlex recalculates the
largest fitting size when you change the layout. The overlay shows the selected
size. A 4K-capable TV does not by itself mean the host is sending 4K; check the
size displayed in DosPlex.

**Steam's own resolution settings work in each part of the screen.** Steam in
each side has the settings of SteamOS: Settings → Display → Maximum Game
Resolution for all games, and the game's Properties → General → Game
Resolution for one game. Steam's list starts from your part of the screen
(Native is that size) and offers smaller sizes. A smaller size makes a heavy
game lighter in a split; Gamescope scales it up to your part of the screen.
Like on SteamOS, Steam recommends a size and uses it until you choose another,
so pick Native there if you want the full size of your part. DosPlex does not
change these saved Steam preferences. For a compatibility check, first use Steam's Default
setting and test DosPlex Native, 1:1, 4:3 and 16:9. Then repeat with a custom Steam
resolution and record both settings in your notes. A pass with a Steam override
does not show that the game works at the DosPlex Native mode.

**A game window that can be resized fills the game monitor.** DosPlex resizes it
to the size of the game monitor when the game starts. A game that remembers an
earlier window size is still resized to fit. A game whose window has a fixed
size is left alone: it keeps the size it asks for, and **Keep ratio** or
**Stretch** decides how that picture fills your space.

**A game keeps the game monitor it started with.** Not every game copes with its
monitor changing while it runs, so DosPlex does not change your game monitor
while a game is running in your session. If you change the layout, or press
**Apply** for another picture size, Steam's interface and every game you start
afterwards use the new size. The game that is running keeps the monitor it
started with, and Gamescope scales its picture into your new area: **Keep ratio**
adds bars when the shapes differ, and **Stretch** stretches
the picture; **Fit** crops it to your area's shape, which helps when
the game shows black bars. You choose Fit yourself with **Apply**;
DosPlex never does. With Fit the overlay says **Fitted**,
with Keep ratio or Stretch it says **Scaled**. To make the running game fill the
new area, change its resolution in its own video settings, or close it and start
it again. When no game is running any more, DosPlex applies your latest choice to
the game monitor by itself.

**DosPlex holds Steam's idle sleep while two players share the screen.** The machine
then does not go to sleep by itself; use **Sleep** on Main's **Session** page or the
power button. Steam's screensaver still comes on a side nobody uses, and about once
an hour that side's picture comes back for five minutes. With Main alone
on the screen Steam idles and sleeps the machine as usual.

**DosPlex wakes an idle Steam when a game starts.** If nobody has used your
session for a while, Steam shows its screensaver, and a game that starts then can
stay behind Steam's window, so you see nothing. If Steam is still in front of the
game three seconds after it started, DosPlex sends one Escape key press to your
Steam to wake it. It does this once for each game that starts, and not when the
game already has the screen.

**A game can still remember its own earlier resolution.** If you see a
widescreen picture with Native selected, check the game's video settings. Select
your half-screen resolution if it is listed, and check the game's fullscreen
mode. Some games omit unusual resolutions even when they detect the monitor
correctly. Report this in your game-test notes. Some games need a relaunch.

**The default is Native in every layout.** A game gets every pixel of your part
of the screen: 1920×2160 in Side by side on a 4K TV. That part is a portrait
shape, and a game that does not cope with it (it does not start, shows bars or
looks wrong) needs **16:9**, **4:3** or **1:1** from the menu. A game saves the
size it ran at, so after you play in a tall half it may need its resolution set
again in Full screen. A choice that you **Apply** for your part stays in every
layout. Steam's own interface is not scaled by Gamescope, so with a 16:9 monitor
in a portrait part it sits in the middle of your part with bars above and below.
That is accepted; Native lets Steam's interface fill your part.

If a game will not start with the default (for example, it shows a Direct3D or
display error), try the other choices with **Keep ratio**: Native, and the
buttons after it, which keep the most of your part of the screen first. Side by
side, try **1:1**, then **4:3**, then **16:9**. In Top / bottom and full screen,
try **16:9**, then **4:3**, then **1:1**. **Apply** changes your game monitor as
soon as no game is running. A game window that can be resized follows it; a game
with a fixed window size, or one that does not redraw at the new size, may need
to be relaunched.
Report the mode that worked and the Steam Game Resolution setting.

**Your menu says whether your picture is scaled.** The line under the menu's
title gives the size of your part of the screen and the layout. While Steam or a
game is on your side (not in Desktop mode), it ends with **Native** when the
picture is shown at the size of its monitor, or with **Scaled (game at
3840×2160)** when it is not. A game keeps the monitor it started with, so a
layout change while a game runs scales its picture into your part of the screen.
Once for each such change, your menu shows "Scaled: game at 3840x2160. Fit to
screen fills the screen if the game shows black bars; relaunch the game to use the
whole area." for a few seconds in place of that line, and the Display tab says it
for as long as it lasts. With Fit chosen it says "Fitted:
game at 3840x2160. Keep ratio shows the whole picture; relaunch the game to
use the whole area." To fill your part again, change
the game's resolution in the game, or relaunch it.

In the development build, DosPlex also corrects Proton's generated mode list for
portrait Native monitors. If a game is being played for the first time, its
Proton prefix may not exist until that game exits. DosPlex repairs the prefix
after exit and briefly shows a notice in that player's menu; reopen the game to
use the corrected list. This portrait fix is still under hardware and game
acceptance testing.

## Choose your controls

Open **Devices** to see connected controls and their owner. The first controller is Main's. While two players share the screen and the second player has no controller, the next controller that wakes goes to the second player by itself. On a Steam Deck the built-in controls are Main's, so the first other controller is the second player's. You can change who has which controller at any time in the menu, under Devices, and DosPlex remembers what you chose. A controller that appears never changes the
layout, and DosPlex needs no controller to start. A controller you named for
Guest when you installed or started DosPlex starts with Guest. You can release a
controller (it is then Unassigned) or give it to Guest, from the overlay or with
`dosplex control assign`, and DosPlex remembers your choice across a stop and a
reboot.

Each controller has a small number that stays with it: the Devices page shows it
beside the name, so you can tell controller 1 from controller 2. A number is
never changed because another controller went away, and a controller you have
not used before gets the smallest number nobody has.

To drop a controller you no longer use, turn it off or unplug it, then choose
**Forget** on its row in Main's Devices page and confirm (or run
`dosplex control forget`). Its number is free for the next new controller. A
controller that is connected cannot be forgotten: release it to Unassigned
instead. Forgetting does not remove a Bluetooth pairing.

- **Claim:** choose **Claim** on an Unassigned controller that is awake. It is
  added to your side. You can hold several, and Steam on your side sees each as
  its own controller. Your bindings stay in your own Steam account. Guest lists
  the free controllers and its own, and claims or releases at once. Main sees
  every controller and uses the **Main**, **Guest** and **Unassigned** buttons on
  its row; check the result, then confirm with **Keep assignment**, or use Undo. A move that you do
  not confirm within 15 seconds is undone by itself.
- **Release:** choose **Release** on a controller you hold, and it is Unassigned
  again for anybody to claim. If it was Guest's last controller, Guest's menu
  closes, because nothing is left to operate it; Main can give Guest a
  controller again.
- **Swap controls:** Main can exchange its controller with Guest's. If either of
  you holds several, it swaps the one each of you pressed last. When it cannot
  tell, it says "Press a button on the controller to swap": press a button on
  the controller you mean, then choose **Swap controls** again.

A game that was already running when you claimed a controller may need a restart
to see the new one. A layout never moves a controller: in **Full screen** and in
**Guest full screen** each controller keeps its owner, as in a split, and an
Unassigned one stays Unassigned. The one thing that is different in Full screen
is that Main's menu chord opens Main's menu from every controller that has an
owner, Guest's included.

The second page of **Devices** has a **Mouse**, a **Keyboard** and, on a
device with one, a **Touchscreen** row. Each covers every connected device of
that kind together and starts with Main; choose **Guest** to hand it over. The
mouse moves the cursor and clicks in the Steam of whoever it belongs to. Choose
**Shared** on the Mouse or the Keyboard to use it for both of you: a shared mouse
crosses the middle of the screen and clicks in the side under it, and a shared
keyboard types in the side where the mouse is. Only Main assigns these, and the
Touchscreen row stays Main or Guest.

A Steam Controller's trackpad mouse and its keyboard emulation work inside your
own session and move your own cursor; the other player's pointer is separate.

**Click a pop-up with your controller.** An installer or licence window in your
Steam can be answered with your own controller, as on a Steam Deck. This is
Steam's own chord and it works only in your side. Hold **Steam**, then:

| Input | What it does |
| --- | --- |
| Right trackpad or right stick | Moves the pointer |
| Right trackpad press, or **R2** | Left click |
| **L2** | Right click |
| D-pad right, left, down | Enter, Escape, Tab |
| Left stick | Volume |

The left trackpad does nothing in the chord, and the button below Steam (Quick
Access) is not part of it. If a pop-up still cannot be reached, check whether
Steam's "Steam Controller detected!" tour is on; it holds the input until it is
switched off (see [Current limitations](limitations.md)). Clicking with a real
controller in a DosPlex side has not been checked on a device yet.

Main's last available menu control cannot be handed away by an ordinary transfer.
Moving Main to the other screen position is separate from assigning a controller.
Wake a sleeping controller before trying to move it between players.

## Choose your sound

The first time DosPlex runs, Main gets the output your system already uses (the
TV, for example), so you hear sound without opening the menu. Guest gets no
output until Guest or Main chooses one.

Open **Audio** to change that: claim a real output (TV/HDMI, speakers or a
headset). Each person can claim a separate microphone too. Release a device
before the other person claims it. A device already claimed by someone else is
reserved for them.

An output you claim stays yours across restarts and reboots until you release it.
If the device is not connected at that moment, for example a headset that is
switched off, the claim stays and shows as disconnected, and the sound starts when
the device appears. DosPlex never plays your sound on another device in the
meantime. Microphone claims are not kept; claim the microphone again after a
restart.

For shared speakers, Main chooses a shared output and one of two mixes:

- **Full mix:** hear both sessions through both speakers.
- **Positioned stereo mix:** hear more of the left screen from the left speaker,
  and more of the right screen from the right speaker. Choose Gentle, Balanced
  or Strong separation. The balance follows the screen positions.

Anyone who has not claimed an output of their own, usually Guest, joins the
shared speakers automatically when Main starts or changes the mix. If you have
claimed one, such as a headset, you keep it until you press **Join shared
speakers** in your Audio menu. **Use own output** takes you out of the mix, and
you stay out when Main changes it, until you join again or Main stops sharing.

Positioned stereo mix provides a speaker balance; the two people can still hear
each other's audio. Headsets provide separate listening. Check the selected
output and volume if a game is silent.

Guest's own Steam shows just one output, **DosPlex sound**, and one microphone,
**DosPlex microphone**. Choose the real devices in the DosPlex Audio menu; Steam's
own audio settings cannot change them.

## Take the whole screen, then return to two people

Main can choose full screen and decide what happens to Guest:

- **Standby:** pauses the existing session in memory. Splitting again resumes it.
- **Off:** closes the session after confirmation. Save progress first. Splitting again starts
  Guest fresh at Steam's own sign-in or onboarding screen.

When the screen is split, both sessions always run. There is no separate on/off control.

Main can also give Guest the whole screen with **Guest full screen** on the Layout row. Your session goes to Standby
and Guest's picture fills the screen. Use it when you quit and Guest keeps playing or watching. Guest cannot ask for
it, and Guest cannot change the layout. You can come back at any time: open your overlay with your usual keys or
controller chord, then choose **Full screen**, **Side by side** or **Top / bottom**. Your session wakes from Standby.
Your overlay is drawn over Guest's picture while it is open, and your keyboard and controllers keep their owners.
Choosing **Full screen** from Guest full screen puts Guest in Standby.

Standby is experimental. It keeps using memory, does not survive power loss,
and online games can disconnect. Save normally before relying on it.

**Reset my side** closes and reopens your session. Main can reset both with a
warning. Steam's own menu provides Desktop mode; DosPlex's overlay stays
available there too.

Main's **Session** page has **Turn off DosPlex**. It asks first. Then it asks Steam to quit on both sides, as
Restart does, stops both sessions and DosPlex, and the screen returns to normal SteamOS. The machine stays on.
Both players' games close, so save first. If something goes wrong the screen returns to Main's menu on the
Session page with a message. It has not been shown to work on a Steam Deck or a Steam Machine yet.

Main's **Session** page also has machine power:

- **Sleep** suspends the machine. Both sessions stay open and resume. Just before the machine goes to sleep DosPlex
  pauses both sessions, and when it wakes it lets them run again. Without that pause a controller read that is still
  waiting can keep the machine from going to sleep. A session that was in Standby stays in Standby.
- **Restart** and **Shut down** ask Steam to quit on both sides (the way
  SteamOS itself closes it for a session switch) and wait up to 45 seconds for
  each, so that it can finish syncing saves to the cloud. They then stop
  both sessions and DosPlex, and restart or turn off the machine. A Steam that
  does not quit in time is stopped. Save first all the same. After a restart
  the machine comes back as normal SteamOS, unless you turned on `sudo dosplex
  autostart on`, which starts DosPlex again at boot; if not, run
  `sudo dosplex start` to bring DosPlex back. If something goes wrong the screen returns to Main's menu on the
  Session page with a message, and both players' controls work again.

These three have not been shown to work on a Steam Deck or a Steam Machine yet.
Until they have, save and quit your games before you use them. If a controller
does not answer after the machine woke up, use **Reset my side**.

## Set the screen brightness

The built-in screen is shared, so Main sets its brightness. Open Main's menu, stay
on the **Display** tab and look under the FPS counter: **Dimmer** and
**Brighter** change the brightness in steps of 5 %, and the line above them shows
the current value. The lowest value is 5 %, so the screen never goes black. The
control appears only in Main's menu, and only on a machine with a built-in screen
backlight (the Steam Deck). Guest's menu has no brightness control, and a Steam
Machine or a monitor does not show it. On a short menu (a screen split into two
rows on a small screen) the control takes the place of the FPS counter's buttons.

Steam's own brightness slider in the Quick Access Menu does not work inside a
side: it never reaches the panel. Use Main's menu instead.

DosPlex remembers the value across restarts and puts it back when it starts. When
DosPlex stops it leaves the screen as you set it. A controller or script can set
it too: `sudo dosplex control brightness 70`.

## Performance overlay and performance logs

Steam's Performance Overlay works in each side's Gaming mode. Open Steam's Quick
Access Menu, choose **Performance** and pick an overlay level from 0 to 4. The
level you pick shows only in your side. The other side keeps its own level, and
its numbers are its own. Desktop mode has no overlay.

To keep the numbers as files for performance testing, turn logging on for a side:

```bash
mkdir -p ~/.local/share/dosplex/perf
touch ~/.local/share/dosplex/perf/enabled
```

Run these as the account of the side (Main or Guest), then start the side again.
Remove the `enabled` file to turn logging off. While it exists, the side writes
MangoHud CSV files (frame time, FPS, and the CPU and GPU load, temperatures and
power MangoHud can read) to `~/.local/share/dosplex/perf/`, every 100 ms. Each
side writes to the home folder of its own account. To read Guest's files as
Main, copy them from Guest's folder with the rights the machine gives you.

Set `DOSPLEX_PERF_OVERLAY=0` in the side's environment to start it without the
overlay. Keep the overlay at level 1 or higher while you measure: on current
SteamOS Gamescope stops its mangoapp a few seconds after the overlay is hidden
(level 0), and a stopped mangoapp writes no log.

### DosPlex's own FPS counter and recording

DosPlex can show and record the frame rate of each side itself, without Steam's
overlay. Open your menu, stay on the **Display** tab and look under the picture
buttons:

- **FPS counter on this side:** **Off**, **FPS**, or **FPS + frame time**. It
  takes effect at once, needs no **Apply**, and does not restart anything. It is
  off by default. Each side has its own setting, and DosPlex remembers it
  across restarts.
- **Record performance:** starts and stops a CSV recording for your side.
  Recording is off after every start of DosPlex.

The counter sits in the top-left corner of your side, above the game and below
your menu. DosPlex draws it, not the game, so it takes no input and does not
change the game's frames. Its size follows the side: about 15 px text on a
1280x800 screen and 45 px on a 3840x2160 output. It updates once a second:

- **FPS:** frames delivered in the last second.
- **FPS + frame time:** also the average frame time over that second and the
  99th-percentile frame time ("p99") over the last five seconds. A p99 far above
  the average means stutter.

For scripts, `FPS <left|right> <off|fps|fps+frametime>` and
`RECORD <left|right> <on|off>` on DosPlex's control socket do what the buttons do.

**Where the files go.** `/var/lib/dosplex/perf/`, owned by root and readable by
everyone. Root owns the folder so a side's account cannot redirect or change the
files. Each recording is one file named
`perf-<date>-<time>-<main|guest>.csv`, and DosPlex keeps the newest 20.
`fps-counter.conf` in the same folder holds the counter settings. To take the
files off a Deck, use `scp` as a normal user.
An uninstall never deletes this folder while it holds recordings.

**Columns.** One row a second:

| Column | Meaning |
| --- | --- |
| `timestamp_ms` | Wall clock, milliseconds since 1970 |
| `side` | `main` or `guest` |
| `fps` | Frames delivered in the last second |
| `frametime_ms_avg` | Average time between those frames |
| `frametime_ms_p99` | 99th-percentile time between those frames (nearest rank) |
| `cpu_load_pct` | Whole-machine CPU load since the last row (`/proc/stat`) |
| `gpu_busy_pct` | `gpu_busy_percent` of the AMD GPU |
| `cpu_temp_c` | `k10temp` (or `coretemp`) `temp1_input` |
| `gpu_temp_c` | `amdgpu` `temp1_input` |
| `power_w` | `amdgpu` `power1_average`; if it has none, the battery's `power_now` |
| `battery_pct` | Battery `capacity`, if the machine has a battery |
| `gpu_sclk_mhz` | The GPU clock in use (`pp_dpm_sclk`) |
| `cpu_mhz` | Average of the cores' `scaling_cur_freq` |

The `cpu_`, `gpu_`, `power_` and `battery_` columns describe the whole machine
and read the same in the Main and Guest files of the same second. A sensor the
machine does not have leaves its field empty. The `fps` and frame time columns
are about one side only. What they measure is in
[Current limitations](limitations.md#dosplexs-own-fps-counter).

## Desktop mode

Steam's own menu offers Desktop mode. It opens a KDE Plasma desktop in your part
of the screen while the other person's session keeps running, and **Return to
Gaming Mode** on the desktop brings your Steam back.

**The desktop opens at a readable scale.** DosPlex chooses it from the size of
your part of the screen when Desktop mode starts: 200 % when your part is at
least 1600 pixels tall or 3000 pixels wide (a half or the whole of a 4K screen),
150 % when it is at least 1000 pixels tall (the whole of a 1080p screen, or its
left or right half) and 100 % on anything smaller. Changing the layout while the
desktop is open does not change the scale.

**Your desktop is kept.** Main and Guest each have a desktop of their own. Its
settings stay in the folder `~/.local/share/dosplex/desktop` in that player's own
home folder and are there again the next time Desktop mode opens.
If you used this under the old name, DuplOS, that folder was `~/.local/share/duplos`; the first start
of DosPlex moves it, with your chosen scale, to the new place (see
[Upgrading from DuplOS](#upgrading-from-duplos)).

**Flatpak apps are there too.** Apps installed as Flatpaks, for the whole machine
or for your own account, appear in the application menu and on the command line
(under their application ID, such as `org.videolan.VLC`), as on SteamOS's own
desktop.

**To choose another scale,** write one of 1, 1.25, 1.5, 1.75, 2, 2.5 or 3 (1 is
100 %, 2 is 200 %) into the file `~/.local/share/dosplex/desktop/desktop-scale`,
for example from a terminal in Desktop mode, and open Desktop mode again:

```bash
echo 1.5 > ~/.local/share/dosplex/desktop/desktop-scale
```

The choice is kept for your side with the rest of your desktop. Anything else in
the file is ignored, and deleting the file returns to the scale DosPlex chooses
from the size. The overlay does not offer this setting. KDE's own display
scale setting has no effect in this desktop (see
[Current limitations](limitations.md)).

## Upgrading from DuplOS

DosPlex was called DuplOS until October 2026. A device has one or the other, never
both: installing DosPlex over a DuplOS install replaces it. If DuplOS is running,
the install stops it first (your games end, so save them), keeps your Guest
account and its Steam library, the controller choices and the sound and display
settings, and removes the old `/opt/duplos` program. You do not need to uninstall
DuplOS yourself. If the install cannot stop the running session, or an earlier
DuplOS operation was interrupted, it says which command to run and changes
nothing.

Your own files move the first time your side starts under the new name: the
Desktop folder `~/.local/share/duplos` (with the scale you chose) becomes
`~/.local/share/dosplex`, and the workspace folder `~/DuplOS` becomes `~/Dosplex`.
A folder is moved only when the new name is free. Nothing is merged or
overwritten; if both exist, the old one stays where it is and the launch log
says so. The DuplOS `update` command cannot find DosPlex releases, so install
the first DosPlex release by hand.

## Help us improve compatibility

Use the [short game-test template](game-test-notes.md). Record which side you
used, controller, resolution, Proton version and what failed. A useful report
says what you did, what you expected, and what happened. Never include passwords,
Steam Guard codes or sign-in QR images.
