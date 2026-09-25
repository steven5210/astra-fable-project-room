"""Explicit, owned-request, bounded evidence Read diagnostic (Project Room #58, version 1).

One standalone read-only diagnostic: an existing owned AO request, its explicit local AO
database opened with SQLite mode=ro, and one operator-authorized evidence root used only for
lexical path classification. It never calls a model, polls a provider, sends a continuation,
starts a monitor, compacts, changes routing, releases a hold, consumes a review allowance,
certifies acceptance, constructs the mutable Service, takes a room lock, repairs state, probes a
lifecycle, inspects provider/routing state, scans ordinary status, uses a new MCP collector, an
exporter/copy or an immutable=1 live-read workaround, or rewrites historical records.
Application database records are never written; SQLite may still perform normal WAL
shared-memory coordination, which read-only application data does not forbid.
"""

import hashlib
import os
from pathlib import Path
import re
import sqlite3

import ao_evidence_audit_io as audit_io
import ao_evidence_audit_native as native
import ao_native_identity as native_identity
import ao_prompt_metrics as prompt_metrics
from room import RoomError


LIMITATIONS = ("other_tool_access_not_counted", "lexical_paths_only", "reported_results_not_full_retrieval",
               "observed_counts_not_acceptance", "not_billing_or_quota", "bounded_time_correlation")
HEX64 = re.compile("[0-9a-f]{64}")
IDENTIFIER = re.compile("[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}")
REQUEST_ROLES = ("engineer", "reviewer")
CLAUDE_HARNESS = "claude-code"


