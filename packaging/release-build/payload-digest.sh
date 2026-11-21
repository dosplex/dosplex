#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Prints one line per file of a payload directory, "<sha256> <mode> <path>" sorted by path, and then the payload
# digest: the SHA-256 of that listing. Two builds of the same commit print the same digest (README.md).
#
#   packaging/release-build/payload-digest.sh PAYLOAD_DIR
set -euo pipefail
dir=${1:?usage: payload-digest.sh PAYLOAD_DIR}
cd "$dir"
listing=$(find . -type f -print0 | LC_ALL=C sort -z | while IFS= read -r -d '' file; do
    printf '%s %s %s\n' "$(sha256sum < "$file" | cut -d' ' -f1)" "$(stat -c %a "$file")" "${file#./}"
done)
printf '%s\n' "$listing"
printf 'payload-digest %s\n' "$(printf '%s\n' "$listing" | sha256sum | cut -d' ' -f1)"
