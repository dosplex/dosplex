#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import argparse
import contextlib
import copy
import fcntl
import shlex
import shutil
import signal
import stat
import tempfile
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

SIDE_ROOT = Path(os.environ.get("DOSPLEX_SIDE_ROOT", "/var/lib/dosplex/sides"))
STATE_PATH = Path(os.environ.get("DOSPLEX_AUDIO_STATE", "/run/dosplex/audio-routes.json"))
# What a player chose (claimed outputs, mix settings) survives restarts and reboots here. The router is the
# only writer; /run holds the runtime copy of the running trial. LEGACY_STATE_PATH is read once to migrate.
PERSIST_PATH = Path(os.environ.get("DOSPLEX_AUDIO_PERSIST", "/var/lib/dosplex/audio.json"))
LEGACY_STATE_PATH = Path(os.environ.get("DOSPLEX_AUDIO_LEGACY_STATE", "/run/dosplex/audio-routes.json"))
PERSIST_SCHEMA = 1
SERVER_USER = os.environ.get("DOSPLEX_AUDIO_SERVER_USER", "deck")
TCP_MODULE = "module-native-protocol-tcp"
TCP_ARGS = "listen=127.0.0.1 auth-ip-acl=127.0.0.1"
# The monitor says every change it makes to the audio graph in one line on standard error, which is the
# journal of the audio service, and says nothing on a pass that changes nothing.
REPEAT_SECONDS = 60         # a failure that goes on is said once a minute
STREAM_HOLD_SECONDS = 5     # how long a loopback stream may be muted or corked before the monitor acts
ATTACH_SECONDS = 1.0        # how long a loopback that has just been loaded has to be linked
RETRY_SECONDS = 30          # how long a loopback that would not link is left alone before it is loaded again
UNATTACHED = 4294967295     # the sink or source Pulse reports for a stream that is linked to nothing


