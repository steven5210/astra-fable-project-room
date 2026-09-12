"""One audited, one-time provider epoch amendment for a normal AO room: DeepInfra V4.1 Flash to official DeepSeek."""

import ast
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace

import ao_project_room as ao
import ao_delegates
import ao_workflow
import deepseek_adapter
from ao_delegate_launcher import owned_bytes
from ao_reviewer_recovery import _store_once
from implementation import candidate_snapshot
from room import RoomError


EPOCH = 2
BASE = "provider-transition"
EPOCH_DIR = BASE + "/epochs/2"
ARCHIVE_DIR = BASE + "/epochs/1"
SOURCE = {"backend": "deepinfra", "model": "deepseek-ai/DeepSeek-V4.1-Flash"}
TARGET = {"backend": "official", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
          "reasoning_effort": "max", "max_tokens": 393216, "context_tokens": 1048576}
ELIGIBLE_STATES = frozenset({deepseek_adapter.NOT_STARTED, deepseek_adapter.COMPLETED,
                            deepseek_adapter.TRUNCATED, deepseek_adapter.REJECTED})
ABANDONED = (deepseek_adapter.ABANDONED_DEADLINE, deepseek_adapter.ABANDONED_CANCELLED)
STOPPED = "stopped"
TARGET_PUBLIC = ("backend", "model", "reasoning_effort", "max_tokens", "context_tokens", "profile_sha256", "room_root")


def _read(path):
    return json.loads(owned_bytes(path))


def _audit_path(directory, audit_sha256):
    if not isinstance(audit_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", audit_sha256):
        raise RoomError("Use the exact saved provider transition audit digest")
    return directory / BASE / "audits" / (audit_sha256 + ".json")


def _saved_audit(directory, audit_sha256):
    # Up to ten individually bounded 8 MB native history pages can be retained in this private observation.
    value = json.loads(owned_bytes(_audit_path(directory, audit_sha256), 96_000_000))
    if ao.digest(value) != audit_sha256:
        raise RoomError("Provider transition audit was modified")
    return value


def _receipt_path(directory, request_id):
    return directory / BASE / "requests" / (ao.identifier(request_id) + ".json")


def _pending_receipts(directory):
    return sorted((directory / BASE / "requests").glob("*.json"))


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _place(path, data):
    if path.exists() or path.is_symlink():
        if owned_bytes(path) != data:
            raise RoomError("Provider transition epoch file is corrupted: " + path.name + "; the transition stays blocked")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _ledger_rows(home, room_id):
    path = Path(home) / "deepseek" / "ledger.sqlite3"
    if not path.exists() and not path.is_symlink():
        return False, []
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("Unsafe ledger path")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT id,request_id,state,profile_sha256,requested_model,created_at,finished_at,"
                              "possibly_billed,content_sha256,content_path FROM jobs WHERE room_id=? ORDER BY created_at,id",
                              (room_id,)).fetchall()
            resolved = {r[0] for r in db.execute("SELECT job_id FROM resolutions WHERE room_id=?", (room_id,))}
        finally:
            db.close()
        return True, [{**dict(row), "possibly_billed": bool(row["possibly_billed"]), "resolved": row["id"] in resolved}
                      for row in rows]
    except (sqlite3.Error, OSError) as exc:
        raise RoomError("Delegate ledger is unavailable; completion cannot be established") from exc


def _ledger_check(rows):
    for row in rows:
        job, state = row["id"], row["state"]
        if state in deepseek_adapter.ACTIVE_STATES:
            raise RoomError("Active delegate job " + job + " (" + state + ") stops the transition; observe it, never replay or self-resolve")
        if state in deepseek_adapter.STOP_STATES and not row["resolved"]:
            raise RoomError("Unresolved delegate job " + job + " (" + state + ") stops the transition; only the user's terminal resolution lifts it")
        if state in ABANDONED:
            raise RoomError("Abandoned delegate job " + job + " (" + state + ") may still be running or billing remotely; the transition refuses rather than bypassing it")
        if state not in ELIGIBLE_STATES and state not in deepseek_adapter.STOP_STATES:
            raise RoomError("Delegate job " + job + " has an unrecognized state")


def _inventory_profiles(inventory):
    files = inventory["files"]
    adapters = [p for p in files if isinstance(p, str) and p.endswith(".py")]
    configs = [p for p in files if isinstance(p, str) and p.endswith(".json")]
    if len(adapters) != 1 or len(configs) != 1 or len(files) != 2:
        raise RoomError("Pinned provider inventory does not name exactly one adapter snapshot and one configuration snapshot")
    return {"adapter_path": adapters[0], "adapter_sha256": files[adapters[0]], "config_path": configs[0],
            "config_sha256": files[configs[0]], "profile_sha256": ao_delegates.expected_profile(inventory)}


def _validation_slice(data, roots=None):
    """Select an AST contract and its module-owned dependencies without importing the adapter.

    The default pure validation slice alone is executable after exact comparison. A caller-supplied key-access
    slice is comparison-only: it must never be compiled or executed, nor read credential contents.
    """
    tree = ast.parse(data)
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    names = set(roots) if roots is not None else {"validate_config", "_positive_int", "reservation_bytes", "AdapterError"}
    selected = set()
    while names - selected:
        name = sorted(names - selected)[0]
        node = definitions[name]
        selected.add(name)
        names.update(child.id for child in ast.walk(node)
                     if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in definitions)
    nodes = [node for node in tree.body if any(definitions[name] is node for name in selected)]
    return ast.Module(body=nodes, type_ignores=[])


def _pinned_validator(profiles):
    data = owned_bytes(profiles["adapter_path"])
    if ao.digest(data) != profiles["adapter_sha256"]:
        raise RoomError("Pinned adapter snapshot changed before target validation")
    installed = owned_bytes(Path(deepseek_adapter.__file__).resolve())
    try:
        pinned_slice, current_slice = _validation_slice(data), _validation_slice(installed)
        canonical = ast.dump(pinned_slice, include_attributes=False)
        if canonical != ast.dump(current_slice, include_attributes=False):
            raise RoomError("Pinned adapter validation contract differs from the current validator; review its exact validation logic before the transition")
        key_roots = {"key_diagnostics", "read_api_key"}
        key_contract = ast.dump(_validation_slice(data, key_roots), include_attributes=False)
        if key_contract != ast.dump(_validation_slice(installed, key_roots), include_attributes=False):
            raise RoomError("Pinned adapter key-access contract differs from current metadata/read validation; review it before the transition")
        namespace = {"__name__": "_provider_transition_validation", "math": math, "os": os, "Path": Path, "re": re}
        exec(compile(pinned_slice, profiles["adapter_path"], "exec"), namespace)
    except (SyntaxError, KeyError, TypeError, ValueError) as exc:
        raise RoomError("Pinned adapter validation contract is unavailable") from exc
    return namespace, {"basis": "exact_pinned_validation_slice", "adapter_sha256": profiles["adapter_sha256"],
                       "installed_adapter_sha256": ao.digest(installed), "validation_sha256": ao.digest(canonical.encode()),
                       "key_access_contract_sha256": ao.digest(key_contract.encode()), "key_access_check": "comparison_only_ast",
                       "credential_check": "installed_metadata_only_with_matching_retained_contract",
                       "whole_adapter_equal": profiles["adapter_sha256"] == ao.digest(installed)}


