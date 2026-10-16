"""execution_evidence_v1 one-time workflow-part delivery: offline, synthetic fixtures only."""
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

import ao_execution_evidence
import ao_progress
import ao_workflow
import lifecycle_closure
import test_ao_continuation
import test_ao_normal as fixtures

# The 16-name PARTS tuple as it stood immediately before this unit, copied literally so a change to
# either the historical ordering or the new element is caught, not just inferred from today's value.
EARLIER_PARTS = ("review_contract", "report_contract", "policy", "settings", "routing", "baseline_rule",
                 "efficiency_contract_v1", "delegation_efficiency_v2", "review_first_routing_v1",
                 "quality_first_review_v1", "progress_plan_v1", "read_admission_v1", "progress_plan_v2",
                 "residual_escalation_v1", "lifecycle_closure_v1", "progress_plan_v3")

# Independently copied pinned digests for the frozen one-time workflow parts. Comparing each against
# both the live module constant and this literal catches drift in either the text or its pin, never
# only one of the two.
EXECUTION_EVIDENCE_SHA256 = "4636e2c8ed80f6e748c538485d1c788d4ba6b9e5734efa3ca1e0aa4b53ff3574"
LIFECYCLE_CLOSURE_SHA256 = "d9db1bee4d3b65b301ab7f3508ca2d71a7b16de7f769626f78415f50715a22da"
PROGRESS_SHA256 = "983fbab2fe00e5c0f432567d5a18d05a5581b81a93f321442944f07f320823ae"
PROGRESS_V2_SHA256 = "3ee80001834443cbdcdf06c15c42692db42e14e5906c46ad2a42249420b78ee0"
PROGRESS_V3_SHA256 = "9e559c9b963f8a883a979f1f97e3936aba0d2df5c7f7b5c682634da3380b665b"


class DigestAndFrozenBytesTests(unittest.TestCase):
    """The new module's own pin, and every existing frozen byte stays exactly as recorded."""

    def test_execution_evidence_digest_matches_its_pin_and_part_name(self):
        self.assertEqual(ao_execution_evidence.PART, "execution_evidence_v1")
        self.assertEqual(ao_execution_evidence.VERSION, 1)
        self.assertEqual(hashlib.sha256(ao_execution_evidence.INSTRUCTION.encode()).hexdigest(),
                         ao_execution_evidence.INSTRUCTION_SHA256)
        self.assertEqual(ao_execution_evidence.INSTRUCTION_SHA256, EXECUTION_EVIDENCE_SHA256)
        self.assertTrue(ao_execution_evidence.INSTRUCTION.startswith("Execution evidence"))
        self.assertNotIn("\n", ao_execution_evidence.INSTRUCTION)

    def test_execution_evidence_module_source_has_no_import_statements(self):
        source = inspect.getsource(ao_execution_evidence)
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("import") or stripped.startswith("from "),
                             "unexpected import line in ao_execution_evidence.py: " + repr(line))

    def test_parts_tuple_keeps_history_and_appends_the_new_part_last(self):
        # EARLIER_PARTS (16 names) is still a frozen prefix and execution_evidence_v1 is the new
        # 17th and last name; the exact order is independently pinned in test_lifecycle_closure.py.
        self.assertEqual(ao_workflow.PARTS[:16], EARLIER_PARTS)
        self.assertEqual(ao_workflow.PARTS[16], ao_execution_evidence.PART)
        self.assertEqual(len(ao_workflow.PARTS), 17)

    def test_existing_frozen_part_digests_are_unchanged(self):
        cases = (
            ("ao_progress.INSTRUCTION", ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_SHA256, PROGRESS_SHA256),
            ("ao_progress.INSTRUCTION_V2", ao_progress.INSTRUCTION_V2, ao_progress.INSTRUCTION_V2_SHA256,
             PROGRESS_V2_SHA256),
            ("ao_progress.INSTRUCTION_V3", ao_progress.INSTRUCTION_V3, ao_progress.INSTRUCTION_V3_SHA256,
             PROGRESS_V3_SHA256),
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

    def test_execution_evidence_part_delivered_once_on_the_first_packet(self):
        request = self.state()['requests']['spec_review']
        carried = request['carried']
        self.assertEqual(carried['parts'].count(ao_execution_evidence.PART), 1)
        self.assertEqual(carried['part_sha256'][ao_execution_evidence.PART],
                         ao_execution_evidence.INSTRUCTION_SHA256)
        self.assertEqual(request['text'].count(ao_execution_evidence.INSTRUCTION), 1)
        # The last carried part in PARTS order.
        self.assertEqual(carried['parts'][-1], ao_execution_evidence.PART)
        # Already delivered with the first packet; a later packet in the same session omits it.
        self.assertNotIn(ao_execution_evidence.PART,
                         self.state()['requests']['impl-1']['carried']['parts'])
        self.assertNotIn(ao_execution_evidence.INSTRUCTION, self.state()['requests']['impl-1']['text'])

    def test_context_summary_reports_the_part_as_verified_delivered(self):
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_execution_evidence.PART, summary['undelivered_parts'])


