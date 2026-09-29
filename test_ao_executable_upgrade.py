"""Explicit audited upgrade of an intact recorded Claude executable; no real CLI or AO calls."""
import copy
import json
import sys
from unittest.mock import patch

import ao_delegates
import ao_executable_binding as binding
import ao_project_room as ao
import ao_routing
import ao_workflow
from test_ao_normal import Fixture


class ExecutableUpgradeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.old = self.root / 'old-claude'
        self.new = self.root / 'new-claude'
        for path, version in ((self.old, '2.1.268'), (self.new, '2.1.280')):
            path.write_text('#!/bin/sh\nprintf "' + version + ' (Claude Code)\\n"\n')
            path.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.old), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open(); self.spec()
        self.historical_preparation()
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', ao_workflow.FABLE_MODEL, 'max')
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', 'astra', 'max')
        self.register_native_source()
        self.prepared = ao_delegates.preparation(self.directory(), self.state())
        self.prep_bytes = (self.directory() / self.state()['preparation']).read_bytes()
        # The shared fixture already registered one complete synthetic owner for this bound
        # engineer. Preserve every actual identity, workspace, conversation and branch field and
        # change only the activity state this executable lane is about to observe.
        self.owner['activity_state'] = 'exited'
        p = patch.object(binding, 'read_owner', side_effect=lambda *a: copy.deepcopy(self.owner)); p.start(); self.addCleanup(p.stop)
        self.fake.sessions['engineer']['isTerminated'] = False
        self.fake.snapshots['engineer'].update(controller='stopped', hasMoreBefore=False, activities=[],
                                               branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original = self.fake.request
        def request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots['engineer'])
            return original(method, path, payload)
        self.fake.request = request
        self.launch = self.root / 'launch-claude'; self.launch.symlink_to(self.new)
        # Unlike a repair fixture, the recorded original executable stays intact here: the
        # upgrade lane only ever applies to a previously recorded, still-healthy identity.
        self.args = dict(room_id=self.room, request_id='upgrade-1', executable_path=str(self.new),
            launch_path=str(self.launch), database_path=str(self.root / 'fake.db'),
            authorization='User authorized this qualified Claude Code upgrade',
            diagnosis='Claude Code 2.1.268 rejects the newly qualified model before inference; operator installed 2.1.280')

    def historical_preparation(self):
        """Build the complete historical v2 preparation this upgrade lane exercises.

        A real, initially compatible preparation is recorded with the fixture's
        floor-compatible executable, then the preparation and worktree agent bytes are
        rewritten to the historical v2 family-alias shape and the recorded old 2.1.268
        executable's own probed identity. The room's root selection and pinned policy are
        not touched; no floor is lowered and no effective executable evidence is mocked.
        """
        config_path = self.home / 'config.json'
        value = ao.read(config_path)
        ao.atomic(config_path, {**value, 'claude_bin': str(self.fixture_claude)})
        try:
            self.service.ao_room_prepare(self.room, str(self.repo))
        finally:
            ao.atomic(config_path, value)
        directory, state = self.directory(), self.state()
        path = directory / state['preparation']
        prepared = ao.read(path)
        routing = prepared['routing']
        for name, model in ao_routing.FAMILY_AGENTS.items():
            relative = '.claude/agents/' + name + '.md'
            data = ao_routing.agent_definition(name, model).encode()
            (self.repo / relative).write_bytes(data)
            routing['files'][relative] = ao.digest(data)
        routing['version'] = 2
        routing['agents'] = dict(ao_routing.FAMILY_AGENTS)
        routing['agent_selection'] = {name: dict(item) for name, item in ao_routing.AGENT_SELECTION.items()}
        routing['agent_identity_basis'] = ao_routing.AGENT_IDENTITY_BASIS
        routing.pop('worker_qualification', None)
        routing['claude'] = ao_routing.claude_evidence(ao.read(config_path).get('claude_bin'))
        ao.atomic(path, prepared)
        state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(directory / 'state.json', state)
        return prepared

    def repair(self, **changes):
        return binding.bind(self.service, **{**self.args, **changes})

    def upgrade(self, **changes):
        defaults = dict(upgrade_authorization='User authorized upgrading to the newly qualified Claude Code 2.1.280',
                         expected_version='2.1.280')
        return self.repair(**{**defaults, **changes})

    def journal(self, request_id='upgrade-1'):
        return json.loads((self.directory() / 'executable-bindings' / (request_id + '.json')).read_text())

    def bindings_dir_exists(self):
        return (self.directory() / 'executable-bindings').exists()

    # a. Intact old executable + upgrade args with expected_version '2.1.280' succeeds.
    def test_intact_executable_upgrade_succeeds_and_preserves_preparation(self):
        result = self.upgrade()
        self.assertEqual(result['lane'], 'upgrade')
        self.assertFalse(result['idempotent'])
        record = self.journal()
        self.assertEqual(record['evidence']['source_failure'], 'authorized_upgrade')
        self.assertEqual(record['evidence']['upgrade'],
                          {'expected_version': '2.1.280', 'source_version': '2.1.268', 'target_version': '2.1.280'})
        self.assertIn('upgrade_authorization', record['inputs'])
        self.assertIn('expected_version', record['inputs'])
        self.assertEqual(record['inputs']['expected_version'], '2.1.280')
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), self.prep_bytes)
        target = binding.effective(self.directory(), self.state(), self.prepared)
        self.assertEqual(target['path'], str(self.new))
        self.assertEqual(target['version'], '2.1.280 (Claude Code)')

    # b. Identical repeat is idempotent (no additional journal file, same pointer, lane 'upgrade').
    def test_identical_upgrade_repeat_is_idempotent(self):
        first = self.upgrade()
        before = (self.directory() / 'state.json').read_bytes()
        files_before = sorted(p.name for p in (self.directory() / 'executable-bindings').glob('*.json'))
        second = self.upgrade()
        self.assertTrue(second['idempotent'])
        self.assertEqual(second['lane'], 'upgrade')
        self.assertEqual(second['sha256'], first['sha256'])
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        files_after = sorted(p.name for p in (self.directory() / 'executable-bindings').glob('*.json'))
        self.assertEqual(files_after, files_before)

    # c. Same request_id with changed upgrade args is refused.
    def test_changed_upgrade_arguments_refused(self):
        first = self.upgrade()
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.upgrade(upgrade_authorization='A different, later authorization text')
        self.assertEqual(self.state()['executable_binding']['sha256'], first['sha256'])

    # d. Only one of the two upgrade args is refused and nothing is written.
    def test_single_upgrade_argument_refused_before_any_write(self):
        self.assertFalse(self.bindings_dir_exists())
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'requires both upgrade_authorization and expected_version'):
            self.repair(upgrade_authorization='Only authorization given')
        with self.assertRaisesRegex(ao.RoomError, 'requires both upgrade_authorization and expected_version'):
            self.repair(expected_version='2.1.280')
        self.assertFalse(self.bindings_dir_exists())
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    # e. expected_version mismatch (probe 2.1.280, expected 2.1.281) refused, nothing written.
    def test_expected_version_mismatch_refused(self):
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'does not match the explicitly qualified expected_version'):
            self.upgrade(expected_version='2.1.281')
        self.assertFalse(self.bindings_dir_exists())
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    # f. Equal or older replacement version refused.
    def test_equal_or_older_replacement_version_refused(self):
        same = self.root / 'same-version-claude'
        same.write_text('#!/bin/sh\nprintf "2.1.268 (Claude Code)\\n"\n'); same.chmod(0o700)
        launch_same = self.root / 'launch-same'; launch_same.symlink_to(same)
        with self.assertRaisesRegex(ao.RoomError, 'strictly newer qualified version'):
            self.upgrade(request_id='upgrade-same', executable_path=str(same), launch_path=str(launch_same), expected_version='2.1.268')
        older = self.root / 'older-claude'
        older.write_text('#!/bin/sh\nprintf "2.1.200 (Claude Code)\\n"\n'); older.chmod(0o700)
        launch_older = self.root / 'launch-older'; launch_older.symlink_to(older)
        with self.assertRaisesRegex(ao.RoomError, 'strictly newer qualified version'):
            self.upgrade(request_id='upgrade-older', executable_path=str(older), launch_path=str(launch_older), expected_version='2.1.200')
        self.assertFalse(self.bindings_dir_exists())

    # g. Missing original + upgrade args refused with the repair-lane message; repair lane still works.
    def test_missing_original_with_upgrade_args_reports_repair_lane_message(self):
        self.old.unlink()
        with self.assertRaisesRegex(ao.RoomError,
                                    'Recorded executable is missing; use the diagnosed repair lane without upgrade arguments'):
            self.upgrade()
        self.assertFalse(self.bindings_dir_exists())
        result = self.repair(request_id='repair-after-missing')
        self.assertEqual(result['lane'], 'repair')
        self.assertFalse(result['idempotent'])

    # h. Unparseable recorded source version with upgrade args refused.
    def test_unparseable_recorded_source_version_refused(self):
        prep_path = self.directory() / self.state()['preparation']
        prepared = json.loads(prep_path.read_text())
        prepared['routing']['claude']['version'] = 'old (Claude Code)'
        ao.atomic(prep_path, prepared)
        state = self.state(); state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'Recorded executable version is not comparable'):
            self.upgrade()
        self.assertFalse(self.bindings_dir_exists())

    # i. Crash between the stored intent and the state commit reconciles; a changed retry is refused.
    def test_crash_between_intent_and_state_commit_reconciles_for_upgrade(self):
        before = self.state()
        with patch.object(self.service, 'save', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                self.upgrade()
        self.assertEqual(self.state(), before)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_routing.validate_local(self.prepared, self.state(), self.directory())
        result = self.upgrade()
        self.assertEqual(result['lane'], 'upgrade')
        ao_delegates.validate_preparation(self.directory(), self.state())
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.upgrade(upgrade_authorization='Yet another authorization text')

    # j. CLI main(): optional flags are parsed and forwarded; omitted flags are not forwarded.
    def test_main_forwards_optional_upgrade_flags_only_when_given(self):
        argv = ['ao_executable_binding', '--home', str(self.home), '--room-id', self.room,
                '--request-id', 'cli-upgrade', '--executable-path', str(self.new), '--launch-path', str(self.launch),
                '--database-path', str(self.root / 'fake.db'), '--authorization', 'CLI authorization text',
                '--diagnosis', 'CLI diagnosis text', '--upgrade-authorization', 'CLI upgrade authorization text',
                '--expected-version', '2.1.280']
        with patch.object(sys, 'argv', argv), patch.object(binding, 'bind', return_value={'ok': True}) as mock_bind:
            binding.main()
        kwargs = mock_bind.call_args.kwargs
        self.assertEqual(kwargs.get('upgrade_authorization'), 'CLI upgrade authorization text')
        self.assertEqual(kwargs.get('expected_version'), '2.1.280')

        plain_argv = ['ao_executable_binding', '--home', str(self.home), '--room-id', self.room,
                      '--request-id', 'cli-plain', '--executable-path', str(self.new), '--launch-path', str(self.launch),
                      '--database-path', str(self.root / 'fake.db'), '--authorization', 'CLI authorization text',
                      '--diagnosis', 'CLI diagnosis text']
        with patch.object(sys, 'argv', plain_argv), patch.object(binding, 'bind', return_value={'ok': True}) as mock_bind:
            binding.main()
        kwargs = mock_bind.call_args.kwargs
        self.assertNotIn('upgrade_authorization', kwargs)
        self.assertNotIn('expected_version', kwargs)

    # k. Intact original WITHOUT upgrade args is still refused with the existing 'unchanged' message.
    def test_intact_original_without_upgrade_args_still_refused(self):
        with self.assertRaisesRegex(ao.RoomError, 'Recorded executable is unchanged'):
            self.repair()
