"""Private, deny-only PreToolUse guard for one prepared Project Room engineer.

This file is copied alone into private controller state (content-addressed) and
executed by Claude's hook shell for the prepared worktree, so it uses only the
stdlib. It reads one PreToolUse event from stdin and either prints a deny decision
(exit 0) or prints nothing (exit 0). It never grants a permission, so ordinary
permission handling still applies to every call it does not deny. Any error exits
2, which Claude treats as a block: a broken, missing or misfed guard fails closed.
"""

import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid

try:
    # POSIX is required; without fcntl the guard refuses Read admission rather
    # than admitting a read without a durable reservation.
    import fcntl
except ImportError:  # pragma: no cover - exercised only where fcntl is unavailable.
    fcntl = None

PINNED_AGENTS = ("pr-sonnet", "pr-opus")
BROWSER_AGENT = "pr-opus"
BROWSER_SKILL = "claude-in-chrome"  # the exact skill observed in the prior Opus browser task
AGENT_KEYS = frozenset({"description", "prompt", "subagent_type", "run_in_background"})
DENIED_TOOLS = ("Workflow", "Task", "SendMessage")
# MCP servers that submit delegate or room work; only the root engineer may use them.
SUBMISSION_PREFIXES = ("mcp__deepseek__", "mcp__qwen-local__", "mcp__project-room__")
# Fable directs and reviews; execution belongs to the assigned operator or pinned workers.
# Enumerating root capabilities also closes alternate shell/notebook/browser/MCP execution routes.
ROOT_TOOLS = frozenset({"Read", "Glob", "Grep", "ToolSearch", "TaskOutput", "TodoWrite",
                        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "AskUserQuestion"})
DEEPSEEK_TOOLS = frozenset("mcp__deepseek__" + name for name in
                         ("deepseek_health", "deepseek_submit", "deepseek_ask", "deepseek_status",
                          "deepseek_result", "deepseek_cancel"))
MAX_EVENT_BYTES = 1_000_000
# Limit work per submission, while requiring the complete current human turn.
# An older transcript can exceed this bound; a turn without its caller in the
# inspected window cannot be declared clear.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_RECORDS = 20_000
# Root Read admission (see docs/read-admission.md). These are conservative,
# product-local budgets for estimated raw UTF-8 source bytes, never tokens,
# billing or exact native tool results. MAX is unchanged.
READ_ADMISSION_VERSION = 1
READ_MAX_SELECTED_BYTES = 32_768
READ_MAX_MESSAGE_BYTES = 65_536
READ_LINE_WRAPPER_MAX_BYTES = 8
READ_MAX_MESSAGE_FRAMED_BYTES = READ_MAX_MESSAGE_BYTES + READ_MAX_SELECTED_BYTES
READ_BATCH_MAX_ENTRIES = 96
READ_MAX_OFFSET = 5_000_000
READ_MAX_LIMIT = 50_000
READ_MAX_SCAN_BYTES = 2 * 1024 * 1024
READ_SCAN_CHUNK_BYTES = 65_536
READ_TRANSCRIPT_WINDOW_BYTES = 2 * 1024 * 1024
READ_MAX_TRANSCRIPT_RECORDS = 20_000
READ_STATE_MAX_RECORDS = 1024
READ_STATE_MAX_ENTRIES = READ_STATE_MAX_RECORDS + 256
READ_RECORD_MAX_BYTES = 32_768
READ_LOCK_WAIT_SECONDS = 2.0
READ_STATE_DIR = ".project-room-read-admission"
READ_LOCK_NAME = "lock"
READ_SCRATCH_PREFIX = ".tmp-"
READ_ALLOWED_FIELDS = frozenset({"file_path", "offset", "limit"})
READ_RECORD_KEYS = frozenset({"version", "session_id", "message_id_sha256", "transcript_device",
                              "transcript_inode", "total_raw_bytes", "total_lines", "entries"})
READ_ENTRY_KEYS = frozenset({"args_sha256", "raw_bytes", "lines", "device", "inode", "size",
                             "mtime_ns", "ctime_ns"})
READ_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json")
READ_DIGEST = re.compile(r"[0-9a-f]{64}")
# Per-session continuity and tool-identity binding (see docs/read-admission.md).
READ_SESSION_NAME = "continuity.json"
READ_SESSION_KEYS = frozenset({"version", "session_id", "transcript_device", "transcript_inode",
                               "transcript_size", "anchor_sha256", "bindings"})
READ_SESSION_MAX_BYTES = 1_048_576
READ_STATE_MAX_BINDINGS = 2048
READ_CONTINUITY_ANCHOR_BYTES = 65_536
READ_BINDING_VALUE = re.compile(r"[0-9a-f]{64}:[0-9a-f]{64}")
# Bounded foreground readiness for the observed native flush race. The wait happens
# inside one hook invocation only: no background polling, no provider or usage probe,
# no second model call, and nothing is written before a positive identity.
READ_CORRELATION_ATTEMPTS = 8
READ_CORRELATION_WAIT_SECONDS = 2.0
READ_CORRELATION_SLEEP_SECONDS = 0.2
# Read is selected by target file, so known auto-detected container forms are
# refused by suffix and by leading bytes instead of being charged as text.
READ_CONTAINER_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif",
                           ".tiff", ".heic", ".heif", ".avif", ".ipynb", ".zip", ".gz",
                           ".tgz", ".tar", ".xlsx", ".docx", ".pptx", ".doc", ".xls", ".ppt")
READ_CONTAINER_MAGIC = ((b"%PDF-", "PDF document"), (b"\x89PNG\r\n\x1a\n", "PNG image"),
                        (b"\xff\xd8\xff", "JPEG image"), (b"GIF87a", "GIF image"),
                        (b"GIF89a", "GIF image"), (b"II*\x00", "TIFF image"),
                        (b"MM\x00*", "TIFF image"), (b"PK\x03\x04", "ZIP or office container"),
                        (b"\x1f\x8b", "gzip archive"))
# Positively identified external SDK caller dialects (Claude Code 2.1.28x). A missing
# origin alone is never human evidence: a dialect is certified only by its complete
# identity, and the origin.kind=human shapes (no turnOrigin on 2.1.268, turnOrigin=human
# on the 2.1.28x sdk-ts rows) stay deliberately supported. See docs/read-admission.md
# for the limits that remain in every dialect.
SDK_CALLER_PROMPT_SOURCE = "sdk"
SDK_CALLER_TURN_ORIGIN = "sdk"
SDK_CALLER_USER_TYPE = "external"
SDK_CALLER_ENTRYPOINTS = ("sdk-cli", "sdk-ts")
HUMAN_CALLER_TURN_ORIGIN = "human"
SDK_CALLER_IDENTITY_KEYS = ("turnOrigin", "promptId")
SDK_CALLER_ENVELOPE_PREFIXES = ("<task-notification", "<system-reminder", "<local-command",
                                "<command-name", "<command-message")
QUOTA_HOOK_FIELDS = ("session_id", "transcript_path", "cwd")
NEW_WORK_TOOLS = frozenset({"Agent", "mcp__deepseek__deepseek_submit", "mcp__deepseek__deepseek_ask"})
# Inherited process-environment disagreements that would override, remap or contradict the pinned
# worker launch. Only key names are ever reported, so no value (including a URL) reaches diagnostics.
# This file is copied standalone: these are explicit bounded key checks, not a model catalog import.
FORCED_SUBAGENT_ENV = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
ALIAS_REMAP_ENV = ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL")
GATEWAY_ENV = "ANTHROPIC_BASE_URL"
PINNED_WORKER_ENV = {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2"}
# Native model-specific windows (for example seven_day_opus) are deliberately
# excluded: a per-model rate limit does not establish a shared account stop.
ACCOUNT_QUOTA_WINDOWS = frozenset({"five_hour", "seven_day"})
ACCOUNT_QUOTA_TEXT = re.compile(r"You've hit your (?:session|account) limit(?: · resets [^\r\n]{1,200})?")


def _absolute_parts(value):
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
            or "\x00" in value):
        raise ValueError("native quota identity requires an absolute path")
    parts = value.split("/")[1:]
    if not parts or len(parts) > 64 or any(x in ("", ".", "..") for x in parts):
        raise ValueError("native quota path has unsafe components")
    return parts


def _open_component(name, flags, dir_fd):
    """Descriptor-relative open seam, including for offline race tests."""
    return os.open(name, flags, dir_fd=dir_fd)


def _file_identity(metadata):
    """The exact file revision fields used to bind decisions to bytes on disk."""
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns)


