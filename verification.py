#!/usr/bin/env python3
"""Audited verification-only retry after a completed model result.

The original model turn returned normally with a valid successful terminal result, the candidate was snapshotted, and a
pinned verification gate then hit the pinned gate timeout. This lane audits exactly that shape and reruns only the pinned
gate argv arrays, never the model, inside a fresh private copy of the audited candidate with a private TMPDIR.

Nothing here calls a model, a delegate or the network. The copy boundary is not an OS sandbox: it proves nothing about a
descendant that is invisible to the lease, process-group, cwd and argument observations, and before/after equality proves
equality at those instants only. That residual was accepted by the user for this project's identified offline unit tests
and local validators only, never for arbitrary gates.
"""

import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import platform as platform_module
import re
import selectors
import stat
import subprocess
import threading
import time
import uuid

import implementation as impl
import recovery
import room
import session_paths


BOUNDARY = "isolated_copy"
MARKER_ENV = "PROJECT_ROOM_VERIFICATION_ID"
BUDGET_MAX = 7200
PROPOSED_BUDGET = 900
COPY_FILE_LIMIT = 64 * 1024 * 1024
COPY_TOTAL_LIMIT = 1024 ** 3
COPY_ENTRY_LIMIT = 100000
PATH_DEPTH_LIMIT = 64
GATE_FILES = ("stdout.txt", "stderr.txt", "process-result.json")
MARKER_SCAN_LIMIT = 1024 * 1024  # bytes of one process environment read on Linux; larger environments count as incomplete
GIT_LISTING_LIMIT = 16 * 1024 * 1024  # bytes of one captured git listing (ls-files, rev-parse, check-ignore); more refuses
PROCESS_LISTING_LIMIT = 64 * 1024 * 1024  # bytes of one captured macOS ps -E listing; more refuses as inspection_unavailable
CAPTURE_INPUT_LIMIT = 60 * 1024  # bytes handed to a captured child's stdin from a helper thread; larger payloads refuse before spawn
CAPTURE_TIMEOUT_SECONDS = 60
LOCK_ORDER = recovery.LOCK_ORDER
INVALIDATING = {"candidate_changed", "candidate_head_moved", "evidence_changed", "transcript_changed",
                "transcript_prefix_changed", "spec_mismatch", "open_findings", "verification_binding_mismatch"}
SUPPORTED_SCOPE = {
    "boundary": BOUNDARY,
    "entries": "regular files only; symlink, submodule and special candidate entries are refused",
    "max_file_bytes": COPY_FILE_LIMIT, "max_total_bytes": COPY_TOTAL_LIMIT, "max_entries": COPY_ENTRY_LIMIT,
    "max_path_depth": PATH_DEPTH_LIMIT, "repository_metadata": "not copied; gates needing .git or external mutable state may fail",
    "gates": "the pinned argv arrays only; an argv element naming the original worktree path is refused before launch"}
LIMITATIONS = [
    "not_an_os_sandbox: the copy and private TMPDIR remove known-path channels only; an unobserved survivor could still touch shared user-level state",
    "descendants_unproven: a free lease, an empty process group and the writer scan prove only what they observe at that instant",
    "candidate_equality_is_point_in_time: before/after equality does not prove the absence of transient, ignored, external or later writes",
    "legacy_gate_receipt: the original gate recorded only its exit receipt pid; no spawn receipt or marker exists for it",
    "argv_check_is_literal: refusing argv elements that name the worktree cannot catch every reference inside executable code",
    "marker_is_correlation_only: " + MARKER_ENV + " never authorizes signalling a process or accepting ownership",
]


def validate_budget(value, original):
    """The recorded per-retry gate budget: an integer (never a boolean) between the pinned original and BUDGET_MAX."""
    if type(value) is not int or value <= 0 or value > BUDGET_MAX:
        raise room.RoomError("gate_timeout_seconds must be a positive integer of at most %d seconds" % BUDGET_MAX)
    if isinstance(original, bool) or not isinstance(original, (int, float)) or value < original:
        raise room.RoomError("gate_timeout_seconds must be at least the original pinned gate timeout")
    return value


def _empty_report(handoff_id, job_id):
    return {"eligible": False, "boundary": BOUNDARY, "reasons": [], "lock_order": LOCK_ORDER,
            "identity": {"handoff_id": handoff_id, "job_id": job_id}, "generation": None, "failed_gate": None,
            "gates_sha256": None, "candidate": None, "transcript": None, "evidence_digest": None, "evidence_sha256": {},
            "stopped_work": None, "existing_verification": None,
            "budget": {"original_seconds": None, "minimum_seconds": None, "maximum_seconds": BUDGET_MAX, "proposed_seconds": PROPOSED_BUDGET},
            "supported_scope": dict(SUPPORTED_SCOPE), "limitations": list(LIMITATIONS)}


def blocked_report(handoff_id, job_id, reason):
    report = _empty_report(handoff_id, job_id)
    report["reasons"].append(reason)
    return report


def _primary_from_bytes(raw, session_id, started_at, finished_at, report, expected):
    """Primary StructuredOutput producer re-derived from verified transcript bytes (same rule as room.primary_producer_evidence)."""
    start, finish = room.parse_timestamp(started_at), room.parse_timestamp(finished_at)
    candidates = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise recovery.ObservationError("transcript_unparsable") from exc
        if not isinstance(event, dict) or event.get("type") != "assistant" or event.get("sessionId") != session_id:
            continue
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        outputs = [item for item in message["content"] if isinstance(item, dict)
                   and item.get("type") == "tool_use" and item.get("name") == "StructuredOutput"]
        if not outputs:
            continue
        try:
            stamp = room.parse_timestamp(event.get("timestamp"))
        except room.RoomError as exc:
            raise recovery.ObservationError("identity_mismatch") from exc
        if start <= stamp <= finish:
            for index, output in enumerate(outputs):
                candidates.append((stamp, number, index, event, message, output))
    if not candidates:
        raise recovery.ObservationError("identity_mismatch")
    _, _, _, event, message, output = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    if room.canonical(output.get("input")) != room.canonical(report) or message.get("model") not in expected:
        raise recovery.ObservationError("identity_mismatch")
    return {"message_id": message.get("id"), "tool_use_id": output.get("id"), "timestamp": event.get("timestamp"),
            "primary_model": message.get("model"), "structured_output_sha256": room.sha(room.canonical(report).encode("utf-8"))}


def _scope_problem(candidate, worktree):
    """Reason code when the audited candidate lies outside the supported copy scope, else None."""
    entries = candidate.get("entries") if isinstance(candidate, dict) else None
    if not isinstance(entries, list) or len(entries) > COPY_ENTRY_LIMIT:
        return "candidate_unsupported_entry"
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return "candidate_unsupported_entry"
        parts = Path(entry["path"]).parts
        if (not parts or len(parts) > PATH_DEPTH_LIMIT or entry["path"].startswith("/") or "\0" in entry["path"]
                or any(part in ("", ".", "..") or "/" in part for part in parts) or parts[0] == ".git"):
            return "candidate_path_unsupported"
        if entry.get("kind") == "deleted":
            continue
        if entry.get("kind") != "file" or type(entry.get("mode")) is not int or not isinstance(entry.get("sha256"), str):
            return "candidate_unsupported_entry"
    return None


def _bounded_capture(argv, limit, timeout=CAPTURE_TIMEOUT_SECONDS, cwd=None, input_bytes=None):
    """(return code, stdout bytes) of one child this call created, with stdout captured up to `limit` bytes (never more than
    limit + 1 are requested). The reader itself owns the deadline: it waits on the pipe with a selector, so a grandchild that
    inherits the pipe cannot keep this call alive past `timeout`. Output beyond the limit or a deadline stops only the child
    this call created and raises ObservationError (listing_oversized / listing_unavailable). Input is fed from a helper thread
    so a child that writes before it reads cannot deadlock the reader on any platform's pipe size. stderr is discarded: the
    caller's reason code and the return code are the authoritative error, never prose."""
    if input_bytes is not None and len(input_bytes) > CAPTURE_INPUT_LIMIT:
        raise recovery.ObservationError("listing_oversized", "input")
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        raise recovery.ObservationError("listing_unavailable", type(exc).__name__) from exc
    writer = None
    if input_bytes is not None:
        def feed():
            with contextlib.suppress(OSError, ValueError):
                process.stdin.write(input_bytes)
                process.stdin.flush()
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
    deadline = time.monotonic() + timeout
    data, truncated, expired = bytearray(), False, False
    try:
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    expired = True
                    break
                if not selector.select(min(remaining, 1.0)):
                    continue
                try:
                    chunk = os.read(descriptor, min(65536, limit + 1 - len(data)))
                except BlockingIOError:
                    time.sleep(0.001)  # readiness without bytes never becomes a hot spin
                    continue
                if not chunk:
                    break
                data += chunk
                if len(data) > limit:
                    truncated = True
                    break
        if truncated or expired:
            with contextlib.suppress(OSError):
                process.kill()
        try:
            code = process.wait(timeout=max(deadline - time.monotonic(), 0.5))
        except subprocess.TimeoutExpired:
            expired = True
            with contextlib.suppress(OSError):
                process.kill()
            code = process.wait()
    finally:
        with contextlib.suppress(OSError):
            process.stdout.close()
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5.0)  # an unreapable child is reported, never waited on without bound
        if writer is not None:
            writer.join(timeout=1.0)
    if truncated:
        raise recovery.ObservationError("listing_oversized", "output")
    if expired:
        raise recovery.ObservationError("listing_unavailable", "timeout")
    return code, bytes(data)


def _git_listing(worktree, *args):
    """Bounded git listing for this lane: over-limit output or a failed command refuses instead of buffering or raising prose."""
    try:
        code, data = _bounded_capture(["git", "-C", str(worktree), *args], GIT_LISTING_LIMIT)
    except recovery.ObservationError as exc:
        if exc.reason == "listing_oversized":
            raise recovery.ObservationError("candidate_oversized", "listing") from exc
        raise recovery.ObservationError("candidate_unreadable", exc.detail) from exc
    if code != 0:
        raise recovery.ObservationError("candidate_unreadable", "git:%d" % code)
    return data