def _target(target_input, home, profiles):
    if not isinstance(target_input, dict) or any(not isinstance(key, str) for key in target_input):
        raise RoomError("target_profile must be a key-free JSON object")
    try:
        config = deepseek_adapter.validate_config(target_input, home)
    except deepseek_adapter.AdapterError as exc:
        raise RoomError("Target profile rejected (" + exc.code + "): " + str(exc)) from exc
    for key, value in TARGET.items():
        if config.get(key) != value:
            raise RoomError("Target profile must pin the authorized official DeepSeek values: " + key + " must be " + str(value))
    namespace, validation = _pinned_validator(profiles)
    try:
        pinned = namespace["validate_config"](target_input, home)
    except namespace["AdapterError"] as exc:
        raise RoomError("Pinned target profile rejected (" + exc.code + "): " + str(exc)) from exc
    if pinned != config:
        raise RoomError("Pinned and current validators normalize the target differently")
    diagnostics = deepseek_adapter.key_diagnostics(config["api_key_file"])
    if not diagnostics["ok"]:
        raise RoomError("Official DeepSeek key file metadata is not usable (" + str(diagnostics["reason"]) + "); the secret is never read")
    return config, diagnostics, validation


def _shared_registration(service, state, prepared):
    shared = []
    for path in sorted((service.root / "rooms").glob("*/state.json")):
        other = _read(path)
        if other["room_id"] == state["room_id"] or not other.get("preparation"):
            continue
        if (other.get("delegate") or {}).get("provider") != "deepseek":
            continue
        try:
            prior = ao_delegates.preparation(path.parent, other)
        except RoomError as exc:
            shared.append({"room_id": other["room_id"], "error": str(exc)})
            continue
        if not prior.get("registry_path") or not prior.get("registry_project"):
            continue
        if (prior["registry_path"], prior["registry_project"]) == (prepared["registry_path"], prepared["registry_project"]):
            if prior["registration"] != prepared["registration"]:
                raise RoomError("Another room shares the repository-local deepseek registration with a different entry; resolve the conflict before the transition")
            shared.append({"room_id": other["room_id"], "worktree": prior["worktree"]})
    return shared


def _launch_evidence(directory, reconcile):
    live, archive = directory / "delegate-launch.json", directory / ARCHIVE_DIR / "delegate-launch.json"
    if reconcile is None:
        if archive.exists():
            raise RoomError("Provider transition archive already exists without a committed transition; diagnose before continuing")
        if live.exists():
            data = owned_bytes(live)
            value = json.loads(data)
            return {"present": True, "sha256": ao.digest(data),
                    **{key: value.get(key) for key in ("session_id", "worktree", "preparation_sha256")}}
        return {"present": False}
    expected = reconcile["original_launch"]
    if expected["present"]:
        live_ok = live.exists() and ao.digest(owned_bytes(live)) == expected["sha256"]
        archive_ok = archive.exists() and ao.digest(owned_bytes(archive)) == expected["sha256"]
        acceptable = (live_ok and (not archive.exists() or archive_ok)) or (not live.exists() and archive_ok)
    else:
        acceptable = not live.exists() and not archive.exists()
    if not acceptable:
        raise RoomError("Original launch evidence is missing or changed; the pending transition stays blocked")
    return expected


class _ReadOnlyIdentity:
    """Reuse the native identity/reroute contract without saving a contradiction during an audit."""
    identity = ao.Service.identity

    def __init__(self, service):
        self.root = service.root

    def record_reroute(self, directory, state, request, reroute):
        raise RoomError("Native model substitution contradicts the pinned identity; audit made no room mutation, preserve and diagnose it through ordinary sync")


class _CompleteClient:
    """Bounded raw history reads; ordinary pagination defaults must not turn missing arrays into empty history."""
    def __init__(self, client):
        self.client = client

    def request(self, method, path, payload=None):
        if method != "GET":
            raise RoomError("Provider audit permits AO GET observations only")
        raw = self.client.request(method, path, payload)
        if re.fullmatch(r"/sessions/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", path):
            session = raw.get("session", raw) if isinstance(raw, dict) else None
            if not isinstance(session, dict) or session.get("isTerminated") is not False:
                raise RoomError("Provider adoption requires native session isTerminated=false; terminated or unknown lifecycle refuses")
        return raw

    def conversation(self, session_id):
        path = "/sessions/" + ao.identifier(session_id) + "/conversation?limit=500"
        result, turns, messages, seen = None, {}, {}, set()
        for _ in range(10):
            page = self.request("GET", path)
            if (not isinstance(page, dict) or any(not isinstance(page.get(k), list) for k in ("turns", "messages"))
                    or type(page.get("hasMoreBefore")) is not bool):
                raise RoomError("Provider transition requires explicit complete native history arrays in the raw AO response")
            if "history_truncated" in page and page["history_truncated"] is not False:
                raise RoomError("Raw native history contains contradictory truncation evidence")
            for name, target in (("turns", turns), ("messages", messages)):
                identities = []
                for item in page[name]:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        raise RoomError("Raw native history identity is ambiguous")
                    identities.append(item["id"])
                    if item["id"] in target and ao.digest(target[item["id"]]) != ao.digest(item):
                        raise RoomError("Raw native history contains conflicting " + name + " across pages")
                    target.setdefault(item["id"], item)
                if len(set(identities)) != len(identities):
                    raise RoomError("Raw native history contains duplicate identities")
            if result is None:
                result = dict(page)
            elif any(ao.digest(page.get(k)) != ao.digest(result.get(k))
                     for k in ("sessionId", "conversationId", "activeBranchId", "controller", "settings", "branchMaterialization")):
                raise RoomError("Native conversation identity changed during bounded history observation")
            if not page["hasMoreBefore"]:
                result.update(turns=list(turns.values()), messages=sorted(messages.values(), key=lambda m: m.get("sequence", 0)),
                              history_truncated=False)
                return result
            cursor = page.get("oldestSequence")
            if type(cursor) is not int or cursor <= 0 or cursor in seen:
                raise RoomError("Native history pagination is incomplete or ambiguous")
            seen.add(cursor)
            path = "/sessions/" + ao.identifier(session_id) + "/conversation?limit=500&beforeSequence=" + str(cursor)
        raise RoomError("Provider transition requires complete native history within its bounded observation window")


def _history_digest(turns, messages):
    return ao.digest({"turns": sorted(turns, key=lambda t: t["id"]), "messages": sorted(messages, key=lambda m: m["id"])})


