"""Offline reports expose follow-ups without granting work, replaying context or hiding gaps."""
import copy
import json
from unittest.mock import patch

import ao_project_room as ao
import ao_review_followups as followups
import ao_routing
import ao_routing_guard
import ao_workflow
from test_ao_normal import Fixture


OPERATOR = {'task': 'Run the exact agreed gate on the supplied candidate.',
            'inputs': ['Current candidate and agreed gate argv'],
            'verification': ['Save full exit status and output against the candidate digest.']}
PROPOSAL = {'title': 'Export comparison results', 'benefit': 'Reviewers can compare runs.',
            'tradeoff': 'Additional format and maintenance work.', 'basis': 'Observed repeated manual comparisons.'}


class FollowupReportsTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()

    def spec_result(self, **changes):
        self.send('spec_review')
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish('engineer', json.dumps({'interpretation': 'Implement the exact contract.', 'findings': [],
                         'decision': 'accept', 'spec_revision': spec['revision'], 'spec_sha256': spec['sha256'], **changes}))
        return self.service.ao_room_sync(self.room)

    def summaries(self):
        return self.service.ao_room_status(self.room)['review_followups']

    def test_pending_operator_work_blocks_verify_acceptance_and_reviewer_dispatch(self):
        self.agree(); self.implement(operator_requests=[OPERATOR])
        # This deliberately contradictory completed/true result is not silently repaired.
        before = copy.deepcopy(self.state())
        request = before['requests']['implementation']
        raw = ao_workflow.final_json(self.directory(), request)
        self.assertTrue(raw['implementation_complete'])
        self.assertEqual(raw['outcome'], 'completed')
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_verify(self.room, str(self.repo))
        with self.assertRaises(ao.RoomError): self.send('acceptance_review')
        with self.assertRaises(ao.RoomError): self.service.ao_room_accept(self.room, 'no-review')
        self.assertEqual(self.summaries()['engineering']['operator_requests']['count'], 1)
        self.assertEqual(ao_workflow.final_json(self.directory(), request), raw)
        self.assertEqual(self.state()['requests'], before['requests'])

    def test_later_truthful_report_can_clear_pending_work_without_rewriting_receipt(self):
        self.agree(); self.implement(operator_requests=[OPERATOR])
        old = self.state()['requests']['implementation']
        receipt = self.directory() / old['receipt']; raw = receipt.read_bytes()
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report(operator_requests=[], enhancement_proposals=[PROPOSAL])))
        self.service.ao_room_sync(self.room)
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.assertEqual(receipt.read_bytes(), raw)
        current = self.summaries()['engineering']
        self.assertEqual(current['request_id'], 'correction')
        self.assertEqual(current['operator_requests']['count'], 0)
        self.assertEqual(current['enhancement_proposals']['count'], 1)
        self.assertEqual(self.service.spec(self.directory(), self.state())['revision'], 1)  # Proposal is not added scope.

    def test_old_gate_and_review_do_not_bypass_new_pending_operator_work(self):
        self.agree(); self.implement(); self.review()
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report(operator_requests=[OPERATOR])))
        self.service.ao_room_sync(self.room)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.send('acceptance_review', 'another-review')
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertEqual(len(self.fake.posts), posts)

    def test_spec_proposals_remain_visible_when_fable_pushes_back(self):
        status = self.spec_result(decision='changes_required', findings=['BLOCKER: Clarify the requirement.'],
                                  enhancement_proposals=[PROPOSAL])
        self.assertFalse(status['agreement']['agreed'])
        summary = status['review_followups']['spec_review']
        self.assertEqual(summary['status'], 'reported')
        self.assertEqual(summary['enhancement_proposals']['items'][0]['title'], PROPOSAL['title'])
        self.assertEqual(summary['receipt_sha256'], self.state()['requests']['spec_review']['receipt_sha256'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_handoff(self.room, str(self.repo))

    def test_missing_field_in_a_later_report_does_not_clear_pending_work(self):
        self.agree(); self.implement(operator_requests=[OPERATOR])
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        current = self.summaries()['engineering']
        self.assertEqual(current['operator_requests']['assessment'], 'not_reported')
        self.assertEqual(current['pending_operator_work_reported_by'], 'implementation')
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_uncaptured_pending_report_cannot_be_skipped_for_an_older_clear_list(self):
        self.agree(); self.implement(operator_requests=[])
        self.send('correction', 'uncaptured')
        self.fake.finish('engineer', json.dumps(self.report(operator_requests=[OPERATOR], tests_reported={})))
        self.service.ao_room_sync(self.room)
        self.assertNotIn('engineering_record', self.state()['requests']['uncaptured'])
        self.send('correction', 'later')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.summaries()['engineering']['pending_operator_work_reported_by'], 'uncaptured')
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_known_format_failure_never_clears_an_earlier_pending_statement(self):
        self.agree(); self.implement(operator_requests=[OPERATOR])
        self.send('correction', 'format-defect')
        self.fake.finish('engineer', 'An incomplete report, not an operator disposition.')
        self.service.ao_room_sync(self.room)
        self.send('correction', 'later')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_lost_or_changed_prior_evidence_does_not_hide_pending_work(self):
        self.agree(); self.implement(operator_requests=[OPERATOR])
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        prior = self.state()['requests']['implementation']
        for name in ('engineering_record', 'receipt'):
            path = self.directory() / prior[name]; original = path.read_bytes()
            path.unlink()
            with self.subTest(name=name):
                self.assertEqual(self.summaries()['engineering']['status'], 'unavailable')
                with self.assertRaises((ao.RoomError, OSError)):
                    self.service.ao_room_verify(self.room, str(self.repo))
            path.write_bytes('{}\n'.encode())
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_verify(self.room, str(self.repo))
            path.write_bytes(original)

    def test_optional_enhancements_do_not_block_exact_spec_agreement(self):
        result = self.spec_result(enhancement_proposals=[PROPOSAL])
        self.assertTrue(result['agreement']['agreed'])
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(self.service.spec(self.directory(), self.state())['revision'], 1)

    def test_legacy_omission_is_distinct_from_explicit_none(self):
        self.agree(); self.implement()
        before = self.summaries()
        self.assertEqual(before['spec_review']['enhancement_proposals']['assessment'], 'not_reported')
        self.assertEqual(before['engineering']['operator_requests'], {'assessment': 'not_reported'})
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report(operator_requests=[], enhancement_proposals=[])))
        self.service.ao_room_sync(self.room)
        for field in followups.FIELDS:
            self.assertEqual(self.summaries()['engineering'][field]['count'], 0)
            self.assertEqual(self.summaries()['engineering'][field]['assessment'], 'reported')

    def test_malformed_optional_fields_refuse_but_do_not_rewrite_native_result(self):
        self.agree(); self.implement(operator_requests='run tests')
        request = self.state()['requests']['implementation']
        self.assertIn('operator_requests', request['engineering_error'])
        self.assertEqual(self.summaries()['engineering']['status'], 'unavailable')
        self.assertEqual(ao_workflow.final_json(self.directory(), request)['operator_requests'], 'run tests')
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
        for value in ({'operator_requests': [dict(OPERATOR, verification=[])]},
                      {'operator_requests': [dict(OPERATOR, authorization='invented')]},
                      {'operator_requests': [dict(OPERATOR, inputs=[False])]},
                      {'enhancement_proposals': None}, {'enhancement_proposals': [dict(PROPOSAL, basis=' ')]},
                      {'enhancement_proposals': ['Invent an improvement']}):
            with self.subTest(value=value), self.assertRaises(ao.RoomError):
                followups.validate(value)

    def test_optional_proposal_defects_do_not_consume_another_review_attempt(self):
        status = self.spec_result(enhancement_proposals={'title': 'Incomplete'})
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['review_followups']['spec_review']['enhancement_proposals']['assessment'], 'invalid')
        self.implement(enhancement_proposals={'title': 'Still advisory'})
        self.assertEqual(self.summaries()['engineering']['enhancement_proposals']['assessment'], 'invalid')
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 1)

    def test_operator_field_in_spec_review_is_visible_but_never_execution_authority(self):
        status = self.spec_result(operator_requests={'task': 'Unexpected, malformed request'})
        self.assertTrue(status['agreement']['agreed'])
        field = status['review_followups']['spec_review']['operator_requests']
        self.assertTrue(field['unexpected_for_spec_review'])
        self.assertEqual(field['assessment'], 'invalid')
        self.assertFalse(field['execution_authorized'])
        self.assertEqual((self.repo / 'feature.txt').read_text(), 'start\n')

    def test_status_is_bounded_read_only_and_has_complete_evidence_pointer(self):
        self.agree()
        large = dict(PROPOSAL, title='x' * 5000)
        self.implement(operator_requests=[OPERATOR] * 30, enhancement_proposals=[large] * 30)
        before = {p: p.read_bytes() for p in self.directory().rglob('*') if p.is_file()}
        gets, posts = self.fake.gets, len(self.fake.posts)
        with patch.object(self.service, 'client', side_effect=AssertionError('Status must not contact AO')):
            summary = self.summaries()['engineering']
        self.assertEqual(summary['enhancement_proposals']['count'], 30)
        self.assertEqual(len(summary['enhancement_proposals']['items']), followups.PREVIEW_ITEMS)
        self.assertTrue(summary['enhancement_proposals']['more_items'])
        self.assertEqual(len(summary['enhancement_proposals']['items'][0]['title']), followups.PREVIEW_TEXT)
        self.assertEqual(len(summary['operator_requests']['items']), followups.PREVIEW_ITEMS)
        self.assertTrue(summary['operator_requests']['preview_only'])
        self.assertTrue((self.directory() / summary['receipt']).is_file())
        self.assertEqual((self.fake.gets, len(self.fake.posts)), (gets, posts))
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory().rglob('*') if p.is_file()})

    def test_pending_new_result_never_falls_back_to_old_clear_report(self):
        self.agree(); self.implement(operator_requests=[])
        self.send('correction')
        summary = self.summaries()['engineering']
        self.assertEqual(summary['request_id'], 'correction')
        self.assertEqual(summary['status'], 'awaiting_usable_result')
        self.assertNotIn('operator_requests', summary)

    def test_stale_spec_and_corrupt_receipt_never_report_a_current_all_clear(self):
        self.agree(); self.implement(operator_requests=[])
        request = self.state()['requests']['implementation']
        (self.directory() / request['receipt']).write_text('{}')
        self.assertEqual(self.summaries()['engineering']['status'], 'unavailable')
        self.spec(2)
        self.assertEqual(self.summaries()['spec_review']['status'], 'historical_spec')
        self.assertEqual(self.summaries()['engineering']['status'], 'historical_spec')

    def test_previous_provider_epoch_is_historical_even_when_spec_is_current(self):
        self.agree(); self.implement(operator_requests=[])
        state = self.state(); state['provider_transition'] = {'original_preparation_sha256': 'historical'}
        projected = followups.latest_summary(self.service, self.directory(), state, {'implementation', 'correction'})
        self.assertEqual(projected['status'], 'historical_provider_epoch')
        self.assertNotIn('operator_requests', projected)


class FollowupAmendmentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()
        with patch.object(ao_workflow, 'PARTS', tuple(p for p in ao_workflow.PARTS if p != followups.PART)):
            self.agree(); self.implement(outcome='changes_required', implementation_complete=False)

    def test_existing_session_receives_only_actual_amendment_once_across_restart(self):
        before = copy.deepcopy(self.state())
        receipts = {self.directory() / r['receipt']: (self.directory() / r['receipt']).read_bytes()
                    for r in before['requests'].values()}
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'new', purpose='correction')
        current = self.state()['requests']['new']
        self.assertEqual(current['text'], followups.INSTRUCTION + '\nContinue.')
        self.assertEqual(current['carried']['parts'], [followups.PART])
        self.assertEqual(current['carried']['part_sha256'], {followups.PART: ao.digest(followups.INSTRUCTION.encode())})
        self.assertIsNone(current['carried']['spec_delivery'])
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'later', purpose='correction')
        self.assertEqual(self.state()['requests']['later']['text'], 'Continue.')
        self.assertEqual(self.state()['requests']['later']['carried']['parts'], [])
        for key, value in before['requests'].items(): self.assertEqual(self.state()['requests'][key], value)
        for path, raw in receipts.items(): self.assertEqual(path.read_bytes(), raw)

    def test_amendment_does_not_release_quota_or_duplicate_request(self):
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'new', purpose='correction')
        before = len(self.fake.posts)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'new', purpose='correction')
        self.assertEqual(len(self.fake.posts), before)
        self.fake.finish('engineer', json.dumps(self.report()))
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit', 'httpStatus': 429}
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'blocked', purpose='correction')
        self.assertEqual(len(self.fake.posts), before)

    def test_capability_projection_matches_guard_without_authorizing_resume(self):
        prepared = ao.read(self.directory() / self.state()['preparation'])
        capabilities = ao_routing.status(prepared, self.state(), self.directory())['worker_recovery']
        self.assertFalse(capabilities['native_child_context_resume'])
        self.assertFalse(capabilities['native_child_messaging'])
        self.assertFalse(capabilities['dispatch_authorized'])
        self.assertEqual(capabilities['fresh_pinned_worker'], 'conditional')
        with patch.dict('os.environ', ao_routing.FOREGROUND_ENV):
            for event in ({'tool_name': 'SendMessage', 'tool_input': {'to': 'prior-child', 'message': 'Continue.'}},
                          {'tool_name': 'ListAgents', 'tool_input': {}},
                          {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-sonnet', 'resume': 'prior-child', 'prompt': 'Continue.'}}):
                self.assertIsNotNone(ao_routing_guard.decide(event))
        missing = ao_routing.status(None, {})['worker_recovery']
        self.assertEqual(missing['fresh_pinned_worker'], 'not_configured')
        self.assertIsNone(missing['native_child_context_resume'])
        self.assertEqual(missing['strategy'], 'inspect_preserved_artifacts_then_operator')
        with patch.object(ao_routing, 'validate_local', side_effect=ao.RoomError('changed guard')):
            value = ao_routing.status(prepared, self.state(), self.directory())
        self.assertEqual(value['status'], 'unverified')
        self.assertEqual(value['worker_recovery']['fresh_pinned_worker'], 'blocked')
        self.assertIsNone(value['worker_recovery']['native_child_messaging'])
        # Old or different pinned guards do not inherit the current implementation's measured capability.
        for version, guard in ((1, prepared['routing']['guard_sha256']), (2, 'different-guard-sha256')):
            historical = copy.deepcopy(prepared); historical['routing']['version'] = version
            projected = {'status': 'configured', 'guard_sha256': guard}
            with patch.object(ao_routing, 'routing_status', return_value=projected):
                old = ao_routing.status(historical, {})['worker_recovery']
            self.assertIsNone(old['native_child_context_resume'])
            self.assertEqual(old['capability_basis'], 'unverified_guard_capability')
