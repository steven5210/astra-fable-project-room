# Project Room on Agent Orchestrator

This is an opt-in local adapter for stock AO, initially exercised against v0.12.12.
AO owns native sessions, worktrees and conversations. Project Room keeps specs,
delivery receipts, verification and acceptance outside the repository and plugin cache.
It is not an additional scheduler and does not require an AO fork or Paperclip.

## Role policy and current readiness

The designated normal workflow keeps Astra responsible for product planning,
specification and independent acceptance, and Fable responsible for engineering
interpretation, implementation, engineering review and delegates. Preserve the
existing exact-spec agreement and pinned delegate-provider requirements. A
temporary request to save Fable usage applies only to the identified task.

New rooms default to `workflow: "fable_engineering"`. They require completed,
exact-spec Fable agreement, a prepared native engineer workspace, an immutable
handoff, a structured engineering result and independent Astra acceptance. The
adapter reuses the retained DeepSeek policy, provider snapshot and ledger. It
introduces no scheduler, model-run deadline or provider implementation.

Choose `delegate_provider: "deepseek"` from the user's recorded setup, or `"none"`
only when explicitly intended. Missing selection fails closed. New AO rooms do
not accept legacy Qwen; existing Qwen rooms remain on their recorded backend.
A task-specific `workflow: "astra_led"` exception requires actual
`exception_authorization`; never infer one from a past usage-saving request.
Existing version-1 pilot rooms remain readable and retain their original meaning.

Offline tests establish adapter contracts. Live local evidence on the installed
setup has established the normal Fable/DeepSeek engineering workflow, independent
acceptance, a native `pr-sonnet` plus `pr-opus` routing probe with no fallback or
nesting, a manual Claude `/compact`, and one controlled interrupted-turn native
stop/resume that kept the same native session, candidate state and Fable MAX and
applied the continuation once. Those checks do not establish threshold-triggered
automatic compaction, OS or daemon crash recovery, reconciliation of arbitrary or
paid delegate interruptions, native-child Chrome access, or automatic Project Room recovery.
Report each fact separately and keep the underlying receipts private.

## Stable-release check

