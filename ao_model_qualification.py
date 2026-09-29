"""Operator-selected family qualification: bounded artifact validation, room retention and admission.

A private controller configuration pointer ``family_qualification`` names one bounded, owned,
non-symlink JSON artifact and its digest. The artifact declares exactly the supported source scope,
a subset of the bundled engineering families mapped to exact expected models and compatibility
floors, and the retained source descriptors. Every descriptor's complete evidence bytes are read
and hashed before anything is snapshotted into a room, and the explicitly selected capture is then
retained privately inside the room's own qualification area as bounded immutable owned bytes. Only
those selected captures are retained; nothing else is copied, nothing is embedded in any
distributable file, and no unrelated file or secret is read. The room keeps the validated artifact
value create-once under its qualification area and binds it through the pinned version 3 policy.
Admission and history thereafter verify the retained room bytes and never need the private
originals again.

Admission verifies the exact current epoch's own pinned artifact bytes (or an explicitly named
committed target's) together with the room-retained source evidence bytes, the compatible
executable floor and the known supported nonsecret configuration sources, and returns bounded
evidence for an audit. It distinguishes operator-qualified source applicability, concrete
contradictions that were actually checked, and runtime facts that remain unobserved. Nothing here
calls a model, probes an account or provider, sorts model numbers, or asserts availability,
entitlement, native enforcement or effective provider effort.
"""

import copy
from collections.abc import Mapping
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import re
import stat

from room import RoomError

FORMAT = "project_room_family_qualification_v1"
SCOPE = {"provider_class": "anthropic_first_party", "alias_remaps": "forbidden", "availability": "not_asserted"}
ARTIFACT_FIELDS = frozenset(("format", "revision", "qualified_at", "scope", "families", "sources"))
FAMILY_FIELDS = frozenset(("expected_model", "source_ids", "minimum_claude_code_version"))
SOURCE_FIELDS = frozenset(("id", "uri", "captured_at", "sha256", "evidence_file"))
SNAPSHOT_FIELDS = frozenset(("artifact", "sha256", "record"))
SNAPSHOT_FIELDS_RETAINED = SNAPSHOT_FIELDS | {"evidence"}
BASE = "engineering-model/qualifications"
MAX_ARTIFACT_BYTES = 262_144
MAX_RECORD_BYTES = MAX_ARTIFACT_BYTES * 4
MAX_EVIDENCE_BYTES = 4_000_000
MAX_SOURCES = 32
MAX_FAMILIES = 8
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
EXACT = re.compile(r"claude-[a-z]+(-[0-9]+)+")
HEX = re.compile(r"[0-9a-f]{64}")
MINIMUM = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
RUNTIME_UNOBSERVED = ("the native process's complete inherited environment",
                      "the actual provider that served the turn",
                      "account availability, entitlement or quota",
                      "effective provider effort",
                      "native enforcement of the configured settings")


def digest(value):
    """The shared canonical digest of one JSON value; the same digest binds the room policy."""
    from ao_project_room import digest as project_digest
    return project_digest(value)


def record_path(sha256):
    """The room-relative immutable record of one qualification artifact value."""
    return BASE + "/" + sha256 + ".json"


def evidence_path(sha256, descriptor):
    """The room-relative immutable record of one explicitly selected source capture."""
    return BASE + "/" + sha256 + "/" + descriptor["id"] + "-" + descriptor["sha256"] + ".bin"


def evidence_entries(artifact, sha256):
    """The exact room-relative retained records one artifact's selected captures bind."""
    sources = artifact.get("sources") if isinstance(artifact, dict) else None
    if not isinstance(sources, list) or not sources:
        raise RoomError("A family qualification snapshot needs its selected source descriptors")
    entries = []
    for descriptor in sources:
        identity, content = descriptor.get("id"), descriptor.get("sha256")
        if (not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity)
                or not isinstance(content, str) or not HEX.fullmatch(content)):
            raise RoomError("A family qualification snapshot needs valid source descriptors")
        entries.append({"id": identity, "sha256": content, "record": evidence_path(sha256, descriptor)})
    return entries


def _minimum_tuple(value):
    return tuple(int(part) for part in value.split("."))


