"""Exact-spec Fable engineering and independent Astra acceptance contracts."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

import ao_delegates
import ao_routing
from implementation import candidate_snapshot, ImplementationError
from room import RoomError

FABLE_MODEL = "claude-fable-5-1"
ENGINEERING_FIELDS = {"outcome", "implementation_complete", "changes", "tests_reported", "review_findings",
                      "remaining_gaps", "backlog", "routing_log", "spec_revision", "spec_sha256", "baseline_commit"}
# One-time workflow parts. A retained engineer session receives each part once; every later engineer turn
# carries only the caller's bytes plus the parts the controller has not yet delivered to that session.
PARTS = ("review_contract", "report_contract", "policy", "settings", "routing", "baseline_rule")
# Packets sent before delivered-context notes existed carried these parts in their saved text.
HISTORICAL_PARTS = {"spec_review": ("review_contract",),
                    "implementation": ("report_contract", "policy", "settings", "routing"),
                    "correction": ("report_contract", "policy", "settings", "routing")}


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


def workspace(service, directory, state, check_routing=True):
    from ao_project_room import common_dir, project_path
    binding = state["bindings"].get("engineer")
    if not binding:
        raise RoomError("Bind the prepared native Fable engineer first")
    prepared = ao_delegates.validate_preparation(directory, state, binding["session_id"], check_routing=check_routing)
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
    ao_delegates.assert_settled(service.root.parent, state, directory)
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
    """Pure shape validation of the saved final JSON; delegate evidence is verified at the lifecycle boundaries."""
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


def capture_engineering(home, directory, state, request):
    from ao_project_room import atomic, digest
    try:
        report = engineering_report(directory, state, request)
        # The claim is recorded before verification so a ledger lost afterwards still counts as recorded delegation.
        request["reported_delegate_job_ids"] = ao_delegates.report_job_ids(report)
        evidence = ao_delegates.verify_delegation(home, directory, state, report)
        candidate = candidate_snapshot(handoff_record(directory, state)["worktree"])
        relative = "engineering/" + request["request_id"] + ".json"
        record = {"candidate": candidate, "report_sha256": digest(report), "receipt_sha256": request["receipt_sha256"],
                  "delegation": evidence}
        atomic(directory / relative, record)
        request.update(engineering_record=relative, engineering_record_sha256=digest(record), result_candidate_sha256=candidate["sha256"],
                       delegate_job_ids=[item["job_id"] for item in evidence])
    except (RoomError, ImplementationError, OSError, ValueError, KeyError, TypeError) as exc:
        request["engineering_error"] = str(exc)[:1000]


def engineering_ready(service, directory, state):
    from ao_project_room import digest, read
    agreement(service, directory, state)
    actual = workspace(service, directory, state, check_routing=False)  # acceptance never delegates
    ao_delegates.assert_settled(service.root.parent, state, directory)
    request = latest(state, {"implementation", "correction"})
    if not request or not request.get("engineering_record"):
        raise RoomError("Acceptance requires a captured completed engineering result")
    report = engineering_report(directory, state, request)
    if report["outcome"] != "completed" or not report["implementation_complete"] or report["remaining_gaps"]:
        raise RoomError("Engineering is incomplete or has remaining gaps")
    # Re-verify the named delegate jobs live; a record captured before this evidence existed is verified live only.
    evidence = ao_delegates.verify_delegation(service.root.parent, directory, state, report)
    record = read(directory / request["engineering_record"])
    if (digest(record) != request["engineering_record_sha256"] or record["report_sha256"] != digest(report)
            or record["receipt_sha256"] != request["receipt_sha256"]
            or candidate_snapshot(actual)["sha256"] != request["result_candidate_sha256"]
            or record["candidate"]["sha256"] != request["result_candidate_sha256"]):
        raise RoomError("Candidate or engineering result changed since Fable completed")
    if record.get("delegation", evidence) != evidence:
        raise RoomError("Delegate evidence changed since Fable completed")
    return request


def part_texts(prepared, policy):
    """The pinned one-time workflow parts for this room, keyed by stable part name."""
    return {
        "review_contract": ("Workflow: Fable engineering with independent Astra acceptance.\nSpecification review turns: Fable owns "
                            "engineering interpretation. Review the exact specification delivered to this session read-only, without "
                            "implementation or delegates; a later revision arrives as its changes only. Finish with one JSON object: "
                            "interpretation (text), findings (list of text; blockers start BLOCKER:), decision (accept or "
                            "changes_required), spec_revision and spec_sha256. Routine design choices are yours."),
        "report_contract": ("Implementation and correction turns: Fable owns implementation, engineering review and eligible delegation "
                            "in the bound AO workspace, which is the candidate worktree. Astra will independently accept the current "
                            "candidate. Do not publish or start extra AO workers. Do not change unrelated sessions or weaken the "
                            "supplied tests. Return one final JSON object with: " + ", ".join(sorted(ENGINEERING_FIELDS)) + ". List "
                            "changes, tests_reported, review_findings, remaining_gaps, backlog and routing_log; each routing_log entry "
                            "includes delegate_job_ids (list, empty for native-only work; name only this room's own delegate jobs). "
                            "Use outcome completed, changes_required or scope_change, and boolean implementation_complete. Only "
                            "completed/true with empty remaining_gaps can be accepted. Record actual limitations. Later implementation "
                            "and correction turns carry only the caller's new instruction; this contract keeps applying to them."),
        "policy": policy["policy"],
        "settings": ("Pinned delegate settings: " + json.dumps(policy["delegate_settings"], sort_keys=True)
                     + "\nThere is no adapter-imposed native model-run deadline. Status/HTTP waits are observation bounds. "
                     "Use bounded deepseek_result chunks; private export file access is not required. Never replay uncertain work."),
        "routing": ao_routing.packet_text(prepared),
        "baseline_rule": ("baseline_commit in the engineering report is the bound worktree HEAD at the start of the implementation "
                          "turn, before any commit of your own; corrections for the same handoff report that same baseline."),
    }


def carried_by(request):
    """What one completed engineer packet delivered: its recorded note, or the historical packet contents."""
    carried = request.get("carried")
    if isinstance(carried, dict):
        names = carried.get("parts")
        hashes = carried.get("part_sha256")
        record, delivery = carried.get("spec_record_sha256"), carried.get("spec_delivery")
        if (not isinstance(names, list) or any(n not in PARTS for n in names) or len(set(names)) != len(names)
                or not isinstance(hashes, dict) or set(hashes) != set(names)
                or any(not isinstance(h, str) or not ao_delegates.CONTENT_DIGEST.fullmatch(h) for h in hashes.values())
                or delivery not in (None, "full", "changes") or (delivery is None) != (record is None)
                or (record is not None and record != request.get("spec_record_sha256"))):
            raise RoomError("Delivered-context record is inconsistent; preserve the evidence, do not resend automatically")
        return carried.get("spec_record_sha256"), tuple(carried.get("parts") or ())
    if "carried" in request:
        raise RoomError("Delivered-context record is malformed")
    purpose = request.get("purpose")
    if purpose not in HISTORICAL_PARTS or not request.get("spec_record_sha256"):
        raise RoomError("Completed engineer request " + str(request.get("request_id")) + " cannot be classified for delivered context; "
                        "inspect its saved record before sending more context, nothing is re-sent automatically")
    return request["spec_record_sha256"], HISTORICAL_PARTS[purpose]


def delivered(state, session_id, directory=None):
    """Context the controller itself delivered to one native session, from completed observed turns only."""
    spec_record, parts = None, set()
    completed = [r for r in state["requests"].values()
                 if r.get("role") == "engineer" and r.get("session_id") == session_id and r.get("state") == "completed"]
    for request in sorted(completed, key=lambda r: r["created_order"]):
        if directory is not None:
            from ao_project_room import digest, sent_message
            receipt = completed_receipt(directory, request)
            if (digest(request["text"].encode()) != request["text_sha256"] or not sent_message(request, receipt)
                    or ("carried" in request and receipt.get("carried_sha256") != digest(request["carried"]))):
                raise RoomError("Delivered-context evidence does not match the immutable native receipt")
            if "carried" not in request:
                spec = spec_record_file(directory, request["spec_record_sha256"])
                expected = ("Exact specification revision " + str(spec["revision"]) + ", SHA256 " + spec["sha256"]
                            + "\n<specification>\n" + spec["content"] + "\n</specification>\nAgreed gates: " + json.dumps(spec["gates"]))
                if expected not in request["text"]:
                    raise RoomError("Historical engineer packet does not contain its recorded canonical specification")
        record, names = carried_by(request)
        if record:
            spec_record = record
        parts.update(names)
    return {"spec_record_sha256": spec_record, "parts": sorted(parts), "completed_requests": len(completed)}


def context_summary(state):
    """Offline status of what the bound engineer session has been sent; never an agent claim."""
    binding = state.get("bindings", {}).get("engineer")
    if not binding:
        return None
    try:
        held = delivered(state, binding["session_id"])
    except RoomError as exc:
        return {"session_id": binding["session_id"], "error": str(exc)}
    return {"session_id": binding["session_id"], "spec_record_sha256": held["spec_record_sha256"],
            "spec_current": held["spec_record_sha256"] == state.get("spec_record_sha256"),
            "parts": held["parts"], "undelivered_parts": [p for p in PARTS if p not in held["parts"]],
            "meaning": "Derived from the controller's completed observed turns, never from agent claims; later engineer turns carry "
                       "only the caller's bytes plus undelivered parts"}


def spec_record_file(directory, record_sha256):
    from ao_project_room import digest, read
    for path in sorted((directory / "specs").glob("*.json")):
        try:
            spec = read(path)
        except (OSError, ValueError):
            continue
        if digest(spec) == record_sha256:
            return spec
    raise RoomError("The specification record already delivered to the engineer session is missing from the room's immutable "
                    "specs; restore that private spec file or supply the needed requirement text as caller clarification. "
                    "No context is re-sent automatically")


def spec_changes(previous, spec):
    """Only the changed lines between two canonical spec bodies: a zero-context unified diff, never the unchanged text."""
    if previous["content"] == spec["content"]:
        return ""
    # difflib's popular-line heuristic can repeat hundreds of unchanged lines for one small edit. Disabling it
    # has quadratic behavior on repetitive input. Git is already required by AO rooms; its minimal diff keeps
    # these edits small, preserves LF/CR and missing-final-newline bytes, and runs under a fixed local timeout.
    try:
        with tempfile.TemporaryDirectory(prefix="project-room-spec-diff-") as temporary:
            root = Path(temporary)
            (root / "previous").write_bytes(previous["content"].encode())
            (root / "current").write_bytes(spec["content"].encode())
            result = subprocess.run(["git", "diff", "--no-index", "--diff-algorithm=minimal", "--no-indent-heuristic",
                                     "--unified=0", "--inter-hunk-context=0",
                                     "--no-ext-diff", "--no-textconv", "--no-color", "--text", "--", "previous", "current"],
                                    cwd=root, capture_output=True, timeout=30,
                                    env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoomError("Cannot compute specification changes; nothing is sent automatically") from exc
    patch = result.stdout.decode("utf-8")
    start = patch.find("\n@@ ")
    if result.returncode != 1 or start < 0:
        raise RoomError("Cannot compute specification changes; nothing is sent automatically")
    # Git may append an unchanged section title to a hunk header even with zero context; omit that title too.
    body = "\n".join(line.split(" @@", 1)[0] + " @@" if line.startswith("@@ ") else line
                     for line in patch[start:].split("\n"))
    if any(line.startswith(" ") for line in body.split("\n")):
        raise RoomError("Specification diff unexpectedly contains unchanged context; nothing is sent automatically")
    return "--- revision " + str(previous["revision"]) + "\n+++ revision " + str(spec["revision"]) + body


def packet(service, directory, state, role, purpose, message):
    """One native message. Every controller check stays; only text the session already holds is omitted."""
    from ao_project_room import digest
    spec = service.spec(directory, state)
    # Routing gates only delegation-capable turns; spec review and acceptance review stay read-only.
    delegating = role == "engineer" and purpose in ("implementation", "correction")
    binding = state.get("bindings", {}).get("engineer", {})
    prepared = ao_delegates.validate_preparation(directory, state, binding.get("session_id"), check_routing=delegating)
    workspace(service, directory, state, check_routing=delegating)
    ao_delegates.assert_settled(service.root.parent, state, directory)
    if role == "reviewer":
        if purpose != "acceptance_review":
            raise RoomError("Reviewer purpose must be acceptance_review")
        engineering_ready(service, directory, state)
        instruction = "Astra independently reviews the exact Fable candidate and gate evidence. Stay read-only; do not delegate."
        return ("Workflow: Fable engineering with independent Astra acceptance.\n" + instruction
                + "\nExact specification revision " + str(spec["revision"]) + ", SHA256 " + spec["sha256"]
                + "\n<specification>\n" + spec["content"] + "\n</specification>\nAgreed gates: "
                + json.dumps(spec["gates"]) + "\nTask instruction:\n" + message), None
    if purpose == "spec_review":
        count = sum(r.get("purpose") == "spec_review" for r in state["requests"].values())
        if count >= 3:
            raise RoomError("Three Fable spec-review attempts exhausted; preserve evidence and surface the user decision")
    elif purpose in ("implementation", "correction"):
        agreement(service, directory, state)
        handoff = handoff_record(directory, state)
        if purpose == "implementation" and candidate_snapshot(handoff["worktree"])["head"] != handoff["baseline_commit"]:
            raise RoomError("Engineer HEAD changed since handoff; reconcile the baseline before the first implementation turn")
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
            if isinstance(prior, dict):
                ao_delegates.verify_delegation(service.root.parent, directory, state, prior)
        ao_routing.before_dispatch(service, directory, state, prepared, purpose)
    else:
        raise RoomError("Normal engineer purpose must be spec_review, implementation or correction")
    policy = ao_delegates.validate_provider(directory, state)
    texts = part_texts(prepared, policy)
    held = delivered(state, binding["session_id"], directory)
    sections = []
    carried = {"spec_record_sha256": None, "spec_delivery": None, "parts": [], "part_sha256": {}}
    for name in PARTS:
        if name not in held["parts"]:
            sections.append(texts[name])
            carried["parts"].append(name)
            carried["part_sha256"][name] = digest(texts[name].encode())
    if held["spec_record_sha256"] != state["spec_record_sha256"]:
        carried["spec_record_sha256"] = state["spec_record_sha256"]
        if held["spec_record_sha256"] is None:
            carried["spec_delivery"] = "full"
            sections.append("Exact specification revision " + str(spec["revision"]) + ", SHA256 " + spec["sha256"]
                            + "\n<specification>\n" + spec["content"] + "\n</specification>\nAgreed gates: " + json.dumps(spec["gates"]))
        else:
            previous = spec_record_file(directory, held["spec_record_sha256"])
            carried.update(spec_delivery="changes", base_revision=previous["revision"])
            block = ("Specification revision " + str(spec["revision"]) + ", SHA256 " + spec["sha256"] + ": only its changes from "
                     "delivered revision " + str(previous["revision"]) + " follow; unchanged requirements are not repeated.\n"
                     "<specification-changes>\n" + (spec_changes(previous, spec) or "No specification text changes.\n")
                     + "</specification-changes>")
            if previous["gates"] != spec["gates"]:
                block += "\nAgreed gates: " + json.dumps(spec["gates"])
            sections.append(block)
    sections.append(message)
    return "\n".join(sections), carried
