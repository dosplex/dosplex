#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Read a validated, committed side configuration for the session supervisor.

state.json carries a top-level "contract_version". Without it (or with any value but the
integer 2) the file is a version-1 contract: the supervisor restarts a side when its
committed display fields change. Version 2 (ADR-012) never restarts a side for them; the
side applies live changes itself (T-413, T-415).
"""
import json
from pathlib import Path
import sys


def _committed(path, side):
    """Validate one side of a state file: (state, entry, (width, height, game_width, game_height,
    scale_mode, display_revision, layout))."""
    if side not in ("left", "right"):
        raise ValueError("invalid side")
    state = json.loads(Path(path).read_text())
    layout = state["layout"]
    if layout not in ("single", "vertical", "horizontal", "guest"):
        raise ValueError("invalid layout")
    entry = state[side]
    values = [entry["width"], entry["height"],
              entry.get("game_width", entry["width"]), entry.get("game_height", entry["height"])]
    if any(type(value) is not int or not 1 <= value <= 16384 for value in values):
        raise ValueError("invalid display dimensions")
    scaler = entry.get("scale_mode", "fit")
    if scaler not in ("fit", "stretch", "zoom"):
        raise ValueError("invalid presentation mode")
    revision = entry.get("display_revision", 0)
    if type(revision) is not int or not 0 <= revision <= 4294967295:
        raise ValueError("invalid display revision")
    # Pending settings are deliberately excluded: only a committed (Apply) change counts.
    return state, entry, (*values, scaler, revision, layout)


def read_configuration(path, side):
    """The committed fields as (width, height, game_width, game_height, scale_mode,
    display_revision, layout). Kept for callers that predate the versioned contract."""
    return _committed(path, side)[2]


def read_contract(path, side):
    """The versioned contract: {'version': 1 or 2, 'live': {x, y, width, height, game_width,
    game_height, scale_mode, display_revision, layout}}. Validated like read_configuration;
    a missing position reads as 0, 0 (state files written before positions were published)."""
    state, entry, (width, height, game_width, game_height, scaler, revision, layout) = _committed(path, side)
    position = [entry.get("x", 0), entry.get("y", 0)]
    if any(type(value) is not int or not -16384 <= value <= 16384 for value in position):
        raise ValueError("invalid display position")
    declared = state.get("contract_version")
    return {'version': 2 if type(declared) is int and declared == 2 else 1,
            'live': {'x': position[0], 'y': position[1], 'width': width, 'height': height,
                     'game_width': game_width, 'game_height': game_height, 'scale_mode': scaler,
                     'display_revision': revision, 'layout': layout}}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("usage: dosplex-display-state.py STATE_FILE left|right")
        print(*read_configuration(sys.argv[1], sys.argv[2]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Cannot read Dosplex display configuration: {error}", file=sys.stderr)
        sys.exit(1)
