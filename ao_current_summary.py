"""Read-only current-summary projection for the AO status and progress views.

current_summary discloses what the room's saved records say at this exact read: the latest
engineering request, the newest digest-checked captured result, the delivery ladder rungs
(preparation, dispatch, reconciliation, engineering verdict, formal verification, independent
acceptance), how each verified or accepted candidate relates to the latest captured baseline,
and a closed set of inconsistency codes when those saved facts conflict. Every entry is a
disclosure derived from one saved record; none is a verdict, a gate, a hold release or
acceptance — ao_room_verify and ao_room_accept remain the checks that decide. The object reads
no transcript, no worktree and no model, and never raises out of a status call.
"""
import ao_findings
import ao_progress
import ao_workflow

VERSION = 1
BASIS = ("Saved room records only; task rows appear only inside ao_room_progress, where the view "
         "already folded them; nothing is inferred from labels; this object reads no transcript, "
         "worktree, model or network.")
MEANING = ("Every entry is a disclosure derived from one saved record; none is a verdict, a gate, "
           "a hold release or acceptance; ao_room_verify and ao_room_accept remain the checks "
           "that decide.")
MAX_INCONSISTENCIES = 8
# Codes whose condition reads the progress view's folded task rows; the status surface cannot
# produce them.
PROGRESS_ONLY = ("in_progress_units_without_live_engineer_turn", "open_units_after_complete_engineering_report",
                 "completed_units_with_incomplete_engineering_report")
STATUS_PLAN_CHECKS = {"available": False, "basis": None,
                      "reason": "status reads no transcript; see ao_room_progress"}


class _Bounds:
    """Bounded text like the sibling view's own fields; every clip marks the object truncated."""

    def __init__(self):
        self.truncated = False

    def ident(self, value):
        text, clipped = ao_progress._bounded_text(value, ao_progress.MAX_ID_CHARS)
        self.truncated = self.truncated or clipped
        return text

    def key(self, value):
        text, clipped = ao_progress._bounded_text(value, ao_progress.MAX_KEY_CHARS)
        self.truncated = self.truncated or clipped
        return text


def _engineering_requests(requests):
    return [item for item in requests.values()
            if isinstance(item, dict) and item.get("purpose") in ao_progress._ENGINEERING_PURPOSES]


def _captured(directory, engineering):
    """(request, record) of the newest engineering request whose saved record passes the same
    digest, candidate and report checks _engineering_stage uses for its 'captured' status."""
    for request in sorted(engineering, key=ao_progress._request_order, reverse=True):
        record, valid = ao_progress._engineering_record(directory, request)
        if valid is not True or not isinstance(record, dict):
            continue
        candidate = ao_progress._sha256_or_none(request.get("result_candidate_sha256"))
        saved = record.get("candidate")
        report_sha256 = ao_progress._sha256_or_none(record.get("report_sha256"))
        if candidate is not None and report_sha256 is not None and isinstance(saved, dict) \
                and saved.get("sha256") == candidate:
            return request, record
    return None, None


def _request_line(request, bounds):
    semantic, clipped = ao_progress._semantic_status_view(request.get("semantic_status"))
    bounds.truncated = bounds.truncated or clipped
    observed = request.get("observed_turn")
    return {"request_id": bounds.ident(request.get("request_id")),
            "purpose": bounds.key(request.get("purpose")),
            "state": bounds.key(request.get("state")),
            "ao_turn_state": bounds.key(observed.get("state")) if isinstance(observed, dict) else None,
            "semantic": ({"kind": semantic["kind"], "hold": semantic["hold"]}
                         if isinstance(semantic, dict) else None),
            "delivery_error": bool(request.get("delivery_error"))}


def _dispatch(latest, bounds):
    status = "none"
    if latest is not None:
        recorded = latest.get("state")
        if recorded == "settled_failure":
            status = "settled_failure"
        elif recorded == "uncertain":
            status = "returned" if isinstance(latest.get("observed_turn"), dict) else "uncertain_delivery"
        elif recorded == "submitted":
            status = "dispatched"
        elif recorded == "running":
            status = "running"
        elif recorded == "completed":
            status = "returned"
        # An unrecognized recorded state is never mapped to a named rung.
    return {"status": status,
            "request_id": bounds.ident(latest.get("request_id")) if latest is not None else None}


