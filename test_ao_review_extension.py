"""Offline fourth-charter allowance contracts; synthetic rooms, native storage and AO only."""

import copy
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_review_extension as extension
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture
from test_ao_adoption import AdoptionFixture


class ReviewExtensionFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.configure_runtime()
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
        self.make_database()
        self.target_spec = None

    def make_database(self):
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
    def configure_runtime(self):
        pass

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


class ReviewExtensionTests(ReviewExtensionFixture):
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

    def test_two_role_native_audit_scopes_engineer_compaction_after_charter_revision(self):
        import ao_outcomes
        self.register()
        state = self.state()
        # Construct an already verified compaction observation in this synthetic
        # fixture. The actual record/receipt validator remains active below.
        request = ao_outcomes.latest_for_role(state, 'engineer')
        record = ao.read(self.directory() / request['semantic_outcome'])
        source = {'session_id': 'engineer', 'native_session_id': 'synthetic-native-owner',
                  'database': str(self.database), 'transcript': str(self.root / 'synthetic-native-owner.jsonl')}
        turn = {'id': 'compacted-turn', 'providerTurnId': 'acp-history-turn:synthetic', 'state': 'recovered'}
        message = {'id': 'compaction-message', 'turnId': turn['id'], 'role': 'user',
                   'text': 'Synthetic retained compacted context.', 'origin': 'human', 'streaming': False, 'sequence': 100}
        proof = {'turn_id': turn['id'], 'provider_turn_id': turn['providerTurnId'], 'message_id': message['id'],
                 'native_uuid': 'synthetic-compaction-uuid', 'text_sha256': ao.digest(message['text'].encode()),
                 'turn_sha256': ao.digest(turn), 'messages_sha256': ao.digest([message])}
        record['native'] = {'source': source, 'compaction_imports': [proof]}
        sha256 = ao.digest(record)
        relative = 'outcomes/' + request['request_id'] + '/' + sha256 + '.json'
        ao.atomic(self.directory() / relative, record)
        request.update(semantic_outcome=relative, semantic_outcome_sha256=sha256)
        state['native_outcome_source'] = source
        state['provider_transition'] = {'epoch': 2}  # Select the pure native-proof path; no epoch mutation or lifecycle call.
        engineer = self.fake.conversation('engineer')
        engineer['turns'].append(turn)
        engineer['messages'].append(message)
        reviewer = self.fake.conversation('reviewer')
        before = {str(p.relative_to(self.directory())): p.read_bytes()
                  for p in self.directory().rglob('*') if p.is_file()}
        self.assertEqual(ao_outcomes.known_compaction_turns(self.directory(), state, engineer), {turn['id']})
        self.assertEqual(ao_outcomes.known_compaction_turns(self.directory(), state, reviewer), set())
        for role, snapshot in (('engineer', engineer), ('reviewer', reviewer)):
            native = extension._native_evidence(self.directory(), state, state['bindings'][role], snapshot)
            self.assertEqual(native['session_id'], state['bindings'][role]['session_id'])
        # Changing only the engineer's verified import still refuses in its
        # owning session, while another session cannot borrow that exemption.
        for field in ('messages', 'turns'):
            changed = copy.deepcopy(engineer)
            changed[field][-1]['changed'] = True
            with self.subTest(field=field), self.assertRaisesRegex(ao.RoomError, 'Verified native compaction import changed'):
                extension._native_evidence(self.directory(), state, state['bindings']['engineer'], changed)
        for session_id in ('reviewer', 'another-engineer', None):
            snapshot = {**engineer, 'sessionId': session_id}
            with self.subTest(session_id=session_id):
                self.assertEqual(ao_outcomes.known_compaction_turns(self.directory(), state, snapshot), set())
        reviewer['turns'].append(copy.deepcopy(turn))
        reviewer['messages'].append(copy.deepcopy(message))
        for outcome in ('recovered', 'failed'):
            reviewer['turns'][-1]['state'] = outcome
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ao.RoomError, 'not completed'):
                extension._native_evidence(self.directory(), state, state['bindings']['reviewer'], reviewer)
        from ao_provider_transition import _CompleteClient, _ReadOnlyIdentity
        for role in ('engineer', 'reviewer'):
            for session_id in ('different-session', None):
                original = self.fake.snapshots[role]['sessionId']
                try:
                    self.fake.snapshots[role]['sessionId'] = session_id
                    with self.subTest(role=role, session_id=session_id), self.assertRaises(ao.RoomError):
                        _ReadOnlyIdentity(self.service).identity(_CompleteClient(self.fake), state, state['bindings'][role])
                finally:
                    self.fake.snapshots[role]['sessionId'] = original
        path = self.directory() / relative
        original = path.read_bytes()
        try:
            path.write_text('{}')
            with self.assertRaises(ao.RoomError):
                extension._native_evidence(self.directory(), state, state['bindings']['engineer'], engineer)
        finally:
            path.write_bytes(original)
        after = {str(p.relative_to(self.directory())): p.read_bytes()
                 for p in self.directory().rglob('*') if p.is_file()}
        self.assertEqual(after, before)

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
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'unused fourth-review grant'):
            self.service.ao_room_spec_put(self.room, 3, 'Another authorized scope.', self.gates, 'Another exact approval')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

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


