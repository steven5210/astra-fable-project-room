"""Lifecycle-closure one-time workflow-part delivery: offline, synthetic fixtures only."""
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

import ao_progress
import ao_quality_review
import ao_read_admission
import ao_report_contract
import ao_residual_escalation
import ao_review_followups
import ao_workflow
import lifecycle_closure
import test_ao_continuation
import test_ao_normal as fixtures

# The 14-name PARTS tuple as it stood immediately before this unit, copied literally so a change to
# either the historical ordering or the new element is caught, not just inferred from today's value.
OLD_PARTS = ("review_contract", "report_contract", "policy", "settings", "routing", "baseline_rule",
             "efficiency_contract_v1", "delegation_efficiency_v2", "review_first_routing_v1",
             "quality_first_review_v1", "progress_plan_v1", "read_admission_v1", "progress_plan_v2",
             "residual_escalation_v1")

# Independently copied pinned digests for the other existing frozen one-time workflow parts. Comparing
# each against both the live module constant and this literal catches drift in either the text or its
# pin, never only one of the two.
PROGRESS_SHA256 = "983fbab2fe00e5c0f432567d5a18d05a5581b81a93f321442944f07f320823ae"
PROGRESS_V2_SHA256 = "3ee80001834443cbdcdf06c15c42692db42e14e5906c46ad2a42249420b78ee0"
RESIDUAL_ESCALATION_SHA256 = "86a1faed57c2119fc492182508cec813d7704318382c11db9380219e81b190e4"
QUALITY_REVIEW_SHA256 = "6d043563ae4c27854dbaad53b9762476efa189c9fb6565dfb5670e24cee47a4e"
READ_ADMISSION_SHA256 = "6fa2a1310c6d724334b2d32e1fc6a5c91811b8e4962b1ac3057348e146d79e99"


class DigestAndFrozenBytesTests(unittest.TestCase):
    """3a/3b: the new module's own pin, and every existing frozen byte stays exactly as recorded."""

    def test_lifecycle_closure_digest_matches_its_pin_and_part_name(self):
        self.assertEqual(hashlib.sha256(lifecycle_closure.INSTRUCTION.encode()).hexdigest(),
                         lifecycle_closure.INSTRUCTION_SHA256)
        self.assertEqual(lifecycle_closure.PART, "lifecycle_closure_v1")

    def test_lifecycle_closure_module_source_has_no_import_statements(self):
        source = inspect.getsource(lifecycle_closure)
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("import") or stripped.startswith("from "),
                             "unexpected import line in lifecycle_closure.py: " + repr(line))

    def test_parts_tuple_keeps_history_and_appends_the_new_part_last(self):
        # Re-pinned for progress_plan_v3: OLD_PARTS (14 names) is still a frozen prefix, lifecycle_closure_v1
        # is still the 15th name, and progress_plan_v3 is appended as the new 16th and last name.
        self.assertEqual(ao_workflow.PARTS[:14], OLD_PARTS)
        self.assertEqual(ao_workflow.PARTS[14], "lifecycle_closure_v1")
        self.assertEqual(ao_workflow.PARTS[15], "progress_plan_v3")
        self.assertEqual(len(ao_workflow.PARTS), 16)

    def test_existing_frozen_part_digests_are_unchanged(self):
        cases = (
            ("ao_progress.INSTRUCTION", ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_SHA256, PROGRESS_SHA256),
            ("ao_progress.INSTRUCTION_V2", ao_progress.INSTRUCTION_V2, ao_progress.INSTRUCTION_V2_SHA256, PROGRESS_V2_SHA256),
            ("ao_residual_escalation.INSTRUCTION", ao_residual_escalation.INSTRUCTION,
             ao_residual_escalation.INSTRUCTION_SHA256, RESIDUAL_ESCALATION_SHA256),
            ("ao_quality_review.INSTRUCTION", ao_quality_review.INSTRUCTION,
             ao_quality_review.INSTRUCTION_SHA256, QUALITY_REVIEW_SHA256),
            ("ao_read_admission.INSTRUCTION", ao_read_admission.INSTRUCTION,
             ao_read_admission.INSTRUCTION_SHA256, READ_ADMISSION_SHA256),
        )
        for label, text, pinned, literal in cases:
            with self.subTest(part=label):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), pinned)
                self.assertEqual(pinned, literal)

    def test_report_contract_and_review_followups_expose_no_pinned_digest_today(self):
        # Verified current fact: neither module pins an INSTRUCTION_SHA256-style digest constant.
        # If a future change adds one, this assertion should start failing and be updated alongside it.
        self.assertFalse(hasattr(ao_report_contract, "INSTRUCTION_SHA256"))
        self.assertFalse(hasattr(ao_review_followups, "INSTRUCTION_SHA256"))


