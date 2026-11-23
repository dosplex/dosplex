#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

left_user="deck"
right_user="guest"
display="${DISPLAY:-:0}"

usage() {
  cat <<'EOF'
usage: start-dosplex-nested.sh [options]

Options:
  --left-user USER    Unix user for the left half (default: deck)
  --right-user USER   Unix user for the right half (default: guest)
  --display DISPLAY   Host X11 display (default: $DISPLAY or :0)
  --help              Show this help
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --left-user)
      left_user="${2:?missing value for --left-user}"
      shift 2
      ;;
    --right-user)
      right_user="${2:?missing value for --right-user}"
      shift 2
      ;;
    --display)
      display="${2:?missing value for --display}"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing dependency: $1" >&2
    exit 1
  }
}

for cmd in Xephyr wmctrl xauth mcookie xdpyinfo xrandr dbus-run-session sudo; do
  need_cmd "$cmd"
done

host_xauth="$(ps -o args= -C Xorg | sed -n 's/.*-auth \([^ ]*\).*/\1/p' | head -n 1)"
if [[ -z "$host_xauth" ]]; then
  echo "could not determine host XAUTHORITY" >&2
  exit 1
fi

screen_geometry="$(DISPLAY="$display" XAUTHORITY="$host_xauth" xrandr --current | awk '
  / connected primary / {
    for (i = 1; i <= NF; i++) {
      if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+$/) {
        print $i
        exit
      }
    }
  }
  / connected / && !seen {
    for (i = 1; i <= NF; i++) {
      if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+$/) {
        print $i
        seen = 1
        exit
      }
    }
  }
')"

if [[ -z "$screen_geometry" ]]; then
  dims="$(DISPLAY="$display" XAUTHORITY="$host_xauth" xdpyinfo | awk '/dimensions:/ {print $2; exit}')"
  screen_width="${dims%x*}"
  screen_height="${dims#*x}"
  screen_x=0
  screen_y=0
else
  read -r screen_width screen_height screen_x screen_y \
    <<<"$(printf '%s\n' "$screen_geometry" | sed -E 's/^([0-9]+)x([0-9]+)\+([0-9]+)\+([0-9]+)$/\1 \2 \3 \4/')"
fi

half_width=$((screen_width / 2))
nested_height="$screen_height"
host_name="$(uname -n)"
runtime_root="/tmp/dosplex-nested"
mkdir -p "$runtime_root"

find_free_display() {
  local start="$1"
  local n
  for n in $(seq "$start" $((start + 50))); do
    if [[ ! -e "/tmp/.X${n}-lock" && ! -S "/tmp/.X11-unix/X${n}" ]]; then
      printf ':%s\n' "$n"
      return 0
    fi
  done
  return 1
}

prepare_runtime_dir() {
  local user="$1"
  local dir="$runtime_root/runtime-$user"
  if [[ "$(id -un)" == "$user" ]]; then
    install -d -m 700 "$dir"
  else
    sudo install -d -m 700 -o "$user" -g "$user" "$dir"
  fi
  printf '%s\n' "$dir"
}

prepare_auth_file() {
  local display_name="$1"
  local auth_file="$2"
  local cookie

  cookie="$(mcookie)"
  : > "$auth_file"
  for entry in \
    "$display_name" \
    "unix${display_name}" \
    "${host_name}/unix${display_name}" \
    "${host_name}${display_name}" \
    "localhost${display_name}" \
    "localhost/unix${display_name}"; do
    xauth -f "$auth_file" add "$entry" MIT-MAGIC-COOKIE-1 "$cookie" >/dev/null || true
  done
  chmod 0644 "$auth_file"
}

