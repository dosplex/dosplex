#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Build the manual chapters of docs/manual.

--target panel (the default) is for the Qt Quick Text item: each chapter (NN-name.md, two digits
and a dash) becomes DIR/NN-name.html, the small subset of HTML 4 the Text item understands, and
DIR/index.json lists them in order with their title. --target site is for the website: one full
HTML5 page per chapter plus DIR/index.html, DIR/manual.css and the pictures. README.md and other
files are skipped. Files in DIR a previous run left behind are removed. The output is the same on
every run.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

CHAPTER = re.compile(r'^(\d{2})-([\w-]+)\.md$')
FENCE = re.compile(r'^```')
HEADING = re.compile(r'^(#{1,6}) (.*)$')
BULLET = re.compile(r'^- (.*)$')
NUMBERED = re.compile(r'^\d+\. (.*)$')
RULE = re.compile(r'^-{3,}$')
QUOTE = re.compile(r'^> (.*)$')
BOLD = re.compile(r'\*\*([^*]+)\*\*')
ITALIC_STAR = re.compile(r'(?<![A-Za-z0-9*])\*([^*\n]+)\*(?!\*)')
ITALIC_UNDERSCORE = re.compile(r'(?<![\w*])_([^_\n]+)_(?![\w*])')
BACKTICK = re.compile(r'`([^`]+)`')
LINK = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)\)')
COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)

SITE_CSS = """body {
  margin: 0;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.6;
}
nav.top {
  display: flex;
  gap: 1em;
  padding: 0.6em 1em;
  border-bottom: 1px solid #777;
  background: #f4f4f4;
}
nav.top a {
  text-decoration: none;
}
main {
  max-width: 44em;
  margin: 0 auto;
  padding: 1.5em 1em 3em;
}
pre, code {
  font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  background: #f0f0f0;
  border-radius: 3px;
}
pre {
  padding: 0.6em 0.8em;
  overflow-x: auto;
}
code {
  padding: 0 0.2em;
}
table {
  border-collapse: collapse;
  margin: 1em 0;
}
th, td {
  border: 1px solid #777;
  padding: 0.3em 0.7em;
  text-align: left;
}
figure {
  margin: 1.2em 0;
}
img {
  max-width: 100%;
}
figcaption {
  font-size: 0.85em;
  color: #444;
}
a {
  color: #0645ad;
}
@media (prefers-color-scheme: dark) {
  body {
    background: #1b1b1b;
    color: #ddd;
  }
  nav.top {
    background: #262626;
    border-bottom: 1px solid #444;
  }
  pre, code {
    background: #2a2a2a;
  }
  th, td {
    border: 1px solid #555;
  }
  a {
    color: #7db4f5;
  }
  figcaption {
    color: #999;
  }
}
"""


