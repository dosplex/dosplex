#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Start the QML panel for Help and Feedback, as the player's own account.

The panel cannot write files or know facts about the machine.  This launcher
collects those facts, starts /usr/bin/qml6, reads the lines the panel prints,
keeps an outbox of feedback that could not be sent, and exits when the panel
closes.  Nothing stays in memory.

Usage:
    dosplex-panel-launch.py --mode feedback|help --side main|guest \
        [--page NAME] [--layout NAME] [--panel-dir DIR] [--manual-dir DIR] \
        [--qml PATH] [--manifest PATH] [--endpoint URL] \
        [--home DIR] [--os-release PATH] [--product-name PATH] \
        [--print-context]

Environment:
    DOSPLEX_TEST_PANEL_UID      override the uid that is checked (digits only)
    DOSPLEX_TEST_PANEL_SECONDS  override the 15-minute limit (seconds)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

OUTBOX_MAX = 20
OUTBOX_LINE_MAX = 4096
PANEL_TIMEOUT = 15 * 60  # seconds
OUTBOX_MARKER = 'DOSPLEX-OUTBOX '
ENDPOINT = 'https://feedback.dosplex.org/v1/feedback'


# --- Arguments -----------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='dosplex-panel-launch',
        description='Start the QML panel for Help and Feedback.',
    )
    parser.add_argument('--mode', required=True, choices=('feedback', 'help'))
    parser.add_argument('--side', required=True, choices=('main', 'guest'))
    parser.add_argument('--page', default='menu')
    parser.add_argument('--layout', default='unknown')
    # The release this launcher belongs to: it lies in <release>/libexec, whether that is the installed release
    # or the copy a running session uses.
    release = Path(__file__).resolve().parent.parent
    parser.add_argument('--panel-dir', default=str(release / 'share/dosplex/panel'))
    parser.add_argument('--manual-dir', default=str(release / 'share/dosplex/manual'))
    parser.add_argument('--qml', default='/usr/bin/qml6')
    parser.add_argument('--manifest', default=str(release / 'manifest.json'))
    parser.add_argument('--version-file', default=str(release / 'share/dosplex/VERSION'))
    parser.add_argument('--endpoint', default=None,
                        help='where feedback is sent (default: the address in --endpoint-file when root wrote one, '
                        'else %s)' % ENDPOINT)
    parser.add_argument('--endpoint-file', default='/var/lib/dosplex/feedback-endpoint')
    parser.add_argument('--home', default=str(Path.home()))
    parser.add_argument('--os-release', default='/etc/os-release')
    parser.add_argument('--product-name', default='/sys/class/dmi/id/product_name')
    parser.add_argument('--print-context', action='store_true')
    return parser.parse_args(argv)


# --- Reading the player's own files ---------------------------------------------

READ_LIMIT = 1 << 20


def _read_small(path, home=None) -> str:
    """The text of a regular file of at most READ_LIMIT bytes, or '' when it is anything else. Under `home`, no
    part of the path below the home may be a symbolic link."""
    path = Path(path)
    try:
        if home is not None:
            part = Path(home)
            for name in path.relative_to(part).parts:
                part = part / name
                if part.is_symlink():
                    return ''
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return ''
    try:
        import stat as stat_module
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode) or info.st_size > READ_LIMIT:
            return ''
        return os.read(fd, READ_LIMIT).decode('utf-8', errors='replace')
    except OSError:
        return ''
    finally:
        os.close(fd)


def endpoint_from(path, trusted_uid=0) -> str:
    """The address root wrote into the endpoint file, or '' when there is none that can be trusted: the file has
    to be a plain file of root's that nobody else can write, with one http or https address in it. It is how a
    test round sends feedback to a stand-in server instead of the real one."""
    try:
        info = os.lstat(path)
    except OSError:
        return ''
    import stat as stat_module
    if not stat_module.S_ISREG(info.st_mode) or info.st_uid != trusted_uid or info.st_mode & 0o022:
        return ''
    address = _read_small(path).strip()
    if len(address) > 300 or not re.fullmatch(r'https?://[^\s"\'<>]+', address):
        return ''
    return address


