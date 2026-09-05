# Recovery and evidence

Start with `room_status`, `room_history`, and the saved job ID. A network error or interrupted connection does not establish whether a model received a request. Read the existing job before deciding what to do next.

A matching completed request returns its saved result without another model call. Reusing the ID with different content is rejected. Uncertain delivery, malformed output, wrong session/spec, or model identity failure must not be bypassed by deleting state, changing the model, creating a replacement room, or submitting the same work under a fresh ID.

A user-requested cancellation uses `room_job_cancel`; then inspect its terminal outcome. Do not call cancellation a rollback of changes or proof that no model tokens were used. Work already performed may need inspection.

## Local authentication failure

Use `room_job_recover(job_id, diagnosis)` only after diagnosing the saved review job's local sign-in failure. This audits unchanged raw output and preserves the original failure. It requires Claude's exact “Not logged in” result, exit code 1, the saved session UUID, zero token usage, empty model usage, zero API duration/cost, and no structured model output. It cannot recover implementation jobs or unknown delivery. The diagnosis is an explanation, not a substitute for evidence.

Successful recovery returns `not_sent` and makes no model call. After fixing the authentication/configuration cause, submit with a new request ID in the same room. The original UUID remains: if Claude persisted the user/error locally, the next request resumes that session. Do not edit raw output, invent evidence, or retry when the recovery check rejects it.

Use Claude Code's normal login flow and verify with `doctor`. The controller snapshots the original `CLAUDE_CONFIG_DIR` override separately from the resolved directory used to find transcripts. On macOS, explicitly setting even the default directory can select a different authentication namespace. Preserve an originally unset override; do not inject `~/.claude` as a login workaround.

## Identity reconciliation in the low-level review engine

Claude may hash project directory names when paths are long. `session_paths.py` locates only the saved UUID filename under the configured projects directory and validates session/worktree metadata. It does not read unrelated sessions or choose one by recency. Ambiguous or mismatched evidence stays blocked.

When a saved successful review fails identity verification because the predicted transcript path is absent, the controller can automatically locate that exact session and perform an audited reconciliation. It does not resubmit the review.

`room.py reconcile` provides the same narrow validation for a saved successful terminal response rejected solely for mixed-model identity. It revalidates the primary `StructuredOutput` producer against the explicit session transcript, exact returned payload, session UUID, expected model, and attempt time window. It makes no subprocess/model call and preserves the original failure and raw evidence.

From the plugin directory, use the room engine directory and actual request ID:

```sh
python3 room.py --room /absolute/private/path/to/engine-room reconcile \
  --request-id ACTUAL_REQUEST_ID \
  --session-transcript /absolute/private/path/to/SESSION_UUID.jsonl \
  --note-file /absolute/private/path/to/evidence-note.md
```

The evidence note explains what was examined. This command does not recover a nonzero process exit, malformed reply, wrong session/spec, or unknown delivery. Where the engine supports a legacy verifier error, any inferred zero exit code remains explicitly distinguished from a measured one. Never manufacture transcript evidence, search unrelated sessions, or expose model thinking.

## Scope discoveries and corrections

An implementation `scope_change` creates a blocking issue. Register a strictly newer specification, record the issue's disposition and rationale against that revision, and obtain renewed Astra/Fable agreement before another handoff. The old consensus cannot clear the discovery.

For a known completed implementation within the same scope, record actionable rejection, call `room_implementation_revise`, then submit a new implementation request ID for that handoff. Rerun gates and independently review the correction. This repair path does not apply to uncertain delivery.

## Diagnose capabilities separately

- A successful MCP initialization/tool list proves the server process and tool schemas are available.
- A successful health request proves only the reported health/model/window facts at that time.
- A completed generation proves inference for that request; it does not independently prove task correctness.
- A TCP timeout proves the target port was unreachable within the probe window. It does not distinguish an offline host, network/VPN route, firewall, or listener problem.

A denied authenticated request must not be retried through another tool or wrapper to evade the denial. Continue safe independent work, use a permitted diagnostic when it answers the remaining question, and report the exact blocked action and reason if authenticated access remains necessary.
