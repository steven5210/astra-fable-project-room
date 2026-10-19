# CI test timing: records, shard weights and drift warnings

The offline suite runs in CI as five module-level shards per platform
(Linux on Python 3.11 and 3.12, macOS on 3.11). Every shard job records how
long each test took, the records are kept as artifacts, and a diagnostic job
turns them into a summary with queue, setup and execution time per job,
the slowest modules, the shard imbalance and a comparison with recent `main`
runs. Nothing in this chain is a required check: the unittest jobs remain the
gates, and the timing job can only warn.

## What is measured

`tests/timed_unittest.py` runs the same modules as `python -m unittest`
(identical loader, identical console output) and writes one JSON record per
shard, format `project_room_test_timing_v1`:

- per test: monotonic seconds, outcome, and the `setUp` / test body /
  `tearDown` split (taken from unittest's `_callSetUp`, `_callTestMethod`
  and `_callTearDown` hooks; `null` when a Python version lacks them);
- per module: wall seconds from its first test start to its last test end,
  the summed test seconds, the summed phase seconds, and
  `fixture_gap_seconds` = wall − tests, which is the time spent in later
  classes' `setUpClass` / `tearDownClass` and in module-level fixtures. The
  first class's `setUpClass` runs before the first test starts and is outside
  the wall, the same blind spot the verbose log timestamps have;
  under the worker pool the merged record also carries `process_seconds` per
  module -- the whole worker process wall, which is what the weights use, so
  the first-class-`setUpClass` blind spot applies only to `wall_seconds`;
- provenance: Python version, OS, machine, the source revision
  (`PROJECT_ROOM_TIMING_REVISION`, the PR head or the pushed commit), the run
  id and the shard index.

Interrupted runs still write a record (`complete: false`) with the rows that
finished. Records are bounded (20,000 test rows, 300-character ids) and are
never used as paths, commands or format strings by the summary job.

Queue time (`started_at − created_at`), runner setup (test step start − job
start) and execution (test step duration) come from the GitHub jobs API, not
from the record, so they are available even for a job whose record is
missing.

## Where the artifacts are

| artifact | producer | retention | content |
| --- | --- | --- | --- |
| `timing-<os>-py<version>-shard<i>` | each unittest job, also on failure | 30 days | the shard's record |
| `timing-summary` | the `timing-summary` job | 90 days | `timing-combined.json`: per job queue/setup/exec/total plus the record's module aggregates (no per-test rows) |

The `timing-summary` job writes the Markdown summary to the run's step
summary page. It needs only `actions: read` on the default token.

## Shard weights

`tests/shard_weights.json` (format `project_room_shard_weights_v2`) pins one
weight per discovered test module: the median macOS/3.11 module *process*
seconds under the worker pool (one process per module, so imports, class
fixtures and teardown are included), falling back to module wall seconds
for single-process records -- over the runs listed in `_meta.sources`, with
`samples` = the number of jobs that measured it. In the timing summary,
compare a weight with the `cost (process|wall)` column of the Slowest
modules table, which uses the same process-first metric.
`tests/run_test_shards.py` partitions modules by longest
processing time first over these weights; the assignment is deterministic
and `test_run_test_shards.py` pins that the union of the shards equals the
discovered inventory exactly, at the test-id level, with no id discovered
twice.

A discovered module without a weight is a hard failure only in the pin
test (`test_run_test_shards.test_every_discovered_module_has_a_pinned_weight`)
and in `--check-weights` (exit 2); the runner itself (`--shard`, `--jobs`,
`--list`) warns on stderr and schedules the module at the median pinned
weight, so an unpinned module does not turn every shard job red and the
other shards still run and record. The fix command is printed either way:

```
python3 tests/run_test_shards.py --measure test_new_module
```

`--measure` runs the module once locally through the timed runner and pins
its wall seconds with `samples: 0`, which marks it as a local estimate until
a CI refresh replaces it. `--check-weights` reports coverage and the local
estimates. A PR that adds a test module pins its weight in the same PR; when
`main` gains a module, an open PR fails the pin test until it merges `main`
and runs `--measure` (or a refresh).

### Refreshing from CI

Download the `timing-summary` artifact of one or more recent `main` runs and
run:

```
python3 tests/run_test_shards.py --refresh-weights timing-combined.json [more...]
```

