#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
#
# Dosplex Session Start Script
# Run this instead of the compositor directly for proper session setup.
#
# On Steam Deck (with display):
#   ./start-dosplex.sh
#
# This handles:
#   - Setting XDG_RUNTIME_DIR
#   - Choosing the right backend (drm on bare metal, x11 if nested)
#   - Choosing the compositor's policy flags: the backend only builds the
#     display, so this script says explicitly what each run may do
#   - Auto-restart on failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${DOSPLEX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSITOR="${DOSPLEX_COMPOSITOR:-$INSTALL_ROOT/bin/dosplex-compositor}"
AUTH_HELPER="${DOSPLEX_AUTH_HELPER:-$INSTALL_ROOT/libexec/dosplex-side-auth.py}"
SESSION_SCRIPT="${DOSPLEX_SESSION_SCRIPT:-$INSTALL_ROOT/libexec/dosplex-user-side-session.sh}"
SIDE_SUPERVISOR="${DOSPLEX_SIDE_SUPERVISOR:-$INSTALL_ROOT/bin/dosplex-side-supervisor.sh}"
AUDIO_ROUTER="${DOSPLEX_AUDIO_ROUTER:-$INSTALL_ROOT/bin/dosplex-audio-router.py}"
DOSPLEX_AUTO_LOGIN_PRIMARY="${DOSPLEX_AUTO_LOGIN_PRIMARY:-deck}"
DOSPLEX_AUTO_LOGIN_GUEST="${DOSPLEX_AUTO_LOGIN_GUEST:-guest}"

SESSION_PAYLOAD="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
SOCKET_NAME="dosplex-0"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
HOME_DIR="${HOME:-/root}"
LOG_FILE="${DOSPLEX_LOG_FILE:-$HOME_DIR/dosplex-compositor.log}"

# Detect backend
detect_backend() {
    if [[ -n "${DOSPLEX_BACKEND:-}" ]]; then
        echo "$DOSPLEX_BACKEND"
    elif [[ -n "${DISPLAY:-}" ]]; then
        echo "x11"
    elif [[ -e /dev/dri/card0 ]]; then
        echo "drm"
    else
        echo "headless"
    fi
}

# The compositor's policy comes from flags, never from the backend (-b only
# builds the display). COMPOSITOR_ARGS is what every start below passes.
set_compositor_args() {
    local backend="$1"
    COMPOSITOR_ARGS=(-b "$backend")
    if [[ "$backend" == "drm" ]]; then
        # The display-manager run owns the machine: real side sessions (the
        # unit runs as root, which -s requires), machine power, idle blanking
        # and the installed audio helper. This unit is the display manager, so
        # a restart or power-off comes back as Dosplex: the confirmations say so.
        COMPOSITOR_ARGS+=(-s --allow-power --boots-into=dosplex --idle-policy=dim --audio-router "$AUDIO_ROUTER")
    else
        # A nested or headless development run keeps placeholder sides and
        # leaves power, idle and audio off.
        COMPOSITOR_ARGS+=(--test-hooks --host-keyboards)
    fi
}

run_dynamic_side_stack() {
    local backend="$1"
    local compositor_pid left_pid right_pid

    echo "[dosplex-start] Starting dynamic compositor with backend=$backend" >> "$LOG_FILE"
    echo "[dosplex-start] $(date)" >> "$LOG_FILE"

    export XDG_RUNTIME_DIR
    export WAYLAND_DISPLAY="$SOCKET_NAME"
    export DOSPLEX_AUTH_HELPER="$AUTH_HELPER"
    export DOSPLEX_RUNTIME_DIR="$RUNTIME_DIR"
    export DOSPLEX_WAYLAND_DISPLAY="$SOCKET_NAME"
    # The compositor uses DOSPLEX_SESSION_SCRIPT as the first script to run after login.
    # We want it to run the supervisor.
    export DOSPLEX_SESSION_SCRIPT="$SIDE_SUPERVISOR"
    # We pass the REAL session script in a different variable so the supervisor can use it.
    export DOSPLEX_REAL_SESSION_SCRIPT="$SESSION_SCRIPT"
    export DOSPLEX_SESSION_PAYLOAD="$SESSION_PAYLOAD"
    export DOSPLEX_SIDE_SCALING="${DOSPLEX_SIDE_SCALING:-1}"
    export DOSPLEX_AUTO_LOGIN_PRIMARY="$DOSPLEX_AUTO_LOGIN_PRIMARY"
    export DOSPLEX_AUTO_LOGIN_GUEST="$DOSPLEX_AUTO_LOGIN_GUEST"

    mkdir -p "$RUNTIME_DIR"
    set_compositor_args "$backend"
    "$COMPOSITOR" "${COMPOSITOR_ARGS[@]}" >> "$LOG_FILE" 2>&1 &
    compositor_pid=$!

    # Start side supervisors
    DOSPLEX_SIDE_ROLE=primary "$SIDE_SUPERVISOR" left "${DOSPLEX_LEFT_USER:-deck}" >> "$LOG_FILE" 2>&1 &
    left_pid=$!

    DOSPLEX_SIDE_ROLE=guest "$SIDE_SUPERVISOR" right "${DOSPLEX_RIGHT_USER:-guest}" >> "$LOG_FILE" 2>&1 &
    right_pid=$!

    wait "$compositor_pid"
    local status=$?

    kill "$left_pid" "$right_pid" "$compositor_pid" >/dev/null 2>&1 || true
    wait "$left_pid" "$right_pid" "$compositor_pid" >/dev/null 2>&1 || true
    return "$status"
}

