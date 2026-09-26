"""Immutable quality-review instruction and bounded, read-only delivery proof.

Only controller-carried text in a verified native receipt establishes a root.
Equivalence metadata names that root directly; it never creates another root.
This module does not dispatch, inspect transcripts, repair files or release holds.
"""

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import re
import stat

from ao_prompt_metrics import digest, _observed_delivery, receipt_projection_sha256
from room import RoomError

VERSION = 1
PART = "quality_first_review_v1"
INSTRUCTION_SHA256 = "6d043563ae4c27854dbaad53b9762476efa189c9fb6565dfb5670e24cee47a4e"
INSTRUCTION = (
    "For supporting review work, use DeepSeek or another suitable delegate to prepare source-grounded facts, draft changes and evidence summaries. "
    "Have the assigned operator establish exact JSON paths, unique edit anchors, old values, hashes, schemas and test receipts with deterministic checks before Fable reviews the substantive changes. "
    "Fable may rely on verified mechanical results without reproducing each mechanical check, but must inspect the actual changes and original evidence needed to assess meaning, correctness, conflicts and omissions. "
    "Summaries and passing tests alone never establish approval; stale, incomplete or conflicting evidence requires investigation. "
    "Preserve approval of unchanged content only when its bytes, requirements and dependencies still match; review new semantic effects and interactions. "
    "For narrow corrections request only the changed items and preserve the unchanged complete result locally after verification. "
    "Fable retains MAX, specification review, pushback, enhancement judgment, routing/escalation discretion and the final engineering verdict. "
    "Necessary deeper review always takes priority over savings. "
    "Validate this allocation on the next bounded milestone using the same correctness and acceptance requirements; shorter prompts or fewer tokens alone are not success. "
    "This operating change grants no new scope, execution permission, recovery or review allowance."
)
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REQUESTS = 4096
MAX_AMENDMENTS = 256
MAX_DEPTH = 64
EQUIVALENCE = "quality_first_review_equivalence"
_HEX = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}")
_OWNER = ("session_id", "harness", "model", "reasoning_effort", "conversation_id", "branch_id")


class EvidenceError(RoomError):
    def __init__(self, reason="quality_evidence_integrity"):
        self.reason = reason
        super().__init__(reason + ": operating evidence changed or cannot be verified (modified or unavailable); preserve the evidence before continuing")


def _require(value, reason="quality_evidence_integrity"):
    if not value:
        raise EvidenceError(reason)


