#!/usr/bin/env python3
"""Audited continuation of an implementation attempt stopped by a timeout or session-usage limit.

Nothing here calls a model, Qwen, or the network. Observation reads local boot time and the
same-user process table only, never signals a process, and reports allowlisted facts.
"""

import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform as platform_module
import re
import stat
import subprocess
import uuid

import implementation as impl
import room
import session_paths


GENERIC_ERROR = "ImplementationError: Claude did not return a successful terminal result for the exact implementation session"
QUOTA_TEXT = re.compile(r"You've hit your session limit · resets (?:1[0-2]|[1-9])(?::[0-5][0-9])?(?:am|pm) "
                        r"\(([A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3})\)")
TIMEOUT_SECONDS = re.compile(r"(?P<seconds>[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?) seconds")
EVIDENCE_LIMIT = 64 * 1024 * 1024
TRANSCRIPT_LIMIT = 1024 ** 3
EVIDENCE_FILES = ("prompt.txt", "argv.json", "stdout.json", "stderr.txt", "process-result.json", "parsed-result.json")
# Reasons caused by changed bytes or identity; a prepared authorization cannot survive them.
INVALIDATING = {"candidate_changed", "candidate_head_moved", "evidence_changed", "transcript_changed",
                "transcript_prefix_changed", "spec_mismatch", "open_findings"}
LOCK_ORDER = "room control -> handoff directory -> original job directory -> original worker lease"


class ObservationError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class LockUnavailable(Exception):
    """A cooperating owner (controller operation or worker lease) is active."""


def _open_component(name, flags, dir_fd):
    """The single descriptor-relative open used for every owned read below a root; tests inject races at this seam."""
    return os.open(name, flags, dir_fd=dir_fd)


def _open_root(path, flags):
    """The single path-based open of a trusted root directory; tests inject root swaps at this seam."""
    return os.open(path, flags)


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LEAF_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def directory_identity(path, kind="evidence"):
    """(st_dev, st_ino) of the user-owned directory currently at `path`, following only the configured prefix."""
    try:
        metadata = os.stat(path)
    except FileNotFoundError as exc:
        raise ObservationError(kind + "_missing") from exc
    except OSError as exc:
        raise ObservationError(kind + "_unsafe", "root " + type(exc).__name__) from exc
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ObservationError(kind + "_unsafe", "root owner")
    return metadata.st_dev, metadata.st_ino


