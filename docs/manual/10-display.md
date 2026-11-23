# Display

Open **Display** in your menu. The tab shows the picture size choices, the brightness control (on the Deck) and the frame-rate counter.

## Brightness

The built-in screen is shared, so Main sets its brightness. The control appears only in Main's menu, and only on a machine with a built-in screen backlight (the Steam Deck). Guest's menu has no brightness control, and a Steam Machine or a monitor does not show it.

**Dimmer** and **Brighter** change the brightness in steps of 5 %, and the line above them shows the current value. The lowest value is 5 %, so the screen never goes black. Change it when the room gets brighter or darker. DosPlex remembers the value across restarts and puts it back when it starts. When DosPlex stops it leaves the screen as you set it.

Steam's own brightness slider in the Quick Access Menu does not work inside a side: it never reaches the panel. Use Main's menu instead.

## Performance overlay

Steam's Performance Overlay works in each side's Gaming mode. Open Steam's Quick Access Menu, choose **Performance** and pick an overlay level from 0 to 4. The level you pick shows only in your side. The other side keeps its own level, and its numbers are its own. Desktop mode has no overlay.

The overlay has only been checked with stand-in programs so far, not on a Steam Deck or a Steam Machine.

## FPS counter

DosPlex can show the frame rate of your side itself, without Steam's overlay.

**FPS counter on this side:** **Off**, **FPS**, or **FPS + frame time**. It takes effect at once, needs no **Apply**, and does not restart anything. It is off by default. Each side has its own setting, and DosPlex remembers it across restarts. The counter sits in the top-left corner of your side, above the game and below your menu. **FPS** shows the frames delivered in the last second. **FPS + frame time** also shows the average frame time over that second and the 99th-percentile frame time over the last five seconds; a p99 far above the average means stutter.

The number is what reaches your part of the screen, not the game's own frame rate. Read the game's own counter for what the game renders. A still picture reads low.

**Record performance** starts and stops a CSV recording for your side. While it records, the button reads **Recording: stop**. Recording is off after every start of DosPlex. The files go to `/var/lib/dosplex/perf/`, and DosPlex keeps the newest 20. The counter and the recording have only been checked with test programs so far, not on a Steam Deck or a Steam Machine.
