#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
set -euo pipefail

side="${1:-}"
if [[ -z "$side" ]]; then
  echo "usage: $0 <left|right>" >&2
  exit 1
fi

case "$side" in
  left)
    pane_title="Dosplex Left Login"
    ;;
  right)
    pane_title="Dosplex Right Login"
    ;;
  *)
    echo "invalid side: $side" >&2
    exit 1
    ;;
esac

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing dependency: $1" >&2
    exit 1
  }
}

require_cmd sudo
require_cmd getent
require_cmd awk

list_login_users() {
  getent passwd | awk -F: '
    $3 >= 1000 &&
    $7 != "/usr/sbin/nologin" &&
    $7 != "/bin/false" {
      print $1 "|" $5
    }
  '
}

prompt_with_zenity() {
  local users=()
  local line username gecos label

  while IFS='|' read -r username gecos; do
    [[ -n "$username" ]] || continue
    label="$username"
    if [[ -n "$gecos" ]]; then
      label="$username ($gecos)"
    fi
    users+=("$username" "$label")
  done < <(list_login_users)

  if [[ "${#users[@]}" -eq 0 ]]; then
    return 1
  fi

  zenity --list \
    --title="$pane_title" \
    --text="Choose the user who should own the ${side} side of Dosplex." \
    --column="username" \
    --column="account" \
    --hide-column=1 \
    --print-column=1 \
    "${users[@]}"
}

prompt_with_xterm() {
  local temp_file menu_file
  temp_file="$(mktemp)"
  menu_file="$(mktemp)"
  list_login_users > "$menu_file"

  xterm -T "$pane_title" -fa Monospace -fs 11 -e bash -lc '
    mapfile -t rows < "'"$menu_file"'"
    printf "Claim the '"$side"' side of Dosplex\n\n"
    for i in "${!rows[@]}"; do
      username="${rows[$i]%%|*}"
      desc="${rows[$i]#*|}"
      if [[ -n "$desc" ]]; then
        printf "%s. %s (%s)\n" "$((i + 1))" "$username" "$desc"
      else
        printf "%s. %s\n" "$((i + 1))" "$username"
      fi
    done
    printf "\nSelection: "
    read -r selection
    if [[ "$selection" =~ ^[0-9]+$ ]] && (( selection >= 1 && selection <= ${#rows[@]} )); then
      printf "%s\n" "${rows[$((selection - 1))]%%|*}" > "'"$temp_file"'"
    fi
  ' >/dev/null 2>&1 || true

  rm -f "$menu_file"

  if [[ -s "$temp_file" ]]; then
    cat "$temp_file"
    rm -f "$temp_file"
    return 0
  fi

  rm -f "$temp_file"
  return 1
}

launch_owned_session() {
  local username="$1"
  local passwd_entry home shell session_snippet bg_color title_prefix

  passwd_entry="$(getent passwd "$username" || true)"
  [[ -n "$passwd_entry" ]] || return 1

  home="$(printf '%s\n' "$passwd_entry" | cut -d: -f6)"
  shell="$(printf '%s\n' "$passwd_entry" | cut -d: -f7)"
  [[ -n "$home" ]] || return 1
  [[ -n "$shell" ]] || return 1

  case "$side" in
    left)
      bg_color="#123c73"
      title_prefix="Dosplex Left"
      ;;
    right)
      bg_color="#6b1f1f"
      title_prefix="Dosplex Right"
      ;;
    *)
      return 1
      ;;
  esac

  session_snippet=$(cat <<EOF
if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid '$bg_color' || true
fi
if command -v xterm >/dev/null 2>&1; then
  exec xterm -T '${title_prefix} - ${username}' -fa Monospace -fs 11 -e "printf 'Dosplex ${side} side is now owned by ${username} on %s\n' \"\$DISPLAY\"; printf 'Exit this shell to return to the chooser.\n'; exec bash -l"
fi
if command -v glxgears >/dev/null 2>&1; then
  exec glxgears
fi
sleep 30
EOF
)

  sudo -H -u "$username" env \
    HOME="$home" \
    USER="$username" \
    LOGNAME="$username" \
    SHELL="$shell" \
    DISPLAY="$DISPLAY" \
    XAUTHORITY="$XAUTHORITY" \
    XDG_SESSION_TYPE="x11" \
    dbus-run-session \
    bash -lc "$session_snippet"
}

show_error() {
  local message="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="$pane_title" --text="$message" 2>/dev/null || true
  else
    printf '%s\n' "$message" >&2
  fi
}

while true; do
  set +e
  if command -v zenity >/dev/null 2>&1; then
    username="$(prompt_with_zenity)"
  else
    username="$(prompt_with_xterm)"
  fi
  prompt_rc=$?
  set -e

  if [[ "$prompt_rc" -ne 0 || -z "${username:-}" ]]; then
    exit 0
  fi

  if ! launch_owned_session "$username"; then
    show_error "Failed to start a Dosplex session for $username."
  fi
done
