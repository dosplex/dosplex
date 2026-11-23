#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""A side's Desktop mode in the session launcher: the profile that outlives one launch, and the scale it opens at.

prepare_desktop_profile and set_desktop_setting of guest/dosplex-user-side-session.sh are the real shell
functions, run in bash for a user whose home is a temporary directory (no KDE, no systemd, no root, and a
PATH that holds only the tools they use):

  - the persistent directories and their modes, the files the launcher writes, and the side user's real
    configuration left alone;
  - the scale in the KWin wrapper and in the launch log, from the size of the side or the player's
    desktop-scale file;
  - the player's own settings after a second launch, with kwriteconfig6 and without it;
  - the trial's own profile root (DOSPLEX_DESKTOP_CONFIG_ROOT);
  - the Flatpak folders: add_flatpak_exports puts the exports of every Flatpak installation on XDG_DATA_DIRS and PATH,
    the launch of the desktop gives them to Plasma and to the nested KWin, and the source shows that nothing resets
    them on the way;
  - the two places that start the nested KWin, which only a real desktop can run, checked in the source;
  - migrate_legacy_user_data: what the old product name (DuplOS) left in the player's home moves once.

tests/desktop-helper-test.py covers the helper itself.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'guest/dosplex-user-side-session.sh'
# The launcher looks for its helper beside this script, as it does in libexec.
SHIM = ROOT / 'packaging/steamos/steamos-session-select-shim.sh'
BASH = shutil.which('bash')
# What the two shell functions run: the tools they use, and nothing else from the host (a host with KDE
# must not lend them its own kwriteconfig6).
TOOLS = ('cat', 'chmod', 'cp', 'date', 'dirname', 'ln', 'mkdir', 'rm')

KWINRC = ('[Compositing]\nBackend=OpenGL\n\n'
          '[Xwayland]\nScale=2\nXwaylandEisNoPrompt=false\n\n'
          '[Windows]\nBorderSnapZone=10\n')

KWINRC_GENERATED = '[Xwayland]\nXwaylandEisNoPrompt=true\n\n[Plugins]\ndosplex-oskEnabled=true\n'


def shell_function(name):
    match = re.search(r'^' + name + r'\(\) \{\n.*?^\}\n', LAUNCHER.read_text(), re.M | re.S)
    assert match, name
    return match.group(0)


