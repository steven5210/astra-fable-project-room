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
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import room

ROOT = Path(__file__).resolve().parent
MODEL = "claude-fable-5-1"
ACTIVE = ("queued", "running")
MAX_TEXT = 2_000_000


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


class Service:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("PROJECT_ROOM_HOME") or Path.home() / ".project-room").expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            """)

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
        except (OSError, ValueError) as exc:
            raise room.RoomError("Project Room is not configured; run project_room.py setup first") from exc
        if value.get("model") != MODEL:
            raise room.RoomError(f"This installation requires the configured Fable model {MODEL}")
        return value

    def setup(self, claude_bin=None, qwen_config=None):
        with room.lock_room(self.home / "setup-lock"):
            target = self.home / "config.json"
            prior = json.loads(target.read_text()) if target.exists() else {}
            executable = str(Path(claude_bin).expanduser().resolve()) if claude_bin else prior.get("claude_bin") or discover_claude()
            if not Path(executable).is_file() or not os.access(executable, os.X_OK):
                raise room.RoomError("claude_bin must name an executable file")
            qwen = str(Path(qwen_config).expanduser().resolve()) if qwen_config else prior.get("qwen_config")
            if qwen:
                from qwen_guard import load_server
                load_server(qwen)  # Validate configuration only; never launch upstream here.
            config = {"version": 1, "claude_bin": executable, "model": MODEL,
                      "claude_config_dir": prior.get("claude_config_dir") or str(Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser().resolve()),
                      "claude_config_dir_override": prior.get("claude_config_dir_override", os.environ.get("CLAUDE_CONFIG_DIR")),
                      "qwen_config": qwen, "review_timeout_seconds": prior.get("review_timeout_seconds", 1800),
                      "implementation_timeout_seconds": prior.get("implementation_timeout_seconds", 3600)}
            atomic_json(target, config)
            return {"configured": True, "config_path": str(target), "model": MODEL,
                    "qwen_configured": bool(qwen), "existing_rooms_unchanged": True}

    def room_doctor(self):
        try:
            config = self.settings()
        except room.RoomError as exc:
            return {"configured": False, "error": str(exc)}
        result = {"configured": True, "model": config["model"], "home": str(self.home),
                  "claude_executable_exists": Path(config["claude_bin"]).is_file(),
                  "qwen_configured": bool(config.get("qwen_config")),
                  "qwen_inference_verified": False}
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
        mcp = {"mcpServers": {}}
        if settings.get("qwen_config"):
            # The upstream config remains private; never embed its env/credential values.
            shutil.copyfile(ROOT / "qwen_guard.py", profiles / "qwen_guard.py")
            mcp["mcpServers"]["qwen-local"] = {"command": sys.executable, "args": [str(profiles / "qwen_guard.py"), "--config", settings["qwen_config"]]}
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
                                         "--strict-mcp-config", "--mcp-config", str(profiles / "implementation-mcp.json")]}
        atomic_json(profiles / "implementation.json", implementation)
        atomic_json(root / "settings.json", settings)

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

    def _guard_idle(self, room_id):
        with self.db() as db:
            identifiers = [row[0] for row in db.execute("SELECT id FROM jobs WHERE room_id=? AND status IN ('queued','running')", (room_id,))]
        for identifier in identifiers:
            self._refresh(identifier)
        with self.db() as db:
            active = db.execute("SELECT id,status FROM jobs WHERE room_id=? AND status IN ('queued','running','uncertain') LIMIT 1", (room_id,)).fetchone()
        if active:
            raise room.RoomError(f"Room is blocked by {active['status']} job {active['id']}; inspect its status, do not resubmit")

    def room_status(self, room_id):
        entry, root, review = self.paths(room_id)
        with self.db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE room_id=? ORDER BY created_at", (room_id,))]
        jobs = [self._refresh(identifier) for identifier in ids]
        with self.db() as db:
            issues = [dict(r) for r in db.execute("SELECT * FROM issues WHERE room_id=? ORDER BY rowid", (room_id,))]
            handoffs = [dict(r) for r in db.execute("SELECT * FROM handoffs WHERE room_id=?", (room_id,))]
        core = room.status_report(review)
        return {"room": entry, "review": core, "issues": issues, "jobs": jobs, "handoffs": handoffs,
                "enhancements": self._enhancements(room_id),
                "ready_for_handoff": core["agreement"] and not any(i["disposition"] == "open" for i in issues)
                and not any(j["status"] in (*ACTIVE, "uncertain") for j in jobs)}

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

    def _refresh(self, job_id):
        value = self._job(job_id)
        if value["status"] in ACTIVE:
            with (self._job_path(job_id) / "worker.lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return value
                # A startup grace interval prevents a second caller racing process startup.
                age = time.time() - room.parse_timestamp(value["created_at"]).timestamp()
                if age > 10:
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
            with self.db() as db:
                old = db.execute("SELECT id,payload FROM jobs WHERE room_id=? AND kind=? AND request_key=?", (room_id, kind, request_id)).fetchone()
            if old:
                if old["payload"] != encoded:
                    raise room.RoomError("request_id was already used with different content")
                return {**self._refresh(old["id"]), "duplicate": True}
            self._guard_idle(room_id)
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
            if kind == "implementation":
                self._ensure_handoff_current(room_id, payload["handoff_id"])
            identifier = uuid.uuid4().hex
            path = self._job_path(identifier)
            path.mkdir(parents=True, mode=0o700)
            with self.db() as db:
                db.execute("INSERT INTO jobs(id,room_id,kind,request_key,payload,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                           (identifier, room_id, kind, request_id, encoded, room.now()))
            try:
                with (path / "worker.log").open("wb") as output:
                    process = subprocess.Popen([sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "_worker", identifier],
                                               stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
                                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                with self.db() as db:
                    db.execute("UPDATE jobs SET pid=? WHERE id=?", (process.pid, identifier))
                # Reap children while this MCP lives; a disconnected client still leaves
                # the detached worker running, with no wait thread keeping the host alive.
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError as exc:
                with self.db() as db:
                    db.execute("UPDATE jobs SET status='failed',finished_at=?,error=? WHERE id=?", (room.now(), f"Worker did not start: {exc}", identifier))
            return self._job(identifier)

    def room_review_submit(self, room_id, revision, message, request_id):
        positive_revision(revision)
        text_value(message, "message")
        _, root, review = self.paths(room_id)
        with contextlib.closing(room.connect(review)) as db:
            spec = room.get_spec(db, review, revision)
            session = room.meta(db, "session_id")
        settings = json.loads((root / "settings.json").read_text())
        return self._submit(room_id, "review", request_id, {"revision": revision, "message": message,
                            "spec_sha256": spec["sha256"], "session_transcript": transcript_path(settings["claude_config_dir"], review, session)})

    def room_job_status(self, job_id, wait_seconds=0):
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 45:
            raise room.RoomError("wait_seconds must be finite and between 0 and 45")
        deadline = time.monotonic() + wait_seconds
        while True:
            job = self._refresh(job_id)
            if job["status"] not in ACTIVE or time.monotonic() >= deadline:
                return job
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
            settings = json.loads((root / "settings.json").read_text())
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

    def room_implementation_submit(self, room_id, handoff_id, request_id):
        path = self._handoff_path(room_id, handoff_id)
        return self._submit(room_id, "implementation", request_id, {"handoff_id": handoff_id, "handoff_path": str(path)})

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
        settings = json.loads((root / "settings.json").read_text())
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
            return implementation.run_implementation(Path(payload["handoff_path"]))
        raise room.RoomError("Unknown job kind")

    def worker(self, job_id):
        path = self._job_path(job_id)
        with room.lock_room(path):
            with (path / "worker.lock").open("a") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                job = self._job(job_id)
                if job["status"] != "queued":
                    return
                with self.db() as db:
                    db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (room.now(), job_id))
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
                if job["kind"] == "implementation" and isinstance(result, dict):
                    report = result.get("report", {})
                    if report.get("outcome") == "scope_change":
                        _, feature_root, review_room = self.paths(job["room_id"])
                        note = path / "scope-change.md"
                        note.write_text("Fable returned this implementation discovery to Astra for requirements review:\n"
                                        + room.canonical({"handoff_id": job["payload"]["handoff_id"], "report": report}), encoding="utf-8")
                        room.record(SimpleNamespace(sender="astra", kind="message", revision=result["spec_revision"], file=str(note)), review_room)
                with self.db() as db:
                    db.execute("UPDATE jobs SET status=?,finished_at=?,result=?,error=? WHERE id=?", (status, room.now(), room.canonical(result) if result is not None else None, error, job_id))
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
        return getattr(self, name)(**arguments)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer", "minimum": 1}
R = {"room_id": S}
TOOL_SCHEMAS = {
    "room_doctor": ("Check local setup and Claude subscription sign-in; does not call a model or Qwen inference.", schema({})),
    "room_open": ("Open or create the persistent room for this exact project directory and feature. Reuses history/session; does not start models.", schema({"project_path": S, "feature": S})),
    "room_list": ("Find existing project/feature rooms before creating another.", schema({"project_path": S}, [])),
    "room_status": ("Read review agreement, unresolved findings, jobs, and implementation handoffs.", schema(R)),
    "room_spec_put": ("Register immutable exact UTF-8 spec revision with repository context and concrete verification.", schema({**R, "revision": I, "content": S})),
    "room_record": ("Record Astra/user discussion or Astra approval of the current exact spec. Does not authorize implementation.", schema({**R, "sender": {"type": "string", "enum": ["astra", "user"]}, "kind": {"type": "string", "enum": ["message", "approval"]}, "revision": I, "content": S})),
    "room_review_submit": ("Start one Fable review asynchronously. Save returned job id; identical request_id/payload reuses the job, never resubmit to poll.", schema({**R, "revision": I, "message": S, "request_id": S})),
    "room_job_status": ("Read or wait up to 45 seconds on a saved job; repeat bounded waits while working. Returns saved terminal evidence.", schema({"job_id": S, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 45}}, ["job_id"])),
    "room_job_cancel": ("Request cancellation of the owning worker. Started operations may remain uncertain; inspect status before continuing.", schema({"job_id": S})),
    "room_job_recover": ("After diagnosis, audit only an exact zero-usage local login failure as not sent. Preserves original failure; runs no model. Then use a new request_id in the same room after fixing setup. Cannot recover unknown delivery.", schema({"job_id": S, "diagnosis": S})),
    "room_history": ("Read room discussion, decisions, backlog, and implementation events. Contains no private model thinking.", schema(R)),
    "room_issue_dispose": ("Record a rationale for every Fable finding: address in a revision, reject with evidence, or defer optional enhancement to backlog.", schema({**R, "issue_id": S, "disposition": {"type": "string", "enum": ["addressed", "rejected", "deferred"]}, "rationale": S, "revision": I})),
    "room_backlog_add": ("Record or update an optional enhancement for the user's opinion, preserving its stable proposal_id and event history. issue_url records an issue Astra separately filed; this tool never publishes. New proposals and changed content/rationale default to pending. Any explicit approved/declined/deferred decision requires actual user decision evidence in decision_rationale, never Fable's suggestion or technical disposition. Approval here does not change the agreed implementation spec.", schema({**R, "content": S, "rationale": S, "issue_url": S, "proposal_id": S,
                                "user_decision": {"type": "string", "enum": ["pending", "approved", "declined", "deferred"]},
                                "decision_rationale": S}, ["room_id", "content", "rationale"])),
    "room_decision_record": ("After the bounded review round is exhausted, record the user's actual decision on the unresolved product tradeoff to allow the next bounded round. Never invent a decision or use this to bypass unknown/failed delivery.", schema({**R, "revision": I, "decision": S})),
    "room_handoff": ("Prepare Fable implementation from exact agreement, resolved findings, original user authorization, and nonempty verification argv gates. Creates isolated git worktree.", schema({**R, "revision": I, "authorization": S, "gates": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 1, "items": S}}})),
    "room_implementation_submit": ("Start/resume authorized Fable implementation asynchronously. Delegates follow fixed Qwen/Sonnet/Opus policy; Astra checks product outcome after evidence.", schema({**R, "handoff_id": S, "request_id": S})),
    "room_implementation_review": ("Record Astra's independent product-outcome verdict against the exact verified candidate. Engineering/delegate verdicts remain Fable's responsibility.", schema({**R, "handoff_id": S, "accepted": {"type": "boolean"}, "review": S})),
    "room_implementation_revise": ("Request a diagnosed correction within the same agreed spec, then submit with a new request_id. Unknown delivery cannot be retried.", schema({**R, "handoff_id": S, "review": S})),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--claude-bin")
    setup.add_argument("--qwen-config")
    commands.add_parser("doctor")
    call = commands.add_parser("call")
    call.add_argument("tool", choices=sorted(TOOL_SCHEMAS))
    call.add_argument("--args", default="{}", help="JSON argument object")
    call.add_argument("--args-file", help="UTF-8 JSON file; avoids shell escaping for large specs")
    for name in ("_worker", "_execute"):
        commands.add_parser(name).add_argument("job_id")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, room.handle_termination)
    try:
        service = Service(args.home)
        if args.command == "setup":
            result = service.setup(args.claude_bin, args.qwen_config)
        elif args.command == "doctor":
            result = service.room_doctor()
        elif args.command == "call":
            result = service.call(args.tool, json.loads(Path(args.args_file).read_text() if args.args_file else args.args))
        elif args.command == "_worker":
            service.worker(args.job_id)
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
    except (room.RoomError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