def _verdict(directory, state, captured, latest, bounds):
    verdict = {"status": "none", "request_id": None, "is_latest_request": None, "outcome": None,
               "implementation_complete": None, "remaining_gaps": None,
               "required_open_findings": None, "tests_reported": None, "reason": None}
    if captured is None:
        return verdict
    unavailable = {"status": "unavailable",
                   "request_id": bounds.ident(captured.get("request_id")),
                   "is_latest_request": captured is latest}
    # A capture from a superseded handoff can never pass the report's own identity check; the
    # saved handoff digest names that history directly, so the report file is not even opened.
    if captured.get("handoff_sha256") != state.get("handoff_sha256"):
        verdict.update(unavailable, reason="historical_handoff")
        return verdict
    from ao_response_normalization import ResponseFormatError
    try:
        report = ao_workflow.engineering_report(directory, state, captured)
        findings = ao_findings.project(report)
    except ao_progress._STAGE_ERRORS + (ResponseFormatError,):
        verdict.update(unavailable, reason="report_unreadable")
        return verdict
    outcome = report.get("outcome")
    complete = report.get("implementation_complete")
    gaps = report.get("remaining_gaps")
    tests = report.get("tests_reported")
    verdict.update(request_id=bounds.ident(captured.get("request_id")),
                   is_latest_request=captured is latest,
                   outcome=bounds.key(outcome),
                   implementation_complete=complete if isinstance(complete, bool) else None,
                   remaining_gaps=len(gaps) if isinstance(gaps, list) else None,
                   required_open_findings=findings["required_open"],
                   tests_reported=len(tests) if isinstance(tests, list) else None)
    verdict["status"] = ("reported_complete"
                         if outcome == "completed" and complete is True and not gaps
                         else "reported_incomplete")
    return verdict


def _relation(stage, record_sha256, spec_sha256, captured_candidate, later_request, state, anchor):
    """How the stage's verified or accepted evidence relates to the latest captured baseline."""
    if stage.get("status") in (None, "missing", "error"):
        return "none"
    stage_candidate = stage.get("candidate_sha256")
    if stage_candidate is not None and captured_candidate is not None \
            and stage_candidate != captured_candidate:
        return "superseded_by_later_candidate"
    record_match = ao_progress._identity_match(
        record_sha256, ao_progress._sha256_or_none(state.get("spec_record_sha256")))
    spec_match = ao_progress._identity_match(
        spec_sha256, ao_progress._sha256_or_none(anchor.get("spec_sha256")))
    if record_match == "mismatched" or spec_match == "mismatched":
        return "spec_revised_since"
    if later_request is not None:
        if later_request.get("state") in ao_progress._LIVE_REQUEST_STATES:
            return "baseline_for_live_turn"
        if later_request.get("state") in ("uncertain", "settled_failure"):
            return "baseline_for_unsettled_turn"
        return "baseline_for_uncaptured_turn"
    if stage_candidate is not None and stage_candidate == captured_candidate \
            and record_match == "matches" and spec_match == "matches":
        return "current"
    return "unknown"


def _plan_checks(view):
    source = view.get("source") if isinstance(view.get("source"), dict) else {}
    plan = view.get("plan") if isinstance(view.get("plan"), dict) else {}
    if not source.get("available"):
        return {"available": False, "basis": None, "reason": "transcript unavailable"}
    if plan.get("source") == "none":
        return {"available": False, "basis": None, "reason": "no task tool activity observed"}
    deliverables = view.get("deliverables") if isinstance(view.get("deliverables"), dict) else {}
    basis = "deliverables" if deliverables.get("available") else "plan_counts"
    return {"available": True, "basis": basis, "reason": None}


def _open_units(view):
    """(open units, blockers) from the deliverables closure when available, else plan counts."""
    deliverables = view.get("deliverables") if isinstance(view.get("deliverables"), dict) else {}
    if deliverables.get("available"):
        closure = deliverables.get("closure") if isinstance(deliverables.get("closure"), dict) else {}
        return closure.get("required_open"), closure.get("blockers")
    counts = (view.get("plan") or {}).get("counts") or {}
    return (counts.get("pending") or 0) + (counts.get("in_progress") or 0), None