# --- Context -------------------------------------------------------------------

def _read_dosplex_version(manifest_path: str, version_path: str = '') -> str:
    # The release's own name file first; the manifest of an installed release says the same.
    named = _read_small(version_path).strip() if version_path else ''
    if named and len(named) <= 80 and '\n' not in named:
        return named
    try:
        raw = Path(manifest_path).read_text(encoding='utf-8', errors='replace')
        data = json.loads(raw)
        if isinstance(data, dict):
            version = data.get('version')
            if isinstance(version, str):
                return version
    except (OSError, ValueError):
        pass
    return 'unknown'


def _read_steamos(os_release_path: str) -> str:
    try:
        for line in Path(os_release_path).read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('VERSION_ID='):
                value = line[len('VERSION_ID='):].strip().strip('"').strip("'")
                return value
    except OSError:
        pass
    return 'unknown'


def _read_device(product_name_path: str) -> str:
    try:
        raw = Path(product_name_path).read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 'unknown'
    if not raw:
        return 'unknown'
    mapping = {'Jupiter': 'Steam Deck', 'Galileo': 'Steam Deck OLED'}
    return mapping.get(raw, raw)[:40]


def _read_steam_id(home: str) -> str:
    # Steam's own folder is often a link (~/.local/share/Steam -> ~/.steam/...): follow it up to the file, then
    # read that file bounded.
    path = Path(home) / '.local' / 'share' / 'Steam' / 'config' / 'loginusers.vdf'
    try:
        raw = _read_small(path.resolve(strict=True))
    except (OSError, RuntimeError):
        return ''
    # Parse the VDF-like structure: find all 17-digit keys and their blocks.
    # Keys look like "7656119..." with surrounding quotes.
    blocks = {}  # key -> dict of lower-cased name -> value
    # Simple parser: find "KEY" { ... } blocks
    pos = 0
    key_re = re.compile(r'"(\d{17})"\s*\{')
    while True:
        m = key_re.search(raw, pos)
        if m is None:
            break
        key = m.group(1)
        # Find the closing brace of this block
        start = m.end()
        depth = 1
        i = start
        while i < len(raw) and depth > 0:
            ch = raw[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        block_text = raw[start:i - 1]
        # Parse key-value pairs inside the block: "Name" "value"
        kv = {}
        kv_re = re.compile(r'"([^"]+)"\s+"([^"]*)"')
        for km in kv_re.finditer(block_text):
            kv[km.group(1).lower()] = km.group(2)
        blocks[key] = kv
        pos = i
    if not blocks:
        return ''
    # Choose the block with MostRecent "1"
    for key, kv in blocks.items():
        if kv.get('mostrecent') == '1':
            return key
    # Otherwise the block with the highest timestamp
    best_key = ''
    best_ts = -1
    for key, kv in blocks.items():
        ts = kv.get('timestamp', '')
        try:
            ts_val = int(ts)
        except ValueError:
            ts_val = -1
        if ts_val > best_ts:
            best_ts = ts_val
            best_key = key
    return best_key


def build_context(args) -> dict:
    return {
        'side': args.side,
        'layout': args.layout[:40],
        'dosplex': _read_dosplex_version(args.manifest, args.version_file),
        'steamos': _read_steamos(args.os_release),
        'device': _read_device(args.product_name),
        'steam_id': _read_steam_id(args.home),
        'outbox': len(_load_outbox(args.home)),
    }


# --- Outbox --------------------------------------------------------------------

def _outbox_path(home: str) -> Path:
    return Path(home) / '.local' / 'share' / 'dosplex' / 'feedback-outbox.jsonl'


def _load_outbox(home: str) -> list:
    """The JSON objects in the outbox, or [] when it is not there, not a plain file, too large, or behind a link."""
    entries = []
    for line in _read_small(_outbox_path(home), home).splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get('payload'), dict):
            entries.append({'queued': str(obj.get('queued', '')), 'payload': obj['payload']})
    return entries[-OUTBOX_MAX:]


