#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,ordinary-user timeout=120
# dosplex-run: default {compositor}
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Splitting the display always shows Guest (regression for a lost Guest sign-in screen).

Full screen offers Standby (keep Guest in the background) or Off (close Guest).
Returning to a split layout must resume a Standby Guest and start an Off Guest
again, through both the overlay and the control socket. Otherwise Guest's half
shows a placeholder forever and Steam's sign-in/onboarding never appears.

Runs a disposable headless compositor as an ordinary user. It checks published
activity state only; Steam start-up itself is covered by hardware checks.
"""
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import time


def main(binary: Path) -> None:
    if os.geteuid() == 0:
        raise RuntimeError("run as an ordinary user")
    with tempfile.TemporaryDirectory(prefix="dosplex-guest-split-") as tmp:
        root = Path(tmp)
        user = pwd.getpwuid(os.getuid()).pw_name
        env = {
            "PATH": os.environ["PATH"], "HOME": str(root), "XDG_RUNTIME_DIR": str(root),
            "DOSPLEX_RUNTIME_DIR": str(root), "WAYLAND_DISPLAY": "dosplex-0", "WLR_RENDERER": "pixman",
            "DOSPLEX_CONTROLLER_DIRECTORY": str(root / "controller-preferences"),
            "DOSPLEX_LEFT_USER": user, "DOSPLEX_RIGHT_USER": user, "DOSPLEX_INITIAL_LAYOUT": "horizontal",
            "DOSPLEX_AUTH_HELPER": "/bin/false", "DOSPLEX_SESSION_SCRIPT": "/bin/false",
        }
        with (root / "compositor.log").open("w+") as log:
            proc = subprocess.Popen([str(binary), "-b", "headless"], env=env,
                                    stdout=log, stderr=log, start_new_session=True)
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            pointer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                def state():
                    if proc.poll() is not None:
                        raise AssertionError(f"compositor exited: {(root / 'compositor.log').read_text()[-2000:]}")
                    path = root / "state.json"
                    return json.loads(path.read_text()) if path.exists() else None

                def wait_for(check, label):
                    deadline, latest = time.monotonic() + 6, None
                    while time.monotonic() < deadline:
                        latest = state()
                        if latest is not None and check(latest):
                            return latest
                        time.sleep(.02)
                    raise AssertionError(f"timeout waiting for {label}; last state={latest}")

                def ipc(command):
                    control.sendto(command.encode(), str(root / "hud-ipc.sock"))

                def click(x, y):
                    pointer.sendall(f"M {x} {y}\nB 272 1\nB 272 0\n".encode())
                    time.sleep(.08)

                wait_for(lambda s: s["layout"] == "horizontal" and s["left"]["width"] == 1280, "headless output")
                pointer.connect(str(root / "dosplex-primary-input.sock"))
                guest = lambda s: s["right"]["activity"]

                # In split both sessions are always active: there is no manual Standby/Off page.
                ipc("OVERLAY left")
                wait_for(lambda s: s["overlay_visible"], "Main overlay")
                time.sleep(.1)
                click(901, 97)  # Session tab.
                wait_for(lambda s: s["overlay_tab"] == 2, "Session tab")
                time.sleep(.15)  # Let the Session tab's hit regions land.
                # Where the removed "Session states" toggle and its Guest "Off" button used to
                # be. On a build that still has them these clicks turn Guest Off; now they hit
                # the ordinary layout page instead (Main top/bottom), which keeps Guest active.
                click(909, 66)
                time.sleep(.15)
                click(872, 207)
                if state()["overlay_confirmation"]:
                    click(814, 229)  # Continue.
                    time.sleep(.2)
                current = state()
                assert current["left"]["activity"] == "active" and guest(current) == "active", \
                    f"a manual session-state control is still reachable: {current['left']['activity']}/{guest(current)}"
                ipc("OVERLAY close")
                ipc("GUEST right")  # Restore Guest at the bottom if the click swapped positions.
                wait_for(lambda s: s["guest_physical_side"] == "right" and not s["overlay_visible"], "restored layout")

                def full_screen(choice):
                    """Main's Session tab -> Full screen -> Standby, or Off plus its destructive confirmation."""
                    ipc("OVERLAY left")
                    wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "left", "Main overlay")
                    time.sleep(.1)
                    click(901, 97)   # Session tab (horizontal Main panel, y=12).
                    wait_for(lambda s: s["overlay_tab"] == 2, "Session tab")
                    click(408, 162)  # Full screen.
                    wait_for(lambda s: s["overlay_confirmation"], "full-screen choice")
                    if choice == "standby":
                        click(466, 270)
                    else:
                        click(814, 270)  # Off... opens a second confirmation.
                        click(814, 229)
                    wait_for(lambda s: s["layout"] == "single" and guest(s) == choice, f"full screen, Guest {choice}")

                def split_from_overlay():
                    click(727, 240)  # Top / bottom (third of four layout buttons) on the full-screen panel (y=90).
                    wait_for(lambda s: s["overlay_confirmation"], "split confirmation")
                    click(814, 307)  # Continue.
                    return wait_for(lambda s: s["layout"] == "horizontal", "split applied")

                full_screen("standby")
                assert guest(split_from_overlay()) == "active", "split did not resume Standby Guest"
                ipc("OVERLAY close")

                full_screen("off")
                after = split_from_overlay()
                assert guest(after) == "active", f"split left Off Guest stopped: {guest(after)}"
                ipc("OVERLAY close")

                full_screen("off")
                ipc("OVERLAY close")
                ipc("LAYOUT vertical")
                wait_for(lambda s: s["layout"] == "vertical" and guest(s) == "active",
                         "control-socket split starts Off Guest")
                # Guest full screen: Main's own choice from the overlay, then every way back.
                ipc("LAYOUT horizontal")
                wait_for(lambda s: s["layout"] == "horizontal", "horizontal again")
                ipc("OVERLAY left")
                wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "left", "Main overlay")
                time.sleep(.1)
                click(901, 97)   # Session tab.
                wait_for(lambda s: s["overlay_tab"] == 2, "Session tab")
                click(901, 162)  # Guest full screen (fourth of four layout buttons).
                wait_for(lambda s: s["overlay_confirmation"], "Guest full screen confirmation")
                assert state()["layout"] == "horizontal", "the choice waits for the confirmation"
                click(814, 229)  # Continue.
                now = wait_for(lambda s: s["layout"] == "guest", "Guest full screen from the overlay")
                assert now["left"]["activity"] == "standby" and guest(now) == "active", (now["left"]["activity"], guest(now))
                assert not now["overlay_visible"], "Main's overlay closes when Guest gets the screen"
                assert (now["right"]["x"], now["right"]["y"], now["right"]["width"], now["right"]["height"]) == (0, 0, 1280, 720)

                # Guest may open its own overlay, but Main's request still takes it, drawn over Guest.
                ipc("OVERLAY right")
                wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "right", "Guest overlay in Guest full screen")
                ipc("OVERLAY left")
                wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "left" and s["layout"] == "guest",
                         "Main's overlay over Guest full screen")
                time.sleep(.1)
                click(901, 97)   # Main's panel is the whole screen's: Session tab at the same place as in Full screen.
                ipc("OVERLAY close")
                wait_for(lambda s: not s["overlay_visible"], "overlay closed")

                # Leaving: any other layout wakes Main; plain Full screen puts Guest in Standby.
                ipc("LAYOUT vertical")
                back = wait_for(lambda s: s["layout"] == "vertical" and s["left"]["activity"] == "active", "Main back from standby")
                assert guest(back) == "active"
                ipc("LAYOUT guest")
                wait_for(lambda s: s["layout"] == "guest" and s["left"]["activity"] == "standby", "Guest full screen by hud-ipc")
                # Main's overlay in Guest full screen is drawn over the whole screen (panel at y=90, as in Full screen).
                ipc("OVERLAY left")
                wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "left", "Main overlay over Guest")
                time.sleep(.1)
                click(901, 175)  # Session tab.
                wait_for(lambda s: s["overlay_tab"] == 2, "Session tab over Guest")
                time.sleep(.15)
                click(379, 240)  # Full screen.
                wait_for(lambda s: s["overlay_confirmation"], "Full screen confirmation over Guest")
                click(466, 348)  # Standby.
                full = wait_for(lambda s: s["layout"] == "single" and s["left"]["activity"] == "active",
                                "Full screen from Guest full screen")
                assert guest(full) == "standby", guest(full)
                # Guest cannot ask for the overlay in Full screen.
                ipc("OVERLAY close")
                ipc("OVERLAY right")
                time.sleep(.2)
                assert not state()["overlay_visible"], "Full screen shows only Main"
                print("PASS: Guest full screen (overlay, hud-ipc, overlay gate, way back)")
                print("PASS: no manual session-state controls; splitting resumes a Standby Guest and starts an Off Guest (overlay and control socket)")
            finally:
                control.close()
                pointer.close()
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: guest-split-restart-test.py COMPOSITOR")
    main(Path(sys.argv[1]))