def escape(text):
    """& first, then < and >; " only in site output, inside attributes."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def strip_comments(text):
    """All HTML comments gone; a comment that never closes runs to the end of the file."""
    out = []
    i = 0
    while i < len(text):
        start = text.find('<!--', i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find('-->', start)
        i = len(text) if end < 0 else end + 3
    return ''.join(out)


def render_inline(text, folder=None, copied=None, site=False):
    """The allowed inline markup of one line; everything else is plain escaped text."""
    text = escape(text)

    def image(match):
        alt = match.group(1)
        name = Path(match.group(2)).name
        if folder is not None and (folder / name).is_file():
            if copied is not None:
                copied.add(name)
            if site:
                alt_out = alt.replace('"', '&quot;')
                return ('<figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>'
                        % (name, alt_out, alt_out))
            return '<img src="%s">' % name
        if site:
            return '<em>[Picture: %s]</em>' % alt.replace('"', '&quot;')
        return '<i>[Picture: %s]</i>' % alt

    def link(match):
        text = match.group(1)
        target = match.group(2)
        if site:
            if target.startswith(('http://', 'https://')):
                return '<a href="%s">%s</a>' % (target, text)
            if re.fullmatch(r'[\w.-]+\.md(#[\w-]+)?', target):
                return '<a href="%s">%s</a>' % (target.replace('.md', '.html'), text)
            return text
        if target.startswith(('http://', 'https://')):
            return '%s (%s)' % (text, target)
        return text

    bold = ('<strong>%s</strong>' if site else '<b>%s</b>')
    italic = ('<em>%s</em>' if site else '<i>%s</i>')
    text = IMAGE.sub(image, text)
    text = LINK.sub(link, text)
    text = BACKTICK.sub(lambda match: '<code>%s</code>' % match.group(1), text)
    text = BOLD.sub(lambda match: bold % match.group(1), text)
    text = ITALIC_STAR.sub(lambda match: italic % match.group(1), text)
    text = ITALIC_UNDERSCORE.sub(lambda match: italic % match.group(1), text)
    return text


def table_cells(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def is_separator_row(cells):
    cells = [cell for cell in cells if cell]
    return bool(cells) and all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells)


def render_table(lines, site=False):
    out = ['<table>' if site else '<table border="1" cellspacing="0" cellpadding="6">']
    first = True
    rows = []
    for line in lines:
        cells = table_cells(line)
        if is_separator_row(cells):
            continue
        cell = 'th' if first else 'td'
        first = False
        rows.append('<tr>%s</tr>' % ''.join(
            '<%s>%s</%s>' % (cell, render_inline(text, site=site), cell) for text in cells))
    if site and rows:
        out.append('<thead>%s</thead>' % rows[0])
        out.append('<tbody>%s</tbody>' % '\n'.join(rows[1:]) if len(rows) > 1 else '<tbody></tbody>')
    else:
        out.extend(rows)
    out.append('</table>')
    return out


def render_list(items, tag, site=False):
    """One list, each item (text, [subitems]); subitems nest one level as a plain ul.

    On the TV a numbered list is a table of two columns, the number and the text: the panel's text element gives a
    list a fixed 40 pixels for its markers, whatever the size of the letters, and at the TV's letter size a
    number does not fit in them (seen on a Steam Deck: "1." was cut off at the left)."""
    if tag == 'ol' and not site:
        out = ['<table cellspacing="0" cellpadding="0">']
        for number, (text, subs) in enumerate(items, 1):
            body = render_inline(text, site=site)
            if subs:
                body += '\n' + '\n'.join(render_list(subs, 'ul', site))
            out.append('<tr><td>%d.&nbsp;</td><td>%s</td></tr>' % (number, body))
        out.append('</table>')
        return out
    out = ['<%s>' % tag]
    for text, subs in items:
        body = render_inline(text, site=site)
        if subs:
            body += '\n' + '\n'.join(render_list(subs, 'ul', site))
        out.append('<li>%s</li>' % body)
    out.append('</%s>' % tag)
    return out


def render_blocks(text, folder=None, copied=None, site=False):
    """The blocks of a comment-free text as a list of lines, and the first heading text."""
    lines = text.split('\n')
    out = []
    title = None
    number = 0
    used = {}
    while number < len(lines):
        line = lines[number]
        if line.strip() == '':
            number += 1
            continue
        if RULE.match(line.strip()):
            out.append('<hr>')
            number += 1
            continue
        if FENCE.match(line):
            number += 1
            body = []
            while number < len(lines) and not FENCE.match(lines[number]):
                body.append(escape(lines[number]))
                number += 1
            number += 1
            out.append('<pre>%s</pre>' % '\n'.join(body))
            continue
        match = HEADING.match(line)
        if match:
            level = min(len(match.group(1)), 3)
            if title is None:
                title = match.group(2)
            if site:
                text = match.group(2)
                base = re.sub(r'[^A-Za-z0-9]+', '-', text.lower()).strip('-')
                used[base] = used.get(base, 0) + 1
                anchor = base if used[base] == 1 else '%s-%d' % (base, used[base])
                out.append('<h%d id="%s">%s</h%d>' % (level, anchor,
                                                      render_inline(text, folder, copied, True),
                                                      level))
            else:
                out.append('<h%d>%s</h%d>' % (level, render_inline(match.group(2), folder, copied),
                                              level))
            number += 1
            continue
        match = QUOTE.match(line)
        if match:
            if site:
                out.append('<blockquote><p>%s</p></blockquote>'
                           % render_inline(match.group(1), folder, copied, True))
            else:
                out.append('<p><i>%s</i></p>' % render_inline(match.group(1), folder, copied))
            number += 1
            continue
        if BULLET.match(line) or NUMBERED.match(line):
            tag = 'ul' if BULLET.match(line) else 'ol'
            items = []
            while number < len(lines):
                line = lines[number]
                item = BULLET.match(line) or NUMBERED.match(line)
                if item is None or (tag == 'ul') != bool(BULLET.match(line)):
                    break
                text, subs = item.group(1), []
                number += 1
                while number < len(lines):
                    sub = lines[number]
                    if sub.strip() == '':
                        break
                    sub_item = BULLET.match(sub.strip()) or NUMBERED.match(sub.strip())
                    if len(sub) - len(sub.lstrip()) < 2:
                        break
                    if sub_item is None:
                        text += ' ' + sub.strip()
                        number += 1
                    else:
                        subs.append([sub_item.group(1), []])
                        number += 1
                        while number < len(lines):
                            deep = lines[number]
                            if deep.strip() == '':
                                break
                            if len(deep) - len(deep.lstrip()) < 4:
                                break
                            deep_item = BULLET.match(deep.strip()) or NUMBERED.match(deep.strip())
                            if deep_item is None:
                                subs[-1][0] += ' ' + deep.strip()
                                number += 1
                            else:
                                break
                items.append([text, subs])
            out.extend(render_list(items, tag, site))
            continue
        if line.strip().startswith('|') or (
                number + 1 < len(lines) and is_separator_row(table_cells(lines[number + 1]))):
            table = []
            while number < len(lines):
                tl = lines[number]
                stripped = tl.strip()
                is_table_line = (
                    stripped.startswith('|')
                    or is_separator_row(table_cells(tl))
                    or (table and '|' in tl)
                    or (not table and number + 1 < len(lines)
                        and is_separator_row(table_cells(lines[number + 1])) and '|' in tl)
                )
                if is_table_line:
                    table.append(tl)
                    number += 1
                else:
                    break
            if table:
                out.extend(render_table(table, site))
            continue
        paragraph = [line.strip()]
        number += 1
        while number < len(lines):
            nxt = lines[number]
            if (nxt.strip() == '' or FENCE.match(nxt) or HEADING.match(nxt) or RULE.match(nxt.strip())
                    or QUOTE.match(nxt) or BULLET.match(nxt) or NUMBERED.match(nxt)
                    or nxt.strip().startswith('|')
                    or ('|' in nxt and number + 1 < len(lines)
                        and is_separator_row(table_cells(nxt)))):
                break
            paragraph.append(nxt.strip())
            number += 1
        out.append('<p>%s</p>' % render_inline(' '.join(paragraph), folder, copied, site))
    return out, title


def render_chapter(text, folder=None, copied=None, site=False):
    """(html, title) for one chapter: comments gone, every block on its own line."""
    blocks, title = render_blocks(strip_comments(text), folder, copied, site)
    return '\n'.join(blocks) + '\n', title


def nav_links(entries, position):
    """The nav's links: the chapter before, Contents, the chapter after; both ends miss one."""
    links = []
    if position > 0:
        links.append('<a href="%s">%s</a>' % (entries[position - 1]['file'],
                                              escape(entries[position - 1]['title'])))
    links.append('<a href="index.html">Contents</a>')
    if position + 1 < len(entries):
        links.append('<a href="%s">%s</a>' % (entries[position + 1]['file'],
                                              escape(entries[position + 1]['title'])))
    return '\n'.join(links)


