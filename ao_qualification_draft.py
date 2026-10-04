"""Qualification draft: turn a verified source+executable observation into an adoptable revision.

A draft is computed evidence, never a mutation: ``draft`` validates every freshly fetched source
body and every model identifier embedded in the configured executable, writes one inert draft
directory under the private AO root, and stops. ``adopt`` is the only operation that moves the
controller's ``family_qualification`` pointer, and only after the draft's artifact, evidence,
provenance and embedded-model claims all re-verify. A draft never mixes stale and fresh captures:
when any source is unreadable, no draft is written at all.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid

from room import RoomError
import ao_engineering_model
import ao_model_qualification

MODEL_ID = re.compile(rb"claude-(fable|opus|sonnet)-\d+(?:-\d+)*")  # bytes form for the executable scan
DRAFTS = "qualification-drafts"        # under root (= <home>/ao)
ADOPTIONS = "qualification-adoptions"  # under root
MAX_EXECUTABLE_BYTES = 1_000_000_000
AUTO_ADOPT_AUTHORIZATION = ("standing authorization: family_qualification_auto_adopt is true "
                            "in the private ao/config.json")
CHUNK_BYTES = 8 * 1024 * 1024
CHUNK_OVERLAP = 128  # an identifier is tens of bytes; the overlap carries it across a chunk boundary
MAX_AUTHORIZATION_BYTES = 2000
MAX_PROPOSAL_BYTES = ao_model_qualification.MAX_ARTIFACT_BYTES


def version_tuple(identifier):
    """The digit components after claude-<family>- of one exact identifier, e.g. (5, 5)."""
    match = re.fullmatch(r"claude-[a-z]+-(\d+(?:-\d+)*)", identifier) if isinstance(identifier, str) else None
    if match is None:
        raise RoomError("A model identifier is an exact claude-<family>-<digits> name")
    return tuple(int(part) for part in match.group(1).split("-"))


def dated(identifier):
    """True for dated-snapshot identifiers like claude-fable-5-5-20260101: never proposals."""
    return any(component > 9999 for component in version_tuple(identifier))


def _version_string(value):
    return ".".join(str(part) for part in value) if value is not None else None


def embedded_model_ids(path, chunk_bytes=CHUNK_BYTES):
    """The claude-<family>-<digits> identifiers embedded in one owned executable's bytes.

    Streamed in chunks with an overlap so an identifier spanning a boundary is still seen;
    a missing, unreadable or oversized file refuses.
    """
    if not isinstance(path, str) or not path.strip():
        raise RoomError("The configured executable is unreadable")
    try:
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RoomError("The configured executable is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_EXECUTABLE_BYTES:
            raise RoomError("The configured executable is not a bounded regular file")
        found = set()
        tail = b""
        total = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while True:
                data = stream.read(chunk_bytes)
                total += len(data)
                if total > MAX_EXECUTABLE_BYTES:
                    raise RoomError("The configured executable exceeds its bounded scan size")
                buffer = tail + data
                final = len(data) < chunk_bytes  # short read means the file ended inside it
                # A match ending at the buffer end may be the prefix of a longer identifier
                # continuing into the next chunk; only the final chunk can close one.
                found.update(match.group(0).decode("ascii")
                             for match in MODEL_ID.finditer(buffer)
                             if final or match.end() < len(buffer))
                tail = buffer[-CHUNK_OVERLAP:]
                if not data:
                    break
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns) \
                != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise RoomError("The configured executable changed during the scan")
    finally:
        os.close(descriptor)
    return found


def _source_ids(body):
    """The model identifiers one source body's bytes name."""
    from ao_release_check import _model_ids
    return {identifier for _, identifier in _model_ids(body)}


