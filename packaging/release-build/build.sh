#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Builds the DosPlex release payload from a commit, in a pinned container. README.md describes what comes out.
#
#   packaging/release-build/build.sh --out DIR [--rev REV] [--version NAME] [--runtime docker|podman] [--no-cache]
#
#   --out DIR       where the payload is written; DIR must not exist or must be empty
#   --rev REV       the commit to build (default HEAD). Only committed files are built; uncommitted changes
#                   are never part of the payload.
#   --version NAME  the release name, letters, digits and . _ + - (default: <project version>-<first 9 of the commit>)
#   --runtime NAME  docker or podman (default: docker if installed, else podman; DOSPLEX_CONTAINER_RUNTIME also works)
#   --no-cache      rebuild the image from scratch instead of reusing its layers
#
# The image is built from the recipe in the commit (the one step besides the sources that needs the network);
# the payload build runs in it with no network at all and prints the payload as a tar stream.
set -euo pipefail

die() { echo "build.sh: $*" >&2; exit 1; }
# The help is the comment block that starts with "# Builds" (the license header above it is not part of it).
usage() { sed -n '/^# Builds the DosPlex release payload/,/^set -euo pipefail/{/^set -euo pipefail/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"; }

out='' rev=HEAD version='' runtime=${DOSPLEX_CONTAINER_RUNTIME:-}
nocache=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --out) out=${2:?--out needs a directory}; shift 2 ;;
        --rev) rev=${2:?--rev needs a revision}; shift 2 ;;
        --version) version=${2:?--version needs a name}; shift 2 ;;
        --runtime) runtime=${2:?--runtime needs docker or podman}; shift 2 ;;
        --no-cache) nocache=(--no-cache); shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done
[[ -n $out ]] || die "--out DIR is required (see --help)"
if [[ -n $version && ! $version =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]]; then
    die "the release name must be letters, digits and . _ + - (at most 64), starting with a letter or digit"
fi
if [[ -z $runtime ]]; then
    if command -v docker >/dev/null; then runtime=docker
    elif command -v podman >/dev/null; then runtime=podman
    else die "neither docker nor podman is installed"; fi
fi
command -v "$runtime" >/dev/null || die "$runtime is not installed"
if [[ -e $out && -n $(ls -A "$out" 2>/dev/null || echo x) ]]; then die "$out exists and is not empty"; fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
commit=$(git -C "$repo" rev-parse --verify --quiet "$rev^{commit}") || die "unknown revision: $rev"
epoch=$(git -C "$repo" show -s --format=%ct "$commit")
if [[ -n $(git -C "$repo" status --porcelain --untracked-files=no) ]]; then
    echo "build.sh: warning: the working tree has uncommitted changes; they are not part of this build" >&2
fi
echo "== building commit $commit (SOURCE_DATE_EPOCH=$epoch) with $runtime" >&2

tmp=$(mktemp -d "${TMPDIR:-/tmp}/dosplex-release-build.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# Everything below is taken from the commit, never from the working tree.
mkdir "$tmp/context"
git -C "$repo" archive --format=tar "$commit" packaging/release-build | tar -x -C "$tmp/context"
context=$tmp/context/packaging/release-build
tag=dosplex-release-build:$(cat "$context/Containerfile" "$context/sources.json" "$context/sources.py" \
    "$context/entrypoint.sh" | sha256sum | cut -c1-12)
echo "== image $tag" >&2
"$runtime" build "${nocache[@]}" --tag "$tag" --file "$context/Containerfile" "$context" >&2
echo "== image id $("$runtime" image inspect --format '{{.Id}}' "$tag")" >&2

git -C "$repo" archive --format=tar "$commit" -- compositor guest packaging legal tools/check-sbom.py \
    tools/generate-notices.py tools/build-manual.py docs/manual LICENSE NOTICE > "$tmp/source.tar"
environment=(-e "SOURCE_DATE_EPOCH=$epoch" -e "DOSPLEX_COMMIT=$commit")
if [[ -n $version ]]; then environment+=(-e "DOSPLEX_VERSION=$version"); fi
echo "== payload build (no network)" >&2
"$runtime" run --rm -i --network none "${environment[@]}" "$tag" < "$tmp/source.tar" > "$tmp/payload.tar"

mkdir -p "$out"
tar -x -p --no-same-owner -f "$tmp/payload.tar" -C "$out"
git -C "$repo" show "$commit:tools/check-sbom.py" > "$tmp/check-sbom.py"
git -C "$repo" show "$commit:tools/generate-notices.py" > "$tmp/generate-notices.py"   # check-sbom.py loads it
python3 "$tmp/check-sbom.py" "$out" >&2

echo "payload  $out"
echo "version  $(cat "$out/share/dosplex/VERSION")"
echo "commit   $commit"
bash "$here/payload-digest.sh" "$out" | tail -n 1
