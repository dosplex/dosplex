#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"

usage() {
  cat <<'EOF'
usage: dosplex-launch.sh [options]

Options:
  --left-user USER         Unix user for the left session
  --right-user USER        Unix user for the right session
  --left-command CMD       Command for the left side
  --right-command CMD      Command for the right side
  --output NAME            Target one connected X output instead of the full desktop
  --screen-width PX        Host screen width override
  --screen-height PX       Host screen height override
  --nested-height PX       Nested session height override
  --display DISPLAY        Host X display, defaults to $DISPLAY
  --same-user-demo         Skip greeters and launch demo payloads as the current user
  --dry-run                Print the planned actions only
  --help                   Show this help
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "missing dependency: $cmd" >&2
    exit 1
  fi
}

log() {
  printf '[dosplex] %s\n' "$*"
}

find_free_display() {
  local start="${1:-101}"
  local display_num

  for display_num in $(seq "$start" $((start + 50))); do
    if [[ ! -e "/tmp/.X${display_num}-lock" && ! -S "/tmp/.X11-unix/X${display_num}" ]]; then
      printf ':%s\n' "$display_num"
      return 0
    fi
  done

  echo "unable to find a free nested X display" >&2
  exit 1
}

screen_width=""
screen_height=""
nested_height=""
screen_x=""
screen_y=""
output_name=""
left_user=""
right_user=""
left_command=""
right_command=""
display="${DISPLAY:-:0}"
dry_run=0
same_user_demo=0

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
    --left-command)
      left_command="${2:?missing value for --left-command}"
      shift 2
      ;;
    --right-command)
      right_command="${2:?missing value for --right-command}"
      shift 2
      ;;
    --output)
      output_name="${2:?missing value for --output}"
      shift 2
      ;;
    --screen-width)
      screen_width="${2:?missing value for --screen-width}"
      shift 2
      ;;
    --screen-height)
      screen_height="${2:?missing value for --screen-height}"
      shift 2
      ;;
    --nested-height)
      nested_height="${2:?missing value for --nested-height}"
      shift 2
      ;;
    --display)
      display="${2:?missing value for --display}"
      shift 2
      ;;
    --same-user-demo)
      same_user_demo=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
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

require_cmd Xephyr
require_cmd wmctrl
require_cmd xauth
require_cmd mcookie
require_cmd xdpyinfo
require_cmd xrandr

host_user="$(id -un)"
if [[ "$same_user_demo" -eq 1 ]]; then
  left_user="$host_user"
  right_user="$host_user"
fi

left_user="${left_user:-$host_user}"
right_user="${right_user:-$host_user}"

set +e
if [[ -z "$output_name" ]]; then
  output_name="$(DISPLAY="$display" xrandr --current | awk '/ connected primary / {print $1; exit}')"
  if [[ -z "$output_name" ]]; then
    output_name="$(DISPLAY="$display" xrandr --current | awk '/ connected / {print $1; exit}')"
  fi
fi

if [[ -n "$output_name" ]]; then
  output_geometry="$(DISPLAY="$display" xrandr --current | awk -v out="$output_name" '
    $1 == out && $2 == "connected" {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+$/) {
          print $i
          exit
        }
      }
    }
  ')"
else
  output_geometry=""
fi
set -e

if [[ -n "$output_geometry" ]]; then
  parsed_output="$(printf '%s\n' "$output_geometry" | sed -E 's/^([0-9]+)x([0-9]+)\+([0-9]+)\+([0-9]+)$/\1 \2 \3 \4/')"
  if [[ -n "$parsed_output" ]]; then
    read -r output_width output_height output_x output_y <<<"$parsed_output"
    screen_width="${screen_width:-$output_width}"
    screen_height="${screen_height:-$output_height}"
    screen_x="${screen_x:-$output_x}"
    screen_y="${screen_y:-$output_y}"
  fi
fi

if [[ -z "$screen_width" || -z "$screen_height" ]]; then
  set +e
  dimensions="$(DISPLAY="$display" xdpyinfo 2>/dev/null | awk '/dimensions:/ {print $2; exit}')"
  xdpyinfo_rc=$?
  set -e
  if [[ "$xdpyinfo_rc" -eq 0 && -n "$dimensions" ]]; then
    screen_width="${screen_width:-${dimensions%x*}}"
    screen_height="${screen_height:-${dimensions#*x}}"
  fi
fi

screen_width="${screen_width:-1920}"
screen_height="${screen_height:-1080}"
nested_height="${nested_height:-$screen_height}"
screen_x="${screen_x:-0}"
screen_y="${screen_y:-0}"

if ! [[ "$screen_width" =~ ^[0-9]+$ && "$screen_height" =~ ^[0-9]+$ && "$nested_height" =~ ^[0-9]+$ ]]; then
  echo "screen dimensions must be integers" >&2
  exit 1
