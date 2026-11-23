#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Start Steam's Maximum Game Resolution at Native, once per Steam account of this user, before Steam starts.

Steam keeps the setting in userdata/<account>/config/localconfig.vdf, UserLocalConfigStore > Gamescope >
GameResolutionGlobal. Until a player chooses, it is absent or "Default", which Steam shows as "Recommended" and
which gives every game 1280x720 or 1920x1080 whatever the side's size. In a side, Native is the side's own size
(the game monitor that DosPlex's picture size sets), the size games had before Steam's resolution settings worked in
a side: Quake saved its mode at that size, got 1920x1080 in a 1920x2160 half and quit while setting up its display
(Steam Machine, 2026-11-18).

So it is set to "Native" once, only where it is absent or "Default", and a marker per account keeps it from being
done again: a player who later picks "Recommended" or any size in Steam keeps it. It is Steam's own setting in Steam's
own menu; nothing else changes. The file is rewritten in place of the old one (a new file next to it, then a rename),
only while Steam is closed (the side starts Steam after this), and a file that cannot be read as expected is left
alone.

The second setting is of the Steam client itself, not of an account: config/config.vdf, InstallConfigStore > Gamescope >
EnableSteamNativeExternalResolution, which Steam shows under Settings > Developer as "Enable Native Resolution on
External Displays". Without it a newer client puts a side's screen back to 1920x1080 whenever it has more pixels than
1920x1200: its interface asks for that itself, 0.3 s after any other size (Steam Deck, client 1795048523, 2026-11-21),
so a picture size of Native, 1:1 or 4:3 in a 1920x2160 half did not stay. It is switched on once, with a marker of its
own, the same way: a player who switches it off in Steam keeps that, and the picture size then says what Steam did.

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


EXTERNAL_KEY = 'EnableSteamNativeExternalResolution'


def apply_external(text):
    """The text of config.vdf with EnableSteamNativeExternalResolution "1", else None (it is on, or not understood)."""
    found = re.search(r'"%s"\s*"([^"]*)"' % EXTERNAL_KEY, text)
    if found:
        if found.group(1) == '1':
            return None
        return text[:found.start(1)] + '1' + text[found.end(1):]
    block = re.search(r'\n\t"Gamescope"\n\t\{\n', text)
    if block:
        return text[:block.end()] + '\t\t"%s"\t\t"1"\n' % EXTERNAL_KEY + text[block.end():]
    top = re.match(r'"InstallConfigStore"\n\{\n', text)
    if top:
        return text[:top.end()] + '\t"Gamescope"\n\t{\n\t\t"%s"\t\t"1"\n\t}\n' % EXTERNAL_KEY + text[top.end():]
    return None


def rewrite(config, changed):
    """Put `changed` in place of config: a new file next to it with the same mode, then a rename."""
    new = config.with_name(config.name + '.dosplex-new')
    new.write_text(changed, encoding='utf-8')
    os.chmod(new, config.stat().st_mode & 0o777)
    os.replace(new, config)


def client_setting(steam, markers):
    """Switch "Enable Native Resolution on External Displays" on, once for this Steam folder."""
    marker = markers / 'native-external-resolution'
    config = steam / 'config' / 'config.vdf'
    if marker.exists() or config.is_symlink() or not config.is_file():
        return          # (a Steam that has never run has no file yet: the next start of the side does it)
    try:
        text = config.read_text(encoding='utf-8')
        changed = apply_external(text)
        if changed is not None:
            rewrite(config, changed)
            print('DosPlex: Steam: Enable Native Resolution on External Displays is switched on', file=sys.stderr)
        elif EXTERNAL_KEY not in text:
            return      # (a file it does not understand: left alone, and looked at again next time)
        marker.write_text('1\n')
    except (OSError, UnicodeError) as error:
        print('DosPlex: Steam: native resolution on external displays not set: %s' % error, file=sys.stderr)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    steam, markers = Path(argv[1]), Path(argv[2])
    if not steam.is_dir():
        return 0
    markers.mkdir(mode=0o700, parents=True, exist_ok=True)
    client_setting(steam, markers)
    try:
        accounts = sorted(p for p in (steam / 'userdata').iterdir() if p.name.isdigit() and p.name != '0')
    except OSError:
        return 0
    for account in accounts:
        marker = markers / ('resolution-native-' + account.name)
        config = account / 'config' / 'localconfig.vdf'
        if marker.exists() or config.is_symlink() or not config.is_file():
            continue
        try:
            text = config.read_text(encoding='utf-8')
            changed = apply(text)
            if changed is not None:
                rewrite(config, changed)
                print('DosPlex: Steam account %s: Maximum Game Resolution starts at Native' % account.name, file=sys.stderr)
            marker.write_text('1\n')
        except (OSError, UnicodeError) as error:
            print('DosPlex: Steam account %s: resolution default not set: %s' % (account.name, error), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
