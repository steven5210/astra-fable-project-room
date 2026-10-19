# Offline workflow evaluation package

This package prepares a bounded, offline evaluation of one Project Room workflow change. It contains frozen tasks, one held-out
case, two variants, deterministic checks, a scorer and a dry run. It runs no model. Execution against models needs a separate
authorization that names the caps in [`caps.json`](caps.json).

Status: prepared, not executed. Nothing in this package touches a live room, Han, credentials or native transcripts.

## Method

The method follows the Harness² separation of concerns as input, not as a dependency: keep the model fixed, change exactly one
harness component, probe the change on identical tasks, and keep the judge outside the optimization loop
([google-research/harness2](https://github.com/google-research/harness2)). The package does not adopt the Harness² runner. Its
benchmarks, executors and recursion loop target other agents and tasks, and one small standalone plan is cheaper to review and to
freeze. The parts that transfer are:

- Fixed weights and one edit. The exact engineering model, MAX effort, the qualified worker expectation, the routing guards, the
  review budgets and independent acceptance stay identical across variants. The only difference is one staged instruction.
- Contrastive probing on the same inputs. Each case runs once per variant with byte-identical specification, fixture, gates and caps.
- No ground-truth leakage. The held-out case `H1` has a sealed expected outcome. The workflow improver sees the inputs, not the
  expectation. The scorer unseals it at scoring time and verifies the commitment digest.
- Deterministic checks first, judgment second. Candidate probes and record checks produce the correctness verdict. The
  independent Astra acceptance review is recorded as a judge dispatch and a cost. It is not an optimization target.

## Frozen tasks

Each task directory holds `task.json`, `spec.md` and a synthetic `fixture/` repository. The digests are pinned in
[`tasks/manifest.json`](tasks/manifest.json). A change to a frozen task is a new case id, never an edit.

| Case | Failure class | Expected outcome | Deterministic checks |
|---|---|---|---|
| [T1](tasks/T1-incomplete-handoff/task.json) | Incomplete handoff or context: the referenced report schema is absent from the packet | `gap_reported` | gap named in the report, no invented contract committed, frozen files unchanged, no acceptance |
| [T2](tasks/T2-helper-only-proof/task.json) | Helper-only proof: tests prove a helper but not the production entry point | `accepted` | gates pass, tests start the real entry point, stub probe makes the tests fail, frozen files unchanged, acceptance recorded |
| [T3](tasks/T3-integration-provenance/task.json) | Integration and provenance: cite a verified upstream identity and keep the import untouched | `accepted` | upstream file unchanged, commit and digest cited verbatim with a recorded verification, stub probe, frozen files unchanged, acceptance recorded |
| [H1](tasks/H1-heldout/task.json) | Held out (same family as T3) | sealed | listed in `task.json`; the required subset and the expected outcome are sealed in `expected.sha256` |

The sealed plaintext for `H1` is kept outside the repository. Its SHA-256 is `tasks/H1-heldout/expected.sha256`.

## Variants

- [`variants/baseline.md`](variants/baseline.md): the installed controller at the recorded commit, no staged instruction.
- [`variants/variant-1.md`](variants/variant-1.md): the same controller plus one instruction staged once before the first engineer
  send with `ao_room_instruction_stage`. The exact bytes are `variants/variant-1.instruction.txt`; the digest is pinned in
  [`variants/manifest.json`](variants/manifest.json).

There are exactly two variants. Specifications, guards, authorization boundaries, review budgets and independent acceptance are
fixed and are not optimization targets.

## Execution protocol (not authorized yet)

One attempt is one ordinary `fable_engineering` AO room on an isolated controller and daemon. The protocol is the normal workflow:

1. Create a fresh synthetic repository from the case fixture at a baseline commit. Open the room with the exact qualified
   engineering model and the attempt's account profile. Prepare before launch, bind the engineer and the reviewer, and register
   the native outcome source.
2. For `variant-1` only, stage the instruction once with `ao_room_instruction_stage` before the first send.
3. Pin the frozen specification with the case gates. Send the spec review. Record attribution, served-model and routing evidence
   as in the Part 2 trial.
4. Hand off and send the implementation. Allow at most one correction. Drain through supported sync.
5. Verify, then send one acceptance review. A rejected acceptance is a recorded failure. There is no second review.
6. Run `checks/probes.py --case <id> --candidate <worktree>` (disposable copy only), then
   `checks/export_bundle.py` with the probe output to produce the attempt bundle. Score all bundles with `checks/score.py`.

Attempts run sequentially, or at most two at a time on distinct account profiles. The order is fixed before execution and recorded.
Interleaving does not change inputs. Observed overlap is reported as an interval, never as a speedup.

## Metrics

Per attempt: `accepted_correctness` (true, false or undetermined), `correction_rounds`, `integration_effort` (engineer
dispatches, judge dispatches, worker launches, elapsed minutes), `usage` split into parent, worker and judge with unknown items
counted separately, and `elapsed_minutes`. Failures, stops and skips stay in the table with their status. Unknown usage is never
zero. Per-variant numbers are counts over at most three cases and carry no significance claim.

## Proposed caps

See [`caps.json`](caps.json). In short: 3 cases x 2 variants = 6 execution attempts; per attempt at most 3 parent dispatches
(spec review, implementation, one correction), 2 native worker launches with one concurrent worker, 1 judge dispatch, 80,000
parent output tokens, 60,000 worker output tokens per launch, 800,000 judge input and 15,000 judge output tokens, and 30 minutes
from first dispatch to the acceptance settle. Totals: 18 parent dispatches, 12 worker launches, 6 judge dispatches, 480,000 parent
output tokens, 90,000 judge output tokens, 4 hours. No retries, no automatic expansion, no quota continuation, no account rotation,
no replacement rooms, no overage and no new cloud or API spend. The held-out case is not in the 6 attempts; running it needs its
own authorization line (2 attempts). Variant authoring used no model calls. Judging costs are the 6 acceptance reviews.

## Dry run

```
python3 evaluation/workflow-eval/dry_run.py
python3 evaluation/workflow-eval/dry_run.py --unseal <path to the sealed H1 plaintext>
```

The dry run verifies the frozen digests, the two variants, the commitment file and the caps; it runs the probes on two synthetic
`T2` candidates (one that exercises the entry point and one helper-only candidate) and scores the synthetic bundles in
[`fixtures/dry-run/`](fixtures/dry-run/), including a stopped attempt with unknown usage and the held-out example. It needs no model
credentials and makes no network call. Exit code 0 means every step passed.

## Limitations

- Three cases cannot estimate a rate. The result is a table of observations, not a measurement of the variant's effect.
- The held-out inputs are visible; only the expectation is sealed. An improver who reads the fixture can still guess the trap.
- Deterministic checks detect the modeled failure patterns only. A passing table does not prove general quality.
- Worker usage is unknown unless the controller exposes it. Cached tokens combine reads and writes and are not quota.
- Configured account identity is not provider billing attribution. Elapsed time depends on provider latency.
- The probes replace an entry point with a stub in a disposable copy. They do not prove test quality beyond that dependency.