class OwnedRoot:
    """One open descriptor for a trusted directory, held for a whole operation so every owned read below it is
    descriptor-relative. `expected` is the identity captured when the directory was validated: the opened
    descriptor must be that exact directory, so a directory swapped for a symlink (or replaced) between validation
    and open is refused before any byte below it is consumed. Configured aliases (for example macOS /var) resolve
    to the same identity and remain legitimate; nothing below the root is ever resolved."""

    def __init__(self, path, expected=None, kind="evidence"):
        self.path, self.fd, self.kind = Path(path), None, kind
        try:
            fd = _open_root(str(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError as exc:
            raise ObservationError(kind + "_missing") from exc
        except OSError as exc:
            raise ObservationError(kind + "_unsafe", "root " + type(exc).__name__) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ObservationError(kind + "_unsafe", "root owner")
            self.identity = (metadata.st_dev, metadata.st_ino)
            if expected is not None and self.identity != tuple(expected):
                raise ObservationError(kind + "_unsafe", "root changed")
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def open_owned_regular(path, limit, kind="evidence", root=None):
    """Binary handle for the regular file at `path`, reached only through user-owned directory descriptors.

    `root` is an OwnedRoot bound to a validated directory (the handoff directory, the configured Claude projects
    directory, or an explicit transcript's parent), or a path from which a transient root is opened; it defaults to
    the path's parent. `path` must lie lexically below the root's path: nothing below the root is resolved, and
    every component is opened relative to the previous descriptor with O_NOFOLLOW and O_DIRECTORY and must be a
    user-owned directory; the leaf is opened with O_NOFOLLOW and O_NONBLOCK and must be a user-owned regular file
    of at most `limit` bytes. A symlink anywhere below the root fails to open (never followed), a FIFO or device
    never blocks, and the returned handle is the validated inode itself."""
    path = Path(path)
    uid = os.getuid()
    transient = None
    if not isinstance(root, OwnedRoot):
        transient = root = OwnedRoot(path.parent if root is None else root, kind=kind)
    elif root.fd is None:
        raise ObservationError(kind + "_unsafe", "root closed")  # never fall back to a cwd-relative open
    try:
        try:
            parts = path.relative_to(root.path).parts
        except ValueError as exc:
            raise ObservationError(kind + "_unsafe", "outside owned root") from exc
        if not parts or any(part in ("", ".", "..") or "/" in part for part in parts):
            raise ObservationError(kind + "_unsafe", "invalid path components")
        return _open_below(root.fd, parts, limit, kind, uid)
    finally:
        if transient is not None:
            transient.close()


def _open_below(root_fd, parts, limit, kind, uid):
    fd, borrowed = root_fd, True
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            try:
                child = _open_component(part, LEAF_FLAGS if leaf else DIRECTORY_FLAGS, fd)
            except FileNotFoundError as exc:
                raise ObservationError(kind + "_missing") from exc
            except OSError as exc:
                raise ObservationError(kind + "_unsafe", type(exc).__name__) from exc
            if not borrowed:
                os.close(fd)
            fd, borrowed = child, False
            metadata = os.fstat(fd)
            if metadata.st_uid != uid:
                raise ObservationError(kind + "_unsafe", "foreign owner")
            if leaf:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ObservationError(kind + "_unsafe", "not a regular file")
                if metadata.st_size > limit:
                    raise ObservationError(kind + "_oversized")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ObservationError(kind + "_unsafe", "not a directory")
        handle = os.fdopen(fd, "rb")
        fd = None
        return handle
    finally:
        if fd is not None and not borrowed:
            os.close(fd)


CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _create_component(name, flags, mode, dir_fd):
    """The single descriptor-relative creating open used for every owned write; tests inject races at this seam."""
    return os.open(name, flags, mode, dir_fd=dir_fd)


def directory_below(base, parts, kind="evidence", create=False, exclusive=False):
    """Descriptor for the directory at base/parts, reached component by component with O_NOFOLLOW|O_DIRECTORY from an
    OwnedRoot or an already-bound directory descriptor. With `create`, missing components are created (mode 0o700)
    relative to the previous descriptor; with `exclusive`, the final component must be newly created. Every component
    must be a user-owned directory. The caller closes the returned descriptor."""
    base_fd = base.fd if isinstance(base, OwnedRoot) else base
    if base_fd is None or not isinstance(base_fd, int):
        raise ObservationError(kind + "_unsafe", "root closed")
    parts = tuple(parts)
    if not parts or any(part in ("", ".", "..") or "/" in part for part in parts):
        raise ObservationError(kind + "_unsafe", "invalid path components")
    fd, borrowed = base_fd, True
    try:
        for index, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError as exc:
                    if exclusive and index == len(parts) - 1:
                        raise ObservationError(kind + "_unsafe", "directory exists") from exc
                except OSError as exc:
                    raise ObservationError(kind + "_unsafe", type(exc).__name__) from exc
            try:
                child = _open_component(part, DIRECTORY_FLAGS, fd)
            except FileNotFoundError as exc:
                raise ObservationError(kind + "_missing") from exc
            except OSError as exc:
                raise ObservationError(kind + "_unsafe", type(exc).__name__) from exc
            if not borrowed:
                os.close(fd)
            fd, borrowed = child, False
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid() or not stat.S_ISDIR(metadata.st_mode):
                raise ObservationError(kind + "_unsafe", "not an owned directory")
        return fd
    except BaseException:
        if not borrowed:
            os.close(fd)
        raise


def exists_below(directory_fd, name):
    """True only when a regular file named `name` exists relative to the descriptor; a symlink, FIFO or directory
    planted under that name does not count, so a later atomic replacement still writes the real record."""
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def write_below(directory_fd, name, payload, kind="evidence", mode=0o600):
    """Atomically place `payload` (bytes, or an iterator of byte chunks) at `name` relative to `directory_fd`: a
    temporary sibling is created with O_EXCL|O_NOFOLLOW under the descriptor, written and fsynced, renamed into place
    relative to the same descriptor, and the directory is fsynced. A failure removes the temporary sibling."""
    if "/" in name or name in ("", ".", ".."):
        raise ObservationError(kind + "_unsafe", "invalid path components")
    temporary = name + "." + uuid.uuid4().hex + ".tmp"
    fd = _create_component(temporary, CREATE_FLAGS, mode, directory_fd)
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            if isinstance(payload, (bytes, bytearray)):
                handle.write(payload)
            else:
                for chunk in payload:
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_json_below(directory_fd, name, value, kind="evidence"):
    """Atomic JSON replacement relative to a bound directory descriptor (same encoding as implementation._atomic)."""
    payload = json_bytes(value)
    write_below(directory_fd, name, payload, kind)
    return payload


def read_owned(path, limit, kind="evidence", root=None):
    """Whole bytes of an owned regular file through open_owned_regular, bounded by `limit`."""
    with open_owned_regular(path, limit, kind, root) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ObservationError(kind + "_oversized")
    return data


def text_opener(limit, kind="transcript", root=None):
    """Opener for session_paths metadata validation: bounded, symlink-refusing, descriptor-bound text handle."""
    import io
    return lambda path: io.TextIOWrapper(open_owned_regular(path, limit, kind, root), encoding="utf-8")


def transcript_root(config, transcript_path):
    """Trusted directory below which a located transcript is opened: the configured projects directory, or the
    explicit template's parent when the configuration names one. Both are configured prefixes, so resolving them
    is resolving trusted configuration, never evidence."""
    if config.get("session_transcript_path"):
        return Path(transcript_path).parent
    return (Path(config["claude_config_dir"]).expanduser().resolve() / "projects").resolve()


def bind_handoff(handoff_path):
    """Validate a handoff through a bound directory descriptor: the directory identity at the registered path is
    captured first, the root descriptor must open to that exact identity, and the manifest, state and pinned
    inputs are read and verified only through that descriptor (bounded, symlink-refusing, never by path).
    Returns (root, directory, manifest, state, state_bytes); the caller closes the root."""
    directory = Path(handoff_path).resolve()  # the registered path is trusted configuration; only its prefix is resolved
    if directory.is_file():
        directory = directory.parent
    identity = directory_identity(directory, "handoff")
    root = OwnedRoot(directory, identity, "handoff")
    try:
        manifest = json.loads(read_owned(directory / "handoff.json", EVIDENCE_LIMIT, "handoff", root))
        state_bytes = read_owned(directory / "state.json", EVIDENCE_LIMIT, "handoff", root)
        state = json.loads(state_bytes)
        if (not isinstance(manifest, dict) or not isinstance(state, dict) or not isinstance(manifest.get("pinned_files"), dict)
                or impl._digest(manifest) != state.get("manifest_sha256")):
            raise impl.ImplementationError("Immutable handoff manifest was modified")
        for name, digest in manifest["pinned_files"].items():
            if (not isinstance(name, str) or not isinstance(digest, str) or Path(name).is_absolute()
                    or any(part in ("", ".", "..") for part in Path(name).parts)):
                raise impl.ImplementationError("Immutable handoff input has an unsupported name")  # nested relative names are fine
            if room.sha(read_owned(directory / name, EVIDENCE_LIMIT, "handoff", root)) != digest:
                raise impl.ImplementationError(f"Immutable handoff input was modified: {name}")
    except BaseException:
        root.close()
        raise
    return root, directory, manifest, state, state_bytes


def classify_quota_result(result, session_id):
    """Reason codes for which `result` misses the pinned session-usage signature; [] means it matches."""
    if not isinstance(result, dict):
        return ["result_not_object"]
    reasons = []
    if result.get("type") != "result":
        reasons.append("result_type_mismatch")
    if result.get("subtype") != "success":
        reasons.append("result_subtype_mismatch")
    if result.get("is_error") is not True:
        reasons.append("is_error_not_true")
    if result.get("terminal_reason") != "api_error":
        reasons.append("terminal_reason_mismatch")
    status = result.get("api_error_status")
    if type(status) is not int or status != 429:
        reasons.append("api_error_status_mismatch")
    if result.get("stop_reason") != "stop_sequence":
        reasons.append("stop_reason_mismatch")
    if not isinstance(session_id, str) or result.get("session_id") != session_id:
        reasons.append("session_mismatch")
    if result.get("permission_denials") not in (None, []):
        reasons.append("permission_denials_present")
    text = result.get("result")
    match = QUOTA_TEXT.fullmatch(text) if isinstance(text, str) else None
    if match is None or len(match.group(1)) > 64:
        reasons.append("result_text_mismatch")
    return reasons


def parse_timeout_error(error, argv, pinned_timeout):
    """(remaining seconds, []) when `error` is this attempt's exact TimeoutExpired text, else (None, reasons).

    S is the time Popen.communicate had left after delivering the prompt, so the pinned 60-second window
    (spec revision 2) also bounds how long the child may take to drain stdin before the classifier refuses.
    """
    if not isinstance(error, str) or not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return None, ["error_prefix_mismatch"]
    prefix = "TimeoutExpired: Command '" + str(argv) + "' timed out after "
    if not error.startswith(prefix):
        return None, ["error_prefix_mismatch"]
    match = TIMEOUT_SECONDS.fullmatch(error[len(prefix):])
    if match is None:
        return None, ["timeout_seconds_invalid"]
    try:
        seconds = float(match.group("seconds"))
    except (ValueError, OverflowError):
        return None, ["timeout_seconds_invalid"]
    if not math.isfinite(seconds):
        return None, ["timeout_seconds_invalid"]
    if (isinstance(pinned_timeout, bool) or not isinstance(pinned_timeout, (int, float))
            or not math.isfinite(pinned_timeout) or pinned_timeout <= 0):
        return None, ["timeout_seconds_out_of_range"]
    if not 0 < seconds <= pinned_timeout or pinned_timeout - seconds > 60:
        return None, ["timeout_seconds_out_of_range"]
    return seconds, []


def _bounded(argv, timeout):
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError("inspection_unavailable", f"{argv[0]}: {type(exc).__name__}") from exc
    return completed.returncode, completed.stdout.decode("utf-8", "replace")


def boot_time():
    """Host boot instant (whole seconds, current wall clock) from a grounded local source."""
    system = platform_module.system()
    if system == "Linux":
        try:
            text = Path("/proc/stat").read_text()
        except OSError as exc:
            raise ObservationError("inspection_unavailable", "cannot read /proc/stat") from exc
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "btime" and parts[1].isdigit():
                return int(parts[1]), "/proc/stat btime"
        raise ObservationError("boot_evidence_invalid", "no btime in /proc/stat")
    if system == "Darwin":
        code, text = _bounded(["/usr/sbin/sysctl", "-n", "kern.boottime"], 5)
        match = re.search(r"sec = ([0-9]+)", text)
        if code != 0 or match is None:
            raise ObservationError("boot_evidence_invalid", "kern.boottime unreadable")
        return int(match.group(1)), "sysctl kern.boottime"
    raise ObservationError("inspection_unavailable", f"unsupported platform {system}")


def process_table(proc_root="/proc"):
    """Same-user processes as {pid, ppid, pgid, uid, args, cwd}; other users are counted, never read.

    `incomplete` lists same-user processes whose required cwd/ownership evidence could not be read (they are
    not proven non-writers); `disappeared` counts processes verified gone or zombie during the scan."""
    uid = os.getuid()
    system = platform_module.system()
    processes, skipped, incomplete, disappeared = [], 0, [], 0
    if system == "Linux":
        proc = Path(proc_root)
        if not proc.is_dir():
            raise ObservationError("inspection_unavailable", "no /proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    skipped += 1  # Other users' processes are counted, never read.
                    continue
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z":
                    disappeared += 1  # A zombie has terminated; it cannot write anything.
                    continue
                args = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                cwd = os.readlink(entry / "cwd")
                processes.append({"pid": int(entry.name), "ppid": int(fields[1]), "pgid": int(fields[2]),
                                  "uid": uid, "args": args, "cwd": cwd})
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ESRCH) or not entry.exists():
                    disappeared += 1  # Verified: the process exited during the scan.
                else:
                    incomplete.append(int(entry.name))  # Same-user process whose required evidence was denied.
            except (ValueError, IndexError):
                incomplete.append(int(entry.name))
        return {"method": "/proc", "processes": processes, "skipped": skipped, "incomplete": incomplete[:64], "disappeared": disappeared}
    if system == "Darwin":
        code, listing = _bounded(["/bin/ps", "-Aww", "-o", "pid=,ppid=,pgid=,uid=,args="], 10)
        if code != 0 or not listing.strip():
            raise ObservationError("inspection_unavailable", "ps failed")
        code, files = _bounded(["/usr/sbin/lsof", "-n", "-P", "-a", "-u", str(uid), "-d", "cwd", "-F", "pn"], 20)
        if code not in (0, 1) or not files.strip():
            raise ObservationError("inspection_unavailable", "lsof failed")
        cwds, current = {}, None
        for line in files.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif line.startswith("n") and current is not None:
                cwds[current] = line[1:]
        for line in listing.splitlines():
            parts = line.split(None, 4)
            try:
                pid, ppid, pgid, owner = (int(value) for value in parts[:4])
            except ValueError:
                skipped += 1
                continue
            if owner != uid:
                skipped += 1
                continue
            if not isinstance(cwds.get(pid), str):
                # lsof reported no cwd for a same-user process. Only verified termination (gone or zombie)
                # lets it be skipped; anything else is required evidence that is unavailable.
                if len(incomplete) >= 64:
                    continue
                code, status = _bounded(["/bin/ps", "-o", "stat=", "-p", str(pid)], 5)
                token = status.split()[0] if status.split() else ""
                if code != 0 and not token or token.startswith("Z"):
                    disappeared += 1
                else:
                    incomplete.append(pid)
                continue
            processes.append({"pid": pid, "ppid": ppid, "pgid": pgid, "uid": owner,
                              "args": parts[4] if len(parts) > 4 else "", "cwd": cwds[pid]})
        return {"method": "/bin/ps + /usr/sbin/lsof", "processes": processes, "skipped": skipped, "incomplete": incomplete, "disappeared": disappeared}
    raise ObservationError("inspection_unavailable", f"unsupported platform {system}")


def default_inspector():
    seconds, source = boot_time()
    table = process_table()
    return {"boot_time": seconds, "boot_source": source, **table}


def observe(receipt_finished_at, receipt_pid, session_id, worktree, exempt=None, inspector=None):
    """Fresh bounded observation of the legacy execution boundary; decisions are recorded, nothing is signalled."""
    exempt = exempt or {}
    observed_at = room.now()
    result = {"label": "legacy", "observed_at": observed_at, "platform": platform_module.system(),
              "boot_time": None, "boot_source": None, "process_method": None, "matched_processes": [],
              "exempt_processes": [], "pid_reuse_after_boot": [], "skipped_count": 0, "incomplete_count": 0,
              "disappeared_count": 0, "reasons": [], "detail": None}
    try:
        facts = (inspector or default_inspector)()
    except ObservationError as exc:
        result.update(reasons=[exc.reason], detail=exc.detail)
        return result
    boot = facts.get("boot_time")
    now = room.parse_timestamp(observed_at).timestamp()
    finished = room.parse_timestamp(receipt_finished_at).timestamp()
    if type(boot) is not int or boot <= 0 or boot > now:
        result["reasons"].append("boot_evidence_invalid")
    else:
        result.update(boot_time=dt.datetime.fromtimestamp(boot, dt.timezone.utc).isoformat(), boot_source=facts.get("boot_source"))
        # Boot sources carry whole seconds; the boot second must exceed the receipt instant.
        if not boot > finished:
            result["reasons"].append("restart_required")
    resolved = Path(worktree).resolve()
    names = {str(worktree), str(resolved)}
    incomplete = facts.get("incomplete") or []
    result.update(process_method=facts.get("method"), skipped_count=facts.get("skipped", 0),
                  incomplete_count=len(incomplete), disappeared_count=facts.get("disappeared", 0))
    if incomplete:
        # Same-user processes without cwd/ownership evidence are not proven non-writers.
        result["reasons"].append("inspection_incomplete")
    for process in facts.get("processes", []):
        kinds = []
        args = process.get("args") or ""
        if session_id in args:
            kinds.append("argv_session")
        if any(name in args for name in names):
            kinds.append("argv_worktree")
        cwd = process.get("cwd")
        if isinstance(cwd, str) and cwd:
            try:
                current = Path(cwd).resolve()
            except OSError:
                current = Path(cwd)
            if current == resolved or resolved in current.parents:
                kinds.append("cwd_worktree")
        if receipt_pid in (process.get("pid"), process.get("pgid")):
            result["pid_reuse_after_boot"].append(process["pid"])
        if kinds:
            entry = {"pid": process["pid"], "match": kinds}
            if process["pid"] in exempt:
                result["exempt_processes"].append({**entry, "role": exempt[process["pid"]]})
            else:
                result["matched_processes"].append(entry)
    if result["matched_processes"]:
        result["reasons"].append("writer_present")
    return result


def _stream_hash(path, limit, kind="transcript", root=None, sink=None):
    """Bounded chunked digest of an owned regular file (symlinks refused, descriptor bound at open); with `sink`,
    every chunk is also written to that open binary handle (the caller places and fsyncs it)."""
    digest, length = hashlib.sha256(), 0
    with open_owned_regular(path, limit, kind, root) as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            length += len(chunk)
            if length > limit:
                raise ObservationError(kind + "_oversized")
            digest.update(chunk)
            if sink is not None:
                sink.write(chunk)
    return digest.hexdigest(), length


def verify_prefix(path, sha256, length, root=None):
    """True when the first `length` bytes of the live file still hash to the snapshot digest, False when they do
    not (or the file is shorter), None when the file could not be read (a transient failure is not tamper)."""
    digest, remaining = hashlib.sha256(), length
    try:
        with open_owned_regular(path, TRANSCRIPT_LIMIT, "transcript", root) as source:
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    return False
                digest.update(chunk)
                remaining -= len(chunk)
    except (OSError, ObservationError):
        return None
    return digest.hexdigest() == sha256


def locate_transcript(config, manifest, worktree):
    """The single transcript for the immutable session UUID; explicit templates are validated, never trusted blindly.

    Recovery requires actual expected-worktree metadata (at least one exact-session record whose cwd is the
    worktree); missing cwd evidence is refused separately from contradictory evidence. Reads are bounded and
    refuse symlinks."""
    if config.get("session_transcript_path"):
        explicit = Path(manifest["session_transcript_path"])
        if explicit.is_absolute():
            explicit = explicit.parent.resolve() / explicit.name  # the configured prefix is trusted; the leaf is never resolved
        with OwnedRoot(explicit.parent, kind="transcript") as root:
            opener = text_opener(TRANSCRIPT_LIMIT, root=root)
            session_paths.explicit_session_transcript(config["claude_config_dir"], manifest["session_id"], worktree,
                                                      str(explicit), require_cwd=True, opener=opener)
            return str(explicit)  # the validated name under the configured parent; a resolved leaf must never re-anchor the root
    with OwnedRoot(transcript_root(config, None), kind="transcript") as root:
        opener = text_opener(TRANSCRIPT_LIMIT, root=root)
        return session_paths.find_session_transcript(config["claude_config_dir"], manifest["session_id"], worktree,
                                                     manifest["session_transcript_path"], require_unique=True,
                                                     require_cwd=True, opener=opener)


def _changed_paths(initial, current):
    before = {entry["path"]: entry for entry in initial.get("entries", [])} if isinstance(initial, dict) else {}
    after = {entry["path"]: entry for entry in current.get("entries", [])}
    added = sorted(path for path in after if path not in before and after[path]["kind"] != "deleted")
    deleted = sorted(path for path in before if path not in after or after[path]["kind"] == "deleted")
    modified = sorted(path for path in after if path in before and after[path] != before[path] and path not in deleted)
    return {"added": added[:200], "modified": modified[:200], "deleted": deleted[:200],
            "counts": {"added": len(added), "modified": len(modified), "deleted": len(deleted)}}


@contextlib.contextmanager
def owner_locks(directory, job_dir, locks):
    """Non-blocking acquisition in the documented order; any failure means a cooperating owner is active."""
    with contextlib.ExitStack() as stack:
        try:
            if "handoff" in locks:
                stack.enter_context(room.lock_room(directory))
            if "job" in locks:
                stack.enter_context(room.lock_room(job_dir))
            if "lease" in locks:
                lease = stack.enter_context((job_dir / "worker.lock").open("a"))
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (room.RoomError, BlockingIOError, OSError) as exc:
            raise LockUnavailable("cooperating_owner_active") from exc
        yield


def _evidence(attempt, root):
    """(sha256 by name, raw bytes by name, reasons) for the attempt's evidence files, each reached through
    owned directory descriptors below the handoff directory `root`."""
    hashes, raw, reasons = {}, {}, []
    for name in EVIDENCE_FILES:
        try:
            if name in ("argv.json", "stdout.json", "parsed-result.json", "process-result.json"):
                raw[name] = read_owned(attempt / name, EVIDENCE_LIMIT, root=root)
                hashes[name] = room.sha(raw[name])
            else:
                hashes[name] = _stream_hash(attempt / name, EVIDENCE_LIMIT, kind="evidence", root=root)[0]
        except ObservationError as exc:
            if exc.reason != "evidence_missing":
                reasons.append(exc.reason)
            elif name == "process-result.json":
                reasons.append("receipt_invalid")
            elif name != "parsed-result.json":
                reasons.append("evidence_missing")
    return hashes, raw, reasons


def _receipt(raw):
    try:
        receipt = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(receipt, dict) or type(receipt.get("pid")) is not int or receipt["pid"] <= 1
            or type(receipt.get("return_code")) is not int or not isinstance(receipt.get("finished_at"), str)):
        return None
    try:
        room.parse_timestamp(receipt["finished_at"])
    except room.RoomError:
        return None
    return receipt


def _empty_report(handoff_id, job_id):
    return {"eligible": False, "restart_required": False, "reasons": [], "lock_order": LOCK_ORDER,
            "interruption": {"kind": None}, "identity": {"handoff_id": handoff_id, "job_id": job_id}, "candidate": None,
            "evidence_digest": None, "evidence_sha256": {}, "transcript": None, "stopped_work": None, "existing_recovery": None}


def blocked_report(handoff_id, job_id, reason):
    report = _empty_report(handoff_id, job_id)
    report["reasons"].append(reason)
    report["restart_required"] = reason == "restart_required"
    return report


def prefix_violation(transcript_path, state, root=None):
    """Reason code when an earlier recovery snapshot cannot be confirmed as a byte-exact prefix of the live transcript:
    transcript_prefix_changed (bytes differ or a snapshot record is malformed) or transcript_unreadable (the live file
    could not be read, which is refused without being treated as tamper). None when every snapshot is intact."""
    for entry in state.get("recovery_history", []):
        snapshot = entry.get("transcript") if isinstance(entry, dict) else None
        if snapshot is None:
            continue
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sha256"), str) or type(snapshot.get("length")) is not int:
            return "transcript_prefix_changed"  # A malformed snapshot record is treated as a violation, never as a pass.
        intact = verify_prefix(transcript_path, snapshot["sha256"], snapshot["length"], root)
        if intact is None:
            return "transcript_unreadable"
        if not intact:
            return "transcript_prefix_changed"
    return None


def audit(job, handoff_id, handoff_path, review_path, open_issues, job_dir, active_recovery=None,
          expect_recovery=None, locks=("handoff", "job", "lease"), inspector=None, on_locked=None):
    """Observe one interrupted attempt. Returns (public report, on_locked result or private facts).

    With expect_recovery, the handoff must already be recovery_prepared for that id and the
    current bytes must equal the prepared binding (dispatch and pre-launch rechecks).
    """
    report = _empty_report(handoff_id, job.get("id"))
    reasons = report["reasons"]
    job_dir = Path(job_dir)
    if active_recovery:
        report["existing_recovery"] = {"recovery_id": active_recovery["id"], "status": active_recovery["status"]}
    payload, result = job.get("payload") or {}, job.get("result")
    if (job.get("kind") != "implementation" or job.get("status") != "uncertain" or payload.get("handoff_id") != handoff_id
            or not isinstance(result, dict) or result.get("phase") != "blocked" or result.get("handoff_id") != handoff_id):
        reasons.append("job_not_eligible")
        return report, None
    if not Path(job_dir).is_dir():
        reasons.append("job_not_eligible")  # The private job directory is gone; nothing is recreated by an audit.
        return report, None
    try:
        root, directory, manifest, state, state_bytes = bind_handoff(handoff_path)
    except (impl.ImplementationError, ObservationError, OSError, ValueError, KeyError, TypeError):
        reasons.append("handoff_integrity")
        return report, None
    with root, contextlib.ExitStack() as descriptors:
        return _audit_bound(root, descriptors, directory, manifest, state, state_bytes, report, reasons, job, handoff_id, review_path,
                            open_issues, job_dir, active_recovery, expect_recovery, locks, inspector, on_locked)


def _audit_bound(root, descriptors, directory, manifest, state, state_bytes, report, reasons, job, handoff_id, review_path, open_issues,
                 job_dir, active_recovery, expect_recovery, locks, inspector, on_locked):
    payload, result = job.get("payload") or {}, job.get("result")
    registered = Path(str(payload.get("handoff_path", ""))).resolve()
    if registered.is_file():
        registered = registered.parent
    if registered != directory or manifest["handoff_id"] != handoff_id:
        reasons.append("job_not_eligible")
        return report, None
    report["identity"].update(spec_revision=manifest["revision"], spec_sha256=manifest["spec_sha256"],
                              baseline_commit=manifest["baseline_commit"], session_id=manifest["session_id"])
    prepared = state.get("recovery") if isinstance(state.get("recovery"), dict) else None
    if expect_recovery is None:
        if state.get("phase") == "recovery_prepared":
            if prepared and active_recovery and prepared.get("recovery_id") == active_recovery["id"]:
                reasons.append("recovery_already_exists")
            else:
                reasons.append("projection_out_of_sync")
            return report, None
        if active_recovery:
            reasons.append("projection_out_of_sync" if state.get("phase") == "blocked" else "recovery_already_exists")
            return report, None
        if state.get("phase") != "blocked" or state.get("replay_allowed") is not False:
            reasons.append("job_not_eligible")
            return report, None
    else:
        if (state.get("phase") != "recovery_prepared" or not prepared or prepared.get("recovery_id") != expect_recovery
                or prepared.get("predecessor_job_id") != job.get("id")):
            reasons.append("recovery_binding_mismatch")
            return report, None
    attempt_count, attempt_path = state.get("attempt_count"), state.get("attempt_path")
    attempt = directory / "attempts" / (f"{attempt_count:04d}" if type(attempt_count) is int else "invalid")
    if (type(attempt_count) is not int or attempt_count < 1 or attempt_path != str(attempt)
            or result.get("attempt_count") != attempt_count or result.get("attempt_path") != attempt_path
            or result.get("error") != state.get("error") or not isinstance(state.get("started_at"), str)
            or not isinstance(state.get("finished_at"), str)):
        reasons.append("attempt_identity_mismatch")
        return report, None
    recoveries = directory / "recoveries"
    if ((directory / "attempts").is_symlink() or attempt.is_symlink() or not attempt.is_dir() or recoveries.is_symlink()
            or (recoveries.exists() and recoveries.resolve() != recoveries)):
        reasons.append("evidence_unsafe")
        return report, None
    report["interruption"].update(attempt_count=attempt_count)
    hashes, raw, evidence_reasons = _evidence(attempt, root)
    reasons.extend(evidence_reasons)
    if any(attempt.glob("gate-*")):
        reasons.append("gate_directory_present")
    if reasons:
        return report, None
    receipt = _receipt(raw["process-result.json"])
    if receipt is None:
        reasons.append("receipt_invalid")
        return report, None
    report["interruption"].update(receipt={"return_code": receipt["return_code"], "finished_at": receipt["finished_at"]})
    observed_now = room.parse_timestamp(room.now())
    try:
        started, state_finished = room.parse_timestamp(state["started_at"]), room.parse_timestamp(state["finished_at"])
    except room.RoomError:
        reasons.append("receipt_time_order")
        return report, None
    receipt_finished = room.parse_timestamp(receipt["finished_at"])
    if not started <= receipt_finished <= state_finished <= observed_now:
        reasons.append("receipt_time_order")
        return report, None
    try:
        config = json.loads(read_owned(directory / "implementation-config.json", EVIDENCE_LIMIT, "handoff", root=root))
        if not isinstance(config, dict) or any(key not in config for key in ("claude_bin", "model", "timeout_seconds", "claude_config_dir")):
            raise ValueError("pinned configuration is incomplete")
    except (ObservationError, ValueError):
        reasons.append("handoff_integrity")
        return report, None
    try:
        argv = json.loads(raw["argv.json"])
    except ValueError:
        argv = None
    session_flag = "--session-id" if attempt_count == 1 else "--resume"
    if (not isinstance(argv, list) or not all(isinstance(value, str) for value in argv) or not argv
            or argv[0] != config["claude_bin"] or impl._flag(argv, "--model") != config["model"]
            or impl._flag(argv, session_flag) != manifest["session_id"]):
        reasons.append("argv_mismatch")
        return report, None
    error = state.get("error")
    seconds, timeout_reasons = parse_timeout_error(error, argv, config["timeout_seconds"])
    kind = None
    if seconds is not None:
        kind = "model_timeout"
        report["interruption"]["timeout_seconds"] = seconds
        finished_model = state.get("model_finished_at")
        if finished_model is not None:
            try:
                if not room.parse_timestamp(finished_model) < started:
                    reasons.append("attempt_identity_mismatch")
            except room.RoomError:
                reasons.append("attempt_identity_mismatch")
        try:
            output = json.loads(raw["stdout.json"])
        except ValueError:
            output = None
        if (isinstance(output, dict) and output.get("type") == "result" and output.get("subtype") == "success"
                and output.get("is_error") is False and output.get("session_id") == manifest["session_id"]):
            reasons.append("contradictory_stdout")
    elif error == GENERIC_ERROR:
        finished_model = state.get("model_finished_at")
        try:
            # The engine records model_finished_at after the process receipt and before the state finish;
            # a current model finish outside that window is not this attempt's receipt evidence.
            model_time_ok = (isinstance(finished_model, str)
                             and started <= receipt_finished <= room.parse_timestamp(finished_model) <= state_finished)
        except room.RoomError:
            model_time_ok = False
        if isinstance(finished_model, str) and not model_time_ok:
            reasons.append("model_finish_time_order")
        try:
            output = json.loads(raw["stdout.json"])
            parsed = json.loads(raw["parsed-result.json"]) if "parsed-result.json" in raw else None
        except ValueError:
            output, parsed = None, None
        if (not model_time_ok or state.get("model_return_code") != receipt["return_code"] or receipt["return_code"] != 1
                or state.get("model_stdout_sha256") != hashes["stdout.json"] or parsed is None
                or room.canonical(parsed) != room.canonical(output)):
            reasons.append("error_not_diagnosable")
        else:
            codes = classify_quota_result(parsed, manifest["session_id"])
            if codes:
                reasons.extend(codes)
            else:
                kind = "session_usage_limit"
    else:
        # A timeout-shaped error whose seconds value is invalid or out of the pinned window reports the specific code.
        reasons.extend(timeout_reasons if timeout_reasons and timeout_reasons != ["error_prefix_mismatch"] else ["error_not_diagnosable"])
    if reasons:
        return report, None
    report["interruption"]["kind"] = kind
    current = room.status_report(review_path)
    if (not current["agreement"] or current["current_revision"] != manifest["revision"]
            or current["spec_sha256"] != manifest["spec_sha256"]):
        reasons.append("spec_mismatch")
    if open_issues:
        reasons.append("open_findings")
    worktree = Path(manifest["worktree_path"])
    try:
        candidate = impl.candidate_snapshot(worktree)
    except (impl.ImplementationError, OSError):
        reasons.append("candidate_unreadable")
        return report, None
    if candidate["head"] != manifest["baseline_commit"]:
        reasons.append("candidate_head_moved")
    report["candidate"] = {"sha256": candidate["sha256"], "head": candidate["head"], "path_count": len(candidate["entries"]),
                           "changed_vs_initial": _changed_paths(state.get("initial_candidate"), candidate)}
    try:
        transcript_path = locate_transcript(config, manifest, worktree)
        transcript_home = descriptors.enter_context(OwnedRoot(transcript_root(config, transcript_path), kind="transcript"))
        transcript_sha, transcript_length = _stream_hash(Path(transcript_path), TRANSCRIPT_LIMIT, root=transcript_home)
    except session_paths.SessionPathError as exc:
        reasons.append(exc.code)
        return report, None
    except ObservationError as exc:
        reasons.append(exc.reason)
        return report, None
    except OSError:
        reasons.append("transcript_missing_or_ambiguous")
        return report, None
    report["transcript"] = {"sha256": transcript_sha, "length": transcript_length}
    prefix_problem = prefix_violation(transcript_path, state, transcript_home)
    if prefix_problem:
        reasons.append(prefix_problem)
    report["evidence_sha256"] = dict(hashes)
    report["evidence_digest"] = impl._digest({"attempt_count": attempt_count, "files": hashes, "receipt": receipt,
                                              "error": error, "transcript": report["transcript"]})
    if expect_recovery is not None:
        if candidate != prepared.get("candidate"):
            reasons.append("candidate_changed")
        if hashes != prepared.get("evidence_sha256") or report["evidence_digest"] != prepared.get("evidence_digest"):
            reasons.append("evidence_changed")
        if report["transcript"] != prepared.get("transcript"):
            reasons.append("transcript_changed")
        record = directory / "recoveries" / expect_recovery / "record.json"
        try:
            if room.sha(read_owned(record, EVIDENCE_LIMIT, "record", root=root)) != prepared.get("record_sha256"):
                reasons.append("evidence_changed")
        except ObservationError:
            reasons.append("evidence_changed")
    if reasons:
        return report, None
    private = {"root": root, "directory": directory, "manifest": manifest, "state": state, "attempt": attempt, "attempt_count": attempt_count,
               "receipt": receipt, "hashes": hashes, "candidate": candidate, "kind": kind, "seconds": seconds,
               "transcript_path": Path(transcript_path), "transcript_root": transcript_home, "transcript": report["transcript"],
               "config": config, "argv": argv}
    try:
        with owner_locks(directory, job_dir, set(locks)):
            observation = observe(receipt["finished_at"], receipt["pid"], manifest["session_id"], worktree,
                                  exempt={os.getpid(): "audit_process"}, inspector=inspector)
            # Rechecks under the owner locks, immediately before any durable preparation or dispatch record:
            # the candidate, the transcript, every evidence file and the handoff projection must still be the
            # bytes that were audited above. A change during the observation refuses instead of being adopted.
            try:
                if impl.candidate_snapshot(worktree) != candidate:
                    observation["reasons"].append("candidate_changed")
            except (impl.ImplementationError, OSError):
                observation["reasons"].append("candidate_unreadable")
            try:
                if _stream_hash(Path(transcript_path), TRANSCRIPT_LIMIT, root=transcript_home) != (transcript_sha, transcript_length):
                    observation["reasons"].append("transcript_changed")
            except ObservationError as exc:
                observation["reasons"].append(exc.reason)
            except OSError:
                observation["reasons"].append("transcript_unreadable")
            try:
                again, raw_again, again_reasons = _evidence(attempt, root)
                if again_reasons or again != hashes or raw_again != raw or any(attempt.glob("gate-*")):
                    observation["reasons"].append("evidence_changed")
                if read_owned(directory / "state.json", EVIDENCE_LIMIT, "handoff", root=root) != state_bytes:
                    observation["reasons"].append("evidence_changed")
            except ObservationError:
                observation["reasons"].append("evidence_changed")
            report["stopped_work"] = {key: observation[key] for key in ("label", "observed_at", "platform", "boot_time", "boot_source",
                                                                        "process_method", "matched_processes", "exempt_processes",
                                                                        "pid_reuse_after_boot", "skipped_count", "incomplete_count",
                                                                        "disappeared_count", "detail")}
            reasons.extend(observation["reasons"])
            report["restart_required"] = "restart_required" in reasons
            if reasons:
                return report, None
            report["eligible"] = True
            private["observation"] = observation
            if on_locked is not None:
                return report, on_locked(report, private)
            return report, private
    except LockUnavailable:
        reasons.append("cooperating_owner_active")
        return report, None


def prepare(report, private, recovery_id, room_id, request_id, job, diagnosis, remaining_work, authorization, supplied, registry_home):
    """Durable one-use preparation, run while the owner locks are held by audit()."""
    directory, manifest, state = private["directory"], private["manifest"], private["state"]
    expected = {"spec_revision": manifest["revision"], "spec_sha256": manifest["spec_sha256"],
                "candidate_sha256": private["candidate"]["sha256"], "evidence_digest": report["evidence_digest"]}
    for key, value in expected.items():
        if supplied.get(key) != value:
            raise room.RoomError(f"Recovery request does not match the fresh audit: {key}")
    root = private["root"]
    _require_bound_path(root, directory)  # a change already visible at this boundary is refused before anything is written
    # Every durable write below is relative to the bound descriptor: a root swapped at any later instant, even at the
    # moment of a write, lands nothing outside the bound directory.
    try:
        recoveries_fd = directory_below(root, ("recoveries",), "record", create=True)
    except ObservationError as exc:
        raise room.RoomError("Recovery is not eligible: evidence_unsafe") from exc
    snapshot = "transcript-snapshot.jsonl"
    try:
        try:
            home_fd = directory_below(recoveries_fd, (recovery_id,), "record", create=True, exclusive=True)
        except ObservationError as exc:
            raise room.RoomError("Recovery is not eligible: evidence_unsafe") from exc
        os.fsync(recoveries_fd)  # the new one-use directory entry is durable before anything is placed inside it
        try:
            record_bytes, created = _write_preparation(private, home_fd, snapshot, recovery_id, room_id, request_id, job, diagnosis,
                                                       remaining_work, authorization, registry_home, report)
        except BaseException:
            for name in (snapshot, "record.json"):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=home_fd)
            os.close(home_fd)
            home_fd = None
            with contextlib.suppress(OSError):
                os.rmdir(recovery_id, dir_fd=recoveries_fd)  # nothing durable was recorded for this id
                os.fsync(recoveries_fd)
            raise
        os.close(home_fd)
    finally:
        os.close(recoveries_fd)
    record_sha = room.sha(record_bytes)
    state["recovery"] = {"recovery_id": recovery_id, "predecessor_job_id": job["id"], "predecessor_attempt": private["attempt_count"],
                         "kind": private["kind"], "candidate": private["candidate"], "evidence_sha256": private["hashes"],
                         "evidence_digest": report["evidence_digest"], "transcript": private["transcript"],
                         "record_sha256": record_sha, "prepared_at": created}
    state.setdefault("recovery_history", []).append({"recovery_id": recovery_id, "status": "prepared", "at": created,
                                                     "predecessor_attempt": private["attempt_count"], "transcript": private["transcript"]})
    state["phase"] = "recovery_prepared"
    write_json_below(root.fd, "state.json", state, "handoff")
    return {"record_sha256": record_sha, "kind": private["kind"], "created_at": created}


