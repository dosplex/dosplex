#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="${DOSPLEX_LEFT_USER:-deck}"
right_user="${DOSPLEX_RIGHT_USER:-guest}"
session_script="${DOSPLEX_SESSION_SCRIPT:-/usr/local/libexec/dosplex-user-side-session.sh}"
session_wrapper="${DOSPLEX_SESSION_WRAPPER:-/usr/local/libexec/dosplex-gamescope-side-wrapper.sh}"
wayland_wrapper="${DOSPLEX_WAYLAND_WRAPPER:-/usr/local/libexec/dosplex-wayland-side-wrapper.sh}"
shim_script="${DOSPLEX_SHIM_SCRIPT:-/usr/local/libexec/steamos-session-select-shim.sh}"
session_payload="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
audio_router="${DOSPLEX_AUDIO_ROUTER:-/usr/local/bin/dosplex-audio-router.py}"
audio_server="${DOSPLEX_AUDIO_SHARED_SERVER:-127.0.0.1}"

host_wayland_socket="/run/dosplex/dosplex-0"
screen_width="${DOSPLEX_SCREEN_WIDTH:-1280}"
screen_height="${DOSPLEX_SCREEN_HEIGHT:-800}"
half_width=$((screen_width / 2))

user_home_dir() {
    getent passwd "$1" | cut -d: -f6
}

user_runtime_dir() {
    local uid
    uid=$(getent passwd "$1" | cut -d: -f3)
    echo "/run/user/${uid}"
}

setup_shared_audio() {
    if [[ "${DOSPLEX_AUDIO_MIX:-1}" != "1" ]]; then
        return 0
    fi
    if [[ -x "$audio_router" ]]; then
        "$audio_router" monitor-shared >>/tmp/dosplex-nested-gamescope/audio.log 2>&1 &
    fi
}

write_side_state() {
    local side="$1" owner="$2" pid="$3" runtime_dir="$4" mode="$5"
    local state_dir="/var/lib/dosplex/sides/${side}"
    sudo mkdir -p "$state_dir"
    
    # Write state.env for the wrapper to read
    sudo tee "${state_dir}/state.env" >/dev/null <<EOF
export SIDE="${side}"
export USER="${owner}"
export PID="${pid}"
export DOSPLEX_USER_RUNTIME_DIR="${runtime_dir}"
export UPDATED_AT="$(date --iso-8601=seconds)"
EOF

    # Write session.json for the compositor to read
    sudo tee "${state_dir}/session.json" >/dev/null <<EOF
{
  "side": "${side}",
  "owner": "${owner}",
  "pid": ${pid},
  "active": true,
  "lifecycle": "running",
  "mode": "${mode}"
}
EOF
}

read_side_mode() {
    local side="$1"
    local state_dir="/var/lib/dosplex/sides/${side}"
    local mode
    mode=$(sudo cat "${state_dir}/mode" 2>/dev/null || true)
    case "$mode" in
        desktop|gaming) printf '%s\n' "$mode" ;;
        *) printf 'gaming\n' ;;
    esac
}

read_side_geometry() {
    local side="$1" fallback_w="$2" fallback_h="$3"
    python3 - "$side" "$fallback_w" "$fallback_h" <<'PY'
import json
import sys

side, fallback_w, fallback_h = sys.argv[1], sys.argv[2], sys.argv[3]
scale_mode = "fit"
role = "guest"
try:
    with open("/run/dosplex/state.json", "r", encoding="utf-8") as fh:
        state = json.load(fh)
    entry = state.get(side) or {}
    width = int(entry.get("width") or fallback_w)
    height = int(entry.get("height") or fallback_h)
    role = str(entry.get("role") or role)
    active_scale = str(entry.get("scale_mode") or state.get("scale_mode") or scale_mode)
    pending_scale = str(entry.get("pending_scale_mode") or active_scale)
    restart_required = bool(entry.get("scale_restart_required"))
    scale_mode = pending_scale if restart_required else active_scale
except Exception:
    width = int(fallback_w)
    height = int(fallback_h)

if width <= 0:
    width = int(fallback_w)
if height <= 0:
    height = int(fallback_h)
print(f"{width} {height} {scale_mode} {role}")
PY
}

