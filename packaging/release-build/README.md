# Release payload build

One command builds the Dosplex release payload from a commit,
in a container that holds the SteamOS 3.8 library set, with no network access once the image exists:

```bash
packaging/release-build/build.sh --out /path/to/payload          # builds HEAD
packaging/release-build/build.sh --out /path/to/payload --rev COMMIT --no-cache
```

It needs git, python3, tar and docker (podman has the same commands and is meant to work; only docker has been
tried) and a network connection for the image build. Only committed files are built: the recipe and the sources
are exported with `git archive` from `--rev` (default `HEAD`), so uncommitted changes are never part of a payload
and the payload is a function of the commit. The script ends by running `tools/check-sbom.py` on the result and
prints the payload digest (see Reproducibility).

## What comes out

```
payload/
  bin/        dosplex-compositor (release build, RUNPATH=$ORIGIN/../lib) and the product scripts
  lib/        libwlroots.so.12 (0.17.4), libdisplay-info.so.2 (0.2.0), libliftoff.so.0 (0.5.0); RUNPATH=$ORIGIN
              libVkLayer_FROG_gamescope_wsi_x86_64.so and libVkLayer_FROG_gamescope_wsi_x86.so (32-bit), the Vulkan
              layers of Gamescope 3.16.30; no RUNPATH
  libexec/    the product scripts that live in libexec/
  share/      dosplex/VERSION, the release name, one line
              vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json and ...x86.json, the manifests of the layers
  licenses/   dosplex-LICENSE, dosplex-NOTICE, dosplex-THIRD-PARTY-NOTICES.md (generated from the SBOM), one license
              text per bundled library, the texts of the sources compiled into the layer (Gamescope, vkroots, glm),
              and wayland-protocols' COPYING, whose generated code is compiled into the compositor and the layer
  sbom/       dosplex-sbom.json
```

`bin/` and `libexec/` carry exactly the names the product files have always had in a bundle (`layout.txt` lists
them, and the bundle builder reads them from it), so `root-session-trial.py` and `update-retained-session.py` find
what they load. The payload holds regular files only (no symlinks): `session-bundle-integrity.py` refuses anything
else. Modes are 0755 for `bin/`,
`libexec/` and `lib/`, 0644 for the rest; every timestamp is the commit date.

`json-c` is not bundled. Both SteamOS releases ship it (0.18 in 3.8, 0.19 in 3.9, soname `libjson-c.so.5`
in both), so it is a host-provided library like the others. If that changes, add its tarball to `sources.json`.

## What is pinned

