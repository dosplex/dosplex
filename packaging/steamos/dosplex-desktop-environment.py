#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Helper of a side's nested Plasma desktop. The session launcher runs it, one command at a time:

    dosplex-desktop-environment.py PID
        Read display names from our own nested KWin child, never guess :0. Prints the Wayland socket, the X
        display and the X authority file of the kwin_wayland that PID started, each ended by a NUL.
    dosplex-desktop-environment.py scale WIDTH HEIGHT PREFERENCE_FILE
        Decide the desktop scale for an output of WIDTH x HEIGHT pixels. Prints "SCALE REASON" on one line.
        PREFERENCE_FILE is the player's desktop-scale file, which may not exist.
    dosplex-desktop-environment.py set-key FILE GROUP KEY VALUE
        Set one key of a KDE settings file (created when missing) and leave the rest of the file as it is.
    dosplex-desktop-environment.py set-var FILE NAME VALUE
        Set NAME="VALUE" in a shell-style settings file such as user-dirs.dirs and leave its other lines.
    dosplex-desktop-environment.py osk-rule FILE
        Add or update the KWin window rule for Steam's on-screen keyboard in the kwinrulesrc FILE and keep
        every other rule of the file.
    dosplex-desktop-environment.py osk-script SCRIPTS_DIR
        Install the KWin script that fits Steam's on-screen keyboard to the side, below SCRIPTS_DIR
        (the kwin/scripts directory of the side's XDG_DATA_HOME).
"""
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time


def values(arguments):
    result=[]
    for key in ('--socket','--xwayland-display','--xwayland-xauthority'):
        if arguments.count(key)!=1:raise ValueError('missing/ambiguous nested display argument')
        result.append(arguments[arguments.index(key)+1])
    if not re.fullmatch(r'wayland-[0-9]+',result[0]) or not re.fullmatch(r':[0-9]+',result[1]):
        raise ValueError('invalid nested display name')
    if not Path(result[2]).is_absolute():raise ValueError('invalid X authority path')
    return result


def main(pid):
    parent=Path('/proc')/str(pid)
    end=time.monotonic()+5
    while time.monotonic()<end:
        if parent.stat().st_uid!=os.getuid():raise RuntimeError('wrong nested compositor owner')
        for child in (parent/'task'/str(pid)/'children').read_text().split():
            p=Path('/proc')/child
            try:
                if p.stat().st_uid!=os.getuid() or (p/'comm').read_text().strip()!='kwin_wayland':continue
                result=values((p/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0'))
                if Path(result[2]).stat().st_uid!=os.getuid():raise RuntimeError('wrong authority owner')
                sys.stdout.buffer.write(('\0'.join(result)+'\0').encode());return
            except (OSError,ValueError,IndexError):continue
        time.sleep(.02)
    raise RuntimeError('nested display environment unavailable')


# The scales the player's desktop-scale file may name, as the text that KWin's --scale takes.
SCALES = ('1', '1.25', '1.5', '1.75', '2', '2.5', '3')
MAX_DIMENSION = 16384
MAX_PREFERENCE = 64         # bytes of the desktop-scale file that are read; no valid value is longer


def dimension(value):
    """A pixel count from text or a number; None when it is missing or not a plausible size."""
    text = '' if value is None else str(value).strip()
    if not re.fullmatch(r'[0-9]{1,5}', text):
        return None
    number = int(text)
    return number if 1 <= number <= MAX_DIMENSION else None


def default_scale(width, height):
    """The scale for an output of width x height pixels: 2 from 1600 pixels tall or 3000 wide (a half or the
    whole of a 4K screen), 1.5 from 1000 pixels tall (1080p), else 1."""
    if height >= 1600 or width >= 3000:
        return '2'
    return '1.5' if height >= 1000 else '1'


def choose_scale(width, height, preference=None):
    """(scale, reason) for the desktop of a side whose output is width x height pixels.

    preference is the text of the player's desktop-scale file, None when the player has none. A preference
    that is one of SCALES wins; anything else is ignored. A size that is missing or implausible counts as
    unknown and gets scale 1. The reason is one line, for the launch log.
    """
    size = (dimension(width), dimension(height))
    if None in size:
        default, basis = '1', 'default for an unknown output size'
    else:
        default, basis = default_scale(*size), 'default for %dx%d' % size
    if preference is None:
        return default, basis
    wanted = preference.strip()
    if wanted in SCALES:
        return wanted, 'desktop-scale preference (default would be %s)' % default
    return default, 'ignored desktop-scale preference %s, %s' % (ascii(wanted[:24]), basis)


def read_preference(path):
    """The text of the player's desktop-scale file; None when there is no such file."""
    try:
        # Never wait for a writer: a pipe in this place reads as empty text, which is no valid scale.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            data = os.read(descriptor, MAX_PREFERENCE + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return None
    except OSError as problem:
        return '<unreadable: %s>' % (problem.strerror or type(problem).__name__)
    if len(data) > MAX_PREFERENCE:
        return '<more than %d bytes>' % MAX_PREFERENCE
    return data.decode('utf-8', 'replace')


def scale_line(width, height, preference_path):
    """What the scale command prints: SCALE, a space, the reason and a newline."""
    scale, reason = choose_scale(width, height, read_preference(preference_path))
    return '%s %s\n' % (scale, reason)


def merge_ini_key(text, group, key, value):
    """The text of a KDE settings file with key=value set in [group]; every other line stays as it was.

    A line of the key anywhere in the group is replaced in place. Otherwise the key goes after the last line
    of the group's last section, or a new section is appended when the file has none.
    """
    for part in (group, key):
        if not part or part[0] in '#;' or any(char in part for char in '[]=\n\r\0'):
            raise ValueError('invalid group or key name: %r' % part)
    if any(char in value for char in '\n\r\0'):
        raise ValueError('invalid value for %s' % key)
    entry = '%s=%s' % (key, value)
    lines = text.split('\n')
    sections, start = [], None                  # (first line, line after the last) of each [group] section
    for number, line in enumerate(lines + ['[']):    # the extra header ends the last section
        stripped = line.strip()
        if stripped.startswith('['):
            if start is not None:
                sections.append((start, number))
            start = number + 1 if stripped == '[%s]' % group else None
    replaced = False
    for first, end in sections:
        for number in range(first, end):
            name, equals, _ = lines[number].partition('=')
            if equals and name.strip() == key:
                lines[number], replaced = entry, True
    if not replaced and not sections:
        body = text if not text or text.endswith('\n') else text + '\n'
        return body + ('\n' if body else '') + '[%s]\n%s\n' % (group, entry)
    if not replaced:
        first, end = sections[-1]
        while end > first and not lines[end - 1].strip():    # blank lines that separate sections stay below
            end -= 1
        lines.insert(end, entry)
    result = '\n'.join(lines)
    return result if result.endswith('\n') else result + '\n'


def merge_assignment(text, name, value):
    """The text of a shell-style settings file (user-dirs.dirs) with the line NAME="VALUE" set; the other
    lines stay as they were."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('invalid variable name: %r' % name)
    if any(char in value for char in '\n\r\0'):
        raise ValueError('invalid value for %s' % name)
    entry = '%s="%s"' % (name, re.sub(r'(["\\$`])', r'\\\1', value))
    lines = text.split('\n')
    assignment = re.compile(r'\s*' + name + '=')
    replaced = False
    for number, line in enumerate(lines):
        if assignment.match(line):
            lines[number], replaced = entry, True
    if not replaced:
        return (text if not text or text.endswith('\n') else text + '\n') + entry + '\n'
    result = '\n'.join(lines)
    return result if result.endswith('\n') else result + '\n'


def update_file(path, change):
    """Replace the file at path by change(its text), which is empty for a file that does not exist yet.

    Nothing is written when the text stays the same. Otherwise the file is replaced in one step, keeps its
    mode, and a new file is private to its owner. Bytes that are not UTF-8 survive unchanged.
    """
    path = Path(path)
    try:
        with open(path, encoding='utf-8', errors='surrogateescape', newline='') as stream:
            old, exists = stream.read(), True
    except FileNotFoundError:
        old, exists = '', False
    new = change(old)
    if exists and new == old:
        return False
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', errors='surrogateescape', newline='') as stream:
            stream.write(new)
        if exists:
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


# Steam's desktop on-screen keyboard (Steam+X) sizes its window from the screen height as if the screen were
# landscape and pins it there with equal minimum and maximum size hints (2560x720 on a 2160 pixel tall side), so
# in a portrait side it is far wider than the side. The size of a side changes while its desktop runs (the
# supervisor restarts nothing for a layout change, the compositor resizes the nested KWin), and a rule holds
# KWin's logical coordinates, which depend on the scale. So a rule only drops the size hints, with values that
# need no scale, and a KWin script fits the window to the screen it is on whenever the window or the screen changes.
OSK_RULE_ID = 'dosplex-steam-osk'
OSK_SCRIPT_ID = 'dosplex-osk'
OSK_RULE = (
    ('Description', 'DosPlex: free the size of the Steam on-screen keyboard'),
    ('wmclass', 'steamwebhelper steam'),    # KWin compares the class part, so only the complete form matches
    ('wmclasscomplete', 'true'),
    ('wmclassmatch', '1'),
    ('title', 'Steam Input On-screen Keyboard'),
    ('titlematch', '1'),
    ('minsize', '100,50'),
    ('minsizerule', '2'),
    ('maxsize', '16384,16384'),
    ('maxsizerule', '2'),
)

OSK_METADATA = """{
    "KPlugin": {
        "Id": "%s",
        "Name": "DosPlex on-screen keyboard fit",
        "Description": "Fits Steam's on-screen keyboard to the width of the side, at its bottom.",
        "Version": "1"
    },
    "KPackageStructure": "KWin/Script",
    "X-Plasma-API": "javascript",
    "X-Plasma-MainScript": "code/main.js"
}
""" % OSK_SCRIPT_ID

# The keyboard gets the full width of the screen it is on, at the bottom, and the height that Steam asks for (its
# size hints depend on the layout: 720 pixels in a portrait side, 716 in a landscape one, and its content does not
# shrink to a shorter window). The rule above forces the hints KWin reports, so Steam's request is taken from the
# window itself: its size when it appears, and every later size it sets by itself (the script's own refits are
# told apart by the re-entry guard and by being what it set last). Sizes are KWin's logical coordinates, so the
# scale does not matter. A window with no height yet gets a third of the screen, which is what Steam picks for 16:9.
OSK_SCRIPT = """// DosPlex: fit Steam's on-screen keyboard to the side. Written by dosplex-desktop-environment.py.
const keyboards = [];
let fitting = false;

function isKeyboard(window) {
    return window.resourceClass == "steam" && window.caption == "Steam Input On-screen Keyboard";
}

function find(window) {
    return keyboards.find(function (keyboard) { return keyboard.window == window; });
}

function fit(keyboard) {
    if (fitting) return;
    const window = keyboard.window;
    const area = workspace.clientArea(KWin.FullScreenArea, window);
    const asked = keyboard.height > 0 ? keyboard.height : Math.round(area.height / 3);
    const height = Math.min(asked, area.height);
    const wanted = {x: area.x, y: area.y + area.height - height, width: area.width, height: height};
    const now = window.frameGeometry;
    if (now.x == wanted.x && now.y == wanted.y && now.width == wanted.width && now.height == wanted.height) return;
    fitting = true;
    window.frameGeometry = wanted;
    fitting = false;
    keyboard.fitted = height;
}

function track(window) {
    if (find(window) || !isKeyboard(window)) return;
    const keyboard = {window: window, height: window.frameGeometry.height, fitted: 0};
    keyboards.push(keyboard);
    window.frameGeometryChanged.connect(function () {
        // A height that the script did not set is Steam's own request.
        const height = window.frameGeometry.height;
        if (!fitting && height > 0 && height != keyboard.fitted) keyboard.height = height;
        fit(keyboard);
    });
    fit(keyboard);
}

function fitAll() {
    keyboards.forEach(fit);
}

function watch(window) {
    track(window);
    window.captionChanged.connect(function () { track(window); });
}

workspace.windowAdded.connect(watch);
workspace.windowRemoved.connect(function (window) {
    const keyboard = find(window);
    if (keyboard) keyboards.splice(keyboards.indexOf(keyboard), 1);
});
workspace.screensChanged.connect(fitAll);
workspace.virtualScreenGeometryChanged.connect(fitAll);
workspace.windowList().forEach(watch);
"""


def rule_ids(text):
    """The ids of the rule groups that a kwinrulesrc names: its rules list, else the numbered groups 1..count."""
    general = {}
    group = None
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped.startswith('['):
            group = stripped
        elif group == '[General]' and '=' in line:
            name, _, value = line.partition('=')
            general[name.strip()] = value.strip()
    listed = [item.strip() for item in general.get('rules', '').split(',') if item.strip()]
    if listed or 'rules' in general:
        return listed
    count = general.get('count', '')
    return [str(number) for number in range(1, int(count) + 1)] if count.isdigit() else []


def replace_ini_group(text, group, entries):
    """The text with every [group] section dropped and one new section of the (key, value) entries appended."""
    kept, skipping = [], False
    for line in text.split('\n'):
        if line.strip().startswith('['):
            skipping = line.strip() == '[%s]' % group
        if not skipping:
            kept.append(line)
    body = '\n'.join(kept).rstrip('\n')
    return (body + '\n\n' if body else '') + '[%s]\n%s\n' % (group, '\n'.join('%s=%s' % pair for pair in entries))


def merge_osk_rule(text):
    """The text of a kwinrulesrc with the rule of the on-screen keyboard in it.

    The rule has one group of its own, OSK_RULE_ID, which is rewritten whole. The player's other rules stay, and
    the rules list and the count in [General] name them and ours, each once.
    """
    ids = [item for item in rule_ids(text) if item != OSK_RULE_ID] + [OSK_RULE_ID]
    text = merge_ini_key(text, 'General', 'rules', ','.join(ids))
    text = merge_ini_key(text, 'General', 'count', str(len(ids)))
    return replace_ini_group(text, OSK_RULE_ID, OSK_RULE)


def install_osk_script(scripts_dir):
    """Write the KWin script that fits the on-screen keyboard below scripts_dir; True when a file changed."""
    base = Path(scripts_dir) / OSK_SCRIPT_ID
    (base / 'contents/code').mkdir(parents=True, exist_ok=True)
    changed = update_file(base / 'metadata.json', lambda _: OSK_METADATA)
    return update_file(base / 'contents/code/main.js', lambda _: OSK_SCRIPT) or changed


USAGE = ('usage: dosplex-desktop-environment.py PID | scale WIDTH HEIGHT PREFERENCE_FILE | '
         'set-key FILE GROUP KEY VALUE | set-var FILE NAME VALUE | osk-rule FILE | osk-script SCRIPTS_DIR\n')


def cli(arguments):
    """Run one command of the command line; the exit status."""
    command, rest = (arguments[0] if arguments else ''), arguments[1:]
    try:
        if command.isdigit() and not rest:
            main(int(command))
        elif command == 'scale' and len(rest) == 3:
            sys.stdout.write(scale_line(*rest))
        elif command == 'set-key' and len(rest) == 4:
            update_file(rest[0], lambda text: merge_ini_key(text, *rest[1:]))
        elif command == 'set-var' and len(rest) == 3:
            update_file(rest[0], lambda text: merge_assignment(text, *rest[1:]))
        elif command == 'osk-rule' and len(rest) == 1:
            update_file(rest[0], merge_osk_rule)
        elif command == 'osk-script' and len(rest) == 1:
            install_osk_script(rest[0])
        else:
            sys.stderr.write(USAGE)
            return 2
    except (OSError, ValueError, RuntimeError) as problem:
        sys.stderr.write('dosplex-desktop-environment: %s\n' % problem)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli(sys.argv[1:]))
