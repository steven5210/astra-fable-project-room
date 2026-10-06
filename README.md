# Project Room

Project Room turns a feature request into a versioned specification, an engineering handoff, executable verification evidence and an independent acceptance record, all kept in persistent private rooms outside the repository. It runs on two layers:

- **Stock [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) (AO)** is the recommended native session and worktree layer. AO owns the Claude Code and Codex chat workers, their isolated Git worktrees, conversations, project hooks and project rules. No AO fork and no Paperclip are required.
- **Project Room** is the specification, handoff, evidence and acceptance layer. Its `ao_room_*` tools pin immutable spec revisions and gates, send each request once with a durable identity, prepare private delegates before the engineer launches, archive per-turn usage receipts, run the agreed gates against the exact candidate, and accept only an independent reviewer verdict that names the same spec, candidate and evidence hashes.

The earlier Astra/Fable controller inside Codex (the `room_*` tools and `project_room.py` CLI) remains available as the legacy backend. Existing legacy rooms keep their recorded backend, exact revisions, session identity and pinned delegate provider; nothing migrates automatically. See [Legacy controller](docs/reference/legacy-controller.md#legacy-controller) below.

An existing normal AO room can make one explicitly authorized DeepInfra-to-official
provider amendment and adopt v2 routing while retaining its native history and
review counters. The [retained adoption procedure](docs/operations/provider-transition.md)
requires stopped-session audits, immutable evidence and verified native MCP
initialization before new work. Changing setup defaults alone does not do this.

After installing and configuring the plugin, ask for the work in ordinary language:

> Use Project Room for this feature: let users save and name their search filters.

The bundled [skill](skills/project-room/SKILL.md) supplies the roles and workflow; you do not paste an orchestration prompt each time. A request to build a feature carries through implementation; a planning-only request stops at the agreed specification. Existing authorization carries forward, and merge, publication and deployment stay outside the room unless you asked for them.

## What each participant owns

See [Roles, agreement and delegation](docs/guides/roles-and-delegation.md).

## Prerequisites

- Python 3.10 or newer on macOS or Linux. The runtime uses POSIX locks, process groups and `/proc` or `ps` inspection; native Windows is not supported. CI exercises Python 3.11 and 3.12.
- Git, with the project you want to work on cloned locally and clean at the commit that will become the handoff baseline.
- Claude Code with its normal saved subscription login and access to the room's configured engineering selector (the `fable` family by default, or the room's explicitly selected qualified family or exact model). Qualification pins an expected model from retained source evidence; it is never a claim that a provider, account or quota is available. A signed-in Claude Desktop tab does not necessarily authenticate the standalone CLI.
- Codex with local plugin and MCP support for Astra and for the legacy controller.
- For AO work: a running, trusted local AO daemon at the official latest stable release, reachable on a loopback address, with the same native authentication AO already uses for its workers. Installing the daemon, adding a project and creating chat workers follow [AO's own documentation](https://github.com/OrchestratorInc/agent-orchestrator). The adapter was initially exercised against AO 0.12.12; that is a validation fact, not a pin.
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

`setup` accepts `--claude-bin`, `--qwen-config`, `--deepseek-config` and `--delegate-provider deepseek|qwen|none`. When `--claude-bin` is given, setup verifies the executable — an executable regular file under 1 GB whose `--version` reports a Claude Code version — and retains a copy under `PROJECT_ROOM_HOME/claude-code/<version>-<sha256[:12]>/claude`, created once: identical bytes are reused and different bytes under the same name refuse. The retained path is pinned as `claude_bin` and the given path is recorded as `claude_bin_source`, so the managed installer's pruning or replacement cannot break prepared or future rooms; `doctor` reports whether the pinned executable is retained or installer-managed. An existing configuration keeps its installer-managed path until the next explicit `setup --claude-bin`; nothing is migrated. Authenticate through Claude Code's standard login flow when needed, then rerun `doctor`. The controller uses the CLI's saved login and preserves its original `CLAUDE_CONFIG_DIR` override; it never asks for an API key, extracts credentials or silently switches providers. Model calls still count toward the account's applicable usage. After installing or updating, open a new Codex task so the current skill and tool definitions load; an existing task keeps its older tool inventory and can continue through the [CLI fallback](docs/guides/ao-setup.md#cli-fallback).

## Configure the private AO backend

See [AO backend setup and normal workflow](docs/guides/ao-setup.md).

## Roles, agreement and delegation

See [Roles, agreement and delegation](docs/guides/roles-and-delegation.md).

## Normal workflow on AO

See [AO backend setup and normal workflow](docs/guides/ao-setup.md).

### CLI fallback

See [AO backend setup and normal workflow](docs/guides/ao-setup.md#cli-fallback).

## Operate: status, sync, usage and version checks

- `ao_room_status` reads saved facts offline: bindings, the agreement, request states, verification and acceptance records, the primary usage subtotal, the configured model and any contradictory native reroute, delegate attachment, the bounded delegate job ledger and the routing state. `ao_room_list` returns at most 50 room records with explicit truncation. Historical acceptance in status does not attest current filesystem bytes.
- `ao_room_sync` makes bounded AO GET requests to reconcile owned turns and archive per-turn usage; it never invokes a model. Receipts are known only for an observed isolated turn on the same native conversation branch; anything else is reported as unknown, never zero. The usage total is a subtotal of known primary receipts, excludes delegates, and is neither subscription quota nor billing. While an owned turn is submitted or running, call `ao_room_sync` with `wait_seconds=45` and repeat bounded waits instead of immediate polling; a timeout is not a stall, failure or permission to replay. `settled` is true only when every owned request is terminal; an `uncertain` request still needs sync/recovery, never replay.
- **Prompt and evidence diagnostics.** Saved status includes `latest_prompt`: exact new-request UTF-8 component counts, separate receipt integrity and delivery coverage, and explicit null component counts for legacy requests. Native worker usage remains unavailable rather than inferred in ordinary status. The standalone `ao-evidence-read-audit` counts supported logged `Read` requests; `ao-native-usage-audit` explicitly attributes native parent, worker and API delegate usage for one owned request. See [diagnostic fields, CLI and limits](docs/reference/diagnostics.md).
- **Stable-release check.** The controller's `ao_release_check` performs the check once before the first model dispatch for each new or resumed AO session. It compares the running daemon with the official latest stable release and saves private evidence in `PROJECT_ROOM_HOME/ao/version-check.json`; `up_to_date`, `update_available`, `mismatch` and `unknown` are distinct outcomes, and unknown is never up to date. A newer release is applied between jobs, with a backup and a read-only daemon smoke test, without restarting active workers, migrating rooms or replaying uncertain requests. The skill installs no background updater; AO's desktop app has its own updater, and any daily release monitor is something you configure separately. Details: [stable-release check](skills/project-room/references/ao.md#stable-release-check).

## Verification and acceptance versus publication

See [Verification, acceptance and validation limits](docs/reference/validation-limits.md).

## Current validation limits

See [Verification, acceptance and validation limits](docs/reference/validation-limits.md).

## DeepSeek delegate

See [DeepSeek delegate quick reference](docs/reference/deepseek-quickstart.md).

## Legacy controller

See [Legacy controller](docs/reference/legacy-controller.md).

### Qwen delegation (legacy)

See [Legacy controller](docs/reference/legacy-controller.md#qwen-delegation-legacy).

## Deterministic change-set application

The standalone [change-set tool](docs/reference/changesets.md) plans and mechanically applies approved JSON or anchored-text edits, with exact source pins, copy-and-fill receipt templates, preserved metadata and explicit interrupted-run audit/resume. It runs no models or project checks. Mechanical completion remains separate from engineering review and acceptance. State and input directories must stay private and outside the candidate; unsupported metadata or durability capabilities refuse before target replacement.

## Private state

The default data directory is `~/.project-room`; set `PROJECT_ROOM_HOME` to use another private directory and keep it outside the source checkout and the installed plugin cache so reinstalling never replaces your rooms. It holds `config.json`, `registry.sqlite3` and `rooms/<id>/` for the legacy controller; `ao/config.json`, `ao/rooms/<id>/`, `ao/launchers/` (the content-addressed DeepSeek launcher and routing guard) and `ao/version-check.json` for AO; and, for the DeepSeek delegate, `deepseek/` (the shared `ledger.sqlite3` with room-scoped rows, per-job artifacts, per-room `exports/` and probe receipts) plus the separate default key files `secrets/deepseek-api-key` and `secrets/deepinfra-api-key`. Room directories contain spec revisions, receipts, verification logs and acceptance records. A room's export directory holds one answer file per job, named by job ID and checked against its recorded digest on every read.

Never publish local paths, session UUIDs, transcripts, receipts, keys or usage material from that directory. The distributable source is the controller, MCP server, adapter, skill, guards, tests and templates; `.gitignore` excludes the common private forms, and staged changes should be inspected before sharing.

## Development and testing

Run the automated suite from the checkout:

```sh
python3 -m unittest discover -v
```

Tests use fake model executables and fake MCP backends in temporary directories, including a fake loopback HTTPS/SSE DeepSeek connection with synthetic keys and a fake AO transport; they make no account, network, paid-model or GPU requests. CI runs the suite on macOS natively and on Linux inside a restricted `python:3.11-bookworm` and `python:3.12-bookworm` container with no network, a read-only root and source, no capabilities, a non-root user and a tmpfs `/tmp`. The container runs with `--init` because the verification lane's process inspection is fail-closed: without an init process, orphaned test processes become zombies whose `/proc/<pid>/environ` is root-owned, and the marker scan correctly refuses to launch anything. To reproduce locally, run the same `docker run` line from `.github/workflows/tests.yml` against a `git archive` of the candidate.

A passing fake-backend suite does not establish live authentication, Fable access, DeepSeek or Qwen health, AO reachability, or a real feature implementation. Validate a changed skill or manifest with the Codex plugin validator before installing it. The module map is in [architecture](docs/reference/architecture.md); `room.py` is the low-level review engine whose `examples/config.example.json` and `examples/policy.example.md` describe that engine's read-only review session rather than the full plugin workflow.

## Documentation

The index of all documents is [docs/README.md](docs/README.md).

- `docs/guides/` — procedures: workflow, continuations, model transitions and review extensions.
- `docs/reference/` — contracts and references: architecture, connectors, providers, diagnostics and validation limits.
- `docs/operations/` — runbooks: recovery and provider adoption.