class Profile:
    """prepare_desktop_profile of the launcher, run in bash for a user whose home is a temporary directory."""
    SCRIPT = ('set -euo pipefail\n'
              'home_dir="$HOME"; side=left; owner=tester; workspace_root="$HOME/Dosplex/left"\n'
              + shell_function('set_desktop_setting') + shell_function('prepare_desktop_profile') +
              'prepare_desktop_profile\n'
              "printf '%s\\0' \"XDG_CONFIG_HOME=$XDG_CONFIG_HOME\" \"XDG_DATA_HOME=$XDG_DATA_HOME\" "
              "\"XDG_CONFIG_DIRS=$XDG_CONFIG_DIRS\" \"XDG_MENU_PREFIX=$XDG_MENU_PREFIX\" \"PATH=$PATH\" "
              "\"desktop_scale=$desktop_scale\" \"desktop_scale_reason=$desktop_scale_reason\"\n")

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-desktop-profile-')
        test.addCleanup(temporary.cleanup)
        self.test = test
        self.root = Path(temporary.name)
        self.home, self.runtime, self.bin = (self.root / name for name in ('home', 'runtime', 'bin'))
        for directory in (self.home, self.runtime, self.bin):
            directory.mkdir(mode=0o700)
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.log = self.root / 'launch.log'
        self.profile = self.home / '.local/share/dosplex/desktop'
        self.config, self.data = self.profile / 'config', self.profile / 'data'
        self.wrapper = self.runtime / 'desktop-bin/kwin_wayland_wrapper'

    def launch(self, width='1920', height='2160', **environment):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'XDG_RUNTIME_DIR': str(self.runtime),
               'DOSPLEX_SHIM_SCRIPT': str(SHIM), 'DOSPLEX_LAUNCH_LOG': str(self.log)}
        if width is not None:
            env['DOSPLEX_OUTPUT_WIDTH'] = width
        if height is not None:
            env['DOSPLEX_OUTPUT_HEIGHT'] = height
        env.update(environment)
        done = subprocess.run([BASH, '-c', self.SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        values = dict(item.split('=', 1) for item in done.stdout.split('\0') if item)
        return SimpleNamespace(returncode=done.returncode, stderr=done.stderr, values=values)

    def succeed(self, *arguments, **environment):
        done = self.launch(*arguments, **environment)
        self.test.assertEqual(done.returncode, 0, done.stderr)
        return done

    def preference(self, text):
        self.profile.mkdir(parents=True, exist_ok=True)
        (self.profile / 'desktop-scale').write_text(text)

    def stub_kwriteconfig6(self, status):
        """A kwriteconfig6 that records its arguments and changes no file; returns the record's path."""
        record = self.root / 'kwriteconfig6.log'
        stub = self.bin / 'kwriteconfig6'
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + "'" + str(record) + "'\nexit " + str(status) + '\n')
        stub.chmod(0o755)
        return record

    def wrapper_scale(self):
        found = re.findall(r' --scale (\S+) "\$@"$', self.wrapper.read_text(), re.M)
        self.test.assertEqual(len(found), 1, self.wrapper.read_text())
        return found[0]


class LauncherProfile(unittest.TestCase):
    def test_the_first_launch_builds_the_profile_in_the_home_of_the_side_user(self):
        run = Profile(self)
        done = run.succeed()
        for directory in (run.profile, run.config, run.data):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(done.values['XDG_CONFIG_HOME'], str(run.config))
        self.assertEqual(done.values['XDG_DATA_HOME'], str(run.data))
        # The defaults stay under the session runtime, and nothing else of the profile does.
        self.assertEqual(done.values['XDG_CONFIG_DIRS'], str(run.runtime / 'desktop-defaults'))
        self.assertEqual(sorted(entry.name for entry in run.runtime.iterdir()), ['desktop-bin', 'desktop-defaults'])
        self.assertEqual(done.values['XDG_MENU_PREFIX'], 'plasma-')
        self.assertEqual(done.values['PATH'].split(':')[0], str(run.runtime / 'desktop-bin'))
        # Generated settings.
        self.assertEqual((run.config / 'startkderc').read_text(), '[General]\nsystemdBoot=false\n')
        self.assertEqual((run.config / 'kwinrc').read_text(), KWINRC_GENERATED)
        self.assertEqual((run.config / 'user-dirs.dirs').read_text(), 'XDG_DESKTOP_DIR="%s/Desktop"\n' % run.data)
        # Steam's on-screen keyboard: the rule frees its size and the script fits it to the side.
        self.assertIn('rules=dosplex-steam-osk\ncount=1\n', (run.config / 'kwinrulesrc').read_text())
        script = run.data / 'kwin/scripts/dosplex-osk'
        self.assertTrue((script / 'metadata.json').is_file())
        self.assertIn('Steam Input On-screen Keyboard', (script / 'contents/code/main.js').read_text())
        steam = (run.config / 'autostart/dosplex-steam.desktop').read_text()
        self.assertIn('Exec=steam -silent -nocrashdialog -nodumpreport\n', steam)
        entry = (run.data / 'applications/dosplex-return.desktop').read_text()
        self.assertIn('Name=Return to Gaming Mode\n', entry)
        self.assertIn('Exec=steamosctl switch-to-game-mode\n', entry)
        self.assertEqual((run.data / 'Desktop/dosplex-return.desktop').read_text(), entry)
        self.assertEqual(stat.S_IMODE((run.data / 'Desktop/dosplex-return.desktop').stat().st_mode), 0o700)
        # The side user's real configuration and the rest of the home are not touched.
        self.assertFalse((run.home / '.config').exists())
        self.assertEqual(sorted(entry.name for entry in run.home.iterdir()), ['.local'])
        self.assertEqual(sorted(entry.name for entry in (run.home / '.local').iterdir()), ['share'])
        self.assertEqual(sorted(entry.name for entry in (run.home / '.local/share').iterdir()), ['dosplex'])
        self.assertEqual(sorted(entry.name for entry in (run.home / '.local/share/dosplex').iterdir()), ['desktop'])

    def test_the_scale_follows_the_size_of_the_side_in_the_wrapper_and_in_the_log(self):
        # One size for each default; the decision itself is tested for every size in desktop-helper-test.py.
        for (width, height), scale in {('3840', '2160'): '2', ('960', '1080'): '1.5', ('1920', '540'): '1',
                                       ('1280', '800'): '1'}.items():
            with self.subTest(width=width, height=height):
                run = Profile(self)
                done = run.succeed(width, height)
                self.assertEqual(done.values['desktop_scale'], scale)
                self.assertEqual(run.wrapper_scale(), scale)
                self.assertEqual(stat.S_IMODE(run.wrapper.stat().st_mode), 0o700)
                self.assertIn('exec env WAYLAND_DISPLAY="$DOSPLEX_HOST_SOCKET" /usr/bin/kwin_wayland_wrapper '
                              '--fullscreen true --no-lockscreen --scale ' + scale + ' "$@"', run.wrapper.read_text())
                log = run.log.read_text().splitlines()
                self.assertRegex(log[0], r'^\[[0-9T:+-]+\] side=left owner=tester payload=desktop$')
                self.assertEqual(log[1], 'desktop output=%sx%s scale=%s reason=default for %sx%s'
                                 % (width, height, scale, width, height))
                self.assertEqual(log[2], 'desktop config=%s data=%s' % (run.config, run.data))
                self.assertEqual(log[3], 'desktop scale report: no supervisor control socket')
                self.assertEqual(len(log), 4)

    def test_the_kwin_wrapper_leaves_the_environment_as_it_is(self):
        # It only names the socket that KWin connects to. The environment of Plasma, the Flatpak folders in it,
        # goes on to KWin unchanged.
        run = Profile(self)
        run.succeed()
        text = run.wrapper.read_text()
        self.assertNotRegex(text, r'PATH|XDG_DATA|FLATPAK')
        self.assertNotRegex(text, r'\benv\s+(-i|-u|--ignore-environment|--unset)\b')

    def test_the_chosen_scale_is_reported_to_the_supervisor_before_the_kwin_starts(self):
        # The compositor sends the nested KWin its preferred scale (T-424) and learns the scale from the
        # supervisor; the client below stands for dosplex-side-supervisor.py client-desktop-scale.
        for width, height, scale in (('3840', '2160', '2'), ('960', '1080', '1.5'), ('1280', '800', '1')):
            with self.subTest(scale=scale):
                run = Profile(self)
                client = run.root / 'client.py'
                record = run.root / 'record'
                client.write_text('import sys\nopen(%r, "a").write(" ".join(sys.argv[1:]) + "\\n")\n' % str(record))
                done = run.succeed(width, height, DOSPLEX_SESSION_CONTROL=str(run.root / 'control.sock'),
                                   DOSPLEX_SESSION_INSTANCE='a' * 32, DOSPLEX_SESSION_CONTROL_CLIENT=str(client))
                self.assertEqual(done.values['desktop_scale'], scale)
                self.assertEqual(record.read_text(), 'client-desktop-scale %s\n' % scale)
                self.assertIn('desktop scale report: sent to the supervisor', run.log.read_text())

    def test_a_supervisor_that_refuses_the_scale_does_not_stop_the_launch(self):
        run = Profile(self)
        client = run.root / 'client.py'
        client.write_text('raise SystemExit(1)\n')
        done = run.succeed(DOSPLEX_SESSION_CONTROL=str(run.root / 'control.sock'),
                           DOSPLEX_SESSION_INSTANCE='a' * 32, DOSPLEX_SESSION_CONTROL_CLIENT=str(client))
        self.assertEqual(run.wrapper_scale(), done.values['desktop_scale'])
        self.assertIn('desktop scale report: the supervisor refused it; the compositor keeps scale 1',
                      run.log.read_text())

    def test_an_unknown_output_size_gives_scale_one(self):
        for width, height in ((None, None), ('1920', None), ('wide', 'tall')):
            with self.subTest(width=width, height=height):
                run = Profile(self)
                done = run.succeed(width, height)
                self.assertEqual((done.values['desktop_scale'], run.wrapper_scale()), ('1', '1'))
                self.assertIn('scale=1 reason=default for an unknown output size', run.log.read_text())

    def test_the_preference_file_sets_the_scale_and_the_log_says_why(self):
        run = Profile(self)
        run.preference('1.5\n')
        done = run.succeed('3840', '2160')
        self.assertEqual((done.values['desktop_scale'], run.wrapper_scale()), ('1.5', '1.5'))
        self.assertIn('scale=1.5 reason=desktop-scale preference (default would be 2)', run.log.read_text())
        run.preference('3')
        self.assertEqual(run.succeed('1280', '800').values['desktop_scale'], '3')

    def test_a_preference_that_is_not_valid_is_ignored_and_logged(self):
        for text in ('2.0', '', 'huge', '5'):
            with self.subTest(text=text):
                run = Profile(self)
                run.preference(text)
                done = run.succeed('1920', '2160')
                self.assertEqual((done.values['desktop_scale'], run.wrapper_scale()), ('2', '2'))
                self.assertIn('scale=2 reason=ignored desktop-scale preference', run.log.read_text())
        run = Profile(self)
        run.profile.mkdir(parents=True)
        (run.profile / 'desktop-scale').mkdir()              # something else where the file belongs
        self.assertEqual(run.succeed('1920', '1080').values['desktop_scale'], '1.5')

    def test_a_second_launch_keeps_what_the_player_changed(self):
        run = Profile(self)
        run.succeed()
        (run.config / 'kwinrc').write_text(KWINRC)
        (run.config / 'startkderc').write_text('[General]\nsystemdBoot=true\nConfirmLogout=false\n')
        (run.config / 'kdeglobals').write_text('[KScreen]\nScaleFactor=2\n')
        (run.config / 'plasma-org.kde.plasma.desktop-appletsrc').write_text('[Containments][1]\nplugin=panel\n')
        (run.config / 'user-dirs.dirs').write_text('XDG_DOWNLOAD_DIR="/p/Downloads"\nXDG_DESKTOP_DIR="/old"\n')
        (run.config / 'autostart/mine.desktop').write_text('[Desktop Entry]\nName=Mine\n')
        (run.data / 'Desktop/notes.txt').write_text('mine')
        (run.data / 'applications/mine.desktop').write_text('[Desktop Entry]\nName=Mine\n')
        (run.config / 'autostart/dosplex-steam.desktop').write_text('changed')     # DosPlex's own entry comes back
        run.succeed()
        self.assertEqual((run.config / 'kwinrc').read_text(),
                         KWINRC.replace('XwaylandEisNoPrompt=false', 'XwaylandEisNoPrompt=true')
                         + '\n[Plugins]\ndosplex-oskEnabled=true\n')
        self.assertEqual((run.config / 'startkderc').read_text(), '[General]\nsystemdBoot=false\nConfirmLogout=false\n')
        self.assertEqual((run.config / 'user-dirs.dirs').read_text(),
                         'XDG_DOWNLOAD_DIR="/p/Downloads"\nXDG_DESKTOP_DIR="%s/Desktop"\n' % run.data)
        self.assertEqual((run.config / 'kdeglobals').read_text(), '[KScreen]\nScaleFactor=2\n')
        self.assertEqual((run.config / 'plasma-org.kde.plasma.desktop-appletsrc').read_text(),
                         '[Containments][1]\nplugin=panel\n')
        self.assertEqual((run.config / 'autostart/mine.desktop').read_text(), '[Desktop Entry]\nName=Mine\n')
        self.assertEqual((run.data / 'Desktop/notes.txt').read_text(), 'mine')
        self.assertEqual((run.data / 'applications/mine.desktop').read_text(), '[Desktop Entry]\nName=Mine\n')
        self.assertIn('Exec=steam -silent', (run.config / 'autostart/dosplex-steam.desktop').read_text())

    def test_the_entries_under_the_old_name_are_removed(self):
        # A desktop profile moved over from DuplOS still holds DuplOS's own entries; its Steam autostart would start
        # a second Steam beside DosPlex's (seen on the Steam Machine, 2026-11-02: Guest's pointer did not move).
        run = Profile(self)
        run.succeed()
        old = (run.config / 'autostart/duplos-steam.desktop', run.data / 'applications/duplos-return.desktop',
               run.data / 'Desktop/duplos-return.desktop')
        for path in old:
            path.write_text('[Desktop Entry]\nExec=steam -silent\n')
        (run.config / 'autostart/duplos-mine.desktop').write_text('[Desktop Entry]\nName=Mine\n')
        run.succeed()
        for path in old:
            self.assertFalse(path.exists(), path)
        self.assertTrue((run.config / 'autostart/duplos-mine.desktop').exists())
        self.assertEqual(sorted(p.name for p in (run.config / 'autostart').glob('*steam*')), ['dosplex-steam.desktop'])

    def test_launches_that_change_nothing_leave_the_files_alone(self):
        run = Profile(self)
        run.succeed()
        names = ('startkderc', 'kwinrc', 'kwinrulesrc', 'user-dirs.dirs')
        before = {name: (run.config / name).stat() for name in names}
        run.succeed()
        for name in names:
            after = (run.config / name).stat()
            self.assertEqual((before[name].st_ino, before[name].st_mtime_ns), (after.st_ino, after.st_mtime_ns), name)

    def test_kwriteconfig6_writes_the_settings_when_the_host_has_it(self):
        run = Profile(self)
        record = run.stub_kwriteconfig6(0)
        run.succeed()
        self.assertEqual(record.read_text().splitlines(), [
            '--file %s/startkderc --group General --key systemdBoot false' % run.config,
            '--file %s/kwinrc --group Xwayland --key XwaylandEisNoPrompt true' % run.config,
            '--file %s/kwinrc --group Plugins --key dosplex-oskEnabled true' % run.config])
        # It did the work (the stub changes nothing), so the helper's own edit did not run behind it.
        self.assertFalse((run.config / 'startkderc').exists() or (run.config / 'kwinrc').exists())

    def test_the_helper_edits_the_files_when_kwriteconfig6_is_missing_or_fails(self):
        for status in (None, 1):
            with self.subTest(kwriteconfig6=status):
                run = Profile(self)
                record = run.stub_kwriteconfig6(status) if status is not None else None
                run.succeed()
                self.assertEqual((run.config / 'kwinrc').read_text(), KWINRC_GENERATED)
                self.assertEqual((run.config / 'startkderc').read_text(), '[General]\nsystemdBoot=false\n')
                if record:
                    self.assertEqual(len(record.read_text().splitlines()), 3)

    def test_the_helper_merges_into_the_players_file_when_kwriteconfig6_is_missing(self):
        run = Profile(self)
        run.succeed()
        (run.config / 'kwinrc').write_text(KWINRC)
        run.succeed()
        self.assertEqual((run.config / 'kwinrc').read_text(),
                         KWINRC.replace('XwaylandEisNoPrompt=false', 'XwaylandEisNoPrompt=true')
                         + '\n[Plugins]\ndosplex-oskEnabled=true\n')

    def test_a_named_root_replaces_the_default_one(self):
        run = Profile(self)
        trial = run.root / 'trial' / 'desktop'
        done = run.succeed(DOSPLEX_DESKTOP_CONFIG_ROOT=str(trial))
        self.assertEqual((done.values['XDG_CONFIG_HOME'], done.values['XDG_DATA_HOME']),
                         (str(trial / 'config'), str(trial / 'data')))
        for directory in (trial, trial / 'config', trial / 'data'):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual((trial / 'config/kwinrc').read_text(), KWINRC_GENERATED)
        self.assertEqual(list(run.home.iterdir()), [])        # nothing in the home, not even .local
        (trial / 'desktop-scale').write_text('2.5')
        done = run.succeed('1280', '800', DOSPLEX_DESKTOP_CONFIG_ROOT=str(trial))
        self.assertEqual(done.values['desktop_scale'], '2.5')

    def test_a_root_that_cannot_be_used_stops_the_launch(self):
        run = Profile(self)
        file_instead = run.root / 'a-file'
        file_instead.write_text('')
        for root, message in (('relative/path', 'absolute path'), (str(file_instead), '')):
            with self.subTest(root=root):
                done = run.launch(DOSPLEX_DESKTOP_CONFIG_ROOT=root)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(done.values, {})
                self.assertIn(message, done.stderr)
        self.assertFalse(run.wrapper.exists())

    def test_directories_that_exist_already_become_private(self):
        run = Profile(self)
        for directory in (run.profile, run.config, run.data):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o755)
        run.succeed()
        for directory in (run.profile, run.config, run.data):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)

    def test_a_scale_answer_that_is_not_a_scale_gives_scale_one(self):
        # A helper that answers with nonsense, or fails, must not put anything odd into the KWin command.
        for answer in ('print("banana split")', 'print("4 too big")', 'print("20 x")', 'print("2.5.1 x")',
                       'print("$(touch hacked) x")', 'raise SystemExit(3)'):
            with self.subTest(answer=answer):
                run = Profile(self)
                libexec = run.root / 'libexec'
                libexec.mkdir()
                (libexec / 'dosplex-desktop-environment.py').write_text(
                    'import sys\nif sys.argv[1] == "scale":\n    ' + answer.replace('\n', ' ') + '\n')
                done = run.succeed(DOSPLEX_SHIM_SCRIPT=str(libexec / 'steamos-session-select-shim.sh'))
                self.assertEqual((done.values['desktop_scale'], run.wrapper_scale()), ('1', '1'))
                self.assertEqual(done.values['desktop_scale_reason'], 'no usable answer from the scale helper')
                self.assertFalse((run.root / 'hacked').exists())
                self.assertIn('scale=1 reason=no usable answer from the scale helper', run.log.read_text())

    def test_the_launch_stops_when_the_helper_is_missing(self):
        run = Profile(self)
        done = run.launch(DOSPLEX_SHIM_SCRIPT=str(run.root / 'libexec' / 'steamos-session-select-shim.sh'))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('desktop helper missing', done.stderr)
        self.assertFalse(run.profile.exists())


