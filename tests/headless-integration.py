#!/usr/bin/env python3
# dosplex-test: tier=integration needs=compositor,wayland-info,wayland-client,cc,ordinary-user timeout=180
# dosplex-run: left {compositor} --client-side left
# dosplex-run: right {compositor} --client-side right
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise the real compositor without DRM, root, accounts or installed services."""
import argparse
import json
import importlib.util
import os
import pwd
from pathlib import Path
import re
import shutil
import shlex
import socket
import subprocess
import tempfile
import time


RATIOS = {"16:9": (16, 9), "4:3": (4, 3), "1:1": (1, 1)}


def fitting_aspect(width, height, aspect):
    if aspect == "native":
        return width, height
    ratio_width, ratio_height = RATIOS[aspect]
    scale = min(width // ratio_width, height // ratio_height)
    assert scale > 0, f"no {aspect} resolution fits {width}x{height}"
    return ratio_width * scale, ratio_height * scale


def ratio_order(width, height):
    """The ratio buttons after Native: descending fill of the region, ties keep 16:9, 4:3, 1:1."""
    def area(aspect):
        size = fitting_aspect(width, height, aspect)
        return size[0] * size[1]
    return sorted(RATIOS, key=lambda aspect: -area(aspect))


def run(binary, client_side="left"):
    if os.geteuid() == 0:
        raise RuntimeError("Run this test as an ordinary user")
    if not shutil.which("wayland-info"):
        raise RuntimeError("wayland-info is required to verify client-observed modes")
    with tempfile.TemporaryDirectory(prefix="dosplex-test-") as directory:
        root = Path(directory)
        project = Path(__file__).resolve().parents[1]
        protocols = subprocess.check_output(["pkg-config", "--variable=pkgdatadir", "wayland-protocols"], text=True).strip()
        # The window needs xdg-shell; the locked pointer needs pointer constraints and relative pointer.
        generated = []
        for xml, header, code in [
                ("stable/xdg-shell/xdg-shell.xml", "xdg-shell-client-protocol.h", "xdg-shell-protocol.c"),
                ("unstable/pointer-constraints/pointer-constraints-unstable-v1.xml",
                 "pointer-constraints-unstable-v1-client-protocol.h", "pointer-constraints-protocol.c"),
                ("unstable/relative-pointer/relative-pointer-unstable-v1.xml",
                 "relative-pointer-unstable-v1-client-protocol.h", "relative-pointer-protocol.c")]:
            for mode, filename in [("client-header", header), ("private-code", code)]:
                subprocess.run(["wayland-scanner", mode, str(Path(protocols) / xml), str(root / filename)],
                               check=True, timeout=5)
            generated.append(str(root / code))
        flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "wayland-client"], text=True))
        subprocess.run(["cc", "-I", directory, str(project / "tools/dosplex-keyprobe.c"), *generated,
                        "-o", str(root / "probe"), *flags], check=True, timeout=15)
        probe = None
        shutil.copyfile(project / "tests/fixtures/audio-overlay-helper.py", root / "audio-helper.py")
        env = {
            "PATH": os.environ["PATH"], "HOME": directory,
            "XDG_RUNTIME_DIR": directory, "WAYLAND_DISPLAY": "dosplex-0",
            "WLR_RENDERER": "pixman", "DOSPLEX_WAYLAND_DEBUG": "0",
            "DOSPLEX_AUDIO_ROUTER": str(root / "audio-helper.py"),
            "DOSPLEX_LEFT_USER": pwd.getpwuid(os.getuid()).pw_name if client_side == "left" else "dosplex-test-no-such-user",
            "DOSPLEX_RIGHT_USER": pwd.getpwuid(os.getuid()).pw_name if client_side == "right" else "dosplex-test-no-such-user",
            "DOSPLEX_AUTH_HELPER": "/bin/false", "DOSPLEX_SESSION_SCRIPT": "/bin/false",
            "DOSPLEX_MAP_WATCHDOG_MS": "1000",   # the map watchdog (20 s in production) within this test's waits
        }
        with (root / "compositor.log").open("w+") as log:
            proc = subprocess.Popen([str(binary), "-b", "headless", "--test-hooks"], env=env,
                                    stdout=log, stderr=log, start_new_session=True)
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            input_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                def state():
                    if proc.poll() is not None:
                        raise RuntimeError(f"compositor exited: {proc.returncode}")
                    path = root / "state.json"
                    return json.loads(path.read_text()) if path.exists() else None

                def wait_for(predicate):
                    deadline = time.monotonic() + 5
                    last = None
                    while time.monotonic() < deadline:
                        last = state()
                        if last is not None and predicate(last):
                            return last
                        time.sleep(0.01)
                    raise AssertionError(f"state did not converge: {last}")

                def send(command, predicate):
                    control.sendto(command.encode(), str(root / "hud-ipc.sock"))
                    return wait_for(predicate)

                first = wait_for(lambda s: s["left"]["width"] > 0)
                input_client.connect(str(root / "dosplex-primary-input.sock"))
                for endpoint in ("dosplex-primary-input.sock", "hud-ipc.sock", "control.sock"):
                    assert (root / endpoint).stat().st_mode & 0o777 == 0o600
                def pointer(x, y, click=False):
                    commands = f"M {x} {y}\n"
                    if click:
                        commands += "B 272 1\nB 272 0\n"
                    input_client.sendall(commands.encode())
                    time.sleep(.04)

                def key(code):
                    input_client.sendall(f"K {code} 1\nK {code} 0\n".encode())
                    time.sleep(.04)

                assert first["layout"] == "single", first
                # State contract version 2 leads the file: no display change restarts a side.
                assert next(iter(first)) == "contract_version" and first["contract_version"] == 2, first
                assert (first["left"]["width"], first["left"]["height"]) == (1280, 720)
                info = subprocess.run(["wayland-info"], env=env, capture_output=True,
                                      text=True, timeout=5, check=True).stdout
                assert f"name: dosplex-{client_side}" in info, info
                assert info.count("interface: 'wl_output'") == 1, info
                assert info.count("interface: 'wl_seat'") == 1, info
                assert "interface: 'wp_single_pixel_buffer_manager_v1'" in info, info
                assert f"name: seat{0 if client_side == 'left' else 1}" in info, info
                assert re.search(r"width: 1280 px, height: 720 px", info), info
                assert "width: 1280 px, height: 800 px" not in info, info

                # A side whose player has chosen nothing is Native in every layout: Full screen, side by side
                # and top/bottom. Its monitor is the region itself, so each layout change moves it by one
                # revision.
                for name in ("left", "right"):
                    assert (first[name]["game_aspect"], first[name]["pending_game_aspect"]) == ("native", "native"), first
                    assert first[name]["native_resolution"] and not first[name]["display_pending"], first
                    assert (first[name]["game_width"], first[name]["game_height"]) == (1280, 720), first
                previous = first
                for layout, aspect, game_size, step in [("vertical", "native", (640, 720), 1),
                                                       ("horizontal", "native", (1280, 360), 1),
                                                       ("single", "native", (1280, 720), 1)]:
                    current = send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    for name in ("left", "right"):
                        assert (current[name]["game_aspect"], current[name]["pending_game_aspect"]) == (aspect, aspect), current
                        assert (current[name]["game_width"], current[name]["game_height"]) == game_size, current
                        assert current[name]["native_resolution"] == (aspect == "native"), current
                        assert not current[name]["display_pending"], current
                        assert current[name]["display_revision"] == previous[name]["display_revision"] + step, (previous, current)
                    previous = current

                for layout, size in [("vertical", (640, 720)), ("horizontal", (1280, 360))]:
                    before_layout = state()
                    current = send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    # Each side's game monitor is the size of its choice in the new region (Native: the
                    # region; the ratio choices: the largest one that fits): one
                    # revision step for a side whose monitor changed size and none for one that did not.
                    for side in ("left", "right"):
                        expected = fitting_aspect(size[0], size[1], current[side]["game_aspect"])
                        assert (current[side]["game_width"], current[side]["game_height"]) == expected, current
                        moved = expected != (before_layout[side]["game_width"], before_layout[side]["game_height"])
                        assert current[side]["display_revision"] == before_layout[side]["display_revision"] + int(moved), (before_layout, current)
                    info = subprocess.run(["wayland-info"], env=env, capture_output=True,
                                          text=True, timeout=5, check=True).stdout
                    assert f"name: dosplex-{client_side}" in info, info
                    assert info.count("interface: 'wl_output'") == 1, info
                    assert f"width: {size[0]} px, height: {size[1]} px" in info, info
                    assert "logical_x: 0, logical_y: 0" in info, info
                    for side in ("left", "right"):
                        assert (current[side]["width"], current[side]["height"]) == size, current
                    if current["right"]["game_aspect"] == "4:3":
                        expected = fitting_aspect(size[0], size[1], "4:3")
                        assert (current["right"]["game_width"], current["right"]["game_height"]) == expected, current
                    send("OVERLAY right", lambda s: s["overlay_visible"] and s["overlay_owner"] == "right")
                    send("ROUTE __dosplex_remote_input__ follow", lambda s: s["input_leap_route"] == "follow")
                    time.sleep(.04)
                    side_state = current["right"]
                    panel_width = min(size[0] - 24, 720)
                    panel_height = min(size[1] - 24, 540)
                    px = side_state["x"] + (size[0] - panel_width) // 2
                    py = side_state["y"] + (size[1] - panel_height) // 2
                    # The ratio buttons follow Native in descending fill of the
                    # half: 1:1, 4:3, 16:9 side by side; 16:9, 4:3, 1:1 top/bottom.
                    # Click the first, move to the second with the arrow keys and
                    # to the third with Tab, all through the real input path.
                    order = ratio_order(size[0], size[1])
                    assert order == (["1:1", "4:3", "16:9"] if layout == "vertical" else ["16:9", "4:3", "1:1"]), order
                    quarter = (panel_width - 56) // 4
                    pointer(px + 16 + (quarter + 8) + quarter // 2, py + 150, True)
                    for step, aspect in enumerate(order):
                        if step == 1:
                            key(106)  # Right
                            key(28)   # Enter
                        elif step == 2:
                            key(15)   # Tab
                            key(28)   # Enter
                        expected_size = fitting_aspect(size[0], size[1], aspect)
                        wait_for(lambda s: (s["right"]["pending_game_width"], s["right"]["pending_game_height"]) == expected_size and s["right"]["pending_game_aspect"] == aspect)
                    # Another side's Escape must not close this menu.
                    pointer(100, 100)
                    key(1)
                    assert state()["overlay_visible"]
                    pointer(px + 50, py + 50)
                    key(1)
                    wait_for(lambda s: not s["overlay_visible"])
                    send("DISPLAY_CANCEL right", lambda s: not s["right"]["display_pending"])


                    untouched = state()["left"]
                    for preset, game_size in [("720p", (1280, 720)), ("1080p", (1920, 1080)), ("native", size)]:
                        previous = state()["right"]
                        selected = send(f"RESOLUTION right {preset}",
                                        lambda s: (s["right"]["pending_game_width"], s["right"]["pending_game_height"]) == game_size)
                        assert selected["right"]["game_width"] == previous["game_width"]
                        assert selected["right"]["display_revision"] == previous["display_revision"]
                        changed = selected["right"]["display_pending"]
                        applied = send("DISPLAY_APPLY right", lambda s: not s["right"]["display_pending"])
                        assert (applied["right"]["game_width"], applied["right"]["game_height"]) == game_size
                        assert applied["right"]["display_revision"] == previous["display_revision"] + int(changed)
                        assert applied["left"] == untouched, "right display setting changed left side"
                    send("SCALE right stretch", lambda s: s["right"]["pending_scale_mode"] == "stretch")
                    cancelled = send("DISPLAY_CANCEL right", lambda s: not s["right"]["display_pending"])
                    assert cancelled["right"]["scale_mode"] == "fit"
                    assert cancelled["right"]["pending_scale_mode"] == "fit"
                    send("SCALE right zoom", lambda s: s["right"]["pending_scale_mode"] == "zoom")
                    cancelled = send("DISPLAY_CANCEL right", lambda s: not s["right"]["display_pending"])
                    assert cancelled["right"]["scale_mode"] == "fit" and cancelled["right"]["pending_scale_mode"] == "fit"
                    # An aspect choice follows the side's available pixels when
                    # its layout changes; it does not keep a stale resolution.
                    # The square 1:1 is min(width, height); 4:3 is applied last.
                    for aspect in ("1:1", "4:3"):
                        expected = fitting_aspect(size[0], size[1], aspect)
                        if aspect == "1:1":
                            assert expected == (min(size),) * 2, expected
                        send(f"ASPECT right {aspect}", lambda s: (s["right"]["pending_game_width"], s["right"]["pending_game_height"]) == expected and s["right"]["pending_game_aspect"] == aspect)
                        applied = send("DISPLAY_APPLY right", lambda s: not s["right"]["display_pending"])
                        assert (applied["right"]["game_width"], applied["right"]["game_height"]) == expected
                        assert applied["right"]["game_aspect"] == aspect

                # Position changes must retain account identities and input assignment.
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                send("ROUTE remote guest", lambda s: s["input_leap_route"] == "right")
                before = state()
                moved = send("GUEST left", lambda s: s["guest_physical_side"] == "left")
                assert moved["left"]["x"] > moved["right"]["x"]
                assert moved["primary_side"] == before["primary_side"]
                assert moved["input_leap_route"] == "right"
                single = send("LAYOUT single", lambda s: s["layout"] == "single")
                expected = fitting_aspect(single["right"]["width"], single["right"]["height"], "4:3")
                assert single["right"]["game_aspect"] == "4:3"
                assert (single["right"]["game_width"], single["right"]["game_height"]) == expected
                assert state()["input_leap_route"] == "right", "full screen lost saved assignment"
                restored = send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                expected = fitting_aspect(restored["right"]["width"], restored["right"]["height"], "4:3")
                assert restored["right"]["game_aspect"] == "4:3"
                assert (restored["right"]["game_width"], restored["right"]["game_height"]) == expected
                assert restored["input_leap_route"] == "right"
                assert restored["guest_physical_side"] == "left"
                # The square choice is recalculated by a layout change like the
                # other ratios, and the other side keeps its default, Native.
                send("ASPECT right 1:1", lambda s: s["right"]["pending_game_aspect"] == "1:1")
                send("DISPLAY_APPLY right", lambda s: not s["right"]["display_pending"])
                for layout in ("single", "horizontal", "vertical"):
                    changed = send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    region = (changed["right"]["width"], changed["right"]["height"])
                    assert changed["right"]["game_aspect"] == "1:1", changed
                    assert (changed["right"]["game_width"], changed["right"]["game_height"]) == (min(region),) * 2, changed
                    assert (changed["right"]["pending_game_width"], changed["right"]["pending_game_height"]) == (min(region),) * 2, changed
                    assert not changed["right"]["display_pending"], changed
                    # The other side made no choice, so it is Native in every layout.
                    assert changed["left"]["game_aspect"] == "native", changed
                assert state()["input_leap_route"] == "right"
                assert state()["guest_physical_side"] == "left"
                send("GUEST right", lambda s: s["guest_physical_side"] == "right")
                send("ROUTE remote follow", lambda s: s["input_leap_route"] == "follow")

                # Left has applied no choice yet. A selection that is not applied does not move with the layout
                # (the applied choice does follow the default), and Cancel returns to the default.
                send("ASPECT left 4:3", lambda s: s["left"]["pending_game_aspect"] == "4:3")
                assert state()["left"]["display_pending"] and state()["left"]["game_aspect"] == "native", state()
                selected = send("LAYOUT horizontal", lambda s: s["layout"] == "horizontal")
                assert (selected["left"]["game_aspect"], selected["left"]["pending_game_aspect"]) == ("native", "4:3"), selected
                assert (selected["left"]["pending_game_width"], selected["left"]["pending_game_height"]) == (480, 360), selected
                assert (selected["left"]["game_width"], selected["left"]["game_height"]) == (1280, 360), selected
                assert selected["left"]["display_pending"], selected
                cancelled = send("DISPLAY_CANCEL left", lambda s: not s["left"]["display_pending"])
                assert (cancelled["left"]["game_aspect"], cancelled["left"]["pending_game_aspect"]) == ("native", "native"), cancelled
                send("LAYOUT single", lambda s: s["layout"] == "single")
                assert state()["left"]["game_aspect"] == "native", "Cancel kept a choice"
                # Choosing what a side already has is no choice: Native is the default in every layout, so there
                # is nothing to Apply.
                control.sendto(b"ASPECT left native", str(root / "hud-ipc.sock"))
                time.sleep(.15)
                assert not state()["left"]["display_pending"], state()
                assert send("LAYOUT vertical", lambda s: s["layout"] == "vertical")["left"]["game_aspect"] == "native"
                # A choice that is applied is the player's own, and no layout change moves it: a side that chose
                # 16:9 keeps 16:9 in every layout, while its neighbour that chose nothing stays Native.
                chosen = send("ASPECT left 16:9", lambda s: s["left"]["display_pending"])
                applied = send("DISPLAY_APPLY left", lambda s: not s["left"]["display_pending"])
                assert (applied["left"]["game_width"], applied["left"]["game_height"]) == (640, 360), applied
                assert applied["left"]["display_revision"] == chosen["left"]["display_revision"] + 1, applied
                for layout, region in (("horizontal", (1280, 360)), ("single", (1280, 720)), ("vertical", (640, 720))):
                    current = send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    assert (current["left"]["game_aspect"], current["left"]["native_resolution"]) == ("16:9", False), current
                    assert (current["left"]["game_width"], current["left"]["game_height"]) == fitting_aspect(*region, "16:9"), current
                    assert not current["left"]["display_pending"], current
                    assert current["right"]["game_aspect"] != "native" or current["right"]["native_resolution"], current
                # Back to Native, now as the player's own choice: the checks below follow what a layout change does
                # to an applied Native monitor.
                send("ASPECT left native", lambda s: s["left"]["display_pending"])
                send("DISPLAY_APPLY left", lambda s: not s["left"]["display_pending"])

                # The side supervisor's report on applying the display reaches state.json: the monitor in effect,
                # whether the picture is scaled (the report for the current revision says deferred, or the monitor is
                # not the size asked for) and the report's result. DEV_SIDE_DISPLAY stands in for session.json, which
                # an ordinary user cannot supply (the state file must be root's).
                KEYS = ("monitor_width", "monitor_height", "display_scaled", "display_result")
                def monitor(name, current=None):
                    return tuple((current or state())[name][key] for key in KEYS)
                before = state()
                assert monitor("left", before) == monitor("right", before) == (0, 0, False, ""), before
                revision = before["left"]["display_revision"]
                asked = (before["left"]["game_width"], before["left"]["game_height"])
                def report(name, rev, width, height, result):
                    return send(f"DEV_SIDE_DISPLAY {name} {rev} {width} {height} {result}",
                                lambda s: s[name]["display_result"] == result and
                                (s[name]["monitor_width"], s[name]["monitor_height"]) == (width, height))
                reported = report("left", revision, *asked, "applied")
                assert monitor("left", reported) == (*asked, False, "applied"), reported
                assert monitor("right", reported) == (0, 0, False, ""), "a report on left reached right"
                # Deferred for the current revision: a game runs, so the monitor kept its size and is scaled.
                reported = report("left", revision, 3840, 2160, "deferred")
                assert monitor("left", reported) == (3840, 2160, True, "deferred"), reported
                assert monitor("right", reported) == (0, 0, False, ""), reported
                # A deferred report without a size still says scaled; a size of 0 is none.
                assert monitor("left", report("left", revision, 0, 0, "deferred")) == (0, 0, True, "deferred")
                # Applied for the same revision once the game has exited: the monitor is the size asked for.
                assert monitor("left", report("left", revision, *asked, "applied")) == (*asked, False, "applied")
                # The monitor is not the size asked for, whatever the result: scaled. The size asked for decides.
                other = (asked[0] // 2, asked[1])
                assert monitor("left", report("left", revision, *other, "applied")) == (*other, True, "applied")
                assert monitor("left", report("left", revision, *other, "failed")) == (*other, True, "failed")
                assert monitor("left", report("left", revision, *asked, "failed")) == (*asked, False, "failed")
                # A deferred report for another revision than the current one is not the current one's: the sizes decide.
                assert monitor("left", report("left", revision + 1, *asked, "deferred")) == (*asked, False, "deferred")
                assert monitor("left", report("left", revision + 1, *other, "deferred")) == (*other, True, "deferred")
                # What is asked for moves with a layout change, the revision with it: the monitor that was the
                # size asked for is not any more until the report on the new revision says it is.
                report("left", revision, *asked, "applied")
                moved = send("LAYOUT horizontal", lambda s: s["layout"] == "horizontal")
                assert moved["left"]["display_revision"] == revision + 1, moved
                assert (moved["left"]["game_width"], moved["left"]["game_height"]) == (1280, 360), moved
                assert monitor("left", moved) == (*asked, True, "applied"), moved
                reported = report("left", revision + 1, 1280, 360, "applied")
                assert monitor("left", reported) == (1280, 360, False, "applied"), reported
                reported = report("left", revision + 1, *asked, "deferred")
                assert monitor("left", reported) == (*asked, True, "deferred"), reported
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                report("left", revision + 2, *asked, "applied")
                # Both sides carry their own report; the right one starts empty and ends up on its own size.
                assert monitor("right") == (0, 0, False, ""), state()
                right_asked = (state()["right"]["game_width"], state()["right"]["game_height"])
                reported = report("right", state()["right"]["display_revision"], *right_asked, "applied")
                assert monitor("right", reported) == (*right_asked, False, "applied"), reported
                # Reports that are not well formed change nothing.
                settled = state()
                for command in ("DEV_SIDE_DISPLAY left 1 100 100 bogus", "DEV_SIDE_DISPLAY middle 1 100 100 applied",
                                "DEV_SIDE_DISPLAY left 1 -1 100 applied", "DEV_SIDE_DISPLAY left 1 100 16385 applied",
                                "DEV_SIDE_DISPLAY left 1 100 applied", "DEV_SIDE_DISPLAY left 1 100 100 applied extra",
                                "DEV_SIDE_DISPLAY left", "DEV_SIDE_DISPLAY "):
                    control.sendto(command.encode(), str(root / "hud-ipc.sock"))
                time.sleep(.15)
                assert state() == settled, "a report that is not well formed changed the state"

                # Run the controller state machine through the real menu IPC.
                spec = importlib.util.spec_from_file_location('controller_menu',
                    project / 'packaging/steamos/dosplex-controller-menu.py')
                menu_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(menu_module)
                # Both orders of the ratio buttons are walked with the D-pad:
                # side by side lists 1:1, 4:3, 16:9 and top/bottom 16:9, 4:3, 1:1.
                # The audio checks below need the top/bottom geometry, so it comes last.
                for layout, listed in (("vertical", ["1:1", "4:3", "16:9"]), ("horizontal", ["16:9", "4:3", "1:1"])):
                    send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    send('OVERLAY close', lambda s: not s['overlay_visible'])
                    for owner in ('left', 'right'):
                        other = 'right' if owner == 'left' else 'left'
                        def menu_send(action):
                            control.sendto(f'MENU {owner} {action}'.encode(), str(root / 'hud-ipc.sock'))
                            time.sleep(.04)
                        menu = menu_module.ControllerMenu(menu_send,
                            lambda: state()['overlay_visible'] and state()['overlay_owner'] == owner)
                        menu.event(1, 314, 1, 0)
                        menu.event(3, 17, -1, .1)
                        menu.tick(1.2)
                        menu.event(1, 314, 0, 1.3)
                        menu.event(3, 17, 0, 1.4)
                        wait_for(lambda s: s['overlay_visible'] and s['overlay_owner'] == owner)
                        # The other controller cannot move or close this menu, and Guest's cannot take
                        # Main's. (Main's chord takes Guest's menu: see the precedence checks below.)
                        for action in ('back', 'toggle', 'select') if other != state()['primary_side'] else ('back', 'select'):
                            control.sendto(f'MENU {other} {action}'.encode(), str(root / 'hud-ipc.sock'))
                        time.sleep(.04)
                        assert state()['overlay_visible'] and state()['overlay_owner'] == owner
                        menu.event(3, 17, 1, 2)  # Down from Display tab to Native.
                        menu.event(3, 17, 0, 2.1)
                        # Right steps through the ratio buttons in the displayed order.
                        order = ratio_order(state()[owner]['width'], state()[owner]['height'])
                        assert order == listed, (layout, order)
                        for step, aspect in enumerate(order):
                            menu.event(3, 16, 1, 3 + step)  # Right to the next ratio button.
                            menu.event(3, 16, 0, 3.1 + step)
                            menu.event(1, 304, 1, 3.2 + step)  # A/Cross selects.
                            menu.event(1, 304, 0, 3.3 + step)
                            expected = fitting_aspect(state()[owner]['width'], state()[owner]['height'], aspect)
                            wait_for(lambda s: (s[owner]['pending_game_width'], s[owner]['pending_game_height']) == expected and s[owner]['pending_game_aspect'] == aspect)
                        menu.event(1, 305, 1, 7)  # B/Circle closes.
                        wait_for(lambda s: not s['overlay_visible'])
                        send(f'DISPLAY_CANCEL {owner}', lambda s: not s[owner]['display_pending'])

                # Precedence. There is one overlay: Main always takes it over, Guest never takes Main's,
                # and in Full screen Guest has none. CHORD is the controller chord (MENU <side> toggle) as a
                # test hook; Ctrl+Shift+M goes through the same decision.
                def hook(command):
                    control.sendto(command.encode(), str(root / "hud-ipc.sock"))
                shortcut = b"K 29 1\nK 42 1\nK 50 1\nK 50 0\nK 42 0\nK 29 0\n"
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                assert state()["primary_side"] == "left"
                send("CHORD right", lambda s: s["overlay_visible"] and s["overlay_owner"] == "right")
                guest = state()["right"]
                # Guest's 640x720 half at scale 1: the Session tab, then Reset my side, which asks first.
                def click_until(x, y, predicate):
                    """A click before the next frame has built the hit regions lands on nothing."""
                    for _ in range(10):
                        time.sleep(.05)
                        pointer(x, y, True)
                        deadline = time.monotonic() + .3
                        while time.monotonic() < deadline:
                            if predicate(state()):
                                return
                            time.sleep(.01)
                    raise AssertionError(f"the click at {x},{y} did nothing: {state()}")
                click_until(guest["x"] + 542, guest["y"] + 175, lambda s: s["overlay_tab"] == 2)
                click_until(guest["x"] + 320, guest["y"] + 240,
                            lambda s: s["overlay_confirmation"] and s["overlay_owner"] == "right")
                taken = send("CHORD left", lambda s: s["overlay_visible"] and s["overlay_owner"] == "left")
                assert not taken["overlay_confirmation"] and taken["overlay_tab"] == 0, taken
                assert "Main took the overlay from Guest (CHORD test hook)" in (root / "compositor.log").read_text()
                # Main's is open: Guest's chord, controller toggle and Ctrl+Shift+M change nothing.
                hook("CHORD right")
                hook("MENU right toggle")
                pointer(guest["x"] + 300, guest["y"] + 300)
                input_client.sendall(shortcut)
                time.sleep(.3)
                current = state()
                assert current["overlay_visible"] and current["overlay_owner"] == "left", current
                diagnostics = (root / "compositor.log").read_text()
                for source in ("CHORD test hook", "controller", "remote keyboard"):
                    assert f"Ignoring Guest's overlay request ({source}): Main's overlay is open" in diagnostics, source
                # Main's own chord closes it; Main's Ctrl+Shift+M takes Guest's as the chord does.
                send("CHORD left", lambda s: not s["overlay_visible"])
                send("CHORD right", lambda s: s["overlay_visible"] and s["overlay_owner"] == "right")
                pointer(100, 300)
                input_client.sendall(shortcut)
                wait_for(lambda s: s["overlay_visible"] and s["overlay_owner"] == "left")
                assert "Main took the overlay from Guest (remote keyboard)" in (root / "compositor.log").read_text()
                send("CHORD left", lambda s: not s["overlay_visible"])
                # Full screen shows only Main, so Guest's chord and toggle do nothing.
                send("LAYOUT single", lambda s: s["layout"] == "single")
                hook("CHORD right")
                hook("MENU right toggle")
                time.sleep(.3)
                assert not state()["overlay_visible"], state()
                assert "Ignoring Guest's overlay request (CHORD test hook): Full screen shows only Main" in (root / "compositor.log").read_text()
                send("CHORD left", lambda s: s["overlay_visible"] and s["overlay_owner"] == "left")
                send("CHORD left", lambda s: not s["overlay_visible"])

                # OVERLAY <side>, the hud-ipc command, decides the same way. It opens and never closes: Main's
                # takes Guest's (and Guest's pending confirmation ends), Guest's never takes or keeps Main's, a
                # side's own request leaves its overlay open, and Full screen shows only Main.
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                send("OVERLAY right", lambda s: s["overlay_visible"] and s["overlay_owner"] == "right")
                guest = state()["right"]
                click_until(guest["x"] + 542, guest["y"] + 175, lambda s: s["overlay_tab"] == 2)
                click_until(guest["x"] + 320, guest["y"] + 240,
                            lambda s: s["overlay_confirmation"] and s["overlay_owner"] == "right")
                hook("OVERLAY right")  # Guest's own request: its overlay and its confirmation stay
                time.sleep(.2)
                assert state()["overlay_visible"] and state()["overlay_confirmation"], state()
                taken = send("OVERLAY left", lambda s: s["overlay_visible"] and s["overlay_owner"] == "left")
                assert not taken["overlay_confirmation"] and taken["overlay_tab"] == 0, taken
                assert "Main took the overlay from Guest (OVERLAY command)" in (root / "compositor.log").read_text()
                hook("OVERLAY right")
                time.sleep(.3)
                current = state()
                assert current["overlay_visible"] and current["overlay_owner"] == "left", current
                assert "Ignoring Guest's overlay request (OVERLAY command): Main's overlay is open" in \
                    (root / "compositor.log").read_text()
                hook("OVERLAY left")  # Main's own request: it is open already and stays
                time.sleep(.3)
                current = state()
                assert current["overlay_visible"] and current["overlay_owner"] == "left", current
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                send("LAYOUT single", lambda s: s["layout"] == "single")
                hook("OVERLAY right")
                time.sleep(.3)
                assert not state()["overlay_visible"], state()
                assert "Ignoring Guest's overlay request (OVERLAY command): Full screen shows only Main" in \
                    (root / "compositor.log").read_text()
                send("OVERLAY left", lambda s: s["overlay_visible"] and s["overlay_owner"] == "left")
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                send("LAYOUT horizontal", lambda s: s["layout"] == "horizontal")

                # Exercise the active audio tab against a slow recording helper.
                # Actual routing and synthetic signal behavior have a separate
                # real PipeWire integration test.
                current = send(f"OVERLAY {client_side}", lambda s: s["overlay_visible"])
                send("ROUTE __dosplex_remote_input__ follow", lambda s: s["input_leap_route"] == "follow")
                time.sleep(.04)
                px, py, panel_width, panel_height = 280, (372 if client_side == "right" else 12), 720, 336
                tab_width = (panel_width - 32 - 24) // 4
                pointer(px + 16 + 2 * (tab_width + 8) + tab_width // 2, py + 85, True)
                wait_for(lambda s: s["audio_busy"])
                # A slow pactl/helper must not stall unrelated display IPC.
                send("RESOLUTION left 720p", lambda s: s["left"]["pending_game_width"] == 1280)
                wait_for(lambda s: not s["audio_busy"] and s["audio_revision"] > 0)
                def audio_click(x, y, expected):
                    time.sleep(.04)  # Allow the next frame to rebuild hit regions.
                    revision = state()["audio_revision"]
                    pointer(x, y, True)
                    wait_for(lambda s: s["audio_revision"] > revision and not s["audio_busy"])
                    calls = [json.loads(line) for line in (root / "audio-calls.jsonl").read_text().splitlines()]
                    assert expected in calls, calls
                audio_click(px + panel_width // 2, py + 220,
                            ["overlay", client_side, "output", "TV speakers"])
                pointer(px + panel_width // 2, py + 125, True)  # Microphone
                audio_click(px + panel_width // 2, py + 182,
                            ["overlay", client_side, "input", "Headset microphone"])
                audio_click(px + panel_width - 60, py + 125,
                            ["overlay", client_side, "input", "none"])
                key(1)
                wait_for(lambda s: not s["overlay_visible"])
                # Main enables sharing with confirmation; cancelling sends no
                # change. Guest joins/leaves through its own menu without any
                # permission to alter the shared mode.
                def open_audio(owner):
                    send(f"OVERLAY {owner}", lambda s: s['overlay_visible'] and s['overlay_owner']==owner)
                    time.sleep(.2)  # the frame after the open builds the hit regions the tab click needs
                    oy=12 if owner=='left' else 372
                    pointer(280+16+2*(tab_width+8)+tab_width//2,oy+85,True)
                    wait_for(lambda s: s['overlay_tab']==3 and not s['audio_busy'])
                    pointer(340,oy+125,True)  # Output
                    return oy
                def mix_calls():
                    return [json.loads(l) for l in (root/'audio-calls.jsonl').read_text().splitlines()
                            if '"mix"' in l]
                oy=open_audio('left');before=mix_calls()
                pointer(550,oy+183,True)  # Full mix
                wait_for(lambda s:s['overlay_confirmation'])
                assert mix_calls()==before
                key(1);wait_for(lambda s:not s['overlay_confirmation'])
                assert mix_calls()==before
                pointer(550,oy+183,True);wait_for(lambda s:s['overlay_confirmation'])
                audio_click(820,oy+214,['overlay','left','mix','full'])
                for strength,px in [('gentle',405),('strong',874),('balanced',640)]:
                    pointer(820,oy+183,True);wait_for(lambda s:s['overlay_confirmation'])
                    before=mix_calls()
                    key(1);wait_for(lambda s:not s['overlay_confirmation'])
                    assert mix_calls()==before
                    pointer(820,oy+183,True);wait_for(lambda s:s['overlay_confirmation'])
                    audio_click(px,oy+256,['overlay','left','mix',strength])
                send('OVERLAY close',lambda s:not s['overlay_visible'])
                oy=open_audio('right')
                audio_click(820,oy+183,['overlay','right','mix','join'])
                audio_click(460,oy+183,['overlay','right','mix','leave'])
                assert not any(c[1]=='right' and c[3] in ('off','full','separated','gentle','balanced','strong') for c in mix_calls())
                send('OVERLAY close',lambda s:not s['overlay_visible'])
                send("DISPLAY_CANCEL left", lambda s: not s["left"]["display_pending"])

                # Exercise output object destruction/recreation with an actual
                # mapped client; session window state must survive every cycle.
                probe = subprocess.Popen([str(root / "probe"), "--test-control"], env=env,
                                         stdin=subprocess.PIPE, stdout=log, stderr=log)
                wait_for(lambda s: s[client_side]["has_app"])
                # Reuse the same xdg role after unmapping, including its
                # initial bufferless commit/configure handshake. Gamescope
                # may do this when a game exits and Steam regains its window.
                for _ in range(3):
                    probe.stdin.write(b'U'); probe.stdin.flush()
                    wait_for(lambda s: not s[client_side]['has_app'])
                    time.sleep(.2)  # Drain configures queued before the unmap.
                    probe.stdin.write(b'R'); probe.stdin.flush()
                    wait_for(lambda s: s[client_side]['has_app'])
                    assert probe.poll() is None, 'remapping killed the client'
                # Gamescope 3.16.30 waits again after attaching null to an
                # already unmapped backing plane. The initial configure may
                # have been consumed while the plane was still hidden.
                for _ in range(3):
                    probe.stdin.write(b'U'); probe.stdin.flush()
                    wait_for(lambda s: not s[client_side]['has_app'])
                    time.sleep(.2)
                    configures = (root / 'compositor.log').read_text().count('TOPLEVEL_CONFIGURE ')
                    probe.stdin.write(b'N'); probe.stdin.flush()
                    wait_for(lambda s: (root / 'compositor.log').read_text().count('TOPLEVEL_CONFIGURE ') > configures)
                    time.sleep(.2)
                    configured = (root / 'compositor.log').read_text().count('CONFIGURED ')
                    probe.stdin.write(b'C'); probe.stdin.flush()
                    wait_for(lambda s: (root / 'compositor.log').read_text().count('CONFIGURED ') > configured)
                    wait_for(lambda s: s[client_side]['has_app'])
                # Gamescope before 3.16.30 attaches a buffer to its unmapped toplevel again without the
                # bufferless commit and configure that xdg-shell asks for (SetVisible(false), then
                # SetVisible(true) when the next window appears). The side takes it as the remap.
                tolerated = (root / 'compositor.log').read_text().count('accepting it as the remap')
                for _ in range(3):
                    probe.stdin.write(b'U'); probe.stdin.flush()
                    wait_for(lambda s: not s[client_side]['has_app'])
                    probe.stdin.write(b'D'); probe.stdin.flush()
                    wait_for(lambda s: s[client_side]['has_app'])
                    time.sleep(.1)
                    assert probe.poll() is None, 'a remap without a new configure killed the client'
                assert (root / 'compositor.log').read_text().count('accepting it as the remap') == tolerated + 3
                pointer(100, 100 if client_side == "left" else 500)
                input_client.sendall(b"K 30 1\n")
                wait_for(lambda s: re.search(r"KEY serial=.*key=30 state=1", (root / "compositor.log").read_text()) is not None)
                input_client.close()
                wait_for(lambda s: re.search(r"KEY serial=.*key=30 state=0", (root / "compositor.log").read_text()) is not None)
                input_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                input_client.connect(str(root / "dosplex-primary-input.sock"))
                # Moving the remote pointer to the other seat releases held
                # keys on this seat, preserves its focus, and sends no new keys.
                input_client.sendall(b"K 32 1\n")
                wait_for(lambda s: "key=32 state=1" in (root / "compositor.log").read_text())
                leaves = (root / "compositor.log").read_text().count("KEYBOARD_LEAVE")
                pointer(100, 500 if client_side == "left" else 100)
                wait_for(lambda s: "key=32 state=0" in (root / "compositor.log").read_text())
                key(33)
                diagnostics = (root / "compositor.log").read_text()
                assert not re.search(r"KEY serial=.*key=33", diagnostics), diagnostics
                assert diagnostics.count("KEYBOARD_LEAVE") == leaves, "other seat stole keyboard focus"
                pointer(100, 100 if client_side == "left" else 500)
                key(34)
                wait_for(lambda s: "key=34 state=1" in (root / "compositor.log").read_text())
                input_client.sendall(b"W 0 15\n")
                wait_for(lambda s: "POINTER_AXIS axis=0 value=15.00" in (root / "compositor.log").read_text())
                send(f"OVERLAY {client_side}", lambda s: s['overlay_visible'])
                input_client.sendall(b"W 0 23\n")
                time.sleep(.05)
                assert "POINTER_AXIS axis=0 value=23.00" not in (root / "compositor.log").read_text(), "remote wheel leaked through overlay"
                send("OVERLAY close", lambda s: not s['overlay_visible'])
                # Closing a controller overlay must return application focus
                # without requiring that user's pointer or keyboard to move.
                # The other user's input while it is open must not change that.
                for close_command in (f"MENU {client_side} back", "OVERLAY close", f"MENU {client_side} toggle"):
                    send(f"OVERLAY {client_side}", lambda s: s['overlay_visible'])
                    pointer(100, 500 if client_side == "left" else 100)
                    key(35)
                    enters = (root / "compositor.log").read_text().count("KEYBOARD_ENTER")
                    send(close_command, lambda s: not s['overlay_visible'])
                    wait_for(lambda s: (root / "compositor.log").read_text().count("KEYBOARD_ENTER") > enters)
                pointer(100, 100 if client_side == "left" else 500)
                input_client.sendall(b"K 17 1\n")
                wait_for(lambda s: "key=17 state=1" in (root / "compositor.log").read_text())
                boundary = len((root / "compositor.log").read_text())
                input_client.sendall(b"K 29 1\nK 42 1\nK 50 1\nK 50 0\nK 42 0\nK 29 0\n")
                wait_for(lambda s: s['overlay_visible'])
                key(1)  # Escape closes, W remains held
                wait_for(lambda s: not s['overlay_visible'])
                key(46)  # suppressed until neutral
                input_client.sendall(b"K 17 0\n")
                key(33)
                wait_for(lambda s: "key=33 state=1" in (root / "compositor.log").read_text())
                menu_events = (root / "compositor.log").read_text()[boundary:]
                assert 'KEYBOARD_LEAVE' in menu_events, menu_events
                assert not re.search(r'KEY serial=.*key=(1|46|50) ', menu_events), menu_events
                assert not re.search(r'KEY serial=.*key=17 state=0', menu_events), menu_events
                boundary = len((root / "compositor.log").read_text())
                input_client.sendall(b"K 42 1\nK 42 0\n")
                def modifiers_released(_):
                    events = (root / "compositor.log").read_text()[boundary:]
                    return re.search(r'MODIFIERS .*depressed=1 .*MODIFIERS .*depressed=0 ', events, re.S) is not None
                wait_for(modifiers_released)
                # Former remote-only restart/scaling shortcuts belong to the
                # application. Verify complete key pairs with Ctrl and Super.
                for modifier in (29, 125):
                    for code in (38, 19, 31):  # L, R, S
                        boundary = len((root / "compositor.log").read_text())
                        input_client.sendall(f"K {modifier} 1\nK 42 1\nK {code} 1\nK {code} 0\nK 42 0\nK {modifier} 0\n".encode())
                        def chord_arrived(_):
                            events = (root / "compositor.log").read_text()[boundary:]
                            return (re.search(rf'KEY serial=.*key={code} state=1', events) and
                                    re.search(rf'KEY serial=.*key={code} state=0', events))
                        wait_for(chord_arrived)
                        assert not state()['overlay_visible']
                # Pointer lock, relative motion, the grab across sides and the layout re-home, with the real
                # client. The layout is top/bottom: the left side's region is the top half, the right side's
                # the bottom half, and Main (the left side) is the top one.
                assert state()["layout"] == "horizontal", state()
                region_y = 0 if client_side == "left" else 360
                other_y = 360 if client_side == "left" else 0

                def probe_command(letter):
                    probe.stdin.write(letter.encode())
                    probe.stdin.flush()

                def probe_output():
                    return (root / "compositor.log").read_text()

                def wait_probe(since, pattern, timeout=5):
                    deadline = time.monotonic() + timeout
                    while True:
                        found = re.search(pattern, probe_output()[since:], re.M)
                        if found:
                            return found
                        assert probe.poll() is None, "the probe exited"
                        assert time.monotonic() < deadline, f"never saw {pattern!r}:\n{probe_output()[since:][-3000:]}"
                        time.sleep(.01)

                def move_until(since, pattern, x, y):
                    # The scene drops a closed menu's nodes in the next frame, and until then the point
                    # is over them, not over the application's surface: move again until it is in.
                    for attempt in range(8):
                        input_client.sendall(f"M {x + attempt % 2} {y}\n".encode())
                        try:
                            return wait_probe(since, pattern, timeout=.4)
                        except AssertionError:
                            assert probe.poll() is None, "the probe exited"
                    return wait_probe(since, pattern)

                def never_probe(since, pattern, settle=.2):
                    time.sleep(settle)
                    found = re.search(pattern, probe_output()[since:], re.M)
                    assert not found, f"saw {pattern!r}:\n{probe_output()[since:][-3000:]}"

                # A client that locks the pointer gets relative motion and no absolute motion.
                at = len(probe_output())
                input_client.sendall(f"M 100 {region_y + 100}\nM 120 {region_y + 100}\n".encode())
                wait_probe(at, r"^POINTER_MOTION x=120\.00 y=100\.00$")
                probe_command("L")
                wait_probe(at, r"^POINTER_LOCKED$")
                at = len(probe_output())
                input_client.sendall(f"R 7 -3\nM 150 {region_y + 120}\nB 272 1\nB 272 0\nW 0 1\n".encode())
                wait_probe(at, r"^POINTER_AXIS axis=0 value=1\.00$")
                events = probe_output()[at:]
                assert "RELATIVE dx=7.00 dy=-3.00 dx_unaccel=7.00 dy_unaccel=-3.00" in events, events
                assert "POINTER_MOTION" not in events, "a locked pointer got a position:\n" + events
                assert "POINTER_BUTTON button=272 state=1" in events and "POINTER_BUTTON button=272 state=0" in events, events
                # The other seat is unaffected: nothing here reached it, and the client's own end of the lock
                # restores absolute motion.
                probe_command("l")
                wait_probe(at, r"^LOCK_ENDED$")
                at = len(probe_output())
                input_client.sendall(f"M 150 {region_y + 120}\nW 0 2\n".encode())
                wait_probe(at, r"^POINTER_AXIS axis=0 value=2\.00$")
                assert "POINTER_MOTION x=150.00 y=120.00" in probe_output()[at:], probe_output()[at:]

                # The menu ends a lock; a persistent lock is back when the pointer enters again.
                at = len(probe_output())
                probe_command("L")
                wait_probe(at, r"^POINTER_LOCKED$")
                send(f"OVERLAY {client_side}", lambda s: s["overlay_visible"])
                wait_probe(at, r"^POINTER_UNLOCKED$")
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                at = len(probe_output())
                move_until(at, r"^POINTER_LOCKED$", 130, region_y + 100)
                assert re.search(r"^POINTER_ENTER ", probe_output()[at:], re.M), probe_output()[at:]

                # A lock's cursor position hint is honoured when the client ends the lock: the pointer
                # continues from (200,150) in the client's surface.
                at = len(probe_output())
                probe_command("h")
                wait_probe(at, r"^HINT_SET$")
                probe_command("l")
                wait_probe(at, r"^LOCK_ENDED$")
                at = len(probe_output())
                input_client.sendall(b"R 5 -5\nW 0 3\n")
                wait_probe(at, r"^POINTER_AXIS axis=0 value=3\.00$")
                assert "POINTER_MOTION x=205.00 y=145.00" in probe_output()[at:], probe_output()[at:]

                # A confined pointer moves, but only inside the region the client asked for: the
                # rectangle of 120x80 at (200,100) in its surface.
                at = len(probe_output())
                probe_command("F")
                wait_probe(at, r"^POINTER_CONFINED$")
                input_client.sendall(f"M 250 {region_y + 150}\nR 500 0\nR 0 500\nR -900 -900\n".encode())
                wait_probe(at, r"^POINTER_MOTION x=200\.00 y=100\.00$")
                events = probe_output()[at:]
                for position in ("x=250.00 y=150.00", "x=319.00 y=150.00", "x=319.00 y=179.00"):
                    assert f"POINTER_MOTION {position}" in events, events
                probe_command("f")
                wait_probe(at, r"^CONFINE_ENDED$")

                # A press stays with the seat that got it: the release follows it, and a release with
                # nothing pressed is not passed on.
                at = len(probe_output())
                input_client.sendall(f"M 100 {region_y + 100}\nB 272 1\n".encode())
                wait_probe(at, r"^POINTER_BUTTON button=272 state=1 x=100\.00 y=100\.00$")
                input_client.sendall(f"M 100 {other_y + 100}\n".encode())
                never_probe(at, r"^POINTER_BUTTON button=272 state=0")
                input_client.sendall(b"B 272 0\n")
                wait_probe(at, r"^POINTER_BUTTON button=272 state=0 x=100\.00 y=100\.00$")
                input_client.sendall(b"B 272 0\n")
                time.sleep(.2)
                assert len(re.findall(r"^POINTER_BUTTON button=272 state=0", probe_output()[at:], re.M)) == 1, probe_output()[at:]

                # A layout change re-homes the pointer: the cursor goes into Main's region, both seats lose
                # their pointer focus, the remote input follows the cursor again and Main's application is
                # entered afresh. The remote pointer starts over the client: in the right side's region (the
                # bottom half) for Guest's client and in Main's (the top half) for Main's. A client that the
                # pointer is not over was told so when it left (the grab above ended with the leave), and is
                # not told again.
                at = len(probe_output())
                if client_side == "right":
                    input_client.sendall(b"M 300 500\n")
                    wait_probe(at, r"^POINTER_(ENTER|MOTION) x=300\.00 y=140\.00$")  # the bottom half is its region
                else:
                    input_client.sendall(b"M 300 100\n")
                    wait_probe(at, r"^POINTER_(ENTER|MOTION) x=300\.00 y=100\.00$")  # the top half is its region
                at = len(probe_output())
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                wait_probe(at, r"^POINTER_LEAVE$")
                if client_side == "left":
                    # Main's region is now the left half, which holds (300,100): the same point, entered afresh.
                    wait_probe(at, r"^POINTER_ENTER x=300\.00 y=100\.00$")
                    assert probe_output()[at:].index("POINTER_LEAVE") < probe_output()[at:].index("POINTER_ENTER")
                    input_client.sendall(b"B 272 1\nB 272 0\n")
                    wait_probe(at, r"^POINTER_BUTTON button=272 state=0 x=300\.00 y=100\.00$")
                else:
                    # Guest's client is left without pointer focus and the click goes to Main, not to it.
                    input_client.sendall(b"B 272 1\nB 272 0\n")
                    never_probe(at, r"^POINTER_(ENTER|BUTTON)")
                    # A point of its own region brings its pointer back.
                    input_client.sendall(b"M 100 100\nM 700 100\n")
                    wait_probe(at, r"^POINTER_ENTER x=60\.00 y=100\.00$")
                # A cursor outside Main's new region is clamped into it.
                at = len(probe_output())
                input_client.sendall(b"M 900 650\n")
                if client_side == "right":
                    wait_probe(at, r"^POINTER_MOTION x=260\.00 y=650\.00$")  # the right half is its region
                else:
                    time.sleep(.1)
                at = len(probe_output())
                send("LAYOUT horizontal", lambda s: s["layout"] == "horizontal")
                if client_side == "left":
                    # Main's region is the top half again: (900,650) becomes (900,359).
                    wait_probe(at, r"^POINTER_ENTER x=900\.00 y=359\.00$")
                else:
                    wait_probe(at, r"^POINTER_LEAVE$")
                    never_probe(at, r"^POINTER_ENTER")

                # Check assignment at the real Wayland client after moving Main.
                role = "main" if client_side == "left" else "guest"
                send(f"ROUTE remote {role}", lambda s: s["input_leap_route"] == client_side)
                for placement in ("left", "right"):
                    send(f"GUEST {placement}", lambda s: s["guest_physical_side"] == placement)
                    boundary = len((root / "compositor.log").read_text())
                    key(35)
                    wait_for(lambda s: "key=35 state=1" in (root / "compositor.log").read_text()[boundary:])
                    assert probe.poll() is None, "moving Main restarted its client"
                send("ROUTE remote follow", lambda s: s["input_leap_route"] == "follow")

                send(f"RESOLUTION {client_side} 1080p", lambda s: s[client_side]["pending_game_width"] == 1920)
                send(f"DISPLAY_APPLY {client_side}", lambda s: s[client_side]["game_width"] == 1920)
                preserved = state()[client_side]["display_revision"]
                for width, height in [(1280, 720), (1920, 1080), (3840, 2160), (1280, 720)]:
                    send("DEV_OUTPUT_REMOVE", lambda s: not s["output_connected"])
                    assert probe.poll() is None
                    assert state()[client_side]["has_app"]
                    current = send(f"DEV_OUTPUT_ADD {width} {height}", lambda s: s["output_connected"] and s["left"]["width"] == width)
                    assert current[client_side]["has_app"]
                    assert current[client_side]["game_width"] == 1920
                    assert current[client_side]["display_revision"] == preserved
                    assert probe.poll() is None
                    info = subprocess.run(["wayland-info"], env=env, capture_output=True, text=True, timeout=5, check=True).stdout
                    assert f"width: {width} px, height: {height // 2} px" in info, info
                # Real pointer hit tests at4K in each layout; both owners are
                # covered by the left/right invocations of this integration test.
                send("DEV_OUTPUT_REMOVE", lambda s: not s["output_connected"])
                send("DEV_OUTPUT_ADD 3840 2160", lambda s: s["output_connected"])
                for layout in ("vertical", "horizontal"):
                    current = send(f"LAYOUT {layout}", lambda s: s["layout"] == layout)
                    side = current[client_side]
                    scale = max(1, min(2, side["width"] / 744, side["height"] / 564))
                    logical_w, logical_h = int(side["width"] / scale), int(side["height"] / scale)
                    panel_w, panel_h = min(720, logical_w - 24), min(540, logical_h - 24)
                    px, py = (logical_w - panel_w) // 2, (logical_h - panel_h) // 2
                    def point(lx, ly):
                        pointer(side["x"] + round(lx * scale), side["y"] + round(ly * scale), click=True)
                    send(f"OVERLAY {client_side}", lambda s: s["overlay_visible"])
                    time.sleep(.06)  # Wait for the newly opened menu to render hit regions.
                    point(px + 16 + (panel_w - 56) // 8, py + 85)  # Display tab
                    quarter = (panel_w - 56) // 4
                    order = ratio_order(side["width"], side["height"])
                    point(px + 16 + quarter // 2, py + 150)  # Native, always first
                    wait_for(lambda s: s[client_side]["pending_native_resolution"])
                    for aspect in ("1:1", "16:9"):
                        # The ratio buttons follow Native in the order listed for this layout.
                        point(px + 16 + (order.index(aspect) + 1) * (quarter + 8) + quarter // 2, py + 150)
                        expected = fitting_aspect(side["width"], side["height"], aspect)
                        wait_for(lambda s: (s[client_side]["pending_game_width"], s[client_side]["pending_game_height"]) == expected and s[client_side]["pending_game_aspect"] == aspect)
                    point(px + panel_w - 30, py + 26)  # Close
                    wait_for(lambda s: not s["overlay_visible"])
                    send(f"DISPLAY_CANCEL {client_side}", lambda s: not s[client_side]["display_pending"])
                send("DEV_OUTPUT_REMOVE", lambda s: not s["output_connected"])
                send("DEV_OUTPUT_ADD 1280 720", lambda s: s["output_connected"])
                probe.terminate()
                probe.wait(timeout=5)
                probe = None
                wait_for(lambda s: not s[client_side]["has_app"])
                diagnostics = (root / "compositor.log").read_text()
                assert f"Side toplevel mapped on {client_side} side: title='DosPlex Key Probe'" in diagnostics, diagnostics[-3000:]
                assert f"App surface unmapped on {client_side} side: toplevel title='DosPlex Key Probe'" in diagnostics, diagnostics[-3000:]

                # A side's new toplevel that never maps (the Steam Machine's third Reset of 2026-10-20): the map
                # watchdog logs what is known after DOSPLEX_MAP_WATCHDOG_MS and configures the toplevel again once.
                # The held probe acknowledges its configures but attaches no buffer until it is told to map.
                at = len((root / "compositor.log").read_text())
                probe = subprocess.Popen([str(root / "probe"), "--test-control", "--hold-map"], env=env,
                                         stdin=subprocess.PIPE, stdout=log, stderr=log)
                wait_for(lambda s: f"Set app_surface for {client_side} side" in (root / "compositor.log").read_text()[at:])
                wait_for(lambda s: (root / "compositor.log").read_text()[at:].count("TOPLEVEL_CONFIGURE ") >= 1)
                warning = re.compile(
                    rf"WARN: the {client_side} side's toplevel 'DosPlex Key Probe' has not mapped \d+ ms after it was assigned \(check 1\): "
                    rf"initial commit made, configure sent, scheduled serial \d+, \d+ configure\(s\) not acked, last acked serial \d+, "
                    rf"\d+ commit\(s\), buffer none, surface 0x0, it is the side's app surface")
                resent = re.compile(
                    rf"Re-sent configure \d+x\d+ to the {client_side} side's toplevel 'DosPlex Key Probe' from the map watchdog")
                wait_for(lambda s: resent.search((root / "compositor.log").read_text()[at:]) is not None)
                assert probe.poll() is None, "the held probe exited"
                # The configures counted up to the warning, which the watchdog logs right before it re-sends the
                # configure, and the configure that the probe reports after the re-sent line: that one is the watchdog's.
                text = (root / "compositor.log").read_text()[at:]
                warned, sent = warning.search(text), resent.search(text)
                assert warned is not None and warned.start() < sent.start(), text[-3000:]
                configures = text[:warned.start()].count("TOPLEVEL_CONFIGURE ")
                assert configures >= 1, text[-3000:]
                wait_for(lambda s: "TOPLEVEL_CONFIGURE " in (root / "compositor.log").read_text()[at:][sent.end():])
                assert "Side toplevel mapped on" not in (root / "compositor.log").read_text()[at:]
                probe.stdin.write(b'M'); probe.stdin.flush()
                wait_for(lambda s: f"Side toplevel mapped on {client_side} side: title='DosPlex Key Probe'" in (root / "compositor.log").read_text()[at:])
                wait_for(lambda s: s[client_side]["has_app"])
                time.sleep(1.2)   # past a second watchdog period: a mapped toplevel is not warned about again
                assert (root / "compositor.log").read_text()[at:].count("WARN: the") == 1, (root / "compositor.log").read_text()[at:]

                # A second toplevel that never maps while the first is the side's app surface: the watchdog warns about
                # it but re-sends it nothing, since activating it could take the activation from the toplevel shown.
                at_other = len((root / "compositor.log").read_text())
                with (root / "other-probe.log").open("w") as other_out:
                    other = subprocess.Popen([str(root / "probe"), "--test-control", "--hold-map"], env=env,
                                             stdin=subprocess.PIPE, stdout=other_out, stderr=log)
                    try:
                        skipped = (f"No configure re-sent to the {client_side} side's toplevel 'DosPlex Key Probe' from the map "
                                   f"watchdog: it is not the side's app surface")
                        wait_for(lambda s: skipped in (root / "compositor.log").read_text()[at_other:])
                        text = (root / "compositor.log").read_text()[at_other:]
                        other_warning = re.search(
                            rf"WARN: the {client_side} side's toplevel 'DosPlex Key Probe' has not mapped \d+ ms after it was "
                            rf"assigned \(check 1\): .*, it is not the side's app surface", text)
                        assert other_warning is not None and other_warning.start() < text.index(skipped), text[-3000:]
                        assert "Re-sent configure" not in text, text[-3000:]
                        configures = (root / "other-probe.log").read_text().count("TOPLEVEL_CONFIGURE ")
                        assert configures >= 1, (root / "other-probe.log").read_text()
                        time.sleep(0.3)
                        assert (root / "other-probe.log").read_text().count("TOPLEVEL_CONFIGURE ") == configures, \
                            (root / "other-probe.log").read_text()
                        assert "Re-sent configure" not in (root / "compositor.log").read_text()[at_other:]
                        assert other.poll() is None, "the second held probe exited"
                        assert state()[client_side]["has_app"], "the first probe is no longer the side's app"
                    finally:
                        other.terminate()
                        other.wait(timeout=5)
                probe.terminate()
                probe.wait(timeout=5)
                probe = None
                wait_for(lambda s: not s[client_side]["has_app"])
                wait_for(lambda s: f"App surface unmapped on {client_side} side: toplevel title='DosPlex Key Probe'" in (root / "compositor.log").read_text()[at:])

                # The control socket (production path of `dosplex control layout`): fixed binary records on a
                # SOCK_SEQPACKET socket, nothing else is accepted. Layout numbers: 1 single, 2 vertical, 3 horizontal.
                def control_request(record):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
                        client.settimeout(3)
                        client.connect(str(root / "control.sock"))
                        client.send(record)
                        return client.recv(64)

                for number, name in ((2, "vertical"), (3, "horizontal"), (1, "single"), (2, "vertical")):
                    reply = control_request(b"DPXC" + bytes([1, 1, number, 0]))
                    assert reply == b"DPXR" + bytes([1, 0, number, 0]), (name, reply)
                    assert state()["layout"] == name, state()
                # Guest full screen is number 4; it publishes "guest" and puts Main in Standby.
                assert control_request(b"DPXC" + bytes([1, 1, 4, 0])) == b"DPXR" + bytes([1, 0, 4, 0])
                assert state()["layout"] == "guest" and state()["left"]["activity"] == "standby", state()
                assert control_request(b"DPXC" + bytes([1, 1, 2, 0])) == b"DPXR" + bytes([1, 0, 2, 0])
                assert state()["layout"] == "vertical" and state()["left"]["activity"] == "active", state()
                held = state()
                for label, record, status in (
                        ("text", b"LAYOUT single", 1), ("wrong magic", b"XXXX" + bytes([1, 1, 1, 0]), 1),
                        ("layout 0", b"DPXC" + bytes([1, 1, 0, 0]), 1), ("layout 5", b"DPXC" + bytes([1, 1, 5, 0]), 1),
                        ("unknown command", b"DPXC" + bytes([1, 9, 1, 0]), 1),
                        ("reserved byte", b"DPXC" + bytes([1, 1, 1, 7]), 1),
                        ("too short", b"DPXC" + bytes([1, 1, 1]), 1), ("too long", b"DPXC" + bytes([1, 1, 1, 0, 0]), 1),
                        ("empty", b"", 1), ("version 2", b"DPXC" + bytes([2, 1, 1, 0]), 3)):
                    reply = control_request(record)
                    assert reply[:4] == b"DPXR" and reply[5] == status and reply[6] == 2, (label, reply)
                    time.sleep(0.05)
                    assert state() == held, f"control request {label!r} changed the state"
                # Several requests in a row on one session, then back to the layout the rest of the test expects.
                for number in (3, 1, 2):
                    assert control_request(b"DPXC" + bytes([1, 1, number, 0]))[5] == 0
                wait_for(lambda s: s["layout"] == "vertical")

                before = state()
                control.sendto(b"LAYOUT invalid", str(root / "hud-ipc.sock"))
                time.sleep(0.05)
                assert state() == before, "invalid layout changed state"
                # A reader must never see truncated JSON during repeated writes.
                send("LAYOUT vertical", lambda s: s["layout"] == "vertical")
                for n in range(100):
                    side = "left" if n % 2 else "right"
                    send("OVERLAY close", lambda s: not s["overlay_visible"])
                    send(f"OVERLAY {side}", lambda s: s["overlay_visible"] and s["overlay_owner"] == side)
                send("OVERLAY close", lambda s: not s["overlay_visible"])
                send("LAYOUT single", lambda s: s["layout"] == "single")
                assert not list(root.glob(".state-*")), "state temporary files leaked"
                log.flush()
                log.seek(0)
                diagnostics = log.read()
                assert "ERROR: AddressSanitizer" not in diagnostics and "runtime error:" not in diagnostics, diagnostics
                print("PASS: per-client monitor modes, layouts, overlay input, display apply/cancel, atomic state and reconnect with mapped client")
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read()[-12000:])
                raise
            finally:
                if probe is not None:
                    probe.terminate()
                    probe.wait(timeout=5)
                input_client.close()
                control.close()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                pid_file = root / "audio-helper.pid"
                if pid_file.exists():
                    helper_pid = int(pid_file.read_text())
                    try:
                        os.kill(helper_pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise AssertionError("Audio helper survived compositor shutdown")
                assert proc.returncode == 0, f"compositor shutdown failed: {proc.returncode}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--client-side", choices=("left", "right"), default="left")
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.client_side)