def proposals(artifact, fresh_bodies, executable_ids, executable_version):
    """One row per qualified family; a row's ``to`` is the proposal or None with the reason."""
    from ao_engineering_model import family_member
    rows = []
    for family, entry in artifact["families"].items():
        current = entry["expected_model"]
        floor = entry.get("minimum_claude_code_version")
        source_ids = entry["source_ids"]
        row = {"family": family, "from": current, "to": None, "floor": None,
               "sources": list(source_ids), "executable_version": _version_string(executable_version),
               "reason": None}
        if any(fresh_bodies.get(source) is None for source in source_ids):
            row["reason"] = "source_unavailable"
            rows.append(row)
            continue
        captured = None
        for source in source_ids:
            ids = _source_ids(fresh_bodies[source])
            captured = ids if captured is None else captured & ids
        captured = captured or set()
        newer = lambda value: (family_member(family, value) and not dated(value)
                               and version_tuple(value) > version_tuple(current))
        in_sources = {identifier for identifier in captured if newer(identifier)}
        in_executable = {identifier for identifier in executable_ids if newer(identifier)}
        candidates = in_sources & in_executable
        if not candidates:
            if current not in captured:
                # The fresh captures no longer name the qualified model at all: a draft must
                # never re-cite them, and removal stays an explicit operator change.
                row["reason"] = "current_model_not_in_source_capture"
            elif in_executable:
                row["reason"] = "not_in_source_capture"
            elif in_sources:
                row["reason"] = "not_embedded_in_executable"
            else:
                row["reason"] = "none_newer"
        elif executable_version is None:
            row["reason"] = "executable_version_unknown"
        elif floor is not None and _minimum_tuple(floor) > executable_version:
            row["reason"] = "executable_below_family_floor"
        else:
            row["to"] = max(candidates, key=version_tuple)
            row["floor"] = _version_string(executable_version)
        rows.append(row)
    return rows


def _minimum_tuple(value):
    return tuple(int(part) for part in value.split("."))


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _proposal_key(rows):
    """The proposal a draft directory is keyed on: the family/from/to/floor tuples, sorted."""
    return sorted(({"family": row["family"], "from": row["from"], "to": row["to"],
                    "floor": row["floor"]}
                   for row in rows if isinstance(row, dict) and row.get("to") is not None),
                  key=lambda item: item["family"])


def draft(root, artifact, pointer, fresh_bodies, executable_ids, executable_version, now):
    """Write one inert, validated revision draft beneath <root>/qualification-drafts/; never config.

    Returns None when no family has a proposal or any artifact source lacks fresh bytes — a draft
    never mixes stale and fresh evidence. The directory name keys on the proposal, not the fresh
    bytes: an existing directory whose proposal.json records the same revision, previous pointer
    and proposal set is reused unchanged (a retained draft is a complete, self-verifying capture);
    a colliding name with a different proposal is refused.
    """
    rows = proposals(artifact, fresh_bodies, executable_ids, executable_version)
    if not any(row["to"] is not None for row in rows):
        return None
    if any(row.get("reason") == "current_model_not_in_source_capture" for row in rows):
        return None  # a revision never re-cites a capture that dropped a qualified model
    if any(fresh_bodies.get(descriptor["id"]) is None for descriptor in artifact["sources"]):
        return None
    root = Path(root).resolve()
    draft_artifact = copy.deepcopy(artifact)
    draft_artifact["revision"] = artifact["revision"] + 1
    draft_artifact["qualified_at"] = now
    for row in rows:
        if row["to"] is not None:
            draft_artifact["families"][row["family"]]["expected_model"] = row["to"]
            draft_artifact["families"][row["family"]]["minimum_claude_code_version"] = row["floor"]
    proposal_key = _proposal_key(rows)
    draft_dir = root / DRAFTS / ("r%d-%s" % (draft_artifact["revision"],
                                           ao_model_qualification.digest(proposal_key)[:12]))
    draft_artifact["sources"] = [
        {"id": descriptor["id"], "uri": descriptor["uri"], "captured_at": now,
         "sha256": hashlib.sha256(fresh_bodies[descriptor["id"]]).hexdigest(),
         "evidence_file": str(draft_dir / "evidence" / (descriptor["id"] + ".bin"))}
        for descriptor in artifact["sources"]]
    ao_model_qualification.validate_qualification(draft_artifact)
    artifact_sha256 = ao_model_qualification.digest(draft_artifact)
    proposal = {"revision": draft_artifact["revision"], "artifact_sha256": artifact_sha256,
                "previous": pointer, "rows": rows, "executable_version": _version_string(executable_version),
                "drafted_at": now}
    additions = {row["to"]: {"harness": "claude-code", "reasoning_effort": "max",
                             "minimum_claude_code_version": row["floor"]}
                 for row in rows
                 if row["to"] is not None and row["to"] not in ao_engineering_model.BUNDLED_MODELS}
    wrote = False
    if not draft_dir.exists():
        # Publish atomically: the complete draft is staged under a unique sibling name and one
        # rename moves it into place, so an interrupted publish can never leave a partial final
        # directory — at most an unverifiable staging directory this run removes.
        staging_parent = root / DRAFTS / "staging"
        staging_dir = staging_parent / (draft_dir.name + "." + uuid.uuid4().hex)
        staging_relative = staging_dir.relative_to(root)
        try:
            for descriptor in artifact["sources"]:
                ao_engineering_model.store_bytes_once(
                    root, str(staging_relative / "evidence" / (descriptor["id"] + ".bin")),
                    fresh_bodies[descriptor["id"]])
            ao_engineering_model.store_once(root, str(staging_relative / "artifact.json"), draft_artifact)
            ao_engineering_model.store_once(root, str(staging_relative / "engineering_models.json"), additions)
            ao_engineering_model.store_once(root, str(staging_relative / "proposal.json"), proposal)
            try:
                os.rename(staging_dir, draft_dir)
                wrote = True
            except OSError:
                if not draft_dir.exists():
                    raise  # the rename failed for a reason other than a concurrent publish
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
            try:
                staging_parent.rmdir()
            except OSError:
                pass
        if wrote:
            ao_model_qualification.verify_sources(draft_artifact)
    if not wrote:
        if not (draft_dir / "proposal.json").is_file():
            raise RoomError("A qualification draft directory already exists with other bytes; "
                            "adopt it or remove it deliberately")
        prior_bytes = ao_model_qualification._private_bytes(
            str(draft_dir / "proposal.json"), MAX_PROPOSAL_BYTES,
            "Qualification draft proposal")
        try:
            prior = json.loads(prior_bytes)
        except ValueError as exc:
            raise RoomError("An existing qualification draft proposal is unreadable") from exc
        if (not isinstance(prior, dict) or prior.get("revision") != draft_artifact["revision"]
                or prior.get("previous") != pointer
                or _proposal_key(prior.get("rows") if isinstance(prior.get("rows"), list) else [])
                != proposal_key):
            raise RoomError("A qualification draft directory already exists with other bytes; "
                            "adopt it or remove it deliberately")
        try:
            prior_artifact = json.loads(ao_model_qualification._private_bytes(
                str(draft_dir / "artifact.json"), ao_model_qualification.MAX_ARTIFACT_BYTES,
                "Qualification draft artifact"))
        except ValueError as exc:
            raise RoomError("An existing qualification draft artifact is unreadable") from exc
        artifact_sha256 = prior.get("artifact_sha256")
        if not isinstance(prior_artifact, dict) or ao_model_qualification.digest(prior_artifact) != artifact_sha256:
            raise RoomError("An existing qualification draft artifact does not match its proposal")
        ao_model_qualification.verify_sources(prior_artifact)
    return {"draft_dir": str(draft_dir), "revision": draft_artifact["revision"],
            "artifact_sha256": artifact_sha256, "rows": rows,
            "adopt_command": ("python3 ao_qualification_draft.py --home " + shlex.quote(str(root.parent))
                              + " adopt --draft " + shlex.quote(str(draft_dir))
                              + " --authorization " + shlex.quote("<operator authorization>"))}


