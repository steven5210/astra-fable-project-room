# Job progress

Every job returned by `room_status` (in `jobs[]`) and `room_job_status` carries an additive, read-only `progress` object. It answers four questions from an ordinary bounded status wait: which lifecycle stage the job is in, how long it has run, what kind of activity was last observed and when, and how many seconds remain before the controller's pinned timeout ends the current stage. Existing job fields, results, gate evidence, delivery verdicts, and approvals are unchanged.

Progress is advisory evidence. Reading it never spawns a process, calls a model or Qwen, edits state, cancels, replays, or extends a job. A tool start is an observation, not proof that the tool is still doing useful work; a missing observation is reported as unavailable, never inferred as a stall or as completion.

## Illustrative output

This synthetic example shows an implementation attempt whose Fable session is waiting on a delegate:

```json
{
  "schema_version": 1,
  "observed_at": "2026-09-05T18:42:10Z",
  "phase": "model",
  "phase_detail": "delegate_pending",
  "outcome": "running",
  "attempt": 1,
  "elapsed_seconds": 1290,
  "elapsed_basis": "job_started_at",
  "deadline": {
    "scope": "model_invocation",
    "basis": "pinned_handoff_model_timeout",
    "started_at": "2026-09-05T18:20:41Z",
    "timeout_seconds": 3600,
    "deadline_at": "2026-09-05T19:20:41Z",
    "remaining_seconds": 2311,
    "expired": false,
    "meaning": "timeout_countdown_not_eta"
  },
  "deadline_unavailable_reason": null,
  "gate": null,
  "activity": {"last_observed_at": "2026-09-05T18:41:58Z", "category": "shell", "source": "child_session", "event": "tool_start"},
  "activity_unavailable_reason": null,
  "delegates": {
    "requested": 2, "pending": 0, "background": 1, "completed": 1, "observed_children": 1, "attributed_children": 1,
    "items": [
      {"handle": "b81d0e5c9a22", "requested_role": "opus-reviewer", "state": "background",
       "requested_at": "2026-09-05T18:33:12Z", "result_at": "2026-09-05T18:33:13Z",
       "child": {"observed_model": "claude-opus-5", "last_observed_at": "2026-09-05T18:41:58Z", "last_category": "shell", "turn_ended": false}},
      {"handle": "3f9c1a7b2e04", "requested_role": "sonnet-worker", "state": "completed",
       "requested_at": "2026-09-05T18:25:03Z", "result_at": "2026-09-05T18:31:40Z", "child": null}
    ],
    "truncated": false
  },
  "limitations": []
}
```

Astra can turn this into one honest sentence: "Fable launched a delegate in the background (an opus-reviewer was requested; the attributed child session reports model claude-opus-5 and has not ended its turn). The latest observed activity is a shell operation in that delegate session at 18:41:58Z. 21 minutes have elapsed and 38 minutes remain until the pinned model timeout; that is a deadline, not an estimate of completion."

## Schema, version 1

All timestamps are second-precision UTC text (`YYYY-MM-DDTHH:MM:SSZ`). Every enumerated field uses the fixed vocabulary below; no model prose, tool names, inputs, paths, or raw identifiers are ever emitted.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | Always `1` for this schema. |
| `observed_at` | timestamp | When the controller computed this object. |
| `phase` | `queued`, `starting`, `model`, `gate`, `finalizing`, `awaiting_review`, `terminal`, `unknown` | Lifecycle stage from owned state (see below). |
| `phase_detail` | `null`, `delegate_pending` | `delegate_pending` when the model session has a delegate request without a result, or a background delegate whose attributed child has not ended its turn. |
| `outcome` | registry job status | `queued`, `running`, `succeeded`, `failed`, `uncertain`, `cancelled`, `not_sent`. Never changed by observation. |
| `attempt` | integer or null | Implementation attempt number bound to this job; null when unknown or not applicable. |
| `elapsed_seconds` | integer or null | Whole seconds; see `elapsed_basis`. |
| `elapsed_basis` | `job_created_at`, `job_started_at`, `frozen_at_finish`, `unavailable` | Queued jobs count from creation; running jobs from the worker start; terminal jobs are frozen at their saved finish. |
| `deadline` | object or null | Countdown to the pinned timeout of the active stage. Fields: `scope` (`model_invocation`, `gate`), `basis` (`pinned_review_timeout`, `pinned_handoff_model_timeout`, `pinned_handoff_gate_timeout`), `started_at`, `timeout_seconds`, `deadline_at`, `remaining_seconds` (clamped at zero), `expired`, `meaning` (always `timeout_countdown_not_eta`). |
| `deadline_unavailable_reason` | code or null | Why `deadline` is null: `queued`, `starting`, `finalizing`, `awaiting_product_review`, `terminal`, `stage_transition` (the previous stage ended and the next start is not yet saved), `gate_start_unavailable_legacy_worker`, `state_unreadable`, `pinned_timeout_unavailable`, `clock_anomaly`, `unknown_job_kind`, `progress_unavailable`. |
| `gate` | object or null | During controller gates: `index` (1-based) and `count` (total gates, or null if unknown). |
| `activity` | object or null | Last observed session record: `last_observed_at`, `category`, `source` (`parent_session`, `child_session`), `event` (`tool_start`, `tool_result`, `assistant_message`, `user_message`). |
| `activity_unavailable_reason` | code or null | Why `activity` is null: `queued`, `starting`, `finalizing`, `gate_phase`, `awaiting_product_review`, `terminal`, `transcript_missing`, `transcript_unreadable`, `session_ambiguous`, `path_rejected`, `session_mismatch`, `cwd_mismatch`, `metadata_unsupported`, `parent_cwd_witness_missing`, `projects_scan_limited`, `projects_scan_failed`, `no_records_in_attempt_window`, `state_unreadable`, `unknown_job_kind`, `progress_unavailable`. |
| `delegates` | object or null | Present only while a model session was observed. Counts `requested`, `pending`, `background`, `completed`, `observed_children` (subagent files with records for this session inside the attempt window), `attributed_children`; `items` (at most 8: pending first, then background, then completed, newest first within each state) and `truncated`. |
| `heartbeat` | object | Worker liveness observation: `available`, `reported_at`, optional `attempt`, `meaning: worker_liveness_only`, and `unavailable_reason`. |
| `recent_activity` | object | At most five safe transitions: `items`, `truncated`, `window_incomplete`, `unavailable_reason`. See [details](status-followups.md). |
| `limitations` | sorted list of codes | Degradations that applied; empty when none. |

