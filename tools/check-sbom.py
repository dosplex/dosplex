#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Check a Dosplex release payload against its SBOM.

  check-sbom.py PAYLOAD_DIR [--sbom FILE]

PAYLOAD_DIR is the directory packaging/release-build/build.sh produces (bin/ lib/ libexec/ share/ licenses/
sbom/). The SBOM is PAYLOAD_DIR/sbom/dosplex-sbom.json unless --sbom names another file. Exit status 0 means the
payload and its SBOM agree; 1 means problems were found (listed on stderr, an unreadable SBOM is one); 2 means
PAYLOAD_DIR is not a directory. Standard library only.

The SBOM is one JSON object. packaging/release-build/README.md describes the format; this check enforces it:

  format, format_version   "dosplex-sbom", 1
  package                  name, version (equal to share/dosplex/VERSION), commit, source_date_epoch
  build                    how and where it was built (base image, package snapshot, toolchain, glibc)
  files                    one record per payload file, except the SBOM itself: path, sha256, size, mode
                           ("0755"), kind (executable | library | script | license | data), name, version,
                           origin (repository | upstream | distribution), license (an SPDX expression),
                           license_files, and the source: source_url and source_sha256 for origin
                           "upstream", source_path for origin "repository". Optional "includes" lists data
                           or code of other packages compiled into the file (name, version, license,
                           license_files; a component compiled from a source archive also has source_url and
                           source_sha256).
  host_provided            libraries: every NEEDED soname that is not bundled (soname, needed_by, package,
                           version, license; abi "i386" for the libraries that 32-bit files need, nothing for
                           the 64-bit ones); python: every third-party module the payload's Python files
                           import (name, used_by, version)

Checked: every payload file is listed, nothing listed is missing, the hashes, sizes and modes match; every
record is complete; every file in lib/ is a library with a version, a license and a license text; every
license text named exists under licenses/ and is itself listed; every NEEDED entry of every ELF file is either
bundled or listed as host-provided; every third-party Python import is listed as host-provided; the payload
holds regular files only; every Vulkan manifest below share/vulkan/ points at a library of the payload by a path
relative to the manifest (the payload is installed in a directory that is not known when it is built);
licenses/dosplex-THIRD-PARTY-NOTICES.md has an entry for every bundled component, every piece of data compiled
into a file and every host-provided library of the SBOM, as tools/generate-notices.py writes them (that script
has to be next to this one), and no entry the SBOM does not support.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import struct
import sys

SBOM_PATH = 'sbom/dosplex-sbom.json'
VERSION_PATH = 'share/dosplex/VERSION'
NOTICES_PATH = 'licenses/dosplex-THIRD-PARTY-NOTICES.md'
NOTICES_TOOL = 'generate-notices.py'
FORMAT = 'dosplex-sbom'
FORMAT_VERSION = 1
KINDS = ('executable', 'library', 'script', 'license', 'data')
ORIGINS = ('repository', 'upstream', 'distribution')
HEX64 = re.compile(r'[0-9a-f]{64}')
VULKAN_MANIFEST = re.compile(r'share/vulkan/(?:implicit_layer|explicit_layer|icd)\.d/[^/]+\.json')


# --- ELF: the dynamic section of a little-endian file, 64-bit or 32-bit (what ldd and readelf -d show) ----

DT_NEEDED, DT_STRTAB, DT_STRSZ, DT_SONAME, DT_RPATH, DT_RUNPATH = 1, 5, 10, 14, 15, 29
# The two kinds of file a payload may hold, by the machine in the ELF header. The libraries that a 32-bit file
# needs are not the libraries that a 64-bit file needs (/usr/lib32 and /usr/lib), whatever their sonames say.
ABIS = {62: 'x86_64', 3: 'i386'}
DEFAULT_ABI = 'x86_64'