def candidate_snapshot(worktree):
    """Bounded fingerprint with exactly the structure and digest of implementation.candidate_snapshot, for this lane only.

    Every entry is stat-checked before any byte is read: per-file and total sizes, entry count and path depth are bounded
    by the module limits, symlink components are never followed (owned descriptor-relative reads below the worktree),
    special entries are refused, and a file that changes or grows while it is hashed refuses. Symlink entries are recorded
    exactly as the shared primitive records them so historical snapshots compare equal; the audit refuses them separately."""
    worktree = Path(worktree)
    try:
        names = set(_git_listing(worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
        head = _git_listing(worktree, "rev-parse", "HEAD").decode("ascii").strip()
        staged = _git_listing(worktree, "ls-files", "--stage", "-z").split(b"\0")
    except (OSError, UnicodeDecodeError) as exc:
        raise recovery.ObservationError("candidate_unreadable", type(exc).__name__) from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise recovery.ObservationError("candidate_unreadable", "head")
    names.discard(b"")
    if len(names) > COPY_ENTRY_LIMIT or len(staged) > COPY_ENTRY_LIMIT + 1:
        raise recovery.ObservationError("candidate_oversized", "entries")
    entries, total = [], 0
    with recovery.OwnedRoot(worktree, kind="candidate") as root:
        for raw in sorted(names):
            name = os.fsdecode(raw)
            parts = Path(name).parts
            if (not parts or len(parts) > PATH_DEPTH_LIMIT or name.startswith("/") or "\0" in name
                    or any(part in ("", ".", "..") or "/" in part for part in parts)):
                raise recovery.ObservationError("candidate_path_unsupported")
            path = worktree / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                entries.append({"path": name, "kind": "deleted"})
                continue
            except OSError as exc:
                raise recovery.ObservationError("candidate_unreadable", type(exc).__name__) from exc
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    entries.append({"path": name, "kind": "symlink", "mode": mode, "target": os.readlink(path)})
                except OSError as exc:
                    raise recovery.ObservationError("candidate_unreadable", type(exc).__name__) from exc
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise recovery.ObservationError("candidate_unsupported_entry")
            if metadata.st_size > COPY_FILE_LIMIT:
                raise recovery.ObservationError("candidate_oversized", "file")
            total += metadata.st_size
            if total > COPY_TOTAL_LIMIT:
                raise recovery.ObservationError("candidate_oversized", "total")
            try:
                digest, length = recovery._stream_hash(path, COPY_FILE_LIMIT, kind="candidate", root=root)
            except recovery.ObservationError as exc:
                if exc.reason.endswith("_oversized"):
                    raise recovery.ObservationError("candidate_oversized", "grew") from exc
                if exc.reason.endswith("_missing"):
                    raise recovery.ObservationError("candidate_changed", "vanished") from exc
                raise recovery.ObservationError("candidate_unsupported_entry", exc.detail) from exc
            try:
                after = path.lstat()
            except OSError as exc:
                raise recovery.ObservationError("candidate_changed", "vanished") from exc
            if (length != metadata.st_size or (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode)
                    != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)):
                raise recovery.ObservationError("candidate_changed", "fingerprint")
            entries.append({"path": name, "kind": "file", "mode": mode, "sha256": digest})
    index = []
    for entry in staged:
        if entry:
            try:
                metadata, name = entry.split(b"\t", 1)
                mode, blob, stage = metadata.decode("ascii").split()
            except (ValueError, UnicodeDecodeError) as exc:
                raise recovery.ObservationError("candidate_unreadable", "index") from exc
            index.append({"path": os.fsdecode(name), "mode": mode, "blob": blob, "stage": int(stage)})
    payload = {"head": head, "entries": entries, "index": index}
    return {**payload, "sha256": impl._digest(payload)}


def _names_worktree(gates, worktree):
    names = {str(worktree), str(Path(worktree).resolve())}
    return any(any(name in arg for name in names) for gate in gates for arg in gate)


def _gate_names(attempt):
    """Every gate-* entry of the attempt directory (files, symlinks and directories alike), so a planted later gate is contradictory evidence."""
    try:
        return {entry.name for entry in os.scandir(attempt) if entry.name.startswith("gate-")}
    except OSError:
        return {"unreadable"}


def _gate_evidence(attempt, index, root):
    """(hashes, receipt raw or None, reasons) for gate-<index> under the attempt, through owned reads."""
    hashes, receipt, reasons = {}, None, []
    gate_dir = attempt / ("gate-%d" % index)
    for name in GATE_FILES:
        try:
            if name == "process-result.json":
                receipt = recovery.read_owned(gate_dir / name, recovery.EVIDENCE_LIMIT, root=root)
                hashes[name] = room.sha(receipt)
            else:
                hashes[name] = recovery._stream_hash(gate_dir / name, recovery.EVIDENCE_LIMIT, kind="evidence", root=root)[0]
        except recovery.ObservationError as exc:
            reasons.append("gate_evidence_missing" if exc.reason == "evidence_missing" else exc.reason)
    return hashes, receipt, reasons


def _evidence_digest(attempt_count, hashes, gate_hashes, model_receipt, gate_receipt, error, gates_sha256, report, candidate, transcript):
    return impl._digest({"attempt_count": attempt_count, "files": hashes, "gates": gate_hashes, "model_receipt": model_receipt,
                         "gate_receipt": gate_receipt, "error": error, "gates_sha256": gates_sha256,
                         "report_sha256": room.sha(room.canonical(report).encode("utf-8")),
                         "candidate_sha256": candidate["sha256"], "transcript": transcript})


def audit(job, handoff_id, handoff_path, review_path, open_issues, job_dir, active_verification=None, expect_verification=None,
          locks=("handoff", "job", "lease"), inspector=None, on_locked=None, earlier_pids=None, earlier_markers=None):
    """Observe one completed-generation, gate-timeout attempt. Returns (public report, on_locked result or private facts)."""
    report = _empty_report(handoff_id, job.get("id"))
    reasons = report["reasons"]
    job_dir = Path(job_dir)
    if active_verification:
        report["existing_verification"] = {"verification_id": active_verification["id"], "status": active_verification["status"]}
    payload, result = job.get("payload") or {}, job.get("result")
    if (job.get("kind") != "implementation" or job.get("status") != "uncertain" or payload.get("handoff_id") != handoff_id
            or not isinstance(result, dict) or result.get("phase") != "blocked" or result.get("handoff_id") != handoff_id):
        reasons.append("job_not_eligible")
        return report, None
    if not job_dir.is_dir():
        reasons.append("job_not_eligible")
        return report, None
    try:
        root, directory, manifest, state, state_bytes = recovery.bind_handoff(handoff_path)
    except (impl.ImplementationError, recovery.ObservationError, OSError, ValueError, KeyError, TypeError):
        reasons.append("handoff_integrity")
        return report, None
    with root, contextlib.ExitStack() as descriptors:
        return _audit_bound(root, descriptors, directory, manifest, state, state_bytes, report, reasons, job, handoff_id, review_path,
                            open_issues, job_dir, active_verification, expect_verification, locks, inspector, on_locked, earlier_pids or (),
                            earlier_markers or ())


def _audit_bound(root, descriptors, directory, manifest, state, state_bytes, report, reasons, job, handoff_id, review_path, open_issues,
                 job_dir, active_verification, expect_verification, locks, inspector, on_locked, earlier_pids, earlier_markers):
    payload, result = job.get("payload") or {}, job.get("result")
    registered = Path(str(payload.get("handoff_path", ""))).resolve()
    if registered.is_file():
        registered = registered.parent
    if registered != directory or manifest["handoff_id"] != handoff_id:
        reasons.append("job_not_eligible")
        return report, None
    report["identity"].update(spec_revision=manifest["revision"], spec_sha256=manifest["spec_sha256"],
                              baseline_commit=manifest["baseline_commit"], session_id=manifest["session_id"])
    binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
    if expect_verification is None:
        if state.get("phase") == "verifying" or binding:
            reasons.append("verification_already_exists" if active_verification and binding
                           and binding.get("verification_id") == active_verification["id"] else "projection_out_of_sync")
            return report, None
        if active_verification:
            reasons.append("projection_out_of_sync" if state.get("phase") == "blocked" else "verification_already_exists")
            return report, None
        if state.get("phase") != "blocked" or state.get("replay_allowed") is not False or isinstance(state.get("recovery"), dict):
            reasons.append("job_not_eligible")
            return report, None
    else:
        if (state.get("phase") != "blocked" or not binding or binding.get("verification_id") != expect_verification
                or binding.get("predecessor_job_id") != job.get("id")):
            reasons.append("verification_binding_mismatch")
            return report, None
    attempt_count, attempt_path = state.get("attempt_count"), state.get("attempt_path")
    attempt = directory / "attempts" / ("%04d" % attempt_count if type(attempt_count) is int else "invalid")
    if (type(attempt_count) is not int or attempt_count < 1 or attempt_path != str(attempt)
            or result.get("attempt_count") != attempt_count or result.get("attempt_path") != attempt_path
            or result.get("error") != state.get("error") or not isinstance(state.get("started_at"), str)
            or not isinstance(state.get("finished_at"), str) or not isinstance(state.get("model_finished_at"), str)):
        reasons.append("attempt_identity_mismatch")
        return report, None
    frozen = result.get("candidate") if isinstance(result.get("candidate"), dict) else None
    saved_candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else None
    if (not frozen or not saved_candidate or frozen.get("sha256") != saved_candidate.get("sha256") or frozen.get("head") != saved_candidate.get("head")
            or result.get("model_stdout_sha256") != state.get("model_stdout_sha256") or result.get("model_return_code") != state.get("model_return_code")
            or room.canonical(result.get("identity_evidence")) != room.canonical(state.get("identity_evidence"))
            or room.canonical(result.get("report")) != room.canonical(state.get("report"))):
        reasons.append("attempt_identity_mismatch")  # the frozen job result and the mutable projection must describe the same generation
        return report, None
    verifications = directory / "verifications"
    if ((directory / "attempts").is_symlink() or attempt.is_symlink() or not attempt.is_dir() or verifications.is_symlink()
            or (verifications.exists() and verifications.resolve() != verifications)):
        reasons.append("evidence_unsafe")
        return report, None
    hashes, raw, evidence_reasons = recovery._evidence(attempt, root)
    reasons.extend(evidence_reasons)
    if "parsed-result.json" not in raw and "evidence_missing" not in reasons:
        reasons.append("evidence_missing")
    if reasons:
        return report, None
    receipt = recovery._receipt(raw["process-result.json"])
    if receipt is None:
        reasons.append("receipt_invalid")
        return report, None
    try:
        config = json.loads(recovery.read_owned(directory / "implementation-config.json", recovery.EVIDENCE_LIMIT, "handoff", root=root))
        if not isinstance(config, dict) or any(key not in config for key in ("claude_bin", "model", "timeout_seconds", "gate_timeout_seconds", "claude_config_dir", "expected_model_ids")):
            raise ValueError("pinned configuration is incomplete")
    except (recovery.ObservationError, ValueError):
        reasons.append("handoff_integrity")
        return report, None
    try:
        argv = json.loads(raw["argv.json"])
    except ValueError:
        argv = None
    session_flag = "--session-id" if attempt_count == 1 else "--resume"
    extra = config.get("extra_args")
    expected_argv = ([config["claude_bin"], *extra, "--print", "--output-format", "json", "--model", config["model"], session_flag,
                      manifest["session_id"], "--json-schema", room.canonical(impl.REPORT_SCHEMA)]
                     if isinstance(extra, list) and all(isinstance(value, str) for value in extra) else None)
    # The complete pinned invocation is reconstructed from the pinned configuration and manifest; any differing element refuses.
    if (not isinstance(argv, list) or not all(isinstance(value, str) for value in argv) or expected_argv is None or argv != expected_argv):
        reasons.append("argv_mismatch")
        return report, None
    # The model returned normally: exit 0 receipt, successful terminal result, validated report, verified primary identity.
    try:
        output = json.loads(raw["stdout.json"])
        parsed = json.loads(raw["parsed-result.json"])
    except ValueError:
        output, parsed = None, None
    if (receipt["return_code"] != 0 or state.get("model_return_code") != 0 or state.get("model_stdout_sha256") != hashes["stdout.json"]
            or not isinstance(output, dict) or parsed is None or room.canonical(parsed) != room.canonical(output)
            or output.get("type") != "result" or output.get("subtype") != "success" or output.get("is_error") is not False
            or output.get("terminal_reason") not in (None, "completed") or output.get("session_id") != manifest["session_id"]
            or output.get("permission_denials")):
        reasons.append("model_result_mismatch")
        return report, None
    saved_report = state.get("report")
    if room.canonical(output.get("structured_output")) != room.canonical(saved_report):
        reasons.append("report_mismatch")
        return report, None
    try:
        impl._validate_report(saved_report, manifest)
    except impl.ImplementationError:
        reasons.append("report_invalid")
        return report, None
    if saved_report["outcome"] == "scope_change":
        reasons.append("scope_change_outcome")
        return report, None
    identity = state.get("identity_evidence")
    if (not isinstance(identity, dict) or identity.get("session_id") != manifest["session_id"]
            or identity.get("primary_model") != config["model"] or state.get("primary_model") != config["model"]
            or identity.get("structured_output_sha256") != room.sha(room.canonical(saved_report).encode("utf-8"))
            or not isinstance(identity.get("transcript_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", identity["transcript_sha256"])):
        reasons.append("identity_mismatch")
        return report, None
    report["generation"] = {"attempt_count": attempt_count, "model_return_code": 0, "model_finished_at": state["model_finished_at"],
                            "report_outcome": saved_report["outcome"], "implementation_complete": saved_report["implementation_complete"],
                            "remaining_gap_count": len(saved_report["remaining_gaps"]), "primary_model": identity["primary_model"]}
    # The identified gate: len(gate_results)+1, the gate-k directory with a receipt and no gate-(k+1), and the error argv all agree.
    gates = manifest.get("gates")
    results = state.get("gate_results")
    if (not isinstance(gates, list) or not gates or not isinstance(results, list)
            or any(not isinstance(gate, list) or not all(isinstance(arg, str) for arg in gate) for gate in gates)):
        reasons.append("handoff_integrity")
        return report, None
    index = len(results) + 1
    if index > len(gates) or _gate_names(attempt) != {"gate-%d" % number for number in range(1, index + 1)} or not (attempt / ("gate-%d" % index)).is_dir():
        reasons.append("gate_index_ambiguous")
        return report, None
    gate_hashes = {}
    for done, saved in enumerate(results, 1):
        earlier, _, earlier_reasons = _gate_evidence(attempt, done, root)
        if (earlier_reasons or not isinstance(saved, dict) or saved.get("argv") != gates[done - 1] or type(saved.get("return_code")) is not int
                or saved.get("stdout_sha256") != earlier.get("stdout.txt") or saved.get("stderr_sha256") != earlier.get("stderr.txt")):
            reasons.append("gate_evidence_missing" if earlier_reasons else "evidence_changed")
            return report, None
        gate_hashes["gate-%d" % done] = earlier
    failed_hashes, gate_receipt_raw, gate_reasons = _gate_evidence(attempt, index, root)
    if gate_reasons:
        reasons.extend(gate_reasons)
        return report, None
    gate_receipt = recovery._receipt(gate_receipt_raw)
    if gate_receipt is None:
        reasons.append("gate_receipt_invalid")
        return report, None
    gate_hashes["gate-%d" % index] = failed_hashes
    error = state.get("error")
    seconds, timeout_reasons = recovery.parse_timeout_error(error, gates[index - 1], config["gate_timeout_seconds"])
    if seconds is None:
        reasons.extend(timeout_reasons if timeout_reasons and timeout_reasons != ["error_prefix_mismatch"] else ["gate_error_mismatch"])
        return report, None
    observed_now = room.parse_timestamp(room.now())
    try:
        started, model_finished, state_finished = (room.parse_timestamp(state[key]) for key in ("started_at", "model_finished_at", "finished_at"))
        receipt_finished, gate_finished = room.parse_timestamp(receipt["finished_at"]), room.parse_timestamp(gate_receipt["finished_at"])
    except room.RoomError:
        reasons.append("receipt_time_order")
        return report, None
    if not started <= receipt_finished <= model_finished <= gate_finished <= state_finished <= observed_now:
        reasons.append("receipt_time_order")
        return report, None
    worktree = Path(manifest["worktree_path"])
    if _names_worktree(gates, worktree):
        reasons.append("gate_names_worktree")
        return report, None
    gates_sha256 = room.sha(room.canonical(gates).encode("utf-8"))
    report["gates_sha256"] = gates_sha256
    report["failed_gate"] = {"index": index, "count": len(gates), "argv_sha256": room.sha(room.canonical(gates[index - 1]).encode("utf-8")),
                             "receipt": {"return_code": gate_receipt["return_code"], "finished_at": gate_receipt["finished_at"]},
                             "timeout_seconds": seconds, "pinned_gate_timeout_seconds": config["gate_timeout_seconds"]}
    report["budget"].update(original_seconds=config["gate_timeout_seconds"], minimum_seconds=config["gate_timeout_seconds"])
    current = room.status_report(review_path)
    if (not current["agreement"] or current["current_revision"] != manifest["revision"] or current["spec_sha256"] != manifest["spec_sha256"]):
        reasons.append("spec_mismatch")
    if open_issues:
        reasons.append("open_findings")
    try:
        candidate = candidate_snapshot(worktree)  # bounded owned reads; oversized, growing or unsupported entries refuse here
    except recovery.ObservationError as exc:
        reasons.append(exc.reason)
        return report, None
    if candidate["head"] != manifest["baseline_commit"]:
        reasons.append("candidate_head_moved")
    if candidate != state.get("candidate"):
        reasons.append("candidate_changed")
    report["candidate"] = {"sha256": candidate["sha256"], "head": candidate["head"], "path_count": len(candidate["entries"]),
                           "changed_vs_initial": recovery._changed_paths(state.get("initial_candidate"), candidate)}
    scope = _scope_problem(candidate, worktree)
    if scope:
        reasons.append(scope)
    try:
        transcript_path = recovery.locate_transcript(config, manifest, worktree)
        transcript_home = descriptors.enter_context(recovery.OwnedRoot(recovery.transcript_root(config, transcript_path), kind="transcript"))
        raw_transcript = recovery.read_owned(Path(transcript_path), recovery.TRANSCRIPT_LIMIT, "transcript", root=transcript_home)
        transcript_sha, transcript_length = room.sha(raw_transcript), len(raw_transcript)  # one read: hashed and parsed from the same bytes
    except session_paths.SessionPathError as exc:
        reasons.append(exc.code)
        return report, None
    except recovery.ObservationError as exc:
        reasons.append(exc.reason)
        return report, None
    except OSError:
        reasons.append("transcript_missing_or_ambiguous")
        return report, None
    report["transcript"] = {"sha256": transcript_sha, "length": transcript_length, "matches_identity": transcript_sha == identity["transcript_sha256"]}
    if transcript_sha != identity["transcript_sha256"]:
        reasons.append("transcript_changed")  # strict equality with the saved identity hash; no append tolerance
    else:
        try:
            derived = _primary_from_bytes(raw_transcript, manifest["session_id"], state["started_at"], state["model_finished_at"],
                                          saved_report, config["expected_model_ids"])
            if any(derived[key] != identity.get(key) for key in derived):
                reasons.append("identity_mismatch")
        except recovery.ObservationError as exc:
            reasons.append(exc.reason)
        except (room.RoomError, OSError):
            reasons.append("identity_mismatch")
    prefix_problem = recovery.prefix_violation(transcript_path, state, transcript_home)
    if prefix_problem:
        reasons.append(prefix_problem)
    report["evidence_sha256"] = {"model": dict(hashes), "gates": gate_hashes}
    report["evidence_digest"] = _evidence_digest(attempt_count, hashes, gate_hashes, receipt, gate_receipt, error, gates_sha256,
                                                saved_report, candidate, {"sha256": transcript_sha, "length": transcript_length})
    if expect_verification is not None:
        if candidate != binding.get("candidate"):
            reasons.append("candidate_changed")
        if report["evidence_sha256"] != binding.get("evidence_sha256") or report["evidence_digest"] != binding.get("evidence_digest"):
            reasons.append("evidence_changed")
        if {"sha256": transcript_sha, "length": transcript_length} != binding.get("transcript") or gates_sha256 != binding.get("gates_sha256"):
            reasons.append("transcript_changed" if gates_sha256 == binding.get("gates_sha256") else "evidence_changed")
        record = directory / "verifications" / str(expect_verification) / "record.json"
        try:
            if room.sha(recovery.read_owned(record, recovery.EVIDENCE_LIMIT, "record", root=root)) != binding.get("record_sha256"):
                reasons.append("evidence_changed")
        except recovery.ObservationError:
            reasons.append("evidence_changed")
    if reasons:
        return report, None
    private = {"root": root, "directory": directory, "manifest": manifest, "state": state, "attempt": attempt, "attempt_count": attempt_count,
               "receipt": receipt, "gate_receipt": gate_receipt, "hashes": report["evidence_sha256"], "candidate": candidate, "index": index,
               "seconds": seconds, "gates": gates, "gates_sha256": gates_sha256, "transcript_path": Path(transcript_path),
               "transcript_root": transcript_home, "transcript": {"sha256": transcript_sha, "length": transcript_length},
               "config": config, "argv": argv, "report": saved_report, "identity": identity, "error": error}
    known_pids = {gate_receipt["pid"]} | {pid for pid in earlier_pids if type(pid) is int}
    try:
        with recovery.owner_locks(directory, job_dir, set(locks)):
            observation = observe_gate(gate_receipt["finished_at"], known_pids, manifest["session_id"], worktree,
                                       directory / "verifications", exempt={os.getpid(): "audit_process"}, inspector=inspector,
                                       markers=earlier_markers)
            try:
                if candidate_snapshot(worktree) != candidate:
                    observation["reasons"].append("candidate_changed")
            except recovery.ObservationError as exc:
                observation["reasons"].append(exc.reason)
            try:
                if recovery._stream_hash(Path(transcript_path), recovery.TRANSCRIPT_LIMIT, root=transcript_home) != (transcript_sha, transcript_length):
                    observation["reasons"].append("transcript_changed")
            except recovery.ObservationError as exc:
                observation["reasons"].append(exc.reason)
            except OSError:
                observation["reasons"].append("transcript_unreadable")
            try:
                again, raw_again, again_reasons = recovery._evidence(attempt, root)
                gate_again, _, gate_again_reasons = _gate_evidence(attempt, index, root)
                if (again_reasons or gate_again_reasons or again != hashes or raw_again != raw or gate_again != failed_hashes
                        or _gate_names(attempt) != {"gate-%d" % number for number in range(1, index + 1)}):
                    observation["reasons"].append("evidence_changed")
                if recovery.read_owned(directory / "state.json", recovery.EVIDENCE_LIMIT, "handoff", root=root) != state_bytes:
                    observation["reasons"].append("evidence_changed")
            except recovery.ObservationError:
                observation["reasons"].append("evidence_changed")
            report["stopped_work"] = {key: observation.get(key) for key in ("label", "observed_at", "platform", "boot_time", "boot_source", "boot_after_receipt",
                                                                            "process_method", "marker_method", "matched_processes", "exempt_processes",
                                                                            "gate_processes", "skipped_count", "incomplete_count", "disappeared_count", "detail")}
            reasons.extend(observation["reasons"])
            if reasons:
                return report, None
            report["eligible"] = True
            private["observation"] = observation
            if on_locked is not None:
                return report, on_locked(report, private)
            return report, private
    except recovery.LockUnavailable:
        reasons.append("cooperating_owner_active")
        return report, None


def marker_processes(markers, exempt=()):
    """Same-user processes whose environment or arguments carry PROJECT_ROOM_VERIFICATION_ID=<marker>: {method, matched
    [{pid, marker}], incomplete, skipped}. Linux reads /proc/<pid>/environ with a bounded read (a denied or oversized read
    counts as incomplete); macOS parses `/bin/ps -E`, which appends the environment of the caller's own processes to the
    command column and cannot distinguish an unreadable environment from an empty one (a documented platform limit).
    Only pids and the matched marker are returned; no environment value is retained or reported."""
    tokens = {MARKER_ENV + "=" + marker: marker for marker in markers if isinstance(marker, str) and marker}
    result = {"method": None, "matched": [], "incomplete": 0, "skipped": 0}
    if not tokens:
        return result
    system, uid, own = platform_module.system(), os.getuid(), os.getpid()
    if system == "Linux":
        proc = Path("/proc")
        if not proc.is_dir():
            raise recovery.ObservationError("inspection_unavailable", "no /proc")
        result["method"] = "/proc environ"
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == own or int(entry.name) in exempt:
                continue
            try:
                before = entry.stat()
                if before.st_uid != uid:
                    result["skipped"] += 1
                    continue
                fd = os.open(str(entry / "environ"), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    if os.fstat(fd).st_uid != uid or entry.stat().st_ino != before.st_ino:
                        result["incomplete"] += 1  # the pid was reused or re-owned between the two observations
                        continue
                    with os.fdopen(fd, "rb") as handle:
                        fd = None
                        data = handle.read(MARKER_SCAN_LIMIT + 1)
                finally:
                    if fd is not None:
                        os.close(fd)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ESRCH) or not entry.exists():
                    continue
                result["incomplete"] += 1
                continue
            if len(data) > MARKER_SCAN_LIMIT:
                result["incomplete"] += 1
                continue
            values = set(data.split(b"\0"))
            hits = [marker for token, marker in tokens.items() if token.encode("utf-8") in values]
            if hits:
                result["matched"].append({"pid": int(entry.name), "marker": hits[0]})
        return result
    if system == "Darwin":
        try:
            code, raw_listing = _bounded_capture(["/bin/ps", "-E", "-Aww", "-o", "pid=,uid=,command="], PROCESS_LISTING_LIMIT, 20)
        except recovery.ObservationError as exc:
            raise recovery.ObservationError("inspection_unavailable", "ps -E " + ("over limit" if exc.reason == "listing_oversized" else str(exc.detail))) from exc
        listing = raw_listing.decode("utf-8", "replace")
        if code != 0 or not listing.strip():
            raise recovery.ObservationError("inspection_unavailable", "ps -E failed code:%d" % code)
        result["method"] = "/bin/ps -E (own processes only; unreadable environments are indistinguishable from empty ones)"
        for line in listing.splitlines():
            parts = line.split(None, 2)
            try:
                pid, owner = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                result["skipped"] += 1
                continue
            if owner != uid:
                result["skipped"] += 1
                continue
            if pid == own or pid in exempt:
                continue
            words = set(parts[2].split()) if len(parts) > 2 else set()
            hits = [marker for token, marker in tokens.items() if token in words]
            if hits:
                result["matched"].append({"pid": pid, "marker": hits[0]})
        return result
    raise recovery.ObservationError("inspection_unavailable", "unsupported platform " + system)


def observe_gate(receipt_finished_at, known_pids, session_id, worktree, verification_root, exempt=None, inspector=None, markers=()):
    """The legacy observation without the model lane's restart requirement: boot facts are recorded informationally; a live
    same-user pid or process group equal to any saved gate pid refuses (gate_process_present), as does any process whose
    cwd or arguments name the private verification directory or whose environment carries an earlier verification marker.
    What each fact proves is stated in the documentation."""
    facts, failure = None, None
    try:
        facts = (inspector or recovery.default_inspector)()  # one bounded observation, shared with the legacy writer scan
    except recovery.ObservationError as exc:
        failure = exc
    def once():
        if failure is not None:
            raise failure
        return facts
    observation = recovery.observe(receipt_finished_at, None, session_id, worktree, exempt=exempt, inspector=once)
    observation["label"] = "isolated_copy_legacy_receipts"
    boot_invalid = "boot_evidence_invalid" in observation["reasons"]
    observation["boot_after_receipt"] = None if boot_invalid else "restart_required" not in observation["reasons"]
    # Boot facts are informational here: this lane runs in a fresh copy and never claims the boot boundary.
    observation["reasons"] = [reason for reason in observation["reasons"] if reason not in ("restart_required", "boot_evidence_invalid")]
    observation["gate_processes"] = []
    if facts is None:
        return observation
    names = {str(verification_root), str(Path(verification_root).resolve())}
    for process in facts.get("processes", []):
        pid, pgid = process.get("pid"), process.get("pgid")
        if pid == os.getpid() or pid in (exempt or {}):
            continue
        matches = []
        if pid in known_pids or pgid in known_pids:
            matches.append("gate_pid_or_group")
        args, cwd = process.get("args") or "", process.get("cwd") or ""
        if any(name in args for name in names):
            matches.append("argv_verification_dir")
        if isinstance(cwd, str) and any(cwd == name or cwd.startswith(name + os.sep) for name in names):
            matches.append("cwd_verification_dir")
        if matches:
            observation["gate_processes"].append({"pid": pid, "match": matches})
    try:
        marked = marker_processes(markers, exempt=set(exempt or ()))
    except recovery.ObservationError as exc:
        observation["reasons"].append(exc.reason)
        observation["detail"] = exc.detail
        return observation
    observation["marker_method"] = marked["method"]
    if marked["incomplete"]:
        observation["reasons"].append("inspection_incomplete")
        observation["incomplete_count"] += marked["incomplete"]
    for item in marked["matched"]:
        known = next((entry for entry in observation["gate_processes"] if entry["pid"] == item["pid"]), None)
        if known is not None:
            known["match"].append("marker_env")
        else:
            observation["gate_processes"].append({"pid": item["pid"], "match": ["marker_env"]})
    if observation["gate_processes"]:
        observation["reasons"].append("gate_process_present")
    return observation


def prepare(report, private, verification_id, room_id, request_id, job, supplied, budget, diagnosis, authorization, registry_home, successor_job_id):
    """Durable one-use record, dispatch note and projection binding, run while the owner locks are held by audit()."""
    directory, manifest, state = private["directory"], private["manifest"], private["state"]
    expected = {"spec_revision": manifest["revision"], "spec_sha256": manifest["spec_sha256"], "candidate_sha256": private["candidate"]["sha256"],
                "evidence_digest": report["evidence_digest"], "gates_sha256": private["gates_sha256"]}
    for key, value in expected.items():
        if supplied.get(key) != value:
            raise room.RoomError("Verification request does not match the fresh audit: " + key)
    budget = validate_budget(budget, private["config"]["gate_timeout_seconds"])
    root = private["root"]
    recovery._require_bound_path(root, directory)
    try:
        home_root_fd = recovery.directory_below(root, ("verifications",), "record", create=True)
    except recovery.ObservationError as exc:
        raise room.RoomError("Verification is not eligible: evidence_unsafe") from exc
    snapshot = "transcript-snapshot.jsonl"
    try:
        try:
            home_fd = recovery.directory_below(home_root_fd, (verification_id,), "record", create=True, exclusive=True)
        except recovery.ObservationError as exc:
            raise room.RoomError("Verification is not eligible: evidence_unsafe") from exc
        os.fsync(home_root_fd)
        try:
            record_bytes, created = _write_record(private, home_fd, snapshot, verification_id, room_id, request_id, job, supplied, budget,
                                                  diagnosis, authorization, registry_home, report, successor_job_id)
        except BaseException:
            for name in (snapshot, "record.json", "dispatch.json"):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=home_fd)
            os.close(home_fd)
            home_fd = None
            with contextlib.suppress(OSError):
                os.rmdir(verification_id, dir_fd=home_root_fd)
                os.fsync(home_root_fd)
            raise
        os.close(home_fd)
    finally:
        os.close(home_root_fd)
    record_sha = room.sha(record_bytes)
    state["verification"] = {"verification_id": verification_id, "predecessor_job_id": job["id"], "predecessor_attempt": private["attempt_count"],
                             "successor_job_id": successor_job_id, "gate_index": private["index"], "boundary": BOUNDARY,
                             "budget_seconds": budget, "original_gate_timeout_seconds": private["config"]["gate_timeout_seconds"],
                             "candidate": private["candidate"], "evidence_sha256": private["hashes"], "evidence_digest": report["evidence_digest"],
                             "gates_sha256": private["gates_sha256"], "transcript": private["transcript"], "record_sha256": record_sha,
                             "dispatched_at": created, "launch_state": "pending"}
    state.setdefault("verification_history", []).append({"verification_id": verification_id, "status": "dispatched", "at": created,
                                                         "predecessor_attempt": private["attempt_count"], "successor_job_id": successor_job_id,
                                                         "budget_seconds": budget, "transcript": private["transcript"]})
    recovery.write_json_below(root.fd, "state.json", state, "handoff")
    return {"record_sha256": record_sha, "created_at": created, "budget_seconds": budget}


def _write_record(private, home_fd, snapshot, verification_id, room_id, request_id, job, supplied, budget, diagnosis, authorization,
                  registry_home, report, successor_job_id):
    manifest, state = private["manifest"], private["state"]
    temporary = snapshot + "." + uuid.uuid4().hex + ".tmp"
    fd = recovery._create_component(temporary, recovery.CREATE_FLAGS, 0o600, home_fd)
    try:
        try:
            sink = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with sink:
            copied = recovery._stream_hash(private["transcript_path"], recovery.TRANSCRIPT_LIMIT, root=private["transcript_root"], sink=sink)
            sink.flush()
            os.fsync(sink.fileno())
        if copied != (private["transcript"]["sha256"], private["transcript"]["length"]):
            raise recovery.ObservationError("transcript_changed")
        os.replace(temporary, snapshot, src_dir_fd=home_fd, dst_dir_fd=home_fd)
        os.fsync(home_fd)
    except recovery.ObservationError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=home_fd)
        raise room.RoomError("Verification is not eligible: " + exc.reason) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=home_fd)
        raise
    created = room.now()
    record = {"verification_id": verification_id, "room_id": room_id, "handoff_id": manifest["handoff_id"], "request_id": request_id,
              "predecessor_job_id": job["id"], "successor_job_id": successor_job_id, "attempt": private["attempt_count"], "created_at": created,
              "boundary": BOUNDARY, "evidence_label": "isolated_copy_legacy_receipts", "original_job": job, "failed_state_snapshot": state,
              "failed_gate": {"index": private["index"], "count": len(private["gates"]), "timeout_seconds": private["seconds"],
                              "receipt": private["gate_receipt"], "error": private["error"]},
              "model_receipt": private["receipt"], "evidence_sha256": private["hashes"], "evidence_digest": report["evidence_digest"],
              "gates_sha256": private["gates_sha256"], "gates": private["gates"], "supplied": supplied,
              "budget_seconds": budget, "original_gate_timeout_seconds": private["config"]["gate_timeout_seconds"],
              "identity": {**report["identity"], "manifest_sha256": state["manifest_sha256"],
                           "original_authorization_sha256": manifest["pinned_files"]["authorization.txt"], "primary": private["identity"]},
              "candidate": private["candidate"], "transcript": {**private["transcript"], "snapshot": snapshot},
              "observation": private["observation"], "diagnosis": diagnosis, "authorization": authorization,
              "authorization_scope": "rerun of exactly the pinned offline gates in an isolated copy; not arbitrary commands, no model call",
              "supported_scope": dict(SUPPORTED_SCOPE), "limitations": list(LIMITATIONS),
              "argv_sha256": room.sha(room.canonical(private["argv"]).encode()), "registry_home": str(registry_home)}
    record_bytes = recovery.write_json_below(home_fd, "record.json", record, "record")
    recovery.write_json_below(home_fd, "dispatch.json", {"verification_id": verification_id, "successor_job_id": successor_job_id, "dispatched_at": created}, "record")
    return record_bytes, created


