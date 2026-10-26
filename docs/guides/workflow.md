# Feature workflow

One room identifies a feature in a project. The room keeps the specification, issues, backlog, decisions, Claude session identity, jobs, handoffs, and acceptance evidence needed to resume work later.

Use the room from the Astra task where the feature is being shaped. That task retains its conversation history and reads Fable's responses from the room; no central Astra task or automatic forwarding is involved. Deliberately handing work to another task does not require a replacement room. Separate feature rooms can run concurrently, while each room keeps one active owner and its own saved job IDs. Account quotas and local model capacity remain shared.

1. **Intent:** Astra identifies the user problem, examples, constraints, and scope. If the request is to build, that authorization is carried forward; if it is to plan, the workflow stops at the agreed spec.
2. **Specification:** Astra grounds requirements in the current repository and writes a self-contained revision with acceptance criteria and executable verification. Reference-based behavior gets a probe in the plan.
3. **Independent interpretation:** Fable states what it thinks the feature means and proposes useful improvements with their benefits and tradeoffs. Findings distinguish blockers from optional enhancements.
4. **Resolution:** Astra evaluates product implications and records each finding's disposition and rationale. Astra surfaces enhancement proposals, files or links project GitHub issues under existing filing authorization, and asks the user for their opinion and scope approval. The room retains the proposal, issue link, and decision; unavailable filing stays explicitly pending. Fable owns routine engineering judgments. Only reviews that completed without accepting consume the debate budget: three non-accepting completed reviews exhaust it, while accepted reviews and failed or uncertain dispatches do not. Every dispatched review intent also counts toward the room's hard ceiling of five spec-review dispatches, which refuses regardless of decisions. After the debate budget is exhausted, further debate returns a focused product decision to the user. Recording the actual answer permits the next bounded round and retains all earlier attempts; existing agreement proceeds to handoff.
5. **Consensus:** Both agents accept the same revision and SHA-256, with blockers resolved. Changing the spec invalidates previous agreement.
6. **Handoff:** Astra binds existing implementation authorization and argument-array gates to the agreed spec. This record does not expand the user's scope or grant unrelated external actions.
7. **Implementation:** Fable chooses delegates under the fixed policy, supplies context, verifies returned code, runs gates and engineering reviews, and supplies evidence. Each room pins exactly one first-tier delegate provider (DeepSeek, legacy Qwen, or none) at creation, which selects the exact policy text Fable follows; Fable never substitutes another provider's settings or a different model silently, and reports unavailability instead. In an AO room this role belongs to the room's configured engineering orchestrator: the `fable` family alias by default, or an explicitly selected qualified engineer-role family or exact model, whose exact expected model the room's operator-selected source qualification pins before inference. The alias is configured family intent, not a resolved model, and configured `max` effort, an observed model id, provider availability and observed effective effort stay separate facts. A scope discovery creates a blocking issue; its resolution requires a strictly newer spec revision and renewed agreement before another handoff.
8. **Product acceptance:** Astra inspects delivered behavior and evidence independently. It records acceptance or specific findings. Known results can enter the diagnosed correction loop; uncertain delivery cannot. An implementation stopped only by the configured model-invocation timeout or the provider's session-usage-limit error can continue through the audited recovery workflow after the host restarts; the resulting successor still needs fresh gates and Astra's independent acceptance like any other implementation. In an AO room, a known owned terminal failure is settled or reconciled through the existing supported outcome tools (`ao_room_outcome_audit` / `ao_room_outcome_resume`) with actual user authority; an engineering-model transition or routing refresh is a separate boundary and never releases the hold or authorizes a new request.
9. **Delivery:** When the user requested the finished feature, Astra integrates the accepted work using normal repository tools, checks the pinned source baseline and current candidate evidence, preserves unrelated changes, and completes authorized review/publication steps. Integration that changes reviewed code requires renewed verification. A current effective undelivered committed root boundary is disclosed once on the next separately authorized normal engineer request, including a read-only specification review, with its committed authority digest; a current effective undelivered worker/routing boundary waits for delegation-capable implementation or correction work. Superseded intermediate boundaries and already authenticated completed deliveries are not replayed. Ordinary continuations carry only actual new user content, and uncertain delivery is never resent blindly.

