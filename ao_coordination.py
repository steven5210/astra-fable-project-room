"""AO-only two-room coordination: bounded assignment lineage, handback and import records.

One owner normal room pins a bounded set of owned repository paths to one assignment
normal room in the same Git repository, under one shared controller home. This module
admits the pair atomically under the adapter-wide lock, validates the assignment
room's accepted candidate at handback, preflights the owner-side import and records
the operator's import. Every check is refuse-only: it validates recorded Git facts
and never dispatches a model, merges, publishes, schedules, or moves work. Ordinary
rooms without a coordination pointer are untouched, and the legacy room_* backend
gets no coordination surface.
"""

import hashlib
from pathlib import Path
import re
import subprocess
import time

from room import RoomError

VERSION = 1
ACTIVE = "active"
HANDBACK_RECORDED = "handback_recorded"
IMPORTED = "imported"
SUPERSEDED = "superseded"
STATES = (ACTIVE, HANDBACK_RECORDED, IMPORTED, SUPERSEDED)
ENGAGED = (ACTIVE, HANDBACK_RECORDED)
OWNED_PATH_LIMIT = 200
OWNED_PATH_BYTES = 512
INTERFACE_BYTES = 8192
OPERATION_BYTES = 4000
REFUSAL_LIMIT = 20
DEDUPE_LIMIT = 100
GIT_TIMEOUT = 15
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VOLATILE = {"created_at", "recorded_at", "updated_at", "assignment_start"}
ENGINEERING_PURPOSES = {"implementation", "correction"}


def _root(ao_root):
    return ao_root / "coordination" / "assignments"


def _room_state(ao_root, room_id):
    from ao_project_room import identifier, read
    directory = ao_root / "rooms" / identifier(room_id)
    if not (directory / "state.json").is_file():
        raise RoomError("Unknown AO room; legacy rooms use the existing room_* tools")
    return directory, read(directory / "state.json")


def _assignment_dir(ao_root, assignment_id):
    from ao_project_room import identifier
    return _root(ao_root) / identifier(assignment_id)


def _sha256(value, label):
    from ao_project_room import nonempty
    nonempty(value, label, 128)
    if not _SHA256.fullmatch(value):
        raise RoomError(label + " must be a lowercase SHA-256 hex digest")
    return value


def _lineage(directory):
    from ao_project_room import read
    path = directory / "lineage.json"
    if not path.is_file():
        raise RoomError("Unknown coordination assignment")
    record = read(path)
    if not isinstance(record, dict) or record.get("version") != VERSION:
        raise RoomError("Assignment lineage is corrupt")
    return record


def _lifecycle(directory):
    from ao_project_room import read
    path = directory / "state.json"
    if not path.is_file():
        raise RoomError("Assignment lifecycle is missing")
    record = read(path)
    if (not isinstance(record, dict) or record.get("version") != VERSION
            or record.get("state") not in STATES):
        raise RoomError("Assignment lifecycle is corrupt")
    return record


def _evidence(directory):
    """(lineage, lifecycle) for one assignment directory, validated together.

    A directory with neither file is empty and returns None. A lineage without a
    lifecycle is an interrupted admission: it returns an ACTIVE synthetic lifecycle
    (the reservation), never written. Anything corrupt or mismatched raises
    RoomError: that evidence is unresolved, and callers must refuse, never skip it.
    """
    from ao_project_room import digest
    if not (directory / "lineage.json").is_file():
        if (directory / "state.json").is_file():
            raise RoomError("lifecycle record without a lineage")
        return None
    lineage = _lineage(directory)
    if not (directory / "state.json").is_file():
        return lineage, {"version": VERSION, "assignment_id": lineage["assignment_id"],
                         "state": ACTIVE, "lineage_sha256": digest(lineage), "reservation": True}
    lifecycle = _lifecycle(directory)
    if lifecycle.get("lineage_sha256") != digest(lineage):
        raise RoomError("Assignment lifecycle refers to another lineage")
    return lineage, lifecycle


def _scan(service, owner, owned, room_id, exclude, cap=True):
    """The shared admission scan over sibling assignment directories.

    Unresolved evidence refuses instead of being skipped; an engaged sibling
    (including a lineage-only reservation) refuses an overlapping admission and,
    with cap, enforces one engaged assignment per owner room.
    """
    root = _root(service.root)
    for other in sorted(root.glob("*")) if root.is_dir() else ():
        if not other.is_dir() or other.name in exclude:
            continue
        try:
            evidence = _evidence(other)
        except (RoomError, OSError, ValueError) as exc:
            raise RoomError("admission refused: coordination evidence unresolved for assignment "
                            + other.name + ": " + str(exc)[:160]) from None
        if evidence is None:
            continue
        other_lineage, other_lifecycle = evidence
        if other_lifecycle.get("state") not in ENGAGED:
            continue
        if other_lineage.get("git_common_dir") == owner["git_common_dir"]:
            overlapping = _overlap(owned, other_lineage.get("owned_paths") or [])
            if overlapping:
                raise RoomError("admission refused: admission overlaps assignment "
                                + other.name + " on paths " + ", ".join(overlapping))
        if cap and other_lineage.get("owner_room_id") == room_id:
            raise RoomError("admission refused: owner_assignment_cap: owner room already has "
                            "engaged assignment " + other.name
                            + "; one engaged assignment per owner")


def _new_lifecycle(lineage):
    from ao_project_room import digest
    return {"version": VERSION, "assignment_id": lineage["assignment_id"], "state": ACTIVE,
            "lineage_sha256": digest(lineage), "handbacks": [], "latest_handback": None,
            "preflights": [], "import": None, "superseded_by": None, "refusals": [],
            "updated_at": time.time()}


def _refuse(directory, lifecycle, operation, reason, detail=""):
    """Append one bounded refusal record, never a state change, then raise."""
    if lifecycle is not None:
        from ao_project_room import atomic
        refusals = lifecycle.setdefault("refusals", [])
        refusals.append({"at": time.time(), "operation": operation, "reason": reason})
        lifecycle["refusals"] = refusals[-REFUSAL_LIMIT:]
        lifecycle["updated_at"] = time.time()
        atomic(directory / "state.json", lifecycle)
    raise RoomError("coordination " + operation + " refused: " + reason + (": " + detail if detail else ""))


def _body(record):
    return {key: value for key, value in record.items() if key not in _VOLATILE}


def _dedupe(files_dir, record):
    """The existing immutable record equal to this request modulo its recorded time."""
    from ao_project_room import read
    if not files_dir.is_dir():
        return None
    body = _body(record)
    for path in sorted(files_dir.glob("*.json"))[:DEDUPE_LIMIT]:
        try:
            existing = read(path)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(existing, dict) and _body(existing) == body:
            return existing
    return None


