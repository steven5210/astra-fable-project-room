"""Evidence-closure one-time workflow-part module: offline, synthetic fixtures only.

Mirrors the frozen-module checks test_lifecycle_closure.py runs for its sibling
one-time parts, plus the delivery/packet assertions that go with this unit's own
ao_workflow.PARTS registration: a new session's first packet carries it, a retained
session receives it exactly once, status/summary and delivered() track it correctly,
carried_by accepts the saved record, it is never superseded or historical, and
earlier parts' texts, digests and receipts stay untouched.
"""
import hashlib
import inspect
import json
import re
import unittest
from unittest.mock import patch

import ao_evidence_closure
import ao_execution_evidence
import ao_finding_provenance
import ao_progress
import ao_quality_review
import ao_read_admission
import ao_residual_escalation
import ao_workflow
import lifecycle_closure
import project_room_runtime
from room import RoomError
import test_ao_continuation
import test_ao_normal as fixtures

UUID_SHAPE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# The PARTS tuple as it stood immediately before this unit: every name except
# evidence_closure_v1, taken from the live tuple so a change to the historical ordering or the
# new element is caught, not just inferred from today's value.
OLDER_PARTS = tuple(p for p in ao_workflow.PARTS if p != ao_evidence_closure.PART)

# Independently copied pinned digests for the sibling one-time parts this unit's own requirement
# names explicitly. Comparing each against both the live module constant and this literal catches
# drift in either the text or its pin, never only one of the two.
PROGRESS_SHA256 = "983fbab2fe00e5c0f432567d5a18d05a5581b81a93f321442944f07f320823ae"
PROGRESS_V2_SHA256 = "3ee80001834443cbdcdf06c15c42692db42e14e5906c46ad2a42249420b78ee0"
PROGRESS_V3_SHA256 = "9e559c9b963f8a883a979f1f97e3936aba0d2df5c7f7b5c682634da3380b665b"
QUALITY_REVIEW_SHA256 = "6d043563ae4c27854dbaad53b9762476efa189c9fb6565dfb5670e24cee47a4e"
LIFECYCLE_CLOSURE_SHA256 = "d9db1bee4d3b65b301ab7f3508ca2d71a7b16de7f769626f78415f50715a22da"
EXECUTION_EVIDENCE_SHA256 = "4636e2c8ed80f6e748c538485d1c788d4ba6b9e5734efa3ca1e0aa4b53ff3574"
FINDING_PROVENANCE_SHA256 = "d67ce9529889ce53b817988c386774148eb92909dcec76d6ea6faa78eb2ad8ec"

# Independent literal pin for this module's own text, exactly like the sibling pins above: comparing
# it against both the live INSTRUCTION_SHA256 constant and a freshly computed digest of the live
# INSTRUCTION catches drift in the text, the pin, or both, rather than only a self-consistency check.
EVIDENCE_CLOSURE_SHA256 = "3ef5a1ea29407100275ace4c98705070f5322caad4033264cc1ba5c886ec3ea1"


class FrozenModulePatternTests(unittest.TestCase):
    """The same frozen-module shape as lifecycle_closure.py, ao_execution_evidence.py
    and ao_finding_provenance.py: a pinned version, part name and literal digest."""

    def test_version_is_1(self):
        self.assertEqual(ao_evidence_closure.VERSION, 1)

    def test_part_name(self):
        self.assertEqual(ao_evidence_closure.PART, "evidence_closure_v1")

    def test_instruction_digest_matches_its_pin(self):
        computed = hashlib.sha256(ao_evidence_closure.INSTRUCTION.encode("utf-8")).hexdigest()
        self.assertEqual(computed, ao_evidence_closure.INSTRUCTION_SHA256)
        # Independent literal, not merely the module comparing its own text to its own pin.
        self.assertEqual(computed, EVIDENCE_CLOSURE_SHA256)

    def test_module_source_has_no_import_statements(self):
        # Same frozen-module guarantee the siblings carry: nothing computes or
        # reaches outside this module to produce VERSION/PART/INSTRUCTION*.
        source = inspect.getsource(ao_evidence_closure)
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("import") or stripped.startswith("from "),
                             "unexpected import line in ao_evidence_closure.py: " + repr(line))