def _open_owned_transcript_leaf(path):
    """Open the exact owned private native transcript without following symlinks."""
    parts = _absolute_parts(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    directory = os.open("/", directory_flags)
    try:
        for part in parts[:-1]:
            child = _open_component(part, directory_flags, directory)
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            # Root-owned sticky system directories such as /private/tmp are safe
            # ancestors of an owned private directory; shared writable directories
            # without that protection are not.
            sticky_system = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            if (metadata.st_uid not in (0, os.getuid())
                    or metadata.st_mode & 0o022 and not sticky_system):
                raise ValueError("native quota transcript directory ownership is unsafe")
        leaf = _open_component(parts[-1], leaf_flags, directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(leaf)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_nlink != 1):
            raise ValueError("native quota transcript must be an owned private regular file")
    except BaseException:
        os.close(leaf)
        raise
    return leaf, before


def _transcript_window(path, limit):
    """Read an owned regular file without following any path component symlink.

    Returns the bounded tail together with the file's device, inode, size and a
    bounded anchor digest over the bytes ending at that size, so a caller can bind a
    decision to the exact transcript revision it inspected. The anchor is a fixed
    bounded window, not an append-only proof for the whole history.
    """
    leaf, before = _open_owned_transcript_leaf(path)
    try:
        start = max(0, before.st_size - limit)
        # One preceding byte establishes whether the first retained line is whole.
        offset = start - 1 if start else 0
        raw = os.pread(leaf, before.st_size - offset, offset)
        after = os.fstat(leaf)
        if len(raw) != before.st_size - offset or _file_identity(before) != _file_identity(after):
            raise ValueError("native quota transcript changed during inspection")
        anchor_length = min(len(raw), READ_CONTINUITY_ANCHOR_BYTES)
        anchor = (hashlib.sha256(raw[-anchor_length:]).hexdigest() if anchor_length
                  else hashlib.sha256(b"").hexdigest())
        if start:
            preceding, raw = raw[:1], raw[1:]
            if preceding != b"\n":
                _, separator, raw = raw.partition(b"\n")
                if not separator:
                    raw = b""
        return raw, before.st_dev, before.st_ino, before.st_size, anchor
    finally:
        os.close(leaf)


def _transcript_tail(path):
    """Native quota evidence tail over the same bounded owned-file inspection."""
    raw, _, _, _, _ = _transcript_window(path, MAX_TRANSCRIPT_BYTES)
    return raw


def _transcript_anchor_digest(path, device, inode, size, length):
    """Digest the bounded bytes ending at an already verified transcript size.

    Continuity is established over this fixed window, so a same-inode rewrite that
    shortens the history or changes this position is refused instead of opening a
    fresh read budget. The window is bounded evidence, not an append-only proof.
    """
    leaf, before = _open_owned_transcript_leaf(path)
    try:
        if (before.st_dev, before.st_ino) != (device, inode) or before.st_size < size:
            raise ValueError("native transcript identity changed during inspection")
        start = max(0, size - length)
        raw = os.pread(leaf, size - start, start)
        after = os.fstat(leaf)
        if len(raw) != size - start or _file_identity(before) != _file_identity(after):
            raise ValueError("native transcript changed during inspection")
        return hashlib.sha256(raw).hexdigest()
    finally:
        os.close(leaf)


def _root_session_record(row, session_id):
    return (row.get("sessionId") == session_id
            and row.get("isSidechain") in (None, False) and row.get("agentId") is None
            and row.get("agent_id") is None)


def _text_content(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if (isinstance(content, list) and content and all(isinstance(item, dict)
            and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content)):
        return "".join(item["text"] for item in content)
    return None


def _human_caller(row):
    message = row.get("message")
    origin = row.get("origin")
    return (row.get("type") == "user" and not row.get("isCompactSummary") and not row.get("isMeta")
            and not row.get("isVisibleInTranscriptOnly") and isinstance(origin, dict)
            and origin.get("kind") == "human" and isinstance(message, dict)
            and message.get("role") == "user" and _text_content(message) is not None)


class _CallerIdentityError(ValueError):
    """A caller-shaped record claims an identity that cannot be certified.

    Such a record is refused instead of being skipped: skipping it could resolve the
    current turn to an older caller and falsely clear a stale or stopped turn.
    """


def _prompt_uuid(value):
    """Canonical UUID form of one promptId, or _CallerIdentityError when unusable."""
    if not isinstance(value, str) or not value:
        raise _CallerIdentityError(
            "a caller record presents external SDK caller identity keys but carries no usable "
            "promptId UUID; caller identity is refused rather than inferred from an older record")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise _CallerIdentityError(
            "a caller record presents external SDK caller identity keys but its promptId is not a "
            "UUID; caller identity is refused rather than inferred from an older record")


def _hook_prompt_id(event):
    """The hook's canonical prompt_id, or None when the event omits the field.

    A missing optional prompt_id stays supported for older hook events: it is the
    documented case where no caller prompt binding can be enforced. A prompt_id that
    is present but null, empty, non-text or not a UUID is refused distinctly, because
    the hook presents a field that cannot be certified; no caller is inferred from an
    older record and an explicit null is never treated as an omitted field.
    """
    if "prompt_id" not in event:
        return None
    value = event["prompt_id"]
    if value is None:
        raise ValueError("native hook prompt_id is present but null, so no current caller can be bound")
    if not isinstance(value, str):
        raise ValueError("native hook prompt_id is present but not text, so no current caller can be bound")
    if not value:
        raise ValueError("native hook prompt_id is present but empty, so no current caller can be bound")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError("native hook prompt_id is not a UUID, so no current caller can be bound")


def _caller_envelope(text):
    """True for harness-generated envelopes that are never a fresh human caller."""
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in SDK_CALLER_ENVELOPE_PREFIXES)


def _prompt_mismatch_error():
    return ("the root caller record's promptId does not match the prompt_id of the current hook "
            "event, so the current caller cannot be bound and no older record may clear this turn")


def _certify_sdk_caller(row, prompt_id):
    """Certify the complete external SDK caller dialect; refuse anything incomplete."""
    missing = [name for name, expected in (("promptSource", SDK_CALLER_PROMPT_SOURCE),
                                           ("userType", SDK_CALLER_USER_TYPE))
               if row.get(name) != expected]
    if row.get("entrypoint") not in SDK_CALLER_ENTRYPOINTS:
        missing.append("entrypoint")
    if missing:
        raise _CallerIdentityError(
            "a root user record presents external SDK caller identity keys but not the complete "
            "dialect (" + ", ".join(missing) + " missing or different); caller identity is refused "
            "rather than inferred from an older record")
    canonical = _prompt_uuid(row.get("promptId"))
    if prompt_id is not None and canonical != prompt_id:
        raise _CallerIdentityError(_prompt_mismatch_error())
    return canonical


def _caller_kind(row, prompt_id=None):
    """Classify one root-session record as 'human', 'sdk' or None when it is not a caller.

    'human' is the origin.kind=human dialect: Claude Code 2.1.268 wrote it with no
    turnOrigin and 2.1.282 writes it with turnOrigin=human under the sdk-ts
    entrypoint; both stay deliberately supported. 'sdk' is the positively
    identified external SDK caller dialect with no origin field: plain text
    content, promptSource=sdk, turnOrigin=sdk, userType=external, entrypoint
    sdk-cli or sdk-ts and a promptId UUID.
    ``prompt_id`` is the hook's own prompt_id when it supplied one; it is then
    enforced against the record's promptId.

    A missing origin is never human evidence. Records that are positively not fresh
    callers (compaction summaries, meta or visible-only records, tool results, nested
    workers and harness envelopes) are never callers, and an explicit nonhuman origin
    is never overridden. A plain-text user record that presents the new dialect's
    identity keys but cannot be certified raises _CallerIdentityError instead of being
    skipped, so a scan can never fall through to an older caller.
    """
    if row.get("type") != "user":
        return None
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    origin = row.get("origin")
    if origin is not None and not isinstance(origin, dict):
        raise _CallerIdentityError(
            "a root user record carries a malformed origin field; caller identity is refused "
            "rather than inferred from an older record")
    text = _text_content(message)
    if (text is None or row.get("isCompactSummary") or row.get("isMeta")
            or row.get("isVisibleInTranscriptOnly") or "toolUseResult" in row
            or row.get("isSidechain") not in (None, False) or row.get("agentId") is not None
            or row.get("agent_id") is not None or _caller_envelope(text)):
        # A tool result, summary, meta or visible-only record, nested worker record or
        # harness envelope is positively not a fresh caller; caller-looking fields on
        # such a record never override that.
        return None
    if isinstance(origin, dict) and origin.get("kind") != "human":
        if any(row.get(name) is not None for name in SDK_CALLER_IDENTITY_KEYS):
            raise _CallerIdentityError(
                "a root user record combines an explicit nonhuman origin with external SDK caller "
                "identity keys; the identity is contradictory and is refused rather than inferred "
                "from an older record")
        # An explicit nonhuman origin is never overridden.
        return None
    if "origin" in row and origin is None:
        raise _CallerIdentityError(
            "a root user record carries an explicit null origin field; the supported caller dialects "
            "use either origin.kind=human or no origin field, so this unobserved shape is refused "
            "rather than inferred from an older record")
    turn_origin = row.get("turnOrigin")
    if _human_caller(row):
        if turn_origin not in (None, SDK_CALLER_TURN_ORIGIN, HUMAN_CALLER_TURN_ORIGIN):
            raise _CallerIdentityError(
                "a caller record combines an origin.kind=human identity with a different turnOrigin; "
                "the identity is contradictory and is refused rather than inferred from an older "
                "record")
        if prompt_id is not None and row.get("promptId") is not None:
            if _prompt_uuid(row.get("promptId")) != prompt_id:
                raise _CallerIdentityError(_prompt_mismatch_error())
        return "human"
    if not any(row.get(name) is not None for name in SDK_CALLER_IDENTITY_KEYS):
        return None
    if turn_origin != SDK_CALLER_TURN_ORIGIN:
        raise _CallerIdentityError(
            "a root user record presents caller identity keys without the external SDK caller "
            "turnOrigin; the identity is refused rather than inferred from an older record")
    _certify_sdk_caller(row, prompt_id)
    return "sdk"


def _account_quota_error(row):
    """Account evidence in a typed API failure; cwd/model identity is checked separately."""
    message = row.get("message")
    if (row.get("type") != "assistant" or row.get("isApiErrorMessage") is not True
            or row.get("error") != "rate_limit" or not isinstance(message, dict)
            or message.get("role") != "assistant"):
        return False
    quota = row.get("quotaLimits")
    if quota is not None:
        # A typed model-specific or unknown window is never promoted to shared
        # account quota by text. Accept only the native rejected account windows.
        return (isinstance(quota, dict) and quota.get("status") == "rejected"
                and quota.get("rateLimitType") in ACCOUNT_QUOTA_WINDOWS)
    text = _text_content(message)
    return isinstance(text, str) and ACCOUNT_QUOTA_TEXT.fullmatch(text) is not None


def inspect_quota(event):
    """Observe current-turn evidence; this neither releases nor edits controller holds.

    Missing fields in old synthetic hook events are explicitly unavailable, not a
    healthy observation. Once native hook metadata is supplied, an incomplete or
    unsafe inspection raises and blocks only an otherwise eligible new submission.
    """
    if not any(key in event for key in QUOTA_HOOK_FIELDS):
        return {"status": "missing_hook_metadata"}
    session_id, path, cwd = (_identity(event, key) for key in QUOTA_HOOK_FIELDS)
    if (not session_id or str(uuid.UUID(session_id)) != session_id
            or not path or not cwd):
        raise ValueError("native quota hook identity is incomplete or invalid")
    if _absolute_parts(path)[-1] != session_id + ".jsonl":
        raise ValueError("native quota transcript does not match the exact root session")
    _absolute_parts(cwd)
    prompt_id = _hook_prompt_id(event)
    raw = _transcript_tail(path)
    if not raw.endswith(b"\n"):
        raise ValueError("native quota transcript has no complete current caller window")
    errors = []
    for count, line in enumerate(reversed(raw.splitlines()), 1):
        if count > MAX_TRANSCRIPT_RECORDS:
            break
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("native quota transcript record is malformed")
        if not _root_session_record(row, session_id):
            continue
        try:
            caller = _caller_kind(row, prompt_id)
        except _CallerIdentityError as exc:
            # A caller-shaped record that cannot be certified refuses the turn; it is
            # never skipped in favour of an older caller record.
            raise ValueError(str(exc))
        if caller is not None and row.get("cwd") == cwd:
            if not isinstance(row.get("uuid"), str) or not row["uuid"]:
                raise ValueError("native quota caller lacks its identity")
            return {"status": "quota_in_current_turn" if errors else "clear_current_turn",
                    "caller_uuid": row["uuid"], "error_uuids": list(reversed(errors))}
        if _account_quota_error(row):
            # Positive account evidence in this exact root session must not be
            # silently cleared because a native identity field changed or is
            # missing. Preserve the ambiguity; do not normalize or guess it.
            if row.get("cwd") != cwd or row["message"].get("model") != "<synthetic>":
                raise ValueError("native account quota evidence has unverified cwd or model identity")
            if not isinstance(row.get("uuid"), str) or not row["uuid"]:
                raise ValueError("native quota error lacks its identity")
            errors.append(row["uuid"])
    raise ValueError("native quota inspection cannot prove the current human caller within its bound")


def _quota_denial(event):
    if inspect_quota(event)["status"] == "quota_in_current_turn":
        return ("native account/session quota was rejected in this human turn; stop new native and provider "
                "submissions and report the pause. Do not escalate, retry, or switch providers to bypass the "
                "quota stop. A later authorized human continuation remains subject to controller outcome holds")
    return None


class _ReadRefused(Exception):
    """A deliberate read-admission refusal that carries a model-visible reason."""


class _ReadCallAbsent(_ReadRefused):
    """The one transient Read outcome: the requesting record is not yet present.

    It carries the verified transcript revision in which the record was absent,
    including a bounded anchor digest over the bytes ending at that revision size when
    the guard observed it. The bounded readiness wait uses that evidence to refuse a
    replacement or a same-inode rewrite instead of treating either as a late append;
    a synthetic or mocked absent without an anchor carries None and cannot establish
    that content evidence. A partial tail, a conflict, staleness, a replacement, a
    rewind and every state or ownership refusal are final and are never retried.
    """

    def __init__(self, reason, device, inode, size, anchor_sha256=None):
        super().__init__(reason)
        self.transcript_device = device
        self.transcript_inode = inode
        self.transcript_size = size
        self.transcript_anchor_sha256 = anchor_sha256


def _read_path_parts(value, what):
    """Validate one absolute POSIX path and return its components."""
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096
            or "\x00" in value or not value.strip()):
        raise _ReadRefused(what + " must be a non-empty absolute path")
    parts = value.split("/")[1:]
    if not parts or len(parts) > 128 or any(part in ("", ".", "..") for part in parts):
        raise _ReadRefused(what + " has unsafe path components")
    return parts