def _store_immutable(path, record):
    """Create-once write: an existing file must hold the identical canonical record."""
    from ao_project_room import atomic, read
    if path.exists():
        existing = read(path)
        if existing != record:
            raise RoomError("Coordination record is immutable; a different record exists at this digest")
        return
    atomic(path, record)


def _git_run(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, timeout=GIT_TIMEOUT)


def _git(path, *args):
    result = _git_run(path, *args)
    if result.returncode:
        raise RoomError("Git check failed: " + result.stderr.decode(errors="replace")[:500])
    return result.stdout.decode().strip()


def _is_ancestor(path, base, commit):
    return _git_run(path, "merge-base", "--is-ancestor", base, commit).returncode == 0


def _status_clean(path):
    result = _git_run(path, "status", "--porcelain", "-z", "--untracked-files=all")
    if result.returncode:
        raise RoomError("Git status failed: " + result.stderr.decode(errors="replace")[:500])
    return not result.stdout.strip()


def _decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def _changed(path, base, head=None):
    """name-status entries base..head (or base..worktree), renames carrying old_path."""
    args = ["diff", "--name-status", "-z", "-M", "--find-renames", base]
    if head is not None:
        args.append(head)
    result = _git_run(path, *args)
    if result.returncode:
        raise RoomError("Git diff failed: " + result.stderr.decode(errors="replace")[:500])
    fields = _decode(result.stdout).split("\0")
    entries = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        code = field[0]
        if code in ("R", "C"):
            entries.append({"status": code, "path": fields[index + 1], "old_path": fields[index]})
            index += 2
        else:
            entries.append({"status": code, "path": fields[index]})
            index += 1
    return entries


def _entry_paths(entry):
    paths = [entry["path"]]
    if entry.get("old_path"):
        paths.append(entry["old_path"])
    return paths


def _covered(path, owned):
    return any(path == item or path.startswith(item + "/") for item in owned)


def _overlap(first, second):
    return sorted({p for p in first if _covered(p, second)} | {p for p in second if _covered(p, first)})


def _relative_path(value, label):
    if not isinstance(value, str) or not value:
        raise RoomError(label + " entries must be nonempty strings")
    try:
        size = len(value.encode())
    except UnicodeEncodeError:
        raise RoomError(label + " entries must be valid UTF-8") from None
    if (size > OWNED_PATH_BYTES or "\\" in value or "\0" in value
            or value.startswith("/") or value.startswith("./")):
        raise RoomError(label + " entries must be normalized relative repository paths")
    if any(part in ("", ".", "..", ".git") for part in value.split("/")):
        raise RoomError(label + " entries must be normalized relative repository paths")
    return value


def _owned_paths(value):
    if not isinstance(value, list) or not value:
        raise RoomError("owned_paths must be a nonempty list of relative repository paths")
    if len(value) > OWNED_PATH_LIMIT:
        raise RoomError("owned_paths exceeds the " + str(OWNED_PATH_LIMIT) + " path bound")
    paths = [_relative_path(item, "owned_paths") for item in value]
    if len(set(paths)) != len(paths):
        raise RoomError("owned_paths contains a duplicate")
    ordered = sorted(paths)
    for earlier, later in zip(ordered, ordered[1:]):
        if later.startswith(earlier + "/"):
            raise RoomError("owned_paths contains the ambiguous ancestor/descendant pair " + earlier + " and " + later)
    return ordered


def _profile_name(state):
    profile = state.get("account_profile")
    return profile.get("name") if isinstance(profile, dict) else None


def _account_of(state):
    """The room's pinned profile name; an unpinned room reads as the saved default login
    for this check (a documented admission assumption, never attribution)."""
    profile = state.get("account_profile")
    return profile.get("name") if isinstance(profile, dict) else "default"


def account_policy(ao_root, strict=True):
    """The opt-in single/dual account mode from PROJECT_ROOM_HOME/ao/config.json.

    Absent key: {"mode": "single", "owner_profile": None, "assignment_profile": None}.
    strict=True raises RoomError on an invalid value; strict=False returns the
    read-only {"mode": "invalid", "error": <bounded reason>} view.
    """
    from ao_project_room import read
    path = ao_root / "config.json"
    config = {}
    if path.is_file():
        try:
            config = read(path)
        except (OSError, ValueError, RecursionError):
            config = {}
    value = config.get("coordination_accounts")
    if value is None:
        value = {"mode": "single", "owner_profile": None, "assignment_profile": None}
    try:
        return _validate_account_policy(value)
    except RoomError as exc:
        if strict:
            raise RoomError(
                "coordination_accounts in PROJECT_ROOM_HOME/ao/config.json is invalid: "
                + str(exc)[:200]) from None
        return {"mode": "invalid", "error": str(exc)[:200]}


def _validate_account_policy(value):
    import ao_account_profiles
    if not isinstance(value, dict):
        raise RoomError("must be an object")
    mode = value.get("mode", "single")
    owner_profile = value.get("owner_profile")
    assignment_profile = value.get("assignment_profile")
    if mode not in ("single", "dual"):
        raise RoomError("mode must be single or dual")
    for label, name in (("owner_profile", owner_profile), ("assignment_profile", assignment_profile)):
        if name is not None and name != ao_account_profiles.DEFAULT:
            try:
                ao_account_profiles.profile_name(name)
            except RoomError as exc:
                raise RoomError(label + ": " + str(exc)[:120]) from None
    if mode == "single":
        if assignment_profile is not None:
            raise RoomError("single mode allows only owner_profile")
    else:
        if not owner_profile or not assignment_profile or owner_profile == assignment_profile:
            raise RoomError("dual mode requires two distinct profile names")
    return {"mode": mode, "owner_profile": owner_profile, "assignment_profile": assignment_profile}


