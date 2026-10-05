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

import copy
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from ao_delegate_launcher import owned_bytes
from room import RoomError

# Worker selectors are frozen record values. New preparations pin the family aliases,
# so Claude Code resolves the latest available model within each family; retained
# preparations that recorded the historical exact pins keep validating against their
# own map. No other map is supported, and a map is never compared with a moving default.
EXACT_AGENTS = {"pr-sonnet": "claude-sonnet-5", "pr-opus": "claude-opus-5"}
FAMILY_AGENTS = {"pr-sonnet": "sonnet", "pr-opus": "opus"}
# The historical renderer default. Old records and the unchanged reproduction of their bundles keep
# this map; a new preparation passes its source-qualified exact models explicitly instead.
MODELS = FAMILY_AGENTS
AGENT_SELECTION = {name: {"kind": "family", "family": family} for name, family in FAMILY_AGENTS.items()}
AGENT_IDENTITY_BASIS = "configured family selector; child execution identity is not attributed by the controller"
QUALIFIED_IDENTITY_BASIS = ("operator-selected source qualification; the exact expected worker model comes from the "
                            "retained qualification artifact and the controller does not attribute actual child model identity")
WORKER_FAMILIES = {"pr-sonnet": "sonnet", "pr-opus": "opus"}
WORKER_QUALIFICATION_VERSION = 1
WORKER_QUALIFICATION_FIELDS = frozenset(("version", "snapshot", "families", "effort", "basis"))
WORKER_SNAPSHOT_FIELDS = frozenset(("artifact", "sha256", "record", "evidence"))
WORKER_REFERENCE_FIELDS = frozenset(("routing_sha256", "routing_version", "qualification_sha256",
                                     "qualification_record"))
WORKER_QUALIFICATION_BASIS = ("the enabled worker families are qualified to exact expected models from the operator-selected "
                              "retained source evidence; family intent and MAX are configured, not observed")
QUALIFIED_WORKER_IDENTITY = ("Source-qualified family selectors: expected exact models come from the retained operator-selected "
                             "qualification artifact, while the controller does not attribute actual child model identity and "
                             "effort max is configured intent, not observed effective effort. Family intent is recorded; a "
                             "served child model is never inferred.")
AGENT_FIELDS = ("agents", "agent_selection", "agent_identity_basis", "worker_qualification")
# These keys remap the family aliases (the bundled Agent SDK recognizes them and AO 0.13.1 treats them as
# the effective configured alias); exact pins were never subject to them, so only family maps refuse them.
ALIAS_ENV = ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL")
# A custom routing destination contradicts the declared first-party qualification scope. Only the key
# name is ever reported, so no URL or credential text reaches diagnostics.
GATEWAY_ENV = ("ANTHROPIC_BASE_URL",)
EFFORT = "max"
WORKER_IDENTITY = ("Configured selectors, not attributed identities: the controller does not attribute actual child model "
                   "identity, and effort max is configured intent, not observed effective effort.")
FAMILY_IDENTITY = (" Per Claude Code documentation, a family alias in the parent session's own family runs on the parent's "
                   "exact model (an Opus 5.5 parent runs pr-opus on Opus 5.5); an alias outside it, such as sonnet under an "
                   "Opus parent, resolves on its own under the installed CLI, provider and account restrictions.")
ENV = {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2",
       "CLAUDE_CODE_DISABLE_WORKFLOWS": "1", "CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS": "1"}
# This setting is pinned by new local-settings bytes, not added to historical
# routing env/rules snapshots that retained preparations must still reproduce.
FOREGROUND_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
# Headless Claude Code offers the TaskCreate/TaskUpdate/TaskList/TaskGet tools only for an
# allow-list of older models, interactive runs, or this variable; pinned the same way.
TASK_TOOLS_ENV = {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "true"}
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
DELEGATING_PURPOSES = ("implementation", "correction")
UNQUALIFIED_ROUTING = ("This routing record has no source-qualified worker selection; new delegating execution requires the "
                       "audited explicit routing refresh with agent_selection='qualified'. Reading the old record and its "
                       "history remains permitted and nothing was changed.")
NOT_CONFIGURED = ("Prepared before native routing or not prepared: no native delegation protection exists for this "
                  "worker and none is claimed; the room stays readable and is never relabeled.")
MAX_SETTINGS_BYTES = 4_000_000
COMPACTION_DEFAULT = 250_000
COMPACTION_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
COMPACTION_OVERRIDES = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT",
                        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")


def _normalized(text):
    return " ".join(text.split())


def _scalar(value):
    # A JSON string is a valid YAML double-quoted scalar, so colons and quotes stay safe.
    return json.dumps(value, ensure_ascii=True)


def _hex_digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def qualified_worker_map(value):
    """Authenticate one versioned worker qualification value and return its exact role map.

    The map is derived only from the recorded artifact snapshot: the expected model of each enabled
    worker family, at MAX. Retained room bytes and the effective executable are verified separately by
    callers that have a room and an executable, so this helper stays pure and reads no mutable config.
    """
    from ao_model_qualification import digest as qualification_digest
    from ao_model_qualification import evidence_entries, record_path, validate_qualification
    if not isinstance(value, dict) or set(value) != WORKER_QUALIFICATION_FIELDS:
        raise RoomError("Recorded worker qualification declares exactly version, snapshot, families, effort and basis")
    if type(value["version"]) is not int or value["version"] != WORKER_QUALIFICATION_VERSION:
        raise RoomError("Unsupported recorded worker qualification version")
    if value["effort"] != EFFORT:
        raise RoomError("Recorded worker qualification is not at max effort")
    if not isinstance(value["basis"], str) or not value["basis"].strip():
        raise RoomError("Recorded worker qualification lacks its recorded basis")
    snapshot = value["snapshot"]
    if (not isinstance(snapshot, dict) or set(snapshot) != WORKER_SNAPSHOT_FIELDS
            or not _hex_digest(snapshot.get("sha256"))):
        raise RoomError("Recorded worker qualification declares exactly its artifact, digest, retained record and evidence")
    artifact = snapshot["artifact"]
    validate_qualification(artifact, bundled=False)
    if qualification_digest(artifact) != snapshot["sha256"]:
        raise RoomError("Recorded worker qualification artifact does not match its digest")
    if snapshot["record"] != record_path(snapshot["sha256"]):
        raise RoomError("Recorded worker qualification names a different retained record")
    if snapshot["evidence"] != evidence_entries(artifact, snapshot["sha256"]):
        raise RoomError("Recorded worker qualification declares inconsistent retained source evidence")
    families = value["families"]
    if not isinstance(families, dict) or set(families) != set(WORKER_FAMILIES):
        raise RoomError("Recorded worker qualification maps exactly the enabled worker families")
    derived = {}
    for role, family in WORKER_FAMILIES.items():
        declared = artifact["families"].get(family)
        if declared is None:
            raise RoomError("Recorded worker qualification does not map worker family " + family)
        if families[role] != {"family": family, "expected_model": declared["expected_model"],
                              "minimum_claude_code_version": declared.get("minimum_claude_code_version")}:
            raise RoomError("Recorded worker qualification entry for " + role + " is not its selected artifact's own "
                            "family mapping")
        derived[role] = declared["expected_model"]
    return derived