class AuditError(Exception):
    """Invalid CLI arguments; the CLI renders this fixed code, never a message or private value."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class AuditRefusal(Exception):
    """A closed-code binding refusal rendered as the public unavailable/incomplete report."""

    def __init__(self, reason, request_sha256=None, owner_sha256=None):
        super().__init__(reason)
        self.reason = reason
        self.request_sha256 = request_sha256
        self.owner_sha256 = owner_sha256


def _digest(value, reason="request_integrity"):
    """Shared finite canonical encoding; malformed private values never escape as exception text."""
    try:
        return prompt_metrics.digest(value)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise AuditRefusal(reason) from None


def _component(value, code):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise AuditError(code)
    return value


def _absolute(value, code, *, lexical=False):
    try:
        text = os.fspath(value)
        size = audit_io.bounded_bytes(text)
        if (size is None or not 0 < size <= audit_io.MAX_PATH_BYTES
                or "\0" in text or not os.path.isabs(text)):
            raise AuditError(code)
        if lexical:
            return os.path.normpath(text)
        # Preserve source spelling until the no-follow descriptor walk. In particular,
        # normpath must not erase a linked component followed by '..'.
        if any(part in (".", "..") for part in text.split("/")):
            raise AuditError(code)
        return text
    except (ValueError, TypeError, UnicodeError):
        raise AuditError(code) from None


def _text(value, maximum=None):
    if not isinstance(value, str) or not value:
        return False
    size = audit_io.bounded_bytes(value)
    if size is None:
        return False
    return size <= (audit_io.MAX_IDENTITY_BYTES if maximum is None else maximum)


def _nonblank(value):
    return isinstance(value, str) and bool(value.strip()) and _text(value)


def _hex64(value):
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def _native_id(request):
    value = prompt_metrics._native_id(request)
    return value if _nonblank(value) else None


def _projection_binding(request):
    """Use the actual #56 consumer contract, including its saved owner/text checks."""
    try:
        return prompt_metrics.receipt_projection_sha256(request)
    except (RoomError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return None


def _report(coverage, request_sha256, owner_sha256, sources, parent, children, child_coverage, reasons):
    return {"version": 1, "coverage": coverage, "request_sha256": request_sha256, "owner_sha256": owner_sha256,
            "sources": sources, "parent": parent, "children": children, "child_coverage": child_coverage,
            "reasons": sorted(set(reasons)), "redundant_read_verdict": "not_established",
            "limitations": list(LIMITATIONS)}


def _binding_json(room_root, parts, missing, unsafe, oversize, malformed):
    try:
        data = audit_io.read_binding_file(room_root, parts, audit_io.MAX_BINDING_BYTES, missing, unsafe, oversize,
                                          malformed)
        value = audit_io.parse_json(data, audit_io.MAX_JSON_DEPTH, malformed)
    except audit_io.SourceError as exc:
        raise AuditRefusal(exc.reason)
    if not isinstance(value, dict):
        raise AuditRefusal(malformed)
    return value


def _validate_request(request, request_id):
    if not isinstance(request, dict) or request.get("request_id") != request_id:
        raise AuditRefusal("request_integrity")
    if request.get("role") not in REQUEST_ROLES:
        raise AuditRefusal("request_integrity")
    harness = request.get("harness")
    if not _text(harness):
        raise AuditRefusal("request_integrity")
    if harness != CLAUDE_HARNESS:
        raise AuditRefusal("unsupported_harness")
    for name in ("session_id", "model", "reasoning_effort", "turn_id", "conversation_id", "branch_id"):
        if not _nonblank(request.get(name)):
            raise AuditRefusal("request_integrity")
    text = request.get("text")
    if not isinstance(text, str) or not _hex64(request.get("text_sha256")):
        raise AuditRefusal("request_integrity")
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        raise AuditRefusal("request_integrity") from None
    if hashlib.sha256(raw).hexdigest() != request["text_sha256"]:
        raise AuditRefusal("request_integrity")
    if not native.finite_seconds(request.get("created_at")):
        raise AuditRefusal("request_integrity")
    provider_id = _native_id(request)
    if provider_id is None:
        raise AuditRefusal("request_integrity")
    if request.get("model_reroute"):
        raise AuditRefusal("request_integrity")
    observed = request.get("observed_turn")
    if observed is not None:
        if (not isinstance(observed, dict) or observed.get("id") != request["turn_id"]
                or observed.get("providerTurnId") != provider_id):
            raise AuditRefusal("request_integrity")
    baseline = request.get("baseline")
    if (not isinstance(baseline, dict) or baseline.get("conversation_id") != request["conversation_id"]
            or baseline.get("branch_id") != request["branch_id"]
            or not isinstance(baseline.get("turn_ids"), list)
            or any(not _nonblank(item) for item in baseline["turn_ids"])
            or len(set(baseline["turn_ids"])) != len(baseline["turn_ids"])):
        raise AuditRefusal("request_integrity")
    if "prompt_projection" in request and _projection_binding(request) is None:
        raise AuditRefusal("request_integrity")


def _select_request(state, request_id):
    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise AuditRefusal("request_unbound")
    candidate = requests.get(request_id)
    if not isinstance(candidate, dict):
        raise AuditRefusal("request_unbound")
    return candidate


def _consistent_reroute(reroute, request, provider_id):
    if reroute is None:
        return True
    if not isinstance(reroute, dict):
        return False
    if (reroute.get("toModel") != request.get("model")
            and (not reroute.get("providerTurnId") or reroute.get("providerTurnId") == provider_id)):
        return False
    return True


def _read_receipt(room_root, request_id, request):
    pointer = request.get("receipt")
    if not isinstance(pointer, str) or not pointer:
        raise AuditRefusal("receipt_missing")
    parts = tuple(pointer.split("/"))
    if (len(parts) != 3 or parts[0] != "receipts" or parts[1] != request_id or not parts[2].endswith(".json")
            or not _hex64(parts[2][:-5])):
        raise AuditRefusal("receipt_integrity")
    receipt = _binding_json(room_root, parts, "receipt_missing", "receipt_integrity", "binding_bytes_limit",
                            "receipt_integrity")
    observed_at = receipt.get("observed_at")
    if not native.finite_seconds(observed_at):
        raise AuditRefusal("receipt_integrity")
    payload = {key: value for key, value in receipt.items() if key != "observed_at"}
    if (_digest(payload, "receipt_integrity") != parts[2][:-5]
            or _digest(receipt, "receipt_integrity") != request.get("receipt_sha256")):
        raise AuditRefusal("receipt_integrity")
    provider_id = _native_id(request)
    turn = receipt.get("turn")
    if (not isinstance(turn, dict) or turn.get("id") != request.get("turn_id")
            or turn.get("providerTurnId") != provider_id or provider_id is None):
        raise AuditRefusal("receipt_integrity")
    settings = receipt.get("settings")
    if (not isinstance(settings, dict) or settings.get("model") != request.get("model")
            or settings.get("reasoningEffort") != request.get("reasoning_effort")):
        raise AuditRefusal("receipt_integrity")
    if not _consistent_reroute(receipt.get("modelReroute"), request, provider_id):
        raise AuditRefusal("receipt_integrity")
    messages = receipt.get("messages")
    if not isinstance(messages, list):
        raise AuditRefusal("receipt_integrity")
    baseline_turns = set(request["baseline"]["turn_ids"])
    matches = 0
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") != "user"
                or message.get("turnId") != request["turn_id"] or message.get("turnId") in baseline_turns):
            continue
        value = message.get("text")
        if not isinstance(value, str):
            continue
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError:
            continue
        if hashlib.sha256(raw).hexdigest() == request["text_sha256"]:
            matches += 1
    if matches != 1:
        raise AuditRefusal("receipt_integrity")
    if "prompt_projection" in request:
        expected = _projection_binding(request)
        if expected is None or receipt.get("prompt_projection_sha256") != expected:
            raise AuditRefusal("receipt_integrity")
    elif "prompt_projection_sha256" in receipt:
        raise AuditRefusal("receipt_integrity")
    return receipt