Activity categories normalize known tools: `read` (Read, Glob, Grep, LS, NotebookRead), `edit` (Edit, Write, MultiEdit, NotebookEdit), `shell` (Bash and its output/kill tools), `delegate` (Agent, Task), `local-model` (`mcp__qwen-local__*`), `remote-model` (`mcp__deepseek__*`), `skill` (Skill), `output` (StructuredOutput), `other` (anything else), and `message` for records without a tool block. A shell category is never labelled "tests passed"; only executed gates prove that.

Each `delegates.items[]` entry has `handle` (a 12-hex digest of the tool-use ID, never the raw ID), `requested_role` (`sonnet-worker`, `opus-reviewer`, `other`, `unknown`, read only from the allowlisted `subagent_type` input key), `state`, `requested_at`, `result_at`, and `child`. States: `pending` means the parent has no result yet and is waiting synchronously; `background` means the parent received only a launch acknowledgement (Claude Code runs delegates in the background by default, recorded as an `async_launched` result status or a `run_in_background` request), so completion is not known; `completed` means a synchronous result was received. `result_at` is the time of that result or acknowledgement. `child` is null unless a subagent file is provably attributable; otherwise it holds `observed_model` (a validated Claude model ID, `unknown` when the recorded value is unrecognized, or null when absent), `last_observed_at`, `last_category`, and `turn_ended` (true when the child's last record is an assistant message that stopped with `end_turn`, false when it is still mid-turn, null when the stop reason is unavailable). A requested role is never presented as an observed model, and a requested delegate is not certification that any delegate ran.

Limitation codes: `attempt_binding_inferred`, `tail_window_truncated`, `record_window_truncated`, `malformed_records_skipped`, `unsupported_records_skipped`, `inline_sidechain_records_ignored`, `future_records_ignored`, `records_without_valid_timestamp_ignored`, `predicted_path_rejected`, `unrelated_candidate_rejected`, `projects_scan_failed`, `transcript_unreadable`, `children_dir_rejected`, `child_path_rejected`, `child_unreadable`, `children_truncated`, `child_cwd_witness_missing`, `child_attribution_ambiguous`, `child_attribution_unavailable`, `observed_model_unrecognized`, `clock_anomaly`, `review_state_unreadable`, `handoff_state_unreadable`, `handoff_manifest_unverified`, `handoff_config_unverified`, `progress_unavailable`.

## Where each value comes from

- **Phase** is sourced only from owned state, in this order of authority: the registry job row, then (while running) the room's review turn row or the handoff `state.json` bound to this job, then nothing else. `queued` and `starting` cover the time before the owned attempt is visible; `model` covers the Claude invocation, including waiting on delegates; `gate` covers controller gates; `finalizing` is the short window after the operation recorded its outcome but before the worker saved the registry result; `awaiting_review` is a succeeded implementation whose saved outcome is awaiting Astra's product review; `terminal` covers every other saved outcome; `unknown` means the owned state could not be read.
- **Elapsed** uses the registry job's own timestamps. Stage-relative time appears only inside `deadline`.
- **Deadline** uses the actual invocation start and the timeout pinned when the room or handoff was created: `config_snapshot` for reviews, the immutable `implementation-config.json` for implementation model and gate stages. The mutable global `config.json` is never consulted for a running job, so changing it after launch does not move a countdown. Once expired, `remaining_seconds` stays at zero and `expired` is true, but the observation changes nothing; the owning worker alone ends the child at its real timeout. Between stages (after the model child or a gate exits and before the next stage start is saved) the reason is `stage_transition`. A saved start later than the observation clock yields no countdown and `clock_anomaly`. Delegates never receive an invented separate deadline.
- **Activity and delegates** come from a bounded scan of the exact owned session transcript for the current attempt only: the predicted path plus any `projects/*/<session-uuid>.jsonl` match, requiring exactly one candidate, containment under the configured projects directory, no symlink escape, absolute paths only, the expected session ID, the expected working directory whenever a record carries one, and timestamps inside the current attempt window. A pinned explicit template is checked alongside the configured projects directory; duplicate exact UUIDs are refused. A parent observation requires at least one accepted cwd witness. Valid absolute directories inside the expected worktree are allowed. Records from earlier attempts on the same resumed session and future-dated records are ignored. Subagent files are read only from `<transcript directory>/<session-uuid>/subagents/agent-<id>.jsonl` and only when modified since the attempt began. A child is attributed to a specific request only through one unique stable link: the `agentId` recorded with the parent's launch result for that request, or (when no such link exists) a child `sourceToolAssistantUUID` naming a parent record that holds exactly one delegate block. Attribution also requires that no other child claims the request, that the tool ID was never reused, that the session ID matches, that records explicitly have `isSidechain: true`, that `agentId` is present and matches the file name, and that at least one child record carries the expected working directory. Unproven children are counted in `observed_children` and reported through limitation codes, while the parent observation is retained.

## Limits and legacy workers

Discovery examines at most 256 entries in each configured projects or subagent directory. Reaching the projects limit refuses ambiguous selection; reaching the child limit marks `children_discovery_limited`. Files are opened through bound directory descriptors with no symlinks followed below the configured root, so a replacement between selection and reading cannot redirect the read.

Bounds: the parent transcript scan reads at most the last 2 MiB and 5,000 records; each of at most 16 subagent files is read to its last 512 KiB and 2,000 records; at most 8 delegate items are emitted. A partial final line is normal while a session is being written and does not invalidate earlier complete records. Corrupt, oversized, missing, unsupported, or ambiguous metadata degrades to null values with a reason or limitation code; it never raises out of the status call and never changes delivery state.

Workers started before this version do not persist `owner_job_id` or `active_stage` in `state.json`. Their attempt is bound by time ordering (`attempt_binding_inferred`), a running model stage still gets its deadline from the saved attempt start, and a running gate reports `deadline: null` with `gate_start_unavailable_legacy_worker` rather than an approximate countdown. New workers save `owner_job_id` (the registry job ID, or `cli` for a manual `implementation.py run`) and each stage start before the child process runs, and clear the stage when the child exits; the audit fields recorded after each gate finishes are unchanged. A manual CLI attempt is never adopted by a registry job.

Terminal progress is frozen from the job's saved row. A succeeded implementation job therefore keeps `phase: awaiting_review` even after Astra later records acceptance or requests a correction, because those are separate handoff events; read `room_implementation_status` or the later job for the handoff's current state. A queued correction shows `queued`/`starting` with no deadline, gate, or delegates until its own attempt is visible.

## The bounded plan fold

`plan.counts` covers every task in the engineer's list, and `plan.groups` folds every task by the area code at the start of its subject — `EXIT-12:` and `EXIT-13:` are one `EXIT` group, a subject with no leading code falls into `other`, and groups are sorted by key with at most thirty-one named keys before the rest fold into `other` — matching and grouping use the full code, and only the displayed key is bounded (a long code truncates with a short digest suffix that keeps distinct codes apart in practice; the displayed key is a label, not a promised unique identifier, and consumers that need identity use the full subject code). Only the step list is bounded: when the list holds more than 64 tasks, `plan.steps` keeps every in-progress unit first, then the newest pending and newest completed, and `total`, `shown` and the per-status `omitted` counts report what was cut — AO's own plan panel is unaffected either way. A shown step's `stale_in_progress` flag marks an in-progress unit whose unit code opens no launch in the turn and whose last status update is over forty-five minutes old on the turn's own clock — it requires a found turn anchor, an unknown update time is never stale, and it is a hygiene signal for list keeping, not evidence that work stopped.

## Provenance and stage evidence

`ao_room_progress` also carries two additive, read-only objects, `provenance` and `stages`. Both are computed from controller state and the room's own saved records before the transcript is opened, so a view whose native source is unavailable still carries them; only the transcript-derived fields of `provenance` are filled in after a successful read. Every existing field keeps its meaning, `rate_limits` is unchanged, and neither object adds to the `deliverables` notes budget. Both use closed vocabularies; neither carries a path, a session ID, transcript content or a credential; a stage `error` status carries no detail; the only free text in either object is `provenance.latest_engineer_request.semantic_status.reason`, the controller's saved semantic-status reason for that request (for an unclassified failure, the saved error summary); string values are redacted like the rest of the view and bounded (identifiers, kinds, states and timestamps to 64 characters, that semantic-status reason to 200); digests are shown only in their exact 64-character lowercase hex form; and each object's `truncated` flag is true when a bound removed characters.

`provenance` says who wrote the plan and `last_text` and how they relate to the selected request (the `plan.*` rows below are fields of `provenance.plan`, not of the top-level `plan`):

| Field | Values | Meaning |
| --- | --- | --- |
| `request_selection` | `latest`, `explicit` | `explicit` when the call named a `request_id`; otherwise the newest request was selected. |
| `selected_request_role` | `engineer`, `reviewer`, `unknown` | The one bound role whose session sent the selected request; `unknown` when no binding, or more than one, matches. |
| `selected_request_is_engineer_turn` | boolean | True only for a request sent from the bound engineer session. |
| `selected_request_created_at` | number or null | The selected request's saved creation time, in epoch seconds. |
| `latest_engineer_request` | object or null | The newest request sent from the bound engineer session: `request_id`, `purpose`, `state` (its saved state), `created_at` and `semantic_status` (`kind`, `hold`, `reason`, or null). |
| `selected_is_latest_engineer_request` | boolean | Whether the selected request is that request. |
| `plan.author` | `engineer` | Only the engineer writes the plan. |
| `plan.source` | `registered_engineer_native_source`, `unavailable` | Whether the registered engineer native source was read. |
| `plan.updated_at` | timestamp text or null | The transcript time of the last plan change, the top-level `plan.updated_at` bounded to 64 characters; null when no task-tool activity was observed. |
| `plan.scope` | `turn`, `window`, null | `turn` when that last change lies inside the selected request's own anchored turn, `window` when it lies elsewhere in the bounded window, null when no task-tool activity was observed. |
| `plan.refreshable_by_selected_request` | boolean | True only while the selected request is an engineer turn whose saved state is `submitted` or `running`, the only kind of turn that can change the plan; an uncertain delivery is not counted as live. |
| `plan.activity` | `observed`, `unknown` | `observed` when task-tool activity was found; `unknown` when the source was unavailable or showed none. |
| `turn_anchor_reason` | null, `selected_request_not_engineer_turn`, `anchor_not_found`, `source_unavailable` | null when the selected request's turn anchor was found; otherwise why it was not: the request is not an engineer turn, the source was read but no delivered row matched, or the source was unavailable. |
| `last_text` | object | `author` is `engineer`; `scope` is `turn` when the shown `last_text` came from the anchored turn, `window` when it is the window's tail, and null when no text is shown. |
| `meaning` | fixed sentence | The plan and last text are the engineer's own, and the controller never refreshes, infers or rewrites them. |
| `truncated` | boolean | A bound shortened a field. |

`unknown` and null mean that a fact is not recorded or could not be read, never a default guess. The view never compares `plan.updated_at` with any request timestamp: whether the plan is older or newer than the selected request is neither computed nor implied, and the plan in a view of a reviewer request still describes the engineer's work.

`stages` restates the saved engineering, verification and acceptance records, each checked only against its recorded digest:

- `engineering` is the newest implementation or correction request, the one the controller's acceptance checks use. `status` is `captured` when its saved engineering record matches its recorded digest and names the request's recorded candidate and a report digest; `error` when a named record is missing, unreadable, oversized, fails its digest or is inconsistent, or when the request recorded a capture failure; and `missing` when nothing is captured yet (no such request, a turn still running, or one that ended before capture). An earlier capture is never substituted. It also carries `request_id`; `candidate_sha256` and `report_sha256`, null unless captured; `selected_request_has_record`, true when the selected request itself has a digest-valid record; and `digest_valid`, null when no record is named.
- `verification` is the newest saved verification attempt: `missing` (none), `pending` (recorded as running), `passed` or `failed` (its checkpoint is readable and consistent with the attempt and, for a pass, matches the recorded checkpoint digest), or `error`. `recorded_status` is the attempt's saved state. `request_id` is always null, because the controller records no request for an attempt. `candidate_sha256`, `spec_sha256`, `spec_record_sha256` and `gates` (the number of gate results) come from the checkpoint; a `pending` attempt has no checkpoint yet and shows only its recorded `candidate_sha256`. `identity_match` follows the same three-way rule as `acceptance`: it is `mismatched` when any known comparison differs (the checkpoint's candidate against the captured engineering candidate, its spec record against the current spec record, and its spec digest against the current spec digest), and `matches` only when all three are known and equal. A newer spec revision therefore keeps a `passed` or `failed` status and the checkpoint's recorded spec identities but reports `mismatched`, and a `pending` attempt never reports `matches`. `digest_valid` is true or false for a passed checkpoint and null otherwise, since a failed checkpoint has no recorded digest.
- `acceptance` is `approved` when the latest recorded acceptance belongs to the newest acceptance review. Otherwise it follows that review: `pending` while it is submitted or running, or once it completed with an approving verdict that is not yet recorded as an acceptance; `rejected` for a rejecting verdict; `uncertain` for an uncertain delivery; and `error` when the verdict fails the controller's receipt and format checks or the review ended in any other state. It is `missing` when there is neither a review nor an acceptance. `recorded_status` is the review's saved state. `request_id`, `candidate_sha256` and `spec_sha256` are the reviewed identities, and `spec_revision` is given only when the reviewed spec record names the same spec digest. `identity_match` is `mismatched` when any known comparison differs (the candidate against the captured engineering candidate, the reviewed spec record against the current spec record, and the spec digest against the current spec digest), and `matches` only when all three are known and equal. There is no `digest_valid`: a recorded acceptance lives in controller state, and a review verdict is read through the controller's receipt-digest check, whose failure is `error`.
- `identity_match` is `unknown` when no known comparison differs but at least one side is unknown, and always for a `missing` or `error` stage, whose identities are null.
- `currentness` is always `{"checked": false, "worktree": "unknown", "basis": "saved_identities_only"}`. The view never reads the live worktree, so `matches` means that the saved identities agree, not that the worktree still holds that candidate.
- `meaning` is a fixed sentence, and `truncated` is true when a bound shortened a field.

