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
PARTS = ("review_contract", "report_contract", "policy", "settings", "routing", "baseline_rule", "efficiency_contract_v1")
# Packets sent before delivered-context notes existed carried these parts in their saved text.
HISTORICAL_PARTS = {"spec_review": ("review_contract",),
                    "implementation": ("report_contract", "policy", "settings", "routing"),
                    "correction": ("report_contract", "policy", "settings", "routing")}


def normal(state):
    return state.get("workflow") == "fable_engineering"


def completed_receipt(directory, request):
    from ao_project_room import digest, read, native_turn_identity
    if request.get("state") not in ('completed', 'settled_failure') or not native_turn_identity(request) or request.get("model_reroute"):
        raise RoomError("A completed native turn with uncontradicted identity is required")
    if request.get('state') == 'settled_failure':
        from ao_outcomes import validate_settlement
        validate_settlement(directory, request)
    if not request.get("receipt"):
        raise RoomError("The native result has no saved receipt")
    receipt = read(directory / request["receipt"])
    if digest(receipt) != request.get("receipt_sha256"):
        raise RoomError("Native result receipt was modified")
    return receipt


def raw_final_text(directory, request, allow_missing=False):
    receipt = completed_receipt(directory, request)
    finals = [m for m in receipt["messages"] if m.get("role") == "assistant" and not m.get("streaming") and m.get("text", "").strip()]
    if not finals:
        if allow_missing:
            return ""
        raise RoomError("Native result has no final response")
    return finals[-1]["text"]


def final_text(directory, request, allow_missing=False):
    text = raw_final_text(directory, request, allow_missing=allow_missing).strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    return text


def final_json(directory, request, allow_missing=False):
    from ao_response_normalization import load_result, strict_object
    raw = raw_final_text(directory, request, allow_missing=allow_missing)
    if "response_normalization" in request or "response_normalization_sha256" in request:
        return load_result(directory, request, raw)
    return strict_object(raw)


def latest(state, purposes):
    items = [r for r in state["requests"].values() if r.get("purpose") in purposes]
    return max(items, key=lambda r: r["created_order"]) if items else None


