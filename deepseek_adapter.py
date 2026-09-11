#!/usr/bin/env python3
"""DeepSeek text delegate for Project Room: a self-contained stdio MCP adapter with durable bounded jobs.

The adapter sends chat completions only to the one fixed backend its key-free configuration selects deliberately:
the official DeepSeek API (https://api.deepseek.com) or DeepInfra's hosted OpenAI-compatible endpoint
(https://api.deepinfra.com/v1/openai), always with the exact configured model and the pinned deep-lane settings,
never through a proxy, redirect or fallback. It is a text delegate, not an agentic runtime: it returns proposed
code, tests, reviews and reasoning summaries, and never executes returned code, shell or tool calls. Every ledger,
job, export, probe and default secret path derives from the controller home passed as --home. Nothing here imports
the controller modules; the small owned-descriptor primitives are duplicated on purpose so that a room's
provider snapshot is exactly this file plus its key-free configuration.
"""

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid

VERSION = "0.3.0"
SERVER_NAME = "deepseek-delegate"
# Exactly two fixed transports. Each pins the TLS host, port and path that is ever connected to, the request syntax the
# body builder speaks, its own default private key file name and the shape of its model identifiers. The configuration
# selects one deliberately through `backend`; there is no arbitrary endpoint, proxy, redirect or fallback provider.
BACKENDS = {
    "official": {"label": "DeepSeek official API", "key_label": "DeepSeek", "base_url": "https://api.deepseek.com",
                 "host": "api.deepseek.com", "port": 443, "path": "/chat/completions", "request_syntax": "deepseek_thinking",
                 "key_name": "deepseek-api-key", "namespaced_model": False},
    "deepinfra": {"label": "DeepInfra hosted OpenAI-compatible endpoint", "key_label": "DeepInfra", "base_url": "https://api.deepinfra.com/v1/openai",
                  "host": "api.deepinfra.com", "port": 443, "path": "/v1/openai/chat/completions", "request_syntax": "openai_reasoning_effort",
                  "key_name": "deepinfra-api-key", "namespaced_model": True},
}
DEFAULT_BACKEND = "official"
EXACT_BASE_URL = BACKENDS["official"]["base_url"]  # the official transport keeps its historical names
API_HOST = BACKENDS["official"]["host"]
API_PORT = BACKENDS["official"]["port"]
API_PATH = BACKENDS["official"]["path"]
DEFAULT_MODEL = "deepseek-v4.1-flash-expires-on-0910"
DEEPINFRA_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
DEEPINFRA_MAX_TOKENS = 131072        # the user's explicit output selection for new DeepInfra rooms; DeepInfra's public max_output_tokens
DEEPINFRA_CONTEXT_TOKENS = 1048576   # DeepInfra's public max_tokens (context) for the hosted model; advertised, not measured here
DEFAULTS = {
    "backend": DEFAULT_BACKEND, "base_url": EXACT_BASE_URL, "model": DEFAULT_MODEL, "api_key_file": None,
    "reasoning_effort": "max", "max_tokens": 393216, "context_tokens": 1048576, "context_basis": "provisional_v4_family",
    "ask_max_tokens": 8192, "ask_effort": "low", "max_input_bytes": 614400,
    "min_tokens_per_second": 40, "connect_margin_seconds": 60, "request_timeout_seconds": 9891, "read_idle_seconds": 120,
    "max_concurrent_per_room": 1, "max_concurrent_per_host": 2, "max_context_files": 16, "max_context_file_bytes": 262144,
    "max_content_bytes": 8388608, "max_reasoning_bytes": 8388608, "max_wire_bytes": 134217728, "max_wire_tail_bytes": 65536,
    "max_metadata_bytes": 1048576, "max_sse_line_bytes": 1048576, "max_retained_bytes": 1073741824, "max_jobs": 10000,
}
# Backend-specific defaults layered over DEFAULTS before the explicit configuration: a DeepInfra configuration pins the
# namespaced hosted model, the user-selected 131,072-token output cap, the advertised context and the timeout the
# validator's own sizing rule derives for that cap (ceil(131072/40)+60). Every value can still be set explicitly and
# every explicit value is validated the same way; nothing here lowers or substitutes a value a configuration pins.
BACKEND_DEFAULTS = {
    "official": {},
    "deepinfra": {"model": DEEPINFRA_MODEL, "max_tokens": DEEPINFRA_MAX_TOKENS, "context_tokens": DEEPINFRA_CONTEXT_TOKENS,
                  "context_basis": "deepinfra_public_metadata", "request_timeout_seconds": 3337},
}
INTEGER_FIELDS = ("max_tokens", "context_tokens", "ask_max_tokens", "max_input_bytes", "min_tokens_per_second",
                  "connect_margin_seconds", "request_timeout_seconds", "read_idle_seconds", "max_concurrent_per_room",
                  "max_concurrent_per_host", "max_context_files", "max_context_file_bytes", "max_content_bytes",
                  "max_reasoning_bytes", "max_wire_bytes", "max_wire_tail_bytes", "max_metadata_bytes", "max_sse_line_bytes",
                  "max_retained_bytes", "max_jobs")
WIRE_BYTES_PER_TOKEN = 320      # conservative SSE framing allowance per streamed token, not a provider guarantee
WIRE_FIXED_ALLOWANCE = 1048576
TEXT_BYTES_PER_TOKEN = 8
EFFORTS = ("low", "high", "max")
ASK_EFFORTS = ("none", "low")
LANES = ("deep", "ask")
FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "function_call", "insufficient_system_resource"})
# Provider error type/code values are classified against this fixed vocabulary; anything else is recorded as "unrecognized".
# No provider-derived string ever reaches a public outcome, a receipt or the ledger, so no envelope can carry the key through.
ERROR_CLASSES = frozenset({
    "invalid_request_error", "invalid_request", "invalid_format", "invalid_parameters", "invalid_parameter", "invalid_argument",
    "authentication_error", "authentication_fails", "invalid_api_key", "permission_error", "permission_denied", "forbidden",
    "insufficient_balance", "insufficient_quota", "billing_not_active", "account_deactivated",
    "not_found_error", "model_not_found", "not_found", "rate_limit_error", "rate_limit_reached", "rate_limit_exceeded",
    "context_length_exceeded", "tokens_exceeded_error", "content_filter", "content_policy_violation",
    "server_error", "internal_error", "internal_server_error", "api_error", "overloaded_error", "server_overloaded",
    "engine_overloaded", "rate_limited",  # DeepInfra's documented busy/rate-limit codes inside its OpenAI-style envelope
    "service_unavailable", "timeout", "request_timeout", "gateway_timeout", "bad_gateway", "unknown_error"})
USAGE_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"})
USAGE_DETAIL_FIELDS = {"prompt_tokens_details": frozenset({"cached_tokens", "audio_tokens"}),
                       "completion_tokens_details": frozenset({"reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"})}
# 4xx statuses whose validated JSON error envelope proves the request was refused before generation: DeepSeek documents
# 400/401/402/422/429 as such refusals, DeepInfra documents 429 busy/rate-limit refusals in the same OpenAI-style
# envelope, and the others are HTTP-level refusals of the request itself. Every other 4xx (for example 408 request
# timeout or 409 conflict) is ambiguous: the request may have been processed, so it stays possibly billed and is never
# replayed automatically.
DEFINITE_REJECTIONS = frozenset({400, 401, 402, 403, 404, 405, 413, 414, 415, 422, 429, 431})
# Per-job metadata records are bounded on their ENCODED bytes (JSON escaping expands a control character sixfold), and
# the reserve is the sum of those bounds, so it holds by construction rather than by assumption: request.json is checked
# at admission, resolution.json before a resolution is recorded, meta.json holds fixed allowlisted fields with bounded
# counters, and heartbeat.json/cancel.json are fixed short records whose allowance also covers one in-flight atomic
# temporary. The remainder of max_metadata_bytes is the redacted wire tail plus worker.log.
MAX_REQUEST_RECORD_BYTES = 24576
MAX_META_RECORD_BYTES = 8192
MAX_RESOLUTION_RECORD_BYTES = 24576
SMALL_RECORDS_RESERVE_BYTES = 8192
METADATA_RESERVE_BYTES = MAX_REQUEST_RECORD_BYTES + MAX_META_RECORD_BYTES + MAX_RESOLUTION_RECORD_BYTES + SMALL_RECORDS_RESERVE_BYTES  # 65536
MAX_DIAGNOSIS_BYTES = 4096
MAX_NOTE_BYTES = 16384
MAX_USAGE_COUNTER = 2 ** 53 - 1  # a provider counter beyond the exact-integer range is unrecognized, never stored
MAX_SNAPSHOT_BYTES = 8 * 1048576  # the pinned adapter source and configuration are read owned and bounded, as the controller reads them
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")
JOB_ID = re.compile(r"[0-9a-f]{32}")
ROOM_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# A hosted namespaced identifier: exactly one organisation/name separator, each side an exact printable token. The
# served model is still compared for byte equality against the configured string; the pattern only shapes what may be pinned.
NAMESPACED_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
KEY_LIMIT = 4097
MAX_CONFIG_BYTES = 1048576
MAX_SETTINGS_BYTES = 4 * 1048576
MAX_TEXT_BYTES = 2_000_000
MAX_LINE = 4_000_000
STARTUP_GRACE_SECONDS = 10.0
STATUS_POLL_SECONDS = 0.5
MAX_WAIT_SECONDS = 49
DEFAULT_WAIT_SECONDS = 45
ASK_INLINE_WAIT_SECONDS = 45
MAX_RESULT_CHARS = 32768
HEARTBEAT_SECONDS = 5.0
MAX_PROBE_ENTRIES = 4096  # receipts plus their artifact directories; nothing prunes them, so the scan bound is generous
LATEST_JOBS = 20
NOT_STARTED, QUEUED, SUBMITTING, STREAMING = "not_started", "queued", "submitting", "streaming"
COMPLETED, TRUNCATED, REJECTED = "completed", "truncated", "rejected_before_generation"
FAILED_AFTER_SEND, ABANDONED_DEADLINE = "failed_after_send", "abandoned_local_deadline"
ABANDONED_CANCELLED, UNKNOWN = "abandoned_cancelled", "unknown_delivery"
ACTIVE_STATES = (QUEUED, SUBMITTING, STREAMING)
STOP_STATES = (UNKNOWN, FAILED_AFTER_SEND)
TERMINAL_STATES = (NOT_STARTED, COMPLETED, TRUNCATED, REJECTED, FAILED_AFTER_SEND, ABANDONED_DEADLINE, ABANDONED_CANCELLED, UNKNOWN)
# Documented state table: what each terminal state means for admission, resolution and deduplication.
STATE_TABLE = {
    NOT_STARTED: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "new request_id permitted", "possibly_billed": False, "meaning": "proven unsent: nothing reached the provider"},
    QUEUED: {"terminal": False, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused while active", "possibly_billed": False, "meaning": "admitted; worker not yet at the send boundary"},
    SUBMITTING: {"terminal": False, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused while active", "possibly_billed": True, "meaning": "durably recorded before any request bytes could be sent"},
    STREAMING: {"terminal": False, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused while active", "possibly_billed": True, "meaning": "response headers received; body streaming"},
    COMPLETED: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused; read the existing job", "possibly_billed": True, "meaning": "finish_reason stop and [DONE] received; content published"},
    TRUNCATED: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused", "possibly_billed": True, "meaning": "incomplete output (finish_reason length/filter or a local cap); never a completed answer"},
    REJECTED: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "new request_id permitted only with a diagnosis", "possibly_billed": False, "meaning": "definite 4xx provider rejection with a validated error envelope before generation"},
    FAILED_AFTER_SEND: {"terminal": True, "stops_room_lane": True, "resolve_eligible": True, "identical_payload": "refused", "possibly_billed": True, "meaning": "request delivered, outcome incomplete, contradictory or a 5xx server/gateway failure; user resolution required"},
    ABANDONED_DEADLINE: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused", "possibly_billed": True, "meaning": "local fixed deadline expired; remote work may continue and bill"},
    ABANDONED_CANCELLED: {"terminal": True, "stops_room_lane": False, "resolve_eligible": False, "identical_payload": "refused", "possibly_billed": True, "meaning": "local cancellation closed the connection; remote outcome unknown"},
    UNKNOWN: {"terminal": True, "stops_room_lane": True, "resolve_eligible": True, "identical_payload": "refused", "possibly_billed": True, "meaning": "delivery unknown (worker vanished or transport failed after possible send); user resolution required"},
}
CONTEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".md", ".markdown", ".txt", ".rst", ".toml",
    ".yaml", ".yml", ".ini", ".cfg", ".sh", ".bash", ".zsh", ".html", ".css", ".scss", ".sql", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".m", ".mm", ".lua", ".r", ".jl", ".scala", ".xml",
    ".csv", ".tsv", ".proto", ".graphql", ".tf", ".gradle", ".cmake", ".mk", ".diff", ".patch", ".svg"})
CONTEXT_BARE_NAMES = frozenset({"Makefile", "Dockerfile", "Justfile", "Rakefile", "Gemfile", "Pipfile", "Procfile", "Vagrantfile",
                                "BUILD", "WORKSPACE", "LICENSE", "README", "CHANGELOG", "NOTICE", "AUTHORS", "CODEOWNERS", "Containerfile"})
CREDENTIAL_NAMES = re.compile(r"(?i)^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|credentials(?:\..*)?"
                              r"|.*\.(?:key|pem|p12|pfx|jks|keystore|secret|crt|cer|der|gpg|asc))$")
