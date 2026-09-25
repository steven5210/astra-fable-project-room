"""Native Claude JSONL support, intervals and supported Read observations for the #58 audit (version 1).

The retained native schemas are consumer profiles: evidence outside the explicitly supported
shapes yields an honest unknown or incomplete result, never a widened claim. Interval and
duplicate accounting is scoped to the proved interval; only bytes actually read are charged.
No model, network, subprocess, lock or mutable state is touched here.
"""

import datetime
import hashlib
import json
import math
import os.path
import re

import ao_evidence_audit_io as audit_io


ADMIN_TYPES = frozenset(("summary", "system", "progress", "file-history-snapshot", "queue-operation"))
MESSAGE_TYPES = frozenset(("user", "assistant"))
AGENT_TOOLS = frozenset(("Agent", "Task"))
READ_TOOL = "Read"
ROLES = ("engineer", "reviewer")
AGENT_ID_PATTERN = re.compile("[A-Za-z0-9_-]{1,128}")
IMAGE_MEDIA_TYPES = frozenset(("image/jpeg", "image/png", "image/gif", "image/webp"))
READ_INPUT_KEYS = frozenset(("file_path", "offset", "limit", "pages"))
LAUNCH_DENIED_KEYS = ("resume", "resume_from", "resumeFrom", "resumeFromId", "isolated", "isolation", "remote",
                      "remote_task", "run_in_remote", "isolationMode")
TERMINAL_TURN_STATES = frozenset(("completed", "failed", "interrupted", "cancelled"))
ABSENT = object()

COUNT_KEYS = ("read_requests", "evidence_root_requests", "outside_root_requests", "unclassified_paths",
              "reported_non_error_results", "reported_error_results", "unresolved_read_results",
              "duplicate_records", "duplicate_tool_observations", "repeated_identical_inputs",
              "distinct_slices", "unsupported_tool_observations")

SOURCE_DIMENSION = frozenset(("source_missing", "source_unsafe", "source_changed", "source_active", "source_torn",
                              "source_malformed", "identity_conflict", "directory_entry_limit",
                              "transcript_bytes_limit", "aggregate_bytes_limit", "record_bytes_limit",
                              "record_count_limit", "record_id_limit", "tool_id_limit", "json_depth_limit",
                              "binding_bytes_limit"))
PATH_DIMENSION = frozenset(("path_relative", "path_invalid", "path_bytes_limit", "tool_format_unsupported"))
RESULT_DIMENSION = frozenset(("result_unresolved", "result_unclassifiable", "tool_format_unsupported"))
INTERVAL_DIMENSION = frozenset(("interval_unbound", "interval_ambiguous"))
HEX64 = re.compile("[0-9a-f]{64}")


