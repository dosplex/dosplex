#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Apply live display changes to this side's nested Gamescope; never restart anything.

The side supervisor owns the display contract (the game monitor size and the scaler) but runs
as root outside the side, so it cannot reach the side's nested X server. This follower runs
inside the side as the side user. The session launcher starts it beside Steam under Gamescope,
which gives it Gamescope's DISPLAY and ends it together with the session.

Every 0.5 s it asks the supervisor for the live contract over the side's control socket
(DOSPLEX_SESSION_CONTROL, SOCK_SEQPACKET). It talks only to a server whose uid is
DOSPLEX_SESSION_CONTROL_SERVER_UID (default 0), checked before anything is sent:

    {"instance": INSTANCE, "display": "get"}
      -> {"ok": true, "live": {"x", "y", "width", "height", "game_width", "game_height",
                               "scale_mode", "display_revision", "layout"}}

When display_revision differs from the last revision it settled (the first poll included), it
applies the contract to Gamescope through two properties of the nested X root window and then
reports the outcome:

    {"instance": INSTANCE, "display": "report", "revision": N, "game_width": W,
     "game_height": H, "result": "applied" | "failed" | "deferred", "detail": "..."}
      -> {"ok": true}

1. Game monitor. GAMESCOPE_XWAYLAND_MODE_CONTROL is set to the four 32-bit cardinals
   "0, W, H, 1" (Xwayland server 0, width, height, allow super resolution). Gamescope resizes
   the nested screen and deletes the property when it is done (at once if the screen already
   is W x H). The follower waits for the property to disappear and for `xrandr --current` to
   show W x H, at most 10 s. It writes nothing when the screen already is W x H.
   Steam watches the nested screen and puts it back to 1920x1080 when a size is too large for it (measured on a
   Steam Deck, SteamOS 3.10.1, Gamescope 3.17: up to 1920x1200 pixels stays, 1920x1210 and more is undone after
   0.3 to 0.9 s). So after every change the follower keeps looking at the screen for HOLD_SECONDS (1.5 s) and
   counts the size as set only when the screen kept it for all of that time. The look is left out when nothing was
   written (the screen already had the size) and while a game runs (deferred, see below).
   A size that was undone is tried again smaller, in the same shape as W x H. The pixel budgets are, in this order,
   the one learned before (see below), 2,304,000 (1920x1200) and three more that are each 15 % smaller. For a
   budget B the width is the largest even number up to sqrt(B * W / H), the height the largest even number up to
   width * H / W, and the width goes down by 2 until width * height is within B. A candidate that is not smaller
   than W x H in pixels, repeats an earlier one, or is below 320 in either dimension is skipped. Each candidate is
   applied like any size, hold look included. The first that holds is the game monitor: the scaler is set for it
   and the report says "applied" with that size as game_width and game_height; its detail names the budget and
   both sizes. When none holds the report says "failed", gives the size the screen has then and the detail
   "Steam set the screen back to AxB". The budget of the candidate that held is remembered while the follower runs,
   for the order only: it is the first budget tried among the smaller sizes. Every new revision tries the
   contract's own size first (a short moment at the full size is accepted), because Steam's setting can change
   while the follower runs. Nothing here is a known limit of Steam: other devices or versions may have another
   budget or none, and a target that holds is never lowered.
   The follower compares the screen with the size it asked for in this revision, never with the contract's size
   after a fallback, so a screen that holds a smaller size is not applied again at every poll. A new revision
   starts fresh and keeps only the remembered budget.
   Keeping the size. Steam can also set the screen once by itself, seconds after the hold has passed (its setting
   "Enable Native Resolution on External Displays" puts the nested screen at the output's full size about 5 to 7 s
   after the side started). The follower remembers the size it put in place for the settled revision (the
   contract's size or the fallback size) and, every CHECK_SECONDS (2 s), while no game runs, reads the screen
   once. A screen of another size is set again like any size (property, wait, hold look, fallback candidates,
   then scaler and filter) and logged as "the screen changed to AxB by itself; setting WxH again (K of 3)". A new
   report is sent only when the size that holds differs from the one reported before. After REASSERT_LIMIT (3)
   such resets in one revision, a screen that changes again is left alone and reported once as "failed", with the
   size it has and the detail "Steam keeps setting the screen to AxB", until the revision changes. While a game
   runs the screen is not read, not touched and not counted; the check goes on when the game has ended. A new
   revision starts the count and the remembered size again.
2. Scaler. GAMESCOPE_NEW_SCALING_SCALER is set to one 32-bit cardinal chosen by scale_mode.

A running game keeps its monitor. A screen that is resized under a game makes the game see its monitor
change, and not every game survives that. So the monitor is frozen while a game runs and Gamescope scales the
picture instead: step 1 is deferred.

  * GAMESCOPE_FOCUSABLE_APPS on the nested root (a 32-bit CARDINAL for each app) lists the apps Gamescope can
    present. 769 is Steam's own interface and any other app id is a game. A property that is missing or
    malformed names no game. The follower reads it at every poll.
  * With a game running and a target size that differs from the screen, step 1 is left out: the screen keeps
    its size, step 2 still runs and Gamescope scales the picture. The report says "deferred" and gives the size
    the monitor keeps as game_width and game_height; its detail ends with the target size.
  * A deferred revision stays pending, not settled, so every poll looks again. When no game runs any more the
    follower applies the latest contract as usual and reports "applied" for that revision. The deferred report
    is made once for each revision and set of running games, not at every poll.
  * A target that already is the screen size is applied as usual, with a game running or not.
  * When GAMESCOPE_FOCUSABLE_APPS cannot be read (a tool that fails) nothing is known about the games, so the
    contract waits for the next poll. That differs from a property that is missing, which is read as no game.

Waking Steam. A side whose Steam has been idle shows its screensaver and the account as Away, and a game that
is started into it runs behind Steam's own window: Gamescope keeps Steam in front (GAMESCOPE_FOCUSED_APP stays
769) and the player sees nothing. So when a game has appeared in GAMESCOPE_FOCUSABLE_APPS and
GAMESCOPE_FOCUSED_APP is still 769 WAKE_AFTER_SECONDS (3 s) later, the follower sends one key press to the nested
X server with `xdotool key Escape` (WAKE_KEY) and logs "woke Steam for app N".

  * That happens once for each game while it stays in the list. A game that leaves the list and returns is new.
  * A game that takes the focus first is never woken for, not even if Steam comes back to the front later, and
    neither is a game that was running when the follower started.
  * xdotool runs through the same tool runner as the other X tools. A missing xdotool, or a key press that
    fails, is logged once and is all that happens.

Holding Steam's idle sleep. Each side's Steam keeps its own idle clock. After five minutes without input it shows
its screensaver, and after its sleep time (an hour on mains power, and this client has no setting for it) it asks
the system to sleep. That does not fit two players on one screen: Main's Steam would put the machine to sleep under
a Guest who is playing, and Guest's Steam may not put the machine to sleep at all, and Steam does not take that
answer: it stays on its sleep picture until the side is started again (seen on a Steam Deck with client
1795048523: "Issuing Suspend Command" after 3600 s, InteractiveAuthorizationRequired, the picture for six hours).
So while the layout shows two players, or Guest alone, the follower tells Steam every KEEP_AWAKE_SECONDS (3000 s,
fifty minutes) that somebody is there, the way the nested Gamescope does when it sees input: it writes
GAMESCOPE_INPUT_COUNTER on the nested root, one more than the value it reads. Steam took that as input when
measured (a screensaver that an Escape key and org.freedesktop.ScreenSaver.SimulateUserActivity left up went
away in 3 s).

  * The screensaver is Steam's own and is left to it: it comes after five minutes and goes with the player's next
    input. The price of the hold is that the picture of a side nobody uses comes back for five minutes after each
    write, so for five minutes in every fifty.
  * Steam's own lock (org.freedesktop.ScreenSaver.Inhibit, which Steam owns on the side's session bus) was tried
    first and does not do it: with the lock held for 70 minutes both sides still asked for sleep at 3600 s.
  * With Main alone on the screen (layout ALONE_LAYOUT, `single`) nothing is written: Main's Steam idles and
    sleeps the machine as it does without DosPlex. Guest's follower is frozen with its side then.
  * The first write goes out with the first cycle, and with the first cycle after Main was alone, so a Steam that
    was close to its sleep time when the second player came does not ask a moment later.
  * A layout the supervisor does not name counts as two players: holding is the safe side.
  * Gamescope only ever writes the property and never reads it, so a value written here is at worst written
    again by Gamescope after one more input, which costs that one input in Steam's idle clock.
  * A read or write that fails is logged once and tried again KEEP_AWAKE_RETRY_SECONDS (60 s) later.
  * The cost: with two players on the screen the machine does not go to sleep by itself. Sleep from Main's menu
    and the power button are untouched.

