"""Private stdio attachment witness. This file is copied alone; stdlib only.

The witness relays bytes, never instantiates the delegate or reads its key. Its
receipt proves an initialized native-client connection and tool enumeration,
not inference, provider health, hook execution, or resolution of provider jobs.
The lifetime advisory lease is local connection evidence, not a portable kernel
attestation of a process's start time or the owning AO controller generation.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


TOOLS = ("deepseek_ask", "deepseek_cancel", "deepseek_health", "deepseek_result", "deepseek_status", "deepseek_submit")
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MEANING = "Native client initialization and server tool enumeration observed; no inference proof."
LIMITATION = ("A held local attachment lease is observed, not a portable process-start or AO-controller "
              "attestation. This never settles provider jobs or paid delivery.")
# Match the existing provider audit bound for derived/current room evidence.
# A connection can outlive the state size accepted by its retained launcher;
# this does not change that launcher's separate historical restart limit.
MAX_EVIDENCE = 96_000_000
MAX_MANIFEST = 256_000


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON")
    return result


def _json(data):
    def bad_constant(value):
        raise ValueError("Nonfinite JSON")
    return json.loads(data, object_pairs_hook=_object, parse_float=_float, parse_constant=bad_constant)


def _path(value):
    if not isinstance(value, str) or not os.path.isabs(value) or str(Path(value)) != value:
        raise ValueError("Evidence requires a normalized absolute path")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Evidence path escapes its owner")
    for entry in (path, *path.parents):
        if entry.is_symlink():
            raise ValueError("Evidence traverses a symlink")
    return path


def _inside(value, directory):
    path = _path(value)
    if path == directory or not path.is_relative_to(directory):
        raise ValueError("Evidence must be below its room")
    return path


def _read(path, maximum=MAX_EVIDENCE):
    path = _path(str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Evidence is unsafe or oversized")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Evidence is oversized")
        return data
    finally:
        os.close(fd)


def _load(path, maximum=MAX_EVIDENCE):
    result = _json(_read(path, maximum))
    if not isinstance(result, dict):
        raise ValueError("Evidence must be an object")
    return result


def _private_directory(path):
    path = _path(str(path))
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Attachment directory must be private and owned")
    return path


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value):
        raise ValueError("Invalid attachment identity")
    return value


def _relative(directory, value):
    if not isinstance(value, str) or Path(value).is_absolute():
        raise ValueError("Expected room-relative evidence")
    return _inside(str(directory / value), directory)


def _server(value, worktree):
    if not isinstance(value, dict) or set(value) - {"type", "command", "args", "env"}:
        raise ValueError("Unexpected server configuration")
    if value.get("type", "stdio") != "stdio" or not os.path.isabs(value.get("command", "")):
        raise ValueError("A stdio argv server is required")
    if not isinstance(value.get("args"), list) or not value["args"] or any(not isinstance(v, str) or "\0" in v for v in value["args"]):
        raise ValueError("Invalid server argv")
    if value.get("env") != {"PROJECT_ROOM_WORKTREE": worktree}:
        raise ValueError("Unexpected server environment")


def _validate_manifest(path, state=None, prepared=None, runtime=False):
    path = _path(str(path))
    manifest = _load(path, MAX_MANIFEST)
    fields = {"version", "attachment_id", "room_directory", "room_id", "session_id", "worktree", "child_server",
              "provider_files", "profile_path", "wrapper_path", "wrapper_sha256", "expected_tools", "receipt_directory"}
    if set(manifest) != fields or manifest["version"] != 1 or isinstance(manifest["version"], bool):
        raise ValueError("Unsupported attachment manifest")
    directory = _path(manifest["room_directory"])
    _inside(str(path), directory)
    for key in ("room_id", "session_id", "attachment_id"):
        _identifier(manifest[key])
    if not isinstance(manifest["expected_tools"], list) or sorted(manifest["expected_tools"]) != list(TOOLS):
        raise ValueError("Attachment must enumerate the exact DeepSeek tools")
    disk_state = _load(directory / "state.json")
    if state is None:
        state = disk_state
    elif any(state.get(k) != disk_state.get(k) for k in (
            "preparation", "preparation_sha256", "preparation_status", "delegate", "bindings", "room_id", "workflow")):
        raise ValueError("Current room provenance changed")
    if prepared is None:
        prepared = _load(_relative(directory, state["preparation"]))
    else:
        if _load(_relative(directory, state["preparation"])) != prepared:
            raise ValueError("Preparation pointer changed")
    if (digest(prepared) != state.get("preparation_sha256") or state.get("preparation_status") != "configured"
            or state.get("workflow") != "fable_engineering" or state.get("room_id") != manifest["room_id"]
            or prepared.get("room_id") != manifest["room_id"] or prepared.get("provider") != "deepseek"
            or state.get("bindings", {}).get("engineer", {}).get("session_id") != manifest["session_id"]):
        raise ValueError("Attachment room or engineer provenance changed")
    worktree = str(_path(manifest["worktree"]))
    if not Path(worktree).is_dir() or prepared.get("worktree") != worktree:
        raise ValueError("Attachment workspace changed")
    delegate = state["delegate"]
    if delegate.get("provider") != "deepseek" or digest(delegate) != prepared.get("delegate_sha256"):
        raise ValueError("Delegate provenance changed")
    files = dict(delegate["files"])
    for name, expected in (delegate.get("inventory") or {}).get("files", {}).items():
        if name in files and files[name] != expected:
            raise ValueError("Conflicting provider evidence")
        files[name] = expected
    if not files or manifest["provider_files"] != files:
        raise ValueError("Incomplete provider file bindings")
    for name, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(_read(name)) != expected:
            raise ValueError("Pinned provider evidence changed")
    profile = manifest["profile_path"]
    if profile not in files:
        raise ValueError("Unpinned child profile")
    child = manifest["child_server"]
    _server(child, worktree)
    original = _load(profile)["mcpServers"]["deepseek"]
    if {**original, "env": {"PROJECT_ROOM_WORKTREE": worktree}} != child:
        raise ValueError("Child server differs from retained profile")
    wrapper = _inside(manifest["wrapper_path"], directory)
    if digest(_read(wrapper)) != manifest["wrapper_sha256"]:
        raise ValueError("Attachment wrapper changed")
    expected_server = {"command": child["command"], "args": [str(wrapper), "--manifest", str(path)],
                       "env": {"PROJECT_ROOM_WORKTREE": worktree}}
    if prepared.get("server") != expected_server or prepared.get("mcp_attachment") != {
            "version": 1, "attachment_id": manifest["attachment_id"], "manifest_path": str(path), "manifest_sha256": digest(manifest)}:
        raise ValueError("Active preparation does not select this attachment")
    receipt_directory = _private_directory(_inside(manifest["receipt_directory"], directory))
    if runtime and (str(Path.cwd().resolve()) != worktree or os.environ.get("AO_SESSION_ID") != manifest["session_id"]
                    or os.environ.get("PROJECT_ROOM_WORKTREE") != worktree or Path(__file__).resolve() != wrapper):
        raise ValueError("Native launch identity differs from attachment")
    return manifest, state, prepared, receipt_directory


def _immutable(path, value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    fd, name = tempfile.mkstemp(prefix=".attachment-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path, follow_symlinks=False)
        except FileExistsError:
            if _read(path) != data:
                raise ValueError("Immutable attachment evidence changed")
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(name)


def _lease_open(directory, create=False, filename="connection.lock"):
    path = _path(str(directory / filename))
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise ValueError("Attachment lease is unsafe")
    return fd


def _held(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def _locator(fd):
    data = os.pread(fd, 8193, 0)
    if len(data) > 8192:
        raise ValueError("Attachment lease is oversized")
    return _json(data) if data else None


def _basis(manifest, prepared):
    return {"version": 1, "attachment_id": manifest["attachment_id"], "room_id": manifest["room_id"],
            "session_id": manifest["session_id"], "worktree": manifest["worktree"],
            "manifest_sha256": digest(manifest), "preparation_sha256": digest(prepared),
            "child_server_sha256": digest(manifest["child_server"]), "provider_files": manifest["provider_files"],
            "wrapper_sha256": manifest["wrapper_sha256"], "expected_tools": list(TOOLS)}


def _ledger_before_launch(manifest, state):
    """Prevent a retained constructor from recreating lost ledger/schema rows.

    This wrapper is for already materialized retained sessions only: even an
    empty ledger must exist. Never import the adapter, repair schema, read a key,
    refresh jobs, or interpret a local lease as resolving provider delivery.
    """
    args = manifest["child_server"]["args"]

    def argument(flag):
        if args.count(flag) != 1:
            raise ValueError("Retained child storage identity is ambiguous")
        index = args.index(flag)
        if index + 1 >= len(args):
            raise ValueError("Retained child storage identity is absent")
        return args[index + 1]

    home = _path(argument("--home"))
    if argument("--room") != manifest["room_id"]:
        raise ValueError("Retained child room identity changed")
    root = _private_directory(home / "deepseek")
    path = _path(str(root / "ledger.sqlite3"))
    info = path.stat()  # mandatory existing file; never connect a missing path
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Retained delegate ledger is unsafe")
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.execute("BEGIN")
            rows = db.execute("SELECT id,room_id,state FROM jobs").fetchall()
            resolutions = set(db.execute("SELECT job_id,room_id FROM resolutions"))
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ValueError("Retained delegate ledger is unreadable or incomplete") from exc
    current = path.stat()
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError("Retained delegate ledger changed during observation")
    known = {row[0] for row in rows}
    own = {row[0] for row in rows if row[1] == manifest["room_id"]}
    for request in state.get("requests", {}).values():
        for field in ("reported_delegate_job_ids", "delegate_job_ids"):
            identifiers = request.get(field, [])
            if not isinstance(identifiers, list) or any(job not in own for job in identifiers):
                raise ValueError("Retained report names a missing delegate job")
    for job, room, outcome in rows:
        if room != manifest["room_id"]:
            continue
        if outcome in ("unknown_delivery", "failed_after_send") and (job, room) in resolutions:
            continue  # an existing user resolution, never a witness decision
        if outcome not in ("not_started", "completed", "truncated", "rejected_before_generation"):
            raise ValueError("Unsettled or unrecognized delegate job prevents native attachment")
    jobs = _path(str(root / "jobs"))
    if jobs.exists():
        _private_directory(jobs)
        for directory in jobs.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.name in known:
                continue
            metadata = _load(directory / "request.json", 24576)
            if metadata.get("job_id") != directory.name or not isinstance(metadata.get("room_id"), str) or not metadata["room_id"]:
                raise ValueError("Orphan delegate metadata is inconsistent")
            if metadata["room_id"] == manifest["room_id"]:
                raise ValueError("Retained delegate ledger lost a recorded job")


def validate_attachment(directory, state, prepared):
    """Read-only current-connection verification; raises ValueError if unproven.

    The caller separately verifies positive AO idle/current native ownership and
    provider ledgers. A successful receipt cannot substitute for those checks.
    """
    try:
        manifest, _, actual, receipts = _validate_manifest(prepared["mcp_attachment"]["manifest_path"], state, prepared)
        if _path(str(directory)) != Path(manifest["room_directory"]):
            raise ValueError("Attachment belongs to another room")
        owner = _lease_open(receipts, filename="owner.lock")
        fd = None
        try:
            fd = _lease_open(receipts)
            if not _held(owner) or not _held(fd):
                raise ValueError("Attachment connection lease is not held")
            locator = _locator(fd)
            connection_id = _identifier(locator["connection_id"])
            info = os.fstat(fd)
            if locator.get("lease_identity") != {"device": info.st_dev, "inode": info.st_ino}:
                raise ValueError("Attachment lease file was replaced")
            receipt = _load(receipts / (connection_id + ".json"))
            if (receipt.get("basis") != _basis(manifest, actual) or receipt.get("connection") != locator
                    or receipt.get("meaning") != MEANING or receipt.get("limitation") != LIMITATION
                    or receipt.get("handshake") != {"initialize_response_forwarded": True, "initialized_observed": True,
                                                    "tools_list_response_forwarded": True}):
                raise ValueError("Attachment receipt does not match the current connection")
            if any(_path(str(receipts / (connection_id + suffix))).exists() for suffix in (".closed.json", ".invalid.json")):
                raise ValueError("Attachment connection ended or was disqualified")
            current = (receipts / "connection.lock").stat()
            if (_locator(fd) != locator or not _held(owner) or not _held(fd)
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError("Attachment connection changed during observation")
            return receipt
        finally:
            if fd is not None:
                os.close(fd)
            os.close(owner)
    except (OSError, KeyError, TypeError, AttributeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Attachment evidence is absent or inconsistent") from exc


class Witness:
    """Observe only handshake metadata. Never retain ordinary tool payloads."""

    def __init__(self, ready, invalid):
        self.ready, self.invalid = ready, invalid
        self.lock = threading.Lock()
        self.initialize_id = None
        self.tools_id = None
        self.initialize_seen = self.initialize_ok = self.initialized = self.tools_ok = self.recorded = self.failed = False

    def reject(self):
        if not self.failed:
            self.failed = True
            try:
                self.invalid()
            except (OSError, ValueError):
                pass  # receipt I/O must never swallow the ordinary MCP response

    def observe(self, line, client):
        with self.lock:
            if self.failed:
                return
            try:
                value = _json(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    self.reject()
                    return
                method = value.get("method")
                identifier = value.get("id")
                valid_id = isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
                if client:
                    if method == "initialize":
                        params = value.get("params")
                        if (self.initialize_seen or not valid_id or not isinstance(params, dict)
                                or not isinstance(params.get("protocolVersion"), str)):
                            self.reject()
                        else:
                            self.initialize_seen, self.initialize_id = True, (type(identifier).__name__, identifier)
                    elif method == "notifications/initialized":
                        if not self.initialize_seen or self.initialized or "id" in value:
                            self.reject()
                        else:
                            self.initialized = True
                    elif method == "tools/list":
                        if (not self.initialize_seen or not valid_id or self.tools_id is not None
                                or (type(identifier).__name__, identifier) == self.initialize_id):
                            self.reject()
                        else:
                            self.tools_id = (type(identifier).__name__, identifier)
                            self.tools_ok = False
                    elif valid_id and (type(identifier).__name__, identifier) in (self.initialize_id, self.tools_id):
                        self.reject()
                elif "method" not in value and valid_id:
                    key = (type(identifier).__name__, identifier)
                    if self.initialize_seen and key == self.initialize_id:
                        result = value.get("result")
                        server = result.get("serverInfo") if isinstance(result, dict) else None
                        capabilities = result.get("capabilities") if isinstance(result, dict) else None
                        if (self.initialize_ok or "error" in value or not isinstance(server, dict)
                                or any(not isinstance(server.get(key), str) or not server[key] for key in ("name", "version"))
                                or result.get("protocolVersion") not in PROTOCOLS or not isinstance(capabilities, dict)
                                or not isinstance(capabilities.get("tools"), dict)):
                            self.reject()
                        else:
                            self.initialize_ok = True
                            self.initialize_id = None
                    elif key == self.tools_id:
                        result = value.get("result")
                        tools = result.get("tools") if isinstance(result, dict) else None
                        if (self.tools_ok or "error" in value or not isinstance(tools, list)
                                or any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in tools)
                                or sorted(tool["name"] for tool in tools) != list(TOOLS) or result.get("nextCursor")):
                            self.reject()
                        else:
                            self.tools_ok = True
                            self.tools_id = None
                if not self.failed and self.initialize_ok and self.initialized and self.tools_ok and not self.recorded:
                    self.ready()
                    self.recorded = True
            except (ValueError, TypeError, OSError, RecursionError):
                self.reject()


class Lines:
    """Keep one transport frame transiently; never persist tool payloads.

    JSON-RPC fields may occur in any order, so skipping an oversized frame would
    allow a padded second initialize to evade disqualification. Transport has no
    additional size cap: the existing delegate owns its normal request limits.
    """

    def __init__(self, callback):
        self.callback, self.pending = callback, bytearray()

    def feed(self, data):
        for index, part in enumerate(data.split(b"\n")):
            if index:
                self.callback(bytes(self.pending))
                self.pending.clear()
            self.pending.extend(part)


def run(manifest_path):
    manifest, state, prepared, receipts = _validate_manifest(manifest_path, runtime=True)
    owner = _lease_open(receipts, create=True, filename="owner.lock")
    lease = None
    child = None
    connection = None
    witness = None
    outcome = "transport_closed"
    ended = threading.Event()
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _ledger_before_launch(manifest, state)
        previous = None
        prior_path = _path(str(receipts / "connection.lock"))
        if prior_path.exists():
            prior = _lease_open(receipts)
            try:
                if _held(prior):
                    raise ValueError("Previous attachment lease is still held")
                previous = _locator(prior)
            finally:
                os.close(prior)
        if previous:
            prior_id = _identifier(previous["connection_id"])
            closed = receipts / (prior_id + ".closed.json")
            if not closed.exists():
                _immutable(closed, {"connection": previous, "reason": "prior_local_lease_observed_released",
                                    "observed_at_ns": time.time_ns(), "limitation": LIMITATION})
        child = subprocess.Popen([manifest["child_server"]["command"], *manifest["child_server"]["args"]],
                                 cwd=manifest["worktree"], env={**os.environ, **manifest["child_server"]["env"]},
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        connection = {"connection_id": uuid.uuid4().hex, "wrapper_pid": os.getpid(), "child_pid": child.pid,
                      "parent_pid": os.getppid(), "observed_start_ns": time.time_ns(),
                      "attachment_id": manifest["attachment_id"], "manifest_sha256": digest(manifest)}
        # Publish a fresh held lease inode, never reacquire the previous inode
        # while its old ready locator is visible. Otherwise a slow restart could
        # temporarily make a crashed connection appear live again.
        lease, temporary = tempfile.mkstemp(prefix=".connection-", dir=receipts)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(lease)
            connection["lease_identity"] = {"device": info.st_dev, "inode": info.st_ino}
            os.write(lease, json.dumps(connection, sort_keys=True).encode())
            os.fsync(lease)
            os.replace(temporary, prior_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        name = connection["connection_id"]

        def ready():
            # Recheck immutable inputs and the committed active pointer at the
            # handshake boundary; launch-time checks alone may now be stale.
            _, _, current, _ = _validate_manifest(manifest_path, runtime=True)
            if current != prepared:
                raise ValueError("Preparation changed during attachment")
            _immutable(receipts / (name + ".json"), {
                "basis": _basis(manifest, prepared), "connection": connection, "observed_at_ns": time.time_ns(),
                "handshake": {"initialize_response_forwarded": True, "initialized_observed": True,
                              "tools_list_response_forwarded": True}, "meaning": MEANING, "limitation": LIMITATION})

        def invalid():
            # Immediately disqualify the held lease even if the private receipt
            # directory becomes unwritable after an earlier ready receipt.
            changed = {**connection, "invalid": True}
            data = json.dumps(changed, sort_keys=True).encode()
            os.ftruncate(lease, 0)
            os.pwrite(lease, data, 0)
            os.fsync(lease)
            _immutable(receipts / (name + ".invalid.json"), {"connection": connection,
                        "reason": "handshake_not_proven", "observed_at_ns": time.time_ns(), "limitation": LIMITATION})

        witness = Witness(ready, invalid)

        def pump(source, destination, client):
            lines = Lines(lambda line: witness.observe(line, client))
            try:
                while not ended.is_set():
                    data = os.read(source, 65536)
                    if not data:
                        break
                    if client:
                        lines.feed(data)
                    remaining = memoryview(data)
                    while remaining and not ended.is_set():
                        count = os.write(destination, remaining)
                        if count <= 0:
                            raise BrokenPipeError()
                        remaining = remaining[count:]
                    if not client and not remaining:
                        lines.feed(data)  # all these bytes have reached client stdout
            except (OSError, ValueError):
                pass
            finally:
                ended.set()

        for source, destination, client in ((sys.stdin.fileno(), child.stdin.fileno(), True),
                                             (child.stdout.fileno(), sys.stdout.fileno(), False)):
            threading.Thread(target=pump, args=(source, destination, client), daemon=True).start()

        def shutdown(signum, frame):
            ended.set()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        ended.wait()  # no timer, inference deadline, or retry
    finally:
        ended.set()
        if witness is not None:
            with witness.lock:
                witness.failed = True
        if child is not None:
            if child.poll() is None:
                # Only the owned stdio server PID. Detached provider job workers
                # intentionally survive; never signal a process group here.
                child.kill()
            child.wait()
            # Daemon pumps may still be blocked on the parent's stdin. Keep the
            # pipe descriptors alive until all evidence writes are finished so
            # they cannot be reused for a receipt underneath a blocked writer.
        if connection is not None:
            _immutable(receipts / (connection["connection_id"] + ".closed.json"),
                       {"connection": connection, "reason": outcome, "observed_at_ns": time.time_ns(), "limitation": LIMITATION})
        if lease is not None:
            os.close(lease)
        os.close(owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        run(args.manifest)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        # No configuration values, tool payloads, subprocess stderr or secrets.
        print("Project Room MCP attachment refused; inspect its private evidence.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
