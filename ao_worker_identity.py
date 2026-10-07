"""W3: served native worker identity observed against each request's frozen expectation.

Bounded, read-only, local. For one retained delegation-capable request carrying
``native_worker_expectations`` this consumer authenticates that field through the reviewed W2
consumer (``ao_model_boundaries.read``) before any legacy absent-field path is chosen, binds the
parent source to the same complete parent transcript digest the parent native outcome used, reuses
``RecordScanner``/``parent_interval``/``launch_candidates`` to prove this request's own interval
and every in-interval Agent/Task launch, reads each resolved child source inside the certified
``subagents`` listing through the bounded no-follow descriptor reader, and requires exactly one
attributable non-synthetic assistant stop-row model id to equal the expectation frozen for that
launch's pinned role before qualified success.

Nothing here sends, writes room state, repairs evidence, calls a model, or claims provider
availability, entitlement, effective effort or a served id beyond the retained bytes. A worker
identity gap is its own structured evidence: it never rewrites the parent native outcome, its
provider/quota lanes, its refusals, its truncation or its original digests, and a genuinely absent
legacy field keeps its old reader contract with no new outcome key at all.

Zero in-interval launches are reported as zero observed executions, never as an observation that
both enabled workers ran.
"""

import copy
import os.path
import re

import ao_delegates
import ao_evidence_audit_io as audit_io
import ao_evidence_audit_native as native
import ao_model_boundaries
import ao_native_identity
import ao_native_outcome
import ao_prompt_metrics
import ao_workflow
from room import RoomError

VERSION = 1
# The exact typed terminal child failure the core-5 parser supports: block content is the typed
# message, and the row-level structured projection is exactly its Error-prefixed form. Attempted
# model prose is never a served identity and never a provider/quota/reset proof.
API_ERROR_PREFIX = "Agent terminated early due to an API error: "
API_ERROR_RESULT_PREFIX = "Error: "
HEX64 = re.compile("[0-9a-f]{64}")
# Launch input keys outside the admitted set that could still steer what runs or what it is bound
# to: model, resume/fork/session, identity, team, tool or working-directory overrides. Any other
# unrecognized key is inert evidence -- recorded as ignored, never a reason by itself.
OVERRIDE_KEYS = frozenset({"model", "isolation", "resume", "resume_session", "resumeSessionAt",
                           "fork", "fork_session", "team", "teams", "team_name", "name", "agentId",
                           "agent_id", "session_id", "sessionId", "settings", "mcp_servers", "tools",
                           "allowed_tools", "disallowed_tools", "permission_mode", "cwd"})
COVERAGE_REASONS = frozenset(native.SOURCE_DIMENSION | native.INTERVAL_DIMENSION)
BASIS = ("Bounded read-only native observation of this request's own frozen worker expectation: the retained "
         "expectation, the same parent source digest the parent native outcome used, each in-interval Agent/Task "
         "launch and each resolved child source. Configured family intent and MAX effort stay separate from the "
         "actually observed child model id; no provider availability, entitlement or effective effort is claimed")
REASON_ORDER = (
    "worker_expectation_unexpected_request", "worker_expectation_malformed", "worker_expectation_provenance",
    "worker_expectation_unsupported", "worker_expectation_deleted", "parent_source_unbound",
    "parent_source_unavailable", "preparation_unbound", "owner_conflict", "source_binding_conflict",
    "launch_inventory_unbound", "identity_conflict", "directory_entry_limit", "child_limit", "source_missing",
    "source_unsafe", "source_active", "source_torn", "source_changed", "source_malformed",
    "launch_unsupported_task", "launch_unsupported_override", "launch_unsupported_launch",
    "launch_unsupported_background", "unknown_role", "child_identity_reused", "nested_launch_unsupported",
    "child_api_error_termination", "child_attribution_ambiguous", "child_interval_unbound", "launch_unresolved",
    "child_missing", "child_row_outside_interval", "child_model_multiple", "child_model_mismatch",
    "child_model_missing", "transcript_bytes_limit", "aggregate_bytes_limit", "record_bytes_limit",
    "record_count_limit", "record_id_limit", "tool_id_limit", "json_depth_limit", "worker_observation_refused",
    "worker_identity_unknown")


