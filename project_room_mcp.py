#!/usr/bin/env python3
"""Local stdio MCP interface. Stdout is exclusively newline-delimited JSON-RPC."""

import json
import math
import sys

import project_room

MAX_LINE = 3_000_000
INSTRUCTIONS = (
    "Project Room: Astra owns grounded requirements, versioned specs, issue dispositions, and product-outcome review. "
    "Fable owns technical design, implementation planning, delegates, and engineering verdicts. "
    "Open the existing project/feature room; load status/history. Submit once with a stable request_id; wait on its job_id "
    "in <=45-second calls. Resolve each finding before exact-revision handoff. Use existing user authorization; "
    "bring meaningful product tradeoffs to the user. Never replay uncertain jobs. "
    "Scope changes return to Astra; optional enhancements go to backlog. The project-room skill supplies the workflow."
)


def error_response(identifier, code, message):
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
                  "serverInfo": {"name": "astra-fable-project-room", "version": "0.2.0"}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        readonly = {"room_doctor", "room_list", "room_status", "room_job_status", "room_history"}
        result = {"tools": [{"name": name, "description": description, "inputSchema": schema,
                             "annotations": {"readOnlyHint": name in readonly, "destructiveHint": False,
                                             "openWorldHint": name in ("room_doctor", "room_review_submit", "room_implementation_submit")}}
                            for name, (description, schema) in project_room.TOOL_SCHEMAS.items()]}
    elif method == "tools/call":
        try:
            value = service.call(params.get("name"), params.get("arguments", {}))
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
