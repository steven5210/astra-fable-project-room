"""The additive read-only current_summary projection: identical record-only facts on every
surface, progress-only codes only where task rows exist; fake backends and synthetic transcripts
only; no account, network, AO process or model call."""

import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ao_project_room as ao
import project_room
import project_room_mcp
from test_progress_provenance import ProvenanceFixture

SUMMARY_KEYS = {'version', 'basis', 'engineering_line', 'ladder', 'inconsistencies', 'plan_checks',
                'meaning', 'truncated'}
LINE_KEYS = {'latest_request', 'latest_captured', 'later_request'}
LATEST_KEYS = {'request_id', 'purpose', 'state', 'ao_turn_state', 'semantic', 'delivery_error'}
CAPTURED_KEYS = {'request_id', 'candidate_sha256', 'report_sha256', 'is_latest_request'}
LATER_KEYS = {'request_id', 'state', 'capture_status'}
LADDER_KEYS = {'prepared', 'dispatch', 'reconciliation', 'engineering_verdict',
               'formal_verification', 'independent_acceptance'}
VERDICT_KEYS = {'status', 'request_id', 'is_latest_request', 'outcome', 'implementation_complete',
                'remaining_gaps', 'required_open_findings', 'tests_reported', 'reason'}
DISPATCH_VOCAB = {'none', 'uncertain_delivery', 'dispatched', 'running', 'returned', 'settled_failure'}
RECON_VOCAB = {'none', 'pending', 'held', 'capture_failed', 'captured', 'unobserved'}
VERDICT_VOCAB = {'none', 'unavailable', 'reported_complete', 'reported_incomplete'}
VERIFY_VOCAB = {'none', 'pending', 'passed', 'failed', 'error'}
ACCEPT_VOCAB = {'none', 'pending', 'approved', 'rejected', 'uncertain', 'error'}
RELATION_VOCAB = {'none', 'superseded_by_later_candidate', 'spec_revised_since',
                  'baseline_for_live_turn', 'baseline_for_unsettled_turn',
                  'baseline_for_uncaptured_turn', 'current', 'unknown'}
CODE_VOCAB = {'verification_not_current', 'acceptance_not_current',
              'engineering_turn_delivery_uncertain', 'engineering_record_invalid',
              'engineering_complete_reported_with_open_required_findings',
              'in_progress_units_without_live_engineer_turn',
              'open_units_after_complete_engineering_report',
              'completed_units_with_incomplete_engineering_report'}
KIND_VOCAB = {'contradiction', 'stale', 'unknown'}
STATUS_PLAN_CHECKS = {'available': False, 'basis': None,
                      'reason': 'status reads no transcript; see ao_room_progress'}
PROGRESS_ONLY = {'in_progress_units_without_live_engineer_turn',
                 'open_units_after_complete_engineering_report',
                 'completed_units_with_incomplete_engineering_report'}


