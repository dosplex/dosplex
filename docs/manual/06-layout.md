# Layout

Only Main changes the layout. Open Main's menu, go to **Session** and use the row **Shared display layout**.

| Button | What you get |
|---|---|
| **Side by side** | A vertical split: one player left, one right. |
| **Top / bottom** | A horizontal split: one player on top, one below. |
| **Full screen** | Main has the whole screen. Main decides what happens to Guest. |
| **Guest full screen** | Guest has the whole screen. Main goes to Standby. |

The row under it says which part is Main's: **Main left**, **Main right**, **Main top** or **Main bottom**. In a split both sessions always run.

## Full screen

- **Standby** pauses Guest's session in memory. Splitting again resumes it.
- **Off** closes Guest's session after you confirm. Save first. Splitting again starts Guest fresh at Steam's sign-in screen.

## Guest full screen

Guest cannot ask for it. To come back, open your menu and choose **Full screen**, **Side by side** or **Top / bottom**. Your session wakes. **Full screen** from here puts Guest in Standby.

> [!WARNING]
> Standby is experimental. It keeps using memory, does not survive power loss, and online games can disconnect. Save before you rely on it.

After more than an hour in Standby, Steam asks for sleep when the session returns:

- Guest's part turns black: choose **Reset my side** in Guest's menu.
- Main's: the machine goes to sleep. Press the power button and both players continue.

More: [the user guide](../user-guide.md#take-the-whole-screen-then-return-to-two-people).
