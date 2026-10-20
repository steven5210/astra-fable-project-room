"""One-time evidence-closure amendment for the retained AO engineer.

This part is delivered once per retained AO engineer session, through
ao_workflow.PARTS like the other one-time workflow parts. It is
delivered text only: it grants nothing, changes no snapshot, and alters
no pinned model, budget, guard or delegate setting.
"""

VERSION = 1
PART = "evidence_closure_v1"
INSTRUCTION_SHA256 = "534caa9fa0ff6e082d04c038774472eb543df4c132e6f644aabaaaea12aa4f2a"
INSTRUCTION = (
    "Evidence closure v1 (engineering default; refines the lifecycle execution and closure, "
    "quality-first review and finding provenance defaults, whose frozen text stays in force; it "
    "grants no scope, execution permission, recovery or review allowance and changes no model, "
    "guard, budget or authority): before a lifecycle-sensitive unit is reported closed, give an "
    "evidence-based handoff assessment of its invariants — name the invariant, every production "
    "writer and entry point the unit actually touched, and the positive and adversarial proof "
    "exercised on the real production path — and state, per claim, which evidence kind backs it: "
    "applied change, worker-reported check, formal candidate verification or independent "
    "acceptance. Evidence that predates a later edit is stale for that edit, never carried forward "
    "silently. A required finding, gap or proof requirement stays open without a concrete basis: it "
    "closes only by citing the specific evidence that discharges it — a named test, a gate's actual "
    "output, or an inspected artifact with its digest — and a belief, a received result, an exited "
    "worker's silence or an unrelated passing check is never that basis; an item without it stays "
    "listed in remaining_gaps or review_findings with its reason, never marked resolved. Plan "
    "delegated work as connected units, not isolated outputs: each unit carries its parent "
    "requirement, the interfaces and verified anchors it depends on, the files it owns and the "
    "integration step that connects its result to the rest of the work; after integration, rerun "
    "the affected focused checks on the connected result before the handback, and no unit is "
    "reported done on its own evidence while its connection to the others is unverified. The "
    "read-only ao_report_validate operation (CLI or MCP) checks only the syntax and shape of one "
    "draft engineering report, lists every check it does not perform, writes nothing and calls no "
    "provider; a worker or the engineer may run it before a handback, but a successful result never "
    "shows authentication, delegate verification, readiness or acceptance, and the final engineering "
    "report keeps its existing format."
)
