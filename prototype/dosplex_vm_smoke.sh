#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${XDG_RUNTIME_DIR:-/tmp}"
socket_name="dosplex-proto-smoke-$$"
weston_log="$runtime_dir/dosplex-proto-weston.log"

cleanup() {
  set +e
  if [[ -n "${app_pid:-}" ]]; then kill "$app_pid" 2>/dev/null; fi
  if [[ -n "${weston_pid:-}" ]]; then kill "$weston_pid" 2>/dev/null; fi
  wait "${app_pid:-}" "${weston_pid:-}" 2>/dev/null
}
trap cleanup EXIT INT TERM

weston \
  --backend=headless \
  --socket="$socket_name" \
  --width=1920 \
  --height=1080 \
  >"$weston_log" 2>&1 &
weston_pid=$!

for _ in $(seq 1 40); do
  if [[ -S "$runtime_dir/$socket_name" ]]; then
    break
  fi
  sleep 0.25
done

export WAYLAND_DISPLAY="$socket_name"
export XDG_SESSION_TYPE=wayland
export GDK_BACKEND=wayland
export GSK_RENDERER=cairo
export LIBGL_ALWAYS_SOFTWARE=1

timeout 10s python3 "$project_root/prototype/dosplex_vm_app.py" --demo-timeout 3 &
app_pid=$!
wait "$app_pid" || {
  rc=$?
  if [[ "$rc" -ne 124 ]]; then
    exit "$rc"
  fi
}

printf 'SMOKE_OK\n'
