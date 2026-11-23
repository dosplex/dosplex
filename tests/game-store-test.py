#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The shared game store's folders, mounts and Steam library list (ADR-023, T-606).

dosplex-game-store.py gives each account a Steam library folder that is an overlay over one root-owned store. This
checks, without root and without mounting anything: where everything is, the two mount commands, that mounting twice
does nothing and that a failed overlay leaves no view behind, which names and places are refused, the edits to
Steam's library list (added once, removed with the later folders renumbered, every other entry and key untouched,
both copies, refused while that account's Steam runs), and that the supervisor mounts and lists the library before a
side starts only when the store is on, and puts it away when it ends. What the kernel, Bubblewrap and Steam do with
it is checked on a device, not here.
"""
import contextlib
import hashlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import signal as signal_module
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_whiteout(info):
    """An overlay whiteout is a character device that only root can make; here it is an empty file with no mode."""
    return stat.S_ISREG(info.st_mode) and info.st_size == 0 and stat.S_IMODE(info.st_mode) == 0


def make_whiteout(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0))
    os.chmod(path, 0)


store = load('game_store', 'packaging/steamos/dosplex-game-store.py')
depot = load('depot', 'packaging/steamos/dosplex-depot.py')
supervisor = load('supervisor', 'packaging/steamos/dosplex-side-supervisor.py')

LIST = '''"libraryfolders"
{
	"0"
	{
		"path"		"/srv/steam/guest"
		"label"		""
		"contentid"		"81352975506955280"
		"apps"
		{
			"2990"		"3583792310"
		}
	}
	"1"
	{
		"path"		"/run/media/guest/card"
		"label"		"card"
	}
}
'''


class Places(unittest.TestCase):
    def test_everything_is_below_the_root_and_a_side_has_its_own_places(self):
        layout = store.Layout('/srv/dosplex-library')
        self.assertEqual(str(layout.steamapps), '/srv/dosplex-library/store/steamapps')
        side = layout.side('guest', '/srv/steam')
        self.assertEqual((str(side.work), str(side.view), str(side.reach)), (
            '/srv/dosplex-library/sides/guest/overlay-work', '/run/dosplex-library/guest',
            '/srv/dosplex-library/sides/guest/real'))
        self.assertEqual((str(side.library), str(side.steamapps), str(side.real_steamapps)),
                         ('/srv/steam', '/srv/steam/steamapps', '/srv/steam/steamapps'))
        self.assertEqual((str(side.stash), str(side.conflicts)), ('/srv/dosplex-library/stash/guest',
                                                                  '/srv/dosplex-library/conflicts/guest'))
        # The earlier layout's places, which the migration empties.
        self.assertEqual((str(side.retired.upper), str(side.retired.work), str(side.retired.library)), (
            '/srv/dosplex-library/sides/guest/upper', '/srv/dosplex-library/sides/guest/work',
            '/srv/dosplex-library/sides/guest/library'))
        self.assertIsNone(side.retired.bound)
        self.assertIsNone(layout.side('guest').library)           # not found yet: no folder is made up
        self.assertIsNone(layout.side('guest').steamapps)

    def test_the_environment_names_the_root_and_the_default_is_below_home(self):
        with patch.dict(os.environ, {'DOSPLEX_GAME_STORE': '/srv/elsewhere'}):
            self.assertEqual(str(store.Layout().root), '/srv/elsewhere')
        with patch.dict(os.environ, clear=True):
            self.assertEqual(store.Layout().root, Path('/home') / 'dosplex-library')

    def test_a_relative_root_a_root_with_dots_and_odd_user_names_are_refused(self):
        for root in ('library', '/srv/../etc', './x'):
            with self.assertRaises(store.StoreError, msg=root):
                store.Layout(root)
        for name in ('', '../deck', 'a/b', 'Deck', 'a b', '-x', 'x' * 40, 'guest\n'):
            with self.assertRaises(store.StoreError, msg=name):
                store.Layout('/srv/dosplex-library').side(name)

    def test_a_library_with_a_colon_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.Layout('/srv/dosplex-library').side('guest', '/srv/a:b')

    def test_only_an_ordinary_account_gets_a_library(self):
        for entry in (SimpleNamespace(pw_uid=0, pw_gid=0), SimpleNamespace(pw_uid=999, pw_gid=1000),
                      SimpleNamespace(pw_uid=1000, pw_gid=10)):
            with patch.object(store.pwd, 'getpwnam', return_value=entry), self.assertRaises(store.StoreError):
                store.account('someone')
        with patch.object(store.pwd, 'getpwnam', side_effect=KeyError), self.assertRaisesRegex(store.StoreError, 'no account'):
            store.account('nobody2')


class Commands(unittest.TestCase):
    def setUp(self):
        self.layout = store.Layout('/srv/dosplex-library')
        self.side = self.layout.side('guest', '/srv/homes/guest/.local/share/Steam')
        self.entry = SimpleNamespace(pw_uid=1004, pw_gid=1005, pw_dir='/srv/homes/guest', pw_name='guest')

    def test_the_view_is_a_read_only_bind_that_shows_roots_files_as_the_users(self):
        self.assertEqual(store.view_command(self.layout, self.side, self.entry), [
            'mount', '--bind', '-o', 'ro,nosuid,nodev,X-mount.idmap=u:0:1004:1 g:0:1005:1',
            '/srv/dosplex-library/store/steamapps', '/run/dosplex-library/guest'])

    def test_the_overlay_is_mounted_on_its_own_upper_directory_with_the_store_below(self):
        steamapps = '/srv/homes/guest/.local/share/Steam/steamapps'
        self.assertEqual(store.overlay_command(self.side), [
            'mount', '-t', 'overlay', 'dosplex-library', '-o',
            'nosuid,nodev,index=off,metacopy=off,redirect_dir=off,xino=off,uuid=off,'
            f'lowerdir=/run/dosplex-library/guest,upperdir={steamapps},'
            'workdir=/srv/dosplex-library/sides/guest/overlay-work', steamapps])

    def test_the_real_folder_is_bound_privately_before_the_overlay_covers_it(self):
        self.assertEqual(store.reach_commands(self.side), [
            ['mount', '--bind', '-o', 'nosuid,nodev', '/srv/homes/guest/.local/share/Steam/steamapps',
             '/srv/dosplex-library/sides/guest/real'],
            ['mount', '--make-private', '/srv/dosplex-library/sides/guest/real']])

    def test_a_place_that_would_break_the_overlay_options_is_refused(self):
        for root in ('/srv/a,b', '/srv/a\\b'):
            with self.assertRaises(store.StoreError, msg=root):
                store.overlay_command(store.Layout(root).side('guest', '/srv/homes/guest/Steam'))
        with self.assertRaises(store.StoreError):
            store.overlay_command(store.Layout('/srv/dosplex-library').side('guest', '/srv/homes/gu,est/Steam'))
        with self.assertRaises(store.StoreError):          # no library found: nothing to mount on
            store.overlay_command(store.Layout('/srv/dosplex-library').side('guest'))


class MountWorld(unittest.TestCase):
    """A temporary tree for mount() and unmount(), with the mount commands recorded instead of run."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.layout = store.Layout(self.base / 'library', view_root=self.base / 'run')
        self.steam = self.base / 'home/tester/.local/share/Steam'
        (self.steam / 'steamapps').mkdir(parents=True)
        self.layout.libraries['tester'] = (self.steam, self.steam)
        self.side = self.layout.side('tester')
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.base / 'home/tester'),
                                     pw_name='tester')
        self.mounted = {}       # mount point -> super options ('' for a bind)
        self.commands = []
        self.fail_overlay = False
        self.mountinfo = self.base / 'mountinfo'
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        for patcher in (patch.object(store, 'account', return_value=self.entry),
                        patch.object(store, 'real_directory', self.real_directory),
                        patch.object(store, 'prepare_store', self.prepare_store),
                        patch.object(store, 'is_whiteout', fake_whiteout)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_mountinfo()

    def real_directory(self, path, **_options):
        Path(path).mkdir(parents=True, exist_ok=True)

    def prepare_store(self, layout):
        for path in (layout.root, layout.store, layout.steamapps, layout.common, layout.sides):
            path.mkdir(parents=True, exist_ok=True)

    def write_mountinfo(self):
        lines = ['22 1 0:21 / / rw - ext4 /dev/root rw']
        for point, options in sorted(self.mounted.items()):
            kind = 'overlay' if 'workdir=' in options else 'ext4'
            lines.append(f'40 22 0:40 / {point} rw,nosuid - {kind} dosplex-library rw{"," + options if options else ""}')
        self.mountinfo.write_text('\n'.join(lines) + '\n')

    def runner(self, command, **_options):
        self.commands.append(command)
        failed = self.fail_overlay and command[:3] == ['mount', '-t', 'overlay']
        if not failed:
            if command[:3] == ['mount', '-t', 'overlay']:
                self.mounted[command[-1]] = command[command.index('-o') + 1].split(',', 2)[2]
            elif command[0] == 'mount' and command[1] == '--bind':
                self.mounted[command[-1]] = ''
            elif command[0] == 'umount':
                self.mounted.pop(command[-1], None)
            self.write_mountinfo()
        return SimpleNamespace(returncode=1 if failed else 0, stdout='', stderr='wrong fs type' if failed else '')

    def mount(self):
        return store.mount(self.layout, 'tester', runner=self.runner, mountinfo=self.mountinfo, proc=self.proc)

    def unmount(self, **options):
        return store.unmount(self.layout, 'tester', runner=self.runner, mountinfo=self.mountinfo, proc=self.proc,
                             **options)

    def hold(self, *apps):
        """The store holds these games: <root>/apps/<appid>.json names the install folder."""
        (self.layout.root / 'apps').mkdir(parents=True, exist_ok=True)
        for appid, installdir in apps:
            (self.layout.root / 'apps' / f'{appid}.json').write_text(json.dumps({'installdir': installdir}))


class Mounting(MountWorld):
    def test_it_binds_the_real_folder_then_mounts_the_view_and_the_overlay_then_takes_the_view_away(self):
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        self.assertEqual([command[:3] for command in self.commands],
                         [['mount', '--bind', '-o'], ['mount', '--make-private', str(self.side.reach)],
                          ['mount', '--bind', '-o'], ['mount', '-t', 'overlay'], ['mount', '--bind', '-o'],
                          ['umount', str(self.side.view)]])
        # The Proton prefixes are the account's own folder, bound over the overlay's (casefolded prefixes).
        self.assertEqual(self.commands[4][-2:], [str(self.side.reach / 'compatdata'), str(self.side.steamapps / 'compatdata')])
        self.assertTrue((self.side.reach / 'compatdata').is_dir())
        self.assertEqual(set(self.mounted), {str(self.side.reach), str(self.side.steamapps),
                                             str(self.side.steamapps / 'compatdata')})
        for path in (self.side.work, self.side.reach, self.layout.common, self.side.steamapps):
            self.assertTrue(path.is_dir(), path)
        self.assertEqual(store.overlay_mounts(self.side, self.mountinfo), [str(self.side.steamapps)])

    def test_a_second_mount_does_nothing(self):
        self.mount()
        self.commands.clear()
        self.assertEqual(self.mount(), [])
        self.assertEqual(self.commands, [])

    def test_a_failed_overlay_leaves_no_view_and_no_bind_and_says_why(self):
        self.fail_overlay = True
        with self.assertRaisesRegex(store.StoreError, 'wrong fs type'):
            self.mount()
        self.assertEqual(self.mounted, {})

    def test_unmount_takes_what_is_mounted_and_is_quiet_when_nothing_is(self):
        self.mount()
        self.assertEqual(self.unmount(), ['prefixes', 'library', 'real'])
        self.assertEqual(self.mounted, {})
        self.assertEqual(self.unmount(), [])
        absent = store.Layout(self.base / 'absent', view_root=self.base / 'run')
        self.assertEqual(store.unmount(absent, 'tester', runner=self.runner, mountinfo=self.mountinfo), [])

    def test_a_library_that_is_in_use_stays_and_that_is_an_error(self):
        self.mount()

        def busy(command, **_options):
            return SimpleNamespace(returncode=32, stdout='', stderr='target is busy')
        with self.assertRaisesRegex(store.StoreError, 'target is busy'):
            store.unmount(self.layout, 'tester', runner=busy, mountinfo=self.mountinfo, proc=self.proc)

    def test_a_busy_overlay_keeps_its_prefix_bind_and_the_mounts_are_as_they_were(self):
        # The prefix bind sits on the overlay and goes first; an overlay that is busy must not be left without it, or
        # every prefix lookup fails until a full remount (LIB-04).
        self.mount()
        before = dict(self.mounted)

        def busy_overlay(command, **options):
            if command[0] == 'umount' and command[-1] == str(self.side.steamapps):
                return SimpleNamespace(returncode=32, stdout='', stderr='target is busy')
            return self.runner(command, **options)
        with self.assertRaisesRegex(store.StoreError, 'target is busy'):
            store.unmount(self.layout, 'tester', runner=busy_overlay, mountinfo=self.mountinfo, proc=self.proc)
        self.assertEqual(self.mounted, before)
        self.assertIn(str(self.side.steamapps / 'compatdata'), self.mounted)

    def test_the_overlay_is_found_by_its_work_directory_whatever_the_library_is(self):
        self.mount()
        other = store.Layout(self.base / 'library', view_root=self.base / 'run').side('tester')
        self.assertIsNone(other.library)
        self.assertEqual(store.overlay_mounts(other, self.mountinfo), [str(self.side.steamapps)])
        self.assertEqual(store.Layout(self.base / 'elsewhere').side('tester').work in
                         [Path(point) for point in store.overlay_mounts(other, self.mountinfo)], False)

    def test_an_overlay_whose_work_directory_has_a_space_is_found_through_the_mount_tables_escapes(self):
        # A card named "My Card" is mounted at /run/media/deck/My Card, and the table shows the space as \040.
        card = store.Layout.for_disk(self.layout, '/run/media/deck/My Card')
        side = card.side('tester', '/run/media/deck/My Card/Lib')
        escaped = str(side.work).replace(' ', '\\040')
        self.mountinfo.write_text('40 22 0:40 / /run/media/deck/My\\040Card/Lib/steamapps rw - overlay dosplex-library '
                                  f'rw,lowerdir=/a,upperdir=/b,workdir={escaped}\n')
        self.assertEqual(store.overlay_mounts(side, self.mountinfo), ['/run/media/deck/My Card/Lib/steamapps'])

    def test_an_earlier_layer_that_has_not_been_moved_yet_stops_the_mount(self):
        self.side.retired.upper.mkdir(parents=True)
        with self.assertRaisesRegex(store.StoreError, 'earlier layout'):
            self.mount()
        self.assertEqual(self.commands, [])
        self.side.retired.upper.rmdir()
        (self.layout.root / 'migrating-tester.json').write_text('{}')
        with self.assertRaisesRegex(store.StoreError, 'earlier layout'):
            self.mount()

    def test_a_library_that_is_a_link_or_not_the_accounts_is_never_mounted_on(self):
        elsewhere = self.base / 'elsewhere'
        (elsewhere / 'steamapps').mkdir(parents=True)
        link = self.base / 'home/tester/Link'
        link.symlink_to(elsewhere)
        self.layout.libraries['tester'] = (link, link)
        with self.assertRaisesRegex(store.StoreError, 'link'):
            self.mount()
        self.layout.libraries['tester'] = (self.steam, self.steam)
        self.entry.pw_uid = os.getuid() + 1
        with self.assertRaisesRegex(store.StoreError, 'does not belong'):
            self.mount()
        self.entry.pw_uid = os.getuid()
        self.assertEqual(self.commands, [])

    def test_the_steamapps_folder_is_made_when_the_library_is_there_and_it_is_not(self):
        shutil.rmtree(self.steam / 'steamapps')
        self.mount()
        self.assertTrue((self.steam / 'steamapps').is_dir())
        shutil.rmtree(self.steam)
        self.unmount()
        with self.assertRaisesRegex(store.StoreError, 'cannot be looked at|not a folder'):
            self.mount()

    def test_an_account_without_a_library_on_the_primary_says_so_and_nothing_is_mounted(self):
        layout = store.Layout(self.base / 'library', view_root=self.base / 'run')
        with patch.object(store, 'resolve_library', return_value=None):
            with self.assertRaisesRegex(store.StoreError, 'no Steam folder yet'):
                store.mount(layout, 'tester', runner=self.runner, mountinfo=self.mountinfo, proc=self.proc)
        self.assertEqual(self.commands, [])

    def test_unmount_moves_the_manifests_of_stored_games_out_and_the_next_mount_puts_them_back(self):
        self.hold(('100', 'Game'))
        steamapps = self.steam / 'steamapps'
        (steamapps / 'appmanifest_100.acf').write_text('mine')
        (steamapps / 'appmanifest_200.acf').write_text('not stored')
        make_whiteout(steamapps / 'common/Game/x.dat')
        inode = os.stat(steamapps / 'appmanifest_100.acf').st_ino
        self.mount()
        self.assertEqual(self.unmount(), ['prefixes', 'library', 'real'])
        self.assertFalse((steamapps / 'appmanifest_100.acf').exists())
        self.assertFalse(os.path.lexists(steamapps / 'common/Game/x.dat'))
        self.assertTrue((steamapps / 'appmanifest_200.acf').exists())             # not a stored game: stays
        self.assertEqual(os.stat(self.side.stash / 'steamapps/appmanifest_100.acf').st_ino, inode)    # by rename
        self.mount()
        self.assertEqual((steamapps / 'appmanifest_100.acf').read_text(), 'mine')
        self.assertEqual(os.stat(steamapps / 'appmanifest_100.acf').st_ino, inode)
        self.assertTrue(fake_whiteout(os.lstat(steamapps / 'common/Game/x.dat')))
        self.assertFalse(self.side.stash.exists())

    def test_the_slots_own_unmount_does_not_stash(self):
        self.hold(('100', 'Game'))
        (self.steam / 'steamapps/appmanifest_100.acf').write_text('mine')
        self.mount()
        self.unmount(stash=False)
        self.assertTrue((self.steam / 'steamapps/appmanifest_100.acf').exists())
        self.assertFalse(self.side.stash.exists())

    def test_a_mount_that_fails_leaves_the_manifests_in_the_stash(self):
        self.hold(('100', 'Game'))
        (self.steam / 'steamapps/appmanifest_100.acf').write_text('mine')
        self.fail_overlay = True
        with self.assertRaisesRegex(store.StoreError, 'wrong fs type'):
            self.mount()
        self.assertFalse((self.steam / 'steamapps/appmanifest_100.acf').exists())
        self.assertTrue((self.side.stash / 'steamapps/appmanifest_100.acf').exists())
        self.fail_overlay = False
        self.mount()
        self.assertTrue((self.steam / 'steamapps/appmanifest_100.acf').exists())

    def test_the_mount_table_is_read_with_its_escapes(self):
        self.mountinfo.write_text('40 22 0:40 / /srv/a\\040b rw - overlay x rw\n41 22 0:41 / /plain rw - ext4 /dev/x rw\n')
        self.assertTrue(store.is_mounted('/srv/a b', self.mountinfo))
        self.assertTrue(store.is_mounted(Path('/plain'), self.mountinfo))
        self.assertFalse(store.is_mounted('/srv', self.mountinfo))
        self.assertEqual(store.mount_roots(self.mountinfo), {'/srv/a b': '/', '/plain': '/'})


class Folders(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_a_link_or_a_file_where_a_folder_belongs_is_refused(self):
        real = self.base / 'real'
        real.mkdir()
        (self.base / 'link').symlink_to(real)
        (self.base / 'file').write_text('x')
        for name in ('link', 'file'):
            with self.assertRaises((store.StoreError, OSError), msg=name):
                store.real_directory(self.base / name, mode=0o755)
        self.assertEqual(os.stat(real).st_mode & 0o777, os.stat(real).st_mode & 0o777)

    def test_a_folder_is_made_with_the_mode_asked_for_and_an_existing_one_is_corrected(self):
        path = self.base / 'made'
        store.real_directory(path, uid=os.getuid(), gid=os.getgid(), mode=0o750)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o750)
        path.chmod(0o777)
        store.real_directory(path, uid=os.getuid(), gid=os.getgid(), mode=0o755)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)

    def test_the_store_is_only_made_below_a_folder_that_root_alone_can_change(self):
        if os.geteuid() == 0:
            self.skipTest('the temporary folder is root\'s here')
        with self.assertRaisesRegex(store.StoreError, 'only root can change'):
            store.prepare_store(store.Layout(self.base / 'library'))
        self.assertFalse((self.base / 'library').exists())


