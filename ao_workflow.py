"""Exact-spec Fable engineering and independent Astra acceptance contracts."""

import json
from pathlib import Path

import ao_delegates
from implementation import candidate_snapshot, ImplementationError
from room import RoomError

FABLE_MODEL = "claude-fable-5-1"
ENGINEERING_FIELDS = {"outcome", "implementation_complete", "changes", "tests_reported", "review_findings",
                      "remaining_gaps", "backlog", "routing_log", "spec_revision", "spec_sha256", "baseline_commit"}


def normal(state):
    return state.get("workflow") == "fable_engineering"


def completed_receipt(directory, request):
    from ao_project_room import digest, read, native_turn_identity
    if request.get("state") != "completed" or not native_turn_identity(request) or request.get("model_reroute"):
        raise RoomError("A completed native turn with uncontradicted identity is required")
    if not request.get("receipt"):
        raise RoomError("The native result has no saved receipt")
    receipt = read(directory / request["receipt"])
    if digest(receipt) != request.get("receipt_sha256"):
        raise RoomError("Native result receipt was modified")
    return receipt


def final_text(directory, request, allow_missing=False):
    receipt = completed_receipt(directory, request)
    finals = [m for m in receipt["messages"] if m.get("role") == "assistant" and not m.get("streaming") and m.get("text", "").strip()]
    if not finals:
        if allow_missing:
            return ""
        raise RoomError("Native result has no final response")
    text = finals[-1]["text"].strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    return text


def final_json(directory, request):
    try:
        value = json.loads(final_text(directory, request))
    except ValueError as exc:
        raise RoomError("Native final response must be one JSON object") from exc
    if not isinstance(value, dict):
        raise RoomError("Native final response must be one JSON object")
    return value


def latest(state, purposes):
    items = [r for r in state["requests"].values() if r.get("purpose") in purposes]
    return max(items, key=lambda r: r["created_order"]) if items else None


def agreement(service, directory, state):
    spec = service.spec(directory, state)
    request = latest(state, {"spec_review"})
    if not request or request["spec_record_sha256"] != state["spec_record_sha256"]:
        raise RoomError("The current exact spec requires completed Fable agreement")
    verdict = final_json(directory, request)
    if (request["role"] != "engineer" or request["harness"] != "claude-code" or request["model"] != FABLE_MODEL
            or verdict.get("decision") != "accept" or type(verdict.get("spec_revision")) is not int
            or verdict["spec_revision"] != spec["revision"] or verdict.get("spec_sha256") != spec["sha256"]
            or not isinstance(verdict.get("interpretation"), str) or not verdict["interpretation"].strip()
            or not isinstance(verdict.get("findings"), list) or not all(isinstance(f, str) for f in verdict["findings"])
            or any(f.startswith("BLOCKER:") for f in verdict["findings"])):
        raise RoomError("Fable rejected, incompletely reviewed, or did not accept the exact current spec")
    return {"agreed": True, "spec_revision": spec["revision"], "spec_sha256": spec["sha256"],
            "request_id": request["request_id"], "receipt_sha256": request["receipt_sha256"], "astra_approval": spec["approval"]}


def workspace(service, directory, state):
    from ao_project_room import common_dir, project_path
    binding = state["bindings"].get("engineer")
    if not binding:
        raise RoomError("Bind the prepared native Fable engineer first")
    prepared = ao_delegates.validate_preparation(directory, state, binding["session_id"])
    raw = service.client(state).request("GET", "/desktop/sessions/" + binding["session_id"] + "/workspace")
    path = raw.get("workspacePath")
    if (raw.get("sessionId") != binding["session_id"] or not isinstance(path, str) or not Path(path).is_absolute()
            or str(Path(path).resolve()) != prepared["worktree"]):
        raise RoomError("AO engineer workspace differs from the prepared exact worktree")
    actual = project_path(path)
    if str(common_dir(actual)) != state["git_common_dir"]:
        raise RoomError("Prepared engineer Git identity changed")
    return actual