def _draft_directory(root, draft_dir):
    """The literal draft path, confined to a direct child of <root>/qualification-drafts."""
    base = Path(os.path.normpath(str(Path(root) / DRAFTS)))
    if not isinstance(draft_dir, str) or not draft_dir.strip():
        raise RoomError("A qualification draft names a directory inside qualification-drafts")
    candidate = Path(os.path.normpath(draft_dir))
    if not candidate.is_absolute() or candidate.parent != base:
        raise RoomError("A qualification draft is a direct child of the private qualification-drafts directory")
    return candidate


def _draft_additions(directory):
    """The draft's validated engineering_models additions; a bundled or malformed entry refuses."""
    try:
        additions = json.loads(ao_model_qualification._private_bytes(
            str(directory / "engineering_models.json"), MAX_PROPOSAL_BYTES,
            "Qualification draft engineering models"))
    except ValueError as exc:
        raise RoomError("A qualification draft's engineering model additions are not valid JSON") from exc
    if not isinstance(additions, dict):
        raise RoomError("A qualification draft's engineering model additions are not an object")
    for model, entry in additions.items():
        if model in ao_engineering_model.BUNDLED_MODELS:
            raise RoomError("A qualification draft never alters a bundled engineering model")
        ao_engineering_model._entry(model, entry)
    return additions