def _read_target_path(file_path, cwd):
    """Return the absolute Read target without normalizing unsafe components away.

    Dot and dot-dot components are refused before any lexical normalization, and a
    relative path is joined to the verified hook cwd without erasing its own
    components, so the guard never measures a target other than the literal path
    the native tool is handed.
    """
    joined = file_path if file_path.startswith("/") else cwd + "/" + file_path
    _read_path_parts(joined, "the Read file path")
    return joined


def _owned_directory(metadata):
    sticky_system = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    return (metadata.st_uid in (0, os.getuid())
            and not (metadata.st_mode & 0o022 and not sticky_system))


def _open_verified_directories(parts):
    """Open a directory chain by components; no component may be a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    try:
        for part in parts:
            child = _open_component(part, flags, directory)
            os.close(directory)
            directory = child
            if not _owned_directory(os.fstat(directory)):
                raise _ReadRefused("the native transcript directory chain is owned by another user or "
                                   "writable by others, so no admission state may be created beside it")
        return directory
    except BaseException:
        os.close(directory)
        raise


def _open_state_directory(transcript_path, session_id):
    """Open or create the private admission namespace for exactly this session.

    Returns the descriptors of the project directory, the hidden admission
    directory and the session directory. Each reached entry is confirmed with a
    parent-directory fsync, because syncing only the session directory cannot
    establish its own newly created entry in its parent.
    """
    parts = _read_path_parts(transcript_path, "the native transcript path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    chain = [_open_verified_directories(parts[:-1])]
    try:
        for name in (READ_STATE_DIR, session_id):
            try:
                os.mkdir(name, 0o700, dir_fd=chain[-1])
            except FileExistsError:
                pass
            child = _open_component(name, flags, chain[-1])
            metadata = os.fstat(child)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                os.close(child)
                raise _ReadRefused("the private read-admission directory is owned by another user or "
                                   "writable by others; no reservation can be recorded")
            chain.append(child)
            _fsync_directory(chain[-2])
        return chain
    except BaseException:
        for fd in reversed(chain):
            os.close(fd)
        raise


def _lock_state(directory):
    """Bounded exclusive lock on a stable owned inode; the file is never removed."""
    if fcntl is None:
        raise _ReadRefused("this platform cannot provide the private admission lock, so no durable "
                           "reservation can be recorded")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(READ_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    except FileExistsError:
        # A single O_CREAT|O_NOFOLLOW open can report ENOENT on macOS while another process
        # creates the same name, so creation is atomic and the loser of that race opens the
        # already-existing lock without O_CREAT (and therefore without the racy lookup).
        try:
            fd = os.open(READ_LOCK_NAME, flags, dir_fd=directory)
        except FileNotFoundError:
            # The name existed a moment ago and this guard never removes it: refuse explicitly
            # rather than retrying over an absent or replaced lock.
            raise _ReadRefused("the private read-admission lock disappeared while it was being "
                               "acquired, so no durable reservation can be recorded")
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
            raise _ReadRefused("the private read-admission lock file is not an owned private regular file")
        deadline = time.monotonic() + READ_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                if time.monotonic() >= deadline:
                    raise _ReadRefused("another process holds the private read-admission lock for this "
                                       "session beyond the bounded wait; retry this bounded read")
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def _message_digest(message_id):
    return hashlib.sha256(b"project-room-read-admission-v1:" + message_id.encode("utf-8")).hexdigest()


def _args_digest(params):
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_digest(row):
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _state_reason(detail):
    return ("the private read-admission state could not be verified (" + detail + "); the read is refused "
            "rather than admitted without a durable reservation. Nothing is deleted or repaired "
            "automatically; report the private admission state for operator review")


def _capacity_reason():
    return ("the private read-admission state for this native session already holds its documented "
            "capacity of " + str(READ_STATE_MAX_RECORDS) + " assistant-message records. No record, "
            "scratch file or other evidence is removed automatically, and no reservation can be "
            "recorded. Treat this as a diagnosed admission-capacity stop: stop here and report the "
            "private admission state for operator review. The current native session and its "
            "retained evidence stay in place; do not replace the session, clear state or delete "
            "evidence to regain allowance")


def _per_read_reason(raw_bytes):
    return ("the requested text read is approximately " + str(raw_bytes)
            + " UTF-8 source bytes, above the " + str(READ_MAX_SELECTED_BYTES)
            + "-byte per-read admission budget. Request explicit line ranges (offset and a smaller "
              "limit) and continue sequentially. This is a local admission estimate, not an exact "
              "token count or tool-result measurement")


def _single_line_reason(raw_bytes):
    return ("a single line in the requested range alone reaches the " + str(READ_MAX_SELECTED_BYTES)
            + "-byte per-read admission budget (at least " + str(raw_bytes)
            + " UTF-8 source bytes here). Reducing the line count cannot make one line fit; report "
              "this oversized line and request a smaller excerpt instead of retrying the same line")


def _range_reason(raw_bytes):
    return ("the selected lines total more than the " + str(READ_MAX_SELECTED_BYTES)
            + "-byte per-read admission budget (at least " + str(raw_bytes)
            + " UTF-8 source bytes). Request fewer lines with an explicit limit and continue "
              "sequentially. This is a local admission estimate, not an exact tool-result measurement")


def _aggregate_reason(raw_total, raw_bytes):
    return ("this native assistant message has already admitted approximately " + str(raw_total)
            + " source bytes of Read results; adding approximately " + str(raw_bytes)
            + " would exceed the " + str(READ_MAX_MESSAGE_BYTES)
            + "-byte per-message admission budget. Request a smaller explicit line range now; a "
              "later native assistant message has its own per-message budget")


def _framing_reason(raw_total, lines_total):
    return ("the estimated line-number framing for this message (" + str(READ_LINE_WRAPPER_MAX_BYTES)
            + " bytes per returned line over " + str(lines_total) + " lines) plus " + str(raw_total)
            + " source bytes exceeds the reserved per-message framing allowance of "
            + str(READ_MAX_MESSAGE_FRAMED_BYTES)
            + " estimated bytes. Request fewer lines with an explicit limit")


def _scan_reason(ahead):
    if ahead:
        return ("the requested range begins after more than the bounded " + str(READ_MAX_SCAN_BYTES)
                + "-byte inspection window, so the guard cannot verify that offset. No smaller line "
                  "count reaches this region: read lines inside the inspected prefix of the file, or "
                  "report the beyond-window region for an authorized smaller excerpt")
    return ("the requested read continues past the bounded " + str(READ_MAX_SCAN_BYTES)
            + "-byte inspection window. Add an explicit limit that ends inside that window (earlier "
              "lines stay verifiable), or report the remaining region for an authorized smaller excerpt")


def _absent_reason():
    """Inner text for the one transient outcome; the bounded wait replaces it.

    It is never shown by itself: the readiness wrapper either admits the call after a
    late append or raises the accurate expiry reason below.
    """
    return ("the requesting root-session assistant record was absent from the verified native "
            "transcript revision at this bounded check")


def _readiness_expired_reason(checks, seconds):
    return ("the current Read call could not be positively correlated to a root-session assistant "
            "record in " + str(checks) + " bounded checks over at most " + str(seconds) + " seconds "
            "inside this single hook invocation. The hook contract does not guarantee that the "
            "requesting assistant record is already flushed when the hook runs, so the guard waits "
            "briefly for a late append; the record did not appear in this bounded window and wait. "
            "Either the record was not yet written in this native session transcript, or it lies "
            "before the bounded 2 MiB inspected window (for example after a large earlier tool "
            "result). No identity and no budget are granted without that correlation, and no evidence "
            "is cleared or repaired. A later attempt may still fail for the same reason or may see a "
            "flushed record, so do not re-issue this call in a loop; stop and report this refusal "
            "together with the private admission state for operator review. Continue only in a later "
            "native assistant message when work continues")


def _waited_revision_reason(detail):
    return ("the native transcript " + detail + " while the guard was waiting for the requesting "
            "assistant record inside this hook, so this Read call is refused rather than correlated "
            "from a changed transcript revision. Treat this as a diagnosed state stop: report the "
            "native transcript and the private admission state for operator review, and keep the "
            "current native session and its evidence in place instead of replacing or clearing it")


def _require_waited_anchor(transcript_path, latest):
    """Confirm the bounded previous-content evidence for the latest absent revision."""
    try:
        current = _transcript_anchor_digest(transcript_path, latest[0], latest[1], latest[2],
                                            READ_CONTINUITY_ANCHOR_BYTES)
    except ValueError:
        raise _ReadRefused(_waited_revision_reason(
            "changed before its bounded previous-content evidence could be verified"))
    if current != latest[3]:
        raise _ReadRefused(_waited_revision_reason(
            "no longer matches its bounded previous-content evidence"))


def _partial_tail_reason():
    return ("the bounded native transcript window ends with a record that is still incomplete, so a "
            "later native caller or assistant message may exist beyond the verified call even when "
            "the call itself matched. Retry this same bounded read after the native record is "
            "flushed. No identity or budget is granted over an uncertain tail")


def _changed_reason():
    return ("the native transcript changed between the guard's inspection and the reservation "
            "transaction, so this Read call cannot inherit the inspected clearance. Re-issuing the "
            "same read against an advanced transcript is refused as stale, so continue in the latest "
            "native assistant message rather than repeating this call in a loop")


def _rewind_reason():
    return ("the bounded per-session continuity record shows the native transcript was rewritten, "
            "truncated or changed at a previously verified position, so no new read clearance is "
            "granted from this transcript state. Treat this as a diagnosed state stop: report the "
            "native transcript and the private admission state for operator review, and keep the "
            "current native session and its evidence in place instead of replacing or clearing it")


def _replacement_reason():
    return ("the native transcript device or inode identity no longer matches the bounded "
            "per-session continuity record, so this read is refused rather than granted from a "
            "replaced history. Treat this as a diagnosed state stop: report the native transcript "
            "and the private admission state for operator review, and keep the current native "
            "session and its evidence in place instead of replacing or clearing it")


def _missing_ledger_reason():
    return ("the per-session continuity record still binds at least one tool identity to this "
            "native assistant message, but that message's reservation ledger is missing. A lost "
            "ledger is indistinguishable from an interrupted first reservation, and the bytes an "
            "earlier admission charged cannot be reconstructed from the remaining evidence, so "
            "this read is refused rather than admitted into a fresh per-message allowance. "
            "Nothing is deleted, rebuilt or refunded automatically. This stop is message-scoped: "
            "after operator review, remaining work may continue in a later native assistant "
            "message in the same native session, which has its own legitimate per-message budget; "
            "do not reset or heal this message or delete its evidence. Report the private "
            "admission state for operator review")


def _missing_continuity_reason():
    return ("the private read-admission directory for this session already holds at least one "
            "message reservation ledger, but the per-session continuity and tool-identity record "
            "is missing. Without it the transcript and tool-identity protections cannot be "
            "re-established from evidence, so no new clearance is granted and no allowance is "
            "reset. Nothing is deleted or rebuilt automatically; report the private admission "
            "state for operator review")


def _directory_growth_reason(entries, growth):
    more = "entry" if growth == 1 else "entries"
    return ("the private read-admission directory for this native session already holds "
            + str(entries) + " of its documented bound of " + str(READ_STATE_MAX_ENTRIES)
            + " entries, and recording this reservation needs room for " + str(growth) + " more "
            + more + ", counting the temporary scratch entry its atomic write uses. The read is "
            + "refused rather than growing the bounded private state past its documented bound. "
            + "No entry is removed automatically; report the private admission state for "
            + "operator review")


def _binding_reason():
    return ("this native tool identity is durably bound to a different native assistant message, "
            "so it is refused as a reused, replayed or rewound identity. No new budget is opened "
            "for it")


def _binding_capacity_reason():
    return ("the per-session tool-identity binding table already holds its documented capacity of "
            + str(READ_STATE_MAX_BINDINGS) + " tool identities. No binding is removed automatically "
            "and no new clearance can be recorded. Treat this as a diagnosed admission-capacity "
            "stop: stop here and report the private admission state for operator review. The current "
            "native session and its retained evidence stay in place; do not replace the session, "
            "clear state or delete evidence to regain allowance")


def _container_reason(detail):
    return ("the Read target is a known non-text container form (" + detail + "), which this "
            "admission does not cover: image, PDF, notebook, archive and office reads are refused "
            "before any content is admitted. Request a plain UTF-8 text file, or report the needed "
            "region for an authorized extraction outside Read admission")


def _text_reason():
    return ("the Read target is not verifiably plain UTF-8 text: the inspected bytes contain a NUL "
            "byte or a sequence that is not valid UTF-8, so this binary or corrupted file is "
            "refused rather than admitted and charged as text. Request a plain text file")


def _stale_reason():
    return ("a later native assistant message or human turn already exists after this Read call, so "
            "this hook is stale and cannot open or reuse a budget. Do not retry it here; continue the "
            "work in the latest native message")


def _bounded_int(value, maximum):
    if type(value) is not int or value < 0 or value > maximum:
        raise _ReadRefused(_state_reason("the reservation record contains a boolean, non-integer or "
                                         "out-of-range value"))
    return value


def _validate_record(record):
    """Refuse any partial, inconsistent or unexpected reservation record."""
    if not isinstance(record, dict) or set(record) != READ_RECORD_KEYS:
        raise _ReadRefused(_state_reason("the reservation record does not have its exact documented "
                                         "fields"))
    if type(record["version"]) is not int or record["version"] != READ_ADMISSION_VERSION:
        raise _ReadRefused(_state_reason("the reservation record version is not supported"))
    if not isinstance(record["session_id"], str) or not isinstance(record["message_id_sha256"], str):
        raise _ReadRefused(_state_reason("the reservation record identity is malformed"))
    entries = record["entries"]
    if not isinstance(entries, dict) or not entries or len(entries) > READ_BATCH_MAX_ENTRIES:
        raise _ReadRefused(_state_reason("the reservation record entry table is inconsistent"))
    total_raw = _bounded_int(record["total_raw_bytes"], READ_MAX_MESSAGE_BYTES)
    total_lines = _bounded_int(record["total_lines"],
                               READ_MAX_MESSAGE_FRAMED_BYTES // READ_LINE_WRAPPER_MAX_BYTES)
    _bounded_int(record["transcript_device"], 2 ** 64)
    _bounded_int(record["transcript_inode"], 2 ** 64)
    raw_sum = 0
    line_sum = 0
    for key, entry in entries.items():
        if (not isinstance(key, str) or not key or len(key) > 256
                or any(ord(character) < 33 for character in key)
                or not isinstance(entry, dict) or set(entry) != READ_ENTRY_KEYS):
            raise _ReadRefused(_state_reason("the reservation record contains a malformed entry"))
        if not isinstance(entry["args_sha256"], str) or READ_DIGEST.fullmatch(entry["args_sha256"]) is None:
            raise _ReadRefused(_state_reason("a reservation entry lacks its argument identity"))
        raw_sum += _bounded_int(entry["raw_bytes"], READ_MAX_SELECTED_BYTES)
        line_sum += _bounded_int(entry["lines"],
                                 READ_MAX_MESSAGE_FRAMED_BYTES // READ_LINE_WRAPPER_MAX_BYTES)
        for field in ("device", "inode", "size", "mtime_ns", "ctime_ns"):
            _bounded_int(entry[field], 2 ** 64)
    if (raw_sum != total_raw or line_sum != total_lines
            or total_raw + READ_LINE_WRAPPER_MAX_BYTES * total_lines > READ_MAX_MESSAGE_FRAMED_BYTES):
        raise _ReadRefused(_state_reason("the reservation record totals do not match its entries"))


def _load_state_json(session_fd, name, maximum):
    """Load one bounded private JSON record; None when it does not exist."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=session_fd)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_nlink != 1
                or metadata.st_size <= 0 or metadata.st_size > maximum):
            raise _ReadRefused(_state_reason("the private admission file is not an owned private "
                                             "regular file of bounded size"))
        raw = os.read(fd, maximum + 1)
        if len(raw) != metadata.st_size:
            raise _ReadRefused(_state_reason("the private admission file changed while it was read"))
    finally:
        os.close(fd)
    try:
        record = json.loads(raw)
    except ValueError:
        raise _ReadRefused(_state_reason("the private admission file is not valid JSON"))
    return record