def _write_outbox(home: str, entries: list) -> bool:
    """Write entries to the outbox file. Returns True when the file was written."""
    path = _outbox_path(home)
    folder = path.parent
    part = Path(home)
    for name in path.relative_to(part).parts:          # .local, share, dosplex, the file: none may be a link
        part = part / name
        if part.is_symlink():
            return False
    # Keep at most 20 entries
    if len(entries) > OUTBOX_MAX:
        entries = entries[-OUTBOX_MAX:]
    # Write through a temp file in the same folder
    # The folder is the account's own Dosplex folder and usually exists, with other things in it: it is made
    # private only when this program makes it. The outbox file itself is always 0600.
    try:
        if not folder.exists():
            folder.mkdir(parents=True, mode=0o700)
    except OSError:
        return False
    import tempfile
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(folder), suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                for entry in entries:
                    f.write(json.dumps(entry, separators=(',', ':')) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, str(path))
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return False
    except OSError:
        return False
    return True


def _outbox_unchanged(old_entries: list, new_entries: list) -> bool:
    """Check whether the new entries differ from the old ones."""
    if len(old_entries) != len(new_entries):
        return False
    for old, new in zip(old_entries, new_entries):
        if old != new:
            return False
    return True


# --- Panel output reading -------------------------------------------------------

def _parse_outbox_line(line: str):
    """Parse a line that may carry a DOSPLEX-OUTBOX marker.

    Returns (queued_text, payload_dict) on success, or (None, None) on failure.
    """
    if len(line.encode('utf-8', errors='replace')) > OUTBOX_LINE_MAX:
        return None, None
    idx = line.find(OUTBOX_MARKER)
    if idx < 0:
        return None, None
    json_text = line[idx + len(OUTBOX_MARKER):]
    try:
        obj = json.loads(json_text)
    except ValueError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    queued = obj.get('queued')
    payload = obj.get('payload')
    if not isinstance(queued, str) or not isinstance(payload, dict):
        return None, None
    return queued, payload


# --- Sending -------------------------------------------------------------------

def _post_payload(endpoint: str, payload: dict) -> int:
    """POST payload to endpoint. Returns the HTTP status, or -1 on no connection."""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        endpoint, data=data, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return -1


def _send_thread(home: str, endpoint: str, outbox_entries: list, done_event: threading.Event):
    """Send waiting entries in outbox_entries, oldest first.

    Marks entries done by setting entry['_done'] = True.
    Sets done_event when finished.
    """
    for entry in outbox_entries:
        if entry.get('_done'):
            continue
        payload = entry.get('payload')
        if not isinstance(payload, dict):
            entry['_done'] = True
            continue
        status = _post_payload(endpoint, payload)
        if 200 <= status < 300:
            entry['_done'] = True
        elif 400 <= status < 500 and status != 429:
            entry['_done'] = True
        else:
            # Keep this entry and all later ones: stop sending
            break
    done_event.set()


