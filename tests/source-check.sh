#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# The portable gate: the published tests of the fast and the integration tiers. It is a thin wrapper around
# the test runner (tests/run.py), which builds the compositor once, runs the tests, prints a summary and exits
# non-zero for a failure and for INCOMPLETE (a required test that was skipped for a missing tool). The exported
# tree runs this same file. Extra arguments go to the runner, for example ./tests/source-check.sh --tier fast.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/run.py" --public --tier fast,integration "$@"