def _primary(reasons):
    for reason in REASON_ORDER:
        if reason in reasons:
            return reason
    return sorted(reasons)[0] if reasons else "worker_identity_unknown"


def _hex(value):
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def _directory_text(value):
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    size = audit_io.bounded_bytes(value)
    return (size is not None and 0 < size <= audit_io.MAX_PATH_BYTES and os.path.isabs(value)
            and os.path.normpath(value) == value)


def _limits():
    return {"bytes": audit_io.MAX_TRANSCRIPT_BYTES, "aggregate": audit_io.MAX_AGGREGATE_BYTES,
            "record": audit_io.MAX_RECORD_BYTES, "records": audit_io.MAX_RECORDS,
            "record_ids": audit_io.MAX_RECORD_IDS, "tool_ids": audit_io.MAX_TOOL_IDS,
            "depth": audit_io.MAX_JSON_DEPTH, "directory": audit_io.MAX_DIRECTORY_ENTRIES,
            "children": audit_io.MAX_CHILD_ACTORS, "child_files": audit_io.MAX_CHILD_FILES}


def _result(request_id, status, qualified, reason, reasons, **extra):
    value = {"version": VERSION, "request_id": request_id, "status": status, "qualified": bool(qualified),
             "reason": reason, "reasons": sorted(set(reasons)), "coverage": "incomplete", "expectation": None,
             "parent": None, "launches": [], "executions": [], "observed_models": [], "executed_roles": [],
             "basis": BASIS}
    value.update(extra)
    return value


def _unqualified(request_id, summary, reason, notes=()):
    reasons = set(notes)
    reasons.add(reason)
    status = "incomplete" if (reasons & COVERAGE_REASONS) else "unavailable"
    return _result(request_id, status, False, _primary(reasons), reasons, expectation=summary)


def _expectation_summary(result):
    try:
        digest_value = ao_prompt_metrics.digest(result.get("expectation"))
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        digest_value = None
    return {"request_id": result.get("request_id"), "purpose": result.get("purpose"),
            "agents": dict(result.get("agents") or {}),
            "agent_selection": copy.deepcopy(result.get("agent_selection") or {}),
            "worker_families": dict(result.get("worker_families") or {}), "effort": result.get("effort"),
            "qualification_sha256": result.get("qualification_sha256"),
            "qualification_record": result.get("qualification_record"),
            "authority": copy.deepcopy(result.get("authority")), "routing": copy.deepcopy(result.get("routing")),
            "expectation_sha256": digest_value,
            "basis": "copied configuration evidence from the reviewed W2 consumer; never an execution proof"}


def _execution(entry):
    return {"tool_use_id": entry["tool_use_id"], "launcher_uuid": entry["launcher_uuid"], "role": entry["role"],
            "agent_id": entry["agent_id"], "model": entry["model"], "source_sha256": entry["source_sha256"],
            "stop_row_uuid": entry["stop_row_uuid"], "stop_row_ids": list(entry["stop_row_ids"]),
            "interval": entry["interval"]}


def _launch_entry(candidate):
    projection = getattr(candidate, "identity", None)
    projection = projection if isinstance(projection, dict) else {}
    role = projection.get("role")
    interval = None
    if candidate.interval is not None:
        interval = {"kind": "child", "launch": getattr(candidate, "launch_timestamp", None),
                    "start": candidate.interval.start, "end": candidate.interval.end}
    entry = {"tool_use_id": getattr(candidate, "tool_id", None), "name": getattr(candidate, "name", None),
             "launcher_uuid": candidate.launch_uuid, "role": role if isinstance(role, str) else None,
             "extra_keys": sorted(projection.get("extra") or ()), "flagged": bool(projection.get("flagged")),
             "background": bool(projection.get("background")), "agent_id": candidate.agent_id,
             "interval": interval, "source_sha256": None, "model": None, "stop_row_uuid": None,
             "stop_row_ids": [], "outcome": "unresolved", "reasons": []}
    if candidate.reason is not None:
        entry["reasons"].append(candidate.reason)
    return entry


