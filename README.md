# Project Room

Project Room gives Astra and Fable a shared feature workflow inside Codex. Tell Astra what you want; Astra shapes the requirements, Fable challenges the specification and implements the agreed feature, and Astra independently checks the product outcome. Specs, findings, decisions, jobs, and acceptance stay in a persistent local room.

After installing and setting up the plugin, use it in ordinary language:

> Use Project Room for this feature: let users save and name their search filters.

The bundled skill supplies the roles and handoff workflow. You do not need to paste an orchestration prompt each time. A request to build a feature carries through implementation; a planning-only request stops at the agreed specification. Existing authorization carries forward.

## What each participant owns

| Participant | Responsibility |
| --- | --- |
| You | Product intent, priorities, and meaningful tradeoffs. |
| Astra | Brainstorming, requirements, acceptance criteria, decisions, and independent product outcome review. |
| Fable | Technical interpretation, engineering design, implementation planning, delegate orchestration, and engineering verdicts. |
| Qwen / Sonnet / Opus | Bounded delegated work, with output checked by Fable. Availability depends on your setup. |

Astra and Fable review the same immutable spec revision and digest. Findings receive explicit dispositions and reasons. Both agents actively suggest useful enhancements; Astra brings you the benefit, tradeoff, and a project GitHub issue link for your opinion and approval. Proposals remain tracked in the room, and filing is reported as pending if no tracker is available. An enhancement enters implementation only after you approve its scope and the revised spec is agreed. Consensus permits handoff when implementation is within your request. Fable then works against executable gates, and Astra records acceptance only after inspecting the delivered behavior and evidence. See [the workflow](docs/workflow.md).

## Install and configure

Requirements:

- Python 3.10 or newer on macOS or Linux. Runtime uses POSIX locks and process groups; native Windows is not supported.
- Codex with local plugin/MCP support for the integrated experience, or a terminal for the controller CLI.
- Claude Code with access to the configured primary model. The default is Fable 5.1 (`claude-fable-5-1`) at maximum effort.
- Claude Code's normal saved subscription authentication. A signed-in Claude Desktop Code tab does not necessarily authenticate the standalone CLI.

