#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

# Allow passing variables via command line for robustness
side="${1:-${DOSPLEX_SIDE:-}}"
owner="${2:-${DOSPLEX_OWNER:-}}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"
user_runtime_dir="${DOSPLEX_USER_RUNTIME_DIR:-}"

if [[ -z "$side" || -z "$owner" || -z "$user_runtime_dir" ]]; then
    # Fallback to reading state from disk if available
    state_file="/var/lib/dosplex/sides/${side:-right}/state.env"
    if [[ -f "$state_file" ]]; then
        source "$state_file"
        side="${SIDE:-$side}"
        owner="${USER:-$owner}"
        user_runtime_dir="${DOSPLEX_USER_RUNTIME_DIR:-$user_runtime_dir}"
    fi
fi

if [[ -z "$side" || -z "$owner" || -z "$user_runtime_dir" ]]; then
    echo "missing gamescope side session environment (side=$side owner=$owner runtime=$user_runtime_dir)" >&2
    exit 1
fi

if [[ ! -x "$session_script" ]]; then
    echo "missing session script: $session_script" >&2
    exit 1
fi

export XDG_RUNTIME_DIR="$user_runtime_dir"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"

exec "$session_script" "$side" "$owner"
