# AO lifecycle contract and coverage map

This map states the supported states, transitions, actors and required evidence of a
normal AO room (`workflow: "fable_engineering"`), names the controller check that
enforces each transition today, and names the offline proof that exercises it through
the production boundary. It is derived from the code at the revision that introduced it
and from three sanitized incident records (capacity, failed native startup,
automation-initiated reconciliation); every claim below is either a code anchor
(`module.function`) or is labelled as a gap. Legacy `room_*` rooms are outside this map.
The repairs for the three incidents keep their existing owners; this map and its harness
define shared invariants and missing coverage, and they are not a prerequisite for any
room's release.

`test_ao_lifecycle_contract.py` is the executable form: end-to-end scenarios through
`ao_project_room.Service` and `project_room_mcp.handle` with the fake AO backend, asserting
durable state, dispatch counts and review counters rather than returned strings. Its
`OPEN_GAPS` registry is the single list of contract clauses the baseline does not meet or
that remain unproven; the ids below match it.

## Actors

| Actor | Role in the contract |
| --- | --- |
| Astra (operator task) | Drives every tool call; owns intent, spec revisions, finding dispositions and independent acceptance. Never implements in a normal room. |
| Fable (native engineer session) | Owns engineering judgment and the engineering report. Reached only through `ao_room_send`. |
| Codex reviewer (native reviewer session) | Independent acceptance verdict for exact identities. |
| Controller (this plugin) | Persists intents before dispatch, reconciles native history read-only, binds evidence by digest, refuses ambiguity. Runs no model. |
| AO | Session host and transport. Its acknowledgement and history are evidence, not authority. |
| Delegates (DeepSeek, `pr-sonnet`, `pr-opus`) | Supporting workers under Fable; their ledgers are verified at capture and acceptance, never trusted from the report alone. |

## Room-level states

Durable in `state.json` and the files it names; each is entered by exactly one operation.

| State | Entered by | Durable evidence | Leaves the state |
| --- | --- | --- | --- |
| opened | `ao_room_open` | workflow, delegate provider, `git_common_dir`, authorization | never (rooms do not migrate) |
| spec pinned | `ao_room_spec_put` | `specs/<n>.json` (content, SHA-256, gates, approval); `spec_record_sha256` | a strictly newer revision; the old file stays immutable (`Spec revisions are immutable`) |
| prepared | `ao_room_prepare` → `ao_delegates.prepare` | `preparation.json` (worktree, provider, delegate digest), `preparation_status` `pending` → `configured`, routing files snapshotted under the worktree before the record exists | never: `Prepared engineer workspace is immutable`; see GAP-FS-1 |
| bound | `ao_room_bind` | `bindings[role]` with session, model, effort, conversation and branch identity | never: `Role bindings are immutable` |
| agreed | a completed `spec_review` request whose verdict accepts the exact current revision (`ao_workflow.agreement`) | the request receipt and its `spec_record_sha256` | any newer spec revision |
| handed off | `ao_room_handoff` → `ao_workflow.handoff` | `handoffs/<spec_record_sha256>.json` (worktree, baseline commit, initial candidate digest, agreement, preparation digest, gates) | a newer agreed revision; a historical handoff is never replaced |
| engineering captured | `ao_room_sync` → `ao_workflow.capture_engineering` | `engineering/<request>.json`, `completion-candidates/<request>.json`, `request.engineering_record` | a later correction request |
| verified | `ao_room_verify` | `verifications[]`, `checkpoint` (passed, candidate digest, gate logs by digest) | any candidate change (`checkpoint` refuses) or a new spec |
| accepted | `ao_room_accept` | `acceptances[]` (review request, identities, receipt digest) | never; a later revision starts a new cycle |
| integrated / installed | outside the controller (operator card phase 7) | none in room state | — |

Status cannot report "integrated" or "installed": acceptance records nothing about
publication (`publication: "not performed"`), so a status surface that claims it would be
inventing evidence. The map treats that phase as out of contract.

## Request-level states

One `ao_room_send` creates one request record; `_sync_once` is the only writer of its
state after dispatch.

