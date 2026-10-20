"""Finding-provenance one-time workflow-part delivery: offline, synthetic fixtures only."""
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

import ao_evidence_closure
import ao_execution_evidence
import ao_finding_provenance
import ao_progress
import ao_project_room as ao
import ao_quality_review
import ao_read_admission
import ao_residual_escalation
import ao_workflow
import lifecycle_closure
from room import RoomError
import test_ao_continuation
import test_ao_normal as fixtures

# The PARTS tuple as it stood immediately before this unit: every name except finding_provenance_v1
# and evidence_closure_v1 (appended immediately after it, later than this unit), taken from the live
# tuple so a change to the historical ordering or either new element is caught, not just inferred
# from today's value. Excluding only finding_provenance_v1 would describe an impossible history --
# evidence_closure_v1 delivered while finding_provenance_v1, which PARTS orders before it, never was
# -- since one engineer packet delivers every then-undelivered part at once, in PARTS order.
OLDER_PARTS = tuple(p for p in ao_workflow.PARTS if p not in (ao_finding_provenance.PART, ao_evidence_closure.PART))
# The real 16-name prefix ending at progress_plan_v3, before either of the last two parts existed.
PRE_EXECUTION_PARTS = ao_workflow.PARTS[:16]

# Independently copied pinned digests for the other existing frozen one-time workflow parts. Comparing
# each against both the live module constant and this literal catches drift in either the text or its
# pin, never only one of the two.
PROGRESS_SHA256 = "983fbab2fe00e5c0f432567d5a18d05a5581b81a93f321442944f07f320823ae"
PROGRESS_V2_SHA256 = "3ee80001834443cbdcdf06c15c42692db42e14e5906c46ad2a42249420b78ee0"
PROGRESS_V3_SHA256 = "9e559c9b963f8a883a979f1f97e3936aba0d2df5c7f7b5c682634da3380b665b"
RESIDUAL_ESCALATION_SHA256 = "86a1faed57c2119fc492182508cec813d7704318382c11db9380219e81b190e4"
QUALITY_REVIEW_SHA256 = "6d043563ae4c27854dbaad53b9762476efa189c9fb6565dfb5670e24cee47a4e"
READ_ADMISSION_SHA256 = "6fa2a1310c6d724334b2d32e1fc6a5c91811b8e4962b1ac3057348e146d79e99"
LIFECYCLE_CLOSURE_SHA256 = "d9db1bee4d3b65b301ab7f3508ca2d71a7b16de7f769626f78415f50715a22da"


class DigestAndFrozenBytesTests(unittest.TestCase):
    """The new module's own pin, its frozen bytes, and every older frozen byte stays as recorded."""

    def test_finding_provenance_digest_matches_its_pin_and_part_name(self):
        self.assertEqual(hashlib.sha256(ao_finding_provenance.INSTRUCTION.encode()).hexdigest(),
                         ao_finding_provenance.INSTRUCTION_SHA256)
        self.assertEqual(ao.digest(ao_finding_provenance.INSTRUCTION.encode()),
                         ao_finding_provenance.INSTRUCTION_SHA256)
        self.assertEqual(ao_finding_provenance.PART, "finding_provenance_v1")
        self.assertEqual(ao_finding_provenance.VERSION, 1)

    def test_finding_provenance_module_source_has_no_import_statements(self):
        source = inspect.getsource(ao_finding_provenance)
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("import") or stripped.startswith("from "),
                             "unexpected import line in ao_finding_provenance.py: " + repr(line))

    def test_parts_tuple_keeps_history_and_appends_the_new_part_last(self):
        # evidence_closure_v1 was appended after this unit, so finding_provenance_v1 is no longer
        # PARTS[-1]; its own fixed position (index 17, immediately after every older part) stays
        # pinned here regardless, and evidence_closure_v1's own ordering is independently pinned in
        # test_lifecycle_closure.py and test_ao_evidence_closure.py.
        self.assertEqual(ao_workflow.PARTS[:17], OLDER_PARTS)
        self.assertEqual(ao_workflow.PARTS[17], ao_finding_provenance.PART)
        self.assertEqual(ao_workflow.PARTS[18], ao_evidence_closure.PART)
        self.assertEqual(len(ao_workflow.PARTS), 19)

    def test_existing_frozen_part_digests_are_unchanged(self):
        cases = (
            ("ao_progress.INSTRUCTION", ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_SHA256, PROGRESS_SHA256),
            ("ao_progress.INSTRUCTION_V2", ao_progress.INSTRUCTION_V2, ao_progress.INSTRUCTION_V2_SHA256, PROGRESS_V2_SHA256),
            ("ao_progress.INSTRUCTION_V3", ao_progress.INSTRUCTION_V3, ao_progress.INSTRUCTION_V3_SHA256, PROGRESS_V3_SHA256),
            ("ao_residual_escalation.INSTRUCTION", ao_residual_escalation.INSTRUCTION,
             ao_residual_escalation.INSTRUCTION_SHA256, RESIDUAL_ESCALATION_SHA256),
            ("ao_quality_review.INSTRUCTION", ao_quality_review.INSTRUCTION,
             ao_quality_review.INSTRUCTION_SHA256, QUALITY_REVIEW_SHA256),
            ("ao_read_admission.INSTRUCTION", ao_read_admission.INSTRUCTION,
             ao_read_admission.INSTRUCTION_SHA256, READ_ADMISSION_SHA256),
            ("lifecycle_closure.INSTRUCTION", lifecycle_closure.INSTRUCTION,
             lifecycle_closure.INSTRUCTION_SHA256, LIFECYCLE_CLOSURE_SHA256),
        )
        for label, text, pinned, literal in cases:
            with self.subTest(part=label):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), pinned)
                self.assertEqual(pinned, literal)


