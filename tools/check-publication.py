#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Fail closed on unexpected public files, personal details, credentials, history payloads and dangling references."""
import argparse
import ast
import bisect
from collections import Counter
import fnmatch
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import urllib.parse

if sys.version_info < (3, 10):
    raise SystemExit('check-publication.py needs Python 3.10 or newer')

CONTACT = 'roy@dosplex.org'
# CONTACT is the author address of the public Git commits and the contact on every public page (the mailbox exists).
# conduct@dosplex.org is the code-of-conduct contact in CODE_OF_CONDUCT.md (the mailbox exists).
RESERVED_ADDRESS = re.compile(rb'@(?:[A-Za-z0-9-]+\.)*(?:invalid|example|test)$|@example\.(?:com|org|net)$')
ALLOWED_EMAILS = {CONTACT, 'noreply@github.com', 'conduct@dosplex.org'}
# The license of the project: first-party files carry the SPDX identifier AGPL-3.0-or-later, LICENSE is the AGPL text,
# and nothing public may still offer the former noncommercial terms. (Commercial licenses by the rights holder
# are offered again since ADR-025, in LICENSING.md, so the words "commercial license" are no longer a marker.)
LICENSE_ID = b'AGPL-3.0-or-later'
SPDX_IDENTIFIER = re.compile(rb'SPDX-License-Identifier:[ \t]*([^\r\n*]*?)[ \t]*(?:\*/)?[ \t]*(?:\r?\n|$)')
# Written in parts so that this file does not match its own rule.
OLD_TERMS = re.compile(rb'(?i)poly' + b'form|COMMERCIAL' + b'-LICENSE|non' + b'commercial use')
LICENSE_MARKERS = (b'GNU AFFERO GENERAL PUBLIC LICENSE\n                       Version 3, 19 November 2007',
                   b'END OF TERMS AND CONDITIONS', b'How to Apply These Terms to Your New Programs')