def _preparation_failure(condition):
    if not condition:
        raise AuditRefusal("preparation_unbound")


def _same(left, right):
    """Exact JSON identity: boolean, integer and floating-point values remain distinct."""
    return _digest(left, "preparation_unbound") == _digest(right, "preparation_unbound")


def _saved_record(root, relative, expected):
    _preparation_failure(isinstance(relative, str) and _hex64(expected))
    parts = tuple(relative.split("/"))
    _preparation_failure(audit_io.valid_parts(parts))
    value = _binding_json(root, parts, "preparation_unbound", "preparation_unbound", "binding_bytes_limit",
                          "preparation_unbound")
    _preparation_failure(_digest(value, "preparation_unbound") == expected)
    return value


def _preparation_record(root, relative, expected, delegate_sha256, room_id):
    _preparation_failure(_hex64(delegate_sha256))
    prepared = _saved_record(root, relative, expected)
    _preparation_failure(prepared.get("room_id") == room_id
                         and prepared.get("delegate_sha256") == delegate_sha256
                         and prepared.get("provider") in ("deepseek", "none", "qwen"))
    routing = prepared.get("routing")
    _preparation_failure(isinstance(routing, dict) and type(routing.get("version")) is int
                         and routing["version"] in (1, 2))
    for value in (routing.get("claude_config_dir"), prepared.get("worktree")):
        size = audit_io.bounded_bytes(value)
        _preparation_failure(size is not None and 0 < size <= audit_io.MAX_PATH_BYTES
                             and "\0" not in value and os.path.isabs(value)
                             and os.path.normpath(value) == value)
    return prepared


def _preserved_workspace(before, after):
    _preparation_failure(before["worktree"] == after["worktree"]
                         and before["routing"]["claude_config_dir"] == after["routing"]["claude_config_dir"])


def _bound_role(request, bindings):
    binding = bindings.get(request["role"]) if isinstance(bindings, dict) else None
    _preparation_failure(isinstance(binding, dict))
    fields = ("session_id", "harness", "model", "reasoning_effort", "conversation_id", "branch_id")
    _preparation_failure(all(isinstance(binding.get(key), str) and binding[key] == request[key] for key in fields))


def _transition_preparations(root, state, request):
    """Consume the recorded identity subchain only; never validate live provider/configuration state.

    All reads use the diagnostic's 8MiB binding ceiling, even though an older producer allowed
    larger audit records. Provider model names describe delegate transport, not native models.
    """
    ref = state["provider_transition"]
    _preparation_failure(isinstance(ref, dict))
    identifier = ref.get("request_id")
    _preparation_failure(isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier)
                         and ref.get("state") == "committed" and type(ref.get("epoch")) is int
                         and ref["epoch"] == 2
                         and ref.get("receipt") == "provider-transition/requests/" + identifier + ".json"
                         and ref.get("epoch_record") == "provider-transition/epochs/2/epoch.json")
    record = _saved_record(root, ref["receipt"], ref.get("receipt_sha256"))
    inputs = record.get("inputs")
    _preparation_failure(isinstance(inputs, dict) and inputs.get("request_id") == identifier
                         and record.get("room_id") == state["room_id"]
                         and ref.get("key") == _digest(inputs, "preparation_unbound")
                         and type(record.get("epoch")) is int and record["epoch"] == 2)
    epoch = _saved_record(root, ref["epoch_record"], ref.get("epoch_sha256"))
    _preparation_failure(record.get("epoch_sha256") == ref["epoch_sha256"]
                         and epoch.get("room_id") == state["room_id"]
                         and type(epoch.get("epoch")) is int and epoch["epoch"] == 2
                         and epoch.get("request_id") == identifier
                         and epoch.get("audit_sha256") == inputs.get("audit_sha256")
                         and record.get("audit_sha256") == inputs.get("audit_sha256"))
    audit_sha = inputs.get("audit_sha256")
    _preparation_failure(_hex64(audit_sha))
    saved = _saved_record(root, "provider-transition/audits/" + audit_sha + ".json", audit_sha)
    evidence = saved.get("evidence")
    _preparation_failure(isinstance(evidence, dict) and evidence.get("room_id") == state["room_id"]
                         and _hex64(evidence.get("state_sha256"))
                         and record.get("before_state_sha256") == evidence["state_sha256"]
                         and epoch.get("before_state_sha256") == evidence["state_sha256"])
    for field in ("spec", "agreement", "handoff", "candidate", "native", "reviewer_native", "engineer",
                  "reviewer", "accounting", "original_launch", "shared_registration_rooms"):
        _preparation_failure(field in evidence and field in epoch and _same(evidence[field], epoch[field]))
    _preparation_failure(_same(record.get("original_launch"), epoch["original_launch"]))
    source, target = epoch.get("source"), epoch.get("target")
    prior_preparation, prior_delegate = evidence.get("preparation"), evidence.get("delegate")
    _preparation_failure(all(isinstance(value, dict) for value in (source, target, prior_preparation, prior_delegate)))
    original_delegate = prior_delegate.get("record")
    _preparation_failure(isinstance(original_delegate, dict)
                         and _digest(original_delegate, "preparation_unbound") == prior_delegate.get("sha256")
                         and prior_delegate["sha256"] == source.get("delegate_sha256")
                         and source.get("preparation") == "preparation.json"
                         and prior_preparation.get("path") == source["preparation"]
                         and prior_preparation.get("sha256") == source.get("preparation_sha256")
                         and ref.get("original_preparation") == source["preparation"]
                         and ref.get("original_preparation_sha256") == source["preparation_sha256"]
                         and ref.get("original_delegate_sha256") == source["delegate_sha256"]
                         and target.get("preparation") == "provider-transition/epochs/2/preparation.json"
                         and record.get("preparation_sha256") == target.get("preparation_sha256")
                         and record.get("delegate_sha256") == target.get("delegate_sha256")
                         and isinstance(state.get("delegate"), dict)
                         and _digest(state["delegate"], "preparation_unbound") == target["delegate_sha256"])
    old = _preparation_record(root, source["preparation"], source["preparation_sha256"],
                              source["delegate_sha256"], state["room_id"])
    target_prepared = _preparation_record(root, target["preparation"], target["preparation_sha256"],
                                          target["delegate_sha256"], state["room_id"])
    _preparation_failure("epoch" not in old
                         and old.get("provider") == original_delegate.get("provider") == "deepseek"
                         and target_prepared.get("provider") == state["delegate"].get("provider") == "deepseek"
                         and type(target_prepared.get("epoch")) is int and target_prepared["epoch"] == 2
                         and target_prepared.get("original_preparation") == source["preparation"]
                         and target_prepared.get("original_preparation_sha256") == source["preparation_sha256"]
                         and _same(old["routing"], target_prepared["routing"]))
    _preserved_workspace(old, target_prepared)
    _bound_role(request, {role: epoch[role] for role in REQUEST_ROLES})
    return epoch, old, target_prepared


