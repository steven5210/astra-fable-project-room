#!/usr/bin/env python3
"""Read-only, bounded progress observation for supervised Project Room jobs.

The ``progress`` object is advisory. Lifecycle comes from owned registry,
review-turn, and handoff state; deadlines come only from the timeouts pinned
per room or per handoff; activity comes from a bounded scan of the exact
owned session transcript plus conservatively attributable subagent files.
Observation never spawns a process, calls a model, edits state, cancels,
replays, or extends a job, and it emits only allowlisted metadata.
"""

import collections
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

import implementation
import room
import recovery
import session_paths

SCHEMA_VERSION = 1
PARENT_TAIL_BYTES = 2 * 1024 * 1024
PARENT_MAX_RECORDS = 5000
CHILD_TAIL_BYTES = 512 * 1024
CHILD_MAX_RECORDS = 2000
MAX_CHILD_FILES = 16
MAX_DELEGATE_ITEMS = 8
MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
FUTURE_TOLERANCE = dt.timedelta(0)
MAX_DISCOVERY_ENTRIES = 256
LIVE_STATE_PHASES = ("running_model", "running_gates")
FINAL_STATE_PHASES = ("awaiting_astra_review", "blocked", "scope_change")
CATEGORIES = {"Read": "read", "Glob": "read", "Grep": "read", "LS": "read", "NotebookRead": "read",
              "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
              "Bash": "shell", "BashOutput": "shell", "KillShell": "shell",
              "Agent": "delegate", "Task": "delegate", "Skill": "skill", "StructuredOutput": "output"}
LOCAL_MODEL_PREFIX = "mcp__qwen-local__"
DELEGATE_TOOLS = ("Agent", "Task")
KNOWN_ROLES = ("sonnet-worker", "opus-reviewer")
MODEL_ID = re.compile(r"^claude-(?:fable|mythos|opus|sonnet|haiku)-[0-9]{1,2}(?:-[0-9]{1,2})?(?:-[0-9]{8})?$")
MAX_CWD_LENGTH = 1024
MAX_CWD_LOOKUPS = 256
AGENT_FILE = re.compile(r"^agent-([A-Za-z0-9_-]{1,64})\.jsonl$")
Event = collections.namedtuple("Event", "ts order kind category source")


def clock():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    """Return an aware UTC datetime for a valid timezone-qualified ISO timestamp, else None."""
    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return None
    try:
        return room.parse_timestamp(value).astimezone(dt.timezone.utc)
    except room.RoomError:
        return None


def stamp(value):
    """Second-precision UTC text; the only timestamp form emitted in progress."""
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _attempt(value):
    return value if type(value) is int and value > 0 else None


def _file_key(path, info, *extra):
    return (os.path.abspath(str(path)), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, *extra)


def read_json(path, limit, cache=None):
    """Read a bounded regular metadata file without following a leaf symlink."""
    try:
        path = Path(path)
        with recovery.open_owned_regular(path, limit, root=path.parent) as handle:
            info = os.fstat(handle.fileno())
            key = _file_key(path, info, "json")
            if cache is not None and key in cache:
                return cache[key], None
            raw = handle.read(limit + 1)
            if len(raw) > limit:
                return None, "oversized"
            value = json.loads(raw)
            if cache is not None:
                cache[key] = value
            return value, None
    except recovery.ObservationError as exc:
        return None, "oversized" if exc.reason.endswith("_oversized") else "unreadable"
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, "unreadable"


@contextlib.contextmanager
def _directory_scan(root, relative=()):
    fd = recovery.directory_below(root, relative) if relative else os.dup(root.fd)
    try:
        with os.scandir(fd) as entries:
            yield entries
    finally:
        os.close(fd)


def _bind_roots(projects_dir, explicit_root, stack):
    roots = {}
    for value in (projects_dir, explicit_root):
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("relative transcript root")
        canonical = Path(os.path.realpath(path))
        if canonical not in roots:
            try:
                identity = recovery.directory_identity(canonical, "transcript")
                roots[canonical] = stack.enter_context(recovery.OwnedRoot(canonical, expected=identity, kind="transcript"))
            except recovery.ObservationError as exc:
                if exc.reason == "transcript_missing":
                    continue
                raise
        roots[path] = roots[canonical]
    return roots