| State | Meaning | Set by |
| --- | --- | --- |
| `uncertain` (initial) | intent persisted before any POST (`Persist intent BEFORE any request may reach AO`) | `ao_room_send` |
| `submitted` | AO acknowledged with a `turnId` | `ao_room_send` |
| `uncertain` + `delivery_error` | the POST raised; AO may or may not hold the message | `ao_room_send` |
| `running` | the exact sent message was found on a new turn that is not terminal | `_sync_once` |
| `completed` | native turn `completed` with a provider turn identity | `_sync_once` |
| `uncertain` + `reconciliation` | native turn terminal but not completed (`failed`, `cancelled`, `interrupted`, `recovered`), or no unique exact sent message observed | `_sync_once` |
| `settled_failure` | a freshly audited resumable failure with recorded human authorization for one named successor | `ao_outcomes.resume` |

Only a `failed` native turn with correlated typed failure evidence has a settlement lane
(T8). An `interrupted` or `cancelled` AO outcome stays `uncertain`, blocks every later
dispatch through `Service.settled`, and `ao_room_outcome_audit` refuses it at the same gate
(`An owned request is active or uncertain`); the AO guide documents this as a deliberate
absence ("no automatic recovery lane for such outcomes; diagnose them without resending").
The harness pins the hold (`InterruptedAndQuotaTests`); the map records the absence as
design, not as an open gap.

Orthogonal to transport state, every completed engineer turn carries a semantic outcome
(`ao_outcomes.classify`): `final_available` (no hold), the resumable holds `quota_limit`,
`provider_error`, `output_truncated`, `model_mismatch`, or `unknown` (hold; human diagnosis).
An implementation or correction turn also carries an engineering capture result:
`engineering_record` or `engineering_error`. The request stays `completed` in both cases —
a malformed report is a completed turn with an error, never a failure and never a success.

## Transitions, required evidence and enforcing checks

| # | Transition | Required evidence | Enforcing check (today) | Offline proof |
| --- | --- | --- | --- | --- |
| T1 | spec pinned → agreed | completed engineer `spec_review` for the exact revision and SHA-256 at MAX, verdict `accept`, no `BLOCKER:` finding | `ao_workflow.spec_review_report`, `ao_workflow.agreement` | `test_ao_normal`; both harness templates |
| T2 | agreed → handed off | agreement, bound engineer whose AO workspace is the prepared worktree, settled delegates, clean baseline commit | `ao_workflow.handoff`, `ao_workflow.workspace` | `AgreedRoomFixture`, `ProgressedRoomWorkspaceLossTests` |
| T3 | any → new dispatch | no active or uncertain request; every terminal result has native delivery identity; failures settled; no running verification; idle native conversation; history within the accounting window; readiness and worker qualification | `Service.settled`, `Service.quiet`, `ao_outcomes.gate`, `ao_model_boundaries.readiness` | `InterruptedAndQuotaTests`, `DuplicateAndUncertainDeliveryTests`, `LongHistoryTests` |
| T4 | dispatch → acknowledged | intent saved first; AO `turnId` | `ao_room_send` | `NormalCompletionTests`, `DuplicateAndUncertainDeliveryTests` |
| T5 | acknowledged/uncertain → running/completed | one exact sent message on a turn absent from the baseline turn set; unchanged conversation and branch identity; no provider-turn identity change | `sent_message`, `_sync_once` | `NormalCompletionTests`, `DuplicateAndUncertainDeliveryTests` |
| T6 | native terminal → receipt | turn, its messages, settings, usage, session and activity failures saved once by digest | `Service.observation`, `_sync_once` | `InterruptedAndQuotaTests`, `ChangedCandidateTests` |
| T7 | completed implementation → engineering captured | first-observation candidate snapshot, usable semantic outcome, report shape and bound metadata, verified delegate claims, candidate unchanged since first observation | `ao_workflow.completion_candidate`, `ao_report_contract.project`, `ao_workflow.engineering_report`, `ao_delegates.verify_delegation` | `NormalCompletionTests`, `MalformedReportTests`, `ChangedCandidateTests` |
| T8 | failed turn → settled failure | read-only audit of the retained outcome, resumable kind, human authorization text, diagnosis, one named unused successor | `ao_outcomes.audit`, `ao_outcomes.resume`, `ao_outcomes.validate_settlement` | `InterruptedAndQuotaTests` |
| T9 | captured → verified | gates as argument arrays, candidate fingerprint unchanged across the run, logs by digest | `ao_room_verify`, `Service.checkpoint` | `NormalCompletionTests`, `ChangedCandidateTests` |
| T10 | verified → acceptance review sent | review identities (spec, candidate, evidence digests) embedded in the request; three attempts per room | `ao_room_send` reviewer branch, `ao_acceptance_extension` | `NormalCompletionTests`, `MalformedReportTests`, `ChangedCandidateTests` |
| T11 | reviewed → accepted | completed reviewer turn with provider identity, unmodified receipt, `approved` verdict naming the exact identities, captured engineering result still current, no remaining gaps or pending operator requests, delegate evidence re-verified live | `ao_room_accept`, `ao_workflow.engineering_ready` | `NormalCompletionTests`, `MalformedReportTests`, `ChangedCandidateTests` |
| T12 | agreed/handed off → stale | a strictly newer spec revision | `ao_room_spec_put`, `ao_workflow.retained_handoff(current_spec=True)` | `ChangedCandidateTests` |

