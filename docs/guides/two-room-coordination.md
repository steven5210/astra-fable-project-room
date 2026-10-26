# Two-room coordination

Two-room coordination lets one integration-owner room hand a bounded set of repository paths to one assignment room. Both rooms are ordinary normal AO rooms. Each room keeps its own Fable engineer, its own Astra reviewer, and its own specification, agreement, handoff, verification and acceptance workflow. The owner stays responsible for the integrated result.

The legacy `room_*` backend has no coordination and gets none. Open both rooms with the AO tools.

## Roles

- The **owner** room owns the integrated result. It admits the assignment, preflights the import and records the import the operator performed.
- The **assignment** room owns a bounded set of repository paths. It engineers, verifies, passes independent acceptance and records one immutable handback.
- The **operator** performs the Git import by hand. The controller never writes to either worktree. The controller validates recorded Git facts and refuses; it never merges, repairs, dispatches a model or moves work.

## Lifecycle

Each assignment moves through four states:

- `active` — admitted and owned. The assignment room may engineer.
- `handback_recorded` — an accepted candidate is recorded as an immutable handback. A newer accepted candidate may record a further handback.
- `imported` — terminal. The operator's import is recorded and ownership is released.
- `superseded` — terminal. A newer admission replaced the lineage. It releases ownership before import.

`ao_room_assignment_open` with `supersedes` is the only operation that releases ownership before an import. Failed or uncertain requests in either room never change lineage state.

Every refusal is appended to the lifecycle `refusals` list, bounded to the last 20. A refusal never changes the state.

## Tool sequence

1. `ao_room_assignment_open(room_id, assignment_id, assignment_room_id, owned_paths, interface, authorization, supersedes=None)` — call it on the owner room after the owner has an agreed spec and a handoff. `assignment_room_id` is a second normal room in the same controller home and the same Git repository with a registered spec. `owned_paths` is a non-empty list of normalized repository-relative paths; each entry covers itself and the directory prefix below it. `interface` is the frozen obligation text recorded verbatim. `authorization` is the operator's recorded assertion; it is not authentication. `supersedes` names an engaged assignment between the same two rooms when a material change needs a clean lineage. The call writes the immutable lineage under the adapter-wide lock, so two overlapping admissions cannot both succeed.
2. Work proceeds in the assignment room through the normal workflow: spec agreement, handoff, implementation, verification and independent acceptance.
3. `ao_room_handback_record(room_id, assignment_id, acceptance_request_id)` — call it on the assignment room. It revalidates the acceptance, the reviewer receipt, the checkpoint, the spec content digest and the Git facts: a committed clean candidate that descends from the lineage baseline, with every changed path inside the owned set. It writes one immutable digest-addressed handback with the changed paths and the binary patch digest.
4. `ao_room_import_preflight(room_id, assignment_id, handback_sha256)` — call it on the owner room. It revalidates the handback against live assignment evidence, observes the owner HEAD, worktree cleanliness, spec digests, owner-touched assigned paths and a merge-tree conflict preview. It records the facts and `ready` only when there are no blockers and the preview is clean. `suggested_operation` documents one possible Git command; the controller never runs it. The preflight promises that the recorded handback evidence is intact and still agrees with the assignment room's saved records and the shared repository. It does not re-run acceptance or re-check live provider identity; the assignment room's own acceptance already did that.
5. The operator performs the Git import in the owner worktree, for example `git merge --no-ff --no-edit <commit>`, the operation the preview models. The merge applies the whole recorded change and keeps the handback commit in the owner history as provenance. A cherry-pick of the recorded range is also accepted when the destination verification passes. The preflight's conflict preview needs git 2.38 or newer; an older git records `preview: "unavailable"`, the preflight is then not `ready`, and the import needs a `resolution` text.
6. `ao_room_import_record(room_id, assignment_id, preflight_sha256, operation, resolution=None)` — call it on the owner room after the operator's operation. It requires the latest recorded preflight and an owner HEAD that strictly descends from the preflight head. It diffs the destination against the handback paths and content. An exact match records `kind: "faithful"`. A missing handback path always refuses `handback_not_incorporated`; a deliberate drop is a scope change and needs a new assignment, never an import. A modified or extra path records `kind: "resolved_with_modifications"` and requires a nonempty `resolution` text. Without it the call refuses `integration_modifications_unresolved`. `operation` is the operator's recorded assertion of the command performed.

Identical repeats of each call return the existing digest-addressed record. A different request against an existing record refuses.

## Enforcement in the normal flow