def _registered_verifier(record, record_sha, manifest, verification_id, successor_job_id, invoking_registry):
    """True only when the registry named by the durable record and the invoking service has this exact successor job
    dispatched for this verification, the registered worker is this invocation's parent, and its lease is held."""
    import fcntl
    import sqlite3
    registry = record.get("registry_home") if isinstance(record, dict) else None
    if (not isinstance(registry, str) or not re.fullmatch(r"[0-9a-f]{32}", str(successor_job_id))
            or not isinstance(invoking_registry, str) or invoking_registry != registry):
        return False
    home = Path(registry)
    database = home / "registry.sqlite3"
    if not home.is_dir() or not database.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            job = db.execute("SELECT room_id,kind,status,payload,pid FROM jobs WHERE id=?", (successor_job_id,)).fetchone()
            row = db.execute("SELECT room_id,handoff_id,status,successor_job_id,record_sha256 FROM implementation_verifications WHERE id=?",
                             (verification_id,)).fetchone()
        payload = json.loads(job["payload"]) if job else None
    except (sqlite3.Error, ValueError, TypeError):
        return False
    if (not job or not row or job["kind"] != "verification" or job["status"] != "running" or not isinstance(payload, dict)
            or type(job["pid"]) is not int or job["pid"] != os.getppid()
            or payload.get("verification_id") != verification_id or payload.get("handoff_id") != manifest["handoff_id"]
            or row["status"] != "dispatched" or row["successor_job_id"] != successor_job_id or row["room_id"] != job["room_id"]
            or row["handoff_id"] != manifest["handoff_id"] or row["record_sha256"] != record_sha):
        return False
    lease = home / "jobs" / successor_job_id / "worker.lock"
    try:
        with lease.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _refused(reason, manifest, successor, detail=None):
    return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": reason, "detail": detail,
            "verification_id": successor.get("verification_id") if isinstance(successor, dict) else None,
            "handoff_id": manifest["handoff_id"] if isinstance(manifest, dict) else None, "gates_launched": False, "model_launched": False}


