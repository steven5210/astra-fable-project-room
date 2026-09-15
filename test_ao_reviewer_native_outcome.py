"""Synthetic reviewer-native audit through normalization and exact acceptance.

Real read-only SQLite owner lookup and bounded JSONL inspection; fake AO only.
No account, provider, native process or live session is accessed.
"""
import copy
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch

import ao_native_outcome as native
import ao_outcomes
import ao_project_room as ao
import ao_review_extension
import ao_workflow
import test_ao_project_room as fixtures


class ReviewerNativeFixture(unittest.TestCase):
    REVIEW_EFFORT = 'max'
    commit = fixtures.AdapterTests.commit
    state = fixtures.AdapterTests.state

    def setUp(self):
        fixtures.AdapterTests.setUp(self)
        self.native_directory = (self.root / 'native-evidence').resolve()
        self.native_directory.mkdir()
        self.workspace = (self.root / 'reviewer-workspace').resolve()
        self.workspace.mkdir()
        self.database = self.native_directory / 'ao.db'
        self.transcript = self.native_directory / 'native-reviewer.jsonl'
        self.fake.add('reviewer', harness='claude-code')
        self.fake.snapshots['reviewer']['settings']['model'] = ao_workflow.FABLE_MODEL
        self.fake.snapshots['reviewer']['settings']['reasoningEffort'] = self.REVIEW_EFFORT
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', ao_workflow.FABLE_MODEL, self.REVIEW_EFFORT,
                                  fable_reason='Explicit independent Fable review of this exact candidate')
        self.service.ao_room_verify(self.room, str(self.repo))
        self.service.ao_room_send(self.room, 'reviewer', 'Read-only independent review.', 'review-1')
        self.request = self.state()['requests']['review-1']
        self.verdict = {**self.request['review'], 'decision': 'approved',
                        'review': 'Inspected the synthetic candidate and exact verification evidence.'}
        self.prefix = 'Independent review completed; the exact verdict follows.'.ljust(169) + '\n'
        self.assertEqual(len(self.prefix), 170)
        self.raw = self.prefix + json.dumps(self.verdict)
        self.fake.finish('reviewer', self.raw)
        self.fake.snapshots['reviewer']['turns'][-1].pop('stopReason')
        self.service.ao_room_sync(self.room)
        self.request = self.state()['requests']['review-1']
        self.anchor = {'type': 'user', 'uuid': 'caller', 'sessionId': 'native-reviewer',
                       'timestamp': self.time(1), 'cwd': str(self.workspace), 'origin': {'kind': 'human'},
                       'message': {'content': self.request['text']}}
        self.final = {'type': 'assistant', 'uuid': 'final', 'sessionId': 'native-reviewer',
                      'timestamp': self.time(2), 'cwd': str(self.workspace),
                      'message': {'id': 'response', 'model': ao_workflow.FABLE_MODEL, 'stop_reason': 'end_turn',
                                  'content': [{'type': 'text', 'text': self.raw}]}}
        self.write_events([self.anchor, self.final])
        with sqlite3.connect(self.database) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       ('reviewer', 'project', 'claude-code', 'chat', 0, 'idle', str(self.workspace), 'native-reviewer', 'generation'))
            db.execute('INSERT INTO conversations VALUES(?,?,?)', ('reviewer-native', 'reviewer', 'root'))
            db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                       ('root', 'reviewer-native', 'native-reviewer', 'reviewer', 'native', 0))
        self.database.chmod(0o600)

    def directory(self):
        return self.service.root / 'rooms' / self.room

    def time(self, delta):
        return datetime.fromtimestamp(self.request['created_at'] + delta, timezone.utc).isoformat()

    def write_events(self, events):
        self.transcript.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
        self.transcript.chmod(0o600)

    def files(self):
        return {str(p.relative_to(self.directory())): p.read_bytes() for p in self.directory().rglob('*') if p.is_file()}

    def audit(self, explicit=True):
        paths = {'ao_database_path': str(self.database), 'native_transcript_path': str(self.transcript)} if explicit else {}
        return self.service.ao_room_outcome_audit(self.room, role='reviewer', **paths)

    def normalization(self):
        request = self.state()['requests']['review-1']
        return {'room_id': self.room, 'request_id': 'review-1', 'receipt_sha256': request['receipt_sha256'],
                'final_text_sha256': ao.digest(self.raw.encode()), 'json_start': len(self.prefix), 'json_end': len(self.raw),
                'astra_review': 'I inspected the entire raw reply. Its preamble adds no contradictory or additional verdict.',
                'confirm_no_additional_verdict': True}

    def prove_final(self):
        self.assertEqual(self.audit()['outcome']['kind'], 'unknown')
        self.assertEqual(self.audit(False)['outcome']['kind'], 'final_available')

    def assert_audit_refuses_unchanged(self, pattern=None):
        files, posts = self.files(), copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex((ao.RoomError, ValueError), pattern or '.'):
            self.audit()
        self.assertEqual(self.files(), files)
        self.assertEqual(self.fake.posts, posts)


