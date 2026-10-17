# Controller maintenance lane

The controller is maintained through Project Room rooms, and in a normal room
Astra does not implement. A controller defect that blocks a live room's
supported path therefore blocks its own repair. This lane is the standing,
documented path for that case; it replaces per-incident improvisation.

## When this lane applies

All of the following must hold:

- The problem is a controller defect, not a product or specification
  disagreement.
- The defect is reproduced with synthetic fixtures — the fake AO backend and
  synthetic rooms — through a fail-first probe on the exact base commit that
  shows the refusal with zero POSTs.
- The defect blocks a live room's supported path, or blocks the repair of such
  a path.

This lane is not for features, optional enhancements, migrations, model,
provider or guard changes, or counter resets. Those go through the ordinary
issue → approval → specification workflow.

## Standing authorization boundary

When the user explicitly adopts this maintenance policy, record that approval
and its scope. Within that recorded authorization, the maintainer may perform
the following actions without asking again for each incident:

- open the GitHub issue, or use the existing one;
- create an isolated worktree at an exact base commit
  (`git worktree add ... origin/main`);
- implement the minimum fix with regression tests on synthetic data;
- run the verification set;
- write the evidence bundle;
- open a draft pull request that links the issue;
- request independent review.

Without explicit per-incident authorization the maintainer must not:

- merge;
- install (`project_room_runtime` sync of the plugin cache);
- perform any live room operation (audit, grant, extend, send, resume, close,
  spec_put);
- edit saved room state, receipts, counters or journals;
- create replacement rooms;
- fabricate receipts.

Installation and every live grant remain separate authorizations even after a
merge.

## Delivery requirements

Each delivery must satisfy all of the following:

- the exact base commit is named;
- fail-first evidence (before) and fixed evidence (after) come from the same
  probe, kept outside the repository;
- focused tests for the changed module, then the neighbouring lane modules,
  then one full `python3 tests/run_test_shards.py --jobs 5 -v` on the final
  commit bytes;
- `python3 -m pyflakes` on every changed `.py` file;
- `git diff --check`;
- `python3 -m unittest test_docs_links -v` when docs change;
- the runtime roster (`project_room_runtime.RUNTIME_FILES`) and the
  `docs/README.md` index are updated when a module or guide is added — the
  #190 proposal missed both, which is why this check exists;
- `tests/shard_weights.json` is updated for a new test module;
- saved record formats are unchanged unless the issue says otherwise —
  existing records must validate byte-for-byte;
- fixtures and PR text are free of live identifiers;
- the final diff receives independent review with runnable reproductions —
  Devin Review on the PR, Codex's review with reproductions, and Astra's
  review when the behaviour touches a room workflow;
- every review thread is answered with a disposition and resolved;
- the PR stays draft until all of the above complete.

## Evidence bundle

The bundle lives under the operator's Codex outputs folder and follows the
#190 delivery layout:

- `REPORT.md` — what was found, what changed, what was excluded, the
  verification table and the remaining limitations;
- `manifest.json` — base, head, per-file SHA-256, patch SHA-256,
  per-evidence-file SHA-256, `prepared_at`;
- the whole-branch patch, plus `commits/` per-commit patches;
- `evidence/` — raw logs named `baseline-*` and `final-*` (focused,
  neighbours, full shards per shard, pyflakes, diff-check, docs-links) plus
  the before/after reproduction JSON and logs.

The hashes let the reviewer verify that the bytes it reviews are the bytes in
the PR.

## Hand-back sequence

Draft PR → CI green → Devin Review clean → Codex check with reproductions →
Astra review when applicable → dispositions posted and threads resolved →
user authorizes merge → merge → user authorizes install → install and verify
the installed version matches main → any live room step under its own
authorization and coordination with Codex/Astra.

A passing test suite never authorizes merge or install. A merged fix never
authorizes a live grant.

## Worked example

Issue #190 ran this lane: branch `codex/spec-verdict-format-recovery` at base
`a2f2619`. The proposal arrived as a patch with its own two test modules green
but seven failures and three errors in the full suite, a missing
runtime-roster entry and a missing docs index entry. The lane produced two
review rounds (five threads, all answered with dispositions), 2918 tests
green on the final bytes, and installation happened only after Astra's
acceptance and the user's merge/install authorization. Issue #196 followed
the same lane for the compound report-correction dependency.

## What this lane never does

- install;
- live grants or sends;
- counter resets;
- state edits;
- replacement rooms;
- receipt fabrication;
- model, provider or guard changes;
- scope beyond the reproduced defect.
