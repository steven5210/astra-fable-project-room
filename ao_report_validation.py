"""Single shared shape schema for a Fable engineering report, plus a read-only draft operation.

ao_workflow.engineering_report (authoritative capture) and the offline ``ao_report_validate`` AO tool
both judge report *shape* through ``shape_diagnostics`` below, so a draft that passes here cannot
later fail capture for a shape reason it was not told about. Capture additionally enforces
authenticated identity equality, delegate attribution, provider delivery and persistence, and the
workflow around capture additionally depends on the candidate's semantic correctness, the pinned
formal gates and Astra's independent acceptance; none of that is performed here. ``UNPERFORMED_CHECKS``
names these deferred authoritative checks verbatim -- always including semantic correctness
assessment, formal candidate verification and independent acceptance, even for a completed/true
draft with no remaining gaps -- so a caller never mistakes a valid draft for an accepted or
capturable one. The tuple is a reminder of named checks, never an exhaustive contract: capture,
readiness and acceptance may perform further checks it does not name.

This module performs no IO, writes nothing, creates no directory, calls no provider or network
endpoint, and never requires a room, provider configuration or authentication. It never echoes the
submitted report's bytes, values or any filesystem path back to the caller.

Argument admission -- whether ``arguments`` is an object carrying exactly one key, ``report_text``,
and whether that ``report_text`` is a nonempty, valid-UTF-8, size- and depth-bounded string -- is this
endpoint's own bounded, non-echoing refusal, raised as ``DraftAdmissionError`` before any shape check
ever runs. ``str(exc)`` is always one of the fixed ``ADMISSION_MESSAGES`` literals, never a supplied
name, value, type name, length, count or path; ``exc.payload`` is the complete, fixed-size refusal
object every public surface (the raised exception's own ``.payload``, the CLI's stderr JSON, and the
MCP ``isError`` result's text and structuredContent) presents, carrying the same ``unperformed_checks``
and ``limits`` a validated draft carries, plus ``"error"``. A refused admission is distinct from a
validated-but-invalid draft: the latter is a normal return with ``syntax_shape_valid`` false and
concrete diagnostics; the former never returns, and its payload's ``diagnostics``/``advisories`` stay
empty because no shape check ever ran.
"""

from ao_quality_review import MAX_DEPTH
import ao_report_contract
from room import RoomError

# Draft-only. Comfortably under project_room_mcp.MAX_LINE (3,000,000 bytes) for ordinary text, but
# this is not a guarantee that an admitted report_text's JSON-escaped bytes always stay under that
# line limit: a pathological admitted text (for example, mostly control or non-ASCII characters that
# each need a \uXXXX escape) can still expand past it once embedded in the MCP request line. That
# case is refused by the MCP transport itself as an oversized request line, failing closed, not by
# this bound.
MAX_TEXT_BYTES = 1_000_000
MAX_DIAGNOSTICS = 64
MAX_MESSAGE_CHARS = 200

# Must equal set(ao_workflow.ENGINEERING_FIELDS); asserted by test_ao_report_validation. Kept as an
# independent literal, never read from ao_workflow, only so this one module-level constant needs no
# import at module-load time -- not a claim that this module never loads ao_workflow at all: when
# validate_draft actually runs, its own lazy `import ao_project_room` already transitively loads
# ao_workflow (ao_project_room imports it at its own module level), well after this constant was
# defined. The literal avoids a module-level dependency for this one tuple; the equality itself is
# asserted by test, not enforced by import.
FIELD_ORDER = ('outcome', 'implementation_complete', 'changes', 'tests_reported', 'review_findings',
               'remaining_gaps', 'backlog', 'routing_log', 'spec_revision', 'spec_sha256', 'baseline_commit')
_IDENTITY_FIELDS = ('spec_revision', 'spec_sha256', 'baseline_commit')
_LIST_FIELDS = ('changes', 'tests_reported', 'review_findings', 'remaining_gaps', 'backlog', 'routing_log')
_OUTCOMES = ('completed', 'changes_required', 'scope_change')