def adopt(root, draft_dir, authorization, executable_ids, executable_version):
    """Move the controller's family_qualification pointer to one verified draft, with evidence.

    Refuses unless the draft artifact still matches the proposal, its retained evidence verifies,
    the configured pointer still equals the draft's previous pointer, the revision is exactly the
    next one, every proposed identifier is still embedded in the configured executable, and that
    executable's version is known and meets every proposed floor. The candidate configuration is
    proven against a policy reload before the pointer switches, so a failed switch never needs a
    restore; a pointer that switched while its adoption record was lost is completed and reported
    as recovered. The one already-adopted pointer returns unchanged instead of failing.
    """
    import ao_project_room
    if not isinstance(authorization, str) or not authorization.strip():
        raise RoomError("Adopting a qualification draft requires explicit operator authorization")
    if len(authorization.encode("utf-8")) > MAX_AUTHORIZATION_BYTES:
        raise RoomError("A qualification adoption authorization exceeds its bounded size")
    root = Path(root).resolve()
    directory = _draft_directory(root, draft_dir)
    raw_proposal = ao_model_qualification._private_bytes(
        str(directory / "proposal.json"), MAX_PROPOSAL_BYTES, "Qualification draft proposal")
    try:
        proposal = json.loads(raw_proposal)
    except ValueError as exc:
        raise RoomError("A qualification draft proposal is not valid JSON") from exc
    raw_artifact = ao_model_qualification._private_bytes(
        str(directory / "artifact.json"), ao_model_qualification.MAX_ARTIFACT_BYTES,
        "Qualification draft artifact")
    try:
        artifact = json.loads(raw_artifact)
    except ValueError as exc:
        raise RoomError("A qualification draft artifact is not valid JSON") from exc
    if (not isinstance(proposal, dict)
            or set(proposal) != {"revision", "artifact_sha256", "previous", "rows",
                                 "executable_version", "drafted_at"}
            or not isinstance(artifact, dict)
            or ao_model_qualification.digest(artifact) != proposal.get("artifact_sha256")):
        raise RoomError("A qualification draft does not match its proposal digest")
    ao_model_qualification.validate_qualification(artifact)
    ao_model_qualification.verify_sources(artifact)
    rows = proposal.get("rows")
    if not isinstance(rows, list):
        raise RoomError("A qualification draft proposal carries its rows")
    record_relative = ADOPTIONS + "/r%d-%s.json" % (artifact["revision"], proposal["artifact_sha256"][:12])
    pointer = ao_engineering_model.configured_qualification(root)
    adopted_pointer = {"path": str(directory / "artifact.json"),
                       "sha256": proposal["artifact_sha256"]}
    if pointer == adopted_pointer:
        if (root / record_relative).is_file():
            return {"adopted": False, "already_adopted": True, "revision": artifact.get("revision"),
                    "pointer": adopted_pointer}
        # The switch committed but the adoption record was lost before it was fsynced; complete
        # the durable record now rather than leaving an unrecorded adoption.
        additions = _draft_additions(directory)
        backups = sorted(root.glob("config.json.bak-*-r%d-%s"
                                   % (artifact["revision"], proposal["artifact_sha256"][:12])))
        record = {"adopted_at": _utc_now(), "authorization": authorization,
                  "previous_pointer": proposal["previous"], "pointer": adopted_pointer,
                  "rows": rows, "engineering_models_added": sorted(additions),
                  "config_backup": str(backups[-1]) if backups else None, "recovered": True}
        ao_engineering_model.store_once(root, record_relative, record)
        return {"adopted": True, "recovered": True, "revision": artifact["revision"],
                "pointer": adopted_pointer, "engineering_models_added": sorted(additions),
                "record": str(root / record_relative), "config_backup": record["config_backup"]}
    if pointer != proposal.get("previous"):
        raise RoomError("Qualification pointer changed since the draft; draft again")
    current_revision = 0
    if pointer is not None:
        current_revision = ao_model_qualification.load(pointer)["artifact"]["revision"]
    if not isinstance(artifact.get("revision"), int) or artifact["revision"] != current_revision + 1:
        raise RoomError("A qualification draft must be exactly the next revision")
    missing = [row["to"] for row in rows
               if isinstance(row, dict) and row.get("to") and row["to"] not in executable_ids]
    if missing:
        raise RoomError("A proposed model is no longer embedded in the configured executable: " + ", ".join(missing))
    if executable_version is None:
        raise RoomError("The configured executable's version is unknown at adoption")
    below = [row["floor"] for row in rows
             if isinstance(row, dict) and isinstance(row.get("floor"), str)
             and _minimum_tuple(row["floor"]) > executable_version]
    if below:
        raise RoomError("The configured executable %s is below the proposed floor %s"
                        % (_version_string(executable_version), below[0]))
    additions = _draft_additions(directory)
    config_path = root / "config.json"
    raw_config = ao_model_qualification._private_bytes(str(config_path), ao_engineering_model.MAX_CONFIG,
                                                       "AO configuration")
    config = json.loads(raw_config)
    if not isinstance(config, dict):
        raise RoomError("AO configuration is not an object")
    configured = config.get(ao_engineering_model.CONFIG_KEY)
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise RoomError("AO engineering_models configuration is not an object")
    for model, entry in additions.items():
        existing = configured.get(model)
        if existing is not None and existing != entry:
            raise RoomError("An engineering_models entry already exists with a different value: " + model)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_relative = ("config.json.bak-%s-r%d-%s"
                       % (stamp, artifact["revision"], proposal["artifact_sha256"][:12]))
    ao_engineering_model.store_bytes_once(root, backup_relative, raw_config)
    new_config = copy.deepcopy(config)
    new_config[ao_engineering_model.QUALIFICATION_KEY] = adopted_pointer
    new_config[ao_engineering_model.CONFIG_KEY] = {**configured, **additions}
    candidate_root = Path(tempfile.mkdtemp(dir=root, prefix="adopt-validate-"))
    try:  # mkdtemp creates the mode-0700 sibling directory; it is always removed.
        (candidate_root / "config.json").write_bytes(
            json.dumps(new_config, sort_keys=True).encode("utf-8"))
        try:
            # The adopted pointer's path is absolute, so the artifact resolves from this root.
            ao_engineering_model.effective_policy(candidate_root)
        except Exception as exc:
            raise RoomError("The candidate qualification configuration failed its effective-policy "
                            "load before the switch; the configuration is unchanged") from exc
    finally:
        shutil.rmtree(candidate_root, ignore_errors=True)
    ao_project_room.atomic(config_path, new_config)
    record = {"adopted_at": _utc_now(), "authorization": authorization, "previous_pointer": proposal["previous"],
              "pointer": adopted_pointer, "rows": rows, "engineering_models_added": sorted(additions),
              "config_backup": str(root / backup_relative)}
    ao_engineering_model.store_once(root, record_relative, record)
    return {"adopted": True, "revision": artifact["revision"], "pointer": adopted_pointer,
            "engineering_models_added": sorted(additions),
            "record": str(root / record_relative), "config_backup": str(root / backup_relative)}


