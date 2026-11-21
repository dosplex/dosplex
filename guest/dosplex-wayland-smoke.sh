#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

runtime_root="${XDG_RUNTIME_DIR:-/tmp}"
session_root="$runtime_root/dosplex-smoke"
socket_name="dosplex-smoke-$$"
log_file="$session_root/weston.log"
mkdir -p "$session_root"

cleanup() {
  set +e
  if [[ -n "${weston_pid:-}" ]]; then
    kill "$weston_pid" 2>/dev/null
    wait "$weston_pid" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

weston \
  --backend=headless \
  --socket="$socket_name" \
  --width=1920 \
  --height=1080 \
  --renderer=gl \
  >"$log_file" 2>&1 &
weston_pid=$!

for _ in $(seq 1 40); do
  if [[ -S "$runtime_root/$socket_name" ]]; then
    break
  fi
  sleep 0.25
done

export WAYLAND_DISPLAY="$socket_name"
export XDG_SESSION_TYPE=wayland

printf 'WAYLAND_DISPLAY=%s\n' "$WAYLAND_DISPLAY"
printf 'log=%s\n' "$log_file"

if command -v weston-info >/dev/null 2>&1; then
  weston-info | sed -n '1,120p'
fi

if command -v wayland-info >/dev/null 2>&1; then
  wayland-info | sed -n '1,120p'
fi

if command -v weston-simple-egl >/dev/null 2>&1; then
  timeout 5s weston-simple-egl >/tmp/dosplex-weston-simple-egl.out 2>&1 || true
  sed -n '1,120p' /tmp/dosplex-weston-simple-egl.out
fi

if command -v glxinfo >/dev/null 2>&1 && command -v Xwayland >/dev/null 2>&1; then
  Xwayland :99 >/tmp/dosplex-xwayland.log 2>&1 &
  xwayland_pid=$!
  sleep 2
  DISPLAY=:99 glxinfo -B | sed -n '1,80p' || true
  kill "$xwayland_pid" 2>/dev/null || true
  wait "$xwayland_pid" 2>/dev/null || true
fi
