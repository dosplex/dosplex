#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import argparse
import faulthandler
import json
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

APP_ID = "com.dosplex.vmprototype"
AUTH_HELPER = "/usr/local/libexec/dosplex-side-auth.py"

def log(message):
    print(f"[dosplex-vm-app] {message}", flush=True)

def run_helper(args, password=""):
    completed = subprocess.run(
        ["sudo", "-n", AUTH_HELPER, *args],
        input=password,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or "helper failed")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid helper output: {exc}") from exc
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "helper failed"))
    return payload

class SidePane(Gtk.Box):
    def __init__(self, side_name):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.side_name = side_name
        self.side_key = side_name.lower()
        self.set_margin_top(32)
        self.set_margin_bottom(32)
        self.set_margin_start(32)
        self.set_margin_end(32)

        title = Gtk.Label()
        title.set_xalign(0)
        title.set_markup(f"<span size='26000' weight='700'>{GLib.markup_escape_text(side_name)} Side</span>")
        self.pack_start(title, False, False, 0)

        description = Gtk.Label(label=(
            "Sign in with a real local Linux user. "
            "This prototype launches a per-user side process with the user's home, "
            "state, and workspace roots."
        ))
        description.set_xalign(0)
        description.set_line_wrap(True)
        self.pack_start(description, False, False, 0)

        self.users_label = Gtk.Label()
        self.users_label.set_xalign(0)
        self.users_label.set_line_wrap(True)
        self.pack_start(self.users_label, False, False, 0)

        user_label = Gtk.Label(label="Username")
        user_label.set_xalign(0)
        self.pack_start(user_label, False, False, 0)
        
        self.user_entry = Gtk.Entry()
        self.user_entry.set_placeholder_text("user1 or user2")
        self.pack_start(self.user_entry, False, False, 0)

        pass_label = Gtk.Label(label="Password")
        pass_label.set_xalign(0)
        self.pack_start(pass_label, False, False, 0)
        
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.pack_start(self.password_entry, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sign_in_button = Gtk.Button(label="Sign In")
        self.sign_in_button.connect("clicked", self.on_sign_in)
        self.sign_out_button = Gtk.Button(label="Sign Out")
        self.sign_out_button.connect("clicked", self.on_sign_out)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.connect("clicked", self.on_refresh)
        buttons.pack_start(self.sign_in_button, False, False, 0)
        buttons.pack_start(self.sign_out_button, False, False, 0)
        buttons.pack_start(self.refresh_button, False, False, 0)
        self.pack_start(buttons, False, False, 0)

        self.status_title = Gtk.Label(label="Status: unclaimed")
        self.status_title.set_xalign(0)
        self.status_title.set_line_wrap(True)
        self.pack_start(self.status_title, False, False, 0)

        self.status_body = Gtk.Label(label="")
        self.status_body.set_xalign(0)
        self.status_body.set_line_wrap(True)
        self.status_body.set_selectable(True)
        self.pack_start(self.status_body, False, False, 0)

        self.error_label = Gtk.Label(label="")
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_selectable(True)
        self.pack_start(self.error_label, False, False, 0)

        self.refresh_status()

    def _load_users_text(self):
        try:
            payload = run_helper(["list-users"])
        except Exception as exc:
            return f"Available users: helper unavailable ({exc})"

        users = payload.get("users", [])
        if not users:
            return "Available users: none. Provision guest users before claiming a side."
        return "Available users: " + ", ".join(user["name"] for user in users)

    def on_sign_in(self, _button):
        username = self.user_entry.get_text().strip()
        password = self.password_entry.get_text()
        if not username or not password:
            self.set_error("username and password are required")
            return

        self.set_error("")
        try:
            payload = run_helper(["login", self.side_key, username], password=password)
        except Exception as exc:
            self.set_error(str(exc))
            self.refresh_status()
            return

        self.password_entry.set_text("")
        self.set_error("")
        self.apply_status(payload)

    def on_sign_out(self, _button):
        self.set_error("")
        try:
            run_helper(["logout", self.side_key])
        except Exception as exc:
            self.set_error(str(exc))
            return
        self.refresh_status()

    def on_refresh(self, _button):
        self.refresh_status()

    def refresh_status(self):
        self.users_label.set_text(self._load_users_text())
        try:
            payload = run_helper(["status", self.side_key])
        except Exception as exc:
            self.set_error(str(exc))
            self.status_title.set_text("Status: unavailable")
            self.status_body.set_text("")
            return
        self.set_error("")
        self.apply_status(payload)

    def apply_status(self, payload):
        if not payload.get("active"):
            self.status_title.set_text("Status: unclaimed")
            self.status_body.set_text(
                "This side is waiting for login.\n"
                "After sign-in, DosPlex will launch a real Unix-user-owned side worker "
                "with that user's home-backed workspace and state."
            )
            self.sign_in_button.set_sensitive(True)
            self.sign_out_button.set_sensitive(False)
            return

        owner = payload.get("owner", "unknown")
        self.status_title.set_text(f"Status: claimed by {owner}")
        self.status_body.set_text(
            f"owner={owner}\n"
            f"pid={payload.get('pid')}\n"
            f"home={payload.get('home')}\n"
            f"workspace_root={payload.get('workspace_root')}\n"
            f"session_root={payload.get('session_root')}\n"
            "scope=real Unix user, isolated files and process owner, shared compositor for now"
        )
        self.user_entry.set_text(owner)
        self.sign_in_button.set_sensitive(False)
        self.sign_out_button.set_sensitive(True)

    def set_error(self, message):
        if message:
            self.error_label.set_text(f"Error: {message}")
        else:
            self.error_label.set_text("")


class DosplexWindow(Gtk.ApplicationWindow):
    def __init__(self, app, demo_timeout=None):
        super().__init__(application=app, title="DosPlex VM Prototype")
        log("window_init_start")
        self.demo_timeout = demo_timeout
        self.set_default_size(1280, 720)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(24)
        root.set_margin_bottom(24)
        root.set_margin_start(24)
        root.set_margin_end(24)
        self.add(root)

        title = Gtk.Label()
        title.set_xalign(0)
        title.set_markup("<span size='32000' weight='800'>DosPlex</span>")
        
        subtitle = Gtk.Label(label=(
            "Split-login VM prototype. Each half can authenticate a real local user "
            "and launch a separate user-owned side process."
        ))
        subtitle.set_xalign(0)
        subtitle.set_line_wrap(True)
        
        root.pack_start(title, False, False, 0)
        root.pack_start(subtitle, False, False, 0)

        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)

        self.left = SidePane("Left")
        self.right = SidePane("Right")

        panes.pack_start(self.left, True, True, 0)
        panes.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        panes.pack_start(self.right, True, True, 0)
        root.pack_start(panes, True, True, 0)

        GLib.timeout_add_seconds(2, self._poll_status)

        if demo_timeout:
            GLib.timeout_add_seconds(demo_timeout, self._on_demo_timeout)

        log("window_init_done")
        self.show_all()

    def present(self):
        log("window_present")
        super().present()

    def _poll_status(self):
        self.left.refresh_status()
        self.right.refresh_status()
        return GLib.SOURCE_CONTINUE

    def _on_demo_timeout(self):
        self.close()
        return GLib.SOURCE_REMOVE


class DosplexApp(Gtk.Application):
    def __init__(self, demo_timeout=None):
        super().__init__(application_id=APP_ID)
        self.demo_timeout = demo_timeout

    def do_activate(self):
        log("app_activate")
        win = self.props.active_window
        if not win:
            win = DosplexWindow(self, demo_timeout=self.demo_timeout)
        win.present()


def main():
    faulthandler.enable(file=sys.stderr, all_threads=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-timeout", type=int, default=0)
    args = parser.parse_args()

    log("main_start")
    app = DosplexApp(demo_timeout=args.demo_timeout or None)
    raise SystemExit(app.run(None))

if __name__ == "__main__":
    main()
