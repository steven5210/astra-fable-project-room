"""Audited operator normalization of presentation around one native JSON result.

The operator, not this parser, reviews the meaning of all surrounding prose. A
derived record never changes the native receipt, verdict, candidate or budget.
"""

import json
import math

from implementation import candidate_snapshot
from room import RoomError


class ResponseFormatError(RoomError):
    pass


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def decoder():
    return json.JSONDecoder(object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)


def strict_object(text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        value = decoder().decode(text)
    except (ValueError, RecursionError) as exc:
        raise ResponseFormatError("Native final response must be one unambiguous JSON object") from exc
    if not isinstance(value, dict):
        raise ResponseFormatError("Native final response must be one unambiguous JSON object")
    return value


def extract(text, start, end):
    """Offsets count Python Unicode characters in the exact, untrimmed final text.

    First-brace anchoring and the ban on structural JSON punctuation outside the
    selected object prevent selecting an inner object, array member, quoted JSON
    string or one convenient verdict among several. Unknown wrappers refuse.
    """
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or start != text.find("{")):
        raise RoomError("Select the complete first and only top-level JSON object")
    try:
        value, actual_end = decoder().raw_decode(text, start)
    except (ValueError, RecursionError) as exc:
        raise RoomError("Selected response contains invalid or ambiguous JSON") from exc
    if not isinstance(value, dict) or actual_end != end:
        raise RoomError("Selected span is not the complete top-level JSON object")
    before, after = text[:start], text[end:]
    if any(c in before + after for c in '{}[]"'):
        raise RoomError("Additional JSON structure outside the selected object is ambiguous")
    if "```" in before + after:
        # Only one ordinary JSON fence surrounding the object is understood.
        if (before.count("```") != 1 or after.count("```") != 1
                or not before.endswith("```json\n") or not after.startswith("\n```")):
            raise RoomError("Response has ambiguous or unsupported Markdown fences")
    return value, before, after


def source(directory, request):
    from ao_project_room import digest, native_turn_identity, sent_message
    from ao_workflow import completed_receipt, raw_final_text
    receipt = completed_receipt(directory, request)
    turn = receipt.get("turn") or {}
    messages = receipt.get("messages") or []
    if (not isinstance(turn, dict) or not isinstance(messages, list)
            or any(not isinstance(m, dict) or not isinstance(m.get("text", ""), str) for m in messages)):
        raise RoomError("Normalization native receipt has malformed turn or message evidence")
    if (receipt.get("history_truncated") or turn.get("state") != "completed"
            or not request.get("turn_id") or turn.get("id") != request["turn_id"]
            or turn.get("providerTurnId") != native_turn_identity(request)
            or any(m.get("turnId") != request["turn_id"] for m in messages)
            or not sent_message(request, receipt)):
        raise RoomError("Normalization requires complete exact native turn and delivery evidence")
    settings = receipt.get("settings") or {}
    if settings.get("model") != request["model"] or settings.get("reasoningEffort") != request["reasoning_effort"]:
        raise RoomError("Normalization receipt contradicts the pinned native model/effort")
    finals = [m for m in messages if m.get("role") == "assistant" and not m.get("streaming") and m.get("text", "").strip()]
    assistants = [m for m in messages if m.get("role") == "assistant" and m.get("text", "").strip()]
    if (not finals or assistants[-1].get("streaming") or not isinstance(finals[-1].get("id"), str)
            or not finals[-1]["id"] or sum(m.get("id") == finals[-1]["id"] for m in messages) != 1):
        raise RoomError("Normalization requires an identifiable final assistant response")
    raw = raw_final_text(directory, request)
    return raw, {"request_id": request["request_id"], "session_id": request["session_id"],
                 "turn_id": request["turn_id"], "provider_turn_id": native_turn_identity(request),
                 "final_message_id": finals[-1]["id"], "receipt": request["receipt"],
                 "receipt_sha256": request["receipt_sha256"], "final_text_sha256": digest(raw.encode()),
                 "spec_record_sha256": request["spec_record_sha256"], "handoff_sha256": request.get("handoff_sha256"),
                 "review": request.get("review")}


