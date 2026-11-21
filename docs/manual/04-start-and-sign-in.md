# Start and sign in

Start Dosplex from Steam's library: open **Dosplex** under **Non-Steam** and press **Play**. Steam closes and the screen changes in a moment; after about half a minute both sides are there. The first start after an install takes longer: Guest's Steam sets itself up first, and Guest's half can stay black for a few minutes. The installer adds Dosplex to Steam's library by itself; if it could not, the chapter on installing shows how to add it.

Only the main player's account can start Dosplex this way. Nothing of Dosplex runs before you press Play, and after a restart of the machine you are in normal SteamOS until you press it again.

You can also start it from a terminal in Desktop mode. Run `sudo /opt/dosplex/bin/dosplex start`. The device switches to Gaming mode and Dosplex takes up to two minutes to start. It runs as a system service, so closing the terminal does not stop it.

The screen splits into two halves. Each half runs its own Steam client, running under its own user account with separate controllers and audio. You sign in on your side using Steam's normal interface, including QR code sign-in and Steam Guard. Steam may ask for language or other first-run settings before showing the sign-in screen.

The second player signs in on their half of the screen in the same way, using their own Steam account. Each person needs their own account and access to the games they want to play. Dosplex does not change Steam's game-ownership rules.

You can leave the second side signed out while you use your own. A keyboard, mouse, or generic gamepad works for starting; a controller does not have to be awake. If you want to use a Steam Controller on the second side, make sure it is paired and assigned before you sign in — see the controller chapter for details.
