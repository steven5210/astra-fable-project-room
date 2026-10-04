"""The standing room policy: follow the newest qualified family member at a stopped boundary.

``record`` writes the operator's standing decision (set or revoke) as one append-only digest-chained
record per room, mirroring the transition journal's create-once and ownership discipline. ``apply``
runs at the top of every engineer ``ao_room_send`` before the send takes its own lock — the room lock
is never nested — and, when the policy is active, the engineer is stopped, no request is active or
held, no transition epoch is pending, and the configured qualification's expected model for the
recorded family is a strictly newer exact member, it runs the unchanged audited ``audit`` +
``transition`` pair in-process and the send proceeds under the new committed epoch. Every outcome
except ``inactive`` appends one application record; a deferral never blocks the send, which continues
on the current pinned model. The policy never crosses families, never trusts an alias, never
downgrades and never releases a hold.
"""

from datetime import datetime, timezone
import re

from room import RoomError

VERSION = 1
POLICY = "follow_newest_qualified_family_member"
BASE = "engineering-model/policy"
POINTER_KEY = "engineering_model_policy"
ACTIONS = ("set", "revoke")
POINTER_FIELDS = frozenset(("version", "active", "family", "role", "record", "sha256", "recorded_at",
                            "last_application"))
RECORD_FIELDS = frozenset(("version", "action", "policy", "family", "role", "authorization",
                           "recorded_at", "previous_sha256", "epoch_sha256", "qualification_sha256"))
APPLICATION_FIELDS = frozenset(("version", "room_id", "recorded_at", "policy", "policy_record",
                                "policy_record_sha256", "purpose", "outcome", "reason", "from", "to",
                                "request_id"))
OUTCOMES = ("inactive", "no_change", "deferred", "transitioned")
_RECORD_NAME = re.compile(r"([1-9][0-9]*)-([0-9a-f]{12})\.json")
DAMAGED = ("The standing engineering model policy evidence is unclaimed, missing or inconsistent; "
           "preserve and diagnose")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _text(value):
    return isinstance(value, str) and bool(value)


def _hex(value):
    import ao_engineering_model as em
    return em._hex(value)


def _record_value(directory, record, sha256):
    """Read and digest-check one retained policy record named by its room-relative path."""
    import ao_engineering_model as em
    value = em._read(directory, record, DAMAGED)
    if not isinstance(value, dict) or em._digest(value) != sha256:
        raise RoomError(DAMAGED)
    return value


def _pointer(directory, state):
    """The validated standing-policy pointer with its head record value, or None."""
    import ao_engineering_model as em
    value = (state or {}).get(POINTER_KEY)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != POINTER_FIELDS or value["version"] != VERSION
            or type(value["active"]) is not bool or value["role"] != "engineer"
            or not isinstance(value["family"], str) or not em.FAMILY_ID.fullmatch(value["family"])
            or not _text(value["record"]) or not _hex(value["sha256"])
            or not _text(value["recorded_at"])
            or not (value["last_application"] is None or isinstance(value["last_application"], dict))):
        raise RoomError(DAMAGED)
    record = _record_value(directory, value["record"], value["sha256"])
    if (set(record) != RECORD_FIELDS or record["version"] != VERSION or record["policy"] != POLICY
            or record["action"] not in ACTIONS or record["role"] != "engineer"
            or record["family"] != value["family"]):
        raise RoomError(DAMAGED)
    if value["active"] != (record["action"] == "set"):
        raise RoomError(DAMAGED)
    return {**value, "record_value": record}