class RuntimeRosterTests(unittest.TestCase):
    """3b-equivalent: the new module and its concurrently-added sibling are both
    retained so an installed runtime carries them (project_room_runtime.RUNTIME_FILES)."""

    def test_both_new_modules_are_in_the_retained_runtime_roster(self):
        self.assertIn("ao_evidence_closure.py", project_room_runtime.RUNTIME_FILES)
        self.assertIn("ao_report_validation.py", project_room_runtime.RUNTIME_FILES)


class InstructionContentTests(unittest.TestCase):
    """Bounded, generalized, product-neutral text: no scope grant, no private
    paths or session identifiers, and it actually names the validator it documents."""

    def test_instruction_grants_no_scope(self):
        self.assertIn("grants no scope", ao_evidence_closure.INSTRUCTION)

    def test_instruction_names_the_validator_tool(self):
        self.assertIn("ao_report_validate", ao_evidence_closure.INSTRUCTION)

    def test_instruction_is_at_most_4000_characters(self):
        self.assertLessEqual(len(ao_evidence_closure.INSTRUCTION), 4000)

    def test_instruction_has_no_machine_specific_path(self):
        self.assertNotIn("/Users/", ao_evidence_closure.INSTRUCTION)

    def test_instruction_has_no_uuid_shaped_text(self):
        self.assertIsNone(UUID_SHAPE.search(ao_evidence_closure.INSTRUCTION))


class SiblingPartDigestTests(unittest.TestCase):
    """Registering evidence_closure_v1 changes none of the other frozen one-time parts' bytes or
    pins, and the new part is never classified as superseded or historical."""

    def test_named_sibling_instruction_digests_are_unchanged(self):
        cases = (
            ("ao_progress.INSTRUCTION", ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_SHA256, PROGRESS_SHA256),
            ("ao_progress.INSTRUCTION_V2", ao_progress.INSTRUCTION_V2, ao_progress.INSTRUCTION_V2_SHA256, PROGRESS_V2_SHA256),
            ("ao_progress.INSTRUCTION_V3", ao_progress.INSTRUCTION_V3, ao_progress.INSTRUCTION_V3_SHA256, PROGRESS_V3_SHA256),
            ("ao_quality_review.INSTRUCTION", ao_quality_review.INSTRUCTION,
             ao_quality_review.INSTRUCTION_SHA256, QUALITY_REVIEW_SHA256),
            ("lifecycle_closure.INSTRUCTION", lifecycle_closure.INSTRUCTION,
             lifecycle_closure.INSTRUCTION_SHA256, LIFECYCLE_CLOSURE_SHA256),
            ("ao_execution_evidence.INSTRUCTION", ao_execution_evidence.INSTRUCTION,
             ao_execution_evidence.INSTRUCTION_SHA256, EXECUTION_EVIDENCE_SHA256),
            ("ao_finding_provenance.INSTRUCTION", ao_finding_provenance.INSTRUCTION,
             ao_finding_provenance.INSTRUCTION_SHA256, FINDING_PROVENANCE_SHA256),
        )
        for label, text, pinned, literal in cases:
            with self.subTest(part=label):
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), pinned)
                self.assertEqual(pinned, literal)

    def test_evidence_closure_is_not_superseded_or_historical(self):
        # evidence_closure_v1 never appears in ao_progress.SUPERSEDED_PARTS (so it is never
        # silently skipped the way progress_plan_v1/v2 are) or in any HISTORICAL_PARTS tuple (so a
        # packet recorded before carried-context notes existed is never misread as having carried
        # it).
        self.assertNotIn(ao_evidence_closure.PART, ao_progress.SUPERSEDED_PARTS)
        for purpose, names in ao_workflow.HISTORICAL_PARTS.items():
            with self.subTest(purpose=purpose):
                self.assertNotIn(ao_evidence_closure.PART, names)


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

    def test_evidence_closure_part_delivered_once_on_the_first_packet(self):
        request = self.state()['requests']['spec_review']
        carried = request['carried']
        self.assertEqual(carried['parts'].count(ao_evidence_closure.PART), 1)
        # evidence_closure_v1 is the newest registered part, so it is always last.
        self.assertEqual(carried['parts'][-1], ao_evidence_closure.PART)
        self.assertEqual(carried['part_sha256'][ao_evidence_closure.PART],
                         ao_evidence_closure.INSTRUCTION_SHA256)
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)
        # Already delivered with the first packet; a later packet in the same session omits it.
        self.assertNotIn(ao_evidence_closure.PART, self.state()['requests']['impl-1']['carried']['parts'])
        self.assertNotIn(ao_evidence_closure.INSTRUCTION, self.state()['requests']['impl-1']['text'])


