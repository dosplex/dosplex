#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${1:-}"
owner="${2:-}"

if [[ ( "$side" != "left" && "$side" != "right" ) || -z "$owner" ]]; then
  echo "usage: $0 <left|right> <owner>" >&2
  exit 1
fi

home_dir="${HOME}"

# Dosplex was called DuplOS until October 2026. The first launch under the new name moves what the old name left in
# this account: the desktop profile (~/.local/share/duplos -> ~/.local/share/dosplex, with the desktop-scale
# preference and the profile's configuration) and the workspace (~/DuplOS -> ~/Dosplex). A directory moves only
# when the new name is free: nothing is merged and nothing is overwritten, a symlink is never followed, and a
# second launch finds nothing to do.
migrate_legacy_user_data() {
  local pair old new
  for pair in ".local/share/duplos:.local/share/dosplex" "DuplOS:Dosplex"; do
    old="$home_dir/${pair%%:*}"
    new="$home_dir/${pair##*:}"
    [[ -d "$old" && ! -L "$old" && -O "$old" ]] || continue
    if [[ -e "$new" || -L "$new" ]]; then
      echo "Dosplex: left $old where it is, because $new exists already" >&2
    elif mv -- "$old" "$new"; then
      echo "Dosplex: moved $old to $new" >&2
    else
      echo "Dosplex: could not move $old to $new" >&2
    fi
  done
  return 0
}
migrate_legacy_user_data

workspace_root="${home_dir}/Dosplex/${side}"
mkdir -p "$workspace_root"

# The supervisor gives each transient service its own runtime. Legacy/manual
# launches keep their existing runtime until they migrate to the supervisor.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${DOSPLEX_SESSION_RUNTIME:-0}" == "1" ]]; then
    [[ -d "$XDG_RUNTIME_DIR" && -O "$XDG_RUNTIME_DIR" ]] || {
        echo "Missing owned session runtime" >&2; exit 1;
    }
    if [[ "${DOSPLEX_PRIVATE_BUS:-0}" != "1" ]]; then
        unset DBUS_SESSION_BUS_ADDRESS
        exec dbus-run-session -- env DOSPLEX_PRIVATE_BUS=1 "$0" "$@"
    fi
    export XAUTHORITY="$XDG_RUNTIME_DIR/Xauthority"
    touch "$XAUTHORITY"
    export DOSPLEX_STEAMOS_SHIM_DIR="$XDG_RUNTIME_DIR/shims"
    mkdir -m 700 -p "$DOSPLEX_STEAMOS_SHIM_DIR"
    ln -sfn "${DOSPLEX_SHIM_SCRIPT:?}" "$DOSPLEX_STEAMOS_SHIM_DIR/steamos-session-select"
    manager="$(dirname "${DOSPLEX_SHIM_SCRIPT}")/dosplex-session-manager.py"
    /usr/bin/python3 "$manager" &
    manager_pid=$!
    for ((attempt=0; attempt<100; attempt++)); do
        [[ -f "$XDG_RUNTIME_DIR/dosplex-manager.ready" ]] && break
        kill -0 "$manager_pid" || { echo "Side session manager failed" >&2; exit 1; }
        sleep .02
    done
    [[ -f "$XDG_RUNTIME_DIR/dosplex-manager.ready" ]] || exit 1
fi