def _native(state, binding, snapshot, directory=None):
    if snapshot.get("history_truncated") is not False:
        raise RoomError("Provider transition requires complete native history; truncated or unknown history refuses")
    if snapshot.get("controller") not in ("ready", STOPPED):
        raise RoomError("Provider transition requires a positively idle or stopped native controller")
    materialization = snapshot.get("branchMaterialization")
    if not isinstance(materialization, dict) or materialization.get("strategy") != "native":
        raise RoomError("Provider transition requires retained native materialization")
    if "replayTruncated" in materialization and materialization["replayTruncated"] is not False:
        raise RoomError("Native materialization replayTruncated must be absent or exactly false; replay truncation refuses")
    turns, messages = snapshot.get("turns"), snapshot.get("messages")
    if not isinstance(turns, list) or not isinstance(messages, list):
        raise RoomError("Provider transition requires explicit native turn and message arrays")
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("state") != "completed":
            value = turn.get("state") if isinstance(turn, dict) else None
            raise RoomError("Native history contains a turn that is not completed (state " + str(value) + "); failed, interrupted, cancelled, recovered or active work refuses")
    ids = [t.get("id") for t in turns]
    message_ids = [m.get("id") if isinstance(m, dict) else None for m in messages]
    if (any(not isinstance(i, str) or not i for i in ids + message_ids)
            or len(set(ids)) != len(ids) or len(set(message_ids)) != len(message_ids)):
        raise RoomError("Native turn or message identities are ambiguous")
    owned = {}
    for request in state["requests"].values():
        if request.get("session_id") != binding["session_id"]:
            continue
        turn_id = request.get("turn_id")
        if request.get("state") != "completed" or not turn_id or not ao.native_turn_identity(request):
            raise RoomError("Every owning native request must be known completed with native identity")
        if turn_id in owned:
            raise RoomError("Two owning requests claim the same native turn")
        owned[turn_id] = request
        if directory is not None:
            receipt = ao_workflow.completed_receipt(directory, request)
            turn = receipt.get("turn") or {}
            if (turn.get("id") != turn_id or turn.get("state") != "completed"
                    or turn.get("providerTurnId") != ao.native_turn_identity(request)
                    or ao.digest(request["text"].encode()) != request["text_sha256"]
                    or not ao.sent_message(request, receipt) or not ao.sent_message(request, snapshot)):
                raise RoomError("Owning native request differs from its completed receipt or observed message")
    if set(owned) - set(ids):
        raise RoomError("Owned native turns are missing from the observed history")
    for turn in turns:
        native_id = turn.get("providerTurnId")
        if not isinstance(native_id, str) or not native_id.strip():
            raise RoomError("Completed native turn lacks its provider identity")
        if turn["id"] in owned and native_id != ao.native_turn_identity(owned[turn["id"]]):
            raise RoomError("Observed native provider turn identity differs from the saved request")
    owned_turns = [{"turn_id": key, "provider_turn_id": ao.native_turn_identity(value), "request_id": value["request_id"]}
                   for key, value in sorted(owned.items())]
    unowned = [{"turn_id": t["id"], "provider_turn_id": t["providerTurnId"]}
               for t in sorted(turns, key=lambda t: t["id"]) if t["id"] not in owned]
    return {"session_id": binding["session_id"], "conversation_id": snapshot.get("conversationId"),
            "branch_id": snapshot.get("activeBranchId"), "model": (snapshot.get("settings") or {}).get("model"),
            "reasoning_effort": (snapshot.get("settings") or {}).get("reasoningEffort"), "turn_count": len(turns),
            "owned_turns": owned_turns, "unowned_completed_turns": unowned,
            "message_ids": sorted(message_ids), "history_sha256": _history_digest(turns, messages)}


def _inspect(service, directory, state, target_input, reconcile=None):
    service.settled(state, pending_transition=reconcile is not None)
    if not ao_workflow.normal(state):
        raise RoomError("Provider transition is for normal Fable rooms")
    if state.get("provider_transition") is not None:
        raise RoomError("The one provider transition for this room is already used")
    if reconcile is None and _pending_receipts(directory):
        raise RoomError("An uncommitted provider transition receipt exists; complete it with the identical request or diagnose it before continuing")
    delegate = state.get("delegate") or {}
    if delegate.get("provider") != "deepseek":
        raise RoomError("Provider transition requires a room pinned to the DeepSeek delegate provider")
    engineer, reviewer = state["bindings"].get("engineer"), state["bindings"].get("reviewer")
    if (not engineer or engineer.get("harness") != "claude-code" or engineer.get("model") != ao_workflow.FABLE_MODEL
            or engineer.get("reasoning_effort") != "max"):
        raise RoomError("Provider transition requires the bound native Fable engineer at max effort")
    if not reviewer:
        raise RoomError("Bind the independent reviewer before a provider transition")
    agreed = ao_workflow.agreement(service, directory, state)
    handoff = ao_workflow.handoff_record(directory, state)
    prepared = ao_delegates.validate_preparation(directory, state, engineer["session_id"], check_routing=False)
    routing = prepared.get("routing")
    if not isinstance(routing, dict) or type(routing.get("version")) is not int or routing["version"] != 1:
        raise RoomError("Provider transition requires an explicit historical v1 routing preparation for the supported v2 adoption; absent or other routing versions are ineligible")
    from ao_routing import validate_local
    validate_local(prepared)
    from ao_routing_adoption import launcher_compatibility, probe_evidence
    launcher_compatibility(service, state, prepared)
    probes = probe_evidence(service.root.parent, state["room_id"])
    actual = ao_workflow.workspace(service, directory, state)
    # assert_settled records an observed-ledger flag in its argument. The audit keeps even the saved state bytes
    # unchanged; this still performs the current missing-ledger and orphan-evidence checks against the real directory.
    ao_delegates.assert_settled(service.root.parent, dict(state), directory)
    present, rows = _ledger_rows(service.root.parent, state["room_id"])
    _ledger_check(rows)
    observer, client = _ReadOnlyIdentity(service), _CompleteClient(service.client(state))
    reviewer_snapshot = observer.identity(client, state, reviewer)
    if ao.busy(reviewer_snapshot) or reviewer_snapshot.get("controller") not in ("ready", STOPPED):
        raise RoomError("A bound AO conversation has active or unestablished work")
    reviewer_native = _native(state, reviewer, reviewer_snapshot, directory)
    snapshot = observer.identity(client, state, engineer)
    native = _native(state, engineer, snapshot, directory)
    source = ao_delegates.validate_provider(directory, state)["delegate_settings"] or {}
    if any(source.get(key) != value for key, value in SOURCE.items()):
        raise RoomError("Provider transition supports only a room pinned to DeepInfra V4.1 Flash")
    profiles = _inventory_profiles(delegate["inventory"])
    source_bytes = owned_bytes(profiles["config_path"])
    if ao.digest(source_bytes) != profiles["config_sha256"]:
        raise RoomError("Pinned source configuration changed before content verification")
    source_config = deepseek_adapter.validate_config(json.loads(source_bytes), service.root.parent)
    for row in rows:
        if row["profile_sha256"] != profiles["profile_sha256"] or row["requested_model"] != source["model"]:
            raise RoomError("Delegate ledger contains a job outside the original pinned provider profile")
        if row["state"] == deepseek_adapter.COMPLETED:
            ao_delegates.verify_content(service.root.parent, state["room_id"], delegate["inventory"]["export_dir"],
                                        row, source_config["max_content_bytes"])
    config, diagnostics, validation = _target(target_input, service.root.parent, profiles)
    config_sha256 = deepseek_adapter.sha(_json_bytes(config))
    profile_sha256 = deepseek_adapter.sha((profiles["adapter_sha256"] + config_sha256).encode("ascii"))
    shared = _shared_registration(service, state, prepared)
    original_launch = _launch_evidence(directory, reconcile)
    candidate = candidate_snapshot(actual)
    evidence = {"room_id": state["room_id"], "state_sha256": ao.digest(state),
                "spec": {"revision": agreed["spec_revision"], "sha256": agreed["spec_sha256"],
                         "spec_record_sha256": state["spec_record_sha256"]}, "agreement": agreed,
                "handoff": {"path": state["handoff"], "sha256": state["handoff_sha256"], "baseline_commit": handoff["baseline_commit"]},
                "preparation": {"path": state["preparation"], "sha256": state["preparation_sha256"],
                                **{k: prepared[k] for k in ("worktree", "launcher_path", "launcher_sha256", "registry_path",
                                                            "registry_project", "registration")},
                                "routing_files": (prepared.get("routing") or {}).get("files"),
                                "guard_sha256": (prepared.get("routing") or {}).get("guard_sha256")},
                "delegate": {"sha256": ao.digest(delegate), "record": delegate, "files": delegate["files"],
                             "policy_path": delegate["policy_path"], "policy_sha256": ao.digest(owned_bytes(delegate["policy_path"]))},
                "source": {**{k: source.get(k) for k in ("backend", "model", "base_url", "reasoning_effort", "max_tokens")}, **profiles},
                "target": {"input": target_input, "profile": config, "config_sha256": config_sha256,
                           "profile_sha256": profile_sha256, "adapter_sha256": profiles["adapter_sha256"],
                           "key_file": diagnostics, "validation": validation},
                "candidate": {"sha256": candidate["sha256"], "head": candidate["head"]}, "native": native,
                "reviewer_native": reviewer_native,
                "engineer": engineer, "reviewer": reviewer,
                "accounting": {"spec_review_attempts": sum(r.get("purpose") == "spec_review" for r in state["requests"].values()),
                               "review_attempts": sum(r.get("role") == "reviewer" for r in state["requests"].values()),
                               "requests": len(state["requests"]), "acceptances": len(state["acceptances"]),
                               "verifications": len(state["verifications"])},
                "ledger": {"present": present, "rows": rows, "count": len(rows)}, "probes": probes,
                "shared_registration_rooms": shared,
                "original_launch": original_launch}
    return evidence, {"controller": snapshot.get("controller"), "reviewer_controller": reviewer_snapshot.get("controller")}, snapshot


