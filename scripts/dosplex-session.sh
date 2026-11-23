#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${1:-}"
if [[ -z "$side" ]]; then
  echo "usage: $0 <left|right> [command ...]" >&2
  exit 1
fi
shift || true

case "$side" in
  left)
    bg_color="#1f3c88"
    title="DosPlex Left"
    ;;
  right)
    bg_color="#7a1f1f"
    title="DosPlex Right"
    ;;
  *)
    echo "invalid side: $side" >&2
    exit 1
    ;;
esac

if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid "$bg_color" || true
fi

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

if command -v glxgears >/dev/null 2>&1; then
  exec glxgears
fi

if command -v xterm >/dev/null 2>&1; then
  exec xterm -T "$title" -fa Monospace -fs 11 -e \
    "printf 'DosPlex %s session on %s\n' '$side' '$DISPLAY'; exec bash"
fi

if command -v chromium >/dev/null 2>&1; then
  profile_root="${XDG_RUNTIME_DIR:-/tmp}"
  profile_dir="${profile_root}/dosplex-chromium-${side}-$$"
  mkdir -p "$profile_dir"
  exec chromium --user-data-dir="$profile_dir" --new-window about:blank
fi

echo "No suitable session payload found" >&2
sleep 10