# --- Main ----------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    uid_env = os.environ.get('DOSPLEX_TEST_PANEL_UID', '')
    if uid_env.isdigit():
        uid = int(uid_env)
    else:
        uid = os.geteuid()
    if uid == 0:
        print('The panel runs as a player\'s account, never as root.')
        return 2

    if args.endpoint is None:
        owner = os.environ.get('DOSPLEX_TEST_ENDPOINT_OWNER', '')
        args.endpoint = endpoint_from(args.endpoint_file, int(owner) if owner.isdigit() else 0) or ENDPOINT
    context = build_context(args)

    if args.print_context:
        print(json.dumps(context, separators=(',', ':')))
        return 0

    panel_qml = Path(args.panel_dir) / 'Panel.qml'
    if not Path(args.qml).is_file():
        print('The qml program is not there.')
        return 3
    if not panel_qml.is_file():
        print('The panel file is not there.')
        return 3
    # A release that does not carry the form has no feedback: nothing is opened, and nothing that waits in the
    # outbox of an earlier release is sent.
    if args.mode == 'feedback' and not (Path(args.panel_dir) / 'Feedback.qml').is_file():
        print('The feedback form is not part of this release.')
        return 3

    context_json = json.dumps(context, separators=(',', ':'))
    cmd = [args.qml, str(panel_qml), '--'] + (['--feedback'] if args.mode == 'feedback' else ['--help', args.page]) + [
        '--context', context_json,
        '--manual-dir', args.manual_dir,
        '--endpoint', args.endpoint,
    ]

    env = dict(os.environ)
    env['QML_XHR_ALLOW_FILE_READ'] = '1'
    env['QT_QUICK_CONTROLS_STYLE'] = 'Basic'
    # Qt writes a program's messages to the system journal when its output is not a terminal. The one line the
    # panel prints holds what a person wrote: it has to come here, into the pipe, and nowhere else.
    env['QT_FORCE_STDERR_LOGGING'] = '1'
    env['QT_LOGGING_TO_CONSOLE'] = '1'

    # The compositor hands the panel its own connection as WAYLAND_SOCKET, an open descriptor: it has to survive
    # into qml6, which subprocess would otherwise close.
    socket_fd = os.environ.get('WAYLAND_SOCKET', '')
    wayland_fds = (int(socket_fd),) if socket_fd.isdigit() else ()
    timeout_env = os.environ.get('DOSPLEX_TEST_PANEL_SECONDS', '')
    panel_timeout = int(timeout_env) if timeout_env.isdigit() else PANEL_TIMEOUT

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        pass_fds=wayland_fds,
    )

    waiting = _load_outbox(args.home)
    for entry in waiting:
        entry.pop('_done', None)
    done_event = threading.Event()
    send_thread = None
    if args.mode == 'feedback' and waiting:
        send_thread = threading.Thread(
            target=_send_thread, args=(args.home, args.endpoint, waiting, done_event),
            daemon=True,
        )
        send_thread.start()

    new_entries = []
    timeout_reached = [False]

    def _timeout():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        timeout_reached[0] = True

    timer = threading.Timer(panel_timeout, _timeout)
    timer.daemon = True
    timer.start()

    def _on_signal(signum, frame):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        timer.cancel()
        # Write outbox and exit
        remaining_old = [e for e in waiting if not e.get('_done')]
        final = remaining_old + list(new_entries)
        old_entries = _load_outbox(args.home)
        if not _outbox_unchanged(old_entries, final):
            _write_outbox(args.home, final)
        os._exit(1)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    for line in process.stdout:
        line_text = line.decode('utf-8', errors='replace').rstrip('\n').rstrip('\r')
        queued, payload = _parse_outbox_line(line_text)
        if queued is not None and payload is not None:
            new_entries.append({'queued': queued, 'payload': payload})
        elif OUTBOX_MARKER not in line_text:
            # A line with the marker holds what a person wrote, also when it is too long or broken: never echoed.
            sys.stderr.write(line_text + '\n')
            sys.stderr.flush()
    process.stdout.close()
    timer.cancel()
    process.wait()

    if send_thread is not None:
        done_event.wait(timeout=12)

    remaining_old = [e for e in waiting if not e.get('_done')]
    # Merge: old entries not done + new entries
    final = remaining_old + new_entries
    old_entries = _load_outbox(args.home)
    if not _outbox_unchanged(old_entries, final):
        _write_outbox(args.home, final)

    status = process.returncode
    if status < 0:
        status = 1
    return status


if __name__ == '__main__':
    sys.exit(main())
