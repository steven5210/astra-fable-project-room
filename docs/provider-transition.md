# Retained AO provider and routing adoption

Changing the setup default does not change an existing room. A normal AO room
pinned to DeepInfra V4.1 Flash can make one explicitly authorized transition to
the official `deepseek-flash` transport at `max`, 393,216 output tokens and
1,048,576 context tokens. The transition is an operating amendment: the original
specification, agreement, native sessions, source inputs, candidate and consumed
review attempts remain intact. It does not establish that the new provider has
completed any work.

This retained transition supports an explicitly recorded v1 routing preparation.
Missing, malformed or already-v2 routing refuses at the provider audit, before
an irreversible provider intent. Those preparations need a separately designed
transition; changing pointers manually is not supported.

The operation requires complete, completed native history with matching saved
receipts, settled delegate delivery across the entire ledger, an intact current
agreement/handoff and unchanged provider artifacts. Definite pre-generation
rejections remain historical evidence. Active, unresolved, abandoned, missing or
contradictory delivery evidence refuses adoption. A model substitution, uncertain
native turn or exhausted unaccepted charter cannot be repaired by this operation.

## Operator sequence

Use the installed CLI or the corresponding `ao_room_*` MCP tools. The tool schemas
are authoritative; values below refer to evidence returned by the preceding step.
Keep a durable private operator intent before each external AO lifecycle or
configuration call. Reconcile its actual result after a lost acknowledgement;
neither AO exit nor resume is an automatically retryable model operation.

1. Perform the current [AO stable-release check](../skills/project-room/references/ao.md#stable-release-check).
   Inspect the existing room and native conversations. Do not start a replacement
   room, repeat a provider job or dispatch a probe to test eligibility.
2. Call `ao_room_provider_transition_audit(room_id, target_profile)`. The target is
   a key-free configuration with the explicit official backend and approved pins.
   Key checks inspect file metadata only. The audit compares the exact owned,
   hash-verified pure validation slice of the frozen adapter with the current
   validator, without importing its executable module or instantiating a ledger.
   A changed validation contract refuses even if unrelated adapter code differs.
   The key-metadata and key-reader AST contracts are also compared, without
   executing the reader. Only metadata diagnostics run; credentials are not read.
3. If the bound engineer is idle, use stock AO `exit-agent` for that exact session.
   Positively observe `controller: stopped`, retained native materialization and
   unchanged complete history. Re-audit after stopping if other evidence changed.
   Record the actual lifecycle operation and observations; a free-text stop claim
   alone never overrides the observed controller state.
4. Call `ao_room_provider_transition` with the saved audit digest, actual user
   authorization, diagnosis, a stable `request_id` and the performed
   `native_stop_record`. It writes immutable epoch-2 evidence and changes only the
   active provider/preparation pointers. The original adapter, profiles, launch
   receipt, requests and handoff remain archived and hash-bound. Identical inputs
   read or reconcile the same intent; changed inputs refuse. No model is called.
5. For a historical v1 preparation, call `ao_room_routing_adoption_audit`. This
   bounded overlay requires an existing owned readable delegate ledger, a stopped
   engineer, no epoch-2 request, ledger job, probe or native launch, inline AO project rules and no other registered
   room sharing those project rules. It cannot adopt an unused, absent-ledger
   preparation or file-based project rules. Perform this overlay before resuming
   the provider-changed engineer: an intervening epoch-2 launch receipt refuses
   rather than being deleted or overwritten.
   Paid CLI probes are outside the job ledger, so both adoption audits also
   inspect their complete bounded receipt directory. Unowned/incomplete probe
   evidence, unsettled source probes and any same-room target-provider probe
   refuse. No probe is sent to establish readiness.
6. Call `ao_room_routing_adoption_stage` with its exact audit digest, authorization,
   diagnosis and stable request ID. It records a durable pending intent, archives
   the original runtime files, and stages a versioned v2 guard and MCP attachment
   witness. Only the ignored worktree-local settings change; unrelated hooks,
   denies, environment and pinned native agent definitions remain. Ordinary room
   work is blocked throughout this pending state.
7. Apply the exact returned `project_config_payload` to its returned stock AO
   project configuration path. It preserves existing configuration fields and
   appends the actual official-provider and v2 operating amendment. This explicit
   amendment supersedes historical DeepInfra-only operating instructions; it does
   not silently rewrite the charter. Do not edit the payload or force through a
   concurrent project change. Repeating `stage` with identical inputs can reconcile
   partial local staging; inspect and apply only the same exact configuration.
8. Call `ao_room_routing_adoption_activate(room_id, request_id)` while still
   stopped. It rechecks native history, candidate, runtime bytes, project rules
   and immutable intent before activating the new preparation pointer. An exact
   repeated activation is read-only. Neither stage nor activation calls a model,
   changes the shared repository MCP registration or resumes a native controller.
   Every engineer request, including a later specification review, remains blocked
   until the configured overlay and initialized attachment are verified. A review
   request cannot consume an epoch-2 request before adoption is ready.
9. Use stock AO `resume-agent` for the same engineer. Verify retained native
   materialization, the same conversation/branch/model/MAX and unchanged history.
   Where the public API omits the underlying provider conversation UUID and
   controller generation, use a narrow read-only query of the supported local AO
   storage to compare them. A new native UUID or reconstructed/replayed history
   is not a successful retained-session adoption.
10. Observe the initialized MCP connection before sending new work. The original
    pre-exec `delegate-launch.json` proves only that the launcher ran. The new
    witness additionally requires a successful `initialize` response forwarded
    to the client, its `notifications/initialized`, the exact six-tool
    `tools/list` result forwarded to that client and the matching held local
    connection lease. Then send the actual new instruction or `Continue.`.
    The provider/routing amendment is delivered once against a completed,
    digest-bound receipt; subsequent routine turns contain only caller bytes.

Every new engineer request requires fresh, bounded raw history with explicit
completeness metadata, `isTerminated: false` and `controller: ready` before the
request is recorded or sent. Missing or contradictory history and lifecycle
evidence refuses. A stopped controller remains eligible for the audit/staging
procedure, but a held MCP lease cannot make it ready to receive new work.

For AO 0.12.12, the verified daemon's `AO_DATA_DIR/ao.db` (default
`~/.ao/data/ao.db`) contains `sessions.provider_conversation_id` and
`sessions.controller_generation`. The optional read-only
`ao_native_identity.read_owner(database, session_id)` helper joins that exact
session to its active `conversations` / `conversation_branches` record and reads
only identity, lifecycle, workspace and materialization fields. Select the
database from the running daemon's verified configuration; do not guess another
installation's path. It refuses schema changes, missing or ambiguous owners,
terminated sessions, replay, or conflicting session/branch provider UUIDs.
Persist the pre-stop owner privately, then read again after resume and call
`verify_resume(before, after)`: the same native identity with a different nonempty
controller generation is required. AO stores that generation as an opaque text
fence, not an increasing number. Its domain normalization treats only the legacy
empty branch strategy as native; the helper retains the raw value and refuses
other unknown or approximate strategies. Corroborate its conversation, branch and
workspace with public AO observations; the helper alone does not prove controller
readiness or MCP initialization. Neither helper writes a database, reads native
messages or performs the lifecycle operation.