class ExtensionEvolutionTests(ReviewExtensionFixture):
    def room_files(self):
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}

    def stage(self, key, message):
        return self.service.ao_room_instruction_stage(self.room, key, message, 'Actual authorization for this new operating instruction')

    def test_authorized_amendments_append_before_and_after_fourth_completion(self):
        first = self.stage('original-instruction', 'Keep each observation explicit.')
        original = (self.directory() / first['path']).read_bytes()
        grant = self.grant()
        second = self.stage('after-grant', 'Name the observed limitation.')
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        self.send('spec_review', 'fourth')
        self.assertIn('Name the observed limitation.', self.state()['requests']['fourth']['text'])
        self.finish_fourth()
        self.stage('after-completion', 'Keep the next verification bounded.')
        self.service.quiet(self.state())
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.state()['spec_review_extension']['receipt_sha256'], grant['receipt_sha256'])
        self.assertEqual((self.directory() / first['path']).read_bytes(), original)
        self.assertEqual(self.state()['instruction_amendments'][:2],
                         [{'path': first['path'], 'sha256': first['sha256']}, {'path': second['path'], 'sha256': second['sha256']}])
        with self.assertRaisesRegex(ao.RoomError, 'fourth.*exhausted'):
            self.send('spec_review', 'fifth')
        state = self.state(); state['instruction_amendments'].pop(0)
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'original instruction amendments'):
            self.service.ao_room_status(self.room)

    def write_native(self, folder):
        import ao_outcomes
        request = ao_outcomes.latest_for_role(self.state(), 'engineer')
        folder.mkdir(exist_ok=True)
        path = folder / 'synthetic-native-owner.jsonl'
        rows = [{'type': 'user', 'sessionId': 'synthetic-native-owner', 'uuid': 'synthetic-human',
                 'timestamp': datetime.fromtimestamp(request['created_at'] + 1, timezone.utc).isoformat(),
                 'origin': {'kind': 'human'}, 'message': {'content': request['text']}},
                {'type': 'assistant', 'sessionId': 'synthetic-native-owner', 'uuid': 'synthetic-assistant',
                 'timestamp': datetime.fromtimestamp(request['created_at'] + 2, timezone.utc).isoformat(),
                 'message': {'id': 'synthetic-final', 'model': ao_workflow.FABLE_MODEL, 'stop_reason': 'end_turn'}}]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        return path

    def test_first_and_moved_native_source_remain_auditable_without_reopening_review(self):
        self.grant()
        path = self.write_native(self.root / 'native-first')
        self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(path))
        first = self.state()['native_outcome_source_evidence']
        original = (self.directory() / first['path']).read_bytes()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        moved_db = self.root / 'moved-ao.db'; shutil.copyfile(self.database, moved_db); self.database.unlink()
        moved_folder = self.root / 'native-moved'; moved_folder.mkdir()
        moved_path = moved_folder / path.name; path.rename(moved_path)
        self.service.ao_room_outcome_audit(self.room, ao_database_path=str(moved_db), native_transcript_path=str(moved_path))
        self.assertNotEqual(self.state()['native_outcome_source_evidence'], first)
        self.assertEqual((self.directory() / first['path']).read_bytes(), original)
        self.send('spec_review', 'fourth')
        # A later ordinary observation can have incomplete native-source evidence;
        # the saved source audit remains independently verifiable and sync stays usable.
        self.finish_fourth()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        current_native = self.write_native(moved_folder)
        self.service.ao_room_outcome_audit(self.room, ao_database_path=str(moved_db), native_transcript_path=str(current_native))
        self.service.quiet(self.state())
        self.assertEqual((self.directory() / first['path']).read_bytes(), original)
        pointer = self.state()['native_outcome_source_evidence']
        (self.directory() / pointer['path']).write_text('{}')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_status(self.room)

    def test_changed_native_owner_source_refuses_before_any_room_write(self):
        self.grant()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='replacement-native'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='replacement-native'")
        before = self.room_files()
        with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
            self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database),
                                             native_transcript_path=str(self.root / 'replacement-native.jsonl'))
        self.assertEqual(self.room_files(), before)

    def moved_source_hold(self, kind):
        self.grant()
        snapshot = self.fake.snapshots['engineer']
        if kind == 'quota_limit':
            snapshot['turns'][-1]['error'] = {'type': 'rate_limit'}
        else:
            snapshot['sessionFailures'] = [{'type': 'unclassified'}]
        self.service.ao_room_sync(self.room)
        original_request = self.state()['requests']['implementation']
        original_outcome = (self.directory() / original_request['semantic_outcome']).read_bytes()
        path = self.write_native(self.root / 'native-first')
        first = self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(path))
        self.assertEqual(first['outcome']['kind'], kind)
        if kind == 'quota_limit':
            self.service.ao_room_outcome_resume(self.room, 'implementation', first['outcome_sha256'], 'fourth-after-source',
                'The original correlated quota stop was diagnosed.', 'Actual approval for only the named continuation.')
        previous = self.state()['requests']['implementation']
        release_bytes = ((self.directory() / previous['outcome_resume']).read_bytes() if previous.get('outcome_resume') else None)
        snapshot['turns'][-1].pop('error', None)
        snapshot['sessionFailures'] = []
        moved_folder = self.root / 'native-moved'; moved_folder.mkdir()
        moved_path = moved_folder / path.name; path.rename(moved_path)
        moved = self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(moved_path))
        current = self.state()['requests']['implementation']
        self.assertEqual(moved['outcome'], first['outcome'])
        self.assertTrue(moved['outcome']['hold'])
        self.assertNotEqual(moved['outcome_sha256'], first['outcome_sha256'])
        self.assertEqual(current.get('outcome_resume'), previous.get('outcome_resume'))
        self.assertEqual(current.get('outcome_resume_sha256'), previous.get('outcome_resume_sha256'))
        if release_bytes is not None:
            self.assertEqual((self.directory() / current['outcome_resume']).read_bytes(), release_bytes)
        self.assertEqual((self.directory() / original_request['semantic_outcome']).read_bytes(), original_outcome)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: ' + kind):
            self.send('spec_review', 'fourth-after-source')
        self.assertNotIn('fourth-after-source', self.state()['requests'])
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        return moved

    def test_source_move_preserves_quota_hold_and_cannot_renew_named_continuation(self):
        self.moved_source_hold('quota_limit')

    def test_source_move_preserves_unknown_hold_until_separate_exact_outcome_audit(self):
        self.moved_source_hold('unknown')
        # The existing no-path diagnostic audit retains its separate authority to
        # clear an unknown outcome after the exact owned evidence becomes complete.
        audited = self.service.ao_room_outcome_audit(self.room)
        self.assertFalse(audited['outcome']['hold'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def known_hold_through_unknown(self, ambiguity):
        from pathlib import Path
        self.moved_source_hold('quota_limit')
        source = self.state()['native_outcome_source']
        snapshot = self.fake.snapshots['engineer']
        path = Path(source['transcript']); raw = path.read_bytes()
        before = self.state()['requests']['implementation']
        old_release = (self.directory() / before['outcome_resume']).read_bytes()
        if ambiguity == 'ao':
            snapshot['sessionFailures'] = [{'type': 'unclassified'}]
            uncertain = self.service.ao_room_outcome_audit(self.room, ao_database_path=source['database'],
                                                         native_transcript_path=source['transcript'])
        else:
            path.unlink()
            uncertain = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(uncertain['outcome']['kind'], 'quota_limit')
        self.assertEqual(uncertain['observation_outcome']['kind'], 'unknown')
        self.assertFalse(uncertain['resume_eligible'])
        self.assertNotEqual(uncertain['outcome_sha256'], before['semantic_outcome_sha256'])
        unknown_request = self.state()['requests']['implementation']
        unknown_bytes = (self.directory() / unknown_request['semantic_outcome']).read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'eligible diagnosed failure'):
            self.service.ao_room_outcome_resume(self.room, 'implementation', uncertain['outcome_sha256'], 'fourth-after-source',
                'Unknown evidence is not a diagnosis.', 'Synthetic approval cannot bypass ambiguous evidence.')
        snapshot['sessionFailures'] = []
        if not path.exists(): path.write_bytes(raw)
        # A quiet automatic observation retains the unresolved diagnosis; only
        # an explicit healthy audit can update it, while preserving known quota.
        self.service.ao_room_sync(self.room)
        automatic = self.state()['requests']['implementation']
        self.assertEqual(ao.read(self.directory() / automatic['semantic_outcome'])['observation_outcome']['kind'], 'unknown')
        healthy = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(healthy['outcome']['kind'], 'quota_limit')
        self.assertTrue(healthy['outcome']['hold'])
        self.assertIsNone(healthy['observation_outcome'])
        self.assertTrue(healthy['resume_eligible'])
        self.assertIn('resolved_observation_sha256', ao.read(self.directory() / self.state()['requests']['implementation']['semantic_outcome']))
        self.assertEqual((self.directory() / before['outcome_resume']).read_bytes(), old_release)
        self.assertEqual((self.directory() / unknown_request['semantic_outcome']).read_bytes(), unknown_bytes)
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: quota_limit'):
            self.send('spec_review', 'unnamed-after-unknown')
        self.service.ao_room_outcome_resume(self.room, 'implementation', healthy['outcome_sha256'], 'fourth-after-source',
            'Complete evidence now resolves the ambiguity; original quota stop remains.',
            'Actual fresh approval for the same named continuation and this exact outcome.')
        self.assertEqual(self.send('spec_review', 'fourth-after-source')['state'], 'submitted')
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_known_quota_survives_ao_unknown_and_healthy_source_audits(self):
        self.known_hold_through_unknown('ao')

    def test_known_quota_survives_native_unknown_and_healthy_explicit_audit(self):
        self.known_hold_through_unknown('native')

    def test_reappearing_known_error_cannot_reactivate_previously_invalidated_release(self):
        from ao_outcomes import gate
        self.grant()
        snapshot = self.fake.snapshots['engineer']
        snapshot['turns'][-1]['error'] = {'type': 'rate_limit'}
        first = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(self.room, 'implementation', first['outcome_sha256'], 'named-successor',
            'Original quota failure diagnosis.', 'Actual approval for this single named successor.')
        previous = self.state()['requests']['implementation']
        original_release_path = previous['outcome_resume']
        release_bytes = (self.directory() / previous['outcome_resume']).read_bytes()
        for cycle in (1, 2):
            snapshot['turns'][-1].pop('error')
            snapshot['sessionFailures'] = [{'type': 'unclassified'}]
            self.service.ao_room_sync(self.room)
            snapshot['sessionFailures'] = []
            snapshot['turns'][-1]['error'] = {'type': 'rate_limit'}
            state = self.state()
            with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: quota_limit'):
                gate(self.service, self.directory(), state, 'engineer', 'named-successor', snapshot)
            ambiguous = self.state()['requests']['implementation']
            self.assertNotEqual(ambiguous['semantic_outcome_sha256'], previous['semantic_outcome_sha256'])
            audit = self.service.ao_room_outcome_audit(self.room)
            self.assertEqual(audit['outcome']['kind'], 'quota_limit')
            self.assertTrue(audit['resume_eligible'])
            self.assertNotEqual(audit['outcome_sha256'], previous['semantic_outcome_sha256'])
            with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: quota_limit'):
                gate(self.service, self.directory(), self.state(), 'engineer', 'named-successor', snapshot)
            self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'], 'named-successor',
                'Explicit resolution of uncertainty cycle ' + str(cycle), 'Actual fresh approval for the exact current outcome.')
            current = self.state()['requests']['implementation']
            current_hash = current['semantic_outcome_sha256']
            gate(self.service, self.directory(), self.state(), 'engineer', 'named-successor', snapshot)
            self.assertEqual(self.state()['requests']['implementation']['semantic_outcome_sha256'], current_hash)
            previous = current
        self.assertEqual((self.directory() / original_release_path).read_bytes(), release_bytes)
        self.assertNotEqual(self.state()['requests']['implementation']['outcome_resume'], original_release_path)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def test_provider_and_routing_replacement_refuse_before_intents_and_used_reviewer_stays_used(self):
        self.grant()
        actions = [lambda: self.service.ao_room_provider_transition(self.room, '0' * 64, 'Diagnosed', 'Actual approval', 'transition', 'Stopped'),
                   lambda: self.service.ao_room_routing_adoption_stage(self.room, '0' * 64, 'Actual approval', 'Diagnosed', 'adoption')]
        for action in actions:
            before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
            posts = copy.deepcopy(self.fake.posts)
            with self.assertRaisesRegex(ao.RoomError, 'unsupported after that grant'):
                action()
            self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
            self.assertEqual(self.fake.posts, posts)
        before = self.room_files()
        with self.assertRaisesRegex(ao.RoomError, 'reviewer was already used'):
            self.service.ao_room_reviewer_recovery_audit(self.room)
        self.assertEqual(self.room_files(), before)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def test_pending_grant_blocks_routing_writes_and_identical_grant_still_reconciles(self):
        inputs = self.grant_inputs()
        with patch.object(self.service, 'save', side_effect=OSError('Synthetic grant projection interruption')):
            with self.assertRaises(OSError): self.service.ao_room_spec_review_extend(**inputs)
        actions = [lambda: self.service.ao_room_routing_adoption_audit(self.room),
                   lambda: self.service.ao_room_routing_adoption_stage(self.room, '0' * 64, 'Actual approval', 'Diagnosed', 'adoption'),
                   lambda: self.service.ao_room_routing_adoption_activate(self.room, 'adoption')]
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        posts = copy.deepcopy(self.fake.posts)
        for action in actions:
            with self.assertRaisesRegex(ao.RoomError, 'uncommitted review-extension receipt'):
                action()
            self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
            self.assertEqual(self.fake.posts, posts)
        self.assertEqual(self.service.ao_room_spec_review_extend(**inputs)['remaining_spec_reviews'], 1)

    def test_complete_two_role_audit_overflow_refuses_before_publication(self):
        self.register()
        for role in ('engineer', 'reviewer'):
            self.fake.snapshots[role]['activities'].append({'id': role + '-large-observation',
                'activityKind': 'tool', 'detail': '🧭' * 15_000, 'status': 'completed'})
        before = self.room_files()
        with patch.object(extension, 'MAX_RECORD_BYTES', 100_000):
            with self.assertRaisesRegex(ao.RoomError, 'exceeds its readable size bound'):
                self.audit()
        self.assertEqual(self.room_files(), before)

    def test_exact_serialized_audit_bound_retains_full_unicode_snapshots_and_remains_readable(self):
        self.register()
        self.fake.snapshots['reviewer']['activities'].append({'id': 'unicode-observation', 'detail': '🧭' * 128})
        with patch.object(extension.time, 'time', return_value=123.25):
            first = self.audit()
            path = self.directory() / extension.BASE / 'audits' / (first['audit_sha256'] + '.json')
            raw = path.read_bytes(); path.unlink()
            with patch.object(extension, 'MAX_RECORD_BYTES', len(raw)):
                second = self.audit()
                saved = extension._audit(self.directory(), second['audit_sha256'])
                self.assertEqual(saved['observed_snapshots']['reviewer']['activities'], self.fake.snapshots['reviewer']['activities'])
                self.assertEqual(path.read_bytes(), raw)
                self.service.ao_room_spec_review_extend(**self.grant_inputs(second))


