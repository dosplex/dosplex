#!/usr/bin/env python3
# dosplex-test: tier=integration needs=userns,ordinary-user timeout=180 skips-ok="an unprivileged overlay mount is not allowed on this kernel or filesystem"
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The shared game store with real mounts, in a user namespace (ADR-023, T-606 step 4).

game-store-test.py plays the mounts with recorded commands. This runs the real code against the real kernel: the test
starts itself inside `unshare --user --map-root-user --mount`, where it may mount, and mounts the store into an
account's own library with a real overlay on the library's own steamapps folder, over its own upper directory.

What a user namespace cannot do is replaced, nothing else: the overlay gets `userxattr` and no `redirect_dir=off` (an
unprivileged overlay refuses it), the idmapped view is a plain bind (idmapped mounts need the real root), and the steps
that run as the account run directly (the namespace cannot drop to another user). It shows, on a real filesystem:

  - the real folder bound before the overlay covers it shows only the account's own files, and the overlay is found in
    the kernel's mount table by its work directory;
  - a delete through the overlay leaves a real whiteout (a character device 0/0) in the real folder;
  - an unmount moves the manifests of stored games and those whiteouts to the stash by rename, the next mount puts them
    back (the same inodes), and the deletion still holds;
  - the earlier layout's private layer, with a real whiteout, moves into the library by rename and nothing is overwritten;
  - leaving gives the game back whole and leaves no whiteout in the library.