def handoff_record(directory, state):
    from ao_project_room import read, digest
    if not state.get("handoff"):
        raise RoomError("Create the agreed engineering handoff first")
    value = read(directory / state["handoff"])
    if (digest(value) != state.get("handoff_sha256") or value["spec_record_sha256"] != state["spec_record_sha256"]
            or value["preparation_sha256"] != state.get("preparation_sha256")):
        raise RoomError("Engineering handoff is stale or changed")
    return value


def handoff(service, directory, state, worktree_path):
    from ao_project_room import atomic, digest, git, project_path
    agreed = agreement(service, directory, state)
    actual = workspace(service, directory, state)
    if project_path(worktree_path) != actual:
        raise RoomError("Handoff must use the actual bound engineer workspace")
    ao_delegates.assert_settled(service.root.parent, state)
    relative = "handoffs/" + state["spec_record_sha256"] + ".json"
    if (directory / relative).exists():
        if state.get("handoff") != relative:
            raise RoomError("Cannot replace a historical handoff")
        return handoff_record(directory, state)
    spec = service.spec(directory, state)
    value = {"worktree": str(actual), "baseline_commit": git(actual, "rev-parse", "HEAD"),
             "initial_candidate_sha256": candidate_snapshot(actual)["sha256"], "agreement": agreed,
             "spec_revision": spec["revision"], "spec_sha256": spec["sha256"], "spec_record_sha256": state["spec_record_sha256"],
             "engineer": state["bindings"]["engineer"], "preparation_sha256": state["preparation_sha256"],
             "delegate_sha256": digest(state["delegate"]), "gates": spec["gates"], "authorization": state["authorization"]}
    atomic(directory / relative, value)
    state.update(handoff=relative, handoff_sha256=digest(value), checkpoint=None)
    service.save(directory, state)
    return value


def engineering_report(directory, state, request):
    record = handoff_record(directory, state)
    report = final_json(directory, request)
    if (not ENGINEERING_FIELDS.issubset(report) or request.get("handoff_sha256") != state["handoff_sha256"]
            or report.get("spec_revision") != record["spec_revision"] or type(report.get("spec_revision")) is not int
            or report.get("spec_sha256") != record["spec_sha256"] or report.get("baseline_commit") != record["baseline_commit"]
            or report.get("outcome") not in ("completed", "changes_required", "scope_change")
            or type(report.get("implementation_complete")) is not bool
            or any(not isinstance(report.get(k), list) for k in ("changes", "tests_reported", "review_findings", "remaining_gaps", "backlog", "routing_log"))
            or any(not isinstance(r, dict) or not isinstance(r.get("delegate_job_ids"), list)
                   or not all(isinstance(j, str) for j in r["delegate_job_ids"]) for r in report["routing_log"])):
        raise RoomError("Engineering report is incomplete or refers to another spec/handoff")
    return report


def capture_engineering(directory, state, request):
    from ao_project_room import atomic, digest
    try:
        report = engineering_report(directory, state, request)
        candidate = candidate_snapshot(handoff_record(directory, state)["worktree"])
        relative = "engineering/" + request["request_id"] + ".json"
        record = {"candidate": candidate, "report_sha256": digest(report), "receipt_sha256": request["receipt_sha256"]}
        atomic(directory / relative, record)
        request.update(engineering_record=relative, engineering_record_sha256=digest(record), result_candidate_sha256=candidate["sha256"])
    except (RoomError, ImplementationError, OSError, ValueError, KeyError, TypeError) as exc:
        request["engineering_error"] = str(exc)[:1000]


def engineering_ready(service, directory, state):
    from ao_project_room import digest, read
    agreement(service, directory, state)
    actual = workspace(service, directory, state)
    ao_delegates.assert_settled(service.root.parent, state)
    request = latest(state, {"implementation", "correction"})
    if not request or not request.get("engineering_record"):
        raise RoomError("Acceptance requires a captured completed engineering result")
    report = engineering_report(directory, state, request)
    if report["outcome"] != "completed" or not report["implementation_complete"] or report["remaining_gaps"]:
        raise RoomError("Engineering is incomplete or has remaining gaps")
    record = read(directory / request["engineering_record"])
    if (digest(record) != request["engineering_record_sha256"] or record["report_sha256"] != digest(report)
            or record["receipt_sha256"] != request["receipt_sha256"]
            or candidate_snapshot(actual)["sha256"] != request["result_candidate_sha256"]
            or record["candidate"]["sha256"] != request["result_candidate_sha256"]):
        raise RoomError("Candidate or engineering result changed since Fable completed")
    return request