class CurrentSummaryFixture(ProvenanceFixture):
    """Shape checks, cross-surface equality, and the quota-shaped failure helper."""

    def assert_shape(self, cs):
        self.assertEqual(set(cs), SUMMARY_KEYS)
        self.assertEqual(cs['version'], 1)
        self.assertIsInstance(cs['basis'], str)
        self.assertIsInstance(cs['meaning'], str)
        self.assertIsInstance(cs['truncated'], bool)
        line = cs['engineering_line']
        self.assertEqual(set(line), LINE_KEYS)
        for key, keys in (('latest_request', LATEST_KEYS), ('latest_captured', CAPTURED_KEYS),
                          ('later_request', LATER_KEYS)):
            if line[key] is not None:
                self.assertEqual(set(line[key]), keys)
        ladder = cs['ladder']
        self.assertEqual(set(ladder), LADDER_KEYS)
        self.assertEqual(set(ladder['prepared']), {'status', 'handed_off'})
        self.assertEqual(set(ladder['dispatch']), {'status', 'request_id'})
        self.assertIn(ladder['dispatch']['status'], DISPATCH_VOCAB)
        self.assertEqual(set(ladder['reconciliation']), {'status', 'request_id', 'semantic_kind'})
        self.assertIn(ladder['reconciliation']['status'], RECON_VOCAB)
        self.assertEqual(set(ladder['engineering_verdict']), VERDICT_KEYS)
        self.assertIn(ladder['engineering_verdict']['status'], VERDICT_VOCAB)
        self.assertEqual(set(ladder['formal_verification']), {'status', 'candidate_sha256', 'relation'})
        self.assertIn(ladder['formal_verification']['status'], VERIFY_VOCAB)
        self.assertIn(ladder['formal_verification']['relation'], RELATION_VOCAB)
        self.assertEqual(set(ladder['independent_acceptance']),
                         {'status', 'request_id', 'candidate_sha256', 'relation'})
        self.assertIn(ladder['independent_acceptance']['status'], ACCEPT_VOCAB)
        self.assertIn(ladder['independent_acceptance']['relation'], RELATION_VOCAB)
        self.assertLessEqual(len(cs['inconsistencies']), 8)
        self.assertEqual(cs['inconsistencies'],
                         sorted(cs['inconsistencies'], key=lambda item: item['code']))
        for item in cs['inconsistencies']:
            self.assertEqual(set(item), {'code', 'kind', 'detail'})
            self.assertIn(item['code'], CODE_VOCAB)
            self.assertIn(item['kind'], KIND_VOCAB)
        self.assertEqual(set(cs['plan_checks']), {'available', 'basis', 'reason'})
        encoded = json.dumps(cs)
        for private in (str(self.root), self.NATIVE, 'Perform the exact authorized purpose.'):
            self.assertNotIn(private, encoded)

    def record_only(self, cs):
        """The surface-independent part: no plan_checks and no progress-only codes."""
        return {**{key: value for key, value in cs.items()
                   if key not in ('plan_checks', 'inconsistencies')},
                'inconsistencies': [item for item in cs['inconsistencies']
                                    if item['code'] not in PROGRESS_ONLY]}

    def read_all(self):
        """(status full, status compact, progress view) results; every read leaves the fixture's
        files, posts and gets byte-identical, the record-only current_summary identical across
        surfaces and the status plan_checks the fixed unavailable object."""
        self.write_transcript()
        before = self.fingerprint()
        calls = (len(self.fake.posts), self.fake.gets)
        full = self.service.ao_room_status(self.room, view='full')
        compact = self.service.ao_room_status(self.room, view='compact')
        view = self.service.ao_room_progress(self.room)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual((len(self.fake.posts), self.fake.gets), calls)
        cs_full, cs_compact, cs_view = (full['current_summary'], compact['current_summary'],
                                       view['current_summary'])
        for cs in (cs_full, cs_compact, cs_view):
            self.assert_shape(cs)
        self.assertEqual(self.record_only(cs_full), self.record_only(cs_compact))
        self.assertEqual(self.record_only(cs_full), self.record_only(cs_view))
        for cs in (cs_full, cs_compact):
            self.assertEqual(cs['plan_checks'], STATUS_PLAN_CHECKS)
        return full, compact, view

    def captured_record(self, request_id='impl-1'):
        request = self.state()['requests'][request_id]
        return ao.read(self.directory() / request['engineering_record'])

    def native_failure_rows(self, request_id, error='rate_limit', status=429):
        """Synthetic native quota-failure evidence for one already-sent request.

        Appends the caller row and one isApiErrorMessage row exactly as the quota-recovery fixture does;
        the caller must dispatch through ao_room_send directly (not send()) so this helper owns the
        request's native rows.
        """
        request = self.state()['requests'][request_id]
        stamp = lambda offset: min(datetime.fromtimestamp(request['created_at'] + offset, timezone.utc),
                                   datetime.now(timezone.utc)).isoformat()
        self.native_events.extend([
            {'type': 'user', 'uuid': request_id + '-caller', 'sessionId': self.NATIVE,
             'timestamp': stamp(0.001), 'cwd': self.native_row_workspace(), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': request_id + '-error', 'sessionId': self.NATIVE,
             'timestamp': stamp(0.002), 'cwd': self.native_row_workspace(), 'isSidechain': False,
             'isApiErrorMessage': True, 'error': error, 'apiErrorStatus': status,
             'message': {'role': 'assistant', 'model': '<synthetic>',
                         'content': [{'type': 'text', 'text': 'Synthetic native session limit'}]}}])
        self.transcript.write_text(''.join(json.dumps(row) + '\n' for row in self.native_events))
        self.native_requests.add(request_id)


