"""Exact outgoing prompt projection and honest compact status coverage for #56.

The controller records one closed version-1 projection while the real packet assembly runs
(tagged fragments, never a rebuilt measurement prompt), saves it in the durable request
intent before dispatch and binds it into new completed receipts. Room status revalidates
the saved bytes and reads at most one bounded owned receipt. Nothing here assembles a
prompt, calls a model, syncs, scans a transcript, repairs state or estimates usage/billing.
"""

import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import stat

from room import RoomError

VERSION = 1
MAX_RECEIPT_BYTES = 8_388_608
MAX_JSON_DEPTH = 64
ROLES = ("engineer", "reviewer")
KINDS = ("workflow", "specification", "caller", "separator")
SEPARATORS = ("", "\n")
DELIVERIES = ("none", "full", "changes")
# Kept aligned with ao_project_room.TERMINAL by an independent regression.
TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted", "settled_failure")
PROJECTION_FIELDS = ("version", "text_sha256", "total_bytes", "caller_bytes", "specification_bytes",
                     "workflow_bytes", "separator_bytes", "spec_delivery")
BYTE_FIELDS = ("total_bytes", "caller_bytes", "specification_bytes", "workflow_bytes", "separator_bytes")
REASONS = frozenset({"no_saved_request", "request_integrity", "projection_integrity",
                     "unsupported_projection_version", "legacy_components_absent",
                     "delivery_unobserved", "receipt_unavailable"})
NATIVE_WORKER_USAGE = {"coverage": "unavailable", "reason": "native_worker_usage_not_attributed"}
_HEX = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}")


def digest(value):
    """Byte-identical to ao_project_room.digest: sorted-key compact JSON, ensure_ascii=False, UTF-8 SHA256."""
    data = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


class PromptAssembly:
    """Tagged fragments of the real outgoing assembly, in dispatch order.

    ``join`` is the exact separator inserted between fragments: "\\n" for the normal
    engineer LF join, "" where the fragments carry their own newlines. Explicit
    "separator" fragments are only legal for a "" join. An existing CR/LF inside a
    fragment belongs to that fragment and is never counted twice.
    """

    def __init__(self, join=""):
        if join not in SEPARATORS:
            raise RoomError("Prompt fragment separator must be empty or exactly one LF")
        self.join = join
        self._fragments = []
        self._text = None

    def add(self, kind, text):
        if kind not in KINDS or not isinstance(text, str):
            raise RoomError("Prompt fragments must be tagged workflow, specification, caller or separator text")
        if kind == "separator" and self.join:
            raise RoomError("A joined prompt assembly counts its separators, not explicit separator fragments")
        if self._text is not None:
            self._text += (self.join if self._fragments else "") + text
        self._fragments.append((kind, text))
        return self

    @property
    def text(self):
        if self._text is None:
            self._text = self.join.join(fragment for _, fragment in self._fragments)
        return self._text

    def projection(self, spec_delivery, text=None):
        """Closed version-1 projection of this exact assembly; never a rebuilt prompt."""
        if spec_delivery not in DELIVERIES:
            raise RoomError("spec_delivery must be none, full or changes")
        if len([1 for kind, _ in self._fragments if kind == "caller"]) != 1:
            raise RoomError("Prompt assembly requires exactly one caller fragment")
        value = self.text
        if text is not None and text != value:
            raise RoomError("Prompt projection does not match the outgoing text")
        counts = {kind: 0 for kind in KINDS}
        for kind, fragment in self._fragments:
            counts[kind] += len(fragment.encode("utf-8"))
        if self.join:
            counts["separator"] += max(0, len(self._fragments) - 1) * len(self.join.encode("utf-8"))
        total = len(value.encode("utf-8"))
        result = {"version": VERSION, "text_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                  "total_bytes": total, "caller_bytes": counts["caller"],
                  "specification_bytes": counts["specification"], "workflow_bytes": counts["workflow"],
                  "separator_bytes": counts["separator"], "spec_delivery": spec_delivery}
        if (result["caller_bytes"] + result["specification_bytes"] + result["workflow_bytes"]
                + result["separator_bytes"]) != total:
            raise RoomError("Prompt projection components do not sum to the assembled text")
        return result