class LibraryList(unittest.TestCase):
    LIBRARY = '/srv/dosplex-library/sides/guest/library'

    def test_adding_appends_a_numbered_folder_with_only_its_path(self):
        new = store.edited_list(LIST, self.LIBRARY, True, depot)
        self.assertEqual(new, LIST[:-2] + '\t"2"\n\t{\n\t\t"path"\t\t"' + self.LIBRARY + '"\n\t}\n}\n')
        self.assertIsNone(store.edited_list(new, self.LIBRARY, True, depot))               # already there

    def test_removing_gives_back_the_list_as_it_was(self):
        new = store.edited_list(LIST, self.LIBRARY, True, depot)
        self.assertEqual(store.edited_list(new, self.LIBRARY, False, depot), LIST)
        self.assertIsNone(store.edited_list(LIST, self.LIBRARY, False, depot))             # not there

    def test_removing_from_the_middle_renumbers_the_folders_after_it(self):
        _name, folders = depot.vdf_loads(LIST)
        folders = {'0': folders['0'], '1': {'path': self.LIBRARY, 'label': '', 'apps': {'10': '5'}}, '2': folders['1']}
        new = store.edited_list(depot.vdf_dumps('libraryfolders', folders), self.LIBRARY, False, depot)
        self.assertEqual(new, LIST)

    def test_only_an_exact_path_counts(self):
        for other in (self.LIBRARY + '/', self.LIBRARY + '2', '/srv/dosplex-library/sides/deck/library'):
            text = store.edited_list(LIST, other, True, depot)
            self.assertIsNone(store.edited_list(text, self.LIBRARY, False, depot), other)

    def test_anything_that_is_not_a_library_list_is_refused(self):
        for text in ('"AppState"\n{\n}\n', '"libraryfolders"\n{\n\t"x"\n\t{\n\t\t"path"\t\t"/a"\n\t}\n}\n', 'garbage {'):
            with self.assertRaises((ValueError, store.StoreError), msg=text):
                store.edited_list(text, self.LIBRARY, True, depot)


