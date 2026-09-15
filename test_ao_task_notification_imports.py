"""Typed notification history recognition using synthetic XML/JSONL, SQLite and fake AO.

No native runtime, account, provider endpoint, notification output file or model is used.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import ao_native_outcome as native
import ao_outcomes as outcomes
import ao_project_room as ao
import ao_provider_transition as transition
import test_ao_adoption as adoption_fixtures
import test_ao_normal as normal
import test_ao_reviewer_native_outcome as reviewer_fixtures

NATIVE_SESSION = '00000000-0000-4000-8000-000000000001'
NOTIFICATION_UUID = '00000000-0000-4000-8000-000000000abc'


def envelope(task='task1', tool='toolu_Ab123', status='failed'):
    return ('<task-notification>\n<task-id>' + task + '</task-id>\n<tool-use-id>' + tool + '</tool-use-id>\n'
            '<output-file>/synthetic/never-open-this.output</output-file>\n<status>' + status + '</status>\n'
            '<summary>Synthetic task failed.</summary>\n<note>Uninterpreted synthetic note.</note>\n</task-notification>')


def notification(workspace, identity=NOTIFICATION_UUID, text=None):
    return {'type': 'user', 'sessionId': NATIVE_SESSION, 'cwd': workspace, 'uuid': identity,
            'isSidechain': False, 'origin': {'kind': 'task-notification'}, 'promptSource': 'sdk',
            'queueSkipAttachments': True, 'userType': 'external', 'entrypoint': 'sdk-ts', 'version': '2.1.268',
            'message': {'role': 'user', 'content': envelope() if text is None else text}}


def imported(snapshot, row, number=1):
    namespace, identity = snapshot['activeBranchId'], row['uuid']
    turn = {'id': 'notification-turn-' + str(number), 'state': 'recovered',
            'providerTurnId': 'acp-history-turn:' + str(len(namespace.encode())) + ':' + namespace
                              + str(len(identity.encode())) + ':' + identity}
    message = {'id': 'notification-message-' + str(number), 'turnId': turn['id'], 'role': 'user', 'origin': 'human',
               'streaming': False, 'text': row['message']['content'], 'sequence': 1000 + number}
    snapshot['turns'].append(turn); snapshot['messages'].append(message)
    return turn, message


class TaskNotificationProofTests(unittest.TestCase):
    def setUp(self):
        self.workspace = '/synthetic/worktree'
        self.row = notification(self.workspace)
        self.snapshot = {'sessionId': 'engineer', 'conversationId': 'synthetic-conversation', 'activeBranchId': 'branch',
                         'history_truncated': False, 'turns': [], 'messages': []}
        self.turn, self.message = imported(self.snapshot, self.row)

    def proofs(self, rows=None, snapshot=None):
        return native.task_notification_imports(rows if rows is not None else [self.row], NATIVE_SESSION,
                                               self.snapshot if snapshot is None else snapshot, self.workspace)

    def test_exact_failed_notification_is_context_with_full_hashes_and_no_output_access(self):
        with patch.object(Path, 'open', side_effect=AssertionError('The notification output file must not be opened')):
            proofs = self.proofs()
        self.assertEqual(len(proofs), 1)
        proof = proofs[0]
        self.assertEqual(proof['kind'], 'task_notification_history_import')
        self.assertEqual(proof['notification_status'], 'failed')
        self.assertEqual(proof['native_event_sha256'], ao.digest(self.row))
        self.assertEqual(proof['text_sha256'], ao.digest(self.message['text'].encode()))
        self.assertEqual(proof['turn_sha256'], ao.digest(self.turn))
        self.assertEqual(proof['messages_sha256'], ao.digest([self.message]))
        self.assertEqual(proof['native_session_id'], NATIVE_SESSION)
        self.assertNotIn('never-open', json.dumps(proof))
        self.assertNotIn('Synthetic task failed', json.dumps(proof))
        self.assertEqual(native.compaction_imports([self.row], NATIVE_SESSION, self.snapshot), [])

    def test_human_quotes_wrong_native_scope_or_incomplete_sdk_identity_are_not_proofs(self):
        changes = [{'origin': {'kind': 'human'}}, {'origin': {'kind': 'task-notification', 'extra': True}},
                   {'origin': 'task-notification'}, {'isSidechain': True}, {'isSidechain': None}, {'isSidechain': 0},
                   {'sessionId': 'foreign'}, {'cwd': self.workspace + '/.'}, {'cwd': self.workspace.upper()},
                   {'cwd': None}, {'agentId': 'child'}, {'agent_id': 'child'}, {'type': 'assistant'},
                   {'promptSource': None}, {'queueSkipAttachments': False}, {'queueSkipAttachments': 1},
                   {'userType': 'internal'}, {'entrypoint': 'cli'}, {'isCompactSummary': False},
                   {'isVisibleInTranscriptOnly': False}, {'message': {'role': 'assistant', 'content': envelope()}},
                   {'message': {'role': 'user', 'content': [{'type': 'text', 'text': envelope()}]}}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.proofs([{**self.row, **change}]), [])
        for key in ('origin', 'isSidechain', 'cwd', 'promptSource', 'queueSkipAttachments', 'userType', 'entrypoint', 'uuid'):
            row = copy.deepcopy(self.row); row.pop(key)
            self.assertEqual(self.proofs([row]), [])
        row = copy.deepcopy(self.row); row['message'].pop('role')
        self.assertEqual(self.proofs([row]), [])
        row = copy.deepcopy(self.row); row.pop('version')
        self.assertEqual(len(self.proofs([row])), 1)  # Typed metadata, not an arbitrary version string, supplies provenance.

    def test_malformed_unknown_status_and_nonplain_envelopes_are_refused(self):
        text = envelope()
        malformed = [text + '\n', '\n' + text, 'Quoted: ' + text, '<?xml version="1.0"?>' + text,
                     text.replace('<task-notification>', '<task-notification extra="1">'),
                     text.replace('<summary>', '<summary extra="1">'),
                     text.replace('Synthetic task failed.', '<nested>failed</nested>'),
                     text.replace('Synthetic task failed.', '<![CDATA[failed]]>'),
                     text.replace('<summary>', '<!--ignored--><summary>'),
                     text.replace('<summary>', '<?ignored x?><summary>'),
                     text.replace('<task-id>task1</task-id>', '<task-id>task1</task-id>\n<task-id>task2</task-id>'),
                     text.replace('<note>Uninterpreted synthetic note.</note>\n', ''),
                     text.replace('<summary>Synthetic task failed.</summary>\n', ''),
                     text.replace('<task-id>task1</task-id>', '<tool-use-id>toolu_A</tool-use-id>'),
                     text.replace('<summary>Synthetic task failed.</summary>', '<summary/>'),
                     text.replace('Synthetic task failed.', '&undefined;'),
                     text.replace('</task-notification>', '</task-notification>\n<other/>'),
                     text.replace('<output-file>', 'extra text<output-file>'),
                     envelope(task='with space'), envelope(task='with-hyphen'), envelope(tool='notTool'),
                     *(envelope(status=status) for status in ('completed', 'running', 'cancelled', 'failed ', 'FAILED', ''))]
        for value in malformed:
            with self.subTest(text=value):
                row = notification(self.workspace, text=value)
                snapshot = copy.deepcopy(self.snapshot); snapshot['messages'][0]['text'] = value
                self.assertEqual(self.proofs([row], snapshot), [])

    def test_native_uuid_and_duplicate_event_identities_are_strict(self):
        for value in ('', None, 'not-a-uuid', NOTIFICATION_UUID.upper(), '{' + NOTIFICATION_UUID + '}'):
            row = {**self.row, 'uuid': value}
            self.assertEqual(self.proofs([row]), [])
        for extra in (self.row, {**self.row, 'timestamp': '2026-01-01T00:00:01Z'},
                      {**self.row, 'origin': {'kind': 'human'}}, {**self.row, 'isSidechain': True}):
            with self.assertRaisesRegex(ao.RoomError, 'Duplicate native'):
                self.proofs([self.row, extra])
        self.assertEqual(len(self.proofs([self.row, {**self.row, 'sessionId': 'foreign'}])), 1)

    def test_unimported_notifications_do_not_add_identity_rejections_to_old_observations(self):
        for turns in ([], [{'id': 'ordinary-turn', 'state': 'completed'}],
                      [{**self.turn, 'state': 'completed'}, {**self.turn, 'state': 'completed'}],
                      [{**self.turn, 'providerTurnId': 'unrelated-recovered-provider'}]):
            snapshot = {**self.snapshot, 'turns': turns, 'messages': [self.message, self.message]}
            self.assertEqual(self.proofs([self.row, self.row], snapshot), [])

    def test_ao_provider_turn_message_and_full_body_must_be_unique_and_exact(self):
        for changed in ({'role': 'assistant'}, {'origin': 'agent'}, {'streaming': True}, {'streaming': None},
                        {'streaming': 0}, {'text': envelope() + '\n'}, {'turnId': 'wrong'}):
            snapshot = copy.deepcopy(self.snapshot); snapshot['messages'][0].update(changed)
            self.assertEqual(self.proofs(snapshot=snapshot), [])
        for changed in ({'state': 'completed'}, {'state': 'running'}, {'providerTurnId': 'wrong'}, {'id': 'wrong'}):
            snapshot = copy.deepcopy(self.snapshot); snapshot['turns'][0].update(changed)
            self.assertEqual(self.proofs(snapshot=snapshot), [])
        for collection, extra in (('turns', self.turn), ('turns', {**self.turn, 'id': 'duplicate-provider'}),
                                  ('messages', self.message), ('messages', {**self.message, 'turnId': 'other'})):
            snapshot = copy.deepcopy(self.snapshot); snapshot[collection].append(extra)
            with self.assertRaisesRegex(ao.RoomError, 'ambiguous'):
                self.proofs(snapshot=snapshot)
        snapshot = copy.deepcopy(self.snapshot); snapshot['messages'].append({**self.message, 'id': 'second-message'})
        self.assertEqual(self.proofs(snapshot=snapshot), [])
        for changed in ({'history_truncated': True}, {'history_truncated': None}, {'activeBranchId': 'wrong'}):
            self.assertEqual(self.proofs(snapshot={**self.snapshot, **changed}), [])

    def test_two_distinct_imports_bind_utf8_branch_lengths_and_uninterpreted_leaf_text(self):
        snapshot = {**self.snapshot, 'activeBranchId': 'branch-λ', 'turns': [], 'messages': []}
        rows = [copy.deepcopy(self.row), notification(self.workspace, '00000000-0000-4000-8000-000000000abd',
                envelope().replace('Uninterpreted synthetic note.', 'Escaped &lt;instruction&gt; &amp; arbitrary text.'))]
        for number, row in enumerate(rows, 1):
            imported(snapshot, row, number)
        self.assertEqual(len(self.proofs(rows, snapshot)), 2)


def write_events(case, rows):
    case.rows = copy.deepcopy(rows)
    case.transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    case.transcript.chmod(0o600)


def source_fixture(case):
    case.request = outcomes.latest_for_role(case.state(), 'engineer')
    binding = case.state()['bindings']['engineer']
    case.database = case.root / 'synthetic-owner.db'
    case.transcript = case.root / (NATIVE_SESSION + '.jsonl')
    with sqlite3.connect(case.database) as db:
        db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
          activity_state,workspace_path,provider_conversation_id,controller_generation);
          CREATE TABLE conversations(id,current_session_id,active_branch_id);
          CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)', ('engineer', 'project', 'claude-code', 'chat', 0,
                   'idle', str(case.repo), NATIVE_SESSION, 'generation-one'))
        db.execute('INSERT INTO conversations VALUES(?,?,?)', (binding['conversation_id'], 'engineer', binding['branch_id']))
        db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                   (binding['branch_id'], binding['conversation_id'], NATIVE_SESSION, 'engineer', 'native', 0))
    case.database.chmod(0o600)
    stamp = lambda seconds: datetime.fromtimestamp(case.request['created_at'] + seconds, timezone.utc).isoformat()
    case.anchor = {'type': 'user', 'uuid': 'owned-caller', 'sessionId': NATIVE_SESSION, 'cwd': str(case.repo),
                   'isSidechain': False, 'timestamp': stamp(1), 'origin': {'kind': 'human'},
                   'message': {'role': 'user', 'content': case.request['text']}}
    case.final = {'type': 'assistant', 'uuid': 'owned-final', 'sessionId': NATIVE_SESSION, 'cwd': str(case.repo),
                  'isSidechain': False, 'timestamp': stamp(2),
                  'message': {'role': 'assistant', 'model': case.request['model'], 'id': 'owned-answer', 'stop_reason': 'end_turn'}}
    case.error = {**case.final, 'uuid': 'typed-quota', 'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
                  'message': {'role': 'assistant', 'model': '<synthetic>', 'content': 'Synthetic quota rejection.'}}
    case.notifications = [notification(str(case.repo)), notification(str(case.repo), '00000000-0000-4000-8000-000000000abd')]
    for index, row in enumerate(case.notifications, 3):
        row['timestamp'] = stamp(index)
    case.snapshot = case.fake.snapshots['engineer']
    case.snapshot.update(controller='ready', history_truncated=False,
                         branchMaterialization={'strategy': 'native', 'replayTruncated': False})
    write_events(case, [case.anchor, case.final])


def add_notifications(case, terminal=None):
    for index, row in enumerate(case.notifications, 1):
        imported(case.snapshot, row, index)
    write_events(case, [case.anchor, terminal or case.final, *case.notifications])


def audit(case):
    return case.service.ao_room_outcome_audit(case.room, ao_database_path=str(case.database),
                                            native_transcript_path=str(case.transcript))


class NotificationFixture(normal.Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required', implementation_complete=False,
                                                          remaining_gaps=['Synthetic remaining work.'])))
        self.service.ao_room_sync(self.room)
        source_fixture(self)


class NotificationOutcomeTests(unittest.TestCase):
    def fixture(self, cls=NotificationFixture):
        case = cls('runTest'); self.addCleanup(case.doCleanups); case.setUp()
        return case

    def test_supported_audit_explains_imports_without_compaction_completion_or_history_rewrite(self):
        case = self.fixture(); add_notifications(case)
        before_state = (case.directory() / 'state.json').read_bytes()
        receipt_path = case.directory() / case.request['receipt']; receipt = receipt_path.read_bytes()
        history, posts = copy.deepcopy(case.snapshot), copy.deepcopy(case.fake.posts)
        raw = case.transcript.read_bytes(); candidate = ao.candidate_snapshot(case.repo)
        with self.assertRaisesRegex(ao.RoomError, 'unverified recovered context'):
            case.service.ao_room_outcome_audit(case.room)
        self.assertEqual((case.directory() / 'state.json').read_bytes(), before_state)
        result = audit(case)
        self.assertEqual(len(result['native']['task_notification_imports']), 2)
        self.assertEqual(result['native']['compaction_imports'], [])
        self.assertFalse(result['outcome']['hold'])
        self.assertEqual(result['native']['stop_reasons'], ['end_turn'])
        self.assertEqual(outcomes.known_task_notification_turns(case.directory(), case.state(), case.snapshot),
                         {'notification-turn-1', 'notification-turn-2'})
        self.assertEqual(outcomes.known_compaction_turns(case.directory(), case.state(), case.snapshot), set())
        self.assertEqual(case.transcript.read_bytes(), raw)
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertEqual(case.snapshot, history); self.assertEqual(case.fake.posts, posts)
        self.assertEqual(ao.candidate_snapshot(case.repo), candidate)
        current = case.state()['requests']['implementation']
        self.assertNotIn('outcome_resume', current); self.assertNotIn('native_failure_settlement', current)
        self.assertEqual(current['state'], case.request['state'])

    def test_import_audit_and_known_context_preserve_existing_typed_quota_hold(self):
        case = self.fixture(); write_events(case, [case.anchor, case.error])
        first = audit(case); self.assertEqual(first['outcome']['kind'], 'quota_limit')
        old_request = case.state()['requests']['implementation']
        old_path = case.directory() / old_request['semantic_outcome']; old_bytes = old_path.read_bytes()
        add_notifications(case, case.error)
        history, posts = copy.deepcopy(case.snapshot), copy.deepcopy(case.fake.posts)
        result = audit(case)
        self.assertEqual(result['outcome'], first['outcome'])
        self.assertEqual(result['native']['errors'], first['native']['errors'])
        self.assertEqual(len(result['native']['task_notification_imports']), 2)
        self.assertEqual(outcomes.known_context_turns(case.directory(), case.state(), case.snapshot),
                         {'notification-turn-1', 'notification-turn-2'})
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold: quota_limit'):
            case.send('correction')
        current = case.state()['requests']['implementation']
        self.assertEqual(current['semantic_status'], old_request['semantic_status'])
        self.assertNotIn('outcome_resume', current); self.assertNotIn('native_failure_settlement', current)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(case.snapshot, history); self.assertEqual(case.fake.posts, posts)

    def test_mixed_compaction_and_notification_imports_keep_distinct_proofs(self):
        case = self.fixture(); add_notifications(case)
        summary = {'type': 'user', 'sessionId': NATIVE_SESSION, 'cwd': str(case.repo), 'uuid': 'synthetic-summary',
                   'isSidechain': False, 'isCompactSummary': True, 'isVisibleInTranscriptOnly': True,
                   'message': {'content': 'Synthetic retained summary.'}}
        imported(case.snapshot, summary, 3)
        write_events(case, [*case.rows, summary])
        result = audit(case)
        self.assertEqual([p['turn_id'] for p in result['native']['compaction_imports']], ['notification-turn-3'])
        self.assertEqual([p['turn_id'] for p in result['native']['task_notification_imports']],
                         ['notification-turn-1', 'notification-turn-2'])
        self.assertEqual(outcomes.known_context_turns(case.directory(), case.state(), case.snapshot),
                         {'notification-turn-1', 'notification-turn-2', 'notification-turn-3'})

    def test_notification_proof_does_not_broaden_initial_provider_transition_eligibility(self):
        case = self.fixture(); add_notifications(case); audit(case)
        state = case.state()
        self.assertNotIn('provider_transition', state)
        self.assertEqual(len(outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)), 2)
        with self.assertRaisesRegex(ao.RoomError, 'not completed.*recovered'):
            transition._native(state, state['bindings']['engineer'], case.snapshot, case.directory())

    def test_reviewer_source_does_not_gain_notification_import_support(self):
        case = self.fixture(reviewer_fixtures.ReviewerNativeFixture)
        row = notification(str(case.workspace)); row['sessionId'] = 'native-reviewer'
        row['timestamp'] = case.time(3)
        snapshot = case.fake.snapshots['reviewer']; imported(snapshot, row)
        case.write_events([case.anchor, case.final, row])
        source = {'database': str(case.database), 'transcript': str(case.transcript), 'session_id': 'reviewer',
                  'native_session_id': 'native-reviewer', 'workspace_path': str(case.workspace)}
        result = native.inspect(case.directory(), case.state(), case.request, source, snapshot)
        self.assertNotIn('task_notification_imports', result)

    def test_a_failed_notification_alone_never_supplies_a_terminal_stop_or_ends_the_caller_interval(self):
        case = self.fixture(); add_notifications(case)
        write_events(case, [case.anchor, *case.notifications])
        source = {'session_id': 'engineer', 'native_session_id': NATIVE_SESSION,
                  'database': str(case.database), 'transcript': str(case.transcript)}
        value = native.inspect(case.directory(), case.state(), case.request, source, case.snapshot)
        self.assertEqual(value['stop_reasons'], [])
        self.assertEqual(value['errors'], [])
        self.assertIsNone(value['next_human_uuid'])
        self.assertEqual(len(value['task_notification_imports']), 2)
        receipt = outcomes.receipt(case.directory(), case.request)
        receipt['turn'].pop('stopReason', None)
        receipt['messages'] = [message for message in receipt['messages'] if message['role'] != 'assistant']
        self.assertEqual(outcomes.classify(receipt, value)['kind'], 'unknown')

    def test_changed_ao_import_or_native_row_requires_reaudit_without_new_writes(self):
        case = self.fixture(); add_notifications(case); audit(case)
        state = case.state(); before = (case.directory() / 'state.json').read_bytes()
        source_raw = case.transcript.read_bytes()
        changes = [('messages', 'text', envelope() + ' changed'), ('messages', 'origin', 'agent'),
                   ('messages', 'streaming', True), ('messages', 'id', 'changed-message'),
                   ('turns', 'state', 'completed'), ('turns', 'providerTurnId', 'wrong'), ('turns', 'id', 'wrong')]
        for collection, key, value in changes:
            snapshot = copy.deepcopy(case.snapshot); snapshot[collection][-1][key] = value
            with self.subTest(collection=collection, key=key), self.assertRaisesRegex(ao.RoomError, 'task-notification import changed'):
                outcomes.known_task_notification_turns(case.directory(), state, snapshot)
        rows = copy.deepcopy(case.rows); rows[-1]['version'] = 'changed-metadata'
        write_events(case, rows)
        with self.assertRaisesRegex(ao.RoomError, 'task-notification import changed'):
            outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)
        case.transcript.write_bytes(source_raw)
        self.assertEqual(len(outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)), 2)
        self.assertEqual((case.directory() / 'state.json').read_bytes(), before)

    def test_unrelated_native_append_preserves_the_exact_import_proof_without_rewriting_it(self):
        case = self.fixture(); add_notifications(case); audit(case)
        state = case.state(); request = outcomes.latest_for_role(state, 'engineer')
        path = case.directory() / request['semantic_outcome']; original = path.read_bytes()
        old_source_hash = ao.digest(case.transcript.read_bytes())
        write_events(case, [*case.rows, {'type': 'queue-operation', 'sessionId': NATIVE_SESSION, 'operation': 'dequeue'}])
        self.assertNotEqual(ao.digest(case.transcript.read_bytes()), old_source_hash)
        self.assertEqual(len(outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)), 2)
        self.assertEqual(path.read_bytes(), original)

    def test_next_human_and_changed_source_owner_cannot_reuse_saved_notification_context(self):
        case = self.fixture(); add_notifications(case); audit(case)
        before = (case.directory() / 'state.json').read_bytes(); state = case.state()
        human = {**case.notifications[-1], 'uuid': 'later-true-human', 'origin': {'kind': 'human'}}
        write_events(case, [*case.rows, human])
        with self.assertRaisesRegex(ao.RoomError, 'task-notification import changed'):
            outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)
        with self.assertRaisesRegex(ao.RoomError, 'unverified recovered context'):
            audit(case)
        write_events(case, [case.anchor, case.final, *case.notifications])
        for updates in ({'provider_conversation_id': 'foreign'}, {'workspace_path': str(case.root)}, {'project_id': 'foreign'}):
            key, value = next(iter(updates.items()))
            with sqlite3.connect(case.database) as db:
                old = db.execute('SELECT ' + key + ' FROM sessions').fetchone()[0]
                db.execute('UPDATE sessions SET ' + key + '=?', (value,))
            with self.assertRaisesRegex(ao.RoomError, 'task-notification import changed'):
                outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)
            with sqlite3.connect(case.database) as db:
                db.execute('UPDATE sessions SET ' + key + '=?', (old,))
        self.assertEqual((case.directory() / 'state.json').read_bytes(), before)

    def test_missing_new_field_cross_role_cross_source_and_cross_session_do_not_reuse_proofs(self):
        case = self.fixture()
        result = audit(case)
        self.assertNotIn('task_notification_imports', result['native'])
        with patch.object(native, 'inspect', side_effect=AssertionError('Legacy records need no new source read')):
            self.assertEqual(outcomes.known_task_notification_turns(case.directory(), case.state(), case.snapshot), set())
        add_notifications(case); audit(case); state = case.state()
        with patch.object(native, 'inspect', side_effect=AssertionError('Foreign role/source needs no source read')):
            for field in ('sessionId',):
                self.assertEqual(outcomes.known_context_turns(case.directory(), state, {**case.snapshot, field: 'reviewer'}), set())
            foreign = copy.deepcopy(state); foreign['bindings']['engineer']['session_id'] = 'foreign'
            self.assertEqual(outcomes.known_task_notification_turns(case.directory(), foreign, case.snapshot), set())
            foreign = copy.deepcopy(state); foreign['native_outcome_source']['transcript'] = '/synthetic/foreign.jsonl'
            self.assertEqual(outcomes.known_task_notification_turns(case.directory(), foreign, case.snapshot), set())
            foreign = {**state, 'workflow': 'astra_led'}
            self.assertEqual(outcomes.known_task_notification_turns(case.directory(), foreign, case.snapshot), set())

    def test_owned_source_with_duplicate_unimported_notification_keeps_legacy_output_shape(self):
        case = self.fixture()
        write_events(case, [case.anchor, case.final, case.notifications[0], case.notifications[0]])
        source = {'session_id': 'engineer', 'native_session_id': NATIVE_SESSION,
                  'database': str(case.database), 'transcript': str(case.transcript)}
        result = native.inspect(case.directory(), case.state(), case.request, source, case.snapshot)
        expected = {**native.events_outcome(case.rows, case.request, NATIVE_SESSION),
                    'source': source, 'source_sha256': ao.digest(case.transcript.read_bytes()),
                    'compaction_imports': native.compaction_imports(case.rows, NATIVE_SESSION, case.snapshot)}
        self.assertEqual(result, expected)
        self.assertNotIn('task_notification_imports', audit(case)['native'])

    def test_changed_saved_proof_and_unsafe_transcript_are_refused(self):
        case = self.fixture(); add_notifications(case); audit(case); state = case.state()
        request = outcomes.latest_for_role(state, 'engineer'); path = case.directory() / request['semantic_outcome']
        original = path.read_bytes(); value = json.loads(original)
        value['native']['task_notification_imports'][0]['text_sha256'] = '0' * 64
        ao.atomic(path, value)
        with self.assertRaisesRegex(ao.RoomError, 'Semantic outcome evidence changed'):
            outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)
        path.write_bytes(original)
        case.transcript.chmod(0o666)
        with self.assertRaisesRegex(ao.RoomError, 'task-notification import changed'):
            outcomes.known_task_notification_turns(case.directory(), state, case.snapshot)
        case.transcript.chmod(0o600)


class NotificationProviderTests(unittest.TestCase):
    def test_native_accounting_and_dispatch_admit_only_audited_context_with_no_post(self):
        case = adoption_fixtures.AdoptionFixture('runTest'); self.addCleanup(case.doCleanups); case.setUp()
        prepared = case.configure_routing(); source_fixture(case)
        before_history = copy.deepcopy(case.snapshot)
        add_notifications(case)
        posts = copy.deepcopy(case.fake.posts)
        ao.atomic(case.directory() / 'delegate-launch.json', {'session_id': 'engineer', 'worktree': str(case.repo),
                                                             'preparation_sha256': case.state()['preparation_sha256']})
        with self.assertRaisesRegex(ao.RoomError, 'not completed.*recovered'):
            transition.dispatch_gate(case.service, case.directory(), case.state(), case.snapshot)
        audit(case)
        state = case.state(); before = (case.directory() / 'state.json').read_bytes()
        with patch.object(transition, 'native_startup_gate', return_value={'synthetic_handshake': True}) as handshake:
            transition.dispatch_gate(case.service, case.directory(), state, case.snapshot)
            handshake.assert_called_once()
            for status in ('recovered', 'failed', 'running', 'completed'):
                snapshot = copy.deepcopy(case.snapshot)
                snapshot['turns'].append({'id': 'unproved', 'providerTurnId': 'unproved-provider', 'state': status})
                with self.subTest(status=status), self.assertRaises(ao.RoomError):
                    transition.dispatch_gate(case.service, case.directory(), state, snapshot)
        accounted = transition._native(state, state['bindings']['engineer'], case.snapshot, case.directory())
        self.assertNotIn('notification-turn-1', {item['turn_id'] for item in accounted['owned_turns']})
        self.assertNotIn('notification-turn-1', {item['turn_id'] for item in accounted['unowned_completed_turns']})
        self.assertEqual(case.snapshot['turns'][:-2], before_history['turns'])
        self.assertEqual(case.snapshot['messages'][:-2], before_history['messages'])
        self.assertEqual((case.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(case.fake.posts, posts)


if __name__ == '__main__':
    unittest.main()
