"""Preparation and validation of retained delegates for native AO workers."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

from ao_delegate_launcher import owned_bytes
from implementation import POLICIES, verify_provider_inventory, _pinned_delegate_settings
from room import RoomError
import ao_routing


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


def validate_preparation(directory, state, session_id=None, check_routing=True):
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
    if check_routing:
        ao_routing.validate_local(prepared)
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
    # Native routing files are written and snapshotted before the record exists,
    # so a refused routing setup leaves the room unprepared and the spawn aborted.
    ao_routing.prepare(service, directory, state, worktree, prepared)
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


CONTENT_DIGEST = re.compile(r"[0-9a-f]{64}")


def delegation_recorded(directory, state):
    """Evidence that this room's delegate lane was used: an observed ledger, a native MCP launch, or a report naming jobs."""
    if state.get("delegate_ledger_observed"):
        return True
    if directory is not None and (Path(directory) / "delegate-launch.json").exists():
        return True
    return any(r.get("reported_delegate_job_ids") or r.get("delegate_job_ids") for r in state.get("requests", {}).values())


def assert_no_lost_job_rows(home, ledger, room_id):
    """Old pinned adapters can recreate an empty ledger. Retained request metadata must not lose its ledger row.
    Inspect metadata only for orphan directories; normal known jobs need no artifact reads, and other rooms' jobs
    do not block this room. Evidence is never deleted automatically by the adapter."""
    import deepseek_adapter
    jobs = Path(home) / "deepseek" / "jobs"
    if not jobs.exists():
        return
    if jobs.is_symlink():
        raise RoomError("Delegate job evidence directory is unsafe")
    with ledger.reading() as db:
        known = {r[0] for r in db.execute("SELECT id FROM jobs")}
    for path in jobs.iterdir():
        if not deepseek_adapter.JOB_ID.fullmatch(path.name) or path.name in known:
            continue
        try:
            record = json.loads(owned_bytes(path / "request.json", deepseek_adapter.MAX_REQUEST_RECORD_BYTES))
        except (OSError, ValueError) as exc:
            raise RoomError("Orphan delegate job evidence is unreadable; ledger completeness cannot be established") from exc
        if (not isinstance(record, dict) or record.get("job_id") != path.name
                or not isinstance(record.get("room_id"), str) or not record["room_id"]):
            raise RoomError("Orphan delegate job evidence is inconsistent; ledger completeness cannot be established")
        if record["room_id"] == room_id:
            raise RoomError("Delegate ledger lost a recorded job; restore its private evidence before any mutation, never replay it")


def assert_settled(home, state, directory=None):
    """Full-ledger admission check, independent of the latest-20 status display."""
    if state["delegate"]["provider"] != "deepseek":
        return
    import deepseek_adapter
    ledger_path = Path(home) / "deepseek" / "ledger.sqlite3"
    if not ledger_path.exists():
        if delegation_recorded(directory, state):
            raise RoomError("Delegate ledger is missing after recorded delegation; restore the private ledger before any "
                            "mutation, never recreate or replay it")
        return  # No delegate has yet created a ledger.
    try:
        ledger = deepseek_adapter.Ledger(home, initialize=False)
        assert_no_lost_job_rows(home, ledger, state["room_id"])
        if ledger.active(state["room_id"]) or ledger.stopping_jobs(state["room_id"]):
            raise RoomError("Active or unresolved delegate jobs stop this room; observe them, never replay or self-resolve")
    except (OSError, sqlite3.Error, deepseek_adapter.AdapterError) as exc:
        raise RoomError("Delegate ledger is unavailable; completion cannot be established") from exc
    state["delegate_ledger_observed"] = True  # a later missing ledger is loss, never a fresh room


