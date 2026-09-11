"""Private, content-addressed MCP launcher; no provider or model implementation.

Claude local MCP registrations are shared by Git worktrees. Select exactly one
prepared room by the actual process cwd, then exec that room's retained server.
This file is copied alone into private controller state, so use only the stdlib.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def owned_bytes(path, maximum=4_000_000):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Private launch evidence traverses a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
            raise ValueError("Private launch evidence is unsafe or oversized")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Private launch evidence is oversized")
        return data
    finally:
        os.close(fd)


def load(path):
    return json.loads(owned_bytes(path))


def select(home, workspace, session_id, launcher_path):
    """Validate launch inputs without touching a key file or making a request."""
    home, workspace = Path(home).resolve(), Path(workspace).resolve()
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", session_id):
        raise ValueError("A native AO_SESSION_ID is required")
    matches = []
    for path in (home / "ao" / "rooms").glob("*/state.json"):
        state = load(path)
        if state.get("workflow") != "fable_engineering" or not state.get("preparation"):
            continue
        preparation = load(path.parent / state["preparation"])
        if preparation.get("worktree") == str(workspace):
            matches.append((path.parent, state, preparation))
    if len(matches) != 1:
        raise ValueError("MCP cwd must match exactly one prepared AO room")
    directory, state, preparation = matches[0]
    try:
        def git(*args):
            return subprocess.check_output(["git", "-C", str(workspace), *args], stderr=subprocess.DEVNULL, timeout=15, text=True).strip()
        if (Path(git("rev-parse", "--show-toplevel")).resolve() != workspace
                or str(Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()) != state["git_common_dir"]):
            raise ValueError("Prepared workspace Git repository changed")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Prepared workspace Git identity is unavailable") from exc
    if (state.get("preparation_status") != "configured"
            or digest(preparation) != state.get("preparation_sha256")
            or preparation.get("room_id") != state.get("room_id")
            or preparation.get("provider") != "deepseek"
            or str(Path(launcher_path).resolve()) != preparation.get("launcher_path")
            or digest(owned_bytes(launcher_path)) != preparation.get("launcher_sha256")):
        raise ValueError("Prepared delegate launch identity is missing or changed")
    delegate = state.get("delegate", {})
    if digest(delegate) != preparation.get("delegate_sha256"):
        raise ValueError("Prepared delegate policy changed")
    for path, expected in delegate["files"].items():
        if digest(owned_bytes(path)) != expected:
            raise ValueError("Pinned delegate file changed")
    for path, expected in (delegate.get("inventory") or {}).get("files", {}).items():
        if digest(owned_bytes(path)) != expected:
            raise ValueError("Retained delegate snapshot changed")
    binding = state.get("bindings", {}).get("engineer")
    if binding and binding.get("session_id") != session_id:
        raise ValueError("AO session differs from the pinned engineer")
    server = preparation["server"]
    if server.get("env") != {"PROJECT_ROOM_WORKTREE": str(workspace)}:
        raise ValueError("Delegate worktree environment changed")
    return directory, preparation, server


def record_launch(directory, preparation, session_id):
    """One session per prepared workspace, including before the later role bind."""
    path = directory / "delegate-launch.json"
    value = {"session_id": session_id, "worktree": preparation["worktree"],
             "preparation_sha256": digest(preparation), "meaning": "MCP launch observed; not inference proof"}
    if path.exists():
        if load(path) != value:
            raise ValueError("Prepared delegate was already launched by another AO session")
        return
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".launch-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    home = Path(args.home).resolve()
    with (home / "ao" / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session_id = os.environ.get("AO_SESSION_ID")
        directory, preparation, server = select(home, Path.cwd(), session_id, __file__)
        record_launch(directory, preparation, session_id)
    os.execve(server["command"], [server["command"], *server["args"]], {**os.environ, **server["env"]})


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Project Room delegate launch refused: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