def worker_floors(value):
    """The strongest effective compatibility floor per worker role of an authenticated qualification value."""
    from ao_engineering_model import required_minimum
    return {role: required_minimum(family, value["families"][role]) for role, family in WORKER_FAMILIES.items()}


def worker_qualification_value(qualification):
    """The versioned routing metadata block one retained qualification snapshot pins."""
    if (not isinstance(qualification, dict) or not _hex_digest(qualification.get("sha256"))
            or not isinstance(qualification.get("record"), str) or not isinstance(qualification.get("evidence"), list)
            or not isinstance(qualification.get("artifact"), dict)):
        raise RoomError("A retained worker qualification snapshot declares its artifact, digest, record and evidence")
    artifact = qualification["artifact"]
    families = {}
    for role, family in WORKER_FAMILIES.items():
        declared = artifact.get("families", {}).get(family) if isinstance(artifact.get("families"), dict) else None
        if declared is None:
            raise RoomError("The selected family qualification does not map worker family " + family)
        families[role] = {"family": family, "expected_model": declared["expected_model"],
                          "minimum_claude_code_version": declared.get("minimum_claude_code_version")}
    value = {"version": WORKER_QUALIFICATION_VERSION,
             "snapshot": {"artifact": copy.deepcopy(artifact), "sha256": qualification["sha256"],
                          "record": qualification["record"], "evidence": copy.deepcopy(qualification["evidence"])},
             "families": families, "effort": EFFORT, "basis": WORKER_QUALIFICATION_BASIS}
    qualified_worker_map(value)  # nothing may pin a value that does not authenticate
    return value


def select_worker_qualification(service, directory):
    """Select the active operator-configured qualification once and retain it create-once in the room.

    A new preparation and a qualified refresh both need both enabled worker families qualified from the
    selected artifact before any runtime file is written. The private originals are required only here;
    the returned snapshot value pins the artifact and every selected source capture inside the room.
    """
    from ao_engineering_model import configured_qualification
    from ao_model_qualification import load, retain, snapshot
    pointer = configured_qualification(service.root)
    if pointer is None:
        raise RoomError("A source-qualified worker preparation requires the operator-selected family_qualification "
                        "artifact mapping both pr-sonnet (sonnet) and pr-opus (opus) to exact expected models; "
                        "configure it and prepare again. Nothing was written.")
    selected = load(pointer)
    for role, family in sorted(WORKER_FAMILIES.items()):
        if family not in selected["artifact"]["families"]:
            raise RoomError("The selected family qualification does not map worker family " + family + " required by "
                            + role + "; a source-qualified worker preparation needs both enabled worker families")
    return retain(directory, snapshot(selected))


def concrete_executable_evidence(evidence, label="the effective Claude executable"):
    """Refuse a partial executable identity; the lower pure comparison needs concrete supplied bytes."""
    if not isinstance(evidence, dict):
        raise RoomError("Qualified worker work requires concrete Claude executable evidence for " + label)
    error, path, size, mtime, version = (evidence.get("error"), evidence.get("path"), evidence.get("size"),
                                         evidence.get("mtime_ns"), evidence.get("version"))
    if (error is not None or not isinstance(path, str) or not path.startswith("/") or "\x00" in path
            or type(size) is not int or size < 0 or type(mtime) is not int or mtime < 0
            or not isinstance(version, str) or not version.strip()):
        raise RoomError("Qualified worker work requires concrete existing Claude executable identity for " + label
                        + ": an absolute path, nonnegative size and mtime, a version and no error. "
                        + str(error or "The supplied evidence is missing or partial"))


def check_qualified_floors(value, evidence):
    """Refuse when the concrete effective executable misses a qualified worker floor."""
    qualified_worker_map(value)
    concrete_executable_evidence(evidence)
    from ao_engineering_model import parse_version
    observed = parse_version(evidence.get("version"))
    for role, floor in sorted(worker_floors(value).items()):
        if floor is None:
            continue
        if observed is None or observed < parse_version(floor):
            raise RoomError(role + " requires Claude Code " + floor + " or newer; the effective executable reports "
                            + str(evidence.get("version") or "no version"))
    return evidence


def check_worker_executable(routing, evidence):
    """Refuse a qualified worker routing record whose effective executable misses a floor or its identity."""
    value = (routing or {}).get("worker_qualification")
    if value is None:
        return None
    return check_qualified_floors(value, evidence)


def recorded_agents(routing):
    """The routing record's own authenticated worker selectors, at MAX.

    Three shapes stay distinct: the historical exact pins, the historical family aliases, and a
    versioned worker qualification whose exact map is derived from its recorded source-qualified
    artifact snapshot. An arbitrary map of syntactically valid Claude identifiers is never accepted.
    """
    if not isinstance(routing, dict):
        raise RoomError("Recorded native worker routing is not an object")
    agents, value = routing.get("agents"), routing.get("worker_qualification")
    if value is not None:
        if routing.get("version") != 3:
            raise RoomError("Recorded worker qualification requires the version 3 routing record that pinned it")
        if agents != qualified_worker_map(value):
            raise RoomError("Recorded native worker map is not the exact map its source qualification derives")
        if routing.get("agent_selection") != AGENT_SELECTION:
            raise RoomError("Qualified worker selectors lack their exact recorded family intents")
        if routing.get("agent_identity_basis") != QUALIFIED_IDENTITY_BASIS:
            raise RoomError("Qualified worker selectors lack their exact recorded identity basis")
    elif routing.get("version") == 3:
        raise RoomError("A version 3 routing record requires its source-qualified worker qualification metadata")
    elif agents == FAMILY_AGENTS:
        if (routing.get("agent_selection") != AGENT_SELECTION
                or routing.get("agent_identity_basis") != AGENT_IDENTITY_BASIS):
            raise RoomError("Family worker selectors lack their exact recorded selection and identity basis")
    elif agents != EXACT_AGENTS or "agent_selection" in routing or "agent_identity_basis" in routing:
        raise RoomError("Recorded native worker models are neither the historical exact pins nor the family selectors")
    if routing.get("effort") != EFFORT:
        raise RoomError("Recorded native worker effort is not max")
    return dict(agents)


def selector_labels(agents, qualification=None):
    """Configured selectors as text for status; never a served or attributed model identity."""
    if qualification is not None:
        derived = qualified_worker_map(qualification)
        families = {role: qualification["families"][role]["family"] for role in sorted(derived)}
        return {name: "source-qualified exact model id " + str(model) + " (family intent " + families[name] + ")"
                for name, model in derived.items()} if isinstance(agents, dict) else {}
    kind = "family alias " if agents == FAMILY_AGENTS else "exact model id " if agents == EXACT_AGENTS else "unsupported selector "
    return {name: kind + str(model) for name, model in agents.items()} if isinstance(agents, dict) else {}