def canonical_json(value):
    """Finite canonical UTF-8 JSON: sorted keys, compact separators, no NaN; None when not encodable."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, UnicodeEncodeError):
        return None


def canonical_bytes(value):
    text = canonical_json(value)
    if text is None:
        return None
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def actor_digest(owner_sha256, kind, agent_id):
    data = canonical_bytes({"owner_sha256": owner_sha256, "kind": kind, "agent_id": agent_id})
    if data is None:
        raise ValueError("actor identity is not encodable")
    return sha256_hex(data)


def bounded_text(value, maximum=None):
    if not isinstance(value, str) or not value:
        return False
    size = audit_io.bounded_bytes(value)
    if size is None:
        return False
    return size <= (audit_io.MAX_IDENTITY_BYTES if maximum is None else maximum)


def parse_timestamp(value):
    """Timezone-bearing ISO-8601 to UTC epoch seconds; None when missing, overlong or malformed."""
    if not isinstance(value, str) or not value:
        return None
    size = audit_io.bounded_bytes(value)
    if size is None or size > audit_io.MAX_TIMESTAMP_BYTES:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def finite_seconds(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def structured_agent_id(value):
    if not isinstance(value, dict):
        return None
    agent_id = value.get("agentId")
    if isinstance(agent_id, str) and AGENT_ID_PATTERN.fullmatch(agent_id):
        return agent_id
    return None


def valid_completed_projection(value):
    """The claude_agent_completed_v1 shape, without using any model-reported total for attribution."""
    if not isinstance(value, dict) or not isinstance(value.get("prompt"), str) or not value["prompt"]:
        return False
    content = value.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            return False
        if "citations" in block and block["citations"] is not None and not isinstance(block["citations"], list):
            return False
    for key in ("totalToolUseCount", "totalDurationMs", "totalTokens"):
        if type(value.get(key)) is not int or value[key] < 0:
            return False
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return False
    for key in ("input_tokens", "output_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            return False
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens", "server_tool_use", "service_tier",
                "cache_creation"):
        if key not in usage:
            return False
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage[key] is not None and (type(usage[key]) is not int or usage[key] < 0):
            return False
    server = usage["server_tool_use"]
    if server is not None:
        if not isinstance(server, dict):
            return False
        for key in ("web_search_requests", "web_fetch_requests"):
            if type(server.get(key)) is not int or server[key] < 0:
                return False
    if usage["service_tier"] is not None and not isinstance(usage["service_tier"], str):
        return False
    cache = usage["cache_creation"]
    if cache is not None:
        if not isinstance(cache, dict):
            return False
        for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
            if type(cache.get(key)) is not int or cache[key] < 0:
                return False
    return True


def completion_observation(value, canonical_result, timestamp):
    """(agent_id, fingerprint, prompt) for a validated completed projection, else None."""
    if canonical_result is None or not isinstance(value, dict) or value.get("status") != "completed":
        return None
    if value.get("isAsync", False) is not False:
        return None
    agent_id = structured_agent_id(value)
    if agent_id is None or not valid_completed_projection(value):
        return None
    data = canonical_bytes({"tool_result": canonical_result, "tool_use_result": value, "timestamp": timestamp})
    if data is None:
        return None
    return agent_id, sha256_hex(data), value.get("prompt")


def supported_read_input(value):
    """Supported Read input selectors, or None when the shape is unsupported for this narrow diagnostic."""
    if not isinstance(value, dict) or not value or set(value) - READ_INPUT_KEYS or "file_path" not in value:
        return None
    path = value["file_path"]
    if not isinstance(path, str) or not path:
        return None
    selectors = {}
    for key in ("offset", "limit"):
        if key in value:
            item = value[key]
            if type(item) is not int or item <= 0:
                return None
            selectors[key] = item
        else:
            selectors[key] = ABSENT
    if "pages" in value:
        pages = value["pages"]
        if not isinstance(pages, str) or not pages:
            return None
        size = audit_io.bounded_bytes(pages)
        if size is None or size > audit_io.MAX_PATH_BYTES:
            return None
        selectors["pages"] = pages
    else:
        selectors["pages"] = ABSENT
    return {"path": path, "offset": selectors["offset"], "limit": selectors["limit"], "pages": selectors["pages"]}


def selector(value):
    return ["absent"] if value is ABSENT else ["present", value]


def classify_path(path, root_norm):
    """Lexical containment only: no evidence target is resolved, opened or read."""
    size = audit_io.bounded_bytes(path)
    if size is None or size == 0 or chr(0) in path:
        return "unclassified", None, "path_invalid"
    if size > audit_io.MAX_PATH_BYTES:
        return "unclassified", None, "path_bytes_limit"
    if not os.path.isabs(path):
        return "unclassified", None, "path_relative"
    if ".." in path.split("/"):
        return "unclassified", None, "path_invalid"
    normalized = os.path.normpath(path)
    if root_norm == "/" or normalized == root_norm or normalized.startswith(root_norm + "/"):
        return "evidence", normalized, None
    return "outside", normalized, None


def classify_read(input_value, evidence_root):
    """Fully supported, classifiable Read input or None when the shape is unsupported."""
    classified = supported_read_input(input_value)
    if classified is None:
        return None
    canonical_input = canonical_json(input_value)
    if canonical_input is None:
        return None
    path_class, normalized, reason = classify_path(classified["path"], evidence_root)
    slice_text = canonical_json([normalized, selector(classified["offset"]), selector(classified["limit"]),
                                 selector(classified["pages"])])
    if slice_text is None:
        return None
    return {"path_class": path_class, "reason": reason, "normalized": normalized, "input_canonical": canonical_input,
            "slice_key": slice_text}


def launch_input(value):
    """(prompt, flagged) for a supported Agent/Task input; prompt None when the launch cannot qualify."""
    if not isinstance(value, dict):
        return None, False
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return None, False
    flagged = False
    if "run_in_background" in value and value["run_in_background"] is not False:
        flagged = True
    if any(key in value for key in LAUNCH_DENIED_KEYS):
        flagged = True
    return prompt, flagged


def supported_image(block):
    source = block.get("source")
    if set(block) != {"type", "source"} or not isinstance(source, dict) or not isinstance(source.get("type"), str):
        return False
    if source["type"] == "base64":
        return (set(source) == {"type", "media_type", "data"} and source.get("media_type") in IMAGE_MEDIA_TYPES
                and isinstance(source.get("data"), str) and bool(source["data"]))
    if source["type"] == "url":
        return set(source) == {"type", "url"} and isinstance(source.get("url"), str) and bool(source["url"])
    return False


def supported_result_content(value):
    if isinstance(value, str):
        return True
    if not isinstance(value, list):
        return False
    for block in value:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return False
        if block["type"] == "text":
            if set(block) != {"type", "text"} or not isinstance(block.get("text"), str):
                return False
        elif block["type"] == "image":
            if not supported_image(block):
                return False
        else:
            return False
    return True


def canonical_result_block(block):
    """Type-preserving canonical supported tool_result block, or None when the shape is unsupported."""
    if not isinstance(block, dict) or block.get("type") != "tool_result":
        return None
    if set(block) - {"type", "tool_use_id", "content", "is_error"}:
        return None
    tool_use_id = block.get("tool_use_id")
    if not bounded_text(tool_use_id):
        return None
    content = block["content"] if "content" in block else ABSENT
    if content is not ABSENT and not supported_result_content(content):
        return None
    error = block["is_error"] if "is_error" in block else ABSENT
    if error is not ABSENT and type(error) is not bool:
        return None
    return canonical_json({"type": "tool_result", "tool_use_id": tool_use_id,
                           "content": selector(content), "is_error": selector(error)})


def result_classification(block):
    if not isinstance(block, dict):
        return None
    error = block.get("is_error", ABSENT)
    if error is ABSENT or error is False:
        return "non_error"
    if error is True:
        return "error"
    return None


def summarize_read_results(observations):
    """non_error | error | unresolved | unclassifiable for the correlated supported read results."""
    if not observations:
        return "unresolved"
    distinct = {}
    for item in observations:
        if item.canonical is None or item.classification is None:
            return "unclassifiable"
        distinct.setdefault(item.canonical, item)
    if len(distinct) > 1:
        return "unclassifiable"
    return next(iter(distinct.values())).classification


class Collector:
    """Frozen global bounds and closed reason codes shared by every scan in one audit."""

    def __init__(self, limits, notes):
        self.limits = limits
        self.notes = notes
        self.records = 0
        self.record_ids = 0
        self.tool_ids = 0
        self.aggregate = 0

    def note(self, reason):
        self.notes.add(reason)

    def charge(self, count):
        self.aggregate += count
        if self.aggregate > self.limits["aggregate"]:
            raise audit_io.SourceError("aggregate_bytes_limit")

    def spend(self, count):
        """Account honestly for bytes already read past the admitted extent; never admits more work."""
        self.aggregate += count

    def new_pass(self):
        """The per-file and aggregate byte ceilings apply to each of the two complete passes."""
        self.aggregate = 0

    def count_records(self):
        self.records += 1
        if self.records > self.limits["records"]:
            raise audit_io.SourceError("record_count_limit")

    def count_record_id(self):
        self.record_ids += 1
        if self.record_ids > self.limits["record_ids"]:
            raise audit_io.SourceError("record_id_limit")

    def count_tool_id(self):
        self.tool_ids += 1
        if self.tool_ids > self.limits["tool_ids"]:
            raise audit_io.SourceError("tool_id_limit")


class ToolOccurrence:
    __slots__ = ("number", "timestamp", "uuid", "name", "canonical", "read", "unsupported", "launch")

    def __init__(self, number, timestamp, uuid, name, canonical, read, unsupported, launch):
        self.number = number
        self.timestamp = timestamp
        self.uuid = uuid
        self.name = name
        self.canonical = canonical
        self.read = read
        self.unsupported = unsupported
        self.launch = launch


class ToolEntry:
    """One actor-local tool identity and every occurrence seen in this actor's bounded source."""

    __slots__ = ("tool_id", "occurrences")

    def __init__(self, tool_id):
        self.tool_id = tool_id
        self.occurrences = []


