#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${1:-}"
owner="${2:-}"

if [[ -z "$side" || -z "$owner" ]]; then
  echo "usage: $0 <left|right> <owner>" >&2
  exit 1
fi

workspace_root="${HOME}/dosplex-workspaces/${side}"
mkdir -p "$workspace_root"

exec weston-terminal --shell=/bin/bash --title="Dosplex ${side} shell (${owner})" -e \
  "cd '$workspace_root'; printf 'Dosplex %s side shell for %s\nworkspace=%s\n' '$side' '$owner' '$workspace_root'; exec bash -l"