def worker_summary(routing):
    agents = recorded_agents(routing)
    value = routing.get("worker_qualification")
    summary = {"agents": agents, "worker_selectors": selector_labels(agents, value),
               "worker_identity": WORKER_IDENTITY + (FAMILY_IDENTITY if agents == FAMILY_AGENTS else "")}
    if value is not None:
        snapshot = value["snapshot"]
        summary["worker_identity"] = QUALIFIED_WORKER_IDENTITY
        summary["worker_qualification"] = {
            "version": value["version"], "sha256": snapshot["sha256"], "record": snapshot["record"],
            "revision": snapshot["artifact"]["revision"], "qualified_at": snapshot["artifact"]["qualified_at"],
            "families": {role: dict(value["families"][role]) for role in sorted(value["families"])},
            "effective_floors": worker_floors(value),
            "source_ids": {role: list(snapshot["artifact"]["families"][value["families"][role]["family"]]["source_ids"])
                           for role in sorted(value["families"])},
            "evidence": copy.deepcopy(snapshot["evidence"]), "basis": value["basis"],
            "meaning": "Expected worker model ids derived from the retained operator-selected source qualification; "
                       "not an attributed served identity, provider availability, entitlement or effective effort"}
    return summary


def agent_definition(name, model=None):
    model = MODELS[name] if model is None else model
    if name == "pr-sonnet":
        description = ("Project Room bounded implementation and test worker on Sonnet. Implement from self-contained "
                       "requirements, verified interfaces and acceptance checks supplied by Fable; apply an existing "
                       "payload when supplied, and run the named gates. Make routine local implementation choices "
                       "within the agreed contract; escalate unresolved requirements or cross-module design. No delegation, "
                       "skills or messaging.")
        disallowed = "Agent, Workflow, Task, Skill, SendMessage, TeamCreate, " + CHILD_DENIED
        rules = ("- Author the bounded implementation, supporting script or tests from the supplied requirements. "
                 "Fable need not dictate complete source. Preserve explicit exact-copy tasks when actually required.\n"
                 "- Make routine local choices within verified interfaces; report missing requirements, conflicting "
                 "evidence and cross-module decisions to Fable rather than inventing behavior.\n"
                 "- Verify the anchors, types and interfaces named in the task before editing.\n"
                 "- Run the named tests and gates. Preserve complete output in an authorized artifact; summarize "
                 "the observed result, failures and evidence paths without repeating full logs.\n"
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
            "you verifies your result and owns the engineering verdict.\n\n" + rules + "\n\n"
            "Use complete functional units and verification requirements to bound work. Do not rewrite readable, "
            "correct source to satisfy an arbitrary source-line or Write-call count. Preserve explicit product and "
            "format requirements. For inventories and logs, keep complete metadata in an authorized artifact and "
            "place operational evidence outside the candidate worktree or in an already ignored path; do not "
            "change ignore rules or add candidate files merely to store logs. Explicitly requested deliverables "
            "still belong in their authorized product paths. "
            "Return counts, relative paths, digests, material findings and limitations. Do not repeat raw directory "
            "listings, file contents or long absolute path prefixes by default. A read-only assignment never grants "
            "artifact-write permission: use an existing artifact or ask the operator to collect it. Report missing "
            "evidence honestly; concise reporting never replaces Fable's necessary independent inspection.\n")


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


def validate_definition(name, text, agents=None, qualification=None):
    """Validate against the preparation's recorded map; only a fresh historical render defaults to MODELS.

    ``qualification`` is the authenticated versioned worker qualification the map was derived from,
    when one exists: a source-qualified exact map is then accepted only while it still derives exactly
    from that recorded artifact, and never for a syntactically valid identifier on its own.
    """
    agents = MODELS if agents is None else agents
    if qualification is None:
        if agents not in (EXACT_AGENTS, FAMILY_AGENTS):
            raise RoomError("Recorded native worker models are neither the historical exact pins nor the family "
                            "selectors")
    else:
        if agents != qualified_worker_map(qualification):
            raise RoomError("Recorded native worker map is not the exact map its source qualification derives")
        if name not in agents:
            raise RoomError("Recorded native worker map does not pin this worker role")
    fields = parse_definition(text)
    if fields.get("name") != name:
        raise RoomError("Agent definition name mismatch")
    model = fields.get("model", "")
    if not model:
        raise RoomError("Agent definition omits an explicit model")
    if model == "inherit" or "fable" in model.lower():
        raise RoomError("Agent definition inherits or names the Fable model")
    if model != agents.get(name):
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


def _environment(value, label):
    """One process or project environment mapping: any supported read-only mapping of text keys.

    ``os.environ`` is an ``os._Environ``, not a ``dict``; a JSON-object check would reject the real
    process environment even when it is empty of conflicting keys. Recognized keys are still checked
    individually and no environment value is ever echoed in an error. JSON settings objects keep
    their own strict object check, and an explicitly supplied mapping is never replaced by ambient
    process state.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RoomError(label + " must be a mapping of text keys")
    return value


def _contradictory_env(env):
    return sorted(key for key in env if key in CONTRADICTORY_ENV or (key in ENV and str(env[key]) != ENV[key]))


def _alias_sensitive(agents, qualification=None):
    """True when this worker map resolves through family aliases or is claimed as source-qualified."""
    return agents == FAMILY_AGENTS or qualification is not None


def _model_policy(data, label, agents=None, qualification=None):
    """Refuse settings whose effective model mapping cannot be proven to keep the recorded worker selectors."""
    agents = MODELS if agents is None else agents
    if data.get("modelOverrides"):
        raise RoomError(label + " Claude settings define modelOverrides; effective model mappings cannot be proven safe")
    allowed = data.get("availableModels")
    if allowed is not None and (not isinstance(allowed, list) or any(model not in allowed for model in agents.values())):
        raise RoomError(label + " Claude settings restrict availableModels without both pinned worker models")
    if _alias_sensitive(agents, qualification):
        _alias_env(data.get("env"), label + " Claude settings")
    if qualification is not None:
        _gateway_env(data.get("env"), label + " Claude settings")


def _alias_env(value, label):
    """Family selectors must resolve through Claude Code's own alias mapping, never a configured remap."""
    env = _environment(value, label + " env")
    bad = [key for key in ALIAS_ENV if env.get(key) not in (None, "")]
    if bad:
        raise RoomError(label + ": " + ", ".join(bad) + " remaps a family worker alias; the configured family cannot be proven")


def _gateway_env(value, label):
    """Source-qualified work refuses a routing destination the declared first-party scope cannot cover.

    The value is never included: only the key name is reported, so no URL secret reaches diagnostics.
    """
    env = _environment(value, label + " env")
    if any(env.get(key) not in (None, "") for key in GATEWAY_ENV):
        raise RoomError(label + ": " + ", ".join(GATEWAY_ENV) + " changes the routing destination; the declared "
                        "first-party qualification scope cannot be asserted for an unverified gateway")


def _alias_project(client, state):
    raw = client.request("GET", "/projects/" + state["ao_project_id"])
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    if not isinstance(project, dict) or project.get("id", project.get("projectId")) != state["ao_project_id"]:
        raise RoomError("AO project identity mismatch while checking family worker aliases")
    _alias_env(_mapping(project.get("config"), "AO project configuration").get("env"), "AO project environment")