def audit(service, room_id, target_profile):
    with service.locked(room_id) as (directory, state):
        evidence, observed, snapshot = _inspect(service, directory, state, target_profile)
        value = {"evidence": evidence, "observed": observed, "observed_snapshot": snapshot, "observed_at": time.time()}
        audit_sha256 = ao.digest(value)
        _store_once(_audit_path(directory, audit_sha256), value)
        return {"eligible": True, "audit_sha256": audit_sha256, "room_id": state["room_id"],
                **{k: evidence[k] for k in ("spec", "handoff", "candidate", "accounting", "shared_registration_rooms")},
                "source": {k: evidence["source"][k] for k in ("backend", "model", "profile_sha256")},
                "target": {**{k: evidence["target"]["profile"][k] for k in TARGET},
                           "profile_sha256": evidence["target"]["profile_sha256"], "key_file_ok": True,
                           "validation": evidence["target"]["validation"]},
                "native": {"turn_count": evidence["native"]["turn_count"], "owned_turns": len(evidence["native"]["owned_turns"]),
                           "unowned_completed_turns": len(evidence["native"]["unowned_completed_turns"]), "controller": observed["controller"]},
                "ledger": {k: evidence["ledger"][k] for k in ("present", "count")},
                "original_launch_present": evidence["original_launch"]["present"],
                "meaning": "Current transition evidence only; the transition rechecks it under the lock, requires the engineer positively stopped and explicit authorization, and never reads the secret"}


