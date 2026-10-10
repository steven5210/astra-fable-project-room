# Efficient review and diagnosed continuation

Fable remains the default MAX engineering orchestrator; a room that explicitly adopted another qualified engineering model is owned by that model for subsequent engineering work, while every judgment keeps the attribution recorded with it. A request for Fable's personal
review assigns the judgment and final verdict to Fable; it does not implicitly
assign document assembly, evidence indexing, tests or every supporting task to
Fable. Delegate those tasks when the full quality bar is met. Preserve explicit
user restrictions narrowly. Exact-spec review itself remains read-only and
non-delegating; evidence can be prepared beforehand.

## Shared quality-first review instruction

Normal AO engineer sessions receive `quality_first_review_v1` once on a separately
authorized send. Delegates prepare source-grounded facts, proposed changes and
supporting evidence; the assigned operator verifies mechanical anchors, hashes,
schemas and test receipts. Fable reviews the actual changes and original evidence
needed for meaning, correctness, conflicts and omissions. Fable retains MAX,
personal specification review, pushback, enhancement judgment, routing discretion
and the final engineering verdict. Passing tests or summaries alone never approve
work, and necessary deeper review takes priority over savings.

The instruction has immutable versioned bytes. A verified earlier operating
amendment with exactly the same bytes satisfies the new part. When identical
amendments are pending with an undelivered part, one copy is sent and each actual
delivery is recorded. When the part is already held, an identical later amendment
is satisfied through explicit provenance naming the original verified receipt.
A one-byte change is a different instruction; caller text and a matching hash by
themselves do not establish delivery. Earlier requests and receipts are not
rewritten, and derived equivalence never becomes a new independent delivery root.

Saved status adds `engineer_context.quality_first_review`, with the frozen part
identity, verified delivery source, equivalent amendment digests and closed refusal
reasons. Delivery is `verified_delivered`, `verified_equivalent`, `undelivered` or
`unavailable_integrity`. Historical `undelivered_parts` reports carried-part
metadata only: an earlier equivalent amendment may leave this list unchanged.
Use `quality_first_review.delivery` and its verified source to determine whether
this instruction was delivered. The other historical context fields also retain
their existing meaning. Missing, contradictory or changed proof prevents a new
send before it records intent; status does not repair the evidence or release an
existing hold.
Failed mutating operations still invalidate an existing history-reconciliation
proof under the established recovery checks. Status projection remains read-only.

Proof reads are local, owned and no-follow: 8 MiB per binding JSON file, 64 MiB of
actual reads per proof, 4,096 requests, 256 amendment pointers and container depth
64. Existing admission and semantic-hold checks remain required before dispatch.
Invalid saved delivery evidence may cause the new proof to refuse earlier. This
proof does not turn their older read paths into the new bounded interface. No
model, network, transcript scan or new subprocess is needed by the proof.

After this information and the other required parts are delivered, an ordinary
continuation remains exactly `Continue.` or the caller's actual new instruction.
The part changes neither Astra-led exception packets nor acceptance-review
wrappers. It grants no installation, continuation, recovery or review allowance.
Validate the allocation at a bounded useful milestone with the same correctness
and acceptance requirements; smaller packets alone do not establish subscription
savings or restored quota.

## Personal review, operator handoffs and enhancements

The `review_first_routing_v1` amendment reaches new and retained engineer sessions
once, on their next separately authorized request. Fable keeps personal spec
review, pushback, useful enhancement suggestions and final engineering judgment
at MAX. A large review is not evidence of waste by itself. Qualified delegates
author substantive supporting work from requirements. Fully specified copying,
byte application, inventories, hashing and exact gate execution belong with the
assigned operator when no further model judgment is needed. Record a concrete
quality or capability reason for choosing a model instead; preserve all quality
checks, provider budgets and data restrictions.

The one-time `residual_escalation_v1` amendment refines routing within the
pinned worker ladder only: concurrency-, financial- and security-sensitive
units route to pr-opus from the first round, and an area escalates one tier
after two same-tier rounds with correctness defects (pr-sonnet to pr-opus,
pr-opus to Fable with the recorded capability reason). It changes no model,
budget, guard or delegate setting.

Engineering reports support `operator_requests`, a list of pending handoffs:

```json
{
  "operator_requests": [{
    "task": "Run the agreed gate against the supplied candidate.",
    "inputs": ["The candidate and the agreed gate argv"],
    "verification": ["Save the complete output and exit status against the candidate digest."]
  }],
  "enhancement_proposals": [{
    "title": "Export comparison results",
    "benefit": "Reviewers can compare runs.",
    "tradeoff": "An additional format to maintain.",
    "basis": "Repeated manual comparisons during the review."
  }]
}
```

