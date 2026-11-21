#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail
# Submit an atomic request; the owning supervisor stops only its own unit.
exec python3 "$(dirname "${BASH_SOURCE[0]}")/dosplex-side-supervisor.py" request-mode "$@"
