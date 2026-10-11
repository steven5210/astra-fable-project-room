# Pre-acceptance exact-spec review extension

This is the second lane of the same one-time, once-per-room fourth charter-review
allowance described in [one additional AO charter review](one-charter-review-extension.md).
That guide's receipt mechanics, durability, status reporting and maintenance
operations during an unused grant all apply unchanged; this guide covers only
what differs when the grant is sought **before any independent acceptance**,
against an unaccepted, unverified candidate whose latest completed engineering
turn proposed a scope change rather than a finished implementation.

Both lanes share the same room-wide counter and the same single fourth-review
intent. Granting one lane consumes the room's only additional review exactly as
granting the other would; neither renews, renumbers or coexists with the other
inside one room.

## When this lane applies instead of the original

Use `ao_room_spec_review_extension_audit` / `ao_room_spec_review_extend` (the
original lane) when the room already holds an independent acceptance and the
retained accepted candidate needs one more charter review.

Use this lane instead when:

- the room holds **zero** retained independent acceptances, and
- the latest completed `implementation` or `correction` request for the
  retained agreed handoff reported `outcome: "scope_change"` — Fable discovered,
  while implementing the agreed charter, that the specification itself needs to
  change.

A normal `scope_change` report ordinarily just blocks further implementation
and correction turns until a revised spec is registered and freshly agreed.
This lane lets the user instead authorize **one** additional charter review of
that proposed revision directly, without discarding the three already-consumed
spec-review attempts or requiring a full independent acceptance cycle first.

## Preconditions

- Exactly three retained `spec_review` intents, none of them this room's
  already-exhausted fourth.
- Zero retained acceptances (`state["acceptances"] == []`).
- The retained agreed handoff's spec record matches the last of those three
  reviews; registering the next charter (below) does not disturb that
  retained handoff pointer.
- The named scope-change request is the room's single latest completed
  `implementation`/`correction` request — not an earlier one superseded by a
  later send, and not still pending. Its engineering report (full form or the
  compact `project_room_engineering_v2` contract) resolves to exactly
  `outcome: "scope_change"` with the handoff's exact bound `spec_revision`,
  `spec_sha256` and `baseline_commit`. Malformed delegate attribution on that
  same report is preserved verbatim as a rejected-report diagnostic and still
  supports the proposal; it is never silently repaired or inferred.
- As with the original lane, the operator registers the approved next charter
  with `ao_room_spec_put` first. The registered revision must be exactly one
  greater than the highest previously reviewed revision. Registering clears
  the current checkpoint pointer but changes nothing else; an identical
  re-registration of the same revision stays idempotent.

## Steps