def _write_preparation(private, home_fd, snapshot, recovery_id, room_id, request_id, job, diagnosis, remaining_work,
                       authorization, registry_home, report):
    """Snapshot copy and immutable record, both placed atomically relative to the bound recovery directory."""
    manifest, state = private["manifest"], private["state"]
    temporary = snapshot + "." + uuid.uuid4().hex + ".tmp"
    fd = _create_component(temporary, CREATE_FLAGS, 0o600, home_fd)
    try:
        try:
            sink = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with sink:
            copied = _stream_hash(private["transcript_path"], TRANSCRIPT_LIMIT, root=private["transcript_root"], sink=sink)
            sink.flush()
            os.fsync(sink.fileno())
        if copied != (private["transcript"]["sha256"], private["transcript"]["length"]):
            raise ObservationError("transcript_changed")
        os.replace(temporary, snapshot, src_dir_fd=home_fd, dst_dir_fd=home_fd)
        os.fsync(home_fd)
    except ObservationError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=home_fd)
        raise room.RoomError("Recovery is not eligible: " + exc.reason) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=home_fd)
        raise
    created = room.now()
    record = {"recovery_id": recovery_id, "room_id": room_id, "handoff_id": manifest["handoff_id"], "request_id": request_id,
              "predecessor_job_id": job["id"], "created_at": created, "evidence_label": "legacy",
              "original_job": job, "failed_state_snapshot": state, "interruption": {"kind": private["kind"],
              "timeout_seconds": private["seconds"], "attempt_count": private["attempt_count"], "receipt": private["receipt"]},
              "evidence_sha256": private["hashes"], "evidence_digest": report["evidence_digest"],
              "identity": {**report["identity"], "manifest_sha256": state["manifest_sha256"],
                           "original_authorization_sha256": manifest["pinned_files"]["authorization.txt"]},
              "candidate": private["candidate"], "transcript": {**private["transcript"], "snapshot": snapshot},
              "observation": private["observation"], "diagnosis": diagnosis, "remaining_work": remaining_work,
              "authorization": authorization, "argv_sha256": room.sha(room.canonical(private["argv"]).encode()),
              "registry_home": str(registry_home)}
    return write_json_below(home_fd, "record.json", record, "record"), created