class RetainedSessionDeliveryTests(fixtures.Fixture):
    """A session retained from before this part existed gets it exactly once, then nothing;
    status/summary and delivered() track it correctly; carried_by accepts the saved record."""

    def setUp(self):
        super().setUp()
        # Simulate a session whose first packet predates only evidence_closure_v1: every older
        # part was already delivered, so the next engineer send carries just the new part.
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
        self.assertNotIn(ao_evidence_closure.PART, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'], [ao_evidence_closure.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_evidence_closure.PART: ao_evidence_closure.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)
        # No other part's text is repeated: every older part (including execution_evidence_v1 and
        # finding_provenance_v1) was already delivered under OLDER_PARTS.
        for other_text in (ao_residual_escalation.INSTRUCTION, ao_progress.INSTRUCTION_V2,
                           ao_progress.INSTRUCTION, ao_progress.INSTRUCTION_V3,
                           ao_read_admission.INSTRUCTION, ao_quality_review.INSTRUCTION,
                           ao_execution_evidence.INSTRUCTION, ao_finding_provenance.INSTRUCTION,
                           lifecycle_closure.INSTRUCTION):
            self.assertNotIn(other_text, request['text'])

        # carried_by accepts the saved record and names this part among the ones it carried.
        record, names = ao_workflow.carried_by(request)
        self.assertIn(ao_evidence_closure.PART, names)

        # After delivery: a further completed request carrying only the new part leaves nothing
        # left to send; the next packet is the caller's bytes only.
        text2, carried2 = self.packet()
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(ao_evidence_closure.INSTRUCTION, text2)

    def test_uncertain_carried_request_is_undelivered_until_a_verified_completion(self):
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('receipt', request)
        self.assertEqual(request['carried']['parts'], [ao_evidence_closure.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_evidence_closure.PART: ao_evidence_closure.INSTRUCTION_SHA256})
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)

        # Not a completed verified turn: the controller withholds credit for it.
        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_evidence_closure.PART, summary['undelivered_parts'])

        # The completed-request pattern: resolve the identical request (no replay) to a
        # completed, verified turn, then confirm the new part is delivered and credited.
        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['corr-uncertain']['state'], 'completed')

        held2 = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertIn(ao_evidence_closure.PART, held2['parts'])
        summary2 = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_evidence_closure.PART, summary2['undelivered_parts'])
        text, carried = self.packet()
        self.assertEqual(carried['parts'], [])
        self.assertNotIn(ao_evidence_closure.INSTRUCTION, text)

    def test_packet_is_idempotent_for_the_same_undelivered_state(self):
        state = self.state()
        text1, carried1 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        text2, carried2 = ao_workflow.packet(self.service, self.directory(), state, 'engineer',
                                             'correction', 'Continue.')
        self.assertEqual(text1, text2)
        self.assertEqual(carried1, carried2)
        self.assertEqual(text1.count(ao_evidence_closure.INSTRUCTION), 1)
        self.assertEqual(carried1['parts'], [ao_evidence_closure.PART])

    def test_context_summary_and_delivered_report_undelivered_then_delivered_without_mutation(self):
        state = self.state()
        before = json.dumps(state, sort_keys=True)
        session_id = state['bindings']['engineer']['session_id']
        summary = ao_workflow.context_summary(state, self.directory())
        held = ao_workflow.delivered(state, session_id, self.directory())
        after = json.dumps(state, sort_keys=True)
        self.assertEqual(before, after)
        self.assertIn(ao_evidence_closure.PART, summary['undelivered_parts'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])

        # Earlier parts' own saved receipt bytes are untouched by later delivering the new one.
        spec_review = self.state()['requests']['spec_review']
        spec_review_receipt_before = (self.directory() / spec_review['receipt']).read_bytes()
        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        spec_review_receipt_after = (self.directory() / spec_review['receipt']).read_bytes()
        self.assertEqual(spec_review_receipt_before, spec_review_receipt_after)

        state2 = self.state()
        summary2 = ao_workflow.context_summary(state2, self.directory())
        self.assertNotIn(ao_evidence_closure.PART, summary2['undelivered_parts'])


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
        self.assertEqual(request['text'].count(ao_evidence_closure.INSTRUCTION), 1)
        self.assertIn(ao_evidence_closure.PART, request['carried']['parts'])
        self.assertEqual(request['carried']['parts'].count(ao_evidence_closure.PART), 1)
        self.assertEqual(request['carried']['part_sha256'][ao_evidence_closure.PART],
                         ao_evidence_closure.INSTRUCTION_SHA256)
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])


