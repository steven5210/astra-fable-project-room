"""Matrix tests for the isolated-copy verification retry lane.

In-process engine crash windows (dispatch_inline + run_inline with unittest.mock patches on verification.*), lazy
reconciliation, bounded candidate reads, real-survivor cleanup, multi-cycle corrections and the real MCP round trips.
Every model, gate and repository here is the fixture's fake; no network, no real model, no third-party package.
"""

import concurrent.futures
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock

import project_room
import recovery
import room
import verification
from test_project_room import ROOT
from test_recovery import fixed_inspector
from test_verification import VerificationFixture


class VerificationMatrixTests(VerificationFixture):
    """One focused behavioural test per numbered contract group of the isolated-copy verification lane."""

    class SimulatedCrash(BaseException):
        """Process-death stand-in: a BaseException, so no ordinary engine handler may swallow it."""

    # -- helpers -------------------------------------------------------------------------------------------------

    def verification_rows(self):
        return {row["id"]: row for row in self.service.room_status(self.room_id)["verifications"]}

    def crash_during_run(self, target, request_id):
        """Dispatch with no live worker, crash the engine at `target` of verification, settle the vanished worker."""
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report, request_id=request_id)
        with unittest.mock.patch.object(verification, target, side_effect=self.SimulatedCrash):
            with self.assertRaises(self.SimulatedCrash):
                self.run_inline(dispatched["successor_job_id"])
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        return job_id, report, dispatched

    def kill_detached(self, pid, timeout=10):
        """SIGKILL a detached survivor, then poll (bounded 0.05 s steps) until it is gone or a zombie."""
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            try:
                if Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return
            except (OSError, IndexError):
                pass
            time.sleep(0.05)
        self.fail("detached gate process %d did not exit" % pid)

    # -- concurrency (contract group 8) --------------------------------------------------------------------------

    def concurrent_retry(self, different):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait(timeout=10)
            try:
                return "result", self.retry(job_id, report, request_id=("race-%d" % index) if different else "race-1")
            except room.RoomError as exc:
                return "conflict", str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            outputs = [future.result(timeout=60) for future in [workers.submit(attempt, index) for index in range(2)]]
        successes = [value for kind, value in outputs if kind == "result"]
        conflicts = [value for kind, value in outputs if kind == "conflict"]
        self.assertTrue(successes, outputs)
        self.assertEqual(len({value["verification_id"] for value in successes}), 1, outputs)
        if different:
            self.assertEqual((len(successes), len(conflicts)), (1, 1), outputs)
        else:
            self.assertLessEqual(len(conflicts), 1, outputs)
            for value in conflicts:
                self.assertRegex(value, "Another room mutation is running|verification_already_exists")
        rows = self.verification_rows()
        self.assertEqual(len(rows), 1, rows)
        row = next(iter(rows.values()))
        self.assertEqual(row["successor_job_id"], successes[0]["successor_job_id"])
        verifier = self.finish(row["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertEqual(verifier["result"]["phase"], "awaiting_astra_review", verifier)
        self.assertTrue(verifier["result"]["gates_passed"])
        self.assertEqual(self.gate_runs(), gates_before + 1)
        self.assertEqual(len(self.impl_calls()), calls_before)
        return self.verification_rows()[row["id"]]

    def test_concurrent_identical_retry_requests_dispatch_exactly_one_verifier(self):
        row = self.concurrent_retry(different=False)
        self.assertEqual(row["status"], "consumed", row)

    def test_concurrent_different_retry_requests_dispatch_exactly_one_verifier(self):
        row = self.concurrent_retry(different=True)
        self.assertEqual(row["status"], "consumed", row)

    # -- registration crash window (prepare-then-registry) -------------------------------------------------------

    def test_crash_between_prepare_and_registry_reconciles_registration_incomplete(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        with unittest.mock.patch.object(project_room.Service, "_submit_locked", side_effect=self.SimulatedCrash):
            with self.assertRaises(self.SimulatedCrash):
                self.retry(job_id, report, request_id="verify-registration-1")
        # The record and the projection binding exist, but no registry row was ever inserted.
        self.assertEqual(self.verification_rows(), {})
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        orphan = state["verification"]["verification_id"]
        record = self.handoff_dir / "verifications" / orphan / "record.json"
        self.assertTrue(record.is_file())
        # A new request reconciles the row-less binding as registration_incomplete and dispatches a fresh verifier.
        fresh = self.retry(job_id, report, request_id="verify-registration-2")
        self.assertEqual(fresh["status"], "dispatched", fresh)
        self.assertNotEqual(fresh["verification_id"], orphan)
        state = self.state()
        self.assertEqual(state["verification"]["verification_id"], fresh["verification_id"])
        invalidated = [entry for entry in state["verification_history"]
                       if entry.get("verification_id") == orphan and entry.get("status") == "invalidated"]
        self.assertEqual(len(invalidated), 1, state["verification_history"])
        self.assertEqual(invalidated[0]["reason"], "registration_incomplete")
        verifier = self.finish(fresh["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertEqual(verifier["result"]["phase"], "awaiting_astra_review", verifier)
        self.assertTrue(verifier["result"]["gates_passed"])
        rows = self.verification_rows()
        self.assertEqual([row["id"] for row in rows.values()], [fresh["verification_id"]])
        self.assertEqual(rows[fresh["verification_id"]]["status"], "consumed")
        self.assertEqual(self.gate_runs(), gates_before + 1)
        self.assertEqual(len(self.impl_calls()), calls_before)
        self.assertTrue(record.is_file())  # the orphan record stays as immutable evidence

    # -- completed-outcome crash windows (contract group 1) ------------------------------------------------------

    def test_crash_after_registered_outcome_before_projection_reconciles_consumed(self):
        job_id, report, dispatched = self.crash_during_run("project_completion", "verify-outcome-1")
        vid = dispatched["verification_id"]
        home = self.handoff_dir / "verifications" / vid
        self.assertTrue((home / "outcome.json").is_file())
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "dispatched")
        self.assertEqual(rows[vid]["outcome_sha256"], room.sha((home / "outcome.json").read_bytes()))
        state = self.state()
        self.assertEqual(state["verification"]["verification_id"], vid)
        # The registered digest, the outcome bytes and the projection re-verify: the row is consumed with no gate run.
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        reconciled = self.retry(job_id, report, request_id="verify-outcome-2")
        self.assertTrue(reconciled["reconciled"], reconciled)
        self.assertEqual(reconciled["status"], "consumed", reconciled)
        self.assertEqual(reconciled["verification_id"], vid)
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        state = self.state()
        self.assertEqual(state["phase"], "awaiting_astra_review")
        self.assertTrue(state["gates_passed"])
        self.assertIsNone(state["error"])
        self.assertIsNone(state["verification"])
        self.assertEqual(state["gate_results"][0]["verification_id"], vid)
        self.assertEqual([entry["status"] for entry in state["verification_history"] if entry["verification_id"] == vid],
                         ["launched", "consumed"])
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "consumed")
        self.assertEqual(rows[vid]["reason"], "reconciled")
        self.assertEqual(json.loads((home / "outcome.json").read_text())["result"], "completed")

    def test_crash_after_registration_before_outcome_file_invalidates_and_dispatches_fresh(self):
        job_id, report, dispatched = self.crash_during_run("_write_outcome", "verify-missing-1")
        vid = dispatched["verification_id"]
        home = self.handoff_dir / "verifications" / vid
        self.assertFalse((home / "outcome.json").exists())  # the digest is registered but the durable file is not
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "dispatched")
        self.assertTrue(rows[vid]["outcome_sha256"])
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        fresh = self.dispatch_inline(job_id, report, request_id="verify-missing-2")
        self.assertNotEqual(fresh["verification_id"], vid)
        self.assertEqual(self.gate_runs(), gates_before)  # the reconciliation itself launched no gate
        self.assertEqual(len(self.impl_calls()), calls_before)
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "invalidated")
        self.assertTrue(rows[vid]["reason"].startswith("verifier_incomplete"), rows[vid]["reason"])
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertEqual(state["verification"]["verification_id"], fresh["verification_id"])
        result = self.run_inline(fresh["successor_job_id"])
        self.mark_job(fresh["successor_job_id"], "uncertain")
        self.assertEqual(result["phase"], "awaiting_astra_review", result)
        self.assertTrue(result["gates_passed"])
        self.assertEqual(self.gate_runs(), gates_before + 1)

    def test_crash_after_projection_before_worker_update_reconciles_consumed(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report, request_id="verify-projected-1")
        vid = dispatched["verification_id"]
        result = self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(result["phase"], "awaiting_astra_review", result)
        self.assertTrue(result["gates_passed"])
        self.mark_job(dispatched["successor_job_id"], "uncertain")  # the worker died before its terminal update
        state = self.state()
        self.assertEqual(state["phase"], "awaiting_astra_review")
        self.assertIsNone(state["verification"])
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "dispatched")
        self.assertTrue(rows[vid]["outcome_sha256"])
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        reconciled = self.retry(job_id, report, request_id="verify-projected-2")
        self.assertTrue(reconciled["reconciled"], reconciled)
        self.assertEqual(reconciled["status"], "consumed", reconciled)
        self.assertEqual(reconciled["verification_id"], vid)
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "consumed")
        self.assertEqual(rows[vid]["reason"], "reconciled")
        status = self.service.room_status(self.room_id)
        self.assertTrue(status["ready_for_handoff"], status)
        old = next(job for job in status["jobs"] if job["id"] == job_id)
        self.assertEqual(old["status"], "uncertain")
        self.assertEqual(old["superseded_by"], dispatched["successor_job_id"])
        successor = next(job for job in status["jobs"] if job["id"] == dispatched["successor_job_id"])
        self.assertEqual(successor["status"], "uncertain")

    # -- post-dispatch binding tampering (contract group 6) ------------------------------------------------------

    def test_post_dispatch_binding_tampering_refuses_before_launch(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report, request_id="verify-binding-1")
        vid = dispatched["verification_id"]
        state = self.state()
        self.assertEqual(state["verification"]["budget_seconds"], 5)
        state["verification"]["budget_seconds"] = 6
        state["verification"]["candidate"] = {**state["verification"]["candidate"], "sha256": "0" * 64}
        project_room.atomic_json(self.handoff_dir / "state.json", state)
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        result = self.run_inline(dispatched["successor_job_id"])
        self.assertEqual(result["phase"], "refused_before_launch", result)
        self.assertIn("evidence_changed", result["reason"])
        self.assertFalse(result["gates_launched"])
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        self.assertEqual(self.state()["phase"], "blocked")
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        # The refusal settles the row either in the inline engine or in the next reconciliation; a deliberately
        # mismatched supplied digest keeps that request from being followed by a fresh dispatch, so the settled
        # projection is observable either way.
        with self.assertRaisesRegex(room.RoomError, "does not match the fresh audit|candidate_sha256"):
            self.retry(job_id, report, request_id="verify-binding-2", candidate_sha256="0" * 64)
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "invalidated")
        self.assertTrue(rows[vid]["reason"] == "evidence_changed" or rows[vid]["reason"].startswith("verifier_incomplete"),
                        rows[vid]["reason"])
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertIsNone(state["verification"])
        fresh = self.audit(job_id)  # a fresh audit is eligible again
        self.assertTrue(fresh["eligible"], fresh)
        again = self.retry(job_id, fresh, request_id="verify-binding-3")
        self.assertEqual(again["status"], "dispatched", again)
        verifier = self.finish(again["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertTrue(verifier["result"]["gates_passed"])
        self.assertEqual(self.gate_runs(), gates_before + 1)
        self.assertEqual(len(self.impl_calls()), calls_before)

    # -- fabricated registration chain and planted outcome (contract group 7) ------------------------------------

    def test_fabricated_registration_chain_does_not_unblock_the_room(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        with self.service.db() as db:
            review = db.execute("SELECT id FROM jobs WHERE room_id=? AND kind='review' AND status='succeeded' ORDER BY created_at",
                                (self.room_id,)).fetchone()
        self.assertIsNotNone(review, "the fixture's succeeded review job is the fabricated successor")
        fake = "c" * 32
        home = self.handoff_dir / "verifications" / fake
        home.mkdir(parents=True)
        (home / "record.json").write_text(json.dumps({"verification_id": fake, "room_id": self.room_id, "handoff_id": self.handoff_id,
                                                     "predecessor_job_id": job_id, "successor_job_id": review["id"], "attempt": 1,
                                                     "boundary": "isolated_copy"}))
        (home / "outcome.json").write_text(json.dumps({"verification_id": fake, "room_id": self.room_id, "handoff_id": self.handoff_id,
                                                       "predecessor_job_id": job_id, "successor_job_id": review["id"], "attempt": 1,
                                                       "result": "completed", "gates_passed": True}))
        with self.service.db() as db:
            db.execute("INSERT INTO implementation_verifications(id,room_id,handoff_id,predecessor_job_id,attempt,request_key,payload,status,"
                       "created_at,successor_job_id,record_sha256,outcome_sha256) VALUES(?,?,?,?,1,'planted-chain',?,'consumed',?,?,?,?)",
                       (fake, self.room_id, self.handoff_id, job_id, room.canonical({"planted": True}), room.now(), review["id"],
                        room.sha((home / "record.json").read_bytes()), room.sha((home / "outcome.json").read_bytes())))
        status = self.service.room_status(self.room_id)
        self.assertIn(fake, [row["id"] for row in status["verifications"]])
        old = next(job for job in status["jobs"] if job["id"] == job_id)
        self.assertIsNone(old["superseded_by"])
        self.assertFalse(status["ready_for_handoff"])
        with self.assertRaisesRegex(room.RoomError, "blocked by uncertain"):
            self.service.room_implementation_submit(self.room_id, self.handoff_id, "ordinary-after-fabrication")
        self.assertEqual(len(self.impl_calls()), 1)
        self.assertEqual(self.state()["phase"], "blocked")

    def test_planted_outcome_without_registration_is_invalidated_not_projected(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report, request_id="verify-planted-1")
        vid = dispatched["verification_id"]
        home = self.handoff_dir / "verifications" / vid
        (home / "outcome.json").write_text(json.dumps({"verification_id": vid, "result": "completed", "gates_passed": True}))
        self.mark_job(dispatched["successor_job_id"], "uncertain")
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        fresh = self.dispatch_inline(job_id, report, request_id="verify-planted-2")
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "invalidated")
        self.assertEqual(rows[vid]["reason"], "verifier_incomplete:unregistered_outcome")
        self.assertNotEqual(fresh["verification_id"], vid)
        self.assertEqual(self.gate_runs(), gates_before)  # the unregistered outcome was never adopted or run
        self.assertEqual(len(self.impl_calls()), calls_before)
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        result = self.run_inline(fresh["successor_job_id"])
        self.mark_job(fresh["successor_job_id"], "uncertain")
        self.assertEqual(result["phase"], "awaiting_astra_review", result)
        self.assertTrue(result["gates_passed"])
        self.assertEqual(self.gate_runs(), gates_before + 1)

    # -- bounded candidate reads (contract group 4) ---------------------------------------------------------------

    def test_bounded_candidate_reads_refuse_oversized_and_changing_files(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        gates_before = self.gate_runs()
        cases = [("COPY_FILE_LIMIT", 10, "candidate_oversized"),
                 ("COPY_TOTAL_LIMIT", 20, "candidate_oversized"),
                 ("COPY_ENTRY_LIMIT", 1, "candidate_oversized"),
                 ("PATH_DEPTH_LIMIT", 0, "candidate_path_unsupported")]
        for name, value, expected in cases:
            with self.subTest(limit=name):
                with unittest.mock.patch.object(verification, name, value):
                    report = self.audit(job_id)
                    self.assertFalse(report["eligible"], report)
                    self.assertIn(expected, report["reasons"], report["reasons"])
                    with self.assertRaisesRegex(room.RoomError, expected):
                        self.retry(job_id, report, request_id="refused-" + name)
        # A file that grows while it is fingerprinted is a changed candidate, never a new snapshot.
        original_open = recovery._open_component
        fired = []
        def seam(component, flags, dir_fd):
            if component == "feature.txt" and not fired:
                fired.append(True)
                (self.worktree / "feature.txt").write_bytes(b"implemented\nplus growth\n")
            return original_open(component, flags, dir_fd)
        try:
            with unittest.mock.patch.object(recovery, "_open_component", new=seam):
                report = self.audit(job_id)
        finally:
            (self.worktree / "feature.txt").write_bytes(b"implemented\n")
        self.assertTrue(fired)
        self.assertFalse(report["eligible"], report)
        self.assertIn("candidate_changed", report["reasons"], report["reasons"])
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), 1)
        self.assertTrue(self.audit(job_id)["eligible"], "the restored bytes must stay auditable")

    # -- full argv binding (contract group 5) ---------------------------------------------------------------------

    def test_full_argv_binding_refuses_any_changed_element(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        argv_path = self.attempt_dir() / "argv.json"
        original = argv_path.read_bytes()
        saved = json.loads(original)
        self.assertTrue(self.audit(job_id)["eligible"])
        gates_before = self.gate_runs()
        variants = [("appended argument", saved + ["--verbose"]),
                    ("replaced element", saved[:-1] + ["--verbose"]),
                    ("removed element", saved[:-1])]
        for label, value in variants:
            with self.subTest(variant=label):
                argv_path.write_text(json.dumps(value))
                try:
                    report = self.audit(job_id)
                    self.assertFalse(report["eligible"], report)
                    self.assertIn("argv_mismatch", report["reasons"], report["reasons"])
                    with self.assertRaisesRegex(room.RoomError, "argv_mismatch"):
                        self.retry(job_id, report, request_id="refused-" + label)
                finally:
                    argv_path.write_bytes(original)
        self.assertTrue(self.audit(job_id)["eligible"], "the original argv must stay auditable")
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), 1)

    # -- survivor and incomplete-inspection cleanup (contract group 3) -------------------------------------------

    def test_surviving_gate_process_defers_cleanup_until_killed(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("survivor")
        self.service.process_inspector = None  # the marker scan is a real platform observation here
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        report = self.audit(job_id)
        self.assertTrue(report["eligible"], report)
        dispatched = self.retry(job_id, report, request_id="verify-survivor-1")
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        survivor = None
        try:
            verifier = self.finish(dispatched["successor_job_id"])
            self.assertEqual(verifier["status"], "uncertain", verifier)
            self.assertEqual(verifier["result"]["phase"], "blocked", verifier["result"])
            self.assertEqual(verifier["result"]["verification"]["result"], "interrupted")
            self.assertEqual(verifier["result"]["verification"]["reason"], "survivors_observed")
            stdout = (home / "gate-1" / "stdout.txt").read_text()
            lines = [line for line in stdout.splitlines() if line.startswith("SLEEPER=")]
            self.assertTrue(lines, stdout)
            survivor = int(lines[0].split("=", 1)[1])
            self.assertEqual(self.gate_runs(), gates_before + 1)
            self.assertEqual(len(self.impl_calls()), calls_before)
            self.assertTrue((home / "candidate").exists())  # deferred cleanup keeps the private copy
            rows = self.verification_rows()
            self.assertEqual(rows[dispatched["verification_id"]]["status"], "interrupted")
            self.assertEqual(rows[dispatched["verification_id"]]["reason"], "survivors_observed")
            state = self.state()
            self.assertEqual(state["phase"], "blocked")
            self.assertTrue(state["error"].startswith("TimeoutExpired"))
            self.assertIsNone(state["verification"])
            blocked = self.audit(job_id)
            self.assertFalse(blocked["eligible"], blocked)
            self.assertIn("gate_process_present", blocked["reasons"])
        finally:
            self.kill_detached(survivor)
        self.flag.write_text("pass")  # the retried gate must not spawn another survivor
        fresh = self.audit(job_id)
        self.assertTrue(fresh["eligible"], fresh)
        second = self.retry(job_id, fresh, request_id="verify-survivor-2")
        self.assertEqual(second["status"], "dispatched", second)
        again = self.finish(second["successor_job_id"])
        self.assertEqual(again["status"], "succeeded", again)
        self.assertEqual(again["result"]["phase"], "awaiting_astra_review", again)
        self.assertTrue(again["result"]["gates_passed"])
        self.assertEqual(self.gate_runs(), gates_before + 2)
        self.assertEqual(len(self.impl_calls()), calls_before)
        self.assertTrue(self.service.room_status(self.room_id)["ready_for_handoff"])

    def test_incomplete_process_inspection_interrupts_instead_of_projecting(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.audit(job_id)
        dispatched = self.dispatch_inline(job_id, report, request_id="verify-incomplete-1")

        calls = []

        def inspector():
            # The pre-launch recheck sees a complete inspection; the cleanup observation after the gates does not.
            calls.append(True)
            incomplete = [4242] if len(calls) > 1 else []
            return {"boot_time": int(time.time()) - 5, "boot_source": "fixture", "method": "fixture",
                    "processes": [], "skipped": 0, "incomplete": incomplete, "disappeared": 0}

        self.service.process_inspector = inspector
        try:
            result = self.run_inline(dispatched["successor_job_id"])
        finally:
            self.mark_job(dispatched["successor_job_id"], "uncertain")
            self.service.process_inspector = fixed_inspector(boot=int(time.time()) - 5)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["verification"]["result"], "interrupted")
        self.assertEqual(result["verification"]["reason"], "inspection_incomplete")
        self.assertIn("inspection_incomplete", str(result.get("error")))
        home = self.handoff_dir / "verifications" / dispatched["verification_id"]
        outcome = json.loads((home / "outcome.json").read_text())
        self.assertEqual(outcome["result"], "interrupted")
        self.assertEqual(outcome["reason"], "inspection_incomplete")
        self.assertEqual(outcome["cleanup"]["status"], "deferred")
        self.assertTrue((home / "candidate").exists())
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertIsNone(state["verification"])

    # -- two further correction/timeout/retry cycles (contract group 9) ------------------------------------------

    def assert_cycle(self, index, candidates, sizes, attempt1):
        """Post-cycle invariants: ready, every uncertain job exempt, one consumed row per cycle, attempt 1 frozen,
        the transcript only grew and every candidate digest is new."""
        status = self.service.room_status(self.room_id)
        self.assertTrue(status["ready_for_handoff"], status)
        for job in status["jobs"]:
            if job["status"] == "uncertain":
                self.assertIsNotNone(job["superseded_by"], job)
        consumed = [row for row in status["verifications"] if row["status"] == "consumed"]
        self.assertEqual(len(consumed), index)
        self.assertEqual(self.tree_hashes(self.handoff_dir / "attempts" / "0001"), attempt1)
        if index > 1:  # a verification retry runs no model: bytes change only after a correction attempt
            candidates.append(self.state()["candidate"]["sha256"])
            sizes.append(self.live_transcript().stat().st_size)
        self.assertEqual(len(set(candidates)), len(candidates), candidates)
        self.assertTrue(all(older < newer for older, newer in zip(sizes, sizes[1:])), sizes)

    def test_two_further_correction_timeout_retry_cycles_and_a_completed_fourth_attempt(self):
        first = self.gate_timeout()
        attempt1 = self.tree_hashes(self.handoff_dir / "attempts" / "0001")
        candidates = [self.state()["candidate"]["sha256"]]
        sizes = [self.live_transcript().stat().st_size]
        self.flag.write_text("pass")
        report = self.audit(first["id"])
        self.assertEqual(report["generation"]["attempt_count"], 1)
        verifier = self.finish(self.retry(first["id"], report, request_id="cycles-verify-1")["successor_job_id"])
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertTrue(verifier["result"]["gates_passed"])
        self.assert_cycle(1, candidates, sizes, attempt1)
        for index in (2, 3):
            self.service.room_implementation_revise(self.room_id, self.handoff_id, "Correction %d" % index)
            self.flag.unlink()  # the correction attempt's gate sleeps again and hits the pinned budget
            terminal = self.gate_timeout("implement-%d" % index)
            self.assertEqual(self.state()["attempt_count"], index)
            report = self.audit(terminal["id"])
            self.assertTrue(report["eligible"], report)
            self.assertEqual(report["generation"]["attempt_count"], index)
            self.flag.write_text("pass")
            verifier = self.finish(self.retry(terminal["id"], report, request_id="cycles-verify-%d" % index)["successor_job_id"])
            self.assertEqual(verifier["status"], "succeeded", verifier)
            self.assertTrue(verifier["result"]["gates_passed"])
            self.assert_cycle(index, candidates, sizes, attempt1)
        self.service.room_implementation_revise(self.room_id, self.handoff_id, "Correction 4")
        self.mode.write_text("normal")
        self.flag.write_text("pass")
        final = self.finish(self.service.room_implementation_submit(self.room_id, self.handoff_id, "implement-4")["id"])
        self.assertEqual(final["status"], "succeeded", final)
        self.assertEqual(final["result"]["attempt_count"], 4)
        self.assertTrue(final["result"]["gates_passed"])
        accepted = self.service.room_implementation_review(self.room_id, self.handoff_id, True,
                                                           "Inspected the finished feature and fresh gate evidence.")
        self.assertEqual(accepted["phase"], "accepted")
        self.assertEqual(self.tree_hashes(self.handoff_dir / "attempts" / "0001"), attempt1)
        self.assertEqual(len(self.impl_calls()), 4)

    # -- output integrity after a durable outcome (contract group 11) --------------------------------------------

    def test_tampered_gate_log_after_durable_outcome_is_invalidated(self):
        job_id, report, dispatched = self.crash_during_run("project_completion", "verify-integrity-1")
        vid = dispatched["verification_id"]
        stdout = self.handoff_dir / "verifications" / vid / "gate-1" / "stdout.txt"
        stdout.write_bytes(stdout.read_bytes() + b"tampered\n")
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        fresh = self.dispatch_inline(job_id, report, request_id="verify-integrity-2")
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        self.assertNotEqual(fresh["verification_id"], vid)
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "invalidated")
        self.assertTrue(rows[vid]["reason"].startswith("verifier_incomplete"), rows[vid]["reason"])
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.mark_job(fresh["successor_job_id"], "uncertain")

    def test_changed_worktree_after_durable_outcome_is_invalidated(self):
        job_id, report, dispatched = self.crash_during_run("project_completion", "verify-worktree-1")
        vid = dispatched["verification_id"]
        extra = self.worktree / "extra.txt"
        extra.write_text("changed after the durable outcome\n")
        gates_before = self.gate_runs()
        calls_before = len(self.impl_calls())
        with self.assertRaisesRegex(room.RoomError, "candidate_changed"):
            self.retry(job_id, report, request_id="verify-worktree-2")
        self.assertEqual(self.gate_runs(), gates_before)
        self.assertEqual(len(self.impl_calls()), calls_before)
        rows = self.verification_rows()
        self.assertEqual(rows[vid]["status"], "invalidated")
        self.assertTrue(rows[vid]["reason"].startswith("verifier_incomplete"), rows[vid]["reason"])
        self.assertIn("candidate_changed", rows[vid]["reason"])
        state = self.state()
        self.assertEqual(state["phase"], "blocked")
        self.assertTrue(state["error"].startswith("TimeoutExpired"))
        self.assertIsNone(state["verification"])
        extra.unlink()
        self.assertTrue(self.audit(job_id)["eligible"], "the restored worktree must stay auditable")