def _candidate_root(path, roots):
    """Map only a configured-prefix alias; never resolve a component below that boundary."""
    path = Path(os.path.abspath(path))
    for prefix, root in roots.items():
        try:
            relative = path.relative_to(prefix)
        except ValueError:
            continue
        if relative.parts and all(part not in ("..", ".") for part in relative.parts):
            return root, root.path / relative
    return None, None


def locate_transcript(session_id, predicted_path=None, projects_dir=None, explicit_root=None, bound_roots=None):
    """Select one exact UUID from bounded filename discovery, including explicit-template duplicate checks."""
    limitations = set()
    try:
        if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        return None, "metadata_unsupported", limitations
    filename = session_id + ".jsonl"
    candidates = {}
    with contextlib.ExitStack() as stack:
        try:
            roots = _bind_roots(projects_dir, explicit_root, stack) if bound_roots is None else bound_roots
        except (OSError, ValueError, recovery.ObservationError):
            return None, "path_rejected", limitations

        def consider(candidate, predicted):
            root, canonical = _candidate_root(candidate, roots)
            if root is None:
                # A configured directory that does not yet exist is normal before
                # the worker writes its first transcript. Do not read outside it.
                allowed_missing = any(value and Path(candidate).is_relative_to(Path(value).expanduser())
                                      for value in (projects_dir, explicit_root))
                if predicted and (not allowed_missing or Path(candidate).name != filename):
                    limitations.add("predicted_path_rejected")
                return
            if canonical.name != filename:
                if predicted:
                    limitations.add("predicted_path_rejected")
                return
            try:
                # Opening below an already bound directory proves containment/type at the actual operation.
                with recovery.open_owned_regular(canonical, 2**63 - 1, "transcript", root=root):
                    pass
                candidates[str(canonical)] = canonical
            except recovery.ObservationError as exc:
                if predicted and exc.reason != "transcript_missing":
                    limitations.add("predicted_path_rejected")

        if isinstance(predicted_path, str) and predicted_path:
            predicted = Path(predicted_path).expanduser()
            if predicted.is_absolute():
                consider(predicted, True)
            else:
                limitations.add("predicted_path_rejected")
        try:
            project_path = Path(projects_dir).expanduser() if projects_dir else None
            root = roots.get(project_path) if project_path is not None else None
            if root is not None:
                with _directory_scan(root) as entries:
                    for _ in range(MAX_DISCOVERY_ENTRIES):
                        entry = next(entries, None)
                        if entry is None:
                            break
                        if entry.is_dir(follow_symlinks=False):
                            consider(root.path / entry.name / filename, False)
                    else:
                        return None, "projects_scan_limited", {"projects_scan_limited"}
        except (OSError, ValueError, recovery.ObservationError):
            return None, "projects_scan_failed", {"projects_scan_failed"}
        if not candidates:
            return None, "path_rejected" if "predicted_path_rejected" in limitations else "transcript_missing", limitations
        if len(candidates) != 1:
            return None, "session_ambiguous", limitations
        return next(iter(candidates.values())), None, limitations


def read_records(path, max_bytes, max_records, cache=None, root=None):
    """Parse a bounded tail of a JSONL file; return (records, limitations) or (None, limitations)."""
    limitations = set()
    try:
        with recovery.open_owned_regular(path, 2**63 - 1, "transcript", root=root) as handle:
            info = os.fstat(handle.fileno())
            key = _file_key(path, info, "records", max_bytes, max_records)
            if cache is not None and key in cache:
                records, cached = cache[key]
                return records, set(cached)
            offset = max(0, info.st_size - max_bytes)
            handle.seek(offset)
            data = handle.read(max_bytes)
    except (OSError, ValueError, recovery.ObservationError):
        return None, {"transcript_unreadable"}
    if offset > 0:
        limitations.add("tail_window_truncated")
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    lines = data.split(b"\n")
    partial = lines.pop() if lines else b""  # bytes after the final newline, normally an in-progress record
    if len(lines) > max_records:
        limitations.add("record_window_truncated")
        lines = lines[-max_records:]
    records, malformed, unsupported = [], 0, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            unsupported += 1
    if partial.strip():
        try:
            value = json.loads(partial)
            if isinstance(value, dict):
                records.append(value)
        except (ValueError, UnicodeDecodeError, RecursionError):
            pass  # A partial last line is normal while the session is being written.
    if len(records) > max_records:
        records = records[-max_records:]
        limitations.add("record_window_truncated")
    if malformed:
        limitations.add("malformed_records_skipped")
    if unsupported:
        limitations.add("unsupported_records_skipped")
    if cache is not None:
        cache[key] = (records, set(limitations))
    return records, limitations


