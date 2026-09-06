"""Additive job progress: pinned deadlines, exact-session metadata, privacy, and read-only reads.

Every transcript here is synthetic and generated in a temporary directory; no model,
network, or GPU is used.
"""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

import implementation
import progress
import project_room
import room
import test_project_room_mcp
from test_project_room import ProjectFixture

UTC = dt.timezone.utc
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
SENTINELS = ["THINKING_SENTINEL_9f1", "PROMPT_SENTINEL_2c4", "TEXT_SENTINEL_77a", "COMMAND_SENTINEL_rm_rf",
             "DESCRIPTION_SENTINEL_tests_passed", "RESULT_SENTINEL_stack", "ERROR_SENTINEL_payload",
             "FILE_PATH_SENTINEL_secret_txt", "sk-ant-api03-CREDENTIAL_SENTINEL", "UNRELATED_CWD_SENTINEL",
             "MALFORMED_SENTINEL_line", "MODEL_SENTINEL_slug", "toolu_RAWID_SENTINEL", "SUMMARY_SENTINEL_title"]
IMPLEMENTATION_FAKE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, re, sys, time
root = pathlib.Path(__file__).parent
argv = sys.argv[1:]
if argv == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)
prompt = sys.stdin.read()
packet = json.loads(prompt.split("IMPLEMENTATION PACKET (JSON):\n", 1)[1])
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
control = json.loads((root / "implementation-control.json").read_text()) if (root / "implementation-control.json").exists() else {}
with (root / "implementation-calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": argv, "session": session, "cwd": os.getcwd(), "pid": os.getpid()}) + "\n")
if control.get("wait"):
    while not (root / "release-model").exists():
        time.sleep(0.05)
scope = bool(control.get("scope"))
if not scope:
    pathlib.Path("feature.txt").write_text("implemented\n")
report = {"summary": "Implemented the agreed fixture feature.", "spec_revision": packet["spec_revision"],
          "spec_sha256": packet["spec_sha256"], "baseline_commit": packet["baseline_commit"],
          "implementation_complete": not scope, "outcome": "scope_change" if scope else "completed",
          "scope_change": "Needs a product decision." if scope else "", "backlog": [],
          "routing_log": [{"task": "fixture change", "tier": "fable", "requested_model": "claude-fable-5-1",
                           "actual_model": "claude-fable-5-1", "reason": "Bounded fixture work", "result": "implemented",
                           "fixes": [], "escalation": "none"}],
          "changes": ["feature.txt"], "tests_reported": ["Self-reported evidence is not independent"],
          "review_findings": [], "remaining_gaps": []}
result = {"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "modelUsage": {"claude-fable-5-1": {}}, "structured_output": report}
transcript = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd()) / (session + ".jsonl")
transcript.parent.mkdir(parents=True, exist_ok=True)
event = {"type": "assistant", "sessionId": session, "cwd": os.getcwd(), "isSidechain": False, "uuid": "final-" + str(os.getpid()),
         "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "message": {"id": "fixture-message", "model": "claude-fable-5-1", "content": [
             {"type": "thinking", "thinking": "DO_NOT_EXPOSE_PRIVATE_THINKING"},
             {"type": "tool_use", "name": "StructuredOutput", "id": "tool-fixture-final", "input": report}]}}
with transcript.open("a") as out:
    out.write(json.dumps(event) + "\n")