class ExtensionExecutableEvolutionTests(ReviewExtensionFixture):
    def configure_runtime(self):
        self.original_executable = self.root / 'original-claude'
        self.original_executable.write_text('#!/bin/sh\nprintf "original (Claude Code)\\n"\n')
        self.original_executable.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.original_executable), 'claude_config_dir': str(self.claude_env)})

    def test_orphan_repair_blocks_fresh_audit_and_old_audit_grant_until_exact_reconciliation(self):
        import ao_executable_binding as executable
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.register()
        self.original_executable.unlink()
        # A missing original with no journal remains auditable. This checks the
        # immutable journal, not the live executable fingerprint.
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        inputs = self.grant_inputs(audited)
        target = self.root / 'orphan-repair-target'
        target.write_text('#!/bin/sh\nprintf "replacement (Claude Code)\\n"\n'); target.chmod(0o700)
        launch = self.root / 'orphan-repair-launch'; launch.symlink_to(target)
        args = (self.service, self.room, 'orphan-repair', str(target), str(launch), str(self.database),
                'Actual repair approval preserves the retained owner.', 'Original executable is missing')
        before_state = (self.directory() / 'state.json').read_bytes()
        with patch.object(self.service, 'save', side_effect=OSError('Synthetic repair projection interruption')):
            with self.assertRaises(OSError): executable.bind(*args)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before_state)
        journal = self.directory() / 'executable-bindings' / 'orphan-repair.json'
        journal_bytes = journal.read_bytes()
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        posts = copy.deepcopy(self.fake.posts)
        with (patch.object(self.service, 'client', side_effect=AssertionError('Orphan refusal precedes AO observation')),
              patch.object(self.service, 'save', side_effect=AssertionError('Orphan refusal precedes state projection'))):
            for action in (self.audit, lambda: self.service.ao_room_spec_review_extend(**inputs)):
                with self.assertRaisesRegex(ao.RoomError, 'Unclaimed or missing executable binding intent'):
                    action()
                self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
        executable.bind(*args)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertNotIn('spec_review_extension', self.state())
        self.assertEqual(self.grant()['remaining_spec_reviews'], 1)
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual(self.fake.posts, posts)

    def test_executable_repairs_append_verified_descendants_before_and_after_review(self):
        import ao_executable_binding as executable
        self.grant()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        original_preparation = (self.directory() / self.state()['preparation']).read_bytes()
        launch = self.root / 'launch-claude'
        previous = self.original_executable
        for number in (1, 2):
            target = self.root / ('replacement-' + str(number))
            target.write_text('#!/bin/sh\nprintf "replacement (Claude Code)\\n"\n'); target.chmod(0o700)
            previous.unlink()
            if launch.is_symlink(): launch.unlink()
            launch.symlink_to(target)
            executable.bind(self.service, self.room, 'repair-' + str(number), str(target), str(launch), str(self.database),
                            'Actual authorization for retained executable repair', 'Original executable was removed')
            self.service.quiet(self.state())
            if number == 1:
                first_pointer = self.state()['executable_binding']
                first_bytes = (self.directory() / first_pointer['path']).read_bytes()
                self.fake.snapshots['engineer']['controller'] = 'ready'
                self.send('spec_review', 'fourth'); self.finish_fourth()
                self.fake.snapshots['engineer']['controller'] = 'stopped'
            previous = target
        self.assertEqual((self.directory() / first_pointer['path']).read_bytes(), first_bytes)
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), original_preparation)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        state = self.state(); state['executable_binding'] = first_pointer
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaises(ao.RoomError): self.service.ao_room_status(self.room)

    def test_pending_grant_blocks_eligible_repair_before_intent_and_exact_grant_reconciles(self):
        import ao_executable_binding as executable
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        inputs = self.grant_inputs()
        with patch.object(self.service, 'save', side_effect=OSError('Synthetic grant projection interruption')):
            with self.assertRaises(OSError): self.service.ao_room_spec_review_extend(**inputs)
        original_bytes = self.original_executable.read_bytes(); original_stat = self.original_executable.stat()
        self.original_executable.unlink()
        target = self.root / 'repair-target'; target.write_text('#!/bin/sh\nprintf "replacement (Claude Code)\\n"\n'); target.chmod(0o700)
        launch = self.root / 'repair-launch'; launch.symlink_to(target)
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'uncommitted review-extension receipt'):
            executable.bind(self.service, self.room, 'pending-repair', str(target), str(launch), str(self.database),
                            'Actual authorized repair', 'Original executable is missing')
        self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.fake.posts, posts)
        self.original_executable.write_bytes(original_bytes); self.original_executable.chmod(original_stat.st_mode)
        os.utime(self.original_executable, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(self.service.ao_room_spec_review_extend(**inputs)['remaining_spec_reviews'], 1)

    def test_changed_native_owner_repair_refuses_before_intent_or_state_write(self):
        import ao_executable_binding as executable
        self.grant()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        target = self.root / 'changed-owner-target'; target.write_text('#!/bin/sh\nprintf "replacement (Claude Code)\\n"\n'); target.chmod(0o700)
        launch = self.root / 'changed-owner-launch'; launch.symlink_to(target)
        self.original_executable.unlink()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='changed-native-owner'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='changed-native-owner'")
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        database_bytes = self.database.read_bytes(); posts = copy.deepcopy(self.fake.posts)
        with (patch.object(extension, 'guard_native_owner', wraps=extension.guard_native_owner) as guard,
              patch.object(executable, '_store_once', side_effect=AssertionError('No repair intent may be written')),
              patch.object(self.service, 'save', side_effect=AssertionError('No room state may be written'))):
            with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
                executable.bind(self.service, self.room, 'changed-owner', str(target), str(launch), str(self.database),
                                'Actual repair approval preserves the original owner.', 'Original executable is missing')
            self.assertEqual(guard.call_count, 1)
            self.assertEqual(guard.call_args.args[2]['provider_conversation_id'], 'changed-native-owner')
        self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.database.read_bytes(), database_bytes)
        self.assertEqual(self.fake.posts, posts)


