#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-hybrid-left-x11}"
side_root="/var/lib/dosplex/sides"
host_runtime_dir="${DOSPLEX_RUNTIME_DIR:-/run/dosplex}"
host_wayland_display="${DOSPLEX_WAYLAND_DISPLAY:-dosplex-0}"
host_wayland_socket="${host_runtime_dir}/${host_wayland_display}"
host_display="${DOSPLEX_HOST_X11_DISPLAY:-:0}"
default_payload="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
screen_width="${DOSPLEX_HOST_WIDTH:-1280}"
screen_height="${DOSPLEX_HOST_HEIGHT:-800}"
stopping=0

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in runuser xauth mcookie xdpyinfo Xephyr; do
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

launch_left_x11() {
    local user="$1" log_dir="$2" nested_display="$3" auth_file="$4"
    local user_home user_shell user_runtime_dir session_cmd payload input_leap_log launcher_script

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    user_runtime_dir="/run/user/$(id -u "$user")"
    install -d -m 700 -o "$user" -g "$user" "$user_runtime_dir"
    chown -R "$user:$user" "$log_dir"

    payload="${DOSPLEX_LEFT_SESSION_PAYLOAD:-$default_payload}"
    session_cmd="${DOSPLEX_LEFT_SESSION_CMD:-}"
    input_leap_log="${log_dir}/input-leap.log"
    launcher_script="${log_dir}/launch-left-session.sh"

    DISPLAY="$host_display" Xephyr "$nested_display" \
        -screen "$((screen_width / 2))x${screen_height}" \
        -title "DosPlex Left" \
        -resizeable \
        -fakexa \
        -softCursor \
        +bs \
        -no-host-grab \
        -auth "$auth_file" \
        >"${log_dir}/xephyr.log" 2>&1 &
    LEFT_XEPHYR_PID=$!

    for _ in $(seq 1 80); do
        if DISPLAY="$nested_display" XAUTHORITY="$auth_file" xdpyinfo >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done

    cat >"$launcher_script" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ -x /usr/local/bin/input-leap-dosplex-client.sh ]]; then
    /usr/local/bin/input-leap-dosplex-client.sh >>"${input_leap_log}" 2>&1 &
fi
EOF
    if [[ -n "$session_cmd" ]]; then
        cat >>"$launcher_script" <<EOF
exec bash -lc '$session_cmd'
EOF
    else
        cat >>"$launcher_script" <<EOF
exec /usr/local/libexec/dosplex-user-side-session.sh left "$user"
EOF
    fi
    chown "$user:$user" "$launcher_script"
    chmod 0755 "$launcher_script"

    runuser -u "$user" -- env \
        HOME="$user_home" \
        USER="$user" \
        LOGNAME="$user" \
        SHELL="$user_shell" \
        DISPLAY="$nested_display" \
        XAUTHORITY="$auth_file" \
        XDG_RUNTIME_DIR="$user_runtime_dir" \
        XDG_SESSION_TYPE="x11" \
        DOSPLEX_SIDE="left" \
        DOSPLEX_OWNER="$user" \
        DOSPLEX_SESSION_PAYLOAD="$payload" \
        DOSPLEX_INPUT_LEAP_DISPLAY="$nested_display" \
        dbus-run-session \
        "$launcher_script" \
        >"${log_dir}/session.log" 2>&1 &
    LEFT_SESSION_PID=$!
    write_side_state left "$user" "$LEFT_SESSION_PID"
}