class RetainedSessionDeliveryTests(fixtures.Fixture):
    """A session retained from before this part existed gets it exactly once, then nothing."""

    def setUp(self):
        super().setUp()
        # Everything except the new part, so this session is missing exactly execution_evidence_v1.
        with patch.object(ao_workflow, 'PARTS', EARLIER_PARTS):
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
        # Before delivery: the retained session's delivered set does not yet hold the new part.
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertNotIn(ao_execution_evidence.PART, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        # This retained session is missing exactly the one appended part, so a single correction
        # carries it alone, exactly once.
        self.assertEqual(request['carried']['parts'], [ao_execution_evidence.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_execution_evidence.PART: ao_execution_evidence.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_execution_evidence.INSTRUCTION), 1)
        # No already-delivered part's text is repeated.
        self.assertNotIn(lifecycle_closure.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V3, request['text'])

        # After delivery: a further packet leaves nothing to send.
        text2, carried2 = self.packet()
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(ao_execution_evidence.INSTRUCTION, text2)

    def test_uncertain_carried_request_is_undelivered_until_a_verified_completion(self):
        # Same lost-ack fixture shape as test_lifecycle_closure's uncertain-delivery test: the
        # native acknowledgement is lost, so this request carries the new part while its own state
        # is "uncertain" -- never completed and with no saved receipt.
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('receipt', request)
        self.assertEqual(request['carried']['parts'], [ao_execution_evidence.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_execution_evidence.PART: ao_execution_evidence.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_execution_evidence.INSTRUCTION), 1)

        # Not a completed verified turn: the controller withholds credit for it.
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_execution_evidence.PART, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_execution_evidence.PART, summary['undelivered_parts'])

        # The completed-request pattern: resolve the identical request (no replay) to a
        # completed, verified turn, then confirm nothing is left to deliver.
        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['corr-uncertain']['state'], 'completed')

        held2 = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertIn(ao_execution_evidence.PART, held2['parts'])
        summary2 = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_execution_evidence.PART, summary2['undelivered_parts'])
        text, carried = self.packet()
        self.assertEqual(carried['parts'], [])
        self.assertNotIn(ao_execution_evidence.INSTRUCTION, text)

    def test_packet_is_idempotent_for_the_same_undelivered_state(self):
        state = self.state()
        text1, carried1 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        text2, carried2 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        self.assertEqual(text1, text2)
        self.assertEqual(carried1, carried2)
        self.assertEqual(text1.count(ao_execution_evidence.INSTRUCTION), 1)
        self.assertEqual(carried1['parts'], [ao_execution_evidence.PART])


class HistoricalSessionDeliveryTests(fixtures.Fixture):
    """A historical packet recorded without a carried key still classifies through HISTORICAL_PARTS,
    and the new part is included once among what remains undelivered on the next real packet."""

    # Literal reuse of the proven fixture helpers from test_ao_continuation.ContinuationTests: same
    # function objects, bound to this fixture's own instances, without inheriting its test_* methods.
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
        self.assertEqual(request['text'].count(ao_execution_evidence.INSTRUCTION), 1)
        self.assertIn(ao_execution_evidence.PART, request['carried']['parts'])
        self.assertEqual(request['carried']['parts'].count(ao_execution_evidence.PART), 1)
        self.assertEqual(request['carried']['part_sha256'][ao_execution_evidence.PART],
                         ao_execution_evidence.INSTRUCTION_SHA256)


if __name__ == '__main__':
    unittest.main()
