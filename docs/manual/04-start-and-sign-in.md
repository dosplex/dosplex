# Start and sign in

Start DosPlex from Steam's library: open **DosPlex** under **Non-Steam** and press **Play**. Steam closes and the screen changes in a moment; after about half a minute both sides are there. The first start after an install takes longer: Guest's Steam sets itself up first, and Guest's half can stay black for a few minutes. The installer adds DosPlex to Steam's library by itself; if it could not, the chapter on installing shows how to add it.

Only the main player's account can start DosPlex this way. Nothing of DosPlex runs before you press Play, and after a restart of the machine you are in normal SteamOS until you press it again.

You can also start it from the DosPlex icon on the desktop, or from a terminal in Desktop mode. Run `sudo /opt/dosplex/bin/dosplex start`. The device switches to Gaming mode and DosPlex takes up to two minutes to start. It runs as a system service, so closing the terminal does not stop it.

The screen splits into two halves. Each half runs its own Steam client, running under its own user account with separate controllers and audio. You sign in on your side using Steam's normal interface, including QR code sign-in and Steam Guard. Steam may ask for language or other first-run settings before showing the sign-in screen.

The second player signs in on their half of the screen in the same way, using their own Steam account. Each person needs their own account and access to the games they want to play. DosPlex does not change Steam's game-ownership rules.

### How the second player gets in

1. Wake a second controller. It goes to the second player by itself while they have none.
2. Use the QR code on Steam's sign-in page on the second half. Scan it with the Steam mobile app: this signs in with no controller, keyboard or mouse.
3. To move a controller by hand, Main opens the DosPlex menu, chooses **Devices**, picks the controller, chooses **Guest** and then **Keep assignment**.
4. A mouse or keyboard is Main's until Main gives it to Guest or shares it on the same page.

You can leave the second side signed out while you use your own. A keyboard, mouse, or generic gamepad works for starting; a controller does not have to be awake. If you want to use a Steam Controller on the second side, make sure it is paired and assigned before you sign in — see the controller chapter for details.