class ExtensionConfiguredIdempotenceTests(AdoptionFixture):
    make_database = ReviewExtensionFixture.make_database
    register = ReviewExtensionFixture.register
    audit = ReviewExtensionFixture.audit
    grant_inputs = ReviewExtensionFixture.grant_inputs
    grant = ReviewExtensionFixture.grant

    def test_existing_provider_and_routing_results_remain_read_only_after_real_grant(self):
        import ao_provider_transition as provider
        import ao_routing_adoption as routing
        prepared = self.configure_routing()
        attachment = self.start_attachment(prepared)
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.implement(); self.review()
        self.accepted = self.service.ao_room_accept(self.room, 'acceptance_review')
        attachment.stdin.close(); attachment.wait(timeout=10)
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.make_database(); self.target_spec = None
        grant = self.grant()
        provider_inputs = ao.read(self.directory() / self.state()['provider_transition']['receipt'])['inputs']
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        posts = copy.deepcopy(self.fake.posts)
        with (patch.object(self.service, 'save', side_effect=AssertionError('Identical results never save')),
              patch.object(self.service, 'client', side_effect=AssertionError('Identical results never observe AO'))):
            self.assertTrue(provider.transition(self.service, self.room, **provider_inputs)['transitioned'])
            self.assertEqual(routing.stage(*self.stage_args)['phase'], 'configured')
            self.assertEqual(routing.activate(self.service, self.room, 'routing-change')['phase'], 'configured')
        self.assertEqual({str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.fake.posts, posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['receipt_sha256'], grant['receipt_sha256'])


class ExtensionAuditEncodingTests(unittest.TestCase):
    def test_unicode_audit_publishes_exact_utf8_and_reads_back_under_ascii_locale(self):
        script = r'''
import contextlib, json, locale, tempfile
from pathlib import Path
from unittest.mock import patch
import ao_review_extension as extension
import ao_project_room as ao
from ao_reviewer_recovery import _saved_audit, _store_once

class SyntheticService:
    @contextlib.contextmanager
    def locked(self, room_id):
        yield directory, {'spec_record_sha256': 'a' * 64}

with tempfile.TemporaryDirectory() as temp:
    directory = Path(temp).resolve()
    snapshots = {role: {'activities': [{'detail': '\U0001f9ed'}]} for role in ('engineer', 'reviewer')}
    with patch.object(extension, '_inspect', return_value=({'synthetic_evidence': '\U0001f9ed'}, snapshots)):
        result = extension.audit(SyntheticService(), 'synthetic-room', 2, 'a' * 64, 'b' * 64,
                                 'synthetic-native-owner', str(directory / 'synthetic.db'))
    record = extension._audit(directory, result['audit_sha256'])
    path = directory / extension.BASE / 'audits' / (result['audit_sha256'] + '.json')
    expected = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
    assert path.read_bytes() == expected
    assert record['observed_snapshots'] == snapshots
    assert len(list(path.parent.iterdir())) == 1
    assert ao.read(path) == record
    recovery_path = directory / 'reviewer-recovery' / 'audits' / (result['audit_sha256'] + '.json')
    _store_once(recovery_path, record)
    assert ao.read(recovery_path) == record
    assert _saved_audit(directory, result['audit_sha256']) == record
    state_path = directory / 'state.json'
    ao.atomic(state_path, record)
    assert state_path.read_bytes() == expected
    assert ao.read(state_path) == record
    print(json.dumps({'encoding': locale.getpreferredencoding(False), 'readable': True}))
'''
        process = subprocess.run([sys.executable, '-c', script], cwd=os.path.dirname(__file__),
            env={**os.environ, 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        self.assertIn(value['encoding'].lower(), ('us-ascii', 'ascii', 'ansi_x3.4-1968'))
        self.assertTrue(value['readable'])


class CompactionOwnerScopeTests(unittest.TestCase):
    def test_absent_blank_or_nonstring_session_identity_never_grants_an_exemption(self):
        import ao_outcomes
        for session_id in (None, '', ' ', 0, False, [], {}):
            state = {'requests': {'earlier': {'role': 'engineer', 'created_order': 1,
                       'session_id': session_id, 'semantic_outcome': 'synthetic-outcome'}},
                     'bindings': {'engineer': {'session_id': session_id}}}
            with self.subTest(session_id=session_id), patch.object(ao_outcomes, 'load', side_effect=AssertionError('No owning identity')):
                self.assertEqual(ao_outcomes.known_compaction_turns(None, state, {'sessionId': session_id}), set())

    def test_malformed_or_different_native_source_never_grants_an_exemption(self):
        import ao_outcomes
        turn = {'id': 'compaction', 'state': 'recovered'}
        message = {'turnId': turn['id'], 'text': 'Synthetic compacted context'}
        proof = {'turn_id': turn['id'], 'turn_sha256': ao.digest(turn), 'messages_sha256': ao.digest([message])}
        state = {'requests': {'earlier': {'role': 'engineer', 'created_order': 1,
                   'session_id': 'engineer', 'semantic_outcome': 'synthetic-outcome'}},
                 'bindings': {'engineer': {'session_id': 'engineer'}}}
        snapshot = {'sessionId': 'engineer', 'turns': [turn], 'messages': [message]}
        for source in (None, 'not-an-object', [], {}, {'session_id': 'reviewer'}, {'session_id': None}):
            state['native_outcome_source'] = source
            record = {'native': {'source': source, 'compaction_imports': [proof]}}
            with self.subTest(source=source), patch.object(ao_outcomes, 'load', return_value=record):
                self.assertEqual(ao_outcomes.known_compaction_turns(None, state, snapshot), set())


if __name__ == '__main__':
    unittest.main()