fi

half_width=$((screen_width / 2))
left_x="$screen_x"
right_x=$((screen_x + half_width))

runtime_dir="$(mktemp -d)"
chmod 0755 "$runtime_dir"
left_auth="$runtime_dir/left.Xauthority"
right_auth="$runtime_dir/right.Xauthority"
left_display="$(find_free_display 101)"
right_display="$(find_free_display $(( ${left_display#:} + 1 )))"
left_title="DosPlex Left"
right_title="DosPlex Right"

cleanup() {
  set +e
  if [[ -n "${left_pid:-}" ]]; then kill "$left_pid" 2>/dev/null; fi
  if [[ -n "${right_pid:-}" ]]; then kill "$right_pid" 2>/dev/null; fi
  wait "${left_pid:-}" "${right_pid:-}" 2>/dev/null
  rm -rf "$runtime_dir"
}
trap cleanup EXIT INT TERM

touch "$left_auth" "$right_auth"

cookie_left="$(mcookie)"
cookie_right="$(mcookie)"
xauth -f "$left_auth" add "${HOSTNAME}/unix${left_display}" MIT-MAGIC-COOKIE-1 "$cookie_left" >/dev/null
xauth -f "$right_auth" add "${HOSTNAME}/unix${right_display}" MIT-MAGIC-COOKIE-1 "$cookie_right" >/dev/null
xauth -f "$left_auth" add "${left_display}" MIT-MAGIC-COOKIE-1 "$cookie_left" >/dev/null
xauth -f "$right_auth" add "${right_display}" MIT-MAGIC-COOKIE-1 "$cookie_right" >/dev/null
chmod 0644 "$left_auth" "$right_auth"

if [[ "$same_user_demo" -eq 1 ]]; then
  left_command="${left_command:-$script_dir/dosplex-session.sh left}"
  right_command="${right_command:-$script_dir/dosplex-session.sh right}"
else
  left_command="${left_command:-$script_dir/dosplex-login-pane.sh left}"
  right_command="${right_command:-$script_dir/dosplex-login-pane.sh right}"
fi

launch_session() {
  local side="$1"
  local user="$2"
  local nested_display="$3"
  local auth_file="$4"
  local command_string="$5"
  local host_x="$6"
  local title="$7"

  local xephyr_cmd=(
    Xephyr
    "$nested_display"
    -screen "${half_width}x${nested_height}"
    -title "$title"
    -resizeable
    -no-host-grab
    -auth "$auth_file"
  )

  local session_cmd=(
    env
    DISPLAY="$nested_display"
    XAUTHORITY="$auth_file"
    dbus-run-session
    bash
    -lc
    "$command_string"
  )

  if [[ "$dry_run" -eq 1 ]]; then
    printf 'Xephyr user=%s display=%s x=%s width=%s height=%s title=%s\n' \
      "$user" "$nested_display" "$host_x" "$half_width" "$nested_height" "$title"
    printf 'Session command: %s\n' "$command_string"
    return 0
  fi

  DISPLAY="$display" "${xephyr_cmd[@]}" &
  local xephyr_pid=$!

  for _ in $(seq 1 40); do
    if DISPLAY="$display" wmctrl -l 2>/dev/null | grep -Fq "$title"; then
      break
    fi
    sleep 0.25
  done

  for _ in $(seq 1 40); do
    if DISPLAY="$nested_display" XAUTHORITY="$auth_file" xdpyinfo >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  DISPLAY="$display" wmctrl -r "$title" -b remove,maximized_vert,maximized_horz >/dev/null 2>&1 || true
  DISPLAY="$display" wmctrl -r "$title" -e "0,${host_x},${screen_y},${half_width},${nested_height}" >/dev/null 2>&1 || true

  if [[ "$user" == "$host_user" ]]; then
    "${session_cmd[@]}" &
  else
    require_cmd sudo
    sudo -u "$user" "${session_cmd[@]}" &
  fi

  if [[ "$side" == "left" ]]; then
    left_session_pid=$!
    left_pid="$xephyr_pid"
  else
    right_session_pid=$!
    right_pid="$xephyr_pid"
  fi
}

log "host display: $display"
log "target output: ${output_name:-full-desktop}"
log "geometry: ${screen_width}x${screen_height}+${screen_x}+${screen_y}"
log "left user: $left_user"
log "right user: $right_user"

launch_session left "$left_user" "$left_display" "$left_auth" "$left_command" "$left_x" "$left_title"
launch_session right "$right_user" "$right_display" "$right_auth" "$right_command" "$right_x" "$right_title"

if [[ "$dry_run" -eq 1 ]]; then
  exit 0
fi

log "nested sessions running; press Ctrl+C to stop"
wait "$left_session_pid" "$right_session_pid"