def report_job_ids(report):
    """Ordered unique delegate job IDs an already-parsed engineering report names; pure, no IO."""
    seen, ids = set(), []
    entries = report.get("routing_log", [])
    if not isinstance(entries, list):
        return []  # A known-completed malformed report may receive a report-only correction.
    for entry in entries:
        jobs = entry.get("delegate_job_ids", []) if isinstance(entry, dict) else []
        if not isinstance(jobs, list):
            continue
        for job_id in jobs:
            if isinstance(job_id, str) and job_id not in seen:
                seen.add(job_id)
                ids.append(job_id)
    return ids


def expected_profile(inventory):
    """profile_sha256 that the pinned adapter snapshot and configuration produce, exactly as the adapter derives it."""
    files = (inventory or {}).get("files", {})
    adapter = [d for p, d in files.items() if Path(str(p)).name == "deepseek_adapter.py"]
    config = [d for p, d in files.items() if Path(str(p)).name == "deepseek.json"]
    if len(adapter) != 1 or len(config) != 1 or not all(isinstance(d, str) for d in adapter + config):
        raise RoomError("Pinned provider inventory does not identify one adapter snapshot and one configuration")
    return hashlib.sha256((adapter[0] + config[0]).encode("ascii")).hexdigest()


def verify_content(home, room_id, export_dir, row, maximum):
    """A claimed completed answer must still exist with exactly its recorded digest, read owned and bounded."""
    import deepseek_adapter
    job_id, expected = row["id"], row["content_sha256"]
    if not isinstance(expected, str) or not CONTENT_DIGEST.fullmatch(expected):
        raise RoomError("Completed delegate job " + job_id + " has no recorded content digest")
    name = job_id + ".md"
    if row["content_path"]:
        path = Path(row["content_path"])
        allowed = {home / "deepseek" / "exports" / room_id / name}
        if isinstance(export_dir, str) and export_dir:
            allowed.add(Path(export_dir) / name)
        if path not in allowed:
            raise RoomError("Completed delegate job " + job_id + " records a content path outside this room's export directory")
        candidates = [path]
    else:
        candidates = [home / "deepseek" / "jobs" / job_id / "content", home / "deepseek" / "exports" / room_id / name]
    for path in candidates:
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            data = owned_bytes(path, maximum)
        except (OSError, ValueError) as exc:
            raise RoomError("Completed delegate job " + job_id + " content is unreadable or unsafe") from exc
        if hashlib.sha256(data).hexdigest() != expected:
            raise RoomError("Completed delegate job " + job_id + " content does not match its recorded digest")
        return
    raise RoomError("Completed delegate job " + job_id + " has no verifiable content")


