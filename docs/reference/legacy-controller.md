# Legacy controller

## Legacy controller

The Codex Astra/Fable controller predates the AO adapter and remains fully supported for existing rooms and for users without AO. Keep its properties separate from AO's:

- **Setup and use.** `python3 project_room.py setup` and `doctor` as above, then `room_open`, `room_spec_put`, `room_review_submit`, `room_handoff`, `room_implementation_submit`, `room_implementation_review` and the other `room_*` tools drive the same spec, review, handoff and acceptance loop. Review and implementation calls return job IDs promptly; follow them with bounded `room_job_status` waits, and resume from saved status and history after a disconnect. Implementation needs a clean Git checkout, creates an isolated `codex/implementation-*` worktree, and leaves the candidate uncommitted. See [operations](../../skills/project-room/references/operations.md).
- **Status and progress.** Each job carries a read-only `progress` object (phase, elapsed time, last observed activity, delegates, and a countdown to the pinned model or gate timeout) plus a worker heartbeat and recent activity; none of it is proof of useful work or completion. See [progress](progress.md) and [status and heartbeat details](status-followups.md).
- **Timeouts.** The legacy controller pins a model-invocation timeout and gate timeouts per job. AO adds no Project Room primary model-run deadline: a native turn runs until it finishes or AO stops it, while verification gate timeouts, DeepSeek request limits, provider and account limits and the model's context window still apply.
- **Recovery.** Legacy audit and recovery lanes handle two exact shapes. For a job stopped only by the model-invocation timeout or the provider's session-usage-limit error, `room_implementation_audit` and `room_implementation_recover` call no model and prepare an immutable continuation record only after a host restart that postdates the original failure; `room_implementation_submit` then launches the one authorized Fable successor, a new job that still faces fresh gates and Astra acceptance. For a completed model result followed by a gate timeout, `room_verification_audit` calls no model and `room_verification_retry` reruns only the pinned gates, never the model, in an isolated copy with a private `TMPDIR`, and requires your separate explicit authorization to rerun those exact gates. Both lanes require every identity and evidence value to match a fresh check and keep the original job unchanged. Each review round allows three Fable reviews, continued by a recorded user decision. See [continuation and recovery](../operations/recovery.md).
- **Rooms and tasks.** Existing legacy rooms keep their backend, exact revisions, session identity and pinned provider. A Codex fork copies conversation history but is not an AO room migration; a deliberate handoff to another task, or from legacy to AO for genuinely new work, must preserve the recorded decisions, authorization and review requirements. Updated tools load in a new task, or the installed CLI operates on an existing room from an existing task.

### Qwen delegation (legacy)

Qwen is the earlier optional delegate provider. A room pins DeepSeek or Qwen at creation, never both; new AO rooms do not accept Qwen, and existing Qwen rooms keep their ladder. To connect an existing trusted `qwen-local` stdio server for new legacy rooms:

```sh
python3 project_room.py setup --qwen-config /absolute/private/path/to/qwen-config.json
```

| Tool | Guard policy |
| --- | --- |
| `qwen_submit` | `effort="xhigh"`, `max_tokens=131072`; omissions receive these values and deviations are rejected before forwarding. |
| `qwen_ask` | Effort `none` or `low`; default `low`. |
| `qwen_status` | `wait=true`; positive finite timeout no greater than 49 seconds; default 45. |

The intended Qwen3.8-27B server window is 262,144 tokens, split between prompt and thinking plus answer; the upstream server owns the prompt-size precheck. Tool discovery, health and successful inference are different checks; report the actual evidence and never weaken the guard settings or claim a delegate ran when it did not.
