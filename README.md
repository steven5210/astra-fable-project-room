# Project Room

Project Room turns a feature request into a versioned specification, an engineering handoff, executable verification evidence and an independent acceptance record, all kept in persistent private rooms outside the repository. It runs on two layers:

- **Stock [Agent Orchestrator](https://github.com/Untrivial-ai/agent-orchestrator) (AO)** is the recommended native session and worktree layer. AO owns the Claude Code and Codex chat workers, their isolated Git worktrees, conversations, project hooks and project rules. No AO fork and no Paperclip are required.
- **Project Room** is the specification, handoff, evidence and acceptance layer. Its `ao_room_*` tools pin immutable spec revisions and gates, send each request once with a durable identity, prepare private delegates before the engineer launches, archive per-turn usage receipts, run the agreed gates against the exact candidate, and accept only an independent reviewer verdict that names the same spec, candidate and evidence hashes.

The earlier Astra/Fable controller inside Codex (the `room_*` tools and `project_room.py` CLI) remains available as the legacy backend. Existing legacy rooms keep their recorded backend, exact revisions, session identity and pinned delegate provider; nothing migrates automatically. See [Legacy controller](#legacy-controller) below.

An existing normal AO room can make one explicitly authorized DeepInfra-to-official
provider amendment and adopt v2 routing while retaining its native history and
review counters. The [retained adoption procedure](docs/provider-transition.md)
requires stopped-session audits, immutable evidence and verified native MCP
initialization before new work. Changing setup defaults alone does not do this.

After installing and configuring the plugin, ask for the work in ordinary language:

> Use Project Room for this feature: let users save and name their search filters.

The bundled [skill](skills/project-room/SKILL.md) supplies the roles and workflow; you do not paste an orchestration prompt each time. A request to build a feature carries through implementation; a planning-only request stops at the agreed specification. Existing authorization carries forward, and merge, publication and deployment stay outside the room unless you asked for them.

## What each participant owns

| Participant | Responsibility |
| --- | --- |
| You | Product intent, priorities, meaningful tradeoffs, and the authorization that each handoff binds. |
| Astra (Codex, native reviewer) | Requirements, the exact specification revision, dispositions of findings, enhancement proposals, and independent acceptance of the exact candidate. |
| Fable (Claude Code, `claude-fable-5-1` at `max`, the engineering director) | Engineering interpretation and agreement on the exact spec, direction of implementation and delegation under the pinned policy, engineering review, and the final engineering verdict. |
| DeepSeek (configured first-tier delegate) | Self-contained analysis, implementation, tests, documentation and review text for Fable through the selected official or DeepInfra backend. The adapter is text-only: it returns code, tests, reviews and reasoning summaries, but it cannot browse, edit files or execute tools, and upstream image or video capability does not establish adapter support. |
| `pr-sonnet` and `pr-opus` (pinned native agents) | `pr-sonnet` pins `claude-sonnet-5` for mechanical implementation and named test runs; `pr-opus` pins `claude-opus-5` for bounded judgment, review and the pinned browser skill when the AO browser capability is present. Fable verifies everything they return. |
| Qwen (legacy) | The earlier optional delegate provider for legacy rooms only; see [Qwen delegation (legacy)](#qwen-delegation-legacy). |

Both agents review the same immutable spec revision and SHA-256. Findings receive explicit dispositions; a finding prefixed `BLOCKER:` prevents agreement. Both agents may propose enhancements, which Astra brings to you with the benefit, tradeoff and an issue link for your opinion and scope approval; an enhancement enters implementation only after you approve its scope and the revised spec is agreed. See [the workflow](docs/workflow.md) and [Fable's delegation policy](skills/project-room/references/fable-policy.md).

## Prerequisites

- Python 3.10 or newer on macOS or Linux. The runtime uses POSIX locks, process groups and `/proc` or `ps` inspection; native Windows is not supported. CI exercises Python 3.11 and 3.12.
- Git, with the project you want to work on cloned locally and clean at the commit that will become the handoff baseline.
- Claude Code with its normal saved subscription login and access to `claude-fable-5-1`. A signed-in Claude Desktop tab does not necessarily authenticate the standalone CLI.
- Codex with local plugin and MCP support for Astra and for the legacy controller.
- For AO work: a running, trusted local AO daemon at the official latest stable release, reachable on a loopback address, with the same native authentication AO already uses for its workers. Installing the daemon, adding a project and creating chat workers follow [AO's own documentation](https://github.com/Untrivial-ai/agent-orchestrator). The adapter was initially exercised against AO 0.12.12; that is a validation fact, not a pin.
- Optional: a separate key for the selected DeepInfra or official DeepSeek backend, entered only at your own terminal, for the first-tier delegate. Docker is only needed to run the Linux CI container locally.

## Install and authenticate

Clone or obtain this repository and ask Codex to install it as a local plugin through its `plugin-creator` workflow, keeping the folder name `astra-fable-project-room`. That workflow can register a personal marketplace entry and install the bundle; local marketplace distribution is separate from publication in a public plugin directory. The source repository is [steven5210/astra-fable-project-room](https://github.com/steven5210/astra-fable-project-room). The plugin manifest is `.codex-plugin/plugin.json` (currently version 0.3.0) and the bundled MCP server is started from `.mcp.json` as `python3 ./project_room_mcp.py` with `cwd: "."`; it needs no hosted room server or listening port.

From the plugin checkout, run setup once and check the result:

```sh
python3 project_room.py setup
python3 project_room.py doctor
```

If Claude is not found, pass its executable path:

```sh
python3 project_room.py setup --claude-bin /absolute/path/to/claude
```

`setup` accepts `--claude-bin`, `--qwen-config`, `--deepseek-config` and `--delegate-provider deepseek|qwen|none`. Authenticate through Claude Code's standard login flow when needed, then rerun `doctor`. The controller uses the CLI's saved login and preserves its original `CLAUDE_CONFIG_DIR` override; it never asks for an API key, extracts credentials or silently switches providers. Model calls still count toward the account's applicable usage. After installing or updating, open a new Codex task so the current skill and tool definitions load; an existing task keeps its older tool inventory and can continue through the [CLI fallback](#cli-fallback).

## Configure the private AO backend

All AO configuration lives in the private data directory, never in the plugin or the repository.

1. **Endpoint.** Add the actual Git project in AO. Then either pass `ao_url` to `ao_room_open` or save it in `PROJECT_ROOM_HOME/ao/config.json`:

   ```json
   {"ao_url": "http://127.0.0.1:PORT", "default_backend": "ao"}
   ```

   The URL must be an explicit loopback IP and port; remote hosts, proxies, credentials in URLs and redirects are refused. The `PROJECT_ROOM_AO_URL` environment variable takes precedence over the file. `default_backend: "ao"` tells the skill to use AO for new Project Room work; the legacy tools stay callable and never migrate a room.

   Every new normal AO engineer preparation enables Claude's native automatic compaction with a **250,000-token window**, across projects and delegate providers. To select another window for future preparations, add `"auto_compact_window": 250000` to this same private file (an integer from 100,000 to 1,000,000). Preparation snapshots the choice in the ignored worktree settings; changing the default never rewrites an existing room. Fable stays at MAX. See [context compaction](docs/context-compaction.md) for configuration, existing sessions and validation.

2. **Delegate provider.** Record the first-tier provider for new rooms with the controller's setup command, which AO and legacy rooms share; new AO rooms accept `deepseek` or an explicit `none`, and a missing selection fails closed rather than downgrading silently:

   ```sh
   python3 project_room.py setup --deepseek-config /absolute/private/path/to/deepinfra-provider.json --delegate-provider deepseek
   python3 deepseek_adapter.py set-key --home ~/.project-room --backend deepinfra
   ```

   Start the private provider file from [examples/deepinfra-provider.example.json](examples/deepinfra-provider.example.json) to select `backend: "deepinfra"`, `deepseek-ai/DeepSeek-V4.1-Flash`, reasoning effort `max` and 131,072 output tokens. The tool family stays named `deepseek`. The [official example](examples/deepseek-provider.example.json) retains `backend: "official"` and its 393,216-token budget; use `set-key --backend official` for that separate key. Both provider files are key-free; the key is entered interactively at your terminal and read only when a job sends its request or when you run the explicit paid `probe`. A room snapshots the adapter, policy and configuration when it opens, so rerunning setup never changes an existing room. See [the DeepSeek delegate guide](docs/deepseek.md).

3. **Repository ignore rule.** The engineer's repository must already ignore `.claude/` (or the three routing paths `.claude/settings.local.json`, `.claude/agents/pr-sonnet.md` and `.claude/agents/pr-opus.md`). Preparation refuses tracked, unignored, symlinked or conflicting paths and never edits ignore rules itself; adding the rule is a deliberate setup change that your existing AO setup authorization covers when it applies, not a fresh permission question for each routine ignore rule.

4. **AO project rules.** Through stock AO project configuration, add the authorized Project Room delegation clause to the project `agentRules`, preserving the existing fields. Preparation snapshots the rules digest and refuses when the clause is absent; implementation and correction packets re-read the rules and refuse on drift. AO's generic worker prompt still forbids native subagents; the clause is the explicit task authorization for the two pinned agents.

5. **Engineer preparation hook.** AO's stock `postCreate` project hook can run the preparation from the created worktree with fixed, quoted argv:

   ```sh
   python3 /absolute/path/to/ao_delegates.py --home /absolute/private/project-room-home --room ROOM_ID
   ```

   Install it only for the creation of the intended engineer, create the Astra reviewer before installing it or after restoring the previous configuration, and restore the prior hook afterwards. A failed preparation leaves the room unprepared and AO does not launch the paid worker. The full procedure, including the user and managed Claude settings that must not disable hooks or force subagent models, is in [Native delegation routing](skills/project-room/references/ao.md#native-delegation-routing).

## Roles, agreement and delegation

New AO rooms use `workflow: "fable_engineering"`. Astra writes and approves the exact specification; Fable reviews it read-only and returns an `accept` or `changes_required` verdict for those exact bytes; only then can Astra record the handoff. A material scope change needs a newer revision and fresh agreement. A task-specific `workflow: "astra_led"` exception, in which Astra implements directly in an isolated worktree, requires the actual per-task `exception_authorization`, attaches no Fable delegates, and never changes the default roles for later rooms.

Fable's delegation is deliberate and bounded:

- **DeepSeek** is the configured first-tier delegate for self-contained work. Only the root Fable engineer can submit to the room's pinned DeepSeek lane, at the room's pinned reasoning effort and output budget that no tool call can lower. Its jobs are room-scoped rows in the private per-home DeepSeek ledger, and an unresolved delivery failure stops that room's lane until you resolve it at your own terminal.
- **Native delegation** is one layer with at most two simultaneous children. The prepared local settings set `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` and `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=2`; those are concurrency limits, not a two-task total and not an intrinsic AO limit, and a launch rejected at capacity is not queued automatically. A private deny-only guard permits `Agent` only for `pr-sonnet` and `pr-opus` without model, isolation, resume or other overrides, and refuses nested dispatch, workflows, teams, messaging routes, every skill except the pinned browser skill inside `pr-opus`, and every DeepSeek, Qwen or room MCP call made from inside a child. Children therefore cannot delegate further or submit DeepSeek work.
- **Execution ownership.** New AO routing preparations enforce `execution_policy: "orchestrator"`: Fable retains read-only inspection, planning, pinned DeepSeek tools and pinned native delegation; root shell commands, edits, tests, browser actions and other execution tools are denied. The assigned operator or native worker runs probes and gates, including checks Fable requests for its verdict. Fable does not duplicate work assigned to Astra. A capability gap is reported for resolution, never worked around through another tool. This keeps necessary Fable judgment and final review while requiring execution to remain delegated; it does not change MAX or delegate budgets. Older preparations retain their original guard and report `historical_unrestricted_root` instead of claiming this protection.
- **Older rooms and workers** do not acquire this routing silently. Rooms prepared before the mechanism report `delegate.routing.status` as `not_configured`; only a fresh preparation writes the routing files, and only a later dispatch or sync can move a room to `verified`. None of these states proves native enforcement, the served model or effort, or compaction behavior.

The pinned delegate ladder, verification duties at every tier, and the fixed DeepSeek and Qwen operating parameters are in [Fable's policy](skills/project-room/references/fable-policy.md).

Fable owns delegation decisions, and quality always beats token savings. In DeepSeek rooms, DeepSeek is the first choice for substantive work whenever it can meet the full quality bar: self-contained implementation, tests, research and reviews against verifiable requirements, plus bounded module design or debugging when its demonstrated quality warrants it. Fable chooses Sonnet, Opus or itself when better suited and routes up when in doubt; it does not need to force a lower-tier failure first. Sonnet supplies agentic execution, Opus supplies bounded judgment or appropriate browser work, and Fable retains cross-cutting judgment, adjudication and the final engineering verdict. Delegates perform routine execution; Fable maintains quality through clear task requirements, suitable routing, checking results and its final verdict. In newly prepared AO rooms, Fable can retain judgment work itself but execution tools stay with the operator or pinned workers. A quality or capability gap must be reported for resolution. Older AO and legacy rooms retain their recorded direct-execution exception; it never overrides an explicit task restriction. Each routing choice records its reason and outcome.

Supply this quality-first policy when establishing the session, then send only actual policy changes. Conserve Fable tokens by delegating suitable work and reusing complete context and evidence, while preserving the review needed for a sound verdict. A request to save tokens does not create a blanket ban on justified Fable work. Explicit task restrictions still apply. The adapter remains text-only; existing rooms retain their pinned policy, provider settings and original routing unless the explicit [retained adoption procedure](docs/provider-transition.md) is completed. See [Fable's policy](skills/project-room/references/fable-policy.md) for diagnosis, escalation and verification duties.

Delegate detailed investigation and implementation before Fable recreates the same work. A native worker may consume a complete task artifact or DeepSeek export directly only when its existing permissions allow the read and it verifies the full bytes against the recorded identity and digest. File existence or a path in a prompt does not establish access. If the export cannot be read, the authorized root engineer uses bounded `deepseek_result` reads, validates the complete answer's digest, and supplies the necessary content through the supported worker input; do not have a native child bypass its denied DeepSeek tools or omit requirements. Return concise findings with changed-file, gate and evidence references, and retain the full artifacts for Fable's required review. Record the artifact/job identity, content digest, access method and evidence inspected in existing reporting text. Exact-spec review still requires Fable's own read-only judgment without delegates.

For an existing AO engineer session, author a routine continuation as only `Continue.` or the actual new instruction. Do not manually repeat the old specification, requirement references, routing policy, gates or summaries. Send actual requirement changes by themselves, preserving the supported exact-revision agreement process. Keep verification metadata in controller state and address a concrete context gap only when encountered. The controller sends the full spec and required workflow information once, then delivers only the caller's bytes for routine continuations. Older sessions receive genuinely undelivered workflow parts once. Actual spec revisions arrive as changes only, with new agreement information; existing request text and receipts remain unchanged. New delegates still need suitable self-contained task inputs.

AO does not by itself certify efficient Claude usage. Routine engineer continuations no longer repeat the specification or policy, but native history can still retain much larger tool outputs and earlier work. Deterministic packet reduction does not establish net subscription savings. Status/sync observation and operator-run gates need no model call. Cache reads, cache creation, current context, primary output and child usage have different meanings; include unknown usage and any compaction cost when comparing runs. New preparations configure Claude's native automatic compaction; the adapter never sends a compaction prompt or retries a turn to force it. Compaction does not restore subscription quota. Actual threshold behavior, continuity and useful work after compaction need live evidence under the [validation procedure](docs/context-compaction.md#verify-without-replaying-work).

## Normal workflow on AO

The skill walks Astra through these steps; the tool names are the `ao_room_*` MCP tools, which the CLI exposes with the same schemas.

1. `ao_room_open` with the actual project path, a stable feature name, the existing AO project ID, your implementation authorization and `delegate_provider` set to `deepseek` (or an intentionally selected `none`). A normal room without a usable selection is refused rather than downgraded; only a provider that `setup --delegate-provider` recorded deliberately fills in an omitted argument, so pass it explicitly. Reopening returns the same room. Save its `room_id`. An Astra-led exception passes `workflow: "astra_led"` and the per-task `exception_authorization` here; see [the exception and delivery rules](skills/project-room/references/ao.md#explicit-astra-exception-and-historical-pilot-rooms).
2. `ao_room_spec_put` with the exact UTF-8 spec content, a positive revision, Astra's approval text and nonempty argv gate arrays, for example `[["python3", "-m", "unittest", "discover", "-v"]]`. Revisions are immutable.
3. Prepare the engineer worktree before Claude launches (the hook above, or `ao_room_prepare` for the exact workspace). Create an ordinary AO Claude chat worker without an initial prompt, configure `claude-fable-5-1` at `max`, and `ao_room_bind` it as `engineer`. Bind a separate native Codex chat worker at the requested Astra model and `max` as `reviewer`. Ordinary bindings and the engineer workspace are immutable. A [single audited recovery](skills/project-room/references/ao.md#recovery-of-a-reviewer-that-has-never-been-used) is available only for a stopped reviewer that has never received a request; it preserves the original binding and review limits.
4. `ao_room_send` to the engineer with purpose `spec_review` and a stable `request_id`; `ao_room_sync` the completed turn. Fable's final JSON must carry `interpretation`, `findings`, `decision`, `spec_revision` and `spec_sha256`. Three spec reviews are available per room across revisions.
5. `ao_room_handoff` for the bound engineer worktree pins the agreement, baseline commit, candidate, authorization, delegate preparation and gates. Send purpose `implementation` once for that handoff; Fable directs implementation and delegates under the policy; the assigned executor leaves the candidate uncommitted unless a commit is part of the intended candidate, and returns a structured engineering report (`outcome`, `implementation_complete`, `changes`, `tests_reported`, `review_findings`, `remaining_gaps`, `backlog`, `routing_log` with `delegate_job_ids`, and the exact spec and baseline identity). Sync promptly so the candidate is captured. A confirmed completed turn may receive focused `correction` requests in the same session, each a new turn rather than a replay; a `scope_change` report returns to the spec.
6. `ao_room_verify` runs the pinned gates on the candidate worktree and binds the logs to the exact Git candidate before and after each gate.
7. `ao_room_send` to the reviewer with purpose `acceptance_review`; sync its terminal response; `ao_room_accept` records it only when the completed verdict names the unchanged spec, candidate and evidence hashes. Three acceptance reviews are available per room.

A completed response with valid JSON surrounded by prose can use [audited formatting recovery](skills/project-room/references/ao.md#completed-response-formatting-recovery): Astra reviews all surrounding text and records the exact object without a model call. Raw evidence and review limits remain intact; ambiguous content, stale candidates and missing completion evidence are refused. This does not approve the result or bypass independent acceptance.

Every step refuses rather than guesses: unknown delivery is never replayed, an AO failure without an observed native turn ID stays uncertain, an active or unresolved delegate job blocks the next phase, and exhausted review budgets surface the decision to you instead of opening another room. The full contract, including the exact result fields and the historical pilot rooms, is in [Project Room on Agent Orchestrator](skills/project-room/references/ao.md).

### CLI fallback

The installed plugin's controller exposes every `ao_room_*` and legacy `room_*` operation with the MCP schemas, which lets an existing task with an older tool inventory continue a room without a new conversation:

```sh
python3 project_room.py call ao_room_status --args '{"room_id":"ROOM_ID"}'
python3 project_room.py call ao_room_open --args-file /absolute/private/path/to/open-arguments.json
python3 project_room.py call ao_room_list --args '{}'
```

Replace `ROOM_ID` with the `room_id` returned by `ao_room_open`; AO room IDs already start with `ao-`. From another directory, use the absolute path of `project_room.py`. Prefer `--args-file` for multiline specs and review notes. `python3 project_room.py transcript-audit --room ROOM_ID --handoff HANDOFF_ID --attempt N` is a read-only tool-use count of one legacy implementation attempt's exact session transcript. The complete legacy tool reference is in [operations](skills/project-room/references/operations.md).

## Operate: status, sync, usage and version checks

- `ao_room_status` reads saved facts offline: bindings, the agreement, request states, verification and acceptance records, the primary usage subtotal, the configured model and any contradictory native reroute, delegate attachment, the bounded delegate job ledger and the routing state. `ao_room_list` returns at most 50 room records with explicit truncation. Historical acceptance in status does not attest current filesystem bytes.
- `ao_room_sync` makes bounded AO GET requests to reconcile owned turns and archive per-turn usage; it never invokes a model. Receipts are known only for an observed isolated turn on the same native conversation branch; anything else is reported as unknown, never zero. The usage total is a subtotal of known primary receipts, excludes delegates, and is neither subscription quota nor billing.
- **Stable-release check.** At the start of each new or resumed AO session, before the first model dispatch, compare the installed and running AO daemon with the official latest stable release, identify the actual daemon through its endpoint's `/healthz` executable path, and save the UTC time, versions, evidence and outcome privately in `PROJECT_ROOM_HOME/ao/version-check.json`. A fetch failure, rate limit or ambiguous local identity is recorded as unknown, never as up to date. A newer release is applied between jobs, with a backup and a read-only daemon smoke test, without restarting active workers, migrating rooms or replaying uncertain requests. The skill installs no background updater; AO's desktop app has its own updater, and any daily release monitor is something you configure separately. Details: [stable-release check](skills/project-room/references/ao.md#stable-release-check).

## Verification and acceptance versus publication

Verification runs the spec's argv gates directly, without a shell, with a pinned per-gate timeout (default 120 seconds, maximum 7200), in the candidate worktree. Gate programs are trusted project commands, not a sandbox; keep model calls and publication commands out of them. The fingerprint covers tracked and untracked committable files, deletions, symlinks, file modes, the index and HEAD, and is taken before the gates and after each gate, so a gate that changes candidate content invalidates the evidence immediately. Ignored files and external dependencies are outside it. Commit before final verification when a commit is part of the intended candidate; committing afterwards changes HEAD and needs fresh verification and review.

Acceptance validates an independent reviewer's completed JSON verdict against the unchanged spec, candidate and gate evidence. It does not merge, publish or deploy. Integration continues through normal repository tools under the scope you authorized, preserving unrelated changes and checking that the reviewed bytes are the ones integrated. A verifier that crashes before saving its receipt leaves a durable running record that blocks further mutation until you diagnose it; the adapter has no automatic verifier-recovery lane and never launches a replacement to bypass the uncertainty.

## Current validation limits

State what the evidence shows and no more:

- Offline tests establish the adapter contracts with fake model and MCP backends.
- Live local evidence on the installed AO setup has established the normal Fable/DeepSeek engineering workflow, independent Astra acceptance, and a native `pr-sonnet` plus `pr-opus` routing probe with no fallback and no nesting.
- A manual Claude `/compact` and one controlled interrupted-turn native stop and resume were validated separately on the installed AO setup: the same native session and candidate state, Fable at `max` retained, partial work retained, and the continuation applied exactly once.
- Those checks do not establish threshold-triggered automatic compaction, OS or daemon crash recovery, reconciliation of arbitrary or paid delegate interruptions, native-child Chrome access, or automatic Project Room recovery. Project Room has no audited continuation operation for interrupted or uncertain AO requests; diagnose them from `ao_room_status` and `ao_room_sync` without resending, following [the delivery and uncertainty rules](skills/project-room/references/ao.md#explicit-astra-exception-and-historical-pilot-rooms).
- Routing states describe local configuration and observation, not enforcement. The prepared engineer's settings deny the automated review skills, and its guard refuses every other skill dispatch except the pinned browser skill inside `pr-opus`.
- A smaller context reading does not restore subscription quota; live receipts stay private and describe exactly what they verified.

## DeepSeek delegate

The DeepSeek lane serves AO rooms and legacy rooms alike. Its key-free configuration selects exactly one fixed TLS transport: `official` uses `https://api.deepseek.com/chat/completions`; `deepinfra` uses `https://api.deepinfra.com/v1/openai/chat/completions`. There are no arbitrary endpoints, redirects or automatic provider/model fallbacks. New DeepInfra profiles default to `deepseek-ai/DeepSeek-V4.1-Flash`, `max` effort, 131,072 output tokens and an advertised 1,048,576 context window. Health and probe receipts distinguish the backend, endpoint, requested settings and observed model; hosted capacity, reasoning behavior and quality require their own evidence. Existing rooms retain their recorded settings.

DeepInfra's [data policy](https://docs.deepinfra.com/account/data-privacy) says inference data is not used for training and inputs/outputs are normally deleted after processing, with exceptions for debugging or security logging. This is not an unconditional zero-retention guarantee. The adapter sends no batch, webhook or explicit prompt-retention option; [automatic provider caching](https://docs.deepinfra.com/chat/prompt-cache-retention) is a separate behavior.

The adapter executes nothing, edits no files and calls no tools; the designated native worker applies and verifies its proposed changes under Fable's direction. Each room's status includes the latest 20 jobs from its private ledger, and admission checks use the full ledger. The one-request live probe is an explicit paid CLI action that takes the room's own snapshot directory. For a legacy room:

```sh
python3 deepseek_adapter.py probe --home ~/.project-room --room ROOM_ID --room-root ~/.project-room/rooms/ROOM_ID --config ~/.project-room/rooms/ROOM_ID/profiles/deepseek.json
```

For an AO room, whose `ROOM_ID` already starts with `ao-`, the snapshot lives under the AO state directory:

```sh
python3 deepseek_adapter.py probe --home ~/.project-room --room ROOM_ID --room-root ~/.project-room/ao/rooms/ROOM_ID --config ~/.project-room/ao/rooms/ROOM_ID/profiles/deepseek.json
```

An unresolved delivery failure stops new paid jobs in that room only; `deepseek_adapter.py resolve` at your own terminal is the only way to clear it, and no MCP tool or agent performs it. Setup, the key file, the state table, calibration order, export folders and privacy limits are in [the DeepSeek delegate guide](docs/deepseek.md).

## Legacy controller

The Codex Astra/Fable controller predates the AO adapter and remains fully supported for existing rooms and for users without AO. Keep its properties separate from AO's:

- **Setup and use.** `python3 project_room.py setup` and `doctor` as above, then `room_open`, `room_spec_put`, `room_review_submit`, `room_handoff`, `room_implementation_submit`, `room_implementation_review` and the other `room_*` tools drive the same spec, review, handoff and acceptance loop. Review and implementation calls return job IDs promptly; follow them with bounded `room_job_status` waits, and resume from saved status and history after a disconnect. Implementation needs a clean Git checkout, creates an isolated `codex/implementation-*` worktree, and leaves the candidate uncommitted. See [operations](skills/project-room/references/operations.md).
- **Status and progress.** Each job carries a read-only `progress` object (phase, elapsed time, last observed activity, delegates, and a countdown to the pinned model or gate timeout) plus a worker heartbeat and recent activity; none of it is proof of useful work or completion. See [progress](docs/progress.md) and [status and heartbeat details](docs/status-followups.md).
- **Timeouts.** The legacy controller pins a model-invocation timeout and gate timeouts per job. AO adds no Project Room primary model-run deadline: a native turn runs until it finishes or AO stops it, while verification gate timeouts, DeepSeek request limits, provider and account limits and the model's context window still apply.
- **Recovery.** Legacy audit and recovery lanes handle two exact shapes. For a job stopped only by the model-invocation timeout or the provider's session-usage-limit error, `room_implementation_audit` and `room_implementation_recover` call no model and prepare an immutable continuation record only after a host restart that postdates the original failure; `room_implementation_submit` then launches the one authorized Fable successor, a new job that still faces fresh gates and Astra acceptance. For a completed model result followed by a gate timeout, `room_verification_audit` calls no model and `room_verification_retry` reruns only the pinned gates, never the model, in an isolated copy with a private `TMPDIR`, and requires your separate explicit authorization to rerun those exact gates. Both lanes require every identity and evidence value to match a fresh check and keep the original job unchanged. Each review round allows three Fable reviews, continued by a recorded user decision. See [continuation and recovery](docs/recovery.md).
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

## Private state

The default data directory is `~/.project-room`; set `PROJECT_ROOM_HOME` to use another private directory and keep it outside the source checkout and the installed plugin cache so reinstalling never replaces your rooms. It holds `config.json`, `registry.sqlite3` and `rooms/<id>/` for the legacy controller; `ao/config.json`, `ao/rooms/<id>/`, `ao/launchers/` (the content-addressed DeepSeek launcher and routing guard) and `ao/version-check.json` for AO; and, for the DeepSeek delegate, `deepseek/` (the shared `ledger.sqlite3` with room-scoped rows, per-job artifacts, per-room `exports/` and probe receipts) plus the separate default key files `secrets/deepseek-api-key` and `secrets/deepinfra-api-key`. Room directories contain spec revisions, receipts, verification logs and acceptance records. A room's export directory holds one answer file per job, named by job ID and checked against its recorded digest on every read.

Never publish local paths, session UUIDs, transcripts, receipts, keys or usage material from that directory. The distributable source is the controller, MCP server, adapter, skill, guards, tests and templates; `.gitignore` excludes the common private forms, and staged changes should be inspected before sharing.

## Development and testing

Run the automated suite from the checkout:

```sh
python3 -m unittest discover -v
```

Tests use fake model executables and fake MCP backends in temporary directories, including a fake loopback HTTPS/SSE DeepSeek connection with synthetic keys and a fake AO transport; they make no account, network, paid-model or GPU requests. CI runs the suite on macOS natively and on Linux inside a restricted `python:3.11-bookworm` and `python:3.12-bookworm` container with no network, a read-only root and source, no capabilities, a non-root user and a tmpfs `/tmp`. The container runs with `--init` because the verification lane's process inspection is fail-closed: without an init process, orphaned test processes become zombies whose `/proc/<pid>/environ` is root-owned, and the marker scan correctly refuses to launch anything. To reproduce locally, run the same `docker run` line from `.github/workflows/tests.yml` against a `git archive` of the candidate.

A passing fake-backend suite does not establish live authentication, Fable access, DeepSeek or Qwen health, AO reachability, or a real feature implementation. Validate a changed skill or manifest with the Codex plugin validator before installing it. The module map is in [architecture](docs/architecture.md); `room.py` is the low-level review engine whose `examples/config.example.json` and `examples/policy.example.md` describe that engine's read-only review session rather than the full plugin workflow.
