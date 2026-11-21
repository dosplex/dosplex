#!/usr/bin/env python3
# dosplex-test: tier=unit timeout=120
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""The tidy module of the shared game store: share, commit, removal and leaving (ADR-023, T-606 step 2).

Everything runs on made-up trees in a temporary folder, without root and without mounting anything. Two account
names stand for the current user, so owners never change; the overlay is played by plain folders, and an overlay
whiteout (a character device, which only root can make) by an empty file with no permissions, through the one
predicate the module uses for it. What the kernel does with leases, overlays and ownership is checked on a device.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
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


tidy = load('game_store_tidy', 'packaging/steamos/dosplex-game-store-tidy.py')
builders = load('depot_test_builders', 'tests/depot-test.py')
depot = tidy.depot
TidyError = tidy.TidyError
USERS = ['main', 'guest']


class Crash(BaseException):
    """A commit that dies in the middle (not an Exception, so nothing in the module catches it)."""


def fake_whiteout(info):
    return stat.S_ISREG(info.st_mode) and info.st_size == 0 and stat.S_IMODE(info.st_mode) == 0


def make_whiteout(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0))
    os.chmod(path, 0)


def snapshot(folder):
    """name -> (kind, content or link target, mtime_ns) of everything below folder, links not followed."""
    found = {}
    folder = Path(folder)
    for path in sorted(folder.rglob('*')):
        info = os.lstat(path)
        name = str(path.relative_to(folder))
        if stat.S_ISLNK(info.st_mode):
            found[name] = ('link', os.readlink(path), 0)
        elif stat.S_ISDIR(info.st_mode):
            found[name] = ('dir', None, 0)
        else:
            found[name] = ('file', path.read_bytes(), info.st_mtime_ns)
    return found


class Env(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='dosplex-tidy-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / 'dosplex-library'
        self.layout = tidy.store.Layout(self.root)
        (self.root / 'store/steamapps/common').mkdir(parents=True)
        self.entries = {}
        # Where the overlay is mounted: the steamapps folder of the account's library. The overlay is played by the
        # plain folder (what the account's Steam wrote is in it, and the store's files are read from the store).
        self.mounted = set()
        for name in USERS:
            home = self.base / 'home' / name
            steam = home / '.local/share/Steam'
            (steam / 'steamapps').mkdir(parents=True)
            (steam / 'depotcache').mkdir()
            self.entries[name] = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home), pw_name=name)
            self.layout.libraries[name] = (steam, steam)
            side = self.layout.side(name)
            side.base.mkdir(parents=True, exist_ok=True)
            self.mounted.add(str(side.steamapps))
        # A real Steam of whoever runs the tests must not count: the code looks at /proc unless a test gives it a
        # process table, so the default table is an empty one.
        empty_proc = self.base / 'empty-proc'
        empty_proc.mkdir()
        running = tidy.store.steam_running
        for patcher in (patch.object(tidy.store, 'account', lambda name: self.entries[name]),
                        patch.object(tidy.store, 'steam_running',
                                     lambda uid, proc=Path('/proc'): running(uid, empty_proc if proc == Path('/proc') else proc)),
                        patch.object(tidy.store, 'is_mounted', lambda path, *args, **kwargs: str(path) in self.mounted),
                        patch.object(tidy.store, 'is_whiteout', fake_whiteout),
                        patch.object(tidy, '_is_whiteout', fake_whiteout)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # Places.

    def steam(self, user):
        return tidy.store.steam_root_of(self.entries[user])

    def upper(self, user):
        """The account's own files of the shared library: its real library, which is the layer of its overlay."""
        return self.steam(user)

    def common(self, installdir='Game'):
        return self.root / 'store/steamapps/common' / installdir

    def own_library(self, user):
        return self.base / 'home' / user / 'SteamLibrary'

    def list_libraries(self, user, *extra):
        folders = {'0': {'path': str(self.steam(user))}}
        for number, path in enumerate(extra, 1):
            folders[str(number)] = {'path': str(path)}
        (self.steam(user) / 'steamapps/libraryfolders.vdf').write_text(depot.vdf_dumps('libraryfolders', folders))

    # Games.

    def depot_manifest(self, user, files, depot_id=1001, gid=555, links=None, executable=()):
        entries = [builders.file_entry(name, data, depot.EXECUTABLE if name in executable else 0)
                   for name, data in files.items()]
        for name, target in (links or {}).items():
            entries.append(builders.entry(name, 0, depot.SYMLINK, link=target))
        (self.steam(user) / 'depotcache' / depot.manifest_name(depot_id, gid)).write_bytes(builders.build(
            entries, depot_id=depot_id, gid=gid))

    def acf(self, appid='100', buildid='7', flags='4', installdir='Game', depots=((1001, 555),), name='Test Game'):
        state = {'appid': appid, 'Universe': '1', 'name': name, 'StateFlags': flags, 'installdir': installdir,
                 'buildid': buildid, 'LastOwner': '1',
                 'UpdateResult': '0', 'BytesToDownload': '5', 'BytesDownloaded': '5',
                 'InstalledDepots': {str(d): {'manifest': str(g), 'size': '1'} for d, g in depots}}
        return depot.vdf_dumps('AppState', state)

    def install(self, user, files, *, appid='100', library=None, installdir='Game', links=None, executable=(),
                manifest_files=None, depot_manifest=True, **acf):
        """A game in the account's own library (default: the Steam folder); manifest_files is what the depot says."""
        library = Path(library) if library else self.steam(user)
        folder = library / 'steamapps/common' / installdir
        for name, data in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            if name in executable:
                os.chmod(folder / name, 0o755)
        for name, target in (links or {}).items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, folder / name)
        (library / 'steamapps').mkdir(parents=True, exist_ok=True)
        (library / 'steamapps' / f'appmanifest_{appid}.acf').write_text(self.acf(appid, installdir=installdir, **acf))
        if depot_manifest:
            self.depot_manifest(user, files if manifest_files is None else manifest_files, links=links,
                                executable=executable)
        return folder

    def share(self, user, appid='100', **kwargs):
        return tidy.share(self.layout, user, appid, USERS, **kwargs)

    def commit(self):
        return tidy.commit(self.layout, USERS)

    def standard(self):
        """Main installs a game in its Steam folder: three depot files, an extra and a file that was changed."""
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa', 'data/b.dat': b'bbbbbb', 'config.ini': b'original'}
        folder = self.install('main', files, executable=('bin/game',))
        (folder / 'config.ini').write_bytes(b'changed!')
        (folder / 'save').mkdir()
        (folder / 'save/slot1').write_bytes(b'my progress')
        return folder

    def shared_game(self):
        folder = self.standard()
        self.share('main')
        self.assertEqual(self.commit()['committed'], ['100'])
        return folder

    def read_acf(self, path):
        return depot.read_vdf(path)[1]

    def override(self, user, changed, *, listed=None, buildid, gid=556, whiteouts=(), appid='100', installdir='Game',
                 merged=True):
        """What the account's Steam does with a game the store holds: the files it changes, and its manifest, are written
        to its real library, and the account sees that over the store's copy. With merged the overlay is played too:
        the real folder is bound at the reach (a folder of its own here) and the library folder shows the store's
        files with the account's over them. listed is every file of the new build (default: the changed ones)."""
        side = self.layout.side(user)
        real = side.reach if merged else side.steamapps
        if merged:
            shutil.copytree(side.steamapps, real, symlinks=True, dirs_exist_ok=True)     # what is there already
        game = real / 'common' / installdir
        for name, data in changed.items():
            (game / name).parent.mkdir(parents=True, exist_ok=True)
            (game / name).write_bytes(data)
        for name in whiteouts:
            make_whiteout(game / name)
        (real / f'appmanifest_{appid}.acf').write_text(self.acf(appid, buildid=buildid, depots=((1001, gid),)))
        self.depot_manifest(user, listed or changed, gid=gid)
        if merged:
            shown = side.steamapps
            shutil.rmtree(shown)
            shutil.copytree(real, shown, symlinks=True, ignore=lambda folder, names: [
                name for name in names if fake_whiteout(os.lstat(os.path.join(folder, name)))])
            if self.common(installdir).exists():
                shutil.copytree(self.common(installdir), shown / 'common' / installdir, symlinks=True,
                                dirs_exist_ok=True, copy_function=lambda one, two: None if os.path.lexists(two)
                                else shutil.copy2(one, two))
            for name in whiteouts:
                with contextlib.suppress(FileNotFoundError):
                    (shown / 'common' / installdir / name).unlink()
            self.mounted.add(str(side.reach))

    def unmerge(self, *users):
        """The slot: the overlays go, and the library folder is the account's real folder again."""
        for user in users or USERS:
            side = self.layout.side(user)
            if str(side.reach) not in self.mounted:
                continue
            self.mounted.discard(str(side.reach))
            if (side.steamapps / 'libraryfolders.vdf').exists():
                shutil.copy(side.steamapps / 'libraryfolders.vdf', side.reach / 'libraryfolders.vdf')
            shutil.rmtree(side.steamapps)
            os.rename(side.reach, side.steamapps)


class ShareRefusals(Env):
    def refused(self, pattern, user='main'):
        with self.assertRaisesRegex(TidyError, pattern):
            self.share(user)
        self.assertEqual(tidy.pending(self.layout), [])

    def list_apps(self, user, listed):
        """libraryfolders.vdf with an "apps" list for each library: {path: [appid, ...]}."""
        folders = {str(number): {'path': str(path), 'apps': {appid: '1' for appid in apps}}
                   for number, (path, apps) in enumerate(listed.items())}
        (self.steam(user) / 'steamapps/libraryfolders.vdf').write_text(depot.vdf_dumps('libraryfolders', folders))

    def test_a_second_copy_that_steam_does_not_use_is_refused_and_says_where_the_used_one_is(self):
        # Seen on the Steam Machine: Crimsonland in the SD card's library (listed) and an old copy with its manifest in
        # the Steam folder (not listed). Each disk's store took one, and Steam found the game in two libraries.
        self.install('main', {'a': b'1'})
        elsewhere = self.base / 'card/SteamLibrary'
        self.list_apps('main', {self.steam('main'): [], elsewhere: ['100']})
        self.refused(r'second copy: Steam uses the one in .*card/SteamLibrary')
        listing = {item['appid']: item for item in tidy.shareable(self.layout, 'main', USERS)}
        self.assertIn('second copy', listing['100']['reason'])

    def test_the_copy_steam_lists_is_shared(self):
        self.install('main', {'a': b'1'})
        self.list_apps('main', {self.steam('main'): ['100']})
        self.assertEqual(self.share('main')['appid'], '100')
        self.assertEqual(tidy._steam_uses(self.steam('main'))['100'], self.steam('main'))

    def test_a_game_steam_lists_nowhere_is_shared_as_before(self):
        self.install('main', {'a': b'1'})
        self.list_apps('main', {self.steam('main'): []})
        self.assertEqual(self.share('main')['appid'], '100')

    def test_a_game_that_is_not_fully_installed_is_refused(self):
        self.install('main', {'a': b'1'}, flags='1026')
        self.refused('not fully installed')

    def test_a_missing_depot_manifest_is_refused_and_counted(self):
        self.install('main', {'a': b'1'}, depot_manifest=False)
        self.refused(r'waits for its file list from Steam \(1 of 1 depot manifests')

    def test_an_install_folder_that_is_not_one_plain_name_is_refused(self):
        for bad in ('', 'a/b', '..', '.', 'x\0y'):
            with self.subTest(bad=bad):
                (self.steam('main') / 'steamapps/appmanifest_100.acf').write_text(
                    self.acf().replace('"Game"', depot._vdf_quote(bad)))
                self.refused('install folder')

    def test_a_game_with_nothing_that_matches_is_refused(self):
        self.install('main', {'a': b'wrong'}, manifest_files={'a': b'right'})
        self.refused('no file that matches')

    def test_a_game_on_another_disk_is_not_offered_and_so_not_shared(self):
        self.install('main', {'a': b'1'})
        with patch.object(tidy, '_same_disk', return_value=False):
            self.refused('not installed')
            self.assertEqual(tidy.shareable(self.layout, 'main', USERS), [])

    def test_an_unknown_game_account_and_app_id_are_refused(self):
        with self.assertRaisesRegex(TidyError, 'not installed'):
            self.share('main', '999')
        with self.assertRaisesRegex(TidyError, 'not an account'):
            tidy.share(self.layout, 'someone', '100', USERS)
        with self.assertRaisesRegex(TidyError, 'app id'):
            self.share('main', '../x')

    def test_a_game_folder_that_is_a_link_is_refused_and_not_read(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'a').write_bytes(b'1')
        self.install('main', {'a': b'1'})
        game = self.steam('main') / 'steamapps/common/Game'
        shutil.rmtree(game)
        os.symlink(elsewhere, game)
        self.refused('link|folder')

    def test_a_common_folder_that_is_a_link_is_refused(self):
        self.install('main', {'a': b'1'})
        common = self.steam('main') / 'steamapps/common'
        real = self.base / 'real-common'
        common.rename(real)
        os.symlink(real, common)
        self.refused('link|folder')

    def test_the_same_build_over_the_store_is_already_shared_and_an_older_one_is_refused(self):
        self.shared_game()
        stored = {'bin/game': b'#!game', 'data/a.dat': b'aaaa', 'data/b.dat': b'bbbbbb'}
        self.override('guest', {'save.dat': b'mine'}, listed=stored, buildid='7', gid=555)
        with self.assertRaisesRegex(TidyError, 'already shared'):
            self.share('guest')
        self.override('guest', {'bin/game': b'#!game'}, buildid='3', gid=777)
        with self.assertRaisesRegex(TidyError, r'holds build 7.*build 3'):
            self.share('guest')

    def test_a_copy_of_another_build_in_a_library_is_refused_with_both_numbers(self):
        self.shared_game()
        self.install('guest', {'bin/game': b'#!game'}, buildid='3')
        with self.assertRaisesRegex(TidyError, r'holds build 7.*build 3'):
            self.share('guest')

    def test_only_the_accounts_own_files_are_ever_taken_never_the_stores(self):
        # The overlay shows the store's files in the account's library. A game the account only has through them (a
        # manifest of the same build, nothing of its own) is not shared again.
        self.shared_game()
        side = self.layout.side('guest')
        shutil.copytree(self.common(), side.steamapps / 'common/Game', symlinks=True)
        self.depot_manifest('guest', {'bin/game': b'#!game', 'data/a.dat': b'aaaa', 'data/b.dat': b'bbbbbb',
                                      'config.ini': b'original'})
        (side.steamapps / 'appmanifest_100.acf').write_text(self.acf(buildid='7'))
        side.reach.mkdir(parents=True)
        (side.reach / 'appmanifest_100.acf').write_text(self.acf(buildid='7'))
        self.mounted.add(str(side.reach))
        with self.assertRaisesRegex(TidyError, 'already shared'):
            self.share('guest')
        self.assertEqual(tidy.pending(self.layout), [])