Astra need not read every delegate transcript. It inspects the code, behavior, engineering verdict, gate output, and remaining risks, then drills into routing or delegate details when a concern requires it. For progress between syncs in an AO room, the read-only `ao_room_progress` view folds one request's task list, Agent launches and error counts from the registered native transcript — launches carry evidenced states (`running`, `unreturned`, `launched`, `completed`, `error`) and pair with a unit only through an explicit `task=<id>` reference in the launch description, never a matching label; AO's plan panel is driven by the same engineer task list, which the one-time `progress_plan_v3` workflow part asks Fable to keep current at unit boundaries (a task is one bounded unit, not a phase; residuals become new tasks; `in_progress` means active now). `progress_plan_v3` consolidates and supersedes the earlier `progress_plan_v1` and `progress_plan_v2` parts: their frozen text and digests stay valid history, but no session is ever sent them again. Fable remains accountable for every engineering verdict.

## Lifecycle execution and closure (AO rooms)

AO engineer sessions also receive the one-time `lifecycle_closure_v1` workflow
part (a new session in its first packet, a retained session once on its next
engineer send — see [lifecycle-closure delivery](efficient-continuation.md#lifecycle-closure-delivery-ao-rooms)
for exactly when). It is a reusable engineering default for how Fable executes
and closes lifecycle-sensitive work in an AO room; delivered text only, it grants
no scope, execution permission, recovery or review allowance and changes no
model, tier policy, guard, review budget or authority. The default, in summary:

- Every coupled lifecycle or invariant — a state machine, marker, lock, counter,
  ledger or delivery record together with all of its writers — has one
  accountable owner for the current work, with self-contained context, explicit
  interfaces and uncontested file ownership for any independently assigned slice.
- Before fixing a defect, Fable identifies every production entry point, writer
  and caller of the affected path, then defines a failing proof on the real
  production path the fix changes and shows that same proof passing afterward. A
  required concurrency, restart or unknown-delivery integration proof is never
  silently replaced by helper-level, mocked, constructed-state or
  expected-failure coverage; when a reproduction is unavailable, the proof gap
  and its limits are reported rather than invented or called closed.
- Before closing a bounded slice, the dependent changes are integrated and the
  meaningful combined checks affected by the final bytes are run — evidence that
  predates a later edit is stale for that edit — and failures, skips, partial
  handbacks and residuals stay visible.
- A repeated correctness defect on the same invariant is a root-cause and
  lifecycle-design question routed through the [residual escalation
  default](../../skills/project-room/references/fable-policy.md#residual-escalation-amendment-one-time-part-residual_escalation_v1)
  with every writer named, not another unexplained local patch.
- A minimal scope or contract decision is surfaced as soon as it is identified,
  with the behavior, affected paths, compatibility implications, evidence and a
  recommendation, feeding the existing resolution step above rather than
  replacing it.
- The agreed specification's stable requirement labels anchor the work. Each
  unit discovered during the work records its parent requirement and is
  classified as defect, proof_gap, dependency or enhancement with its source
  finding, carried as task lineage (see [deliverables](../reference/progress.md#deliverables-lineage-and-closure)
  for the exact grammar). Absent lineage is reported as unmapped, never inferred
  from labels. An obsolete unit is dispositioned superseded by naming an observed
  successor task and the reason, while its own status and history stay in place;
  a superseded row is never counted as completed, residual work gets a new unit
  carrying its own lineage, and completed rows are never reopened or bulk-deleted.
  Optional enhancements stay outside acceptance unless approved in a revised
  specification.
- Closure is reported as measured counts and deltas — required and enhancement
  units counted separately — together with the actual acceptance blockers, never
  as a task-based percentage or an invented denominator. Applied code,
  worker-reported checks, operator or formal candidate verification and
  independent acceptance remain distinct kinds of evidence.

This default is scoped to AO rooms; legacy `room_*` rooms are unchanged by it.
Its lineage labels, classifications and dispositions ride inside the *existing*
engineering report string-list fields (`changes`, `remaining_gaps`, `backlog`,
`review_findings`, `routing_log` entries) — no report field was added. Like the
other reusable instructions this guide and the Fable policy reference describe,
it is a global default that takes effect in a room only after the updated plugin
has been installed or resynced (see [Install and authenticate](../../README.md#install-and-authenticate));
existing room snapshots and historical records already saved are not rewritten.

## Draft report validation and evidence closure (AO rooms)

The read-only `ao_report_validate` tool checks only the syntax and shape of one
draft engineering report — the full report form or the compact
`project_room_engineering_v2` form — against the same shared schema authoritative
final capture uses. It takes one argument, `report_text` (a bounded string, never
a path: at most 1,000,000 UTF-8 bytes, nesting depth at most 64), needs no
room, provider or authentication, and is available from both the CLI and the
bundled MCP server, where it is listed read-only. The exact local CLI form:

```sh
python3 project_room.py call ao_report_validate --args '{"report_text": "{\"report_format\": \"project_room_engineering_v2\", \"outcome\": \"completed\", \"implementation_complete\": true, \"changes\": [], \"tests_reported\": [], \"review_findings\": [], \"remaining_gaps\": [], \"backlog\": [], \"routing_log\": []}"}'
```

and, to avoid shell escaping for a large draft, the `--args-file` variant with a
JSON file containing `{"report_text": "..."}`:

```sh
python3 project_room.py call ao_report_validate --args-file /absolute/path/to/args.json
```

(`--home`, when given, is a global option that precedes the subcommand —
`python3 project_room.py --home /absolute/controller-home call ao_report_validate ...`
— because the `call` subcommand does not re-accept it after `call` itself.)

The result object reports `operation`, `validity_scope` (always
`syntax_and_shape_only`), `syntax_shape_valid` (bool), `report_form` (`full`,
`project_room_engineering_v2`, `unsupported`, or `null`), `diagnostics` (a list of
`{field, message}` entries that block validity), `advisories` (a list of
`{field, message}` entries that never change validity — a malformed
`enhancement_proposals` entry is always advisory), `unperformed_checks` (always
the same named list, naming deferred authoritative checks this call never
performs, always including semantic correctness assessment, formal candidate
verification and independent acceptance, even for a completed/true draft with
no remaining gaps — a reminder of named checks, never an exhaustive contract:
`handoff_authentication`, `spec_identity_equality`,
`compact_contract_delivery`, `controller_metadata_attachment`,
`delegate_attribution_verification`, `engineering_readiness`,
`findings_projection`, `capture_receipts`, `candidate_binding`,
`native_outcome_evidence`, `correction_chain`, `issue_dispositions`,
`candidate_unchanged`, `semantic_correctness_assessment`,
`formal_candidate_verification`, `independent_acceptance`; readiness also
refuses a report entry explicitly tagged `scope=required disposition=open`),
and `limits`
(`max_text_bytes`, `max_depth`, `max_diagnostics`, `truncated`). Admission
failures — arguments other than exactly `report_text`, or a `report_text` that
is a non-string, empty, oversized or invalid-UTF-8, or nesting past the
admitted depth — are refusals, never diagnostics, and every refusal carries a
fixed, bounded payload with that same field set (`syntax_shape_valid: false`,
`report_form: null`, empty `diagnostics`/`advisories`, the full
`unperformed_checks` list and `limits`) plus `error`, never echoing the
caller's supplied argument names, values or paths: the API raises
`DraftAdmissionError` with that object on its `.payload` attribute, the CLI
exits 2 with that object on stderr, and MCP returns an `isError` result whose
`content` text and `structuredContent` are that same object. A malformed
syntax or root shape is instead a diagnostic on field `$`, in a normal
(non-refusal) result. The full form keeps whatever unknown fields it is given, including a
model-supplied `controller_metadata`; the compact form refuses a model-supplied
`controller_metadata` exactly as the existing compact-contract projection does.

The operation itself writes nothing, creates no home, config or registry, and
calls no provider from any public entry: the CLI preflight runs before
controller construction; `Service.call` dispatches it, through its own bounded
argument admission, before the shared `tool_arguments` check every other tool
goes through and before any room or home access; and the MCP `tools/call`
dispatch consults the shared legacy-tools gate
(`project_room.Service.legacy_tools_visible`) only for a legacy `room_*` tool
name, so a draft validation never opens or creates the local SQLite room
registry, including on a configured home whose saved config selects the `ao`
backend and whose registry file is missing. That is a fact about this
operation, not a claim that the whole CLI or MCP process never touches the
filesystem: ordinary controller-home provisioning at MCP process startup is
pre-existing and unchanged; the `initialize` and `tools/list` methods still
consult the same gate as before (pre-existing listing behavior outside this
operation); and running any `project_room.py` command through the `python3`
interpreter can let it write its own `__pycache__` bytecode for imported
modules (already git-ignored, avoided with `PYTHONDONTWRITEBYTECODE=1`), never
a room-state or controller-home write.

The refusal boundary is exact: a request is inside the operation once an
`arguments` value — any JSON value, including a non-object — reaches the
endpoint through `Service.call`, through MCP `tools/call` naming this tool, or
through the CLI preflight given any `--args`/`--args-file` text, including
malformed JSON text; every refusal from that point on is the same fixed
payload described above. A refusal raised before that point is pre-existing,
unchanged, and carries no payload, because nothing reached the endpoint: the
MCP transport's `-32600 Request exceeds maximum size` for an oversized line,
its `-32700 Invalid JSON`, its invalid request/id/params and unknown-method
errors, the generic `Unknown tool` dispatch error for a name that is not this
endpoint, and the CLI's historical generic error for an unreadable
`--args-file`.
A passing result is never evidence of handoff authentication, delegate
attribution, engineering readiness or independent acceptance, and it changes
nothing else: the final engineering report keeps its existing format regardless
of whether this tool ran. A qualified worker or the operator invokes it on a
draft before a handback, as a preflight syntax/shape check, and the engineer
reads the result; the engineer's own guard is unchanged and admits no shell,
CLI or MCP execution. An operator may run it on a malformed engineer handback
to get field-level diagnostics before starting the correction loop; it is
never a substitute for capture, verification or independent acceptance, and
the operator still inspects the actual evidence needed for those steps.

The companion evidence-closure default asks Fable to ground every closure
claim in named evidence: before reporting a lifecycle-sensitive unit closed,
give an evidence-based handoff assessment that names, per invariant, its
authoritative truth source (the final production path and saved state that
decide it) and every reader, writer and interface the unit affects; an intact
positive control exercised through the public entry point; an independently
varied invalid case shown to reach the intended check rather than an earlier
or unrelated one; the actual effect of each refusal on files, saved state,
allowances, consumption and dispatch wherever those could change; and the
assumptions the proof rests on and how they compose. State per claim which
evidence kind backs it: applied change, worker-reported check, formal
candidate verification, or independent acceptance. Evidence that predates a
later edit is stale for that edit and is reused only where the edit
demonstrably leaves it unaffected, with that reason stated. A required
finding, gap or proof requirement closes only by citing the specific evidence
that discharges it (a named test, a gate's actual output, or an inspected
artifact with its digest); calling it pre-existing, private-store, backlog or
out of scope leaves it open unless a concrete contract or evidence reason is
given, and without such a basis it stays listed in remaining_gaps or
review_findings with its reason, while a justified non-blocking limitation
may be recorded as such. Resolve known required correctness and contract
questions before expensive formal verification is requested. Known related
defects are consolidated into complete connected units — each with one
accountable owner, explicit interfaces and file ownership, its parent
requirement and verified anchors, a challenging proof, and the integration
step that connects its result to the rest — and after integration the
connected result gets the affected focused checks again before the unit is
reported done; no unit is reported done on its own evidence while its
connection to the others is unverified. This adds no report schema or
checklist, and no backlog item blocks by itself; the read-only, offline
`ao_report_validate` operation names deferred authoritative checks it never
performs, a qualified worker or the operator invokes it before a handback, and
the engineer reads the result.

AO engineer sessions receive this guidance once, as the one-time
`evidence_closure_v1` workflow part: a new session receives it in its first
packet, and a retained session receives it once, carried on its next ordinary
engineer send (`spec_review`, `implementation`, or `correction`). A read-only
status read, `setup`, `ao_room_prepare`, or a paused room never dispatches it by
itself; only an actual engineer send does. It refines the already-merged
lifecycle execution and closure, quality-first review, and finding provenance
defaults above — their frozen text and digests stay exactly as recorded, and
this part supersedes none of them. See [lifecycle-closure
delivery](efficient-continuation.md#lifecycle-closure-delivery-ao-rooms) for the
exact delivery timing it shares, and the [Fable policy reference's matching
section](../../skills/project-room/references/fable-policy.md#evidence-closure-one-time-part-evidence_closure_v1)
for the exact delivered text. This default is scoped to AO rooms; legacy
`room_*` rooms are unaffected by it. Like the other reusable instructions in this
guide, it is a global default that takes effect in a room only after the updated
plugin has been installed or resynced (see [Install and
authenticate](../../README.md#install-and-authenticate)); existing room
snapshots and already-delivered workflow-part records are not rewritten.

## Enhancement issue handoff

Under the user's existing filing authorization, Astra opens or links a GitHub issue for each worthwhile enhancement in the feature project's repository. Check both open and closed issues and the current implementation first; reuse an existing issue and record completed or superseded proposals rather than opening stale duplicates. A local backlog entry alone is not a filed issue.

Inspect the repository's labels and apply `enhancement` (or its established equivalent), relevant feature/area labels, and an accurate workflow status where that taxonomy exists. For example, a research proposal awaiting a scope decision can use existing `enhancement`, `research`, feature-area and `pending` labels. Preserve existing labels; do not invent urgency, mark unapproved work ready/in-progress, or assume issue creation approves implementation. If a needed label is unavailable, use the closest established label and state the missing classification explicitly in the issue body.

Make each issue usable for pickup: describe the problem and evidence, proposed outcome, benefit, tradeoff, acceptance criteria, dependencies and scope-decision status. Keep private project material in the appropriate private tracker. Fable supplies proposal data; Astra checks the content and performs issue operations through trusted tools.

Read back the issue after creation or labeling. Retain its verified URL, labels, source proposal and user-decision status in the room's proposal record or linked operator artifact, and show the user the issue link. If creation or labeling is blocked, preserve the draft and report exactly what remains pending; do not claim filing is complete.

Filing an enhancement issue is separate from approving its implementation. Continue agreed work while awaiting any needed scope decision. Approved scope is recorded in a revised specification and reviewed before implementation; optional ideas must not disappear silently into a backlog.

The shared room avoids using chat history as the only source of truth. Existing app conversations remain available; the plugin resumes dedicated worker sessions and explicit room records. The MCP server is a local tool surface, and the skill in the active Astra conversation drives the workflow. Jobs can outlive the connection, but the plugin does not create a future Astra wakeup automatically.
