"""Single shared shape schema for a Fable engineering report, plus a read-only draft operation.

ao_workflow.engineering_report (authoritative capture) and the offline ``ao_report_validate`` AO tool
both judge report *shape* through ``shape_diagnostics`` below, so a draft that passes here cannot
later fail capture for a shape reason it was not told about. Capture additionally enforces
authenticated identity equality, delegate attribution, provider delivery and persistence; those are
never performed here and are named verbatim in ``UNPERFORMED_CHECKS`` so a caller never mistakes a
valid draft for an accepted or capturable one.

This module performs no IO, writes nothing, creates no directory, calls no provider or network
endpoint, and never requires a room, provider configuration or authentication. It never echoes the
submitted report's bytes, values or any filesystem path back to the caller.
"""

from ao_quality_review import MAX_DEPTH
import ao_report_contract

MAX_TEXT_BYTES = 1_000_000  # Draft-only; below project_room_mcp.MAX_LINE (3,000,000 bytes) with JSON-escaping headroom.
MAX_DIAGNOSTICS = 64
MAX_MESSAGE_CHARS = 200

# Must equal set(ao_workflow.ENGINEERING_FIELDS); asserted by test_ao_report_validation. Kept as an
# independent literal (not imported) so this module never has to import ao_workflow.
FIELD_ORDER = ('outcome', 'implementation_complete', 'changes', 'tests_reported', 'review_findings',
               'remaining_gaps', 'backlog', 'routing_log', 'spec_revision', 'spec_sha256', 'baseline_commit')
_IDENTITY_FIELDS = ('spec_revision', 'spec_sha256', 'baseline_commit')
_LIST_FIELDS = ('changes', 'tests_reported', 'review_findings', 'remaining_gaps', 'backlog', 'routing_log')
_OUTCOMES = ('completed', 'changes_required', 'scope_change')

# Checks authoritative capture/readiness/acceptance perform that this offline, parser-only draft
# operation never performs. Order is fixed and this tuple is always returned in full, whether or not
# the submitted draft is valid, so a caller never infers acceptance from a clean draft result.
UNPERFORMED_CHECKS = (
    {"check": "handoff_authentication", "description": "request handoff_sha256 equals the bound handoff"},
    {"check": "spec_identity_equality", "description": "spec_revision/spec_sha256/baseline_commit equal the handoff record"},
    {"check": "compact_contract_delivery", "description": "efficiency_contract_v1 delivered to the native session before a compact report is projected"},
    {"check": "controller_metadata_attachment", "description": "controller-owned identity attached by projection, never by the model"},
    {"check": "delegate_attribution_verification", "description": "routing_log delegate job ids verified against the room's pinned provider ledger"},
    {"check": "engineering_readiness", "description": "completed outcome, implementation_complete, empty remaining_gaps, no pending operator work"},
    {"check": "findings_projection", "description": "review_findings lineage/provenance projection"},
    {"check": "capture_receipts", "description": "response-normalization and engineering capture records written only by authoritative capture"},
)


def shape_diagnostics(report, identity_required):
    """The pure-shape half of the historical engineering_report predicate; no IO, never raises.

    ``identity_required`` controls only whether a *missing* identity-trio field is reported: the
    compact contract lets the controller project identity after capture, so a compact draft need not
    carry it yet. A *present* identity field is always type-checked, in every form. Returns a list of
    {"field", "message"} objects; messages never include submitted values. Field paths use only the
    literal field name, "." and integer indices (never a value) so a path can be safely shown back.
    """
    diagnostics = []
    for field in FIELD_ORDER:
        if field not in report:
            if field not in _IDENTITY_FIELDS or identity_required:
                diagnostics.append({"field": field, "message": "missing required field"})
            continue
        value = report[field]
        if field == "outcome":
            if value not in _OUTCOMES:
                diagnostics.append({"field": field, "message": "must be completed, changes_required or scope_change"})
        elif field == "implementation_complete":
            if type(value) is not bool:
                diagnostics.append({"field": field, "message": "must be a boolean"})
        elif field in _LIST_FIELDS:
            if not isinstance(value, list):
                diagnostics.append({"field": field, "message": "must be a list"})
            elif field == "routing_log":
                for index, entry in enumerate(value):
                    path = f"routing_log[{index}]"
                    if not isinstance(entry, dict):
                        diagnostics.append({"field": path, "message": "must be an object"})
                        continue
                    jobs = entry.get("delegate_job_ids")
                    jobs_path = path + ".delegate_job_ids"
                    if not isinstance(jobs, list):
                        diagnostics.append({"field": jobs_path, "message": "must be a list of strings"})
                    else:
                        for job_index, job_id in enumerate(jobs):
                            if not isinstance(job_id, str):
                                diagnostics.append({"field": f"{jobs_path}[{job_index}]", "message": "must be a string"})
        elif field == "spec_revision":
            if type(value) is not int:
                diagnostics.append({"field": field, "message": "must be an integer"})
        elif field in ("spec_sha256", "baseline_commit"):
            if not isinstance(value, str):
                diagnostics.append({"field": field, "message": "must be a string"})
    return diagnostics