def load_result(directory, request, raw):
    """Every consumer rechecks derived evidence against the immutable native bytes."""
    from ao_project_room import digest, read
    relative = "response-normalizations/" + request["request_id"] + ".json"
    if request.get("response_normalization") != relative:
        raise RoomError("Response normalization record has no exact owned path")
    record = read(directory / relative)
    if digest(record) != request.get("response_normalization_sha256"):
        raise RoomError("Response normalization evidence was modified")
    observed, identity = source(directory, request)
    if (observed != raw or record.get("source") != identity or record.get("version") != 1
            or record.get("room_id") != directory.name):
        raise RoomError("Response normalization refers to different native evidence")
    inputs = record.get("inputs") or {}
    if (inputs.get("receipt_sha256") != identity["receipt_sha256"]
            or inputs.get("final_text_sha256") != identity["final_text_sha256"]
            or inputs.get("confirm_no_additional_verdict") is not True
            or not isinstance(inputs.get("astra_review"), str) or not inputs["astra_review"].strip()):
        raise RoomError("Response normalization lacks the exact operator review")
    value, before, after = extract(raw, inputs.get("json_start"), inputs.get("json_end"))
    if (record.get("outside_text") != {"before": before, "after": after}
            or record.get("json_sha256") != digest(raw[inputs["json_start"]:inputs["json_end"]].encode())
            or record.get("result_sha256") != digest(value)):
        raise RoomError("Response normalization span or outside text changed")
    return value


def validate_current(service, directory, state, request, value):
    """The result keeps its existing purpose, identities and refusal semantics."""
    import ao_delegates
    import ao_workflow
    from ao_outcomes import usable
    usable(directory, request)
    spec = service.spec(directory, state)
    if request["spec_record_sha256"] != state["spec_record_sha256"]:
        raise RoomError("Cannot normalize a stale specification response")
    same_role = [r for r in state["requests"].values() if r["role"] == request["role"]]
    if max(same_role, key=lambda r: r["created_order"])["request_id"] != request["request_id"]:
        raise RoomError("Only the latest response for its role may be normalized")
    from ao_project_room import turn_ids, sent_message
    current = service.identity(service.client(state), state, request)
    new_turns = turn_ids(current) - set(request["baseline"]["turn_ids"])
    current_turn = [t for t in current.get("turns", []) if t.get("id") == request["turn_id"]]
    saved_receipt = ao_workflow.completed_receipt(directory, request)
    live_messages = [m for m in current.get("messages", []) if m.get("turnId") == request["turn_id"]]
    if (current.get("history_truncated") or new_turns != {request["turn_id"]} or len(current_turn) != 1
            or current_turn[0].get("state") != "completed"
            or current_turn[0].get("providerTurnId") != request.get("provider_turn_id")
            or not sent_message(request, current) or live_messages != saved_receipt["messages"]):
        raise RoomError("Native result is stale, incomplete or differs from its saved completed turn")
    from ao_outcomes import observe
    observe(service, directory, state, request, current)
    usable(directory, request)
    if request["role"] == "engineer":
        if not ao_workflow.normal(state):
            raise RoomError("Astra-led engineering has no normal Fable report contract")
        if request.get("purpose") == "spec_review":
            if (value.get("decision") not in ("accept", "changes_required")
                    or type(value.get("spec_revision")) is not int or value["spec_revision"] != spec["revision"]
                    or value.get("spec_sha256") != spec["sha256"]
                    or not isinstance(value.get("interpretation"), str) or not value["interpretation"].strip()
                    or not isinstance(value.get("findings"), list) or not all(isinstance(f, str) for f in value["findings"])):
                raise RoomError("Normalized specification response has an invalid verdict or exact-spec identity")
        elif request.get("purpose") in ("implementation", "correction"):
            ao_workflow.agreement(service, directory, state)
            value = ao_workflow.engineering_report(directory, state, request, report=value)
            actual = ao_workflow.workspace(service, directory, state, check_routing=False)
            if candidate_snapshot(actual) != ao_workflow.completion_candidate(directory, state, request):
                raise RoomError("Candidate changed after the completed native result")
            ao_delegates.verify_delegation(service.root.parent, directory, state, value)
        else:
            raise RoomError("Unknown engineer response purpose")
    elif request["role"] == "reviewer" and request.get("purpose") in (None, "acceptance_review"):
        if ao_workflow.normal(state):
            ao_workflow.engineering_ready(service, directory, state)
        checkpoint = service.checkpoint(directory, state)
        expected = {"spec_sha256": checkpoint["spec_sha256"], "candidate_sha256": checkpoint["candidate_sha256"],
                    "evidence_sha256": state["checkpoint_sha256"]}
        if (request.get("review") != expected or value.get("decision") not in ("approved", "rejected")
                or any(value.get(k) != v for k, v in expected.items())
                or not isinstance(value.get("review"), str) or not value["review"].strip() or len(value["review"]) > 30000):
            raise RoomError("Normalized reviewer result has an invalid verdict or stale candidate/gate identities")
    else:
        raise RoomError("Unknown result role or purpose")