def flatpak_stub(directory, lines=(), status=0, record=None):
    """A flatpak in `directory` that prints `lines` (what `flatpak --installations` prints) and exits with `status`.

    With `record`, every call appends a line to that file: its arguments, and what GIO_USE_VFS and G_MESSAGES_DEBUG
    were in its environment ("unset" when they were not set)."""
    body = ['#!/bin/sh']
    if record is not None:
        body.append('printf "%s|%s|%s\\n" "$*" "${GIO_USE_VFS-unset}" "${G_MESSAGES_DEBUG-unset}" >> '
                    + shlex.quote(str(record)))
    body += ['printf "%s\\n" ' + shlex.quote(str(line)) for line in lines]
    body.append('exit %d' % status)
    stub = Path(directory) / 'flatpak'
    stub.write_text('\n'.join(body) + '\n')
    stub.chmod(0o755)


def flatpak_installation(path, share=True, bin=True):
    """A Flatpak installation at `path` with the exports folders that are asked for; returns the path."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for name, wanted in (('share', share), ('bin', bin)):
        if wanted:
            (path / 'exports' / name).mkdir(parents=True, exist_ok=True)
    return path


class Exports:
    """add_flatpak_exports of the launcher, run in a shell for a user whose home is a temporary directory.

    The installations are directories below the temporary root. The PATH of the run holds one directory, with the
    stand-in for flatpak when a test makes one, so nothing of the host shows through; FLATPAK_SYSTEM_DIR points at a
    directory that is not there unless a test says otherwise, because the default system installation would be the
    one of the host."""
    SCRIPT = ('{options}\n' + shell_function('add_flatpak_exports') + 'add_flatpak_exports\n'
              "printf '%s\\0' \"XDG_DATA_DIRS=${XDG_DATA_DIRS-unset}\" \"PATH=$PATH\" "
              "\"FLATPAK_USER_DIR=$FLATPAK_USER_DIR\" \"flatpak_user_dir=$flatpak_user_dir\"\n")
    # How to run the text of a script in each shell, and the options that make that shell as strict as the launcher.
    SHELLS = {'bash': ([BASH, '-c'], 'set -euo pipefail'),
              'sh': ([shutil.which('sh'), '-c'], 'set -eu'),
              'dash': ([shutil.which('dash'), '-c'], 'set -eu'),
              'busybox': ([shutil.which('busybox'), 'sh', '-c'], 'set -eu')}

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-flatpak-exports-')
        test.addCleanup(temporary.cleanup)
        self.test = test
        self.root = Path(temporary.name)
        self.home, self.bin = self.root / 'home', self.root / 'bin'
        self.home.mkdir()
        self.bin.mkdir()
        self.user = self.home / '.local/share/flatpak'       # where Flatpak keeps the installation of a user
        self.system = self.root / 'var/lib/flatpak'          # stands in for /var/lib/flatpak
        self.record = self.root / 'flatpak.log'

    def flatpak(self, *lines, status=0):
        flatpak_stub(self.bin, lines, status, self.record)

    def run(self, shell='bash', **environment):
        argv, options = self.SHELLS[shell]
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'FLATPAK_SYSTEM_DIR': str(self.root / 'no-system')}
        env.update(environment)
        env = {name: value for name, value in env.items() if value is not None}      # None takes a variable away
        return subprocess.run(argv + [self.SCRIPT.replace('{options}', options)], env=env, cwd=self.root,
                              capture_output=True, text=True, timeout=60)

    def succeed(self, shell='bash', **environment):
        """What the function leaves behind: XDG_DATA_DIRS ("unset" when it is not set), PATH, FLATPAK_USER_DIR and
        flatpak_user_dir. It must not print anything."""
        done = self.run(shell, **environment)
        self.test.assertEqual((done.returncode, done.stderr), (0, ''), done.stdout)
        return dict(item.split('=', 1) for item in done.stdout.split('\0') if item)

    @staticmethod
    def share(*installations):
        return ':'.join('%s/exports/share' % path for path in installations)

    @staticmethod
    def commands(*installations):
        return ':'.join('%s/exports/bin' % path for path in installations)


class FlatpakExports(unittest.TestCase):
    DEFAULT = ':/usr/local/share:/usr/share'        # XDG_DATA_DIRS when nothing set it

    def test_the_exports_of_every_installation_go_in_front_of_xdg_data_dirs_and_behind_path(self):
        run = Exports(self)
        user = flatpak_installation(run.user)
        system = flatpak_installation(run.system)
        card = flatpak_installation(run.root / 'run/media/card/flatpak')       # one of /etc/flatpak/installations.d
        run.flatpak(system, card)
        done = run.succeed()
        # The installation of the user first, then the system ones in the order that Flatpak names them, and the
        # default search path of the specification behind them (nothing set the variable).
        self.assertEqual(done['XDG_DATA_DIRS'], run.share(user, system, card) + self.DEFAULT)
        # The commands go behind everything that was on PATH, in the same order.
        self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(user, system, card)))
        self.assertEqual((done['flatpak_user_dir'], done['FLATPAK_USER_DIR']), (str(user), str(user)))

    def test_a_search_path_that_is_set_stays_behind_the_new_folders(self):
        run = Exports(self)
        system = flatpak_installation(run.system)
        run.flatpak(system)
        self.assertEqual(run.succeed(XDG_DATA_DIRS='/opt/share:/usr/share')['XDG_DATA_DIRS'],
                         run.share(system) + ':/opt/share:/usr/share')
        # Set but empty counts as not set, as it does in flatpak.sh.
        self.assertEqual(run.succeed(XDG_DATA_DIRS='')['XDG_DATA_DIRS'], run.share(system) + self.DEFAULT)

    def test_a_folder_that_does_not_exist_is_left_out(self):
        run = Exports(self)
        # The installation of the user has no exports (it was made by a Flatpak that exported nothing yet), the first
        # system installation has only menu entries, the second only commands, the third is not there at all and the
        # fourth has files where the folders belong.
        flatpak_installation(run.user, share=False, bin=False)
        first = flatpak_installation(run.root / 'first', bin=False)
        second = flatpak_installation(run.root / 'second', share=False)
        third = run.root / 'third'
        fourth = flatpak_installation(run.root / 'fourth', share=False, bin=False)
        (fourth / 'exports').mkdir()
        (fourth / 'exports/share').write_text('')
        (fourth / 'exports/bin').write_text('')
        run.flatpak(first, second, third, fourth)
        done = run.succeed()
        self.assertEqual(done['XDG_DATA_DIRS'], run.share(first) + self.DEFAULT)
        self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(second)))
        # With nothing to add, nothing is set: XDG_DATA_DIRS stays unset and PATH stays what it was.
        run = Exports(self)
        run.flatpak(run.root / 'nothing')
        done = run.succeed()
        self.assertEqual((done['XDG_DATA_DIRS'], done['PATH']), ('unset', str(run.bin)))

    def test_a_folder_that_is_there_already_is_not_added_again(self):
        run = Exports(self)
        user = flatpak_installation(run.user)
        system = flatpak_installation(run.system)
        run.flatpak(system, system, user)                      # an installation that is named twice counts once
        done = run.succeed(XDG_DATA_DIRS='%s/exports/share/:/usr/share' % system,         # with a slash at its end
                           PATH='%s:%s' % (run.bin, run.commands(user)))
        self.assertEqual(done['XDG_DATA_DIRS'], '%s:%s/exports/share/:/usr/share' % (run.share(user), system))
        self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(user, system)))

    def test_without_flatpak_the_default_system_installation_is_used(self):
        run = Exports(self)                                      # there is no flatpak on the PATH
        system = flatpak_installation(run.system)
        done = run.succeed(FLATPAK_SYSTEM_DIR=str(system))
        self.assertEqual(done['XDG_DATA_DIRS'], run.share(system) + self.DEFAULT)
        self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(system)))
        # Unless it is told otherwise it is /var/lib/flatpak, which this host may have or not (a test cannot make it).
        done = run.succeed(FLATPAK_SYSTEM_DIR=None)
        share = ['/var/lib/flatpak/exports/share'] if os.path.isdir('/var/lib/flatpak/exports/share') else []
        commands = ['/var/lib/flatpak/exports/bin'] if os.path.isdir('/var/lib/flatpak/exports/bin') else []
        self.assertEqual(done['XDG_DATA_DIRS'], (':'.join(share) + self.DEFAULT) if share else 'unset')
        self.assertEqual(done['PATH'], ':'.join([str(run.bin)] + commands))

    def test_a_flatpak_that_fails_or_names_no_path_leaves_the_default_system_installation(self):
        cases = {'fails': (1, lambda other: []),
                 'fails after it named an installation': (1, lambda other: [other]),
                 'names nothing': (0, lambda other: []),
                 'says something else': (0, lambda other: ['error: no installations'])}
        for label, (status, lines) in cases.items():
            with self.subTest(label):
                run = Exports(self)
                system = flatpak_installation(run.system)
                run.flatpak(*lines(flatpak_installation(run.root / 'other')), status=status)
                done = run.succeed(FLATPAK_SYSTEM_DIR=str(system))
                self.assertEqual(done['XDG_DATA_DIRS'], run.share(system) + self.DEFAULT)
                self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(system)))

    def test_only_an_absolute_path_without_a_colon_is_an_installation(self):
        run = Exports(self)
        system = flatpak_installation(run.system)
        flatpak_installation(run.root / 'relative')            # the shell runs in the root, so "relative" is there
        colon = flatpak_installation(run.root / 'with:colon')
        run.flatpak(system, 'relative', colon, '', '/', '   ')
        done = run.succeed()
        self.assertEqual(done['XDG_DATA_DIRS'], run.share(system) + self.DEFAULT)
        self.assertEqual(done['PATH'], '%s:%s' % (run.bin, run.commands(system)))

    def test_flatpak_is_asked_for_its_installations_as_the_profile_script_asks(self):
        run = Exports(self)
        run.flatpak(flatpak_installation(run.system))
        run.succeed(G_MESSAGES_DEBUG='all')
        # One call, "--installations" and nothing else. GIO is told not to load its network file system (nothing may
        # wait for a bus that is not there yet), and a debug setting of the caller does not reach it: messages are
        # not paths.
        self.assertEqual(run.record.read_text(), '--installations|local|unset\n')

    def test_the_installation_of_the_user_is_where_flatpak_looks_for_it(self):
        run = Exports(self)
        user = flatpak_installation(run.user)
        moved = flatpak_installation(run.root / 'data/flatpak')
        chosen = flatpak_installation(run.root / 'chosen')
        # Below the home of the user, else below XDG_DATA_HOME, and FLATPAK_USER_DIR beats both.
        for environment, where in (({}, user), ({'XDG_DATA_HOME': str(run.root / 'data')}, moved),
                                   ({'XDG_DATA_HOME': str(run.root / 'data'), 'FLATPAK_USER_DIR': str(chosen)}, chosen)):
            with self.subTest(**environment):
                done = run.succeed(**environment)
                self.assertEqual(done['XDG_DATA_DIRS'], run.share(where) + self.DEFAULT)
                self.assertEqual((done['flatpak_user_dir'], done['FLATPAK_USER_DIR']), (str(where), str(where)))

    def test_a_plain_posix_shell_gives_the_same_result_as_bash(self):
        run = Exports(self)
        flatpak_installation(run.user)
        run.flatpak(flatpak_installation(run.system))
        expected = run.succeed('bash', XDG_DATA_DIRS='/usr/share')
        self.assertNotEqual(expected['PATH'], str(run.bin))
        others = [name for name, (argv, _) in Exports.SHELLS.items() if name != 'bash' and argv[0]]
        if not others:
            self.skipTest('no other shell than bash on this host')
        for name in others:
            with self.subTest(shell=name):
                self.assertEqual(run.succeed(name, XDG_DATA_DIRS='/usr/share'), expected)


class DesktopLaunch:
    """launch_desktop_session of the launcher, run in bash up to the start of Plasma.

    A desktop cannot run in a test, so what it would start is replaced by stand-ins that record how they were
    started: the `env` that starts the nested KWin, busctl (which answers once that KWin has been started), the
    helper that names the displays of KWin, and startplasma-wayland. The rest is the launcher's own code, the profile
    and the KWin wrapper included. The one change to its text is the directory that it makes in the private /tmp of a
    side, which is made below the directory of the test."""
    SCRIPT = ('set -euo pipefail\n'
              'home_dir="$HOME"; side=left; owner=tester; workspace_root="$HOME/Dosplex/left"\n'
              'bundle_dir="${TEST_BUNDLE_DIR:-}"\n'
              + shell_function('set_desktop_setting') + shell_function('prepare_desktop_profile')
              + shell_function('add_flatpak_exports') + shell_function('append_unique_paths')
              + shell_function('use_bundled_vulkan_layer')
              + shell_function('launch_desktop_session').replace('/tmp/.X11-unix', '"$X11_DIR"')
              + 'launch_desktop_session\n')
    RECORD = ('#!/usr/bin/python3\nimport json, os, sys\n'
              'name = os.environ["CAPTURE"] + "%s"\n'
              'open(name + ".part", "w").write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))\n'
              'os.replace(name + ".part", name)\n')
    BUSCTL = ('#!/usr/bin/python3\nimport os, sys, time\n'
              'kwin = os.environ["CAPTURE"] + ".kwin"\n'
              'end = time.time() + 10\n'
              'while time.time() < end and not os.path.exists(kwin):\n'
              '    time.sleep(0.02)\n'
              'sys.exit(0 if os.path.exists(kwin) else 1)\n')
    HELPER = ('import sys\n'
              'if sys.argv[1] == "scale":\n'
              '    print("1 stand-in")\n'
              'elif sys.argv[1].isdigit():\n'
              '    sys.stdout.buffer.write(b"wayland-7\\0:7\\0/nested/Xauthority\\0")\n')

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-desktop-launch-')
        test.addCleanup(temporary.cleanup)
        self.test = test
        self.root = Path(temporary.name)
        self.home, self.runtime, self.bin, self.libexec = (self.root / name
                                                           for name in ('home', 'runtime', 'bin', 'libexec'))
        for directory in (self.home, self.runtime, self.bin, self.libexec):
            directory.mkdir(mode=0o700)
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        for name, text in (('env', self.RECORD % '.kwin'), ('startplasma-wayland', self.RECORD % ''),
                           ('busctl', self.BUSCTL)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        (self.libexec / 'dosplex-desktop-environment.py').write_text(self.HELPER)
        self.user = self.home / '.local/share/flatpak'
        self.system = self.root / 'var/lib/flatpak'
        self.profile = self.home / '.local/share/dosplex/desktop'
        self.capture = self.root / 'plasma.json'
        self.log = self.root / 'launch.log'

    def flatpak(self, *lines, status=0):
        flatpak_stub(self.bin, lines, status)

    def start(self, **environment):
        """Run the launch; returns what startplasma-wayland and the stand-in for `env` were started with."""
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'XDG_RUNTIME_DIR': str(self.runtime),
               'DOSPLEX_SHIM_SCRIPT': str(self.libexec / 'steamos-session-select-shim.sh'),
               'DOSPLEX_LAUNCH_LOG': str(self.log), 'DOSPLEX_HOST_SOCKET': str(self.root / 'host.sock'),
               'WAYLAND_DISPLAY': str(self.root / 'host.sock'),
               'DOSPLEX_OUTPUT_WIDTH': '1920', 'DOSPLEX_OUTPUT_HEIGHT': '2160',
               'CAPTURE': str(self.capture), 'X11_DIR': str(self.root / 'x11-unix'),
               'FLATPAK_SYSTEM_DIR': str(self.root / 'no-system')}
        env.update(environment)
        done = subprocess.run([BASH, '-c', self.SCRIPT], env=env, cwd=self.root, capture_output=True, text=True,
                              timeout=60)
        self.test.assertEqual(done.returncode, 0, done.stderr)
        return (json.loads(self.capture.read_text()),
                json.loads(Path(str(self.capture) + '.kwin').read_text()))


class DesktopLaunchEnvironment(unittest.TestCase):
    """What the Plasma session and its KWin start with, taken from the launch itself."""

    def test_plasma_and_the_nested_kwin_start_with_the_flatpak_folders(self):
        run = DesktopLaunch(self)
        user = flatpak_installation(run.user)
        system = flatpak_installation(run.system)
        flatpak_installation(run.profile / 'data/flatpak')     # the data folder of the profile is no installation
        run.flatpak(system)
        plasma, kwin = run.start()
        share = '%s:%s/exports/share:/usr/local/share:/usr/share' % (Exports.share(user), system)
        path = ':'.join([str(run.runtime / 'desktop-bin'), str(run.bin), Exports.commands(user, system)])
        for name, started in (('Plasma', plasma), ('KWin', kwin)):
            with self.subTest(name):
                environment = started['env']
                self.assertEqual(environment['XDG_DATA_DIRS'], share)
                self.assertEqual(environment['PATH'], path)
                # The profile moves XDG_DATA_HOME, and Flatpak is told where the installation of the user is anyway.
                self.assertEqual(environment['XDG_DATA_HOME'], str(run.profile / 'data'))
                self.assertEqual(environment['FLATPAK_USER_DIR'], str(user))
        self.assertEqual(kwin['argv'][:3], ['WAYLAND_DISPLAY=%s' % (run.root / 'host.sock'),
                                            '/usr/bin/kwin_wayland_wrapper', '--fullscreen'])
        self.assertEqual(run.log.read_text().splitlines()[-3:], [
            'desktop flatpak user installation=%s' % user, 'desktop XDG_DATA_DIRS=%s' % share,
            'desktop PATH=%s' % path])

    def test_a_desktop_without_a_usable_flatpak_starts_as_it_did(self):
        for label, status in (('no flatpak', None), ('a flatpak that fails', 3)):
            with self.subTest(label):
                run = DesktopLaunch(self)
                if status is not None:
                    run.flatpak(status=status)
                plasma, kwin = run.start()
                for started in (plasma, kwin):
                    self.assertNotIn('XDG_DATA_DIRS', started['env'])
                    self.assertEqual(started['env']['PATH'], '%s:%s' % (run.runtime / 'desktop-bin', run.bin))
                    self.assertEqual(started['env']['FLATPAK_USER_DIR'], str(run.user))
                self.assertEqual(run.log.read_text().splitlines()[-3:], [
                    'desktop flatpak user installation=%s' % run.user, 'desktop XDG_DATA_DIRS=',
                    'desktop PATH=%s:%s' % (run.runtime / 'desktop-bin', run.bin)])

    def test_the_desktop_steam_is_not_told_about_a_second_x_server(self):
        # A side's Gamescope has two X servers and says so to Steam (STEAM_MULTIPLE_XWAYLANDS). The nested desktop has
        # one: with the variable, the desktop's Steam could not open the second and made no mouse or keyboard input
        # from a controller (Steam Machine, 2026-11-18).
        run = DesktopLaunch(self)
        run.flatpak(flatpak_installation(run.system))
        plasma, kwin = run.start(STEAM_MULTIPLE_XWAYLANDS='1')
        for started in (plasma, kwin):
            self.assertNotIn('STEAM_MULTIPLE_XWAYLANDS', started['env'])

    def test_the_desktop_leaves_the_vulkan_layer_variables_alone_even_next_to_a_complete_bundle(self):
        # The desktop has no Gamescope and no ENABLE_GAMESCOPE_WSI, so neither layer is active for its games; the
        # bundled layer is for Game mode only. The launch below runs with a complete bundle and the layer functions
        # defined, so a call put back into launch_desktop_session would show here as a changed environment.
        run = DesktopLaunch(self)
        system = flatpak_installation(run.system)
        run.flatpak(system)
        bundle = run.root / 'bundle'
        for family in ('x86_64', 'x86'):
            manifest = bundle / ('share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.%s.json' % family)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({'file_format_version': '1.0.0', 'layer': {
                'name': 'VK_LAYER_DOSPLEX_gamescope_wsi_' + family,
                'library_path': '../../../lib/libVkLayer_FROG_gamescope_wsi_%s.so' % family,
                'enable_environment': {'ENABLE_GAMESCOPE_WSI': '1'},
                'disable_environment': {'DOSPLEX_DISABLE_GAMESCOPE_WSI': '1'}}}))
            library = bundle / ('lib/libVkLayer_FROG_gamescope_wsi_%s.so' % family)
            library.parent.mkdir(parents=True, exist_ok=True)
            library.write_bytes(b'\x7fELF')
        plasma, kwin = run.start(TEST_BUNDLE_DIR=str(bundle), PRESSURE_VESSEL_FILESYSTEMS_RO='/srv/configs')
        share = '%s/exports/share:/usr/local/share:/usr/share' % system
        for name, started in (('Plasma', plasma), ('KWin', kwin)):
            with self.subTest(name):
                environment = started['env']
                self.assertEqual(environment['XDG_DATA_DIRS'], share)
                self.assertEqual(environment['PRESSURE_VESSEL_FILESYSTEMS_RO'], '/srv/configs')
                self.assertNotIn('DISABLE_GAMESCOPE_WSI', environment)
        log = run.log.read_text()
        self.assertIn('desktop XDG_DATA_DIRS=%s\n' % share, log)
        self.assertNotIn('vulkan_layer=', log)


class LauncherSource(unittest.TestCase):
    """What the shell tests above cannot run, read from the source: the two places that start the nested KWin, and
    where the Flatpak folders and the profile come in the launch of the desktop."""
    text = LAUNCHER.read_text()

    def test_both_starts_of_the_nested_kwin_carry_the_scale(self):
        starts = [line.strip() for line in self.text.splitlines()
                  if 'kwin_wayland_wrapper --fullscreen true --no-lockscreen' in line]
        self.assertEqual(len(starts), 2, starts)             # the wrapper script and the direct start
        wrapper, direct = sorted(starts, key=lambda line: 'xwayland' in line)
        self.assertRegex(wrapper, r'--no-lockscreen --scale \$desktop_scale "\\\$@"$')
        self.assertRegex(direct, r'--no-lockscreen --xwayland --scale "\$desktop_scale" &$')

    def test_the_profile_is_not_made_in_the_session_runtime(self):
        body = shell_function('prepare_desktop_profile')
        self.assertNotIn('desktop-config', self.text)
        self.assertNotIn('desktop-data', self.text)
        self.assertIn('export XDG_CONFIG_HOME="$desktop_root/config"', body)
        self.assertIn('export XDG_DATA_HOME="$desktop_root/data"', body)
        self.assertIn('export XDG_CONFIG_DIRS="$XDG_RUNTIME_DIR/desktop-defaults"', body)
        self.assertIn('${DOSPLEX_DESKTOP_CONFIG_ROOT:-$home_dir/.local/share/dosplex/desktop}', body)
        self.assertIn('mkdir -p -m 700 "$desktop_root" "$desktop_root/config" "$desktop_root/data"', body)
        # Only a directory of the side user is used (ownership cannot be faked without root, so it is read here).
        self.assertIn('[[ -d "$dir" && -O "$dir" ]]', body)

    def test_only_game_mode_uses_the_bundled_layer(self):
        callers = [name for name in ('launch_steam_session', 'launch_desktop_session', 'launch_wayland_terminal',
                                     'launch_xterm_terminal', 'prepare_desktop_profile')
                   if re.search(r'^\s+use_bundled_vulkan_layer$', shell_function(name), re.M)]
        self.assertEqual(callers, ['launch_steam_session'])
        self.assertEqual(len(re.findall(r'^\s+use_bundled_vulkan_layer$', self.text, re.M)), 1)

    def test_only_the_desktop_adds_the_flatpak_folders_and_before_the_profile_moves_xdg_data_home(self):
        # Steam's side keeps the environment that the supervisor gave it, with the bundled layer as its one change
        # (tests/session-launch-test.py); the desktop gets the Flatpak folders.
        callers = [name for name in ('launch_steam_session', 'launch_desktop_session', 'launch_wayland_terminal',
                                     'launch_xterm_terminal', 'prepare_desktop_profile')
                   if 'add_flatpak_exports' in shell_function(name)]
        self.assertEqual(callers, ['launch_desktop_session'])
        self.assertEqual(len(re.findall(r'^\s+add_flatpak_exports$', self.text, re.M)), 1)
        # The profile is the one place that moves XDG_DATA_HOME, below which Flatpak looks for the installation of
        # the user; the folders are taken before that.
        body = shell_function('launch_desktop_session')
        self.assertEqual(self.text.count('export XDG_DATA_HOME='), 1)
        self.assertLess(body.index('\n  add_flatpak_exports\n'), body.index('\n  prepare_desktop_profile\n'))

    def test_nothing_in_the_desktop_launch_replaces_xdg_data_dirs_or_path(self):
        launch, profile = shell_function('launch_desktop_session'), shell_function('prepare_desktop_profile')
        for body in (launch, profile):
            for name, value in re.findall(r'^\s*(?:export\s+)?(XDG_DATA_DIRS|PATH)=(.*)$', body, re.M):
                # The only assignment puts the directory of the KWin wrapper in front of what PATH was.
                self.assertEqual((name, value), ('PATH', '"$XDG_RUNTIME_DIR/desktop-bin:$PATH"'))
            self.assertNotRegex(body, r'\benv\s+(-i|-u|--ignore-environment|--unset)\b')
        self.assertEqual(profile.count('export PATH='), 1)

    def test_the_launcher_prepares_the_profile_before_it_starts_the_desktop(self):
        body = shell_function('launch_desktop_session')
        self.assertLess(body.index('prepare_desktop_profile\n'), body.index('/usr/bin/kwin_wayland_wrapper'))
        self.assertLess(body.index('prepare_desktop_profile\n'), body.index('exec startplasma-wayland'))
        self.assertIn('"$desktop_helper" "$nested_pid"', body)


class LegacyUserData(unittest.TestCase):
    """migrate_legacy_user_data: what the DuplOS name left in a player's home moves to the DosPlex name, once."""
    TOOLS = ('mv',)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dosplex-user-data-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.bin = self.root / 'home', self.root / 'bin'
        self.home.mkdir(mode=0o700)
        self.bin.mkdir()
        for tool in self.TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))

    def migrate(self):
        script = ('set -euo pipefail\nhome_dir="$HOME"\n' + shell_function('migrate_legacy_user_data')
                  + 'migrate_legacy_user_data\n')
        env = {'PATH': str(self.bin), 'HOME': str(self.home)}
        return subprocess.run([BASH, '-c', script], env=env, capture_output=True, text=True, timeout=60)

    def old_profile(self):
        profile = self.home / '.local/share/duplos/desktop'
        (profile / 'config').mkdir(parents=True)
        (profile / 'desktop-scale').write_text('1.5\n')
        (profile / 'config/kwinrc').write_text('[Xwayland]\nScale=2\n')
        return profile

    def test_the_desktop_profile_and_the_scale_preference_move(self):
        self.old_profile()
        done = self.migrate()
        self.assertEqual(done.returncode, 0, done.stderr)
        new = self.home / '.local/share/dosplex/desktop'
        self.assertEqual((new / 'desktop-scale').read_text(), '1.5\n')
        self.assertEqual((new / 'config/kwinrc').read_text(), '[Xwayland]\nScale=2\n')
        self.assertFalse((self.home / '.local/share/duplos').exists())
        self.assertIn('moved', done.stderr)

    def test_the_workspace_moves(self):
        (self.home / 'DuplOS/left').mkdir(parents=True)
        (self.home / 'DuplOS/left/launch.log').write_text('log\n')
        self.assertEqual(self.migrate().returncode, 0)
        self.assertEqual((self.home / 'Dosplex/left/launch.log').read_text(), 'log\n')
        self.assertFalse((self.home / 'DuplOS').exists())

    def test_a_second_run_changes_nothing(self):
        self.old_profile()
        self.migrate()
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*'))
        done = self.migrate()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, '')
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*')), before)

    def test_an_existing_new_directory_is_never_merged_or_overwritten(self):
        self.old_profile()
        new = self.home / '.local/share/dosplex/desktop'
        new.mkdir(parents=True)
        (new / 'desktop-scale').write_text('2\n')
        done = self.migrate()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((new / 'desktop-scale').read_text(), '2\n')
        self.assertEqual((self.home / '.local/share/duplos/desktop/desktop-scale').read_text(), '1.5\n')
        self.assertIn('exists already', done.stderr)

    def test_a_symlink_is_not_followed(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (self.home / '.local/share').mkdir(parents=True)
        (self.home / '.local/share/duplos').symlink_to(elsewhere)
        self.assertEqual(self.migrate().returncode, 0)
        self.assertTrue((self.home / '.local/share/duplos').is_symlink())
        self.assertFalse((self.home / '.local/share/dosplex').exists())

    def test_a_home_without_old_data_is_left_alone(self):
        done = self.migrate()
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_the_launcher_migrates_before_it_uses_either_directory(self):
        text = LAUNCHER.read_text()
        self.assertLess(text.index('\nmigrate_legacy_user_data\n'), text.index('\nworkspace_root='))
        self.assertLess(text.index('\nmigrate_legacy_user_data\n'), text.index('prepare_desktop_profile()'))


if __name__ == '__main__':
    unittest.main()
