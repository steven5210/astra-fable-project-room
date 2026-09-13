"""Offline terminal-outcome regression coverage; fake AO and synthetic transcripts."""
import copy
import json
from unittest.mock import patch
import unittest

import ao_project_room as ao
import ao_native_outcome as native
import ao_outcomes as outcomes
from test_ao_normal import Fixture


class OutcomeClassificationTests(unittest.TestCase):
    def receipt(self, text='{}', **turn):
        return {'turn': {'id': 'turn', 'providerTurnId': 'provider', 'state': 'completed', **turn},
                'messages': [{'role': 'assistant', 'text': text}], 'history_truncated': False}

    def test_quota_precedes_truncation_and_valid_or_missing_output(self):
        for text in ('{}', '{"outcome":', ''):
            with self.subTest(text=text):
                result = outcomes.classify(self.receipt(text, stopReason='max_tokens', error={'type': 'rate_limit', 'httpStatus': 429}))
                self.assertEqual(result['kind'], 'quota_limit'); self.assertTrue(result['hold'])

    def test_typed_provider_error_is_not_a_formatting_correction(self):
        self.assertEqual(outcomes.classify(self.receipt(error={'type': 'auth_required'}))['kind'], 'provider_error')

    def test_canned_or_quoted_prose_is_not_typed_quota_evidence(self):
        for text in ("You've reached your Fable limit", 'A source says HTTP 429', '{"finding":"a quota error occurred historically"}'):
            self.assertNotEqual(outcomes.classify(self.receipt(text))['kind'], 'quota_limit')

    def test_missing_and_unstructured_output_hold_without_positive_end_turn(self):
        for text in ('', '{"outcome":', 'Done'):
            self.assertEqual(outcomes.classify(self.receipt(text))['kind'], 'unknown')
        self.assertFalse(outcomes.classify(self.receipt('Done', stopReason='end_turn'))['hold'])

    def test_stale_failure_does_not_poison_later_turn(self):
        value = self.receipt()
        value['sessionFailures'] = [{'turnId': 'old', 'kind': 'quota_exhausted'}]
        self.assertFalse(outcomes.classify(value)['hold'])
        value['sessionFailures'].append({'providerTurnId': 'provider', 'kind': 'quota_exhausted'})
        self.assertEqual(outcomes.classify(value)['kind'], 'quota_limit')

    def test_unattributed_and_malformed_evidence_hold(self):
        for failures in ({}, [None], [{'kind': 'quota_exhausted'}]):
            value = self.receipt(); value['sessionFailures'] = failures
            self.assertTrue(outcomes.classify(value)['hold'])
        self.assertTrue(outcomes.classify([])['hold'])
        self.assertTrue(outcomes.classify(self.receipt(error={'type': []}))['hold'])


class NativeCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.request = {'text': 'Continue.', 'text_sha256': ao.digest(b'Continue.'), 'created_at': 1000, 'model': 'fable'}
        self.anchor = {'type': 'user', 'uuid': 'anchor', 'sessionId': 'native', 'timestamp': '1970-01-01T00:16:41Z',
                       'origin': {'kind': 'human'}, 'message': {'content': 'Continue.'}}
        self.error = {'type': 'assistant', 'uuid': 'error', 'sessionId': 'native', 'timestamp': '1970-01-01T00:16:43Z',
                      'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
                      'message': {'model': '<synthetic>', 'content': [{'type': 'text', 'text': 'not exported'}]}}

    def test_compact_summary_tool_result_and_other_session_do_not_replace_anchor(self):
        compact = {**self.anchor, 'uuid': 'compact', 'isCompactSummary': True}
        tool = {**self.anchor, 'uuid': 'tool', 'message': {'content': [{'type': 'tool_result'}]}}
        foreign = {**self.error, 'uuid': 'foreign', 'sessionId': 'other'}
        result = native.events_outcome([compact, tool, self.anchor, foreign, self.error], self.request, 'native')
        self.assertEqual(result['anchor_uuid'], 'anchor'); self.assertEqual(len(result['errors']), 1)
        self.assertNotIn('not exported', json.dumps(result))

    def test_only_error_inside_current_human_interval_counts(self):
        older = {**self.error, 'uuid': 'older', 'timestamp': '1970-01-01T00:16:39Z'}
        next_user = {**self.anchor, 'uuid': 'next', 'timestamp': '1970-01-01T00:18:00Z'}
        later = {**self.error, 'timestamp': '1970-01-01T00:18:01Z'}
        result = native.events_outcome([older, self.anchor, next_user, later], self.request, 'native')
        self.assertEqual(result['errors'], []); self.assertEqual(result['next_human_uuid'], 'next')

    def test_ambiguous_changed_or_malformed_source_refuses(self):
        for rows in ([self.anchor, {**self.anchor, 'uuid': 'duplicate'}], [self.anchor, []],
                     [self.anchor, {**self.anchor, 'timestamp': '1970-01-01T00:16:42Z'}]):
            with self.assertRaises(ao.RoomError): native.events_outcome(rows, self.request, 'native')
        with self.assertRaises(ao.RoomError):
            native.events_outcome([self.anchor], {**self.request, 'text_sha256': '0' * 64}, 'native')

    def test_repeated_records_deduplicate_and_root_model_is_pinned(self):
        result = native.events_outcome([self.anchor, self.error, self.error], self.request, 'native')
        self.assertEqual(len(result['errors']), 1)
        wrong = {**self.error, 'isApiErrorMessage': False, 'message': {'model': 'other', 'id': 'response', 'stop_reason': 'end_turn'}}
        with self.assertRaises(ao.RoomError): native.events_outcome([self.anchor, wrong], self.request, 'native')

    def test_queue_records_without_message_uuid_are_not_caller_or_error_evidence(self):
        queue = {'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': 'native', 'timestamp': self.anchor['timestamp']}
        result = native.events_outcome([queue, self.anchor, self.error], self.request, 'native')
        self.assertEqual(result['anchor_uuid'], 'anchor'); self.assertEqual(len(result['errors']), 1)

    def test_native_retry_success_settles_only_errors_before_its_final(self):
        final = {'type':'assistant', 'uuid':'final', 'sessionId':'native', 'timestamp':'1970-01-01T00:16:44Z',
                 'message':{'model':'fable','id':'answer','stop_reason':'end_turn'}}
        result = native.events_outcome([self.anchor, self.error, final], self.request, 'native')
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['settled_errors']), 1)
        late = {**self.error, 'uuid':'late', 'timestamp':'1970-01-01T00:16:45Z'}
        result = native.events_outcome([self.anchor, self.error, final, late], self.request, 'native')
        self.assertEqual([x['uuid'] for x in result['errors']], ['late'])