class CurrentSummaryTests(CurrentSummaryFixture):
    """Each ladder rung, relation and code against the saved records that produce it."""

    def _green(self):
        """Scenario baseline: plan rows, a captured impl-1, a passed gate, an approved acceptance."""
        self.plan_rows()
        request = self.finish_engineering()
        self.approve()
        return request

    def test_accepted_candidate_is_current_on_every_rung(self):
        request = self._green()
        full, compact, view = self.read_all()
        cs = full['current_summary']
        record = self.captured_record()
        candidate = request['result_candidate_sha256']
        self.assertEqual(cs['ladder']['prepared'], {'status': 'configured', 'handed_off': True})
        self.assertEqual(cs['ladder']['dispatch'], {'status': 'returned', 'request_id': 'impl-1'})
        self.assertEqual(cs['ladder']['reconciliation'],
                         {'status': 'captured', 'request_id': 'impl-1', 'semantic_kind': None})
        self.assertEqual(cs['ladder']['engineering_verdict'],
                         {'status': 'reported_complete', 'request_id': 'impl-1',
                          'is_latest_request': True, 'outcome': 'completed',
                          'implementation_complete': True, 'remaining_gaps': 0,
                          'required_open_findings': 0, 'tests_reported': 1, 'reason': None})
        self.assertEqual(cs['ladder']['formal_verification'],
                         {'status': 'passed', 'candidate_sha256': candidate, 'relation': 'current'})
        self.assertEqual(cs['ladder']['independent_acceptance'],
                         {'status': 'approved', 'request_id': 'acceptance_review',
                          'candidate_sha256': candidate, 'relation': 'current'})
        self.assertEqual(cs['engineering_line']['latest_request'],
                         {'request_id': 'impl-1', 'purpose': 'implementation', 'state': 'completed',
                          'ao_turn_state': request['observed_turn']['state'],
                          'semantic': {'kind': request['semantic_status']['kind'], 'hold': False},
                          'delivery_error': False})
        self.assertEqual(cs['engineering_line']['latest_captured'],
                         {'request_id': 'impl-1', 'candidate_sha256': candidate,
                          'report_sha256': record['report_sha256'], 'is_latest_request': True})
        self.assertIsNone(cs['engineering_line']['later_request'])
        self.assertEqual(cs['inconsistencies'], [])
        self.assertEqual(view['current_summary']['plan_checks'], {'available': True, 'basis': 'deliverables', 'reason': None})
        # Task 2 is in_progress while the latest engineer turn is completed, and the same open
        # unit contradicts the complete report: the stale-closure-text case.
        self.assertEqual(view['current_summary']['inconsistencies'], [
            {'code': 'in_progress_units_without_live_engineer_turn', 'kind': 'stale',
             'detail': {'in_progress': 1, 'latest_engineer_request_id': 'impl-1',
                        'latest_engineer_request_state': 'completed', 'semantic_hold': False,
                        'semantic_kind': request['semantic_status']['kind']}},
            {'code': 'open_units_after_complete_engineering_report', 'kind': 'contradiction',
             'detail': {'open_units': 1, 'blockers': 0}}])

    def test_live_correction_turn_marks_the_verified_baseline(self):
        request = self._green()
        self.send('correction', 'fix-1')
        full, compact, view = self.read_all()
        cs = full['current_summary']
        record = self.captured_record()
        candidate = request['result_candidate_sha256']
        self.assertEqual(cs['engineering_line']['latest_request'],
                         {'request_id': 'fix-1', 'purpose': 'correction', 'state': 'submitted',
                          'ao_turn_state': None, 'semantic': None, 'delivery_error': False})
        self.assertEqual(cs['engineering_line']['latest_captured'],
                         {'request_id': 'impl-1', 'candidate_sha256': candidate,
                          'report_sha256': record['report_sha256'], 'is_latest_request': False})
        self.assertEqual(cs['engineering_line']['later_request'],
                         {'request_id': 'fix-1', 'state': 'submitted', 'capture_status': 'pending'})
        self.assertEqual(cs['ladder']['dispatch'], {'status': 'dispatched', 'request_id': 'fix-1'})
        self.assertEqual(cs['ladder']['reconciliation'],
                         {'status': 'pending', 'request_id': 'fix-1', 'semantic_kind': None})
        self.assertEqual(cs['ladder']['engineering_verdict']['status'], 'reported_complete')
        self.assertEqual(cs['ladder']['engineering_verdict']['is_latest_request'], False)
        self.assertEqual(cs['ladder']['formal_verification']['relation'], 'baseline_for_live_turn')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'], 'baseline_for_live_turn')
        self.assertEqual(cs['inconsistencies'], [
            {'code': 'acceptance_not_current', 'kind': 'stale',
             'detail': {'relation': 'baseline_for_live_turn', 'later_request_id': 'fix-1',
                        'later_request_state': 'submitted'}},
            {'code': 'verification_not_current', 'kind': 'stale',
             'detail': {'relation': 'baseline_for_live_turn', 'later_request_id': 'fix-1',
                        'later_request_state': 'submitted'}}])
        # The live turn adds no progress-only codes: it is live, and the capture is not the latest.
        self.assertEqual([item['code'] for item in view['current_summary']['inconsistencies']],
                         ['acceptance_not_current', 'verification_not_current'])
        # Existing objects keep their meaning: status has no stages, and the stage's own
        # identity_match stays what it was (unknown against a request with no record).
        self.assertNotIn('stages', full)
        self.assertEqual(view['stages']['verification']['identity_match'], 'unknown')

    def test_held_correction_turn_keeps_the_baseline_disclosed(self):
        self._green()
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'fix-2', purpose='correction')
        self.native_failure_rows('fix-2')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.service.ao_room_outcome_audit(self.room)
        request = self.state()['requests']['fix-2']
        self.assertEqual(request['state'], 'uncertain')
        self.assertEqual(request['observed_turn']['state'], 'failed')
        kind = request['semantic_status']['kind']
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['engineering_line']['latest_request']['state'], 'uncertain')
        self.assertEqual(cs['engineering_line']['latest_request']['ao_turn_state'], 'failed')
        self.assertEqual(cs['engineering_line']['latest_request']['semantic'],
                         {'kind': kind, 'hold': True})
        self.assertEqual(cs['ladder']['dispatch'], {'status': 'returned', 'request_id': 'fix-2'})
        self.assertEqual(cs['ladder']['reconciliation'],
                         {'status': 'held', 'request_id': 'fix-2', 'semantic_kind': kind})
        self.assertEqual(cs['ladder']['formal_verification']['relation'],
                         'baseline_for_unsettled_turn')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'],
                         'baseline_for_unsettled_turn')
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['acceptance_not_current', 'verification_not_current'])
        self.assertEqual(view['current_summary']['inconsistencies'], [
            {'code': 'acceptance_not_current', 'kind': 'stale',
             'detail': {'relation': 'baseline_for_unsettled_turn', 'later_request_id': 'fix-2',
                        'later_request_state': 'uncertain'}},
            {'code': 'in_progress_units_without_live_engineer_turn', 'kind': 'stale',
             'detail': {'in_progress': 1, 'latest_engineer_request_id': 'fix-2',
                        'latest_engineer_request_state': 'uncertain', 'semantic_hold': True,
                        'semantic_kind': kind}},
            {'code': 'verification_not_current', 'kind': 'stale',
             'detail': {'relation': 'baseline_for_unsettled_turn', 'later_request_id': 'fix-2',
                        'later_request_state': 'uncertain'}}])

    def test_uncertain_delivery_is_named_without_a_turn(self):
        self._green()
        self.fake.lose_ack = True
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'fix-3', purpose='correction')
        self.fake.lose_ack = False
        request = self.state()['requests']['fix-3']
        self.assertEqual(request['state'], 'uncertain')
        self.assertIn('delivery_error', request)
        self.assertNotIn('observed_turn', request)
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['dispatch'],
                         {'status': 'uncertain_delivery', 'request_id': 'fix-3'})
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['acceptance_not_current', 'engineering_turn_delivery_uncertain',
                          'verification_not_current'])
        for item in cs['inconsistencies']:
            if item['code'] == 'engineering_turn_delivery_uncertain':
                self.assertEqual(item, {'code': 'engineering_turn_delivery_uncertain',
                                        'kind': 'unknown',
                                        'detail': {'request_id': 'fix-3', 'delivery_error': True,
                                                   'reconciliation': False}})
            else:
                self.assertEqual(item['detail']['relation'], 'baseline_for_unsettled_turn')

    def test_complete_report_with_an_open_required_finding_is_a_contradiction(self):
        self._green()
        self.send('correction', 'fix-1')
        self.fake.finish('engineer', json.dumps(self.report(
            review_findings=['stale lock order origin=introduced scope=required disposition=open'])))
        self.service.ao_room_sync(self.room)
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['engineering_verdict']['status'], 'reported_complete')
        self.assertEqual(cs['ladder']['engineering_verdict']['required_open_findings'], 1)
        self.assertIn({'code': 'engineering_complete_reported_with_open_required_findings',
                       'kind': 'contradiction',
                       'detail': {'request_id': 'fix-1', 'required_open_findings': 1}},
                      cs['inconsistencies'])
        # A new capture of the SAME candidate keeps verification and acceptance current.
        self.assertEqual(cs['ladder']['formal_verification']['relation'], 'current')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'], 'current')

    def test_a_later_capture_supersedes_then_reverification_restores_current(self):
        self._green()
        # A new captured candidate that still passes the gate: the fixture's gate pins
        # feature.txt byte-for-byte, so the drift lands in a new file instead.
        self.send('correction', 'fix-1')
        (self.repo / 'followup.txt').write_text('follow-up\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        corrected = self.state()['requests']['fix-1']
        candidate = corrected['result_candidate_sha256']
        full, compact, view = self.read_all()
        cs = full['current_summary']
        stale = self.state()['requests']['impl-1']['result_candidate_sha256']
        self.assertNotEqual(candidate, stale)
        self.assertEqual(cs['ladder']['formal_verification'],
                         {'status': 'passed', 'candidate_sha256': stale,
                          'relation': 'superseded_by_later_candidate'})
        self.assertEqual(cs['ladder']['independent_acceptance'],
                         {'status': 'approved', 'request_id': 'acceptance_review',
                          'candidate_sha256': stale, 'relation': 'superseded_by_later_candidate'})
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['acceptance_not_current', 'verification_not_current'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        full2, compact2, view2 = self.read_all()
        cs2 = full2['current_summary']
        self.assertEqual(cs2['ladder']['formal_verification']['relation'], 'current')
        self.assertEqual(cs2['ladder']['independent_acceptance']['relation'],
                         'superseded_by_later_candidate')
        self.assertEqual([item['code'] for item in cs2['inconsistencies']],
                         ['acceptance_not_current'])

    def test_spec_revision_marks_prior_evidence_as_revised(self):
        self._green()
        self.spec(revision=2)
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['formal_verification']['relation'], 'spec_revised_since')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'], 'spec_revised_since')
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['acceptance_not_current', 'verification_not_current'])

    def test_completed_units_with_an_incomplete_report_is_a_contradiction(self):
        self.create('1', 'EXIT-6: first part', metadata={'req': 'R1'})
        self.create('2', 'EXIT-7: second part', metadata={'req': 'R1'})
        self.update('1', status='completed')
        self.update('2', status='completed')
        self.say('Both parts recorded complete.')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report(
            outcome='changes_required', implementation_complete=False,
            remaining_gaps=['missing restart proof'])))
        self.service.ao_room_sync(self.room)
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['engineering_verdict'],
                         {'status': 'reported_incomplete', 'request_id': 'impl-1',
                          'is_latest_request': True, 'outcome': 'changes_required',
                          'implementation_complete': False, 'remaining_gaps': 1,
                          'required_open_findings': 0, 'tests_reported': 1, 'reason': None})
        self.assertEqual(view['current_summary']['inconsistencies'], [
            {'code': 'completed_units_with_incomplete_engineering_report', 'kind': 'contradiction',
             'detail': {'completed': 2, 'outcome': 'changes_required', 'remaining_gaps': 1}}])

    def test_labels_never_infer_a_ladder_rung(self):
        self._green()
        baseline, _, _ = self.read_all()
        self.create('9', 'EXIT-9: formally verified and independently accepted', metadata={'req': 'R1'})
        self.update('9', status='completed')
        self.say('Recorded.')
        labeled, _, _ = self.read_all()
        self.assertEqual(labeled['current_summary']['ladder'], baseline['current_summary']['ladder'])

    def test_current_summary_round_trips_over_mcp_tools_call(self):
        self._green()
        service = project_room.Service(self.home)
        self.write_transcript()
        before = self.fingerprint()
        with patch.object(project_room.ao_project_room, 'Service', return_value=self.service):
            status_response = project_room_mcp.handle(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                 'params': {'name': 'ao_room_status',
                            'arguments': {'room_id': self.room, 'view': 'compact'}}}, service)
            progress_response = project_room_mcp.handle(
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'ao_room_progress', 'arguments': {'room_id': self.room}}}, service)
        self.assertEqual(self.fingerprint(), before)
        served_status = json.loads(status_response['result']['content'][0]['text'])
        served_progress = json.loads(progress_response['result']['content'][0]['text'])
        self.assertEqual(served_status['current_summary'],
                         json.loads(json.dumps(self.service.ao_room_status(self.room, view='compact')['current_summary'])))
        self.assertEqual(served_progress['current_summary'],
                         json.loads(json.dumps(self.service.ao_room_progress(self.room)['current_summary'])))

    def test_an_unavailable_transcript_keeps_the_record_only_object(self):
        self._green()
        full, compact, _ = self.read_all()
        state = self.state()
        del state['native_outcome_source']
        self.service.save(self.directory(), state)
        view = self.service.ao_room_progress(self.room)
        degraded = view['current_summary']
        self.assert_shape(degraded)
        self.assertEqual(degraded['plan_checks'],
                         {'available': False, 'basis': None, 'reason': 'transcript unavailable'})
        self.assertEqual(self.record_only(degraded), self.record_only(full['current_summary']))

    def test_a_pending_verification_with_a_later_turn_is_not_a_stale_approval(self):
        self.plan_rows()
        request = self.finish_engineering()
        # A running verification blocks any further send, so the later turn is dispatched first.
        self.send('correction', 'fix-1')
        state = self.state()
        state['verifications'].append({'id': 'verify-running', 'state': 'running',
                                       'candidate_sha256': request['result_candidate_sha256']})
        self.service.save(self.directory(), state)
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['formal_verification'],
                         {'status': 'pending',
                          'candidate_sha256': request['result_candidate_sha256'],
                          'relation': 'baseline_for_live_turn'})
        self.assertEqual(cs['inconsistencies'], [])
        # The later turn is live and its capture is not the baseline, so progress adds nothing.
        self.assertEqual(view['current_summary']['inconsistencies'], [])

    def test_a_failed_verification_with_a_later_turn_is_not_a_stale_approval(self):
        self.plan_rows()
        self.finish_engineering(content='draft\n')
        self.assertFalse(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        self.send('correction', 'fix-1')
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['formal_verification']['status'], 'failed')
        self.assertEqual(cs['ladder']['formal_verification']['relation'],
                         'baseline_for_live_turn')
        self.assertEqual(self.record_only(cs)['inconsistencies'], [])

    def test_a_rejected_acceptance_with_a_later_turn_is_not_a_stale_approval(self):
        self.plan_rows()
        self.finish_engineering()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        self.send('acceptance_review')
        request = self.state()['requests']['acceptance_review']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'rejected',
                                                 'review': 'Gate evidence is incomplete.'}))
        self.service.ao_room_sync(self.room)
        self.send('correction', 'fix-1')
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['independent_acceptance']['status'], 'rejected')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'],
                         'baseline_for_live_turn')
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['verification_not_current'])

    def test_an_error_verification_rung_produces_no_stale_code(self):
        self._green()
        state = self.state()
        candidate = state['requests']['impl-1']['result_candidate_sha256']
        path = self.directory() / state['checkpoint']
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        full, compact, view = self.read_all()
        cs = full['current_summary']
        self.assertEqual(cs['ladder']['formal_verification'],
                         {'status': 'error', 'candidate_sha256': None, 'relation': 'none'})
        # Acceptance does not read the checkpoint: it stays approved/current, so no code at all.
        self.assertEqual(cs['ladder']['independent_acceptance'],
                         {'status': 'approved', 'request_id': 'acceptance_review',
                          'candidate_sha256': candidate, 'relation': 'current'})
        self.assertEqual(cs['inconsistencies'], [])
        self.assertEqual([item['code'] for item in view['current_summary']['inconsistencies']
                          if item['code'] not in PROGRESS_ONLY], [])

    def test_a_superseded_handoff_makes_the_verdict_unavailable(self):
        self._green()
        self.spec(revision=2)
        self.agree(revision=2, key='spec_review-2')
        self.service.ao_room_handoff(self.room, str(self.repo))
        cs, _, view = (result['current_summary'] for result in self.read_all())
        self.assertEqual(cs['ladder']['engineering_verdict'],
                         {'status': 'unavailable', 'request_id': 'impl-1',
                          'is_latest_request': True,
                          'outcome': None, 'implementation_complete': None, 'remaining_gaps': None,
                          'required_open_findings': None, 'tests_reported': None,
                          'reason': 'historical_handoff'})
        self.assertEqual(cs['ladder']['formal_verification']['relation'], 'spec_revised_since')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'], 'spec_revised_since')
        self.assertEqual([item['code'] for item in cs['inconsistencies']],
                         ['acceptance_not_current', 'verification_not_current'])

    def test_attach_plan_leaves_the_record_only_part_byte_identical(self):
        self._green()
        import ao_current_summary
        state = self.state()
        summary = ao_current_summary.summarize(self.directory(), state)
        before = copy.deepcopy(summary)
        self.write_transcript()
        attached = self.service.ao_room_progress(self.room)['current_summary']
        self.assertNotEqual(attached['plan_checks'], before['plan_checks'])
        self.assertEqual(self.record_only(attached), self.record_only(before))
        self.assertEqual(json.dumps(self.record_only(attached), sort_keys=True),
                         json.dumps(self.record_only(before), sort_keys=True))
        for item in attached['inconsistencies']:
            if item['code'] in PROGRESS_ONLY:
                self.assertIn(item['code'], PROGRESS_ONLY)

    def test_a_tampered_record_degrades_without_a_capture(self):
        self._green()
        path = self.directory() / self.state()['requests']['impl-1']['engineering_record']
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        full, compact, view = self.read_all()  # every surface still returns; nothing raises
        cs = full['current_summary']
        self.assertIsNone(cs['engineering_line']['latest_captured'])
        # Without a captured baseline there is nothing for the latest request to be later than.
        self.assertIsNone(cs['engineering_line']['later_request'])
        self.assertEqual(cs['ladder']['engineering_verdict']['status'], 'none')
        # The saved fields still report their recorded capture status; the digest check is what
        # withholds the record, and both disclosures stay side by side.
        self.assertEqual(cs['ladder']['reconciliation']['status'], 'captured')
        # The unverifiable capture is a named contradiction; with no baseline the stages' saved
        # evidence can only report unknown, which is not flagged as stale.
        self.assertEqual(cs['inconsistencies'],
                         [{'code': 'engineering_record_invalid', 'kind': 'contradiction',
                           'detail': {'request_id': 'impl-1'}}])
        self.assertEqual(cs['ladder']['formal_verification']['relation'], 'unknown')
        self.assertEqual(cs['ladder']['independent_acceptance']['relation'], 'unknown')


if __name__ == '__main__':
    unittest.main()