def _exceeds_max_depth(value):
    """Same container-depth convention as ao_quality_review._json: the root container is depth 1."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            depth += 1
            if depth > MAX_DEPTH:
                return True
            stack.extend((v, depth) for v in item.values())
        elif isinstance(item, list):
            depth += 1
            if depth > MAX_DEPTH:
                return True
            stack.extend((v, depth) for v in item)
    return False


def _bounded(entries):
    """Truncate a diagnostics/advisories list to MAX_DIAGNOSTICS and each message to MAX_MESSAGE_CHARS."""
    truncated = len(entries) > MAX_DIAGNOSTICS
    bounded = [{"field": entry["field"], "message": entry["message"][:MAX_MESSAGE_CHARS]} for entry in entries[:MAX_DIAGNOSTICS]]
    return bounded, truncated


def validate_draft(report_text):
    """Read-only, offline syntax/shape validation of one draft engineering report text.

    Never raises for a malformed *report*: malformed JSON, an unsupported report_format, model-written
    controller_metadata, malformed operator_requests/enhancement_proposals and pure shape defects all
    become diagnostics/advisories in the returned object. Only a malformed *admission* (wrong type,
    empty, not UTF-8, oversized, or nested deeper than MAX_DEPTH) raises RoomError, before any parsing.
    """
    import ao_project_room
    import ao_response_normalization
    import ao_review_followups
    from room import RoomError

    ao_project_room.nonempty(report_text, "report_text", MAX_TEXT_BYTES)
    try:
        report = ao_response_normalization.strict_object(report_text)
        parsed = True
    except ao_response_normalization.ResponseFormatError:
        report = None
        parsed = False

    if parsed and _exceeds_max_depth(report):
        raise RoomError("Draft report exceeds the admitted nesting depth")

    diagnostics = []
    advisories = []
    report_form = None
    if not parsed:
        diagnostics.append({"field": "$", "message": "must be one unambiguous JSON object, optionally inside a "
                                                       "```json fence; prose is never extracted"})
    else:
        if "report_format" not in report:
            report_form = "full"
        elif report["report_format"] == ao_report_contract.FORMAT:
            report_form = "project_room_engineering_v2"
        else:
            report_form = "unsupported"

        try:
            ao_report_contract.supported_format(report)
        except RoomError as exc:
            diagnostics.append({"field": "report_format", "message": str(exc)})

        if report_form == "project_room_engineering_v2":
            try:
                ao_report_contract.refuse_model_metadata(report)
            except RoomError as exc:
                diagnostics.append({"field": "controller_metadata", "message": str(exc)})

        try:
            ao_review_followups.validate(report, fields=("operator_requests",))
        except RoomError as exc:
            diagnostics.append({"field": "operator_requests", "message": str(exc)})

        diagnostics.extend(shape_diagnostics(report, identity_required=(report_form == "full")))

        try:
            ao_review_followups.validate(report, fields=("enhancement_proposals",))
        except RoomError as exc:
            advisories.append({"field": "enhancement_proposals", "message": str(exc)})

    syntax_shape_valid = parsed and not diagnostics
    diagnostics, diagnostics_truncated = _bounded(diagnostics)
    advisories, advisories_truncated = _bounded(advisories)
    return {
        "operation": "ao_report_validate",
        "validity_scope": "syntax_and_shape_only",
        "syntax_shape_valid": syntax_shape_valid,
        "report_form": report_form,
        "diagnostics": diagnostics,
        "advisories": advisories,
        "unperformed_checks": [dict(item) for item in UNPERFORMED_CHECKS],
        "limits": {"max_text_bytes": MAX_TEXT_BYTES, "max_depth": MAX_DEPTH, "max_diagnostics": MAX_DIAGNOSTICS,
                   "truncated": diagnostics_truncated or advisories_truncated},
    }
