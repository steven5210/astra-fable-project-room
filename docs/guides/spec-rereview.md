# One authorized exact-spec re-review

This guide describes the room's one-ever explicitly authorized exact-spec
re-review, implemented by `ao_spec_rereview.py`. It is a separate, once-per-room
continuation — never an extension of the ordinary review limit and never a
refund of a consumed charter review. One grant exists per room, ever: there is
no banking, no chaining and no counter; any further substantive review needs a
new product decision.

## When it applies

All of these must already be true:

- The room's one charter-review extension was granted **and consumed** by a
  fourth `spec_review` intent (either lane of the extension; see
  [pre-acceptance exact-spec review extension](pre-acceptance-review-extension.md)).
- That fourth review **completed** and its retained final result is a negative
  or noncanonical-negative verdict: a canonical `changes_required` decision, a
  canonical `accept` carrying a `BLOCKER:` finding, or exactly one JSON object
  in a noncanonical schema that cannot be read as the required verdict (for
  example a `project_room_spec_verdict` object with a `verdict` field and
  object-typed findings instead of `decision`). The room's ordinary agreement
  still refuses on it; the re-review lane never reinterprets, repairs or
  re-reads that original verdict.
- The operator registered the exact corrected next charter revision with
  `ao_room_spec_put` — exactly one revision above the reviewed one.
- The actual human answer explicitly authorized one more exact-spec review.

A completed fourth review whose verdict is a clean canonical `accept` makes
the lane inapplicable (the room would already agree on registering the same
spec). A prose-only or multi-object fourth result belongs to the format lanes
instead — see
[one-use spec-verdict format recovery](spec-verdict-format-recovery.md) — and
is refused here.

## Steps

1. Register the corrected charter with `ao_room_spec_put` (the next revision,
   immutable). If the correction changed a gate script, declare each artifact
   the re-review must pin in `gate_artifacts`.
2. Call `ao_room_spec_rereview_audit` with `room_id`, `spec_revision`,
   `spec_sha256`, `source_request_id` (the consumed fourth review's own
   `request_id`), `source_receipt_sha256`, `source_final_text_sha256` (the
   SHA256 of its retained raw final text), `source_classification`,
   `request_id` (the one named intent), `message_sha256` (the SHA256 of the
   exact caller message that will be sent), `gate_artifacts` (a list of
   absolute paths; may be empty), `native_session_id` and the explicit absolute
   `native_owner_database` path.

   `source_classification` is the operator's declared reading of the retained
   result and must equal the audit's mechanical classification exactly:
   `changes_required` for a canonical changes-required verdict,
   `accept_with_blockers` for a canonical accept carrying a `BLOCKER:`
   finding, or `noncanonical_negative` for the noncanonical single object.
   The audit computes the mechanical kind (`changes_required`,
   `accept_with_blockers`, `noncanonical_object`) from the stored bytes and
   refuses a mismatch; it saves a private immutable audit and returns
   `audit_sha256`, `source_result_kind`, the bound `gate_artifacts` digests
   and `review_contract_sha256`. An audit alone is never an allowance.
3. Call `ao_room_spec_rereview_grant` with `room_id`, the exact
   `audit_sha256`, `authorization` (the user's actual answer and its
   context), `authorization_reference` (the provenance of that decision) and
   `diagnosis` (the concrete bounded correction being answered). The grant
   receipt is stored durably before its state reference, exactly like the
   extension's; an identical replay reconciles a pending receipt, and a
   different payload refuses.
4. Send the named intent once: `ao_room_send` role `engineer`, purpose
   `spec_review`, the audited `request_id` and the exact audited caller
   message bytes. Admission re-verifies the corrected spec, the message
   digest, gate artifacts, candidate, owner and native history, then consumes
   the grant durably before the state projection and before any POST. The
   request carries `review_contract` again even though an earlier review
   already delivered it; every other already-delivered part stays omitted.

Example (synthetic; ids and hashes are placeholders):

```sh
project-room call ao_room_spec_rereview_audit --args '{
  "room_id": "ao-room", "spec_revision": 3,
  "spec_sha256": "<sha of the registered revision-3 spec>",
  "source_request_id": "fourth", "source_receipt_sha256": "<sha>",
  "source_final_text_sha256": "<sha of the retained raw final text>",
  "source_classification": "noncanonical_negative",
  "request_id": "spec-rereview",
  "message_sha256": "<sha of the exact caller message>",
  "gate_artifacts": ["/abs/path/to/scope_gate.py"],
  "native_session_id": "<native session id>",
  "native_owner_database": "/abs/path/to/ao.db"}'
project-room call ao_room_spec_rereview_grant --args '{
  "room_id": "ao-room", "audit_sha256": "<from the audit>",
  "authorization": "User answered yes to one re-review of the corrected charter.",
  "authorization_reference": "The retained operator answer and where it was recorded.",
  "diagnosis": "The consumed fourth review returned a retained negative verdict on the prior charter."}'
project-room call ao_room_send --args '{
  "room_id": "ao-room", "role": "engineer", "purpose": "spec_review",
  "request_id": "spec-rereview",
  "message": "Review the exact corrected specification revision 3."}'
```

## Semantics and boundaries

- One named intent only. Failed or uncertain delivery exhausts the grant;
  there is no refund, no retry intent and no second grant. An interrupted
  grant commit leaves a durable pending receipt that only the identical grant
  request can reconcile. A POST failure after consumption keeps the request
  uncertain with `delivery_error`: sync it without resending.
- A provider-limit failure of the re-review turn settles through the ordinary
  outcome audit and resume; the grant stays consumed and every further
  `spec_review` send refuses. A semantic hold on the completed re-review
  blocks agreement and handoff until independently resolved; the grant never
  clears it.
- The lane never renews, renumbers, resets counters, releases a provider or
  semantic hold, replaces a model, widens the ordinary three-attempt limit or
  adds a second extension. It does not change what the original verdict
  meant; it only admits one new exact-spec review of the corrected charter.
- A different `ao_room_spec_put` while the grant is unused refuses; the
  identical registration stays idempotent. `ao_room_status.spec_rereview`
  reports `none` (no field), `pending_uncommitted`, `granted`, `consumed` or
  `damaged`, with the named request id, remaining count and receipt fields.
- Supported room operations stay valid through the grant. An explicit
  `ao_room_outcome_audit` with native source paths may replace
  `native_outcome_source` and `native_outcome_source_evidence`; the saved
  verified source audit must prove the same retained native owner. A
  stopped-controller executable repair may append a verified descendant to
  `executable_binding`; every journal entry must prove the same retained
  owner and the chain must reach the audited pointer. Neither change makes
  the audited evidence stale, before the intent is consumed or after it is
  agreed.
- A routing refresh is not supported in this lane. The existing
  fourth-review refresh guard requires the room agreement to be the consumed
  fourth review's own; after a re-review grant there is no agreement, and
  after an accepted re-review the agreement belongs to that fifth review, so
  the guard refuses the refresh before any `routing-refresh/` journal write
  and the room is never stranded. Validation also pins the audited
  `routing_refresh` pointer itself, so a tampered pointer refuses.