Nothing in either object is inferred, auto-completed or rewritten, and reading them changes no state, releases no hold and replaces no receipt, verification or acceptance. What improves: a view of a reviewer or operator turn now says explicitly that its plan and last text are the engineer's, whether the selected request can still refresh them, and which saved evidence exists for each stage with its identities, so a reader no longer infers this from timestamps. What does not change: the plan is still only as current as the engineer's last list update, nothing wakes the engineer or refreshes the list, stage statuses are saved records rather than re-run gates or a fresh review, and `ao_room_verify` and `ao_room_accept` remain the checks that decide.

## The one-time plan instruction

The one-time `progress_plan_v3` workflow part, delivered once to each retained engineer session, consolidates and supersedes the list-keeping rules of `progress_plan_v1` and `progress_plan_v2`. Their frozen text and pinned digests remain valid history, but no session, old or new, is ever sent them again. A task is one bounded work unit, never a phase, a spec section or an umbrella. The instruction keeps its distinct evidence levels apart (result received, result inspected, changes applied, worker-reported tests, formal candidate verification and independent acceptance). A received handback is never by itself a completed unit: a unit is marked completed only once that evidence actually exists. A unit waiting for an operator, a decision, a quota hold or another unit is waiting, not implementing, and stays pending. A superseded unit keeps its lineage, and lineage keys, dispositions and closure counts instead follow the `lifecycle_closure_v1` default, which this instruction references rather than duplicates. No instruction, operator or controller can refresh the list while the engineer is blocked, idle or waiting, so a stale row after a turn ends is a disclosed reporting limit, not evidence of activity. When two workers run at once, the instruction frames pairing units of similar expected size as a slot-utilisation preference only, not a bookkeeping rule: it never predicts sizes or serializes work to keep rows aligned, and it never overrides the truthful status rules above. It is delivered text only: it grants no scope, execution permission, recovery or review allowance, changes no model, guard, budget or authority, and the read-only views in this document are unchanged; the final JSON report keeps its existing format and remains the engineering verdict.

