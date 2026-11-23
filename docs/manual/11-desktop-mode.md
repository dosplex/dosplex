# Desktop mode

Open it from Steam's own menu: press the **Steam** button on your side, choose **Power**, then **Switch to Desktop**. A KDE Plasma desktop opens in your part of the screen. The other player keeps playing, and DosPlex's menu stays available. **Return to Gaming Mode** brings your Steam back.

Main and Guest each have their own desktop. Its settings are kept in `~/.local/share/dosplex/desktop` in that player's home folder.

## Scale

DosPlex chooses the scale from the size of your part of the screen when Desktop mode starts:

| Your part of the screen | Scale |
|---|---|
| At least 1600 pixels tall or 3000 pixels wide | 200 % |
| At least 1000 pixels tall | 150 % |
| Smaller | 100 % |

Changing the layout while the desktop is open does not change the scale.

To choose another scale:

1. In Desktop mode, write 1, 1.25, 1.5, 1.75, 2, 2.5 or 3 into `~/.local/share/dosplex/desktop/desktop-scale`. 1 is 100 %.
2. Open Desktop mode again.

Deleting the file returns to the automatic scale. KDE's own display scale setting has no effect here.

More: [the user guide](../user-guide.md#desktop-mode).
