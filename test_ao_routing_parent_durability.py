"""Launcher-parent durability and supported adoption provenance; all inputs synthetic."""

import copy
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_routing
import ao_routing_refresh as refresh
import test_ao_adoption as adoption_fixtures
import test_ao_review_extension as extension_fixtures


class LauncherParentDurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.controller = self.root / 'controller'
        self.controller.mkdir()
        self.target = self.controller / 'launchers' / 'synthetic-guard.py'
        self.raw = b'# synthetic complete guard\n'

    def parent_failure(self, failures):
        fsync = os.fsync

        def fail(fd):
            descriptor, directory = os.fstat(fd), self.controller.stat()
            if (stat.S_ISDIR(descriptor.st_mode) and self.target.parent.is_dir()
                    and (descriptor.st_dev, descriptor.st_ino) == (directory.st_dev, directory.st_ino)):
                failures.append(True)
                raise OSError(errno.EIO, 'synthetic launcher-parent directory sync failure')
            return fsync(fd)

        return fail

    def test_missing_launcher_directory_must_sync_its_parent_before_guard_publication_and_retry(self):
        self.assertFalse(self.target.parent.exists())
        failures = []
        with patch.object(refresh.os, 'fsync', side_effect=self.parent_failure(failures)), \
                patch.object(refresh.os, 'link', side_effect=AssertionError('guard published before parent sync')):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, 'launcher-parent directory sync failure'):
                    refresh._place_guard(self.target, self.raw)
                self.assertTrue(self.target.parent.is_dir())  # mkdir really completed before the injected fault.
                self.assertEqual(list(self.target.parent.iterdir()), [])
        self.assertEqual(failures, [True, True])
        refresh._place_guard(self.target, self.raw)
        self.assertEqual(self.target.read_bytes(), self.raw)

    def test_exact_existing_guard_retries_parent_sync_without_rewriting_its_bytes(self):
        refresh._place_guard(self.target, self.raw)
        stamp = self.target.stat().st_mtime_ns
        failures = []
        with patch.object(refresh.os, 'fsync', side_effect=self.parent_failure(failures)), \
                patch.object(refresh.os, 'link', side_effect=AssertionError('exact guard was republished')):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, 'launcher-parent directory sync failure'):
                    refresh._place_guard(self.target, self.raw)
                self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), (self.raw, stamp))
        self.assertEqual(failures, [True, True])
        refresh._place_guard(self.target, self.raw)
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), (self.raw, stamp))

    def test_unknown_guard_and_symlink_paths_refuse_before_any_directory_sync(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'unknown guard bytes')
        with patch.object(refresh, '_sync_directory', side_effect=AssertionError('unsafe guard directory was synced')):
            with self.assertRaisesRegex(ao.RoomError, 'corrupted'):
                refresh._place_guard(self.target, self.raw)
        self.assertEqual(self.target.read_bytes(), b'unknown guard bytes')
        self.target.unlink()
        outside = self.root / 'outside'
        outside.mkdir()
        self.target.parent.rmdir()
        self.target.parent.symlink_to(outside, target_is_directory=True)
        with patch.object(refresh, '_sync_directory', side_effect=AssertionError('symlinked parent was synced')):
            with self.assertRaisesRegex(ao.RoomError, 'symlink'):
                refresh._place_guard(self.target, self.raw)
        self.assertEqual(list(outside.iterdir()), [])
        self.target.parent.unlink()
        self.target.parent.mkdir()
        other = outside / 'foreign-guard.py'; other.write_bytes(b'foreign guard')
        self.target.symlink_to(other)
        with patch.object(refresh, '_sync_directory', side_effect=AssertionError('symlinked guard was synced')):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                refresh._place_guard(self.target, self.raw)
        self.assertEqual(other.read_bytes(), b'foreign guard')


class SupportedAdoptionFixture(adoption_fixtures.AdoptionFixture):
    def install_frozen_routing(self, service, directory, state, worktree, prepared):
        routing = super().install_frozen_routing(service, directory, state, worktree, prepared)
        # The synthetic historical receipt records a working executable from its
        # creation; no immutable preparation is changed to admit maintenance.
        routing['claude'] = ao_routing.claude_evidence(str(self.fake_cli))
        return routing


class AdoptedGuardParentDurabilityTests(unittest.TestCase):
    def test_supported_adoption_reaches_refresh_with_a_room_local_source_guard(self):
        case = SupportedAdoptionFixture('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        prepared = case.configure_routing()
        extension_fixtures.ReviewExtensionFixture.make_database(case)
        case.database.chmod(0o600)
        source = case.directory() / 'routing-adoption/v2/ao_routing_guard.py'
        self.assertEqual(prepared['routing']['guard_path'], str(source))
        original = copy.deepcopy(case.state())
        files = {str(path.relative_to(case.directory())): path.read_bytes()
                 for path in case.directory().rglob('*') if path.is_file()}
        posts = copy.deepcopy(case.fake.posts)
        synced = []
        sync = refresh._sync_directory

        def observe(path):
            synced.append(path)
            return sync(path)

        with patch.object(refresh, '_sync_directory', side_effect=observe):
            result = refresh.refresh(case.service, case.room, 'refresh-adopted-guard', str(case.database),
                'synthetic-native-owner', 'Synthetic authorization for the retained routing update.',
                'Synthetic source guard comes from supported v2 routing adoption.')
        self.assertFalse(result['model_dispatch'])
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertEqual(record['source']['guard_path'], str(source))
        self.assertEqual(Path(record['target']['guard_path']).parent, case.service.root / 'launchers')
        self.assertIn(case.service.root, synced)
        self.assertEqual({key: value for key, value in case.state().items() if key != 'routing_refresh'}, original)
        for name, raw in files.items():
            if name != 'state.json':
                self.assertEqual((case.directory() / name).read_bytes(), raw)
        self.assertEqual(case.fake.posts, posts)


if __name__ == '__main__':
    unittest.main()