def normalize(service, directory, state, request_id, receipt_sha256, final_text_sha256,
              json_start, json_end, astra_review, confirm_no_additional_verdict):
    from ao_project_room import atomic, digest, identifier, nonempty
    import ao_workflow
    identifier(request_id)
    nonempty(astra_review, "astra_review", 6000)
    if confirm_no_additional_verdict is not True:
        raise RoomError("Astra must review all outside prose and explicitly confirm it adds no contradictory or additional verdict")
    service.quiet(state)
    request = state["requests"].get(request_id)
    if not request:
        raise RoomError("Unknown native result request")
    raw, identity = source(directory, request)
    if identity["receipt_sha256"] != receipt_sha256 or identity["final_text_sha256"] != final_text_sha256:
        raise RoomError("Operator review does not name the exact receipt and untrimmed final text")
    inputs = {"receipt_sha256": receipt_sha256, "final_text_sha256": final_text_sha256,
              "json_start": json_start, "json_end": json_end, "astra_review": astra_review,
              "confirm_no_additional_verdict": confirm_no_additional_verdict}
    if "response_normalization" in request or "response_normalization_sha256" in request:
        from ao_project_room import read
        value = load_result(directory, request, raw)
        old = read(directory / request["response_normalization"])
        if old["inputs"] != inputs:
            raise RoomError("This response already has a different immutable normalization")
        validate_current(service, directory, state, request, value)
        return {"normalized": True, "request_id": request_id, "normalization_sha256": request["response_normalization_sha256"],
                "model_dispatch": False}
    try:
        strict_object(raw)
    except ResponseFormatError:
        pass
    else:
        raise RoomError("The native final response is already strict JSON; normalization cannot change its verdict")
    value, before, after = extract(raw, json_start, json_end)
    validate_current(service, directory, state, request, value)
    relative = "response-normalizations/" + request_id + ".json"
    if (directory / relative).exists():
        raise RoomError("Unclaimed response normalization exists; preserve it for diagnosis")
    record = {"version": 1, "room_id": state["room_id"], "source": identity, "inputs": inputs,
              "outside_text": {"before": before, "after": after},
              "json_sha256": digest(raw[json_start:json_end].encode()), "result_sha256": digest(value)}
    atomic(directory / relative, record)
    request.update(response_normalization=relative, response_normalization_sha256=digest(record))
    # Save the derived receipt before engineering capture; an I/O failure cannot erase the reviewed normalization.
    service.save(directory, state)
    if request["role"] == "engineer" and request.get("purpose") in ("implementation", "correction"):
        ao_workflow.capture_engineering(service.root.parent, directory, state, request, capture_completion=False)
        service.save(directory, state)
    return {"normalized": True, "request_id": request_id, "normalization_sha256": digest(record), "model_dispatch": False}
