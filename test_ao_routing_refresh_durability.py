"""Directory-sync faults occur after real publication; all rooms and AO are synthetic."""

import errno
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_routing_guard
import ao_routing_refresh as refresh
import test_ao_routing_refresh as fixtures


class RoutingRefreshDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.RoutingRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()

    def exercise_directory_failure(self, kind):
        case = self.case
        if kind == 'guard':
            path = case.service.root / 'launchers' / (ao.digest(Path(ao_routing_guard.__file__).read_bytes()) + '.py')
            published = path.exists
        elif kind == 'state':
            path = case.directory() / 'state.json'
            published = lambda: bool(case.state().get('routing_refresh'))
        else:
            relative = '.claude/settings.local.json' if kind == 'settings' else '.claude/agents/pr-opus.md'
            path = case.repo / relative
            published = lambda: path.read_bytes() != case.original_files[relative]
        fsync = os.fsync
        failures = []

        def fail(fd):
            descriptor, directory = os.fstat(fd), path.parent.stat()
            if (stat.S_ISDIR(descriptor.st_mode) and published()
                    and (descriptor.st_dev, descriptor.st_ino) == (directory.st_dev, directory.st_ino)):
                failures.append(kind)
                raise OSError(errno.EIO, 'synthetic post-publication directory fsync failure: ' + kind)
            return fsync(fd)

        journal = case.directory() / refresh.BASE / 'refresh-one.json'
        original_state = (case.directory() / 'state.json').read_bytes()
        with patch.object(refresh.os, 'fsync', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'post-publication directory fsync failure'):
                case.do_refresh()
            self.assertTrue(published())  # The real link/rename completed before the injected fault.
            record, state_after = journal.read_bytes(), (case.directory() / 'state.json').read_bytes()
            target, stamp = path.read_bytes(), path.stat().st_mtime_ns
            if kind == 'state':
                self.assertTrue(case.state().get('routing_refresh'))  # Visible, but not yet durably acknowledged.
            else:
                self.assertEqual(state_after, original_state)
            with self.assertRaisesRegex(OSError, 'post-publication directory fsync failure'):
                case.do_refresh()
            self.assertEqual(failures, [kind, kind])
            self.assertEqual((case.directory() / 'state.json').read_bytes(), state_after)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (target, stamp))
            self.assertEqual(journal.read_bytes(), record)
        result = case.do_refresh()
        self.assertFalse(result['model_dispatch'])
        self.assertEqual(result['idempotent'], kind == 'state')
        self.assertEqual(journal.read_bytes(), record)
        self.assertEqual({key: value for key, value in case.state().items() if key != 'routing_refresh'}, case.original)
        self.assertEqual((case.directory() / case.state()['preparation']).read_bytes(), case.prepared_bytes)
        self.assertEqual(case.fake.posts, case.posts)

    def test_guard_link_directory_failure_blocks_each_retry_until_synced(self):
        self.exercise_directory_failure('guard')

    def test_settings_rename_directory_failure_blocks_each_retry_until_synced(self):
        self.exercise_directory_failure('settings')

    def test_last_agent_rename_directory_failure_blocks_each_retry_until_synced(self):
        self.exercise_directory_failure('last_agent')

    def test_visible_state_pointer_is_not_acknowledged_until_its_directory_sync_succeeds(self):
        self.exercise_directory_failure('state')

    def test_guard_publication_race_and_unknown_runtime_bytes_are_never_overwritten(self):
        case = self.case
        guard = case.root / 'synthetic-guard.py'

        def competing_link(*args, **kwargs):
            guard.write_bytes(b'foreign guard bytes')
            raise FileExistsError('synthetic competing guard publication')

        with patch.object(refresh.os, 'link', side_effect=competing_link):
            with self.assertRaisesRegex(ao.RoomError, 'appeared with different bytes'):
                refresh._place_guard(guard, b'exact target guard bytes')
        self.assertEqual(guard.read_bytes(), b'foreign guard bytes')
        relative = '.claude/agents/pr-sonnet.md'
        path = case.repo / relative
        path.write_bytes(b'foreign runtime bytes')
        with self.assertRaisesRegex(ao.RoomError, 'matches neither recorded source nor target'):
            refresh._replace_runtime(case.repo, relative, case.original_files[relative], b'exact target runtime bytes')
        self.assertEqual(path.read_bytes(), b'foreign runtime bytes')
        self.assertEqual(case.state(), case.original)
        self.assertEqual(case.fake.posts, case.posts)


if __name__ == '__main__':
    unittest.main()