def _adopted_preparation(root, state, request, epoch, target_prepared):
    ref = state["routing_adoption"]
    _preparation_failure(isinstance(ref, dict) and ref.get("phase") == "configured")
    identifier = ref.get("request_id")
    _preparation_failure(isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier)
                         and ref.get("receipt") == "routing-adoption/requests/" + identifier + ".json")
    record = _saved_record(root, ref["receipt"], ref.get("receipt_sha256"))
    inputs = record.get("inputs")
    _preparation_failure(isinstance(inputs, dict) and inputs.get("request_id") == identifier
                         and record.get("room_id") == state["room_id"]
                         and ref.get("key") == _digest(inputs, "preparation_unbound"))
    audit_sha = inputs.get("audit_sha256")
    _preparation_failure(_hex64(audit_sha))
    saved = _saved_record(root, "routing-adoption/audits/" + audit_sha + ".json", audit_sha)
    evidence = saved.get("evidence")
    _preparation_failure(isinstance(evidence, dict) and evidence.get("room_id") == state["room_id"])
    before = evidence.get("state")
    _preparation_failure(isinstance(before, dict) and before.get("room_id") == state["room_id"]
                         and _digest(before, "preparation_unbound") == record.get("before_state_sha256")
                         and evidence.get("state_sha256") == record["before_state_sha256"]
                         and evidence.get("provider_epoch_sha256") == state["provider_transition"]["epoch_sha256"]
                         and _same(evidence.get("provider_epoch"), epoch)
                         and _same(before.get("provider_transition"), state["provider_transition"])
                         and _same(before.get("delegate"), state["delegate"])
                         and before.get("preparation") == epoch["target"]["preparation"]
                         and before.get("preparation_sha256") == epoch["target"]["preparation_sha256"]
                         and _same(evidence.get("prepared"), target_prepared)
                         and target_prepared["routing"]["version"] == 1)
    prior_requests = before.get("requests")
    _preparation_failure(isinstance(prior_requests, dict) and all(isinstance(item, dict)
                         and "provider_epoch" not in item for item in prior_requests.values()))
    _bound_role(request, before.get("bindings"))
    relative = "routing-adoption/v2/preparation.json"
    expected = record.get("target_preparation_sha256")
    _preparation_failure(state.get("preparation") == relative and state.get("preparation_sha256") == expected)
    prepared = _preparation_record(root, relative, expected, epoch["target"]["delegate_sha256"], state["room_id"])
    _preparation_failure(prepared["routing"]["version"] == 2
                         and prepared.get("provider") == target_prepared.get("provider")
                         and type(prepared.get("epoch")) is int and prepared["epoch"] == 2
                         and prepared.get("original_preparation") == target_prepared.get("original_preparation")
                         and prepared.get("original_preparation_sha256") == target_prepared.get("original_preparation_sha256"))
    _preserved_workspace(target_prepared, prepared)
    return prepared, expected


