#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Connection health derived from native reports, not presence of the USB puck."""


class ControllerHealth:
    def __init__(self, timeout=2.0):
        self.timeout=timeout
        self.last_input=None
        self.connected=False
        self.reports=0
        self.disconnects=0
        self.generation=0

    def input(self, now):
        if not self.connected:self.generation+=1
        self.connected=True;self.last_input=now;self.reports+=1

    def disconnect(self):
        changed=self.connected
        if changed:self.disconnects+=1
        self.connected=False;self.last_input=None
        return changed

    def expired(self, now):
        if self.connected and self.last_input is not None and now-self.last_input>=self.timeout:
            return self.disconnect()
        return False

    def snapshot(self, now):
        return {'connected':self.connected,'input_reports':self.reports,
                'connection_generation':self.generation,'disconnects':self.disconnects,
                'input_age_seconds':None if self.last_input is None else round(max(0,now-self.last_input),3)}
