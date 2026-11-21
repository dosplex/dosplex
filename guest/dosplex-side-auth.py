#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import json
import contextlib
import os
import pwd
import subprocess
import stat
import sys
import tempfile
import time
import fcntl
from functools import wraps
from pathlib import Path

try:
    import PAM  # type: ignore
except ImportError:
    PAM = None

try:
    import pam as pam_module  # type: ignore
except ImportError:
    pam_module = None


STATE_ROOT = Path("/var/lib/dosplex")
SIDE_ROOT = Path(os.environ.get("DOSPLEX_SIDE_ROOT", str(STATE_ROOT / "sides")))
DEFAULT_RUNTIME_DIR = Path(f"/run/user/{os.getuid()}")
SOCKET_RUNTIME_DIR = Path(os.environ.get("DOSPLEX_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)))
SOCKET_NAME = os.environ.get("DOSPLEX_WAYLAND_DISPLAY", "dosplex-0")
DEFAULT_SESSION_SCRIPT = Path(__file__).with_name("dosplex-user-side-session.sh")
SESSION_SCRIPT = os.environ.get(
    "DOSPLEX_SESSION_SCRIPT",
    str(DEFAULT_SESSION_SCRIPT)
    if DEFAULT_SESSION_SCRIPT.exists()
    else "/usr/local/libexec/dosplex-user-side-session.sh",
)
VALID_SIDES = {"left", "right"}
LOGOUT_TIMEOUT_SECONDS = 25


def fail(message, code=1):
    print(json.dumps({"ok": False, "error": message}))
    raise SystemExit(code)


def prepare_state_root():
    """Admit only a root-owned directory chain before any privileged state use."""
    if not SIDE_ROOT.is_absolute() or ".." in SIDE_ROOT.parts:
        fail("invalid state location")
    current = Path(SIDE_ROOT.anchor)
    for component in (None, *SIDE_ROOT.parts[1:]):
        if component is not None:
            current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Its parent was checked on the preceding iteration. Never adopt
            # or chown existing user-owned state into the privileged service.
            current.mkdir(mode=0o755)
            info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022):
            fail("untrusted state directory; administrator recovery is required")

    for side in VALID_SIDES:
        directory = SIDE_ROOT / side
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            pass
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022):
            fail("untrusted side directory; administrator recovery is required")

    paths = [SIDE_ROOT / "allocation.lock"]
    for side in VALID_SIDES:
        paths.extend(SIDE_ROOT / side / name for name in
                     ("session.json", "auth.lock", "supervisor.lock", "mode", "restart", "stop"))
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022 or info.st_nlink != 1):
            fail("untrusted state file; administrator recovery is required")


def load_state(side):
    require_side(side)
    state_file = SIDE_ROOT / side / "session.json"
    try:
        with state_file.open(encoding="utf-8") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            fail("side state is too large; recovery is required")
        state = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("cannot read side state; recovery is required")
    if not isinstance(state, dict) or state.get("side") != side or not isinstance(state.get("owner"), str):
        fail("invalid side state; recovery is required")
    return state


def save_state(side, payload):
    require_side(side)
    side_dir = SIDE_ROOT / side
    side_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    state_file = side_dir / "session.json"
    publish_json(state_file, payload)


def publish_json(state_file, payload):
    side_dir = state_file.parent
    # Serialize before opening anything: failed encoding must leave the old
    # complete state visible. Readers see either version, never a partial file.
    encoded = json.dumps(payload, indent=2)
    fd, temporary = tempfile.mkstemp(prefix=".auth-state-", dir=side_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, state_file)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_state(side):
    side_dir = SIDE_ROOT / side
    state_file = side_dir / "session.json"
    if state_file.exists():
        state_file.unlink()


def eligible_login_user(user):
    # Use the same policy for discovery and both authentication entry points.
    # A direct helper call must not bypass an account's disabled login shell.
    return (user.pw_uid >= 1000 and user.pw_uid != 65534 and
            user.pw_name not in ("dosplex", "nobody") and
            Path(user.pw_shell).name not in ("nologin", "false"))


def list_login_users():
    users = []
    for entry in pwd.getpwall():
        if not eligible_login_user(entry):
            continue
        users.append(
            {
                "name": entry.pw_name,
                "uid": entry.pw_uid,
                "home": entry.pw_dir,
                "shell": entry.pw_shell,
            }
        )
    users.sort(key=lambda item: item["name"])
    print(json.dumps({"ok": True, "users": users}))


def side_status(side):
    state = load_state(side)
    if state is None:
        print(json.dumps({"ok": True, "active": False, "side": side}))
        return

    pid = int(state.get("pid", 0))
    active = pid > 0 and Path(f"/proc/{pid}").exists()
    state["active"] = active
    print(json.dumps({"ok": True, **state}))


def require_side(side):
    if side not in VALID_SIDES:
        fail("side must be left or right")


