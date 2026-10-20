"""One-time evidence-closure amendment for the retained AO engineer.

This part is delivered once per retained AO engineer session, through
ao_workflow.PARTS like the other one-time workflow parts. It is
delivered text only: it grants nothing, changes no snapshot, and alters
no pinned model, budget, guard or delegate setting.
"""

VERSION = 1
PART = "evidence_closure_v1"
INSTRUCTION_SHA256 = "f7b58c915e3d566332f0e612fb7bea521455ae044342838360929cf85d1fa0a1"
INSTRUCTION = (
    "Evidence closure v1 (engineering default; refines the lifecycle execution and closure, "
    "quality-first review and finding provenance defaults, whose frozen text stays in force; it "
    "grants no scope, execution permission, recovery or review allowance and changes no model, "
    "guard, budget or authority): before a lifecycle-sensitive unit is reported closed, give an "
    "evidence-based handoff assessment that names, per invariant, its authoritative truth source "
    "(the final production path and saved state that decide it) and every reader, writer and "
    "interface the unit affects; an intact positive control exercised through the public entry "
    "point; an independently varied invalid case shown to reach the intended check rather than an "
    "earlier or unrelated one; the actual effect of each refusal on files, saved state, "
    "allowances, consumption and dispatch wherever those could change; and the assumptions the "
    "proof rests on and how they compose. State per claim which evidence kind backs it: applied "
    "change, worker-reported check, formal candidate verification or independent acceptance. "
    "Evidence that predates a later edit is stale for that edit and is reused only where the edit "
    "demonstrably leaves it unaffected, with that reason stated. A required finding, gap or proof "
    "requirement closes only by citing the specific evidence that discharges it (a named test, a "
    "gate's actual output or an inspected artifact with its digest); calling it pre-existing, "
    "private-store, backlog or out of scope leaves it open unless a concrete contract or evidence "
    "reason is given, and without such a basis it stays listed in remaining_gaps or "
    "review_findings with its reason, while a justified non-blocking limitation may be recorded "
    "as such. Resolve known required correctness and contract questions before expensive formal "
    "verification is requested. Consolidate known related defects into complete connected units, "
    "each with one accountable owner, explicit interfaces and file ownership, its parent "
    "requirement and verified anchors, a challenging proof and the integration step that connects "
    "its result to the rest; after integration, rerun the affected focused checks on the "
    "connected result before the handback, and report no unit done on its own evidence while its "
    "connection to the others is unverified. This adds no report schema or checklist, and no "
    "backlog item blocks by itself. The read-only ao_report_validate operation (CLI or MCP) "
    "checks only the syntax and shape of one draft engineering report, names deferred "
    "authoritative checks it never performs, writes nothing and calls no provider; a qualified "
    "worker or the operator invokes it before a handback and the engineer reads its result, "
    "because the engineer's own guard admits no shell, CLI or MCP execution; a successful result "
    "never shows semantic correctness, authentication, delegate verification, readiness, formal "
    "candidate verification or independent acceptance, and the final engineering report keeps its "
    "existing format."
)