def _read_record(session_fd, name):
    """Load and fully validate one message reservation record; None when it is absent."""
    record = _load_state_json(session_fd, name, READ_RECORD_MAX_BYTES)
    if record is None:
        return None
    _validate_record(record)
    return record


def _read_session_record(session_fd):
    """Load the bounded per-session continuity record; None when it is absent."""
    return _load_state_json(session_fd, READ_SESSION_NAME, READ_SESSION_MAX_BYTES)


def _validate_session_record(record, session_id, transcript_device, transcript_inode):
    """Validate one continuity record and return (bindings, size, anchor)."""
    if not isinstance(record, dict) or set(record) != READ_SESSION_KEYS:
        raise _ReadRefused(_state_reason("the per-session continuity record does not have its exact "
                                         "documented fields"))
    if type(record["version"]) is not int or record["version"] != READ_ADMISSION_VERSION:
        raise _ReadRefused(_state_reason("the per-session continuity record version is not supported"))
    if record["session_id"] != session_id:
        raise _ReadRefused(_state_reason("the per-session continuity record belongs to another "
                                         "session"))
    device = _bounded_int(record["transcript_device"], 2 ** 64)
    inode = _bounded_int(record["transcript_inode"], 2 ** 64)
    if device != transcript_device or inode != transcript_inode:
        raise _ReadRefused(_replacement_reason())
    size = _bounded_int(record["transcript_size"], 2 ** 64)
    if (not isinstance(record["anchor_sha256"], str)
            or READ_DIGEST.fullmatch(record["anchor_sha256"]) is None):
        raise _ReadRefused(_state_reason("the per-session continuity record lacks its transcript "
                                         "anchor digest"))
    bindings = record["bindings"]
    if not isinstance(bindings, dict) or len(bindings) > READ_STATE_MAX_BINDINGS:
        raise _ReadRefused(_state_reason("the tool-identity binding table is inconsistent"))
    for key, value in bindings.items():
        if (not isinstance(key, str) or not key or len(key) > 256
                or any(ord(character) < 33 for character in key)
                or not isinstance(value, str) or READ_BINDING_VALUE.fullmatch(value) is None):
            raise _ReadRefused(_state_reason("the tool-identity binding table contains a malformed "
                                             "entry"))
    return bindings, size, record["anchor_sha256"]


