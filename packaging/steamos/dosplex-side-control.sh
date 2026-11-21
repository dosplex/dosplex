#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

command="${1:-status}"
target="${2:-all}"
side_root="${DOSPLEX_SIDE_ROOT:-/var/lib/dosplex/sides}"

usage() {
    echo "usage: $0 <status|restart|stop> <left|right|primary|guest|all>" >&2
}

resolve_side() {
    case "$1" in
        left|right)
            printf '%s\n' "$1"
            ;;
        primary)
            printf '%s\n' "${DOSPLEX_PRIMARY_SIDE:-left}"
            ;;
        guest)
            if [[ "${DOSPLEX_PRIMARY_SIDE:-left}" == "left" ]]; then
                printf 'right\n'
            else
                printf 'left\n'
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

status_side() {
    local side="$1"
    local state_file="${side_root}/${side}/session.json"
    if [[ ! -f "$state_file" ]]; then
        echo "${side}: missing"
        return 0
    fi
    python3 - "$state_file" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    state = json.load(f)
print("{side}: lifecycle={lifecycle} pid={pid} active={active} restarts={restarts} exit={exit_status}".format(
    side=state.get("side", "?"),
    lifecycle=state.get("lifecycle", "?"),
    pid=state.get("pid", 0),
    active=state.get("active", False),
    restarts=state.get("restart_count", 0),
    exit_status=state.get("exit_status", 0),
))
PY
}

restart_side() {
    local side="$1"
    python3 "$(dirname "${BASH_SOURCE[0]}")/dosplex-side-supervisor.py" request-restart "$side"
}

case "$command" in
    status)
        if [[ "$target" == "all" ]]; then
            status_side left
            status_side right
        else
            status_side "$(resolve_side "$target")"
        fi
        ;;
    stop)
        if [[ "$target" == "all" ]]; then
            python3 "$(dirname "${BASH_SOURCE[0]}")/dosplex-side-supervisor.py" request-stop left
            python3 "$(dirname "${BASH_SOURCE[0]}")/dosplex-side-supervisor.py" request-stop right
        else
            python3 "$(dirname "${BASH_SOURCE[0]}")/dosplex-side-supervisor.py" request-stop "$(resolve_side "$target")"
        fi
        ;;
    restart)
        if [[ "$target" == "all" ]]; then
            restart_side left
            restart_side right
        else
            restart_side "$(resolve_side "$target")"
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac
