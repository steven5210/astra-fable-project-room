# Efficient review and diagnosed continuation

Fable remains the MAX engineering orchestrator. A request for Fable's personal
review assigns the judgment and final verdict to Fable; it does not implicitly
assign document assembly, evidence indexing, tests or every supporting task to
Fable. Delegate those tasks when the full quality bar is met. Preserve explicit
user restrictions narrowly. Exact-spec review itself remains read-only and
non-delegating; evidence can be prepared beforehand.

## Compact engineering reports

New and retained native engineer sessions receive `efficiency_contract_v1` once.
This is an actual operating/reporting update, not a specification replay. It
retires routine repetition of historical provider attempts, tool counts and
overlapping status, while retaining truthful current routing, findings and gaps.
The operator maintains historical accounting. Long document assembly belongs in
eligible delegates; Fable inspects the evidence needed for its own verdict.

Implementation/correction reports can use:

```json
{
  "report_format": "project_room_engineering_v2",
  "outcome": "changes_required",
  "implementation_complete": false,
  "changes": [],
  "tests_reported": [],
  "review_findings": [],
  "remaining_gaps": [],
  "backlog": [],
  "routing_log": []
}
```

The example describes types, not a verdict. Valid outcomes remain `completed`,
`changes_required`, and `scope_change`. Each routing entry retains its
`delegate_job_ids` list. The controller attaches the immutable handoff's
`spec_revision`, `spec_sha256` and `baseline_commit` as explicitly
controller-authored metadata, recording the raw report and receipt hashes.
Contradictory model-provided identifiers refuse; they are never overwritten.
Missing judgment fields, wrong types and unsupported verdicts still refuse.
Existing full reports retain their original interpretation and identities.

When a document is requested, deliver complete bounded units, placing artifact
content before optional reporting. Preserve all required substantive coverage.
Neither a partial JSON object nor a claim that a document was authored proves
delivery. The parser never reconstructs truncated JSON, invents an artifact or
changes a model verdict. Existing audited extraction of a complete JSON object
from surrounding prose remains separate from this metadata projection.

Use `ao_room_instruction_stage(room_id, request_id, message, authorization)` to
save an actual new operating instruction while a room is paused. Staging makes
no model call and grants no resume permission. The next separately authorized
engineer request carries that instruction once, with immutable delivery
provenance. It cannot revise product scope, change provider settings, renew
review budgets, or settle uncertainty. Normal subsequent messages remain only
`Continue.` or the user's actual new requirement.

## Transport completion is not semantic success

AO's native lifecycle and Project Room's `semantic_status` are separate. Typed
provider failures take precedence over truncation and formatting. Missing or
unstructured output without positive native completion evidence is unknown, not
a quota diagnosis and not a reason to retry automatically. A valid report also
cannot hide an accompanying provider failure.

`ao_room_sync` and dispatch save a content-addressed semantic observation without
rewriting the original receipt or its usage. Errors supplied by AO turn fields,
attributed failure records and typed `provider.failure` system activities are
considered. Historical failures from other turns and settled retry warnings do
not become current failures. A semantic hold blocks new requests and acceptance
even when the saved transport lifecycle says `completed`.

Run `ao_room_outcome_audit(room_id)` to inspect the latest engineer result. For a
reviewer, supply `role: "reviewer"`. These are bounded GETs and local evidence
reads, not model calls or proof of a quota reset.

When the bridge omitted error details, the operator can provide both
`ao_database_path` and `native_transcript_path`. The adapter validates the exact
retained Claude engineer, AO project/conversation/branch and prepared workspace
against read-only native ownership storage. It binds those paths for future
observations of that engineer. It reads the explicit owned transcript only,
never credentials or account settings. Correlation uses the caller's exact
digest, native session and bounded human-message interval; compaction summaries,
tool results, queue metadata and child sessions are not caller messages.
Malformed, changed or ambiguous evidence holds rather than authorizing work.

After diagnosing the failure and obtaining actual authorization to continue,
call `ao_room_outcome_resume` with the failed `request_id`, the fresh audit's
`outcome_sha256`, one unused `resume_request_id`, `diagnosis` and `authorization`.
The operation makes no model call. Only the named successor can pass that hold;
it is still subject to the normal session, model, delegate, scope and review
checks. Send that successor through ordinary `ao_room_send`. Never replay the
old request. A new user instruction to resume can supply authorization; elapsed
time, a presumed reset or the original feature request cannot.

An AO `failed` turn ordinarily remains uncertain. There is one narrow exception:
a matched native API quota error, exact delivery evidence, an idle retained
owner and actual continuation authorization can settle it as `settled_failure`.
The original failed receipt, partial work, native identity and consumed attempts
remain intact. That state is not successful engineering or acceptance. Unknown
delivery, arbitrary crashes, unresolved paid delegates and generic transport
failures cannot use this exception. Review budgets are never renewed here.

## Affected AO bridge workaround

The reviewed AO 0.13.0 bundle contains a Claude ACP result path that checks
`max_tokens` before `is_error`. The explicit `ao_acp_patch.py` operator tool adds
`!message.is_error` to those two truncation conditions, allowing the existing
typed failure handler to run. It accepts only the exact reviewed source SHA-256,
writes a private original-byte backup and immutable patch intent, and refuses an
unknown/new upstream module. It is not run by installation, dispatch or an
updater. No model, effort, output budget or provider is changed.

Verify that the affected runtime is idle before using the tool. Existing native
controllers must reload the patched module through a separately verified
stop/start preserving the same native session; changing disk bytes alone does
not patch an already loaded process. This is a local vendor workaround and
changes bundled app resources. Preserve the original official bundle and its
provenance. Recheck official stable releases and compatibility at the next
update; do not carry this patch blindly to another version or label modified
bytes as a pristine vendor installation.

## Evidence and remaining validation

Offline tests cover quota precedence, stale and ambiguous evidence, exact-once
continuation, restart persistence, immutable failures, schema projection and
one-time instruction delivery. They use fake AO and synthetic native evidence.
Useful live work after an authorized resume must still demonstrate actual
quality, delegation and token savings. Retain MAX and existing delegate budgets.
The 250K native compaction default controls context growth; it does not cap a
single thinking response or restore quota. Do not lower output/thinking limits,
send paid keepalives or repeat complete specifications as an efficiency shortcut.

Recovery authorizations are content-addressed records. If later evidence changes,
a fresh explicit audit and authorization may supersede the unused authorization
for the **same** successor request; previous records remain immutable. A used
successor cannot be renewed. Unknown results require explicit audit with positive
completion evidence before their hold can clear. Sync can record unavailable
history without failing; sending and acceptance still require complete evidence.
Native retry errors followed by an observed successful final are retained as
settled history, not treated as a new quota stop.
