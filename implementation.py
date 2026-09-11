#!/usr/bin/env python3
"""Authorized, isolated implementation handoffs following an exact room agreement."""

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import uuid

import room
import session_paths


class ImplementationError(Exception):
    pass


class ModelSpawnError(ImplementationError):
    """No child process exists: the launch failed, or the invocation was terminated before any process was created.
    `reason` is the allowlisted refusal code (model_spawn_failure or cancelled_before_launch)."""

    def __init__(self, message, reason="model_spawn_failure"):
        super().__init__(message)
        self.reason = reason


class LaunchUnknown(ImplementationError):
    """The invocation was interrupted while the process was being created; whether a child exists is unknown."""


COMMON_POLICY = """Delegates share none of your context: give self-contained specs, anchors, interfaces,
acceptance criteria, and verification. Diagnose failures before escalation; fix input
gaps and retry the same tier. Escalate true capability misses with evidence. After two
failed tiers on one subtask, Fable takes over. Record every route, escalation and fix,
including actual delegate model when available; never invent an actual model.
Plan reference/ground-truth test probes before changes. Verify returned code anchors,
types and interfaces. After every code change run the usual adversarial/code-review
flow available in this session. Report any missing review capability as a gap.
No delegate self-certifies. Your report is evidence for separate gates and Astra review.
Tools run under automatic permission checks without permission prompts; never bypass
those checks. Report denied or unavailable required tools and review capabilities as
remaining gaps. Do not claim a delegate or code-review flow ran when unavailable.
Work only in the isolated worktree. Do not deploy, push, merge, change other checkouts,
change git administrative data, or weaken security/model/Qwen policies. Do not commit;
leave the candidate changes available for independent review and later integration.
Do not modify handoff records, configuration, or evidence outside the worktree.
Propose grounded useful enhancements in backlog with their expected benefit and
tradeoff. Astra files or links proposal issues for the user's opinion and scope
approval outside this delegate session; implementation waits for approval.
"""

# Legacy Qwen policy: byte-identical to the text pinned by earlier handoffs, so a qwen room keeps its policy hash.
POLICY_QWEN = """Fable is the implementation orchestrator and owns engineering judgments.
Quality always beats token savings. Use the cheapest delegate only when it delivers
full quality: Qwen for self-contained specified work, Sonnet for mechanical agentic
work, Opus for bounded judgment, Fable for cross-cutting judgment and final review.
Where subagents are unavailable, use Qwen/Fable and report that limitation.
Qwen3.8-27B's intended server window is 262144 tokens. Every qwen_submit uses
effort=xhigh and max_tokens=131072, never less; 131072 remains for task/context/system.
Use context_path for large contexts and retain the upstream prompt-budget precheck.
qwen_ask alone permits none/low effort. qwen_status uses wait=true and bounded waits
under 50 seconds, chaining waits instead of polling. Access Qwen only via the configured
qwen-local guard; never bypass it through Bash or a direct upstream connection.
""" + COMMON_POLICY

POLICY_DEEPSEEK = """Fable is the implementation orchestrator and owns engineering judgments.
Quality always beats token savings. Use the cheapest delegate only when it delivers
full quality: DeepSeek (deepseek_submit) for self-contained specified work such as
implementation, tests and reviews against verifiable specs, and for bounded module
design, debugging or review when its demonstrated quality warrants it; Sonnet for
mechanical agentic work; Opus for bounded judgment; Fable for cross-cutting judgment
and final review. Where subagents are unavailable, use DeepSeek/Fable and report it.
DeepSeek is a text delegate: it returns code, tests, reviews and reasoning summaries
but executes nothing, edits no files and invokes no tools; Sonnet applies and verifies.
Every deepseek_submit runs the exact configured model with thinking enabled, the
room's pinned reasoning effort and pinned output budget (max and 393216 tokens by
default; the packet's delegate_settings carry the exact pinned values); tool calls
cannot lower them, and nobody changes the room's snapshot as a workaround.
Give the delegate the full relevant context and never trim it to save its tokens.
deepseek_ask alone permits effort none or low. context_path names explicit files
beneath the verified worktree only. deepseek_status uses wait=true and bounded waits
of at most 49 seconds, chaining waits instead of polling; keep the durable job_id and
never resubmit to poll. Read completed answers with deepseek_result or the exported
content file after validating its digest; truncated or unverified output is never an
accepted answer. Size deep tasks to the remaining implementation window in the packet;
a detached job may outlive this session and a later authorized attempt can read it
without resubmitting. Unknown delivery stops the room's DeepSeek lane until the user
resolves it at their own terminal; never work around it. Cite job_id in routing
records; token facts come from the ledger. Never invoke local Qwen in this room. If
DeepSeek is unavailable or rejects the pinned parameters, report it and route to an
appropriate Claude tier, recording why; never substitute another model silently.
""" + COMMON_POLICY

POLICY_NONE = """Fable is the implementation orchestrator and owns engineering judgments.
Quality always beats token savings. This room pins no delegate provider: use Sonnet
for mechanical agentic work, Opus for bounded judgment, and Fable for cross-cutting
judgment and final review. Where subagents are unavailable, Fable does the work and
reports that limitation. Never invoke a Qwen or DeepSeek tool; none is configured here.
""" + COMMON_POLICY

POLICIES = {"none": POLICY_NONE, "qwen": POLICY_QWEN, "deepseek": POLICY_DEEPSEEK}
POLICY = POLICY_QWEN  # legacy name
PROVIDERS = ("none", "qwen", "deepseek")
INVENTORY_FILE_LIMIT = 8 * 1024 * 1024

ROUTE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "task": {"type": "string"}, "tier": {"type": "string", "enum": ["qwen", "deepseek", "sonnet", "opus", "fable"]},
    "requested_model": {"type": "string"}, "actual_model": {"type": "string"},
    "reason": {"type": "string"}, "result": {"type": "string"},
    "fixes": {"type": "array", "items": {"type": "string"}}, "escalation": {"type": "string"}},
    "required": ["task", "tier", "requested_model", "actual_model", "reason", "result", "fixes", "escalation"]}
REPORT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "summary": {"type": "string"}, "spec_revision": {"type": "integer"}, "spec_sha256": {"type": "string"},
    "baseline_commit": {"type": "string"}, "implementation_complete": {"type": "boolean"},
    "outcome": {"type": "string", "enum": ["completed", "needs_changes", "scope_change"]},
    "scope_change": {"type": "string"}, "backlog": {"type": "array", "items": {"type": "string"}},
    "routing_log": {"type": "array", "items": ROUTE_SCHEMA},
    **{key: {"type": "array", "items": {"type": "string"}} for key in
       ("changes", "tests_reported", "review_findings", "remaining_gaps")}},
    "required": ["summary", "spec_revision", "spec_sha256", "baseline_commit", "implementation_complete",
                 "routing_log", "changes", "tests_reported", "review_findings", "remaining_gaps", "outcome", "scope_change", "backlog"]}


def _digest(value):
    return room.sha(room.canonical(value).encode("utf-8"))


