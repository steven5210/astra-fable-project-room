"""Preparation and validation of retained delegates for native AO workers."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from ao_delegate_launcher import owned_bytes
from implementation import POLICIES, verify_provider_inventory, _pinned_delegate_settings
from room import RoomError


def initialize(service, directory, state, provider):
    from ao_project_room import atomic, digest
    if provider not in ("deepseek", "none"):
        raise RoomError("New AO rooms require deepseek or explicit none; legacy Qwen rooms remain unchanged")
    if provider == "deepseek":
        from project_room import Service
        controller = Service(service.root.parent)
        settings = controller.settings()
        settings = {**settings, "delegate_provider": provider}
        controller._profiles(directory, state["project_path"], settings)
        settings = json.loads((directory / "settings.json").read_text())
        inventory = settings["provider_inventory"]
    else:
        inventory = None
        atomic(directory / "settings.json", {"delegate_provider": "none"})
    policy = directory / "delegate-policy.txt"
    policy.write_text(POLICIES[provider])
    paths = [directory / "settings.json", policy]
    if provider == "deepseek":
        paths += [directory / "profiles" / "implementation.json", directory / "profiles" / "implementation-mcp.json"]
    state["delegate"] = {"provider": provider, "inventory": inventory,
                         "files": {str(p): digest(p.read_bytes()) for p in paths}, "policy_path": str(policy)}
    validate_provider(directory, state)


def validate_provider(directory, state):
    from ao_project_room import digest
    try:
        delegate = state["delegate"]
        for path, expected in delegate["files"].items():
            if digest(owned_bytes(path)) != expected:
                raise RoomError("Pinned delegate configuration or policy changed")
        mismatch, verified = verify_provider_inventory(delegate["inventory"], repair_export_dir=False)
        if mismatch:
            raise RoomError("Pinned delegate inventory changed: " + mismatch)
        settings = None
        if delegate["provider"] == "deepseek":
            settings, error = _pinned_delegate_settings(delegate["inventory"], verified)
            if error:
                raise RoomError("Pinned delegate settings unavailable: " + error)
        return {"provider": delegate["provider"], "policy": owned_bytes(delegate["policy_path"]).decode(),
                "delegate_settings": settings}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError("Delegate evidence is unreadable or inconsistent") from exc


def registry_entry(path, project):
    """Inspect only the selected MCP definition; never return other config fields."""
    if not path.exists():
        return None
    try:
        config = json.loads(owned_bytes(path, 16_000_000))
        return config.get("projects", {}).get(project, {}).get("mcpServers", {}).get("deepseek")
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise RoomError("Private Claude MCP registry is unreadable") from exc


def preparation(directory, state):
    from ao_project_room import digest, read
    if not state.get("preparation"):
        raise RoomError("Prepare the native engineer workspace before binding or dispatch")
    try:
        result = read(directory / state["preparation"])
        if (digest(result) != state.get("preparation_sha256") or result["room_id"] != state["room_id"]
                or result["delegate_sha256"] != digest(state["delegate"])):
            raise RoomError("Prepared delegate/workspace receipt changed")
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError("Prepared delegate/workspace receipt is unreadable") from exc


def validate_preparation(directory, state, session_id=None):
    from ao_project_room import digest
    validate_provider(directory, state)
    prepared = preparation(directory, state)
    from ao_project_room import project_path, common_dir
    actual = project_path(prepared["worktree"])
    if str(common_dir(actual)) != state["git_common_dir"]:
        raise RoomError("Prepared workspace Git repository changed")
    if state.get("preparation_status") != "configured":
        raise RoomError("Delegate preparation did not finish; inspect its original evidence")
    if prepared["provider"] == "deepseek":
        if digest(owned_bytes(prepared["launcher_path"])) != prepared["launcher_sha256"]:
            raise RoomError("Private delegate launcher changed")
        if registry_entry(Path(prepared["registry_path"]), prepared["registry_project"]) != prepared["registration"]:
            raise RoomError("Private Claude delegate attachment is missing or changed")
        launch = directory / "delegate-launch.json"
        if launch.exists():
            observed = json.loads(owned_bytes(launch))
            if (observed.get("preparation_sha256") != state["preparation_sha256"]
                    or observed.get("worktree") != prepared["worktree"]
                    or (session_id is not None and observed.get("session_id") != session_id)):
                raise RoomError("Native delegate launch contradicts the prepared engineer")
    return prepared


def prepare(service, directory, state, worktree_path):
    from ao_project_room import atomic, digest, git, common_dir, project_path, read
    worktree = project_path(worktree_path)
    validate_provider(directory, state)
    if str(common_dir(worktree)) != state["git_common_dir"] or directory.is_relative_to(worktree):
        raise RoomError("Prepared workspace must belong to the room repository and exclude private state")
    if state.get("preparation"):
        saved = preparation(directory, state)
        if saved["worktree"] != str(worktree):
            raise RoomError("Prepared engineer workspace is immutable")
        # A CLI acknowledgement may be lost. Only reconcile its exact observed
        # configuration; never repeat an uncertain setup command automatically.
        if state.get("preparation_status") != "configured":
            if saved["provider"] != "deepseek" or registry_entry(Path(saved["registry_path"]), saved["registry_project"]) != saved["registration"]:
                raise RoomError("Prior preparation did not finish; diagnose without replaying it")
            state["preparation_status"] = "configured"
            state["preparation_reconciled"] = True
            service.save(directory, state)
        return validate_preparation(directory, state)
    if state["bindings"].get("engineer"):
        raise RoomError("Prepare attachment before creating/binding the engineer")
    for other in (service.root / "rooms").glob("*/state.json"):
        existing = read(other)
        if existing.get("preparation"):
            prior = preparation(other.parent, existing)
            if prior["worktree"] == str(worktree):
                raise RoomError("Workspace is already prepared for another room")
    provider = state["delegate"]["provider"]
    prepared = {"room_id": state["room_id"], "worktree": str(worktree), "provider": provider,
                "delegate_sha256": digest(state["delegate"])}
    if provider == "deepseek":
        settings = read(directory / "settings.json")
        data = Path(__file__).with_name("ao_delegate_launcher.py").read_bytes()
        launcher_hash = digest(data)
        launcher = service.root / "launchers" / (launcher_hash + ".py")
        launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if launcher.exists():
            if owned_bytes(launcher) != data:
                raise RoomError("Existing private launcher was modified")
        else:
            with launcher.open("xb") as stream:
                stream.write(data)
            launcher.chmod(0o600)
        # Claude stores Git-worktree local-scope entries under the main repo.
        first = git(worktree, "worktree", "list", "--porcelain").splitlines()[0]
        if not first.startswith("worktree "):
            raise RoomError("Cannot resolve Claude's repository-local MCP scope")
        project = str(Path(first[len("worktree "):]).resolve())
        override = settings.get("claude_config_dir_override")
        registry = Path(override).expanduser().resolve() / ".claude.json" if override else Path.home() / ".claude.json"
        registration = {"type": "stdio", "command": sys.executable,
                        "args": [str(launcher), "--home", str(service.root.parent)]}
        present = registry_entry(registry, project)
        if present is not None and present != registration:
            raise RoomError("An unrelated or different deepseek MCP entry already exists; preserve it and resolve the conflict")
        server = read(directory / "profiles" / "implementation-mcp.json")["mcpServers"]["deepseek"]
        server = {**server, "env": {"PROJECT_ROOM_WORKTREE": str(worktree)}}
        prepared.update(launcher_path=str(launcher), launcher_sha256=launcher_hash, registry_path=str(registry),
                        registry_project=project, registration=registration, server=server)
    atomic(directory / "preparation.json", prepared)
    state.update(preparation="preparation.json", preparation_sha256=digest(prepared), preparation_status="pending")
    service.save(directory, state)
    if provider == "deepseek" and present is None:
        env = dict(os.environ)
        if override:
            env["CLAUDE_CONFIG_DIR"] = override
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)
        argv = [settings["claude_bin"], "mcp", "add-json", "deepseek", json.dumps(registration), "--scope", "local"]
        try:
            result = subprocess.run(argv, cwd=worktree, env=env, capture_output=True, timeout=30)
            atomic(directory / "preparation-command.json", {"returncode": result.returncode, "argv": argv})
            if result.returncode or registry_entry(registry, project) != registration:
                raise RoomError("Claude MCP registration failed; inspect preparation evidence without replaying")
        except (OSError, subprocess.TimeoutExpired) as exc:
            atomic(directory / "preparation-command.json", {"error": type(exc).__name__, "argv": argv})
            raise RoomError("Claude MCP registration is uncertain; inspect without replaying") from exc
    state["preparation_status"] = "configured"
    service.save(directory, state)
    return validate_preparation(directory, state)


def assert_settled(home, state):
    """Full-ledger admission check, independent of the latest-20 status display."""
    if state["delegate"]["provider"] != "deepseek":
        return
    import deepseek_adapter
    ledger_path = Path(home) / "deepseek" / "ledger.sqlite3"
    if not ledger_path.exists():
        return  # No delegate has yet created a ledger.
    try:
        ledger = deepseek_adapter.Ledger(home)
        if ledger.active(state["room_id"]) or ledger.stopping_jobs(state["room_id"]):
            raise RoomError("Active or unresolved delegate jobs stop this room; observe them, never replay or self-resolve")
    except (OSError, sqlite3.Error, deepseek_adapter.AdapterError) as exc:
        raise RoomError("Delegate ledger is unavailable; completion cannot be established") from exc


def status(home, directory, state):
    from project_room import Service
    # Reuse the read-only status projection without constructing/mutating the
    # legacy controller's registry on every AO status read.
    controller = object.__new__(Service)
    controller.home = Path(home)
    jobs = controller._delegate_jobs(state["room_id"], directory)
    attachment = "not_prepared"
    error = None
    if state.get("preparation"):
        try:
            validate_preparation(directory, state, state.get("bindings", {}).get("engineer", {}).get("session_id"))
            attachment = "configuration_verified"
            if (directory / "delegate-launch.json").exists():
                attachment = "native_mcp_launch_observed"
        except (RoomError, OSError, ValueError, TypeError, KeyError) as exc:
            attachment, error = "unverified", str(exc)
    return {"provider": state["delegate"]["provider"], "attachment": attachment, "error": error,
            "meaning": "Configuration/launch evidence is not successful inference or native subagent accounting", "jobs": jobs}


def main():
    parser = argparse.ArgumentParser(description="Prepare one native AO engineer workspace before launch")
    parser.add_argument("--home", required=True)
    parser.add_argument("--room", required=True)
    args = parser.parse_args()
    from ao_project_room import Service
    result = Service(Path(args.home)).ao_room_prepare(args.room, str(Path.cwd()))
    print(json.dumps({"room_id": result["room_id"], "worktree": result["worktree"], "prepared": True}))


if __name__ == "__main__":
    try:
        main()
    except (RoomError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
