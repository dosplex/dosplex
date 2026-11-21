#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Generate THIRD-PARTY-NOTICES.md for a Dosplex release payload from its SBOM.

  generate-notices.py SBOM [-o FILE] [--rights-holder-file FILE] [--strict]
  generate-notices.py SBOM --check NOTICES [--strict]

SBOM is the sbom/dosplex-sbom.json of a payload (packaging/release-build/README.md describes the format). The first form
writes the notices to FILE, or to standard output. The second form reads an existing notices file and lists what the
SBOM requires and the file lacks, and what the file states that the SBOM does not support. Exit status 0 means done or
matching; 1 means problems (an unreadable or foreign SBOM, notices that do not match, or, with --strict, decisions that
are still open); 2 means a usage error. Standard library only.

Everything the notices state about components comes from the SBOM:

  - every bundled third-party component (a file of origin "upstream"): name, version, files, license, license text,
    the URL of the source archive and its SHA-256;
  - data and code of other packages that is compiled into files of the payload (the SBOM's "includes": hwdata and
    wayland-protocols from the build image, snapshots of subprojects such as vkroots and glm from a source archive),
    with its license as the SBOM states it, its license text when one ships and, for a source archive, its URL and
    SHA-256;
  - the libraries and the Python interpreter and modules that the payload expects the host to provide, listed as such;
  - license obligations that need a statement of their own (libfuse, LGPL-2.1).

A component without a license, a license text or a source is not an error here (tools/check-sbom.py enforces
completeness of the records): it becomes a line marked OWNER DECISION REQUIRED, and --strict refuses to write a file
that still has one. This tool never writes a license text of its own; the texts are the files under licenses/.

The rights holder's name is read from legal/RIGHTS_HOLDER, the one place it is written.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
RIGHTS_HOLDER_FILE = ROOT / 'legal' / 'RIGHTS_HOLDER'
NOTICES_PATH = 'licenses/dosplex-THIRD-PARTY-NOTICES.md'
FORMAT = 'dosplex-sbom'
FORMAT_VERSION = 1

# The statement about the controller report layouts. THIRD-PARTY-NOTICES.md in the repository carries the same
# paragraph (tests/notices-test.py keeps the two equal).
CONTROLLER_LAYOUT_NOTE = (
    'The controller report layouts that Dosplex reads (the byte offsets and bit masks of the HID reports of the '
    'supported controllers) were derived from public Linux kernel documentation. No code was copied from the kernel '
    'sources.')

DECISION = '- **OWNER DECISION REQUIRED:** '
LIBFUSE_SONAME = re.compile(r'libfuse3?\.so(\.|$)')
LIBFUSE_NAMES = {'libfuse', 'libfuse3', 'fuse', 'fuse3'}
LIBFUSE_PACKAGES = LIBFUSE_NAMES | {'fuse2'}


class NoticeError(Exception):
    """The SBOM or the rights holder file cannot be used."""


# --- Reading the inputs --------------------------------------------------------------------------------------

