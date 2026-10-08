"""One-time residual-escalation routing amendment for the retained engineer.

This part is delivered once per retained engineer session like the other
one-time workflow parts. It is delivered text only: it grants nothing,
changes no snapshot, and alters no pinned model, budget, guard or delegate
setting.
"""

VERSION = 1
PART = "residual_escalation_v1"
INSTRUCTION_SHA256 = "86a1faed57c2119fc492182508cec813d7704318382c11db9380219e81b190e4"
INSTRUCTION = (
    "Residual escalation (routing default among the room's pinned workers; it changes no model, budget, guard or "
    "delegate setting): route concurrency-sensitive, financially precise or security-sensitive units — "
    "reservations, cancel/replace, locks, recovery and reconciliation, race and contention proofs, money or "
    "quantity arithmetic — to pr-opus from the first round, not after a cheaper attempt. For any one area, after "
    "two rounds at the same tier whose review found correctness defects, treat the next round as a capability "
    "miss: route it one tier up — pr-sonnet to pr-opus, pr-opus to Fable with the recorded capability reason — "
    "and record the reason in routing_log; mechanical breakage (fixture repair, type or token alignment, a "
    "renamed symbol) and a quota cut-off do not count as rounds. The pinned policy is unchanged: diagnose before "
    "escalating, name the defect class and the exact functions in the packet, never resend the same packet to a "
    "stronger tier without that diagnosis, and after two failed tiers on one subtask Fable takes over. "
    "Quality-first routing is unchanged: route up when in doubt, and record every routing outcome."
)
