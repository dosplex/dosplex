# Limits of the alpha

## The picture

| Limit | What to do |
|---|---|
| A running game does not follow a change of layout or picture size. | Quit it and start it again. |
| A game keeps its own resolution setting. One that saved the full size may quit at once in a split. | See [When something is wrong](14-when-something-is-wrong.md). |
| On the Deck's own screen both players share one backlight. | Main's menu is the only brightness control. |
| The frame-rate counter and Steam's overlay were only checked with test programs. | None. |
| Standby is experimental. After an hour, Steam asks for sleep when the session returns. | See [Layout](06-layout.md). |

## Controllers and sound

| Limit | What to do |
|---|---|
| Other gamepads open the menu only experimentally. The chord reaches the game and your side is paused. | Use a keyboard, a Steam Controller or the Deck's controls. |
| A new controller goes to Guest only while Guest is on the screen with none awake. Otherwise it is Main's. | Main moves it in **Devices**. |
| A running game may not see a newly claimed controller. | Restart the game. |
| Guest's Steam shows one output and one microphone. | Choose the real devices in **Audio**. |
| A microphone claim is not kept. | Claim it again after a restart. |

## Steam's own settings pages

Wi-Fi, power, updates and storage pages do not work inside a side.

| Page | What to do |
|---|---|
| Wi-Fi and power | Use Steam's Quick Access Menu on the side. |
| Updates | Update the machine first. Then run `sudo /opt/dosplex/bin/dosplex update` in a shell. |
| Storage | Use Desktop mode. |

## Between the two players

> [!WARNING]
> DosPlex keeps the players apart in ordinary use. It is not yet a wall against someone who sets out to cross it. Use the alpha only with people you trust at the same screen.

- A program of one player can draw a window over the other player's part and take that player's pointer. It does not get key presses.
- The two sides share one network. A program on one side can reach services the other side opens on the machine.

## What Main decides for both

Main decides the layout, who owns each controller, mouse and keyboard, the shared speakers, and Turn off, Sleep, Restart and Shut down. With two players the machine does not sleep by itself ([Turn off, sleep, restart](12-turn-off-sleep-restart.md)).

More: [the limitations page](../limitations.md).
