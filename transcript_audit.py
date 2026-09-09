#!/usr/bin/env python3
"""CLI-only, read-only audit of one implementation attempt's exact session transcript and its subagent files.

The audit derives the session UUID, transcript root and attempt interval from the immutable handoff manifest,
its pinned configuration and the attempt's process receipts; it accepts no override of those paths. It streams
the entire exact parent transcript and every bounded agent-*.jsonl file in the session's subagents directory
through owned descriptors, counting tool uses by name (mcp__qwen-local__*, mcp__deepseek__*, Agent, Task) and
reporting hashes, bytes scanned, attribution and completeness with reason codes. Every Agent/Task tool use must
carry launch evidence in its own file (a tool_result naming the child agent, or an error result proving no child
started); descendants requested from any scanned file (children launched by the parent, grandchildren launched by
children, and so on) must all be present, and the whole observed subtree must stay stable: the parent, every
earlier child and the bounded directory listing are re-checked after the last file is read. Counts are conservative
upper bounds: duplicate records, inline sidechain copies and records attributed to another session are counted and
reported separately, never subtracted. It emits no text or tool arguments, calls no model or network, and changes
no room, job or handoff state. Absence of Qwen use is acceptable evidence only when complete is true and every
Qwen count is zero.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

import implementation
import progress
import recovery
import room
import session_paths

SCHEMA_VERSION = 1
MAX_CHILD_FILES = 256
RECORD_LIMIT = session_paths.LINE_LIMIT
QWEN_PREFIX = progress.LOCAL_MODEL_PREFIX
DEEPSEEK_PREFIX = progress.REMOTE_MODEL_PREFIX
DELEGATE_TOOLS = progress.DELEGATE_TOOLS
AGENT_FILE = progress.AGENT_FILE
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
COUNT_KEYS = ("qwen_local", "deepseek", "agent", "task", "other")


class AuditError(Exception):
    pass


def _empty_counts():
    return {key: 0 for key in COUNT_KEYS}


def _classify(name):
    if not isinstance(name, str):
        return "other"
    if name.startswith(QWEN_PREFIX):
        return "qwen_local"
    if name.startswith(DEEPSEEK_PREFIX):
        return "deepseek"
    if name == "Agent":
        return "agent"
    if name == "Task":
        return "task"
    return "other"


def _handle(value):
    return progress._handle(value)


def _snapshot(fd):
    """Identity and extent of an open file, compared before and after a scan to detect a growing or replaced source."""
    info = os.fstat(fd)
    return info.st_size, info.st_mtime_ns, info.st_ino


def _identity(path, root):
    """The current (size, mtime, inode) of an owned regular file below `root`, or None when it cannot be opened safely."""
    try:
        with recovery.open_owned_regular(path, recovery.TRANSCRIPT_LIMIT, "transcript", root) as source:
            return _snapshot(source.fileno())
    except (recovery.ObservationError, OSError):
        return None


def _listing(fd):
    """Bounded view of the subagents directory: (name, agent, inode, size, mtime) for every regular user-owned
    agent-*.jsonl entry, the number of rejected entries, and whether the bound cut the listing short."""
    entries, rejected, truncated = [], 0, False
    with os.scandir(fd) as scan:
        for index, entry in enumerate(scan):
            if index >= MAX_CHILD_FILES:
                truncated = True
                break
            match = AGENT_FILE.fullmatch(entry.name)
            if not match:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                rejected += 1
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                rejected += 1
                continue
            entries.append((entry.name, match.group(1), info.st_ino, info.st_size, info.st_mtime_ns))
    return sorted(entries), rejected, truncated


def scan_file(path, root, session_id, interval, expected_agent=None):
    """Stream one JSONL file below `root`: hash every byte, count tool uses (whole file and inside the attempt
    interval), collect launched agent ids and observed models, and refuse to call the scan complete when the file
    grew or changed during the read, exceeded its bounds, or held a malformed or overlong record. Returns the
    report, the launched agent ids and the file identity observed when the read finished (None on failure)."""
    report = {"sha256": None, "bytes": 0, "records": 0, "malformed_records": 0, "overlong_records": 0, "foreign_session_records": 0,
              "duplicate_records": 0, "sidechain_records": 0, "launches": 0, "unresolved_launches": 0, "unidentified_launches": 0,
              "failed_launches": 0, "counts": _empty_counts(), "counts_in_attempt": _empty_counts(), "records_in_attempt": 0,
              "records_without_timestamp": 0, "launched_agent_handles": [], "models": [], "reasons": []}
    digest = hashlib.sha256()
    launched, models, seen_uuids, pending = set(), set(), set(), {}
    try:
        with recovery.open_owned_regular(path, recovery.TRANSCRIPT_LIMIT, "transcript", root) as source:
            before = _snapshot(source.fileno())
            while True:
                line = source.readline(RECORD_LIMIT + 1)
                if not line:
                    break
                digest.update(line)
                report["bytes"] += len(line)
                if report["bytes"] > recovery.TRANSCRIPT_LIMIT:
                    report["reasons"].append("transcript_oversized")
                    break
                if len(line) > RECORD_LIMIT:
                    report["overlong_records"] += 1
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                report["records"] += 1
                try:
                    record = json.loads(stripped)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    report["malformed_records"] += 1
                    continue
                if not isinstance(record, dict):
                    report["malformed_records"] += 1
                    continue
                if record.get("sessionId") != session_id:
                    report["foreign_session_records"] += 1
                if expected_agent is not None and record.get("agentId") not in (None, expected_agent):
                    report["foreign_session_records"] += 1
                identifier = record.get("uuid")
                if isinstance(identifier, str) and identifier:
                    if identifier in seen_uuids:
                        report["duplicate_records"] += 1  # counted again on purpose; the overcount is reported, not hidden
                    seen_uuids.add(identifier)
                if record.get("isSidechain") is True:
                    report["sidechain_records"] += 1
                inside = None
                if interval is not None:
                    stamp = record.get("timestamp")
                    try:
                        moment = room.parse_timestamp(stamp) if isinstance(stamp, str) else None
                    except room.RoomError:
                        moment = None
                    if moment is None:
                        report["records_without_timestamp"] += 1
                        inside = False
                    else:
                        inside = interval[0] <= moment <= interval[1]
                        if inside:
                            report["records_in_attempt"] += 1
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                if record.get("type") == "assistant":
                    model = message.get("model")
                    if isinstance(model, str) and MODEL_ID.match(model):
                        models.add(model)
                content = message.get("content") if isinstance(message.get("content"), list) else []
                results = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        category = _classify(block.get("name"))
                        report["counts"][category] += 1
                        if inside:
                            report["counts_in_attempt"][category] += 1
                        if category in ("agent", "task"):
                            report["launches"] += 1
                            identifier = block.get("id")
                            if isinstance(identifier, str) and identifier and identifier not in pending:
                                pending[identifier] = True  # awaiting this file's launch evidence
                            else:
                                report["unidentified_launches"] += 1  # no usable tool id: its child can never be correlated
                    elif block.get("type") == "tool_result":
                        results.append(block)
                outcome = record.get("toolUseResult") if isinstance(record.get("toolUseResult"), dict) else {}
                agent = outcome.get("agentId")
                agent = agent if isinstance(agent, str) and AGENT_FILE.match("agent-" + agent + ".jsonl") else None
                if agent is not None:
                    launched.add(agent)  # a launch receipt requests its descendant whether or not the tool use was seen
                for block in results:
                    identifier = block.get("tool_use_id")
                    if not isinstance(identifier, str) or not pending.pop(identifier, False):
                        continue
                    if agent is not None and len(results) == 1:
                        continue  # the receipt names the child; it is requested above
                    if block.get("is_error") is True:
                        report["failed_launches"] += 1  # evidence that no child started
                    else:
                        # A result that names no child (child transcripts record their own launches this way): the
                        # transcript it produced cannot be located by identity, so completeness is never claimed.
                        report["unidentified_launches"] += 1
            after = _snapshot(source.fileno())
    except recovery.ObservationError as exc:
        report["reasons"].append(exc.reason)
        return report, launched, None
    except OSError:
        report["reasons"].append("transcript_unreadable")
        return report, launched, None
    report["sha256"] = digest.hexdigest()
    report["unresolved_launches"] = len(pending)  # tool uses whose launch result never appeared (for example an attempt killed mid-launch)
    if before != after:
        report["reasons"].append("source_growing")
    if report["malformed_records"]:
        report["reasons"].append("malformed_records")
    if report["overlong_records"]:
        report["reasons"].append("overlong_records")
    if report["unresolved_launches"]:
        report["reasons"].append("launch_evidence_missing")
    if report["unidentified_launches"]:
        report["reasons"].append("launch_unidentified")
    report["launched_agent_handles"] = sorted(_handle(agent) for agent in launched)
    report["models"] = sorted(models)
    return report, launched, after


def _registered_handoff(home, room_id, handoff_id):
    if not isinstance(handoff_id, str) or not re.fullmatch(r"[0-9a-f]{64}", handoff_id):
        raise AuditError("handoff_id must be the 64-hex identifier returned by the room")
    database = Path(home) / "registry.sqlite3"
    if not database.is_file():
        raise AuditError("Project Room registry not found under the controller home")
    try:
        db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT path FROM handoffs WHERE room_id=? AND id=?", (room_id, handoff_id)).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise AuditError("Project Room registry is unreadable") from exc
    if row is None:
        raise AuditError("Unknown handoff for this room")
    return Path(row[0])


def _attempt_interval(directory, root, attempt, reasons):
    """(start, finish) from the attempt's spawn and exit receipts; None with reason codes when either is missing."""
    attempt_dir = directory / "attempts" / f"{attempt:04d}"
    values = {}
    for name, key in (("process-start.json", "started_at"), ("process-result.json", "finished_at")):
        try:
            value = json.loads(recovery.read_owned(attempt_dir / name, recovery.EVIDENCE_LIMIT, "evidence", root))
            values[key] = room.parse_timestamp(value.get(key)) if isinstance(value, dict) else None
        except (recovery.ObservationError, ValueError, RecursionError, room.RoomError, AttributeError):
            values[key] = None
    if values["started_at"] is None:
        reasons.append("attempt_start_receipt_missing")
    if values["finished_at"] is None:
        reasons.append("attempt_in_progress_or_result_receipt_missing")
    if values["started_at"] is None or values["finished_at"] is None:
        return None
    if values["finished_at"] < values["started_at"]:
        reasons.append("receipt_time_order")
        return None
    return values["started_at"], values["finished_at"]