def _chain(directory, pointer):
    """The ordered validated record chain; a pointer must name exactly its head."""
    import ao_engineering_model as em
    base = em._relative(directory, BASE + "/records")
    entries = []
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        raise RoomError(DAMAGED)
    if base.exists():
        for path in base.iterdir():
            match = _RECORD_NAME.fullmatch(path.name)
            relative = BASE + "/records/" + path.name
            if match is None or path.is_symlink():
                raise RoomError(DAMAGED)
            value = em._read(directory, relative, DAMAGED)
            if (not isinstance(value, dict) or set(value) != RECORD_FIELDS
                    or value["version"] != VERSION or value["policy"] != POLICY
                    or value["action"] not in ACTIONS or value["role"] != "engineer"
                    or em._digest(value)[:12] != match.group(2)):
                raise RoomError(DAMAGED)
            entries.append((int(match.group(1)), relative, em._digest(value), value))
        entries.sort(key=lambda item: item[0])
        if [number for number, _, _, _ in entries] != list(range(1, len(entries) + 1)):
            raise RoomError(DAMAGED)
        for index, (_, _, sha256, value) in enumerate(entries):
            if value["previous_sha256"] != (entries[index - 1][2] if index else None):
                raise RoomError(DAMAGED)
    if pointer is None:
        if entries:
            raise RoomError(DAMAGED)
        return []
    if not entries or pointer["record"] != entries[-1][1] or pointer["sha256"] != entries[-1][2]:
        raise RoomError(DAMAGED)
    return entries


def _family_of_model(service_root, model):
    """The sole engineer-role family this exact identifier belongs to, or None."""
    import ao_engineering_model as em
    policy, _ = em.effective_policy(service_root)
    candidates = [name for name, entry in (policy.get("families") or {}).items()
                  if "engineer" in (entry.get("roles") or []) and em.family_member(name, model)]
    return candidates[0] if len(candidates) == 1 else None


def record(service, room_id, action, authorization, family=None):
    """Append one audited standing-policy record and move the room's pointer to it.

    ``set`` requires a normal room whose current committed epoch pins an exact identifier of the
    recorded family (the caller's ``family``, defaulting to the current model's family, must equal
    it — a cross-family standing policy is refused and stays manual). ``revoke`` requires an active
    policy. The authorization text is the operator's actual decision, stored verbatim and bounded.
    """
    import ao_project_room as ao
    import ao_engineering_model as em
    import ao_workflow
    ao.identifier(room_id)
    if action not in ACTIONS:
        raise RoomError("A standing policy action is exactly set or revoke")
    authorization = ao.nonempty(authorization,
                                "authorization: the actual user decision for this standing policy", 6000)
    if family is not None and not (isinstance(family, str) and em.FAMILY_ID.fullmatch(family)):
        raise RoomError("family must be an exact Claude family alias")
    with service.locked(room_id) as (directory, state):
        if not ao_workflow.normal(state):
            raise RoomError("A standing engineering model policy is for normal engineering-orchestrator rooms")
        pointer = _pointer(directory, state)
        entries = _chain(directory, pointer)
        if action == "set":
            em.journal(directory, state)  # a pending transition epoch refuses the new policy
            epoch = em.current(directory, state)
            selector = epoch.get("selector") if isinstance(epoch, dict) else None
            if not isinstance(selector, dict) or selector.get("kind") != "exact":
                raise RoomError("A standing policy requires a committed epoch pinned to an exact identifier; "
                                "a family alias stays the per-release manual transition")
            current = epoch["configured_model"]
            derived = _family_of_model(service.root, current)
            if derived is None:
                raise RoomError("The room's pinned model is not an exact member of an engineer family; "
                                "the standing policy stays unavailable")
            if family is None:
                family = derived
            elif family != derived:
                raise RoomError("A standing policy never crosses families: this room's exact model belongs "
                                "to " + derived + ", not " + family)
        else:
            if pointer is None or not pointer["active"]:
                raise RoomError("No active standing engineering model policy to revoke")
            family = pointer["family"]
            epoch = em.current(directory, state)
        qualification = em.configured_qualification(service.root)
        value = {"version": VERSION, "action": action, "policy": POLICY, "family": family,
                 "role": "engineer", "authorization": authorization, "recorded_at": _utc(),
                 "previous_sha256": entries[-1][2] if entries else None,
                 "epoch_sha256": ao.digest(epoch), "qualification_sha256":
                     qualification["sha256"] if isinstance(qualification, dict) else None}
        relative = BASE + "/records/%d-%s.json" % (len(entries) + 1, ao.digest(value)[:12])
        sha256 = em.store_once(directory, relative, value)
        pointer = {"version": VERSION, "active": action == "set", "family": family, "role": "engineer",
                   "record": relative, "sha256": sha256, "recorded_at": value["recorded_at"],
                   "last_application": None}
        state[POINTER_KEY] = pointer
        service.save(directory, state)
        return {"version": VERSION, "policy": POLICY, "action": action, "active": pointer["active"],
                "family": family, "record": relative, "record_sha256": sha256,
                "previous_sha256": value["previous_sha256"], "recorded_at": value["recorded_at"],
                "epoch_sha256": value["epoch_sha256"],
                "qualification_sha256": value["qualification_sha256"]}