def site_page(title, body, nav):
    return ('<!DOCTYPE html>\n'
            '<html lang="en">\n'
            '<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>%s \u00b7 Dosplex manual</title>\n'
            '<link rel="stylesheet" href="manual.css">\n'
            '</head>\n'
            '<body>\n'
            '<nav class="top">\n%s\n</nav>\n'
            '<main>\n%s</main>\n'
            '<nav class="top">\n%s\n</nav>\n'
            '</body>\n'
            '</html>\n' % (escape(title or ''), nav, body, nav))


def build_panel(source, out, strict):
    """The small subset of HTML 4 the Qt Quick Text item understands, and index.json."""
    chapters = []
    for path in sorted(source.iterdir()):
        match = CHAPTER.match(path.name)
        if path.is_file() and match:
            chapters.append((path, '%s-%s' % (match.group(1), match.group(2))))
    if not chapters:
        print('no chapter in %s' % source)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    written = set()
    index = []
    todos = 0
    for path, chapter in chapters:
        copied = set()
        text = path.read_text(encoding='utf-8')
        html, title = render_chapter(text, path.parent, copied)
        (out / (chapter + '.html')).write_text(html, encoding='utf-8')
        written.add(chapter + '.html')
        for name in sorted(copied):
            (out / name).write_bytes((path.parent / name).read_bytes())
        index.append({'file': chapter + '.html', 'chapter': chapter, 'title': title or ''})
        for number, line in enumerate(text.split('\n'), 1):
            if 'TODO' in strip_comments(line):
                todos += 1
                print('%s:%d: %s' % (path.name, number, line.strip()))
    for path in out.iterdir():
        if path.suffix == '.html' and path.name not in written:
            path.unlink()
    (out / 'index.json').write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if todos and strict:
        return 1
    return 0