EMAIL = re.compile(rb'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
# Characters of a key, token or password value, and the names that say a variable holds one:
# api_key, SERVICE_API_KEY, client_secret, access_token, password, Authorization, ...
TOKEN = rb'[A-Za-z0-9._~+/=\-]'
SECRET_NAME = rb'(?:(?:api|access|auth|private|secret|signing)[_-]?key|secret|token|passw(?:or)?d|passphrase|authorization|bearer)'
# NAME = "<20+ characters>" in code, or NAME: <20+ characters> in a config file, header or environment
# line, with an optional closing quote, `: type` annotation and escaped quotes. An unquoted value also
# needs a letter and a digit within its first 100 characters, because `password =
# self.password_entry.get_text` is an attribute chain and not a secret, while a real key has both.
# (The window is bounded so that a long run such as token=token=token=... cannot make the scan quadratic.)
# These patterns use [ \t] and never \s, so a match cannot reach into the next line and the line
# number of a finding is exact.
ASSIGNED_VALUE = (rb'(?:\\?["\']' + TOKEN + rb'{20,}'
                  rb'|(?=' + TOKEN + rb'{0,100}[0-9])(?=' + TOKEN + rb'{0,100}[A-Za-z])' + TOKEN + rb'{20,})')
RULES = {
    # A person's home folder. Not one: SteamOS's fixed account (deck), the two names the product itself makes
    # (guest, dosplex-library) and the build image's user (builder). Anything longer than these names still counts.
    'personal-home-path': rb'(?:/home/(?!(?:deck|guest|dosplex-library|builder)(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-]))|/Users/)[A-Za-z0-9_.-]+'
                          rb'|[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+',
    'private-ip': rb'\b(?:192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b',
    # PEM private keys, and the file minisign writes for its encrypted secret key (T-702): its comment line and its
    # key line, which starts with the base64 of "EdScB2". The brackets keep this line from matching itself.
    'private-key': (rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
                    rb'|minisign encrypted secret ke[y]|\bRWRTY0I[y][A-Za-z0-9+/]{60,}'),
    'access-token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})\b',
    'credential-url': rb'https?://[^\s/@:]+:[^\s/@]{4,}@',
    # Credentials without a vendor shape: any key, token or password name given a long value, and any
    # `Bearer <token>`.
    'secret-assignment': (rb'(?i)' + SECRET_NAME + rb'\\?["\']?(?:[ \t]*:[ \t]*[\w.\[\]|]+)?[ \t]*'
                          rb'(?:=>|:?={1,2}|:)[ \t]*' + ASSIGNED_VALUE),
    'bearer-token': rb'(?i)\bbearer[ \t]+' + TOKEN + rb'{20,}',
}
# The rules that look for credentials. --staged applies exactly these to the staged changes, for a
# pre-commit hook; the personal-detail rules and the private markers are left out because they guard
# what is published, not what is committed. 'high-entropy-literal' is not a regular expression, see
# looks_random().
SECRET_RULES = ('private-key', 'access-token', 'credential-url', 'secret-assignment', 'bearer-token',
                'high-entropy-literal')
# A single- or double-quoted literal of 32 or more key characters. Base64 padding may only end it, so
# KEY=value lines are not literals. Backticks are not quotes here: Markdown code spans are full of
# paths and generated names that contain long numbers.
QUOTED_LITERAL = re.compile(rb'''(["'])([A-Za-z0-9+/_.\-]{32,}={0,2})\\?\1''')
# Entropy limit in bits per character. Hex digests (checksums, UUIDs, git object ids) cannot exceed
# log2(16) = 4.0 bits per character however long they are, so they never get above the limit, and
# they are exempted explicitly as well (HASH_LIKE). Random keys land above it: measured over random
# draws, 32 characters average 4.3 bits (a-z0-9) to 4.6 (A-Za-z0-9, base64), 40 average 4.4 to 4.8,
# and 96% to 100% of them clear 4.0. A limit of 4.5 would find only about two thirds of random
# 32-character keys. Paths, file names and identifiers also reach 4.0 to 4.6 bits, so they are told
# apart by their structure (looks_random) and not by a higher limit.
ENTROPY_LIMIT = 4.0
HASH_LIKE = re.compile(rb'[0-9A-Fa-f]+(?:-[0-9A-Fa-f]+)*|sha(?:1|224|256|384|512)-[A-Za-z0-9+/]+={0,2}')
NAME_SEPARATORS = re.compile(rb'[/._\-]')
# A word of a path, file name or identifier: lower case, UPPER CASE or Capitalized/camelCase letters.
# The letters of a random key switch case far too often to fit.
NAME_WORD = re.compile(rb'(?:[a-z]+(?:[A-Z][a-z]+){0,3}|[A-Z]+|(?:[A-Z][a-z]+){1,3})')
# The reviewed allowlist: lines of "<path or glob> <rule> <reason>". It exempts a file from a secret
# rule, never a value. A missing file means there are no exemptions.
ALLOWLIST_FILE = 'tools/check-publication-allowlist.txt'
FINDINGS_PER_FILE = 20
FORBIDDEN = re.compile(r'(^|/)(private|plans|evidence|research|\.ssh|\.env)(/|$)|\.(o|ko|a|so|wav|mkv|mp4|deb|ddeb|pyc)$')

# Reference closure. Whatever a published file loads, runs, sources, imports or links to
# must itself be published, or be listed with a reason in the reviewed waiver file.
# References are resolved inside the published tree only; nothing outside it is consulted.
WAIVER_FILE = 'tools/publication-waivers.json'
# Top-level directories of the development repository. Directories that exist in the
# published tree are added to this set when the check runs.
REPO_DIRS = ('tests', 'packaging', 'docs', 'scripts', 'guest', 'compositor', 'tools',
             'compatibility', 'prototype', 'vm')
# The manifests list paths by design, and the waiver and allowlist files quote what they excuse.
CLOSURE_EXEMPT = {'.publication-files', '.publication-images.json', WAIVER_FILE, ALLOWLIST_FILE}
# Project scripts are installed flat, so a bare script name resolves by name anywhere in the tree.
SCRIPT_NAME = r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.(?:py|sh|qml)'
SCRIPT_REFERENCE = re.compile(r'(?<=/)(' + SCRIPT_NAME + r')(?![\w-]|\.\w)')
SCRIPT_REFERENCE_FILES = ('.py', '.sh', '.qml', '.service', '.rules', '.c', '.h')
# A shell command that is handed a bare script name: source helper.sh, bash helper.sh, exec helper.py.
SHELL_COMMAND = re.compile(r'(?:^|[;&|(`]|\s)(?:source|\.|exec|bash|sh|python3?)\s+(?:-[\w-]+\s+)*["\']?(' + SCRIPT_NAME + r')(?![\w/-]|\.\w)')
UNIT_REFERENCE = re.compile(r'(?<![\w@.-])(dosplex-[A-Za-z0-9_.-]*[A-Za-z0-9_]\.service)(?![\w@-])')
# Written in two parts so that this file does not match its own rule.
PRIVATE_DIR = 'priv' + 'ate'
PRIVATE_REFERENCE = re.compile(r'(?<![\w.~-])' + PRIVATE_DIR + '/')
MD_LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:"[^"]*"|\'[^\']*\'))?\s*\)')
MD_DEFINITION = re.compile(r'^ {0,3}\[(?!\^)[^\]\n]+\]:\s*<?([^\s>]+)')
MD_CODE_SPAN = re.compile(r'`[^`\n]*`')
URL_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')
GLOB = re.compile(r'[*?\[]')