def _read_preparation(room_root, state, request):
    """Select the request's original provider epoch through immutable recorded identity chains."""
    try:
        delegate = state.get("delegate")
        _preparation_failure(isinstance(delegate, dict))
        if state.get("provider_transition") is None:
            _preparation_failure("provider_epoch" not in request and state.get("routing_adoption") is None
                                 and state.get("preparation") == "preparation.json")
            if "bindings" in state:
                _bound_role(request, state["bindings"])
            expected = state.get("preparation_sha256")
            prepared = _preparation_record(room_root, "preparation.json", expected,
                                            _digest(delegate, "preparation_unbound"), state["room_id"])
            _preparation_failure("epoch" not in prepared and prepared.get("provider") == delegate.get("provider"))
            return prepared, expected
        epoch, original, target_prepared = _transition_preparations(room_root, state, request)
        if state.get("routing_adoption") is not None:
            current, current_sha = _adopted_preparation(room_root, state, request, epoch, target_prepared)
        else:
            current, current_sha = target_prepared, epoch["target"]["preparation_sha256"]
            _preparation_failure(state.get("preparation") == epoch["target"]["preparation"]
                                 and state.get("preparation_sha256") == current_sha)
        if "provider_epoch" not in request:
            return original, epoch["source"]["preparation_sha256"]
        _preparation_failure(type(request["provider_epoch"]) is int and request["provider_epoch"] == 2)
        return current, current_sha
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise AuditRefusal("preparation_unbound") from None


def _validate_owner(owner, session_id):
    """The existing owner _validated contract, without activating its live reader."""
    if not isinstance(owner, dict) or set(owner) != set(native_identity.QUERY_COLUMNS):
        raise AuditRefusal("owner_unavailable")
    if owner["harness"] != CLAUDE_HARNESS:
        raise AuditRefusal("unsupported_harness")
    if (owner["id"] != session_id or owner["branch_session_id"] != session_id
            or type(owner["is_terminated"]) is not int or owner["is_terminated"] != 0
            or owner["session_mode"] != "chat" or owner["strategy"] not in ("", "native")
            or type(owner["replay_truncated"]) is not int or owner["replay_truncated"] != 0
            or not _nonblank(owner["controller_generation"])
            or not _text(owner["project_id"]) or not _text(owner["workspace_path"])
            or not _text(owner["ao_conversation_id"]) or not _text(owner["active_branch_id"])
            or not _canonical_provider_uuid(owner["provider_conversation_id"])
            or owner["branch_provider_conversation_id"] != owner["provider_conversation_id"]
            or not (owner["activity_state"] is None or (isinstance(owner["activity_state"], str)
                    and audit_io.bounded_bytes(owner["activity_state"]) is not None
                    and audit_io.bounded_bytes(owner["activity_state"]) <= audit_io.MAX_IDENTITY_BYTES))):
        raise AuditRefusal("owner_unavailable")