def packet(service, directory, state, role, purpose, message):
    spec = service.spec(directory, state)
    ao_delegates.validate_preparation(directory, state, state.get("bindings", {}).get("engineer", {}).get("session_id"))
    workspace(service, directory, state)
    ao_delegates.assert_settled(service.root.parent, state)
    if role == "reviewer":
        if purpose != "acceptance_review":
            raise RoomError("Reviewer purpose must be acceptance_review")
        engineering_ready(service, directory, state)
        instruction = "Astra independently reviews the exact Fable candidate and gate evidence. Stay read-only; do not delegate."
    elif purpose == "spec_review":
        count = sum(r.get("purpose") == "spec_review" for r in state["requests"].values())
        if count >= 3:
            raise RoomError("Three Fable spec-review attempts exhausted; preserve evidence and surface the user decision")
        instruction = ("Fable owns engineering interpretation. Review this exact specification read-only, without implementation or delegates. "
                       "Finish with one JSON object: interpretation (text), findings (list of text; blockers start BLOCKER:), "
                       "decision (accept or changes_required), spec_revision and spec_sha256. Routine design choices are yours.")
    elif purpose in ("implementation", "correction"):
        agreement(service, directory, state)
        handoff = handoff_record(directory, state)
        previous = [r for r in state["requests"].values() if r.get("purpose") in ("implementation", "correction")
                    and r.get("handoff_sha256") == state["handoff_sha256"]]
        if (purpose == "implementation" and previous) or (purpose == "correction" and not previous):
            raise RoomError("Use implementation once per handoff, then correction only for known completed work")
        if previous:
            last = max(previous, key=lambda r: r["created_order"])
            previous_text = final_text(directory, last, allow_missing=True)
            try:
                prior = json.loads(previous_text)
            except ValueError:
                prior = None  # known completion may need a report-only correction
            if isinstance(prior, dict) and prior.get("outcome") == "scope_change":
                raise RoomError("Engineering discovered a scope change; revise and agree the specification first")
        policy = ao_delegates.validate_provider(directory, state)
        instruction = ("Fable owns implementation, engineering review and eligible delegation in " + handoff["worktree"] + ". "
                       "Astra will independently accept the current candidate. Do not publish or start extra AO workers. "
                       "Do not change unrelated sessions or weaken the supplied tests. Return one final JSON object with: "
                       + ", ".join(sorted(ENGINEERING_FIELDS)) + ". List changes, tests_reported, review_findings, remaining_gaps, backlog "
                       "and routing_log; each routing_log entry includes delegate_job_ids (list, empty for native-only work). "
                       "Use outcome completed, changes_required or scope_change, and boolean implementation_complete. "
                       "Only completed/true with empty remaining_gaps can be accepted. Record actual limitations. "
                       "Baseline commit: " + handoff["baseline_commit"] + "\n" + policy["policy"]
                       + "\nPinned delegate settings: " + json.dumps(policy["delegate_settings"], sort_keys=True)
                       + "\nThere is no adapter-imposed native model-run deadline. Status/HTTP waits are observation bounds. "
                       "Use bounded deepseek_result chunks; private export file access is not required. Never replay uncertain work.")
    else:
        raise RoomError("Normal engineer purpose must be spec_review, implementation or correction")
    return ("Workflow: Fable engineering with independent Astra acceptance.\n" + instruction
            + "\nExact specification revision " + str(spec["revision"]) + ", SHA256 " + spec["sha256"]
            + "\n<specification>\n" + spec["content"] + "\n</specification>\nAgreed gates: "
            + json.dumps(spec["gates"]) + "\nTask instruction:\n" + message)