def run(handoff_path, successor):
    """Execute the registered verifier: {"verification_id", "successor_job_id", "registry", "recheck": zero-argument callable
    returning the audit report, "inspector": optional process inspector}. Runs no model."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        try:
            root, directory, manifest, state, _ = recovery.bind_handoff(directory)
        except Exception as exc:
            return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "prelaunch_error", "detail": type(exc).__name__,
                    "verification_id": successor.get("verification_id") if isinstance(successor, dict) else None, "handoff_id": None,
                    "gates_launched": False, "model_launched": False}
        with root:
            try:
                refusal, record, record_sha = _prelaunch(root, directory, manifest, state, successor)
            except Exception as exc:  # nothing has been written or spawned: a proven non-launch
                return _refused("prelaunch_error", manifest, successor, type(exc).__name__)
            if refusal:
                return _refused(refusal, manifest, successor)
            return _execute(root, directory, manifest, state, record, record_sha, successor)


def _prelaunch(root, directory, manifest, state, successor):
    binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
    if (not isinstance(successor, dict) or not binding or binding.get("verification_id") != successor.get("verification_id")
            or binding.get("successor_job_id") != successor.get("successor_job_id") or state.get("phase") != "blocked"):
        return "verification_binding_mismatch", None, None
    home = directory / "verifications" / str(successor["verification_id"])
    try:
        dispatch = json.loads(recovery.read_owned(home / "dispatch.json", recovery.EVIDENCE_LIMIT, "record", root=root))
        record_bytes = recovery.read_owned(home / "record.json", recovery.EVIDENCE_LIMIT, "record", root=root)
        record = json.loads(record_bytes)
    except (recovery.ObservationError, OSError, ValueError):
        return "verification_binding_mismatch", None, None
    if (not isinstance(dispatch, dict) or dispatch.get("verification_id") != successor["verification_id"]
            or dispatch.get("successor_job_id") != successor.get("successor_job_id") or not isinstance(record, dict)):
        return "verification_binding_mismatch", None, None
    record_sha = room.sha(record_bytes)
    if record_sha != binding.get("record_sha256") or record.get("successor_job_id") != successor["successor_job_id"]:
        return "evidence_changed", None, None
    for key in ("verification_id", "predecessor_job_id", "successor_job_id", "candidate", "evidence_sha256", "evidence_digest", "gates_sha256",
                "budget_seconds", "original_gate_timeout_seconds", "transcript"):
        expected = record.get(key) if key != "transcript" else {k: v for k, v in (record.get("transcript") or {}).items() if k != "snapshot"}
        if room.canonical(binding.get(key)) != room.canonical(expected) or (key == "predecessor_job_id" and record.get("attempt") != binding.get("predecessor_attempt")):
            return "evidence_changed", None, None  # the mutable projection must equal the immutable, registry-authenticated record
    try:
        validate_budget(record.get("budget_seconds"), record.get("original_gate_timeout_seconds"))
    except room.RoomError:
        return "evidence_changed", None, None
    if not _registered_verifier(record, record_sha, manifest, successor["verification_id"], successor["successor_job_id"], successor.get("registry")):
        return "verification_binding_mismatch", None, None
    recheck = successor.get("recheck")
    if not callable(recheck):
        return "verification_binding_mismatch", None, None
    report = recheck()
    if not isinstance(report, dict) or not report.get("eligible"):
        reasons = report.get("reasons") if isinstance(report, dict) else None
        return (",".join(reasons) if reasons else "verification_binding_mismatch"), None, None
    try:
        impl._require_current_agreement(manifest)
    except impl.ImplementationError:
        return "spec_mismatch", None, None
    try:
        if candidate_snapshot(manifest["worktree_path"]) != binding["candidate"]:
            return "candidate_changed", None, None
    except recovery.ObservationError as exc:
        return exc.reason, None, None
    return None, record, record_sha


def _gate_env(tmpdir, verification_id):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmpdir))
    env[MARKER_ENV] = verification_id
    for key in ("TMP", "TEMP"):
        env.pop(key, None)
    return env


def _execute(root, directory, manifest, state, record, record_sha, successor):
    verification_id, successor_job_id = successor["verification_id"], successor["successor_job_id"]
    binding = state["verification"]
    candidate, gates, budget = record["candidate"], record["gates"], record["budget_seconds"]  # authenticated by the registry digest
    worktree = Path(manifest["worktree_path"])
    home = directory / "verifications" / verification_id
    started = room.now()
    binding.update(launch_state="running", started_at=started)
    state.update(phase="verifying", owner_job_id=successor_job_id, started_verification_at=started,
                 active_stage={"kind": "gate", "index": 1, "started_at": started, "budget_seconds": budget, "verification_id": verification_id})
    recovery.write_json_below(root.fd, "state.json", state, "handoff")
    gate_results, interruption, detail, copy_report, gate_pids, identities = [], None, None, None, [], {}
    tmpdir = home / "tmp"
    copy_dir = home / "candidate"
    try:
        try:
            home_fd = recovery.directory_below(root, ("verifications", verification_id), "record")
        except recovery.ObservationError as exc:
            raise _Interrupted("copy_mismatch", exc.reason) from exc
        try:
            if _snapshot_or_interrupt(worktree) != candidate:
                raise _Interrupted("candidate_changed", "before_copy")
            with recovery.OwnedRoot(worktree, kind="candidate") as source:
                copy_report = _build_copy(source, worktree, candidate, home_fd)
            tmp_fd = recovery.directory_below(home_fd, ("tmp",), "record", create=True, exclusive=True)
            try:
                tmp_identity = _identity(tmp_fd)
            finally:
                os.close(tmp_fd)
            identities = {"candidate": copy_report["identity"], "tmp": tmp_identity}
        finally:
            os.close(home_fd)
        env = _gate_env(tmpdir, verification_id)
        for index, gate in enumerate(gates, 1):
            gate_dir = home / ("gate-%d" % index)
            gate_dir.mkdir(mode=0o700)
            before = _verify_copy(copy_dir, candidate, worktree, identities["candidate"])
            if before["reason"]:
                raise _Interrupted(before["reason"], "gate-%d before" % index)
            gate_started = room.now()
            state["active_stage"] = {"kind": "gate", "index": index, "started_at": gate_started, "budget_seconds": budget, "verification_id": verification_id}
            recovery.write_json_below(root.fd, "state.json", state, "handoff")
            try:
                code = impl._run_child(gate, copy_dir, gate_dir / "stdout.txt", gate_dir / "stderr.txt", budget, env=env,
                                       spawn_receipt=gate_dir / "process-start.json", on_spawn=lambda: _record_launch(root, state, binding, gate_dir, index))
            finally:
                _record_launch(root, state, binding, gate_dir, index)
                pid = _receipt_pid(gate_dir, root)
                if pid is not None:
                    gate_pids.append(pid)
            after = _verify_copy(copy_dir, candidate, worktree, identities["candidate"])
            if _snapshot_or_interrupt(worktree) != candidate:
                raise _Interrupted("candidate_changed", "gate-%d original" % index)  # the ignore classification above read the original repository
            gate_result = {"argv": gate, "return_code": code, "started_at": gate_started, "finished_at": room.now(),
                           "stdout_path": str(gate_dir / "stdout.txt"), "stderr_path": str(gate_dir / "stderr.txt"),
                           "stdout_sha256": recovery._stream_hash(gate_dir / "stdout.txt", recovery.EVIDENCE_LIMIT, kind="record", root=root)[0],
                           "stderr_sha256": recovery._stream_hash(gate_dir / "stderr.txt", recovery.EVIDENCE_LIMIT, kind="record", root=root)[0],
                           "receipt_sha256": room.sha(recovery.read_owned(gate_dir / "process-result.json", recovery.EVIDENCE_LIMIT, "record", root=root)),
                           "candidate_unchanged": not after["reason"], "copy_verified": {"entries": after["entries"], "created_ignored": after["created_ignored"]},
                           "verification_id": verification_id, "boundary": BOUNDARY, "budget_seconds": budget}
            gate_results.append(gate_result)
            impl._atomic(gate_dir / "result.json", gate_result)
            if after["reason"]:
                raise _Interrupted(after["reason"], "gate-%d after: %s" % (index, ",".join(after["detail"][:5])))
        if _snapshot_or_interrupt(worktree) != candidate:
            raise _Interrupted("candidate_changed", "before_projection")
    except _Interrupted as exc:
        interruption, detail = exc.reason, exc.detail
    except subprocess.TimeoutExpired:
        interruption, detail = "gate_timeout:%d" % len(gate_results + [None]), None
    except room.InvocationTerminated:
        interruption, detail = "cancelled", None
    except Exception as exc:  # concise: the type name only, never raw exception prose or argv
        interruption, detail = "verifier_error", type(exc).__name__
    try:
        cleanup = _cleanup(root, directory, home, verification_id, gate_pids, successor.get("inspector"), identities)
    except Exception as exc:
        cleanup = {"status": "deferred", "reason": "cleanup_error:" + type(exc).__name__, "survivors": [], "gate_pids": gate_pids}
    if interruption is None and cleanup.get("status") != "removed":
        # Observed continuing workers, unprovable inspection or an unfinished removal never become a pass: the agreed refusals hold at completion too.
        interruption = cleanup.get("reason") or "survivors_observed"
        detail = "survivors:%d" % len(cleanup.get("survivors") or [])
    finished = room.now()
    if interruption is None:
        outcome = {"verification_id": verification_id, "successor_job_id": successor_job_id, "room_id": record["room_id"],
                   "handoff_id": manifest["handoff_id"], "attempt": binding["predecessor_attempt"], "predecessor_job_id": binding["predecessor_job_id"],
                   "boundary": BOUNDARY, "result": "completed", "gate_results": gate_results,
                   "gates_passed": all(gate["return_code"] == 0 for gate in gate_results), "candidate_sha256": candidate["sha256"],
                   "candidate_head": candidate["head"], "record_sha256": binding["record_sha256"], "copy": copy_report,
                   "budget_seconds": budget, "started_at": started, "completed_at": finished, "cleanup": cleanup}
        outcome_bytes = recovery.json_bytes(outcome)
        digest = room.sha(outcome_bytes)
        # Durable outcome protocol: the digest is registered under the running successor's ownership before the file exists
        # and before any projection, so a crash after this point reconciles without executing the gates again, while a file
        # the registry never registered is never adopted.
        if not _register_outcome(record, record_sha, manifest, verification_id, successor_job_id, digest):
            interruption, detail, outcome = "outcome_registration_failed", None, None
        else:
            try:
                _write_outcome(root, verification_id, outcome_bytes)
                project_completion(state, binding, outcome, digest)
                recovery.write_json_below(root.fd, "state.json", state, "handoff")
            except Exception as exc:  # the registered digest stays; reconciliation decides, and no raw prose escapes
                return {**impl._summary(directory, manifest, state), "status": "blocked", "error": "verification_interrupted:projection_write_failed:" + type(exc).__name__,
                        "verification": {"verification_id": verification_id, "result": "unsettled", "reason": "projection_write_failed", "outcome_sha256": digest,
                                         "launched_at": binding.get("launched_at"), "cleanup": cleanup}}
            return {**impl._summary(directory, manifest, state), "verification": {"verification_id": verification_id, "result": "completed",
                    "gates_passed": outcome["gates_passed"], "outcome_sha256": digest, "launched_at": binding.get("launched_at"), "cleanup": cleanup}}
    outcome = {"verification_id": verification_id, "successor_job_id": successor_job_id, "room_id": record["room_id"], "handoff_id": manifest["handoff_id"],
               "attempt": binding["predecessor_attempt"], "predecessor_job_id": binding["predecessor_job_id"], "boundary": BOUNDARY,
               "result": "interrupted", "reason": interruption, "detail": detail, "gate_results": gate_results, "budget_seconds": budget,
               "record_sha256": binding["record_sha256"], "started_at": started, "finished_at": finished, "cleanup": cleanup}
    outcome_bytes = recovery.json_bytes(outcome)
    _register_outcome(record, record_sha, manifest, verification_id, successor_job_id, room.sha(outcome_bytes))  # best effort: an interrupted outcome is never a pass
    with contextlib.suppress(recovery.ObservationError, OSError):
        _write_outcome(root, verification_id, outcome_bytes)
    launched = binding.get("launched_at")
    state.update(phase="blocked", needs_attention=True, active_stage=None, owner_job_id=None, verification=None)
    state.setdefault("verification_history", []).append({"verification_id": verification_id, "status": "interrupted", "reason": interruption,
                                                         "at": finished, "predecessor_attempt": binding["predecessor_attempt"],
                                                         "successor_job_id": successor_job_id, "launched_at": launched,
                                                         "outcome_sha256": room.sha(outcome_bytes), "cleanup": cleanup})
    recovery.write_json_below(root.fd, "state.json", state, "handoff")
    return {**impl._summary(directory, manifest, state), "status": "blocked", "error": "verification_interrupted:" + interruption,
            "verification": {"verification_id": verification_id, "result": "interrupted", "reason": interruption, "launched_at": launched,
                             "outcome_sha256": room.sha(outcome_bytes), "cleanup": cleanup}}


class _Interrupted(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _snapshot_or_interrupt(worktree):
    try:
        return candidate_snapshot(worktree)
    except recovery.ObservationError as exc:
        raise _Interrupted(exc.reason, exc.detail) from exc


def _register_outcome(record, record_sha, manifest, verification_id, successor_job_id, digest):
    """Ownership-bound registration of the outcome digest: only the registered running successor (this invocation is the
    registered worker's child, the row is still dispatched for it and that worker's lease is held) may set the row's
    outcome_sha256, exactly once. Returns True only when the registry recorded it. This binds the digest to the owning
    execution; it does not defend against an actor who controls all trusted owner state (registry, lease and worker)."""
    import sqlite3
    if not _registered_verifier(record, record_sha, manifest, verification_id, successor_job_id, record.get("registry_home")):
        return False
    database = Path(record["registry_home"]) / "registry.sqlite3"
    try:
        with contextlib.closing(sqlite3.connect(str(database), timeout=10)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute("UPDATE implementation_verifications SET outcome_sha256=? WHERE id=? AND status='dispatched' "
                                     "AND successor_job_id=? AND outcome_sha256 IS NULL", (digest, verification_id, successor_job_id)).rowcount
        return changed == 1
    except sqlite3.Error:
        return False


def _receipt_pid(gate_dir, root=None):
    for name in ("process-start.json", "process-result.json"):
        try:
            saved = json.loads(recovery.read_owned(gate_dir / name, recovery.EVIDENCE_LIMIT, "record", root=root))  # bounded, owned
        except (recovery.ObservationError, OSError, ValueError):
            continue
        if isinstance(saved, dict) and type(saved.get("pid")) is int:
            return saved["pid"]
    return None


def _record_launch(root, state, binding, gate_dir, index):
    """launched_at comes only from the first gate's spawn receipt (or its exit receipt when the start receipt was lost)."""
    if binding.get("launched_at") or index != 1:
        return
    for name in ("process-start.json", "process-result.json"):
        try:
            saved = json.loads(recovery.read_owned(gate_dir / name, recovery.EVIDENCE_LIMIT, "record", root=root))  # bounded, owned
        except (recovery.ObservationError, OSError, ValueError):
            continue
        if isinstance(saved, dict) and type(saved.get("pid")) is int:
            binding.update(launched_at=saved.get("started_at") if name == "process-start.json" else binding.get("started_at"),
                           launch_state="launched", launch_evidence=name, first_gate_pid=saved["pid"])
            for entry in state.get("verification_history", []):
                if isinstance(entry, dict) and entry.get("verification_id") == binding.get("verification_id") and entry.get("status") == "dispatched":
                    entry.update(status="launched", launched_at=binding["launched_at"])
            recovery.write_json_below(root.fd, "state.json", state, "handoff")
            return