class _Window:
    """Exact session, expected working directory, and current attempt time bounds."""

    def __init__(self, session_id, expected_cwd, start, now):
        self.session_id = session_id
        self.expected = os.path.realpath(str(expected_cwd)) if expected_cwd else None
        self.start = start
        self.end = now
        self.cache = {}

    def cwd_matches(self, value):
        if self.expected is None or not isinstance(value, str) or not 0 < len(value) <= MAX_CWD_LENGTH:
            return False
        if value not in self.cache:
            if len(self.cache) >= MAX_CWD_LOOKUPS or value.count(os.sep) > 64:
                return False
            try:
                self.cache[value] = session_paths.cwd_relation(value, Path(self.expected)) is not None
            except (OSError, ValueError):
                self.cache[value] = False
        return self.cache[value]


def _admit(record, window, scan, sidechain):
    """Return the timestamp of a record that belongs to this session, cwd and attempt window."""
    if not isinstance(record, dict) or record.get("sessionId") != window.session_id:
        return None
    scan["session_seen"] = True
    if record.get("type") not in ("user", "assistant"):
        return None
    flag = record.get("isSidechain")
    if sidechain:
        if flag is not True:
            return None
    elif flag is True:
        scan["limitations"].add("inline_sidechain_records_ignored")
        return None
    if "cwd" in record and not window.cwd_matches(record.get("cwd")):
        scan["cwd_rejected"] = True
        return None
    ts = parse_time(record.get("timestamp"))
    if ts is None:
        scan["limitations"].add("records_without_valid_timestamp_ignored")
        return None
    if ts < window.start:
        return None
    if ts > window.end:
        scan["limitations"].add("future_records_ignored")
        return None
    return ts


def _category(name):
    if not isinstance(name, str):
        return "other"
    if name.startswith(LOCAL_MODEL_PREFIX):
        return "local-model"
    return CATEGORIES.get(name, "other")


def _role(tool_input):
    if isinstance(tool_input, dict):
        value = tool_input.get("subagent_type")
        if isinstance(value, str):
            return value if value in KNOWN_ROLES else "other"
    return "unknown"


def _background(tool_input):
    return isinstance(tool_input, dict) and tool_input.get("run_in_background") is True


def _handle(tool_id):
    return hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:12]


