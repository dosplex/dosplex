# Turn off, sleep, restart

Open Main's menu and go to the **Session** page. It has **Turn off Dosplex** and three power entries. Only Main sees them.

**Turn off Dosplex** ends Dosplex and leaves the machine on. Use it when you are done playing together and want plain SteamOS back.

1. Save in both games first: both players' games close.
2. Choose **Turn off Dosplex**.
3. Choose **Continue**. **Cancel** is selected first, so a stray press changes nothing.

The screen says that Steam is closing on both sides. Within a minute you are back in normal SteamOS. To play together again, start Dosplex from Steam's library.

**Sleep** suspends the machine. Both sessions stay open and resume when the machine wakes. Dosplex pauses both sessions just before sleep and lets them run again on wake. A session that was in Standby stays in Standby. If a controller does not answer after the machine wakes, use **Reset my side**.

**The machine does not go to sleep by itself while two players are on the screen.** Each player's Steam only knows about its own controller, so Dosplex holds Steam's idle sleep on both sides: otherwise Main's Steam would put the machine to sleep while Guest is still playing. Steam's screensaver still comes after a few minutes on a side nobody uses, and goes away when that player presses a button. About once an hour the picture of such a side comes back for five minutes; that is Dosplex telling Steam not to sleep. Choose **Sleep** here, or press the power button, when you are done. With Main alone on the screen, Steam sleeps the machine after its usual time. Do not use **Sleep** in Guest's own Steam menu: Guest's Steam may not put the machine to sleep, and it stays on its sleep picture until Dosplex is turned off and started again.

**Restart** asks Steam to quit on both sides and waits up to 45 seconds for each, so saves can sync to the cloud. It then stops both sessions and Dosplex, and restarts the machine. A Steam that does not quit in time is stopped. Save your progress first. After a restart the machine comes back as normal SteamOS. Start Dosplex from Steam's library to bring it back.

**Shut down** works the same way but turns the machine off instead of restarting it.

Each of these asks first, and says **This affects both players**. **Cancel** is selected first. If something goes wrong during a restart, the screen returns to Main's menu on the **Session** page with a message, and both players' controls work again.

**Reset my side** restarts your own Steam and leaves the other player alone. **Reset both sides** restarts both. While one player has the full screen there is one button instead, **Reset Steam**, and it does the same for that player.