# Main talks to the shared Dosplex PipeWire server. Guest never does: it gets its
# own restricted endpoint (one output, one microphone) as a Unix socket, and
# without one it has no audio rather than the shared server.
if [[ "${DOSPLEX_SIDE_ROLE:-primary}" == "guest" ]]; then
    unset PULSE_SERVER
    case "${DOSPLEX_GUEST_AUDIO_SERVER:-}" in
        unix:/*) export PULSE_SERVER="$DOSPLEX_GUEST_AUDIO_SERVER" ;;
        *) echo "Dosplex: no Guest audio endpoint (DOSPLEX_GUEST_AUDIO_SERVER); Guest has no audio" >&2 ;;
    esac
    # Guest's endpoint calls its microphone after the bus, without ".monitor": a
    # name ending in ".monitor" is read as the monitor of a sink, i.e. Guest's own sound.
    export PULSE_SOURCE="dosplex_${side}_mic"
else
    export PULSE_SERVER="${DOSPLEX_AUDIO_SERVER:-127.0.0.1}"
    export PULSE_SOURCE="dosplex_${side}_mic.monitor"
fi
export PULSE_SINK="dosplex_${side}"
export PULSE_PROP="${PULSE_PROP:+$PULSE_PROP }dosplex.side=${side}"

# The standard Wayland socket name for Steam containers, unless a nested
# compositor wrapper already selected a side-specific socket.
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

# Robust SteamOS / nested-gamescope environment.
# The side supervisor creates a steamos-session-select shim.  Make sure the
# deck user sees that shim before the real SteamOS tool; otherwise Steam may
# try to control the host SteamOS session instead of staying inside Dosplex.
if [[ -n "${DOSPLEX_STEAMOS_SHIM_DIR:-}" && -d "${DOSPLEX_STEAMOS_SHIM_DIR}" ]]; then
    export PATH="${DOSPLEX_STEAMOS_SHIM_DIR}:$PATH"
fi
# X11, as SteamOS's own Game Mode session says (its display manager starts it as an X11 session): Steam's
# Performance Overlay (mangoapp) picks its window system from this, and with "wayland" it chose Wayland, could not
# open its X11 window and crashed in a loop (Steam Machine, 2026-11-18, twice a second). The Desktop payload sets
# wayland for its own session (start_desktop).
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=gamescope
export DESKTOP_SESSION=gamescope
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
export LANGUAGE=en_US:en

# Synchronization and Frame Pacing fixes for nested Wayland
export GAMESCOPE_WSI_PRESENT_WAIT=0
export GAMESCOPE_WSI_XWAYLAND_WAIT=0
# Two X servers in each side's Gamescope, as in SteamOS's own session (STEAM_MULTIPLE_XWAYLANDS=1 with
# --xwayland-count 2): Steam runs on the first and starts games on the second, and only then applies Maximum Game
# Resolution and a game's own resolution to the game's server (Deck, 2026-11-18: Valheim at 640x360 when its
# properties said so; with one server Steam left the game at the side's size).
export STEAM_MULTIPLE_XWAYLANDS=1
# Volume keys (a Steam Deck's own volume buttons, a keyboard's volume keys) are handled by Steam in SteamOS's own
# session, and that Steam is paused while Dosplex runs. Each side's Steam handles them for the keyboard it has.
export STEAM_ENABLE_VOLUME_HANDLER=1

# Suppress Gamescope WSI error dialogs to prevent blocking the game
export GAMESCOPE_ZENITY_DISABLE=1
export GAMESCOPE_WSI_HIDEDIALOG=1

# Preserve the caller's Vulkan layer policy. Gamescope scales the game's buffer;
# disabling Steam/WSI layers globally is not needed for fixed-size windows.

# Nested Gamescope hands its cursor image to the host pointer and draws
# nothing itself (cursor_composite defaults to "composite when not nested").
# Steam Input's trackpad mouse moves Gamescope's own cursor through XTest and
# libei, which the host pointer never follows, so games that use the hardware
# cursor showed no visible mouse at all. Gamescope reads ConVar overrides from
# "gamescope_<name>" environment variables; always composite the cursor.
export gamescope_cursor_composite=2

# Proton / Steam stability hints
export PROTON_USE_WAYLAND=0
unset SDL_VIDEODRIVER
unset GDK_BACKEND
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"

# Ensure X11 connectivity
export XAUTHORITY="${XAUTHORITY:-${home_dir}/.Xauthority}"
if [[ ! -f "$XAUTHORITY" ]]; then
    touch "$XAUTHORITY"
fi

payload="${DOSPLEX_SESSION_PAYLOAD:-terminal}"
desktop_mode="${DOSPLEX_DESKTOP_MODE:-nested}"

# This launcher is part of a release bundle (/opt/dosplex/releases/<name>/) when it lives in a directory called
# libexec: the bundle's root is the directory above it. Builtins only, because the PATH of a side can be short.
bundle_dir=""
launcher_dir="."
[[ "${BASH_SOURCE[0]}" == */* ]] && launcher_dir="${BASH_SOURCE[0]%/*}"
launcher_dir="${launcher_dir:-/}"
[[ "$launcher_dir" == /* ]] || launcher_dir="./$launcher_dir"   # never let CDPATH decide
if launcher_dir="$(cd -- "$launcher_dir" 2>/dev/null && pwd)" && [[ "${launcher_dir##*/}" == "libexec" ]]; then
  bundle_dir="${launcher_dir%/*}"
fi

vulkan_layer_source="host (no bundled layer next to this launcher)"
# The bundle carries the Vulkan layers of a newer Gamescope, 64-bit and 32-bit (lib/), with their manifests
# (share/vulkan/implicit_layer.d/): the layer of the Gamescope installed on SteamOS 3.8 never hears about a retired
# swapchain, and a game that re-creates its swapchain (a resize, a resolution or fullscreen switch) waits forever.
# The bundled manifests name the layers VK_LAYER_DOSPLEX_gamescope_wsi_<family> and turn them off with
# DOSPLEX_DISABLE_GAMESCOPE_WSI=1; Gamescope's ENABLE_GAMESCOPE_WSI=1 turns them on like the host's. Three changes
# to the environment make every game load them and not the host's:
#   - XDG_DATA_DIRS gets the bundle's share/ in front, so that the Vulkan loader and pressure-vessel find the
#     manifests;
#   - DISABLE_GAMESCOPE_WSI=1 turns off the host's layers, which keep their own names. A same name cannot be relied
#     on: in a Steam Linux Runtime container pressure-vessel copies every layer manifest into one directory, and the
#     loader takes the first one of a name in the order readdir gives, not in the order of XDG_DATA_DIRS;
#   - PRESSURE_VESSEL_FILESYSTEMS_RO gets the bundle's lib/ and share/ (after what it held, without repeats). That is
#     redundant and harmless, and it stays: pressure-vessel already exports the library of a captured layer itself
#     (bind_runtime_finish exports the targets of the symlinks it made in its overrides directory), so the bundle is
#     visible in a soldier or sniper container without it.
# Only when every bundled manifest has its library and is a Dosplex manifest; otherwise nothing is changed and games
# keep the host's layers. A launcher outside a bundle changes nothing either. Game mode only (launch_steam_session);
# launch_desktop_session says why.
# The off switch reaches further than games: DISABLE_GAMESCOPE_WSI=1 is in every process under Steam. Flatpak passes it
# on to an app and resets XDG_DATA_DIRS, so a Flatpak app that Steam starts (Heroic, an emulator added as a non-Steam
# game) and that uses the org.freedesktop.Platform.VulkanLayer.gamescope extension loses its own layer and cannot see
# the bundled one. The per-app override that unsets the variable gives it its own back:
#   flatpak override --user --unset-env=DISABLE_GAMESCOPE_WSI <app>

# Appends directories to the colon-separated list in the variable named $1 and exports it: what the list held stays
# first and in its order; empty entries and entries already there are left out. Builtins only.
append_unique_paths() {
  local name="$1" rest item seen=":" joined=""
  local -a items=()
  shift
  rest="${!name-}"
  while [[ -n "$rest" ]]; do
    item="${rest%%:*}"
    if [[ "$rest" == *:* ]]; then rest="${rest#*:}"; else rest=""; fi
    items+=("$item")
  done
  items+=("$@")
  for item in "${items[@]}"; do
    [[ -n "$item" && "$seen" != *":$item:"* ]] || continue
    seen+="$item:"
    joined+="${joined:+:}$item"
  done
  printf -v "$name" '%s' "$joined"
  export "${name?}"
}

use_bundled_vulkan_layer() {
  vulkan_layer_source="host (no bundled layer next to this launcher)"
  [[ -n "$bundle_dir" ]] || return 0
  # manifest|library|layer name, for each family the bundle carries.
  local -a layers=(
    "share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json|lib/libVkLayer_FROG_gamescope_wsi_x86_64.so|VK_LAYER_DOSPLEX_gamescope_wsi_x86_64"
    "share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86.json|lib/libVkLayer_FROG_gamescope_wsi_x86.so|VK_LAYER_DOSPLEX_gamescope_wsi_x86"
  )
  local entry rest manifest library name text first=""
  for entry in "${layers[@]}"; do
    manifest="$bundle_dir/${entry%%|*}"
    rest="${entry#*|}"
    library="$bundle_dir/${rest%%|*}"
    name="${rest#*|}"
    # A bundle without one of its four layer files keeps the host's layers; the log names the file.
    if [[ ! -f "$manifest" ]]; then
      vulkan_layer_source="host (the bundle has no $manifest)"
      return 0
    fi
    if [[ ! -f "$library" ]]; then
      vulkan_layer_source="host (the bundle has no $library)"
      return 0
    fi
    text=""
    IFS= read -r -d '' text < "$manifest" || true
    # DISABLE_GAMESCOPE_WSI=1 would turn off a layer that still answers to it, so a manifest that does (or that
    # does not carry the Dosplex name) leaves everything as it is.
    if [[ "$text" != *"\"$name\""* || "$text" != *'"DOSPLEX_DISABLE_GAMESCOPE_WSI"'* ||
          "$text" == *'"DISABLE_GAMESCOPE_WSI"'* ]]; then
      vulkan_layer_source="host (the bundled manifest $manifest is not the Dosplex layer $name)"
      return 0
    fi
    first="${first:-$manifest}"
  done
  export XDG_DATA_DIRS="$bundle_dir/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
  export DISABLE_GAMESCOPE_WSI=1
  append_unique_paths PRESSURE_VESSEL_FILESYSTEMS_RO "$bundle_dir/lib" "$bundle_dir/share"
  vulkan_layer_source="bundled ($first)"
}

launch_wayland_terminal() {
  if command -v konsole >/dev/null 2>&1; then
    local cmd=(konsole --platform wayland --nofork --separate --fullscreen --hide-menubar --hide-tabbar --notransparency --workdir "$workspace_root" -e bash -l)
    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]] && command -v dbus-run-session >/dev/null 2>&1; then
      exec dbus-run-session -- "${cmd[@]}"
    fi
    exec "${cmd[@]}"
  fi
}