At the start of each new or resumed AO work session, compare the installed and
running AO release with the official [latest stable release](https://github.com/Untrivial-ai/agent-orchestrator/releases/latest).
The machine-readable feed is `https://api.github.com/repos/Untrivial-ai/agent-orchestrator/releases/latest`;
exclude drafts and prereleases. Do not treat the initial validation version above
as a permanent pin, or a development branch as a stable update.

Identify the actual daemon through its configured endpoint's `GET /healthz`
`executablePath`, and inspect that executable's version and installation provenance.
Some official bundled daemons report `dev`; on macOS, corroborate the containing
signed app's `CFBundleShortVersionString` with the saved verified release receipt.
A config string alone does not prove the running version. If AO is stopped, report
the installed version separately and check the running daemon after startup.

Save the UTC check time, installed/running/latest versions, their evidence, release
URL and outcome in private `PROJECT_ROOM_HOME/ao/version-check.json`. Fetch failure,
rate limiting or ambiguous local identity means **unknown**, never "up to date";
retain the last successful check separately. Warn about a new release, a version
mismatch or a check failure. Do not prevent read-only status, reconciliation or
recovery, and do not interrupt active workers to complete this check.

When a newer stable release is available, review its changes and arrange an update
between jobs. Preserve the existing runtime/state and a consistent backup before
switching; check adapter compatibility and a read-only daemon smoke test before
new model work. Follow existing upgrade authorization, escalating only actual
breaking changes or decisions outside that scope. Never silently switch to a nightly,
restart active workers, migrate rooms or replay uncertain requests for an update.

AO's packaged desktop app has its own updater; a directly launched bundled daemon
does not run that desktop updater. A configured daily release monitor supplements
this per-session check. The skill itself does not install a background service.

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

## Normal Fable workflow

1. Register exact UTF-8 content, a positive revision, explicit Astra approval and
   nonempty executable gate argument arrays using `ao_room_spec_put`. These are
   immutable. A material scope change needs a newer revision and fresh agreement.
2. Prepare the native engineer's isolated Git worktree **before Claude launches**.
   AO's stock `postCreate` project hook can run `python3 /absolute/ao_delegates.py
   --home /absolute/private-state --room ROOM_ID` from the created worktree. Build
   that hook using properly quoted fixed argv and preserve unrelated hooks. Scope
   its installation to creation of the intended engineer, then restore the prior
   project configuration. A failed preparation must not launch a paid turn.
   Create the reviewer before installing this room-specific hook, or only after
   restoring it; otherwise its workspace would receive the engineer preparation.
   `ao_room_prepare` is idempotent for the exact workspace. A pending preparation
   is reconciled only from matching observed configuration, never by replaying it.
   The same preparation writes the worktree-scoped native routing files described
   below into already-ignored paths and refuses before writing anything when a
   path is tracked, unignored, symlinked or conflicting.
3. Create an ordinary AO Claude chat worker without an initial prompt; configure
   exact `claude-fable-5-1` and `max`, then bind it as `engineer`. Prepare a separate
   native Codex chat worker at the requested Astra model and `max`, and bind it as
   `reviewer`. `ao_room_bind` checks AO's actual workspace, project, harness and
   conversation. Bindings and the engineer workspace cannot be replaced.
4. Send `ao_room_send` with engineer purpose `spec_review` and a stable request ID.
   Fable's native final JSON must contain `interpretation`, `findings`, `decision`
   (`accept` or `changes_required`), `spec_revision` and `spec_sha256`. Findings
   prefixed `BLOCKER:` prevent agreement. Sync the completed turn before another
   send. Agreement binds that actual receipt to Astra's exact approved spec.
   Three spec-review attempts are available across revisions; no automatic renewal.
5. Call `ao_room_handoff` for the actual bound engineer worktree. It pins the exact
   agreement, baseline, candidate, authorization, delegate preparation and gates.
   Send engineer purpose `implementation` once for that handoff. The packet carries
   the exact spec and retained policy, plus the pinned delegate settings. Fable
   owns engineering and eligible delegation; it must not publish or start extra
   AO workers. Native MCP launch evidence is distinct from successful inference.
6. Sync completion to capture Fable's candidate immediately. Its final JSON must
   include `outcome` (`completed`, `changes_required` or `scope_change`), boolean
   `implementation_complete`, lists `changes`, `tests_reported`, `review_findings`,
   `remaining_gaps`, `backlog`, `routing_log`, and exact `spec_revision`,
   `spec_sha256`, `baseline_commit`. Each routing entry includes
   `delegate_job_ids` (empty for native-only work). Acceptance requires completed,
   true and no remaining gaps. Optional proposals remain proposals until approved.
7. For a confirmed completed turn, a new `correction` request may repair the
   implementation or missing/malformed report in the same session. Preserve the
   previous receipt; this is a new focused turn, never a replay. A `scope_change`
   report requires a revised agreed spec first. Unknown delivery stays blocked.
   Commit, if needed, before the final engineering response is captured: later
   changes to HEAD/index/files invalidate its candidate and need a correction.
8. Run the gates and independent acceptance below using reviewer purpose
   `acceptance_review`. Inspect actual delegate ledger facts when assessing
   Fable's routing report. Native subagent audit coverage remains a separate item.

Claude's local MCP registry is shared by Git worktrees of the same repository.
Preparation installs one shared, private, content-addressed launcher using
`claude mcp add-json --scope local`. The launcher selects exactly one pinned room
by the actual worktree and native `AO_SESSION_ID`, verifies Git identity and
snapshot hashes, then executes the retained room-specific DeepSeek server.
Room paths, settings, export directories and model policy never enter candidate
files. Unrelated MCP entries are preserved; a conflicting `deepseek` entry blocks
preparation. Do not overwrite it, silently switch providers or relax the pins.

## Native delegation routing

Newly prepared normal rooms route Fable's native delegation deliberately. The
postCreate preparation writes three worktree-scoped files into paths the
repository must already ignore: `.claude/settings.local.json`,
`.claude/agents/pr-sonnet.md` and `.claude/agents/pr-opus.md`. It runs
`git check-ignore` and a tracked-file check for each path first, refuses
tracked, unignored, symlinked or conflicting files before writing either agent
file, never edits `.gitignore` or shared Git exclusions, and preserves unrelated
keys of an existing local settings file. The definitions pin `pr-sonnet`
(`claude-sonnet-5`, effort max, mechanical implementation and tests, no skills)
and `pr-opus` (`claude-opus-5`, effort max, bounded judgment/review plus the
pinned browser skill when the AO browser capability is present); both refuse
further delegation, workflows and messaging, and both disallow the inherited
MCP submission routes (the `mcp__deepseek`, `mcp__qwen-local` and
`mcp__project-room` servers, plus the DeepSeek submit and ask tools by name) so
a native worker cannot submit delegate or room work while the root Fable
engineer keeps its pinned provider access. Their frontmatter scalars are quoted
so Claude's own YAML loader reads them. The local settings carry
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=2`,
`CLAUDE_CODE_DISABLE_WORKFLOWS=1`, `CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS=1`,
deny rules for `Workflow` and the automated review skills, and a `PreToolUse`
hook running a private, content-addressed, deny-only guard from
`PROJECT_ROOM_HOME/ao/launchers`. The guard permits `Agent` only for the two
pinned types without model, isolation, resume or other overrides, refuses any
nested dispatch (an event carrying `agent_id` or `agent_type`), workflows,
teams, `SendMessage` continuation routes, every skill except the pinned
browser skill inside an event whose `agent_type` is exactly `pr-opus`, and any
`mcp__deepseek__*`, `mcp__qwen-local__*` or `mcp__project-room__*` call made
inside a native worker (the root engineer's own calls take no decision). It never
grants a permission; a guard error, a missing interpreter or a missing script
exits 2, so the call is blocked.

Preparation snapshots the file digests, guard digest, interpreter, knobs, the
pinned browser skill name (`claude-in-chrome`, a constant that no private
configuration can override), bounded Claude executable identity (configured
absolute path, size, mtime and the reported `--version` line; no inference), and
the AO project rules it observed through the project API: the inline
`agentRules` digest, verbatim presence of the authorized Project Room delegation
clause, an `agentRulesFile` digest when one is configured, preserved fields
(worker harness, model and permissions, container reap, default branch) and the
routing-related project `env`. The Claude configuration directory is resolved
once from the room or controller settings (the override key and the recorded
directory must agree). It refuses to prepare when the clause is absent, when
user, managed, project or existing local settings disable hooks, allow managed
hooks only (`allowManagedHooksOnly`, which would suppress the local guard), set
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE`, change the depth/concurrency values, define
`modelOverrides`, or restrict `availableModels` without both pinned models, when
those settings files exist but cannot be read, when a same-named `pr-sonnet` or
`pr-opus` definition exists in the user-level `agents` directory, or when the AO
project `env` does the same. Every refusal writes `routing-error-*.json` in the
room directory and leaves the room unprepared, so the postCreate hook fails and
AO does not launch the worker.

Only delegation-capable steps re-validate offline (engineer bind, handoff,
and implementation or correction dispatch): pinned file digests and parsed
definitions, ignore status, guard digest, interpreter, executable identity and
the surrounding settings. Fable's read-only specification review and Astra's
acceptance review are allowed without revalidating routing, while delegating
implementation or correction remains blocked by pre-dispatch validation; after
drift, a running session's effective enforcement is not certified.
Implementation and correction packets also re-read the AO project rules and
refuse when the clause is missing, the rules digest, a preserved field or the
routing-related env changed, or the rules cannot be read (the failure is
recorded). `ao_room_sync` records the latest observation without refusing on
drift or an unreadable AO, but it does refuse when the content-addressed
observation receipt it would reuse has been modified. Observation evidence is
re-verified before status relies on it. `ao_room_status` stays offline:
`delegate.routing.status` is `not_configured` for rooms prepared before this
mechanism (readable, never relabeled), `configured` after preparation or while
executable version evidence is missing, `verified` once a later dispatch or sync
observed consistent rules with that evidence present, and `unverified` with the
reason on any local drift, contradictory settings, damaged evidence or
inconsistent observation. None of these states
proves native enforcement, the served model or effort, the concurrency/depth
caps, resumption paths or compaction; workflow, resume and fork paths outside
the `Agent` tool are not covered by the guard, and the worker's actual process
environment is set by AO and is not observable at preparation time.

Adoption procedure for a future session: (1) confirm the repository ignores
`.claude/` (or those three paths) and that the user/managed Claude settings do
not disable hooks, force subagent models or restrict the pinned models;
(2) install the authorized delegation clause in the AO project `agentRules`
through stock AO configuration, preserving the existing fields; (3) install the
scoped postCreate hook, create the engineer, restore the hook; (4) bind, agree,
hand off and dispatch as usual; (5) after offline acceptance, the Astra operator
runs the separately recorded smoke test with at most one `pr-sonnet` and one
`pr-opus` task, reading the native subagent transcript model/effort evidence and
Fable's verdict. The stock AO worker prompt keeps its generic prohibition on
native subagents; the project clause is explicit task authorization, not
enforcement. If `pr-opus` cannot use the pinned browser skill, report it as
unvalidated and keep the existing explicit AO Opus browser route. Existing
legacy rooms keep their recorded backend, policy and planning state; nothing
here migrates or relabels them. A repository that does not ignore the required
paths is not adopted by this mechanism: adding its ignore rule is a deliberate
setup change under the user's existing setup authorization, and preparation
refuses until it exists.

## Explicit Astra exception and historical pilot rooms

Use `workflow: "astra_led"` only for the user's actual task-specific exception.
Astra may implement directly in an isolated worktree; an AO engineer is optional.
Bind a separate native Codex reviewer and use the same verification/acceptance
contract. Omitted send purpose preserves the historical pilot packet. A Claude
binding requires `fable_reason`. This mode does not establish Fable agreement or
delegate use, and never changes the default roles for future rooms.

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
Primary native turns have no adapter model deadline. The 15-second HTTP waits,
verification gate timeouts, retained DeepSeek request limits and provider quotas
still exist; an observation timeout does not prove primary completion or cancel it.

## Verify and accept

Run `ao_room_verify` on the intended candidate worktree. It must belong to the
room's Git repository. Normal rooms require the unchanged captured Fable candidate
in its bound worktree and a complete engineering report. This runs the spec's argv gates (no implicit shell) with a
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
reroute is preserved and blocks acceptance if it contradicts the pinned model.
Normal rooms also audit engineer identity before new dispatch, handoff and acceptance. Sync persists the contradiction immediately when observed,
including during a running turn, and retains it even if later metadata names the
pinned model again. Acceptance checks for late reroute evidence too. A reroute
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

Normal AO rooms snapshot the selected retained DeepSeek adapter, policy and
configuration at creation; no key contents are copied or exposed. Private
`delegate.attachment` distinguishes configured attachment, observed native MCP
launch and unverified evidence. `delegate.jobs` exposes the existing bounded
ledger projection, with usage separate from primary totals. Admission checks use
the **full** room ledger: an old unresolved job still blocks even outside the
latest 20 displayed records. Active or unresolved delegate jobs prevent another
phase or acceptance. Only the user's own supported terminal action can resolve
uncertain DeepSeek delivery; neither agent may self-resolve or replay it.

Compact spec/evidence packets limit repeated context. Native compaction remains
owned by AO/the provider and is not automatically triggered by this adapter.
Manual Claude compaction and a controlled native stop/resume are validated only
as described under readiness above; automatic compaction thresholds and crash
recovery still require their own live validation. Do not claim a smaller context
window reading proves lower total usage or recovered subscription budget.
