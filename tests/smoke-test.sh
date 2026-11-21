#!/usr/bin/env bash
# dosplex-test: tier=integration needs=xephyr-tools manual="Legacy Xephyr prototype launcher, a dry run. It needs Xephyr, wmctrl and xauth, writes a fixed file in /tmp and covers only the prototype scripts, which are being retired. Run it by hand: bash tests/smoke-test.sh"
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

check_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing dependency: $1"
}

check_file() {
  [[ -f "$1" ]] || fail "missing file: $1"
}

check_cmd Xephyr
check_cmd wmctrl
check_cmd xauth
check_cmd bash

check_file "$project_root/scripts/dosplex-launch.sh"
check_file "$project_root/scripts/dosplex-login-pane.sh"
check_file "$project_root/scripts/dosplex-owned-session.sh"
check_file "$project_root/scripts/dosplex-session.sh"
check_file "$project_root/docs/architecture.md"

bash "$project_root/scripts/dosplex-launch.sh" --same-user-demo --dry-run >/tmp/dosplex-smoke.out

grep -q "Xephyr user=" /tmp/dosplex-smoke.out || fail "launcher dry-run did not describe Xephyr plan"
grep -q "Session command:" /tmp/dosplex-smoke.out || fail "launcher dry-run did not describe session command"

printf 'PASS: Dosplex smoke test completed\n'