1. Call `ao_room_preacceptance_review_audit` with `room_id`, `spec_revision`,
   `spec_sha256`, `scope_request_id` (the exact scope-change request's own
   `request_id`), `candidate_sha256` (the exact immutable candidate-at-completion
   identity from that same request), `native_session_id`, and the explicit
   absolute `native_owner_database` path. The audit re-derives the scope
   proposal from the room's own retained evidence — the handoff, the named
   request's receipt, completion-candidate and engineering-record (or rejected
   diagnostic) files — rather than trusting the caller's description of it, and
   requires the live worktree to still match the candidate captured at that
   request's completion. It saves a private immutable audit, tagged with this
   lane, and returns `audit_sha256`; `eligible`, `acceptances` (always `0`),
   `candidate_status` (`"unaccepted_unverified"`) and the embedded
   `scope_proposal` evidence summary are included for inspection before
   granting.

   The audit authenticates the scope proposal's identity and outcome, not its
   engineering report's completeness: a completed latest engineering turn with
   a settled `final_available` outcome, matching completion-candidate and a
   strict-JSON report whose `outcome` is `scope_change` with the handoff's
   exact bound `spec_revision`, `spec_sha256` and `baseline_commit`. When that
   report instead fails the normal engineering-report shape checks — an
   incomplete or malformed report, for example — the room's own capture
   diagnostic ("Engineering report is incomplete or refers to another
   spec/handoff") is preserved verbatim as the proposal's
   `rejected_report_diagnostic` evidence, exactly as a malformed delegate
   attribution already is, and still supports the proposal; the audit never
   re-validates, repairs or normalizes that diagnostic. The fourth review
   reviews the registered charter, not the engineer's report, and the ordinary
   correction path already refuses any further correction after a
   `scope_change` outcome, so without this lane an incomplete `scope_change`
   report would otherwise leave the room with no supported continuation.
2. Call `ao_room_preacceptance_review_extend` with `room_id`, that exact
   `audit_sha256`, `authorization`, `diagnosis`, and a durable `request_id`, the
   same way as the original lane's `ao_room_spec_review_extend`. `authorization`
   must quote or accurately preserve the user's actual new answer and the
   proposal it approved. The grant is committed before its state reference the
   same way, with the same pending-receipt reconciliation and tamper-evidence
   guarantees. A grant audited under one lane refuses to renew, renumber or
   commit under the other lane's request; an already-committed grant likewise
   refuses a changed payload or a cross-lane replay.
3. The room's authorized owner sends the ordinary `ao_room_send` with purpose
   `spec_review`. Admission re-validates the scope proposal and the unaccepted
   candidate live, exactly as the original lane re-validates the retained
   accepted candidate; a worktree or evidence change since the grant refuses
   the send with the same live check the audit itself performs. The send
   consumes the room's one fourth-review intent immediately and durably, the
   same as the original lane.

`ao_room_status.spec_review_extension` reports `lane: "pre_acceptance"` and the
pre-acceptance `meaning` text alongside the same `remaining_spec_reviews`,
receipt and consuming-request fields the original lane reports. The grant
result and status report a `lane` field this way for either lane, including
`"accepted_candidate"` for the original lane; only the stored evidence bytes
themselves (the grant receipt file and the state's own `spec_review_extension`
reference) carry no `lane` field under an accepted-candidate grant, for
backward compatibility with historical rooms. A pre-acceptance grant's stored
receipt and reference always carry it.

## After the fourth review

Reaching agreement on the fourth review does not itself finish anything: the
room still needs a genuine implementation turn against the newly agreed
charter (a fresh handoff and a new `implementation`-purpose send under its own
request identity, distinct from the scope-change proposal's own request),
independent review, and `ao_room_accept`, exactly as any other room. Nothing
about this lane shortens or bypasses that remaining lifecycle.

## What stays unchanged

Source-review and independent-acceptance allowances, provider and semantic
holds, the retained native owner check, and every maintenance operation
available during an unused original-lane grant (instruction staging, outcome-
source relocation, executable repair) all apply identically to a pre-acceptance
grant. See [one additional AO charter review](one-charter-review-extension.md)
for their exact behavior; this lane changes only what is audited and admitted,
never how the shared grant is committed, consumed or reported.

A pre-acceptance grant leaves the bound reviewer unused, so recovering that
never-used reviewer through the existing reviewer-recovery lane
(`ao_room_reviewer_recovery_audit`, then `ao_room_reviewer_recover`) remains
supported after the grant; its own audit applies its usual eligibility checks
unchanged, including a passed verification checkpoint. The grant's validation
authenticates exactly that one committed recovery: the record must pass the
recovery lane's own validation, must have recovered the reviewer bound at grant
time and must name the current reviewer binding as its replacement, with the
engineer binding and every other pinned field unchanged. The tolerated
successor must also differ from the original reviewer binding and from the
engineer's own bound native session -- cheap local checks that the recovery
lane's own legitimate-recovery guarantees mean can never refuse a genuine
recovery. Any other reviewer binding change, a modified recovery record or a
recovery of a different reviewer still refuses ("Review-extension room,
native binding, authorization or pinned metadata changed", or the recovery
lane's own error). The
accepted-candidate lane is unchanged: its grant always retains an independent
acceptance, which keeps reviewer recovery ineligible.
