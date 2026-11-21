#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
socket_name="dosplex-visible-0"
weston_log="$runtime_dir/dosplex-visible-weston.log"
visible_log="${HOME}/dosplex-artifacts/entry/visible-$(date +%Y%m%d-%H%M%S).log"
app_log="${HOME}/dosplex-artifacts/entry/app-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$runtime_dir"
mkdir -p "$(dirname "$visible_log")"

tty_path="$(tty || true)"

cleanup() {
  set +e
  if [[ -n "${app_pid:-}" ]]; then kill "$app_pid" 2>/dev/null; fi
  if [[ -n "${weston_pid:-}" ]]; then kill "$weston_pid" 2>/dev/null; fi
  wait "${app_pid:-}" "${weston_pid:-}" 2>/dev/null
}
trap cleanup EXIT INT TERM

weston_cmd=(
  weston
  --backend=drm
  --idle-time=0
  --socket="$socket_name"
  --log="$weston_log"
)

dbus-run-session bash -c '
  set -euo pipefail
  export XDG_RUNTIME_DIR="'"$runtime_dir"'"
  echo "[dosplex-vm-visible] runtime_dir='"$runtime_dir"' socket='"$socket_name"' tty='"$tty_path"'" >>"'"$visible_log"'"
  '"$(printf '%q ' "${weston_cmd[@]}")"' &
  weston_pid=$!
  for _ in $(seq 1 40); do
    if [[ -S "'"$runtime_dir"'/'"$socket_name"'" ]]; then
      break
    fi
    sleep 0.25
  done
  if [[ ! -S "'"$runtime_dir"'/'"$socket_name"'" ]]; then
    echo "[dosplex-vm-visible] weston socket did not appear" >>"'"$visible_log"'"
    if [[ -f "'"$weston_log"'" ]]; then
      tail -n 200 "'"$weston_log"'" >>"'"$visible_log"'" 2>/dev/null || true
    fi
    exit 1
  fi
  export WAYLAND_DISPLAY="'"$socket_name"'"
  export XDG_SESSION_TYPE=wayland
  export GDK_BACKEND=wayland
  export GSK_RENDERER=cairo
  export LIBGL_ALWAYS_SOFTWARE=1
  export NO_AT_BRIDGE=1
  export WAYLAND_DEBUG=1
  echo "[dosplex-vm-visible] waiting for weston drm backend..." >>"'"$visible_log"'"
  sleep 2
  echo "[dosplex-vm-visible] launching app" >>"'"$visible_log"'"
  python3 "'"$project_root"'/prototype/dosplex_vm_app.py" >>"'"$app_log"'" 2>&1 &
  app_pid=$!
  set +e
  wait "$app_pid"
  app_status=$?
  set -e
  echo "[dosplex-vm-visible] app_exit_status=$app_status" >>"'"$visible_log"'"
  if [[ -f "'"$weston_log"'" ]]; then
    echo "[dosplex-vm-visible] weston_tail_begin" >>"'"$visible_log"'"
    tail -n 200 "'"$weston_log"'" >>"'"$visible_log"'" 2>/dev/null || true
    echo "[dosplex-vm-visible] weston_tail_end" >>"'"$visible_log"'"
  fi
  exit "$app_status"
' 
