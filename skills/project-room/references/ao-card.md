# AO operator card

Work a normal AO room (`workflow: "fable_engineering"`) phase by phase from this card.
Each phase names the tools and the rules that always apply. Open the linked
section of [the full AO guide](ao.md) only when a phase's **Stop** line matches,
or for a situation this card does not cover. When in doubt, read the full section;
this card never overrides it.

Rules for every phase:
- Astra owns product intent, spec revisions, finding dispositions and independent
  acceptance. Fable owns engineering interpretation, delegation and the
  engineering verdict. In a normal room, Astra does not implement; the operator runs
  probes, gates and checks that Fable requests.
- Use a stable, new `request_id` per logical send. An identical repeated call
  reads the saved record. Never resend under a new ID after `uncertain`,
  `failed`, `interrupted`, `cancelled`, `recovered` or a lost acknowledgement.
- While an owned turn is submitted or running, call `ao_room_sync` with
  `wait_seconds=45`, and give a brief update between waits. A `timeout` wait
  reason is not a stall. `settled` is true only when every owned request is
  terminal. Sync each turn to terminal before another send.
- Report usage as the attributable native subtotal from status, with delegates
  separate. Unknown is never zero, and it is not quota or billing.
- Keep room IDs, native IDs, paths and receipts private.

## 0. Session start or resume

`ao_release_check` once, before the first new model dispatch. Continue on any
outcome; only `up_to_date` counts as current. Then `ao_room_list` (plus legacy
`room_list` if it is listed), `ao_room_status`, and `ao_room_sync` for active work.
Resume the recorded backend and state before anything new.
**Stop:** outcome other than `up_to_date` → [stable-release check](ao.md#stable-release-check).
Missing setup, URL or config → [setup and resume](ao.md#setup-and-resume).
Provider limit, truncated output or a paused room → [efficient continuation](../../../docs/guides/efficient-continuation.md).

## 1. Open, prepare, bind

`ao_room_open` with the project path, a stable feature name, the AO project ID,
the user's existing `authorization`, and `delegate_provider` from the recorded
setup (`none` only when explicitly intended). Install the scoped postCreate hook,
create the engineer, and restore the prior project configuration. Create the
reviewer outside that hook window. `ao_room_prepare` is idempotent for the exact
worktree. Bind the engineer (the room's selector, `max`) and a separate Codex
reviewer (the requested Astra model, `max`) with `ao_room_bind`.
**Stop:** any preparation or routing refusal (`routing-error-*.json`) →
[native delegation routing](ao.md#native-delegation-routing). Never launch a paid
turn after a failed preparation.

## 2. Spec and exact-spec review

Ground the spec in current behavior, and prepare evidence so Fable doesn't
repeat discovery. `ao_room_spec_put` takes the exact content, a positive
`revision`, explicit Astra `approval`, and nonempty argv `gates`; it is immutable. Gates are
trusted project commands: never put model calls or publication in them.
For a source-qualified family, run `ao_room_engineer_source_register` before
the first engineer response. Then `ao_room_send` role `engineer`, purpose
`spec_review`, and sync to terminal. `BLOCKER:` findings prevent agreement.
Dispose of each finding with evidence; a behavior change needs a newer revision.
There are 3 spec-review attempts across revisions, with no automatic renewal.
**Stop:** attempts exhausted → bring the product tradeoff to the user; an
extension needs [one-charter review extension](../../../docs/guides/one-charter-review-extension.md).
Prose around the final JSON → [formatting recovery](ao.md#completed-response-formatting-recovery).

## 3. Handoff and implementation

`ao_room_handoff` with the bound engineer worktree. Send purpose `implementation`
once for that handoff. The message is only the actual instruction; never repeat
the spec, policy, gates or summaries. Sync to terminal. The final report needs
`outcome`, `implementation_complete`, `changes`, `tests_reported`,
`review_findings`, `remaining_gaps`, `backlog`, `routing_log` and the exact
spec, baseline and SHA fields. Read full receipts, not previews. In status,
inspect `review_followups`, `enhancement_proposals` and `operator_requests`.
Pending operator requests block acceptance: do only the authorized execution,
return the evidence, then get Fable's next judgment.
**Stop:** `scope_change` → a revised, agreed spec first. Enhancement proposals →
[enhancement issue handoff](../../../docs/guides/workflow.md#enhancement-issue-handoff).
Delegate ledger or routing refusal → [usage, context and delegates](ao.md#usage-context-and-delegates).

## 4. Correction

For a confirmed completed turn only, send purpose `correction`. The message is
exactly the new instruction, or `Continue.` when there is nothing else. The
previous receipt is preserved. Commit before the final engineering response is
captured; later changes to HEAD, index or files need another correction.
**Stop:** unknown delivery, or a mis-attributed partial report → [normal workflow step 7](ao.md#normal-fable-workflow).

## 5. Verify

`ao_room_verify` on the unchanged captured candidate. The default per-gate
timeout is 120 s (maximum 7200); pass a larger `timeout_seconds` when the known
suite needs it. Drift or a failed gate means no checkpoint. Committing afterwards
needs fresh verification.
**Stop:** a verifier crash left a running record → [verify and accept](ao.md#verify-and-accept).
Do not erase it or relaunch.

## 6. Independent acceptance

`ao_room_send` role `reviewer`, purpose `acceptance_review`, with a focused
request; sync to terminal. Read Fable's routing report against the actual ledger
facts. On `rejected`, investigate, correct, reverify and send a new review.
`ao_room_accept` with that review's `request_id`. There are 3
acceptance-review requests per room.
**Stop:** allowance exhausted → [acceptance-review continuation](../../../docs/guides/acceptance-review-continuation.md),
which needs the user's actual authorization. An unused reviewer fails to resume →
[reviewer recovery](ao.md#recovery-of-a-reviewer-that-has-never-been-used).

## 7. Integrate and report

Acceptance does not merge or publish. Continue authorized integration with normal
Git tools, and check that the integrated bytes are the reviewed bytes. The final
update keeps these separate: agreed spec, implemented code, passed gates, Astra
acceptance, and remaining blockers.

## Situations outside the card

- Engineer model change → [engineering model selection and transition](ao.md#engineering-model-selection-and-transition).
- Provider amendment → [provider transition](../../../docs/operations/provider-transition.md).
- `astra_led` exception or pilot rooms → [explicit Astra exception](ao.md#explicit-astra-exception-and-historical-pilot-rooms).
- Uncertain, failed or recovered turns → the second paragraph of
  [explicit Astra exception](ao.md#explicit-astra-exception-and-historical-pilot-rooms).
  Diagnose them without resending.
- Quota stop, semantic hold, or an instruction staged while paused →
  [efficient continuation](../../../docs/guides/efficient-continuation.md).
