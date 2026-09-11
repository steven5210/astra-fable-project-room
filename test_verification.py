"""Audited verification-only retry after a completed model result; fake Claude, fake gates, temporary Git repositories."""

import contextlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import implementation
import project_room
import recovery
import room
import verification
from test_project_room import ProjectFixture

contextlib_suppress = contextlib.suppress
from test_recovery import FAKE as RECOVERY_FAKE, fixed_inspector


FAKE = (RECOVERY_FAKE
        .replace('"implementation_complete": True,', '"implementation_complete": mode != "complete-incomplete",')
        .replace('"remaining_gaps": []}', '"remaining_gaps": ["live continuity smoke pending"] if mode == "complete-incomplete" else []}')
        .replace('pathlib.Path("feature.txt").write_text("implemented\\n")\n',
                 'pathlib.Path("feature.txt").write_text("implemented\\n")\n'
                 'if packet.get("correction_request"):\n'
                 '    pathlib.Path("notes.txt").write_text(packet["correction_request"]["review_text"] + "\\n")  # candidate bytes change per correction\n'))
assert FAKE != RECOVERY_FAKE and "notes.txt" in FAKE

GATE = r'''
import os, subprocess, sys, tempfile, time
from pathlib import Path
flag = Path(sys.argv[1])
with open(str(flag) + ".runs", "a") as runs:
    runs.write("run\n")  # every invocation of this gate, original or retried, is counted outside the candidate
print("GATE_STARTED", flush=True)
mode = flag.read_text().strip() if flag.exists() else "sleep"
if mode == "sleep":
    time.sleep(30)
if mode == "survivor":
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd="/", start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("SLEEPER=%d" % sleeper.pid, flush=True)
scratch = Path(tempfile.gettempdir()) / "gate-scratch.txt"  # the private TMPDIR when the lane sets one; never the candidate when the host leaves TMPDIR unset
scratch.write_text("scratch")
print("TMPDIR=" + os.environ.get("TMPDIR", ""), flush=True)
print("MARKER=" + os.environ.get("PROJECT_ROOM_VERIFICATION_ID", ""), flush=True)
print("CWD=" + os.getcwd(), flush=True)
assert Path("feature.txt").read_text() == "implemented\n"
print("GIT=" + str(Path(".git").exists()), flush=True)
if mode == "mutate":
    Path("feature.txt").write_text("mutated\n")
if mode == "create":
    Path("created.txt").write_text("created\n")
if mode == "create-ignored":
    Path("__pycache__").mkdir(exist_ok=True)
    Path("__pycache__/x.pyc").write_text("x")
if mode == "bloat-ignored":
    Path("__pycache__").mkdir(exist_ok=True)
    Path("__pycache__/big.pyc").write_bytes(b"x" * 100)  # ignored by the fixture rules, but far beyond a tiny copy bound
if mode == "fail":
    sys.exit(3)
'''