class OutcomeWorkflowTests(Fixture):
    def setUp(self):
        super().setUp(); self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def failed(self, text='', **turn):
        self.send('implementation'); self.fake.finish('engineer', text)
        self.fake.snapshots['engineer']['turns'][-1].update(turn)
        return self.service.ao_room_sync(self.room)

    def test_unknown_completion_does_not_send_another_request_or_accept(self):
        self.failed()
        before = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.send('correction')
        self.assertEqual(len(self.fake.posts), before)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertFalse(audit['resume_eligible'])
        with self.assertRaisesRegex(ao.RoomError, 'eligible'):
            self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'], 'resume', 'Inspected', 'Continue')

    def test_quota_hold_survives_restart_and_allows_only_explicit_one_use_successor(self):
        self.failed(json.dumps(self.report()), stopReason='max_tokens', error={'type': 'rate_limit', 'httpStatus': 429})
        self.service = ao.Service(self.home, lambda url: self.fake)
        before = copy.deepcopy(self.state()['requests']['implementation'])
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'): self.send('correction')
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(audit['outcome']['kind'], 'quota_limit')
        args = (self.room, 'implementation', audit['outcome_sha256'], 'approved-resume', 'User confirmed reset; partial work inspected', 'Continue once')
        posts = len(self.fake.posts)
        self.service.ao_room_outcome_resume(*args)
        self.service.ao_room_outcome_resume(*args)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'): self.send('correction', 'other-id')
        self.send('correction', 'approved-resume')
        self.send('correction', 'approved-resume')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(self.state()['requests']['implementation']['receipt_sha256'], before['receipt_sha256'])
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'): self.send('correction', 'again')

    def test_quota_is_never_hidden_by_valid_verdict_or_normalization(self):
        self.failed(json.dumps(self.report()), error={'type': 'rate_limit'})
        self.assertNotIn('engineering_record', self.state()['requests']['implementation'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))

    def test_late_failure_keeps_receipt_and_blocks_new_work(self):
        self.failed(json.dumps(self.report()))
        before = self.state()['requests']['implementation']['receipt_sha256']
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit'}
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['receipt_sha256'], before)
        self.assertEqual(request['semantic_status']['kind'], 'quota_limit')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'): self.send('correction')

    def test_changed_evidence_invalidates_authorized_successor(self):
        self.failed('{', stopReason='max_tokens')
        audit = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'], 'resume', 'Reviewed partial work', 'Continue')
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit'}
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'): self.send('correction', 'resume')
        original = self.state()['requests']['implementation']['outcome_resume']
        audit = self.service.ao_room_outcome_audit(self.room)
        self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'], 'resume',
                                           'New quota evidence inspected; user approved same successor', 'Continue after reset')
        latest = self.state()['requests']['implementation']['outcome_resume']
        self.assertNotEqual(original, latest)
        self.assertTrue((self.directory() / original).exists())
        self.send('correction', 'resume')

    def test_result_tampering_and_untouched_historical_request_id_refuse(self):
        self.failed('{}', error={'type': 'rate_limit'})
        request = self.state()['requests']['implementation']
        path = self.directory() / request['semantic_outcome']
        path.write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'modified|changed'): self.send('correction')

    def test_failed_transport_is_not_eligible_for_semantic_recovery(self):
        self.send('implementation'); self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertFalse(self.service.ao_room_outcome_audit(self.room)['resume_eligible'])
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'): self.send('correction')

    def test_failed_ao_turn_needs_exact_native_quota_and_authorized_settlement(self):
        self.send('implementation'); self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        state = self.state(); state['native_outcome_source'] = {'session_id': 'engineer'}
        ao.atomic(self.directory() / 'state.json', state)
        observed = {'anchor_uuid': 'owned-native-packet', 'next_human_uuid': None,
                    'errors': [{'uuid': 'native-quota', 'error': 'rate_limit', 'http_status': 429}],
                    'stop_reasons': [], 'source_sha256': 'f' * 64}
        with patch.object(native, 'inspect', return_value=observed):
            audit = self.service.ao_room_outcome_audit(self.room)
            self.assertTrue(audit['resume_eligible'])
            self.assertEqual(self.state()['requests']['implementation']['state'], 'uncertain')
            self.service.ao_room_outcome_resume(self.room, 'implementation', audit['outcome_sha256'], 'resume',
                                               'Native quota rejection identified; user confirmed availability', 'Continue once')
            self.assertEqual(self.state()['requests']['implementation']['state'], 'settled_failure')
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'resume', purpose='correction')
            self.assertEqual(self.state()['requests']['resume']['text'], 'Continue.')
            request = self.state()['requests']['implementation']
            self.assertEqual(ao.read(self.directory() / request['receipt'])['turn']['state'], 'failed')

    def test_typed_ao_failure_activity_blocks_even_when_transport_and_json_look_complete(self):
        self.failed(json.dumps(self.report()))
        turn = self.fake.snapshots['engineer']['turns'][-1]['id']
        self.fake.snapshots['engineer']['activities'] = [{'id': 'failure', 'turnId': turn,
            'activityKind': 'system', 'status': 'completed',
            'detail': {'event': 'provider.failure', 'category': 'limit', 'severity': 'error'}}]
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['implementation']['semantic_status']['kind'], 'provider_error')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'): self.send('correction')

    def test_failed_receipt_survives_late_metadata_and_remains_auditable(self):
        self.send('implementation'); self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        first = self.state()['requests']['implementation']
        self.service.ao_room_outcome_audit(self.room)
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit'}
        self.fake.snapshots['engineer']['usage']['outputTokens'] += 2
        self.service.ao_room_sync(self.room)
        after = self.state()['requests']['implementation']
        self.assertEqual(after['receipt_sha256'], first['receipt_sha256'])
        self.assertEqual(after['usage'], first['usage'])
        self.assertEqual(len(after['receipt_history']), 2)
        self.assertEqual(self.service.ao_room_outcome_audit(self.room)['outcome']['kind'], 'quota_limit')

    def test_sync_with_aged_out_turn_remains_readable_but_dispatch_stays_blocked(self):
        self.failed(json.dumps(self.report()))
        self.fake.snapshots['engineer']['history_truncated'] = True
        result = self.service.ao_room_sync(self.room)
        self.assertEqual(result['requests'][-1]['state'], 'completed')
        self.assertIn('semantic_observation_error', self.state()['requests']['implementation'])
        before = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.send('correction')
        self.assertEqual(len(self.fake.posts), before)

    def test_unknown_requires_explicit_audit_to_clear_with_positive_evidence(self):
        self.failed('Unformatted but complete answer', stopReason=None)
        self.assertEqual(self.state()['requests']['implementation']['semantic_status']['kind'], 'unknown')
        self.fake.snapshots['engineer']['turns'][-1]['stopReason'] = 'end_turn'
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['implementation']['semantic_status']['kind'], 'unknown')
        self.assertEqual(self.service.ao_room_outcome_audit(self.room)['outcome']['kind'], 'final_available')
        self.send('correction')

    def test_mcp_outcome_audit_resume_and_instruction_stage_use_fake_backend_only(self):
        import project_room, project_room_mcp
        service = project_room.Service(self.root / 'mcp-state')
        self.failed('{}', error={'type': 'rate_limit'})
        before = len(self.fake.posts)
        def call(name, **args):
            with patch.object(project_room.ao_project_room, 'Service', return_value=self.service):
                result = project_room_mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':name,'arguments':{'room_id':self.room, **args}}}, service)['result']
            self.assertFalse(result['isError'], result)
            return result['structuredContent']
        audit = call('ao_room_outcome_audit')
        call('ao_room_outcome_resume', request_id='implementation', outcome_sha256=audit['outcome_sha256'],
             resume_request_id='resume', diagnosis='Quota inspected; user confirmed reset', authorization='Continue once')
        call('ao_room_instruction_stage', request_id='new-instruction', message='Delegate suitable supporting document assembly.',
             authorization='User requested durable delegation fix')
        self.assertEqual(len(self.fake.posts), before)


