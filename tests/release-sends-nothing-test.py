#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""PRIVACY.md says a release sends nothing anywhere, and this check keeps that true.

The only program in a release that reaches the internet is the dosplex program, and only in dosplex update,
which a person runs by choice. The feedback form is on hold and not part of a release, so the sender that the
panel launcher carries cannot run in one. This test reads the product files of a release (the lines of
packaging/release-build/layout.txt, and the dosplex program that tools/release.py puts beside the bundle), looks
for markers of network code, and keeps an account (KNOWN) of every hit: where it reaches, and why. A new marker in
a product file fails the test until someone has read the code and added it.

It reads text only. What the compiled programs of a release do (the compositor, the controller relay) is not
covered here.
"""
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MARKERS = ('urllib.request', 'urlopen', 'http.client', 'XMLHttpRequest', 'AF_INET',
           'create_connection', 'requests.get', 'requests.post', 'curl ', 'wget ', 'TCP:',
           'https://', 'http://')

# tools/release.py puts this program into the archive beside the bundle; it is not a line of layout.txt.
PROGRAM = 'tools/dosplex'

# Where a hit reaches: the internet, this machine only (127.0.0.1, a Unix socket, a local file), or nothing at all
# (a comment, a help text, a licence line, a name that is never fetched).
INTERNET, LOCAL, NOTHING = 'internet', 'this machine', 'nothing'

# Every network marker the tree has in a product file today: where it reaches, and what the code there does.
KNOWN = {
    ('LICENSE', 'https://'): (NOTHING, 'the licence text names the FSF and GNU sites'),
    ('NOTICE', 'https://'): (NOTHING, 'the notice names the holder\'s website'),
    (PROGRAM, 'urllib.request'):
        (INTERNET, 'fetch_url, called only by dosplex update: the release index, a manifest, a signature and the '
                   'archive, from the channel the person names or the project\'s releases on GitHub'),
    (PROGRAM, 'http.client'): (INTERNET, 'the error type of that same fetch'),
    (PROGRAM, 'https://'): (INTERNET, 'the default channel of dosplex update, and help texts about it'),
    ('packaging/steamos/dosplex-panel-launch.py', 'urllib.request'):
        (INTERNET, 'the feedback sender. It runs only in feedback mode, and the launcher refuses that mode when '
                   'the release has no form, which is the case while the form is on hold'),
    ('packaging/steamos/dosplex-panel-launch.py', 'urlopen'): (INTERNET, 'the one call of that sender'),
    ('packaging/steamos/dosplex-panel-launch.py', 'https://'): (INTERNET, 'the address that sender would use'),
    ('packaging/steamos/panel/Help.qml', 'XMLHttpRequest'):
        (LOCAL, 'Help reads a chapter of the manual from the release\'s own folder, as file://'),
    ('packaging/steamos/icons/dosplex.svg', 'http://'):
        (NOTHING, 'the XML namespace name of the SVG image; nothing fetches it'),
    ('packaging/steamos/session/native-pads.py', 'http://'):
        (NOTHING, 'the DTD name inside a D-Bus policy file the program writes; nothing fetches it'),
    ('packaging/steamos/session/retained-audio-trial.py', 'TCP:'):
        (LOCAL, 'socat joins a Unix socket to the sound server on 127.0.0.1:4713'),
}


def product_sources(root):
    """The (destination, source_path) pairs of layout.txt under root; comments and blanks are skipped."""
    entries = []
    for line in (root / 'packaging' / 'release-build' / 'layout.txt').read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            destination, source = line.split()
            entries.append((destination, source))
    return entries


def network_hits(root):
    """The (source_path, marker) pairs, sorted, for each marker found in each listed source file.

    Only files that exist and read as UTF-8 text are read; the rest are skipped."""
    hits = set()
    for source in [source for _destination, source in product_sources(root)] + [PROGRAM]:
        path = root / source
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            continue
        for marker in MARKERS:
            if marker in text:
                hits.add((source, marker))
    return sorted(hits)


class ReleaseSendsNothing(unittest.TestCase):
    def test_the_feedback_form_is_not_in_a_release(self):
        destinations = [destination for destination, _ in product_sources(ROOT)]
        for destination in destinations:
            self.assertFalse(destination.endswith('Feedback.qml'), destination)
        self.assertIn('share/dosplex/panel/Help.qml', destinations)
        self.assertIn('share/dosplex/panel/Panel.qml', destinations)

    def test_every_network_marker_in_a_product_file_is_known(self):
        new = [pair for pair in network_hits(ROOT) if pair not in KNOWN]
        self.assertEqual(new, [],
                         'new network markers in a product file:\n  ' + '\n  '.join(
                             '%s: %s' % pair for pair in new) +
                         '\nread the code, and if it is right add it to KNOWN with a reason; '
                         'PRIVACY.md says a release sends nothing')

    def test_known_has_no_stale_entry(self):
        stale = [key for key in KNOWN if key not in network_hits(ROOT)]
        self.assertEqual(stale, [], 'stale KNOWN entries:\n  ' + '\n  '.join(
            '%s: %s' % key for key in stale))

    def test_only_the_update_command_and_the_parked_sender_reach_the_internet(self):
        internet = {source for (source, _marker), (reach, _why) in KNOWN.items() if reach == INTERNET}
        self.assertEqual(internet, {PROGRAM, 'packaging/steamos/dosplex-panel-launch.py'})

    def test_the_parked_sender_cannot_run_without_the_form(self):
        # The launcher's refusal comes before anything is started or sent (tests/panel-launch-test.py shows it
        # running); here only that the refusal is still in the file the release carries.
        launcher = (ROOT / 'packaging/steamos/dosplex-panel-launch.py').read_text()
        refusal = launcher.index("'The feedback form is not part of this release.'")
        self.assertLess(refusal, launcher.index('subprocess.Popen('))
        self.assertLess(refusal, launcher.index('_send_thread, args='))


class TheCheckItself(unittest.TestCase):
    # The made-up files have no extension: the publication check reads a script's name in a published file as a
    # file that has to exist.
    LAYOUT = 'packaging/release-build/layout.txt'

    def make_tree(self, layout_text, sources):
        """A small tree under a temporary directory: its layout.txt plus the given source files."""
        temp = tempfile.TemporaryDirectory(prefix='dosplex-sends-nothing-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        layout = root / self.LAYOUT
        layout.parent.mkdir(parents=True, exist_ok=True)
        layout.write_text(layout_text)
        for source, text in sources.items():
            path = root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return root

    def test_a_product_file_that_opens_an_url_gives_hits_for_each_marker(self):
        root = self.make_tree(
            'share/dosplex/x  made-up/x\n',
            {'made-up/x': "import urllib.request\nurllib.request.urlopen('https://example.org')\n"})
        self.assertEqual(network_hits(root),
                         [('made-up/x', 'https://'), ('made-up/x', 'urllib.request'), ('made-up/x', 'urlopen')])

    def test_a_file_not_in_the_layout_is_not_read(self):
        root = self.make_tree(
            'share/dosplex/x  made-up/x\n',
            {'made-up/x': 'import json\n',
             'made-up/not-listed': "import urllib.request\nurllib.request.urlopen('https://example.org')\n"})
        self.assertEqual(network_hits(root), [])

    def test_a_listed_source_file_that_does_not_exist_is_skipped(self):
        root = self.make_tree(
            'share/dosplex/a  made-up/a\nshare/dosplex/b  made-up/missing\n',
            {'made-up/a': "import urllib.request\nurllib.request.urlopen('https://example.org')\n"})
        self.assertEqual(network_hits(root),
                         [('made-up/a', 'https://'), ('made-up/a', 'urllib.request'), ('made-up/a', 'urlopen')])

    def test_comment_and_blank_lines_of_the_layout_are_skipped(self):
        root = self.make_tree(
            '# a comment\n\nbin/one  made-up/one\n# share/dosplex/none  made-up/none\n\n',
            {'made-up/one': "import urllib.request\nurllib.request.urlopen('https://example.org')\n"})
        self.assertEqual(product_sources(root), [('bin/one', 'made-up/one')])


if __name__ == '__main__':
    unittest.main()