FRAMING = ("You are a text-only engineering delegate working for an orchestrator. Produce a complete, self-contained answer "
           "to the task below: code, tests, review findings or a reasoning summary, exactly as requested. You have no tools, "
           "shell, file system, network or memory of other tasks, and nothing you return is executed automatically. State "
           "assumptions explicitly, keep code anchored to the supplied context, and never claim to have run anything.")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LEAF_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class AdapterError(Exception):
    """A controlled refusal. `code` is an allowlisted reason; the message never carries keys, prompts or provider prose."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra

    def payload(self):
        return {"error": str(self), "code": self.code, **self.extra}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_timestamp(value):
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("naive timestamp")
    return stamp


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _positive_int(value):
    return type(value) is int and value > 0


# ---------------------------------------------------------------------------------------------------------------
# Owned-descriptor primitives (duplicated from the controller on purpose; this file must stay self-contained).
# ---------------------------------------------------------------------------------------------------------------

def _require_owned_directory(fd, code):
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise AdapterError(code, "Expected a user-owned directory")
    return metadata


def open_directory(path, code="path_unsafe"):
    """Open a trusted configured directory by path (O_NOFOLLOW on the final component) and require user ownership."""
    try:
        fd = os.open(str(path), DIRECTORY_FLAGS)
    except FileNotFoundError as exc:
        raise AdapterError(code.replace("_unsafe", "_missing"), "Directory is missing") from exc
    except OSError as exc:
        raise AdapterError(code, "Directory could not be opened: " + type(exc).__name__) from exc
    try:
        _require_owned_directory(fd, code)
    except BaseException:
        os.close(fd)
        raise
    return fd


def directory_below(base_fd, parts, code="path_unsafe", create=False, mode=0o700):
    """Descriptor for base/parts reached component by component with O_NOFOLLOW; every component is a user-owned directory."""
    parts = tuple(parts)
    if not parts or any(part in ("", ".", "..") or "/" in part or "\0" in part for part in parts):
        raise AdapterError(code, "Invalid path components")
    fd, borrowed = base_fd, True
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode, dir_fd=fd)
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError as exc:
                raise AdapterError(code.replace("_unsafe", "_missing"), "Directory is missing") from exc
            except OSError as exc:
                raise AdapterError(code, "Directory could not be opened: " + type(exc).__name__) from exc
            if not borrowed:
                os.close(fd)
            fd, borrowed = child, False
            _require_owned_directory(fd, code)
        return fd
    except BaseException:
        if not borrowed:
            os.close(fd)
        raise


def open_regular_below(base_fd, parts, limit, code="path_unsafe", require_private=False):
    """Handle for a user-owned regular file at base/parts, opened O_NOFOLLOW|O_NONBLOCK, at most `limit` bytes."""
    parts = tuple(parts)
    if not parts or any(part in ("", ".", "..") or "/" in part or "\0" in part for part in parts):
        raise AdapterError(code, "Invalid path components")
    fd, borrowed = base_fd, True
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            try:
                child = os.open(part, LEAF_FLAGS if leaf else DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError as exc:
                raise AdapterError(code.replace("_unsafe", "_missing"), "File is missing") from exc
            except OSError as exc:
                raise AdapterError(code, "File could not be opened: " + type(exc).__name__) from exc
            if not borrowed:
                os.close(fd)
            fd, borrowed = child, False
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid():
                raise AdapterError(code, "Foreign owner")
            if leaf:
                if not stat.S_ISREG(metadata.st_mode):
                    raise AdapterError(code, "Not a regular file")
                if require_private and stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise AdapterError(code.replace("_unsafe", "_mode"), "File is readable or writable by group/other")
                if metadata.st_size > limit:
                    raise AdapterError(code.replace("_unsafe", "_oversized"), "File exceeds its bounded size")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise AdapterError(code, "Not a directory")
        handle = os.fdopen(fd, "rb")
        fd = None
        return handle
    finally:
        if fd is not None and not borrowed:
            os.close(fd)


def read_below(base_fd, parts, limit, code="path_unsafe", require_private=False):
    with open_regular_below(base_fd, parts, limit, code, require_private) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise AdapterError(code.replace("_unsafe", "_oversized"), "File exceeds its bounded size")
    return data


def exists_below(dir_fd, name):
    try:
        metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def create_below(dir_fd, name, mode=0o600):
    if "/" in name or name in ("", ".", "..") or "\0" in name:
        raise AdapterError("path_unsafe", "Invalid path components")
    return os.open(name, CREATE_FLAGS, mode, dir_fd=dir_fd)


def write_below(dir_fd, name, payload, mode=0o600):
    """Atomic replacement relative to a bound directory descriptor: O_EXCL temporary sibling, fsync, rename, fsync."""
    if "/" in name or name in ("", ".", "..") or "\0" in name:
        raise AdapterError("path_unsafe", "Invalid path components")
    temporary = name + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, CREATE_FLAGS, mode, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)
        raise


def write_json_below(dir_fd, name, value, mode=0o600):
    write_below(dir_fd, name, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"), mode)


def read_json_below(dir_fd, parts, limit, code="path_unsafe"):
    try:
        return json.loads(read_below(dir_fd, parts, limit, code))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise AdapterError(code.replace("_unsafe", "_malformed"), "File is not valid JSON") from exc


def ensure_private_directory(path):
    """Create a 0700 directory (each missing parent also 0700) by trusted configured path; refuse a symlink or foreign owner."""
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700)
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise AdapterError("path_unsafe", "Expected a user-owned directory: " + path.name)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        # The same rule the launch gate applies to the export grant, enforced by the creator too: refused, never chmodded.
        raise AdapterError("path_unsafe_mode", "Directory must not be accessible to group or other; run chmod 700 on " + str(path))
    return path


# ---------------------------------------------------------------------------------------------------------------
# Provider configuration (key-free) and the numeric consistency validator.
# ---------------------------------------------------------------------------------------------------------------

def validate_config(raw, home):
    """Normalize a key-free provider configuration; every bound must be a finite positive integer and consistent.

    `backend` selects exactly one fixed transport (official by default, deepinfra deliberately). Its base_url, model
    shape and default key file are backend-specific: a base_url belonging to another backend, a model identifier of
    the other backend's shape, or a key file named after the other backend's default are contradictions and are
    refused rather than reconciled, so one backend's credential or endpoint is never reused for the other by accident."""
    if not isinstance(raw, dict):
        raise AdapterError("config_invalid", "Provider configuration must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise AdapterError("config_invalid", "Unknown provider configuration fields: " + ", ".join(unknown))
    if any("key" in name.lower() and name != "api_key_file" for name in raw):
        raise AdapterError("config_invalid", "Credential material does not belong in provider configuration")
    backend = raw.get("backend", DEFAULT_BACKEND)
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise AdapterError("config_invalid", "backend must be one of: " + ", ".join(BACKENDS))
    transport = BACKENDS[backend]
    config = {**DEFAULTS, **BACKEND_DEFAULTS[backend], **raw}
    if "base_url" not in raw:
        config["base_url"] = transport["base_url"]
    if config["base_url"] != transport["base_url"]:
        raise AdapterError("config_invalid", f"base_url must be exactly {transport['base_url']} for the {backend} backend; "
                           "a transport is selected deliberately with backend, never by URL")
    model = config["model"]
    if transport["namespaced_model"]:
        if not isinstance(model, str) or not NAMESPACED_MODEL_ID.fullmatch(model):
            raise AdapterError("config_invalid", f"model must be the exact namespaced organisation/name identifier of the {backend} backend")
    elif not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise AdapterError("config_invalid", f"model must be an exact printable model identifier without a namespace for the {backend} backend")
    key_file = config["api_key_file"]
    if key_file is None:
        config["api_key_file"] = str(Path(home) / "secrets" / transport["key_name"])
    elif not isinstance(key_file, str) or not key_file or "\0" in key_file or not os.path.isabs(key_file):
        raise AdapterError("config_invalid", "api_key_file must be null or an absolute path")
    if any(Path(config["api_key_file"]).name == other["key_name"] for name, other in BACKENDS.items() if name != backend):
        raise AdapterError("config_invalid", f"api_key_file names another backend's default key file; the {backend} backend keeps its own separate private key")
    if config["reasoning_effort"] not in EFFORTS:
        raise AdapterError("config_invalid", "reasoning_effort must be low, high or max")
    if config["ask_effort"] not in ASK_EFFORTS:
        raise AdapterError("config_invalid", "ask_effort must be none or low")
    if not isinstance(config["context_basis"], str) or not TOKEN.fullmatch(config["context_basis"]):
        raise AdapterError("config_invalid", "context_basis must be a short token")
    for name in INTEGER_FIELDS:
        if not _positive_int(config[name]):
            raise AdapterError("config_invalid", name + " must be a positive integer")
    tokens, seconds = config["max_tokens"], config["min_tokens_per_second"]
    rules = [
        (config["max_wire_bytes"] >= tokens * WIRE_BYTES_PER_TOKEN + WIRE_FIXED_ALLOWANCE,
         f"max_wire_bytes must be at least max_tokens*{WIRE_BYTES_PER_TOKEN}+{WIRE_FIXED_ALLOWANCE}"),
        (config["max_content_bytes"] >= tokens * TEXT_BYTES_PER_TOKEN, f"max_content_bytes must be at least max_tokens*{TEXT_BYTES_PER_TOKEN}"),
        (config["max_reasoning_bytes"] >= tokens * TEXT_BYTES_PER_TOKEN, f"max_reasoning_bytes must be at least max_tokens*{TEXT_BYTES_PER_TOKEN}"),
        (config["request_timeout_seconds"] >= math.ceil(tokens / seconds) + config["connect_margin_seconds"],
         "request_timeout_seconds must be at least ceil(max_tokens/min_tokens_per_second)+connect_margin_seconds"),
        (config["read_idle_seconds"] < config["request_timeout_seconds"], "read_idle_seconds must be below request_timeout_seconds"),
        (config["max_wire_tail_bytes"] + METADATA_RESERVE_BYTES <= config["max_metadata_bytes"],
         f"max_wire_tail_bytes plus the {METADATA_RESERVE_BYTES}-byte metadata reserve must fit within max_metadata_bytes (the remainder bounds worker.log)"),
        (config["max_sse_line_bytes"] <= config["max_wire_bytes"], "max_sse_line_bytes must not exceed max_wire_bytes"),
        (config["context_tokens"] > tokens, "context_tokens must exceed max_tokens"),
        (config["max_input_bytes"] <= config["context_tokens"] - tokens,
         "max_input_bytes must leave the output budget within context_tokens at one byte per token"),
        (config["max_context_file_bytes"] <= config["max_input_bytes"], "max_context_file_bytes must not exceed max_input_bytes"),
        (config["ask_max_tokens"] <= tokens, "ask_max_tokens must not exceed max_tokens"),
        (config["max_concurrent_per_host"] >= config["max_concurrent_per_room"], "max_concurrent_per_host must be at least max_concurrent_per_room"),
        (reservation_bytes(config) <= config["max_retained_bytes"], "one worst-case job reservation must fit within max_retained_bytes"),
    ]
    for satisfied, message in rules:
        if not satisfied:
            raise AdapterError("config_invalid", message)
    return config


def reservation_bytes(config):
    """Worst-case retained bytes for one job: input, content, reasoning and bounded metadata; unretained wire is excluded."""
    return config["max_input_bytes"] + config["max_content_bytes"] + config["max_reasoning_bytes"] + config["max_metadata_bytes"]


def worker_log_limit(config):
    """Bytes the private worker.log may hold: what remains of max_metadata_bytes after the wire tail and the metadata reserve."""
    return config["max_metadata_bytes"] - config["max_wire_tail_bytes"] - METADATA_RESERVE_BYTES


def load_config(path, home):
    """Read and validate the key-free provider configuration; returns (config, sha256 of the exact file bytes)."""
    path = Path(path)
    if not path.is_absolute():
        raise AdapterError("config_invalid", "Provider configuration path must be absolute")
    parent_fd = open_directory(path.parent, "config_unsafe")
    try:
        raw = read_below(parent_fd, (path.name,), MAX_CONFIG_BYTES, "config_unsafe")
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise AdapterError("config_invalid", "Provider configuration is not valid JSON") from exc
    return validate_config(value, home), sha(raw)


def _read_snapshot(path, limit=None):
    """Bytes of a pinned snapshot file (the adapter source or its configuration) read through an owned directory
    descriptor without following symlinks and within a fixed bound, so a replaced snapshot is a refusal, never a hang."""
    path = Path(path)
    parent_fd = open_directory(path.parent, "snapshot_unsafe")
    try:
        return read_below(parent_fd, (path.name,), MAX_SNAPSHOT_BYTES if limit is None else limit, "snapshot_unsafe")
    finally:
        os.close(parent_fd)


def input_estimate(config):
    return {"method": "utf8_bytes", "max_input_bytes": config["max_input_bytes"],
            "assumption": "at most one token per UTF-8 byte plus fixed framing; no tokenizer is bundled and no exact token count is claimed",
            "provisional_input_tokens": config["context_tokens"] - config["max_tokens"], "context_basis": config["context_basis"]}


# ---------------------------------------------------------------------------------------------------------------
# Credential file: written only by set-key at a TTY, read only at request execution, never echoed.
# ---------------------------------------------------------------------------------------------------------------

def _open_key_component(part, dir_fd, create):
    try:
        return os.open(part, DIRECTORY_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create:
            raise
    with contextlib.suppress(FileExistsError):  # whatever appeared meanwhile is re-checked by the O_NOFOLLOW open
        os.mkdir(part, 0o700, dir_fd=dir_fd)
    return os.open(part, DIRECTORY_FLAGS, dir_fd=dir_fd)


def _key_parent(path, create=False):
    """Descriptor for the key file's directory, reached from the filesystem root one component at a time with
    O_NOFOLLOW. No ancestor may be a symlink, be owned by anyone but root or the current user, or be writable by
    group/other without the sticky bit; the key directory itself must be user-owned with no group/other bits.
    With `create`, a missing component is created 0700 relative to the previous descriptor and then opened with
    the same checks, so set-key applies the read-side ancestor rules before any secret is handled."""
    parent = Path(path).parent
    if not parent.is_absolute() or any(part in ("", ".", "..") for part in parent.parts[1:]):
        raise AdapterError("key_unsafe_ancestor", "The key path must be absolute and normalized")
    parts = parent.parts[1:]
    if not parts:
        raise AdapterError("key_unsafe_ancestor", "The key directory must not be the filesystem root")
    try:
        fd = os.open(parent.anchor, DIRECTORY_FLAGS)
    except OSError as exc:
        raise AdapterError("key_unsafe_ancestor", "The filesystem root could not be opened: " + type(exc).__name__) from exc
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            try:
                child = _open_key_component(part, fd, create)
            except FileNotFoundError as exc:
                raise AdapterError("key_missing", "The key directory does not exist; run set-key") from exc
            except OSError as exc:  # ELOOP for a symlinked component, ENOTDIR, EACCES
                raise AdapterError("key_unsafe_ancestor", "A key path component is a symlink or not an openable directory: " + type(exc).__name__) from exc
            os.close(fd)
            fd = child
            metadata = os.fstat(fd)
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISDIR(metadata.st_mode):
                raise AdapterError("key_unsafe_ancestor", "A key path component is not a directory")
            if leaf:
                if metadata.st_uid != os.getuid() or mode & 0o077:
                    raise AdapterError("key_unsafe_ancestor", "The key directory must be user-owned with no group/other access")
            elif metadata.st_uid not in (0, os.getuid()):
                raise AdapterError("key_unsafe_ancestor", "A key path ancestor is owned by another user")
            elif mode & 0o022 and not mode & stat.S_ISVTX:
                raise AdapterError("key_unsafe_ancestor", "A key path ancestor is writable by group or other without the sticky bit")
        return fd
    except BaseException:
        os.close(fd)
        raise


def key_diagnostics(path):
    """Metadata-only diagnostics for the configured key file; never reads or prints its contents."""
    path = Path(path)
    result = {"path": str(path), "present": False, "ok": False, "reason": None}
    try:
        parent_fd = _key_parent(path)
    except AdapterError as exc:
        result["reason"] = exc.code
        return result
    try:
        metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        result["reason"] = "key_missing"
        return result
    except OSError:
        result["reason"] = "key_unsafe"
        return result
    finally:
        os.close(parent_fd)
    result["present"] = True
    if not stat.S_ISREG(metadata.st_mode):
        result["reason"] = "key_not_regular"
    elif metadata.st_uid != os.getuid():
        result["reason"] = "key_foreign_owner"
    elif stat.S_IMODE(metadata.st_mode) & 0o077:
        result["reason"] = "key_unsafe_mode"
    elif metadata.st_size > KEY_LIMIT:
        result["reason"] = "key_oversized"
    elif metadata.st_size == 0:
        result["reason"] = "key_empty"
    else:
        result["ok"] = True
    return result


def read_api_key(path):
    """The key bytes (at most 4097 including one final newline) through owned, symlink-refusing, mode-checked access."""
    path = Path(path)
    parent_fd = _key_parent(path)
    try:
        raw = read_below(parent_fd, (path.name,), KEY_LIMIT, "key_unsafe", require_private=True)
    finally:
        os.close(parent_fd)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        key = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AdapterError("key_invalid", "The key file must contain a single printable ASCII token") from exc
    if not key or len(key) > KEY_LIMIT - 1 or any(not 0x21 <= ord(char) <= 0x7E for char in key):
        raise AdapterError("key_invalid", "The key file must contain a single printable ASCII token")
    return key


def set_key(home, path, rotate=False, reader=None, interactive=None, label=None):
    """Store a key entered at a user TTY: 0700 directories, 0600 file, exclusive temporary plus rename, never echoed.

    The root-to-leaf owned traversal that guards every read also guards this write: a symlinked, foreign-owned or
    writable ancestor is refused before the user is asked for the secret, missing directories are created 0700 on
    the way down, and an existing key is replaced only with an explicit rotation. `home` is the controller home the
    CLI resolved the default path from; it is not created or trusted by itself."""
    interactive = os.isatty(0) if interactive is None else interactive
    if not interactive:
        raise AdapterError("tty_required", "set-key must run at an interactive terminal")
    path = Path(path)
    if not Path(home).is_absolute():
        raise AdapterError("home_invalid", "--home must be absolute")
    parent_fd = _key_parent(path, create=True)
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise AdapterError("key_unsafe", "The key path could not be inspected: " + type(exc).__name__) from exc
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
                raise AdapterError("key_unsafe", "The key path is not a user-owned regular file; nothing was written")
            if not rotate:
                raise AdapterError("key_exists", "A key is already saved; pass --rotate to replace it deliberately")
        if reader is None:
            import getpass
            reader = lambda prompt: getpass.getpass(prompt)
        label = label or BACKENDS[DEFAULT_BACKEND]["key_label"]
        first = reader(label + " API key (input hidden): ")
        second = reader("Repeat the key: ")
        if first != second:
            raise AdapterError("key_mismatch", "The two entries differ; nothing was written")
        key = first.strip()
        if not key or len(key) > KEY_LIMIT - 1 or any(not 0x21 <= ord(char) <= 0x7E for char in key):
            raise AdapterError("key_invalid", "The key must be a single printable ASCII token")
        write_below(parent_fd, path.name, key.encode("ascii") + b"\n", 0o600)
    finally:
        os.close(parent_fd)
    return {"saved": True, "path": str(path), "rotated": rotate, "key_label": label}


def redact(data, key):
    """Replace the exact configured key value inside private diagnostics."""
    if not key:
        return data
    marker = b"[REDACTED]" if isinstance(data, bytes) else "[REDACTED]"
    return data.replace(key.encode("ascii") if isinstance(data, bytes) else key, marker)


# ---------------------------------------------------------------------------------------------------------------
# Durable ledger.
# ---------------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, room_id TEXT NOT NULL, request_id TEXT NOT NULL, lane TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, submitting_at TEXT, streaming_at TEXT, finished_at TEXT, deadline_at TEXT,
  requested_model TEXT NOT NULL, observed_model TEXT, thinking TEXT NOT NULL, reasoning_effort TEXT,
  max_tokens INTEGER NOT NULL, input_bytes INTEGER NOT NULL, content_bytes INTEGER, reasoning_bytes INTEGER,
  wire_bytes INTEGER, finish_reason TEXT, usage_json TEXT, usage_source TEXT, http_status INTEGER, error_code TEXT,
  error_type TEXT, availability TEXT, remote_outcome TEXT, possibly_billed INTEGER, reserved_bytes INTEGER NOT NULL,
  retained_bytes INTEGER, worker_pid INTEGER, execution_id TEXT, content_sha256 TEXT, content_path TEXT,
  export_reason TEXT, diagnosis TEXT, UNIQUE(room_id, request_id));
CREATE INDEX IF NOT EXISTS jobs_room_payload ON jobs(room_id, payload_sha256);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_room_created ON jobs(room_id, created_at);
CREATE TABLE IF NOT EXISTS resolutions(
  job_id TEXT PRIMARY KEY REFERENCES jobs(id), room_id TEXT NOT NULL, note_sha256 TEXT NOT NULL,
  note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS resolutions_room ON resolutions(room_id);
"""


class Ledger:
    def __init__(self, home):
        self.home = Path(home)
        self.root = ensure_private_directory(self.home / "deepseek")
        self.path = self.root / "ledger.sqlite3"
        if not self.path.exists():
            os.close(os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        db = self.connect()
        try:
            db.executescript(SCHEMA)  # executescript manages its own transaction boundaries
        finally:
            db.close()

    def connect(self, readonly=False):
        if readonly:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None)
        else:
            db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextlib.contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE ... COMMIT with synchronous=FULL: a committed row is durable before the next step runs."""
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    @contextlib.contextmanager
    def reading(self):
        db = self.connect(readonly=True)
        try:
            yield db
        finally:
            db.close()

    def job(self, job_id, room_id=None):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise AdapterError("job_id_invalid", "job_id must be the 32-hex identifier returned by this adapter")
        with self.reading() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (room_id is not None and row["room_id"] != room_id):
            raise AdapterError("job_unknown", "Unknown job_id for this room")
        return dict(row)

    def resolved(self, job_id):
        with self.reading() as db:
            return db.execute("SELECT job_id FROM resolutions WHERE job_id=?", (job_id,)).fetchone() is not None

    def stopping_jobs(self, room_id):
        with self.reading() as db:
            rows = db.execute("SELECT id,state,finished_at FROM jobs WHERE room_id=? AND state IN (?,?) AND id NOT IN (SELECT job_id FROM resolutions) ORDER BY created_at",
                              (room_id, UNKNOWN, FAILED_AFTER_SEND)).fetchall()
        return [dict(row) for row in rows]

    def active(self, room_id=None):
        with self.reading() as db:
            if room_id is None:
                rows = db.execute("SELECT * FROM jobs WHERE state IN (?,?,?) ORDER BY created_at", ACTIVE_STATES).fetchall()
            else:
                rows = db.execute("SELECT * FROM jobs WHERE room_id=? AND state IN (?,?,?) ORDER BY created_at", (room_id, *ACTIVE_STATES)).fetchall()
        return [dict(row) for row in rows]

    def storage(self):
        with self.reading() as db:
            reserved = db.execute("SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs WHERE state IN (?,?,?)", ACTIVE_STATES).fetchone()[0]
            retained = db.execute("SELECT COALESCE(SUM(retained_bytes),0) FROM jobs").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return {"reserved_bytes": int(reserved), "retained_bytes": int(retained), "jobs": int(count)}



# ---------------------------------------------------------------------------------------------------------------
# Request assembly: framing, task, inline context and explicitly named worktree files.
# ---------------------------------------------------------------------------------------------------------------

def worktree_root():
    """The verified handoff worktree the controller passes as PROJECT_ROOM_WORKTREE; context_path is refused without it."""
    value = os.environ.get("PROJECT_ROOM_WORKTREE")
    if not value or "\0" in value or not os.path.isabs(value):
        return None
    return Path(value)


def context_files(paths, config, root):
    """Read explicitly named files lexically beneath the worktree root through descriptor-relative no-symlink opens."""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or not all(isinstance(item, str) and item for item in paths):
        raise AdapterError("context_path_invalid", "context_path must name one or more absolute files")
    if len(paths) > config["max_context_files"]:
        raise AdapterError("context_files_limit", f"context_path may name at most {config['max_context_files']} files")
    if root is None:
        raise AdapterError("worktree_root_unavailable", "context_path requires PROJECT_ROOM_WORKTREE from the controller")
    root_fd = open_directory(root, "worktree_unsafe")
    files, seen = [], set()
    try:
        identity = os.fstat(root_fd)
        for item in paths:
            if "\0" in item or not os.path.isabs(item) or os.path.normpath(item) != item or item.endswith("/"):
                raise AdapterError("context_path_invalid", "context_path entries must be normalized absolute paths")
            try:
                relative = Path(item).relative_to(root)
            except ValueError as exc:
                raise AdapterError("context_path_outside_worktree", "context_path must lie beneath the verified worktree root") from exc
            parts = relative.parts
            if not parts or len(parts) > 32 or any(part.startswith(".") for part in parts):
                raise AdapterError("context_path_invalid", "context_path may not name dotfiles, hidden directories or overlong paths")
            name = parts[-1]
            suffix = Path(name).suffix.lower()
            if CREDENTIAL_NAMES.fullmatch(name):
                raise AdapterError("context_path_credential", "context_path refuses credential-looking files")
            if suffix not in CONTEXT_SUFFIXES and name not in CONTEXT_BARE_NAMES:
                raise AdapterError("context_path_suffix", "context_path allows common source, documentation and build files only")
            if item in seen:
                raise AdapterError("context_path_invalid", "context_path lists a file twice")
            seen.add(item)
            data = read_below(root_fd, parts, config["max_context_file_bytes"], "context_unsafe")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AdapterError("context_not_utf8", "context files must be UTF-8 text") from exc
            files.append({"path": str(relative), "bytes": len(data), "sha256": sha(data), "text": text})
        return files, {"path": str(root), "identity": [identity.st_dev, identity.st_ino]}
    finally:
        os.close(root_fd)


def assemble(task, context, files):
    parts = ["TASK:\n" + task]
    if context:
        parts.append("CONTEXT (inline):\n" + context)
    for entry in files:
        parts.append(f"FILE {entry['path']} ({entry['bytes']} bytes, sha256 {entry['sha256']}):\n" + entry["text"])
    return "\n\n-----\n\n".join(parts)


def lane_parameters(config, lane, effort=None):
    if lane == "deep":
        return {"thinking": "enabled", "reasoning_effort": config["reasoning_effort"], "max_tokens": config["max_tokens"]}
    effort = config["ask_effort"] if effort is None else effort
    if effort not in ASK_EFFORTS:
        raise AdapterError("effort_invalid", "deepseek_ask permits only effort none or low")
    return {"thinking": "disabled" if effort == "none" else "enabled", "reasoning_effort": None if effort == "none" else "low",
            "max_tokens": config["ask_max_tokens"]}


def build_body(config, parameters, user_text):
    """The request for one lane on the configured backend. Both transports receive the same framing, the exact
    configured model, streaming and the pinned output cap; only the reasoning syntax differs, so the lane semantics
    (thinking enabled at the pinned effort, or disabled for effort none) are identical on either backend."""
    body = {"model": config["model"], "messages": [{"role": "system", "content": FRAMING}, {"role": "user", "content": user_text}],
            "stream": True, "max_tokens": parameters["max_tokens"]}
    if BACKENDS[config["backend"]]["request_syntax"] == "deepseek_thinking":
        body["thinking"] = {"type": parameters["thinking"]}
        if parameters["reasoning_effort"]:
            body["reasoning_effort"] = parameters["reasoning_effort"]
    else:
        # DeepInfra's hosted Chat Completions schema: reasoning_effort carries the pinned effort (its published enum
        # includes max) and "none" disables reasoning entirely; stream usage is requested explicitly instead of relying
        # on the schema default. No cache-retention, webhook, batch or sampling parameter is ever sent.
        body["reasoning_effort"] = parameters["reasoning_effort"] if parameters["thinking"] == "enabled" else "none"
        body["stream_options"] = {"include_usage": True}
    return body


# ---------------------------------------------------------------------------------------------------------------
# Transport: one verifying TLS connection to the exact host, no redirects, no proxies, bounded SSE parsing.
# ---------------------------------------------------------------------------------------------------------------

def tls_context():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _open_connection(host, port, context, timeout):
    """The single transport seam. Production always builds a verifying TLS connection to the exact host; tests inject
    a loopback fake by replacing http.client.HTTPSConnection in-process, never through configuration, CLI or MCP."""
    return http.client.HTTPSConnection(host, port, context=context, timeout=timeout)


def _classify_error(*values):
    """Safe class for provider error type/code values: a vocabulary entry, 'unrecognized' when present but unknown,
    None when absent. The provider's own string is never recorded, whatever it contains."""
    present = False
    for value in values:
        if isinstance(value, str) and value:
            present = True
            token = value.strip().lower()
            if token in ERROR_CLASSES:
                return token
    return "unrecognized" if present else None


def _error_envelope(raw):
    """(validated, classified type/code) for an OpenAI-style error body; anything else is not a validated envelope."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False, None
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return False, None
    return True, _classify_error(error.get("type"), error.get("code"))


def _availability(status):
    if status == 401:
        return "credential_rejected"
    if status == 402:
        return "insufficient_balance"
    if status == 404:
        return "model_unavailable"
    if status == 429 or status >= 500:
        return "provider_unavailable"
    if status in DEFINITE_REJECTIONS:
        return "request_rejected"
    return "outcome_ambiguous"


def _bounded_tail(data, config, key):
    """The last max_wire_tail_bytes of `data` AFTER the configured key is redacted from the whole window, so replacement
    can never grow the stored tail past its bound. A key cut by the window's front edge cannot be matched whole; any
    leading bytes that form a proper suffix of the key are dropped so that no fragment of it is retained either."""
    limit = config["max_wire_tail_bytes"]
    tail = redact(bytes(data), key)[-limit:] if data else b""
    if key:
        secret = key.encode("ascii")
        for length in range(min(len(secret) - 1, len(tail)), 0, -1):
            if tail.startswith(secret[-length:]):
                tail = tail[length:]
                break
    return tail


def _retain_tail(artifacts_fd, data, config, key):
    """Keep at most max_wire_tail_bytes of the raw response as the private, key-redacted wire tail the specification
    retains for transport diagnosis (redacted first, then bounded). No tool ever returns it; a lost tail never changes
    the delivery verdict."""
    try:
        write_below(artifacts_fd, "wire-tail", _bounded_tail(data, config, key))
    except (OSError, AdapterError):
        pass


def _read_bounded(response, limit, deadline=None):
    data = bytearray()
    while len(data) <= limit:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("local deadline while reading the response body")
        chunk = response.read1(65536)
        if not chunk:
            break
        data += chunk
    return bytes(data[:limit])


def _clean_usage(usage):
    """Recognized usage counters only (non-negative integers under allowlisted names); None when nothing is recognized.
    Field names come from the provider, so only the fixed vocabulary is ever copied into public metadata."""
    clean = {}
    for key, value in usage.items():
        if key in USAGE_FIELDS and type(value) is int and 0 <= value <= MAX_USAGE_COUNTER:
            clean[key] = value
        elif key in USAGE_DETAIL_FIELDS and isinstance(value, dict):
            nested = {inner: number for inner, number in value.items()
                      if inner in USAGE_DETAIL_FIELDS[key] and type(number) is int and 0 <= number <= MAX_USAGE_COUNTER}
            if nested:
                clean[key] = nested
    return clean or None


class _Stream:
    """Incremental SSE consumer writing content and reasoning to private artifacts with every cap enforced."""

    def __init__(self, config, artifacts_fd, key, outcome):
        self.config, self.fd, self.key, self.outcome = config, artifacts_fd, key, outcome
        # The generic OpenAI-style backend may name its reasoning delta `reasoning`; DeepSeek's own is `reasoning_content`.
        self.openai_reasoning = BACKENDS[config["backend"]]["request_syntax"] != "deepseek_thinking"
        self.content = os.fdopen(create_below(artifacts_fd, "content"), "wb")
        try:
            self.reasoning = os.fdopen(create_below(artifacts_fd, "reasoning"), "wb")
        except BaseException:
            self.content.close()
            raise
        self.digest = hashlib.sha256()
        self.tail = bytearray()
        # The rolling window keeps one key length beyond the retained bound, so a key straddling the retained window's
        # front edge is still seen whole by the redaction that runs before the tail is bounded (_bounded_tail).
        self.tail_limit = config["max_wire_tail_bytes"] + (len(key.encode("utf-8")) if key else 0)
        self.buffer = b""
        self.content_bytes = self.reasoning_bytes = self.wire_bytes = 0
        self.finished = self.done = False
        self.error = None

    def feed(self, chunk):
        self.wire_bytes += len(chunk)
        if self.wire_bytes > self.config["max_wire_bytes"]:
            return self.fail(TRUNCATED, "wire_cap_exceeded")
        self.tail += chunk
        if len(self.tail) > self.tail_limit:
            del self.tail[:len(self.tail) - self.tail_limit]
        data = self.buffer + chunk if self.buffer else chunk
        limit = self.config["max_sse_line_bytes"]
        start = 0
        while self.error is None and not self.done:
            newline = data.find(b"\n", start)
            if newline < 0:
                break
            line = data[start:newline]
            start = newline + 1
            if len(line) > limit:
                return self.fail(FAILED_AFTER_SEND, "sse_line_too_long")
            self.line(line)
        self.buffer = data[start:]
        if self.error is None and len(self.buffer) > limit:
            return self.fail(FAILED_AFTER_SEND, "sse_line_too_long")
        return self.error

    def fail(self, state, code, error_type=None):
        if self.error is None:
            self.error = (state, code, error_type)
        return self.error

    def line(self, line):
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line or line.startswith(b":") or line.startswith((b"event:", b"id:", b"retry:")):
            return
        if not line.startswith(b"data:"):
            self.fail(FAILED_AFTER_SEND, "malformed_stream")
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.fail(FAILED_AFTER_SEND, "malformed_stream")
            return
        if not isinstance(chunk, dict):
            self.fail(FAILED_AFTER_SEND, "malformed_stream")
            return
        if isinstance(chunk.get("error"), dict):
            self.fail(FAILED_AFTER_SEND, "provider_error_in_stream", _classify_error(chunk["error"].get("type"), chunk["error"].get("code")))
            return
        model = chunk.get("model")
        if model is not None:
            if not isinstance(model, str) or model != self.config["model"]:
                # The differing value is provider-derived: it is never recorded as public metadata (it survives only in
                # the redacted private wire tail), so observed_model stays null and the code says why.
                self.outcome["observed_model"] = None
                self.fail(FAILED_AFTER_SEND, "model_mismatch")
                return
            self.outcome["observed_model"] = model
        choices = chunk.get("choices", [])
        if choices is None:
            choices = []
        if not isinstance(choices, list):
            self.fail(FAILED_AFTER_SEND, "malformed_stream")
            return
        finished_here = False
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(choice, dict) or (delta is not None and not isinstance(delta, dict)):
                self.fail(FAILED_AFTER_SEND, "malformed_stream")
                return
            delta = delta or {}
            if delta.get("tool_calls"):
                self.fail(FAILED_AFTER_SEND, "unsupported_tool_calls")
                return
            text, reasoning = delta.get("content"), delta.get("reasoning_content")
            if reasoning is None and self.openai_reasoning:
                reasoning = delta.get("reasoning")  # kept apart from content exactly like reasoning_content; never exposed
            if (text is not None and not isinstance(text, str)) or (reasoning is not None and not isinstance(reasoning, str)):
                self.fail(FAILED_AFTER_SEND, "malformed_stream")
                return
            if self.finished and (text or reasoning):
                self.fail(FAILED_AFTER_SEND, "content_after_finish")
                return
            try:
                encoded = text.encode("utf-8") if text else b""
                reasoning_encoded = reasoning.encode("utf-8") if reasoning else b""
            except UnicodeEncodeError:
                self.fail(FAILED_AFTER_SEND, "malformed_stream")  # for example a lone surrogate escape
                return
            if text:
                if self.content_bytes + len(encoded) > self.config["max_content_bytes"]:
                    self.fail(TRUNCATED, "content_cap_exceeded")  # the overflowing delta is neither written nor counted
                    return
                self.content.write(encoded)
                self.digest.update(encoded)
                self.content_bytes += len(encoded)
            if reasoning:
                if self.reasoning_bytes + len(reasoning_encoded) > self.config["max_reasoning_bytes"]:
                    self.fail(TRUNCATED, "reasoning_cap_exceeded")
                    return
                self.reasoning.write(reasoning_encoded)
                self.reasoning_bytes += len(reasoning_encoded)
            reason = choice.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str):
                    self.fail(FAILED_AFTER_SEND, "malformed_stream")
                    return
                self.outcome["finish_reason"] = reason if reason in FINISH_REASONS else "other"  # never a provider-derived string
                self.finished = finished_here = True
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            clean = _clean_usage(usage)
            if clean is not None:
                self.outcome["usage"] = clean
                self.outcome["usage_source"] = "final_chunk" if finished_here else "usage_only_chunk" if not any(
                    isinstance(choice, dict) and (choice.get("delta") or {}) for choice in choices) else "content_chunk"
            elif self.outcome["usage"] is None:
                # Usage arrived without a recognized counter: unknown, not zero. Counters already captured from an
                # earlier chunk are never erased by a later empty or unrecognized usage object.
                self.outcome["usage_source"] = "unrecognized"

    def close(self):
        """Flush and close both artifacts and retain the tail even when the first flush or fsync fails; the first
        failure is raised afterwards so the outcome still records artifact_write_failed."""
        failure = None
        for handle in (self.content, self.reasoning):
            try:
                if not handle.closed:
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                failure = failure or exc
            finally:
                with contextlib.suppress(OSError):
                    handle.close()
        _retain_tail(self.fd, self.tail, self.config, self.key)
        if failure is not None:
            raise failure


def _stage_timeout(config, deadline):
    """Socket timeout for one transport stage: the idle window, never beyond the fixed local deadline."""
    return max(0.001, min(config["read_idle_seconds"], deadline - time.monotonic()))


def _unsent_reason(deadline, should_cancel):
    """Why nothing may be sent now: the fixed deadline already elapsed, or a local cancellation is on record."""
    if time.monotonic() >= deadline:
        return "local_deadline_before_send"
    if should_cancel is not None and should_cancel():
        return "cancelled_before_send"
    return None


def _scrub(outcome, key):
    """Final backstop: no outcome field may carry the configured key, whatever the provider sent back."""
    if key:
        for name, value in outcome.items():
            if isinstance(value, str) and key in value:
                outcome[name] = "[REDACTED]"
    return outcome


def execute_request(config, key, body, artifacts_fd, deadline, should_cancel=None, tick=None, on_response=None):
    """Send one request and stream its response into private artifacts below `artifacts_fd`.

    Stages are distinguished truthfully. Before any byte is sent, the fixed deadline and a local cancellation are
    checked, and checked again after connecting because a connect can outlast the deadline: either yields a proven
    unsent outcome. A failure while connecting (TCP/TLS) is proven unsent; a failure while sending or waiting for
    headers is unknown delivery; anything after headers is at least failed_after_send. Every stage's socket timeout
    is the idle window bounded by the same fixed deadline, which is never reset or extended. `deadline` is a
    monotonic instant; cancellation is observed between reads, so a silent stream defers it by at most
    read_idle_seconds. Returns allowlisted facts only: classified codes, never provider prose or the key."""
    outcome = {"state": None, "error_code": None, "error_type": None, "detail": None, "http_status": None, "observed_model": None,
               "finish_reason": None, "usage": None, "usage_source": "missing", "content_bytes": 0, "reasoning_bytes": 0,
               "wire_bytes": 0, "content_sha256": None, "remote_outcome": "unknown", "possibly_billed": True, "availability": None}
    try:
        _execute(config, key, body, artifacts_fd, deadline, should_cancel, tick, on_response, outcome)
    finally:
        _scrub(outcome, key)
    return outcome


def _execute(config, key, body, artifacts_fd, deadline, should_cancel, tick, on_response, outcome):
    transport = BACKENDS[config["backend"]]  # the one fixed host, port and path this configuration may ever reach
    payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "text/event-stream",
               "User-Agent": SERVER_NAME + "/" + VERSION, "Content-Length": str(len(payload))}
    connection = None
    try:
        unsent = _unsent_reason(deadline, should_cancel)
        if unsent:
            outcome.update(state=NOT_STARTED, error_code=unsent, remote_outcome="not_sent", possibly_billed=False)
            return
        try:
            connection = _open_connection(transport["host"], transport["port"], tls_context(), _stage_timeout(config, deadline))
            connection.connect()
        except Exception as exc:  # TCP or TLS failure: no application byte was sent
            outcome.update(state=NOT_STARTED, error_code="transport_connect_failed", detail=type(exc).__name__,
                           remote_outcome="not_sent", possibly_billed=False)
            return
        unsent = _unsent_reason(deadline, should_cancel)  # connecting may have consumed the remaining time
        if unsent:
            outcome.update(state=NOT_STARTED, error_code=unsent, remote_outcome="not_sent", possibly_billed=False)
            return  # the connection closes below without a single request byte
        sock = getattr(connection, "sock", None)
        try:
            if sock is not None:  # sending is bounded by the same fixed deadline
                sock.settimeout(_stage_timeout(config, deadline))
            connection.request("POST", transport["path"], body=payload, headers=headers)
        except Exception as exc:
            outcome.update(state=UNKNOWN, error_code="transport_send_failed", detail=type(exc).__name__)
            return
        try:
            if sock is not None:  # headers must arrive within the idle window and before the fixed deadline
                sock.settimeout(_stage_timeout(config, deadline))
            response = connection.getresponse()
        except Exception as exc:
            outcome.update(state=UNKNOWN, error_code="transport_response_failed", detail=type(exc).__name__)
            return
        status = response.status
        outcome["http_status"] = status
        if 300 <= status < 400:
            outcome.update(state=FAILED_AFTER_SEND, error_code="redirect_refused")
            return
        if status != 200:
            try:
                raw = _read_bounded(response, config["max_sse_line_bytes"], deadline)
            except Exception as exc:
                outcome.update(state=FAILED_AFTER_SEND, error_code=f"http_{status}_unvalidated", detail=type(exc).__name__,
                               availability=_availability(status))
                return
            _retain_tail(artifacts_fd, raw, config, key)  # the error body survives only as the redacted private tail
            validated, error_class = _error_envelope(raw)
            outcome.update(error_type=error_class, availability=_availability(status))
            if not validated:
                # Without a validated envelope nothing proves generation never started: possibly billed, never replayed.
                outcome.update(state=FAILED_AFTER_SEND, error_code=f"http_{status}_unvalidated")
            elif status >= 500:
                # A server, gateway or overload response is ambiguous even with a well-formed envelope: the request
                # was delivered and generation may have started, so it stays potentially billed and unknown.
                outcome.update(state=FAILED_AFTER_SEND, error_code=f"http_{status}")
            elif status in DEFINITE_REJECTIONS:
                outcome.update(state=REJECTED, error_code=f"http_{status}", remote_outcome="rejected", possibly_billed=False)
            else:
                # 408 request timeout, 409 conflict and similar: a valid envelope does not prove a pre-generation refusal.
                outcome.update(state=FAILED_AFTER_SEND, error_code=f"http_{status}_ambiguous")
            return
        content_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "text/event-stream":
            outcome.update(state=FAILED_AFTER_SEND, error_code="unexpected_content_type")
            return
        if on_response is not None:
            on_response()  # streaming_at is stamped only for a 200 event stream; a rejection never carries one
        _consume(response, connection, config, key, artifacts_fd, deadline, should_cancel, tick, outcome)
    finally:
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()


def _consume(response, connection, config, key, artifacts_fd, deadline, should_cancel, tick, outcome):
    try:
        stream = _Stream(config, artifacts_fd, key, outcome)
    except (OSError, AdapterError) as exc:
        outcome.update(state=FAILED_AFTER_SEND, error_code="artifact_write_failed", detail=type(exc).__name__)
        return
    error = None
    try:
        while error is None and not stream.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = (ABANDONED_DEADLINE, "local_deadline", None)
                break
            if should_cancel is not None and should_cancel():
                error = (ABANDONED_CANCELLED, "cancelled", None)
                break
            if tick is not None:
                tick()
            sock = getattr(connection, "sock", None)
            if sock is not None:
                sock.settimeout(min(config["read_idle_seconds"], remaining))
            try:
                chunk = response.read1(65536)
            except TimeoutError:
                error = (ABANDONED_DEADLINE, "local_deadline", None) if deadline - time.monotonic() <= 0 else (FAILED_AFTER_SEND, "read_idle_timeout", None)
                break
            except http.client.IncompleteRead:
                error = (FAILED_AFTER_SEND, "stream_incomplete", None)  # premature EOF inside the chunked body
                break
            except (OSError, http.client.HTTPException) as exc:
                error = (FAILED_AFTER_SEND, "transport_read_failed", None)
                outcome["detail"] = type(exc).__name__
                break
            if not chunk:
                error = (FAILED_AFTER_SEND, "stream_incomplete", None)
                break
            try:
                error = stream.feed(chunk)
            except OSError as exc:
                error = (FAILED_AFTER_SEND, "artifact_write_failed", None)
                outcome["detail"] = type(exc).__name__
            except (ValueError, TypeError) as exc:  # never let a decoding surprise escape as a worker crash
                error = (FAILED_AFTER_SEND, "malformed_stream", None)
                outcome["detail"] = type(exc).__name__
    finally:
        try:
            stream.close()
        except OSError as exc:
            error = error or (FAILED_AFTER_SEND, "artifact_write_failed", None)
            outcome["detail"] = type(exc).__name__
    outcome.update(content_bytes=stream.content_bytes, reasoning_bytes=stream.reasoning_bytes, wire_bytes=stream.wire_bytes)
    if error is None:
        if not stream.finished:
            error = (FAILED_AFTER_SEND, "finish_missing", None)
        elif outcome["observed_model"] != config["model"]:
            # No chunk identified the exact configured model: the answer cannot be attributed to it, so it is never
            # completed. observed_model stays null; it is never inferred from the request.
            error = (FAILED_AFTER_SEND, "model_unverified", None)
        elif outcome["finish_reason"] == "stop":
            outcome.update(state=COMPLETED, remote_outcome="completed", content_sha256=stream.digest.hexdigest())
            return
        elif outcome["finish_reason"] == "tool_calls":
            error = (FAILED_AFTER_SEND, "unsupported_tool_calls", None)
        else:
            error = (TRUNCATED, "finish_" + outcome["finish_reason"], None)
    state, code, error_type = error
    outcome.update(state=state, error_code=code, error_type=error_type, content_sha256=stream.digest.hexdigest())
    if state == TRUNCATED:
        outcome["remote_outcome"] = "truncated"


def run_probe(adapter, prompt="Reply with exactly the single word OK and nothing else.", expected="OK"):
    """One tiny paid synthetic request with the pinned deep settings; writes a credential-free private receipt."""
    config = adapter.config
    transport = BACKENDS[config["backend"]]
    parameters = lane_parameters(config, "deep")
    user_text = assemble(prompt, None, [])
    body = build_body(config, parameters, user_text)
    input_bytes = len(FRAMING.encode("utf-8")) + len(user_text.encode("utf-8"))
    key = read_api_key(config["api_key_file"])
    name = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    probes_fd = open_directory(adapter.probes_dir, "probes_unsafe")
    try:
        artifacts_fd = directory_below(probes_fd, (name,), "probes_unsafe", create=True)
        try:
            started = time.monotonic()
            outcome = execute_request(config, key, body, artifacts_fd, started + config["request_timeout_seconds"])
            elapsed = round(time.monotonic() - started, 3)
            matched = None
            if outcome["state"] == COMPLETED:
                try:
                    matched = read_below(artifacts_fd, ("content",), config["max_content_bytes"]).decode("utf-8").strip() == expected
                except (AdapterError, UnicodeDecodeError):
                    matched = False
        finally:
            os.close(artifacts_fd)
        del key
        receipt = {"kind": "deepseek_probe", "adapter_version": VERSION, "recorded_at": now(), "room_id": adapter.room_id,
                   "backend": config["backend"], "endpoint": {"host": transport["host"], "path": transport["path"], "base_url": config["base_url"]},
                   "requested": {"backend": config["backend"], "model": config["model"], "request_syntax": transport["request_syntax"], **parameters},
                   "input_bytes": input_bytes,
                   "observed_model": outcome["observed_model"], "http_status": outcome["http_status"], "state": outcome["state"],
                   "finish_reason": outcome["finish_reason"], "usage": outcome["usage"], "usage_source": outcome["usage_source"],
                   "elapsed_seconds": elapsed, "content_bytes": outcome["content_bytes"], "reasoning_bytes": outcome["reasoning_bytes"],
                   "content_sha256": outcome["content_sha256"], "expected_answer_matched": matched, "error_code": outcome["error_code"],
                   "error_type": outcome["error_type"], "availability": outcome["availability"], "artifacts": name,
                   "meaning": "Records one probe attempt: state, http_status, observed_model, finish_reason and expected_answer_matched "
                              "describe its observed outcome. An unsuccessful or unknown outcome does not establish credential or "
                              "parameter acceptance. This tiny request does not measure how the hosted model applies the requested "
                              "effort, nor capacity, throughput, retention or quality"}
        write_json_below(probes_fd, name + ".json", receipt)
    finally:
        os.close(probes_fd)
    return receipt


PROBE_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\.json")
PROBE_FIELDS = ("kind", "adapter_version", "recorded_at", "room_id", "backend", "endpoint", "requested", "input_bytes", "observed_model", "http_status", "state",
                "finish_reason", "usage", "usage_source", "elapsed_seconds", "content_bytes", "reasoning_bytes", "content_sha256",
                "expected_answer_matched", "error_code", "error_type", "availability", "meaning")


def latest_probe(probes_dir):
    """Allowlisted facts of the newest private probe receipt, None when there is none, or an explicit unavailable
    marker when the directory holds more entries than the bounded scan reads: a partial enumeration of an unordered
    directory must never present a possibly stale receipt as the latest one. Reads at most one bounded file."""
    try:
        fd = open_directory(probes_dir, "probes_unsafe")
    except AdapterError:
        return None
    try:
        names, truncated = [], False
        try:
            with os.scandir(fd) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_PROBE_ENTRIES:
                        truncated = True
                        break
                    if PROBE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
        except OSError:
            return {"receipt": None, "unavailable": True, "unavailable_reason": "probe_directory_unreadable",
                    "meaning": "the probe directory could not be enumerated; no receipt is claimed"}
        if truncated:
            return {"receipt": None, "unavailable": True, "unavailable_reason": "probe_directory_exceeds_bound", "bound": MAX_PROBE_ENTRIES,
                    "meaning": "more entries than the bounded scan reads; the newest receipt cannot be identified truthfully"}
        if not names:
            return None
        newest = max(names)
        try:
            value = read_json_below(fd, (newest,), 65536, "probes_unsafe")
        except AdapterError:
            return {"receipt": newest, "unreadable": True}
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        return {"receipt": newest, "unreadable": True}
    return {"receipt": newest, **{field: value.get(field) for field in PROBE_FIELDS}}


# ---------------------------------------------------------------------------------------------------------------
# The per-room adapter: admission, detached workers, status, results, exports, health and resolution.
# ---------------------------------------------------------------------------------------------------------------

class _BoundedLog(io.RawIOBase):
    """Byte-bounded sink for the detached worker's stdout/stderr: the private worker.log never grows past its share
    of the per-job metadata budget. Excess output is dropped after one truncation marker; a write failure never
    raises, so logging can never change a delivery verdict."""
    MARKER = b"\n[worker.log truncated at its configured bound; later output dropped]\n"

    def __init__(self, fd, limit):
        super().__init__()
        self.fd, self.limit, self.written, self.truncated = fd, limit, 0, False

    def writable(self):
        return True

    def rebound(self, limit):
        self.limit = limit

    def _emit(self, data):
        view = memoryview(data)
        while view:
            try:
                count = os.write(self.fd, view)
            except OSError:
                return
            self.written += count
            view = view[count:]

    def write(self, data):
        data = bytes(data)
        room = self.limit - len(self.MARKER) - self.written
        if room > 0:
            self._emit(data[:room])
        if len(data) > max(room, 0) and not self.truncated:
            self.truncated = True
            if self.limit - self.written >= len(self.MARKER):  # a bound smaller than the marker stays exact, not exceeded
                self._emit(self.MARKER)
        return len(data)


def _bound_worker_log(limit):
    """Route the worker's stdout and stderr through one bounded sink on descriptor 2 (its private worker.log)."""
    sink = _BoundedLog(2, limit)
    stream = io.TextIOWrapper(sink, encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    sys.stdout = sys.stderr = stream
    return sink


def worker_command():
    """Interpreter plus this exact file: a room's copied snapshot spawns workers from its own copy."""
    return [sys.executable, str(Path(__file__).resolve())]


def read_inventory(room_root, room_id):
    """The immutable provider inventory recorded in the room's settings.json, read through owned descriptors."""
    fd = open_directory(room_root, "room_unsafe")
    try:
        settings = read_json_below(fd, ("settings.json",), MAX_SETTINGS_BYTES, "room_unsafe")
    finally:
        os.close(fd)
    inventory = settings.get("provider_inventory") if isinstance(settings, dict) else None
    if not isinstance(inventory, dict) or inventory.get("provider") != "deepseek" or not isinstance(inventory.get("files"), dict):
        raise AdapterError("inventory_missing", "The room records no DeepSeek provider inventory")
    if inventory.get("room_id") != room_id:
        raise AdapterError("inventory_room_mismatch", "The provider inventory belongs to a different room")
    return inventory


def _validate_lease(job_fd, lease_fd):
    """True only when the inherited descriptor is this job's worker.lock and its lease can be (re)asserted."""
    try:
        held = os.fstat(lease_fd)
        expected = os.stat("worker.lock", dir_fd=job_fd, follow_symlinks=False)
    except OSError:
        return False
    if (not stat.S_ISREG(expected.st_mode) or not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
            or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)):
        return False
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # idempotent on the already-locked inherited description
    except OSError:
        return False
    return True


class _Duplicate(Exception):
    """Internal control flow: an identical request_id/payload reuses its existing job without a second submission."""


def _unsent(code):
    return {"state": NOT_STARTED, "error_code": code, "error_type": None, "detail": None, "http_status": None, "observed_model": None,
            "finish_reason": None, "usage": None, "usage_source": "missing", "content_bytes": 0, "reasoning_bytes": 0, "wire_bytes": 0,
            "content_sha256": None, "remote_outcome": "not_sent", "possibly_billed": False, "availability": None}


class Adapter:
    def __init__(self, home, room_id, config_path, room_root=None, adapter_path=None):
        self.home = Path(home)
        if not self.home.is_absolute():
            raise AdapterError("home_invalid", "--home must be an absolute controller home")
        if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
            raise AdapterError("room_invalid", "The room identifier is invalid")
        self.room_id = room_id
        self.config_path = Path(config_path)
        self.config, self.config_sha256 = load_config(self.config_path, self.home)
        self.adapter_path = Path(adapter_path or __file__).resolve()
        self.adapter_sha256 = sha(_read_snapshot(self.adapter_path))  # owned, symlink-refusing and bounded, like every pinned read
        self.profile_sha256 = sha((self.adapter_sha256 + self.config_sha256).encode("ascii"))
        self.room_root = Path(room_root) if room_root else None
        self.integrity = self._verify_inventory()
        export_dir = self.integrity.get("export_dir")
        self.export_dir = Path(export_dir) if export_dir else self.home / "deepseek" / "exports" / self.room_id
        self.ledger = Ledger(self.home)
        self.jobs_dir = ensure_private_directory(self.home / "deepseek" / "jobs")
        self.probes_dir = ensure_private_directory(self.home / "deepseek" / "probes")

    # -- integrity -------------------------------------------------------------------------------------------

    def _verify_inventory(self, fresh=False):
        """Compare the snapshot against the room inventory. At startup the digests computed for this process are used;
        with `fresh`, the current file bytes are re-read so status and health report a change made after startup."""
        if self.room_root is None:
            return {"verified": False, "reason": "inventory_unavailable", "export_dir": None}
        try:
            inventory = read_inventory(self.room_root, self.room_id)
        except AdapterError as exc:
            return {"verified": False, "reason": exc.code, "export_dir": None}
        digests = {Path(path).name: digest for path, digest in inventory["files"].items() if isinstance(path, str) and isinstance(digest, str)}
        export_dir = inventory.get("export_dir")
        export_dir = export_dir if isinstance(export_dir, str) and os.path.isabs(export_dir) else None
        adapter_sha, config_sha = self.adapter_sha256, self.config_sha256
        if fresh:
            try:
                adapter_sha = sha(_read_snapshot(self.adapter_path))
                config_sha = sha(_read_snapshot(self.config_path, MAX_CONFIG_BYTES))
            except AdapterError:
                return {"verified": False, "reason": "snapshot_unreadable", "export_dir": export_dir}
        if digests.get(self.adapter_path.name) != adapter_sha:
            return {"verified": False, "reason": "adapter_source_mismatch", "export_dir": export_dir}
        if digests.get(self.config_path.name) != config_sha:
            return {"verified": False, "reason": "config_mismatch", "export_dir": export_dir}
        if export_dir is None:
            return {"verified": False, "reason": "export_dir_unrecorded", "export_dir": None}
        return {"verified": True, "reason": None, "export_dir": export_dir}

    # -- private storage -------------------------------------------------------------------------------------

    def _job_fd(self, job_id, create=False):
        jobs_fd = open_directory(self.jobs_dir, "jobs_unsafe")
        try:
            return directory_below(jobs_fd, (job_id,), "job_unsafe", create=create)
        finally:
            os.close(jobs_fd)

    @staticmethod
    def _retained(job_fd):
        total = 0
        try:
            with os.scandir(job_fd) as entries:
                for entry in entries:
                    with contextlib.suppress(OSError):
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
        except OSError:
            pass
        return total

    def _export_bytes(self, job_id, room_id):
        """Size of an export artifact carrying this job's name in the export directory of the room that owns the job
        (every room's directory is a sibling below the same exports root), reached through owned descriptors. It is
        counted whenever a job is relabelled without its worker, from whichever room's adapter observes it: a durable
        export written before the worker died is still that job's evidence. Accounted, never adopted (#31 stays out)."""
        if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
            return 0
        try:
            exports_fd = open_directory(self.export_dir.parent, "export_unsafe")
        except AdapterError:
            return 0
        try:
            try:
                room_fd = directory_below(exports_fd, (room_id,), "export_unsafe")
            except AdapterError:
                return 0
            try:
                metadata = os.stat(job_id + ".md", dir_fd=room_fd, follow_symlinks=False)
            except OSError:
                return 0
            finally:
                os.close(room_fd)
        finally:
            os.close(exports_fd)
        return metadata.st_size if stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid() else 0

    def _heartbeat_at(self, job_id):
        try:
            job_fd = self._job_fd(job_id)
        except AdapterError:
            return None
        try:
            value = read_json_below(job_fd, ("heartbeat.json",), 4096, "job_unsafe")
        except AdapterError:
            return None
        finally:
            os.close(job_fd)
        reported = value.get("reported_at") if isinstance(value, dict) else None
        return reported if isinstance(reported, str) and len(reported) <= 64 else None

    # -- lifecycle observation ---------------------------------------------------------------------------------

    def _refresh(self, row):
        """Relabel a lease-free active job after the startup grace; a lease is never probed inside that grace, a
        missing directory or lease file is never taken as evidence, and only the durable state decides the label."""
        if row["state"] not in ACTIVE_STATES:
            return row
        reference = row["streaming_at"] or row["submitting_at"] or row["created_at"]
        try:
            age = time.time() - parse_timestamp(reference).timestamp()
        except ValueError:
            return row
        if age <= STARTUP_GRACE_SECONDS:
            return row
        try:
            job_fd = self._job_fd(row["id"])
        except AdapterError:
            return row
        lease = None
        try:
            try:
                lease = os.open("worker.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=job_fd)
            except OSError:
                return row
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return row  # a worker holds its lease
            except OSError:
                return row  # the lease cannot be probed here; nothing is relabelled without proof
            with self.ledger.transaction() as db:
                current = db.execute("SELECT state FROM jobs WHERE id=?", (row["id"],)).fetchone()
                state = current["state"] if current else None
                # Everything the job left behind is counted: its private directory plus an export artifact under its
                # name (a worker that died after publishing but before its terminal commit). The export is accounted,
                # never adopted: the state below stays unknown_delivery and deepseek_result keeps refusing it.
                retained = self._retained(job_fd) + self._export_bytes(row["id"], row["room_id"])
                if state == QUEUED:
                    db.execute("UPDATE jobs SET state=?,finished_at=?,error_code='not_launched',remote_outcome='not_sent',possibly_billed=0,"
                               "reserved_bytes=0,retained_bytes=? WHERE id=?", (NOT_STARTED, now(), retained, row["id"]))
                elif state in (SUBMITTING, STREAMING):
                    db.execute("UPDATE jobs SET state=?,finished_at=?,error_code='worker_vanished',remote_outcome='unknown',possibly_billed=1,"
                               "reserved_bytes=0,retained_bytes=? WHERE id=?", (UNKNOWN, now(), retained, row["id"]))
        finally:
            if lease is not None:
                os.close(lease)
            os.close(job_fd)
        return self.ledger.job(row["id"])

    def _refresh_all(self):
        for row in self.ledger.active():
            self._refresh(row)

    def _reconcile_resolutions(self):
        """Complete resolution projections whose durable row exists but whose private file was never written."""
        with self.ledger.reading() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM resolutions WHERE room_id=?", (self.room_id,))]
        for record in rows:
            _write_resolution(self.jobs_dir, record["job_id"], record, only_if_missing=True)

    def _project(self, row, duplicate=None):
        state = row["state"]
        table = STATE_TABLE[state]
        resolved = self.ledger.resolved(row["id"]) if state in STOP_STATES else False
        usage = json.loads(row["usage_json"]) if row["usage_json"] else None
        start = row["submitting_at"] or row["created_at"]
        try:
            end = parse_timestamp(row["finished_at"]) if row["finished_at"] else dt.datetime.now(dt.timezone.utc)
            elapsed = max(0, math.floor((end - parse_timestamp(start)).total_seconds()))
        except ValueError:
            elapsed = None
        value = {"job_id": row["id"], "request_id": row["request_id"], "room_id": row["room_id"], "lane": row["lane"], "state": state,
                 "terminal": table["terminal"], "complete": state == COMPLETED, "requested_model": row["requested_model"],
                 "backend": self.config["backend"],  # this room's pinned transport; the ledger row itself names only the exact model
                 "observed_model": row["observed_model"], "thinking": row["thinking"], "reasoning_effort": row["reasoning_effort"],
                 "max_tokens": row["max_tokens"], "created_at": row["created_at"], "submitting_at": row["submitting_at"],
                 "streaming_at": row["streaming_at"], "finished_at": row["finished_at"], "deadline_at": row["deadline_at"],
                 "elapsed_seconds": elapsed, "usage": usage, "usage_source": row["usage_source"], "finish_reason": row["finish_reason"],
                 "http_status": row["http_status"], "error_code": row["error_code"], "error_type": row["error_type"],
                 "availability": row["availability"], "remote_outcome": row["remote_outcome"],
                 "possibly_billed": bool(row["possibly_billed"]), "input_bytes": row["input_bytes"], "content_bytes": row["content_bytes"],
                 "reasoning_bytes": row["reasoning_bytes"], "wire_bytes": row["wire_bytes"], "content_sha256": row["content_sha256"],
                 "content_path": row["content_path"], "export_reason": row["export_reason"],
                 "stops_room_lane": state in STOP_STATES and not resolved, "resolved": resolved,
                 "heartbeat_at": self._heartbeat_at(row["id"]) if state in ACTIVE_STATES else None, "meaning": table["meaning"]}
        if state in STOP_STATES and not resolved:
            value["resolve_command"] = self._resolve_syntax(row["id"])
        if duplicate is not None:
            value["duplicate"] = duplicate
        return value

    def _resolve_syntax(self, job_id):
        return (f"python3 {self.adapter_path} resolve --home {self.home} --room {self.room_id} --job {job_id} "
                "--note-file /absolute/private/path/to/decision.md  (run by the user at their own terminal)")

    # -- admission -------------------------------------------------------------------------------------------

    @staticmethod
    def _text(value, name, required=True, limit=MAX_TEXT_BYTES):
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
            raise AdapterError(name + "_invalid", f"{name} must be nonempty text of at most {limit} UTF-8 bytes")
        return value

    def submit(self, task, request_id, context=None, context_path=None, diagnosis=None, lane="deep", effort=None):
        self._text(task, "task")
        self._text(context, "context", required=False)
        self._text(diagnosis, "diagnosis", required=False, limit=MAX_DIAGNOSIS_BYTES)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise AdapterError("request_id_invalid", "request_id must match [A-Za-z0-9._-]{1,200}")
        if lane not in LANES:
            raise AdapterError("lane_invalid", "lane must be deep or ask")
        parameters = lane_parameters(self.config, lane, effort)
        files, worktree = [], None
        root = worktree_root()
        if context_path is not None:
            files, worktree = context_files(context_path, self.config, root)
        elif root is not None:
            with contextlib.suppress(AdapterError, OSError):
                fd = open_directory(root, "worktree_unsafe")
                try:
                    identity = os.fstat(fd)
                    worktree = {"path": str(root), "identity": [identity.st_dev, identity.st_ino]}
                finally:
                    os.close(fd)
        user_text = assemble(task, context, files)
        text_bytes = user_text.encode("utf-8")
        input_bytes = len(FRAMING.encode("utf-8")) + len(text_bytes)
        if input_bytes > self.config["max_input_bytes"]:
            raise AdapterError("input_too_large", f"The framed input is {input_bytes} bytes; the pinned limit is {self.config['max_input_bytes']} bytes. "
                               "Nothing is truncated silently: trim the task or context.", input_bytes=input_bytes, estimate=input_estimate(self.config))
        payload = {"room_id": self.room_id, "profile_sha256": self.profile_sha256, "lane": lane, "model": self.config["model"], **parameters,
                   "task": task, "context": context or "", "files": [{"path": item["path"], "sha256": item["sha256"], "bytes": item["bytes"]} for item in files],
                   "worktree": worktree}
        digest = sha(canonical(payload).encode("utf-8"))
        self._refresh_all()
        self._reconcile_resolutions()
        job_id = uuid.uuid4().hex
        reservation = reservation_bytes(self.config)
        # The admitted text is stored as the exact UTF-8 bytes that were measured against max_input_bytes (request-text);
        # the metadata record never embeds it, so JSON escaping of NUL, newline, quote or backslash characters cannot
        # inflate what is retained past the reservation or past what the worker may read back.
        request_record = {"job_id": job_id, "room_id": self.room_id, "request_id": request_id, "lane": lane, "parameters": parameters,
                          "model": self.config["model"], "backend": self.config["backend"], "base_url": self.config["base_url"],
                          "payload_sha256": digest, "input_bytes": input_bytes, "worktree": worktree,
                          "files": payload["files"], "user_text_bytes": len(text_bytes), "user_text_sha256": sha(text_bytes),
                          "diagnosis": diagnosis, "created_at": now()}
        record_bytes = (json.dumps(request_record, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(record_bytes) > MAX_REQUEST_RECORD_BYTES:
            raise AdapterError("request_metadata_too_large", f"The request metadata record (context paths and diagnosis, JSON-encoded) would exceed "
                               f"{MAX_REQUEST_RECORD_BYTES} bytes; name fewer or shorter context paths or shorten the diagnosis (JSON escaping "
                               "expands control characters)", record_bytes=len(record_bytes))
        job_fd = lease_fd = None
        duplicate = None
        try:
            with self.ledger.transaction() as db:
                existing = db.execute("SELECT * FROM jobs WHERE room_id=? AND request_id=?", (self.room_id, request_id)).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != digest:
                        raise AdapterError("request_id_conflict", "request_id was already used with different content", existing_job_id=existing["id"])
                    duplicate = dict(existing)
                    raise _Duplicate()
                stops = db.execute("SELECT id,state FROM jobs WHERE room_id=? AND state IN (?,?) AND id NOT IN (SELECT job_id FROM resolutions) ORDER BY created_at",
                                   (self.room_id, UNKNOWN, FAILED_AFTER_SEND)).fetchall()
                if stops:
                    raise AdapterError("room_stopped", "This room's DeepSeek lane is stopped by an unresolved " + stops[0]["state"] + " job; the user must resolve it at their terminal before new paid jobs. Nothing is resent.",
                                       job_id=stops[0]["id"], state=stops[0]["state"], resolve_command=self._resolve_syntax(stops[0]["id"]))
                same = db.execute("SELECT id,state FROM jobs WHERE room_id=? AND payload_sha256=? ORDER BY created_at DESC", (self.room_id, digest)).fetchall()
                for row in same:
                    if row["state"] not in (NOT_STARTED, REJECTED):
                        raise AdapterError("duplicate_payload", "An identical request already exists in this room; inspect it instead of paying twice",
                                           existing_job_id=row["id"], existing_state=row["state"])
                if any(row["state"] == REJECTED for row in same) and not diagnosis:
                    raise AdapterError("diagnosis_required", "The identical request was definitively rejected before generation; supply a nonempty diagnosis of the fix to submit it again",
                                       existing_job_id=next(row["id"] for row in same if row["state"] == REJECTED))
                room_active = db.execute("SELECT COUNT(*) FROM jobs WHERE room_id=? AND state IN (?,?,?)", (self.room_id, *ACTIVE_STATES)).fetchone()[0]
                host_active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN (?,?,?)", ACTIVE_STATES).fetchone()[0]
                if room_active >= self.config["max_concurrent_per_room"]:
                    raise AdapterError("busy", "This room already has its maximum number of queued or active DeepSeek jobs; wait on them first", scope="room")
                if host_active >= self.config["max_concurrent_per_host"]:
                    raise AdapterError("busy", "This host already runs its maximum number of DeepSeek jobs across rooms; wait for one to finish", scope="host")
                reserved = db.execute("SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs WHERE state IN (?,?,?)", ACTIVE_STATES).fetchone()[0]
                retained = db.execute("SELECT COALESCE(SUM(retained_bytes),0) FROM jobs").fetchone()[0]
                count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                if count >= self.config["max_jobs"]:
                    raise AdapterError("job_limit", "The ledger holds its maximum number of jobs; evidence is never deleted automatically")
                if reserved + retained + reservation > self.config["max_retained_bytes"]:
                    raise AdapterError("storage_exhausted", "Admitting this job would exceed the host retention quota; evidence is never deleted automatically")
                job_fd = self._job_fd(job_id, create=True)
                lease_fd = os.open("worker.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=job_fd)
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the exact job lease is held before queued is published
                # The durable row owns the job directory and reserves the worst case BEFORE any nontrivial artifact is
                # written: a crash between this commit and the request files leaves a queued, lease-free row whose
                # bytes the startup-grace relabel measures, never unowned bytes with no accounting.
                db.execute("INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,"
                           "reasoning_effort,max_tokens,input_bytes,reserved_bytes,retained_bytes,diagnosis,possibly_billed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,0)",
                           (job_id, self.room_id, request_id, lane, digest, self.profile_sha256, QUEUED, request_record["created_at"], self.config["model"],
                            parameters["thinking"], parameters["reasoning_effort"], parameters["max_tokens"], input_bytes, reservation, diagnosis))
            stage = "request_write_failed"
            try:
                # The worker reads both files back and cross-checks them (size and digest) before anything can be sent,
                # so a crash between these writes and the spawn leaves a proven-unsent job, never a partial request.
                write_below(job_fd, "request-text", text_bytes)
                write_below(job_fd, "request.json", record_bytes)
                stage = "spawn_failed"
                self._spawn_worker(job_id, job_fd, lease_fd)
            except (OSError, AdapterError) as exc:
                with self.ledger.transaction() as db:
                    db.execute("UPDATE jobs SET state=?,finished_at=?,error_code=?,remote_outcome='not_sent',possibly_billed=0,reserved_bytes=0,retained_bytes=? WHERE id=? AND state=?",
                               (NOT_STARTED, now(), stage, self._retained(job_fd), job_id, QUEUED))
                message = ("The local worker did not start: " if stage == "spawn_failed" else "The admitted request could not be stored: ") + type(exc).__name__ + "; nothing was sent"
                raise AdapterError(stage, message, job_id=job_id) from exc
        except _Duplicate:
            pass
        finally:
            if lease_fd is not None:
                os.close(lease_fd)  # the worker keeps the inherited lease; the parent's copy is released
            if job_fd is not None:
                os.close(job_fd)
        if duplicate is not None:
            return self._project(self._refresh(duplicate), duplicate=True)
        return {**self._project(self.ledger.job(job_id), duplicate=False), "input_estimate": input_estimate(self.config),
                "next": f"deepseek_status(job_id, wait=true, timeout_s<={MAX_WAIT_SECONDS}); then deepseek_result for a completed job"}

    def _spawn_worker(self, job_id, job_fd, lease_fd):
        log_fd = create_below(job_fd, "worker.log", 0o600)
        argv = [*worker_command(), "worker", "--home", str(self.home), "--room", self.room_id, "--config", str(self.config_path),
                "--job", job_id, "--lease-fd", str(lease_fd)]
        if self.room_root is not None:
            argv += ["--room-root", str(self.room_root)]
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, start_new_session=True,
                                       pass_fds=(lease_fd,), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        finally:
            os.close(log_fd)
        with self.ledger.transaction() as db:
            db.execute("UPDATE jobs SET worker_pid=? WHERE id=?", (process.pid, job_id))
        threading.Thread(target=process.wait, daemon=True).start()  # reap while this process lives; the worker is detached
        return process.pid

    def ask(self, question, request_id, context=None, effort=None, diagnosis=None):
        submitted = self.submit(question, request_id, context=context, diagnosis=diagnosis, lane="ask", effort=effort)
        value = self.status(submitted["job_id"], True, ASK_INLINE_WAIT_SECONDS)
        if value["state"] == COMPLETED:
            answer = self.result(value["job_id"], 0, MAX_RESULT_CHARS)
            value.update(answer=answer["text"], answer_truncated=answer["next_offset"] is not None, content_sha256=answer["content_sha256"])
        elif value["state"] in ACTIVE_STATES:
            value["next"] = f"deepseek_status(job_id, wait=true, timeout_s<={MAX_WAIT_SECONDS})"
        return value

    # -- observation tools -------------------------------------------------------------------------------------

    def status(self, job_id, wait=True, timeout_s=DEFAULT_WAIT_SECONDS):
        if wait is not True:
            raise AdapterError("wait_required", "deepseek_status requires wait=true; chain bounded waits instead of polling")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= MAX_WAIT_SECONDS:
            raise AdapterError("timeout_invalid", f"timeout_s must be greater than zero and at most {MAX_WAIT_SECONDS} seconds")
        row = self.ledger.job(job_id, self.room_id)
        self._reconcile_resolutions()
        deadline = time.monotonic() + timeout_s
        while True:
            row = self._refresh(row)
            if row["state"] in TERMINAL_STATES or time.monotonic() >= deadline:
                return {**self._project(row), "integrity": self._verify_inventory(fresh=True)}
            time.sleep(min(STATUS_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            row = self.ledger.job(job_id, self.room_id)

    def result(self, job_id, offset=0, max_chars=MAX_RESULT_CHARS):
        if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 0 < max_chars <= MAX_RESULT_CHARS:
            raise AdapterError("range_invalid", f"offset must be a non-negative integer and max_chars an integer between 1 and {MAX_RESULT_CHARS}")
        row = self.ledger.job(job_id, self.room_id)
        if row["state"] != COMPLETED:
            raise AdapterError("result_unavailable", "Only a completed job exposes an answer; this job is " + row["state"], state=row["state"])
        data = self._read_content(row)
        if row["content_sha256"] is None or sha(data) != row["content_sha256"]:
            raise AdapterError("content_changed", "The stored answer no longer matches its recorded digest; do not use it")
        text = data.decode("utf-8")
        chunk = text[offset:offset + max_chars]
        end = offset + len(chunk)
        return {"job_id": row["id"], "state": COMPLETED, "complete": True, "offset": offset, "text": chunk,
                "next_offset": end if end < len(text) else None, "total_chars": len(text), "content_bytes": len(data),
                "content_sha256": row["content_sha256"], "content_path": row["content_path"], "export_reason": row["export_reason"],
                "meaning": "read-only completed answer; consumers validate content_sha256 before applying anything and never treat it as verified"}

    def _read_content(self, row):
        """The completed answer bytes from the recorded location: the export artifact when content_path names it,
        otherwise the private copy. When the private copy is absent, the export artifact under this job's name is
        the only other place the single retained copy can be; the caller's digest check decides whether it is."""
        limit = self.config["max_content_bytes"]
        if row["content_path"]:
            path = Path(row["content_path"])
            if path.parent != self.export_dir or path.name != row["id"] + ".md":
                raise AdapterError("content_changed", "The recorded export path is not this room's export artifact")
            return self._read_export(path.name, limit)
        try:
            job_fd = self._job_fd(row["id"])
        except AdapterError as exc:
            if not exc.code.endswith("_missing"):
                raise
            job_fd = None  # the private directory is gone: the export artifact is the only remaining place
        if job_fd is not None:
            try:
                try:
                    return read_below(job_fd, ("content",), limit, "job_unsafe")
                except AdapterError as exc:
                    if not exc.code.endswith("_missing"):
                        raise
            finally:
                os.close(job_fd)
        return self._read_export(row["id"] + ".md", limit)

    def _read_export(self, name, limit):
        exports_fd = open_directory(self.export_dir.parent, "export_unsafe")
        try:
            return read_below(exports_fd, (self.export_dir.name, name), limit, "export_unsafe")
        finally:
            os.close(exports_fd)

    def cancel(self, job_id):
        row = self._refresh(self.ledger.job(job_id, self.room_id))
        if row["state"] not in ACTIVE_STATES:
            return {"job_id": job_id, "cancel_requested": False, "state": row["state"]}
        job_fd = self._job_fd(job_id)
        try:
            write_json_below(job_fd, "cancel.json", {"requested_at": now()})
        finally:
            os.close(job_fd)
        return {"job_id": job_id, "cancel_requested": True, "state": row["state"], "remote_outcome": "unknown", "possibly_billed": True,
                "note": "The owning worker closes its connection and records abandoned_cancelled; the provider may still finish and bill this request. Nothing is resent."}

    def _export_diagnostics(self):
        value = {"path": str(self.export_dir), "ok": False, "reason": None}
        try:
            metadata = os.lstat(self.export_dir)
        except FileNotFoundError:
            value["reason"] = "export_dir_missing"
            return value
        except OSError:
            value["reason"] = "export_dir_unreadable"
            return value
        if not stat.S_ISDIR(metadata.st_mode):
            value["reason"] = "export_dir_not_directory"
        elif metadata.st_uid != os.getuid():
            value["reason"] = "export_dir_foreign_owner"
        elif stat.S_IMODE(metadata.st_mode) & 0o077:
            value["reason"] = "export_dir_unsafe_mode"
        else:
            value["ok"] = True
        return value

    def health(self):
        """Configuration, integrity, key metadata, storage, admission and probe facts; no network, no key contents."""
        self._refresh_all()
        self._reconcile_resolutions()
        config = self.config
        storage = self.ledger.storage()
        stops = self.ledger.stopping_jobs(self.room_id)
        transport = BACKENDS[config["backend"]]
        return {"provider": "deepseek", "adapter_version": VERSION, "room_id": self.room_id, "model": config["model"], "base_url": config["base_url"],
                "backend": config["backend"], "backend_label": transport["label"], "request_syntax": transport["request_syntax"],
                "endpoint": {"host": transport["host"], "port": transport["port"], "path": transport["path"]},
                "backend_meaning": "provider names the delegate tool family; backend names the one fixed transport this configuration pins",
                "deep_lane": lane_parameters(config, "deep"), "ask_lane": {"effort": config["ask_effort"], **lane_parameters(config, "ask")},
                "context_tokens": config["context_tokens"], "context_basis": config["context_basis"], "input_limit": input_estimate(config),
                "bounds": {**{name: config[name] for name in INTEGER_FIELDS}, "worker_log_bytes": worker_log_limit(config)},
                "integrity": {**self._verify_inventory(fresh=True), "startup": self.integrity, "adapter_sha256": self.adapter_sha256,
                              "config_sha256": self.config_sha256, "profile_sha256": self.profile_sha256},
                "key_file": key_diagnostics(config["api_key_file"]), "export_dir": self._export_diagnostics(),
                "storage": {**storage, "max_retained_bytes": config["max_retained_bytes"], "max_jobs": config["max_jobs"], "reservation_per_job": reservation_bytes(config)},
                "active": {"room": len(self.ledger.active(self.room_id)), "host": len(self.ledger.active()),
                           "max_room": config["max_concurrent_per_room"], "max_host": config["max_concurrent_per_host"],
                           "meaning": "local owned-execution bounds; locally abandoned requests may still run remotely"},
                "room_stop": {"stopped": bool(stops), "jobs": [{"job_id": item["id"], "state": item["state"], "finished_at": item["finished_at"],
                              "resolve_command": self._resolve_syntax(item["id"])} for item in stops]},
                "latest_probe": latest_probe(self.probes_dir), "state_table": STATE_TABLE, "network": False,
                "text_only": "returns text; executes no returned code, shell or tool calls"}

    # -- worker ----------------------------------------------------------------------------------------------

    def run_worker(self, job_id, lease_fd):
        """The detached job worker: validates its inherited lease, commits submitting before any byte can be sent,
        streams, publishes completed content and records exactly one terminal state."""
        if not JOB_ID.fullmatch(str(job_id)):
            return 2
        job_fd = self._job_fd(job_id)
        try:
            if not _validate_lease(job_fd, lease_fd):
                return 2  # an invalid inherited descriptor never runs anything; status relabels the lease-free job
            try:
                row = self.ledger.job(job_id, self.room_id)
            except AdapterError:
                return 2
            if row["state"] != QUEUED:
                return 0
            execution_id = uuid.uuid4().hex
            beat = self._heartbeat(job_fd, job_id, execution_id)
            beat(True)
            if exists_below(job_fd, "cancel.json"):
                self._finish(job_id, job_fd, _unsent("cancelled_before_send"), execution_id)
                return 0
            if self.room_root is not None and not self.integrity["verified"]:
                self._finish(job_id, job_fd, _unsent("provider_inventory_mismatch"), execution_id)
                return 0
            try:
                parameters, user_text = self._load_request(job_fd)
            except AdapterError as exc:
                self._finish(job_id, job_fd, _unsent(exc.code), execution_id)
                return 0
            try:
                key = read_api_key(self.config["api_key_file"])
            except AdapterError as exc:
                self._finish(job_id, job_fd, _unsent(exc.code), execution_id)
                return 0
            body = build_body(self.config, parameters, user_text)
            if key in user_text or key in json.dumps(body, ensure_ascii=False):  # raw text first: escaping could hide a key holding quotes or backslashes
                del key
                self._finish(job_id, job_fd, _unsent("credential_in_payload"), execution_id)
                return 0
            timeout = self.config["request_timeout_seconds"]
            deadline_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=timeout)).isoformat()
            with self.ledger.transaction() as db:
                changed = db.execute("UPDATE jobs SET state=?,submitting_at=?,deadline_at=?,worker_pid=?,execution_id=?,possibly_billed=1 WHERE id=? AND state=?",
                                     (SUBMITTING, now(), deadline_at, os.getpid(), execution_id, job_id, QUEUED)).rowcount
            if changed != 1:
                return 0  # relabelled meanwhile; nothing may be sent under a state this worker does not own
            beat(True)

            def on_response():
                with self.ledger.transaction() as db:
                    db.execute("UPDATE jobs SET state=?,streaming_at=? WHERE id=? AND state=?", (STREAMING, now(), job_id, SUBMITTING))
                beat(True)

            outcome = execute_request(self.config, key, body, job_fd, time.monotonic() + timeout,
                                      lambda: exists_below(job_fd, "cancel.json"), beat, on_response)
            del key, body
            self._finish(job_id, job_fd, outcome, execution_id)
            return 0
        finally:
            os.close(job_fd)

    def _load_request(self, job_fd):
        """The admitted request exactly as the worker consumes it: the bounded metadata record plus the raw UTF-8 text
        file, read within the same limits admission enforced and cross-checked by size and digest. Any inconsistency
        is a proven-unsent request_unreadable; nothing is ever truncated or repaired."""
        try:
            record = read_json_below(job_fd, ("request.json",), MAX_REQUEST_RECORD_BYTES, "job_unsafe")
            text = read_below(job_fd, ("request-text",), self.config["max_input_bytes"], "job_unsafe")
        except AdapterError as exc:
            raise AdapterError("request_unreadable", "The admitted request record could not be read: " + exc.code) from exc
        parameters = record.get("parameters") if isinstance(record, dict) else None
        if (not isinstance(parameters, dict) or not all(key in parameters for key in ("thinking", "reasoning_effort", "max_tokens"))
                or record.get("user_text_bytes") != len(text) or record.get("user_text_sha256") != sha(text)):
            raise AdapterError("request_unreadable", "The admitted request record does not match its text")
        try:
            return parameters, text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterError("request_unreadable", "The admitted request text is not valid UTF-8") from exc

    def _heartbeat(self, job_fd, job_id, execution_id):
        last = [None]

        def beat(force=False):
            tick = time.monotonic()
            if not force and last[0] is not None and tick - last[0] < HEARTBEAT_SECONDS:
                return
            last[0] = tick
            with contextlib.suppress(OSError, AdapterError):
                write_json_below(job_fd, "heartbeat.json", {"job_id": job_id, "execution_id": execution_id, "reported_at": now(), "pid": os.getpid()})
        return beat

    def _finish(self, job_id, job_fd, outcome, execution_id):
        content_path = export_reason = None
        if outcome["state"] == COMPLETED:
            content_path, export_reason = self._publish(job_fd, job_id, outcome["content_sha256"])
        finished = now()
        meta = {"job_id": job_id, "execution_id": execution_id, "worker_pid": os.getpid(), "finished_at": finished, "content_path": content_path,
                "backend": self.config["backend"], "requested_model": self.config["model"],
                "export_reason": export_reason, **{key: outcome[key] for key in ("state", "error_code", "error_type", "detail", "http_status", "observed_model",
                "finish_reason", "usage", "usage_source", "content_bytes", "reasoning_bytes", "wire_bytes", "content_sha256", "remote_outcome",
                "possibly_billed", "availability")}}
        encoded = (json.dumps(meta, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) <= MAX_META_RECORD_BYTES:  # fixed allowlisted fields with bounded counters; the ledger row is authoritative
            with contextlib.suppress(OSError, AdapterError):
                write_below(job_fd, "meta.json", encoded)
        retained = self._retained(job_fd)
        if content_path:
            with contextlib.suppress(OSError):
                retained += os.lstat(content_path).st_size
        with self.ledger.transaction() as db:
            db.execute("UPDATE jobs SET state=?,finished_at=?,observed_model=?,finish_reason=?,usage_json=?,usage_source=?,http_status=?,error_code=?,"
                       "error_type=?,availability=?,remote_outcome=?,possibly_billed=?,content_bytes=?,reasoning_bytes=?,wire_bytes=?,content_sha256=?,"
                       "content_path=?,export_reason=?,retained_bytes=?,reserved_bytes=0 WHERE id=?",
                       (outcome["state"], finished, outcome["observed_model"], outcome["finish_reason"],
                        canonical(outcome["usage"]) if outcome["usage"] is not None else None, outcome["usage_source"], outcome["http_status"],
                        outcome["error_code"], outcome["error_type"], outcome["availability"], outcome["remote_outcome"], 1 if outcome["possibly_billed"] else 0,
                        outcome["content_bytes"], outcome["reasoning_bytes"], outcome["wire_bytes"], outcome["content_sha256"], content_path, export_reason,
                        retained, job_id))

    def _publish(self, job_fd, job_id, digest):
        """Publish the completed content into the room export directory as a 0400 file named by the opaque job ID.

        The ordering keeps exactly one durable authoritative copy at every failure and crash boundary. The
        export name is hard-linked to the private content inode (no bytes are duplicated), fixed to 0400 and made
        durable with a directory fsync BEFORE the private name is removed. Any failure up to that point rolls the
        export name back and leaves the private copy authoritative (content_path null, export_reason set); once the
        export is durable it is authoritative and removing the private name is best effort (a stale private name
        would share the inode, so nothing is duplicated). A worker that dies between this publication and the ledger
        commit leaves that one durable copy on disk for inspection: the relabel to unknown_delivery counts its bytes,
        and retrievability through deepseek_result is only guaranteed once the terminal ledger row is committed.
        Nothing is ever overwritten and no symlink is followed."""
        name = job_id + ".md"
        try:
            exports_fd = open_directory(self.export_dir.parent, "export_unsafe")
        except AdapterError:
            return None, "export_dir_missing"
        try:
            try:
                room_fd = directory_below(exports_fd, (self.export_dir.name,), "export_unsafe")
            except AdapterError as exc:
                return None, "export_dir_missing" if exc.code.endswith("_missing") else "export_dir_unsafe"
            try:
                if stat.S_IMODE(os.fstat(room_fd).st_mode) & 0o077:
                    return None, "export_dir_unsafe"  # the room's --add-dir grant assumes a private directory; never chmod here
                linked = False
                try:
                    os.link("content", name, src_dir_fd=job_fd, dst_dir_fd=room_fd, follow_symlinks=False)
                    linked = True
                except FileExistsError:
                    try:
                        existing = read_below(room_fd, (name,), self.config["max_content_bytes"], "export_unsafe")
                    except (AdapterError, OSError):
                        return None, "export_conflict"  # an artifact that cannot be verified identical is never overwritten
                    if sha(existing) != digest:
                        return None, "export_conflict"
                except OSError:
                    return None, "export_unavailable"
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=room_fd)
                    try:
                        os.fchmod(fd, 0o400)
                    finally:
                        os.close(fd)
                    os.fsync(room_fd)
                except OSError:
                    self._rollback_export(room_fd, name, job_fd, linked)
                    return None, "export_unavailable"
                # The export is durable and authoritative from here on; the private name goes away best effort.
                with contextlib.suppress(OSError):
                    os.unlink("content", dir_fd=job_fd)
                with contextlib.suppress(OSError):
                    os.fsync(job_fd)
                return str(self.export_dir / name), None
            finally:
                os.close(room_fd)
        finally:
            os.close(exports_fd)

    @staticmethod
    def _rollback_export(room_fd, name, job_fd, linked):
        """Undo a publication that did not become durable: drop the export name this call created and restore the
        private copy's mode, so the job keeps its single authoritative copy in its own directory."""
        if linked:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=room_fd)
        with contextlib.suppress(OSError):
            fd = os.open("content", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=job_fd)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)


def _encode_resolution(record, note=True):
    """The exact bytes of a resolution projection; without `note` the text is omitted and the omission is stated."""
    value = {key: record[key] for key in ("job_id", "room_id", "note_sha256", "created_at")}
    if note:
        value["note"] = record["note"]
    else:
        value.update(note=None, note_omitted="encoded_record_exceeds_bound")
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _write_resolution(jobs_dir, job_id, record, only_if_missing=False):
    """Project a durable resolution row into the job directory within MAX_RESOLUTION_RECORD_BYTES. A projection that
    already holds exactly these bytes is left alone (no atomic temporary is ever created beside it), and a record whose
    encoding exceeds the bound is projected without its note text: the ledger row is authoritative and the note's
    digest still identifies it. resolve_job refuses such a note before it is recorded; this path is the backstop."""
    encoded = _encode_resolution(record)
    if len(encoded) > MAX_RESOLUTION_RECORD_BYTES:
        encoded = _encode_resolution(record, note=False)
    try:
        jobs_fd = open_directory(jobs_dir, "jobs_unsafe")
    except AdapterError:
        return False
    try:
        try:
            job_fd = directory_below(jobs_fd, (job_id,), "job_unsafe")
        except AdapterError:
            return False  # the ledger row is authoritative; a missing directory needs no projection
        try:
            if exists_below(job_fd, "resolution.json"):
                if only_if_missing:
                    return False
                with contextlib.suppress(AdapterError):
                    if read_below(job_fd, ("resolution.json",), MAX_RESOLUTION_RECORD_BYTES, "job_unsafe") == encoded:
                        return True  # already projected exactly; nothing is rewritten
            write_below(job_fd, "resolution.json", encoded)
            return True
        finally:
            os.close(job_fd)
    finally:
        os.close(jobs_fd)


def resolve_job(home, room_id, job_id, note, interactive=None):
    """CLI-only user resolution of one exact unknown_delivery or failed_after_send job: records the user's actual
    decision idempotently, lifts only that job's room stop, and never resends, deletes or relabels anything."""
    interactive = os.isatty(0) if interactive is None else interactive
    if not interactive:
        raise AdapterError("tty_required", "resolve must run in the user's own interactive terminal")
    if not Path(home).is_absolute():
        raise AdapterError("home_invalid", "--home must be an absolute controller home")
    if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
        raise AdapterError("room_invalid", "The room identifier is invalid")
    if not isinstance(note, str) or not note.strip():
        raise AdapterError("note_required", "resolve requires the user's actual decision text acknowledging the unknown remote outcome and possible billing")
    if len(note.encode("utf-8")) > MAX_NOTE_BYTES:
        raise AdapterError("note_too_large", f"The decision note must be at most {MAX_NOTE_BYTES} bytes")
    digest, stamp = sha(note.encode("utf-8")), now()
    encoded = len(_encode_resolution({"job_id": str(job_id), "room_id": room_id, "note_sha256": digest, "created_at": stamp, "note": note}))
    if encoded > MAX_RESOLUTION_RECORD_BYTES:
        raise AdapterError("note_too_large", f"The decision note encodes to a {encoded}-byte JSON record; the bound is {MAX_RESOLUTION_RECORD_BYTES} "
                           "bytes (control characters, quotes and backslashes expand under JSON escaping)", record_bytes=encoded)
    ledger = Ledger(home)
    row = ledger.job(job_id, room_id)
    if row["state"] not in STOP_STATES:
        raise AdapterError("resolve_not_eligible", "Only an unknown_delivery or failed_after_send job in this room can be resolved; this job is " + row["state"], state=row["state"])
    with ledger.transaction() as db:
        existing = db.execute("SELECT * FROM resolutions WHERE job_id=?", (job_id,)).fetchone()
        if existing is None:
            db.execute("INSERT INTO resolutions(job_id,room_id,note_sha256,note,created_at) VALUES(?,?,?,?,?)", (job_id, room_id, digest, note, stamp))
            record, duplicate = {"job_id": job_id, "room_id": room_id, "note_sha256": digest, "created_at": stamp, "note": note}, False
        else:
            record, duplicate = {key: existing[key] for key in ("job_id", "room_id", "note_sha256", "created_at", "note")}, True
    _write_resolution(Path(home) / "deepseek" / "jobs", job_id, record)
    return {"job_id": job_id, "room_id": room_id, "state": row["state"], "resolved": True, "duplicate": duplicate, "created_at": record["created_at"],
            "note_sha256": record["note_sha256"], "original_state_unchanged": True, "remote_outcome": "unknown", "possibly_billed": True, "resent": False,
            "meaning": "lifts only this job's room-admission stop; the identical payload stays deduplicated"}


# ---------------------------------------------------------------------------------------------------------------
# Stdio MCP surface: stdout carries newline-delimited JSON-RPC only.
# ---------------------------------------------------------------------------------------------------------------

S = {"type": "string"}
TOOLS = {
    "deepseek_submit": ("Submit one self-contained deep task to the exact configured model on this room's pinned backend (the official "
                        "DeepSeek API or DeepInfra, named by deepseek_health) with the pinned deep settings "
                        "(thinking enabled, configured reasoning_effort, pinned max_tokens; none can be lowered here). Returns a durable "
                        "job_id; identical request_id/payload reuses the job and an identical payload under a new request_id is refused, so "
                        "never resubmit to poll. context_path names explicit files beneath the verified worktree only. Text only: nothing "
                        "returned is executed. diagnosis is required only to resubmit a payload that was definitively rejected before generation.",
                       {"type": "object", "additionalProperties": False, "required": ["task", "request_id"],
                        "properties": {"task": S, "context": S, "context_path": {"type": ["string", "array"], "items": S, "maxItems": DEFAULTS["max_context_files"]},
                                       "request_id": S, "diagnosis": S}}),
    "deepseek_ask": ("Quick text question on the same durable job machinery with a small output budget and effort none (thinking disabled) "
                     "or low; waits inline up to 45 seconds and returns the answer when completed, otherwise the job to wait on.",
                     {"type": "object", "additionalProperties": False, "required": ["question", "request_id"],
                      "properties": {"question": S, "context": S, "request_id": S, "effort": {"type": "string", "enum": ["none", "low"]}, "diagnosis": S}}),
    "deepseek_status": ("Wait (wait=true required) up to timeout_s<=49 seconds (default 45) on a saved job and read allowlisted ledger facts: "
                        "state, requested/observed model, pinned effort/output, usage when reported (missing usage is unknown, not zero), "
                        "elapsed/deadline, finish reason, byte counts and error codes. Never resubmits or extends a deadline.",
                       {"type": "object", "additionalProperties": False, "required": ["job_id"],
                        "properties": {"job_id": S, "wait": {"type": "boolean", "const": True},
                                       "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_WAIT_SECONDS}}}),
    "deepseek_result": ("Read a bounded chunk (max_chars<=32768) of a completed job's answer with next_offset, plus content_path/content_sha256 "
                        "of the content-only export artifact in this room's granted export directory. Never reasoning, never arbitrary files; "
                        "truncated or unverified output is not exposed as a completed answer.",
                       {"type": "object", "additionalProperties": False, "required": ["job_id"],
                        "properties": {"job_id": S, "offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 32768}}}),
    "deepseek_cancel": ("Ask the owning worker to close its connection. The provider may still finish and bill the request; the job records "
                        "abandoned_cancelled with remote_outcome unknown, never a remote cancellation.",
                       {"type": "object", "additionalProperties": False, "required": ["job_id"], "properties": {"job_id": S}}),
    "deepseek_health": ("Read the pinned backend and endpoint, configuration, integrity, key-file metadata (never contents), export directory, storage, admission counts, room "
                        "stop state with the user-run resolve syntax, and the latest probe facts. No network, no model call.",
                       {"type": "object", "additionalProperties": False, "properties": {}}),
}
INSTRUCTIONS = ("DeepSeek-model text delegate for Fable on this room's one pinned backend (official DeepSeek API or DeepInfra; deepseek_health names "
                "it). Submit self-contained tasks with a stable request_id, keep the returned job_id, wait with "
                "deepseek_status in bounded calls, read completed answers with deepseek_result or the exported content file after validating "
                "its digest. Deep effort and output budgets are pinned; nothing returned is executed; unknown delivery stops this room's "
                "lane until the user resolves it at their terminal.")


def error_response(identifier, code, message):
    if identifier is not None and (not isinstance(identifier, (str, int, float)) or isinstance(identifier, bool)
                                   or (isinstance(identifier, float) and not math.isfinite(identifier))):
        identifier = None
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def call_tool(adapter, name, arguments):
    if name not in TOOLS:
        raise AdapterError("tool_unknown", "Unknown tool " + str(name))
    if not isinstance(arguments, dict):
        raise AdapterError("arguments_invalid", "arguments must be an object")
    schema = TOOLS[name][1]
    missing = set(schema.get("required", [])) - set(arguments)
    extra = set(arguments) - set(schema["properties"])
    if missing or extra:
        raise AdapterError("arguments_invalid", f"Invalid arguments; missing={sorted(missing)}, unexpected={sorted(extra)}")
    if name == "deepseek_submit":
        return adapter.submit(arguments["task"], arguments["request_id"], context=arguments.get("context"),
                              context_path=arguments.get("context_path"), diagnosis=arguments.get("diagnosis"))
    if name == "deepseek_ask":
        return adapter.ask(arguments["question"], arguments["request_id"], context=arguments.get("context"),
                           effort=arguments.get("effort"), diagnosis=arguments.get("diagnosis"))
    if name == "deepseek_status":
        return adapter.status(arguments["job_id"], arguments.get("wait", True), arguments.get("timeout_s", DEFAULT_WAIT_SECONDS))
    if name == "deepseek_result":
        return adapter.result(arguments["job_id"], arguments.get("offset", 0), arguments.get("max_chars", MAX_RESULT_CHARS))
    if name == "deepseek_cancel":
        return adapter.cancel(arguments["job_id"])
    return adapter.health()


def handle(message, adapter):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return error_response(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid JSON-RPC request")
    identifier, method = message.get("id"), message["method"]
    if "id" not in message:
        return None  # notifications are one-way
    if isinstance(identifier, (dict, list, bool)) or (isinstance(identifier, float) and not math.isfinite(identifier)):
        return error_response(None, -32600, "Invalid request id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error_response(identifier, -32602, "params must be an object")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2024-11-05",
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": SERVER_NAME, "version": VERSION}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        readonly = {"deepseek_status", "deepseek_result", "deepseek_health"}
        result = {"tools": [{"name": name, "description": description, "inputSchema": schema,
                             "annotations": {"readOnlyHint": name in readonly, "destructiveHint": False,
                                             "openWorldHint": name in ("deepseek_submit", "deepseek_ask")}}
                            for name, (description, schema) in TOOLS.items()]}
    elif method == "tools/call":
        try:
            value = call_tool(adapter, params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}], "structuredContent": value, "isError": False}
        except AdapterError as exc:
            result = {"content": [{"type": "text", "text": json.dumps(exc.payload(), ensure_ascii=False)}], "isError": True}
        except Exception as exc:  # never leak paths, prompts or keys from an unexpected failure
            result = {"content": [{"type": "text", "text": json.dumps({"error": "adapter failure: " + type(exc).__name__, "code": "internal"})}], "isError": True}
    else:
        return error_response(identifier, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _finite(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def _emit(value, indent=None):
    """UTF-8 JSON on the binary stdout: neither the MCP transport nor the CLI depends on the inherited text encoding."""
    encoded = (json.dumps(value, indent=indent, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(encoded.decode("utf-8"))
        sys.stdout.flush()
        return
    buffer.write(encoded)
    buffer.flush()


def serve(adapter):
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            while line and not line.endswith(b"\n"):
                line = stdin.readline(MAX_LINE + 1)
            response = error_response(None, -32600, "Request exceeds maximum size")
        else:
            try:
                message = json.loads(line, parse_float=_finite, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
                response = handle(message, adapter)
            except (ValueError, UnicodeDecodeError, RecursionError):
                response = error_response(None, -32700, "Invalid JSON")
        if response is not None:
            _emit(response)


# ---------------------------------------------------------------------------------------------------------------
# CLI: serve (MCP), worker (internal), set-key, probe, resolve, health.
# ---------------------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "worker", "probe", "health"):
        command = commands.add_parser(name)
        command.add_argument("--home", required=True)
        command.add_argument("--room", required=True)
        command.add_argument("--config", required=True)
        command.add_argument("--room-root")
        if name == "worker":
            command.add_argument("--job", required=True)
            command.add_argument("--lease-fd", type=int, required=True)
    key = commands.add_parser("set-key")
    key.add_argument("--home", required=True)
    target = key.add_mutually_exclusive_group()
    target.add_argument("--config", help="absolute path of the key-free provider configuration whose backend and api_key_file the key is for")
    target.add_argument("--backend", choices=sorted(BACKENDS), help="store the key at this backend's default private file under --home (default: official)")
    key.add_argument("--rotate", action="store_true")
    resolve = commands.add_parser("resolve")
    for name in ("home", "room", "job", "note-file"):
        resolve.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "set-key":
            home = Path(args.home)
            if not home.is_absolute():
                raise AdapterError("home_invalid", "--home must be absolute")
            if args.config:
                config = load_config(args.config, home)[0]
                path, backend = config["api_key_file"], config["backend"]
            else:
                backend = args.backend or DEFAULT_BACKEND
                path = str(home / "secrets" / BACKENDS[backend]["key_name"])
            result = {"backend": backend, **set_key(home, path, rotate=args.rotate, label=BACKENDS[backend]["key_label"])}
        elif args.command == "resolve":
            note_path = Path(args.note_file)
            if not note_path.is_absolute():
                raise AdapterError("note_invalid", "--note-file must be an absolute path")
            parent_fd = open_directory(note_path.parent, "note_unsafe")
            try:
                note = read_below(parent_fd, (note_path.name,), MAX_NOTE_BYTES, "note_unsafe").decode("utf-8")
            finally:
                os.close(parent_fd)
            result = resolve_job(args.home, args.room, args.job, note)
        else:
            log = _bound_worker_log(worker_log_limit(DEFAULTS)) if args.command == "worker" else None
            adapter = Adapter(args.home, args.room, args.config, args.room_root)
            if log is not None:
                log.rebound(worker_log_limit(adapter.config))
            if args.command == "serve":
                if args.room_root is not None and not adapter.integrity["verified"]:
                    print("deepseek adapter: provider inventory mismatch (" + str(adapter.integrity["reason"]) + "); refusing to serve", file=sys.stderr)
                    return 3
                serve(adapter)
                return 0
            if args.command == "worker":
                return adapter.run_worker(args.job, args.lease_fd)
            if args.command == "probe":
                # A deliberate, explicit CLI action (exactly one paid synthetic request) usable by authorized automation
                # as well as the user; only set-key and resolve need the user's own terminal. Never automatic, never a tool.
                if args.room_root is not None and not adapter.integrity["verified"]:
                    raise AdapterError("inventory_mismatch", "provider inventory mismatch (" + str(adapter.integrity["reason"]) + "); refusing to probe")
                result = run_probe(adapter)
            else:
                result = adapter.health()
        _emit(result, indent=2)
        return 0
    except AdapterError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": "adapter failure: " + type(exc).__name__, "code": "internal"}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
