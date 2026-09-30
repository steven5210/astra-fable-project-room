"""A served-model contradiction settles as a diagnosed ``model_mismatch`` on complete evidence.

A qualified family request whose retained transcript rows all end_turn with no errors but report
other members of the same qualified family keeps the completed result under a named hold, becomes
resumable through the recorded continuation lane, and clears only after the audited transition
pins the exact model identifier. Incomplete or cross-family contradictions stay ``unknown``.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ao_engineering_model as em
import ao_engineering_transition as et
import ao_model_qualification as qmod
import ao_project_room as ao
from test_ao_model_qualification import OPUS, QualificationFixture

OLDER_OPUS = 'claude-opus-5'
FABLE = 'claude-fable-5-1'


class ModelMismatchLaneTests(QualificationFixture):
    """Complete-evidence same-family contradiction: diagnosed failure, hold, recorded resume."""

    def test_complete_same_family_contradiction_is_a_resumable_model_mismatch(self):
        artifact = self.qualified_room('served-older-opus')
        sha = qmod.digest(artifact)
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS)
        audit = self.service.ao_room_outcome_audit(self.room)
        outcome = audit['outcome']
        self.assertEqual(outcome['kind'], 'model_mismatch')
        self.assertTrue(outcome['hold'])
        self.assertIn('contradicts the qualified expected model', outcome['reason'])
        self.assertTrue(audit['resume_eligible'])
        native = audit['native']
        self.assertNotIn('unknown', native)
        self.assertEqual(native['model_mismatch'],
                         {**native['model_contradiction'], 'source_sha256': native['source_sha256']})
        self.assertEqual(native['model_mismatch']['observed_models'], [OLDER_OPUS])
        self.assertEqual(native['model_mismatch']['expected_model'], OPUS)
        self.assertEqual(native['model_mismatch']['qualification_sha256'], sha)

    def test_cross_family_served_model_stays_unknown(self):
        self.qualified_room('served-sonnet')
        self.turn('spec_review', 'spec_review', self.verdict(), 'claude-sonnet-5')
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        self.assertIn('contradicts the qualified expected model', outcome['native']['unknown'])
        self.assertIn('model_contradiction', outcome['native'])
        self.assertNotIn('model_mismatch', outcome['native'])

    def test_unsettled_native_error_keeps_the_lane_unknown(self):
        self.qualified_room('served-opus-error')
        self.send('spec_review')
        self.native_turn('spec_review', OLDER_OPUS)
        request = self.request('spec_review')
        stamp = min(datetime.fromtimestamp(request['created_at'] + 0.003, timezone.utc),
                    datetime.now(timezone.utc)).isoformat()
        self.events.append({'type': 'assistant', 'uuid': 'spec_review-error', 'sessionId': self.NATIVE,
                            'timestamp': stamp, 'cwd': self.native_row_workspace(),
                            'isSidechain': False, 'isApiErrorMessage': True, 'error': 'overloaded',
                            'apiErrorStatus': 529, 'message': {'model': '<synthetic>'}})
        self.write_native()
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        outcome = self.outcome('spec_review')
        # The provider error row stayed unsettled: the evidence is incomplete and the contradiction
        # falls back to the unchanged unknown lane rather than a settled mismatch diagnosis.
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        self.assertNotIn('model_mismatch', outcome['native'])

    def test_typed_receipt_failure_precedes_the_mismatch_lane(self):
        self.qualified_room('served-opus-typed')
        self.send('spec_review')
        self.native_turn('spec_review', OLDER_OPUS)
        self.fake.finish('engineer', self.verdict())
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit',
                                                                'httpStatus': 429}
        self.service.ao_room_sync(self.room)
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['outcome']['kind'], 'quota_limit')
        self.assertIn('model_mismatch', outcome['native'])

    def test_refusal_stop_row_keeps_the_lane_unknown(self):
        self.qualified_room('served-opus-refusal')
        self.send('spec_review')
        self.native_turn('spec_review', OLDER_OPUS)
        self.events[-1]['message']['stop_reason'] = 'refusal'
        self.write_native()
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        self.assertNotIn('model_mismatch', outcome['native'])

    def test_an_earlier_refusal_stop_row_is_not_hidden_by_the_later_end_turn(self):
        self.qualified_room('served-opus-hidden-refusal')
        self.send('spec_review')
        self.native_turn('spec_review', OLDER_OPUS, OLDER_OPUS)
        # The first served row refused; the later end_turn resets the condensed stop map, so
        # completeness is judged from every attributable stop row, not the condensed map alone.
        self.events[1]['message']['stop_reason'] = 'refusal'
        self.write_native()
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['native']['stop_row_reasons'], ['end_turn', 'refusal'])
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        self.assertNotIn('model_mismatch', outcome['native'])

    def test_tool_use_stop_rows_do_not_block_the_settled_mismatch(self):
        self.qualified_room('served-opus-tools')
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS, OLDER_OPUS, OLDER_OPUS)
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['native']['stop_row_reasons'], ['end_turn', 'tool_use'])
        self.assertEqual(outcome['outcome']['kind'], 'model_mismatch')

    def test_no_final_response_stays_unknown_and_cannot_be_resumed(self):
        self.qualified_room('served-opus-nofinal')
        self.send('spec_review')
        self.native_turn('spec_review', OLDER_OPUS)
        self.fake.finish('engineer', '')
        self.service.ao_room_sync(self.room)
        outcome = self.outcome('spec_review')
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertFalse(audit['resume_eligible'])

    def test_resume_records_the_continuation_and_a_different_successor_is_refused(self):
        self.qualified_room('served-opus-resume')
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS)
        audit = self.service.ao_room_outcome_audit(self.room)
        result = self.service.ao_room_outcome_resume(
            self.room, 'spec_review', audit['outcome_sha256'], 'resume-1',
            'AO resolved the bare opus alias to claude-opus-5; pin claude-opus-5-5',
            'The user authorized this continuation')
        self.assertEqual(result['resume_request_id'], 'resume-1')
        self.assertEqual(result['outcome_sha256'], audit['outcome_sha256'])
        request = self.request('spec_review')
        self.assertEqual(request['state'], 'completed')
        saved = ao.read(self.directory() / request['outcome_resume'])
        self.assertEqual(saved['resume_request_id'], 'resume-1')
        with self.assertRaisesRegex(ao.RoomError, 'different immutable continuation'):
            self.service.ao_room_outcome_resume(
                self.room, 'spec_review', audit['outcome_sha256'], 'resume-2',
                'same diagnosis', 'The user authorized this continuation')

    def test_successor_is_refused_until_the_exact_identifier_is_committed(self):
        self.qualified_room('served-opus-gate')
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(
            self.room, 'spec_review', audit['outcome_sha256'], 'resume-1',
            'AO resolved the bare opus alias to claude-opus-5; pin claude-opus-5-5',
            'The user authorized this continuation')
        with self.assertRaisesRegex(ao.RoomError, 'committed transition to an exact identifier'):
            self.send('spec_review', 'resume-1')
        self.assertNotIn('resume-1', self.state()['requests'])

    def test_any_exact_identifier_selector_admits_the_successor(self):
        self.qualified_room('served-opus-wrong-target')
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(
            self.room, 'spec_review', audit['outcome_sha256'], 'resume-1',
            'AO resolved the bare opus alias to claude-opus-5; pin claude-opus-5-5',
            'The user authorized this continuation')
        # The gate reads only the current epoch selector: any exact identifier admits the successor
        # (an alias could be mis-resolved again; an exact identifier cannot), a family alias refuses.
        epoch = em.current(self.directory(), self.state())
        with patch.object(em, 'current', return_value={**epoch, 'selector': {'kind': 'family',
                                                                             'family': 'fable'}}):
            with self.assertRaisesRegex(ao.RoomError, 'committed transition to an exact identifier'):
                self.send('spec_review', 'resume-1')
        self.assertNotIn('resume-1', self.state()['requests'])
        with patch.object(em, 'current', return_value={**epoch, 'selector': {'kind': 'exact',
                                                                             'model': 'claude-fable-5-1'}}):
            self.send('spec_review', 'resume-1')
        self.assertIn('resume-1', self.state()['requests'])

    def recover_through(self, target, room_suffix):
        """Mismatch, resume, audited transition to an exact identifier, reserved successor completes."""
        self.qualified_room(room_suffix)
        self.turn('spec_review', 'spec_review', self.verdict(), OLDER_OPUS)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(
            self.room, 'spec_review', audit['outcome_sha256'], 'resume-1',
            'AO resolved the bare opus alias to claude-opus-5; pin claude-opus-5-5',
            'The user authorized this continuation')
        # The unknown outcome no longer blocks the audited transition; the fake AO answers the
        # settings PATCH so the committed epoch can be re-observed exactly like a live controller.
        self.fake.snapshots['engineer']['controller'] = 'ready'
        original = type(self.fake).request.__get__(self.fake, type(self.fake))

        def request(method, path, payload=None):
            if method == 'PATCH' and path.endswith('/conversation/settings'):
                self.fake.snapshots['engineer']['settings'].update(payload or {})
                return {'ok': True}
            return original(method, path, payload)

        patcher = patch.object(self.fake, 'request', side_effect=request)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = et.audit(self.service, self.room, target, str(self.database), str(self.transcript))
        self.assertTrue(result['eligible'])
        committed = et.transition(self.service, room_id=self.room, request_id='model-2',
                                  source_model=result['source']['configured_model'],
                                  target_model=result['target']['configured_model'],
                                  audit_sha256=result['audit_sha256'],
                                  spec_record_sha256=result['spec_record_sha256'],
                                  candidate_sha256=result['candidate_sha256'],
                                  native_history_sha256=result['native_history_sha256'],
                                  native_owner_database=str(self.database),
                                  native_transcript_path=str(self.transcript),
                                  authorization='The user authorized this exact engineering model '
                                                'transition',
                                  reason='AO resolved the bare alias to an older member; pin the '
                                         'exact identifier')
        self.assertTrue(committed['transitioned'])
        self.assertEqual(committed['configured_model'], target)
        self.send('spec_review', 'resume-1')
        self.native_turn('resume-1', target)
        self.fake.finish('engineer', self.verdict())
        self.service.ao_room_sync(self.room)
        done = self.outcome('resume-1')
        self.assertEqual(done['outcome']['kind'], 'final_available')
        return done

    def test_exact_identifier_transition_releases_the_reserved_successor(self):
        self.recover_through(OPUS, 'served-opus-recovery')

    def test_another_qualified_exact_identifier_releases_the_reserved_successor(self):
        # The operator may pin any qualified exact identifier (here Fable 5.1, a different family)
        # instead of the mismatched family's expected member; the successor then completes under
        # that identifier and audits as final. The gate reads the current selector only, so a room
        # whose earlier epochs ran that identifier behaves the same way.
        done = self.recover_through(FABLE, 'served-opus-to-fable')
        # An exact request keeps its historical result shape: no family observation, no contradiction.
        self.assertNotIn('model_contradiction', done['native'])
        self.assertNotIn('model_mismatch', done['native'])


if __name__ == '__main__':
    unittest.main()