When public AO fields omit the underlying UUID or generation, this before/after
storage verification is required before the first epoch-2 dispatch. Only the
choice between this helper and an equivalent verified read-only query is
optional. A missing or cleared generation refuses; do not treat it as evidence
that the owner was retained. The accepted raw strategy values are exactly the
legacy empty string and `native`. Check the room listing for sole project
ownership; historical registered rooms also count as conflicts in this version.

## Evidence and failure boundaries

The witness is a separate versioned stdlib process in the room. It relays MCP
bytes to the exact retained child server and checks pinned provider files before
starting that child. It reads no key and does not call inference. Its startup
ledger check prevents the retained adapter from recreating lost delivery history.
The wrapper preserves the original launcher and shared local MCP registration.

The preserved launcher has a 4,000,000-byte read ceiling on every scanned room
state and active normal preparation, including unrelated rooms in that controller
home. Adoption checks those current files and proposed target state/preparation
before writing an intent. This does not guarantee unlimited future history:
growth beyond that ceiling can still block a later native restart. The new
wrapper and combined audit evidence use an explicit 96,000,000-byte bound;
manifests remain limited to 256,000 bytes. Oversized audits refuse before being
written. Expanding the historical launcher's restart capacity requires a separate
versioned launcher and shared-registration change.

Each connection has an immutable ready receipt and ending or invalidation
evidence. A held lease identifies the current connection; an old receipt cannot
certify a replacement lease after crash/restart. These observations establish
native-client initialization and tool enumeration at inspection time. They do
not provide portable process-start attestation, prove a particular AO controller
generation by themselves, establish inference health, settle paid jobs, prove a
guard was invoked by Claude or demonstrate token savings. Room dispatch separately
checks the current native identity/history and full ledger.

If initialization or tool listing fails, keep the room blocked and inspect the
private evidence. Do not replace a missing receipt with a manually written file,
send a model prompt to bypass readiness, erase an uncertain job or try a different
provider. A partial immutable intent reconciles only with identical authorized
inputs and matching observations. Drift requires diagnosis; it is not permission
to reset counters or recreate a room.

The pre-child ledger refusal also makes this connection's model-free
`deepseek_status` and `deepseek_result` tools unavailable until the cause is
resolved. Operator read-only ledger/status inspection remains available. Only
the user's own supported terminal resolution can settle uncertain paid delivery;
the wrapper never performs that action.

The existing audited recovery of a reviewer that has never received a request
remains available. The adoption validators accept its changed reviewer binding
only through the intact original-to-replacement recovery receipt. The engineer
binding remains exact, counters and old claims remain intact, and first independent
acceptance does not require the completed engineer's MCP process to stay open.
This is not recovery of an interrupted engineer or a used reviewer.

Historical jobs retain epoch-1 attribution and settings. New requests and jobs
are tied to epoch 2, and old engineering/gate evidence cannot certify new work.
Normal Fable engineering review, applicable gates and independent Astra acceptance
continue after adoption. The synthetic tests establish the local state machine,
stdio handshake and one-time packet behavior. Live v2 delegation, compaction,
interrupted-run recovery and measured total usage require separate live evidence.