Focus refresh. Under Gamescope 3.16.23 Steam's gamepad UI (a Chromium window) never counts itself active: Gamescope
gives the window the X input focus once, at map time, before Chromium is ready, so the UI has no DOM focus, its Play
button stays grey and the footer shows only "MENU". Gamescope 3.16.30 does not have the problem. One X focus cycle
repairs it: the input focus goes to the root window and straight back to Steam's window (no key or pointer event).

  * When GAMESCOPE_FOCUSED_WINDOW has named the same window for FOCUS_SETTLE_SECONDS (0.75 s) and the app Gamescope
    presents (GAMESCOPE_FOCUSED_APP) is STEAM_APP_ID, the follower runs the cycle once with `xdotool windowfocus`
    (ROOT, then WINDOW) and logs "focus refresh: window 0x...". The first poll counts as a change, so a window that
    is focused when the follower starts gets it too, and so does Steam's window when it takes the focus back from a
    game, because the game's window was the focus in between.
  * Once for each window that takes the focus, and never twice within FOCUS_REFRESH_SECONDS (1 s). Its own effects
    on the focus properties change nothing here: an empty GAMESCOPE_FOCUSED_WINDOW is ignored, and the same window
    that comes back is the same focus.
  * Never while a game is presented (any app but STEAM_APP_ID): a game can pause when it loses the focus. A focus
    that moves to a game, or to another window, before the settle is over gets no cycle.
  * Done for every Gamescope version, because it is harmless on 3.16.30. The root window is read once with
    `xwininfo -root -int`. A tool that fails, or a window that is gone (BadWindow), is logged once and the cycle is
    not repeated for that window.

Gamescope 3.16.30, the version DosPlex targets, defines both (checked in its source at that tag):
  * src/steamcompmgr.cpp, handle_property_notify, gamescopeXWaylandModeControl: a CARDINAL
    property of exactly four values {server index, width, height, allowSuperRes}; the property
    is deleted again by configure_win when the root window changes size.
  * src/steamcompmgr.cpp, handle_property_notify, gamescopeNewScalingScaler: one CARDINAL that
    must not exceed GamescopeUpscaleScaler::STRETCH.
  * src/main.hpp, enum class GamescopeUpscaleScaler: AUTO = 0, INTEGER = 1, FIT = 2, FILL = 3,
    STRETCH = 4. `--scaler fit|stretch` (src/main.cpp, parse_upscaler_scaler) maps to the same
    enum, so a session that starts with `--scaler fit` and one switched live agree.
  Gamescope reads a deleted GAMESCOPE_NEW_SCALING_SCALER as 0 (AUTO), so this property is only
  ever set, never removed. The scaling filter (GAMESCOPE_NEW_SCALING_FILTER, one CARDINAL, src/main.hpp
  enum class GamescopeUpscaleFilter: LINEAR = 0, NEAREST = 1, FSR = 2, NIS = 3, PIXEL = 4) is set after the scaler:
  NEAREST when the picture is scaled up by a whole number (see scaling_filter), LINEAR otherwise. Those values
  come from the Gamescope source as described to the author; they were not re-read against the 3.16.30 tag here.

Window fit. On every poll, and so right after an apply, the follower also keeps one more property of
the nested root, GAMESCOPE_FORCE_WINDOWS_FULLSCREEN, with which Gamescope resizes the focused window to
the screen size. Nothing else resizes a resizable game window: a game that restores its saved window size
(Valheim's 1920x2160) comes up on half of a Full screen side, and the FIT scaler cannot enlarge a window that
already spans the output height. The property acts on whichever window has the focus, at every focus pass,
so the follower uses it as a one-shot inside one poll: it writes 1 only for the focused window that needs the
fit, looks at that window for at most FIT_SECONDS (250 ms), and writes 0 before the poll goes on. The property
is never 1 between two polls, so nothing chosen for one window can resize another that takes the focus later.

  * One `xprop -root GAMESCOPE_FOCUSED_WINDOW GAMESCOPE_FOCUSED_APP_GFX` call gives the focus window (a 32-bit
    CARDINAL, empty when none is focused) and its app id. GAMESCOPE_FOCUSED_APP_GFX is the focus window's app,
    the window Gamescope resizes; GAMESCOPE_FOCUSED_APP is the input focus window's app, which is Steam's (769)
    while a Steam overlay has the input, so it is not used here. Only when the root has no
    GAMESCOPE_FOCUSED_APP_GFX is the window's own STEAM_GAME read instead. While neither names an app (the
    property is empty, or it is absent and the window has no STEAM_GAME) nothing is decided: Gamescope resizes
    only a window with an app id. The wait is logged once for each window.
  * A property that may be on when a poll starts is switched off first, before anything else: that is only the
    case after a 0 that could not be written. The first poll also reads the property itself and switches it
    off when it is on (a follower that ended during a one-shot). A follower that is stopped by a signal
    switches off a property that may be on, also when the signal came during the write of the 1: the property
    counts as on from just before that write is sent, not from the moment xprop answers.
  * Steam's own window (app 769) is always left alone: Gamescope's focus pass resizes it itself whenever it has
    the focus, with the property on or off, so the property would add nothing for it and could only reach the
    window that takes the focus next.
  * `xprop -id WINDOW WM_NORMAL_HINTS STEAM_GAME` gives the window's size hints. A window that states one size
    (a non-zero minimum equal to its maximum) is left alone: forcing such a window made Alien Breed Impact
    fight Gamescope in a loop (commit 265b47e removed the command-line flag that forced every window), and the
    Steam Machine's Absolum window (fixed 3840x2160) was resized by a value left on for Steam's window. Every
    other window is resizable, a window without WM_NORMAL_HINTS included, as in Gamescope's get_size_hints.
  * A resizable window whose size (`xdotool getwindowgeometry --shell WINDOW`) already is the screen size
    (`xrandr --current`) is left alone. For any other the focus is read once more and, when it has not moved,
    the one-shot runs in three steps, inside the same poll (fit_once):
      1. The property is marked as possibly on, and 1 is written.
      2. The follower looks at the focus, the window's hints and the window's size, about every
         FIT_STEP_SECONDS (20 ms), for at most FIT_SECONDS (250 ms) from the start of the write.
      3. 0 is written in a `finally`: as soon as the window has the screen size, as soon as it states one size
         (hints published late), as soon as the focus has moved, when a look fails, when the follower is
         stopped, or when FIT_SECONDS are over and the window did not take the size.
    Gamescope does not resize a resizable window back when the property goes off, so the window keeps the
    screen size. The window is then decided for that screen. A window that lost the focus is not decided.
  * After every display revision the follower applies, the screen is read again; a decided resizable window
    whose screen size has changed is decided again the same way (refit on a monitor or layout change). A window
    that takes the focus again is decided again.
  * Every decision is logged once. A tool that fails is logged once; when it fails during a one-shot, the 0 is
    written first and the window stays undecided, so the step is tried again at the next poll. A 0 that cannot
    be written leaves the property marked as possibly on, and the next poll (or the stop) writes it before
    anything else. Without xdotool no window is ever forced.

  What is left: while the property is 1 (from the write of the 1 to the 0: a few tens of milliseconds when the
  window takes the size at the first look, at most FIT_SECONDS plus the look in progress and the write of the 0),
  a window that takes the focus is resized in that focus pass, before the follower sees it; the next look finds
  the focus moved and writes the 0. The focus is read in separate xprop requests from the window's own properties
  and from the write, so a focus change in the few milliseconds between the last read and the write is not seen
  either. A tool that hangs, and does not fail, keeps the property on until its timeout (TOOL_TIMEOUT_SECONDS).
  And the forced window itself is resized once: a game that re-creates its swapchain at that moment depends on
  the Gamescope WSI layer it loaded (T-417); the fit does not avoid that resize, it only bounds it to one.

Gamescope (src/steamcompmgr.cpp), read in two local source copies: 3.16.30, the Deck's version, and 3.16.23.6,
the copy used for T-417 (line numbers 3.16.30 / 3.16.23.6). Whether that copy matches the Steam Machine's
3.16.23.6 binary has not been checked on the Machine.
  * handle_property_notify reads GAMESCOPE_FORCE_WINDOWS_FULLSCREEN as `!!get_prop(...)` on the root window,
    so a non-zero first cardinal is on and a deleted property is off, and calls MakeFocusDirty (7502-7506 /
    6302-6306). It reads the value when the event is handled, so a 1 that a 0 follows before Gamescope looks is
    never seen: the one-shot keeps the 1 until the window shows the new size, and does not write the 0 sooner.
  * The focus pass (xwayland_ctx_t::DetermineAndApplyFocus, 4907-4931 / 3873-3899) acts on the focus window
    (ctx->focus.focusWindow) only when it has an app id (win_has_game_id: appID != 0). With the property on, or
    for a window that is "fullscreen" (window_is_fullscreen: Steam's own window, appID 769, or one that asked for
    fullscreen; 1951 / 1496), it resizes the window to the root size when its size differs (Steam's window to the
    height capped by GAMESCOPE_STEAM_MAX_HEIGHT). With the property off it resizes only a window whose hints
    state one size (sizeHintsSpecified) back to that size. configure_win marks the focus dirty when the root
    window changes size; with the property off that leaves a resizable window at its size, so the follower
    fits it again.
  * get_size_hints (5586-5615 / from 4403) sets sizeHintsSpecified only for non-zero, equal minimum and maximum
    sizes; a window without WM_NORMAL_HINTS is not fixed.
  * A window's appID comes from its process (GetAppIdFromPid / get_appid_from_pid: the SteamLaunch AppId of the
    reaper it runs under, 6040 / 4946), and a non-zero STEAM_GAME on the window overrides it (5801-5815 /
    4615-4630), so a game window need not carry STEAM_GAME.
  * GAMESCOPE_FOCUSED_APP_GFX holds the focus window's appID and GAMESCOPE_FOCUSED_APP the input focus window's
    (5436-5474 / 4255-4291), both written in the same pass as GAMESCOPE_FOCUSED_WINDOW, with no items when the
    app id is 0. The input focus is a Steam overlay window with inputFocusMode in place of the focus window
    (4636-4641 / 3744).
  Another local copy labelled 3.16.23, of unverified origin, has a focus pass that does not read the property at
  all; the 3.16.23.6 copy does, as quoted above.

