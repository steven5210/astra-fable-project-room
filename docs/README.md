# Documentation index

Project Room's documents are grouped into guides, references and operations runbooks. The README keeps only the install path and a one-sentence pointer under each moved heading, so existing links and anchors keep working.

## Guides

- [Feature workflow](guides/workflow.md) — the room-level lifecycle: specification, handoffs, jobs, decisions and acceptance evidence for one feature.
- [Efficient review and diagnosed continuation](guides/efficient-continuation.md) — what a personal review assigns to Fable, compact reports, and how diagnosed continuations and holds are handled.
- [Configured engineering model selection and transition](guides/engineering-model-transition.md) — the audited one-time change of a room's engineering selector: a qualified family alias or exact model.
- [Audited acceptance-review continuation](guides/acceptance-review-continuation.md) — the single-use grant for one more acceptance review after the ordinary three attempts.
- [One additional AO charter review](guides/one-charter-review-extension.md) — the audited, once-per-room extension after three charter reviews are consumed.
- [Pre-acceptance exact-spec review extension](guides/pre-acceptance-review-extension.md) — the same once-per-room extension granted before any acceptance, for a scope-change proposal on an unaccepted candidate.
- [Native context compaction for AO rooms](guides/context-compaction.md) — the configured automatic compaction window and its validation limits.
- [Roles, agreement and delegation](guides/roles-and-delegation.md) — participant responsibilities, the exact-spec agreement flow and the bounded delegation ladder.
- [AO backend setup and normal workflow](guides/ao-setup.md) — configuring the private AO endpoint, delegate provider and preparation hook, then the normal room workflow.

## Reference

- [Architecture](reference/architecture.md) — the runtime module map and component responsibilities.
- [MCP runtime lifetime](reference/connector-runtime.md) — how the connector retains a coherent runtime outside the replaceable plugin cache.
- [DeepSeek delegate](reference/deepseek.md) — setup, invocation and evidence reference for the optional DeepSeek delegate provider.
- [Text delegate profiles](reference/text-delegate.md) — profile schema, transport rules and the standalone text delegate's bounded state.
- [AO prompt and evidence-read diagnostics](reference/diagnostics.md) — prompt bytes, delivery proof, logged reads, native usage counters and their limits.
- [Deterministic change-set plan, apply, audit and resume](reference/changesets.md) — the dependency-free change-set tool and its boundaries.
- [Bounded native history reads](reference/ao-history-reads.md) — smaller history pages when a native history exceeds the response limit.
- [Job progress](reference/progress.md) — the read-only per-job progress object: phase, elapsed time, last activity and deadline.
- [Current handoff state, worker heartbeat and recent activity](reference/status-followups.md) — handoff phase, heartbeat liveness and bounded activity windows.
- [Verification, acceptance and validation limits](reference/validation-limits.md) — how verification and acceptance differ from publication, and what the evidence does not establish.
- [DeepSeek delegate quick reference](reference/deepseek-quickstart.md) — the delegate's fixed transports, defaults, ledger semantics and probe commands.
- [Legacy controller](reference/legacy-controller.md) — the Codex controller's tools, timeouts, recovery lanes and legacy Qwen delegation.

## Operations

- [Recovery and evidence](operations/recovery.md) — reading saved state to continue or refuse interrupted jobs without replaying a model call.
- [Compaction-failure recovery](operations/compaction-failure-recovery.md) — the supported lane for one exact native autocompact-thrashing failure.
- [Retained AO provider and routing adoption](operations/provider-transition.md) — the audited one-time DeepInfra-to-official provider and routing adoption for existing AO rooms.
- [Qualification drafts](operations/qualification-draft.md) — the report-plus-draft workflow that turns a newer qualified family member into a validated, explicitly adopted qualification revision.

## Guard-referenced

- [Root Read admission in the deny-only routing guard](read-admission.md) — stays at this path because `ao_routing_guard.py` names it in operator messages; moving it would change the pinned guard bytes.
