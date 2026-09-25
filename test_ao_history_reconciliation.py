"""Offline proof lifecycle tests using real native parsing and a synthetic owner."""
import copy
from datetime import datetime, timezone
import json
from unittest.mock import patch

import ao_project_room as ao
import ao_outcomes
import ao_history_reconciliation as reconciliation
from test_ao_normal import Fixture
from test_ao_reviewer_native_outcome import ReviewerNativeFixture


class HistoryReconciliationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        original_request = self.fake.request
        def paged_request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                sid = path.split('/')[2]
                return {**copy.deepcopy(self.fake.snapshots[sid]), 'hasMoreBefore': False}
            return original_request(method, path, payload)
        paging = patch.object(self.fake, 'request', side_effect=paged_request)
        paging.start(); self.addCleanup(paging.stop)
        self.send('implementation')
        self.fake.finish('engineer', '')
        self.snapshot = self.fake.snapshots['engineer']
        self.snapshot['history_truncated'] = True
        # Produce the historical receipt using the prior non-strict capture path.
        # All subsequent recovery uses the actual strict page reader.
        with patch.object(reconciliation, 'requires_strict_history', return_value=False):
            self.service.ao_room_sync(self.room)
        self.snapshot['history_truncated'] = False
        self.service.ao_room_sync(self.room)
        self.request = self.state()['requests']['implementation']
        self.receipt_path = self.directory() / self.request['receipt']
        self.receipt_bytes = self.receipt_path.read_bytes()
        self.original_usage = copy.deepcopy(self.request['usage'])
        self.original_history = copy.deepcopy(self.request['receipt_history'])
        self.transcript = self.root / 'native-fixture.jsonl'
        self.source = {'database': str(self.root / 'owner.db'), 'transcript': str(self.transcript),
                       'session_id': 'engineer', 'native_session_id': 'native-fixture'}
        self.owner = {'project_id': 'project', 'provider_conversation_id': 'native-fixture',
                      'workspace_path': str(self.repo), 'ao_conversation_id': 'engineer-native',
                      'active_branch_id': 'root'}
        created = self.request['created_at']
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        self.events = [
            {'type': 'user', 'uuid': 'caller', 'sessionId': 'native-fixture',
             'timestamp': stamp(created + 0.1), 'origin': {'kind': 'human'},
             'message': {'content': self.request['text']}},
            {'type': 'assistant', 'uuid': 'quota', 'sessionId': 'native-fixture',
             'timestamp': stamp(created + 0.2), 'isApiErrorMessage': True,
             'error': 'rate_limit', 'apiErrorStatus': 429, 'message': {'model': '<synthetic>'}},
        ]
        self.write_native()
        state = self.state(); state['native_outcome_source'] = self.source
        ao.atomic(self.directory() / 'state.json', state)
        patcher = patch('ao_native_identity.read_owner', side_effect=lambda *a: copy.deepcopy(self.owner))
        patcher.start(); self.addCleanup(patcher.stop)

    def write_native(self):
        self.transcript.write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def current(self):
        return self.state()['requests']['implementation']

    def audit(self):
        return self.service.ao_room_outcome_audit(self.room)

    def release(self, audit):
        return self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'],
                                                  'continue-once', 'Inspected exact failure', 'User authorizes continuation')

    def assert_preserved(self):
        self.assertEqual(self.receipt_path.read_bytes(), self.receipt_bytes)
        current = self.current()
        self.assertEqual(current['usage'], self.original_usage)
        self.assertFalse(current['usage']['known'])
        self.assertEqual(current['receipt_history'], self.original_history)
        self.assertTrue(json.loads(self.receipt_bytes)['history_truncated'])

    def test_explicit_audit_release_and_exactly_one_successor_preserve_receipt(self):
        posts = len(self.fake.posts)
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.current()['semantic_status']['kind'], 'unknown')
        self.assertNotIn(reconciliation.PROOF, self.current())
        result = self.audit()
        self.assertTrue(result['resume_eligible'])
        self.assertEqual(result['outcome']['kind'], 'quota_limit')
        self.assertTrue(result['outcome']['hold'])
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_preserved()
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.current()['semantic_outcome_sha256'], result['outcome_sha256'])
        self.release(result); self.release(result)
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_preserved()
        self.send('correction', 'continue-once'); self.send('correction', 'continue-once')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assert_preserved()

    def test_no_successor_before_separate_release(self):
        self.audit(); posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.send('correction', 'continue-once')
        self.assertEqual(len(self.fake.posts), posts)
        result = self.audit(); self.release(result)
        self.send('correction', 'continue-once')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_receipt_classifier_is_still_fail_closed(self):
        value = json.loads(self.receipt_bytes)
        native = {'unknown': None, 'errors': [{'error': 'rate_limit', 'http_status': 429}], 'stop_reasons': []}
        self.assertEqual(ao_outcomes.classify(value, native)['kind'], 'unknown')

    def test_corrupt_unreconciled_receipt_still_refuses_audit(self):
        for broken in ('{}', 'invalid json'):
            with self.subTest(broken=broken):
                self.receipt_path.write_text(broken)
                with self.assertRaises((ao.RoomError, ValueError)):
                    self.audit()
                self.assertNotIn(reconciliation.PROOF, self.current())
                self.assertNotIn('outcome_resume', self.current())
        self.receipt_path.write_bytes(self.receipt_bytes)
        self.assertTrue(self.audit()['resume_eligible'])

    def test_incomplete_current_read_cannot_originate(self):
        self.snapshot['history_truncated'] = True
        with self.assertRaises(ao.RoomError): self.audit()
        self.assertNotIn(reconciliation.PROOF, self.current())

    def test_unknown_and_non_quota_native_results_never_originate(self):
        for error, status in (('invalid_request', 400), ('auth_required', 401), (None, None)):
            with self.subTest(error=error):
                self.events[-1].update(error=error, apiErrorStatus=status); self.write_native()
                result = self.audit()
                self.assertFalse(result['resume_eligible'])
                self.assertNotIn(reconciliation.PROOF, self.current())
        self.transcript.write_text('malformed native data\n')
        self.assertFalse(self.audit()['resume_eligible'])

    def test_settled_retry_error_and_successful_final_do_not_originate(self):
        final = copy.deepcopy(self.events[-1]); final.update(uuid='answer', isApiErrorMessage=False)
        final.pop('error'); final.pop('apiErrorStatus')
        final['timestamp'] = datetime.fromtimestamp(self.request['created_at'] + 0.3, timezone.utc).isoformat()
        final['message'] = {'model': self.request['model'], 'id': 'response', 'stop_reason': 'end_turn'}
        self.events.append(final); self.write_native()
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.current())

    def test_wrong_or_ambiguous_native_anchor_does_not_originate(self):
        original = copy.deepcopy(self.events)
        for mode in ('changed', 'duplicate', 'foreign', 'later-human'):
            with self.subTest(mode=mode):
                self.events = copy.deepcopy(original)
                if mode == 'changed': self.events[0]['message']['content'] = 'Different instruction'
                if mode == 'duplicate': self.events.insert(1, {**self.events[0], 'uuid': 'duplicate'})
                if mode == 'foreign': self.events[-1]['sessionId'] = 'foreign-session'
                if mode == 'later-human':
                    self.events.append({**self.events[0], 'uuid': 'later', 'timestamp':
                        datetime.fromtimestamp(self.request['created_at'] + 0.4, timezone.utc).isoformat()})
                self.write_native()
                self.assertFalse(self.audit()['resume_eligible'])
                self.assertNotIn(reconciliation.PROOF, self.current())

    def test_wrong_native_owner_never_originates(self):
        original = copy.deepcopy(self.owner)
        for key in ('project_id', 'provider_conversation_id', 'workspace_path', 'ao_conversation_id', 'active_branch_id'):
            with self.subTest(key=key):
                self.owner = {**original, key: 'wrong'}
                self.assertFalse(self.audit()['resume_eligible'])
                self.assertNotIn(reconciliation.PROOF, self.current())

    def test_malformed_current_failures_cannot_originate_a_proof(self):
        self.snapshot['sessionFailures'] = ['malformed']
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.current())

    def test_changed_or_missing_owned_messages_and_turn_identity_refuse(self):
        original = copy.deepcopy(self.snapshot)
        def message(): self.snapshot['messages'][-1]['text'] = 'Changed'
        def missing(): self.snapshot['messages'].pop()
        def provider(): self.snapshot['turns'][-1]['providerTurnId'] = 'wrong'
        def branch(): self.snapshot['activeBranchId'] = 'wrong'
        def session(): self.snapshot['sessionId'] = 'wrong'
        for mutate in (message, missing, provider, branch, session):
            with self.subTest(mutate=mutate.__name__):
                self.snapshot.clear(); self.snapshot.update(copy.deepcopy(original)); mutate()
                with self.assertRaises(ao.RoomError): self.audit()
                self.assertNotIn(reconciliation.PROOF, self.current())

    def test_extra_and_unproven_recovered_turns_refuse(self):
        for status in ('completed', 'running', 'recovered'):
            with self.subTest(status=status):
                self.snapshot['turns'].append({'id': 'extra', 'providerTurnId': 'extra-native', 'state': status})
                with self.assertRaises(ao.RoomError): self.audit()
                self.snapshot['turns'].pop()
                self.assertNotIn(reconciliation.PROOF, self.current())

    def test_extra_turn_observed_then_removed_cannot_revive_existing_release(self):
        for status in ('completed', 'recovered'):
            with self.subTest(status=status):
                result = self.audit(); self.release(result)
                self.snapshot['turns'].append({'id': 'extra', 'providerTurnId': 'extra-native', 'state': status})
                self.service.ao_room_sync(self.room)
                self.snapshot['turns'].pop()
                self.assert_stale_requires_new_audit(result)

    def test_missing_baseline_turn_cannot_originate_or_revive_proof(self):
        baseline = self.request['baseline']['turn_ids'][0]
        original = copy.deepcopy(self.snapshot['turns'])
        self.snapshot['turns'] = [t for t in original if t['id'] != baseline]
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.current())
        self.snapshot['turns'] = copy.deepcopy(original)
        result = self.audit(); self.release(result)
        self.snapshot['turns'] = [t for t in original if t['id'] != baseline]
        self.service.ao_room_sync(self.room)
        self.snapshot['turns'] = original
        self.assert_stale_requires_new_audit(result)

    def test_active_delegate_refuses_and_invalidates_existing_release(self):
        result = self.audit(); self.release(result)
        with patch('ao_delegates.assert_settled', side_effect=ao.RoomError('Active delegate')):
            with self.assertRaises(ao.RoomError): self.release(result)
        self.assert_stale_requires_new_audit(result)

    def test_unknown_request_order_preserves_guard_and_invalidates_existing_proof(self):
        for malformed in ({'request_id': 'malformed', 'role': 'engineer', 'session_id': 'engineer'}, None, 'damaged'):
            with self.subTest(malformed=malformed):
                result = self.audit(); self.release(result)
                state = self.state(); state['requests']['malformed'] = malformed
                ao.atomic(self.directory() / 'state.json', state)
                self.assertTrue(reconciliation.requires_strict_history(self.directory(), state, {'session_id': 'engineer'}))
                with self.assertRaisesRegex(ao.RoomError, 'Original refusal'):
                    with self.service.locked(self.room):
                        raise ao.RoomError('Original refusal')
                state = self.state(); state['requests'].pop('malformed')
                ao.atomic(self.directory() / 'state.json', state)
                self.assert_stale_requires_new_audit(result)

    def assert_stale_requires_new_audit(self, old):
        with self.assertRaises(ao.RoomError): self.release(old)
        posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.send('correction', 'continue-once')
        self.assertEqual(len(self.fake.posts), posts)
        new = self.audit()
        self.assertTrue(new['resume_eligible'])
        self.assertNotEqual(new['outcome_sha256'], old['outcome_sha256'])
        self.release(new)
        self.assert_preserved()

    def test_changed_then_restored_message_cannot_revive_old_release_via_sync(self):
        result = self.audit(); self.release(result)
        self.snapshot['messages'][-1]['text'] = 'Changed'
        self.service.ao_room_sync(self.room)  # observe's refusal is caught by sync.
        self.assertIn(reconciliation.INVALIDATION, self.current())
        self.snapshot['messages'][-1]['text'] = ''
        self.assert_stale_requires_new_audit(result)

    def test_strict_reader_refusal_before_observe_cannot_revive_release(self):
        result = self.audit(); self.release(result)
        original = self.fake.request
        def refused(method, path, payload=None):
            if '/conversation?' in path: raise ao.RoomError('Strict history bound exhausted')
            return original(method, path, payload)
        with patch.object(self.fake, 'request', side_effect=refused):
            with self.assertRaises(ao.RoomError): self.service.ao_room_sync(self.room)
        self.assertIn(reconciliation.INVALIDATION, self.current())
        self.assert_stale_requires_new_audit(result)

    def test_conflicting_raw_history_pages_refuse_before_origin_and_after_release(self):
        full = copy.deepcopy(self.snapshot)
        first = {**copy.deepcopy(full), 'messages': full['messages'][2:], 'hasMoreBefore': True, 'oldestSequence': 3}
        last = {**copy.deepcopy(full), 'messages': full['messages'][:2], 'hasMoreBefore': False, 'oldestSequence': 1}
        last['turns'][-1]['providerTurnId'] = 'conflicting-older-page-provider-turn'
        original = self.fake.request
        def pages(method, path, payload=None):
            if '/sessions/engineer/conversation?' in path:
                return copy.deepcopy(last if 'beforeSequence=' in path else first)
            return original(method, path, payload)
        with patch.object(self.fake, 'request', side_effect=pages):
            with self.assertRaisesRegex(ao.RoomError, 'conflicting turns'): self.audit()
        self.assertNotIn(reconciliation.PROOF, self.current())
        result = self.audit(); self.release(result)
        with patch.object(self.fake, 'request', side_effect=pages):
            with self.assertRaisesRegex(ao.RoomError, 'conflicting turns'): self.release(result)
        self.assert_stale_requires_new_audit(result)

    def test_restored_branch_after_identity_refusal_cannot_revive_release(self):
        result = self.audit(); self.release(result)
        self.snapshot['activeBranchId'] = 'wrong'
        with self.assertRaises(ao.RoomError): self.release(result)
        self.snapshot['activeBranchId'] = 'root'
        self.assert_stale_requires_new_audit(result)

    def test_native_source_change_requires_explicit_reaudit(self):
        result = self.audit(); self.release(result)
        self.transcript.write_text(self.transcript.read_text() + '\n')
        self.service.ao_room_sync(self.room)
        self.assertFalse(self.current()['semantic_status']['kind'] == 'final_available')
        self.assert_stale_requires_new_audit(result)

    def test_legitimate_source_move_requires_explicit_audit_and_new_release(self):
        result = self.audit(); self.release(result)
        old_proof = self.current()[reconciliation.PROOF]
        moved = self.root / 'relocated'; moved.mkdir()
        new_path = moved / self.transcript.name
        new_path.write_bytes(self.transcript.read_bytes())
        new = self.service.ao_room_outcome_audit(self.room, ao_database_path=self.source['database'],
                                                native_transcript_path=str(new_path))
        self.assertTrue(new['resume_eligible'])
        self.assertNotEqual(result['outcome_sha256'], new['outcome_sha256'])
        self.assertNotEqual(old_proof, self.current()[reconciliation.PROOF])
        self.assertTrue((self.directory() / 'history-reconciliations' / 'implementation' /
                         (old_proof + '.json')).is_file())
        self.release(new)
        self.send('correction', 'continue-once')
        self.assert_preserved()

    def test_restored_active_owner_does_not_revive_release(self):
        result = self.audit(); self.release(result)
        self.fake.snapshots['reviewer']['turns'].append({'id': 'outside', 'state': 'running'})
        with self.assertRaises(ao.RoomError): self.release(result)
        self.fake.snapshots['reviewer']['turns'].clear()
        self.assert_stale_requires_new_audit(result)

    def test_restored_corrupt_invalidation_journal_requires_fresh_audit(self):
        result = self.audit(); self.release(result)
        self.snapshot['messages'][-1]['text'] = 'Changed'
        self.service.ao_room_sync(self.room)
        self.snapshot['messages'][-1]['text'] = ''
        result = self.audit(); self.release(result)
        sha = self.current()[reconciliation.INVALIDATION]
        path = self.directory() / 'history-reconciliation-invalidations' / 'implementation' / (sha + '.json')
        original = path.read_bytes(); path.write_text('{}\n')
        with self.assertRaises(ao.RoomError): self.release(result)
        path.write_bytes(original)
        self.assert_stale_requires_new_audit(result)

    def test_restored_unknown_native_result_cannot_revive_release(self):
        result = self.audit(); self.release(result)
        original = self.transcript.read_bytes(); self.transcript.write_text('invalid json\n')
        self.service.ao_room_sync(self.room)
        self.transcript.write_bytes(original)
        self.assert_stale_requires_new_audit(result)

    def test_idempotent_release_rechecks_proof_and_receipt_tampering(self):
        for which in ('receipt', 'proof', 'outcome'):
            with self.subTest(which=which):
                result = self.audit(); self.release(result)
                request = self.current()
                path = self.receipt_path if which == 'receipt' else (self.directory() / request['semantic_outcome']
                    if which == 'outcome' else self.directory() / 'history-reconciliations' / 'implementation' /
                    (request[reconciliation.PROOF] + '.json'))
                original = path.read_bytes(); path.write_text('{}\n')
                with self.assertRaises(ao.RoomError): self.release(result)
                path.write_bytes(original)
                self.assert_stale_requires_new_audit(result)

    def test_failed_transport_does_not_enter_this_lane(self):
        self.snapshot['turns'][-1]['state'] = 'failed'
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.current())

    def test_foreign_named_successor_is_not_dispatched(self):
        result = self.audit(); self.release(result); posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.send('correction', 'unapproved-successor')
        self.assertEqual(len(self.fake.posts), posts)

    def test_proof_binds_original_receipt_and_current_native_observation(self):
        result = self.audit(); request = self.current()
        proof = reconciliation.load_proof(self.directory(), request)
        self.assertEqual(proof['receipt_sha256'], request['receipt_sha256'])
        self.assertEqual(proof['inputs']['messages_sha256'], ao.digest(json.loads(self.receipt_bytes)['messages']))
        self.assertEqual(proof['inputs']['native']['source_sha256'], ao.digest(self.transcript.read_bytes()))
        self.assertEqual(proof['inputs']['native']['anchor_uuid'], 'caller')
        self.assertTrue(proof['saved_history_truncated'])
        self.assertTrue(result['outcome']['hold'])


