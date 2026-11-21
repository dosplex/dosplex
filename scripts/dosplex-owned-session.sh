#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${1:-}"
username="${2:-$(id -un)}"

case "$side" in
  left)
    bg_color="#123c73"
    title_prefix="Dosplex Left"
    ;;
  right)
    bg_color="#6b1f1f"
    title_prefix="Dosplex Right"
    ;;
  *)
    echo "invalid side: $side" >&2
    exit 1
    ;;
esac

if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid "$bg_color" || true
fi

if command -v xterm >/dev/null 2>&1; then
  exec xterm -T "${title_prefix} - ${username}" -fa Monospace -fs 11 -e \
    "printf 'Dosplex %s side is now owned by %s on %s\n' '$side' '$username' '$DISPLAY'; printf 'Exit this shell to return to the login screen.\n'; exec bash -l"
fi

if command -v glxgears >/dev/null 2>&1; then
  exec glxgears
fi

sleep 30
