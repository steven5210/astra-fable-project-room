# Configured engineering model selection and transition

## Overview and roles

"Fable" names the historical and default engineering role in a normal
`fable_engineering` AO room: the native AO engineer runs the room's configured
Claude engineering selector at `max` with harness `claude-code`. The workflow
name `fable_engineering` and the Claude-binding reason `fable_reason` are
unchanged compatibility names for this same role. This document covers the
selector a room configures when it opens, the operator-selected source
qualification that pins a qualified family's exact expected model before
inference, how an already-open room adopts a new selection through an audited
boundary, and the executable prerequisite a newer model may require. It does
not change what the role owns: the configured engineering orchestrator retains
engineering interpretation, delegation and the final engineering verdict as
described in [Fable's policy](../../skills/project-room/references/fable-policy.md);
only the configured model identity moves through a controlled, auditable path
instead of an inferred or silent switch. Historical judgments keep the
attribution recorded with them: Fable's prior judgments remain Fable's, and
later Opus judgments remain Opus's.

A selector is either an engineer-role family alias — `fable` (the default) or
`opus` — meaning the latest available member of that family at MAX, or a
qualified exact identifier kept for compatibility. A family selector records
configured intent, not a resolved model: a qualified family dispatches from
the exact expected model the operator's retained source qualification pins
before inference, and this controller never discovers or invents an unknown
future model on its own, nor asserts that a provider, account or quota is
available. A room may explicitly select another qualified engineer-role
family alias or exact model when it is opened. Reopening with a different
selector is refused, and existing rooms never migrate on their own: a room
keeps the selection it opened with until an audited boundary below changes
it, and editing a private artifact or configuration file alone changes no
room.

## Selectors, qualified policy and the source qualification

A selector is a family alias (`fable`, `opus`) or an exact identifier kept for
compatibility. The engineer-role families a room can select are `fable` (the
default) and `opus`; the `sonnet` family is bundled for the `pr-sonnet` worker
role only. A family alias means the latest available member of that family at
`max`, and a family's `minimum_claude_code_version` is a compatibility floor
for the effective Claude Code executable, never a mapping to an exact model.

| Selector | Kind | Harness | Reasoning effort | Compatibility floor |
| --- | --- | --- | --- | --- |
| `fable` (default) | family alias | `claude-code` | `max` | none |
| `opus` | family alias | `claude-code` | `max` | `2.1.280` |
| `claude-fable-5-1` | exact identifier (legacy default) | `claude-code` | `max` | none |
| `claude-opus-5-5` | exact identifier | `claude-code` | `max` | `2.1.280` |

Claude Code 2.1.268 was observed to refuse `claude-opus-5-5` before inference;
2.1.280 is the operator-qualified minimum. A bundled or recorded floor gates
new readiness only and blocks a new dispatch without rewriting historical
records. Qualification is a policy fact about which model/harness/effort
combinations this controller will select or transition to — it is not a claim
that a provider, account or quota is actually available.

Future compatible exact versions can be added without code changes through the
optional `engineering_models` key in the private
`PROJECT_ROOM_HOME/ao/config.json`, mapping an exact model id to an entry with
the same shape:

```json
{"harness": "claude-code", "reasoning_effort": "max", "minimum_claude_code_version": "X.Y.Z"}
```

`minimum_claude_code_version` is optional. This configuration can only add
exact models: it cannot redefine, weaken or remove a bundled entry, and it
cannot change the default selector. Malformed configuration, or configuration
that conflicts with a bundled entry, refuses selection and transition rather
than falling back silently.

A private operator-selected `family_qualification` pointer in the same
configuration names one bounded, owned, non-symlink JSON artifact and the
SHA256 of its complete value. The artifact declares the supported scope
(`anthropic_first_party`, alias remaps forbidden, availability not asserted),
maps a subset of the bundled families to an exact expected model of that same
family with optionally an exact `minimum_claude_code_version`, and cites the
retained source descriptors it was captured from — official `https`
documentation sources without credentials. Every selected descriptor's
complete evidence bytes are verified and then retained privately inside the
room's own qualification area as bounded immutable owned bytes, so later
admission and history verify the retained room bytes and never need the
private originals again. Only those explicitly selected captures are retained;
nothing else is copied, and no secret or unrelated file is read.

A later edit of the artifact or of the private configuration alone changes no
room. Each room pins its own policy snapshot and immutable policy record when
it is opened or transitioned, and moving a room to a new qualified mapping
requires an explicit authorized boundary — the audited engineering-model
transition — that validates and pins the new mapping. A future compatible
version is reached by preparing a new qualified artifact and performing that
supported adoption, never by editing a per-version source constant. This
controller does not automatically discover an unknown future model, and a
qualification asserts only its declared source scope: it is not provider
availability, account entitlement, a served model or observed effective
effort.

Root and worker qualification are independent authorities. The room's pinned
engineering root artifact and the effective executable identity that satisfies
its floor qualify the root only; each native worker is qualified separately
from the routing record's own retained qualification artifact. The unchanged
root's artifact or observed executable version never qualifies a worker, and
a worker's own compatibility floor applies to the worker's executable
requirement. An old exact or family-alias routing record and every old request
record remain readable and unchanged, while a new delegating
implementation/correction request on a routing record without a
source-qualified worker selection refuses and requires the supported explicit
source-qualified refresh (`ao_routing_refresh.py --agent-selection
qualified`), which is itself conditional on the room's retained effective
executable identity — or an audited executable-binding replacement — re-probed
at refresh time. A `--agent-selection family` refresh only moves the historical
exact pins to the family aliases; by itself it does not qualify new delegating
execution. New native worker definitions pin each enabled worker's qualified exact
expected model with its family intent and configured `max` effort. Configured
family intent, qualification and MAX are intent: bounded authenticated
nonsynthetic native stop-row evidence may establish an observed served child
model identity and is compared against the frozen exact role expectation, while
effective effort, provider availability and entitlement remain unobserved and
missing or ambiguous evidence stays insufficient.

## Selecting a model for a new room

`ao_room_open` accepts an optional `engineering_model` naming an engineer-role
family alias (`fable`, the default, or `opus`) or a qualified exact identifier;
omitting it selects the default family. Reopening the same room with a
different selector is refused — a room's initial selection is immutable, like
its other bindings — and existing rooms never migrate to a newer default,
family member or qualified model on their own.

The engineer AO session is bound at exactly the room's configured selector and
`max`. When the pinned policy or qualification records a
`minimum_claude_code_version`, binding requires the effective Claude executable
to satisfy that floor at bind time; an executable below the floor blocks the
bind. A family epoch that the room's pinned policy does not qualify refuses a
new dispatch until the supported qualification refresh below pins it. A
qualified family dispatch additionally verifies the room's registered
prospective native outcome source before anything is sent; register it first
with `ao_room_engineer_source_register`.

## Audited transition of an existing room

An existing room changes its engineering selection only through this
explicit, audited transition, which is also the supported adoption boundary
for a newly qualified mapping. It retains the same AO engineer session,
conversation, branch, worktree and reviewer, and preserves the specification,
counters, prior reviews and independent holds; nothing about the room's
identity, history or delegate ledger is replaced, and no hold is released.
The transition sends no prompt and does not authorize a new request. Never
infer a model switch from quota pressure, model availability or elapsed time;
it always requires the user's actual authorization for that named change.

### Tools

The public tools are `ao_room_engineer_model_audit`,
`ao_room_engineer_model_transition`,
`ao_room_engineer_model_transition_abandon` and
`ao_room_engineer_source_register`. MCP and the CLI expose the same schema
definitions, so the generic supported call path works for every tool:

```sh
python3 project_room.py --home /synthetic/controller-state call ao_room_engineer_model_audit --args-file arguments.json
```

- **`ao_room_engineer_model_audit(room_id, target_model, native_owner_database, native_transcript_path)`**
  is read-only: it performs bounded AO GETs and local reads, writes no room
  file, journal pointer or state, and makes no model call. It returns
  `eligible`, an `audit_sha256`, the source and target selector and configured
  value, the pinned qualification and `expected_model`, `spec_record_sha256`,
  `candidate_sha256`, `native_history_sha256`, a null observed served model
  with its basis, the configured-versus-effective effort split, any retained
  semantic holds and the supporting evidence. Ordinary ineligibility is a
  structured response, not a transport error.
- **`ao_room_engineer_model_transition(room_id, request_id, source_model, target_model, audit_sha256, spec_record_sha256, candidate_sha256, native_history_sha256, native_owner_database, native_transcript_path, authorization, reason)`**
  is the explicit, idempotent apply/reconcile call, and it is a mutation. It
  reobserves the exact audited evidence under the core's own room lock, writes
  one create-once intent, and then: a configured-model change sends exactly
  one guarded AO `PATCH /sessions/{id}/conversation/settings` before it can
  commit, while a same-selector qualification-only refresh — the target is the
  identical engineer-role family selector at the configured value already in
  force, used to pin a newly qualified mapping — sends zero PATCHes. An
  identical repeat of the same `request_id` with the same arguments returns
  the saved result; changed arguments under that same `request_id` are
  refused. A pending attempt can only be observed or reconciled by that
  identical request, and it uses the target and retained proof stored in its
  own intent even if today's private pointer later changes; if the retained
  qualification proof is missing or changed, the boundary refuses rather than
  repairing it. A request that already reached a terminal outcome is an
  idempotent historical read that observes nothing new.
- **`ao_room_engineer_model_transition_abandon(room_id, request_id, native_owner_database, native_transcript_path, authorization, diagnosis)`**
  explicitly closes only that still-pending attempt, and only when a fresh
  read-only observation shows exactly the recorded source settings and
  otherwise unchanged frozen evidence. It sends no PATCH, writes no
  transitioned epoch, and never claims that the earlier attempt did not apply.
  A later transition always needs a new `request_id`, a fresh audit and fresh
  authorization, and an identical repeat reads the saved terminal record.
- **`ao_room_engineer_source_register(room_id, native_owner_database, native_transcript_path)`**
  registers the bound engineer's prospective native outcome source before its
  first response. It is engineer-only in a normal engineering-orchestrator
  room and it is a mutation of room state. It authenticates the explicit
  verified AO owner database and the exact prospective or existing owned
  transcript path, derives the workspace from the retained preparation rather
  than from caller input, and refuses a conflicting registered source instead
  of replacing it. It also refuses held, inconclusive or missing latest
  outcomes, an unused acceptance-review grant, and ambiguous retained request
  ordering. An identical supported registration revalidates ownership.
  Registration is local preparation only: it is not provider availability,
  quota, execution or permission to send. The room lock's existing
  diagnostic-invalidation semantics still apply to a failed locked operation.

The public methods and schemas forward these implemented arguments directly;
the core owns the lock, idempotency and exact retry semantics of the three
model operations, so callers must not wrap another lock around them or
reinterpret a saved result.

### Eligibility

All of the following are required before a transition can be applied:

- A normal `fable_engineering` room with a bound engineer.
- A settled room: no active or uncertain request, and no pending journal of
  any kind.
- No committed fourth charter/specification-review extension grant, used or
  unused: it pins provider and routing identity and blocks this
  engineering-model transition. The separate acceptance-review grant blocks
  only while unused. This review-extension restriction is specific to the
  engineering-model transition; a routing refresh has its own separate
  consumed-grant path.
- A fresh target resolves from the current operator `effective_policy`: either a different configured value, or the identical engineer-role family selector re-pinning a newly qualified mapping; a pending replay instead uses its own recorded target, and the current source epoch stays pinned. A qualified target's retained artifact and selected source captures are verified from the room's own bytes; a missing or changed retained proof refuses rather than falling back to the private originals.
- Valid preparation, routing, AO project rules, compaction and provider files.
- The effective Claude executable satisfies the target's recorded
  compatibility floor, when one is recorded; a floor is a compatibility
  check, never a mapping to an exact model.
- The same retained engineer session, reloaded and idle, with a live AO
  controller (`controller: ready`), an AO session that is not terminated, and
  idle owner-database activity.
- Strict, complete native history: every turn is owned by a completed
  request, a validated settled quota failure, or a proven
  compaction/task-notification import. An unowned or intervening turn
  refuses.
- No model reroute.
- Strict DeepSeek ledger quiescence.
- Positive native child evidence from the exact retained transcript: every
  `Agent`/`Task` launch has its result, and every background launch has a
  terminal task notification.
- Latest semantic outcomes are known: a known quota/provider/truncation hold
  is retained (the transition never clears it), but an unknown outcome
  refuses.

### Write-ahead and reconciliation

Applying a transition writes a hash-bound create-once intent, then rechecks
every eligibility condition above under the core's own room lock, writes a
durable attempt marker, and commits a new epoch only from a fresh observation
that equals the intended target with every other setting, native history,
owner, candidate, spec, routing and ledger evidence unchanged. The commit is
conditional:

- A configured-model change (the target's configured value differs from the
  source's) sends exactly one guarded AO
  `PATCH /sessions/{id}/conversation/settings` carrying `model`,
  `reasoningEffort: "max"` and — because AO v0.13.0 stores these settings
  wholesale and would otherwise clear it — the exact current nonempty
  `approvalMode`, and then reads AO back. Settings read-back confirms only the
  stored setting for the next turn; it is not native execution or adoption.
- A same-selector qualification-only refresh (the target is the identical
  engineer-role family selector at the configured value already in force,
  pinning a newly qualified mapping) opens a new family epoch with **zero**
  PATCHes. It still writes the intent, the attempt record and an immutable
  committed record, and it is the supported adoption for a new qualified
  mapping. Do not describe every transition as sending one PATCH.

An interrupted or uncertain PATCH is never repeated blindly: the identical
request can only commit an exact target observation against unchanged
evidence, and it uses the target and retained proof stored in its own pending
intent — a later change to today's private pointer, artifact file or
configuration does not change that attempt. If the stored retained proof is
missing or changed, the boundary refuses and preserves the evidence rather
than repairing it from the private originals. While pending, ordinary
dispatch, sync writes and other mutations are blocked until the attempt is
diagnosed. The only other way to close a pending attempt is the explicit
abandon operation below. `ao_room_status` shows the pending state without
modifying it.

### Abandonment

`ao_room_engineer_model_transition_abandon` is the only other closure for a
pending attempt. It requires a fresh read-only observation showing exactly the
recorded source settings and otherwise-unchanged frozen evidence, and it uses
the target and retained proof stored in the pending attempt's own intent. It
sends no PATCH, and it makes no claim about whether the earlier PATCH attempt
actually applied — only that the currently observed settings match the source.
A later transition needs its own new `request_id`, a fresh audit and fresh
authorization; abandonment does not reopen or retry the old attempt, and an
identical repeat reads the saved terminal record.

## Executable prerequisite and operator sequence

AO's settings endpoint needs a live idle controller, while binding or
replacing the Claude executable — and the stopped-controller routing refresh —
need a stopped owner. These are separate operations with separate audited
tools, not one atomic automatic stop/patch/restart. The operator validates the
exact current state and the supported prerequisites at each boundary, retains
any unknown delivery for diagnosis, and never repairs database state or
injects digests. For an existing room whose pinned Claude Code build is below
the target's compatibility floor, follow this sequence:

1. Reconcile known states first. Perform the [stable-release check](../../skills/project-room/references/ao.md#stable-release-check),
   sync, and check status. Reconcile or settle every known owned terminal
   failure through the existing supported outcome tools
   (`ao_room_outcome_audit` / `ao_room_outcome_resume`); unknown delivery stays
   blocked and is never replayed. Confirm every request is settled and inspect
   any retained hold.
2. Stop the idle engineer through AO. If the recorded executable is intact,
   use the explicit upgrade lane of the standalone `ao_executable_binding.py`
   CLI with `--upgrade-authorization` and `--expected-version X.Y.Z`; it moves
   only to a strictly newer, exactly matching version, keeps the original
   preparation and every earlier journal, and runs only a bounded `--version`
   probe. If the recorded executable is missing or has changed instead, use
   the existing repair lane (without upgrade arguments) — see
   [a pinned Claude executable disappeared](efficient-continuation.md#a-pinned-claude-executable-disappeared).
   A routing refresh (`ao_routing_refresh.py --agent-selection qualified`) is
   another standalone stopped-controller operation, not an MCP tool. Every one
   of these lanes requires a previously recorded successful identity: a room
   whose preparation never recorded one (missing identity fields or a recorded
   probe error) cannot be first-bound in place, because no audited lane may
   establish an absent historical identity. Such a room and its history stay
   unchanged and readable; new qualified worker execution needs a separately
   authorized, freshly prepared room. Never infer the missing pin from mutable
   `claude_bin` or controller configuration, and never treat a current response
   as historical authority. A recorded identity that was successful but whose
   executable is now missing, changed or below the target floor keeps the
   existing audited repair/upgrade lane above; a recorded historical identity
   is that room's own history, not current readiness.
3. Resume the same native session through AO. Verify native identity
   continuity, the loaded executable, `max` and any required delegate
   attachment, and keep the session idle — send no prompt. A later prompt is a
   separately authorized step, not part of the transition.
4. Run `ao_room_engineer_model_audit` with the AO ownership database and the
   exact native transcript; the audit requires the live idle controller.
5. With the user's actual authorization, run
   `ao_room_engineer_model_transition`. A configured-model change sends one
   PATCH; a same-selector qualification-only refresh sends none.
6. Handle any retained hold through the ordinary `ao_room_outcome_audit` /
   `ao_room_outcome_resume` sequence, with actual user authority.
7. Send only the actual new instruction or `Continue.` — the next separately
   authorized engineer request — and verify the response's native model
   identity. The current effective undelivered committed root boundary is
   disclosed on the next separately authorized normal engineer request,
   including a read-only specification review; the current effective
   undelivered worker/routing boundary waits for delegation-capable
   implementation or correction work. Superseded intermediate boundaries and
   already authenticated completed deliveries are not replayed. Ordinary
   continuations then carry only actual new user content, never a full-spec
   replay or a retrospective relabeling.

### CLI argument files

These examples are synthetic. Replace every identity, path and digest with the
exact value observed for the intended room. The generic supported call path is
`project_room.py --home <controller home> call <tool> --args-file arguments.json`;
MCP exposes the same schema for each tool.

`audit-arguments.json` (read-only):

```json
{
  "room_id": "ao-example",
  "target_model": "opus",
  "native_owner_database": "/synthetic/ao-owner.sqlite",
  "native_transcript_path": "/synthetic/engineer-transcript.jsonl"
}
```

```sh
python3 project_room.py --home /synthetic/controller-state call ao_room_engineer_model_audit --args-file audit-arguments.json
```

`transition-arguments.json` (mutation; a configured-model change sends one
PATCH):

```json
{
  "room_id": "ao-example",
  "request_id": "engineer-model-2",
  "source_model": "fable",
  "target_model": "opus",
  "audit_sha256": "<audit_sha256 returned by the fresh audit>",
  "spec_record_sha256": "<spec_record_sha256 returned by the audit>",
  "candidate_sha256": "<candidate_sha256 returned by the audit>",
  "native_history_sha256": "<native_history_sha256 returned by the audit>",
  "native_owner_database": "/synthetic/ao-owner.sqlite",
  "native_transcript_path": "/synthetic/engineer-transcript.jsonl",
  "authorization": "The user authorized moving this room's engineer to the opus family at max",
  "reason": "The user wants the qualified Opus mapping for the remaining work"
}
```

```sh
python3 project_room.py --home /synthetic/controller-state call ao_room_engineer_model_transition --args-file transition-arguments.json
```

A same-selector qualification-only refresh uses the same tool with
`target_model` equal to the room's current configured value and a fresh audit;
it pins the newly qualified mapping and sends zero PATCHes.

`abandon-arguments.json` (mutation; closes only the pending attempt):

```json
{
  "room_id": "ao-example",
  "request_id": "engineer-model-2",
  "native_owner_database": "/synthetic/ao-owner.sqlite",
  "native_transcript_path": "/synthetic/engineer-transcript.jsonl",
  "authorization": "The user authorized abandoning the uncertain transition attempt",
  "diagnosis": "The settings response was lost after a connection drop; the fresh read-only observation still shows the recorded source selector"
}
```

```sh
python3 project_room.py --home /synthetic/controller-state call ao_room_engineer_model_transition_abandon --args-file abandon-arguments.json
```

`source-registration-arguments.json` (mutation; before the first response):

```json
{
  "room_id": "ao-example",
  "native_owner_database": "/synthetic/ao-owner.sqlite",
  "native_transcript_path": "/synthetic/engineer-transcript.jsonl"
}
```

```sh
python3 project_room.py --home /synthetic/controller-state call ao_room_engineer_source_register --args-file source-registration-arguments.json
```

`ao_executable_binding.py` and `ao_routing_refresh.py` are separate standalone
CLIs with their own arguments; they are not exposed through `call` or MCP.

## What the transition never does

A transition never clears a quota or other semantic hold, authorizes
dispatch, sends a prompt, renews review attempts, changes the specification or
gates, grants acceptance or replaces the reviewer. The ordinary
`ao_room_outcome_audit` / `ao_room_outcome_resume` sequence with actual user
authority remains required for holds. The same AO engineer session, worktree,
specification, counters, prior reviews and independent holds are preserved.
Stored bindings, receipts, the original spec agreement and its recorded
attribution stay byte-for-byte unchanged; requests recorded before a committed
boundary keep the attribution saved with them, including the Fable engineering
role, and engineering ownership from the boundary belongs to the room's
configured engineering orchestrator, named only by its configured value.
Nothing is retrospectively relabeled. Each historical request keeps and is
checked against the model recorded for its own epoch; new requests record the
new expectation, and native responses must name that exact model — never a
silent substitution. AO's settings read-back confirms only the stored setting
for the next turn; actual native use is established only by the next owned
response's native model identity, and observed effective effort stays
unevidenced.

## One-time notice and continuations

The current effective undelivered committed root boundary is reported once,
in the next separately authorized normal engineer request, including a
read-only specification review, carrying the committed digest of its own
authority; the current effective undelivered worker/routing boundary waits
for delegation-capable implementation or correction work. Superseded
intermediate boundaries and deliveries already authenticated against a
completed request are not replayed, and no later delivery of a superseded
boundary is promised. The notice states the model boundary; that the native
history, current specification and any existing agreement are unchanged and
remain attributed to the model that produced them; that this is not an
invitation to re-review or replay that work; and to inspect and adopt the
current worktree before giving any new engineering verdict. Deliveries are
authenticated against the owning request's verified receipt: a successful
known delivery is recorded, pending or abandoned transition or refresh
evidence is no notice, and uncertain delivery never authorizes a resend.
Later continuations carry only the caller's actual text (`Continue.` or the
new instruction) — the notice is delivered once, not on every turn, and no
full specification is replayed.

## Limitations

- Offline fake-backend tests only; no live readiness, provider availability or
  model quality is established.
- In-place first binding of a Claude executable identity is unsupported: a
  preparation that never recorded a successful identity stays readable with its
  history unchanged, but the audited repair and upgrade lanes and a qualified
  routing refresh cannot establish that first identity, and new qualified work
  requires a separately authorized, freshly prepared room. A recorded
  historical identity in a retained room is that room's own history, not current
  readiness.
- A family alias is configured intent. Neither the alias, the pinned
  qualification, nor an AO settings read-back alone proves the model that
  actually served a turn, account entitlement or quota; effective effort,
  provider availability and entitlement remain unobserved. Bounded
  authenticated nonsynthetic native stop-row evidence may establish an observed
  served model identity and is compared against the frozen exact role
  expectation; missing or ambiguous evidence remains insufficient.
- Worker qualification is pinned separately from the routing record's own
  retained artifact. The root's qualification or observed executable version
  never qualifies a worker. The controller attributes an observed served child
  model identity only through that bounded authenticated native child evidence
  and never attributes effective effort; configured family intent and MAX are
  not observed execution.
- Child-internal background work invisible in the parent transcript cannot be
  observed by the native-child-evidence check above.
- AO's settings read-back is not proof of native adoption; only the next
  owned response's native model identity establishes that.
- Preserving `approvalMode` in the PATCH payload is AO v0.13.0 compatibility,
  not a permission change.