def read_elf(path):
    """{'needed': [...], 'soname': str or None, 'runpath': ..., 'rpath': ..., 'abi': 'x86_64' | 'i386'} for an ELF
    file, None for anything else."""
    with open(path, 'rb') as stream:
        head = stream.read(64)
        if len(head) < 4 or head[:4] != b'\x7fELF':
            return None
        wide = len(head) > 4 and head[4] == 2
        if len(head) < (64 if wide else 52) or head[4] not in (1, 2) or head[5] != 1:
            raise ValueError('%s: only little-endian ELF files are supported' % path)
        machine, = struct.unpack_from('<H', head, 0x12)
        if wide:
            phoff, = struct.unpack_from('<Q', head, 0x20)
            phentsize, phnum = struct.unpack_from('<HH', head, 0x36)
        else:
            phoff, = struct.unpack_from('<I', head, 0x1C)
            phentsize, phnum = struct.unpack_from('<HH', head, 0x2A)
        stream.seek(phoff)
        table = stream.read(phentsize * phnum)
        loads, dynamic = [], None
        for index in range(phnum):
            if wide:
                kind, _flags, offset, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from(
                    '<IIQQQQQQ', table, index * phentsize)
            else:
                kind, offset, vaddr, _paddr, filesz, _memsz, _flags, _align = struct.unpack_from(
                    '<IIIIIIII', table, index * phentsize)
            if kind == 1:                    # PT_LOAD
                loads.append((vaddr, offset, filesz))
            elif kind == 2:                  # PT_DYNAMIC
                dynamic = (offset, filesz)
        result = {'needed': [], 'soname': None, 'runpath': None, 'rpath': None,
                  'abi': ABIS.get(machine, 'machine-%d' % machine)}
        if dynamic is None:
            return result
        stream.seek(dynamic[0])
        raw = stream.read(dynamic[1])
        entries = []
        size, layout = (16, '<qQ') if wide else (8, '<iI')
        for position in range(0, len(raw) - size + 1, size):
            tag, value = struct.unpack_from(layout, raw, position)
            if tag == 0:
                break
            entries.append((tag, value))
        strtab = next((value for tag, value in entries if tag == DT_STRTAB), None)
        strsz = next((value for tag, value in entries if tag == DT_STRSZ), None)
        if strtab is None or strsz is None:
            return result
        for vaddr, offset, filesz in loads:
            if vaddr <= strtab < vaddr + filesz:
                stream.seek(strtab - vaddr + offset)
                break
        else:
            raise ValueError('%s: the string table is not in a loadable segment' % path)
        strings = stream.read(strsz)

    def text(index):
        return strings[index:strings.index(b'\0', index)].decode()
    for tag, value in entries:
        if tag == DT_NEEDED:
            result['needed'].append(text(value))
        elif tag == DT_SONAME:
            result['soname'] = text(value)
        elif tag == DT_RUNPATH:
            result['runpath'] = text(value)
        elif tag == DT_RPATH:
            result['rpath'] = text(value)
    return result


# --- Python: the third-party modules a script imports ------------------------------------------------------

def third_party_imports(path, local_names=()):
    """Top-level names a Python file imports that are not standard library, not the file's siblings."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    return {name for name in names
            if name not in sys.stdlib_module_names and name != '__future__' and name not in local_names}


def python_files(files):
    """The payload's Python files among `files` (relative paths): name -> path."""
    return sorted(path for path in files if path.endswith('.py') and path.split('/')[0] in ('bin', 'libexec'))


def local_module_names(paths):
    return {Path(path).stem.replace('-', '_') for path in paths}


# --- the check ----------------------------------------------------------------------------------------------

def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def text_field(record, key):
    value = record.get(key)
    return value if isinstance(value, str) and value.strip() else None