def _require_bound_path(root, directory):
    """Refuse a durable path-based write when the handoff path no longer names the bound directory."""
    if isinstance(root, OwnedRoot) and directory_identity(directory, "handoff") != root.identity:
        raise room.RoomError("Recovery is not eligible: evidence_unsafe")


def write_dispatch(directory, recovery_id, successor_job_id, root=None):
    """Record the single dispatch relative to the bound recovery directory. Without a caller-held root, a transient
    root is bound at the directory's current identity; the write is still descriptor-relative."""
    transient = None
    try:
        if not isinstance(root, OwnedRoot):
            transient = root = OwnedRoot(Path(directory), directory_identity(directory, "handoff"), "handoff")
        _require_bound_path(root, directory)
        try:
            home_fd = directory_below(root, ("recoveries", recovery_id), "record")
        except ObservationError as exc:
            raise room.RoomError("Recovery dispatch refused: evidence_unsafe") from exc
        try:
            write_json_below(home_fd, "dispatch.json", {"recovery_id": recovery_id, "successor_job_id": successor_job_id,
                                                        "dispatched_at": room.now()}, "record")
        finally:
            os.close(home_fd)
    except ObservationError as exc:
        raise room.RoomError("Recovery dispatch refused: " + exc.reason) from exc
    finally:
        if transient is not None:
            transient.close()
    return str(directory / "recoveries" / recovery_id / "dispatch.json")


