import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
from test_ao_normal import Fixture


class RoomLockTests(Fixture):
    """The shared adapter lock plus per-room locks: concurrency, exclusion, diagnosis."""

    def setUp(self):
        super().setUp()
        self.room_a = self.open('locks-a')
        self.room_b = self.open('locks-b')
        self.ao_root = self.service.root

    def _start(self, room_id=None, exclusive=False, hold_seconds=None):
        """Start service.locked() in a thread; returns (entered, release, done, errors)."""
        entered = threading.Event()
        release = threading.Event()
        done = threading.Event()
        errors = []

        def run():
            try:
                with self.service.locked(room_id, exclusive=exclusive):
                    entered.set()
                    if hold_seconds is not None:
                        time.sleep(hold_seconds)
                    else:
                        release.wait(20)
            except Exception as exc:  # noqa: BLE001 - surfaced to the test body
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: release.set() or done.wait(20))
        return entered, release, done, errors

    def _hold(self, room_id=None, exclusive=False, hold_seconds=None):
        """Like _start but asserts the thread actually acquired its locks."""
        entered, release, done, errors = self._start(room_id, exclusive=exclusive, hold_seconds=hold_seconds)
        self.assertTrue(entered.wait(20), "holder thread did not acquire its locks")
        return release, done, errors

    def _hold_raw(self, path, flags=fcntl.LOCK_EX):
        """Hold a raw flock on path in a thread, without the service machinery."""
        entered = threading.Event()
        release = threading.Event()
        done = threading.Event()

        def run():
            with open(path, "a+") as stream:
                fcntl.flock(stream, flags)
                entered.set()
                release.wait(20)
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(20), "raw holder did not acquire")
        self.addCleanup(lambda: release.set() or done.wait(20))
        return release, done

    def _blocked(self, room_id, timeout_seconds=0.5):
        """Assert a bounded locked() raises the timeout RoomError; returns its message."""
        with self.assertRaises(ao.RoomError) as caught:
            with self.service.locked(room_id, timeout_seconds=timeout_seconds):
                pass
        self.assertIn("not acquired within", str(caught.exception))
        return str(caught.exception)

    def test_independent_rooms_progress_concurrently(self):
        release, done, errors = self._hold(self.room_a, hold_seconds=2.0)
        started = time.monotonic()
        with self.service.locked(self.room_b):
            pass
        self.service.ao_room_sync(self.room_b)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "room b work waited on room a's lock")
        release.set()
        self.assertTrue(done.wait(20))
        self.assertFalse(errors)

    def test_same_room_serializes_and_diagnoses_holder(self):
        release, done, errors = self._hold(self.room_a)
        message = self._blocked(self.room_a)
        self.assertIn(str(os.getpid()), message)
        self.assertIn("shared", message)
        self.assertIn(self.room_a, message)
        acquired = threading.Event()

        def wait_for_room():
            with self.service.locked(self.room_a):
                acquired.set()

        waiter = threading.Thread(target=wait_for_room, daemon=True)
        waiter.start()
        time.sleep(0.2)
        self.assertFalse(acquired.is_set(), "blocking room lock did not wait for the holder")
        release.set()
        self.assertTrue(acquired.wait(20))
        self.assertTrue(done.wait(20))
        self.assertFalse(errors)
        waiter.join(20)

    def test_room_lock_waiter_is_named_distinctly_from_the_holder(self):
        release, done, errors = self._hold(self.room_a)
        entered, release2, done2, errors2 = self._start(self.room_a)
        # The second thread holds the adapter lock shared and blocks on room_a's lock.
        deadline = time.monotonic() + 10
        holders = []
        while time.monotonic() < deadline:
            holders = [record for record in ao.lock_holders(self.ao_root) if record.get("verified")]
            if len(holders) == 2 and any(record.get("holds") == ["adapter"] for record in holders):
                break
            time.sleep(0.05)
        self.assertEqual(sorted(tuple(record["holds"]) for record in holders),
                         [("adapter",), ("adapter", "room")])
        message = self._blocked(self.room_a)
        self.assertIn("holds room " + self.room_a, message)
        self.assertIn("waiting for room " + self.room_a, message)
        self.assertIn(str(os.getpid()), message)
        release.set()
        self.assertTrue(entered.wait(20), "blocked same-room acquisition did not proceed on release")
        release2.set()
        self.assertTrue(done.wait(20))
        self.assertTrue(done2.wait(20))
        self.assertFalse(errors)
        self.assertFalse(errors2)

    def test_exclusive_and_shared_modes_exclude_each_other(self):
        release, done, errors = self._hold(self.room_a)
        message = self._blocked(None)
        self.assertIn(str(os.getpid()), message)
        release.set()
        self.assertTrue(done.wait(20))
        self.assertFalse(errors)
        release2, done2, errors2 = self._hold(None, exclusive=True)
        message = self._blocked(self.room_b)
        self.assertIn(str(os.getpid()), message)
        self.assertIn("exclusive", message)
        release2.set()
        self.assertTrue(done2.wait(20))
        self.assertFalse(errors2)

    def test_old_style_exclusive_lock_still_excludes_new_shared_holders(self):
        release, done = self._hold_raw(self.ao_root / ".lock")
        message = self._blocked(self.room_a)
        self.assertIn("no verified holder record", message)
        release.set()
        self.assertTrue(done.wait(20))
        release2, done2, errors = self._hold(self.room_a)
        with open(self.ao_root / ".lock", "a+") as stream:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release2.set()
        self.assertTrue(done2.wait(20))
        self.assertFalse(errors)

    def test_duplicate_session_claim_is_serialized_to_exactly_one_winner(self):
        outcomes = {}

        def bind(room_id):
            try:
                # The reviewer role needs no preparation or workspace check, so the
                # cross-room claims scan is the only thing that can refuse here.
                outcomes[room_id] = self.service.ao_room_bind(
                    room_id, 'reviewer', 'reviewer', 'astra', 'max')
            except ao.RoomError as exc:
                outcomes[room_id] = exc

        threads = [threading.Thread(target=bind, args=(room,)) for room in (self.room_a, self.room_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        wins = [room for room, outcome in outcomes.items() if not isinstance(outcome, ao.RoomError)]
        losses = [outcome for outcome in outcomes.values() if isinstance(outcome, ao.RoomError)]
        self.assertEqual(len(wins), 1, outcomes)
        self.assertEqual(len(losses), 1, outcomes)
        self.assertIn("already bound or retired", str(losses[0]))

    def test_duplicate_worktree_preparation_is_serialized_to_exactly_one_winner(self):
        outcomes = {}

        def prepare(room_id):
            try:
                outcomes[room_id] = self.service.ao_room_prepare(room_id, str(self.repo))
            except ao.RoomError as exc:
                outcomes[room_id] = exc

        threads = [threading.Thread(target=prepare, args=(room,)) for room in (self.room_a, self.room_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        wins = [room for room, outcome in outcomes.items() if not isinstance(outcome, ao.RoomError)]
        losses = [outcome for outcome in outcomes.values() if isinstance(outcome, ao.RoomError)]
        self.assertEqual(len(wins), 1, outcomes)
        self.assertEqual(len(losses), 1, outcomes)
        self.assertIn("already prepared for another room", str(losses[0]))

    def test_turnstile_blocks_new_adapter_acquisition_until_released(self):
        release, done = self._hold_raw(self.ao_root / ".lock.turnstile")
        self._blocked(self.room_a)
        release.set()
        self.assertTrue(done.wait(20))
        with self.service.locked(self.room_a):
            pass

    def test_holder_records_tracked_verified_and_removed(self):
        timing = {}
        holders_dir = self.ao_root / "locks" / "holders"
        with self.service.locked(self.room_a, timing=timing):
            files = list(holders_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text())
            self.assertEqual(record["pid"], os.getpid())
            self.assertEqual(record["mode"], "shared")
            self.assertEqual(record["room_id"], self.room_a)
            self.assertTrue(record["started_at"] > 0)
            holders = ao.lock_holders(self.ao_root)
            self.assertEqual(len(holders), 1)
            self.assertTrue(holders[0]["verified"])
            self.assertIn("lock_wait_seconds", timing)
        self.assertEqual(list(holders_dir.glob("*.json")), [])
        stale = holders_dir / "99999999-deadbeef.json"
        stale.write_text(json.dumps({"pid": 99999999, "mode": "shared", "room_id": self.room_a,
                                     "started_at": time.time(), "argv": ["stale.py"]}))
        holders = ao.lock_holders(self.ao_root)
        self.assertEqual(len(holders), 1)
        self.assertFalse(holders[0]["verified"])
        self.assertEqual(holders[0]["pid"], 99999999)
        self.assertFalse(stale.exists())

    def test_holder_record_argv_never_carries_paths_or_payloads(self):
        argv = ["/x/project_room.py", "--home", "/private/home", "call", "ao_room_sync",
                "--args", '{"room_id":"r"}']
        holders_dir = self.ao_root / "locks" / "holders"
        with patch.object(sys, "argv", argv):
            with self.service.locked(self.room_a):
                files = list(holders_dir.glob("*.json"))
                self.assertEqual(len(files), 1)
                record = json.loads(files[0].read_text())
        self.assertEqual(record["argv"], ["project_room.py", "call", "ao_room_sync"])

    def test_prepare_helper_bounded_wait_reports_holder(self):
        release, done, errors = self._hold(self.room_b)
        try:
            with patch.object(ao_delegates, "PREPARE_LOCK_WAIT_SECONDS", 0.4), \
                    patch.object(sys, "argv",
                                 ["ao_delegates.py", "--home", str(self.home), "--room", self.room_b]), \
                    patch.object(Path, "cwd", return_value=self.repo):
                with self.assertRaises(ao.RoomError) as caught:
                    ao_delegates.main()
        finally:
            release.set()
        message = str(caught.exception)
        self.assertIn("not acquired within", message)
        self.assertIn(str(os.getpid()), message)
        self.assertIn(self.room_b, message)
        self.assertFalse((self.directory(self.room_b) / "preparation.json").exists())
        self.assertTrue(done.wait(20))
        self.assertFalse(errors)


class _DelayedWakeClock:
    """sleep() lands 30 seconds later — a scheduler delay far past any deadline."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += 30.0


class FlockAcquireBudgetTests(unittest.TestCase):
    """_flock_acquire's deadline is a best-effort budget, not a guaranteed maximum."""

    def _run_acquire(self, outcomes, timeout):
        """Drive _flock_acquire with a fake clock, fake sleep and canned flock outcomes."""
        clock = _DelayedWakeClock()
        deadline = None if timeout is None else clock.now + timeout
        calls = []

        def flock(stream, flags):
            calls.append(flags)
            outcome = outcomes[len(calls) - 1]
            if outcome is not None:
                raise outcome

        error = None
        with patch("ao_project_room.time.monotonic", side_effect=clock.monotonic), \
                patch("ao_project_room.time.sleep", side_effect=clock.sleep), \
                patch("ao_project_room.fcntl.flock", side_effect=flock):
            try:
                ao._flock_acquire(object(), fcntl.LOCK_EX, deadline)
            except Exception as exc:  # noqa: BLE001 - asserted by the test body
                error = exc
        return clock, calls, error

    def test_successful_poll_after_a_delayed_wake_is_accepted(self):
        clock, calls, error = self._run_acquire([BlockingIOError(), None], timeout=0.5)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(flags & fcntl.LOCK_NB for flags in calls))
        self.assertEqual(clock.sleeps, [0.1])
        self.assertEqual(clock.now, 30.0)

    def test_failed_poll_after_a_delayed_wake_raises_lock_timeout(self):
        clock, calls, error = self._run_acquire([BlockingIOError(), BlockingIOError()], timeout=0.5)
        self.assertIsInstance(error, ao._LockTimeout)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(flags & fcntl.LOCK_NB for flags in calls))
        self.assertEqual(clock.sleeps, [0.1])

    def test_blocking_acquire_is_one_plain_flock_without_sleep(self):
        clock, calls, error = self._run_acquire([None], timeout=None)
        self.assertIsNone(error)
        self.assertEqual(calls, [fcntl.LOCK_EX])
        self.assertFalse(calls[0] & fcntl.LOCK_NB)
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