class ShareAndCommit(Env):
    def test_a_library_game_goes_into_the_store_and_the_rest_stays_with_the_account(self):
        folder = self.standard()
        calls = []
        summary = self.share('main', progress=lambda name, size: calls.append(name))
        self.assertEqual(summary, {'appid': '100', 'name': 'Test Game', 'source': 'library', 'kind': 'new',
                                   'files': 3, 'bytes': 4 + 6 + 6, 'private': 2})
        self.assertIn('data/a.dat', calls)
        record = json.loads((self.root / 'pending/100.json').read_text())
        self.assertEqual((stat.S_IMODE(os.stat(self.root / 'pending/100.json').st_mode)), 0o600)
        self.assertEqual(sorted(item['name'] for item in record['files']), ['bin/game', 'data/a.dat', 'data/b.dat'])
        self.assertEqual(record['listed'], ['bin/game', 'config.ini', 'data/a.dat', 'data/b.dat'])
        self.assertEqual(tidy.pending(self.layout)[0]['name'], 'Test Game')
        self.assertEqual(tidy.pending(self.layout)[0]['files'], 3)

        result = self.commit()
        self.assertEqual((result['committed'], result['dropped'], result['removed']), (['100'], [], []))
        game = self.common()
        self.assertEqual((game / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual(stat.S_IMODE(os.stat(game / 'bin/game').st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(game / 'data/b.dat').st_mode), 0o644)
        self.assertFalse((game / 'config.ini').exists())
        self.assertFalse((game / 'save').exists())
        private = self.upper('main') / 'steamapps/common/Game'
        self.assertEqual((private / 'config.ini').read_bytes(), b'changed!')
        self.assertEqual((private / 'save/slot1').read_bytes(), b'my progress')
        self.assertFalse((private / 'data').exists())
        self.assertEqual(sorted(os.listdir(folder)), ['config.ini', 'save'])       # the account's own folder keeps its own
        self.assertFalse((self.steam('main') / 'steamapps/appmanifest_100.acf').exists())
        canonical = self.read_acf(self.root / 'store/steamapps/appmanifest_100.acf')
        self.assertEqual(canonical['StateFlags'], '4')
        for key in tidy.TRANSIENT_KEYS:
            self.assertNotIn(key, canonical)
        self.assertEqual(canonical['InstalledDepots'], {'1001': {'manifest': '555', 'size': '1'}})
        other = self.read_acf(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((other['StateFlags'], other['FullValidateBeforeNextUpdate']), ('2', '1'))
        self.assertNotIn('BytesToDownload', other)
        self.assertFalse((self.upper('main') / 'steamapps/appmanifest_100.acf').exists())
        app = tidy.stored(self.layout)['100']
        self.assertEqual((app['shared_by'], app['installdir'], app['buildid'], app['depots']),
                         ('main', 'Game', '7', {'1001': '555'}))
        self.assertEqual(app['files'], ['bin/game', 'data/a.dat', 'data/b.dat'])
        self.assertEqual(tidy.pending(self.layout), [])
        self.assertEqual(os.listdir(self.root / 'staging'), [])

    def test_an_account_that_uninstalled_the_game_gets_no_manifest(self):
        self.standard()
        self.share('main')
        make_whiteout(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual(self.commit()['committed'], ['100'])
        self.assertTrue(fake_whiteout(os.lstat(self.upper('guest') / 'steamapps/appmanifest_100.acf')))

    def test_an_account_that_has_the_game_in_its_own_library_gets_no_manifest(self):
        self.standard()
        self.list_libraries('guest', self.own_library('guest'))
        self.install('guest', {'z': b'z'}, library=self.own_library('guest'), depot_manifest=False)
        self.share('main')
        self.assertEqual(self.commit()['committed'], ['100'])
        self.assertFalse((self.upper('guest') / 'steamapps/appmanifest_100.acf').exists())

    def test_symlinks_that_stay_inside_are_shared_and_those_that_leave_or_have_a_second_link_are_not(self):
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa'}
        links = {'bin/current': 'game', 'bin/escape': '../../outside', 'bin/absolute': '/etc/passwd'}
        folder = self.install('main', files, links=links)
        os.link(folder / 'data/a.dat', folder / 'data/a.copy')
        summary = self.share('main')
        self.assertEqual(summary['files'], 2)   # bin/game and bin/current; a.dat has two links
        self.assertEqual(summary['private'], 4)  # the copy, a.dat, escape, absolute
        self.commit()
        self.assertEqual(os.readlink(self.common() / 'bin/current'), 'game')
        self.assertFalse((self.common() / 'bin/escape').exists() or (self.common() / 'bin/escape').is_symlink())
        private = self.upper('main') / 'steamapps/common/Game'
        self.assertEqual(os.readlink(private / 'bin/escape'), '../../outside')
        self.assertEqual(os.readlink(private / 'bin/absolute'), '/etc/passwd')
        self.assertEqual((private / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertTrue((private / 'data/a.copy').exists())

    def test_a_file_changed_after_share_rolls_the_whole_game_back(self):
        folder = self.standard()
        self.share('main')
        target = folder / 'data/a.dat'
        old = os.stat(target)
        target.write_bytes(b'AAAA')                      # same size
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))   # same mtime: only ctime shows it
        before = snapshot(folder)
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertEqual([item['appid'] for item in result['dropped']], ['100'])
        self.assertEqual(snapshot(folder), before)
        self.assertFalse(self.common().exists())
        self.assertEqual(tidy.pending(self.layout), [])
        self.assertEqual(os.listdir(self.root / 'staging'), [])
        self.assertTrue((self.steam('main') / 'steamapps/appmanifest_100.acf').exists())
        self.assertFalse((self.upper('guest') / 'steamapps/appmanifest_100.acf').exists())

    def test_a_changed_app_manifest_drops_the_record(self):
        folder = self.standard()
        self.share('main')
        manifest = self.steam('main') / 'steamapps/appmanifest_100.acf'
        manifest.write_text(manifest.read_text() + '\n')
        before = snapshot(folder)
        result = self.commit()
        self.assertEqual(result['dropped'][0]['appid'], '100')
        self.assertIn('manifest', result['dropped'][0]['reason'])
        self.assertEqual(snapshot(folder), before)

    def test_a_writer_holding_a_file_open_stops_the_commit(self):
        folder = self.standard()
        self.share('main')
        before = snapshot(folder)
        with open(folder / 'data/b.dat', 'ab'):
            result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertIn('open for writing', result['dropped'][0]['reason'])
        self.assertEqual(snapshot(folder), before)
        self.assertFalse(self.common().exists())

    def test_a_folder_replaced_by_a_link_after_share_is_never_followed(self):
        folder = self.standard()
        self.share('main')
        (folder / 'data').rename(folder / 'data.real')
        os.symlink('data.real', folder / 'data')
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertIn('data', result['dropped'][0]['reason'])
        self.assertEqual((folder / 'data.real/a.dat').read_bytes(), b'aaaa')
        self.assertFalse(self.common().exists())
        self.assertEqual((folder / 'bin/game').read_bytes(), b'#!game')   # moved first, put back

    def test_a_file_replaced_by_a_link_after_share_is_not_moved(self):
        folder = self.standard()
        self.share('main')
        (folder / 'data/a.dat').unlink()
        os.symlink('b.dat', folder / 'data/a.dat')
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertTrue((folder / 'data/a.dat').is_symlink())

    def test_the_record_is_replaced_by_a_new_share(self):
        folder = self.standard()
        self.share('main')
        first = json.loads((self.root / 'pending/100.json').read_text())
        (folder / 'extra').write_bytes(b'x')
        self.share('main')
        second = json.loads((self.root / 'pending/100.json').read_text())
        self.assertEqual(first['version'], second['version'])
        self.assertEqual(second['files'], first['files'])
        self.assertEqual(len(tidy.pending(self.layout)), 1)

    def test_the_old_game_folder_is_cleaned_bottom_up_and_a_non_empty_folder_stays(self):
        folder = self.standard()
        (folder / 'deep/er/still').mkdir(parents=True)
        (folder / 'empty').mkdir()
        self.share('main')
        self.commit()
        private = self.upper('main') / 'steamapps/common/Game'
        self.assertEqual(private, folder)
        self.assertTrue((private / 'save/slot1').exists())
        # A folder the game made stays with the account even when it is empty; one the store has is not copied.
        self.assertTrue((private / 'empty').is_dir())
        self.assertTrue((private / 'deep/er/still').is_dir())
        self.assertFalse((private / 'data').exists())

    def test_a_game_in_a_second_library_of_the_account_is_found(self):
        self.list_libraries('main', self.own_library('main'))
        folder = self.install('main', {'a': b'1'}, library=self.own_library('main'))
        self.assertEqual(self.share('main')['kind'], 'new')
        self.commit()
        self.assertFalse(folder.exists())
        self.assertEqual((self.common() / 'a').read_bytes(), b'1')

    def test_a_pending_record_that_is_not_understood_is_dropped_and_others_go_on(self):
        self.standard()
        self.share('main')
        (self.root / 'pending/555.json').write_text('{"version": 9}')
        (self.root / 'pending/556.json').write_text('not json')
        result = self.commit()
        self.assertEqual(result['committed'], ['100'])
        self.assertEqual(sorted(item['appid'] for item in result['dropped']), ['555', '556'])
        self.assertEqual(tidy.pending(self.layout), [])


class SharedFolder(Env):
    """Two apps in one install folder (a game and its add-on; or a forged manifest naming a stored game's folder)
    are never stored together: one would overwrite, prune or delete the other (LIB-01)."""

    def add_on(self, user, installdir='Game', appid='200', files=None):
        """A second app in the same folder: its files are the add-on's own, listed in a depot of its own."""
        files = files or {'dlc/extra.dat': b'xx'}
        folder = self.steam(user) / 'steamapps/common' / installdir
        for name, data in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
        (self.steam(user) / 'steamapps' / f'appmanifest_{appid}.acf').write_text(
            self.acf(appid, installdir=installdir, depots=((1002, 777),), name='Add-on'))
        self.depot_manifest(user, files, depot_id=1002, gid=777)

    def reasons(self, user):
        return {item['appid']: item['reason'] for item in tidy.shareable(self.layout, user, USERS)}

    def pair_in_records(self):
        """What a version before the check could have left: a second app stored in the first one's folder."""
        self.shared_game()
        (self.common() / 'dlc').mkdir()
        (self.common() / 'dlc/extra.dat').write_bytes(b'xx')
        (self.root / 'apps/200.json').write_text(json.dumps(dict(tidy.stored(self.layout)['100'], appid='200',
                                                                    name='Add-on')))

    def test_two_apps_of_one_account_in_one_folder_are_neither_taken(self):
        self.install('main', {'bin/game': b'#!game'})
        self.add_on('main')
        reasons = self.reasons('main')
        for appid, other in (('100', '200'), ('200', '100')):
            self.assertRegex(reasons[appid], f'shares its install folder with game {other}')
            with self.assertRaisesRegex(TidyError, 'shares its install folder'):
                self.share('main', appid)
        self.assertEqual((tidy.pending(self.layout), self.commit()['committed']), ([], []))
        self.assertEqual((self.steam('main') / 'steamapps/common/Game/bin/game').read_bytes(), b'#!game')
        self.assertEqual(tidy.stored(self.layout), {})

    def test_the_folder_is_compared_without_regard_to_letter_case(self):
        self.install('main', {'bin/game': b'#!game'})
        self.add_on('main', installdir='GAME')
        self.assertRegex(self.reasons('main')['100'], 'shares its install folder with game 200')

    def test_a_game_whose_folder_a_stored_app_owns_is_refused_and_the_store_stays_as_it_was(self):
        self.shared_game()
        before = snapshot(self.common())
        # The other account's Steam writes a manifest for another app into the stored game's folder (an honest add-on
        # or a forged one: the store cannot tell).
        self.add_on('guest')
        self.assertRegex(self.reasons('guest')['200'], r'shares its install folder with Test Game \(100\), which is '
                                                       r'stored already, and is left alone')
        with self.assertRaisesRegex(TidyError, 'stored already'):
            self.share('guest', '200')
        self.assertEqual(snapshot(self.common()), before)
        self.assertEqual(sorted(tidy.stored(self.layout)), ['100'])

    def test_a_record_pending_for_a_stored_folder_is_dropped_at_commit(self):
        # Prepared before the other app was stored: commit checks again.
        self.add_on('guest')
        self.share('guest', '200')
        record = tidy._json_files(self.layout, 'pending')['200']
        (self.root / 'pending/200.json').unlink()
        self.shared_game()
        before = snapshot(self.common())
        (self.root / 'pending/200.json').write_text(json.dumps(record))
        result = self.commit()
        self.assertEqual([item['appid'] for item in result['dropped']], ['200'])
        self.assertIn('stored already', result['dropped'][0]['reason'])
        self.assertEqual(snapshot(self.common()), before)

    def two_pending(self, first, second):
        """Records written as the two shares would, one for each account (the share-time check sees one account)."""
        self.install('main', {'bin/game': b'#!game'}, appid=first)
        self.add_on('guest', appid=second)
        with patch.object(tidy, '_account_folders', lambda *args: {}):
            self.share('main', first)
            self.share('guest', second)
        result = self.commit()
        self.assertEqual(sorted(item['appid'] for item in result['dropped']), sorted([first, second]))
        self.assertEqual(result['committed'], [])
        self.assertFalse(self.common().exists())

    def test_two_records_pending_for_one_folder_are_both_dropped_when_the_lower_app_id_is_first(self):
        self.two_pending('100', '200')

    def test_two_records_pending_for_one_folder_are_both_dropped_when_the_higher_app_id_is_first(self):
        self.two_pending('300', '200')

    def test_an_update_into_a_folder_of_two_stored_apps_is_refused_and_prunes_nothing(self):
        self.pair_in_records()
        before = snapshot(self.common())
        files = {'bin/game': b'#!game', 'data/a.dat': b'AAAAAA'}
        self.override('main', {'data/a.dat': b'AAAAAA'}, listed=files, buildid='8')
        with self.assertRaisesRegex(TidyError, r'shares its install folder with Add-on \(200\)'):
            self.share('main')
        # A record that got past the share check (made here by switching it off) is stopped at commit.
        with patch.object(tidy, '_require_alone', lambda *args: None):
            self.share('main')
        self.unmerge()
        self.assertEqual(tidy._json_files(self.layout, 'pending')['100']['kind'], 'update')
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertEqual([item['appid'] for item in result['dropped']], ['100'])
        self.assertEqual(snapshot(self.common()), before)

    def test_a_delete_never_takes_the_folder_of_two_stored_apps(self):
        self.pair_in_records()
        before = snapshot(self.common())
        for user in USERS:
            make_whiteout(self.upper(user) / 'steamapps/appmanifest_100.acf')
        result = self.commit()
        self.assertEqual(result['removed'], [])
        self.assertTrue(any('could not be removed' in item and 'shares its install folder' in item
                            for item in result['warnings']), result['warnings'])
        self.assertEqual(snapshot(self.common()), before)
        self.assertEqual(sorted(tidy.stored(self.layout)), ['100', '200'])

    def test_a_pair_already_in_the_records_is_listed_with_the_reason(self):
        self.pair_in_records()
        (self.upper('main') / 'steamapps/appmanifest_100.acf').write_text(self.acf(buildid='8'))
        self.assertRegex(self.reasons('main')['100'], r'shares its install folder with Add-on \(200\)')


class JoinAndUpdate(Env):
    def test_a_copy_of_the_stored_build_joins_and_only_its_extras_stay(self):
        self.shared_game()
        before = snapshot(self.common())
        self.list_libraries('guest', self.own_library('guest'))
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa', 'data/b.dat': b'bbbbbb', 'config.ini': b'original'}
        folder = self.install('guest', files, library=self.own_library('guest'), executable=('bin/game',),
                              buildid='7')
        (folder / 'prefs.ini').write_bytes(b'mine')
        summary = self.share('guest')
        self.assertEqual((summary['kind'], summary['files'], summary['private']), ('join', 4, 1))
        result = self.commit()
        self.assertEqual(result['committed'], ['100'])
        self.assertEqual(result['warnings'], [])
        # Main had changed config.ini before it shared, so the store never got that file: Guest's verified copy is the
        # only pristine one and enters the store. (It used to be deleted with the duplicates: afterwards neither Guest
        # nor the store had the file.) The three files the store has are dropped as duplicates and stay as they were.
        after = snapshot(self.common())
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(set(after) - set(before), {'config.ini'})
        self.assertEqual((self.common() / 'config.ini').read_bytes(), b'original')
        self.assertEqual(os.stat(self.common() / 'config.ini').st_uid, os.getuid())
        self.assertIn('config.ini', tidy.stored(self.layout)['100']['files'])
        self.assertFalse(folder.exists())
        self.assertFalse((self.own_library('guest') / 'steamapps/appmanifest_100.acf').exists())
        private = self.upper('guest') / 'steamapps/common/Game'
        self.assertEqual((private / 'prefs.ini').read_bytes(), b'mine')
        self.assertFalse((private / 'data').exists())
        manifest = self.read_acf(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((manifest['StateFlags'], manifest['FullValidateBeforeNextUpdate']), ('2', '1'))
        self.assertEqual(tidy.stored(self.layout)['100']['shared_by'], 'main')

    def test_a_joining_file_that_differs_from_the_stores_is_never_deleted(self):
        # The store's copy of a file is not what the joining account verified (damage, or a hand in the store): the
        # verified copy must not be thrown away as a "duplicate".
        self.shared_game()
        (self.common() / 'data/a.dat').write_bytes(b'XXXXXXXX')            # the store's copy is another file now
        self.list_libraries('guest', self.own_library('guest'))
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa', 'data/b.dat': b'bbbbbb', 'config.ini': b'original'}
        self.install('guest', files, library=self.own_library('guest'), executable=('bin/game',), buildid='7')
        self.share('guest')
        result = self.commit()
        self.assertEqual((self.common() / 'data/a.dat').read_bytes(), b'XXXXXXXX')   # the store is not overwritten
        kept = [path for path in Path(self.layout.root).rglob('a.dat') if path.read_bytes() == b'aaaa']
        self.assertTrue(kept, 'the verified copy of data/a.dat is gone')
        self.assertTrue(any('data/a.dat' in item and 'differs' in item for item in result['warnings']), result['warnings'])
        # A later commit and a later share of the same game find the kept file and neither fail nor delete it.
        again = self.commit()
        kept = [path for path in Path(self.layout.root).rglob('a.dat') if path.read_bytes() == b'aaaa']
        self.assertTrue(kept, 'the verified copy of data/a.dat was deleted by the next commit')
        self.assertEqual((self.common() / 'data/a.dat').read_bytes(), b'XXXXXXXX')
        # It is said again for as long as the two copies differ: a store that holds a wrong file should not go quiet.
        self.assertTrue(any('data/a.dat' in item and 'differs' in item for item in again['warnings']), again)

    def test_an_update_by_the_other_account_does_not_change_who_gets_the_game_back(self):
        self.shared_game()
        self.assertEqual(tidy.stored(self.layout)['100']['shared_by'], 'main')
        files = {'bin/game': b'#!game', 'data/a.dat': b'AAAAAA', 'data/c.dat': b'cc'}
        changed = {name: files[name] for name in ('data/a.dat', 'data/c.dat')}
        self.override('guest', changed, listed=files, buildid='8', whiteouts=('data/b.dat',))
        self.assertEqual(self.share('guest')['kind'], 'update')
        self.unmerge()
        self.assertEqual(self.commit()['committed'], ['100'])
        app = tidy.stored(self.layout)['100']
        self.assertEqual((app['shared_by'], app['buildid']), ('main', '8'))
        self.assertEqual(app.get('updated_by'), 'guest')

    def test_a_newer_build_over_the_store_updates_the_store(self):
        self.shared_game()
        guest_manifest = self.upper('guest') / 'steamapps/appmanifest_100.acf'
        self.assertTrue(guest_manifest.exists())
        # Main's Steam updated the game in its library: a changed file and a new one are written over the store's copy,
        # one file is gone (a whiteout), one is untouched below. The depot says: bin/game, data/a.dat, data/c.dat.
        files = {'bin/game': b'#!game', 'data/a.dat': b'AAAAAA', 'data/c.dat': b'cc'}
        changed = {name: files[name] for name in ('data/a.dat', 'data/c.dat')}
        self.override('main', changed, listed=files, buildid='8', whiteouts=('data/b.dat', 'keepme.dat'))
        summary = self.share('main')
        self.assertEqual((summary['kind'], summary['source'], summary['files']), ('update', 'library', 2))
        self.unmerge()
        upper_game = self.upper('main') / 'steamapps/common/Game'
        result = self.commit()
        self.assertEqual(result['committed'], ['100'])
        game = self.common()
        self.assertEqual((game / 'data/a.dat').read_bytes(), b'AAAAAA')
        self.assertEqual((game / 'data/c.dat').read_bytes(), b'cc')
        self.assertEqual((game / 'bin/game').read_bytes(), b'#!game')
        self.assertFalse((game / 'data/b.dat').exists())
        self.assertFalse((upper_game / 'data/b.dat').exists())
        self.assertFalse((upper_game / 'keepme.dat').exists())
        self.assertFalse((upper_game / 'data/a.dat').exists())
        self.assertFalse((self.upper('main') / 'steamapps/appmanifest_100.acf').exists())
        canonical = self.read_acf(self.root / 'store/steamapps/appmanifest_100.acf')
        self.assertEqual((canonical['buildid'], canonical['StateFlags']), ('8', '4'))
        app = tidy.stored(self.layout)['100']
        self.assertEqual((app['buildid'], app['depots'], app['files']),
                         ('8', {'1001': '556'}, ['bin/game', 'data/a.dat', 'data/c.dat']))
        again = self.read_acf(guest_manifest)
        self.assertEqual((again['buildid'], again['StateFlags'], again['FullValidateBeforeNextUpdate']), ('8', '2', '1'))
        # What the account wrote into the game folder itself stays where it was.
        self.assertEqual((upper_game / 'save/slot1').read_bytes(), b'my progress')

    def test_a_game_installed_by_steam_into_the_library_is_new(self):
        side = self.layout.side('guest')
        files = {'run': b'#!run', 'pak/one': b'1'}
        game = side.steamapps / 'common/Fresh'
        for name, data in files.items():
            (game / name).parent.mkdir(parents=True, exist_ok=True)
            (game / name).write_bytes(data)
        (game / 'save.dat').write_bytes(b's')
        (side.steamapps / 'appmanifest_200.acf').write_text(self.acf('200', installdir='Fresh'))
        self.depot_manifest('guest', files)
        self.assertEqual(self.share('guest', '200')['kind'], 'new')
        self.assertEqual(self.commit()['committed'], ['200'])
        self.assertEqual((self.common('Fresh') / 'pak/one').read_bytes(), b'1')
        self.assertFalse((self.common('Fresh') / 'save.dat').exists())
        self.assertEqual((game / 'save.dat').read_bytes(), b's')
        self.assertFalse((side.steamapps / 'appmanifest_200.acf').exists())
        self.assertTrue((self.upper('main') / 'steamapps/appmanifest_200.acf').exists())

    def test_a_stored_game_is_refused_while_the_library_is_not_merged_with_the_store(self):
        self.shared_game()
        self.mounted.clear()
        (self.upper('main') / 'steamapps/appmanifest_100.acf').write_text(self.acf(buildid='8'))
        with self.assertRaisesRegex(TidyError, 'not merged'):
            self.share('main')


class Duplicates(Env):
    """Both accounts' Steam downloaded the same update; one copy enters the store, the other must not stay."""

    def updated_by_both(self):
        """Both accounts' Steam updated the game. Main's overlay is played (it is the account that shares); Guest's is
        not needed, only what its Steam wrote."""
        self.shared_game()                          # the store holds build 7: bin/game, data/a.dat, data/b.dat
        new = {'bin/game': b'#!game v2', 'data/a.dat': b'aaaa', 'data/c.dat': b'cc'}
        changed = {name: new[name] for name in ('bin/game', 'data/c.dat')}     # the changed file and the new one
        self.override('main', changed, listed=new, buildid='8', whiteouts=('data/b.dat',))
        self.override('guest', changed, listed=new, buildid='8', whiteouts=('data/b.dat',), merged=False)
        return new

    def test_the_other_accounts_identical_copy_of_an_update_goes_and_its_own_files_stay(self):
        self.updated_by_both()
        guest = self.upper('guest') / 'steamapps/common/Game'
        (guest / 'data/a.dat').write_bytes(b'AAAA')                 # same size as the store's, other bytes: its own
        (guest / 'save').mkdir()
        (guest / 'save/slot1').write_bytes(b'hers')
        self.assertEqual(self.share('main')['kind'], 'update')
        self.unmerge()
        result = self.commit()
        self.assertEqual((result['committed'], result['warnings']), (['100'], []))
        self.assertEqual((self.common() / 'bin/game').read_bytes(), b'#!game v2')
        left = sorted(str(path.relative_to(guest)) for path in guest.rglob('*') if not path.is_dir())
        self.assertEqual(left, ['data/a.dat', 'save/slot1'])
        self.assertEqual((guest / 'data/a.dat').read_bytes(), b'AAAA')
        self.assertFalse((guest / 'bin').exists())                  # emptied by the comparison: gone
        state = self.read_acf(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((state['StateFlags'], state['buildid']), ('2', '8'))

    def test_a_library_that_held_nothing_else_loses_the_folder(self):
        self.updated_by_both()
        self.share('main')
        self.unmerge()
        self.commit()
        self.assertFalse((self.upper('guest') / 'steamapps/common/Game').exists())

    def test_an_opaque_folder_hides_the_store_and_keeps_its_files(self):
        self.updated_by_both()
        guest = self.upper('guest') / 'steamapps/common/Game'
        real = tidy._opaque
        with patch.object(tidy, '_opaque', lambda fd: real(fd) or os.path.samefile(f'/proc/self/fd/{fd}', guest / 'bin')):
            self.share('main')
            self.unmerge()
            self.commit()
        self.assertEqual((guest / 'bin/game').read_bytes(), b'#!game v2')
        self.assertFalse((guest / 'data/c.dat').exists())

    def test_a_link_or_a_hard_linked_file_in_the_layer_is_not_compared(self):
        self.updated_by_both()
        guest = self.upper('guest') / 'steamapps/common/Game'
        os.unlink(guest / 'data/c.dat')
        os.symlink(self.common() / 'data/c.dat', guest / 'data/c.dat')
        os.link(guest / 'bin/game', self.base / 'second-name')
        self.share('main')
        self.unmerge()
        self.commit()
        self.assertTrue(os.path.islink(guest / 'data/c.dat'))
        self.assertTrue((guest / 'bin/game').exists())


class LetterCase(Env):
    """Seen on the Deck: Thronefall lives in `Thronefall_Data`, its list says `thronefall_Data`."""
    LISTED = {'bin/game': b'#!game', 'game_Data/level1': b'one', 'game_Data/sub/level2': b'two'}

    def other_case(self, user, **options):
        on_disk = {'bin/game': b'#!game', 'Game_Data/level1': b'one', 'Game_Data/Sub/level2': b'two'}
        return self.install(user, on_disk, manifest_files=self.LISTED, **options)

    def test_a_game_in_another_letter_case_goes_into_the_store_whole_and_as_it_is_spelled(self):
        folder = self.other_case('main')
        summary = self.share('main')
        self.assertEqual((summary['files'], summary['private']), (3, 0))
        self.assertEqual(self.commit()['committed'], ['100'])
        store = self.common()
        self.assertEqual(sorted(str(path.relative_to(store)) for path in store.rglob('*') if path.is_file()),
                         ['Game_Data/Sub/level2', 'Game_Data/level1', 'bin/game'])
        self.assertFalse((store / 'game_Data').exists())
        self.assertFalse(folder.exists())
        kept = self.upper('main') / 'steamapps/common/Game'
        self.assertEqual([path for path in kept.rglob('*') if not path.is_dir()], [])      # no file stayed behind
        self.assertFalse((kept / 'game_Data').exists())
        self.assertEqual(tidy.stored(self.layout)['100']['files'], ['Game_Data/Sub/level2', 'Game_Data/level1', 'bin/game'])

    def test_a_second_copy_spelled_like_the_store_joins_and_one_spelled_differently_keeps_to_itself(self):
        self.other_case('main')
        self.share('main')
        self.commit()
        self.install('guest', self.LISTED)                              # spelled as listed: not as the store
        with self.assertRaisesRegex(TidyError, 'spelled differently'):
            self.share('guest')
        shutil.rmtree(self.steam('guest') / 'steamapps/common/Game')
        self.other_case('guest')
        self.assertEqual(self.share('guest')['kind'], 'join')

    def test_two_spellings_of_one_file_are_refused(self):
        folder = self.other_case('main')
        (folder / 'GAME_DATA').mkdir()
        (folder / 'GAME_DATA/level1').write_bytes(b'one')
        with self.assertRaisesRegex(TidyError, 'more than one spelling'):
            self.share('main')


class ManyFiles(Env):
    def test_the_record_of_a_game_with_tens_of_thousands_of_files_is_written_small_and_read_back(self):
        files = {f'data/{number:05d}.bin': b'x' for number in range(3000)}
        self.install('main', files)
        self.share('main')
        record = self.root / 'pending/100.json'
        self.assertLess(record.stat().st_size, 3000 * 220)          # one line, no indentation
        with patch.object(tidy, 'RECORD_LIMIT', record.stat().st_size - 1):
            dropped = self.commit()['dropped']
        self.assertEqual(len(dropped), 1)
        self.assertIn('not a readable file of at most', dropped[0]['reason'])
        self.share('main')
        self.assertEqual(self.commit()['committed'], ['100'])
        self.assertEqual(len(tidy.stored(self.layout)['100']['files']), 3000)


class Listing(Env):
    def test_shareable_names_every_installed_game_with_its_reason(self):
        self.standard()
        self.install('main', {'a': b'1'}, appid='101', flags='1026', installdir='Half')
        self.install('main', {'a': b'1'}, appid='102', installdir='NoDepot', depot_manifest=False, depots=((1002, 999),))
        listing = {item['appid']: item for item in tidy.shareable(self.layout, 'main', USERS)}
        self.assertEqual(sorted(listing), ['100', '101', '102'])
        self.assertIsNone(listing['100']['reason'])
        self.assertEqual((listing['100']['name'], listing['100']['source'], listing['100']['library']),
                         ('Test Game', 'library', str(self.steam('main'))))
        self.assertIn('not fully installed', listing['101']['reason'])
        self.assertIn('depot manifests', listing['102']['reason'])
        self.assertEqual(tidy.pending(self.layout), [])      # a listing writes nothing


class Removal(Env):
    def test_a_game_every_account_uninstalled_is_deleted_with_its_whiteouts(self):
        self.shared_game()
        make_whiteout(self.upper('main') / 'steamapps/appmanifest_100.acf')
        shutil.rmtree(self.upper('main') / 'steamapps/common/Game')
        make_whiteout(self.upper('main') / 'steamapps/common/Game')
        make_whiteout(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        result = self.commit()
        self.assertEqual(result['removed'], ['100'])
        self.assertFalse(self.common().exists())
        self.assertFalse((self.root / 'store/steamapps/appmanifest_100.acf').exists())
        self.assertEqual(tidy.stored(self.layout), {})
        for user in USERS:
            self.assertFalse(os.path.lexists(self.upper(user) / 'steamapps/appmanifest_100.acf'))
        self.assertFalse(os.path.lexists(self.upper('main') / 'steamapps/common/Game'))

    def test_an_account_that_had_files_of_its_own_loses_the_whiteouts_and_keeps_those_files(self):
        # Seen on the Deck: Steam's uninstall leaves the folder with what the game wrote, and one whiteout for every
        # file it shipped. With the game gone they would hide the files if it is ever shared again.
        self.shared_game()
        game = self.upper('main') / 'steamapps/common/Game'
        make_whiteout(self.upper('main') / 'steamapps/appmanifest_100.acf')
        for name in ('bin/game', 'data/a.dat', 'data/b.dat'):
            make_whiteout(game / name)
        make_whiteout(self.upper('guest') / 'steamapps/appmanifest_100.acf')
        make_whiteout(self.upper('guest') / 'steamapps/common/Game/data/a.dat')
        self.assertEqual(self.commit()['removed'], ['100'])
        self.assertEqual(sorted(str(path.relative_to(game)) for path in game.rglob('*') if not path.is_dir()),
                         ['config.ini', 'save/slot1'])
        self.assertEqual((game / 'save/slot1').read_bytes(), b'my progress')
        self.assertFalse(os.path.lexists(self.upper('guest') / 'steamapps/common/Game'))

    def test_one_account_still_showing_the_game_keeps_it(self):
        self.shared_game()
        make_whiteout(self.upper('main') / 'steamapps/appmanifest_100.acf')
        self.assertEqual(self.commit()['removed'], [])
        self.assertTrue(self.common().exists())

    def test_no_accounts_means_nothing_is_removed(self):
        self.shared_game()
        self.assertEqual(tidy.commit(self.layout, [])['removed'], [])
        self.assertTrue(self.common().exists())


class Interrupted(Env):
    """A commit that dies at each point is finished or rolled back by the next one."""

    def crash_with(self, name, after=0):
        original = getattr(tidy, name)
        calls = []

        def wrapper(*args, **kwargs):
            if len(calls) >= after:
                raise Crash(name)
            calls.append(1)
            return original(*args, **kwargs)
        return patch.object(tidy, name, wrapper)

    def interrupted(self, name, after=0):
        folder = self.standard()
        self.share('main')
        before = snapshot(folder)
        with self.crash_with(name, after), self.assertRaises(Crash):
            self.commit()
        return folder, before

    def assert_rolled_back(self, folder, before):
        result = self.commit()
        self.assertEqual((result['committed'], [item['appid'] for item in result['dropped']]), ([], ['100']))
        self.assertEqual(snapshot(folder), before)
        self.assertFalse(self.common().exists())
        self.assertEqual(tidy.pending(self.layout), [])
        self.assertEqual(os.listdir(self.root / 'staging'), [])

    def assert_finished(self, folder):
        result = self.commit()
        self.assertEqual((result['committed'], result['dropped']), (['100'], []))
        self.assertEqual((self.common() / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual(sorted(os.listdir(folder)), ['config.ini', 'save'])     # what the account keeps stays
        self.assertEqual(self.read_acf(self.root / 'store/steamapps/appmanifest_100.acf')['StateFlags'], '4')
        self.assertTrue((self.upper('guest') / 'steamapps/appmanifest_100.acf').exists())
        self.assertEqual(tidy.pending(self.layout), [])
        self.assertEqual(os.listdir(self.root / 'staging'), [])

    def journal_phase(self):
        return json.loads((self.root / 'staging/100/journal.json').read_text())['phase']

    def test_dying_while_collecting_rolls_back(self):
        folder, before = self.interrupted('_move_in', after=2)
        self.assertEqual(self.journal_phase(), 'collecting')
        self.assertTrue((self.root / 'staging/100/files').is_dir())
        self.assert_rolled_back(folder, before)

    def test_dying_while_preparing_rolls_back(self):
        folder, before = self.interrupted('_prepare_staged')
        self.assertEqual(self.journal_phase(), 'preparing')
        self.assert_rolled_back(folder, before)

    def test_dying_before_the_first_file_is_published_finishes(self):
        folder, _before = self.interrupted('_publish_files')
        self.assertEqual(self.journal_phase(), 'publishing')
        self.assert_finished(folder)

    def test_dying_after_the_files_are_published_finishes(self):
        folder, _before = self.interrupted('_write_store_records')
        self.assertEqual((self.common() / 'data/a.dat').read_bytes(), b'aaaa')
        self.assert_finished(folder)

    def test_dying_after_the_store_manifest_finishes(self):
        folder, _before = self.interrupted('_cleanup_source')
        self.assertTrue((self.root / 'apps/100.json').exists())
        self.assert_finished(folder)

    def test_dying_before_the_validation_manifests_finishes(self):
        folder, _before = self.interrupted('_validation')
        self.assertEqual(sorted(os.listdir(folder)), ['config.ini', 'save'])
        self.assert_finished(folder)

    def test_a_rollback_that_cannot_put_a_file_back_keeps_it_and_the_journal(self):
        folder, _before = self.interrupted('_move_in', after=2)
        (folder / 'bin').mkdir(exist_ok=True)
        (folder / 'bin/game').write_bytes(b'someone made a new one')
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertTrue(any('bin/game' in warning for warning in result['warnings']))
        self.assertEqual((folder / 'bin/game').read_bytes(), b'someone made a new one')
        self.assertEqual((self.root / 'staging/100/files/bin/game').read_bytes(), b'#!game')
        self.assertTrue((self.root / 'staging/100/journal.json').exists())


class Leaving(Env):
    def test_a_stored_game_goes_back_to_the_account_that_shared_it(self):
        folder = self.shared_game()
        private = self.upper('main') / 'steamapps/common/Game'
        self.assertEqual(private, folder)
        (private / 'data').mkdir()
        (private / 'data/new.dat').write_bytes(b'n')
        make_whiteout(private / 'data/b.dat')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual((result['restored'], result['kept']), (['100'], []))
        self.assertEqual((folder / 'bin/game').read_bytes(), b'#!game')
        self.assertEqual(stat.S_IMODE(os.stat(folder / 'bin/game').st_mode), 0o755)
        self.assertEqual((folder / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual((folder / 'data/new.dat').read_bytes(), b'n')
        self.assertEqual((folder / 'config.ini').read_bytes(), b'changed!')
        self.assertEqual((folder / 'save/slot1').read_bytes(), b'my progress')
        self.assertFalse(os.path.lexists(folder / 'data/b.dat'))            # hidden by the account, and the whiteout is gone
        self.assertEqual(os.listdir(self.root / 'store/steamapps/common'), [])
        self.assertEqual(tidy.stored(self.layout), {})
        self.assertFalse((self.root / 'store/steamapps/appmanifest_100.acf').exists())
        manifest = self.read_acf(self.steam('main') / 'steamapps/appmanifest_100.acf')
        self.assertEqual(manifest['StateFlags'], '4')
        # Guest's manifest (the request to validate) is not for a game it does not have whole: set aside, not deleted.
        self.assertFalse(os.path.lexists(self.steam('guest') / 'steamapps/appmanifest_100.acf'))
        self.assertTrue((self.root / 'conflicts/guest/left-over/steamapps/appmanifest_100.acf').exists())
        self.assertEqual(result['set_aside'], [{'user': 'guest', 'name': 'left-over/steamapps/appmanifest_100.acf'}])
        self.assertEqual(result['conflicts'], {'guest': ['left-over/steamapps']})

    def test_a_game_folder_the_account_has_already_keeps_its_files_and_gets_the_rest(self):
        folder = self.shared_game()
        (folder / 'mine').write_bytes(b'1')
        (folder / 'data').mkdir(exist_ok=True)
        (folder / 'data/a.dat').write_bytes(b'my own a')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual((result['restored'], result['kept']), (['100'], []))
        self.assertEqual((folder / 'mine').read_bytes(), b'1')
        self.assertEqual((folder / 'data/a.dat').read_bytes(), b'my own a')       # the account's file wins
        self.assertEqual((folder / 'data/b.dat').read_bytes(), b'bbbbbb')
        self.assertEqual(os.listdir(self.root / 'store/steamapps/common'), [])
        self.assertEqual(os.listdir(self.root / 'staging'), [])

    def test_a_folder_of_another_kind_where_the_game_goes_keeps_the_game_in_the_store(self):
        self.shared_game()
        shutil.rmtree(self.steam('main') / 'steamapps/common/Game')
        (self.steam('main') / 'steamapps/common/Game').write_bytes(b'a file')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], [])
        self.assertEqual(result['kept'][0]['appid'], '100')
        self.assertIn('Game', result['kept'][0]['reason'])
        self.assertEqual((self.common() / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertIn('100', tidy.stored(self.layout))

    def test_a_manifest_the_account_has_already_stays_as_its_steam_wrote_it(self):
        self.shared_game()
        (self.steam('main') / 'steamapps/appmanifest_100.acf').write_text(self.acf(buildid='9'))
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], ['100'])
        self.assertEqual(self.read_acf(self.steam('main') / 'steamapps/appmanifest_100.acf')['buildid'], '9')

    def test_if_the_sharer_uninstalled_the_game_goes_to_the_next_account(self):
        self.shared_game()
        make_whiteout(self.upper('main') / 'steamapps/appmanifest_100.acf')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], ['100'])
        self.assertEqual((self.steam('guest') / 'steamapps/common/Game/data/a.dat').read_bytes(), b'aaaa')
        manifest = self.read_acf(self.steam('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((manifest['StateFlags'], manifest['FullValidateBeforeNextUpdate']), ('2', '1'))
        # Main uninstalled it: what it kept of its own (saves) is set aside, and no whiteout is left.
        self.assertFalse(os.path.lexists(self.steam('main') / 'steamapps/appmanifest_100.acf'))
        self.assertFalse((self.steam('main') / 'steamapps/common/Game').exists())
        self.assertEqual((self.root / 'conflicts/main/left-over/steamapps/common/Game/save/slot1').read_bytes(),
                         b'my progress')

    def test_no_whiteout_of_the_design_is_left_in_any_library_when_leaving_is_done(self):
        self.shared_game()
        other = self.steam('guest') / 'steamapps/common/Other'
        other.mkdir(parents=True)
        make_whiteout(other / 'gone.dat')                     # a whiteout for a file of a game that is not stored
        make_whiteout(self.steam('guest') / 'steamapps/appmanifest_555.acf')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['kept'], [])
        for user in USERS:
            for place in (self.steam(user) / 'steamapps').rglob('*'):
                self.assertFalse(fake_whiteout(os.lstat(place)) and place.name != 'libraryfolders.vdf', place)
        self.assertEqual(sorted(item.split(': ')[1] for item in result['whiteouts_dropped']),
                         ['steamapps/appmanifest_555.acf'])

    def test_the_stash_comes_back_before_the_games_are_given_back(self):
        folder = self.shared_game()
        stash = self.root / 'stash/main/steamapps'
        stash.mkdir(parents=True)
        (stash / 'appmanifest_100.acf').write_text(self.acf(buildid='9'))
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], ['100'])
        self.assertEqual(self.read_acf(self.steam('main') / 'steamapps/appmanifest_100.acf')['buildid'], '9')
        self.assertFalse((self.root / 'stash/main').exists())
        self.assertTrue(folder.is_dir())


    def test_a_game_nobody_shows_any_more_is_deleted(self):
        self.shared_game()
        for user in USERS:
            make_whiteout(self.upper(user) / 'steamapps/appmanifest_100.acf')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual((result['restored'], result['removed']), ([], ['100']))
        self.assertFalse(self.common().exists())

    def test_a_steam_folder_that_is_a_link_is_not_followed(self):
        self.shared_game()
        common = self.steam('main') / 'steamapps/common'
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        if common.exists():
            shutil.rmtree(common)
        os.symlink(elsewhere, common)
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], [])
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertTrue((self.common() / 'data/a.dat').exists())
        self.assertFalse((self.steam('main') / 'steamapps/appmanifest_100.acf').exists())


class Links(Env):
    def test_a_library_folder_that_is_a_link_gets_no_validation_manifest_through_it(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        shutil.rmtree(self.steam('guest') / 'steamapps')
        (self.steam('guest') / 'steamapps').symlink_to(elsewhere)
        self.standard()
        self.share('main')
        result = self.commit()
        self.assertEqual(result['committed'], ['100'])
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertTrue(any('guest' in warning for warning in result['warnings']))

    def test_a_link_where_the_leftovers_go_is_not_followed(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (self.upper('main') / 'steamapps/common').symlink_to(elsewhere)
        self.list_libraries('main', self.own_library('main'))
        folder = self.install('main', {'a': b'1'}, library=self.own_library('main'))
        (folder / 'save').mkdir()
        (folder / 'save/slot1').write_bytes(b'x')
        self.share('main')
        result = self.commit()
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(result['committed'], [])
        self.assertTrue(any('100' in warning for warning in result['warnings']))
        self.assertTrue((folder / 'save/slot1').exists())


class Command(Env):
    def run_cli(self, *arguments):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tidy.main(list(arguments))
        return code, json.loads(out.getvalue()) if out.getvalue() else None

    def test_the_command_line_prints_json_for_every_command(self):
        self.standard()
        root = ['--root', str(self.root)]
        users = ['--users', 'main,guest']
        code, listing = self.run_cli('shareable', *root, *users, '--user', 'main')
        self.assertEqual((code, listing[0]['appid']), (0, '100'))
        code, summary = self.run_cli('share', *root, *users, '--user', 'main', '--app', '100')
        self.assertEqual((code, summary['kind']), (0, 'new'))
        self.assertEqual(self.run_cli('pending', *root)[1][0]['appid'], '100')
        self.assertEqual(self.run_cli('commit', *root, *users)[1]['committed'], ['100'])
        self.assertEqual(self.run_cli('restore', *root, *users)[1]['restored'], ['100'])

    def test_a_refusal_is_a_message_and_a_failing_exit_code(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = tidy.main(['share', '--root', str(self.root), '--users', 'main,guest', '--user', 'main',
                              '--app', '5'])
        self.assertEqual(code, 1)
        self.assertIn('not installed', err.getvalue())


class OwnersAndFolders(Env):
    def test_a_game_folder_that_is_not_the_accounts_own_is_never_worked_on(self):
        folder = self.standard()
        self.share('main')
        before = snapshot(folder)
        # The account named this library in its list, but the folders belong to someone else.
        self.entries['main'] = SimpleNamespace(**{**vars(self.entries['main']), 'pw_uid': os.getuid() + 1})
        result = self.commit()
        self.assertEqual(result['committed'], [])
        self.assertIn('does not belong', result['dropped'][0]['reason'])
        self.assertEqual(snapshot(folder), before)
        self.assertTrue((self.steam('main') / 'steamapps/appmanifest_100.acf').exists())
        self.assertFalse(self.common().exists())

    def test_folders_the_depot_lists_are_in_the_store_even_when_empty(self):
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa'}
        folder = self.install('main', files, executable=('bin/game',))
        entries = [builders.file_entry(name, data) for name, data in files.items()]
        entries += [builders.entry('logs', 0, depot.DIRECTORY), builders.entry('mods/user', 0, depot.DIRECTORY)]
        (self.steam('main') / 'depotcache' / depot.manifest_name(1001, 555)).write_bytes(
            builders.build(entries, depot_id=1001, gid=555))
        (folder / 'logs').mkdir()
        self.share('main')
        self.assertEqual(self.commit()['committed'], ['100'])
        self.assertTrue((self.common() / 'logs').is_dir())
        self.assertTrue((self.common() / 'mods/user').is_dir())
        # The account's own empty copy of a folder the store has is not kept.
        self.assertFalse((self.upper('main') / 'steamapps/common/Game/logs').exists())

    def test_a_restore_that_stopped_with_the_game_in_staging_goes_on_from_there(self):
        folder = self.shared_game()
        stage = self.root / 'staging/100'
        stage.mkdir(parents=True)
        os.rename(self.common(), stage / 'Game')
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual((result['restored'], result['kept']), (['100'], []))
        self.assertEqual((folder / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual((folder / 'save/slot1').read_bytes(), b'my progress')
        self.assertFalse(stage.exists())

    class Died(BaseException):
        """The machine stopped here: nothing after this line of the program ran."""

    def die_in(self, name, *, after=True):
        """Give the games back, and stop the program at one function of the tool (after it ran, or before)."""
        real = getattr(tidy, name)

        def stopped(*args, **kwargs):
            if after:
                real(*args, **kwargs)
            raise self.Died(name)
        with patch.object(tidy, name, stopped), self.assertRaises(self.Died):
            tidy.restore_all(self.layout, USERS)

    def assert_given_back(self, folder, result, restored=('100',)):
        self.assertEqual((result['restored'], result['kept']), (list(restored), []), result)
        self.assertEqual((folder / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual((folder / 'bin/game').read_bytes(), b'#!game')
        self.assertEqual((folder / 'config.ini').read_bytes(), b'changed!')
        self.assertEqual((folder / 'save/slot1').read_bytes(), b'my progress')
        self.assertTrue((self.steam('main') / 'steamapps/appmanifest_100.acf').is_file())
        self.assertEqual(tidy.stored(self.layout), {})
        self.assertFalse((self.root / 'store/steamapps/appmanifest_100.acf').exists())
        self.assertFalse(self.common().exists())
        self.assertFalse((self.root / 'staging/100').exists())
        self.assertEqual(list((self.root / 'restoring').glob('*')) if (self.root / 'restoring').exists() else [], [])

    def test_a_restore_that_stopped_right_after_the_files_went_back_is_finished_by_the_next_one(self):
        # The files are in the account's library already, the store and staging are empty of them, and the account
        # has no manifest yet. The next run used to say "the store has no files for it" and keep the game for ever.
        folder = self.shared_game()
        self.die_in('_restore_files')
        self.assertTrue((folder / 'data/a.dat').is_file())
        self.assertFalse(self.common().exists())
        self.assertEqual(sorted(tidy.stored(self.layout)), ['100'])
        self.assert_given_back(folder, tidy.restore_all(self.layout, USERS))

    def test_a_restore_that_stopped_at_any_later_step_is_finished_by_the_next_one(self):
        for name, after in (('_write_manifest_as_user', True), ('_set_aside_leftovers', False),
                            ('_set_aside_leftovers', True), ('_end_restoring', False)):
            with self.subTest(stopped=name, after=after):
                self.tearDown()
                self.setUp()
                folder = self.shared_game()
                self.die_in(name, after=after)
                # Stopped before its last step, the game is whole already and only the restore's note is left:
                # the next run has no game to give back and takes the note away.
                done = name == '_end_restoring'
                self.assert_given_back(folder, tidy.restore_all(self.layout, USERS), restored=() if done else ('100',))

    def test_a_restore_goes_back_to_the_account_it_began_with(self):
        # What the first attempt chose stays chosen: the answer to "who gets it" must not change between two halves.
        folder = self.shared_game()
        self.die_in('_restore_files')
        app = tidy.stored(self.layout)['100']
        with patch.object(tidy, '_restore_target', return_value='guest'):
            result = tidy.restore_all(self.layout, USERS)
        self.assert_given_back(folder, result)
        self.assertEqual(app['shared_by'], 'main')

    def test_a_game_with_no_files_anywhere_is_still_kept_with_the_reason(self):
        # Without a begun restore, a store that has no files for a game is a fault to look at, as it always was.
        self.shared_game()
        shutil.rmtree(self.common())
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], [])
        self.assertIn('the store has no files for it', result['kept'][0]['reason'])


class DiskEnv(Env):
    """A disk store next to the primary: a second mount point on the same filesystem (every check is relative to the
    store's own root, so one filesystem is enough here)."""

    def setUp(self):
        super().setUp()
        self.mount = self.base / 'usb'
        self.layout.media_root = self.base / 'media'
        for patcher in (patch.object(tidy, '_device', self.device), patch.object(tidy.store, 'device_of', self.device)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.disk = tidy.store.Layout.for_disk(self.layout, self.mount)
        (self.disk.root / 'store/steamapps/common').mkdir(parents=True)
        for name in USERS:
            self.disk.side(name).base.mkdir(parents=True)
        tidy.store.write_disks(self.layout, [{'root': str(self.disk.root), 'mount': str(self.mount)}])

    def device(self, path):
        """Everything below the mount point is on a second filesystem; a path that is not there raises as stat does."""
        os.stat(path)
        return 2 if str(path).startswith((str(self.mount), str(self.layout.media_root))) else 1

    def usb_library(self, user):
        return self.mount / f'Library-{user}'

    def shared_on_disk(self, listed=('main',)):
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa'}
        self.install('main', files, library=self.usb_library('main'), executable=('bin/game',))
        (self.usb_library('guest') / 'steamapps').mkdir(parents=True, exist_ok=True)
        self.relist(USERS)
        tidy.share(self.disk, 'main', '100', USERS)
        self.assertEqual(tidy.commit(self.disk, USERS)['committed'], ['100'])
        self.relist(listed)
        return self.usb_library('main') / 'steamapps/common/Game'

    def relist(self, listed):
        """Which accounts have a library on the disk: the list names it, and the store's side knows it."""
        for user in USERS:
            self.list_libraries(user, *([self.usb_library(user)] if user in listed else []))
            if user in listed:
                self.disk.libraries[user] = (self.usb_library(user), self.usb_library(user))
                self.mounted.add(str(self.usb_library(user) / 'steamapps'))
            else:
                self.disk.libraries.pop(user, None)


class DiskStores(DiskEnv):
    def test_a_game_on_the_disk_goes_into_the_store_of_the_disk(self):
        self.shared_on_disk()
        self.assertTrue((self.disk.common / 'Game/data/a.dat').exists())
        self.assertEqual(os.listdir(self.layout.common), [])
        self.assertEqual(sorted(tidy.stored(self.disk)), ['100'])

    def test_it_goes_back_to_the_library_of_the_account_that_shared_it_on_that_disk(self):
        folder = self.shared_on_disk()
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual((result['restored'], result['kept']), (['100'], []))
        self.assertNotIn('given', result)
        self.assertEqual((folder / 'data/a.dat').read_bytes(), b'aaaa')
        self.assertTrue((self.usb_library('main') / 'steamapps/appmanifest_100.acf').exists())
        self.assertFalse((self.steam('main') / 'steamapps/common/Game').exists())
        self.assertEqual(os.listdir(self.disk.common), [])

    def test_when_the_sharer_uninstalled_it_the_next_account_with_a_library_there_gets_it(self):
        self.shared_on_disk(listed=('main', 'guest'))
        make_whiteout(self.usb_library('main') / 'steamapps/appmanifest_100.acf')
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual((result['restored'], result['kept']), (['100'], []))
        self.assertNotIn('given', result)       # the account that _restore_target named is the one that got it
        self.assertTrue((self.usb_library('guest') / 'steamapps/common/Game/data/a.dat').exists())

    def test_an_account_without_a_library_there_is_passed_over_even_if_the_next_has_a_whiteout(self):
        self.shared_on_disk(listed=('guest',))
        make_whiteout(self.usb_library('guest') / 'steamapps/appmanifest_100.acf')
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual(result['restored'], ['100'])
        self.assertEqual(result['given'], [{'appid': '100', 'to': 'guest', 'instead_of': 'main'}])
        self.assertTrue((self.usb_library('guest') / 'steamapps/common/Game/data/a.dat').exists())
        self.assertTrue((self.usb_library('guest') / 'steamapps/appmanifest_100.acf').exists())

    def test_without_any_library_there_the_game_is_kept_and_the_reason_says_so(self):
        self.shared_on_disk(listed=())
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual(result['restored'], [])
        self.assertEqual(result['kept'], [{'appid': '100', 'reason':
                                           'no account has a Steam library on this disk to give it back to'}])
        self.assertTrue((self.disk.common / 'Game/data/a.dat').exists())
        self.assertIn('100', tidy.stored(self.disk))

    def test_a_game_that_every_account_uninstalled_is_deleted_as_on_the_primary(self):
        self.shared_on_disk(listed=('main', 'guest'))
        for user in USERS:
            make_whiteout(self.usb_library(user) / 'steamapps/appmanifest_100.acf')
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual((result['restored'], result['removed']), ([], ['100']))
        self.assertFalse((self.disk.common / 'Game').exists())

    def test_a_library_on_the_disk_that_is_not_the_accounts_own_is_not_used(self):
        self.shared_on_disk()
        self.entries['main'] = SimpleNamespace(**{**vars(self.entries['main']), 'pw_uid': os.getuid() + 1})
        result = tidy.restore_all(self.disk, USERS)
        self.assertEqual(result['restored'], [])
        self.assertIn('does not belong', result['kept'][0]['reason'])
        self.assertTrue((self.disk.common / 'Game/data/a.dat').exists())

    def test_the_primary_still_gives_games_back_to_the_default_steam_folder(self):
        folder = self.shared_game()
        result = tidy.restore_all(self.layout, USERS)
        self.assertEqual(result['restored'], ['100'])
        self.assertNotIn('given', result)
        self.assertTrue((folder / 'data/a.dat').exists())


class LibrariesAndDisks(DiskEnv):
    def test_a_library_on_another_filesystem_is_not_listed_as_a_source_and_says_nothing(self):
        self.install('main', {'a': b'1'}, library=self.usb_library('main'))
        self.install('main', {'a': b'1'}, appid='101', installdir='Near')
        self.list_libraries('main', self.usb_library('main'))
        entry = self.entries['main']
        self.assertEqual([item[0] for item in tidy._candidates(self.layout, 'main', entry)], ['101'])
        self.assertEqual([item['appid'] for item in tidy.shareable(self.layout, 'main', USERS)], ['101'])
        with self.assertRaisesRegex(TidyError, 'not installed'):
            tidy.share(self.layout, 'main', '100', USERS)

    def test_the_disk_store_lists_only_the_games_on_its_own_filesystem(self):
        self.install('main', {'a': b'1'}, library=self.usb_library('main'))
        self.install('main', {'a': b'1'}, appid='101', installdir='Near')
        self.list_libraries('main', self.usb_library('main'))
        entry = self.entries['main']
        self.assertEqual([item[0] for item in tidy._candidates(self.disk, 'main', entry)], ['100'])

    def test_a_game_on_another_filesystem_is_still_refused_by_the_inspection_itself(self):
        self.install('main', {'a': b'1'})
        (self.mount / 'steamapps').mkdir(parents=True)
        with self.assertRaisesRegex(TidyError, 'on another disk'):
            tidy._inspect(self.layout, 'main', self.entries['main'], '100', 'library', self.mount, {}, {})

    def test_no_library_folder_of_any_store_is_taken_for_an_account_library(self):
        elsewhere = self.base / 'Elsewhere'
        self.list_libraries('main', self.layout.side('main').retired.library, self.disk.side('main').retired.library, elsewhere)
        entry = self.entries['main']
        self.assertEqual(tidy._libraries(self.layout, entry, 'main'), [self.steam('main'), elsewhere])
        self.assertEqual(tidy._libraries(self.disk, entry, 'main'), [self.steam('main'), elsewhere])

    def test_a_listed_disk_that_is_not_connected_still_counts_as_the_stores_own(self):
        other = tidy.store.Layout.for_disk(self.layout, self.base / 'gone')
        tidy.store.write_disks(self.layout, [{'root': str(self.disk.root), 'mount': str(self.mount)},
                                             {'root': str(other.root), 'mount': str(other.mount_point)}])
        self.list_libraries('main', other.side('main').retired.library)
        self.assertEqual(tidy._libraries(self.layout, self.entries['main'], 'main'), [self.steam('main')])

    def test_account_libraries_names_the_accounts_own_folders_with_their_device(self):
        with_apps = self.base / 'WithApps'
        (with_apps / 'steamapps').mkdir(parents=True)
        (self.base / 'NoApps').mkdir()
        self.list_libraries('main', self.layout.side('main').retired.library, self.disk.side('main').retired.library,
                            with_apps, self.base / 'NoApps', self.base / 'Missing')
        found = tidy.account_libraries(self.layout, 'main', USERS)
        self.assertEqual(found, [{'path': str(self.steam('main')), 'dev': 1}, {'path': str(with_apps), 'dev': 1}])
        self.assertEqual(tidy.account_libraries(self.disk, 'main', USERS), found)
        with self.assertRaisesRegex(TidyError, 'not an account'):
            tidy.account_libraries(self.layout, 'someone', USERS)


class LibraryGivenOnADisk(DiskEnv):
    """An account with no library on a disk is given one; the account reaches it by a bind, root works at the real folder
    (a rename between two mounts is refused). Here the bind is a second name that the test moves the folder to."""

    def setUp(self):
        super().setUp()
        self.visible, self.real, _binds = self.disk.provisioned('guest')

    def test_the_places_of_a_library_that_is_given(self):
        self.assertEqual(self.visible, self.base / 'media/guest/usb/SteamLibrary')
        self.assertEqual(self.real, self.disk.root / 'accounts/guest/SteamLibrary')
        self.assertEqual(self.disk.provisioned('guest')[2], (self.disk.root / 'accounts/guest', self.base / 'media/guest/usb'))
        self.assertIsNone(self.layout.provisioned('guest'))
        side = self.disk.side('guest', self.visible, self.real)
        self.assertEqual((side.library, side.real_library), (self.visible, self.real))
        self.assertEqual(side.steamapps, self.visible / 'steamapps')
        self.assertEqual(side.real_steamapps, self.real / 'steamapps')
        self.assertEqual(side.root_path(self.visible / 'steamapps/common/Game'), self.real / 'steamapps/common/Game')
        self.assertEqual(side.root_path('/elsewhere/x'), Path('/elsewhere/x'))
        plain = self.disk.side('main', self.usb_library('main'))
        self.assertEqual(plain.root_path(self.usb_library('main') / 'steamapps'), self.usb_library('main') / 'steamapps')

    def test_the_account_shares_through_the_path_it_reaches_and_root_commits_at_the_real_folder(self):
        # Guest installs a game into the library it was given; its Steam sees it below the media folder.
        side = self.disk.side('guest', self.visible, self.real)
        self.disk.libraries['guest'] = (self.visible, self.real)
        self.list_libraries('guest', self.visible)
        self.list_libraries('main', self.usb_library('main'))
        (self.usb_library('main') / 'steamapps').mkdir(parents=True)
        self.disk.libraries['main'] = (self.usb_library('main'), self.usb_library('main'))
        files = {'bin/game': b'#!game', 'data/a.dat': b'aaaa'}
        self.install('guest', files, library=self.visible)
        (self.visible / 'steamapps/common/Game/save.dat').write_bytes(b'hers')
        summary = tidy.share(self.disk, 'guest', '100', USERS)
        self.assertEqual((summary['kind'], summary['files'], summary['private']), ('new', 2, 1))
        record = json.loads((self.disk.root / 'pending/100.json').read_text())
        self.assertEqual(record['library'], str(self.visible))
        # The slot: root reaches the same folder at the real path (the bind is not there for it).
        self.real.parent.mkdir(parents=True)
        shutil.move(self.visible, self.real)
        result = tidy.commit(self.disk, USERS)
        self.assertEqual((result['committed'], result['dropped']), (['100'], []))
        self.assertEqual((self.disk.common / 'Game/data/a.dat').read_bytes(), b'aaaa')
        self.assertEqual((self.real / 'steamapps/common/Game/save.dat').read_bytes(), b'hers')
        self.assertFalse((self.real / 'steamapps/appmanifest_100.acf').exists())
        state = self.read_acf(self.usb_library('main') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((state['StateFlags'], state['FullValidateBeforeNextUpdate']), ('2', '1'))

    def test_what_the_store_holds_is_read_before_the_accounts_step_and_not_inside_it(self):
        # Seen on the Deck: Guest cannot walk to a disk store's folders, took every stored game for new and queued it.
        self.shared_on_disk(listed=('main', 'guest'))
        # The commit left Guest a validation manifest; its Steam validated and wrote the game as installed.
        (self.usb_library('guest') / 'steamapps/appmanifest_100.acf').write_text(self.acf())
        self.depot_manifest('guest', {'bin/game': b'#!game', 'data/a.dat': b'aaaa'}, executable=('bin/game',))
        (self.usb_library('guest') / 'steamapps/common/Game').mkdir(parents=True)
        real = tidy._as_user

        def account_cannot_see_the_store(entry, work):
            with patch.object(tidy, '_stored_one', lambda *a: None), patch.object(tidy, 'stored', lambda *a: {}):
                return real(entry, work)
        with patch.object(tidy, '_as_user', account_cannot_see_the_store):
            listing = tidy.shareable(self.disk, 'guest', USERS)
            self.assertEqual([(item['appid'], item['reason']) for item in listing], [('100', 'is already shared (build 7)')])
            with self.assertRaisesRegex(TidyError, 'already shared'):
                tidy.share(self.disk, 'guest', '100', USERS)

    def test_a_record_that_no_longer_fits_what_the_store_holds_is_dropped(self):
        self.shared_on_disk(listed=('main', 'guest'))
        pending = self.disk.root / 'pending'
        pending.mkdir(exist_ok=True)
        (pending / '100.json').write_text(json.dumps({
            'version': tidy.RECORD_VERSION, 'appid': '100', 'name': 'Test Game', 'user': 'guest', 'source': 'library',
            'kind': 'new', 'library': str(self.usb_library('guest')), 'installdir': 'Game', 'buildid': '7',
            'depots': {'1001': '555'}, 'acf': self.acf(), 'listed': ['bin/game', 'data/a.dat'], 'folders': [],
            'files': [], 'prepared_utc': 'x'}))
        result = tidy.commit(self.disk, USERS)
        self.assertEqual(result['committed'], [])
        self.assertEqual(result['dropped'], [{'appid': '100', 'reason': 'the store changed since the game was checked'}])
        self.assertEqual(tidy.stored(self.disk)['100']['shared_by'], 'main')
        self.assertFalse((pending / '100.json').exists())

    def test_the_validation_manifest_is_written_into_the_real_library_of_the_account_that_has_none_of_the_game(self):
        self.shared_on_disk(listed=('main', 'guest'))
        os.unlink(self.usb_library('guest') / 'steamapps/appmanifest_100.acf')
        record = {'appid': '100', 'kind': 'new', 'user': 'main', 'acf': self.acf()}
        self.assertEqual(tidy._validation(self.disk, USERS, record), [])
        state = self.read_acf(self.usb_library('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual((state['StateFlags'], state['FullValidateBeforeNextUpdate']), ('2', '1'))
        make_whiteout(self.usb_library('guest') / 'steamapps/appmanifest_100.acf')
        self.assertEqual(tidy._validation_for(self.disk, 'guest', self.entries['guest'], record), 'uninstalled')


class Migration(DiskEnv):
    """The first slot after the update: each private layer of the earlier layout moves, by rename, into the account's real
    library of the same disk; nothing is overwritten; the old library leaves the account's Steam list."""

    def setUp(self):
        super().setUp()
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        for user in USERS:
            (self.usb_library(user) / 'steamapps').mkdir(parents=True)
        self.relist(USERS)
        self.inodes = {}
        for place in (self.layout, self.disk):
            for user in USERS:
                side = place.side(user)
                side.retired.upper.mkdir(parents=True)
        self.list_libraries('main', self.layout.side('main').retired.library, self.disk.side('main').retired.library,
                            self.usb_library('main'))
        # Main on the primary: an update's manifest, a save inside the game folder, a file the real library has too, a
        # whiteout, and the stray files an old library carried.
        real = self.steam('main') / 'steamapps/common/Game'
        real.mkdir(parents=True)
        (real / 'config.ini').write_bytes(b'real')
        layer = self.layout.side('main').retired.upper
        (layer / 'steamapps/common/Game/save').mkdir(parents=True)
        (layer / 'steamapps/common/Game/data').mkdir()
        (layer / 'steamapps/common/Game/save/slot1').write_bytes(b'progress')
        (layer / 'steamapps/common/Game/config.ini').write_bytes(b'layer')
        make_whiteout(layer / 'steamapps/common/Game/data/b.dat')
        (layer / 'steamapps/appmanifest_100.acf').write_text(self.acf(buildid='8'))
        (layer / 'steamapps/libraryfolders.vdf').write_text('stray')
        (layer / 'libraryfolder.vdf').write_text('"libraryfolder"\n{\n}\n')
        # The library's config.ini is the newer of the two, so it stays (the layer's newer files win: a test below).
        later = os.stat(layer / 'steamapps/common/Game/config.ini').st_mtime_ns + 10_000_000_000
        os.utime(real / 'config.ini', ns=(later, later))
        self.inodes['slot1'] = os.stat(layer / 'steamapps/common/Game/save/slot1').st_ino
        # Guest on the primary: only a manifest. Main on the disk: a game it installed there.
        guest_layer = self.layout.side('guest').retired.upper
        (guest_layer / 'steamapps').mkdir(parents=True)
        (guest_layer / 'steamapps/appmanifest_100.acf').write_text(self.acf(buildid='8'))
        disk_layer = self.disk.side('main').retired.upper
        (disk_layer / 'steamapps/common/Other').mkdir(parents=True)
        (disk_layer / 'steamapps/common/Other/x').write_bytes(b'x')
        self.old_work = self.layout.side('main').retired.work / 'work'
        self.old_work.mkdir(parents=True)

    def migrate(self, place=None):
        return tidy.migrate(place or self.layout, USERS, proc=self.proc)

    def assert_migrated(self):
        real = self.steam('main') / 'steamapps'
        self.assertEqual((real / 'common/Game/config.ini').read_bytes(), b'real')          # nothing was overwritten
        self.assertEqual((real / 'common/Game/save/slot1').read_bytes(), b'progress')
        self.assertEqual(os.stat(real / 'common/Game/save/slot1').st_ino, self.inodes['slot1'])      # by rename
        self.assertTrue(fake_whiteout(os.lstat(real / 'common/Game/data/b.dat')))                    # as a whiteout
        self.assertEqual(self.read_acf(real / 'appmanifest_100.acf')['buildid'], '8')
        self.assertEqual(self.read_acf(self.steam('guest') / 'steamapps/appmanifest_100.acf')['buildid'], '8')
        self.assertEqual((self.root / 'conflicts/main/layer/steamapps/common/Game/config.ini').read_bytes(), b'layer')
        self.assertEqual((self.root / 'conflicts/main/layer/steamapps/libraryfolders.vdf').read_text(), 'stray')
        self.assertTrue((self.root / 'conflicts/main/layer/libraryfolder.vdf').exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'conflicts/main').st_mode), 0o700)
        for place in (self.layout, self.disk):
            for user in USERS:
                side = place.side(user)
                self.assertFalse(os.path.lexists(side.retired.upper), side.retired.upper)
        self.assertFalse(os.path.lexists(self.old_work))
        self.assertFalse(os.path.lexists(self.root / 'migrating-main.json'))
        self.assertEqual((self.usb_library('main') / 'steamapps/common/Other/x').read_bytes(), b'x')
        listed = depot.read_vdf(self.steam('main') / 'steamapps/libraryfolders.vdf')[1]
        paths = [item['path'] for item in listed.values() if isinstance(item, dict)]
        self.assertEqual(paths, [str(self.steam('main')), str(self.usb_library('main'))])

    def run_all(self):
        return self.migrate(), self.migrate(self.disk)

    def test_both_layers_of_both_accounts_move_into_the_real_libraries(self):
        primary, disk = self.run_all()
        self.assertEqual(primary['main']['moved'], 3)
        self.assertEqual(sorted(primary['main']['conflicts']),
                         ['layer/steamapps/common/Game/config.ini', 'layer/steamapps/libraryfolders.vdf'])
        self.assertTrue(primary['main']['listed_off'])
        self.assertEqual(primary['guest']['moved'], 1)
        self.assertEqual(disk['main']['moved'], 1)
        self.assertEqual(disk['main']['conflicts'], [])
        self.assert_migrated()

    def test_a_second_run_changes_nothing(self):
        self.run_all()
        def names():
            return [(str(path), os.lstat(path).st_ino) for top in (self.steam('main'), self.root / 'conflicts')
                    for path in sorted(top.rglob('*'))]
        before = names()
        self.assertEqual(self.run_all(), ({}, {}))
        self.assertEqual(names(), before)

    def test_an_item_that_exists_in_the_library_is_never_overwritten_not_even_by_a_whiteout(self):
        real = self.steam('main') / 'steamapps/common/Game'
        (real / 'data').mkdir()
        (real / 'data/b.dat').write_bytes(b'the account made it again')
        self.migrate()
        self.assertEqual((real / 'data/b.dat').read_bytes(), b'the account made it again')
        self.assertTrue(fake_whiteout(os.lstat(self.root / 'conflicts/main/layer/steamapps/common/Game/data/b.dat')))

    def test_of_two_copies_of_a_file_the_newer_stays_in_the_library_and_the_older_goes_aside(self):
        # Seen on the Deck: FlatOut 2's saves from the earlier layout (2026) were set aside and the copies from before
        # the game was shared (2024) kept, which would have lost the player's progress.
        layer_save = self.root / 'sides/main/upper/steamapps/common/Game/save/slot1'
        real = self.steam('main') / 'steamapps/common/Game/save'
        real.mkdir(parents=True, exist_ok=True)
        (real / 'slot1').write_bytes(b'from before sharing')
        os.utime(real / 'slot1', ns=(1_000_000_000, 1_000_000_000))
        self.assertTrue(layer_save.exists())
        self.migrate()
        self.assertEqual((real / 'slot1').read_bytes(), b'progress', 'the layer\'s newer save is in the library')
        self.assertEqual((self.root / 'conflicts/main/library/steamapps/common/Game/save/slot1').read_bytes(),
                         b'from before sharing')
        # Steam's own files never take this way: the layer's stray list stays aside, the account's list stays.
        self.assertTrue((self.root / 'conflicts/main/layer/steamapps/libraryfolders.vdf').exists())

    def test_a_folder_in_the_layer_where_the_library_has_a_file_is_kept_whole_in_conflicts(self):
        real = self.steam('main') / 'steamapps/common'
        shutil.rmtree(real / 'Game')
        (real / 'Game').write_bytes(b'a file')
        self.migrate()
        self.assertEqual((real / 'Game').read_bytes(), b'a file')
        self.assertEqual((self.root / 'conflicts/main/layer/steamapps/common/Game/save/slot1').read_bytes(), b'progress')

    def test_a_link_in_the_library_is_not_followed(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        game = self.steam('main') / 'steamapps/common/Game'
        shutil.rmtree(game)
        game.symlink_to(elsewhere)
        self.migrate()
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual((self.root / 'conflicts/main/layer/steamapps/common/Game/save/slot1').read_bytes(), b'progress')

    def interrupted(self, label=None, renames=None):
        """Run the migration until it dies at a checkpoint or at its n-th rename; then run it again, to the end."""
        class Dead(BaseException):
            pass
        count = [0]
        real_rename = os.rename

        def rename(*args, **kwargs):
            count[0] += 1
            if renames is not None and count[0] == renames:
                raise Dead()
            return real_rename(*args, **kwargs)

        def checkpoint(name):
            if name == label:
                raise Dead()
        with patch.object(tidy, '_checkpoint', checkpoint), patch.object(tidy.os, 'rename', rename):
            with self.assertRaises(Dead):
                self.migrate()
        self.assertTrue((self.root / 'migrating-main.json').exists() or renames is not None)

    def test_dying_at_any_point_loses_nothing_and_the_next_run_finishes(self):
        for name, kwargs in (('before the first move', {'label': 'begin'}), ('after the moves', {'label': 'layers'}),
                             ('after the list', {'label': 'lists'}), ('after the clean-up', {'label': 'cleanup'}),
                             ('after the 1st rename', {'renames': 1}), ('after the 3rd rename', {'renames': 3}),
                             ('after the 5th rename', {'renames': 5}), ('after the 6th rename', {'renames': 6})):
            with self.subTest(name):
                self.tearDown_world()
                self.setUp()
                self.interrupted(**kwargs)
                self.run_all()
                self.assert_migrated()
                self.assertEqual(self.run_all(), ({}, {}))

    def tearDown_world(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_the_journal_names_the_step(self):
        phases = []

        def checkpoint(name):
            phases.append((name, json.loads((self.root / 'migrating-main.json').read_text())['phase']))
        with patch.object(tidy, '_checkpoint', checkpoint):
            self.migrate()
        self.assertEqual(phases, [('begin', 'layers'), ('layers', 'layers'), ('lists', 'lists'), ('cleanup', 'cleanup')])

    def test_an_account_without_a_library_on_the_disk_waits_and_nothing_of_it_moves(self):
        self.disk.libraries.pop('main')
        shutil.rmtree(self.usb_library('main'))
        report = self.migrate(self.disk)
        self.assertIn('no Steam library', report['main']['error'])
        self.assertTrue((self.disk.side('main').retired.upper / 'steamapps/common/Other/x').exists())

    def test_it_waits_while_the_old_library_is_still_mounted(self):
        side = self.layout.side('main')
        self.mounted.add(str(side.retired.library))
        report = self.migrate()
        self.assertIn('still mounted', report['main']['error'])
        self.assertTrue((side.retired.upper / 'steamapps/appmanifest_100.acf').exists())
        self.assertFalse((self.steam('main') / 'steamapps/appmanifest_100.acf').exists())

    def test_it_is_refused_while_the_accounts_steam_runs(self):
        (self.proc / '41').mkdir()
        (self.proc / '41/comm').write_text('steam\n')
        report = self.migrate()
        self.assertIn('Steam is running', report['main']['error'])
        self.assertTrue((self.layout.side('main').retired.upper / 'steamapps/appmanifest_100.acf').exists())


class FileLists(Env):
    """Asking a closed Steam for the depot manifests it dropped, and taking the question back when it never answered."""
    NOW = 1_800_000_000
    MINE, HERS = '/'.join(('main', '100')), '/'.join(('guest', '200'))      # the memory's keys: user, then app

    def setUp(self):
        super().setUp()
        self.install('main', {'a': b'1'}, appid='100', installdir='Old', depot_manifest=False)
        self.install('main', {'b': b'2'}, appid='101', installdir='Fresh', depots=((1002, 777),), depot_manifest=False)
        (self.steam('main') / 'depotcache' / depot.manifest_name(1002, 777)).write_bytes(b'cached')
        self.install('main', {'c': b'3'}, appid='102', installdir='Tool', name='Proton 11.0', depot_manifest=False)
        self.install('main', {'d': b'4'}, appid='103', installdir='Half', flags='1026', depot_manifest=False)
        self.install('guest', {'e': b'5'}, appid='200', installdir='Hers', depot_manifest=False)

    def manifest(self, user, appid):
        return self.steam(user) / 'steamapps' / f'appmanifest_{appid}.acf'

    def flags(self, user, appid):
        return self.read_acf(self.manifest(user, appid))['StateFlags']

    def ask(self, online=True, later=0, users=USERS):
        return tidy.ask_for_lists(self.layout, users, online=online, now=self.NOW + later)

    def memory(self):
        return json.loads((self.root / 'lists.json').read_text())['asked']

    def test_only_installed_games_without_their_lists_are_asked_for(self):
        before = self.read_acf(self.manifest('main', '100'))
        self.assertEqual(self.ask(), {'asked': 2, 'returned': 0, 'answered': 0})
        self.assertEqual([self.flags('main', appid) for appid in ('100', '101', '102', '103')], ['2', '4', '4', '1026'])
        self.assertEqual(self.flags('guest', '200'), '2')
        after = self.read_acf(self.manifest('main', '100'))
        self.assertEqual({key: value for key, value in after.items() if key != 'StateFlags'},
                         {key: value for key, value in before.items() if key != 'StateFlags'})
        self.assertEqual(sorted(self.memory()), [self.HERS, self.MINE])
        self.assertEqual(self.memory()[self.MINE]['state'], 'asked')
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'lists.json').st_mode), 0o600)

    def stopped_after_asking(self):
        """Ask, with the write of the memory after the manifests were changed failing (the machine stopped there)."""
        real, calls = tidy._write_lists, []

        def write(layout, asked):
            calls.append(1)
            if len(calls) > 1:
                raise OSError('the machine stopped')
            real(layout, asked)
        with patch.object(tidy, '_write_lists', write), self.assertRaises(OSError):
            self.ask()
        self.assertEqual((self.flags('main', '100'), self.flags('guest', '200')), ('2', '2'))

    def test_a_question_whose_record_was_never_finished_is_still_taken_back(self):
        # The manifests said "update required" and nothing remembered why: the next run, offline, reported nothing to
        # take back and the games stayed that way.
        self.stopped_after_asking()
        self.assertEqual(self.ask(online=False, later=600), {'asked': 0, 'returned': 2, 'answered': 0})
        self.assertEqual((self.flags('main', '100'), self.flags('guest', '200')), ('4', '4'))

    def test_a_question_that_was_written_down_and_never_put_is_forgotten_without_a_touch(self):
        # The run stopped between writing the question down and changing the manifest.
        path = self.manifest('main', '100')
        info = os.lstat(path)
        (self.root / 'lists.json').write_text(json.dumps({'version': 1, 'asked': {self.MINE: {
            'path': str(path), 'state': 'asking', 'utc': self.NOW, 'intended': 'f' * 64,
            'before': [info.st_size, info.st_mtime_ns, info.st_ino]}}}))
        before = path.read_bytes()
        self.assertEqual(self.ask(online=False, later=600), {'asked': 0, 'returned': 0, 'answered': 0})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.memory(), {})

    def test_what_steam_wrote_after_an_unfinished_question_is_left_alone(self):
        self.stopped_after_asking()
        # Steam ran and wrote the manifest itself: still "update required", but its own text now.
        text = self.manifest('main', '100').read_text().replace('"Old"', '"Old"\n\t"LastUpdated"\t\t"1800000001"')
        self.manifest('main', '100').write_text(text)
        result = self.ask(online=False, later=600)
        self.assertEqual((result['returned'], result['answered']), (1, 1))
        self.assertEqual(self.manifest('main', '100').read_text(), text)
        self.assertEqual(self.flags('guest', '200'), '4')

    def test_a_library_on_any_disk_counts_and_the_earlier_overlay_folders_do_not(self):
        other = self.own_library('main')
        self.install('main', {'f': b'6'}, appid='104', installdir='Far', library=other, depot_manifest=False)
        earlier = self.layout.side('main').retired.library
        (earlier / 'steamapps').mkdir(parents=True)
        (earlier / 'steamapps/appmanifest_105.acf').write_text(self.acf('105', installdir='Shared'))
        self.list_libraries('main', other, earlier)
        self.ask(users=['main'])
        self.assertEqual(self.read_acf(other / 'steamapps/appmanifest_104.acf')['StateFlags'], '2')
        self.assertEqual(self.read_acf(earlier / 'steamapps/appmanifest_105.acf')['StateFlags'], '4')

    def test_offline_nothing_is_asked(self):
        self.assertEqual(self.ask(online=False), {'asked': 0, 'returned': 0, 'answered': 0})
        self.assertEqual(self.flags('main', '100'), '4')

    def test_what_steam_answered_is_forgotten(self):
        self.ask()
        # Steam ran its update: it wrote the manifest anew (installed again) and fetched the list.
        os.unlink(self.manifest('main', '100'))
        self.manifest('main', '100').write_text(self.acf('100', installdir='Old'))
        (self.steam('main') / 'depotcache' / depot.manifest_name(1001, 555)).write_bytes(b'fetched')
        os.unlink(self.manifest('guest', '200'))
        self.manifest('guest', '200').write_text(self.acf('200', installdir='Hers'))
        self.assertEqual(self.ask(later=600, users=['main']), {'asked': 0, 'returned': 0, 'answered': 1})
        self.assertEqual(self.memory(), {})
        self.assertEqual(self.flags('main', '100'), '4')

    def test_a_question_never_answered_is_taken_back_and_not_asked_again_for_a_day(self):
        self.ask()
        self.assertEqual(self.ask(later=600), {'asked': 0, 'returned': 2, 'answered': 0})
        self.assertEqual((self.flags('main', '100'), self.flags('guest', '200')), ('4', '4'))
        self.assertEqual(self.memory()[self.MINE]['state'], 'returned')
        self.assertEqual(self.ask(later=3600), {'asked': 0, 'returned': 0, 'answered': 0})
        self.assertEqual(self.flags('main', '100'), '4')
        self.assertEqual(self.ask(later=600 + 24 * 3600)['asked'], 2)
        self.assertEqual(self.flags('main', '100'), '2')

    def test_it_is_taken_back_while_offline_too(self):
        self.ask()
        self.assertEqual(self.ask(online=False, later=600)['returned'], 2)
        self.assertEqual(self.flags('main', '100'), '4')

    def test_a_manifest_steam_changed_but_left_waiting_is_not_touched(self):
        self.ask()
        state = self.read_acf(self.manifest('main', '100'))
        state['BytesToDownload'] = '99'
        os.unlink(self.manifest('main', '100'))
        self.manifest('main', '100').write_text(depot.vdf_dumps('AppState', state))
        self.ask(later=600, users=['main'])
        self.assertEqual(self.flags('main', '100'), '2')

    def test_a_damaged_memory_is_an_empty_one_and_an_account_that_left_is_dropped(self):
        (self.root / 'lists.json').write_text('{"version": 1, "asked": [')
        self.assertEqual(self.ask()['asked'], 2)
        self.assertEqual(sorted(self.ask(later=600, users=['main'])), ['answered', 'asked', 'returned'])
        self.assertEqual(sorted(self.memory()), [self.MINE])

    def test_a_link_in_place_of_a_manifest_is_never_followed(self):
        target = self.base / 'elsewhere.acf'
        target.write_text(self.acf('100', installdir='Old'))
        os.unlink(self.manifest('main', '100'))
        os.symlink(target, self.manifest('main', '100'))
        self.ask(users=['main'])
        self.assertEqual(self.read_acf(target)['StateFlags'], '4')


if __name__ == '__main__':
    unittest.main()