def _hash(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_number(value):
    raise EvidenceError()


def _json(raw):
    """Unique keys, finite numbers, scalar Unicode and a closed container depth."""
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_number)
    except RecursionError as exc:
        raise EvidenceError("quality_evidence_limit") from exc
    except (UnicodeError, ValueError) as exc:
        raise EvidenceError() from exc
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            depth += 1
            _require(depth <= MAX_DEPTH, "quality_evidence_limit")
            stack.extend((v, depth) for v in (item.values() if isinstance(item, dict) else item))
            if isinstance(item, dict):
                stack.extend((key, depth) for key in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise EvidenceError() from exc
        elif isinstance(item, float):
            _require(math.isfinite(item))
    return value


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Reader:
    """Resolve only closed owned paths, retaining and rechecking every directory."""
    def __init__(self, directory):
        self.root = Path(directory)
        self.total = 0
        _require(self.root.is_absolute() and not any(x in (".", "..") for x in self.root.parts))

    @contextmanager
    def parent(self, relative):
        components = relative.split("/")
        _require(all(_identifier(x) and x not in (".", "..") for x in components))
        opened, links = [], []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            current = os.open(self.root.anchor, flags)
            opened.append(current)
            parts = list(self.root.parts[1:]) + components[:-1]
            for index, name in enumerate(parts):
                child = os.open(name, flags, dir_fd=current)
                opened.append(child)
                info = os.fstat(child)
                _require(stat.S_ISDIR(info.st_mode))
                if index >= len(self.root.parts) - 2:
                    _require(info.st_uid == os.getuid())
                links.append((current, name, child, info.st_dev, info.st_ino, info.st_uid))
                current = child
            yield current, components[-1]
            for parent, name, child, dev, ino, uid in links:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                held = os.fstat(child)
                _require(stat.S_ISDIR(named.st_mode) and
                         (named.st_dev, named.st_ino, named.st_uid) == (dev, ino, uid) ==
                         (held.st_dev, held.st_ino, held.st_uid))
        except OSError as exc:
            reason = "quality_evidence_unavailable" if exc.errno in (errno.ENOENT, errno.EACCES) else "quality_evidence_integrity"
            raise EvidenceError(reason) from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def read(self, relative):
        with self.parent(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                handle = os.fdopen(fd, "rb", buffering=0)
            except OSError:
                os.close(fd)
                raise
            with handle as stream:
                before = os.fstat(stream.fileno())
                _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1)
                _require(before.st_size <= MAX_FILE_BYTES and self.total + before.st_size <= MAX_TOTAL_BYTES,
                         "quality_evidence_limit")
                # FileIO prevents buffered read-ahead beyond the budget. The
                # original admitted extent also bounds concurrent file growth;
                # charge every actual byte, including an eventual refused read.
                remaining, chunks = before.st_size, []
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    self.total += len(block)
                    remaining -= len(block)
                    chunks.append(block)
                raw = b"".join(chunks)
                after = os.fstat(stream.fileno())
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                _require(len(raw) == before.st_size and _identity(before) == _identity(after) == _identity(named))
        value = _json(raw)
        _require(isinstance(value, dict))
        return value


def _settlement(reader, request):
    """The existing settlement contract, read through the bounded owned reader."""
    from ao_outcomes import native_quota_failure
    name = request["request_id"]
    sha = request.get("outcome_resume_sha256")
    _require(_hash(sha))
    prefix = "outcome-resumes/" + name
    path = request.get("outcome_resume")
    _require(path in (prefix + ".json", prefix + "/" + sha + ".json"))
    release = reader.read(path)
    outcome_sha = release.get("outcome_sha256")
    _require(_hash(outcome_sha))
    record = reader.read("outcomes/" + name + "/" + outcome_sha + ".json")
    _require(digest(release) == sha and type(release.get("version")) is int and release["version"] == VERSION
             and release.get("room_id") == reader.root.name
             and release.get("blocked_request_id") == name and digest(record) == outcome_sha
             and type(record.get("version")) is int and record["version"] == VERSION
             and record.get("room_id") == reader.root.name and record.get("request_id") == name
             and all(record.get(k) == request.get(k) for k in ("text_sha256", "turn_id", "provider_turn_id"))
             and native_quota_failure(record) and record.get("receipt_sha256") == request["receipt_sha256"]
             and release.get("native_failure_settlement") == {
                 "prior_state": "uncertain", "ao_state": "failed", "receipt_sha256": request["receipt_sha256"]}
             and (request.get("observed_turn") or {}).get("state") == "failed")


def _history_record(reader, area, request, sha):
    _require(_hash(sha))
    value = reader.read(area + "/" + request["request_id"] + "/" + sha + ".json")
    _require(digest(value) == sha and type(value.get("version")) is int and value["version"] == VERSION
             and value.get("room_id") == reader.root.name and value.get("request_id") == request["request_id"])
    return value


def _reconciled_history(reader, state, request, receipt, latest):
    """Verify saved audit authority locally; never observe history or release a hold.

    The existing reconciliation lane audits complete current history and native
    quota evidence without rewriting the truncated receipt. Its ordinary send
    gates still revalidate live inputs and require the one named release. Here
    only the immutable local proof, outcome and invalidation chain are read.
    """
    from ao_history_reconciliation import PROOF, INVALIDATION
    from ao_outcomes import native_quota_failure
    sha = request.get(PROOF)
    proof = _history_record(reader, "history-reconciliations", request, sha)
    _require(request.get("state") == "completed" and receipt.get("history_truncated") is True
             and proof.get("kind") == "complete_history_native_quota" and proof.get("saved_history_truncated") is True
             and proof.get("receipt_sha256") == request["receipt_sha256"]
             and proof.get("text_sha256") == request["text_sha256"])
    head = request.get(INVALIDATION)
    cursor, seen = head, set()
    while cursor is not None:
        _require(cursor not in seen)
        _require(len(seen) < 1000, "quality_evidence_limit")  # Existing reconciliation-chain ceiling.
        seen.add(cursor)
        row = _history_record(reader, "history-reconciliation-invalidations", request, cursor)
        _require(row.get("kind") == "invalidation" and _hash(row.get("proof_sha256")))
        cursor = row.get("previous_sha256")
    _require(proof.get("invalidation_sha256") == head)

    outcome_sha = request.get("semantic_outcome_sha256")
    _require(_hash(outcome_sha) and request.get("semantic_outcome") ==
             "outcomes/" + request["request_id"] + "/" + outcome_sha + ".json")
    outcome = _history_record(reader, "outcomes", request, outcome_sha)
    identity = {"receipt_sha256": request["receipt_sha256"], "text_sha256": request["text_sha256"],
                "turn_id": request["turn_id"], "provider_turn_id": receipt["turn"]["providerTurnId"]}
    _require(all(outcome.get(key) == value for key, value in identity.items())
             and outcome.get(PROOF) == sha and outcome.get("outcome") == request.get("semantic_status")
             and native_quota_failure(outcome))
    inputs, native = proof.get("inputs"), outcome.get("native")
    _require(isinstance(inputs, dict) and isinstance(native, dict) and inputs.get("native") == native
             and _hash(native.get("source_sha256"))
             and isinstance(native.get("anchor_uuid"), str) and native["anchor_uuid"].strip())
    source = native.get("source")
    _require(isinstance(source, dict) and set(source) == {"database", "transcript", "session_id", "native_session_id"}
             and all(isinstance(value, str) and value.strip() and "\x00" not in value for value in source.values())
             and source["session_id"] == request["session_id"] and request["harness"] == "claude-code"
             and Path(source["transcript"]).is_absolute()
             and Path(source["transcript"]).name == source["native_session_id"] + ".jsonl")
    # A later explicit source audit belongs to the latest request. Historical
    # delivery keeps its own immutable proof/outcome source and identity anchors;
    # it must not be rebound to today's transcript path. No native file is read.
    _require(not latest or source == state.get("native_outcome_source"))
    terminal = outcome.get("ao_terminal")
    _require(isinstance(terminal, dict) and terminal.get("state") == "completed"
             and terminal.get("id") == request["turn_id"] and terminal.get("providerTurnId") == identity["provider_turn_id"])
    anchors = {key: request[key] for key in ("session_id", "role", "turn_id", "conversation_id", "branch_id")}
    anchors.update(provider_turn_id=identity["provider_turn_id"], baseline_sha256=digest(request["baseline"]),
                   messages_sha256=digest(receipt["messages"]), ao_terminal_sha256=digest(outcome.get("ao_terminal")),
                   provider_failures_sha256=digest(outcome.get("provider_failures")),
                   session_failures_sha256=digest(outcome.get("session_failures")))
    _require(all(inputs.get(key) == value for key, value in anchors.items()) and _hash(inputs.get("turns_sha256")))


def _receipt_integrity(reader, request, binding):
    """Immutable owned input/carried evidence, independent of history availability."""
    _require(all(isinstance(request.get(k), str) and request[k] and request[k] == binding.get(k) for k in _OWNER))
    name, pointer = request["request_id"], request.get("receipt")
    prefix = "receipts/" + name + "/"
    _require(isinstance(pointer, str) and pointer.startswith(prefix) and pointer.endswith(".json"))
    payload_sha = pointer[len(prefix):-5]
    _require(_hash(payload_sha) and _hash(request.get("receipt_sha256")))
    receipt = reader.read(pointer)
    _require(digest(receipt) == request["receipt_sha256"] and
             digest({k: v for k, v in receipt.items() if k != "observed_at"}) == payload_sha)
    _require(isinstance(request.get("text"), str) and digest(request["text"].encode()) == request.get("text_sha256"))
    _require(_observed_delivery(request, receipt))
    truncated = receipt.get("history_truncated")
    _require(truncated is None or type(truncated) is bool)
    if "prompt_projection" in request:
        _require(receipt.get("prompt_projection_sha256") == receipt_projection_sha256(request))
    else:
        _require("prompt_projection_sha256" not in receipt)  # Genuine pre-projection receipts remain valid.
    acknowledgement = request.get("acknowledgement")
    _require(acknowledgement is None or isinstance(acknowledgement, dict) and
             acknowledgement.get("turnId") == request["turn_id"])
    expected = "failed" if request["state"] == "settled_failure" else "completed"
    _require(receipt["turn"].get("state") == expected)
    observed = request.get("observed_turn")
    _require(observed is None or isinstance(observed, dict) and observed.get("state") == expected)
    if request["state"] == "settled_failure":
        _settlement(reader, request)
    carried = request.get("carried")
    if "carried" in request:
        _require(isinstance(carried, dict) and receipt.get("carried_sha256") == digest(carried))
    else:
        _require("carried_sha256" not in receipt)
    return receipt


def _receipt(reader, request, binding, state, latest):
    receipt = _receipt_integrity(reader, request, binding)
    if receipt.get("history_truncated"):
        _reconciled_history(reader, state, request, receipt, latest)
    return receipt


def _contains(text, instruction):
    # A carried identity plus an exact full fragment is required. Caller text by
    # itself is never examined for equivalence, nor are substrings or paraphrases.
    return ("\n" + instruction + "\n") in ("\n" + text + "\n")


def _source(request, kind, amendment=None):
    return {"kind": kind, "request_sha256": digest(request["request_id"].encode()),
            "receipt_sha256": request["receipt_sha256"], "amendment_sha256": amendment}


def _names(value, amendments):
    _require(isinstance(value, list))
    _require(len(value) <= MAX_AMENDMENTS, "quality_evidence_limit")
    _require(all(_hash(x) and x in amendments for x in value) and len(set(value)) == len(value))
    return value


def _amendment_receipts(ordered, amendments):
    """Only carried amendments/equivalence and their named direct roots matter here."""
    needed = set()
    for request in ordered:
        carried = request.get("carried")
        if carried is None:
            continue
        _require(isinstance(carried, dict))
        names = _names(carried.get("instruction_amendments", []), amendments)
        if names or EQUIVALENCE in carried:
            needed.add(digest(request["request_id"].encode()))
        if EQUIVALENCE in carried:
            value = carried[EQUIVALENCE]
            _require(isinstance(value, dict) and isinstance(value.get("source"), dict)
                     and _hash(value["source"].get("request_sha256")))
            needed.add(value["source"]["request_sha256"])
    return needed


def _inspect(reader, state, session_id, *, amendment_integrity=False):
    # Verify the bytes parsed by the ordinary controller without changing its
    # legacy parser or making an unbounded second read of any binding file.
    saved = reader.read("state.json")
    _require(digest(saved) == digest(state) and state.get("room_id") == reader.root.name)
    _require(state.get("workflow") == "fable_engineering")
    binding = state.get("bindings", {}).get("engineer")
    _require(isinstance(binding, dict) and binding.get("session_id") == session_id)
    requests = state.get("requests")
    pointers = state.get("instruction_amendments", [])
    _require(isinstance(requests, dict) and isinstance(pointers, list))
    _require(len(requests) <= MAX_REQUESTS and len(pointers) <= MAX_AMENDMENTS, "quality_evidence_limit")
    amendments, paths = {}, set()
    for pointer in pointers:
        _require(isinstance(pointer, dict) and set(pointer) == {"path", "sha256"} and _hash(pointer.get("sha256")))
        path = pointer["path"]
        _require(isinstance(path, str) and path.startswith("instruction-amendments/") and path.endswith(".json"))
        name = path[len("instruction-amendments/"):-5]
        _require(_identifier(name) and path not in paths and pointer["sha256"] not in amendments)
        value = reader.read(path)
        _require(set(value) == {"version", "room_id", "session_id", "request_id", "message", "authorization"}
                 and type(value["version"]) is int and value["version"] == VERSION
                 and value["room_id"] == state["room_id"] and value["session_id"] == session_id
                 and value["request_id"] == name and digest(value) == pointer["sha256"]
                 and isinstance(value["message"], str) and value["message"].strip()
                 and isinstance(value["authorization"], str) and value["authorization"].strip())
        amendments[pointer["sha256"]] = value["message"]
        paths.add(path)
    ordered, orders, latest = [], set(), None
    for key, request in requests.items():
        _require(_identifier(key) and isinstance(request, dict) and request.get("request_id") == key)
        order = request.get("created_order")
        _require(type(order) is int and order > 0 and order not in orders)
        orders.add(order)
        _require(request.get("role") in ("engineer", "reviewer"))
        if request["role"] == "engineer":
            _require(request.get("session_id") == session_id)
            if latest is None or order > latest["created_order"]:
                latest = request
            if request.get("state") in ("completed", "settled_failure"):
                ordered.append(request)
    roots, root, delivered, native_turns, turns = {}, None, set(), set(), set()
    needed = _amendment_receipts(ordered, amendments) if amendment_integrity else None
    for request in sorted(ordered, key=lambda r: r["created_order"]):
        if needed is not None and digest(request["request_id"].encode()) not in needed:
            continue
        receipt = (_receipt_integrity(reader, request, binding) if amendment_integrity else
                   _receipt(reader, request, binding, state, request is latest))
        native = receipt["turn"]["providerTurnId"]
        turn = receipt["turn"]["id"]
        _require(native not in native_turns and turn not in turns)
        native_turns.add(native); turns.add(turn)
        # Keep the canonical historical-packet verifier in ao_workflow.delivered.
        # Pre-carried packets cannot establish this new instruction or amendments.
        carried = request.get("carried")
        if carried is None:
            continue
        from ao_workflow import carried_by
        _, parts = carried_by(request)
        names = _names(carried.get("instruction_amendments", []), amendments)
        for sha in names:
            _require(_contains(request["text"], amendments[sha]))
        equivalent = [sha for sha in names if amendments[sha] == INSTRUCTION]
        if PART in parts:
            _require(carried["part_sha256"][PART] == INSTRUCTION_SHA256 and _contains(request["text"], INSTRUCTION))
            source = _source(request, "workflow_part")
            roots[digest(source)] = source
            root = root or source
        for sha in sorted(equivalent):
            source = _source(request, "instruction_amendment", sha)
            roots[digest(source)] = source
            root = root or source
        if EQUIVALENCE in carried:
            value = carried[EQUIVALENCE]
            _require(isinstance(value, dict) and set(value) == {
                "version", "part", "instruction_sha256", "source", "amendment_sha256"})
            _require(type(value["version"]) is int and value["version"] == VERSION
                     and value["part"] == PART and value["instruction_sha256"] == INSTRUCTION_SHA256)
            source = value["source"]
            _require(isinstance(source, dict) and digest(source) in roots and roots[digest(source)] == source
                     and source["request_sha256"] != digest(request["request_id"].encode()))
            satisfied = _names(value["amendment_sha256"], amendments)
            _require(satisfied == sorted(satisfied) and satisfied and PART not in parts
                     and not set(satisfied).intersection(names) and all(amendments[s] == INSTRUCTION for s in satisfied))
            delivered.update(satisfied)
        delivered.update(names)
    pending = [(message, sha) for sha, message in amendments.items() if sha not in delivered]
    satisfy = sorted(sha for message, sha in pending if root is not None and message == INSTRUCTION)
    provenance = ({"version": VERSION, "part": PART, "instruction_sha256": INSTRUCTION_SHA256,
                   "source": root, "amendment_sha256": satisfy} if satisfy else None)
    return {"source": root, "pending": pending, "equivalence": provenance,
            "equivalent_amendment_sha256": sorted(sha for sha in delivered if amendments[sha] == INSTRUCTION)}


def inspect(directory, state, session_id=None):
    """Fail closed before an intent is saved; no filesystem or controller writes."""
    try:
        session_id = session_id or state["bindings"]["engineer"]["session_id"]
        return _inspect(_Reader(directory), state, session_id)
    except EvidenceError:
        raise
    except (RoomError, OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
        raise EvidenceError() from exc


def pending_amendments(directory, state, session_id):
    """Validate amendment callers against only the state this proof consumes.

    Review-extension admission also validates amendments after an existing gate
    has observed the delegate ledger in memory. That observation is not a proof
    input. Bind every consumed state field to strict saved bytes, then verify the
    saved amendment evidence and any directly named equivalence roots. Unrelated
    receipts remain the full inspector's concern. This gate must not require the current
    reconciliation proof that an explicit outcome audit is about to create.
    It returns pending amendments only, never a delivery-availability verdict.
    The send/status inspector retains whole-state equality and full proof.
    """
    try:
        reader = _Reader(directory)
        saved = reader.read("state.json")
        fields = ("room_id", "workflow", "bindings", "requests", "instruction_amendments", "native_outcome_source")
        _require(digest({key: state[key] for key in fields if key in state}) ==
                 digest({key: saved[key] for key in fields if key in saved}))
        return _inspect(reader, saved, session_id, amendment_integrity=True)["pending"]
    except EvidenceError:
        raise
    except (RoomError, OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as exc:
        raise EvidenceError() from exc


def summary(directory, state):
    result = {"version": VERSION, "part": PART, "instruction_sha256": INSTRUCTION_SHA256,
              "delivery": "undelivered", "source": None, "equivalent_amendment_sha256": [], "reasons": []}
    try:
        proof = inspect(directory, state)
        result["source"] = proof["source"]
        result["equivalent_amendment_sha256"] = proof["equivalent_amendment_sha256"]
        if proof["source"] is not None:
            result["delivery"] = ("verified_delivered" if proof["source"]["kind"] == "workflow_part"
                                  else "verified_equivalent")
    except EvidenceError as exc:
        result.update(delivery="unavailable_integrity", reasons=[exc.reason])
    return result
