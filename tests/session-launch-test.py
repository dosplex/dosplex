#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Check actual Gamescope launch argv using temporary executables, without Steam."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "guest/dosplex-user-side-session.sh"
LAYER_MANIFEST = "share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86_64.json"
LAYER_LIBRARY = "lib/libVkLayer_FROG_gamescope_wsi_x86_64.so"
LAYER_MANIFEST_32 = "share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.x86.json"
LAYER_LIBRARY_32 = "lib/libVkLayer_FROG_gamescope_wsi_x86.so"


def layer_manifest(family, name=None, disable="DOSPLEX_DISABLE_GAMESCOPE_WSI", also_disable=None):
    """A manifest like the one assemble.py makes: the DosPlex name and off switch unless told otherwise.
    `also_disable` adds a second variable to disable_environment."""
    switches = {disable: "1", **({also_disable: "1"} if also_disable else {})}
    return json.dumps({"file_format_version": "1.0.0", "layer": {
        "name": name or "VK_LAYER_DOSPLEX_gamescope_wsi_" + family, "type": "GLOBAL",
        "library_path": "../../../lib/libVkLayer_FROG_gamescope_wsi_%s.so" % family,
        "enable_environment": {"ENABLE_GAMESCOPE_WSI": "1"}, "disable_environment": switches}}, indent=4)


# What the launcher adds to the environment when it uses the bundled layer, besides XDG_DATA_DIRS.
BUNDLED = {"DISABLE_GAMESCOPE_WSI": "1", "PRESSURE_VESSEL_FILESYSTEMS_RO": "<run>/bundle/lib:<run>/bundle/share"}