Failures are reported, not retried: the next revision applies again. Any error on the control
socket or in an X tool is logged once, never ends the follower and never changes anything else.
A control exchange that fails or times out (3 s) is asked again at most 1 s after that poll began instead of
at a longer poll interval, up to three times in a row, and then at the normal poll until one succeeds; at
the default 0.5 s poll the next poll is already that soon.
The follower signals, kills and restarts nothing (its X tools are its own short-lived children).
It ends on SIGTERM, or when its display has been unreachable for several checks in a row.
A missing DISPLAY or control endpoint at start is a usage error (exit status 2).
"""
import argparse
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import namedtuple

POLL_SECONDS = 0.5
APPLY_TIMEOUT_SECONDS = 10.0
TOOL_TIMEOUT_SECONDS = 5.0
CONTROL_TIMEOUT_SECONDS = 3.0
CONTROL_RETRY_SECONDS = 1.0   # a failed control exchange is asked again at most this long after its poll began
CONTROL_RETRIES = 3           # such quick retries in a row; then the normal poll applies until an exchange succeeds
WAIT_STEP_SECONDS = 0.1
HOLD_SECONDS = 1.5            # a screen has to keep the size it was set to for this long before the size counts as set
STEAM_PIXEL_BUDGET = 2304000  # 1920x1200: the most pixels Steam left alone when measured; a first guess, not a limit
BUDGET_SHRINK_PERCENT = 85    # each further budget is this many percent of the one before
BUDGET_STEPS = 3              # how many smaller budgets follow STEAM_PIXEL_BUDGET
MIN_MONITOR_SIDE = 320        # a fallback screen is never smaller than this in either dimension
MAX_DETAIL = 200
CHECK_SECONDS = 2.0           # a settled screen is looked at this often, while no game runs
REASSERT_LIMIT = 3            # how often one revision sets a screen right that changed by itself
DISPLAY_PROBE_EVERY = 4    # idle polls between checks that the display still answers
DISPLAY_LOST_AFTER = 3     # consecutive unreachable results before the follower ends
MAX_REPLY_BYTES = 65536
MAX_TEXT = 160

MODE_PROPERTY = 'GAMESCOPE_XWAYLAND_MODE_CONTROL'
SCALER_PROPERTY = 'GAMESCOPE_NEW_SCALING_SCALER'
FOCUS_PROPERTY = 'GAMESCOPE_FOCUSED_WINDOW'
FORCE_FULLSCREEN_PROPERTY = 'GAMESCOPE_FORCE_WINDOWS_FULLSCREEN'
HINTS_PROPERTY = 'WM_NORMAL_HINTS'
APP_PROPERTY = 'STEAM_GAME'  # a window's own app id, read only when the root has no GAMESCOPE_FOCUSED_APP_GFX
FOCUSABLE_APPS_PROPERTY = 'GAMESCOPE_FOCUSABLE_APPS'
FOCUSED_APP_PROPERTY = 'GAMESCOPE_FOCUSED_APP'           # the input focus window's app: what the player is shown
FOCUS_APP_PROPERTY = 'GAMESCOPE_FOCUSED_APP_GFX'         # the focus window's app: the window Gamescope resizes
FIT_SECONDS = 0.25           # the longest the forced fullscreen stays on for one window, from the write of 1
FIT_STEP_SECONDS = 0.02      # the pause between two looks at that window while the property is on
STEAM_APP_ID = 769           # Steam's own interface: the one focusable app that is not a game
MAX_LISTED_GAMES = 4         # app ids that a log line and a report detail name; with more, the rest is cut
WAKE_AFTER_SECONDS = 3.0     # Steam has to stay in front this long after a game has appeared to be woken for it
WAKE_KEY = 'Escape'          # the key press that wakes an idle Steam; it closes nothing on Steam's home page
FOCUS_SETTLE_SECONDS = 0.75  # a window has to stay the focus this long before Steam's window gets its focus cycle
FOCUS_REFRESH_SECONDS = 1.0  # two focus cycles are at least this far apart
KEEP_AWAKE_SECONDS = 3000.0  # Steam is told that somebody is there this often; it asks for sleep after 3600 s idle
KEEP_AWAKE_RETRY_SECONDS = 60.0   # a telling that failed is tried again this much later
INPUT_COUNTER_PROPERTY = 'GAMESCOPE_INPUT_COUNTER'   # the nested Gamescope's count of inputs, which Steam watches
ALONE_LAYOUT = 'single'      # Main alone on the screen: the one layout in which Steam's idle sleep is not held
# GamescopeUpscaleScaler in Gamescope 3.16.30, src/main.hpp: AUTO 0, INTEGER 1, FIT 2, FILL 3, STRETCH 4.
FILTER_PROPERTY = 'GAMESCOPE_NEW_SCALING_FILTER'
FILTER_LINEAR, FILTER_NEAREST = 0, 1
SCALER_VALUES = {'fit': 2, 'stretch': 4, 'zoom': 2}   # Zoom is the compositor's crop over Gamescope's Fit scaler

# The bounds the supervisor's state reader enforces (dosplex-display-state.py).
MIN_DIMENSION = 1
MAX_DIMENSION = 16384
MAX_REVISION = 4294967295
MAX_WINDOW_ID = 4294967295
MAX_CARDINAL = 4294967295    # the largest 32-bit cardinal, which is also the largest app id

Contract = namedtuple('Contract', 'revision width height scale_mode region_width region_height',
                      defaults=(0, 0))


def scaling_filter(scale_mode, monitor, region):
    """The Gamescope scaling filter for a monitor (width, height) shown in a region (width, height).

    NEAREST gives each game pixel a crisp block, and is only right for a whole-number scale: Keep ratio (fit) when the
    scale that Gamescope's fit uses, the smaller of the two ratios, is a whole number; Stretch when each axis
    scales by a whole number. Fit (zoom) is scaled by the DosPlex compositor, so Gamescope stays linear. A region
    that is unknown, or smaller than the monitor, is linear.
    """
    (mw, mh), (rw, rh) = monitor, region
    if scale_mode == 'zoom' or min(mw, mh, rw, rh) <= 0:
        return FILTER_LINEAR
    if scale_mode == 'stretch':
        whole = rw % mw == 0 and rh % mh == 0
    else:
        whole = (rw * mh <= rh * mw and rw % mw == 0) or (rh * mw < rw * mh and rh % mh == 0)
    return FILTER_NEAREST if whole else FILTER_LINEAR
# The size range a window states in WM_NORMAL_HINTS: (width, height) pairs, None for a size it does not state.
SizeHints = namedtuple('SizeHints', 'minimum maximum')
NO_HINTS = SizeHints(None, None)

_SCREEN = re.compile(r'^Screen\s+\d+:.*?\bcurrent\s+(\d+)\s+x\s+(\d+)', re.M)
_FOCUSED_WINDOW = re.compile(
    r'^' + FOCUS_PROPERTY + r'\(\w+\)(?:[ \t]*=[ \t]*(\d+)|:[ \t]*window id # (0x[0-9a-fA-F]+))[ \t]*$', re.M)
_MINIMUM_SIZE = re.compile(r'^[ \t]*program specified minimum size:[ \t]*(\d+) by (\d+)[ \t]*$', re.M)
_MAXIMUM_SIZE = re.compile(r'^[ \t]*program specified maximum size:[ \t]*(\d+) by (\d+)[ \t]*$', re.M)
_HINTS_PRESENT = re.compile(r'^' + HINTS_PROPERTY + r'\(WM_SIZE_HINTS\):', re.M)
_GEOMETRY_WIDTH = re.compile(r'^WIDTH=(\d+)[ \t]*$', re.M)
_GEOMETRY_HEIGHT = re.compile(r'^HEIGHT=(\d+)[ \t]*$', re.M)
_DISPLAY_UNREACHABLE = re.compile(r"(?:unable to|cannot|can't) open display", re.I)


class ToolError(Exception):
    """An X tool failed, was missing or ran out of time."""


class DisplayUnreachable(ToolError):
    """An X tool could not connect to the display."""


class ScreenSetBack(ToolError):
    """Steam undid every size that was tried; `size` is the screen as it is then."""
    def __init__(self, size):
        super().__init__('Steam set the screen back to %dx%d' % size)
        self.size = size


class ControlRefused(Exception):
    """The control server answered, but not with ok."""


def log(text):
    try:
        print('dosplex-display-follower: ' + text, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def clean(text):
    """One short printable line, safe for a log and for the report's detail field."""
    printable = ''.join(char if char.isprintable() else ' ' for char in str(text))
    return ' '.join(printable.split())[:MAX_TEXT]


