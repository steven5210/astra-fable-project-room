# Verification, acceptance and validation limits

## Verification and acceptance versus publication

Verification runs the spec's argv gates directly, without a shell, with a pinned per-gate timeout (default 120 seconds, maximum 7200), in the candidate worktree. Gate programs are trusted project commands, not a sandbox; keep model calls and publication commands out of them. The fingerprint covers tracked and untracked committable files, deletions, symlinks, file modes, the index and HEAD, and is taken before the gates and after each gate, so a gate that changes candidate content invalidates the evidence immediately. Ignored files and external dependencies are outside it. Commit before final verification when a commit is part of the intended candidate; committing afterwards changes HEAD and needs fresh verification and review.

Acceptance validates an independent reviewer's completed JSON verdict against the unchanged spec, candidate and gate evidence. It does not merge, publish or deploy. Integration continues through normal repository tools under the scope you authorized, preserving unrelated changes and checking that the reviewed bytes are the ones integrated. A verifier that crashes before saving its receipt leaves a durable running record that blocks further mutation until you diagnose it; the adapter has no automatic verifier-recovery lane and never launches a replacement to bypass the uncertainty.


## Current validation limits

State what the evidence shows and no more:

- Offline tests establish the adapter contracts with fake model and MCP backends.
- Live local evidence on the installed AO setup has established the normal Fable/DeepSeek engineering workflow, independent Astra acceptance, and a native `pr-sonnet` plus `pr-opus` routing probe with no fallback and no nesting.
- A manual Claude `/compact` and one controlled interrupted-turn native stop and resume were validated separately on the installed AO setup: the same native session and candidate state, Fable at `max` retained, partial work retained, and the continuation applied exactly once.
- Those checks do not establish threshold-triggered automatic compaction, OS or daemon crash recovery, reconciliation of arbitrary or paid delegate interruptions, native-child Chrome access, or automatic Project Room recovery. Only the documented, explicitly authorized [outcome recovery paths](../guides/efficient-continuation.md) may continue a diagnosed eligible failure; other uncertain delivery stays blocked without replay.
- Routing states describe local configuration and observation, not enforcement. The prepared engineer's settings deny the automated review skills, and its guard refuses every other skill dispatch except the pinned browser skill inside `pr-opus`.
- A smaller context reading does not restore subscription quota; live receipts stay private and describe exactly what they verified.
- Engineering selectors, qualifications, audit/transition records and worker expectations describe configuration and retained evidence. They do not establish provider availability, account entitlement, a served model, effective effort, live adoption or a passed live verification.