### Where malformed reports are caught

The earliest supported point is the sync that observes the completed turn:
`capture_engineering` records `engineering_error` and never an `engineering_record`, and
later syncs do not retry or upgrade it (`MalformedReportTests`). Downstream work is then
refused before it costs anything: `ao_room_verify` calls `engineering_ready` before any gate
runs (`Acceptance requires a captured completed engineering result`), the reviewer send
needs a passed checkpoint, and `ao_room_accept` repeats the readiness check. No review
attempt is consumed. Detection therefore needed no change; visibility did: status now carries
`engineering_capture` (the latest engineering request's capture status, its `engineering_error`
and currentness), and the verify-time refusal names the request and its error instead of
repeating the acceptance wording. Eligibility, ordering, counters and gates are unchanged.

### Bounds the harness pins as observed behavior, not as contract

- The candidate-at-completion is the candidate at the controller's first observation after
  the native turn completed; a change made between native completion and that sync is bound
  into the record, not detected (`ChangedCandidateTests`, captured silently), and nothing
  downstream distinguishes it from the worker's own output: the gates run on the bound
  candidate (the harness fixture's gate fails only because it checks the changed text; a gate
  that passes on those bytes would verify them) and acceptance binds to the same candidate.
  Closing that window would need evidence the controller does not have, such as the worker's
  own candidate digest at completion; it is recorded here as a bound, not a protection.
- A changed `completedAt` on an owned completed turn is admitted at the next observation and
  mints a new semantic-outcome record: **GAP-TT-1**, adopted from review finding B5 of the
  automation-reconciliation lane. It is unassigned. Binding it strictly needs evidence of how
  AO rewrites live turn rows on restart (a failed-turn proof already binds the provider error
  text through the preserved receipt because AO clears the live row on restart), so the
  correction is deferred until that evidence exists rather than risking a refusal of every
  post-restart observation.
- A missing prepared worktree refuses through `ao_room_prepare` and `ao_room_bind` with the
  raw `FileNotFoundError` from `project_path`, and the MCP error payload carries that OS text
  (finding F-1, below).

### Safety clauses that hold across every transition

- A refused operation never changes authority: no dispatch, no request intent, no counter,
  no spec, preparation, handoff, receipt, settlement or acceptance change. Whether a refusal
  also leaves an append-only diagnostic trace differs by operation; the table below is what
  the harness observed, where `refuses()` snapshots every durable effect (every file in the
  room directory by digest, the complete parsed `state.json` compared value by value as dotted
  paths, the review counters and the POST count) before and after each refused call and
  requires an empty difference unless an explicit allowance names the exact paths and files
  that may differ. The harness checks itself: a synthetic refusal that rewrites an existing
  request's `spec_record_sha256` or an acceptance's `candidate_sha256` with no key or count
  change must fail the snapshot (`HarnessSelfCheckTests`).

