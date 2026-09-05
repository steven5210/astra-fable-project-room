"""Registry and supervised-job regressions. All Claude processes are local fakes."""

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

import implementation
import project_room
import room


ROOT = Path(__file__).resolve().parent
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "fixture", "private": "DO_NOT_EXPOSE"}))
    raise SystemExit(0)
control = json.loads((root / "control.json").read_text())
prompt = sys.stdin.read()
packet = json.loads(prompt.split("REVIEW PACKET (JSON):\n", 1)[1])
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": args, "packet": packet, "session": session, "cwd": os.getcwd(), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "pid": os.getpid()}) + "\n")
if control.get("wait"):
    while not (root / "release").exists():
        time.sleep(.05)
time.sleep(control.get("delay", 0))
if control.get("malformed"):
    print("not a verified result")
    raise SystemExit(0)
review = {"interpretation": "Saved filters require a nonempty user-provided name.",
          "findings": control.get("findings", []), "decision": control.get("decision", "accept"),
          "spec_revision": packet["spec_revision"], "spec_sha256": packet["spec_sha256"]}
result = {"type": "result", "subtype": "success", "is_error": False,
          "session_id": session, "modelUsage": {control.get("model", "claude-fable-5-1"): {"outputTokens": 1}},
          "result": "Public independent review.", "structured_output": review}
if control.get("mixed"):
    result["modelUsage"]["fixture-helper"] = {"outputTokens": 1}
    transcript = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture-hashed-directory" / (session + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    event = {"type": "assistant", "sessionId": session, "cwd": os.getcwd(),
             "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "message": {"id": "fixture-review-message", "model": "claude-fable-5-1", "content": [
                 {"type": "thinking", "thinking": "PRIVATE_REVIEW_THINKING_MUST_NOT_BE_EXPOSED"},
                 {"type": "tool_use", "id": "fixture-output", "name": "StructuredOutput", "input": review}]}}
    with transcript.open("a") as evidence:
        evidence.write(json.dumps(event) + "\n")
