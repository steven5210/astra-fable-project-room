"""Explicit native delegation routing for prepared Project Room AO engineers.

Preparation writes worktree-scoped Claude configuration into already-ignored paths
(two named native agents and one local settings file carrying deny rules, env
knobs and a private deny-only guard hook), snapshots it into the immutable
preparation record together with bounded Claude executable evidence, and observes
the AO project rules through the project API. Validation is offline (pinned files,
ignore status, guard, interpreter, executable identity, user/managed/project
settings); the AO rules are re-observed only on the prepare, pre-dispatch and sync
paths. Nothing here launches a model, edits shared Git exclusions, or touches
settings outside the prepared worktree and the private controller state.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from ao_delegate_launcher import owned_bytes
from room import RoomError

MODELS = {"pr-sonnet": "claude-sonnet-5", "pr-opus": "claude-opus-5"}
EFFORT = "max"
ENV = {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2",
       "CLAUDE_CODE_DISABLE_WORKFLOWS": "1", "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"}
# A forced subagent model overrides explicit definitions; the plain default only
# applies to agents without a model and is recorded, not treated as protection.
CONTRADICTORY_ENV = ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE",)
RECORDED_ENV = tuple(ENV) + ("CLAUDE_CODE_SUBAGENT_MODEL",) + CONTRADICTORY_ENV
DENY = ["Workflow", "Skill(code-review)", "Skill(simplify)", "Skill(security-review)"]
# Every root route must reach the ownership guard, including future/unknown execution tools.
MATCHER = ".*"
EXECUTION_POLICY = "orchestrator"
# Native workers inherit the parent session's MCP tools; these servers submit delegate or room work.
SUBMISSION_SERVERS = ("mcp__deepseek", "mcp__qwen-local", "mcp__project-room")
SUBMISSION_TOOLS = ("mcp__deepseek__deepseek_submit", "mcp__deepseek__deepseek_ask")
CHILD_DENIED = ", ".join(SUBMISSION_SERVERS + SUBMISSION_TOOLS)
FILES = (".claude/settings.local.json", ".claude/agents/pr-sonnet.md", ".claude/agents/pr-opus.md")
FRONTMATTER = ("name", "description", "model", "effort", "disallowedTools")
BROWSER_SKILL = "claude-in-chrome"  # the exact skill observed in the prior Opus browser task; never operator-overridable
CLAUSE = ("For Project Room work, this project-specific delegation rule is the explicit exception to AO's generic "
          "prohibition on native subagents. The bound Fable engineer, at MAX, may delegate authorized "
          "implementation/correction tasks only to the pinned pr-sonnet and pr-opus native agents and to the room's "
          "pinned DeepSeek tools. Use one layer and at most two concurrent native subagents. Built-in agent types, "
          "native forks, workflows, teams, autonomous review skills and extra AO workers are not authorized. Fable "
          "verifies results and retains the final engineering verdict. Specification reviewers and Astra acceptance "
          "reviewers remain read-only and do not delegate. This exception does not authorize any continuation of an "
          "uncertain/refused turn, model fallback, new product scope or publication.")
PRESERVED = (("worker.agent", ("worker", "agent")), ("worker.model", ("worker", "agentConfig", "model")),
             ("worker.permissions", ("worker", "agentConfig", "permissions")),
             ("containerReap.disabled", ("containerReap", "disabled")), ("defaultBranch", ("defaultBranch",)))
MEANING = ("Configured worktree files, executable identity and observed AO project rules, validated offline. Not "
           "proof of native enforcement, served models or efforts, concurrency/depth caps, resumption paths or compaction.")
NOT_CONFIGURED = ("Prepared before native routing or not prepared: no native delegation protection exists for this "
                  "worker and none is claimed; the room stays readable and is never relabeled.")
MAX_SETTINGS_BYTES = 4_000_000


def _normalized(text):
    return " ".join(text.split())


def _scalar(value):
    # A JSON string is a valid YAML double-quoted scalar, so colons and quotes stay safe.
    return json.dumps(value, ensure_ascii=True)


def agent_definition(name):
    model = MODELS[name]
    if name == "pr-sonnet":
        description = ("Project Room mechanical implementation and test worker on Sonnet. Use only for exact, "
                       "self-contained instructions from the Fable engineer: apply prepared edits or diffs, write or "
                       "run the named tests and gates, and report results verbatim. No design decisions, delegation, "
                       "skills or messaging.")
        disallowed = "Agent, Workflow, Task, Skill, SendMessage, TeamCreate, " + CHILD_DENIED
        rules = ("- Do exactly the bounded task in the prompt; report anything ambiguous instead of deciding it.\n"
                 "- Verify the anchors, types and interfaces named in the task before editing.\n"
                 "- Run only the tests and gates the task names and quote their actual output.\n"
                 "- Never launch agents, workflows, skills or messages, publish, or touch other checkouts.\n"
                 "- Finish with what changed, what was verified, and every limitation you hit.")
    else:
        description = ("Project Room bounded judgment, review and authenticated browser worker on Opus. Use only for "
                       "one bounded module design, debugging or review question, or one authenticated browser task "
                       "when the AO browser capability is present; report findings with evidence. No delegation, "
                       "workflows or messaging; the only permitted skill is the pinned browser skill.")
        disallowed = "Agent, Workflow, Task, SendMessage, TeamCreate, " + CHILD_DENIED
        rules = ("- Answer only the bounded question in the prompt with concrete evidence and file references.\n"
                 "- Do not self-certify: report uncertainty and what remains unverified.\n"
                 "- Browser work uses only the pinned browser skill and the capability already present; never "
                 "reauthenticate, copy credentials or claim a passing test you did not observe.\n"
                 "- Never launch agents, workflows or messages, publish, or touch other checkouts.\n"
                 "- Finish with findings, evidence, and every limitation you hit.")
    fields = {"name": name, "description": description, "model": model, "effort": EFFORT, "disallowedTools": disallowed}
    front = "".join(key + ": " + _scalar(fields[key]) + "\n" for key in FRONTMATTER)
    return ("---\n" + front + "---\n\nYou are a bounded Project Room native worker. The Fable engineer that launched "
            "you verifies your result and owns the engineering verdict.\n\n" + rules + "\n")


def parse_definition(text):
    if not text.startswith("---\n"):
        raise RoomError("Agent definition lacks frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RoomError("Agent definition frontmatter is unterminated")
    fields = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise RoomError("Malformed agent frontmatter line")
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise RoomError("Malformed quoted agent frontmatter value") from exc
            if not isinstance(value, str):
                raise RoomError("Agent frontmatter values must be text")
        fields[key.strip()] = value
    return fields


def validate_definition(name, text):
    fields = parse_definition(text)
    if fields.get("name") != name:
        raise RoomError("Agent definition name mismatch")
    model = fields.get("model", "")
    if not model:
        raise RoomError("Agent definition omits an explicit model")
    if model == "inherit" or "fable" in model.lower():
        raise RoomError("Agent definition inherits or names the Fable model")
    if model != MODELS.get(name):
        raise RoomError("Agent definition model is not the pinned mapping")
    if fields.get("effort") != EFFORT:
        raise RoomError("Agent definition effort is not max")
    tools = {item.strip() for item in fields.get("disallowedTools", "").split(",")}
    if not {"Agent", "Workflow", "SendMessage"} <= tools:
        raise RoomError("Agent definition could delegate or message further")
    if name == "pr-sonnet" and "Skill" not in tools:
        raise RoomError("pr-sonnet must not invoke skills")
    if not set(SUBMISSION_SERVERS + SUBMISSION_TOOLS) <= tools:
        raise RoomError("Agent definition could submit delegate or room work through inherited MCP tools")
    return fields


def hook_command(python, guard):
    quoted = " ".join(shlex.quote(str(item)) for item in (python, guard))
    # Map every non-zero exit (missing interpreter or guard, crash) to 2 so the call is blocked.
    return quoted + '; s=$?; [ "$s" -eq 0 ] || exit 2'


def _mapping(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RoomError(label + " must be a JSON object")
    return value


def _contradictory_env(env):
    return sorted(key for key in env if key in CONTRADICTORY_ENV or (key in ENV and str(env[key]) != ENV[key]))


def _model_policy(data, label):
    """Refuse settings whose effective model mapping cannot be proven to keep the pinned workers."""
    if data.get("modelOverrides"):
        raise RoomError(label + " Claude settings define modelOverrides; effective model mappings cannot be proven safe")
    allowed = data.get("availableModels")
    if allowed is not None and (not isinstance(allowed, list) or any(model not in allowed for model in MODELS.values())):
        raise RoomError(label + " Claude settings restrict availableModels without both pinned worker models")


def settings_document(existing, command):
    existing = _mapping(existing, "Existing local settings")
    if existing.get("disableAllHooks") or existing.get("allowManagedHooksOnly"):
        raise RoomError("Existing local settings disable or suppress local hooks")
    _model_policy(existing, "existing local")
    env = dict(_mapping(existing.get("env"), "Existing local settings env"))
    bad = _contradictory_env(env)
    if bad:
        raise RoomError("Existing local settings override native routing knobs: " + ", ".join(bad))
    env.update(ENV)
    permissions = dict(_mapping(existing.get("permissions"), "Existing local permissions"))
    deny = permissions.get("deny") or []
    if not isinstance(deny, list):
        raise RoomError("Existing local deny rules must be a list")
    deny = list(deny) + [rule for rule in DENY if rule not in deny]
    permissions["deny"] = deny
    hooks = dict(_mapping(existing.get("hooks"), "Existing local hooks"))
    pre = hooks.get("PreToolUse") or []
    if not isinstance(pre, list):
        raise RoomError("Existing local PreToolUse hooks must be a list")
    entry = {"matcher": MATCHER, "hooks": [{"type": "command", "command": command, "timeout": 30}]}
    pre = list(pre) + ([] if entry in pre else [entry])
    hooks["PreToolUse"] = pre
    return {**existing, "env": env, "permissions": permissions, "hooks": hooks}


def check_settings(settings, routing):
    settings = _mapping(settings, "Local settings")
    if settings.get("disableAllHooks") or settings.get("allowManagedHooksOnly"):
        raise RoomError("Local settings disable or suppress local hooks")
    _model_policy(settings, "local")
    env = _mapping(settings.get("env"), "Local settings env")
    if any(env.get(key) != value for key, value in routing["env"].items()) or _contradictory_env(env):
        raise RoomError("Local settings no longer pin the native routing knobs")
    deny = _mapping(settings.get("permissions"), "Local permissions").get("deny") or []
    if any(rule not in deny for rule in routing["deny"]):
        raise RoomError("Local settings no longer deny the review and workflow routes")
    entries = _mapping(settings.get("hooks"), "Local hooks").get("PreToolUse") or []
    hooks = [hook for entry in entries if isinstance(entry, dict) and entry.get("matcher") == routing["matcher"]
             for hook in (entry.get("hooks") or []) if isinstance(hook, dict)]
    if not any(hook.get("type") == "command" and hook.get("command") == routing["hook_command"] for hook in hooks):
        raise RoomError("Local settings no longer run the routing guard")


def managed_settings_path(environ):
    explicit = environ.get("CLAUDE_CODE_MANAGED_SETTINGS_PATH")
    if explicit:
        return Path(explicit)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _settings_file(path):
    """Return the parsed settings object, None only when the file is genuinely absent."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_SETTINGS_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise RoomError(str(path) + " is unreadable: " + (exc.strerror or type(exc).__name__)) from exc
    if len(data) > MAX_SETTINGS_BYTES:
        raise RoomError(str(path) + " is oversized")
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise RoomError(str(path) + " is not valid JSON") from exc
    return _mapping(value, str(path))