class Registering(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest('runs the list changes as the calling user')
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.steam = self.home / '.local/share/Steam'
        for relative in store.LIBRARY_LISTS:
            (self.steam / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.steam / relative).write_text(LIST)
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home), pw_name='tester')
        self.layout = store.Layout('/srv/dosplex-library')
        self.library = '/srv/lib/tester'
        self.layout.libraries['tester'] = (Path(self.library), Path(self.library))
        self.proc = self.home / 'proc'
        self.proc.mkdir()
        patcher = patch.object(store, 'account', return_value=self.entry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_copies_get_the_folder_once_and_lose_it_again(self):
        self.assertEqual(store.register(self.layout, 'tester', proc=self.proc), list(store.LIBRARY_LISTS))
        for relative in store.LIBRARY_LISTS:
            self.assertIn(self.library, (self.steam / relative).read_text())
        self.assertEqual(store.registered(self.layout, 'tester'), list(store.LIBRARY_LISTS))
        self.assertEqual(store.register(self.layout, 'tester', proc=self.proc), [])
        self.assertEqual(store.register(self.layout, 'tester', False, proc=self.proc), list(store.LIBRARY_LISTS))
        for relative in store.LIBRARY_LISTS:
            self.assertEqual((self.steam / relative).read_text(), LIST)
        self.assertEqual(store.registered(self.layout, 'tester'), [])

    def test_a_copy_that_is_not_there_is_not_made_and_the_mode_is_kept(self):
        (self.steam / 'config/libraryfolders.vdf').unlink()
        (self.steam / 'steamapps/libraryfolders.vdf').chmod(0o640)
        self.assertEqual(store.register(self.layout, 'tester', proc=self.proc), ['steamapps/libraryfolders.vdf'])
        self.assertFalse((self.steam / 'config/libraryfolders.vdf').exists())
        self.assertEqual(os.stat(self.steam / 'steamapps/libraryfolders.vdf').st_mode & 0o777, 0o640)
        self.assertEqual([item.name for item in (self.steam / 'steamapps').iterdir()], ['libraryfolders.vdf'])

    def test_a_list_that_is_a_link_is_refused_and_its_target_untouched(self):
        target = self.home / 'target.vdf'
        target.write_text(LIST)
        path = self.steam / 'steamapps/libraryfolders.vdf'
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises((store.StoreError, OSError)):
            store.register(self.layout, 'tester', proc=self.proc)
        self.assertEqual(target.read_text(), LIST)

    def test_it_is_refused_while_that_accounts_steam_runs(self):
        process = self.proc / '4242'
        process.mkdir()
        (process / 'comm').write_text('steam\n')
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            store.register(self.layout, 'tester', proc=self.proc)
        self.assertEqual((self.steam / 'steamapps/libraryfolders.vdf').read_text(), LIST)
        (process / 'comm').write_text('steamwebhelper\n')
        self.assertTrue(store.register(self.layout, 'tester', proc=self.proc))

    def test_the_stored_games_are_read_from_the_stores_manifests(self):
        layout = store.Layout(self.home / 'library')
        layout.steamapps.mkdir(parents=True)
        (layout.steamapps / 'appmanifest_262830.acf').write_text(depot.vdf_dumps('AppState', {
            'appid': '262830', 'name': 'A Game', 'installdir': 'A Game', 'buildid': '7', 'SizeOnDisk': '80'}))
        (layout.steamapps / 'appmanifest_9.acf').write_text('broken {')
        (layout.steamapps / 'notes.txt').write_text('x')
        apps = store.stored_apps(layout)
        self.assertEqual(apps['262830'], {'name': 'A Game', 'installdir': 'A Game', 'buildid': '7', 'SizeOnDisk': '80'})
        self.assertIn('error', apps['9'])
        self.assertEqual(sorted(apps), ['262830', '9'])


class FakeStore:
    """Stands in for dosplex-game-store.py in the supervisor's two helpers."""

    def __init__(self, mount_fails=False, permanent=False, disks=()):
        self.calls, self.mount_fails = [], mount_fails
        self.is_permanent = permanent
        self.disks = list(disks)
        self.Layout = store.Layout

    def note(self, verb, layout, owner):
        self.calls.append((verb, owner) if layout.primary is None else (verb, owner, str(layout.mount_point)))

    def permanent(self, layout):
        if self.is_permanent is None:
            raise store.StoreError('config is broken')
        return self.is_permanent

    def layouts(self, layout):
        return [layout] + self.disks, []

    def listed_layouts(self, layout):
        return [layout] + self.disks

    def mount(self, layout, owner):
        self.note('mount', layout, owner)
        if self.mount_fails:
            raise store.StoreError('idmapped mounts are not supported')
        return ['library']

    def mount_all(self, layout, owner):
        paths, problems = [], []
        for item in [layout] + self.disks:
            try:
                self.mount(item, owner)
                paths.append(f'{item.root}/steam-of-{owner}/steamapps')
            except store.StoreError as error:
                problems.append(str(error))
        return paths, problems

    def unmount(self, layout, owner):
        self.note('unmount', layout, owner)
        return ['library']


def said_by(call, *arguments, **options):
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        return call(*arguments, **options), said.getvalue()


class Supervisor(unittest.TestCase):
    def test_off_unless_the_store_is_named(self):
        fake = FakeStore()
        with patch.dict(os.environ, clear=True):
            self.assertEqual(said_by(supervisor.prepare_game_library, 'guest', module=fake), (None, ''))
            self.assertEqual(said_by(supervisor.release_game_library, 'guest', module=fake), (None, ''))
        self.assertEqual(fake.calls, [])

    def test_it_merges_the_store_into_the_library_and_returns_the_folder_the_overlay_is_on(self):
        fake = FakeStore()
        library, said = said_by(supervisor.prepare_game_library, 'guest', '/srv/dosplex-library', fake)
        self.assertEqual(library, '/srv/dosplex-library/steam-of-guest/steamapps')
        self.assertEqual(fake.calls, [('mount', 'guest')])
        self.assertEqual(said, '')          # the account's Steam list is not touched

    def test_a_library_that_cannot_be_mounted_is_said_and_the_side_starts_without(self):
        fake = FakeStore(mount_fails=True)
        library, said = said_by(supervisor.prepare_game_library, 'guest', '/srv/dosplex-library', fake)
        self.assertIsNone(library)
        self.assertEqual(fake.calls, [('mount', 'guest')])
        self.assertIn('guest starts without the shared game library: idmapped mounts are not supported', said)

    def test_a_bad_store_or_user_is_said_and_nothing_is_mounted(self):
        fake = FakeStore()
        for place in ('relative', '/srv/../dosplex-library'):
            library, said = said_by(supervisor.prepare_game_library, 'guest', place, fake)
            self.assertIsNone(library)
            self.assertIn('starts without the shared game library', said)
        self.assertEqual(fake.calls, [])

    def test_putting_away_unmounts_and_stashes_through_unmount(self):
        fake = FakeStore()
        self.assertEqual(said_by(supervisor.release_game_library, 'guest', '/srv/dosplex-library', fake), (None, ''))
        self.assertEqual(fake.calls, [('unmount', 'guest')])
        failing = FakeStore()
        failing.unmount = lambda layout, owner: (_ for _ in ()).throw(store.StoreError('target is busy'))
        _none, said = said_by(supervisor.release_game_library, 'guest', '/srv/dosplex-library', failing)
        self.assertIn('was not put away', said)


class SupervisorDisks(unittest.TestCase):
    def setUp(self):
        primary = store.Layout('/srv/dosplex-library')
        self.disk = store.Layout.for_disk(primary, '/mnt/usb')
        self.fake = FakeStore(disks=[self.disk])

    def test_every_library_is_mounted_and_handed_on_joined_by_colons(self):
        library, _said = said_by(supervisor.prepare_game_library, 'guest', '/srv/dosplex-library', self.fake)
        self.assertEqual(library, '/srv/dosplex-library/steam-of-guest/steamapps:'
                                  f'{self.disk.root}/steam-of-guest/steamapps')
        self.assertEqual(self.fake.calls, [('mount', 'guest'), ('mount', 'guest', '/mnt/usb')])

    def test_a_disk_that_fails_is_left_off_and_the_main_library_still_starts(self):
        fake = self.fake
        original = fake.mount

        def mount(layout, owner):
            if layout.primary is not None:
                raise store.StoreError('no such device')
            return original(layout, owner)
        fake.mount = mount
        library, said = said_by(supervisor.prepare_game_library, 'guest', '/srv/dosplex-library', fake)
        self.assertEqual(library, '/srv/dosplex-library/steam-of-guest/steamapps')
        self.assertIn('starts without the shared game library: no such device', said)

    def test_nothing_mounted_gives_none(self):
        fake = FakeStore(mount_fails=True, disks=[self.disk])
        library, _said = said_by(supervisor.prepare_game_library, 'guest', '/srv/dosplex-library', fake)
        self.assertIsNone(library)

    def test_putting_away_goes_over_every_listed_store(self):
        said_by(supervisor.release_game_library, 'guest', '/srv/dosplex-library', self.fake)
        self.assertEqual(self.fake.calls, [('unmount', 'guest'), ('unmount', 'guest', '/mnt/usb')])


class PermanentSupervisor(unittest.TestCase):
    def test_a_permanent_store_keeps_the_library_mounted_and_says_so_once(self):
        fake = FakeStore(permanent=True)
        supervisor._PERMANENT_SAID.clear()
        _none, first = said_by(supervisor.release_game_library, 'guest', '/srv/dosplex-library', fake)
        _none, second = said_by(supervisor.release_game_library, 'deck', '/srv/dosplex-library', fake)
        self.assertEqual(fake.calls, [])
        self.assertIn('permanent', first)
        self.assertEqual(second, '')

    def test_a_broken_config_falls_back_to_putting_the_library_away_and_says_why(self):
        fake = FakeStore(permanent=None)
        _none, said = said_by(supervisor.release_game_library, 'guest', '/srv/dosplex-library', fake)
        self.assertEqual(fake.calls, [('unmount', 'guest')])
        self.assertIn('config is unusable (config is broken)', said)


class ConfigFile(unittest.TestCase):
    GOOD = {'version': 1, 'mode': 'on', 'users': ['deck']}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.layout = store.Layout(self.base / 'library')
        self.layout.root.mkdir()

    def put(self, content, mode=0o644):
        data = content if isinstance(content, bytes) else (content if isinstance(content, str) else json.dumps(content)).encode()
        self.layout.config.write_bytes(data)
        self.layout.config.chmod(mode)

    def test_no_file_means_no_config_and_not_permanent(self):
        self.assertIsNone(store.read_config(self.layout))
        self.assertFalse(store.permanent(self.layout))

    def test_a_good_config_is_read_and_is_permanent_in_both_modes(self):
        self.put({'version': 1, 'mode': 'on', 'users': ['deck', 'guest']})
        self.assertEqual(store.read_config(self.layout), {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']})
        self.assertTrue(store.permanent(self.layout))
        self.put({'version': 1, 'mode': 'leaving', 'users': ['deck']})
        self.assertTrue(store.permanent(self.layout))    # until a slot has given every game back

    def test_every_malformed_config_is_refused(self):
        good = self.GOOD
        bad = {
            'not json': 'x{', 'a list': [], 'extra key': dict(good, more=1), 'missing key': {'version': 1, 'mode': 'on'},
            'version 2': dict(good, version=2), 'version true': dict(good, version=True),
            'version text': dict(good, version='1'), 'unknown mode': dict(good, mode='off'),
            'users text': dict(good, users='deck'), 'no users': dict(good, users=[]),
            'five users': dict(good, users=['a', 'b', 'c', 'd', 'e']), 'duplicate': dict(good, users=['deck', 'deck']),
            'bad name': dict(good, users=['../x']), 'upper name': dict(good, users=['Deck']),
            'not a name': dict(good, users=[7]), 'not utf-8': b'\xff\xfe',
            'too large': ' ' * (store.CONFIG_LIMIT + 1)}
        for label, content in bad.items():
            self.put(content)
            with self.assertRaises(store.StoreError, msg=label):
                store.read_config(self.layout)
            with self.assertRaises(store.StoreError, msg=label):
                store.permanent(self.layout)

    def test_a_config_others_can_write_a_link_a_folder_and_a_foreign_owner_are_refused(self):
        for mode in (0o664, 0o646):
            self.put(self.GOOD, mode)
            with self.assertRaisesRegex(store.StoreError, 'not be writable'):
                store.read_config(self.layout)
        self.layout.config.unlink()
        target = self.base / 'target.json'
        target.write_text(json.dumps(self.GOOD))
        self.layout.config.symlink_to(target)
        with self.assertRaises(store.StoreError):
            store.read_config(self.layout)
        self.layout.config.unlink()
        self.layout.config.mkdir()
        with self.assertRaises(store.StoreError):
            store.read_config(self.layout)
        self.layout.config.rmdir()
        self.put(self.GOOD)
        with patch.object(store.os, 'geteuid', return_value=os.getuid() + 1), \
                self.assertRaisesRegex(store.StoreError, 'belong to root'):
            store.read_config(self.layout)

    def test_writing_is_atomic_mode_0644_and_leaves_no_temporary_file(self):
        store.write_config(self.layout, self.GOOD)
        self.assertEqual(os.stat(self.layout.config).st_mode & 0o777, 0o644)
        self.assertEqual(os.listdir(self.layout.root), ['config.json'])
        self.assertEqual(store.read_config(self.layout)['users'], ['deck'])

    def test_on_writes_the_config_once_and_refuses_a_different_one(self):
        entry = SimpleNamespace(pw_uid=1000, pw_gid=1000)
        with patch.object(store, 'account', return_value=entry), patch.object(store, 'prepare_store'):
            self.assertEqual(store.turn_on(self.layout, ['deck', 'guest'])['mode'], 'on')
            before = os.stat(self.layout.config).st_mtime_ns
            self.assertEqual(store.turn_on(self.layout, ['deck', 'guest'])['users'], ['deck', 'guest'])
            self.assertEqual(os.stat(self.layout.config).st_mtime_ns, before)
            with self.assertRaisesRegex(store.StoreError, 'configured already: mode on, users deck, guest'):
                store.turn_on(self.layout, ['deck'])
            with self.assertRaisesRegex(store.StoreError, 'at least one'):
                store.turn_on(self.layout, [])

    def test_on_checks_every_account_and_writes_nothing_when_one_is_wrong(self):
        with patch.object(store, 'account', side_effect=store.StoreError('no account x')), \
                self.assertRaisesRegex(store.StoreError, 'no account'):
            store.turn_on(self.layout, ['x'])
        self.assertFalse(self.layout.config.exists())

    def test_off_sets_leaving_and_needs_a_config(self):
        with self.assertRaisesRegex(store.StoreError, 'not on'):
            store.turn_off(self.layout)
        self.put(self.GOOD)
        self.assertEqual(store.turn_off(self.layout)['mode'], 'leaving')
        self.assertEqual(store.read_config(self.layout)['mode'], 'leaving')
        self.assertTrue(store.permanent(self.layout))


class StubTidy:
    class TidyError(RuntimeError):
        pass

    def __init__(self):
        self.calls = []
        self.restore = {'restored': ['10'], 'kept': []}
        self.commit_result = {'committed': ['10'], 'dropped': [], 'removed': []}
        self.fail = False
        self.refuse = set()
        self.order = []
        self.restore_for = {}
        self.libraries = []
        self.listings = {}
        self.account_calls = 0
        self.migrate_result = {}

    def account_libraries(self, layout, user, users):
        self.account_calls += 1
        return self.libraries

    def ask_for_lists(self, layout, users, *, online, now=None):
        self.calls.append(('lists', list(users), online))
        return {'asked': 0, 'returned': 0, 'answered': 0}

    def migrate(self, layout, users, *, proc=None):
        self.calls.append(('migrate', list(users)))
        return self.migrate_result

    def commit(self, layout, users):
        self.order.append(str(layout.root))
        self.calls.append(('commit', list(users)))
        if self.fail:
            raise RuntimeError('staging is damaged')
        return self.commit_result

    def restore_all(self, layout, users):
        self.order.append(str(layout.root))
        self.calls.append(('restore_all', list(users)))
        if self.fail:
            raise RuntimeError('no room')
        return self.restore_for.get(str(layout.root), self.restore)

    def pending(self, layout):
        return [{'appid': '10'}]

    def stored(self, layout):
        return {'10': {'shared_by': 'deck'}}

    def shareable(self, layout, user, users):
        if str(layout.root) in self.listings:
            return self.listings[str(layout.root)]
        return [{'appid': '10', 'name': 'A', 'source': 'library', 'library': '/x', 'reason': ''},
                {'appid': '11', 'name': 'B', 'source': 'library', 'library': '/x', 'reason': 'not fully installed'},
                {'appid': '12', 'name': 'C', 'source': 'layer', 'library': '/x', 'reason': ''}]

    def share(self, layout, user, appid, users, *, progress=None):
        self.calls.append(('share', user, appid, list(users)))
        self.shared_in = getattr(self, 'shared_in', []) + [(appid, str(layout.root))]
        if appid in self.refuse:
            raise self.TidyError(f'{appid} is not installed')
        if progress:        # the real module reports every file it hashed: its name and its size
            progress('data/big.pak', 3 << 29)
            progress('data/more.pak', 3 << 29)
        return {'appid': appid, 'name': 'n', 'source': 'library', 'kind': 'join', 'files': 3, 'bytes': 9, 'private': 0}


class Online(unittest.TestCase):
    HEAD = 'Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'

    def routes(self, *lines):
        temp = tempfile.NamedTemporaryFile('w', prefix='dosplex-routes-', delete=False)
        self.addCleanup(os.unlink, temp.name)
        temp.write(self.HEAD + ''.join(line + '\n' for line in lines))
        temp.close()
        return temp.name

    def test_a_default_route_through_a_gateway_that_is_up_means_online(self):
        self.assertTrue(store.online(self.routes('wlan0\t0000A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0',
                                                 'wlan0\t00000000\t0100A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0')))

    def test_only_local_routes_no_routes_and_no_file_mean_offline(self):
        self.assertFalse(store.online(self.routes('wlan0\t0000A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0')))
        self.assertFalse(store.online(self.routes('wlan0\t00000000\t0100A8C0\t0002\t0\t0\t600\t00000000\t0\t0\t0')))
        self.assertFalse(store.online(self.routes()))
        self.assertFalse(store.online(self.routes('garbage')))
        self.assertFalse(store.online('/nonexistent/routes'))


class Slot(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.layout = store.Layout(Path(temp.name) / 'library')
        self.layout.root.mkdir()
        self.calls = []
        self.running = set()
        self.busy = set()
        self.bad_mount = set()
        self.unpreparable = False
        self.problems = {}
        for name, function in (('account', self.account), ('steam_running', self.steam_running),
                               ('unmount', self.unmount), ('mount', self.mount),
                               ('prepare_sides', self.prepare_sides), ('restore_account', self.restore_account),
                               ('online', lambda routes=None: True)):
            patcher = patch.object(store, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tidy = StubTidy()

    def config(self, mode, users=('deck', 'guest')):
        store.write_config(self.layout, {'version': 1, 'mode': mode, 'users': list(users)})

    def account(self, name):
        return SimpleNamespace(pw_uid={'deck': 1000, 'guest': 1001}[name], pw_gid=1000, pw_dir='/nonexistent')

    def prepare_sides(self, layout, users, **options):
        self.calls.append(('prepare', tuple(users)))
        if self.unpreparable:
            raise store.StoreError('the parent folder is not root\'s')
        return self.problems

    def steam_running(self, uid, proc=None):
        return uid in self.running

    def unmount(self, layout, name, **options):
        self.calls.append(('unmount' if not options.get('stash', True) else 'stash', name))
        if name in self.busy:
            raise store.StoreError('target is busy')
        return ['library']

    def restore_account(self, layout, name, **options):
        self.calls.append(('restore', name))
        return []

    def mount(self, layout, name, **_options):
        self.calls.append(('mount', name))
        if name in self.bad_mount:
            raise store.StoreError('idmapped mounts are not supported')
        return ['library']

    def slot(self):
        return store.slot(self.layout, tidy=self.tidy)

    def test_no_config_does_nothing(self):
        self.assertEqual(self.slot(), {'config': None})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tidy.calls, [])

    def test_the_slot_keeps_its_report_for_status_and_a_store_that_left_keeps_none(self):
        self.config('on')
        self.tidy.commit_result = {'committed': [], 'dropped': [{'appid': '550', 'reason': 'x is open for writing'}],
                                   'removed': []}
        report = self.slot()
        kept = store.last_slot(self.layout)
        self.assertEqual(kept['report'], json.loads(json.dumps(report)))
        self.assertRegex(kept['utc'], r'^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(os.stat(self.layout.root / 'slot.json').st_mode & 0o777, 0o644)
        with patch.object(store, 'tidy_module', lambda: self.tidy), patch.object(store, 'registered', lambda *a: []):
            self.assertEqual(store.status(self.layout, [])['last_slot'], kept)
        (self.layout.root / 'slot.json').write_text('{"version": 1, "utc": 5}')
        self.assertIsNone(store.last_slot(self.layout))
        os.unlink(self.layout.root / 'slot.json')
        self.config('leaving')
        self.assertIs(self.slot()['left'], True)
        self.assertFalse((self.layout.root / 'slot.json').exists())

    def test_status_names_the_games_that_cannot_be_shared_yet_and_why(self):
        self.config('on')
        self.tidy.listings[str(self.layout.root)] = [
            {'appid': '10', 'name': 'A', 'source': 'library', 'library': '/x', 'reason': None},
            {'appid': '11', 'name': 'B', 'source': 'library', 'library': '/x', 'reason': 'waits for its file list'},
            {'appid': '12', 'name': 'C', 'source': 'layer', 'library': '/x', 'reason': 'is already shared (build 7)'},
            {'appid': '13', 'name': 'Proton 11.0', 'source': 'library', 'library': '/x', 'reason': 'has no build number'}]
        with patch.object(store, 'registered', lambda *a: []):
            report = store.status(self.layout, [], tidy=self.tidy)
            self.assertEqual(report['unshared'], {
                'deck': [{'appid': '11', 'name': 'B', 'reason': 'waits for its file list'}],
                'guest': [{'appid': '11', 'name': 'B', 'reason': 'waits for its file list'}]})
            # A game whose file list DosPlex asked Steam for says so, whatever its manifest shows meanwhile.
            self.tidy.asked_for_lists = lambda layout: {'/'.join(('deck', '11'))}
            waits = store.status(self.layout, [], tidy=self.tidy)['unshared']
            self.assertIn('waits for its file list from Steam (asked at the last start', waits['deck'][0]['reason'])
            self.assertEqual(waits['guest'][0]['reason'], 'waits for its file list')
            del self.tidy.asked_for_lists
            # A stored game that an account has still to validate (its manifest says update required) is shared.
            (self.layout.steamapps).mkdir(parents=True, exist_ok=True)
            (self.layout.steamapps / 'appmanifest_11.acf').write_text('"AppState"\n{\n\t"appid"\t\t"11"\n}\n')
            self.assertEqual(store.status(self.layout, [], tidy=self.tidy)['unshared'], {'deck': [], 'guest': []})
            os.unlink(self.layout.steamapps / 'appmanifest_11.acf')
            self.tidy.shareable = lambda *a: (_ for _ in ()).throw(RuntimeError('the step run as the user failed'))
            self.assertEqual(store.status(self.layout, [], tidy=self.tidy)['unshared'],
                             {'error': 'the step run as the user failed'})
            self.config('leaving')
            self.assertEqual(store.status(self.layout, [], tidy=self.tidy)['unshared'], {})

    def test_mode_on_asks_for_the_file_lists_and_leaving_does_not(self):
        self.config('on')
        self.assertEqual(self.slot()['lists'], {'asked': 0, 'returned': 0, 'answered': 0})
        self.assertIn(('lists', ['deck', 'guest'], True), self.tidy.calls)
        with patch.object(store, 'online', lambda routes=None: False):
            self.slot()
        self.assertEqual(self.tidy.calls[-1], ('lists', ['deck', 'guest'], False))
        self.tidy.calls.clear()
        self.config('leaving')
        self.assertNotIn('lists', self.slot())
        self.assertNotIn('lists', [call[0] for call in self.tidy.calls])

    def test_a_failure_to_ask_for_the_file_lists_is_said_and_stops_nothing(self):
        self.config('on')
        self.tidy.ask_for_lists = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('lists.json is damaged'))
        report = self.slot()
        self.assertEqual(report['lists_error'], 'lists.json is damaged')
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_a_running_steam_of_any_account_refuses_before_anything_is_changed(self):
        self.config('on')
        self.running.add(1001)
        with self.assertRaisesRegex(store.StoreError, "guest's Steam is running"):
            self.slot()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tidy.calls, [])

    def test_mode_on_unmounts_moves_the_earlier_layers_commits_then_mounts_every_library(self):
        self.config('on')
        report = self.slot()
        self.assertEqual(report['commit'], self.tidy.commit_result)
        self.assertEqual(self.calls, [('unmount', 'deck'), ('unmount', 'guest'), ('prepare', ('deck', 'guest')),
                                      ('restore', 'deck'), ('restore', 'guest'), ('mount', 'deck'), ('mount', 'guest')])
        self.assertEqual(self.tidy.calls[:2], [('migrate', ['deck', 'guest']), ('commit', ['deck', 'guest'])])
        self.assertEqual(report['migrate'], {})
        self.assertNotIn('busy', report)
        json.dumps(report)

    def test_the_stash_is_put_back_before_the_commit_reads_the_manifests(self):
        # After a clean shutdown the manifests of stored games are in the stash, and an update record is dropped when
        # the commit does not find the account's manifest (LIB-05).
        self.config('on')
        seen = []
        real_commit = self.tidy.commit
        self.tidy.commit = lambda layout, users: (seen.append([c for c in self.calls if c[0] == 'restore']),
                                                  real_commit(layout, users))[1]
        self.slot()
        self.assertEqual(seen, [[('restore', 'deck'), ('restore', 'guest')]])

    def test_a_stash_that_cannot_be_put_back_is_said_and_the_commit_still_runs(self):
        self.config('on')

        def failing(layout, name, **options):
            raise store.StoreError('its Steam is running')
        with patch.object(store, 'restore_account', failing):
            report = self.slot()
        self.assertEqual(report['stash_errors'], {'deck': 'its Steam is running', 'guest': 'its Steam is running'})
        self.assertIn(('commit', ['deck', 'guest']), self.tidy.calls)

    def test_the_slot_never_stashes_between_its_unmount_and_its_mount(self):
        # The commit needs the manifests, and no stock Steam runs in between.
        self.config('on')
        self.slot()
        self.assertNotIn('stash', [call[0] for call in self.calls])

    def test_an_account_without_a_library_on_a_disk_is_named_and_the_others_go_on(self):
        self.config('on')
        self.problems = {'guest': 'guest has no Steam library on this disk'}
        report = self.slot()
        self.assertEqual(report['no_library'], self.problems)
        self.assertIn(('commit', ['deck', 'guest']), self.tidy.calls)

    def test_a_commit_that_raises_is_reported_and_the_libraries_still_come_back(self):
        self.config('on')
        self.tidy.fail = True
        report = self.slot()
        self.assertEqual(report['commit_error'], 'staging is damaged')
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_a_busy_library_skips_the_tidy_work_and_the_libraries_are_mounted_again(self):
        self.config('on')
        self.busy.add('deck')
        report = self.slot()
        self.assertEqual(report['busy'], ['deck'])
        self.assertEqual(self.tidy.calls, [('lists', ['deck', 'guest'], True)])
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_a_library_that_cannot_be_mounted_has_its_manifests_stashed(self):
        self.config('on')
        self.bad_mount.add('guest')
        report = self.slot()
        self.assertIn('idmapped', report['failed']['guest'])
        self.assertNotIn('deck', report['failed'])
        self.assertEqual(self.calls[-2:], [('mount', 'guest'), ('stash', 'guest')])

    def test_leaving_gives_the_games_back_unlists_and_removes_the_config(self):
        self.config('leaving')
        report = self.slot()
        self.assertTrue(report['left'])
        self.assertEqual(report['restore'], {'restored': ['10'], 'kept': []})
        self.assertFalse(self.layout.config.exists())
        self.assertEqual(self.calls, [('unmount', 'deck'), ('unmount', 'guest'), ('prepare', ('deck', 'guest'))])
        self.assertEqual(self.tidy.calls, [('migrate', ['deck', 'guest']), ('restore_all', ['deck', 'guest'])])

    def test_leaving_waits_for_an_earlier_layer_that_could_not_be_moved(self):
        self.config('leaving')
        self.tidy.migrate_result = {'guest': {'error': 'Steam is running'}}
        report = self.slot()
        self.assertIs(report['left'], False)
        self.assertTrue(self.layout.config.exists())
        self.assertEqual(report['migrate'], {'guest': {'error': 'Steam is running'}})
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_a_migration_that_raises_is_reported_and_the_slot_goes_on(self):
        self.config('on')
        self.tidy.migrate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no room'))
        report = self.slot()
        self.assertEqual(report['migrate_error'], 'no room')
        self.assertIn(('commit', ['deck', 'guest']), self.tidy.calls)

    def test_leaving_with_a_kept_game_keeps_the_config_and_the_libraries_playable(self):
        self.config('leaving')
        self.tidy.restore = {'restored': [], 'kept': ['10']}
        report = self.slot()
        self.assertIs(report['left'], False)
        self.assertEqual(store.read_config(self.layout)['mode'], 'leaving')
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_leaving_when_the_restore_raises_keeps_the_config_too(self):
        self.config('leaving')
        self.tidy.fail = True
        report = self.slot()
        self.assertEqual(report['restore_error'], 'no room')
        self.assertIs(report['left'], False)
        self.assertTrue(self.layout.config.exists())
        self.assertEqual(self.calls[-2:], [('mount', 'deck'), ('mount', 'guest')])

    def test_leaving_with_a_busy_library_does_not_restore_and_stays(self):
        self.config('leaving')
        self.busy.add('guest')
        report = self.slot()
        self.assertEqual(report['busy'], ['guest'])
        self.assertIs(report['left'], False)
        self.assertEqual(self.tidy.calls, [])
        self.assertTrue(self.layout.config.exists())

    def test_a_slot_that_cannot_make_the_folders_changes_nothing_and_says_why(self):
        self.config('on')
        self.unpreparable = True
        report = store.slot(self.layout, tidy=self.tidy)
        self.assertIn("root's", report['prepare_error'])
        self.assertNotIn('commit', report)
        self.assertNotIn(('commit',), [call[:1] for call in self.tidy.calls])


class StatusAndShare(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.layout = store.Layout(Path(temp.name) / 'library')
        self.layout.root.mkdir()
        self.tidy = StubTidy()

    def test_status_without_users_uses_the_config_and_lists_pending_and_who_shared(self):
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck']})
        self.layout.steamapps.mkdir(parents=True)
        (self.layout.steamapps / 'appmanifest_10.acf').write_text(depot.vdf_dumps('AppState', {'name': 'A', 'installdir': 'A'}))
        with patch.object(store, 'registered', return_value=[]), \
                patch.object(store, 'account', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000,
                                                                             pw_dir='/nonexistent')), \
                patch.object(store, 'steam_running', return_value=False):
            report = store.status(self.layout, [], tidy=self.tidy)
        self.assertEqual(report['config']['users'], ['deck'])
        self.assertEqual(list(report['sides']), ['deck'])
        self.assertEqual(report['pending'], [{'appid': '10'}])
        self.assertEqual(report['apps']['10']['shared_by'], 'deck')

    def test_status_tolerates_a_missing_tidy_module(self):
        def missing():
            raise FileNotFoundError('dosplex-game-store-tidy.py')
        with patch.object(store, 'tidy_module', missing):
            report = store.status(self.layout, [])
        self.assertEqual(report['tidy'], 'missing')
        self.assertIsNone(report['config'])

    def test_share_all_shares_every_entry_without_a_reason_and_names_what_it_refused(self):
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']})
        self.tidy.refuse.add('12')
        lines = []
        summary = store.share(self.layout, 'deck', [], True, tidy=self.tidy, progress=lines.append)
        self.assertEqual([item['appid'] for item in summary['shared']], ['10'])
        self.assertEqual(summary['refused'], [{'appid': '12', 'reason': '12 is not installed'}])
        self.assertNotIn(('share', 'deck', '11', ['deck', 'guest']), self.tidy.calls)
        self.assertIn(('share', 'deck', '10', ['deck', 'guest']), self.tidy.calls)
        self.assertEqual(lines, ['sharing 10', '  1 GiB checked', '  3 GiB checked', 'sharing 12'])

    def test_share_all_leaves_steams_own_tools_alone(self):
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']})
        self.tidy.shareable = lambda layout, user, users: [
            {'appid': '10', 'name': 'A Game', 'source': 'library', 'library': '/x', 'reason': None},
            {'appid': '1628350', 'name': 'Steam Linux Runtime 3.0 (sniper)', 'source': 'library', 'library': '/x', 'reason': None},
            {'appid': '4628710', 'name': 'Proton 11.0', 'source': 'library', 'library': '/x', 'reason': None}]
        summary = store.share(self.layout, 'deck', [], True, tidy=self.tidy)
        self.assertEqual([item['appid'] for item in summary['shared']], ['10'])
        self.assertEqual(summary['skipped_tools'], ['1628350', '4628710'])

    def test_share_needs_an_account_of_the_store(self):
        with self.assertRaisesRegex(store.StoreError, 'not an account'):
            store.share(self.layout, 'deck', ['10'], False, tidy=self.tidy)
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['guest']})
        with self.assertRaisesRegex(store.StoreError, 'not an account'):
            store.share(self.layout, 'deck', ['10'], False, tidy=self.tidy)


class DiskPlaces(unittest.TestCase):
    def setUp(self):
        self.primary = store.Layout('/srv/dosplex-library')
        self.disk = store.Layout.for_disk(self.primary, '/run/media/deck/abc')
        self.key = hashlib.sha256(b'/run/media/deck/abc/dosplex-library/disk').hexdigest()[:12]

    def test_a_disk_store_has_the_same_folders_below_its_own_root(self):
        self.assertEqual(str(self.disk.root), '/run/media/deck/abc/dosplex-library/disk')
        self.assertEqual(str(self.disk.wrapper), '/run/media/deck/abc/dosplex-library')
        self.assertEqual(str(self.disk.steamapps), '/run/media/deck/abc/dosplex-library/disk/store/steamapps')
        self.assertEqual(str(self.disk.sides), '/run/media/deck/abc/dosplex-library/disk/sides')
        self.assertEqual(str(self.disk.config), '/run/media/deck/abc/dosplex-library/disk/config.json')
        self.assertEqual(str(self.disk.lock), '/run/media/deck/abc/dosplex-library/disk/.lock')
        self.assertEqual((self.disk.key, self.disk.primary, str(self.disk.mount_point)),
                         (self.key, self.primary, '/run/media/deck/abc'))
        self.assertEqual((self.primary.primary, self.primary.mount_point, self.primary.wrapper), (None, None, None))

    def test_the_work_and_the_stash_are_on_the_disk_and_the_view_and_the_real_folder_are_not(self):
        side = self.disk.side('guest')
        self.assertEqual((str(side.base), str(side.work), str(side.stash), str(side.conflicts)), (
            '/run/media/deck/abc/dosplex-library/disk/sides/guest',
            '/run/media/deck/abc/dosplex-library/disk/sides/guest/overlay-work',
            '/run/media/deck/abc/dosplex-library/disk/stash/guest',
            '/run/media/deck/abc/dosplex-library/disk/conflicts/guest'))
        self.assertEqual(str(side.reach), f'/srv/dosplex-library/sides/guest/real-{self.key}')
        self.assertEqual(str(side.view), f'/run/dosplex-library-disks/{self.key}/guest')
        self.assertEqual([str(item) for item in self.disk.view_folders],
                         ['/run/dosplex-library-disks', f'/run/dosplex-library-disks/{self.key}'])
        self.assertEqual((str(side.retired.library), str(side.retired.bound), str(side.retired.upper)), (
            f'/srv/dosplex-library/sides/guest/library-{self.key}', f'/srv/dosplex-library/sides/guest/layer-{self.key}',
            '/run/media/deck/abc/dosplex-library/disk/sides/guest/upper'))

    def test_a_library_that_is_given_has_its_places_below_the_disks_store_and_the_accounts_drives_folder(self):
        visible, real, (home, bind) = self.disk.provisioned('guest')
        self.assertEqual(str(visible), '/run/media/guest/abc/SteamLibrary')
        self.assertEqual(str(real), '/run/media/deck/abc/dosplex-library/disk/accounts/guest/SteamLibrary')
        self.assertEqual((str(home), str(bind)), ('/run/media/deck/abc/dosplex-library/disk/accounts/guest',
                                                  '/run/media/guest/abc'))
        self.assertIsNone(self.primary.provisioned('guest'))
        # Nothing at the top of what is bound looks like a library root: steamapps is one level below.
        self.assertNotEqual(visible.name, 'steamapps')

    def test_the_overlay_of_a_disk_mounts_on_the_accounts_library_there_below_a_view_of_the_disk(self):
        side = self.disk.side('guest', '/run/media/guest/abc/SteamLibrary', real='/x/real')
        entry = SimpleNamespace(pw_uid=1004, pw_gid=1005)
        self.assertEqual(store.view_command(self.disk, side, entry)[-2:],
                         ['/run/media/deck/abc/dosplex-library/disk/store/steamapps', str(side.view)])
        overlay = store.overlay_command(side)
        self.assertEqual(overlay[-1], '/run/media/guest/abc/SteamLibrary/steamapps')
        # The upper directory as root reaches it, under the same mount as the work directory (the kernel wants that).
        self.assertIn('upperdir=/x/real/steamapps', overlay[5])
        self.assertIn('workdir=/run/media/deck/abc/dosplex-library/disk/sides/guest/overlay-work', overlay[5])

    def test_a_path_with_a_colon_is_refused_because_the_environment_joins_them_with_one(self):
        with self.assertRaises(store.StoreError):
            store.overlay_command(store.Layout('/srv/a:b').side('guest', '/srv/homes/guest'))
        with self.assertRaises(store.StoreError):
            store.overlay_command(store.Layout.for_disk(self.primary, '/mnt/a:b').side('guest', '/srv/homes/guest'))

    def test_a_mount_point_must_be_absolute_without_dots(self):
        for mount in ('mnt/usb', '/mnt/../etc'):
            with self.assertRaises(store.StoreError, msg=mount):
                store.Layout.for_disk(self.primary, mount)


class DisksFile(unittest.TestCase):
    ENTRY = {'root': '/mnt/usb/dosplex-library/disk', 'mount': '/mnt/usb'}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.primary = store.Layout(self.base / 'library')
        self.primary.root.mkdir()

    def put(self, content, mode=0o644):
        data = content if isinstance(content, bytes) else (content if isinstance(content, str) else json.dumps(content)).encode()
        self.primary.disks.write_bytes(data)
        self.primary.disks.chmod(mode)

    def test_no_file_means_no_disks(self):
        self.assertEqual(store.read_disks(self.primary), [])

    def test_a_good_file_is_read(self):
        other = {'root': '/run/media/deck/x/dosplex-library/disk', 'mount': '/run/media/deck/x'}
        self.put({'version': 1, 'disks': [self.ENTRY, other]})
        self.assertEqual(store.read_disks(self.primary), [self.ENTRY, other])
        self.put({'version': 1, 'disks': []})
        self.assertEqual(store.read_disks(self.primary), [])

    def test_every_malformed_file_is_refused(self):
        good = {'version': 1, 'disks': [self.ENTRY]}
        many = [{'root': f'/mnt/d{number}/dosplex-library/disk', 'mount': f'/mnt/d{number}'} for number in range(9)]
        bad = {
            'not json': 'x{', 'a list': [], 'extra key': dict(good, more=1), 'missing key': {'version': 1},
            'version 2': dict(good, version=2), 'version true': dict(good, version=True),
            'disks not a list': dict(good, disks={}), 'nine disks': dict(good, disks=many),
            'entry text': dict(good, disks=['/mnt/usb']), 'entry extra key': dict(good, disks=[dict(self.ENTRY, x=1)]),
            'entry missing key': dict(good, disks=[{'root': self.ENTRY['root']}]),
            'root text': dict(good, disks=[dict(self.ENTRY, root=7)]),
            'relative mount': dict(good, disks=[{'root': 'usb/dosplex-library/disk', 'mount': 'usb'}]),
            'dots': dict(good, disks=[{'root': '/mnt/../x/dosplex-library/disk', 'mount': '/mnt/../x'}]),
            'trailing slash': dict(good, disks=[{'root': '/mnt/usb/dosplex-library/disk', 'mount': '/mnt/usb/'}]),
            'root elsewhere': dict(good, disks=[dict(self.ENTRY, root='/mnt/usb/other/disk')]),
            'duplicate': dict(good, disks=[self.ENTRY, dict(self.ENTRY)]), 'not utf-8': b'\xff\xfe',
            'too large': ' ' * (store.CONFIG_LIMIT + 1)}
        for label, content in bad.items():
            self.put(content)
            with self.assertRaises(store.StoreError, msg=label):
                store.read_disks(self.primary)

    def test_eight_disks_are_the_most(self):
        eight = [{'root': f'/mnt/d{number}/dosplex-library/disk', 'mount': f'/mnt/d{number}'} for number in range(8)]
        self.put({'version': 1, 'disks': eight})
        self.assertEqual(len(store.read_disks(self.primary)), 8)

    def test_others_who_can_write_a_link_a_folder_and_a_foreign_owner_are_refused(self):
        for mode in (0o664, 0o646):
            self.put({'version': 1, 'disks': []}, mode)
            with self.assertRaisesRegex(store.StoreError, 'not be writable'):
                store.read_disks(self.primary)
        self.primary.disks.unlink()
        target = self.base / 'target.json'
        target.write_text(json.dumps({'version': 1, 'disks': []}))
        self.primary.disks.symlink_to(target)
        with self.assertRaises(store.StoreError):
            store.read_disks(self.primary)
        self.primary.disks.unlink()
        self.primary.disks.mkdir()
        with self.assertRaises(store.StoreError):
            store.read_disks(self.primary)
        self.primary.disks.rmdir()
        self.put({'version': 1, 'disks': []})
        with patch.object(store.os, 'geteuid', return_value=os.getuid() + 1), \
                self.assertRaisesRegex(store.StoreError, 'belong to root'):
            store.read_disks(self.primary)

    def test_a_step_run_as_an_account_may_read_a_file_of_root(self):
        self.put({'version': 1, 'disks': [self.ENTRY]})
        self.assertEqual(store.read_disks(self.primary, owners=(os.getuid() + 1, os.getuid())), [self.ENTRY])
        with self.assertRaises(store.StoreError):
            store.read_disks(self.primary, owners=(os.getuid() + 1,))

    def test_writing_is_atomic_mode_0644_and_leaves_no_temporary_file(self):
        store.write_disks(self.primary, [self.ENTRY])
        self.assertEqual(os.stat(self.primary.disks).st_mode & 0o777, 0o644)
        self.assertEqual(os.listdir(self.primary.root), ['disks.json'])
        self.assertEqual(store.read_disks(self.primary), [self.ENTRY])
        with self.assertRaises(store.StoreError):
            store.write_disks(self.primary, [{'root': '/x', 'mount': '/y'}])
        self.assertEqual(store.read_disks(self.primary), [self.ENTRY])

    def test_the_earlier_libraries_of_every_listed_disk_are_known_present_or_not(self):
        store.write_disks(self.primary, [self.ENTRY])
        disk = store.Layout.for_disk(self.primary, '/mnt/usb')
        expected = {self.primary.side('deck').retired.library, disk.side('deck').retired.library}
        self.assertEqual(self.primary.retired_libraries('deck'), expected)
        self.assertEqual(disk.retired_libraries('deck'), expected)
        self.assertEqual(len(expected), 2)


class DiskWorld(unittest.TestCase):
    """A primary store and one mount point for a second filesystem, with the inode flag and the devices played."""
    USERS = ['deck', 'guest']

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.primary = store.Layout(self.base / 'library', view_root=self.base / 'run', media_root=self.base / 'media')
        self.primary.root.mkdir()
        store.write_config(self.primary, {'version': 1, 'mode': 'on', 'users': self.USERS})
        self.usb = self.base / 'usb'
        self.usb.mkdir()
        self.immutable = set()
        self.mountinfo = self.base / 'mountinfo'
        self.write_mountinfo()
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.base), pw_name='deck')
        for patcher in (patch.object(store, 'ROOT_UID', os.getuid()), patch.object(store, 'device_of', self.device_of),
                        patch.object(store, 'get_immutable', self.get_immutable),
                        patch.object(store, 'set_immutable', self.set_immutable),
                        patch.object(store, 'real_directory', self.real_directory),
                        patch.object(store, 'account', lambda name: self.entry)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def device_of(self, path):
        return 2 if str(path).startswith(str(self.usb)) else 1

    def get_immutable(self, descriptor):
        return os.fstat(descriptor).st_ino in self.immutable

    def set_immutable(self, descriptor, value=True):
        (self.immutable.add if value else self.immutable.discard)(os.fstat(descriptor).st_ino)

    def real_directory(self, path, **_options):
        Path(path).mkdir(parents=True, exist_ok=True)

    def write_mountinfo(self, kind='ext4', options='rw,relatime', super_options='rw', usb=True):
        lines = ['22 1 0:21 / / rw - ext4 /dev/root rw']
        if usb:
            lines.append(f'50 22 0:50 / {self.usb} {options} - {kind} /dev/sdb1 {super_options}')
        self.mountinfo.write_text('\n'.join(lines) + '\n')

    def add(self):
        return store.add_disk(self.primary, self.usb, mountinfo=self.mountinfo)

    @property
    def disk(self):
        return store.Layout.for_disk(self.primary, self.usb)

    def layouts(self):
        return store.layouts(self.primary, mountinfo=self.mountinfo)


class AddingDisks(DiskWorld):
    def refused(self, pattern):
        with self.assertRaisesRegex(store.StoreError, pattern):
            self.add()
        self.assertEqual(store.read_disks(self.primary), [])

    def test_a_folder_that_is_not_a_mount_point_is_refused(self):
        other = self.base / 'plain'
        other.mkdir()
        with self.assertRaisesRegex(store.StoreError, 'not a mount point'):
            store.add_disk(self.primary, other, mountinfo=self.mountinfo)
        self.write_mountinfo(usb=False)
        self.refused('not a mount point')
        self.assertFalse((self.usb / 'dosplex-library').exists())

    def test_a_filesystem_that_is_not_one_of_the_four_is_refused_by_name(self):
        for kind in ('vfat', 'exfat', 'ntfs3', 'fuseblk'):
            self.write_mountinfo(kind=kind)
            self.refused(kind)
        self.assertFalse((self.usb / 'dosplex-library').exists())

    def test_a_filesystem_mounted_read_only_is_refused(self):
        self.write_mountinfo(options='ro,relatime')
        self.refused('read-only')
        self.write_mountinfo(super_options='ro')
        self.refused('read-only')

    def test_the_filesystem_of_the_primary_is_refused(self):
        with patch.object(store, 'device_of', lambda path: 1):
            self.refused('same filesystem')
        self.assertFalse((self.usb / 'dosplex-library').exists())

    def test_the_primary_must_be_on_in_mode_on(self):
        store.write_config(self.primary, {'version': 1, 'mode': 'leaving', 'users': self.USERS})
        self.refused('not on')
        self.primary.config.unlink()
        self.refused('not on')

    def test_a_wrapper_that_is_not_roots_is_refused_and_so_is_a_link(self):
        with patch.object(store, 'ROOT_UID', os.getuid() + 1):
            self.refused('not a folder of root')
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (self.usb / 'dosplex-library').rmdir()
        (self.usb / 'dosplex-library').symlink_to(elsewhere)
        self.refused('not a real folder')
        self.assertEqual(os.listdir(elsewhere), [])
        (self.usb / 'dosplex-library').unlink()
        (self.usb / 'dosplex-library').mkdir(mode=0o777)
        (self.usb / 'dosplex-library').chmod(0o777)
        self.refused('others')

    def test_a_filesystem_that_cannot_take_the_flag_is_refused_and_not_listed(self):
        def cannot(descriptor, value=True):
            raise store.StoreError('this filesystem cannot take the immutable flag (Operation not supported)')
        with patch.object(store, 'set_immutable', cannot):
            self.refused('cannot take the immutable flag')

    def test_a_flag_that_did_not_stick_is_refused(self):
        with patch.object(store, 'set_immutable', lambda descriptor, value=True: None):
            self.refused('did not take')

    def test_a_disk_gets_its_wrapper_flag_folders_config_and_entry(self):
        result = self.add()
        self.assertEqual(result, {'root': str(self.disk.root), 'mount': str(self.usb), 'added': True})
        self.assertIn(os.stat(self.usb / 'dosplex-library').st_ino, self.immutable)
        for path in (self.disk.root, self.disk.common, self.disk.sides):
            self.assertTrue(path.is_dir(), path)
        self.assertEqual(store.read_config(self.disk), store.read_config(self.primary))
        self.assertEqual(store.read_disks(self.primary), [{'root': str(self.disk.root), 'mount': str(self.usb)}])

    def test_doing_it_again_changes_nothing(self):
        self.add()
        before = [os.stat(path).st_mtime_ns for path in (self.primary.disks, self.disk.config)]
        flags = set(self.immutable)
        self.assertFalse(self.add()['added'])
        self.assertEqual([os.stat(path).st_mtime_ns for path in (self.primary.disks, self.disk.config)], before)
        self.assertEqual(self.immutable, flags)
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_a_disk_that_lost_its_config_gets_the_primarys_again(self):
        self.add()
        self.disk.config.unlink()
        self.assertFalse(self.add()['added'])
        self.assertEqual(store.read_config(self.disk)['users'], self.USERS)


class RealSecondFilesystem(unittest.TestCase):
    def test_add_disk_compares_the_real_devices(self):
        shm = Path('/dev/shm')
        with tempfile.TemporaryDirectory(prefix='dosplex-store-') as first:
            if not shm.is_dir() or os.stat(shm).st_dev == os.stat(first).st_dev:
                self.skipTest('no second filesystem to test with')
            with tempfile.TemporaryDirectory(prefix='dosplex-store-', dir=shm) as second:
                primary = store.Layout(Path(first) / 'library', view_root=Path(first) / 'run')
                primary.root.mkdir()
                store.write_config(primary, {'version': 1, 'mode': 'on', 'users': ['deck']})
                info = Path(first) / 'mountinfo'
                info.write_text(f'22 1 0:21 / / rw - ext4 /dev/root rw\n50 22 0:50 / {second} rw - ext4 /dev/sdb1 rw\n'
                                f'51 22 0:51 / {first} rw - ext4 /dev/sdb2 rw\n')
                flagged = set()
                with patch.object(store, 'ROOT_UID', os.getuid()), \
                        patch.object(store, 'real_directory', lambda path, **_o: Path(path).mkdir(parents=True, exist_ok=True)), \
                        patch.object(store, 'get_immutable', lambda fd: os.fstat(fd).st_ino in flagged), \
                        patch.object(store, 'set_immutable', lambda fd, value=True: flagged.add(os.fstat(fd).st_ino)):
                    with self.assertRaisesRegex(store.StoreError, 'same filesystem'):
                        store.add_disk(primary, first, mountinfo=info)
                    self.assertTrue(store.add_disk(primary, second, mountinfo=info)['added'])
                self.assertEqual(len(flagged), 1)


class ListingDisks(DiskWorld):
    def test_no_list_means_the_primary_alone_and_the_mount_table_is_not_read(self):
        self.assertEqual(store.layouts(self.primary, mountinfo='/nonexistent'), ([self.primary], []))

    def test_a_connected_whole_disk_comes_after_the_primary(self):
        self.add()
        present, absent = self.layouts()
        self.assertEqual([str(item.root) for item in present], [str(self.primary.root), str(self.disk.root)])
        self.assertEqual((present[1].primary, str(present[1].mount_point)), (self.primary, str(self.usb)))
        self.assertEqual(absent, [])

    def test_a_disk_that_is_not_mounted_is_absent_with_the_reason(self):
        self.add()
        self.write_mountinfo(usb=False)
        present, absent = self.layouts()
        self.assertEqual(present, [self.primary])
        self.assertEqual(absent, [{'root': str(self.disk.root), 'mount': str(self.usb), 'reason': f'{self.usb} is not mounted'}])

    def test_a_wrapper_that_lost_its_flag_or_was_swapped_makes_the_disk_absent(self):
        self.add()
        self.immutable.clear()
        self.assertIn('not immutable', self.layouts()[1][0]['reason'])
        self.immutable.add(os.stat(self.usb / 'dosplex-library').st_ino)
        self.assertEqual(self.layouts()[1], [])
        (self.usb / 'dosplex-library').rename(self.usb / 'moved')
        (self.usb / 'dosplex-library').symlink_to(self.usb / 'moved')
        self.assertIn('cannot be opened', self.layouts()[1][0]['reason'])

    def test_a_disk_that_is_mounted_read_only_is_absent(self):
        self.add()
        self.write_mountinfo(options='ro,relatime')
        self.assertIn('read-only', self.layouts()[1][0]['reason'])

    def fake_mount(self, calls=None, fail_disk=False):
        def fake(layout, name, **_options):
            if calls is not None:
                calls.append(('mount', str(layout.root)))
            if fail_disk and layout.primary is not None:
                raise store.StoreError('no such device')
            library = Path(f'/srv/homes/{name}/steam-{layout.key}')
            layout.libraries[name] = (library, library)
            return ['library']
        return fake

    def test_every_library_is_mounted_for_the_account_and_a_failing_disk_is_left_off(self):
        self.add()
        calls = []
        with patch.object(store, 'mount', self.fake_mount(calls, fail_disk=True)):
            paths, problems = store.mount_all(self.primary, 'deck', mountinfo=self.mountinfo)
        self.assertEqual(paths, [f'/srv/homes/deck/steam-{self.primary.key}/steamapps'])
        self.assertEqual(problems, [f'disk {self.usb}: no such device'])
        self.assertEqual(calls, [('mount', str(self.primary.root)), ('mount', str(self.disk.root))])

    def test_every_connected_disk_has_its_overlay_in_the_paths_in_order(self):
        self.add()
        with patch.object(store, 'mount', self.fake_mount()):
            paths, problems = store.mount_all(self.primary, 'deck', mountinfo=self.mountinfo)
        self.assertEqual(problems, [])
        self.assertEqual(paths, [f'/srv/homes/deck/steam-{self.primary.key}/steamapps',
                                 f'/srv/homes/deck/steam-{self.disk.key}/steamapps'])

    def test_a_disk_that_is_not_connected_is_skipped_and_its_library_stays_on_the_list(self):
        self.add()
        self.write_mountinfo(usb=False)
        calls = []
        with patch.object(store, 'mount', self.fake_mount(calls)), \
                patch.object(store, 'register', lambda *a, **k: calls.append('the list was changed')):
            paths, problems = store.mount_all(self.primary, 'deck', mountinfo=self.mountinfo)
        self.assertEqual(paths, [f'/srv/homes/deck/steam-{self.primary.key}/steamapps'])
        self.assertEqual(problems, [])
        self.assertEqual(calls, [('mount', str(self.primary.root))])

    def test_a_broken_list_of_disks_leaves_the_primary_mounted_and_is_said(self):
        self.primary.disks.write_text('x{')
        self.primary.disks.chmod(0o644)
        with patch.object(store, 'mount', self.fake_mount()):
            paths, problems = store.mount_all(self.primary, 'deck', mountinfo=self.mountinfo)
        self.assertEqual(paths, [f'/srv/homes/deck/steam-{self.primary.key}/steamapps'])
        self.assertIn('list of disks is unusable', problems[0])

    def test_the_folders_of_a_disk_side_and_its_mount_commands(self):
        self.add()
        commands = []

        def runner(command, **_options):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        library = self.usb / 'Library'
        (library / 'steamapps').mkdir(parents=True)
        disk = self.disk
        disk.libraries['deck'] = (library, library)
        proc = self.base / 'proc'
        proc.mkdir()
        self.assertEqual(store.mount(disk, 'deck', runner=runner, mountinfo=self.mountinfo, proc=proc),
                         ['real', 'library', 'prefixes'])
        side = disk.side('deck')
        for path in (self.primary.sides / 'deck', side.base, side.work, side.view, side.reach, side.steamapps):
            self.assertTrue(path.is_dir(), path)
        self.assertFalse(side.retired.upper.exists())          # the earlier layer is not made again
        # The account's real folder is bound below the primary, where the account can walk to it, before the overlay
        # covers it.
        self.assertEqual(side.reach, self.primary.sides / 'deck' / f'real-{disk.key}')
        self.assertEqual(commands[0], ['mount', '--bind', '-o', 'nosuid,nodev', str(library / 'steamapps'), str(side.reach)])
        self.assertEqual(commands[1], ['mount', '--make-private', str(side.reach)])
        self.assertEqual(commands[2][-2:], [str(disk.steamapps), str(side.view)])
        self.assertEqual(commands[3][-1], str(library / 'steamapps'))
        self.assertEqual(commands[4][-2:], [str(side.reach / 'compatdata'), str(library / 'steamapps/compatdata')])
        self.assertEqual(commands[5], ['umount', str(side.view)])

    def test_a_reach_that_is_bound_already_is_not_made_again_as_a_folder_of_roots(self):
        side = self.primary.side('deck', '/srv/homes/deck/Steam')
        made = []
        with patch.object(store, 'is_mount_point', lambda path: Path(path) == side.reach), \
                patch.object(store, 'real_directory', lambda path, **options: made.append(Path(path))):
            store.prepare_side(self.primary, side, self.entry)
        self.assertNotIn(side.reach, made)
        made.clear()
        with patch.object(store, 'is_mount_point', lambda path: False), \
                patch.object(store, 'real_directory', lambda path, **options: made.append(Path(path))):
            store.prepare_side(self.primary, side, self.entry)
        self.assertIn(side.reach, made)
        self.assertEqual(side.reach, self.primary.sides / 'deck' / 'real')

    def test_a_plain_unmount_keeps_the_media_bind_of_a_given_library_and_leaving_takes_it_away(self):
        self.add()
        side = self.disk.side('deck', self.usb / 'Library')
        bind = side.provisioned[2][1]
        mounted = self.base / 'mountinfo-bound'
        mounted.write_text(Path(self.mountinfo).read_text() +
                           f'90 1 0:9 / {bind} rw - ext4 /dev/x rw\n'
                           f'91 1 0:9 / {side.steamapps} rw - overlay o rw,lowerdir=a,upperdir=b,workdir={side.work}\n')
        commands = []

        def runner(command, **_options):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        self.assertEqual(store.unmount(self.disk, 'deck', runner=runner, mountinfo=mounted, stash=False), ['library'])
        self.assertEqual(store.unmount(self.disk, 'deck', runner=runner, mountinfo=mounted, layer=True, stash=False),
                         ['library', 'media'])
        self.assertEqual(commands[-1], ['umount', str(bind)])

    def test_leaving_never_unmounts_the_disk_itself_for_the_account_that_reaches_it_there(self):
        # On SteamOS the disk is mounted at /run/media/deck/<uuid>, which is also where Main's "media bind" would be:
        # at shutdown the release tried to unmount the card itself and stopped there (Deck, 2026-11-18).
        self.add()
        disk = store.Layout.for_disk(self.primary, self.usb)
        given = disk.provisioned

        def at_the_mount_point(name):
            visible, real, (home, _bind) = given(name)
            return disk.mount_point / 'SteamLibrary', real, (home, disk.mount_point)
        disk.provisioned = at_the_mount_point
        side = disk.side('deck', self.usb / 'Library')
        self.assertEqual(Path(side.provisioned[2][1]), disk.mount_point)
        mounted = self.base / 'mountinfo-card'
        mounted.write_text(Path(self.mountinfo).read_text() + f'90 1 0:9 / {disk.mount_point} rw - ext4 /dev/x rw\n')
        commands = []

        def runner(command, **_options):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        store.unmount(disk, 'deck', runner=runner, mountinfo=mounted, layer=True, stash=False)
        self.assertNotIn(['umount', str(disk.mount_point)], commands)
        with self.assertRaisesRegex(store.StoreError, 'its own mount point'):
            store.provision(disk, 'deck', self.entry, runner=runner, mountinfo=mounted)


class SlotWithDisks(DiskWorld):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.running = set()
        self.tidy = StubTidy()
        for name, function in (('account', self.account), ('steam_running', self.steam_running), ('unmount', self.unmount),
                               ('mount', self.mount), ('register', self.register), ('prepare_sides', self.prepare_sides),
                               ('online', lambda routes=None: True)):
            patcher = patch.object(store, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def label(self, layout):
        return 'primary' if layout.primary is None else 'disk'

    def account(self, name):
        return SimpleNamespace(pw_uid={'deck': 1000, 'guest': 1001}[name], pw_gid=1000, pw_dir='/nonexistent')

    def steam_running(self, uid, proc=None):
        return uid in self.running

    def prepare_sides(self, layout, users, **_options):
        self.calls.append(('prepare', self.label(layout)))
        return {}

    def unmount(self, layout, name, **_options):
        self.calls.append(('unmount', name, self.label(layout)))
        return ['library']

    def mount(self, layout, name, **_options):
        self.calls.append(('mount', name, self.label(layout)))
        return ['library']

    def register(self, layout, name, add=True, **_options):
        self.calls.append(('register' if add else 'unregister', name, self.label(layout)))
        return []

    def slot(self, **options):
        return store.slot(self.primary, tidy=self.tidy, mountinfo=self.mountinfo, **options)

    def set_mode(self, layout, mode, users=None):
        store.write_config(layout, {'version': 1, 'mode': mode, 'users': users or self.USERS})

    def test_each_disk_is_worked_on_before_the_primary(self):
        self.add()
        report = self.slot()
        self.assertEqual(self.tidy.order, [str(self.disk.root), str(self.primary.root)])
        self.assertLess(self.calls.index(('unmount', 'deck', 'disk')), self.calls.index(('unmount', 'deck', 'primary')))
        self.assertLess(self.calls.index(('mount', 'guest', 'disk')), self.calls.index(('unmount', 'deck', 'primary')))
        self.assertEqual(report['commit'], self.tidy.commit_result)
        self.assertEqual(report['config']['mode'], 'on')
        self.assertEqual(report['disks'][str(self.disk.root)]['commit'], self.tidy.commit_result)
        json.dumps(report)

    def test_without_disks_the_report_has_the_primarys_keys_and_an_empty_disks(self):
        report = self.slot()
        self.assertEqual(sorted(report), ['commit', 'config', 'disks', 'lists', 'migrate'])
        self.assertEqual(report['disks'], {})

    def test_a_disk_gets_the_mode_and_users_of_the_primary_first(self):
        self.add()
        self.set_mode(self.disk, 'leaving', ['guest'])
        self.slot()
        self.assertEqual(store.read_config(self.disk), {'version': 1, 'mode': 'on', 'users': self.USERS})

    def test_a_running_steam_refuses_before_any_disk_is_touched(self):
        self.add()
        self.running.add(1001)
        with self.assertRaisesRegex(store.StoreError, "guest's Steam is running"):
            self.slot()
        self.assertEqual(self.calls, [])

    def test_a_disk_that_is_not_connected_is_reported_and_its_libraries_stay_on_the_steam_lists(self):
        self.add()
        self.write_mountinfo(usb=False)
        report = self.slot()
        self.assertEqual(report['disks'], {str(self.disk.root): {'absent': f'{self.usb} is not mounted'}})
        self.assertNotIn('unregister', [call[0] for call in self.calls])
        self.assertNotIn(('mount', 'deck', 'disk'), self.calls)
        self.assertEqual(self.tidy.order, [str(self.primary.root)])
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_leaving_frees_a_disk_that_gave_everything_back_and_then_the_primary_leaves(self):
        self.add()
        self.set_mode(self.primary, 'leaving')
        report = self.slot()
        entry = report['disks'][str(self.disk.root)]
        self.assertTrue(report['left'])
        self.assertEqual((entry['left'], entry['unlisted']), (True, True))
        self.assertFalse(self.disk.config.exists())
        self.assertFalse(self.primary.config.exists())
        self.assertEqual(store.read_disks(self.primary), [])
        self.assertEqual(self.immutable, set())
        self.assertTrue(self.disk.sides.is_dir())      # the folders stay; nothing is deleted
        self.assertTrue((self.usb / 'dosplex-library').is_dir())

    def test_leaving_with_a_disk_that_is_not_connected_keeps_the_primarys_config_and_libraries(self):
        self.add()
        self.write_mountinfo(usb=False)
        self.set_mode(self.primary, 'leaving')
        report = self.slot()
        self.assertIs(report['left'], False)
        self.assertEqual(report['restore'], self.tidy.restore)       # the primary still gave its own games back
        self.assertEqual(store.read_config(self.primary)['mode'], 'leaving')
        self.assertEqual(self.calls[-2:], [('mount', 'deck', 'primary'), ('mount', 'guest', 'primary')])
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_leaving_with_a_game_kept_on_a_disk_keeps_that_disk_and_the_primary_config(self):
        self.add()
        self.set_mode(self.primary, 'leaving')
        self.tidy.restore_for[str(self.disk.root)] = {'restored': [], 'kept': [{'appid': '20', 'reason': 'no room'}]}
        report = self.slot()
        entry = report['disks'][str(self.disk.root)]
        self.assertIs(entry['left'], False)
        self.assertNotIn('unlisted', entry)
        self.assertEqual(store.read_config(self.disk)['mode'], 'leaving')
        self.assertEqual(len(store.read_disks(self.primary)), 1)
        self.assertEqual(self.immutable, {os.stat(self.usb / 'dosplex-library').st_ino})
        self.assertEqual(self.tidy.order, [str(self.disk.root), str(self.primary.root)])
        self.assertIs(report['left'], False)
        self.assertTrue(self.primary.config.exists())
        self.assertIn(('mount', 'deck', 'disk'), self.calls)

    def test_a_disk_that_cannot_be_unlisted_stays_and_so_does_the_primary(self):
        self.add()
        self.set_mode(self.primary, 'leaving')
        with patch.object(store, 'write_disks', side_effect=OSError('read-only file system')):
            report = self.slot()
        self.assertIs(report['disks'][str(self.disk.root)]['left'], False)
        self.assertIn('read-only', report['disks'][str(self.disk.root)]['unlist_error'])
        self.assertIs(report['left'], False)

    def test_a_listed_disk_is_given_time_to_be_mounted_and_not_more(self):
        self.add()
        self.write_mountinfo(usb=False)
        clock, sleeps, appears = [0], [], [True]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            if appears[0] and len(sleeps) == 2:
                self.write_mountinfo()
        report = self.slot(wait=5, sleep=sleep, clock=lambda: clock[0])
        self.assertEqual(sleeps, [1, 1])
        self.assertIn('commit', report['disks'][str(self.disk.root)])
        sleeps.clear()
        clock[0] = 0
        appears[0] = False
        self.write_mountinfo(usb=False)
        report = self.slot(wait=3, sleep=sleep, clock=lambda: clock[0])
        self.assertEqual(sleeps, [1, 1, 1])
        self.assertIn('absent', report['disks'][str(self.disk.root)])
        sleeps.clear()
        self.slot(sleep=sleep, clock=lambda: clock[0])
        self.assertEqual(sleeps, [])

    def test_off_writes_leaving_to_every_connected_disk(self):
        self.add()
        store.turn_off(self.primary, mountinfo=self.mountinfo)
        self.assertEqual(store.read_config(self.primary)['mode'], 'leaving')
        self.assertEqual(store.read_config(self.disk)['mode'], 'leaving')


class ForgettingAndStatus(DiskWorld):
    def setUp(self):
        super().setUp()
        self.unlisted = []
        self.add()
        patcher = patch.object(store, 'register', self.register)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, layout, name, add=True, **_options):
        if getattr(self, 'steam_open', False):
            raise store.StoreError(f"{name}'s Steam is running; its library list can only be changed while it is closed")
        self.unlisted.append((name, add, str(layout.root)))
        return []

    def forget(self):
        return store.forget(self.primary, str(self.disk.root), mountinfo=self.mountinfo)

    def test_a_disk_that_is_not_connected_is_taken_off_the_list_and_every_users_steam_list(self):
        self.write_mountinfo(usb=False)
        self.assertEqual(self.forget()['users'], self.USERS)
        self.assertEqual(self.unlisted, [('deck', False, str(self.disk.root)), ('guest', False, str(self.disk.root))])
        self.assertEqual(store.read_disks(self.primary), [])

    def test_a_connected_disk_is_refused_with_the_way_out(self):
        with self.assertRaisesRegex(store.StoreError, 'is connected; turn the shared game library off instead'):
            self.forget()
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_an_unknown_disk_is_refused(self):
        with self.assertRaisesRegex(store.StoreError, 'not a listed disk'):
            store.forget(self.primary, '/mnt/other/dosplex-library/disk', mountinfo=self.mountinfo)

    def test_a_running_steam_refuses_and_the_disk_stays_listed(self):
        self.write_mountinfo(usb=False)
        self.steam_open = True
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            self.forget()
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_status_names_every_disk_with_what_it_holds_or_why_it_is_away(self):
        self.disk.steamapps.mkdir(parents=True, exist_ok=True)
        (self.disk.steamapps / 'appmanifest_20.acf').write_text(depot.vdf_dumps('AppState', {'name': 'On usb', 'installdir': 'U'}))
        with patch.object(store, 'registered', return_value=[]), patch.object(store, 'steam_running', return_value=False):
            report = store.status(self.primary, [], tidy=StubTidy(), mountinfo=self.mountinfo)
            self.assertEqual(report['root'], str(self.primary.root))
            item, = report['disks']
            self.assertEqual((item['root'], item['mount'], item['present'], item['reason']),
                             (str(self.disk.root), str(self.usb), True, None))
            self.assertEqual(list(item['apps']), ['20'])
            self.assertEqual((item['pending'], sorted(item['sides'])), ([{'appid': '10'}], self.USERS))
            self.assertIsNone(item['sides']['deck']['library'])           # deck has no library on that disk
            self.assertEqual((item['sides']['deck']['stash'], item['sides']['deck']['conflicts']),
                             ({'journal': None, 'entries': 0}, []))
            self.assertIs(item['sides']['deck']['mounted'], False)
            self.write_mountinfo(usb=False)
            item, = store.status(self.primary, [], tidy=StubTidy(), mountinfo=self.mountinfo)['disks']
        self.assertEqual((item['present'], item['reason'], item['apps'], item['pending'], item['sides']),
                         (False, f'{self.usb} is not mounted', {}, [], {}))


class Stash(unittest.TestCase):
    """The stash: what stock Steam must not find while the overlay is not mounted, moved out by rename and back again."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.layout = store.Layout(self.base / 'library', view_root=self.base / 'run')
        self.steam = self.base / 'home/deck/.local/share/Steam'
        self.apps = self.steam / 'steamapps'
        self.layout.libraries['deck'] = (self.steam, self.steam)
        self.side = self.layout.side('deck')
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.base / 'home/deck'),
                                     pw_name='deck')
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        (self.layout.root / 'apps').mkdir(parents=True)
        for appid, installdir in (('100', 'Game'), ('101', 'Other')):
            (self.layout.root / 'apps' / f'{appid}.json').write_text(json.dumps({'installdir': installdir}))
        (self.apps / 'common/Game/save').mkdir(parents=True)
        (self.apps / 'common/Game/data').mkdir()
        (self.apps / 'common/Free').mkdir()
        (self.apps / 'appmanifest_100.acf').write_text('one hundred')
        make_whiteout(self.apps / 'appmanifest_101.acf')
        (self.apps / 'appmanifest_300.acf').write_text('not stored')
        make_whiteout(self.apps / 'common/Game/data/b.dat')
        (self.apps / 'common/Game/save/slot1').write_text('progress')
        make_whiteout(self.apps / 'common/Other')
        make_whiteout(self.apps / 'common/Free/gone')
        for patcher in (patch.object(store, 'account', return_value=self.entry),
                        patch.object(store, 'is_whiteout', fake_whiteout)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.before = self.listing()

    def listing(self, folder=None):
        folder = folder or self.apps
        return sorted((str(path.relative_to(folder)), os.lstat(path).st_ino) for path in folder.rglob('*'))

    def stash(self):
        return store.stash_manifests(self.layout, self.side, self.entry, proc=self.proc)

    def restore(self):
        return store.restore_manifests(self.layout, self.side, self.entry, proc=self.proc)

    def test_the_manifests_of_stored_games_and_the_whiteouts_for_them_leave_by_rename(self):
        self.assertEqual(self.stash(), ['appmanifest_100.acf', 'appmanifest_101.acf', 'common/Game/data/b.dat',
                                        'common/Other'])
        self.assertEqual([name for name, _inode in self.listing()],
                         ['appmanifest_300.acf', 'common', 'common/Free', 'common/Free/gone', 'common/Game',
                          'common/Game/data', 'common/Game/save', 'common/Game/save/slot1'])
        kept = dict(self.listing(self.side.stash / 'steamapps'))
        inodes = dict(self.before)
        for name in ('appmanifest_100.acf', 'appmanifest_101.acf', 'common/Game/data/b.dat', 'common/Other'):
            self.assertEqual(kept[name], inodes[name], name)
        self.assertEqual(store.stash_state(self.side), {'journal': 'stashed', 'entries': 4})
        self.assertEqual(stat.S_IMODE(os.stat(self.side.stash).st_mode), 0o700)

    def test_putting_back_restores_every_entry_and_removes_the_stash(self):
        self.stash()
        self.assertEqual(self.restore(), ['appmanifest_100.acf', 'appmanifest_101.acf', 'common/Game/data/b.dat',
                                          'common/Other'])
        self.assertEqual(self.listing(), self.before)
        self.assertFalse(self.side.stash.exists())
        self.assertEqual(store.stash_state(self.side), {'journal': None, 'entries': 0})

    def test_a_second_stash_and_a_second_restore_do_nothing(self):
        self.stash()
        self.assertEqual(self.stash(), [])
        self.restore()
        self.assertEqual(self.restore(), [])
        self.assertEqual(self.listing(), self.before)

    def test_dying_in_the_middle_of_either_step_loses_nothing_and_the_other_step_finishes(self):
        class Dead(BaseException):
            pass
        real_rename = os.rename
        for step in ('stash', 'restore'):
            for number in (1, 2, 3, 4):
                with self.subTest(step=step, number=number):
                    count = [0]

                    def rename(*args, **kwargs):
                        count[0] += 1
                        if count[0] == number:
                            raise Dead()
                        return real_rename(*args, **kwargs)
                    if step == 'restore':
                        self.stash()
                    with patch.object(store.os, 'rename', rename), self.assertRaises(Dead):
                        (self.stash if step == 'stash' else self.restore)()
                    self.assertEqual(store.stash_state(self.side)['journal'], 'stashing' if step == 'stash' else 'restoring')
                    # What stock Steam sees now is partial, but nothing is gone: the other step puts it all back.
                    names = [name for name, _ in self.listing()] + [
                        f'stash/{name}' for name, _ in self.listing(self.side.stash / 'steamapps')
                        if (self.side.stash / 'steamapps').exists()]
                    self.assertGreaterEqual(len(names), len(self.before) - 4)
                    self.restore()
                    self.assertEqual(self.listing(), self.before)
                    self.assertFalse(self.side.stash.exists())
                    self.assertEqual(self.stash(), ['appmanifest_100.acf', 'appmanifest_101.acf',
                                                    'common/Game/data/b.dat', 'common/Other'])
                    self.restore()

    def test_a_name_that_exists_again_when_the_stash_comes_back_is_kept_in_conflicts_not_overwritten(self):
        self.stash()
        (self.apps / 'appmanifest_100.acf').write_text('made by a stock Steam')
        self.restore()
        self.assertEqual((self.apps / 'appmanifest_100.acf').read_text(), 'made by a stock Steam')
        self.assertEqual((self.side.conflicts / 'stash/appmanifest_100.acf').read_text(), 'one hundred')
        self.assertTrue(fake_whiteout(os.lstat(self.apps / 'appmanifest_101.acf')))
        self.assertFalse(self.side.stash.exists())
        self.assertEqual(store.conflicts_of(self.side), ['stash/appmanifest_100.acf'])

    def manifest(self, appid, buildid):
        return depot.vdf_dumps('AppState', {'appid': str(appid), 'name': 'Game', 'StateFlags': '4',
                                            'installdir': 'Game', 'buildid': str(buildid)})

    def test_a_newer_manifest_in_the_library_replaces_the_older_stashed_one(self):
        # A second stash must not keep the older stashed manifest over the newer one the library holds (LIB-07):
        # the newer one goes to the stash, and the older one is kept in the account's conflicts.
        self.stash()
        (self.apps / 'appmanifest_100.acf').write_text(self.manifest('100', '8'))
        self.assertEqual(self.stash(), ['appmanifest_100.acf'])
        self.assertEqual((self.side.stash / 'steamapps/appmanifest_100.acf').read_text(), self.manifest('100', '8'))
        self.assertEqual((self.side.conflicts / 'stash/appmanifest_100.acf').read_text(), 'one hundred')

    def test_the_newer_of_a_stashed_and_a_live_manifest_of_the_same_app_is_put_back(self):
        # When the stash is put back and the library holds a manifest of the same app, the newer one stays and the
        # older one is kept in the account's conflicts (LIB-07).
        self.stash()
        (self.side.stash / 'steamapps/appmanifest_100.acf').write_text(self.manifest('100', '8'))
        (self.apps / 'appmanifest_100.acf').write_text(self.manifest('100', '7'))
        self.restore()
        self.assertEqual((self.apps / 'appmanifest_100.acf').read_text(), self.manifest('100', '8'))
        self.assertEqual((self.side.conflicts / 'stash/appmanifest_100.acf').read_text(), self.manifest('100', '7'))

    def test_a_name_that_is_in_the_stash_already_is_not_overwritten_by_a_second_stash(self):
        self.stash()
        (self.apps / 'appmanifest_100.acf').write_text('made again')
        self.assertEqual(self.stash(), ['appmanifest_100.acf'])
        self.assertEqual((self.side.conflicts / 'stash/appmanifest_100.acf').read_text(), 'made again')
        self.assertEqual((self.side.stash / 'steamapps/appmanifest_100.acf').read_text(), 'one hundred')

    def test_nothing_moves_while_the_accounts_steam_runs(self):
        (self.proc / '77').mkdir()
        (self.proc / '77/comm').write_text('steam\n')
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            self.stash()
        self.assertEqual(self.listing(), self.before)
        self.assertFalse(self.side.stash.exists())
        (self.proc / '77/comm').write_text('other\n')
        self.stash()
        (self.proc / '77/comm').write_text('steam\n')
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            self.restore()
        self.assertTrue((self.side.stash / 'steamapps/appmanifest_100.acf').exists())

    def test_nothing_is_stashed_and_no_journal_is_made_when_the_store_holds_nothing(self):
        for path in (self.layout.root / 'apps').iterdir():
            path.unlink()
        self.assertEqual(self.stash(), [])
        self.assertFalse(self.side.stash.exists())

    def test_a_game_folder_that_is_a_link_is_not_followed(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        make_whiteout(elsewhere / 'x.dat')
        shutil.rmtree(self.apps / 'common/Game')
        (self.apps / 'common/Game').symlink_to(elsewhere)
        self.stash()
        self.assertEqual(os.listdir(elsewhere), ['x.dat'])

    def test_a_library_that_is_not_the_accounts_is_never_worked_in(self):
        self.entry.pw_uid = os.getuid() + 1
        with self.assertRaisesRegex(store.StoreError, 'does not belong'):
            self.stash()
        self.assertEqual(self.listing(), self.before)

    def test_a_stashed_entry_whose_folder_is_gone_gets_it_made_for_the_account(self):
        self.stash()
        shutil.rmtree(self.apps / 'common/Game')
        self.restore()
        self.assertTrue(fake_whiteout(os.lstat(self.apps / 'common/Game/data/b.dat')))

    def test_conflicts_are_listed_for_status(self):
        self.assertEqual(store.conflicts_of(self.side), [])
        folder = store._root_folder(self.layout, 'conflicts', 'deck', 'layer', 'steamapps')
        os.close(folder)
        (self.side.conflicts / 'layer/steamapps/a').write_text('x')
        self.assertEqual(store.conflicts_of(self.side), ['layer/steamapps'])


class GivenLibraries(DiskWorld):
    """An account with no library on a disk is given one: Guest on the SD card."""

    def setUp(self):
        super().setUp()
        self.add()
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        self.commands = []
        self.mounted = set()
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.base / 'home/guest'),
                                     pw_name='guest')
        self.steam = self.base / 'home/guest/.local/share/Steam'
        (self.steam / 'steamapps').mkdir(parents=True)
        (self.steam / 'steamapps/libraryfolders.vdf').write_text(depot.vdf_dumps('libraryfolders', {
            '0': {'path': str(self.steam)}}))
        self.visible, self.real, (self.home, self.bind) = self.disk.provisioned('guest')

    def runner(self, command, **_options):
        self.commands.append(command)
        if command[:2] == ['mount', '--bind'] and '-o' in command:
            self.mounted.add(command[-1])
        elif command[0] == 'umount':
            self.mounted.discard(command[-1])
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def info(self):
        lines = ['22 1 0:21 / / rw - ext4 /dev/root rw', f'50 22 0:50 / {self.usb} rw - ext4 /dev/sdb1 rw']
        lines += [f'60 22 0:50 /x {point} rw - ext4 /dev/sdb1 rw' for point in sorted(self.mounted)]
        path = self.base / 'mountinfo-now'
        path.write_text('\n'.join(lines) + '\n')
        return path

    def listed(self):
        return [item['path'] for item in depot.read_vdf(self.steam / 'steamapps/libraryfolders.vdf')[1].values()
                if isinstance(item, dict)]

    def provision(self):
        disk = self.disk
        return disk, store.provision(disk, 'guest', self.entry, runner=self.runner, mountinfo=self.info(), proc=self.proc)

    def test_the_account_gets_a_folder_of_its_own_a_bind_to_its_drives_folder_and_a_list_entry(self):
        disk, done = self.provision()
        self.assertEqual(done, ['media', 'listed'])
        self.assertTrue((self.real / 'steamapps').is_dir())
        self.assertEqual(self.real, disk.root / 'accounts/guest/SteamLibrary')
        self.assertTrue(self.bind.is_dir())
        self.assertEqual(self.commands, [['mount', '--bind', '-o', 'nosuid,nodev', str(self.home), str(self.bind)]])
        self.assertEqual(self.listed(), [str(self.steam), str(self.visible)])
        # What the bind shows at its top is the account's folder, which holds SteamLibrary and not steamapps: Steam adds
        # a mounted filesystem with steamapps at its top as a drive by itself.
        self.assertEqual(sorted(os.listdir(self.home)), ['SteamLibrary'])

    def test_doing_it_again_changes_nothing(self):
        self.provision()
        self.commands.clear()
        _disk, done = self.provision()
        self.assertEqual(done, [])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.listed(), [str(self.steam), str(self.visible)])

    def test_the_bind_is_made_again_after_a_reboot_and_the_list_entry_is_kept(self):
        self.provision()
        self.mounted.clear()                     # /run is empty after a boot
        self.commands.clear()
        _disk, done = self.provision()
        self.assertEqual(done, ['media'])
        self.assertEqual(self.listed().count(str(self.visible)), 1)

    def test_it_waits_while_the_accounts_steam_runs_for_the_list_but_the_folders_are_made(self):
        (self.proc / '9').mkdir()
        (self.proc / '9/comm').write_text('steam\n')
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            self.provision()
        self.assertEqual(self.listed(), [str(self.steam)])

    def test_a_library_is_found_on_the_disk_and_a_given_one_is_known_by_its_two_paths(self):
        disk = self.disk
        self.assertIsNone(store.resolve_library(disk, 'guest', self.entry))
        self.provision()
        self.assertEqual(store.resolve_library(disk, 'guest', self.entry), (self.visible, self.real))
        # An account that has a library of its own there keeps it, and nothing is given.
        own = self.usb / 'SteamLibrary'
        (own / 'steamapps').mkdir(parents=True)
        (self.steam / 'steamapps/libraryfolders.vdf').write_text(depot.vdf_dumps('libraryfolders', {
            '0': {'path': str(self.steam)}, '1': {'path': str(own)}}))
        self.assertEqual(store.resolve_library(disk, 'guest', self.entry), (own, own))

    def test_the_overlay_of_a_given_library_is_mounted_where_the_account_sees_it_over_the_real_folder(self):
        disk = self.disk
        done = store.mount(disk, 'guest', runner=self.runner, mountinfo=self.info(), proc=self.proc)
        self.assertEqual(done, ['media', 'listed', 'real', 'library', 'prefixes'])
        side = disk.side('guest')
        self.assertEqual(side.library, self.visible)
        reach = [command for command in self.commands if command[:2] == ['mount', '--bind'] and str(side.reach) in command]
        self.assertEqual(reach[0][-2:], [str(self.real / 'steamapps'), str(side.reach)])      # the real folder, not the bind
        overlay = [command for command in self.commands if command[:3] == ['mount', '-t', 'overlay']][0]
        self.assertEqual(overlay[-1], str(self.visible / 'steamapps'))
        self.assertIn(f'upperdir={self.real / "steamapps"}', overlay[5])      # same mount as the work directory
        self.assertIn(f'workdir={disk.root / "sides/guest/overlay-work"}', overlay[5])
        self.assertTrue((disk.root / 'sides/guest/overlay-work').is_dir())

    def test_a_library_given_leaves_nothing_with_steamapps_at_a_mount_top(self):
        self.provision()
        for folder in (self.home, self.bind):
            self.assertFalse((folder / 'steamapps').exists(), folder)

    def test_leaving_drops_the_list_entry_and_names_a_library_that_holds_games(self):
        self.provision()
        report = {}
        with patch.object(store, 'account', lambda name: self.entry):
            store._retire_provision(self.disk, 'guest', report, self.proc)
        self.assertEqual(self.listed(), [str(self.steam)])
        self.assertEqual(report, {})
        store.change_library(self.entry, self.visible, True)
        (self.real / 'steamapps/appmanifest_5.acf').write_text('x')
        with patch.object(store, 'account', lambda name: self.entry):
            store._retire_provision(self.disk, 'guest', report, self.proc)
        self.assertEqual(report, {'kept_libraries': {'guest': str(self.real)}})
        self.assertTrue((self.real / 'steamapps/appmanifest_5.acf').exists())      # never deleted

    def test_forgetting_a_disk_takes_the_given_library_off_the_list(self):
        self.provision()
        self.write_mountinfo(usb=False)
        with patch.object(store, 'account', lambda name: self.entry):
            store.forget(self.primary, str(self.disk.root), mountinfo=self.mountinfo, proc=self.proc)
        self.assertEqual(self.listed(), [str(self.steam)])

    def test_a_disk_that_was_pulled_out_has_its_mounts_detached(self):
        disk = self.disk
        store.mount(disk, 'guest', runner=self.runner, mountinfo=self.info(), proc=self.proc)
        side = disk.side('guest')
        text = self.info().read_text() + (f'70 22 0:70 / {side.steamapps} rw - overlay dosplex-library rw,'
                                          f'lowerdir=a,upperdir=b,workdir={side.work}\n')
        info = self.base / 'mountinfo-pulled'
        info.write_text(text)
        self.commands.clear()
        commands = []

        def runner(command, **_options):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        report = store._slot_absent(self.primary, {'root': str(disk.root), 'mount': str(self.usb), 'reason': 'gone'},
                                    ['guest'], self.proc, runner, info)
        self.assertEqual(report, {'absent': 'gone'})
        self.assertIn(['umount', '-l', str(side.steamapps)], commands)
        self.assertIn(['umount', '-l', str(side.reach)], commands)
        self.assertIn(['umount', '-l', str(self.bind)], commands)

    def test_a_library_given_on_a_disk_is_not_taken_for_a_disk_of_its_own(self):
        # The bind of the account's folder is a mount point with a library below it; the disk's store is not made there.
        class Listing(StubTidy):
            pass
        tidy = Listing()
        tidy.libraries = [{'path': str(self.visible), 'dev': 3}]
        self.mounted.add(str(self.bind))
        info = self.info()
        text = info.read_text().replace(f'60 22 0:50 /x {self.bind}', f'60 22 0:61 /dosplex-library/disk/accounts/guest {self.bind}')
        info.write_text(text)
        summary = {'disks_refused': []}
        with patch.object(store, 'device_of', lambda path: 3 if str(path).startswith(str(self.bind)) else 1):
            store._find_disks(self.primary, 'guest', self.USERS, tidy, info, summary)
        self.assertEqual(summary['disks_refused'], [])
        self.assertEqual(len(store.read_disks(self.primary)), 1)


class ShareAcrossDisks(DiskWorld):
    def setUp(self):
        super().setUp()
        self.tidy = StubTidy()
        self.library = str(self.usb / 'SteamLibrary')
        self.tidy.libraries = [{'path': self.library, 'dev': 2}]
        self.disk_root = str(self.disk.root)
        self.tidy.listings = {self.disk_root: [
            {'appid': '20', 'name': 'On usb', 'source': 'library', 'library': self.library, 'reason': None}]}

    def share(self, apps, every=False):
        return store.share(self.primary, 'deck', apps, every, tidy=self.tidy, mountinfo=self.mountinfo)

    def test_all_finds_the_disk_gives_it_a_store_and_routes_each_game_to_its_disk(self):
        summary = self.share([], True)
        self.assertEqual(summary['disks_refused'], [])
        self.assertEqual(self.tidy.shared_in, [('10', str(self.primary.root)), ('12', str(self.primary.root)),
                                               ('20', self.disk_root)])
        by_app = {item['appid']: item for item in summary['shared']}
        self.assertEqual(by_app['20']['disk'], self.disk_root)
        self.assertNotIn('disk', by_app['10'])
        self.assertEqual(summary['refused'], [])
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_a_disk_that_cannot_have_a_store_is_named_and_the_rest_is_shared(self):
        self.write_mountinfo(kind='vfat')
        summary = self.share([], True)
        self.assertEqual([item['library'] for item in summary['disks_refused']], [self.library])
        self.assertIn('vfat', summary['disks_refused'][0]['reason'])
        self.assertEqual([appid for appid, _root in self.tidy.shared_in], ['10', '12'])
        self.assertEqual(store.read_disks(self.primary), [])

    def test_a_library_on_no_mount_point_that_matches_is_named(self):
        self.tidy.libraries = [{'path': self.library, 'dev': 3}]
        summary = self.share([], True)
        self.assertIn('no mount point', summary['disks_refused'][0]['reason'])

    def test_a_library_on_the_primarys_filesystem_needs_no_disk(self):
        self.tidy.libraries = [{'path': str(self.base / 'SteamLibrary'), 'dev': 1}]
        summary = self.share([], True)
        self.assertEqual((summary['disks_refused'], store.read_disks(self.primary)), ([], []))

    def test_a_named_game_that_the_primary_has_is_shared_without_looking_for_disks(self):
        summary = self.share(['10'])
        self.assertEqual(self.tidy.account_calls, 0)
        self.assertEqual([item['appid'] for item in summary['shared']], ['10'])
        self.assertEqual(store.read_disks(self.primary), [])

    def test_a_named_game_on_a_disk_without_a_store_makes_the_discovery_once_and_goes_there(self):
        summary = self.share(['10', '20', '99'])
        self.assertEqual(self.tidy.account_calls, 1)
        self.assertEqual(self.tidy.shared_in, [('10', str(self.primary.root)), ('20', self.disk_root)])
        self.assertEqual(summary['refused'], [{'appid': '99', 'reason': 'game 99 is not installed for this account'}])
        self.assertEqual(summary['shared'][1]['disk'], self.disk_root)

    def test_a_disk_that_has_its_store_already_is_not_added_again(self):
        self.add()
        before = os.stat(self.primary.disks).st_mtime_ns
        summary = self.share([], True)
        self.assertEqual(os.stat(self.primary.disks).st_mtime_ns, before)
        self.assertEqual([appid for appid, _root in self.tidy.shared_in], ['10', '12', '20'])
        self.assertEqual(summary['disks_refused'], [])

    def test_the_automatic_check_finds_the_disk_too_and_queues_each_game_in_its_store(self):
        report = store.share_new(self.primary, ['deck'], tidy=self.tidy, mountinfo=self.mountinfo)
        summary = report['users']['deck']
        # Game 10 waits for a slot already (the stub's pending list), so it is not read again.
        self.assertEqual(self.tidy.shared_in, [('12', str(self.primary.root)), ('20', self.disk_root)])
        self.assertEqual([item.get('disk') for item in summary['shared']], [None, self.disk_root])
        self.assertEqual(summary['disks_refused'], [])
        self.assertEqual(len(store.read_disks(self.primary)), 1)

    def test_the_automatic_check_names_a_disk_it_cannot_use_and_goes_on(self):
        self.write_mountinfo(kind='exfat')
        summary = store.share_new(self.primary, ['deck'], tidy=self.tidy, mountinfo=self.mountinfo)['users']['deck']
        self.assertIn('exfat', summary['disks_refused'][0]['reason'])
        self.assertEqual([appid for appid, _root in self.tidy.shared_in], ['12'])


class AutoTidy(StubTidy):
    """A listing the test sets, a list of games already queued, and a share that can refuse by app id."""
    refuse_reason = 'has a file that changed while it was read'

    def __init__(self):
        super().__init__()
        self.listing = {}
        self.queued = []

    def pending(self, layout):
        return [{'appid': appid, 'user': 'deck'} for appid in self.queued]

    def shareable(self, layout, user, users):
        return [dict(item) for item in self.listing.get(user, [])]

    def share(self, layout, user, appid, users, *, progress=None):
        self.calls.append(('share', user, appid))
        if appid in self.refuse:
            raise self.TidyError(f'game {appid} {self.refuse_reason}')
        return {'appid': appid, 'name': 'n', 'kind': 'new'}


class AutomaticSharing(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.layout = store.Layout(self.temp / 'library')
        self.layout.root.mkdir()
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck', 'guest']})
        self.tidy = AutoTidy()
        self.now = 1_800_000_000
        self.library = self.temp / 'lib'
        (self.library / 'steamapps').mkdir(parents=True)
        self.tidy.listing['deck'] = [self.game('10', 'Alpha'), self.game('20', 'Beta')]

    def game(self, appid, name, reason=None, source='library'):
        manifest = self.library / 'steamapps' / f'appmanifest_{appid}.acf'
        if not manifest.exists():
            manifest.write_text('"AppState"\n')
        return {'appid': appid, 'name': name, 'source': source, 'library': str(self.library), 'reason': reason}

    def memory(self):
        return json.loads((self.layout.root / 'auto.json').read_text())['refused']

    def run_new(self, names=None, now=None):
        return store.share_new(self.layout, names, tidy=self.tidy, now=self.now if now is None else now)

    def shared(self):
        return [call[2] for call in self.tidy.calls if call[0] == 'share']

    def test_new_games_are_shared_one_by_one(self):
        report = self.run_new(['deck'])
        self.assertEqual([item['appid'] for item in report['users']['deck']['shared']], ['10', '20'])
        self.assertEqual(report['users']['deck']['refused'], [])
        self.assertEqual(report['users']['deck']['skipped'], 0)
        self.assertEqual(list(report['users']), ['deck'])

    def test_every_account_of_the_config_is_checked_without_names(self):
        self.tidy.listing['guest'] = [self.game('30', 'Gamma')]
        self.assertEqual(set(self.run_new()['users']), {'deck', 'guest'})
        with self.assertRaisesRegex(store.StoreError, 'not an account'):
            self.run_new(['stranger'])

    def test_tools_listed_refusals_and_queued_games_are_left_out(self):
        self.tidy.listing['deck'] = [self.game('1', 'Proton 9.0'), self.game('2', 'Steam Linux Runtime 3.0'),
                                     self.game('3', 'Broken', reason='not fully installed'),
                                     self.game('4', 'Queued'), self.game('5', 'Fine')]
        self.tidy.queued = ['4']
        self.run_new(['deck'])
        self.assertEqual(self.shared(), ['5'])

    def test_a_game_queued_for_one_account_is_not_queued_again_for_the_other(self):
        self.tidy.listing['guest'] = [self.game('10', 'Alpha')]
        report = self.run_new()
        self.assertEqual(self.shared(), ['10', '20'])
        self.assertEqual(report['users']['guest']['shared'], [])

    def test_a_refusal_is_remembered_and_the_game_is_not_read_again(self):
        self.tidy.refuse = {'10'}
        first = self.run_new(['deck'])
        self.assertEqual(first['users']['deck']['refused'],
                         [{'appid': '10', 'name': 'Alpha', 'reason': 'game 10 ' + self.tidy.refuse_reason}])
        path = self.layout.root / 'auto.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        entry = self.memory()['deck/10']
        self.assertEqual(entry['manifest'][0], len('"AppState"\n'))
        self.assertEqual(entry['utc'], '2027-01-15T08:00:00Z')
        self.tidy.calls.clear()
        second = self.run_new(['deck'], now=self.now + 600)
        self.assertEqual(self.shared(), ['20'])
        self.assertEqual(second['users']['deck']['skipped'], 1)
        self.assertEqual(second['users']['deck']['refused'], [])

    def test_a_remembered_refusal_expires_after_a_day(self):
        self.tidy.listing['deck'] = [self.game('10', 'Alpha')]
        self.tidy.refuse = {'10'}
        self.run_new(['deck'])
        self.tidy.calls.clear()
        self.run_new(['deck'], now=self.now + 24 * 3600 - 1)
        self.assertEqual(self.shared(), [])
        self.run_new(['deck'], now=self.now + 24 * 3600 + 1)
        self.assertEqual(self.shared(), ['10'])

    def test_a_changed_manifest_tries_the_game_again(self):
        self.tidy.listing['deck'] = [self.game('10', 'Alpha')]
        self.tidy.refuse = {'10'}
        self.run_new(['deck'])
        self.tidy.calls.clear()
        (self.library / 'steamapps' / 'appmanifest_10.acf').write_text('"AppState"\n"more"\n')
        self.run_new(['deck'], now=self.now + 60)
        self.assertEqual(self.shared(), ['10'])

    def test_a_game_shared_later_is_forgotten_and_a_game_that_is_gone_is_pruned(self):
        self.tidy.refuse = {'10', '20'}
        self.run_new(['deck'])
        self.assertEqual(set(self.memory()), {'deck/10', 'deck/20'})
        self.tidy.refuse = set()
        self.tidy.listing['deck'] = [self.game('10', 'Alpha')]
        (self.library / 'steamapps' / 'appmanifest_10.acf').write_text('changed')
        self.run_new(['deck'], now=self.now + 60)
        self.assertEqual(self.memory(), {})

    def test_a_gone_game_is_pruned_even_when_nothing_else_changes(self):
        self.tidy.refuse = {'10', '20'}
        self.run_new(['deck'])
        self.tidy.listing['deck'] = [self.game('10', 'Alpha')]
        self.run_new(['deck'], now=self.now + 60)
        self.assertEqual(set(self.memory()), {'deck/10'})

    def test_other_accounts_entries_survive_a_run_for_one_account(self):
        self.tidy.listing['guest'] = [self.game('30', 'Gamma')]
        self.tidy.refuse = {'30'}
        self.run_new(['guest'])
        self.tidy.refuse = set()
        self.run_new(['deck'])
        self.assertIn('/'.join(('guest', '30')), self.memory())

    def test_only_a_tidy_refusal_is_remembered(self):
        def broken(*_args, **_kwargs):
            raise OSError('disk went away')
        self.tidy.share = broken
        report = self.run_new(['deck'])
        self.assertEqual(len(report['users']['deck']['refused']), 2)
        self.assertFalse((self.layout.root / 'auto.json').exists())

    def test_an_unreadable_or_malformed_memory_is_an_empty_one(self):
        path = self.layout.root / 'auto.json'
        good = {'manifest': [1, 2], 'reason': 'r', 'utc': '2027-01-15T08:00:00Z'}
        for text in ('{not json', '[]', '{"version": 2, "refused": {}}', '{"version": 1, "refused": []}',
                     json.dumps({'version': 1, 'refused': {'deck/10': dict(good, manifest=[1])}}),
                     json.dumps({'version': 1, 'refused': {'deck/10': dict(good, utc='yesterday')}}),
                     json.dumps({'version': 1, 'refused': {'deck/10': good, 'deck/11': 5}}),
                     'x' * (store.AUTO_LIMIT + 1)):
            path.write_text(text)
            self.assertEqual(store.read_auto(self.layout), {}, text[:40])
        path.unlink()
        path.symlink_to(self.temp / 'elsewhere')
        self.assertEqual(store.read_auto(self.layout), {})
        path.unlink()
        path.mkdir()
        self.assertEqual(store.read_auto(self.layout), {})

    def test_a_good_memory_is_read_back_and_leaves_no_temporary_file(self):
        good = {'manifest': [1, 2], 'reason': 'r', 'utc': '2027-01-15T08:00:00Z'}
        store.write_auto(self.layout, {'deck/10': good})
        self.assertEqual(store.read_auto(self.layout), {'deck/10': good})
        self.assertEqual([item.name for item in self.layout.root.iterdir() if item.name.startswith('.auto-')], [])

    def test_a_malformed_memory_does_not_stop_a_run(self):
        (self.layout.root / 'auto.json').write_text('garbage')
        self.assertEqual(len(self.run_new(['deck'])['users']['deck']['shared']), 2)

    def test_leaving_and_no_config_share_nothing(self):
        store.write_config(self.layout, {'version': 1, 'mode': 'leaving', 'users': ['deck', 'guest']})
        self.assertEqual(self.run_new(), {'users': {}})
        self.layout.config.unlink()
        self.assertEqual(self.run_new(), {'users': {}})
        self.assertEqual(self.shared(), [])


class OneShareAtATime(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.layout = store.Layout(Path(temp.name) / 'library')
        self.layout.root.mkdir()
        self.lock = self.layout.root / '.share-lock'

    def test_the_lock_is_exclusive_and_holds_the_process_id(self):
        with store.sharing(self.layout):
            self.assertEqual(self.lock.read_text().strip(), f'{os.getpid()} named')
            self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(store.StoreError, 'being checked for sharing already'):
                with store.sharing(self.layout):
                    pass
        with store.sharing(self.layout):
            pass

    def test_a_longer_old_process_id_is_cut_off(self):
        self.lock.write_text('9' * 40 + '\n')
        with store.sharing(self.layout, 'new'):
            self.assertEqual(self.lock.read_text().strip(), f'{os.getpid()} new')

    def test_only_the_background_check_gives_way_to_a_share_somebody_asked_for(self):
        # Seen on the Steam Machine: the first `library share` after `library on` was refused, the timer had fired.
        calls = []
        child = self.hold()                 # a check somebody asked for: it stays
        self.assertFalse(store.stop_sharing(self.layout, only='new', signaller=lambda *a: calls.append(a)))
        self.assertEqual(calls, [])
        child.kill()
        child.wait()
        background = self.hold('new')
        self.assertTrue(store.stop_sharing(self.layout, only='new', patience=5))
        self.assertIsNotNone(background.wait(timeout=10))

    def hold(self, kind='named'):
        """A child process that holds the lock and writes its id, in a process group of its own."""
        code = ('import importlib.util as u, os, time\n'
                's = u.spec_from_file_location("gs", %r); m = u.module_from_spec(s); s.loader.exec_module(m)\n'
                'os.setpgrp()\n'
                'with m.sharing(m.Layout(%r), %r):\n'
                '    print("held", flush=True); time.sleep(60)\n'
                % (str(ROOT / 'packaging/steamos/dosplex-game-store.py'), str(self.layout.root), kind))
        child = subprocess.Popen([os.sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def test_stop_sharing_returns_when_the_lock_is_free_or_missing(self):
        calls = []
        self.assertFalse(store.stop_sharing(self.layout, signaller=lambda *a: calls.append(a)))
        with store.sharing(self.layout):
            pass
        self.assertFalse(store.stop_sharing(self.layout, signaller=lambda *a: calls.append(a)))
        self.assertEqual(calls, [])

    def test_a_running_check_gets_sigterm_and_the_lock_is_freed(self):
        child = self.hold()
        seen = []

        def term(pid, number):
            seen.append((pid, number))
            os.kill(pid, number)
        self.assertTrue(store.stop_sharing(self.layout, patience=5, signaller=term))
        self.assertEqual(seen, [(child.pid, signal_module.SIGTERM)])
        child.wait(timeout=5)

    def test_a_check_that_ignores_sigterm_is_killed(self):
        child = self.hold()
        seen = []

        def stubborn(pid, number):
            seen.append(number)
            if number == signal_module.SIGKILL:
                os.kill(pid, number)
        self.assertTrue(store.stop_sharing(self.layout, patience=0.2, signaller=stubborn))
        self.assertEqual(seen, [signal_module.SIGTERM, signal_module.SIGKILL])
        child.wait(timeout=5)

    def test_a_check_that_cannot_be_stopped_is_an_error(self):
        self.hold()
        with patch.object(store.time, 'monotonic', side_effect=itertools.count(0, 100)), \
                patch.object(store.time, 'sleep'):
            with self.assertRaisesRegex(store.StoreError, 'could not be stopped'):
                store.stop_sharing(self.layout, patience=1, signaller=lambda *a: None)

    def test_the_default_signal_reaches_the_group(self):
        child = self.hold()
        self.assertTrue(store.stop_sharing(self.layout, patience=5))
        self.assertEqual(child.wait(timeout=5), -signal_module.SIGTERM)

    def test_an_unreadable_process_id_is_never_signalled(self):
        calls = []
        with store.sharing(self.layout):
            self.lock.write_text('nonsense')
            # The lock is held by this very process on another descriptor, so flock sees it as taken.
            with self.assertRaisesRegex(store.StoreError, 'could not be stopped'):
                store.stop_sharing(self.layout, signaller=lambda *a: calls.append(a))
        self.assertEqual(calls, [])

    def test_the_slot_stops_a_check_first_and_says_so(self):
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck']})
        with patch.object(store, 'account', lambda name: SimpleNamespace(pw_uid=1000)), \
                patch.object(store, 'steam_running', lambda uid, proc=None: False), \
                patch.object(store, 'unmount', lambda *a, **k: None), patch.object(store, 'mount', lambda *a, **k: None), \
                patch.object(store, 'register', lambda *a, **k: None), patch.object(store, 'prepare_sides', lambda *a, **k: None), \
                patch.object(store, 'restore_account', lambda *a, **k: []):
            with patch.object(store, 'stop_sharing', return_value=True) as stop:
                report = store.slot(self.layout, tidy=StubTidy())
            stop.assert_called_once_with(self.layout)
            self.assertTrue(report['stopped_sharing'])
            self.assertNotIn('stopped_sharing', store.slot(self.layout, tidy=StubTidy()))


class ShareNewCommandLine(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dosplex-store-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'library'
        self.root.mkdir()

    def run_main(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        fake = lambda layout, names, **_kwargs: {'users': {name: {} for name in names or ['deck']}}
        with patch.object(os, 'geteuid', return_value=0), patch.object(os, 'setpgrp') as group, \
                patch.object(store, 'share_new', side_effect=fake), \
                patch('sys.argv', ['store', 'share', '--root', str(self.root), *arguments]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = store.main()
        return code, out.getvalue(), err.getvalue(), group

    def test_new_prints_the_summary_and_takes_users(self):
        code, out, _err, group = self.run_main('--new', '--user', 'deck', '--user', 'guest')
        self.assertEqual((code, json.loads(out)), (0, {'users': {'deck': {}, 'guest': {}}}))
        group.assert_called_once_with()
        self.assertEqual(self.run_main('--new')[0], 0)

    def test_new_excludes_app_and_all(self):
        for extra in (('--app', '5'), ('--all',)):
            code, out, err, _group = self.run_main('--new', *extra)
            self.assertEqual((code, out), (1, ''))
            self.assertIn('share --new takes no --app and no --all', err)

    def test_every_share_form_runs_inside_the_lock(self):
        with store.sharing(store.Layout(self.root)):
            for arguments in (('--new',), ('--user', 'deck', '--all'), ('--user', 'deck', '--app', '5')):
                code, _out, err, _group = self.run_main(*arguments)
                self.assertEqual(code, 1)
                self.assertIn('being checked for sharing already', err)


class MountWhileSteamRuns(MountWorld):
    """A card that arrives while a Steam runs: the overlay may be mounted, the real folder may not be changed."""

    def steam_is_running(self):
        (self.proc / '77').mkdir(exist_ok=True)
        (self.proc / '77/comm').write_text('steam\n')

    def test_a_running_steam_refuses_the_plain_mount_when_manifests_wait_in_the_stash(self):
        self.hold(('100', 'Game'))
        (self.steam / 'steamapps/appmanifest_100.acf').write_text('mine')
        self.mount()
        self.unmount()
        self.steam_is_running()
        with self.assertRaisesRegex(store.StoreError, 'Steam is running'):
            self.mount()
        self.assertEqual(self.mounted, {})

    def test_the_overlay_is_mounted_over_a_running_steam_and_the_stash_is_left_for_the_next_slot(self):
        self.hold(('100', 'Game'))
        steamapps = self.steam / 'steamapps'
        (steamapps / 'appmanifest_100.acf').write_text('mine')
        self.mount()
        self.unmount()
        self.steam_is_running()
        done = store.mount(self.layout, 'tester', runner=self.runner, mountinfo=self.mountinfo, proc=self.proc,
                           while_running=True)
        self.assertEqual(done, ['manifests wait', 'real', 'library', 'prefixes'])
        self.assertEqual(store.overlay_mounts(self.side, self.mountinfo), [str(self.side.steamapps)])
        # The real folder was not changed: the stash still holds the manifest.
        self.assertFalse((steamapps / 'appmanifest_100.acf').exists())
        self.assertEqual((self.side.stash / 'steamapps/appmanifest_100.acf').read_text(), 'mine')
        # Once the Steam is closed the slot's mount puts it back, from an unmounted state.
        (self.proc / '77/comm').write_text('other\n')
        self.unmount(stash=False)
        self.mount()
        self.assertEqual((steamapps / 'appmanifest_100.acf').read_text(), 'mine')
        self.assertFalse(self.side.stash.exists())

    def test_with_no_stash_there_is_nothing_to_wait_for(self):
        self.steam_is_running()
        done = store.mount(self.layout, 'tester', runner=self.runner, mountinfo=self.mountinfo, proc=self.proc,
                           while_running=True)
        self.assertEqual(done, ['real', 'library', 'prefixes'])


class ReleaseOnDisk(MountWorld):
    """release and a crash in it, with the real mount, unmount and stash of the primary and recorded commands."""

    def setUp(self):
        super().setUp()
        self.layout.root.mkdir(parents=True, exist_ok=True)
        store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['tester']})
        self.hold(('100', 'Game'), ('101', 'Other'))
        self.steamapps = self.steam / 'steamapps'
        (self.steamapps / 'appmanifest_100.acf').write_text('one')
        (self.steamapps / 'appmanifest_101.acf').write_text('two')
        make_whiteout(self.steamapps / 'common/Game/x.dat')
        self.names = ['appmanifest_100.acf', 'appmanifest_101.acf', 'common/Game/x.dat']

    def release(self, **options):
        return store.release(self.layout, runner=self.runner, mountinfo=self.mountinfo, proc=self.proc, **options)

    def test_release_takes_every_overlay_off_and_stashes_what_stock_steam_must_not_find(self):
        self.mount()
        report = self.release()
        self.assertEqual(self.mounted, {})
        item = report['stores'][str(self.layout.root)]['tester']
        self.assertEqual(item['unmounted'], ['prefixes', 'library', 'real'])
        self.assertEqual(item['stashed'], self.names)
        self.assertEqual(sorted(os.listdir(self.steamapps)), ['common'])
        self.assertEqual(store.last_events(self.layout)['release']['report'], report)

    def test_release_twice_changes_nothing_and_the_next_mount_puts_everything_back(self):
        self.mount()
        self.release()
        again = self.release()
        self.assertEqual(again['stores'][str(self.layout.root)]['tester'], {'unmounted': [], 'stashed': []})
        self.mount()
        self.assertEqual((self.steamapps / 'appmanifest_100.acf').read_text(), 'one')
        self.assertTrue(fake_whiteout(os.lstat(self.steamapps / 'common/Game/x.dat')))
        self.assertFalse(self.side.stash.exists())

    def test_a_steam_that_runs_keeps_its_manifests_and_the_overlay_still_comes_off(self):
        self.mount()
        (self.proc / '77').mkdir()
        (self.proc / '77/comm').write_text('steam\n')
        item = self.release()['stores'][str(self.layout.root)]['tester']
        self.assertEqual(item['unmounted'], ['prefixes', 'library', 'real'])
        self.assertIn('Steam is running', item['stash_waits'])
        self.assertTrue((self.steamapps / 'appmanifest_100.acf').exists())      # the next slot or arrival does the stash
        self.assertEqual(self.mounted, {})

    def test_a_steam_that_ends_within_the_wait_is_not_in_the_way(self):
        self.mount()
        (self.proc / '77').mkdir()
        (self.proc / '77/comm').write_text('steam\n')
        sleeps = []

        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) == 3:
                (self.proc / '77/comm').write_text('other\n')
        item = self.release(wait=30, sleep=sleep, clock=itertools.count().__next__)['stores'][str(self.layout.root)]['tester']
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(item['stashed'], self.names)

    def test_an_overlay_that_is_in_use_stays_and_nothing_is_stashed_under_it(self):
        self.mount()
        busy = self.runner

        def runner(command, **options):
            if command[0] == 'umount' and command[-1] == str(self.side.steamapps):
                return SimpleNamespace(returncode=32, stdout='', stderr='target is busy')
            return busy(command, **options)
        report = store.release(self.layout, runner=runner, mountinfo=self.mountinfo, proc=self.proc)
        item = report['stores'][str(self.layout.root)]['tester']
        self.assertIn('target is busy', item['error'])
        self.assertNotIn('stashed', item)
        self.assertTrue((self.steamapps / 'appmanifest_100.acf').exists())

    def test_dying_between_the_unmount_and_the_stash_or_in_the_middle_of_it_loses_nothing(self):
        class Dead(BaseException):
            pass
        real_rename = os.rename
        for number in (0, 1, 2):
            with self.subTest(dies_at=number):
                self.mount()
                count = [0]

                def rename(*args, **kwargs):
                    if count[0] == number:
                        raise Dead()
                    count[0] += 1
                    return real_rename(*args, **kwargs)
                with patch.object(store.os, 'rename', rename), self.assertRaises(Dead):
                    self.release()
                # The machine went down here. At the next boot or arrival the mount puts every entry back, once.
                self.mounted.clear()
                self.write_mountinfo()
                self.mount()
                for name in ('appmanifest_100.acf', 'appmanifest_101.acf'):
                    self.assertTrue((self.steamapps / name).exists(), name)
                self.assertTrue(fake_whiteout(os.lstat(self.steamapps / 'common/Game/x.dat')))
                self.assertFalse(self.side.stash.exists())
                self.release()
                self.mounted.clear()
                self.write_mountinfo()
                self.mount()


class Cards(DiskWorld):
    """release, arrive and eject for a store on a second disk, with mount, unmount and the stash played."""

    def setUp(self):
        super().setUp()
        self.add()
        self.calls = []
        self.running = set()
        self.errors = {}
        self.stash_errors = {}
        self.proc = self.base / 'proc'
        for name, function in (('account', self.account), ('steam_running', self.steam_running), ('unmount', self.unmount),
                               ('mount', self.mount), ('stash_account', self.stash)):
            patcher = patch.object(store, name, function)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.commands = []

    def label(self, layout):
        return 'primary' if layout.primary is None else 'disk'

    def account(self, name):
        return SimpleNamespace(pw_uid={'deck': 1000, 'guest': 1001}[name], pw_gid=1000, pw_dir='/nonexistent')

    def steam_running(self, uid, proc=None):
        return uid in self.running

    def unmount(self, layout, name, **options):
        self.calls.append(('unmount', name, self.label(layout), options.get('layer'), options.get('stash')))
        if (name, self.label(layout)) in self.errors:
            raise store.StoreError(self.errors[(name, self.label(layout))])
        return ['library']

    def mount(self, layout, name, **options):
        self.calls.append(('mount', name, self.label(layout), options.get('while_running')))
        if (name, self.label(layout)) in self.errors:
            raise store.StoreError(self.errors[(name, self.label(layout))])
        return ['library']

    def stash(self, layout, name, **_options):
        self.calls.append(('stash', name, self.label(layout)))
        if name in self.stash_errors:
            raise store.StoreError(self.stash_errors[name])
        return [f'appmanifest_{name}.acf']

    def runner(self, command, **_options):
        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def write_card(self, card=True):
        """The mount table with the card at the usb folder; when it is gone the node of its device is gone too."""
        self.write_mountinfo(usb=card)

    def not_gone(self, _source):
        return False

    # --- shutdown ------------------------------------------------------------------------------------------
    def test_release_goes_through_every_store_disks_first_and_stashes_after_each_unmount(self):
        report = store.release(self.primary, mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(self.calls, [
            ('unmount', 'deck', 'disk', True, False), ('stash', 'deck', 'disk'),
            ('unmount', 'guest', 'disk', True, False), ('stash', 'guest', 'disk'),
            ('unmount', 'deck', 'primary', True, False), ('stash', 'deck', 'primary'),
            ('unmount', 'guest', 'primary', True, False), ('stash', 'guest', 'primary')])
        self.assertEqual(list(report['stores']), [str(self.disk.root), str(self.primary.root)])
        self.assertEqual(report['stores'][str(self.disk.root)]['guest']['stashed'], ['appmanifest_guest.acf'])
        self.assertEqual(store.last_events(self.primary)['release']['report'], report)
        json.dumps(report)

    def test_release_with_no_config_does_nothing(self):
        self.primary.config.unlink()
        self.assertEqual(store.release(self.primary, mountinfo=self.mountinfo, runner=self.runner), {'config': None})
        self.assertEqual(self.calls, [])

    def test_a_failing_account_does_not_stop_the_others_and_is_named(self):
        self.errors[('deck', 'disk')] = 'target is busy'
        self.stash_errors['guest'] = "guest's Steam is running; its manifests can only be moved while it is closed"
        report = store.release(self.primary, mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        disk = report['stores'][str(self.disk.root)]
        self.assertEqual(disk['deck'], {'error': 'target is busy'})
        self.assertIn('Steam is running', disk['guest']['stash_waits'])
        self.assertIn(('unmount', 'deck', 'primary', True, False), self.calls)
        self.assertNotIn(('stash', 'deck', 'disk'), self.calls)

    def test_a_card_that_was_pulled_has_what_is_left_of_it_detached_at_shutdown(self):
        self.write_card(card=False)
        report = store.release(self.primary, mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(list(report['stores']), [str(self.primary.root)])
        self.assertEqual(report['absent'], {str(self.disk.root): {'absent': f'{self.usb} is not mounted'}})

    def test_a_broken_disk_list_still_releases_the_primary_and_is_said(self):
        self.primary.disks.write_text('x{')
        self.primary.disks.chmod(0o644)
        report = store.release(self.primary, mountinfo=self.mountinfo, runner=self.runner)
        self.assertIn('list of disks is unusable', report['disks_error'])
        self.assertEqual(list(report['stores']), [str(self.primary.root)])

    # --- arrival -------------------------------------------------------------------------------------------
    def test_an_arrival_mounts_every_account_on_the_card_with_a_running_steam_allowed_and_leaves_the_primary_alone(self):
        self.running.add(1000)
        report = store.arrive(self.primary, device='sdb1', mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(self.calls, [('mount', 'deck', 'disk', True), ('mount', 'guest', 'disk', True)])
        disk = report['disks'][str(self.disk.root)]
        self.assertEqual(disk['deck'], {'done': ['library'], 'steam_running': True})
        self.assertEqual(disk['guest'], {'done': ['library']})
        self.assertEqual(store.last_events(self.primary)['arrive']['report'], report)

    def test_a_card_that_cannot_be_mounted_gets_its_manifests_stashed_when_that_is_safe(self):
        self.errors[('deck', 'disk')] = 'wrong fs type'
        self.errors[('guest', 'disk')] = 'wrong fs type'
        self.running.add(1001)
        self.stash_errors['guest'] = "guest's Steam is running; its manifests can only be moved while it is closed"
        disk = store.arrive(self.primary, mountinfo=self.mountinfo, runner=self.runner,
                            gone=self.not_gone)['disks'][str(self.disk.root)]
        self.assertEqual(disk['deck'], {'error': 'wrong fs type', 'stashed': ['appmanifest_deck.acf']})
        self.assertEqual(disk['guest']['error'], 'wrong fs type')
        self.assertIn('Steam is running', disk['guest']['stash_waits'])

    def test_a_second_arrival_does_the_same_again_and_changes_nothing_of_its_own(self):
        first = store.arrive(self.primary, mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        second = store.arrive(self.primary, mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(first['disks'], second['disks'])

    def test_a_device_that_holds_a_filesystem_is_waited_for_until_it_is_mounted(self):
        self.write_card(card=False)
        sleeps = []

        def sleep(_seconds):
            sleeps.append(1)
            if len(sleeps) == 2:
                self.write_card()
        report = store.arrive(self.primary, device='sdb1', wait=60, mountinfo=self.mountinfo, runner=self.runner,
                              sleep=sleep, clock=itertools.count().__next__, gone=self.not_gone, probe=lambda device: 'ext4')
        self.assertEqual(len(sleeps), 2)
        self.assertNotIn('not_mounted', report)
        self.assertIn(('mount', 'deck', 'disk', True), self.calls)

    def test_a_device_that_never_gets_mounted_is_given_up_after_the_wait(self):
        self.write_card(card=False)
        report = store.arrive(self.primary, device='sdb1', wait=5, mountinfo=self.mountinfo, runner=self.runner,
                              sleep=lambda _s: None, clock=itertools.count().__next__, gone=self.not_gone,
                              probe=lambda device: 'ext4')
        self.assertEqual(report['not_mounted'], '/dev/sdb1 was not mounted after 5 seconds')
        self.assertEqual(self.calls, [])

    def test_a_device_without_a_filesystem_of_ours_is_not_waited_for(self):
        self.write_card(card=False)
        slept = []
        for found in (None, 'swap', 'vfat'):
            store.arrive(self.primary, device='sda2', wait=60, mountinfo=self.mountinfo, runner=self.runner,
                         sleep=slept.append, gone=self.not_gone, probe=lambda device, found=found: found)
        self.assertEqual(slept, [])

    def test_a_device_name_that_is_not_one_is_refused(self):
        for name in ('../sda', 'a b', '', 'x' * 40, 'sda1;id'):
            with self.assertRaises(store.StoreError):
                store.arrive(self.primary, device=name, mountinfo=self.mountinfo, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_a_pulled_card_whose_mount_is_still_in_the_table_is_detached_not_mounted_into(self):
        # The card's own mount is stale: its device node is gone. DosPlex's overlay and binds on it are detached lazily.
        side = self.disk.side('deck', self.usb / 'SteamLibrary', self.usb / 'SteamLibrary')
        lines = self.mountinfo.read_text().splitlines()
        lines.append(f'60 50 0:60 / {self.usb}/SteamLibrary/steamapps rw - overlay dosplex-library rw,workdir={side.work}')
        lines.append(f'61 22 0:50 / {side.reach} rw - ext4 /dev/sdb1 rw')
        self.mountinfo.write_text('\n'.join(lines) + '\n')
        report = store.arrive(self.primary, device='sdb1', mountinfo=self.mountinfo, runner=self.runner,
                              gone=lambda source: source == '/dev/sdb1')
        self.assertEqual(self.calls, [])
        lazy = [command[-1] for command in self.commands if command[:2] == ['umount', '-l']]
        self.assertEqual(lazy, [f'{self.usb}/SteamLibrary/steamapps', str(side.reach)])
        self.assertIn('is gone', report['disks'][str(self.disk.root)]['absent'])

    def test_a_card_that_comes_back_after_being_pulled_is_mounted_again(self):
        self.write_card(card=False)
        store.arrive(self.primary, device='sdb1', mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(self.calls, [])
        self.write_card()
        store.arrive(self.primary, device='sdb1', mountinfo=self.mountinfo, runner=self.runner, gone=self.not_gone)
        self.assertEqual(self.calls, [('mount', 'deck', 'disk', True), ('mount', 'guest', 'disk', True)])

    def test_with_no_config_an_arrival_does_nothing(self):
        self.primary.config.unlink()
        self.assertEqual(store.arrive(self.primary, mountinfo=self.mountinfo, runner=self.runner), {'config': None})

    def test_the_cycle_lock_lets_one_of_slot_release_arrive_and_eject_work_at_a_time(self):
        import fcntl
        with store.cycling(self.primary):
            descriptor = os.open(self.primary.root / store.CYCLE_LOCK, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        descriptor = os.open(self.primary.root / store.CYCLE_LOCK, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    # --- eject ---------------------------------------------------------------------------------------------
    def eject(self, **options):
        return store.eject(self.primary, self.usb, mountinfo=self.mountinfo, runner=self.runner, **options)

    def test_an_eject_releases_the_card_and_asks_udisks_to_unmount_its_device(self):
        report = self.eject()
        self.assertEqual((report['ejected'], report['mount'], report['device']), (True, str(self.usb), '/dev/sdb1'))
        self.assertEqual([call[:3] for call in self.calls if call[0] == 'unmount'], [('unmount', 'deck', 'disk'),
                                                                                      ('unmount', 'guest', 'disk')])
        self.assertEqual(self.commands, [['udisksctl', 'unmount', '--no-user-interaction', '--block-device', '/dev/sdb1']])
        self.assertEqual(store.last_events(self.primary)['release']['report']['ejected'], str(self.usb))

    def test_a_disk_that_is_not_listed_is_refused_and_nothing_is_touched(self):
        for place in (self.base / 'elsewhere', '/', 'relative', f'{self.usb}/../usb'):
            with self.assertRaises(store.StoreError):
                store.eject(self.primary, place, mountinfo=self.mountinfo, runner=self.runner)
        self.assertEqual((self.calls, self.commands), ([], []))

    def test_a_disk_that_is_not_mounted_is_already_ejected(self):
        self.write_card(card=False)
        self.assertEqual(self.eject(), {'ejected': True, 'already': True, 'mount': str(self.usb)})
        self.assertEqual((self.calls, self.commands), ([], []))

    def test_something_of_the_card_in_use_stops_the_eject_and_mounts_what_was_released_again(self):
        self.errors[('guest', 'disk')] = 'target is busy'
        with self.assertRaisesRegex(store.StoreError, 'was not ejected.*guest: target is busy'):
            self.eject()
        self.assertEqual(self.commands, [])                                        # udisks was never asked
        self.assertEqual([call for call in self.calls if call[0] == 'mount'], [('mount', 'deck', 'disk', True)])

    def test_udisks_refusing_puts_everything_back(self):
        def runner(command, **_options):
            return SimpleNamespace(returncode=1, stdout='', stderr='Error unmounting: device is busy')
        with self.assertRaisesRegex(store.StoreError, 'was not ejected.*device is busy'):
            store.eject(self.primary, self.usb, mountinfo=self.mountinfo, runner=runner)
        self.assertEqual([call for call in self.calls if call[0] == 'mount'],
                         [('mount', 'deck', 'disk', True), ('mount', 'guest', 'disk', True)])

    def test_without_udisksctl_a_plain_unmount_is_used(self):
        def runner(command, **_options):
            self.commands.append(command)
            if command[0] == 'udisksctl':
                raise FileNotFoundError('udisksctl')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        store.eject(self.primary, self.usb, mountinfo=self.mountinfo, runner=runner)
        self.assertEqual(self.commands[-1], ['umount', str(self.usb)])

    def test_another_mount_of_the_same_device_that_the_release_did_not_take_stops_the_eject(self):
        with self.mountinfo.open('a') as stream:
            stream.write('70 22 0:50 / /run/media/guest/card rw - ext4 /dev/sdb1 rw\n')
        with self.assertRaisesRegex(store.StoreError, 'still mounted elsewhere: /run/media/guest/card'):
            self.eject()
        self.assertEqual(self.commands, [])

    def test_a_running_steam_does_not_stop_an_eject_but_its_manifests_stay_for_the_next_slot(self):
        self.running.add(1000)
        self.stash_errors['deck'] = "deck's Steam is running; its manifests can only be moved while it is closed"
        report = self.eject()
        self.assertIn('Steam is running', report['released']['deck']['stash_waits'])
        self.assertEqual(len(self.commands), 1)

    def test_an_eject_while_the_library_is_off_is_refused(self):
        self.primary.config.unlink()
        with self.assertRaisesRegex(store.StoreError, 'not on'):
            self.eject()

    def test_the_events_file_is_read_defensively(self):
        self.assertEqual(store.last_events(self.primary), {})
        (self.primary.root / store.EVENTS_NAME).write_text('{"release": {"utc": 1, "report": []}, "arrive": 4}')
        self.assertEqual(store.last_events(self.primary), {})
        (self.primary.root / store.EVENTS_NAME).write_text('nonsense')
        self.assertEqual(store.last_events(self.primary), {})
        store.keep_event(self.primary, 'arrive', {'x': 1}, now=0)
        store.keep_event(self.primary, 'release', {'y': 2}, now=60)
        self.assertEqual(store.last_events(self.primary), {'arrive': {'utc': '1970-01-01T00:00:00Z', 'report': {'x': 1}},
                                                           'release': {'utc': '1970-01-01T00:01:00Z', 'report': {'y': 2}}})

    def test_the_slot_takes_the_cycle_lock_too_so_an_arrival_waits_for_it(self):
        seen = []
        original = store.cycling

        class Spy:
            def __enter__(inner):
                seen.append('lock')
                return original(self.primary).__enter__()

            def __exit__(inner, *exc):
                seen.append('unlock')
        with patch.object(store, 'cycling', lambda primary: Spy()), patch.object(store, '_slot', lambda primary, **o: {'x': 1}):
            self.assertEqual(store.slot(self.primary), {'x': 1})
        self.assertEqual(seen, ['lock', 'unlock'])

    def test_status_names_the_stashed_manifests_and_keeps_the_last_events(self):
        store.keep_event(self.primary, 'arrive', {'x': 1})
        with patch.object(store, 'account', lambda name: self.entry):
            report = store.status(self.primary, [], tidy=StubTidy(), mountinfo=self.mountinfo)
        self.assertIn('arrive', report['events'])
        self.assertEqual(report['disks'][0]['sides']['deck'].get('stashed'), [])


class CommandLine(unittest.TestCase):
    def test_it_needs_root_and_says_so(self):
        if os.geteuid() == 0:
            self.skipTest('running as root')
        result = subprocess.run([os.sys.executable, str(ROOT / 'packaging/steamos/dosplex-game-store.py'), 'status'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), 'dosplex-game-store: must run as root')

    def test_every_new_command_needs_root_and_an_unknown_one_is_refused_by_the_parser(self):
        if os.geteuid() == 0:
            self.skipTest('running as root')
        script = str(ROOT / 'packaging/steamos/dosplex-game-store.py')
        for command in ('on', 'off', 'slot', 'share', 'release', 'arrive', 'eject'):
            result = subprocess.run([os.sys.executable, script, command, '--user', 'x'], capture_output=True, text=True, timeout=30)
            self.assertEqual((result.returncode, result.stderr.strip()), (1, 'dosplex-game-store: must run as root'), command)
        result = subprocess.run([os.sys.executable, script, 'bogus'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
