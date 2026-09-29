# Current handoff state, worker heartbeat and recent activity

These answer different questions. `room_implementation_status` reads the handoff's current saved decision. A job's `progress.heartbeat` says when its owning worker last wrote a liveness record. `progress.recent_activity` shows a bounded window of observed activity categories. None freshly certifies the candidate or estimates completion.

## Current handoff status

Call `room_implementation_status(room_id, handoff_id)` through MCP, or the equivalent CLI:

```sh
python3 project_room.py call room_implementation_status --args '{"room_id":"<saved-room-id>","handoff_id":"<saved-handoff-id>"}'
```

Use IDs already returned by the room; the caller cannot pass a filesystem path. The room/handoff association and immutable saved manifest/input hashes are checked. A historical handoff remains readable after a newer spec is registered. This does not permit running or accepting a stale spec.

The response is an allowlisted object:

| Field | Meaning |
| --- | --- |
| `schema_version`, `meaning` | `1`, `current_saved_handoff_record`. |
| `room_id`, `handoff_id` | Exact stored association. |
| `spec_revision`, `spec_sha256`, `baseline_commit` | Identity pinned by that handoff. |
| `phase`, `attempt` | Saved phase and positive attempt number, or null when unknown. Phases: prepared, preparing, running_model, running_gates, blocked, awaiting_astra_review, accepted, changes_required, correction_pending, scope_change, recovery_prepared; unsupported values become unknown. |
| `astra_accepted`, `acceptance` | Saved boolean and compact review identity: reviewer, accepted, recorded_at, spec_revision, spec_sha256, candidate_sha256. Missing review is null; no review prose is included. A correction can retain the prior review while its current acceptance flag is false. |
| `gates_passed`, `gates`, `gates_truncated` | Saved verdict and at most 64 entries containing index, return_code, started_at, finished_at, stdout_sha256 and stderr_sha256. No commands or output bodies. |
| `candidate` | Saved head, sha256, and path_count; null when absent. |
| `lineage` | active_recovery or null, the latest 16 history entries, and truncated. Entries contain validated recovery/job identifiers, predecessor_attempt, known kind/status/launch_state, and timestamps. Missing or unsupported values become null; arbitrary recovery reasons and private evidence are excluded. |

Example: job A finishes with `awaiting_review`. Astra accepts its candidate. The handoff now reads `accepted`; job A still shows its frozen original result. A correction request instead makes the current handoff `correction_pending`; a queued job B acquires no activity or heartbeat from A.

Reads do not run gates, fingerprint the candidate, open transcripts, launch a model, refresh a worker lease, reconcile recovery or write state. `accepted` describes a recorded decision, not a fresh check that candidate files remain unchanged. Integrity failures return a controlled error without private loader details.

## Worker heartbeat

New supervising workers write `heartbeat.json` in their existing private job directory at start, approximately every five monotonic seconds, and once at the end. The loop already supervising the child makes these writes; there is no new thread, daemon, monitor or automation. Each replacement is atomic, at most 4,096 bytes and mode 0600, reached through owned directory descriptors. Writes cannot follow a symlink outside job storage.

A separate `worker_executions` registry table binds each record to the exact job, worker execution and recorded start time without altering historical job rows. An optional attempt number is emitted only when handoff state names that job as its owner. Failed heartbeat I/O remains advisory and does not stop work or trigger repeated writes at loop speed.

Both job status surfaces expose:

```json
{"available":true,"reported_at":"2026-09-06T00:00:05Z","attempt":2,"meaning":"worker_liveness_only","unavailable_reason":null}
```

It means the worker wrote a record at that time. It says nothing about useful model activity, current process health, a stall, gate success or completion. Even an old timestamp is an observation, not a health verdict. Fresh heartbeats do not extend model/gate deadlines or worker leases.

Unavailable records keep the same shape with `available:false` and null reported_at/attempt. Reasons include legacy_worker, missing, malformed, oversized, unsafe_or_unreadable, ownership_mismatch, attempt_mismatch, future_timestamp and registry_unreadable. Queued and terminal jobs report queued/terminal without reading an old heartbeat file. Existing workers require no restart or migration and report unavailable until a new worker runs. If the whole progress observation fails, the heartbeat placeholder is not_observed.

## Recent activity

`progress.recent_activity` contains `items` (at most five), `truncated`, `window_incomplete`, and `unavailable_reason`. Each item has observed_at, category, source, event and actor. Category/source/event use the existing progress allowlists; actor is null for the parent or the existing 12-hex delegate handle for a conservatively attributed child.

Events are sorted by their parsed timestamps. At exact ties, child observations sort before parent observations, then by safe actor and original record order. Consecutive observations with equivalent category/source/actor collapse to the most recent one. The newest five transitions are returned oldest to newest. Timestamps do not establish causality between concurrent delegates.

`truncated` means earlier transitions were dropped to satisfy the five-item output bound. `window_incomplete` means the reused observation reported a limitation, such as a truncated byte/record/discovery window, malformed records or unavailable child attribution. An empty, queued, terminal or otherwise unobservable attempt has an empty timeline and an explicit reason. The timeline is never an exhaustive attempt log, and no old terminal transcript is reconstructed.

The reader reuses the same bounded scan as latest activity: no additional transcript bytes or child files, journal, background collection, model calls or state writes. Thinking, prose, descriptions, commands, tool inputs/results, errors, paths, raw tool IDs and unattributable child activity cannot enter the timeline. A shell transition never means tests passed.