print(json.dumps(result))
'''


def record(kind, session, cwd, when, content=None, sidechain=False, record_uuid=None, model="claude-fable-5-1", **extra):
    value = {"type": kind, "sessionId": session, "timestamp": when.isoformat(), "uuid": record_uuid or uuid.uuid4().hex,
             "parentUuid": None, "isSidechain": sidechain, "message": {"role": kind, "content": [] if content is None else content}}
    if cwd is not None:
        value["cwd"] = cwd
    if kind == "assistant":
        value["message"]["model"] = model
    value.update(extra)
    return value


def tool_use(name, tool_id, **tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def tool_result(tool_id, text="ok"):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": text}


def write_jsonl(path, records, trailing=b"", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for value in records:
            handle.write((value if isinstance(value, bytes) else json.dumps(value).encode()) + b"\n")
        handle.write(trailing)
    if mtime is not None:
        os.utime(path, ns=(int(mtime.timestamp() * 1e9), int(mtime.timestamp() * 1e9)))


def without_observation_time(value):
    return {key: item for key, item in value.items() if key != "observed_at"}


class ProgressUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="progress-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.projects = self.root / "claude" / "projects"
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.session = str(uuid.uuid4())
        self.now = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
        self.start = self.now - dt.timedelta(minutes=10)
        self.transcript = self.projects / "encoded-worktree" / (self.session + ".jsonl")
        self.job_id = "a" * 32

    def at(self, seconds):
        return self.start + dt.timedelta(seconds=seconds)

    def parent(self, kind, when, content=None, **extra):
        return record(kind, self.session, str(self.worktree), when, content, **extra)

    def child_path(self, agent_id):
        return self.transcript.parent / self.session / "subagents" / f"agent-{agent_id}.jsonl"

    def write_child(self, agent_id, records):
        # Positive fixtures carry explicit identity. Identity-negative probes write
        # their raw records directly so missing evidence is never repaired there.
        write_jsonl(self.child_path(agent_id), [dict(value, agentId=value.get("agentId", agent_id)) for value in records], mtime=self.now)

    def child(self, kind, when, source=None, content=None, cwd="expected", agent_id=None, session=None, stop_reason=None, **extra):
        value = record(kind, session or self.session, str(self.worktree) if cwd == "expected" else cwd, when, content,
                       sidechain=True, **extra)
        if source is not None:
            value["sourceToolAssistantUUID"] = source
        if agent_id is not None:
            value["agentId"] = agent_id
        if stop_reason is not None:
            value["message"]["stop_reason"] = stop_reason
        return value

    def launched(self, tool_id, when, agent_id):
        """The parent's tool_result for a background delegate: a launch acknowledgement, not completion."""
        value = self.parent("user", when, [tool_result(tool_id, "RESULT_SENTINEL_stack")])
        value["toolUseResult"] = {"status": "async_launched", "agentId": agent_id, "prompt": "PROMPT_SENTINEL_2c4"}
        return value

    def inputs(self, **overrides):
        value = {"predicted": str(self.transcript), "projects_dir": str(self.projects), "explicit_root": None}
        value.update(overrides)
        return value

    def observe(self, now=None, **overrides):
        return progress.observe_session(self.inputs(**overrides), self.session, str(self.worktree), self.start, now or self.now)

    def job(self, status, kind="implementation", **overrides):
        value = {"id": self.job_id, "room_id": "room", "kind": kind, "request_key": "request", "payload": {}, "status": status,
                 "created_at": (self.start - dt.timedelta(seconds=30)).isoformat(),
                 "started_at": (self.start - dt.timedelta(seconds=5)).isoformat(),
                 "finished_at": None, "pid": None, "result": None, "error": None}
        value.update(overrides)
        return value

    def handoff(self, phase="running_model", **state_overrides):
        state = {"phase": phase, "started_at": self.start.isoformat(), "attempt_count": 1, "owner_job_id": self.job_id,
                 "active_stage": {"kind": "model", "index": 1, "started_at": self.start.isoformat()}, "gate_results": []}
        state.update(state_overrides)
        return {"state": state, "manifest": {"session_id": self.session, "worktree_path": str(self.worktree), "gates": [["one"], ["two"]]},
                "config": {"timeout_seconds": 3600, "gate_timeout_seconds": 300}, "config_verified": True,
                "limitations": set(), "transcript": self.inputs()}

    def review(self, status="pending", started=None, timeout=1800, **overrides):
        value = {"turn": {"status": status, "session_id": self.session, "started_at": (started or self.start).isoformat(), "finished_at": None},
                 "timeout_seconds": timeout, "expected_cwd": str(self.worktree), "transcript": self.inputs()}
        value.update(overrides)
        return value

    def test_queued_awaiting_review_and_terminal_jobs_have_frozen_elapsed_and_no_countdown(self):
        queued = progress.job_progress(self.job("queued"), self.now)
        self.assertEqual((queued["phase"], queued["elapsed_seconds"], queued["elapsed_basis"]), ("queued", 630, "job_created_at"))
        self.assertEqual((queued["deadline"], queued["deadline_unavailable_reason"]), (None, "queued"))
        self.assertEqual((queued["activity"], queued["activity_unavailable_reason"], queued["delegates"], queued["gate"]), (None, "queued", None, None))
        finished = self.at(100)
        succeeded = self.job("succeeded", finished_at=finished.isoformat(), result={"phase": "awaiting_astra_review", "attempt_count": 2, "gate_results": [{}]})
        first = progress.job_progress(succeeded, self.now)
        later = progress.job_progress(succeeded, self.now + dt.timedelta(days=3))
        self.assertEqual(first["phase"], "awaiting_review")
        self.assertEqual((first["elapsed_seconds"], first["elapsed_basis"], first["attempt"]), (105, "frozen_at_finish", 2))
        self.assertEqual((first["deadline"], first["deadline_unavailable_reason"]), (None, "awaiting_product_review"))
        self.assertEqual((first["activity"], first["activity_unavailable_reason"]), (None, "awaiting_product_review"))
        self.assertEqual(without_observation_time(first), without_observation_time(later))
        for status, result in (("failed", None), ("uncertain", {"phase": "blocked"}), ("cancelled", None), ("not_sent", None),
                               ("succeeded", {"phase": "scope_change", "attempt_count": 1})):
            with self.subTest(status=status):
                value = progress.job_progress(self.job(status, finished_at=finished.isoformat(), result=result), self.now)
                self.assertEqual((value["phase"], value["outcome"], value["deadline_unavailable_reason"]), ("terminal", status, "terminal"))
                self.assertEqual(value["elapsed_basis"], "frozen_at_finish")
        review = progress.job_progress(self.job("succeeded", kind="review", finished_at=finished.isoformat(), result={"status": "completed"}), self.now)
        self.assertEqual((review["phase"], review["attempt"]), ("terminal", None))
        unfinished = progress.job_progress(self.job("failed"), self.now)
        self.assertEqual((unfinished["elapsed_seconds"], unfinished["elapsed_basis"]), (None, "unavailable"))
        for value in (queued, first, review):
            self.assertEqual(value["schema_version"], 1)
            self.assertRegex(value["observed_at"], STAMP)
            self.assertEqual(value["limitations"], [])

    def test_pinned_model_and_gate_deadlines_count_down_clamp_and_expire_without_changing_outcome(self):
        model = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        self.assertEqual((model["phase"], model["phase_detail"], model["attempt"], model["outcome"]), ("model", None, 1, "running"))
        self.assertEqual((model["elapsed_seconds"], model["elapsed_basis"]), (605, "job_started_at"))
        self.assertEqual(model["deadline"], {"scope": "model_invocation", "basis": "pinned_handoff_model_timeout",
                                             "started_at": "2026-09-05T11:50:00Z", "timeout_seconds": 3600,
                                             "deadline_at": "2026-09-05T12:50:00Z", "remaining_seconds": 3000, "expired": False,
                                             "meaning": "timeout_countdown_not_eta"})
        self.assertIsNone(model["deadline_unavailable_reason"])
        self.assertEqual((model["activity"], model["activity_unavailable_reason"], model["delegates"]), (None, "transcript_missing", None))
        gate_state = self.handoff("running_gates", active_stage={"kind": "gate", "index": 2, "started_at": self.at(500).isoformat()},
                                  gate_results=[{"return_code": 0}])
        gate = progress.job_progress(self.job("running"), self.now, handoff=gate_state)
        self.assertEqual((gate["phase"], gate["gate"]), ("gate", {"index": 2, "count": 2}))
        self.assertEqual((gate["deadline"]["basis"], gate["deadline"]["scope"], gate["deadline"]["timeout_seconds"]), ("pinned_handoff_gate_timeout", "gate", 300))
        self.assertEqual((gate["deadline"]["remaining_seconds"], gate["deadline"]["expired"]), (200, False))
        self.assertEqual((gate["activity"], gate["activity_unavailable_reason"], gate["delegates"]), (None, "gate_phase", None))
        overdue = progress.job_progress(self.job("running"), self.at(5000), handoff=self.handoff())
        self.assertEqual((overdue["deadline"]["remaining_seconds"], overdue["deadline"]["expired"]), (0, True))
        self.assertEqual((overdue["phase"], overdue["outcome"]), ("model", "running"))
        # A current worker between stages: the previous stage ended, the next start is not yet saved.
        for state, expected_gate in ((self.handoff("running_gates", active_stage=None), {"index": 1, "count": 2}),
                                     (self.handoff("running_gates", active_stage={"kind": "gate", "index": 1, "started_at": self.at(500).isoformat()},
                                                   gate_results=[{"return_code": 0}]), {"index": 2, "count": 2}),
                                     (self.handoff("running_gates", active_stage=None, gate_results=[{"return_code": 0}, {"return_code": 0}]), {"index": 2, "count": 2})):
            with self.subTest(gate=expected_gate):
                between = progress.job_progress(self.job("running"), self.now, handoff=state)
                self.assertEqual((between["phase"], between["gate"], between["deadline"], between["deadline_unavailable_reason"]),
                                 ("gate", expected_gate, None, "stage_transition"))
        exited = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(active_stage=None))
        self.assertEqual((exited["phase"], exited["deadline"], exited["deadline_unavailable_reason"]), ("model", None, "stage_transition"))
        for timeout in (10 ** 12, 1e300):
            with self.subTest(timeout=timeout):
                huge = self.handoff()
                huge["config"]["timeout_seconds"] = timeout
                value = progress.job_progress(self.job("running"), self.now, handoff=huge)
                self.assertEqual((value["phase"], value["deadline"], value["deadline_unavailable_reason"]), ("model", None, "pinned_timeout_unavailable"))
        for future in ((self.now + dt.timedelta(hours=1)).isoformat(), "9999-12-31T23:59:59+00:00"):
            with self.subTest(future=future):
                skewed = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(started_at=future))
                self.assertEqual((skewed["deadline"], skewed["deadline_unavailable_reason"]), (None, "clock_anomaly"))
                self.assertIn("clock_anomaly", skewed["limitations"])
                turn = progress.job_progress(self.job("running", kind="review"), self.now, review=self.review(started=progress.parse_time(future)))
                self.assertEqual((turn["deadline"], turn["deadline_unavailable_reason"]), (None, "clock_anomaly"))
        slightly_ahead = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(started_at=(self.now + dt.timedelta(microseconds=3)).isoformat()))
        self.assertEqual((slightly_ahead["deadline"], slightly_ahead["deadline_unavailable_reason"]), (None, "clock_anomaly"))
        unverified = self.handoff()
        unverified.update(config_verified=False, config={"timeout_seconds": 1})
        self.assertEqual(progress.job_progress(self.job("running"), self.now, handoff=unverified)["deadline_unavailable_reason"], "pinned_timeout_unavailable")
        review = progress.job_progress(self.job("running", kind="review"), self.now, review=self.review())
        self.assertEqual((review["phase"], review["deadline"]["basis"], review["deadline"]["remaining_seconds"]), ("model", "pinned_review_timeout", 1200))
        self.assertEqual(review["activity_unavailable_reason"], "transcript_missing")
        starting = progress.job_progress(self.job("running", kind="review"), self.now, review=self.review(turn=None))
        self.assertEqual((starting["phase"], starting["deadline_unavailable_reason"], starting["activity_unavailable_reason"]), ("starting", "starting", "starting"))
        finalizing = progress.job_progress(self.job("running", kind="review"), self.now, review=self.review(status="completed"))
        self.assertEqual((finalizing["phase"], finalizing["deadline"]), ("finalizing", None))
        unreadable = progress.job_progress(self.job("running", kind="review"), self.now, review={"error": "unreadable"})
        self.assertEqual((unreadable["phase"], unreadable["deadline_unavailable_reason"]), ("unknown", "state_unreadable"))
        self.assertIn("review_state_unreadable", unreadable["limitations"])
        for timeout in (0, -5, float("nan"), float("inf"), True, "60", None):
            with self.subTest(timeout=timeout):
                value = progress.job_progress(self.job("running", kind="review"), self.now, review=self.review(timeout=timeout))
                self.assertEqual((value["deadline"], value["deadline_unavailable_reason"]), (None, "pinned_timeout_unavailable"))

    def test_legacy_worker_foreign_owner_and_stale_state_are_never_adopted(self):
        legacy_gate = self.handoff("running_gates", gate_results=[{"return_code": 0}])
        for key in ("owner_job_id", "active_stage"):
            legacy_gate["state"].pop(key)
        value = progress.job_progress(self.job("running"), self.now, handoff=legacy_gate)
        self.assertEqual((value["phase"], value["gate"]), ("gate", {"index": 2, "count": 2}))
        cli = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(owner_job_id="cli"))
        self.assertEqual((cli["phase"], cli["deadline_unavailable_reason"]), ("starting", "starting"))
        self.assertEqual((value["deadline"], value["deadline_unavailable_reason"]), (None, "gate_start_unavailable_legacy_worker"))
        self.assertIn("attempt_binding_inferred", value["limitations"])
        legacy_model = self.handoff()
        for key in ("owner_job_id", "active_stage"):
            legacy_model["state"].pop(key)
        value = progress.job_progress(self.job("running"), self.now, handoff=legacy_model)
        self.assertEqual((value["phase"], value["deadline"]["basis"], value["deadline"]["remaining_seconds"]), ("model", "pinned_handoff_model_timeout", 3000))
        self.assertIn("attempt_binding_inferred", value["limitations"])
        foreign = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(owner_job_id="b" * 32))
        self.assertEqual((foreign["phase"], foreign["attempt"], foreign["deadline_unavailable_reason"], foreign["gate"]), ("starting", None, "starting", None))
        stale_start = (self.start - dt.timedelta(hours=1)).isoformat()
        for phase in ("awaiting_astra_review", "correction_pending", "running_gates", "blocked", "accepted"):
            with self.subTest(phase=phase):
                stale = self.handoff(phase, started_at=stale_start, gate_results=[{"return_code": 1}], attempt_count=3)
                stale["state"].pop("owner_job_id")
                stale["state"].pop("active_stage")
                value = progress.job_progress(self.job("running"), self.now, handoff=stale)
                self.assertEqual((value["phase"], value["attempt"], value["gate"], value["delegates"]), ("starting", None, None, None))
                self.assertNotIn("attempt_binding_inferred", value["limitations"])
        finalizing = progress.job_progress(self.job("running"), self.now, handoff=self.handoff("awaiting_astra_review"))
        self.assertEqual((finalizing["phase"], finalizing["deadline_unavailable_reason"]), ("finalizing", "finalizing"))
        missing = progress.job_progress(self.job("running"), self.now, handoff={"state": None})
        self.assertEqual((missing["phase"], missing["deadline_unavailable_reason"]), ("unknown", "state_unreadable"))
        self.assertIn("handoff_state_unreadable", missing["limitations"])
        self.assertEqual(progress.job_progress(self.job("running"), self.now)["phase"], "unknown")

    def test_parent_tool_correlation_reports_parallel_and_completed_delegates_and_last_activity(self):
        write_jsonl(self.transcript, [
            self.parent("user", self.at(0), [{"type": "text", "text": "PROMPT_SENTINEL_2c4"}]),
            self.parent("assistant", self.at(10), [tool_use("Read", "toolu_read", file_path="FILE_PATH_SENTINEL_secret_txt")]),
            self.parent("user", self.at(11), [tool_result("toolu_read", "RESULT_SENTINEL_stack")]),
            self.parent("assistant", self.at(20), [tool_use("Agent", "toolu_RAWID_SENTINEL", subagent_type="sonnet-worker", prompt="PROMPT_SENTINEL_2c4")], record_uuid="record-1"),
            self.parent("assistant", self.at(21), [tool_use("Agent", "toolu_second", subagent_type="opus-reviewer")], record_uuid="record-2"),
            self.parent("user", self.at(30), [tool_result("toolu_RAWID_SENTINEL", "RESULT_SENTINEL_stack")]),
            self.parent("assistant", self.at(40), [tool_use("Bash", "toolu_bash", command="COMMAND_SENTINEL_rm_rf", description="DESCRIPTION_SENTINEL_tests_passed")]),
        ])
        value = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        self.assertEqual((value["phase"], value["phase_detail"]), ("model", "delegate_pending"))
        self.assertEqual(value["activity"], {"last_observed_at": "2026-09-05T11:50:40Z", "category": "shell", "source": "parent_session", "event": "tool_start"})
        delegates = value["delegates"]
        self.assertEqual({key: delegates[key] for key in ("requested", "pending", "background", "completed", "observed_children", "attributed_children", "truncated")},
                         {"requested": 2, "pending": 1, "background": 0, "completed": 1, "observed_children": 0, "attributed_children": 0, "truncated": False})
        pending, completed = delegates["items"]  # pending waits are listed first
        self.assertEqual(completed, {"handle": hashlib.sha256(b"toolu_RAWID_SENTINEL").hexdigest()[:12], "requested_role": "sonnet-worker",
                                     "state": "completed", "requested_at": "2026-09-05T11:50:20Z", "result_at": "2026-09-05T11:50:30Z", "child": None})
        self.assertEqual((pending["requested_role"], pending["state"], pending["result_at"], pending["child"]), ("opus-reviewer", "pending", None, None))
        # Claude Code launches delegates in the background by default: the tool result only acknowledges the launch.
        write_jsonl(self.transcript, [
            self.parent("assistant", self.at(41), [tool_use("Agent", "toolu_bg", subagent_type="sonnet-worker")], record_uuid="record-bg"),
            self.launched("toolu_bg", self.at(42), "bg-agent"),
            self.parent("assistant", self.at(43), [tool_use("Agent", "toolu_flag", subagent_type="opus-reviewer", run_in_background=True)], record_uuid="record-flag"),
            self.parent("user", self.at(44), [tool_result("toolu_flag", "RESULT_SENTINEL_stack")])])
        launched = self.observe()["delegates"]
        self.assertEqual((launched["requested"], launched["pending"], launched["background"], launched["completed"]), (4, 1, 2, 1))
        self.assertEqual([item["state"] for item in launched["items"]], ["pending", "background", "background", "completed"])
        self.assertEqual([item["requested_at"] for item in launched["items"][1:3]], ["2026-09-05T11:50:43Z", "2026-09-05T11:50:41Z"])
        serialized = json.dumps(value)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        self.assertNotIn("toolu_", serialized)
        for name, category in (("mcp__qwen-local__qwen_submit", "local-model"), ("Skill", "skill"), ("WebFetch", "other"), ("Edit", "edit"), ("StructuredOutput", "output")):
            with self.subTest(name=name):
                write_jsonl(self.transcript, [self.parent("assistant", self.at(50), [tool_use(name, "toolu_" + category)])])
                self.assertEqual(self.observe()["activity"]["category"], category)
        write_jsonl(self.transcript, [self.parent("user", self.at(60), [tool_result("toolu_local-model")])])
        self.assertEqual(self.observe()["activity"], {"last_observed_at": "2026-09-05T11:51:00Z", "category": "local-model", "source": "parent_session", "event": "tool_result"})
        write_jsonl(self.transcript, [self.parent("assistant", self.at(70), [{"type": "text", "text": "TEXT_SENTINEL_77a"}])])
        self.assertEqual(self.observe()["activity"]["category"], "message")
        many = [self.parent("assistant", self.at(80 + index), [tool_use("Agent", f"toolu_many_{index}", subagent_type="sonnet-worker")], record_uuid=f"many-{index}") for index in range(9)]
        write_jsonl(self.transcript, many)
        crowded = self.observe()["delegates"]
        self.assertEqual((crowded["requested"], crowded["pending"], len(crowded["items"]), crowded["truncated"]), (13, 10, 8, True))
        self.assertTrue(all(item["state"] == "pending" for item in crowded["items"]))
        self.assertEqual(crowded["items"][0]["requested_at"], "2026-09-05T11:51:28Z")

    def test_child_attribution_requires_unique_correlation_session_and_cwd_witness(self):
        write_jsonl(self.transcript, [
            self.parent("assistant", self.at(20), [tool_use("Agent", "toolu_one", subagent_type="sonnet-worker")], record_uuid="record-one"),
            self.launched("toolu_one", self.at(21), "alpha"),
            self.parent("assistant", self.at(22), [tool_use("Agent", "toolu_two", subagent_type="opus-reviewer"),
                                                    tool_use("Agent", "toolu_three", subagent_type="opus-reviewer")], record_uuid="record-pair"),
            self.parent("assistant", self.at(23), [tool_use("Agent", "toolu_four", subagent_type="sonnet-worker")], record_uuid="record-four"),
            self.parent("assistant", self.at(24), [tool_use("Agent", "toolu_five", subagent_type="sonnet-worker")], record_uuid="record-five"),
            self.parent("assistant", self.at(25), [tool_use("Agent", "toolu_six", subagent_type="sonnet-worker")], record_uuid="record-six"),
            self.parent("assistant", self.at(30), [tool_use("Grep", "toolu_grep")]),
        ])
        # alpha: linked through the launch acknowledgement's agentId; its own sourceToolAssistantUUIDs point at its own records.
        self.write_child("alpha", [
            self.child("user", self.at(26), None, [{"type": "text", "text": "PROMPT_SENTINEL_2c4"}], agent_id="alpha"),
            self.child("assistant", self.at(27), None, [tool_use("Read", "toolu_child_read")], model="claude-sonnet-5", agent_id="alpha", record_uuid="alpha-1", stop_reason="tool_use"),
            self.child("user", self.at(28), "alpha-1", [tool_result("toolu_child_read")], agent_id="alpha"),
            self.child("assistant", self.at(50), "alpha-1", [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1"},
                                                            tool_use("Edit", "toolu_child_edit", file_path="FILE_PATH_SENTINEL_secret_txt")],
                       model="claude-sonnet-5", agent_id="alpha", stop_reason="tool_use"),
        ])
        # beta: unique source uuid naming a record with two delegate blocks -> ambiguous.
        self.write_child("beta", [self.child("assistant", self.at(40), "record-pair", [tool_use("Bash", "toolu_b")], model="claude-opus-5")])
        # gamma: no working-directory witness.
        self.write_child("gamma", [self.child("assistant", self.at(41), "record-four", [tool_use("Bash", "toolu_c")], cwd=None)])
        # delta: another session entirely -> never observed.
        self.write_child("delta", [self.child("assistant", self.at(42), "record-one", [tool_use("Bash", "toolu_d")], session=str(uuid.uuid4()))])
        # epsilon: wrong working directory -> records rejected, not observed.
        self.write_child("epsilon", [self.child("assistant", self.at(43), "record-four", [tool_use("Bash", "toolu_e")], cwd="/UNRELATED_CWD_SENTINEL/elsewhere")])
        # zeta: no correlation metadata at all.
        self.write_child("zeta", [self.child("assistant", self.at(44), None, [tool_use("Bash", "toolu_f")])])
        # eta: records claim a different agentId than the file name -> skipped.
        self.write_child("eta", [self.child("assistant", self.at(45), "record-five", [tool_use("Bash", "toolu_g")], agent_id="someone-else")])
        # theta: two distinct source uuids -> ambiguous.
        self.write_child("theta", [self.child("assistant", self.at(46), "record-five", [tool_use("Bash", "toolu_h")]),
                                   self.child("assistant", self.at(47), "record-four", [tool_use("Bash", "toolu_i")])])
        # iota: unique source uuid naming a record with exactly one delegate block -> attributed by the fallback path.
        self.write_child("iota", [self.child("assistant", self.at(48), "record-six", [tool_use("Bash", "toolu_j")], model="claude-opus-5", stop_reason="end_turn")])
        value = self.observe()
        delegates = value["delegates"]
        self.assertEqual({key: delegates[key] for key in ("requested", "pending", "background", "completed", "observed_children", "attributed_children")},
                         {"requested": 6, "pending": 5, "background": 1, "completed": 0, "observed_children": 6, "attributed_children": 2})
        by_handle = {item["handle"]: item for item in delegates["items"]}
        alpha = by_handle[hashlib.sha256(b"toolu_one").hexdigest()[:12]]
        self.assertEqual((alpha["state"], alpha["result_at"]), ("background", "2026-09-05T11:50:21Z"))
        self.assertEqual(alpha["child"], {"observed_model": "claude-sonnet-5", "last_observed_at": "2026-09-05T11:50:50Z", "last_category": "edit", "turn_ended": False})
        six = by_handle[hashlib.sha256(b"toolu_six").hexdigest()[:12]]
        self.assertEqual(six["child"], {"observed_model": "claude-opus-5", "last_observed_at": "2026-09-05T11:50:48Z", "last_category": "shell", "turn_ended": True})
        self.assertEqual(sum(item["child"] is None for item in delegates["items"]), 4)
        self.assertEqual(value["activity"], {"last_observed_at": "2026-09-05T11:50:50Z", "category": "edit", "source": "child_session", "event": "tool_start"})
        self.assertEqual(value["limitations"], {"child_attribution_ambiguous", "child_attribution_unavailable", "child_cwd_witness_missing"})
        applied = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        self.assertEqual(applied["phase_detail"], "delegate_pending")
        serialized = json.dumps(value, default=sorted)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        for private in ("record-", "alpha", "bg-agent", "async_launched"):
            self.assertNotIn(private, serialized)
        # A second file claiming the same unique request makes that request ambiguous too.
        self.write_child("kappa", [self.child("assistant", self.at(51), "record-six", [tool_use("Bash", "toolu_k")], model="claude-opus-5")])
        contested = self.observe()
        self.assertEqual(contested["delegates"]["attributed_children"], 1)
        self.assertIsNone({item["handle"]: item for item in contested["delegates"]["items"]}[six["handle"]]["child"])
        self.assertIn("child_attribution_ambiguous", contested["limitations"])
        # An agentId link and a source link that disagree are ambiguous; a reused tool ID never collapses the object.
        write_jsonl(self.transcript, [
            self.parent("assistant", self.at(60), [tool_use("Agent", "toolu_dup", subagent_type="sonnet-worker")], record_uuid="record-dup-a"),
            self.parent("assistant", self.at(61), [tool_use("Agent", "toolu_dup", subagent_type="sonnet-worker")], record_uuid="record-dup-b"),
            self.parent("assistant", self.at(62), [tool_use("Agent", "toolu_seven", subagent_type="sonnet-worker")], record_uuid="record-seven"),
            self.launched("toolu_seven", self.at(63), "lambda")])
        self.write_child("mu", [self.child("assistant", self.at(64), "record-dup-a", [tool_use("Bash", "toolu_m")])])
        self.write_child("lambda", [self.child("assistant", self.at(65), "record-dup-b", [tool_use("Bash", "toolu_l")], agent_id="lambda")])
        reused = self.observe()
        self.assertIsNotNone(reused["activity"])
        self.assertEqual(reused["delegates"]["requested"], 8)
        self.assertTrue(all(item["child"] is None for item in reused["delegates"]["items"] if item["handle"] in
                            {hashlib.sha256(b"toolu_dup").hexdigest()[:12], hashlib.sha256(b"toolu_seven").hexdigest()[:12]}))
        # A launched delegate whose child ended its turn no longer counts as waiting.
        self.transcript.unlink()
        write_jsonl(self.transcript, [
            self.parent("assistant", self.at(70), [tool_use("Agent", "toolu_done", subagent_type="sonnet-worker")], record_uuid="record-done"),
            self.launched("toolu_done", self.at(71), "nu")])
        self.write_child("nu", [self.child("assistant", self.at(72), None, [tool_use("Bash", "toolu_n")], agent_id="nu", stop_reason="tool_use"),
                                self.child("user", self.at(73), None, [tool_result("toolu_n")], agent_id="nu"),
                                self.child("assistant", self.at(74), None, [{"type": "text", "text": "TEXT_SENTINEL_77a"}], agent_id="nu", stop_reason="end_turn")])
        settled = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        self.assertEqual((settled["phase_detail"], settled["delegates"]["background"]), (None, 1))
        self.assertEqual(settled["delegates"]["items"][0]["child"]["turn_ended"], True)

    def test_requested_role_is_never_conflated_with_observed_model_and_unknown_models_normalize(self):
        write_jsonl(self.transcript, [
            self.parent("assistant", self.at(20), [tool_use("Agent", "toolu_sonnet", subagent_type="sonnet-worker", model="MODEL_SENTINEL_slug")], record_uuid="record-sonnet"),
            self.parent("assistant", self.at(21), [tool_use("Agent", "toolu_other", subagent_type="general-purpose")], record_uuid="record-other"),
            self.parent("assistant", self.at(22), [tool_use("Task", "toolu_task")], record_uuid="record-task"),
            self.parent("assistant", self.at(23), [tool_use("Agent", "toolu_opus", subagent_type="opus-reviewer")], record_uuid="record-opus"),
        ])
        requested_only = self.observe()["delegates"]["items"]
        self.assertEqual([item["requested_role"] for item in requested_only], ["opus-reviewer", "unknown", "other", "sonnet-worker"])
        self.assertTrue(all(item["child"] is None for item in requested_only))
        self.assertNotIn("claude-sonnet", json.dumps(requested_only))
        self.write_child("slug", [self.child("assistant", self.at(30), "record-sonnet", [tool_use("Read", "toolu_r")], model="sk-ant-api03-CREDENTIAL_SENTINEL")])
        self.write_child("known", [self.child("assistant", self.at(31), "record-opus", [tool_use("Read", "toolu_k")], model="claude-opus-5")])
        self.write_child("generic", [self.child("assistant", self.at(32), "record-other", [tool_use("Read", "toolu_g")], model="MODEL_SENTINEL_slug")])
        value = self.observe()
        items = {item["requested_role"]: item for item in value["delegates"]["items"]}
        self.assertEqual(items["sonnet-worker"]["child"]["observed_model"], "unknown")
        self.assertEqual(items["opus-reviewer"]["child"]["observed_model"], "claude-opus-5")
        self.assertEqual(items["other"]["child"]["observed_model"], "unknown")
        self.assertIn("observed_model_unrecognized", value["limitations"])
        serialized = json.dumps(value, default=sorted)
        self.assertNotIn("sk-ant", serialized)
        self.assertNotIn("MODEL_SENTINEL", serialized)
        for candidate, accepted in (("claude-fable-5-1", True), ("claude-haiku-4-5-20251001", True), ("claude-opus-5", True),
                                    ("claude-sonnet-4-5-20250929", True), ("claude-3-5-sonnet-20241022", False), ("claude-opus-5-x", False),
                                    ("", False), ("claude-sonnet-" + "9" * 40, False), ("claude-opus-99999999-99999999-99999999", False)):
            with self.subTest(candidate=candidate):
                self.assertEqual(bool(progress.MODEL_ID.match(candidate)), accepted)

    def test_secret_sentinels_never_appear_in_serialized_progress(self):
        other_session = str(uuid.uuid4())
        write_jsonl(self.transcript, [
            {"type": "summary", "summary": "SUMMARY_SENTINEL_title", "leafUuid": "x"},
            self.parent("user", self.at(1), [{"type": "text", "text": "PROMPT_SENTINEL_2c4"}]),
            self.parent("assistant", self.at(2), [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1", "signature": "sk-ant-api03-CREDENTIAL_SENTINEL"},
                                                   {"type": "text", "text": "TEXT_SENTINEL_77a"},
                                                   tool_use("Bash", "toolu_RAWID_SENTINEL", command="COMMAND_SENTINEL_rm_rf", description="DESCRIPTION_SENTINEL_tests_passed")],
                        model="MODEL_SENTINEL_slug", error="ERROR_SENTINEL_payload"),
            self.parent("user", self.at(3), [tool_result("toolu_RAWID_SENTINEL", "RESULT_SENTINEL_stack")], toolUseResult={"stderr": "ERROR_SENTINEL_payload"}),
            record("assistant", self.session, "/UNRELATED_CWD_SENTINEL/other", self.at(4), [tool_use("Write", "toolu_w", file_path="FILE_PATH_SENTINEL_secret_txt")]),
            record("assistant", other_session, str(self.worktree), self.at(5), [tool_use("Write", "toolu_x", content="TEXT_SENTINEL_77a")]),
            b'{"type":"assistant","sessionId":"' + self.session.encode() + b'","timestamp":"MALFORMED_SENTINEL_line',
            self.parent("assistant", self.at(6), [tool_use("Agent", "toolu_agent", subagent_type="sonnet-worker", prompt="PROMPT_SENTINEL_2c4")], record_uuid="record-agent"),
            self.launched("toolu_agent", self.at(7), "kid"),
        ])
        self.write_child("kid", [self.child("assistant", self.at(8), "kid-own-record", [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1"},
                                                                                        tool_use("Bash", "toolu_kid", command="COMMAND_SENTINEL_rm_rf")],
                                            model="MODEL_SENTINEL_slug", error="ERROR_SENTINEL_payload", agent_id="kid")])
        value = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        serialized = json.dumps(value)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        self.assertNotIn(str(self.worktree), serialized)
        self.assertNotIn(self.session, serialized)
        self.assertNotIn("toolu_", serialized)
        self.assertEqual(value["activity"]["source"], "child_session")
        self.assertEqual(value["delegates"]["items"][0]["child"]["observed_model"], "unknown")
        self.assertEqual(set(value), {"schema_version", "observed_at", "phase", "phase_detail", "outcome", "attempt", "elapsed_seconds",
                                      "elapsed_basis", "deadline", "deadline_unavailable_reason", "gate", "activity",
                                      "activity_unavailable_reason", "delegates", "heartbeat", "recent_activity", "limitations"})
        self.assertEqual(set(value["activity"]), {"last_observed_at", "category", "source", "event"})
        self.assertEqual(set(value["delegates"]["items"][0]), {"handle", "requested_role", "state", "requested_at", "result_at", "child"})
        self.assertEqual(set(value["delegates"]["items"][0]["child"]), {"observed_model", "last_observed_at", "last_category", "turn_ended"})
        self.assertNotIn("kid", serialized)
        self.assertIn("malformed_records_skipped", value["limitations"])

    def test_partial_tail_malformed_oversized_missing_duplicate_and_symlink_inputs_degrade_safely(self):
        self.assertEqual(self.observe()["activity_unavailable_reason"], "transcript_missing")
        write_jsonl(self.transcript, [self.parent("assistant", self.at(10), [tool_use("Read", "toolu_1")])],
                    trailing=b'{"type":"assistant","sessionId":"' + self.session.encode() + b'","timestamp":"2026-09-05T11:5')
        partial = self.observe()
        self.assertEqual((partial["activity"]["category"], partial["limitations"]), ("read", set()))
        write_jsonl(self.transcript, [b"{not json", b"[1, 2, 3]", b"\xff\xfe binary", self.parent("assistant", self.at(20), [tool_use("Edit", "toolu_2")])])
        recovered = self.observe()
        self.assertEqual(recovered["activity"]["category"], "edit")
        self.assertEqual(recovered["limitations"], {"malformed_records_skipped", "unsupported_records_skipped"})
        padding = "p" * 1024
        write_jsonl(self.transcript, [self.parent("assistant", self.at(30 + index), [{"type": "text", "text": padding}]) for index in range(3000)])
        write_jsonl(self.transcript, [self.parent("assistant", self.at(4000), [tool_use("Bash", "toolu_3")])])
        self.assertGreater(self.transcript.stat().st_size, progress.PARENT_TAIL_BYTES)
        oversized = self.observe(now=self.at(5000))
        self.assertEqual(oversized["activity"]["category"], "shell")
        self.assertIn("tail_window_truncated", oversized["limitations"])
        self.transcript.unlink()
        compact = [{"type": "assistant", "sessionId": self.session, "timestamp": self.at(index).isoformat(),
                    "cwd": str(self.worktree), "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read"}]}} for index in range(5001)]
        write_jsonl(self.transcript, compact)
        self.assertLess(self.transcript.stat().st_size, progress.PARENT_TAIL_BYTES)
        windowed = self.observe(now=self.at(7000))
        self.assertEqual(windowed["activity"]["last_observed_at"], progress.stamp(self.at(5000)))
        self.assertIn("record_window_truncated", windowed["limitations"])
        self.transcript.unlink()
        write_jsonl(self.transcript, [self.parent("assistant", self.at(10), [tool_use("Read", "toolu_1")])])
        duplicate = self.projects / "another-encoded-dir" / self.transcript.name
        write_jsonl(duplicate, [self.parent("assistant", self.at(11), [tool_use("Bash", "toolu_dup")])])
        self.assertEqual(self.observe()["activity_unavailable_reason"], "session_ambiguous")
        self.assertEqual(self.observe(predicted=None)["activity_unavailable_reason"], "session_ambiguous")
        duplicate.unlink()
        self.assertEqual(self.observe(predicted=None)["activity"]["category"], "read")
        self.assertEqual(self.observe(predicted=str(self.projects / "wrong-prediction" / self.transcript.name))["activity"]["category"], "read")
        outside = self.root / "outside" / self.transcript.name
        write_jsonl(outside, [self.parent("assistant", self.at(12), [tool_use("Bash", "toolu_out")])])
        self.transcript.unlink()
        self.transcript.symlink_to(outside)
        escaped = self.observe()
        self.assertEqual((escaped["activity"], escaped["activity_unavailable_reason"]), (None, "path_rejected"))
        self.assertIn("predicted_path_rejected", escaped["limitations"])
        self.assertEqual(self.observe(predicted=str(outside), projects_dir=None)["activity_unavailable_reason"], "path_rejected")
        self.transcript.unlink()
        linked_dir = self.projects / "linked-dir"
        linked_dir.symlink_to(outside.parent)
        self.assertEqual(self.observe(predicted=str(linked_dir / self.transcript.name))["activity_unavailable_reason"], "path_rejected")
        linked_dir.unlink()
        explicit = self.root / "explicit" / self.transcript.name
        write_jsonl(explicit, [self.parent("assistant", self.at(13), [tool_use("Grep", "toolu_explicit")])])
        self.assertEqual(self.observe(predicted=str(explicit), explicit_root=str(explicit.parent))["activity"]["category"], "read")
        self.assertEqual(self.observe(predicted=str(explicit))["activity_unavailable_reason"], "path_rejected")
        write_jsonl(self.transcript, [self.parent("assistant", self.at(14), [tool_use("Edit", "toolu_projects")])])
        # The exact UUID must be unambiguous across both configured locations.
        self.assertEqual(self.observe(predicted=str(explicit), explicit_root=str(explicit.parent))["activity_unavailable_reason"], "session_ambiguous")
        self.assertEqual(self.observe(predicted=self.transcript.name)["activity"]["category"], "edit")
        self.assertIn("predicted_path_rejected", self.observe(predicted=self.transcript.name)["limitations"])
        self.assertEqual(self.observe(predicted=None, projects_dir="relative/projects")["activity_unavailable_reason"], "path_rejected")
        self.transcript.unlink()
        write_jsonl(self.transcript, [self.parent("assistant", self.at(20), [tool_use("Agent", "toolu_a", subagent_type="sonnet-worker")], record_uuid="record-a")])
        children = self.transcript.parent / self.session / "subagents"
        elsewhere = self.root / "elsewhere-children"
        write_jsonl(elsewhere / "agent-x.jsonl", [self.child("assistant", self.at(30), "record-a", [tool_use("Bash", "toolu_x")], model="claude-sonnet-5")], mtime=self.now)
        children.parent.mkdir(parents=True)
        children.symlink_to(elsewhere)
        linked_children = self.observe()
        self.assertEqual(linked_children["delegates"]["attributed_children"], 0)
        self.assertIn("children_dir_rejected", linked_children["limitations"])
        children.unlink()
        children.mkdir()
        (children / "agent-y.jsonl").symlink_to(elsewhere / "agent-x.jsonl")
        linked_child = self.observe()
        self.assertEqual(linked_child["delegates"]["attributed_children"], 0)
        self.assertIn("child_path_rejected", linked_child["limitations"])
        (children / "agent-y.jsonl").unlink()
        stale = children / "agent-stale.jsonl"
        write_jsonl(stale, [self.child("assistant", self.at(40), "record-a", [tool_use("Bash", "toolu_stale")])], mtime=self.start - dt.timedelta(hours=1))
        self.assertEqual(self.observe()["delegates"]["observed_children"], 0, "A file untouched since the attempt began holds no attempt records")
        stale.unlink()
        for index in range(progress.MAX_CHILD_FILES + 3):
            write_jsonl(children / f"agent-{index:03d}.jsonl", [self.child("assistant", self.at(40), None, [tool_use("Bash", "toolu_n")], agent_id=f"{index:03d}")],
                        mtime=self.now - dt.timedelta(seconds=index))
        self.assertIn("children_truncated", self.observe()["limitations"])
        self.assertEqual(self.observe()["delegates"]["observed_children"], progress.MAX_CHILD_FILES)
        unreadable = children / "agent-000.jsonl"
        unreadable.chmod(0)
        try:
            self.assertIn("child_unreadable", self.observe()["limitations"])
        finally:
            unreadable.chmod(0o600)
        self.assertEqual(progress.observe_session(self.inputs(), "not-a-uuid", str(self.worktree), self.start, self.now)["activity_unavailable_reason"], "metadata_unsupported")
        self.transcript.write_bytes(b"")
        self.assertEqual(self.observe()["activity_unavailable_reason"], "no_records_in_attempt_window")
        write_jsonl(self.transcript, [record("assistant", str(uuid.uuid4()), str(self.worktree), self.at(5), [tool_use("Bash", "toolu_other")])])
        self.assertEqual(self.observe()["activity_unavailable_reason"], "session_mismatch")
        write_jsonl(self.transcript, [{"type": "summary", "sessionId": self.session, "summary": "SUMMARY_SENTINEL_title"}])
        self.assertEqual(self.observe()["activity_unavailable_reason"], "no_records_in_attempt_window")
        write_jsonl(self.transcript, [record("assistant", self.session, "/UNRELATED_CWD_SENTINEL/other", self.at(6), [tool_use("Bash", "toolu_cwd")])])
        self.assertEqual(self.observe()["activity_unavailable_reason"], "cwd_mismatch")
        self.transcript.chmod(0)
        try:
            self.assertEqual(self.observe()["activity_unavailable_reason"], "path_rejected")
        finally:
            self.transcript.chmod(0o600)
        hostile = [self.parent("assistant", self.at(7 + index), [tool_use("Bash", "toolu_h")]) for index in range(300)]
        for index, value in enumerate(hostile):
            value["cwd"] = "/" + "/".join(["d"] * 900) + f"/{index}"
        self.transcript.write_bytes(b"")
        write_jsonl(self.transcript, hostile)
        started = time.monotonic()
        self.assertEqual(self.observe()["activity_unavailable_reason"], "cwd_mismatch")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_future_dated_old_attempt_and_sidechain_records_are_ignored(self):
        write_jsonl(self.transcript, [
            self.parent("assistant", self.start - dt.timedelta(seconds=1), [tool_use("Bash", "toolu_old")]),
            self.parent("assistant", self.now + dt.timedelta(minutes=1), [tool_use("Bash", "toolu_future")]),
            self.parent("assistant", self.at(10), [tool_use("Read", "toolu_read")]),
            {**self.parent("assistant", self.at(11), [tool_use("Edit", "toolu_notime")]), "timestamp": None},
            {**self.parent("assistant", self.at(12), [tool_use("Edit", "toolu_naive")]), "timestamp": "2026-09-05T11:52:00"},
            self.parent("assistant", self.at(13), [tool_use("Edit", "toolu_side")], sidechain=True),
        ])
        value = self.observe()
        self.assertEqual(value["activity"], {"last_observed_at": "2026-09-05T11:50:10Z", "category": "read", "source": "parent_session", "event": "tool_start"})
        self.assertEqual(value["limitations"], {"future_records_ignored", "records_without_valid_timestamp_ignored", "inline_sidechain_records_ignored"})
        empty = progress.job_progress(self.job("running"), self.now, handoff=self.handoff(started_at=self.at(60).isoformat()))
        self.assertEqual((empty["activity"], empty["activity_unavailable_reason"]), (None, "no_records_in_attempt_window"))
        self.assertEqual(empty["delegates"]["requested"], 0)
        write_jsonl(self.transcript, [self.parent("assistant", self.now + dt.timedelta(microseconds=2), [tool_use("Grep", "toolu_soon")])])
        self.assertEqual(self.observe()["activity"]["last_observed_at"], "2026-09-05T11:50:10Z")

    def test_timestamps_handles_and_clock_anomalies_are_normalized_before_emission(self):
        write_jsonl(self.transcript, [self.parent("assistant", self.at(20), [tool_use("Agent", "toolu_a", subagent_type="sonnet-worker")], record_uuid="record-a")])
        value = progress.job_progress(self.job("running"), self.now, handoff=self.handoff())
        for stamp in (value["observed_at"], value["activity"]["last_observed_at"], value["delegates"]["items"][0]["requested_at"],
                      value["deadline"]["started_at"], value["deadline"]["deadline_at"]):
            self.assertRegex(stamp, STAMP)
        self.assertRegex(value["delegates"]["items"][0]["handle"], r"^[0-9a-f]{12}$")
        future_job = self.job("running", started_at=(self.now + dt.timedelta(hours=1)).isoformat())
        anomaly = progress.job_progress(future_job, self.now, handoff=self.handoff(started_at=(self.now + dt.timedelta(hours=1)).isoformat()))
        self.assertEqual(anomaly["elapsed_seconds"], 0)
        self.assertIn("clock_anomaly", anomaly["limitations"])
        for bad in ("yesterday", "2026-09-05T11:50:00", 12345, None, ""):
            with self.subTest(bad=bad):
                self.assertIsNone(progress.parse_time(bad))
                broken = progress.job_progress(self.job("running", started_at=bad, created_at=bad), self.now, handoff=self.handoff(started_at=bad))
                self.assertEqual((broken["elapsed_seconds"], broken["elapsed_basis"]), (None, "unavailable"))
                self.assertEqual((broken["deadline"], broken["deadline_unavailable_reason"], broken["activity_unavailable_reason"]), (None, "state_unreadable", "state_unreadable"))
        self.assertEqual(progress.unavailable(self.job("running"), self.now)["limitations"], ["progress_unavailable"])
        self.assertFalse(hasattr(progress, "subprocess"))
        for hostile in ({}, {"status": None}, {"status": "running", "kind": "implementation", "started_at": 5, "payload": []},
                        {"status": "running", "kind": "review"}, {"status": "succeeded", "result": "text"}, {"status": 7, "kind": []}):
            with self.subTest(hostile=hostile):
                value = progress.job_progress(hostile, self.now, review={"turn": "bad"}, handoff={"state": "bad"})
                self.assertEqual(value["schema_version"], 1)
                json.dumps(value, allow_nan=False)

    def test_handoff_and_review_contexts_verify_pinned_inputs_and_read_only(self):
        directory = self.root / "handoff"
        directory.mkdir()
        config = {"timeout_seconds": 1234, "gate_timeout_seconds": 56, "claude_config_dir": str(self.root / "claude"), "session_transcript_path": None}
        config_bytes = (room.canonical(config) + "\n").encode()
        (directory / "implementation-config.json").write_bytes(config_bytes)
        manifest = {"handoff_id": "h", "session_id": self.session, "worktree_path": str(self.worktree), "gates": [["one"]],
                    "session_transcript_path": str(self.transcript), "pinned_files": {"implementation-config.json": room.sha(config_bytes)}}
        (directory / "handoff.json").write_text(json.dumps(manifest))
        state = {"phase": "running_model", "manifest_sha256": implementation._digest(manifest), "started_at": self.start.isoformat(),
                 "attempt_count": 1, "owner_job_id": self.job_id, "active_stage": {"kind": "model", "index": 1, "started_at": self.start.isoformat()}}
        (directory / "state.json").write_text(json.dumps(state))
        cache = {}
        context = progress.handoff_context(str(directory / "handoff.json"), cache)
        self.assertTrue(context["config_verified"])
        self.assertEqual(context["transcript"], {"predicted": str(self.transcript), "projects_dir": str(self.root / "claude" / "projects"), "explicit_root": None})
        value = progress.job_progress(self.job("running"), self.now, handoff=context)
        self.assertEqual((value["phase"], value["deadline"]["timeout_seconds"], value["limitations"]), ("model", 1234, []))
        self.assertTrue(cache)
        (directory / "implementation-config.json").write_text(json.dumps(dict(config, timeout_seconds=1)))
        tampered = progress.handoff_context(str(directory / "handoff.json"), cache)
        self.assertFalse(tampered["config_verified"])
        self.assertIn("handoff_config_unverified", tampered["limitations"])
        self.assertEqual(progress.job_progress(self.job("running"), self.now, handoff=tampered)["deadline_unavailable_reason"], "pinned_timeout_unavailable")
        (directory / "implementation-config.json").write_bytes(config_bytes)
        (directory / "handoff.json").write_text(json.dumps(dict(manifest, gates=[["changed"]])))
        unverified = progress.handoff_context(str(directory), cache)
        self.assertIn("handoff_manifest_unverified", unverified["limitations"])
        self.assertIsNone(unverified["manifest"])
        (directory / "handoff.json").write_text(json.dumps(manifest))
        explicit = dict(config, session_transcript_path="{worktree}/../logs/{session_id}.jsonl")
        explicit_bytes = (room.canonical(explicit) + "\n").encode()
        (directory / "implementation-config.json").write_bytes(explicit_bytes)
        pinned = dict(manifest, pinned_files={"implementation-config.json": room.sha(explicit_bytes)}, session_transcript_path=str(self.root / "logs" / self.transcript.name))
        (directory / "handoff.json").write_text(json.dumps(pinned))
        (directory / "state.json").write_text(json.dumps(dict(state, manifest_sha256=implementation._digest(pinned))))
        self.assertEqual(progress.handoff_context(str(directory), {})["transcript"]["explicit_root"], str(self.root / "logs"))
        (directory / "state.json").write_bytes(b"{" * 10)
        self.assertIsNone(progress.handoff_context(str(directory), {})["state"])
        (directory / "state.json").write_bytes(b"x" * (progress.MAX_STATE_BYTES + 1))
        self.assertEqual(progress.read_json(directory / "state.json", progress.MAX_STATE_BYTES), (None, "oversized"))
        self.assertIsNone(progress.handoff_context(None)["state"])
        missing = progress.review_context(self.root / "no-room", "request", None, str(self.root / "claude"))
        self.assertEqual((missing.get("error"), missing["turn"]), ("unreadable", None))
        self.assertFalse((self.root / "no-room" / "room.sqlite3").exists())
        review_dir = self.root / "review-room"
        review_dir.mkdir()
        with sqlite3.connect(str(review_dir / "room.sqlite3")) as db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE turns(request_id TEXT, status TEXT, session_id TEXT, started_at TEXT, finished_at TEXT)")
            db.execute("INSERT INTO metadata VALUES('config_snapshot', ?)", (json.dumps({"timeout_seconds": 77}),))
            db.execute("INSERT INTO turns VALUES('request', 'pending', ?, ?, NULL)", (self.session, self.start.isoformat()))
        before = (review_dir / "room.sqlite3").read_bytes()
        context = progress.review_context(review_dir, "request", str(self.transcript), str(self.root / "claude"))
        self.assertEqual((context["timeout_seconds"], context["turn"]["status"], context["turn"]["session_id"]), (77, "pending", self.session))
        self.assertEqual((review_dir / "room.sqlite3").read_bytes(), before)
        self.assertEqual(sorted(path.name for path in review_dir.iterdir()), ["room.sqlite3"])
        self.assertIsNone(progress.review_context(review_dir, "other", None, None)["turn"])


    def test_descriptor_discovery_limits_and_final_complete_line_cap(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        import stat
        visited = 0
        @contextmanager
        def entries(*unused):
            def stream():
                nonlocal visited
                for index in range(progress.MAX_DISCOVERY_ENTRIES + 1):
                    visited += 1
                    self.assertLessEqual(visited, progress.MAX_DISCOVERY_ENTRIES)
                    yield SimpleNamespace(name=f"agent-{index:04d}.jsonl", stat=lambda **kw: SimpleNamespace(
                        st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_mtime=self.now.timestamp(), st_mtime_ns=1))
            yield stream()
        write_jsonl(self.transcript, [self.parent("assistant", self.at(1))])
        with mock.patch.object(progress, "_directory_scan", entries):
            children, limits = progress._child_files(self.transcript, self.session, self.start)
        self.assertEqual(visited, progress.MAX_DISCOVERY_ENTRIES, "The actual descriptor-scan seam must be exercised")
        self.assertEqual(len(children), progress.MAX_CHILD_FILES)
        self.assertIn("children_discovery_limited", limits)
        self.transcript.write_bytes(b'{"i":0}\n{"i":1}\n{"i":2}')
        records, limits = progress.read_records(self.transcript, 128, 2)
        self.assertEqual(records, [{"i": 1}, {"i": 2}])
        self.assertIn("record_window_truncated", limits)

    def test_descriptor_read_rejects_leaf_and_directory_swaps(self):
        outside = self.root / "outside"
        outside.mkdir()
        for directory_swap in (False, True):
            with self.subTest(directory_swap=directory_swap):
                parent = self.transcript.parent
                if parent.is_symlink():
                    parent.unlink()
                write_jsonl(self.transcript, [self.parent("assistant", self.at(1), [tool_use("Read", "good")])])
                (outside / self.transcript.name).write_text(json.dumps(self.parent("assistant", self.at(2), [tool_use("Bash", "bad")])))
                original = progress.read_records
                swapped = []
                def swap(path, *args, **kwargs):
                    if Path(path) == self.transcript and not swapped:
                        self.transcript.unlink()
                        if directory_swap:
                            parent.rmdir()
                            parent.symlink_to(outside)
                        else:
                            self.transcript.symlink_to(outside / self.transcript.name)
                        swapped.append(True)
                    return original(path, *args, **kwargs)
                with mock.patch.object(progress, "read_records", side_effect=swap):
                    value = self.observe()
                self.assertEqual(swapped, [True])
                self.assertIsNone(value["activity"])
                if directory_swap:
                    parent.unlink()
                else:
                    self.transcript.unlink()

    def test_strict_child_identity_future_records_and_parent_cwd_witness(self):
        write_jsonl(self.transcript, [self.parent("assistant", self.at(1), [tool_use("Agent", "request")], record_uuid="origin")])
        for missing in ("agentId", "isSidechain"):
            child = self.child("assistant", self.at(2), "origin", [tool_use("Bash", "secret")], agent_id="child")
            del child[missing]
            self.child_path("child").parent.mkdir(parents=True, exist_ok=True)
            self.child_path("child").write_text(json.dumps(child) + "\n")
            observed = self.observe()
            self.assertEqual(observed["delegates"]["attributed_children"], 0)
            self.assertEqual(observed["activity"]["source"], "parent_session")
        self.transcript.write_text(json.dumps(self.parent("assistant", self.now + dt.timedelta(seconds=3), [tool_use("Bash", "future")])) + "\n")
        self.assertIsNone(self.observe()["activity"])
        missing = self.parent("assistant", self.at(1), [tool_use("Read", "missing-cwd")])
        del missing["cwd"]
        self.transcript.write_text(json.dumps(missing) + "\n")
        self.assertEqual(self.observe()["activity_unavailable_reason"], "parent_cwd_witness_missing")
        nested = self.worktree / "src"
        nested.mkdir()
        missing["cwd"] = str(nested)
        self.transcript.write_text(json.dumps(missing) + "\n")
        self.assertEqual(self.observe()["activity"]["category"], "read")


class ProgressServiceTests(ProjectFixture):
    def tearDown(self):
        (self.base / "release-model").touch()
        (self.base / "release-gate").touch()
        super().tearDown()

    def implementation_control(self, **value):
        (self.base / "implementation-control.json").write_text(json.dumps(value))

    def implementation_calls(self):
        path = self.base / "implementation-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_model_started(self, count=1, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.implementation_calls()) >= count:
                return
            time.sleep(0.05)
        self.fail("Fake implementation model did not start")

    def wait_state(self, state_path, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = json.loads(Path(state_path).read_text())
            except (OSError, ValueError):
                state = None
            if state and predicate(state):
                return state
            time.sleep(0.05)
        self.fail("Implementation state did not reach the expected phase")

    def waiting_gate(self):
        return [sys.executable, "-c", "import pathlib, time\nwhile not pathlib.Path(" + repr(str(self.base / "release-gate")) + ").exists(): time.sleep(0.05)"]

    def checking_gate(self):
        return [sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]

    def prepare(self, gates=None, **control):
        self.init_git()
        self.review()
        self.approve()
        handoff = self.service.room_handoff(self.room_id, 1, "Build it through independent review.", gates or [self.checking_gate()])
        self.fake.write_text(IMPLEMENTATION_FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.implementation_control(**control)
        manifest = json.loads(Path(handoff["handoff_path"]).read_text())
        return handoff, manifest

    def registry_rows(self):
        with self.service.db() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at")]

    def test_review_job_progress_uses_pinned_review_timeout_and_ignores_later_global_changes(self):
        self.control(wait=True)
        job = self.review(wait=False)
        self.wait_started()
        session = self.service.room_status(self.room_id)["review"]["session_id"]
        review_dir = str(self.room_root / "review")
        now = dt.datetime.now(UTC)
        write_jsonl(Path(job["payload"]["session_transcript"]), [
            record("user", session, review_dir, now, [{"type": "text", "text": "PROMPT_SENTINEL_2c4"}]),
            record("assistant", session, review_dir, now + dt.timedelta(microseconds=1),
                   [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1"}, tool_use("Grep", "toolu_RAWID_SENTINEL", pattern="COMMAND_SENTINEL_rm_rf")])])
        time.sleep(0.001)
        live = self.service.room_job_status(job["id"])
        value = live["progress"]
        self.assertEqual((live["status"], value["phase"], value["outcome"], value["elapsed_basis"]), ("running", "model", "running", "job_started_at"))
        self.assertEqual((value["deadline"]["scope"], value["deadline"]["basis"], value["deadline"]["timeout_seconds"]), ("model_invocation", "pinned_review_timeout", 12))
        self.assertLessEqual(value["deadline"]["remaining_seconds"], 12)
        self.assertEqual((value["activity"]["category"], value["activity"]["event"], value["activity"]["source"]), ("read", "tool_start", "parent_session"))
        self.assertEqual(value["delegates"]["requested"], 0)
        self.assertNotIn("progress_unavailable", value["limitations"])
        config = self.service.settings()
        config["review_timeout_seconds"] = 999
        project_room.atomic_json(self.home / "config.json", config)
        again = self.service.room_job_status(job["id"])["progress"]
        self.assertEqual(again["deadline"]["timeout_seconds"], 12)
        serialized = json.dumps(self.service.room_status(self.room_id))
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        (self.base / "release").touch()
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        frozen = terminal["progress"]
        self.assertEqual((frozen["phase"], frozen["elapsed_basis"], frozen["deadline"], frozen["deadline_unavailable_reason"]), ("terminal", "frozen_at_finish", None, "terminal"))
        self.assertEqual(without_observation_time(frozen), without_observation_time(self.service.room_job_status(job["id"])["progress"]))
        self.assertEqual(len(self.calls()), 1)

    def test_implementation_progress_tracks_model_delegates_gate_start_and_awaiting_review(self):
        handoff, manifest = self.prepare(gates=[self.waiting_gate(), self.checking_gate()], wait=True)
        job = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-progress")
        self.wait_model_started()
        state = self.wait_state(handoff["state_path"], lambda value: value.get("phase") == "running_model")
        self.assertEqual(state["owner_job_id"], job["id"])
        self.assertEqual(state["active_stage"]["kind"], "model")
        session, worktree = manifest["session_id"], manifest["worktree_path"]
        transcript = Path(manifest["session_transcript_path"])
        now = dt.datetime.now(UTC)
        write_jsonl(transcript, [
            record("user", session, worktree, now, [{"type": "text", "text": "PROMPT_SENTINEL_2c4"}]),
            record("assistant", session, worktree, now + dt.timedelta(microseconds=1), [tool_use("Agent", "toolu_one", subagent_type="sonnet-worker", prompt="PROMPT_SENTINEL_2c4")], record_uuid="record-one"),
            record("assistant", session, worktree, now + dt.timedelta(microseconds=2), [tool_use("Agent", "toolu_two", subagent_type="opus-reviewer")], record_uuid="record-two"),
            record("user", session, worktree, now + dt.timedelta(microseconds=3), [tool_result("toolu_one", "RESULT_SENTINEL_stack")])])
        launch = record("user", session, worktree, now + dt.timedelta(microseconds=4), [tool_result("toolu_two", "RESULT_SENTINEL_stack")])
        launch["toolUseResult"] = {"status": "async_launched", "agentId": "alpha"}
        write_jsonl(transcript, [launch])
        write_jsonl(transcript.parent / session / "subagents" / "agent-alpha.jsonl", [
            record("assistant", session, worktree, now + dt.timedelta(microseconds=5), [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1"},
                                                                                     tool_use("Bash", "toolu_child", command="COMMAND_SENTINEL_rm_rf")],
                   sidechain=True, model="claude-opus-5", agentId="alpha", sourceToolAssistantUUID="alpha-own-record")])
        time.sleep(0.001)
        live = self.service.room_job_status(job["id"])
        value = live["progress"]
        self.assertEqual((live["status"], value["phase"], value["phase_detail"], value["attempt"]), ("running", "model", "delegate_pending", 1))
        self.assertEqual((value["deadline"]["basis"], value["deadline"]["timeout_seconds"], value["deadline"]["expired"]), ("pinned_handoff_model_timeout", 3600, False))
        self.assertEqual({key: value["delegates"][key] for key in ("requested", "pending", "background", "completed", "observed_children", "attributed_children")},
                         {"requested": 2, "pending": 0, "background": 1, "completed": 1, "observed_children": 1, "attributed_children": 1})
        launched, completed = value["delegates"]["items"]
        self.assertEqual((completed["requested_role"], completed["state"], completed["child"]), ("sonnet-worker", "completed", None))
        self.assertEqual((launched["requested_role"], launched["state"], launched["child"]["observed_model"], launched["child"]["last_category"], launched["child"]["turn_ended"]),
                         ("opus-reviewer", "background", "claude-opus-5", "shell", None))
        self.assertEqual((value["activity"]["source"], value["activity"]["category"]), ("child_session", "shell"))
        self.assertEqual(value["limitations"], [])
        status = self.service.room_status(self.room_id)
        self.assertEqual({item["id"]: item["progress"]["phase"] for item in status["jobs"]}[job["id"]], "model")
        serialized = json.dumps(status)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        config = self.service.settings()
        config["implementation_timeout_seconds"] = 7
        project_room.atomic_json(self.home / "config.json", config)
        self.assertEqual(self.service.room_job_status(job["id"])["progress"]["deadline"]["timeout_seconds"], 3600)
        (self.base / "release-model").touch()
        state = self.wait_state(handoff["state_path"], lambda value: value.get("phase") == "running_gates" and value.get("active_stage"))
        self.assertEqual(state["gate_results"], [], "The gate start must be saved before the gate finishes")
        self.assertEqual({key: state["active_stage"][key] for key in ("kind", "index")}, {"kind": "gate", "index": 1})
        self.assertIsNotNone(state["model_finished_at"], "The model stage end is saved before evidence verification")
        gate = self.service.room_job_status(job["id"])["progress"]
        self.assertEqual((gate["phase"], gate["gate"], gate["attempt"]), ("gate", {"index": 1, "count": 2}, 1))
        self.assertEqual((gate["deadline"]["scope"], gate["deadline"]["basis"], gate["deadline"]["timeout_seconds"]), ("gate", "pinned_handoff_gate_timeout", 300))
        self.assertEqual(gate["deadline"]["started_at"], progress.stamp(progress.parse_time(state["active_stage"]["started_at"])))
        self.assertEqual((gate["activity"], gate["activity_unavailable_reason"], gate["delegates"]), (None, "gate_phase", None))
        (self.base / "release-gate").touch()
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(terminal["result"]["phase"], "awaiting_astra_review")
        final = terminal["progress"]
        self.assertEqual((final["phase"], final["attempt"], final["elapsed_basis"], final["deadline_unavailable_reason"]), ("awaiting_review", 1, "frozen_at_finish", "awaiting_product_review"))
        self.assertEqual((final["gate"], final["delegates"], final["activity"]), (None, None, None))
        saved = json.loads(Path(handoff["state_path"]).read_text())
        self.assertIsNone(saved["active_stage"])
        self.assertEqual(saved["owner_job_id"], job["id"])
        self.assertEqual([gate_result["argv"] for gate_result in saved["gate_results"]], [self.waiting_gate(), self.checking_gate()])
        self.assertTrue(all(gate_result["started_at"] <= gate_result["finished_at"] for gate_result in saved["gate_results"]))
        self.service.room_implementation_review(self.room_id, handoff["handoff_id"], True, "Inspected the candidate and gate evidence.")
        self.assertEqual(without_observation_time(self.service.room_job_status(job["id"])["progress"]), without_observation_time(final))
        self.assertEqual(len(self.implementation_calls()), 1)

    def test_prior_job_stays_frozen_and_queued_correction_does_not_inherit_old_attempt(self):
        handoff, manifest = self.prepare()
        first = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-1")
        done = self.service.room_job_status(first["id"], 15)
        self.assertEqual(done["status"], "succeeded", done)
        frozen = without_observation_time(done["progress"])
        self.assertEqual((frozen["phase"], frozen["attempt"]), ("awaiting_review", 1))
        session, worktree = manifest["session_id"], manifest["worktree_path"]
        transcript = Path(manifest["session_transcript_path"])
        now = dt.datetime.now(UTC)
        write_jsonl(transcript, [
            record("assistant", session, worktree, now, [tool_use("Agent", "toolu_old", subagent_type="sonnet-worker")], record_uuid="record-old"),
            record("user", session, worktree, now + dt.timedelta(milliseconds=5), [tool_result("toolu_old")])])
        self.service.room_implementation_revise(self.room_id, handoff["handoff_id"], "Diagnosed: keep the feature file and rerun gates.")
        self.implementation_control(wait=True)
        second = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-2")
        early = self.service.room_job_status(second["id"])["progress"]
        self.assertNotIn(early["phase"], ("awaiting_review", "gate", "terminal", "finalizing"))
        self.assertIn(early["attempt"], (None, 2))
        self.assertEqual((early["gate"], early["delegates"]), (None, None))
        if early["phase"] in ("queued", "starting"):
            self.assertEqual((early["deadline"], early["deadline_unavailable_reason"]), (None, early["phase"]))
        self.wait_model_started(2)
        self.wait_state(handoff["state_path"], lambda value: value.get("phase") == "running_model" and value.get("owner_job_id") == second["id"])
        live = self.service.room_job_status(second["id"])["progress"]
        self.assertEqual((live["phase"], live["attempt"], live["deadline"]["basis"]), ("model", 2, "pinned_handoff_model_timeout"))
        self.assertEqual((live["activity"], live["activity_unavailable_reason"]), (None, "no_records_in_attempt_window"))
        self.assertEqual((live["delegates"]["requested"], live["delegates"]["completed"]), (0, 0))
        write_jsonl(transcript, [record("assistant", session, worktree, dt.datetime.now(UTC), [tool_use("Agent", "toolu_new", subagent_type="opus-reviewer")], record_uuid="record-new")])
        fresh = self.service.room_job_status(second["id"])["progress"]
        self.assertEqual((fresh["phase_detail"], fresh["delegates"]["requested"], fresh["delegates"]["pending"]), ("delegate_pending", 1, 1))
        self.assertEqual(without_observation_time(self.service.room_job_status(first["id"])["progress"]), frozen)
        (self.base / "release-model").touch()
        terminal = self.service.room_job_status(second["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual((terminal["progress"]["phase"], terminal["progress"]["attempt"]), ("awaiting_review", 2))
        self.assertEqual(without_observation_time(self.service.room_job_status(first["id"])["progress"]), frozen)
        self.assertEqual(len(self.implementation_calls()), 2)

    def test_repeated_progress_reads_cannot_spawn_cancel_replay_or_extend_deadlines(self):
        handoff, _ = self.prepare(wait=True)
        job = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-readonly")
        self.wait_model_started()
        self.wait_state(handoff["state_path"], lambda value: value.get("phase") == "running_model")
        state_path = Path(handoff["state_path"])
        before_state, before_rows, before_calls = state_path.read_bytes(), self.registry_rows(), len(self.implementation_calls())
        watched = [self.service._job_path(job["id"]), state_path.parent, self.base / "claude-storage"]
        before_files = {str(path) for root in watched for path in root.rglob("*")}
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=AssertionError("progress must not spawn")), \
                mock.patch.object(implementation, "run_implementation", side_effect=AssertionError("progress must not run")), \
                mock.patch.object(room, "ask", side_effect=AssertionError("progress must not ask")), \
                mock.patch.object(project_room, "atomic_json", side_effect=AssertionError("progress must not write")), \
                mock.patch.object(implementation, "_atomic", side_effect=AssertionError("progress must not write")), \
                mock.patch.object(project_room.os, "replace", side_effect=AssertionError("progress must not replace files")):
            for _ in range(3):
                live = self.service.room_job_status(job["id"])
                status = self.service.room_status(self.room_id)
            self.assertEqual((live["status"], live["progress"]["phase"]), ("running", "model"))
            self.assertEqual({item["id"]: item["status"] for item in status["jobs"]}[job["id"]], "running")
            far_future = dt.datetime.now(UTC) + dt.timedelta(days=2)
            with mock.patch.object(progress, "clock", return_value=far_future):
                overdue = self.service.room_job_status(job["id"])
            self.assertEqual((overdue["status"], overdue["progress"]["deadline"]["remaining_seconds"], overdue["progress"]["deadline"]["expired"]), ("running", 0, True))
            self.assertEqual(overdue["progress"]["observed_at"], progress.stamp(far_future))
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(self.registry_rows(), before_rows)
        self.assertEqual(len(self.implementation_calls()), before_calls)
        self.assertEqual({str(path) for root in watched for path in root.rglob("*")} - before_files, set(), "Observation must create no files")
        self.assertFalse((self.service._job_path(job["id"]) / "cancel.json").exists())
        self.assertNotIn("progress_unavailable", live["progress"]["limitations"])
        (self.base / "release-model").touch()
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual((terminal["status"], terminal["result"]["phase"]), ("succeeded", "awaiting_astra_review"))
        self.assertEqual(len(self.implementation_calls()), 1)

    def test_legacy_active_worker_without_stage_telemetry_stays_readable_with_null_gate_deadline(self):
        handoff, _ = self.prepare(gates=[self.waiting_gate(), self.checking_gate()], wait=True)
        job = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-legacy")
        self.wait_model_started()
        state_path = Path(handoff["state_path"])
        state = self.wait_state(state_path, lambda value: value.get("phase") == "running_model")
        for key in ("owner_job_id", "active_stage"):
            state.pop(key)
        state_path.write_text(json.dumps(state))
        legacy_model = self.service.room_job_status(job["id"])["progress"]
        self.assertEqual((legacy_model["phase"], legacy_model["deadline"]["basis"], legacy_model["deadline"]["timeout_seconds"]), ("model", "pinned_handoff_model_timeout", 3600))
        self.assertIn("attempt_binding_inferred", legacy_model["limitations"])
        (self.base / "release-model").touch()
        state = self.wait_state(state_path, lambda value: value.get("phase") == "running_gates" and value.get("active_stage"))
        for key in ("owner_job_id", "active_stage"):
            state.pop(key)
        state_path.write_text(json.dumps(state))
        legacy_gate = self.service.room_job_status(job["id"])["progress"]
        self.assertEqual((legacy_gate["phase"], legacy_gate["gate"]), ("gate", {"index": 1, "count": 2}))
        self.assertEqual((legacy_gate["deadline"], legacy_gate["deadline_unavailable_reason"]), (None, "gate_start_unavailable_legacy_worker"))
        self.assertIn("attempt_binding_inferred", legacy_gate["limitations"])
        self.assertIsInstance(legacy_gate["elapsed_seconds"], int)
        (self.base / "release-gate").touch()
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual((terminal["status"], terminal["result"]["phase"]), ("succeeded", "awaiting_astra_review"))
        self.assertEqual(terminal["progress"]["phase"], "awaiting_review")

    def test_scope_change_outcome_reports_terminal_progress_without_review_phase(self):
        marker = self.base / "gate-must-not-run"
        handoff, _ = self.prepare(gates=[[sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()"]], scope=True)
        job = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-scope")
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual((terminal["status"], terminal["result"]["phase"]), ("succeeded", "scope_change"))
        self.assertFalse(marker.exists())
        value = terminal["progress"]
        self.assertEqual((value["phase"], value["outcome"], value["attempt"], value["deadline_unavailable_reason"]), ("terminal", "succeeded", 1, "terminal"))
        self.assertEqual(value["elapsed_basis"], "frozen_at_finish")
        self.assertNotIn("DO_NOT_EXPOSE_PRIVATE_THINKING", json.dumps(terminal))


class ProgressMcpTests(unittest.TestCase):
    def test_progress_is_exposed_through_mcp_status_tools_without_private_metadata(self):
        fixture = test_project_room_mcp.ProjectRoomMcpTests(methodName="test_initialize_discovery_and_fake_auth_have_protocol_only_stdout")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.control(wait=True)
        job = fixture.tool("room_review_submit", {"room_id": fixture.room_id, "revision": 1, "message": "Review via MCP", "request_id": "mcp-progress"})
        fixture.wait_started()
        session = fixture.service.room_status(fixture.room_id)["review"]["session_id"]
        review_dir = str(fixture.room_root / "review")
        now = dt.datetime.now(UTC)
        write_jsonl(Path(job["payload"]["session_transcript"]), [
            record("assistant", session, review_dir, now, [{"type": "thinking", "thinking": "THINKING_SENTINEL_9f1"},
                                                            tool_use("Read", "toolu_RAWID_SENTINEL", file_path="FILE_PATH_SENTINEL_secret_txt")])])
        response = fixture.request("tools/call", {"name": "room_job_status", "arguments": {"job_id": job["id"], "wait_seconds": 0}})
        self.assertFalse(response["result"]["isError"], response)
        value = response["result"]["structuredContent"]["progress"]
        self.assertEqual((value["schema_version"], value["phase"], value["activity"]["category"]), (1, "model", "read"))
        self.assertEqual(value["deadline"]["basis"], "pinned_review_timeout")
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["progress"], value)
        serialized = json.dumps(response)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, serialized)
        status = fixture.tool("room_status", {"room_id": fixture.room_id})
        self.assertEqual([item["progress"]["phase"] for item in status["jobs"]], ["model"])
        (fixture.base / "release").touch()
        terminal = fixture.tool("room_job_status", {"job_id": job["id"], "wait_seconds": 15})
        self.assertEqual((terminal["status"], terminal["progress"]["phase"], terminal["progress"]["elapsed_basis"]), ("succeeded", "terminal", "frozen_at_finish"))
        discovered = fixture.request("tools/list")["result"]["tools"]
        self.assertEqual(len(discovered), 21)
        descriptions = {tool["name"]: tool["description"] for tool in discovered}
        self.assertIn("progress", descriptions["room_job_status"])
        self.assertIn("not an ETA", descriptions["room_job_status"])
        self.assertIn("progress", descriptions["room_status"])


if __name__ == "__main__":
    unittest.main()