def _check_account_mode(service, policy, owner, assign):
    """Admission-only account rules; drains never read the mode. Returns the recorded policy."""
    import ao_account_profiles
    from ao_project_room import read
    distinct = None
    if policy["mode"] == "single":
        owner_account = _account_of(owner)
        assign_account = _account_of(assign)
        if owner_account != assign_account:
            raise RoomError("admission refused: dual_account_requires_dual_mode: owner is on "
                            + owner_account + ", assignment on " + assign_account
                            + "; set coordination_accounts.mode to dual")
        configured = policy.get("owner_profile")
        if configured and owner_account != configured:
            raise RoomError("admission refused: profile_mismatch: owner is on " + owner_account
                            + ", configured owner_profile " + configured)
    else:
        owner_pin = owner.get("account_profile")
        assign_pin = assign.get("account_profile")
        if not isinstance(owner_pin, dict) or not owner_pin.get("name"):
            raise RoomError("admission refused: room_not_pinned: owner room has no account profile")
        if not isinstance(assign_pin, dict) or not assign_pin.get("name"):
            raise RoomError("admission refused: room_not_pinned: assignment room has no account profile")
        if (owner_pin["name"] != policy["owner_profile"]
                or assign_pin["name"] != policy["assignment_profile"]):
            raise RoomError("admission refused: profile_mismatch: pins name "
                            + owner_pin["name"] + "/" + assign_pin["name"]
                            + ", configured " + str(policy["owner_profile"])
                            + "/" + str(policy["assignment_profile"]))
        home = service.root.parent
        config_path = home / "config.json"
        controller_config = read(config_path) if config_path.is_file() else {}
        try:
            # The configured names resolve exactly the way ao_room_open resolves a pin: an
            # invalid, unenrolled or same-account configuration fails with #220's own messages.
            ao_account_profiles.pin(home, policy["owner_profile"], controller_config)
            ao_account_profiles.pin(home, policy["assignment_profile"], controller_config)
            ao_account_profiles.check_identity(owner_pin)
            ao_account_profiles.check_identity(assign_pin)
        except RoomError as exc:
            raise RoomError("admission refused: account_identity_changed: "
                            + str(exc)[:200]) from None
        distinct = owner_pin["identity_digest"] != assign_pin["identity_digest"]
        if not distinct:
            raise RoomError("admission refused: same_account_identity: both rooms pin the "
                            "same account identity")
    return {"mode": policy["mode"], "owner_profile": policy.get("owner_profile"),
            "assignment_profile": policy.get("assignment_profile"),
            "distinct_identities": distinct}


def assignment_open(service, room_id, assignment_id, assignment_room_id, owned_paths,
                    interface, authorization, supersedes=None):
    """Atomically admit one owner/assignment room pair and pin the lineage record."""
    from ao_project_room import atomic, digest, identifier, nonempty
    import ao_workflow
    identifier(room_id)
    identifier(assignment_id)
    identifier(assignment_room_id)
    if room_id == assignment_room_id:
        raise RoomError("The assignment room must be a different room")
    if supersedes is not None:
        identifier(supersedes)
    owned = _owned_paths(owned_paths)
    interface = nonempty(interface, "interface", INTERFACE_BYTES)
    authorization = nonempty(authorization, "authorization")
    with service.locked():
        owner_dir, owner = _room_state(service.root, room_id)
        if not ao_workflow.normal(owner):
            raise RoomError("The owner room must be a normal fable_engineering room")
        owner_coord = owner.get("coordination")
        if isinstance(owner_coord, dict) and owner_coord.get("role") not in (None, "owner"):
            raise RoomError("A room is never both owner and assignment")
        ao_workflow.agreement(service, owner_dir, owner)
        handoff = ao_workflow.handoff_record(owner_dir, owner)
        assign_dir, assign = _room_state(service.root, assignment_room_id)
        if not ao_workflow.normal(assign):
            raise RoomError("The assignment room must be a normal fable_engineering room")
        if assign["git_common_dir"] != owner["git_common_dir"]:
            raise RoomError("The assignment room must share the owner's Git repository")
        # An idempotent repair of a terminal assignment adds no engagement: it stays
        # exempt from the engaged-pointer and per-owner-cap refusals (the overlap and
        # evidence scans still apply).
        this_dir = _assignment_dir(service.root, assignment_id)
        terminal_repair = False
        try:
            if (this_dir / "lineage.json").is_file() and (this_dir / "state.json").is_file():
                terminal_repair = _lifecycle(this_dir).get("state") not in ENGAGED
        except RoomError:
            terminal_repair = False
        superseded = None
        if supersedes is not None:
            superseded_dir = _assignment_dir(service.root, supersedes)
            old_lineage = _lineage(superseded_dir)
            old_lifecycle = _lifecycle(superseded_dir)
            if (old_lineage.get("owner_room_id") != room_id
                    or old_lineage.get("assignment_room_id") != assignment_room_id):
                raise RoomError("supersedes must name an assignment between this owner and this assignment room")
            if old_lifecycle["state"] not in ENGAGED:
                raise RoomError("supersedes target is already terminal")
            from ao_project_room import TERMINAL
            unsettled = sorted(rid for rid, request in (assign.get("requests") or {}).items()
                               if request.get("state") not in TERMINAL)
            if unsettled:
                raise RoomError("supersedes refused: assignment room has unsettled request(s) "
                                + ", ".join(unsettled[:5])
                                + "; reach the settled or stopped boundary first")
            superseded = (superseded_dir, old_lifecycle)
        prior_coord = assign.get("coordination")
        if prior_coord:
            if prior_coord.get("role") != "assignment":
                raise RoomError("The assignment room already participates in coordination")
            # Terminal assignments stay in the pointer for audit without blocking
            # admission; an engaged id admits only an idempotent retry of this call
            # or its named supersession. An unresolvable pointer entry blocks.
            for prior_id in prior_coord.get("assignment_ids") or []:
                if prior_id == assignment_id or prior_id == supersedes:
                    continue
                try:
                    prior_lifecycle = _lifecycle(_assignment_dir(service.root, prior_id))
                except RoomError:
                    raise RoomError(
                        "The assignment room already participates in coordination") from None
                if prior_lifecycle.get("state") in ENGAGED and not terminal_repair:
                    raise RoomError("The assignment room already participates in coordination")
        try:
            assign_spec = service.spec(assign_dir, assign)
        except RoomError:
            raise RoomError("Register the assignment room's authorized spec first") from None
        owner_spec = service.spec(owner_dir, owner)
        recorded_policy = _check_account_mode(
            service, account_policy(service.root, strict=True), owner, assign)
        lineage = {"version": VERSION, "assignment_id": assignment_id,
                   "owner_room_id": room_id, "assignment_room_id": assignment_room_id,
                   "git_common_dir": owner["git_common_dir"],
                   "owner": {"spec_revision": owner_spec["revision"], "spec_sha256": owner_spec["sha256"],
                             "spec_record_sha256": owner["spec_record_sha256"],
                             "handoff_sha256": owner["handoff_sha256"],
                             "baseline_commit": handoff["baseline_commit"], "worktree": handoff["worktree"]},
                   "assignment": {"spec_revision": assign_spec["revision"], "spec_sha256": assign_spec["sha256"],
                                  "spec_record_sha256": assign["spec_record_sha256"]},
                   "owned_paths": owned, "interface": interface, "authorization": authorization,
                   "account_profiles": {"owner": _profile_name(owner), "assignment": _profile_name(assign)},
                   "account_policy": recorded_policy,
                   "assignment_start": {"request_ids": sorted((assign.get("requests") or {}).keys()),
                                        "checkpoint_sha256": assign.get("checkpoint_sha256"),
                                        "acceptance_request_ids": sorted(
                                            a["request_id"] for a in (assign.get("acceptances") or []))},
                   "supersedes": supersedes, "created_at": time.time()}
        directory = _assignment_dir(service.root, assignment_id)
        lineage_path = directory / "lineage.json"
        if lineage_path.exists():
            existing = _lineage(directory)
            if _body(existing) != _body(lineage):
                raise RoomError("Assignment lineage is immutable; a different record exists for this assignment_id")
            lineage = existing
            # A retry or repair that can still engage runs the same reservation scan a fresh
            # admission would; repairing a terminal record changes no ownership.
            if not terminal_repair:
                try:
                    _scan(service, owner, owned, room_id, {assignment_id, supersedes})
                except RoomError as exc:
                    raise RoomError("admission refused: retry conflicts with assignment: "
                                    + str(exc)[:220]) from None
            if not (directory / "state.json").exists():
                atomic(directory / "state.json", _new_lifecycle(lineage))
            lifecycle = _lifecycle(directory)
            if lifecycle["lineage_sha256"] != digest(lineage):
                raise RoomError("Assignment lifecycle refers to another lineage")
        else:
            _scan(service, owner, owned, room_id, {assignment_id, supersedes})
            atomic(lineage_path, lineage)
            lifecycle = _new_lifecycle(lineage)
            atomic(directory / "state.json", lifecycle)
            if superseded is not None:
                superseded_dir, old_lifecycle = superseded
                old_lifecycle["state"] = SUPERSEDED
                old_lifecycle["superseded_by"] = assignment_id
                old_lifecycle["updated_at"] = time.time()
                atomic(superseded_dir / "state.json", old_lifecycle)
        # Room pointers complete last; a repeat call repairs any pointer a crash skipped.
        owner_ids = sorted(set((owner_coord or {}).get("assignment_ids") or []) | {assignment_id})
        owner["coordination"] = {"role": "owner", "assignment_ids": owner_ids}
        service.save(owner_dir, owner)
        assign_coord = assign.get("coordination")
        if assign_coord is None:
            assign["coordination"] = {"role": "assignment", "assignment_ids": [assignment_id]}
        elif (assign_coord.get("role") == "assignment"
                and assignment_id not in (assign_coord.get("assignment_ids") or [])):
            assign_coord["assignment_ids"].append(assignment_id)
        service.save(assign_dir, assign)
        return {"opened": True, "assignment_id": assignment_id, "lineage": lineage,
                "lineage_sha256": digest(lineage), "state": lifecycle["state"]}


