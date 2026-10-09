"""One-time lifecycle-closure amendment for the retained AO engineer.

This part is delivered once per retained AO engineer session, through
ao_workflow.PARTS like the other one-time workflow parts. It is
delivered text only: it grants nothing, changes no snapshot, and alters
no pinned model, budget, guard or delegate setting.
"""

VERSION = 1
PART = "lifecycle_closure_v1"
INSTRUCTION_SHA256 = "d9db1bee4d3b65b301ab7f3508ca2d71a7b16de7f769626f78415f50715a22da"
INSTRUCTION = (
    "Lifecycle execution and closure (reusable engineering default for new and retained AO rooms; it grants no scope, "
    "execution permission, recovery or review allowance and changes no model, tier policy, guard, review budget "
    "or authority): every coupled lifecycle or invariant — a state machine, marker, lock, counter, ledger or "
    "delivery record together with all of its writers — has one accountable owner for the current work, and an "
    "independently assigned slice receives self-contained context, explicit interfaces and uncontested file "
    "ownership. Before fixing a defect, identify every production entry point, writer and caller of the affected "
    "path and the contracts and fixtures that depend on it; define a failing proof that exercises the real "
    "production path the fix changes, then show that same proof passing afterwards. A required concurrency, "
    "restart or unknown-delivery integration proof is never silently replaced by helper-level, mocked, "
    "constructed-state or expected-failure coverage; when a reproduction is unavailable, report the proof gap and "
    "its limits instead of inventing a failure or calling the unit closed. Before closing a bounded slice, "
    "integrate the dependent changes and run the meaningful combined checks affected by the final bytes; evidence "
    "that predates a later edit is stale for that edit. Keep failures, skips, partial handbacks and residuals "
    "visible; reuse existing evidence and avoid broad reruns without changed bytes or a concrete concern. A "
    "repeated correctness defect on the same invariant is a root-cause and lifecycle-design question routed "
    "through the residual escalation default with every writer named, not another unexplained local patch. "
    "Surface a minimal scope or contract decision as soon as it is identified, with the behavior, affected paths, "
    "compatibility implications, evidence and a recommendation. Stable deliverables and task lineage: the agreed "
    "specification's stable requirement labels anchor the work. Each unit records its parent requirement, and a "
    "unit discovered during the work is classified as defect, proof_gap, dependency or enhancement with its "
    "source finding; record this lineage as the keys req, kind, from, blocker, superseded_by and reason in the "
    "native task metadata, or as the same key=value tokens in the task description with one whitespace-free "
    "token per key and reason last on its line. Absent lineage is reported as unmapped, never inferred from "
    "labels. An obsolete pending or umbrella row is dispositioned superseded by naming an observed successor "
    "task id and the reason while its own status and history stay in place; a superseded row is never counted "
    "as completed, unmet behavior is never marked completed, residual work receives a new unit carrying its "
    "lineage, completed rows are never reopened and rows are never bulk-deleted. Optional enhancements stay "
    "outside acceptance unless approved in a revised specification. Report closure as measured counts and "
    "deltas, with required and enhancement units counted separately, together with the actual acceptance "
    "blockers, never as a task-based percentage or an invented denominator; carry the same labels, classifications "
    "and dispositions in the existing engineering report fields without adding "
    "report fields. Applied code, worker-reported checks, operator or formal candidate verification and "
    "independent acceptance remain distinct kinds of evidence."
)
