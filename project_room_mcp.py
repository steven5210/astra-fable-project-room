#!/usr/bin/env python3
"""Local stdio MCP interface. Stdout is exclusively newline-delimited JSON-RPC."""

import json
import math
from pathlib import Path
import sys

RUNTIME = None
INPUT_CWD = None
if __name__ == "__main__":
    # The plugin installer may remove its old cache while this connection (or
    # a detached legacy worker) is still alive. Pin files before lazy imports.
    sys.dont_write_bytecode = True
    INPUT_CWD = Path.cwd()
    from project_room_runtime import RuntimeRetentionError, activate
    try:
        RUNTIME = activate(__file__)
    except (OSError, RuntimeRetentionError) as exc:
        sys.stderr.write("Project Room MCP runtime could not be retained: " + str(exc) + "\n")
        raise SystemExit(1)

import project_room

RELATIVE_PATH_ARGUMENTS = {
    "room_open": "project_path", "room_list": "project_path",
    "ao_room_open": "project_path", "ao_room_list": "project_path",
    "ao_room_prepare": "worktree_path", "ao_room_handoff": "worktree_path",
    "ao_room_verify": "candidate_path",
}


def input_arguments(name, arguments):
    """Keep supported caller-relative paths anchored before runtime chdir.

    Native evidence paths already require exact absolute, non-symlink inputs;
    changing those would weaken their validators. Text, gate argv, profiles,
    identifiers, invalid types and API-import behavior remain untouched.
    """
    if INPUT_CWD is None or not isinstance(name, str) or not isinstance(arguments, dict):
        return arguments
    field = RELATIVE_PATH_ARGUMENTS.get(name)
    value = arguments.get(field) if field else None
    # Empty room_list paths mean an unfiltered listing. Other path APIs reject
    # blanks; normalization must not turn an invalid blank into a valid path.
    if not isinstance(value, str) or not value or (name != "room_list" and not value.strip()):
        return arguments
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return arguments
    return {**arguments, field: str(INPUT_CWD / expanded)}


MAX_LINE = 3_000_000
INSTRUCTIONS = (
    "AO rooms use ao_room_*: Fable owns normal engineering/delegation; Astra owns product/spec and independent acceptance. "
    "An Astra-led exception requires actual per-task authorization. Prepare private delegates before native Fable launch, "
    "bind exact roles/models, obtain Fable acceptance of the exact spec, then hand off. Pin gates; send once, sync, verify, "
    "then accept the exact independent reviewer verdict. Usage is an attributable native subtotal, not quota. "
    "The following rules apply to legacy room_* rooms, which never migrate automatically: "
    "Project Room: Astra owns grounded requirements, versioned specs, issue dispositions, and product-outcome review. "
    "Fable owns technical design, implementation planning, delegates, and engineering verdicts. "
    "Open the existing project/feature room; load status/history. Submit once with a stable request_id; wait on its job_id "
    "in <=45-second calls. Resolve each finding before exact-revision handoff. Use existing user authorization; "
    "bring meaningful product tradeoffs to the user. Never replay uncertain jobs. "
    "Scope changes return to Astra. Encourage useful enhancements, track their proposal and filed issue link, "
    "and surface their benefit and tradeoff for the user's opinion and scope approval before implementation. "
    "Fable's delegates follow each room's pinned provider policy: a DeepSeek room routes self-contained work to the "
    "text-only deepseek_* tools with pinned max effort and output, a legacy qwen room keeps its Qwen ladder, and a "
    "room without a provider routes among Claude tiers only; existing rooms never migrate silently. "
    "The project-room skill supplies the workflow."
)


def error_response(identifier, code, message):
    if identifier is not None and (not isinstance(identifier, (str, int, float))
                                   or isinstance(identifier, bool)
                                   or (isinstance(identifier, float) and not math.isfinite(identifier))):
        identifier = None
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def handle(message, service):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return error_response(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid JSON-RPC request")
    identifier = message.get("id")
    method = message["method"]
    if "id" not in message:
        return None  # MCP notifications are one-way; background job cancellation has an explicit tool.
    if isinstance(identifier, (dict, list, bool)) or (isinstance(identifier, float) and not math.isfinite(identifier)):
        return error_response(None, -32600, "Invalid request id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return error_response(identifier, -32602, "params must be an object")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2024-11-05",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "astra-fable-project-room", "version": RUNTIME["version"] if RUNTIME else "0.3.0"}, "instructions": INSTRUCTIONS}
        if RUNTIME:
            result["_meta"] = {"project-room/runtime": RUNTIME}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        readonly = {"room_doctor", "room_list", "room_status", "room_job_status", "room_history", "room_implementation_audit", "room_verification_audit", "ao_room_status", "ao_room_list"}
        result = {"tools": [{"name": name, "description": description, "inputSchema": schema,
                             "annotations": {"readOnlyHint": name in readonly, "destructiveHint": False,
                                             "openWorldHint": name in ("room_doctor", "room_review_submit", "room_implementation_submit", "ao_room_send", "ao_room_verify")}}
                            for name, (description, schema) in project_room.TOOL_SCHEMAS.items()]}
    elif method == "tools/call":
        try:
            name = params.get("name")
            value = service.call(name, input_arguments(name, params.get("arguments", {})))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}],
                      "structuredContent": value, "isError": False}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True}
    else:
        return error_response(identifier, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def main():
    service = project_room.Service()
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            while line and not line.endswith(b"\n"):
                line = sys.stdin.buffer.readline(MAX_LINE + 1)
            response = error_response(None, -32600, "Request exceeds maximum size")
        else:
            try:
                message = json.loads(line, parse_float=finite_float, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
                response = handle(message, service)
            except (ValueError, UnicodeDecodeError, RecursionError):
                response = error_response(None, -32700, "Invalid JSON")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
