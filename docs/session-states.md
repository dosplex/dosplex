# Session states and device ownership

DosPlex has three session states. Only Main's **Full screen** choice changes them: in
split layouts both sessions are always Active, and there are no manual state controls.

| State | Behaviour |
| --- | --- |
| Active | The player's Steam or desktop session runs. Always the case in split layouts, and always for Main. |
| Standby | Guest's session is frozen in memory (its systemd cgroup); processes stop, audio is detached and input is suppressed. Chosen by Main when going to full screen. |
| Off | Guest's session is closed after a second confirmation; unsaved progress is lost. Chosen by Main when going to full screen. |

Device claims are kept in all three states. Releasing or passing a device is an
explicit action; changing the display layout never grants another player ownership.

Selecting full screen asks whether Guest should enter **Standby** or **Off**.
Standby preserves the existing process group. Off requires a second confirmation
warning that unsaved progress will be lost. Returning to split resumes a standby
Guest and starts an Off Guest again, at Steam's own sign-in or onboarding screen. A Guest session
that has not started yet waits until it can be shown.

Sleeping the machine pauses a running session as well and wakes it afterwards; a
Guest in Standby stays in Standby, because only what DosPlex paused itself is woken.

Standby is not a saved game. It retains memory and GPU resources, does not survive
power loss, and can cause online sessions to disconnect. GPU/game compatibility
must be validated on hardware. A change of layout or picture size does not
restart a session: a game that is running keeps the game monitor it started with,
and its picture is scaled until you change its resolution or relaunch it (see
[Choose a picture size](user-guide.md#choose-a-picture-size)). A failed freeze is
reported as a recovery condition instead of deliberately closing the game.

## Audio claims

Each player has stable output and microphone buses. In Audio, select an
available output or microphone to claim it. Release disconnects that direction
and allows the other player to claim it. Another player's claimed device cannot
be taken. Outputs and microphones are selected separately, allowing TV speakers
with a headset microphone or two independent headsets.

Unplugging a claimed device retains its reservation and silences its route until
it returns. Standby and Off also detach routes while retaining claims. Active
reconnects available claimed endpoints. Test devices are explicitly labelled
Simulated Headset and Simulated Microphone.

Main's Steam uses the shared Pulse transport, which is experimental routing
infrastructure. Guest's Steam does not: it talks to an endpoint of its own that
offers one output, **DosPlex sound**, and one microphone, **DosPlex microphone**.
They lead to Guest's playback bus and microphone bus, so what Guest hears and
records is still chosen in the DosPlex Audio menu. Guest's Steam cannot list
Main's devices or the hardware, change a shared default, change any volume but
its own, or load or unload an audio module. Its own volume and mute control only
Guest's sound. The shared server's local TCP listener still exists, so this is
what Guest's Steam is offered, not a barrier against a hostile process running
as Guest. It is not an access boundary between the two players; see the security
section of [limitations](limitations.md).

## Acceptance and regression checks

Selecting Full screen asks what to do with Guest. Standby resumes on resplit; Off
starts a new Guest session on resplit (`tests/guest-split-restart-test.py`). Select must be pressed before D-pad Up to open the
controller overlay; hold the combination for about a second and release.

Process and synthetic-audio tests cover the basic transitions and routing.
Physical-headset hotplug and game/GPU standby compatibility still require
hardware acceptance. Standby is experimental.

For human acceptance, save both games, then:

1. In each overlay, claim a different real output and microphone. Check each
   player's game audio and microphone independently. Simulated devices are
   silent test endpoints; select real hardware to hear sound.
2. Release a headset and claim it from the other overlay. Verify that the other
   player cannot take a device while it remains claimed.
3. Run Valheim in Guest, choose Full → Standby from Main, then split again.
   Check that the world resumes, its controls work, and its audio returns only
   to the claimed output. Online sessions can disconnect during standby.
4. Save and exit the world before checking Full → Off. Resplitting must leave
   Guest closed; Active should start Steam again.
5. Check the overlay on both controllers and in single-screen mode, including
   when the other controller is asleep.

### Steam runtime audio transport

Steam game containers need an accessible Unix PulseAudio socket. A TCP-only
`PULSE_SERVER` can work for host tests while games remain silent. Configure
`DOSPLEX_AUDIO_SERVER=unix:/path/to/socket` for Main's side launcher and verify
the socket is forwarded into the actual game container. Preserve each side's
`PULSE_SINK` and `PULSE_SOURCE`. The test harness supplies this
connection. It is not an access boundary between the two players; see the security
section of [limitations](limitations.md).

Guest never receives that variable. Its launcher takes
`DOSPLEX_GUEST_AUDIO_SERVER=unix:/path/to/socket` and, without one, starts Steam
with no audio rather than on the shared server. The audio router provides the
endpoint when `DOSPLEX_GUEST_AUDIO_DIR` names a directory (the trial uses
`<trial root>/audio-guest`, the packaged default is `/run/dosplex/audio/guest`):
a private PipeWire, pipewire-pulse and WirePlumber run as Guest in a directory
only Guest can enter, and their two devices are tunnels to Guest's buses.
The retained trial's session wrapper runs
`dosplex-audio-router.py guest-endpoint-check` before Steam starts and refuses an
endpoint that offers anything else or allows module loading. Tunnelling adds
about 60 ms to Guest's audio (a synthetic click on an idle workstation; not
measured on a Deck).
It was checked against PipeWire 1.2.4 and WirePlumber 0.5.15 in the isolated
audio test (`pulse.allow-module-loading`, the pulse tunnel module and
WirePlumber's `policy` profile); other versions are untested, and the endpoint
check refuses one that does not behave as described.