default_payload() {
  cat <<'EOF'
export XDG_SESSION_TYPE=x11
export QT_QPA_PLATFORM=xcb

log_root="${XDG_RUNTIME_DIR:-/tmp}"
kwin_log="${log_root}/dosplex-kwin.log"
konsole_log="${log_root}/dosplex-konsole.log"
xterm_log="${log_root}/dosplex-xterm.log"
dolphin_log="${log_root}/dosplex-dolphin.log"

if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid '#12263f' || true
fi

kwin_pid=""
if command -v kwin_x11 >/dev/null 2>&1; then
  kwin_x11 --replace >"$kwin_log" 2>&1 &
  kwin_pid=$!
  for _ in $(seq 1 20); do
    if command -v wmctrl >/dev/null 2>&1 && wmctrl -m >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

main_pid=""
if command -v konsole >/dev/null 2>&1; then
  konsole --fullscreen --workdir "$HOME" >"$konsole_log" 2>&1 &
  main_pid=$!
elif command -v xterm >/dev/null 2>&1; then
  xterm -fa Monospace -fs 11 -fullscreen -e bash -lc 'exec bash -l' >"$xterm_log" 2>&1 &
  main_pid=$!
else
  bash -lc 'exec bash -l' &
  main_pid=$!
fi

if command -v dolphin >/dev/null 2>&1; then
  sleep 1
  dolphin "$HOME" >"$dolphin_log" 2>&1 &
fi

wait "$main_pid"
if [[ -n "$kwin_pid" ]]; then
  kill "$kwin_pid" >/dev/null 2>&1 || true
  wait "$kwin_pid" >/dev/null 2>&1 || true
fi
EOF
}

launch_side() {
  local side="$1"
  local user="$2"
  local nested_display="$3"
  local auth_file="$4"
  local host_x="$5"
  local title="$6"
  local runtime_dir="$7"
  local log_dir="$runtime_root/$side"
  local payload

  mkdir -p "$log_dir"
  payload="$(default_payload)"

  DISPLAY="$display" XAUTHORITY="$host_xauth" \
    Xephyr "$nested_display" \
    -screen "${half_width}x${nested_height}" \
    -title "$title" \
    -resizeable \
    -no-host-grab \
    -auth "$auth_file" \
    >"$log_dir/xephyr.log" 2>&1 &
  local xephyr_pid=$!

  for _ in $(seq 1 40); do
    if DISPLAY="$nested_display" XAUTHORITY="$auth_file" xdpyinfo >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  DISPLAY="$display" XAUTHORITY="$host_xauth" \
    wmctrl -r "$title" -b remove,maximized_vert,maximized_horz >/dev/null 2>&1 || true
  DISPLAY="$display" XAUTHORITY="$host_xauth" \
    wmctrl -r "$title" -e "0,${host_x},${screen_y},${half_width},${nested_height}" >/dev/null 2>&1 || true
  DISPLAY="$display" XAUTHORITY="$host_xauth" \
    wmctrl -r "$title" -b add,above >/dev/null 2>&1 || true

  sudo -H -u "$user" env \
    HOME="$(getent passwd "$user" | cut -d: -f6)" \
    USER="$user" \
    LOGNAME="$user" \
    SHELL="$(getent passwd "$user" | cut -d: -f7)" \
    DISPLAY="$nested_display" \
    XAUTHORITY="$auth_file" \
    XDG_RUNTIME_DIR="$runtime_dir" \
    XDG_SESSION_TYPE="x11" \
    dbus-run-session \
    bash -lc "$payload" \
    >"$log_dir/session.log" 2>&1 &
  local session_pid=$!

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
  kill "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
  kill "${LEFT_XEPHYR_PID:-}" "${RIGHT_XEPHYR_PID:-}" >/dev/null 2>&1
  wait "${LEFT_SESSION_PID:-}" "${RIGHT_SESSION_PID:-}" >/dev/null 2>&1
  wait "${LEFT_XEPHYR_PID:-}" "${RIGHT_XEPHYR_PID:-}" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

left_display="$(find_free_display 110)"
right_display="$(find_free_display $(( ${left_display#:} + 1 )))"
left_auth="$runtime_root/left.Xauthority"
right_auth="$runtime_root/right.Xauthority"

prepare_auth_file "$left_display" "$left_auth"
prepare_auth_file "$right_display" "$right_auth"

left_runtime="$(prepare_runtime_dir "$left_user")"
right_runtime="$(prepare_runtime_dir "$right_user")"

launch_side left "$left_user" "$left_display" "$left_auth" "$screen_x" "DosPlex Left" "$left_runtime"
launch_side right "$right_user" "$right_display" "$right_auth" "$((screen_x + half_width))" "DosPlex Right" "$right_runtime"

echo "DosPlex nested sessions running on $display"
echo "left=$left_user display=$left_display"
echo "right=$right_user display=$right_display"

wait "$LEFT_SESSION_PID" "$RIGHT_SESSION_PID"
