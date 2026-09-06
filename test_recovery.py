"""Audited continuation after a stopped implementation; fake Claude, temporary Git repositories, real OS observation."""

import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

import implementation
import project_room
import recovery
import room
from test_project_room import ProjectFixture


FAKE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
mode = (root / "implementation-mode.txt").read_text().strip()
argv = sys.argv[1:]
prompt = sys.stdin.read()
packet = json.loads(prompt.split("IMPLEMENTATION PACKET (JSON):\n", 1)[1])
session = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
with (root / "implementation-calls.jsonl").open("a") as log:
    log.write(json.dumps({"argv": argv, "session": session, "cwd": os.getcwd(), "packet": packet, "pid": os.getpid()}) + "\n")
transcript = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture-hashed-directory" / (session + ".jsonl")
transcript.parent.mkdir(parents=True, exist_ok=True)
def emit(event):
    with transcript.open("a") as out:
        out.write(json.dumps(event) + "\n")
stamp = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
emit({"type": "user", "sessionId": session, "cwd": os.getcwd(), "timestamp": stamp(), "message": {"content": "PRIVATE_PROMPT_CANARY"}})
emit({"type": "assistant", "sessionId": session, "cwd": os.path.join(os.getcwd(), "apps", "backend", "src"), "timestamp": stamp(),
      "isSidechain": False, "message": {"content": "changed into a project subdirectory within the same worktree"}})
if mode.startswith("interrupt"):
    pathlib.Path("feature.txt").write_text("partial\n")
    pathlib.Path("notes.txt").write_text("TODO: finish; tests pass (unverified claim)\n")
    if mode == "interrupt-timeout":
        time.sleep(30)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error", "api_error_status": 429,
                      "stop_reason": "stop_sequence", "session_id": session, "result": "You've hit your session limit · resets 2pm (America/Los_Angeles)",
                      "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                      "modelUsage": {"claude-fable-5-1": {"inputTokens": 4321, "outputTokens": 99}}, "duration_ms": 5, "num_turns": 3}))
    sys.exit(1)
pathlib.Path("feature.txt").write_text("implemented\n")
report = {"summary": "Completed the fixture feature.", "spec_revision": packet["spec_revision"], "spec_sha256": packet["spec_sha256"],
          "baseline_commit": packet["baseline_commit"], "implementation_complete": True, "outcome": "completed", "scope_change": "", "backlog": [],
          "routing_log": [{"task": "fixture", "tier": "fable", "requested_model": "claude-fable-5-1", "actual_model": "claude-fable-5-1",
                           "reason": "bounded", "result": "done", "fixes": [], "escalation": "none"}],
          "changes": ["feature.txt"], "tests_reported": ["gate"], "review_findings": [], "remaining_gaps": []}
if mode == "wrong-producer":
    producer = "helper-model"
else:
    producer = "claude-fable-5-1"
emit({"type": "assistant", "sessionId": session, "cwd": os.getcwd(), "timestamp": stamp(),
      "message": {"id": "fixture-message", "model": producer, "content": [{"type": "thinking", "thinking": "DO_NOT_EXPOSE_PRIVATE_THINKING"},
                  {"type": "tool_use", "name": "StructuredOutput", "id": "tool-fixture", "input": report}]}})
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
                  "modelUsage": {"claude-fable-5-1": {}, "helper-model": {}}, "structured_output": report}))