def _claude_bin(home, override):
    """The configured controller executable path; --claude-bin overrides it for the scan."""
    if override is not None:
        return override
    try:
        config = json.loads(Path(home / "config.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise RoomError("The controller configuration is unreadable") from exc
    value = config.get("claude_bin") if isinstance(config, dict) else None
    if not isinstance(value, str) or not value:
        raise RoomError("The controller configuration names no claude_bin")
    return value


def _draft_command(root, claude_bin):
    import ao_release_check
    pointer = ao_engineering_model.configured_qualification(root)
    if pointer is None:
        return {"outcome": "no_qualification", "rows": [], "draft": None}
    artifact = ao_model_qualification.load(pointer)["artifact"]
    fresh_bodies = {}
    for descriptor in artifact["sources"]:
        if not ao_release_check._source_uri_allowed(descriptor.get("uri")):
            fresh_bodies[descriptor["id"]] = None
            continue
        try:
            fresh_bodies[descriptor["id"]] = ao_release_check._fetch_source(descriptor["uri"])
        except Exception:
            fresh_bodies[descriptor["id"]] = None
    executable_ids = embedded_model_ids(claude_bin)
    executable_version = ao_release_check._claude_version(subprocess.run, claude_bin)
    rows = proposals(artifact, fresh_bodies, executable_ids, executable_version)
    result = draft(root, artifact, pointer, fresh_bodies, executable_ids, executable_version, _utc_now())
    outcome = "drafted" if result is not None else ("proposal" if any(row["to"] for row in rows) else "none")
    return {"outcome": outcome, "rows": rows, "draft": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(Path.home() / ".project-room"))
    commands = parser.add_subparsers(dest="command", required=True)
    draft_parser = commands.add_parser("draft")
    draft_parser.add_argument("--claude-bin", default=None)
    adopt_parser = commands.add_parser("adopt")
    adopt_parser.add_argument("--draft", required=True)
    adopt_parser.add_argument("--authorization", required=True)
    args = parser.parse_args()
    home = Path(args.home).expanduser()
    root = home / "ao"
    claude_bin = _claude_bin(home, getattr(args, "claude_bin", None))
    if args.command == "draft":
        result = _draft_command(root, claude_bin)
    else:
        import ao_release_check
        executable_ids = embedded_model_ids(claude_bin)
        executable_version = ao_release_check._claude_version(subprocess.run, claude_bin)
        result = adopt(root, args.draft, args.authorization, executable_ids, executable_version)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
