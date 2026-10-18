"""Focused review_decision status projection tests: the digest-validated saved verdict's decision on
completed reviewer rows in both status views; fake backends only; no account, network, AO process or
model call."""
import json

from test_ao_lifecycle_contract import AgreedRoomFixture


class ReviewDecisionStatusTests(AgreedRoomFixture):
    """review_decision derives read-only from the preserved verdict and the bound identities."""

    def _row(self, status, request_id):
        return next(r for r in status['requests'] if r.get('request_id') == request_id)

    def _approved_review(self, key='acceptance_review'):
        self.service.ao_room_verify(self.room, str(self.repo))
        result = self.send('acceptance_review', key)
        request = self.state()['requests'][key]
        self.fake.finish('reviewer', json.dumps(
            {**request['review'], 'decision': 'approved',
             'review': 'Independently inspected exact behavior and gates.'}))
        self.service.ao_room_sync(self.room)
        return result

    def test_approved_then_accepted_is_distinct(self):
        self.implement()
        sent = self._approved_review()
        # ao_room_send returns the unenriched request summary; ao_room_sync returns the same enriched
        # summary the status view uses.
        self.assertNotIn('review_decision', sent)
        synced = self.service.ao_room_sync(self.room)
        self.assertEqual(self._row(synced, 'acceptance_review')['review_decision'],
                         {'decision': 'approved', 'accepted': False})
        full = self.service.ao_room_status(self.room, view='full')
        self.assertEqual(self._row(full, 'acceptance_review')['review_decision'],
                         {'decision': 'approved', 'accepted': False})
        for row in full['requests']:
            if row.get('role') != 'reviewer':
                self.assertNotIn('review_decision', row)
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(self._row(compact, 'acceptance_review')['review_decision'],
                         self._row(full, 'acceptance_review')['review_decision'])
        self.service.ao_room_accept(self.room, 'acceptance_review')
        full = self.service.ao_room_status(self.room, view='full')
        compact = self.mcp('ao_room_status', view='compact')
        for status in (full, compact):
            self.assertEqual(self._row(status, 'acceptance_review')['review_decision'],
                             {'decision': 'approved', 'accepted': True})

    def test_rejected_verdict_is_rejected_not_accepted(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send('acceptance_review')
        request = self.state()['requests']['acceptance_review']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'rejected',
                                                 'review': 'Candidate behavior contradicts the spec.'}))
        self.service.ao_room_sync(self.room)
        status = self.service.ao_room_status(self.room, view='full')
        self.assertEqual(self._row(status, 'acceptance_review')['review_decision'],
                         {'decision': 'rejected', 'accepted': False})
        self.refuses('Reviewer rejected or did not approve',
                     self.service.ao_room_accept, self.room, 'acceptance_review')

    def test_identity_mismatch_and_missing_decision_are_invalid(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        cases = [('acceptance_review',
                  lambda review: {**review, 'candidate_sha256': review['candidate_sha256'][:16],
                                  'decision': 'approved', 'review': 'Approved with a prefix identity.'},
                  {'decision': 'invalid', 'reason': 'identities_mismatch', 'accepted': False}),
                 ('acceptance_review-2',
                  lambda review: {**review, 'review': 'No decision field.'},
                  {'decision': 'invalid', 'reason': 'decision_missing', 'accepted': False})]
        for key, build, expected in cases:
            with self.subTest(request_id=key):
                self.send('acceptance_review', key)
                request = self.state()['requests'][key]
                self.fake.finish('reviewer', json.dumps(build(request['review'])))
                self.service.ao_room_sync(self.room)
                status = self.service.ao_room_status(self.room, view='full')
                self.assertEqual(self._row(status, key)['review_decision'], expected)

    def test_prose_or_modified_receipt_is_unavailable(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send('acceptance_review')
        self.fake.finish('reviewer', 'Approved by inspection, no JSON.')
        self.service.ao_room_sync(self.room)
        status = self.service.ao_room_status(self.room, view='full')
        row = self._row(status, 'acceptance_review')['review_decision']
        self.assertEqual(row['decision'], 'unavailable')
        self.assertTrue(row['reason'])
        self.send('acceptance_review', 'acceptance_review-2')
        request = self.state()['requests']['acceptance_review-2']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'approved',
                                                 'review': 'Independently inspected.'}))
        self.service.ao_room_sync(self.room)
        receipt_path = self.directory() / self.state()['requests']['acceptance_review-2']['receipt']
        original = receipt_path.read_bytes()
        receipt = json.loads(original)
        receipt['messages'][-1]['text'] += ' '  # valid JSON, one changed byte of evidence
        receipt_path.write_text(json.dumps(receipt) + '\n')
        status = self.service.ao_room_status(self.room, view='full')
        self.assertEqual(self._row(status, 'acceptance_review-2')['review_decision'],
                         {'decision': 'unavailable', 'reason': 'Native result receipt was modified',
                          'accepted': False})
        receipt_path.write_bytes(original)
        self.assertEqual(receipt_path.read_bytes(), original)
        before = self.effects()
        status = self.service.ao_room_status(self.room, view='full')
        self.assertEqual(self._row(status, 'acceptance_review-2')['review_decision'],
                         {'decision': 'approved', 'accepted': False})
        self.assert_no_effects(before)  # status is read-only

    def test_status_views_agree_for_every_reviewer_row(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send('acceptance_review')
        request = self.state()['requests']['acceptance_review']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'rejected',
                                                 'review': 'Candidate behavior contradicts the spec.'}))
        self.service.ao_room_sync(self.room)
        self.send('acceptance_review', 'acceptance_review-2')
        request = self.state()['requests']['acceptance_review-2']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'approved',
                                                 'review': 'Independently inspected.'}))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_accept(self.room, 'acceptance_review-2')
        full = self.service.ao_room_status(self.room, view='full')
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(self._row(full, 'acceptance_review')['review_decision'],
                         {'decision': 'rejected', 'accepted': False})
        self.assertEqual(self._row(full, 'acceptance_review-2')['review_decision'],
                         {'decision': 'approved', 'accepted': True})
        # Compact keeps the latest request per role; its reviewer row agrees with the full view.
        compact_reviewer = next(r for r in compact['requests'] if r.get('role') == 'reviewer')
        self.assertEqual(compact_reviewer['request_id'], 'acceptance_review-2')
        self.assertEqual(compact_reviewer['review_decision'],
                         self._row(full, 'acceptance_review-2')['review_decision'])
        self.assertEqual(compact['latest_acceptance'], full['latest_acceptance'])