def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)

# Secret scan. Everything here reports a file, a line and a rule name, never the text that matched.

def shannon(text):
    """Entropy of the characters of text, in bits per character."""
    return -sum(n / len(text) * math.log2(n / len(text)) for n in Counter(text).values())

def has_sequence(text, length=8):
    """Whether text holds an ascending run such as abcdefgh or 01234567 (alphabet tables)."""
    run = 1
    for before, after in zip(text, text[1:]):
        run = run + 1 if after == before + 1 else 1
        if run >= length:
            return True
    return False

def looks_random(literal):
    """Whether a quoted literal of 32 or more key characters looks like a key or token.

    Digests, names and paths are told apart from keys by what they are made of: a digest is hex
    (or an integrity hash), a key mixes letters and digits, an alphabet table is a run, and a file
    name, path or identifier is mostly words. What is left is judged by its Shannon entropy.
    """
    if HASH_LIKE.fullmatch(literal):
        return False
    if not (re.search(rb'[A-Za-z]', literal) and re.search(rb'[0-9]', literal)):
        return False
    if has_sequence(literal):
        return False
    parts = NAME_SEPARATORS.split(literal)
    if 2 * sum(len(p) for p in parts if 3 <= len(p) <= 16 and NAME_WORD.fullmatch(p)) > sum(len(p) for p in parts):
        return False  # a longer run of letters is more likely random than a word
    return shannon(literal) > ENTROPY_LIMIT

def secret_hits(data):
    """(line number, rule) for every credential-shaped string in data, as sorted pairs.

    Lines are numbered from 1. The matched text itself is never returned.
    """
    starts, hits = None, set()
    def line_at(offset):
        nonlocal starts
        if starts is None:
            starts = [0] + [m.end() for m in re.finditer(rb'\n', data)]
        return bisect.bisect_right(starts, offset)
    for rule in SECRET_RULES:
        if rule in RULES:
            hits.update((line_at(m.start()), rule) for m in re.finditer(RULES[rule], data))
    hits.update((line_at(m.start()), 'high-entropy-literal')
                for m in QUOTED_LITERAL.finditer(data) if looks_random(m.group(2)))
    return sorted(hits)

def parse_allowlist(data):
    """The reviewed allowlist as ([(path glob, rule)], problems), from the bytes of the file.

    Each line is "<path or glob> <rule> <reason>". An entry without a reason, with an unknown
    rule, or with a path outside the repository is a problem, and a problem fails the check.
    """
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return [], [f'{ALLOWLIST_FILE}: cannot be read as text']
    entries, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split(None, 2)
        if len(fields) < 3:
            problems.append(f'{ALLOWLIST_FILE}:{number}: an entry needs a path, a rule and a reason')
        elif fields[1] not in SECRET_RULES:
            problems.append(f'{ALLOWLIST_FILE}:{number}: unknown rule (expected one of {", ".join(SECRET_RULES)})')
        elif fields[0].startswith('/') or '..' in fields[0].split('/'):
            problems.append(f'{ALLOWLIST_FILE}:{number}: the path must stay inside the repository')
        else:
            entries.append((fields[0], fields[1]))
    return entries, problems