def handback_record(service, room_id, assignment_id, acceptance_request_id):
    """Validate the assignment room's accepted candidate and write its immutable handback."""
    from ao_project_room import atomic, digest, identifier, read
    identifier(room_id)
    identifier(assignment_id)
    identifier(acceptance_request_id)
    operation = "handback_record"
    with service.locked():
        directory = _assignment_dir(service.root, assignment_id)
        lineage = _lineage(directory)
        lifecycle = _lifecycle(directory)
        if lifecycle["state"] not in ENGAGED:
            _refuse(directory, lifecycle, operation, "invalid_state",
                    "assignment state is " + lifecycle["state"])
        if lineage.get("assignment_room_id") != room_id:
            _refuse(directory, lifecycle, operation, "mismatched_assignment_id",
                    "this room is not the lineage's assignment room")
        room_dir, state = _room_state(service.root, room_id)
        coord = state.get("coordination") or {}
        if coord.get("role") != "assignment" or assignment_id not in (coord.get("assignment_ids") or []):
            _refuse(directory, lifecycle, operation, "mismatched_assignment_id",
                    "the room's coordination pointer does not name this assignment")
        try:
            spec = service.spec(room_dir, state)
        except RoomError as exc:
            _refuse(directory, lifecycle, operation, "assignment_spec_changed", str(exc)[:200])
        if spec["sha256"] != lineage["assignment"]["spec_sha256"]:
            _refuse(directory, lifecycle, operation, "assignment_spec_changed",
                    "registered revision " + str(spec["revision"]) + " content digest "
                    + spec["sha256"] + " differs from the pinned lineage")
        try:
            checkpoint = service.checkpoint(room_dir, state)
        except RoomError as exc:
            reason = "tampered_checkpoint" if state.get("checkpoint") else "missing_checkpoint"
            _refuse(directory, lifecycle, operation, reason, str(exc)[:200])
        expected = {"spec_sha256": spec["sha256"], "candidate_sha256": checkpoint["candidate_sha256"],
                    "evidence_sha256": state["checkpoint_sha256"]}
        acceptance = None
        for entry in state["acceptances"]:
            if entry.get("request_id") == acceptance_request_id:
                acceptance = entry
        if acceptance is None:
            _refuse(directory, lifecycle, operation, "missing_acceptance",
                    "no acceptance for request " + acceptance_request_id)
        if any(acceptance.get(key) != value for key, value in expected.items()):
            _refuse(directory, lifecycle, operation, "stale_acceptance",
                    "the acceptance does not bind the current spec, candidate and checkpoint")
        request = state["requests"].get(acceptance_request_id)
        if (not request or request.get("role") != "reviewer" or request.get("state") != "completed"
                or request.get("review") != expected):
            _refuse(directory, lifecycle, operation, "stale_acceptance",
                    "the reviewer request does not bind the current spec, candidate and checkpoint")
        try:
            receipt = read(room_dir / request["receipt"])
        except (OSError, ValueError, KeyError):
            receipt = None
        if receipt is None or digest(receipt) != request["receipt_sha256"]:
            _refuse(directory, lifecycle, operation, "tampered_receipt",
                    "the reviewer receipt is missing or modified")
        # Evidence must postdate this assignment's own admission, not a reused room's history.
        start = lineage.get("assignment_start")
        if start is None:
            _refuse(directory, lifecycle, operation, "assignment_start_missing",
                    "re-open the assignment; this lineage predates provenance recording")
        if acceptance_request_id in (start.get("request_ids") or []):
            _refuse(directory, lifecycle, operation, "acceptance_predates_assignment",
                    "the acceptance request predates this assignment's admission")
        if (start.get("checkpoint_sha256")
                and state.get("checkpoint_sha256") == start["checkpoint_sha256"]):
            _refuse(directory, lifecycle, operation, "checkpoint_predates_assignment",
                    "the verification checkpoint predates this assignment's admission")
        import ao_workflow
        engineering = ao_workflow.latest(state, ENGINEERING_PURPOSES)
        if (engineering is None
                or engineering.get("request_id") in (start.get("request_ids") or [])):
            _refuse(directory, lifecycle, operation, "engineering_predates_assignment",
                    "the latest engineering result predates this assignment's admission")
        worktree = Path(checkpoint["candidate_path"])
        commit = _git(worktree, "rev-parse", "HEAD")
        if commit != checkpoint["candidate"]["head"]:
            _refuse(directory, lifecycle, operation, "tampered_checkpoint",
                    "the candidate HEAD moved after verification")
        try:
            clean = _status_clean(worktree)
        except RoomError as exc:
            _refuse(directory, lifecycle, operation, "worktree_unverified", str(exc)[:200])
        if not clean:
            _refuse(directory, lifecycle, operation, "worktree_dirty",
                    "the assignment worktree must be fully committed")
        baseline = lineage["owner"]["baseline_commit"]
        owned = lineage["owned_paths"]
        if commit == baseline:
            _refuse(directory, lifecycle, operation, "empty_diff",
                    "the candidate has no changes over the assignment baseline")
        if not _is_ancestor(worktree, baseline, commit):
            _refuse(directory, lifecycle, operation, "baseline_mismatch",
                    "the candidate HEAD does not descend from the lineage baseline")
        changed = _changed(worktree, baseline, commit)
        if not changed:
            _refuse(directory, lifecycle, operation, "empty_diff",
                    "the candidate has no changes over the assignment baseline")
        for entry in changed:
            for path in _entry_paths(entry):
                if not _covered(path, owned):
                    _refuse(directory, lifecycle, operation, "scope_drift",
                            "the candidate touches unowned path " + path)
        patch = _git_run(worktree, "diff", "--binary", baseline, commit)
        if patch.returncode:
            _refuse(directory, lifecycle, operation, "empty_diff",
                    "the baseline-to-candidate diff is unavailable")
        record = {"version": VERSION, "assignment_id": assignment_id, "assignment_room_id": room_id,
                  "owner_room_id": lineage["owner_room_id"],
                  "baseline_commit": baseline, "commit": commit, "changed_paths": changed,
                  "patch_sha256": hashlib.sha256(patch.stdout).hexdigest(),
                  "candidate_sha256": checkpoint["candidate_sha256"],
                  "spec": {"revision": spec["revision"], "sha256": spec["sha256"],
                           "record_sha256": state["spec_record_sha256"]},
                  "verification": {"id": checkpoint["id"], "checkpoint_path": state["checkpoint"],
                                   "checkpoint_sha256": state["checkpoint_sha256"]},
                  "acceptance": {"request_id": acceptance_request_id,
                                 "receipt_sha256": request["receipt_sha256"],
                                 "reviewer_session": acceptance["reviewer_session"],
                                 "reviewer_model": acceptance["reviewer_model"],
                                 "review_sha256": digest(acceptance["review"])},
                  "recorded_at": time.time()}
        prior = _dedupe(directory / "handbacks", record)
        if prior is not None:
            record = prior
        sha = digest(record)
        relative = "handbacks/" + sha + ".json"
        _store_immutable(directory / relative, record)
        pointer = {"path": relative, "sha256": sha, "commit": commit}
        if not any(item.get("sha256") == sha for item in lifecycle["handbacks"]):
            lifecycle["handbacks"].append(pointer)
        lifecycle["latest_handback"] = pointer
        lifecycle["state"] = HANDBACK_RECORDED
        lifecycle["updated_at"] = time.time()
        atomic(directory / "state.json", lifecycle)
        return record | {"path": relative, "handback_sha256": sha}