def validate_projection(projection, text):
    """Pure: the exact closed version-1 projection of ``text``, else None."""
    if not isinstance(projection, dict) or not isinstance(text, str) or set(projection) != set(PROJECTION_FIELDS):
        return None
    if type(projection.get("version")) is not int or projection["version"] != VERSION:
        return None
    if projection.get("spec_delivery") not in DELIVERIES:
        return None
    for field in BYTE_FIELDS:
        value = projection.get(field)
        if type(value) is not int or value < 0:
            return None
    text_sha256 = projection.get("text_sha256")
    if not isinstance(text_sha256, str) or not _HEX.fullmatch(text_sha256):
        return None
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return None
    if hashlib.sha256(raw).hexdigest() != text_sha256 or projection["total_bytes"] != len(raw):
        return None
    if (projection["caller_bytes"] + projection["specification_bytes"] + projection["workflow_bytes"]
            + projection["separator_bytes"]) != projection["total_bytes"]:
        return None
    return projection


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _native_id(request):
    # Preserve the same historical observed-turn fallback as native_turn_identity().
    value = request.get("provider_turn_id")
    observed = request.get("observed_turn")
    if not value and isinstance(observed, dict):
        value = observed.get("providerTurnId")
    return value if _nonempty(value) else None


def _request_identity(request):
    """Validate and select the immutable saved owner, without consulting current bindings."""
    if not isinstance(request, dict):
        raise RoomError("Saved request identity is unavailable")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
        raise RoomError("Saved request identity is invalid")
    if request.get("role") not in ROLES:
        raise RoomError("Saved request role is invalid")
    fields = ("session_id", "harness", "model", "reasoning_effort", "conversation_id", "branch_id")
    if any(not _nonempty(request.get(name)) for name in fields):
        raise RoomError("Saved request owner is incomplete")
    baseline = request.get("baseline")
    if (not isinstance(baseline, dict)
            or baseline.get("conversation_id") != request["conversation_id"]
            or baseline.get("branch_id") != request["branch_id"]
            or not isinstance(baseline.get("turn_ids"), list)
            or any(not _nonempty(item) for item in baseline["turn_ids"])
            or len(set(baseline["turn_ids"])) != len(baseline["turn_ids"])):
        raise RoomError("Saved request baseline is invalid")
    return {"request_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            "role": request["role"], **{name: request[name] for name in fields},
            "baseline": {name: baseline[name] for name in ("conversation_id", "branch_id", "turn_ids")}}


def projection_binding(request, projection):
    """Canonical digest of the exact projection and saved request/owner/turn identity.

    This does not attest provider execution. Receipt settings, native turn identity
    and the unique sent message are independently checked before observed delivery.
    """
    identity = _request_identity(request)
    if (validate_projection(projection, request.get("text")) is None
            or request.get("text_sha256") != projection["text_sha256"]):
        raise RoomError("Saved request text identity contradicts its prompt projection")
    turn_id, provider_id = request.get("turn_id"), _native_id(request)
    if not _nonempty(turn_id):
        raise RoomError("Projection receipt requires an exact saved turn identity")
    # A failed/uncertain native dispatch can lack a provider ID. Bind that absence
    # truthfully while preserving its receipt; it cannot prove observed delivery.
    return digest({"prompt_projection": projection, **identity,
                   "text_sha256": request.get("text_sha256"),
                   "turn_id": turn_id, "provider_turn_id": provider_id})


def receipt_projection_sha256(request):
    """Binding for a new receipt; None keeps the exact legacy receipt shape.

    A present-but-invalid projection is refused, never silently treated as an absent
    legacy projection and never rewritten.
    """
    if not isinstance(request, dict) or "prompt_projection" not in request:
        return None
    projection = validate_projection(request.get("prompt_projection"), request.get("text"))
    if projection is None or request.get("text_sha256") != projection["text_sha256"]:
        raise RoomError("Saved prompt projection is invalid; preserve it and reconcile before binding a receipt")
    return projection_binding(request, projection)