class NewSessionDeliveryTests(fixtures.Fixture):
    """3c: a brand-new retained session receives the new part exactly once, on its first packet."""

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

    def test_lifecycle_closure_part_delivered_once_on_the_first_packet(self):
        request = self.state()['requests']['spec_review']
        carried = request['carried']
        self.assertEqual(carried['parts'].count(lifecycle_closure.PART), 1)
        self.assertEqual(carried['part_sha256'][lifecycle_closure.PART], lifecycle_closure.INSTRUCTION_SHA256)
        self.assertEqual(request['text'].count(lifecycle_closure.INSTRUCTION), 1)
        # Already delivered with the first packet; a later packet in the same session omits it.
        self.assertNotIn(lifecycle_closure.PART, self.state()['requests']['impl-1']['carried']['parts'])
        self.assertNotIn(lifecycle_closure.INSTRUCTION, self.state()['requests']['impl-1']['text'])


class RetainedSessionDeliveryTests(fixtures.Fixture):
    """3d/3f/3g: a session retained from before this part existed gets it exactly once, then nothing."""

    def setUp(self):
        super().setUp()
        with patch.object(ao_workflow, 'PARTS', OLD_PARTS):
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
        # Before delivery: the retained session's own PARTS tuple (the real, unpatched one) does not
        # yet list the new part among what it has received.
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertNotIn(lifecycle_closure.PART, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        # Re-pinned for progress_plan_v3: this retained (OLD_PARTS) session is missing both of the
        # two parts added since, so a single correction now carries lifecycle_closure_v1 *and*
        # progress_plan_v3 together, in PARTS order.
        self.assertEqual(request['carried']['parts'], [lifecycle_closure.PART, ao_progress.PART_V3])
        self.assertEqual(request['carried']['part_sha256'],
                          {lifecycle_closure.PART: lifecycle_closure.INSTRUCTION_SHA256,
                           ao_progress.PART_V3: ao_progress.INSTRUCTION_V3_SHA256})
        self.assertEqual(request['text'].count(lifecycle_closure.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)
        # No other part's text is repeated: every older part was already delivered under OLD_PARTS,
        # and progress_plan_v1/v2 are additionally superseded so they are never resent regardless.
        for other_text in (ao_residual_escalation.INSTRUCTION, ao_progress.INSTRUCTION_V2,
                           ao_progress.INSTRUCTION, ao_read_admission.INSTRUCTION, ao_quality_review.INSTRUCTION):
            self.assertNotIn(other_text, request['text'])

        # After delivery: a further completed request carrying only the two new parts leaves nothing
        # left to send; the next packet is the caller's bytes only.
        text2, carried2 = self.packet()
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(lifecycle_closure.INSTRUCTION, text2)
        self.assertNotIn(ao_progress.INSTRUCTION_V3, text2)

    def test_uncertain_carried_request_is_undelivered_until_a_verified_completion(self):
        # Same lost-ack fixture shape as test_ao_continuation.ContinuationTests.
        # test_lost_ack_reconciles_same_text_without_replay and test_ao_model_boundaries.
        # FrozenExpectationTests.test_uncertain_notice_delivery_is_held_and_never_silently_retried:
        # the native acknowledgement is lost, so this request carries the new part while its own
        # state is "uncertain" -- never completed, never settled_failure, and with no saved receipt.
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('receipt', request)
        # Re-pinned for progress_plan_v3: the same uncertain turn carries both new parts together.
        self.assertEqual(request['carried']['parts'], [lifecycle_closure.PART, ao_progress.PART_V3])
        self.assertEqual(request['carried']['part_sha256'],
                          {lifecycle_closure.PART: lifecycle_closure.INSTRUCTION_SHA256,
                           ao_progress.PART_V3: ao_progress.INSTRUCTION_V3_SHA256})
        self.assertEqual(request['text'].count(lifecycle_closure.INSTRUCTION), 1)
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)

        # Not a completed verified turn: the controller withholds credit for it.
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(lifecycle_closure.PART, held['parts'])
        self.assertNotIn(ao_progress.PART_V3, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(lifecycle_closure.PART, summary['undelivered_parts'])
        self.assertIn(ao_progress.PART_V3, summary['undelivered_parts'])

        # The completed-request pattern: resolve the identical request (no replay) to a
        # completed, verified turn, then confirm nothing is left to deliver.
        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['corr-uncertain']['state'], 'completed')

        held2 = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertIn(lifecycle_closure.PART, held2['parts'])
        self.assertIn(ao_progress.PART_V3, held2['parts'])
        summary2 = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(lifecycle_closure.PART, summary2['undelivered_parts'])
        self.assertNotIn(ao_progress.PART_V3, summary2['undelivered_parts'])
        text, carried = self.packet()
        self.assertEqual(carried['parts'], [])
        self.assertNotIn(lifecycle_closure.INSTRUCTION, text)
        self.assertNotIn(ao_progress.INSTRUCTION_V3, text)

    def test_packet_is_idempotent_for_the_same_undelivered_state(self):
        state = self.state()
        text1, carried1 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        text2, carried2 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        self.assertEqual(text1, text2)
        self.assertEqual(carried1, carried2)
        self.assertEqual(text1.count(lifecycle_closure.INSTRUCTION), 1)
        self.assertEqual(text1.count(ao_progress.INSTRUCTION_V3), 1)
        # Re-pinned for progress_plan_v3: both new parts are carried together, in PARTS order.
        self.assertEqual(carried1['parts'], [lifecycle_closure.PART, ao_progress.PART_V3])

    def test_context_summary_and_delivered_report_undelivered_then_delivered_without_mutation(self):
        state = self.state()
        before = json.dumps(state, sort_keys=True)
        session_id = state['bindings']['engineer']['session_id']
        summary = ao_workflow.context_summary(state, self.directory())
        held = ao_workflow.delivered(state, session_id, self.directory())
        after = json.dumps(state, sort_keys=True)
        self.assertEqual(before, after)
        self.assertIn(lifecycle_closure.PART, summary['undelivered_parts'])
        self.assertNotIn(lifecycle_closure.PART, held['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

        state2 = self.state()
        summary2 = ao_workflow.context_summary(state2, self.directory())
        self.assertNotIn(lifecycle_closure.PART, summary2['undelivered_parts'])


class HistoricalSessionDeliveryTests(fixtures.Fixture):
    """3e: a historical packet recorded without a carried key still classifies through HISTORICAL_PARTS,
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
        self.assertEqual(request['text'].count(lifecycle_closure.INSTRUCTION), 1)
        self.assertIn(lifecycle_closure.PART, request['carried']['parts'])
        self.assertEqual(request['carried']['parts'].count(lifecycle_closure.PART), 1)
        self.assertEqual(request['carried']['part_sha256'][lifecycle_closure.PART], lifecycle_closure.INSTRUCTION_SHA256)
        # Truthful extension for progress_plan_v3: the same historical-fallback request also carries
        # it exactly once, alongside lifecycle_closure_v1, never progress_plan_v1/v2 (superseded).
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)
        self.assertEqual(request['carried']['parts'].count(ao_progress.PART_V3), 1)
        self.assertEqual(request['carried']['part_sha256'][ao_progress.PART_V3], ao_progress.INSTRUCTION_V3_SHA256)
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, request['text'])


if __name__ == '__main__':
    unittest.main()