def _revalidate_handback(service, directory, lineage, lifecycle, handback, owner_worktree,
                         operation="import_preflight"):
    """Re-run every recorded handback fact against live room evidence and Git."""
    from ao_project_room import digest, read
    try:
        assign_dir, assign = _room_state(service.root, lineage["assignment_room_id"])
    except RoomError as exc:
        _refuse(directory, lifecycle, operation, "tampered_handback", str(exc)[:200])
    recorded = handback["acceptance"]
    entry = None
    for item in assign.get("acceptances") or []:
        if item.get("request_id") == recorded["request_id"]:
            entry = item
    if (entry is None or entry.get("receipt_sha256") != recorded["receipt_sha256"]
            or entry.get("reviewer_session") != recorded["reviewer_session"]
            or entry.get("reviewer_model") != recorded["reviewer_model"]
            or digest(entry.get("review")) != recorded["review_sha256"]):
        _refuse(directory, lifecycle, operation, "stale_acceptance",
                "the recorded acceptance no longer matches the assignment room")
    request = (assign.get("requests") or {}).get(recorded["request_id"])
    if not request or request.get("state") != "completed" or request.get("receipt_sha256") != recorded["receipt_sha256"]:
        _refuse(directory, lifecycle, operation, "stale_acceptance",
                "the reviewer request no longer matches the recorded acceptance")
    try:
        receipt = read(assign_dir / request["receipt"])
    except (OSError, ValueError, KeyError, TypeError):
        receipt = None
    if receipt is None or digest(receipt) != recorded["receipt_sha256"]:
        _refuse(directory, lifecycle, operation, "tampered_receipt",
                "the reviewer receipt is missing or modified")
    try:
        checkpoint = read(assign_dir / handback["verification"]["checkpoint_path"])
    except (OSError, ValueError, KeyError, TypeError):
        checkpoint = None
    if checkpoint is None or digest(checkpoint) != handback["verification"]["checkpoint_sha256"]:
        _refuse(directory, lifecycle, operation, "tampered_checkpoint",
                "the verification checkpoint is missing or modified")
    if _git_run(owner_worktree, "cat-file", "-e", handback["commit"] + "^{commit}").returncode:
        _refuse(directory, lifecycle, operation, "tampered_handback",
                "the handback commit is not in the shared repository")
    baseline = handback["baseline_commit"]
    if _changed(owner_worktree, baseline, handback["commit"]) != handback["changed_paths"]:
        _refuse(directory, lifecycle, operation, "tampered_handback",
                "the recorded changed paths no longer match the commit")
    patch = _git_run(owner_worktree, "diff", "--binary", baseline, handback["commit"])
    if patch.returncode or hashlib.sha256(patch.stdout).hexdigest() != handback["patch_sha256"]:
        _refuse(directory, lifecycle, operation, "tampered_handback",
                "the recorded patch digest no longer matches the commit")


