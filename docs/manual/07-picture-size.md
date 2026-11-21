# Picture size

The picture size decides what resolution your game sees and how the picture fills your part of the screen. Open **Display** in your menu.

| Setting | What it does |
| --- | --- |
| **Native** | Gives your game the pixels of your allocated part of the screen. |
| **1:1** | Gives the game the largest square resolution that fits your part of the screen. |
| **4:3** | Gives the game the largest 4:3 resolution that fits your part of the screen. |
| **16:9** | Gives the game the largest 16:9 resolution that fits your part of the screen. |
| **Keep ratio** | Fits the picture into your space, adding bars when needed. |
| **Stretch** | Fills your space with the game's picture, changing its proportions. Steam's own menus always keep their proportions. |
| **Fit** | Crops the picture to the shape of your space and fills it. For games that show black bars. Nothing is stretched, and the edges that do not fit are hidden. Dosplex never picks it for you. |

**Native** is always the first button. The **1:1**, **4:3** and **16:9** buttons follow it, ordered from the one that fills your part of the screen most to the one that fills it least in your current layout. Each button shows the size it gives, for example **1:1 1920x1920**.

In Full screen your part is the whole output: on 4K, **Native** and **16:9** are 3840x2160, **4:3** is 2880x2160 and **1:1** is 2160x2160. Dosplex recalculates the largest fitting size when you change the layout, and the overlay shows the selected size.

**When you would change it.** The default is **16:9** in the two split layouts and **Native** in Full screen, so a game sees your TV's ratio in every layout and does not turn into a narrow portrait picture with bars when the layout changes. If a game will not start with the default, try the other choices with **Keep ratio**: in Side by side, **1:1** then **4:3** then **16:9**; in Top / bottom and Full screen, **16:9** then **4:3** then **1:1**. **Fit** is the choice for a game that draws black bars inside its picture; it crops them away. Choose it yourself with **Apply**; Dosplex never does.

**Apply** changes your game monitor as soon as no game is running. When there is nothing to apply, the button reads **Up to date**. A game that is running keeps the monitor it started with, and Gamescope scales its picture into your new area. To make the running game fill the new area, change its resolution in its own video settings, or close it and start it again.

If a size above 1920x1200 pixels does not stay, Steam has set it back. Newer Steam clients do that unless Steam's setting "Enable Native Resolution on External Displays" is on. Dosplex switches it on for you before Steam starts, the first time. If you switched it off, it is in Steam under Settings, System, Enable Developer Mode, and then Settings, Developer. A side that has just been created gets the setting at its second start. Until it is on, Dosplex gives you the largest picture of the same shape that Steam leaves alone and scales it to your area.

Steam's own resolution settings work in each part of the screen, as on SteamOS: Settings → Display → Maximum Game Resolution for all games, and the game's Properties → General → Game Resolution for one game. Steam's list starts from your part of the screen and offers smaller sizes. A smaller size makes a heavy game lighter in a split, and Gamescope scales it up.
