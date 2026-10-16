"""One-time finding-provenance amendment for the retained AO engineer.

This part is delivered once per retained AO engineer session, through
ao_workflow.PARTS like the other one-time workflow parts. It is
delivered text only: it grants nothing, changes no snapshot, and alters
no pinned model, budget, guard or delegate setting.
"""

VERSION = 1
PART = "finding_provenance_v1"
INSTRUCTION_SHA256 = "d67ce9529889ce53b817988c386774148eb92909dcec76d6ea6faa78eb2ad8ec"
INSTRUCTION = (
    "Finding provenance (reporting default; it grants no scope, execution permission, "
    "recovery or review allowance and changes no model, guard, budget or authority): in "
    "review_findings, remaining_gaps and backlog, tag each entry with whitespace-free tokens "
    "origin=introduced|preexisting|integration, scope=required|optional, "
    "disposition=fixed|open|deferred|decision_needed, and when known invariant=<name>, "
    "evidence=<reference> and owner=<unit or area>. A preexisting or integration issue that "
    "prevents the approved behavior from working is scope=required and stays a remaining gap; "
    "unrelated cleanup is scope=optional and stays separate. An ordinary in-scope fix needs no "
    "product decision: reserve disposition=decision_needed for an actual scope or contract "
    "choice. An untagged entry is read as unknown, never as resolved or optional. These tokens "
    "change no report field and grant nothing; the controller only reads them."
)
