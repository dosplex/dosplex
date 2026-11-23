#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
host_display="${DOSPLEX_HOST_X11_DISPLAY:-:0}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"
session_payload="${DOSPLEX_SESSION_PAYLOAD:-files}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-nested-xwayland}"
side_root="/var/lib/dosplex/sides"
screen_width="${DOSPLEX_HOST_WIDTH:-1280}"
screen_height="${DOSPLEX_HOST_HEIGHT:-800}"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in Xephyr xauth mcookie xdpyinfo dbus-run-session runuser; do
    need_cmd "$cmd"
done

mkdir -p "$runtime_root" "$side_root/left" "$side_root/right"

wait_for_host_x() {
    local try
    for try in $(seq 1 80); do
        if DISPLAY="$host_display" xdpyinfo >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.25
    done
    echo "host X display $host_display did not become ready" >&2
    return 1
}

find_free_display() {
    local start="$1"
    local n
    for n in $(seq "$start" $((start + 50))); do
        if [[ ! -e "/tmp/.X${n}-lock" && ! -S "/tmp/.X11-unix/X${n}" ]]; then
            printf ':%s\n' "$n"
            return 0
        fi
    done
    echo "unable to find a free nested X display" >&2
    return 1
}

prepare_auth_file() {
    local display_name="$1"
    local auth_file="$2"
    local owner="$3"
    local cookie host_name

    cookie="$(mcookie)"
    host_name="$(uname -n)"
    : >"$auth_file"
    chmod 0600 "$auth_file"
    for entry in \
        "$display_name" \
        "unix${display_name}" \
        "${host_name}/unix${display_name}" \
        "${host_name}${display_name}" \
        "localhost${display_name}" \
        "localhost/unix${display_name}"; do
        xauth -f "$auth_file" add "$entry" MIT-MAGIC-COOKIE-1 "$cookie" >/dev/null || true
    done
    chown "$owner:$owner" "$auth_file"
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
    local side="$1" user="$2" nested_display="$3" auth_file="$4" title="$5" log_dir="$6"
    local dims width height runtime_dir user_home user_shell

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    runtime_dir="$(user_runtime_dir "$user")"

    DISPLAY="$host_display" Xephyr "$nested_display" \
        -screen "${half_width}x${screen_height}" \
        -title "$title" \
        -resizeable \
        -fakexa \
        -softCursor \
        +bs \
        -no-host-grab \
        -auth "$auth_file" \
        >"${log_dir}/xephyr.log" 2>&1 &
    local xephyr_pid=$!

    for _ in $(seq 1 80); do
        if DISPLAY="$nested_display" XAUTHORITY="$auth_file" xdpyinfo >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done

    runuser -u "$user" -- env \
        HOME="$user_home" \
        USER="$user" \
        LOGNAME="$user" \
        SHELL="$user_shell" \
        DISPLAY="$nested_display" \
        XAUTHORITY="$auth_file" \
        XDG_RUNTIME_DIR="$runtime_dir" \
        XDG_SESSION_TYPE="x11" \
        DOSPLEX_SIDE="$side" \
        DOSPLEX_SESSION_PAYLOAD="$session_payload" \
        dbus-run-session \
        "$session_script" "$side" "$user" \
        >"${log_dir}/session.log" 2>&1 &
    local session_pid=$!

    write_side_state "$side" "$user" "$session_pid"

    if [[ "$side" == "left" ]]; then
        LEFT_XEPHYR_PID="$xephyr_pid"
        LEFT_SESSION_PID="$session_pid"
    else
        RIGHT_XEPHYR_PID="$xephyr_pid"
        RIGHT_SESSION_PID="$session_pid"
    fi
}

cleanup() {
    set +e
    clear_side_state
    kill "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
    kill "${LEFT_XEPHYR_PID:-}" "${RIGHT_XEPHYR_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
    wait "${LEFT_XEPHYR_PID:-}" "${RIGHT_XEPHYR_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_x

half_width=$((screen_width / 2))

left_display="$(find_free_display 110)"
right_display="$(find_free_display $(( ${left_display#:} + 1 )))"
left_dir="${runtime_root}/left"
right_dir="${runtime_root}/right"
mkdir -p "$left_dir" "$right_dir"
left_auth="${left_dir}/Xauthority"
right_auth="${right_dir}/Xauthority"
prepare_auth_file "$left_display" "$left_auth" "$left_user"
prepare_auth_file "$right_display" "$right_auth" "$right_user"

write_side_state left "$left_user" 0
write_side_state right "$right_user" 0
sleep 1

launch_side left "$left_user" "$left_display" "$left_auth" "DosPlex Left" "$left_dir"
sleep 1
launch_side right "$right_user" "$right_display" "$right_auth" "DosPlex Right" "$right_dir"

echo "DosPlex nested X sessions active on ${host_display}"
echo "left=${left_user} display=${left_display}"
echo "right=${right_user} display=${right_display}"

wait -n "$LEFT_SESSION_PID" "$RIGHT_SESSION_PID" "$LEFT_XEPHYR_PID" "$RIGHT_XEPHYR_PID"
