"""One-time execution-evidence amendment for the retained AO engineer.

This part is delivered once per retained AO engineer session, through
ao_workflow.PARTS like the other one-time workflow parts. It is
delivered text only: it grants nothing, changes no snapshot, and alters
no pinned model, budget, guard or delegate setting. Its explicit
task=<id> launch references are what the read-only progress view pairs
launches and units by; the view never infers a link from a label.
"""

VERSION = 1
PART = "execution_evidence_v1"
INSTRUCTION_SHA256 = "4636e2c8ed80f6e748c538485d1c788d4ba6b9e5734efa3ca1e0aa4b53ff3574"
INSTRUCTION = (
    "Execution evidence (reporting default; it grants no scope, execution permission, recovery or review "
    "allowance and changes no model, guard, budget or authority): when you launch a native worker with Agent "
    "for one bounded unit, put the token task=<id> — that unit's native task id, one task per launch — in the "
    "launch description, so the read-only progress view pairs the launch with its unit by that explicit "
    "reference and never by a matching label; a launch without the token stays unlinked and its unit's "
    "execution stays unknown. A returned handback is evidence received, not a completed unit: inspect it, then "
    "update that unit's task — stating which evidence exists — before you launch further work or write the "
    "final handback, and leave a failed, cut-off or unreturned worker's unit unfinished with its reason. The "
    "controller only reads these references; it never edits the task list, completes a unit, interrupts a "
    "worker or infers a link."
)