| Refused operation (observed in `test_ao_lifecycle_contract.REFUSAL_EFFECTS`) | Durable effect of the refusal |
| --- | --- |
| `ao_room_prepare` (removed prepared worktree; fresh worktree on a prepared room) | none |
| `ao_room_bind` (removed prepared workspace) | none |
| `ao_room_send` (role never bound; routing evidence lost; owned request active or uncertain; not the named outcome successor; identical id with another payload; unowned completed turn; unverified recovered context; candidate changed after verification; no passed checkpoint; snapshot `history_truncated`; spec record changed) | none |
| `ao_room_handoff` (no current agreement; spec record changed) | none |
| `ao_room_verify` (no captured engineering result) | none |
| `ao_room_accept` (no captured engineering result; receipt bytes modified; prefix or foreign verdict identity; spec record changed) | none — `observe()` on identical reviewer evidence returns the existing record without minting |
| `ao_room_outcome_audit` (owned request still uncertain; unowned completed turn) | none |
| `ao_room_outcome_resume` (extra completed turn after the audit) | none |
| `ao_room_outcome_resume` (the failed turn's own evidence changed after the audit) | append-only: `observe()` mints one new `outcomes/<request>/<sha>.json` and moves exactly `requests.<id>.semantic_outcome` and `requests.<id>.semantic_outcome_sha256` before the digest check refuses with `Outcome evidence changed or does not establish an eligible diagnosed failure`; the request stays `uncertain`, no settlement is written |

  A stronger "no write at all on refusal" target is therefore **unmet** for that last case
  and is left as observed: the appended record is evidence of what was seen, the original
  failed receipt is unchanged, and no authority moved. `Service.locked` can also invalidate a
  retained history-reconciliation proof when an operation inside it refuses; no refusal in the
  harness exercised that path, and if one did the whole-state snapshot would show its exact
  paths and this table would have to name them. A successful `ao_room_sync` is not a refusal
  and re-stamps `routing_rules.observed_at` on every iteration; the harness names that one
  path wherever a measured span contains real syncs.
- Uncertain delivery is held, never resent: an identical `ao_room_send` reads the saved
  record; a different payload under the same id refuses; nothing resends under a new id
  while a request is active or uncertain.
- Historical evidence is preserved after successors begin: settlement keeps the original
  failed receipt; a correction keeps the previous receipt; a superseded spec revision keeps
  its file.
- Authorization binds to the exact evidence acted upon: `ao_room_outcome_resume` binds the
  audit digest and the successor id; `ao_room_accept` binds the review's spec, candidate and
  evidence digests and the receipt digest.

## Incident families and what the baseline does

### Failed native startup (sanitized record, 2026-10-18)

Observed: preparation and routing succeeded; AO's native session creation failed
definitively (HTTP 409 after several minutes, client timed out first, original body
unknown); AO removed the prepared workspace and kept an empty seed session (zero turns,
messages and activities; no native-owner row; no session-worktree row); the room had no
engineer binding and no request.

Expected: retain the room, the exact specification, the original failure and the review
allowance; support audited re-preparation only after proving no delivered work and no live
owner; refuse ambiguous or progressed state.

Baseline (`FailedStartupTests`): the safety half holds — every ordinary path refuses, no
request or counter changes, the spec and `preparation.json` stay byte-identical, and status
reports the delegate attachment as `unverified` with the error. The progress half is absent:
`ao_delegates.prepare` pins the first worktree forever and `validate_preparation` refuses a
missing one, so no supported operation re-prepares the room. This is **GAP-FS-1**; its repair
is owned by the failed-startup recovery lane on an unpublished controller branch and is not
reimplemented here. Two findings travel with the gap for that owner:

- F-1 The missing-worktree refusal surfaces as the raw OS error from
  `project_path(..., strict=True)` (`FileNotFoundError`) rather than a `RoomError` that names
  the pinned worktree and the supported next step; through MCP the operator sees the OS text.
- F-2 Whether an empty seed session (zero turns, no owner row) is "no delivered work" cannot
  be proven from `ao_room_status` alone; the audit the owner adds must bind fresh correlated
  no-owner/no-work evidence, not the retained controller-generation value.

### Automation-initiated reconciliation (sanitized record, 2026-10-18)

Observed: a room held a completed owned engineering result with retained verification and
acceptance. PR CI then failed on one Python 3.12 test-portability case, and AO's own
CI-failure automation started a native Fable turn in the same engineering session with no
Project Room request (`origin=automation`). That turn fixed the tests and documentation,
committed, pushed and completed; CI passed. Bounded metadata showed exactly one extra
completed automation-origin turn beyond the owned baseline and unchanged prior owned
evidence. `ao_room_outcome_audit` and a report-only `ao_room_send` both refused with
`Cannot attribute semantic outcome to an unchanged completed native result`
(`ao_outcomes.observe`: the non-recovered turn set must equal the baseline plus the owned
turn). The send refused before dispatch: no intent, no turn, no counter.

Expected: that refusal (safe); then an explicit read-only audit and a narrowly authorized
reconciliation of the exact positively proven external turn that permits a new ordinary
report in the same retained session, with capture, verification and acceptance still
following.

Baseline (`ExternalTurnTests`): the refusal holds and nothing is written or consumed. No
supported audit or reconciliation lane exists: **GAP-AR-1**, owned by the
automation-reconciliation repair lane on an unpublished controller branch whose first
complete candidate was independently rejected. The rejection findings are contract clauses
this map adopts for every lane, and the harness probes the existing lanes against them:

- reconciliation lineage stays bound and revalidated through acceptance after a successor
  becomes the latest request;
- an audited authorization consumes the exact reviewed evidence-set digest and refuses
  drift without writing (`InterruptedAndQuotaTests`, resume after evidence change);
- message, model, tool and worker correspondence must be complete; unavailable proof refuses;
- artifacts bind by complete digests of actual files, index and HEAD, never by prefix or
  handback text (`ChangedCandidateTests`, prefix identity in a verdict);
- immutable terminal-turn fields must match at audit and admission
  (`ChangedCandidateTests`, changed `completedAt` after capture).

Supported AO readers expose `origin=automation` but no trigger-level identifier; the map
does not invent one, and nothing here connects this incident to any other PR.

### Capacity (PR #218)

Complete native histories above 64,000,000 bytes were refused before their completion and
ownership evidence was read. **GAP-CAP-1** records the pending owner change; its proofs live in
that change's own modules and are not duplicated by the harness.

## Status distinctions (input to the minimum-correction step)

What a reader must look at to tell these apart. `engineering_capture` and the reviewer rows'
`review_decision` are the two additive, read-only projections this work added; both derive
from saved fields and the digest-validated receipt, grant nothing, and appear identically in
the full and compact views.

| Operator question | Where the answer is in `ao_room_status` |
| --- | --- |
| running | `requests[].state` `submitted`/`running`; `ao_room_sync.wait.settled` false |
| waiting for the provider (quota or provider hold) | `requests[].state` `uncertain` + `ao_turn_state` `failed`; `engineering_capture.status` `held` with `semantic_kind` once audited |
| worker returned, awaiting Fable's judgment | not a room state: `ao_room_progress.plan.steps[].execution.latest` `returned` with `task_updated_after_latest_result` false; `engineering_capture.status` stays `pending` while the turn runs |
| invalid report | `engineering_capture.status` `capture_failed` with the request id and its `engineering_error`; `ao_room_verify` refuses with a verification-worded message naming both |
| verification failure | `latest_verification.state` `failed`; `checkpoint` null |
| review rejection | the reviewer row's `review_decision` (`rejected`; `approved` with `accepted` false is approved-but-not-accepted; `invalid`/`unavailable` when the saved verdict or its identities do not validate), derived read-only from the digest-validated receipt |
| accepted | `latest_acceptance` |
| installed | not representable (see room-level states) |

## Proof conventions

- Every harness scenario runs in a fresh temporary home and repository, through
  `Service.ao_room_*` or `project_room_mcp.handle`, with `FakeAO`/`NativeFake` and synthetic
  native rows; no account, network, AO process or model is used.
- A scenario that demonstrates an open gap says so in its docstring and in `OPEN_GAPS`; it
  pins the dead end and never claims the contract is met. No gap is hidden behind a skip.
- Expected values come from this map and the sanitized records, not from reading the
  implementation's output back into the assertion.
