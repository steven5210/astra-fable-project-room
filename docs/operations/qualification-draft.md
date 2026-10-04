# Qualification drafts: report, draft, adopt

When a newer member of an already qualified model family exists, `ao_release_check`
reports it and retains a validated draft. Adoption is a separate explicit operator
action — the printed `adopt` command — unless the operator has recorded the standing
decision `family_qualification_auto_adopt: true` in the private `ao/config.json`, in
which case the same check adopts the validated draft and reports `adopted`.

## What a proposal is

The `qualification_draft` section of `ao_release_check` carries one row per
qualified family. A proposal requires all of:

- the newer `claude-<family>-<digits>` identifier appears in **every** retained
  source's freshly fetched capture (the two-source evidence rule when a family
  cites two sources);
- the same identifier is embedded in the configured `claude_bin` executable's
  bytes;
- it is a family member, not a dated snapshot (any component above 9999, like
  `claude-fable-5-5-20260101`, is never a candidate), and strictly newer than the
  current expected model — proposals never downgrade;
- the configured executable's version meets the family's existing
  `minimum_claude_code_version` floor; the new floor is the executable version
  and never moves below the recorded one.

Rows without a proposal carry the reason: `source_unavailable`,
`not_in_source_capture`, `not_embedded_in_executable`,
`executable_below_family_floor`, `executable_version_unknown`,
`current_model_not_in_source_capture`, or `none_newer`.
`current_model_not_in_source_capture` means the fresh captures no longer name
the family's qualified model at all: a draft is never written while any row
carries it — a revision never re-cites evidence that dropped a qualified model,
and removal stays an explicit operator change.

## Drafts

`outcome: drafted` means a complete, verified, **inert** draft was written under
`<home>/ao/qualification-drafts/r<revision>-<key12>/`, where the directory key is
the canonical digest of the proposal — the `{family, from, to, floor}` set — not
of the fetched bytes:

- `artifact.json` — revision N+1 with fresh source descriptors (fresh
  `captured_at`, `sha256`, and evidence paths into the draft);
- `evidence/<id>.bin` — the freshly fetched source bytes each descriptor pins;
- `engineering_models.json` — the exact-model additions adoption would merge;
- `proposal.json` — the rows, the draft artifact digest, and the previous
  pointer it was drafted against.

A draft never mixes stale and fresh evidence: when any source is unreachable, no
draft is written at all. Drafts are idempotent — the same inputs reuse the same
directory, and the *same proposal* reuses it even when a later fetch's bytes
differ: a retained draft is a complete, self-verifying capture, never partially
re-written. A colliding directory name with a different proposal or previous
pointer refuses. Every draft is validated with
`ao_model_qualification.validate_qualification` and re-verified with
`verify_sources` before it is reported. Drafting is retention only:
`<home>/ao/config.json` is never touched by the release check.

## Adoption

`action` names the command; adopt between jobs, then transition rooms:

```sh
python3 ao_qualification_draft.py --home ~/.project-room adopt \
    --draft <draft_dir> --authorization "<operator authorization>"
```

`adopt` refuses unless the draft still verifies end to end: its artifact digest
matches `proposal.json`, every retained evidence file verifies, the current
configured pointer still equals the draft's `previous` pointer (a stale draft is
refused — draft again), the revision is exactly the next one, every proposed
identifier is still embedded in the configured executable, and that executable's
version is known and meets every proposed floor. The draft path must be
a direct child of `qualification-drafts`; empty or oversized authorization
refuses. If the pointer already names this draft's artifact, `adopt` returns
`already_adopted` — or, when the adoption record itself was lost after the
switch, completes it once and reports `recovered`.

On success `adopt` backs up the exact prior `<home>/ao/config.json` bytes to
`config.json.bak-<UTC stamp>-r<revision>-<sha12>`, proves the candidate
configuration against an effective-policy reload **before** the switch (a
failure refuses with the configuration untouched, so no restore is needed),
then switches `family_qualification` to the draft artifact, merges the draft's
`engineering_models` additions (a key colliding with a bundled model or an
existing different entry refuses), and records the adoption under
`qualification-adoptions/` with the authorization.

When the private `<home>/ao/config.json` sets `family_qualification_auto_adopt:
true`, the release check performs the same `adopt` call itself after drafting
instead of printing the command: the section reports `adopted` with the adoption
details, and the recorded authorization is the standing text naming the flag. An
adoption failure reports `unknown` with reason `adopt_failed` and leaves the
draft retained and the configuration untouched; without the flag the check only
ever reports `drafted`.

Existing rooms are untouched: rooms transition separately after adoption under
the documented engineering-model transition procedure.

## Vocabulary

- **Source capture** — the evidence bytes retained when a qualification was
  written; `qualification_sources` compares fresh fetches against them.
- **Executable embedding** — model identifiers present in the configured
  `claude_bin`'s bytes; a model not embedded cannot be selected.
- **Qualification** — the digest-pinned artifact naming each family's expected
  model and floor.
- **Drafting** — writing the next revision's artifact plus fresh evidence under
  the private home; inert, idempotent, validated.
- **Adoption** — the authorized pointer switch that makes a draft active.
