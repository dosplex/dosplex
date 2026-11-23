# Display

Open **Display** in your menu. It has the picture size ([Picture size](07-picture-size.md)), brightness on a Deck and the frame-rate counter.

## Brightness

Only Main sees it, and only on a machine with a built-in backlight, the Steam Deck.

- **Dimmer** and **Brighter** change it in steps of 5 %. The lowest value is 5 %.
- DosPlex remembers the value.
- Steam's brightness slider in the Quick Access Menu does not work inside a side.

## Performance overlay

In Steam's Quick Access Menu choose **Performance** and pick a level from 0 to 4. It shows only on your side. Desktop mode has none.

## FPS counter

**FPS counter on this side** is **Off**, **FPS** or **FPS + frame time**.

- It takes effect at once, needs no **Apply**, and is off by default. Each side has its own.
- It sits in the top-left corner of your side.
- **FPS** is the frames delivered in the last second.
- **FPS + frame time** adds the average and the 99th-percentile frame time. A 99th percentile far above the average means stutter.
- It counts what reaches your part of the screen, not the game's own frame rate.

**Record performance** starts and stops a CSV recording for your side, and the button reads **Recording: stop**. It is off after every start of DosPlex. Files go to `/var/lib/dosplex/perf/`, newest 20 kept.

> [!NOTE]
> The counter, the recording and Steam's overlay have only been checked with test programs, not on a Steam Deck or Steam Machine.

More: [the user guide](../user-guide.md#performance-overlay-and-performance-logs).