def _authority_blockers(service, directory, lineage, lifecycle, owner_dir, owner):
    """The sorted set of authority conditions that currently block an owner-side import."""
    import ao_workflow
    from ao_project_room import TERMINAL
    blockers = []
    baseline = lineage["owner"]["baseline_commit"]
    try:
        if service.spec(owner_dir, owner)["sha256"] != lineage["owner"]["spec_sha256"]:
            blockers.append("owner_spec_changed")
    except RoomError:
        blockers.append("owner_spec_changed")
    try:
        assign_dir, assign = _room_state(service.root, lineage["assignment_room_id"])
        if service.spec(assign_dir, assign)["sha256"] != lineage["assignment"]["spec_sha256"]:
            blockers.append("assignment_spec_changed")
    except RoomError:
        blockers.append("assignment_spec_changed")
    try:
        if ao_workflow.handoff_record(owner_dir, owner)["baseline_commit"] != baseline:
            blockers.append("owner_baseline_changed")
    except RoomError:
        blockers.append("owner_baseline_changed")
    if any(request.get("state") not in TERMINAL for request in owner.get("requests", {}).values()):
        blockers.append("owner_busy")
    return sorted(blockers)


def import_preflight(service, room_id, assignment_id, handback_sha256):
    """Record the owner-side import readiness of the latest handback as observed facts."""
    from ao_project_room import atomic, digest, identifier, read
    identifier(room_id)
    identifier(assignment_id)
    _sha256(handback_sha256, "handback_sha256")
    operation = "import_preflight"
    with service.locked():
        directory = _assignment_dir(service.root, assignment_id)
        lineage = _lineage(directory)
        lifecycle = _lifecycle(directory)
        if lineage.get("owner_room_id") != room_id:
            _refuse(directory, lifecycle, operation, "mismatched_owner",
                    "this room is not the lineage's owner room")
        if lifecycle["state"] != HANDBACK_RECORDED:
            _refuse(directory, lifecycle, operation, "invalid_state",
                    "assignment state is " + lifecycle["state"])
        handback_path = directory / "handbacks" / (handback_sha256 + ".json")
        handback = None
        if handback_path.is_file():
            try:
                candidate = read(handback_path)
                if digest(candidate) == handback_sha256:
                    handback = candidate
            except (OSError, ValueError, RecursionError):
                handback = None
        if handback is None:
            _refuse(directory, lifecycle, operation, "tampered_handback",
                    "the handback record is missing or modified")
        if (lifecycle.get("latest_handback") or {}).get("sha256") != handback_sha256:
            _refuse(directory, lifecycle, operation, "stale_handback",
                    "only the latest handback may be preflighted")
        owner_dir, owner = _room_state(service.root, room_id)
        coord = owner.get("coordination") or {}
        if coord.get("role") != "owner" or assignment_id not in (coord.get("assignment_ids") or []):
            _refuse(directory, lifecycle, operation, "mismatched_owner",
                    "the room's coordination pointer does not name this assignment")
        worktree = Path(lineage["owner"]["worktree"])
        baseline = lineage["owner"]["baseline_commit"]
        owned = lineage["owned_paths"]
        _revalidate_handback(service, directory, lineage, lifecycle, handback, worktree)
        blockers, touched = [], []
        head = _git(worktree, "rev-parse", "HEAD")
        try:
            if not _status_clean(worktree):
                blockers.append("owner_worktree_dirty")
        except RoomError as exc:
            _refuse(directory, lifecycle, operation, "worktree_unverified", str(exc)[:200])
        blockers += _authority_blockers(service, directory, lineage, lifecycle, owner_dir, owner)
        for entry in _changed(worktree, baseline, head):
            if any(_covered(path, owned) for path in _entry_paths(entry)):
                touched.append(entry["path"])
        preview = "unavailable"
        conflicts = []
        if head == baseline or _is_ancestor(worktree, baseline, head):
            # Three-way preview against the actual common ancestor of the owner head and the
            # handback commit: for both descendants of the lineage baseline that ancestor is the
            # baseline, so the preview covers the whole baseline..commit range the handback
            # recorded. The two-argument form needs git 2.38; older git records "unavailable".
            # suggested_operation merges the handback commit itself: the same three-way
            # operation the preview models, carried into the owner history as provenance.
            merged = _git_run(worktree, "merge-tree", "--write-tree", head, handback["commit"])
            if merged.returncode == 0:
                preview = "clean"
            elif merged.returncode == 1:
                preview = "conflicts"
                for line in merged.stdout.decode().splitlines()[1:]:
                    if not line.strip():
                        break
                    match = re.match(r"[0-7]{6} [0-9a-f]{40} \d+\t(.+)", line)
                    if match:
                        conflicts.append(match.group(1))
                conflicts = sorted(set(conflicts))
        record = {"version": VERSION, "assignment_id": assignment_id,
                  "handback_sha256": handback_sha256, "owner_head_before": head,
                  "preview": preview, "conflicts": conflicts,
                  "owner_touched_assigned_paths": sorted(set(touched)),
                  "blockers": sorted(set(blockers)),
                  "suggested_operation": ["git", "-C", str(worktree), "merge", "--no-ff",
                                          "--no-edit", handback["commit"]],
                  "ready": not blockers and preview == "clean",
                  "recorded_at": time.time()}
        prior = _dedupe(directory / "import-preflights", record)
        if prior is not None:
            record = prior
        sha = digest(record)
        relative = "import-preflights/" + sha + ".json"
        _store_immutable(directory / relative, record)
        if not any(item.get("sha256") == sha for item in lifecycle["preflights"]):
            lifecycle["preflights"].append({"path": relative, "sha256": sha})
        lifecycle["updated_at"] = time.time()
        atomic(directory / "state.json", lifecycle)
        return record | {"path": relative, "preflight_sha256": sha}


