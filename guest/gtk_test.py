# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

def on_activate(app):
    win = Gtk.ApplicationWindow(application=app)
    win.set_title("Test Window")
    win.present()

app = Gtk.Application(application_id='com.test.GtkApp')
app.connect('activate', on_activate)
app.run(None)