Only complete macOS/3.11 job records are used; the weight is the median
across all of them -- of the module's process samples when any input run
measured them, else its wall samples, never mixed -- so pass several runs
to average out runner variability
(the same module varied by 30–56 % between two macOS runs twelve hours
apart). A module absent from every input keeps its previous value and is
listed as kept; entries for modules that no longer exist are dropped. Review
the printed change table and commit the file like any other change. CI never
rewrites it.

Refresh when the summary warns about imbalance or stale estimates, after a
fixture change that materially alters a module's cost, or after adding
several modules.

## Reading the summary and its warnings

Warnings appear in the step summary and as workflow annotations; they never
fail the run.

- **Shard imbalance**: per OS/Python, `max / median` execution time above
  1.5 with the slowest shard more than three minutes over the median. Fix:
  refresh the weights.
- **Stale estimates**: a module whose measured macOS cost (process seconds
  under the pool, else module wall) differs from its
  weight by more than a factor of two and more than 30 seconds
  (`run_test_shards.stale_estimates`). Fix: refresh the weights.
- **Local estimates**: modules still at `samples: 0`. Fix: refresh after the
  next `main` run that contains them.
- **Slower than baseline**: a module above the rolling `main` median by more
  than a factor of 1.5 and more than 60 seconds, compared only against
  baseline runs of the same measurement kind (process vs wall). The
  baseline is the median
  over the last ten successful `main` runs that have a complete macOS/3.11
  record; the sample size is printed next to every comparison, and a
  comparison with fewer than three same-kind samples is reported as
  inconclusive. A
  single slow run is runner noise as often as a regression; the warning
  says `sustained` only when the previous two `main` runs were also above
  the threshold.

Queue time is reported but is not something the suite controls: it grows
with the number of macOS jobs waiting across all open PRs. Same-PR
superseded runs are already cancelled; the lever for queue time is fewer
macOS runner-minutes per run.

## In-job workers

Each shard job runs its modules through a bounded pool of worker processes
(`--workers K`, one module per process, longest estimated module first).
The chosen counts are 2 on macOS and 3 on Linux -- one core of headroom on
the three- and four-core hosted runners for the runner itself, subprocess
bursts and the OS. Modules never share a process, every test keeps its own
temporary directories, and the shard's record is the merge of the per-module
records. `--workers 1` restores the previous single process for the whole
shard.

Exclusive modules: on Linux, the modules that launch the legacy controller's
verification job (`test_verification`, `test_verification_matrix`, the
`EXCLUSIVE_MODULES` set in `tests/run_test_shards.py`) run alone in the pool.
The job's gate observation and cleanup scan every same-user process through
/proc, and a transient process of another worker -- between exit and reap,
or mid-exec -- reads as `inspection_incomplete` to the fail-closed scan.
macOS is unaffected: the scan there parses `/bin/ps -E`, which cannot
distinguish an unreadable environment from an empty one, so it is never
`inspection_incomplete` (a documented platform limit in `verification.py`) --
the exclusivity is Linux-only for that reason. A new
module that drives that path must be added to the set; the symptom of
forgetting is a deterministic `inspection_incomplete` refusal in that module
under the pool, never a silent pass.

## Rollback

Every piece is independent:

- `--workers 1` in `.github/workflows/tests.yml` returns to one process per
  shard;
- removing `--record` returns the shard to plain `python -m unittest`; the
  summary job then reports every record as missing and still shows the
  job-level times;
- the weights file is ordinary committed data: `git checkout <rev> --
  tests/shard_weights.json` restores any earlier v2 revision of the file (the
  runner refuses the old v1 format), as long as every discovered module keeps
  a weight;
- the fixture templates (`TEMPLATE_SETUP`) are per class; setting
  `TEMPLATE_SETUP = False` on a fixture class returns it to building the room
  in every test.

## Limits

- Sample sizes are small: a handful of runs supports medians and ranges,
  not percentiles. The summary prints `n` next to every aggregate.
- The timing of the first test of a templated class includes the template
  build; later tests of the class show only the restore.
- Records are produced by the code under test; the summary treats them as
  untrusted data (bounded, type-checked, never executed) and the unittest
  jobs decide pass or fail on their own.
- Tests with short real-time waits are the contention risk a larger pool
  trades against: a wait sized for an idle runner can miss under a full
  pool (the reason `--workers` stays one below the core count).