def _record_codes(latest, captured, verification_status, acceptance_status,
                  verification_relation, acceptance_relation, verdict, later, bounds):
    """The closed inconsistency codes derived from saved records alone; identical on every
    surface."""
    codes = []
    for code, decided, status, relation in (("verification_not_current", "passed",
                                             verification_status, verification_relation),
                                            ("acceptance_not_current", "approved",
                                             acceptance_status, acceptance_relation)):
        # Only a decided rung (passed/approved) is an approval that can go stale; pending,
        # failed, rejected and error rungs keep their status and relation on the ladder.
        if status == decided and relation not in ("current", "unknown"):
            codes.append({"code": code, "kind": "stale",
                          "detail": {"relation": relation,
                                     "later_request_id": bounds.ident(later.get("request_id"))
                                     if later is not None else None,
                                     "later_request_state": bounds.key(later.get("state"))
                                     if later is not None else None}})
    if latest is not None and latest.get("state") == "uncertain" \
            and not isinstance(latest.get("observed_turn"), dict):
        codes.append({"code": "engineering_turn_delivery_uncertain", "kind": "unknown",
                      "detail": {"request_id": bounds.ident(latest.get("request_id")),
                                 "delivery_error": bool(latest.get("delivery_error")),
                                 "reconciliation": bool(latest.get("reconciliation"))}})
    if latest is not None and (latest.get("engineering_record")
                               or latest.get("engineering_record_sha256")) \
            and captured is not latest:
        codes.append({"code": "engineering_record_invalid", "kind": "contradiction",
                      "detail": {"request_id": bounds.ident(latest.get("request_id"))}})
    if verdict["status"] == "reported_complete" and (verdict["required_open_findings"] or 0) > 0:
        codes.append({"code": "engineering_complete_reported_with_open_required_findings",
                      "kind": "contradiction",
                      "detail": {"request_id": verdict["request_id"],
                                 "required_open_findings": verdict["required_open_findings"]}})
    return codes


def _progress_codes(summary, view):
    """The closed codes that additionally read the progress view's folded task rows."""
    codes = []
    plan = view.get("plan") if isinstance(view.get("plan"), dict) else {}
    source = view.get("source") if isinstance(view.get("source"), dict) else {}
    if plan.get("source") == "none" or not source.get("available"):
        return codes
    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    provenance = view.get("provenance") if isinstance(view.get("provenance"), dict) else {}
    provenance_latest = provenance.get("latest_engineer_request")
    provenance_latest = provenance_latest if isinstance(provenance_latest, dict) else None
    semantic = provenance_latest.get("semantic_status") if provenance_latest is not None else None
    if (counts.get("in_progress") or 0) > 0 and (provenance_latest is None
                                               or provenance_latest.get("state") not in
                                               ao_progress._LIVE_REQUEST_STATES):
        codes.append({"code": "in_progress_units_without_live_engineer_turn", "kind": "stale",
                      "detail": {"in_progress": counts.get("in_progress"),
                                 "latest_engineer_request_id":
                                     (provenance_latest or {}).get("request_id"),
                                 "latest_engineer_request_state":
                                     (provenance_latest or {}).get("state"),
                                 "semantic_hold": (semantic or {}).get("hold")
                                     if isinstance(semantic, dict) else None,
                                 "semantic_kind": (semantic or {}).get("kind")
                                     if isinstance(semantic, dict) else None}})
    open_units, blockers = _open_units(view)
    verdict = summary["ladder"]["engineering_verdict"]
    captured_is_latest = bool((summary["engineering_line"]["latest_captured"] or {})
                              .get("is_latest_request"))
    if verdict["status"] == "reported_complete" and captured_is_latest \
            and isinstance(open_units, int) and open_units > 0:
        codes.append({"code": "open_units_after_complete_engineering_report",
                      "kind": "contradiction",
                      "detail": {"open_units": open_units, "blockers": blockers}})
    if verdict["status"] == "reported_incomplete" and captured_is_latest \
            and (plan.get("total") or 0) > 0 and open_units == 0 \
            and (counts.get("completed") or 0) > 0:
        codes.append({"code": "completed_units_with_incomplete_engineering_report",
                      "kind": "contradiction",
                      "detail": {"completed": counts.get("completed"),
                                 "outcome": verdict["outcome"],
                                 "remaining_gaps": verdict["remaining_gaps"]}})
    return codes