class ResultObs:
    __slots__ = ("number", "timestamp", "uuid", "canonical", "classification", "agent_id", "fingerprint",
                 "prompt", "source_uuid")

    def __init__(self, number, timestamp, uuid, canonical, classification, source_uuid):
        self.number = number
        self.timestamp = timestamp
        self.uuid = uuid
        self.canonical = canonical
        self.classification = classification
        self.source_uuid = source_uuid
        self.agent_id = None
        self.fingerprint = None
        self.prompt = None


class HumanObs:
    __slots__ = ("number", "timestamp", "uuid", "text_sha256", "unique")

    def __init__(self, number, timestamp, uuid, text_sha256, unique):
        self.number = number
        self.timestamp = timestamp
        self.uuid = uuid
        self.text_sha256 = text_sha256
        self.unique = unique


class Interval:
    """A proved parent interval: an event must carry both a record number and a timestamp inside it."""

    __slots__ = ("kind", "anchor_number", "anchor_timestamp", "end_number", "end_timestamp")

    def __init__(self, kind, anchor_number, anchor_timestamp, end_number, end_timestamp):
        self.kind = kind
        self.anchor_number = anchor_number
        self.anchor_timestamp = anchor_timestamp
        self.end_number = end_number
        self.end_timestamp = end_timestamp

    def contains(self, number, timestamp):
        if timestamp is None or timestamp < self.anchor_timestamp:
            return False
        if self.kind == "boundary":
            return (self.anchor_number < number < self.end_number
                    and self.anchor_timestamp <= timestamp <= self.end_timestamp)
        return number > self.anchor_number and self.anchor_timestamp <= timestamp <= self.end_timestamp

    def contains_time(self, timestamp):
        return timestamp is not None and self.anchor_timestamp <= timestamp <= self.end_timestamp

    def covers_child(self, launch_number, launch_timestamp, result_number, result_timestamp):
        if launch_timestamp is None or result_timestamp is None or launch_timestamp > result_timestamp:
            return False
        return (self.contains(launch_number, launch_timestamp)
                and self.contains(result_number, result_timestamp))

    def child(self, start, end):
        return ChildInterval(start, end, (self,))


