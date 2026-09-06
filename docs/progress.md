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

Activity categories normalize known tools: `read` (Read, Glob, Grep, LS, NotebookRead), `edit` (Edit, Write, MultiEdit, NotebookEdit), `shell` (Bash and its output/kill tools), `delegate` (Agent, Task), `local-model` (`mcp__qwen-local__*`), `skill` (Skill), `output` (StructuredOutput), `other` (anything else), and `message` for records without a tool block. A shell category is never labelled "tests passed"; only executed gates prove that.

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

## How Astra should use it

During a bounded `room_job_status` wait, summarize `phase`, `phase_detail`, `elapsed_seconds`, `activity` (category, source, time), pending, background, and completed delegates with any attributed child's model and `turn_ended`, and `deadline.remaining_seconds` with its scope. Say "remaining until the pinned timeout", never an estimated finish. A `background` delegate is launched, not finished: report its child's last observed activity instead of claiming completion. If `activity` is null, report the reason code as unavailable evidence rather than concluding that work stalled or finished. Treat `expired: true` as a signal to keep waiting for the worker's own terminal outcome, not as permission to cancel, resubmit, or edit state.

No automatic wakeup or monitor is added: the controller still cannot wake an idle Astra conversation. Progress is available whenever Astra reads the job, including after reconnecting.