def _retained_paths(chain):
    """The native owner database/transcript the most recent committed transition retained, or None."""
    committed = chain.get("committed") or []
    if not committed:
        return None
    inputs = (committed[-1].get("intent_value") or {}).get("inputs") or {}
    database, transcript = inputs.get("native_owner_database"), inputs.get("native_transcript_path")
    return (database, transcript) if _text(database) and _text(transcript) else None


def _write_application(service, directory, state, application):
    """Store one create-once application record and update the pointer; the caller holds the lock."""
    import ao_project_room as ao
    import ao_engineering_model as em
    slug = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    relative = BASE + "/applications/%s-%s.json" % (slug, ao.digest(application)[:12])
    sha256 = em.store_once(directory, relative, application)
    pointer = state.get(POINTER_KEY)
    if isinstance(pointer, dict) and pointer.get("sha256") == application["policy_record_sha256"]:
        pointer["last_application"] = {"outcome": application["outcome"],
                                       "reason": application["reason"],
                                       "at": application["recorded_at"], "record": relative,
                                       "record_sha256": sha256}
        service.save(directory, state)
    return sha256


def _transition_label(result):
    if result.get("pending"):
        return "pending"
    if result.get("reaudit_required"):
        return "reaudit_required"
    outcome = result.get("outcome")
    if isinstance(outcome, str) and outcome:
        return outcome
    return "abandoned" if result.get("abandoned") else "unknown"


