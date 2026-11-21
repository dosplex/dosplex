#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-direct-wayland}"
side_root="/var/lib/dosplex/sides"
host_runtime_dir="${DOSPLEX_RUNTIME_DIR:-/run/dosplex}"
host_wayland_display="${DOSPLEX_WAYLAND_DISPLAY:-dosplex-0}"
host_wayland_socket="${host_runtime_dir}/${host_wayland_display}"
host_width="${DOSPLEX_HOST_WIDTH:-1280}"
host_height="${DOSPLEX_HOST_HEIGHT:-800}"
default_payload="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
stopping=0

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in runuser; do
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

write_side_state() {
    local side="$1" owner="$2" pid="$3" active="${4:-true}"
    cat >"${side_root}/${side}/session.json" <<EOF
{
  "side": "${side}",
  "owner": "${owner}",
  "pid": ${pid},
  "active": ${active}
}
EOF
}

clear_side_state() {
    rm -f "${side_root}/left/session.json" "${side_root}/right/session.json"
}

launch_side() {
    local side="$1" user="$2" log_dir="$3"
    local user_home user_shell user_runtime_dir host_link_name session_cmd payload
    local side_width side_height physical_x physical_y side_role
    local payload_var cmd_var upper_side

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    user_runtime_dir="/run/user/$(id -u "$user")"
    install -d -m 700 -o "$user" -g "$user" "$user_runtime_dir"

    host_link_name="dosplex-host-${side}-0"
    ln -snf "$host_wayland_socket" "${user_runtime_dir}/${host_link_name}"

    upper_side="${side^^}"
    payload_var="DOSPLEX_${upper_side}_SESSION_PAYLOAD"
    cmd_var="DOSPLEX_${upper_side}_SESSION_CMD"
    payload="${!payload_var:-$default_payload}"
    session_cmd="${!cmd_var:-}"
    side_width=$((host_width / 2))
    side_height="$host_height"
    physical_x=0
    physical_y=0
    side_role="guest"
    if [[ "$side" == "left" ]]; then
        side_role="primary"
    fi
    if [[ "$side" == "right" ]]; then
        physical_x="$side_width"
    fi

    runuser -u "$user" -- env \
        HOME="$user_home" \
        USER="$user" \
        LOGNAME="$user" \
        SHELL="$user_shell" \
        XDG_RUNTIME_DIR="$user_runtime_dir" \
        XDG_SESSION_TYPE="wayland" \
        QT_QPA_PLATFORM="wayland" \
        PULSE_SERVER="127.0.0.1" \
        PULSE_SINK="dosplex_$side" \
        WAYLAND_DISPLAY="$host_link_name" \
        DOSPLEX_SIDE="$side" \
        DOSPLEX_OWNER="$user" \
        DOSPLEX_SIDE_ROLE="$side_role" \
        DOSPLEX_OUTPUT_NAME="dosplex-${side}" \
        DOSPLEX_OUTPUT_DESCRIPTION="Dosplex ${side} logical output" \
        DOSPLEX_OUTPUT_WIDTH="$side_width" \
        DOSPLEX_OUTPUT_HEIGHT="$side_height" \
        DOSPLEX_OUTPUT_SCALE_MILLI="1000" \
        DOSPLEX_OUTPUT_PHYSICAL_X="$physical_x" \
        DOSPLEX_OUTPUT_PHYSICAL_Y="$physical_y" \
        DOSPLEX_OUTPUT_LOGICAL_X="0" \
        DOSPLEX_OUTPUT_LOGICAL_Y="0" \
        DOSPLEX_SESSION_PAYLOAD="$payload" \
        DOSPLEX_SESSION_CMD="$session_cmd" \
        /usr/local/libexec/dosplex-user-side-session.sh "$side" "$user" \
        >"${log_dir}/session.log" 2>&1 &
    local session_pid=$!

    write_side_state "$side" "$user" "$session_pid"

    if [[ "$side" == "left" ]]; then
        LEFT_SESSION_PID="$session_pid"
    else
        RIGHT_SESSION_PID="$session_pid"
    fi
}

supervise_side() {
    local side="$1" user="$2" log_dir="$3"
    local session_pid=0 status=0

    trap '
        stopping=1
        if [[ "$session_pid" -gt 0 ]]; then
            kill "$session_pid" >/dev/null 2>&1 || true
            wait "$session_pid" >/dev/null 2>&1 || true
        fi
        exit 0
    ' INT TERM

    while [[ "$stopping" -eq 0 ]]; do
        launch_side "$side" "$user" "$log_dir"
        if [[ "$side" == "left" ]]; then
            session_pid="${LEFT_SESSION_PID:-0}"
        else
            session_pid="${RIGHT_SESSION_PID:-0}"
        fi

        status=0
        wait "$session_pid" || status=$?
        write_side_state "$side" "$user" 0 false

        if [[ "$stopping" -ne 0 ]]; then
            break
        fi

        echo "[dosplex-direct-wayland] side=${side} user=${user} exited status=${status}, relaunching" >&2
        sleep 1
    done
}

cleanup() {
    set +e
    stopping=1
    clear_side_state
    kill "${LEFT_SUPERVISOR_PID:-}" "${RIGHT_SUPERVISOR_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SUPERVISOR_PID:-}" "${RIGHT_SUPERVISOR_PID:-}" >/dev/null 2>&1
    kill "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_wayland

left_dir="${runtime_root}/left"
right_dir="${runtime_root}/right"
mkdir -p "$left_dir" "$right_dir"

write_side_state left "$left_user" 0 false
write_side_state right "$right_user" 0 false

supervise_side left "$left_user" "$left_dir" &
LEFT_SUPERVISOR_PID=$!
sleep 1
supervise_side right "$right_user" "$right_dir" &
RIGHT_SUPERVISOR_PID=$!

echo "Dosplex direct Wayland sessions active on ${host_wayland_socket}"
echo "left=${left_user}"
echo "right=${right_user}"

wait -n "$LEFT_SUPERVISOR_PID" "$RIGHT_SUPERVISOR_PID"
