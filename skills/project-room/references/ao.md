# Project Room on Agent Orchestrator

This is an opt-in local adapter for stock AO, initially exercised against v0.12.12.
AO owns native sessions, worktrees and conversations. Project Room keeps specs,
delivery receipts, verification and acceptance outside the repository and plugin cache.
It is not an additional scheduler and does not require an AO fork or Paperclip.

## Setup and resume

Use an already running, trusted local AO daemon with existing native authentication.
Add the actual Git project in AO. Use an explicit loopback IP and port, for example
`http://127.0.0.1:PORT`; remote hosts, proxies, credentials in URLs and redirects are
refused. Pass `ao_url` to `ao_room_open`, or save `{"ao_url":"http://127.0.0.1:PORT"}`
in `PROJECT_ROOM_HOME/ao/config.json` (default home: `~/.project-room`). The
`PROJECT_ROOM_AO_URL` environment variable takes precedence over that config.
Never embed local paths, live IDs or authentication in the distributed plugin.
When the user chooses AO for new Project Room work, also record
`"default_backend": "ao"` in that private config. The skill uses this preference;
the low-level legacy tools remain callable and never migrate a room automatically.

Call `ao_room_open` with the actual project path, stable feature name, existing AO
project ID and the user's existing implementation authorization. Reopening returns
the same room. Save its `room_id` and `room_path`. Inspect `ao_room_status`; use
`ao_room_sync` to reconcile active work. Status reads saved facts without network
access. Sync makes bounded AO GET requests and saves evidence; it never invokes a
model. AO must be reachable for operations that check whether workers are idle.
Saved status remains readable while a verification gate holds the mutation lock.
Use `ao_room_list` to find saved AO rooms; it returns at most 50 metadata records
with explicit truncation. Check legacy `room_list` before treating an ambiguous
feature as new. Do not silently replace an existing legacy feature with an AO room.

If the current task still has an older tool inventory, use the installed plugin's
CLI: `python3 project_room.py call ao_room_status --args-file /absolute/args.json`.
The CLI exposes all `ao_room_*` operations using the same schemas as MCP. A new
Codex task discovers updated MCP tools after plugin installation.

## Astra-led implementation

1. Ground the spec in the repository. Register exact UTF-8 content, a positive
   revision, explicit Astra approval and nonempty executable gate argument arrays
   using `ao_room_spec_put`. All four are immutable. Use a newer revision for a
   genuine scope change; preserve actual user decisions. Approval means Astra
   supports this authorized scope, not that Fable reviewed it.
2. Astra may implement directly in an isolated Git worktree. An AO engineer is
   optional. If useful, create an ordinary native AO **chat worker**, then bind its
   exact session ID, configured model and reasoning effort with `ao_room_bind`.
   AO creates the session; this adapter does not spawn or replace workers.
3. Use a separate native Codex chat worker for independent review. Configure the
   requested model and effort in AO, then bind it as `reviewer`. Each session can
   belong to only one room/role. Bindings cannot be swapped to evade a failure.
   Claude bindings require `fable_reason` describing why Fable is actually needed.
   Do not call Fable just to satisfy the legacy workflow's role assignment.
4. For an AO engineer, send a concise task with `ao_room_send`. Keep the stable
   `request_id`. The packet includes the exact spec path/hash; reference relevant
   artifacts rather than copying accumulated transcripts. Do not start work on the
   same bound sessions through a second controller while a room request is active.
5. Call `ao_room_sync` after a turn completes and **before starting another turn**.
   Inspect saved terminal receipts and actual changes; successful process exit is
   not acceptance. Correct a rejection reported by a confirmed completed native turn
   under the existing authorized scope. A new send requires a new request ID. An uncertain turn is never replayed,
   even if a status page appears idle or a POST returned an error.

One adapter-wide filesystem lock serializes local changes and cross-room session
claims. A durable intent with a native `clientMessageId` is written before POST.
Identical repeated calls read the saved record; they do not POST again. After a
lost acknowledgement, sync requires a unique exact sent message and matching
conversation branch before adopting its turn. Missing history is not proof of
non-delivery. A saved AO failure without an observed native `providerTurnId` remains
uncertain: AO may have lost the provider's acknowledgement after dispatch. Original
failed observations remain in the receipt history when later evidence settles the
turn. Failed, interrupted and cancelled AO outcomes remain uncertain even when
they have a turn ID: the native driver can assign that ID before a later transport
failure. Status exposes the observed `ao_turn_state` separately. This conservative
initial adapter has no automatic recovery lane for such outcomes; diagnose them
without resending. There is no automatic retry, timeout cancellation or worker failover.