'''
SPEC_QUOTA_TEXT = "You've hit your session limit · resets 2pm (America/Los_Angeles)"  # literal from the pinned spec, not the fixture


def fixed_inspector(boot_offset_seconds=None, processes=(), boot=None, error=None):
    def inspect():
        if error:
            raise recovery.ObservationError(error, "fixture")
        seconds = boot if boot is not None else int(time.time()) + (boot_offset_seconds or 0)
        return {"boot_time": seconds, "boot_source": "fixture", "method": "fixture", "processes": list(processes), "skipped": 0}
    return inspect


class RecoveryTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.init_git()
        self.review()
        self.approve()
        self.impl_fake = self.base / "fake-implementation"
        self.impl_fake.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.impl_fake.chmod(0o700)
        self.mode = self.base / "implementation-mode.txt"
        self.mode.write_text("normal")
        profile_path = self.room_root / "profiles/implementation.json"
        profile = json.loads(profile_path.read_text())
        profile.update(claude_bin=str(self.impl_fake), timeout_seconds=2)
        project_room.atomic_json(profile_path, profile)
        self.gates = [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]
        self.handoff = self.service.room_handoff(self.room_id, 1, "Build it through independent review.", self.gates)
        self.handoff_id = self.handoff["handoff_id"]
        self.handoff_dir = Path(self.handoff["handoff_path"]).parent
        self.worktree = Path(self.handoff["worktree_path"])
        self.authorization = "The user said: yes, continue the interrupted implementation after the restart."

    def impl_calls(self):
        path = self.base / "implementation-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def interrupt(self, mode, request_id="implement-1"):
        self.mode.write_text(mode)
        job = self.service.room_implementation_submit(self.room_id, self.handoff_id, request_id)
        terminal = self.service.room_job_status(job["id"], 40)
        self.assertEqual(terminal["status"], "uncertain", terminal)
        self.assertEqual(terminal["result"]["phase"], "blocked")
        self.mode.write_text("normal")
        return terminal

    def state(self):
        return json.loads((self.handoff_dir / "state.json").read_text())

    def attempt_dir(self):
        return Path(self.state()["attempt_path"])

    def backdate(self, days=2):
        """Fixture boot after the interrupted receipt: move the saved attempt times before the real boot."""
        state = self.state()
        base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        state["started_at"] = (base - datetime.timedelta(minutes=5)).isoformat()
        receipt_path = self.attempt_dir() / "process-result.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["finished_at"] = (base - datetime.timedelta(minutes=1)).isoformat()
        state["finished_at"] = base.isoformat()
        if state.get("model_finished_at"):
            # Quota: the model finished this attempt; timeout: retained prior-attempt fields predate this attempt.
            state["model_finished_at"] = receipt["finished_at"] if state.get("error") == recovery.GENERIC_ERROR \
                else (base - datetime.timedelta(minutes=10)).isoformat()
        implementation._atomic(receipt_path, receipt)
        implementation._atomic(self.handoff_dir / "state.json", state)

    def snapshot_original(self, job_id, attempt=None):
        with self.service.db() as db:
            row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        attempt = Path(attempt or json.loads(row["result"])["attempt_path"])
        files = {path.name: room.sha(path.read_bytes()) for path in sorted(attempt.iterdir()) if path.is_file()}
        pinned = {name: room.sha((self.handoff_dir / name).read_bytes()) for name in json.loads((self.handoff_dir / "handoff.json").read_text())["pinned_files"]}
        return {"row": row, "attempt": files, "pinned": pinned, "initial_candidate": self.state()["initial_candidate"]}

    def audit(self, job_id):
        return self.service.room_implementation_audit(self.room_id, self.handoff_id, job_id)

    def recover(self, job_id, report, request_id="recover-1", **overrides):
        self.assertIn("spec_revision", report.get("identity") or {}, report)
        arguments = {"room_id": self.room_id, "handoff_id": self.handoff_id, "job_id": job_id,
                     "spec_revision": report["identity"]["spec_revision"], "spec_sha256": report["identity"]["spec_sha256"],
                     "candidate_sha256": report["candidate"]["sha256"], "evidence_digest": report["evidence_digest"],
                     "diagnosis": "Timeout/quota interruption diagnosed from saved receipt and state; partial candidate inspected.",
                     "remaining_work": "Finish feature.txt so the gate passes; nothing from the interrupted attempt is verified.",
                     "authorization": self.authorization, "request_id": request_id, **overrides}
        return self.service.call("room_implementation_recover", arguments)

    def assert_original_preserved(self, before, job_id):
        self.assertEqual(self.snapshot_original(job_id), before)
        self.assertEqual(self.service.room_job_status(job_id)["status"], "uncertain")

    def complete_recovery(self, job_id, kind):
        first = self.audit(job_id)
        self.assertFalse(first["eligible"], first)
        self.assertTrue(first["restart_required"])
        self.assertIn("restart_required", first["reasons"])
        self.assertEqual(first["interruption"]["kind"], kind)
        with self.assertRaisesRegex(room.RoomError, "restart_required"):
            self.recover(job_id, first)
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"], [])
        self.backdate()
        before = self.snapshot_original(job_id)
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["interruption"]["kind"], kind)
        self.assertEqual(report["stopped_work"]["label"], "legacy")
        self.assertTrue(report["stopped_work"]["boot_time"])
        self.assertEqual(report["candidate"]["head"], self.handoff["baseline_commit"])
        self.assertEqual(sorted(report["candidate"]["changed_vs_initial"]["added"]), ["feature.txt", "notes.txt"])
        for canary in ("PRIVATE_PROMPT_CANARY", "IMPLEMENTATION PACKET", str(self.handoff_dir), "--session-id", "TimeoutExpired"):
            self.assertNotIn(canary, json.dumps(report))
        prepared = self.recover(job_id, report)
        self.assertEqual(prepared["status"], "prepared")
        self.assertFalse(prepared["duplicate"])
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        self.assert_original_preserved(before, job_id)
        record_dir = self.handoff_dir / "recoveries" / prepared["recovery_id"]
        record = json.loads((record_dir / "record.json").read_text())
        self.assertEqual(record["authorization"], self.authorization)
        self.assertEqual(record["failed_state_snapshot"]["phase"], "blocked")
        self.assertEqual(record["original_job"]["id"], job_id)
        self.assertEqual(record["transcript"]["sha256"], report["transcript"]["sha256"])
        snapshot_bytes = (record_dir / "transcript-snapshot.jsonl").read_bytes()
        self.assertEqual(room.sha(snapshot_bytes), report["transcript"]["sha256"])
        duplicate = self.recover(job_id, report)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["recovery_id"], prepared["recovery_id"])
        with self.assertRaisesRegex(room.RoomError, "different content"):
            self.recover(job_id, report, remaining_work="Changed instructions under the same request id")
        with self.assertRaisesRegex(room.RoomError, "recovery_already_exists"):
            self.recover(job_id, report, request_id="recover-2")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-submit")
        calls = len(self.impl_calls())
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        self.assertNotEqual(successor["id"], job_id)
        terminal = self.service.room_job_status(successor["id"], 40)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(terminal["result"]["phase"], "awaiting_astra_review")
        self.assertEqual(terminal["result"]["attempt_count"], 2)
        self.assertTrue(terminal["result"]["gates_passed"])
        self.assertFalse(terminal["result"]["astra_accepted"])
        self.assertEqual(len(self.impl_calls()), calls + 1)
        launched = self.state()["recovery"]
        self.assertEqual(launched["launch_state"], "launched")
        self.assertEqual(launched["launched_at"], json.loads((self.handoff_dir / "attempts" / "0002" / "process-start.json").read_text())["started_at"])
        self.assertEqual(project_room.recovery_linkage(terminal["result"], "succeeded"), ("consumed", None))
        call = self.impl_calls()[-1]
        self.assertIn("--resume", call["argv"])
        self.assertNotIn("--session-id", call["argv"])
        self.assertEqual(call["session"], self.handoff["implementation_session_id"])
        packet = call["packet"]["recovery"]
        self.assertEqual(packet["recovery_id"], prepared["recovery_id"])
        self.assertEqual(packet["interruption"]["kind"], kind)
        self.assertEqual(packet["recovery_authorization"], self.authorization)
        self.assertEqual(packet["partial_candidate"]["sha256"], report["candidate"]["sha256"])
        self.assertIn("Inspect the existing partial work", packet["instructions"])
        self.assert_original_preserved(before, job_id)
        self.assertEqual((record_dir / "transcript-snapshot.jsonl").read_bytes(), snapshot_bytes)
        live = next((self.base / "claude-storage" / "projects").glob("*/" + self.handoff["implementation_session_id"] + ".jsonl"))
        self.assertGreater(live.stat().st_size, len(snapshot_bytes))
        self.assertTrue(recovery.verify_prefix(live, report["transcript"]["sha256"], report["transcript"]["length"]))
        status = self.service.room_status(self.room_id)
        old = next(job for job in status["jobs"] if job["id"] == job_id)
        self.assertEqual(old["status"], "uncertain")
        self.assertEqual(old["superseded_by"], successor["id"])
        self.assertEqual(status["recoveries"][0]["status"], "consumed")
        self.assertEqual(status["handoffs"][0]["lineage"]["recovery_history"][0]["status"], "launched")
        self.assertEqual([item["outcome"] for item in terminal["result"]["turn_history"]], ["interrupted"])
        self.assertNotIn("DO_NOT_EXPOSE_PRIVATE_THINKING", json.dumps(status))
        kinds = [event["kind"] for event in self.service.room_history(self.room_id)["events"]]
        for kind_name in ("implementation_recovery_prepared", "implementation_recovery_dispatched", "implementation_recovery_consumed"):
            self.assertIn(kind_name, kinds)
        with self.assertRaisesRegex(room.RoomError, "already has a registered successor|is consumed"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=prepared["recovery_id"])
        return prepared, successor

    def test_timeout_interruption_requires_restart_then_continues_in_lineage(self):
        terminal = self.interrupt("interrupt-timeout")
        state = self.state()
        self.assertTrue(state["error"].startswith("TimeoutExpired: Command '"))
        self.assertNotIn("model_return_code", state)
        self.assertNotIn("model_stdout_sha256", state)
        self.assertNotIn("candidate", state)
        self.assertTrue((self.attempt_dir() / "process-result.json").is_file())
        self.assertEqual((self.worktree / "feature.txt").read_text(), "partial\n")
        prepared, successor = self.complete_recovery(terminal["id"], "model_timeout")
        rejected = self.service.room_implementation_review(self.room_id, self.handoff_id, False, "Needs one diagnosed follow-up within the same scope.")
        self.assertEqual(rejected["phase"], "changes_required")
        revised = self.service.room_implementation_revise(self.room_id, self.handoff_id, "Diagnosed follow-up within the same scope.")
        self.assertEqual(revised["phase"], "correction_pending")
        corrected = self.service.room_job_status(self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-correction")["id"], 40)
        self.assertEqual(corrected["status"], "succeeded", corrected)
        self.assertEqual(corrected["result"]["attempt_count"], 3)
        accepted = self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Inspected the finished feature and fresh gate evidence.")
        self.assertEqual(accepted["phase"], "accepted")
        self.assertEqual(self.service.room_job_status(terminal["id"])["status"], "uncertain")
        self.service.room_spec_put(self.room_id, 2, "Revision two remains possible after recovery")
        self.assertEqual(self.service.room_job_status(terminal["id"])["status"], "uncertain")

    def test_quota_interruption_matches_pinned_signature_and_continues(self):
        terminal = self.interrupt("interrupt-quota")
        state = self.state()
        self.assertEqual(state["error"], "ImplementationError: Claude did not return a successful terminal result for the exact implementation session")
        self.assertEqual(state["model_return_code"], 1)
        saved = json.loads((self.attempt_dir() / "stdout.json").read_bytes())
        # Independent literal from the pinned spec; a shared fixture/classifier transcription slip cannot pass this.
        self.assertEqual(saved["result"], SPEC_QUOTA_TEXT)
        self.assertEqual((saved["type"], saved["subtype"], saved["is_error"], saved["terminal_reason"], saved["api_error_status"], saved["stop_reason"]),
                         ("result", "success", True, "api_error", 429, "stop_sequence"))
        self.assertEqual(json.loads((self.attempt_dir() / "process-result.json").read_text())["return_code"], 1)
        self.complete_recovery(terminal["id"], "session_usage_limit")

    def test_interrupted_correction_retaining_prior_fields_is_eligible(self):
        job = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-0")
        first = self.service.room_job_status(job["id"], 40)
        self.assertEqual(first["status"], "succeeded", first)
        self.service.room_implementation_revise(self.room_id, self.handoff_id, "Add notes.txt with the rationale.")
        terminal = self.interrupt("interrupt-timeout", request_id="implement-correction")
        state = self.state()
        self.assertEqual(state["attempt_count"], 2)
        self.assertIn("model_return_code", state)  # retained from attempt 1
        self.assertLess(state["model_finished_at"], state["started_at"])
        self.backdate()
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["interruption"]["kind"], "model_timeout")
        self.assertEqual(report["interruption"]["attempt_count"], 2)
        prepared = self.recover(terminal["id"], report)
        self.mode.write_text("interrupt-timeout")
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=prepared["recovery_id"])
        self.assertEqual(self.service.room_job_status(successor["id"], 40)["status"], "uncertain")
        self.mode.write_text("normal")
        self.assertEqual(self.impl_calls()[-1]["packet"]["recovery"]["pending_correction_request"]["after_attempt"], 1)
        # Second-generation recovery: the correction is still outstanding because no later attempt reported.
        self.backdate()
        second = self.audit(successor["id"])
        self.assertTrue(second["eligible"], second)
        again = self.recover(successor["id"], second, request_id="recover-2")
        third = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-4", recovery_id=again["recovery_id"])
        self.assertEqual(self.service.room_job_status(third["id"], 40)["status"], "succeeded")
        self.assertEqual(self.impl_calls()[-1]["packet"]["recovery"]["pending_correction_request"]["after_attempt"], 1)
        self.assertEqual(self.state()["attempt_count"], 4)

    def test_refusals_leave_state_unchanged(self):
        terminal = self.interrupt("interrupt-timeout")
        job_id = terminal["id"]
        self.backdate()
        before = self.snapshot_original(job_id)
        attempt = self.attempt_dir()
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        cases = []
        def case(name, setup, teardown, expected):
            cases.append((name, setup, teardown, expected))
        lease = (self.service._job_path(job_id) / "worker.lock").open("a")
        import fcntl
        case("held lease", lambda: fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB), lambda: fcntl.flock(lease, fcntl.LOCK_UN), "cooperating_owner_active")
        gate = attempt / "gate-1"
        case("gate directory", lambda: gate.mkdir(), lambda: gate.rmdir(), "gate_directory_present")
        receipt = attempt / "process-result.json"
        original_receipt = receipt.read_bytes()
        case("missing receipt", lambda: receipt.unlink(), lambda: receipt.write_bytes(original_receipt), "receipt_invalid")
        case("future receipt", lambda: receipt.write_text(json.dumps({**json.loads(original_receipt), "finished_at": "2999-01-01T00:00:00+00:00"})),
             lambda: receipt.write_bytes(original_receipt), "receipt_time_order")
        case("symlinked receipt", lambda: (receipt.unlink(), receipt.symlink_to(self.base / "elsewhere")), lambda: (receipt.unlink(), receipt.write_bytes(original_receipt)), "evidence_unsafe")
        def generic_error():
            state = self.state(); state["error"] = "ImplementationError: something else"; implementation._atomic(self.handoff_dir / "state.json", state)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=json_set(result,'$.error',?) WHERE id=?", (state["error"], job_id))
        def restore_state():
            (self.handoff_dir / "state.json").write_bytes(state_bytes)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=? WHERE id=?", (room.canonical(before["row"]["result"] and json.loads(before["row"]["result"])), job_id))
        case("generic error", generic_error, restore_state, "error_not_diagnosable")
        def cancellation_error():
            state = self.state(); state["error"] = "InvocationTerminated: Received signal 15"; implementation._atomic(self.handoff_dir / "state.json", state)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=json_set(result,'$.error',?) WHERE id=?", (state["error"], job_id))
        case("cancellation error", cancellation_error, restore_state, "error_not_diagnosable")
        stdout = attempt / "stdout.json"
        original_stdout = stdout.read_bytes()
        case("contradictory stdout", lambda: stdout.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": self.handoff["implementation_session_id"]})),
             lambda: stdout.write_bytes(original_stdout), "contradictory_stdout")
        session = self.handoff["implementation_session_id"]
        duplicate = self.base / "claude-storage" / "projects" / "another-directory" / (session + ".jsonl")
        case("ambiguous transcript", lambda: (duplicate.parent.mkdir(), duplicate.write_text("{}\n")), lambda: (duplicate.unlink(), duplicate.parent.rmdir()), "transcript_missing_or_ambiguous")
        sleeper_holder = {}
        def start_sleeper():
            sleeper_holder["p"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(self.worktree))
            time.sleep(0.3)
        def stop_sleeper():
            sleeper_holder["p"].kill(); sleeper_holder["p"].wait()
        case("worktree cwd writer", start_sleeper, stop_sleeper, "writer_present")
        case("session argv writer", lambda: (sleeper_holder.__setitem__("p", subprocess.Popen([sys.executable, "-c", "import time,sys; time.sleep(60)", session])), time.sleep(0.3)), stop_sleeper, "writer_present")
        def open_finding():
            with self.service.db() as db:
                db.execute("INSERT INTO issues(id,room_id,job_id,revision,content,severity,disposition) VALUES('open-1',?,?,1,'finding','blocker','open')", (self.room_id, job_id))
        def close_finding():
            with self.service.db() as db:
                db.execute("DELETE FROM issues WHERE id='open-1'")
        case("open finding", open_finding, close_finding, "open_findings")
        extra = self.worktree / "extra.txt"
        for name, setup, teardown, expected in cases:
            with self.subTest(case=name):
                setup()
                try:
                    report = self.audit(job_id)
                    self.assertFalse(report["eligible"], report)
                    self.assertIn(expected, report["reasons"], report["reasons"])
                    filled = {**report, "candidate": report.get("candidate") or {"sha256": "0" * 64}, "evidence_digest": report.get("evidence_digest") or "0" * 64}
                    with self.assertRaisesRegex(room.RoomError, expected):
                        self.recover(job_id, filled, request_id="refused-" + name)
                finally:
                    teardown()
                self.assertEqual(self.state()["phase"], "blocked")
                self.assertEqual(self.snapshot_original(job_id), before)
        lease.close()
        with self.assertRaisesRegex(room.RoomError, "Unknown handoff"):
            self.service.room_implementation_audit(self.room_id, "0" * 64, job_id)
        other = self.service.room_open(str(self.project), "Other feature")
        with self.assertRaisesRegex(room.RoomError, "Unknown handoff"):
            self.service.room_implementation_audit(other["id"], self.handoff_id, job_id)
        self.assertEqual(self.impl_calls()[-1]["packet"].get("recovery"), None)
        self.assertEqual(len(self.impl_calls()), 1)

    def test_synthetic_boot_and_process_fixtures_distinguish_old_boot_reuse_and_current_writers(self):
        terminal = self.interrupt("interrupt-timeout")
        job_id = terminal["id"]
        self.backdate()
        receipt = json.loads((self.attempt_dir() / "process-result.json").read_text())
        finished = int(room.parse_timestamp(receipt["finished_at"]).timestamp())
        expectations = [
            ("boot before receipt", fixed_inspector(boot=finished - 30), ["restart_required"]),
            ("boot same second", fixed_inspector(boot=finished), ["restart_required"]),
            ("boot in the future", fixed_inspector(boot=int(time.time()) + 3600), ["boot_evidence_invalid"]),
            ("boot unreadable", fixed_inspector(boot=0), ["boot_evidence_invalid"]),
            ("inspection unavailable", fixed_inspector(error="inspection_unavailable"), ["inspection_unavailable"]),
            ("ancestor shell in worktree", fixed_inspector(boot=finished + 30, processes=[{"pid": os.getppid(), "ppid": 1, "pgid": os.getppid(), "uid": os.getuid(), "args": "zsh", "cwd": str(self.worktree)}]), ["writer_present"]),
            ("editor holding worktree file", fixed_inspector(boot=finished + 30, processes=[{"pid": 424242, "ppid": 1, "pgid": 424242, "uid": os.getuid(), "args": "vim " + str(self.worktree / "feature.txt"), "cwd": "/"}]), ["writer_present"]),
        ]
        for name, inspector, expected in expectations:
            with self.subTest(case=name):
                self.service.process_inspector = inspector
                report = self.audit(job_id)
                self.assertFalse(report["eligible"])
                self.assertEqual(report["reasons"], expected, report)
        # Boot 30 seconds after the receipt is enough; a reused old PID/group after that boot is informational only,
        # and the audit process itself (matched by the session id in its arguments) is exempt with a recorded role.
        self.service.process_inspector = fixed_inspector(boot=finished + 30, processes=[
            {"pid": receipt["pid"], "ppid": 1, "pgid": receipt["pid"], "uid": os.getuid(), "args": "unrelated-daemon", "cwd": "/"},
            {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpid(), "uid": os.getuid(), "args": "python audit " + self.handoff["implementation_session_id"], "cwd": "/"}])
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["stopped_work"]["pid_reuse_after_boot"], [receipt["pid"]])
        self.assertEqual(report["stopped_work"]["exempt_processes"][0]["role"], "audit_process")
        self.assertEqual(report["stopped_work"]["matched_processes"], [])
        self.assertNotIn("unrelated-daemon", json.dumps(report))

    def test_dispatch_rechecks_invalidate_changed_candidate_and_spawn_failure_permits_fresh_recovery(self):
        terminal = self.interrupt("interrupt-timeout")
        job_id = terminal["id"]
        self.backdate()
        report = self.audit(job_id)
        prepared = self.recover(job_id, report)
        (self.worktree / "feature.txt").write_text("edited after audit\n")
        calls = len(self.impl_calls())
        with self.assertRaisesRegex(room.RoomError, "candidate_changed"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"][0]["status"], "invalidated")
        self.assertTrue((self.handoff_dir / "recoveries" / prepared["recovery_id"] / "record.json").is_file())
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-invalidation")
        with self.assertRaisesRegex(room.RoomError, "invalidated"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2b", recovery_id=prepared["recovery_id"])
        fresh = self.audit(job_id)
        self.assertTrue(fresh["eligible"], fresh)
        self.assertNotEqual(fresh["candidate"]["sha256"], report["candidate"]["sha256"])
        second = self.recover(job_id, fresh, request_id="recover-2")
        self.assertNotEqual(second["recovery_id"], prepared["recovery_id"])
        real_popen = subprocess.Popen
        def worker_launch_fails(argv, *args, **kwargs):
            if "_worker" in argv:
                raise OSError("fixture launch failure")
            return real_popen(argv, *args, **kwargs)
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=worker_launch_fails):
            failed = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=second["recovery_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(self.impl_calls()), calls)
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["recoveries"]}
        self.assertEqual(rows[second["recovery_id"]]["status"], "invalidated")
        self.assertEqual(rows[second["recovery_id"]]["reason"], "worker_spawn_failure")
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual([entry["status"] for entry in self.state()["recovery_history"]], ["prepared", "invalidated", "prepared", "invalidated"])
        third = self.recover(job_id, self.audit(job_id), request_id="recover-3")
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-4", recovery_id=third["recovery_id"])
        self.assertEqual(self.service.room_job_status(successor["id"], 40)["status"], "succeeded")
        self.assertEqual(len(self.impl_calls()), calls + 1)
        self.assertEqual(self.service.room_job_status(job_id)["status"], "uncertain")

    def test_model_spawn_failure_after_dispatch_invalidates_instead_of_consuming(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        state_before = self.state()
        self.impl_fake.chmod(0o600)  # The pinned claude_bin can no longer start; nothing else changed.
        try:
            successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
            result = self.service.room_job_status(successor["id"], 40)
        finally:
            self.impl_fake.chmod(0o700)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["result"]["reason"], "model_spawn_failure")
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["recoveries"]}
        self.assertEqual(rows[prepared["recovery_id"]]["status"], "invalidated")
        after = self.state()
        self.assertEqual(after["phase"], "blocked")
        self.assertEqual((after["attempt_count"], after["attempt_path"]), (state_before["attempt_count"], state_before["attempt_path"]))
        self.assertTrue(any(path.name.startswith("0002-unlaunched-") for path in (self.handoff_dir / "attempts").iterdir()))
        self.assertEqual(len(self.impl_calls()), 1)
        fresh = self.recover(terminal["id"], self.audit(terminal["id"]), request_id="recover-2")
        again = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=fresh["recovery_id"])
        self.assertEqual(self.service.room_job_status(again["id"], 40)["status"], "succeeded")

    def test_stray_attempt_directory_and_worker_recheck_refusal_do_not_wedge_the_lane(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        stray = self.handoff_dir / "attempts" / "0002"
        stray.mkdir()
        (stray / "prompt.txt").write_text("left behind by an earlier pre-launch failure")
        calls = len(self.impl_calls())
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        result = self.service.room_job_status(successor["id"], 40)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["result"]["attempt_count"], 2)
        self.assertTrue(any(path.name.startswith("0002-unlaunched-") for path in (self.handoff_dir / "attempts").iterdir()))
        self.assertEqual(len(self.impl_calls()), calls + 1)
        # Worker-level recheck refusal: dispatch passes, then the record is tampered before the worker starts.
        self.service.room_implementation_review(self.room_id, self.handoff_id, False, "Needs a follow-up.")
        self.service.room_implementation_revise(self.room_id, self.handoff_id, "Add the missing note.")
        third = self.interrupt("interrupt-timeout", request_id="implement-3")
        self.backdate()
        again = self.recover(third["id"], self.audit(third["id"]), request_id="recover-2")
        class Held:
            pid = 0
            def wait(self):
                return 0
        real_popen = subprocess.Popen
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=lambda argv, *a, **k: Held() if "_worker" in argv else real_popen(argv, *a, **k)):
            queued = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-4", recovery_id=again["recovery_id"])
        record = self.handoff_dir / "recoveries" / again["recovery_id"] / "record.json"
        record.write_text(record.read_text() + "\n")
        self.service.worker(queued["id"])
        outcome = self.service.room_job_status(queued["id"])
        self.assertEqual(outcome["status"], "failed", outcome)
        self.assertIn("evidence_changed", outcome["error"])
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["recoveries"]}
        self.assertEqual(rows[again["recovery_id"]]["status"], "invalidated")
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual(len(self.impl_calls()), calls + 2)
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-worker-refusal")

    def test_direct_run_and_arbitrary_recovery_identity_cannot_launch(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        calls = len(self.impl_calls())
        with self.assertRaisesRegex(implementation.ImplementationError, "registered successor"):
            implementation.run_implementation(self.handoff["handoff_path"])
        refused = implementation.run_implementation(self.handoff["handoff_path"], successor={"recovery_id": prepared["recovery_id"], "successor_job_id": "0" * 32, "recheck": lambda: {"eligible": True}})
        self.assertEqual(refused["phase"], "refused_before_launch")
        self.assertEqual(refused["reason"], "recovery_binding_mismatch")
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        self.assertEqual(len(self.impl_calls()), calls)
        process = subprocess.run([sys.executable, str(Path(implementation.__file__)), "run", "--handoff", self.handoff["handoff_path"]], capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(len(self.impl_calls()), calls)
        # An orphan dispatch file (crash before registration) plus a caller-supplied eligible callback never reaches launch:
        # the engine binds to the registry rows named by the durable record and to the registered worker's held lease.
        orphan = "a" * 32
        recovery.write_dispatch(self.handoff_dir, prepared["recovery_id"], orphan)
        with self.service.db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM jobs WHERE id=?", (orphan,)).fetchone())
        with mock.patch.object(implementation, "_run_child", side_effect=RuntimeError("model launch reached")) as launch:
            refused = implementation.run_implementation(self.handoff["handoff_path"], successor={"recovery_id": prepared["recovery_id"], "successor_job_id": orphan, "recheck": lambda: {"eligible": True, "reasons": []}})
        self.assertFalse(launch.called)
        self.assertEqual((refused["phase"], refused["reason"]), ("refused_before_launch", "recovery_binding_mismatch"))
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        # A registered, dispatched successor whose worker lease nobody holds is not the owning invocation either.
        class Held:
            pid = 0
            def wait(self):
                return 0
        real_popen = subprocess.Popen
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=lambda argv, *a, **k: Held() if "_worker" in argv else real_popen(argv, *a, **k)):
            queued = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        with self.service.db() as db:
            db.execute("UPDATE jobs SET status='running',started_at=?,pid=? WHERE id=?", (room.now(), os.getppid(), queued["id"]))
        with mock.patch.object(implementation, "_run_child", side_effect=RuntimeError("model launch reached")) as launch:
            refused = self.service.execute_job(queued["id"])
        self.assertFalse(launch.called)
        self.assertEqual((refused["phase"], refused["reason"]), ("refused_before_launch", "recovery_binding_mismatch"))
        # Under the registered worker's lease (and as that worker's child), a failure after the rechecks but before any
        # state write or spawn is a proven pre-launch refusal.
        with self.held_lease(queued["id"]):
            with mock.patch.object(room, "validate_subscription_environment", side_effect=room.RoomError("fixture environment conflict")):
                refused = self.service.execute_job(queued["id"])
        self.assertEqual((refused["phase"], refused["reason"]), ("refused_before_launch", "prelaunch_error"))
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        self.assertEqual(len(self.impl_calls()), calls)

    def live_transcript(self):
        return next((self.base / "claude-storage" / "projects").glob("*/" + self.handoff["implementation_session_id"] + ".jsonl"))

    def register_successor(self, prepared, request_id, worker_pid=None):
        """Dispatch a successor as the Service would, without a live worker process: the registry records the worker
        pid (this test's parent by default, so an in-process execute_job counts as that worker's child) and the job runs."""
        class Held:
            pid = os.getppid() if worker_pid is None else worker_pid

            def wait(self):
                return 0
        real_popen = subprocess.Popen
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=lambda argv, *a, **k: Held() if "_worker" in argv else real_popen(argv, *a, **k)):
            queued = self.service.room_implementation_submit(self.room_id, self.handoff_id, request_id, recovery_id=prepared["recovery_id"])
        with self.service.db() as db:
            db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (room.now(), queued["id"]))
        return queued

    def held_lease(self, job_id):
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def hold():
            with (self.service._job_path(job_id) / "worker.lock").open("a") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
        return hold()

    def guarded_popen(self, on_model):
        """Popen replacement that intercepts only the pinned fake claude binary and passes everything else through."""
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if argv and argv[0] == str(self.impl_fake):
                return on_model(argv, *args, **kwargs)
            return real_popen(argv, *args, **kwargs)
        return mock.patch.object(implementation.subprocess, "Popen", side_effect=popen)

    def test_cancellation_before_process_creation_is_a_proven_refusal(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        queued = self.register_successor(prepared, "implement-2")
        original_open = Path.open

        def terminate_while_opening_output(path, *args, **kwargs):
            if path.name == "stdout.json" and path.parent.name == "0002":
                raise room.InvocationTerminated("Received signal 15")
            return original_open(path, *args, **kwargs)

        def must_not_spawn(argv, *args, **kwargs):
            raise AssertionError("model process must not be created")
        with self.held_lease(queued["id"]), mock.patch.object(Path, "open", new=terminate_while_opening_output), self.guarded_popen(must_not_spawn):
            refused = self.service.execute_job(queued["id"])
        self.assertEqual((refused["phase"], refused["reason"]), ("refused_before_launch", "cancelled_before_launch"))
        self.assertFalse(refused["model_launched"])
        state = self.state()
        self.assertEqual(state["phase"], "recovery_prepared")
        self.assertNotIn("launched_at", state["recovery"])
        self.assertEqual([entry["status"] for entry in state["recovery_history"]], ["prepared"])
        self.assertTrue(any(path.name.startswith("0002-unlaunched-") for path in (self.handoff_dir / "attempts").iterdir()))
        self.assertFalse((self.handoff_dir / "attempts" / "0002").exists())
        self.assertEqual(project_room.recovery_linkage(refused, "failed"), ("invalidated", "cancelled_before_launch"))
        self.assertEqual(len(self.impl_calls()), 1)

    def test_interruption_inside_process_creation_stays_unknown_and_blocked(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        queued = self.register_successor(prepared, "implement-2")

        def interrupted_spawn(argv, *args, **kwargs):
            raise room.InvocationTerminated("Received signal 15")
        with self.held_lease(queued["id"]), self.guarded_popen(interrupted_spawn):
            result = self.service.execute_job(queued["id"])
        self.assertEqual(result["phase"], "blocked")
        self.assertTrue(result["error"].startswith("LaunchUnknown:"))
        self.assertEqual(result["recovery"]["launch_state"], "unknown")
        self.assertNotIn("launched_at", result["recovery"])
        self.assertFalse((self.handoff_dir / "attempts" / "0002" / "process-start.json").exists())
        self.assertEqual(project_room.recovery_linkage(result, "uncertain"), ("launch_unknown", "unknown"))
        self.assertEqual(project_room.recovery_linkage({"status": "uncertain", "error": "no result"}, "uncertain"), ("launch_unknown", "no_classifiable_result"))
        self.assertEqual(project_room.recovery_linkage(None, "cancelled"), ("invalidated", "cancelled_before_launch"))
        with self.service.db() as db:  # the worker's registry update for this outcome
            db.execute("UPDATE jobs SET status='uncertain',finished_at=?,result=? WHERE id=?", (room.now(), room.canonical(result), queued["id"]))
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"][0]["status"], "dispatched")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-unknown")
        with self.assertRaisesRegex(room.RoomError, "dispatched"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=prepared["recovery_id"])
        second = self.service.room_implementation_audit(self.room_id, self.handoff_id, queued["id"])
        self.assertFalse(second["eligible"])
        self.assertIn("receipt_invalid", second["reasons"])
        self.assertEqual(len(self.impl_calls()), 1)

    def test_lost_start_receipt_after_process_creation_is_still_a_launch(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        queued = self.register_successor(prepared, "implement-2")
        original_atomic = implementation._atomic

        def lose_start_receipt(path, value):
            if Path(path).name == "process-start.json":
                raise OSError("synthetic write failure after the process was created")
            return original_atomic(path, value)
        with self.held_lease(queued["id"]), mock.patch.object(implementation, "_atomic", side_effect=lose_start_receipt):
            result = self.service.execute_job(queued["id"])
        attempt = self.handoff_dir / "attempts" / "0002"
        self.assertFalse((attempt / "process-start.json").exists())
        self.assertTrue((attempt / "process-result.json").is_file())  # written only because a process existed
        self.assertEqual(result["phase"], "blocked")
        self.assertEqual(result["recovery"]["launch_state"], "launched")
        self.assertEqual(result["recovery"]["launch_evidence"], "process-result.json")
        self.assertEqual(result["recovery"]["launched_at"], result["started_at"])
        self.assertEqual(project_room.recovery_linkage(result, "uncertain"), ("consumed", None))
        receipt = json.loads((attempt / "process-result.json").read_text())
        self.assertIsInstance(receipt["pid"], int)  # the created process was stopped by the engine; its exit receipt is the proof

    def test_fabricated_callback_cannot_beat_the_registered_worker(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        queued = self.register_successor(prepared, "implement-2", worker_pid=1)  # a real registered worker that is not our parent
        claimed = {"recovery_id": prepared["recovery_id"], "successor_job_id": queued["id"], "registry": str(self.service.home),
                   "recheck": lambda: {"eligible": True, "reasons": []}}
        with self.held_lease(queued["id"]), mock.patch.object(implementation, "_run_child", side_effect=RuntimeError("launch reached")) as launch:
            refused = implementation.run_implementation(self.handoff["handoff_path"], successor=claimed)
        self.assertFalse(launch.called)
        self.assertEqual((refused["phase"], refused["reason"]), ("refused_before_launch", "recovery_binding_mismatch"))
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        self.assertEqual(len(self.impl_calls()), 1)

    def concurrent_preparations(self, different):
        import concurrent.futures
        import threading
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        before = self.snapshot_original(terminal["id"])
        barrier = threading.Barrier(2)

        def prepare(index):
            barrier.wait(timeout=5)
            try:
                return "result", self.recover(terminal["id"], report, request_id="transition-%d" % index if different else "transition-1")
            except room.RoomError as exc:
                return "conflict", str(exc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            outputs = [future.result(timeout=30) for future in [workers.submit(prepare, index) for index in range(2)]]
        successes = [value for kind, value in outputs if kind == "result"]
        self.assertTrue(successes, outputs)
        self.assertEqual(len({value["recovery_id"] for value in successes}), 1)
        rows = self.service.room_status(self.room_id)["recoveries"]
        self.assertEqual([row["status"] for row in rows], ["prepared"])
        self.assert_original_preserved(before, terminal["id"])
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-must-not-start")
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        with self.service.db() as db:
            winning_key = db.execute("SELECT request_key FROM implementation_recoveries WHERE id=?", (rows[0]["id"],)).fetchone()[0]
        duplicate = self.recover(terminal["id"], report, request_id=winning_key)
        self.assertEqual(duplicate["recovery_id"], rows[0]["id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual((self.handoff_dir / "state.json").read_bytes(), state_bytes)

    def test_concurrent_identical_preparations_have_one_authorization(self):
        self.concurrent_preparations(False)

    def test_concurrent_different_keys_cannot_authorize_two_successors(self):
        self.concurrent_preparations(True)

    def test_crash_before_projection_leaves_only_an_unusable_orphan_record(self):
        class SimulatedCrash(BaseException):
            pass
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        original_write, hit = recovery.write_json_below, []

        def interrupt(directory_fd, name, value, kind="evidence"):
            if name == "state.json" and isinstance(value, dict) and value.get("phase") == "recovery_prepared":
                hit.append(True)
                raise SimulatedCrash()
            return original_write(directory_fd, name, value, kind)
        with mock.patch.object(recovery, "write_json_below", side_effect=interrupt), self.assertRaises(SimulatedCrash):
            self.recover(terminal["id"], report)
        self.assertTrue(hit)
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"], [])
        self.assertEqual((self.handoff_dir / "state.json").read_bytes(), state_bytes)
        orphan = list((self.handoff_dir / "recoveries").glob("*/record.json"))
        self.assertEqual(len(orphan), 1)
        orphan_bytes = orphan[0].read_bytes()
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-must-not-start")
        fresh = self.audit(terminal["id"])
        self.assertTrue(fresh["eligible"], fresh)
        prepared = self.recover(terminal["id"], fresh, request_id="after-crash")
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(len(self.service.room_status(self.room_id)["recoveries"]), 1)
        self.assertEqual(orphan[0].read_bytes(), orphan_bytes)

    def test_crash_after_projection_is_read_only_until_a_supported_mutation(self):
        class SimulatedCrash(BaseException):
            pass
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        original_prepare, hit = recovery.prepare, []

        def interrupt(*args, **kwargs):
            original_prepare(*args, **kwargs)
            hit.append(True)
            raise SimulatedCrash()
        with mock.patch.object(recovery, "prepare", side_effect=interrupt), self.assertRaises(SimulatedCrash):
            self.recover(terminal["id"], report)
        self.assertTrue(hit)
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"], [])
        self.assertEqual(self.state()["phase"], "recovery_prepared")
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        stale = self.audit(terminal["id"])
        self.assertFalse(stale["eligible"])
        self.assertIn("projection_out_of_sync", stale["reasons"])
        self.assertEqual((self.handoff_dir / "state.json").read_bytes(), state_bytes)
        orphan = list((self.handoff_dir / "recoveries").glob("*/record.json"))
        self.assertEqual(len(orphan), 1)
        orphan_bytes = orphan[0].read_bytes()
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-must-not-start")
        prepared = self.recover(terminal["id"], report, request_id="after-projection-crash")
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(len(self.service.room_status(self.room_id)["recoveries"]), 1)
        self.assertEqual(orphan[0].read_bytes(), orphan_bytes)
        self.assertTrue((orphan[0].parent / "invalidation.json").is_file())

    def test_crash_after_registry_commit_before_event_returns_the_existing_preparation(self):
        class SimulatedCrash(BaseException):
            pass
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        original_event, hit = self.service._event, []

        def interrupt(room_id, kind, value):
            if kind == "implementation_recovery_prepared":
                hit.append(True)
                raise SimulatedCrash()
            return original_event(room_id, kind, value)
        with mock.patch.object(self.service, "_event", side_effect=interrupt), self.assertRaises(SimulatedCrash):
            self.recover(terminal["id"], report)
        self.assertTrue(hit)
        rows = self.service.room_status(self.room_id)["recoveries"]
        self.assertEqual([row["status"] for row in rows], ["prepared"])
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        duplicate = self.recover(terminal["id"], report)
        self.assertEqual(duplicate["recovery_id"], rows[0]["id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual((self.handoff_dir / "state.json").read_bytes(), state_bytes)
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-must-not-start")
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=rows[0]["id"])
        self.assertEqual(self.service.room_job_status(successor["id"], 40)["status"], "succeeded")

    def test_transcript_worktree_binding_missing_and_contradictory_are_distinct(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        live = self.live_transcript()
        original = live.read_bytes()
        records = [json.loads(line) for line in original.splitlines()]
        unbound = [{key: value for key, value in record.items() if key != "cwd"} for record in records]
        live.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in unbound))
        report = self.audit(terminal["id"])
        self.assertFalse(report["eligible"])
        self.assertEqual(report["reasons"], ["transcript_worktree_unbound"])
        contradictory = [{**record, "cwd": str(self.base / "elsewhere")} for record in records]
        live.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in contradictory))
        report = self.audit(terminal["id"])
        self.assertEqual(report["reasons"], ["transcript_worktree_mismatch"])
        live.write_bytes(original)
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        self.assertEqual(self.state()["phase"], "blocked")

    def test_same_worktree_subdirectory_records_keep_recovery_eligible(self):
        terminal = self.interrupt("interrupt-quota")
        self.backdate()
        live = self.live_transcript()
        original = live.read_bytes()

        def append(cwd):
            with live.open("ab") as handle:
                handle.write(json.dumps({"type": "assistant", "sessionId": self.handoff["implementation_session_id"], "cwd": str(cwd),
                                         "isSidechain": False}).encode() + b"\n")
        append(self.worktree / "apps" / "backend" / "src")
        append(self.worktree / "apps" / "backend")
        append(self.worktree)
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        (self.base / "elsewhere").mkdir(exist_ok=True)
        for bad in (self.base / "elsewhere", Path(str(self.worktree) + "-other") / "apps"):
            live.write_bytes(original)
            append(bad)
            self.assertIn("transcript_worktree_mismatch", self.audit(terminal["id"])["reasons"], bad)
        escape = self.worktree / "linked-outside"
        escape.symlink_to(self.base / "elsewhere", target_is_directory=True)
        try:
            live.write_bytes(original)
            append(escape / "deeper")
            self.assertIn("transcript_worktree_mismatch", self.audit(terminal["id"])["reasons"])
        finally:
            escape.unlink()
        # descendant-only records: the worktree is not contradicted, but the required root anchor is missing
        records = [json.loads(line) for line in original.splitlines()]
        live.write_bytes(b"".join(json.dumps({**record, "cwd": str(self.worktree / "apps")} if "cwd" in record else record).encode() + b"\n" for record in records))
        self.assertIn("transcript_worktree_unbound", self.audit(terminal["id"])["reasons"])
        live.write_bytes(original)
        self.assertTrue(self.audit(terminal["id"])["eligible"])

    def test_nested_records_in_the_timeout_lane_complete_and_recover(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        live = self.live_transcript()
        with live.open("ab") as handle:
            handle.write(json.dumps({"type": "assistant", "sessionId": self.handoff["implementation_session_id"],
                                     "cwd": str(self.worktree / "apps" / "backend" / "src"), "isSidechain": False}).encode() + b"\n")
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        prepared = self.recover(terminal["id"], report)
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        outcome = self.service.room_job_status(successor["id"], 40)
        self.assertEqual(outcome["status"], "succeeded", outcome)  # the completion lane accepted the nested records the fake emitted
        self.assertEqual(outcome["result"]["primary_model"], "claude-fable-5-1")

    def test_quota_model_finish_outside_receipt_window_is_refused(self):
        terminal = self.interrupt("interrupt-quota")
        self.backdate()
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        state_path = self.handoff_dir / "state.json"
        original = state_path.read_bytes()
        receipt = json.loads((self.attempt_dir() / "process-result.json").read_text())
        for label, value in (("future", (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).isoformat()),
                             ("before receipt", (room.parse_timestamp(receipt["finished_at"]) - datetime.timedelta(seconds=1)).isoformat()),
                             ("after state finish", (room.parse_timestamp(self.state()["finished_at"]) + datetime.timedelta(seconds=1)).isoformat())):
            with self.subTest(case=label):
                state = self.state()
                state["model_finished_at"] = value
                implementation._atomic(state_path, state)
                report = self.audit(terminal["id"])
                self.assertFalse(report["eligible"])
                self.assertIn("model_finish_time_order", report["reasons"])
                state_path.write_bytes(original)
        self.assertTrue(self.audit(terminal["id"])["eligible"])

    def test_evidence_changed_during_observation_refuses_preparation(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        before = self.snapshot_original(terminal["id"])
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        prompt = self.attempt_dir() / "prompt.txt"
        inspector = fixed_inspector(boot=int(time.time()) - 5)
        changed = []
        def changing_inspector():
            prompt.write_bytes(prompt.read_bytes() + b"changed while the audit observed processes\n")
            changed.append(True)
            return inspector()
        self.service.process_inspector = changing_inspector
        with self.assertRaisesRegex(room.RoomError, "evidence_changed"):
            self.recover(terminal["id"], report)
        self.assertTrue(changed)
        self.assertEqual((self.handoff_dir / "state.json").read_bytes(), state_bytes)
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"], [])
        self.assertFalse((self.handoff_dir / "recoveries").exists())
        self.assertNotEqual(self.snapshot_original(terminal["id"])["attempt"], before["attempt"])  # the injected change itself

    def evidence_race(self, job_id, component, action):
        """Inject `action` at the descriptor seam right before `component` is opened during the audit; return
        (report, fired, outside_inodes_opened) where the last records every opened descriptor's inode."""
        original = recovery._open_component
        fired, opened = [], []

        def seam(name, flags, dir_fd):
            if name == component and not fired:
                fired.append(True)
                action()
            fd = original(name, flags, dir_fd)
            opened.append(os.fstat(fd).st_ino)
            return fd
        with mock.patch.object(recovery, "_open_component", new=seam):
            report = self.audit(job_id)
        return report, fired, opened

    def test_evidence_swapped_to_symlink_at_open_is_refused_without_reading_outside(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        target = self.attempt_dir() / "stderr.txt"
        outside = self.base / "outside-canary.txt"
        outside.write_bytes(b"OUTSIDE_CANARY_MUST_NOT_BE_READ")

        def swap():
            target.unlink()
            target.symlink_to(outside)
        report, fired, opened = self.evidence_race(terminal["id"], "stderr.txt", swap)
        self.assertTrue(fired)
        self.assertNotIn(outside.stat().st_ino, opened)
        self.assertFalse(report["eligible"])
        self.assertIn("evidence_unsafe", report["reasons"])
        self.assertNotIn("OUTSIDE_CANARY", json.dumps(report))

    def test_attempt_directory_swapped_to_symlink_is_refused_without_reading_outside(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        attempt = self.attempt_dir()
        outside = self.base / "outside-owned-attempt"
        outside.mkdir()
        for source in attempt.iterdir():
            if source.is_file():
                (outside / source.name).write_bytes(source.read_bytes())
        (outside / "stderr.txt").write_bytes(b"OUTSIDE_PARENT_CANARY")
        canary = (outside / "stderr.txt").stat().st_ino

        def swap():
            attempt.rename(attempt.with_name("preserved-original-attempt"))
            attempt.symlink_to(outside, target_is_directory=True)
        for component in ("stderr.txt", attempt.name):
            with self.subTest(component=component):
                report, fired, opened = self.evidence_race(terminal["id"], component, swap)
                self.assertTrue(fired)
                self.assertNotIn(canary, opened)
                self.assertFalse(report["eligible"])
                self.assertTrue({"evidence_unsafe", "evidence_changed"} & set(report["reasons"]), report["reasons"])
                attempt.unlink()
                attempt.with_name("preserved-original-attempt").rename(attempt)
        self.assertTrue(self.audit(terminal["id"])["eligible"])

    def test_handoff_root_swapped_before_its_open_is_refused_without_reading_outside(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        self.assertTrue(self.audit(terminal["id"])["eligible"])
        import shutil
        outside = self.base / "outside-handoff-copy"
        shutil.copytree(self.handoff_dir, outside, symlinks=True)
        outside_inodes = {path.stat().st_ino for path in outside.rglob("*") if path.is_file() and not path.is_symlink()}
        original_root, original_component, fired, opened = recovery._open_root, recovery._open_component, [], []

        def swap_root(path, flags):
            if path == str(self.handoff_dir) and not fired:
                fired.append(True)
                self.handoff_dir.rename(self.handoff_dir.with_name(self.handoff_dir.name + "-preserved"))
                self.handoff_dir.symlink_to(outside, target_is_directory=True)
            return original_root(path, flags)

        def track(name, flags, dir_fd):
            fd = original_component(name, flags, dir_fd)
            opened.append(os.fstat(fd).st_ino)
            return fd
        try:
            with mock.patch.object(recovery, "_open_root", new=swap_root), mock.patch.object(recovery, "_open_component", new=track):
                report = self.audit(terminal["id"])
        finally:
            if self.handoff_dir.is_symlink():
                self.handoff_dir.unlink()
                self.handoff_dir.with_name(self.handoff_dir.name + "-preserved").rename(self.handoff_dir)
        self.assertTrue(fired)
        self.assertFalse(outside_inodes & set(opened), "a file of the outside copy was opened through the swapped root")
        self.assertFalse(report["eligible"])
        self.assertIn("handoff_integrity", report["reasons"])
        restored = self.audit(terminal["id"])
        self.assertTrue(restored["eligible"], restored)  # positive control once the real directory is back

    def test_handoff_root_redirected_during_observation_cannot_prepare_outside_owned_storage(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        import shutil
        outside = self.base / "outside-preparation-copy"
        shutil.copytree(self.handoff_dir, outside, symlinks=True)
        preserved = self.handoff_dir.with_name(self.handoff_dir.name + "-preserved")
        original_state = (self.handoff_dir / "state.json").read_bytes()
        inspector, fired = fixed_inspector(boot=int(time.time()) - 5), []

        def redirect_during_observation():
            if not fired:
                fired.append(True)
                self.handoff_dir.rename(preserved)
                self.handoff_dir.symlink_to(outside, target_is_directory=True)
            return inspector()
        self.service.process_inspector = redirect_during_observation
        try:
            with self.assertRaisesRegex(room.RoomError, "evidence_unsafe"):
                self.recover(terminal["id"], report)
            self.assertTrue(fired)
            self.assertEqual((preserved / "state.json").read_bytes(), original_state)
            self.assertFalse((outside / "recoveries").exists(), "preparation wrote outside the bound handoff directory")
            self.assertFalse((preserved / "recoveries").exists())
        finally:
            self.handoff_dir.unlink()
            preserved.rename(self.handoff_dir)
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"], [])
        self.service.process_inspector = inspector
        self.assertTrue(self.audit(terminal["id"])["eligible"])  # positive control with the real directory back

    def test_handoff_root_swapped_at_recovery_write_open_never_changes_outside_copy(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        self.assertTrue(report["eligible"], report)
        import hashlib
        import shutil
        outside = self.base / "outside-write-copy"
        preserved = self.handoff_dir.with_name(self.handoff_dir.name + "-preserved")
        original_create, fired, baseline = recovery._create_component, [], {}

        def contents(folder):
            return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in folder.rglob("*") if path.is_file() and not path.is_symlink()}

        def swap_at_write(name, flags, mode, dir_fd):
            if not fired:  # the first mutating open of the preparation: after checks and directory creation
                fired.append(True)
                shutil.copytree(self.handoff_dir, outside, symlinks=True)
                baseline.update(contents(outside))
                self.handoff_dir.rename(preserved)
                self.handoff_dir.symlink_to(outside, target_is_directory=True)
            return original_create(name, flags, mode, dir_fd)
        try:
            with mock.patch.object(recovery, "_create_component", new=swap_at_write):
                prepared = self.recover(terminal["id"], report)
            self.assertTrue(fired)
            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(contents(outside), baseline, "the substituted outside copy changed")
            home = preserved / "recoveries" / prepared["recovery_id"]
            self.assertTrue((home / "record.json").is_file() and (home / "transcript-snapshot.jsonl").is_file())
            self.assertEqual(json.loads((preserved / "state.json").read_text())["phase"], "recovery_prepared")
            self.assertEqual(oct((home / "record.json").stat().st_mode & 0o777), oct(0o600))
            self.assertEqual(oct(home.stat().st_mode & 0o777), oct(0o700))
            self.assertEqual([entry.name for entry in home.iterdir() if entry.name.endswith(".tmp")], [])
        finally:
            if self.handoff_dir.is_symlink():
                self.handoff_dir.unlink()
                preserved.rename(self.handoff_dir)
        # positive control: with the real directory back under its name, the prepared recovery dispatches normally
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        self.assertEqual(self.service.room_job_status(successor["id"], 40)["status"], "succeeded")

    def test_evidence_swapped_to_fifo_at_open_is_refused_without_blocking(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        target = self.attempt_dir() / "stderr.txt"

        def swap():
            target.unlink()
            os.mkfifo(target, 0o600)
        report, fired, _ = self.evidence_race(terminal["id"], "stderr.txt", swap)
        self.assertTrue(fired)
        self.assertFalse(report["eligible"])
        self.assertIn("evidence_unsafe", report["reasons"])

    def test_tampered_transcript_prefix_blocks_successor_before_acceptance(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        report = self.audit(terminal["id"])
        prepared = self.recover(terminal["id"], report)
        live = next((self.base / "claude-storage" / "projects").glob("*/" + self.handoff["implementation_session_id"] + ".jsonl"))
        original = live.read_bytes()
        live.write_bytes(original + b'{"sessionId":"' + self.handoff["implementation_session_id"].encode() + b'","cwd":"' + str(self.worktree).encode() + b'"}\n')
        with self.assertRaisesRegex(room.RoomError, "transcript_changed"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        live.write_bytes(original)
        again = self.recover(terminal["id"], self.audit(terminal["id"]), request_id="recover-2")
        # Rewrite one byte inside the captured prefix while the successor runs: completion must block before identity evidence.
        self.impl_fake.write_text(self.impl_fake.read_text().replace('pathlib.Path("feature.txt").write_text("implemented\\n")',
            'data = bytearray(transcript.read_bytes()); data[5] ^= 1; transcript.write_bytes(bytes(data))\npathlib.Path("feature.txt").write_text("implemented\\n")'))
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3", recovery_id=again["recovery_id"])
        terminal2 = self.service.room_job_status(successor["id"], 40)
        self.assertEqual(terminal2["status"], "uncertain", terminal2)
        self.assertIn("transcript_prefix_changed", terminal2["result"]["error"])
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-4")

    def test_interrupted_successor_needs_another_restart_and_recovery(self):
        terminal = self.interrupt("interrupt-timeout")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        self.mode.write_text("interrupt-timeout")
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        terminal2 = self.service.room_job_status(successor["id"], 40)
        self.mode.write_text("normal")
        self.assertEqual(terminal2["status"], "uncertain")
        self.assertEqual(self.state()["attempt_count"], 2)
        self.assertEqual(self.state()["recovery"]["launch_state"], "launched")  # the process existed; its interruption is post-launch
        self.assertEqual(self.service.room_status(self.room_id)["recoveries"][0]["status"], "consumed")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain job " + successor["id"]):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-3")
        second = self.service.room_implementation_audit(self.room_id, self.handoff_id, successor["id"])
        self.assertFalse(second["eligible"])
        self.assertTrue(second["restart_required"])
        self.assertEqual(second["interruption"]["kind"], "model_timeout")
        self.assertEqual(second["interruption"]["attempt_count"], 2)
        stale = self.service.room_implementation_audit(self.room_id, self.handoff_id, terminal["id"])
        self.assertFalse(stale["eligible"], "a superseded predecessor must not be re-recoverable")
        self.assertTrue(stale["reasons"])

    def test_wrong_producer_and_failed_gates_block_successor_acceptance(self):
        terminal = self.interrupt("interrupt-quota")
        self.backdate()
        prepared = self.recover(terminal["id"], self.audit(terminal["id"]))
        self.mode.write_text("wrong-producer")
        successor = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2", recovery_id=prepared["recovery_id"])
        result = self.service.room_job_status(successor["id"], 40)
        self.mode.write_text("normal")
        self.assertEqual(result["status"], "uncertain")
        self.assertIn("primary model", result["result"]["error"])
        with self.assertRaises(room.RoomError):
            self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Would accept without evidence.")

    def test_other_rooms_continue_while_this_room_is_blocked_or_recovering(self):
        terminal = self.interrupt("interrupt-timeout")
        other = self.service.room_open(str(self.project), "Export filters")
        self.service.room_spec_put(other["id"], 1, "Export saved filters")
        review = self.service.room_review_submit(other["id"], 1, "Review export", "review-1")
        self.assertEqual(self.service.room_job_status(review["id"], 15)["status"], "succeeded")
        self.backdate()
        self.recover(terminal["id"], self.audit(terminal["id"]))
        self.assertEqual(self.service.room_job_status(self.service.room_review_submit(other["id"], 1, "Review export again", "review-2")["id"], 15)["status"], "succeeded")


class ProcessInspectionTests(unittest.TestCase):
    def observe(self, table):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            receipt = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=100)).isoformat()
            facts = {"boot_time": int(time.time()) - 10, "boot_source": "fixture", **table}
            return recovery.observe(receipt, 424242, "00000000-0000-4000-8000-000000000000", temp, inspector=lambda: facts)

    def test_darwin_same_uid_process_without_cwd_evidence_is_incomplete_unless_gone_or_zombie(self):
        uid = os.getuid()
        listing = f"424242 1 424242 {uid} python synthetic-task.py\n424243 1 424243 {uid} exited-meanwhile\n424244 1 424244 {uid} zombie\n424245 1 424245 {uid} readable\n"
        statuses = {"424242": (0, "S+\n"), "424243": (1, ""), "424244": (0, "Z\n")}
        calls = []
        def bounded(argv, timeout):
            calls.append(argv)
            self.assertLessEqual(timeout, 20)
            if argv[:2] == ["/bin/ps", "-Aww"]:
                return 0, listing
            if argv[0] == "/usr/sbin/lsof":
                return 1, "p424242\np424243\np424244\np424245\nn/somewhere\n"
            if argv[:3] == ["/bin/ps", "-o", "stat="]:
                return statuses[argv[4]]
            raise AssertionError(argv)
        with mock.patch.object(recovery.platform_module, "system", return_value="Darwin"), mock.patch.object(recovery, "_bounded", side_effect=bounded):
            table = recovery.process_table()
        self.assertEqual(table["incomplete"], [424242])
        self.assertEqual(table["disappeared"], 2)
        self.assertEqual([process["pid"] for process in table["processes"]], [424245])
        self.assertEqual(sum(1 for argv in calls if argv[:3] == ["/bin/ps", "-o", "stat="]), 3)
        observation = self.observe(table)
        self.assertEqual(observation["reasons"], ["inspection_incomplete"])
        self.assertEqual((observation["incomplete_count"], observation["disappeared_count"]), (1, 2))
        self.assertNotIn("synthetic-task", json.dumps(observation))
        complete = self.observe({**table, "incomplete": []})
        self.assertEqual(complete["reasons"], [])

    def test_linux_same_uid_proc_read_errors_are_incomplete_and_disappearance_is_verified(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            def make(pid, state="S", cwd=None):
                directory = proc / str(pid)
                directory.mkdir()
                (directory / "stat").write_text(f"{pid} (python task) {state} 1 {pid} {pid} 0 -1 4194560 0 0\n")
                (directory / "cmdline").write_bytes(b"python\0task.py\0")
                if cwd is not None:
                    os.symlink(cwd, directory / "cwd")
            make(101, cwd=temp)
            make(102)
            (proc / "102" / "cwd").write_text("")  # readlink fails with EINVAL while the process still exists
            make(103, state="Z")
            (proc / "104").mkdir()  # no stat: the process exited during the scan
            (proc / "self").mkdir()
            with mock.patch.object(recovery.platform_module, "system", return_value="Linux"):
                table = recovery.process_table(proc_root=proc)
        self.assertEqual([process["pid"] for process in table["processes"]], [101])
        self.assertEqual(table["processes"][0]["cwd"], temp)
        self.assertEqual(table["incomplete"], [102])
        self.assertEqual(table["disappeared"], 2)
        self.assertEqual(self.observe(table)["reasons"], ["inspection_incomplete"])


class SessionMetadataBindingTests(unittest.TestCase):
    def test_descendant_cwd_records_are_inside_the_worktree_but_do_not_replace_the_root_anchor(self):
        import tempfile
        import session_paths
        session = "11111111-2222-4333-8444-555555555555"
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            (worktree / "apps" / "backend").mkdir(parents=True)
            outside = base / "elsewhere"
            outside.mkdir()
            (worktree / "linked").symlink_to(outside, target_is_directory=True)
            lookalike = base / "worktree-other" / "apps"
            lookalike.mkdir(parents=True)
            transcript = base / (session + ".jsonl")

            def write(*cwds):
                transcript.write_text("".join(json.dumps({"sessionId": session, "cwd": cwd} if cwd is not None else {"sessionId": session}) + "\n" for cwd in cwds))

            def code(*cwds, require=True):
                write(*cwds)
                try:
                    session_paths.validate_session_metadata(transcript, session, worktree, require_cwd=require)
                except session_paths.SessionPathError as exc:
                    return exc.code
                return "accepted"
            resolved = worktree.resolve()
            self.assertEqual(session_paths.cwd_relation(str(worktree), resolved), "root")
            self.assertEqual(session_paths.cwd_relation(str(worktree / "apps" / "backend" / "src"), resolved), "inside")
            self.assertEqual(session_paths.cwd_relation(str(worktree / "apps" / "backend" / "missing"), resolved), "inside")
            for bad in (str(outside), str(lookalike), str(worktree) + "-other", str(worktree / "linked"), str(worktree / "linked" / "deeper"),
                        "apps/backend", "", None, 7, str(worktree) + "\0x"):
                self.assertIsNone(session_paths.cwd_relation(bad, resolved), bad)
            # root anchor plus later descendant records: normal work inside the worktree
            self.assertEqual(code(str(worktree), str(worktree / "apps" / "backend" / "src"), str(worktree / "apps" / "backend"), str(worktree)), "accepted")
            self.assertEqual(code(str(worktree), str(worktree / "apps" / "backend"), require=False), "accepted")
            # descendant-only metadata keeps the worktree but does not replace the required anchor
            self.assertEqual(code(str(worktree / "apps" / "backend" / "src")), "transcript_worktree_unbound")
            self.assertEqual(code(str(worktree / "apps" / "backend" / "src"), require=False), "accepted")
            self.assertEqual(code(None), "transcript_worktree_unbound")
            # foreign worktree, lookalike prefix, symlink escape, relative and malformed values are contradictory
            for bad in (str(outside), str(lookalike), str(worktree) + "-other", str(worktree / "linked"), "apps/backend", "", 7):
                self.assertEqual(code(str(worktree), bad), "transcript_worktree_mismatch", bad)
                self.assertEqual(code(str(worktree), bad, require=False), "transcript_worktree_mismatch", bad)

    def test_require_cwd_distinguishes_unbound_from_contradictory_and_keeps_compatibility(self):
        import tempfile
        import session_paths
        session = "11111111-2222-4333-8444-555555555555"
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            worktree.mkdir()
            transcript = base / (session + ".jsonl")
            transcript.write_text(json.dumps({"sessionId": session, "type": "user"}) + "\n")
            session_paths.validate_session_metadata(transcript, session, worktree)  # compatible: cwd optional outside recovery
            with self.assertRaises(session_paths.SessionPathError) as unbound:
                session_paths.validate_session_metadata(transcript, session, worktree, require_cwd=True)
            self.assertEqual(unbound.exception.code, "transcript_worktree_unbound")
            transcript.write_text(json.dumps({"sessionId": session, "cwd": str(base / "other")}) + "\n")
            with self.assertRaises(session_paths.SessionPathError) as mismatch:
                session_paths.validate_session_metadata(transcript, session, worktree, require_cwd=True)
            self.assertEqual(mismatch.exception.code, "transcript_worktree_mismatch")
            transcript.write_text(json.dumps({"sessionId": session, "cwd": str(worktree)}) + "\n")
            session_paths.validate_session_metadata(transcript, session, worktree, require_cwd=True, opener=recovery.text_opener(1024))
            transcript.unlink()
            transcript.symlink_to(base / "elsewhere.jsonl")
            with self.assertRaises(recovery.ObservationError) as unsafe:  # the bounded opener refuses symlinks before any read
                session_paths.validate_session_metadata(transcript, session, worktree, require_cwd=True, opener=recovery.text_opener(1024))
            self.assertEqual(unsafe.exception.reason, "transcript_unsafe")


class ExplicitTranscriptTests(unittest.TestCase):
    def test_explicit_template_path_is_validated_and_must_be_unique(self):
        import tempfile
        import uuid as uuid_module
        import session_paths
        session = str(uuid_module.uuid4())
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            worktree.mkdir()
            explicit = base / (session + ".jsonl")
            explicit.write_text(json.dumps({"sessionId": session, "cwd": str(worktree)}) + "\n")
            config = {"session_transcript_path": str(base / "{session_id}.jsonl"), "claude_config_dir": str(base / "claude")}
            manifest = {"session_id": session, "session_transcript_path": str(explicit)}
            self.assertEqual(recovery.locate_transcript(config, manifest, worktree), str(explicit.resolve()))
            with self.assertRaisesRegex(session_paths.SessionPathError, "cwd"):
                recovery.locate_transcript(config, manifest, base / "other")
            duplicate = base / "claude" / "projects" / "hashed" / explicit.name
            duplicate.parent.mkdir(parents=True)
            duplicate.write_text(explicit.read_text())
            with self.assertRaisesRegex(session_paths.SessionPathError, "found 2"):
                recovery.locate_transcript(config, manifest, worktree)
            duplicate.unlink()
            explicit.unlink()
            explicit.symlink_to(base / "elsewhere.jsonl")
            with self.assertRaisesRegex(session_paths.SessionPathError, "regular file"):
                recovery.locate_transcript(config, manifest, worktree)


class RecoveryTransportTests(ProjectFixture):
    def test_tools_list_and_call_expose_recovery_operations_safely(self):
        from project_room_mcp import handle
        listing = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.service)["result"]["tools"]
        names = {tool["name"]: tool for tool in listing}
        self.assertEqual(len(names), 20)
        self.assertTrue(names["room_implementation_audit"]["annotations"]["readOnlyHint"])
        self.assertFalse(names["room_implementation_recover"]["annotations"]["readOnlyHint"])
        self.assertIn("recovery_id", names["room_implementation_submit"]["inputSchema"]["properties"])
        self.assertNotIn("recovery_id", names["room_implementation_submit"]["inputSchema"]["required"])
        response = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "room_implementation_audit",
                           "arguments": {"room_id": self.room_id, "handoff_id": "0" * 64, "job_id": "0" * 32}}}, self.service)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("Unknown handoff", response["result"]["content"][0]["text"])
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