launch_xterm_terminal() {
  if [[ -z "${DISPLAY:-}" ]]; then
    return 1
  fi
  local cmd=(xterm -fa Monospace -fs 14 -fg '#d7dde8' -bg '#0b1220' -fullscreen +maximized -xrm 'XTerm*scrollBar:false' -xrm 'XTerm*rightScrollBar:false' -xrm 'XTerm*toolBar:false' -xrm 'XTerm*internalBorder:0' -e bash -lc "cd '$workspace_root'; printf 'Dosplex ${side^^} ready for %s\n\n' '$owner'; printf 'Workspace: %s\n\n' '$workspace_root'; exec bash -l")
  if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]] && command -v dbus-run-session >/dev/null 2>&1; then
    exec dbus-run-session -- "${cmd[@]}"
  fi
  exec "${cmd[@]}"
}

# Gaming mode only. Prepares what Steam's Performance Overlay needs in this side, and returns non-zero (with the
# reason on stderr) when the side has to run without it. Sets perf_ipc_dir and perf_orig_cwd for the caller.
#
# How the numbers reach mangoapp (gamescope 3.16.x, src/mangoapp.cpp): Gamescope sends every frame as a System V
# message (msgsnd) on a queue found with ftok("mangoapp", 65), and mangoapp reads it from there. The path is
# relative, so the key comes from the "mangoapp" file in the working directory of each process (device and inode;
# with no such file the key is -1 for everyone). Two sides and the host would otherwise share one queue: one
# Gamescope would drain the other's messages (mangoapp_drop_stream, mangoapp_relay_control) and each mangoapp
# would show the other side's frames. This side therefore runs Gamescope, and with it the mangoapp that Gamescope
# spawns, in its own directory below the runtime directory (owned by this user, removed with the session) that
# holds an empty "mangoapp" file. The sides do not unshare IPC namespaces in production, so this is the isolation.
# Two such files meet in the key only when their inode numbers agree in the low 16 bits (docs/limitations.md).
#
# The overlay level is the file MANGOHUD_CONFIGFILE: it starts as "no_display" (nothing shown before Steam is up),
# Steam rewrites it when the player picks a level, and Gamescope watches it.
#
# Optional CSV logging (docs/user-guide.md): when the file <perf dir>/enabled exists, MANGOHUD_CONFIG adds logging
# keys. "read_cfg" keeps the file in use next to them, the way MangoHud and Gamescope read it
# (src/mangoapp_config.h), so Steam's rewrites still show and hide the overlay.
prepare_perf_overlay() {
  local runtime="${XDG_RUNTIME_DIR:-}" perf_dir
  if [[ "${DOSPLEX_PERF_OVERLAY:-1}" == "0" ]]; then
    return 1
  fi
  if ! command -v mangoapp >/dev/null 2>&1; then
    echo "Dosplex: mangoapp is not installed; the Performance Overlay is unavailable in this side" >&2
    return 1
  fi
  if [[ -z "$runtime" || ! -d "$runtime" || ! -O "$runtime" ]]; then
    echo "Dosplex: no runtime directory of this user; the Performance Overlay is unavailable in this side" >&2
    return 1
  fi
  perf_ipc_dir="$runtime/mangoapp-ipc"
  perf_orig_cwd="$PWD"
  if ! mkdir -p -m 700 "$perf_ipc_dir" || ! touch "$perf_ipc_dir/mangoapp" ||
      ! printf 'no_display' > "$runtime/mangohud.config"; then
    echo "Dosplex: cannot prepare the Performance Overlay files in $runtime" >&2
    return 1
  fi
  export MANGOHUD_CONFIGFILE="$runtime/mangohud.config"
  # mangoapp draws with GLFW on X11. Gamescope starts it with --expose-wayland's environment (WAYLAND_DISPLAY and a
  # Wayland session type), GLFW then chose Wayland, and mangoapp crashed in its X11 calls, twice a second for hours
  # (Steam Machine, 2026-11-18). SteamOS's Gamescope has no --expose-wayland. A wrapper first in PATH gives it the
  # X11 session it expects; nothing else that Gamescope starts is touched.
  local real_mangoapp
  real_mangoapp="$(command -v mangoapp)"
  if mkdir -p -m 700 "$runtime/mangoapp-bin" &&
      printf '#!/bin/sh\nunset WAYLAND_DISPLAY\nXDG_SESSION_TYPE=x11 exec %q "$@"\n' "$real_mangoapp" \
        > "$runtime/mangoapp-bin/mangoapp" && chmod 700 "$runtime/mangoapp-bin/mangoapp" 2>/dev/null; then
    export PATH="$runtime/mangoapp-bin:$PATH"
  else
    echo "Dosplex: the Performance Overlay starts without its X11 wrapper" >&2
  fi
  # The values Valve's gamescope-session exports; Gamescope sets the same for Steam when they are not set.
  export STEAM_MANGOAPP_PRESETS_SUPPORTED=1 STEAM_USE_MANGOAPP=1 STEAM_DISABLE_MANGOAPP_ATOM_WORKAROUND=1
  export STEAM_MANGOAPP_HORIZONTAL_SUPPORTED=1
  perf_dir="${DOSPLEX_PERF_DIR:-$home_dir/.local/share/dosplex/perf}"
  unset MANGOHUD_CONFIG
  if [[ -f "$perf_dir/enabled" ]]; then
    if [[ "$perf_dir" == *,* || "$perf_dir" == *$'\n'* ]] || ! mkdir -p -m 700 "$perf_dir"; then
      echo "Dosplex: performance logging is on but $perf_dir cannot be used; not logging" >&2
    else
      export MANGOHUD_CONFIG="read_cfg,output_folder=$perf_dir,autostart_log=1,log_interval=100"
    fi
  fi
  return 0
}