def say(message):
    try:
        print(f"dosplex audio: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # a journal that cannot be written to must not stop the routing


def plain(text, limit=120):
    """Text from the audio server, a session file or an application, fit for one journal line."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(text))
    return text if len(text) <= limit else text[:limit - 3] + "..."


def user_runtime_dir(user):
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return None
    return f"/run/user/{uid}"


def run_pactl(*args, user=None, server=None):
    command = ["pactl", *args]
    env = os.environ.copy()
    if server:
        env["PULSE_SERVER"] = server
    if user and os.geteuid() == 0:
        runtime_dir = user_runtime_dir(user)
        user_env = []
        if runtime_dir:
            user_env.append(f"XDG_RUNTIME_DIR={runtime_dir}")
        if server:
            user_env.append(f"PULSE_SERVER={server}")
        command = ["sudo", "-u", user, "env", *user_env, "pactl", *args]
    return subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if command[0] == "sudo" else env,
        timeout=10,
    )


def ensure_user_audio_server():
    runtime_dir = user_runtime_dir(SERVER_USER)
    if not runtime_dir:
        return
    if os.geteuid() == 0:
        command = [
            "sudo",
            "-u",
            SERVER_USER,
            "env",
            f"XDG_RUNTIME_DIR={runtime_dir}",
            "systemctl",
            "--user",
            "start",
            "pipewire.service",
            "pipewire-pulse.service",
            "wireplumber.service",
        ]
    else:
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = runtime_dir
        command = [
            "systemctl",
            "--user",
            "start",
            "pipewire.service",
            "pipewire-pulse.service",
            "wireplumber.service",
        ]
    subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=None if os.geteuid() == 0 else env,
    )


def load_side_owner(side):
    state_file = SIDE_ROOT / side / "session.json"
    try:
        state = json.loads(state_file.read_text())
    except Exception:
        if side == "left":
            return os.environ.get("DOSPLEX_LEFT_USER", "deck")
        if side == "right":
            return os.environ.get("DOSPLEX_RIGHT_USER", "guest")
        return None
    return state.get("owner") or state.get("username") or state.get("user")


def physical_nodes(kind, nodes):
    """The sinks or sources among `nodes` that a player can choose: not Dosplex's own buses, and no monitors."""
    for device in nodes:
        name = device.get("name", "")
        # Virtual buses and playback monitors are never microphone choices.
        if not name or name.startswith("dosplex_"):
            continue
        if kind == "sources" and (name.endswith(".monitor") or
                device.get("monitor_of_sink_name")):
            continue
        yield device


def list_devices(kind, nodes=None):
    devices = []
    for device in physical_nodes(kind, query(kind) if nodes is None else nodes):
        props = device.get("properties", {})
        name = device.get("name", "")
        # The endpoint description distinguishes Speaker/Headphones/HDMI;
        # device.description can be the same card name for every connector.
        description = device.get("description") or props.get("device.description") or name
        devices.append({"id": name, "name": description,
                        "icon": "microphone" if kind == "sources" else audio_icon(props, name, description)})
    return devices


def list_sinks():
    return list_devices("sinks")


def query(kind):
    # Some pactl versions omit module indices from JSON output. The short form
    # supplies the actual Pulse module ID needed for exact unload operations.
    if kind == "modules":
        result = run_pactl("list", "short", "modules", user=SERVER_USER)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Cannot query modules")
        modules = []
        for line in result.stdout.splitlines():
            if not re.match(r'^\d+\t', line):
                # PipeWire includes multiline JSON-like native module args
                # even in pactl's "short" listing.
                if not modules:
                    raise ValueError("Invalid module list")
                modules[-1]['argument'] += '\n' + line.rstrip('\t')
                continue
            fields = line.split("\t")
            if len(fields) < 3 or not fields[0].isdigit():
                raise ValueError("Invalid module list")
            modules.append({"index": int(fields[0]), "name": fields[1], "argument": fields[2]})
        return modules
    result = run_pactl("-f", "json", "list", kind, user=SERVER_USER)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Cannot query {kind}")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise ValueError(f"Invalid {kind} response")
    return value


def pactl_checked(*args):
    result = run_pactl(*args, user=SERVER_USER)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Audio command failed: {args[0]}")
    return result.stdout.strip()


def audio_icon(props, name, description):
    haystack = " ".join(
        [
            str(props.get("device.bus", "")),
            str(props.get("device.form_factor", "")),
            name,
            description,
        ]
    ).lower()
    if "bluez" in haystack or "bluetooth" in haystack:
        return "headphones"
    if "hdmi" in haystack or "displayport" in haystack:
        return "display"
    if "headphone" in haystack or "headset" in haystack:
        return "headphones"
    return "speaker"


SIDES = ("left", "right")
KINDS = ("output", "input")
SEPARATION_GAINS = {"gentle": .25, "balanced": .125, "strong": .0625}


def empty_routes():
    # opted_out: the side chose "Use own output" while a shared mix is on, so a mix change
    # does not join it again. It lasts only as long as that mix.
    return {**{side: {"output": None, "input": None, "shared": False, "opted_out": False} for side in SIDES},
            "shared": {"output": None, "mode": "off", "separation": "balanced"}}


def load_routes():
    try:
        data = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return empty_routes()
    return parse_routes(data)


def parse_routes(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid audio route state")
    routes = empty_routes()
    for side in SIDES:
        entry = data.get(side, {})
        if isinstance(entry, str):  # Earlier output-only format.
            entry = {"output": entry}
        if not isinstance(entry, dict):
            raise ValueError("Invalid side audio route")
        for kind in KINDS:
            target = entry.get(kind)
            if target is not None and (not isinstance(target, str) or not target):
                raise ValueError("Invalid audio device ID")
            routes[side][kind] = target
        joined = entry.get("shared", False)
        if type(joined) is not bool:
            raise ValueError("Invalid shared participation")
        routes[side]["shared"] = joined
        opted_out = entry.get("opted_out", False)  # Absent in files from before auto-join.
        if type(opted_out) is not bool:
            raise ValueError("Invalid shared opt-out")
        routes[side]["opted_out"] = opted_out
    shared = data.get("shared", routes["shared"])
    if not isinstance(shared, dict) or shared.get("mode") not in ("off", "full", "separated"):
        raise ValueError("Invalid shared audio mode")
    target = shared.get("output")
    if target is not None and (not isinstance(target, str) or not target):
        raise ValueError("Invalid shared output")
    if shared["mode"] != "off" and not target:
        raise ValueError("Shared audio requires an output")
    separation = shared.get("separation", "balanced")
    if not isinstance(separation, str) or separation not in SEPARATION_GAINS:
        raise ValueError("Invalid stereo separation")
    routes["shared"] = {"output": target, "mode": shared["mode"], "separation": separation}
    return routes


def display_state():
    path = Path(os.environ.get("DOSPLEX_STATE_PATH", "/run/dosplex/state.json"))
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def shared_target(routes, side):
    shared = routes["shared"]
    return shared["output"] if shared["mode"] != "off" and routes[side]["shared"] else None


def shared_gains(routes, side):
    # Linear amplitude, not Pulse's cubic user-volume scale. Never amplify:
    # two coherent full-scale participants have at most unity combined gain.
    state = display_state()
    if routes["shared"]["mode"] == "separated" and state.get("layout") == "vertical":
        a, b = state.get("left", {}), state.get("right", {})
        if isinstance(a.get("x"), int) and isinstance(b.get("x"), int) and a["x"] != b["x"]:
            physical_left = "left" if a["x"] < b["x"] else "right"
            quieter = SEPARATION_GAINS[routes["shared"].get("separation", "balanced")]
            return (.5, quieter) if side == physical_left else (quieter, .5)
    # Horizontal/single/unknown placement has no meaningful left/right split.
    return (.5, .5)


UNITY_VOLUME = 65536
# WirePlumber stores the volume of a stream under its media name and gives it to the next
# stream with that name. A loopback is named after the id of its module and PipeWire reuses
# ids, so a new loopback can come up with the volume an earlier one was left at, for example
# a positioned mix's channel balance. WirePlumber 0.5 reads "state.restore-props" on a stream
# to skip this. "node.stream.restore-props" is the name of its global setting and does nothing
# on a stream, but loopbacks have always carried it, so it stays. The volume set after the
# load is what guarantees the result.
NO_RESTORE_PROPERTIES = ("node.stream.restore-props=false", "state.restore-props=false")
SHARED_PROPERTIES = ("dosplex.shared=true", "channelmix.max-volume=0.7071067811865476")


def loopback_properties(shared):
    return " ".join((*(SHARED_PROPERTIES if shared else ()), *NO_RESTORE_PROPERTIES))


def owned_by(stream, module_id):
    """Whether a stream belongs to the module with this id (a loopback has one of each kind)."""
    return (str(stream.get("owner_module")) == str(module_id)
            or str(stream.get("properties", {}).get("pulse.module.id")) == str(module_id))


def settle_loopback_volume(module_id, desired, stereo_only=True):
    """Set the volume of the sink input that loopback module `module_id` owns and wait until
    the server reports it. `desired` holds one Pulse volume per channel, or a single volume
    for every channel the stream has."""
    deadline = time.monotonic() + 2
    while True:
        all_streams = query("sink-inputs")
        streams = [s for s in all_streams if owned_by(s, module_id)]
        if len(streams) == 1:
            stream = streams[0]
            volume = stream.get("volume", {})
            if stereo_only and set(volume) != {"front-left", "front-right"}:
                raise RuntimeError("Shared output requires a stereo loopback")
            channels = ("front-left", "front-right") if stereo_only else tuple(volume)
            wanted = list(desired) * len(channels) if len(desired) == 1 else list(desired)
            actual = [volume[ch]["value"] for ch in channels]
            if channels and all(abs(a-b) <= 2 for a,b in zip(actual, wanted)):
                return
            if channels:
                pactl_checked("set-sink-input-volume", str(stream["index"]), *map(str, wanted))
        if time.monotonic() >= deadline:
            raise RuntimeError("Audio volume did not settle")
        time.sleep(.04)


def apply_shared_gain(module_id, gains):
    # PipeWire clamps both the master and channel gains. A sqrt(.5)
    # ceiling bounds their product to .5 from creation. Compensate for that
    # fixed master attenuation when setting the channel gains.
    ceiling = .7071067811865476
    desired = [round(65536 * (value / ceiling) ** (1 / 3)) for value in gains]
    settle_loopback_volume(module_id, desired)


def start_at_unity(module_id):
    # A private loopback follows the layout of its device (a microphone can be mono), so
    # every channel it has is set, not a stereo pair.
    try:
        settle_loopback_volume(module_id, [UNITY_VOLUME], stereo_only=False)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        # Never leave it playing at a volume nobody chose: the next pass loads it again.
        with contextlib.suppress(RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            pactl_checked("unload-module", str(module_id))
        raise


def persisted_document(routes):
    """The part of the routes a player chose. Inputs and anything recomputed from the server stay out."""
    sides = {}
    for side in SIDES:
        target = routes[side]["output"]
        sides[side] = {"output": {"node": target, "identity": None} if target else None,
                       "shared": routes[side]["shared"], "opted_out": routes[side]["opted_out"]}
    return {"schema": PERSIST_SCHEMA, "sides": sides, "shared": dict(routes["shared"])}


def routes_from_document(document):
    """Routes from a persistent document; raises ValueError when it is not one."""
    if not isinstance(document, dict) or document.get("schema") != PERSIST_SCHEMA:
        raise ValueError("Unknown audio persistence schema")
    sides = document.get("sides")
    if not isinstance(sides, dict):
        raise ValueError("Invalid persisted sides")
    data = {}
    for side in SIDES:
        entry = sides.get(side)
        if not isinstance(entry, dict):
            raise ValueError("Invalid persisted side")
        claim = entry.get("output")
        if claim is not None:
            if not isinstance(claim, dict) or not isinstance(claim.get("node"), str) or not claim["node"]:
                raise ValueError("Invalid persisted claim")
            claim = claim["node"]
        data[side] = {"output": claim, "shared": entry.get("shared", False),
                      "opted_out": entry.get("opted_out", False)}
    data["shared"] = document.get("shared")
    return parse_routes(data)


def write_persisted(routes):
    """Write the persistent file atomically, root-owned, mode 0600. Never raises: a copy that cannot be
    written is said in the journal and the runtime routes stay the truth."""
    document = persisted_document(routes)
    try:
        try:
            if json.loads(PERSIST_PATH.read_text()) == document:
                return
        except (OSError, ValueError):
            pass
        PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".audio-", dir=PERSIST_PATH.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(document, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, PERSIST_PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    except OSError as error:
        say(f"cannot write {PERSIST_PATH}: {plain(error)}")


def read_persisted():
    """The persisted routes, or None when there is no file. A file that cannot be read or understood is
    renamed to audio.json.corrupt-<time> and treated as absent."""
    try:
        text = PERSIST_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        reason = error
    else:
        try:
            return routes_from_document(json.loads(text))
        except ValueError as error:
            reason = error
    quarantine = PERSIST_PATH.with_name(f"{PERSIST_PATH.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10**9}")
    try:
        os.replace(PERSIST_PATH, quarantine)
        say(f"{PERSIST_PATH} is unusable ({plain(reason)}); moved to {quarantine.name}, starting from defaults")
    except OSError as error:
        say(f"{PERSIST_PATH} is unusable ({plain(reason)}) and cannot be moved aside: {plain(error)}")
    return None


def legacy_routes():
    """Claims of the pre-persistence route file, or None."""
    try:
        data = json.loads(LEGACY_STATE_PATH.read_text())
        return parse_routes(data)
    except (OSError, ValueError):
        return None


def initial_routes():
    """The routes a new trial starts with. Persisted choices win; else a legacy route file is migrated once;
    else Main gets the server's default output (the default itself is not changed) and Guest nothing.
    Under route_lock so a claim made meanwhile on the other side is not overwritten."""
    with route_lock():
        routes = read_persisted()
        if routes is None:
            routes = legacy_routes()
            if routes is not None:
                say(f"took audio claims from {LEGACY_STATE_PATH} once")
        if routes is None:
            routes = empty_routes()
            try:
                default = pactl_checked("get-default-sink")
                known = {d["id"] for d in list_devices("sinks")}
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
                default, known = "", set()
            if default and default in known:
                main = display_state().get("primary_side")
                routes[main if main in SIDES else "left"]["output"] = default
                say(f"first run: Main claims the default output {plain(default)}")
        save_routes(routes)
    return routes


@contextlib.contextmanager
def route_lock():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STATE_PATH) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_routes(routes):
    fd, name = tempfile.mkstemp(prefix=".audio-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(routes, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, STATE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    write_persisted(routes)


def bus(side, kind):
    return f"dosplex_{side}" if kind == "output" else f"dosplex_{side}_mic"


def ensure_per_side_sinks():
    existing = {item["name"] for item in query("sinks")}
    for side in SIDES:
        for kind in KINDS:
            name = bus(side, kind)
            if name not in existing:
                module = pactl_checked("load-module", "module-null-sink", f"sink_name={name}",
                                       f"sink_properties=device.description=Dosplex_{side}_{kind}")
                say(f"created bus {name} (module {module})")


def module_args(module):
    try:
        return dict(token.split("=", 1) for token in shlex.split(
            module.get("argument") or module.get("arguments") or "") if "=" in token)
    except ValueError:
        return {}


def managed_route(module):
    if module.get("name") != "module-loopback":
        return None
    args = module_args(module)
    for side in SIDES:
        if args.get("source") == bus(side, "output") + ".monitor":
            return side, "output"
        if args.get("sink") == bus(side, "input"):
            return side, "input"
    return None


class Monitor:
    """What the monitor remembers from one pass to the next, so that it can say what changed. A router
    that runs once (assign, ensure-shared) has no such memory and is given None instead."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.devices = None    # {"sinks": {name: index}, "sources": {name: index}} of the pass before
        self.since = {}        # a muted or corked stream to the time it was first seen so
        self.noted = set()     # the corked streams that were reported already
        self.retry_after = {}  # (side, kind) to the time before which its loopback is left alone
        self.said = {}         # what was said to [when, how often it was left out since]
        self.failing = {}      # where a failure goes on to the message that was said last

    def limited(self, key, text):
        """Say `text`. If `key` was said less than REPEAT_SECONDS ago, only count it."""
        now = self.clock()
        self.said = {k: v for k, v in self.said.items() if now - v[0] < 10 * REPEAT_SECONDS}
        entry = self.said.get(key)
        if entry and now - entry[0] < REPEAT_SECONDS:
            entry[1] += 1
            return
        left_out = entry[1] if entry else 0
        self.said[key] = [now, 0]
        say(f"{text} (repeated {left_out} times since it was last said)" if left_out else text)

    def failure(self, source, message):
        """Say that `source` failed, at most once a minute while the message stays the same."""
        message = plain(" ".join(str(message).split()), 400)
        self.failing[source] = message
        self.limited(("failure", source, message), message)

    def recovered(self, source):
        message = self.failing.pop(source, None)
        if message is not None:
            say(f"recovered: {message}")

    def observe_devices(self, current):
        """Say which sinks and sources that a player can choose came, went or were created again since the
        pass before. The first pass has nothing to compare with and says nothing."""
        before, self.devices = self.devices, current
        if before is None:
            return
        for kind in ("sinks", "sources"):
            word = kind[:-1]
            for name in sorted(before[kind].keys() - current[kind].keys()):
                say(f"{word} {plain(name)} disappeared (was index {before[kind][name]})")
            for name in sorted(current[kind].keys() - before[kind].keys()):
                say(f"{word} {plain(name)} appeared (index {current[kind][name]})")
            for name in sorted(before[kind].keys() & current[kind].keys()):
                if before[kind][name] != current[kind][name]:
                    say(f"{word} {plain(name)} was re-created (index {before[kind][name]} -> {current[kind][name]})")

    def watch_streams(self, route, label, module_id, sink_inputs, source_outputs, abnormal):
        """Look at the streams of a loopback that carries `route`. With PipeWire they run, neither corked
        nor muted, whether or not anything plays (tests/audio-integration.py), so either state is wrong.
        A stream that stays muted for more than STREAM_HOLD_SECONDS is unmuted where it is: loading the
        loopback again would not cure it, because WirePlumber gives a new capture stream the mute of the
        one before. A corked stream is only reported: nothing here can make one, so nothing is known to
        cure it. `abnormal` collects the states seen, so that the others can be forgotten."""
        now = self.clock()
        for role, streams, unmute in (("playback", sink_inputs, "set-sink-input-mute"),
                                      ("capture", source_outputs, "set-source-output-mute")):
            for stream in streams:
                if not owned_by(stream, module_id):
                    continue
                for state, field in (("muted", "mute"), ("corked", "corked")):
                    key = (route, module_id, role, state)
                    if stream.get(field) is not True:
                        self.since.pop(key, None)
                        self.noted.discard(key)
                        continue
                    abnormal.add(key)
                    first = self.since.setdefault(key, now)
                    if now - first <= STREAM_HOLD_SECONDS:
                        continue
                    if state == "muted":
                        pactl_checked(unmute, str(stream["index"]), "0")
                        self.limited(("unmute", route, role),
                                     f"unmuted the {role} stream of the {label} loopback (module {module_id}), "
                                     f"muted for more than {STREAM_HOLD_SECONDS} seconds")
                        del self.since[key]
                    elif key not in self.noted:
                        self.noted.add(key)
                        say(f"the {role} stream of the {label} loopback (module {module_id}) has been corked for "
                            f"more than {STREAM_HOLD_SECONDS} seconds; left as it is")

    def forget_except(self, abnormal):
        self.since = {k: v for k, v in self.since.items() if k in abnormal}
        self.noted &= abnormal


def loopback_problem(module_id, source, sink, nodes, sink_inputs, source_outputs):
    """Whether loopback module `module_id`, which is to carry sound from `source` to `sink`, can do it.
    Its capture stream has to be on the source and its playback stream on the sink, judged by the index
    the server gives those nodes now and not by their name: a sink that was removed and created again
    keeps its name and gets a new index, and a stream that was loaded against the old one is on nothing.
    The loopback is loaded with sink_dont_move, so nothing links it again, and its arguments still match.
    `nodes` maps the names of the sinks and of the sources to their indices.

    Returns None, or (state, text): state is "missing" for a stream that is not there and "detached"
    for one that is on another node or on none. A node that is not there itself is not the loopback's fault
    and cannot be cured by loading it again, so it is not judged."""
    for role, field, name, live, streams in (("playback", "sink", sink, nodes["sinks"], sink_inputs),
                                              ("capture", "source", source, nodes["sources"], source_outputs)):
        wanted = live.get(name)
        if wanted is None:
            continue
        found = [s for s in streams if owned_by(s, module_id)]
        if not found:
            return "missing", f"no {role} stream"
        for stream in found:
            current = stream.get(field)
            if current != wanted:
                known = {index: node for node, index in live.items()}
                if current in (None, UNATTACHED):
                    where = f"no {field}"
                elif current in known:
                    where = f"{plain(known[current])} (index {current})"
                else:
                    where = f"{field} index {current}"
                return "detached", f"{role} stream is on {where}, not on {plain(name)} (index {wanted})"
    return None


def wait_loopback_attached(module_id, source, sink, nodes):
    """Give a loopback that has just been loaded time to be linked, so that a stream that is on nothing
    later on is a fault and not a loopback that is still starting. Returns what is still wrong, or None."""
    deadline = time.monotonic() + ATTACH_SECONDS
    while True:
        problem = loopback_problem(module_id, source, sink, nodes, query("sink-inputs"), query("source-outputs"))
        if problem is None or time.monotonic() >= deadline:
            return problem
        time.sleep(.04)


def loopback_line(action, label, endpoints, module_id, reason):
    source, sink = endpoints
    return f"{action} loopback {label} {plain(source)} -> {plain(sink)} (module {module_id}), reason: {reason}"


def default_output(available):
    """The shared server's default output when a side can play on it, else None. A claimed output that went
    away falls back to it, so the side is not silent; the claim itself stays and wins again when the device is back."""
    try:
        default = pactl_checked("get-default-sink")
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        return None
    return default if default in available else None


def ensure_loopbacks(routes=None, monitor=None):
    """Load, keep and unload the loopbacks the routes ask for, and say each change. `monitor` is the memory
    of the long-running monitor, which it uses to report devices that came and went and streams that stay
    muted or corked, and to leave a loopback that would not link alone for a while."""
    routes = load_routes() if routes is None else routes
    sinks, sources = query("sinks"), query("sources")
    available = {"output": {d["id"] for d in list_devices("sinks", sinks)},
                 "input": {d["id"] for d in list_devices("sources", sources)}}
    nodes = {"sinks": {n["name"]: n["index"] for n in sinks},
             "sources": {n["name"]: n["index"] for n in sources}}
    if monitor:
        monitor.observe_devices({kind: {n["name"]: n["index"] for n in physical_nodes(kind, found)}
                                 for kind, found in (("sinks", sinks), ("sources", sources))})
    modules = query("modules")
    streams = None  # the sink inputs and source outputs, fetched when there is a loopback to look at
    abnormal = set()
    for side in SIDES:
        for kind in KINDS:
            route = (side, kind)
            mixed = shared_target(routes, side) if kind == "output" else None
            target = mixed or routes[side][kind]
            label = f"{side} {kind}" + (" (shared mix)" if mixed else "")
            # A shared participant with a disconnected shared output must stay
            # silent; never fall back to a headset or somebody else's speakers.
            desired = None
            try:
                lifecycle = json.loads((SIDE_ROOT / side / 'session.json').read_text()).get('lifecycle')
            except (OSError, ValueError):
                lifecycle = None
            if lifecycle in ('standby', 'off', 'sleeping'):
                idle = f"side is {plain(lifecycle)}"
            elif not target:
                idle = "route released"
            else:
                fallback = None
                if target not in available[kind] and kind == "output" and not mixed:
                    # A claimed output that is gone: play on the shared default until it is back.
                    fallback = default_output(available["output"])
                if target not in available[kind] and not fallback:
                    idle = f"{plain(target)} is not available"
                else:
                    idle = None
                    endpoint = fallback or target
                    desired = ((bus(side, kind) + ".monitor", endpoint) if kind == "output"
                               else (endpoint, bus(side, kind)))
            kept = False
            gain_module = None
            replaced = None  # why an earlier loopback of this route went, if one did
            for module in modules:
                if managed_route(module) != route:
                    continue
                args = module_args(module)
                actual = args.get("source"), args.get("sink")
                properties = args.get("sink_input_properties", "")
                if desired is None:
                    why = idle
                elif kept:
                    why = "duplicate loopback"
                elif desired != actual:
                    why = "arguments changed (source or sink)"
                elif bool(mixed) != ("dosplex.shared=true" in properties):
                    why = f"arguments changed (shared mix {'on' if mixed else 'off'})"
                elif not (args.get("source_dont_move") == "true" and args.get("sink_dont_move") == "true"
                          and all(flag in properties.split() for flag in NO_RESTORE_PROPERTIES)):
                    # A loopback from before the restore flags existed is loaded again.
                    why = "arguments changed (stream flags)"
                else:
                    why = None
                    if streams is None:
                        streams = (query("sink-inputs"), query("source-outputs"))
                    if not (monitor and monitor.clock() < monitor.retry_after.get(route, 0)):
                        problem = loopback_problem(module["index"], desired[0], desired[1], nodes, *streams)
                        if problem:
                            why = f"stream {problem[0]} ({problem[1]})"
                    if why is None and monitor:
                        monitor.watch_streams(route, label, module["index"], *streams, abnormal)
                if why is None:
                    kept = True
                    gain_module = module["index"]
                else:
                    pactl_checked("unload-module", str(module["index"]))
                    say(loopback_line("unloaded", label, actual, module["index"], why))
                    if desired is not None and replaced is None:
                        replaced = why.split(" (")[0]  # "stream detached", without the detail in brackets
            loaded = False
            if desired and not kept:
                source, sink = desired
                options = [f'sink_input_properties="{loopback_properties(bool(mixed))}"']
                if mixed:
                    # Clamp from stream creation, before gain configuration, so
                    # graph changes cannot briefly play both sources at unity.
                    options = ["channels=2", "channel_map=front-left,front-right", *options]
                gain_module = pactl_checked("load-module", "module-loopback", f"source={source}",
                              f"sink={sink}", "source_dont_move=true", "sink_dont_move=true",
                              "latency_msec=30", *options)
                loaded = True
                say(loopback_line("loaded", label, desired, gain_module, replaced or "new route"))
            if desired and mixed:
                apply_shared_gain(gain_module, shared_gains(routes, side))
            elif desired and not kept:
                start_at_unity(gain_module)
            if loaded:
                # It takes a few tens of milliseconds to be linked. Wait for that here, while the route lock
                # is held, so that no other pass takes a loopback that is still starting for a broken one.
                problem = wait_loopback_attached(gain_module, source, sink, nodes)
                if monitor:
                    monitor.retry_after.pop(route, None)
                if problem:
                    text = (f"the {label} loopback (module {gain_module}) was not linked within {ATTACH_SECONDS:g} "
                            f"seconds of loading: {problem[1]}")
                    if monitor:
                        monitor.retry_after[route] = monitor.clock() + RETRY_SECONDS
                        monitor.limited(("attach", route), text + f"; leaving it for {RETRY_SECONDS} seconds")
                    else:
                        say(text)
    if monitor:
        monitor.forget_except(abnormal)


def stream_side(stream):
    props = stream.get("properties", {})
    # Session launch tags distinguish sides even when account names match.
    tagged = props.get("dosplex.side")
    if tagged in SIDES:
        return tagged
    owner = props.get("application.process.user")
    matches = [side for side in SIDES if owner and load_side_owner(side) == owner]
    return matches[0] if len(matches) == 1 else None


def application_name(stream):
    props = stream.get("properties", {})
    return (props.get("application.name") or props.get("application.process.binary")
            or props.get("media.name") or "unknown application")


def route_streams():
    moved = 0
    for kind, collection, command, field in (
        ("output", "sink-inputs", "move-sink-input", "sink"),
        ("input", "source-outputs", "move-source-output", "source"),
    ):
        nodes = query("sinks" if kind == "output" else "sources")
        indices = {n["name"]: n["index"] for n in nodes}
        names = {n["index"]: n["name"] for n in nodes}
        for stream in query(collection):
            # Loopback streams must retain their physical endpoints.
            if stream.get("owner_module") not in (None, 4294967295):
                continue
            side = stream_side(stream)
            if not side:
                continue
            target = bus(side, kind) + (".monitor" if kind == "input" else "")
            if target not in indices:
                raise RuntimeError(f"Audio bus missing: {target}")
            if stream.get(field) != indices[target]:
                try:
                    pactl_checked(command, str(stream["index"]), target)
                except RuntimeError as error:
                    # The stream ended between the listing and the move (Steam closing, a game
                    # exiting): nothing to route any more, and not a reason to fail a start.
                    if "No such entity" not in str(error):
                        raise
                    say(f'skipped {side} {kind} stream "{plain(application_name(stream), 60)}" '
                        f'(index {stream["index"]}): it ended before it could be moved')
                    continue
                moved += 1
                current = stream.get(field)
                where = (names.get(current) or (f"no {field}" if current in (None, UNATTACHED)
                                                else f"{field} index {current}"))
                say(f'moved {side} {kind} stream "{plain(application_name(stream), 60)}" '
                    f'(index {stream["index"]}): {plain(where)} -> {target}')
    return moved


# --- Guest audio endpoint (T-310) -------------------------------------------
# Guest's Steam lists every device of the server it talks to. Making Guest a
# restricted client of Main's server does not fix that. Checked on PipeWire
# 1.2.4 with WirePlumber 0.5.15: such a client still sees every node, can set
# defaults and volumes and load modules, and can lift its own restriction with
# PULSE_PROP=pipewire.client.access=unrestricted. Guest therefore gets a server
# of its own: PipeWire, pipewire-pulse and WirePlumber running as Guest in a
# directory only Guest can enter, whose only devices are two tunnels to the
# Guest bus and the Guest microphone bus on the shared server. Main's server and
# Main's path are not touched. The shared server's TCP listener stays until
# T-301, so this limits what Guest's Steam is offered. It does not stop a
# hostile process running as Guest from using that listener.
GUEST_SIDE = "right"
GUEST_UPSTREAM = "tcp:127.0.0.1:4713"
GUEST_LATENCY_MS = 60
GUEST_RESTART_SECONDS = 5
GUEST_SOUND_LABEL = "Dosplex sound"
GUEST_MIC_LABEL = "Dosplex microphone"
CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
UPSTREAM_PATTERN = re.compile(r"unix:/[A-Za-z0-9_./+-]+|tcp:[A-Za-z0-9_.-]+(?::[0-9]{1,5})?")

GUEST_PIPEWIRE_HEAD = """# Generated by dosplex-audio-router.py: the private audio server of the Guest side.
context.properties = {
    core.daemon = true
    core.name = pipewire-0
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-link-factory }
    { name = libpipewire-module-session-manager flags = [ ifexists nofail ] }
    { name = libpipewire-module-access }
"""
GUEST_PIPEWIRE_TAIL = """]
context.objects = [
    { factory = spa-node-factory args = { factory.name = support.node.driver node.name = Dummy-Driver priority.driver = 20000 } }
]
"""
# No pulse.cmd: module-always-sink would invent a fallback device for Guest.
GUEST_PULSE_CONFIG = """# Generated by dosplex-audio-router.py: the Pulse endpoint of the Guest side.
context.properties = { }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-protocol-pulse args = { } }
]
pulse.properties = {
    server.address = [ "unix:native" ]
    pulse.allow-module-loading = false
}
"""


def guest_node_names(side=GUEST_SIDE):
    """What the Guest server calls its two devices. The microphone is not called
    <bus>.monitor: Pulse clients read that name as the monitor of a sink and would
    record Guest's own sound instead of the microphone."""
    return bus(side, "output"), bus(side, "input")


def check_guest_settings(upstream, side, latency_ms):
    if side not in SIDES:
        raise ValueError("Invalid Guest audio side")
    if not isinstance(upstream, str) or not UPSTREAM_PATTERN.fullmatch(upstream):
        raise ValueError("Invalid Guest audio upstream address")
    if type(latency_ms) is not int or not 20 <= latency_ms <= 500:
        raise ValueError("Invalid Guest audio latency")


def guest_tunnel(mode, node, target, label, upstream, latency_ms):
    # JSON strings are valid SPA-JSON strings, so json.dumps quotes every value.
    return f"""    {{ name = libpipewire-module-pulse-tunnel
      args = {{
        tunnel.mode = {mode}
        pulse.server.address = {json.dumps(upstream)}
        pulse.latency = {latency_ms}
        reconnect.interval.ms = 1000
        target.object = {json.dumps(target)}
        audio.format = F32
        audio.rate = 48000
        audio.channels = 2
        audio.position = [ FL FR ]
        stream.props = {{
          node.name = {json.dumps(node)}
          node.description = {json.dumps(label)}
        }}
      }}
    }}
"""


def guest_pipewire_config(upstream=GUEST_UPSTREAM, side=GUEST_SIDE, latency_ms=GUEST_LATENCY_MS):
    check_guest_settings(upstream, side, latency_ms)
    sink, source = guest_node_names(side)
    return (GUEST_PIPEWIRE_HEAD
            + guest_tunnel("sink", sink, bus(side, "output"), GUEST_SOUND_LABEL, upstream, latency_ms)
            + guest_tunnel("source", source, bus(side, "input") + ".monitor", GUEST_MIC_LABEL, upstream, latency_ms)
            + GUEST_PIPEWIRE_TAIL)


def guest_pactl(server, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PULSE_")}
    env["PULSE_SERVER"] = server
    return subprocess.run(["pactl", *args], check=False, text=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


def guest_names(server, kind):
    result = guest_pactl(server, "-f", "json", "list", kind)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Cannot list {kind}")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise ValueError(f"Invalid {kind} response")
    return sorted(str(item.get("name", "")) for item in value if isinstance(item, dict))


def guest_endpoint_problems(server, side=GUEST_SIDE):
    """Say what is wrong with a Guest endpoint. An empty list means it offers
    exactly one output and one microphone and refuses module loading."""
    if side not in SIDES:
        raise ValueError("Invalid Guest audio side")
    sink, source = guest_node_names(side)
    try:
        info = guest_pactl(server, "info")
        if info.returncode:
            return ["the endpoint does not answer: " + (info.stderr.strip() or "no reply")]
        sinks = guest_names(server, "sinks")
        sources = guest_names(server, "sources")
        attempt = guest_pactl(server, "load-module", "module-null-sink", "sink_name=dosplex_guest_probe")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        return [f"the endpoint cannot be checked: {error}"]
    problems = []
    if sinks != [sink]:
        problems.append(f"outputs are {sinks}, expected only {[sink]}")
    if source not in sources or set(sources) - {source, sink + ".monitor"}:
        problems.append(f"inputs are {sources}, expected {source} and at most {sink}.monitor")
    if attempt.returncode == 0:
        problems.append("module loading is not refused")
        try:
            guest_pactl(server, "unload-module", attempt.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return problems


class GuestEndpoint:
    """The private audio server of the Guest side: a PipeWire daemon, a
    pipewire-pulse server and WirePlumber, run as Guest.

    The runtime directory belongs to Guest (mode 0700), so only Guest can reach
    the sockets. The configuration lives next to it and belongs to the process
    that starts the endpoint (root in the product), so Guest cannot loosen it.
    """

    def __init__(self, directory, upstream=GUEST_UPSTREAM, side=GUEST_SIDE,
                 latency_ms=GUEST_LATENCY_MS, owner=None, config_directory=None, log=None):
        self.directory = Path(directory)
        self.config_directory = Path(config_directory) if config_directory else \
            self.directory.with_name(self.directory.name + ".conf")
        if not (self.directory.is_absolute() and self.config_directory.is_absolute()) or not self.directory.name:
            raise ValueError("The Guest audio directory must be an absolute path")
        self.owner, self.log = owner, log
        # Reject bad settings now, before anything is started.
        self.files = {"pipewire.conf": guest_pipewire_config(upstream, side, latency_ms),
                      "pipewire-pulse.conf": GUEST_PULSE_CONFIG}
        self.children = []
        self.retry_at = 0.0

    @property
    def server(self):
        return "unix:" + str(self.directory / "pulse" / "native")

    def identity(self):
        if os.geteuid() != 0:
            return os.geteuid(), os.getegid()
        if self.owner is None or self.owner.pw_uid < 1000:
            raise RuntimeError("The Guest audio endpoint needs an ordinary Guest account")
        return self.owner.pw_uid, self.owner.pw_gid

    @staticmethod
    def trusted_parent(path):
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or \
                info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError(f"Untrusted Guest audio parent directory: {path}")

    def prepare(self):
        uid, gid = self.identity()
        for parent in {self.directory.parent, self.config_directory.parent}:
            self.trusted_parent(parent)
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            pass
        # Work on the open directory, never on the path: Guest owns what is inside.
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        try:
            os.mkdir(self.config_directory, 0o755)
        except FileExistsError:
            pass
        info = self.config_directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or \
                info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PermissionError("Untrusted Guest audio configuration directory")
        self.config_directory.chmod(0o755)
        for name, text in self.files.items():
            temporary = self.config_directory / ("." + name + ".new")
            temporary.write_text(text)
            temporary.chmod(0o644)
            os.replace(temporary, self.config_directory / name)

    def environment(self):
        # Nothing is inherited: the router may carry state Guest must not see.
        return {"PATH": CHILD_PATH, "HOME": str(self.directory),
                "XDG_RUNTIME_DIR": str(self.directory), "PIPEWIRE_RUNTIME_DIR": str(self.directory),
                "XDG_CONFIG_HOME": str(self.directory / "config"),
                "XDG_STATE_HOME": str(self.directory / "state"),
                "XDG_CACHE_HOME": str(self.directory / "cache"),
                "XDG_DATA_HOME": str(self.directory / "data")}

    def launch(self, name, argv):
        if not argv[0]:
            raise RuntimeError(f"{name} is not installed")
        drop = {}
        if os.geteuid() == 0:
            uid, gid = self.identity()
            drop = {"user": uid, "group": gid, "extra_groups": []}
        process = subprocess.Popen(argv, env=self.environment(), cwd=self.directory,
                                   stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                   start_new_session=True, close_fds=True, **drop)
        self.children.append((name, process))

    def wait_for_socket(self, path, timeout):
        deadline = time.monotonic() + timeout
        while not path.is_socket():
            for name, process in self.children:
                if process.poll() is not None:
                    raise RuntimeError(f"The Guest audio endpoint's {name} exited with status {process.returncode}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"The Guest audio endpoint's {path.name} did not appear")
            time.sleep(.05)

    def wait_for_exit(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for name, process in self.children:
                if process.poll() is not None:
                    raise RuntimeError(f"The Guest audio endpoint's {name} exited with status {process.returncode}")
            time.sleep(.05)

    def start(self, timeout=10.0):
        self.stop()
        self.prepare()
        try:
            self.launch("pipewire", [shutil.which("pipewire", path=CHILD_PATH),
                                     "-c", str(self.config_directory / "pipewire.conf")])
            self.wait_for_socket(self.directory / "pipewire-0", timeout)
            self.launch("pipewire-pulse", [shutil.which("pipewire-pulse", path=CHILD_PATH),
                                           "-c", str(self.config_directory / "pipewire-pulse.conf")])
            self.wait_for_socket(self.directory / "pulse" / "native", timeout)
            self.launch("wireplumber", [shutil.which("wireplumber", path=CHILD_PATH), "--profile", "policy"])
            # A WirePlumber that cannot start (an unknown profile, say) exits at once.
            self.wait_for_exit(1.0)
        except BaseException:
            self.retry_at = time.monotonic() + GUEST_RESTART_SECONDS
            self.stop()
            raise
        # However it ends, a restart is not tried again at once.
        self.retry_at = time.monotonic() + GUEST_RESTART_SECONDS

    def running(self):
        return bool(self.children) and all(process.poll() is None for _, process in self.children) \
            and (self.directory / "pulse" / "native").is_socket()

    def ensure(self):
        """Start the endpoint if it is not running. Returns True when it was (re)started."""
        if self.running() or time.monotonic() < self.retry_at:
            return False
        if self.children:
            print("dosplex audio: Guest endpoint stopped; restarting", file=sys.stderr, flush=True)
        self.start()
        return True

    def stop(self):
        children, self.children = self.children, []
        for _, process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for _, process in reversed(children):
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def guest_endpoint_from_environment():
    """The Guest endpoint the environment asks for, or None. DOSPLEX_GUEST_AUDIO_DIR
    switches it on, so only an audio service that is meant to provide it does."""
    directory = os.environ.get("DOSPLEX_GUEST_AUDIO_DIR", "")
    if not directory:
        return None
    owner = None
    if os.geteuid() == 0:
        owner = pwd.getpwnam(load_side_owner(GUEST_SIDE) or "guest")
    return GuestEndpoint(Path(directory),
                         upstream=os.environ.get("DOSPLEX_GUEST_AUDIO_UPSTREAM", GUEST_UPSTREAM),
                         latency_ms=int(os.environ.get("DOSPLEX_GUEST_AUDIO_LATENCY_MS", GUEST_LATENCY_MS)),
                         owner=owner)


def ensure_shared_audio(monitor=None):
    ensure_user_audio_server()
    # Kept for the existing experimental launcher. Local TCP remains a release
    # blocker: stream properties are routing hints, not an authorization boundary.
    # Main's launcher, the trial's forwarder and Guest's endpoint (as its upstream)
    # still use it; Guest's Steam never talks to it.
    modules = query("modules")
    if not any(m.get("name") == TCP_MODULE for m in modules):
        module = pactl_checked("load-module", TCP_MODULE, *TCP_ARGS.split())
        say(f"loaded {TCP_MODULE} (module {module})")
    with route_lock():
        ensure_per_side_sinks()
        ensure_loopbacks(monitor=monitor)
        route_streams()
    return 0


def monitor_shared_audio(monitor=None):
    """Keep the audio graph as the routes say, once a second. Every change is said in one line, and a
    failure that goes on is said once a minute (see Monitor)."""
    monitor = monitor or Monitor()
    try:
        endpoint = guest_endpoint_from_environment()
    except (KeyError, ValueError, OSError) as error:
        endpoint = None
        say(f"Guest endpoint is not available: {error}")
    if endpoint:
        # Stop the endpoint's processes with the monitor, not after a kill.
        signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
    try:
        while True:
            try:
                ensure_shared_audio(monitor)
                monitor.recovered("audio")
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                monitor.failure("audio", error)
            if endpoint:
                try:
                    endpoint.ensure()
                    monitor.recovered("guest endpoint")
                except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                    monitor.failure("guest endpoint", f"Guest endpoint: {error}")
            time.sleep(1)
    finally:
        if endpoint:
            endpoint.stop()


def assign_audio(side, device, kind="output"):
    if kind not in KINDS or side not in (*SIDES, "free"):
        raise ValueError("Invalid audio side or direction")
    target = None if device == "none" else device
    with route_lock():
        routes = load_routes()
        previous = copy.deepcopy(routes)
        if side == "free":
            for name in SIDES:
                if routes[name][kind] == target:
                    if kind == "output" and target is not None:
                        routes[name]["opted_out"] = False
                    routes[name][kind] = None
        else:
            if target is not None:
                choices = list_devices("sinks" if kind == "output" else "sources")
                if target not in {d["id"] for d in choices}:
                    raise ValueError("Audio device is unavailable")
                if kind == "output" and routes["shared"]["mode"] != "off" and routes["shared"]["output"] == target:
                    raise ValueError("This output is shared; join shared speakers instead")
                other = "right" if side == "left" else "left"
                if routes[other][kind] == target:
                    raise ValueError("Device belongs to the other side; release it there first")
            if kind == "output" and routes[side][kind] != target:
                # Claiming or releasing an output of their own ends the player's opt-out.
                routes[side]["opted_out"] = False
            routes[side][kind] = target
            if kind == "output":
                routes[side]["shared"] = False
        commit_routes(routes, previous)
    return 0


def commit_routes(routes, previous):
    ensure_per_side_sinks()
    try:
        ensure_loopbacks(routes)
        route_streams()
        # Publish only after the requested graph is installed. Otherwise a
        # failed device change appears selected and replaces the last route.
        save_routes(routes)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        try:
            ensure_loopbacks(previous)
            route_streams()
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as recovery_error:
            raise RuntimeError(f"Audio change failed; previous route restoration also failed: {recovery_error}") from error
        raise

def change_mix(side, action):
    if side not in SIDES or action not in ("off", "full", "separated", "join", "leave", *SEPARATION_GAINS):
        raise ValueError("Invalid shared audio request")
    with route_lock():
        routes = load_routes()
        previous = copy.deepcopy(routes)
        if action in ("off", "full", "separated", *SEPARATION_GAINS):
            if display_state().get("primary_side") != side:
                raise ValueError("Only Main can change shared speakers")
            if action == "off":
                routes["shared"].update(mode="off", output=None)
                for name in SIDES:
                    routes[name]["shared"] = False
                    routes[name]["opted_out"] = False
            else:
                target = routes["shared"]["output"] if routes["shared"]["mode"] != "off" else routes[side]["output"]
                if target not in {d["id"] for d in list_sinks()}:
                    raise ValueError("Claim an output first, then choose a shared mix")
                other = "right" if side == "left" else "left"
                if routes[other]["output"] == target and not routes[other]["shared"]:
                    raise ValueError("The other player owns that output")
                routes["shared"].update(mode="separated" if action in SEPARATION_GAINS else action, output=target)
                if action in SEPARATION_GAINS:
                    routes["shared"]["separation"] = action
                routes[side]["shared"] = True
                routes[side]["opted_out"] = False
                # A player without an output of their own joins at once: nobody has to know
                # that Audio has a join button. A claimed output is kept, and "Use own output"
                # keeps a player out for as long as the mix stays on.
                for name in SIDES:
                    if routes[name]["output"] is None and not routes[name]["opted_out"]:
                        routes[name]["shared"] = True
        elif action == "join":
            if routes["shared"]["mode"] == "off":
                raise ValueError("Main has not enabled shared speakers")
            routes[side]["shared"] = True
            routes[side]["opted_out"] = False
        else:
            # If the old private claim became the shared output, leaving must
            # not create a second independent path into those same speakers.
            if routes[side]["output"] == routes["shared"]["output"] and routes["shared"]["mode"] != "off":
                routes[side]["output"] = None
            routes[side]["shared"] = False
            routes[side]["opted_out"] = routes["shared"]["mode"] != "off"
        commit_routes(routes, previous)
    return 0


def status():
    routes = load_routes()
    devices = {"output": list_sinks(), "input": list_devices("sources")}
    result = {"routes": routes, "devices": devices, "sides": {}}
    for side in SIDES:
        result["sides"][side] = {}
        for kind in KINDS:
            target = (shared_target(routes, side) if kind == "output" else None) or routes[side][kind]
            present = target in {d["id"] for d in devices[kind]}
            result["sides"][side][kind] = {
                "device": target,
                "state": "selected" if present else "disconnected" if target else "muted",
            }
    return result


def overlay_snapshot(side=None, kind=None, device=None):
    """Bounded tab-separated UI protocol; hex fields preserve IDs without quoting."""
    lines = ["DOSPLEX_AUDIO_3"]
    def encoded(value):
        text = str(value)
        if len(text.encode("utf-8")) >= 256:
            raise ValueError("Audio device identifier or label is too long")
        return text.encode("utf-8").hex() or "-"
    try:
        if side is not None:
            if kind == "mix":
                change_mix(side, device)
            else:
                assign_audio(side, device, kind)
        current = status()
        shared = current["routes"]["shared"]
        members = "".join("1" if current["routes"][s]["shared"] else "0" for s in SIDES)
        effective = "separated" if shared["mode"] == "separated" and shared_gains(current["routes"], "left") != (.5, .5) else "full"
        lines.append(f"M\t{shared['mode']}\t{members}\t{encoded(shared['output'] or '')}\t{effective}")
        lines.append(f"P\t{shared['separation']}\t-\t-\t-")
        for current_side in SIDES:
            for direction in KINDS:
                entry = current["sides"][current_side][direction]
                lines.append(f"S\t{current_side}\t{direction}\t{entry['state']}\t{encoded(entry['device'] or '')}")
        for direction in KINDS:
            for entry in current["devices"][direction][:64]:
                owner = next((s for s in SIDES if current["routes"][s][direction] == entry['id']), "free")
                if direction == "output" and shared["mode"] != "off" and entry['id'] == shared["output"]:
                    owner = "shared"
                lines.append(f"D\t{direction}\t{owner}\t{encoded(entry['id'])}\t{encoded(entry['name'])}")
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        # Keep errors bounded and encoded; never let device text become commands.
        message = str(error).encode("utf-8")[:240].decode("utf-8", errors="ignore")
        lines = ["DOSPLEX_AUDIO_3", "E\t" + encoded(message)]
    return "\n".join(lines) + "\nEND\n"


def main():
    parser = argparse.ArgumentParser(description="Dosplex PipeWire/PulseAudio side router")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-sinks")
    sub.add_parser("list-sources")
    sub.add_parser("ensure-shared")
    sub.add_parser("monitor-shared")
    sub.add_parser("status")
    guest_check = sub.add_parser("guest-endpoint-check",
                                 help="verify that a Guest endpoint offers one output and one microphone")
    guest_check.add_argument("--server", default=os.environ.get("DOSPLEX_GUEST_AUDIO_SERVER", ""))
    guest_check.add_argument("--side", choices=SIDES, default=GUEST_SIDE)
    assign = sub.add_parser("assign")
    assign.add_argument("sink")
    assign.add_argument("side", choices=("left", "right", "free"))
    assign.add_argument("--kind", choices=KINDS, default="output")
    overlay = sub.add_parser("overlay")
    overlay.add_argument("side", nargs="?", choices=SIDES)
    overlay.add_argument("kind", nargs="?", choices=(*KINDS, "mix"))
    overlay.add_argument("device", nargs="?")
    args = parser.parse_args()

    if args.command == "overlay":
        if args.side is not None and (args.kind is None or args.device is None):
            parser.error("overlay assignment requires side, direction and device")
        print(overlay_snapshot(args.side, args.kind, args.device), end="")
        return 0
    if args.command == "list-sinks":
        print(json.dumps(list_sinks()))
        return 0
    if args.command == "list-sources":
        print(json.dumps(list_devices("sources")))
        return 0
    if args.command == "ensure-shared":
        return ensure_shared_audio()
    if args.command == "monitor-shared":
        monitor_shared_audio()
        return 0
    if args.command == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if args.command == "guest-endpoint-check":
        if not args.server.startswith("unix:/"):
            parser.error("guest-endpoint-check needs --server unix:/path")
        problems = guest_endpoint_problems(args.server, args.side)
        for problem in problems:
            print(f"dosplex audio: {problem}", file=sys.stderr)
        return 1 if problems else 0
    if args.command == "assign":
        return assign_audio(args.side, args.sink, args.kind)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"dosplex audio: {error}", file=sys.stderr)
        raise SystemExit(1)
