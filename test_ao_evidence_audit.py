"""Synthetic offline fixtures for the #58 evidence Read audit: real CLI/owner/file boundaries, no model or network.

Expected semantic facts come from the independently written literal fixtures; this module never
computes an expectation with the implementation under test. No real home, configuration,
transcript, evidence target or external URL is read or opened. The AO request session id is an
AO identifier and is deliberately distinct from the canonical native UUID.
"""

import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ao_evidence_audit
import ao_evidence_audit_io
import ao_evidence_audit_native


ROOT = Path(__file__).resolve().parent
ORACLE_PATH = ROOT / "tests" / "fixtures" / "evidence_reads" / "CASES.json"
ORACLE_SHA256 = "b338c9a73c4937eb1f0a4129ab0d113bc275963deb15cff12d4cda36582645d3"
HISTORICAL_PATH = ROOT / "tests" / "fixtures" / "evidence_reads" / "historical-ACCEPTANCE-CASES.json"
HISTORICAL_SHA256 = "957aebcda65daf1e5aace9d96836fb5808a1562c1832fa1a471857d0bee46cf7"
AO_SESSION_ID = "ao-session-58-abcd"
NATIVE_UUID = "11111111-1111-4111-8111-111111111111"
CREATED_AT = 1767225610.0
OBSERVED_AT = 1767225670.0
LIMITATIONS = ["other_tool_access_not_counted", "lexical_paths_only", "reported_results_not_full_retrieval",
               "observed_counts_not_acceptance", "not_billing_or_quota", "bounded_time_correlation"]
TOP_KEYS = {"version", "coverage", "request_sha256", "owner_sha256", "sources", "parent", "children",
            "child_coverage", "reasons", "redundant_read_verdict", "limitations"}
COUNT_KEYS = ("read_requests", "evidence_root_requests", "outside_root_requests", "unclassified_paths",
              "reported_non_error_results", "reported_error_results", "unresolved_read_results",
              "duplicate_records", "duplicate_tool_observations", "repeated_identical_inputs", "distinct_slices",
              "unsupported_tool_observations")