def audit(home, room_id, handoff_id, attempt):
    """The audit report for one registered handoff attempt; raises AuditError only for unusable identifiers."""
    if type(attempt) is not int or attempt < 1:
        raise AuditError("attempt must be a positive integer")
    if not isinstance(room_id, str) or not room_id.strip():
        raise AuditError("room_id is required")
    handoff_path = _registered_handoff(home, room_id, handoff_id)
    report = {"schema_version": SCHEMA_VERSION, "meaning": "read-only tool-use audit of one exact session; no text, arguments, model or network",
              "room_id": room_id, "handoff_id": handoff_id, "attempt": attempt, "session_id": None, "attempt_interval": None,
              "parent": None, "children": [], "launched_children": 0, "requested_descendants": 0, "attributable_children": 0,
              "unattributed_children": 0, "children_missing": 0, "launches": 0, "unresolved_launches": 0, "unidentified_launches": 0,
              "failed_launches": 0, "children_listing_complete": False, "counts_total": _empty_counts(), "counts_in_attempt": _empty_counts(),
              "attribution": {"foreign_session_records": 0, "duplicate_records": 0, "sidechain_records_in_parent": 0, "ambiguous": False,
                              "meaning": "counts are conservative upper bounds: records attributed to another session, duplicate uuids and "
                                         "inline sidechain copies are counted and reported here, never subtracted"},
              "complete": False, "reasons": [], "qwen_absent": False}
    reasons = report["reasons"]
    try:
        root, directory, manifest, state, _ = recovery.bind_handoff(handoff_path)
    except (implementation.ImplementationError, recovery.ObservationError, OSError, ValueError, KeyError, TypeError):
        reasons.append("handoff_integrity")
        return report
    with root:
        if manifest.get("handoff_id") != handoff_id:
            reasons.append("handoff_integrity")
            return report
        session_id = manifest.get("session_id")
        worktree = manifest.get("worktree_path")
        report["session_id"] = session_id
        try:
            config = json.loads(recovery.read_owned(directory / "implementation-config.json", recovery.EVIDENCE_LIMIT, "handoff", root))
            if not isinstance(config, dict) or "claude_config_dir" not in config:
                raise ValueError("pinned configuration incomplete")
        except (recovery.ObservationError, ValueError, RecursionError):
            reasons.append("handoff_integrity")
            return report
        interval = _attempt_interval(directory, root, attempt, reasons)
        report["attempt_interval"] = {"started_at": interval[0].isoformat(), "finished_at": interval[1].isoformat()} if interval else None
    try:
        # The same binding rules as the recovery lane: exactly one transcript for the session UUID, metadata naming
        # the worktree root, read only below the configured transcript root through owned descriptors.
        transcript = Path(recovery.locate_transcript(config, manifest, worktree))
        transcript_root = recovery.transcript_root(config, transcript)
    except session_paths.SessionPathError as exc:
        reasons.append(exc.code)
        return report
    except (recovery.ObservationError, OSError, ValueError, KeyError, TypeError):
        reasons.append("transcript_missing_or_ambiguous")
        return report
    try:
        bound = recovery.OwnedRoot(transcript_root, kind="transcript")
    except (recovery.ObservationError, OSError):
        reasons.append("transcript_root_unsafe")  # the located root went missing or unsafe before the scan could bind it
        report["reasons"] = sorted(set(reasons))
        return report
    with bound:
        parent, launched_by_parent, parent_identity = scan_file(transcript, bound, session_id, interval)
        report["parent"] = parent
        reasons.extend(parent["reasons"])
        for key in COUNT_KEYS:
            report["counts_total"][key] += parent["counts"][key]
            report["counts_in_attempt"][key] += parent["counts_in_attempt"][key]
        _add_launch_facts(report, parent)
        report["attribution"]["sidechain_records_in_parent"] = parent["sidechain_records"]
        report["launched_children"] = len(launched_by_parent)
        requested = set(launched_by_parent)  # every descendant requested anywhere in the scanned session subtree
        subagents = transcript.parent / session_id / "subagents"
        fd = None
        try:
            relative = subagents.relative_to(bound.path).parts
            fd = recovery.directory_below(bound, relative, "transcript")
        except recovery.ObservationError as exc:
            if not exc.reason.endswith("_missing"):
                reasons.append("children_dir_unsafe")
        except ValueError:
            reasons.append("children_dir_unsafe")
        listing, scanned, identities, truncated = [], [], {}, False
        if fd is not None:
            try:
                try:
                    listing, rejected, truncated = _listing(fd)
                except OSError:
                    listing, rejected, truncated = [], 0, False
                    reasons.append("children_dir_unreadable")
                if rejected:
                    reasons.append("child_path_rejected")
                if truncated:
                    reasons.append("children_truncated")
                for name, agent, *_ in listing:
                    child, launched_here, identity = scan_file(subagents / name, bound, session_id, interval, expected_agent=agent)
                    requested |= launched_here
                    identities[name] = identity
                    scanned.append((agent, child))
                # Whole-scan stability: a complete listing observed before any child was read must still hold afterwards.
                # A truncated listing is already incomplete, and two partial enumerations of an unordered directory must
                # not be compared as though an order difference were a definitive change.
                if not truncated and "children_dir_unreadable" not in reasons:
                    try:
                        if _listing(fd)[0] != listing:
                            reasons.append("children_dir_changed")
                        else:
                            # Every agent-*.jsonl the session's subagents directory holds was read, whether or not a launch
                            # receipt attributes it; this is the coverage fact a launch that names no child still leaves.
                            report["children_listing_complete"] = True
                    except OSError:
                        reasons.append("children_dir_unreadable")
            finally:
                os.close(fd)
        elif "children_dir_unsafe" not in reasons:
            report["children_listing_complete"] = True  # no subagents directory: the session launched nothing into one
        # Earlier sources must not have changed while later ones were being read; per-file checks alone cannot see that.
        if parent_identity is not None and _identity(transcript, bound) != parent_identity:
            reasons.append("parent_changed_during_scan")
        for name, identity in identities.items():
            if identity is not None and _identity(subagents / name, bound) != identity:
                reasons.append("child_changed_during_scan")
        seen = set()
        for agent, child in scanned:
            seen.add(agent)
            attributable = agent in requested  # launched by the parent or by any scanned descendant of this session
            summary = {"handle": _handle(agent), "attributable": attributable, "models": child["models"], "sha256": child["sha256"], "bytes": child["bytes"],
                       "records": child["records"], "foreign_session_records": child["foreign_session_records"], "duplicate_records": child["duplicate_records"],
                       "launches": child["launches"], "unresolved_launches": child["unresolved_launches"], "unidentified_launches": child["unidentified_launches"],
                       "failed_launches": child["failed_launches"], "counts": child["counts"], "counts_in_attempt": child["counts_in_attempt"],
                       "launched_agent_handles": child["launched_agent_handles"], "reasons": child["reasons"]}
            report["children"].append(summary)
            reasons.extend("child_" + reason for reason in child["reasons"])
            report["attributable_children" if attributable else "unattributed_children"] += 1
            _add_launch_facts(report, child)
            for key in COUNT_KEYS:
                report["counts_total"][key] += child["counts"][key]
                report["counts_in_attempt"][key] += child["counts_in_attempt"][key]
        missing = requested - seen
        report["requested_descendants"] = len(requested)
        report["children_missing"] = len(missing)
        if missing:
            reasons.append("child_missing")
    attribution = report["attribution"]
    attribution["ambiguous"] = bool(attribution["foreign_session_records"] or attribution["duplicate_records"] or attribution["sidechain_records_in_parent"])
    report["reasons"] = sorted(set(reasons))
    report["complete"] = not report["reasons"]
    report["qwen_absent"] = report["complete"] and report["counts_total"]["qwen_local"] == 0
    return report


def _add_launch_facts(report, scanned):
    """Fold one scanned file's launch and attribution counters into the report totals."""
    for key in ("launches", "unresolved_launches", "unidentified_launches", "failed_launches"):
        report[key] += scanned[key]
    report["attribution"]["foreign_session_records"] += scanned["foreign_session_records"]
    report["attribution"]["duplicate_records"] += scanned["duplicate_records"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(Path(args.home).expanduser().resolve(), args.room, args.handoff, args.attempt)
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
