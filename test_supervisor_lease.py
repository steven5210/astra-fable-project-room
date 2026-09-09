"""Shared job supervisor lease handoff (#22): a live worker holds its lease from before spawn, status never competes
with a starting worker, legacy launches retry transient contention, and at most one model launch ever happens."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import project_room
import room
from test_project_room import ProjectFixture, ROOT


def delayed_popen(seconds):
    """Wrap the worker launch so the child sleeps before executing project_room.py, keeping its inherited lease."""
    original = subprocess.Popen

    def popen(args, *positional, **keywords):
        if isinstance(args, list) and len(args) > 3 and args[1] == str(ROOT / "project_room.py") and "_worker" in args:
            bootstrap = f"import runpy, sys, time; time.sleep({seconds}); path = sys.argv.pop(1); runpy.run_path(path, run_name='__main__')"
            args = [args[0], "-c", bootstrap, *args[1:]]
        return original(args, *positional, **keywords)
    return popen


class LeaseHandoffTests(ProjectFixture):
    def lease_is_held(self, job_id):
        with (self.service._job_path(job_id) / "worker.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False

    def test_worker_inherits_the_lease_before_it_starts_and_status_never_probes_inside_the_grace(self):
        self.control(wait=True)
        job = self.review(wait=False)
        self.assertTrue(self.lease_is_held(job["id"]), "the lease is held from the instant the job is published")
        with mock.patch.object(project_room.fcntl, "flock", side_effect=AssertionError("status must not touch a lease inside the startup grace")):
            for _ in range(5):
                self.assertIn(self.service.room_job_status(job["id"], 0)["status"], ("queued", "running"))
        self.wait_started()
        self.assertTrue(self.lease_is_held(job["id"]))
        (self.base / "release").touch()
        self.assertEqual(self.service.room_job_status(job["id"], 15)["status"], "succeeded")
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.lease_is_held(job["id"]), "a finished worker releases its lease")

    def test_worker_delayed_beyond_the_old_grace_is_never_marked_disappeared(self):
        with mock.patch.object(project_room, "STARTUP_GRACE_SECONDS", 0.3), mock.patch.object(project_room.subprocess, "Popen", delayed_popen(1.5)):
            job = self.review(wait=False)
            deadline = time.monotonic() + 1.3
            observed = set()
            while time.monotonic() < deadline:
                observed.add(self.service.room_job_status(job["id"], 0.2)["status"])
            self.assertEqual(observed, {"queued"}, "the delayed worker kept the inherited lease; status saw no disappearance")
            terminal = self.service.room_job_status(job["id"], 15)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(len(self.calls()), 1)

    def test_concurrent_status_polls_during_launch_never_block_or_relabel_the_worker(self):
        self.control(wait=True)
        stop, seen = threading.Event(), []

        def poll(job_id):
            while not stop.is_set():
                seen.append(self.service.room_job_status(job_id, 0.2)["status"])
        with mock.patch.object(project_room, "STARTUP_GRACE_SECONDS", 0.0):
            job = self.review(wait=False)
            threads = [threading.Thread(target=poll, args=(job["id"],)) for _ in range(4)]
            for thread in threads:
                thread.start()
            self.wait_started()
            time.sleep(0.5)
            (self.base / "release").touch()
            terminal = self.service.room_job_status(job["id"], 15)
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertNotIn("uncertain", seen)
        self.assertEqual(len(self.calls()), 1)

    def test_transient_contention_does_not_kill_a_legacy_worker_without_an_inherited_lease(self):
        identifier = self.insert_orphan(status="queued", age_seconds=0)
        path = self.service._job_path(identifier)
        holder = (path / "worker.lock").open("a")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release_soon():
            time.sleep(0.05)
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        threading.Thread(target=release_soon).start()
        original = fcntl.flock
        attempts = []

        def counting(handle, operation):
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and getattr(handle, "name", None) == str(path / "worker.lock"):
                attempts.append(1)
            return original(handle, operation)
        with mock.patch.object(project_room.fcntl, "flock", counting):
            self.service.worker(identifier)  # legacy launch: no --lease-fd
        self.assertGreater(len(attempts), 1, "the worker retried the contended lease instead of dying")
        self.assertNotEqual(self.service.room_job_status(identifier)["status"], "queued", "the worker ran once the lease was free")
        self.assertEqual(self.calls(), [])
        other = self.insert_orphan(status="queued", age_seconds=0, request_key="orphan-2")
        raised = [False]

        def once(handle, operation):
            if not raised[0] and operation == fcntl.LOCK_EX | fcntl.LOCK_NB and getattr(handle, "name", None) == str(self.service._job_path(other) / "worker.lock"):
                raised[0] = True
                raise BlockingIOError()
            return original(handle, operation)
        with mock.patch.object(project_room.fcntl, "flock", once):
            self.service.worker(other)
        self.assertTrue(raised[0])
        self.assertNotEqual(self.service.room_job_status(other)["status"], "queued")

    def test_invalid_inherited_descriptors_are_discarded_and_the_job_launches_exactly_once(self):
        original = subprocess.Popen
        bogus = os.open(os.devnull, os.O_RDWR)
        self.addCleanup(os.close, bogus)

        def swap_descriptor(args, *positional, **keywords):
            if isinstance(args, list) and "_worker" in args and "--lease-fd" in args:
                args = list(args)
                args[args.index("--lease-fd") + 1] = str(bogus)
                keywords["pass_fds"] = (bogus,)
            return original(args, *positional, **keywords)
        with mock.patch.object(project_room.subprocess, "Popen", swap_descriptor):
            job = self.review(wait=False)
        self.assertEqual(self.service.room_job_status(job["id"], 15)["status"], "succeeded")
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("inherited lease descriptor was not this job's worker.lock", (self.service._job_path(job["id"]) / "worker.log").read_text())

        def unknown_descriptor(args, *positional, **keywords):
            if isinstance(args, list) and "_worker" in args and "--lease-fd" in args:
                args = list(args)
                args[args.index("--lease-fd") + 1] = "987"
                keywords.pop("pass_fds", None)
            return original(args, *positional, **keywords)
        with mock.patch.object(project_room.subprocess, "Popen", unknown_descriptor):
            second = self.review("review-2", wait=False)
        self.assertEqual(self.service.room_job_status(second["id"], 15)["status"], "succeeded")
        self.assertEqual(len(self.calls()), 2)

    def test_a_rejected_inherited_descriptor_is_never_closed_and_a_lease_failure_submits_nothing(self):
        path = self.service._job_path("0" * 32)
        path.mkdir(parents=True, mode=0o700)
        held = os.open(os.devnull, os.O_RDWR)
        self.addCleanup(os.close, held)
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.service._worker_lease(path, held) as lease:
                self.assertNotEqual(lease, held)
                os.fstat(held)  # the rejected number is still whatever this process had open there
        self.assertIn("was not this job's worker.lock", stderr.getvalue())
        os.fstat(held)
        with self.service.db() as db:
            before = {row[0] for row in db.execute("SELECT id FROM jobs")}
        original = fcntl.flock

        def failing(handle, operation):
            if isinstance(handle, int):  # the job lease is a bare descriptor; room locks are file objects
                raise OSError(38, "synthetic lease failure")
            return original(handle, operation)
        with mock.patch.object(project_room.fcntl, "flock", failing):
            with self.assertRaisesRegex(room.RoomError, "lease could not be acquired; nothing was submitted"):
                self.review("lease-failure", wait=False)
        with self.service.db() as db:
            after = {row[0] for row in db.execute("SELECT id FROM jobs")}
        self.assertEqual(after, before, "no job row is published without its lease")
        self.assertEqual(self.calls(), [])

    def test_worker_crash_after_launch_stays_uncertain_and_is_never_replayed(self):
        self.control(wait=True)
        job = self.review(wait=False)
        self.wait_started()
        worker_pid = self.service._job(job["id"])["pid"]
        os.kill(worker_pid, signal.SIGKILL)
        with mock.patch.object(project_room, "STARTUP_GRACE_SECONDS", 0.5):
            deadline = time.monotonic() + 10
            status = self.service.room_job_status(job["id"], 0)["status"]
            while status in ("queued", "running") and time.monotonic() < deadline:
                time.sleep(0.2)
                status = self.service.room_job_status(job["id"], 0)["status"]
        self.assertEqual(status, "uncertain")
        (self.base / "release").touch()
        with self.assertRaisesRegex(room.RoomError, "uncertain"):
            self.review("after-crash", wait=False)
        self.assertEqual(len(self.calls()), 1)

    def test_parent_spawn_failure_releases_ownership_and_records_the_failure(self):
        with mock.patch.object(project_room.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            job = self.review(wait=False)
        self.assertEqual(job["status"], "failed")
        self.assertIn("did not start", job["error"])
        self.assertFalse(self.lease_is_held(job["id"]), "a failed spawn releases the lease the parent had acquired")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.review("after-failure")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