Saved status reports this delivery's progress through `successor_delivery`, one of four values: `verified_delivered` (a completed/settled_failure engineer turn's verified receipt shows the part was actually carried), `undelivered` (that receipt-authenticated chain is intact but the part is not yet present), `unavailable_integrity` (a directory was given but its own receipt chain could not be authenticated — a missing or corrupt receipt, or a tampered carried record — so a broken chain is never read as a verified absence), or `unverified` (status was read without a directory; metadata-only, never satisfied). Beside those four, `delivered_history` is one of two values: `receipt_verified` when a directory was given and its own receipt chain authenticated cleanly (the `verified_delivered`/`undelivered` cases), or `metadata_only` when no directory was given at all or that chain's own authentication failed (the `unverified`/`unavailable_integrity` cases). See [Progress-plan successor delivery](../guides/efficient-continuation.md#progress-plan-successor-delivery-ao-rooms) for the full accounting, including the paired `superseded`/`satisfied` fields.

## Rate limits

The top-level `rate_limits` block in `ao_room_progress`, and the small `rate_limits` object in the compact `ao_room_status` view, fold the provider's own rate-limit readings for the viewed request's session. AO's bridge stores each `rate_limit_event` in the SQLite table `conversation_provider_events` (`method='account.rateLimits'`); the view reads that database read-only through the path recorded in `native_outcome_source.database` — no writes, no model, no network. When no database is bound, it is unreadable, or it holds no rows for the session, the block reports `unavailable_reason` (`database_unbound`, `database_unreadable`, or `no_readings`) instead of a guess.

