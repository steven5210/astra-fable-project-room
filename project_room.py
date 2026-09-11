#!/usr/bin/env python3
"""Persistent project/feature rooms and supervised jobs for the local plugin."""

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import progress
import heartbeat
import room

ROOT = Path(__file__).resolve().parent
MODEL = "claude-fable-5-1"
ACTIVE = ("queued", "running")
MAX_TEXT = 2_000_000
PROVIDERS = ("none", "qwen", "deepseek")
STARTUP_GRACE_SECONDS = 10.0  # status never probes a job lease younger than this; a live worker holds its lease from spawn
LEASE_RETRIES = 40  # legacy workers without an inherited lease retry transient contention for about two seconds
DELEGATE_JOB_FIELDS = ("request_id", "lane", "state", "requested_model", "observed_model", "thinking", "reasoning_effort", "max_tokens",
                       "created_at", "submitting_at", "streaming_at", "finished_at", "deadline_at", "usage_source", "finish_reason",
                       "http_status", "error_code", "error_type", "availability", "remote_outcome", "input_bytes", "content_bytes",
                       "reasoning_bytes", "wire_bytes", "content_sha256", "content_path", "export_reason")
LATEST_DELEGATE_JOBS = 20


def text_value(value, name, maximum=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise room.RoomError(f"{name} must be nonempty text, at most {maximum} UTF-8 bytes")
    return value


def positive_revision(value):
    if type(value) is not int or value < 1:
        raise room.RoomError("revision must be a positive integer")
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def discover_claude():
    executable = shutil.which("claude")
    if executable:
        return str(Path(executable).resolve())
    candidates = list((Path.home() / "Library/Application Support/Claude/claude-code").glob("*/claude.app/Contents/MacOS/claude"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise room.RoomError("Claude Code executable not found; run setup --claude-bin /absolute/path/to/claude")


def transcript_path(config_dir, cwd, session_id):
    # Claude Code's POSIX project storage encoding; never search unrelated sessions.
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(Path(cwd).resolve()))
    return str(Path(config_dir) / "projects" / encoded / f"{session_id}.jsonl")


def claude_environment(settings):
    environment = dict(os.environ)
    override = settings.get("claude_config_dir_override")
    if override is None:
        environment.pop("CLAUDE_CONFIG_DIR", None)
    else:
        environment["CLAUDE_CONFIG_DIR"] = override
    return environment


def controller_home(home=None):
    """The controller home a command names (--home, PROJECT_ROOM_HOME or ~/.project-room), resolved but never created."""
    return Path(home or os.environ.get("PROJECT_ROOM_HOME") or Path.home() / ".project-room").expanduser().resolve()


class Service:
    def __init__(self, home=None):
        self.home = controller_home(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._progress_cache = {}  # Parsed, stat-keyed metadata reused by read-only progress observation.
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rooms(
                  id TEXT PRIMARY KEY, project_path TEXT NOT NULL, feature TEXT NOT NULL,
                  path TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project_path,feature));
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), kind TEXT NOT NULL,
                  request_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, pid INTEGER,
                  result TEXT, error TEXT, UNIQUE(room_id,kind,request_key));
                CREATE TABLE IF NOT EXISTS worker_executions(
                  job_id TEXT PRIMARY KEY REFERENCES jobs(id), execution_id TEXT NOT NULL, started_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS issues(
                  id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), job_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, content TEXT NOT NULL, severity TEXT NOT NULL,
                  disposition TEXT NOT NULL, rationale TEXT, resolved_revision INTEGER);
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES rooms(id),
                  kind TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS handoffs(
                  id TEXT NOT NULL, room_id TEXT NOT NULL REFERENCES rooms(id), path TEXT NOT NULL,
                  PRIMARY KEY(room_id,id));
                CREATE TABLE IF NOT EXISTS implementation_recoveries(
                  id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), handoff_id TEXT NOT NULL,
                  predecessor_job_id TEXT NOT NULL, request_key TEXT NOT NULL, payload TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, dispatched_at TEXT, successor_job_id TEXT,
                  invalidated_at TEXT, reason TEXT, record_sha256 TEXT NOT NULL, UNIQUE(room_id,request_key));
                CREATE UNIQUE INDEX IF NOT EXISTS implementation_recoveries_active
                  ON implementation_recoveries(predecessor_job_id) WHERE status IN ('prepared','dispatched');
                CREATE TABLE IF NOT EXISTS implementation_verifications(
                  id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), handoff_id TEXT NOT NULL,
                  predecessor_job_id TEXT NOT NULL, attempt INTEGER NOT NULL, request_key TEXT NOT NULL, payload TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, successor_job_id TEXT, finished_at TEXT, reason TEXT,
                  record_sha256 TEXT NOT NULL, outcome_sha256 TEXT, UNIQUE(room_id,request_key));
                CREATE UNIQUE INDEX IF NOT EXISTS implementation_verifications_active
                  ON implementation_verifications(predecessor_job_id) WHERE status='dispatched';
            """)
        self.process_inspector = None  # Python-only test injection; CLI/MCP callers cannot reach it.

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(str(self.home / "registry.sqlite3"), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        try:
            value = json.loads((self.home / "config.json").read_text())
        except (OSError, ValueError, RecursionError) as exc:
            raise room.RoomError("Project Room is not configured; run project_room.py setup first") from exc
        if not isinstance(value, dict):
            raise room.RoomError("Project Room configuration must be a JSON object; move config.json aside and run project_room.py setup again")
        if value.get("model") != MODEL:
            raise room.RoomError(f"This installation requires the configured Fable model {MODEL}")
        return value

    def setup(self, claude_bin=None, qwen_config=None, deepseek_config=None, delegate_provider=None):
        """Persist the private controller configuration for future rooms.

        Provider selection is compatible with the documented flows: a `delegate_provider` recorded in config.json is
        a deliberate selection and is kept until an explicit --delegate-provider replaces it; with no recorded
        selection the legacy inference applies (qwen when a Qwen configuration exists, else none), so a plain setup
        followed by --qwen-config enables Qwen exactly as before. Selecting DeepSeek is explicit: a newly supplied
        --deepseek-config with no recorded selection is refused before anything is written, with the flag to pass.
        Only a configuration supplied in this run, or a provider explicitly reselected in it, is validated, so a
        stored provider file that is missing or unmounted never blocks unrelated repairs such as --claude-bin."""
        with room.lock_room(self.home / "setup-lock"):
            target = self.home / "config.json"
            try:
                prior = json.loads(target.read_text()) if target.exists() else {}
            except (ValueError, RecursionError) as exc:
                raise room.RoomError("The existing configuration is not valid JSON; move " + str(target) + " aside before running setup") from exc
            if not isinstance(prior, dict):
                raise room.RoomError("The existing configuration is not a JSON object; move " + str(target) + " aside before running setup")
            executable = str(Path(claude_bin).expanduser().resolve()) if claude_bin else prior.get("claude_bin") or discover_claude()
            if not Path(executable).is_file() or not os.access(executable, os.X_OK):
                raise room.RoomError("claude_bin must name an executable file")
            qwen = str(Path(qwen_config).expanduser().resolve()) if qwen_config else prior.get("qwen_config")
            deepseek = str(Path(deepseek_config).expanduser().resolve()) if deepseek_config else prior.get("deepseek_config")
            if delegate_provider is not None and delegate_provider not in PROVIDERS:
                raise room.RoomError("delegate_provider must be deepseek, qwen or none")
            selected = delegate_provider or prior.get("delegate_provider")  # recorded only when chosen deliberately
            if selected is not None and selected not in PROVIDERS:
                raise room.RoomError("Unknown delegate provider recorded in configuration; pass --delegate-provider deepseek, qwen or none")
            provider = selected or ("qwen" if qwen else "none")
            if provider == "qwen" and not qwen:
                raise room.RoomError("delegate_provider qwen requires --qwen-config")
            if provider == "deepseek" and not deepseek:
                raise room.RoomError("delegate_provider deepseek requires --deepseek-config")
            if deepseek_config and selected is None:
                raise room.RoomError("A DeepSeek provider configuration was supplied but no delegate provider is selected, so it would be "
                                     "stored without ever being used; pass --delegate-provider deepseek to select it for new rooms "
                                     "(or --delegate-provider qwen/none to keep it stored but unselected). Nothing was written.")
            if qwen and (qwen_config or delegate_provider == "qwen"):
                from qwen_guard import load_server
                load_server(qwen)  # Validate configuration only; never launch upstream here.
            if deepseek and (deepseek_config or delegate_provider == "deepseek"):
                self._deepseek_config(deepseek)  # key-free validation of the supplied or reselected file; no key, no network
            config = {"version": 1, "claude_bin": executable, "model": MODEL,
                      "claude_config_dir": prior.get("claude_config_dir") or str(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser().resolve()),
                      "claude_config_dir_override": prior.get("claude_config_dir_override", os.environ.get("CLAUDE_CONFIG_DIR")),
                      "qwen_config": qwen, "deepseek_config": deepseek,
                      "review_timeout_seconds": prior.get("review_timeout_seconds", 1800),
                      "implementation_timeout_seconds": prior.get("implementation_timeout_seconds", 3600)}
            if selected is not None:
                config["delegate_provider"] = selected
            atomic_json(target, config)
            return {"configured": True, "config_path": str(target), "model": MODEL, "delegate_provider": provider,
                    "delegate_provider_selected": selected is not None, "qwen_configured": bool(qwen), "deepseek_configured": bool(deepseek),
                    "existing_rooms_unchanged": True}

    def _deepseek_config(self, path):
        import deepseek_adapter
        try:
            return deepseek_adapter.load_config(path, self.home)[0]
        except deepseek_adapter.AdapterError as exc:
            raise room.RoomError("DeepSeek provider configuration rejected (" + exc.code + "): " + str(exc)) from exc

    @staticmethod
    def _provider(settings):
        provider = provider_name(settings)
        if provider not in PROVIDERS:
            raise room.RoomError("Unknown delegate provider in configuration")
        if provider == "qwen" and not settings.get("qwen_config"):
            raise room.RoomError("delegate_provider qwen requires a configured qwen_config")
        if provider == "deepseek" and not settings.get("deepseek_config"):
            raise room.RoomError("delegate_provider deepseek requires a configured deepseek_config")
        return provider

    def room_doctor(self):
        try:
            config = self.settings()
        except room.RoomError as exc:
            return {"configured": False, "error": str(exc)}
        result = {"configured": True, "model": config["model"], "home": str(self.home),
                  "claude_executable_exists": Path(config["claude_bin"]).is_file(),
                  "delegate_provider": provider_name(config),
                  "delegate_provider_selected": "delegate_provider" in config,
                  "qwen_configured": bool(config.get("qwen_config")),
                  "qwen_inference_verified": False,
                  "deepseek_configured": bool(config.get("deepseek_config")),
                  "deepseek_inference_verified": False}
        try:
            self._provider(config)  # the same consistency rules setup and room_open apply; doctor names the problem instead of hiding it
        except room.RoomError as exc:
            result["delegate_provider_error"] = str(exc)
        if config.get("deepseek_config"):
            try:
                import deepseek_adapter
                provider = self._deepseek_config(config["deepseek_config"])
                result["deepseek"] = {"model": provider["model"], "deep_lane": deepseek_adapter.lane_parameters(provider, "deep"),
                                      "key_file": deepseek_adapter.key_diagnostics(provider["api_key_file"]),
                                      "latest_probe": deepseek_adapter.latest_probe(self.home / "deepseek" / "probes"),
                                      "meaning": "metadata only: the key is never read here and no probe or model call is made"}
            except room.RoomError as exc:
                result["deepseek"] = {"error": str(exc)}
            except ImportError as exc:
                result["deepseek"] = {"error": "deepseek_adapter cannot be imported: " + str(exc)}
        try:
            room.validate_subscription_environment()
            process = subprocess.run([config["claude_bin"], "auth", "status"], capture_output=True, timeout=15,
                                     env=claude_environment(config))
            auth = json.loads(process.stdout)
            result["claude_auth"] = {key: auth[key] for key in ("loggedIn", "authMethod", "subscriptionType") if key in auth}
            result["auth_status_exit_code"] = process.returncode
        except (room.RoomError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result["auth_error"] = str(exc)
        return result

    def entry(self, room_id):
        text_value(room_id, "room_id", 200)
        with self.db() as db:
            row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if row is None:
            raise room.RoomError("Unknown room_id; use room_list or room_open")
        return dict(row)

    def paths(self, room_id):
        entry = self.entry(room_id)
        root = Path(entry["path"])
        return entry, root, root / "review"

    @staticmethod
    def _room_settings(root):
        return json.loads((root / "settings.json").read_text())

    def _profiles(self, root, project_path, settings):
        # These files are private snapshots: package updates cannot silently change a room.
        profiles = root / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        review = {"claude_bin": settings["claude_bin"], "model": MODEL, "expected_model_ids": [MODEL],
                  "timeout_seconds": settings["review_timeout_seconds"],
                  "extra_args": ["--effort", "max", "--permission-mode", "dontAsk", "--permission-prompts", "none",
                                 "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                                 "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                                 "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                                 "--add-dir", project_path]}
        atomic_json(profiles / "review.json", review)
        provider = self._provider(settings)
        mcp = {"mcpServers": {}}
        add_dirs, inventory = [], None
        if provider == "qwen":
            # The upstream config remains private; never embed its env/credential values.
            shutil.copyfile(ROOT / "qwen_guard.py", profiles / "qwen_guard.py")
            mcp["mcpServers"]["qwen-local"] = {"command": sys.executable, "args": [str(profiles / "qwen_guard.py"), "--config", settings["qwen_config"]]}
        elif provider == "deepseek":
            import deepseek_adapter
            adapter_copy = profiles / "deepseek_adapter.py"
            shutil.copyfile(ROOT / "deepseek_adapter.py", adapter_copy)
            snapshot = profiles / "deepseek.json"
            atomic_json(snapshot, self._deepseek_config(settings["deepseek_config"]))  # normalized, key-free
            export_dir = self.home / "deepseek" / "exports" / root.name
            try:
                deepseek_adapter.ensure_private_directory(export_dir)
            except deepseek_adapter.AdapterError as exc:
                raise room.RoomError("Cannot create the room export directory: " + str(exc)) from exc
            mcp["mcpServers"]["deepseek"] = {"command": sys.executable, "args": [str(adapter_copy), "serve", "--home", str(self.home), "--room", root.name,
                                                                                  "--room-root", str(root), "--config", str(snapshot)]}
            add_dirs = ["--add-dir", str(export_dir)]  # exactly this room's content-only export directory, nothing else
            inventory = {"provider": "deepseek", "room_id": root.name, "export_dir": str(export_dir), "recorded_at": room.now(),
                         "files": {str(adapter_copy): room.sha(adapter_copy.read_bytes()), str(snapshot): room.sha(snapshot.read_bytes())}}
        atomic_json(profiles / "implementation-mcp.json", mcp)
        agents = {
            "sonnet-worker": {"description": "Mechanical code application, file operations, and verification delegated by Fable.",
                               "prompt": "Perform only Fable's self-contained assignment. Verify anchors, types, and interfaces. Return changes, gates, and evidence. Do not self-certify or broaden scope.",
                               "model": "sonnet", "tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]},
            "opus-reviewer": {"description": "Bounded module-level judgment, debugging, or deep review delegated by Fable.",
                              "prompt": "Perform only Fable's bounded assignment. Return evidence, findings, fixes needed, and uncertainty. Fable owns the final engineering verdict.",
                              "model": "opus", "tools": ["Read", "Glob", "Grep", "Bash"]}}
        implementation = {"claude_bin": settings["claude_bin"], "model": MODEL, "expected_model_ids": [MODEL],
                          "timeout_seconds": settings["implementation_timeout_seconds"],
                          "claude_config_dir": settings["claude_config_dir"],
                          "claude_config_dir_override": settings.get("claude_config_dir_override"),
                          "extra_args": ["--effort", "max", "--permission-mode", "auto", "--permission-prompts", "none",
                                         "--tools", "Read,Glob,Grep,Edit,Write,Bash,Agent,Skill", "--agents", room.canonical(agents),
                                         "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                                         "--strict-mcp-config", "--mcp-config", str(profiles / "implementation-mcp.json"), *add_dirs]}
        atomic_json(profiles / "implementation.json", implementation)
        pinned = {**settings, "delegate_provider": provider}
        if inventory is not None:
            pinned["provider_inventory"] = inventory
        atomic_json(root / "settings.json", pinned)

    def room_open(self, project_path, feature):
        project = Path(text_value(project_path, "project_path", 4096)).expanduser().resolve()
        if not project.is_dir():
            raise room.RoomError("project_path must be an existing directory")
        feature = text_value(feature, "feature", 200).strip()
        with room.lock_room(self.home / "registry-lock"):
            with self.db() as db:
                prior = db.execute("SELECT * FROM rooms WHERE project_path=? AND feature=?", (str(project), feature)).fetchone()
            if prior:
                return {**dict(prior), "existing": True}
            settings = self.settings()
            identifier = (re.sub(r"[^a-z0-9]+", "-", feature.lower()).strip("-")[:45] or "feature") + "-" + room.sha(room.canonical([str(project), feature]).encode())[:12]
            root = self.home / "rooms" / identifier
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (root / "review/room.sqlite3").exists():
                raise room.RoomError("Unregistered existing room state requires inspection; it will not be replaced")
            self._profiles(root, str(project), settings)
            room.initialize(SimpleNamespace(config=str(root / "profiles/review.json")), root / "review")
            entry = {"id": identifier, "project_path": str(project), "feature": feature,
                     "path": str(root), "created_at": room.now()}
            atomic_json(root / "project.json", entry)
            with self.db() as db:
                db.execute("INSERT INTO rooms VALUES(:id,:project_path,:feature,:path,:created_at)", entry)
            return {**entry, "existing": False}

    def room_list(self, project_path=None):
        with self.db() as db:
            if project_path:
                rows = db.execute("SELECT * FROM rooms WHERE project_path=? ORDER BY created_at", (str(Path(project_path).expanduser().resolve()),)).fetchall()
            else:
                rows = db.execute("SELECT * FROM rooms ORDER BY created_at").fetchall()
        return {"rooms": [dict(row) for row in rows]}

    def _event(self, room_id, kind, value):
        with self.db() as db:
            db.execute("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,?,?,?)", (room_id, kind, room.canonical(value), room.now()))

    def _superseded(self, room_id, recovery_id=None, verification_for=None):
        """Predecessor jobs exempt from blocking: only verified lineage edges to a registered successor,
        plus the predecessor of a prepared recovery for the submit that carries that exact recovery_id.

        Verification edges exempt their predecessor (and that attempt's earlier interrupted verifier jobs) only while
        the registered row is dispatched with an active successor, or consumed with its immutable record and outcome
        still hashing to the registry's digests; `verification_for` names the predecessor whose retry is being dispatched."""
        exempt = {}
        with self.db() as db:
            for row in db.execute("SELECT id,status,predecessor_job_id,successor_job_id FROM implementation_recoveries WHERE room_id=?", (room_id,)):
                if row["status"] in ("dispatched", "consumed") and row["successor_job_id"]:
                    successor = db.execute("SELECT status FROM jobs WHERE id=? AND room_id=?", (row["successor_job_id"], room_id)).fetchone()
                    # A dispatched edge counts only while its successor is still active; a successor that ended
                    # without a classifiable outcome never launched provably, so its predecessor blocks again.
                    if successor and (row["status"] == "consumed" or successor["status"] in ACTIVE):
                        exempt[row["predecessor_job_id"]] = {"recovery_id": row["id"], "successor_job_id": row["successor_job_id"], "status": row["status"]}
                elif row["status"] == "prepared" and recovery_id is not None and row["id"] == recovery_id:
                    exempt[row["predecessor_job_id"]] = {"recovery_id": row["id"], "successor_job_id": None, "status": row["status"]}
        exempt.update(self._verification_edges(room_id, verification_for))
        return exempt

    def _verification_edges(self, room_id, verification_for=None):
        import verification
        exempt, rows = {}, self._verifications(room_id)
        interrupted = {}
        for row in rows:
            # Earlier verifier jobs of the same predecessor and attempt whose workers ended interrupted or invalidated (for
            # example a vanished worker whose unregistered outcome was refused) are lineage only through their re-verified record.
            if row["status"] in ("interrupted", "invalidated") and row["successor_job_id"]:
                interrupted.setdefault((row["predecessor_job_id"], row["attempt"]), []).append(row)
        def lineage(row):
            edge = {"verification_id": row["id"], "successor_job_id": row["successor_job_id"], "status": row["status"]}
            exempt[row["predecessor_job_id"]] = edge
            if row["status"] == "consumed" and row.get("reason") == "reconciled":
                # The verifier's registered, re-verified outcome explains its own vanished worker; the uncertain successor row stays as it is.
                exempt[row["successor_job_id"]] = {**edge, "reconciled": True}
            for earlier in interrupted.get((row["predecessor_job_id"], row["attempt"]), []):
                if validated(earlier) and not active_job(earlier["successor_job_id"]):
                    exempt[earlier["successor_job_id"]] = {**edge, "earlier_verification_id": earlier["id"]}
        def active_job(job_id):
            with self.db() as db:
                found = db.execute("SELECT status FROM jobs WHERE id=? AND room_id=?", (job_id, room_id)).fetchone()
            return bool(found) and found["status"] in ACTIVE

        def validated(row):
            try:
                successor = self._job(row["successor_job_id"]) if row["successor_job_id"] else None
            except room.RoomError:
                return False
            payload = successor["payload"] if successor and isinstance(successor.get("payload"), dict) else {}
            if (not successor or successor["kind"] != "verification" or successor["room_id"] != room_id or payload.get("verification_id") != row["id"]
                    or payload.get("job_id") != row["predecessor_job_id"] or payload.get("handoff_id") != row["handoff_id"]):
                return False
            if row["status"] == "consumed":
                frozen = successor["result"].get("verification") if isinstance(successor.get("result"), dict) and isinstance(successor["result"].get("verification"), dict) else {}
                if not row["outcome_sha256"]:
                    return False
                if row.get("reason") == "reconciled":
                    if successor["status"] in ACTIVE:
                        return False  # a reconciled edge exists only because its worker vanished; a live worker contradicts it
                elif successor["status"] != "succeeded" or frozen.get("result") != "completed" or frozen.get("outcome_sha256") != row["outcome_sha256"]:
                    return False
            try:
                return verification.validate_edge(self._handoff_path(room_id, row["handoff_id"]), row)[0]
            except room.RoomError:
                return False
        for row in rows:
            if row["status"] == "consumed" and row["successor_job_id"] and validated(row):
                with self.db() as db:
                    successor = db.execute("SELECT status FROM jobs WHERE id=? AND room_id=?", (row["successor_job_id"], room_id)).fetchone()
                if successor and (successor["status"] == "succeeded" or (row.get("reason") == "reconciled" and successor["status"] not in ACTIVE)):
                    lineage(row)
            elif row["status"] == "dispatched" and row["successor_job_id"] and validated(row):
                with self.db() as db:
                    successor = db.execute("SELECT status FROM jobs WHERE id=? AND room_id=?", (row["successor_job_id"], room_id)).fetchone()
                if successor and successor["status"] in ACTIVE:
                    lineage(row)
        if verification_for is not None:
            job = self._job(verification_for)
            exempt[verification_for] = {"verification_id": None, "successor_job_id": None, "status": "dispatching"}
            attempt = (job.get("result") or {}).get("attempt_count") if isinstance(job.get("result"), dict) else None
            for earlier in interrupted.get((verification_for, attempt), []):
                if validated(earlier) and not active_job(earlier["successor_job_id"]):
                    exempt[earlier["successor_job_id"]] = {"verification_id": earlier["id"], "successor_job_id": earlier["successor_job_id"], "status": "interrupted"}
        return exempt

    def _blocking_jobs(self, room_id, recovery_id=None, verification_for=None):
        with self.db() as db:
            identifiers = [row[0] for row in db.execute("SELECT id FROM jobs WHERE room_id=? AND status IN ('queued','running')", (room_id,))]
        for identifier in identifiers:
            self._refresh(identifier)
        exempt = self._superseded(room_id, recovery_id, verification_for)
        with self.db() as db:
            rows = db.execute("SELECT id,status FROM jobs WHERE room_id=? AND status IN ('queued','running','uncertain') ORDER BY created_at", (room_id,)).fetchall()
        return [dict(row) for row in rows if row["id"] not in exempt]

    def _guard_idle(self, room_id, recovery_id=None, verification_for=None):
        blocking = self._blocking_jobs(room_id, recovery_id, verification_for)
        if blocking:
            active = blocking[0]
            raise room.RoomError(f"Room is blocked by {active['status']} job {active['id']}; inspect its status, do not resubmit")

    def _recoveries(self, room_id):
        with self.db() as db:
            rows = db.execute("SELECT id,handoff_id,predecessor_job_id,successor_job_id,status,created_at,dispatched_at,invalidated_at,reason,record_sha256 "
                              "FROM implementation_recoveries WHERE room_id=? ORDER BY created_at", (room_id,)).fetchall()
        return [dict(row) for row in rows]

    def _verifications(self, room_id):
        with self.db() as db:
            rows = db.execute("SELECT id,room_id,handoff_id,predecessor_job_id,attempt,successor_job_id,status,created_at,finished_at,reason,record_sha256,outcome_sha256 "
                              "FROM implementation_verifications WHERE room_id=? ORDER BY created_at", (room_id,)).fetchall()
        return [dict(row) for row in rows]

    def room_status(self, room_id):
        import implementation
        import recovery
        entry, root, review = self.paths(room_id)
        with self.db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE room_id=? ORDER BY created_at", (room_id,))]
        jobs = [self._refresh(identifier) for identifier in ids]
        exempt = self._superseded(room_id)
        recoveries = self._recoveries(room_id)
        verifications = self._verifications(room_id)
        for job in jobs:
            edge = exempt.get(job["id"])
            job["superseded_by"] = edge["successor_job_id"] if edge else None
            job["recovery_id"] = next((row["id"] for row in recoveries if job["id"] in (row["predecessor_job_id"], row["successor_job_id"])), None)
            job["verification_id"] = next((row["id"] for row in verifications if job["id"] in (row["predecessor_job_id"], row["successor_job_id"])), None)
        with self.db() as db:
            issues = [dict(r) for r in db.execute("SELECT * FROM issues WHERE room_id=? ORDER BY rowid", (room_id,))]
            handoffs = [dict(r) for r in db.execute("SELECT * FROM handoffs WHERE room_id=?", (room_id,))]
        for handoff in handoffs:
            try:
                _, _, state = implementation._load(handoff["path"])
                import verification
                handoff["lineage"] = {**recovery.lineage(state), **verification.lineage(state)}
            except (implementation.ImplementationError, OSError, ValueError, KeyError, TypeError, AttributeError):
                handoff["lineage"] = {"error": "handoff integrity check failed"}
        core = room.status_report(review)
        blocking = [job for job in jobs if job["status"] in (*ACTIVE, "uncertain") and job["id"] not in exempt]
        now = progress.clock()
        jobs = [self._with_progress(job, now, (entry, root, review)) for job in jobs]
        return {"room": entry, "review": core, "issues": issues, "jobs": jobs, "handoffs": handoffs,
                "recoveries": recoveries, "verifications": verifications, "enhancements": self._enhancements(room_id), "delegate_jobs": self._delegate_jobs(room_id, root),
                "ready_for_handoff": core["agreement"] and not any(i["disposition"] == "open" for i in issues) and not blocking}

    def _delegate_jobs(self, room_id, root):
        """Bounded read-only summary of this room's provider jobs from the private ledger: allowlisted facts only, no network,
        no lease probe and no relabelling; token counts are provider usage from the ledger, never model assertions."""
        value = {"provider": None, "items": [], "truncated": False, "unavailable_reason": None,
                 "meaning": "latest ledger facts for this room's delegate jobs; usage is provider-reported or unknown"}
        try:
            settings = self._room_settings(root)
            if not isinstance(settings, dict):
                raise ValueError("settings.json is not a JSON object")
            value["provider"] = provider_name(settings)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            value["unavailable_reason"] = "settings_unreadable"  # a damaged settings file never breaks the read-only status surface
            return value
        if value["provider"] != "deepseek":
            value["unavailable_reason"] = "provider_not_deepseek"
            return value
        ledger = self.home / "deepseek" / "ledger.sqlite3"
        if not ledger.is_file():
            value["unavailable_reason"] = "ledger_missing"
            return value
        try:
            db = sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                rows = db.execute("SELECT * FROM jobs WHERE room_id=? ORDER BY created_at DESC, id DESC LIMIT ?", (room_id, LATEST_DELEGATE_JOBS + 1)).fetchall()
                resolved = {row[0] for row in db.execute("SELECT job_id FROM resolutions WHERE room_id=?", (room_id,))}
            finally:
                db.close()
        except sqlite3.Error:
            value["unavailable_reason"] = "ledger_unreadable"
            return value
        try:
            import deepseek_adapter
        except ImportError:
            value["unavailable_reason"] = "adapter_unavailable"
            return value
        required = set(DELEGATE_JOB_FIELDS) | {"id", "possibly_billed", "usage_json"}
        if rows and not required <= set(rows[0].keys()):
            value["unavailable_reason"] = "ledger_schema_mismatch"  # an older or newer ledger never breaks room_status
            return value
        for row in rows[:LATEST_DELEGATE_JOBS]:
            item = {"job_id": row["id"], **{field: row[field] for field in DELEGATE_JOB_FIELDS}}
            item["possibly_billed"] = bool(row["possibly_billed"])
            try:
                item["usage"] = json.loads(row["usage_json"]) if row["usage_json"] else None
            except ValueError:
                item["usage"] = None
            item["resolved"] = row["id"] in resolved
            item["stops_room_lane"] = row["state"] in deepseek_adapter.STOP_STATES and row["id"] not in resolved
            value["items"].append(item)
        value["truncated"] = len(rows) > LATEST_DELEGATE_JOBS
        return value

    def room_spec_put(self, room_id, revision, content):
        positive_revision(revision)
        text_value(content, "content")
        _, root, review = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            path = root / "inputs" / f"spec-{revision}-{room.sha(content.encode())}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
            return room.save_spec(SimpleNamespace(revision=revision, file=str(path)), review)

    def room_record(self, room_id, sender, kind, revision, content):
        positive_revision(revision)
        text_value(content, "content")
        if sender not in ("astra", "user") or kind not in ("message", "approval"):
            raise room.RoomError("sender must be astra/user and kind message/approval")
        _, root, review = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            path = root / "inputs" / (uuid.uuid4().hex + ".md")
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content.encode())
            return room.record(SimpleNamespace(sender=sender, kind=kind, revision=revision, file=str(path)), review)

    def room_issue_dispose(self, room_id, issue_id, disposition, rationale, revision):
        positive_revision(revision)
        text_value(rationale, "rationale")
        if disposition not in ("addressed", "rejected", "deferred"):
            raise room.RoomError("disposition must be addressed, rejected, or deferred")
        _, root, review = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            with contextlib.closing(room.connect(review)) as db:
                room.get_spec(db, review, revision, current=True)
            with self.db() as db:
                issue = db.execute("SELECT * FROM issues WHERE id=? AND room_id=?", (issue_id, room_id)).fetchone()
                if not issue:
                    raise room.RoomError("Unknown issue")
                source_job = db.execute("SELECT kind FROM jobs WHERE id=?", (issue["job_id"],)).fetchone()
                if source_job and source_job["kind"] == "implementation" and revision <= issue["revision"]:
                    raise room.RoomError("An implementation scope discovery requires a newer spec revision before disposition")
                if disposition == "deferred" and issue["severity"] == "blocker":
                    raise room.RoomError("A blocking requirement cannot be deferred; resolve it in the spec or explain rejection")
                db.execute("UPDATE issues SET disposition=?,rationale=?,resolved_revision=? WHERE id=?", (disposition, rationale, revision, issue_id))
            self._event(room_id, "issue_disposition", {"issue_id": issue_id, "disposition": disposition, "rationale": rationale, "revision": revision})
            note = root / "inputs" / (uuid.uuid4().hex + ".md")
            note.parent.mkdir(exist_ok=True)
            note.write_text(room.canonical({"issue_id": issue_id, "finding": issue["content"], "disposition": disposition,
                                            "rationale": rationale, "revision": revision}), encoding="utf-8")
            room.record(SimpleNamespace(sender="astra", kind="message", revision=revision, file=str(note)), review)
            if disposition == "deferred":
                self._event(room_id, "backlog", {"content": issue["content"], "rationale": rationale})
            return {"issue_id": issue_id, "disposition": disposition, "revision": revision}

    def room_backlog_add(self, room_id, content, rationale, issue_url=None, proposal_id=None,
                         user_decision=None, decision_rationale=None):
        self.entry(room_id)
        text_value(content, "content")
        text_value(rationale, "rationale")
        if issue_url is not None:
            text_value(issue_url, "issue_url", 2048)
            match = re.fullmatch(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/([A-Za-z0-9_.-]+)/issues/[1-9][0-9]*", issue_url)
            if not match or match.group(1) in (".", ".."):
                raise room.RoomError("issue_url must be an HTTPS github.com/owner/repo/issues/positive-integer URL")
        if proposal_id is not None and (not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id)):
            raise room.RoomError("proposal_id must be the stable ID returned for an existing proposal")
        if user_decision is not None and user_decision not in ("pending", "approved", "declined", "deferred"):
            raise room.RoomError("user_decision must be pending, approved, declined, or deferred")
        if decision_rationale is not None:
            text_value(decision_rationale, "decision_rationale")
            if user_decision is None:
                raise room.RoomError("decision_rationale requires an explicit user_decision")
        if user_decision in ("approved", "declined", "deferred") and decision_rationale is None:
            raise room.RoomError("A nonpending user_decision requires actual user decision evidence in decision_rationale")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")  # Serialize read/update so linked events cannot lose an intervening decision.
            previous, previous_id = None, None
            if proposal_id is not None:
                for event in db.execute("SELECT id,content FROM events WHERE room_id=? AND kind='backlog' ORDER BY id DESC", (room_id,)):
                    value = json.loads(event["content"])
                    if isinstance(value, dict) and value.get("proposal_id") == proposal_id:
                        previous, previous_id = value, event["id"]
                        break
                if previous is None:
                    raise room.RoomError("Unknown enhancement proposal for this room")
            else:
                proposal_id = uuid.uuid4().hex
            unchanged = previous is not None and previous["content"] == content and previous["rationale"] == rationale
            decision = user_decision if user_decision is not None else previous["user_decision"] if unchanged else "pending"
            evidence = decision_rationale if user_decision is not None else previous.get("decision_rationale") if unchanged else None
            value = {"proposal_id": proposal_id, "previous_event_id": previous_id, "content": content, "rationale": rationale,
                     "issue_url": issue_url if issue_url is not None else previous.get("issue_url") if previous else None,
                     "user_decision": decision, "decision_rationale": evidence}
            stamp = room.now()
            event_id = db.execute("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,'backlog',?,?)",
                                  (room_id, room.canonical(value), stamp)).lastrowid
        return {"recorded": True, **value, "event_id": event_id, "recorded_at": stamp,
                "needs_issue": value["issue_url"] is None, "needs_user_decision": decision == "pending"}

    def _enhancements(self, room_id):
        latest = {}
        with self.db() as db:
            for event in db.execute("SELECT id,content,created_at FROM events WHERE room_id=? AND kind='backlog' ORDER BY id", (room_id,)):
                value = json.loads(event["content"])
                if not isinstance(value, dict) or not value.get("proposal_id") or "user_decision" not in value:
                    continue  # Legacy technical backlog entries never imply a user decision.
                latest[value["proposal_id"]] = {**value, "event_id": event["id"], "recorded_at": event["created_at"],
                                                 "needs_issue": value.get("issue_url") is None,
                                                 "needs_user_decision": value["user_decision"] == "pending"}
        return sorted(latest.values(), key=lambda value: value["event_id"])

    def room_decision_record(self, room_id, revision, decision):
        positive_revision(revision)
        text_value(decision, "decision")
        _, root, review = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            note = root / "inputs" / ("user-decision-" + uuid.uuid4().hex + ".md")
            note.parent.mkdir(exist_ok=True)
            note.write_text(decision, encoding="utf-8")
            result = room.record_user_decision(SimpleNamespace(revision=revision, decision_file=str(note)), review)
            self._event(room_id, "user_decision", {"revision": revision, "decision": decision, "checkpoint": result})
            return result

    def room_history(self, room_id):
        entry, root, review = self.paths(room_id)
        with self.db() as db:
            events = [dict(r) for r in db.execute("SELECT * FROM events WHERE room_id=? ORDER BY id", (room_id,))]
        # Export only public-facing model text and structured outcomes, never thinking.
        history = room.transcript(SimpleNamespace(file=None), review)
        destination = root / "history.md"
        destination.write_text(history, encoding="utf-8")
        return {"room": entry, "review_history": history[-60000:], "truncated": len(history) > 60000,
                "full_review_history_path": str(destination), "events": events[-200:],
                "enhancements": self._enhancements(room_id)}

    def _job_path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise room.RoomError("Invalid job_id")
        return self.home / "jobs" / job_id

    def _job(self, job_id):
        self._job_path(job_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise room.RoomError("Unknown job_id")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def _with_progress(self, job, now, paths=None):
        """Attach the additive read-only progress view. Observation never changes a job."""
        try:
            context = {}
            if job["status"] == "running":
                _, root, review = paths or self.paths(job["room_id"])
                if len(self._progress_cache) > 64:
                    self._progress_cache.clear()
                context["cache"] = self._progress_cache
                if job["kind"] == "review":
                    config_dir = self._room_settings(root).get("claude_config_dir")
                    context["review"] = progress.review_context(review, job["request_key"], job["payload"].get("session_transcript"), config_dir)
                elif job["kind"] in ("implementation", "verification"):
                    context["handoff"] = progress.handoff_context(job["payload"].get("handoff_path"), self._progress_cache)
            value = progress.job_progress(job, now, **context)
            execution = None
            if job['status'] == 'running':
                try:
                    with self.db() as db:
                        row = db.execute('SELECT execution_id,started_at FROM worker_executions WHERE job_id=?', (job['id'],)).fetchone()
                        execution = dict(row) if row else None
                except sqlite3.Error:
                    execution = 'unreadable'
            value['heartbeat'] = (heartbeat.unavailable('registry_unreadable') if execution == 'unreadable' else
                                  heartbeat.observe(self.home / 'jobs', job, execution, now, value.get('attempt')))
        except Exception as exc:  # Status must stay readable; a failed observation is reported, never raised.
            print(json.dumps({"progress_error": type(exc).__name__, "job_id": job.get("id")}), file=sys.stderr)
            value = progress.unavailable(job, now)
        return {**job, "progress": value}

    def _refresh(self, job_id):
        value = self._job(job_id)
        if value["status"] in ACTIVE:
            # The startup grace is checked before the lease is touched: status never competes with a starting worker
            # for its lease. New workers inherit the lease held since before spawn, so a free lease after the grace
            # proves that no owner survives.
            age = time.time() - room.parse_timestamp(value["created_at"]).timestamp()
            if age <= STARTUP_GRACE_SECONDS:
                return value
            with (self._job_path(job_id) / "worker.lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return value
                with self.db() as db:
                    db.execute("UPDATE jobs SET status='uncertain',finished_at=?,error=? WHERE id=? AND status IN ('queued','running')",
                               (room.now(), "Worker disappeared before saving a terminal outcome; do not resubmit", job_id))
                value = self._job(job_id)
        return value

    def _submit(self, room_id, kind, request_id, payload):
        text_value(request_id, "request_id", 200)
        _, root, _ = self.paths(room_id)
        encoded = room.canonical(payload)
        with room.lock_room(root / "control"):
            return self._submit_locked(room_id, kind, request_id, payload, encoded, root)

    def _submit_locked(self, room_id, kind, request_id, payload, encoded, root, verification=None):
        """Job creation under the room control lock held by the caller. `verification` is the prepared verification
        (verification_id, record digest, attempt, successor identifier) whose registry row is inserted in the same
        transaction as its job row, so a crash before that transaction leaves no registered verification."""
        if True:
            with self.db() as db:
                old = db.execute("SELECT id,payload FROM jobs WHERE room_id=? AND kind=? AND request_key=?", (room_id, kind, request_id)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise room.RoomError("request_id was already used with different content")
                return {**self._refresh(old["id"]), "duplicate": True}
            recovery_id = payload.get("recovery_id") if kind == "implementation" else None
            if recovery_id is not None:
                self._prepared_recovery(room_id, payload["handoff_id"], recovery_id)
            self._guard_idle(room_id, recovery_id, payload.get("job_id") if kind == "verification" else None)
            if kind == "review":
                review_room = root / "review"
                with contextlib.closing(room.connect(review_room)) as db:
                    room.get_spec(db, review_room, payload["revision"], current=True)
                    profile = room.load_config(room.meta(db, "config_path"))
                    if room.canonical(profile) != room.meta(db, "config_snapshot"):
                        raise room.RoomError("Review configuration changed since room initialization")
                report = room.status_report(review_room)
                if report["blocking_turns"]:
                    raise room.RoomError("A prior review has an unresolved outcome; inspect or use its narrow audited recovery")
                if report.get("user_decision_required", sum(turn["status"] != "not_sent" for turn in report["turns"]) >= report["review_turn_limit"]):
                    raise room.RoomError("Review exchange limit reached; bring unresolved decisions to the user")
                room.validate_subscription_environment()
            identifier = uuid.uuid4().hex
            if kind == "verification":
                identifier = verification["successor_job_id"]  # pre-generated: the durable record and dispatch note already name it
            if kind in ("implementation", "verification"):
                self._ensure_handoff_current(room_id, payload["handoff_id"])
                if recovery_id is not None:
                    self._dispatch_recovery(room_id, payload["handoff_id"], recovery_id, identifier)
            path = self._job_path(identifier)
            path.mkdir(parents=True, mode=0o700)
            # The worker lease is acquired here, before the job row is published, and handed to the worker as an
            # inherited descriptor: a live worker holds its lease from its first instant, so status can never take it
            # during startup and a delayed worker is never mistaken for a vanished one.
            try:
                lease = os.open(str(path / "worker.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except OSError as exc:
                raise room.RoomError(f"Worker lease could not be created; nothing was submitted: {type(exc).__name__}") from exc
            try:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise room.RoomError(f"Worker lease could not be acquired; nothing was submitted: {type(exc).__name__}") from exc
                with self.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO jobs(id,room_id,kind,request_key,payload,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                               (identifier, room_id, kind, request_id, encoded, room.now()))
                    if recovery_id is not None:
                        changed = db.execute("UPDATE implementation_recoveries SET status='dispatched',dispatched_at=?,successor_job_id=? "
                                             "WHERE id=? AND room_id=? AND status='prepared'", (room.now(), identifier, recovery_id, room_id)).rowcount
                        if changed != 1:
                            raise room.RoomError("Recovery is no longer prepared; audit it again")
                    if kind == "verification":
                        db.execute("INSERT INTO implementation_verifications(id,room_id,handoff_id,predecessor_job_id,attempt,request_key,payload,status,created_at,successor_job_id,record_sha256) "
                                   "VALUES(?,?,?,?,?,?,?,'dispatched',?,?,?)",
                                   (verification["verification_id"], room_id, payload["handoff_id"], payload["job_id"], verification["attempt"], request_id,
                                    verification["request_payload"], verification["created_at"], identifier, verification["record_sha256"]))
                if recovery_id is not None:
                    self._event(room_id, "implementation_recovery_dispatched", {"recovery_id": recovery_id, "successor_job_id": identifier, "handoff_id": payload["handoff_id"]})
                if kind == "verification":
                    self._event(room_id, "implementation_verification_dispatched", {"verification_id": verification["verification_id"], "successor_job_id": identifier,
                                                                                    "handoff_id": payload["handoff_id"], "job_id": payload["job_id"],
                                                                                    "record_sha256": verification["record_sha256"], "budget_seconds": verification["budget_seconds"]})
                try:
                    with (path / "worker.log").open("wb") as output:
                        process = subprocess.Popen([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "_worker", identifier,
                                                    "--lease-fd", str(lease)],
                                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
                                                   pass_fds=(lease,), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    with self.db() as db:
                        db.execute("UPDATE jobs SET pid=? WHERE id=?", (process.pid, identifier))
                    # Reap children while this MCP lives; a disconnected client still leaves
                    # the detached worker running, with no wait thread keeping the host alive.
                    threading.Thread(target=process.wait, daemon=True).start()
                except OSError as exc:
                    # No worker exists: closing the parent's descriptor below releases ownership; the failure stays truthful.
                    with self.db() as db:
                        db.execute("UPDATE jobs SET status='failed',finished_at=?,error=? WHERE id=?", (room.now(), f"Worker did not start: {exc}", identifier))
                    if recovery_id is not None:
                        self._invalidate_recovery(room_id, recovery_id, "worker_spawn_failure", identifier)
                    if kind == "verification":
                        self._invalidate_verification(room_id, verification["verification_id"], "worker_spawn_failure", identifier)
            finally:
                os.close(lease)  # the spawned worker keeps the inherited lease; the parent's copy is released
            return self._job(identifier)

    def _recovery_row(self, recovery_id):
        if not isinstance(recovery_id, str) or not re.fullmatch(r"[0-9a-f]{32}", recovery_id):
            raise room.RoomError("Invalid recovery_id")
        with self.db() as db:
            row = db.execute("SELECT * FROM implementation_recoveries WHERE id=?", (recovery_id,)).fetchone()
        return dict(row) if row else None

    def _active_recovery(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM implementation_recoveries WHERE predecessor_job_id=? AND status IN ('prepared','dispatched')", (job_id,)).fetchone()
        return dict(row) if row else None

    def _recovery_context(self, room_id, handoff_id, job_id):
        handoff_path = self._handoff_path(room_id, handoff_id)
        job = self._job(job_id)
        if job["room_id"] != room_id:
            raise room.RoomError("Job does not belong to this room")
        _, root, review = self.paths(room_id)
        with self.db() as db:
            open_issues = db.execute("SELECT 1 FROM issues WHERE room_id=? AND disposition='open'", (room_id,)).fetchone() is not None
        return {"job": job, "handoff_id": handoff_id, "handoff_path": handoff_path, "review_path": review,
                "open_issues": open_issues, "job_dir": self._job_path(job_id), "inspector": self.process_inspector}

    def _invalidate_recovery(self, room_id, recovery_id, reason, successor_job_id=None):
        import recovery
        row = self._recovery_row(recovery_id)
        if row is None:
            return
        with self.db() as db:
            db.execute("UPDATE implementation_recoveries SET status='invalidated',invalidated_at=?,reason=?,successor_job_id=COALESCE(successor_job_id,?) "
                       "WHERE id=? AND status IN ('prepared','dispatched')", (room.now(), reason, successor_job_id, recovery_id))
        try:
            recovery.invalidate(self._handoff_path(room_id, row["handoff_id"]), recovery_id, reason, successor_job_id)
        except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError):
            pass  # The registry row is authoritative; the next room_implementation_recover completes the projection lazily.
        self._event(room_id, "implementation_recovery_invalidated", {"recovery_id": recovery_id, "reason": reason, "successor_job_id": successor_job_id})

    def _reconcile_projection(self, room_id, handoff_id):
        """Mutating-only lazy reconciliation: a projection left recovery_prepared by a crash before registration,
        or after the registry already invalidated its recovery, returns to blocked through an audited transition."""
        import implementation
        import recovery
        path = self._handoff_path(room_id, handoff_id)
        _, _, state = implementation._load(path)
        prepared = state.get("recovery") if isinstance(state.get("recovery"), dict) else None
        if state.get("phase") != "recovery_prepared" or not prepared:
            return
        row = self._recovery_row(prepared["recovery_id"]) if re.fullmatch(r"[0-9a-f]{32}", str(prepared.get("recovery_id"))) else None
        try:
            if row is None:
                recovery.invalidate(path, prepared["recovery_id"], "registration_incomplete")
                self._event(room_id, "implementation_recovery_invalidated", {"recovery_id": prepared["recovery_id"], "reason": "registration_incomplete", "successor_job_id": None})
            elif row["status"] == "invalidated":
                recovery.invalidate(path, prepared["recovery_id"], row["reason"] or "invalidated", row["successor_job_id"])
        except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
            pass  # The projection stays where it is; the following audit reports projection_out_of_sync instead of guessing.

    def room_implementation_audit(self, room_id, handoff_id, job_id):
        """Read-only observation; repairs nothing and creates no authorization."""
        import recovery
        text_value(handoff_id, "handoff_id", 200)
        context = self._recovery_context(room_id, handoff_id, job_id)
        _, root, _ = self.paths(room_id)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(room.lock_room(root / "control"))
            except room.RoomError:
                return recovery.blocked_report(handoff_id, job_id, "cooperating_owner_active")
            try:
                report, _ = recovery.audit(active_recovery=self._active_recovery(job_id), **context)
            except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
                return recovery.blocked_report(handoff_id, job_id, "handoff_integrity")
        return report

    def room_implementation_recover(self, room_id, handoff_id, job_id, spec_revision, spec_sha256, candidate_sha256,
                                    evidence_digest, diagnosis, remaining_work, authorization, request_id):
        import recovery
        text_value(handoff_id, "handoff_id", 200)
        positive_revision(spec_revision)
        for name, value in (("spec_sha256", spec_sha256), ("candidate_sha256", candidate_sha256), ("evidence_digest", evidence_digest)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise room.RoomError(f"{name} must be a 64-character lowercase hex digest from the audit")
        text_value(diagnosis, "diagnosis")
        text_value(remaining_work, "remaining_work")
        text_value(authorization, "authorization")
        text_value(request_id, "request_id", 200)
        supplied = {"spec_revision": spec_revision, "spec_sha256": spec_sha256, "candidate_sha256": candidate_sha256, "evidence_digest": evidence_digest}
        encoded = room.canonical({"handoff_id": handoff_id, "job_id": job_id, **supplied, "diagnosis": diagnosis,
                                  "remaining_work": remaining_work, "authorization": authorization})
        _, root, _ = self.paths(room_id)
        with room.lock_room(root / "control"):
            with self.db() as db:
                old = db.execute("SELECT * FROM implementation_recoveries WHERE room_id=? AND request_key=?", (room_id, request_id)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise room.RoomError("request_id was already used with different content")
                return self._recovery_public(dict(old), duplicate=True)
            context = self._recovery_context(room_id, handoff_id, job_id)
            self._reconcile_projection(room_id, handoff_id)
            active = self._active_recovery(job_id)
            if active:
                raise room.RoomError("Recovery is not eligible: recovery_already_exists")
            recovery_id = uuid.uuid4().hex
            def on_locked(report, private):
                return recovery.prepare(report, private, recovery_id, room_id, request_id, context["job"], diagnosis, remaining_work,
                                        authorization, supplied, self.home)
            report, prepared = recovery.audit(active_recovery=None, on_locked=on_locked, **context)
            if not report["eligible"]:
                raise room.RoomError("Recovery is not eligible: " + ", ".join(report["reasons"]))
            try:
                with self.db() as db:
                    db.execute("INSERT INTO implementation_recoveries(id,room_id,handoff_id,predecessor_job_id,request_key,payload,status,created_at,record_sha256) "
                               "VALUES(?,?,?,?,?,?,'prepared',?,?)", (recovery_id, room_id, handoff_id, job_id, request_id, encoded, prepared["created_at"], prepared["record_sha256"]))
            except sqlite3.Error as exc:
                # The projection advanced before registration; return it to blocked through the audited transition.
                recovery.invalidate(context["handoff_path"], recovery_id, "registration_failed")
                raise room.RoomError("Recovery registration failed; the handoff returned to blocked and may be audited again") from exc
            self._event(room_id, "implementation_recovery_prepared", {"recovery_id": recovery_id, "job_id": job_id, "handoff_id": handoff_id,
                                                                     "kind": prepared["kind"], "record_sha256": prepared["record_sha256"]})
            return self._recovery_public(self._recovery_row(recovery_id), duplicate=False, report=report)

    def _recovery_public(self, row, duplicate, report=None):
        value = {"recovery_id": row["id"], "status": row["status"], "duplicate": duplicate, "handoff_id": row["handoff_id"],
                 "predecessor_job_id": row["predecessor_job_id"], "successor_job_id": row["successor_job_id"],
                 "created_at": row["created_at"], "record_sha256": row["record_sha256"], "reason": row["reason"],
                 "next": {"tool": "room_implementation_submit", "handoff_id": row["handoff_id"], "recovery_id": row["id"]} if row["status"] == "prepared" else None}
        if report is not None:
            value["audit"] = report
        return value

    def _prepared_recovery(self, room_id, handoff_id, recovery_id):
        row = self._recovery_row(recovery_id)
        if row is None or row["room_id"] != room_id or row["handoff_id"] != handoff_id:
            raise room.RoomError("Unknown recovery for this room and handoff")
        if row["status"] != "prepared":
            raise room.RoomError(f"Recovery is {row['status']}; only a prepared recovery dispatches its single successor")
        return row

    def _dispatch_recovery(self, room_id, handoff_id, recovery_id, successor_job_id):
        import recovery
        row = self._prepared_recovery(room_id, handoff_id, recovery_id)
        context = self._recovery_context(room_id, handoff_id, row["predecessor_job_id"])
        handoff_dir = Path(context["handoff_path"]).parent
        existing = handoff_dir / "recoveries" / recovery_id / "dispatch.json"
        try:
            # A dispatch file whose job never reached the registry (crash before the transaction) may be replaced;
            # one naming a registered job proves an earlier dispatch and refuses. Read only through owned descriptors.
            named = json.loads(recovery.read_owned(existing, recovery.EVIDENCE_LIMIT, "record", root=handoff_dir)).get("successor_job_id")
        except (recovery.ObservationError, ValueError, AttributeError):
            named = None
        if named is not None:
            with self.db() as db:
                if named and db.execute("SELECT 1 FROM jobs WHERE id=?", (named,)).fetchone():
                    raise room.RoomError("Recovery already has a registered successor")
        def on_locked(report, private):
            return recovery.write_dispatch(private["directory"], recovery_id, successor_job_id, private["root"])
        report, _ = recovery.audit(active_recovery=row, expect_recovery=recovery_id, on_locked=on_locked, **context)
        if not report["eligible"]:
            if any(reason in recovery.INVALIDATING for reason in report["reasons"]):
                self._invalidate_recovery(room_id, recovery_id, ",".join(report["reasons"]))
            raise room.RoomError("Recovery dispatch refused: " + ", ".join(report["reasons"]))

    def _verification_row(self, verification_id):
        if not isinstance(verification_id, str) or not re.fullmatch(r"[0-9a-f]{32}", verification_id):
            raise room.RoomError("Invalid verification_id")
        with self.db() as db:
            row = db.execute("SELECT * FROM implementation_verifications WHERE id=?", (verification_id,)).fetchone()
        return dict(row) if row else None

    def _active_verification(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM implementation_verifications WHERE predecessor_job_id=? AND status='dispatched'", (job_id,)).fetchone()
        return dict(row) if row else None

    def _earlier_gate_pids(self, room_id, handoff_id, job_id):
        """Pids recorded by earlier verifier gates for this predecessor (spawn and exit receipts under the private verification records)."""
        pids = set()
        handoff_dir = Path(self._handoff_path(room_id, handoff_id)).parent
        with self.db() as db:
            rows = db.execute("SELECT id FROM implementation_verifications WHERE room_id=? AND predecessor_job_id=?", (room_id, job_id)).fetchall()
        import recovery
        for row in rows:
            for index in range(1, 65):
                found = False
                for name in ("process-start.json", "process-result.json"):
                    receipt = handoff_dir / "verifications" / row["id"] / ("gate-%d" % index) / name
                    try:
                        saved = json.loads(recovery.read_owned(receipt, recovery.EVIDENCE_LIMIT, "record", root=handoff_dir))
                    except (recovery.ObservationError, OSError, ValueError):
                        continue
                    found = True
                    if isinstance(saved, dict) and type(saved.get("pid")) is int:
                        pids.add(saved["pid"])
                if not found:
                    break
        return pids

    def _invalidate_verification(self, room_id, verification_id, reason, successor_job_id=None):
        import verification
        row = self._verification_row(verification_id)
        if row is None:
            return
        with self.db() as db:
            db.execute("UPDATE implementation_verifications SET status='invalidated',finished_at=?,reason=?,successor_job_id=COALESCE(successor_job_id,?) "
                       "WHERE id=? AND status='dispatched'", (room.now(), reason, successor_job_id, verification_id))
        try:
            verification.invalidate(self._handoff_path(room_id, row["handoff_id"]), verification_id, reason, successor_job_id)
        except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError):
            pass  # The registry row is authoritative; the next room_verification_retry completes the projection lazily.
        self._event(room_id, "implementation_verification_invalidated", {"verification_id": verification_id, "reason": reason, "successor_job_id": successor_job_id})

    def _reconcile_verification(self, room_id, handoff_id):
        """Mutating-only lazy reconciliation of a verification binding left behind by a crash: no registry row returns the
        projection to blocked (registration_incomplete); a dispatched row whose verifier is no longer active projects a
        durable completed outcome only after the outcome chain, gate digests and current candidate re-verify, otherwise it
        is invalidated (verifier_incomplete). Returns the consumed row when a completion was projected."""
        import implementation
        import verification
        path = self._handoff_path(room_id, handoff_id)
        _, _, state = implementation._load(path)
        binding = state.get("verification") if isinstance(state.get("verification"), dict) else None
        try:
            if not binding:
                return self._settle_unbound(room_id, handoff_id, path)
            if state.get("phase") not in ("blocked", "verifying"):
                return None
            row = self._verification_row(binding["verification_id"]) if re.fullmatch(r"[0-9a-f]{32}", str(binding.get("verification_id"))) else None
            if row is None:
                verification.invalidate(path, binding["verification_id"], "registration_incomplete")
                self._event(room_id, "implementation_verification_invalidated", {"verification_id": binding["verification_id"], "reason": "registration_incomplete", "successor_job_id": None})
                return None
            if row["status"] in ("invalidated", "interrupted"):
                verification.invalidate(path, row["id"], row["reason"] or row["status"], row["successor_job_id"])
                return None
            successor = self._refresh(row["successor_job_id"]) if row["successor_job_id"] else None
            if row["status"] == "dispatched" and successor and successor["status"] in ACTIVE:
                return None  # a live verifier owns the projection
            if row["status"] != "dispatched":
                return None  # a settled row never reconciles a stale binding; the audit reports projection_out_of_sync
            if not row["outcome_sha256"]:
                # No digest was registered by the owning execution: a planted or unregistered outcome is never adopted, so the
                # verification is invalidated and the (unchanged) candidate needs a fresh, separately requested retry.
                self._invalidate_verification(room_id, row["id"], "verifier_incomplete:unregistered_outcome", row["successor_job_id"])
                return None
            status, detail = verification.reconcile_completion(path, row["id"], row["outcome_sha256"])
            return self._settle_row(room_id, row, status, detail)
        except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return None  # The projection stays where it is; the following audit reports projection_out_of_sync instead of guessing.

    def _settle_unbound(self, room_id, handoff_id, path):
        """Crash after the engine projected its outcome but before the worker recorded it: dispatched rows whose successor is
        no longer active are settled only from their registered digest re-verified against the outcome chain and the current
        projection and candidate; anything else is invalidated without touching the projection."""
        import verification
        with self.db() as db:
            stale = [dict(r) for r in db.execute("SELECT * FROM implementation_verifications WHERE room_id=? AND handoff_id=? AND status='dispatched'", (room_id, handoff_id))]
        settled = None
        for row in stale:
            successor = self._refresh(row["successor_job_id"]) if row["successor_job_id"] else None
            if successor and successor["status"] in ACTIVE:
                continue
            if not row["outcome_sha256"]:
                self._invalidate_verification(room_id, row["id"], "verifier_incomplete:unregistered_outcome", row["successor_job_id"])
                continue
            status, detail = verification.settle_projected(path, row["id"], row["outcome_sha256"], row["record_sha256"])
            settled = self._settle_row(room_id, row, status, detail) or settled
        if settled is None and not stale:
            with contextlib.suppress(room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
                if verification.clear_orphan_verifying(path):
                    self._event(room_id, "implementation_verification_invalidated", {"verification_id": None, "reason": "registration_incomplete", "successor_job_id": None})
        return settled

    def _settle_row(self, room_id, row, status, detail):
        if status == "completed":
            with self.db() as db:
                db.execute("UPDATE implementation_verifications SET status='consumed',finished_at=COALESCE(finished_at,?),reason='reconciled' "
                           "WHERE id=? AND status='dispatched' AND outcome_sha256=?", (room.now(), row["id"], detail))
            self._event(room_id, "implementation_verification_consumed", {"verification_id": row["id"], "successor_job_id": row["successor_job_id"], "reconciled": True})
            return self._verification_row(row["id"])
        if status == "interrupted":
            with self.db() as db:
                db.execute("UPDATE implementation_verifications SET status='interrupted',finished_at=COALESCE(finished_at,?),reason=? WHERE id=? AND status='dispatched'",
                           (room.now(), detail, row["id"]))
            self._event(room_id, "implementation_verification_interrupted", {"verification_id": row["id"], "successor_job_id": row["successor_job_id"], "reason": detail, "reconciled": True})
            return None
        self._invalidate_verification(room_id, row["id"], "verifier_incomplete:" + str(detail), row["successor_job_id"])
        return None

    def _verification_context(self, room_id, handoff_id, job_id):
        context = self._recovery_context(room_id, handoff_id, job_id)
        context["earlier_pids"] = self._earlier_gate_pids(room_id, handoff_id, job_id)
        with self.db() as db:
            context["earlier_markers"] = [row[0] for row in db.execute("SELECT id FROM implementation_verifications WHERE room_id=? AND predecessor_job_id=?", (room_id, job_id))]
        return context

    def room_verification_audit(self, room_id, handoff_id, job_id):
        """Read-only observation of a completed-generation, gate-timeout attempt; runs no model, gate or network call and repairs nothing."""
        import verification
        text_value(handoff_id, "handoff_id", 200)
        context = self._verification_context(room_id, handoff_id, job_id)
        _, root, _ = self.paths(room_id)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(room.lock_room(root / "control"))
            except room.RoomError:
                return verification.blocked_report(handoff_id, job_id, "cooperating_owner_active")
            try:
                report, _ = verification.audit(active_verification=self._active_verification(job_id), **context)
            except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
                return verification.blocked_report(handoff_id, job_id, "handoff_integrity")
        return report

    def room_verification_retry(self, room_id, handoff_id, job_id, spec_revision, spec_sha256, candidate_sha256, evidence_digest,
                                gates_sha256, gate_timeout_seconds, diagnosis, authorization, request_id):
        import verification
        text_value(handoff_id, "handoff_id", 200)
        positive_revision(spec_revision)
        for name, value in (("spec_sha256", spec_sha256), ("candidate_sha256", candidate_sha256), ("evidence_digest", evidence_digest), ("gates_sha256", gates_sha256)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise room.RoomError(f"{name} must be a 64-character lowercase hex digest from the audit")
        if type(gate_timeout_seconds) is not int or gate_timeout_seconds <= 0 or gate_timeout_seconds > verification.BUDGET_MAX:
            raise room.RoomError("gate_timeout_seconds must be a positive integer of at most %d seconds" % verification.BUDGET_MAX)
        text_value(diagnosis, "diagnosis")
        text_value(authorization, "authorization")
        text_value(request_id, "request_id", 200)
        supplied = {"spec_revision": spec_revision, "spec_sha256": spec_sha256, "candidate_sha256": candidate_sha256,
                    "evidence_digest": evidence_digest, "gates_sha256": gates_sha256}
        payload = {"handoff_id": handoff_id, "job_id": job_id, **supplied, "gate_timeout_seconds": gate_timeout_seconds,
                   "diagnosis": diagnosis, "authorization": authorization, "boundary": verification.BOUNDARY}
        encoded = room.canonical(payload)
        _, root, _ = self.paths(room_id)
        with room.lock_room(root / "control"):
            with self.db() as db:
                old = db.execute("SELECT * FROM implementation_verifications WHERE room_id=? AND request_key=?", (room_id, request_id)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise room.RoomError("request_id was already used with different content")
                return self._verification_public(dict(old), duplicate=True)
            context = self._verification_context(room_id, handoff_id, job_id)
            reconciled = self._reconcile_verification(room_id, handoff_id)
            if reconciled is not None:
                return self._verification_public(reconciled, duplicate=False, reconciled=True)
            if self._active_verification(job_id):
                raise room.RoomError("Verification is not eligible: verification_already_exists")
            verification_id, successor_job_id = uuid.uuid4().hex, uuid.uuid4().hex
            def on_locked(report, private):
                return verification.prepare(report, private, verification_id, room_id, request_id, context["job"], supplied, gate_timeout_seconds,
                                            diagnosis, authorization, self.home, successor_job_id)
            try:
                report, prepared = verification.audit(active_verification=None, on_locked=on_locked, **context)
            except (implementation_error_types(), OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise room.RoomError("Verification is not eligible: handoff_integrity") from exc
            if not report["eligible"]:
                raise room.RoomError("Verification is not eligible: " + ", ".join(report["reasons"]))
            handoff_path = str(context["handoff_path"])
            job_payload = {"handoff_id": handoff_id, "handoff_path": handoff_path, "job_id": job_id, "verification_id": verification_id}
            try:
                job = self._submit_locked(room_id, "verification", request_id, job_payload, room.canonical(job_payload), root,
                                          verification={"verification_id": verification_id, "successor_job_id": successor_job_id,
                                                        "record_sha256": prepared["record_sha256"], "created_at": prepared["created_at"], "request_payload": encoded,
                                                        "attempt": context["job"]["result"]["attempt_count"], "budget_seconds": prepared["budget_seconds"]})
            except (room.RoomError, sqlite3.Error) as exc:
                # The record and projection binding exist but no registration happened: return the projection to blocked.
                try:
                    verification.invalidate(context["handoff_path"], verification_id, "registration_failed", successor_job_id)
                except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError):
                    pass
                raise room.RoomError("Verification registration failed; the handoff returned to blocked and may be audited again") from exc
            row = self._verification_row(verification_id)
            if row is None:
                raise room.RoomError("Verification registration failed; the handoff returned to blocked and may be audited again")
            return self._verification_public(row, duplicate=False, report=report, job=job)

    def _verification_public(self, row, duplicate, report=None, job=None, reconciled=False):
        value = {"verification_id": row["id"], "status": row["status"], "duplicate": duplicate, "reconciled": reconciled, "handoff_id": row["handoff_id"],
                 "predecessor_job_id": row["predecessor_job_id"], "successor_job_id": row["successor_job_id"], "attempt": row["attempt"],
                 "created_at": row["created_at"], "finished_at": row["finished_at"], "record_sha256": row["record_sha256"],
                 "outcome_sha256": row["outcome_sha256"], "reason": row["reason"], "boundary": "isolated_copy",
                 "next": {"tool": "room_job_status", "job_id": row["successor_job_id"]} if row["status"] == "dispatched" else None}
        if report is not None:
            value["audit"] = report
        if job is not None:
            value["job"] = {"id": job["id"], "status": job["status"]}
        return value

    def room_review_submit(self, room_id, revision, message, request_id):
        positive_revision(revision)
        text_value(message, "message")
        _, root, review = self.paths(room_id)
        with contextlib.closing(room.connect(review)) as db:
            spec = room.get_spec(db, review, revision)
            session = room.meta(db, "session_id")
        settings = self._room_settings(root)
        return self._submit(room_id, "review", request_id, {"revision": revision, "message": message,
                            "spec_sha256": spec["sha256"], "session_transcript": transcript_path(settings["claude_config_dir"], review, session)})

    def room_job_status(self, job_id, wait_seconds=0):
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 45:
            raise room.RoomError("wait_seconds must be finite and between 0 and 45")
        deadline = time.monotonic() + wait_seconds
        while True:
            job = self._refresh(job_id)
            if job["status"] not in ACTIVE or time.monotonic() >= deadline:
                return self._with_progress(job, progress.clock())
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def room_job_cancel(self, job_id):
        job = self._refresh(job_id)
        if job["status"] in ACTIVE:
            atomic_json(self._job_path(job_id) / "cancel.json", {"requested_at": room.now()})
            return {"job_id": job_id, "cancel_requested": True, "note": "The owning worker stops its own process; inspect the terminal outcome before further work"}
        return {"job_id": job_id, "cancel_requested": False, "status": job["status"]}

    def room_job_recover(self, job_id, diagnosis):
        text_value(diagnosis, "diagnosis")
        job = self._job(job_id)
        if job["kind"] != "review" or job["status"] != "failed":
            raise room.RoomError("Only a failed review with proven local authentication non-delivery supports this recovery")
        _, root, review = self.paths(job["room_id"])
        with room.lock_room(root / "control"):
            self._guard_idle(job["room_id"])
            note = self._job_path(job_id) / "nondelivery-diagnosis.md"
            note.write_text(diagnosis, encoding="utf-8")
            from session_paths import find_session_transcript, SessionPathError
            settings = self._room_settings(root)
            current = room.status_report(review)
            explicit = None
            try:
                explicit = find_session_transcript(settings["claude_config_dir"], current["session_id"], expected_cwd=review,
                                                   predicted_path=job["payload"]["session_transcript"])
            except SessionPathError as exc:
                # No session file is also a valid preflight outcome. Ambiguous or
                # mismatched evidence must remain blocked rather than create another.
                if "found 0" not in str(exc):
                    raise room.RoomError(str(exc)) from exc
            result = room.recover_not_sent(SimpleNamespace(request_id=job["request_key"], note_file=str(note), session_transcript=explicit), review)
            with self.db() as db:
                db.execute("UPDATE jobs SET status='not_sent' WHERE id=?", (job_id,))
            self._event(job["room_id"], "nondelivery_recovery", {"job_id": job_id, "diagnosis": diagnosis, "evidence": result})
            return result

    def room_handoff(self, room_id, revision, authorization, gates):
        import implementation
        positive_revision(revision)
        text_value(authorization, "authorization")
        entry, root, review = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            with self.db() as db:
                if db.execute("SELECT 1 FROM issues WHERE room_id=? AND disposition='open'", (room_id,)).fetchone():
                    raise room.RoomError("Every review finding needs a disposition and rationale before handoff")
            result = implementation.prepare_handoff(review, Path(entry["project_path"]), revision, authorization, gates, root / "profiles/implementation.json")
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO handoffs VALUES(?,?,?)", (result["handoff_id"], room_id, result["handoff_path"]))
            self._event(room_id, "handoff", result)
            return result

    def _handoff_path(self, room_id, handoff_id):
        self.entry(room_id)
        with self.db() as db:
            row = db.execute("SELECT path FROM handoffs WHERE room_id=? AND id=?", (room_id, handoff_id)).fetchone()
        if not row:
            raise room.RoomError("Unknown handoff for this room")
        return Path(row[0])

    def room_implementation_status(self, room_id, handoff_id):
        import handoff_status
        if not isinstance(handoff_id, str) or not re.fullmatch(r'[0-9a-f]{64}', handoff_id):
            raise room.RoomError('Invalid handoff_id')
        path = self._handoff_path(room_id, handoff_id)
        return handoff_status.load(room_id, handoff_id, path)

    def _ensure_handoff_current(self, room_id, handoff_id):
        import implementation
        _, _, review = self.paths(room_id)
        handoff = implementation.implementation_status(self._handoff_path(room_id, handoff_id))
        current = room.status_report(review)
        with self.db() as db:
            unresolved = db.execute("SELECT 1 FROM issues WHERE room_id=? AND disposition='open'", (room_id,)).fetchone()
        if (not current["agreement"] or unresolved or current["current_revision"] != handoff["spec_revision"]
                or current["spec_sha256"] != handoff["spec_sha256"]):
            raise room.RoomError("Handoff no longer matches the current agreed spec and resolved findings")

    def room_implementation_submit(self, room_id, handoff_id, request_id, recovery_id=None):
        path = self._handoff_path(room_id, handoff_id)
        payload = {"handoff_id": handoff_id, "handoff_path": str(path)}
        if recovery_id is not None:
            self._recovery_row(recovery_id)  # Validates the identifier shape before any registry work.
            payload["recovery_id"] = recovery_id
        return self._submit(room_id, "implementation", request_id, payload)

    def room_implementation_review(self, room_id, handoff_id, accepted, review):
        import implementation
        if type(accepted) is not bool:
            raise room.RoomError("accepted must be boolean")
        text_value(review, "review")
        _, root, _ = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            self._ensure_handoff_current(room_id, handoff_id)
            result = implementation.record_astra_review(self._handoff_path(room_id, handoff_id), accepted, review)
            self._event(room_id, "product_review", result)
            return result

    def room_implementation_revise(self, room_id, handoff_id, review):
        import implementation
        text_value(review, "review")
        _, root, _ = self.paths(room_id)
        with room.lock_room(root / "control"):
            self._guard_idle(room_id)
            self._ensure_handoff_current(room_id, handoff_id)
            result = implementation.request_changes(self._handoff_path(room_id, handoff_id), review)
            self._event(room_id, "implementation_correction", result)
            return result

    def execute_job(self, job_id):
        job = self._job(job_id)
        _, root, review = self.paths(job["room_id"])
        settings = self._room_settings(root)
        if settings.get("claude_config_dir_override") is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = settings["claude_config_dir_override"]
        payload = job["payload"]
        if job["kind"] == "review":
            request = self._job_path(job_id) / "message.md"
            request.write_bytes((payload["message"] + "\n\nIn findings, prefix blocking objections with BLOCKER: and optional out-of-scope enhancements with SUGGESTION:. Give an independent interpretation first; include your proposed technical design and implementation/verification plan in that interpretation, proportional to this feature. The room tracks each finding's disposition. This turn remains review only.").encode())
            result = room.ask(SimpleNamespace(revision=payload["revision"], message_file=str(request),
                                             config=None, request_id=job["request_key"], timeout=None,
                                             session_transcript=payload["session_transcript"]), review)
            if (result["status"] == "failed" and result["return_code"] == 0
                    and (result.get("error") or "").startswith("Model identity verification failed:")):
                from session_paths import find_session_transcript, SessionPathError
                try:
                    actual = find_session_transcript(settings["claude_config_dir"], result["session_id"],
                                                     expected_cwd=review, predicted_path=payload["session_transcript"])
                    note = self._job_path(job_id) / "identity-reconciliation.md"
                    note.write_text("Initial primary-producer evidence verification failed. Located exactly one transcript with the preallocated session UUID in the configured projects directory and checked its session/cwd metadata. Revalidate the saved terminal reply against that explicit file, preserving the original identity failure. No model resubmission.\n")
                    result = room.reconcile(SimpleNamespace(config=None, request_id=job["request_key"],
                                                            session_transcript=actual, note_file=str(note)), review)
                except (room.RoomError, SessionPathError, OSError, ValueError):
                    pass  # Preserve the original failure; finding evidence is not a retry.
            return result
        if job["kind"] == "implementation":
            import implementation
            recovery_id = payload.get("recovery_id")
            if recovery_id is None:
                return implementation.run_implementation(Path(payload["handoff_path"]), owner_job_id=job_id)
            import recovery
            try:
                row = self._recovery_row(recovery_id)
                if (row is None or row["status"] != "dispatched" or row["successor_job_id"] != job_id
                        or row["room_id"] != job["room_id"] or row["handoff_id"] != payload["handoff_id"]):
                    return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "recovery_binding_mismatch",
                            "recovery_id": recovery_id, "handoff_id": payload["handoff_id"], "model_launched": False}
                context = self._recovery_context(job["room_id"], payload["handoff_id"], row["predecessor_job_id"])
            except Exception as exc:  # nothing written or spawned yet: a proven pre-launch refusal, never an unclassifiable successor
                return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "prelaunch_error", "detail": type(exc).__name__,
                        "recovery_id": recovery_id, "handoff_id": payload["handoff_id"], "model_launched": False}
            def recheck():
                # The handoff lock is already held by run_implementation; take only the predecessor's job lock and lease.
                report, _ = recovery.audit(active_recovery=row, expect_recovery=recovery_id, locks=("job", "lease"), **context)
                return report
            return implementation.run_implementation(Path(payload["handoff_path"]),
                                                     successor={"recovery_id": recovery_id, "successor_job_id": job_id, "recheck": recheck,
                                                                "registry": str(self.home)}, owner_job_id=job_id)
        if job["kind"] == "verification":
            import verification
            verification_id = payload.get("verification_id")
            try:
                row = self._verification_row(verification_id)
                if (row is None or row["status"] != "dispatched" or row["successor_job_id"] != job_id
                        or row["room_id"] != job["room_id"] or row["handoff_id"] != payload["handoff_id"] or row["predecessor_job_id"] != payload.get("job_id")):
                    return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "verification_binding_mismatch",
                            "verification_id": verification_id, "handoff_id": payload["handoff_id"], "gates_launched": False, "model_launched": False}
                context = self._verification_context(job["room_id"], payload["handoff_id"], row["predecessor_job_id"])
            except Exception as exc:  # nothing written or spawned yet: a proven pre-launch refusal
                return {"phase": "refused_before_launch", "status": "refused_before_launch", "reason": "prelaunch_error", "detail": type(exc).__name__,
                        "verification_id": verification_id, "handoff_id": payload["handoff_id"], "gates_launched": False, "model_launched": False}
            def recheck():
                # The handoff lock is already held by verification.run; take only the predecessor's job lock and lease.
                report, _ = verification.audit(active_verification=row, expect_verification=verification_id, locks=("job", "lease"), **context)
                return report
            return verification.run(Path(payload["handoff_path"]), {"verification_id": verification_id, "successor_job_id": job_id, "recheck": recheck,
                                                                     "registry": str(self.home), "inspector": self.process_inspector})
        raise room.RoomError("Unknown job kind")

    @contextlib.contextmanager
    def _worker_lease(self, path, lease_fd):
        """Hold this job's worker lease for the whole execution.

        A descriptor inherited from _submit is used only after it is proven to be this job's worker.lock (same
        device and inode, user-owned regular file) and its lease can be re-asserted on that same open file
        description; the inherited lease has then been held since before the spawn. A launch without a handed-off
        descriptor (legacy) or with an invalid one acquires the lease itself with a bounded retry, so transient
        contention such as a status probe never kills the worker."""
        if lease_fd is not None:
            valid = False
            try:
                held, expected = os.fstat(lease_fd), os.lstat(path / "worker.lock")
                valid = (stat.S_ISREG(expected.st_mode) and stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid()
                         and (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino))
                if valid:
                    fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # idempotent on the inherited description
            except OSError:
                valid = False
            if valid:
                try:
                    yield lease_fd
                finally:
                    os.close(lease_fd)
                return
            # The rejected number is left exactly as it was: closing it could drop whatever this process really holds
            # at that number (its own stderr, the room lock or another job's lease), so only a proven descriptor is owned.
            print(json.dumps({"worker": job_id_of(path), "note": "inherited lease descriptor was not this job's worker.lock; acquiring the lease directly"}), file=sys.stderr, flush=True)
        with (path / "worker.lock").open("a") as lease:
            for attempt in range(LEASE_RETRIES):
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt == LEASE_RETRIES - 1:
                        raise
                    time.sleep(0.05)
            yield lease.fileno()

    def worker(self, job_id, lease_fd=None):
        path = self._job_path(job_id)
        with room.lock_room(path):
            with self._worker_lease(path, lease_fd):
                job = self._job(job_id)
                if job["status"] != "queued":
                    return
                started, execution_id = room.now(), uuid.uuid4().hex
                with self.db() as db:
                    db.execute("UPDATE jobs SET status='running',started_at=?,pid=? WHERE id=?", (started, os.getpid(), job_id))
                try:
                    with self.db() as db:
                        db.execute('INSERT INTO worker_executions(job_id,execution_id,started_at) VALUES(?,?,?)', (job_id, execution_id, started))
                except sqlite3.Error:
                    execution_id = None  # Heartbeat storage failure must not stop authorized work.
                def owned_attempt():
                    if job['kind'] != 'implementation':
                        return None
                    path_value = job['payload'].get('handoff_path')
                    if not isinstance(path_value, str):
                        return None
                    state, _ = progress.read_json(Path(path_value).parent / 'state.json', progress.MAX_STATE_BYTES)
                    return state.get('attempt_count') if isinstance(state, dict) and state.get('owner_job_id') == job_id else None
                beat = heartbeat.Emitter(self.home / 'jobs', {**job, 'started_at': started}, execution_id, owned_attempt)
                beat.pulse()
                process = None
                result, error, status = None, None, "uncertain"
                try:
                    if (path / "cancel.json").exists():
                        status, error = "cancelled", "Cancelled before the operation started"
                    else:
                        with (path / "stdout.json").open("wb") as output, (path / "stderr.txt").open("wb") as errors:
                            process = subprocess.Popen([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "_execute", job_id],
                                                       stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
                            while process.poll() is None:
                                beat.pulse()
                                if (path / "cancel.json").exists():
                                    # Signal only the child we created, never a PID read from old state.
                                    process.send_signal(signal.SIGTERM)
                                    try:
                                        process.wait(timeout=10)
                                    except subprocess.TimeoutExpired:
                                        room.stop_process(process)
                                    break
                                time.sleep(0.1)
                        raw = (path / "stdout.json").read_bytes()
                        try:
                            result = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            error = "Operation ended without a readable result; inspect private attempt files"
                        if isinstance(result, dict):
                            phase = result.get("status", result.get("phase"))
                            status = "succeeded" if process.returncode == 0 and phase in ("completed", "awaiting_astra_review", "accepted", "scope_change") else "uncertain" if phase in ("uncertain", "blocked") else "failed"
                            error = result.get("error")
                            if phase == "refused_before_launch" and not error:
                                error = "Refused before launch: " + str(result.get("reason"))
                        if (path / "cancel.json").exists() and status != "succeeded":
                            status, error = "uncertain", "Operation cancelled after starting; session/worktree may have advanced"
                except BaseException as exc:
                    if process is not None and process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            room.stop_process(process)
                    error = f"Worker interrupted ({type(exc).__name__}); delivery may be uncertain"
                beat.pulse(final=True)
                if job["kind"] == "implementation" and isinstance(result, dict):
                    report = result.get("report", {})
                    if report.get("outcome") == "scope_change":
                        _, feature_root, review_room = self.paths(job["room_id"])
                        note = path / "scope-change.md"
                        note.write_text("Fable returned this implementation discovery to Astra for requirements review:\n"
                                        + room.canonical({"handoff_id": job["payload"]["handoff_id"], "report": report}), encoding="utf-8")
                        room.record(SimpleNamespace(sender="astra", kind="message", revision=result["spec_revision"], file=str(note)), review_room)
                recovery_id = job["payload"].get("recovery_id") if job["kind"] == "implementation" else None
                linkage, link_reason = recovery_linkage(result, status) if recovery_id is not None else (None, None)
                verification_id = job["payload"].get("verification_id") if job["kind"] == "verification" else None
                verification_link, verification_reason = (None, None)
                if verification_id is not None:
                    import verification
                    verification_link, verification_reason = verification.linkage(result, status)
                with self.db() as db:
                    db.execute("UPDATE jobs SET status=?,finished_at=?,result=?,error=? WHERE id=?", (status, room.now(), room.canonical(result) if result is not None else None, error, job_id))
                    if verification_link in ("consumed", "interrupted", "invalidated"):
                        outcome_sha = (result.get("verification") or {}).get("outcome_sha256") if isinstance(result, dict) and isinstance(result.get("verification"), dict) else None
                        if verification_link == "consumed":
                            # Only the digest the engine registered under its own ownership can be consumed, and only when the
                            # durable outcome hashing to it records a completed result; a frozen result naming any other digest
                            # (for example a rewritten stdout file, or a registered interrupted outcome) never consumes.
                            durable = verification.durable_outcome_result(Path(job["payload"]["handoff_path"]), verification_id, outcome_sha) if outcome_sha else None
                            changed = 0
                            if durable == "completed":
                                changed = db.execute("UPDATE implementation_verifications SET status='consumed',finished_at=?,reason=? "
                                                     "WHERE id=? AND status='dispatched' AND successor_job_id=? AND outcome_sha256 IS NOT NULL AND outcome_sha256=?",
                                                     (room.now(), verification_reason, verification_id, job_id, outcome_sha)).rowcount
                            if changed != 1:
                                registered = db.execute("SELECT outcome_sha256 FROM implementation_verifications WHERE id=? AND status='dispatched' AND successor_job_id=?",
                                                        (verification_id, job_id)).fetchone()
                                if registered and registered[0]:
                                    # The engine registered a digest but the frozen result could not be trusted or read: the row stays
                                    # dispatched and is settled below from the registry digest, never from the frozen file.
                                    verification_link, verification_reason = "unsettled", "consume_deferred"
                                else:
                                    verification_link, verification_reason = "invalidated", "verifier_incomplete:unregistered_outcome"
                                    db.execute("UPDATE implementation_verifications SET status='invalidated',finished_at=?,reason=? WHERE id=? AND status='dispatched' AND successor_job_id=?",
                                               (room.now(), verification_reason, verification_id, job_id))
                        else:
                            db.execute("UPDATE implementation_verifications SET status=?,finished_at=?,reason=? WHERE id=? AND status='dispatched' AND successor_job_id=?",
                                       (verification_link, room.now(), verification_reason, verification_id, job_id))
                    if verification_link:
                        db.execute("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,?,?,?)", (job["room_id"], "implementation_verification_" + verification_link,
                                   room.canonical({"verification_id": verification_id, "successor_job_id": job_id, "reason": verification_reason}), room.now()))
                    if linkage == "invalidated":
                        db.execute("UPDATE implementation_recoveries SET status='invalidated',invalidated_at=?,reason=? WHERE id=? AND status='dispatched' AND successor_job_id=?",
                                   (room.now(), link_reason, recovery_id, job_id))
                    elif linkage == "consumed":
                        db.execute("UPDATE implementation_recoveries SET status='consumed' WHERE id=? AND status='dispatched' AND successor_job_id=?", (recovery_id, job_id))
                    if linkage:
                        # "launch_unknown" changes no row: the recovery stays dispatched and the successor stays blocked (documented limit).
                        db.execute("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,?,?,?)", (job["room_id"], "implementation_recovery_" + linkage,
                                   room.canonical({"recovery_id": recovery_id, "successor_job_id": job_id, "reason": link_reason}), room.now()))
                    if job["kind"] == "review" and status == "succeeded":
                        review = result["result"]["structured_output"]
                        for index, finding in enumerate(review["findings"]):
                            severity = "suggestion" if finding.lstrip().startswith("SUGGESTION:") else "blocker" if review["decision"] == "changes_required" or finding.lstrip().startswith("BLOCKER:") else "suggestion"
                            db.execute("INSERT OR IGNORE INTO issues(id,room_id,job_id,revision,content,severity,disposition) VALUES(?,?,?,?,?,?,'open')",
                                       (job_id + f"-{index + 1}", job["room_id"], job_id, review["spec_revision"], finding, severity))
                    if job["kind"] == "implementation" and isinstance(result, dict):
                        event_values = [("implementation_result", {"job_id": job_id, "result": result})]
                        report = result.get("report", {})
                        event_values.extend(("backlog", {"content": item, "rationale": "Fable identified an enhancement outside the agreed scope"}) for item in report.get("backlog", []))
                        if report.get("outcome") == "scope_change":
                            event_values.append(("scope_change", {"handoff_id": job["payload"]["handoff_id"], "explanation": report["scope_change"]}))
                            db.execute("INSERT OR IGNORE INTO issues(id,room_id,job_id,revision,content,severity,disposition) VALUES(?,?,?,?,?,'blocker','open')",
                                       (job_id + "-scope", job["room_id"], job_id, result["spec_revision"], report["scope_change"]))
                        for kind, value in event_values:
                            db.execute("INSERT INTO events(room_id,kind,content,created_at) VALUES(?,?,?,?)", (job["room_id"], kind, room.canonical(value), room.now()))
                if linkage == "invalidated":
                    import recovery
                    try:
                        recovery.invalidate(Path(job["payload"]["handoff_path"]), recovery_id, link_reason, job_id)
                    except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
                        pass  # The registry row is authoritative; the next mutating operation completes the projection lazily.
                if verification_link == "invalidated":
                    import verification
                    try:
                        verification.invalidate(Path(job["payload"]["handoff_path"]), verification_id, verification_reason, job_id)
                    except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError):
                        pass  # The registry row is authoritative; the next room_verification_retry completes the projection lazily.
                if verification_link == "unsettled":
                    # Settle immediately from the registry digest when the durable chain re-verifies; otherwise the dispatched row
                    # waits for the next mutating call's reconciliation. A genuine completion is never invalidated here.
                    import verification
                    try:
                        row = self._verification_row(verification_id)
                        if row and row["status"] == "dispatched" and row["outcome_sha256"]:
                            settled = self._settle_row(job["room_id"], row, *verification.settle_projected(Path(job["payload"]["handoff_path"]), verification_id,
                                                                                                          row["outcome_sha256"], row["record_sha256"]))
                            if settled is None:
                                self._event(job["room_id"], "implementation_verification_unsettled", {"verification_id": verification_id, "successor_job_id": job_id, "reason": "consume_deferred"})
                    except (room.RoomError, implementation_error_types(), OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        pass

    def call(self, name, arguments):
        if name not in TOOL_SCHEMAS:
            raise room.RoomError(f"Unknown tool {name}")
        if not isinstance(arguments, dict):
            raise room.RoomError("arguments must be an object")
        schema = TOOL_SCHEMAS[name][1]
        missing = set(schema.get("required", [])) - set(arguments)
        extra = set(arguments) - set(schema["properties"])
        if missing or extra:
            raise room.RoomError(f"Invalid arguments; missing={sorted(missing)}, unexpected={sorted(extra)}")
        if name.startswith("ao_room_"):
            import ao_project_room
            return getattr(ao_project_room.Service(self.home), name)(**arguments)
        return getattr(self, name)(**arguments)


def implementation_error_types():
    import implementation
    return implementation.ImplementationError


def job_id_of(path):
    return Path(path).name


def provider_name(settings):
    """The delegate provider a configuration records (exactly as recorded, so an empty or null selection is judged by
    the provider rules rather than silently inferred), or the legacy inference: qwen when a Qwen config exists, else none."""
    if "delegate_provider" in settings:
        return settings["delegate_provider"]
    return "qwen" if settings.get("qwen_config") else "none"


def recovery_linkage(result, status):
    """Classify a finished recovery successor for the registry: ("invalidated", reason) for a proven pre-launch
    refusal, ("consumed", None) once the engine confirmed the model process was created, otherwise
    ("launch_unknown", reason): no row changes, the recovery stays dispatched and the successor stays blocked."""
    recovery = result.get("recovery") if isinstance(result, dict) else None
    if isinstance(recovery, dict) and recovery.get("launched_at"):
        return "consumed", None  # A confirmed spawn always wins: a later cancellation is a post-launch interruption.
    if isinstance(result, dict) and result.get("phase") == "refused_before_launch":
        return "invalidated", str(result.get("reason") or "refused_before_launch")
    if status == "cancelled":
        return "invalidated", "cancelled_before_launch"  # The worker never started the operation; no result exists.
    return "launch_unknown", (recovery.get("launch_state") if isinstance(recovery, dict) else None) or "no_classifiable_result"


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer", "minimum": 1}
R = {"room_id": S}
TOOL_SCHEMAS = {
    "room_doctor": ("Check local setup and Claude subscription sign-in; does not call a model or Qwen inference.", schema({})),
    "room_open": ("Open or create the persistent room for this exact project directory and feature. Reuses history/session; does not start models.", schema({"project_path": S, "feature": S})),
    "room_list": ("Find existing project/feature rooms before creating another.", schema({"project_path": S}, [])),
    "room_status": ("Read review agreement, unresolved findings, jobs, implementation handoffs and, for a DeepSeek room, delegate_jobs (the latest 20 allowlisted provider ledger facts with usage from the provider, never model assertions). Each job carries an additive read-only progress object (phase, elapsed_seconds, last observed activity category, attributable delegates, countdown to the pinned timeout, limitations); the countdown is a deadline, never an ETA.", schema(R)),
    "room_spec_put": ("Register immutable exact UTF-8 spec revision with repository context and concrete verification.", schema({**R, "revision": I, "content": S})),
    "room_record": ("Record Astra/user discussion or Astra approval of the current exact spec. Does not authorize implementation.", schema({**R, "sender": {"type": "string", "enum": ["astra", "user"]}, "kind": {"type": "string", "enum": ["message", "approval"]}, "revision": I, "content": S})),
    "room_review_submit": ("Start one Fable review asynchronously. Save returned job id; identical request_id/payload reuses the job, never resubmit to poll.", schema({**R, "revision": I, "message": S, "request_id": S})),
    "room_job_status": ("Read or wait up to 45 seconds on a saved job; repeat bounded waits while working. Returns saved terminal evidence plus a read-only progress object: phase (queued/starting/model/gate/finalizing/awaiting_review/terminal/unknown), elapsed_seconds, activity (last observed category/time/source), delegates (requested/pending/background/completed, attributable child models), deadline (remaining seconds until the pinned model or gate timeout, not an ETA), worker heartbeat (liveness only), up to five recent safe activity transitions, and limitation codes. Unavailable evidence is reported as unavailable, never inferred as a stall or completion; reading progress never changes the job.", schema({"job_id": S, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 45}}, ["job_id"])),
    "room_job_cancel": ("Request cancellation of the owning worker. Started operations may remain uncertain; inspect status before continuing.", schema({"job_id": S})),
    "room_job_recover": ("After diagnosis, audit only an exact zero-usage local login failure as not sent. Preserves original failure; runs no model. Then use a new request_id in the same room after fixing setup. Cannot recover unknown delivery.", schema({"job_id": S, "diagnosis": S})),
    "room_history": ("Read room discussion, decisions, backlog, and implementation events. Contains no private model thinking.", schema(R)),
    "room_issue_dispose": ("Record a rationale for every Fable finding: address in a revision, reject with evidence, or defer optional enhancement to backlog.", schema({**R, "issue_id": S, "disposition": {"type": "string", "enum": ["addressed", "rejected", "deferred"]}, "rationale": S, "revision": I})),
    "room_backlog_add": ("Record or update an optional enhancement for the user's opinion, preserving its stable proposal_id and event history. issue_url records an issue Astra separately filed; this tool never publishes. New proposals and changed content/rationale default to pending. Any explicit approved/declined/deferred decision requires actual user decision evidence in decision_rationale, never Fable's suggestion or technical disposition. Approval here does not change the agreed implementation spec.", schema({**R, "content": S, "rationale": S, "issue_url": S, "proposal_id": S,
                                "user_decision": {"type": "string", "enum": ["pending", "approved", "declined", "deferred"]},
                                "decision_rationale": S}, ["room_id", "content", "rationale"])),
    "room_decision_record": ("After the bounded review round is exhausted, record the user's actual decision on the unresolved product tradeoff to allow the next bounded round. Never invent a decision or use this to bypass unknown/failed delivery.", schema({**R, "revision": I, "decision": S})),
    "room_handoff": ("Prepare Fable implementation from exact agreement, resolved findings, original user authorization, and nonempty verification argv gates. Creates isolated git worktree.", schema({**R, "revision": I, "authorization": S, "gates": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 1, "items": S}}})),
    "room_implementation_submit": ("Start/resume authorized Fable implementation asynchronously. Delegates follow the room's pinned provider policy (DeepSeek for new DeepSeek rooms, the legacy Qwen ladder for qwen rooms, Claude tiers only otherwise) plus Sonnet/Opus subagents; Astra checks product outcome after evidence. A tampered provider snapshot is refused before launch (provider_inventory_mismatch). With recovery_id, dispatches the single audited successor of an interrupted attempt (new job/request ID, same session, --resume).", schema({**R, "handoff_id": S, "request_id": S, "recovery_id": S}, ["room_id", "handoff_id", "request_id"])),
    "room_implementation_status": ("Read the current saved handoff phase, spec/candidate identity, acceptance, gate digests and recovery lineage. Historical handoffs remain readable. This compact read does not run gates, inspect candidate files, audit recovery or change frozen job outcomes.", schema({**R, "handoff_id": S})),
    "room_implementation_audit": ("Read-only audit of one interrupted implementation job (configured timeout or session-usage limit): identity, stopped-work evidence, boot boundary, current writers, partial candidate and transcript digests. Runs no model/Qwen/network and repairs nothing. Reports restart_required until the host booted after the interruption.", schema({**R, "handoff_id": S, "job_id": S})),
    "room_implementation_recover": ("After Astra's diagnosis and the user's actual authorization, durably prepare one audited continuation of an eligible interrupted implementation. Requires the audit's exact spec revision/hash, candidate sha256 and evidence digest; never adopts new bytes. Idempotent per request_id. Then submit with the returned recovery_id.", schema({**R, "handoff_id": S, "job_id": S, "spec_revision": I, "spec_sha256": S, "candidate_sha256": S, "evidence_digest": S, "diagnosis": S, "remaining_work": S, "authorization": S, "request_id": S})),
    "room_verification_audit": ("Read-only audit of one implementation job whose model turn completed normally with a valid report before a pinned verification gate hit the pinned gate timeout: original model/report/identity evidence, the identified gate and its receipt, strict transcript equality, candidate identity, evidence digest, stopped-work observation and the isolated-copy boundary's limits. Runs no model, gate or network call and repairs nothing.", schema({**R, "handoff_id": S, "job_id": S})),
    "room_verification_retry": ("After diagnosis and the user's actual authorization for these specific offline gates, re-audit under locks and dispatch one asynchronous verifier that reruns exactly the pinned gate argv arrays in a fresh private copy of the audited candidate with a private TMPDIR (never the model). Requires the audit's exact spec revision/hash, candidate sha256, evidence digest and gates_sha256, plus an integer gate_timeout_seconds between the original pinned budget and 7200 (900 proposed). Idempotent per request_id; returns the verification and its successor job id to wait on. A passed run is gate evidence only; an incomplete report still needs a normal correction.", schema({**R, "handoff_id": S, "job_id": S, "spec_revision": I, "spec_sha256": S, "candidate_sha256": S, "evidence_digest": S, "gates_sha256": S, "gate_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 7200}, "diagnosis": S, "authorization": S, "request_id": S})),
    "room_implementation_review": ("Record Astra's independent product-outcome verdict against the exact verified candidate. Engineering/delegate verdicts remain Fable's responsibility.", schema({**R, "handoff_id": S, "accepted": {"type": "boolean"}, "review": S})),
    "room_implementation_revise": ("Request a diagnosed correction within the same agreed spec, then submit with a new request_id. Unknown delivery cannot be retried.", schema({**R, "handoff_id": S, "review": S})),
}


import ao_project_room
TOOL_SCHEMAS.update(ao_project_room.TOOL_SCHEMAS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--claude-bin")
    setup.add_argument("--qwen-config")
    setup.add_argument("--deepseek-config", help="absolute path to a private key-free DeepSeek provider configuration")
    setup.add_argument("--delegate-provider", choices=PROVIDERS, help="provider snapshotted into NEW rooms; existing rooms keep their pins")
    commands.add_parser("doctor")
    audit = commands.add_parser("transcript-audit", help="read-only tool-use count of one attempt's exact session transcript and its subagent files; no model, no network")
    audit.add_argument("--room", required=True)
    audit.add_argument("--handoff", required=True)
    audit.add_argument("--attempt", type=int, required=True)
    call = commands.add_parser("call")
    call.add_argument("tool", choices=sorted(TOOL_SCHEMAS))
    call.add_argument("--args", default="{}", help="JSON argument object")
    call.add_argument("--args-file", help="UTF-8 JSON file; avoids shell escaping for large specs")
    for name in ("_worker", "_execute"):
        internal = commands.add_parser(name)
        internal.add_argument("job_id")
        if name == "_worker":
            internal.add_argument("--lease-fd", type=int)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, room.handle_termination)
    try:
        if args.command == "transcript-audit":
            # Read-only by contract: the home is only resolved (never created) and the registry is opened read-only by
            # the audit itself, so a mistyped --home provisions nothing and reports the same error as transcript_audit.py.
            import transcript_audit
            try:
                result = transcript_audit.audit(controller_home(args.home), args.room, args.handoff, args.attempt)
            except transcript_audit.AuditError as exc:
                raise room.RoomError(str(exc)) from exc
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["complete"] else 1  # the documented contract shared with transcript_audit.py: 1 means incomplete
        service = Service(args.home)
        if args.command == "setup":
            result = service.setup(args.claude_bin, args.qwen_config, args.deepseek_config, args.delegate_provider)
        elif args.command == "doctor":
            result = service.room_doctor()
        elif args.command == "call":
            result = service.call(args.tool, json.loads(Path(args.args_file).read_text() if args.args_file else args.args))
        elif args.command == "_worker":
            service.worker(args.job_id, args.lease_fd)
            return 0
        else:
            try:
                result = service.execute_job(args.job_id)
            except Exception as exc:
                # The operation owns model-delivery classification. An exception which
                # escapes it has no verified terminal outcome and cannot be retried.
                result = {"status": "uncertain", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (room.RoomError, OSError, ValueError, TypeError, RecursionError, ImportError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