def _write_outcome(root, verification_id, outcome_bytes):
    """Place the immutable outcome bytes (already registered by digest) exactly once, relative to the bound directory."""
    home_fd = recovery.directory_below(root, ("verifications", verification_id), "record")
    try:
        if recovery.exists_below(home_fd, "outcome.json"):
            raise recovery.ObservationError("evidence_unsafe", "outcome exists")
        recovery.write_below(home_fd, "outcome.json", outcome_bytes, "record")
        return outcome_bytes
    finally:
        os.close(home_fd)


def project_completion(state, binding, outcome, outcome_sha):
    """Mutable projection after the immutable outcome: the original error is cleared only here; the failed state stays in the record."""
    state.update(phase="awaiting_astra_review", gate_results=outcome["gate_results"], gates_passed=outcome["gates_passed"],
                 finished_at=outcome["completed_at"], error=None, needs_attention=False, active_stage=None, owner_job_id=None, verification=None)
    state.setdefault("verification_history", []).append({"verification_id": binding["verification_id"], "status": "consumed", "at": outcome["completed_at"],
                                                         "predecessor_attempt": binding["predecessor_attempt"], "successor_job_id": binding["successor_job_id"],
                                                         "launched_at": binding.get("launched_at"), "gates_passed": outcome["gates_passed"],
                                                         "outcome_sha256": outcome_sha, "cleanup": outcome.get("cleanup")})