class ChildInterval:
    """A proved child interval [start, end] in timestamp order, bounded by every ancestor interval."""

    __slots__ = ("start", "end", "ancestors")

    def __init__(self, start, end, ancestors=()):
        self.start = start
        self.end = end
        self.ancestors = tuple(ancestors)

    def contains(self, number, timestamp):
        return self.contains_time(timestamp)

    def contains_time(self, timestamp):
        return (timestamp is not None and self.start <= timestamp <= self.end
                and all(ancestor.contains_time(timestamp) for ancestor in self.ancestors))

    def covers_child(self, launch_number, launch_timestamp, result_number, result_timestamp):
        if launch_timestamp is None or result_timestamp is None or launch_timestamp > result_timestamp:
            return False
        if not (self.start <= launch_timestamp <= self.end and self.start <= result_timestamp <= self.end):
            return False
        # Record numbers are actor-local. Only timestamps can be compared across transcripts.
        return self.contains_time(launch_timestamp) and self.contains_time(result_timestamp)

    def child(self, start, end):
        return ChildInterval(start, end, self.ancestors + (self,))


class Candidate:
    __slots__ = ("agent_id", "interval", "reason")

    def __init__(self, agent_id, interval, reason):
        self.agent_id = agent_id
        self.interval = interval
        self.reason = reason


def human_candidate(record):
    """A potentially genuine human record or boundary: never a compaction summary or notification."""
    origin = record.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return False
    if record.get("isCompactSummary") is True:
        return False
    return True


def looks_human(record):
    return human_candidate(record)


