#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Entrypoint of the release build image. The repository export (a tar stream, see build.sh) arrives on stdin; it is
# unpacked and the build script from that export runs, so the payload is built by the scripts of the commit being
# built and not by whatever the image was made with. stdout carries the payload tar stream, stderr the log.
set -eu
rm -rf /build/src /build/work /build/deps /build/stage /build/payload
mkdir -p /build/src
tar -x -C /build/src --no-same-owner
exec bash /build/src/packaging/release-build/payload-build.sh "$@"