class VerificationMcpTests(VerificationFixture):
    """The real MCP stdio round trips over the verification audit and retry tools."""

    def setUp(self):
        super().setUp()
        self.server = self.start_server()

    def tearDown(self):
        if self.server.poll() is None:
            self.server.stdin.close()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.terminate()
                self.server.wait(timeout=5)
        self.server.stdout.close()
        self.server.stderr.close()
        super().tearDown()

    def start_server(self):
        return subprocess.Popen([sys.executable, str(ROOT / "project_room_mcp.py")], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "PROJECT_ROOM_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})

    def read_response(self, timeout=30):
        with selectors.DefaultSelector() as selector:
            selector.register(self.server.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout), "MCP response timed out")
        line = self.server.stdout.readline()
        self.assertTrue(line, "MCP exited without a JSON-RPC response")
        return json.loads(line)

    def request(self, method, params=None, identifier=1, timeout=30):
        envelope = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            envelope["params"] = params
        self.server.stdin.write(json.dumps(envelope).encode() + b"\n")
        self.server.stdin.flush()
        return self.read_response(timeout)

    def tool(self, name, arguments=None, timeout=30):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        self.assertNotIn("error", response, response)
        self.assertFalse(response["result"]["isError"], response)
        content = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(response["result"]["structuredContent"], content)
        return content

    def audit_arguments(self, report, job_id):
        return {"room_id": self.room_id, "handoff_id": self.handoff_id, "job_id": job_id,
                "spec_revision": report["identity"]["spec_revision"], "spec_sha256": report["identity"]["spec_sha256"],
                "candidate_sha256": report["candidate"]["sha256"], "evidence_digest": report["evidence_digest"],
                "gates_sha256": report["gates_sha256"]}

    def test_verification_tools_are_discoverable_and_the_audit_round_trips(self):
        listing = {tool["name"]: tool for tool in self.request("tools/list")["result"]["tools"]}
        self.assertIn("room_verification_audit", listing)
        self.assertIn("room_verification_retry", listing)
        self.assertTrue(listing["room_verification_audit"]["annotations"]["readOnlyHint"])
        terminal = self.gate_timeout()
        report = self.tool("room_verification_audit", {"room_id": self.room_id, "handoff_id": self.handoff_id,
                                                       "job_id": terminal["id"]})
        self.assertTrue(report["eligible"], report)
        self.assertEqual(report["boundary"], "isolated_copy")
        self.assertEqual(self.service.room_status(self.room_id)["verifications"], [])
        self.assertEqual(len(self.impl_calls()), 1)

    def test_verification_retry_round_trip_and_boolean_budget_refusal(self):
        terminal = self.gate_timeout()
        job_id = terminal["id"]
        self.flag.write_text("pass")
        report = self.tool("room_verification_audit", {"room_id": self.room_id, "handoff_id": self.handoff_id, "job_id": job_id})
        self.assertTrue(report["eligible"], report)
        dispatched = self.tool("room_verification_retry", {**self.audit_arguments(report, job_id), "gate_timeout_seconds": 5,
                                                           "diagnosis": "Gate 1 exceeded the pinned budget after the model completed normally.",
                                                           "authorization": self.authorization, "request_id": "mcp-verify-1"})
        self.assertEqual(dispatched["status"], "dispatched", dispatched)
        verifier = self.tool("room_job_status", {"job_id": dispatched["successor_job_id"], "wait_seconds": 40}, timeout=45)
        self.assertEqual(verifier["status"], "succeeded", verifier)
        self.assertEqual(verifier["result"]["phase"], "awaiting_astra_review")
        self.assertTrue(verifier["result"]["gates_passed"])
        response = self.request("tools/call", {"name": "room_verification_retry", "arguments": {
            **self.audit_arguments(report, job_id), "gate_timeout_seconds": True,
            "diagnosis": "A boolean budget is not an integer budget.",
            "authorization": self.authorization, "request_id": "mcp-verify-bad"}})
        self.assertTrue(response["result"]["isError"], response)
        text = response["result"]["content"][0]["text"]
        self.assertIn("gate_timeout_seconds", text)
        self.assertNotIn("TimeoutExpired", text)
        self.assertNotIn(str(self.flag), text)
        self.assertEqual(len(self.impl_calls()), 1)
        self.assertEqual(self.service.room_status(self.room_id)["verifications"][0]["status"], "consumed")


if __name__ == "__main__":
    unittest.main()
