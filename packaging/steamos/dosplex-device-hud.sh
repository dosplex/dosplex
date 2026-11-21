#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Wrapper to launch the device HUD overlay
# This script is called by the compositor when Super+Shift+M is pressed.

# Ensure we use the compositor's root socket and deck's runtime directory
export XDG_RUNTIME_DIR="/run/dosplex"
export WAYLAND_DISPLAY="dosplex-0"
export DOSPLEX_HUD_IPC_SOCK="${DOSPLEX_HUD_IPC_SOCK:-/run/dosplex/hud-ipc.sock}"
export GDK_BACKEND="wayland"
export QT_QPA_PLATFORM="wayland"

# Launch the HUD Python script
exec /usr/local/libexec/dosplex-device-hud.py