class ReviewerNativeOutcomeTests(ReviewerNativeFixture):
    def test_two_audits_then_operator_normalization_then_exact_acceptance(self):
        original, files, posts = self.state(), self.files(), copy.deepcopy(self.fake.posts)
        self.assertNotIn('preparation', original)
        self.assertEqual(original['requests']['review-1']['semantic_status']['kind'], 'unknown')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_response_normalize(**self.normalization())
        first = self.audit()
        self.assertFalse(first['model_dispatch']); self.assertFalse(first['quota_reset_established'])
        self.assertEqual(first['outcome']['kind'], 'unknown')
        self.assertEqual(first['native']['stop_reasons'], ['end_turn'])
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_response_normalize(**self.normalization())
        second = self.audit(False)
        self.assertEqual(second['outcome']['kind'], 'final_available')
        after_audit = self.files()
        self.assertEqual(self.audit(False), second)
        self.assertEqual(self.files(), after_audit)
        with self.assertRaisesRegex(ao.RoomError, 'one JSON verdict'):
            self.service.ao_room_accept(self.room, 'review-1')
        result = self.service.ao_room_response_normalize(**self.normalization())
        self.assertTrue(result['normalized']); self.assertFalse(result['model_dispatch'])
        self.assertEqual(self.state()['acceptances'], [])
        accepted = self.service.ao_room_accept(self.room, 'review-1')
        self.assertTrue(accepted['accepted'])
        self.assertEqual(accepted['reviewer_model'], ao_workflow.FABLE_MODEL)
        self.assertEqual({k: accepted[k] for k in self.request['review']}, self.request['review'])
        current = self.state(); request = current['requests']['review-1']
        for key in ('receipt_sha256', 'usage', 'text', 'text_sha256', 'session_id', 'model', 'reasoning_effort', 'baseline', 'created_order'):
            self.assertEqual(request[key], original['requests']['review-1'][key])
        self.assertEqual(current['bindings'], original['bindings'])
        self.assertEqual(set(current['requests']), {'review-1'})
        self.assertNotIn('outcome_resume', request)
        self.assertNotIn('native_outcome_source', current)
        self.assertEqual(current['native_reviewer_outcome_source']['workspace_path'], str(self.workspace))
        self.assertEqual(self.fake.posts, posts)
        for path, content in files.items():
            if path != 'state.json':
                self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_engineer_source_and_proof_are_never_replaced_or_used_by_reviewer(self):
        state = self.state()
        old_source = {'database': '/synthetic/engineer.db', 'transcript': '/synthetic/engineer.jsonl',
                      'session_id': 'engineer', 'native_session_id': 'engineer-native'}
        old_proof = {'path': 'outcomes/engineer/original.json', 'sha256': 'a' * 64, 'request_id': 'engineer-old'}
        state.update(native_outcome_source=old_source, native_outcome_source_evidence=old_proof)
        self.service.save(self.directory(), state)
        self.prove_final()
        current = self.state()
        self.assertEqual(current['native_outcome_source'], old_source)
        self.assertEqual(current['native_outcome_source_evidence'], old_proof)
        self.assertEqual(current['native_reviewer_outcome_source']['session_id'], 'reviewer')
        self.assertEqual(current['native_reviewer_outcome_source_evidence']['request_id'], 'review-1')

    def test_cross_role_sources_requests_and_shared_bindings_refuse(self):
        source = {'database': str(self.database), 'transcript': str(self.transcript),
                  'session_id': 'reviewer', 'native_session_id': 'native-reviewer', 'workspace_path': str(self.workspace)}
        with self.assertRaises(ao.RoomError): native.validate_source(self.state(), source)
        wrong = {k: v for k, v in source.items() if k != 'workspace_path'}
        with self.assertRaises(ao.RoomError): native.validate_source(self.state(), wrong, 'reviewer')
        with self.assertRaises(ao.RoomError): native.validate_source(self.state(), source, 'observer')
        request = copy.deepcopy(self.request); request['session_id'] = 'engineer'
        with self.assertRaisesRegex(ao.RoomError, 'request/model/history'):
            native.inspect(self.directory(), self.state(), request, source, self.fake.conversation('reviewer'))
        state = self.state(); state['bindings']['engineer'] = dict(state['bindings']['reviewer'])
        with self.assertRaisesRegex(ao.RoomError, 'exact Astra-led binding'):
            native.validate_source(state, source, 'reviewer')

    def test_native_owner_project_conversation_branch_and_replay_drift_refuse(self):
        original = self.database.read_bytes()
        statements = ["UPDATE sessions SET project_id='foreign'", "UPDATE sessions SET harness='codex'",
                      "UPDATE conversations SET id='foreign'", "UPDATE conversation_branches SET session_id='foreign'",
                      "UPDATE conversation_branches SET provider_conversation_id='foreign'",
                      "UPDATE conversation_branches SET replay_truncated=1", "UPDATE sessions SET is_terminated=1",
                      "INSERT INTO conversations VALUES('reviewer-native','reviewer','root')"]
        for statement in statements:
            with self.subTest(statement=statement):
                with sqlite3.connect(self.database) as db: db.execute(statement)
                self.assert_audit_refuses_unchanged()
                self.database.write_bytes(original)

    def test_workspace_mismatch_missing_cwd_and_nonterminal_model_reroute_refuse(self):
        for key, value in [('cwd', '/foreign'), ('cwd', None)]:
            for target in ('anchor', 'final'):
                with self.subTest(target=target, key=key, value=value):
                    anchor, final = copy.deepcopy(self.anchor), copy.deepcopy(self.final)
                    (anchor if target == 'anchor' else final)[key] = value
                    self.write_events([anchor, final]); self.assert_audit_refuses_unchanged('workspace')
        changed = copy.deepcopy(self.final)
        changed['message'].update(model='foreign-model', stop_reason=None)
        self.write_events([self.anchor, changed]); self.assert_audit_refuses_unchanged('model')

    def test_exact_caller_ambiguity_wrong_native_session_and_child_only_evidence_refuse(self):
        for events in ([{**self.anchor, 'sessionId': 'foreign'}, self.final],
                       [{**self.anchor, 'isSidechain': True}, self.final],
                       [self.anchor, {**self.anchor, 'uuid': 'second-caller'}, self.final],
                       [{**self.anchor, 'message': {'content': self.request['text'] + ' '}}, self.final]):
            with self.subTest(events=events):
                self.write_events(events); self.assert_audit_refuses_unchanged('caller correlation')

    def test_public_model_effort_harness_session_branch_and_truncated_history_refuse(self):
        snapshot = copy.deepcopy(self.fake.snapshots['reviewer'])
        session = copy.deepcopy(self.fake.sessions['reviewer'])
        mutations = [lambda: self.fake.snapshots['reviewer']['settings'].update(model='other'),
                     lambda: self.fake.snapshots['reviewer']['settings'].update(reasoningEffort='high'),
                     lambda: self.fake.snapshots['reviewer'].update(sessionId='foreign'),
                     lambda: self.fake.snapshots['reviewer'].update(activeBranchId='foreign'),
                     lambda: self.fake.snapshots['reviewer'].update(history_truncated=True),
                     lambda: self.fake.sessions['reviewer'].update(harness='codex')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                mutate(); self.assert_audit_refuses_unchanged()
                self.fake.snapshots['reviewer'] = copy.deepcopy(snapshot)
                self.fake.sessions['reviewer'] = copy.deepcopy(session)

    def test_later_human_truncation_and_late_quota_never_authorize_normalization(self):
        later_human = {**self.anchor, 'uuid': 'later-human', 'timestamp': self.time(3), 'message': {'content': 'Different instruction'}}
        truncated = copy.deepcopy(self.final); truncated['message']['stop_reason'] = 'max_tokens'
        error = {'type': 'assistant', 'uuid': 'error', 'sessionId': 'native-reviewer', 'timestamp': self.time(3),
                 'cwd': str(self.workspace), 'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
                 'message': {'model': '<synthetic>', 'content': [{'type': 'text', 'text': 'Synthetic quota failure'}]}}
        initial = self.files()
        for events, expected in (([self.anchor, self.final, later_human], 'unknown'),
                                 ([self.anchor, truncated], 'output_truncated'),
                                 ([self.anchor, self.final, error], 'quota_limit')):
            with self.subTest(expected=expected):
                self.write_events(events); self.audit(); result = self.audit(False)
                self.assertEqual(result['outcome']['kind'], expected)
                self.assertFalse(result['quota_reset_established'])
                with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
                    self.service.ao_room_response_normalize(**self.normalization())
                self.assertEqual(len(self.fake.posts), 1)
                for path, content in initial.items():
                    (self.directory() / path).write_bytes(content)

    def test_source_paths_can_move_but_saved_native_identity_and_workspace_cannot(self):
        self.prove_final()
        old = self.state()['native_reviewer_outcome_source']
        moved = self.native_directory / 'moved'; moved.mkdir()
        self.transcript = moved / self.transcript.name
        self.write_events([self.anchor, self.final])
        self.audit()
        self.assertNotEqual(self.state()['native_reviewer_outcome_source']['transcript'], old['transcript'])
        original = self.database.read_bytes()
        for statements in (("UPDATE sessions SET workspace_path='/foreign'",),
                           ("UPDATE sessions SET provider_conversation_id='replacement'", "UPDATE conversation_branches SET provider_conversation_id='replacement'")):
            with self.subTest(statements=statements):
                with sqlite3.connect(self.database) as db:
                    for statement in statements: db.execute(statement)
                self.assert_audit_refuses_unchanged('retained owner or workspace')
                self.database.write_bytes(original)

    def test_reviewer_cannot_bypass_engineer_fourth_review_owner_guard(self):
        from ao_native_identity import read_owner
        state = self.state(); state['spec_review_extension'] = {'synthetic': True}
        owner = read_owner(self.database, 'reviewer')
        with patch.object(ao_review_extension, '_anchor') as anchor:
            with self.assertRaisesRegex(ao.RoomError, 'without an engineer review grant'):
                ao_review_extension.guard_native_owner(self.service, state, owner, role='reviewer')
            anchor.assert_not_called()
        state['spec_review_extension'] = {'synthetic': True}
        with patch.object(ao_review_extension, '_anchor', return_value={'native_owner': {}}) as anchor:
            with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
                ao_review_extension.guard_native_owner(self.service, state, owner)
            anchor.assert_called_once()

    def test_stale_spec_candidate_gate_and_changed_live_receipt_still_refuse(self):
        self.prove_final()
        before = self.files()
        (self.repo / 'feature.txt').write_text('Changed after review\n')
        with self.assertRaises(ao.RoomError): self.service.ao_room_response_normalize(**self.normalization())
        (self.repo / 'feature.txt').write_text('verified behavior\n')
        checkpoint = self.service.checkpoint(self.directory(), self.state())
        log = self.directory() / checkpoint['gates'][0]['log']; saved = log.read_bytes(); log.write_text('Changed evidence')
        with self.assertRaises(ao.RoomError): self.service.ao_room_response_normalize(**self.normalization())
        log.write_bytes(saved)
        self.fake.snapshots['reviewer']['messages'][-1]['text'] += ' extra verdict'
        with self.assertRaises(ao.RoomError): self.service.ao_room_response_normalize(**self.normalization())
        self.fake.snapshots['reviewer']['messages'][-1]['text'] = self.raw
        self.service.ao_room_spec_put(self.room, 2, 'A different exact contract.', self.gates, 'Actual revised scope approval')
        with self.assertRaisesRegex(ao.RoomError, 'stale specification'):
            self.service.ao_room_response_normalize(**self.normalization())
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.state()['acceptances'], [])
        for path, content in before.items():
            if path != 'state.json': self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_late_source_failures_remain_sticky_after_native_final_returns(self):
        self.prove_final()
        error = {**self.final, 'uuid': 'late-error', 'timestamp': self.time(3), 'isApiErrorMessage': True,
                 'error': 'rate_limit', 'apiErrorStatus': 429, 'message': {'model': '<synthetic>'}}
        self.write_events([self.anchor, self.final, error])
        quota = self.audit(False)
        self.assertEqual(quota['outcome']['kind'], 'quota_limit')
        self.write_events([self.anchor, self.final])
        self.assertEqual(self.audit()['outcome']['kind'], 'quota_limit')
        self.assertEqual(self.audit(False)['outcome']['kind'], 'quota_limit')
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_response_normalize(**self.normalization())
        self.assertEqual(len(self.fake.posts), 1)

    def test_saved_source_without_end_turn_cannot_reuse_prior_completion(self):
        self.prove_final()
        original = self.files()
        final = copy.deepcopy(self.final); final['message'].pop('stop_reason')
        self.write_events([self.anchor, final])
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                result = self.audit(explicit)
                self.assertEqual(result['native']['stop_reasons'], [])
                self.assertEqual(result['outcome']['kind'], 'unknown')
                self.assertTrue(result['outcome']['hold'])
                self.assertEqual(self.state()['requests']['review-1']['semantic_status'], result['outcome'])
                with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
                    self.service.ao_room_response_normalize(**self.normalization())
        self.assertEqual(len(self.fake.posts), 1)
        for path, content in original.items():
            if path != 'state.json': self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_no_path_audit_rechecks_saved_workspace_and_revokes_available_outcome(self):
        self.prove_final()
        source = copy.deepcopy(self.state()['native_reviewer_outcome_source'])
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET workspace_path='/different-reviewer-workspace'")
        result = self.audit(False)
        self.assertEqual(result['outcome']['kind'], 'unknown')
        self.assertTrue(result['outcome']['hold'])
        self.assertIn('workspace', result['native']['unknown'])
        self.assertEqual(self.state()['native_reviewer_outcome_source'], source)
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_response_normalize(**self.normalization())
        self.assertEqual(len(self.fake.posts), 1)

    def test_legacy_binding_without_conversation_ids_refuses_before_source_registration(self):
        state = self.state()
        state['bindings']['reviewer'].pop('conversation_id')
        state['bindings']['reviewer'].pop('branch_id')
        self.service.save(self.directory(), state)
        with patch.object(ao_review_extension, 'guard_native_owner', wraps=ao_review_extension.guard_native_owner) as guard:
            self.assert_audit_refuses_unchanged('exact Astra-led native owner')
            guard.assert_called_once()
        self.assertNotIn('native_reviewer_outcome_source', self.state())

    def test_direct_inspect_rejects_mutated_public_identity_before_reading_native_events(self):
        source = {'database': str(self.database), 'transcript': str(self.transcript), 'session_id': 'reviewer',
                  'native_session_id': 'native-reviewer', 'workspace_path': str(self.workspace)}
        mutations = [({'settings': {'model': 'foreign', 'reasoningEffort': 'max'}}, 'request/model/history'),
                     ({'settings': {'model': ao_workflow.FABLE_MODEL, 'reasoningEffort': 'high'}}, 'request/model/history'),
                     ({'sessionId': 'foreign'}, 'request/model/history'),
                     ({'history_truncated': True}, 'request/model/history'),
                     ({'activeBranchId': 'foreign'}, 'workspace/conversation identity'),
                     ({'conversationId': 'foreign'}, 'workspace/conversation identity')]
        original = self.files()
        with patch.object(native, 'events_outcome', side_effect=AssertionError('Invalid snapshot reached native event parsing')):
            for changes, expected in mutations:
                with self.subTest(changes=changes):
                    snapshot = {**self.fake.conversation('reviewer'), **changes}
                    with self.assertRaisesRegex(ao.RoomError, expected):
                        native.inspect(self.directory(), self.state(), self.request, source, snapshot)
        self.assertEqual(self.files(), original)

    def test_codex_astra_led_reviewer_keeps_ordinary_outcome_and_refuses_native_claude_source(self):
        other = fixtures.AdapterTests(); other.setUp(); self.addCleanup(other.doCleanups)
        other.bind('reviewer')
        other.service.ao_room_verify(other.room, str(other.repo))
        other.send('reviewer')
        other.fake.finish('reviewer', other.verdict())
        other.service.ao_room_sync(other.room)
        directory = other.service.root / 'rooms' / other.room
        files = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        posts = copy.deepcopy(other.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exact Astra-led native owner'):
            other.service.ao_room_outcome_audit(other.room, role='reviewer', ao_database_path=str(self.database),
                                               native_transcript_path=str(self.transcript))
        self.assertEqual({str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}, files)
        self.assertEqual(other.fake.posts, posts)
        self.assertNotIn('native_reviewer_outcome_source', other.state())
        self.assertEqual(other.service.ao_room_outcome_audit(other.room, role='reviewer')['outcome']['kind'], 'final_available')

    def test_normal_codex_reviewer_never_enters_native_claude_source_lane(self):
        import test_ao_normal
        other = test_ao_normal.Fixture(); other.setUp(); self.addCleanup(other.doCleanups)
        other.room = other.open(); other.spec(); other.bind(); other.agree(); other.implement(); other.review()
        directory = other.directory()
        files = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        posts = copy.deepcopy(other.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'exact Astra-led native owner'):
            other.service.ao_room_outcome_audit(other.room, role='reviewer', ao_database_path=str(self.database),
                                               native_transcript_path=str(self.transcript))
        self.assertEqual({str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}, files)
        self.assertEqual(other.fake.posts, posts)
        self.assertNotIn('native_reviewer_outcome_source', other.state())
        self.assertEqual(other.service.ao_room_outcome_audit(other.room, role='reviewer')['outcome']['kind'], 'final_available')

    def test_cwd_less_native_error_is_unknown_and_cannot_reuse_completed_source(self):
        self.prove_final()
        error = {'type': 'assistant', 'uuid': 'error', 'sessionId': 'native-reviewer', 'timestamp': self.time(3),
                 'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
                 'message': {'model': '<synthetic>'}}
        self.write_events([self.anchor, self.final, error])
        result = self.audit(False)
        self.assertEqual(result['outcome']['kind'], 'unknown')
        self.assertIn('workspace', result['native']['unknown'])
        self.assertFalse(result['resume_eligible'])
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_response_normalize(**self.normalization())
        self.assertEqual(len(self.fake.posts), 1)

    def test_first_workspace_pin_trusts_explicit_storage_without_existence_or_candidate_inference(self):
        self.workspace.rmdir()
        self.assertFalse(self.workspace.exists())
        self.assertNotEqual(self.workspace, self.repo.resolve())
        self.prove_final()
        self.assertEqual(self.state()['native_reviewer_outcome_source']['workspace_path'], str(self.workspace))
        self.assertFalse(self.workspace.exists())
        self.assertEqual(len(self.fake.posts), 1)


class ReviewerNonMaxNativeOutcomeTests(ReviewerNativeFixture):
    REVIEW_EFFORT = 'high'

    def test_consistently_non_max_reviewer_refuses_direct_and_supported_source_admission(self):
        from ao_native_identity import read_owner
        state = self.state(); binding = state['bindings']['reviewer']
        self.assertEqual(binding['reasoning_effort'], 'high')
        self.assertEqual(self.request['reasoning_effort'], 'high')
        self.assertEqual(self.fake.snapshots['reviewer']['settings']['reasoningEffort'], 'high')
        source = {'database': str(self.database), 'transcript': str(self.transcript), 'session_id': 'reviewer',
                  'native_session_id': 'native-reviewer', 'workspace_path': str(self.workspace)}
        files, posts = self.files(), copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'MAX'):
            native.validate_source(state, source, 'reviewer')
        with self.assertRaisesRegex(ao.RoomError, 'MAX'):
            native.inspect(self.directory(), state, self.request, source, self.fake.conversation('reviewer'))
        with self.assertRaisesRegex(ao.RoomError, 'MAX'):
            ao_review_extension.guard_native_owner(self.service, state, read_owner(self.database, 'reviewer'), role='reviewer')
        self.assert_audit_refuses_unchanged('MAX')
        self.assertEqual(self.files(), files); self.assertEqual(self.fake.posts, posts)
        self.assertEqual(self.state()['requests']['review-1']['semantic_status']['kind'], 'unknown')
        self.assertNotIn('native_reviewer_outcome_source', self.state())


if __name__ == '__main__':
    unittest.main()
