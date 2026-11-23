# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
project('dosplex-compositor', 'c',
    version: '0.1.0',
    license: 'AGPL-3.0-or-later',
    meson_version: '>=0.59.0',
    # Release by default (T-007). A debug build is still one option away:
    #   meson setup build compositor -Dbuildtype=debug
    default_options: ['buildtype=release', 'b_ndebug=if-release'],
)

math_dep = meson.get_compiler('c').find_library('m', required: true)

# The 0.17 series is the one this code is written and tested against. The release build bundles 0.17.4 with the
# release recipe's patches (the X11 backend's presentation feedback, which a nested KWin's frame pacing depends
# on). subprojects/wlroots.wrap is that same archive with those patches, for a development build:
#   -Dwlroots=subproject   build it here (the test runner does; the archive is fetched once into
#                          subprojects/packagecache/)
#   -Dwlroots=system       the wlroots pkg-config finds, which has to satisfy the version range; an unpatched
#                          0.17 answers presentation feedback one host commit late under the X11 backend
#   -Dwlroots=auto         the subproject when its archive is already in subprojects/packagecache/ and meson can
#                          apply its patches (0.63 or later), else system
wlroots_choice = get_option('wlroots')
fs = import('fs')
if wlroots_choice == 'auto'
    wlroots_choice = fs.is_file('subprojects/packagecache/wlroots-0.17.4.tar.gz') and \
        meson.version().version_compare('>=0.63.0') ? 'subproject' : 'system'
endif
if wlroots_choice == 'subproject'
    # The wrap's diff_files (the release's patches) is meson 0.63; an older meson would build it unpatched.
    if meson.version().version_compare('<0.63.0')
        error('-Dwlroots=subproject needs meson 0.63 or later to apply the wrap\'s patches; this is meson ' +
              meson.version() + '. Use -Dwlroots=system.')
    endif
    wlroots_dep = subproject('wlroots', default_options: [
        'examples=false', 'xcb-errors=disabled', 'xwayland=enabled', 'backends=drm,libinput,x11',
        'renderers=gles2,vulkan', 'allocators=gbm', 'session=enabled', 'werror=false', 'warning_level=1',
    ]).get_variable('wlroots')
    message('wlroots: the patched 0.17.4 subproject')
else
    wlroots_dep = dependency('wlroots', version: ['>=0.17.1', '<0.18'], required: true)
    # The release build links its own patched 0.17.4 this way, through pkg-config; a distribution's 0.17 is
    # unpatched, and its X11 backend answers presentation feedback one host commit late. The compositor detects that
    # at run time and commits every host frame (state.json: "frame_pacing": "every-frame-late-presents").
    warning('wlroots: the library pkg-config finds, ' + wlroots_dep.version() + '. Without the release ' +
            'build\'s X11 presentation patch a nested KWin runs a host frame behind under the X11 backend; ' +
            '-Dwlroots=subproject builds the patched 0.17.4.')
endif
wayland_server_dep = dependency('wayland-server', required: true)
wayland_client_dep = dependency('wayland-client', required: true)
xkbcommon_dep = dependency('xkbcommon', required: true)
# wlroots' X11 backend (how DosPlex runs as a client of SteamOS's Gamescope) keeps its connection to the display to
# itself; the compositor opens one of its own to bring the host pointer back to the middle of the output while a game
# holds a pointer lock. wlroots.pc already requires xcb, so a host that builds against wlroots has it.
xcb_dep = dependency('xcb', required: true)
# libei lets the compositor deliver a host pointer to a nested Gamescope through its EIS socket (T-423). Optional:
# without it the code compiles out and pointer input uses Wayland only.
libei_dep = dependency('libei-1.0', required: get_option('libei'))
cairo_dep = dependency('cairo', required: true)
# The scene's regions (a scaled side's opaque region, the damage the test hooks count) are pixman regions;
# wlroots requires pixman itself, so a host that builds against wlroots has it.
pixman_dep = dependency('pixman-1', required: true)
libinput_dep = dependency('libinput')
json_c_dep = dependency('json-c', version: '>=0.15', required: true)
wayland_scanner = find_program('wayland-scanner', required: true)

if libei_dep.found()
    add_project_arguments('-DHAVE_LIBEI=1', language: 'c')
endif

add_project_arguments('-DWLR_USE_UNSTABLE', language: 'c')
add_project_arguments('-D_XOPEN_SOURCE=700', language: 'c')
# Prefer headers generated from the installed protocol definitions.
protocol_includes = include_directories('.')

wayland_protos = dependency('wayland-protocols', version: '>=1.31')
wl_protocol_dir = wayland_protos.get_variable('pkgdatadir')

xdg_shell_server_header = custom_target(
    'xdg-shell-server-header',
    input: wl_protocol_dir + '/stable/xdg-shell/xdg-shell.xml',
    output: 'xdg-shell-protocol.h',
    command: [wayland_scanner, 'server-header', '@INPUT@', '@OUTPUT@'],
)

pointer_constraints_server_header = custom_target(
    'pointer-constraints-server-header',
    input: wl_protocol_dir + '/unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
    output: 'pointer-constraints-unstable-v1-protocol.h',
    command: [wayland_scanner, 'server-header', '@INPUT@', '@OUTPUT@'],
)

# Version stamp: dosplex-version.h defines DOSPLEX_VERSION (the release name), DOSPLEX_PROJECT_VERSION and
# DOSPLEX_GIT_REVISION. The C source can include it; nothing needs editing per release. Release builds pass
# -Drelease_version and -Dgit_revision (an exported tree has no .git); a developer build detects the revision
# when meson configures, so it is as stale as the configuration.
git_revision = get_option('git_revision')
if git_revision == ''
    git = find_program('git', required: false)
    if git.found()
        described = run_command(git, '-C', meson.project_source_root(), 'describe', '--always', '--dirty',
            '--abbrev=9', check: false)
        if described.returncode() == 0
            git_revision = described.stdout().strip()
        endif
    endif
endif
if git_revision == ''
    git_revision = 'unknown'
endif
release_version = get_option('release_version')
if release_version == ''
    release_version = meson.project_version() + '-' + git_revision
endif
version_data = configuration_data()
version_data.set_quoted('DOSPLEX_VERSION', release_version)
version_data.set_quoted('DOSPLEX_PROJECT_VERSION', meson.project_version())
version_data.set_quoted('DOSPLEX_GIT_REVISION', git_revision)
version_header = configure_file(output: 'dosplex-version.h', configuration: version_data)

# install: true lets the release build use `meson install --destdir`, which also drops the build-tree rpath
# that meson adds for libraries outside the system directories.
compositor = executable('dosplex-compositor',
    'dosplex-compositor.c',
    xdg_shell_server_header,
    pointer_constraints_server_header,
    version_header,
    include_directories: protocol_includes,
    dependencies: [wlroots_dep, wayland_server_dep, wayland_client_dep, xkbcommon_dep, xcb_dep, libei_dep, cairo_dep, pixman_dep, json_c_dep, math_dep, libinput_dep],
    install: true,
)