def build_site(source, out, strict):
    """One full HTML5 page per chapter, index.html and manual.css, for the website."""
    chapters = []
    for path in sorted(source.iterdir()):
        match = CHAPTER.match(path.name)
        if path.is_file() and match:
            chapters.append((path, '%s-%s' % (match.group(1), match.group(2))))
    if not chapters:
        print('no chapter in %s' % source)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    written = set()
    pages = []
    entries = []
    todos = 0
    for path, chapter in chapters:
        copied = set()
        text = path.read_text(encoding='utf-8')
        html, title = render_chapter(text, path.parent, copied, True)
        entries.append({'file': chapter + '.html', 'title': title or ''})
        pages.append((chapter + '.html', html))
        written.add(chapter + '.html')
        for name in sorted(copied):
            (out / name).write_bytes((path.parent / name).read_bytes())
        for number, line in enumerate(text.split('\n'), 1):
            if 'TODO' in strip_comments(line):
                todos += 1
                print('%s:%d: %s' % (path.name, number, line.strip()),
                      file=sys.stderr)
    for position, (name, html) in enumerate(pages):
        (out / name).write_text(
            site_page(entries[position]['title'], html, nav_links(entries, position)),
            encoding='utf-8')
    (out / 'manual.css').write_text(SITE_CSS, encoding='utf-8')
    written.add('manual.css')
    (out / 'index.html').write_text(site_page(
        'Dosplex manual',
        '<h1>Dosplex manual</h1>\n<ol>\n%s\n</ol>' % '\n'.join(
            '<li><a href="%s">%s</a></li>' % (entry['file'], escape(entry['title']))
            for entry in entries),
        '<a href="#">Dosplex manual</a>'), encoding='utf-8')
    written.add('index.html')
    for path in out.iterdir():
        if path.suffix == '.html' and path.name not in written:
            path.unlink()
    if todos and strict:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='build-manual.py', description='Build the manual for the TV.')
    parser.add_argument('--source', type=Path, required=True, help='the folder of the chapters')
    parser.add_argument('--out', type=Path, required=True, help='the folder to write into')
    parser.add_argument('--target', choices=('panel', 'site'), default='panel',
                        help='panel: the TV pages and index.json; site: full HTML5 pages and index.html')
    parser.add_argument('--strict', action='store_true', help='a TODO outside a comment is an error')
    args = parser.parse_args(argv)
    if args.target == 'site':
        return build_site(args.source, args.out, args.strict)
    return build_panel(args.source, args.out, args.strict)


if __name__ == '__main__':
    sys.exit(main())