launch_steam_session() {
  local dependency
  for dependency in steam gamescope dbus-run-session; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
      echo "Cannot start Steam side: required command $dependency is unavailable" >&2
      return 1
    fi
  done
  use_bundled_vulkan_layer
  # Steam's Maximum Game Resolution starts at Native in a side, once per account (the helper says why).
  local resolution_default
  resolution_default="${BASH_SOURCE[0]%/*}/dosplex-steam-resolution-default.py"
  if [[ -f "$resolution_default" && -x /usr/bin/python3 ]]; then
    /usr/bin/python3 -I "$resolution_default" "$HOME/.local/share/Steam" "$HOME/.local/share/dosplex" || true
  fi
  if command -v steam >/dev/null 2>&1; then
    # Both sides start Steam with the flags of SteamOS's own launcher (/usr/lib/steamos/steam-launcher):
    # -steamos3 -steampal -steamdeck. Only these give Steam's SteamOS settings: Maximum Game Resolution and
    # each game's resolution, Bluetooth, Storage, Internet and System (shown on the Deck, 2026-11-18: with
    # -steampal alone there is no resolution setting and no Bluetooth page). Guest's Steam navigated with its
    # controller as before (the reason it once ran without host mode). What Guest must not change on the host
    # is closed on the system bus instead (native-pads.py bluez_policy: Bluetooth, updates, SteamOS Manager and
    # disks); network changes need the wheel group, which Guest does not have.
    # Account role follows ownership, independently of left/right placement.
    local cmd=(steam -gamepadui -nocrashdialog -nodumpreport -steamos3 -steampal -steamdeck)
    case "${DOSPLEX_SIDE_ROLE:-primary}" in
      # Main manages the device as on stock SteamOS: Steam may eject a card and adopt a new one as a library.
      primary) export STEAM_ALLOW_DRIVE_UNMOUNT=1 STEAM_ALLOW_DRIVE_ADOPT=1 ;;
      guest) ;;
      *) echo "Invalid Dosplex side role" >&2; return 2 ;;
    esac
    if command -v gamescope >/dev/null 2>&1; then
      local output_width="${DOSPLEX_OUTPUT_WIDTH:-1280}"
      local output_height="${DOSPLEX_OUTPUT_HEIGHT:-800}"
      local game_width="${DOSPLEX_GAME_WIDTH:-$output_width}"
      local game_height="${DOSPLEX_GAME_HEIGHT:-$output_height}"
      local scaler="${DOSPLEX_SCALE_MODE:-fit}"
      local dimension
      for dimension in "$output_width" "$output_height" "$game_width" "$game_height"; do
        if [[ ! "$dimension" =~ ^[1-9][0-9]{0,4}$ ]] || ((10#$dimension > 16384)); then
          echo "Invalid Dosplex display dimension: $dimension" >&2
          return 2
        fi
      done
      case "$scaler" in
        fit|stretch|zoom) ;;
        *) echo "Invalid Dosplex presentation mode: $scaler" >&2; return 2 ;;
      esac
      # Zoom is the compositor's crop of the picture; Gamescope itself keeps its Fit scaler.
      if [[ "$scaler" == zoom ]]; then scaler=fit; fi
      # Use a unique internal socket for gamescope to avoid lockfile conflicts.
      # Games see the nested dimensions. Gamescope scales that monitor into the
      # player's output region and maps input through the same transformation.
      export GAMESCOPE_WAYLAND_DISPLAY="wayland-gamescope-${side}"
      if [[ "${DOSPLEX_SESSION_RUNTIME:-0}" == "1" ]]; then
        # This unprivileged watcher belongs to the same systemd cgroup. It
        # only creates the compatibility alias inside this session's runtime.
        (
          for ((attempt=0; attempt<100; attempt++)); do
            for nested in "$GAMESCOPE_WAYLAND_DISPLAY" gamescope-0; do
              if [[ -S "$XDG_RUNTIME_DIR/$nested" ]]; then
                ln -snf "$nested" "$XDG_RUNTIME_DIR/wayland-0"
                exit 0
              fi
            done
            sleep 0.1
          done
          echo "Nested Wayland socket did not appear" >&2
        ) &
      fi
      # Live display changes never restart the side. A follower beside Steam applies the game
      # monitor size and the scaler to this Gamescope through its root properties, so it has to
      # be part of Gamescope's child command: that gives it Gamescope's DISPLAY and lets
      # gamescopereaper end it with the session. The child shell backgrounds the follower and
      # then execs Steam, so Steam stays the reaper's primary child (same PID) and its exit still
      # ends the session. The follower sits next to the launcher (libexec) like the other
      # helpers. It talks to the supervisor's control socket, so a launch without one has none.
      # The desktop payload runs its own nested compositor and never gets a follower.
      local follower=""
      if [[ -n "${DOSPLEX_SHIM_SCRIPT:-}" && -n "${DOSPLEX_SESSION_CONTROL:-}" && -n "${DOSPLEX_SESSION_INSTANCE:-}" ]]; then
        follower="$(dirname "$DOSPLEX_SHIM_SCRIPT")/dosplex-side-display-follower.py"
        if [[ ! -f "$follower" ]]; then
          echo "Dosplex: display follower missing ($follower); live display changes will not be applied" >&2
          follower=""
        fi
      fi
      if [[ -n "$follower" ]]; then
        cmd=(/bin/sh -c '/usr/bin/python3 "$1" & shift; exec "$@"' dosplex-display-follower "$follower" "${cmd[@]}")
      fi
      # Gamescope hides its cursor after 10 s without motion by default and then tells Steam the pointer sits
      # at the bottom right; --hide-cursor-delay 3000 keeps the host mouse visible while someone uses it.
      # Respect fixed-size game windows (e.g. Alien Breed's 1024x768 buffer).
      # Forcing their X window to the monitor size causes clipping/resize loops.
      # Games requesting fullscreen still receive the nested monitor dimensions.
      # Steam's Performance Overlay (Quick Access Menu > Performance) is drawn by mangoapp. Gamescope starts it
      # itself when it gets --mangoapp, in its own watchdog, so it lives and ends with this side's Gamescope and
      # attaches to this side's DISPLAY only. prepare_perf_overlay explains the config file, the queue isolation
      # and the opt-in CSV logging. It is Gaming mode only: the desktop payload never reaches this function.
      local mangoapp_args=()
      if prepare_perf_overlay; then
        mangoapp_args=(--mangoapp)
        # Gamescope keeps this directory as its working directory (the queue key depends on it); Steam and the
        # games it starts get the directory the launcher had, as before.
        cmd=(/bin/sh -c 'cd "$1" 2>/dev/null; shift; exec "$@"' dosplex-restore-cwd "$perf_orig_cwd" "${cmd[@]}")
        cd "$perf_ipc_dir"
      fi
      cmd=(gamescope -e --backend wayland --output-width "$output_width" --output-height "$output_height" --nested-width "$game_width" --nested-height "$game_height" --scaler "$scaler" --xwayland-count 2 --expose-wayland --borderless --hide-cursor-delay 3000 --force-composition "${mangoapp_args[@]}" -- "${cmd[@]}")
    fi
    # Use dbus-run-session to ensure a private bus for this user side.
    # Log the exact launch environment because deck-vs-guest differences here
    # are the current suspected failure layer.
    {
      echo "[$(date -Is)] side=$side owner=$owner"
      echo "PATH=$PATH"
      echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
      echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
      echo "GAMESCOPE_WAYLAND_DISPLAY=${GAMESCOPE_WAYLAND_DISPLAY:-}"
      echo "PULSE_SERVER=${PULSE_SERVER:-}"
      echo "XDG_DATA_DIRS=${XDG_DATA_DIRS:-}"
      echo "vulkan_layer=$vulkan_layer_source"
      echo "PRESSURE_VESSEL_FILESYSTEMS_RO=${PRESSURE_VESSEL_FILESYSTEMS_RO:-}"
      echo "DISABLE_GAMESCOPE_WSI=${DISABLE_GAMESCOPE_WSI:-}"
      echo "MANGOHUD_CONFIGFILE=${MANGOHUD_CONFIGFILE:-}"
      echo "MANGOHUD_CONFIG=${MANGOHUD_CONFIG:-}"
      echo "XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}"
      echo "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-}"
      echo "cmd=${cmd[*]}"
    } >> "${DOSPLEX_LAUNCH_LOG:-${workspace_root}/launch.log}" 2>&1 || true

    if command -v dbus-run-session >/dev/null 2>&1; then
      exec dbus-run-session -- "${cmd[@]}"
    fi
    exec "${cmd[@]}"
  fi
}