class OnceOnlyExclusionProofTests(fixtures.Fixture):
    """Two further once-only delivery proofs, beyond RetainedSessionDeliveryTests above: the part is
    named undelivered before any send ever reaches the engineer session, and an attempted send that a
    semantic hold refuses never advances delivery credit or posts, exactly like any other refused
    ao_room_send."""

    def test_part_is_named_undelivered_before_the_first_send_with_zero_posts(self):
        # After bind, before any engineer send exists at all: no completed turn has carried
        # anything yet, so every part -- including evidence_closure_v1 -- is still undelivered,
        # and this read-only status call itself never posts.
        self.room = self.open()
        self.spec()
        self.bind()
        status = self.service.ao_room_status(self.room)
        self.assertIn(ao_evidence_closure.PART, status['engineer_context']['undelivered_parts'])
        self.assertEqual(len(self.fake.posts), 0)
        # Confirmed from the same offline projection a live send would consult: delivered() finds no
        # completed engineer turn yet, so the part is credited to no one.
        held = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'])
        self.assertNotIn(ao_evidence_closure.PART, held['parts'])

    def test_a_held_room_refuses_the_send_without_crediting_delivery_or_posting(self):
        # Simulate a session whose first packet predates evidence_closure_v1 (exactly like
        # RetainedSessionDeliveryTests.setUp), but let the one implementation turn finish with no
        # final response at all: ao_outcomes classifies that as an unconditional semantic hold
        # (kind "unknown"), entirely from the real completed-turn evidence, not a patched field.
        with patch.object(ao_workflow, 'PARTS', OLDER_PARTS):
            self.room = self.open()
            self.spec()
            self.bind()
            self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', '')
            self.service.ao_room_sync(self.room)
        request = self.state()['requests']['impl-1']
        self.assertIn('no final response', request['engineering_error'])
        self.assertEqual(ao_workflow.engineering_capture(self.state())['status'], 'held')
        session_id = self.state()['bindings']['engineer']['session_id']
        held_before = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_evidence_closure.PART, held_before['parts'])
        summary_before = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_evidence_closure.PART, summary_before['undelivered_parts'])

        state_before = json.dumps(self.state(), sort_keys=True)
        posts_before = len(self.fake.posts)
        # Back on the real, unpatched PARTS: a successful correction here would deliver
        # evidence_closure_v1 for the first time, but the room is held, so ao_outcomes.gate must
        # refuse before any packet is assembled or posted -- the same boundary
        # test_ao_normal.py's test_missing_final_can_be_corrected_but_modified_receipt_cannot
        # already demonstrates for the engineering_error/semantic-hold distinction itself.
        with self.assertRaisesRegex(RoomError, 'Native semantic hold'):
            self.send('correction', 'corr-1')
        self.assertEqual(len(self.fake.posts), posts_before)
        self.assertEqual(json.dumps(self.state(), sort_keys=True), state_before)
        self.assertNotIn('corr-1', self.state()['requests'])

        held_after = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_evidence_closure.PART, held_after['parts'])
        summary_after = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_evidence_closure.PART, summary_after['undelivered_parts'])


if __name__ == "__main__":
    unittest.main()
