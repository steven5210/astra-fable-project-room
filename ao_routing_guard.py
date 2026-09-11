"""Private, deny-only PreToolUse guard for one prepared Project Room engineer.

This file is copied alone into private controller state (content-addressed) and
executed by Claude's hook shell for the prepared worktree, so it uses only the
stdlib. It reads one PreToolUse event from stdin and either prints a deny decision
(exit 0) or prints nothing (exit 0). It never grants a permission, so ordinary
permission handling still applies to every call it does not deny. Any error exits
2, which Claude treats as a block: a broken, missing or misfed guard fails closed.
"""

import json
import sys

PINNED_AGENTS = ("pr-sonnet", "pr-opus")
BROWSER_AGENT = "pr-opus"
BROWSER_SKILL = "claude-in-chrome"  # the exact skill observed in the prior Opus browser task
AGENT_KEYS = frozenset({"description", "prompt", "subagent_type", "run_in_background"})
DENIED_TOOLS = ("Workflow", "Task", "SendMessage")
# MCP servers that submit delegate or room work; only the root engineer may use them.
SUBMISSION_PREFIXES = ("mcp__deepseek__", "mcp__qwen-local__", "mcp__project-room__")
MAX_EVENT_BYTES = 1_000_000


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
    # Any present identity field proves the event fired inside a native worker;
    # an id without a usable type is ambiguous nested dispatch and stays denied.
    inside = agent_type is not None or agent_id is not None
    if tool == "Agent":
        if inside:
            return "native workers cannot delegate further (one layer)"
        if params.get("subagent_type") not in PINNED_AGENTS:
            return "only the pinned pr-sonnet and pr-opus agents may be launched; omitted, built-in, fork or unknown types are refused"
        extra = sorted(str(key) for key in params if key not in AGENT_KEYS)
        if extra:
            return "Agent parameters " + ", ".join(extra) + " are refused (no model override, isolation, resume or team routing)"
        return None
    if tool in DENIED_TOOLS or tool.startswith("Team"):
        return tool + " routes (workflows, teams, continuation or messaging) are not authorized in this bounded routing configuration"
    if tool == "Skill":
        if inside and agent_type == BROWSER_AGENT and params.get("skill") == BROWSER_SKILL:
            return None
        return "skill dispatch is refused except the pinned browser skill inside pr-opus"
    if inside and tool.startswith(SUBMISSION_PREFIXES):
        return "native workers cannot submit delegate or room work; only the root engineer uses the pinned provider tools"
    return None


def main():
    if len(sys.argv) > 1:
        raise ValueError("the routing guard takes no arguments")
    raw = sys.stdin.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("hook event is oversized")
    reason = decide(json.loads(raw))
    if reason is not None:
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": "Project Room routing guard: " + reason}}, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - every failure must block the call
        print("Project Room routing guard error: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
