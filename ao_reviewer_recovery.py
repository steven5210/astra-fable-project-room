"""One audited binding recovery before a native Codex reviewer has ever been used."""

import json
import os
import re
import time
from pathlib import Path

import ao_project_room as ao
import ao_workflow
from room import RoomError


def _store_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _audit_path(directory, audit_sha256):
    if not isinstance(audit_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", audit_sha256):
        raise RoomError("Use the exact saved reviewer recovery audit digest")
    return directory / "reviewer-recovery" / "audits" / (audit_sha256 + ".json")


def _saved_audit(directory, audit_sha256):
    value = ao.read(_audit_path(directory, audit_sha256))
    if ao.digest(value) != audit_sha256:
        raise RoomError("Reviewer recovery audit was modified")
    return value


def validate(service, state):
    """Verify the committed receipt before using its binding or retired claim."""
    reference = state.get("reviewer_recovery")
    directory = service.root / "rooms" / state["room_id"]
    if reference is None:
        if any((directory / "reviewer-recovery" / "requests").glob("*.json")):
            raise RoomError("An uncommitted reviewer recovery receipt exists; preserve it and diagnose before continuing")
        return None
    request_id = ao.identifier(reference["request_id"])
    relative = "reviewer-recovery/requests/" + request_id + ".json"
    if reference["receipt"] != relative:
        raise RoomError("Reviewer recovery receipt path changed")
    record = ao.read(directory / relative)
    if (ao.digest(record) != reference["receipt_sha256"] or record["room_id"] != state["room_id"]
            or record["inputs"]["request_id"] != request_id or ao.digest(record["inputs"]) != reference["key"]
            or record["replacement"] != state["bindings"].get("reviewer")):
        raise RoomError("Committed reviewer recovery evidence or binding was modified")
    audit = _saved_audit(directory, record["inputs"]["audit_sha256"])
    if (audit["evidence"]["reviewer"] != record["original"]
            or audit["evidence"]["state_sha256"] != record["before_state_sha256"]):
        raise RoomError("Reviewer recovery audit chain changed")
    return record


def _claimed_bindings(service, state):
    result = list(state["bindings"].values())
    record = validate(service, state)
    if record:
        result.append(record["original"])
    return result


def claims(service, state):
    return [binding["session_id"] for binding in _claimed_bindings(service, state)]


def summary(service, state):
    record = validate(service, state)
    if record is None:
        return None
    return {"request_id": record["inputs"]["request_id"],
            "original_session_id": record["original"]["session_id"],
            "replacement_session_id": record["replacement"]["session_id"],
            "receipt": state["reviewer_recovery"]["receipt"],
            "receipt_sha256": state["reviewer_recovery"]["receipt_sha256"],
            "meaning": "One authorized unused-reviewer recovery; original claims and review limits retained"}


def _empty(snapshot, controller):
    materialization = snapshot.get("branchMaterialization") or {}
    sequence = snapshot.get("latestSequence")
    fork_sequence = snapshot.get("nativeForkAvailableAfterSequence")
    if (not isinstance(materialization, dict) or snapshot.get("controller") != controller or snapshot.get("mode") != "chat"
            or snapshot.get("harness") != "codex" or not snapshot.get("conversationId")
            or snapshot.get("activeBranchId") != snapshot["conversationId"] + ":root"
            or snapshot.get("branchedFromEarlierMessage") is not False
            or type(sequence) is not int or sequence != 0
            or type(fork_sequence) is not int or fork_sequence != 0
            or snapshot.get("history_truncated") is not False or snapshot.get("hasMoreBefore") is not False
            or snapshot.get("turns") != [] or snapshot.get("messages") != [] or snapshot.get("activities") != []
            or materialization.get("strategy") != "native" or materialization.get("replayTruncated") is not False
            or snapshot.get("modelReroute") is not None):
        raise RoomError("Recovery requires a positively empty, complete native root conversation with a " + controller + " controller")
    usage = snapshot.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or any(usage.get(field) is not None and
                (type(usage[field]) is not int or usage[field] != 0) for field in (*ao.COUNTERS, "contextUsed")):
            raise RoomError("Reported usage contradicts an unused reviewer")
    # MCP readiness and context-window metadata are not conversation history.
    return {key: snapshot.get(key) for key in ("sessionId", "conversationId", "activeBranchId", "settings",
                                              "controller", "latestSequence", "nativeForkAvailableAfterSequence")}


def _workspace(service, state, session_id, candidate_path):
    raw = service.client(state).request("GET", "/desktop/sessions/" + session_id + "/workspace")
    path = raw.get("workspacePath")
    if raw.get("sessionId") != session_id or not isinstance(path, str) or not Path(path).is_absolute():
        raise RoomError("Reviewer workspace identity is unavailable")
    try:
        path = ao.project_path(path)
    except OSError as exc:
        raise RoomError("Reviewer workspace is unavailable") from exc
    if str(ao.common_dir(path)) != state["git_common_dir"] or path == Path(candidate_path).resolve():
        raise RoomError("Reviewer needs a separate workspace in the room's Git repository")
    return str(path)


class _UnusedClient:
    """Use the normal identity contract with one strictly validated raw snapshot."""
    def __init__(self, client, controller):
        self.client, self.controller = client, controller

    def request(self, method, path, payload=None):
        return self.client.request(method, path, payload)

    def conversation(self, session_id):
        # No pagination/normalization: positive empty history must fit one page.
        raw = self.client.request("GET", "/sessions/" + session_id + "/conversation?limit=1")
        if not isinstance(raw, dict) or any(type(raw.get(field)) is not list
                                            for field in ("messages", "turns", "activities")):
            raise RoomError("Recovery requires explicit native history arrays in the raw AO response")
        if "history_truncated" in raw and raw["history_truncated"] is not False:
            raise RoomError("Native history reports contradictory truncation evidence")
        snapshot = {**raw, "history_truncated": raw.get("hasMoreBefore") is not False, "raw_conversation": raw}
        _empty(snapshot, self.controller)
        return snapshot


def _unused_snapshot(service, state, binding, controller):
    client = _UnusedClient(service.client(state), controller)
    snapshot = service.identity(client, state, binding)
    return _empty(snapshot, controller), snapshot


def _inspect(service, directory, state):
    service.settled(state)
    if state.get("reviewer_recovery") is not None:
        raise RoomError("The one unused-reviewer recovery for this room is already used")
    if any((directory / "reviewer-recovery" / "requests").glob("*.json")):
        raise RoomError("An uncommitted reviewer recovery receipt exists; preserve it and diagnose before continuing")
    old = state["bindings"].get("reviewer")
    if not old or old["harness"] != "codex" or old["reasoning_effort"] != "max":
        raise RoomError("Unused-reviewer recovery requires a bound native Codex reviewer at max effort")
    if state["acceptances"] or any(r.get("role") == "reviewer" or r.get("session_id") == old["session_id"]
                                   for r in state["requests"].values()):
        raise RoomError("A reviewer was already used; recovery cannot replace it or renew review attempts")
    try:
        service.quiet(state)
    except (KeyError, TypeError) as exc:
        raise RoomError("Native history is malformed; unused-reviewer eligibility is unavailable") from exc
    if ao_workflow.normal(state):
        ao_workflow.engineering_ready(service, directory, state)
    spec = service.spec(directory, state)
    checkpoint = service.checkpoint(directory, state)
    # This must be the final conversation observation of the old reviewer in
    # this pass. Ordinary quiet() accepts terminal history, which recovery cannot.
    native, snapshot = _unused_snapshot(service, state, old, "stopped")
    workspace = _workspace(service, state, old["session_id"], checkpoint["candidate_path"])
    evidence = {"room_id": state["room_id"], "state_sha256": ao.digest(state), "reviewer": old,
                "spec_revision": spec["revision"], "spec_sha256": spec["sha256"],
                "candidate_sha256": checkpoint["candidate_sha256"], "candidate_path": checkpoint["candidate_path"],
                "evidence_sha256": state["checkpoint_sha256"], "native": native, "workspace": workspace}
    return evidence, snapshot


def audit(service, room_id):
    with service.locked(room_id) as (directory, state):
        evidence, snapshot = _inspect(service, directory, state)
        value = {"evidence": evidence, "observed_snapshot": snapshot, "observed_at": time.time()}
        audit_sha256 = ao.digest(value)
        _store_once(_audit_path(directory, audit_sha256), value)
        return {"eligible": True, "audit_sha256": audit_sha256, **evidence,
                "meaning": "Current unused-reviewer evidence only; recovery rechecks it and requires explicit authorization"}


def _target(service, state, session_id, evidence):
    old = evidence["reviewer"]
    conversations = set()
    for path in (service.root / "rooms").glob("*/state.json"):
        other = ao.read(path)
        if other["ao_url"] == state["ao_url"]:
            for binding in _claimed_bindings(service, other):
                if session_id == binding["session_id"]:
                    raise RoomError("Replacement reviewer is already claimed, including retired reviewer identities")
                conversations.add(binding.get("conversation_id"))
    binding = {key: value for key, value in old.items() if key not in ("session_id", "conversation_id", "branch_id")}
    binding["session_id"] = session_id
    native, snapshot = _unused_snapshot(service, state, binding, "ready")
    if snapshot["conversationId"] in conversations:
        raise RoomError("Replacement native conversation is already claimed")
    workspace = _workspace(service, state, session_id, evidence["candidate_path"])
    if workspace == evidence["workspace"]:
        raise RoomError("Replacement must have its own reviewer workspace")
    binding.update(conversation_id=snapshot["conversationId"], branch_id=snapshot["activeBranchId"])
    return binding, native, workspace, snapshot


def _result(state, record):
    return {"recovered": True, "request_id": record["inputs"]["request_id"],
            "original_binding": record["original"], "binding": record["replacement"],
            "audit_sha256": record["inputs"]["audit_sha256"],
            "receipt": state["reviewer_recovery"]["receipt"],
            "receipt_sha256": state["reviewer_recovery"]["receipt_sha256"],
            "review_attempts_at_recovery": 0, "model_requests": 0}


def recover(service, room_id, audit_sha256, replacement_session_id, diagnosis, authorization, request_id):
    ao.identifier(request_id)
    ao.identifier(replacement_session_id)
    ao.nonempty(diagnosis, "diagnosis")
    ao.nonempty(authorization, "authorization: actual user decision")
    inputs = {"request_id": request_id, "audit_sha256": audit_sha256, "replacement_session_id": replacement_session_id,
              "diagnosis": diagnosis, "authorization": authorization}
    with service.locked(room_id) as (directory, state):
        record = validate(service, state)
        if record is not None:
            if record["inputs"] != inputs:
                raise RoomError("Reviewer recovery already belongs to another payload; only its identical result can be read")
            return _result(state, record)
        saved = _saved_audit(directory, audit_sha256)
        evidence, old_snapshot = _inspect(service, directory, state)
        if saved["evidence"] != evidence:
            raise RoomError("Reviewer recovery audit is stale; inspect the changed state or native evidence")
        replacement, native, workspace, snapshot = _target(service, state, replacement_session_id, evidence)
        # Recheck both independent surfaces after preparation, while retaining the
        # global room/claim lock. AO itself remains an externally operated system.
        repeated, _ = _inspect(service, directory, state)
        target_again = _target(service, state, replacement_session_id, evidence)
        if repeated != evidence or target_again[:3] != (replacement, native, workspace):
            raise RoomError("Reviewer recovery observations changed before commit")
        service.checkpoint(directory, state)
        record = {"room_id": room_id, "inputs": inputs, "original": evidence["reviewer"], "replacement": replacement,
                  "before_state_sha256": evidence["state_sha256"], "old_snapshot": old_snapshot,
                  "replacement_snapshot": snapshot, "replacement_workspace": workspace, "recorded_at": time.time()}
        relative = "reviewer-recovery/requests/" + request_id + ".json"
        _store_once(directory / relative, record)
        state["reviewer_recovery"] = {"request_id": request_id, "key": ao.digest(inputs),
                                      "receipt": relative, "receipt_sha256": ao.digest(record)}
        state["bindings"]["reviewer"] = replacement
        service.save(directory, state)  # Binding + recovery reference commit together.
        return _result(state, record)
