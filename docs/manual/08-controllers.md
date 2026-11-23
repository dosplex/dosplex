# Controllers

Open **Devices** to see the connected controllers and who they belong to. The first controller you wake is **Main's**. While the second player is on the screen and has no controller that is awake, the next one is Guest's. On a Steam Deck the built-in controls are Main's, so the first other controller is Guest's. A controller never changes the layout, and DosPlex needs no controller to start. Each controller has a small number that stays with it; the Devices page shows it beside the name, so you can tell controller 1 from controller 2.

## Claim and release

- **Claim:** choose **Claim** on an Unassigned controller that is awake, and it is added to your side. You can hold several, and Steam on your side sees each as its own controller. Guest lists the free controllers and its own. Main sees every controller and uses the **Main**, **Guest** and **Unassigned** buttons on its row; check the result, then confirm with **Keep assignment**, or use **Undo**. A move that you do not confirm within 15 seconds is undone by itself.
- **Release:** choose **Release** on a controller you hold, and it is Unassigned again for anybody to claim. If it was Guest's last controller, Guest's menu closes, because nothing is left to operate it; Main can give Guest a controller again.
- **Swap controls:** Main can exchange its controller with Guest's. When either of you holds several, it swaps the one each of you pressed last. When it cannot tell, it says "Press a button on the controller to swap": press a button on the controller you mean, then choose **Swap controls** again.

DosPlex remembers your choice across a stop and a reboot. To drop a controller you no longer use, turn it off or unplug it, then choose **Forget** on its row in Main's Devices page and confirm. Forgetting does not remove a Bluetooth pairing. A connected controller cannot be forgotten: release it to Unassigned instead.

A game that was already running when you claimed a controller may need a restart to see the new one. A layout never moves a controller. In Full screen, Main's menu chord opens Main's menu from every controller that has an owner, Guest's included.

## Mouse, keyboard and Shared

The second page of **Devices** has a **Mouse**, a **Keyboard** and, on a device with one, a **Touchscreen** row. Each covers every connected device of that kind together and starts with Main; choose **Guest** to hand it over. The mouse moves the cursor and clicks in the Steam of whoever it belongs to.

Choose **Shared** on the Mouse or the Keyboard to use it for both of you: a shared mouse crosses the middle of the screen and clicks in the side under it, and a shared keyboard types in the side where the mouse is. Only Main assigns these, and the Touchscreen row stays Main or Guest.

## Clicking with a Steam Controller

A Steam Controller's trackpad mouse and keyboard emulation work inside your own session and move your own cursor; the other player's pointer is separate.

You can click a pop-up in your own Steam with your own controller, as on a Steam Deck. Hold **Steam**, then:

| Input | What it does |
| --- | --- |
| Right trackpad or right stick | Moves the pointer |
| Right trackpad press, or **R2** | Left click |
| **L2** | Right click |
| D-pad right, left, down | Enter, Escape, Tab |
| Left stick | Volume |

This is Steam's own chord and it works only in your side. The left trackpad does nothing in the chord, and the button below Steam (Quick Access) is not part of it. Clicking with a real controller in a DosPlex side has not been checked on a device yet. If a pop-up still cannot be reached, check whether Steam's "Steam Controller detected!" tour is on; it holds the input until it is switched off.
