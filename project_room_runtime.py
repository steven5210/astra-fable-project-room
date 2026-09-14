"""Retain one coherent MCP runtime beyond plugin-cache and MCP-process lifetime.

Only the stdio entrypoint activates this copy. Normal Python API imports keep
their source checkout semantics. Retained releases are never removed here:
legacy workers can outlive the MCP process that created them.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


RUNTIME_FILES = (
    "ao_acp_patch.py", "ao_delegate_launcher.py", "ao_delegates.py",
    "ao_executable_binding.py", "ao_history.py", "ao_instruction_amendments.py", "ao_mcp_attachment.py",
    "ao_native_identity.py", "ao_native_outcome.py", "ao_outcomes.py", "ao_project_room.py",
    "ao_provider_transition.py", "ao_report_contract.py", "ao_response_normalization.py",
    "ao_review_extension.py", "ao_reviewer_recovery.py", "ao_routing.py", "ao_routing_adoption.py", "ao_routing_guard.py",
    "ao_workflow.py", "deepseek_adapter.py", "handoff_status.py", "heartbeat.py",
    "implementation.py", "progress.py", "project_room.py", "project_room_mcp.py",
    "project_room_runtime.py", "qwen_guard.py", "recovery.py", "room.py",
    "session_paths.py", "transcript_audit.py", "verification.py",
)
MARKER = "runtime.json"
MAX_FILE_BYTES = 4_000_000


class RuntimeRetentionError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path, maximum=MAX_FILE_BYTES):
    """Reject links and special files, and bound/verify a file read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise RuntimeRetentionError("Runtime input is not a bounded regular file: " + path.name)
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(data) > maximum or _signature(before) != _signature(after):
            raise RuntimeRetentionError("Runtime input changed while being read: " + path.name)
        return data, _signature(after)
    except OSError as exc:
        raise RuntimeRetentionError("Runtime input is unavailable or unsafe: " + path.name) from exc


def _private_directory(path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeRetentionError("Retained runtime directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeRetentionError("Retained runtime requires an owned private directory")


def _source_snapshot(source):
    metadata_directory = source / ".codex-plugin"
    try:
        if not stat.S_ISDIR(metadata_directory.lstat().st_mode):
            raise RuntimeRetentionError("Plugin metadata directory must not be a link")
        raw, metadata_signature = _read_regular(metadata_directory / "plugin.json", 64_000)
        package = json.loads(raw)
        version = package["version"]
        if (package.get("name") != "astra-fable-project-room" or not isinstance(version, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}", version)):
            raise RuntimeRetentionError("Plugin runtime identity is invalid")
        files = {}
        signatures = {}
        for name in RUNTIME_FILES:
            files[name], signatures[name] = _read_regular(source / name)
        # Detect an in-place source edit spanning the collection, not just one
        # occurring during an individual read. Installation removal also fails
        # before publishing an incomplete runtime.
        signatures[".codex-plugin/plugin.json"] = metadata_signature
        if any(_signature((source / name).lstat()) != signature for name, signature in signatures.items()):
            raise RuntimeRetentionError("Plugin source changed while retaining its runtime")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeRetentionError("Plugin runtime source or identity is incomplete") from exc
    manifest = {"format": 1, "package": "astra-fable-project-room", "version": version,
                "files": {name: _sha(data) for name, data in files.items()}}
    return manifest, files


def _verify(directory, expected=None):
    _private_directory(directory)
    try:
        manifest = json.loads(_read_regular(directory / MARKER, 64_000)[0])
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "package", "version", "files"}
                or type(manifest["format"]) is not int or manifest["format"] != 1
                or manifest["package"] != "astra-fable-project-room"
                or not isinstance(manifest["version"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}", manifest["version"])
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != set(RUNTIME_FILES)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in manifest["files"].values())
                or (expected is not None and manifest != expected)
                or set(child.name for child in directory.iterdir()) != {*RUNTIME_FILES, MARKER}):
            raise RuntimeRetentionError("Retained runtime manifest or inventory was modified")
        for name, digest in manifest["files"].items():
            if _sha(_read_regular(directory / name)[0]) != digest:
                raise RuntimeRetentionError("Retained runtime file was modified: " + name)
        return manifest
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeRetentionError("Retained runtime is incomplete or modified") from exc


def _write_once(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retain(source, home):
    """Capture or verify the exact release, without reading controller settings."""
    source, home = Path(source).resolve(), Path(home).expanduser().resolve()
    store = home / "runtimes"
    if store == source or store.is_relative_to(source):
        raise RuntimeRetentionError("Controller home must keep retained runtimes outside the plugin source")
    manifest, files = _source_snapshot(source)
    identity = _sha(_canonical(manifest))
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.mkdir(exist_ok=True, mode=0o700)
    _private_directory(store)
    fd = os.open(store / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeRetentionError("Runtime publication lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        target = store / identity
        if os.path.lexists(target):
            _verify(target, manifest)  # Never repair or replace a damaged release in place.
        else:
            staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=store))
            try:
                for name, data in files.items():
                    _write_once(staging / name, data)
                _write_once(staging / MARKER, _canonical(manifest) + b"\n")
                _verify(staging, manifest)
                staging.chmod(0o500)
                _sync_directory(staging)
                staging.rename(target)
                _sync_directory(store)
            finally:
                if staging.exists():
                    staging.chmod(0o700)
                    shutil.rmtree(staging)
    finally:
        os.close(fd)
    return {"path": str(target), "version": manifest["version"], "sha256": identity}


def activate(entrypoint):
    """Pin imports/resources before the MCP imports any first-party service code."""
    source = Path(entrypoint).resolve().parent
    startup_cwd = Path.cwd()
    search_paths = [str((Path(entry) if Path(entry).is_absolute() else startup_cwd / entry).resolve()) for entry in sys.path]
    home = Path(os.environ.get("PROJECT_ROOM_HOME") or Path.home() / ".project-room").expanduser().resolve()
    if any(Path(name).stem in sys.modules for name in RUNTIME_FILES if name != "project_room_runtime.py"):
        raise RuntimeRetentionError("MCP startup requires first-party modules to be loaded from one retained release")
    if os.path.lexists(source / MARKER):
        manifest = _verify(source)
        identity = _sha(_canonical(manifest))
        if source.name != identity:
            raise RuntimeRetentionError("Retained runtime directory does not match its identity")
        runtime = {"path": str(source), "version": manifest["version"], "sha256": identity}
    else:
        runtime = retain(source, home)
    # Resolve relative PROJECT_ROOM_HOME before changing cwd, preserving the
    # caller's state selection for this process and its detached workers.
    os.environ["PROJECT_ROOM_HOME"] = str(home)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path[:] = [runtime["path"]] + [entry for entry in search_paths if Path(entry) != source]
    os.chdir(runtime["path"])
    return runtime