def _bundle(service, directory, state, evidence, inputs, recorded_at):
    epoch_dir = directory / EPOCH_DIR
    config_path = epoch_dir / "deepseek.json"
    config_bytes = _json_bytes(evidence["target"]["profile"])
    if deepseek_adapter.sha(config_bytes) != evidence["target"]["config_sha256"]:
        raise RoomError("Target profile bytes drifted")
    original_settings = _read(directory / "settings.json")
    inventory = {"provider": "deepseek", "room_id": state["room_id"],
                 "export_dir": state["delegate"]["inventory"]["export_dir"], "epoch": EPOCH, "audit_sha256": inputs["audit_sha256"],
                 "files": {evidence["source"]["adapter_path"]: evidence["source"]["adapter_sha256"],
                           str(config_path): evidence["target"]["config_sha256"]}}
    settings = {**original_settings, "delegate_provider": "deepseek", "provider_inventory": inventory}
    policy_bytes = owned_bytes(state["delegate"]["policy_path"])
    original_mcp = _read(directory / "profiles" / "implementation-mcp.json")
    if set(original_mcp["mcpServers"]) != {"deepseek"}:
        raise RoomError("Original delegate server definition includes unrelated entries; preserve them and review the transition")
    server = original_mcp["mcpServers"]["deepseek"]
    args = list(server["args"])
    if (args.count("serve") != 1 or any(args.count(name) != 1 or args.index(name) + 1 >= len(args)
                                       for name in ("--room-root", "--config"))):
        raise RoomError("Original delegate server definition has an unexpected shape")
    args[args.index("--room-root") + 1] = str(epoch_dir)
    args[args.index("--config") + 1] = str(config_path)
    mcp = {"mcpServers": {"deepseek": {**server, "args": args}}}
    impl = _read(directory / "profiles" / "implementation.json")
    extra = impl.get("extra_args")
    if not isinstance(extra, list) or extra.count("--mcp-config") != 1 or extra.index("--mcp-config") + 1 >= len(extra):
        raise RoomError("Original implementation profile has an unexpected MCP configuration shape")
    extra[extra.index("--mcp-config") + 1] = str(epoch_dir / "profiles" / "implementation-mcp.json")
    files = {EPOCH_DIR + "/deepseek.json": config_bytes, EPOCH_DIR + "/settings.json": _json_bytes(settings),
             EPOCH_DIR + "/delegate-policy.txt": policy_bytes, EPOCH_DIR + "/profiles/implementation-mcp.json": _json_bytes(mcp),
             EPOCH_DIR + "/profiles/implementation.json": _json_bytes(impl)}
    new_delegate = {"provider": "deepseek", "inventory": inventory,
                    "files": {str(directory / rel): ao.digest(data) for rel, data in files.items() if rel != EPOCH_DIR + "/deepseek.json"},
                    "policy_path": str(epoch_dir / "delegate-policy.txt"), "epoch": EPOCH}
    original_prepared = _read(directory / state["preparation"])
    if original_prepared["server"] != {**server, "env": {"PROJECT_ROOM_WORKTREE": original_prepared["worktree"]}}:
        raise RoomError("Original prepared delegate server differs from its pinned MCP profile")
    new_prepared = {**original_prepared, "delegate_sha256": ao.digest(new_delegate),
                    "server": {**original_prepared["server"], "args": args, "env": {"PROJECT_ROOM_WORKTREE": original_prepared["worktree"]}},
                    "epoch": EPOCH, "original_preparation": state["preparation"],
                    "original_preparation_sha256": state["preparation_sha256"]}
    epoch_record = {"epoch": EPOCH, "room_id": state["room_id"],
                    **{k: inputs[k] for k in ("request_id", "audit_sha256", "authorization", "diagnosis", "native_stop_record")},
                    "recorded_at": recorded_at, "before_state_sha256": evidence["state_sha256"],
                    **{k: evidence[k] for k in ("spec", "agreement", "handoff", "candidate", "native", "reviewer_native", "engineer", "reviewer",
                                                "accounting", "original_launch", "shared_registration_rooms")},
                    "source": {**evidence["source"], "preparation": state["preparation"], "preparation_sha256": state["preparation_sha256"],
                               "delegate_sha256": evidence["delegate"]["sha256"], "policy_sha256": evidence["delegate"]["policy_sha256"]},
                    "target": {**{k: evidence["target"]["profile"][k] for k in TARGET},
                               "api_key_file": evidence["target"]["profile"]["api_key_file"], "config_path": str(config_path),
                               "config_sha256": evidence["target"]["config_sha256"], "adapter_path": evidence["source"]["adapter_path"],
                               "adapter_sha256": evidence["source"]["adapter_sha256"], "profile_sha256": evidence["target"]["profile_sha256"],
                               "validation": evidence["target"]["validation"], "room_root": str(epoch_dir),
                               "preparation": EPOCH_DIR + "/preparation.json", "preparation_sha256": ao.digest(new_prepared),
                               "delegate_sha256": ao.digest(new_delegate), "policy_path": new_delegate["policy_path"],
                               "policy_sha256": evidence["delegate"]["policy_sha256"]},
                    "meaning": "Provider-only amendment authorized separately by the user; the exact charter agreement is unchanged and no review attempt was consumed or renewed"}
    files[EPOCH_DIR + "/preparation.json"] = _json_bytes(new_prepared)
    files[EPOCH_DIR + "/epoch.json"] = _json_bytes(epoch_record)
    return files, new_delegate, new_prepared, epoch_record, {rel: ao.digest(data) for rel, data in files.items()}


def _receipt(state, inputs, evidence, recorded_at, manifest, epoch_record, new_prepared, new_delegate):
    return {"room_id": state["room_id"], "inputs": inputs, "audit_sha256": inputs["audit_sha256"],
            "before_state_sha256": evidence["state_sha256"], "epoch": EPOCH, "recorded_at": recorded_at, "manifest": manifest,
            "original_launch": evidence["original_launch"], "epoch_sha256": ao.digest(epoch_record),
            "preparation_sha256": ao.digest(new_prepared), "delegate_sha256": ao.digest(new_delegate)}


def _archive_launch(directory, original_launch):
    if not original_launch["present"]:
        return
    live, archive = directory / "delegate-launch.json", directory / ARCHIVE_DIR / "delegate-launch.json"
    if live.exists():
        data = owned_bytes(live)
        if ao.digest(data) != original_launch["sha256"]:
            raise RoomError("Original launch evidence changed; the transition stays blocked")
        _place(archive, data)
        os.unlink(live)
        _fsync_directory(directory)
    elif not archive.exists() or ao.digest(owned_bytes(archive)) != original_launch["sha256"]:
        raise RoomError("Original launch evidence is missing; the transition stays blocked")


def transition(service, room_id, audit_sha256, diagnosis, authorization, request_id, native_stop_record):
    ao.identifier(request_id)
    ao.nonempty(diagnosis, "diagnosis")
    ao.nonempty(authorization, "authorization: actual user switch decision")
    ao.nonempty(native_stop_record, "native_stop_record: the operator's record of the performed AO exit-agent")
    inputs = {"request_id": request_id, "audit_sha256": audit_sha256, "diagnosis": diagnosis,
              "authorization": authorization, "native_stop_record": native_stop_record}
    with service.locked(room_id) as (directory, state):
        committed = validate(service, state, allow_pending=True)
        if committed is not None:
            record, epoch = committed
            if record["inputs"] != inputs:
                raise RoomError("Provider transition already belongs to another payload; only its identical result can be read")
            return _result(state, record, epoch)
        pending = _pending_receipts(directory)
        if len(pending) > 1:
            raise RoomError("Multiple uncommitted provider transition receipts exist; diagnose before continuing")
        reconcile = _read(pending[0]) if pending else None
        if pending and (pending[0].name != request_id + ".json" or reconcile.get("inputs") != inputs):
            raise RoomError("An uncommitted provider transition receipt belongs to another payload; preserve it and diagnose before continuing")
        saved = _saved_audit(directory, audit_sha256)
        target_input = saved["evidence"]["target"]["input"]
        evidence, observed, _ = _inspect(service, directory, state, target_input, reconcile)
        if saved["evidence"] != evidence:
            raise RoomError("Provider transition audit is stale; inspect the changed room, ledger, candidate or native evidence")
        if observed["controller"] != STOPPED:
            raise RoomError("Engineer native controller is not positively stopped; perform and record the AO exit-agent before the transition")
        recorded_at = reconcile["recorded_at"] if reconcile else time.time()
        files, new_delegate, new_prepared, epoch_record, manifest = _bundle(service, directory, state, evidence, inputs, recorded_at)
        receipt = _receipt(state, inputs, evidence, recorded_at, manifest, epoch_record, new_prepared, new_delegate)
        if reconcile is not None and reconcile != receipt:
            raise RoomError("Pending provider transition receipt does not match the recomputed epoch; the transition stays blocked")
        target_state = {**state, "preparation": EPOCH_DIR + "/preparation.json", "preparation_sha256": ao.digest(new_prepared),
                        "delegate": new_delegate, "checkpoint": None,
                        "provider_transition": {"request_id": request_id, "key": ao.digest(inputs),
                            "receipt": BASE + "/requests/" + request_id + ".json", "receipt_sha256": ao.digest(receipt),
                            "epoch": EPOCH, "epoch_record": EPOCH_DIR + "/epoch.json", "epoch_sha256": ao.digest(epoch_record),
                            "original_preparation": evidence["preparation"]["path"],
                            "original_preparation_sha256": evidence["preparation"]["sha256"],
                            "original_delegate_sha256": evidence["delegate"]["sha256"], "state": "committed"}}
        repeated, observed_again, _ = _inspect(service, directory, state, target_input, reconcile)
        if repeated != evidence or observed_again["controller"] != STOPPED:
            raise RoomError("Provider transition observations changed before commit")
        from ao_routing_adoption import launcher_compatibility
        launcher_compatibility(service, target_state, new_prepared)
        if reconcile is None:
            _store_once(_receipt_path(directory, request_id), receipt)
        for rel, data in files.items():
            _place(directory / rel, data)
        _archive_launch(directory, evidence["original_launch"])
        service.save(directory, target_state)
        return _result(target_state, receipt, epoch_record)