print(json.dumps(result))
'''


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="project-registry-test-")
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "state"
        self.project = self.base / "project"
        self.project.mkdir()
        self.fake = self.base / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.fake.chmod(0o700)
        self.control()
        self.service = project_room.Service(self.home)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.base / "claude-storage")}):
            self.service.setup(claude_bin=str(self.fake))
        config = self.service.settings()
        config["review_timeout_seconds"] = 12
        project_room.atomic_json(self.home / "config.json", config)
        self.entry = self.service.room_open(str(self.project), "Saved filters")
        self.room_id = self.entry["id"]
        self.room_root = Path(self.entry["path"])
        self.service.room_spec_put(self.room_id, 1, "# Saved filters\r\nNames must be nonempty.\r\n")

    def tearDown(self):
        # Release only our fake, then ask actual owning workers to finish/cancel.
        (self.base / "release").touch()
        try:
            with self.service.db() as db:
                ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')")]
            for identifier in ids:
                self.service.room_job_cancel(identifier)
                self.service.room_job_status(identifier, 15)
        finally:
            self.temp.cleanup()

    def control(self, **value):
        (self.base / "control.json").write_text(json.dumps(value))

    def calls(self):
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_started(self, count=1, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.calls()) >= count:
                return self.calls()[-1]
            time.sleep(.05)
        self.fail("Fake Claude did not start; " + json.dumps(self.service.room_status(self.room_id)))

    def review(self, request_id="review-1", revision=1, message="Review independently", wait=True):
        submitted = self.service.room_review_submit(self.room_id, revision, message, request_id)
        if not wait:
            return submitted
        result = self.service.room_job_status(submitted["id"], 15)
        self.assertEqual(result["status"], "succeeded", result)
        return result

    def approve(self, revision=1):
        return self.service.room_record(self.room_id, "astra", "approval", revision, "Acceptance criteria and scope verified.")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def init_git(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Project Room Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.hooksPath", "/dev/null")
        (self.project / "README.md").write_text("Fixture project\n")
        self.git("add", "README.md")
        self.git("-c", "commit.gpgSign=false", "commit", "-qm", "Fixture baseline")

    def insert_orphan(self, status="running", pid=None, age_seconds=60):
        identifier = uuid.uuid4().hex
        self.service._job_path(identifier).mkdir(parents=True)
        stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)).isoformat()
        with self.service.db() as db:
            db.execute("INSERT INTO jobs(id,room_id,kind,request_key,payload,status,created_at,pid) VALUES(?,?,?,?,?,?,?,?)",
                       (identifier, self.room_id, "review", "orphan", "{}", status, stamp, pid))
        return identifier


class ProjectRoomTests(ProjectFixture):
    def test_reopening_canonical_feature_preserves_session_spec_and_config_snapshot(self):
        initial = self.service.room_status(self.room_id)
        pinned = (self.room_root / "settings.json").read_bytes()
        second_fake = self.base / "fake-claude-new"
        second_fake.write_text(self.fake.read_text())
        second_fake.chmod(0o700)
        self.service.setup(claude_bin=str(second_fake))
        fresh = project_room.Service(self.home)
        reopened = fresh.room_open(str(self.project / "."), " Saved filters ")
        self.assertTrue(reopened["existing"])
        self.assertEqual(reopened["id"], self.room_id)
        self.assertEqual((self.room_root / "settings.json").read_bytes(), pinned)
        self.assertEqual(fresh.room_status(self.room_id)["review"]["session_id"], initial["review"]["session_id"])
        self.assertEqual(len(fresh.room_list(str(self.project))["rooms"]), 1)
        self.review()
        self.assertEqual(self.calls()[0]["config_dir"], str(self.base / "claude-storage"))
        next_room = fresh.room_open(str(self.project), "Another feature")
        self.assertNotEqual(next_room["id"], self.room_id)
        self.assertEqual(json.loads((Path(next_room["path"]) / "settings.json").read_text())["claude_bin"], str(second_fake))

    def test_replay_survives_service_restart_and_changed_content_never_launches(self):
        initial = self.review()
        fresh = project_room.Service(self.home)
        duplicate = fresh.room_review_submit(self.room_id, 1, "Review independently", "review-1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["id"], initial["id"])
        self.assertEqual(duplicate["result"], initial["result"])
        with self.assertRaisesRegex(room.RoomError, "different content"):
            fresh.room_review_submit(self.room_id, 1, "Changed request", "review-1")
        self.review("review-2")
        self.assertEqual(len(self.calls()), 2)
        first, second = self.calls()
        self.assertEqual(first["session"], second["session"])
        self.assertIn("--session-id", first["argv"])
        self.assertIn("--resume", second["argv"])
        fresh.room_spec_put(self.room_id, 2, "A newer current spec")
        cached_old = fresh.room_review_submit(self.room_id, 1, "Review independently", "review-1")
        self.assertEqual(cached_old["id"], initial["id"])
        self.assertTrue(cached_old["duplicate"])
        self.assertEqual(len(self.calls()), 2)

    def test_overlong_predicted_review_path_reconciles_saved_mixed_reply_without_replay(self):
        settings = self.service.settings()
        self.home = self.base / ("long-registry-" + "x" * 170)
        self.service = project_room.Service(self.home)
        project_room.atomic_json(self.home / "config.json", settings)
        self.entry = self.service.room_open(str(self.project), "Saved filters")
        self.room_id, self.room_root = self.entry["id"], Path(self.entry["path"])
        self.service.room_spec_put(self.room_id, 1, "# Saved filters\nNames must be nonempty.\n")
        self.control(mixed=True)
        job = self.review(wait=False)
        predicted = Path(job["payload"]["session_transcript"])
        self.assertGreater(len(predicted.parent.name.encode()), 255)
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        turn = terminal["result"]
        self.assertTrue(turn["reconciled"])
        self.assertEqual(turn["primary_model"], project_room.MODEL)
        self.assertEqual(turn["auxiliary_models"], ["fixture-helper"])
        self.assertEqual(turn["return_code_basis"], "persisted subprocess return code")
        with contextlib.closing(room.connect(self.room_root / "review")) as db:
            audits = db.execute("SELECT original_turn_json,evidence_json FROM reconciliations").fetchall()
        self.assertEqual(len(audits), 1)
        original, evidence = (json.loads(audits[0][key]) for key in ("original_turn_json", "evidence_json"))
        self.assertEqual(original["status"], "failed")
        self.assertIn("Model identity verification failed:", original["error"])
        self.assertEqual(original["return_code"], 0)
        self.assertEqual(room.sha(Path(original["stdout_path"]).read_bytes()), original["stdout_sha256"])
        self.assertEqual(Path(evidence["path"]).parent.name, "fixture-hashed-directory")
        self.assertEqual(evidence["session_id"], self.calls()[0]["session"])
        duplicate = self.review(wait=False)
        self.assertEqual(duplicate["id"], job["id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.calls()), 1, "Recovery must validate saved output, never invoke the model again")
        self.assertNotIn("PRIVATE_REVIEW_THINKING_MUST_NOT_BE_EXPOSED", json.dumps(self.service.room_status(self.room_id)))

    def test_pending_job_blocks_mutation_and_duplicate_returns_the_same_job(self):
        self.control(wait=True)
        job = self.review(wait=False)
        self.wait_started()
        duplicate = self.review(wait=False)
        self.assertEqual(duplicate["id"], job["id"])
        for operation in (
            lambda: self.service.room_spec_put(self.room_id, 2, "New spec"),
            lambda: self.approve(),
            lambda: self.review("another", wait=False),
        ):
            with self.assertRaisesRegex(room.RoomError, "blocked by .* job"):
                operation()
        self.assertEqual(len(self.calls()), 1)
        (self.base / "release").touch()
        self.assertEqual(self.service.room_job_status(job["id"], 15)["status"], "succeeded")

    def test_control_lock_rejects_cross_process_mutation_without_partial_spec(self):
        with room.lock_room(self.room_root / "control"):
            command = [sys.executable, str(ROOT / "project_room.py"), "--home", str(self.home), "call", "room_spec_put", "--args",
                       json.dumps({"room_id": self.room_id, "revision": 2, "content": "Must not be published"})]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.service.room_status(self.room_id)["review"]["current_revision"], 1)
        self.assertFalse(list((self.room_root / "inputs").glob("spec-2-*")))

    def test_orphan_marks_uncertain_without_signalling_stale_stored_pid(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            identifier = self.insert_orphan(pid=sleeper.pid)
            status = self.service.room_job_status(identifier)
            self.assertEqual(status["status"], "uncertain")
            self.assertFalse(self.service.room_job_cancel(identifier)["cancel_requested"])
            self.assertIsNone(sleeper.poll(), "Stored PID must never be used as signal authority")
            with self.assertRaisesRegex(room.RoomError, "uncertain"):
                self.review("new", wait=False)
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)

    def test_held_worker_lease_overrides_old_timestamp_then_detects_disappearance(self):
        identifier = self.insert_orphan()
        with (self.service._job_path(identifier) / "worker.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(project_room.Service(self.home).room_job_status(identifier)["status"], "running")
        self.assertEqual(self.service.room_job_status(identifier)["status"], "uncertain")

    def test_cancellation_before_worker_start_has_known_outcome_and_no_model_call(self):
        identifier = self.insert_orphan(status="queued", age_seconds=0)
        self.assertTrue(self.service.room_job_cancel(identifier)["cancel_requested"])
        self.service.worker(identifier)
        self.assertEqual(self.service.room_job_status(identifier)["status"], "cancelled")
        self.assertEqual(self.calls(), [])
        self.review("after-cancel")

    def test_worker_spawn_failure_is_known_failed_and_does_not_quarantine_room(self):
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=OSError("fixture process launch failure")):
            job = self.review(wait=False)
        self.assertEqual(job["status"], "failed")
        self.assertIn("did not start", job["error"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.review(wait=False)["id"], job["id"])
        self.review("after-spawn-failure")

    def test_cancel_running_fake_stops_owned_operation_and_blocks_blind_replay(self):
        self.control(wait=True)
        job = self.review(wait=False)
        self.wait_started()
        self.assertTrue(self.service.room_job_cancel(job["id"])["cancel_requested"])
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "uncertain", terminal)
        duplicate = self.review(wait=False)
        self.assertEqual(duplicate["id"], job["id"])
        self.assertEqual(duplicate["status"], "uncertain")
        with self.assertRaisesRegex(room.RoomError, "uncertain"):
            self.review("new-request", wait=False)
        self.assertEqual(len(self.calls()), 1)

    def test_malformed_model_output_is_durable_and_not_replayed(self):
        self.control(malformed=True)
        job = self.review(wait=False)
        result = self.service.room_job_status(job["id"], 15)
        self.assertIn(result["status"], ("failed", "uncertain"))
        self.assertEqual(self.review(wait=False)["id"], job["id"])
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.service.room_status(self.room_id)["ready_for_handoff"])

    def test_findings_need_dispositions_and_current_consensus_before_real_handoff(self):
        self.init_git()
        baseline = self.git("rev-parse", "HEAD")
        self.control(decision="changes_required", findings=["BLOCKER: Reject empty names.", "SUGGESTION: Sharing UI."])
        self.review()
        self.approve()
        status = self.service.room_status(self.room_id)
        blocker, suggestion = status["issues"]
        self.assertEqual((blocker["severity"], suggestion["severity"]), ("blocker", "suggestion"))
        gates = [[sys.executable, "-c", "assert 2 + 2 == 4"]]
        with self.assertRaisesRegex(room.RoomError, "disposition"):
            self.service.room_handoff(self.room_id, 1, "Build it through review.", gates)
        with self.assertRaisesRegex(room.RoomError, "cannot be deferred"):
            self.service.room_issue_dispose(self.room_id, blocker["id"], "deferred", "Later", 1)
        self.service.room_issue_dispose(self.room_id, suggestion["id"], "deferred", "Outside requested scope", 1)
        self.service.room_spec_put(self.room_id, 2, "# Saved filters\nReject empty and whitespace-only names; no sharing UI.\n")
        with self.assertRaisesRegex(room.RoomError, "Stale revision"):
            self.service.room_issue_dispose(self.room_id, blocker["id"], "addressed", "Added validation", 1)
        self.service.room_issue_dispose(self.room_id, blocker["id"], "addressed", "Revision two specifies the validation", 2)
        self.assertFalse(self.service.room_status(self.room_id)["ready_for_handoff"])
        self.control()
        self.review("review-v2", revision=2)
        self.approve(2)
        self.assertTrue(self.service.room_status(self.room_id)["ready_for_handoff"])
        handoff = self.service.room_handoff(self.room_id, 2, "Build it through review.", gates)
        self.assertEqual(handoff["phase"], "prepared")
        self.assertTrue(Path(handoff["worktree_path"]).is_dir())
        self.assertEqual(self.git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.git("status", "--porcelain"), "")
        history = self.service.room_history(self.room_id)
        self.assertTrue(any(event["kind"] == "backlog" for event in history["events"]))
        self.service.room_spec_put(self.room_id, 3, "Another changed requirement")
        with self.assertRaisesRegex(room.RoomError, "no longer matches"):
            self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implement-stale")
        self.assertEqual(len(self.calls()), 2)

    def test_user_approval_is_not_astra_approval_and_revision_bytes_are_immutable(self):
        self.review()
        self.service.room_record(self.room_id, "user", "approval", 1, "User approves feature")
        self.assertFalse(self.service.room_status(self.room_id)["ready_for_handoff"])
        self.approve()
        self.assertTrue(self.service.room_status(self.room_id)["ready_for_handoff"])
        with self.assertRaisesRegex(room.RoomError, "immutable"):
            self.service.room_spec_put(self.room_id, 1, "# Saved filters\nNames must be nonempty.\n")

    def test_implementation_scope_change_requires_new_spec_consensus_before_handoff(self):
        from test_implementation import FAKE as IMPLEMENTATION_FAKE
        self.init_git()
        self.review()
        self.approve()
        marker = self.base / "scope-gate-must-not-run"
        gates = [[sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()"]]
        handoff = self.service.room_handoff(self.room_id, 1, "Build it through independent review.", gates)
        review_executable = self.fake.read_text()
        implementation_fake = IMPLEMENTATION_FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
        implementation_fake = implementation_fake.replace(
            '    with (root / (session + ".jsonl")).open("a") as out:',
            '    transcript = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture-hashed-directory" / (session + ".jsonl")\n'
            '    transcript.parent.mkdir(parents=True, exist_ok=True)\n'
            '    event["cwd"] = os.getcwd()\n'
            '    with transcript.open("a") as out:')
        self.fake.write_text(implementation_fake)
        (self.base / "implementation-mode.txt").write_text("scope")
        job = self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "implementation-scope")
        terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(terminal["result"]["phase"], "scope_change")
        self.assertFalse(marker.exists(), "Scope discovery must return before gates")
        status = self.service.room_status(self.room_id)
        self.assertFalse(status["ready_for_handoff"])
        issue = next(issue for issue in status["issues"] if issue["job_id"] == job["id"])
        self.assertEqual((issue["severity"], issue["disposition"]), ("blocker", "open"))
        self.assertNotIn("DO_NOT_EXPOSE_PRIVATE_THINKING", json.dumps(status))
        with self.assertRaisesRegex(room.RoomError, "newer spec"):
            self.service.room_issue_dispose(self.room_id, issue["id"], "rejected", "Would otherwise reuse stale approval", 1)
        with self.assertRaisesRegex(room.RoomError, "disposition"):
            self.service.room_handoff(self.room_id, 1, "Build it through independent review.", gates)
        with self.assertRaisesRegex(room.RoomError, "no longer matches"):
            self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "unsafe-replay")
        history = self.service.room_history(self.room_id)
        self.assertIn("product decision", history["review_history"])
        self.assertTrue(any(event["kind"] == "scope_change" for event in history["events"]))
        self.service.room_spec_put(self.room_id, 2, "Resolve the product decision explicitly in revised acceptance criteria.")
        self.service.room_issue_dispose(self.room_id, issue["id"], "addressed", "Revision two incorporates the decision", 2)
        self.approve(2)
        self.assertFalse(self.service.room_status(self.room_id)["ready_for_handoff"])
        self.fake.write_text(review_executable)
        self.review("review-after-scope", revision=2)
        self.assertTrue(self.service.room_status(self.room_id)["ready_for_handoff"])
        with self.assertRaisesRegex(room.RoomError, "no longer matches"):
            self.service.room_implementation_submit(self.room_id, handoff["handoff_id"], "stale-after-new-consensus")

    def test_bounded_waits_reject_invalid_numbers_before_accessing_state(self):
        for value in (True, -1, 46, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaisesRegex(room.RoomError, "wait_seconds"):
                self.service.room_job_status("0" * 32, value)

    def test_stale_review_is_rejected_before_creating_an_uncertain_job(self):
        self.service.room_spec_put(self.room_id, 2, "Current revision two")
        with self.assertRaisesRegex(room.RoomError, "Stale revision"):
            self.review("stale-review", revision=1, wait=False)
        self.assertEqual(self.service.room_status(self.room_id)["jobs"], [])
        self.assertEqual(self.calls(), [])
        self.review("current-review", revision=2)

    def test_separate_feature_rooms_can_run_concurrently_without_shared_session(self):
        self.control(wait=True)
        other = self.service.room_open(str(self.project), "Export filters")
        self.service.room_spec_put(other["id"], 1, "Export saved filters")
        first = self.review(wait=False)
        second = self.service.room_review_submit(other["id"], 1, "Review export", "review-1")
        self.wait_started(count=2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len({call["session"] for call in self.calls()}), 2)
        (self.base / "release").touch()
        for job in (first, second):
            self.assertEqual(self.service.room_job_status(job["id"], 15)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