def summarize(directory, state):
    """The record-only current_summary object, identical on every surface.

    ao_findings already imports ao_progress._clip, so reading these private helpers from a
    sibling module follows the same precedent. ao_room_progress attaches plan_checks and the
    progress-only codes afterwards through attach_plan; ao_room_status and ao_room_open return
    this object as is. Never raises out of a status call: record reads degrade to unavailable.
    """
    requests = state.get("requests")
    requests = requests if isinstance(requests, dict) else {}
    bounds = _Bounds()
    engineering = _engineering_requests(requests)
    latest = max(engineering, key=ao_progress._request_order) if engineering else None
    captured, record = _captured(directory, engineering)
    capture = ao_workflow.engineering_capture(state)
    anchor, _notes, _anchor_truncated = ao_progress._spec_anchor(directory, state)
    stage_request = latest if latest is not None else (
        max((item for item in requests.values() if isinstance(item, dict)),
            key=ao_progress._request_order, default=None))
    stages = ao_progress._stages(directory, state, requests, stage_request, anchor)
    verification = stages["verification"]
    acceptance = stages["acceptance"]
    captured_candidate = (ao_progress._sha256_or_none(captured.get("result_candidate_sha256"))
                          if captured is not None else None)
    # "later" is meaningful only against a baseline: without a captured result there is nothing
    # for the newer request to be later than.
    later = latest if captured is not None and captured is not latest else None
    verification_relation = _relation(verification, verification.get("spec_record_sha256"),
                                      verification.get("spec_sha256"), captured_candidate,
                                      later, state, anchor)
    acceptance_relation = _relation(
        acceptance, ao_progress._acceptance_record_sha256(state, requests),
        acceptance.get("spec_sha256"), captured_candidate, later, state, anchor)
    verdict = _verdict(directory, state, captured, latest, bounds)
    engineering_line = {
        "latest_request": _request_line(latest, bounds) if latest is not None else None,
        "latest_captured": ({"request_id": bounds.ident(captured.get("request_id")),
                             "candidate_sha256": captured_candidate,
                             "report_sha256": ao_progress._sha256_or_none(record.get("report_sha256")),
                             "is_latest_request": captured is latest}
                            if captured is not None else None),
        "later_request": ({"request_id": bounds.ident(latest.get("request_id")),
                           "state": bounds.key(latest.get("state")),
                           "capture_status": bounds.key(capture.get("status"))}
                          if later is not None else None)}
    ladder = {
        "prepared": {"status": bounds.key(state.get("preparation_status") or "none"),
                     "handed_off": bool(state.get("handoff"))},
        "dispatch": _dispatch(latest, bounds),
        "reconciliation": {"status": bounds.key(capture.get("status") or "none"),
                           "request_id": bounds.ident(capture.get("request_id")),
                           "semantic_kind": bounds.key(capture.get("semantic_kind"))},
        "engineering_verdict": verdict,
        "formal_verification": {
            "status": "none" if verification.get("status") in (None, "missing")
            else bounds.key(verification.get("status")),
            "candidate_sha256": verification.get("candidate_sha256"),
            "relation": verification_relation},
        "independent_acceptance": {
            "status": "none" if acceptance.get("status") in (None, "missing")
            else bounds.key(acceptance.get("status")),
            "request_id": acceptance.get("request_id"),
            "candidate_sha256": acceptance.get("candidate_sha256"),
            "relation": acceptance_relation}}
    codes = _record_codes(latest, captured, ladder["formal_verification"]["status"],
                          ladder["independent_acceptance"]["status"], verification_relation,
                          acceptance_relation, verdict, later, bounds)
    return {"version": VERSION, "basis": BASIS, "engineering_line": engineering_line,
            "ladder": ladder,
            "inconsistencies": sorted(codes, key=lambda item: item["code"])[:MAX_INCONSISTENCIES],
            "plan_checks": dict(STATUS_PLAN_CHECKS), "meaning": MEANING,
            "truncated": bounds.truncated}


def attach_plan(summary, view):
    """Attach the progress-only part to a record-only summary in place and return it.

    plan_checks now describes the folded view, and the three codes that read folded task rows
    join the record-only list, re-sorted and re-bounded; every other key is untouched, so the
    record-only part stays byte-identical.
    """
    summary["plan_checks"] = _plan_checks(view)
    summary["inconsistencies"] = sorted(
        summary["inconsistencies"] + _progress_codes(summary, view),
        key=lambda item: item["code"])[:MAX_INCONSISTENCIES]
    return summary