# Set one key of a KDE settings file and leave the rest of the file as the player has it: with KDE's own
# writer when this host has one, else with the small merge of the desktop helper.
set_desktop_setting() {
  local file="$1" group="$2" key="$3" value="$4"
  if command -v kwriteconfig6 >/dev/null 2>&1 &&
      kwriteconfig6 --file "$file" --group "$group" --key "$key" "$value"; then
    return 0
  fi
  /usr/bin/python3 "$desktop_helper" set-key "$file" "$group" "$key" "$value"
}

# A side's Desktop mode keeps its own profile from one launch to the next, so that what the player changes
# in it (panel, theme, the settings of its programs) is still there next time. The profile is a directory
# of this user below the user's home, never the real ~/.config, which the host's own Plasma may be using.
# A trial or retained experiment names a root of its own in DOSPLEX_DESKTOP_CONFIG_ROOT instead.
# Sets desktop_helper, which set_desktop_setting and the launch use, and desktop_scale.
prepare_desktop_profile() {
  local desktop_root dir entry scale_answer
  desktop_helper="$(dirname "${DOSPLEX_SHIM_SCRIPT:?}")/dosplex-desktop-environment.py"
  [[ -f "$desktop_helper" ]] || { echo "Dosplex: desktop helper missing ($desktop_helper)" >&2; return 1; }
  desktop_root="${DOSPLEX_DESKTOP_CONFIG_ROOT:-$home_dir/.local/share/dosplex/desktop}"
  [[ "$desktop_root" == /* ]] || {
    echo "Dosplex: the desktop configuration root must be an absolute path: $desktop_root" >&2; return 1;
  }
  mkdir -p "$(dirname "$desktop_root")"
  mkdir -p -m 700 "$desktop_root" "$desktop_root/config" "$desktop_root/data"
  for dir in "$desktop_root" "$desktop_root/config" "$desktop_root/data"; do
    [[ -d "$dir" && -O "$dir" ]] || { echo "Dosplex: $dir is not a directory of this user" >&2; return 1; }
    chmod 700 "$dir"
  done
  # Only the defaults are fresh at every launch: XDG_CONFIG_DIRS holds the desktop essentials and nothing else.
  export XDG_CONFIG_HOME="$desktop_root/config"
  export XDG_DATA_HOME="$desktop_root/data"
  export XDG_CONFIG_DIRS="$XDG_RUNTIME_DIR/desktop-defaults"
  mkdir -p "$XDG_CONFIG_DIRS/autostart"
  ln -sfn /etc/xdg/menus "$XDG_CONFIG_DIRS/menus"
  export XDG_MENU_PREFIX=plasma-
  # Desktop essentials only: host power/indexing/setup autostarts must not
  # change shared machine settings or the normal desktop profile.
  for entry in org.kde.plasmashell.desktop kglobalacceld.desktop kaccess.desktop gmenudbusmenuproxy.desktop xembedsniproxy.desktop; do
    if [[ -f "/etc/xdg/autostart/$entry" ]]; then
      cp "/etc/xdg/autostart/$entry" "$XDG_CONFIG_DIRS/autostart/"
    fi
  done
  mkdir -p "$XDG_CONFIG_HOME/autostart" "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME/Desktop" "$XDG_RUNTIME_DIR/desktop-bin"
  # What the launcher needs in the settings is set key by key: the player's other settings stay. Plasma
  # starts without systemd, inside this side's private bus/cgroup rather than the host user manager.
  set_desktop_setting "$XDG_CONFIG_HOME/startkderc" General systemdBoot false
  # Steam Input's controller mouse and keyboard reach the nested desktop as X
  # test input, which KWin routes through its input-emulation permission and
  # answers with a "Remote Control" prompt nobody can click without a pointer.
  # This desktop is private to one player, so accept emulated input without
  # the prompt (KWin's XwaylandEisNoPrompt, Plasma 6).
  set_desktop_setting "$XDG_CONFIG_HOME/kwinrc" Xwayland XwaylandEisNoPrompt true
  # Steam's on-screen keyboard (Steam+X) is sized for a landscape screen and pinned by size hints, so it is far
  # wider than a portrait side. A rule frees its size and a KWin script fits it to the width of the side at the
  # bottom, whatever the scale and layout are at the moment; the helper explains why it is a script.
  /usr/bin/python3 "$desktop_helper" osk-rule "$XDG_CONFIG_HOME/kwinrulesrc"
  /usr/bin/python3 "$desktop_helper" osk-script "$XDG_DATA_HOME/kwin/scripts"
  set_desktop_setting "$XDG_CONFIG_HOME/kwinrc" Plugins dosplex-oskEnabled true
  /usr/bin/python3 "$desktop_helper" set-var "$XDG_CONFIG_HOME/user-dirs.dirs" XDG_DESKTOP_DIR "$XDG_DATA_HOME/Desktop"
  # The entries below are Dosplex's own and written again at every launch. Their copies under the old name
  # (DuplOS) came along with the moved desktop profile; left in place, the old Steam autostart starts a second
  # Steam beside the new one and neither gets the side's controller.
  rm -f "$XDG_CONFIG_HOME/autostart/duplos-steam.desktop" "$XDG_DATA_HOME/applications/duplos-return.desktop" \
    "$XDG_DATA_HOME/Desktop/duplos-return.desktop"
  cat > "$XDG_CONFIG_HOME/autostart/dosplex-steam.desktop" <<'STEAM'
[Desktop Entry]
Type=Application
Name=Steam
Exec=steam -silent -nocrashdialog -nodumpreport
STEAM
  cat > "$XDG_DATA_HOME/applications/dosplex-return.desktop" <<'RETURN'
[Desktop Entry]
Type=Application
Name=Return to Gaming Mode
Icon=steam
Exec=steamosctl switch-to-game-mode
Terminal=false
RETURN
  cp "$XDG_DATA_HOME/applications/dosplex-return.desktop" "$XDG_DATA_HOME/Desktop/dosplex-return.desktop"
  chmod 700 "$XDG_DATA_HOME/Desktop/dosplex-return.desktop"
  # A nested KWin ignores a scale set through KDE's display configuration (System Settings, kscreen-doctor);
  # only its --scale argument sets the scale of its output. The scale comes from the size of this side, or
  # from the player's desktop-scale file, and is the same in both places that start KWin. The helper answers
  # "SCALE REASON"; scale 1 is the fallback for an answer that is not usable.
  scale_answer="$(/usr/bin/python3 "$desktop_helper" scale "${DOSPLEX_OUTPUT_WIDTH:-}" "${DOSPLEX_OUTPUT_HEIGHT:-}" \
    "$desktop_root/desktop-scale")" || scale_answer=""
  desktop_scale="${scale_answer%% *}"
  desktop_scale_reason="${scale_answer#* }"
  if [[ ! "$desktop_scale" =~ ^[1-3](\.[0-9]+)?$ ]]; then
    desktop_scale=1
    desktop_scale_reason="no usable answer from the scale helper"
  fi
  # The KWin of this side takes its scale from the preferred scale the Dosplex compositor sends it, not from
  # --scale. The compositor learns the scale from the supervisor, so report it before KWin starts. A launch
  # without a supervisor (a manual run) has none to tell; the scale then stays at 1 in the compositor.
  desktop_scale_report="no supervisor control socket"
  if [[ -n "${DOSPLEX_SESSION_CONTROL:-}" && -n "${DOSPLEX_SESSION_INSTANCE:-}" && -f "${DOSPLEX_SESSION_CONTROL_CLIENT:-}" ]]; then
    if /usr/bin/python3 "$DOSPLEX_SESSION_CONTROL_CLIENT" client-desktop-scale "$desktop_scale" 2>/dev/null; then
      desktop_scale_report="sent to the supervisor"
    else
      desktop_scale_report="the supervisor refused it; the compositor keeps scale 1"
    fi
  fi
  {
    echo "[$(date -Is)] side=$side owner=$owner payload=desktop"
    echo "desktop output=${DOSPLEX_OUTPUT_WIDTH:-unknown}x${DOSPLEX_OUTPUT_HEIGHT:-unknown} scale=$desktop_scale reason=$desktop_scale_reason"
    echo "desktop config=$XDG_CONFIG_HOME data=$XDG_DATA_HOME"
    echo "desktop scale report: $desktop_scale_report"
  } >> "${DOSPLEX_LAUNCH_LOG:-${workspace_root}/launch.log}" 2>&1 || true
  cat > "$XDG_RUNTIME_DIR/desktop-bin/kwin_wayland_wrapper" <<WRAPPER
#!/bin/sh
exec env WAYLAND_DISPLAY="\$DOSPLEX_HOST_SOCKET" /usr/bin/kwin_wayland_wrapper --fullscreen true --no-lockscreen --scale $desktop_scale "\$@"
WRAPPER
  chmod 700 "$XDG_RUNTIME_DIR/desktop-bin/kwin_wayland_wrapper"
  export PATH="$XDG_RUNTIME_DIR/desktop-bin:$PATH"
}

# Flatpak keeps what an app exports (its menu entry, its icon and a command that starts it) in the exports folder of
# the installation that holds the app. A login shell puts those folders on XDG_DATA_DIRS and on PATH (the profile
# scripts flatpak.sh and flatpak-bindir.sh in /etc/profile.d), but a side starts from an empty environment and runs
# neither, so its desktop would list no Flatpak app in the menu and start none from the command line. This does
# what the two scripts do, written out because they are not Dosplex's to source.
#   - The installations are the one of the user, which Flatpak looks for in $FLATPAK_USER_DIR or else in
#     $XDG_DATA_HOME/flatpak, and every system installation that "flatpak --installations" prints. Without flatpak,
#     or when it fails or prints no path, the system installation is the default one ($FLATPAK_SYSTEM_DIR or else
#     /var/lib/flatpak).
#   - exports/share of each goes in front of XDG_DATA_DIRS (whose default is /usr/local/share:/usr/share), as
#     flatpak.sh puts it, and exports/bin behind PATH, so that no command of the system gives way to one that was
#     installed for a user. A folder that does not exist, or that is there already, is left out.
# Flatpak looks for the installation of the user below XDG_DATA_HOME, which prepare_desktop_profile moves into the
# profile of the side. So this runs before that, and it sets FLATPAK_USER_DIR to the installation that the folders
# came from, so that Flatpak does not look for it in the moved folder. It sets flatpak_user_dir for the launch log,
# and it is plain sh apart from local.
add_flatpak_exports() {
  local installations="" installation="" share_dirs="" bin_dirs="" dir=""
  flatpak_user_dir="${FLATPAK_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/flatpak}"
  if command -v flatpak >/dev/null 2>&1; then
    installations="$(unset G_MESSAGES_DEBUG; GIO_USE_VFS=local flatpak --installations 2>/dev/null)" || installations=""
  fi
  case "$installations" in
    /*) ;;
    *) installations="${FLATPAK_SYSTEM_DIR:-/var/lib/flatpak}" ;;
  esac
  while IFS= read -r installation; do
    installation="${installation%/}"
    case "$installation" in
      *:*) continue ;;     # a colon would split the folder in two
      /?*) ;;
      *) continue ;;       # only an absolute path is a place
    esac
    dir="$installation/exports/share"
    if [ -d "$dir" ]; then
      case ":${XDG_DATA_DIRS:-}:$share_dirs:" in
        *":$dir:"*|*":$dir/:"*) ;;
        *) share_dirs="${share_dirs:+$share_dirs:}$dir" ;;
      esac
    fi
    dir="$installation/exports/bin"
    if [ -d "$dir" ]; then
      case ":${PATH:-}:$bin_dirs:" in
        *":$dir:"*|*":$dir/:"*) ;;
        *) bin_dirs="${bin_dirs:+$bin_dirs:}$dir" ;;
      esac
    fi
  done <<EOF
$flatpak_user_dir
$installations
EOF
  if [ -n "$share_dirs" ]; then
    export XDG_DATA_DIRS="$share_dirs:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
  fi
  if [ -n "$bin_dirs" ]; then
    export PATH="${PATH:+$PATH:}$bin_dirs"
  fi
  export FLATPAK_USER_DIR="$flatpak_user_dir"
}

launch_desktop_session() {
  command -v startplasma-wayland >/dev/null 2>&1 || {
    echo "Nested Plasma Wayland is unavailable" >&2; return 1;
  }
  [[ -n "${WAYLAND_DISPLAY:-}" ]] || return 1
  export XDG_SESSION_TYPE=wayland
  export XDG_CURRENT_DESKTOP=KDE
  export DESKTOP_SESSION=plasma
  export KDE_FULL_SESSION=true
  # Steam Input moves KWin’s internal pointer independently of the host pointer.
  # Include its cursor in the nested desktop image instead of a host cursor plane.
  export KWIN_FORCE_SW_CURSOR=1
  export QT_QPA_PLATFORM=wayland
  unset DISPLAY
  # The nested desktop has one X server. With STEAM_MULTIPLE_XWAYLANDS (set above for a side's Gamescope) the
  # desktop's Steam looks for a second one at the next display number, cannot open it, and its input generator
  # never starts: a controller's desktop layout loads but the trackpad moves no pointer and no button clicks
  # (Steam Machine, 2026-11-18: "CInputGenerator XOpenDisplay failed", "Couldn't open display :3"). SteamOS's own
  # Desktop session does not have the variable either.
  unset STEAM_MULTIPLE_XWAYLANDS
  add_flatpak_exports
  # No use_bundled_vulkan_layer here: the desktop has no Gamescope and no ENABLE_GAMESCOPE_WSI (the supervisor starts
  # the side from a cleared environment), so neither the host's nor the bundled layer is active for its games. A game
  # that runs gamescope from its launch options gets the installed Gamescope with its layer, as on a stock desktop.
  prepare_desktop_profile
  {
    echo "desktop flatpak user installation=$flatpak_user_dir"
    echo "desktop XDG_DATA_DIRS=${XDG_DATA_DIRS:-}"
    echo "desktop PATH=$PATH"
  } >> "${DOSPLEX_LAUNCH_LOG:-${workspace_root}/launch.log}" 2>&1 || true
  # Without a user systemd manager, Plasma's legacy launcher does not receive
  # KWin's display environment. Both names are private to this fresh namespace;
  # the KWin wrapper alone connects to the outer Dosplex socket.
  # bwrap gives the side a fresh /tmp. KWin's Xwayland socket allocator
  # requires this directory to exist before it starts.
  if [[ ! -e /tmp/.X11-unix ]]; then mkdir -m 1777 /tmp/.X11-unix; fi
  [[ -d /tmp/.X11-unix && ! -L /tmp/.X11-unix ]] || return 1
  env WAYLAND_DISPLAY="$DOSPLEX_HOST_SOCKET" /usr/bin/kwin_wayland_wrapper --fullscreen true --no-lockscreen --xwayland --scale "$desktop_scale" &
  nested_pid=$!
  for ((attempt=0; attempt<100; attempt++)); do
    busctl --user status org.kde.KWinWrapper >/dev/null 2>&1 && break
    kill -0 "$nested_pid" || { echo "Nested compositor failed" >&2; return 1; }
    sleep .1
  done
  busctl --user status org.kde.KWinWrapper >/dev/null 2>&1 || return 1
  mapfile -d '' -t nested_env < <(/usr/bin/python3 "$desktop_helper" "$nested_pid")
  [[ ${#nested_env[@]} == 3 ]] || return 1
  export WAYLAND_DISPLAY="${nested_env[0]}"
  export DISPLAY="${nested_env[1]}"
  export XAUTHORITY="${nested_env[2]}"
  exec startplasma-wayland
}

case "$payload" in
  steam)
    launch_steam_session
    ;;
  desktop)
    launch_desktop_session
    ;;
  terminal)
    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
      launch_wayland_terminal
    fi
    if command -v xterm >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
      launch_xterm_terminal
    fi
    echo "No usable terminal launcher found for Wayland or X11" >&2
    exit 1
    ;;
  *)
    echo "Unsupported Dosplex session payload" >&2
    exit 1
    ;;
esac

echo "Dosplex session launcher returned without starting its activity" >&2
exit 1