def assert_dispatch_projections(state):
    """Fail closed before new dispatch when a saved request carries a present-but-invalid projection."""
    requests = state.get("requests") if isinstance(state, dict) else None
    if not isinstance(requests, dict):
        return
    for request in requests.values():
        if isinstance(request, dict) and "prompt_projection" in request:
            projection = validate_projection(request.get("prompt_projection"), request.get("text"))
            if projection is None or request.get("text_sha256") != projection["text_sha256"]:
                raise RoomError("A saved prompt projection is invalid; preserve it and reconcile before dispatch")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _check_depth(value, limit):
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, (dict, list)):
            continue
        depth += 1
        if depth > limit:
            raise RoomError("Selected receipt exceeds the JSON depth bound")
        if isinstance(node, dict):
            stack.extend((item, depth) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth) for item in node)


def strict_json(text):
    """Finite, duplicate-free JSON bounded to MAX_JSON_DEPTH containers."""
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, RecursionError) as exc:
        raise RoomError("Selected receipt is not finite strict JSON") from exc
    _check_depth(value, MAX_JSON_DEPTH)
    return value


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read_bounded_json(path, maximum=None, *, dir_fd=None):
    """No-follow owned regular file, bounded read, finite duplicate-free JSON."""
    maximum = MAX_RECEIPT_BYTES if maximum is None else maximum
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise RoomError("Selected receipt is unavailable") from exc
    try:
        stream = os.fdopen(fd, "rb", buffering=0)
    except OSError as exc:
        os.close(fd)
        raise RoomError("Selected receipt is unavailable") from exc
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > maximum:
            raise RoomError("Selected receipt is not an owned bounded regular file")
        # Bound actual descriptor reads to the admitted extent, including during
        # concurrent growth. Buffered read-ahead and an extra EOF byte can exceed it.
        chunks, left = [], before.st_size
        while left:
            chunk = stream.read(min(left, 262144))
            if not chunk:
                raise RoomError("Selected receipt changed while being read")
            chunks.append(chunk)
            left -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(stream.fileno())
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    if (len(raw) > maximum or len(raw) != before.st_size
            or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(named)):
        raise RoomError("Selected receipt changed while being read")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RoomError("Selected receipt is not UTF-8") from exc
    return strict_json(text)


def _canonical(value):
    try:
        return digest(value)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RoomError("Selected receipt is not canonically hashable") from exc


def _receipt_path(directory, request_id, pointer):
    if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
        raise RoomError("Selected request identifier is unsafe")
    if not isinstance(pointer, str) or not 0 < len(pointer) <= 512:
        raise RoomError("Selected receipt pointer is unsafe")
    prefix = "receipts/" + request_id + "/"
    name = pointer[len(prefix):-5] if pointer.startswith(prefix) and pointer.endswith(".json") else ""
    if not name or not _HEX.fullmatch(name):
        raise RoomError("Selected receipt pointer is unsafe")
    return Path(directory) / pointer