# Named, deferred authoritative checks that this offline, parser-only draft operation never
# performs. Order is fixed and this tuple is always returned in full, whether or not the submitted
# draft is valid -- including a completed/true draft with no remaining gaps -- so a caller never
# infers acceptance from a clean draft result. The list always includes semantic correctness
# assessment, formal candidate verification and independent acceptance. This is a reminder of the
# checks named here, never an exhaustive contract that capture, readiness and acceptance perform
# only these and nothing else.
UNPERFORMED_CHECKS = (
    {"check": "handoff_authentication", "description": "request handoff_sha256 equals the bound handoff"},
    {"check": "spec_identity_equality", "description": "spec_revision/spec_sha256/baseline_commit equal the handoff record"},
    {"check": "compact_contract_delivery", "description": "efficiency_contract_v1 delivered to the native session before a compact report is projected"},
    {"check": "controller_metadata_attachment", "description": "controller-owned identity attached by projection, never by the model"},
    {"check": "delegate_attribution_verification", "description": "routing_log delegate job ids verified against the room's pinned provider ledger"},
    {"check": "engineering_readiness", "description": "completed outcome, implementation_complete, empty remaining_gaps, no finding explicitly tagged scope=required disposition=open, no pending operator work"},
    {"check": "findings_projection", "description": "review_findings lineage/provenance projection"},
    {"check": "capture_receipts", "description": "response-normalization and engineering capture records written only by authoritative capture"},
    {"check": "candidate_binding", "description": "the candidate is bound to immutable completion-candidate evidence at first completed observation"},
    {"check": "native_outcome_evidence", "description": "the request's native/worker outcome evidence is authenticated and checked for a semantic hold"},
    {"check": "correction_chain", "description": "report-only correction evidence is walked, authenticated and checked for cycles"},
    {"check": "issue_dispositions", "description": "malformed operator work in a walked correction chain requires an explicit operator_requests disposition"},
    {"check": "candidate_unchanged", "description": "the candidate is re-snapshotted and compared equal to the bound completion candidate before capture succeeds"},
    {"check": "semantic_correctness_assessment", "description": "whether the reported changes, tests, findings and gaps are true, sufficient and bound to the actual candidate; a parser never judges this"},
    {"check": "formal_candidate_verification", "description": "the pinned formal gates run by the operator on the exact candidate (ao_room_verify), never by this call"},
    {"check": "independent_acceptance", "description": "Astra's independent acceptance of the exact spec, candidate and gate evidence (ao_room_accept), never implied by a valid draft"},
)

# The complete, fixed set of admission-refusal messages this operation can ever raise, in no
# particular priority order. Every one is a literal (the nonempty-text message is built from
# MAX_TEXT_BYTES so it cannot drift from the actual bound); never a supplied name, value, type name,
# length, count or path. str(DraftAdmissionError(...)) is always a member of this tuple.
ADMISSION_MESSAGES = (
    "arguments must be an object",
    "Invalid arguments; ao_report_validate admits exactly one argument, report_text",
    f"report_text must be nonempty text, at most {MAX_TEXT_BYTES} UTF-8 bytes",
    "report_text must be valid UTF-8 text",
    "Draft report exceeds the admitted nesting depth",
)


def refusal_payload(message):
    """The complete, fixed-size refusal object every public surface presents for one admission
    refusal: DraftAdmissionError.payload, the CLI's stderr JSON, and the MCP isError result's text and
    structuredContent. ``message`` must be one of ADMISSION_MESSAGES. Carries the same
    unperformed_checks and limits a validated draft result carries, plus "error"; diagnostics and
    advisories stay empty because no shape check ever ran. Fixed size regardless of the refused
    input: no submitted bytes, lengths or counts are embedded anywhere in this object."""
    return {
        "error": message,
        "operation": "ao_report_validate",
        "validity_scope": "syntax_and_shape_only",
        "syntax_shape_valid": False,
        "report_form": None,
        "diagnostics": [],
        "advisories": [],
        "unperformed_checks": [dict(item) for item in UNPERFORMED_CHECKS],
        "limits": {"max_text_bytes": MAX_TEXT_BYTES, "max_depth": MAX_DEPTH, "max_diagnostics": MAX_DIAGNOSTICS,
                   "truncated": False},
    }