Skipped, and said so, where the kernel does not let a user namespace mount an overlay.
"""
import os
from pathlib import Path
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem.replace('-', '_'), ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outer():
    """Run the inner tests in a user namespace; report what they said."""
    if '--inner' in sys.argv:
        return False
    command = ['unshare', '--user', '--map-root-user', '--mount', sys.executable, str(HERE), '--inner'] + sys.argv[1:]
    done = subprocess.run(command, text=True, capture_output=True, timeout=170)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    sys.exit(done.returncode)


class Real(unittest.TestCase):
    def setUp(self):
        self.tidy = load('packaging/steamos/dosplex-game-store-tidy.py')
        self.store = self.tidy.store
        self.depot = self.tidy.depot
        temp = tempfile.TemporaryDirectory(prefix='dosplex-overlay-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.layout = self.store.Layout(self.base / 'library', view_root=self.base / 'run')
        self.steam = self.base / 'home/deck/.local/share/Steam'
        self.apps = self.steam / 'steamapps'
        self.apps.mkdir(parents=True)
        self.layout.libraries['deck'] = (self.steam, self.steam)
        self.entry = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.base / 'home/deck'),
                                     pw_name='deck')
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        self.commands = []
        for module in (self.store, self.tidy.store):
            for name, value in (('account', lambda name: self.entry), ('as_user', lambda entry, work: work())):
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        # A real Steam of whoever runs the tests must not count (the account here is the tester's own): a call
        # that names no process table gets this test's own, which is empty unless a test fills it.
        running = self.store.steam_running
        patcher = patch.object(self.store, 'steam_running',
                               lambda uid, proc=Path('/proc'): running(uid, self.proc if proc == Path('/proc') else proc))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.leave_mounts)
        # The store holds game 100: a manifest and two files.
        store = self.layout.store / 'steamapps'
        (store / 'common/Game/data').mkdir(parents=True)
        (store / 'appmanifest_100.acf').write_text(self.depot.vdf_dumps('AppState', {
            'appid': '100', 'name': 'Game', 'StateFlags': '4', 'installdir': 'Game', 'buildid': '7'}))
        (store / 'common/Game/data/x.dat').write_text('x')
        (store / 'common/Game/data/y.dat').write_text('y')
        (self.layout.root / 'apps').mkdir()
        (self.layout.root / 'apps/100.json').write_text(json.dumps({
            'version': 1, 'appid': '100', 'name': 'Game', 'shared_by': 'deck', 'installdir': 'Game', 'buildid': '7',
            'depots': {}, 'files': ['data/x.dat', 'data/y.dat']}))
        (self.apps / 'common/Game').mkdir(parents=True)
        (self.apps / 'common/Game/save.dat').write_text('my progress')
        self.side = self.layout.side('deck')

    def leave_mounts(self):
        for path in (self.side.steamapps, self.side.reach, self.side.view):
            subprocess.run(['umount', '-l', str(path)], capture_output=True)

    def runner(self, command, **options):
        """The real commands, as a user namespace takes them."""
        command = list(command)
        if command[:3] == ['mount', '-t', 'overlay']:
            at = command.index('-o') + 1
            kept = [item for item in command[at].split(',') if item != 'redirect_dir=off' and item != 'nosuid'
                    and item != 'nodev']
            command[at] = ','.join(kept + ['userxattr'])
        elif command[:2] == ['mount', '--bind'] and '-o' in command:
            at = command.index('-o')
            if 'X-mount.idmap' in command[at + 1]:
                del command[at:at + 2]
        self.commands.append(command)
        return subprocess.run(command, **options)

    def mount(self):
        try:
            return self.store.mount(self.layout, 'deck', runner=self.runner, proc=self.proc)
        except self.store.StoreError as error:
            # Only what `mount` itself says when the kernel refuses. Matching the bare word "permission" also matched
            # the store's own refusals, and a change that broke every mount here showed up as eight skips.
            if 'wrong fs type' in str(error) or 'Operation not permitted' in str(error) or \
                    'permission denied' in str(error).lower():
                self.skipTest(f'this kernel does not let a user namespace mount an overlay: {error}')
            raise

    def unmount(self, **options):
        return self.store.unmount(self.layout, 'deck', runner=self.runner, proc=self.proc, **options)

    def kinds(self, folder, *names):
        return {name: os.lstat(folder / name) for name in names if os.path.lexists(folder / name)}

    def test_the_store_is_merged_into_the_library_and_the_real_folder_stays_the_accounts(self):
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        side = self.side
        # The kernel's own table names the overlay by its work directory; the bind shows the account's files only.
        self.assertEqual(self.store.overlay_mounts(side), [str(side.steamapps)])
        # compatdata: the account's own Proton prefixes, made if missing and bound over the overlay's.
        self.assertEqual(sorted(os.listdir(side.steamapps)), ['appmanifest_100.acf', 'common', 'compatdata'])
        self.assertEqual(sorted(os.listdir(side.reach)), ['common', 'compatdata'])
        self.assertEqual(sorted(os.listdir(side.steamapps / 'common/Game')), ['data', 'save.dat'])
        self.assertEqual(sorted(os.listdir(side.reach / 'common/Game')), ['save.dat'])
        # A write lands in the real folder; a delete of the store's file leaves a real whiteout there.
        (side.steamapps / 'appmanifest_200.acf').write_text('new')
        os.unlink(side.steamapps / 'common/Game/data/y.dat')
        os.unlink(side.steamapps / 'appmanifest_100.acf')
        self.assertEqual((self.apps / 'appmanifest_200.acf').read_text(), 'new')
        for name in ('common/Game/data/y.dat', 'appmanifest_100.acf'):
            info = os.lstat(side.reach / name)
            self.assertTrue(stat.S_ISCHR(info.st_mode) and info.st_rdev == 0, name)
            self.assertTrue(self.store.is_whiteout(info))
        self.assertFalse(os.path.lexists(side.steamapps / 'appmanifest_100.acf'))
        self.assertEqual(self.mount(), [])            # a second mount does nothing
        # Nothing at the top of what is mounted looks like a library root to Steam.
        self.assertFalse(os.path.lexists(side.steamapps / 'steamapps'))

    def test_unmount_stashes_the_manifests_and_whiteouts_of_stored_games_and_the_next_mount_restores_them(self):
        self.mount()
        side = self.side
        os.unlink(side.steamapps / 'common/Game/data/y.dat')
        os.unlink(side.steamapps / 'appmanifest_100.acf')
        (side.steamapps / 'appmanifest_200.acf').write_text('not stored')
        (self.apps / 'appmanifest_100.acf').write_text('an update that Steam wrote')       # a manifest over the whiteout
        before = self.kinds(self.apps, 'common/Game/data/y.dat', 'appmanifest_200.acf', 'appmanifest_100.acf')
        self.assertEqual(self.unmount(), ['prefixes', 'library', 'real'])
        self.assertEqual(self.store.overlay_mounts(side), [])
        # The library stock Steam sees: no manifest and no whiteout of a stored game; the other one stays.
        self.assertEqual(sorted(os.listdir(self.apps)), ['appmanifest_200.acf', 'common', 'compatdata'])
        self.assertFalse(os.path.lexists(self.apps / 'common/Game/data/y.dat'))
        self.assertEqual(os.stat(side.stash / 'steamapps/appmanifest_100.acf').st_ino, before['appmanifest_100.acf'].st_ino)
        self.assertTrue(stat.S_ISCHR(os.lstat(side.stash / 'steamapps/common/Game/data/y.dat').st_mode))
        self.assertEqual(self.store.stash_state(side), {'journal': 'stashed', 'entries': 2})
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        self.assertFalse(side.stash.exists())
        after = self.kinds(self.apps, 'common/Game/data/y.dat', 'appmanifest_200.acf', 'appmanifest_100.acf')
        self.assertEqual({name: info.st_ino for name, info in after.items()},
                         {name: info.st_ino for name, info in before.items()})
        self.assertFalse(os.path.lexists(side.steamapps / 'common/Game/data/y.dat'))         # the deletion still holds
        self.assertEqual((side.steamapps / 'appmanifest_100.acf').read_text(), 'an update that Steam wrote')

    def test_the_earlier_layers_content_moves_into_the_library_and_nothing_is_overwritten(self):
        old = self.side.retired.upper / 'steamapps'
        (old / 'common/Game').mkdir(parents=True)
        (old / 'common/Game/save.dat').write_text('an older save')                            # exists in the library
        os.utime(old / 'common/Game/save.dat', (1_000_000_000, 1_000_000_000))           # and is the older of the two
        (old / 'common/Game/settings.ini').write_text('settings')
        os.mknod(old / 'common/Game/gone.dat', stat.S_IFCHR | 0o600, os.makedev(0, 0))        # a real whiteout
        (old / 'appmanifest_100.acf').write_text('updated by the old layer')
        inode = os.stat(old / 'common/Game/settings.ini').st_ino
        with self.assertRaisesRegex(self.store.StoreError, 'earlier layout'):
            self.mount()
        report = self.tidy.migrate(self.layout, ['deck'], proc=self.proc)
        self.assertEqual(report['deck']['conflicts'], ['layer/steamapps/common/Game/save.dat'])
        self.assertEqual((self.apps / 'common/Game/save.dat').read_text(), 'my progress')
        self.assertEqual((self.side.conflicts / 'layer/steamapps/common/Game/save.dat').read_text(), 'an older save')
        self.assertEqual(os.stat(self.apps / 'common/Game/settings.ini').st_ino, inode)
        info = os.lstat(self.apps / 'common/Game/gone.dat')
        self.assertTrue(stat.S_ISCHR(info.st_mode) and info.st_rdev == 0)                      # still a whiteout
        self.assertFalse(os.path.lexists(self.side.retired.upper))
        self.assertEqual(self.tidy.migrate(self.layout, ['deck'], proc=self.proc), {})
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        self.assertFalse(os.path.lexists(self.side.steamapps / 'common/Game/gone.dat'))        # hides nothing here: no such file below

    def test_leaving_gives_the_game_back_whole_and_leaves_no_whiteout_and_no_stash(self):
        self.mount()
        side = self.side
        os.unlink(side.steamapps / 'common/Game/data/y.dat')                        # the account deleted a file
        (side.steamapps / 'common/Game/data/own.cfg').write_text('settings')
        self.unmount()                                                              # stashes the whiteout and the manifests
        self.assertTrue(side.stash.exists())
        result = self.tidy.restore_all(self.layout, ['deck'])
        self.assertEqual((result['restored'], result['kept'], result['removed']), (['100'], [], []))
        game = self.apps / 'common/Game'
        self.assertEqual((game / 'data/x.dat').read_text(), 'x')
        self.assertFalse(os.path.lexists(game / 'data/y.dat'))                       # what the account deleted stays deleted
        self.assertEqual((game / 'data/own.cfg').read_text(), 'settings')
        self.assertEqual((game / 'save.dat').read_text(), 'my progress')
        self.assertTrue((self.apps / 'appmanifest_100.acf').exists())
        self.assertFalse(side.stash.exists())
        for top, folders, names in os.walk(self.apps):
            for name in names + folders:
                info = os.lstat(os.path.join(top, name))
                self.assertFalse(stat.S_ISCHR(info.st_mode), os.path.join(top, name))
        self.assertEqual(os.listdir(self.layout.root / 'store/steamapps/common'), [])

    def configure(self):
        self.store.write_config(self.layout, {'version': 1, 'mode': 'on', 'users': ['deck']})

    def release(self):
        return self.store.release(self.layout, runner=self.runner, proc=self.proc)

    def test_release_takes_the_real_mounts_off_and_stashes_and_a_second_release_changes_nothing(self):
        self.configure()
        self.mount()
        side = self.side
        os.unlink(side.steamapps / 'common/Game/data/y.dat')
        (self.apps / 'appmanifest_100.acf').write_text('what Steam wrote')
        item = self.release()['stores'][str(self.layout.root)]['deck']
        self.assertEqual(item['unmounted'], ['prefixes', 'library', 'real'])
        self.assertEqual(sorted(item['stashed']), ['appmanifest_100.acf', 'common/Game/data/y.dat'])
        self.assertEqual(self.store.overlay_mounts(side), [])
        self.assertFalse(os.path.lexists(self.apps / 'appmanifest_100.acf'))      # stock Steam finds no manifest
        self.assertFalse(os.path.lexists(self.apps / 'common/Game/data/y.dat'))   # and no whiteout
        self.assertEqual(self.release()['stores'][str(self.layout.root)]['deck'], {'unmounted': [], 'stashed': []})
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        self.assertEqual((side.steamapps / 'appmanifest_100.acf').read_text(), 'what Steam wrote')
        self.assertFalse(os.path.lexists(side.steamapps / 'common/Game/data/y.dat'))

    def test_an_overlay_that_is_busy_stays_mounted_with_its_prefix_bind(self):
        self.mount()
        side = self.side
        held = os.open(side.steamapps / 'common/Game/data/x.dat', os.O_RDONLY)      # a game that has a file open
        self.addCleanup(os.close, held)
        with self.assertRaises(self.store.StoreError):
            self.unmount(stash=False)
        mounts = self.store.mount_points()
        self.assertIn(str(side.steamapps), mounts)
        self.assertIn(str(side.steamapps / 'compatdata'), mounts)
        # The prefixes are the account's own folder again, not the overlay's.
        self.assertEqual(os.stat(side.steamapps / 'compatdata').st_dev, os.stat(side.reach / 'compatdata').st_dev)

    def test_the_slot_puts_the_stash_back_before_it_commits(self):
        # After a clean shutdown the manifests of stored games are in the stash; the commit reads the account's own
        # manifest of a game it updates (LIB-05).
        self.configure()
        self.mount()
        (self.apps / 'appmanifest_100.acf').write_text('an update that Steam wrote')
        self.release()
        self.assertFalse(os.path.lexists(self.apps / 'appmanifest_100.acf'))
        seen = {}

        class Tidy:
            @staticmethod
            def migrate(layout, users, proc=None):
                return {}

            @staticmethod
            def commit(layout, users):
                path = layout.side('deck').real_steamapps / 'appmanifest_100.acf'
                seen['manifest'] = path.read_text() if os.path.lexists(path) else None
                return {'committed': [], 'dropped': [], 'removed': []}
        report = self.store._slot_layout(self.layout, {'users': ['deck'], 'mode': 'on'}, tidy=Tidy, runner=self.runner,
                                         proc=self.proc)
        self.assertEqual(seen['manifest'], 'an update that Steam wrote')
        self.assertNotIn('stash_errors', report)
        self.assertFalse(self.side.stash.exists())

    def test_a_mount_over_a_running_steam_leaves_the_real_folder_alone_and_what_holds_it_open_does_not_see_the_merge(self):
        self.configure()
        self.mount()
        (self.apps / 'appmanifest_100.acf').write_text('what the account wrote')
        self.release()                                                             # the manifest is in the stash now
        self.assertFalse(os.path.lexists(self.apps / 'appmanifest_100.acf'))
        (self.proc / '77').mkdir()
        (self.proc / '77/comm').write_text('steam\n')                              # the account's Steam runs
        # A process that has the real folder open and a watch on it (what a running Steam may have), and a lookup by path.
        held = os.open(self.apps, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, held)
        watcher = self.watch(self.apps)
        before = sorted(os.listdir(held))
        self.events(watcher) if watcher is not None else None                  # (what the listing above made)
        done = self.store.mount(self.layout, 'deck', runner=self.runner, proc=self.proc, while_running=True)
        self.assertEqual(done, ['manifests wait', 'real', 'library', 'prefixes'])
        changes = self.events(watcher) if watcher is not None else []          # before anything looks at the merge
        # The real folder was not changed: the manifest is still in the stash, nothing was added.
        self.assertEqual(sorted(os.listdir(self.side.reach)), ['common', 'compatdata'])
        self.assertTrue((self.side.stash / 'steamapps/appmanifest_100.acf').exists())
        # By path the library is merged now (the store's manifest and files show); the open folder is the real one.
        self.assertEqual(sorted(os.listdir(self.apps / 'common/Game')), ['data', 'save.dat'])
        self.assertIn('appmanifest_100.acf', os.listdir(self.apps))
        self.assertEqual(sorted(os.listdir(held)), before)
        self.assertNotIn('appmanifest_100.acf', os.listdir(held))
        if watcher is not None:
            self.addCleanup(os.close, watcher)
            # Mounting makes no change event on the real folder. (The first lookup through the overlay then makes one:
            # an attribute change on `common`, the overlay marking the folder it merges. Steam may or may not rescan on it.)
            self.assertEqual(changes, [])
        # Once the Steam is closed the next mount puts the stash back.
        (self.proc / '77/comm').write_text('other\n')
        self.unmount(stash=False)
        self.assertEqual(self.mount(), ['real', 'library', 'prefixes'])
        self.assertEqual((self.side.steamapps / 'appmanifest_100.acf').read_text(), 'what the account wrote')
        self.assertFalse(self.side.stash.exists())

    def watch(self, folder):
        """An inotify watch on the folder for every event, or None where libc has no inotify."""
        import ctypes
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            descriptor = libc.inotify_init1(os.O_NONBLOCK)
            if descriptor < 0 or libc.inotify_add_watch(descriptor, str(folder).encode(), 0xfff) < 0:
                return None
        except (AttributeError, OSError):
            return None
        return descriptor

    def events(self, descriptor):
        """The change events (create, delete, move, modify, attribute) the watch has seen; reads are not changes."""
        import struct
        try:
            data = os.read(descriptor, 65536)
        except BlockingIOError:
            return []
        found, at = [], 0
        while at < len(data):
            _wd, mask, _cookie, length = struct.unpack_from('iIII', data, at)
            name = data[at + 16:at + 16 + length].rstrip(b'\0').decode()
            if mask & (0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800):
                found.append((hex(mask), name))
            at += 16 + length
        return found


if __name__ == '__main__':
    if outer() is False:
        sys.argv.remove('--inner')
        unittest.main()