def _result(state, record, epoch):
    reference = state["provider_transition"]
    return {"transitioned": True, "request_id": record["inputs"]["request_id"], "epoch": EPOCH,
            "audit_sha256": record["audit_sha256"],
            **{k: reference[k] for k in ("receipt", "receipt_sha256", "epoch_record", "epoch_sha256")},
            "source": {k: epoch["source"][k] for k in ("backend", "model", "profile_sha256")},
            "target": {k: epoch["target"][k] for k in TARGET_PUBLIC},
            "original_launch_archived": bool(epoch["original_launch"]["present"]),
            "attachment": "configuration_committed", "attached": False, "model_requests": 0,
            "meaning": "Configuration committed for provider epoch 2; no AO POST, registration change or model dispatch was performed. Native launch alone does not establish successful MCP initialization. Dispatch requires separately verified native startup and unchanged identity/history."}


def validate_bindings(service, state, pinned_bindings):
    """Keep native owners exact except for the separately verified one-time unused-reviewer recovery.

    This reads only the existing recovery chain, never provider or routing validation, so either epoch can
    apply it to its own immutable historical binding without recursion or rewriting that evidence.
    """
    try:
        current = state["bindings"]
        if (set(current) != set(pinned_bindings)
                or any(current[role] != binding for role, binding in pinned_bindings.items() if role != "reviewer")):
            raise RoomError("Native owner bindings contradict the committed adoption")
        from ao_reviewer_recovery import validate as recovery_validate
        recovery = recovery_validate(service, state)
        if current.get("reviewer") != pinned_bindings.get("reviewer"):
            if (recovery is None or recovery["original"] != pinned_bindings.get("reviewer")
                    or recovery["replacement"] != current.get("reviewer")):
                raise RoomError("Reviewer binding differs without a verified recovery from the pinned reviewer")
        return recovery
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RoomError("Adoption reviewer recovery evidence is unreadable or inconsistent") from exc


def validate(service, state, allow_pending=False):
    """Verify immutable epoch and original evidence; runtime overlays must prove their base epoch explicitly."""
    try:
        reference = state.get("provider_transition")
        directory = service.root / "rooms" / state["room_id"]
        if reference is None:
            if not allow_pending and _pending_receipts(directory):
                raise RoomError("An uncommitted provider transition receipt exists; complete it with the identical request or diagnose it before continuing")
            return None
        request_id = ao.identifier(reference["request_id"])
        relative = BASE + "/requests/" + request_id + ".json"
        if (reference["receipt"] != relative or reference["state"] != "committed" or reference["epoch"] != EPOCH
                or reference["epoch_record"] != EPOCH_DIR + "/epoch.json"):
            raise RoomError("Committed provider transition reference is malformed")
        if _pending_receipts(directory) != [directory / relative]:
            raise RoomError("Unexpected provider transition receipt exists; preserve it and diagnose before continuing")
        record = _read(directory / relative)
        if (ao.digest(record) != reference["receipt_sha256"] or record["room_id"] != state["room_id"]
                or record["inputs"]["request_id"] != request_id or ao.digest(record["inputs"]) != reference["key"]):
            raise RoomError("Committed provider transition evidence was modified")
        epoch = _read(directory / reference["epoch_record"])
        if (ao.digest(epoch) != reference["epoch_sha256"] or reference["epoch_sha256"] != record["epoch_sha256"]
                or epoch["audit_sha256"] != record["inputs"]["audit_sha256"] or epoch["request_id"] != request_id):
            raise RoomError("Committed provider epoch record was modified")
        audit = _saved_audit(directory, record["inputs"]["audit_sha256"])
        evidence = audit["evidence"]
        if (evidence["state_sha256"] != record["before_state_sha256"] or evidence["native"] != epoch["native"]
                or evidence["reviewer_native"] != epoch["reviewer_native"]
                or evidence["candidate"] != epoch["candidate"] or evidence["agreement"] != epoch["agreement"]
                or evidence["spec"] != epoch["spec"] or epoch["before_state_sha256"] != record["before_state_sha256"]):
            raise RoomError("Provider transition audit chain changed")
        for rel, expected in record["manifest"].items():
            if not isinstance(rel, str) or not rel.startswith(EPOCH_DIR + "/") or ".." in Path(rel).parts:
                raise RoomError("Provider transition manifest path changed")
            if ao.digest(owned_bytes(directory / rel)) != expected:
                raise RoomError("Provider transition epoch file was modified: " + rel)
        for path, expected in {**evidence["delegate"]["files"], **evidence["delegate"]["record"]["inventory"]["files"]}.items():
            if ao.digest(owned_bytes(path)) != expected:
                raise RoomError("Original provider evidence was modified: " + Path(path).name)
        if (ao.digest(_read(directory / evidence["preparation"]["path"])) != evidence["preparation"]["sha256"]
                or ao.digest(_read(directory / evidence["handoff"]["path"])) != evidence["handoff"]["sha256"]
                or ao.digest(evidence["delegate"]["record"]) != evidence["delegate"]["sha256"]):
            raise RoomError("Original provider preparation, delegate or handoff evidence changed")
        ao_workflow.spec_record_file(directory, epoch["spec"]["spec_record_sha256"])
        if (ao.digest(state.get("delegate")) != epoch["target"]["delegate_sha256"]
                or reference["original_preparation"] != epoch["source"]["preparation"]
                or reference["original_preparation_sha256"] != epoch["source"]["preparation_sha256"]
                or reference["original_delegate_sha256"] != epoch["source"]["delegate_sha256"]):
            raise RoomError("Room delegate pointers or native bindings contradict the committed provider epoch")
        validate_bindings(service, state, {"engineer": epoch["engineer"], "reviewer": epoch["reviewer"]})
        routing = state.get("routing_adoption")
        pending_routing = isinstance(routing, dict) and routing.get("phase") == "pending"
        if allow_pending and pending_routing:
            # Read-only status/identical-result projection may verify the committed provider chain while a
            # separate overlay is pending. Its active pointers must still be the exact unactivated base.
            if (state.get("preparation") != epoch["target"]["preparation"]
                    or state.get("preparation_sha256") != epoch["target"]["preparation_sha256"]):
                raise RoomError("Pending routing adoption changed the active provider preparation")
        elif routing is not None:
            from ao_routing_adoption import validate as routing_validate
            if routing_validate(service, state, provider_epoch=epoch) is None:
                raise RoomError("Active routing adoption has no verified provider epoch base")
        elif (state.get("preparation") != epoch["target"]["preparation"]
              or state.get("preparation_sha256") != epoch["target"]["preparation_sha256"]):
            raise RoomError("Room delegate pointers contradict the committed provider epoch")
        if epoch["original_launch"]["present"]:
            archive = directory / ARCHIVE_DIR / "delegate-launch.json"
            if not archive.exists() or ao.digest(owned_bytes(archive)) != epoch["original_launch"]["sha256"]:
                raise RoomError("Archived original launch evidence is missing or modified")
        return record, epoch
    except (KeyError, TypeError, ValueError, OSError, ImportError) as exc:
        raise RoomError("Provider transition evidence is unreadable or inconsistent") from exc