def read_rights_holder(path=RIGHTS_HOLDER_FILE):
    """The name in the rights holder file: exactly one non-empty line."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError as error:
        raise NoticeError('cannot read the rights holder file %s: %s' % (path, error.strerror or error))
    names = [line.strip() for line in lines if line.strip()]
    if len(names) != 1 or any(ord(character) < 32 for character in names[0]):
        raise NoticeError('%s must hold exactly one line, the name of the rights holder' % path)
    return names[0]


def validate(sbom):
    """The SBOM itself when it has the shape this tool reads, else NoticeError."""
    if not isinstance(sbom, dict) or sbom.get('format') != FORMAT or sbom.get('format_version') != FORMAT_VERSION:
        raise NoticeError('not a %s version %d file' % (FORMAT, FORMAT_VERSION))
    host = sbom.get('host_provided')
    if (not isinstance(sbom.get('package'), dict) or not isinstance(sbom.get('files'), list)
            or not isinstance(host, dict) or not isinstance(host.get('libraries'), list)
            or not isinstance(host.get('python'), list)):
        raise NoticeError('the SBOM needs package, files and host_provided with libraries and python')
    return sbom


def load_sbom(path):
    try:
        return validate(json.loads(Path(path).read_text(encoding='utf-8')))
    except (OSError, ValueError) as error:
        raise NoticeError('cannot read the SBOM %s: %s' % (path, error))


# --- The facts the notices state -----------------------------------------------------------------------------

def _text(value, default):
    return value.strip() if isinstance(value, str) and value.strip() else default


def _unique(values):
    seen, result = set(), []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _paths(items):
    return ', '.join('`%s`' % path for path in sorted(items))


def _cell(value):
    return _text(value, 'not recorded').replace('|', '\\|')


def _records(sbom):
    return [record for record in sbom['files'] if isinstance(record, dict) and isinstance(record.get('path'), str)]


def _by_name_and_version(pairs):
    """{(name, version): [(path, item)]} for (path, item) pairs, where item is a dict with a name and a version."""
    groups = {}
    for path, item in pairs:
        key = (_text(item.get('name'), '(unnamed)'), _text(item.get('version'), 'unknown version'))
        groups.setdefault(key, []).append((path, item))
    return groups


def bundled_components(sbom):
    """The third-party components shipped in the payload, sorted by name and version.

    A component is what the SBOM records as a file of origin "upstream"; the license texts of such files are not
    components of their own."""
    groups = _by_name_and_version((record['path'], record) for record in _records(sbom)
                                  if record.get('origin') == 'upstream' and record.get('kind') != 'license')
    components = []
    for (name, version), members in sorted(groups.items()):
        compiled = [item for _, record in members for item in record.get('includes') or [] if isinstance(item, dict)]
        components.append({
            'name': name, 'version': version,
            'files': sorted(path for path, _ in members),
            'licenses': _unique(record.get('license') for _, record in members),
            'license_texts': _unique(text for _, record in members for text in _strings(record.get('license_files'))),
            'urls': _unique(record.get('source_url') for _, record in members),
            'hashes': _unique(record.get('source_sha256') for _, record in members),
            'includes': sorted('%s %s' % key for key in _by_name_and_version(('', item) for item in compiled)),
        })
    return components


def compiled_data(sbom):
    """Data of other packages that is compiled into payload files (the SBOM's "includes"), sorted."""
    groups = _by_name_and_version(
        (record['path'], item) for record in _records(sbom)
        for item in (record['includes'] if isinstance(record.get('includes'), list) else []) if isinstance(item, dict))
    return [{
        'name': name, 'version': version,
        'carriers': sorted(set(path for path, _ in members)),
        'licenses': _unique(item.get('license') for _, item in members),
        'license_texts': _unique(text for _, item in members for text in _strings(item.get('license_files'))),
        'notes': _unique(item.get('note') for _, item in members),
        'urls': _unique(item.get('source_url') for _, item in members),
        'hashes': _unique(item.get('source_sha256') for _, item in members),
    } for (name, version), members in sorted(groups.items())]


def host_libraries(sbom):
    return sorted((item for item in sbom['host_provided']['libraries']
                   if isinstance(item, dict) and isinstance(item.get('soname'), str)),
                  key=lambda item: (item['soname'], _text(item.get('abi'), '')))


def _library_name(item):
    """The soname, and the ABI after it when it is not the usual 64-bit one (the libraries of 32-bit files)."""
    abi = _text(item.get('abi'), '')
    return item['soname'] + (' (%s)' % abi if abi else '')


def host_python(sbom):
    return sorted((item for item in sbom['host_provided']['python']
                   if isinstance(item, dict) and isinstance(item.get('name'), str)),
                  key=lambda item: (item.get('kind') != 'interpreter', item['name']))


# --- The entries ---------------------------------------------------------------------------------------------
# An entry is a block of lines: a heading and its bullets, a table row, or a list line. The document is made of
# entries, and the check looks for each of them, word for word.

def component_entry(component):
    name, version = component['name'], component['version']
    lines = ['### %s %s' % (name, version), '- Files: ' + _paths(component['files'])]
    if component['licenses']:
        lines.append('- License: ' + ', '.join(component['licenses']))
    else:
        lines += ['- License: not recorded in the SBOM',
                  DECISION + 'The SBOM records no license for %s %s.' % (name, version)]
    if component['license_texts']:
        lines.append('- License text: ' + _paths(component['license_texts']))
    else:
        lines += ['- License text: none ships with this release',
                  DECISION + 'No license text ships for %s %s.' % (name, version)]
    if component['urls']:
        lines += ['- Source: ' + url for url in component['urls']]
    else:
        lines += ['- Source: not recorded in the SBOM',
                  DECISION + 'The SBOM records no source for %s %s.' % (name, version)]
    lines += ['- Source SHA-256: `%s`' % digest for digest in component['hashes']]
    if component['includes']:
        lines.append('- Also compiled in: %s (see "Data compiled into bundled files")'
                     % ', '.join(component['includes']))
    return '\n'.join(lines)


def compiled_entry(data, snapshot):
    name, version = data['name'], data['version']
    lines = ['### %s %s' % (name, version), '- Compiled into: ' + _paths(data['carriers'])]
    lines += ['- SBOM note: ' + note for note in data['notes']]
    lines.append('- License (as recorded in the SBOM): ' + (', '.join(data['licenses']) or 'not recorded'))
    lines.append('- License text: ' + (_paths(data['license_texts']) if data['license_texts']
                                       else 'none ships with this release'))
    if data['urls']:
        # Code from a source archive (a subproject snapshot), not from a package of the image.
        lines += ['- Source: ' + url for url in data['urls']]
        lines += ['- Source SHA-256: `%s`' % digest for digest in data['hashes']]
    else:
        lines.append('- Package: %s %s of the build image%s'
                     % (name, version, ' (package snapshot %s)' % snapshot if snapshot else ''))
    if not data['license_texts']:
        recorded = ', '.join(data['licenses']) or 'no license'
        lines.append(DECISION + 'The SBOM records %s %s under %s, and no license text for it ships with this release. '
                     'The owner decides how it is treated before a public release.' % (name, version, recorded))
    return '\n'.join(lines)


def host_row(item):
    needed = _strings(item.get('needed_by'))
    abi = _text(item.get('abi'), '')
    license = _cell(item.get('license'))
    if LIBFUSE_SONAME.match(item['soname']):
        # The package holds more than the library (the fusermount program is GPL-2.0), and its tag names that.
        license += ' (the package; the library is LGPL-2.1, see "libfuse" below)'
    return '| `%s`%s | %s | %s | %s | %s |' % (item['soname'], ' (%s)' % abi if abi else '', _cell(item.get('package')),
                                            _cell(item.get('version')), license,
                                            _paths(needed) if needed else 'not recorded')


def python_line(item):
    name, version = _text(item.get('name'), '(unnamed)'), item.get('version')
    label = '`%s`%s' % (name, ' ' + version if isinstance(version, str) and version else '')
    if item.get('kind') == 'interpreter':
        return '- %s (interpreter): runs the Python files of this release' % label
    users = _strings(item.get('used_by'))
    return '- %s: used by %s' % (label, _paths(users) if users else 'not recorded')


def obligation_entries(sbom, components, compiled):
    """Statements for licenses whose obligations depend on how a library is linked: [(label, block)]."""
    hosts = [item for item in host_libraries(sbom)
             if LIBFUSE_SONAME.match(item['soname']) or _text(item.get('package'), '') in LIBFUSE_PACKAGES]
    shipped = [component for component in components if component['name'] in LIBFUSE_NAMES]
    inside = [data for data in compiled if data['name'] in LIBFUSE_NAMES]
    if not (hosts or shipped or inside):
        return []
    found = ['host-provided `%s` (package %s %s), needed by %s'
             % (item['soname'], _cell(item.get('package')), _cell(item.get('version')),
                _paths(_strings(item.get('needed_by'))) or 'not recorded') for item in hosts]
    found += ['bundled %s %s (%s)' % (c['name'], c['version'], _paths(c['files'])) for c in shipped]
    found += ['%s %s compiled into %s' % (d['name'], d['version'], _paths(d['carriers'])) for d in inside]
    lines = ['### libfuse (LGPL-2.1)', '- Found in this release: ' + '; '.join(found),
             '- License: libfuse is licensed under the GNU Lesser General Public License, version 2.1 (LGPL-2.1).']
    if hosts:
        lines.append('- Dynamic linking: the programs that need libfuse are linked against the shared library of the '
                     'system and contain no copy of libfuse. The library on the device can be replaced by a modified '
                     'version with the same interface, and these programs then use it (LGPL-2.1, section 6).')
    if shipped or inside:
        lines += ['- Static linking: libfuse is part of files of this release. LGPL-2.1, section 6, then requires '
                  'that a recipient can relink the program with a modified libfuse: the object files or the source '
                  'of that program have to accompany the release, or the program has to link libfuse dynamically.',
                  DECISION + 'The SBOM records no way to relink the files that contain libfuse. Link libfuse '
                  'dynamically, or ship what relinking needs, before a public release.']
    lines.append('- Source: https://github.com/libfuse/libfuse')
    return [('libfuse', '\n'.join(lines))]


def entries(sbom):
    """Everything the notices state about this SBOM, as {section: [(label, block)]} in document order."""
    build = sbom.get('build')
    snapshot = build.get('package_snapshot') if isinstance(build, dict) else None
    snapshot = snapshot if isinstance(snapshot, str) else None
    components, compiled = bundled_components(sbom), compiled_data(sbom)
    return {
        'components': [('%s %s (%s)' % (c['name'], c['version'], ', '.join(c['files'])), component_entry(c))
                       for c in components],
        'compiled': [('%s %s' % (d['name'], d['version']), compiled_entry(d, snapshot)) for d in compiled],
        'host': [('host library %s' % _library_name(item), host_row(item)) for item in host_libraries(sbom)],
        'python': [('Python %s' % item['name'], python_line(item)) for item in host_python(sbom)],
        'obligations': obligation_entries(sbom, components, compiled),
    }


def decisions_in(found):
    """[(label, message)] for every line marked OWNER DECISION REQUIRED in the entries `found`."""
    return [(label, line[len(DECISION):]) for section in ('components', 'compiled', 'obligations')
            for label, block in found[section] for line in block.split('\n') if line.startswith(DECISION)]


def open_decisions(sbom):
    """The decisions the notices of this SBOM still leave to the owner: [(label, message)]."""
    return decisions_in(entries(validate(sbom)))


# --- The document --------------------------------------------------------------------------------------------

def generate(sbom, rights_holder):
    """The text of THIRD-PARTY-NOTICES.md for this SBOM. The same SBOM and name always give the same text."""
    sbom = validate(sbom)
    holder = rights_holder.strip()
    if not holder or '\n' in holder:
        raise NoticeError('the rights holder must be one non-empty line')
    package = sbom['package']
    found = entries(sbom)
    observed = _unique(item.get('observed') for item in host_libraries(sbom))
    out = [
        '# Third-Party Notices',
        'Dosplex %s, built from commit %s.' % (_text(package.get('version'), 'unknown version'),
                                              _text(package.get('commit'), 'unknown')),
        'This file is generated from the SBOM of this release (`sbom/dosplex-sbom.json`) by '
        '`tools/generate-notices.py`; it is not edited by hand. It lists the third-party components that the release '
        'bundles or compiles in, and the libraries and Python modules that it expects the host system to provide.',
        "Dosplex's own files are licensed under the GNU Affero General Public License, version 3 or later (`licenses/dosplex-LICENSE`). The "
        'copyright holder is %s (`licenses/dosplex-NOTICE`). The AGPL covers Dosplex-owned files '
        'only. It does not replace or override the license of any component listed here.' % holder,
        '## Bundled components',
    ]
    if found['components']:
        out.append('These third-party files are part of the release. Each one is built from the upstream source '
                   'archive named in its entry (identified by its SHA-256), and its license text is under `licenses/`.')
        out += [block for _, block in found['components']]
    else:
        out.append('None.')
    out.append('## Data compiled into bundled files')
    if found['compiled']:
        out.append('Data and code of other packages is compiled into files of this release and is distributed with '
                   'them. The packages of the build image are named as such; the others were compiled from the '
                   'source archive named in their entry (identified by its SHA-256).')
        out += [block for _, block in found['compiled']]
    else:
        out.append('None.')
    out.append('## Libraries provided by the host system')
    if found['host']:
        out.append('These libraries are not part of the release. The files listed under "Needed by" load them from '
                   'the system they run on. Package, version and license are as the SBOM records them%s; the packages '
                   'on the device are the ones that are loaded, and their licenses apply.'
                   % (' (observed: %s)' % ', '.join(observed) if observed else ''))
        out.append('| Library | Package | Version | License | Needed by |\n| --- | --- | --- | --- | --- |\n'
                   + '\n'.join(row for _, row in found['host']))
    else:
        out.append('None.')
    out.append('## Python provided by the host system')
    if found['python']:
        out.append('The Python files of this release run on the Python of the system. The interpreter and these '
                   'modules are not part of the release; their versions and licenses are those of the packages '
                   'installed on the device.')
        out.append('\n'.join(line for _, line in found['python']))
    else:
        out.append('None.')
    if found['obligations']:
        out.append('## License obligations')
        out += [block for _, block in found['obligations']]
    out += ['## Provenance', CONTROLLER_LAYOUT_NOTE]
    decisions = decisions_in(found)
    if decisions:
        out += ['## Open decisions',
                'These items need a decision by the release owner before a public release (each one is marked '
                'OWNER DECISION REQUIRED in its entry above). `tools/generate-notices.py --strict` refuses to write '
                'this file while one is open.',
                '\n'.join('- %s' % label for label in _unique(label for label, _ in decisions))]
    return '\n\n'.join(out) + '\n'


# --- The check -----------------------------------------------------------------------------------------------

def notice_problems(sbom, text):
    """What the notices lack that the SBOM requires, and what they state that the SBOM does not support.

    Every entry the SBOM implies has to appear in the text word for word, so an entry that was edited by hand or
    belongs to another SBOM is reported as well as a missing one."""
    lines = text.replace('\r\n', '\n').split('\n')
    haystack = '\n' + '\n'.join(lines).rstrip('\n') + '\n\n'
    problems, headings, rows = [], set(), set()
    for section, found in entries(validate(sbom)).items():
        for label, block in found:
            first = block.split('\n', 1)[0]
            if section == 'host':
                rows.add(first)
            elif first.startswith('### '):
                headings.add(first)
            # A heading with its bullets is followed by a blank line; a table row or a list line is one line.
            if '\n%s\n%s' % (block, '\n' if first.startswith('### ') else '') in haystack:
                continue
            if first not in lines:
                problems.append('%s has no entry in the notices' % label)
                continue
            actual = []
            for line in lines[lines.index(first):]:
                if not line.strip():
                    break
                actual.append(line)
            expected = block.split('\n')
            for number in range(max(len(expected), len(actual))):
                want = expected[number] if number < len(expected) else None
                got = actual[number] if number < len(actual) else None
                if want != got:
                    problems.append('%s: its entry in the notices differs from the SBOM (expected %r, found %r)'
                                    % (label, want, got))
                    break
    for line in lines:
        if line.startswith('### ') and line not in headings:
            problems.append('the notices have an entry %r that the SBOM does not support' % line[4:])
        elif line.startswith('| `') and line not in rows:
            problems.append('the notices list a host library that the SBOM does not: %s' % line.split('|')[1].strip())
    return problems


# --- The command ---------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('sbom', type=Path, help='sbom/dosplex-sbom.json of a payload')
    parser.add_argument('-o', '--output', type=Path, help='write the notices here instead of standard output')
    parser.add_argument('--rights-holder-file', type=Path, default=RIGHTS_HOLDER_FILE,
                        help='file with the name of the rights holder (default: legal/RIGHTS_HOLDER)')
    parser.add_argument('--check', type=Path, metavar='NOTICES',
                        help='check this notices file against the SBOM instead of writing one')
    parser.add_argument('--strict', action='store_true',
                        help='fail while a decision marked OWNER DECISION REQUIRED is open')
    args = parser.parse_args(argv)
    try:
        sbom = load_sbom(args.sbom)
        decisions = open_decisions(sbom)
        if args.check:
            try:
                existing = args.check.read_text(encoding='utf-8')
            except OSError as error:
                raise NoticeError('cannot read %s: %s' % (args.check, error.strerror or error))
            problems = notice_problems(sbom, existing)
            if args.strict:
                problems += ['open decision for %s: %s' % decision for decision in decisions]
            if problems:
                print('generate-notices: %d problem(s) in %s' % (len(problems), args.check), file=sys.stderr)
                for problem in problems:
                    print('  - ' + problem, file=sys.stderr)
                return 1
            print('generate-notices: %s matches the SBOM' % args.check)
            return 0
        text = generate(sbom, read_rights_holder(args.rights_holder_file))
        if args.strict and decisions:
            print('generate-notices: %d open decision(s), nothing written:' % len(decisions), file=sys.stderr)
            for label, message in decisions:
                print('  - %s: %s' % (label, message), file=sys.stderr)
            return 1
    except NoticeError as error:
        print('generate-notices: %s' % error, file=sys.stderr)
        return 1
    if args.output:
        args.output.write_bytes(text.encode('utf-8'))
    else:
        sys.stdout.buffer.write(text.encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