def describe(error):
    reason = (error.strerror or str(error)) if isinstance(error, OSError) else str(error)
    return clean(type(error).__name__ + ': ' + reason)


def is_int(value):
    return type(value) is int


def parse_live(live):
    """Validate the live contract the supervisor returned; raises ValueError when it is unusable."""
    if not isinstance(live, dict):
        raise ValueError('the reply has no live contract')
    revision, width, height = live.get('display_revision'), live.get('game_width'), live.get('game_height')
    scale_mode = live.get('scale_mode')
    if not is_int(revision) or not 0 <= revision <= MAX_REVISION:
        raise ValueError('invalid display_revision')
    if any(not is_int(value) or not MIN_DIMENSION <= value <= MAX_DIMENSION for value in (width, height)):
        raise ValueError('invalid game size')
    if not isinstance(scale_mode, str) or scale_mode not in SCALER_VALUES:
        raise ValueError('invalid scale_mode')
    region = tuple(live.get(key) if is_int(live.get(key)) and MIN_DIMENSION <= live.get(key) <= MAX_DIMENSION else 0
                   for key in ('width', 'height'))
    return Contract(revision, width, height, scale_mode, *region)


def capped_size(target, budget):
    """The screen of the shape of target (width, height) that has at most `budget` pixels, or None when it is no
    candidate: not smaller than target, or below MIN_MONITOR_SIDE in a dimension."""
    width, height = target
    wide = math.isqrt(budget * width // height)
    wide -= wide % 2
    while wide >= 2:
        high = wide * height // width
        high -= high % 2
        if wide * high <= budget:
            break
        wide -= 2
    else:
        return None
    if wide * high >= width * height or min(wide, high) < MIN_MONITOR_SIDE:
        return None
    return wide, high


def describe_budget(budget):
    return '1920x1200' if budget == STEAM_PIXEL_BUDGET else str(budget)


def mode_control_values(width, height):
    """GAMESCOPE_XWAYLAND_MODE_CONTROL: Xwayland server index, width, height, allowSuperRes."""
    return [0, int(width), int(height), 1]


def xprop_set_argv(name, values):
    return ['xprop', '-root', '-f', name, '32c', '-set', name, ', '.join(str(int(value)) for value in values)]


def xprop_get_argv(*names):
    return ['xprop', '-root', *names]


def xprop_window_argv(window, *names):
    return ['xprop', '-id', '0x%x' % window, *names]


def xdotool_key_argv(key):
    return ['xdotool', 'key', key]


def xdotool_geometry_argv(window):
    return ['xdotool', 'getwindowgeometry', '--shell', '0x%x' % window]


def xwininfo_root_argv():
    return ['xwininfo', '-root', '-int']


def xdotool_windowfocus_argv(window):
    return ['xdotool', 'windowfocus', str(window)]


_ROOT_WINDOW = re.compile(r'^xwininfo: Window id: (\d+)\b', re.M)


def parse_root_window(text):
    """The root window id in `xwininfo -root -int` output ("xwininfo: Window id: 1023 (the root window)"), or None."""
    match = _ROOT_WINDOW.search(text)
    return int(match.group(1)) if match else None


def parse_screen_size(text):
    """The current screen size from `xrandr --current`, or None."""
    match = _SCREEN.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_focused_window(text):
    """The window id in `xprop -root GAMESCOPE_FOCUSED_WINDOW` output, or None when no window is focused.

    Gamescope writes the id as a 32-bit CARDINAL ("NAME(CARDINAL) = 12582915") and leaves the property
    empty ("NAME(CARDINAL) = ") when nothing is focused. The property can also be absent, or hold 0, which
    names no window. A value typed as a WINDOW ("NAME(WINDOW): window id # 0xc00003") reads the same way.
    """
    match = _FOCUSED_WINDOW.search(text)
    if not match:
        return None
    window = int(match.group(1)) if match.group(1) is not None else int(match.group(2), 16)
    return window if 0 < window <= MAX_WINDOW_ID else None


def parse_size_hints(text):
    """The SizeHints in `xprop -id WINDOW WM_NORMAL_HINTS` output, or None while the window has no such property.

    A window that has published the property ("WM_NORMAL_HINTS(WM_SIZE_HINTS):") states a minimum and a maximum
    size, or either, or none (NO_HINTS). One without it ("WM_NORMAL_HINTS:  not found.") states nothing, and the
    window fit treats it as resizable, as Gamescope does. The other lines xprop prints (location, size, resize
    increment, aspect ratio, base size, gravity) are not needed and are skipped.
    """
    if not _HINTS_PRESENT.search(text):
        return None
    minimum, maximum = _MINIMUM_SIZE.search(text), _MAXIMUM_SIZE.search(text)
    return SizeHints(tuple(int(value) for value in minimum.groups()) if minimum else None,
                     tuple(int(value) for value in maximum.groups()) if maximum else None)


def parse_window_app(text):
    """The app id in `xprop -id WINDOW STEAM_GAME` output ("STEAM_GAME(CARDINAL) = 769"), or None when not set."""
    apps = parse_cardinals(text, APP_PROPERTY)
    return apps[0] if apps is not None and len(apps) == 1 else None


def parse_window_geometry(text):
    """The window's size in `xdotool getwindowgeometry --shell WINDOW` output ("WIDTH=1920", "HEIGHT=2160"), or None."""
    width, height = _GEOMETRY_WIDTH.search(text), _GEOMETRY_HEIGHT.search(text)
    return (int(width.group(1)), int(height.group(1))) if width and height else None


def parse_cardinals(text, name):
    """The 32-bit cardinals in `xprop -root NAME` output ("NAME(CARDINAL) = 769, 892970"), as a list, or None.

    A property with no items ("NAME(CARDINAL) = ") is the empty list. None says that the property is not set
    ("not found"), has another type, or has anything on its line that is not a number from 0 to 4294967295.
    """
    match = re.search(r'^' + re.escape(name) + r'\(CARDINAL\)[ \t]*=[ \t]*([0-9]+(?:[ \t]*,[ \t]*[0-9]+)*)?[ \t]*$',
                      text, re.M)
    if not match:
        return None
    values = [int(item) for item in re.findall(r'[0-9]+', match.group(1) or '')]
    return values if all(value <= MAX_CARDINAL for value in values) else None


def next_input_counter(text):
    """The value to write to INPUT_COUNTER_PROPERTY after reading `xprop -root` output for it: one more than the
    value there, 1 when the property is not set or not understood, and 1 after the largest cardinal."""
    values = parse_cardinals(text, INPUT_COUNTER_PROPERTY)
    current = values[0] if values else 0
    return current + 1 if current < MAX_CARDINAL else 1


def running_games(text):
    """The app ids of the games in `xprop -root GAMESCOPE_FOCUSABLE_APPS` output, as a sorted list.

    Gamescope lists the apps it can present: Steam's own interface (STEAM_APP_ID, always) and, while one runs,
    the game. Any other app id is a game. A property that is missing or malformed names no game.
    """
    return sorted({app for app in parse_cardinals(text, FOCUSABLE_APPS_PROPERTY) or () if app != STEAM_APP_ID})


def parse_focused_app(text):
    """The app id in `xprop -root GAMESCOPE_FOCUSED_APP` output, or None when it names none.

    Gamescope writes the app it presents as one 32-bit CARDINAL ("NAME(CARDINAL) = 769"). A side whose Steam
    is idle presents STEAM_APP_ID, also while a game runs behind it. The property can be absent as well.
    """
    apps = parse_cardinals(text, FOCUSED_APP_PROPERTY)
    return apps[0] if apps is not None and len(apps) == 1 else None


def describe_games(games):
    """The games for a log line and a report detail: "game 892970", "games 262830, 892970"; the first few of many."""
    shown = ', '.join(str(app) for app in games[:MAX_LISTED_GAMES])
    if len(games) > MAX_LISTED_GAMES:
        shown += ', ...'
    return ('games ' if len(games) > 1 else 'game ') + shown


def parse_focus_app(text):
    """The app id Gamescope names for its focus window in `xprop -root GAMESCOPE_FOCUSED_APP_GFX` output.

    Returns (present, app): (False, None) when the property is absent or malformed, so the window's own STEAM_GAME
    is read instead; (True, None) when Gamescope names no app (no items, or 0); (True, app) otherwise.
    """
    apps = parse_cardinals(text, FOCUS_APP_PROPERTY)
    if apps is None or len(apps) > 1:
        return False, None
    return True, ((apps[0] or None) if apps else None)


def parse_force(text):
    """GAMESCOPE_FORCE_WINDOWS_FULLSCREEN in xprop output as Gamescope reads it (`!!get_prop(..., 0)`): 1 when its first
    cardinal is non-zero, else 0 (also when it is absent)."""
    values = parse_cardinals(text, FORCE_FULLSCREEN_PROPERTY)
    return 1 if values and values[0] else 0


def is_fixed(hints):
    """Whether a window states one size, as Gamescope's get_size_hints sees it: a minimum and a maximum, both
    non-zero and equal. A window without WM_NORMAL_HINTS (None) is resizable."""
    return (hints is not None and hints.minimum is not None and hints.minimum == hints.maximum
            and all(hints.minimum))


def describe_hints(hints):
    if hints is None:
        return 'no WM_NORMAL_HINTS'
    stated = (('minimum', hints.minimum), ('maximum', hints.maximum))
    sizes = ['%s %dx%d' % (label, *size) for label, size in stated if size]
    return ', '.join(sizes) or 'no size hints'


def describe_window(window, app, hints, size=None, screen=None):
    """What the window fit looked at, for the log."""
    text = 'window 0x%x, app %d, %s' % (window, app, describe_hints(hints))
    if size and screen:
        text += ', %dx%d, screen %dx%d' % (*size, *screen)
    return text


def property_absent(text):
    """True when `xprop -root NAME` says the property is not set, i.e. Gamescope has consumed it."""
    return ('not found' in text or 'no such atom' in text) and '(CARDINAL)' not in text


def run_tool(argv, environment):
    try:
        done = subprocess.run(argv, env=environment, capture_output=True, text=True, errors='replace',
                              timeout=TOOL_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolError(argv[0] + ' timed out') from None
    except OSError as error:
        raise ToolError(argv[0] + ' unavailable: ' + (error.strerror or str(error))) from None
    if done.returncode != 0:
        text = clean(done.stderr or done.stdout)
        if _DISPLAY_UNREACHABLE.search(text):
            raise DisplayUnreachable(text)
        raise ToolError(clean(argv[0] + ' failed: ' + text))
    return done.stdout


def control_exchange(endpoint, server_uid, payload, timeout=CONTROL_TIMEOUT_SECONDS):
    """One request and its one reply on the per-side control socket.

    The server's kernel-reported uid is checked before the request is sent, so the instance
    token never reaches an untrusted listener (the same check as the supervisor's client-mode).
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.settimeout(timeout)
        connection.connect(endpoint)
        _, uid, _ = struct.unpack('3i', connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        if uid != server_uid:
            raise PermissionError('session control server is not trusted')
        connection.sendall(json.dumps(payload, separators=(',', ':')).encode())
        raw = connection.recv(MAX_REPLY_BYTES)
    if not raw:
        raise ConnectionError('the control server closed without a reply')
    reply = json.loads(raw)
    if not isinstance(reply, dict):
        raise ValueError('the control reply is not an object')
    return reply


class Follower:
    def __init__(self, endpoint, instance, server_uid, poll=POLL_SECONDS,
                 apply_timeout=APPLY_TIMEOUT_SECONDS, environment=None,
                 control_timeout=CONTROL_TIMEOUT_SECONDS, retry_interval=CONTROL_RETRY_SECONDS):
        if not os.path.isabs(endpoint):
            raise ValueError('DOSPLEX_SESSION_CONTROL must be an absolute path')
        if len(instance) != 32:
            raise ValueError('DOSPLEX_SESSION_INSTANCE must be 32 characters')
        self.endpoint, self.instance, self.server_uid = endpoint, instance, server_uid
        self.poll, self.apply_timeout = poll, apply_timeout
        self.control_timeout, self.retry_interval = control_timeout, retry_interval
        # The tools read DISPLAY (and a possibly empty XAUTHORITY) from the session's own environment.
        self.environment = dict(os.environ if environment is None else environment, LC_ALL='C', LANG='C')
        self.display = self.environment.get('DISPLAY', '')
        if not self.display:
            raise ValueError('DISPLAY is not set')
        self.settled = None          # revision whose apply was last attempted, applied or failed
        self.budget = None           # the pixel budget of the smaller size that held last; None until one did
        self.fallback = None         # (budget, size) when the apply just run had to use a smaller size
        self.in_place = None         # (revision, size) the settled revision put in place; None while none is kept
        self.reported_size = None    # the game monitor size last reported for the settled revision
        self.reasserts = 0           # times the settled revision set a changed screen right again
        self.checked = 0.0           # when the screen was last compared with in_place
        self.deferred = None         # (revision, games) of the deferral last reported; None while none is pending
        self.games = None            # the games that ran at the last read, sorted; None before the first read
        self.watching = {}           # game -> time.monotonic() when it appeared and Steam has not been woken for it
        self.forced = None           # FORCE_FULLSCREEN_PROPERTY as left here: 1 while it may be on (from just before
                                     # a write of 1 until a 0 is written), else 0; None until the first fit read it
        self.fit_window = None       # the focused window the window fit is about, or None
        self.fit_decided = False     # fit_window has been decided (left alone or fitted) and the property is off
        self.fit_screen = None       # the screen size fit_window was decided for; None for one that is never refitted
        self.fit_waiting = False     # the wait for fit_window's app id has been logged
        self.refit = False           # a display revision was applied since fit_window was decided
        self.root_window = None      # the nested root's window id, read when the first focus cycle is due
        self.refresh_window = None   # the window the focus refresh is about: the last focus window seen
        self.refresh_since = 0.0     # when refresh_window was first seen as the focus
        self.refresh_done = False    # refresh_window has had its focus cycle (or its attempt)
        self.refresh_last = None     # when the last focus cycle ran
        self.layout = None           # the layout the supervisor last named, or None when it named none
        self.awake_next = None       # when Steam is next told that somebody is there; None: with the next cycle
        self.pending_report = None   # the latest report the supervisor has not acknowledged
        self.errors = {}             # kind -> last logged text, so a persistent error is logged once
        self.control_failed = False  # a control exchange of the cycle just run failed; a new revision may be behind it
        self.retries_left = CONTROL_RETRIES   # quick retries left in the current run of failed cycles
        self.cycles = 0
        self.display_failures = 0
        self.display_lost = False

    # -- logging that does not repeat itself -----------------------------------------------
    def note_error(self, kind, text):
        if self.errors.get(kind) != text:
            self.errors[kind] = text
            log(kind + ': ' + text)

    def clear_error(self, kind):
        if self.errors.pop(kind, None) is not None:
            log(kind + ': recovered')

    # -- the supervisor ----------------------------------------------------------------------
    def exchange(self, payload):
        return control_exchange(self.endpoint, self.server_uid, payload, self.control_timeout)

    def poll_contract(self):
        try:
            reply = self.exchange({'instance': self.instance, 'display': 'get'})
            if reply.get('ok') is not True:
                raise ControlRefused('the control server refused display get')
            contract = parse_live(reply.get('live'))
            layout = reply['live'].get('layout')
            self.layout = layout if isinstance(layout, str) else None
        except (OSError, ValueError, ControlRefused) as error:
            self.control_failed = True
            self.note_error('control', describe(error))
            return None
        self.clear_error('control')
        return contract

    def deliver_report(self):
        report = self.pending_report
        if report is None:
            return
        try:
            reply = self.exchange(report)
        except (OSError, ValueError) as error:
            self.control_failed = True
            self.note_error('report', describe(error))   # keep it and try again soon
            return
        self.pending_report = None
        self.clear_error('report')
        if reply.get('ok') is not True:
            log('revision %d: the control server rejected the report' % report['revision'])

    # -- X tools ---------------------------------------------------------------------------------
    def tool(self, argv):
        try:
            output = run_tool(argv, self.environment)
        except DisplayUnreachable:
            self.display_failures += 1
            raise
        self.display_failures = 0
        return output

    def screen_size(self):
        size = parse_screen_size(self.tool(['xrandr', '--current']))
        if size is None:
            raise ToolError('xrandr output has no screen size')
        return size

    def mode_request_absent(self):
        return property_absent(self.tool(xprop_get_argv(MODE_PROPERTY)))

    def wait_for_mode(self, target):
        deadline = time.monotonic() + self.apply_timeout
        while True:
            screen = self.screen_size()
            if screen == target and self.mode_request_absent():
                return
            if time.monotonic() >= deadline:
                request = 'cleared' if self.mode_request_absent() else 'pending'
                raise ToolError('timed out after %gs waiting for Gamescope to apply %dx%d (screen %dx%d, request %s)'
                                % (self.apply_timeout, target[0], target[1], screen[0], screen[1], request))
            time.sleep(WAIT_STEP_SECONDS)

    def hold_size(self, size):
        """Look at the screen for HOLD_SECONDS; None when it kept `size`, else the other size it showed."""
        end = time.monotonic() + HOLD_SECONDS
        while True:
            time.sleep(WAIT_STEP_SECONDS)
            screen = self.screen_size()
            if screen != size:
                return screen
            if time.monotonic() >= end:
                return None

    def set_monitor(self, size, screen):
        """Set the game monitor to size. None when the screen has it and keeps it (nothing is written when it had
        it already), else the other size it showed after the write."""
        if screen == size and self.mode_request_absent():
            return None
        self.tool(xprop_set_argv(MODE_PROPERTY, mode_control_values(*size)))
        self.wait_for_mode(size)
        return self.hold_size(size)

    def monitor_plan(self, target, keep=None):
        """The sizes to try for target, in order, as (budget, size); the budget is None for target itself.

        target comes first, whatever was learned: the learned budget only puts its candidate first among the smaller
        sizes. With `keep`, a smaller size that is in place for this revision, target is left out (a screen that
        changed by itself is set to what was in place, never climbed back to the full size).
        """
        plan = [] if keep and keep != target else [(None, target)]
        budgets = [self.budget] if self.budget else []
        budget = STEAM_PIXEL_BUDGET
        for _ in range(BUDGET_STEPS + 1):
            budgets.append(budget)
            budget = budget * BUDGET_SHRINK_PERCENT // 100
        for budget in budgets:
            size = capped_size(target, budget)
            if size and size not in [tried for _, tried in plan]:
                plan.append((budget, size))
        return plan or [(None, target)]   # a target with no usable smaller shape is still tried as it is

    def apply(self, contract, games=(), keep=None):
        """Bring Gamescope to the contract: the game monitor first, then the scaler.

        A game that is running keeps the monitor it started with, so while one runs a target that differs from
        the screen is not applied: the scaler still is, and the size the screen keeps is returned. None says
        that the contract was applied; self.fallback then names the smaller size and budget when Steam undid
        the target. Raises ScreenSetBack when every size was undone. `keep` is the size in place when a screen
        that changed by itself is set right again.
        """
        target = (contract.width, contract.height)
        self.fallback = None
        plan = self.monitor_plan(target, keep)
        screen = self.screen_size()
        kept = screen if games and screen != plan[0][1] else None
        monitor = kept or target
        if kept is None:
            seen = screen
            for position, (budget, size) in enumerate(plan):
                undone = self.set_monitor(size, seen)
                if undone is None:
                    monitor = size
                    if budget is not None:
                        self.budget, self.fallback = budget, (budget, size)
                    break
                following = plan[position + 1:]
                log('revision %d: Steam set %dx%d back to %dx%d%s' % (
                    contract.revision, *size, *undone, '; trying %dx%d' % following[0][1] if following else ''))
                seen = undone
            else:
                raise ScreenSetBack(self.screen_size())
        self.tool(xprop_set_argv(SCALER_PROPERTY, [SCALER_VALUES[contract.scale_mode]]))
        picked = scaling_filter(contract.scale_mode, monitor, (contract.region_width, contract.region_height))
        self.tool(xprop_set_argv(FILTER_PROPERTY, [picked]))
        log('scaler %s (%d), filter %s (%d) for a %dx%d monitor in a %dx%d region'
            % (contract.scale_mode, SCALER_VALUES[contract.scale_mode],
               'nearest' if picked == FILTER_NEAREST else 'linear', picked, *monitor,
               contract.region_width, contract.region_height))
        return kept

    def apply_and_report(self, contract, games=(), again=False):
        """Apply the contract, or defer it while a game runs, and report the outcome to the supervisor.

        A deferred revision stays unsettled, so that the poll after the game has ended applies it. `deferred`
        remembers what the supervisor was told, so the same deferral is not reported again at every poll.
        `again` is a settled revision that sets its screen right once more: it counts on, and it is reported only
        when the size that holds is not the one reported before.
        """
        began = time.monotonic()
        target = (contract.width, contract.height)
        summary = '%dx%d %s' % (contract.width, contract.height, contract.scale_mode)
        size = target                # what the report gives as the game monitor
        try:
            self.fallback = None
            kept = self.apply(contract, games, **({'keep': self.in_place[1]} if again else {}))
            if kept is None and self.fallback:
                budget, size = self.fallback
                result = 'applied'
                summary = '%dx%d %s (scaled to %dx%d)' % (*size, contract.scale_mode, *target)
                detail = ('Steam keeps this screen within %s pixels: %dx%d is scaled to fill %dx%d'
                          % (describe_budget(budget), *size, *target))[:MAX_DETAIL]
            elif kept is None:
                result, detail = 'applied', summary
            else:
                result, size = 'deferred', kept
                detail = clean('%s running; monitor stays %dx%d (target %dx%d)'
                               % (describe_games(games), *kept, *target))
        except ScreenSetBack as error:
            result, size, detail = 'failed', error.size, clean(error)
        except ToolError as error:
            result, detail = 'failed', clean(error)
        except Exception as error:   # an unexpected bug is still only a failed apply
            result, detail = 'failed', describe(error)
        if result == 'deferred':
            log('revision %d: deferred %s: %s running, monitor stays %dx%d'
                % (contract.revision, summary, describe_games(games), *size))
            self.deferred = (contract.revision, tuple(games))
        else:
            log('revision %d: %s %s in %.2fs%s' % (contract.revision, result, summary, time.monotonic() - began,
                                                    '' if result == 'applied' else ': ' + detail))
            self.settled, self.deferred = contract.revision, None
            self.refit = True        # the screen may have another size now (a failed apply may have changed it too)
            if not again:
                self.reasserts = 0
            self.in_place = (contract.revision, size) if result == 'applied' else None
            self.checked = time.monotonic()
        if result == 'deferred':
            self.in_place = None
        if again and result == 'applied' and size == self.reported_size:
            return
        self.reported_size = size
        self.pending_report = {'instance': self.instance, 'display': 'report', 'revision': contract.revision,
                               'game_width': size[0], 'game_height': size[1],
                               'result': result, 'detail': detail}
        self.deliver_report()

    def keep_size(self, contract, games):
        """Set the screen right again when it changed by itself; True when the screen was looked at.

        Only a settled revision whose size is in place, and only while no game runs, every CHECK_SECONDS. After
        REASSERT_LIMIT resets the next change is reported as failed once and left alone.
        """
        if self.in_place is None or contract is None or games is None or games or contract.revision != self.in_place[0]:
            return False
        if time.monotonic() - self.checked < CHECK_SECONDS:
            return False
        self.checked = time.monotonic()
        wanted = self.in_place[1]
        try:
            screen = self.screen_size()
        except ToolError as error:
            self.note_error('display', describe(error))
            return True
        if screen == wanted:
            return True
        if self.reasserts >= REASSERT_LIMIT:
            detail = 'Steam keeps setting the screen to %dx%d' % screen
            log('revision %d: %s' % (contract.revision, detail))
            self.in_place, self.reported_size = None, screen
            self.pending_report = {'instance': self.instance, 'display': 'report', 'revision': contract.revision,
                                   'game_width': screen[0], 'game_height': screen[1],
                                   'result': 'failed', 'detail': detail}
            self.deliver_report()
            return True
        self.reasserts += 1
        log('revision %d: the screen changed to %dx%d by itself; setting %dx%d again (%d of %d)'
            % (contract.revision, *screen, *wanted, self.reasserts, REASSERT_LIMIT))
        self.apply_and_report(contract, (), True)
        return True

    def needs_apply(self, contract, games):
        """Whether this revision still has to be applied, or reported as deferred for these games.

        A revision that is settled does not. One that is deferred has been told to the supervisor already, so it
        is looked at again only when the games that run have changed, and when none runs any more.
        """
        if contract.revision == self.settled:
            return False
        return not (games and self.deferred == (contract.revision, tuple(games)))

    def fit_tool(self, argv):
        """Run an X tool for the window fit, the games or the wake, leaving the display check's failure count alone.

        A call that fails adds nothing to that count, so the end of the follower still waits for the checks.
        A call that answers ends the streak, because the failures counted so far were not consecutive.
        """
        output = run_tool(argv, self.environment)
        self.display_failures = 0
        return output

    def write_force(self, value, why):
        """Write GAMESCOPE_FORCE_WINDOWS_FULLSCREEN and log the change; a write that fails raises.

        A 1 counts as written from the moment its request goes out, not when the call has answered: xprop may already
        have set the property when a stop unwinds the call or the call fails, and `forced` has to say then that a 0 is
        owed. A 0 counts once its call has answered.
        """
        was = self.forced or 0
        if value:
            self.forced = value
        self.fit_tool(xprop_set_argv(FORCE_FULLSCREEN_PROPERTY, [value]))
        self.forced = value
        log('window fit: %s %d -> %d (%s)' % (FORCE_FULLSCREEN_PROPERTY, was, value, why))

    def read_focus(self, with_force=False):
        """The focus window and the app Gamescope names for it, from one xprop call of the root window.

        Returns (window, present, app) as parse_focused_window and parse_focus_app give them, and with with_force
        the forced fullscreen's value as a fourth item.
        """
        names = [FOCUS_PROPERTY, FOCUS_APP_PROPERTY] + ([FORCE_FULLSCREEN_PROPERTY] if with_force else [])
        text = self.fit_tool(xprop_get_argv(*names))
        focus = (parse_focused_window(text), *parse_focus_app(text))
        return (*focus, parse_force(text)) if with_force else focus

    def fit_windows(self):
        """Fit the focused window to the screen with a one-shot GAMESCOPE_FORCE_WINDOWS_FULLSCREEN.

        Every call reads the focus. The first call also reads the property, and switches it off when it is on (left
        by a follower that ended during a one-shot). A property that may still be on when a call starts (a switch-off
        that failed at the end of the last call) is switched off before anything else. A window that has just taken
        the focus is decided once its app id is known: Steam's own window and a window that states one size are left
        alone, and so is a window that already has the screen size. For any other window the whole one-shot runs
        inside this call (fit_once): the property goes on, the window is looked at for at most FIT_SECONDS, and the
        property goes off again before the call returns. A decided window is looked at again when an applied display
        revision has changed the screen size. A tool that fails during a one-shot ends it, with the property switched
        off first; the window stays undecided and the step that failed is tried again at the next call.
        """
        try:
            window, present, app, *force = self.read_focus(with_force=self.forced is None)
            if self.forced is None:
                self.forced = force[0]
                if self.forced:
                    try:
                        self.write_force(0, 'on when the follower started')
                    except ToolError:
                        self.forced = None   # read again and switched off at the next call
                        raise
            elif self.forced:
                # Only a switch-off that failed at the end of the last call (or a write of 1 that did not finish) gets
                # here: a one-shot ends with its 0. Nothing is decided for any window while the property may be on,
                # and a write that fails raises before that, so it is tried again at the next call.
                self.write_force(0, 'it may have been left on by the last call')
            if window != self.fit_window:
                self.fit_window, self.fit_decided, self.fit_screen = window, False, None
                self.fit_waiting, self.refit = False, False
            if window is None:
                return
            if not self.fit_decided:
                self.decide_fit(window, present, app)
            elif self.refit and self.fit_screen is not None:
                screen = self.fit_screen_size()
                self.refit = False
                if screen != self.fit_screen:
                    log('window fit: the screen is %dx%d now, was %dx%d: window 0x%x is fitted again'
                        % (*screen, *self.fit_screen, window))
                    self.fit_decided = False
                    self.decide_fit(window, present, app)
        except ToolError as error:
            self.note_error('fit', describe(error))
        else:
            self.clear_error('fit')

    def fit_screen_size(self):
        screen = parse_screen_size(self.fit_tool(['xrandr', '--current']))
        if screen is None:
            raise ToolError('xrandr output has no screen size')
        return screen

    def fit_window_size(self, window):
        size = parse_window_geometry(self.fit_tool(xdotool_geometry_argv(window)))
        if size is None:
            raise ToolError('xdotool output has no window size')
        return size

    def decide_fit(self, window, present, named):
        """Decide a focused window once its app id is known: leave it alone, or fit it with the one-shot (fit_once).

        `present` and `named` are GAMESCOPE_FOCUSED_APP_GFX as read with the focus; only while that property is
        absent is the window's own STEAM_GAME its app id.
        """
        text = self.fit_tool(xprop_window_argv(window, HINTS_PROPERTY, APP_PROPERTY))
        app, hints = (named if present else parse_window_app(text)), parse_size_hints(text)
        if app is None:
            if not self.fit_waiting:
                self.fit_waiting = True
                log('window fit: window 0x%x: waiting for its app id (%s and its %s name none)'
                    % (window, FOCUS_APP_PROPERTY, APP_PROPERTY))
            return
        if app == STEAM_APP_ID:
            # Gamescope's focus pass resizes Steam's own window itself whenever it has the focus (window_is_fullscreen
            # is true for it), so the property would add nothing for it and only reach the window focused next.
            log("window fit: left Steam's own window 0x%x alone (Gamescope sizes it itself)" % window)
            self.fit_decided, self.fit_screen = True, None
            return
        if is_fixed(hints):
            log('window fit: left %s alone' % describe_window(window, app, hints))
            self.fit_decided, self.fit_screen = True, None
            return
        size, screen = self.fit_window_size(window), self.fit_screen_size()
        detail = describe_window(window, app, hints, size, screen)
        if size == screen:
            log('window fit: left %s alone' % detail)
            self.fit_decided, self.fit_screen, self.refit = True, screen, False
            return
        # The focus is read once more right before the write, so that a window that took the focus while this one
        # was looked at is not resized for it; it is decided at the next call instead.
        if self.read_focus() != (window, present, named):
            log('window fit: the focus moved while window 0x%x was looked at; nothing written' % window)
            return
        self.fit_once(window, present, named, screen, detail)

    def fit_once(self, window, present, named, screen, detail):
        """The one-shot for a window that needs the fit, from the 1 to the 0, inside this call.

        The property is marked as possibly on, written 1, and the window is looked at (watch_fit) about every
        FIT_STEP_SECONDS for at most FIT_SECONDS from the start of the write. The 0 follows in a `finally`, whatever
        ended the looking: the window took the screen size, a look failed, the follower is being stopped. So the
        property is never left on for a later call, and it never outlives this one by more than the time the 0 takes.
        A 0 that cannot be written is raised after it was logged, and leaves `forced` set: the next call, or release(),
        writes it before anything else.
        """
        began = time.monotonic()
        why = 'the fit did not finish'
        try:
            self.write_force(1, detail)
            why = self.watch_fit(window, present, named, screen, began)
        except ToolError as error:
            why = 'a tool failed: ' + clean(error)
            raise
        except SystemExit:
            why = 'the follower is stopping'
            raise
        finally:
            failure = self.switch_off(why)
        if failure is not None:
            raise failure

    def watch_fit(self, window, present, named, screen, began):
        """Look at the window while the property is on, until it can go off; returns why.

        A look reads the focus, the window's hints and the window's size, in that order. The property can go off when
        the focus has moved (a window that took it meanwhile may have been resized in that focus pass already), when
        the window states one size now (hints published late: Gamescope sizes it back to them once the property is
        off), when it has the screen size, or when FIT_SECONDS have passed since `began` and it still has not. The last
        two decide the window for this screen, the third for none, and a moved focus decides nothing: the next call
        decides whichever window has the focus. The screen is not read again: only this follower changes it, between
        two calls. A tool that fails raises, and the caller switches the property off.
        """
        expected = (window, present, named)
        while True:
            focus = self.read_focus()
            if focus != expected:
                if focus[0] is None:
                    return 'no window is focused'
                if focus[0] != window:
                    return 'the focus moved to window 0x%x' % focus[0]
                return 'window 0x%x is named for another app now' % window
            hints = parse_size_hints(self.fit_tool(xprop_window_argv(window, HINTS_PROPERTY)))
            if is_fixed(hints):
                self.fit_decided, self.fit_screen, self.refit = True, None, False
                return 'window 0x%x states one size now (%s)' % (window, describe_hints(hints))
            size = self.fit_window_size(window)
            elapsed = time.monotonic() - began
            if size == screen:
                self.fit_decided, self.fit_screen, self.refit = True, screen, False
                return 'window 0x%x has the screen size %dx%d after %d ms' % (window, *screen, round(elapsed * 1000))
            if elapsed >= FIT_SECONDS:
                self.fit_decided, self.fit_screen, self.refit = True, screen, False
                return 'window 0x%x is %dx%d, not %dx%d, after %d ms: left at that size' % (
                    window, *size, *screen, round(elapsed * 1000))
            time.sleep(min(FIT_STEP_SECONDS, FIT_SECONDS - elapsed))

    def switch_off(self, why):
        """Write the 0 that ends a one-shot. A write that fails is logged and returned, not raised: raised, it would
        replace what is unwinding the call (a stop above all). `forced` stays set then."""
        try:
            self.write_force(0, why)
        except ToolError as error:
            log('window fit: %s could not be switched off: %s' % (FORCE_FULLSCREEN_PROPERTY, describe(error)))
            return error
        return None

    def release(self):
        """On the way out: a forced fullscreen that is on, or may be (a stop can come during the write of a 1), is
        switched off, so that it is not left on the root."""
        if not self.forced:
            return
        try:
            self.write_force(0, 'the follower is stopping')
        except ToolError as error:
            log('window fit: %s could not be switched off: %s' % (FORCE_FULLSCREEN_PROPERTY, describe(error)))

    def watch_games(self):
        """The sorted app ids of the games that run now, read from the nested root; None when it cannot be read.

        Every poll asks, before it applies anything: a change of the game monitor waits while a game runs, and
        a game that has appeared may call for waking Steam. A tool that fails tells nothing about the games, so
        the caller leaves everything as it is until the next poll; a property that is missing or malformed
        reads as no game.
        """
        try:
            games = running_games(self.fit_tool(xprop_get_argv(FOCUSABLE_APPS_PROPERTY)))
        except ToolError as error:
            self.note_error('games', describe(error))
            return None
        self.clear_error('games')
        self.wake_for_new_games(games)
        return games

    def wake_for_new_games(self, games):
        """Start to watch each game that has appeared since the last read, and wake Steam when one is due.

        What runs at the first read was running before the follower looked and is never watched. A game that
        leaves the list before Steam was woken for it is forgotten, and is new when it returns.
        """
        now = time.monotonic()
        before, self.games = self.games, games
        if before is None:
            return
        for app in games:
            if app not in before:
                self.watching[app] = now
        for app in [app for app in self.watching if app not in games]:
            del self.watching[app]
        if self.watching:
            self.wake_steam(now)

    def wake_steam(self, now):
        """Send WAKE_KEY once when Steam has stayed in front of a game for WAKE_AFTER_SECONDS since it appeared.

        A side whose Steam is idle shows its screensaver, and a game that starts into it stays behind Steam's
        window: Gamescope presents Steam (the focused app stays STEAM_APP_ID). One key press wakes the side.
        When Gamescope presents anything else the game has the focus, so the side is awake and none of the
        games watched is woken for. A tool that fails is logged once, and its key press is not repeated.
        """
        try:
            focused = parse_focused_app(self.fit_tool(xprop_get_argv(FOCUSED_APP_PROPERTY)))
        except ToolError as error:
            self.note_error('focus', describe(error))
            return
        self.clear_error('focus')
        if focused != STEAM_APP_ID:
            self.watching.clear()
            return
        due = sorted(app for app, since in self.watching.items() if now - since >= WAKE_AFTER_SECONDS)
        for app in due:
            del self.watching[app]
        if not due:
            return
        try:
            self.fit_tool(xdotool_key_argv(WAKE_KEY))
        except ToolError as error:
            self.note_error('wake', describe(error))
            return
        self.clear_error('wake')
        for app in due:
            log('woke Steam for app %d' % app)

    def refresh_focus(self):
        """Give Steam's window one X focus cycle once it has been the focus for FOCUS_SETTLE_SECONDS.

        Gamescope 3.16.23 sets the input focus before Chromium is ready, and Steam's UI then never counts itself
        active. The cycle (root, then the window) makes it so. It is made for the window Gamescope names as the focus
        when the app presented is STEAM_APP_ID, once for each window that takes the focus, never within
        FOCUS_REFRESH_SECONDS of the last one, and never for a game. A window that is no longer the focus when the
        settle is over gets none. A tool that fails (a vanished window included) is logged once and not repeated.
        """
        try:
            text = self.fit_tool(xprop_get_argv(FOCUS_PROPERTY, FOCUSED_APP_PROPERTY))
            window, app = parse_focused_window(text), parse_focused_app(text)
            now = time.monotonic()
            if window is None:
                return                       # nothing is focused: not a change, so a window that returns is not new
            if window != self.refresh_window:
                self.refresh_window, self.refresh_since, self.refresh_done = window, now, False
            if self.refresh_done or app != STEAM_APP_ID or now - self.refresh_since < FOCUS_SETTLE_SECONDS:
                return
            if self.refresh_last is not None and now - self.refresh_last < FOCUS_REFRESH_SECONDS:
                return
            self.refresh_done, self.refresh_last = True, now
            if self.root_window is None:
                self.root_window = parse_root_window(self.fit_tool(xwininfo_root_argv()))
                if self.root_window is None:
                    raise ToolError('xwininfo output has no root window')
            self.fit_tool(xdotool_windowfocus_argv(self.root_window))
            self.fit_tool(xdotool_windowfocus_argv(window))
            log('focus refresh: window 0x%x' % window)
        except ToolError as error:
            self.note_error('focus refresh', describe(error))
        else:
            self.clear_error('focus refresh')

    def keep_awake(self, now):
        """Tell Steam that somebody is there, every KEEP_AWAKE_SECONDS, unless Main is alone on the screen.

        The first one goes out with the first cycle, and one that failed is tried again KEEP_AWAKE_RETRY_SECONDS
        later. See "Holding Steam's idle sleep" at the top of this file.
        """
        if self.layout == ALONE_LAYOUT:
            self.awake_next = None           # the next layout with two players starts with one at once
            return
        if self.awake_next is not None and now < self.awake_next:
            return
        try:
            value = next_input_counter(self.fit_tool(xprop_get_argv(INPUT_COUNTER_PROPERTY)))
            self.fit_tool(xprop_set_argv(INPUT_COUNTER_PROPERTY, [value]))
        except ToolError as error:
            self.awake_next = now + KEEP_AWAKE_RETRY_SECONDS
            self.note_error('keep awake', describe(error))
        else:
            self.awake_next = now + KEEP_AWAKE_SECONDS
            self.clear_error('keep awake')

    def probe_display(self):
        try:
            self.mode_request_absent()
        except DisplayUnreachable:
            if self.display_failures >= DISPLAY_LOST_AFTER:
                log('display %s is gone; ending' % self.display)
                self.display_lost = True
        except ToolError as error:
            self.note_error('display', describe(error))
        else:
            self.clear_error('display')

    # -- the loop ----------------------------------------------------------------------------------
    def cycle(self):
        self.cycles += 1
        self.control_failed = False
        self.deliver_report()
        contract = self.poll_contract()
        games = self.watch_games()
        if contract is not None and games is not None and self.needs_apply(contract, games):
            self.apply_and_report(contract, games)
        elif self.keep_size(contract, games):
            pass
        elif self.cycles % DISPLAY_PROBE_EVERY == 0:
            self.probe_display()
        if not self.display_lost:
            self.fit_windows()
            self.refresh_focus()
            self.keep_awake(time.monotonic())

    def retry_soon(self):
        """Whether the cycle just run is followed by a quick retry instead of by the normal poll.

        A control exchange that failed may hide a new revision, so it is asked again soon. That happens a few
        times in a row at most: a supervisor that stays silent is polled at the normal interval after that,
        until one exchange succeeds and gives the quick retries back.
        """
        if not self.control_failed:
            self.retries_left = CONTROL_RETRIES
            return False
        if self.retries_left <= 0:
            return False
        self.retries_left -= 1
        return True

    def run(self):
        log('following %s every %gs' % (self.display, self.poll))
        next_poll = time.monotonic()
        while not self.display_lost:
            began = time.monotonic()
            try:
                self.cycle()
            except Exception as error:   # SystemExit from a signal is not an Exception and still ends the loop
                self.note_error('internal', describe(error))
            next_poll += self.poll
            if self.retry_soon():
                # Never later than the normal poll: with a poll interval below the retry interval this changes nothing.
                next_poll = min(next_poll, began + self.retry_interval)
            delay = next_poll - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_poll = time.monotonic()
        return 0


def follower_from_environment(poll, apply_timeout, control_timeout=CONTROL_TIMEOUT_SECONDS,
                              retry_interval=CONTROL_RETRY_SECONDS):
    try:
        server_uid = int(os.environ.get('DOSPLEX_SESSION_CONTROL_SERVER_UID', '0'))
    except ValueError:
        raise ValueError('DOSPLEX_SESSION_CONTROL_SERVER_UID is not a number') from None
    return Follower(os.environ.get('DOSPLEX_SESSION_CONTROL', ''), os.environ.get('DOSPLEX_SESSION_INSTANCE', ''),
                    server_uid, poll, apply_timeout, control_timeout=control_timeout, retry_interval=retry_interval)


def stop_on_signal(_signum, _frame):
    raise SystemExit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apply live display changes to the side\'s nested Gamescope.')
    parser.add_argument('--poll-interval', type=float, default=POLL_SECONDS, metavar='SECONDS',
                        help='seconds between polls of the supervisor (default %(default)s)')
    parser.add_argument('--apply-timeout', type=float, default=APPLY_TIMEOUT_SECONDS, metavar='SECONDS',
                        help='seconds to wait for Gamescope to apply a game monitor size (default %(default)s)')
    parser.add_argument('--control-timeout', type=float, default=CONTROL_TIMEOUT_SECONDS, metavar='SECONDS',
                        help='seconds to wait for one reply from the supervisor (default %(default)s)')
    parser.add_argument('--retry-interval', type=float, default=CONTROL_RETRY_SECONDS, metavar='SECONDS',
                        help='longest wait, from the start of a poll, before a failed exchange with the supervisor '
                             'is tried again, up to %d times in a row (default %%(default)s)' % CONTROL_RETRIES)
    args = parser.parse_args(argv)
    if not 0.01 <= args.poll_interval <= 60:
        parser.error('--poll-interval must be between 0.01 and 60 seconds')
    if not 0.1 <= args.apply_timeout <= 120:
        parser.error('--apply-timeout must be between 0.1 and 120 seconds')
    if not 0.1 <= args.control_timeout <= 60:
        parser.error('--control-timeout must be between 0.1 and 60 seconds')
    if not 0.01 <= args.retry_interval <= 60:
        parser.error('--retry-interval must be between 0.01 and 60 seconds')
    try:
        follower = follower_from_environment(args.poll_interval, args.apply_timeout, args.control_timeout,
                                             args.retry_interval)
    except ValueError as error:
        log(str(error))
        return 2
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop_on_signal)
    try:
        return follower.run()
    except SystemExit as stop:
        log('stopping')
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)   # the switch-off below is one xprop call of at most 5 s
        follower.release()
        return stop.code or 0


if __name__ == '__main__':
    raise SystemExit(main())