def native_startup_gate(service, directory, state, snapshot, epoch, prepared):
    """Extension point for verified initialize/initialized/tools-list evidence from the actual native MCP client.

    A pre-exec launcher receipt is insufficient: exec, adapter initialization and the native handshake may all fail.
    The connection wrapper records its actual native client handshake and holds its lease; this gate itself
    makes no lifecycle calls. Rooms with only the original launcher receipt remain blocked.
    """
    if not prepared.get("mcp_attachment"):
        raise RoomError("Epoch 2 has no verified native MCP startup evidence; launch_observed alone does not qualify attachment")
    try:
        from ao_mcp_attachment import validate_attachment
        evidence = validate_attachment(directory, state, prepared)
    except (ImportError, OSError, ValueError) as exc:
        raise RoomError("Epoch 2 native MCP startup evidence is unavailable or inconsistent: " + str(exc)) from exc
    if evidence is None:
        raise RoomError("Epoch 2 native MCP startup has not been positively established")
    return evidence


def dispatch_gate(service, directory, state, snapshot):
    result = validate(service, state)
    if result is None:
        return None
    _, epoch = result
    if snapshot.get("controller") != "ready":
        raise RoomError("Epoch 2 dispatch requires a ready native controller; a held MCP lease cannot prove AO readiness")
    launch = directory / "delegate-launch.json"
    if not launch.exists():
        raise RoomError("Epoch 2 native launch has not been observed; diagnose native startup before dispatch")
    observed = _read(launch)
    engineer = state["bindings"]["engineer"]
    prepared = ao_delegates.validate_preparation(directory, state, engineer["session_id"])
    if (observed.get("preparation_sha256") != state["preparation_sha256"] or observed.get("session_id") != engineer["session_id"]
            or observed.get("worktree") != prepared["worktree"]):
        raise RoomError("Native delegate launch contradicts the active provider epoch")
    current = _native(state, engineer, snapshot, directory)
    if (current["conversation_id"] != epoch["native"]["conversation_id"] or current["branch_id"] != epoch["native"]["branch_id"]
            or current["model"] != epoch["native"]["model"] or current["reasoning_effort"] != epoch["native"]["reasoning_effort"]):
        raise RoomError("Native conversation identity differs from the committed provider epoch")
    original = {t["turn_id"] for t in epoch["native"]["owned_turns"] + epoch["native"]["unowned_completed_turns"]}
    allowed = original | {r["turn_id"] for r in state["requests"].values()
                          if r.get("provider_epoch") == EPOCH and r.get("turn_id") and r.get("session_id") == engineer["session_id"]}
    ids = {t["id"] for t in snapshot["turns"]}
    if allowed - ids:
        raise RoomError("Pinned native turns are missing from the observed history")
    if ids - allowed:
        raise RoomError("Native history changed since the provider transition (unattributed turns); diagnose before dispatch")
    old_messages = set(epoch["native"]["message_ids"])
    if (_history_digest([t for t in snapshot["turns"] if t["id"] in original],
                        [m for m in snapshot["messages"] if m["id"] in old_messages]) != epoch["native"]["history_sha256"]):
        raise RoomError("Native history bytes differ from the committed provider epoch")
    native_startup_gate(service, directory, state, snapshot, epoch, prepared)
    return epoch


def amendment_text(epoch):
    target = epoch["target"]
    return ("Provider amendment epoch 2 (request " + epoch["request_id"] + ", separately authorized by the user; recorded "
            + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch["recorded_at"])) + "): this room's delegate transport changed from "
            + epoch["source"]["backend"] + " model " + epoch["source"]["model"] + " to the official DeepSeek API model "
            + target["model"] + " at reasoning effort " + target["reasoning_effort"] + " with a " + str(target["max_tokens"])
            + "-token output cap and " + str(target["context_tokens"]) + "-token context. The exact charter agreement for specification revision "
            + str(epoch["spec"]["revision"]) + " (SHA256 " + epoch["spec"]["sha256"] + ") is unchanged: it was not re-reviewed, and no charter attempt was consumed or renewed. Delegate jobs from before this amendment keep their original settings and provenance; attribute new jobs to epoch 2 in routing_log.")


def amendment_delivered(directory, state):
    """A separately carried epoch note must have its own completed, digest-bound native receipt."""
    expected = state["provider_transition"]["epoch_sha256"]
    delivered = False
    for request in state["requests"].values():
        carried = request.get("carried") or {}
        if "provider_amendment_sha256" not in carried:
            continue
        if request.get("role") != "engineer" or request.get("provider_epoch") != EPOCH or carried["provider_amendment_sha256"] != expected:
            raise RoomError("Delivered provider amendment contradicts its epoch")
        if state.get("routing_adoption"):
            from ao_routing_adoption import INSTRUCTION
            if (carried.get("routing_amendment_sha256") != ao.digest(INSTRUCTION.encode())
                    or carried.get("routing_adoption_sha256") != state["routing_adoption"]["receipt_sha256"]):
                raise RoomError("Delivered routing amendment contradicts its configured adoption")
        if request.get("state") == "completed":
            receipt = ao_workflow.completed_receipt(directory, request)
            if receipt.get("carried_sha256") != ao.digest(carried) or not ao.sent_message(request, receipt):
                raise RoomError("Delivered provider amendment has no matching native receipt")
            delivered = True
    return delivered


def report_state(directory, state, request):
    """A prior report is checked against its own immutable profile; this never admits a prior result for acceptance."""
    if not state.get("provider_transition") or request.get("provider_epoch") == EPOCH:
        return state
    _, epoch = validate(SimpleNamespace(root=directory.parent.parent), state)
    audit = _saved_audit(directory, epoch["audit_sha256"])
    return {**state, "delegate": audit["evidence"]["delegate"]["record"]}


