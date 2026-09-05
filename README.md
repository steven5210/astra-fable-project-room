# Astra–Fable project room

A local command-line bridge for a specification review between Astra and Fable. Astra records a feature spec, Claude Code reviews it in a persistent Fable session, and the room records the discussion and agreement on an exact spec revision. This removes the manual copying between the two agents when Astra has permission to run the CLI.

The room stores immutable spec bytes, messages, approvals, requests, results, and identity evidence locally. It starts its own Claude session and resumes that session by UUID. It does not import an existing app conversation, run continuously in the background, or authorize implementation after a review.

## Requirements

- Python 3.10 or newer on macOS or Linux. The room uses POSIX `fcntl` locking and process groups; native Windows is not supported.
- A standalone Claude Code CLI with access to the exact model configured for Fable. The example requests `claude-fable-5-1` at maximum effort.
- A saved Claude subscription login. Authenticate through the CLI's normal login flow and confirm the account using `claude auth status`. This tool neither reads credentials nor switches to API billing.

No Python dependencies are required. A Codex integration is not embedded in the program: Astra, another authorized agent, or a person invokes the commands below. The optional Qwen proxy requires an existing `qwen-local` stdio MCP server.

## Set up private local configuration

Copy the templates from a clone of this repository:

```sh
cp examples/config.example.json local-config.json
cp examples/mcp.empty.example.json local-mcp.json
```

Edit `local-config.json` before initializing a room:

1. Replace `claude_bin` with the absolute path of your Claude Code executable.
2. Replace the MCP and policy paths in `extra_args` with existing absolute paths in your checkout. The policy template can remain at `examples/policy.example.md`, or you can make a private local copy.
3. Confirm the requested model and its exact reported identity. The example pins both to `claude-fable-5-1`. If your account does not provide this model, resolve that with the user; the bridge never chooses a fallback model.

The example disables ambient setting sources, hooks, slash commands, and permission prompts. It enables only `Read`, `Glob`, and `Grep`, and loads an empty strict MCP configuration. The room also instructs Fable to review only: no implementation, builds, tests, delegation, or Qwen calls during a review.

The normalized configuration and contents of referenced policy/MCP/settings files are pinned at initialization. Changing them later prevents further calls in that room. Choose the configuration before a review starts; do not replace a room to bypass an unresolved delivery outcome.

Nonempty inherited `ANTHROPIC_*` variables and known Claude provider/auth overrides are rejected before a model call. Run with those overrides unset to use the saved subscription login. The diagnostic reports variable names, never their values. In a sandbox, use the normal authorized execution path for Claude's keychain, session storage, and network access.

## Review a spec

Initialize one room and register a UTF-8 spec:

```sh
python3 room.py --room rooms/example init --config local-config.json
python3 room.py --room rooms/example spec --revision 1 --file examples/spec.example.md
python3 room.py --room rooms/example status
```

`init` generates and saves the session UUID before the first model call. The spec digest covers its exact UTF-8 bytes, including line endings and final newlines. Re-registering the same bytes is harmless; changing an existing revision is rejected.

Use a unique request ID for a new review:

```sh
python3 room.py --room rooms/example ask \
  --revision 1 \
  --message-file examples/review-request.example.md \
  --request-id spec-v1-review \
  --session-transcript /absolute/private/path/to/SESSION-UUID.jsonl
```

Replace the transcript argument with the exact session JSONL path used by your installed Claude Code for this room. Its filename must be the UUID reported by `status`, followed by `.jsonl`. The CLI runs with the room directory as its working directory. Claude commonly stores session files beneath its private projects directory; confirm the actual location for your installation rather than selecting a recent unrelated conversation. For the first turn, the path may be the location where Claude will create that UUID's file.

`--session-transcript` is optional when the result reports exactly the expected model. When Claude reports auxiliary model usage, it is required to prove which model produced the review. The bridge checks the last `StructuredOutput` call in the attempt's time window, exact returned payload, session ID, and primary model. It records primary and auxiliary usage separately. It never searches other sessions or exposes thinking content. Missing evidence blocks the room; supplying evidence later uses the narrow recovery command below, without resubmission.

Fable returns a structured interpretation, findings, `accept` or `changes_required`, and the exact revision and SHA-256. Astra evaluates those findings, writes a revised spec if needed, registers a higher revision, and asks again with a new request ID. Each invocation is a new CLI process; successful subsequent calls resume the saved Claude UUID.

Record Astra's approval only after its review of the current spec. First create a local UTF-8 note explaining that decision:

```sh
python3 room.py --room rooms/example record \
  --sender astra --kind approval --revision 1 --file /absolute/private/path/to/approval.md
python3 room.py --room rooms/example status
python3 room.py --room rooms/example transcript --file transcripts/example.md
```

Use the actual current revision in these commands. Agreement requires an Astra approval and the latest verified Fable acceptance of the same revision and digest, with no unresolved turns. A new revision invalidates the previous agreement. Agreement does not start implementation. The CLI can also record user/Astra messages with `record --kind message`.

## Delivery, limits, and recovery

- A completed request ID with the identical payload returns its saved result without another model call. Different content under the same ID is rejected.
- An operating-system lock permits one room mutation at a time. The bridge never invokes Claude through a shell.
- Failed, interrupted, timed-out, malformed, or otherwise uncertain calls block further calls. Raw attempt output and the original request remain available for inspection. Supervised interruption terminates the child process group; an orphaned pending record is made uncertain on the next submission attempt.
- The pilot caps a room at **three new review attempts**. Cached duplicate reads do not count. Bring unresolved decisions to the user when this limit is reached; the CLI has no automatic debate loop or cap reset.
- The example allows 1,800 seconds per call, with maximum model effort unchanged. An explicit `ask --timeout SECONDS` can change only that invocation's wall-clock limit. Timeout values must be positive and finite.

The `reconcile` command has one narrow purpose: a saved successful terminal response rejected solely for mixed-model identity can be revalidated against its explicit session transcript:

```sh
python3 room.py --room rooms/example reconcile \
  --request-id spec-v1-review \
  --session-transcript /absolute/private/path/to/SESSION-UUID.jsonl \
  --note-file /absolute/private/path/to/reconciliation-note.md
```

The note must explain the evidence examined. Reconciliation runs no subprocess or model, preserves raw output and the original failure in an audit record, and checks the original attempt's model, session, structured result, and time window. It cannot approve a nonzero process exit, malformed reply, wrong session/spec, or unknown delivery. A specific legacy verifier error permits a documented zero-exit-code inference; the audit distinguishes that inference from a measured return code. Do not edit the database, delete failure records, or create a replacement request to bypass these checks.

## Optional Qwen guard

`qwen_guard.py` is a stdio MCP proxy for an existing `qwen-local` server. It does not install Qwen, select its model, change its server window, or establish inference health. The server must separately provide the intended Qwen3.8-27B model and a 262,144-token window.

The guard uses the installed tool schema's `effort` field:

| Tool | Enforced parameters |
| --- | --- |
| `qwen_submit` | `effort="xhigh"`, `max_tokens=131072`; omissions receive these values and conflicting values are rejected. |
| `qwen_ask` | `effort="none"` or `"low"`; default `"low"`. |
| `qwen_status` | `wait=true`, finite positive `timeout_s` no greater than 49; default 45 seconds. |

The intended prompt budget is 131,072 tokens for task, context, and system text, leaving 131,072 for thinking plus answer. The upstream server owns the prompt-size precheck; this proxy does not tokenize prompts or verify the inference server's configured window. Use `context_path` for large contexts, and chain bounded waits for long jobs.

To configure the proxy, copy `examples/qwen-upstream.example.json` to a private `qwen-upstream.local.json` and replace its command/arguments with those of your existing MCP server. Copy `examples/mcp.qwen-review.example.json` to `local-mcp.json`, replace every absolute-path placeholder, and keep credentials in your normal private server configuration.

The default review configuration still denies Qwen tool calls. If a reviewed setup needs only a connectivity probe, add `mcp__qwen-local__qwen_health` to the allowed tools before room initialization; do not allow the generating tools in this review pilot. A tool-list or health response alone does not prove successful inference. Future Fable implementation orchestration is a separate authorized workflow; the policy template records the intended delegate rules but this pilot does not execute them.

## Tests

```sh
python3 -m unittest -v test_room test_qwen_guard
```

Tests use fake Claude executables and fake MCP backends in temporary directories. They check restart/resume, exact spec binding, duplicate suppression, stale approvals, primary-producer evidence, failure handling, reconciliation, process cleanup, and Qwen policy enforcement. They use no accounts, model calls, GPU jobs, or network access. CI runs the same suite on Linux and macOS with Python 3.11 and 3.12.

## Local data

Keep room databases, attempts, session paths, configuration, credentials, transcripts, and review receipts private. The included `.gitignore` excludes common local files and folders; inspect the staged diff before every commit. Share only deliberately prepared artifacts. This repository contains reusable source, fake fixtures, and templates, with no live conversation history or account data.