def contradictions(config_dir, worktree=None, environ=None):
    """Refuse configuration that would silently override or disable the routing protections."""
    sources = [("user", Path(config_dir) / "settings.json"), ("managed", managed_settings_path(environ or os.environ))]
    if worktree is not None:
        sources.append(("project", Path(worktree) / ".claude" / "settings.json"))
    for label, path in sources:
        data = _settings_file(path)
        if data is None:
            continue
        if data.get("disableAllHooks"):
            raise RoomError(label + " Claude settings disable hooks; native routing cannot be protected")
        if data.get("allowManagedHooksOnly"):
            raise RoomError(label + " Claude settings allow managed hooks only; the local routing guard would be suppressed")
        _model_policy(data, label)
        bad = _contradictory_env(_mapping(data.get("env"), label + " settings env"))
        if bad:
            raise RoomError(label + " Claude settings override native routing knobs: " + ", ".join(bad))
    for name in MODELS:
        if (Path(config_dir) / "agents" / (name + ".md")).exists():
            raise RoomError("user-level agent definition " + name + " exists; the pinned worktree definition cannot be proven effective")
    if environ is not None:
        bad = _contradictory_env({key: environ[key] for key in environ if key in ENV or key in CONTRADICTORY_ENV})
        if bad:
            raise RoomError("Process environment overrides native routing knobs: " + ", ".join(bad))