def _gateway_project(client, state):
    """Source-qualified work refuses an AO project environment that changes the routing destination.

    The concrete configured AO project mapping is read once, read-only; only the key name is ever
    reported, so no URL or credential text reaches diagnostics.
    """
    _gateway_env(_project_env(client, state), "AO project environment")


def _foreground_env(value, label):
    env = _environment(value, label + " env")
    if any(key in env and env[key] != expected for key, expected in FOREGROUND_ENV.items()):
        raise RoomError(label + " conflicts with foreground native delegation (CLAUDE_CODE_DISABLE_BACKGROUND_TASKS must be exactly '1')")
    if any(key in env and env[key] != expected for key, expected in TASK_TOOLS_ENV.items()):
        raise RoomError(label + " conflicts with the pinned native task tools (CLAUDE_CODE_ENABLE_TODO_TOOLS must be exactly 'true')")


def foreground_settings(settings):
    _foreground_env(settings.get("env"), "Local settings")
    return {**settings, "env": {**_mapping(settings.get("env"), "Local settings env"), **FOREGROUND_ENV, **TASK_TOOLS_ENV}}


def settings_document(existing, command, *, foreground=False, agents=None, qualification=None):
    existing = _mapping(existing, "Existing local settings")
    if existing.get("disableAllHooks") or existing.get("allowManagedHooksOnly"):
        raise RoomError("Existing local settings disable or suppress local hooks")
    _model_policy(existing, "existing local", agents, qualification)
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
    result = {**existing, "env": env, "permissions": permissions, "hooks": hooks}
    return foreground_settings(result) if foreground else result


def check_settings(settings, routing):
    settings = _mapping(settings, "Local settings")
    if settings.get("disableAllHooks") or settings.get("allowManagedHooksOnly"):
        raise RoomError("Local settings disable or suppress local hooks")
    qualification = routing.get("worker_qualification")
    _model_policy(settings, "local", recorded_agents(routing), qualification)
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
    if "compaction" in routing:
        policy = _compaction_policy(routing["compaction"])
        if (settings.get("autoCompactEnabled") is not True
                or type(settings.get("autoCompactWindow")) is not int
                or settings["autoCompactWindow"] != policy["window"]
                or env.get(COMPACTION_KEY) != str(policy["window"])
                or routing["env"].get(COMPACTION_KEY) != str(policy["window"])):
            raise RoomError("Local settings no longer pin the prepared compaction window")


def check_settings_document(settings, *, agents=None, foreground=False, qualification=None):
    """Safe read-only validation of one local/planned settings document; never writes it.

    ``agents`` is the checked worker map when available; without it, modelOverrides and alias
    remaps are still refused, while availableModels cannot be proven safe and is refused rather
    than silently accepted. ``qualification`` is the authenticated source-qualified worker
    metadata when the map came from one; it additionally forbids a custom routing destination.
    """
    settings = _mapping(settings, "Local settings")
    if settings.get("disableAllHooks") or settings.get("allowManagedHooksOnly"):
        raise RoomError("Local settings disable or suppress local hooks")
    if settings.get("modelOverrides"):
        raise RoomError("Local settings define modelOverrides; effective model mappings cannot be proven safe")
    if agents is not None:
        _model_policy(settings, "local", agents, qualification)
    else:
        _alias_env(settings.get("env"), "Local settings")
        if qualification is not None:
            _gateway_env(settings.get("env"), "Local settings")
        if settings.get("availableModels") is not None:
            raise RoomError("Local settings restrict availableModels; the worker map was not supplied to prove them")
    env = _mapping(settings.get("env"), "Local settings env")
    bad = _contradictory_env(env)
    if bad:
        raise RoomError("Local settings override native routing knobs: " + ", ".join(bad))
    if foreground:
        _foreground_env(env, "Local settings")
    return settings


def check_settings_file(path, *, agents=None, foreground=False, qualification=None):
    """Validate one settings file when it is present; genuine absence is not checked."""
    data = _settings_file(path)
    if data is None:
        return None
    check_settings_document(data, agents=agents, foreground=foreground, qualification=qualification)
    return data


def managed_settings_path(environ):
    """The managed settings path of one supported environment mapping; an explicit override wins.

    ``os.environ`` is an ``os._Environ``, not a dict, so the supported process mapping is accepted
    here as any read-only mapping; only the recognized key is read and only its own malformed type is
    refused, and the value itself is never echoed.
    """
    explicit = environ.get("CLAUDE_CODE_MANAGED_SETTINGS_PATH") if isinstance(environ, Mapping) else None
    if explicit is not None and not isinstance(explicit, str):
        raise RoomError("CLAUDE_CODE_MANAGED_SETTINGS_PATH must be a text path, not " + type(explicit).__name__)
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


def contradictions(config_dir, worktree=None, environ=None, *, foreground=False, agents=None, qualification=None):
    """Refuse configuration that would silently override or disable the routing protections.

    ``agents`` is the checked routing record's own authenticated worker map and ``qualification``
    its source-qualified metadata when one exists; only a fresh preparation uses the default.
    ``environ=None`` means the ambient process environment, while an explicitly supplied empty
    mapping is honored exactly and never falls back to unrelated ambient state.
    """
    sources = []
    if config_dir is not None:
        sources.append(("user", Path(config_dir) / "settings.json"))
    sources.append(("managed", managed_settings_path(os.environ if environ is None else environ)))
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
        _model_policy(data, label, agents, qualification)
        bad = _contradictory_env(_mapping(data.get("env"), label + " settings env"))
        if bad:
            raise RoomError(label + " Claude settings override native routing knobs: " + ", ".join(bad))
        if qualification is not None:
            _gateway_env(data.get("env"), label + " Claude settings")
        if foreground:
            _foreground_env(data.get("env"), label + " Claude settings")
    if foreground and worktree is not None:
        local = _settings_file(Path(worktree) / ".claude/settings.local.json")
        if local is not None:
            _foreground_env(local.get("env"), "Local settings")
    if config_dir is not None:
        for name in MODELS:
            if (Path(config_dir) / "agents" / (name + ".md")).exists():
                raise RoomError("user-level agent definition " + name + " exists; the pinned worktree definition cannot be proven effective")
    if environ is not None:
        bad = _contradictory_env({key: environ[key] for key in environ if key in ENV or key in CONTRADICTORY_ENV})
        if bad:
            raise RoomError("Process environment overrides native routing knobs: " + ", ".join(bad))
        if _alias_sensitive(agents, qualification):
            _alias_env(environ, "Process environment")
        if qualification is not None:
            _gateway_env(environ, "Process environment")
        if foreground:
            _foreground_env(dict(environ), "Process environment")


def _project_env(client, state):
    """The configured AO project environment mapping, read-only, for explicit admission evidence."""
    raw = client.request("GET", "/projects/" + state["ao_project_id"])
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    if not isinstance(project, dict) or project.get("id", project.get("projectId")) != state["ao_project_id"]:
        raise RoomError("AO project identity mismatch while reading its environment")
    return _mapping(_mapping(project.get("config"), "AO project configuration").get("env"), "AO project env")