def agreement(service, directory, state):
    spec = service.spec(directory, state)
    request = latest(state, {"spec_review"})
    if not request or request["spec_record_sha256"] != state["spec_record_sha256"]:
        raise RoomError("The current exact spec requires completed Fable agreement")
    from ao_outcomes import usable
    usable(directory, request)
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
    expected = state.get("preparation_sha256")
    transition = state.get("provider_transition")
    if isinstance(transition, dict) and transition.get("original_preparation_sha256"):
        try:
            epoch = json.loads(ao_delegates.owned_bytes(directory / transition["epoch_record"]))
            if digest(epoch) != transition.get("epoch_sha256"):
                raise RoomError("Provider epoch handoff reference was modified")
            if state["handoff"] == epoch["handoff"]["path"]:
                expected = transition["original_preparation_sha256"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RoomError("Provider epoch handoff reference is unreadable or inconsistent") from exc
    if (digest(value) != state.get("handoff_sha256") or value["spec_record_sha256"] != state["spec_record_sha256"]
            or value["preparation_sha256"] != expected):
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


def engineering_report(directory, state, request, report=None):
    """Pure shape validation of the saved final JSON; delegate evidence is verified at the lifecycle boundaries."""
    record = handoff_record(directory, state)
    if report is None:
        report = final_json(directory, request)
    from ao_report_contract import project
    report = project(directory, state, request, report)
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


def completion_candidate(directory, state, request, capture=False):
    """Bind a candidate to the first completed observation, even when report formatting is invalid."""
    from ao_project_room import atomic, digest, read
    handoff = handoff_record(directory, state)
    relative = request.get("completion_candidate")
    if relative:
        value = read(directory / relative)
        if (digest(value) != request.get("completion_candidate_sha256")
                or value.get("receipt_sha256") != request["receipt_sha256"]
                or value.get("handoff_sha256") != request.get("handoff_sha256")
                or value.get("handoff_sha256") != state["handoff_sha256"]):
            raise RoomError("Completion candidate evidence was modified or belongs to another result")
        return value["candidate"]
    if not capture or request.get("completion_capture_attempted"):
        raise RoomError("No immutable candidate-at-completion evidence; do not recapture historical work")
    request["completion_capture_attempted"] = True
    candidate = candidate_snapshot(handoff["worktree"])
    value = {"candidate": candidate, "receipt_sha256": request["receipt_sha256"], "handoff_sha256": state["handoff_sha256"]}
    relative = "completion-candidates/" + request["request_id"] + ".json"
    if (directory / relative).exists():
        raise RoomError("Unclaimed completion candidate evidence exists; preserve it for diagnosis")
    atomic(directory / relative, value)
    request.update(completion_candidate=relative, completion_candidate_sha256=digest(value))
    return candidate


def capture_engineering(home, directory, state, request, capture_completion=True):
    from ao_project_room import atomic, digest
    try:
        completed_candidate = completion_candidate(directory, state, request, capture=capture_completion)
        from ao_outcomes import usable
        usable(directory, request)
        report = engineering_report(directory, state, request)
        # The claim is recorded before verification so a ledger lost afterwards still counts as recorded delegation.
        request["reported_delegate_job_ids"] = ao_delegates.report_job_ids(report)
        evidence = ao_delegates.verify_delegation(home, directory, state, report)
        candidate = candidate_snapshot(handoff_record(directory, state)["worktree"])
        if candidate != completed_candidate:
            raise RoomError("Candidate changed after the completed native result")
        relative = "engineering/" + request["request_id"] + ".json"
        record = {"candidate": candidate, "report_sha256": digest(report), "receipt_sha256": request["receipt_sha256"],
                  "delegation": evidence}
        if 'controller_metadata' in report:
            record['controller_metadata'] = report['controller_metadata']
        if request.get("provider_epoch") is not None:
            record["provider_epoch"] = request["provider_epoch"]
        atomic(directory / relative, record)
        request.update(engineering_record=relative, engineering_record_sha256=digest(record), result_candidate_sha256=candidate["sha256"],
                       delegate_job_ids=[item["job_id"] for item in evidence])
    except (RoomError, ImplementationError, OSError, ValueError, KeyError, TypeError) as exc:
        # A derived-envelope capture is a separate observation, not permission to erase the initial failure.
        field = "normalization_capture_error" if not capture_completion else "engineering_error"
        request[field] = str(exc)[:1000]


def engineering_ready(service, directory, state):
    from ao_project_room import digest, read
    agreement(service, directory, state)
    actual = workspace(service, directory, state, check_routing=False)  # acceptance never delegates
    ao_delegates.assert_settled(service.root.parent, state, directory)
    request = latest(state, {"implementation", "correction"})
    if not request or not request.get("engineering_record"):
        raise RoomError("Acceptance requires a captured completed engineering result")
    from ao_outcomes import usable, observe
    observe(service, directory, state, request, service.identity(service.client(state), state, request))
    usable(directory, request)
    if state.get("provider_transition") and request.get("provider_epoch") != 2:
        raise RoomError("Acceptance requires a completed engineering result from the current provider epoch; historical results stay historical")
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
    if state.get("provider_transition") and record.get("provider_epoch") != request.get("provider_epoch"):
        raise RoomError("Captured engineering result belongs to another provider epoch")
    return request


def part_texts(prepared, policy):
    """The pinned one-time workflow parts for this room, keyed by stable part name."""
    parts = {
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
    if (prepared.get("routing") or {}).get("version") == 2:
        parts["report_contract"] = parts["report_contract"].replace(
            "Fable owns implementation, engineering review and eligible delegation",
            "Fable directs implementation, delegates execution, reviews evidence and owns the engineering verdict")
        parts["report_contract"] += (
            " Execution ownership includes validation probes and tests: use the assigned operator or pinned workers, "
            "and request missing evidence instead of running their work yourself. Use the supplied spec and baseline "
            "identifiers; the controller checks their integrity. Keep the engineering verdict grounded in the complete "
            "evidence, without reconstructing delegate work just to report it.")
        for name in ("review_contract", "report_contract"):
            parts[name] += " The entire final response must be that JSON object, with no preamble, Markdown fence or trailing prose."
    return parts


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
                 if r.get("role") == "engineer" and r.get("session_id") == session_id and r.get("state") in ('completed', 'settled_failure')]
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


def packet(service, directory, state, role, purpose, message, snapshot=None):
    """One native message. Every controller check stays; only text the session already holds is omitted."""
    from ao_project_room import digest
    spec = service.spec(directory, state)
    # Specification and acceptance stay read-only. Live routing-rule observations apply to delegation-capable
    # turns; every transitioned engineer turn separately requires the native attachment gate below.
    delegating = role == "engineer" and purpose in ("implementation", "correction")
    epoch = None
    correction_admission = None
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
            from ao_response_normalization import ResponseFormatError
            try:
                prior = final_json(directory, last, allow_missing=True) if last["state"] == "completed" else None
            except ResponseFormatError:
                prior = None  # known completion may need a report-only correction
            if isinstance(prior, dict) and prior.get("outcome") == "scope_change":
                raise RoomError("Engineering discovered a scope change; revise and agree the specification first")
            if isinstance(prior, dict):
                prior_state = state
                if state.get("provider_transition"):
                    from ao_provider_transition import report_state
                    prior_state = report_state(directory, state, last)
                try:
                    from ao_report_contract import project
                    prior = project(directory, prior_state, last, prior)
                    ao_delegates.verify_delegation(service.root.parent, directory, prior_state, prior)
                except RoomError:
                    if purpose != "correction" or not state.get("provider_transition") or last.get("provider_epoch") != 2:
                        raise
                    from ao_provider_transition import historical_correction
                    if snapshot is None:
                        snapshot = service.identity(service.client(state), state, binding)
                    correction_admission = historical_correction(service, directory, state, last, prior, snapshot)
    else:
        raise RoomError("Normal engineer purpose must be spec_review, implementation or correction")
    # Every new engineer turn belongs to the active epoch, including read-only specification review.
    # Qualify routing/native attachment before any request intent can be persisted or sent to AO.
    if state.get("provider_transition"):
        import ao_provider_transition
        if snapshot is None:
            snapshot = service.identity(service.client(state), state, state["bindings"]["engineer"])
        epoch = ao_provider_transition.dispatch_gate(service, directory, state, snapshot)
    if delegating:
        ao_routing.before_dispatch(service, directory, state, prepared, purpose)
    policy = ao_delegates.validate_provider(directory, state)
    texts = part_texts(prepared, policy)
    from ao_report_contract import PART, INSTRUCTION
    texts[PART] = INSTRUCTION  # A new one-time amendment, never a rewrite of frozen workflow bytes.
    held = delivered(state, binding["session_id"], directory)
    sections = []
    carried = {"spec_record_sha256": None, "spec_delivery": None, "parts": [], "part_sha256": {}}
    if correction_admission is not None:
        carried["correction_admission"] = correction_admission  # audit metadata only, never appended to the native prompt
    for name in PARTS:
        if name not in held["parts"]:
            sections.append(texts[name])
            carried["parts"].append(name)
            carried["part_sha256"][name] = digest(texts[name].encode())
    if epoch is not None:
        import ao_provider_transition
        if not ao_provider_transition.amendment_delivered(directory, state):
            sections.append(ao_provider_transition.amendment_text(epoch))
            carried["provider_amendment_sha256"] = state["provider_transition"]["epoch_sha256"]
            if state.get("routing_adoption"):
                from ao_routing_adoption import INSTRUCTION
                sections.append(INSTRUCTION)
                carried["routing_amendment_sha256"] = digest(INSTRUCTION.encode())
                carried["routing_adoption_sha256"] = state["routing_adoption"]["receipt_sha256"]
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
    from ao_instruction_amendments import pending
    amendments = pending(directory, state, binding['session_id'])
    if amendments:
        sections.extend(text for text, _ in amendments)
        carried['instruction_amendments'] = [sha for _, sha in amendments]
    sections.append(message)
    return "\n".join(sections), carried
