"""Executable replacement preserves preparation/history; no real CLI or AO calls."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import ao_delegates
import ao_executable_binding as binding
import ao_project_room as ao
import ao_routing
from test_ao_normal import Fixture


class ExecutableBindingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.old = self.root / 'old-claude'
        self.new = self.root / 'new-claude'
        for path, version in ((self.old, 'old'), (self.new, 'new')):
            path.write_text('#!/bin/sh\nprintf "' + version + ' (Claude Code)\\n"\n')
            path.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.old), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open(); self.spec(); self.bind()
        self.prepared = ao_delegates.preparation(self.directory(), self.state())
        self.prep_bytes = (self.directory() / self.state()['preparation']).read_bytes()
        engineer = self.state()['bindings']['engineer']
        self.owner = {'id': 'engineer', 'project_id': 'project', 'workspace_path': str(self.repo),
            'ao_conversation_id': engineer['conversation_id'], 'active_branch_id': engineer['branch_id'],
            'provider_conversation_id': 'native-uuid', 'controller_generation': 'generation-1',
            'activity_state': 'exited', 'strategy': 'native'}
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
        self.old.unlink()
        self.args = dict(room_id=self.room, request_id='repair-1', executable_path=str(self.new),
            launch_path=str(self.launch), database_path=str(self.root / 'fake.db'),
            authorization='User authorized repair of this saved session', diagnosis='Previously pinned executable removed by an app update')

    def repair(self, **changes):
        return binding.bind(self.service, **{**self.args, **changes})

    def test_preserves_original_state_and_preparation_and_no_post(self):
        before = self.state(); result = self.repair(); after = self.state()
        self.assertFalse(result['model_dispatch'])
        self.assertEqual({k: v for k, v in after.items() if k != 'executable_binding'}, before)
        self.assertEqual((self.directory() / after['preparation']).read_bytes(), self.prep_bytes)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(ao_delegates.validate_preparation(self.directory(), after), self.prepared)
        status = self.service.ao_room_status(self.room)['delegate']['routing']
        self.assertEqual(status['claude']['path'], str(self.new))
        self.assertEqual(status['original_claude']['path'], str(self.old))
        self.assertNotEqual(status['status'], 'unverified')

    def test_real_guard_and_file_checks_still_apply(self):
        self.repair()
        path = self.repo / '.claude/settings.local.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ao.RoomError, 'Pinned routing file changed'):
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_target_content_drift_even_same_size_and_mtime_is_refused(self):
        self.repair(); stamp = self.new.stat(); raw = self.new.read_bytes()
        self.new.write_bytes(raw.replace(b'new', b'bad'))
        os.utime(self.new, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # synthetic attack fixture only
        with self.assertRaisesRegex(ao.RoomError, 'executable changed'):
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_launch_path_drift_is_refused(self):
        self.repair(); self.launch.unlink(); self.launch.symlink_to(self.root / 'missing')
        with self.assertRaises((ao.RoomError, FileNotFoundError)):
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_idempotent_and_different_inputs_refused(self):
        first = self.repair(); before = (self.directory() / 'state.json').read_bytes()
        self.assertTrue(self.repair()['idempotent'])
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.repair(diagnosis='A different diagnosis')
        self.assertEqual(self.state()['executable_binding']['sha256'], first['sha256'])

    def test_missing_intent_and_pointer_rollback_refused(self):
        self.repair(); state = self.state(); state.pop('executable_binding')
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_routing.validate_local(self.prepared, state, self.directory())

    def test_native_owner_preserved_across_controller_reload(self):
        self.repair(); self.owner.update(controller_generation='generation-2', activity_state='idle')
        ao_delegates.validate_preparation(self.directory(), self.state())
        self.owner['provider_conversation_id'] = 'replacement-session'
        with self.assertRaisesRegex(ao.RoomError, 'native owner changed'):
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_stopped_owner_required_and_unknown_history_refused(self):
        self.fake.snapshots['engineer']['controller'] = 'ready'
        with self.assertRaisesRegex(ao.RoomError, 'Stop the idle'):
            self.repair()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.fake.snapshots['engineer']['turns'] = [{'id': 'active', 'state': 'running'}]
        with self.assertRaisesRegex(ao.RoomError, 'Unsettled native'):
            self.repair()
        self.fake.snapshots['engineer']['hasMoreBefore'] = None
        with self.assertRaisesRegex(ao.RoomError, 'explicit complete'):
            self.repair()

    def test_original_failure_and_recovered_context_are_only_preserved(self):
        self.fake.snapshots['engineer']['turns'] = [{'id': 'context', 'state': 'recovered'}, {'id': 'failure', 'state': 'failed'}]
        before = copy.deepcopy(self.fake.snapshots['engineer'])
        self.repair()
        self.assertEqual(self.fake.snapshots['engineer'], before)
        self.assertNotIn('outcome_resume', self.state())
        self.assertEqual(self.fake.posts, [])

    def test_healthy_original_and_unsafe_target_refused(self):
        self.old.write_text('#!/bin/sh\nprintf "old (Claude Code)\\n"\n'); self.old.chmod(0o700)
        original = self.prepared['routing']['claude']
        os.utime(self.old, ns=(original['mtime_ns'], original['mtime_ns']))  # reconstruct synthetic fixture
        with self.assertRaisesRegex(ao.RoomError, 'unchanged'):
            self.repair()
        self.old.unlink(); self.new.chmod(0o777)
        with self.assertRaisesRegex(ao.RoomError, 'unsafe'):
            self.repair()

    def test_empty_authorization_and_wrong_owner_refused(self):
        with self.assertRaises(ao.RoomError):
            self.repair(authorization='')
        self.owner['active_branch_id'] = 'unrelated'
        with self.assertRaisesRegex(ao.RoomError, 'Native owner'):
            self.repair()

    def test_crash_after_intent_before_state_can_reconcile_exact_request(self):
        before = self.state()
        with patch.object(self.service, 'save', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError): self.repair()
        self.assertEqual(self.state(), before)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_routing.validate_local(self.prepared, self.state(), self.directory())
        self.repair()
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_second_missing_executable_extends_chain_without_edits(self):
        self.repair(); first = (self.directory() / 'executable-bindings/repair-1.json').read_bytes()
        third = self.root / 'third-claude'; third.write_bytes(self.new.read_bytes()); third.chmod(0o700)
        self.new.unlink(); self.launch.unlink(); self.launch.symlink_to(third)
        self.repair(request_id='repair-2', executable_path=str(third))
        self.assertEqual((self.directory() / 'executable-bindings/repair-1.json').read_bytes(), first)
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_malformed_pointer_reports_unverified_instead_of_crashing_status(self):
        self.repair(); state = self.state(); state['executable_binding'] = ['malformed']
        ao.atomic(self.directory() / 'state.json', state)
        status = self.service.ao_room_status(self.room)['delegate']['routing']
        self.assertEqual(status['status'], 'unverified')
        self.assertIn('Malformed executable binding pointer', status['error'])

    def test_preparation_transition_is_not_implicitly_accepted(self):
        self.repair(); state = self.state(); altered = copy.deepcopy(self.prepared)
        altered['epoch'] = 'another-preparation'
        state['preparation_sha256'] = ao.digest(altered)
        with self.assertRaisesRegex(ao.RoomError, 'unchanged engineer/preparation'):
            ao_routing.validate_local(altered, state, self.directory())