`used_percent` is the provider's own figure. A provider `-1` means "not reported" — never a zero — so the block reports `reported: false` with a null percent. On one account in October 2026 the provider sent the five-hour window's percentage only from about 90% used and the seven-day window's only from about 50% used; that is an observation, not a provider guarantee. `resets_at` is the row's time plus the reset seconds — reported whenever the seconds are positive, independently of whether the percent was reported, since a reset value of 0 means no window is running and reports null; `burn` summarizes only the reported readings with the newest row's window label in the last 600 seconds, and `minutes_remaining_at_current_rate` is arithmetic over those readings, not a forecast of the provider's policy.

The block is advisory only: it grants nothing, changes no state, and a stale or absent reading is a reason to report "not reported" or the unavailability reason, not to infer headroom.

## Deliverables (lineage and closure)

`ao_room_progress` also carries an additive, read-only `deliverables` object,
folded from the same native task-tool rows as `plan` above; it performs no state
mutation, hold release, receipt or report replacement. Shape, version 1:

```json
{
  "version": 1, "available": true,
  "anchor": {"spec_record_sha256": "...", "spec_revision": 3, "spec_sha256": "...",
             "declared_labels": ["R1", "R2"]},
  "requirements": [{
    "label": "R1", "declared": true, "folded": false,
    "counts": {"pending": 1, "in_progress": 0, "completed": 2, "unknown": 0},
    "superseded": 0, "required_open": 1, "enhancement_open": 0, "blockers": 0,
    "kinds": {"planned": 2, "defect": 1, "proof_gap": 0, "dependency": 0, "enhancement": 0, "invalid": 0},
    "units": [{"id": "...", "label": "...", "status": "pending", "kind": "defect", "kind_raw": null,
               "from": "...", "superseded_by": null, "blocker": null, "stale_in_progress": false}],
    "units_total": 3
  }],
  "unmapped": {"count": 0, "metadata_unavailable": 0, "superseded": 0, "required_open": 0,
               "completed": 0, "blockers": 0, "ids": []},
  "superseded": [], "superseded_total": 0,
  "blockers": [], "blockers_total": 0,
  "conflicts": [{"id": "...", "req": "...", "successor": "...",
                 "reason": "successor_unobserved | superseded_cycle | completed_and_superseded"}],
  "conflicts_total": 1,
  "closure": {"completed": 2, "enhancement_completed": 0, "required_open": 1, "enhancement_open": 0,
              "superseded": 0, "blockers": 0, "conflicts": 1, "stale_in_progress": 0,
              "window": {"created": 3, "completed": 2, "superseded": 0}},
  "limits": {"truncated": false, "notes": []}
}
```

