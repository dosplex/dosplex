#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Compare the build image's package snapshot with SteamOS's own package repositories.

  check-steamos-parity.py [--steamos 3.8] [--snapshot 2025/07/30] [--packages FILE]

The image is built from a dated Arch Linux Archive snapshot instead of Valve's package mirror (README.md says
why). This downloads the repository databases of both, core, extra and multilib (the lib32 packages of the
32-bit Gamescope layer), and compares them package by package:
name, version and the SHA-256 of the package file. With --packages (the output of `pacman -Q` in the image,
for example `docker run --rm --entrypoint pacman IMAGE -Q`) it also says whether every package the image holds
is the same file in the SteamOS repository. Use it to judge a new snapshot date before moving the pin.

Standard library only; downloads about 20 MB.
"""
import argparse
import io
from pathlib import Path
import sys
import tarfile
import urllib.request

STEAMOS = 'https://steamdeck-packages.steamos.cloud/archlinux-mirror/{repo}-{release}/os/x86_64/{repo}-{release}.db'
ARCHIVE = 'https://archive.archlinux.org/repos/{snapshot}/{repo}/os/x86_64/{repo}.db'


def load(url):
    """name -> (version, sha256) from a pacman repository database."""
    with urllib.request.urlopen(url, timeout=120) as reply:
        data = reply.read()
    packages = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith('/desc'):
                continue
            fields, key = {}, None
            for line in tar.extractfile(member).read().decode().splitlines():
                if line.startswith('%') and line.endswith('%'):
                    key = line.strip('%')
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            packages[fields['NAME'][0]] = (fields['VERSION'][0], fields.get('SHA256SUM', [''])[0])
    return packages


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--steamos', default='3.8', help='SteamOS repository release (default 3.8)')
    parser.add_argument('--snapshot', default='2025/07/30', help='Arch Linux Archive date (default 2025/07/30)')
    parser.add_argument('--packages', type=Path, help='`pacman -Q` output of the image')
    args = parser.parse_args()
    merged = {'steamos': {}, 'archive': {}}
    for repo in ('core', 'extra', 'multilib'):
        steamos = load(STEAMOS.format(repo=repo, release=args.steamos))
        archive = load(ARCHIVE.format(snapshot=args.snapshot, repo=repo))
        same = sum(1 for name, entry in steamos.items() if archive.get(name) == entry)
        print('%-5s SteamOS %s: %d packages, Arch Linux Archive %s: %d packages, identical file: %d, different: %d'
              % (repo, args.steamos, len(steamos), args.snapshot, len(archive), same,
                 len(set(steamos) | set(archive)) - same))
        for side, table in (('steamos', steamos), ('archive', archive)):
            for name, entry in table.items():
                merged[side][name] = entry
    if args.packages is None:
        return 0
    differing = []
    lines = [line.split() for line in args.packages.read_text().splitlines() if line.strip()]
    for name, version in lines:
        if merged['steamos'].get(name) != merged['archive'].get(name) or \
                (merged['archive'].get(name) or ('',))[0] != version:
            differing.append((name, version, merged['archive'].get(name), merged['steamos'].get(name)))
    print('the image holds %d packages; %d of them are not the same file in SteamOS %s'
          % (len(lines), len(differing), args.steamos))
    for name, version, archive, steamos in differing:
        print('  %s %s: archive %s, SteamOS %s' % (name, version, archive and archive[0], steamos and steamos[0]))
    return 1 if differing else 0


if __name__ == '__main__':
    sys.exit(main())