def _atomic(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git(project, *args):
    result = subprocess.run(["git", "-C", str(project), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        raise ImplementationError("Git operation failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _flag(args, flag):
    for index, value in enumerate(args):
        if value == flag:
            return args[index + 1] if index + 1 < len(args) else None
        if value.startswith(flag + "="):
            return value.split("=", 1)[1]
    return None


def _config(path):
    original = json.loads(Path(path).read_text())
    value = room.load_config(path)
    if value["model"] != "claude-fable-5-1" or value["expected_model_ids"] != ["claude-fable-5-1"]:
        raise ImplementationError("Implementation requires exact claude-fable-5-1 primary identity")
    args = value["extra_args"]
    singletons = {"--permission-mode", "--effort", "--setting-sources", "--settings", "--permission-prompts",
                  "--tools", "--allowedTools", "--disallowedTools", "--agents", "--append-system-prompt-file", "--system-prompt-file"}
    seen = set()
    for arg in args:
        key = arg.split("=", 1)[0]
        key = {"--allowed-tools": "--allowedTools", "--disallowed-tools": "--disallowedTools"}.get(key, key)
        if key in singletons:
            if key in seen:
                raise ImplementationError("Duplicate security/identity configuration option: " + key)
            seen.add(key)
    if _flag(args, "--effort") != "max":
        raise ImplementationError("Implementation requires --effort max")
    forbidden = {"--bare", "--bg", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
    if any(arg.split("=", 1)[0] in forbidden for arg in args):
        raise ImplementationError("Implementation configuration contains an unsupported bypass/background flag")
    if _flag(args, "--permission-mode") != "auto":
        raise ImplementationError("Implementation requires --permission-mode auto; permission bypasses are unsupported")
    for key in ("--allowedTools", "--allowed-tools"):
        for index, arg in enumerate(args):
            if arg == key or arg.startswith(key + "="):
                grants = [arg.split("=", 1)[1]] if "=" in arg else []
                for following in args[index + 1:]:
                    if following.startswith("-"):
                        break
                    grants.append(following)
                if any("Bash" in grant or grant.strip() == "*" for grant in grants):
                    raise ImplementationError("Explicit Bash allow grants are unsupported; use automatic permission checks")
    if "--strict-mcp-config" not in args or _flag(args, "--setting-sources") != "":
        raise ImplementationError("Implementation requires strict MCP configuration and empty ambient setting sources")
    settings = _flag(args, "--settings")
    settings = json.loads(settings) if settings and settings.lstrip().startswith("{") else json.loads(Path(settings).read_text()) if settings else {}
    if settings.get("disableAllHooks") is not True:
        raise ImplementationError("Implementation requires disableAllHooks=true")
    grants = settings.get("permissions", {}).get("allow", []) if isinstance(settings.get("permissions", {}), dict) else []
    if any(isinstance(grant, str) and ("Bash" in grant or grant.strip() == "*") for grant in grants):
        raise ImplementationError("Settings may not grant unrestricted Bash access")
    if _flag(args, "--permission-prompts") != "none":
        raise ImplementationError("Noninteractive implementation requires --permission-prompts none")
    gate_timeout = original.get("gate_timeout_seconds", 300)
    if type(gate_timeout) not in (int, float) or not math.isfinite(gate_timeout) or gate_timeout <= 0:
        raise ImplementationError("gate_timeout_seconds must be positive and finite")
    transcript = original.get("session_transcript_path")
    if transcript is not None and (not isinstance(transcript, str) or "{session_id}" not in transcript):
        raise ImplementationError("session_transcript_path must contain {session_id}")
    config_dir = str(Path(original.get("claude_config_dir", Path.home() / ".claude")).expanduser().resolve())
    override = original.get("claude_config_dir_override", os.environ.get("CLAUDE_CONFIG_DIR"))
    if override is not None and (not isinstance(override, str) or not override):
        raise ImplementationError("claude_config_dir_override must be a nonempty original override or null")
    return {**value, "gate_timeout_seconds": gate_timeout, "session_transcript_path": transcript,
            "claude_config_dir": config_dir, "claude_config_dir_override": override}


def _mcp_servers(config):
    """Server names launched by the pinned --mcp-config values of an implementation profile: inline JSON or files,
    collecting every value of every occurrence exactly as room.load_config does, so no launched server is overlooked."""
    args = config["extra_args"]
    values, index = [], 0
    while index < len(args):
        option, equals, inline = args[index].partition("=")
        index += 1
        if option != "--mcp-config":
            continue
        if equals:
            values.append(inline)
            continue
        while index < len(args) and not args[index].startswith("--"):
            values.append(args[index])
            index += 1
    servers = {}
    references = config.get("referenced_files_sha256") if isinstance(config.get("referenced_files_sha256"), dict) else {}
    for value in values:
        try:
            if value.lstrip().startswith("{"):
                loaded = json.loads(value)
            else:
                # The same bytes room.load_config hashed decide the provider: read owned, bounded and symlink-refusing,
                # then compared with the recorded digest so a file replaced in between is a refusal, not a silent choice.
                import recovery
                reference = Path(value).expanduser()
                data = recovery.read_owned(reference, INVENTORY_FILE_LIMIT, "handoff", root=reference.parent)
                expected = references.get(str(reference))
                if expected is not None and room.sha(data) != expected:
                    raise ImplementationError("Implementation MCP configuration changed since the profile was loaded")
                loaded = json.loads(data)
        except (OSError, ValueError, RecursionError) as exc:
            raise ImplementationError("Implementation MCP configuration is unreadable") from exc
        except Exception as exc:
            if isinstance(exc, ImplementationError):
                raise
            raise ImplementationError("Implementation MCP configuration is unreadable") from exc
        found = loaded.get("mcpServers") if isinstance(loaded, dict) else None
        if not isinstance(found, dict):
            raise ImplementationError("Implementation MCP configuration must contain an mcpServers object")
        servers.update(found)
    return servers


def _read_inventory_file(path):
    """Bytes of a snapshotted provider file, read like every other pinned input: descriptor-relative below the room
    directory (the trusted prefix two levels up), O_NOFOLLOW on each component, user-owned, regular and bounded."""
    import recovery
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ImplementationError("Provider inventory paths must be absolute")
    target = Path(path)
    if len(target.parts) < 4 or any(part in (".", "..") for part in target.parts):
        raise ImplementationError("Provider inventory paths must lie below a room directory")
    try:
        return recovery.read_owned(target, INVENTORY_FILE_LIMIT, "handoff", root=target.parent.parent)
    except recovery.ObservationError as exc:
        if exc.reason.endswith("_missing"):
            raise FileNotFoundError(path) from exc
        raise ImplementationError("Provider inventory file is not an owned regular file within bounds: " + exc.reason) from exc


def verify_provider_inventory(inventory, repair_export_dir=True):
    """(mismatch code or None, {pinned path: verified bytes}) for a pinned DeepSeek inventory.

    Every snapshotted file is read descriptor-relatively and compared with its pinned digest; the verified bytes are
    returned so a caller that needs the pinned configuration (the packet builder) reuses exactly the bytes that
    passed rather than a second read that could be tampered with. Paths are pinned as strings; the export directory
    is verified by identity at each use (an existing user-owned, non-symlink directory with no group/other permission
    bits, exactly what the room's --add-dir grant assumes) and recreated only when merely missing. Inodes are never
    pinned into the inventory and modes are never repaired."""
    if inventory is None:
        return None, {}
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), dict) or not inventory["files"]:
        return "inventory_invalid", {}
    verified = {}
    for path, digest in inventory["files"].items():
        try:
            data = _read_inventory_file(path)
        except FileNotFoundError:
            return "file_missing:" + Path(str(path)).name, {}
        except (OSError, ImplementationError):
            return "file_unsafe:" + Path(str(path)).name, {}
        if room.sha(data) != digest:
            return "content_changed:" + Path(str(path)).name, {}
        verified[path] = data
    export_dir = inventory.get("export_dir")
    if not isinstance(export_dir, str) or not os.path.isabs(export_dir):
        return "export_dir_unrecorded", {}
    problem = _export_dir_problem(export_dir, repair_export_dir)
    return problem, ({} if problem else verified)


def _export_dir_problem(export_dir, repair):
    """None when the export directory (<home>/deepseek/exports/<room>) is reachable component by component below its
    existing user-owned grandparent without following a symlink, is user-owned, and carries no group/other permission
    bits. With `repair`, at most the three missing levels below that grandparent are recreated 0700 relative to the
    previous descriptor (a concurrent creator's directory is revalidated, never trusted). Modes are never repaired."""
    import recovery
    target = Path(export_dir)
    if len(target.parts) < 4:
        return "export_dir_unsafe"
    try:
        with recovery.OwnedRoot(target.parents[2], kind="export_dir") as root:
            fd = recovery.directory_below(root, target.parts[-3:], "export_dir", create=repair)
    except recovery.ObservationError as exc:
        return "export_dir_missing" if exc.reason.endswith("_missing") else "export_dir_unsafe"
    except OSError:
        return "export_dir_unsafe"
    try:
        metadata = os.fstat(fd)
    except OSError:
        return "export_dir_unsafe"
    finally:
        os.close(fd)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return "export_dir_unsafe_mode"  # group/other bits would widen the --add-dir grant; never chmod on the launch path
    return None


def provider_inventory_mismatch(inventory, repair_export_dir=True):
    """Allowlisted mismatch code for a pinned DeepSeek inventory, or None when every snapshotted byte still matches."""
    return verify_provider_inventory(inventory, repair_export_dir)[0]


DELEGATE_SETTING_KEYS = ("model", "reasoning_effort", "max_tokens", "ask_effort", "ask_max_tokens", "max_input_bytes", "request_timeout_seconds")


def _pinned_delegate_settings(inventory, verified):
    """(delegate settings, None) from the verified bytes of the pinned provider configuration, or (None, code).

    The configuration is the pinned deepseek.json (or the single pinned .json when no file carries that name); its
    values come from the exact bytes the digest check just verified, so nothing is re-read after the check and a
    snapshot that cannot be parsed is a proven refusal, never a silently null packet field."""
    candidates = [path for path in inventory.get("files", {}) if isinstance(path, str) and path.endswith(".json")]
    preferred = [path for path in candidates if Path(path).name == "deepseek.json"]
    if len(preferred) == 1:
        chosen = preferred[0]
    elif len(candidates) == 1:
        chosen = candidates[0]
    elif not candidates:
        return None, "config_unpinned"
    else:
        return None, "config_ambiguous"
    try:
        snapshot = json.loads(verified[chosen].decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError):
        return None, "config_unparsable:" + Path(chosen).name
    if not isinstance(snapshot, dict) or any(key not in snapshot for key in DELEGATE_SETTING_KEYS):
        return None, "config_incomplete:" + Path(chosen).name
    settings = {key: snapshot[key] for key in DELEGATE_SETTING_KEYS}
    # The packet names the pinned transport explicitly so no delegate settings imply an official DeepSeek request. Snapshots
    # written before transport selection existed carry no backend field and could only ever have been official.
    settings["backend"] = snapshot.get("backend", "official")
    settings["base_url"] = snapshot.get("base_url")
    return settings, None


def resolve_provider(config_path, config):
    """(provider, pinned inventory or None) for the room owning an implementation profile.

    An explicit delegate_provider in the room's settings.json wins. Legacy settings without one infer only from the
    pinned profile: a lone qwen-local server means qwen, no server means none, anything else refuses before handoff.
    A deepseek room must carry a consistent inventory whose snapshotted bytes still match; a qwen room never
    receives DeepSeek instructions and a none room routes only among its Claude tiers."""
    config_path = Path(config_path).resolve()
    room_root = config_path.parent.parent if config_path.parent.name == "profiles" else None
    settings = None
    if room_root is not None:
        import recovery
        try:  # the same owned, bounded, symlink-refusing read the adapter applies to this file at startup
            settings = json.loads(recovery.read_owned(room_root / "settings.json", INVENTORY_FILE_LIMIT, "handoff", root=room_root))
        except recovery.ObservationError as exc:
            if not exc.reason.endswith("_missing"):
                raise ImplementationError("Room settings are unreadable; refusing handoff") from exc
        except (ValueError, RecursionError) as exc:
            raise ImplementationError("Room settings are unreadable; refusing handoff") from exc
        if settings is not None and not isinstance(settings, dict):
            raise ImplementationError("Room settings must be a JSON object; refusing handoff")
    servers = _mcp_servers(config)
    explicit = settings.get("delegate_provider") if settings else None
    if explicit is not None:
        if explicit not in PROVIDERS:
            raise ImplementationError("Unknown delegate provider in room settings; refusing handoff")
        provider = explicit
    elif not servers:
        provider = "none"
    elif set(servers) == {"qwen-local"}:
        provider = "qwen"
    else:
        raise ImplementationError("Ambiguous legacy delegate configuration; refusing handoff")
    if provider == "qwen" and set(servers) != {"qwen-local"}:
        raise ImplementationError("Room selects qwen but its pinned profile does not launch exactly the qwen-local guard")
    if provider == "none" and servers:
        raise ImplementationError("Room selects no delegate but its pinned profile launches MCP servers")
    if provider != "deepseek":
        return provider, None
    inventory = settings.get("provider_inventory") if settings else None
    if (set(servers) != {"deepseek"} or not isinstance(inventory, dict) or inventory.get("provider") != "deepseek"
            or not isinstance(inventory.get("files"), dict) or not inventory["files"] or not isinstance(inventory.get("export_dir"), str)
            or any(not isinstance(path, str) or not os.path.isabs(path) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for path, digest in inventory["files"].items())):
        raise ImplementationError("DeepSeek provider inventory is missing or inconsistent; refusing handoff")
    if inventory.get("room_id") != room_root.name or any(Path(path).parent != room_root / "profiles" for path in inventory["files"]):
        # The adapter enforces the same binding at startup (inventory_room_mismatch); refusing here keeps a copied or
        # restored room from launching an attempt whose delegate would refuse every job.
        raise ImplementationError("DeepSeek provider inventory does not belong to this room; refusing handoff")
    pinned = {"provider": "deepseek", "room_id": inventory.get("room_id"), "files": dict(inventory["files"]), "export_dir": inventory["export_dir"]}
    mismatch = provider_inventory_mismatch(pinned)
    if mismatch:
        raise ImplementationError("DeepSeek provider snapshot does not match its inventory (" + mismatch + "); refusing handoff")
    return provider, pinned


def candidate_snapshot(worktree):
    """Bind every committable path, deletion, file mode, symlink, and current HEAD."""
    worktree = Path(worktree)
    names = set(_git(worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    entries = []
    for raw in sorted(names - {b""}):
        name = os.fsdecode(raw)
        path = worktree / name
        if any(parent.is_symlink() for parent in path.parents if parent != worktree and worktree in parent.parents):
            raise ImplementationError("Candidate path traverses a symlink directory")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            entries.append({"path": name, "kind": "deleted"})
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            entry = {"path": name, "kind": "symlink", "mode": mode, "target": os.readlink(path)}
        elif stat.S_ISREG(metadata.st_mode):
            data = path.read_bytes()
            after = path.lstat()
            if (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
                raise ImplementationError("Candidate changed while it was being fingerprinted")
            entry = {"path": name, "kind": "file", "mode": mode, "sha256": room.sha(data)}
        else:
            raise ImplementationError("Unsupported candidate path type; submodules/special files require separate review")
        entries.append(entry)
    head = _git(worktree, "rev-parse", "HEAD").decode().strip()
    index = []
    for entry in _git(worktree, "ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, blob, stage = metadata.decode("ascii").split()
            index.append({"path": os.fsdecode(name), "mode": mode, "blob": blob, "stage": int(stage)})
    payload = {"head": head, "entries": entries, "index": index}
    return {**payload, "sha256": _digest(payload)}


def _load(handoff_path):
    directory = Path(handoff_path).resolve()
    if directory.is_file():
        directory = directory.parent
    manifest = json.loads((directory / "handoff.json").read_text())
    state = json.loads((directory / "state.json").read_text())
    if _digest(manifest) != state["manifest_sha256"]:
        raise ImplementationError("Immutable handoff manifest was modified")
    for name, digest in manifest["pinned_files"].items():
        if room.sha((directory / name).read_bytes()) != digest:
            raise ImplementationError(f"Immutable handoff input was modified: {name}")
    return directory, manifest, state


def _summary(directory, manifest, state):
    compact = dict(state)
    for key in ("candidate", "initial_candidate"):
        if isinstance(compact.get(key), dict):
            snapshot = compact[key]
            compact[key] = {"head": snapshot["head"], "sha256": snapshot["sha256"], "path_count": len(snapshot["entries"])}
    if isinstance(compact.get("recovery"), dict) and isinstance(compact["recovery"].get("candidate"), dict):
        snapshot = compact["recovery"]["candidate"]
        compact["recovery"] = {**compact["recovery"], "candidate": {"head": snapshot["head"], "sha256": snapshot["sha256"], "path_count": len(snapshot["entries"])}}
    compact["turn_history"] = [{"attempt_count": item.get("attempt_count"), "attempt_path": item.get("attempt_path"),
                                 "outcome": item.get("outcome") or (item.get("report") or {}).get("outcome"),
                                 "summary": (item.get("report") or {}).get("summary"),
                                 "recovery_id": item.get("recovery_id"),
                                 "gate_return_codes": [gate.get("return_code") for gate in item.get("gate_results") or []]}
                                for item in state.get("turn_history", [])]
    return {"handoff_id": manifest["handoff_id"], "handoff_path": str(directory / "handoff.json"),
            "state_path": str(directory / "state.json"),
            "worktree_path": manifest["worktree_path"], "branch": manifest["branch"],
            "spec_revision": manifest["revision"], "spec_sha256": manifest["spec_sha256"],
            "baseline_commit": manifest["baseline_commit"], "implementation_session_id": manifest["session_id"],
            **compact}


def implementation_status(handoff_path):
    directory, manifest, state = _load(handoff_path)
    return _summary(directory, manifest, state)


def _require_current_agreement(manifest):
    current = room.status_report(Path(manifest["room_path"]))
    if (not current["agreement"] or current["current_revision"] != manifest["revision"]
            or current["spec_sha256"] != manifest["spec_sha256"]):
        raise ImplementationError("Handoff spec is stale or no longer has current exact Astra/Fable agreement")


def prepare_handoff(room_path, project_path, revision, authorization_text, gates, config_path):
    if not isinstance(authorization_text, str) or not authorization_text.strip():
        raise ImplementationError("A nonempty explicit implementation authorization is required")
    if type(revision) is not int or revision < 1:
        raise ImplementationError("Revision must be a positive integer")
    if (not isinstance(gates, list) or not gates or any(not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command) for command in gates)):
        raise ImplementationError("Provide at least one independent gate as a nonempty argument array")
    room_path, project = Path(room_path).resolve(), Path(project_path).resolve()
    config = _config(config_path)
    provider, inventory = resolve_provider(config_path, config)
    policy = POLICIES[provider]
    with room.lock_room(room_path):
        report = room.status_report(room_path)
        if not report["agreement"] or report["current_revision"] != revision:
            raise ImplementationError("Implementation requires current exact-revision Astra/Fable agreement")
        project = Path(_git(project, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if _git(project, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ImplementationError("Source checkout must be clean; commit or separately preserve uncommitted work before handoff")
        baseline = _git(project, "rev-parse", "HEAD").decode().strip()
        with contextlib.closing(room.connect(room_path)) as db:
            spec = room.get_spec(db, room_path, revision, current=True)
            spec_bytes = bytes(spec["content"])
        history = room.transcript(argparse.Namespace(file=None), room_path)
        identity = {"room_path": str(room_path), "project_path": str(project), "revision": revision,
                    "spec_sha256": room.sha(spec_bytes), "review_history_sha256": room.sha(history.encode()),
                    "baseline_commit": baseline, "authorization_text": authorization_text, "gates": gates,
                    "config": config, "delegation_policy_sha256": room.sha(policy.encode())}
        if inventory is not None:
            identity["provider_inventory"] = inventory  # pinned bytes of the provider snapshot enter the handoff identity
        handoff_id = _digest(identity)
        directory = room_path / "implementations" / handoff_id
        if (directory / "handoff.json").exists():
            return implementation_status(directory)
        directory.mkdir(parents=True, exist_ok=False)
        worktree = directory / "worktree"
        branch = "codex/implementation-" + handoff_id[:16]
        session_id = str(uuid.uuid4())
        pinned = {}
        def pin(name, data):
            destination = directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            pinned[name] = room.sha(data)
        pin("spec.md", spec_bytes)
        pin("review-history.md", history.encode())
        pin("authorization.txt", authorization_text.encode())
        pin("delegation-policy.txt", policy.encode())
        owned = dict(config)
        owned_args = list(config["extra_args"])
        for index, (source, digest) in enumerate(config["referenced_files_sha256"].items()):
            content = Path(source).read_bytes()
            if room.sha(content) != digest:
                raise ImplementationError("Configuration reference changed during preparation")
            name = f"config-inputs/{index}-{Path(source).name}"
            pin(name, content)
            owned_args = [_pinned_arg(arg, source, str(directory / name)) for arg in owned_args]
        owned["extra_args"] = owned_args
        pin("implementation-config.json", (room.canonical(owned) + "\n").encode())
        transcript_template = config["session_transcript_path"]
        transcript = (transcript_template.replace("{session_id}", session_id).replace("{worktree}", str(worktree))
                      if transcript_template else str(Path(config["claude_config_dir"]) / "projects" /
                          re.sub(r"[^a-zA-Z0-9]", "-", str(worktree)) / (session_id + ".jsonl")))
        manifest = {**identity, "handoff_id": handoff_id, "created_at": room.now(), "branch": branch,
                    "worktree_path": str(worktree), "session_id": session_id, "session_transcript_path": transcript,
                    "pinned_files": pinned, "provider": provider}
        _atomic(directory / "handoff.json", manifest)
        state = {"phase": "preparing", "manifest_sha256": _digest(manifest), "implementation_authorized": True,
                 "astra_accepted": False, "created_at": room.now()}
        _atomic(directory / "state.json", state)
        try:
            _git(project, "worktree", "add", "-b", branch, str(worktree), baseline)
            state.update(phase="prepared", initial_candidate=candidate_snapshot(worktree))
        except Exception as exc:
            state.update(phase="blocked", error=str(exc), needs_attention=True)
            _atomic(directory / "state.json", state)
            raise
        _atomic(directory / "state.json", state)
        return _summary(directory, manifest, state)


def _pinned_arg(arg, source, replacement):
    """`arg` with a reference to `source` (bare or --flag=value, written literally or with a leading ~ as the loader
    expands it) replaced by the pinned copy, so the launched argv names the pinned bytes rather than the live file."""
    try:
        if str(Path(arg).expanduser()) == source:
            return replacement  # a bare reference (a path may itself contain '='), literal or with a leading ~
        option, equals, value = arg.partition("=")
        if equals and option.startswith("--") and str(Path(value).expanduser()) == source:
            return option + "=" + replacement
    except RuntimeError:
        pass  # no home directory to expand ~ against; the literal argument is left alone
    return arg


def _validate_report(report, manifest):
    if not isinstance(report, dict) or set(report) != set(REPORT_SCHEMA["required"]):
        raise ImplementationError("Implementation report does not match its required schema")
    if (type(report["spec_revision"]) is not int or report["spec_revision"] != manifest["revision"]
            or report["spec_sha256"] != manifest["spec_sha256"] or report["baseline_commit"] != manifest["baseline_commit"]):
        raise ImplementationError("Implementation report does not match the authorized revision/hash/baseline")
    if type(report["implementation_complete"]) is not bool or not isinstance(report["summary"], str):
        raise ImplementationError("Implementation report has invalid scalar fields")
    if report["outcome"] not in ("completed", "needs_changes", "scope_change") or not isinstance(report["scope_change"], str):
        raise ImplementationError("Implementation outcome is invalid")
    if report["outcome"] == "scope_change" and not report["scope_change"].strip():
        raise ImplementationError("Scope changes require a concrete explanation for Astra")
    for key in ("changes", "tests_reported", "review_findings", "remaining_gaps", "backlog"):
        if not isinstance(report[key], list) or not all(isinstance(value, str) for value in report[key]):
            raise ImplementationError("Implementation report has invalid evidence fields")
    routes = report["routing_log"]
    if not isinstance(routes, list) or not routes:
        raise ImplementationError("Implementation requires a routing record, including Fable-only work")
    for route in routes:
        if not isinstance(route, dict) or set(route) != set(ROUTE_SCHEMA["required"]):
            raise ImplementationError("Invalid routing record")
        if route["tier"] not in ("qwen", "deepseek", "sonnet", "opus", "fable"):
            raise ImplementationError("Invalid routing tier")
        if any(not isinstance(route[key], str) for key in route if key != "fixes"):
            raise ImplementationError("Invalid routing metadata")
        if not isinstance(route["fixes"], list) or not all(isinstance(value, str) for value in route["fixes"]):
            raise ImplementationError("Invalid routing fixes")


def _run_child(argv, cwd, output, errors, timeout, input_bytes=None, env=None, spawn_receipt=None, on_spawn=None):
    """Run one bounded child. Three launch outcomes are distinguished truthfully: ModelSpawnError proves no process
    was created (failures and terminations before Popen, or Popen's own cleanup-guaranteed failures); a normal
    return or any later exception means the process was created and `spawn_receipt` (when given) was written
    immediately after creation; LaunchUnknown means the interruption landed inside process creation itself."""
    process = None
    stage = "before_spawn"
    previous_term = signal.signal(signal.SIGTERM, room.handle_termination)
    previous_int = signal.signal(signal.SIGINT, room.handle_termination)
    try:
        streams = contextlib.ExitStack()
        try:
            stdout = streams.enter_context(output.open("wb"))
            stderr = streams.enter_context(errors.open("wb"))
        except OSError as exc:
            streams.close()
            raise ModelSpawnError(f"Process output could not be opened: {exc}") from exc
        with streams:
            stage = "spawning"
            try:
                process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, start_new_session=True, env=env)
            except (OSError, ValueError, TypeError) as exc:
                stage = "before_spawn"  # Popen reaps any partially created child before raising these.
                raise ModelSpawnError(f"Process did not start: {exc}") from exc
            stage = "spawned"
            if spawn_receipt is not None:
                _atomic(spawn_receipt, {"pid": process.pid, "started_at": room.now()})
            if on_spawn is not None:
                on_spawn()  # durable launch bookkeeping while the child runs
            process.communicate(input=input_bytes, timeout=timeout)
        return process.returncode
    except Exception as exc:
        if stage == "before_spawn" and not isinstance(exc, ModelSpawnError):
            reason = "cancelled_before_launch" if isinstance(exc, room.InvocationTerminated) else "model_spawn_failure"
            raise ModelSpawnError(f"Terminated before any process was created: {type(exc).__name__}", reason) from exc
        if stage == "spawning":
            raise LaunchUnknown(f"Interrupted while creating the process: {type(exc).__name__}") from exc
        raise
    finally:
        if process is not None and process.poll() is None:
            room.stop_process(process)
        if process is not None:
            _atomic(output.parent / "process-result.json", {"pid": process.pid, "return_code": process.returncode, "finished_at": room.now()})
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def _registered_successor(record, record_sha, manifest, recovery_id, successor_job_id, invoking_registry=None):
    """True only when the registry named by the durable recovery record (and by the invoking service) has this
    exact successor job dispatched for this recovery, the registered worker process is this invocation's parent,
    and that worker's lease is currently held.

    A dispatch file, a caller-supplied dictionary or an eligible callback cannot stand in for that registration:
    the registry rows, the parent-process identity recorded at worker spawn, and the held lease bind the engine
    to the owning worker. A held lease alone only proves that someone holds it."""
    import fcntl
    import sqlite3
    registry = record.get("registry_home") if isinstance(record, dict) else None
    if (not isinstance(registry, str) or not re.fullmatch(r"[0-9a-f]{32}", str(successor_job_id))
            or not isinstance(invoking_registry, str) or invoking_registry != registry):
        return False  # The invoking service must name the same registry the durable record was prepared in.
    home = Path(registry)
    database = home / "registry.sqlite3"
    if not home.is_dir() or not database.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            job = db.execute("SELECT room_id,kind,status,payload,pid FROM jobs WHERE id=?", (successor_job_id,)).fetchone()
            row = db.execute("SELECT room_id,handoff_id,status,successor_job_id,record_sha256 FROM implementation_recoveries WHERE id=?",
                             (recovery_id,)).fetchone()
        payload = json.loads(job["payload"]) if job else None
    except (sqlite3.Error, ValueError, TypeError):
        return False
    if (not job or not row or job["kind"] != "implementation" or job["status"] != "running" or not isinstance(payload, dict)
            or type(job["pid"]) is not int or job["pid"] != os.getppid()  # this invocation must be the registered worker's child
            or payload.get("recovery_id") != recovery_id or payload.get("handoff_id") != manifest["handoff_id"]
            or row["status"] != "dispatched" or row["successor_job_id"] != successor_job_id or row["room_id"] != job["room_id"]
            or row["handoff_id"] != manifest["handoff_id"] or row["record_sha256"] != record_sha):
        return False
    lease = home / "jobs" / successor_job_id / "worker.lock"
    try:
        with lease.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True  # Held: this invocation runs under the registered worker's lease.
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False  # Nobody holds the registered worker's lease, so this is not the owning invocation.


def _recovery_refusal(directory, manifest, state, successor, root=None):
    """Deterministic pre-launch rechecks for a registered successor.

    Returns (reason code or None, current candidate). A reason code proves no launch happened. The engine
    binds itself to the durable registration (registry rows named by the recovery record plus the held worker
    lease) before consulting the registered recheck; a caller-supplied callback alone can never reach launch.
    """
    prepared = state.get("recovery") if isinstance(state.get("recovery"), dict) else None
    if not isinstance(successor, dict) or not prepared or prepared.get("recovery_id") != successor.get("recovery_id"):
        return "recovery_binding_mismatch", None
    home = directory / "recoveries" / str(successor["recovery_id"])
    try:
        import recovery
        bound = root if root is not None else directory
        dispatch = json.loads(recovery.read_owned(home / "dispatch.json", recovery.EVIDENCE_LIMIT, "record", root=bound))
        record_bytes = recovery.read_owned(home / "record.json", recovery.EVIDENCE_LIMIT, "record", root=bound)
        record = json.loads(record_bytes)
    except (recovery.ObservationError, OSError, ValueError):
        return "recovery_binding_mismatch", None
    if (not isinstance(dispatch, dict) or dispatch.get("recovery_id") != successor["recovery_id"]
            or dispatch.get("successor_job_id") != successor.get("successor_job_id") or not successor.get("successor_job_id")):
        return "recovery_binding_mismatch", None
    if room.sha(record_bytes) != prepared.get("record_sha256") or not isinstance(record, dict):
        return "evidence_changed", None
    if not _registered_successor(record, room.sha(record_bytes), manifest, successor["recovery_id"], successor["successor_job_id"],
                                 successor.get("registry")):
        return "recovery_binding_mismatch", None
    recheck = successor.get("recheck")
    if not callable(recheck):
        return "recovery_binding_mismatch", None
    report = recheck()
    if not isinstance(report, dict) or not report.get("eligible"):
        reasons = report.get("reasons") if isinstance(report, dict) else None
        return (",".join(reasons) if reasons else "recovery_binding_mismatch"), None
    try:
        _require_current_agreement(manifest)
    except ImplementationError:
        return "spec_mismatch", None
    current = candidate_snapshot(manifest["worktree_path"])
    if current != prepared["candidate"]:
        return "candidate_changed", None
    return None, current


def run_implementation(handoff_path, successor=None, owner_job_id=None):
    """Run the next authorized attempt. `successor` binds a registered recovery successor job:
    {"recovery_id", "successor_job_id", "recheck": zero-argument callable returning the audit report}."""
    if successor is not None:
        directory = Path(handoff_path).resolve()  # the registered path only; the recovery lane reads nothing before binding
        if directory.is_file():
            directory = directory.parent
    else:
        directory, _, _ = _load(handoff_path)
    with room.lock_room(directory):
        root = None
        if successor is not None:
            import recovery
            try:
                # The recovery lane binds the handoff directory: identity captured around validation, one descriptor for every read.
                root, directory, manifest, state, _ = recovery.bind_handoff(directory)
            except Exception as exc:
                return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "prelaunch_error", "detail": type(exc).__name__,
                        "recovery_id": successor.get("recovery_id") if isinstance(successor, dict) else None, "handoff_id": None, "model_launched": False}
        else:
            directory, manifest, state = _load(directory)
        with root if root is not None else contextlib.nullcontext():
            return _continue_run(directory, manifest, state, successor, root, owner_job_id)


def _continue_run(directory, manifest, state, successor, root, owner_job_id=None):
    if state["phase"] in ("running_model", "running_gates", "preparing"):
        state.update(phase="blocked", needs_attention=True, active_stage=None,
                     error="Previous invocation ended without a recorded outcome; no replay is allowed")
        _atomic(directory / "state.json", state)
    is_recovery = state["phase"] == "recovery_prepared"
    current = None
    if is_recovery and successor is None:
        raise ImplementationError("A prepared recovery runs only through its registered successor job")
    if not is_recovery and successor is not None:
        raise ImplementationError("Recovery identity was supplied for a handoff that has no prepared recovery")
    if not is_recovery and state["phase"] not in ("prepared", "correction_pending"):
        return _summary(directory, manifest, state)
    # Each lane's own safety checks run first (the recovery recheck, or the agreement check), so their reasons are the
    # ones durably recorded; the pinned provider snapshot is then re-verified before any candidate fingerprint, state
    # write, attempt increment or spawn. A mismatch is a proven non-launch: initial and correction attempts return the
    # refusal shape (the worker records failed, never uncertain) with the handoff phase and candidate untouched; a
    # registered successor takes the existing invalidation path, and any exception in its pre-launch work is the same
    # proven non-launch (prelaunch_error) because nothing has been written or spawned yet.
    if is_recovery:
        try:
            refusal, current = _recovery_refusal(directory, manifest, state, successor, root)
            if refusal:
                return _refused(refusal, manifest, successor)
            refused, delegate_settings = _verify_delegate_snapshot(manifest, successor)
        except Exception as exc:  # Nothing has been written or spawned yet: any failure here is a proven non-launch.
            return _refused("prelaunch_error", manifest, successor, type(exc).__name__)
    else:
        _require_current_agreement(manifest)
        try:
            refused, delegate_settings = _verify_delegate_snapshot(manifest, successor)
        except Exception as exc:  # nothing has been written or spawned: a proven non-launch, never an uncertain job
            return _refused("prelaunch_error", manifest, successor, type(exc).__name__)
    if refused is not None:
        return refused
    is_correction = state["phase"] == "correction_pending"
    worktree = Path(manifest["worktree_path"])
    expected_candidate = state["recovery"]["candidate"] if is_recovery else state["candidate"] if is_correction else state["initial_candidate"]
    if (current if is_recovery else candidate_snapshot(worktree)) != expected_candidate:
        raise ImplementationError("Prepared worktree changed before its authorized implementation began")
    return _launch_attempt(directory, manifest, state, worktree, is_correction, is_recovery, successor, root,
                           successor["successor_job_id"] if is_recovery else owner_job_id, delegate_settings)


def _verify_delegate_snapshot(manifest, successor):
    """(refusal, delegate settings): the refusal for a pinned DeepSeek snapshot that no longer matches or cannot be
    parsed, or None with the packet's delegate settings taken from the verified bytes themselves (None for a room
    without a pinned provider snapshot)."""
    inventory = manifest.get("provider_inventory")
    if not inventory:
        return None, None
    mismatch, verified = verify_provider_inventory(inventory)
    if mismatch:
        return _refused("provider_inventory_mismatch", manifest, successor, mismatch), None
    # The packet's pinned delegate settings come from the verified bytes themselves, so no later read exists to
    # tamper with, and an unusable snapshot is refused here, before any state mutation, attempt or spawn.
    delegate_settings, problem = _pinned_delegate_settings(inventory, verified)
    if problem:
        return _refused("provider_inventory_mismatch", manifest, successor, problem), None
    return None, delegate_settings


def _set_aside(attempt):
    """Keep an unlaunched attempt directory's files without letting them block the next launch."""
    if attempt.is_dir():
        attempt.rename(attempt.with_name(attempt.name + "-unlaunched-" + uuid.uuid4().hex))


def _refused(reason, manifest, successor, detail=None):
    return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": reason, "detail": detail,
            "recovery_id": successor.get("recovery_id") if isinstance(successor, dict) else None,
            "handoff_id": manifest["handoff_id"], "model_launched": False}


def _pinned_bytes(directory, name, root=None):
    """Bytes of a pinned handoff input: through the bound root descriptor in the recovery lane, by path otherwise."""
    if root is None:
        return (directory / name).read_bytes()
    import recovery
    return recovery.read_owned(directory / name, recovery.EVIDENCE_LIMIT, "handoff", root=root)


def _launch_attempt(directory, manifest, state, worktree, is_correction, is_recovery, successor, root=None, owner_job_id=None, delegate_settings=None):
    import copy
    import recovery
    rollback = copy.deepcopy(state)
    attempt_number = state.get("attempt_count", 0) + 1
    attempt = directory / "attempts" / f"{attempt_number:04d}"
    try:
        config = json.loads(_pinned_bytes(directory, "implementation-config.json", root))
        room.validate_subscription_environment()
        if is_recovery:
            _set_aside(attempt)  # A stray directory from an earlier pre-launch failure must not wedge the lane.
        attempt.mkdir(parents=True, exist_ok=False)
        argv, prompt, model_env = _prepare_attempt(directory, manifest, state, worktree, is_correction, is_recovery, successor,
                                                   config, attempt, attempt_number, recovery, root, owner_job_id, delegate_settings)
    except Exception as exc:
        if is_recovery:
            # The on-disk projection is still recovery_prepared and no process was spawned.
            _set_aside(attempt)
            state.clear()
            state.update(rollback)
            return _refused("prelaunch_error", manifest, successor, type(exc).__name__)
        raise
    return _run_attempt(directory, manifest, state, worktree, is_recovery, successor, config, attempt, attempt_number,
                        argv, prompt, model_env, rollback, recovery)


def _prepare_attempt(directory, manifest, state, worktree, is_correction, is_recovery, successor, config, attempt, attempt_number, recovery, root=None, owner_job_id=None, delegate_settings=None):
    started_at = room.now()
    import datetime as _dt
    deadline = (room.parse_timestamp(started_at) + _dt.timedelta(seconds=config["timeout_seconds"])).isoformat()
    inventory = manifest.get("provider_inventory") if isinstance(manifest.get("provider_inventory"), dict) else None
    settings = delegate_settings  # bound to the verified pinned bytes by _continue_run; never re-read here
    packet = {"handoff_id": manifest["handoff_id"], "spec_revision": manifest["revision"],
              "spec_sha256": manifest["spec_sha256"], "baseline_commit": manifest["baseline_commit"],
              "authorization": manifest["authorization_text"], "gates": manifest["gates"],
              "spec": _pinned_bytes(directory, "spec.md", root).decode("utf-8"),
              "review_history": _pinned_bytes(directory, "review-history.md", root).decode("utf-8"),
              "worktree_path": str(worktree), "delegation_policy": _pinned_bytes(directory, "delegation-policy.txt", root).decode("utf-8"),
              "implementation_timeout_seconds": config["timeout_seconds"], "attempt_started_at": started_at, "attempt_deadline_at": deadline,
              "delegate_provider": manifest.get("provider", "unrecorded"),
              "delegate_export_dir": inventory.get("export_dir") if inventory else None,
              "delegate_settings": settings,
              "timeout_note": ("The pinned Claude invocation timeout ends this attempt at attempt_deadline_at. Size delegated work to the "
                               "remaining window; a detached provider job may outlive it and a later authorized attempt can read the "
                               "saved job without resubmitting. A host restart (the recovery lane's boot boundary) kills a streaming "
                               "local worker and leaves its remote delivery unknown.")}
    if is_correction:
        packet.update(correction_request=state["correction_request"], previous_report=state.get("report"),
                      previous_gate_results=state.get("gate_results", []))
        packet["previous_gate_output"] = [{"argv": gate["argv"], "return_code": gate["return_code"],
                                           "stdout": Path(gate["stdout_path"]).read_text(errors="replace"),
                                           "stderr": Path(gate["stderr_path"]).read_text(errors="replace")}
                                          for gate in state.get("gate_results", [])]
    if is_recovery:
        prepared = state["recovery"]
        bound = root if root is not None else directory
        record = json.loads(recovery.read_owned(directory / "recoveries" / prepared["recovery_id"] / "record.json", recovery.EVIDENCE_LIMIT, "record", root=bound))
        interrupted = Path(state["attempt_path"])
        try:
            stderr_tail = recovery.read_owned(interrupted / "stderr.txt", recovery.EVIDENCE_LIMIT, root=bound)[-4000:].decode("utf-8", "replace")
        except recovery.ObservationError:
            stderr_tail = ""
        # A correction stays pending until an attempt after it completed with a report; interrupted
        # attempts (including earlier recovered successors) never satisfy it.
        correction = state.get("correction_request") if isinstance(state.get("correction_request"), dict) else None
        after = correction.get("after_attempt") if correction else None
        reported_attempt = state["attempt_count"] - 1 if state.get("report") else None
        pending_correction = correction if (type(after) is int and after < state["attempt_count"]
                                            and (reported_attempt is None or reported_attempt <= after)) else None
        packet.update(recovery={
            "recovery_id": prepared["recovery_id"], "interrupted_attempt": state["attempt_count"],
            "interruption": record["interruption"], "diagnosis": record["diagnosis"], "remaining_work": record["remaining_work"],
            "recovery_authorization": record["authorization"],
            "partial_candidate": {"sha256": prepared["candidate"]["sha256"], "head": prepared["candidate"]["head"],
                                  "changed_vs_initial": recovery._changed_paths(state.get("initial_candidate"), prepared["candidate"])},
            "pending_correction_request": pending_correction,
            "interrupted_attempt_stderr_tail_unverified": stderr_tail,
            "instructions": ("This is an authorized continuation of the interrupted implementation session in the same worktree. "
                             "Inspect the existing partial work before editing. Nothing produced by the interrupted attempt "
                             "(code, tests, comments, claims) is verified; no report or gate result from it exists. Complete the "
                             "remaining agreed requirements, run the gates yourself, and return a fresh structured report.")})
    prompt = ("Implement only the authorized agreed specification in this isolated worktree. "
              "The review-only restrictions in the historical transcript applied to that prior review; "
              "this exact handoff separately authorizes implementation. Follow the fixed delegation policy "
              "and leave the candidate for independently executed gates and Astra review. Return the "
              "required structured report, with actual evidence and remaining gaps; no self-certification. "
              "remaining_gaps is only for unresolved agreed acceptance criteria or verification blockers. "
              "Keep nonblocking capability observations, optional-tool availability, evidence provenance, "
              "and expected workflow limits in review_findings. Never relabel an actual unmet requirement "
              "as nonblocking merely to obtain acceptance. Describe grounded useful enhancements in backlog "
              "with each proposal's benefit and tradeoff. Astra files or links proposal issues for the "
              "user's opinion and scope approval outside this session; implementation waits for approval. "
              "If discoveries require a material scope change, stop and return outcome=scope_change with "
              "a concrete scope_change explanation for Astra. Put optional enhancements in backlog.\n"
              + "IMPLEMENTATION PACKET (JSON):\n" + room.canonical(packet) + "\n").encode()
    (attempt / "prompt.txt").write_bytes(prompt)
    argv = [config["claude_bin"], *config["extra_args"], "--print", "--output-format", "json", "--model", config["model"],
            "--resume" if (is_correction or is_recovery) else "--session-id", manifest["session_id"], "--json-schema", room.canonical(REPORT_SCHEMA)]
    _atomic(attempt / "argv.json", argv)
    if is_correction:
        state.setdefault("turn_history", []).append({key: state.get(key) for key in
            ("attempt_count", "attempt_path", "report", "identity_evidence", "gate_results", "candidate", "astra_review", "correction_request")})
    if is_recovery:
        state.setdefault("turn_history", []).append({
            "attempt_count": state["attempt_count"], "attempt_path": state["attempt_path"], "outcome": "interrupted",
            "interruption": record["interruption"], "recovery_id": prepared["recovery_id"], "error": state.get("error"),
            "correction_request": pending_correction})
        # launched_at is recorded only from the spawn receipt written after the process exists (see _record_launch).
        state["recovery"] = {**prepared, "successor_job_id": successor["successor_job_id"], "successor_attempt": attempt_number,
                             "launch_state": "pending"}
        for key in ("report", "identity_evidence", "gate_results", "astra_review", "primary_model", "actual_models", "auxiliary_models"):
            state.pop(key, None)
        state.update(replay_allowed=False, error=None, needs_attention=False)
    model_env = dict(os.environ)
    if config["claude_config_dir_override"] is None:
        model_env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        model_env["CLAUDE_CONFIG_DIR"] = config["claude_config_dir_override"]
    model_env["PROJECT_ROOM_WORKTREE"] = str(worktree)  # the verified worktree root the delegate adapter binds context_path to
    # Advisory ownership/stage telemetry; this never replaces launch or acceptance evidence.
    state.update(phase="running_model", started_at=started_at, attempt_path=str(attempt), attempt_count=attempt_number,
                 astra_accepted=False, gates_passed=False,
                 owner_job_id=owner_job_id if isinstance(owner_job_id, str) and owner_job_id else None,
                 active_stage={"kind": "model", "index": 1, "started_at": started_at})
    _atomic(directory / "state.json", state)
    return argv, prompt, model_env


def _record_launch(state, attempt, successor, attempt_number):
    """Mark the recovery launched only from the spawn receipt that _run_child writes after the process exists."""
    if not isinstance(state.get("recovery"), dict) or state["recovery"].get("launched_at"):
        return bool(isinstance(state.get("recovery"), dict) and state["recovery"].get("launched_at"))
    launched_at, evidence = None, None
    for name in ("process-start.json", "process-result.json"):
        receipt = attempt / name
        if not receipt.is_file():
            continue
        try:
            saved = json.loads(receipt.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(saved, dict) and type(saved.get("pid")) is int:
            # The start receipt carries the spawn instant; the exit receipt (written in _run_child's finally only when a
            # process existed) proves a launch whose start receipt was lost to a signal or write failure.
            launched_at, evidence = (saved.get("started_at") if name == "process-start.json" else state.get("started_at")), name
            if isinstance(launched_at, str):
                break
    if not isinstance(launched_at, str):
        return False
    state["recovery"].update(launched_at=launched_at, launch_state="launched", launch_evidence=evidence)
    for entry in state.get("recovery_history", []):
        if isinstance(entry, dict) and entry.get("recovery_id") == state["recovery"].get("recovery_id") and entry.get("status") == "prepared":
            entry.update(status="launched", launched_at=launched_at, successor_job_id=successor["successor_job_id"], successor_attempt=attempt_number)
    return True


def _run_attempt(directory, manifest, state, worktree, is_recovery, successor, config, attempt, attempt_number, argv, prompt, model_env, rollback, recovery):
    try:
        try:
            def launched():
                if is_recovery and _record_launch(state, attempt, successor, attempt_number):
                    _atomic(directory / "state.json", state)
            try:
                code = _run_child(argv, worktree, attempt / "stdout.json", attempt / "stderr.txt", config["timeout_seconds"], prompt, model_env,
                                  spawn_receipt=attempt / "process-start.json", on_spawn=launched)
            finally:
                if is_recovery:
                    _record_launch(state, attempt, successor, attempt_number)
        except ModelSpawnError as exc:
            if is_recovery:
                # No process was created: restore the prepared projection first, then keep the unlaunched attempt files aside.
                _atomic(directory / "state.json", rollback)
                state.clear()
                state.update(rollback)
                _set_aside(attempt)
                return _refused(exc.reason, manifest, successor)
            raise
        except LaunchUnknown:
            if is_recovery:
                # Whether a process exists is unknown: this stays a blocked, unclassifiable successor, never a proven non-launch.
                state["recovery"]["launch_state"] = "unknown"
            raise
        finished = room.now()
        state.update(model_finished_at=finished, model_return_code=code, active_stage=None)
        _atomic(directory / "state.json", state)
        raw = (attempt / "stdout.json").read_bytes()
        state["model_stdout_sha256"] = room.sha(raw)
        result = json.loads(raw)
        _atomic(attempt / "parsed-result.json", result)
        if (code != 0 or not isinstance(result, dict) or result.get("is_error") is not False
                or result.get("type") != "result" or result.get("subtype") != "success"
                or result.get("terminal_reason") not in (None, "completed") or result.get("session_id") != manifest["session_id"]):
            raise ImplementationError("Claude did not return a successful terminal result for the exact implementation session")
        if result.get("permission_denials"):
            state["permission_denials"] = result["permission_denials"]
            raise ImplementationError("Claude reported permission denials; required work needs attention")
        report = result.get("structured_output")
        _validate_report(report, manifest)
        usage = result.get("modelUsage")
        models = sorted(usage) if isinstance(usage, dict) else []
        transcript_path = manifest["session_transcript_path"]
        if not config["session_transcript_path"]:
            transcript_path = session_paths.find_session_transcript(config["claude_config_dir"], manifest["session_id"],
                                                                    worktree, transcript_path)
        located = recovery.locate_transcript(config, manifest, worktree) if state.get("recovery_history") else None
        prefix_problem = recovery.prefix_violation(located, state, recovery.transcript_root(config, located)) if located is not None else None
        if prefix_problem:
            raise ImplementationError(prefix_problem + ": the captured pre-recovery transcript prefix could not be verified as intact")
        evidence = room.primary_producer_evidence(transcript_path, manifest["session_id"],
                                                 state["started_at"], finished, report, models, config)
        state.update(report=report, primary_model=evidence["primary_model"], actual_models=models,
                     auxiliary_models=[value for value in models if value != evidence["primary_model"]], identity_evidence=evidence)
        candidate = candidate_snapshot(worktree)
        if candidate["head"] != manifest["baseline_commit"]:
            raise ImplementationError("Implementation changed worktree HEAD; committed candidates require separate integration review")
        if report["outcome"] == "scope_change":
            state.update(phase="scope_change", needs_attention=True, candidate=candidate, gate_results=[], finished_at=room.now(), active_stage=None)
            _atomic(directory / "state.json", state)
            return _summary(directory, manifest, state)
        state.update(phase="running_gates", candidate=candidate, gate_results=[])
        _atomic(directory / "state.json", state)
        gate_env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for index, gate in enumerate(manifest["gates"]):
            gate_dir = attempt / f"gate-{index + 1}"
            gate_dir.mkdir()
            started = room.now()
            state["active_stage"] = {"kind": "gate", "index": index + 1, "started_at": started}
            _atomic(directory / "state.json", state)
            result_code = _run_child(gate, worktree, gate_dir / "stdout.txt", gate_dir / "stderr.txt",
                                     config["gate_timeout_seconds"], env=gate_env)
            state["active_stage"] = None
            _atomic(directory / "state.json", state)
            after = candidate_snapshot(worktree)
            gate_result = {"argv": gate, "return_code": result_code, "started_at": started,
                           "finished_at": room.now(), "stdout_path": str(gate_dir / "stdout.txt"),
                           "stderr_path": str(gate_dir / "stderr.txt"),
                           "stdout_sha256": room.sha((gate_dir / "stdout.txt").read_bytes()),
                           "stderr_sha256": room.sha((gate_dir / "stderr.txt").read_bytes()), "candidate_unchanged": after == candidate}
            state["gate_results"].append(gate_result)
            _atomic(directory / "state.json", state)
            if after != candidate:
                raise ImplementationError("A gate changed candidate content or HEAD; gate evidence cannot certify this candidate")
        state.update(phase="awaiting_astra_review", gates_passed=all(gate["return_code"] == 0 for gate in state["gate_results"]),
                     candidate=candidate, finished_at=room.now(), needs_attention=False, active_stage=None)
    except BaseException as exc:
        state.update(phase="blocked", needs_attention=True, finished_at=room.now(),
                     error=f"{type(exc).__name__}: {exc}", replay_allowed=False, active_stage=None)
        _atomic(directory / "state.json", state)
        if not isinstance(exc, (Exception, KeyboardInterrupt)):
            raise
    _atomic(directory / "state.json", state)
    return _summary(directory, manifest, state)


def record_astra_review(handoff_path, accepted, review_text):
    if type(accepted) is not bool or not isinstance(review_text, str) or not review_text.strip():
        raise ImplementationError("A nonempty independent Astra review and boolean decision are required")
    directory, _, _ = _load(handoff_path)
    with room.lock_room(directory):
        directory, manifest, state = _load(directory)
        if state["phase"] != "awaiting_astra_review":
            raise ImplementationError("Only a completed implementation with independent gate evidence is reviewable")
        _require_current_agreement(manifest)
        _verify_saved_evidence(state)
        current = candidate_snapshot(manifest["worktree_path"])
        if current != state["candidate"]:
            raise ImplementationError("Candidate changed after independent gates; existing evidence cannot approve it")
        if accepted and (not state["gates_passed"] or not state["report"]["implementation_complete"]
                         or state["report"]["remaining_gaps"] or state["report"]["outcome"] != "completed"):
            raise ImplementationError("A candidate with failed gates, incomplete implementation, or remaining gaps cannot be accepted")
        review = {"reviewer": "astra", "accepted": accepted, "review_text": review_text, "recorded_at": room.now(),
                  "spec_revision": manifest["revision"], "spec_sha256": manifest["spec_sha256"],
                  "baseline_commit": manifest["baseline_commit"], "candidate_sha256": current["sha256"], "candidate_head": current["head"]}
        _atomic(directory / f"astra-review-{state['attempt_count']:04d}.json", review)
        state.update(phase="accepted" if accepted else "changes_required", astra_accepted=accepted, astra_review=review)
        _atomic(directory / "state.json", state)
        return _summary(directory, manifest, state)


def _verify_saved_evidence(state):
    if room.sha((Path(state["attempt_path"]) / "stdout.json").read_bytes()) != state["model_stdout_sha256"]:
        raise ImplementationError("Saved model output changed after verification")
    for gate in state["gate_results"]:
        if any(room.sha(Path(gate[kind + "_path"]).read_bytes()) != gate[kind + "_sha256"] for kind in ("stdout", "stderr")):
            raise ImplementationError("Saved independent gate output changed after execution")


def request_changes(handoff_path, review_text):
    if not isinstance(review_text, str) or not review_text.strip():
        raise ImplementationError("Explicit diagnosed correction instructions are required")
    directory, _, _ = _load(handoff_path)
    with room.lock_room(directory):
        directory, manifest, state = _load(directory)
        if state["phase"] == "correction_pending":
            if state["correction_request"]["review_text"] != review_text:
                raise ImplementationError("Different correction already pending")
            return _summary(directory, manifest, state)
        if state["phase"] not in ("awaiting_astra_review", "changes_required"):
            raise ImplementationError("Only a known completed implementation/gate outcome permits an explicit correction")
        _require_current_agreement(manifest)
        _verify_saved_evidence(state)
        if candidate_snapshot(manifest["worktree_path"]) != state["candidate"]:
            raise ImplementationError("Candidate changed since recorded gates; correction requires fresh diagnosis")
        correction = {"reviewer": "astra", "review_text": review_text, "created_at": room.now(),
                      "spec_sha256": manifest["spec_sha256"], "candidate_sha256": state["candidate"]["sha256"],
                      "after_attempt": state["attempt_count"]}
        _atomic(directory / f"correction-{state['attempt_count']:04d}.json", correction)
        state.update(phase="correction_pending", correction_request=correction, astra_accepted=False)
        _atomic(directory / "state.json", state)
        return _summary(directory, manifest, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    for name in ("room", "project", "authorization-file", "gates-file", "config"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--revision", type=int, required=True)
    for name in ("run", "status", "review", "request-changes"):
        command = commands.add_parser(name)
        command.add_argument("--handoff", required=True)
        if name == "review":
            command.add_argument("--decision", choices=("accept", "changes_required"), required=True)
            command.add_argument("--review-file", required=True)
        if name == "request-changes":
            command.add_argument("--review-file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_handoff(args.room, args.project, args.revision, Path(args.authorization_file).read_text(),
                                     json.loads(Path(args.gates_file).read_text()), args.config)
        elif args.command == "run":
            result = run_implementation(args.handoff, owner_job_id="cli")
        elif args.command == "review":
            result = record_astra_review(args.handoff, args.decision == "accept", Path(args.review_file).read_text())
        elif args.command == "request-changes":
            result = request_changes(args.handoff, Path(args.review_file).read_text())
        else:
            result = implementation_status(args.handoff)
        print(json.dumps(result, indent=2))
        return 2 if result["phase"] == "blocked" else 0
    except (ImplementationError, room.RoomError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