start_compositor() {
    local backend="$1"

    echo "[dosplex-start] Starting compositor with backend=$backend" >> "$LOG_FILE"
    echo "[dosplex-start] $(date)" >> "$LOG_FILE"

    export XDG_RUNTIME_DIR
    export WAYLAND_DISPLAY="$SOCKET_NAME"
    export DOSPLEX_AUTH_HELPER="$AUTH_HELPER"
    export DOSPLEX_RUNTIME_DIR="$RUNTIME_DIR"
    export DOSPLEX_WAYLAND_DISPLAY="$SOCKET_NAME"
    # The compositor uses DOSPLEX_SESSION_SCRIPT as the first script to run after login.
    # We want it to run the supervisor.
    export DOSPLEX_SESSION_SCRIPT="$SIDE_SUPERVISOR"
    # We pass the REAL session script in a different variable so the supervisor can use it.
    export DOSPLEX_REAL_SESSION_SCRIPT="$SESSION_SCRIPT"
    export DOSPLEX_SESSION_PAYLOAD="$SESSION_PAYLOAD"
    export DOSPLEX_SIDE_SCALING="${DOSPLEX_SIDE_SCALING:-1}"
    export DOSPLEX_AUTO_LOGIN_PRIMARY="$DOSPLEX_AUTO_LOGIN_PRIMARY"
    export DOSPLEX_AUTO_LOGIN_GUEST="$DOSPLEX_AUTO_LOGIN_GUEST"

    mkdir -p "$RUNTIME_DIR"

    set_compositor_args "$backend"
    exec "$COMPOSITOR" "${COMPOSITOR_ARGS[@]}" >> "$LOG_FILE" 2>&1
}

if [[ ! -x "$COMPOSITOR" ]]; then
    echo "[dosplex-start] ERROR: Compositor not found at $COMPOSITOR" >&2
    exit 1
fi
if [[ ! -x "$SIDE_SUPERVISOR" ]]; then
    echo "[dosplex-start] WARNING: Side supervisor not found at $SIDE_SUPERVISOR" >&2
fi

BACKEND=$(detect_backend)
echo "[dosplex-start] Detected backend: $BACKEND"

if [[ "$BACKEND" == "drm" ]] && command -v systemctl >/dev/null 2>&1; then
    for dm_unit in sddm.service; do
        if systemctl is-active --quiet "$dm_unit"; then
            echo "[dosplex-start] Stopping conflicting display manager: $dm_unit"
            systemctl stop "$dm_unit" || true
        fi
    done
fi

# Consolidate to always use dynamic stack for DRM
if [[ "$BACKEND" == "drm" ]]; then
    echo "[dosplex-start] === Dosplex DRM Startup ===" >> "$LOG_FILE"
    echo "[dosplex-start] Timestamp: $(date)" >> "$LOG_FILE"
    echo "[dosplex-start] Compositor: $COMPOSITOR" >> "$LOG_FILE"
    echo "[dosplex-start] Auth helper: $AUTH_HELPER" >> "$LOG_FILE"
    echo "[dosplex-start] Session script: $SESSION_SCRIPT" >> "$LOG_FILE"
    echo "[dosplex-start] Side supervisor: $SIDE_SUPERVISOR" >> "$LOG_FILE"
    echo "[dosplex-start] Backend: $BACKEND" >> "$LOG_FILE"
    echo "[dosplex-start] Layout: ${DOSPLEX_INITIAL_LAYOUT:-single}" >> "$LOG_FILE"
    echo "[dosplex-start] Auto-login primary: $DOSPLEX_AUTO_LOGIN_PRIMARY" >> "$LOG_FILE"

    export XDG_RUNTIME_DIR
    export WAYLAND_DISPLAY="$SOCKET_NAME"
    export DOSPLEX_AUTH_HELPER="$AUTH_HELPER"
    export DOSPLEX_RUNTIME_DIR="$RUNTIME_DIR"
    export DOSPLEX_WAYLAND_DISPLAY="$SOCKET_NAME"
    # Set the session script to be the supervisor, so when the user logs in, the supervisor is launched
    export DOSPLEX_SESSION_SCRIPT="$SIDE_SUPERVISOR"
    export DOSPLEX_REAL_SESSION_SCRIPT="$SESSION_SCRIPT"
    export DOSPLEX_SESSION_PAYLOAD="$SESSION_PAYLOAD"
    export DOSPLEX_SIDE_SCALING="${DOSPLEX_SIDE_SCALING:-1}"
    export DOSPLEX_AUTO_LOGIN_PRIMARY="$DOSPLEX_AUTO_LOGIN_PRIMARY"
    export DOSPLEX_AUTO_LOGIN_GUEST="$DOSPLEX_AUTO_LOGIN_GUEST"
    export DOSPLEX_INITIAL_LAYOUT="${DOSPLEX_INITIAL_LAYOUT:-single}"

    mkdir -p "$RUNTIME_DIR"
    set_compositor_args "$BACKEND"
    exec "$COMPOSITOR" "${COMPOSITOR_ARGS[@]}" >> "$LOG_FILE" 2>&1
else
    start_compositor "$BACKEND"
fi