def _foreground_project(client, state):
    raw = client.request("GET", "/projects/" + state["ao_project_id"])
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    if not isinstance(project, dict) or project.get("id", project.get("projectId")) != state["ao_project_id"]:
        raise RoomError("AO project identity mismatch while checking foreground native delegation")
    config = _mapping(project.get("config"), "AO project configuration")
    _foreground_env(config.get("env"), "AO project environment")


def foreground_configured(worktree, routing):
    """Read the pinned settings choice; only the hook can check its inherited process environment."""
    if routing.get("version") not in (2, 3):
        return False
    settings = _settings_file(Path(worktree) / ".claude/settings.local.json") or {}
    env = _mapping(settings.get("env"), "Local settings env")
    return all(env.get(key) == value for key, value in FOREGROUND_ENV.items())


def _compaction_policy(value):
    if (not isinstance(value, dict) or set(value) != {"version", "window"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["window"]) is not int or not 100_000 <= value["window"] <= 1_000_000):
        raise RoomError("Compaction window must be an integer from 100000 to 1000000")
    return value


def compaction_policy(service):
    """Select a default only for a fresh preparation; never reread defaults for an existing room."""
    config = _settings_file(service.root / "config.json") or {}
    return _compaction_policy({"version": 1, "window": config.get("auto_compact_window", COMPACTION_DEFAULT)})


def _compaction_env(policy, value, label):
    env = _mapping(value, label + " env")
    if any(env.get(key) not in (None, "") for key in COMPACTION_OVERRIDES):
        raise RoomError(label + " overrides or disables automatic compaction")
    if COMPACTION_KEY in env and env[COMPACTION_KEY] != str(policy["window"]):
        raise RoomError(label + " conflicts with the prepared compaction window")


def _compaction_sources(policy, config_dir, worktree, environ):
    _compaction_policy(policy)
    sources = (("user", Path(config_dir) / "settings.json"),
               ("managed", managed_settings_path(environ)),
               ("project", Path(worktree) / ".claude/settings.json"),
               ("local", Path(worktree) / ".claude/settings.local.json"))
    for label, path in sources:
        data = _settings_file(path)
        if data is None:
            continue
        if "autoCompactEnabled" in data and data["autoCompactEnabled"] is not True:
            raise RoomError(label + " settings disable automatic compaction")
        _compaction_env(policy, data.get("env"), label + " settings")
    _compaction_env(policy, dict(environ), "Process environment")