class RecordScanner:
    """One actor's bounded record scan: supported observations, boundaries and source notes."""

    def __init__(self, collector, actor_sha256, kind, agent_id, native_session_id, workspace, evidence_root):
        self.collector = collector
        self.actor_sha256 = actor_sha256
        self.kind = kind
        self.agent_id = agent_id
        self.session_id = native_session_id
        self.workspace = workspace
        self.evidence_root = evidence_root
        self.notes = set()
        self.failure = None
        self.torn = False
        self.duplicate_records = 0
        self.duplicates = []
        self.record_conflict = False
        self.contradictory = False
        self.record_ids = {}
        self.tool_entries = {}
        self.results = {}
        self.humans = []

    def note(self, reason):
        self.notes.add(reason)
        self.collector.note(reason)

    def _human(self, record, number, timestamp, uuid_value, unique, text):
        if self.kind != "parent" or not human_candidate(record):
            return
        if record.get("isSidechain") is True or record.get("agentId") is not None:
            return
        digest_value = None
        if isinstance(text, str):
            try:
                digest_value = sha256_hex(text.encode("utf-8"))
            except UnicodeEncodeError:
                digest_value = None
        self.humans.append(HumanObs(number, timestamp, uuid_value, digest_value, unique))

    @staticmethod
    def _human_text(record, content):
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        texts = []
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                return None
            if block["type"] == "text":
                if not isinstance(block.get("text"), str):
                    return None
                texts.append(block["text"])
            elif block["type"] == "image":
                return None
            else:
                return None
        return "".join(texts)

    def record(self, payload, terminated, number):
        self.collector.count_records()
        if not terminated:
            self.torn = True
            self.note("source_torn")
        try:
            value = audit_io.parse_record(payload, self.collector.limits["depth"], "source_malformed")
        except audit_io.SourceError as exc:
            self.note("json_depth_limit" if exc.reason == "json_depth_limit" else "source_malformed")
            return
        if not isinstance(value, dict):
            self.note("source_malformed")
            return
        kind = value.get("type")
        if kind in ADMIN_TYPES:
            return
        if kind not in MESSAGE_TYPES:
            self.note("source_malformed")
            return
        timestamp = parse_timestamp(value.get("timestamp"))
        human_like = self.kind == "parent" and kind == "user" and human_candidate(value)
        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) and message.get("role") == kind else None
        human_text = self._human_text(value, content) if human_like else None
        uuid_value = value.get("uuid")
        if not bounded_text(uuid_value):
            self.note("source_malformed")
            self._human(value, number, timestamp, None, False, human_text)
            return
        digest_value = sha256_hex(payload)
        previous = self.record_ids.get(uuid_value)
        unique = previous is None
        if unique:
            self.record_ids[uuid_value] = digest_value
            self.collector.count_record_id()
        elif previous == digest_value:
            self.duplicates.append((number, timestamp))
            self.duplicate_records += 1
            for human in self.humans:
                if human.uuid == uuid_value:
                    human.unique = False
            return
        else:
            self.record_conflict = True
            self.note("identity_conflict")
            for human in self.humans:
                if human.uuid == uuid_value:
                    human.unique = False
            self._human(value, number, timestamp, uuid_value, False, human_text)
            return
        if value.get("sessionId") != self.session_id:
            self.note("source_malformed")
            self._human(value, number, timestamp, uuid_value, False, human_text)
            return
        cwd = value.get("cwd")
        if not isinstance(cwd, str) or not cwd or os.path.normpath(cwd) != self.workspace:
            self.note("source_malformed")
            self._human(value, number, timestamp, uuid_value, False, human_text)
            return
        sidechain = value.get("isSidechain", False)
        if type(sidechain) is not bool:
            self.note("source_malformed")
            self._human(value, number, timestamp, uuid_value, False, human_text)
            return
        if self.kind == "parent":
            if sidechain:
                return
            if "agentId" in value:
                self.note("identity_conflict")
                return
        else:
            if sidechain is not True or value.get("agentId") != self.agent_id:
                self.contradictory = True
                self.note("child_attribution_ambiguous")
                return
        if not isinstance(message, dict) or message.get("role") != kind:
            self.note("source_malformed")
            self._human(value, number, timestamp, uuid_value, False, human_text)
            return
        if timestamp is None:
            self.note("source_malformed")
            self._human(value, number, None, uuid_value, unique, human_text)
            return
        if kind == "assistant":
            self._assistant(content, uuid_value, timestamp, number)
        else:
            self._user(value, content, uuid_value, timestamp, number, unique)

    def _assistant(self, content, uuid_value, timestamp, number):
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            self.note("source_malformed")
            return
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                self.note("source_malformed")
                return
            if block["type"] != "tool_use":
                continue
            tool_id = block.get("id")
            name = block.get("name")
            input_value = block.get("input")
            if not bounded_text(tool_id) or not bounded_text(name) or not isinstance(input_value, dict):
                self.note("source_malformed")
                return
            canonical = canonical_json({"type": "tool_use", "id": tool_id, "name": name, "input": input_value})
            if canonical is None:
                self.note("source_malformed")
                return
            entry = self.tool_entries.get(tool_id)
            if entry is None:
                entry = ToolEntry(tool_id)
                self.tool_entries[tool_id] = entry
                self.collector.count_tool_id()
            classified = None
            unsupported = False
            if name == READ_TOOL:
                classified = classify_read(input_value, self.evidence_root)
                unsupported = classified is None
            launch = launch_input(input_value) if name in AGENT_TOOLS else None
            entry.occurrences.append(ToolOccurrence(number, timestamp, uuid_value, name, canonical, classified,
                                                    unsupported, launch))

    def _user(self, record, content, uuid_value, timestamp, number, unique):
        if isinstance(content, str):
            self._human(record, number, timestamp, uuid_value, unique, content)
            return
        if not isinstance(content, list):
            self.note("source_malformed")
            self._human(record, number, timestamp, uuid_value, False, None)
            return
        has_result = any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
        if has_result:
            self._human(record, number, timestamp, uuid_value, False, None)
            self._results(record, content, uuid_value, timestamp, number)
            return
        texts = []
        only_text = True
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                self.note("source_malformed")
                self._human(record, number, timestamp, uuid_value, False, None)
                return
            if block["type"] == "text":
                if not isinstance(block.get("text"), str):
                    self.note("source_malformed")
                    self._human(record, number, timestamp, uuid_value, False, None)
                    return
                texts.append(block["text"])
            elif block["type"] == "image":
                only_text = False
            else:
                self.note("source_malformed")
                self._human(record, number, timestamp, uuid_value, False, None)
                return
        self._human(record, number, timestamp, uuid_value, unique, "".join(texts) if only_text else None)

    def _results(self, record, content, uuid_value, timestamp, number):
        structured = record.get("toolUseResult", ABSENT)
        source_uuid = record.get("sourceToolAssistantUUID")
        if not bounded_text(source_uuid):
            source_uuid = None
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                self.note("source_malformed")
                return
            if block["type"] != "tool_result":
                if block["type"] == "text" and isinstance(block.get("text"), str):
                    continue
                if block["type"] == "image" and supported_image(block):
                    continue
                self.note("source_malformed")
                return
            tool_use_id = block.get("tool_use_id")
            if not bounded_text(tool_use_id):
                self.note("result_unclassifiable")
                continue
            canonical = canonical_result_block(block)
            classification = result_classification(block) if canonical is not None else None
            observation = ResultObs(number, timestamp, uuid_value, canonical, classification, source_uuid)
            if structured is not ABSENT and classification == "non_error":
                completion = completion_observation(structured, canonical, timestamp)
                if completion is not None:
                    observation.agent_id, observation.fingerprint, observation.prompt = completion
                else:
                    observation.agent_id = structured_agent_id(structured)
                    observation.prompt = structured.get("prompt") if isinstance(structured, dict) else None
            self.results.setdefault(tool_use_id, []).append(observation)


