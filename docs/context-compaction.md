# Native context compaction for AO rooms

Every new normal AO Fable engineer preparation configures Claude's native
automatic compaction with a **250,000-token window**. This applies to every
project and to both DeepSeek rooms and rooms with `delegate_provider: "none"`.
The latter still have native Sonnet/Opus routing. Compaction preserves
the selected model, Fable MAX, delegation ownership, review budgets and quality
requirements. The legacy controller and Astra-led exceptions are unchanged.

Claude performs the compaction in the retained native session. Project Room
configures the window before launch; it does not send `/compact`, issue an extra
model call, or automatically retry interrupted work.

## Configure future preparations

The default requires no extra setup. To choose another window, merge this field
into the existing private `PROJECT_ROOM_HOME/ao/config.json` (default home
`~/.project-room`), preserving its other keys:

```json
{"auto_compact_window": 250000}
```

The value must be a JSON integer from 100,000 to 1,000,000. Strings, booleans,
fractions and out-of-range values are refused before preparation writes routing
files or invokes Claude configuration. A later default change affects only new preparations, including a room
opened earlier that has never been prepared. Repeating `ao_room_prepare` for an
existing preparation retains its exact recorded files and policy.

Preparation writes these values into the worktree's already-ignored
`.claude/settings.local.json`, alongside its existing routing protections:

```json
{
  "autoCompactEnabled": true,
  "autoCompactWindow": 250000,
  "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "250000"}
}
```

This is an excerpt; never replace the complete prepared settings with it. The
files and chosen window are part of the preparation's immutable evidence.
Status exposes the choice under `delegate.routing.compaction`. A configured or
verified routing status attests configuration checks, not an actual compaction.

Preparation and later dispatch validate user, managed, project and local
settings, the controller environment, and AO's project environment. An explicit
disabled `autoCompactEnabled`, a conflicting `CLAUDE_CODE_AUTO_COMPACT_WINDOW`,
or a nonempty `DISABLE_COMPACT`, `DISABLE_AUTO_COMPACT`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` or `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` refuses
the new policy. Sync records AO environment conflicts as unverified. Resolve
the conflicting source deliberately rather than changing immutable snapshots.

Claude documents a 100,000–1,000,000 range and gives this environment variable
priority over other window settings. Safety buffers can make the actual
trigger occur before the configured window. The selected model's full context
capacity is unchanged. See [Claude model configuration](https://code.claude.com/docs/en/model-config#context-window-and-auto-compaction)
and [environment variables](https://code.claude.com/docs/en/env-vars).

## Existing prepared rooms

Updating the plugin does not retrofit or relabel an older preparation. Its
settings, guard, routing evidence, provider adoption history and request bytes
remain valid. Do not delete a preparation, edit its hashes, reconstruct its
specification, or open a replacement room to obtain the new default.

An operator can configure an existing retained session at an idle boundary:

1. Read status and sync once. Establish that every owned request has known
   completion, the native worker is idle and no delegate is active or unresolved.
   Preserve the native session and conversation identities, history prefix,
   candidate fingerprint, room evidence and model/effort configuration.
2. Through stock AO project configuration, merge
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW: "250000"` into `config.env`, preserving all
   other fields. Check the same disable and override sources listed above. This
   project setting applies to other Claude workers on their next launch too;
   account for that scope before changing a shared project. An older pinned
   local disable setting requires a separately reviewed migration, not an edit
   to the saved preparation.
3. Use stock AO lifecycle controls to exit the idle native controller and resume
   the **same** native session. Do not assume a file change hot-reloads the
   running window. Verify the selected process environment, unchanged native
   session/history/candidate and model at MAX. Verify the room's existing
   delegate attachment using its supported checks, including a fresh complete
   MCP handshake when its preparation requires one. A room with provider
   `none` has no delegate MCP attachment; an older preparation does not acquire
   a new handshake requirement. Read-only reconciliation and lifecycle checks
   need no inference.
4. Let the room's sole dispatcher issue the next authorized continuation once,
   using only `Continue.` or the actual new requirement. Do not send slash
   commands through `ao_room_send`, repeat retained context, or have a second
   task dispatch work concurrently.

A pending, interrupted or uncertain request is outside this procedure: preserve
the evidence and diagnose it through status/sync. This configuration change adds
no audited replay or automatic crash-recovery operation.

## Verify without replaying work

The offline suite tests default propagation, conflict refusal, immutable
repreparation, unchanged historical routing/adoption evidence and dispatch/sync
checks using fake backends. It calls no real model or provider. Run:

```sh
python3 -m unittest test_ao_compaction -v
python3 -m unittest discover -v
```

Live validation uses useful work already authorized for the room. Record an
actual native compaction boundary and its summary evidence; check the same
session, unchanged task requirements and candidate, MAX, and the next useful
result against its agreed gates and independent acceptance requirements. A
smaller context reading or a successful restart alone proves none of these.
If the native run produces a context gap, address that specific gap once.

Measure individual request context separately from aggregate input/cache/output
usage, include children and compaction work, and mark unreported usage unknown.
Compaction itself costs tokens and cannot restore subscription quota. Claim net
savings only from comparable observed work and quality; see [Claude's explanation
of long-session usage](https://code.claude.com/docs/en/costs#why-usage-climbs-in-a-long-session).