Clone or obtain this repository, then ask Codex to install it as a local plugin using the `plugin-creator` workflow. Keep the folder name `astra-fable-project-room`. That workflow can register a personal marketplace entry and install the bundle. Local marketplace distribution is separate from publication in a public plugin directory. The source repository is [steven5210/astra-fable-project-room](https://github.com/steven5210/astra-fable-project-room).

From the plugin checkout, run setup once:

```sh
python3 project_room.py setup
python3 project_room.py doctor
```

If Claude is not found, pass its actual executable path:

```sh
python3 project_room.py setup --claude-bin /absolute/path/to/claude
```

Authenticate through Claude Code's standard login flow when needed, then rerun doctor. The controller uses the CLI's saved login and preserves its original configuration-directory override; it does not ask for an API key, extract credentials, or silently switch providers. Model calls still count toward the account's applicable usage. Setup and doctor are distinct from making a model request. See [authentication recovery](docs/recovery.md) if a saved job failed before reaching the model.

The default data directory is `~/.project-room`; set `PROJECT_ROOM_HOME` to use a different private directory. It contains `config.json`, `registry.sqlite3`, and `rooms/<id>/`. Keep it outside the source repository and installed plugin cache so reinstalling the plugin does not replace your rooms.

The plugin starts a local stdio MCP server with `python3 ./project_room_mcp.py` and `cwd: "."`. This follows the portable pattern in OpenAI's [bundled local MCP example](https://github.com/openai/plugins/blob/main/plugins/openai-developers/.mcp.json). It requires no hosted room server or exposed listening port. After installing or updating, test in a new Codex task so the current skill and tool definitions load.

## Use the room

Astra opens or reuses one room for the project path and feature, records intent, writes a spec, and submits Fable's independent review. Review and implementation calls return job IDs promptly. Bounded status waits follow the job; the room retains results across process restarts.

Implementation requires a clean Git checkout with a commit to use as its baseline. Preserve existing changes deliberately before handoff; the plugin does not automatically commit or discard them. It creates an isolated `codex/implementation-*` worktree and leaves the candidate uncommitted for review. Acceptance records the reviewed candidate; that controller operation does not merge, push, deploy, or apply it to another checkout. When you requested a finished feature, the Astra skill continues integration through normal repository tools, preserving unrelated changes and verifying the integrated result. Publishing follows the scope you authorized.

Once the exact current spec is accepted by both agents and blocking findings are resolved, Astra records a handoff with the user's existing authorization and executable validation gates. Fable implements and owns engineering review. A material scope discovery creates a blocking issue that requires a newer spec revision and renewed agreement before handoff. Astra checks the result and records either acceptance or actionable rejection.

The worker can continue after the MCP connection closes. The plugin does **not** wake an idle Astra conversation: ask to resume the room, and Astra reads its saved status and history. A background job finishing is distinct from gates passing and Astra accepting the result. Gates run independently in the implementation worktree, and acceptance binds the candidate content they checked. A gate that changes candidate files invalidates that evidence; use verification commands that leave source unchanged.

Existing app conversations retain their own history. Rooms start dedicated Claude sessions and resume their saved UUIDs; they do not automatically import or synchronize your exact Codex/Claude Desktop transcripts. Relevant decisions and context are recorded explicitly in the room.

## CLI fallback

The controller exposes the same 18 operations as MCP. From the plugin directory:

```sh
python3 project_room.py call room_open --args '{"project_path":"/absolute/path/to/project","feature":"Saved filters"}'
python3 project_room.py call room_list --args '{}'
python3 project_room.py call room_status --args '{"room_id":"ROOM_ID"}'
python3 project_room.py call room_history --args '{"room_id":"ROOM_ID"}'
```

Replace `ROOM_ID` with the value returned by `room_open`. From another directory, use the absolute path to `project_room.py`. Prefer structured MCP arguments or `call TOOL --args-file /absolute/private/path/to/arguments.json` for multiline specs and review notes. The complete tool reference is in [operations](skills/project-room/references/operations.md).

## Qwen and delegation

Qwen is optional; Fable orchestrates the available delegates without lowering the quality bar. To connect an existing trusted `qwen-local` stdio server during setup:

```sh
python3 project_room.py setup --qwen-config /absolute/private/path/to/qwen-config.json
```

Use the server's existing configuration. Do not commit credentials or machine-specific paths to the plugin. The Qwen guard can consume either one `{command, args, env}` server definition or a full configuration containing `mcpServers.qwen-local`.

| Tool | Guard policy |
| --- | --- |
| `qwen_submit` | `effort="xhigh"`, `max_tokens=131072`; omissions receive these values and deviations are rejected before forwarding. |
| `qwen_ask` | Effort `none` or `low`; default `low`. |
| `qwen_status` | `wait=true`; positive finite timeout no greater than 49 seconds; default 45. |

The intended Qwen3.8-27B server window is 262,144 tokens: 131,072 for prompt/context/system and 131,072 for thinking plus answer. The upstream server owns the prompt-size precheck; the guard does not tokenize inputs or set the server window. Use `context_path` for large context and chain bounded status waits. The complete [Fable policy](skills/project-room/references/fable-policy.md) specifies routing, diagnosis, escalation, and verification.

Tool discovery, health, and successful inference are different checks. Installation does not prove an upstream server is reachable or has the intended model/window. If a configured endpoint is unavailable, report the actual evidence and apply the routing policy; do not weaken Qwen's settings or claim a delegate ran when it did not.

## Reliability and verification limits

The controller preserves exact spec binding, request IDs, session identity evidence, and durable outcomes. It prevents accidental duplicate model submission and blocks uncertain delivery. Do not delete state, reuse a request ID with changed content, or create a replacement room to evade a blocked attempt. Each review round allows three Fable reviews. If further debate is needed, Astra brings you a focused product decision; recording your answer permits the next bounded round while retaining every prior attempt. Agreement can proceed directly to handoff. See [continuation and recovery](docs/recovery.md).

The bundled skill drives Astra's reasoning and independent product review. Automated gates prove their own checks, not every aspect of product quality. Fable's delegate choices and engineering judgments must remain reviewable in its result. The package does not certify model quality, install local inference, or assume every Claude session supports subagents.

Run the automated suite:

```sh
python3 -m unittest discover -v
```

Tests use fake model executables and fake MCP backends in temporary directories. They make no account, network, paid-model, or GPU requests. CI runs discovery on Linux and macOS with Python 3.11 and 3.12. A passing fake-backend suite does not establish live authentication, Fable access, Qwen health, or complete a real feature implementation. Keep live verification receipts private and describe exactly what they verified.

## Source and private data

The distributable source includes the controller, MCP server, skill, policy guard, tests, and templates. Runtime configuration, databases, attempts, transcripts, worktrees, session paths, and review receipts belong in private local state. `.gitignore` excludes common forms; inspect staged changes before sharing.

`room.py` supplies the low-level spec-review engine, including audited identity reconciliation and recovery for proven local authentication failures. Its `examples/config.example.json`, `examples/policy.example.md`, and related review fixtures describe that engine's read-only review session, not the full plugin's implementation workflow. Use `project_room.py setup` for normal plugin setup; consult [architecture](docs/architecture.md) when developing or diagnosing the underlying engine.