class ReviewerHistoryReconciliationTests(ReviewerNativeFixture):
    """Exercise the Astra-led Claude reviewer with real SQLite/native readers."""
    def setUp(self):
        original_sync = ao.Service.ao_room_sync
        def capture(service, room):
            self.fake.snapshots['reviewer']['history_truncated'] = True
            try:
                with patch.object(reconciliation, 'requires_strict_history', return_value=False):
                    return original_sync(service, room)
            finally:
                self.fake.snapshots['reviewer']['history_truncated'] = False
        with patch.object(ao.Service, 'ao_room_sync', new=capture):
            super().setUp()
        original_request = self.fake.request
        def pages(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**copy.deepcopy(self.fake.snapshots[path.split('/')[2]]), 'hasMoreBefore': False}
            return original_request(method, path, payload)
        paging = patch.object(self.fake, 'request', side_effect=pages)
        paging.start(); self.addCleanup(paging.stop)
        error = {**self.final, 'uuid': 'quota', 'isApiErrorMessage': True,
                 'error': 'rate_limit', 'apiErrorStatus': 429, 'message': {'model': '<synthetic>'}}
        self.write_events([self.anchor, error])

    def test_real_reviewer_owner_and_complete_reader_can_reconcile(self):
        result = self.audit()
        self.assertTrue(result['resume_eligible'])
        self.assertEqual(result['outcome']['kind'], 'quota_limit')

    def test_other_role_unknown_order_cannot_disable_reviewer_strict_selection(self):
        state = self.state()
        state['requests']['malformed-engineer'] = {'request_id': 'malformed-engineer',
            'role': 'engineer', 'session_id': 'engineer'}
        self.assertTrue(reconciliation.requires_strict_history(self.directory(), state, {'session_id': 'reviewer'}))

    def test_unproven_reviewer_recovered_turn_cannot_originate_or_revive_proof(self):
        snapshot = self.fake.snapshots['reviewer']
        extra = {'id': 'unproven-context', 'providerTurnId': 'unproven-native', 'state': 'recovered'}
        snapshot['turns'].append(extra)
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.state()['requests']['review-1'])
        snapshot['turns'].pop()
        result = self.audit()
        args = (self.room, 'review-1', result['outcome_sha256'], 'review-2', 'Exact quota diagnosis', 'User continuation')
        self.service.ao_room_outcome_resume(*args)
        snapshot['turns'].append(extra)
        self.assertFalse(self.audit(False)['resume_eligible'])
        snapshot['turns'].pop()
        with self.assertRaises(ao.RoomError): self.service.ao_room_outcome_resume(*args)
        fresh = self.audit(False)
        self.assertTrue(fresh['resume_eligible'])
        self.assertNotEqual(fresh['outcome_sha256'], result['outcome_sha256'])

    def test_live_reroute_refuses_but_attributed_historical_reroute_does_not(self):
        snapshot = self.fake.snapshots['reviewer']
        snapshot['modelReroute'] = {'toModel': 'substitute', 'providerTurnId': self.request['provider_turn_id']}
        self.assertFalse(self.audit()['resume_eligible'])
        self.assertNotIn(reconciliation.PROOF, self.state()['requests']['review-1'])
        snapshot['modelReroute']['providerTurnId'] = 'older-turn'
        self.assertTrue(self.audit()['resume_eligible'])

    def test_live_reviewer_reroute_invalidates_prior_continuation(self):
        result = self.audit()
        args = (self.room, 'review-1', result['outcome_sha256'], 'review-2', 'Exact quota diagnosis', 'User continuation')
        self.service.ao_room_outcome_resume(*args)
        snapshot = self.fake.snapshots['reviewer']
        snapshot['modelReroute'] = {'toModel': 'substitute', 'providerTurnId': self.request['provider_turn_id']}
        self.assertFalse(self.audit(False)['resume_eligible'])
        snapshot.pop('modelReroute')
        with self.assertRaises(ao.RoomError): self.service.ao_room_outcome_resume(*args)
        fresh = self.audit(False)
        self.assertTrue(fresh['resume_eligible'])
        self.assertNotEqual(fresh['outcome_sha256'], result['outcome_sha256'])