def invalidate(handoff_path, recovery_id, reason, successor_job_id=None):
    """Audited return to the retryable blocked projection; the record, snapshot and failed job stay."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        try:
            root, directory, manifest, state, _ = bind_handoff(directory)
        except ObservationError as exc:
            raise impl.ImplementationError("Handoff storage is not safely readable: " + exc.reason) from exc
        with root:
            stamp = room.now()
            try:
                home_fd = directory_below(root, ("recoveries", recovery_id), "record")
            except ObservationError:
                home_fd = None  # no usable recovery directory to annotate; the projection transition below still applies
            if home_fd is not None:
                try:
                    if not exists_below(home_fd, "invalidation.json"):
                        write_json_below(home_fd, "invalidation.json", {"recovery_id": recovery_id, "reason": reason,
                                                                        "successor_job_id": successor_job_id, "invalidated_at": stamp}, "record")
                finally:
                    os.close(home_fd)
            prepared = state.get("recovery") if isinstance(state.get("recovery"), dict) else None
            if state.get("phase") == "recovery_prepared" and prepared and prepared.get("recovery_id") == recovery_id:
                state["phase"] = "blocked"
                state["recovery"] = None
                state.setdefault("recovery_history", []).append({"recovery_id": recovery_id, "status": "invalidated", "reason": reason,
                                                                 "successor_job_id": successor_job_id, "at": stamp})
                write_json_below(root.fd, "state.json", state, "handoff")
                return True
            return False


def lineage(state):
    """Allowlisted lineage summary for status output."""
    prepared = state.get("recovery") if isinstance(state.get("recovery"), dict) else None
    return {"phase": state.get("phase"), "attempt_count": state.get("attempt_count"),
            "active_recovery": {key: prepared.get(key) for key in ("recovery_id", "predecessor_job_id", "predecessor_attempt", "kind", "prepared_at", "launched_at")} if prepared else None,
            "recovery_history": [{key: entry.get(key) for key in ("recovery_id", "status", "reason", "at", "predecessor_attempt", "successor_job_id")}
                                 for entry in state.get("recovery_history", []) if isinstance(entry, dict)]}