def import_record(service, room_id, assignment_id, preflight_sha256, operation, resolution=None):
    """Record the operator's performed Git import against the latest valid preflight."""
    from ao_project_room import atomic, digest, identifier, nonempty, read
    identifier(room_id)
    identifier(assignment_id)
    _sha256(preflight_sha256, "preflight_sha256")
    nonempty(operation, "operation", OPERATION_BYTES)
    if resolution is not None:
        nonempty(resolution, "resolution", OPERATION_BYTES)
    tool = "import_record"
    with service.locked():
        directory = _assignment_dir(service.root, assignment_id)
        lineage = _lineage(directory)
        lifecycle = _lifecycle(directory)
        if lineage.get("owner_room_id") != room_id:
            _refuse(directory, lifecycle, tool, "mismatched_owner",
                    "this room is not the lineage's owner room")
        if lifecycle["state"] == IMPORTED:
            stored = lifecycle.get("import") or {}
            existing = None
            try:
                existing = read(directory / stored["path"])
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                existing = None
            if (existing is not None and existing.get("preflight_sha256") == preflight_sha256
                    and existing.get("operation") == operation
                    and existing.get("resolution") == resolution):
                return existing | {"path": stored["path"], "import_sha256": stored["sha256"]}
            _refuse(directory, lifecycle, tool, "immutable_import",
                    "the assignment is already imported; a different import cannot replace the record")
        if lifecycle["state"] != HANDBACK_RECORDED:
            _refuse(directory, lifecycle, tool, "invalid_state",
                    "assignment state is " + lifecycle["state"])
        preflight_path = directory / "import-preflights" / (preflight_sha256 + ".json")
        preflight = None
        if preflight_path.is_file():
            try:
                candidate = read(preflight_path)
                if digest(candidate) == preflight_sha256:
                    preflight = candidate
            except (OSError, ValueError, RecursionError):
                preflight = None
        if preflight is None:
            _refuse(directory, lifecycle, tool, "tampered_preflight",
                    "the preflight record is missing or modified")
        if not lifecycle.get("preflights") or lifecycle["preflights"][-1].get("sha256") != preflight_sha256:
            _refuse(directory, lifecycle, tool, "stale_preflight",
                    "only the latest preflight may be imported")
        owner_dir, owner = _room_state(service.root, room_id)
        coord = owner.get("coordination") or {}
        if coord.get("role") != "owner" or assignment_id not in (coord.get("assignment_ids") or []):
            _refuse(directory, lifecycle, tool, "mismatched_owner",
                    "the room's coordination pointer does not name this assignment")
        if preflight["blockers"]:
            _refuse(directory, lifecycle, tool, "unresolvable_blockers",
                    "no recorded resolution repairs " + ", ".join(preflight["blockers"]))
        # The preflight only observed authority once: recheck it now, under the record lock.
        if preflight["handback_sha256"] != (lifecycle.get("latest_handback") or {}).get("sha256"):
            _refuse(directory, lifecycle, tool, "stale_preflight",
                    "a later handback superseded the preflighted one; run a new preflight")
        if lifecycle.get("lineage_sha256") != digest(lineage):
            _refuse(directory, lifecycle, tool, "tampered_lineage",
                    "the lifecycle record no longer binds the pinned lineage")
        current = _authority_blockers(service, directory, lineage, lifecycle, owner_dir, owner)
        if current:
            _refuse(directory, lifecycle, tool, "preflight_authority_changed",
                    "since the preflight: " + ", ".join(current) + "; run a new preflight")
        worktree = Path(lineage["owner"]["worktree"])
        before = preflight["owner_head_before"]
        head = _git(worktree, "rev-parse", "HEAD")
        if head == before:
            _refuse(directory, lifecycle, tool, "import_not_performed",
                    "the owner HEAD did not move; no import operation is recorded")
        if not _is_ancestor(worktree, before, head):
            _refuse(directory, lifecycle, tool, "preflight_stale",
                    "the owner HEAD no longer descends from the preflight head")
        # Destination verification compares the actual post-import tree against the
        # recorded handback commit; it never trusts the operator's description.
        handback_path = directory / "handbacks" / (preflight["handback_sha256"] + ".json")
        handback = None
        try:
            candidate = read(handback_path)
            if digest(candidate) == preflight["handback_sha256"]:
                handback = candidate
        except (OSError, ValueError, RecursionError):
            handback = None
        if handback is None:
            _refuse(directory, lifecycle, tool, "tampered_handback",
                    "the handback record is missing or modified")
        _revalidate_handback(service, directory, lineage, lifecycle, handback, worktree,
                             operation=tool)
        destination = _changed(worktree, before, head)
        destination_paths = {path for entry in destination for path in _entry_paths(entry)}
        handback_paths = {path for entry in handback["changed_paths"] for path in _entry_paths(entry)}
        identical = {}
        for path in sorted(handback_paths):
            identical[path] = _git_run(worktree, "diff", "--quiet",
                                       handback["commit"], head, "--", path).returncode == 0
        # A path already holding the handback bytes, or the handback commit already in
        # ancestry, counts as incorporated; only a genuine advance without either refuses.
        incorporated = handback_paths & destination_paths
        ancestor = _is_ancestor(worktree, handback["commit"], head)
        if not incorporated and not ancestor:
            _refuse(directory, lifecycle, tool, "handback_not_incorporated",
                    "the owner HEAD advanced without incorporating the handback")
        already_incorporated = sorted(
            path for path in handback_paths - destination_paths if identical[path])
        missing = sorted(path for path in handback_paths - destination_paths if not identical[path])
        extra = sorted(destination_paths - handback_paths)
        modified = sorted(path for path in handback_paths & destination_paths if not identical[path])
        if missing:
            _refuse(directory, lifecycle, tool, "handback_not_incorporated",
                    "the destination lacks handback path(s) " + ", ".join(missing)
                    + "; a deliberate drop is a scope change and needs a new assignment")
        faithful = not extra and not modified
        kind = "faithful" if faithful else "resolved_with_modifications"
        if not faithful and resolution is None:
            _refuse(directory, lifecycle, tool, "integration_modifications_unresolved",
                    "the destination differs from the handback at " + ", ".join(modified)
                    + "; record a nonempty resolution")
        if not preflight["ready"] and resolution is None:
            _refuse(directory, lifecycle, tool, "integration_modifications_unresolved",
                    "the preflight was not ready; record a nonempty resolution")
        record = {"version": VERSION, "assignment_id": assignment_id,
                  "handback_sha256": preflight["handback_sha256"], "preflight_sha256": preflight_sha256,
                  "owner_head_before": before, "owner_head_after": head,
                  "destination_changes": destination, "content_identical": identical,
                  "kind": kind, "modified_paths": modified, "extra_paths": extra,
                  "missing_paths": missing, "already_incorporated_paths": already_incorporated,
                  "operation": operation, "resolution": resolution,
                  "recorded_at": time.time()}
        prior = _dedupe(directory / "imports", record)
        if prior is not None:
            record = prior
        sha = digest(record)
        relative = "imports/" + sha + ".json"
        _store_immutable(directory / relative, record)
        lifecycle["import"] = {"path": relative, "sha256": sha}
        lifecycle["state"] = IMPORTED
        lifecycle["updated_at"] = time.time()
        atomic(directory / "state.json", lifecycle)
        return record | {"path": relative, "import_sha256": sha}