def _utc(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RoomError(label + " must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RoomError(label + " must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RoomError(label + " must carry an explicit UTC offset")
    return parsed


def _private_bytes(path, maximum, label):
    """Complete bounded bytes of one private source file, read-only and stable across the read.

    The file identity, size and mtime are re-checked after the read and against the named path, so
    a file that changes while it is being read never silently counts as the captured immutable
    source. Symlinks are refused at every parent.
    """
    if not isinstance(path, str) or not path.strip():
        raise RoomError(label + " must be an absolute private path")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RoomError(label + " must be an absolute private path")
    for parent in (candidate, *candidate.parents):
        if parent.is_symlink():
            raise RoomError(label + " traverses a symlink")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RoomError(label + " is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022
                or before.st_size > maximum):
            raise RoomError(label + " is not a bounded owned private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    finally:
        os.close(descriptor)
    if len(data) > maximum:
        raise RoomError(label + " is oversized")
    if (len(data) != before.st_size
            or (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)):
        raise RoomError(label + " changed while being read")
    try:
        named = candidate.lstat()
    except OSError as exc:
        raise RoomError(label + " is unreadable") from exc
    if ((named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or not stat.S_ISREG(named.st_mode)):
        raise RoomError(label + " changed while being read")
    return data


def file_digest(path, maximum=MAX_EVIDENCE_BYTES):
    """SHA256 of the complete bytes of one bounded owned private file; never its contents."""
    return hashlib.sha256(_private_bytes(path, maximum, "Private file")).hexdigest()


def validate_qualification(artifact, *, bundled=True):
    """Structural validation of one complete artifact value; private evidence bytes stay private.

    ``bundled`` False validates a recorded snapshot against itself, without comparing its declared
    floors to the bundled table in force, so a later bundled-table change can never reinterpret an
    untouched historical record. Malformed types always refuse cleanly.
    """
    if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_FIELDS:
        raise RoomError("A family qualification declares exactly format, revision, qualified_at, scope, families "
                        "and sources")
    if artifact["format"] != FORMAT:
        raise RoomError("Unsupported family qualification format")
    if type(artifact["revision"]) is not int or artifact["revision"] < 1:
        raise RoomError("A family qualification revision is a positive integer")
    qualified_at = _utc(artifact["qualified_at"], "qualified_at")
    if artifact["scope"] != SCOPE:
        raise RoomError("A family qualification declares the supported scope exactly: anthropic_first_party, "
                        "alias remaps forbidden and availability not asserted")
    sources = artifact["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise RoomError(f"A family qualification retains 1-{MAX_SOURCES} source descriptors")
    described = {}
    for descriptor in sources:
        if not isinstance(descriptor, dict) or set(descriptor) != SOURCE_FIELDS:
            raise RoomError("Each source descriptor declares exactly id, uri, captured_at, sha256 and evidence_file")
        identity = descriptor["id"]
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity) or identity in described:
            raise RoomError("Source descriptor ids are unique lowercase identifiers")
        uri = descriptor["uri"]
        if (not isinstance(uri, str) or not uri.startswith("https://") or " " in uri or "@" in uri[8:].split("/")[0]
                or not uri[8:].split("/")[0]):
            raise RoomError("Source descriptors name an official https documentation source without credentials")
        if not isinstance(descriptor["evidence_file"], str) or not descriptor["evidence_file"].strip():
            raise RoomError("Source descriptors retain the private evidence file they were captured from")
        if not isinstance(descriptor["sha256"], str) or not HEX.fullmatch(descriptor["sha256"]):
            raise RoomError("Source descriptors pin the SHA256 of their complete retained evidence bytes")
        if _utc(descriptor["captured_at"], "captured_at") > qualified_at:
            raise RoomError("A family qualification cannot predate the source evidence it cites")
        described[identity] = descriptor
    from ao_engineering_model import BUNDLED_FAMILIES, family_member
    basis = BUNDLED_FAMILIES if bundled else None
    families = artifact["families"]
    if not isinstance(families, dict) or not 1 <= len(families) <= MAX_FAMILIES:
        raise RoomError(f"A family qualification maps 1-{MAX_FAMILIES} bundled families")
    for family, entry in families.items():
        if basis is not None and family not in basis:
            raise RoomError("A family qualification maps only existing bundled families")
        if (not isinstance(entry, dict) or not set(entry) <= FAMILY_FIELDS
                or "expected_model" not in entry or "source_ids" not in entry):
            raise RoomError("Each qualified family declares expected_model, source_ids and an optional compatibility "
                            "floor")
        if (not isinstance(entry["expected_model"], str) or not EXACT.fullmatch(entry["expected_model"])
                or not family_member(family, entry["expected_model"])):
            raise RoomError("Each qualified family maps to an exact identifier of that same family")
        source_ids = entry["source_ids"]
        if (not isinstance(source_ids, list) or not source_ids
                or any(not isinstance(item, str) for item in source_ids)
                or len(set(source_ids)) != len(source_ids)
                or any(item not in described for item in source_ids)):
            raise RoomError("Each qualified family names nonempty retained source descriptors")
        floor = entry.get("minimum_claude_code_version")
        bundled_floor = None if basis is None else basis[family].get("minimum_claude_code_version")
        if floor is not None:
            if not isinstance(floor, str) or not MINIMUM.fullmatch(floor):
                raise RoomError("A family compatibility floor is an exact X.Y.Z Claude Code version")
            if bundled_floor is not None and _minimum_tuple(floor) < _minimum_tuple(bundled_floor):
                raise RoomError("A family qualification never weakens a bundled compatibility floor")
        elif bundled_floor is not None:
            raise RoomError("A bundled compatibility floor is retained, never removed")
    return artifact


def verify_sources(artifact, *, bundled=True):
    """Verify the complete bytes of every declared private evidence file, read-only.

    This is the active configuration load path: it may require the original operator input files.
    The preserved room proof is the capture retained inside the room by ``retain``. ``bundled``
    False validates a recorded snapshot against itself, so a later bundled-table change cannot
    reinterpret preserved evidence.
    """
    validate_qualification(artifact, bundled=bundled)
    verified = []
    for descriptor in artifact["sources"]:
        data = _private_bytes(descriptor["evidence_file"], MAX_EVIDENCE_BYTES,
                              "Source evidence " + descriptor["id"])
        if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise RoomError("Source evidence " + descriptor["id"] + " does not match its retained SHA256")
        verified.append({"id": descriptor["id"], "uri": descriptor["uri"], "captured_at": descriptor["captured_at"],
                         "sha256": descriptor["sha256"], "bytes": len(data)})
    return verified


def load(pointer):
    """Read, validate and verify one private {path, sha256} pointer; return {artifact, sha256}.

    This is the active configuration load path; it may require the private operator input sources.
    The preserved room proof is the retained artifact and captures instead.
    """
    if (not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}
            or not isinstance(pointer.get("sha256"), str) or not HEX.fullmatch(pointer["sha256"])):
        raise RoomError("family_qualification configuration declares exactly path and sha256")
    raw = _private_bytes(pointer["path"], MAX_ARTIFACT_BYTES, "Family qualification artifact")
    try:
        artifact = json.loads(raw)
    except ValueError as exc:
        raise RoomError("Family qualification artifact is not valid JSON") from exc
    validate_qualification(artifact)
    if digest(artifact) != pointer["sha256"]:
        raise RoomError("Family qualification artifact does not match its configured digest")
    verify_sources(artifact)  # complete source bytes before anything is snapshotted
    return {"artifact": artifact, "sha256": pointer["sha256"]}


def snapshot(qualification):
    """The room-retained snapshot of one validated policy qualification {artifact, sha256}.

    The snapshot additionally binds every explicitly selected source capture to its exact
    room-relative retained record, so retention and later admission never need the private
    originals again. The artifact value and its original descriptors are never modified.
    """
    if (not isinstance(qualification, dict) or set(qualification) != {"artifact", "sha256"}
            or not isinstance(qualification.get("sha256"), str) or not HEX.fullmatch(qualification["sha256"])):
        raise RoomError("A family qualification snapshot declares its artifact and digest")
    validate_qualification(qualification["artifact"])
    if digest(qualification["artifact"]) != qualification["sha256"]:
        raise RoomError("A family qualification snapshot does not match its digest")
    sha256 = qualification["sha256"]
    return {"artifact": copy.deepcopy(qualification["artifact"]), "sha256": sha256,
            "record": record_path(sha256), "evidence": evidence_entries(qualification["artifact"], sha256)}


def retain(directory, value):
    """Retain one snapshot and its explicitly selected source captures inside the room, create-once.

    The artifact record and every selected capture are written with safe owned immutable byte
    writes and durable barriers. Repeating an identical retention re-reads the room bytes when they
    are already present and republishes the file and ancestor barriers instead of accepting a
    directory entry that may have been lost; the private originals are then no longer required.
    Only the explicitly selected captures are read and retained, and no other file or secret is
    copied.
    """
    from ao_engineering_model import store_bytes_once, store_once
    if (not isinstance(value, dict) or set(value) not in (SNAPSHOT_FIELDS, SNAPSHOT_FIELDS_RETAINED)
            or not isinstance(value.get("sha256"), str) or not HEX.fullmatch(value["sha256"])
            or not isinstance(value.get("record"), str) or value["record"] != record_path(value["sha256"])):
        raise RoomError("A family qualification snapshot declares its artifact, digest and retained record")
    artifact, sha256 = value["artifact"], value["sha256"]
    validate_qualification(artifact, bundled=False)
    if digest(artifact) != sha256:
        raise RoomError("A family qualification snapshot does not match its digest")
    entries = evidence_entries(artifact, sha256)
    if "evidence" in value and value["evidence"] != entries:
        raise RoomError("A family qualification snapshot declares inconsistent retained evidence")
    if store_once(directory, value["record"], artifact) != sha256:
        raise RoomError("Retained family qualification evidence does not match its digest")
    for descriptor, entry in zip(artifact["sources"], entries):
        retained = None
        try:
            retained = _room_bytes(directory, entry["record"], MAX_EVIDENCE_BYTES,
                                   "Retained source evidence " + descriptor["id"])
        except RoomError:
            retained = None
        if retained is not None:
            if hashlib.sha256(retained).hexdigest() != descriptor["sha256"]:
                raise RoomError("Retained source evidence " + descriptor["id"] + " was changed; never edit or "
                                "replace it")
            data = retained
        else:
            data = _private_bytes(descriptor["evidence_file"], MAX_EVIDENCE_BYTES,
                                  "Source evidence " + descriptor["id"])
            if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
                raise RoomError("Source evidence " + descriptor["id"] + " does not match its retained SHA256")
        if store_bytes_once(directory, entry["record"], data) != descriptor["sha256"]:
            raise RoomError("Retained source evidence does not match its digest")
    return {**copy.deepcopy(value), "evidence": entries}


def _room_bytes(directory, relative, maximum, message):
    """Complete bounded bytes of one room-relative retained file; symlinks are never absence."""
    from ao_engineering_model import _owned_directory
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise RoomError(message)
    folder = Path(directory)
    for part in parts[:-1]:
        folder = folder / part
        if not _owned_directory(folder, message):
            raise RoomError(message)
    path = Path(directory).joinpath(relative)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RoomError(message) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > maximum:
        raise RoomError(message)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RoomError(message) from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > maximum:
                raise RoomError(message)
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    finally:
        os.close(descriptor)
    if (len(data) != before.st_size
            or (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns)
            != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns)):
        raise RoomError(message)
    try:
        named = path.lstat()
    except OSError as exc:
        raise RoomError(message) from exc
    if ((named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or not stat.S_ISREG(named.st_mode)):
        raise RoomError(message)
    return data


def verify_reference(directory, reference, message):
    """Load and verify one compact room-relative qualification reference against its retained bytes.

    The artifact record and every declared source capture are read from the room only; the private
    originals are never consulted. Missing, changed, symlinked or unowned retained bytes refuse.
    """
    if (not isinstance(reference, dict) or set(reference) not in ({"sha256", "record"},
                                                                  {"sha256", "record", "evidence"})
            or not isinstance(reference.get("sha256"), str) or not HEX.fullmatch(reference["sha256"])
            or reference.get("record") != record_path(reference["sha256"])):
        raise RoomError(message)
    try:
        artifact = json.loads(_room_bytes(directory, reference["record"], MAX_RECORD_BYTES, message))
    except ValueError as exc:
        raise RoomError(message) from exc
    try:
        validate_qualification(artifact, bundled=False)
    except RoomError as exc:
        raise RoomError(message) from exc
    if digest(artifact) != reference["sha256"]:
        raise RoomError(message)
    entries = reference.get("evidence")
    if entries != evidence_entries(artifact, reference["sha256"]):
        raise RoomError(message)
    sources = []
    for descriptor in artifact["sources"]:
        data = _room_bytes(directory, evidence_path(reference["sha256"], descriptor), MAX_EVIDENCE_BYTES, message)
        if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
            raise RoomError(message)
        sources.append({"id": descriptor["id"], "uri": descriptor["uri"], "captured_at": descriptor["captured_at"],
                        "sha256": descriptor["sha256"], "bytes": len(data)})
    return {"artifact": artifact, "sha256": reference["sha256"], "record": reference["record"],
            "evidence": copy.deepcopy(entries), "retained_evidence": True, "sources": sources}


def read_retained(directory, value, message):
    """Validate one room-retained snapshot against the immutable room bytes.

    A snapshot that declares retained evidence verifies every retained source capture; a historical
    three-field snapshot keeps its original bytes readable but reports that it holds no retained
    evidence. Missing, changed or symlinked retained bytes refuse.
    """
    from ao_engineering_model import _read
    if (not isinstance(value, dict) or set(value) not in (SNAPSHOT_FIELDS, SNAPSHOT_FIELDS_RETAINED)
            or not isinstance(value.get("sha256"), str) or not HEX.fullmatch(value["sha256"])
            or not isinstance(value.get("record"), str) or value["record"] != record_path(value["sha256"])):
        raise RoomError(message)
    if "evidence" not in value:
        try:
            validate_qualification(value["artifact"], bundled=False)
        except RoomError as exc:
            raise RoomError(message) from exc
        if digest(value["artifact"]) != value["sha256"]:
            raise RoomError(message)
        retained = _read(directory, value["record"], message)
        if retained != value["artifact"] or digest(retained) != value["sha256"]:
            raise RoomError(message)
        return {**copy.deepcopy(value), "retained_evidence": False}
    pinned = verify_reference(directory, {"sha256": value["sha256"], "record": value["record"],
                                          "evidence": value["evidence"]}, message)
    if pinned["artifact"] != value["artifact"]:
        raise RoomError(message)
    return {**copy.deepcopy(value), "retained_evidence": True}


def family_entry(artifact, family):
    """The pinned-policy family entry a qualified artifact derives for one bundled family."""
    from ao_engineering_model import BUNDLED_FAMILIES, _v3_entry
    if not isinstance(family, str) or family not in BUNDLED_FAMILIES:
        raise RoomError("A family qualification applies only to an existing bundled family")
    declared = artifact["families"].get(family) if isinstance(artifact.get("families"), dict) else None
    if declared is None:
        raise RoomError("The pinned family qualification does not map family " + family)
    return _v3_entry(family, digest(artifact), declared)


ADMISSION_ORIGINS = ("current_epoch", "committed_target", "pending_engineering_target",
                     "pending_routing_snapshot", "current_routing_snapshot")
CONFIGURATION_FIELDS = frozenset(("config_dir", "worktree", "agents", "foreground", "environ", "local_settings",
                                  "project_env"))
EXECUTABLE_EVIDENCE_FIELDS = frozenset(("path", "size", "mtime_ns", "sha256", "version", "error"))
QUALIFICATION_SNAPSHOT_FIELDS = ({"artifact", "sha256"}, SNAPSHOT_FIELDS, SNAPSHOT_FIELDS_RETAINED)
# The default ``worker_directory`` of admit_qualification: inherit the root proof location exactly.
# An explicit None always means prospective proof from that worker snapshot's own selected private
# captures; an explicit room path always means its own retained room proof. Neither is a fallback.
WORKER_DIRECTORY_INHERIT = object()


def _qualification_parts(value):
    """One immutable qualification snapshot's artifact and digest, validated structurally."""
    if (not isinstance(value, dict) or set(value) not in QUALIFICATION_SNAPSHOT_FIELDS
            or not isinstance(value.get("sha256"), str) or not HEX.fullmatch(value["sha256"])):
        raise RoomError("A family qualification snapshot declares its artifact and digest")
    artifact, sha256 = value["artifact"], value["sha256"]
    validate_qualification(artifact, bundled=False)
    if digest(artifact) != sha256:
        raise RoomError("A family qualification snapshot does not match its digest")
    if "record" in value and value["record"] != record_path(sha256):
        raise RoomError("A family qualification snapshot declares a different retained record")
    if "evidence" in value and value["evidence"] != evidence_entries(artifact, sha256):
        raise RoomError("A family qualification snapshot declares inconsistent retained evidence")
    return artifact, sha256


def _executable_evidence(value):
    """Bounded concrete executable evidence; absence is allowed only when no effective floor is binding.

    Supplied evidence must be concrete identity, not a version string on its own: an absolute path, a
    nonnegative size and mtime, a version and no error. The lower helper compares the identity its
    authorized caller supplied; it never observes native execution or provider serving itself.
    """
    if value is None:
        return {key: None for key in EXECUTABLE_EVIDENCE_FIELDS}
    if not isinstance(value, dict) or set(value) - EXECUTABLE_EVIDENCE_FIELDS:
        raise RoomError("Qualification admission requires bounded concrete Claude executable evidence")
    result = {}
    for key in EXECUTABLE_EVIDENCE_FIELDS:
        item = value.get(key)
        if key in ("path", "version", "error"):
            if item is not None and not isinstance(item, str):
                raise RoomError("Qualification admission executable " + key + " must be text or null")
        elif key in ("size", "mtime_ns"):
            if item is not None and type(item) is not int:
                raise RoomError("Qualification admission executable " + key + " must be an integer or null")
        elif item is not None and (not isinstance(item, str) or not HEX.fullmatch(item)):
            raise RoomError("Qualification admission executable sha256 must be a SHA256 or null")
        result[key] = item
    if not (result.get("error") is None and isinstance(result.get("path"), str)
            and result["path"].startswith("/") and "\x00" not in result["path"]
            and type(result.get("size")) is int and result["size"] >= 0
            and type(result.get("mtime_ns")) is int and result["mtime_ns"] >= 0
            and isinstance(result.get("version"), str) and result["version"].strip()):
        raise RoomError("Qualification admission requires concrete existing Claude executable identity from its "
                        "caller, an absolute path with nonnegative size and mtime, a version and no error; a "
                        "version-only or partial identity is refused and native execution is not observed here")
    return result


def _worker_roles(value):
    """Validate an optional role to family mapping used to accept a source-qualified worker map."""
    if value is None:
        return None
    if (not isinstance(value, dict) or not value
            or any(not isinstance(role, str) or not role or not isinstance(family, str) or not family
                   for role, family in value.items())):
        raise RoomError("Worker families are a nonempty mapping of role names to family names")
    return dict(value)


def _derived_worker_map(qualification, worker_roles):
    """The exact role map the supplied qualification derives for the declared worker families.

    A source-qualified map is accepted only by family equality to the exact supplied qualification; a
    syntactically valid Claude identifier is never accepted on its own.
    """
    if qualification is None:
        raise RoomError("A source-qualified worker map is accepted only with the exact qualification it derives from")
    artifact = qualification.get("artifact") if isinstance(qualification, dict) else None
    artifact = artifact if isinstance(artifact, dict) else qualification
    if not isinstance(artifact, dict) or not isinstance(worker_roles, dict) or not worker_roles:
        raise RoomError("A source-qualified worker map requires the supplied qualification artifact and its worker "
                        "families")
    declared_families = artifact.get("families")
    derived = {}
    for role, family in worker_roles.items():
        if not isinstance(role, str) or not role or not isinstance(family, str) or not family:
            raise RoomError("Worker families are nonempty role to family names")
        declared = declared_families.get(family) if isinstance(declared_families, dict) else None
        if declared is None:
            raise RoomError("The supplied qualification does not map worker family " + family)
        derived[role] = declared["expected_model"]
    return derived


def _origin_evidence(value):
    """A caller-supplied bounded context label, never a reusable authorization token."""
    if not isinstance(value, dict) or set(value) - {"context", "request_id", "digest"} or "context" not in value:
        raise RoomError("Qualification admission origin declares exactly context, request_id and digest")
    context = value["context"]
    if not isinstance(context, str) or context not in ADMISSION_ORIGINS:
        raise RoomError("Qualification admission origin context is not supported")
    request_id, digest_value = value.get("request_id"), value.get("digest")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise RoomError("Qualification admission origin request_id must be a nonempty identifier")
    if digest_value is not None and (not isinstance(digest_value, str) or not HEX.fullmatch(digest_value)):
        raise RoomError("Qualification admission origin digest must be a SHA256 or null")
    return {"context": context, "request_id": request_id, "digest": digest_value,
            "authenticated_by": "caller-supplied context only; not verified here",
            "reusable_authorization_token": False}


def _qualification_basis(qualification, worker_qualification):
    """The source-qualified basis for alias and gateway sensitivity, or None for historical work."""
    return qualification if qualification is not None else worker_qualification


def _configuration_evidence(value, *, qualification=None, worker_roles=None, worker_qualification=None):
    """Concrete bounded configuration evidence, checked read-only; no secrets or full values returned.

    ``qualification`` is the exact artifact being admitted and ``worker_qualification`` a routing
    record's own authenticated versioned worker metadata when the supplied map came from it; that
    independently pinned value is the worker authority, so the accepted worker map derives from its
    artifact and never from an unrelated root artifact. ``worker_roles`` is the enabled role map when
    the supplied artifact itself is the worker authority. A source-qualified map is accepted only
    while it equals the exact map one of those derives; a syntactically valid Claude identifier is
    never accepted on its own. Absent configuration is reported as not checked, and an explicitly
    supplied empty environment never falls back to the ambient process environment. Environment
    evidence is the supported read-only process or project mapping, so ``os.environ`` is accepted
    where a JSON settings object keeps its own strict object check.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - CONFIGURATION_FIELDS:
        raise RoomError("Qualification admission configuration declares only config_dir, worktree, agents, "
                        "foreground, environ, local_settings and project_env")
    import ao_routing
    checked, not_checked = [], []
    config_dir, worktree = value.get("config_dir"), value.get("worktree")
    if config_dir is not None and (not isinstance(config_dir, str) or not config_dir.strip()):
        raise RoomError("Qualification admission config_dir must be a nonempty path")
    if worktree is not None and (not isinstance(worktree, str) or not worktree.strip()):
        raise RoomError("Qualification admission worktree must be a nonempty path")
    foreground = value.get("foreground", False)
    if type(foreground) is not bool:
        raise RoomError("Qualification admission foreground must be an explicit boolean")
    basis = _qualification_basis(qualification, worker_qualification)
    agents = value.get("agents")
    if agents is not None:
        if not isinstance(agents, dict):
            raise RoomError("Qualification admission worker map must be a JSON object of role to model")
        accepted = [ao_routing.FAMILY_AGENTS, ao_routing.EXACT_AGENTS]
        if worker_qualification is not None:
            # The independently pinned snapshot is the worker authority, never an unrelated root artifact.
            accepted.append(ao_routing.qualified_worker_map(worker_qualification))
        elif qualification is not None and worker_roles:
            accepted.append(_derived_worker_map(qualification, worker_roles))
        if agents not in accepted:
            raise RoomError("Qualification admission worker map is neither the historical exact/alias map nor the "
                            "exact map the supplied source qualification derives for its worker families")
        checked.append("supplied native worker selector map")
    else:
        not_checked.append("native worker selector map (not supplied)")
    environ = value.get("environ")
    if environ is not None and (not isinstance(environ, Mapping)
                               or any(not isinstance(key, str) for key in environ)):
        raise RoomError("Qualification admission environment evidence must be a mapping of text keys")
    local_settings = value.get("local_settings")
    if local_settings is not None:
        if not isinstance(local_settings, dict):
            raise RoomError("Qualification admission local_settings must be a JSON object document")
        ao_routing.check_settings_document(local_settings, agents=agents, foreground=foreground, qualification=basis)
        checked.append("explicitly supplied planned local settings document")
    project_env = value.get("project_env")
    if project_env is not None:
        if not isinstance(project_env, Mapping) or any(not isinstance(key, str) for key in project_env):
            raise RoomError("Qualification admission AO project environment evidence must be a mapping of text keys")
        bad = ao_routing._contradictory_env(project_env)
        if bad:
            raise RoomError("Qualification admission AO project environment overrides native routing knobs: "
                            + ", ".join(bad))
        if ao_routing._alias_sensitive(agents, basis):
            ao_routing._alias_env(project_env, "AO project environment")
        if basis is not None:
            ao_routing._gateway_env(project_env, "AO project environment")
        checked.append("explicitly supplied AO project environment contradictions")
    else:
        not_checked.append("AO project and controller environment (not supplied)")
    if config_dir is not None or worktree is not None or environ is not None:
        try:
            ao_routing.contradictions(config_dir, worktree, environ if environ is not None else {},
                                      foreground=foreground, agents=agents, qualification=basis)
        except RoomError as exc:
            raise RoomError("Qualification admission configuration contradicts native routing: " + str(exc)) from exc
        if config_dir is not None:
            checked.append("user Claude settings source")
        else:
            not_checked.append("user Claude settings source (no config_dir supplied)")
        checked.append("managed Claude settings source")
        if worktree is not None:
            checked.append("project Claude settings source")
            local = ao_routing.check_settings_file(Path(worktree) / ".claude" / "settings.local.json",
                                                   agents=agents, foreground=foreground, qualification=basis)
            if local is not None:
                checked.append("worktree local Claude settings file")
            else:
                not_checked.append("worktree local Claude settings file (absent)")
        else:
            not_checked.append("project Claude settings source (no worktree supplied)")
        if environ is not None:
            checked.append("explicitly supplied process environment contradictions (no ambient fallback)")
        else:
            not_checked.append("process environment contradictions (no environ supplied; the ambient process "
                               "environment was not consulted)")
    else:
        not_checked.extend(["user and managed Claude settings sources (no configuration context supplied)",
                            "project and local Claude settings sources (no worktree supplied)",
                            "process environment contradictions (no environ supplied)"])
    not_checked.extend(["local pinned routing files (validated by the caller-level routing boundary, never here)",
                        "the native process's complete inherited environment",
                        "the executing native Agent guard's inherited environment (checked only by the guard itself)",
                        "provider availability, account entitlement or quota", "effective provider effort",
                        "native enforcement of the configured settings"])
    return {"config_dir": config_dir, "worktree": worktree,
            "agents": copy.deepcopy(agents) if agents is not None else None,
            "foreground": foreground,
            "worker_roles": copy.deepcopy(worker_roles) if worker_roles else None,
            "checked": checked, "not_checked": not_checked}


def admit_qualification(qualification, family, *, directory=None, executable, configuration, origin,
                        worker_roles=None, worker_qualification=None,
                        worker_directory=WORKER_DIRECTORY_INHERIT):
    """Lower explicit admission boundary for one immutable qualification snapshot, no root state.

    The snapshot is accepted as the existing {artifact, sha256} value or a retained room snapshot.
    With ``directory`` the exact retained artifact and every selected retained source capture are
    verified; without it the selected private source bytes and artifact digest are verified
    read-only and the result is labelled prospective/private proof. A missing or changed retained
    capture never falls back to today's private source. Family, expected model and recorded floor
    come from this explicit artifact. ``worker_roles`` names the enabled worker roles this artifact
    itself is the worker authority for, e.g. a fresh preparation qualifying its workers from the
    current artifact. An independently pinned ``worker_qualification`` (the routing record's own
    authenticated versioned value) instead makes that snapshot's artifact the worker authority: the
    worker map and the worker compatibility floors are derived from it, never from this artifact, so
    a root artifact that maps only its own family still admits that root while the workers keep the
    newer snapshot's exact models and floors; that independent snapshot's own retained artifact record
    and every one of its selected captures are authenticated separately, and a root proof never stands
    in for missing or changed worker bytes. ``worker_directory`` locates that independent worker proof
    alone and is consulted only when a ``worker_qualification`` is supplied: the default sentinel
    inherits ``directory`` exactly, so an omitted parameter keeps every existing caller's exact
    behavior and result shape; an explicit room path authenticates that independent snapshot's own
    retained room bytes; an explicit None deliberately verifies its own selected private captures
    prospectively for a caller that has no room, never as a fallback for a retained byte that failed
    to verify. The strongest current bundled floor is enforced against the supplied concrete
    executable evidence. No preparation, effective routing, routing.validate_local, convenience
    admission, committed journal or response record is consulted here. The result is bounded
    transaction evidence, not a reusable authorization token, and a later boundary caller must retain
    the snapshot before successful publication.
    """
    import ao_engineering_model as model
    artifact, sha256 = _qualification_parts(qualification)
    if not isinstance(family, str) or family not in model.BUNDLED_FAMILIES:
        raise RoomError("A family qualification applies only to an existing bundled family")
    declared = artifact["families"].get(family)
    if declared is None:
        raise RoomError("The supplied family qualification does not map family " + family)
    worker_roles = _worker_roles(worker_roles)
    worker_artifact, worker_families, worker_sources, worker_proof = None, {}, None, None
    if worker_qualification is not None:
        # The independently pinned worker snapshot is the worker authority: authenticate its own
        # versioned value and derive the map and floors from its own artifact only.
        import ao_routing
        derived = ao_routing.qualified_worker_map(worker_qualification)
        worker_families = {role: worker_qualification["families"][role]["family"] for role in derived}
        if worker_roles is not None and worker_roles != worker_families:
            raise RoomError("An independently pinned worker qualification must map exactly the enabled worker roles")
        worker_artifact = worker_qualification["snapshot"]["artifact"]
    else:
        worker_artifact, worker_families = artifact, dict(worker_roles or {})
        derived = _derived_worker_map(artifact, worker_roles) if worker_roles else {}
    if worker_directory is WORKER_DIRECTORY_INHERIT:
        worker_location = directory
    else:
        worker_location = worker_directory
        if worker_location is not None and not isinstance(worker_location, (str, os.PathLike)):
            raise RoomError("An independent worker proof directory is a room directory or an explicit None")
    if directory is not None:
        reference = {"sha256": sha256, "record": record_path(sha256),
                     "evidence": evidence_entries(artifact, sha256)}
        pinned = verify_reference(directory, reference,
                                  "The candidate family qualification retained evidence is missing or changed")
        artifact = pinned["artifact"]
        sources = pinned["sources"]
        proof = "retained"
    else:
        sources = verify_sources(artifact, bundled=False)
        proof = "private_prospective"
    if worker_qualification is not None:
        if worker_location is not None:
            # The independent worker snapshot keeps its own exact retained artifact record and every
            # one of its own selected captures, read from its own located room proof: missing,
            # changed or substituted worker bytes refuse here, and neither the root proof nor a
            # private original is ever used as a fallback.
            worker_snapshot = worker_qualification["snapshot"]
            worker_pinned = verify_reference(
                worker_location,
                {"sha256": worker_snapshot["sha256"], "record": worker_snapshot["record"],
                 "evidence": worker_snapshot["evidence"]},
                "The independent worker qualification retained evidence is missing or changed")
            worker_artifact = worker_pinned["artifact"]
            worker_sources = worker_pinned["sources"]
            worker_proof = "retained"
        else:
            # No located room bytes for this independent snapshot: its own selected private captures
            # and digest are authenticated directly, exactly as the root snapshot's are, and never
            # substituted. This is an explicit prospective mode, never a fallback from a retained
            # capture that failed to verify.
            worker_snapshot = worker_qualification["snapshot"]
            validate_qualification(worker_artifact, bundled=False)
            if digest(worker_artifact) != worker_snapshot["sha256"]:
                raise RoomError("The independent worker qualification artifact does not match its recorded digest")
            worker_sources = verify_sources(worker_artifact, bundled=False)
            worker_proof = "private_prospective"
    declared = artifact["families"].get(family)
    if declared is None:
        raise RoomError("The supplied family qualification does not map family " + family)
    expected_model = declared["expected_model"]
    recorded_floor = declared.get("minimum_claude_code_version")
    effective_floor = model.required_minimum(family, declared)
    concrete = _executable_evidence(executable)
    observed = None if concrete.get("error") else model.parse_version(concrete.get("version"))
    if effective_floor is not None:
        required = model.parse_version(effective_floor)
        if observed is None or observed < required:
            raise RoomError(f"{expected_model} requires Claude Code {effective_floor} or newer; the supplied "
                            "executable reports " + str(concrete.get("version") or concrete.get("error")
                                                        or "no version"))
    for role, role_family in sorted(worker_families.items()):
        role_entry = worker_artifact["families"].get(role_family)
        role_floor = model.required_minimum(role_family, role_entry) if role_entry is not None else None
        if role_floor is not None:
            required = model.parse_version(role_floor)
            if observed is None or observed < required:
                raise RoomError(role + " requires Claude Code " + role_floor + " or newer; the supplied "
                                "executable reports " + str(concrete.get("version") or concrete.get("error")
                                                            or "no version"))
    configuration_evidence = _configuration_evidence(configuration, qualification=artifact,
                                                     worker_roles=worker_families,
                                                     worker_qualification=worker_qualification)
    origin_evidence = _origin_evidence(origin)
    checked = ["retained family qualification bytes and source captures" if proof == "retained"
               else "selected private source bytes and artifact digest (prospective proof)"]
    checked.append("effective compatibility floor " + effective_floor if effective_floor is not None
                   else "no effective compatibility floor is declared")
    if worker_families:
        checked.append("worker family map derived by family equality from the supplied qualification"
                       if worker_qualification is None else
                       "worker family map derived by family equality from the independently pinned worker "
                       "qualification")
    checked.extend(configuration_evidence["checked"])
    return {"version": 1, "family": family, "selector": {"kind": "family", "family": family},
            "expected_model": expected_model,
            # The caller-level admission boundary reads these at the top level; the same values stay
            # nested under qualification for readers that recorded the original shape.
            "minimum_claude_code_version": recorded_floor,
            "effective_minimum_claude_code_version": effective_floor,
            "worker_families": copy.deepcopy(worker_families) if worker_families else None,
            "worker_expected_models": derived,
            # Separate independent-worker proof: its own retained artifact record and selected
            # captures (or its own selected private captures prospectively), never the root proof.
            "worker_proof": worker_proof,
            "worker_reference": (None if worker_qualification is None else {
                "sha256": worker_qualification["snapshot"]["sha256"],
                "record": worker_qualification["snapshot"]["record"],
                "evidence": copy.deepcopy(worker_qualification["snapshot"]["evidence"])}),
            "worker_sources": worker_sources,
            "qualification": {"sha256": sha256, "revision": artifact["revision"],
                              "qualified_at": artifact["qualified_at"], "record": record_path(sha256),
                              "expected_model": expected_model,
                              "minimum_claude_code_version": recorded_floor,
                              "effective_minimum_claude_code_version": effective_floor,
                              "source_ids": list(declared["source_ids"])},
            "sources": sources, "scope": dict(artifact["scope"]), "proof": proof,
            "executable": concrete,
            "executable_basis": ("caller-supplied concrete executable identity compared with the declared floor; the "
                                 "lower pure helper does not observe native execution or provider serving"),
            "configuration": configuration_evidence, "origin": origin_evidence,
            "checked": checked,
            "not_checked": configuration_evidence["not_checked"],
            "runtime_unobserved": list(RUNTIME_UNOBSERVED),
            "basis": ("Explicit immutable qualification snapshot, its selected source bytes and the caller-supplied "
                      "concrete Claude executable identity; not provider availability, account entitlement, native "
                      "execution or effective effort"),
            "transaction_evidence": True, "reusable_authorization_token": False,
            "meaning": ("Bounded transaction evidence for one qualification admission. The caller-supplied origin "
                        "is context, not an independently authenticated fact, and a later boundary caller must "
                        "retain the snapshot before successful publication.")}


def _supported_preparation(directory, state):
    """The room's supported preparation, or a bounded readiness refusal; never a guessed file."""
    try:
        from ao_delegates import preparation
        prepared = preparation(directory, state)
    except (RoomError, OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        raise RoomError("Qualified admission requires the room's supported preparation to check the qualified "
                        "compatibility floor and known configuration sources; prepare the engineer first") from exc
    if not isinstance(prepared, dict) or not prepared.get("worktree"):
        raise RoomError("Qualified admission requires the room's supported preparation to check the qualified "
                        "compatibility floor and known configuration sources; prepare the engineer first")
    return prepared


def admission(directory, state, family=None, *, prepared=None, target=None):
    """Bound admission evidence for one qualified family, or RoomError.

    The qualification is the exact current epoch's own pinned artifact, or the explicitly named
    committed transition's target artifact for an audit; today's private configuration is never
    consulted for a room's history. The room-retained artifact bytes and the retained source
    evidence bytes are verified, the compatible executable floor and the known supported nonsecret
    configuration sources are checked, and runtime facts that cannot be established here stay
    explicitly unobserved. A missing or unsupported preparation is a bounded readiness refusal:
    admission never claims a check that could not run. Never dispatches and never writes room state.
    """
    import ao_engineering_model as model
    context = model.qualification_context(directory, state, target)
    reference = context.get("reference")
    if reference is None:
        raise RoomError("This room's engineering epoch pins no operator-selected family qualification; a supported "
                        "audited qualification refresh is required before admission")
    if not reference.get("evidence"):
        raise RoomError("The pinned family qualification holds no retained source bytes; a supported audited "
                        "qualification refresh is required")
    pinned = verify_reference(directory, reference,
                              "The pinned family qualification evidence is missing or changed")
    artifact = pinned["artifact"]
    selector = context["selector"]
    if family is None:
        if selector.get("kind") != "family":
            raise RoomError("An exact engineering selector has no family qualification expectation; name a family")
        family = selector["family"]
        entry = context.get("qualification") or {}
        if not entry.get("execution_qualification"):
            raise RoomError("Family " + str(family) + " has no operator-selected qualification in this room's "
                            "pinned artifact; a supported audited qualification refresh is required")
        declared = artifact["families"].get(family)
        if declared is None or declared["expected_model"] != entry["execution_qualification"]["expected_model"]:
            raise RoomError("The pinned family qualification does not match this epoch's frozen expectation")
    else:
        entry = family_entry(artifact, family)
        declared = artifact["families"][family]
    if prepared is None:
        prepared = _supported_preparation(directory, state)
    import ao_routing
    # The worker snapshot and map are the effective routing in force, never this preparation's own
    # epoch zero: a committed qualified refresh replaces them, and the routing record in force (the
    # room's own retained target) is the worker authority validate_local just authenticated.
    routing = ao_routing.validate_local(prepared, state, directory)
    routing_input = routing or prepared.get("routing") or {}
    configuration = {"worktree": prepared.get("worktree")}
    if routing_input.get("claude_config_dir"):
        configuration["config_dir"] = routing_input["claude_config_dir"]
    configuration["agents"] = routing_input.get("agents")
    worker_qualification = routing_input.get("worker_qualification")
    lower = admit_qualification({"artifact": pinned["artifact"], "sha256": pinned["sha256"],
                                 "record": pinned["record"], "evidence": pinned["evidence"]},
                                family, directory=directory,
                                executable=model.executable_identity(directory, state, prepared),
                                configuration=configuration,
                                origin={"context": context.get("source") or "current_epoch",
                                        "request_id": context.get("request_id")},
                                worker_roles=ao_routing.WORKER_FAMILIES if worker_qualification is not None else None,
                                worker_qualification=worker_qualification)
    executable = lower["executable"]
    if (lower.get("effective_minimum_claude_code_version")
            != lower.get("qualification", {}).get("effective_minimum_claude_code_version")):
        raise RoomError("Qualification admission returned an inconsistent compatibility floor; preserve and diagnose")
    effective_floor = lower["effective_minimum_claude_code_version"]
    checked = ["retained family qualification bytes", "retained source evidence bytes",
               "qualified compatibility floor" if effective_floor else "no qualified compatibility floor is declared"]
    not_checked = ["the AO project and controller environment (observed by the later dispatch wiring)",
                   "the native process's complete inherited environment"]
    if routing is not None:
        checked.append("pinned routing files and Claude executable identity")
        checked.append("user, managed, project and local Claude settings sources")
        if ao_routing.foreground_configured(prepared.get("worktree"), routing):
            checked.append("process environment contradictions for foreground delegation")
        else:
            not_checked.append("process environment (foreground delegation is not configured here)")
    return {"version": 1, "family": family, "selector": copy.deepcopy(selector),
            "source": context.get("source"), "source_request_id": context.get("request_id"),
            "expected_model": declared["expected_model"],
            "effective_minimum_claude_code_version": effective_floor,
            "qualification": {"sha256": pinned["sha256"], "revision": artifact["revision"],
                              "qualified_at": artifact["qualified_at"], "record": pinned["record"],
                              "expected_model": declared["expected_model"],
                              "minimum_claude_code_version": declared.get("minimum_claude_code_version"),
                              "source_ids": list(declared["source_ids"])},
            "sources": pinned["sources"], "scope": dict(artifact["scope"]),
            "operator_qualified_source_applicability": "the declared source scope, exact expected model and "
                                                       "compatibility floor; not availability, entitlement or "
                                                       "effective effort",
            "executable": executable, "checked": checked, "not_checked": not_checked,
            "runtime_unobserved": list(RUNTIME_UNOBSERVED),
            "basis": "Private operator-selected source evidence and its retained room capture verified "
                     "read-only; not provider availability, account entitlement, native enforcement or effective "
                     "effort"}
