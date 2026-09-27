"""Lock-creation race regression tests for the private Read admission lock.

Synthetic and offline: no model, provider, credential, network, live native
transcript or private room state is used. These tests drive
``ao_routing_guard._lock_state`` directly and through the copied-alone guard
script, and they reuse the synthetic builders of the accepted root Read
admission suite without inheriting (and thus re-running) its cases.
"""

import errno
import fcntl
import os
import stat
import time
import unittest
from unittest import mock

import ao_routing_guard as guard

# Accepted synthetic fixture suite. If the suite file is named differently,
# only this import needs adjusting; the class and its builders are unchanged.
import test_ao_read_admission as _fixtures


def _darwin_create_race_open(attempts):
    """``os.open`` stand-in for the recorded macOS create race.

    The reproduced failure is ``ENOENT`` from one ``O_CREAT|O_NOFOLLOW`` open of
    the lock name while another process is creating that same name. This
    stand-in fails exactly on that combination and delegates every other open
    unchanged, so a racy single-open acquisition fails and a two-step
    acquisition passes.
    """
    real_open = os.open

    def open_with_race(path, flags, mode=0o777, *, dir_fd=None):
        attempts.append(flags)
        if flags & os.O_CREAT and flags & os.O_NOFOLLOW and not flags & os.O_EXCL:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    return open_with_race


class _DescriptorLedger:
    """Records guard-side opens and closes so refused acquisitions cannot leak."""

    def __init__(self):
        self.opened = []
        self.closed = []

    def patch(self):
        real_open, real_close = os.open, os.close
        ledger = self

        def open_recorded(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            ledger.opened.append(descriptor)
            return descriptor

        def close_recorded(descriptor):
            ledger.closed.append(descriptor)
            return real_close(descriptor)

        return mock.patch.multiple(guard.os, open=open_recorded, close=close_recorded)


class LockCreationRaceTests(unittest.TestCase):
    """Regressions over the accepted synthetic fixture builders."""

    # Fixture builders are bound explicitly: reusing the accepted synthetic
    # fixture must not re-run the accepted cases from this module.
    SESSION = _fixtures.ReadAdmissionTests.SESSION
    setUp = _fixtures.ReadAdmissionTests.setUp
    assistant_row = _fixtures.ReadAdmissionTests.assistant_row
    read_item = _fixtures.ReadAdmissionTests.read_item
    caller_row = _fixtures.ReadAdmissionTests.caller_row
    source = _fixtures.ReadAdmissionTests.source
    message = _fixtures.ReadAdmissionTests.message
    write_transcript = _fixtures.ReadAdmissionTests.write_transcript
    event = _fixtures.ReadAdmissionTests.event
    state_directory = _fixtures.ReadAdmissionTests.state_directory
    record_path = _fixtures.ReadAdmissionTests.record_path
    record = _fixtures.ReadAdmissionTests.record
    run_guard_processes = _fixtures.ReadAdmissionTests.run_guard_processes

    def lock_directory(self):
        """Session-directory descriptor of the shape ``_reserve_read`` passes in."""
        directory = self.state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(directory), os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def lock_path(self):
        return self.state_directory() / guard.READ_LOCK_NAME

    def acquire(self, directory):
        descriptor = guard._lock_state(directory)
        self.addCleanup(os.close, descriptor)
        return descriptor

    # -- creation race ------------------------------------------------------
    def test_fresh_lock_survives_the_simulated_creation_race(self):
        directory = self.lock_directory()
        attempts = []
        with mock.patch.object(guard.os, "open", _darwin_create_race_open(attempts)):
            descriptor = self.acquire(directory)
        self.assertTrue(attempts)
        metadata = os.fstat(descriptor)
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(metadata.st_uid, os.getuid())
        self.assertEqual(metadata.st_mode & 0o022, 0)
        self.assertEqual(metadata.st_nlink, 1)
        self.assertTrue(self.lock_path().is_file())
        second = os.open(str(self.lock_path()), os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(os.close, second)
        with self.assertRaises(OSError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_existing_lock_inode_is_reused_under_the_race(self):
        directory = self.lock_directory()
        first = guard._lock_state(directory)
        try:
            before = os.fstat(first)
        finally:
            os.close(first)
        attempts = []
        with mock.patch.object(guard.os, "open", _darwin_create_race_open(attempts)):
            second = self.acquire(directory)
        self.assertTrue(attempts)
        after = os.fstat(second)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_lock_that_vanishes_between_create_and_open_is_refused(self):
        directory = self.lock_directory()

        def vanishing(path, flags, mode=0o777, *, dir_fd=None):
            if flags & os.O_CREAT:
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), path)
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

        with mock.patch.object(guard.os, "open", vanishing):
            with self.assertRaises(guard._ReadRefused):
                guard._lock_state(directory)
        self.assertFalse(self.lock_path().exists())

    # -- refusal boundaries -------------------------------------------------
    def test_missing_flock_support_is_refused(self):
        directory = self.lock_directory()
        with mock.patch.object(guard, "fcntl", None):
            with self.assertRaises(guard._ReadRefused):
                guard._lock_state(directory)

    def test_symlink_lock_name_is_not_followed(self):
        directory = self.lock_directory()
        target = self.worktree / "not-the-lock.txt"
        target.write_bytes(b"untouched")
        os.symlink(str(target), str(self.lock_path()))
        with self.assertRaises(OSError) as caught:
            guard._lock_state(directory)
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertTrue(self.lock_path().is_symlink())

    def test_refusal_closes_the_descriptor(self):
        directory = self.lock_directory()
        lock = self.lock_path()
        lock.write_bytes(b"")
        lock.chmod(0o600)
        os.link(str(lock), str(lock) + ".alias")
        self.addCleanup(os.unlink, str(lock) + ".alias")
        ledger = _DescriptorLedger()
        with ledger.patch():
            with self.assertRaises(guard._ReadRefused):
                guard._lock_state(directory)
        self.assertEqual(ledger.opened, ledger.closed)

    def test_acquisition_wait_is_bounded_and_closes_the_descriptor(self):
        directory = self.lock_directory()
        lock = self.lock_path()
        lock.write_bytes(b"")
        lock.chmod(0o600)
        holder = os.open(str(lock), os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        wait = 0.05
        ledger = _DescriptorLedger()
        with mock.patch.object(guard, "READ_LOCK_WAIT_SECONDS", wait), ledger.patch():
            started = time.monotonic()
            with self.assertRaises(guard._ReadRefused):
                guard._lock_state(directory)
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, wait)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(ledger.opened, ledger.closed)

    # -- reported concurrent scenario ---------------------------------------
    def test_concurrent_copied_alone_reservations_with_fresh_lock_file(self):
        self.assertFalse(self.state_directory().exists())
        files = [self.source("race-%d.txt" % index, 12000) for index in range(3)]
        tool_uses = [("toolu_race_%d" % index, {"file_path": str(files[index])})
                     for index in range(3)]
        self.write_transcript([self.caller_row(), self.message("msg-race", tool_uses)])
        results = self.run_guard_processes([self.event(tool_id, params)
                                            for tool_id, params in tool_uses])
        for returncode, stdout, _ in results:
            self.assertEqual((returncode, stdout), (0, ""))
        self.assertTrue(self.lock_path().is_file())
        self.assertEqual(self.record("msg-race")["total_raw_bytes"], 36000)
