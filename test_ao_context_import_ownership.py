"""Context cannot reclassify owned turns, even when receipt/text/provider checks pass.

Requests and completed receipts use supported APIs against fake AO. A synthetic
backend supplies an accepted opaque provider ID, then contradicts that completed
turn with a recovered import. No account, native runtime or model is used.
"""
import copy
import json
import unittest
from unittest.mock import patch

import ao_native_identity
import ao_native_outcome as native
import ao_outcomes as outcomes
import ao_project_room as ao
import ao_provider_transition as transition
import ao_workflow
from test_ao_adoption import AdoptionFixture
from test_ao_task_notification_imports import (NATIVE_SESSION, envelope, imported, notification,
                                               source_fixture, write_events, audit)


class ContextOwnershipTests(unittest.TestCase):
    def collision(self, kind):
        case = AdoptionFixture('runTest'); self.addCleanup(case.doCleanups); case.setUp()
        case.configure_routing()
        snapshot = case.fake.snapshots['engineer']; snapshot['controller'] = 'ready'
        ao.atomic(case.directory() / 'delegate-launch.json', {
            'session_id': 'engineer', 'worktree': str(case.repo),
            'preparation_sha256': case.state()['preparation_sha256']})
        text = envelope() if kind == 'notification' else 'Synthetic exact retained context.'
        row = notification(str(case.repo)) if kind == 'notification' else {
            'type': 'user', 'sessionId': NATIVE_SESSION, 'cwd': str(case.repo), 'uuid': 'synthetic-summary',
            'isSidechain': False, 'isCompactSummary': True, 'isVisibleInTranscriptOnly': True,
            'message': {'content': text}}
        identity = row['uuid']; branch = snapshot['activeBranchId']
        provider_id = ('acp-history-turn:' + str(len(branch.encode())) + ':' + branch
                       + str(len(identity.encode())) + ':' + identity)
        response = json.dumps(case.report(outcome='changes_required', implementation_complete=False,
                                          remaining_gaps=['Synthetic remaining work.']))
        # Only the unrelated live MCP startup handshake is replaced. All request,
        # receipt, exact sent-text, owner, source and provider-epoch checks run.
        with patch.object(transition, 'native_startup_gate', return_value={'synthetic_handshake': True}):
            case.send('implementation')
            case.fake.finish('engineer', response)
            case.service.ao_room_sync(case.room)
            case.service.ao_room_send(case.room, 'engineer', text, 'prior-owned', purpose='correction')
            snapshot['turns'][-1]['providerTurnId'] = provider_id
            case.fake.finish('engineer', response)
            case.service.ao_room_sync(case.room)
            case.prior = copy.deepcopy(case.state()['requests']['prior-owned'])
            self.assertEqual(case.prior['text'], text)
            self.assertEqual(case.prior['provider_turn_id'], provider_id)
            case.service.ao_room_send(case.room, 'engineer', 'Continue.', 'latest', purpose='correction')
            case.fake.finish('engineer', response)
            case.service.ao_room_sync(case.room)
        source_fixture(case)
        case.source = {'session_id': 'engineer', 'native_session_id': NATIVE_SESSION,
                       'database': str(case.database), 'transcript': str(case.transcript)}
        snapshot = case.snapshot
        self.assertIn(case.prior['turn_id'], case.request['baseline']['turn_ids'])
        snapshot['turns'] = [turn for turn in snapshot['turns'] if turn['id'] != case.prior['turn_id']]
        snapshot['messages'] = [message for message in snapshot['messages'] if message['turnId'] != case.prior['turn_id']]
        turn, message = imported(snapshot, row)
        turn['id'] = case.prior['turn_id']; message['turnId'] = case.prior['turn_id']
        row['timestamp'] = case.notifications[0]['timestamp']
        write_events(case, [case.anchor, case.final, row])
        case.proof_field = 'task_notification_imports' if kind == 'notification' else 'compaction_imports'
        # Prove this exercises the gap after existing integrity checks, rather
        # than merely relying on a forged request or mismatched receipt/text.
        receipt = ao_workflow.completed_receipt(case.directory(), case.prior)
        self.assertEqual(ao.digest(receipt), case.prior['receipt_sha256'])
        self.assertEqual(receipt['turn']['state'], 'completed')
        self.assertEqual(turn['state'], 'recovered')
        self.assertEqual(receipt['turn']['providerTurnId'], turn['providerTurnId'])
        self.assertTrue(ao.sent_message(case.prior, receipt))
        self.assertTrue(ao.sent_message(case.prior, snapshot))
        self.assertEqual(ao.digest(case.prior['text'].encode()), case.prior['text_sha256'])
        case.native_proof = native.inspect(case.directory(), case.state(), case.request, case.source, snapshot)
        self.assertEqual([item['turn_id'] for item in case.native_proof[case.proof_field]], [case.prior['turn_id']])
        self.assertIsNone(case.native_proof['next_human_uuid'])
        self.assertNotIn('unknown', case.native_proof)
        return case

    def preserved(self, case):
        return {'files': {str(path.relative_to(case.directory())): path.read_bytes()
                          for path in case.directory().rglob('*') if path.is_file()},
                'transcript': case.transcript.read_bytes(), 'database': case.database.read_bytes(),
                'snapshot': copy.deepcopy(case.snapshot), 'posts': copy.deepcopy(case.fake.posts),
                'candidate': ao.candidate_snapshot(case.repo)}

    def save_historical_audit(self, case):
        """Recreate the sealed pre-fix audit format without changing old receipts.

        This deliberately represents evidence already persisted by the prior
        observer. Its proof is computed by real owned-source inspection; no
        revalidation, ownership, receipt or provider gate is mocked.
        """
        state = case.state(); request = outcomes.latest_for_role(state, 'engineer')
        record = copy.deepcopy(outcomes.load(case.directory(), request))
        record['native'] = case.native_proof
        record['native_source_verification'] = {
            'source': case.source, 'native_owner': ao_native_identity.read_owner(str(case.database), 'engineer'),
            'native': case.native_proof}
        identity = ao.digest(record); path = 'outcomes/' + request['request_id'] + '/' + identity + '.json'
        ao.atomic(case.directory() / path, record)
        request.update(semantic_outcome=path, semantic_outcome_sha256=identity)
        source_key, evidence_key = native.source_keys('engineer')
        state[source_key] = case.source
        state[evidence_key] = {'path': path, 'sha256': identity, 'request_id': request['request_id']}
        case.service.save(case.directory(), state)
        self.assertEqual(outcomes.load(case.directory(), request), record)

    def assert_audit_refuses(self, kind):
        case = self.collision(kind); before = self.preserved(case)
        with self.assertRaisesRegex(ao.RoomError, 'context import contradicts an owned request turn'):
            audit(case)
        self.assertEqual(self.preserved(case), before)

    def test_notification_collision_refuses_observe_before_any_write(self):
        self.assert_audit_refuses('notification')

    def test_compaction_collision_refuses_observe_before_any_write(self):
        self.assert_audit_refuses('compaction')

    def assert_historical_audit_refuses(self, kind):
        case = self.collision(kind); self.save_historical_audit(case)
        state = case.state(); before = self.preserved(case)
        # The underlying kind-specific proof remains valid and byte-identical;
        # the shared admission boundary refuses its contradictory ownership.
        reader = outcomes.known_task_notification_turns if kind == 'notification' else outcomes.known_compaction_turns
        self.assertEqual(reader(case.directory(), state, case.snapshot), {case.prior['turn_id']})
        operations = {
            'known_context_turns': lambda: outcomes.known_context_turns(case.directory(), state, case.snapshot),
            '_native': lambda: transition._native(state, state['bindings']['engineer'], case.snapshot, case.directory()),
            'dispatch_gate': lambda: transition.dispatch_gate(case.service, case.directory(), state, case.snapshot),
            'observe': lambda: outcomes.observe(case.service, case.directory(), state,
                                              outcomes.latest_for_role(state, 'engineer'), case.snapshot),
        }
        with patch.object(transition, 'native_startup_gate', side_effect=AssertionError('Must refuse before startup')):
            for name, operation in operations.items():
                with self.subTest(gate=name), self.assertRaisesRegex(
                        ao.RoomError, 'context import contradicts an owned request turn'):
                    operation()
                self.assertEqual(self.preserved(case), before)
        current = native.inspect(case.directory(), state, case.request, case.source, case.snapshot)
        self.assertEqual(current, case.native_proof)

    def test_historical_notification_proof_cannot_admit_owned_recovered_work(self):
        self.assert_historical_audit_refuses('notification')

    def test_historical_compaction_proof_cannot_admit_owned_recovered_work(self):
        self.assert_historical_audit_refuses('compaction')


if __name__ == '__main__':
    unittest.main()
