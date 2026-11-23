#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# Builds the DosPlex release payload. Runs inside the release-build image (see entrypoint.sh and README.md); do not
# run it on a workstation.
#
# Environment: SOURCE_DATE_EPOCH and DOSPLEX_COMMIT (the full commit id) are required, DOSPLEX_VERSION is optional.
# The repository export is in /build/src. stdout carries the payload as a tar stream, the log goes to stderr, and
# the container has no network.
set -euo pipefail
exec 3>&1 1>&2

die() { echo "payload-build: $*" >&2; exit 1; }

: "${SOURCE_DATE_EPOCH:?SOURCE_DATE_EPOCH is required}"
: "${DOSPLEX_COMMIT:?DOSPLEX_COMMIT is required}"
[[ $DOSPLEX_COMMIT =~ ^[0-9a-f]{40}$ ]] || die "DOSPLEX_COMMIT must be a full commit id"
[[ $SOURCE_DATE_EPOCH =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be a number"
export SOURCE_DATE_EPOCH LC_ALL=C TZ=UTC PYTHONDONTWRITEBYTECODE=1

# The newest glibc and libstdc++ symbol versions the payload may require: the oldest supported SteamOS (3.8) ships
# glibc 2.41 and gcc-libs 15.1.1 (GLIBCXX_3.4.34).
glibc_floor=2.41
glibcxx_floor=3.4.34

src=/build/src
recipe=$src/packaging/release-build
work=/build/work        # unpacked sources, meson build directories
deps=/build/deps        # the private libraries, installed here for the compositor to link against
stage=/build/stage      # `meson install --destdir` of the compositor; stage/gamescope/ holds what is built from Gamescope
payload=/build/payload
mkdir -p "$work/src" "$work/shim/drm" "$deps" "$stage" "$payload"
mapfile -t private_libraries < <(python3 "$src/packaging/release-build/sources.py" libraries)

project_version=$(sed -n "s/^[[:space:]]*version:[[:space:]]*'\([^']*\)'.*/\1/p" "$src/compositor/meson.build" | head -n 1)
[[ -n $project_version ]] || die "no project version in compositor/meson.build"
version=${DOSPLEX_VERSION:-$project_version-${DOSPLEX_COMMIT:0:9}}
[[ $version =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]] || die "invalid release name: $version"
echo "== DosPlex $version from commit $DOSPLEX_COMMIT, SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

echo "== sources"
python3 "$recipe/sources.py" unpack --from /opt/dosplex-release/sources --dest "$work/src"
sdir() { local found=("$work/src/$1"-[0-9]*); [[ -d ${found[0]} ]] || die "no source directory for $1"; echo "${found[0]}"; }

export PKG_CONFIG_PATH=$deps/lib/pkgconfig
# gcc lists LIBRARY_PATH among its own library directories, and meson adds no rpath for those. The private
# libraries are found at link time without a build rpath that would leave the build directory in the payload.
export LIBRARY_PATH=$deps/lib
# Link only what is used (the NEEDED list stays as small as the code needs), leave out the build id (it only
# restates the content) and write RUNPATH, not RPATH. The private libraries find each other in their own
# directory, the compositor finds them in ../lib.
common_ldflags='-Wl,--as-needed -Wl,--build-id=none -Wl,--enable-new-dtags'

build_library() {
    local name=$1 dir=$2
    shift 2
    echo "== $name"
    LDFLAGS="$common_ldflags "'-Wl,-rpath,$ORIGIN' \
        meson setup "$work/build/$name" "$dir" --prefix="$deps" --libdir=lib --buildtype=release \
        --wrap-mode=nodownload "$@"
    meson compile -C "$work/build/$name"
    meson install -C "$work/build/$name" --no-rebuild
}

build_library libdisplay-info "$(sdir libdisplay-info)"
build_library libliftoff "$(sdir libliftoff)"
# Features as in the Ubuntu build that the hardware trials have used so far, so the host libraries it needs are
# the same ones. xcb-errors is left out: it only improves X11 error messages and would need one more host library.
# The cursor themes are searched in the host's /usr/share/icons, not in the private prefix.
build_library wlroots "$(sdir wlroots)" -Dexamples=false -Dxcb-errors=disabled -Dxwayland=enabled \
    -Dbackends=drm,libinput,x11 -Drenderers=gles2,vulkan -Dallocators=gbm -Dsession=enabled \
    -Dicon_directory=/usr/share/icons

echo "== dosplex-compositor"
# The source includes <drm/drm_fourcc.h>. Debian and Ubuntu get that from the kernel headers; Arch only installs
# libdrm's copy, as <libdrm/drm_fourcc.h>. Give the compiler a header for the first form.
printf '#include <libdrm/drm_fourcc.h>\n' > "$work/shim/drm/drm_fourcc.h"
CFLAGS="-I$work/shim" LDFLAGS="$common_ldflags "'-Wl,-rpath,$ORIGIN/../lib' \
    meson setup "$work/build/compositor" "$src/compositor" --prefix=/ --buildtype=release -Db_ndebug=if-release \
    -Dlibei=enabled -Drelease_version="$version" -Dgit_revision="${DOSPLEX_COMMIT:0:9}" --wrap-mode=nodownload
meson compile -C "$work/build/compositor"
meson install -C "$work/build/compositor" --no-rebuild --strip --destdir "$stage"

echo "== gamescope WSI layer"
# Only the Vulkan layer of Gamescope is built (enable_gamescope=false): the layer that games load to present to the
# nested Gamescope. Its subprojects (vkroots, glm, stb) were placed in the tree by sources.py; nothing is downloaded.
gamescope_src=$(sdir gamescope)
LDFLAGS="$common_ldflags" \
    meson setup "$work/build/gamescope" "$gamescope_src" --prefix=/ --libdir=lib --buildtype=release \
    -Db_ndebug=if-release --wrap-mode=nodownload \
    -Denable_gamescope=false -Denable_gamescope_wsi_layer=true -Denable_tests=false -Denable_openvr_support=false \
    -Dpipewire=disabled -Dbenchmark=disabled
meson compile -C "$work/build/gamescope"
# Copied from the build directory, not installed: meson install would add Gamescope's default scripts, and the
# manifest of the layer is made by assemble.py with a path relative to itself.
install -D -m 0755 "$work/build/gamescope/layer/libVkLayer_FROG_gamescope_wsi_x86_64.so" \
    "$stage/gamescope/lib/libVkLayer_FROG_gamescope_wsi_x86_64.so"

echo "== gamescope WSI layer, 32-bit"
# The same tree and options once more for 32-bit games (a 32-bit game loads a 32-bit layer; meson names it
# ..._x86 and its manifest ...x86.json), cross-compiled with -m32 against the lib32 packages (cross-x86.txt).
# LIBRARY_PATH holds the private 64-bit libraries and has no place in a 32-bit link.
(
    unset LIBRARY_PATH
    LDFLAGS="$common_ldflags" \
        meson setup "$work/build/gamescope-x86" "$gamescope_src" --cross-file "$recipe/cross-x86.txt" --prefix=/ \
        --libdir=lib --buildtype=release -Db_ndebug=if-release --wrap-mode=nodownload \
        -Denable_gamescope=false -Denable_gamescope_wsi_layer=true -Denable_tests=false \
        -Denable_openvr_support=false -Dpipewire=disabled -Dbenchmark=disabled
    meson compile -C "$work/build/gamescope-x86"
)
install -D -m 0755 "$work/build/gamescope-x86/layer/libVkLayer_FROG_gamescope_wsi_x86.so" \
    "$stage/gamescope/lib/libVkLayer_FROG_gamescope_wsi_x86.so"

echo "== helpers"
# The C programs listed in helpers.txt (none yet), linked dynamically against the image's libraries.
mkdir -p "$stage/helpers"
while read -r destination source modules; do
    [[ -z ${destination:-} || $destination == '#'* ]] && continue
    [[ $destination == bin/* || $destination == libexec/* ]] || die "helpers.txt: $destination is not under bin/ or libexec/"
    echo "-- $destination from $source"
    flags=''
    if [[ -n ${modules:-} ]]; then flags=$(pkg-config --cflags --libs $modules); fi
    # shellcheck disable=SC2086  # $flags and $common_ldflags are lists of arguments
    cc -std=c11 -O2 -Wall -Wextra -o "$stage/helpers/${destination//\//__}" "$src/$source" $flags \
        $common_ldflags '-Wl,-rpath,$ORIGIN/../lib'
done < "$recipe/helpers.txt"

echo "== payload"
python3 "$recipe/assemble.py" --source "$src" --unpacked "$work/src" --deps "$deps" --stage "$stage" \
    --payload "$payload" --version "$version" --commit "$DOSPLEX_COMMIT" --glibc-floor "$glibc_floor" \
    --glibcxx-floor "$glibcxx_floor"

echo "== checks"
python3 "$src/tools/check-sbom.py" "$payload"
bin=$payload/bin/dosplex-compositor
readelf -d "$bin" | grep -E 'NEEDED|RUNPATH|RPATH'
runpath=$(readelf -d "$bin" | sed -n 's/.*(RUNPATH).*\[\(.*\)\]/\1/p')
[[ $runpath == '$ORIGIN/../lib' ]] || die "dosplex-compositor has RUNPATH '$runpath', expected \$ORIGIN/../lib"
for name in "${private_libraries[@]}"; do
    runpath=$(readelf -d "$payload/lib/$name" | sed -n 's/.*(RUNPATH).*\[\(.*\)\]/\1/p')
    [[ $runpath == '$ORIGIN' ]] || die "lib/$name has RUNPATH '$runpath', expected \$ORIGIN"
done
if readelf -d "$bin" "$payload"/lib/* | grep -q '(RPATH)'; then die "a payload file has DT_RPATH"; fi
# Nothing may carry the build directory: not as an rpath, not as a leftover string, not as a default search path.
if grep -a -l '/build/' "$bin" "$payload"/lib/*; then die "the build directory is embedded in a payload file"; fi
# No LD_LIBRARY_PATH here: everything must resolve, and the private libraries from the payload itself.
ldd "$bin"
if ldd "$bin" | grep -q 'not found'; then die "dosplex-compositor has unresolved libraries"; fi
for name in "${private_libraries[@]}"; do
    found=$(ldd "$bin" | sed -n "s/^[[:space:]]*$name => \([^ ]*\) .*/\1/p")
    [[ -n $found && $(realpath "$found") == "$payload/lib/$name" ]] || die "$name is not resolved from the payload's lib/ (got '$found')"
done
# The Gamescope layers need nothing private: their libraries come from the system, so they have no RUNPATH at all
# (the 32-bit one finds the lib32 libraries of the system as well). A game's Vulkan loader opens a layer by the path
# in its manifest, which is checked here by really opening each one, with a program of its own architecture.
layers=(libVkLayer_FROG_gamescope_wsi_x86_64.so libVkLayer_FROG_gamescope_wsi_x86.so)
for name in "${layers[@]}"; do
    readelf -d "$payload/lib/$name" | grep -E 'NEEDED|RUNPATH|RPATH'
    if readelf -d "$payload/lib/$name" | grep -qE '\((RUNPATH|RPATH)\)'; then die "lib/$name has an rpath or runpath"; fi
    ldd "$payload/lib/$name"
    if ldd "$payload/lib/$name" | grep -q 'not found'; then die "lib/$name has unresolved libraries"; fi
done
cc -m64 -O2 -Wall -o "$work/open-layer-64" "$recipe/open-layer.c"
cc -m32 -O2 -Wall -o "$work/open-layer-32" "$recipe/open-layer.c"
"$work/open-layer-64" "$payload/lib/${layers[0]}"
"$work/open-layer-32" "$payload/lib/${layers[1]}"
echo "highest GLIBC symbol version: $(grep -o '"glibc_max_required": "[0-9.]*"' "$payload/sbom/dosplex-sbom.json")"

echo "== tar"
tar --format=gnu --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH" \
    -C "$payload" -cf - . >&3
echo "== done: DosPlex $version"