def authenticate(username, password):
    if pam_module is not None:
        # python-pam authenticate() includes pam_acct_mgmt; do not retry a
        # denial through another backend. Package the reviewed binding version.
        authenticator = pam_module.pam()
        return bool(authenticator.authenticate(username, password, service="login"))

    if PAM is None:
        fail("python PAM module is not installed")

    def conversation(_auth, query_list, _user_data):
        responses = []
        for _query, prompt_type in query_list:
            if prompt_type == PAM.PAM_PROMPT_ECHO_OFF:
                responses.append((password, 0))
            elif prompt_type == PAM.PAM_PROMPT_ECHO_ON:
                responses.append((username, 0))
            else:
                responses.append(("", 0))
        return responses

    authenticator = PAM.pam()
    authenticator.start("login")
    authenticator.set_item(PAM.PAM_USER, username)
    authenticator.set_item(PAM.PAM_CONV, conversation)
    try:
        authenticator.authenticate()
        authenticator.acct_mgmt()
    except PAM.error:
        return False
    return True


@contextlib.contextmanager
def grant_wayland_access(username):
    """Keep exact ACL snapshots until claim publication succeeds."""
    descriptors=[]
    snapshots=[]
    changed=[]
    try:
        try:
            runtime=os.open(SOCKET_RUNTIME_DIR,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            descriptors.append(runtime)
            endpoint=os.open(SOCKET_NAME,os.O_PATH|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=runtime)
            descriptors.append(endpoint)
        except OSError:
            fail("display socket is unavailable; sign-in was not completed")
        runtime_info,endpoint_info=(os.fstat(fd) for fd in descriptors)
        if (not stat.S_ISDIR(runtime_info.st_mode) or not stat.S_ISSOCK(endpoint_info.st_mode) or
                runtime_info.st_uid != os.geteuid() or endpoint_info.st_uid != os.geteuid() or
                runtime_info.st_mode & 0o022):
            fail("display endpoint is invalid; sign-in was not completed")
        # Pass pinned descriptors to each child tool. Rename/replacement cannot
        # redirect a grant or its rollback to a different inode.
        for fd in descriptors:
            result=subprocess.run(['getfacl','-cpn','--',f'/proc/self/fd/{fd}'],
                check=True,capture_output=True,text=True,pass_fds=(fd,),timeout=5)
            snapshots.append((fd,result.stdout))
        try:
            for (fd,acl),permission in zip(snapshots,('x','rw')):
                changed.append((fd,acl))
                subprocess.run(['setfacl','-m',f'u:{username}:{permission}','--',f'/proc/self/fd/{fd}'],
                    check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(fd,),timeout=5)
            yield
        except BaseException as error:
            failed=False
            for fd,acl in reversed(changed):
                try:
                    subprocess.run(['setfacl','--set-file=-','--',f'/proc/self/fd/{fd}'],
                        input=acl,text=True,check=True,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,pass_fds=(fd,),timeout=5)
                except (OSError,subprocess.SubprocessError):
                    failed=True
            if failed:
                raise RuntimeError('display access rollback failed; recovery is required') from error
            raise
    finally:
        for fd in reversed(descriptors):os.close(fd)


def revoke_wayland_access(username):
    runtime_dir = SOCKET_RUNTIME_DIR
    socket_path = runtime_dir / SOCKET_NAME
    for path in (socket_path, runtime_dir):
        if path.exists():
            subprocess.run(
                ["setfacl", "-x", f"u:{username}", str(path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def serialized_claim(operation):
    @wraps(operation)
    def run(side, *args):
        require_side(side)
        directory = SIDE_ROOT / side
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        with os.fdopen(os.open(SIDE_ROOT / "allocation.lock", flags, 0o600), "r+") as allocation, \
                os.fdopen(os.open(directory / "auth.lock", flags, 0o600), "r+") as lock:
            try:
                fcntl.flock(allocation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("another authentication operation is in progress")
            return operation(side, *args)
    return run


def require_exclusive_identity(side, user):
    # Steam storage and Wayland admission are keyed by OS identity. Two side
    # claims must never share a UID, including different names for the same UID.
    other = load_state("right" if side == "left" else "left")
    if not other:
        return
    if other["owner"] == user.pw_name:
        fail("this local profile is already assigned to the other side")
    try:
        other_user = pwd.getpwnam(other["owner"])
    except KeyError:
        fail("the other side's local profile is missing; recovery is required")
    if other_user.pw_uid == user.pw_uid:
        fail("this local identity is already assigned to the other side")


@serialized_claim
def auto_login_side(side, username):
    require_side(side)
    if not username:
        fail("username is required for auto-login")

    try:
        user = pwd.getpwnam(username)
    except KeyError:
        fail("unknown user")

    if not eligible_login_user(user):
        fail("user is not eligible for Dosplex side auto-login")

    require_exclusive_identity(side, user)
    existing = load_state(side)
    if existing:
        if existing.get("lifecycle") == "cleanup_failed":
            fail("previous session cleanup failed; recovery is required")
        if existing.get("instance"):
            fail("side remains claimed; complete logout before signing in")
        pid = int(existing.get("pid", 0))
        if pid > 0 and Path(f"/proc/{pid}").exists():
            if existing.get("owner") != username:
                fail("side is already claimed by another user")
            # Idempotent only for the requested owner, never a different claim.
            print(json.dumps({"ok": True, **existing, "active": True}))
            return

    side_dir = SIDE_ROOT / side
    payload = {
        "side": side,
        "owner": username,
        "pid": 0,
        "active": True,
        "home": user.pw_dir,
        "shell": user.pw_shell,
        "session_root": str(side_dir),
        "workspace_root": f"{user.pw_dir}/Dosplex/{side}",
    }
    with grant_wayland_access(username):
        save_state(side, payload)
    print(json.dumps({"ok": True, **payload}))


@serialized_claim
def login_side(side, username, password):
    require_side(side)
    if not username or not password:
        fail("username and password are required")

    try:
        user = pwd.getpwnam(username)
    except KeyError:
        fail("unknown user")

    if not eligible_login_user(user):
        fail("user is not eligible for Dosplex side login")

    require_exclusive_identity(side, user)
    existing = load_state(side)
    if existing:
        if existing.get("lifecycle") == "cleanup_failed":
            fail("previous session cleanup failed; recovery is required")
        if existing.get("instance"):
            fail("side remains claimed; complete logout before signing in")
        pid = int(existing.get("pid", 0))
        if pid > 0 and Path(f"/proc/{pid}").exists():
            fail(f"{side} side is already claimed by {existing.get('owner')}")

    if not authenticate(username, password):
        fail("authentication failed")

    side_dir = SIDE_ROOT / side
    payload = {
        "side": side,
        "owner": username,
        "pid": 0,
        "active": True,
        "home": user.pw_dir,
        "shell": user.pw_shell,
        "session_root": str(side_dir),
        "workspace_root": f"{user.pw_dir}/Dosplex/{side}",
    }
    with grant_wayland_access(username):
        save_state(side, payload)
    print(json.dumps({"ok": True, **payload}))


@serialized_claim
def logout_side(side, expected_owner=None, expected_instance=None):
    require_side(side)
    guarded = expected_owner is not None or expected_instance is not None
    if guarded and (not isinstance(expected_owner, str) or not expected_owner or
                    not isinstance(expected_instance, str) or len(expected_instance) != 32 or
                    any(c not in "0123456789abcdef" for c in expected_instance)):
        fail("invalid expected session identity")
    state = load_state(side)
    if guarded and (state is None or state.get("owner") != expected_owner or
                    state.get("instance") != expected_instance):
        fail("session changed before logout; ownership was retained")
    if state is None:
        print(json.dumps({"ok": True, "side": side, "active": False}))
        return

    instance = state.get("instance")
    owner = state.get("owner")
    if not isinstance(instance, str) or len(instance) != 32 or not owner:
        fail("session identity is unavailable; recovery is required")
    expected = {"instance": instance, "side": side, "owner": owner}
    directory = SIDE_ROOT / side
    publish_json(directory / "stop", expected)
    deadline = time.monotonic() + LOGOUT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = load_state(side)
        if current is None or any(current.get(key) != value for key, value in expected.items()):
            fail("session changed during logout; recovery is required")
        if current.get("lifecycle") == "cleanup_failed":
            fail("session cleanup failed; recovery is required")
        if current.get("lifecycle") == "stopped" and current.get("active") is False and not current.get("unit"):
            # The final state is written before the supervisor releases its
            # lock. Wait for that release, then recheck while preventing a new
            # supervisor from starting during access revocation/state removal.
            with (directory / "supervisor.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    if load_state(side) != current:
                        fail("session changed during logout; recovery is required")
                    revoke_wayland_access(owner)
                    remove_state(side)
                    print(json.dumps({"ok": True, "side": side, "active": False}))
                    return
        time.sleep(.05)
    fail("session stop timed out; ownership was retained")


def read_password():
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096 or b"\0" in raw:
        fail("invalid password input")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("invalid password encoding")


def main():
    if os.geteuid() != 0:
        fail("must run as root")

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    allowed_lengths = {"list-users": (2,), "status": (3,), "auto-login": (4,),
                       "login": (4,), "logout": (3, 5)}
    if command not in allowed_lengths or len(sys.argv) not in allowed_lengths[command]:
        fail("invalid command or arguments")
    prepare_state_root()
    if command == "list-users":
        list_login_users()
        return
    if command == "status":
        side = sys.argv[2] if len(sys.argv) > 2 else ""
        require_side(side)
        side_status(side)
        return
    if command == "auto-login":
        if len(sys.argv) != 4:
            fail("usage: auto-login <left|right> <username>")
        auto_login_side(sys.argv[2], sys.argv[3])
        return
    if command == "login":
        if len(sys.argv) != 4:
            fail("usage: login <left|right> <username>; password is read from stdin")
        login_side(sys.argv[2], sys.argv[3], read_password())
        return
    if command == "logout":
        if len(sys.argv) not in (3, 5):
            fail("usage: logout <left|right> [<owner> <instance>]")
        logout_side(sys.argv[2], *(sys.argv[3:] if len(sys.argv) == 5 else []))
        return

    fail("unknown command")


if __name__ == "__main__":
    main()