def before_owner_accept(service, directory, state, checkpoint):
    """Refuse-only owner acceptance checks over the verified checkpoint candidate.

    An owner candidate that changes a path owned by an engaged assignment refuses
    outright. After a recorded import, the import head must be an ancestor of the
    checkpoint candidate and the latest engineering result must postdate the import.
    """
    coord = state.get("coordination")
    if not isinstance(coord, dict) or coord.get("role") != "owner":
        return
    import ao_workflow
    from ao_project_room import read
    worktree = Path(checkpoint["candidate_path"])
    head = checkpoint["candidate"]["head"]
    for assignment_id in coord.get("assignment_ids") or []:
        assignment_dir = _assignment_dir(service.root, assignment_id)
        try:
            evidence = _evidence(assignment_dir)
        except (RoomError, OSError, ValueError) as exc:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (" + str(exc)[:120] + ")") from None
        if evidence is None:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (no lineage or lifecycle record)")
        lineage, lifecycle = evidence
        if lineage.get("owner_room_id") != state["room_id"]:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (lineage names another owner room)")
        owned = lineage.get("owned_paths") or []
        status = lifecycle.get("state")
        if status in ENGAGED:
            baseline = lineage["owner"]["baseline_commit"]
            touched = sorted({path for entry in _changed(worktree, baseline, head)
                              for path in _entry_paths(entry) if _covered(path, owned)})
            if touched:
                raise RoomError("owned_by_active_assignment: the owner candidate touches owned path(s) "
                                + ", ".join(touched) + " of assignment " + assignment_id)
        elif status == IMPORTED:
            pointer = lifecycle.get("import") or {}
            record = None
            try:
                record = read(assignment_dir / pointer["path"])
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                record = None
            owner_head_after = (record or {}).get("owner_head_after")
            engineering = ao_workflow.latest(state, ENGINEERING_PURPOSES)
            fresh = (engineering is not None and record is not None
                     and engineering.get("created_at", 0) > record.get("recorded_at", 0))
            if (not owner_head_after
                    or (owner_head_after != head and not _is_ancestor(worktree, owner_head_after, head))
                    or not fresh):
                raise RoomError("integration_requires_fresh_engineering: the owner candidate does not "
                                "descend from the recorded import of assignment " + assignment_id
                                + " or the latest engineering result predates it")


def before_assignment_accept(service, directory, state, checkpoint):
    """Refuse-only assignment acceptance checks, run before the acceptance is appended."""
    coord = state.get("coordination")
    if not isinstance(coord, dict) or coord.get("role") != "assignment":
        return
    lineage = None
    for assignment_id in coord.get("assignment_ids") or []:
        assignment_dir = _assignment_dir(service.root, assignment_id)
        try:
            evidence = _evidence(assignment_dir)
        except (RoomError, OSError, ValueError) as exc:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (" + str(exc)[:120] + ")") from None
        if evidence is None:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (no lineage or lifecycle record)")
        candidate_lineage, candidate_lifecycle = evidence
        if candidate_lineage.get("assignment_room_id") != state["room_id"]:
            raise RoomError("coordination_evidence_unresolved: assignment " + assignment_id
                            + " lineage or lifecycle is missing, corrupt or mismatched; "
                            "restore the record before acceptance (lineage names another assignment room)")
        if candidate_lifecycle.get("state") in ENGAGED:
            lineage = candidate_lineage
    if lineage is None:
        return
    baseline = lineage["owner"]["baseline_commit"]
    owned = lineage.get("owned_paths") or []
    worktree = Path(checkpoint["candidate_path"])
    head = checkpoint["candidate"]["head"]
    if not _is_ancestor(worktree, baseline, head):
        raise RoomError("baseline_mismatch: the accepted assignment candidate does not descend "
                        "from the lineage baseline")
    for entry in _changed(worktree, baseline, head):
        for path in _entry_paths(entry):
            if not _covered(path, owned):
                raise RoomError("scope_drift: the accepted assignment candidate touches "
                                "unowned path " + path)


def _current_spec_sha(ao_root, room_id):
    """The room's current registered spec content digest, or None when unreadable."""
    from ao_project_room import read
    try:
        directory, state = _room_state(ao_root, room_id)
        if not state.get("spec"):
            return None
        spec = read(directory / state["spec"])
        return spec.get("sha256")
    except (RoomError, OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def _room_has_acceptance(ao_root, room_id):
    try:
        _, state = _room_state(ao_root, room_id)
        return bool(state.get("acceptances"))
    except RoomError:
        return False


def view(ao_root, state):
    """The neutral or populated coordination status projection; never mutates.

    Blockers are computed from saved room and lifecycle records only: no Git runs,
    no AO calls, and no transcript reads.
    """
    from ao_project_room import read
    policy_view = account_policy(ao_root, strict=False)
    coord = state.get("coordination")
    if not isinstance(coord, dict) or coord.get("role") not in ("owner", "assignment"):
        return {"role": None, "assignments": [], "account_policy": policy_view}
    assignments = []
    for assignment_id in coord.get("assignment_ids") or []:
        entry = {"assignment_id": assignment_id, "blockers": []}
        try:
            assignment_dir = _assignment_dir(ao_root, assignment_id)
            lineage = _lineage(assignment_dir)
            lifecycle = _lifecycle(assignment_dir)
            blockers = entry["blockers"]
            if _current_spec_sha(ao_root, lineage["owner_room_id"]) != lineage["owner"]["spec_sha256"]:
                blockers.append("owner_spec_changed")
            if _current_spec_sha(ao_root, lineage["assignment_room_id"]) != lineage["assignment"]["spec_sha256"]:
                blockers.append("assignment_spec_changed")
            if lifecycle["state"] == HANDBACK_RECORDED:
                blockers.append("handback_pending_import")
            elif lifecycle["state"] == ACTIVE and _room_has_acceptance(ao_root, lineage["assignment_room_id"]):
                blockers.append("awaiting_handback")
            elif lifecycle["state"] == SUPERSEDED:
                blockers.append("superseded")
            imported = None
            pointer = lifecycle.get("import") or {}
            try:
                record = read(assignment_dir / pointer["path"])
                imported = {"sha256": pointer["sha256"], "kind": record.get("kind"),
                            "owner_head_after": record.get("owner_head_after")}
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                pass
            latest = lifecycle.get("latest_handback") or {}
            pinned = {side: {key: (lineage.get(side) or {}).get(key)
                             for key in ("spec_revision", "spec_sha256", "spec_record_sha256")}
                      for side in ("owner", "assignment")}
            entry.update(owner_room_id=lineage["owner_room_id"],
                         assignment_room_id=lineage["assignment_room_id"],
                         account_policy=lineage.get("account_policy"),
                         state=lifecycle["state"], owned_paths=lineage.get("owned_paths") or [],
                         spec_pinned=pinned,
                         latest_handback=({"sha256": latest.get("sha256"), "commit": latest.get("commit")}
                                          if latest else None),
                         **{"import": imported})
        except (RoomError, OSError, ValueError, KeyError, TypeError, RecursionError):
            entry["state"] = "unreadable"
        assignments.append(entry)
    return {"role": coord["role"], "assignments": assignments, "account_policy": policy_view}
