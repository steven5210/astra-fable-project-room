# Audit measurement example (synthetic room, fixed epochs)

Intervals and gaps come from `ao_room_timing` over saved controller records only; the census, usage and capacity come from the exporter and scorer on the same room.

## Intervals and gaps by category

| category | seconds |
|---|---|
| acceptance_review | 180.0 |
| engineering | 1800.0 |
| other | 0.0 |
| spec_review | 300.0 |
| verification | 120.0 |

| gap kind | seconds |
|---|---|
| quota_wait | 2760.0 |
| hold_wait | 0.0 |
| unattributed | 300.0 |
| unknown_bounds count | 0 |

Coverage: first 2027-01-15T08:00:00+00:00 to last 2027-01-15T09:31:00+00:00 (span 5460.0 s).
Overlap (sum - union): 0.0 s; elapsed alone is never a speedup.

## Audit classes

| audit class | value |
|---|---|
| implementation | engineering intervals of `implementation` purpose |
| product-correctness repair | engineering intervals of `correction` purpose; the cause is not recorded |
| operator or harness failure | unknown: no representation in controller records |
| provider wait | quota_wait gaps, 2760.0 s, basis: saved semantic status kind quota_limit; hold_wait gaps of kind provider_error would count too (none here) |
| resource wait | unknown: no representation in controller records |
| handoff delay | inside unattributed; not separable |
| accepted outcomes (complete) | 1 (latest acceptance bound to the exported candidate: True) |
| recorded acceptances (any) | 1 |
| engineering-integrated outcomes | unavailable: a single-room bundle carries no coordination import record |
| tasks | unavailable: bundles carry no plan or transcript |
| reports | engineering requests completed 2, not completed 1; correction dispatches 2 (dispatches, not completed corrections) |
| tests | 1 tests_reported entries in the final report; claims, never summed across requests |

## Usage

| role | input | output | cached | known items | unknown items |
|---|---|---|---|---|---|
| parent | 800 | 80000 | 1200000 | 4 | 0 |
| worker | unknown | unknown | unknown | 0 | 2 |
| judge | 200 | 20000 | 300000 | 1 | 0 |
Unknown is not zero: a role with no known item shows unknown; a partial sum is labelled partial. Cached tokens are recorded, never a quota. Shared-account rate readings are never attributable.

## Capacity vs caps.json

| counter | used | retired | remaining | cap |
|---|---|---|---|---|
| judge_dispatches | 1 | 0 | 5 | 6 |
| judge_output_tokens | 20000 | 0 | 70000 | 90000 |
| native_worker_launches | 2 | 0 | 10 | 12 |
| parent_dispatches | 4 | 0 | 14 | 18 |
| parent_output_tokens | 80000 | 0 | 400000 | 480000 |

## Limitations

- native_worker_usage_not_attributed
- The room is synthetic; figures exercise the fields, not real behavior.
- tasks stay unavailable: bundles carry no plan or transcript.
