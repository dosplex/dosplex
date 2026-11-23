#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"
session_wrapper="${DOSPLEX_WAYLAND_SESSION_WRAPPER:-/usr/local/libexec/dosplex-wayland-side-wrapper.sh}"
session_payload="${DOSPLEX_SESSION_PAYLOAD:-files}"
runtime_root="${DOSPLEX_NESTED_RUNTIME_ROOT:-/tmp/dosplex-nested-wayland}"
side_root="/var/lib/dosplex/sides"
screen_width="${DOSPLEX_HOST_WIDTH:-1280}"
screen_height="${DOSPLEX_HOST_HEIGHT:-800}"
host_runtime_dir="${DOSPLEX_RUNTIME_DIR:-/run/dosplex}"
host_wayland_display="${DOSPLEX_WAYLAND_DISPLAY:-dosplex-0}"
host_wayland_socket="${host_runtime_dir}/${host_wayland_display}"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing dependency: $1" >&2
        exit 1
    }
}

for cmd in kwin_wayland dbus-run-session runuser loginctl; do
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
    local runtime_dir user_home user_shell side_socket physical_x physical_y side_role

    user_home="$(getent passwd "$user" | cut -d: -f6)"
    user_shell="$(getent passwd "$user" | cut -d: -f7)"
    runtime_dir="$(user_runtime_dir "$user")"
    side_socket="dosplex-${side}-0"
    host_link_name="dosplex-host-${side}-0"
    physical_x=0
    physical_y=0
    side_role="guest"
    if [[ "$side" == "left" ]]; then
        side_role="primary"
    fi
    if [[ "$side" == "right" ]]; then
        physical_x="$half_width"
    fi

    ln -snf "$host_wayland_socket" "${runtime_dir}/${host_link_name}"

    # Detect whether to use XWayland bridge for kwin.
    # When DOSPLEX_KWIN_X11=1, kwin runs as an X11 client of the host
    # compositor's XWayland, bypassing DMA-BUF import issues that cause
    # black screens when kwin is a native Wayland client of wlroots.
    local kwin_mode="${DOSPLEX_KWIN_X11:-0}"

    if [[ "$kwin_mode" == "1" ]]; then
        # X11 mode: kwin connects to XWayland, not native Wayland.
        # The host compositor handles X11 surfaces via its own XWayland server.
        # kwin creates its own Wayland socket for sub-clients.
        runuser -u "$user" -- env \
            HOME="$user_home" \
            USER="$user" \
            LOGNAME="$user" \
            SHELL="$user_shell" \
            XDG_RUNTIME_DIR="$runtime_dir" \
            DISPLAY="${DOSPLEX_X11_DISPLAY:-:0}" \
            XDG_SESSION_TYPE="wayland" \
            DOSPLEX_SIDE="$side" \
            DOSPLEX_OWNER="$user" \
            DOSPLEX_SIDE_ROLE="$side_role" \
            DOSPLEX_SIDE_SOCKET="$side_socket" \
            DOSPLEX_HOST_WAYLAND_SOCKET="$host_wayland_socket" \
            DOSPLEX_OUTPUT_NAME="dosplex-${side}" \
            DOSPLEX_OUTPUT_DESCRIPTION="DosPlex ${side} logical output" \
            DOSPLEX_OUTPUT_WIDTH="$width" \
            DOSPLEX_OUTPUT_HEIGHT="$height" \
            DOSPLEX_OUTPUT_SCALE_MILLI="1000" \
            DOSPLEX_OUTPUT_PHYSICAL_X="$physical_x" \
            DOSPLEX_OUTPUT_PHYSICAL_Y="$physical_y" \
            DOSPLEX_OUTPUT_LOGICAL_X="0" \
            DOSPLEX_OUTPUT_LOGICAL_Y="0" \
            DOSPLEX_SESSION_SCRIPT="$session_script" \
            DOSPLEX_SESSION_PAYLOAD="$session_payload" \
            dbus-run-session \
            kwin_wayland \
            --x11-display "${DOSPLEX_X11_DISPLAY:-:0}" \
            --socket "$side_socket" \
            --width "$width" \
            --height "$height" \
            --no-lockscreen \
            --no-global-shortcuts \
            --no-kactivities \
            --exit-with-session "$session_wrapper" \
            >"${log_dir}/kwin.log" 2>&1 &
    else
        # Native Wayland mode: kwin connects as a Wayland client.
        # Force Qt to use software rendering to avoid DMA-BUF buffer issues
        # that cause black screens when kwin is nested in wlroots.
        runuser -u "$user" -- env \
            HOME="$user_home" \
            USER="$user" \
            LOGNAME="$user" \
            SHELL="$user_shell" \
            XDG_RUNTIME_DIR="$runtime_dir" \
            XDG_SESSION_TYPE="wayland" \
            QT_QPA_PLATFORM="wayland" \
            WAYLAND_DISPLAY="$host_link_name" \
            QT_QUICK_BACKEND=software \
            QT_OPENGL=software \
            LIBGL_ALWAYS_SOFTWARE=1 \
            QSG_RENDER_LOOP=basic \
            KWIN_COMPOSE=O2 \
            DOSPLEX_SIDE="$side" \
            DOSPLEX_OWNER="$user" \
            DOSPLEX_SIDE_ROLE="$side_role" \
            DOSPLEX_SIDE_SOCKET="$side_socket" \
            DOSPLEX_HOST_WAYLAND_SOCKET="$host_wayland_socket" \
            DOSPLEX_OUTPUT_NAME="dosplex-${side}" \
            DOSPLEX_OUTPUT_DESCRIPTION="DosPlex ${side} logical output" \
            DOSPLEX_OUTPUT_WIDTH="$width" \
            DOSPLEX_OUTPUT_HEIGHT="$height" \
            DOSPLEX_OUTPUT_SCALE_MILLI="1000" \
            DOSPLEX_OUTPUT_PHYSICAL_X="$physical_x" \
            DOSPLEX_OUTPUT_PHYSICAL_Y="$physical_y" \
            DOSPLEX_OUTPUT_LOGICAL_X="0" \
            DOSPLEX_OUTPUT_LOGICAL_Y="0" \
            DOSPLEX_SESSION_SCRIPT="$session_script" \
            DOSPLEX_SESSION_PAYLOAD="$session_payload" \
            dbus-run-session \
            kwin_wayland \
            --wayland-display "$host_link_name" \
            --socket "$side_socket" \
            --width "$width" \
            --height "$height" \
            --no-lockscreen \
            --no-global-shortcuts \
            --no-kactivities \
            --exit-with-session "$session_wrapper" \
            >"${log_dir}/kwin.log" 2>&1 &
    fi
    local kwin_pid=$!

    for _ in $(seq 1 80); do
        if [[ -S "${runtime_dir}/${side_socket}" ]]; then
            break
        fi
        sleep 0.25
    done

    write_side_state "$side" "$user" "$kwin_pid"

    if [[ "$side" == "left" ]]; then
        LEFT_KWIN_PID="$kwin_pid"
    else
        RIGHT_KWIN_PID="$kwin_pid"
    fi
}

cleanup() {
    set +e
    clear_side_state
    kill "${LEFT_KWIN_PID:-}" "${RIGHT_KWIN_PID:-}" >/dev/null 2>&1
    wait "${LEFT_KWIN_PID:-}" "${RIGHT_KWIN_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

wait_for_host_wayland

# Allow local users to connect to the host's XWayland display
# (compositor runs XWayland as root; deck/guest need access)
if [[ "${DOSPLEX_KWIN_X11:-1}" == "1" ]]; then
    xhost +local: >/dev/null 2>&1 || true
fi

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

echo "DosPlex nested Wayland sessions active on ${host_wayland_socket}"
echo "left=${left_user} socket=dosplex-left-0"
echo "right=${right_user} socket=dosplex-right-0"

wait -n "$LEFT_KWIN_PID" "$RIGHT_KWIN_PID"