def _build_copy(source, worktree, candidate, home_fd):
    """Fresh exclusive copy of the audited candidate below the bound verification directory: every entry is read through the
    owned worktree root, its bytes must hash to the audited digest, its mode is applied exactly, and nothing else is copied."""
    copy_fd = recovery.directory_below(home_fd, ("candidate",), "record", create=True, exclusive=True)
    total, count = 0, 0
    identity = _identity(copy_fd)
    try:
        for entry in candidate["entries"]:
            if entry["kind"] == "deleted":
                continue
            if entry["kind"] != "file":
                raise _Interrupted("copy_mismatch", "unsupported entry")
            parts = Path(entry["path"]).parts
            data = recovery.read_owned(Path(worktree) / entry["path"], COPY_FILE_LIMIT, "candidate", root=source)
            if room.sha(data) != entry["sha256"]:
                raise _Interrupted("candidate_changed", "digest")
            total += len(data)
            count += 1
            if total > COPY_TOTAL_LIMIT or count > COPY_ENTRY_LIMIT:
                raise _Interrupted("copy_mismatch", "bounds")
            parent_fd, borrowed = copy_fd, True
            if len(parts) > 1:
                parent_fd, borrowed = recovery.directory_below(copy_fd, parts[:-1], "record", create=True), False
            try:
                recovery.write_below(parent_fd, parts[-1], data, "record", mode=entry["mode"])
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
                try:
                    os.fchmod(fd, entry["mode"])
                finally:
                    os.close(fd)
            finally:
                if not borrowed:
                    os.close(parent_fd)
    finally:
        os.close(copy_fd)
    return {"entries": count, "bytes": total, "candidate_sha256": candidate["sha256"], "head": candidate["head"], "identity": identity}


def _identity(fd):
    metadata = os.fstat(fd)
    return [metadata.st_dev, metadata.st_ino]