def parent_interval(scan, request, receipt, allow_terminal=True):
    """(Interval | None, reasons tuple) for the selected request's parent interval."""
    text_sha256 = request.get("text_sha256")
    created_at = request.get("created_at")
    observed_at = receipt.get("observed_at")
    if (not isinstance(text_sha256, str) or not HEX64.fullmatch(text_sha256)
            or not finite_seconds(created_at) or not finite_seconds(observed_at)):
        return None, ("interval_unbound",)
    candidates = [human for human in scan.humans
                  if human.unique and human.timestamp is not None and human.text_sha256 == text_sha256
                  and created_at <= human.timestamp <= created_at + 60.0 and human.timestamp <= observed_at]
    if not candidates:
        return None, ("interval_unbound",)
    if len(candidates) > 1:
        return None, ("interval_ambiguous",)
    anchor = candidates[0]
    later = [human for human in scan.humans if human.number > anchor.number]
    if later:
        endpoint = min(later, key=lambda human: human.number)
        if endpoint.timestamp is None or not endpoint.unique:
            return None, ("interval_unbound",)
        if endpoint.timestamp <= anchor.timestamp:
            return None, ("interval_ambiguous",)
        if any(other.number != endpoint.number and other.timestamp == endpoint.timestamp for other in later):
            return None, ("interval_ambiguous",)
        return Interval("boundary", anchor.number, anchor.timestamp, endpoint.number, endpoint.timestamp), ()
    if not allow_terminal:
        return None, ("interval_unbound",)
    turn = receipt.get("turn")
    if not isinstance(turn, dict) or turn.get("state") not in TERMINAL_TURN_STATES:
        return None, ("interval_unbound",)
    completed_at = parse_timestamp(turn.get("completedAt"))
    if completed_at is None or not (anchor.timestamp <= completed_at <= observed_at):
        return None, ("interval_unbound",)
    return Interval("terminal", anchor.number, anchor.timestamp, None, completed_at), ()