def apply(service, room_id, purpose):
    """Apply the room's standing family-member policy at this engineer dispatch boundary.

    Returns {"outcome", "reason", "from", "to", "request_id", "record_sha256"}; every outcome except
    ``inactive`` appends an application record. A ``deferred`` outcome never blocks the send, which
    continues on the current pinned model; a ``transitioned`` outcome means the unchanged audited
    transition committed a new epoch and the send proceeds under it. The room lock is taken only in
    short non-nested windows: ``audit`` and ``transition`` take it themselves.
    """
    import ao_project_room as ao
    import ao_engineering_model as em
    import ao_engineering_transition as transition_module
    import ao_outcomes
    import ao_qualification_draft
    ao.identifier(room_id)
    result = {"outcome": "inactive", "reason": None, "from": None, "to": None,
              "request_id": None, "record_sha256": None}
    with service.locked(room_id) as (directory, state):
        pointer = _pointer(directory, state)
        if pointer is None:
            return result
        _chain(directory, pointer)  # every retained record must validate, active or revoked
        if not pointer["active"]:
            return result
        requests = state.get("requests") or {}
        if any(isinstance(r, dict) and r.get("role") == "engineer" and r.get("state") not in ao.TERMINAL
               for r in requests.values()):
            outcome, reason, current, target, request_id = "deferred", "engineer_active", None, None, None
        else:
            latest = ao_outcomes.latest_for_role(state, "engineer")
            status = latest.get("semantic_status") if isinstance(latest, dict) else None
            if isinstance(latest, dict) and (latest.get("semantic_observation_error")
                                           or (status or {}).get("hold")):
                outcome, reason, current, target, request_id = (
                    "deferred", "semantic_hold", None, None, None)
            else:
                chain = em.journal(directory, state, allow_pending=True)
                if chain.get("pending") is not None:
                    outcome, reason, current, target, request_id = (
                        "deferred", "transition_pending", None, None, None)
                else:
                    epoch = em.current(directory, state)
                    current = epoch.get("configured_model")
                    policy, provenance = em.effective_policy(service.root)
                    entry = (policy.get("families") or {}).get(pointer["family"]) or {}
                    execution = entry.get("execution_qualification")
                    target = execution.get("expected_model") if isinstance(execution, dict) else None
                    if target == current:
                        outcome, reason, request_id = "no_change", None, None
                    elif not em.family_member(pointer["family"], target):
                        outcome, reason, request_id = "deferred", "family_mismatch", None
                    elif ao_qualification_draft.version_tuple(target) \
                            <= ao_qualification_draft.version_tuple(current):
                        outcome, reason, request_id = "deferred", "qualified_not_newer", None
                    else:
                        source = state.get("native_outcome_source")
                        database = transcript = None
                        if isinstance(source, dict):
                            database, transcript = source.get("database"), source.get("transcript")
                        else:
                            retained = _retained_paths(chain)
                            if retained is not None:
                                database, transcript = retained
                        if not (_text(database) and _text(transcript)):
                            outcome, reason, request_id = "deferred", "no_registered_source", None
                        else:
                            revision = provenance.get("family_qualification_revision")
                            request_id = "standing-%s-r%s-%s" % (pointer["sha256"][:12],
                                                                 revision, target)
                            outcome, reason = None, (database, transcript)
        def application(outcome, reason, request_id):
            return {"version": VERSION, "room_id": state["room_id"], "recorded_at": _utc(),
                    "policy": POLICY, "policy_record": pointer["record"],
                    "policy_record_sha256": pointer["sha256"], "purpose": purpose,
                    "outcome": outcome, "reason": reason, "from": current, "to": target,
                    "request_id": request_id}

        if outcome is not None:
            result.update(outcome=outcome, reason=reason, **{"from": current, "to": target,
                                                             "request_id": request_id})
            result["record_sha256"] = _write_application(
                service, directory, state, application(outcome, reason, request_id))
            return result
        database, transcript = reason
    # Outside the room lock: the unchanged audited pair takes it themselves.
    audited = transition_module.audit(service, room_id, target, database, transcript)
    if not audited["eligible"]:
        outcome, reason = "deferred", "audit:" + str(audited.get("reason") or "unknown")[:300]
        transition_request_id = None
    else:
        transition_request_id = request_id
        try:
            outcome_result = transition_module.transition(
                service, room_id, request_id=request_id, source_model=current, target_model=target,
                audit_sha256=audited["audit_sha256"],
                spec_record_sha256=audited["spec_record_sha256"],
                candidate_sha256=audited["candidate_sha256"],
                native_history_sha256=audited["native_history_sha256"],
                native_owner_database=database, native_transcript_path=transcript,
                authorization=pointer["record_value"]["authorization"],
                reason="standing policy %s: %s -> %s; qualification revision %s; policy record %s"
                       % (POLICY, current, target, revision, pointer["sha256"][:12]))
        except RoomError as exc:
            outcome, reason = "deferred", str(exc)[:400]
        else:
            if outcome_result.get("transitioned"):
                outcome, reason = "transitioned", None
            else:
                outcome, reason = "deferred", "transition:" + _transition_label(outcome_result)
    with service.locked(room_id) as (directory, state):
        result.update(outcome=outcome, reason=reason, **{"from": current, "to": target,
                                                       "request_id": transition_request_id})
        result["record_sha256"] = _write_application(
            service, directory, state, application(outcome, reason, transition_request_id))
    return result


def summary(state):
    """The compact pointer summary ``ao_room_status`` reports; reads no files and validates nothing."""
    value = (state or {}).get(POINTER_KEY)
    if not isinstance(value, dict):
        return {"active": False}
    last = value.get("last_application")
    last = last if isinstance(last, dict) else None
    return {"active": value.get("active"), "family": value.get("family"),
            "record_sha256": value.get("sha256"),
            "last_application": None if last is None else
            {"outcome": last.get("outcome"), "reason": last.get("reason"), "at": last.get("at"),
             "record_sha256": last.get("record_sha256")}}
