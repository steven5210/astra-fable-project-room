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
4. `ao_room_import_preflight(room_id, assignment_id, handback_sha256)` — call it on the owner room. It revalidates the handback against live assignment evidence, observes the owner HEAD, worktree cleanliness, spec digests, owner-touched assigned paths and a merge-tree conflict preview. It records the facts and `ready` only when there are no blockers and the preview is clean. `suggested_operation` documents one possible Git command; the controller never runs it.
5. The operator performs the Git import in the owner worktree, for example `git cherry-pick -x <commit>`.
6. `ao_room_import_record(room_id, assignment_id, preflight_sha256, operation, resolution=None)` — call it on the owner room after the operator's operation. It requires the latest recorded preflight and an owner HEAD that strictly descends from the preflight head. It diffs the destination against the handback paths and content. An exact match records `kind: "faithful"`. Any deviation records `kind: "resolved_with_modifications"` with the modified, extra and missing paths and requires a nonempty `resolution` text. Without it the call refuses `integration_modifications_unresolved`. `operation` is the operator's recorded assertion of the command performed.

Identical repeats of each call return the existing digest-addressed record. A different request against an existing record refuses.

## Enforcement in the normal flow

- `ao_room_accept` on an owner room refuses a candidate whose `baseline..candidate` diff changes a path owned by an engaged assignment (`owned_by_active_assignment`). Import the accepted handback or supersede the assignment first.
- `ao_room_accept` on an owner room after an import refuses unless the recorded import head is an ancestor of the checkpoint candidate and the latest engineering result postdates the import (`integration_requires_fresh_engineering`). Fresh owner engineering judgment must follow every import.
- `ao_room_accept` on an assignment room refuses a candidate that does not descend from the lineage baseline (`baseline_mismatch`) or that changes an unowned path (`scope_drift`). A clean worktree is not required at acceptance; the handback requires it.
- Status, compact status, progress and room list show the coordination role, assignment state, owned paths, pinned spec digests, latest handback, import kind and blockers. A room without a coordination pointer shows `{"role": None, "assignments": []}`.

## What stays manual

- The operator chooses the owned paths, both specs and when to supersede.
- The operator performs the Git import. The controller validates and records it; it never runs a Git write.
- The operator resolves conflicts by hand and states the resolution in `resolution`.
- Nothing runs a model, schedules work or publishes a candidate.

## Limitations

- The checks validate recorded Git facts and refuse. They do not prevent arbitrary external filesystem writes.
- Admission is atomic within one controller home only.
- The recorded account profile names are configuration metadata, not provider billing attribution.
- An assignment room participates in at most one assignment at a time. An owner room may own many assignments. A room is never both.

## Installation

`ao_coordination.py` is a new runtime file in `project_room_runtime.RUNTIME_FILES`. Existing rooms are unaffected: the coordination surface reads the `coordination` key and leaves state without it byte-identical. No migration runs.

## Rollback

Stop using the four tools. The recorded lineage, handback, preflight, import and refusal records are inert audit data.
