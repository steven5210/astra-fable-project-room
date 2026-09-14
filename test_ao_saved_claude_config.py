"""Saved Claude directory selection across cwd changes; no account or model access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ao_routing
import project_room


class SavedClaudeConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        self.caller = self.root / 'caller'; self.caller.mkdir()
        self.runtime = self.root / 'retained-runtime'; self.runtime.mkdir()
        self.user_home = self.root / 'user-home'; self.user_home.mkdir()
        self.controller_home = self.root / 'controller'
        environment = patch.dict(os.environ, {'HOME': str(self.user_home)}, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        self.stub = self.root / 'never-executed-claude'
        self.stub.write_text('This fixture must never be executed.\n'); self.stub.chmod(0o700)
        self.service = project_room.Service(self.controller_home)
        self.config_path = self.controller_home / 'config.json'
        self.ao_service = SimpleNamespace(root=self.controller_home / 'ao')
        self.room_directory = self.ao_service.root / 'rooms' / 'synthetic-room'
        os.chdir(self.caller)

    def setup(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('Setup must not invoke a CLI')):
            self.service.setup(claude_bin=str(self.stub), delegate_provider='none')
        return json.loads(self.config_path.read_bytes())

    def context(self):
        return ao_routing.context(self.ao_service, self.room_directory, {})

    def test_canonical_setup_keeps_child_environment_and_ao_context_aligned_after_cwd_change(self):
        for number, override in enumerate(('claude-local', '~/claude-local', str(self.root / 'absolute-claude'))):
            with self.subTest(override=override):
                os.chdir(self.caller)
                self.config_path.unlink(missing_ok=True)
                os.environ['CLAUDE_CONFIG_DIR'] = override
                expected = str(Path(override).expanduser().resolve())
                config = self.setup()
                self.assertEqual(config['claude_config_dir_override'], expected)
                self.assertEqual(config['claude_config_dir'], expected)
                # Exercise the real immutable room settings/profile snapshots.
                opened = self.service.room_open(str(self.caller), 'directory-selection-' + str(number))
                snapshot = Path(opened['path']) / 'settings.json'
                pinned = json.loads(snapshot.read_bytes())
                self.assertEqual(pinned['claude_config_dir_override'], expected)
                implementation = json.loads((snapshot.parent / 'profiles/implementation.json').read_bytes())
                self.assertEqual(implementation['claude_config_dir_override'], expected)
                os.chdir(self.runtime)
                # The inherited environment intentionally differs: the saved
                # selection must continue to own both invocation and routing.
                os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'unrelated-directory')
                self.assertEqual(self.context()['claude_config_dir'], expected)
                self.assertEqual(ao_routing.context(self.ao_service, snapshot.parent, {})['claude_config_dir'], expected)
                child = subprocess.run([sys.executable, '-I', '-B', '-c',
                                        'import json, os; print(json.dumps(os.environ.get("CLAUDE_CONFIG_DIR")))'],
                                       cwd=self.runtime, env=project_room.claude_environment(pinned),
                                       capture_output=True, text=True, timeout=5, check=True)
                self.assertEqual(json.loads(child.stdout), expected)

    def test_unset_override_stays_unset_across_setup_repair_and_invocation(self):
        config = self.setup()
        expected = str(self.user_home / '.claude')
        self.assertIsNone(config['claude_config_dir_override'])
        self.assertEqual(config['claude_config_dir'], expected)
        os.chdir(self.runtime)
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'unrelated-directory')
        repaired = self.setup()
        self.assertIsNone(repaired['claude_config_dir_override'])
        self.assertEqual(repaired['claude_config_dir'], expected)
        self.assertNotIn('CLAUDE_CONFIG_DIR', project_room.claude_environment(repaired))
        self.assertEqual(self.context()['claude_config_dir'], expected)

    def test_matching_historical_pair_normalizes_only_controller_and_keeps_selection(self):
        os.environ['CLAUDE_CONFIG_DIR'] = 'claude-local'
        config = self.setup()
        config['claude_config_dir_override'] = 'claude-local'
        project_room.atomic_json(self.config_path, config)  # exact historical setup shape
        opened = self.service.room_open(str(self.caller), 'historical-selection')
        room_root = Path(opened['path'])
        before = {str(path.relative_to(room_root)): path.read_bytes()
                  for path in room_root.rglob('*') if path.is_file()}
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'unrelated-directory')
        normalized = self.setup()
        self.assertEqual(normalized['claude_config_dir_override'], str(self.caller / 'claude-local'))
        self.assertEqual(normalized['claude_config_dir'], config['claude_config_dir'])
        self.assertEqual({str(path.relative_to(room_root)): path.read_bytes()
                          for path in room_root.rglob('*') if path.is_file()}, before)
        os.chdir(self.runtime)
        with self.assertRaisesRegex(project_room.room.RoomError, 'Existing room snapshots remain unchanged'):
            ao_routing.context(self.ao_service, room_root, {})

    def test_ambiguous_historical_pair_refuses_before_rewrite_and_account_selection(self):
        os.environ['CLAUDE_CONFIG_DIR'] = 'claude-local'
        config = self.setup()
        config['claude_config_dir_override'] = 'claude-local'
        project_room.atomic_json(self.config_path, config)
        before = self.config_path.read_bytes()
        os.chdir(self.runtime)
        # An inherited absolute value does not authorize repairing a different
        # saved relative selection or overriding its conflict.
        os.environ['CLAUDE_CONFIG_DIR'] = config['claude_config_dir']
        with patch.object(project_room, 'atomic_json', side_effect=AssertionError('No configuration publication allowed')):
            with self.assertRaisesRegex(project_room.room.RoomError, 'run setup from the original directory'):
                self.setup()
        self.assertEqual(self.config_path.read_bytes(), before)
        with self.assertRaisesRegex(project_room.room.RoomError, 'no account directory was selected'):
            self.context()

    def test_saved_relative_override_requires_an_absolute_recorded_directory(self):
        os.environ['CLAUDE_CONFIG_DIR'] = 'claude-local'
        config = self.setup()
        for recorded in (None, 'claude-local'):
            with self.subTest(recorded=recorded):
                prior = {**config, 'claude_config_dir_override': 'claude-local', 'claude_config_dir': recorded}
                project_room.atomic_json(self.config_path, prior)
                before = self.config_path.read_bytes()
                with self.assertRaisesRegex(project_room.room.RoomError, 'recorded absolute directory'):
                    self.setup()
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_relative_recorded_directory_refuses_with_missing_or_null_override_after_cwd_drift(self):
        config = self.setup()
        for explicit_null in (False, True):
            with self.subTest(override='null' if explicit_null else 'missing'):
                prior = {**config, 'claude_config_dir': 'relative-claude'}
                if explicit_null:
                    prior['claude_config_dir_override'] = None
                else:
                    prior.pop('claude_config_dir_override')
                project_room.atomic_json(self.config_path, prior)
                before = self.config_path.read_bytes()
                os.chdir(self.runtime)
                self.assertNotIn('CLAUDE_CONFIG_DIR', os.environ)
                with patch.object(project_room, 'atomic_json', side_effect=AssertionError('No configuration publication allowed')):
                    with self.assertRaisesRegex(project_room.room.RoomError, 'nonempty recorded absolute directory'):
                        self.setup()
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_present_empty_null_or_malformed_recorded_directory_is_not_an_absent_default(self):
        config = self.setup()
        for explicit_null in (False, True):
            for recorded in (None, '', [], 42, False):
                with self.subTest(override='null' if explicit_null else 'missing', recorded=recorded):
                    prior = {**config, 'claude_config_dir': recorded}
                    if not explicit_null:
                        prior.pop('claude_config_dir_override')
                    project_room.atomic_json(self.config_path, prior)
                    before = self.config_path.read_bytes()
                    with patch.object(project_room, 'atomic_json', side_effect=AssertionError('No configuration publication allowed')):
                        with self.assertRaisesRegex(project_room.room.RoomError, 'nonempty recorded absolute directory'):
                            self.setup()
                    self.assertEqual(self.config_path.read_bytes(), before)

    def test_absolute_disagreement_and_empty_override_refuse_without_rewrite(self):
        os.environ['CLAUDE_CONFIG_DIR'] = ''
        with self.assertRaisesRegex(project_room.room.RoomError, 'nonempty directory override'):
            self.setup()
        self.assertFalse(self.config_path.exists())
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.root / 'selected-directory')
        config = self.setup()
        config['claude_config_dir_override'] = str(self.root / 'other-directory')
        project_room.atomic_json(self.config_path, config)
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(project_room.room.RoomError, 'override and recorded directory disagree'):
            self.setup()
        with self.assertRaisesRegex(project_room.room.RoomError, 'override and recorded directory disagree'):
            self.context()
        self.assertEqual(self.config_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