def _lookup(config, path):
    value = config
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def observe_rules(client, state):
    """Read the configured AO project rules; this is configuration, not the text inside a running session."""
    from ao_project_room import digest
    raw = client.request("GET", "/projects/" + state["ao_project_id"])
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    if not isinstance(project, dict) or project.get("id", project.get("projectId")) != state["ao_project_id"]:
        raise RoomError("AO project identity mismatch while reading its rules")
    config = _mapping(project.get("config"), "AO project configuration")
    inline = config.get("agentRules")
    inline = inline if isinstance(inline, str) else ""
    file_name = config.get("agentRulesFile")
    file_name = file_name if isinstance(file_name, str) and file_name.strip() else ""
    file_text, file_digest = "", None
    if file_name:
        relative = Path(file_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RoomError("AO agentRulesFile is not repo-relative")
        try:
            data = owned_bytes(Path(state["project_path"]) / relative)
        except (OSError, ValueError) as exc:
            raise RoomError("AO agentRulesFile is unreadable") from exc
        file_text, file_digest = data.decode(errors="replace"), digest(data)
    env = _mapping(config.get("env"), "AO project env")
    return {"agent_rules_sha256": digest(inline.encode()), "agent_rules_file": file_name or None,
            "agent_rules_file_sha256": file_digest, "clause_present": _normalized(CLAUSE) in _normalized(inline + "\n" + file_text),
            "preserved_fields": {label: _lookup(config, path) for label, path in PRESERVED},
            "contradictory_env": _contradictory_env(env),
            "recorded_env": {key: env[key] for key in sorted(env) if key in RECORDED_ENV},
            "meaning": "Configured AO project rules observed through the project API; not the text inside a running session"}


def rules_match(observed, snapshot):
    keys = ("agent_rules_sha256", "agent_rules_file", "agent_rules_file_sha256", "preserved_fields", "recorded_env")
    return (bool(observed.get("clause_present")) and not observed.get("contradictory_env")
            and all(observed.get(key) == snapshot.get(key) for key in keys))


def context(service, directory, state):
    from ao_project_room import read
    room = read(directory / "settings.json") if (directory / "settings.json").exists() else {}
    controller_path = service.root.parent / "config.json"
    controller = read(controller_path) if controller_path.exists() else {}
    override = room.get("claude_config_dir_override") or controller.get("claude_config_dir_override")
    recorded = room.get("claude_config_dir") or controller.get("claude_config_dir")
    candidates = {str(Path(value).expanduser().resolve()) for value in (override, recorded) if isinstance(value, str) and value}
    if len(candidates) > 1:
        raise RoomError("Claude configuration directory override and recorded directory disagree")
    config_dir = candidates.pop() if candidates else str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")).expanduser().resolve())
    return {"claude_config_dir": config_dir, "claude_bin": room.get("claude_bin") or controller.get("claude_bin"), "python": sys.executable}


def claude_evidence(claude_bin):
    """Bounded executable identity: path, size, mtime and the reported --version line. Never inference."""
    if not claude_bin:
        return {"path": None, "size": None, "mtime_ns": None, "version": None, "error": "no configured claude_bin"}
    path = Path(claude_bin).expanduser()
    if not path.is_absolute():
        return {"path": str(path), "size": None, "mtime_ns": None, "version": None, "error": "claude_bin is not an absolute path"}
    try:
        info = path.stat()
    except OSError as exc:
        return {"path": str(path), "size": None, "mtime_ns": None, "version": None, "error": "stat failed: " + type(exc).__name__}
    evidence = {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns, "version": None, "error": None}
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, timeout=20)
        lines = result.stdout.decode(errors="replace").strip().splitlines()
        if result.returncode == 0 and lines:
            evidence["version"] = lines[0][:200]
        else:
            evidence["error"] = "version probe exit " + str(result.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        evidence["error"] = "version probe failed: " + type(exc).__name__
    return evidence


def check_claude(evidence):
    if not evidence.get("path") or evidence.get("size") is None:
        return  # identity was never recorded; status stays below verified
    try:
        info = Path(evidence["path"]).stat()
    except OSError as exc:
        raise RoomError("Claude executable is missing: " + evidence["path"]) from exc
    if (info.st_size, info.st_mtime_ns) != (evidence["size"], evidence["mtime_ns"]):
        raise RoomError("Claude executable changed since preparation")


def _require_ignored(worktree, relative):
    tracked = subprocess.run(["git", "-C", str(worktree), "ls-files", "--error-unmatch", "--", relative], capture_output=True, timeout=15)
    if tracked.returncode == 0:
        raise RoomError(relative + " is tracked; routing files must be ignored runtime configuration")
    ignored = subprocess.run(["git", "-C", str(worktree), "check-ignore", "-q", "--", relative], capture_output=True, timeout=15)
    if ignored.returncode == 1:
        raise RoomError(relative + " is not ignored by this repository; add the ignore rule before adopting native routing (shared Git exclusions are never edited)")
    if ignored.returncode != 0:
        raise RoomError("git check-ignore failed for " + relative + ": " + ignored.stderr.decode(errors="replace")[:300])


def _target(worktree, relative):
    """Refuse symlinked or non-directory parents, special targets and unsafe temporary paths before any write."""
    path = worktree
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise RoomError(relative + " traverses a symlink; refusing to write outside the worktree")
    if path.parent.exists() and not path.parent.is_dir():
        raise RoomError(relative + " parent is not a directory")
    if path.exists() and not path.is_file():
        raise RoomError(relative + " is not a regular file")
    pending = path.with_name(".pending-" + path.name)
    if pending.is_symlink() or pending.exists():
        raise RoomError("Unsafe existing temporary target for " + relative)
    return path, pending


def _write(worktree, relative, data, previous=None):
    path, pending = _target(worktree, relative)  # re-walk the parents immediately before touching the tree
    if previous is not None and path.exists() and owned_bytes(path) != previous:
        raise RoomError(relative + " changed while preparation was merging it; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if pending.exists():
            os.unlink(pending)


def prepare(service, directory, state, worktree, prepared):
    """Write and snapshot the routing files for one prepared worktree; refuse before writing on any conflict."""
    from ao_project_room import atomic, digest
    stage = "context"
    try:
        settings = context(service, directory, state)
        stage = "ignore"
        for relative in FILES:
            _target(worktree, relative)  # symlinked or special parents are refused before git is consulted
            _require_ignored(worktree, relative)
        stage = "settings"
        contradictions(settings["claude_config_dir"], worktree, os.environ)
        stage = "rules"
        rules = observe_rules(service.client(state), state)
        if not rules["clause_present"]:
            raise RoomError("AO project agentRules do not contain the authorized Project Room delegation clause; install it before preparing the engineer")
        if rules["contradictory_env"]:
            raise RoomError("AO project env overrides native routing knobs: " + ", ".join(rules["contradictory_env"]))
        stage = "guard"
        data = Path(__file__).with_name("ao_routing_guard.py").read_bytes()
        guard_hash = digest(data)
        guard = service.root / "launchers" / (guard_hash + ".py")
        guard.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if guard.exists():
            if owned_bytes(guard) != data:
                raise RoomError("Existing private routing guard was modified")
        else:
            with guard.open("xb") as stream:
                stream.write(data)
            guard.chmod(0o600)
        command = hook_command(settings["python"], guard)
        stage = "claude"
        claude = claude_evidence(settings["claude_bin"])
        stage = "render"
        documents = {}
        for name in MODELS:
            text = agent_definition(name)
            validate_definition(name, text)
            documents[".claude/agents/" + name + ".md"] = text.encode()
        settings_path, _ = _target(worktree, ".claude/settings.local.json")
        existing_bytes = owned_bytes(settings_path) if settings_path.exists() else None
        existing = json.loads(existing_bytes) if existing_bytes is not None else None
        merged = settings_document(existing, command)
        documents[".claude/settings.local.json"] = (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode()
        stage = "preflight"
        plan = []
        for relative, content in documents.items():
            path, _ = _target(worktree, relative)
            if path.exists():
                if owned_bytes(path) == content:
                    continue
                if relative != ".claude/settings.local.json":
                    raise RoomError("Existing " + relative + " differs from the pinned definition; remove or align it")
            plan.append((relative, content))
        stage = "write"
        for relative, content in plan:
            _write(worktree, relative, content, existing_bytes if relative == ".claude/settings.local.json" else None)
        files = {relative: digest(owned_bytes(worktree / relative)) for relative in FILES}
        prepared["routing"] = {
            "version": 2, "execution_policy": EXECUTION_POLICY,
            "files": files, "guard_path": str(guard), "guard_sha256": guard_hash, "hook_command": command,
            "python": settings["python"], "agents": dict(MODELS), "effort": EFFORT, "env": dict(ENV), "deny": list(DENY),
            "matcher": MATCHER, "browser_skill": BROWSER_SKILL, "claude_config_dir": settings["claude_config_dir"],
            "claude": claude, "rules": rules,
            "prepare_environment": {key: os.environ[key] for key in sorted(os.environ) if key in RECORDED_ENV},
            "meaning": MEANING}
        return prepared["routing"]
    except (RoomError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        evidence = {"stage": stage, "error": str(exc)[:2000], "worktree": str(worktree), "room_id": state["room_id"], "observed_at": time.time()}
        atomic(directory / ("routing-error-" + digest(evidence)[:16] + ".json"), evidence)
        raise RoomError("Native routing preparation refused at " + stage + ": " + str(exc)) from exc


def validate_local(prepared):
    """Offline re-validation: pinned files, ignore status, guard, interpreter, executable identity, surrounding settings."""
    from ao_project_room import digest
    routing = prepared.get("routing") if prepared else None
    if not routing:
        return None
    try:
        version = routing.get("version", 1)
        if type(version) is not int or version not in (1, 2):
            raise RoomError("Unsupported native routing version")
        if version == 2 and (routing.get("execution_policy") != EXECUTION_POLICY or routing.get("matcher") != MATCHER):
            raise RoomError("Native routing no longer pins orchestration ownership for every tool")
        worktree = Path(prepared["worktree"])
        for relative, expected in routing["files"].items():
            _require_ignored(worktree, relative)
            data = owned_bytes(worktree / relative)
            if digest(data) != expected:
                raise RoomError("Pinned routing file changed: " + relative)
            if relative.endswith(".md"):
                validate_definition(Path(relative).stem, data.decode())
            else:
                check_settings(json.loads(data), routing)
        if digest(owned_bytes(routing["guard_path"])) != routing["guard_sha256"]:
            raise RoomError("Private routing guard changed")
        if routing.get("browser_skill") != BROWSER_SKILL:
            raise RoomError("Pinned browser skill differs from the exact observed skill")
        if not os.access(routing["python"], os.X_OK):
            raise RoomError("Routing guard interpreter is unavailable")
        check_claude(routing.get("claude") or {})
        contradictions(routing["claude_config_dir"], worktree)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise RoomError("Native routing evidence is unreadable or inconsistent: " + str(exc)) from exc
    return routing


def record_observation(service, directory, state, observed, source, consistent):
    from ao_project_room import atomic, digest
    from ao_project_room import read
    relative = "routing/observations/" + digest(observed)[:16] + ".json"
    target = directory / relative
    if target.exists():
        if read(target) != observed:
            raise RoomError("Saved routing observation evidence was modified; preserve it for diagnosis")
    else:
        atomic(target, observed)
    state["routing_rules"] = {"observed_at": time.time(), "source": source, "consistent": consistent, "evidence": relative,
                              "evidence_sha256": digest(observed), "clause_present": observed["clause_present"],
                              "agent_rules_sha256": observed["agent_rules_sha256"]}
    service.save(directory, state)


def before_dispatch(service, directory, state, prepared, purpose):
    """Local validation for every engineer dispatch; live rules check before implementation/correction."""
    routing = validate_local(prepared)
    if routing is None or purpose not in ("implementation", "correction"):
        return routing
    try:
        observed = observe_rules(service.client(state), state)
    except RoomError as exc:
        state["routing_rules"] = {"observed_at": time.time(), "source": "dispatch", "consistent": False, "error": str(exc)[:1000]}
        service.save(directory, state)
        raise RoomError("AO project rules could not be read before dispatch: " + str(exc)) from exc
    consistent = rules_match(observed, routing["rules"])
    record_observation(service, directory, state, observed, "dispatch", consistent)
    if not consistent:
        raise RoomError("AO project rules changed since preparation (delegation clause, rules digest, preserved fields or env); restore them or prepare a new room")
    return routing


def observe_on_sync(service, directory, state):
    """Record the latest configured rules for a routing room without refusing the sync."""
    import ao_delegates
    if not state.get("preparation"):
        return
    try:
        prepared = ao_delegates.preparation(directory, state)
    except RoomError:
        return
    routing = prepared.get("routing")
    if not routing:
        return
    try:
        observed = observe_rules(service.client(state), state)
    except RoomError as exc:
        state["routing_rules"] = {"observed_at": time.time(), "source": "sync", "consistent": False, "error": str(exc)[:1000]}
        service.save(directory, state)
        return
    record_observation(service, directory, state, observed, "sync", rules_match(observed, routing["rules"]))


def status(prepared, state, directory=None):
    if not prepared or not prepared.get("routing"):
        return {"status": "not_configured", "error": None, "meaning": NOT_CONFIGURED}
    routing = prepared["routing"]
    claude = routing.get("claude") or {}
    summary = {"agents": routing["agents"], "effort": routing["effort"], "env": routing["env"], "deny": routing["deny"],
               "execution_policy": routing.get("execution_policy") if routing.get("version") == 2 else "historical_unrestricted_root",
               "browser_skill": routing["browser_skill"], "files": routing["files"], "guard_sha256": routing["guard_sha256"],
               "claude": claude, "rules_snapshot": routing["rules"], "last_observed_rules": state.get("routing_rules"),
               "meaning": MEANING}
    try:
        validate_local(prepared)
    except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "unverified", "error": str(exc), **summary}
    last = state.get("routing_rules")
    if last is None:
        return {"status": "configured", "error": None, **summary}
    if last.get("error") or not last.get("consistent"):
        return {"status": "unverified", "error": last.get("error") or "Last observed AO project rules differ from the preparation snapshot", **summary}
    if directory is not None:
        try:
            from ao_project_room import digest, read
            evidence = read(directory / last["evidence"])
            if digest(evidence) != last.get("evidence_sha256") or not rules_match(evidence, routing["rules"]):
                raise RoomError("Saved routing observation evidence is missing, modified or inconsistent")
        except (RoomError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "unverified", "error": "Routing observation evidence problem: " + str(exc), **summary}
    if not claude.get("version"):
        return {"status": "configured", "error": None, "note": "Claude executable version evidence is missing: " + str(claude.get("error") or "unknown"), **summary}
    return {"status": "verified", "error": None, **summary}


def packet_text(prepared):
    if prepared and prepared.get("routing"):
        agents = prepared["routing"]["agents"]
        ownership = ("Fable is the orchestrator: inspect evidence with Read/Grep/Glob, direct the pinned provider and "
                     "native workers, adjudicate results and give the final engineering verdict. The guard denies root "
                     "shell commands, edits, tests, browser work and unknown execution tools. Routine implementation "
                     "and checks belong to the assigned operator or delegates, including probes used for verification. "
                     "If a check is assigned to Astra, request its result and wait; do not duplicate it or reassign it "
                     "without an actual task change. Do not reconstruct specifications or hashes: the controller "
                     "checks exact source, native identity and candidate evidence; use the supplied identities. "
                     "Choose Sonnet or Opus when needed for full quality; judgment remains yours. If the authorized "
                     "tools cannot achieve the quality bar, report the capability gap instead of bypassing the guard. "
                     if prepared["routing"].get("version") == 2 else "")
        return (ownership + "Native delegation routing is configured for this worktree: launch only the pinned native agents pr-sonnet ("
                + agents["pr-sonnet"] + ", mechanical implementation and tests) and pr-opus (" + agents["pr-opus"]
                + ", bounded judgment/review and the pinned browser skill when the AO browser capability is present); "
                "one layer, at most two concurrent, no model overrides, built-in agent types, forks, isolation, resume, "
                "messaging, workflows, teams or review skills; the routing guard denies other routes. Record each native "
                "route in routing_log with the requested model and the observed native evidence.")
    return ("Native delegation routing is not configured for this worktree (prepared before routing protections): do "
            "not launch native subagents, workflows or review skills; use the pinned delegate tools and Fable only, "
            "and record that limitation.")