def _claimed_identities(entry, observations):
    """Every child identity this launch positively claims, globally retained for reuse checks."""
    values = []
    if isinstance(entry.get("agent_id"), str) and entry["agent_id"]:
        values.append(("agent", entry["agent_id"]))
    for item in observations:
        agent = getattr(item, "agent_id", None)
        if isinstance(agent, str) and agent:
            values.append(("agent", agent))
        structured = getattr(item, "structured", None)
        if isinstance(structured, dict):
            # An explicitly present, valid structured ``agentId`` is an actor claim independent of
            # the result's completion status or classification: a typed error result may still name
            # the child that was launched, so a later launch whose own completion claims the same
            # identity is revoked by the existing global reuse check. This stays in the existing
            # agent namespace; it is never a new alias and never a task identity.
            explicit = native.structured_agent_id(structured)
            if isinstance(explicit, str) and explicit:
                values.append(("agent", explicit))
            for key in ("taskId", "task_id"):
                task = structured.get(key)
                if isinstance(task, str) and task:
                    values.append(("task", task))
    unique = []
    for pair in values:
        if pair not in unique:
            unique.append(pair)
    return unique


def _nested_entry(candidate, parent_agent_id):
    projection = getattr(candidate, "identity", None)
    projection = projection if isinstance(projection, dict) else {}
    role = projection.get("role")
    entry = {"tool_use_id": getattr(candidate, "tool_id", None), "name": getattr(candidate, "name", None),
             "launcher_uuid": candidate.launch_uuid, "role": role if isinstance(role, str) else None,
             "extra_keys": sorted(projection.get("extra") or ()), "flagged": bool(projection.get("flagged")),
             "background": bool(projection.get("background")), "parent_agent_id": parent_agent_id,
             "agent_id": candidate.agent_id, "interval": None, "source_sha256": None, "model": None,
             "stop_row_uuid": None, "stop_row_ids": [], "outcome": "unsupported",
             "reasons": ["nested_launch_unsupported"]}
    ignored = [key for key in entry["extra_keys"] if key not in OVERRIDE_KEYS]
    if ignored:
        entry["extra_keys_ignored"] = ignored
    return entry


def _api_error_termination(entry, observations):
    """The one supported typed terminal child API failure, or None.

    Only a foreground Agent launch with a matching source assistant identity and an unshared row-level
    projection can carry it. Such evidence proves termination; it never establishes a child filename,
    a served model, a provider failure or a quota reset.
    """
    if entry["name"] != "Agent" or entry["background"]:
        return None
    for item in observations:
        if getattr(item, "classification", None) != "error" or getattr(item, "shared_projection", False):
            continue
        if getattr(item, "source_uuid", None) != entry["launcher_uuid"]:
            continue
        content = getattr(item, "content", None)
        structured = getattr(item, "structured", None)
        if not isinstance(content, str) or not isinstance(structured, str):
            continue
        if not content.startswith(API_ERROR_PREFIX) or structured != API_ERROR_RESULT_PREFIX + content:
            continue
        detail = content[len(API_ERROR_PREFIX):]
        if not detail.strip():
            continue
        return {"kind": "terminal_api_error", "error_kind": "AgentApiErrorTerminationError",
                "error_text_sha256": ao_prompt_metrics.digest(content.encode("utf-8")),
                "detail_sha256": ao_prompt_metrics.digest(detail.encode("utf-8"))}
    return None


def _scan(root, parts, scanner, collector, limits):
    try:
        opened = root.open_file(parts, limits["bytes"], "source_missing", "source_unsafe",
                                "transcript_bytes_limit")
    except audit_io.SourceError as exc:
        scanner.failure = exc.reason
        scanner.note(exc.reason)
        return None
    try:
        result = audit_io.scan_source(opened, scanner.record, collector, limits)
    except audit_io.SourceError as exc:
        scanner.failure = exc.reason
        scanner.note(exc.reason)
        return None
    if result.torn:
        scanner.note("source_torn")
    return result


