#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120 skips-ok="the rules are tested with Node, which a build machine may lack; the static checks always run"
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The panel (Help and Feedback): its rules under Node, and what can be checked without Qt.

The QML itself runs only on SteamOS's qml6; the workstation has none. What is checked here without it: every rule
the QML calls exists in logic.mjs, every QML file the window loads is there, and no QML file writes the person's
text anywhere but into the one outbox line the launcher keeps."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / 'packaging/steamos/panel'
QML = sorted(PANEL.glob('*.qml'))


class Static(unittest.TestCase):
    def test_every_rule_the_qml_calls_is_exported_by_the_logic(self):
        logic = (PANEL / 'logic.mjs').read_text()
        exported = set(re.findall(r'^export (?:function|const) (\w+)', logic, re.M))
        self.assertTrue(QML)
        for path in QML:
            for name in set(re.findall(r'\bLogic\.(\w+)', path.read_text())):
                self.assertIn(name, exported, '%s uses Logic.%s' % (path.name, name))

    def test_the_pages_the_window_loads_are_there(self):
        window = (PANEL / 'Panel.qml').read_text()
        for name in re.findall(r'"(\w+\.qml)"', window):
            self.assertTrue((PANEL / name).is_file(), name)

    def test_what_a_person_wrote_is_printed_only_as_the_outbox_line(self):
        # The launcher never echoes a DOSPLEX-OUTBOX line; any other console line ends in the journal.
        form = (PANEL / 'Feedback.qml').read_text()
        logged = [line for line in form.splitlines() if 'console.log' in line]
        self.assertEqual(len(logged), 2, logged)
        outbox = [line for line in logged if 'DOSPLEX-OUTBOX' in line]
        self.assertEqual(len(outbox), 1)
        other = [line for line in logged if 'DOSPLEX-OUTBOX' not in line][0]
        # The key trace holds the typed text too. It is printed in a test run only (found on a Deck: Qt wrote it
        # into the system journal).
        self.assertIn('if (trace) console.log("DOSPLEX-KEY', other)
        self.assertIn('property bool trace: false', form)
        window = (PANEL / 'Panel.qml').read_text()
        self.assertEqual(window.count('"trace": keys !== ""'), 2)

    def test_nothing_but_the_endpoint_and_the_manual_is_asked_for(self):
        for path in QML:
            opens = re.findall(r'\.open\("(\w+)", ([^,]+),', path.read_text())
            for method, target in opens:
                self.assertIn((path.name, method, target.strip()),
                              {('Feedback.qml', 'POST', 'endpoint'),
                               ('Help.qml', 'GET', '"file://" + manualDir + "/" + name')})


class Rules(unittest.TestCase):
    def test_the_rules_pass_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        done = subprocess.run([node, '--test'] + [str(p) for p in sorted((PANEL / 'test').glob('*.mjs'))],
                              capture_output=True, text=True, timeout=100)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-1000:])


if __name__ == '__main__':
    unittest.main()
