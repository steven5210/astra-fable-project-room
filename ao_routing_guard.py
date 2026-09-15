"""Private, deny-only PreToolUse guard for one prepared Project Room engineer.

This file is copied alone into private controller state (content-addressed) and
executed by Claude's hook shell for the prepared worktree, so it uses only the
stdlib. It reads one PreToolUse event from stdin and either prints a deny decision
(exit 0) or prints nothing (exit 0). It never grants a permission, so ordinary
permission handling still applies to every call it does not deny. Any error exits
2, which Claude treats as a block: a broken, missing or misfed guard fails closed.
"""

import json
import os
import re
import stat
import sys
import uuid

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
QUOTA_HOOK_FIELDS = ("session_id", "transcript_path", "cwd")
NEW_WORK_TOOLS = frozenset({"Agent", "mcp__deepseek__deepseek_submit", "mcp__deepseek__deepseek_ask"})
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


def _transcript_tail(path):
    """Read an owned regular file without following any path component symlink."""
    parts = _absolute_parts(path)
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
            # Root-owned sticky system directories such as /private/tmp are safe
            # ancestors of an owned private directory; shared writable directories
            # without that protection are not.
            sticky_system = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            if (metadata.st_uid not in (0, os.getuid())
                    or metadata.st_mode & 0o022 and not sticky_system):
                raise ValueError("native quota transcript directory ownership is unsafe")
        leaf = _open_component(parts[-1], leaf_flags, directory)
        before = os.fstat(leaf)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o022 or before.st_nlink != 1):
            raise ValueError("native quota transcript must be an owned private regular file")
        start = max(0, before.st_size - MAX_TRANSCRIPT_BYTES)
        # One preceding byte establishes whether the first retained line is whole.
        offset = start - 1 if start else 0
        raw = os.pread(leaf, before.st_size - offset, offset)
        after = os.fstat(leaf)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) != before.st_size - offset or identity(before) != identity(after):
            raise ValueError("native quota transcript changed during inspection")
        if start:
            preceding, raw = raw[:1], raw[1:]
            if preceding != b"\n":
                _, separator, raw = raw.partition(b"\n")
                if not separator:
                    raw = b""
        return raw
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(directory)


def _root_record(row, session_id, cwd):
    return (row.get("sessionId") == session_id and row.get("cwd") == cwd
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


def _account_quota_error(row):
    """Only native typed API failures can establish quota; tool/result text cannot."""
    message = row.get("message")
    if (row.get("type") != "assistant" or row.get("isApiErrorMessage") is not True
            or row.get("error") != "rate_limit" or not isinstance(message, dict)
            or message.get("role") != "assistant" or message.get("model") != "<synthetic>"):
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
        if not _root_record(row, session_id, cwd):
            continue
        if _human_caller(row):
            if not isinstance(row.get("uuid"), str) or not row["uuid"]:
                raise ValueError("native quota caller lacks its identity")
            return {"status": "quota_in_current_turn" if errors else "clear_current_turn",
                    "caller_uuid": row["uuid"], "error_uuids": list(reversed(errors))}
        if _account_quota_error(row):
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
        return _quota_denial(event)
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