def _fsync_directory(directory):
    """Confirm a directory-entry change is durable; every failure refuses.

    Some platforms cannot fsync a directory descriptor at all. That is not a
    licence to admit on unconfirmed storage: without directory durability the
    reservation could disappear after a crash, so the read is refused with the
    platform's own error instead of being granted on faith.
    """
    try:
        os.fsync(directory)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
        raise _ReadRefused("the platform could not confirm directory durability for the private "
                           "read-admission state (fsync failed with " + (code or str(exc.errno))
                           + "), so no durable reservation can be recorded here and the read is "
                             "refused. This is a storage or capability stop, not an assumption that "
                             "the reservation exists")


def _serialize(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _write_state_record(session_fd, name, raw):
    """Write one bounded private record durably; scratch names are never reused."""
    scratch = READ_SCRATCH_PREFIX + uuid.uuid4().hex
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
                 | os.O_CLOEXEC, 0o600, dir_fd=session_fd)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
            raise _ReadRefused(_state_reason("the reservation scratch file is not an owned private "
                                             "regular file"))
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining[:65536])
            if written <= 0:
                raise OSError("read-admission state write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(scratch, name, src_dir_fd=session_fd, dst_dir_fd=session_fd)
    _fsync_directory(session_fd)


def _write_record(session_fd, name, record):
    """Write one reservation record durably before admission."""
    raw = _serialize(record)
    if len(raw) > READ_RECORD_MAX_BYTES:
        raise _ReadRefused("the bounded reservation record for this native assistant message cannot "
                           "hold another reservation within its documented size limit; continue in a "
                           "later assistant message with explicit smaller ranges")
    _write_state_record(session_fd, name, raw)


def _write_session_record(session_fd, session_id, device, inode, size, anchor, bindings):
    """Write the bounded per-session continuity and tool-identity binding record.

    It is written before any message record that claims a new tool identity. An
    interrupted attempt therefore leaves a durable binding whose ledger was never
    committed; that state is indistinguishable from a ledger lost after an accepted
    reservation, so a retry is refused with the evidence preserved rather than
    completing or reopening the reservation.
    """
    raw = _serialize({"version": READ_ADMISSION_VERSION, "session_id": session_id,
                      "transcript_device": device, "transcript_inode": inode,
                      "transcript_size": size, "anchor_sha256": anchor, "bindings": bindings})
    if len(raw) > READ_SESSION_MAX_BYTES:
        raise _ReadRefused("the bounded per-session continuity record cannot hold another "
                           "tool-identity binding within its documented size limit. Treat this as "
                           "a diagnosed admission-capacity stop: stop here and report the private "
                           "admission state for operator review. The current native session and its "
                           "retained evidence stay in place; do not replace the session, clear state "
                           "or delete evidence to regain allowance")
    _write_state_record(session_fd, READ_SESSION_NAME, raw)


def _state_entry_names(directory_fd):
    """Names of the private admission directory, with an early stop at its bound."""
    try:
        scanner = os.scandir(directory_fd)
    except TypeError:
        names = os.listdir(directory_fd)
        if len(names) > READ_STATE_MAX_ENTRIES:
            raise _ReadRefused(_state_reason("the private admission directory holds more entries than "
                                             "its documented bound"))
        return names
    names = []
    try:
        for entry in scanner:
            names.append(entry.name)
            if len(names) > READ_STATE_MAX_ENTRIES:
                raise _ReadRefused(_state_reason("the private admission directory holds more entries "
                                                 "than its documented bound"))
    finally:
        scanner.close()
    return names


def _require_state_inventory(directory_fd, needs_record):
    """Validate the bounded private inventory under the lock; evidence is never pruned.

    Returns the number of message reservation ledgers and the exact number of
    directory entries observed, so a later growth decision can count new
    persistent entries and the live scratch entry its atomic write occupies
    against the same bound.
    """
    entries = 0
    records = 0
    for name in _state_entry_names(directory_fd):
        entries += 1
        if name in (READ_LOCK_NAME, READ_SESSION_NAME) or name.startswith(READ_SCRATCH_PREFIX):
            continue
        if READ_RECORD_NAME.fullmatch(name) is None:
            raise _ReadRefused(_state_reason("the private admission directory contains an entry the "
                                             "guard does not recognize"))
        records += 1
    if needs_record and records >= READ_STATE_MAX_RECORDS:
        raise _ReadRefused(_capacity_reason())
    return records, entries


def _utf8_scan(state, raw):
    """Advance a strict UTF-8 text check over one chunk of bytes.

    ``state`` carries the pending multi-byte sequence so a character split by a
    chunk boundary is never mistaken for malformed text. NUL bytes are refused as
    binary markers; overlong encodings, surrogates and values above U+10FFFF are
    refused as invalid UTF-8.
    """
    need, code, lower = state
    for byte in raw:
        if need:
            if byte < 0x80 or byte > 0xBF:
                return False
            code = (code << 6) | (byte & 0x3F)
            need -= 1
            if not need and (code < lower or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF):
                return False
        elif byte == 0:
            return False
        elif byte < 0x80:
            continue
        elif 0xC2 <= byte <= 0xDF:
            need, code, lower = 1, byte & 0x1F, 0x80
        elif 0xE0 <= byte <= 0xEF:
            need, code, lower = 2, byte & 0x0F, 0x800
        elif 0xF0 <= byte <= 0xF4:
            need, code, lower = 3, byte & 0x07, 0x10000
        else:
            return False
    state[0], state[1], state[2] = need, code, lower
    return True


def _scan_selection(fd, size, first, last):
    """Measure the requested line range without scanning past its end.

    The estimate covers exactly the requested 1-based line range, or every
    remaining line when no limit is given, so every denial has a smaller range
    as a real remedy. The scanned byte window is bounded, so a prefix beyond it
    is refused truthfully instead of being reported as a missing step. Every
    inspected byte must also positively look like plain UTF-8 text: a NUL byte or
    an invalid sequence refuses the target instead of charging it as text.
    """
    position = 0
    number = 1
    raw_bytes = 0
    lines = 0
    final_byte = None
    state = [0, 0, 0]
    while position < size:
        if position >= READ_MAX_SCAN_BYTES:
            return None, None, _scan_reason(number < first)
        chunk = os.pread(fd, min(READ_SCAN_CHUNK_BYTES, size - position), position)
        if not chunk:
            break
        if not _utf8_scan(state, chunk):
            return None, None, _text_reason()
        final_byte = chunk[-1]
        index = 0
        while index < len(chunk):
            newline = chunk.find(b"\n", index)
            end = len(chunk) if newline < 0 else newline + 1
            if first <= number and (last is None or number <= last):
                raw_bytes += end - index
                if raw_bytes > READ_MAX_SELECTED_BYTES:
                    if lines == 0:
                        return None, None, _single_line_reason(raw_bytes)
                    return None, None, _range_reason(raw_bytes)
                if newline >= 0:
                    lines += 1
            if newline < 0:
                index = len(chunk)
            else:
                number += 1
                index = newline + 1
                if last is not None and number > last:
                    return raw_bytes, lines, None
        position += len(chunk)
    if state[0]:
        return None, None, _text_reason()
    if (final_byte is not None and final_byte != 10 and first <= number
            and (last is None or number <= last)):
        lines += 1
    return raw_bytes, lines, None


def _container_form(name, head):
    """Return a description when the target is a known non-text container form."""
    lowered = name.lower()
    for suffix in READ_CONTAINER_SUFFIXES:
        if lowered.endswith(suffix):
            return "the file name ends in " + suffix
    for magic, label in READ_CONTAINER_MAGIC:
        if head.startswith(magic):
            return "the leading bytes are a " + label
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "the leading bytes are a WebP image"
    return None


def _measure_read(path, offset, limit):
    """Conservative pre-read estimate for one coverable plain text Read.

    Coverage is decided positively: the leaf must not be a known auto-detected
    container form and the inspected bytes must be strict UTF-8 without NUL bytes.
    """
    parts = _read_path_parts(path, "the Read file path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    leaf_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    directory = os.open("/", directory_flags)
    leaf = None
    try:
        for part in parts[:-1]:
            child = _open_component(part, directory_flags, directory)
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            sticky_system = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            if (metadata.st_uid not in (0, os.getuid())
                    or metadata.st_mode & 0o022 and not sticky_system):
                raise _ReadRefused("the Read path has a parent directory owned by another user or "
                                   "writable by others; symlinked, shared and unsafe parents are refused")
        leaf = _open_component(parts[-1], leaf_flags, directory)
        before = os.fstat(leaf)
        if not stat.S_ISREG(before.st_mode):
            raise _ReadRefused("the requested path is not a directly openable regular file (symlinked "
                               "targets, directories, FIFOs, devices and sockets are refused); this "
                               "admission covers plain text files only")
        head = os.pread(leaf, 16, 0)
        container = _container_form(parts[-1], head)
        if container is not None:
            raise _ReadRefused(_container_reason(container))
        if offset is None and limit is None and before.st_size > READ_MAX_SELECTED_BYTES:
            raise _ReadRefused(_per_read_reason(before.st_size))
        first = 1 if offset is None else offset
        last = None if limit is None else first + limit - 1
        raw_bytes, lines, refusal = _scan_selection(leaf, before.st_size, first, last)
        if refusal is not None:
            raise _ReadRefused(refusal)
        after = os.fstat(leaf)
        if _file_identity(before) != _file_identity(after):
            raise _ReadRefused("the file changed while the guard measured it; the read is refused "
                               "rather than admitted with a stale estimate. Request the bounded range "
                               "again")
        return {"raw_bytes": raw_bytes, "lines": lines, "device": before.st_dev, "inode": before.st_ino,
                "size": before.st_size, "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns}
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(directory)


def _root_human_row(row, session_id, cwd):
    """A certified root-session caller of either supported dialect, or None.

    Both the older origin.kind=human dialect and the positively identified external
    SDK caller dialect count here, so a later external SDK caller makes an older Read
    stale exactly like a later origin.kind=human caller. Raises _CallerIdentityError
    for a caller-shaped record whose identity is contradictory; the Read path refuses
    that instead of skipping past it.
    """
    if (row.get("sessionId") != session_id or row.get("cwd") != cwd
            or row.get("isSidechain") not in (None, False) or row.get("agentId") is not None
            or row.get("agent_id") is not None):
        return None
    return _caller_kind(row)


def _read_message_identity(transcript_path, session_id, cwd, tool_use_id, params):
    """Return (message_id, transcript_device, transcript_inode) for this exact call."""
    raw, device, inode, size, anchor = _transcript_window(transcript_path, READ_TRANSCRIPT_WINDOW_BYTES)
    lines = raw.split(b"\n")
    if lines and lines[-1] != b"":
        # A later partial record may change caller authority or reveal a newer
        # native assistant message, so it refuses even when this call matched.
        raise _ReadRefused(_partial_tail_reason())
    seen_rows = {}
    matched = False
    message_id = None
    stale = False
    count = 0
    for line in lines:
        if not line.strip():
            continue
        count += 1
        if count > READ_MAX_TRANSCRIPT_RECORDS:
            raise _ReadRefused("the bounded native transcript window holds more records than the guard "
                               "will inspect, so the current assistant message cannot be verified")
        try:
            row = json.loads(line)
        except ValueError:
            raise _ReadRefused("the bounded native transcript window contains a record that is not valid "
                               "JSON, so the current assistant message cannot be verified")
        if not isinstance(row, dict):
            raise _ReadRefused("the bounded native transcript window contains a record that is not a JSON "
                               "object")
        if not (row.get("type") == "assistant" and row.get("sessionId") == session_id
                and row.get("isSidechain") in (None, False)
                and row.get("agentId") is None and row.get("agent_id") is None
                and row.get("cwd") == cwd):
            if matched:
                try:
                    later_caller = _root_human_row(row, session_id, cwd)
                except _CallerIdentityError as exc:
                    # A later caller-shaped record whose identity is contradictory is
                    # refused here rather than skipped, because skipping it could admit
                    # a read from an older turn.
                    raise _ReadRefused(str(exc))
                if later_caller is not None:
                    stale = True
            continue
        identity = row.get("uuid")
        if isinstance(identity, str) and identity:
            digest = _row_digest(row)
            if identity in seen_rows and seen_rows[identity] != digest:
                raise _ReadRefused("the bounded native transcript window contains conflicting duplicate "
                                   "root assistant records with the same identity")
            seen_rows[identity] = digest
        if row.get("isApiErrorMessage") is True:
            if matched:
                stale = True
            continue
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise _ReadRefused("a root assistant record in the bounded transcript window is not an "
                               "assistant message")
        row_message_id = message.get("id")
        if not isinstance(row_message_id, str) or not row_message_id.strip():
            raise _ReadRefused("a root assistant record in the bounded transcript window lacks its native "
                               "message identity")
        found = False
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                if item.get("id") != tool_use_id:
                    continue
                if item.get("name") != "Read":
                    raise _ReadRefused("the native transcript records this tool identity for a different "
                                       "tool, so the Read call cannot be verified")
                if item.get("input") != params:
                    raise _ReadRefused("the native transcript records different Read arguments for this "
                                       "tool identity than the hook event carries, so the call is refused "
                                       "as a conflicting duplicate")
                found = True
        if found:
            if matched and message_id != row_message_id:
                raise _ReadRefused("this tool identity appears in more than one native assistant message")
            if not matched:
                matched = True
                message_id = row_message_id
                stale = False
            continue
        if matched and row_message_id != message_id:
            stale = True
    if not matched:
        raise _ReadCallAbsent(_absent_reason(), device, inode, size, anchor)
    if stale:
        raise _ReadRefused(_stale_reason())
    return message_id, device, inode, size


def _read_message_identity_with_readiness(transcript_path, session_id, cwd, tool_use_id, params):
    """Bounded foreground wait for a late native flush, then fail closed.

    Only the specific transient outcome - the requesting root assistant record is not
    present in a complete, verified transcript revision - is retried. Every other
    outcome (a partial tail, a conflicting duplicate, staleness, a replacement, a
    rewind, malformed or missing state, an ownership failure) is final and is returned
    immediately without waiting. Every attempt re-inspects the owned transcript from
    scratch, so ownership, the exact tool id/name/arguments, the transcript identity,
    its size and the completeness of its tail are all revalidated on the revision that
    is finally accepted; the reservation transaction then revalidates everything again
    under the lock. Nothing is written before a positive identity.

    The wait remembers the latest verified absent revision, not only the first: its
    device, inode, size and the bounded anchor digest of the bytes ending at that size
    are carried forward. A later attempt is accepted only when the same device and
    inode still carry at least that many bytes and the bounded previous-content
    evidence still matches, so a grow-then-rewind or a same-inode rewrite that only
    increases the apparent size is refused before any ledger is created. This is
    bounded evidence over the latest verified revision, not whole-history integrity.
    """
    deadline = time.monotonic() + READ_CORRELATION_WAIT_SECONDS
    latest = None
    checks = 0
    while True:
        # Re-check the wall-clock wait deadline before a resumed attempt, so a sleep
        # or slow filesystem observation that returns after the deadline cannot be
        # admitted by the next positive read. The check is necessarily after the
        # fact: Python's monotonic clock cannot preempt a kernel or filesystem stall.
        if checks and time.monotonic() >= deadline:
            raise _ReadRefused(_readiness_expired_reason(checks, READ_CORRELATION_WAIT_SECONDS))
        checks += 1
        try:
            identity = _read_message_identity(transcript_path, session_id, cwd, tool_use_id, params)
        except _ReadCallAbsent as exc:
            revision = (exc.transcript_device, exc.transcript_inode, exc.transcript_size)
            anchor = exc.transcript_anchor_sha256
            if latest is None:
                latest = (revision[0], revision[1], revision[2], anchor)
            else:
                if revision[:2] != latest[:2]:
                    raise _ReadRefused(_waited_revision_reason(
                        "was replaced: its device or inode identity changed"))
                if revision[2] < latest[2]:
                    raise _ReadRefused(_waited_revision_reason(
                        "was truncated or rewritten below the revision already inspected"))
                if latest[3] is not None:
                    _require_waited_anchor(transcript_path, latest)
                latest = (revision[0], revision[1], revision[2], anchor)
            if checks >= READ_CORRELATION_ATTEMPTS or time.monotonic() >= deadline:
                raise _ReadRefused(_readiness_expired_reason(checks, READ_CORRELATION_WAIT_SECONDS))
            time.sleep(max(0.0, min(READ_CORRELATION_SLEEP_SECONDS, deadline - time.monotonic())))
        else:
            if latest is not None:
                if identity[1:3] != latest[:2]:
                    raise _ReadRefused(_waited_revision_reason(
                        "was replaced: its device or inode identity changed"))
                if identity[3] < latest[2]:
                    raise _ReadRefused(_waited_revision_reason(
                        "was truncated or rewritten below the revision already inspected"))
                if latest[3] is not None:
                    _require_waited_anchor(transcript_path, latest)
                if time.monotonic() >= deadline:
                    raise _ReadRefused(_readiness_expired_reason(
                        checks, READ_CORRELATION_WAIT_SECONDS))
            return identity


def _reserve_read(transcript_path, session_id, cwd, tool_use_id, params, message_id, transcript_device,
                  transcript_inode, estimate):
    """Record or reuse one durable reservation; None admits, text refuses.

    The native correlation, the per-session continuity record and the tool-identity
    binding are all revalidated under the reservation lock, so a transcript that
    advanced, became incomplete, was rewritten or was replaced after the earlier
    inspection cannot inherit that clearance.
    """
    digest = _message_digest(message_id)
    name = digest + ".json"
    args_digest = _args_digest(params)
    binding_value = digest + ":" + args_digest
    chain = _open_state_directory(transcript_path, session_id)
    session_fd = chain[-1]
    try:
        lock_fd = _lock_state(session_fd)
        try:
            try:
                rechecked = _read_message_identity(transcript_path, session_id, cwd, tool_use_id,
                                                   params)
            except _ReadCallAbsent:
                # The positive correlation was established outside the lock moments
                # earlier; its disappearance here means the transcript changed under the
                # reservation transaction. The readiness wait is deliberately not used
                # under the lock.
                raise _ReadRefused(_changed_reason())
            if rechecked[:3] != (message_id, transcript_device, transcript_inode):
                raise _ReadRefused(_changed_reason())
            rechecked_size = rechecked[3]
            session_record = _read_session_record(session_fd)
            if session_record is None:
                bindings = {}
                stored_size = None
                stored_anchor = None
            else:
                bindings, stored_size, stored_anchor = _validate_session_record(
                    session_record, session_id, transcript_device, transcript_inode)
                if rechecked_size < stored_size:
                    raise _ReadRefused(_rewind_reason())
                if (_transcript_anchor_digest(transcript_path, transcript_device, transcript_inode,
                                              stored_size, READ_CONTINUITY_ANCHOR_BYTES)
                        != stored_anchor):
                    raise _ReadRefused(_rewind_reason())
            record = _read_record(session_fd, name)
            records, entries = _require_state_inventory(session_fd, record is None)
            if session_record is None and records:
                # A ledger whose continuity record is gone cannot be revalidated, and
                # silently rebuilding the lost identity protection would grant a fresh
                # allowance for a rewritten, reused or replaced transcript identity.
                raise _ReadRefused(_missing_continuity_reason())
            if record is None:
                if any(value.startswith(digest + ":") for value in bindings.values()):
                    # A durable binding for this native message without its ledger is
                    # ambiguous: a lost accepted ledger cannot be told apart from an
                    # unfinished first reservation, and the bytes an accepted admission
                    # charged cannot be reconstructed from the surviving evidence.
                    raise _ReadRefused(_missing_ledger_reason())
                record = {"version": READ_ADMISSION_VERSION, "session_id": session_id,
                          "message_id_sha256": digest, "transcript_device": transcript_device,
                          "transcript_inode": transcript_inode, "total_raw_bytes": 0, "total_lines": 0,
                          "entries": {}}
            elif (record["session_id"] != session_id or record["message_id_sha256"] != digest
                    or record["transcript_device"] != transcript_device
                    or record["transcript_inode"] != transcript_inode):
                raise _ReadRefused(_state_reason("the reservation record does not match this session, "
                                                 "native message or transcript identity"))
            bound = bindings.get(tool_use_id)
            if bound is not None and bound != binding_value:
                raise _ReadRefused(_binding_reason())
            entry = record["entries"].get(tool_use_id)
            new_entry = entry is None
            total_raw = record["total_raw_bytes"]
            total_lines = record["total_lines"]
            if new_entry:
                if bound is None and len(bindings) >= READ_STATE_MAX_BINDINGS:
                    raise _ReadRefused(_binding_capacity_reason())
                if len(record["entries"]) >= READ_BATCH_MAX_ENTRIES:
                    raise _ReadRefused("the bounded reservation record for this native assistant "
                                       "message already holds its documented maximum of "
                                       + str(READ_BATCH_MAX_ENTRIES) + " admitted Read calls; "
                                         "continue in a later assistant message with explicit smaller "
                                         "ranges")
                total_raw += estimate["raw_bytes"]
                total_lines += estimate["lines"]
                if total_raw > READ_MAX_MESSAGE_BYTES:
                    raise _ReadRefused(_aggregate_reason(record["total_raw_bytes"],
                                                         estimate["raw_bytes"]))
                if total_raw + READ_LINE_WRAPPER_MAX_BYTES * total_lines > READ_MAX_MESSAGE_FRAMED_BYTES:
                    raise _ReadRefused(_framing_reason(total_raw, total_lines))
            else:
                if bound is None:
                    raise _ReadRefused(_state_reason("a reservation entry has no durable "
                                                     "tool-identity binding, so the interrupted state "
                                                     "is refused instead of being treated as a "
                                                     "completed reservation"))
                if entry["args_sha256"] != args_digest:
                    raise _ReadRefused("the same native tool identity now carries different Read "
                                       "arguments, so the call is refused as a conflicting duplicate")
                if any(entry[field] != estimate[field] for field in READ_ENTRY_KEYS - {"args_sha256"}):
                    raise _ReadRefused("the target file or its measured extent changed after this call "
                                       "was already reserved; re-issue a fresh bounded read so a new "
                                       "estimate is recorded")
            if stored_size is None or stored_size != rechecked_size:
                anchor = _transcript_anchor_digest(transcript_path, transcript_device,
                                                   transcript_inode, rechecked_size,
                                                   READ_CONTINUITY_ANCHOR_BYTES)
            else:
                anchor = stored_anchor
            write_session = (stored_size is None or stored_size != rechecked_size
                             or (new_entry and bound is None))
            # Count the persistent entries this attempt can add and the one live
            # scratch entry its atomic write occupies against the bounded directory
            # inventory before writing anything, so the documented entry bound is
            # never crossed even transiently. A first continuity record is written
            # before the ledger, so it is a persistent addition of its own.
            growth = (1 if session_record is None else 0) + (1 if write_session or new_entry else 0)
            if entries + growth > READ_STATE_MAX_ENTRIES:
                raise _ReadRefused(_directory_growth_reason(entries, growth))
            if new_entry:
                record["entries"][tool_use_id] = dict(estimate, args_sha256=args_digest)
                record["total_raw_bytes"] = total_raw
                record["total_lines"] = total_lines
                if bound is None:
                    bindings[tool_use_id] = binding_value
            wrote = False
            if write_session:
                _write_session_record(session_fd, session_id, transcript_device, transcript_inode,
                                      rechecked_size, anchor, bindings)
                wrote = True
            if new_entry:
                _write_record(session_fd, name, record)
                wrote = True
            if not wrote:
                # Nothing changed, but an idempotent retry still re-confirms the
                # directory durability instead of trusting an earlier rename whose
                # directory fsync failed.
                _fsync_directory(session_fd)
            return None
        finally:
            os.close(lock_fd)
    finally:
        for fd in reversed(chain):
            os.close(fd)


def _os_failure_reason(exc):
    """Truthful errno-based text for a failed path operation, with no path text.

    ``os.open`` and the private-state file operations carry an errno that
    distinguishes a missing path from a symlinked component, a non-directory
    component or an unreadable path. The exception's filename and string form are
    deliberately never used, so the denial cannot echo private paths or
    transcript content.
    """
    code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    if exc.errno == errno.ELOOP:
        condition = ("a symlinked or looping path component cannot be followed; symlinked Read "
                     "targets are outside this admission's plain-file coverage")
    elif exc.errno == errno.ENOENT:
        condition = "a required path or one of its parent directories does not exist"
    elif exc.errno == errno.ENOTDIR:
        condition = "a component of a required path is not a directory"
    elif exc.errno == errno.EACCES:
        condition = "a required path cannot be read or written by this process"
    else:
        condition = "a required file operation failed"
    return ("the call could not be verified or durably reserved because " + condition
            + " (" + (code or ("errno " + str(exc.errno) if exc.errno is not None else "no errno"))
            + "). A smaller line range cannot change path access, so request an existing directly "
              "readable plain text regular file, or report the target and private admission state "
              "for operator review. No path text is echoed here. Ordinary permission rules still "
              "decide file access")


def _read_denial(event, params):
    """Apply bounded admission to one root engineer Read; never grants permission."""
    try:
        unsupported = sorted(str(key) for key in params if key not in READ_ALLOWED_FIELDS)
        if unsupported:
            raise _ReadRefused("this Read form is not covered by text admission: the field(s) "
                               + ", ".join(unsupported) + " are unsupported and PDF/page, image, "
                                 "notebook and unknown Read forms are refused. Request file_path with an "
                                 "optional positive integer line offset and limit for a plain text file")
        file_path = params.get("file_path")
        if (not isinstance(file_path, str) or not file_path.strip() or len(file_path) > 4096
                or "\x00" in file_path):
            raise _ReadRefused("a plain text Read admission requires a non-empty text file_path; the call "
                               "is refused rather than admitted without a measurable target")
        offset = params.get("offset")
        limit = params.get("limit")
        if "offset" in params and (type(offset) is not int or not 1 <= offset <= READ_MAX_OFFSET):
            raise _ReadRefused("the Read offset must be a positive integer line number no larger than "
                               + str(READ_MAX_OFFSET) + "; booleans, fractions, text, null and out-of-range "
                                 "values are refused")
        if "limit" in params and (type(limit) is not int or not 1 <= limit <= READ_MAX_LIMIT):
            raise _ReadRefused("the Read limit must be a positive integer line count no larger than "
                               + str(READ_MAX_LIMIT) + "; booleans, fractions, text, null and out-of-range "
                                 "values are refused")
        session_id = event.get("session_id")
        transcript_path = event.get("transcript_path")
        cwd = event.get("cwd")
        tool_use_id = event.get("tool_use_id")
        if (not isinstance(session_id, str) or not isinstance(transcript_path, str)
                or not isinstance(cwd, str) or not isinstance(tool_use_id, str)
                or not session_id or not transcript_path or not cwd or not tool_use_id):
            raise _ReadRefused("Read admission requires non-empty text session_id, transcript_path, cwd "
                               "and tool_use_id hook fields; the call is refused rather than admitted "
                               "without that identity")
        try:
            canonical = str(uuid.UUID(session_id))
        except ValueError:
            raise _ReadRefused("the native session_id is not a session UUID")
        if canonical != session_id:
            raise _ReadRefused("the native session_id is not the canonical session UUID form")
        _read_path_parts(transcript_path, "the native transcript path")
        if transcript_path.split("/")[-1] != session_id + ".jsonl":
            raise _ReadRefused("the native transcript does not match the exact root session file")
        _read_path_parts(cwd, "the hook cwd")
        if len(tool_use_id) > 256 or any(ord(character) < 33 for character in tool_use_id):
            raise _ReadRefused("the native tool_use_id is malformed or overlong")
        path = _read_target_path(file_path, cwd)
        message_id, device, inode, _ = _read_message_identity_with_readiness(
            transcript_path, session_id, cwd, tool_use_id, params)
        estimate = _measure_read(path, offset, limit)
        return _reserve_read(transcript_path, session_id, cwd, tool_use_id, params, message_id, device,
                             inode, estimate)
    except _ReadRefused as exc:
        return "Read admission: " + str(exc)
    except OSError as exc:
        return "Read admission: " + _os_failure_reason(exc)
    except (ValueError, TypeError, KeyError) as exc:
        return ("Read admission: the call could not be verified or durably reserved ("
                + type(exc).__name__ + "); the read is refused rather than admitted without a bounded "
                  "reservation. Retry after the path, native transcript and private admission state are "
                  "readable, or request a smaller explicit line range. Ordinary permission rules still "
                  "decide file access")


def _inherited_worker_conflict():
    """A denial reason when the inherited environment contradicts a pinned worker launch, else None.

    The guard never echoes values: only the offending key name is reported. Absent keys are left to
    the prepared local settings; a present key that forces a model, remaps a family alias, changes
    the routing destination or contradicts the pinned delegation limits means the launch cannot be
    proven to match the preparation, so it is refused. Generated exact definitions are not a bypass
    for an inherited force flag.
    """
    if os.environ.get(FORCED_SUBAGENT_ENV):
        return (FORCED_SUBAGENT_ENV + " is set in the inherited environment and would force a subagent model over "
                "the pinned worker definition")
    remaps = [key for key in ALIAS_REMAP_ENV if os.environ.get(key)]
    if remaps:
        return (", ".join(remaps) + " remaps a worker family alias in the inherited environment; the pinned worker "
                "route cannot be proven")
    if os.environ.get(GATEWAY_ENV):
        return (GATEWAY_ENV + " is set in the inherited environment, so the routing destination is an unverified "
                "gateway and the pinned first-party worker qualification does not apply")
    for key in sorted(PINNED_WORKER_ENV):
        value = os.environ.get(key)
        if value is not None and value != PINNED_WORKER_ENV[key]:
            return key + " in the inherited environment contradicts the pinned native delegation limits"
    return None


def _identity(event, key):
    """None when the key is absent or null; otherwise the text value (an empty string still counts as present)."""
    if key not in event or event[key] is None:
        return None
    value = event[key]
    if not isinstance(value, str):
        raise ValueError(key + " must be text")
    return value


def decide(event):
    """Return a denial reason, or None when the guard takes no decision."""
    if not isinstance(event, dict):
        raise ValueError("hook event must be a JSON object")
    tool = event.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise ValueError("hook event lacks tool_name")
    params = event.get("tool_input")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("tool_input must be a JSON object")
    agent_type = _identity(event, "agent_type")
    agent_id = _identity(event, "agent_id")
    # A main session launched with --agent may also have agent_type. Only a
    # nonempty agent_id together with a pinned type identifies an execution worker.
    # Any partial identity remains ambiguous and must never gain worker tools.
    inside = agent_type is not None or agent_id is not None
    worker = bool(agent_id and agent_id.strip()) and agent_type in PINNED_AGENTS
    if tool == "Agent":
        if inside:
            return "native workers cannot delegate further (one layer)"
        if params.get("subagent_type") not in PINNED_AGENTS:
            return "only the pinned pr-sonnet and pr-opus agents may be launched; omitted, built-in, fork or unknown types are refused"
        extra = sorted(str(key) for key in params if key not in AGENT_KEYS)
        if extra:
            return "Agent parameters " + ", ".join(extra) + " are refused (no model override, isolation, resume or team routing)"
        quota = _quota_denial(event)
        if quota is not None:
            return quota
        if params.get("run_in_background") is True:
            return "native agents must finish in the foreground; run_in_background=true is refused"
        if "run_in_background" in params and params["run_in_background"] is not False:
            return "run_in_background must be omitted or exactly false for foreground native delegation"
        conflict = _inherited_worker_conflict()
        if conflict is not None:
            return conflict
        if os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") != "1":
            return ("foreground native delegation requires inherited CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1; "
                    "the current native process has not proven that setting")
        return None
    if tool in DENIED_TOOLS or tool.startswith("Team"):
        return tool + " routes (workflows, teams, continuation or messaging) are not authorized in this bounded routing configuration"
    if tool == "Skill":
        if worker and agent_type == BROWSER_AGENT and params.get("skill") == BROWSER_SKILL:
            return None
        return "skill dispatch is refused except the pinned browser skill inside pr-opus"
    if inside and tool.startswith(SUBMISSION_PREFIXES):
        return "native workers cannot submit delegate or room work; only the root engineer uses the pinned provider tools"
    if inside:
        if not worker:
            return "execution requires an identified pinned pr-sonnet or pr-opus worker; ambiguous worker identity is refused"
        return None
    if tool == "Read":
        return _read_denial(event, params)
    if tool not in ROOT_TOOLS and tool not in DEEPSEEK_TOOLS:
        return ("Fable orchestrates and reviews; shell commands, edits, tests, browser work and other execution "
                "belong to the assigned operator or pinned workers. Delegate the bounded task when authorized; "
                "do not retry through another tool or treat verification as permission to take over execution")
    if tool in NEW_WORK_TOOLS:
        return _quota_denial(event)
    return None


def main():
    if len(sys.argv) > 1:
        raise ValueError("the routing guard takes no arguments")
    raw = sys.stdin.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("hook event is oversized")
    event = json.loads(raw)
    reason = decide(event)
    if reason is not None:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": "Project Room routing guard: " + reason}}, sys.stdout)
        sys.stdout.write("\n")
    elif event["tool_name"] in NEW_WORK_TOOLS and not any(key in event for key in QUOTA_HOOK_FIELDS):
        print("Project Room routing guard: native quota evidence unavailable (missing hook metadata); "
              "only legacy routing rules were checked", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - every failure must block the call
        print("Project Room routing guard error: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