class DraftAdmissionError(RoomError):
    """Fixed, bounded, non-echoing refusal of one ao_report_validate call at admission, before any
    shape check. str(exc) is one of the fixed admission messages; ``payload`` is the complete refusal
    object every public surface presents."""

    def __init__(self, message):
        super().__init__(message)
        self.payload = refusal_payload(message)


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


def admit_arguments(arguments):
    """Admit exactly one ao_report_validate argument, report_text, before any shape check ever runs.

    This is the endpoint's own bounded, non-echoing argument admission, used in place of the shared
    tool_arguments check for this one tool. Touches only the submitted keys, never a submitted value:
    a nonobject ``arguments``, or one that does not carry exactly the single key "report_text", is
    refused by one of the two fixed ADMISSION_MESSAGES literals, never by interpolating what was
    actually submitted.
    """
    if not isinstance(arguments, dict):
        raise DraftAdmissionError("arguments must be an object")
    if set(arguments) != {"report_text"}:
        raise DraftAdmissionError("Invalid arguments; ao_report_validate admits exactly one argument, report_text")
    return arguments["report_text"]


def validate_call(arguments):
    """Public entry point shared by Service.call, the CLI preflight and MCP tools/call: admit the
    raw ``arguments`` object, then validate the admitted report_text. Raises DraftAdmissionError for
    any refused admission (argument shape or report_text shape/size/encoding/depth); never for a
    malformed *report*, which always comes back as a normal result with diagnostics."""
    return validate_draft(admit_arguments(arguments))


def validate_draft(report_text):
    """Read-only, offline syntax/shape validation of one draft engineering report text.

    Never raises for a malformed *report*: malformed JSON, an unsupported report_format, model-written
    controller_metadata, malformed operator_requests/enhancement_proposals and pure shape defects all
    become diagnostics/advisories in the returned object. Only a malformed *admission* (wrong type,
    empty, not UTF-8, oversized, or nested deeper than MAX_DEPTH) raises DraftAdmissionError, a
    RoomError subclass, at admission, before any shape check. str(exc) is always one of the fixed
    ADMISSION_MESSAGES literals -- never a supplied value, type name, length or path -- and
    exc.payload is the complete, fixed-size refusal object every public surface presents, carrying the
    same unperformed_checks and limits a validated draft carries, plus "error". Nesting deeper than
    MAX_DEPTH is refused as the same admission failure whether it is detected by the post-parse depth
    walk or, for a document nested past the interpreter's own recursion limit, by the parser's
    RecursionError.
    """
    import ao_project_room
    import ao_response_normalization
    import ao_review_followups

    try:
        ao_project_room.nonempty(report_text, "report_text", MAX_TEXT_BYTES)
    except RoomError as exc:
        raise DraftAdmissionError(str(exc)) from exc
    try:
        report = ao_response_normalization.strict_object(report_text)
        parsed = True
    except ao_response_normalization.ResponseFormatError as exc:
        # A document nested deeper than the interpreter recursion limit (about 995 levels on
        # Python 3.11) makes strict_object's decoder raise RecursionError before it can return a
        # value; strict_object wraps that as this same ResponseFormatError, so it must be told
        # apart from an ordinary malformed-JSON ResponseFormatError and refused as the depth
        # admission failure below, exactly like the post-parse MAX_DEPTH check, never surfaced as
        # a "$" diagnostic.
        if isinstance(exc.__cause__, RecursionError):
            raise DraftAdmissionError("Draft report exceeds the admitted nesting depth") from exc
        report = None
        parsed = False

    if parsed and _exceeds_max_depth(report):
        raise DraftAdmissionError("Draft report exceeds the admitted nesting depth")

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