| Input | Pin |
| --- | --- |
| Base image | `docker.io/library/archlinux@sha256:5b1b7713182110aee0829fa9cc0571c451b391f0ad2bbafcf7a4ba907ee72ec3` (the official `base-20250727.0.390543`, digest of its manifest list; the linux/amd64 image is `sha256:a3820cc687d769deca35ee12a1f978c8639ef62b5b97cde59fb37fd875bc0550`) |
| Packages | Arch Linux Archive snapshot `https://archive.archlinux.org/repos/2025/07/30`, `pacman -Syyuu` to exactly that day, then the list in the `Containerfile` (207 packages with dependencies, 14 of them lib32 packages of the snapshot's multilib repository; glibc 2.41+r65, gcc 15.1.1, binutils 2.45, meson 1.8.2, wayland-protocols 1.45) |
| wlroots 0.17.4 | `wlroots-0.17.4.tar.gz`, SHA-256 `d3190d19d03446955e68a92c77d4c74af78384b0db39a85a0b1582adc80f36d1` |
| libdisplay-info 0.2.0 | `libdisplay-info-0.2.0.tar.xz`, SHA-256 `5a2f002a16f42dd3540c8846f80a90b8f4bdcd067a94b9d2087bc2feae974176` |
| libliftoff 0.5.0 | `libliftoff-0.5.0.tar.gz`, SHA-256 `e29af0c9dc765916c1f192f118efffc947af6600178b04d5bccca1901246f83b` |
| Gamescope 3.16.30 | `gamescope-3.16.30.tar.gz`, the GitHub archive of the tag `3.16.30` (commit `ad2763da1c48860f649abfe842a087188dcb6e20`), SHA-256 `e85b06cfa0fce1f7609bb3a691911029b1014bc6014f5d20dc9fbeac80a3d628` |
| vkroots `5106d8a` | `vkroots-5106d8a.tar.gz`, the GitHub archive of commit `5106d8a0df95de66cc58dc1ea37e69c99afc9540`, SHA-256 `37b77586e91f7ebee70380dcddd73bf01ae4acef1053e6be41d0485ede022422` |
| glm `0af55cc` | `glm-0af55cc.tar.gz`, the GitHub archive of commit `0af55ccecd98d4e5a8d1fad7de25ba429d60e863`, SHA-256 `e7f187d83523f505eb38dd25d297ea6c0d4ed856d733e808f18253f5a8fa88a0` |
| stb `5736b15` | `stb-5736b15.tar.gz`, the GitHub archive of commit `5736b15f7ea0ffb08dd38af21067c314d6a3aae9`, SHA-256 `d00921d49b06af62aa6bfb97c1b136bec661dd11dd4eecbcb0da1f6da7cedb4c` |

The first three tarballs are the upstream release assets (URLs in `sources.json`); the last four are GitHub archives, of
a tag and of three commits. `sources.py` downloads them while the image is built and refuses a file whose SHA-256
differs; the payload build checks again and runs with `--network none`. When they were pinned each one was also checked against its detached GPG signature
(`.sig` beside it) with Simon Ser's key `34FF9526CFEF0E97A340E2E40FDE7BE0E88F5E48`, fetched from
keys.openpgp.org into a throwaway keyring: wlroots signed 2024-06-27, libdisplay-info 2024-06-20, libliftoff
2024-05-27. Arch's PKGBUILDs for `wlroots0.17` 0.17.4-3 and `libdisplay-info` 0.2.0-2 list the same two SHA-256
sums; Arch builds libliftoff from a git tag, so there is no sum to compare. The build does not verify the
signatures itself; the hashes are the pin.

The Gamescope archive is the one GitHub makes of the tag `3.16.30`; the commit in its header is
`ad2763da1c48860f649abfe842a087188dcb6e20`, and its files equal a checkout of that tag. GitHub
leaves out the submodules, so Gamescope's own subprojects are pinned the same way, to the commits that the tag records
for them (`git ls-tree` of the tag, and `subprojects/glm.wrap` and `stb.wrap`): `sources.py` unpacks each of them into
the empty directory the archive has for it (`into`) and copies the wrap's `patch_directory` over glm and stb
(`overlay`), because meson does not apply a wrap's patches to a subproject that is already in place. Only what the
layer needs is pinned so far: vkroots and glm are compiled into it, stb is only looked up by the top-level
`meson.build`. wlroots, libdisplay-info, libliftoff, openvr, reshade and SPIRV-Headers belong to the Gamescope program
itself, which is not part of this payload. A GitHub archive is not signed and GitHub does not promise its bytes for
ever; the hash is the pin, and a changed archive fails the image build (it is not trusted silently).

Versions of the three libraries are the ones SteamOS 3.8's own repositories pair: `extra-3.8` ships `wlroots0.17` 0.17.4-3,
`libdisplay-info` 0.2.0-2 and `libliftoff` 0.5.0-1. wlroots 0.17.4 builds with libliftoff >= 0.4.0 (it has a code
path for 0.5). The previous Ubuntu bundle had libdisplay-info 0.1.1 and libliftoff 0.4.1. The devices ship
libdisplay-info 0.3.0 (`libdisplay-info.so.3`); the bundled 0.2.0 has another soname, so the two never meet. The
Arch package of wlroots 0.17.4 applies one patch, `Revert-layer-shell-error-on-0-dimension-without-anch.patch`. It
is not applied here: the compositor does not use layer-shell, and nothing unverified is vendored.

## Patches

A source can name patch files in `sources.json` (`patches`, paths relative to this directory, kept under
`patches/`). `sources.py unpack` applies them in order to the unpacked tree with GNU `patch -p1 --fuzz=0`, exactly
or not at all, and the manifest records each with its SHA-256 (`patches` on every record and `includes` entry of
that source). One is applied today:

- `patches/wlroots-0.17.4-x11-present-commit-seq.patch`: the X11 backend tags each present with the commit
  sequence its commit gets, as the headless and Wayland backends do, so that presentation feedback completes with
  the commit that presented the surface and not with the next one. Without it, a client that commits a frame only
  when the feedback of the one before has arrived runs a host frame behind under the X11 backend (shown under Xvfb
  by `tests/x11-presentation-test.py`), and the compositor has to commit every host frame for that feedback to
  arrive at all; with it, the compositor commits only when something changed and such a client gets a frame on
  every host frame. The development build and the tests use the same archive and patch through
  `compositor/subprojects/wlroots.wrap`.

wlroots is built with the same features as the Ubuntu build the hardware trials have used (DRM, libinput and X11
backends, GLES2 and Vulkan renderers, GBM, Xwayland support, libseat), with `xcb-errors` off. The result is that
the host libraries the payload needs are exactly the 28 sonames the previous bundle needed: none is new
(compared with `readelf -d` of the Ubuntu libwlroots and compositor).

## Why the Arch Linux Archive and not the SteamOS mirror

Bootstrapping pacman from Valve's mirror
(`https://steamdeck-packages.steamos.cloud/archlinux-mirror/`, `core-3.8` and `extra-3.8`) was tried. It is reachable and it has
what the build needs (glibc 2.41+r65, gcc 15.1.1, meson, even wlroots0.17). The decisive finding is that it serves the
same files as the Arch Linux Archive:

- `core-3.8` is identical to the archive's `core` of **2025-07-30**: 273 of 273 packages have the same version and the
  same SHA-256. (2025-07-31 also matches; 2025-07-29 differs in 8 packages.)
- `extra-3.8`: 14,465 of 14,508 packages are identical; the other 43 (vivaldi, vaultwarden, mold, xmonad, ...) are
  not in the image.
- `multilib-3.8` (the 32-bit libraries of the 32-bit Gamescope layer) is identical to the archive's `multilib` of
  2025-07-30: 289 of 289 packages.
- Every one of the 207 packages in the image is the same file, by version and SHA-256, in SteamOS 3.8.

`check-steamos-parity.py` reproduces this (about 20 MB of downloads); run it before moving the snapshot date.

So the archive snapshot is the SteamOS 3.8 package set, and it is better to pin: it is immutable and dated, and its
packages carry the Arch packagers' signatures. The mirror is a rolling repository without snapshots or a stated
retention policy (`core-3.8.db` is rewritten in place), so a build could only be pinned there by collecting
hashes ourselves. Its signatures are Valve's: glibc-2.41+r65 is signed in the mirror with key
`889B5EBDDD505A683621900DAF1D2199EF0A3CCF` on 2025-07-30, the same file in the archive with the Arch packager's
`05C7775A9E8B977407FE08E69D4C5AA15426DA0A` on 2025-07-24, and Valve does not document the mirror. A mirror
bootstrap would give the same bytes and cost the pin; it was not built.

Signatures in the image are still required: `SigLevel = Required DatabaseOptional TrustAll`. Without `TrustAll` pacman
rejects the snapshot ("unknown trust") because packager keys in that day's keyring have expired since: David Runge's
`139B09DA5BF0D338`, which signed `pambase`, expired on 2026-05-09. `TrustAll` waives the trust level of a key that is in
the keyring, not the signature, which must still verify.

Do not use the Steam Runtime SDK images: they are the game runtime's ABI, not the host's.

## The ABI floor

The supported floor is SteamOS 3.8 / glibc 2.41 (SteamOS 3.8 ships glibc 2.41, json-c 0.18 and libdisplay-info 0.3.0;
SteamOS 3.9 ships glibc 2.43, json-c 0.19 and libdisplay-info 0.3.0; neither ships wlroots, libliftoff or
wayland-protocols). `assemble.py` refuses a payload where any ELF file needs a
`GLIBC_` symbol version above 2.41 (read with `objdump -T`) and records the highest one in the SBOM
(`build.glibc_max_required`). Measured on a build: `bin/dosplex-compositor` GLIBC_2.38,
`lib/libwlroots.so.12` GLIBC_2.34, `lib/libdisplay-info.so.2` GLIBC_2.27, `lib/libliftoff.so.0` GLIBC_2.17,
`lib/libVkLayer_FROG_gamescope_wsi_x86_64.so` and `lib/libVkLayer_FROG_gamescope_wsi_x86.so` GLIBC_2.38 each.

C++ files have a second floor, the libstdc++ of SteamOS 3.8 (gcc-libs 15.1.1): `GLIBCXX_3.4.34`. The Gamescope 3.16.30
program of the Deck was built with a newer compiler and needs `GLIBCXX_3.4.35` (and `GLIBC_2.43`), so it does not load on
SteamOS 3.8; the same sources built in this image do. `assemble.py --glibcxx-floor 3.4.34` refuses a payload above it and
records `build.glibcxx_floor` and `build.glibcxx_max_required` in the SBOM. Both layers need `GLIBCXX_3.4.31`
and `CXXABI_1.3.9`.

`payload-build.sh` also checks that the compositor has `RUNPATH $ORIGIN/../lib` and no `RPATH`, that each private library
(`sources.py libraries`) has `RUNPATH $ORIGIN`, that `ldd` on the compositor (no `LD_LIBRARY_PATH`) reports nothing
missing and resolves the three private libraries from the payload's `lib/`, that each Gamescope layer has neither
`RUNPATH` nor `RPATH`, resolves with `ldd` and really opens (`open-layer.c`, built with `cc -m64` or `cc -m32` for the
layer it opens, `dlopen(RTLD_LAZY | RTLD_LOCAL)` as the Vulkan loader opens a layer) with the entry point
`vkNegotiateLoaderLayerInterfaceVersion`, and that no binary contains the build directory.

How the libraries are found: gcc lists `LIBRARY_PATH` among its own library directories and meson adds no rpath for
those, so setting it to the private prefix keeps the build directory out of every binary. `-Wl,--as-needed` keeps the
NEEDED list to what is used (the Arch compiler does not do that by default); `--build-id=none` leaves out the build id.

## Version stamping

`compositor/meson.build` generates `dosplex-version.h` (`DOSPLEX_VERSION`, `DOSPLEX_PROJECT_VERSION`,
`DOSPLEX_GIT_REVISION`) at configure time. The release build passes `-Drelease_version` (the name in `share/dosplex/VERSION`,
default `<meson project version>-<first 9 of the commit>`, e.g. `0.1.0-abcdef123`) and `-Dgit_revision`; a developer build
reads the revision with `git describe`, and a tree without git history says `unknown`. The compositor does not
include the header and has no `--version`, so no binary contains the version. The same name is in `share/dosplex/VERSION`, in the SBOM, and in the bundle manifest's `version`
(`prepare-session-bundle.py --payload` reads it from the file).

A binary cannot contain the SHA-256 of the manifest that covers it, so a manifest identity has to be read at run time:
from `<release dir>/manifest.json`, when that file is next to `bin/`.

## The Gamescope WSI layer

Games under the nested Gamescope present through its Vulkan layer, `VK_LAYER_FROG_gamescope_wsi_x86_64` (the bundled
copy is named `VK_LAYER_DOSPLEX_gamescope_wsi_x86_64`, see **Name** below). Gamescope
sets `ENABLE_GAMESCOPE_WSI=1` in its own environment (`steamcompmgr.cpp`), the manifest of the layer lists that
variable as `enable_environment`, and so the layer is active for Steam and for every game it starts. The layer of the
Gamescope that SteamOS 3.8 ships (3.16.23.6) does not deliver buffer releases for a retired swapchain, so a game that
switches to a fullscreen resolution equal to its monitor's waits for ever; the layer of 3.16.30 (SteamOS 3.9) has a
`PresentRetiredSwapchain` path and does not. On a Deck, the old Gamescope program with only the 3.16.30 layer
in front of it completes the 4K switch and the game keeps running.

Under pressure-vessel the old layer loads but does not take over. Its `isRunningUnderGamescope()` compares
`GAMESCOPE_WAYLAND_DISPLAY` and `WAYLAND_DISPLAY` as strings, and a container changes both:
`GAMESCOPE_WAYLAND_DISPLAY` becomes `/run/pressure-vessel/gamescope-socket` (`pv_wrap_add_gamescope_args` in
`pressure-vessel/wrap-setup.c`), and the side's `WAYLAND_DISPLAY=gamescope-0`, which the nested Gamescope sets because
it runs with `--expose-wayland` (`src/main.cpp`), becomes `wayland-0`, since it does not start with `wayland-`
(`flatpak_run_add_wayland_args` in `pressure-vessel/flatpak-run-wayland.c`). 3.16.30 compares the `st_dev` and `st_ino`
of the two sockets, which are one socket bound twice, and takes over. A layer that has not taken over still hooks
`vkDestroySwapchainKHR` and prints `[Gamescope WSI] Destroying swapchain` (3.16.23 `layer/VkLayer_FROG_gamescope_wsi.cpp`).
That line in a game's log shows the layer is loaded, not that it is active. The log of a game on SteamOS 3.8 ends
with it. So there is no contradiction between "the old layer never activates in a container" and "containerized games
on 3.8 load the 3.16.23.6 layer": they load it, it passes everything through, and the game presents through Mesa's X11
WSI path on Xwayland, the path that stalls on a swapchain re-create. All of this is read in
the sources. That log fits it; a backtrace of the stalled game (it waits in radv below the layer's hook) does
not tell the two paths apart.

What the release's layer changes on 3.8 is therefore "no active layer, X11 path" into "the 3.16.30 layer active,
Gamescope's WSI path". What the devices have shown so far:

- Deck, SteamOS 3.9, with the bundle: a game in the soldier container mapped the bundle's
  `libVkLayer_FROG_gamescope_wsi_x86_64.so`, and its `Player.log` shows `[Gamescope WSI] Creating Gamescope surface` and a
  swapchain that was destroyed and created again without a hang. So the bundled layer reaches a game in a Steam
  container and takes over there.
- SteamOS 3.8, Gamescope 3.16.23.6, no bundle: a game's `Player.log` shows the old layer loaded
  (`Forcing on VK_EXT_swapchain_maintenance1`) and never `Creating Gamescope surface`, so the game presented through
  X11 WSI. It hung after a swapchain re-create from 3840x1080 to 3840x2160.
- Deck, SteamOS 3.9, both layers off: the X11 path survived a resize.

So the hang on SteamOS 3.8 is in its own stack, and the active bundled layer avoids that path. That the hang is gone
with the bundle on SteamOS 3.8 is not shown.

**Build.** `payload-build.sh` configures the pinned tree with `-Denable_gamescope=false -Denable_gamescope_wsi_layer=true
-Denable_tests=false -Denable_openvr_support=false -Dpipewire=disabled -Dbenchmark=disabled`, a release build with
`NDEBUG`, the linker flags of the other binaries and `--wrap-mode=nodownload`; the image's packages are enough (libx11
is named in the `Containerfile`; `libxcb`, `wayland`, `vulkan-icd-loader` and `vulkan-headers` are installed already). No patch is applied. The layer is copied out of the build directory and not installed with meson,
because `meson install` would add Gamescope's default scripts. Measured: 221,800 bytes stripped; NEEDED `libxcb.so.1`,
`libX11-xcb.so.1`, `libwayland-client.so.0`, `libstdc++.so.6`, `libc.so.6`, `ld-linux-x86-64.so.2`, all of them host
libraries from the package set of SteamOS 3.8 (the preflight of a device is what confirms them there); no `RUNPATH`; GLIBC_2.38, GLIBCXX_3.4.31,
CXXABI_1.3.9; no build directory in the file.

**32-bit layer.** A 32-bit game loads a 32-bit layer, `VK_LAYER_FROG_gamescope_wsi_x86` (bundled as
`VK_LAYER_DOSPLEX_gamescope_wsi_x86`), and without one it would keep the layer of the installed Gamescope. The payload
ships it, so the launcher turns off the installed 32-bit layer together with the 64-bit one. The image
enables the `multilib` repository of the same Arch Linux Archive snapshot (`Containerfile`: a `[multilib]` section
appended to `pacman.conf`, which the base image does not have) and installs `lib32-glibc`, `lib32-gcc-libs`,
`lib32-libx11`, `lib32-libxcb`, `lib32-wayland` and `lib32-vulkan-icd-loader`, and with them `lib32-libxau`,
`lib32-libxdmcp`, `lib32-libffi`, `lib32-expat`, `lib32-icu`, `lib32-xz`, `lib32-zlib` and `lib32-libxml2`: 14 packages,
48 MiB to download, all of them the same files as SteamOS 3.8's `multilib-3.8` (`check-steamos-parity.py` compares
multilib as well). `payload-build.sh` configures the same tree again with the same options and the cross file
`cross-x86.txt` (`gcc -m32`, `cpu_family = 'x86'`, `pkg_config_libdir` in `/usr/lib32/pkgconfig`), so meson names the
result `libVkLayer_FROG_gamescope_wsi_x86.so` and its manifest `...x86.json`; `unset LIBRARY_PATH` keeps the private
64-bit libraries out of that link. Measured: 215,740 bytes stripped; NEEDED `libxcb.so.1`, `libX11-xcb.so.1`,
`libwayland-client.so.0`, `libstdc++.so.6`, `libc.so.6`, `ld-linux.so.2`; no `RUNPATH`; GLIBC_2.38, GLIBCXX_3.4.31,
CXXABI_1.3.9. These are other files than the 64-bit libraries of the same names (`/usr/lib32`, not `/usr/lib`), so the
SBOM records them as host-provided libraries with `abi: "i386"` (owner `lib32-glibc`, `lib32-libxcb`, ...),
`tools/check-sbom.py` reads 32-bit ELF files and matches NEEDED entries per ABI, and the notices mark the rows `(i386)`.
Tested on the payload with the 32-bit and the 64-bit loader of this package set and lavapipe (`vulkan-swrast`
and `lib32-vulkan-swrast` 25.1.6) in a throwaway container: a 32-bit program creates an instance with
`ENABLE_GAMESCOPE_WSI=1` and the loader inserts `VK_LAYER_FROG_gamescope_wsi_x86` (the manifest of the 64-bit layer is
passed over with "wrong ELF class: ELFCLASS64"); a 64-bit program inserts the 64-bit layer and passes over the 32-bit
one ("wrong ELF class: ELFCLASS32"); both processes exit 0; a 32-bit process loads no layer without the variable; a
copy of the payload reached through a `current` symlink works for the 32-bit layer too. The two manifests are in the
same directory, so the one `XDG_DATA_DIRS` entry of the launcher makes both visible.

