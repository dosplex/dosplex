#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-direct-plasma}"
side_root="/var/lib/dosplex/sides"
host_runtime_dir="${DOSPLEX_RUNTIME_DIR:-/run/dosplex}"
host_wayland_display="${DOSPLEX_WAYLAND_DISPLAY:-dosplex-0}"
host_wayland_socket="${host_runtime_dir}/${host_wayland_display}"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in runuser dbus-run-session startplasma-wayland; do
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
    local side="$1" user="$2" log_dir="$3"
    local user_home user_shell user_runtime_dir host_link_name

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    user_runtime_dir="/run/user/$(id -u "$user")"
    install -d -m 700 -o "$user" -g "$user" "$user_runtime_dir"
    host_link_name="dosplex-host-${side}-0"
    ln -snf "$host_wayland_socket" "${user_runtime_dir}/${host_link_name}"

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
        dbus-run-session \
        startplasma-wayland \
        >"${log_dir}/plasma.log" 2>&1 &
    local session_pid=$!

    write_side_state "$side" "$user" "$session_pid"

    if [[ "$side" == "left" ]]; then
        LEFT_SESSION_PID="$session_pid"
    else
        RIGHT_SESSION_PID="$session_pid"
    fi
}

cleanup() {
    set +e
    clear_side_state
    kill "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_wayland

left_dir="${runtime_root}/left"
right_dir="${runtime_root}/right"
mkdir -p "$left_dir" "$right_dir"

write_side_state left "$left_user" 0
write_side_state right "$right_user" 0
sleep 1

launch_side left "$left_user" "$left_dir"
sleep 2
launch_side right "$right_user" "$right_dir"

echo "Dosplex direct Plasma Wayland sessions active on ${host_wayland_socket}"
echo "left=${left_user}"
echo "right=${right_user}"

wait -n "$LEFT_SESSION_PID" "$RIGHT_SESSION_PID"
