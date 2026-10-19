"""Offline spec-review debate-budget contracts; synthetic rooms and fake backends only."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone

import ao_project_room as ao
import ao_spec_review_budget as budget
import ao_workflow
from test_ao_normal import Fixture


class BudgetFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()

    def finish_review(self, decision='changes_required', findings=None, revision=1):
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact charter only.',
                         'findings': findings if findings is not None else ['Required behavior differs'],
                         'decision': decision, 'spec_revision': revision, 'spec_sha256': spec['sha256']}))
        return self.service.ao_room_sync(self.room)

    def verdict_review(self, key, **changes):
        self.send('spec_review', key)
        return self.finish_review(**changes)

    def prose_review(self, key):
        self.send('spec_review', key)
        self.fake.finish('engineer', 'I agree to the exact current specification. No specification change is required.')
        return self.service.ao_room_sync(self.room)

    def quota_failed_review(self, key, successor):
        """A settled quota failure on a spec_review intent: dispatch capacity consumed, never debate."""
        self.send('spec_review', key)
        request = self.state()['requests'][key]
        stamp = lambda seconds: min(datetime.fromtimestamp(request['created_at'] + seconds, timezone.utc),
                                    datetime.now(timezone.utc)).isoformat()
        # The turn fails at the provider limit: the synthetic caller row stays but its positive
        # reply is replaced by the native 429 error the outcome audit correlates.
        self.native_events = [event for event in self.native_events if event['uuid'] != key + '-reply']
        self.native_events.append({'type': 'assistant', 'uuid': key + '-error', 'sessionId': self.NATIVE,
            'timestamp': stamp(0.002), 'cwd': str(self.repo), 'isSidechain': False,
            'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
            'message': {'role': 'assistant', 'model': '<synthetic>',
                        'content': [{'type': 'text', 'text': 'Synthetic native session limit'}]}})
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events))
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests'][key]['state'], 'uncertain')
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(audit['outcome']['kind'], 'quota_limit')
        self.service.ao_room_outcome_resume(self.room, key, audit['outcome_sha256'], successor,
                                            'Operator inspected the quota evidence and authorized one successor',
                                            'Continue once')
        self.assertEqual(self.state()['requests'][key]['state'], 'settled_failure')

    def accept_review(self, key, drop=(), **changes):
        """A completed review whose verdict defaults to a canonical strict accept of the current spec."""
        spec = self.service.spec(self.directory(), self.state())
        verdict = {'interpretation': 'Exact charter only.', 'findings': [], 'decision': 'accept',
                   'spec_revision': spec['revision'], 'spec_sha256': spec['sha256'], **changes}
        for field in drop:
            verdict.pop(field)
        self.send('spec_review', key)
        self.fake.finish('engineer', json.dumps(verdict))
        return self.service.ao_room_sync(self.room)

    def counted(self):
        state = self.state()
        return {request['request_id']: budget.counted_as(self.directory(), state, request)
                for request in budget.reviews(state)}

    def digest(self):
        return hashlib.sha256((self.directory() / 'state.json').read_bytes()).hexdigest()


class DebateBudgetTests(BudgetFixture):
    def test_accepting_reviews_do_not_consume_the_debate_budget(self):
        self.agree(key='review1')
        self.agree(key='review2')
        self.verdict_review('review3')
        posts = len(self.fake.posts)
        self.assertEqual(self.send('spec_review', 'review4')['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        state = self.state()
        self.assertIn('review4', state['requests'])
        self.assertNotIn('spec_review_extension', state)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_attempts'], 4)
        summary = status['spec_review_budget']
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (1, 4))
        self.assertEqual((summary['debate_limit'], summary['dispatch_ceiling']), (3, 5))
        self.assertTrue(summary['ordinary_review_capacity'])
        self.assertEqual(self.counted(), {'review1': 'accepting', 'review2': 'accepting',
                                         'review3': 'debate', 'review4': 'dispatch_only'})
        # The admitted fourth review still leaves the unchanged five-dispatch ceiling in force.
        self.finish_review()
        self.agree(key='review5')
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'reached the room ceiling'):
            self.send('spec_review', 'review6')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('review6', self.state()['requests'])

    def test_three_accepting_reviews_admit_an_ordinary_fourth(self):
        for number in range(1, 4):
            self.agree(key='review' + str(number))
        posts = len(self.fake.posts)
        self.assertEqual(self.send('spec_review', 'review4')['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertNotIn('spec_review_extension', self.state())
        self.finish_review()
        self.agree(key='review5')
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'reached the room ceiling'):
            self.send('spec_review', 'review6')
        self.assertEqual(len(self.fake.posts), posts)

    def test_three_non_accepting_reviews_exhaust_the_debate_budget(self):
        for key in ('review1', 'review2', 'review3'):
            self.verdict_review(key)
        before, posts = self.digest(), len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exhausted the debate budget'):
            self.send('spec_review', 'review4')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.digest(), before)
        self.assertNotIn('review4', self.state()['requests'])

    def test_an_accepting_handoff_review_does_not_extend_past_three_debates(self):
        self.agree(key='review1')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.spec(2)
        for key in ('review2', 'review3', 'review4'):
            self.verdict_review(key, revision=2)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exhausted the debate budget'):
            self.send('spec_review', 'review5')
        self.assertEqual(len(self.fake.posts), posts)
        summary = self.service.ao_room_status(self.room)['spec_review_budget']
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (3, 4))
        self.assertFalse(summary['ordinary_review_capacity'])

    def test_five_dispatches_reach_the_hard_ceiling_even_when_all_accept(self):
        for number in range(1, 6):
            self.agree(key='review' + str(number))
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'reached the room ceiling'):
            self.send('spec_review', 'review6')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('review6', self.state()['requests'])
        summary = self.service.ao_room_status(self.room)['spec_review_budget']
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (0, 5))
        self.assertFalse(summary['ordinary_review_capacity'])

    def test_accept_with_blocker_prose_only_and_quota_failure_classify_separately(self):
        # A completed accept whose findings carry a BLOCKER is debate, not accepting.
        self.verdict_review('review1', decision='accept', findings=['BLOCKER: Need a product decision'])
        self.prose_review('review2')
        self.quota_failed_review('review3', 'review4')
        counted = self.counted()
        self.assertEqual((counted['review1'], counted['review2'], counted['review3']),
                         ('debate', 'debate', 'dispatch_only'))
        summary = self.service.ao_room_status(self.room)['spec_review_budget']
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (2, 3))
        # The settled failure consumed dispatch capacity only: its authorized successor is ordinary.
        self.assertEqual(self.send('spec_review', 'review4')['state'], 'submitted')

    def test_retained_history_like_the_repair_room_reads_without_mutation(self):
        self.agree(key='review1')
        self.agree(key='review2')
        self.prose_review('review3')
        before = self.digest()
        summary = self.service.ao_room_status(self.room)['spec_review_budget']
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (1, 3))
        self.assertTrue(summary['ordinary_review_capacity'])
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.send('spec_review', 'review4')['state'], 'submitted')

    def test_invalid_accepts_count_as_debate_under_the_agreement_contract(self):
        """Every deviation the agreement path refuses counts as debate under the same strict
        contract, evaluated against each request's own immutable spec record."""
        self.accept_review('review1')
        self.accept_review('review2', drop=('interpretation',))
        self.accept_review('review3', interpretation='   ')
        self.accept_review('review4', interpretation=['Exact charter only.'])
        self.assertEqual(self.counted(), {'review1': 'accepting', 'review2': 'debate',
                                         'review3': 'debate', 'review4': 'debate'})
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exhausted the debate budget'):
            self.send('spec_review', 'review5')
        self.assertEqual(len(self.fake.posts), posts)

    def test_boolean_revision_and_wrong_engineer_accepts_count_as_debate(self):
        self.accept_review('review1', spec_revision=True)  # JSON true is never revision 1
        self.accept_review('review2')
        self.assertEqual(self.counted(), {'review1': 'debate', 'review2': 'accepting'})
        # The bound-engineer check belongs to the same contract: a completed accept whose
        # retained request identity no longer matches the bound session is debate.
        state = self.state()
        request = copy.deepcopy(state['requests']['review2'])
        request['session_id'] = 'another-session'
        state['requests']['review2'] = request
        self.assertEqual(budget.counted_as(self.directory(), state, request), 'debate')

    def test_two_debates_and_an_invalid_accept_still_refuse_the_ordinary_fourth(self):
        self.verdict_review('review1')
        self.verdict_review('review2')
        self.accept_review('review3', interpretation='   ')  # completed, never an acceptance
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exhausted the debate budget'):
            self.send('spec_review', 'review4')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('review4', self.state()['requests'])
        self.assertEqual(self.counted(), {'review1': 'debate', 'review2': 'debate', 'review3': 'debate'})

    def test_a_stale_spec_accept_stays_accepting_for_budget_while_agreement_refuses(self):
        self.accept_review('review1')
        self.spec(2)
        request = self.state()['requests']['review1']
        self.assertEqual(budget.counted_as(self.directory(), self.state(), request), 'accepting')
        with self.assertRaisesRegex(ao.RoomError, 'current exact spec requires completed Fable agreement'):
            ao_workflow.agreement(self.service, self.directory(), self.state())

    def test_a_damaged_completed_review_counts_as_debate_and_summary_survives(self):
        self.agree(key='review1')
        self.agree(key='review2')
        request = self.state()['requests']['review1']
        (self.directory() / request['receipt']).write_bytes(b'{"tampered": true}\n')
        state = self.state()
        self.assertEqual(budget.counted_as(self.directory(), state, request), 'debate')
        summary = budget.summary(self.directory(), state)
        rows = {row['request_id']: row['counted_as'] for row in summary['reviews']}
        self.assertEqual(rows, {'review1': 'debate', 'review2': 'accepting'})
        self.assertEqual(summary['debate_attempts'], 1)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_budget']['reviews'],
                         summary['reviews'])


