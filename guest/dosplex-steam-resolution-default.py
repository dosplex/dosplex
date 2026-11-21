#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Start Steam's Maximum Game Resolution at Native, once per Steam account of this user, before Steam starts.

Steam keeps the setting in userdata/<account>/config/localconfig.vdf, UserLocalConfigStore > Gamescope >
GameResolutionGlobal. Until a player chooses, it is absent or "Default", which Steam shows as "Recommended" and
which gives every game 1280x720 or 1920x1080 whatever the side's size. In a side, Native is the side's own size
(the game monitor that Dosplex's picture size sets), the size games had before Steam's resolution settings worked in
a side: Quake saved its mode at that size, got 1920x1080 in a 1920x2160 half and quit while setting up its display
(Steam Machine, 2026-11-18).

So it is set to "Native" once, only where it is absent or "Default", and a marker per account keeps it from being
done again: a player who later picks "Recommended" or any size in Steam keeps it. It is Steam's own setting in Steam's
own menu; nothing else changes. The file is rewritten in place of the old one (a new file next to it, then a rename),
only while Steam is closed (the side starts Steam after this), and a file that cannot be read as expected is left
alone.

    dosplex-steam-resolution-default.py STEAM_ROOT MARKER_DIR
"""
import os
import re
import sys
from pathlib import Path

KEY = 'GameResolutionGlobal'
VALUE = 'Native'


def apply(text):
    """The text with GameResolutionGlobal set to Native if it is absent or Default, else None (nothing to do)."""
    found = re.search(r'"%s"\s*"([^"]*)"' % KEY, text)
    if found:
        if found.group(1) != 'Default':
            return None
        return text[:found.start(1)] + VALUE + text[found.end(1):]
    block = re.search(r'\n(\t)"Gamescope"\n\t\{\n', text)
    if block:
        return text[:block.end()] + '\t\t"%s"\t\t"%s"\n' % (KEY, VALUE) + text[block.end():]
    top = re.match(r'"UserLocalConfigStore"\n\{\n', text)
    if top:
        return text[:top.end()] + '\t"Gamescope"\n\t{\n\t\t"%s"\t\t"%s"\n\t}\n' % (KEY, VALUE) + text[top.end():]
    return None


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    steam, markers = Path(argv[1]), Path(argv[2])
    try:
        accounts = sorted(p for p in (steam / 'userdata').iterdir() if p.name.isdigit() and p.name != '0')
    except OSError:
        return 0
    markers.mkdir(mode=0o700, parents=True, exist_ok=True)
    for account in accounts:
        marker = markers / ('resolution-native-' + account.name)
        config = account / 'config' / 'localconfig.vdf'
        if marker.exists() or config.is_symlink() or not config.is_file():
            continue
        try:
            text = config.read_text(encoding='utf-8')
            changed = apply(text)
            if changed is not None:
                new = config.with_name(config.name + '.dosplex-new')
                new.write_text(changed, encoding='utf-8')
                os.chmod(new, config.stat().st_mode & 0o777)
                os.replace(new, config)
                print('Dosplex: Steam account %s: Maximum Game Resolution starts at Native' % account.name, file=sys.stderr)
            marker.write_text('1\n')
        except (OSError, UnicodeError) as error:
            print('Dosplex: Steam account %s: resolution default not set: %s' % (account.name, error), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