def _walk(dir_fd, prefix, found, depth=0, budget=None):
    """Descriptor-relative walk of the copy with bounded reads: every file is read through its own owned descriptor at most
    COPY_FILE_LIMIT + 1 bytes (a file that grows after its stat is caught by the read itself, not only by the stat), and the
    bytes visited across the whole copy, ignored files included, never exceed COPY_TOTAL_LIMIT; entry count and depth are
    bounded as well. Exceeding any bound records the path as oversized and stops reading it."""
    budget = {"remaining": COPY_TOTAL_LIMIT, "exceeded": [], "scanned": 0} if budget is None else budget
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            path = prefix + entry.name if not prefix else prefix + "/" + entry.name
            budget["scanned"] = budget.get("scanned", 0) + 1  # directories count too, so a directory forest is bounded
            if budget["scanned"] > COPY_ENTRY_LIMIT or len(found) >= COPY_ENTRY_LIMIT or depth >= PATH_DEPTH_LIMIT:
                found[path] = ("oversized", None, None)
                budget["exceeded"].append(path)
                return
            if entry.is_symlink():
                found[path] = ("symlink", None, None)
            elif entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, recovery.DIRECTORY_FLAGS, dir_fd=dir_fd)
                except OSError:
                    found[path] = ("unreadable", None, None)
                    continue
                try:
                    _walk(child, path, found, depth + 1, budget)
                finally:
                    os.close(child)
            elif entry.is_file(follow_symlinks=False):
                try:
                    fd = os.open(entry.name, recovery.LEAF_FLAGS, dir_fd=dir_fd)
                except OSError:
                    found[path] = ("unreadable", None, None)
                    continue
                try:
                    metadata = os.fstat(fd)
                    if metadata.st_size > COPY_FILE_LIMIT or metadata.st_size > budget["remaining"]:
                        found[path] = ("oversized", None, None)
                        budget["exceeded"].append(path)
                        continue
                    digest, consumed, oversized = hashlib.sha256(), 0, False
                    with os.fdopen(fd, "rb") as handle:
                        fd = None
                        while True:
                            # Never read past the bounds even if the file grew after its stat: at most one byte beyond
                            # the smaller of the per-file limit and the remaining aggregate budget is requested.
                            allowance = min(1024 * 1024, COPY_FILE_LIMIT - consumed + 1, budget["remaining"] - consumed + 1)
                            chunk = handle.read(max(allowance, 1))
                            if not chunk:
                                break
                            consumed += len(chunk)
                            if consumed > COPY_FILE_LIMIT or consumed > budget["remaining"]:
                                oversized = True
                                break
                            digest.update(chunk)
                    if oversized:
                        found[path] = ("oversized", None, None)
                        budget["exceeded"].append(path)
                        continue
                    budget["remaining"] -= consumed
                    found[path] = ("file", stat.S_IMODE(metadata.st_mode), digest.hexdigest())
                finally:
                    if fd is not None:
                        os.close(fd)
            else:
                found[path] = ("special", None, None)


def _verify_copy(copy_dir, candidate, worktree, identity=None):
    """Copy fidelity: the copy directory is still the one created for this run (device and inode), every audited entry is
    present with the audited digest and mode, nothing is modified, and any created non-ignored path (classified read-only
    through git check-ignore against the original repository) refuses. Empty created directories are not candidate
    content, matching the normal lane's git-based candidate semantics."""
    expected = {entry["path"]: (entry["mode"], entry["sha256"]) for entry in candidate["entries"] if entry["kind"] == "file"}
    found, budget = {}, {"remaining": COPY_TOTAL_LIMIT, "exceeded": [], "scanned": 0}
    try:
        fd = os.open(str(copy_dir), recovery.DIRECTORY_FLAGS)
    except OSError:
        return {"reason": "copy_mismatch", "detail": ["copy missing"], "entries": 0, "created_ignored": 0}
    try:
        if identity is not None and _identity(fd) != list(identity):
            return {"reason": "copy_mismatch", "detail": ["copy directory replaced"], "entries": 0, "created_ignored": 0}
        _walk(fd, "", found, 0, budget)
    finally:
        os.close(fd)
    if budget["exceeded"] or any(spec[0] == "oversized" for spec in found.values()):
        # The verifier's own resource bounds hold for every visited byte, ignored files included; ignore semantics never lift them.
        return {"reason": "copy_bounds_exceeded", "detail": sorted(set(budget["exceeded"]))[:20], "entries": len(expected), "created_ignored": 0}
    modified = sorted(path for path, spec in expected.items() if found.get(path) != ("file",) + spec)
    if modified:
        return {"reason": "gate_changed_candidate", "detail": modified[:20], "entries": len(expected), "created_ignored": 0}
    created = sorted(path for path in found if path not in expected)
    if not created:
        return {"reason": None, "detail": [], "entries": len(expected), "created_ignored": 0}
    if any(found[path][0] != "file" for path in created):
        return {"reason": "gate_created_candidate_path", "detail": [path for path in created if found[path][0] != "file"][:20], "entries": len(expected), "created_ignored": 0}
    ignored = _check_ignore(worktree, created)
    if ignored is None:
        return {"reason": "gate_created_candidate_path", "detail": ["check-ignore unavailable"], "entries": len(expected), "created_ignored": 0}
    unexpected = [path for path in created if path not in ignored]
    if unexpected:
        return {"reason": "gate_created_candidate_path", "detail": unexpected[:20], "entries": len(expected), "created_ignored": len(ignored)}
    return {"reason": None, "detail": [], "entries": len(expected), "created_ignored": len(ignored)}


def _check_ignore(worktree, paths):
    """Set of `paths` the original repository ignores, or None when the read-only classification could not run within the
    lane's capture bounds (input and output are both capped; an over-limit classification refuses, it never buffers)."""
    payload = bytearray()
    for path in paths:
        payload += os.fsencode(path) + b"\0"
        if len(payload) > CAPTURE_INPUT_LIMIT:
            return None  # fail closed before any allocation beyond the cap
    try:
        code, data = _bounded_capture(["git", "-C", str(worktree), "check-ignore", "-z", "--stdin"], GIT_LISTING_LIMIT, input_bytes=bytes(payload))
    except (recovery.ObservationError, OSError):
        return None
    if code not in (0, 1):
        return None
    return {os.fsdecode(item) for item in data.split(b"\0") if item}


def _cleanup(root, directory, home, verification_id, gate_pids, inspector, identities=None):
    """Remove the private copy and TMPDIR only inside the bound verification directory and only when no survivor is observed
    by cwd or arguments inside it, by a live pid/process group from a gate receipt, or by this run's environment marker;
    otherwise defer and record. A deferred cleanup also refuses to project the run as a pass."""
    survivors, reason = [], None
    try:
        marked = marker_processes([verification_id])
        for item in marked["matched"]:
            survivors.append({"pid": item["pid"], "match": ["marker_env"]})
        if marked["incomplete"]:
            reason = "inspection_incomplete"
        facts = (inspector or recovery.default_inspector)()
        names = {str(home), str(Path(home).resolve())}
        for process in facts.get("processes", []):
            if process.get("pid") == os.getpid():
                continue
            args, cwd = process.get("args") or "", process.get("cwd") or ""
            matches = []
            if process.get("pid") in gate_pids or process.get("pgid") in gate_pids:
                matches.append("gate_pid_or_group")
            if any(name in args for name in names):
                matches.append("argv_verification_dir")
            if isinstance(cwd, str) and any(cwd == name or cwd.startswith(name + os.sep) for name in names):
                matches.append("cwd_verification_dir")
            if matches:
                known = next((entry for entry in survivors if entry["pid"] == process["pid"]), None)
                if known is not None:
                    known["match"].extend(matches)
                else:
                    survivors.append({"pid": process["pid"], "match": matches})
        if facts.get("incomplete"):
            reason = "inspection_incomplete"
    except recovery.ObservationError as exc:
        reason = exc.reason
    if survivors or reason:
        return {"status": "deferred", "reason": "survivors_observed" if survivors else reason, "survivors": survivors[:32], "gate_pids": gate_pids}
    removed = []
    try:
        home_fd = recovery.directory_below(root, ("verifications", verification_id), "record")
    except recovery.ObservationError as exc:
        return {"status": "deferred", "reason": exc.reason, "survivors": [], "gate_pids": gate_pids}
    try:
        for name in ("candidate", "tmp"):
            try:
                expected = (identities or {}).get(name)
                try:
                    probe = os.open(name, recovery.DIRECTORY_FLAGS, dir_fd=home_fd)
                except FileNotFoundError:
                    continue
                try:
                    current = _identity(probe)
                finally:
                    os.close(probe)
                if expected is None or current != list(expected):
                    return {"status": "deferred", "reason": "directory_identity_changed", "removed": removed, "survivors": [], "gate_pids": gate_pids}
                _remove_tree(home_fd, name)  # only the directory this run created, by device and inode
                removed.append(name)
            except OSError as exc:
                return {"status": "partial", "reason": type(exc).__name__, "removed": removed, "survivors": [], "gate_pids": gate_pids}
    finally:
        os.close(home_fd)
    return {"status": "removed", "removed": removed, "survivors": [], "gate_pids": gate_pids}


def _remove_tree(dir_fd, name):
    """Descriptor-relative removal: symlinks are unlinked, never followed; directories are opened with O_NOFOLLOW."""
    try:
        metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    child = os.open(name, recovery.DIRECTORY_FLAGS, dir_fd=dir_fd)
    try:
        with os.scandir(child) as entries:
            for entry in list(entries):
                _remove_tree(child, entry.name)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=dir_fd)