class VerificationFixture(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.init_git()
        (self.project / ".gitignore").write_text("__pycache__/\n")
        self.git("add", ".gitignore")
        self.git("-c", "commit.gpgSign=false", "commit", "-qm", "ignore rules")
        self.review()
        self.approve()
        self.impl_fake = self.base / "fake-implementation"
        self.impl_fake.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        self.impl_fake.chmod(0o700)
        self.mode = self.base / "implementation-mode.txt"
        self.mode.write_text("complete-incomplete")
        profile_path = self.room_root / "profiles/implementation.json"
        profile = json.loads(profile_path.read_text())
        profile.update(claude_bin=str(self.impl_fake), timeout_seconds=8, gate_timeout_seconds=1)
        project_room.atomic_json(profile_path, profile)
        self.flag = self.base / "allow-verification"
        self.gates = [[sys.executable, "-c", GATE, str(self.flag)]]
        self.handoff = self.service.room_handoff(self.room_id, 1, "Build it through independent review.", self.gates)
        self.handoff_id = self.handoff["handoff_id"]
        self.handoff_dir = Path(self.handoff["handoff_path"]).parent
        self.worktree = Path(self.handoff["worktree_path"])
        self.authorization = "The user said: rerun exactly these offline gates in an isolated copy; no model call."
        self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5)

    def impl_calls(self):
        path = self.base / "implementation-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self):
        return json.loads((self.handoff_dir / "state.json").read_text())

    def attempt_dir(self):
        return Path(self.state()["attempt_path"])

    def gate_timeout(self, request_id="implement-1"):
        job = self.service.room_implementation_submit(self.room_id, self.handoff_id, request_id)
        terminal = self.service.room_job_status(job["id"], 40)
        self.assertEqual(terminal["status"], "uncertain", terminal)
        self.assertEqual(terminal["result"]["phase"], "blocked")
        return terminal

    def tree_hashes(self, directory):
        return {str(path.relative_to(directory)): room.sha(path.read_bytes()) for path in sorted(directory.rglob("*")) if path.is_file()}

    def audit(self, job_id):
        return self.service.room_verification_audit(self.room_id, self.handoff_id, job_id)

    def retry(self, job_id, report, request_id="verify-1", budget=5, **overrides):
        arguments = {"room_id": self.room_id, "handoff_id": self.handoff_id, "job_id": job_id,
                     "spec_revision": report["identity"]["spec_revision"], "spec_sha256": report["identity"]["spec_sha256"],
                     "candidate_sha256": (report.get("candidate") or {}).get("sha256") or "0" * 64,
                     "evidence_digest": report.get("evidence_digest") or "0" * 64, "gates_sha256": report.get("gates_sha256") or "0" * 64,
                     "gate_timeout_seconds": budget, "diagnosis": "Gate 1 exceeded the pinned budget after the model completed normally.",
                     "authorization": self.authorization, "request_id": request_id, **overrides}
        return self.service.call("room_verification_retry", arguments)

    def finish(self, job_id):
        return self.service.room_job_status(job_id, 40)

    def gate_runs(self):
        runs = Path(str(self.flag) + ".runs")
        return len(runs.read_text().splitlines()) if runs.exists() else 0

    def live_transcript(self):
        return next((self.base / "claude-storage" / "projects").glob("*/" + self.handoff["implementation_session_id"] + ".jsonl"))

    def held_popen(self):
        """Dispatch without a live worker: the controller's _worker spawn returns a held placeholder whose pid is this
        process's parent, so an in-process execute_job counts as that registered worker's child."""
        class Held:
            pid = os.getppid()

            def wait(self):
                return 0
        real_popen = subprocess.Popen
        return mock.patch.object(project_room.subprocess, "Popen", side_effect=lambda argv, *a, **k: Held() if "_worker" in argv else real_popen(argv, *a, **k))

    def dispatch_inline(self, job_id, report, request_id="verify-1", budget=5):
        with self.held_popen():
            dispatched = self.retry(job_id, report, request_id=request_id, budget=budget)
        with self.service.db() as db:
            db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (room.now(), dispatched["successor_job_id"]))
        return dispatched

    def run_inline(self, successor_job_id):
        """Run the registered verifier in this process under its held worker lease (the worker's terminal bookkeeping is not run)."""
        import fcntl
        with (self.service._job_path(successor_job_id) / "worker.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                return self.service.execute_job(successor_job_id)
            finally:
                fcntl.flock(lease, fcntl.LOCK_UN)

    def mark_job(self, job_id, status):
        with self.service.db() as db:
            db.execute("UPDATE jobs SET status=?,finished_at=? WHERE id=?", (status, room.now(), job_id))


class VerificationTests(VerificationFixture):
    def test_completed_generation_then_gate_timeout_is_audited_and_verified_without_the_model(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        state = self.state()
        self.assertEqual(state["model_return_code"], 0)
        self.assertFalse(state["report"]["implementation_complete"])
        self.assertTrue(state["error"].startswith("TimeoutExpired: Command '"))
        self.assertEqual(state["gate_results"], [])
        self.assertTrue((self.attempt_dir() / "gate-1" / "process-result.json").is_file())
        self.assertFalse(self.service.room_implementation_audit(self.room_id, self.handoff_id, job_id)["eligible"])  # the model lane refuses
        before = self.tree_hashes(self.attempt_dir())
        with self.service.db() as db:
            original_row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["boundary"], "isolated_copy")
        self.assertEqual(report["failed_gate"]["index"], 1)
        self.assertEqual(report["failed_gate"]["pinned_gate_timeout_seconds"], 1)
        self.assertEqual(report["generation"]["model_return_code"], 0)
        self.assertFalse(report["generation"]["implementation_complete"])
        self.assertTrue(report["transcript"]["matches_identity"])
        self.assertEqual(report["budget"], {"original_seconds": 1, "minimum_seconds": 1, "maximum_seconds": 7200, "proposed_seconds": 900})
        self.assertEqual(report["stopped_work"]["label"], "isolated_copy_legacy_receipts")
        for canary in ("PRIVATE_PROMPT_CANARY", "IMPLEMENTATION PACKET", str(self.handoff_dir), "--session-id", "TimeoutExpired", "GATE_STARTED"):
            self.assertNotIn(canary, json.dumps(report))
        self.assertEqual(self.impl_calls()[-1]["session"], self.handoff["implementation_session_id"])
        # Budget validation: boolean, zero, above the ceiling and non-integers are refused; nothing is dispatched.
        for bad in (True, 0, 7201, 2.5):
            with self.assertRaisesRegex(room.RoomError, "gate_timeout_seconds"):
                self.retry(job_id, report, request_id="bad-budget", budget=bad)
        with self.assertRaisesRegex(room.RoomError, "candidate_sha256"):
            self.retry(job_id, report, request_id="bad-candidate", candidate_sha256="1" * 64)
        with self.assertRaisesRegex(room.RoomError, "gates_sha256"):
            self.retry(job_id, report, request_id="bad-gates", gates_sha256="1" * 64)
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        self.assertEqual(self.state()["phase"], "blocked")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-must-not-start")
        # The retry: exactly the pinned gates rerun in a private copy; the fake model is never called again.
        self.flag.write_text("pass")
        calls = len(self.impl_calls())
        dispatched = self.retry(job_id, report)
        self.assertEqual(dispatched["status"], "dispatched")
        self.assertFalse(dispatched["duplicate"])
        verifier = self.finish(dispatched["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertEqual(verifier["kind"], "verification")
        self.assertEqual(verifier["result"]["phase"], "awaiting_astra_review")
        self.assertTrue(verifier["result"]["gates_passed"])
        self.assertEqual(verifier["result"]["verification"]["result"], "completed")
        self.assertEqual(len(self.impl_calls()), calls)
        self.assertEqual(self.tree_hashes(self.attempt_dir()), before)
        with self.service.db() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), original_row)
        state = self.state()
        self.assertEqual(state["phase"], "awaiting_astra_review")
        self.assertIsNone(state["error"])
        self.assertIsNone(state["verification"])
        self.assertEqual([entry["status"] for entry in state["verification_history"]], ["launched", "consumed"])
        gate = state["gate_results"][0]
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        self.assertEqual(Path(gate["stdout_path"]).parent, home / "gate-1")
        stdout = Path(gate["stdout_path"]).read_text()
        self.assertIn("TMPDIR=" + str(home / "tmp"), stdout)
        self.assertIn("MARKER=" + dispatched["verification_id"], stdout)
        self.assertIn("CWD=" + str(home / "candidate"), stdout)
        self.assertIn("GIT=False", stdout)  # repository metadata is not copied
        self.assertFalse((home / "candidate").exists())
        self.assertFalse((home / "tmp").exists())
        record = json.loads((home / "record.json").read_text())
        self.assertEqual(record["boundary"], "isolated_copy")
        self.assertEqual(record["budget_seconds"], 5)
        self.assertEqual(record["original_gate_timeout_seconds"], 1)
        self.assertEqual(record["authorization"], self.authorization)
        self.assertEqual(record["failed_state_snapshot"]["phase"], "blocked")
        self.assertTrue(record["failed_state_snapshot"]["error"].startswith("TimeoutExpired"))
        self.assertEqual(room.sha((home / "transcript-snapshot.jsonl").read_bytes()), report["transcript"]["sha256"])
        outcome = json.loads((home / "outcome.json").read_text())
        self.assertEqual(outcome["result"], "completed")
        self.assertEqual(outcome["cleanup"]["status"], "removed")
        self.assertEqual(json.loads((self.handoff_dir / "implementation-config.json").read_text())["gate_timeout_seconds"], 1)
        status = self.service.room_status(self.room_id)
        old = next(job for job in status["jobs"] if job["id"] == job_id)
        self.assertEqual(old["status"], "uncertain")
        self.assertEqual(old["superseded_by"], dispatched["successor_job_id"])
        self.assertEqual(status["verifications"][0]["status"], "consumed")
        self.assertEqual(status["verifications"][0]["outcome_sha256"], verifier["result"]["verification"]["outcome_sha256"])
        self.assertEqual(status["handoffs"][0]["lineage"]["verification_history"][-1]["status"], "consumed")
        self.assertTrue(status["ready_for_handoff"])
        projected = self.service.room_implementation_status(self.room_id, self.handoff_id)
        self.assertEqual(projected["phase"], "awaiting_astra_review")
        self.assertEqual(projected["verification"]["history"][-1]["status"], "consumed")
        kinds = [event["kind"] for event in self.service.room_history(self.room_id)["events"]]
        self.assertIn("implementation_verification_dispatched", kinds)
        self.assertIn("implementation_verification_consumed", kinds)
        duplicate = self.retry(job_id, report)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["verification_id"], dispatched["verification_id"])
        with self.assertRaisesRegex(room.RoomError, "different content"):
            self.retry(job_id, report, budget=6)
        with self.assertRaisesRegex(room.RoomError, "job_not_eligible"):
            self.retry(job_id, report, request_id="verify-2")
        # A passed suite with an incomplete report is still unacceptable; a normal correction resumes the same session.
        with self.assertRaisesRegex(implementation.ImplementationError, "incomplete implementation, or remaining gaps"):
            self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Gates passed but the report is incomplete.")
        revised = self.service.room_implementation_revise(self.room_id, self.handoff_id, "Finish the pending live smoke and complete the report.")
        self.assertEqual(revised["phase"], "correction_pending")
        self.mode.write_text("normal")
        self.flag.write_text("pass")
        corrected = self.finish(self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-correction")["id"])
        self.assertEqual(corrected["status"], "succeeded", corrected)
        self.assertEqual(corrected["result"]["attempt_count"], 2)
        call = self.impl_calls()[-1]
        self.assertIn("--resume", call["argv"])
        self.assertEqual(call["packet"]["previous_gate_output"][0]["stdout"], stdout)
        self.assertEqual(call["packet"]["previous_gate_results"][0]["verification_id"], dispatched["verification_id"])
        accepted = self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Inspected the finished feature and fresh gate evidence.")
        self.assertEqual(accepted["phase"], "accepted")
        self.assertEqual(self.service.room_job_status(job_id)["status"], "uncertain")
        self.assertEqual(self.tree_hashes(self.attempt_dir().parent / "0001"), before)

    def test_failing_gate_yields_failed_evidence_and_refusals_never_launch(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        attempt = self.attempt_dir()
        state_bytes = (self.handoff_dir / "state.json").read_bytes()
        cases = []
        def case(name, setup, teardown, expected):
            cases.append((name, setup, teardown, expected))
        lease = (self.service._job_path(job_id) / "worker.lock").open("a")
        import fcntl
        case("held lease", lambda: fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB), lambda: fcntl.flock(lease, fcntl.LOCK_UN), "cooperating_owner_active")
        receipt = attempt / "gate-1" / "process-result.json"
        original_receipt = receipt.read_bytes()
        case("missing gate receipt", lambda: receipt.unlink(), lambda: receipt.write_bytes(original_receipt), "gate_evidence_missing")
        case("malformed gate receipt", lambda: receipt.write_text("{}"), lambda: receipt.write_bytes(original_receipt), "gate_receipt_invalid")
        planted = attempt / "gate-2"
        case("planted later gate", lambda: planted.mkdir(), lambda: planted.rmdir(), "gate_index_ambiguous")
        stdout = attempt / "stdout.json"
        original_stdout = stdout.read_bytes()
        case("tampered model stdout", lambda: stdout.write_bytes(original_stdout + b"\n"), lambda: stdout.write_bytes(original_stdout), "model_result_mismatch")
        def wrong_error():
            state = self.state(); state["error"] = re.sub(r"after [0-9.e+-]+ seconds$", "after 5.0 seconds", state["error"])
            implementation._atomic(self.handoff_dir / "state.json", state)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=json_set(result,'$.error',?) WHERE id=?", (state["error"], job_id))
        def restore_state():
            (self.handoff_dir / "state.json").write_bytes(state_bytes)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=json_set(result,'$.error',?) WHERE id=?", (json.loads(state_bytes)["error"], job_id))
        case("wrong timeout seconds", wrong_error, restore_state, "timeout_seconds_out_of_range")
        def model_error():
            state = self.state(); state["error"] = "ImplementationError: Claude did not return a successful terminal result for the exact implementation session"
            implementation._atomic(self.handoff_dir / "state.json", state)
            with self.service.db() as db:
                db.execute("UPDATE jobs SET result=json_set(result,'$.error',?) WHERE id=?", (state["error"], job_id))
        case("model error text", model_error, restore_state, "gate_error_mismatch")
        live = next((self.base / "claude-storage" / "projects").glob("*/" + self.handoff["implementation_session_id"] + ".jsonl"))
        original_transcript = live.read_bytes()
        appended = json.dumps({"type": "assistant", "sessionId": self.handoff["implementation_session_id"], "cwd": str(self.worktree), "isSidechain": False}).encode() + b"\n"
        case("appended transcript record", lambda: live.write_bytes(original_transcript + appended), lambda: live.write_bytes(original_transcript), "transcript_changed")
        case("rewritten transcript prefix", lambda: live.write_bytes(original_transcript.replace(b"\n", b" \n", 1)), lambda: live.write_bytes(original_transcript), "transcript_changed")
        case("malformed transcript bytes", lambda: live.write_bytes(original_transcript + b"\n"), lambda: live.write_bytes(original_transcript), "transcript_unparsable")
        extra = self.worktree / "extra.txt"
        case("changed candidate", lambda: extra.write_text("new\n"), lambda: extra.unlink(), "candidate_changed")
        link = self.worktree / "linked.txt"
        case("symlink candidate entry", lambda: link.symlink_to("feature.txt"), lambda: link.unlink(), "candidate_changed")
        receipt_pid = json.loads(original_receipt)["pid"]
        def gate_survivor():
            self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5, processes=[
                {"pid": 424242, "ppid": 1, "pgid": receipt_pid, "uid": os.getuid(), "args": "detached test worker", "cwd": "/"}])
        def restore_inspector():
            self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5)
        case("surviving gate process group", gate_survivor, restore_inspector, "gate_process_present")
        def worktree_writer():
            self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5, processes=[
                {"pid": 424243, "ppid": 1, "pgid": 424243, "uid": os.getuid(), "args": "sleep", "cwd": str(self.worktree)}])
        case("worktree cwd writer", worktree_writer, restore_inspector, "writer_present")
        case("inspection unavailable", lambda: setattr(self.service, "process_inspector", fixed_inspector(error="inspection_unavailable")), restore_inspector, "inspection_unavailable")
        def open_finding():
            with self.service.db() as db:
                db.execute("INSERT INTO issues(id,room_id,job_id,revision,content,severity,disposition) VALUES('open-1',?,?,1,'finding','blocker','open')", (self.room_id, job_id))
        def close_finding():
            with self.service.db() as db:
                db.execute("DELETE FROM issues WHERE id='open-1'")
        case("open finding", open_finding, close_finding, "open_findings")
        calls = len(self.impl_calls())
        for name, setup, teardown, expected in cases:
            with self.subTest(case=name):
                setup()
                try:
                    refused = self.audit(job_id)
                    self.assertFalse(refused["eligible"], refused)
                    self.assertIn(expected, refused["reasons"], refused["reasons"])
                    with self.assertRaisesRegex(room.RoomError, expected):
                        self.retry(job_id, {**report, **{key: refused[key] for key in ("candidate", "evidence_digest", "gates_sha256") if refused.get(key)}},
                                   request_id="refused-" + name)
                finally:
                    teardown()
                self.assertEqual(self.state()["phase"], "blocked")
                self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        lease.close()
        self.assertEqual(len(self.impl_calls()), calls)
        self.assertFalse((self.handoff_dir / "verifications").exists())
        with self.assertRaisesRegex(room.RoomError, "Unknown handoff"):
            self.service.room_verification_audit(self.room_id, "0" * 64, job_id)
        # A failing gate outcome is recorded as failed evidence, not acceptance.
        self.flag.write_text("fail")
        dispatched = self.retry(self.audit(job_id) and job_id, self.audit(job_id))
        verifier = self.finish(dispatched["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertFalse(verifier["result"]["gates_passed"])
        self.assertEqual(self.state()["gate_results"][0]["return_code"], 3)
        with self.assertRaisesRegex(implementation.ImplementationError, "failed gates"):
            self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Trying to accept failed gates.")
        self.assertEqual(len(self.impl_calls()), calls)

    def test_gate_mutating_or_creating_candidate_paths_interrupts_and_preserves_original(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        for mode, expected in (("mutate", "gate_changed_candidate"), ("create", "gate_created_candidate_path")):
            with self.subTest(mode=mode):
                self.flag.write_text(mode)
                report = self.audit(job_id)
                self.assertTrue(report["eligible"], report)
                dispatched = self.retry(job_id, report, request_id="verify-" + mode)
                verifier = self.finish(dispatched["successor_job_id"])
                self.assertEqual(verifier["status"], "uncertain", verifier)
                self.assertEqual(verifier["result"]["verification"]["reason"], expected)
                state = self.state()
                self.assertEqual(state["phase"], "blocked")
                self.assertTrue(state["error"].startswith("TimeoutExpired"))
                self.assertIsNone(state["verification"])
                self.assertEqual(state["verification_history"][-1]["status"], "interrupted")
                rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
                self.assertEqual(rows[dispatched["verification_id"]]["status"], "interrupted")
                self.assertEqual(rows[dispatched["verification_id"]]["reason"], expected)
                self.assertEqual((self.worktree / "feature.txt").read_text(), "implemented\n")
                self.assertFalse((self.worktree / "created.txt").exists())
                with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
                    self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-" + mode)
        # Ignored creations are tolerated, matching the normal lane's candidate semantics; earlier interrupted verifiers
        # are exempt only through the registered lineage of this exact predecessor.
        self.flag.write_text("create-ignored")
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        dispatched = self.retry(job_id, report, request_id="verify-ignored")
        verifier = self.finish(dispatched["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertTrue(verifier["result"]["gates_passed"])
        self.assertEqual(self.state()["gate_results"][0]["copy_verified"]["created_ignored"], 1)
        self.assertTrue(self.service.room_status(self.room_id)["ready_for_handoff"])
        self.assertEqual(len(self.impl_calls()), 1)

    def test_verifier_timeout_is_preserved_and_a_second_retry_needs_a_fresh_audit(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        report = self.audit(job_id)
        dispatched = self.retry(job_id, report, budget=1)  # the flag is absent: the gate sleeps past the recorded budget again
        verifier = self.finish(dispatched["successor_job_id"])
        self.assertEqual(verifier["status"], "uncertain", verifier)
        self.assertEqual(verifier["result"]["verification"]["reason"], "gate_timeout:1")
        self.assertEqual(verifier["error"], "verification_interrupted:gate_timeout:1")
        self.assertNotIn("TimeoutExpired", json.dumps(verifier["result"]["verification"]))
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired: Command '"))
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        self.assertTrue((home / "gate-1" / "process-start.json").is_file())
        self.assertEqual(json.loads((home / "outcome.json").read_text())["result"], "interrupted")
        rows = self.service.room_status(self.room_id)["verifications"]
        self.assertEqual([row["status"] for row in rows], ["interrupted"])
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-timeout")
        # No automatic retry: the interrupted verifier's own pids are part of the next audit's process check.
        pid = json.loads((home / "gate-1" / "process-start.json").read_text())["pid"]
        self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5, processes=[
            {"pid": pid, "ppid": 1, "pgid": pid, "uid": os.getuid(), "args": "sleep", "cwd": "/"}])
        self.assertIn("gate_process_present", self.audit(job_id)["reasons"])
        self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5)
        self.flag.write_text("pass")
        fresh = self.audit(job_id)
        self.assertTrue(fresh["eligible"], fresh)
        second = self.retry(job_id, fresh, request_id="verify-2")
        self.assertNotEqual(second["verification_id"], dispatched["verification_id"])
        verifier = self.finish(second["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        status = self.service.room_status(self.room_id)
        self.assertTrue(status["ready_for_handoff"])
        exempt = {job["id"]: job["superseded_by"] for job in status["jobs"] if job["status"] == "uncertain"}
        self.assertEqual(set(exempt), {job_id, dispatched["successor_job_id"]})
        self.assertEqual(len(self.impl_calls()), 1)
        # A tampered interrupted-verifier record no longer exempts that earlier verifier job; nothing else is unblocked.
        record = home / "record.json"
        record.write_bytes(record.read_bytes() + b"\n")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_revise(self.room_id, self.handoff_id, "Diagnosed correction.")

    def test_planted_outcome_or_record_without_registration_does_not_unblock(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        home = self.handoff_dir / "verifications" / ("b" * 32)
        home.mkdir(parents=True)
        (home / "record.json").write_text("{}")
        (home / "outcome.json").write_text(json.dumps({"verification_id": "b" * 32, "result": "completed", "gates_passed": True}))
        state = self.state()
        state["phase"] = "awaiting_astra_review"
        state["gates_passed"] = True
        implementation._atomic(self.handoff_dir / "state.json", state)
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Planted evidence must not count.")
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-plant")
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        # Restoring the projection makes the attempt auditable again; the planted, unregistered files change nothing.
        (self.handoff_dir / "state.json").write_bytes(json.dumps({**state, "phase": "blocked", "gates_passed": False}).encode())
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        self.assertIsNone(report["existing_verification"])
        self.flag.write_text("pass")
        dispatched = self.retry(job_id, report)
        self.assertEqual(self.finish(dispatched["successor_job_id"])["status"], "succeeded")
        self.assertEqual([row["id"] for row in self.service.room_status(self.room_id)["verifications"]], [dispatched["verification_id"]])
        self.assertEqual((home / "record.json").read_text(), "{}")

    def test_worker_recheck_refusal_before_launch_spawns_no_gate(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        class Held:
            pid = os.getppid()
            def wait(self):
                return 0
        real_popen = subprocess.Popen
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=lambda argv, *a, **k: Held() if "_worker" in argv else real_popen(argv, *a, **k)):
            dispatched = self.retry(job_id, report)
        record = self.handoff_dir / "verifications" / dispatched["verification_id"] / "record.json"
        record.write_bytes(record.read_bytes() + b"\n")
        with mock.patch.object(implementation, "_run_child", side_effect=RuntimeError("gate launch reached")) as launch:
            self.service.worker(dispatched["successor_job_id"])
        self.assertFalse(launch.called)
        outcome = self.service.room_job_status(dispatched["successor_job_id"])
        self.assertEqual(outcome["status"], "failed", outcome)
        self.assertIn("evidence_changed", outcome["error"])
        self.assertFalse(outcome["result"]["gates_launched"])
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual(rows[dispatched["verification_id"]]["status"], "invalidated")
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertIsNone(state["verification"])
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertEqual(state["verification_history"][-1]["status"], "invalidated")
        self.assertFalse((self.handoff_dir / "verifications" / dispatched["verification_id"] / "candidate").exists())
        fresh = self.audit(job_id)
        self.assertTrue(fresh["eligible"], fresh)
        again = self.retry(job_id, fresh, request_id="verify-2")
        self.assertEqual(self.finish(again["successor_job_id"])["status"], "succeeded")
        self.assertEqual(len(self.impl_calls()), 1)

    def test_model_timeout_shape_is_not_eligible_here(self):
        self.mode.write_text("interrupt-timeout")
        job = self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-timeout")
        terminal = self.service.room_job_status(job["id"], 40)
        self.assertEqual(terminal["status"], "uncertain", terminal)
        report = self.audit(job["id"])
        self.assertFalse(report["eligible"])
        self.assertIn("attempt_identity_mismatch", report["reasons"])  # the model never finished: no model_finished_at, no parsed result
        with self.assertRaisesRegex(room.RoomError, "not eligible"):
            self.retry(job["id"], report, request_id="verify-timeout")


class VerificationStatusTests(unittest.TestCase):
    def test_status_projection_and_progress_cover_the_verifying_phase_and_older_rooms(self):
        import datetime
        import handoff_status
        import progress
        manifest = {"handoff_id": "a" * 64, "revision": 1, "spec_sha256": "b" * 64, "baseline_commit": "c" * 40, "gates": [["true"]]}
        older = {"phase": "blocked", "attempt_count": 1, "error": "PRIVATE_ERROR", "recovery_history": []}
        projected = handoff_status.project("room", manifest, older)
        self.assertEqual(projected["phase"], "blocked")
        self.assertEqual(projected["verification"], {"boundary": "isolated_copy", "active": None, "history": [], "truncated": False,
                                                     "meaning": "gate evidence lineage only; acceptance still requires a complete report and Astra review"})
        self.assertEqual(verification.lineage(older), {"active_verification": None, "verification_history": []})
        now = datetime.datetime.now(datetime.timezone.utc)
        started = (now - datetime.timedelta(seconds=30)).isoformat()
        state = {"phase": "verifying", "attempt_count": 1, "owner_job_id": "j" * 32, "started_at": started, "error": "PRIVATE_ERROR",
                 "active_stage": {"kind": "gate", "index": 1, "started_at": started, "budget_seconds": 900, "verification_id": "v" * 32},
                 "verification": {"verification_id": "v" * 32, "predecessor_job_id": "p" * 32, "predecessor_attempt": 1, "successor_job_id": "j" * 32,
                                  "gate_index": 1, "boundary": "isolated_copy", "budget_seconds": 900, "launch_state": "running", "dispatched_at": started,
                                  "candidate": {"sha256": "d" * 64, "head": "c" * 40, "entries": []}},
                 "verification_history": [{"verification_id": "v" * 32, "status": "launched", "at": started, "predecessor_attempt": 1,
                                           "successor_job_id": "j" * 32, "budget_seconds": 900, "launched_at": started, "reason": "PRIVATE_REASON"}]}
        projected = handoff_status.project("room", manifest, state)
        self.assertEqual(projected["phase"], "verifying")
        self.assertEqual(projected["verification"]["active"]["budget_seconds"], 900)
        self.assertEqual(projected["verification"]["history"][0]["status"], "launched")
        self.assertNotIn("PRIVATE", json.dumps(projected))
        job = {"id": "j" * 32, "kind": "verification", "status": "running", "created_at": started, "started_at": started, "payload": {}}
        handoff = {"state": state, "manifest": manifest, "config": {"gate_timeout_seconds": 300, "timeout_seconds": 3600}, "config_verified": True,
                   "limitations": set(), "transcript": {}}
        value = progress.job_progress(job, now, handoff=handoff)
        self.assertEqual(value["phase"], "gate")
        self.assertEqual(value["phase_detail"], "verification_retry")
        self.assertEqual(value["deadline"]["timeout_seconds"], 900)
        self.assertEqual(value["deadline"]["basis"], "recorded_verification_budget")
        self.assertEqual(value["gate"], {"index": 1, "count": 1})
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(verification.lineage(state)["active_verification"]["budget_seconds"], 900)
        self.assertEqual(verification.linkage({"phase": "refused_before_launch", "reason": "evidence_changed"}, "failed"), ("invalidated", "evidence_changed"))
        self.assertEqual(verification.linkage({"verification": {"result": "interrupted", "reason": "gate_timeout:1"}}, "uncertain"), ("interrupted", "gate_timeout:1"))
        self.assertEqual(verification.linkage(None, "uncertain"), ("unknown", "no_classifiable_result"))
        with self.assertRaisesRegex(room.RoomError, "gate_timeout_seconds"):
            verification.validate_budget(True, 300)
        with self.assertRaisesRegex(room.RoomError, "at least the original"):
            verification.validate_budget(299, 300)
        self.assertEqual(verification.validate_budget(900, 300), 900)


class VerificationDurabilityTests(VerificationFixture):
    def test_durable_registered_outcome_reconciles_after_a_crash_without_rerunning_gates(self):
        class SimulatedCrash(BaseException):
            pass
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        dispatched = self.dispatch_inline(job_id, report)
        runs, calls = self.gate_runs(), len(self.impl_calls())
        with mock.patch.object(verification, "project_completion", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
            self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(self.gate_runs(), runs + 1)
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        outcome_bytes = (home / "outcome.json").read_bytes()
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual(rows[dispatched["verification_id"]]["status"], "dispatched")
        self.assertEqual(rows[dispatched["verification_id"]]["outcome_sha256"], room.sha(outcome_bytes))  # registered before the file, by the owning execution
        self.assertEqual(self.state()["phase"], "verifying")
        self.mark_job(dispatched["successor_job_id"], "uncertain")  # the worker vanished before recording anything
        reconciled = self.retry(job_id, report, request_id="verify-2")
        self.assertTrue(reconciled["reconciled"], reconciled)
        self.assertEqual((reconciled["status"], reconciled["verification_id"], reconciled["reason"]), ("consumed", dispatched["verification_id"], "reconciled"))
        state = self.state()
        self.assertEqual(state["phase"], "awaiting_astra_review")
        self.assertTrue(state["gates_passed"])
        self.assertIsNone(state["error"])
        self.assertEqual(Path(state["gate_results"][0]["stdout_path"]).parent, home / "gate-1")
        self.assertEqual(self.gate_runs(), runs + 1)  # reconciliation executed nothing
        self.assertEqual(len(self.impl_calls()), calls)
        self.assertEqual((home / "outcome.json").read_bytes(), outcome_bytes)
        status = self.service.room_status(self.room_id)
        self.assertTrue(status["ready_for_handoff"], status["jobs"])
        jobs = {job["id"]: job for job in status["jobs"]}
        self.assertEqual(jobs[job_id]["superseded_by"], dispatched["successor_job_id"])
        self.assertEqual(jobs[dispatched["successor_job_id"]]["status"], "uncertain")  # the vanished worker's row is never rewritten
        with self.assertRaisesRegex(implementation.ImplementationError, "incomplete implementation, or remaining gaps"):
            self.service.room_implementation_review(self.room_id, self.handoff_id, True, "Passed gates, incomplete report.")
        # A digest registered without a durable file is never adopted: the second window invalidates and needs a fresh retry.
        revised = self.service.room_implementation_revise(self.room_id, self.handoff_id, "Finish the report.")
        self.assertEqual(revised["phase"], "correction_pending")
        self.flag.unlink()
        second = self.finish(self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2")["id"])
        self.assertEqual(second["status"], "uncertain", second)
        self.flag.write_text("pass")
        report = self.audit(second["id"])
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["generation"]["attempt_count"], 2)
        dispatched = self.dispatch_inline(second["id"], report, request_id="verify-3")
        runs = self.gate_runs()
        with mock.patch.object(verification, "_write_outcome", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
            self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(self.gate_runs(), runs + 1)
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        again = self.retry(second["id"], report, request_id="verify-4")
        self.assertFalse(again["reconciled"])
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual(rows[dispatched["verification_id"]]["status"], "invalidated")
        self.assertTrue(rows[dispatched["verification_id"]]["reason"].startswith("verifier_incomplete"))
        self.assertEqual(self.finish(again["successor_job_id"])["status"], "succeeded")
        self.assertEqual(self.gate_runs(), runs + 2)  # the fresh retry ran the gate again; the lost outcome never counted

    def test_marked_surviving_worker_refuses_the_pass_and_blocks_the_next_audit_until_it_exits(self):
        import signal
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.service.process_inspector = None  # real platform observation, including the environment marker scan
        self.flag.write_text("survivor")
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        dispatched = self.retry(job_id, report)
        verifier = self.finish(dispatched["successor_job_id"])
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        stdout = (home / "gate-1" / "stdout.txt").read_text()
        pid = int(re.search(r"SLEEPER=(\d+)", stdout).group(1))
        try:
            self.assertEqual(verifier["status"], "uncertain", verifier)
            self.assertEqual(verifier["result"]["verification"]["reason"], "survivors_observed")
            self.assertEqual([entry["match"] for entry in verifier["result"]["verification"]["cleanup"]["survivors"]], [["marker_env"]])
            self.assertEqual(verifier["result"]["verification"]["cleanup"]["survivors"][0]["pid"], pid)
            self.assertTrue((home / "candidate").is_dir())  # cleanup deferred, never hidden
            state = self.state()
            self.assertEqual(state["phase"], "blocked")
            self.assertTrue(state["error"].startswith("TimeoutExpired"))
            rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
            self.assertEqual((rows[dispatched["verification_id"]]["status"], rows[dispatched["verification_id"]]["reason"]), ("interrupted", "survivors_observed"))
            refused = self.audit(job_id)
            self.assertFalse(refused["eligible"])
            self.assertIn("gate_process_present", refused["reasons"])
            self.assertIn({"pid": pid, "match": ["marker_env"]}, refused["stopped_work"]["gate_processes"])
            self.assertNotIn("PROJECT_ROOM_VERIFICATION_ID=", json.dumps(refused))
        finally:
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
        self.flag.write_text("pass")
        fresh = self.audit(job_id)
        self.assertTrue(fresh["eligible"], fresh)
        again = self.retry(job_id, fresh, request_id="verify-2")
        self.assertEqual(self.finish(again["successor_job_id"])["status"], "succeeded")
        self.assertEqual(len(self.impl_calls()), 1)


class VerificationMarkerScanTests(unittest.TestCase):
    """The environment-marker scan stays fail-closed: an unreadable same-user process refuses the observation."""

    def test_incomplete_marker_scan_refuses_the_gate_observation(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="verification-marker-") as temp:
            worktree, root = Path(temp) / "worktree", Path(temp) / "verifications"
            worktree.mkdir(); root.mkdir()
            inspector = fixed_inspector(boot=int(time.time()) - 5)
            for incomplete in (1, 0):
                with mock.patch.object(verification, "marker_processes", return_value={"method": "fixture", "matched": [], "incomplete": incomplete, "skipped": 0}):
                    observation = verification.observe_gate(room.now(), set(), "session-fixture", worktree, root, inspector=inspector, markers=["a" * 32])
                self.assertEqual("inspection_incomplete" in observation["reasons"], bool(incomplete), observation)
                self.assertEqual(observation["incomplete_count"], incomplete)
                self.assertEqual(observation["gate_processes"], [])

    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() != 0, "Linux /proc environ ownership as a non-root user")
    def test_same_user_zombie_is_incomplete_until_reaped(self):
        # A zombie keeps a same-user /proc/<pid> directory, but its environ is root-owned, so the bounded read is denied:
        # the scan counts it as incomplete rather than proving it carries no marker. Reaping restores a complete scan,
        # which is why the CI container runs under an init process.
        import signal
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.kill(child.pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if Path("/proc/%d/stat" % child.pid).read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
                time.sleep(0.05)
            else:
                self.fail("the killed child never became a zombie")
            before = verification.marker_processes(["a" * 32])
            self.assertEqual(before["method"], "/proc environ")
            self.assertGreaterEqual(before["incomplete"], 1, before)
        finally:
            child.wait()
        after = verification.marker_processes(["a" * 32])
        self.assertLess(after["incomplete"], before["incomplete"], (before, after))


class VerificationBoundsTests(unittest.TestCase):
    """The verifier's own read bounds hold for every visited byte of the copy and for every captured listing."""

    def pinned(self, root):
        tracked = root / "tracked.txt"
        tracked.write_bytes(b"a")
        return {"entries": [{"path": "tracked.txt", "kind": "file", "mode": tracked.stat().st_mode & 0o777, "sha256": room.sha(b"a")}]}

    def test_ignored_file_growing_after_its_stat_is_refused_by_the_bounded_read(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="verification-bounds-") as temp:
            root = Path(temp)
            pinned = self.pinned(root)
            ignored = root / "cache.tmp"
            ignored.write_bytes(b"x")
            inode = ignored.stat().st_ino
            original_fdopen = os.fdopen
            grown = []

            def grow_after_stat(fd, *rest, **kwargs):
                if os.fstat(fd).st_ino == inode:
                    ignored.write_bytes(b"x" * 32)  # the race Astra modelled: growth between the stat and the read
                    grown.append(True)
                return original_fdopen(fd, *rest, **kwargs)
            with mock.patch.object(verification, "COPY_FILE_LIMIT", 8), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 64), \
                    mock.patch.object(verification, "_check_ignore", return_value={"cache.tmp"}), mock.patch.object(verification.os, "fdopen", side_effect=grow_after_stat):
                observed = verification._verify_copy(root, pinned, root)
            self.assertTrue(grown)
            self.assertEqual(observed["reason"], "copy_bounds_exceeded", observed)
            self.assertEqual(observed["detail"], ["cache.tmp"])

    def test_aggregate_visited_bytes_are_bounded_while_in_bound_ignored_files_keep_candidate_semantics(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="verification-bounds-") as temp:
            root = Path(temp)
            pinned = self.pinned(root)
            for name in ("cache-one.tmp", "cache-two.tmp"):
                (root / name).write_bytes(b"x" * 12)  # each within the per-file bound; together beyond the aggregate bound
            with mock.patch.object(verification, "COPY_FILE_LIMIT", 16), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 16), \
                    mock.patch.object(verification, "_check_ignore", return_value={"cache-one.tmp", "cache-two.tmp"}):
                observed = verification._verify_copy(root, pinned, root)
            self.assertEqual(observed["reason"], "copy_bounds_exceeded", observed)
            # Directory enumeration order is unspecified: the first 12-byte file fits the remaining aggregate budget (16 bytes
            # shared with the 1-byte tracked file) and whichever file is visited second trips it, so exactly one is reported.
            self.assertEqual(len(observed["detail"]), 1, observed)
            self.assertIn(observed["detail"][0], ("cache-one.tmp", "cache-two.tmp"))
            (root / "cache-two.tmp").unlink()
            (root / "cache-one.tmp").write_bytes(b"x" * 5)
            with mock.patch.object(verification, "COPY_FILE_LIMIT", 16), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 16), \
                    mock.patch.object(verification, "_check_ignore", return_value={"cache-one.tmp"}):
                observed = verification._verify_copy(root, pinned, root)
            self.assertIsNone(observed["reason"], observed)
            self.assertEqual((observed["entries"], observed["created_ignored"]), (1, 1))
            with mock.patch.object(verification, "COPY_FILE_LIMIT", 16), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 16), \
                    mock.patch.object(verification, "_check_ignore", return_value=set()):
                observed = verification._verify_copy(root, pinned, root)
            self.assertEqual(observed["reason"], "gate_created_candidate_path")  # a non-ignored creation is still a candidate change

    def test_bounded_capture_stops_its_own_child_on_oversized_output_and_timeouts(self):
        token = "capture-nonce-%s" % os.urandom(8).hex()  # the leak check below matches only this test's own child, never a sibling suite's
        flood = [sys.executable, "-c", "import sys  # %s\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()" % token]
        with self.assertRaises(recovery.ObservationError) as caught:
            verification._bounded_capture(flood, 100000, timeout=20)
        self.assertEqual(caught.exception.reason, "listing_oversized")
        sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(recovery.ObservationError) as caught:
            verification._bounded_capture(sleeper, 1000, timeout=0.5)
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("listing_unavailable", "timeout"))
        code, data = verification._bounded_capture([sys.executable, "-c", "print('ok')"], 1000, timeout=20)
        self.assertEqual((code, data), (0, b"ok\n"))
        with self.assertRaises(recovery.ObservationError) as caught:
            verification._bounded_capture([sys.executable, "-c", "print('never')"], 1000, timeout=20, input_bytes=b"x" * (verification.CAPTURE_INPUT_LIMIT + 1))
        self.assertEqual(caught.exception.detail, "input")
        self.assertNotIn("never", str(caught.exception))
        self.assertFalse(subprocess.run(["pgrep", "-f", token], capture_output=True).stdout.strip())  # no runaway child of this test

    def test_bounded_capture_deadline_holds_when_a_grandchild_keeps_the_pipe_open_and_stdin_never_deadlocks(self):
        import signal
        import tempfile
        with tempfile.TemporaryDirectory(prefix="verification-capture-") as temp:
            pid_file = Path(temp) / "sleeper.pid"
            holder = [sys.executable, "-c", "import subprocess, sys\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
                      "open(sys.argv[1], 'w').write(str(child.pid))\n"  # the grandchild inherits our stdout pipe and outlives us
                      "print('parent done', flush=True)", str(pid_file)]
            started = time.monotonic()
            try:
                with self.assertRaises(recovery.ObservationError) as caught:
                    verification._bounded_capture(holder, 10000, timeout=1.5)
                self.assertEqual((caught.exception.reason, caught.exception.detail), ("listing_unavailable", "timeout"))
                self.assertLess(time.monotonic() - started, 10)  # the reader owns the deadline; the inherited pipe cannot hold it
            finally:
                if pid_file.exists():
                    with contextlib_suppress(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
        # A child that floods stdout before it reads stdin cannot deadlock the capture on any pipe size.
        flood_then_read = [sys.executable, "-c", "import sys\nsys.stdout.write('y' * 300000)\nsys.stdout.flush()\n"
                           "data = sys.stdin.buffer.read()\nsys.stdout.write('\\nread=%d' % len(data))\nsys.stdout.flush()"]
        code, data = verification._bounded_capture(flood_then_read, 1_000_000, timeout=20, input_bytes=b"z" * 50000)
        self.assertEqual(code, 0)
        self.assertTrue(data.endswith(b"read=50000"), data[-40:])


class VerificationBoundsLaneTests(VerificationFixture):
    def test_listing_and_copy_bounds_refuse_without_launching_anything(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        runs, calls = self.gate_runs(), len(self.impl_calls())
        with mock.patch.object(verification, "GIT_LISTING_LIMIT", 8):
            refused = self.audit(job_id)
            self.assertFalse(refused["eligible"])
            self.assertIn("candidate_oversized", refused["reasons"], refused["reasons"])
            with self.assertRaisesRegex(room.RoomError, "candidate_oversized"):
                self.retry(job_id, {**refused, "candidate": {"sha256": "0" * 64}, "evidence_digest": "0" * 64, "gates_sha256": "0" * 64}, request_id="listing-limit")
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        if sys.platform == "darwin":
            # The audit scans markers only once an earlier verifier exists; the scan itself refuses beyond the listing cap.
            with mock.patch.object(verification, "PROCESS_LISTING_LIMIT", 8):
                with self.assertRaises(recovery.ObservationError) as caught:
                    verification.marker_processes(["a" * 32])
                self.assertEqual(caught.exception.reason, "inspection_unavailable")
                self.assertIn("over limit", str(caught.exception.detail))
            self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        self.assertEqual((self.gate_runs(), len(self.impl_calls())), (runs, calls))
        # In-process runs with tiny copy bounds: an in-bound ignored creation passes; an ignored creation beyond the bound is
        # interrupted and never projected, even though the ignore rule would have accepted it as candidate content.
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        self.flag.write_text("create-ignored")
        dispatched = self.dispatch_inline(job_id, report, request_id="bounds-in")
        with mock.patch.object(verification, "COPY_FILE_LIMIT", 64), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 64):
            result = self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(result["phase"], "awaiting_astra_review", result.get("verification"))
        self.assertEqual(result["verification"]["result"], "completed")
        self.assertEqual(result["gate_results"][0]["copy_verified"]["created_ignored"], 1)
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        settled = self.retry(job_id, report, request_id="bounds-settle")
        self.assertTrue(settled["reconciled"], settled)
        revised = self.service.room_implementation_revise(self.room_id, self.handoff_id, "Finish the report.")
        self.assertEqual(revised["phase"], "correction_pending")
        self.flag.unlink()
        second = self.finish(self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-2")["id"])
        self.assertEqual(second["status"], "uncertain", second)
        self.flag.write_text("bloat-ignored")
        report = self.audit(second["id"])
        self.assertTrue(report["eligible"], report)
        dispatched = self.dispatch_inline(second["id"], report, request_id="bounds-out")
        runs = self.gate_runs()
        with mock.patch.object(verification, "COPY_FILE_LIMIT", 64), mock.patch.object(verification, "COPY_TOTAL_LIMIT", 64):
            result = self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(result["status"], "blocked", result.get("verification"))
        self.assertEqual(result["verification"]["reason"], "copy_bounds_exceeded")
        self.assertEqual(self.gate_runs(), runs + 1)
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertFalse(state["gates_passed"])
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        again = self.retry(second["id"], report, request_id="bounds-after")
        self.assertFalse(again["reconciled"])
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual(rows[dispatched["verification_id"]]["status"], "interrupted")
        self.assertEqual(rows[dispatched["verification_id"]]["reason"], "copy_bounds_exceeded")
        self.assertEqual(self.finish(again["successor_job_id"])["status"], "succeeded")  # default bounds: the same 100-byte ignored file is in bound
        self.assertEqual(len(self.impl_calls()), 2)


class VerificationRecordChainTests(VerificationFixture):
    def test_reconciliation_after_a_crash_requires_the_registered_record_bytes(self):
        class SimulatedCrash(BaseException):
            pass
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report)
        runs = self.gate_runs()
        with mock.patch.object(verification, "project_completion", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
            self.run_inline(dispatched["successor_job_id"])
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        original_record = (home / "record.json").read_bytes()
        (home / "record.json").write_bytes(original_record + b"\n")  # the immutable record no longer hashes to the registry
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        # The registered outcome is never projected over a changed record, and the vanished verifier is not lineage while its
        # record is tampered: the room stays blocked by that uncertain job instead of dispatching anything.
        with self.assertRaisesRegex(room.RoomError, "registration failed|blocked by uncertain"):
            self.retry(job_id, report, request_id="verify-2")
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual(rows[dispatched["verification_id"]]["status"], "invalidated")
        self.assertTrue(rows[dispatched["verification_id"]]["reason"].startswith("verifier_incomplete"))
        self.assertEqual(self.state()["phase"], "blocked")
        self.assertFalse(self.service.room_status(self.room_id)["ready_for_handoff"])
        self.assertEqual(self.gate_runs(), runs + 1)  # reconciliation itself ran nothing
        (home / "record.json").write_bytes(original_record)  # with the registered bytes back, the earlier verifier is lineage again
        again = self.retry(job_id, report, request_id="verify-3")
        self.assertFalse(again["reconciled"], again)
        self.assertEqual(self.finish(again["successor_job_id"])["status"], "succeeded")  # the fresh retry ran the gate again
        self.assertEqual(self.gate_runs(), runs + 2)
        self.assertEqual(len(self.impl_calls()), 1)


class VerificationConsumeSettlementTests(VerificationFixture):
    def test_unreadable_frozen_digest_after_a_completed_projection_settles_from_the_registry_instead_of_wedging(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        with self.held_popen():
            dispatched = self.retry(job_id, report)  # queued with a held placeholder worker; this process then runs the worker itself
        with mock.patch.object(verification, "durable_outcome_result", return_value=None):  # the worker cannot trust or read the frozen digest
            self.service.worker(dispatched["successor_job_id"])
        outcome = self.service.room_job_status(dispatched["successor_job_id"])
        self.assertEqual(outcome["status"], "succeeded", outcome)
        rows = {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}
        self.assertEqual((rows[dispatched["verification_id"]]["status"], rows[dispatched["verification_id"]]["reason"]), ("consumed", "reconciled"))
        self.assertEqual(self.state()["phase"], "awaiting_astra_review")
        status = self.service.room_status(self.room_id)
        self.assertTrue(status["ready_for_handoff"], status["jobs"])
        self.assertEqual(len(self.impl_calls()), 1)
