#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${DOSPLEX_SIDE:-}"
owner="${DOSPLEX_OWNER:-}"
side_socket="${DOSPLEX_SIDE_SOCKET:-}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"

if [[ -z "$side" || -z "$owner" || -z "$side_socket" ]]; then
    echo "missing nested Wayland side session environment" >&2
    exit 1
fi

if [[ ! -x "$session_script" ]]; then
    echo "missing session script: $session_script" >&2
    exit 1
fi

export WAYLAND_DISPLAY="$side_socket"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"

exec "$session_script" "$side" "$owner"