class NewSessionDeliveryTests(fixtures.Fixture):
    """A brand-new retained session receives the new part exactly once, on its first packet."""

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def test_finding_provenance_part_delivered_once_on_the_first_packet(self):
        request = self.state()['requests']['spec_review']
        carried = request['carried']
        self.assertEqual(carried['parts'].count(ao_finding_provenance.PART), 1)
        # evidence_closure_v1 was appended after this unit, so it is now last; finding_provenance_v1
        # is second to last.
        self.assertEqual(carried['parts'][-2], ao_finding_provenance.PART)
        self.assertEqual(carried['parts'][-1], ao_evidence_closure.PART)
        self.assertEqual(carried['part_sha256'][ao_finding_provenance.PART],
                         ao_finding_provenance.INSTRUCTION_SHA256)
        self.assertEqual(request['text'].count(ao_finding_provenance.INSTRUCTION), 1)
        # Already delivered with the first packet; a later packet in the same session omits it.
        self.assertNotIn(ao_finding_provenance.PART, self.state()['requests']['impl-1']['carried']['parts'])
        self.assertNotIn(ao_finding_provenance.INSTRUCTION, self.state()['requests']['impl-1']['text'])


class RetainedSessionDeliveryTests(fixtures.Fixture):
    """A session retained from before finding_provenance_v1 (and evidence_closure_v1, appended
    immediately after it) existed gets both exactly once together, then nothing."""

    def setUp(self):
        super().setUp()
        # Simulate a session whose first packet predates both finding_provenance_v1 and
        # evidence_closure_v1: every older part was already delivered, so the next engineer send
        # carries just the two new parts, together, in PARTS order.
        with patch.object(ao_workflow, 'PARTS', OLDER_PARTS):
            self.room = self.open()
            self.spec()
            self.bind()
            self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def packet(self, purpose='correction', now=None):
        return ao_workflow.packet(self.service, self.directory(), self.state(), 'engineer',
                                  purpose, 'Continue.', now=now)

    def test_new_part_delivers_once_to_a_retained_session_then_nothing(self):
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertNotIn(ao_finding_provenance.PART, held_before['parts'])
        self.assertNotIn(ao_evidence_closure.PART, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'], [ao_finding_provenance.PART, ao_evidence_closure.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_finding_provenance.PART: ao_finding_provenance.INSTRUCTION_SHA256,
                          ao_evidence_closure.PART: ao_evidence_closure.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_finding_provenance.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)
        for other_text in (ao_residual_escalation.INSTRUCTION, ao_progress.INSTRUCTION_V2,
                           ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_V3,
                           ao_read_admission.INSTRUCTION, ao_quality_review.INSTRUCTION,
                           ao_execution_evidence.INSTRUCTION, lifecycle_closure.INSTRUCTION):
            self.assertNotIn(other_text, request['text'])

        text2, carried2 = self.packet()
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(ao_finding_provenance.INSTRUCTION, text2)
        self.assertNotIn(ao_evidence_closure.INSTRUCTION, text2)

    def test_uncertain_carried_request_is_undelivered_until_a_verified_completion(self):
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('receipt', request)
        self.assertEqual(request['carried']['parts'], [ao_finding_provenance.PART, ao_evidence_closure.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_finding_provenance.PART: ao_finding_provenance.INSTRUCTION_SHA256,
                          ao_evidence_closure.PART: ao_evidence_closure.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_finding_provenance.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)

        # Not a completed verified turn: the controller withholds credit for it.
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_finding_provenance.PART, held['parts'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_finding_provenance.PART, summary['undelivered_parts'])
        self.assertIn(ao_evidence_closure.PART, summary['undelivered_parts'])

        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['corr-uncertain']['state'], 'completed')

        held2 = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertIn(ao_finding_provenance.PART, held2['parts'])
        self.assertIn(ao_evidence_closure.PART, held2['parts'])
        summary2 = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_finding_provenance.PART, summary2['undelivered_parts'])
        self.assertNotIn(ao_evidence_closure.PART, summary2['undelivered_parts'])
        text, carried = self.packet()
        self.assertEqual(carried['parts'], [])
        self.assertNotIn(ao_finding_provenance.INSTRUCTION, text)
        self.assertNotIn(ao_evidence_closure.INSTRUCTION, text)

    def test_a_downgraded_controller_refuses_the_carried_record_without_repair(self):
        # Rollback boundary: once a completed engineer turn has carried the new parts, the
        # previous PARTS vocabulary cannot read that session's delivered-context record, and
        # the refusal repairs nothing — the saved receipt bytes are untouched throughout.
        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['state'], 'completed')
        self.assertEqual(request['carried']['parts'], [ao_finding_provenance.PART, ao_evidence_closure.PART])
        receipt_bytes = (self.directory() / request['receipt']).read_bytes()
        # The current vocabulary reads the same record fine.
        record, names = ao_workflow.carried_by(request)
        self.assertEqual(names, (ao_finding_provenance.PART, ao_evidence_closure.PART))
        with patch.object(ao_workflow, 'PARTS', OLDER_PARTS):
            with self.assertRaisesRegex(
                    RoomError, 'Delivered-context record is inconsistent; preserve the '
                               'evidence, do not resend automatically'):
                ao_workflow.carried_by(request)
        self.assertEqual((self.directory() / request['receipt']).read_bytes(), receipt_bytes)

    def test_packet_is_idempotent_for_the_same_undelivered_state(self):
        state = self.state()
        text1, carried1 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        text2, carried2 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        self.assertEqual(text1, text2)
        self.assertEqual(carried1, carried2)
        self.assertEqual(text1.count(ao_finding_provenance.INSTRUCTION), 1)
        self.assertEqual(text1.count(ao_evidence_closure.INSTRUCTION), 1)
        self.assertEqual(carried1['parts'], [ao_finding_provenance.PART, ao_evidence_closure.PART])

    def test_context_summary_and_delivered_report_undelivered_then_delivered_without_mutation(self):
        state = self.state()
        before = json.dumps(state, sort_keys=True)
        session_id = state['bindings']['engineer']['session_id']
        summary = ao_workflow.context_summary(state, self.directory())
        held = ao_workflow.delivered(state, session_id, self.directory())
        after = json.dumps(state, sort_keys=True)
        self.assertEqual(before, after)
        self.assertIn(ao_finding_provenance.PART, summary['undelivered_parts'])
        self.assertIn(ao_evidence_closure.PART, summary['undelivered_parts'])
        self.assertNotIn(ao_finding_provenance.PART, held['parts'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

        state2 = self.state()
        summary2 = ao_workflow.context_summary(state2, self.directory())
        self.assertNotIn(ao_finding_provenance.PART, summary2['undelivered_parts'])
        self.assertNotIn(ao_evidence_closure.PART, summary2['undelivered_parts'])


class CombinedPartsDeliveryTests(fixtures.Fixture):
    """A session retained from before any of the last three parts existed receives
    execution_evidence_v1, finding_provenance_v1 and evidence_closure_v1 once, together, in PARTS
    order (evidence_closure_v1 was appended after this unit; see test_ao_evidence_closure.py)."""

    def setUp(self):
        super().setUp()
        with patch.object(ao_workflow, 'PARTS', PRE_EXECUTION_PARTS):
            self.room = self.open()
            self.spec()
            self.bind()
            self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def receipt_digests(self):
        digests = {}
        for key, request in self.state()['requests'].items():
            receipt = request.get('receipt')
            if receipt is not None:
                digests[key] = (self.directory() / receipt).read_bytes()
        return digests

    def test_a_pre_execution_session_receives_both_new_parts_once_in_parts_order(self):
        before = self.receipt_digests()
        session_id = self.state()['bindings']['engineer']['session_id']
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_execution_evidence.PART, held['parts'])
        self.assertNotIn(ao_finding_provenance.PART, held['parts'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'],
                         [ao_execution_evidence.PART, ao_finding_provenance.PART, ao_evidence_closure.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_execution_evidence.PART: ao_execution_evidence.INSTRUCTION_SHA256,
                          ao_finding_provenance.PART: ao_finding_provenance.INSTRUCTION_SHA256,
                          ao_evidence_closure.PART: ao_evidence_closure.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_execution_evidence.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_finding_provenance.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)
        # Every pre-existing receipt's bytes are unchanged by the new send.
        for key, saved in before.items():
            self.assertEqual((self.directory() / self.state()['requests'][key]['receipt']).read_bytes(),
                             saved)

        # The next send after that verified completion carries neither.
        self.send('correction', 'corr-2')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        carried2 = self.state()['requests']['corr-2']['carried']
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(ao_execution_evidence.INSTRUCTION,
                         self.state()['requests']['corr-2']['text'])
        self.assertNotIn(ao_finding_provenance.INSTRUCTION,
                         self.state()['requests']['corr-2']['text'])
        self.assertNotIn(ao_evidence_closure.INSTRUCTION,
                         self.state()['requests']['corr-2']['text'])

    def test_an_uncertain_turn_leaves_both_new_parts_undelivered(self):
        before = self.receipt_digests()
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['carried']['parts'],
                         [ao_execution_evidence.PART, ao_finding_provenance.PART, ao_evidence_closure.PART])
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_execution_evidence.PART, held['parts'])
        self.assertNotIn(ao_finding_provenance.PART, held['parts'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_execution_evidence.PART, summary['undelivered_parts'])
        self.assertIn(ao_finding_provenance.PART, summary['undelivered_parts'])
        self.assertIn(ao_evidence_closure.PART, summary['undelivered_parts'])
        for key, saved in before.items():
            self.assertEqual((self.directory() / self.state()['requests'][key]['receipt']).read_bytes(),
                             saved)


class HistoricalSessionDeliveryTests(fixtures.Fixture):
    """A historical packet recorded without a carried key still classifies through HISTORICAL_PARTS,
    and the new part is included once among what remains undelivered on the next real packet."""

    historical_request = test_ao_continuation.ContinuationTests.historical_request
    historical_review = test_ao_continuation.ContinuationTests.historical_review

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()

    def test_new_part_is_undelivered_after_a_historical_implementation_record(self):
        self.historical_review()
        self.service.ao_room_handoff(self.room, str(self.repo))
        state = self.state()
        spec = self.service.spec(self.directory(), state)
        response = json.dumps(self.report(outcome='changes_required', implementation_complete=False,
                                          remaining_gaps=['Unfinished work']))
        for purpose in ('implementation', 'correction'):
            key = 'old-' + purpose
            text = ('[Project Room historical request ' + purpose + ']' + chr(10)
                    + 'Historical workflow/policy/report/settings/routing.' + chr(10)
                    + 'Exact specification revision 1, SHA256 ' + spec['sha256'] + chr(10)
                    + '<specification>' + chr(10) + spec['content'] + chr(10) + '</specification>' + chr(10)
                    + 'Agreed gates: ' + json.dumps(spec['gates']) + chr(10) + 'Task instruction:' + chr(10) + 'Continue.')
            old = self.historical_request(key, purpose, text, response)
            self.assertNotIn('carried', old)

        self.send('correction', 'new-correction')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['new-correction']
        self.assertEqual(request['text'].count(ao_finding_provenance.INSTRUCTION), 1)
        self.assertIn(ao_finding_provenance.PART, request['carried']['parts'])
        self.assertEqual(request['carried']['parts'].count(ao_finding_provenance.PART), 1)
        self.assertEqual(request['carried']['part_sha256'][ao_finding_provenance.PART],
                         ao_finding_provenance.INSTRUCTION_SHA256)
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, request['text'])


if __name__ == '__main__':
    unittest.main()
