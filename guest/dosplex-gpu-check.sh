#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

out_root="${1:-$HOME/dosplex-artifacts/gpu-check}"
mkdir -p "$out_root"
timestamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$out_root/$timestamp"
mkdir -p "$out_dir"

log() {
  printf '[dosplex-gpu-check] %s\n' "$*"
}

capture() {
  local name="$1"
  shift
  local cmd="$1"
  {
    printf '$ %s\n' "$*"
    "$@"
  } >"$out_dir/$name.txt" 2>&1 || true
}

log "writing results to $out_dir"

# Create a 'latest' symlink for predictable access
ln -sfn "$timestamp" "$out_root/latest"

capture env env
capture id id
capture uname uname -a
capture groups id
capture loginctl loginctl session-status
capture lspci lspci -nnk

glx_ok=0
if command -v glxinfo >/dev/null 2>&1; then
  if capture glxinfo glxinfo -B; then
    glx_ok=1
  fi
fi

dri3_ok=0
if [[ "$glx_ok" == "1" ]]; then
  if grep -qi "DRI3" "$out_dir/glxinfo.txt"; then
    dri3_ok=1
  fi
fi

if command -v vulkaninfo >/dev/null 2>&1; then
  capture vulkaninfo vulkaninfo --summary
fi

if command -v weston-info >/dev/null 2>&1; then
  capture weston-info weston-info
fi

if command -v wayland-info >/dev/null 2>&1; then
  capture wayland-info wayland-info
fi

summary_file="$out_dir/summary.txt"
{
  printf 'user=%s\n' "$(id -un)"
  printf 'display=%s\n' "${DISPLAY:-}"
  printf 'wayland_display=%s\n' "${WAYLAND_DISPLAY:-}"
  printf 'xdg_session_type=%s\n' "${XDG_SESSION_TYPE:-}"
  printf 'xdg_runtime_dir=%s\n' "${XDG_RUNTIME_DIR:-}"
  printf 'glx_connectivity=%s\n' "$glx_ok"
  printf 'dri3_support=%s\n' "$dri3_ok"
} >"$summary_file"

log "summary:"
cat "$summary_file"
log "done"
