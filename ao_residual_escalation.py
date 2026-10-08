"""One-time residual-escalation routing amendment for the retained engineer.

This part is delivered once per retained engineer session like the other
one-time workflow parts. It is delivered text only: it grants nothing,
changes no snapshot, and alters no pinned model, budget, guard or delegate
setting.
"""

VERSION = 1
PART = "residual_escalation_v1"
INSTRUCTION_SHA256 = "81fa95903c9fc91d60370ab2456ee5f749964bc0f341be5b49a5299a33a556ed"
INSTRUCTION = (
    "Residual escalation (routing default among the room's pinned workers; it changes no model, budget, guard or "
    "delegate setting): route concurrency-sensitive, financially precise or security-sensitive units — "
    "reservations, cancel/replace, locks, recovery and reconciliation, race and contention proofs, money or "
    "quantity arithmetic — to pr-opus from the first round, not after a cheaper attempt. For any one area, after "
    "two rounds whose review found correctness defects, route the next round to pr-opus and record the reason in "
    "routing_log; mechanical breakage (fixture repair, type or token alignment, a renamed symbol) and a quota "
    "cut-off do not count as rounds. Diagnosis before escalation still applies: the packet names the defect "
    "class and the exact functions; never resend the same packet to a stronger model without that diagnosis. "
    "Quality-first routing is unchanged: route up when in doubt, and record every routing outcome."
)