- `ao_room_accept` on an owner room refuses a candidate whose `baseline..candidate` diff changes a path owned by an engaged assignment (`owned_by_active_assignment`). Import the accepted handback or supersede the assignment first.
- `ao_room_accept` on an owner room after an import refuses unless the recorded import head is an ancestor of the checkpoint candidate and the latest engineering result postdates the import (`integration_requires_fresh_engineering`). Fresh owner engineering judgment must follow every import.
- `ao_room_accept` on an assignment room refuses a candidate that does not descend from the lineage baseline (`baseline_mismatch`) or that changes an unowned path (`scope_drift`). A clean worktree is not required at acceptance; the handback requires it.
- Coordination evidence never removes ownership controls silently. A lineage without its lifecycle is an unfinished reservation and still engages its owned paths and its owner's slot until the retry repairs it or an operator removes it. A missing lifecycle for a missing lineage, a corrupt record or a record bound to another lineage is `coordination_evidence_unresolved`: admission and both acceptance boundaries refuse until the evidence is restored.
- A retry or repair of admission re-runs the same reservation scan a fresh admission would. A conflict discovered then refuses and writes nothing.
- Supersession requires the settled boundary: `supersedes` refuses while the assignment room holds any request that is not in a terminal state. `uncertain` is not terminal; it still blocks supersession.
- `ao_room_import_record` revalidates the preflight's authority before it records: the preflighted handback must still be the latest, the lifecycle must still bind the pinned lineage, the blocker set is recomputed, and every recorded handback fact re-verifies against live evidence. Any change refuses and asks for a new preflight.
- `ao_room_handback_record` refuses evidence that predates the assignment's own admission (`acceptance_predates_assignment`, `checkpoint_predates_assignment`, `engineering_predates_assignment`). A reused room's terminal history never stands in for work done under the new assignment.
- An import counts a handback path the destination already holds byte-identically, or the handback commit already in ancestry, as incorporated and records `already_incorporated_paths`. A genuine drop still refuses by name and an unrelated advance still refuses `handback_not_incorporated`.
- One engaged assignment per owner room: a second admission for an owner that already has an engaged assignment refuses `owner_assignment_cap`. The cap is atomic under the admission lock.
- Status, compact status, progress and room list show the coordination role, assignment state, owned paths, pinned spec digests, latest handback, import kind, blockers and the recorded account policy. A room without a coordination pointer shows `{"role": None, "assignments": []}` plus the effective account policy.

## Account mode

The optional `coordination_accounts` key in `PROJECT_ROOM_HOME/ao/config.json` names the allowed account pairing for new admissions:

```json
"coordination_accounts": {"mode": "single", "owner_profile": null, "assignment_profile": null}
```

An absent key means exactly that value, so every existing setup keeps working. `single` keeps coordination inside one account: at admission an unpinned room resolves as the saved default login — that is a documented assumption for this check only, not attribution — and the owner and the assignment room must resolve the same account, else `dual_account_requires_dual_mode`. A configured `owner_profile` in single mode must equal the owner's resolved account, else `profile_mismatch`. `dual` requires two distinct enrolled profile names: both rooms must be pinned (`room_not_pinned`), the pins must name the configured profiles (`profile_mismatch`), each pin's live account identity is rechecked (`account_identity_changed`) and the two identities must differ (`same_account_identity`). The configured names resolve through the same enrollment check `ao_room_open` performs, so an unenrolled or same-account configuration refuses with the existing enrollment messages.

The mode is admission-only. Handback, preflight, import and the acceptance checks never re-read it: an assignment admitted under one mode drains under its recorded policy, and a later config edit changes only the effective mode status reports. The lineage records `account_policy` with the mode and profile names — never digests, directories or credentials. Status adds `account_policy` at the top of the coordination view (the effective mode, or `{"mode": "invalid", "error": ...}` for a bad value, which still never breaks the read) and per assignment (the recorded mode).

Nothing in the account mode creates a room, dispatches, switches accounts or attributes billing; the names are configuration metadata.

## What stays manual

- The operator chooses the owned paths, both specs and when to supersede.
- The operator performs the Git import. The controller validates and records it; it never runs a Git write.
- The operator resolves conflicts by hand and states the resolution in `resolution`.
- Nothing runs a model, schedules work or publishes a candidate.

## Limitations

- The checks validate recorded Git facts and refuse. They do not prevent arbitrary external filesystem writes.
- Admission is atomic within one controller home only.
- The recorded account profile names are configuration metadata, not provider billing attribution.
- An assignment room engages in at most one assignment at a time, and an owner room engages in at most one assignment at a time. An imported or superseded assignment stays in its pointer for audit and does not block a new admission. An idempotent repair of a terminal assignment adds no engagement and stays exempt from the engaged-pointer and per-owner-cap refusals. A room is never both.

## Installation

`ao_coordination.py` is a new runtime file in `project_room_runtime.RUNTIME_FILES`. Existing rooms are unaffected: the coordination surface reads the `coordination` key and leaves state without it byte-identical. No migration runs.

## Rollback

Stop using the four tools. The recorded lineage, handback, preflight, import and refusal records are inert audit data.
