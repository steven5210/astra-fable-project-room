# Text delegate profiles

`text_delegate.py` is a standalone, profile-driven MCP text delegate. It has no controller-module or third-party
runtime dependencies. Its six tools are `delegate_submit`, `delegate_ask`, `delegate_status`, `delegate_result`,
`delegate_cancel` and `delegate_health`. Returned text is never executed.

This implementation covers issue #137 steps 1–2 only. A profile is not selectable for rooms until the controller
wiring in step 3 is implemented.

## Profile file

Pass an absolute path to a key-free JSON profile with `--config`. Profiles use `profile_version: 1` and require
`id`, `transport`, `model`, `request_syntax`, `auth` and `limits`. The ID matches
`[a-z0-9][a-z0-9._-]{0,63}`. An optional printable `label` of 1–120 characters defaults to the ID. Unknown fields
are rejected at every level. Field names containing “key” are rejected except for `auth.api_key_file`; the profile
stores only a path, never credential material.

`transport` has exactly `kind`, `host`, `port` and `path`. For `https`, the host is a lowercase DNS name with at
least two valid labels, never an IP literal; TLS certificate and hostname verification remain enabled. For
`loopback_http`, the host is a loopback IP literal such as `127.0.0.1`, not a hostname, and the connection uses
plain HTTP. Both kinds require an explicit integer port from 1 through 65535. The path begins with `/`, is at most
256 printable ASCII characters, and contains no whitespace, query or fragment.

`model` matches `MODEL_ID` or `NAMESPACED_MODEL_ID`. A streamed response is accepted only when its model matches
the configured value byte-for-byte. `request_syntax` is `deepseek_thinking`, `openai_reasoning_effort` or
`openai_plain`. The first two require `reasoning_effort` (`low`, `high` or `max`). `openai_plain` forbids that
field. Optional `ask_effort` defaults to `low` for reasoning syntaxes and to `none` for `openai_plain`; plain
profiles permit only `none`.

`auth` is exactly one of:

```json
{"kind": "bearer_file", "api_key_file": null}
```

or:

```json
{"kind": "none"}
```

A null key path resolves to `<home>/secrets/<id>-api-key`; otherwise the path must be absolute. `auth: none` is
allowed only with `loopback_http`. The `set-key` command requires `--config` and refuses profiles with no
authentication.

`limits` requires `max_tokens`, `context_tokens`, `min_tokens_per_second`, `request_timeout_seconds` and
`context_basis` (a short `TOKEN`-pattern value). Every other `INTEGER_FIELDS` value is optional and uses the
delegate's current default; `max_concurrent_per_host` defaults to 1 for loopback HTTP and 2 for HTTPS. Every
numeric value is a positive integer, and the existing context, output, timeout, streaming, metadata and retention
consistency checks apply. The host limit counts only this profile's jobs; because each profile has its own ledger,
profiles that share one server count separately.

Delegate state is private and profile-scoped at `<home>/delegates/<id>/`: `ledger.sqlite3`, `jobs/`, `probes/`
and `exports/<room_id>/`. Usage is requested for streaming responses when the selected syntax supports the
usage-request field. If a response omits usage, the result preserves `usage: null` and `usage_source: "missing"`;
unknown usage is never represented as zero.

## Loopback profile example

The following is the single local-server example in this reference. Run the command with the selected model and
served name, and set the profile's `model` to exactly the value supplied for `--served-model-name`. Set
`limits.context_tokens` to exactly the server's `--max-model-len`:

```sh
vllm serve <model> --served-model-name <name> --host 127.0.0.1 --port 8000 --max-model-len 32768
```

For example, replace `<name>` with `local/model-name` in both the profile and command:

```json
{
  "profile_version": 1,
  "id": "local-vllm",
  "transport": {
    "kind": "loopback_http",
    "host": "127.0.0.1",
    "port": 8000,
    "path": "/v1/chat/completions"
  },
  "model": "local/model-name",
  "request_syntax": "openai_plain",
  "auth": {"kind": "none"},
  "limits": {
    "max_tokens": 8192,
    "context_tokens": 32768,
    "ask_max_tokens": 1024,
    "min_tokens_per_second": 20,
    "request_timeout_seconds": 470,
    "context_basis": "vllm_max_model_len",
    "max_input_bytes": 24576,
    "max_context_file_bytes": 24576
  }
}
```

The command line's `--served-model-name` value in this example must be `local/model-name`, matching the profile's
`model`. `request_timeout_seconds` is the smallest value accepted by the sizing rule with the default
`connect_margin_seconds` of 60: `ceil(8192 / 20) + 60 = 470`. `max_input_bytes` leaves exactly the context budget
remaining after `max_tokens`, and `max_context_file_bytes` does not exceed it. Plain syntax sends no reasoning or
thinking parameters and requests streamed usage. If the endpoint does not return usage, the delegate reports it as
missing and unknown, not zero.