from test_ao_spec_rereview import ReReviewFixture


class HanCountingShapeTests(ReReviewFixture):
    """The retained-room counting shape: two accepting reviews, one non-accepting review,
    the consumed extension's noncanonical fourth review, and the re-review as the fifth
    dispatch. Han's room reached its grant under the retired completed-review eligibility,
    so ReReviewFixture mints it that way while every later validation runs the real
    counters."""

    def test_consumed_extension_and_rereview_count_like_the_retained_room(self):
        audited, grant = self.granted()
        self.send_rereview()
        self.finish_rereview()
        self.assertTrue(ao_workflow.agreement(self.service, self.directory(), self.state())['agreed'])
        summary = budget.summary(self.directory(), self.state())
        self.assertEqual((summary['debate_attempts'], summary['dispatches']), (2, 5))
        self.assertFalse(summary['ordinary_review_capacity'])
        rows = {row['request_id']: row['counted_as'] for row in summary['reviews']}
        self.assertEqual(rows, {'charter-1': 'accepting', 'charter-2': 'debate', 'charter-3': 'accepting',
                                'fourth': 'debate', 'spec-rereview': 'accepting'})
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'one re-review intent is exhausted'):
            self.service.ao_room_send(self.room, 'engineer', 'Review the charter again.', 'sixth',
                                      purpose='spec_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('sixth', self.state()['requests'])
        # The committed extension and the consumed re-review grant still validate under the
        # new counters, including the rereview exclusion inside the granted-at window.
        import ao_review_extension
        import ao_spec_rereview
        self.assertIsNotNone(ao_review_extension.validate(self.service, self.state()))
        self.assertIsNotNone(ao_spec_rereview.validate(self.service, self.state()))


if __name__ == '__main__':
    unittest.main()