class SessionLaunch(unittest.TestCase):
    def launch(self, width, height, scaler="fit", side="left", missing=None, payload="steam", role="primary",
               environment=None, bundle=None, cwd=None, prepare=None):
        """Run the launcher with stand-ins for steam, gamescope and dbus-run-session.

        `bundle` is a function that gets the temporary directory and makes a release bundle in it (see
        make_bundle); it returns the launcher to run, else the launcher of the repository is run. `prepare` is a
        function that gets the temporary directory and the directory of the stand-ins, and may add to both; the
        environment variables it returns are added to those of the run."""
        with tempfile.TemporaryDirectory(prefix="dosplex-launch-test-") as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            scripts = {
                "steam": "#!/bin/sh\nexit 91\n",
                "gamescope": '#!/usr/bin/python3\nimport json,os,sys\nopen(os.environ["CAPTURE"],"w").write(json.dumps(sys.argv[1:]))\n'
                             'open(os.environ["CAPTURE"]+".audio","w").write(json.dumps({k:v for k,v in os.environ.items() if k.startswith("PULSE_")}))\n'
                             'open(os.environ["CAPTURE"]+".env","w").write(json.dumps(dict(os.environ)))\n'
                             'open(os.environ["CAPTURE"]+".cwd","w").write(os.getcwd())\n'
                             'c=os.environ.get("MANGOHUD_CONFIGFILE")\n'
                             'open(os.environ["CAPTURE"]+".files","w").write(json.dumps({"config":open(c).read() if c and os.path.exists(c) else None,"config_uid":os.stat(c).st_uid if c and os.path.exists(c) else None,"marker":os.path.isfile("mangoapp"),"cwd_mode":os.stat(".").st_mode&0o777,"perf":os.path.isdir(os.path.expanduser("~/.local/share/dosplex/perf")) and oct(os.stat(os.path.expanduser("~/.local/share/dosplex/perf")).st_mode&0o777),"runtime_has_ipc":os.path.isdir(os.environ.get("XDG_RUNTIME_DIR","/nonexistent")+"/mangoapp-ipc")}))\n',
                "dbus-run-session": '#!/bin/sh\nshift\nexec "$@"\n',
            }
            for name, body in scripts.items():
                if name == missing:
                    continue
                path = bin_dir / name
                path.write_text(body)
                path.chmod(0o755)
            # Exclude host-installed Steam/Gamescope from dependency tests.
            for name in ('mkdir', 'touch', 'date', 'id', 'bash'):
                (bin_dir / name).symlink_to('/usr/bin/' + name)
            env = {
                "PATH": str(bin_dir), "HOME": directory,
                "CAPTURE": str(root / "argv.json"),
                "DOSPLEX_SESSION_PAYLOAD": payload,
                "DOSPLEX_SIDE_ROLE": role,
                "DOSPLEX_OUTPUT_WIDTH": "960", "DOSPLEX_OUTPUT_HEIGHT": "1080",
                "DOSPLEX_GAME_WIDTH": str(width), "DOSPLEX_GAME_HEIGHT": str(height),
                "DOSPLEX_SCALE_MODE": scaler,
                "DOSPLEX_LAUNCH_LOG": str(root / "launch.log"),
                **(environment or {}),
            }
            if prepare:
                env.update(prepare(root, bin_dir))
            launcher = bundle(root) if bundle else LAUNCHER
            result = subprocess.run(["bash", str(launcher), side, "tester"], env=env, text=True,
                                    capture_output=True, timeout=5, cwd=cwd(root) if callable(cwd) else cwd)
            capture = root / "argv.json"
            audio = root / "argv.json.audio"
            environment_seen = root / "argv.json.env"
            self.audio = json.loads(audio.read_text()) if audio.exists() else None
            # What the stand-in for gamescope found in its environment, with this run's directory made anonymous.
            self.seen = (json.loads(environment_seen.read_text().replace(directory, "<run>"))
                         if environment_seen.exists() else None)
            files_seen = root / "argv.json.files"
            self.files = json.loads(files_seen.read_text()) if files_seen.exists() else None
            cwd_seen = root / "argv.json.cwd"
            self.gamescope_cwd = cwd_seen.read_text() if cwd_seen.exists() else None
            log = root / "launch.log"
            self.log = log.read_text().replace(directory, "<run>") if log.exists() else ""
            self.directory = directory
            return result, json.loads(capture.read_text()) if capture.exists() else None

    @staticmethod
    def make_bundle(layer=True, library=True, directory="libexec", link=False, relative=False, layer32=True,
                    library32=True, manifests=None):
        """A function for launch(bundle=...): a release bundle with the launcher in libexec/ and the layers.

        The tree is <run>/bundle/{libexec,lib,share}; the launcher it returns is the copy in libexec/, by way of
        <run>/opt/current (a symlink to the bundle) when `link` is set, and by its bare name when `relative` is
        set (run it from the directory it is in). `manifests` replaces the text of the manifests (path: text)."""
        texts = {LAYER_MANIFEST: layer_manifest("x86_64"), LAYER_MANIFEST_32: layer_manifest("x86"),
                 **(manifests or {})}

        def make(root):
            bundle = root / "bundle"
            (bundle / directory).mkdir(parents=True)
            for path, wanted in ((LAYER_MANIFEST, layer), (LAYER_MANIFEST_32, layer32)):
                if wanted:
                    (bundle / path).parent.mkdir(parents=True, exist_ok=True)
                    (bundle / path).write_text(texts[path])
            for path, wanted in ((LAYER_LIBRARY, library), (LAYER_LIBRARY_32, library32)):
                if wanted:
                    (bundle / path).parent.mkdir(parents=True, exist_ok=True)
                    (bundle / path).write_bytes(b"\x7fELF")
            copy = bundle / directory / "dosplex-user-side-session.sh"
            shutil.copy(LAUNCHER, copy)
            if link:
                (root / "opt").mkdir()
                (root / "opt" / "current").symlink_to(bundle)
                return root / "opt" / "current" / directory / "dosplex-user-side-session.sh"
            return Path(copy.name) if relative else copy
        return make

    def test_independent_game_and_output_dimensions(self):
        for side in ("left", "right"):
            for width, height in ((960, 1080), (1280, 720), (1920, 1080)):
                for scaler in ("fit", "stretch"):
                    with self.subTest(side=side, width=width, height=height, scaler=scaler):
                        result, args = self.launch(width, height, scaler, side)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        for option, value in {"--output-width": "960", "--output-height": "1080",
                                              "--nested-width": str(width), "--nested-height": str(height),
                                              "--scaler": scaler}.items():
                            self.assertEqual(args[args.index(option) + 1], value)
                        self.assertEqual(args[args.index("--") + 1], "steam")
                        self.assertNotIn("--force-windows-fullscreen", args)

    def test_zoom_starts_gamescope_with_the_fit_scaler(self):
        # Zoom is the compositor's crop over Gamescope's Fit scaler; Gamescope has no such scaler.
        result, args = self.launch(1920, 2160, "zoom", "left")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args[args.index("--scaler") + 1], "fit")

    def test_reject_invalid_configuration_before_launch(self):
        for width, height, scaler in ((0, 720, "fit"), (99999, 720, "fit"),
                                      ("1280;false", 720, "fit"), (1280, -1, "fit"),
                                      (1280, 720, "unknown")):
            with self.subTest(width=width, height=height, scaler=scaler):
                result, args = self.launch(width, height, scaler)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(args)

    def test_guest_controller_launch_does_not_force_host_mode_on_either_side(self):
        for side in ('left', 'right'):
            for role in ('primary', 'guest'):
                with self.subTest(side=side, role=role):
                    result, args = self.launch(1280, 720, side=side, role=role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    steam_args = args[args.index('--') + 1:]
                    self.assertEqual(steam_args[0], 'steam')
                    self.assertIn('-gamepadui', steam_args)
                    for flag in ('-steamos3', '-steampal', '-steamdeck'):
                        self.assertIn(flag, steam_args, 'the flags of SteamOS\'s own launcher on both sides')
                    self.assertNotIn('-steamos', steam_args)
                    self.assertNotIn('-cef-enable-debugging', steam_args)

    def test_main_reaches_the_shared_audio_server_as_before(self):
        result, _ = self.launch(1280, 720, side='left')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.audio, {'PULSE_SERVER': '127.0.0.1', 'PULSE_SINK': 'dosplex_left',
                                      'PULSE_SOURCE': 'dosplex_left_mic.monitor', 'PULSE_PROP': 'dosplex.side=left'})
        result, _ = self.launch(1280, 720, side='left', environment={'DOSPLEX_AUDIO_SERVER': 'unix:/run/main/native'})
        self.assertEqual(self.audio['PULSE_SERVER'], 'unix:/run/main/native')

    def test_guest_gets_its_own_endpoint_and_never_the_shared_server(self):
        shared = {'DOSPLEX_AUDIO_SERVER': 'unix:/run/main/native', 'PULSE_SERVER': '127.0.0.1'}
        result, _ = self.launch(1280, 720, side='right', role='guest', environment={
            **shared, 'DOSPLEX_GUEST_AUDIO_SERVER': 'unix:/run/guest/pulse/native'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.audio, {'PULSE_SERVER': 'unix:/run/guest/pulse/native', 'PULSE_SINK': 'dosplex_right',
                                      'PULSE_SOURCE': 'dosplex_right_mic', 'PULSE_PROP': 'dosplex.side=right'})

    def test_guest_without_a_unix_endpoint_has_no_audio_instead_of_the_shared_server(self):
        shared = {'DOSPLEX_AUDIO_SERVER': 'unix:/run/main/native', 'PULSE_SERVER': '127.0.0.1'}
        for guest_server in (None, '', '127.0.0.1', 'tcp:127.0.0.1:4713', 'unix:relative/native'):
            with self.subTest(guest_server=guest_server):
                environment = dict(shared)
                if guest_server is not None:
                    environment['DOSPLEX_GUEST_AUDIO_SERVER'] = guest_server
                result, args = self.launch(1280, 720, side='right', role='guest', environment=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Guest has no audio', result.stderr)
                self.assertNotIn('PULSE_SERVER', self.audio)
                self.assertEqual(args[args.index('--') + 1], 'steam')

    def test_unknown_role_rejected_before_launch(self):
        result, args = self.launch(1280, 720, role='unknown')
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(args)

    def test_the_bundled_vulkan_layer_comes_first_in_xdg_data_dirs_for_both_sides(self):
        for side in ("left", "right"):
            for role in ("primary", "guest"):
                with self.subTest(side=side, role=role):
                    result, args = self.launch(1280, 720, side=side, role=role, bundle=self.make_bundle(),
                                               environment={"XDG_DATA_DIRS": "/var/lib/flatpak/exports/share:/usr/share"})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.seen["XDG_DATA_DIRS"],
                                     "<run>/bundle/share:/var/lib/flatpak/exports/share:/usr/share")
                    self.assertEqual(args[args.index("--") + 1], "steam")
                    self.assertIn("vulkan_layer=bundled (<run>/bundle/" + LAYER_MANIFEST + ")", self.log)
                    self.assertIn("XDG_DATA_DIRS=<run>/bundle/share:/var/lib/flatpak/exports/share:/usr/share", self.log)
                    for name, value in BUNDLED.items():
                        self.assertEqual(self.seen[name], value)
                    # Logged next to the vulkan_layer line.
                    lines = self.log.splitlines()
                    at = lines.index("vulkan_layer=bundled (<run>/bundle/" + LAYER_MANIFEST + ")")
                    self.assertEqual(lines[at + 1:at + 3], [
                        "PRESSURE_VESSEL_FILESYSTEMS_RO=<run>/bundle/lib:<run>/bundle/share",
                        "DISABLE_GAMESCOPE_WSI=1"])

    def test_without_xdg_data_dirs_the_default_search_path_stays_behind_the_bundle(self):
        for value in (None, ""):
            with self.subTest(value=value):
                result, _ = self.launch(1280, 720, bundle=self.make_bundle(),
                                        environment={} if value is None else {"XDG_DATA_DIRS": value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.seen["XDG_DATA_DIRS"], "<run>/bundle/share:/usr/local/share:/usr/share")

    def test_the_bundle_is_found_through_a_symlink_and_from_a_relative_launcher_path(self):
        result, _ = self.launch(1280, 720, bundle=self.make_bundle(link=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seen["XDG_DATA_DIRS"], "<run>/opt/current/share:/usr/local/share:/usr/share")
        # "cd libexec; bash dosplex-user-side-session.sh": the directory put into XDG_DATA_DIRS is still absolute,
        # and a CDPATH that leads somewhere else makes no difference.
        result, _ = self.launch(1280, 720, bundle=self.make_bundle(relative=True),
                                cwd=lambda root: root / "bundle" / "libexec", environment={"CDPATH": "/usr"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seen["XDG_DATA_DIRS"], "<run>/bundle/share:/usr/local/share:/usr/share")

    def test_nothing_changes_outside_a_bundle_or_in_a_bundle_without_the_layer(self):
        nothing = "vulkan_layer=host (no bundled layer next to this launcher)"
        cases = {
            "the launcher of the repository": (dict(), nothing),
            "a bundle without the manifest": (dict(bundle=self.make_bundle(layer=False)), LAYER_MANIFEST),
            "a bundle without the library": (dict(bundle=self.make_bundle(library=False)), LAYER_LIBRARY),
            "a bundle without the 32-bit manifest": (dict(bundle=self.make_bundle(layer32=False)), LAYER_MANIFEST_32),
            "a bundle without the 32-bit library": (dict(bundle=self.make_bundle(library32=False)), LAYER_LIBRARY_32),
            "a launcher that is not in libexec": (dict(bundle=self.make_bundle(directory="guest")), nothing),
        }
        for label, (arguments, missing) in cases.items():
            with self.subTest(label):
                result, args = self.launch(1280, 720, environment={"XDG_DATA_DIRS": "/usr/share"}, **arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.seen["XDG_DATA_DIRS"], "/usr/share")
                for name in BUNDLED:
                    self.assertNotIn(name, self.seen)
                # The log names the file that is missing, so a device log tells which one it was.
                expected = (missing if missing == nothing
                            else "vulkan_layer=host (the bundle has no <run>/bundle/%s)" % missing)
                self.assertIn(expected + "\n", self.log)
                self.assertIn("PRESSURE_VESSEL_FILESYSTEMS_RO=\n", self.log)
                self.assertEqual(args[args.index("--") + 1], "steam")
        result, _ = self.launch(1280, 720)
        self.assertNotIn("XDG_DATA_DIRS", self.seen)

    def test_a_bundled_manifest_that_still_answers_to_the_hosts_switch_changes_nothing(self):
        # DISABLE_GAMESCOPE_WSI=1 would turn off such a layer as well, and games would have no Gamescope layer.
        cases = {
            "the upstream name": {LAYER_MANIFEST: layer_manifest("x86_64", name="VK_LAYER_FROG_gamescope_wsi_x86_64",
                                                                 disable="DISABLE_GAMESCOPE_WSI")},
            "the upstream off switch": {LAYER_MANIFEST_32: layer_manifest("x86", disable="DISABLE_GAMESCOPE_WSI")},
            "the name of the other family": {
                LAYER_MANIFEST_32: layer_manifest("x86", name="VK_LAYER_DOSPLEX_gamescope_wsi_x86_64")},
            "an empty manifest": {LAYER_MANIFEST: ""},
            # The DosPlex name and switch, but the host's switch as well: DISABLE_GAMESCOPE_WSI=1 would turn it off.
            "the DosPlex switch and the upstream one": {
                LAYER_MANIFEST: layer_manifest("x86_64", also_disable="DISABLE_GAMESCOPE_WSI")},
        }
        for label, manifests in cases.items():
            with self.subTest(label):
                result, args = self.launch(1280, 720, environment={"XDG_DATA_DIRS": "/usr/share"},
                                           bundle=self.make_bundle(manifests=manifests))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.seen["XDG_DATA_DIRS"], "/usr/share")
                for name in BUNDLED:
                    self.assertNotIn(name, self.seen)
                self.assertRegex(self.log, r"vulkan_layer=host \(the bundled manifest <run>/bundle/share/vulkan/"
                                           r"implicit_layer\.d/VkLayer_FROG_gamescope_wsi\.x86(_64)?\.json is not "
                                           r"the DosPlex layer VK_LAYER_DOSPLEX_gamescope_wsi_x86(_64)?\)")
                self.assertEqual(args[args.index("--") + 1], "steam")

    def test_the_container_paths_keep_what_was_there_and_are_not_repeated(self):
        cases = {
            "unset": (None, "<run>/bundle/lib:<run>/bundle/share"),
            "empty": ("", "<run>/bundle/lib:<run>/bundle/share"),
            "a path of the player": ("/srv/mangohud.conf", "/srv/mangohud.conf:<run>/bundle/lib:<run>/bundle/share"),
            "several, with empty entries": (":/a::/b:", "/a:/b:<run>/bundle/lib:<run>/bundle/share"),
            "one of ours already there": ("{run}/bundle/share:/a", "<run>/bundle/share:/a:<run>/bundle/lib"),
            "both of ours already there": ("{run}/bundle/lib:{run}/bundle/share",
                                           "<run>/bundle/lib:<run>/bundle/share"),
            "a repeat of the player's own": ("/a:/b:/a", "/a:/b:<run>/bundle/lib:<run>/bundle/share"),
            "a path with glob characters": ("/g*[x]?", "/g*[x]?:<run>/bundle/lib:<run>/bundle/share"),
            "a path with a space": ("/my games", "/my games:<run>/bundle/lib:<run>/bundle/share"),
        }
        for label, (value, expected) in cases.items():
            with self.subTest(label):
                def prepare(root, bin_dir, value=value):
                    return {} if value is None else {"PRESSURE_VESSEL_FILESYSTEMS_RO": value.format(run=root)}
                result, _ = self.launch(1280, 720, bundle=self.make_bundle(), prepare=prepare)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.seen["PRESSURE_VESSEL_FILESYSTEMS_RO"], expected)
                self.assertIn("PRESSURE_VESSEL_FILESYSTEMS_RO=%s\n" % expected, self.log)

    def test_a_symlinked_bundle_is_shared_with_containers_by_the_path_the_launcher_has(self):
        # pressure-vessel shares the target of a symlink along with the symlink, so the path is not resolved here.
        result, _ = self.launch(1280, 720, bundle=self.make_bundle(link=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seen["PRESSURE_VESSEL_FILESYSTEMS_RO"], "<run>/opt/current/lib:<run>/opt/current/share")
        self.assertEqual(self.seen["DISABLE_GAMESCOPE_WSI"], "1")

    def test_the_bundle_changes_the_layer_variables_and_nothing_else_in_the_environment(self):
        _, plain_args = self.launch(1280, 720, environment={"XDG_DATA_DIRS": "/usr/share"})
        plain = dict(self.seen)
        _, bundled_args = self.launch(1280, 720, environment={"XDG_DATA_DIRS": "/usr/share"}, bundle=self.make_bundle())
        bundled = dict(self.seen)
        self.assertEqual(bundled.pop("XDG_DATA_DIRS"), "<run>/bundle/share:/usr/share")
        self.assertEqual(plain.pop("XDG_DATA_DIRS"), "/usr/share")
        for name, value in BUNDLED.items():
            self.assertEqual(bundled.pop(name), value)
            self.assertNotIn(name, plain)
        self.assertEqual(bundled, plain)
        self.assertEqual(bundled_args, plain_args)

    def test_the_steam_environment_gets_no_flatpak_folders(self):
        # Desktop mode puts the exports of the Flatpak installations on XDG_DATA_DIRS and PATH (add_flatpak_exports of
        # the launcher). Steam's side keeps the environment that it was given, and the bundled layer stays its one
        # change, in front and alone, though Flatpak is there with exports of its own.
        def flatpak(root, bin_dir):
            system = root / "var/lib/flatpak"
            for installation in (system, root / ".local/share/flatpak"):
                for name in ("share", "bin"):
                    (installation / "exports" / name).mkdir(parents=True)
            stub = bin_dir / "flatpak"
            stub.write_text('#!/bin/sh\necho "%s"\n' % system)
            stub.chmod(0o755)
            return {"FLATPAK_SYSTEM_DIR": str(system)}
        for bundle in (None, self.make_bundle()):
            with self.subTest(bundle=bundle is not None):
                result, args = self.launch(1280, 720, bundle=bundle, prepare=flatpak,
                                           environment={"XDG_DATA_DIRS": "/usr/share"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.seen["XDG_DATA_DIRS"],
                                 "<run>/bundle/share:/usr/share" if bundle else "/usr/share")
                self.assertEqual(self.seen["PATH"], "<run>/bin")
                self.assertNotIn("FLATPAK_USER_DIR", self.seen)
                self.assertEqual(args[args.index("--") + 1], "steam")

    # Steam's Performance Overlay: mangoapp, started by Gamescope for this side only.
    def perf_launch(self, side="left", mangoapp=True, environment=None, enabled=False, **kwargs):
        def prepare(root, bin_dir):
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            if mangoapp:
                stub = bin_dir / "mangoapp"
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
            if enabled:
                (root / ".local/share/dosplex/perf").mkdir(parents=True)
                (root / ".local/share/dosplex/perf/enabled").write_text("")
            return {"XDG_RUNTIME_DIR": str(runtime)}
        return self.launch(1280, 720, side=side, prepare=prepare, environment=environment, **kwargs)

    def test_the_overlay_is_set_up_per_side_in_its_own_runtime(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                result, args = self.perf_launch(side)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--mangoapp", args[:args.index("--")])
                self.assertEqual(self.seen["MANGOHUD_CONFIGFILE"], "<run>/runtime/mangohud.config")
                self.assertEqual(self.files["config"], "no_display")
                self.assertEqual(self.files["config_uid"], os.getuid())
                for name in ("STEAM_MANGOAPP_PRESETS_SUPPORTED", "STEAM_USE_MANGOAPP",
                             "STEAM_DISABLE_MANGOAPP_ATOM_WORKAROUND", "STEAM_MANGOAPP_HORIZONTAL_SUPPORTED"):
                    self.assertEqual(self.seen[name], "1", name)
                self.assertNotIn("MANGOHUD_CONFIG", self.seen)

    def test_gamescope_runs_in_its_own_queue_directory_and_steam_keeps_its_directory(self):
        # ftok("mangoapp", 65) uses the "mangoapp" file in the working directory of Gamescope and of mangoapp.
        result, args = self.perf_launch(cwd=lambda root: root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gamescope_cwd, self.directory + "/runtime/mangoapp-ipc")
        self.assertTrue(self.files["marker"])
        self.assertEqual(self.files["cwd_mode"], 0o700)
        # The child of Gamescope returns to the launcher's directory before it starts Steam.
        child = args[args.index("--") + 1:]
        self.assertEqual(child[:2], ["/bin/sh", "-c"])
        self.assertIn("cd", child[2])
        self.assertIn("exec", child[2])
        self.assertEqual(child[4], self.directory)
        self.assertEqual(child[5], "steam")

    def test_without_mangoapp_the_side_launches_as_before(self):
        result, args = self.perf_launch(mangoapp=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--mangoapp", args)
        self.assertEqual(args[args.index("--") + 1], "steam")
        self.assertNotIn("MANGOHUD_CONFIGFILE", self.seen)
        self.assertNotIn("STEAM_USE_MANGOAPP", self.seen)
        self.assertIn("mangoapp is not installed", result.stderr)

    def test_the_overlay_can_be_switched_off(self):
        result, args = self.perf_launch(environment={"DOSPLEX_PERF_OVERLAY": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--mangoapp", args)
        self.assertNotIn("MANGOHUD_CONFIGFILE", self.seen)

    def test_desktop_mode_gets_no_overlay(self):
        result, _ = self.perf_launch(payload="desktop")
        # The desktop payload never reaches Gamescope (and needs Plasma, which this test host does not have).
        self.assertIsNone(self.seen)
        self.assertNotIn("mangoapp", self.log)
        self.assertNotIn("MANGOHUD", self.log)

    def test_logging_is_off_until_the_player_creates_the_enabled_file(self):
        result, _ = self.perf_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("MANGOHUD_CONFIG", self.seen)
        self.assertFalse(self.files["perf"])

    def test_logging_adds_keys_next_to_the_config_file_and_names_the_folder(self):
        result, _ = self.perf_launch(enabled=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seen["MANGOHUD_CONFIG"],
                         "read_cfg,output_folder=<run>/.local/share/dosplex/perf,autostart_log=1,log_interval=100")
        self.assertEqual(self.seen["MANGOHUD_CONFIGFILE"], "<run>/runtime/mangohud.config")
        self.assertTrue(self.files["perf"])

    def test_a_folder_name_with_a_comma_is_refused_for_logging(self):
        def prepare(root, bin_dir):
            folder = root / "a,b"
            folder.mkdir()
            (folder / "enabled").write_text("")
            stub = bin_dir / "mangoapp"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
            (root / "runtime").mkdir(mode=0o700)
            return {"XDG_RUNTIME_DIR": str(root / "runtime"), "DOSPLEX_PERF_DIR": str(folder)}
        result, args = self.launch(1280, 720, prepare=prepare)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--mangoapp", args)
        self.assertNotIn("MANGOHUD_CONFIG", self.seen)
        self.assertIn("cannot be used", result.stderr)

    def test_missing_dependency_does_not_launch_steam_unisolated_or_open_shell(self):
        for dependency in ('steam', 'gamescope', 'dbus-run-session'):
            with self.subTest(dependency=dependency):
                result, args = self.launch(1280, 720, missing=dependency)
                self.assertEqual(result.returncode, 1)
                self.assertIn('required command ' + dependency, result.stderr)
                self.assertIsNone(args)

    def test_unknown_payload_does_not_open_shell(self):
        result, args = self.launch(1280, 720, payload='typo')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Unsupported DosPlex session payload', result.stderr)
        self.assertIsNone(args)


if __name__ == "__main__":
    unittest.main()