def _canonical_provider_uuid(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        import uuid
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _read_owner(database, session_id):
    """The existing explicit mode=ro native owner join with an escaped URI; no write, copy or immutable=1."""
    try:
        database_identity = audit_io.owned_file_identity(database, "owner_unavailable")
    except audit_io.SourceError as exc:
        raise AuditRefusal("owner_unavailable") from exc
    uri = Path(database).as_uri() + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        rows = connection.execute(native_identity.QUERY + " LIMIT 2", (session_id,)).fetchmany(2)
    except sqlite3.Error as exc:
        raise AuditRefusal("owner_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()
    if len(rows) != 1:
        raise AuditRefusal("owner_unavailable")
    owner = dict(rows[0])
    _validate_owner(owner, session_id)
    try:
        if audit_io.owned_file_identity(database, "owner_unavailable") != database_identity:
            raise AuditRefusal("owner_unavailable")
    except audit_io.SourceError:
        raise AuditRefusal("owner_unavailable") from None
    return owner, database_identity


def _compare_owner(owner, state, request, prepared):
    baseline = request["baseline"]
    if (owner["project_id"] != state.get("ao_project_id")
            or owner["ao_conversation_id"] != baseline["conversation_id"]
            or owner["active_branch_id"] != baseline["branch_id"]
            or owner["workspace_path"] != prepared["worktree"]):
        raise AuditRefusal("owner_conflict")


def _bind(home, room, request_id, database, evidence_root):
    request_sha256 = None
    owner_sha256 = None
    room_path = os.path.join(home, "ao", "rooms", room)
    try:
        room_root = audit_io.Root(room_path, "request_unbound", "request_integrity")
    except audit_io.SourceError as exc:
        raise AuditRefusal(exc.reason)
    try:
        state = _binding_json(room_root, ("state.json",), "request_unbound", "request_integrity",
                              "binding_bytes_limit", "request_integrity")
        if state.get("room_id") != room:
            raise AuditRefusal("request_integrity")
        request = _select_request(state, request_id)
        request_sha256 = native.sha256_hex(request_id.encode("utf-8"))
        _validate_request(request, request_id)
        receipt = _read_receipt(room_root, request_id, request)
        prepared, preparation_sha256 = _read_preparation(room_root, state, request)
        owner, database_identity = _read_owner(database, request["session_id"])
        _compare_owner(owner, state, request, prepared)
        owner_sha256 = _digest({"native_owner": {key: owner[key] for key in native_identity.QUERY_COLUMNS},
                                "preparation_sha256": preparation_sha256})
        try:
            room_root.recheck_bindings("request_integrity")
        except audit_io.SourceError as exc:
            raise AuditRefusal(exc.reason) from None
    except AuditRefusal as refusal:
        room_root.close()
        raise AuditRefusal(refusal.reason, refusal.request_sha256 or request_sha256,
                           refusal.owner_sha256 or owner_sha256) from None
    except BaseException:
        room_root.close()
        raise
    return {"room_root": room_root, "preparation_sha256": preparation_sha256, "home": home, "room": room, "request_id": request_id, "database": database,
            "evidence_root": evidence_root, "request": request, "receipt": receipt, "prepared": prepared,
            "owner": owner, "owner_sha256": owner_sha256, "request_sha256": request_sha256,
            "native_session_id": owner["provider_conversation_id"],
            "workspace": prepared["worktree"],
            "config_root": prepared["routing"]["claude_config_dir"],
            "evidence_norm": os.path.normpath(evidence_root),
            "database_identity": database_identity,
            "source_active": owner["activity_state"] != "idle"}


def _limits():
    return {"bytes": audit_io.MAX_TRANSCRIPT_BYTES, "aggregate": audit_io.MAX_AGGREGATE_BYTES,
            "record": audit_io.MAX_RECORD_BYTES, "records": audit_io.MAX_RECORDS,
            "record_ids": audit_io.MAX_RECORD_IDS, "tool_ids": audit_io.MAX_TOOL_IDS,
            "depth": audit_io.MAX_JSON_DEPTH, "directory": audit_io.MAX_DIRECTORY_ENTRIES,
            "children": audit_io.MAX_CHILD_ACTORS, "child_files": audit_io.MAX_CHILD_FILES}


def _unavailable(binding, reason, notes=None):
    reasons = set(notes or ())
    reasons.add(reason)
    return _report("unavailable", binding["request_sha256"], binding["owner_sha256"], [], None, [], "unavailable",
                   reasons)


def _scan(collector, root, parts, scanner, limits):
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


def _list_subagents(config_root, project_dir, native_id, limits):
    state = {"parts": ("projects", project_dir, native_id, "subagents"), "present": False, "identity": None, "names": None,
             "overflow": False}
    try:
        names, identity, overflow = config_root.list_dir(state["parts"], limits["directory"], "source_missing",
                                                         "source_unsafe")
    except audit_io.SourceError:
        return state
    state.update(present=True, identity=identity, names=names, overflow=overflow)
    return state


def _recheck_listing(config_root, state, limits, collector):
    if not state["present"]:
        try:
            config_root.list_dir(state["parts"], limits["directory"], "source_missing", "source_changed")
        except audit_io.SourceError as exc:
            if exc.reason != "source_missing":
                collector.note("source_changed")
                return True
        else:
            collector.note("source_changed")
            return True
        return False
    try:
        names, identity, overflow = config_root.list_dir(state["parts"], limits["directory"], "source_changed",
                                                         "source_changed")
    except audit_io.SourceError:
        collector.note("source_changed")
        return True
    if overflow != state["overflow"] or identity != state["identity"] or names != state["names"]:
        collector.note("source_changed")
        return True
    return False


def _note_scanners(collector, records, reason):
    """Shared identity/discovery evidence affects each source whose authority depends on it."""
    collector.note(reason)
    for record in records:
        record["scan"].note(reason)


def _descends_from(index, ancestor, admitted):
    seen = set()
    while index is not None and index not in seen:
        seen.add(index)
        if index == ancestor:
            return True
        index = admitted[index]["parent"]
    return False


def _collect(binding):
    notes = set()
    limits = _limits()
    collector = native.Collector(limits, notes)
    try:
        config_root = audit_io.Root(binding["config_root"], "source_missing", "source_unsafe")
    except audit_io.SourceError as exc:
        notes.add(exc.reason)
        return _unavailable(binding, exc.reason, notes)
    try:
        return _collect_sources(binding, collector, limits, notes, config_root)
    finally:
        config_root.close()


def _collect_sources(binding, collector, limits, notes, config_root):
    parent_actor = native.actor_digest(binding["owner_sha256"], "parent", None)
    native_id = binding["native_session_id"]
    try:
        names, listing_identity, overflow = config_root.list_dir(("projects",), limits["directory"],
                                                                 "source_missing", "source_unsafe")
    except audit_io.SourceError as exc:
        notes.add(exc.reason)
        return _unavailable(binding, exc.reason, notes)
    if overflow:
        notes.add("directory_entry_limit")
    try:
        matches = config_root.find_exact(names, native_id + ".jsonl")
    except audit_io.SourceError as exc:
        notes.add(exc.reason)
        return _unavailable(binding, exc.reason, notes)
    if not matches:
        notes.add("source_missing")
        return _unavailable(binding, "source_missing", notes)
    if len(matches) > 1:
        notes.add("identity_conflict")
        return _unavailable(binding, "identity_conflict", notes)
    project_dir = matches[0]
    required_listings = [{"parts": ("projects",), "present": True, "identity": listing_identity, "names": names,
                          "overflow": overflow}]
    parent_scan = native.RecordScanner(collector, parent_actor, "parent", None, native_id, binding["workspace"],
                                       binding["evidence_norm"])
    if binding["source_active"]:
        parent_scan.note("source_active")
    parent_parts = ("projects", project_dir, native_id + ".jsonl")
    parent_result = _scan(collector, config_root, parent_parts, parent_scan, limits)
    if parent_result is None:
        return _unavailable(binding, parent_scan.failure or "source_missing", notes)
    scanned = [{"root": config_root, "parts": parent_parts, "result": parent_result, "scan": parent_scan}]
    interval, interval_reasons = native.parent_interval(parent_scan, binding["request"], binding["receipt"],
                                                        allow_terminal=not binding["source_active"])
    for reason in interval_reasons:
        parent_scan.note(reason)
    admitted = []
    child_problems = set()
    by_agent = {}
    subagents_state = None
    if interval is not None:
        queue = [(parent_scan, interval, None)]
        head = 0
        stop_children = False
        while head < len(queue) and not stop_children:
            scan, scope, parent_index = queue[head]
            head += 1
            if parent_index is not None and any(entry["ambiguous"] and _descends_from(parent_index, index, admitted)
                                                for index, entry in enumerate(admitted)):
                continue
            for candidate in native.launch_candidates(scan, scope):
                if candidate.agent_id is None:
                    child_problems.add(candidate.reason)
                    collector.note(candidate.reason)
                    continue
                if candidate.agent_id in by_agent:
                    index = by_agent[candidate.agent_id]
                    admitted[index]["ambiguous"] = True
                    child_problems.add("child_attribution_ambiguous")
                    collector.note("child_attribution_ambiguous")
                    continue
                if len(admitted) >= limits["children"]:
                    child_problems.add("child_limit")
                    collector.note("child_limit")
                    stop_children = True
                    break
                index = len(admitted)
                by_agent[candidate.agent_id] = index
                entry = {"actor_sha256": native.actor_digest(binding["owner_sha256"], "child", candidate.agent_id),
                         "agent_id": candidate.agent_id, "interval": candidate.interval, "scan": None,
                         "result": None, "ambiguous": False, "parent": parent_index, "reason": candidate.reason}
                admitted.append(entry)
                if candidate.interval is None:
                    child_problems.add(candidate.reason)
                    collector.note(candidate.reason)
                    continue
                if subagents_state is None:
                    subagents_state = _list_subagents(config_root, project_dir, native_id, limits)
                    required_listings.append(subagents_state)
                    if subagents_state["overflow"]:
                        child_problems.add("directory_entry_limit")
                        collector.note("directory_entry_limit")
                child_scan = native.RecordScanner(collector, entry["actor_sha256"], "child", candidate.agent_id,
                                                  native_id, binding["workspace"], binding["evidence_norm"])
                child_parts = ("projects", project_dir, native_id, "subagents", "agent-" + candidate.agent_id + ".jsonl")
                child_result = _scan(collector, config_root, child_parts, child_scan, limits)
                if child_result is None:
                    reason = child_scan.failure
                    if reason == "source_missing":
                        reason = "child_missing"
                    entry["reason"] = reason
                    child_problems.add(reason)
                    collector.note(reason)
                    continue
                entry["scan"] = child_scan
                entry["result"] = child_result
                scanned.append({"root": config_root, "parts": child_parts, "result": child_result, "scan": child_scan})
                if child_scan.contradictory:
                    entry["ambiguous"] = True
                    entry["reason"] = "child_attribution_ambiguous"
                    child_problems.add("child_attribution_ambiguous")
                    collector.note("child_attribution_ambiguous")
                    continue
                queue.append((child_scan, candidate.interval, index))
    for index, entry in enumerate(admitted):
        if not entry["ambiguous"]:
            continue
        for other_index in range(len(admitted)):
            if _descends_from(other_index, index, admitted):
                admitted[other_index]["ambiguous"] = True
    collector.new_pass()
    for record in scanned:
        try:
            opened = record["root"].open_file(record["parts"], limits["bytes"], "source_changed", "source_changed",
                                              "source_changed")
            audit_io.rehash_source(opened, record["result"], collector, limits)
        except audit_io.SourceError as exc:
            collector.note(exc.reason)
            record["scan"].note(exc.reason)
            # Each remaining source still needs its own bounded second-pass
            # proof (or refusal) before its group may claim complete coverage.
    owner_changed = True
    try:
        owner, database_identity = _read_owner(binding["database"], binding["request"]["session_id"])
        owner_changed = (owner != binding["owner"] or database_identity != binding["database_identity"])
    except AuditRefusal:
        owner_changed = True
    if owner_changed:
        _note_scanners(collector, scanned, "owner_conflict")
        if interval is not None and interval.kind == "terminal":
            interval = None
            parent_scan.note("interval_unbound")
            child_problems.add("child_interval_unbound")
    for record in scanned:
        try:
            record["root"].recheck_file(record["parts"], record["result"].signature)
        except audit_io.SourceError as exc:
            collector.note(exc.reason)
            record["scan"].note(exc.reason)
    for state in required_listings:
        # Project discovery and the configuration root authorize all sources. The
        # subagents inventory affects child sources, not the parent's own file.
        affected = scanned if state["parts"] == ("projects",) else scanned[1:]
        if state["overflow"]:
            _note_scanners(collector, affected, "directory_entry_limit")
        if _recheck_listing(config_root, state, limits, collector):
            _note_scanners(collector, affected, "source_changed")
    try:
        if config_root.find_exact(names, native_id + ".jsonl") != matches:
            _note_scanners(collector, scanned, "source_changed")
        config_root.recheck_identity("source_changed")
    except audit_io.SourceError:
        _note_scanners(collector, scanned, "source_changed")
    parent_group = native.group_report(parent_scan, interval)
    public_children = []
    child_labels = []
    for entry in sorted(admitted, key=lambda item: item["actor_sha256"]):
        observation = None
        if not entry["ambiguous"] and entry["scan"] is not None:
            observation = native.group_report(entry["scan"], entry["interval"])
        public_children.append({"actor_sha256": entry["actor_sha256"], "observation": observation})
        child_labels.append(observation is None or observation["coverage"] != "complete")
    if parent_group["coverage"] == "unavailable":
        child_coverage = "unavailable"
    elif (child_problems or any(child_labels) or notes & (native.SOURCE_DIMENSION | native.INTERVAL_DIMENSION)
          or parent_group["source_coverage"] != "complete"
          or parent_group["interval_coverage"] != "complete"):
        child_coverage = "incomplete"
    else:
        child_coverage = "complete"
    sources = [{"source_sha256": parent_result.digest, "actor_sha256": parent_actor}]
    child_sources = [{"source_sha256": entry["result"].digest, "actor_sha256": entry["actor_sha256"]}
                     for entry in admitted if entry["result"] is not None]
    child_sources.sort(key=lambda item: item["actor_sha256"])
    sources.extend(child_sources)
    reasons = sorted(notes)
    coverage = ("complete" if parent_group["coverage"] == "complete" and child_coverage == "complete"
                and not reasons else "incomplete")
    return _report(coverage, binding["request_sha256"], binding["owner_sha256"], sources, parent_group,
                   public_children, child_coverage, notes)


def audit(home, room, request_id, database, evidence_root):
    """Standalone read-only audit; binding refusals have closed codes and no private exception text."""
    room = _component(room, "room_invalid")
    request_id = _component(request_id, "request_invalid")
    database = _absolute(database, "database_invalid")
    evidence_root = _absolute(evidence_root, "evidence_root_invalid", lexical=True)
    home = _absolute(home, "home_invalid")
    binding = None
    try:
        binding = _bind(home, room, request_id, database, evidence_root)
        result = _collect(binding)
        try:
            binding["room_root"].recheck_bindings("request_integrity")
        except audit_io.SourceError:
            raise AuditRefusal("request_integrity", binding["request_sha256"], binding["owner_sha256"]) from None
        return result
    except AuditRefusal as refusal:
        return _report("unavailable", refusal.request_sha256, refusal.owner_sha256, [], None, [], "unavailable",
                       {refusal.reason})
    finally:
        if binding is not None:
            binding["room_root"].close()