def canon(value):
    """Independent canonical encoding written from the documented existing digest semantics."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def completed_result(agent_id, prompt):
    return {"status": "completed", "agentId": agent_id, "prompt": prompt,
            "content": [{"type": "text", "text": "Finished."}], "totalToolUseCount": 1, "totalDurationMs": 1,
            "totalTokens": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": None,
                      "cache_read_input_tokens": None, "server_tool_use": None, "service_tier": None,
                      "cache_creation": None}}


def substitute(value, mapping):
    if isinstance(value, str):
        for source, target in mapping.items():
            if value == source or value.startswith(source + "/"):
                return target + value[len(source):]
        return value
    if isinstance(value, list):
        return [substitute(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, mapping) for key, item in value.items()}
    return value


class AuditFixture:
    def setUp(self):
        self.temporary = None
        self.reset()

    def tearDown(self):
        if self.temporary is not None:
            self.temporary.cleanup()

    def reset(self):
        if self.temporary is not None:
            self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.room = "room-58"
        self.request_id = "audit-request-1"
        self.workspace = self.base / "workspace"
        self.evidence = self.base / "evidence"
        self.other = self.base / "other"
        self.private = self.base / "private-project"
        self.config_root = self.base / "claude-config"
        self.project_dir = self.config_root / "projects" / str(self.workspace).replace("/", "-")
        self.subagents = self.project_dir / NATIVE_UUID / "subagents"
        self.env_home = self.base / "env-home"
        for path in (self.home / "ao" / "rooms" / self.room, self.workspace, self.evidence, self.other, self.private,
                     self.project_dir, self.env_home):
            path.mkdir(parents=True, exist_ok=True)
        self.delegate = {"provider": "deepseek", "inventory": {"files": {}}, "files": {}, "policy_path": "p"}
        self.mapping = {"/fixture/workspace": str(self.workspace), "/fixture/evidence": str(self.evidence),
                        "/fixture/other": str(self.other), "/fixture/private-project": str(self.private)}

    # ---- fixture construction -------------------------------------------------------------------

    def parent_record(self, kind, record_id, timestamp, content, **extra):
        value = {"type": kind, "sessionId": NATIVE_UUID, "cwd": str(self.workspace), "uuid": record_id,
                 "timestamp": timestamp, "isSidechain": False,
                 "message": {"role": kind, "content": content}}
        value.update(extra)
        return value

    def human(self, uuid="human-1", timestamp="2026-01-01T00:00:10+00:00", text="Continue."):
        return self.parent_record("user", uuid, timestamp, text, origin={"kind": "human"})

    def assistant_tools(self, uuid, timestamp, blocks, **extra):
        return self.parent_record("assistant", uuid, timestamp, blocks, **extra)

    def user_results(self, uuid, timestamp, blocks, **extra):
        return self.parent_record("user", uuid, timestamp, blocks, **extra)

    def child_record(self, kind, uuid, timestamp, content, agent="a1", **extra):
        value = {"type": kind, "sessionId": NATIVE_UUID, "cwd": str(self.workspace), "uuid": uuid,
                 "timestamp": timestamp, "isSidechain": True, "agentId": agent,
                 "message": {"role": kind, "content": content}}
        value.update(extra)
        return value

    def target(self, name="a.txt"):
        return str(self.evidence / name)

    def write_transcript(self, records):
        path = self.project_dir / (NATIVE_UUID + ".jsonl")
        self.subagents.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
        self.transcript = path
        return path

    def write_child(self, agent_id, records):
        self.subagents.mkdir(parents=True, exist_ok=True)
        path = self.subagents / ("agent-" + agent_id + ".jsonl")
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
        return path

    def build(self, request_text="Continue.", created_at=CREATED_AT, observed_at=OBSERVED_AT,
              turn_state="completed", completed_at="2026-01-01T00:00:40+00:00", owner_activity="idle", request_overrides=None,
              receipt_overrides=None, preparation_overrides=None, delegate=None, owner_overrides=None,
              harness="claude-code", model="claude-fable-5-1", effort="max", routing_version=2,
              preparation_relative="preparation.json"):
        turn = {"id": "turn-1", "providerTurnId": "provider-turn-1", "state": turn_state}
        if completed_at is not None:
            turn["completedAt"] = completed_at
        receipt = {"settings": {"model": model, "reasoningEffort": effort}, "turn": turn,
                   "messages": [{"role": "user", "text": request_text, "turnId": "turn-1"},
                                {"role": "assistant", "text": "Working on it.", "turnId": "turn-1"}],
                   "policy": "x" * 1024, "observed_at": observed_at}
        receipt.update(receipt_overrides or {})
        payload = {key: value for key, value in receipt.items() if key != "observed_at"}
        receipt_path = "receipts/%s/%s.json" % (self.request_id, canon(payload))
        room = self.home / "ao" / "rooms" / self.room
        (room / "receipts" / self.request_id).mkdir(parents=True, exist_ok=True)
        (room / receipt_path).write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        delegate = delegate if delegate is not None else self.delegate
        prepared = {"room_id": self.room, "worktree": str(self.workspace), "provider": "deepseek",
                    "delegate_sha256": canon(delegate),
                    "routing": {"version": routing_version, "claude_config_dir": str(self.config_root),
                                "files": {}, "guard_sha256": canon({"guard": 1})}}
        prepared.update(preparation_overrides or {})
        (room / preparation_relative).parent.mkdir(parents=True, exist_ok=True)
        (room / preparation_relative).write_text(json.dumps(prepared, indent=2, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
        request = {"request_id": self.request_id, "role": "engineer", "harness": harness, "model": model,
                   "reasoning_effort": effort, "session_id": AO_SESSION_ID, "state": "completed",
                   "conversation_id": "conversation-1", "branch_id": "branch-1",
                   "turn_id": "turn-1", "provider_turn_id": "provider-turn-1", "created_at": created_at,
                   "text": request_text, "text_sha256": sha(request_text),
                   "baseline": {"conversation_id": "conversation-1", "branch_id": "branch-1",
                                "turn_ids": ["turn-0"]},
                   "receipt": receipt_path, "receipt_sha256": canon(receipt)}
        request.update(request_overrides or {})
        state = {"room_id": self.room, "ao_project_id": "project-1", "delegate": delegate,
                 "preparation": preparation_relative, "preparation_sha256": canon(prepared),
                 "requests": {self.request_id: request}}
        (room / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.state = state
        self.request = request
        self.receipt = receipt
        self.prepared = prepared
        self.delegate = delegate
        self.build_database(**(owner_overrides or {}), activity=owner_activity)

    def build_database(self, harness="claude-code", activity="idle", workspace=None, conversation="conversation-1",
                       branch="branch-1", project="project-1", generation="generation-before",
                       provider_uuid=NATIVE_UUID, broken=None):
        self.database = self.base / "ao.db"
        if self.database.exists():
            self.database.unlink()
        with sqlite3.connect(self.database) as db:
            db.executescript("""CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);""")
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (AO_SESSION_ID, project, harness, "chat", 0, activity, str(workspace or self.workspace),
                        provider_uuid, generation))
            db.execute("INSERT INTO conversations VALUES (?,?,?)", (conversation, AO_SESSION_ID, branch))
            db.execute("INSERT INTO conversation_branches VALUES (?,?,?,?,?,?)",
                       (branch, conversation, provider_uuid, AO_SESSION_ID, "native", 0))
        self.owner_row = {"id": AO_SESSION_ID, "project_id": project, "harness": harness, "session_mode": "chat",
                          "is_terminated": 0, "activity_state": activity,
                          "workspace_path": str(workspace or self.workspace),
                          "provider_conversation_id": provider_uuid, "controller_generation": generation,
                          "ao_conversation_id": conversation, "active_branch_id": branch,
                          "branch_provider_conversation_id": provider_uuid, "branch_session_id": AO_SESSION_ID,
                          "strategy": "native", "replay_truncated": 0}
        if broken:
            with sqlite3.connect(self.database) as db:
                db.execute("UPDATE sessions SET " + broken)
            self.owner_row[broken.split("=")[0].strip()] = broken.split("=", 1)[1].strip().strip("'")

    def rewrite_receipt(self, receipt):
        room = self.home / "ao" / "rooms" / self.room
        payload = {key: value for key, value in receipt.items() if key != "observed_at"}
        relative = "receipts/%s/%s.json" % (self.request_id, canon(payload))
        (room / "receipts" / self.request_id).mkdir(parents=True, exist_ok=True)
        (room / relative).write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.request["receipt"] = relative
        self.request["receipt_sha256"] = canon(receipt)
        self.write_state()
        return relative

    def write_state(self, state=None):
        room = self.home / "ao" / "rooms" / self.room
        (room / "state.json").write_text(json.dumps(state or self.state, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")

    # ---- running --------------------------------------------------------------------------------

    def environment(self):
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(self.env_home)}

    def command(self, home=None, global_home=False):
        home = str(self.home if home is None else home)
        tail = ["--room", self.room, "--request", self.request_id, "--ao-database", str(self.database),
                "--evidence-root", str(self.evidence)]
        if global_home:
            return [sys.executable, str(ROOT / "project_room.py"), "--home", home, "ao-evidence-read-audit"] + tail
        return [sys.executable, str(ROOT / "project_room.py"), "ao-evidence-read-audit", "--home", home] + tail

    def run_cli(self, home=None, global_home=False):
        return subprocess.run(self.command(home, global_home), capture_output=True, text=True, timeout=300,
                              env=self.environment())

    def run_command(self, tail):
        return subprocess.run([sys.executable, str(ROOT / "project_room.py")] + tail, capture_output=True, text=True,
                              timeout=300, env=self.environment())

    def audit_in_process(self):
        return ao_evidence_audit.audit(self.home, self.room, self.request_id, self.database, self.evidence)

    def report(self, home=None, global_home=False):
        process = self.run_cli(home, global_home)
        self.assertIn(process.returncode, (0, 1), process.stderr)
        self.assertEqual(process.stderr, "")
        report = json.loads(process.stdout)
        self.assertEqual(set(report), TOP_KEYS)
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["limitations"], LIMITATIONS)
        self.assertEqual(report["redundant_read_verdict"], "not_established")
        if report["parent"] is not None:
            self.assertEqual(set(report["parent"]),
                             {"coverage", "source_coverage", "interval_coverage", "path_coverage",
                              "result_coverage", "counts", "reasons"})
            self.assertEqual(tuple(report["parent"]["counts"]), COUNT_KEYS)
        for entry in report["children"]:
            self.assertEqual(set(entry), {"actor_sha256", "observation"})
        return report

    def owner_sha256(self):
        return canon({"native_owner": self.owner_row, "preparation_sha256": canon(self.prepared)})

    def expect_actor(self, report, agent_id):
        digest = canon({"owner_sha256": report["owner_sha256"], "kind": "child", "agent_id": agent_id})
        entries = [entry for entry in report["children"] if entry["actor_sha256"] == digest]
        self.assertEqual(len(entries), 1)
        return entries[0]

    def private_literals(self, extra=()):
        literals = [str(self.base), str(self.home), str(self.workspace), str(self.evidence), str(self.config_root),
                    str(self.project_dir), str(self.subagents), str(self.database), self.request_id, AO_SESSION_ID,
                    NATIVE_UUID]
        literals.extend(extra)
        return literals

    def snapshot(self, root):
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(root.rglob("*")) if path.is_file()}

    # ---- the nineteen literal oracle cases ------------------------------------------------------

    def check_case(self, case):
        self.write_transcript(substitute(case["parent_records"], self.mapping))
        for agent_id, child_records in (case.get("child_records") or {}).items():
            if child_records:
                self.write_child(agent_id, substitute(child_records, self.mapping))
        observed = OBSERVED_AT
        turn_state = "completed"
        completed = None
        activity = "idle"
        override = case.get("terminal_receipt_override")
        if override:
            turn_state = override["turn_state"]
            completed = iso(CREATED_AT + override["completed_seconds"])
            observed = CREATED_AT + override["observed_seconds"]
            activity = override["owner_activity"]
        self.build(observed_at=observed, turn_state=turn_state, completed_at=completed, owner_activity=activity)
        report = self.report()
        expected = case["expected"]
        self.assertEqual(report["coverage"], expected["coverage"])
        self.assertEqual(report["request_sha256"], sha(self.request_id))
        self.assertEqual(report["owner_sha256"], self.owner_sha256())
        if "parent_counts" in expected:
            self.assertEqual(report["parent"]["counts"], expected["parent_counts"])
        if "children" in expected:
            self.assertEqual(len(report["children"]), expected["children"])
        if "child_coverage" in expected:
            self.assertEqual(report["child_coverage"], expected["child_coverage"])
        for agent_id in expected.get("child_observations_null", ()):
            self.assertIsNone(self.expect_actor(report, agent_id)["observation"])
        for agent_id, counts in (expected.get("child_counts") or {}).items():
            observation = self.expect_actor(report, agent_id)["observation"]
            self.assertIsNotNone(observation)
            self.assertEqual(observation["counts"], counts)
        for reason in expected.get("reasons_include", ()):
            self.assertIn(reason, report["reasons"])
        for limitation in expected.get("limitations_include", ()):
            self.assertIn(limitation, report["limitations"])
        if expected.get("no_authorized_child_files"):
            self.assertEqual(report["children"], [])
        if expected.get("no_complete_child_interval"):
            self.assertNotEqual(report["child_coverage"], "complete")
            for entry in report["children"]:
                observation = entry["observation"]
                self.assertTrue(observation is None or observation["interval_coverage"] != "complete")
        if expected.get("no_terminal_fallback"):
            self.assertNotEqual(report["parent"]["interval_coverage"], "complete")
        if "no_attribution_of_tool_ids" in expected:
            counts = report["parent"]["counts"]
            self.assertTrue(counts is None or counts["read_requests"] in (None, 0))
        if "redundant_read_verdict" in expected:
            self.assertEqual(report["redundant_read_verdict"], expected["redundant_read_verdict"])
        serialized = json.dumps(report)
        for sentinel in list(case.get("private_sentinels", ())) + self.private_literals(
                ["human-1", "human-2", "read-1", "read-2", "read-copy", "read-conflict", "too-late-read",
                 "launch-1", "launch-result-1", "child-read-1", "child-result-1", "relative-read", "outside-read",
                 "bash-record", "INVALID_BOUNDARY_SENTINEL", "/untrusted", "agent-a1", "agent-a2"]):
            self.assertNotIn(sentinel, serialized)
        # Hex digests may contain short ID substrings by chance; reject raw JSON string values.
        self.assertNotIn('"a1"', serialized)
        self.assertNotIn('"a2"', serialized)


class AuditTests(AuditFixture, unittest.TestCase):
    def test_cli_keeps_symlinked_home_visible_to_the_audit(self):
        self.write_transcript([self.human()])
        self.build()
        link = self.base / "linked-home"
        link.symlink_to(self.home, target_is_directory=True)
        before = self.snapshot(self.home)
        process = self.run_cli(home=link)
        self.assertEqual(process.returncode, 1, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["coverage"], "unavailable")
        self.assertIn("request_integrity", report["reasons"])
        self.assertEqual(self.snapshot(self.home), before)

    def test_independent_literal_cases_through_the_real_cli(self):
        self.assertTrue(ORACLE_PATH.exists(), "the 19 independent literal oracle cases are required")
        self.assertEqual(hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest(), ORACLE_SHA256)
        cases = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(cases["schema_version"], 1)
        self.assertEqual(len(cases["cases"]), 19)
        for case in cases["cases"]:
            self.reset()
            with self.subTest(case=case["name"]):
                self.check_case(case)

    def test_historical_acceptance_oracle_is_retained(self):
        self.assertTrue(HISTORICAL_PATH.is_file(), "the historical literal oracle remains packaged")
        self.assertEqual(hashlib.sha256(HISTORICAL_PATH.read_bytes()).hexdigest(), HISTORICAL_SHA256)

    # ---- CLI/read-only boundaries ----------------------------------------------------------------

    def test_missing_home_and_success_are_read_only(self):
        self.write_transcript([self.human()])
        self.build()
        missing = self.base / "absent-home"
        process = self.run_cli(home=missing)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["reasons"], ["request_unbound"])
        self.assertFalse(missing.exists())
        before = self.snapshot(self.home)
        self.assertFalse((self.home / "ao" / ".lock").exists())
        self.assertEqual(self.report()["coverage"], "complete")
        self.assertEqual(self.snapshot(self.home), before)
        self.assertFalse((self.home / "ao" / ".lock").exists())

    def test_cli_accepts_global_home_and_refuses_unsafe_arguments(self):
        self.write_transcript([self.human()])
        self.build()
        self.assertEqual(self.report(global_home=True)["coverage"], "complete")
        process = self.run_command(["ao-evidence-read-audit", "--home", str(self.home), "--room", self.room,
                                    "--request", "../escape", "--ao-database", str(self.database),
                                    "--evidence-root", str(self.evidence)])
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertEqual(json.loads(process.stderr), {"error": "request_invalid"})
        process = self.run_command(["ao-evidence-read-audit", "--home", str(self.home), "--room", self.room,
                                    "--request", self.request_id, "--ao-database", "relative.db",
                                    "--evidence-root", str(self.evidence)])
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stderr), {"error": "database_invalid"})

    def test_wal_owner_read_is_read_only(self):
        self.write_transcript([self.human()])
        self.build()
        writer = sqlite3.connect(self.database)
        self.addCleanup(writer.close)
        self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE sessions SET controller_generation='generation-wal'")
        writer.commit()
        self.owner_row["controller_generation"] = "generation-wal"
        # The uncommitted value must remain invisible and untouched; no sidecar byte claim is made.
        writer.execute("UPDATE sessions SET controller_generation='uncommitted-generation'")
        report = self.report()
        self.assertEqual(report["owner_sha256"], self.owner_sha256())
        self.assertEqual(writer.execute("SELECT controller_generation FROM sessions").fetchone()[0],
                         "uncommitted-generation")
        writer.rollback()
        self.assertEqual(writer.execute("SELECT controller_generation FROM sessions").fetchone()[0],
                         "generation-wal")

    def test_canonical_receipt_identity_is_not_raw_file_hashing(self):
        self.write_transcript([self.human()])
        self.build()
        room = self.home / "ao" / "rooms" / self.room
        receipt_file = room / self.request["receipt"]
        self.assertIn(b"\n  ", receipt_file.read_bytes())
        self.assertEqual(self.report()["coverage"], "complete")
        changed = json.loads(receipt_file.read_text(encoding="utf-8"))
        changed["turn"]["state"] = "failed"
        receipt_file.write_text(json.dumps(changed, indent=4) + "\n", encoding="utf-8")
        process = self.run_cli()
        self.assertEqual(process.returncode, 1)
        self.assertIn("receipt_integrity", json.loads(process.stdout)["reasons"])


if __name__ == "__main__":
    unittest.main()