def _registered_parent_root(source, native_id):
    """One bounded no-follow descriptor root for the exact registered parent source.

    The public audit may persist an owned parent copy outside the prepared config root. The parent
    bytes are read and rechecked through their own exact registered-parent descriptor root; child
    discovery below never uses this path.
    """
    transcript = source.get("transcript")
    if (not isinstance(transcript, str) or not os.path.isabs(transcript) or "\0" in transcript
            or not isinstance(native_id, str) or not native_id):
        raise audit_io.SourceError("source_unsafe")
    name = os.path.basename(transcript)
    if name != native_id + ".jsonl" or not audit_io.valid_parts((name,)):
        raise audit_io.SourceError("source_unsafe")
    directory = os.path.dirname(transcript)
    if not directory:
        raise audit_io.SourceError("source_unsafe")
    return audit_io.Root(directory, "source_missing", "source_unsafe"), (name,)


def observe_workers(directory, state, request, receipt=None, native_outcome=None):
    """Structured worker identity evidence for one retained request, or None for a genuine legacy one.

    Signature: ``observe_workers(directory, state, request, receipt=None, native_outcome=None) -> dict | None``.

    ``native_outcome`` is the parent native outcome dictionary the shared outcome path already
    established (``ao_native_outcome.inspect``); its ``source_sha256`` is the exact parent snapshot
    this consumer must reproduce. A genuine legacy absence returns literally ``None`` so old outcome
    records and their digests stay unchanged; every present, malformed, torn or uncollectable state
    returns a structured record that withholds qualified success.
    """
    if not isinstance(request, dict) or not isinstance(request.get("request_id"), str):
        return None
    request_id = request["request_id"]
    present = "native_worker_expectations" in request
    delegating = request.get("purpose") in ("implementation", "correction")
    if not present and not delegating:
        return None
    try:
        normal = ao_workflow.normal(state)
    except (RoomError, AttributeError, TypeError):
        normal = False
    if request.get("role") != "engineer" or not normal:
        if present:
            return _result(request_id, "unqualified", False, "worker_expectation_unexpected_request",
                           ["worker_expectation_unexpected_request"])
        return None
    # Provenance precedes the legacy branch. Every relevant normal engineer request is
    # authenticated through the reviewed W2 consumer before an absent field can read as genuine
    # history, including when prompt_projection is also absent. A deleted field, a deleted prompt
    # projection or a stale caller copy is a provenance failure, never authentic absence. Only the
    # reader's explicit absent result for this retained request and its own persisted receipt may
    # select the old path; a missing, malformed, non-dictionary or unexpected reader result is
    # refused, never interpreted as old history.
    try:
        expectation = ao_model_boundaries.read(directory, state, request)
    except RoomError as exc:
        reason = "worker_expectation_malformed" if present else "worker_expectation_provenance"
        return _result(request_id, "unqualified", False, reason, [reason], detail=str(exc)[:300])
    if not isinstance(expectation, dict):
        reason = "worker_expectation_malformed" if present else "worker_expectation_provenance"
        return _result(request_id, "unqualified", False, reason, [reason])
    if expectation.get("status") == "absent":
        if present:
            return _result(request_id, "unqualified", False, "worker_expectation_deleted",
                           ["worker_expectation_deleted"])
        return None
    if expectation.get("status") != "frozen":
        reason = "worker_expectation_unsupported" if present else "worker_expectation_provenance"
        return _result(request_id, "unqualified", False, reason, [reason])
    summary = _expectation_summary(expectation)
    try:
        return _collect(directory, state, request, expectation, summary, receipt, native_outcome)
    except RoomError as exc:
        return _result(request_id, "unqualified", False, "worker_observation_refused",
                       ["worker_observation_refused"], expectation=summary, detail=str(exc)[:300])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _result(request_id, "unqualified", False, "worker_observation_refused",
                       ["worker_observation_refused"], expectation=summary, detail=type(exc).__name__)


def gated_outcome(outcome, worker):
    """The parent outcome, with qualified final availability withheld when worker identity is unproven.

    Only ``final_available`` is gated: a correlated parent provider/quota failure, a refusal, an
    output truncation and every other lane keep their established precedence and settlement proof.
    """
    if not isinstance(worker, dict) or worker.get("qualified"):
        return outcome
    if not isinstance(outcome, dict) or outcome.get("kind") != "final_available":
        return outcome
    return {"kind": "unknown", "hold": True,
            "reason": "Qualified native worker identity was not established (" + str(worker.get("reason"))
                      + "); the parent result and its separate worker evidence are retained"}