def verify_delegation(home, directory, state, report):
    """Report-named delegate jobs must be this room's own jobs on the pinned provider configuration and terminal:
    a digest-verified completed answer, a user-resolved failure (never a result) or a non-result. Anything else
    refuses. Runs at engineering capture and readiness, never inside the pure report parser."""
    ids = report_job_ids(report)
    if state["delegate"]["provider"] != "deepseek":
        if ids:
            raise RoomError("Engineering report names delegate jobs, but this room has no delegate provider")
        return []
    if not ids:
        return []
    import deepseek_adapter
    home = Path(home)
    if not (home / "deepseek" / "ledger.sqlite3").exists():
        raise RoomError("Delegate ledger is missing after recorded delegation; the reported delegate jobs cannot be verified "
                        "and nothing is recreated or replayed")
    settings = validate_provider(directory, state)["delegate_settings"]
    inventory = state["delegate"]["inventory"]
    profile = expected_profile(inventory)
    evidence = []
    try:
        config_path = next(p for p in inventory["files"] if Path(p).name == "deepseek.json")
        config, config_hash = deepseek_adapter.load_config(config_path, home)
        if config_hash != inventory["files"][config_path]:
            raise RoomError("Pinned delegate configuration changed during evidence verification")
        ledger = deepseek_adapter.Ledger(home, initialize=False)
        for job_id in ids:
            if not deepseek_adapter.JOB_ID.fullmatch(job_id):
                raise RoomError("Engineering report names a malformed delegate job identifier")
            try:
                row = ledger.job(job_id, state["room_id"])
            except deepseek_adapter.AdapterError as exc:
                raise RoomError("Engineering report names an unknown or foreign delegate job " + job_id) from exc
            if row["requested_model"] != settings["model"] or row["profile_sha256"] != profile:
                raise RoomError("Delegate job " + job_id + " was not produced by this room's pinned provider configuration")
            job_state = row["state"]
            if job_state in deepseek_adapter.ACTIVE_STATES:
                raise RoomError("Delegate job " + job_id + " is still active; its result cannot be claimed")
            if job_state in deepseek_adapter.STOP_STATES:
                if not ledger.resolved(job_id):
                    raise RoomError("Delegate job " + job_id + " has unresolved delivery; the user must resolve it before its report is evaluated")
                classification = "resolved_failure"
            elif job_state == deepseek_adapter.COMPLETED:
                verify_content(home, state["room_id"], (inventory or {}).get("export_dir"), row, config["max_content_bytes"])
                classification = "completed"
            elif job_state in deepseek_adapter.TERMINAL_STATES:
                classification = "non_result"
            else:
                raise RoomError("Delegate job " + job_id + " has an unknown state")
            evidence.append({"job_id": job_id, "state": job_state, "classification": classification,
                             "content_sha256": row["content_sha256"] if classification == "completed" else None,
                             "requested_model": row["requested_model"], "profile_sha256": row["profile_sha256"]})
    except (OSError, sqlite3.Error, deepseek_adapter.AdapterError) as exc:
        raise RoomError("Delegate ledger is unavailable; reported delegate jobs cannot be verified") from exc
    return evidence


def status(home, directory, state):
    from project_room import Service
    # Reuse the read-only status projection without constructing/mutating the
    # legacy controller's registry on every AO status read.
    controller = object.__new__(Service)
    controller.home = Path(home)
    jobs = controller._delegate_jobs(state["room_id"], directory)
    if state.get("provider_transition"):
        import ao_provider_transition
        ao_provider_transition.job_attribution(home, directory, state, jobs)
    attachment = "not_prepared"
    native_startup = None
    error = None
    routing = ao_routing.status(None, state)
    if state.get("preparation"):
        session_id = state.get("bindings", {}).get("engineer", {}).get("session_id")
        try:
            prepared = validate_preparation(directory, state, session_id, check_routing=False)
            attachment = "configuration_verified"
            if (directory / "delegate-launch.json").exists():
                attachment = "launch_observed" if state.get("provider_transition") else "native_mcp_launch_observed"
            if state.get("provider_transition") and prepared.get("mcp_attachment"):
                try:
                    from ao_mcp_attachment import validate_attachment
                    startup = validate_attachment(directory, state, prepared)
                    if startup is None:
                        raise ValueError("No verified native attachment receipt")
                    native_startup = {"verified": True, "connection_id": startup["connection"]["connection_id"],
                                      "handshake": startup["handshake"], "meaning": startup["meaning"], "limitation": startup["limitation"]}
                    attachment = "native_mcp_initialized_observed"
                except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
                    native_startup = {"verified": False, "error": str(exc)}
        except (RoomError, OSError, ValueError, TypeError, KeyError) as exc:
            attachment, error = "unverified", str(exc)
            try:
                prepared = preparation(directory, state)
            except (RoomError, OSError, ValueError, TypeError, KeyError):
                prepared = None
        # Offline only: pinned local files and the last observed rules evidence.
        routing = ao_routing.status(prepared, state, directory) if prepared is not None else {
            "status": "unverified", "error": error, "meaning": ao_routing.MEANING}
    return {"provider": state["delegate"]["provider"], "attachment": attachment, "error": error,
            "meaning": "Configuration/launch evidence is not successful inference or native subagent accounting",
            "jobs": jobs, "routing": routing, **({"native_startup": native_startup} if state.get("provider_transition") else {})}


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