def historical_correction(service, directory, state, request, report, snapshot):
    """Admit correction of a completed partial report, never capture or accept its historical claims.

    The saved transition supplies the original profile and captured ledger rows. Current routing prose
    supplies no authority. The caller still runs the ordinary native/routing dispatch gates afterwards.
    """
    if (request.get("provider_epoch") != EPOCH or request.get("role") != "engineer"
            or request.get("purpose") not in ("implementation", "correction")
            or request.get("handoff_sha256") != state.get("handoff_sha256")
            or report.get("outcome") != "changes_required" or report.get("implementation_complete") is not False):
        raise RoomError("Historical provider correction requires a completed partial report on the current handoff")
    receipt = ao_workflow.completed_receipt(directory, request)
    if ao_workflow.engineering_report(directory, state, request) != report:
        raise RoomError("Historical provider correction differs from the saved engineering report")
    ao_workflow.completion_candidate(directory, state, request)  # validate the old evidence; never recapture later edits
    turns = [t for t in snapshot.get("turns", []) if t.get("id") == request["turn_id"]]
    messages = [m for m in snapshot.get("messages", []) if m.get("turnId") == request["turn_id"]]
    if (snapshot.get("history_truncated") or len(turns) != 1 or turns[0].get("state") != "completed"
            or turns[0].get("providerTurnId") != ao.native_turn_identity(request)
            or messages != receipt.get("messages") or not ao.sent_message(request, snapshot)):
        raise RoomError("Historical provider correction requires the unchanged completed native result")
    committed = validate(service, state)
    if committed is None:
        raise RoomError("Historical provider correction requires a verified committed transition")
    _, epoch = committed
    audit = _saved_audit(directory, epoch["audit_sha256"])
    original = audit["evidence"]["ledger"]
    ao_delegates.assert_settled(service.root.parent, dict(state), directory)
    present, rows = _ledger_rows(service.root.parent, state["room_id"])
    _ledger_check(rows)  # also refuses unreported abandoned or unrecognized rows beyond the status window
    old_rows = {row["id"]: row for row in original["rows"]}
    current_rows = {row["id"]: row for row in rows}
    if (not present or not original["present"] or original["count"] != len(old_rows)
            or any(current_rows.get(job_id) != row for job_id, row in old_rows.items())):
        raise RoomError("Historical provider correction requires unchanged captured transition-ledger rows")
    for row in rows:
        if row["id"] not in old_rows and (row["profile_sha256"] != epoch["target"]["profile_sha256"]
                                         or row["requested_model"] != epoch["target"]["model"]):
            raise RoomError("Historical provider correction found an unaudited provider job")
    ids = ao_delegates.report_job_ids(report)
    historical_ids = [job_id for job_id in ids if job_id in old_rows]
    if not historical_ids:
        raise RoomError("The rejected report names no audited historical provider jobs")
    source_state = {**state, "delegate": audit["evidence"]["delegate"]["record"]}
    def claims(job_ids):
        return {"routing_log": [{"delegate_job_ids": job_ids}]}
    # All original jobs must retain their captured rows and verifiable content, including unreported jobs.
    original_evidence = ao_delegates.verify_delegation(service.root.parent, directory, source_state, claims(list(old_rows)))
    current_evidence = ao_delegates.verify_delegation(service.root.parent, directory, state,
                                                    claims([job_id for job_id in ids if job_id not in old_rows]))
    return {"kind": "historical_provider_correction", "admission_only": True,
            "prior_request_id": request["request_id"], "prior_receipt_sha256": request["receipt_sha256"],
            "prior_report_sha256": ao.digest(report), "provider_transition_sha256": state["provider_transition"]["receipt_sha256"],
            "audit_sha256": epoch["audit_sha256"], "owning_ledger_sha256": ao.digest(rows),
            "historical_jobs": [item for item in original_evidence if item["job_id"] in historical_ids],
            "current_jobs": current_evidence}


def summary(service, directory, state, delegate_status):
    try:
        result = validate(service, state, allow_pending=True)
        pending = _pending_receipts(directory)
        if result is None:
            return ({"state": "pending_uncommitted", "receipts": [p.name for p in pending],
                     "meaning": "A durable transition receipt exists without a committed epoch; only the identical request can complete it and every other mutation refuses"}
                    if pending else None)
        record, epoch = result
        reference = state["provider_transition"]
        attachment = (delegate_status or {}).get("attachment")
        routing_pending = (state.get("routing_adoption") or {}).get("phase") == "pending"
        if routing_pending:
            attachment = "routing_pending"
        if attachment == "native_mcp_launch_observed":
            attachment = "launch_observed"
        return {"state": "committed", "epoch": EPOCH,
                **{k: reference[k] for k in ("request_id", "receipt", "receipt_sha256", "epoch_record", "epoch_sha256")},
                "audit_sha256": record["audit_sha256"], "recorded_at": epoch["recorded_at"],
                "source": {k: epoch["source"][k] for k in ("backend", "model", "profile_sha256")},
                "target": {k: epoch["target"][k] for k in TARGET_PUBLIC}, "attachment": attachment,
                "attached": attachment == "native_mcp_initialized_observed",
                "routing_pending": routing_pending,
                "native_startup": None if routing_pending else (delegate_status or {}).get("native_startup"),
                "original_launch_archived": bool(epoch["original_launch"]["present"]),
                "meaning": "Configuration committed for epoch 2. launch_observed proves only that the launcher ran. attached requires the current held local lease and the native initialize/initialized/tools-list exchange; it is not portable process-start, AO-controller or inference proof. Dispatch separately verifies unchanged native identity/history."}
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"state": "damaged", "error": str(exc),
                "meaning": "Committed provider transition evidence failed verification; every room mutation refuses until it is diagnosed"}


def job_attribution(home, directory, state, jobs):
    if not state.get("provider_transition"):
        return
    try:
        _, epoch = validate(SimpleNamespace(root=Path(home) / "ao"), state, allow_pending=True)
        profiles = {epoch[key]["profile_sha256"]: {"epoch": number, "backend": epoch[key]["backend"], "model": epoch[key]["model"]}
                    for key, number in (("source", 1), ("target", EPOCH))}
        try:
            _, rows = _ledger_rows(home, state["room_id"])
        except RoomError:
            jobs["attribution_error"] = "ledger_unreadable"
            return
        mapping = {row["id"]: row["profile_sha256"] for row in rows}
        for item in jobs.get("items", []):
            info = profiles.get(mapping.get(item["job_id"]))
            item.update(epoch=info["epoch"] if info else None, backend=info["backend"] if info else None,
                        attribution="epoch_profile" if info else "unknown_profile")
        jobs.update(backend=epoch["target"]["backend"], model=epoch["target"]["model"],
                    epochs={str(info["epoch"]): {**info, "profile_sha256": profile} for profile, info in profiles.items()},
                    meaning="latest ledger facts for this room's delegate jobs; usage is provider-reported or unknown; after the provider transition each job is attributed by its own profile_sha256 to epoch 1 or 2 and the top-level backend/model name the active epoch")
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        jobs["attribution_error"] = str(exc)