def walk_payload(payload, problems):
    """relative path -> absolute path of every regular file; anything else in the tree is a problem."""
    found = {}
    for directory, dirnames, filenames in os.walk(payload, followlinks=False):
        for name in dirnames + filenames:
            full = Path(directory) / name
            relative = full.relative_to(payload).as_posix()
            mode = full.lstat().st_mode
            if stat.S_ISLNK(mode):
                problems.append('%s is a symbolic link; a payload holds regular files only' % relative)
            elif stat.S_ISREG(mode):
                found[relative] = full
            elif not stat.S_ISDIR(mode):
                problems.append('%s is not a regular file' % relative)
    return found


def check_record(index, record, problems):
    """The shape of one files[] record. Returns its path or None."""
    where = 'files[%d]' % index
    if not isinstance(record, dict):
        problems.append('%s is not an object' % where)
        return None
    path = text_field(record, 'path')
    if path is None:
        problems.append('%s has no path' % where)
        return None
    where = path
    for key in ('name', 'version', 'license'):
        if text_field(record, key) is None:
            problems.append('%s: no %s' % (where, key))
    if record.get('kind') not in KINDS:
        problems.append('%s: kind must be one of %s' % (where, ', '.join(KINDS)))
    if not isinstance(record.get('sha256'), str) or not HEX64.fullmatch(record['sha256']):
        problems.append('%s: sha256 must be 64 lowercase hex digits' % where)
    if type(record.get('size')) is not int or record['size'] < 0:
        problems.append('%s: size must be a number of bytes' % where)
    if not isinstance(record.get('mode'), str) or not re.fullmatch(r'0[0-7]{3}', record['mode']):
        problems.append('%s: mode must be an octal string like "0755"' % where)
    licenses = record.get('license_files', [])
    if not isinstance(licenses, list) or not all(isinstance(item, str) for item in licenses):
        problems.append('%s: license_files must be a list of paths' % where)
    origin = record.get('origin')
    if origin not in ORIGINS:
        problems.append('%s: origin must be one of %s' % (where, ', '.join(ORIGINS)))
    elif origin == 'upstream':
        url = record.get('source_url')
        if not isinstance(url, str) or not url.startswith('https://'):
            problems.append('%s: an upstream file needs an https source_url' % where)
        if not isinstance(record.get('source_sha256'), str) or not HEX64.fullmatch(record['source_sha256']):
            problems.append('%s: an upstream file needs the source_sha256 of its source archive' % where)
        problems.extend(patches_problems(where, record))
    elif origin == 'repository' and text_field(record, 'source_path') is None:
        problems.append('%s: a file from the repository needs a source_path' % where)
    includes = record.get('includes', [])
    if not isinstance(includes, list):
        problems.append('%s: includes must be a list' % where)
    else:
        for item in includes:
            if not isinstance(item, dict) or not all(text_field(item, key) for key in ('name', 'version', 'license')):
                problems.append('%s: every includes entry needs a name, a version and a license' % where)
            elif ('source_url' in item) != ('source_sha256' in item):
                problems.append('%s: includes %s needs both source_url and source_sha256, or neither'
                                % (where, item['name']))
            elif 'source_url' in item and (not isinstance(item['source_url'], str)
                                           or not item['source_url'].startswith('https://')
                                           or not isinstance(item['source_sha256'], str)
                                           or not HEX64.fullmatch(item['source_sha256'])):
                problems.append('%s: includes %s needs an https source_url and the 64 hex digits of source_sha256'
                                % (where, item['name']))
            else:
                problems.extend(patches_problems('%s: includes %s' % (where, item['name']), item))
    return path


def patches_problems(where, record):
    """The problems of a record's optional patches list: the patches applied to the source archive before the
    build, each named by its path in the repository and the SHA-256 of the patch file."""
    if 'patches' not in record:
        return []
    patches = record['patches']
    if not isinstance(patches, list) or not patches:
        return ['%s: patches must be a non-empty list' % where]
    for item in patches:
        if (not isinstance(item, dict) or text_field(item, 'path') is None
                or not isinstance(item.get('sha256'), str) or not HEX64.fullmatch(item['sha256'])):
            return ['%s: every patches entry needs a path and the 64 hex digits of its sha256' % where]
    return []


