# Tool operations and recovery

Prefer the installed `room_*` MCP tools. Inspect their live schemas for exact types and return values; the controller CLI exposes the same operations. The plugin root is two directories above this skill directory.

## Setup and CLI fallback

Run from the installed plugin directory or use its absolute controller path:

```sh
python3 project_room.py setup
python3 project_room.py doctor
python3 project_room.py call room_list --args '{}'
```

`setup` can take `--claude-bin /absolute/path/to/claude` and `--qwen-config /absolute/private/path/to/qwen-config.json`. Qwen is optional. Use an existing trusted server definition; do not fetch or expose a credential. State defaults to `~/.project-room` and can be relocated with `PROJECT_ROOM_HOME`. Configuration lives at `config.json`, the index at `registry.sqlite3`, and individual rooms under `rooms/<id>`. Keep this data outside the plugin cache and source repository.

Claude Code needs its normal saved subscription login and access to the requested Fable model. Use the CLI's standard authentication flow if necessary. A Desktop Code-tab login may not authenticate the standalone CLI. The controller preserves the original `CLAUDE_CONFIG_DIR` override, including leaving it unset; the transcript lookup directory is separate. Do not set the default directory explicitly as an authentication fix. `room_doctor`/`doctor` reports minimal authentication and setup metadata. It does not authorize API billing or establish Qwen inference health.

From another working directory, replace `project_room.py` with its absolute installed path. Use `call TOOL --args-file /absolute/private/path/to/arguments.json` for multiline specs and notes, or use a structured MCP call. With `--args`, shell-quote JSON as code rather than constructing shell commands from untrusted text.

## Operations

The plugin exposes these 17 tools:

| Tool | Purpose |
| --- | --- |
| `room_open(project_path, feature)` | Create or reuse the project/feature room. |
| `room_list(project_path?)` | Find existing rooms. |
| `room_status(room_id)` | Read lifecycle state, current revision, and work status. |
| `room_spec_put(room_id, revision, content)` | Store immutable specification bytes. |
| `room_record(room_id, sender, kind, revision, content)` | Record user/Astra discussion against an existing revision or Astra approval of the current revision. |
| `room_review_submit(room_id, revision, message, request_id)` | Submit Fable's independent spec review as an asynchronous job. |
| `room_job_status(job_id, wait_seconds)` | Read or wait for a job, at most 45 seconds per call. |
| `room_job_cancel(job_id)` | Cancel a job when requested. Inspect its resulting state before further action. |
| `room_job_recover(job_id, diagnosis)` | Audit a proven local login failure as `not_sent`; preserves original evidence and calls no model. |
| `room_history(room_id)` | Read the preserved discussion and decisions. |
| `room_issue_dispose(room_id, issue_id, disposition, rationale, revision)` | Record addressed/rejected findings or defer an optional finding to backlog. A blocker cannot be deferred. |
| `room_backlog_add(room_id, content, rationale)` | Preserve optional work outside the agreed scope. |
| `room_handoff(room_id, revision, authorization, gates)` | Bind authorized implementation and executable gates to the agreed spec. |
| `room_implementation_submit(room_id, handoff_id, request_id)` | Start Fable's implementation job. |
| `room_implementation_review(room_id, handoff_id, accepted, review)` | Record Astra's independent acceptance or actionable rejection. |
| `room_implementation_revise(room_id, handoff_id, review)` | Queue diagnosed corrections after a known result, then submit using a new request ID. |
| `room_doctor()` | Inspect local setup and authentication status. |

`gates` is a list of argument arrays, such as `[["python3", "-m", "unittest", "discover"]]`. Select the project's actual commands and working assumptions; do not use a shell string. At least one gate is required. Gates run in the isolated implementation worktree and must leave candidate source unchanged. A gate passing establishes only what it exercises, so combine it with product acceptance evidence. Handoff requires a clean Git source checkout with a baseline commit. Candidate acceptance does not itself commit, merge, push, or deploy the work. Astra continues integration and any authorized publication using normal repository tools when the user requested delivery.

## Delivery, identity, and recovery

Persist request IDs and job IDs. A completed duplicate request with the same payload reads its cached result; different content needs a genuinely new request. Read an existing pending job after a reconnect. Uncertain delivery, cancellation, interruption, model identity mismatch, or a malformed result must be examined before another submission. Never reset state, delete attempts, replace the room, or silently change primary model to get past a block.

The review engine pins the requested Fable identity and spec revision/digest. Auxiliary model usage is distinct from the primary producer. Transcript discovery matches only the saved session UUID filename and validates its metadata, including Claude's hashed directories for long paths. It does not read unrelated conversations. If a successful saved review lacks its predicted transcript path, the controller can reconcile its identity against the discovered exact session without another model call. Other identity failures remain blocked.

`room_job_recover` accepts only the documented, zero-usage local authentication failure backed by unchanged raw evidence. Supply a diagnosis, inspect the returned audit, and fix the cause. Only a successful `not_sent` result permits a new request ID in the same room; the saved UUID is retained and resumed if Claude persisted a local session. Recovery itself never retries. See [recovery](../../../docs/recovery.md) for the exact boundaries and low-level diagnostic command.

Respect the recorded review-attempt cap. A revision change invalidates previous agreement. Resolve outstanding findings and obtain acceptance for the exact current revision. If the cap prevents progress, report the concrete decision needed; do not manufacture approval.

An implementation `scope_change` creates a blocker. Its disposition requires a strictly newer spec revision; obtain renewed agreement and resolve the issue before another handoff. A rejected implementation within unchanged scope needs diagnosed instructions through `room_implementation_revise`, another `room_implementation_submit` with a new request ID, corrected findings, and rerun gates before Astra accepts it. A completed worker is not itself evidence that implementation passed gates or product review.
