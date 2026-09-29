"""Offline exact autocompact-thrashing settlement tests; fake AO and synthetic fixtures."""
import json
import unittest

import ao_native_outcome as native
import ao_outcomes as outcomes
from datetime import datetime, timezone
import ao_project_room as ao
import ao_quality_review as quality
import test_ao_routing_refresh as routing_fixtures

ERROR_TEXT = native.COMPACTION_THRASHING_TEXT
WORKSPACE = '/synthetic/worktree'
GUARD = 'a' * 64


class NativeCompactionProofTests(unittest.TestCase):
    def setUp(self):
        self.request = {'text': 'Continue.', 'text_sha256': ao.digest(b'Continue.'),
                        'created_at': 1000, 'model': 'fable'}
        self.anchor = {'type': 'user', 'uuid': 'anchor', 'sessionId': 'native', 'cwd': WORKSPACE,
                       'timestamp': '1970-01-01T00:16:41Z', 'origin': {'kind': 'human'},
                       'message': {'role': 'user', 'content': 'Continue.'}}
        self.error = {'type': 'assistant', 'uuid': 'compaction-error', 'sessionId': 'native', 'cwd': WORKSPACE,
                      'timestamp': '1970-01-01T00:16:43Z', 'isApiErrorMessage': True,
                      'error': 'invalid_request',
                      'message': {'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence',
                                  'content': [{'type': 'text', 'text': ERROR_TEXT}]}}

    def outcome(self, events):
        return native.events_outcome(events, self.request, 'native', workspace=WORKSPACE)

    def proof(self, events):
        return native.compaction_failure(events, self.request, 'native', WORKSPACE, self.outcome(events))

    def test_exact_terminal_error_produces_narrow_proof(self):
        proof = self.proof([self.anchor, self.error])
        self.assertIsNotNone(proof)
        self.assertEqual(proof['kind'], 'autocompact_thrashing')
        self.assertEqual(proof['error_text_sha256'], native.COMPACTION_THRASHING_TEXT_SHA256)
        self.assertEqual(proof['workspace_sha256'], ao.digest(WORKSPACE.encode()))
        self.assertIsNone(proof['error_message_id'])
        self.assertEqual(proof['assistant_uuid'], 'compaction-error')

    def test_wrong_root_role_model_or_error_is_ineligible(self):
        for change in ({'message': {**self.error['message'], 'role': 'user'}},
                       {'message': {**self.error['message'], 'model': 'other'}},
                       {'message': {**self.error['message'], 'stop_reason': 'end_turn'}},
                       {'error': 'other_error'},
                       {'agentId': 'child-agent'}):
            with self.subTest(change=list(change)):
                error = {**self.error, **change}
                self.assertIsNone(self.proof([self.anchor, error]))
        wrong_cwd = {**self.anchor, 'cwd': '/other'}
        with self.assertRaises(ao.RoomError):
            self.outcome([wrong_cwd, self.error])

    def test_later_streaming_text_or_tool_call_without_stop_reason_is_ineligible(self):
        later_text = {'type': 'assistant', 'uuid': 'later-text', 'sessionId': 'native', 'cwd': WORKSPACE,
                      'timestamp': '1970-01-01T00:16:44Z',
                      'message': {'role': 'assistant', 'model': 'fable',
                                  'content': [{'type': 'text', 'text': 'Still working'}]}}
        later_tool = {**later_text, 'uuid': 'later-tool',
                      'message': {'role': 'assistant', 'model': 'fable',
                                  'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {}}]}}
        for later in (later_text, later_tool):
            with self.subTest(uuid=later['uuid']):
                self.assertEqual(len(self.outcome([self.anchor, self.error, later])['errors']), 1)
                self.assertIsNone(self.proof([self.anchor, self.error, later]))

    def test_later_opaque_or_malformed_block_is_ineligible(self):
        base = {'type': 'assistant', 'uuid': 'later-opaque', 'sessionId': 'native', 'cwd': WORKSPACE,
                'timestamp': '1970-01-01T00:16:44Z',
                'message': {'role': 'assistant', 'model': 'fable'}}
        opaque = {**base, 'message': {**base['message'],
                  'content': [{'type': 'opaque', 'payload': 'synthetic-placeholder'}]}}
        malformed_text = {**base, 'uuid': 'later-malformed', 'message': {**base['message'],
                  'content': [{'type': 'text', 'text': None}]}}
        non_block = {**base, 'uuid': 'later-nonblock', 'message': {**base['message'],
                  'content': ['synthetic-placeholder']}}
        empty_text = {**base, 'uuid': 'later-empty', 'message': {**base['message'],
                  'content': [{'type': 'text', 'text': '   '}]}}
        for row in (opaque, malformed_text, non_block):
            with self.subTest(uuid=row['uuid']):
                self.assertIsNone(self.proof([self.anchor, self.error, row]))
        self.assertIsNotNone(self.proof([self.anchor, self.error, empty_text]))

    def test_later_human_at_equal_timestamp_is_ineligible(self):
        human = {**self.anchor, 'uuid': 'later-human', 'timestamp': self.anchor['timestamp'],
                 'message': {'role': 'user', 'content': 'New instruction.'}}
        self.assertIsNone(self.proof([self.anchor, self.error, human]))

    def test_http_quota_or_safety_contradiction_is_ineligible(self):
        for change in ({'apiErrorStatus': 429},
                       {'quotaLimits': {'status': 'rejected', 'rateLimitType': 'five_hour'}},
                       {'rateLimitType': 'five_hour'}):
            with self.subTest(change=list(change)):
                error = {**self.error, **change}
                self.assertIsNone(self.proof([self.anchor, error]))

    def test_later_refusal_or_safety_stop_is_ineligible(self):
        for stop in ('refusal', 'safety'):
            later = {'type': 'assistant', 'uuid': 'later-refusal', 'sessionId': 'native', 'cwd': WORKSPACE,
                     'timestamp': '1970-01-01T00:16:44Z',
                     'message': {'role': 'assistant', 'model': 'fable', 'id': 'refusal-message',
                                 'stop_reason': stop, 'content': []}}
            rewound = {**later, 'uuid': 'later-refusal-rewound', 'timestamp': '1970-01-01T00:16:40Z'}
            with self.subTest(stop=stop):
                self.assertEqual(self.outcome([self.anchor, self.error, later])['stop_reasons'], [stop])
                self.assertIsNone(self.proof([self.anchor, self.error, later]))
                self.assertIsNone(self.proof([self.anchor, self.error, rewound]))

    def test_unrelated_invalid_request_keeps_old_projection_shape(self):
        other = {**self.error,
                 'message': {**self.error['message'],
                             'content': [{'type': 'text', 'text': 'Different public error.'}]}}
        result = self.outcome([self.anchor, other])
        self.assertEqual(set(result['errors'][0]), {'uuid', 'error', 'http_status'})
        self.assertIsNone(self.proof([self.anchor, other]))

    def test_later_human_source_order_with_rewound_timestamp_is_ineligible(self):
        human = {**self.anchor, 'uuid': 'later-human', 'timestamp': '1970-01-01T00:16:40Z',
                 'message': {'role': 'user', 'content': 'Different instruction.'}}
        self.assertIsNone(self.proof([self.anchor, self.error, human]))

    def test_same_native_message_accepts_only_exact_non_substantive_continuation(self):
        error = {**self.error, 'message': {**self.error['message'], 'id': 'message-id'}}

        def later(uuid, content=None, **changes):
            message = {'role': 'assistant', 'id': 'message-id', 'model': '<synthetic>', **changes}
            if content is not None:
                message['content'] = content
            return {'type': 'assistant', 'uuid': uuid, 'sessionId': 'native', 'cwd': WORKSPACE,
                    'timestamp': '1970-01-01T00:16:44Z', 'message': message}

        self.assertIsNotNone(self.proof([self.anchor, error, later('empty')]))
        self.assertIsNotNone(self.proof([self.anchor, error, later('exact', [{'type': 'text', 'text': ERROR_TEXT}])]))
        for content in ([{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {}}],
                        [{'type': 'text', 'text': 'Still working'}],
                        [{'type': 'text', 'text': 'x'},
                         {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Read', 'input': {}}]):
            self.assertIsNone(self.proof([self.anchor, error, later('later-tool', content)]))

    def test_same_native_message_contradictory_or_malformed_envelope_is_ineligible(self):
        error = {**self.error, 'message': {**self.error['message'], 'id': 'message-id'}}
        for changes in ({'error': 'safety'}, {'apiErrorStatus': 429}, {'quotaLimits': {'status': 'rejected'}},
                        {'rateLimitType': 'five_hour'}, {'message': {'role': 'assistant'}}):
            row = {'type': 'assistant', 'uuid': 'later-contradiction', 'sessionId': 'native', 'cwd': WORKSPACE,
                   'timestamp': '1970-01-01T00:16:44Z',
                   'message': {'role': 'assistant', 'id': 'message-id', 'model': '<synthetic>'}}
            malformed = 'message' in changes
            if malformed:
                row['message'] = changes['message']
            else:
                row.update(changes)
            with self.subTest(change=list(changes)):
                if malformed:
                    # A later envelope with a contradictory model is safely refused
                    # by the native reader instead of producing a compaction proof.
                    with self.assertRaises(ao.RoomError):
                        self.proof([self.anchor, error, row])
                else:
                    self.assertIsNone(self.proof([self.anchor, error, row]))


class CompactionOutcomeRecordTests(unittest.TestCase):
    def record(self):
        return {
            'version': 1,
            'room_id': 'room',
            'request_id': 'implementation',
            'receipt_sha256': 'b' * 64,
            'text_sha256': 'c' * 64,
            'turn_id': 'turn',
            'provider_turn_id': 'provider',
            'outcome': {'kind': 'provider_error', 'hold': True, 'reason': 'synthetic'},
            'session_failures': [],
            'provider_failures': [],
            'native': {
                'anchor_uuid': 'anchor',
                'next_human_uuid': None,
                'errors': [{'uuid': 'compaction-error', 'error': 'invalid_request', 'http_status': None}],
                'settled_errors': [],
                'stop_reasons': [],
                'source': {'session_id': 'engineer'},
                'source_sha256': 'e' * 64,
                'compaction_failure': {
                    'version': 1,
                    'kind': 'autocompact_thrashing',
                    'error': 'invalid_request',
                    'error_role': 'assistant',
                    'error_model': '<synthetic>',
                    'error_stop_reason': 'stop_sequence',
                    'error_text_sha256': native.COMPACTION_THRASHING_TEXT_SHA256,
                    'error_message_id': None,
                    'assistant_uuid': 'compaction-error',
                    'anchor_uuid': 'anchor',
                    'next_human_uuid': None,
                    'later_substantive': False,
                    'http_status_absent': True,
                    'workspace_sha256': 'd' * 64,
                }
            }
        }

    def test_exact_record_is_recognized(self):
        record = self.record()
        self.assertTrue(outcomes.native_compaction_failure(record))
        self.assertEqual(outcomes.native_failure_kind(record), 'compaction_thrashing')

    def test_native_or_ao_terminal_refusal_stop_is_refused(self):
        for change in ({'native': ['refusal']}, {'native': ['safety']},
                       {'stop_reason': 'refusal'}, {'stopReason': 'refusal'}):
            record = self.record()
            if 'native' in change:
                record['native']['stop_reasons'] = change['native']
            else:
                record['ao_terminal'] = {'id': 'turn', 'providerTurnId': 'provider', **change}
            with self.subTest(change=change):
                self.assertFalse(outcomes.native_compaction_failure(record))
                self.assertIsNone(outcomes.native_failure_kind(record))

    def test_quota_or_changed_proof_is_refused(self):
        variants = []
        record = self.record(); record['outcome']['kind'] = 'quota_limit'; variants.append(record)
        record = self.record(); record['native']['compaction_failure']['later_substantive'] = True; variants.append(record)
        record = self.record(); record['native']['errors'][0]['http_status'] = 429; variants.append(record)
        record = self.record(); record['session_failures'] = [{'type': 'rate_limit'}]; variants.append(record)
        for variant in variants:
            self.assertFalse(outcomes.native_compaction_failure(variant))

    def test_ambiguous_bare_limit_disqualifies_only_the_compaction_lane(self):
        record = self.record()
        record['provider_failures'] = [
            {'activity_id': 'limit', 'turn_id': 'turn', 'category': 'limit',
             'severity': 'error', 'status': 'completed', 'type': 'provider_failure'}]
        self.assertFalse(outcomes.native_compaction_failure(record))
        self.assertIsNone(outcomes.native_failure_kind(record))
        session_record = self.record()
        session_record['session_failures'] = [{'turnId': 'turn', 'category': 'limit'}]
        self.assertFalse(outcomes.native_compaction_failure(session_record))
        classified = outcomes.classify(
            {'turn': {'id': 'turn', 'providerTurnId': 'provider', 'state': 'failed'},
             'messages': [], 'sessionFailures': [],
             'provider_failures': [{'category': 'limit', 'severity': 'error'}]})
        self.assertEqual(classified['kind'], 'provider_error')

    def test_quality_mirror_rejects_the_same_changed_quota_values(self):
        request = {'receipt_sha256': 'b' * 64, 'role': 'engineer', 'session_id': 'engineer'}
        record = {'outcome': {'kind': 'quota_limit', 'hold': True},
                  'native': {'anchor_uuid': 'anchor', 'next_human_uuid': None,
                             'errors': [{'error': 'rate_limit', 'http_status': 429}]}}
        base = {'prior_state': 'uncertain', 'ao_state': 'failed', 'receipt_sha256': 'b' * 64}
        self.assertTrue(quality._settlement_valid(record, {'native_failure_settlement': base}, request))
        for proof in ({**base, 'prior_state': 'active'},
                      {**base, 'ao_state': 'completed'},
                      {**base, 'receipt_sha256': 'wrong'}):
            self.assertFalse(quality._settlement_valid(record, {'native_failure_settlement': proof}, request))


class CompactionServiceFixture(unittest.TestCase):
    TURN_STATE = 'failed'

    def setUp(self):
        self.fixture = routing_fixtures.RoutingRefreshTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        f = self.fixture
        f.service.ao_room_handoff(f.room, str(f.repo))
        f.send('implementation')
        f.fake.finish('engineer', state=self.TURN_STATE)
        f.service.ao_room_sync(f.room)
        self.request_id = 'implementation'
        request = f.state()['requests'][self.request_id]
        self.transcript = f.root / (f.native_session + '.jsonl')
        self.events = [
            {'type': 'user', 'uuid': 'owned-native-caller', 'sessionId': f.native_session,
             'timestamp': self.stamp(request, 0.1), 'cwd': str(f.repo), 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': 'compaction-error', 'sessionId': f.native_session,
             'timestamp': self.stamp(request, 0.2), 'cwd': str(f.repo), 'isApiErrorMessage': True,
             'error': 'invalid_request',
             'message': {'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence',
                         'content': [{'type': 'text', 'text': native.COMPACTION_THRASHING_TEXT}]}},
        ]
        self.write_events()
        state = f.state()
        state['native_outcome_source'] = {'database': str(f.database), 'transcript': str(self.transcript),
                                          'session_id': request['session_id'],
                                          'native_session_id': f.native_session}
        ao.atomic(f.directory() / 'state.json', state)

    def stamp(self, request, delta):
        return datetime.fromtimestamp(request['created_at'] + delta, timezone.utc).isoformat()

    def write_events(self):
        self.transcript.write_text('\n'.join(json.dumps(event) for event in self.events) + '\n')
        self.transcript.chmod(0o600)

    def audit(self):
        return self.fixture.service.ao_room_outcome_audit(self.fixture.room)

    def settle(self):
        f = self.fixture
        audit = self.audit()
        self.assertTrue(audit['resume_eligible'])
        f.service.ao_room_outcome_resume(
            f.room, self.request_id, audit['outcome_sha256'], 'resume',
            'Exact native autocompact-thrashing terminal error inspected; one bounded recovery authorized',
            'Continue once with bounded reads')
        return audit

    def posts(self):
        return len(self.fixture.fake.posts)

    def assert_no_posts(self, count):
        self.assertEqual(len(self.fixture.fake.posts), count)


class RefreshedCompactionServiceTests(CompactionServiceFixture):
    def test_settlement_is_readable_before_refresh_and_send_refuses_old_guard(self):
        f = self.fixture
        self.settle()
        request = f.state()['requests'][self.request_id]
        self.assertEqual(request['state'], 'settled_failure')
        release = json.loads((f.directory() / request['outcome_resume']).read_text())
        self.assertEqual(release['native_failure_settlement']['kind'], 'compaction_thrashing')
        self.assertEqual(release['native_failure_settlement']['required_guard_sha256'],
                         outcomes._reviewed_guard_sha256())
        outcomes.validate_settlement(f.directory(), request)
        quality.inspect(f.directory(), f.state())
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'reviewed read-admission routing guard'):
            f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assert_no_posts(posts)

    def test_successful_audit_settle_refresh_send_with_real_native_inspect(self):
        f = self.fixture
        self.settle()
        posts = self.posts()
        result = f.do_refresh()
        self.assertFalse(result['model_dispatch'])
        self.assert_no_posts(posts)
        f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assertEqual(f.state()['requests']['resume']['text'], 'Continue.')
        self.assertEqual(self.posts(), posts + 1)

    def test_stale_source_between_settlement_and_refresh_refuses_without_intent(self):
        f = self.fixture
        self.settle()
        before = f.state()
        request = f.state()['requests'][self.request_id]
        self.events.append({'type': 'user', 'uuid': 'later-human', 'sessionId': f.native_session,
                            'timestamp': self.stamp(request, 0.3), 'cwd': str(f.repo),
                            'origin': {'kind': 'human'},
                            'message': {'role': 'user', 'content': 'A different later instruction.'}})
        self.write_events()
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'Unused compaction continuation source changed'):
            f.do_refresh()
        self.assertEqual(f.state(), before)
        self.assertFalse((f.directory() / routing_fixtures.refresh.BASE).exists())
        self.assert_no_posts(posts)

    def test_changed_ao_safety_evidence_refuses_refresh_without_intent_or_dispatch(self):
        f = self.fixture
        self.settle()
        before = f.state()
        request = before['requests'][self.request_id]
        outcome = f.directory() / request['semantic_outcome']
        outcome_raw = outcome.read_bytes()
        outcome_names = sorted(path.name for path in outcome.parent.iterdir())
        turn = next(item for item in f.fake.snapshots['engineer']['turns'] if item['id'] == request['turn_id'])
        turn['error'] = {'type': 'safety'}
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'Unused compaction continuation AO evidence changed'):
            f.do_refresh()
        self.assertEqual(f.state(), before)
        self.assertEqual(outcome.read_bytes(), outcome_raw)
        self.assertEqual(sorted(path.name for path in outcome.parent.iterdir()), outcome_names)
        self.assertFalse((f.directory() / routing_fixtures.refresh.BASE).exists())
        self.assert_no_posts(posts)
        audit = self.audit()
        self.assertFalse(audit['resume_eligible'])
        self.assert_no_posts(posts)

    def assert_changed_eligible_evidence_refuses_refresh(self, change):
        f = self.fixture
        self.settle()
        request = f.state()['requests'][self.request_id]
        change(f, request)
        audit = self.audit()
        self.assertTrue(audit['resume_eligible'])
        before = f.state()
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'Unused compaction continuation AO evidence changed'):
            f.do_refresh()
        self.assertEqual(f.state(), before)
        self.assertFalse((f.directory() / routing_fixtures.refresh.BASE).exists())
        self.assert_no_posts(posts)

    def test_late_terminal_error_message_refuses_refresh_without_mutation(self):
        def change(f, request):
            turn = next(item for item in f.fake.snapshots['engineer']['turns'] if item['id'] == request['turn_id'])
            turn['errorMessage'] = 'Late upstream provider message.'
        self.assert_changed_eligible_evidence_refuses_refresh(change)

    def test_upstream_provider_activity_refuses_refresh_without_mutation(self):
        def change(f, request):
            f.fake.snapshots['engineer'].setdefault('activities', []).append({
                'id': 'late-upstream', 'turnId': request['turn_id'], 'activityKind': 'system',
                'status': 'completed',
                'detail': {'event': 'provider.failure', 'category': 'upstream', 'severity': 'error'}})
        self.assert_changed_eligible_evidence_refuses_refresh(change)

    def test_attributed_upstream_session_failure_refuses_refresh_without_mutation(self):
        def change(f, request):
            f.fake.snapshots['engineer'].setdefault('sessionFailures', []).append({
                'turnId': request['turn_id'], 'type': 'upstream_provider', 'kind': 'upstream_provider'})
        self.assert_changed_eligible_evidence_refuses_refresh(change)

    def test_matching_fast_path_repeats_exact_args_and_rechecks_changed_source(self):
        f = self.fixture
        audit = self.settle()
        args = (f.room, self.request_id, audit['outcome_sha256'], 'resume',
                'Exact native autocompact-thrashing terminal error inspected; one bounded recovery authorized',
                'Continue once with bounded reads')
        first = f.service.ao_room_outcome_resume(*args)
        posts = self.posts()
        second = f.service.ao_room_outcome_resume(*args)
        self.assertEqual(first, second)
        self.assert_no_posts(posts)
        request = f.state()['requests'][self.request_id]
        self.events.append({'type': 'user', 'uuid': 'later-human', 'sessionId': f.native_session,
                            'timestamp': self.stamp(request, 0.3), 'cwd': str(f.repo),
                            'origin': {'kind': 'human'},
                            'message': {'role': 'user', 'content': 'A different later instruction.'}})
        self.write_events()
        with self.assertRaisesRegex(ao.RoomError, 'evidence changed'):
            f.service.ao_room_outcome_resume(*args)
        self.assert_no_posts(posts)

    def test_later_streaming_or_tool_response_is_ineligible_at_audit(self):
        f = self.fixture
        request = f.state()['requests'][self.request_id]
        self.events.append({'type': 'assistant', 'uuid': 'later-tool', 'sessionId': f.native_session,
                            'timestamp': self.stamp(request, 0.3), 'cwd': str(f.repo),
                            'message': {'role': 'assistant', 'model': '<synthetic>', 'id': 'message-id',
                                        'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {}}]}})
        self.write_events()
        audit = self.audit()
        self.assertFalse(audit['resume_eligible'])
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'exactly correlated'):
            f.service.ao_room_outcome_resume(
                f.room, self.request_id, audit['outcome_sha256'], 'resume',
                'Inspect the streaming response', 'Continue')
        self.assert_no_posts(posts)


class CompletedTransportCompactionTests(CompactionServiceFixture):
    TURN_STATE = 'completed'

    def test_completed_transport_compaction_is_explicitly_refused(self):
        f = self.fixture
        request = f.state()['requests'][self.request_id]
        self.assertEqual(request['state'], 'completed')
        audit = self.audit()
        self.assertFalse(audit['resume_eligible'])
        posts = self.posts()
        with self.assertRaisesRegex(ao.RoomError, 'AO-completed'):
            f.service.ao_room_outcome_resume(
                f.room, self.request_id, audit['outcome_sha256'], 'resume',
                'Completed transport must not use the failed-turn settlement', 'Continue once')
        self.assert_no_posts(posts)


if __name__ == '__main__':
    unittest.main()