**Lineage source.** Each task's `req`, `kind`, `from`, `blocker`, `superseded_by`
and `reason` come from the native `TaskCreate`/`TaskUpdate` `metadata` (merged key
by key across calls — a `null` value deletes that key, and only string/bool/number
values are kept) or from `key=value` tokens in the task description: one
whitespace-free token per key, except `reason=` which consumes the rest of its
line and so must be last on that line. Metadata wins over a description token for
the same key; the two sources disagreeing is not itself reported, only the merged
(metadata-wins) result. The merged `superseded_by` value is then normalized:
surrounding whitespace and exactly one leading `#` are stripped, and a value of
`None`, `false`, an empty string, or the literal text `false` in any letter case
— or an empty `superseded_by=` description token — is treated as absent,
meaning no supersession and no conflict for that row.

**Kind.** An empty or absent `kind` is `planned`; `defect`, `proof_gap`,
`dependency` and `enhancement` are kept as given; any other value is reported as
`invalid` with the raw value (bounded) in `kind_raw`.

**Unmapped work.** A unit with no usable `req` is unmapped — never inferred from
its label — and an open unmapped unit still counts as required work. Among
unmapped units, `metadata_unavailable` further counts those whose task id this
window never created or updated (seen only through `TaskList`), so their lineage
is unobservable rather than merely missing a `req` token. `unmapped.superseded`,
`.required_open` and `.completed` partition `unmapped.count` by the same
per-row disposition as the per-requirement `counts` — superseded identity
first (below), then open or completed by the row's own status; `unmapped.blockers`
counts the subset of `unmapped.required_open` rows carrying a blocker, exactly
like the per-requirement blockers rule. Unmapped rows are never enhancements,
so there is no `unmapped.enhancement_*` split.

**Superseded and conflicts.** Identity for supersession is compared on the
normalized `superseded_by` id above against the window's tracked task ids,
never on a redacted or bounded display value; `units[].superseded_by`,
`superseded[].successor` and `conflicts[].successor` show that id after
redaction and bounding like every other id (see Redaction and bounds below). A
row is superseded only when its successor is an observed tracked task **and**
the successor chain starting from that row never leads back to the row itself;
this depends only on that, never on the unit's own open/closed status. A
superseded row keeps its own status but is excluded from completed, open and
blocker counts. A self-reference or a longer cycle is not a valid supersession:
the row stays open (or completed, by its own status), is never counted under
`superseded`, and is reported instead as a conflict with reason
`superseded_cycle`. Chains are dispositions resolved along the whole chain, not
a rule applied to one row in isolation: for 1 → 2 → 3, rows 1 and 2 are
superseded and 3 stays open; for 1 → 2 → 3 → 2, row 1 is superseded while rows
2 and 3 are cycle conflicts (2 closes the cycle back to itself, and 3's chain
also returns to 2). One shared predicate feeds both `closure.superseded` and
`closure.window.superseded`, so the two counts never disagree on which rows
qualify.