These are optional additions to the existing report, not a complete report or
executable instructions. All object fields shown are required when an entry is
present; strings must be nonempty, inputs is a list and verification is a nonempty
list. A nonempty `operator_requests` list blocks verification and acceptance even
if the report mistakenly claims `completed/true`. The operator reads the complete
request, checks existing authority, performs authorized work and provides new
evidence. A subsequent truthful Fable report can clear the pending list. Omitting
the field does not clear previously reported pending work. Prior reports and
receipts remain unchanged; a handoff never grants new permission.
The controller also retains explicit pending requests from a parseable report
that failed some other engineering validation. Intact non-JSON replies never
clear an earlier structured statement; missing or corrupt evidence is unknown
and blocks an implicit clearance. An explicit new list requires Fable's truthful
disposition and the ordinary candidate and acceptance checks.

Both spec-review and engineering reports support `enhancement_proposals`. Suggest
worthwhile improvements, without manufacturing a required number. Return `[]`
when there are no new proposals. Astra presents grounded proposals with benefits,
tradeoffs and its recommendation, and files or links each worthwhile proposal under
existing filing authority. Use the repository's enhancement, relevant area and accurate
status labels; verify and record the issue URL and labels with the user's decision
as specified in the [enhancement issue handoff](workflow.md#enhancement-issue-handoff).
Filing and implementation
retain their separate authorization requirements. Optional proposals do not block
acceptance or amend the specification; unmet agreed requirements remain findings
or gaps. Legacy reports may omit either field, which means **not reported**, never
"no findings". Older proposals can still be present in backlog or findings.
Malformed optional enhancement data is exposed as `assessment: invalid`; it does
not invalidate the core verdict or consume another spec review just to repair
advisory formatting. Malformed engineering `operator_requests` refuse the report
because pending execution affects readiness. Operator requests in a spec review
are exposed as unexpected data with no execution authority; that phase remains
read-only and its core interpretation, findings and decision govern agreement.

Saved `ao_room_status.review_followups` exposes the latest spec-review and engineering
reports without AO/model calls or writes. Counts and bounded previews point to each
complete receipt and digest. A pending, stale or unusable latest result is explicit;
status never falls back to an older all-clear. Previews are not complete task inputs,
scope decisions or live candidate verification.

`delegate.routing.worker_recovery` distinguishes verified current guard capability
from unverified, historical or unconfigured routing. Only configured or verified routing with
preparation routing version 2 or 3 and an effective guard digest matching the
current guard bytes establishes the reported protection for the root engineer
managing its children; that is static guard capability, not a live recovery
attestation or dispatch authorization. Null capability values mean unknown, never permission. Unconfigured
or unverified routing offers the operator path, not a native dispatch. Under the
bounded contract, child context resume, `SendMessage` and root `ListAgents` remain
unsupported; this does not claim every child tool is denied by historical guards.
Continuing the retained Fable parent session is a separate audited operation and
does not resume a child's context. After an authorized continuation, inspect and
verify preserved artifacts, then use the operator for settled execution or a fresh
pinned worker for substantive remaining work when routing and normal dispatch gates
permit it. Supply the new worker's complete necessary context and verify artifact
access; a path alone is insufficient. Do not retry a denied resume through another
tool or weaken the guard. This amendment does not wake work, clear quota/uncertainty
holds, renew reviews, modify native configuration or prove live usage savings.

## Lifecycle-closure delivery (AO rooms)

The one-time `lifecycle_closure_v1` amendment is carried through `ao_workflow.PARTS`
exactly like the other one-time parts above, and only in AO rooms: a new AO engineer
session receives it in its first packet, and a retained AO engineer session receives
it once, carried on its next engineer send (`spec_review`, `implementation`, or
`correction`). Saved `context_summary`/status lists it under `undelivered_parts`
until a `completed` or `settled_failure` engineer turn's verified immutable receipt
shows it was actually carried; an `uncertain`, `failed`, `interrupted` or otherwise
unsettled turn never counts as delivery, so the part is carried again on the next
send rather than assumed sent (fail closed, like the rest of `delivered()`). This
delivery mechanism adds no repeated consent step: an ordinary continuation remains
exactly `Continue.` or the caller's actual new instruction, and the controller —
not the human turn — appends the undelivered part. The instruction itself is text
only: it grants no scope, execution permission, recovery or review allowance and
changes no model, tier policy, guard, review budget or authority. See
[lifecycle execution and closure](workflow.md#lifecycle-execution-and-closure-ao-rooms)
for the default it states, and the [Fable policy reference's matching
section](../../skills/project-room/references/fable-policy.md#lifecycle-execution-and-closure-ao-rooms)
for the exact delivered text and the native task-lineage grammar. Like the other
reusable instructions in this document, it is a global default that takes effect
in a room only after the updated plugin has been installed or resynced (see
[Install and authenticate](../../README.md#install-and-authenticate)); existing
room snapshots and already-delivered workflow-part records are not rewritten.

## Progress-plan successor delivery (AO rooms)

The one-time `progress_plan_v3` part is carried through `ao_workflow.PARTS`
exactly like the other one-time parts above: a new AO engineer session receives
it in its first packet, and a retained AO engineer session receives it once,
carried on its next engineer send (`spec_review`, `implementation`, or
`correction`). It consolidates and supersedes the earlier `progress_plan_v1` and
`progress_plan_v2` parts — their frozen text, pinned digests and historical
carried records stay valid forever, but `ao_workflow.PARTS`'s delivery loop
skips both names unconditionally, so no future packet ever sends either again,
to any session, retained or new, regardless of what that exact session actually
received before.

Saved `context_summary`/status adds a bounded `progress_plan` object: `successor`
names the current part (`progress_plan_v3`); `successor_delivery` is one of four
values. `verified_delivered`: a `completed` or `settled_failure` engineer turn's
verified immutable receipt shows it was actually carried. `undelivered`: a given
directory's receipt-authenticated chain is intact but that is not yet true
(including an `uncertain`, `failed`, `interrupted` or otherwise unsettled turn —
fail closed, like the rest of `delivered()`). `unavailable_integrity`: a
directory is given but that receipt chain itself cannot be authenticated — a
missing or corrupt receipt on any of the session's completed engineer turns, or
a tampered carried record, for example — so a broken chain is reported through
its own status and never misread as a verified absence ("undelivered") or
trusted into a successor claim. It is also reported, regardless of whether a
directory is given, when the session's own saved carried-part history cannot
be read at all; see the `unavailable` value of `delivered_history` below for
that case. `unverified`: status is read without this
room's own directory to verify receipts against (metadata-only; never treated
as satisfied). Beside these four sits `delivered_history`, one of three values:
`receipt_verified` when a directory was given and its own receipt chain
authenticated cleanly (exactly the `verified_delivered`/`undelivered` cases
above), `metadata_only` when the carried-part history itself was readable but no
directory was given at all or that chain's own authentication failed
(`unverified`, or `unavailable_integrity` with readable history), or
`unavailable` when the session's own saved carried-part history cannot be
read at all (malformed saved metadata, for example a duplicated carried part
name). In the `unavailable` case, status keeps its existing `error` field, reports
no `parts`, `undelivered_parts`, `spec_record_sha256` or `spec_current` (as
before), `successor_delivery` is `unavailable_integrity`, each superseded part's
`delivered` is null rather than a true or false claim, `satisfied` is `false`, and
nothing is inferred or repaired. And `superseded`
lists `progress_plan_v1` and `progress_plan_v2`, each with `delivered` (that
session's own literal carried-part history for the older part: `true` for a
retained session whose recorded carried parts include it, `false` for a session
that never received it, which includes every new session, or null when
`delivered_history` is `unavailable`; receipt-verified only when
`delivered_history` is `receipt_verified`, metadata-only when it is
`metadata_only`, and null with no claim when it is `unavailable`) and `satisfied`
(`true` only when `successor_delivery` is `verified_delivered`; always `false`
without a directory or when `delivered_history` is `unavailable`).
Neither field ever causes a superseded part to be
re-sent. The existing `undelivered_parts` list also excludes both superseded
names, so it never reports a gap that no future packet will ever fill. This
delivery mechanism adds no repeated consent step: an ordinary continuation remains
exactly `Continue.` or the caller's actual new instruction, and the controller —
not the human turn — appends the undelivered part.
The instruction itself is text
only: it grants no scope, execution permission, recovery or review allowance
and changes no model, guard, budget or authority. See [the one-time plan
instruction](../reference/progress.md#the-one-time-plan-instruction) for the
exact delivered text and list-keeping rules it states. Like the other reusable
instructions in this document, it is a global default that takes effect in a
room only after the updated plugin has been installed or resynced (see
[Install and authenticate](../../README.md#install-and-authenticate)); existing
room snapshots and already-delivered workflow-part records are not rewritten.

## Compact engineering reports

The additional `delegation_efficiency_v2` amendment is delivered once to new or
retained native engineer sessions on their next separately authorized request.
It permits qualified supporting workers to author code from complete requirements,
removes arbitrary source-line and Write-call limits, keeps routine inventories with
the operator, and requests concise summaries backed by complete authorized artifacts.
It also forbids model escalation merely to evade an explicit account/session quota.
Fable keeps MAX, necessary engineering judgment and final review. Pinned provider
budgets, exact-spec review restrictions, data restrictions and semantic holds stay
in force. Installing the update does not wake a paused session or grant a retry.
Historical request bytes and earlier delivered contracts remain unchanged.
Pinned historical policy files also remain unchanged; the new amendment is the
delivery mechanism for this update in retained sessions. Store operational logs
and inventories outside the candidate worktree or in an already ignored path.
Do not change ignore rules or add candidate files merely to store operational
evidence. Explicitly requested deliverables retain their authorized product paths.

New preparations include the updated Sonnet definition and quota-aware routing
guard. Existing preparations retain their pinned files; an instruction amendment
alone is not evidence that an already loaded native guard has changed. Verify an
audited idle configuration transition before claiming native enforcement there.

The quota guard checks only the exact current native human turn. A typed
account/session quota error blocks further native-worker and DeepSeek submissions
from that turn; result/status inspection remains available. It does not infer
account exhaustion from a generic or model-specific 429, and it cannot undo work
already launched. Missing or unsafe supplied transcript evidence blocks new
submissions. Older hook calls with no transcript metadata retain the historical
guard with an explicit diagnostic; native quota enforcement is unverified there.
Project Room's semantic hold separately controls any authorized continuation. The native scan is bounded to 8 MiB and 20,000 records.
If that window cannot establish the current human-turn boundary, new submissions
are denied with an evidence-incomplete diagnosis. This is not a quota assertion;
the operator should inspect the evidence instead of retrying through another tier.

For a retained v2 engineer, the CLI-only `ao_routing_refresh.py` operation installs
the reviewed definitions and guard through an immutable routing journal. Supply
`--home`, `--room-id`, an unused `--request-id`, `--database-path`,
`--native-session-id`, `--authorization` and `--diagnosis`. It requires the bound
engineering orchestrator at its current effective model and MAX, stopped, with
settled requests and delegates, complete native history, unchanged candidate and
provider attachment, and the existing ignored runtime paths. The intent binds
that observed session identity without embedding activity payloads: new intents
store the bounded activity identity projection together with the count and
digest of the complete activity rows. Stop only after independently checking
native quiescence; an AO idle label
alone is insufficient when diagnosing premature completion.

The operation changes only the three ignored routing files and their
content-addressed guard, recording original and replacement bytes before writes.
It retains the original preparation, executable binding, provider epochs, quota
holds, requests and review counts. An existing fourth-review grant must already be consumed
by the completed accepted review of the current exact spec; an unused or rejected
grant cannot be refreshed. A crash leaves ordinary routing blocked until the
identical request reconciles unchanged evidence. While its intent is pending,
specification, instruction and grant changes, and sync observation writes, are
blocked before they can invalidate recovery. Saved status remains readable.
Unknown file changes are never overwritten. Keep the native controller stopped and
its owner/evidence unchanged from intent publication until the refresh completes.
A restart or concurrent lifecycle change during that interval can invalidate the
exact retry and requires operator diagnosis; do not remove or rewrite the journal
to unblock it. Reload the retained controller separately and verify native identity
and loaded settings before an independently authorized prompt. Neither refresh nor
reload grants continuation, clears quota, or proves live enforcement or savings.

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

A completed report that puts malformed strings (for example native Agent IDs) in
`routing_log[].delegate_job_ids` still fails engineering capture. That field is
reserved for the room's provider ledger IDs; record native worker attribution
separately. The existing engineer `purpose: correction` operation can admit a
report-only repair when the full report is otherwise valid, the original
completion receipt and candidate are intact, and the same engineer/spec/handoff
still apply. It verifies every well-formed provider claim first, regardless of
where malformed strings occur; unknown or foreign IDs, wrong provider identities,
damaged results and unresolved jobs remain refusals. A rejected string is not
thereby verified as a native worker ID.
In a room without a delegate provider, this repair applies only when every named
ID is malformed; any provider-shaped ID remains refused.

The successor intent carries controller-authored `report_correction_admission`
evidence binding the rejected report, original candidate and verified provider
jobs. The original failure and receipt remain unchanged. The candidate must be
unchanged at dispatch, result capture and acceptance. Recovery is not available
for scope changes, semantic holds, missing completion evidence or a provider
transition; the separate historical-provider correction retains its own rules.
A corrected report still requires normal strict capture, formal verification and
independent acceptance. Reissuing the identical request remains idempotent;
unknown delivery is never retried under another request ID.

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
retained Claude role and AO project/conversation/branch against read-only native
ownership storage. An engineer keeps its immutable prepared-workspace check. An
Astra-led Claude reviewer must be bound at MAX and needs no engineer preparation: the exact native caller
and every correlated root assistant row must carry the retained owner's workspace,
and the request, binding and observed model/effort must match. Every correlated root
assistant row needs an exactly matching `cwd`, and each non-synthetic,
non-API-error row must name the pinned model even without a terminal stop. Missing
`cwd` on an error row makes the source unknown; it cannot establish a quota diagnosis.
These comparisons do not normalize paths or infer missing fields. Reviewer source
paths and workspace are saved separately from engineer evidence; later path
audits cannot replace that reviewer native identity or workspace. It reads the explicit owned transcript only,
never credentials or account settings. Correlation uses the caller's exact
digest, native session and bounded human-message interval; compaction summaries,
tool results, queue metadata and child sessions are not caller messages.
Malformed, changed or ambiguous evidence holds rather than authorizing work.

The explicit operator-supplied read-only owner storage and matching transcript are
the first reviewer workspace trust anchor. This is not an independent preexisting
room proof or a candidate-directory inference, and the workspace need not still
exist. The first audit pins that identity and workspace; later supported calls may
move evidence paths but cannot replace the owner or workspace. Mutually consistent
forged local evidence is outside this trust model. A wrong first pin requires
operator diagnosis, never editing saved state to bypass it.

Establishing an explicit source preserves an existing unknown hold. Run a second
`ao_room_outcome_audit` for the same role without source paths to reassess the
unchanged verified source. A proven native `end_turn` can then establish that an
unstructured response finished. An operator must still review the complete raw
response and submit exact receipt/text hashes and a JSON span to
`ao_room_response_normalize`; exact candidate, gates, verdict and review-budget
checks remain required for acceptance. Neither audit parses an approval from prose,
accepts the candidate, clears a known quota failure, or grants another review.

After diagnosing the failure and obtaining actual authorization to continue,
call `ao_room_outcome_resume` with the failed `request_id`, the fresh audit's
`outcome_sha256`, one unused `resume_request_id`, `diagnosis` and `authorization`.
The operation makes no model call. Only the named successor can pass that hold;
it is still subject to the normal session, model, delegate, scope and review
checks. Send that successor through ordinary `ao_room_send`. Never replay the
old request. A new user instruction to resume can supply authorization; elapsed
time, a presumed reset or the original feature request cannot.

### A completed receipt captured with truncated history

An explicit outcome audit can reconcile one narrow historical case: the owned
request and its AO turn completed, but the immutable receipt was captured with
`history_truncated: true`. The current strict history read must be complete, all
owned messages must match the original receipt exactly, and fresh verified native
evidence must establish a correlated terminal quota error with a unique caller
anchor and no later human instruction. Partial-message reconstruction, uncertain
delivery, generic provider failures and successful-final promotion are excluded.
The ordinary classifier continues refusing the original truncated receipt.

The audit saves a separate content-addressed reconciliation proof. It binds the
original receipt and caller digests, session/turn/provider/conversation/branch,
baseline and current turns, owned messages, typed failures and exact verified
native source. Original receipt bytes, truncation truth, usage uncertainty and
historical outcomes remain unchanged. The result is still a quota hold; the
separate authorized `ao_room_outcome_resume` and named successor are required.

Ordinary sync cannot originate or renew this authority. Subsequent observations,
release calls (including identical repeats) and successor admission revalidate
the proof. Changed or unknown evidence invalidates it through an immutable
invalidation chain. Refusals before outcome observation, such as an identity or
strict-reader failure, also invalidate an established proof. Conservatively, any
failed locked room operation while that proof is current requires another
explicit outcome audit, even if the cause was an incorrect operator command.
If malformed request ordering makes the latest proof unknowable, every proof
for that role is invalidated conservatively; restoring order still needs a fresh audit.
Restoring old bytes never restores the prior continuation digest. A fresh audit
must verify the evidence and create a new proof linked to that invalidation;
the separate continuation authorization must then bind the new outcome digest.
Corrupt proof or journal records must be diagnosed and preserved, not edited.
When exact-byte restoration is needed, retain the damaged file as diagnostic
evidence and restore only a trusted backup whose bytes match the originally
recorded digest. Missing or unverified backups remain a blocker; do not invent
replacement records or remove the invalidation chain. Even after exact restoration,
run a fresh supported outcome audit and bind any new release to its new digest.

An existing one-charter review grant retains its original proof and invalidation
ancestry when an explicit audit renews them. Local retention checks authenticate
the original records and subsequent links; missing, altered or unrelated history
still refuses. New grants inventory this history, while earlier grant records
remain unchanged. A legitimate stale proof can reach explicit audit, but this
check cannot establish current delivery, release a hold or add a review allowance.
The added traversal reads at most 8 MiB per record and 64 MiB total, with at most
1,000 invalidations per chain. Current native evidence and the named release remain required
before a successor can run.

If an upgrade follows an interruption after an older grant receipt was saved,
the identical pending request can still finish using its original audit and
receipt. Compatibility permits only the newly authenticated history inventory;
all other evidence must be unchanged and pass the second inspection before
commit. A changed payload, a partial history inventory or an old audit without
an existing pending receipt receives no such exception.

This lane does not establish quota availability, clear a provider safety refusal,
resume an uncertain failed transport or reset review budgets. An active delegate
or native owner continues to block recovery. No model is called by reconciliation.

An AO `failed` turn ordinarily remains uncertain. There are three narrow settlement
exceptions, each requiring its own exact positive evidence. The first is a
matched native API quota error with exact delivery evidence, an idle retained
owner and actual continuation authorization. The second is the separately
documented exact autocompact-thrashing compaction diagnosis, which is not quota
and is not a generic provider-error recovery: it requires the exact native
public-error proof and a fresh positive diagnosis, and contradictory quota,
safety or refusal evidence disqualifies it. The read-admission mitigation for
that compaction lane binds only the exact settled diagnosis; it does not let a
contradictory or ambiguous record bypass the fresh positive proof. See
[compaction-failure recovery](../operations/compaction-failure-recovery.md).
The third is the audited complete-history lane below for the same quota
diagnosis when the saved receipt itself was captured with truncated history.
The original failed receipt, partial work, native identity and consumed attempts
remain intact in either case. Neither state is successful engineering or
acceptance. Unknown delivery, arbitrary crashes, unresolved paid delegates and
generic transport failures cannot use these exceptions. Review budgets are never
renewed here.

History reads project large activity payloads before accumulation and before
the overlap comparison: an activity row whose `activityKind` is not one of
`system`, `error`, `approval`, `user_input`, `plan` or `usage` retains its
identity, kind, status, summary and timestamps, while its `detail` is replaced
by a marker recording the omitted byte count and the SHA-256 of the canonical
original. The per-response bound still applies to the raw page; only the
projected rows count toward the aggregate bound, so a long session's complete
history still fits the bounded read. The typed `provider.failure` details used
by failure classification are on the retained list and arrive verbatim, and a
payload-only change still alters the marker digest and fails closed.

### A failed receipt captured with truncated history

The same audited reconciliation structure covers one narrower case: the owned
request remains `uncertain`, its saved receipt has `history_truncated: true`,
and the receipt's failed AO turn — same turn and provider-turn identity and the
same error message — is still the failed turn in a fresh complete strict
observation. AO clears a failed turn's error message when its native controller
restarts, so the lane binds the provider error text through the preserved
receipt instead of the live AO row, whose message can stay the same or become
empty but can never change to a different message. Verified native evidence
must again establish the typed quota error with a unique owned caller anchor,
no later human instruction, an idle owner and no unproven extra turns. The separate proof additionally binds the
saved failed turn itself; ordinary sync can only validate or invalidate it, and
only an explicit outcome audit creates or renews it. The original receipt bytes
and the failed AO state are never rewritten, and a successful retry or final
response is no ground for this proof. A valid proof lets a separate authorized
`ao_room_outcome_resume` settle the request as `settled_failure` and admit its
one named successor; changed or unknown evidence, a non-quota native outcome,
and any active delegate refuse and invalidate through the same immutable chain.

## Session window notice

A send after a quota-limited turn does not need an operator to repeat the window
arithmetic. When the latest engineer request's semantic outcome stands at
`quota_limit`, every later engineer `ao_room_send` of any purpose appends one
controller-derived notice fragment after the workflow parts. It names the
previous request and quotes the provider's own reset text — the saved outcome's
terminal `errorMessage` first, then the request's observed snapshot, bounded to
200 characters, else a generic phrase. The zone named in that text sets the
rendered times (UTC when unnamed or unreadable): the notice reports this send's
dispatch instant and, if the provider's five-hour window applies as before and
starts with this request, the expected limit no later than about five real
hours later — earlier if the account was used elsewhere since the reset. The
notice is not a one-time workflow part: it accompanies every send while the
diagnosis stands, and the request's `carried.session_window_notice` records the
previous request id, the reset text, both instants in ISO UTC and the fragment's
digest. It grants no new instruction, does not clear the semantic hold and does
not prove the provider's window behaves as before; the authorized-resume path
above still applies.

## Affected AO bridge workaround

The reviewed AO 0.13.0 bundle contains a Claude ACP result path that checks
`max_tokens` before `is_error`. The explicit `ao_acp_patch.py` operator tool adds
`!message.is_error` to those two truncation conditions, allowing the existing
typed failure handler to run. It accepts only an exact reviewed source SHA-256 (the 0.13.0/0.13.1 module and, since the review below, the 0.13.3 module),
writes a private original-byte backup and immutable patch intent, and refuses an
unknown/new upstream module. It is not run by installation, dispatch or an
updater. No model, effort, output budget or provider is changed.

The experimental completion barrier remains available as an offline transform for
compatibility probes, but **installation with `--completion-barrier` is disabled**.
Inspection of Claude Code 2.1.268 and source-derived consumer probes showed that a
standalone task notification can omit both its SDK replay and result UUID. A
notification injected during another turn can instead share that turn's primary
result UUID. The proposed exact notification/result match therefore cannot finish
these real paths. Widening the XML parser does not repair missing identity; a
worker-terminal event or empty task list does not prove Fable processed the result.
The rejected experiment and its tests remain inspectable without modifying a
runtime, accepting an old completion, or rewriting any prior patch receipt.

New preparations and the audited routing refresh set
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in ignored local settings, using
[Claude's documented foreground option](https://code.claude.com/docs/en/subagents#run-subagents-in-foreground-or-background). The new guard
requires that setting in its inherited environment before a native Agent launch
and refuses an explicit background request. The pinned local Sonnet/Opus workers
still execute delegated work with their own contexts and MAX settings; Fable waits
for their result before continuing. The existing isolation, fork, team and nested
worker restrictions remain intact. DeepSeek's external submit/status/result jobs
are unchanged. The reviewed native version also disables automatic migration of
long foreground workers; this is not a new task timeout or model budget.
Independently of the guard, the worker-identity audit records every unrecognized
Agent input key: keys that could steer what runs or what it binds to (model,
isolation, resume, fork, session, team, identity, tool, settings or
working-directory overrides) still refuse as `launch_unsupported_override`, while
any other recorded key is listed as `extra_keys_ignored` and the child's own
bounded evidence alone decides identity.

New preparations and the audited routing refresh also pin
`CLAUDE_CODE_ENABLE_TODO_TOOLS=true` in the ignored local settings: headless
Claude Code offers the TaskCreate/TaskUpdate/TaskList/TaskGet tools to current
models only with it, and the `progress_plan_v3` part — which consolidates and
supersedes the frozen `progress_plan_v1`/`progress_plan_v2` list-keeping rules,
never resent once delivered — and AO's plan panel depend on them.

Keep the existing precedence-only ACP workaround. A stopped, positively quiescent
retained controller must reload the audited settings and guard before new work;
no daemon restart or experimental runtime switch is needed. Historical preparation
and routing-policy records remain unchanged, and the refresh journal binds the
new settings bytes. Old flagless configurations remain readable but are not
relabelled as foreground-protected. A pending historical adoption still requires
its exact recorded guard/wrapper payload for reconciliation; preserve the pinned
controller version for that operation, then perform the reviewed refresh. A new
installed guard must not be substituted into the old audit. The guard refuses new native submissions if
the required flag did not actually reach it. Validate the selected Claude version
against this behavior when upgrading; the environment setting alone does not
prove that every historical or future version enforces it correctly.

For protected app bundles, copy the packaged ACP runtime to a separate operator-owned directory, patch that copy, and configure AO’s supported `AO_ACP_RUNTIME_DIR` override at daemon startup. Verify the copied runtime and upstream source hashes before each start; an upstream update requires a new compatibility check. This preserves the official app. Restart the idle daemon to select the override, then explicitly exit/resume each affected idle native controller. Persistent ACP hosts can survive a daemon restart; verify the new process uses the copied runtime and retains the same native conversation, without sending a model prompt.

The reviewed AO 0.13.3 bundle's `acp-agent.js` (upstream SHA-256
`64cce3640aba406202e2fd1f4b6f65191ccea4db3b2cb5cb69edc97f94c5d8e4`) differs from
the 0.13.1 module only by an additive block that calls the SDK's
`getContextUsage()` with a two-second timeout for AO's context display; both
`max_tokens` precedence anchors are unchanged. `ao_acp_patch.py` accepts it as a
second reviewed source and pins the produced module to the reviewed patched
digest `af5179e7a87f4eab3e9f8c114c148801aafe00a1fcdaca450395e8d2d1174477`,
refusing every other upstream module exactly as before. To switch a running
daemon to the 0.13.3 bundle between jobs: back up the AO state, stop the daemon
with no owned turn running, start the 0.13.3 bundle with the same `AO_DATA_DIR`
and tmux environment and `AO_ACP_RUNTIME_DIR` pointing at a runtime patched from
the 0.13.3 bundle, then run a `/healthz` and session-list smoke test and
`ao_release_check` (`up_to_date`) before new work. Roll back with the retained
0.13.1 bundle, the old runtime directory and the state backup.

Verify that the affected runtime is idle before using the tool. Existing native
controllers must reload the patched module through a separately verified
stop/start preserving the same native session; changing disk bytes alone does
not patch an already loaded process. This is a local vendor workaround; patching an app-bundled path directly changes its resources. Preserve the original official bundle and its
provenance. Recheck official stable releases and compatibility at the next
update; do not carry this patch blindly to another version or label modified
bytes as a pristine vendor installation.

## Evidence and remaining validation

Offline tests cover quota precedence, stale and ambiguous evidence, exact-once
continuation, restart persistence, immutable failures, schema projection and
one-time instruction delivery. They use fake AO and synthetic native evidence.
The experimental completion barrier has offline probes of the exact reviewed
vendor consumer, including source-derived missing-UUID paths. These demonstrate
why the transform cannot be installed, rather than certify native asynchronous
completion. Foreground delegation avoids that detached notification path. Its
actual useful work, quality and parent result handling still need live observation;
static source and synthetic tests are not a subscription-savings measurement.
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

AO can import a saved Claude compaction summary as a recovered human turn on
native resume. The native outcome audit recognizes only a transcript-only
`isCompactSummary` record in the exact retained session, with its length-prefixed
AO native identity and complete text digest matching the single imported user
message. That proven context import may pass the existing provider-epoch history
gate; it is never a new command, completion or quota reset. Ordinary recovered
turns, lookalike summary prose, extra replies and changed bytes remain blocked.

This compaction-import allowance applies to continuation within an already
committed provider epoch. It does not broaden initial provider-transition
eligibility or recompute an existing epoch's frozen history digest.

AO can also import a typed SDK task notification as a recovered human turn on
reload. The engineer outcome audit records this separately as
`task_notification_imports`; it is not compaction. The supported shape is narrow:
an owned same-native user event with exact task-notification origin, SDK metadata,
workspace, UUID and a plain six-field notification envelope whose status is
`failed`. Its complete native-row and text hashes must match one nonstreaming
recovered user message and the exact branch-derived provider identity. The
notification's output path is never opened. Before provider history uses the
proof, the owned source and the exact imported turn/message bytes are rechecked.

This proves only where existing context came from. It does not prove worker
completion, authorize new work, establish acceptance or release any quota/error
hold. Launch correlation is not required for this context identity; it would
still be necessary for a separate worker-settlement claim. Reviewer imports,
other statuses or envelopes, human lookalikes, duplicate identities and changed
or unverified evidence remain unsupported. Preserve the original failure and
audit records and diagnose the exact source instead of rewriting recovered
turns. As with compaction, provider history admits these proofs only within an
already committed epoch; initial transition eligibility and frozen history stay
unchanged.


## A pinned Claude executable disappeared

A Claude app or CLI update can remove a versioned binary that AO still uses.
AO may then fail `session/load` before a controller or model request starts.
Keep the native session stopped while diagnosing the exact launch path and
recorded preparation. Changing only a symlink does not update Project Room's
historical executable identity check.

The operator can repair this through `ao_executable_binding.py`. Prepare an
independently retained copy of the selected user-installed executable, inspect
its real version and bytes, and set AO's observed launch path to that copy.
Then invoke the module with `--home`, `--room-id`, an unused `--request-id`,
`--executable-path`, `--launch-path`, `--database-path`, and the actual
`--authorization` and `--diagnosis`. All paths must be absolute; the executable
must be a canonical owned file without symlinks. The launch path may be an
owned symlink that resolves to that exact file. The helper does not install,
retarget, start or resume anything, and runs only a bounded `--version` probe.

Repair requires a previously recorded executable that is now missing or
changed, an idle stopped bound engineering orchestrator at its current effective
model and MAX in the same native conversation and branch, complete settled
transport history, no unsettled owned request and unchanged routing files and
provider attachment. A separate immutable journal
records the actual replacement SHA-256, size, mtime, version and launch path.
The original preparation, provider epochs, routing adoption, requests, quota
holds and review limits remain unchanged. Every subsequent routing check
validates the journal, actual bytes, launch path and retained native identity.
Status presents both the original and current executable identity.

A failed or recovered historical turn remains failed or recovered; this
operation neither accepts it nor grants continuation. A receipt written before
an interrupted state commit holds further work and can be reconciled only by
the identical request against unchanged evidence. After repair, the assigned
operator may perform the separately authorized same-session resume and verify
the new process, MAX, native continuity and MCP connection before any prompt.
Configured identity and an idle reload do not prove native routing enforcement,
model quality, quota availability or token savings. Never spoof metadata or
rewrite historical pin receipts to make a replacement appear unchanged.

Executable repair is bound to the exact current preparation. A later provider
or routing-preparation transition is not implicitly supported by this receipt;
it needs its own compatibility handling. The operator must also prove that AO
uses the supplied launch path, since a valid arbitrary symlink alone is not
evidence of the live process executable.

A separate, explicitly authorized upgrade lane moves an intact recorded
executable to a strictly newer, exactly matching qualified version instead of
repairing loss or drift, keeping the original preparation and every earlier
journal. Invoke the same module with the same required arguments plus
`--upgrade-authorization` and `--expected-version X.Y.Z`; it refuses when the
recorded executable is missing or has changed (use the repair lane above
instead), when the replacement's actual version does not exactly match
`--expected-version`, or when that version is not strictly newer than the
recorded source version. It requires the same stopped-owner, idle, settled and
unchanged-routing preconditions as repair, and runs only the same bounded
`--version` probe — never an inference, a lifecycle action or the release of a
quota or other hold. The journal records outcome `authorized_upgrade` together
with the source and target versions. See
[configured engineering model selection and transition](engineering-model-transition.md#executable-prerequisite-and-operator-sequence)
for the room-level sequence that uses this lane before an engineering model
transition.