def _collect(directory, state, request, expectation, summary, receipt, native_outcome):
    request_id = request["request_id"]
    notes = set()
    scan_notes = set()
    limits = _limits()
    collector = native.Collector(limits, scan_notes)
    source = state.get(ao_native_outcome.source_keys("engineer")[0])
    if not isinstance(source, dict) or source.get("session_id") != request.get("session_id"):
        return _unqualified(request_id, summary, "parent_source_unbound")
    if not isinstance(receipt, dict):
        return _unqualified(request_id, summary, "parent_source_unbound", notes)
    if isinstance(native_outcome, dict):
        parent_source = native_outcome.get("source")
        if isinstance(parent_source, dict) and parent_source != source:
            return _unqualified(request_id, summary, "parent_source_unbound", notes)
    try:
        owner = ao_native_outcome.validate_source(state, source, "engineer")
    except RoomError:
        return _unqualified(request_id, summary, "owner_conflict", notes)
    try:
        prepared = ao_delegates.validate_preparation(directory, state, request.get("session_id"),
                                                     check_routing=False)
    except RoomError:
        return _unqualified(request_id, summary, "preparation_unbound", notes)
    routing = prepared.get("routing") if isinstance(prepared, dict) else None
    workspace = prepared.get("worktree") if isinstance(prepared, dict) else None
    config_root = routing.get("claude_config_dir") if isinstance(routing, dict) else None
    if not _directory_text(config_root) or not _directory_text(workspace):
        return _unqualified(request_id, summary, "preparation_unbound", notes)
    if owner.get("workspace_path") != workspace:
        return _unqualified(request_id, summary, "owner_conflict", notes)
    try:
        root = audit_io.Root(config_root, "source_missing", "source_unsafe")
    except audit_io.SourceError as exc:
        return _unqualified(request_id, summary, exc.reason, notes)
    try:
        parent_root, parent_parts = _registered_parent_root(source, source["native_session_id"])
    except audit_io.SourceError as exc:
        root.close()
        return _unqualified(request_id, summary, exc.reason, notes)
    try:
        return _sources(root, parent_root, parent_parts, collector, limits, notes, scan_notes, request,
                        expectation, summary, source, owner, state, config_root, workspace, native_outcome,
                        receipt)
    finally:
        root.close()
        parent_root.close()