@contextmanager
def _receipt_directory(directory, request_id):
    """Keep every directory descriptor through the read; never follow a link.

    The absolute room path comes from the controller. System ancestors need not be
    user-owned; the room and both receipt directories must be. Recheck each opened
    name before returning a result. Concurrent modification can refuse coverage,
    but cannot redirect this read through a replacement directory or symlink.
    """
    root = Path(directory)
    if not root.is_absolute() or any(part in (".", "..") for part in root.parts):
        raise RoomError("Selected room path is unsafe")
    opened, links = [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        current = os.open(root.anchor, flags)
        opened.append(current)
        components = list(root.parts[1:]) + ["receipts", request_id]
        for index, component in enumerate(components):
            child = os.open(component, flags, dir_fd=current)
            opened.append(child)
            info = os.fstat(child)
            if (not stat.S_ISDIR(info.st_mode)
                    or (index >= len(components) - 3 and info.st_uid != os.getuid())):
                raise RoomError("Selected receipt directory is unsafe")
            links.append((current, component, child, info.st_dev, info.st_ino))
            current = child
        yield current
        for parent, component, child, device, inode in links:
            named = os.stat(component, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            if (not stat.S_ISDIR(named.st_mode) or named.st_dev != device or named.st_ino != inode
                    or held.st_dev != device or held.st_ino != inode or held.st_uid != named.st_uid):
                raise RoomError("Selected receipt directory changed during the read")
    except OSError as exc:
        raise RoomError("Selected receipt directory is unavailable") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_selected_receipt(directory, request_id, request):
    pointer = request.get("receipt")
    if pointer is None:
        if request.get("state") in TERMINAL_STATES:
            raise RoomError("Selected receipt pointer is missing for a terminal request")
        return None
    path = _receipt_path(directory, request_id, pointer)
    with _receipt_directory(directory, request_id) as parent:
        receipt = _read_bounded_json(path.name, dir_fd=parent)
    if not isinstance(receipt, dict):
        raise RoomError("Selected receipt is not a JSON object")
    expected = request.get("receipt_sha256")
    if not isinstance(expected, str) or _canonical(receipt) != expected:
        raise RoomError("Selected receipt does not match its saved canonical identity")
    payload = {key: value for key, value in receipt.items() if key != "observed_at"}
    if os.path.basename(pointer) != _canonical(payload) + ".json":
        raise RoomError("Selected receipt payload basename does not match its content")
    return receipt


def _receipt_identity(request, receipt):
    """Check saved owner evidence before treating any receipt as verified."""
    try:
        _request_identity(request)
    except RoomError:
        return False
    turn = receipt.get("turn")
    turn_id = request.get("turn_id")
    provider_id = _native_id(request)
    settings = receipt.get("settings")
    if (not isinstance(turn, dict) or not _nonempty(turn_id)
            or not isinstance(settings, dict) or settings.get("model") != request["model"]
            or settings.get("reasoningEffort") != request["reasoning_effort"]):
        return False
    receipt_provider_id = turn.get("providerTurnId")
    receipt_provider_id = receipt_provider_id if _nonempty(receipt_provider_id) else None
    if turn.get("id") != turn_id or receipt_provider_id != provider_id:
        return False
    observed = request.get("observed_turn")
    if observed is not None:
        if not isinstance(observed, dict) or observed.get("id") != turn_id:
            return False
        observed_provider_id = observed.get("providerTurnId")
        observed_provider_id = observed_provider_id if _nonempty(observed_provider_id) else None
        if observed_provider_id != provider_id:
            return False
    if request.get("model_reroute"):
        return False
    reroute = receipt.get("modelReroute")
    if reroute is not None:
        if not isinstance(reroute, dict):
            return False
        if (reroute.get("toModel") != request["model"]
                and (not reroute.get("providerTurnId") or reroute.get("providerTurnId") == provider_id)):
            return False
    return True


def _observed_delivery(request, receipt):
    """The one exact delivered user message plus uncontradicted native turn identity."""
    if _native_id(request) is None or not _receipt_identity(request, receipt):
        return False
    messages = receipt.get("messages")
    turn_id = request.get("turn_id")
    if not isinstance(messages, list):
        return False
    baseline = request.get("baseline")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("turn_ids"), list):
        return False
    earlier = set(baseline["turn_ids"])
    matches = []
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") != "user"
                or message.get("turnId") != turn_id or message.get("turnId") in earlier):
            continue
        value = message.get("text")
        if not isinstance(value, str):
            continue
        try:
            raw = value.encode("utf-8")
        except UnicodeError:
            continue
        if hashlib.sha256(raw).hexdigest() == request.get("text_sha256"):
            matches.append(message)
    return len(matches) == 1


def _select_latest(requests):
    best_order, winners = None, []
    seen_orders = set()
    for key, request in requests.items():
        if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key) or not isinstance(request, dict):
            return None
        order = request.get("created_order")
        if type(order) is not int or order <= 0 or order in seen_orders:
            return None
        seen_orders.add(order)
        if best_order is None or order > best_order:
            best_order, winners = order, [(key, request)]
        elif order == best_order:
            winners.append((key, request))
    return winners[0] if len(winners) == 1 else None


