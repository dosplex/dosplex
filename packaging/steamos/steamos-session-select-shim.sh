#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Route an explicitly identified DosPlex session to its own supervisor.
# Only invocations outside DosPlex may use the original host selector.
set -euo pipefail

orig="/usr/bin/steamos-session-select.orig"
helper="/usr/local/bin/dosplex-switch-mode.sh"

run_original() {
    if [[ -x "$orig" ]]; then
        exec "$orig" "$@"
    fi
    echo "Original SteamOS session selector not found: $orig" >&2
    exit 1
}

if [[ -z "${DOSPLEX_SIDE+x}" && "${DOSPLEX_SESSION_RUNTIME:-0}" != "1" ]]; then
    run_original "$@"
fi

if [[ $# != 1 ]]; then
    echo "DosPlex session selection requires exactly one activity" >&2
    exit 1
fi
side="${DOSPLEX_SIDE:-}"
case "$side" in
    left|right) ;;
    *) echo "DosPlex side identity is missing or invalid" >&2; exit 1 ;;
esac

case "$1" in
    desktop|plasma|plasma-wayland|plasma-wayland-persistent|plasma-x11-persistent)
        mode=desktop ;;
    gamescope|gaming|steam|steam-gamepadui)
        mode=gaming ;;
    *) echo "Unsupported DosPlex activity" >&2; exit 1 ;;
esac

if [[ -n "${DOSPLEX_SESSION_CONTROL:-}" ]]; then
    if [[ -z "${DOSPLEX_SESSION_CONTROL_CLIENT:-}" || ! -f "$DOSPLEX_SESSION_CONTROL_CLIENT" ]]; then
        echo "DosPlex session control client is unavailable" >&2
        exit 1
    fi
    exec python3 "$DOSPLEX_SESSION_CONTROL_CLIENT" client-mode "$mode"
fi

socket="${DOSPLEX_HOST_SOCKET:-/run/dosplex/dosplex-0}"
if [[ ! -S "$socket" || ! -x "$helper" ]]; then
    echo "DosPlex session control is unavailable; host session unchanged" >&2
    exit 1
fi

# sudo does not preserve caller environment. An isolated trial must install a
# trusted request route before enabling this legacy helper for its state tree.
if [[ "${DOSPLEX_SIDE_ROOT:-/var/lib/dosplex/sides}" != /var/lib/dosplex/sides ]]; then
    echo "DosPlex trial session control is not configured; host session unchanged" >&2
    exit 1
fi
exec sudo -n "$helper" "$side" "$mode"