def _compaction_project(client, state, policy):
    raw = client.request("GET", "/projects/" + state["ao_project_id"])
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    if not isinstance(project, dict) or project.get("id", project.get("projectId")) != state["ao_project_id"]:
        raise RoomError("AO project identity mismatch while checking compaction")
    config = _mapping(project.get("config"), "AO project configuration")
    _compaction_env(policy, config.get("env"), "AO project environment")


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
        if isinstance(override, str) and not Path(override).expanduser().is_absolute():
            raise RoomError("Saved relative Claude configuration override disagrees with its recorded directory from this cwd; "
                            "run controller setup from the original matching directory. Existing room snapshots remain unchanged "
                            "and require explicit recovery if they still disagree; no account directory was selected")
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
    """Write and snapshot the routing files for one prepared worktree; refuse before writing on any conflict.

    A new preparation is prospective: it selects the active operator-configured qualification once,
    retains the artifact and every selected source capture create-once with durable barriers, checks
    concrete executable evidence against the qualified compatibility floors, and only then renders and
    writes the pinned worktree files. No bound native response or local pinned file is required first,
    and no model or provider call is made. Historical v1/v2 records keep their own reader and refresh
    paths, and a root engineer may stay on an older historical exact id.
    """
    from ao_project_room import atomic, digest
    stage = "context"
    try:
        settings = context(service, directory, state)
        stage = "ignore"
        for relative in FILES:
            _target(worktree, relative)  # symlinked or special parents are refused before git is consulted
            _require_ignored(worktree, relative)
        stage = "settings"
        contradictions(settings["claude_config_dir"], worktree, os.environ, foreground=True)
        _foreground_project(service.client(state), state)
        project_env = _project_env(service.client(state), state)
        _alias_env(project_env, "AO project environment")
        _gateway_env(project_env, "AO project environment")
        compaction = compaction_policy(service)
        _compaction_sources(compaction, settings["claude_config_dir"], worktree, os.environ)
        _compaction_project(service.client(state), state, compaction)
        stage = "rules"
        rules = observe_rules(service.client(state), state)
        if not rules["clause_present"]:
            raise RoomError("AO project agentRules do not contain the authorized Project Room delegation clause; install it before preparing the engineer")
        if rules["contradictory_env"]:
            raise RoomError("AO project env overrides native routing knobs: " + ", ".join(rules["contradictory_env"]))
        stage = "qualification"
        qualification = select_worker_qualification(service, directory)
        worker_qualification = worker_qualification_value(qualification)
        worker_map = qualified_worker_map(worker_qualification)
        stage = "executable"
        claude = claude_evidence(settings["claude_bin"])
        check_qualified_floors(worker_qualification, claude)
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
        stage = "render"
        documents = {}
        for name in worker_map:
            text = agent_definition(name, worker_map[name])
            validate_definition(name, text, worker_map, worker_qualification)
            documents[".claude/agents/" + name + ".md"] = text.encode()
        settings_path, _ = _target(worktree, ".claude/settings.local.json")
        existing_bytes = owned_bytes(settings_path) if settings_path.exists() else None
        existing = json.loads(existing_bytes) if existing_bytes is not None else None
        merged = settings_document(existing, command, foreground=True, agents=worker_map,
                                   qualification=worker_qualification)
        # Only fresh preparations receive this default. The unchanged renderer is
        # also used to reproduce immutable historical routing-adoption bundles.
        merged.update(autoCompactEnabled=True, autoCompactWindow=compaction["window"])
        merged["env"][COMPACTION_KEY] = str(compaction["window"])
        documents[".claude/settings.local.json"] = (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode()
        stage = "admission"
        from ao_model_qualification import admit_qualification
        for family in sorted(set(WORKER_FAMILIES.values())):
            admit_qualification(qualification, family, directory=directory, executable=claude,
                                configuration={"config_dir": settings["claude_config_dir"],
                                               "worktree": str(worktree), "agents": worker_map,
                                               "foreground": True, "environ": dict(os.environ),
                                               "local_settings": merged, "project_env": project_env},
                                origin={"context": "pending_routing_snapshot"},
                                worker_roles=WORKER_FAMILIES)
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
            "version": 3, "execution_policy": EXECUTION_POLICY,
            "files": files, "guard_path": str(guard), "guard_sha256": guard_hash, "hook_command": command,
            "python": settings["python"], "agents": dict(worker_map),
            "agent_selection": {name: dict(item) for name, item in AGENT_SELECTION.items()},
            "agent_identity_basis": QUALIFIED_IDENTITY_BASIS,
            "worker_qualification": worker_qualification, "effort": EFFORT,
            "env": {**ENV, COMPACTION_KEY: str(compaction["window"])}, "compaction": compaction, "deny": list(DENY),
            "matcher": MATCHER, "browser_skill": BROWSER_SKILL, "claude_config_dir": settings["claude_config_dir"],
            "claude": claude, "rules": rules,
            "prepare_environment": {key: os.environ[key] for key in sorted(os.environ) if key in RECORDED_ENV},
            "meaning": MEANING}
        return prepared["routing"]
    except (RoomError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        evidence = {"stage": stage, "error": str(exc)[:2000], "worktree": str(worktree), "room_id": state["room_id"], "observed_at": time.time()}
        atomic(directory / ("routing-error-" + digest(evidence)[:16] + ".json"), evidence)
        raise RoomError("Native routing preparation refused at " + stage + ": " + str(exc)) from exc


def validate_local(prepared, state=None, directory=None):
    """Offline re-validation: pinned files, ignore status, guard, interpreter, executable identity, surrounding settings.

    This is caller-level file validation and never calls qualification admission, so the lower
    admission helper and this boundary keep distinct responsibilities and cannot recurse. A version 3
    record additionally re-verifies its retained qualification bytes and the qualified executable
    floors; today's private configuration pointer is never consulted, so a later pointer change alone
    cannot alter the pinned expectation.
    """
    from ao_project_room import digest
    routing = prepared.get("routing") if prepared else None
    if not routing:
        return None
    try:
        if state is not None and directory is not None:
            from ao_routing_refresh import effective as refreshed_routing
            routing = refreshed_routing(directory, state, prepared) or routing
        version = routing.get("version", 1)
        if type(version) is not int or version not in (1, 2, 3):
            raise RoomError("Unsupported native routing version")
        if version >= 2 and (routing.get("execution_policy") != EXECUTION_POLICY or routing.get("matcher") != MATCHER):
            raise RoomError("Native routing no longer pins orchestration ownership for every tool")
        agents = recorded_agents(routing)  # this preparation's (or its committed refresh's) own map
        qualification = routing.get("worker_qualification")
        if qualification is not None and directory is not None and state is not None:
            from ao_model_qualification import verify_reference
            snapshot = qualification["snapshot"]
            verify_reference(directory, {"sha256": snapshot["sha256"], "record": snapshot["record"],
                                         "evidence": snapshot["evidence"]},
                             "The retained worker qualification evidence is missing or changed")
        worktree = Path(prepared["worktree"])
        for relative, expected in routing["files"].items():
            _require_ignored(worktree, relative)
            data = owned_bytes(worktree / relative)
            if digest(data) != expected:
                raise RoomError("Pinned routing file changed: " + relative)
            if relative.endswith(".md"):
                validate_definition(Path(relative).stem, data.decode(), agents, qualification)
            else:
                check_settings(json.loads(data), routing)
        if digest(owned_bytes(routing["guard_path"])) != routing["guard_sha256"]:
            raise RoomError("Private routing guard changed")
        if routing.get("browser_skill") != BROWSER_SKILL:
            raise RoomError("Pinned browser skill differs from the exact observed skill")
        if not os.access(routing["python"], os.X_OK):
            raise RoomError("Routing guard interpreter is unavailable")
        replacement = None
        if state is not None and directory is not None:
            from ao_executable_binding import effective
            replacement = effective(directory, state, prepared)
        evidence = replacement or routing.get("claude") or {}
        check_claude(evidence)
        if qualification is not None:
            check_qualified_floors(qualification, evidence)
        foreground = foreground_configured(worktree, routing)
        contradictions(routing["claude_config_dir"], worktree, os.environ if foreground else None, foreground=foreground,
                       agents=agents, qualification=qualification)
        if "compaction" in routing:
            _compaction_sources(routing["compaction"], routing["claude_config_dir"], worktree, os.environ)
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
    """Local validation for every engineer dispatch; live rules check before implementation/correction.

    Reading an old record stays permitted, but new delegating execution against a record without a
    source-qualified worker selection refuses with explicit-refresh readiness instead of silently
    running historical or moving selectors.
    """
    routing = validate_local(prepared, state, directory)
    if routing is None:
        return routing
    if routing.get("worker_qualification") is None and purpose in DELEGATING_PURPOSES:
        raise RoomError(UNQUALIFIED_ROUTING)
    try:
        if foreground_configured(prepared["worktree"], routing):
            _foreground_project(service.client(state), state)
        selection = routing.get("worker_qualification")
        if routing["agents"] == FAMILY_AGENTS or selection is not None:
            _alias_project(service.client(state), state)
        if selection is not None:
            _gateway_project(service.client(state), state)
        if "compaction" in routing:
            _compaction_project(service.client(state), state, routing["compaction"])
        if purpose not in ("implementation", "correction"):
            return routing
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
        if "compaction" in routing:
            _compaction_project(service.client(state), state, routing["compaction"])
        observed = observe_rules(service.client(state), state)
    except RoomError as exc:
        state["routing_rules"] = {"observed_at": time.time(), "source": "sync", "consistent": False, "error": str(exc)[:1000]}
        service.save(directory, state)
        return
    record_observation(service, directory, state, observed, "sync", rules_match(observed, routing["rules"]))


def status(prepared, state, directory=None):
    from ao_project_room import digest
    result = routing_status(prepared, state, directory)
    configured = result['status'] in ('configured', 'verified')
    try:
        current_guard_sha256 = digest(Path(__file__).with_name('ao_routing_guard.py').read_bytes())
    except OSError:
        current_guard_sha256 = None
    known_guard = (configured and (prepared.get('routing') or {}).get('version') in (2, 3)
                   and current_guard_sha256 is not None and result.get('guard_sha256') == current_guard_sha256)
    result['worker_recovery'] = {
        'native_child_context_resume': False if known_guard else None,
        'native_child_messaging': False if known_guard else None,
        'capability_basis': ('verified_current_guard_for_root_engineer' if known_guard
                             else 'not_configured' if result['status'] == 'not_configured' else 'unverified_guard_capability'),
        'fresh_pinned_worker': ('conditional' if configured
                                else 'blocked' if result['status'] == 'unverified' else 'not_configured'),
        'strategy': ('inspect_preserved_artifacts_then_operator_or_fresh_pinned_worker' if configured
                     else 'inspect_preserved_artifacts_then_operator'),
        'dispatch_authorized': False,
        'meaning': 'Static guard capability for the root engineer managing its children, not a live recovery attestation. '
                   'Null means unverified or not configured, never permission to resume or message. '
                   'A fresh worker does not retain the old child context. Verify complete artifact access and '
                   'inputs first; normal routing, quota, ownership and authorization gates still apply. '
                   'Retained parent-session continuation is a separate audited operation.'}
    return result


def routing_status(prepared, state, directory=None):
    if not prepared or not prepared.get("routing"):
        return {"status": "not_configured", "error": None, "meaning": NOT_CONFIGURED}
    routing = prepared["routing"]
    claude = routing.get("claude") or {}
    try:
        summary = {**worker_summary(routing), "effort": routing["effort"], "env": routing["env"], "deny": routing["deny"],
                   "execution_policy": (routing.get("execution_policy") if routing.get("version") in (2, 3)
                                        else "historical_unrestricted_root"),
                   "browser_skill": routing["browser_skill"], "files": routing["files"], "guard_sha256": routing["guard_sha256"],
                   "claude": claude, "rules_snapshot": routing["rules"], "last_observed_rules": state.get("routing_rules"),
                   "meaning": MEANING}
    except (RoomError, KeyError, TypeError) as exc:
        return {"status": "unverified", "error": "Recorded worker selection is inconsistent: " + str(exc),
                "agents": routing.get("agents") if isinstance(routing, dict) else None, "meaning": MEANING}
    if "compaction" in routing:
        summary["compaction"] = routing["compaction"]
        summary["compaction_meaning"] = "Configured native window; actual compaction, continuity, quality and usage need observation."
    try:
        current = validate_local(prepared, state, directory)
        if directory is not None and state.get("routing_refresh"):
            summary["original_guard_sha256"] = routing["guard_sha256"]
            summary["original_files"] = routing["files"]
            summary["guard_sha256"] = current["guard_sha256"]
            summary["files"] = current["files"]
            summary["routing_refresh"] = state["routing_refresh"]
            if (current.get("agents") != routing.get("agents")
                    or current.get("worker_qualification") != routing.get("worker_qualification")):
                summary["original_agents"] = routing.get("agents")
                if routing.get("worker_qualification") is not None:
                    summary["original_worker_qualification"] = worker_summary(routing)["worker_qualification"]
                summary.update(worker_summary(current))
        if directory is not None and state.get("executable_binding"):
            from ao_executable_binding import effective
            summary["original_claude"] = claude
            claude = effective(directory, state, prepared)
            summary["claude"] = claude
            summary["executable_binding"] = state["executable_binding"]
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


def worker_qualification_reference(routing):
    """The compact immutable reference a frozen worker expectation records for one routing record.

    The full routing metadata is authenticated first, so a reference is never derived from a record
    whose worker map, family intents or effort do not actually hold.
    """
    from ao_project_room import digest
    if not isinstance(routing, dict):
        return None
    recorded_agents(routing)
    value = routing.get("worker_qualification")
    snapshot = value["snapshot"] if isinstance(value, dict) else None
    return {"routing_sha256": digest(routing), "routing_version": routing.get("version"),
            "qualification_sha256": snapshot["sha256"] if snapshot else None,
            "qualification_record": snapshot["record"] if snapshot else None}


def fresh_executable_identity(directory, state, prepared, routing):
    """The predecessor's tested fresh reported-version consistency check, reused at a new boundary.

    ``validate_local`` and ``check_claude`` compare only the recorded size and mtime, so an unchanged
    wrapper that now reports a different version would silently pass a new qualified request on a
    stale value. The audited replacement fingerprint/ownership chain stays authoritative: the same
    effective identity ``validate_local`` used is re-resolved through ``ao_executable_binding`` and
    re-probed, and today's mutable controller configuration is never consulted. This is consistent
    admission at the request boundary, not provider availability or effective-effort attestation.
    """
    from ao_executable_binding import effective
    from ao_routing_refresh import _current_executable
    # The predecessor's exact check reads only the recorded effective identity and one fresh bounded
    # probe; it needs no service handle for that comparison.
    return _current_executable(None, directory, state, routing, effective(directory, state, prepared))


def effective_worker_selection(prepared, state=None, directory=None):
    """Authenticated read-only snapshot of the effective worker selection for a future request freeze.

    Returns the routing digest, the retained qualification digest and exact role map, the family
    intents, the configured effort and the authenticated preparation path paired with its digest.
    The recorded identity comes only from the immutable preparation/refresh ancestry and the
    room-retained qualification bytes, and today's private configuration pointer is never consulted,
    so a later refresh cannot reinterpret what an already-recorded request froze. This call is a
    NEW-dispatch boundary: it re-validates the current worktree files, surrounding settings and the
    effective executable exactly as a dispatch would, and it never rewrites history. This is
    configured intent only.
    """
    from ao_project_room import digest
    routing = validate_local(prepared, state, directory)
    if routing is None:
        return None
    if (state is not None and directory is not None and routing.get("worker_qualification") is not None):
        # A source-qualified worker selection is a new-dispatch boundary exactly like a qualified
        # request freeze: an unchanged wrapper that now reports another version refuses here rather
        # than letting a new qualified request continue on the recorded value.
        fresh_executable_identity(directory, state, prepared, routing)
    agents = recorded_agents(routing)
    value = routing.get("worker_qualification")
    return {"routing_version": routing.get("version"), "routing_sha256": digest(routing),
            "agents": agents, "agent_selection": copy.deepcopy(routing.get("agent_selection")),
            "effort": routing.get("effort"), "source_qualified": value is not None,
            "qualification_sha256": value["snapshot"]["sha256"] if value else None,
            "qualification_record": value["snapshot"]["record"] if value else None,
            "worker_families": ({role: value["families"][role]["family"] for role in sorted(WORKER_FAMILIES)}
                                if value else {}),
            "expected_models": ({role: value["families"][role]["expected_model"] for role in sorted(WORKER_FAMILIES)}
                                if value else {}),
            # The preparation object carries no such key; the authentication is the state's own
            # preparation path paired with the digest validate_local just authenticated it under.
            "preparation": (state or {}).get("preparation"),
            "preparation_sha256": (state or {}).get("preparation_sha256"),
            "routing_refresh": copy.deepcopy((state or {}).get("routing_refresh")),
            "basis": "The routing record's own authenticated selector map and retained qualification snapshot; "
                     "configured intent only, never an attributed served identity or effective effort"}


def historical_routing(directory, state, prepared, reference, authority=None):
    """Re-validate one frozen worker-selection reference against its own historical routing epoch.

    ``authority`` optionally names the exact immutable authority that was frozen: the initial
    preparation (``{"kind": "preparation"}``) or one exact committed routing-refresh pointer
    (``{"kind": "routing_refresh", "path": ..., "sha256": ...}``). When it is supplied, that exact
    record must carry the reference digest: two committed refreshes with equal routing bytes are
    never conflated with the newest matching digest, and a valid old reference is never rejected for
    naming an older ancestor. Existing callers that omit it keep the preparation-first/newest
    matching-digest behavior exactly.

    Epoch zero is the routing record retained inside the immutable preparation; a later epoch is the
    target stored in the named immutable routing-refresh record. The supplied preparation bytes are
    authenticated against the state's own preparation digest, and the whole committed refresh
    ancestry is validated with its own ownership, continuity, cycle and bound checks before any
    record is described, so a read-only historical lookup never silently trusts malformed ancestry.
    Current mutable runtime files and today's private configuration are never read, so a later
    refresh cannot retroactively change a request's own historical expectation. Bounded lookup only;
    old requests are never modified.
    """
    from ao_project_room import digest
    from ao_model_qualification import record_path as qualification_record_path
    if (not isinstance(reference, dict) or set(reference) != WORKER_REFERENCE_FIELDS
            or not _hex_digest(reference.get("routing_sha256"))
            or type(reference.get("routing_version")) is not int
            or reference["routing_version"] not in (1, 2, 3)):
        raise RoomError("A frozen worker selection reference declares its routing digest, version and qualification reference")
    if ((reference.get("qualification_sha256") is None) != (reference.get("qualification_record") is None)
            or (reference.get("qualification_sha256") is not None
                and (not _hex_digest(reference["qualification_sha256"])
                     or reference["qualification_record"]
                     != qualification_record_path(reference["qualification_sha256"])))):
        raise RoomError("A frozen worker selection qualification reference names its digest and its retained record")
    if (not isinstance(state, dict) or not _hex_digest(state.get("preparation_sha256"))
            or not isinstance(prepared, dict) or digest(prepared) != state["preparation_sha256"]):
        raise RoomError("A frozen worker selection is looked up against this room's own retained preparation bytes only")
    import ao_routing_refresh
    try:
        ao_routing_refresh._chain(directory, state, prepared)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RoomError("The frozen worker selection's committed routing ancestry is unreadable or inconsistent") from exc
    exact_kind = None
    if authority is not None:
        if (not isinstance(authority, dict) or authority.get("kind") not in ("preparation", "routing_refresh")
                or (authority["kind"] == "routing_refresh"
                    and (set(authority) != {"kind", "path", "sha256"}
                         or not isinstance(authority.get("path"), str)
                         or not _hex_digest(authority.get("sha256"))))):
            raise RoomError("A frozen worker selection authority is the initial preparation or one exact committed "
                            "routing-refresh pointer")
        exact_kind = authority["kind"]
    routing = (prepared or {}).get("routing")
    found = None
    if (exact_kind != "routing_refresh" and isinstance(routing, dict)
            and digest(routing) == reference["routing_sha256"]):
        found = {"routing": copy.deepcopy(routing), "source": "preparation", "routing_refresh": None}
    elif exact_kind == "preparation":
        raise RoomError("The frozen worker selection names the initial preparation, but its retained routing record "
                        "does not carry the frozen digest")
    if found is None:
        found, pointer, seen = None, state.get("routing_refresh"), set()
        while pointer is not None:
            name = pointer.get("path") if isinstance(pointer, dict) else None
            if name is None or name in seen or len(seen) >= ao_routing_refresh.MAX_CHAIN:
                raise RoomError("The frozen worker selection's committed routing ancestry is cyclic or exceeds its bound")
            seen.add(name)
            record = ao_routing_refresh._read(directory, pointer)
            target = record.get("target")
            if (exact_kind == "routing_refresh"
                    and (pointer["path"] != authority["path"] or pointer["sha256"] != authority["sha256"])):
                pointer = record.get("previous")
                continue
            if isinstance(target, dict) and digest(target) == reference["routing_sha256"]:
                found = {"routing": copy.deepcopy(target), "source": "routing_refresh",
                         "routing_refresh": {"path": pointer["path"], "sha256": pointer["sha256"]}}
                break
            if exact_kind == "routing_refresh":
                raise RoomError("The exact committed routing refresh this frozen worker selection names does not carry "
                                "its recorded digest")
            pointer = record.get("previous")
        if found is None:
            raise RoomError("The frozen worker selection names a routing record this room's immutable preparation and "
                            "refresh ancestry do not contain")
    if found["routing"].get("version") != reference["routing_version"]:
        raise RoomError("The frozen worker selection version differs from its historical routing record")
    value = found["routing"].get("worker_qualification")
    snapshot = value["snapshot"] if isinstance(value, dict) else None
    if ((snapshot or {}).get("sha256") != reference["qualification_sha256"]
            or (snapshot or {}).get("record") != reference["qualification_record"]):
        raise RoomError("The frozen worker selection qualification reference differs from its historical routing record")
    if value is not None:
        from ao_model_qualification import verify_reference
        verify_reference(directory, {"sha256": snapshot["sha256"], "record": snapshot["record"],
                                     "evidence": snapshot["evidence"]},
                         "The frozen worker selection's retained qualification evidence is missing or changed")
    recorded_agents(found["routing"])
    return found


def packet_text(prepared):
    if prepared and prepared.get("routing"):
        routing = prepared["routing"]
        # The complete routing metadata is authenticated before it is described: an unsupported or
        # inconsistent worker map is refused here exactly as validate_local refuses it.
        agents = recorded_agents(routing)
        value = routing.get("worker_qualification")
        if value is not None:
            families = {role: value["families"][role]["family"] for role in sorted(agents)}
            ownership = ("The engineering orchestrator directs this work: inspect evidence with Read/Grep/Glob, direct the "
                         "pinned provider and native workers, adjudicate results and give the final engineering verdict. "
                         "The guard denies root shell commands, edits, tests, browser work and unknown execution tools. "
                         "Routine implementation and checks belong to the assigned operator or delegates, including probes "
                         "used for verification. If a check is assigned to Astra, request its result and wait; do not "
                         "duplicate it or reassign it without an actual task change. Do not reconstruct specifications or "
                         "hashes: the controller checks exact source, native identity and candidate evidence; use the "
                         "supplied identities. Choose Sonnet or Opus when needed for full quality; judgment remains yours. "
                         "If the authorized tools cannot achieve the quality bar, report the capability gap instead of "
                         "bypassing the guard. ")
            return (ownership + "Native delegation routing is configured for this worktree: launch only the pinned native "
                    "agents pr-sonnet (source-qualified exact model " + agents["pr-sonnet"] + " for family "
                    + families["pr-sonnet"] + ", bounded implementation and tests) and pr-opus (source-qualified exact "
                    "model " + agents["pr-opus"] + " for family " + families["pr-opus"] + ", bounded judgment/review and "
                    "the pinned browser skill when the AO browser capability is present); the expected exact models come "
                    "from the operator-selected source qualification retained with this preparation, family intent is "
                    "retained and effort is configured MAX, not observed. One layer, at most two concurrent, no model "
                    "overrides, built-in agent types, forks, isolation, resume, messaging, workflows, teams or review "
                    "skills; the routing guard denies other routes. Record each native route in routing_log with the "
                    "configured qualified selector and any observed native model evidence; never report an expected "
                    "exact id as the observed served model.")
        # Historical exact pins keep their delivered bytes; family selectors are presented as configured intent.
        family = agents == FAMILY_AGENTS
        label = "family alias " if family else ""
        record = (WORKER_IDENTITY + FAMILY_IDENTITY + " Record each native route in routing_log with the configured selector "
                  "and any observed native model evidence; never report an alias as an observed model." if family else
                  "Record each native route in routing_log with the requested model and the observed native evidence.")
        sonnet_work = "bounded implementation" if prepared["routing"].get("version") == 2 else "mechanical implementation"
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
                + label + agents["pr-sonnet"] + ", " + sonnet_work + " and tests) and pr-opus (" + label + agents["pr-opus"]
                + ", bounded judgment/review and the pinned browser skill when the AO browser capability is present); "
                "one layer, at most two concurrent, no model overrides, built-in agent types, forks, isolation, resume, "
                "messaging, workflows, teams or review skills; the routing guard denies other routes. " + record)
    return ("Native delegation routing is not configured for this worktree (prepared before routing protections): do "
            "not launch native subagents, workflows or review skills; use the pinned delegate tools and Fable only, "
            "and record that limitation.")