launch_right_wayland() {
    local user="$1" log_dir="$2"
    local user_home user_shell user_runtime_dir host_link_name session_cmd payload

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    user_runtime_dir="/run/user/$(id -u "$user")"
    install -d -m 700 -o "$user" -g "$user" "$user_runtime_dir"

    host_link_name="dosplex-host-right-0"
    ln -snf "$host_wayland_socket" "${user_runtime_dir}/${host_link_name}"

    payload="${DOSPLEX_RIGHT_SESSION_PAYLOAD:-$default_payload}"
    session_cmd="${DOSPLEX_RIGHT_SESSION_CMD:-}"

    runuser -u "$user" -- env \
        HOME="$user_home" \
        USER="$user" \
        LOGNAME="$user" \
        SHELL="$user_shell" \
        XDG_RUNTIME_DIR="$user_runtime_dir" \
        XDG_SESSION_TYPE="wayland" \
        QT_QPA_PLATFORM="wayland" \
        WAYLAND_DISPLAY="$host_link_name" \
        DOSPLEX_SIDE="right" \
        DOSPLEX_OWNER="$user" \
        DOSPLEX_SESSION_PAYLOAD="$payload" \
        DOSPLEX_SESSION_CMD="$session_cmd" \
        /usr/local/libexec/dosplex-user-side-session.sh right "$user" \
        >"${log_dir}/session.log" 2>&1 &
    RIGHT_SESSION_PID=$!
    write_side_state right "$user" "$RIGHT_SESSION_PID"
}

supervise_left() {
    local display_name="$1" auth_file="$2" log_dir="$3"
    local status=0

    while [[ "$stopping" -eq 0 ]]; do
        launch_left_x11 "$left_user" "$log_dir" "$display_name" "$auth_file"
        status=0
        wait -n "$LEFT_SESSION_PID" "$LEFT_XEPHYR_PID" || status=$?
        kill "${LEFT_SESSION_PID:-}" "${LEFT_XEPHYR_PID:-}" >/dev/null 2>&1 || true
        wait "${LEFT_SESSION_PID:-}" "${LEFT_XEPHYR_PID:-}" >/dev/null 2>&1 || true
        write_side_state left "$left_user" 0 false
        [[ "$stopping" -ne 0 ]] && break
        echo "[dosplex-hybrid-left-x11] left exited status=${status}, relaunching" >&2
        sleep 1
    done
}

supervise_right() {
    local log_dir="$1"
    local status=0

    while [[ "$stopping" -eq 0 ]]; do
        launch_right_wayland "$right_user" "$log_dir"
        status=0
        wait "$RIGHT_SESSION_PID" || status=$?
        write_side_state right "$right_user" 0 false
        [[ "$stopping" -ne 0 ]] && break
        echo "[dosplex-hybrid-left-x11] right exited status=${status}, relaunching" >&2
        sleep 1
    done
}

cleanup() {
    set +e
    stopping=1
    clear_side_state
    kill "${LEFT_SUPERVISOR_PID:-}" "${RIGHT_SUPERVISOR_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SUPERVISOR_PID:-}" "${RIGHT_SUPERVISOR_PID:-}" >/dev/null 2>&1
    kill "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" "${LEFT_XEPHYR_PID:-}" >/dev/null 2>&1
    wait "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" "${LEFT_XEPHYR_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_wayland
wait_for_host_x

left_dir="${runtime_root}/left"
right_dir="${runtime_root}/right"
mkdir -p "$left_dir" "$right_dir"

left_display="$(find_free_display 110)"
left_auth="${left_dir}/Xauthority"
prepare_auth_file "$left_display" "$left_auth" "$left_user"

write_side_state left "$left_user" 0 false
write_side_state right "$right_user" 0 false

# Give the host compositor time to claim the left side before the first Xephyr
# host window appears, otherwise wlroots may ignore the initial Xwayland toplevel.
sleep 2
supervise_left "$left_display" "$left_auth" "$left_dir" &
LEFT_SUPERVISOR_PID=$!
sleep 1
supervise_right "$right_dir" &
RIGHT_SUPERVISOR_PID=$!

echo "DosPlex hybrid sessions active"
echo "left=${left_user} display=${left_display} input-leap=enabled"
echo "right=${right_user} wayland=${host_wayland_socket}"

wait -n "$LEFT_SUPERVISOR_PID" "$RIGHT_SUPERVISOR_PID"
