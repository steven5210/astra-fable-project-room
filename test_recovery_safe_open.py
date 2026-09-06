"""Owned, descriptor-bound evidence reads: every component below the trusted root is opened relative to the previous
directory descriptor with O_NOFOLLOW, the leaf with O_NOFOLLOW|O_NONBLOCK, and races are injected at that exact seam."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import recovery


class OpenOwnedRegularTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.attempt = self.root / "attempts" / "0001"
        self.attempt.mkdir(parents=True)
        self.target = self.attempt / "evidence.txt"
        self.target.write_bytes(b"owned evidence bytes")
        self.outside = self.root.parent / (self.root.name + "-outside-canary")
        self.outside.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.outside, ignore_errors=True))
        (self.outside / "evidence.txt").write_bytes(b"OUTSIDE_CANARY")

    def refused(self, path, limit=1024, kind="evidence", root=None):
        with self.assertRaises(recovery.ObservationError) as caught:
            with recovery.open_owned_regular(path, limit, kind, root or self.root):
                pass
        return caught.exception.reason

    def inject(self, action, when):
        """Run `action` at the seam right before the component named `when` is opened; report whether it fired."""
        original, fired = recovery._open_component, []

        def seam(name, flags, dir_fd):
            if name == when and not fired:
                fired.append(True)
                action()
            return original(name, flags, dir_fd)
        return mock.patch.object(recovery, "_open_component", new=seam), fired

    def test_regular_owned_file_below_root_is_readable(self):
        with recovery.open_owned_regular(self.target, 1024, root=self.root) as handle:
            self.assertEqual(handle.read(), b"owned evidence bytes")
            self.assertTrue(stat.S_ISREG(os.fstat(handle.fileno()).st_mode))
        self.assertEqual(recovery.read_owned(self.target, 1024, root=self.root), b"owned evidence bytes")
        with recovery.open_owned_regular(self.target, 1024) as handle:  # default root: the parent directory
            self.assertEqual(handle.read(), b"owned evidence bytes")

    def test_missing_path_and_kind_prefix(self):
        self.assertEqual(self.refused(self.attempt / "absent"), "evidence_missing")
        self.assertEqual(self.refused(self.root / "gone" / "absent", kind="transcript"), "transcript_missing")

    def test_paths_outside_root_or_with_dot_components_are_unsafe(self):
        self.assertEqual(self.refused(self.outside / "evidence.txt"), "evidence_unsafe")
        self.assertEqual(self.refused(self.root / "attempts" / ".." / ".." / self.outside.name / "evidence.txt"), "evidence_unsafe")
        self.assertEqual(self.refused(self.root / "attempts" / ".." / "evidence.txt"), "evidence_unsafe")  # nothing below the root is resolved

    def test_configured_alias_of_the_root_is_the_same_bound_directory(self):
        alias = self.root.parent / (self.root.name + "-alias")
        alias.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(alias.unlink)
        self.assertEqual(recovery.directory_identity(alias), recovery.directory_identity(self.root))
        with recovery.OwnedRoot(alias, recovery.directory_identity(self.root)) as root:
            self.assertEqual(recovery.read_owned(alias / "attempts" / "0001" / "evidence.txt", 1024, root=root), b"owned evidence bytes")

    def test_root_swapped_before_its_open_is_refused_before_any_read(self):
        identity = recovery.directory_identity(self.root)
        original, fired, opened = recovery._open_root, [], []

        def swap_root(path, flags):
            if path == str(self.root) and not fired:
                fired.append(True)
                self.root.rename(self.root.with_name(self.root.name + "-preserved"))
                self.root.symlink_to(self.outside, target_is_directory=True)
            return original(path, flags)
        original_component = recovery._open_component

        def track(name, flags, dir_fd):
            opened.append(name)
            return original_component(name, flags, dir_fd)
        with mock.patch.object(recovery, "_open_root", new=swap_root), mock.patch.object(recovery, "_open_component", new=track):
            with self.assertRaises(recovery.ObservationError) as caught:
                recovery.OwnedRoot(self.root, identity)
        self.assertTrue(fired)
        self.assertEqual(caught.exception.reason, "evidence_unsafe")
        self.assertEqual(opened, [])  # refused at the root descriptor; no component below it was opened
        self.root.unlink()
        self.root.with_name(self.root.name + "-preserved").rename(self.root)

    def test_bound_root_survives_a_later_swap_of_its_path(self):
        with recovery.OwnedRoot(self.root, recovery.directory_identity(self.root)) as root:
            self.root.rename(self.root.with_name(self.root.name + "-preserved"))
            self.root.symlink_to(self.outside, target_is_directory=True)
            try:
                data = recovery.read_owned(self.root / "attempts" / "0001" / "evidence.txt", 1024, root=root)
            finally:
                self.root.unlink()
                self.root.with_name(self.root.name + "-preserved").rename(self.root)
        self.assertEqual(data, b"owned evidence bytes")  # the descriptor, not the swapped path, is what is read

    def test_symlink_leaf_symlink_component_and_directory_leaf_are_unsafe(self):
        link = self.attempt / "link.txt"
        link.symlink_to(self.outside / "evidence.txt")
        self.assertEqual(self.refused(link), "evidence_unsafe")
        (self.root / "attempts" / "0002").symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(self.refused(self.root / "attempts" / "0002" / "evidence.txt"), "evidence_unsafe")
        self.assertEqual(self.refused(self.attempt), "evidence_unsafe")

    def test_size_limit_is_inclusive(self):
        self.target.write_bytes(b"x" * 8)
        with recovery.open_owned_regular(self.target, 8, root=self.root) as handle:
            self.assertEqual(len(handle.read()), 8)
        self.target.write_bytes(b"x" * 9)
        self.assertEqual(self.refused(self.target, limit=8), "evidence_oversized")

    def test_foreign_owner_is_unsafe(self):
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(self.refused(self.target), "evidence_unsafe")

    def test_leaf_swapped_to_symlink_at_open_is_refused_without_following(self):
        def swap():
            self.target.unlink()
            self.target.symlink_to(self.outside / "evidence.txt")
        patcher, fired = self.inject(swap, "evidence.txt")
        with patcher:
            self.assertEqual(self.refused(self.target), "evidence_unsafe")
        self.assertTrue(fired)

    def test_leaf_swapped_to_fifo_at_open_is_refused_without_blocking(self):
        def swap():
            self.target.unlink()
            os.mkfifo(self.target, 0o600)
        patcher, fired = self.inject(swap, "evidence.txt")
        with patcher:
            self.assertEqual(self.refused(self.target), "evidence_unsafe")  # a FIFO opened O_NONBLOCK returns at once and fails fstat
        self.assertTrue(fired)

    def test_attempt_directory_swapped_to_symlink_before_its_open_is_refused(self):
        def swap():
            self.attempt.rename(self.attempt.with_name("preserved-original"))
            self.attempt.symlink_to(self.outside, target_is_directory=True)
        patcher, fired = self.inject(swap, "0001")
        with patcher:
            self.assertEqual(self.refused(self.target), "evidence_unsafe")
        self.assertTrue(fired)

    def test_attempt_directory_swapped_after_its_open_reads_the_bound_directory_not_the_symlink_target(self):
        def swap():
            self.attempt.rename(self.attempt.with_name("preserved-original"))
            self.attempt.symlink_to(self.outside, target_is_directory=True)
        patcher, fired = self.inject(swap, "evidence.txt")
        with patcher, recovery.open_owned_regular(self.target, 1024, root=self.root) as handle:
            data = handle.read()
        self.assertTrue(fired)
        self.assertEqual(data, b"owned evidence bytes")  # the descriptor of the validated directory, never the outside canary

    def test_replacement_by_a_different_inode_before_open_is_the_new_file_only_if_owned_and_regular(self):
        def replace():
            self.target.unlink()
            with open(str(self.target), "wb") as replacement:
                replacement.write(b"different inode")
        patcher, fired = self.inject(replace, "evidence.txt")
        with patcher, recovery.open_owned_regular(self.target, 1024, root=self.root) as handle:
            self.assertEqual(handle.read(), b"different inode")  # still an owned regular file below the bound directory
        self.assertTrue(fired)


class DescriptorRelativeWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_path = Path(self.temp.name).resolve()
        self.root = recovery.OwnedRoot(self.root_path)
        self.addCleanup(self.root.close)
        self.outside = self.root_path.parent / (self.root_path.name + "-outside-write")
        self.outside.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.outside, ignore_errors=True))

    def test_create_write_and_replace_stay_relative_to_the_bound_descriptor(self):
        fd = recovery.directory_below(self.root, ("recoveries", "abc"), create=True, exclusive=True)
        try:
            payload = recovery.write_json_below(fd, "record.json", {"ok": True})
            self.assertEqual((self.root_path / "recoveries" / "abc" / "record.json").read_bytes(), payload)
            self.assertEqual(oct((self.root_path / "recoveries" / "abc").stat().st_mode & 0o777), oct(0o700))
            self.assertEqual(oct((self.root_path / "recoveries" / "abc" / "record.json").stat().st_mode & 0o777), oct(0o600))
            recovery.write_below(fd, "record.json", b"replaced")  # atomic replacement of an existing name
            self.assertEqual((self.root_path / "recoveries" / "abc" / "record.json").read_bytes(), b"replaced")
            self.assertTrue(recovery.exists_below(fd, "record.json"))
            self.assertFalse(recovery.exists_below(fd, "absent"))
            with self.assertRaises(recovery.ObservationError):
                recovery.directory_below(self.root, ("recoveries", "abc"), create=True, exclusive=True)  # one-use directory
        finally:
            os.close(fd)

    def test_writes_follow_the_descriptor_after_the_root_path_is_swapped(self):
        fd = recovery.directory_below(self.root, ("recoveries",), create=True)
        try:
            self.root_path.rename(self.root_path.with_name(self.root_path.name + "-preserved"))
            self.root_path.symlink_to(self.outside, target_is_directory=True)
            try:
                recovery.write_below(fd, "note.json", b"{}")
            finally:
                self.root_path.unlink()
                self.root_path.with_name(self.root_path.name + "-preserved").rename(self.root_path)
            self.assertTrue((self.root_path / "recoveries" / "note.json").is_file())
            self.assertEqual(list(self.outside.iterdir()), [])  # nothing landed in the substituted directory
        finally:
            os.close(fd)

    def test_symlinked_or_foreign_components_are_refused_and_failures_leave_no_temporary(self):
        (self.root_path / "recoveries").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(recovery.ObservationError) as caught:
            recovery.directory_below(self.root, ("recoveries", "abc"), create=True)
        self.assertEqual(caught.exception.reason, "evidence_unsafe")
        self.assertEqual(list(self.outside.iterdir()), [])
        (self.root_path / "recoveries").unlink()
        fd = recovery.directory_below(self.root, ("recoveries",), create=True)
        try:
            with self.assertRaises(RuntimeError):
                recovery.write_below(fd, "record.json", iter([b"part", RuntimeError("synthetic write failure")]) if False else _failing_chunks())
            self.assertEqual(sorted(path.name for path in (self.root_path / "recoveries").iterdir()), [])
            with self.assertRaises(recovery.ObservationError):
                recovery.write_below(fd, "../escape.json", b"{}")
            with mock.patch.object(os, "getuid", return_value=os.getuid() + 1), self.assertRaises(recovery.ObservationError):
                recovery.directory_below(self.root, ("recoveries",))
        finally:
            os.close(fd)
        closed = recovery.OwnedRoot(self.root_path)
        closed.close()
        with self.assertRaises(recovery.ObservationError):
            recovery.directory_below(closed, ("recoveries",))


def _failing_chunks():
    yield b"part"
    raise RuntimeError("synthetic write failure")


if __name__ == "__main__":
    unittest.main()