## Verify and accept

Run `ao_room_verify` on the intended candidate worktree. It must belong to the
room's Git repository. This runs the spec's argv gates (no implicit shell) with a
pinned per-gate timeout, default 120 seconds, maximum 7200. Supply a larger budget
when the known suite requires it. Gate programs are trusted authorized project
commands, not a sandbox; do not put model calls or publication commands in them.
Timed-out process groups are stopped and their failure logs retained.
If the verifier itself crashes before saving a terminal receipt, its durable
running record blocks further mutations. Diagnose the saved evidence and any
surviving processes; this initial adapter has no automatic verifier-recovery lane.
Do not erase the record or launch a replacement to bypass the uncertainty.

Verification binds tracked and untracked committable files, deletions, symlinks,
file modes, the index and HEAD before the gates and after **each** gate. Observable
drift stops the sequence immediately, so another gate cannot conceal it by restoring
the files. Ignored files and
external dependencies are outside this fingerprint. A changed candidate or failed
gate cannot produce an acceptable checkpoint. Commit before final verification
when a commit is part of the intended candidate; committing afterwards changes
HEAD and the index and therefore requires fresh verification and review.

Send the reviewer a focused acceptance request through `ao_room_send`. The adapter
adds the exact candidate/evidence paths and required verdict contract. The reviewer
must inspect independently, stay read-only, and finish with one JSON object:

```json
{
  "decision": "approved",
  "spec_sha256": "the exact supplied spec hash",
  "candidate_sha256": "the exact supplied candidate hash",
  "evidence_sha256": "the exact supplied checkpoint hash",
  "review": "Concrete assessment of behavior, evidence and remaining limitations"
}
```

Use `rejected` with actionable findings when appropriate. Sync its terminal native
response, investigate findings, repair and reverify if needed. `ao_room_accept`
accepts only an actual completed independent reviewer response with matching
identities, intact logs and unchanged candidate bytes. There are at most three
review requests per room across revisions. If exhausted, surface the unresolved
decision to the user; this initial adapter has no automatic budget-renewal lane.
Do not create another room/session to bypass that limit. An explicit AO model
reroute for the reviewer turn is preserved and blocks acceptance if it contradicts
the pinned model. Acceptance checks for late reroute evidence too. A reroute
explicitly attributed to another native turn remains historical.

Acceptance does not merge, publish or deploy. Continue authorized integration
using normal repository tools, preserving unrelated changes and checking that the
reviewed bytes are the ones integrated. Saved `latest_acceptance` is historical;
status alone does not revalidate current filesystem content.

## Usage, context and delegates

Receipts archive per-turn primary usage before another turn can overwrite it.
For native Codex, counters are cumulative: the adapter subtracts the pre-send
baseline. For Claude ACP, counters describe the latest user turn: the adapter sums
distinct recorded turns. Cache counters are not added a second time to total
tokens. AO combines Claude cache reads and writes; the adapter does not invent a
split. Context occupancy is a separate latest-turn value, not total consumption.

Only an observed isolated turn on the same native conversation branch gets a
known receipt. Missing or unchanged counters, decreased cumulative baselines,
overlapping external turns or truncated history produce **unknown**, not zero.
AO can retain old counters when a provider omits new usage. Identical Claude totals
may be legitimate, but this snapshot cannot prove freshness, so they remain unknown.
The status total is a subtotal of
known primary receipts, excludes delegates, and is neither subscription quota nor
billing. Configured model/effort is checked through AO, not presented as a provider
attestation. Status labels the pinned model as `configured_model` and exposes any
contradictory native reroute separately. Requests are ordered by durable creation
order, with active/uncertain work retained within the bounded projection. Native
transport receipts remain the source for stronger attribution.

The existing DeepSeek adapter and its pinned provider policy remain available and
unchanged. Reuse them only for work that benefits from delegation and preserves
the configured room/context binding. This initial AO layer does not auto-create a
DeepSeek room, transfer its provider pins, or merge delegate usage into primary
totals. Inspect an existing delegate room's `room_status.delegate_jobs` separately;
never relabel its usage as an AO primary turn or bypass its context-path guard.

Compact spec/evidence packets limit repeated context. Native compaction remains
owned by AO/the provider and is not automatically triggered by this adapter.
Claude native compaction and production failure recovery still require their own
live validation. Do not claim a smaller context window reading proves lower total
usage or recovered subscription budget.
