#!/usr/bin/env python3
"""Stdio MCP proxy enforcing Qwen budgets; --config accepts a server or full config.

The full form is {"mcpServers": {"qwen-local": {"command": ..., "args": [],
"env": {}}}}. Only that server is launched, directly without a shell.
"""
import argparse
import asyncio
import json
import math
import os
import sys

MAX_LINE = 4_000_000


class GuardError(Exception):
    pass


def parse_message(line):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        message = json.loads(line, object_pairs_hook=object_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise GuardError("invalid MCP message") from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise GuardError("invalid MCP message")
    if "id" in message and (isinstance(message["id"], bool) or
                            not isinstance(message["id"], (str, int, type(None)))):
        raise GuardError("invalid MCP message ID")
    if "method" in message:
        if (not isinstance(message["method"], str) or "result" in message or "error" in message
                or ("params" in message and not isinstance(message["params"], (dict, list)))):
            raise GuardError("invalid MCP request")
    elif "id" not in message or ("result" in message) == ("error" in message):
        raise GuardError("invalid MCP response")
    elif "error" in message:
        error = message["error"]
        if (not isinstance(error, dict) or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)):
            raise GuardError("invalid MCP error response")
    return message


def enforce(message):
    """Return (forwarded_message, rejection); never mutate the caller's object."""
    if message.get("method") != "tools/call":
        return message, None
    params = message.get("params")
    if "id" not in message or not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise GuardError("invalid tools/call request")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise GuardError("invalid tool arguments")
    arguments = dict(arguments)
    name = params["name"]
    reason = None
    if name == "qwen_submit":
        arguments.setdefault("effort", "xhigh")
        arguments.setdefault("max_tokens", 131072)
        if arguments["effort"] != "xhigh" or type(arguments["max_tokens"]) is not int or arguments["max_tokens"] != 131072:
            reason = "qwen_submit requires effort=xhigh and max_tokens=131072."
        # The server's schema uses effort, not reasoning_effort. Reject ambiguous overrides.
        if "reasoning_effort" in arguments and arguments["reasoning_effort"] != "xhigh":
            reason = "qwen_submit requires reasoning effort xhigh; use the effort parameter."
    elif name == "qwen_ask":
        arguments.setdefault("effort", "low")
        if arguments["effort"] not in ("none", "low"):
            reason = "qwen_ask permits only effort=none or effort=low."
    elif name == "qwen_status":
        arguments.setdefault("wait", True)
        arguments.setdefault("timeout_s", 45)
        timeout = arguments["timeout_s"]
        if arguments["wait"] is not True:
            reason = "qwen_status requires wait=true."
        elif type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 49:
            reason = "qwen_status timeout_s must be greater than zero and at most 49 seconds."
    if reason:
        return None, {"jsonrpc": "2.0", "id": message["id"], "result": {
            "isError": True, "content": [{"type": "text", "text": reason}]}}
    return {**message, "params": {**params, "arguments": arguments}}, None


def load_server(path):
    try:
        with open(path, encoding="utf-8") as source:
            config = json.load(source)
        if "mcpServers" in config:
            config = config["mcpServers"]["qwen-local"]
        command, args, extra_env = config["command"], config.get("args", []), config.get("env", {})
        if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise ValueError()
        if not isinstance(extra_env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in extra_env.items()):
            raise ValueError()
        return [command, *args], {**os.environ, **extra_env}
    except (OSError, ValueError, KeyError, TypeError):
        raise GuardError("cannot load Qwen server configuration") from None


def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()


async def run(path):
    command, env = load_server(path)
    try:
        child = await asyncio.create_subprocess_exec(*command, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_LINE)
    except (OSError, ValueError):
        raise GuardError("cannot start Qwen backend") from None
    tasks = []
    transport = None
    try:
        source = asyncio.StreamReader(limit=MAX_LINE)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(source), sys.stdin.buffer)

        async def from_client():
            while line := await source.readline():
                if not line.endswith(b"\n"):
                    raise GuardError("incomplete client MCP message")
                forward, rejection = enforce(parse_message(line))
                if rejection:
                    emit(rejection)
                else:
                    child.stdin.write((json.dumps(forward, allow_nan=False) + "\n").encode())
                    await child.stdin.drain()
            child.stdin.close()

        async def from_backend():
            while line := await child.stdout.readline():
                if not line.endswith(b"\n"):
                    raise GuardError("incomplete backend MCP message")
                emit(parse_message(line))

        incoming = asyncio.create_task(from_client())
        outgoing = asyncio.create_task(from_backend())
        tasks = [incoming, outgoing]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if incoming not in done:
            raise GuardError("Qwen backend disconnected")
        await asyncio.wait_for(outgoing, timeout=3)
        code = await asyncio.wait_for(child.wait(), timeout=3)
        if code:
            raise GuardError("Qwen backend exited unsuccessfully")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if transport is not None:
            transport.close()
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=2)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config))
    except (GuardError, OSError, ValueError, asyncio.TimeoutError):
        # Never echo upstream payloads, configuration, environment, or stderr.
        print("qwen guard: MCP transport or policy configuration failure", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