**Manifest.** `assemble.py` makes `share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json` (and the
`...x86.json` of the 32-bit layer in the same way) from the
template of the tree (`layer/VkLayer_FROG_gamescope_wsi.json.in`) with `@family@` = `x86_64` and `library_path` =
`../../../lib/libVkLayer_FROG_gamescope_wsi_x86_64.so`, a path relative to the manifest, so the payload can be installed
anywhere (`/opt/dosplex/releases/<name>`, a trial directory) and the SBOM hashes stay those of the build. The Vulkan
loader reads a relative `library_path` that has a directory part relative to the directory of the manifest. Tested
with the loader of this package set (`vulkan-icd-loader` 1.4.321.0, which is also SteamOS 3.8's) and lavapipe
(`vulkan-swrast` 25.1.6) in a throwaway container, on the layer of the payload: `vulkaninfo` loads
`<dir>/share/vulkan/implicit_layer.d/../../../lib/libVkLayer_FROG_gamescope_wsi_x86_64.so`, inserts
`VK_LAYER_FROG_gamescope_wsi_x86_64` and exits 0 with `ENABLE_GAMESCOPE_WSI=1`; it does not load the layer without that
variable; a copy of the payload in another directory, reached through a `current` symlink and with a trailing slash in
`XDG_DATA_DIRS`, loads the same way; when two directories hold a manifest of the same name, the first one in
`XDG_DATA_DIRS` is used and the second is never opened (a second manifest with a library that does not exist is
ignored when it comes later and makes the layer fail to load when it comes first). The loader searches the
`XDG_DATA_DIRS` entries in order after `~/.config`, `/etc/xdg`, `/etc` and `~/.local/share`. steam-runtime-tools, which
pressure-vessel uses to carry host layers into the Steam runtime, resolves a relative `library_path` against the
directory of the manifest in the same way (`srt_base_json_graphics_module_resolve_library_path`, read in the source)
and writes a manifest with the in-container path; that part has not been run on a device.
`tools/check-sbom.py` fails a payload whose Vulkan manifest has an absolute path, a bare name, a path that leaves the
payload or one that leads to a file the payload does not have.

**Name.** `assemble.py` also gives each bundled layer a name and an off switch of its own (`layer_name` and
`disable_variable` in `sources.json`): `VK_LAYER_DOSPLEX_gamescope_wsi_x86_64` and `..._x86`, turned off by
`DOSPLEX_DISABLE_GAMESCOPE_WSI=1` instead of `DISABLE_GAMESCOPE_WSI=1`. Each replaces the one JSON string of the template
it stands for, so nothing else of the text changes; the enable variable stays `ENABLE_GAMESCOPE_WSI`. The library
itself is unchanged: the layer looks for its own name only in `vkEnumerateDeviceExtensionProperties`, and there as
`VK_LAYER_FROG_gamescope_wsi` without the family, which neither manifest uses.

Why a same name is not enough (read in the sources of steam-runtime-tools and Vulkan-Loader, main branches). For a
game in a Steam Linux Runtime container, pressure-vessel enumerates the layers of the side in the loader's order,
writes one manifest per layer and architecture into the single directory
`/usr/lib/pressure-vessel/overrides/share/vulkan/implicit_layer.d` named `<sequence>-<tuple>.json`
(`get_manifest_relative_to_overrides` in `pressure-vessel/runtime.c`; a library given by an absolute or relative path
keeps that path, with `/usr` read through `/run/host`), and points `VK_IMPLICIT_LAYER_PATH` and the front of
`XDG_DATA_DIRS` at it; it unsets `VK_ADD_IMPLICIT_LAYER_PATH` in the container. The loader reads that directory in
`readdir` order and does not sort it (`add_data_files` in `loader/loader.c`), and it keeps the first layer of a name
(`loader_remove_duplicate_layers`; older loaders skip the later one in `loader_add_implicit_layer`). The sequence
numbers follow `XDG_DATA_DIRS`, but `readdir` order on ext4 follows the hash of the name, not the number or the order of
writing. Measured (loader 1.3.275, lavapipe, ext4): the two manifests 07 and 11 come back
07 first whichever is written first; with the same name and a library of 07 that does not exist, no Gamescope layer is
loaded at all; with the Dosplex name, `DISABLE_GAMESCOPE_WSI=1` and both libraries present, only the bundled layer is
inserted, whichever is written first; with the Dosplex name and without `DISABLE_GAMESCOPE_WSI=1` both layers are
inserted and the process crashes (SIGSEGV), so the switch is required.

That order explains a symptom seen on a Deck: a game in the soldier container mapped the host's layer from
`/run/host/usr/lib`, through the manifest 11, while 07 pointed at the bundle. The container was not missing the bundle (see **Launcher**: pressure-vessel
exports the library itself). Two manifests had the same layer name, and the loader kept the one that `readdir` gave first.
ext4 returns the entries of a directory in hash order, and the hash seed belongs to the filesystem, so the order differs
from one machine to the next: the same payload can load the bundled layer on one and the host's on another. The fix is
the rename to `VK_LAYER_DOSPLEX_gamescope_wsi_*` together with `DISABLE_GAMESCOPE_WSI=1` for the host's layer, after which
the order does not matter.

**Launcher.** `guest/dosplex-user-side-session.sh` is part of a bundle when it lives in a directory called `libexec`
(`/opt/dosplex/releases/<name>/libexec`); the bundle is the directory above. When the bundle has both layers, each with
its manifest, and each manifest carries the Dosplex name and switch (and not `"DISABLE_GAMESCOPE_WSI"`), the launcher
exports, for the Steam payload (Game mode):

- `XDG_DATA_DIRS=<bundle>/share:<old XDG_DATA_DIRS>` (the old value, or `/usr/local/share:/usr/share` when it is unset
  or empty), so that the loader and pressure-vessel find the manifests;
- `DISABLE_GAMESCOPE_WSI=1`, which turns off the layers of the installed Gamescope (64-bit and 32-bit) by their own
  `disable_environment`, in and out of containers and whatever the order of the manifests;
- `PRESSURE_VESSEL_FILESYSTEMS_RO` with `<bundle>/lib` and `<bundle>/share` appended to what it held (empty entries
  and repeats left out). It is redundant and harmless, and the launcher keeps it. pressure-vessel already exports the
  captured layer library itself: for an implicit layer whose library is given by a path it puts a symlink to the library
  into its overrides directory (`bind_icds`, called for "Vulkan implicit layers" by `collect_json_based_drivers` in
  `pressure-vessel/runtime.c`), and `bind_runtime_finish` marks the target of every symlink there for export
  (`pv_export_symlink_targets`, `pressure-vessel/exports.c`). Real soldier and sniper
  containers run with the payload's manifests showed the bundle's library without this variable. And `/opt` is shared
  along with the home directory: `shared_like_home_absolute` in `pressure-vessel/wrap-home.c` (in main) lists
  `/opt` next to `/home`, `/srv`, `/media`, `/mnt`, `/run/media` and `/var/tmp`. What the variable does: pressure-vessel-wrap
  reads it from the environment Steam hands it and shares each path that exists read-only at the same path
  (`known_required_env` and `bind_and_propagate_from_environ` in `pressure-vessel/wrap-setup.c`; `pressure-vessel/wrap.1.md`
  and `docs/shared-paths.md` document it). The paths it never shares are listed in `pv_get_reserved_paths`
  (`pressure-vessel/utils.c`): `/run` itself and some of its children such as `/run/host` and `/run/pressure-vessel`,
  but not other directories below `/run`, so a trial bundle in `/run/dosplex-trial-<id>/bundle` can be shared. A symlink
  in a shared path is exported as a symlink and its target is exported as well (`_exports_path_expose` in
  `pressure-vessel/flatpak-exports.c`), so the launcher keeps `/opt/dosplex/current/lib` as it is. The functions named
  here were read in the sources of steam-runtime-tools main.

The launch log says `vulkan_layer=bundled (<x86_64 manifest>)`, then `PRESSURE_VESSEL_FILESYSTEMS_RO=...` and
`DISABLE_GAMESCOPE_WSI=1`. Outside a bundle (a checkout, an older install) the log says
`vulkan_layer=host (no bundled layer next to this launcher)`; in a bundle without one of the four files it says
`vulkan_layer=host (the bundle has no <path>)`; with a manifest that is not the Dosplex layer, or that still answers to
`DISABLE_GAMESCOPE_WSI`, it says `vulkan_layer=host (the bundled manifest <path> is not the Dosplex layer <name>)`. In
each of these cases nothing changes: turning off the host's layer then could leave a game with none.

Should a container not show the bundle's library after all (pressure-vessel's export of it is its own behavior, which
the launcher does not control), its games would have no Gamescope layer, because the host's is off. On SteamOS 3.8 that
changes nothing, because the 3.16.23.6 layer does not take over in a container (see above). On SteamOS 3.9 it would lose
the host's working 3.16.30 layer.