def vulkan_manifest_problems(path, full, on_disk):
    """Problems of one Vulkan loader manifest (a layer or an ICD) of the payload, as a list of sentences.

    The loader reads "library_path" relative to the directory of the manifest when it has a directory part, and
    from the system's library directories when it is a bare name. A payload is installed in a directory that is
    chosen later (/opt/dosplex/releases/<name>, or wherever a trial puts it), so the path has to be relative and to
    lead to a library of this payload."""
    try:
        data = json.loads(full.read_text())
    except (OSError, ValueError) as error:
        return ['%s: cannot read the manifest: %s' % (path, error)]
    sections = []
    if isinstance(data, dict):
        sections += [data[key] for key in ('layer', 'ICD') if isinstance(data.get(key), dict)]
        if isinstance(data.get('layers'), list):
            sections += [item for item in data['layers'] if isinstance(item, dict)]
    if not sections:
        return ['%s: the manifest describes no layer and no driver' % path]
    problems = []
    for section in sections:
        library = section.get('library_path')
        if not isinstance(library, str) or not library.strip():
            problems.append('%s: a layer without a library_path' % path)
        elif library.startswith('/'):
            problems.append('%s: library_path %s is absolute; the payload moves, so it has to be relative to the '
                            'manifest' % (path, library))
        elif '/' not in library:
            problems.append('%s: library_path %s is a bare name, which the loader looks for in the system, not in '
                            'the payload' % (path, library))
        else:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), library))
            if target.startswith('../') or target == '..':
                problems.append('%s: library_path %s leaves the payload' % (path, library))
            elif target not in on_disk:
                problems.append('%s: library_path %s leads to %s, which is not in the payload'
                                % (path, library, target))
    return problems