Each row gets at most one conflict reason, chosen in this precedence:
`successor_unobserved` when `superseded_by` names an id the window never
observed as a tracked task (for example, never created, or later deleted);
`superseded_cycle` for the self-reference/cycle case above; then
`completed_and_superseded` when a completed row also has a currently-tracked,
non-cyclic successor — that row is counted under `superseded`, never under
`completed`.

**Enhancements.** `kind: "enhancement"` paired with a usable `req` is counted
separately (`enhancement_open`/`enhancement_completed`) and never counts toward
required work or blockers.

**Blockers.** An open, non-superseded, non-enhancement unit with a `blocker`
value other than `false` is a blocker; a bare `true` borrows the unit's own
`reason` text as the explanation shown in the `blockers` list.

**Requirement anchor.** `declared_labels` are the requirement labels the room's
immutable spec body declares — a line starting with a label such as `R1` followed
by whitespace and a hyphen, colon, en dash or em dash, then whitespace — read
independently of the transcript and bounded to 32; `declared` marks whether a
requirement entry's label was one of them. An unreadable or malformed spec record
yields the `"spec record unavailable"` note instead of a crash. Requirement
entries are sorted by label and bounded to 32; overflow folds into one
`label: "other"` entry with `folded: true` (a requirement literally named `other`
keeps its own entry with `folded: false`).

**Closure counts.** Every number in `closure` is a measured count, never a
percentage or an invented denominator. The frozen rule "a superseded row is
never counted as completed" applies at every level: each requirement's
`counts` (`pending`, `in_progress`, `completed`, `unknown`) counts only its
non-superseded rows; the requirement's own `superseded` counts its superseded
rows of any status; and `units_total == sum(counts) + superseded` for every
requirement, including the folded `other` entry. The same five identities hold
on every available view:

- (a) `closure.superseded == Σ requirements[].superseded` (the folded `other`
  entry included) `+ unmapped.superseded`.
- (b) `closure.required_open + closure.enhancement_open == Σ requirements[].(counts.pending + counts.in_progress + counts.unknown) + unmapped.required_open`.
- (c) `closure.completed + closure.enhancement_completed == Σ requirements[].counts.completed + unmapped.completed`.
- (d) `closure.blockers == Σ requirements[].blockers + unmapped.blockers`.
- (e) `closure.conflicts == conflicts_total`.

`closure.window.created` counts successful `TaskCreate` calls observed in the
window; `closure.window.completed` counts distinct tasks observed being set to
`completed` through `TaskUpdate`; `closure.window.superseded` applies the same
per-row supersession verdict as `closure.superseded` (successor observed and
tracked, and the chain never cycles back) to tasks tracked at the end of the
window, so the two `superseded` counts never disagree on which rows qualify.

The window's `created` and `completed` counts retain those observed events even
when a task is later deleted or omitted by `TaskList`. Current requirement and
closure totals include only tasks still tracked. For example, creating a task,
completing it, and then removing it leaves `window.created = 1` and
`window.completed = 1`, while current `closure.completed = 0`. That difference
alone does not indicate missing work; compare current totals with current tasks,
and use the window counts to describe activity during the observed window.

`stale_in_progress` reuses exactly the staleness rule the `plan` steps above
already use (over forty-five minutes since the last update on the turn's own
clock, and only once a turn anchor is found).

**Availability.** `available` is false whenever the transcript is unavailable or
no task tool activity was observed; then every count is zero, every list is
empty, and `limits.notes` states why.

**Redaction and bounds.** A single shared `_redact` helper is the one path
redactor used across the view: the plan's step and group labels, `last_text`,
and every string inside `deliverables` all run through it. It rewrites every
absolute POSIX path — a `/`-rooted run, including one that starts with a
second `/` — and every `file://` URL to `<path>`, wherever that path starts: at
the start of the string, after whitespace, or after any non-path character
such as a backtick, a straight or smart quote, `<`, `[`, `{`, `(`, `@`, `|`,
`*`, `=`, `:`, `,` or `;`. A `/` immediately preceded by a word character, `.`
or `~` is not a path start, so relative paths, numeric ratios and `and/or` are
left intact. Any other `scheme://` URL keeps its scheme and authority and the
rest of it is scanned like other text, so `https://example.com/a/b` stays
intact while an absolute path embedded later, for example after `?next=`, is
still redacted; `file://` URLs are always redacted whole. Two gaps are
known and pre-existing (tracked as backlog, not claimed as covered): `~/`
home-relative paths and Windows-style paths are not redacted. This redaction
claim is scoped to the shared redactor's callers and to every string inside
`deliverables`; it is not a claim that every string the whole progress view
emits is redacted — outside `deliverables`, `plan.steps[].status`,
`launches[].subagent_type` and the turn histogram keys are emitted exactly as
observed.