def read_allowlist(root):
    """The allowlist of the tree at root, as parse_allowlist returns it. A missing file allows nothing."""
    path = Path(root) / ALLOWLIST_FILE
    if not path.is_file():
        return [], []
    try:
        return parse_allowlist(path.read_bytes())
    except OSError:
        return [], [f'{ALLOWLIST_FILE}: cannot be read']

def staged_allowlist(root):
    """The allowlist as the staged changes would commit it, so a commit is judged by its own content.

    An entry that is only in the working tree does not count, and an entry that is staged together
    with the file it excuses does. No staged allowlist allows nothing.
    """
    shown = subprocess.run(['git', '-C', str(root), 'cat-file', 'blob', ':' + ALLOWLIST_FILE], capture_output=True)
    return parse_allowlist(shown.stdout) if shown.returncode == 0 else ([], [])

def secret_errors(label, path, hits, allowlist):
    """Diagnostics for (line, rule) hits in one file, minus the allowlisted ones and capped per file."""
    hits = [(line, rule) for line, rule in hits
            if not (path and any(r == rule and fnmatch.fnmatchcase(path, glob) for glob, r in allowlist))]
    errors = [f'{label}:{line}: {rule}' for line, rule in hits[:FINDINGS_PER_FILE]]
    if len(hits) > FINDINGS_PER_FILE:
        errors.append(f'{label}: {len(hits) - FINDINGS_PER_FILE} more secret findings')
    return errors