def notices_file_problems(sbom, notices):
    """Problems of the notices file `notices` (a path) measured against the SBOM, as a list of sentences.

    The comparison is made by tools/generate-notices.py, which has to be next to this script: a payload whose
    notices cannot be checked does not pass."""
    tool_path = Path(__file__).resolve().with_name(NOTICES_TOOL)
    try:
        spec = importlib.util.spec_from_file_location('generate_notices', tool_path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
    except (OSError, ImportError, SyntaxError, AttributeError) as error:
        return ['cannot check %s: %s is not usable next to check-sbom.py (%s)' % (NOTICES_PATH, NOTICES_TOOL, error)]
    try:
        found = tool.notice_problems(sbom, notices.read_text(encoding='utf-8'))
    except (OSError, ValueError, tool.NoticeError) as error:
        return ['cannot check %s: %s' % (NOTICES_PATH, error)]
    return ['%s: %s' % (NOTICES_PATH, problem) for problem in found]


def check(payload, sbom_file=None):
    """The problems found, as a list of sentences (empty when the payload and its SBOM agree)."""
    payload = Path(payload)
    problems = []
    sbom_path = Path(sbom_file) if sbom_file else payload / SBOM_PATH
    try:
        sbom = json.loads(sbom_path.read_text())
    except (OSError, ValueError) as error:
        return ['cannot read the SBOM %s: %s' % (sbom_path, error)]
    if not isinstance(sbom, dict) or sbom.get('format') != FORMAT or sbom.get('format_version') != FORMAT_VERSION:
        return ['%s is not a %s version %d file' % (sbom_path, FORMAT, FORMAT_VERSION)]
    package = sbom.get('package')
    if not isinstance(package, dict) or not all(text_field(package, key) for key in ('name', 'version', 'commit')):
        problems.append('package needs a name, a version and a commit')
        package = {}
    if type(package.get('source_date_epoch')) is not int:
        problems.append('package.source_date_epoch must be a number')
    if not isinstance(sbom.get('build'), dict):
        problems.append('build must be an object')
    records = sbom.get('files')
    host = sbom.get('host_provided')
    if not isinstance(records, list) or not isinstance(host, dict):
        problems.append('files must be a list and host_provided an object')
        return problems
    libraries, python = host.get('libraries'), host.get('python')
    if not isinstance(libraries, list) or not isinstance(python, list):
        problems.append('host_provided needs a libraries list and a python list')
        return problems

    on_disk = walk_payload(payload, problems)
    listed = {}
    for index, record in enumerate(records):
        path = check_record(index, record, problems)
        if path is None:
            continue
        if path in listed:
            problems.append('%s is listed twice' % path)
        listed[path] = record

    for path in sorted(on_disk):
        if path not in listed and path != SBOM_PATH:
            problems.append('%s is in the payload but has no SBOM entry' % path)
    for path, record in sorted(listed.items()):
        if path not in on_disk:
            problems.append('%s is in the SBOM but not in the payload' % path)
            continue
        info = on_disk[path].stat()
        if isinstance(record.get('sha256'), str) and sha256_of(on_disk[path]) != record['sha256']:
            problems.append('%s differs from its SBOM sha256' % path)
        if type(record.get('size')) is int and info.st_size != record['size']:
            problems.append('%s has %d bytes, the SBOM says %d' % (path, info.st_size, record['size']))
        if isinstance(record.get('mode'), str) and '%04o' % (info.st_mode & 0o777) != record['mode']:
            problems.append('%s has mode %04o, the SBOM says %s' % (path, info.st_mode & 0o777, record['mode']))

    for path in sorted(on_disk):
        if path.startswith('lib/') and path in listed:
            record = listed[path]
            if record.get('kind') != 'library':
                problems.append('%s is in lib/ but its SBOM kind is %r, not "library"' % (path, record.get('kind')))
            if record.get('origin') == 'distribution':
                problems.append('%s: a bundled library comes from upstream source or from this repository' % path)
            if not record.get('license_files'):
                problems.append('%s: a bundled library has to name its license text in license_files' % path)

    def check_license_files(owner, names):
        for name in names:
            if not name.startswith('licenses/'):
                problems.append('%s: license text %s is not under licenses/' % (owner, name))
            elif name not in on_disk:
                problems.append('%s: license text %s is missing from the payload' % (owner, name))
            elif name not in listed:
                problems.append('%s: license text %s has no SBOM entry' % (owner, name))
            elif on_disk[name].stat().st_size == 0:
                problems.append('%s: license text %s is empty' % (owner, name))
    for path, record in sorted(listed.items()):
        names = record.get('license_files', [])
        if isinstance(names, list):
            check_license_files(path, [name for name in names if isinstance(name, str)])
        for item in record.get('includes', []) if isinstance(record.get('includes'), list) else []:
            if isinstance(item, dict) and isinstance(item.get('license_files'), list):
                check_license_files('%s includes %s' % (path, item.get('name')),
                                    [name for name in item['license_files'] if isinstance(name, str)])

    for path in sorted(on_disk):
        if VULKAN_MANIFEST.fullmatch(path):
            problems += vulkan_manifest_problems(path, on_disk[path], on_disk)

    # Every NEEDED entry is bundled or declared host-provided, per ABI: a 32-bit file needs the 32-bit library.
    bundled = set()
    elf = {}
    for path, full in sorted(on_disk.items()):
        try:
            info = read_elf(full)
        except (OSError, ValueError, struct.error) as error:
            problems.append('%s: unreadable ELF file: %s' % (path, error))
            continue
        if info is None:
            continue
        elf[path] = info
        if path.startswith('lib/'):
            bundled.add((path.split('/', 1)[1], info['abi']))
            if info['soname']:
                bundled.add((info['soname'], info['abi']))

    def shown(soname, abi):
        return soname if abi == DEFAULT_ABI else '%s (%s)' % (soname, abi)
    declared = {}
    for item in libraries:
        if not isinstance(item, dict) or text_field(item, 'soname') is None:
            problems.append('host_provided.libraries: every entry needs a soname')
            continue
        needed_by = item.get('needed_by')
        if not isinstance(needed_by, list) or not needed_by:
            problems.append('host_provided.libraries: %s needs a needed_by list' % item['soname'])
            continue
        abi = item.get('abi', DEFAULT_ABI)
        if abi not in ABIS.values():
            problems.append('host_provided.libraries: %s has the abi %r, not one of %s'
                            % (item['soname'], abi, ', '.join(sorted(ABIS.values()))))
            continue
        declared[(item['soname'], abi)] = needed_by
        for path in needed_by:
            if path not in on_disk:
                problems.append('host_provided.libraries: %s is needed by %s, which is not in the payload'
                                % (shown(item['soname'], abi), path))
    needed = {}
    for path, info in elf.items():
        for soname in info['needed']:
            needed.setdefault((soname, info['abi']), set()).add(path)
    for (soname, abi), users in sorted(needed.items()):
        if (soname, abi) not in bundled and (soname, abi) not in declared:
            problems.append('%s needs %s, which is neither bundled in lib/ nor listed under host_provided.libraries'
                            % (', '.join(sorted(users)), shown(soname, abi)))
    for soname, abi in sorted(declared):
        if (soname, abi) in bundled:
            problems.append('host_provided.libraries lists %s, but the payload bundles it' % shown(soname, abi))
        elif (soname, abi) not in needed:
            problems.append('host_provided.libraries lists %s, which no file in the payload needs'
                            % shown(soname, abi))

    # Every third-party Python module is declared host-provided.
    scripts = python_files(on_disk)
    local = local_module_names(scripts)
    imported = {}
    for path in scripts:
        try:
            names = third_party_imports(on_disk[path], local)
        except (OSError, SyntaxError, ValueError) as error:
            problems.append('%s: cannot read the imports: %s' % (path, error))
            continue
        for name in names:
            imported.setdefault(name, set()).add(path)
    declared_modules = set()
    for item in python:
        if not isinstance(item, dict) or text_field(item, 'name') is None:
            problems.append('host_provided.python: every entry needs a name')
            continue
        declared_modules.add(item['name'])
    for name, users in sorted(imported.items()):
        if name not in declared_modules:
            problems.append('%s imports the Python module %s, which is not listed under host_provided.python'
                            % (', '.join(sorted(users)), name))

    # The notices have an entry for every bundled library, compiled-in component and host-provided library.
    if NOTICES_PATH in on_disk:
        problems += notices_file_problems(sbom, on_disk[NOTICES_PATH])
    else:
        problems.append('%s is missing; it is generated from the SBOM by tools/%s and has to list every bundled library'
                        % (NOTICES_PATH, NOTICES_TOOL))

    # The release name is the same everywhere it is written.
    if VERSION_PATH in on_disk and text_field(package, 'version'):
        written = on_disk[VERSION_PATH].read_text().strip()
        if written != package['version']:
            problems.append('%s says %r but the SBOM package version is %r' % (VERSION_PATH, written, package['version']))
    elif VERSION_PATH not in on_disk:
        problems.append('%s is missing' % VERSION_PATH)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('payload', type=Path, help='the payload directory')
    parser.add_argument('--sbom', type=Path, help='the SBOM file (default: sbom/dosplex-sbom.json in the payload)')
    args = parser.parse_args(argv)
    if not args.payload.is_dir():
        print('check-sbom: %s is not a directory' % args.payload, file=sys.stderr)
        return 2
    problems = check(args.payload, args.sbom)
    if problems:
        print('check-sbom: %d problem(s) in %s' % (len(problems), args.payload), file=sys.stderr)
        for text in problems:
            print('  - ' + text, file=sys.stderr)
        return 1
    sbom = json.loads((args.sbom or args.payload / SBOM_PATH).read_text())
    print('check-sbom: OK, %d files, %d host-provided libraries, %d host-provided Python modules (%s)'
          % (len(sbom['files']), len(sbom['host_provided']['libraries']), len(sbom['host_provided']['python']),
             sbom['package']['version']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