Three things for a player to know. The first and the third were measured with loader 1.3.275, lavapipe, the
bundled library and the manifests as `assemble.py` writes them; the second was read in the man pages of Flatpak 1.14.6
and tried with an installed Flatpak app. None was tried on a device.

- `DOSPLEX_DISABLE_GAMESCOPE_WSI=1` in a game's launch options turns the bundled layer off, but `DISABLE_GAMESCOPE_WSI=1`
  stays, so that game has no Gamescope layer at all and presents through Xwayland's X11 WSI path, the one that stalls
  on a swapchain re-create under 3.8. The loader turns a layer off when its disable variable is set to any value:
  `DISABLE_GAMESCOPE_WSI=` (empty) and `DISABLE_GAMESCOPE_WSI=0` still keep the host's layer off
  (`loader_implicit_layer_is_enabled` in `loader/loader.c` only checks that the variable exists). To give one game the
  host's layer instead, the launch options have to unset the variable:
  `env -u DISABLE_GAMESCOPE_WSI DOSPLEX_DISABLE_GAMESCOPE_WSI=1 %command%`. With that environment only
  `VK_LAYER_FROG_gamescope_wsi_x86_64` is inserted.
- `DISABLE_GAMESCOPE_WSI=1` reaches every process under the side's Steam, a Flatpak app that Steam starts included
  (Heroic, or an emulator added as a non-Steam game). Flatpak passes the variable on to the sandbox and resets
  `XDG_DATA_DIRS` (`flatpak-run(1)`: environment variables are generally passed on, and `XDG_DATA_DIRS` is one of those
  that Flatpak always unsets or overrides). An app that uses the `org.freedesktop.Platform.VulkanLayer.gamescope`
  extension then has its own layer turned off by the variable (the manifest of the layer in the tree this payload builds
  from lists it as the `disable_environment`; the extension's own manifest was not read), and cannot see the bundled
  one: it has no Gamescope layer. To give such an app its own layer back, unset the variable for that app, as the
  account of the side (`--user` writes the override into that account's Flatpak user installation):
  `flatpak override --user --unset-env=DISABLE_GAMESCOPE_WSI <app>` (`flatpak-override(1)`: `--user` and `--unset-env=VAR`).
  Tried with Flatpak 1.14.6 and an installed app, without the extension: with `DISABLE_GAMESCOPE_WSI=1`,
  `ENABLE_GAMESCOPE_WSI=1` and a bundle first in `XDG_DATA_DIRS` in the environment of `flatpak run`, the sandbox had
  `DISABLE_GAMESCOPE_WSI=1` and `XDG_DATA_DIRS=/app/share:/usr/share:/usr/share/runtime/share:/run/host/user-share:/run/host/share`;
  with the override (in a throwaway `FLATPAK_USER_DIR`) the variable was not set in the sandbox, and `ENABLE_GAMESCOPE_WSI`
  still was. That the extension's layer then loads in a game is not shown, and the flag spelling was not checked on the
  Flatpak of SteamOS.
- The bundled and the host's layer must never be active together. A process that sees the bundled manifest and
  `ENABLE_GAMESCOPE_WSI=1` but not `DISABLE_GAMESCOPE_WSI` (a launch option, a wrapper or a tool that clears or
  replaces the environment) gets both layers, and in the local run it crashed (`vulkaninfo`, SIGSEGV). Before the
  rename the shared name meant at most one of them loaded. A device check should read `DISABLE_GAMESCOPE_WSI=1` from
  the game's own `/proc/<pid>/environ`, not only from Steam's.

Symlinks in the path of the launcher are not resolved, and only builtins are used, so
`/opt/dosplex/current/libexec/...` gives `/opt/dosplex/current/share`.

Desktop mode does not get any of this. The desktop payload runs KWin and Plasma, not Gamescope, and the supervisor
starts the side with `env -i`, so `ENABLE_GAMESCOPE_WSI` is not set there and neither the host's nor the bundled layer
is active for a desktop game. Exporting the three variables for the whole Plasma session would change the environment
of every desktop program for no effect on games. A game that runs `gamescope` from its launch options in Desktop mode
gets the installed Gamescope and its own layer, as on a stock desktop.

`tests/session-launch-test.py` covers both sides and both roles, a symlinked and a relative launcher path with a
hostile `CDPATH`, an unset and an empty `XDG_DATA_DIRS`, the composition of `PRESSURE_VESSEL_FILESYSTEMS_RO`, the
cases without a bundle or with one of its files missing or not renamed (each with its log line), and that the
environment of Gamescope is otherwise the same; `tests/desktop-profile-test.py` checks that Desktop mode leaves the
three variables alone even next to a complete bundle.

**Not in the payload.** The Gamescope program itself.

## The SBOM

`sbom/dosplex-sbom.json`, one JSON object with sorted keys and no timestamps (so it is part of the reproducible output):

| Key | Content |
| --- | --- |
| `format`, `format_version` | `"dosplex-sbom"`, `1` |
| `package` | `name`, `version` (equal to `share/dosplex/VERSION`), `commit`, `source_date_epoch` |
| `build` | `base_image`, `package_snapshot`, `toolchain` (gcc, binutils, glibc, ... with versions), `environment_sha256` (of the image's `pacman -Q`), `glibc_floor`, `glibc_max_required`, and when the payload has C++ files `glibcxx_floor`, `glibcxx_max_required` |
| `files` | one record per payload file except the SBOM itself |
| `host_provided.libraries` | one record per NEEDED soname that is not bundled |
| `host_provided.python` | the interpreter and every third-party module the payload's Python files import |

A `files` record: `path`, `sha256`, `size`, `mode` (`"0755"`), `kind` (`executable`, `library`, `script`, `license`,
`data`), `name`, `version`, `origin`, `license` (SPDX expression), `license_files` (paths under `licenses/`), and the
source: for `origin: "upstream"` the `source_url` and the `source_sha256` of the tarball, for `origin: "repository"` the
`source_path`, for `origin: "distribution"` (license texts taken from a package of the image) the `package`. Optional
`includes` lists data or code of other packages compiled into the file (`name`, `version`, `license`, `license_files`,
`note`): `hwdata` in the two libraries that build its PNP ID table, `wayland-protocols` in the compositor and the
layer; code compiled from a source archive of `sources.json` (vkroots and glm in the layer) also has `source_url` and
`source_sha256`.

A `host_provided.libraries` record: `soname`, `needed_by` (payload paths), `provided_by: "host"`, and what the build
image had of it: `package`, `version`, `license`, `observed: "build image"`; `abi: "i386"` for the libraries that 32-bit
files need (`/usr/lib32`; a record without `abi` is the 64-bit library of that soname). The target preflight records what
the device has. Python entries have `version: null` because the image has no Python modules; the preflight fills them.

`tools/check-sbom.py PAYLOAD` fails when a payload file has no record or a record has no file, when a hash, size or
mode differs, when a record lacks a field, when a file in `lib/` has no license text, when a license text a record
names is missing from `licenses/` or empty, when a NEEDED entry is neither bundled nor listed, when a third-party
Python import is not listed, when `licenses/dosplex-THIRD-PARTY-NOTICES.md` lacks an entry for a bundled library, a piece
of compiled-in data or a host-provided library of the SBOM (or has one the SBOM does not support), or when the payload
holds anything but regular files, when an `includes` entry has a `source_url` without a `source_sha256` or the other way
round, or when a Vulkan manifest below `share/vulkan/` does not lead to a library of the payload by a path relative to
itself. `tests/sbom-check-test.py` runs it on fake payloads (51 cases); it is in `tests/pre-deploy.sh`.

## The third-party notices

`licenses/dosplex-THIRD-PARTY-NOTICES.md` is not copied from the repository. `assemble.py` generates it from the SBOM
with `tools/generate-notices.py` (the rights holder's name comes from `legal/RIGHTS_HOLDER`) and adds it to the payload
and to the SBOM like any other file. It lists every bundled component (name, version, files, license, license text,
source URL and the SHA-256 of the source archive), the data and code compiled into those files (hwdata,
wayland-protocols, and the snapshots vkroots and glm with their source archives) with its license as the SBOM states it, the host-provided libraries and Python modules as such, and a statement for libfuse
(LGPL-2.1) whenever the SBOM has it. A component that lacks a license, a license text or a source becomes a line marked
`OWNER DECISION REQUIRED`; today no component has one (the payload ships hwdata's license texts).
`generate-notices.py SBOM --strict` writes nothing while such a line is open, and `tools/release.py build` refuses
a bundle whose notices carry one, which makes it the gate for a public release; the payload build itself does not
use `--strict`.

```bash
python3 tools/generate-notices.py PAYLOAD/sbom/dosplex-sbom.json -o /tmp/notices.md     # what assemble.py writes
python3 tools/generate-notices.py PAYLOAD/sbom/dosplex-sbom.json --check PAYLOAD/licenses/dosplex-THIRD-PARTY-NOTICES.md
```

`check-sbom.py` runs the second form on every payload (the generator has to be next to it, so `build.sh` extracts both).
A payload built before the notices were generated fails that check; build it again. `THIRD-PARTY-NOTICES.md` in the
repository is the hand-written notice for the source tree. `tests/notices-test.py` covers the generator, the check and
the assembly with stand-ins for the image's tools.

## Using the payload

```bash
python3 packaging/release-build/make-bundle.py --payload /path/to/payload --output /tmp/candidate
```

The bundle builder takes the compositor, everything below `lib/`, `licenses/` and `share/` (the Gamescope layer and
its manifest among them; a payload without them is refused as older than the layer), the programs `helpers.txt` had
the payload compile, the SBOM and the version from the payload, and the product scripts `layout.txt` names. It
refuses a payload that does not agree with its SBOM, that was built from another commit than `HEAD` (in a tree
without git: than `--commit`), or whose scripts differ from the repository's. It writes `bundle/`, with a
`manifest.json` that lists every file with its digest, mode and size, and `manifest.sha256`: the digest a device is
given with `dosplex install BUNDLE --manifest`. The tested versions come from `tested-versions.json` beside the
builder. `--extra NAME=PATH` adds a file that is not part of the payload; the manifest's scope then says so.

## Reproducibility

`SOURCE_DATE_EPOCH` is the commit date; `LC_ALL=C`, `TZ=UTC`; the build directories have fixed names in the container;
`--build-id=none`; libraries and the compositor are stripped; modes and timestamps are normalised; the SBOM has no
time. `payload-digest.sh DIR` prints `<sha256> <mode> <path>` for every file and then the SHA-256 of that listing. Two
builds of the same commit must print the same digest.

## Known gaps

- **`<drm/drm_fourcc.h>`.** `compositor/dosplex-compositor.c:31` includes `<drm/drm_fourcc.h>`, which Debian and Ubuntu get
  from the kernel headers and Arch does not have. `payload-build.sh` gives the compiler a one-line header for it. The
  proper fix is `#include <drm_fourcc.h>` in the C file (`wlroots.pc` already passes `-I.../libdrm` on both a Debian-based host and
  the image, and the plain include compiles on both). Until the C file changes, the one-line header stays.
- **hwdata in the libraries.** `libwlroots` and `libdisplay-info` compile in the PNP ID table from `hwdata` 0.397-1,
  whose license the Arch package states as GPL-2.0-or-later (upstream also offers XFree86-1.1). The SBOM records it under
  `includes`. The payload ships both license texts (`licenses/hwdata-0.397-1-COPYING` and
  `-LICENSE`) and the notices name the source archive by address and SHA-256, so no open line is left for it.
- **Rights holder.** The name is written once, in `legal/RIGHTS_HOLDER`, which the notices generator reads; the
  headers of the sources, `NOTICE`, `AUTHORS.md` and `README.md` are written from and checked against it
  (`tools/apply-spdx-headers.py`). It holds "RoyÅke (Roy-Åke Martinsson)"; the payload
  ships `NOTICE` as it is.
- **Gamescope notices.** The `LICENSE` of the Gamescope tarball is the BSD-2-Clause text, followed by the BSD-3-Clause
  texts of two parts that are compiled into the program only (Tetrahedral 3D LUT code from OpenColorIO, the ReShade
  effects compiler); it is shipped whole as `licenses/gamescope-3.16.30-LICENSE`. The layer's own records say
  BSD-2-Clause. The protocol XML files in Gamescope's `protocol/` (some are copies of wlroots and wayland-protocols
  files with their own MIT notices) and `src/messagey.h` (Z-Lib, adapted from SDL; compiled into the layer for its error
  dialog) carry their own notices, which the SBOM does not list; the per-file audit has not been done. glm is offered
  under the Happy Bunny License or the MIT License; the notices record MIT.
- **The 32-bit layer on a device.** It has been run with a 32-bit loader and lavapipe in the image, not in a 32-bit
  game on a device.
- **wlroots notices.** The tarball's `LICENSE` is wlroots' own; the source also carries files with their own notices
  (the xcursor code, protocol XML). The SBOM names the tarball's `LICENSE` only; the per-file audit has not been done.
- **C helpers.** The image has gcc, pkgconf and fuse3, and `helpers.txt` is where a C program is listed to be built, stripped
  and recorded in the SBOM with its host libraries (tried with a throwaway fuse3 program, not kept). The CUSE relay still
  links a static libfuse and is not part of the payload. If it is added, libfuse appears under
  `host_provided.libraries` (`libfuse3.so.3`) and the generated notices carry the LGPL-2.1 statement for it; a libfuse
  that is compiled into a file instead is recorded with an `OWNER DECISION REQUIRED` line, because it needs a relink path.
- **Python.** The payload's scripts need the host's `python3` and `evdev`, `dbus`, `gi`, `PAM`/`pam` (SBOM
  `host_provided.python`).
- **`libei`.** The image has libei (the Containerfile names it; it is also an Xwayland dependency), so meson finds `libei-1.0` and the
  compositor is built with `-DHAVE_LIBEI=1`. It then has `libei.so.1` NEEDED; it is not bundled and appears under
  `host_provided.libraries` (SteamOS ships it). The compositor uses it to send the host pointer to a nested Gamescope through
  its EIS socket; without it the compositor builds and runs with Wayland pointer input only.
- **Compiler warnings.** `-Werror` is not enabled.
- **docker only.** `build.sh` uses only `build`, `run -i --network none`, `image inspect`; podman has not been tried.
- **Updating an installed trial bundle** with a container-built compositor makes the old Ubuntu `lib/` in that bundle the
  one found by `RUNPATH`; stage a fresh bundle first.

## Changing a pin

Edit the `ARG`s of the `Containerfile` (base image digest, snapshot date) or `sources.json` (url, SHA-256, directory), run
`check-steamos-parity.py --snapshot YYYY/MM/DD` for a new date, rebuild twice with `--no-cache`, and compare the digests. A new wlroots must still satisfy `dependency('wlroots', version: ['>=0.17.1', '<0.18'])`. A new Gamescope tag changes
the tag archive and the commits of its submodules (`git ls-tree TAG subprojects`, the two wraps, `src/reshade`,
`thirdparty/SPIRV-Headers`) together; `tests/release-payload-test.py` checks the shape of the entries and
`tests/release-sources-test.py` the unpacking.
