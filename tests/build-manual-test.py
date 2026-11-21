#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Build the manual for the television: one small tool, one test file, both of them the manual's contract.

The test runs tools/build-manual.py as a subprocess on small sources it writes into a temporary
folder and checks the exact HTML it writes; then it builds the real manual in docs/manual and
checks that what comes out is the small subset of HTML 4 the Qt Quick Text item understands.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / 'tools' / 'build-manual.py'
REAL_MANUAL = ROOT / 'docs' / 'manual'
CHAPTERS = tuple(range(1, 18))


def run_build(source, out, strict=False, target='panel'):
    """(exit status, stdout, stderr) of one build of source into out."""
    arguments = [sys.executable, str(PROGRAM), '--source', str(source), '--out', str(out),
                 '--target', target]
    if strict:
        arguments.append('--strict')
    result = subprocess.run(arguments, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


class BuildManualTest(unittest.TestCase):

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='build-manual-'))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.source = self.temp / 'source'
        self.source.mkdir()
        self.out = self.temp / 'out'

    def write_chapter(self, name, text):
        (self.source / name).write_text(text, encoding='utf-8')
        return self.source / name

    def build(self, *chapters, strict=False):
        """Write the given (name, text) chapters and build them; returns (code, out, err)."""
        for name, text in chapters:
            self.write_chapter(name, text)
        return run_build(self.source, self.out, strict)

    def build_site(self, *chapters, strict=False):
        """The same chapters as website pages; returns (code, out, err)."""
        for name, text in chapters:
            self.write_chapter(name, text)
        return run_build(self.source, self.out, strict, target='site')

    def text_of(self, name, text):
        self.write_chapter(name, text)
        return self.html(name)

    def html(self, name):
        return (self.out / name).read_text(encoding='utf-8')

    def test_headings(self):
        self.assertEqual(self.build(('01-a.md', '# One\n\n## Two\n\n### Three\n\n#### Four\n'))[0], 0)
        self.assertEqual(self.html('01-a.html'),
                         '<h1>One</h1>\n<h2>Two</h2>\n<h3>Three</h3>\n<h3>Four</h3>\n')

    def test_paragraphs_join(self):
        self.build(('01-a.md', 'First line\nsecond line.\n\nAnother one\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<p>First line second line.</p>\n<p>Another one</p>\n')

    def test_escaping(self):
        self.build(('01-a.md', 'a < b & c\n\n`<tag>`\n\n```c\n<tag>\n**not bold**\n```\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<p>a &lt; b &amp; c</p>\n'
                         '<p><code>&lt;tag&gt;</code></p>\n'
                         '<pre>&lt;tag&gt;\n**not bold**</pre>\n')

    def test_fenced_block_drops_language_and_keeps_lines(self):
        self.build(('01-a.md', '```bash\nline one\nline two\n```\n'))
        self.assertEqual(self.html('01-a.html'), '<pre>line one\nline two</pre>\n')

    def test_bullet_list_with_continuation(self):
        self.build(('01-a.md', '- first\n  continues here\n- second\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<ul>\n<li>first continues here</li>\n<li>second</li>\n</ul>\n')

    def test_nested_list(self):
        self.build(('01-a.md', '- top\n  - nested one\n  - nested two\n- next\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<ul>\n<li>top\n<ul>\n<li>nested one</li>\n<li>nested two</li>\n</ul></li>\n'
                         '<li>next</li>\n</ul>\n')

    def test_numbered_list(self):
        self.build(('01-a.md', '1. first\n2. second\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<table cellspacing="0" cellpadding="0">\n<tr><td>1.&nbsp;</td><td>first</td></tr>\n'
                         '<tr><td>2.&nbsp;</td><td>second</td></tr>\n</table>\n')

    def test_table_with_bold_cell(self):
        self.build(('01-a.md', '| A | B |\n| --- | --- |\n| **x** | y |\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<table border="1" cellspacing="0" cellpadding="6">\n'
                         '<tr><th>A</th><th>B</th></tr>\n'
                         '<tr><td><b>x</b></td><td>y</td></tr>\n'
                         '</table>\n')

    def test_emphasis(self):
        self.build(('01-a.md', '**bold** and *ital* and _under_ and `code`\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<p><b>bold</b> and <i>ital</i> and <i>under</i> and <code>code</code></p>\n')

    def test_not_emphasis(self):
        self.build(('01-a.md', 'some_name and 2 * 3\n'))
        self.assertEqual(self.html('01-a.html'), '<p>some_name and 2 * 3</p>\n')

    def test_links(self):
        self.build(('01-a.md', '[site](https://example.org) and [next](02-b.md)\n'))
        self.assertEqual(self.html('01-a.html'),
                         '<p>site (https://example.org) and next</p>\n')

    def test_image_copied_when_present(self):
        (self.source / 'pic.png').write_bytes(b'png-bytes')
        self.build(('01-a.md', '![The picture](pic.png)\n'))
        self.assertEqual(self.html('01-a.html'), '<p><img src="pic.png"></p>\n')
        self.assertEqual((self.out / 'pic.png').read_bytes(), b'png-bytes')

    def test_image_placeholder_when_missing(self):
        self.build(('01-a.md', '![The picture](missing.png)\n'))
        self.assertEqual(self.html('01-a.html'), '<p><i>[Picture: The picture]</i></p>\n')

    def test_multiline_comment_removed(self):
        self.build(('01-a.md', 'before\n<!-- one\n\ntwo\n\nthree -->\n\nafter\n'))
        self.assertEqual(self.html('01-a.html'), '<p>before</p>\n<p>after</p>\n')

    def test_quote_and_rule(self):
        self.build(('01-a.md', '> a quote\n\n---\n'))
        self.assertEqual(self.html('01-a.html'), '<p><i>a quote</i></p>\n<hr>\n')

    def test_index_file(self):
        self.build(('01-first.md', '# First\n\n12-second'), ('01-second.md', '# Second\n'),
                   ('README.md', '# not a chapter\n'), ('notes.txt', 'skipped\n'))
        self.assertEqual(self.build(('02-later.md', 'no heading\n'))[0], 0)
        index = json.loads((self.out / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual(index, [
            {'file': '01-first.html', 'chapter': '01-first', 'title': 'First'},
            {'file': '01-second.html', 'chapter': '01-second', 'title': 'Second'},
            {'file': '02-later.html', 'chapter': '02-later', 'title': ''},
        ])
        self.assertFalse((self.out / 'README.html').exists())
        self.assertFalse((self.out / 'notes.txt').exists())

    def test_todo_without_strict_is_a_warning(self):
        code, out, err = self.build(('01-a.md', '# A\n\nTODO(coordinator): check it\n'))
        self.assertEqual(code, 0)
        self.assertIn('01-a.md:3: TODO(coordinator): check it', out)
        self.assertIn('TODO(coordinator): check it', self.html('01-a.html'))

    def test_todo_in_comment_is_not_reported(self):
        code, _, err = self.build(('01-a.md', '# A\n\n<!-- TODO inside\nstill a comment -->\n'))
        self.assertEqual(code, 0)
        self.assertNotIn('TODO', err)
        self.assertEqual(self.build(('01-a.md', '# A\n\n<!-- TODO inside\nstill a comment -->\n'),
                                    strict=True)[0], 0)

    def test_todo_with_strict_is_an_error(self):
        code, out, _ = self.build(('01-a.md', '# A\n\nTODO(coordinator): check it\n'), strict=True)
        self.assertEqual(code, 1)
        self.assertIn('01-a.md:3: TODO(coordinator): check it', out)

    def test_stale_html_removed_other_files_kept(self):
        self.build(('01-gone.md', '# Gone\n'), ('02-kept.md', '# Kept\n'))
        (self.out / 'keep.txt').write_text('stays\n', encoding='utf-8')
        (self.source / '01-gone.md').unlink()
        self.assertEqual(self.build(('02-kept.md', '# Kept\n'))[0], 0)
        self.assertFalse((self.out / '01-gone.html').exists())
        self.assertTrue((self.out / '02-kept.html').exists())
        self.assertEqual((self.out / 'keep.txt').read_text(encoding='utf-8'), 'stays\n')

    def test_two_runs_identical_bytes(self):
        text = '# A\n\n**bold**\n\n- one\n  - sub\n\n```\ncode\n```\n'
        self.build(('01-a.md', text))
        first = sorted(p.read_bytes() for p in self.out.iterdir())
        self.assertEqual(self.build(('01-a.md', text))[0], 0)
        second = sorted(p.read_bytes() for p in self.out.iterdir())
        self.assertEqual(first, second)

    def test_no_chapter_gives_exit_2(self):
        self.assertEqual(self.build(('README.md', '# nothing\n'), ('notes.txt', 'x\n'))[0], 2)
        self.assertFalse(self.out.exists())

    def test_real_manual(self):
        code, _, _ = run_build(REAL_MANUAL, self.out)
        self.assertEqual(code, 0)
        chapter_files = sorted(
            name for name in os.listdir(REAL_MANUAL) if re.match(r'\d{2}-', name))
        self.assertEqual(len(chapter_files), len(CHAPTERS))
        self.assertEqual(len(list(self.out.glob('*.html'))), len(CHAPTERS))
        for path in self.out.glob('*.html'):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('**', text, path.name)
            self.assertNotIn('<!--', text, path.name)
            for line in text.splitlines():
                self.assertFalse(line.startswith('#'), (path.name, line))
        index = json.loads((self.out / 'index.json').read_text(encoding='utf-8'))
        self.assertEqual(len(index), len(CHAPTERS))
        self.assertEqual([entry['file'] for entry in index],
                         [stem + '.html' for stem in (name[:-3] for name in chapter_files)])


class PanelTargetSiteBuildTest(unittest.TestCase):
    """--target site: the same chapters as ordinary HTML5 web pages."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.source = Path(self.dir.name) / 'manual'
        self.source.mkdir()
        self.out = Path(self.dir.name) / 'out'
        self.addCleanup(self.dir.cleanup)

    def write_chapter(self, name, text):
        (self.source / name).write_text(text, encoding='utf-8')

    def build_site(self, *chapters, strict=False):
        """Write the given (name, text) chapters and build them as site pages."""
        for name, text in chapters:
            self.write_chapter(name, text)
        return run_build(self.source, self.out, strict, target='site')

    def html(self, name):
        return (self.out / name).read_text(encoding='utf-8')

    def test_page_structure(self):
        self.write_chapter('01-first.md', '# First\n\nA word.\n')
        self.write_chapter('02-second.md', '# Second\n\nA word.\n')
        self.write_chapter('03-third.md', '# Third\n\nA word.\n')
        status, _, _ = self.build_site()
        self.assertEqual(status, 0)
        first = self.html('01-first.html')
        for line in ('<!DOCTYPE html>', '<html lang="en">', '<meta charset="utf-8">',
                     '<meta name="viewport" content="width=device-width, initial-scale=1">',
                     '<title>First · Dosplex manual</title>',
                     '<link rel="stylesheet" href="manual.css">'):
            self.assertIn(line, first)
        # The first chapter: no "before" link, and both nav blocks sit in the page.
        self.assertEqual(first.count('<nav class="top">'), 2)
        self.assertIn('<a href="index.html">Contents</a>', first)
        self.assertIn('<a href="02-second.html">Second</a>', first)
        self.assertNotIn('href="00-', first)
        self.assertNotIn('Contents</a>Contents', first)
        last = self.html('03-third.html')
        self.assertNotIn('href="04-', last)
        self.assertIn('<a href="02-second.html">Second</a>', last)

    def test_links(self):
        status, _, _ = self.build_site(
            ('01-first.md', '# First\n\n'
             '[Chapter two](02-second.md), [deeper](02-second.md#deeper), '
             '[the site](https://example.com/x), [the notes](notes.txt)\n'),
            ('02-second.md', '# Second\n\n## Deeper\n\n\n'),
        )
        self.assertEqual(status, 0)
        page = self.html('01-first.html')
        self.assertIn('<a href="02-second.html">Chapter two</a>', page)
        self.assertIn('<a href="02-second.html#deeper">deeper</a>', page)
        self.assertIn('<a href="https://example.com/x">the site</a>', page)
        # The address is not printed after the external link.
        self.assertNotIn('https://example.com/x</a>', page.replace('<a href="https://example.com/x">', ''))
        # A link to a file outside the manual is plain text without any link element.
        self.assertIn(', the notes</p>', page)
        self.assertNotIn('notes.txt', page)

    def test_heading_ids(self):
        status, _, _ = self.build_site(
            ('01-first.md', '# First\n\n## The Menu\n\n## The Menu\n\n## The Menu\n\n'
             '## a-b c\n'),
        )
        self.assertEqual(status, 0)
        page = self.html('01-first.html')
        self.assertIn('<h1 id="first">First</h1>', page)
        self.assertIn('<h2 id="the-menu">The Menu</h2>', page)
        self.assertIn('<h2 id="the-menu-2">The Menu</h2>', page)
        self.assertIn('<h2 id="the-menu-3">The Menu</h2>', page)
        self.assertIn('<h2 id="a-b-c">a-b c</h2>', page)

    def test_images(self):
        (self.source / 'shot.png').write_bytes(b'picture')
        status, _, _ = self.build_site(
            ('01-first.md', '# First\n\n'
             '![A picture](shot.png)\n\n![Missing](missing.png)\n'),
        )
        self.assertEqual(status, 0)
        page = self.html('01-first.html')
        self.assertIn('<figure><img src="shot.png" alt="A picture">'
                      '<figcaption>A picture</figcaption></figure>', page)
        self.assertIn('<p><em>[Picture: Missing]</em></p>', page)
        self.assertEqual((self.out / 'shot.png').read_bytes(), b'picture')

    def test_table_strong_em_blockquote(self):
        status, _, _ = self.build_site(
            ('01-first.md', '# First\n\n'
             'a | b\n--- | ---\n1 | 2\n\n**bold** and *italic*\n\n> quoted\n'),
        )
        self.assertEqual(status, 0)
        page = self.html('01-first.html')
        self.assertIn('<table>\n<thead><tr><th>a</th><th>b</th></tr></thead>\n'
                      '<tbody><tr><td>1</td><td>2</td></tr></tbody>\n</table>', page)
        self.assertNotIn('<table border', page)
        self.assertNotIn('cellspacing', page)
        self.assertNotIn('cellpadding', page)
        self.assertIn('<strong>bold</strong>', page)
        self.assertIn('<em>italic</em>', page)
        self.assertIn('<blockquote><p>quoted</p></blockquote>', page)

    def test_escaping(self):
        status, _, _ = self.build_site(
            ('01-first.md', '# First\n\n'
             'x & y < z\n\n[the & "name" <here>](notes.txt)\n\n'
             '![a & b <c>](missing.png)\n'),
        )
        self.assertEqual(status, 0)
        page = self.html('01-first.html')
        self.assertIn('<p>x &amp; y &lt; z</p>', page)
        self.assertIn('the &amp; "name" &lt;here&gt;', page)
        self.assertIn('[Picture: a &amp; b &lt;c&gt;]', page)

    def test_index_and_css(self):
        status, _, _ = self.build_site(
            ('02-second.md', '# Second\n\nA word.\n'),
            ('01-first.md', '# First\n\nA word.\n'),
        )
        self.assertEqual(status, 0)
        index = self.html('index.html')
        self.assertIn('<h1>Dosplex manual</h1>', index)
        self.assertIn('<a href="01-first.html">First</a>', index)
        self.assertIn('<a href="02-second.html">Second</a>', index)
        self.assertLess(index.index('01-first.html'), index.index('02-second.html'))
        css = self.html('manual.css')
        self.assertIn('prefers-color-scheme: dark', css)
        self.assertNotIn('@import', css)
        self.assertNotIn('url(', css)

    def test_stale_html_removed(self):
        self.write_chapter('01-first.md', '# First\n\nA word.\n')
        self.build_site()
        (self.out / 'stale.html').write_text('<html></html>', encoding='utf-8')
        status, _, _ = self.build_site()
        self.assertEqual(status, 0)
        self.assertFalse((self.out / 'stale.html').exists())
        self.assertTrue((self.out / '01-first.html').exists())

    def test_deterministic(self):
        self.write_chapter('01-first.md', '# First\n\nA word.\n')
        self.write_chapter('02-second.md', '# Second\n\nAnother word.\n')
        self.build_site()
        first = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.build_site()
        second = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.assertEqual(first, second)

    def test_strict_todo_fails(self):
        status, _, err = self.build_site(
            ('01-first.md', '# First\n\nA word.\n\nTODO(owner): a note.\n'), strict=True)
        self.assertEqual(status, 1)
        self.assertIn('TODO', err)


class SiteTargetRealManualTest(unittest.TestCase):
    """The site build of docs/manual: every page resolves to a file that exists."""

    def test_real_manual_site(self):
        out = Path(tempfile.mkdtemp(prefix='dosplex-manual-site-test.'))
        self.addCleanup(shutil.rmtree, out)
        status, _, _ = run_build(REAL_MANUAL, out, target='site')
        self.assertEqual(status, 0, 'building the real manual as site pages must succeed')
        self.assertEqual(len(list(out.glob('[0-9][0-9]-*.html'))), len(CHAPTERS))
        self.assertTrue((out / 'index.html').is_file())
        self.assertTrue((out / 'manual.css').is_file())
        for name in ('**', '<!--', '.md"'):
            for path in out.glob('*.html'):
                self.assertNotIn(name, path.read_text(encoding='utf-8'),
                                 'leftover %r in %s' % (name, path.name))
        for path in out.glob('*.html'):
            for match in re.finditer(r'href="([^"]+)"', path.read_text(encoding='utf-8')):
                href = match.group(1)
                if href.startswith('http') or href in ('#',):
                    continue
                self.assertTrue((out / href.split('#')[0]).is_file(),
                                'dangling link %s in %s' % (href, path.name))


if __name__ == '__main__':
    unittest.main()
