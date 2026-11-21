#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

# Dosplex nested side stack using weston with pixman (software) renderer.
# Uses shared memory buffers instead of DMA-BUF, avoiding the black-screen
# issue when nested Wayland compositors submit DMA-BUF to wlroots.

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"
session_payload="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-nested-weston}"
side_root="/var/lib/dosplex/sides"
screen_width="${DOSPLEX_HOST_WIDTH:-1280}"
screen_height="${DOSPLEX_HOST_HEIGHT:-800}"
host_runtime_dir="${DOSPLEX_RUNTIME_DIR:-/run/dosplex}"
host_wayland_display="${DOSPLEX_WAYLAND_DISPLAY:-dosplex-0}"
host_wayland_socket="${host_runtime_dir}/${host_wayland_display}"
use_pixman="${DOSPLEX_WESTON_PIXMAN:-1}"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in weston runuser; do
    need_cmd "$cmd"
done

mkdir -p "$runtime_root" "$side_root/left" "$side_root/right"

wait_for_host_wayland() {
    local try
    for try in $(seq 1 80); do
        if [[ -S "$host_wayland_socket" ]]; then
            return 0
        fi
        sleep 0.25
    done
    echo "host Wayland socket $host_wayland_socket did not become ready" >&2
    return 1
}

user_runtime_dir() {
    local user="$1" uid dir
    uid="$(id -u "$user")"
    dir="/run/user/${uid}"
    install -d -m 700 -o "$user" -g "$user" "$dir"
    printf '%s\n' "$dir"
}

write_side_state() {
    local side="$1" owner="$2" pid="$3"
    cat >"${side_root}/${side}/session.json" <<EOF
{
  "side": "${side}",
  "owner": "${owner}",
  "pid": ${pid},
  "active": true
}
EOF
}

clear_side_state() {
    rm -f "${side_root}/left/session.json" "${side_root}/right/session.json"
}

launch_side() {
    local side="$1" user="$2" width="$3" height="$4" log_dir="$5"
    local user_home user_shell side_socket physical_x physical_y side_role

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    side_socket="dosplex-${side}-0"
    physical_x=0
    physical_y=0
    side_role="guest"
    if [[ "$side" == "left" ]]; then
        side_role="primary"
    fi
    if [[ "$side" == "right" ]]; then
        physical_x="$half_width"
    fi

    local pixman_flag=""
    if [[ "$use_pixman" == "1" ]]; then
        pixman_flag="--use-pixman"
    fi

    # Export env vars for the session script
    export DOSPLEX_SIDE="$side"
    export DOSPLEX_OWNER="$user"
    export DOSPLEX_SIDE_ROLE="$side_role"
    export DOSPLEX_SIDE_SOCKET="$side_socket"
    export DOSPLEX_HOST_WAYLAND_SOCKET="$host_wayland_socket"
    export DOSPLEX_OUTPUT_NAME="dosplex-${side}"
    export DOSPLEX_OUTPUT_DESCRIPTION="Dosplex ${side} logical output"
    export DOSPLEX_OUTPUT_WIDTH="$width"
    export DOSPLEX_OUTPUT_HEIGHT="$height"
    export DOSPLEX_OUTPUT_SCALE_MILLI="1000"
    export DOSPLEX_OUTPUT_PHYSICAL_X="$physical_x"
    export DOSPLEX_OUTPUT_PHYSICAL_Y="$physical_y"
    export DOSPLEX_OUTPUT_LOGICAL_X="0"
    export DOSPLEX_OUTPUT_LOGICAL_Y="0"
    export DOSPLEX_SESSION_SCRIPT="$session_script"
    export DOSPLEX_SESSION_PAYLOAD="$session_payload"

    # Launch weston as the nested compositor.
    # Use /run/dosplex as XDG_RUNTIME_DIR since /run/user/UID gets cleaned
    # by systemd-logind for non-logged-in users. The host compositor socket
    # is at /run/dosplex/dosplex-0, so WAYLAND_DISPLAY=dosplex-0 works directly.
    runuser -u "$user" -- env \
        HOME="$user_home" \
        USER="$user" \
        LOGNAME="$user" \
        SHELL="$user_shell" \
        XDG_RUNTIME_DIR="$host_runtime_dir" \
        XDG_SESSION_TYPE="wayland" \
        WAYLAND_DISPLAY="$host_wayland_display" \
        weston \
        $pixman_flag \
        --width="$width" \
        --height="$height" \
        --socket="$side_socket" \
        >"${log_dir}/weston.log" 2>&1 &
    local weston_pid=$!

    for _ in $(seq 1 80); do
        if [[ -S "${host_runtime_dir}/${side_socket}" ]]; then
            break
        fi
        sleep 0.25
    done

    write_side_state "$side" "$user" "$weston_pid"

    if [[ "$side" == "left" ]]; then
        LEFT_PID="$weston_pid"
    else
        RIGHT_PID="$weston_pid"
    fi
}

cleanup() {
    set +e
    clear_side_state
    kill "${LEFT_PID:-}" "${RIGHT_PID:-}" >/dev/null 2>&1
    wait "${LEFT_PID:-}" "${RIGHT_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_wayland

half_width=$((screen_width / 2))
left_dir="${runtime_root}/left"
right_dir="${runtime_root}/right"
mkdir -p "$left_dir" "$right_dir"

write_side_state left "$left_user" 0
write_side_state right "$right_user" 0
sleep 1

launch_side left "$left_user" "$half_width" "$screen_height" "$left_dir"
sleep 1
launch_side right "$right_user" "$half_width" "$screen_height" "$right_dir"

echo "Dosplex nested Weston sessions active on ${host_wayland_socket}"
echo "left=${left_user} socket=dosplex-left-0"
echo "right=${right_user} socket=dosplex-right-0"

wait -n "$LEFT_PID" "$RIGHT_PID"