def launch_candidates(scan, scope):
    """Resolve every Agent/Task launch inside the scope into one bounded candidate in admission order."""
    launch_uuids = set()
    for entry in scan.tool_entries.values():
        for occurrence in entry.occurrences:
            if occurrence.name in AGENT_TOOLS:
                launch_uuids.add(occurrence.uuid)
    candidates = []
    ordered = sorted(scan.tool_entries.values(), key=lambda item: item.occurrences[0].number)
    for entry in ordered:
        occurrences = [occurrence for occurrence in entry.occurrences if scope.contains(occurrence.number,
                                                                                      occurrence.timestamp)]
        if not occurrences:
            continue
        if any(occurrence.name not in AGENT_TOOLS for occurrence in occurrences):
            if any(occurrence.name in AGENT_TOOLS for occurrence in occurrences):
                candidates.append(Candidate(None, None, "child_attribution_ambiguous"))
            continue
        if len(set(occurrence.canonical for occurrence in occurrences)) > 1:
            candidates.append(Candidate(None, None, "child_attribution_ambiguous"))
            continue
        first = occurrences[0]
        prompt, flagged = first.launch if first.launch is not None else (None, False)
        if not isinstance(prompt, str) or not prompt:
            candidates.append(Candidate(None, None, "launch_unresolved"))
            continue
        observations = [item for item in scan.results.get(entry.tool_id, ())
                        if scope.contains(item.number, item.timestamp)]
        mismatched = [item for item in observations
                      if item.source_uuid is not None and item.source_uuid != first.uuid
                      and item.source_uuid in launch_uuids]
        ids = {item.agent_id for item in observations if item.agent_id}
        if mismatched or len(ids) > 1:
            candidates.append(Candidate(None, None, "child_attribution_ambiguous"))
            continue
        if not ids:
            candidates.append(Candidate(None, None, "launch_unresolved"))
            continue
        agent_id = next(iter(ids))
        completions = {}
        for item in observations:
            if item.fingerprint is None or item.canonical is None or item.prompt != prompt:
                continue
            completions.setdefault(item.fingerprint, item)
        if len(completions) > 1:
            candidates.append(Candidate(agent_id, None, "child_attribution_ambiguous"))
            continue
        if flagged or not completions:
            candidates.append(Candidate(agent_id, None, "child_interval_unbound"))
            continue
        completion = next(iter(completions.values()))
        if not scope.covers_child(first.number, first.timestamp, completion.number, completion.timestamp):
            candidates.append(Candidate(agent_id, None, "child_interval_unbound"))
            continue
        candidates.append(Candidate(agent_id, scope.child(first.timestamp, completion.timestamp), None))
    return candidates