class NativeFileTests(unittest.TestCase):
    def test_explicit_owned_native_file_is_bounded_and_wrong_paths_do_not_persist(self):
        import tempfile
        from pathlib import Path
        import ao_delegates
        fixture = NativeCorrelationTests(); fixture.setUp()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); path = root / 'native.jsonl'
            source = {'transcript': str(path), 'native_session_id': 'native'}
            owner = {'workspace_path': str(root), 'ao_conversation_id': 'conversation', 'active_branch_id': 'branch'}
            snapshot = {'conversationId': 'conversation', 'activeBranchId': 'branch'}
            request = {**fixture.request, 'session_id': 'engineer'}
            rows = [fixture.anchor, fixture.error]
            path.write_text('\n'.join(json.dumps(x) for x in rows)); path.chmod(0o600)
            with patch.object(native, 'validate_source', return_value=owner), patch.object(ao_delegates, 'validate_preparation', return_value={'worktree':str(root)}):
                result = native.inspect(root, {}, request, source, snapshot)
                self.assertEqual(result['errors'][0]['error'], 'rate_limit')
                self.assertEqual(result['source_sha256'], ao.digest(path.read_bytes()))
                path.chmod(0o666)
                with self.assertRaisesRegex(ao.RoomError, 'ownership'): native.inspect(root, {}, request, source, snapshot)
                path.chmod(0o600)
                other = root / 'other.jsonl'; other.symlink_to(path)
                with self.assertRaises(ao.RoomError): native.inspect(root, {}, request, {**source, 'transcript':str(other)}, snapshot)
                with self.assertRaisesRegex(ao.RoomError, 'identity'): native.inspect(root, {}, request, source, {**snapshot, 'activeBranchId':'other'})