def _blocks(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _short_text(value, limit=128):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _agent_id(value):
    return value if isinstance(value, str) and AGENT_FILE.match(f"agent-{value}.jsonl") else None


def _scan(records, window, source, agent_id=None):
    """Collect events, delegate requests/results and child correlation facts from admitted records."""
    sidechain = source == "child_session"
    scan = {"events": [], "delegates": {}, "pending": {}, "agent_blocks": {}, "agent_tools": {}, "record_counts": {}, "sources": set(),
            "witness": False, "model": None, "session_seen": False, "cwd_rejected": False, "last_type": None, "last_stop": None,
            "limitations": set()}
    for order, record in enumerate(records):
        if sidechain and isinstance(record, dict) and record.get("agentId") != agent_id:
            continue
        ts = _admit(record, window, scan, sidechain)
        if ts is None:
            continue
        if "cwd" in record:
            scan["witness"] = True
        origin = _short_text(record.get("sourceToolAssistantUUID"))
        if origin is not None:
            scan["sources"].add(origin)
        record_uuid = _short_text(record.get("uuid"))
        if record_uuid is not None:
            scan["record_counts"][record_uuid] = scan["record_counts"].get(record_uuid, 0) + 1
        scan["last_type"] = record["type"]
        if record["type"] == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if isinstance(message.get("model"), str):
                scan["model"] = message["model"]
            scan["last_stop"] = message.get("stop_reason") if isinstance(message.get("stop_reason"), str) else None
            kind, category, agent_blocks = "assistant_message", "message", 0
            for block in _blocks(record):
                if block.get("type") != "tool_use":
                    continue
                name, tool_id = block.get("name"), block.get("id")
                category, kind = _category(name), "tool_start"
                if name in DELEGATE_TOOLS:
                    agent_blocks += 1
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                scan["pending"][tool_id] = category
                if name in DELEGATE_TOOLS:
                    if record_uuid is not None:
                        scan["agent_tools"].setdefault(record_uuid, []).append(tool_id)
                    if tool_id in scan["delegates"]:
                        scan["delegates"][tool_id]["reused"] = True  # A reused tool ID can never be correlated.
                    else:
                        scan["delegates"][tool_id] = {
                            "handle": _handle(tool_id), "requested_role": _role(block.get("input")),
                            "background": _background(block.get("input")), "state": "pending", "requested_at": ts,
                            "result_at": None, "agent_id": None, "reused": False, "order": order, "child": None}
            if record_uuid is not None:
                scan["agent_blocks"][record_uuid] = scan["agent_blocks"].get(record_uuid, 0) + agent_blocks
        else:
            scan["last_stop"] = None
            kind, category = "user_message", "message"
            results = [block for block in _blocks(record) if block.get("type") == "tool_result"]
            outcome = record.get("toolUseResult") if isinstance(record.get("toolUseResult"), dict) else {}
            for block in results:
                kind = "tool_result"
                tool_id = block.get("tool_use_id")
                category = scan["pending"].pop(tool_id, "other") if isinstance(tool_id, str) else "other"
                delegate = scan["delegates"].get(tool_id) if isinstance(tool_id, str) else None
                if delegate is not None and delegate["state"] == "pending":
                    # Claude Code launches delegates in the background by default: the result only
                    # acknowledges the launch, so completion is not claimed for a launched delegate.
                    launched = delegate["background"] or outcome.get("status") == "async_launched"
                    delegate.update(state="background" if launched else "completed", result_at=ts,
                                    agent_id=_agent_id(outcome.get("agentId")) if len(results) == 1 else None)
        scan["events"].append(Event(ts, order, kind, category, source))
    return scan


def _child_files(transcript, session_id, window_start, root=None):
    """Inspect at most 256 directory entries and retain at most 16 recent regular child files."""
    limitations = set()
    base = Path(transcript).parent
    directory = base / session_id / "subagents"
    entries = []
    with contextlib.ExitStack() as stack:
        try:
            if root is None:
                identity = recovery.directory_identity(base, "transcript")
                root = stack.enter_context(recovery.OwnedRoot(base, expected=identity, kind="transcript"))
            relative = directory.relative_to(root.path).parts
            with _directory_scan(root, relative) as children:
                for _ in range(MAX_DISCOVERY_ENTRIES):
                    child = next(children, None)
                    if child is None:
                        break
                    match = AGENT_FILE.fullmatch(child.name)
                    if not match:
                        continue
                    info = child.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                        limitations.add("child_path_rejected")
                        continue
                    if info.st_mtime < window_start.timestamp():
                        continue
                    entries.append((-info.st_mtime_ns, child.name, directory / child.name, match.group(1)))
                else:
                    limitations.add("children_discovery_limited")
        except recovery.ObservationError as exc:
            if not exc.reason.endswith("_missing"):
                limitations.add("children_dir_rejected")
            return [], limitations
        except (OSError, ValueError):
            return [], {"children_dir_rejected"}
    entries.sort()
    if len(entries) > MAX_CHILD_FILES:
        limitations.add("children_truncated")
    return [(child, agent_id) for _, _, child, agent_id in entries[:MAX_CHILD_FILES]], limitations


def _correlate(parent, scan, agent_id):
    """Return (delegate key, reason). Attribution needs one unique stable link, never location alone.

    The structured agentId recorded with the parent's launch result is the primary link. A
    child record's sourceToolAssistantUUID is used only when it names a parent record that
    holds exactly one delegate block; links to records the parent never wrote are ignored.
    """
    delegates = parent["delegates"]
    by_agent = [key for key, item in delegates.items() if item["agent_id"] == agent_id]
    if len(by_agent) > 1:
        return None, "child_attribution_ambiguous"
    links = {origin for origin in scan["sources"] if origin in parent["record_counts"]}
    by_source = None  # None: no usable link; "ambiguous"; or a delegate key
    if len(links) > 1:
        by_source = "ambiguous"
    elif links:
        origin = next(iter(links))
        tools = parent["agent_tools"].get(origin, [])
        if parent["record_counts"][origin] > 1 or parent["agent_blocks"].get(origin, 0) > 1 or (tools and delegates[tools[0]]["reused"]):
            by_source = "ambiguous"
        elif tools:
            by_source = tools[0]
    if by_agent:
        key = by_agent[0]
        if delegates[key]["reused"] or (by_source is not None and by_source != key):
            return None, "child_attribution_ambiguous"
        return key, None
    if by_source == "ambiguous":
        return None, "child_attribution_ambiguous"
    if by_source is not None:
        return by_source, None
    return None, "child_attribution_unavailable"


def _attach_children(transcript, session_id, window, parent, cache, root=None):
    files, limitations = _child_files(transcript, session_id, window.start, root=root)
    observed = attributed = 0
    claims = {}
    for path, agent_id in files:
        records, limits = read_records(path, CHILD_TAIL_BYTES, CHILD_MAX_RECORDS, cache, root=root)
        if records is None:
            limitations.add("child_unreadable")
            continue
        limitations |= limits
        scan = _scan(records, window, "child_session", agent_id)
        limitations |= scan["limitations"]
        if not scan["events"]:
            continue
        observed += 1
        if not scan["witness"]:
            limitations.add("child_cwd_witness_missing")
            continue
        key, reason = _correlate(parent, scan, agent_id)
        if key is None:
            limitations.add(reason)
            continue
        claims.setdefault(key, []).append(scan)
    for key, scans in claims.items():
        if len(scans) != 1:
            limitations.add("child_attribution_ambiguous")
            continue
        delegate, scan = parent["delegates"][key], scans[0]
        latest = max(scan["events"], key=lambda event: (event.ts, event.order))
        model = scan["model"]
        if model is not None and not MODEL_ID.match(model):
            model = "unknown"
            limitations.add("observed_model_unrecognized")
        # The child's turn is known to have ended only when its last record is an assistant
        # message that stopped with end_turn; a missing stop reason is unknown, not false.
        ended = (None if scan["last_stop"] is None else scan["last_stop"] == "end_turn") if scan["last_type"] == "assistant" else False
        delegate["child"] = {"observed_model": model, "last_observed_at": stamp(latest.ts), "last_category": latest.category,
                             "turn_ended": ended}
        parent["events"].extend(scan["events"])
        attributed += 1
    return observed, attributed, limitations


def observe_session(transcript, session_id, expected_cwd, window_start, now, cache=None):
    """Bounded read-only observation of one owned session within one attempt window."""
    result = {"activity": None, "activity_unavailable_reason": None, "delegates": None, "limitations": set()}
    transcript = transcript if isinstance(transcript, dict) else {}
    with contextlib.ExitStack() as stack:
        try:
            roots = _bind_roots(transcript.get("projects_dir"), transcript.get("explicit_root"), stack)
        except (OSError, ValueError, recovery.ObservationError):
            result["activity_unavailable_reason"] = "path_rejected"
            return result
        path, reason, limits = locate_transcript(session_id, transcript.get("predicted"), transcript.get("projects_dir"),
                                                 transcript.get("explicit_root"), bound_roots=roots)
        result["limitations"] |= limits
        if path is None:
            result["activity_unavailable_reason"] = reason
            return result
        owned, path = _candidate_root(path, roots)
        records, limits = read_records(path, PARENT_TAIL_BYTES, PARENT_MAX_RECORDS, cache, root=owned)
        result["limitations"] |= limits
        if records is None:
            result["activity_unavailable_reason"] = "transcript_unreadable"
            return result
        window = _Window(session_id, expected_cwd, window_start, now)
        parent = _scan(records, window, "parent_session")
        result["limitations"] |= parent["limitations"]
        if not parent["session_seen"]:
            result["activity_unavailable_reason"] = "session_mismatch" if records else "no_records_in_attempt_window"
            return result
        if parent["events"] and not parent["witness"]:
            result["activity_unavailable_reason"] = "parent_cwd_witness_missing"
            return result
        observed, attributed, limits = (_attach_children(path, session_id, window, parent, cache, root=owned)
                                        if parent["witness"] else (0, 0, set()))
        result["limitations"] |= limits
        if parent["events"]:
            latest = max(parent["events"], key=lambda event: (event.ts, event.source == "parent_session", event.order))
            result["activity"] = {"last_observed_at": stamp(latest.ts), "category": latest.category,
                                  "source": latest.source, "event": latest.kind}
        else:
            result["activity_unavailable_reason"] = "cwd_mismatch" if parent["cwd_rejected"] else "no_records_in_attempt_window"
        priority = {"pending": 0, "background": 1, "completed": 2}
        delegates = sorted(parent["delegates"].values(), key=lambda item: (priority[item["state"]], -item["requested_at"].timestamp(), -item["order"]))
        result["delegates"] = {
            "requested": len(delegates),
            **{state: sum(item["state"] == state for item in delegates) for state in ("pending", "background", "completed")},
            "observed_children": observed, "attributed_children": attributed,
            "items": [{"handle": item["handle"], "requested_role": item["requested_role"], "state": item["state"],
                       "requested_at": stamp(item["requested_at"]), "result_at": stamp(item["result_at"]),
                       "child": item["child"]} for item in delegates[:MAX_DELEGATE_ITEMS]],
            "truncated": len(delegates) > MAX_DELEGATE_ITEMS}
        return result


def review_context(review_dir, request_key, predicted_transcript, claude_config_dir):
    """Read-only lookup of the owned review turn, its pinned timeout, and transcript inputs."""
    projects = str(Path(claude_config_dir) / "projects") if isinstance(claude_config_dir, str) and claude_config_dir else None
    context = {"turn": None, "timeout_seconds": None, "expected_cwd": str(review_dir),
               "transcript": {"predicted": predicted_transcript if isinstance(predicted_transcript, str) else None,
                              "projects_dir": projects, "explicit_root": None}}
    try:
        db = sqlite3.connect((Path(review_dir) / "room.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2)
    except (sqlite3.Error, OSError, ValueError):
        context["error"] = "unreadable"
        return context
    try:
        row = db.execute("SELECT status,session_id,started_at,finished_at FROM turns WHERE request_id=?", (request_key,)).fetchone()
        if row is not None:
            context["turn"] = {"status": row[0], "session_id": row[1], "started_at": row[2], "finished_at": row[3]}
        snapshot = db.execute("SELECT value FROM metadata WHERE key='config_snapshot'").fetchone()
        if snapshot is not None:
            config = json.loads(snapshot[0])
            context["timeout_seconds"] = config.get("timeout_seconds") if isinstance(config, dict) else None
    except (sqlite3.Error, ValueError, TypeError):
        context["error"] = "unreadable"
    finally:
        db.close()
    return context


def handoff_context(handoff_path, cache=None):
    """Bounded read-only load of handoff state, manifest, and pinned config with integrity checks."""
    context = {"state": None, "manifest": None, "config": None, "config_verified": False, "limitations": set(), "transcript": {}}
    if not isinstance(handoff_path, str) or not handoff_path:
        return context
    directory = Path(handoff_path)
    if directory.name == "handoff.json":
        directory = directory.parent
    state, _ = read_json(directory / "state.json", MAX_STATE_BYTES, cache)
    if not isinstance(state, dict):
        return context
    context["state"] = state
    manifest, _ = read_json(directory / "handoff.json", MAX_METADATA_BYTES, cache)
    if not isinstance(manifest, dict) or implementation._digest(manifest) != state.get("manifest_sha256"):
        context["limitations"].add("handoff_manifest_unverified")
        return context
    context["manifest"] = manifest
    pinned = manifest.get("pinned_files") if isinstance(manifest.get("pinned_files"), dict) else {}
    config = None
    try:
        config_path = directory / "implementation-config.json"
        with recovery.open_owned_regular(config_path, MAX_METADATA_BYTES, root=directory) as source:
            raw = source.read(MAX_METADATA_BYTES + 1)
            if len(raw) <= MAX_METADATA_BYTES and room.sha(raw) == pinned.get("implementation-config.json"):
                config = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, recovery.ObservationError):
        config = None
    if isinstance(config, dict):
        context["config"], context["config_verified"] = config, True
    else:
        context["limitations"].add("handoff_config_unverified")
        config = {}
    predicted = manifest.get("session_transcript_path")
    predicted = predicted if isinstance(predicted, str) and predicted else None
    config_dir = config.get("claude_config_dir")
    context["transcript"] = {
        "predicted": predicted,
        "projects_dir": str(Path(config_dir) / "projects") if isinstance(config_dir, str) and config_dir else None,
        "explicit_root": str(Path(predicted).parent) if predicted and config.get("session_transcript_path") else None}
    return context


def _elapsed(start, end, limitations):
    if start is None or end is None:
        return None
    seconds = math.floor((end - start).total_seconds())
    if seconds < 0:
        limitations.add("clock_anomaly")
        return 0
    return seconds


def _deadline(scope, basis, start, timeout, now, limitations):
    """Countdown to the pinned kill deadline for the active stage; never an ETA."""
    if start is None:
        return None, "state_unreadable"
    if not _positive(timeout):
        return None, "pinned_timeout_unavailable"
    if start > now + FUTURE_TOLERANCE:
        limitations.add("clock_anomaly")
        return None, "clock_anomaly"
    try:
        deadline_at = start + dt.timedelta(seconds=timeout)
        remaining = math.floor((deadline_at - now).total_seconds())
    except (OverflowError, ValueError):
        return None, "pinned_timeout_unavailable"
    return {"scope": scope, "basis": basis, "started_at": stamp(start), "timeout_seconds": timeout,
            "deadline_at": stamp(deadline_at), "remaining_seconds": max(0, min(remaining, math.floor(timeout))),
            "expired": remaining < 0, "meaning": "timeout_countdown_not_eta"}, None


def _stall(out, phase, reason):
    out.update(phase=phase, deadline_unavailable_reason=reason, activity_unavailable_reason=reason)


def _model_stage(out, basis, start, timeout, transcript, session_id, expected_cwd, now, limitations, cache, active=True):
    out["phase"] = "model"
    if active:
        out["deadline"], out["deadline_unavailable_reason"] = _deadline("model_invocation", basis, start, timeout, now, limitations)
    else:
        out["deadline_unavailable_reason"] = "stage_transition"
    if start is None:
        out["activity_unavailable_reason"] = "state_unreadable"
        return
    observation = observe_session(transcript, session_id, expected_cwd, start, now, cache)
    limitations |= observation["limitations"]
    out.update(activity=observation["activity"], activity_unavailable_reason=observation["activity_unavailable_reason"],
               delegates=observation["delegates"])
    delegates = observation["delegates"]
    if delegates and (delegates["pending"] or any(item["state"] == "background" and item["child"] and item["child"]["turn_ended"] is not True
                                                  for item in delegates["items"])):
        out["phase_detail"] = "delegate_pending"


def _running_review(out, now, review, limitations, cache):
    if not isinstance(review, dict) or review.get("error"):
        limitations.add("review_state_unreadable")
        _stall(out, "unknown", "state_unreadable")
        return
    turn = review.get("turn")
    if not isinstance(turn, dict):
        _stall(out, "starting", "starting")
    elif turn.get("status") != "pending":
        _stall(out, "finalizing", "finalizing")
    else:
        _model_stage(out, "pinned_review_timeout", parse_time(turn.get("started_at")), review.get("timeout_seconds"),
                     review.get("transcript"), turn.get("session_id"), review.get("expected_cwd"), now, limitations, cache)


def _running_implementation(out, job, now, handoff, limitations, cache):
    state = handoff.get("state") if isinstance(handoff, dict) else None
    if not isinstance(state, dict):
        limitations.add("handoff_state_unreadable")
        _stall(out, "unknown", "state_unreadable")
        return
    limitations |= set(handoff.get("limitations", ()))
    manifest = handoff.get("manifest") if isinstance(handoff.get("manifest"), dict) else {}
    config = handoff.get("config") if handoff.get("config_verified") and isinstance(handoff.get("config"), dict) else {}
    phase = state.get("phase")
    start = parse_time(state.get("started_at"))
    owner = state.get("owner_job_id")
    if owner is not None:
        bound = owner == job.get("id")
    else:
        job_started = parse_time(job.get("started_at"))
        bound = (phase in LIVE_STATE_PHASES + FINAL_STATE_PHASES and start is not None
                 and job_started is not None and start >= job_started)
        if bound:
            limitations.add("attempt_binding_inferred")
    if not bound:
        _stall(out, "starting", "starting")
        return
    out["attempt"] = _attempt(state.get("attempt_count"))
    active = state.get("active_stage") if isinstance(state.get("active_stage"), dict) else {}
    if phase == "running_model":
        # A new worker clears active_stage when the model child exits; the model countdown then ends.
        _model_stage(out, "pinned_handoff_model_timeout", start, config.get("timeout_seconds"), handoff.get("transcript"),
                     manifest.get("session_id"), manifest.get("worktree_path"), now, limitations, cache,
                     active=owner is None or active.get("kind") == "model")
    elif phase == "running_gates":
        out["phase"] = "gate"
        gates = manifest.get("gates")
        count = len(gates) if isinstance(gates, list) else None
        results = state.get("gate_results")
        done = len(results) if isinstance(results, list) else 0
        index = done + 1 if count is None else max(1, min(done + 1, count))
        if active.get("kind") == "gate" and active.get("index") == index:
            deadline, reason = _deadline("gate", "pinned_handoff_gate_timeout", parse_time(active.get("started_at")),
                                         config.get("gate_timeout_seconds"), now, limitations)
        else:
            deadline, reason = None, "stage_transition" if owner is not None else "gate_start_unavailable_legacy_worker"
        out.update(gate={"index": index, "count": count}, deadline=deadline, deadline_unavailable_reason=reason,
                   activity_unavailable_reason="gate_phase")
    else:
        _stall(out, "finalizing", "finalizing")


def _base(job, now):
    status = job.get("status") if isinstance(job, dict) else None
    return {"schema_version": SCHEMA_VERSION, "observed_at": stamp(now), "phase": "unknown", "phase_detail": None,
            "outcome": status if isinstance(status, str) else "unknown", "attempt": None,
            "elapsed_seconds": None, "elapsed_basis": "unavailable", "deadline": None, "deadline_unavailable_reason": None,
            "gate": None, "activity": None, "activity_unavailable_reason": None, "delegates": None, "limitations": []}


def job_progress(job, now=None, review=None, handoff=None, cache=None):
    """Build the additive progress object for one registry job dict."""
    now = now or clock()
    limitations = set()
    status, kind = job.get("status"), job.get("kind")
    created, started, finished = (parse_time(job.get(key)) for key in ("created_at", "started_at", "finished_at"))
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    out = _base(job, now)
    if status == "queued":
        out.update(elapsed_seconds=_elapsed(created, now, limitations), elapsed_basis="job_created_at" if created else "unavailable")
        _stall(out, "queued", "queued")
    elif status == "running":
        if started is not None:
            out.update(elapsed_seconds=_elapsed(started, now, limitations), elapsed_basis="job_started_at")
        elif created is not None:
            out.update(elapsed_seconds=_elapsed(created, now, limitations), elapsed_basis="job_created_at")
        if kind == "review":
            _running_review(out, now, review, limitations, cache)
        elif kind == "implementation":
            _running_implementation(out, job, now, handoff, limitations, cache)
        else:
            _stall(out, "unknown", "unknown_job_kind")
    elif isinstance(status, str):
        awaiting = kind == "implementation" and result.get("phase") == "awaiting_astra_review"
        _stall(out, "awaiting_review" if awaiting else "terminal", "awaiting_product_review" if awaiting else "terminal")
        origin = started or created
        if finished is not None and origin is not None:
            out.update(elapsed_seconds=_elapsed(origin, finished, limitations), elapsed_basis="frozen_at_finish")
        out["attempt"] = _attempt(result.get("attempt_count"))
    out["limitations"] = sorted(limitations)
    return out


def unavailable(job, now=None):
    """Progress placeholder when observation itself failed; the job is never altered."""
    out = _base(job, now or clock())
    _stall(out, "unknown", "progress_unavailable")
    out["limitations"] = ["progress_unavailable"]
    return out