Every id-valued field in `deliverables` — `units[].id`, `units[].superseded_by`,
`superseded[].id` and `.successor`, `blockers[].id`, `conflicts[].id` and
`.successor`, and `unmapped.ids[]` — is redacted and then bounded to 64
characters (`MAX_ID_CHARS`); the former 32-character bound that applied only to
`superseded_by` is now this same 64. The other per-key bounds are `req` 16
characters (shown as `requirements[].label`, `blockers[].req` and
`conflicts[].req`), `label` 200 (`units[].label` and `blockers[].label`),
`from` 64, `blocker` 120, `reason` 200 and `kind_raw` 32. Each requirement's
`units` list, the top-level `superseded`, `blockers` and `conflicts` lists, and
`unmapped.ids` hold at most 16 entries (the true count is in the matching
`_total`/`count` field); `declared_labels` and the requirement list are each
bounded to 32; `limits.notes` holds at most 8 strings.

Every exceeded bound is disclosed in `limits`. A list or label bound sets
`limits.truncated` and adds that bound's own note
(`requirement units bounded to 16`, `requirement labels bounded to 32`,
`unmapped ids bounded to 16`, `superseded list bounded to 16`,
`blockers list bounded to 16`, `conflicts list bounded to 16`,
`declared labels bounded to 32`). A character bound that shortened a value the
view shows also sets `limits.truncated` and adds exactly one note,
`character bounds clipped: <field> <count>, ...`, listing in a fixed order
(`id`, `label`, `req`, `kind_raw`, `from`, `blocker`, `reason`, `superseded_by`)
each per-key field with the number of shown values that were shortened. A
value shown in two places counts in each: a 121-character `blocker` on an open
blocker row counts once in `units[].blocker` and once as the `blockers[].reason`
echo of that text (`blocker 2`); `superseded_by` covers `units[].superseded_by`,
`superseded[].successor` and `conflicts[].successor`; `reason` covers
`superseded[].reason` and the `blockers[].reason` that a bare `true` blocker
borrows; `req` counts a kept requirement entry's label once plus each
`blockers[].req` and `conflicts[].req`. Redaction never counts as clipping:
bounding is measured on the redacted text, so a path rewritten to `<path>` is
not a shortened value. Values the view does not show are not counted — a
row's `reason` when the row is neither superseded nor a bare-`true` blocker,
unit records beyond the 16-unit bound, entries beyond a list bound, and
requirement labels folded into `other` — because the list and label notes
already disclose those. With at most one anchor note, six list/label notes and
this one clip note, the eight-string `limits.notes` budget is never exceeded.
The view's `plan.steps[].label`, `plan.groups[].active[]` and top-level
`launches[].description` are pre-existing outputs outside `deliverables` and
carry no clip accounting.

**Status vocabulary.** Inside `deliverables`, a unit's `status` is one of
`pending`, `in_progress`, `completed` or `unknown`; any other observed value is
shown as `unknown`, listed under the pending bucket, counted as open (never as
completed), and folded into that requirement's `counts.unknown`.
`plan.steps[].status` above is a separate field and is unchanged by this.

**Engineer reports.** The lineage labels, classifications and dispositions this
view folds are the same ones Fable records inside the existing engineering report
string-list fields (`changes`, `remaining_gaps`, `backlog`, `review_findings`,
`routing_log` entries); no report field was added for this. The one-time
`lifecycle_closure_v1` workflow part (see
[lifecycle-closure delivery](../guides/efficient-continuation.md#lifecycle-closure-delivery-ao-rooms))
is what asks Fable to record this lineage going forward; the `deliverables`
projection itself is always computed read-only from whatever task metadata and
description tokens are actually present, independently of whether that part has
been delivered yet.

## How Astra should use it

During a bounded `room_job_status` wait, summarize `phase`, `phase_detail`, `elapsed_seconds`, `activity` (category, source, time), pending, background, and completed delegates with any attributed child's model and `turn_ended`, and `deadline.remaining_seconds` with its scope. Say "remaining until the pinned timeout", never an estimated finish. A `background` delegate is launched, not finished: report its child's last observed activity instead of claiming completion. If `activity` is null, report the reason code as unavailable evidence rather than concluding that work stalled or finished. Treat `expired: true` as a signal to keep waiting for the worker's own terminal outcome, not as permission to cancel, resubmit, or edit state.

No automatic wakeup or monitor is added: the controller still cannot wake an idle Astra conversation. Progress is available whenever Astra reads the job, including after reconnecting.
