"""Offline fourth-charter allowance contracts; synthetic rooms, native storage and AO only."""

import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_review_extension as extension
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture


class ReviewExtensionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        original_request = self.fake.request

        def raw_request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original_request(method, path, payload)

        self.fake.request = raw_request
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.bind()
        for number in range(1, 4):
            self.agree(key='charter-' + str(number))
        self.implement()
        self.review()
        self.accepted = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.database = self.root / 'synthetic-ao.db'
        binding = self.state()['bindings']['engineer']
        with sqlite3.connect(self.database) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       ('engineer', 'project', 'claude-code', 'chat', 0, 'idle', str(self.repo),
                        'synthetic-native-owner', 'generation-one'))
            db.execute('INSERT INTO conversations VALUES(?,?,?)',
                       (binding['conversation_id'], 'engineer', binding['branch_id']))
            db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                       (binding['branch_id'], binding['conversation_id'], 'synthetic-native-owner', 'engineer', 'native', 0))
        self.target_spec = None

    def register(self):
        if self.target_spec is None:
            self.target_spec = self.service.ao_room_spec_put(self.room, 2,
                'Review the newly authorized validation charter. Preserve the accepted research candidate.',
                self.gates, 'Astra approves this exact next charter under the actual scope decision')
        return self.target_spec

    def audit(self, **changes):
        spec = self.register()
        inputs = {'room_id': self.room, 'spec_revision': 2, 'spec_sha256': spec['sha256'],
                  'retained_candidate_sha256': self.accepted['candidate_sha256'],
                  'native_session_id': 'synthetic-native-owner', 'native_owner_database': str(self.database), **changes}
        return self.service.ao_room_spec_review_extension_audit(**inputs)

    def grant_inputs(self, audit=None, **changes):
        return {'room_id': self.room, 'audit_sha256': (audit or self.audit())['audit_sha256'],
                'authorization': 'User answered "I approve" to the explicit proposal for one additional charter review in this retained room and session.',
                'diagnosis': 'Three prior source/spec review intents remain; the newly approved charter needs one exact review.',
                'request_id': 'one-charter-extension', **changes}

    def grant(self):
        self.inputs = self.grant_inputs()
        return self.service.ao_room_spec_review_extend(**self.inputs)

    def finish_fourth(self):
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact next charter only.', 'findings': [],
                         'decision': 'accept', 'spec_revision': 2, 'spec_sha256': self.target_spec['sha256']}))
        return self.service.ao_room_sync(self.room)

    def test_registration_does_not_consume_or_grant_and_audit_is_observational(self):
        self.register()
        before = (self.directory() / 'state.json').read_bytes()
        posts = len(self.fake.posts)
        audited = self.audit()
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(audited['prior_spec_review_attempts'], 3)
        self.assertEqual(self.state()['acceptances'][-1]['candidate_sha256'], self.accepted['candidate_sha256'])
        self.assertIsNone(self.state()['checkpoint'])
        self.assertNotIn('spec_review_extension', self.state())
        with self.assertRaisesRegex(ao.RoomError, 'Three Fable'):
            self.send('spec_review', 'ungranted-fourth')

    def test_one_grant_preserves_state_and_one_completed_fourth_intent_exhausts_it(self):
        self.register()
        before = self.state()
        granted = self.grant()
        after = self.state()
        self.assertEqual({k: v for k, v in after.items() if k != 'spec_review_extension'}, before)
        self.assertEqual(granted['remaining_spec_reviews'], 1)
        self.assertEqual(self.service.ao_room_spec_review_extend(**self.inputs), granted)
        sent = self.send('spec_review', 'fourth-charter')
        self.assertEqual(sent['state'], 'submitted')
        intent = self.state()['requests']['fourth-charter']
        self.assertEqual(intent['carried']['spec_review_extension_sha256'], granted['receipt_sha256'])
        self.assertNotIn(granted['receipt_sha256'], intent['text'])
        consumption_path = self.directory() / intent['review_extension_consumption']
        consumption = ao.read(consumption_path)
        self.assertEqual(ao.digest(consumption), intent['review_extension_consumption_sha256'])
        self.assertEqual(consumption['request']['request_id'], 'fourth-charter')
        self.assertEqual(consumption['request']['client_message_id'], intent['client_message_id'])
        self.assertEqual(consumption['grant_receipt_sha256'], granted['receipt_sha256'])
        status = self.finish_fourth()
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth-charter')
        self.assertEqual(self.state()['acceptances'], before['acceptances'])
        for request_id, request in before['requests'].items():
            self.assertEqual(self.state()['requests'][request_id], request)
        with self.assertRaisesRegex(ao.RoomError, 'fourth.*exhausted'):
            self.send('spec_review', 'fifth-charter')
        posts = len(self.fake.posts)
        self.assertEqual(self.send('spec_review', 'fourth-charter')['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.service.ao_room_spec_review_extend(**self.inputs)['remaining_spec_reviews'], 0)
        with self.assertRaisesRegex(ao.RoomError, 'cannot renew or renumber'):
            self.service.ao_room_spec_review_extend(**{**self.inputs, 'request_id': 'renumbered-extension'})

    def test_lost_ack_and_failed_native_result_consume_the_single_slot(self):
        self.grant()
        self.fake.lose_ack = True
        first = self.send('spec_review', 'uncertain-fourth')
        self.assertEqual(first['state'], 'uncertain')
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.send('spec_review', 'uncertain-fourth'), first)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'replacement-fourth')
        self.fake.finish('engineer', 'Synthetic failed delivery', state='failed')
        status = self.service.ao_room_sync(self.room)
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.state()['requests']['uncertain-fourth']['state'], 'uncertain')
        self.assertEqual(self.service.ao_room_spec_review_extend(**self.inputs)['remaining_spec_reviews'], 0)

    def test_crash_after_fourth_intent_before_post_stays_consumed_and_never_replays(self):
        self.grant()
        save = self.service.save
        posts = len(self.fake.posts)

        def interrupted_save(directory, state):
            save(directory, state)
            if 'crashed-fourth' in state['requests']:
                raise OSError('synthetic interruption after durable intent and before POST')

        with patch.object(self.service, 'save', side_effect=interrupted_save):
            with self.assertRaises(OSError):
                self.send('spec_review', 'crashed-fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.send('spec_review', 'crashed-fourth')['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'replacement-after-crash')

    def test_deleted_or_altered_undelivered_fourth_intent_cannot_restore_allowance(self):
        self.grant()
        snapshots = copy.deepcopy(self.fake.snapshots)
        original_request = self.fake.request
        attempts = []

        def definite_no_delivery(method, path, payload=None):
            if method == 'POST':
                attempts.append((path, payload))
                raise ao.RoomError('Synthetic transport exception before any native delivery')
            return original_request(method, path, payload)

        self.fake.request = definite_no_delivery
        self.assertEqual(self.send('spec_review', 'undelivered-fourth')['state'], 'uncertain')
        self.assertEqual(self.fake.snapshots, snapshots)
        original = self.state()
        for change in ('delete', 'relabel-purpose', 'rename-request', 'key', 'text', 'client-message-id', 'consumption-pointer'):
            state = copy.deepcopy(original)
            request = state['requests']['undelivered-fourth']
            if change == 'delete':
                del state['requests']['undelivered-fourth']
            elif change == 'relabel-purpose':
                request['purpose'] = 'correction'
            elif change == 'rename-request':
                state['requests']['renamed-fourth'] = state['requests'].pop('undelivered-fourth')
                request['request_id'] = 'renamed-fourth'
            elif change == 'key':
                request['key'] = '0' * 64
            elif change == 'text':
                request['text'] += ' changed'
            elif change == 'client-message-id':
                request['client_message_id'] = 'replacement-client-message-id'
            else:
                request.pop('review_extension_consumption')
            try:
                ao.atomic(self.directory() / 'state.json', state)
                before = (self.directory() / 'state.json').read_bytes()
                with self.subTest(change=change), self.assertRaises(ao.RoomError):
                    self.service.ao_room_status(self.room)
                with self.assertRaises(ao.RoomError):
                    self.send('spec_review', 'replacement-fourth')
                with self.assertRaises(ao.RoomError):
                    self.service.ao_room_spec_review_extend(**self.inputs)
                self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
                self.assertEqual(len(attempts), 1)
            finally:
                ao.atomic(self.directory() / 'state.json', original)
        self.assertEqual(self.send('spec_review', 'undelivered-fourth')['state'], 'uncertain')
        self.assertEqual(len(attempts), 1)

    def test_consumption_receipt_without_state_projection_requires_diagnosis_not_resend(self):
        self.grant()
        save = self.service.save
        posts = len(self.fake.posts)
        snapshots = copy.deepcopy(self.fake.snapshots)

        def interrupted_projection(directory, state):
            if 'orphan-fourth' in state['requests']:
                raise OSError('Synthetic crash after consumption receipt and before state projection')
            save(directory, state)

        with patch.object(self.service, 'save', side_effect=interrupted_projection):
            with self.assertRaises(OSError):
                self.send('spec_review', 'orphan-fourth')
        self.assertNotIn('orphan-fourth', self.state()['requests'])
        self.assertEqual(len(extension._consumptions(self.directory())), 1)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.fake.snapshots, snapshots)
        with self.assertRaisesRegex(ao.RoomError, 'projection is missing; diagnose without resending'):
            self.service.ao_room_status(self.room)
        for request_id in ('orphan-fourth', 'replacement-fourth'):
            with self.subTest(request_id=request_id), self.assertRaisesRegex(ao.RoomError, 'projection is missing'):
                self.send('spec_review', request_id)
        with self.assertRaisesRegex(ao.RoomError, 'projection is missing'):
            self.service.ao_room_spec_review_extend(**self.inputs)
        self.assertEqual(len(self.fake.posts), posts)

    def test_partial_consumption_write_blocks_all_new_intents(self):
        self.grant()
        posts = len(self.fake.posts)

        def interrupted_consumption(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"partial":')
            raise OSError('Synthetic partial consumption write')

        with patch.object(extension, '_store_once', side_effect=interrupted_consumption):
            with self.assertRaises(OSError):
                self.send('spec_review', 'partial-fourth')
        self.assertNotIn('partial-fourth', self.state()['requests'])
        paths = extension._consumptions(self.directory())
        self.assertEqual(len(paths), 1)
        before = paths[0].read_bytes()
        for request_id in ('partial-fourth', 'replacement-fourth'):
            with self.subTest(request_id=request_id), self.assertRaises(ao.RoomError):
                self.send('spec_review', request_id)
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_spec_review_extend(**self.inputs)
        self.assertEqual(paths[0].read_bytes(), before)
        self.assertEqual(len(self.fake.posts), posts)

    def test_missing_or_modified_consumption_evidence_cannot_admit_another_request(self):
        self.grant()
        self.send('spec_review', 'fourth-charter')
        self.finish_fourth()
        path = extension._consumptions(self.directory())[0]
        original = path.read_bytes()
        for change in ('missing', 'modified'):
            try:
                if change == 'missing':
                    path.unlink()
                else:
                    value = ao.read(path)
                    value['request']['key'] = '0' * 64
                    ao.atomic(path, value)
                with self.subTest(change=change), self.assertRaises(ao.RoomError):
                    self.service.ao_room_status(self.room)
                with self.assertRaises(ao.RoomError):
                    self.send('spec_review', 'replacement-fourth')
            finally:
                path.write_bytes(original)

    def test_sync_refuses_corrupt_consumption_or_projection_before_any_evidence_mutation(self):
        self.grant()
        self.send('spec_review', 'sync-fourth')
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact next charter only.', 'findings': [],
                         'decision': 'accept', 'spec_revision': 2, 'spec_sha256': self.target_spec['sha256']}))
        posts = len(self.fake.posts)
        original_state = self.state()
        consumption_path = extension._consumptions(self.directory())[0]
        original_consumption = consumption_path.read_bytes()

        def room_files():
            return {str(path.relative_to(self.directory())): path.read_bytes()
                    for path in self.directory().rglob('*') if path.is_file()}

        for change in ('modified-consumption', 'missing-consumption', 'missing-projection', 'relabeled-projection'):
            try:
                if change == 'modified-consumption':
                    consumption_path.write_text('{}')
                elif change == 'missing-consumption':
                    consumption_path.unlink()
                else:
                    state = copy.deepcopy(original_state)
                    if change == 'missing-projection':
                        del state['requests']['sync-fourth']
                    else:
                        state['requests']['sync-fourth']['purpose'] = 'correction'
                    ao.atomic(self.directory() / 'state.json', state)
                before = room_files()
                with self.subTest(change=change), self.assertRaises(ao.RoomError):
                    self.service.ao_room_sync(self.room)
                self.assertEqual(room_files(), before)
                self.assertEqual(len(self.fake.posts), posts)
            finally:
                ao.atomic(self.directory() / 'state.json', original_state)
                consumption_path.write_bytes(original_consumption)
        status = self.service.ao_room_sync(self.room)
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(self.state()['requests']['sync-fourth']['state'], 'completed')
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_valid_uncertain_fourth_still_syncs_to_completed_without_replay(self):
        self.grant()
        self.fake.lose_ack = True
        self.assertEqual(self.send('spec_review', 'lost-ack-fourth')['state'], 'uncertain')
        posts = len(self.fake.posts)
        status = self.finish_fourth()
        self.assertEqual(self.state()['requests']['lost-ack-fourth']['state'], 'completed')
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.send('spec_review', 'lost-ack-fourth')['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts)

    def test_existing_three_acceptance_attempts_get_no_extra_allowance(self):
        for request_id in ('acceptance-two', 'acceptance-three'):
            self.send('acceptance_review', request_id)
            request = self.state()['requests'][request_id]
            self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'approved', 'review': 'Exact candidate independently checked.'}))
            self.service.ao_room_sync(self.room)
        self.grant()
        self.assertEqual(sum(r['role'] == 'reviewer' for r in self.state()['requests'].values()), 3)
        with self.assertRaisesRegex(ao.RoomError, 'Three review attempts'):
            self.send('acceptance_review', 'acceptance-four')
        self.assertEqual(ao.MAX_REVIEW_ATTEMPTS, 3)

    def test_requires_registered_exact_next_charter_and_retained_accepted_candidate(self):
        self.register()
        for changed in ({'spec_revision': 1}, {'spec_revision': 3}, {'spec_sha256': '0' * 64},
                        {'retained_candidate_sha256': '0' * 64}, {'native_session_id': 'another-native-owner'}):
            with self.subTest(changed=changed), self.assertRaises(ao.RoomError):
                self.audit(**changed)
        self.service.ao_room_spec_put(self.room, 3, 'Another charter.', self.gates, 'Another exact approval')
        with self.assertRaisesRegex(ao.RoomError, 'exact next charter'):
            self.audit()

    def test_changed_candidate_and_original_receipt_block_audit(self):
        original = (self.repo / 'feature.txt').read_bytes()
        (self.repo / 'feature.txt').write_text('changed after acceptance\n')
        with self.assertRaisesRegex(ao.RoomError, 'accepted candidate'):
            self.audit()
        (self.repo / 'feature.txt').write_bytes(original)
        request = self.state()['requests']['charter-1']
        path = self.directory() / request['receipt']
        path.write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'receipt was modified'):
            self.audit()

    def test_active_unknown_and_missing_native_evidence_block_audit(self):
        self.register()
        for change in ('active', 'unknown-controller', 'missing-turn', 'terminated', 'unknown-lifecycle'):
            snapshots, sessions = copy.deepcopy(self.fake.snapshots), copy.deepcopy(self.fake.sessions)
            try:
                if change == 'active':
                    self.fake.snapshots['engineer']['turns'][-1]['state'] = 'running'
                elif change == 'unknown-controller':
                    self.fake.snapshots['engineer'].pop('controller')
                elif change == 'missing-turn':
                    self.fake.snapshots['engineer']['turns'].pop(0)
                elif change == 'terminated':
                    self.fake.sessions['engineer']['isTerminated'] = True
                else:
                    self.fake.sessions['engineer'].pop('isTerminated')
                with self.subTest(change=change), self.assertRaises(ao.RoomError):
                    self.audit()
            finally:
                self.fake.snapshots, self.fake.sessions = snapshots, sessions
        state = self.state()
        state['requests']['charter-1']['state'] = 'uncertain'
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.audit()

    def test_stale_audit_cannot_be_committed(self):
        inputs = self.grant_inputs()
        self.fake.snapshots['engineer']['messages'][0]['text'] += ' changed'
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_spec_review_extend(**inputs)
        self.assertNotIn('spec_review_extension', self.state())
        self.assertFalse(extension._pending(self.directory()))

    def test_activity_only_drift_after_audit_blocks_grant(self):
        self.fake.snapshots['engineer']['activities'] = [{'id': 'earlier-activity', 'turnId': 'turn-1',
            'activityKind': 'system', 'status': 'completed', 'detail': 'Retained earlier activity'}]
        inputs = self.grant_inputs()
        posts = len(self.fake.posts)
        self.fake.snapshots['engineer']['activities'][0]['detail'] = 'Different earlier activity'
        with self.assertRaisesRegex(ao.RoomError, 'audit is stale'):
            self.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('spec_review_extension', self.state())

    def test_activity_only_drift_after_grant_blocks_fourth_intent(self):
        self.fake.snapshots['engineer']['activities'] = [{'id': 'earlier-activity', 'turnId': 'turn-1',
            'activityKind': 'system', 'status': 'completed', 'detail': 'Retained earlier activity'}]
        self.grant()
        posts = len(self.fake.posts)
        self.fake.snapshots['engineer']['activities'][0]['detail'] = 'Different earlier activity'
        with self.assertRaisesRegex(ao.RoomError, 'Native history or metadata changed'):
            self.send('spec_review', 'changed-activity-fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('changed-activity-fourth', self.state()['requests'])

    def test_identical_grant_reconciles_receipt_then_state_crash_without_a_model_call(self):
        inputs = self.grant_inputs()
        before = (self.directory() / 'state.json').read_bytes()
        posts = len(self.fake.posts)
        with patch.object(self.service, 'save', side_effect=OSError('synthetic state write interruption')):
            with self.assertRaises(OSError):
                self.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        pending = extension._pending(self.directory())
        self.assertEqual(len(pending), 1)
        receipt_bytes = pending[0].read_bytes()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['state'], 'pending_uncommitted')
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted review-extension'):
            self.send('spec_review', 'cannot-dispatch')
        with self.assertRaisesRegex(ao.RoomError, 'another payload'):
            self.service.ao_room_spec_review_extend(**{**inputs, 'request_id': 'other-grant'})
        result = self.service.ao_room_spec_review_extend(**inputs)
        self.assertEqual(result['remaining_spec_reviews'], 1)
        self.assertEqual(pending[0].read_bytes(), receipt_bytes)
        self.assertEqual(len(self.fake.posts), posts)

    def test_pending_receipt_tampering_refuses_reconciliation(self):
        inputs = self.grant_inputs()
        with patch.object(self.service, 'save', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                self.service.ao_room_spec_review_extend(**inputs)
        path = extension._pending(self.directory())[0]
        record = ao.read(path); record['maximum_spec_review_attempts'] = 5
        ao.atomic(path, record)
        with self.assertRaisesRegex(ao.RoomError, 'recomputed grant'):
            self.service.ao_room_spec_review_extend(**inputs)
        self.assertNotIn('spec_review_extension', self.state())

    def test_committed_receipt_audit_and_retained_file_tampering_block_without_mutation(self):
        self.grant()
        pointer = self.state()['spec_review_extension']
        paths = [self.directory() / pointer['receipt'],
                 self.directory() / extension.BASE / 'audits' / (self.inputs['audit_sha256'] + '.json'),
                 self.directory() / self.state()['requests']['charter-1']['receipt']]
        for path in paths:
            original = path.read_bytes()
            try:
                path.write_text('{}')
                state_bytes = (self.directory() / 'state.json').read_bytes()
                with self.subTest(path=path.name), self.assertRaises(ao.RoomError):
                    self.service.ao_room_status(self.room)
                with self.assertRaises(ao.RoomError):
                    self.send('spec_review', 'blocked-fourth')
                self.assertEqual((self.directory() / 'state.json').read_bytes(), state_bytes)
            finally:
                path.write_bytes(original)

    def test_prior_requests_counters_metadata_and_grant_identity_cannot_be_rewritten(self):
        self.grant()
        original = self.state()
        for change in ('delete-attempt', 'usage', 'renumber-attempt', 'grant-id', 'effort', 'acceptance'):
            state = copy.deepcopy(original)
            if change == 'delete-attempt':
                del state['requests']['charter-1']
            elif change == 'usage':
                state['requests']['charter-1']['usage'] = {'known': False, 'reason': 'reset'}
            elif change == 'renumber-attempt':
                state['requests']['charter-1']['created_order'] = 99
            elif change == 'grant-id':
                state['spec_review_extension']['request_id'] = 'new-round'
            elif change == 'effort':
                state['bindings']['engineer']['reasoning_effort'] = 'low'
            else:
                state['acceptances'].clear()
            try:
                ao.atomic(self.directory() / 'state.json', state)
                with self.subTest(change=change), self.assertRaises(ao.RoomError):
                    self.send('spec_review', 'blocked-fourth')
            finally:
                ao.atomic(self.directory() / 'state.json', original)

    def test_changed_charter_native_owner_and_history_cannot_use_committed_grant(self):
        self.grant()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='replacement'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='replacement'")
        with self.assertRaisesRegex(ao.RoomError, 'Native owner differs'):
            self.send('spec_review', 'wrong-native')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='synthetic-native-owner'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='synthetic-native-owner'")
        self.fake.snapshots['engineer']['messages'][0]['text'] += ' changed'
        with self.assertRaises(ao.RoomError):
            self.send('spec_review', 'wrong-history')
        self.fake.snapshots['engineer']['messages'][0]['text'] = self.state()['requests']['charter-1']['text']
        self.service.ao_room_spec_put(self.room, 3, 'Another authorized scope.', self.gates, 'Another exact approval')
        with self.assertRaisesRegex(ao.RoomError, 'exact audited next charter'):
            self.send('spec_review', 'wrong-spec')

    def test_known_semantic_hold_is_preserved_and_still_blocks_dispatch(self):
        self.register()
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'quota_limit'}
        self.service.ao_room_sync(self.room)
        original = self.state()['requests']['implementation']
        self.assertTrue(original['semantic_status']['hold'])
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: quota_limit'):
            self.send('spec_review', 'fourth-on-hold')
        self.assertEqual(self.state()['requests']['implementation'], original)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def test_existing_provider_gate_still_runs_after_extension_admission(self):
        self.grant()
        # A controlled guard rejection proves the extension does not bypass the
        # existing provider-validation path that follows review admission.
        with patch.object(ao_workflow.ao_delegates, 'validate_provider', side_effect=ao.RoomError('pinned provider hold')):
            with self.assertRaisesRegex(ao.RoomError, 'pinned provider hold'):
                self.send('spec_review', 'held-fourth')
        self.assertNotIn('held-fourth', self.state()['requests'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def test_mcp_callable_interfaces_require_explicit_complete_inputs(self):
        self.register()
        controller = project_room.Service(self.home)
        listed = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, controller)
        names = {item['name'] for item in listed['result']['tools']}
        self.assertIn('ao_room_spec_review_extension_audit', names)
        self.assertIn('ao_room_spec_review_extend', names)
        with patch.object(ao, 'Service', return_value=self.service):
            with self.assertRaisesRegex(ao.RoomError, 'Invalid arguments'):
                controller.call('ao_room_spec_review_extend', {'room_id': self.room})
            inputs = self.grant_inputs()
            response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'ao_room_spec_review_extend', 'arguments': inputs}}, controller)
        self.assertFalse(response['result']['isError'])
        self.assertEqual(response['result']['structuredContent']['remaining_spec_reviews'], 1)


if __name__ == '__main__':
    unittest.main()