def count_group(scan, interval):
    """(counts | None, reasons, flags) for one actor under its proved interval.

    Every partition is scoped to the interval: duplicates, conflicting identities, paths,
    inputs and results after the exclusive boundary never leak into these counts.
    """
    flags = {"quarantined": False, "unsupported": False}
    if interval is None:
        return None, set(scan.notes), flags
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["duplicate_records"] = sum(1 for number, timestamp in scan.duplicates
                                      if interval.contains(number, timestamp))
    slices = set()
    inputs = {}
    for entry in scan.tool_entries.values():
        occurrences = [occurrence for occurrence in entry.occurrences if interval.contains(occurrence.number,
                                                                                          occurrence.timestamp)]
        if not occurrences:
            continue
        names = {occurrence.name for occurrence in occurrences}
        canonicals = [occurrence.canonical for occurrence in occurrences]
        per_canonical = {}
        for canonical in canonicals:
            per_canonical[canonical] = per_canonical.get(canonical, 0) + 1
        counts["duplicate_tool_observations"] += sum(size - 1 for size in per_canonical.values() if size > 1)
        observations = [item for item in scan.results.get(entry.tool_id, ())
                        if interval.contains(item.number, item.timestamp)]
        result_canonicals = {}
        for item in observations:
            if item.canonical is not None:
                result_canonicals[item.canonical] = result_canonicals.get(item.canonical, 0) + 1
        counts["duplicate_tool_observations"] += sum(size - 1 for size in result_canonicals.values() if size > 1)
        if len(names) > 1:
            if READ_TOOL in names:
                flags["quarantined"] = True
                counts["unsupported_tool_observations"] += 1
                scan.note("identity_conflict")
            continue
        if len(set(canonicals)) > 1:
            if READ_TOOL in names:
                flags["quarantined"] = True
                counts["unsupported_tool_observations"] += 1
                scan.note("identity_conflict")
            continue
        if occurrences[0].name != READ_TOOL:
            continue
        classified = occurrences[0].read
        if classified is None:
            flags["unsupported"] = True
            counts["unsupported_tool_observations"] += 1
            scan.note("tool_format_unsupported")
            continue
        counts["read_requests"] += 1
        if classified["path_class"] == "evidence":
            counts["evidence_root_requests"] += 1
            slices.add(classified["slice_key"])
        elif classified["path_class"] == "outside":
            counts["outside_root_requests"] += 1
            slices.add(classified["slice_key"])
        else:
            counts["unclassified_paths"] += 1
            scan.note(classified["reason"])
        inputs[classified["input_canonical"]] = inputs.get(classified["input_canonical"], 0) + 1
        outcome = summarize_read_results(observations)
        if outcome == "non_error":
            counts["reported_non_error_results"] += 1
        elif outcome == "error":
            counts["reported_error_results"] += 1
        else:
            counts["unresolved_read_results"] += 1
            scan.note("result_unresolved" if outcome == "unresolved" else "result_unclassifiable")
    counts["distinct_slices"] = len(slices)
    counts["repeated_identical_inputs"] = sum(size - 1 for size in inputs.values() if size > 1)
    return counts, set(scan.notes), flags


def group_report(scan, interval):
    """The exact public actor group; counts are null only for an unavailable whole group."""
    counts, reasons, flags = count_group(scan, interval)
    if counts is None:
        return {"coverage": "unavailable", "source_coverage": "unavailable", "interval_coverage": "unavailable",
                "path_coverage": "unavailable", "result_coverage": "unavailable",
                "counts": dict.fromkeys(COUNT_KEYS), "reasons": sorted(reasons)}
    source = "incomplete" if (reasons & SOURCE_DIMENSION) else "complete"
    interval_label = "incomplete" if (reasons & INTERVAL_DIMENSION) else "complete"
    path = "incomplete" if (reasons & PATH_DIMENSION) or flags["quarantined"] or flags["unsupported"] else "complete"
    result = "incomplete" if (reasons & RESULT_DIMENSION) or flags["quarantined"] or flags["unsupported"] else "complete"
    coverage = "complete" if source == interval_label == path == result == "complete" else "incomplete"
    return {"coverage": coverage, "source_coverage": source, "interval_coverage": interval_label,
            "path_coverage": path, "result_coverage": result, "counts": counts, "reasons": sorted(reasons)}