watch_side_geometry() {
    local side="$1" pid="$2" width="$3" height="$4" scale_mode="$5" role="$6"
    while kill -0 "$pid" 2>/dev/null; do
        read -r current_w current_h current_scale_mode current_role < <(read_side_geometry "$side" "$width" "$height")
        local native_scale=0
        if [[ "$scale_mode" == "native" || "$scale_mode" == "1:1" || "$scale_mode" == "no-scaling" ]]; then
            native_scale=1
        fi
        if [[ "$current_scale_mode" == "native" || "$current_scale_mode" == "1:1" || "$current_scale_mode" == "no-scaling" ]]; then
            native_scale=1
        fi
        if (( native_scale )) && [[ "$current_w" != "$width" || "$current_h" != "$height" || "$current_role" != "$role" ]]; then
            sudo pkill -TERM -P "$pid" 2>/dev/null || true
            sudo kill -TERM "$pid" 2>/dev/null || true
            return 0
        fi
        sleep 1
    done
}

supervisor_loop() {
    local side="$1" user="$2"
    local user_home log_dir width height scale_mode role runtime_dir host_link_name shim_dir
    
    user_home=$(user_home_dir "$user")
    log_dir="/tmp/dosplex-nested-gamescope/${side}"
    mkdir -p "$log_dir"
    chown "$user:$user" "$log_dir"

    width="$half_width"
    height="$screen_height"
    runtime_dir=$(user_runtime_dir "$user")
    host_link_name="dosplex-host-${side}-0"
    shim_dir="/tmp/dosplex-shims-${side}"

    mkdir -p "$runtime_dir"
    chown "$user:$user" "$runtime_dir"
    chmod 700 "$runtime_dir"
    mkdir -p "$shim_dir"
    if [[ -x "$shim_script" ]]; then
        ln -snf "$shim_script" "${shim_dir}/steamos-session-select"
        chown -R "$user:$user" "$shim_dir" 2>/dev/null || true
    fi

    for i in {1..50}; do
        if [[ -S "${host_wayland_socket}" ]]; then break; fi
        sleep 0.2
    done

    while true; do
        local mode
        mode=$(read_side_mode "$side")
        read -r width height scale_mode role < <(read_side_geometry "$side" "$half_width" "$screen_height")
        echo "[dosplex-supervisor] Side ${side}: Preparing environment..." >> "${log_dir}/supervisor.log"

        # SteamOS can remove /run/user/$uid when a side session exits. Recreate
        # it on every launch attempt so gamescope always has a valid runtime dir.
        mkdir -p "$runtime_dir"
        chown "$user:$user" "$runtime_dir"
        chmod 700 "$runtime_dir"
        
        sudo -u "$user" rm -f "${runtime_dir}/gamescope-"*
        sudo -u "$user" rm -f "${runtime_dir}/wayland-"*
        sudo -u "$user" rm -f "${runtime_dir}/dosplex-${side}-"*
        ln -snf "${host_wayland_socket}" "${runtime_dir}/${host_link_name}"
        chown -h "$user:$user" "${runtime_dir}/${host_link_name}"

        # Unified Launch Logic: Always use Gamescope as the container
        # to ensure stability and proper seat management for both Gaming and Desktop.
        local active_payload
        local active_log
        if [[ "$mode" == "desktop" ]]; then
            active_payload="desktop"
            # Policy marker: desktop mode exports DOSPLEX_SESSION_PAYLOAD=desktop via active_payload below.
            active_log="${log_dir}/gamescope-desktop.log"
            echo "[dosplex-supervisor] Side ${side}: Launching desktop mode..." >> "${log_dir}/supervisor.log"
        else
            active_payload="$session_payload"
            active_log="${log_dir}/gamescope.log"
            echo "[dosplex-supervisor] Side ${side}: Launching gaming mode..." >> "${log_dir}/supervisor.log"
        fi

        (
            for i in {1..40}; do
                if [[ -S "${runtime_dir}/gamescope-0" ]]; then
                    ln -snf "gamescope-0" "${runtime_dir}/wayland-0"
                    chown -h "$user:$user" "${runtime_dir}/wayland-0"
                    break
                fi
                sleep 0.2
            done
        ) &

        local render_width=1280
        local render_height=800
        local scaler="fit"
        if [[ "$scale_mode" == "stretch" ]]; then
            scaler="stretch"
        fi
        if [[ "$scale_mode" == "native" || "$scale_mode" == "1:1" || "$scale_mode" == "no-scaling" ]]; then
            render_width="$width"
            render_height="$height"
            scaler="fit"
        fi

        # DBus Isolation: Wrap the entire gamescope session in a unique DBus bus
        sudo -u "$user" env \
            HOME="$user_home" \
            USER="$user" \
            XDG_RUNTIME_DIR="$runtime_dir" \
            PATH="${shim_dir}:$PATH" \
            PULSE_SERVER="$audio_server" \
            WAYLAND_DISPLAY="$host_link_name" \
            XDG_CURRENT_DESKTOP="gamescope" \
            GAMESCOPE_WSI_PRESENT_WAIT=0 \
            GAMESCOPE_WSI_XWAYLAND_WAIT=0 \
            DOSPLEX_SIDE="$side" \
            DOSPLEX_OWNER="$user" \
            DOSPLEX_USER_RUNTIME_DIR="$runtime_dir" \
            DOSPLEX_SESSION_SCRIPT="$session_script" \
            DOSPLEX_DESKTOP_MODE=direct \
            DOSPLEX_SESSION_PAYLOAD="$active_payload" \
            dbus-run-session -- \
            gamescope \
                -e \
                --backend wayland \
                --xwayland-count 1 \
                --expose-wayland \
                --borderless \
                --force-composition \
                -W "$width" \
                -H "$height" \
                -w "$render_width" \
                -h "$render_height" \
                -S "$scaler" \
                -- \
                "$session_wrapper" "$side" "$user" \
        >>"$active_log" 2>&1 &
        local session_pid=$!
        write_side_state "$side" "$user" "$session_pid" "$runtime_dir" "$mode"
        watch_side_geometry "$side" "$session_pid" "$width" "$height" "$scale_mode" "$role" &
        local geometry_watch_pid=$!
        
        wait "$session_pid" || true
        
        # Crash Recovery: If we were in desktop mode and it exited, return to gaming (greeter)
        if [[ "$mode" == "desktop" ]]; then
            echo "[dosplex-supervisor] Side ${side}: Desktop session ended, returning to greeter." >> "${log_dir}/supervisor.log"
            echo "gaming" | sudo tee "/var/lib/dosplex/sides/${side}/mode" >/dev/null
        fi

        kill "$geometry_watch_pid" >/dev/null 2>&1 || true
        wait "$geometry_watch_pid" >/dev/null 2>&1 || true

        echo "[dosplex-supervisor] Side ${side} session exited. Cooling down..." >> "${log_dir}/supervisor.log"
        
        # Cleanup side-specific processes for this user
        if [[ -n "${session_pid:-}" ]]; then
            sudo pkill -P "$session_pid" || true
        fi
        sudo -u "$user" pkill -u "$user" -f gamescope || true
        sudo -u "$user" pkill -u "$user" -f kwin_wayland || true
        
        sleep 1
    done
}

# Ensure log root exists
mkdir -p /tmp/dosplex-nested-gamescope
chmod 777 /tmp/dosplex-nested-gamescope

setup_shared_audio

# Read current layout from compositor state
current_layout="single"
if [[ -f /run/dosplex/state.json ]]; then
    current_layout=$(python3 -c "import json; d=json.load(open('/run/dosplex/state.json')); print(d.get('layout','single'))" 2>/dev/null || echo "single")
fi
# Also check env var as override
if [[ -n "${DOSPLEX_INITIAL_LAYOUT:-}" ]]; then
    current_layout="$DOSPLEX_INITIAL_LAYOUT"
fi

# Start primary supervisor always
supervisor_loop left "$left_user" &
primary_supervisor_pid=$!

# Start guest supervisor only in split layout
if [[ "$current_layout" != "single" ]]; then
    supervisor_loop right "$right_user" &
    guest_supervisor_pid=$!
    wait $primary_supervisor_pid $guest_supervisor_pid
else
    echo "[dosplex] Single-screen mode: guest supervisor skipped to save resources" >> /tmp/dosplex-nested-gamescope/layout.log
    wait $primary_supervisor_pid
fi