GIT_ESCAPES = {b'a': 7, b'b': 8, b'f': 12, b'n': 10, b'r': 13, b't': 9, b'v': 11, b'"': 34, b'\\': 92}
HUNK = re.compile(rb'@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

def diff_path(header):
    """The file named by a '+++ b/<path>' header of git diff, or None for /dev/null."""
    name = header.rstrip(b'\t')  # git marks names that contain spaces with a trailing tab
    if name.startswith(b'"') and name.endswith(b'"'):  # C-style quoting for tabs, quotes, backslashes
        name = re.sub(rb'\\([0-7]{3}|[abfnrtv"\\])',
                      lambda m: bytes([int(m[1], 8)]) if m[1][:1].isdigit() else bytes([GIT_ESCAPES[m[1]]]),
                      name[1:-1])
    return None if name == b'/dev/null' else name[2:].decode('utf-8', errors='replace')

def added_lines(diff):
    """{path: [(line number, line bytes)]} for the lines a `git diff -U0` text adds.

    Hunks are read by their line counts, so an added line that looks like a diff header ('++ x'
    shows up as '+++ x') cannot be taken for one.
    """
    added, path, rows, index = {}, None, diff.split(b'\n'), 0
    while index < len(rows):
        row = rows[index]
        index += 1
        if row.startswith(b'+++ '):
            path = diff_path(row[4:])
        elif row.startswith(b'@@ '):
            hunk = HUNK.match(row)
            if not hunk:
                raise ValueError('unreadable hunk header in the staged diff')
            removed = int(hunk[1]) if hunk[1] is not None else 1
            wanted = int(hunk[3]) if hunk[3] is not None else 1
            number = int(hunk[2])
            while (removed or wanted) and index < len(rows):
                row = rows[index]
                index += 1
                if row.startswith(b'-'):
                    removed -= 1
                elif row.startswith(b'+'):
                    wanted -= 1
                    if path is not None:
                        added.setdefault(path, []).append((number, row[1:]))
                    number += 1
    return added

def staged_changes(root):
    """(added lines by file, number of binary files) of the staged changes, read from the index and not
    from the working tree. A binary file has no lines to scan; it is counted so that the report says so."""
    diff = subprocess.run(['git', '-C', str(root), '-c', 'core.quotepath=off', 'diff', '--cached', '-U0',
                           '--no-color', '--no-ext-diff', '--no-textconv', '--no-renames',
                           '--src-prefix=a/', '--dst-prefix=b/'], capture_output=True)
    if diff.returncode != 0:
        raise ValueError('git could not produce the staged diff')
    return added_lines(diff.stdout), sum(row.startswith(b'Binary files ') for row in diff.stdout.split(b'\n'))

def check_staged(root):
    """Secret scan of the staged changes, for a pre-commit hook. Anything that goes wrong blocks."""
    try:
        allowlist, problems = staged_allowlist(root)
        added, binaries = staged_changes(root)
    except (ValueError, OSError) as problem:
        print(f'Secret scan blocked the commit: {problem}.', file=sys.stderr)  # fail closed
        return 1
    findings = []
    for path in sorted(added):
        # No rule reaches across a line, so the added lines of a file can be scanned as one text.
        numbers = [number for number, _ in added[path]]
        joined = b'\n'.join(text for _, text in added[path])
        hits = sorted({(numbers[line - 1], rule) for line, rule in secret_hits(joined)})
        findings += secret_errors(path, path, hits, allowlist)
    if problems or findings:
        print('\n'.join(problems + findings), file=sys.stderr)
        if findings:
            print('Secret scan: the staged changes look like they contain credentials (see the lines above; '
                  'values are never printed).\nRemove them and keep keys in the environment or a keyring, and '
                  f'rotate any that were real. A reviewed false positive goes into {ALLOWLIST_FILE}, by path '
                  'and rule, never by value.', file=sys.stderr)
        return 1
    print(f'Secret scan passed: {sum(map(len, added.values()))} added line(s) in {len(added)} staged file(s)'
          + (f'; {binaries} binary file(s) not scanned' if binaries else ''))
    return 0

class Tree:
    """The published files, with the lookups the reference checks need."""
    def __init__(self, names, installed=()):
        self.names = set(names)
        # Where the published files lie in an installed release (the destinations of the release layout): a page
        # or a program that names a tool by its place in a release means the installed file, which is published
        # under the place the layout gives as its source.
        self.installed = set(installed)
        self.dirs = {''}
        for name in self.names:
            parent = posixpath.dirname(name)
            while parent:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        self.basenames = {posixpath.basename(n) for n in self.names}
        self.top = set(REPO_DIRS) | {n.split('/')[0] for n in self.names if '/' in n}
        body = r'((?:%s)/[\w./@+*?-]*)' % '|'.join(re.escape(d) for d in sorted(self.top))
        # Plainly written (optionally after ./), or after a shell/format variable or a closing bracket.
        self.path_patterns = [re.compile(r'(?<![\w.~/-])(?:\./)?' + body), re.compile(r'(?:\$\w+|[})])/' + body)]

    def has_path(self, token):
        """A repo-relative file, directory or glob that is published."""
        token = token.rstrip('.,;:')
        if token.endswith('/'):
            token = token[:-1]
        if not token:
            return True
        if GLOB.search(token):
            return any(fnmatch.fnmatchcase(n, token) for n in self.names | self.dirs)
        token = posixpath.normpath(token)
        return not token.startswith('..') and (token in self.names or token in self.dirs or token in self.installed)

    def has_link(self, source, target):
        """A Markdown link target, relative to the linking file unless it starts with a slash."""
        if target.startswith('/'):
            joined = posixpath.normpath(target.lstrip('/') or '.')
        else:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
        joined = '' if joined == '.' else joined
        return not joined.startswith('..') and (joined in self.names or joined in self.dirs)

def markdown_lines(text):
    """(line number, line, inside a code fence) for Markdown text."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r' {0,3}(```|~~~)', line):
            fenced = not fenced
            yield number, line, True
        else:
            yield number, line, fenced

def markdown_links(text):
    """Relative link targets in Markdown prose, as (line number, target)."""
    for number, line, fenced in markdown_lines(text):
        if not fenced:
            line = MD_CODE_SPAN.sub('', line)
            for match in [*MD_LINK.finditer(line), *MD_DEFINITION.finditer(line)]:
                target = match.group(1)
                if not target.startswith('#') and not URL_SCHEME.match(target):
                    yield number, urllib.parse.unquote(re.split(r'[#?]', target, 1)[0])

def path_tokens(tree, line):
    """Repo-relative paths mentioned in a line."""
    for pattern in tree.path_patterns:
        for match in pattern.finditer(line):
            yield match.group(1).rstrip('.,;:')

def path_roots(module):
    """Variables assigned from an expression that involves __file__, i.e. script-relative roots."""
    roots, changed = set(), True
    while changed:
        changed = False
        for node in ast.walk(module):
            if isinstance(node, ast.Assign):
                used = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if '__file__' in used or used & roots:
                    for target in node.targets:
                        names = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                        for name in names:
                            if isinstance(name, ast.Name) and name.id not in roots:
                                roots.add(name.id)
                                changed = True
    return roots

def python_references(tree, name, text, third_party):
    """Imports, __file__.with_name() siblings and joined path constants in Python code.

    Yields (line, kind, target). An import of a module that only the waiver file allows has
    kind 'third-party', so the caller can tell which waiver entries are still in use. A file
    that does not parse yields a single 'unparsable' item.
    """
    try:
        module = ast.parse(text, filename=name)
    except (SyntaxError, ValueError):
        yield 1, 'unparsable', name
        return
    here = posixpath.dirname(name)
    roots = path_roots(module)
    def script_relative(expression):
        used = {n.id for n in ast.walk(expression) if isinstance(n, ast.Name)}
        return '__file__' in used or bool(used & roots)
    def imported(top, line, base):
        local = posixpath.join(base, top + '.py') in tree.names or posixpath.join(base, top, '__init__.py') in tree.names
        if top not in sys.stdlib_module_names and not local:
            yield line, 'third-party' if top in third_party else 'import', top
    def joined(root, parts, line):
        """Path built by joining string constants onto a script-relative root."""
        run = []
        for part in parts:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                break
            run.append(part.value.strip('/'))
        if not run or not script_relative(root):
            return
        token = posixpath.normpath('/'.join(run))
        if token.split('/')[0] == PRIVATE_DIR:
            # Built from parts, so the plain-text rule does not see it: the file would need the development tree.
            yield line, 'private', token
        elif token.split('/')[0] in tree.top:
            if not tree.has_path(token):
                yield line, 'path', token
        elif len(run) == 1 and re.fullmatch(SCRIPT_NAME, run[0]) and run[0] not in tree.basenames:
            yield line, 'sibling', run[0]
    inner = {id(n.left) for n in ast.walk(module) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield from imported(alias.name.split('.')[0], node.lineno, here)
        elif isinstance(node, ast.ImportFrom):
            base = here
            for _ in range(max(node.level - 1, 0)):
                base = posixpath.dirname(base)
            if node.module:
                yield from imported(node.module.split('.')[0], node.lineno, base)
            elif node.level:
                for alias in node.names:
                    yield from imported(alias.name, node.lineno, base)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner:
            parts, root = [], node
            while isinstance(root, ast.BinOp) and isinstance(root.op, ast.Div):
                parts.append(root.right)
                root = root.left
            yield from joined(root, parts[::-1], node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            function, arguments = node.func, node.args
            if function.attr == 'with_name' and arguments:
                sibling = arguments[0]
                if (isinstance(sibling, ast.Constant) and isinstance(sibling.value, str) and
                        re.fullmatch(SCRIPT_NAME, sibling.value) and '__file__' in
                        {n.id for n in ast.walk(function.value) if isinstance(n, ast.Name)}):
                    target = posixpath.join(here, sibling.value)
                    if target not in tree.names:
                        yield node.lineno, 'sibling', target
            elif function.attr == 'joinpath':
                yield from joined(function.value, arguments, node.lineno)
            elif function.attr == 'join' and arguments and (
                    isinstance(function.value, ast.Attribute) and function.value.attr == 'path' or
                    isinstance(function.value, ast.Name) and function.value.id == 'posixpath'):
                yield from joined(arguments[0], arguments[1:], node.lineno)

def read_waivers(data):
    """Validate the parsed waiver file. Returns (reference waivers, third-party modules, problems)."""
    if data is None:
        return [], {}, []
    problems = []
    if not isinstance(data, dict) or set(data) - {'references', 'third_party_modules'}:
        return [], {}, [f'{WAIVER_FILE}: expected an object with references and third_party_modules']
    entries = data.get('references', [])
    modules = data.get('third_party_modules', {})
    if not isinstance(entries, list):
        problems.append(f'{WAIVER_FILE}: references must be a list')
        entries = []
    if not isinstance(modules, dict) or not all(isinstance(v, str) and v.strip() for v in modules.values()):
        problems.append(f'{WAIVER_FILE}: every third-party module needs a reason')
        modules = {}
    valid = []
    for index, entry in enumerate(entries, 1):
        if (isinstance(entry, dict) and set(entry) <= {'file', 'target', 'reason'} and
                all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('target', 'reason')) and
                isinstance(entry.get('file', ''), str)):
            valid.append(entry)
        else:
            problems.append(f'{WAIVER_FILE}: reference waiver {index} needs a target and a reason')
    return valid, modules, problems

RELEASE_LAYOUT = 'packaging/release-build/layout.txt'


def installed_paths(names, texts):
    """The destinations of the release layout whose source is a published file: `destination  source` per line.
    Only when the layout itself is published; a destination whose source is not published resolves nothing."""
    found = set()
    for line in texts.get(RELEASE_LAYOUT, '').splitlines():
        fields = line.split()
        if len(fields) == 2 and not line.startswith('#') and fields[1] in names:
            found.add(posixpath.normpath(fields[0]))
    return found


def closure_errors(names, texts, waivers):
    """Diagnostics for references that do not resolve inside the published tree.

    names: every published path. texts: path -> text for the published text files.
    waivers: the parsed waiver file, or None. Diagnostics give file, line and the reference
    as written in the published file; they never quote anything else.
    """
    tree = Tree(names, installed_paths(names, texts))
    entries, third_party, errors = read_waivers(waivers)
    modules_used, unresolved = set(), []
    for name in sorted(texts):
        if name in CLOSURE_EXEMPT:
            continue
        text = texts[name]
        found = set()
        markdown = name.endswith('.md')
        lines = markdown_lines(text) if markdown else ((n, l, True) for n, l in enumerate(text.splitlines(), 1))
        for number, line, literal in lines:
            if PRIVATE_REFERENCE.search(line):
                errors.append(f'{name}:{number}: reference into the private directory is not allowed in a published file')
            if name == '.gitignore':
                continue  # Ignore patterns may name things that do not exist.
            if not literal:
                line = MD_LINK.sub('[link]()', line)  # Link targets are checked as links below.
            tokens = set(path_tokens(tree, line))
            found.update((number, 'path', t) for t in tokens if not tree.has_path(t))
            if name.endswith(SCRIPT_REFERENCE_FILES):
                scripts = [m.group(1) for m in SCRIPT_REFERENCE.finditer(line)]
                if name.endswith('.sh'):
                    scripts += [m.group(1) for m in SHELL_COMMAND.finditer(line)]
                found.update((number, 'script', script) for script in scripts
                             if script not in tree.basenames and
                             not any(posixpath.basename(t) == script for t in tokens))
            found.update((number, 'unit', m.group(1)) for m in UNIT_REFERENCE.finditer(line)
                         if m.group(1) not in tree.basenames)
        if markdown:
            found.update((number, 'link', t) for number, t in markdown_links(text) if not tree.has_link(name, t))
        if name.endswith('.py'):
            for number, kind, target in python_references(tree, name, text, third_party):
                if kind == 'third-party':
                    modules_used.add(target)
                elif kind == 'unparsable':
                    errors.append(f'{name}: not valid Python, so its imports and paths cannot be checked')
                elif kind == 'private':
                    errors.append(f'{name}:{number}: reference into the private directory is not allowed in a published file')
                else:
                    found.add((number, kind, target))
        unresolved += [(name, number, kind, target) for number, kind, target in sorted(found)]
    entries_used = set()
    for name, number, kind, target in unresolved:
        for index, entry in enumerate(entries):
            if entry['target'] == target and entry.get('file', name) == name:
                entries_used.add(index)
                break
        else:
            errors.append(f'{name}:{number}: unresolved {kind} reference: {target}')
    for index, entry in enumerate(entries):
        if index not in entries_used:
            where = f' in {entry["file"]}' if entry.get('file') else ''
            errors.append(f'{WAIVER_FILE}: stale waiver, nothing references {entry["target"]}{where}')
    for module in sorted(set(third_party) - modules_used):
        errors.append(f'{WAIVER_FILE}: stale third-party module, nothing imports {module}')
    return errors

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path.cwd())
    ap.add_argument('--history', action='store_true')
    ap.add_argument('--private-markers', type=Path)
    ap.add_argument('--staged', action='store_true',
                    help='scan only the lines added by the staged changes (git diff --cached -U0) for '
                         'credentials; meant for a pre-commit hook')
    args = ap.parse_args()
    root = args.root.resolve()
    if args.staged:
        if args.history or args.private_markers:
            ap.error('--staged cannot be combined with --history or --private-markers')
        return check_staged(root)
    allowlist, errors = read_allowlist(root)  # A malformed allowlist is an error of its own.
    allowed = set((root / '.publication-files').read_text().splitlines())
    if any(not x or x.startswith('/') or '..' in Path(x).parts for x in allowed):
        raise SystemExit('Invalid publication manifest')
    images = json.loads((root / '.publication-images.json').read_text())
    markers = json.loads(args.private_markers.read_text()) if args.private_markers else []
    def scan(label, data, path=None):
        # Diagnostics identify a file/rule, never print matched sensitive values.
        for name, pattern in RULES.items():
            if name not in SECRET_RULES and re.search(pattern, data): errors.append(f'{label}: {name}')
        errors.extend(secret_errors(label, path, secret_hits(data), allowlist))
        for address in EMAIL.findall(data):
            # Names under the domains reserved for examples and tests (RFC 2606) are nobody's address.
            if address.decode(errors='replace') not in ALLOWED_EMAILS and not RESERVED_ADDRESS.search(address):
                errors.append(f'{label}: unapproved email')
        if any(m.lower().encode() in data.lower() for m in markers):
            errors.append(f'{label}: private marker')
        if label in files:
            for identifier in SPDX_IDENTIFIER.findall(b'\n'.join(data.split(b'\n')[:30])):
                if identifier != LICENSE_ID:
                    errors.append(f'{label}: SPDX-License-Identifier is not AGPL-3.0-or-later')
            if OLD_TERMS.search(data):
                errors.append(f'{label}: mentions the former license terms')
    try:
        files = set(git(root, 'ls-files', '-z').decode().rstrip('\0').split('\0'))
    except (subprocess.CalledProcessError, OSError):  # Not a git checkout, or no git installed.
        files = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    if files != allowed:
        errors.append('File allowlist mismatch: ' + repr(sorted(files ^ allowed)))
    present, texts = set(), {}
    if 'LICENSE' in files and (root / 'LICENSE').is_file():
        if not all(marker in (root / 'LICENSE').read_bytes() for marker in LICENSE_MARKERS):
            errors.append('LICENSE: is not the GNU AGPL v3.0 text')
    for name in sorted(files):
        path = root / name
        if FORBIDDEN.search(name): errors.append(f'{name}: forbidden path')
        if path.is_symlink():
            errors.append(f'{name}: symlink not allowed'); continue
        if not path.is_file():
            errors.append(f'{name}: missing file'); continue
        present.add(name)
        data = path.read_bytes()
        if name in images:
            if hashlib.sha256(data).hexdigest() != images[name]:
                errors.append(f'{name}: image requires visual review and new approved hash')
        elif b'\0' in data:
            errors.append(f'{name}: unexpected binary')
        else:
            scan(name, data, name)
            texts[name] = data.decode('utf-8', errors='replace')
    waivers = None
    if (root / WAIVER_FILE).is_file():
        try:
            waivers = json.loads((root / WAIVER_FILE).read_text())
        except ValueError:
            errors.append(f'{WAIVER_FILE}: not valid JSON')
    errors += closure_errors(present, texts, waivers)
    if args.history:
        scan('commit metadata/messages', git(root, 'log', '--all', '--format=%an <%ae>%n%cn <%ce>%n%B'))
        inventory = git(root, 'rev-list', '--all', '--objects').decode().splitlines()
        proc = subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        for line in inventory:
            oid, _, name = line.partition(' ')
            proc.stdin.write((oid+'\n').encode()); proc.stdin.flush()
            hdr = proc.stdout.readline().decode().split()
            data = proc.stdout.read(int(hdr[2])); proc.stdout.read(1)
            if hdr[1] != 'blob': continue
            if FORBIDDEN.search(name): errors.append(f'history {name}: forbidden path')
            if b'\0' in data:
                if hashlib.sha256(data).hexdigest() not in images.values():
                    errors.append(f'history {name}: unapproved binary')
            else: scan('history '+name, data, name)
        proc.stdin.close(); proc.wait()
    if errors:
        print('\n'.join(sorted(set(errors))), file=sys.stderr)
        return 1
    print(f'Publication check passed: {len(files)} allowed files' + ('; reachable history checked' if args.history else ''))
    return 0

if __name__ == '__main__':
    sys.exit(main())
