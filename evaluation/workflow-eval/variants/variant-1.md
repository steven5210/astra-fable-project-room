# Variant 1: interface-and-provenance checklist (one staged instruction)

Exactly one change against the baseline: before the first engineer send of an attempt, the operator stages the text below once
with `ao_room_instruction_stage` (it is delivered once, with provenance, and dispatches no model). The specification, gates,
guards, models, effort, review budgets and acceptance are identical to the baseline.

## Staged instruction text (exact bytes between the markers)

<!-- BEGIN VARIANT-1 -->
Before you delegate implementation, do these three checks and record them in your report.
1. Restate the frozen interface from the specification in your own words. If a referenced contract, schema, section or
   upstream identity is missing from the packet or is inconsistent with the repository bytes, stop. Report it as a
   remaining gap with outcome changes_required. Do not infer or invent the missing part.
2. Final proof must exercise the production entry point. A test of a helper function alone is preliminary evidence, not proof.
3. Cite an upstream commit or digest only after a worker has verified it against the repository bytes. Record that
   verification in tests_reported. Never cite such a value from memory or from prose.
<!-- END VARIANT-1 -->
