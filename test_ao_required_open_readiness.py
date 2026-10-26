"""An explicit scope=required disposition=open finding refuses engineering readiness (#234).

The refusal is a service-level RoomError before any gate runs; Service/MCP surfaces only, fake
backends, no account, network, AO process or model call.
"""

import json
import unittest
from unittest.mock import patch

from test_ao_lifecycle_contract import AgreedRoomFixture

OPEN = ('stale lock order invariant=lock_order owner=EXIT-5 origin=introduced scope=required '
        'disposition=open')
REFUSAL = (r'Engineering reports completion while a required finding is open: '
           r'review_findings\[0\] "stale lock order" \(invariant=lock_order, owner=EXIT-5\)')


class RequiredOpenReadinessTests(AgreedRoomFixture):
    """Exact refusals, unchanged earlier checks, and the bounded redacted message."""

    def implement_with(self, **report_changes):
        self.send('implementation', 'implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report(**report_changes)))
        self.service.ao_room_sync(self.room)
        return self.state()['requests']['implementation']

    def test_complete_report_with_an_explicit_open_required_finding_is_refused_before_any_gate(self):
        request = self.implement_with(review_findings=[OPEN])
        self.assertEqual(request['state'], 'completed')
        self.assertIn('engineering_record', request)  # capture is not the gate
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['engineering_capture']['status'], 'captured')
        self.refuses(REFUSAL, self.service.ao_room_verify, self.room, str(self.repo))
        state = self.state()
        self.assertEqual(state['verifications'], [])
        self.assertIsNone(state['checkpoint'])
        self.assertFalse((self.directory() / 'verification').exists())  # no gate ran
        # Existing precondition order is unchanged: the review send still needs the checkpoint.
        self.refuses('Create a passed verification checkpoint first', self.send, 'acceptance_review')
        self.refuses(REFUSAL, self.service.ao_room_accept, self.room, 'acceptance_review')
        error = self.mcp_error('ao_room_verify', room_id=self.room, candidate_path=str(self.repo))
        self.assertIn('Engineering reports completion while a required finding is open', error)
        self.assertIn('review_findings[0] "stale lock order" (invariant=lock_order, owner=EXIT-5)',
                      error)
        state = self.state()
        self.assertEqual(state['acceptances'], [])
        self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_attempts'], 0)
        # The read-only disclosure agrees and is unchanged.
        codes = [item['code'] for item in
                 self.service.ao_room_status(self.room)['current_summary']['inconsistencies']]
        self.assertIn('engineering_complete_reported_with_open_required_findings', codes)

    def test_a_fixed_finding_lets_the_existing_sequence_proceed(self):
        self.implement_with(review_findings=[OPEN.replace('disposition=open', 'disposition=fixed')])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        findings = self.service.ao_room_status(
            self.room)['review_followups']['engineering']['findings']
        self.assertEqual(findings['required_open'], 0)

    def test_untagged_prose_is_not_refused(self):
        self.implement_with(review_findings=['open required lock-order issue remains'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_scope_only_finding_is_not_refused(self):
        self.implement_with(review_findings=['lock order scope=required'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_disposition_only_finding_is_not_refused(self):
        self.implement_with(review_findings=['lock order disposition=open'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_optional_scope_finding_is_not_refused(self):
        self.implement_with(review_findings=['lock order scope=optional disposition=open'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_non_vocabulary_case_is_not_refused(self):
        self.implement_with(review_findings=['lock order scope=Required disposition=Open'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_deferred_and_decision_needed_required_findings_are_not_refused_by_this_check(self):
        """The recorded #234 policy: only an explicit disposition=open refuses; deferred and
        decision_needed stay open in the projection but are not refused by this check."""
        self.implement_with(review_findings=[
            'lock order scope=required disposition=deferred',
            'scope choice scope=required disposition=decision_needed'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_an_open_required_backlog_entry_is_refused_too(self):
        self.implement_with(
            backlog=['drop dead code path origin=preexisting scope=required disposition=open'])
        exc = self.refuses(r'Engineering reports completion while a required finding is open: '
                           r'backlog\[0\] "drop dead code path"',
                           self.service.ao_room_verify, self.room, str(self.repo))
        # No invariant/owner tokens, so the entry has no parenthesis.
        self.assertNotIn('(', str(exc))

    def test_earlier_readiness_refusals_keep_their_order_and_wording(self):
        self.implement_with(review_findings=[OPEN], remaining_gaps=['restart proof missing'])
        # The pre-existing incomplete/remaining-gaps check runs first and keeps its wording.
        self.refuses('Engineering is incomplete or has remaining gaps',
                     self.service.ao_room_verify, self.room, str(self.repo))
        self.send('correction', 'cor-1')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps({'outcome': 'completed'}))
        self.service.ao_room_sync(self.room)
        self.refuses('Verification requires a captured completed engineering result; request '
                     'cor-1 recorded engineering_error:',
                     self.service.ao_room_verify, self.room, str(self.repo))

    def test_message_names_at_most_three_findings_and_redacts_paths(self):
        self.implement_with(review_findings=[
            'x' * 200 + ' scope=required disposition=open',
            '/private/evidence/x.log leaked scope=required disposition=open',
            'third issue scope=required disposition=open',
            'fourth issue scope=required disposition=open'])
        exc = self.refuses('Engineering reports completion while 4 required findings are open:',
                           self.service.ao_room_verify, self.room, str(self.repo))
        message = str(exc)
        self.assertTrue(message.startswith(
            'Engineering reports completion while 4 required findings are open:'))
        self.assertIn('and 1 more', message)
        self.assertIn('<path>', message)
        self.assertNotIn('/private/evidence', message)
        self.assertIn('x' * 80 + '…', message)
        self.assertNotIn('fourth issue', message)

    def test_a_historical_accepted_room_keeps_its_acceptance_and_revalidation_refuses(self):
        # Simulate the pre-change controller for the history part of the room.
        with patch('ao_findings.explicit_required_open', return_value=[]):
            self.implement_with(review_findings=[OPEN])
            self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
            self.review()
            self.assertTrue(
                self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        status = self.service.ao_room_status(self.room)
        self.assertIsNotNone(status['latest_acceptance'])
        self.assertEqual(status['acceptance_review_attempts'], 1)
        self.assertEqual(status['current_summary']['ladder']['independent_acceptance']['status'],
                         'approved')
        # Revalidation under the new check refuses; the recorded acceptance is untouched.
        self.refuses(REFUSAL, self.service.ao_room_accept, self.room, 'acceptance_review')
        self.assertEqual(len(self.state()['acceptances']), 1)


if __name__ == '__main__':
    unittest.main()
