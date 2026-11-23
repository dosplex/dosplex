#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Expose SteamOS activity switching on one private DosPlex session bus.

Never forwards calls to the host manager. Steam owns its login/PIN UI; this
adapter scopes the resulting activity request to the caller's supervisor.
"""
import importlib.util
import os
from pathlib import Path

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

NAME = 'com.steampowered.SteamOSManager1'
PATH = '/com/steampowered/SteamOSManager1'
INTERFACE = NAME + '.SessionManagement1'
PROPERTIES = 'org.freedesktop.DBus.Properties'


class Manager(dbus.service.Object):
    def __init__(self, bus, switch):
        self.name = dbus.service.BusName(NAME, bus=bus, do_not_queue=True)
        super().__init__(bus, PATH)
        self.switch = switch
        self.defaults = {'DefaultDesktopSession': 'plasma.desktop', 'DefaultLoginMode': 'game'}

    @dbus.service.method(INTERFACE, in_signature='', out_signature='')
    def SwitchToDesktopMode(self):
        self.switch('desktop')

    @dbus.service.method(INTERFACE, in_signature='s', out_signature='')
    def SwitchToDesktopSession(self, session):
        if session != 'plasma.desktop':
            raise dbus.exceptions.DBusException('Unsupported nested desktop', name=NAME+'.Unsupported')
        self.switch('desktop')

    @dbus.service.method(INTERFACE, in_signature='', out_signature='')
    def SwitchToGameMode(self):
        self.switch('gaming')

    @dbus.service.method(INTERFACE, in_signature='s', out_signature='')
    def SwitchToLoginMode(self, mode):
        if mode not in ('desktop', 'game'):
            raise dbus.exceptions.DBusException('Invalid activity', name=NAME+'.Unsupported')
        self.switch('desktop' if mode == 'desktop' else 'gaming')

    @dbus.service.method(INTERFACE, in_signature='', out_signature='as')
    def ValidDesktopSessions(self):
        return ['plasma.desktop']

    @dbus.service.method(PROPERTIES, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return {k: dbus.String(v) for k, v in self.defaults.items()} if interface == INTERFACE else {}

    @dbus.service.method(PROPERTIES, in_signature='ss', out_signature='v')
    def Get(self, interface, key):
        if interface != INTERFACE or key not in self.defaults:
            raise dbus.exceptions.DBusException('Unknown property', name='org.freedesktop.DBus.Error.UnknownProperty')
        return dbus.String(self.defaults[key])

    @dbus.service.method(PROPERTIES, in_signature='ssv', out_signature='')
    def Set(self, interface, key, value):
        valid = {'DefaultDesktopSession': ('plasma.desktop',), 'DefaultLoginMode': ('desktop', 'game')}
        if interface != INTERFACE or value not in valid.get(key, ()):
            raise dbus.exceptions.DBusException('Invalid property', name='org.freedesktop.DBus.Error.InvalidArgs')
        # Session-local preference only; never writes the host boot mode.
        self.defaults[key] = str(value)


def main():
    if os.environ.get('DOSPLEX_SESSION_RUNTIME') != '1' or os.environ.get('DOSPLEX_SIDE') not in ('left', 'right'):
        raise RuntimeError('requires a private DosPlex side')
    endpoint = os.environ['DOSPLEX_SESSION_CONTROL']
    instance = os.environ['DOSPLEX_SESSION_INSTANCE']
    client = Path(os.environ['DOSPLEX_SESSION_CONTROL_CLIENT'])
    spec = importlib.util.spec_from_file_location('supervisor', client)
    supervisor = importlib.util.module_from_spec(spec); spec.loader.exec_module(supervisor)
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus(private=True)
    manager = Manager(bus, lambda mode: supervisor.client_mode(mode, endpoint, instance))
    ready = Path(os.environ['XDG_RUNTIME_DIR']) / 'dosplex-manager.ready'
    ready.touch(mode=0o600)
    GLib.MainLoop().run()


if __name__ == '__main__':
    main()