def _sources(root, parent_root, parent_parts, collector, limits, notes, scan_notes, request, expectation, summary,
             source, owner, state, config_root, workspace, native_outcome, receipt):
    request_id = request["request_id"]
    native_id = source["native_session_id"]
    agents = expectation["agents"]
    try:
        names, listing_identity, overflow = root.list_dir(("projects",), limits["directory"], "source_missing",
                                                          "source_unsafe")
    except audit_io.SourceError as exc:
        return _unqualified(request_id, summary, exc.reason, notes)
    if overflow:
        notes.add("directory_entry_limit")
    try:
        matches = root.find_exact(names, native_id + ".jsonl")
    except audit_io.SourceError as exc:
        return _unqualified(request_id, summary, exc.reason, notes)
    if len(matches) > 1:
        notes.add("identity_conflict")
        return _unqualified(request_id, summary, "identity_conflict", notes)
    required = [{"parts": ("projects",), "names": names, "identity": listing_identity, "overflow": overflow}]
    owner_sha256 = ao_prompt_metrics.digest(
        {"native_owner": {key: owner[key] for key in ao_native_identity.QUERY_COLUMNS},
         "preparation_sha256": state.get("preparation_sha256")})
    # Only the two positively stopped activity states are stopped; every other value -- working,
    # unknown, null, an unrecognized string or a changed owner -- remains active and cannot
    # produce a qualified worker proof.
    active = owner.get("activity_state") not in ("idle", "exited")
    if active:
        notes.add("source_active")
    parent_scan = native.RecordScanner(collector, native.actor_digest(owner_sha256, "parent", None), "parent",
                                       None, native_id, workspace, workspace, identity=True)
    parent_result = _scan(parent_root, parent_parts, parent_scan, collector, limits)
    notes.update(scan_notes & COVERAGE_REASONS)
    if parent_result is None:
        reason = parent_scan.failure or "source_missing"
        notes.add(reason)
        return _unqualified(request_id, summary, reason, notes)
    if parent_result.torn:
        notes.add("source_torn")
    if not isinstance(native_outcome, dict) or native_outcome.get("unknown") \
            or not _hex(native_outcome.get("source_sha256")):
        notes.add("parent_source_unavailable")
        return _unqualified(request_id, summary, "parent_source_unavailable", notes)
    if parent_result.digest != native_outcome["source_sha256"]:
        notes.add("source_changed")
        return _unqualified(request_id, summary, "source_changed", notes)
    interval, interval_reasons = native.parent_interval(parent_scan, request, receipt, allow_terminal=not active)
    notes.update(interval_reasons)
    if interval is None:
        return _unqualified(request_id, summary, _primary(notes), notes)
    candidates = native.launch_inventory(parent_scan, interval, strict_identity=True)
    if candidates is None:
        notes.add("launch_inventory_unbound")
        return _unqualified(request_id, summary, "launch_inventory_unbound", notes)
    scanned = [{"parts": parent_parts, "result": parent_result, "scan": parent_scan, "root": parent_root}]
    launches, claims = [], {}
    admitted_agents, admitted_files = set(), set()
    subagents = None
    lineage_bindings = None
    lineage_seen = None
    for candidate in candidates:
        entry = _launch_entry(candidate)
        launches.append(entry)
        for pair in _claimed_identities(entry, parent_scan.results.get(entry["tool_use_id"], ())):
            claims.setdefault(pair, []).append(entry)
        if candidate.reason == "child_attribution_ambiguous":
            entry["outcome"] = "unresolved"
            continue
        if _api_error_termination(entry, parent_scan.results.get(entry["tool_use_id"], ())) is not None:
            entry["outcome"] = "terminated_unqualified"
            entry["reasons"] = ["child_api_error_termination"]
            continue
        if entry["name"] == "Task":
            entry["outcome"] = "unsupported"
            entry["reasons"].append("launch_unsupported_task")
            continue
        if entry["extra_keys"]:
            if any(key in OVERRIDE_KEYS for key in entry["extra_keys"]):
                entry["reasons"].append("launch_unsupported_override")
            ignored = [key for key in entry["extra_keys"] if key not in OVERRIDE_KEYS]
            if ignored:
                # Recorded inert input: Claude Code does not present these keys to the launch
                # guard and the audit never acts on them; the child's own evidence decides.
                entry["extra_keys_ignored"] = ignored
        if entry["flagged"]:
            entry["reasons"].append("launch_unsupported_launch")
        if entry["background"]:
            entry["reasons"].append("launch_unsupported_background")
        if not isinstance(entry["role"], str) or entry["role"] not in agents:
            entry["reasons"].append("unknown_role")
        if entry["reasons"]:
            entry["outcome"] = "unsupported"
            continue
        if candidate.reason is not None or candidate.agent_id is None or candidate.interval is None:
            entry["outcome"] = "unresolved"
            entry["reasons"].append(candidate.reason or "child_interval_unbound")
            continue
        if subagents is None:
            try:
                lineage_bindings = root.find_lineage(names, native_id)
            except audit_io.SourceError as exc:
                subagents = {"parts": None, "names": None, "identity": None, "overflow": False,
                             "present": False, "error": exc.reason, "project_dir": None}
            else:
                lineage_seen = [binding.parts[1] for binding in lineage_bindings]
                if len(lineage_bindings) > 1:
                    subagents = {"parts": None, "names": None, "identity": None, "overflow": False,
                                 "present": False, "error": "identity_conflict", "project_dir": None}
                elif not lineage_bindings:
                    subagents = {"parts": None, "names": None, "identity": None, "overflow": False,
                                 "present": False, "error": "child_missing", "project_dir": None}
                else:
                    parts = ("projects", lineage_seen[0], native_id, "subagents")
                    try:
                        sub_names, sub_identity, sub_overflow = root.list_dir(parts, limits["directory"],
                                                                              "source_missing", "source_unsafe")
                        subagents = {"parts": parts, "names": sub_names, "identity": sub_identity,
                                     "overflow": sub_overflow, "present": True, "error": None,
                                     "project_dir": lineage_seen[0]}
                    except audit_io.SourceError as exc:
                        subagents = {"parts": parts, "names": None, "identity": None, "overflow": False,
                                     "present": False, "error": exc.reason, "project_dir": lineage_seen[0]}
            if subagents["parts"] is not None:
                required.append(subagents)
            if subagents["overflow"]:
                notes.add("directory_entry_limit")
        if candidate.agent_id not in admitted_agents and len(admitted_agents) >= limits["children"]:
            entry["outcome"] = "unresolved"
            entry["reasons"].append("child_limit")
            notes.add("child_limit")
            continue
        filename = "agent-" + candidate.agent_id + ".jsonl"
        if filename not in admitted_files and len(admitted_files) >= limits["child_files"]:
            entry["outcome"] = "unresolved"
            entry["reasons"].append("child_limit")
            notes.add("child_limit")
            continue
        admitted_agents.add(candidate.agent_id)
        admitted_files.add(filename)
        if subagents["error"] is not None:
            reason = subagents["error"]
            if reason == "source_missing":
                reason = "child_missing"
            entry["outcome"] = "unresolved"
            entry["reasons"].append(reason)
            notes.add(reason)
            continue
        if not subagents["present"] or subagents["names"] is None or filename not in subagents["names"]:
            entry["outcome"] = "unresolved"
            entry["reasons"].append("child_missing")
            notes.add("child_missing")
            continue
        child_parts = subagents["parts"] + (filename,)
        child_scan = native.RecordScanner(collector, native.actor_digest(owner_sha256, "child", candidate.agent_id),
                                          "child", candidate.agent_id, native_id, workspace, workspace,
                                          launch_uuid=candidate.launch_uuid,
                                          parent_launch_uuids=candidate.parent_launch_uuids, identity=True)
        child_result = _scan(root, child_parts, child_scan, collector, limits)
        notes.update(scan_notes & COVERAGE_REASONS)
        if child_result is None:
            reason = child_scan.failure or "child_missing"
            if reason == "source_missing":
                reason = "child_missing"
            entry["outcome"] = "unresolved"
            entry["reasons"].append(reason)
            notes.add(reason)
            continue
        entry["source_sha256"] = child_result.digest
        scanned.append({"parts": child_parts, "result": child_result, "scan": child_scan, "root": root})
        if child_result.torn:
            entry["reasons"].append("source_torn")
            notes.add("source_torn")
        if child_scan.contradictory:
            entry["reasons"].append("child_attribution_ambiguous")
        if child_scan.record_conflict:
            entry["reasons"].append("identity_conflict")
        inner_candidates = native.launch_inventory(child_scan, candidate.interval)
        if inner_candidates is None:
            entry["reasons"].append("launch_inventory_unbound")
            notes.add("launch_inventory_unbound")
        else:
            for inner in inner_candidates:
                launches.append(_nested_entry(inner, candidate.agent_id))
                notes.add("nested_launch_unsupported")
        if entry["reasons"]:
            entry["outcome"] = "unresolved"
            continue
        models, outside = {}, 0
        for row in child_scan.stop_rows:
            if row.model is None or row.model == "<synthetic>" or row.stop_reason is None:
                continue
            if getattr(row, "api_error", False) or getattr(row, "synthetic", False):
                # An API-error or synthetic assistant row is attempted text, never an observed
                # served identity. It cannot prove a model even when it carries a model string.
                continue
            if candidate.interval.contains(row.number, row.timestamp):
                models.setdefault(row.model, []).append(row)
            else:
                outside += 1
        if outside:
            entry["reasons"].append("child_row_outside_interval")
            entry["outcome"] = "contradicted"
            continue
        if not models:
            entry["reasons"].append("child_model_missing")
            entry["outcome"] = "contradicted"
            continue
        if len(models) > 1:
            entry["reasons"].append("child_model_multiple")
            entry["observed_models"] = sorted(models)
            entry["outcome"] = "contradicted"
            continue
        model = next(iter(models))
        rows = models[model]
        entry["model"] = model
        entry["stop_row_uuid"] = rows[0].uuid
        entry["stop_row_ids"] = sorted(row.uuid for row in rows)
        if model != agents[entry["role"]]:
            entry["reasons"].append("child_model_mismatch")
            entry["expected_model"] = agents[entry["role"]]
            entry["outcome"] = "contradicted"
            continue
        entry["outcome"] = "qualified"
    for entries in claims.values():
        if len({entry["tool_use_id"] for entry in entries}) > 1:
            for entry in entries:
                if "child_identity_reused" not in entry["reasons"]:
                    entry["reasons"].append("child_identity_reused")
                if entry["outcome"] == "qualified":
                    entry["outcome"] = "unsupported"
            notes.add("child_identity_reused")
    collector.new_pass()
    for record in scanned:
        try:
            opened = record["root"].open_file(record["parts"], limits["bytes"], "source_changed", "source_changed",
                                               "source_changed")
            audit_io.rehash_source(opened, record["result"], collector, limits)
        except audit_io.SourceError as exc:
            notes.add(exc.reason)
    notes.update(scan_notes & COVERAGE_REASONS)
    for record in scanned:
        try:
            record["root"].recheck_file(record["parts"], record["result"].signature)
        except audit_io.SourceError as exc:
            notes.add(exc.reason)
    for listing in required:
        try:
            after = root.list_dir(listing["parts"], limits["directory"], "source_missing", "source_unsafe")
        except audit_io.SourceError as exc:
            notes.add(exc.reason)
            continue
        if (after[0] != listing["names"] or after[1] != listing["identity"] or after[2] != listing["overflow"]):
            notes.add("source_changed")
    if lineage_seen is not None:
        try:
            current_lineage = root.find_lineage(names, native_id)
        except audit_io.SourceError as exc:
            notes.add(exc.reason)
        else:
            try:
                if [binding.parts[1] for binding in current_lineage] != lineage_seen:
                    notes.add("source_changed")
            finally:
                for binding in current_lineage:
                    binding.close()
        if lineage_bindings and not all(binding.verify() for binding in lineage_bindings):
            notes.add("source_changed")
    try:
        if root.find_exact(names, native_id + ".jsonl") != matches:
            notes.add("source_changed")
        root.recheck_identity("source_changed")
        parent_root.recheck_identity("source_changed")
    except audit_io.SourceError as exc:
        notes.add(exc.reason)
    try:
        owner_after = ao_native_outcome.validate_source(state, source, "engineer")
    except RoomError:
        owner_after = None
    if owner_after != owner:
        notes.add("owner_conflict")
    for entry in launches:
        notes.update(entry["reasons"])
    project_dir = matches[0] if matches else None
    if subagents is not None and subagents.get("project_dir"):
        project_dir = subagents["project_dir"]
    executions = [entry for entry in launches if entry["outcome"] == "qualified"]
    coverage = "complete" if not (notes & COVERAGE_REASONS) else "incomplete"
    qualified = coverage == "complete" and all(entry["outcome"] == "qualified" for entry in launches)
    status = "qualified" if qualified else ("incomplete" if coverage == "incomplete" else "unqualified")
    return _result(request_id, status, qualified, None if qualified else _primary(notes), notes,
                   expectation=summary,
                   parent={"session_id": native_id, "source_sha256": parent_result.digest,
                           "workspace": workspace, "config_root": config_root, "project_dir": project_dir,
                           "interval": {"kind": interval.kind, "anchor_timestamp": interval.anchor_timestamp,
                                        "end_timestamp": interval.end_timestamp}},
                   coverage=coverage, launches=launches,
                   executions=[_execution(entry) for entry in executions],
                   observed_models=sorted({entry["model"] for entry in executions if entry["model"]}),
                   executed_roles=sorted({entry["role"] for entry in executions if entry["role"]}))