def invalidate(handoff_path, verification_id, reason, successor_job_id=None):
    """Audited return to the blocked projection with the original error intact; the record and any outcome stay."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        try:
            root, directory, manifest, state, _ = recovery.bind_handoff(directory)
        except recovery.ObservationError as exc:
            raise impl.ImplementationError("Handoff storage is not safely readable: " + exc.reason) from exc
        with root:
            stamp = room.now()
            try:
                home_fd = recovery.directory_below(root, ("verifications", verification_id), "record")
            except recovery.ObservationError:
                home_fd = None
            if home_fd is not None:
                try:
                    if not recovery.exists_below(home_fd, "invalidation.json"):
                        recovery.write_json_below(home_fd, "invalidation.json", {"verification_id": verification_id, "reason": reason,
                                                                                 "successor_job_id": successor_job_id, "invalidated_at": stamp}, "record")
                finally:
                    os.close(home_fd)
            binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
            if binding and binding.get("verification_id") == verification_id and state.get("phase") in ("blocked", "verifying"):
                state.update(phase="blocked", verification=None, active_stage=None, owner_job_id=None, needs_attention=True)
                state.setdefault("verification_history", []).append({"verification_id": verification_id, "status": "invalidated", "reason": reason,
                                                                     "successor_job_id": successor_job_id, "at": stamp,
                                                                     "predecessor_attempt": binding.get("predecessor_attempt")})
                recovery.write_json_below(root.fd, "state.json", state, "handoff")
                return True
            return False


def verify_outcome(root, directory, verification_id, outcome_sha=None):
    """The durable outcome, re-verified through owned reads: its digest (against the registry when known), every saved gate
    log and receipt digest, and the identity fields. Raises ObservationError when anything fails."""
    raw = recovery.read_owned(directory / "verifications" / verification_id / "outcome.json", recovery.EVIDENCE_LIMIT, "record", root=root)
    if outcome_sha is not None and room.sha(raw) != outcome_sha:
        raise recovery.ObservationError("evidence_changed", "outcome digest")
    outcome = json.loads(raw)
    if (not isinstance(outcome, dict) or outcome.get("verification_id") != verification_id or outcome.get("boundary") != BOUNDARY
            or outcome.get("result") not in ("completed", "interrupted") or not isinstance(outcome.get("gate_results"), list)):
        raise recovery.ObservationError("evidence_changed", "outcome shape")
    for index, gate in enumerate(outcome["gate_results"], 1):
        gate_dir = directory / "verifications" / verification_id / ("gate-%d" % index)
        for kind in ("stdout", "stderr"):
            if (not isinstance(gate, dict) or gate.get(kind + "_path") != str(gate_dir / (kind + ".txt"))
                    or recovery._stream_hash(gate_dir / (kind + ".txt"), recovery.EVIDENCE_LIMIT, kind="record", root=root)[0] != gate.get(kind + "_sha256")):
                raise recovery.ObservationError("evidence_changed", "gate log")
        receipt_raw = recovery.read_owned(gate_dir / "process-result.json", recovery.EVIDENCE_LIMIT, "record", root=root)
        if recovery._receipt(receipt_raw) is None or room.sha(receipt_raw) != gate.get("receipt_sha256"):
            raise recovery.ObservationError("evidence_changed", "gate receipt")
    return outcome, room.sha(raw)


def reconcile_completion(handoff_path, verification_id, outcome_sha):
    """Crash reconciliation for a still-bound projection. Returns ("completed", digest) after projecting a durable, registered
    completed outcome whose chain, gate log and receipt digests and the current original candidate all re-verify;
    ("interrupted", reason) after returning the projection to blocked for a registered interrupted outcome; or (None, reason)
    when nothing may be projected (the caller invalidates)."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        root, directory, manifest, state, _ = recovery.bind_handoff(directory)
        with root:
            binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
            if not binding or binding.get("verification_id") != verification_id or state.get("phase") not in ("blocked", "verifying"):
                return None, "verification_binding_mismatch"
            try:
                record_bytes = recovery.read_owned(directory / "verifications" / verification_id / "record.json", recovery.EVIDENCE_LIMIT, "record", root=root)
                if room.sha(record_bytes) != binding.get("record_sha256"):
                    return None, "evidence_changed"  # the immutable record must still be the registered bytes
                outcome, digest = verify_outcome(root, directory, verification_id, outcome_sha)
            except (recovery.ObservationError, OSError, ValueError, TypeError) as exc:
                return None, getattr(exc, "reason", "evidence_changed")
            if (outcome.get("handoff_id") != manifest["handoff_id"] or outcome.get("attempt") != binding.get("predecessor_attempt")
                    or outcome.get("predecessor_job_id") != binding.get("predecessor_job_id") or outcome.get("successor_job_id") != binding.get("successor_job_id")):
                return None, "verification_binding_mismatch"
            if outcome.get("result") == "interrupted":
                stamp = room.now()
                state.update(phase="blocked", needs_attention=True, active_stage=None, owner_job_id=None, verification=None)
                state.setdefault("verification_history", []).append({"verification_id": verification_id, "status": "interrupted",
                                                                     "reason": outcome.get("reason"), "at": stamp, "predecessor_attempt": binding.get("predecessor_attempt"),
                                                                     "successor_job_id": binding.get("successor_job_id"), "outcome_sha256": digest, "reconciled": True})
                recovery.write_json_below(root.fd, "state.json", state, "handoff")
                return "interrupted", str(outcome.get("reason") or "interrupted")
            if (outcome.get("result") != "completed" or outcome.get("record_sha256") != binding.get("record_sha256")
                    or outcome.get("candidate_sha256") != binding["candidate"]["sha256"]):
                return None, "verification_binding_mismatch"
            try:
                if candidate_snapshot(manifest["worktree_path"]) != binding["candidate"]:
                    return None, "candidate_changed"
            except recovery.ObservationError as exc:
                return None, exc.reason
            project_completion(state, binding, outcome, digest)
            recovery.write_json_below(root.fd, "state.json", state, "handoff")
            return "completed", digest


def settle_projected(handoff_path, verification_id, outcome_sha, record_sha):
    """Crash reconciliation after the engine already projected completion but before the worker recorded the outcome: the
    registered digest, the outcome chain, every gate digest, the projected gate results and the current original candidate
    must agree. Returns ("completed", digest), ("interrupted", reason) or (None, reason); the projection is never rewritten."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        root, directory, manifest, state, _ = recovery.bind_handoff(directory)
        with root:
            if isinstance(state.get("verification"), dict) or not isinstance(record_sha, str) or not record_sha:
                return None, "verification_binding_mismatch"
            try:
                record_bytes = recovery.read_owned(directory / "verifications" / verification_id / "record.json", recovery.EVIDENCE_LIMIT, "record", root=root)
                if room.sha(record_bytes) != record_sha:
                    return None, "evidence_changed"
                outcome, digest = verify_outcome(root, directory, verification_id, outcome_sha)
            except (recovery.ObservationError, OSError, ValueError, TypeError) as exc:
                return None, getattr(exc, "reason", "evidence_changed")
            if outcome.get("handoff_id") != manifest["handoff_id"] or outcome.get("record_sha256", record_sha) != record_sha:
                return None, "verification_binding_mismatch"  # an older interrupted outcome without the field is bound by the record bytes alone
            if outcome.get("result") == "interrupted":
                return ("interrupted", str(outcome.get("reason") or "interrupted")) if state.get("phase") == "blocked" else (None, "verification_binding_mismatch")
            history = [entry for entry in state.get("verification_history", []) if isinstance(entry, dict) and entry.get("verification_id") == verification_id]
            if (outcome.get("result") != "completed" or state.get("phase") != "awaiting_astra_review" or state.get("attempt_count") != outcome.get("attempt")
                    or room.canonical(state.get("gate_results")) != room.canonical(outcome.get("gate_results"))
                    or state.get("gates_passed") != outcome.get("gates_passed") or not any(entry.get("status") == "consumed" for entry in history)):
                return None, "verification_binding_mismatch"
            try:
                current = candidate_snapshot(manifest["worktree_path"])
            except recovery.ObservationError as exc:
                return None, exc.reason
            if current["sha256"] != outcome.get("candidate_sha256") or current != state.get("candidate"):
                return None, "candidate_changed"
            return "completed", digest


def clear_orphan_verifying(handoff_path):
    """An orphan projection (phase verifying with no binding and no dispatched row) returns to blocked through an audited
    history entry; nothing else is touched. Returns True when a transition was written."""
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    with room.lock_room(directory):
        root, directory, manifest, state, _ = recovery.bind_handoff(directory)
        with root:
            if state.get("phase") != "verifying" or isinstance(state.get("verification"), dict):
                return False
            state.update(phase="blocked", needs_attention=True, active_stage=None, owner_job_id=None, verification=None)
            state.setdefault("verification_history", []).append({"verification_id": None, "status": "invalidated", "reason": "registration_incomplete", "at": room.now()})
            recovery.write_json_below(root.fd, "state.json", state, "handoff")
            return True


def durable_outcome_result(handoff_path, verification_id, outcome_sha):
    """The result recorded by the durable outcome whose bytes hash to the registered digest, read through the owned handoff
    root; None when no such outcome exists. The worker consumes a row only when this says completed."""
    try:
        directory = Path(handoff_path)
        if directory.name == "handoff.json":
            directory = directory.parent
        identity = recovery.directory_identity(directory, "handoff")
        with recovery.OwnedRoot(directory, identity, "handoff") as root:
            raw = recovery.read_owned(directory / "verifications" / str(verification_id) / "outcome.json", recovery.EVIDENCE_LIMIT, "record", root=root)
    except (recovery.ObservationError, OSError, TypeError):
        return None
    if not isinstance(outcome_sha, str) or room.sha(raw) != outcome_sha:
        return None
    try:
        outcome = json.loads(raw)
    except ValueError:
        return None
    return outcome.get("result") if isinstance(outcome, dict) and outcome.get("verification_id") == verification_id else None


def validate_edge(handoff_path, row):
    """(valid, reason) for a registry verification row against its own immutable evidence: the record hashes to the row's
    digest and, for a consumed edge, the outcome hashes to the row's digest and names the same room, handoff, attempt,
    predecessor and successor. The live candidate and transcript are never consulted, so legitimate later corrections do
    not invalidate a historical edge."""
    directory = Path(handoff_path)
    if directory.name == "handoff.json":
        directory = directory.parent
    try:
        identity = recovery.directory_identity(directory, "handoff")
        with recovery.OwnedRoot(directory, identity, "handoff") as root:
            home = directory / "verifications" / str(row["id"])
            if room.sha(recovery.read_owned(home / "record.json", recovery.EVIDENCE_LIMIT, "record", root=root)) != row["record_sha256"]:
                return False, "record_tampered"
            if row["status"] != "consumed":
                return True, None
            raw = recovery.read_owned(home / "outcome.json", recovery.EVIDENCE_LIMIT, "record", root=root)
            if not row.get("outcome_sha256") or room.sha(raw) != row["outcome_sha256"]:
                return False, "outcome_tampered"
            outcome = json.loads(raw)
    except (recovery.ObservationError, OSError, ValueError, TypeError, KeyError):
        return False, "evidence_unreadable"
    if (not isinstance(outcome, dict) or outcome.get("result") != "completed" or outcome.get("room_id") != row["room_id"]
            or outcome.get("handoff_id") != row["handoff_id"] or outcome.get("attempt") != row.get("attempt")
            or outcome.get("predecessor_job_id") != row["predecessor_job_id"] or outcome.get("successor_job_id") != row["successor_job_id"]):
        return False, "outcome_identity_mismatch"
    return True, None


def linkage(result, status):
    """Classify a finished verifier for the registry: ("consumed", None), ("interrupted", reason), ("invalidated", reason) for a
    proven pre-launch refusal, or ("unknown", reason) which changes no row (lazy reconciliation decides later)."""
    verification = result.get("verification") if isinstance(result, dict) else None
    if isinstance(verification, dict) and verification.get("result") == "completed" and status == "succeeded":
        return "consumed", None
    if isinstance(verification, dict) and verification.get("result") == "interrupted":
        return "interrupted", str(verification.get("reason") or "interrupted")
    if isinstance(result, dict) and result.get("phase") == "refused_before_launch":
        return "invalidated", str(result.get("reason") or "refused_before_launch")
    if status == "cancelled":
        return "invalidated", "cancelled_before_launch"
    return "unknown", "no_classifiable_result"


def lineage(state):
    """Allowlisted verification lineage for status output."""
    binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
    keys = ("verification_id", "predecessor_job_id", "predecessor_attempt", "successor_job_id", "gate_index", "boundary", "budget_seconds",
            "dispatched_at", "launch_state", "launched_at")
    return {"active_verification": {key: binding.get(key) for key in keys} if binding else None,
            "verification_history": [{key: entry.get(key) for key in ("verification_id", "status", "reason", "at", "predecessor_attempt",
                                                                    "successor_job_id", "budget_seconds", "gates_passed", "launched_at")}
                                     for entry in state.get("verification_history", []) if isinstance(entry, dict)]}