def presentation_order(request):
    """Total ordering for the existing request list, never evidence of latest identity."""
    order, at, name = request.get("created_order"), request.get("created_at"), request.get("request_id")
    return (order if type(order) is int else 0,
            at if type(at) is int or (type(at) is float and math.isfinite(at)) else 0,
            name if isinstance(name, str) else "")


def _blank(reason):
    return {"version": VERSION, "coverage": "unavailable", "role": None, "request_sha256": None,
            "text_sha256": None, "total_bytes": None, "caller_bytes": None, "specification_bytes": None,
            "workflow_bytes": None, "separator_bytes": None, "spec_delivery": None,
            "integrity": "unavailable", "delivery": "unobserved", "reasons": [reason]}


def _latest_prompt(directory, state):
    requests = state.get("requests") if isinstance(state, dict) else None
    if not isinstance(requests, dict) or not requests:
        return _blank("no_saved_request")
    selected = _select_latest(requests)
    if selected is None:
        return _blank("request_integrity")
    request_id, request = selected
    if request.get("request_id") != request_id or request.get("role") not in ROLES:
        return _blank("projection_integrity")
    try:
        _request_identity(request)
    except RoomError:
        return _blank("projection_integrity")
    text = request.get("text")
    text_sha256 = request.get("text_sha256")
    if (not isinstance(text, str) or not isinstance(text_sha256, str) or not _HEX.fullmatch(text_sha256)):
        return _blank("projection_integrity")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return _blank("projection_integrity")
    if hashlib.sha256(raw).hexdigest() != text_sha256:
        return _blank("projection_integrity")
    request_sha256 = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    projected = "prompt_projection" in request
    projection = None
    if projected:
        projection = validate_projection(request.get("prompt_projection"), text)
        if projection is None:
            saved = request.get("prompt_projection")
            if isinstance(saved, dict) and type(saved.get("version")) is int and saved["version"] != VERSION:
                return _blank("unsupported_projection_version")
            return _blank("projection_integrity")
    delivery = "prepared"
    acknowledgement = request.get("acknowledgement")
    if (isinstance(acknowledgement, dict) and isinstance(acknowledgement.get("turnId"), str)
            and acknowledgement["turnId"].strip() and request.get("turn_id") == acknowledgement["turnId"]):
        delivery = "submitted"
    integrity = "request_observed"
    reasons = []
    try:
        receipt = _read_selected_receipt(directory, request_id, request)
    except RoomError:
        receipt = None
        reasons.append("receipt_unavailable")
    if receipt is not None:
        if projected:
            try:
                verified = (_receipt_identity(request, receipt)
                            and receipt.get("prompt_projection_sha256") == projection_binding(request, projection))
            except RoomError:
                verified = False
            if not verified:
                integrity = "unavailable"
                reasons.append("projection_integrity")
            else:
                integrity = "receipt_verified"
        elif not _receipt_identity(request, receipt):
            reasons.append("receipt_unavailable")
        if integrity != "unavailable" and _observed_delivery(request, receipt):
            delivery = "observed"
    if delivery != "observed":
        reasons.append("delivery_unobserved")
    if not projected:
        reasons.append("legacy_components_absent")
    if projected:
        fields = {"role": request.get("role"), "request_sha256": request_sha256,
                  "text_sha256": projection["text_sha256"], "total_bytes": projection["total_bytes"],
                  "caller_bytes": projection["caller_bytes"], "specification_bytes": projection["specification_bytes"],
                  "workflow_bytes": projection["workflow_bytes"], "separator_bytes": projection["separator_bytes"],
                  "spec_delivery": projection["spec_delivery"]}
    else:
        fields = {"role": request.get("role"), "request_sha256": request_sha256, "text_sha256": text_sha256,
                  "total_bytes": len(raw), "caller_bytes": None, "specification_bytes": None,
                  "workflow_bytes": None, "separator_bytes": None, "spec_delivery": None}
    return {"version": VERSION, "coverage": "known", **fields, "integrity": integrity, "delivery": delivery,
            "reasons": sorted(set(reasons))}


def latest_prompt(directory, state):
    """Closed latest-prompt coverage for the latest saved request; never assembles, scans or repairs."""
    try:
        return _latest_prompt(directory, state)
    except (RoomError, OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        return _blank("request_integrity")
