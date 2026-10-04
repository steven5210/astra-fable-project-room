"""Read-only progress view of one owned request: bounded transcript fold, one-time part and surface registration."""

import contextlib
from datetime import datetime, timezone
import io
import json
import unittest
import uuid
from unittest.mock import patch

import ao_progress
import ao_project_room as ao
import project_room
import project_room_mcp
from test_ao_normal import Fixture


MODEL = Fixture.QUALIFIED_MODEL


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _identity():
    return str(uuid.uuid4())


class ProgressViewTests(Fixture):
    LONG_TEXT = 'Implementation evidence complete. ' + 'x' * 500

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()
        self.agree()
        self.workspace = self.native_row_workspace()
        spec_request = self.state()['requests']['spec_review']
        # An earlier turn's TodoWrite, later replaced by this turn's TaskList.
        stamp = lambda offset: _iso(spec_request['created_at'] + offset)
        self.native_events.extend([
            self._assistant('todo-write', stamp(0.01),
                            [self._use('tu-todo', 'TodoWrite',
                                      {'todos': [{'content': 'Superseded unit', 'status': 'in_progress',
                                                  'activeForm': 'Running the superseded unit'},
                                                 {'content': 'Superseded second', 'status': 'pending'}]})],
                            stop='tool_use'),
            self._result('todo-write-result', stamp(0.02), 'tu-todo', 'Todos written'),
        ])
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        request = self.state()['requests']['impl-1']
        base = request['created_at'] + 1.0
        tick = [0]

        def at():
            tick[0] += 1
            return _iso(base + tick[0] / 100)

        create1 = self._assistant('create-1', at(), [self._use('tu-c1', 'TaskCreate',
                                  {'subject': 'Implement parser', 'activeForm': 'Implementing the parser'})])
        create1r = self._result('create-1-result', at(), 'tu-c1', 'Task #1 created successfully: Implement parser')
        create2 = self._assistant('create-2', at(), [self._use('tu-c2', 'TaskCreate',
                                  {'subject': 'Add parser tests'})])
        create2r = self._result('create-2-result', at(), 'tu-c2',
                                json.dumps({'task': {'id': '2'}, 'subject': 'Add parser tests'}))
        update1 = self._assistant('update-1', at(), [self._use('tu-u1', 'TaskUpdate',
                                  {'taskId': '1', 'status': 'in_progress', 'activeForm': 'Implementing the parser'})])
        update1r = self._result('update-1-result', at(), 'tu-u1', 'Task #1 updated successfully')
        update2 = self._assistant('update-2', at(), [self._use('tu-u2', 'TaskUpdate',
                                  {'taskId': '1', 'status': 'completed'})])
        update2r = self._result('update-2-result', at(), 'tu-u2', 'Task #1 updated successfully')
        update3 = self._assistant('update-3', at(), [self._use('tu-u3', 'TaskUpdate',
                                  {'taskId': '2', 'status': 'deleted'})])
        update3r = self._result('update-3-result', at(), 'tu-u3', 'Task #2 updated successfully')
        create3 = self._assistant('create-3', at(), [self._use('tu-c3', 'TaskCreate',
                                  {'subject': 'Gate evidence'})])
        create3r = self._result('create-3-result', at(), 'tu-c3', json.dumps({'task': {'id': '3'}}))
        task_list = self._assistant('task-list', at(), [self._use('tu-l1', 'TaskList', {})])
        self.task_list_result = self._result('task-list-result', at(), 'tu-l1',
                                             '#1 [completed] Implement parser\n#3 [pending] Gate evidence')
        agent1 = self._assistant('agent-1', at(), [self._use('tu-a1', 'Agent',
                                 {'subagent_type': 'Explore', 'description': 'Survey the parser module'})])
        agent1r = self._result('agent-1-result', at(), 'tu-a1', 'Done: surveyed 12 files')
        agent2 = self._assistant('agent-2', at(), [self._use('tu-a2', 'Agent',
                                 {'subagent_type': 'general-purpose', 'description': 'd' * 130})])
        agent2r = self._result('agent-2-result', at(), 'tu-a2', 'Agent failed', is_error=True)
        agent3 = self._assistant('agent-3', at(), [self._use('tu-a3', 'Agent',
                                 {'subagent_type': 'Explore', 'description': 'Long survey'})])
        agent3r = self._result('agent-3-result', at(), 'tu-a3', 'Agent terminated early: context limit reached')
        agent4 = self._assistant('agent-4', at(), [self._use('tu-a4', 'Agent',
                                 {'subagent_type': 'Plan', 'description': 'Deep plan'})])
        denied = self._assistant('guard-denial', at(), [self._use('tu-x', 'Bash', {'command': 'make test'})])
        deniedr = self._result('guard-denial-result', at(), 'tu-x',
                               'Project Room routing guard: root execution is denied (orchestrator)')
        api_error = {'type': 'assistant', 'uuid': _identity(), 'sessionId': self.NATIVE, 'timestamp': at(),
                     'cwd': self.workspace, 'isSidechain': False, 'isApiErrorMessage': True,
                     'apiError': {'status': 429}, 'error': 'rate_limit',
                     'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'rate limited'}]}}
        synthetic = self._assistant('synthetic', at(), [{'type': 'text', 'text': 'Context window nearly full'}],
                                  model='<synthetic>', stop=None)
        compaction = {'type': 'user', 'uuid': _identity(), 'sessionId': self.NATIVE, 'timestamp': at(),
                      'cwd': self.workspace, 'isSidechain': False,
                      'isCompactSummary': True, 'isVisibleInTranscriptOnly': True,
                      'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Earlier work summarized'}]}}
        sidechain = self._assistant('sidechain', at(), [{'type': 'text', 'text': 'Subagent row to ignore'}],
                                  model='claude-haiku-9', stop=None)
        sidechain['isSidechain'] = True
        self.last_row = self._assistant('final', at(), [{'type': 'text', 'text': self.LONG_TEXT}], stop='end_turn')
        self.native_events.extend([
            create1, create1r, create2, create2r, update1, update1r, update2, update2r, update3, update3r,
            create3, create3r, task_list, self.task_list_result,
            agent1, agent1r, agent2, agent2r, agent3, agent3r, agent4,
            denied, deniedr, api_error, synthetic, compaction, sidechain, self.last_row,
        ])
        self._write_transcript()
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.request = self.state()['requests']['impl-1']
        self.anchor = next(row for row in self.native_events
                           if row.get('uuid', '').startswith('impl-1-caller'))
        # The next controller request's human row ends this turn's window.
        self.trailing = {'type': 'user', 'uuid': _identity(), 'sessionId': self.NATIVE,
                         'timestamp': _iso(self.request['created_at'] + 5.0), 'cwd': self.workspace,
                         'isSidechain': False, 'origin': {'kind': 'human'},
                         'message': {'role': 'user', 'content': spec_request['text']}}
        self.native_events.append(self.trailing)
        self._write_transcript(extra=['{not json'])

    def _write_transcript(self, extra=()):
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events)
                                   + ''.join(line + '\n' for line in extra))

    def _use(self, tool_use_id, name, tool_input):
        return {'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': tool_input}

    def _assistant(self, label, stamp, content, model=MODEL, stop='tool_use'):
        message = {'role': 'assistant', 'model': model, 'id': label + '-m', 'content': content}
        if stop is not None:
            message['stop_reason'] = stop
        return {'type': 'assistant', 'uuid': 'impl-1-' + label + '-' + _identity(), 'sessionId': self.NATIVE,
                'timestamp': stamp, 'cwd': self.workspace, 'isSidechain': False, 'message': message}

    def _result(self, label, stamp, tool_use_id, text, is_error=False):
        return {'type': 'user', 'uuid': 'impl-1-' + label + '-' + _identity(), 'sessionId': self.NATIVE,
                'timestamp': stamp, 'cwd': self.workspace, 'isSidechain': False,
                'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': text, 'is_error': is_error}]}}

    def view(self, **kwargs):
        return ao_progress.inspect(self.directory(), self.state(), **kwargs)

    def test_progress_part_delivered_once_with_pinned_digest(self):
        carried = self.state()['requests']['spec_review']['carried']
        self.assertIn(ao_progress.PART, carried['parts'])
        self.assertEqual(carried['part_sha256'][ao_progress.PART], ao_progress.INSTRUCTION_SHA256)
        self.assertEqual(ao.digest(ao_progress.INSTRUCTION.encode()), ao_progress.INSTRUCTION_SHA256)
        self.assertEqual(self.state()['requests']['impl-1']['carried']['parts'], [])

    def test_latest_request_view_matches_the_folded_window(self):
        view = self.view()
        self.assertEqual(view['request'], {'request_id': 'impl-1', 'state': 'completed', 'purpose': 'implementation',
                                           'created_at': self.request['created_at'], 'turn_id': self.request['turn_id'],
                                           'configured_model': self.request['model'],
                                           'semantic_status': self.request.get('semantic_status')})
        self.assertEqual(view['source'], {'available': True})
        self.assertEqual(view['malformed_rows'], 1)
        turn = view['turn']
        self.assertTrue(turn['anchor_found'])
        self.assertEqual(turn['started_at'], self.anchor['timestamp'])
        self.assertEqual(turn['last_activity_at'], self.last_row['timestamp'])
        self.assertEqual(turn['assistant_rows'], 16)
        self.assertEqual(turn['served_models'], {MODEL: 14, '<synthetic>': 1})
        self.assertEqual(turn['stop_reasons'], {'end_turn': 2, 'tool_use': 12})
        self.assertEqual(turn['tool_calls'], {'TaskCreate': 3, 'TaskUpdate': 3, 'TaskList': 1, 'Agent': 4, 'Bash': 1})
        self.assertEqual(turn['api_errors'], 1)
        self.assertEqual(turn['compactions'], 1)
        self.assertEqual(turn['guard_refusals'], 1)
        import ao_native_outcome
        self.assertAlmostEqual(turn['elapsed_seconds'],
                               ao_native_outcome.timestamp(self.last_row['timestamp'])
                               - ao_native_outcome.timestamp(self.anchor['timestamp']), places=6)
        launches = view['launches']
        self.assertEqual([launch['status'] for launch in launches], ['completed', 'error', 'error', 'running'])
        self.assertEqual([launch['subagent_type'] for launch in launches],
                         ['Explore', 'general-purpose', 'Explore', 'Plan'])
        self.assertEqual(launches[1]['description'], 'd' * 120)
        self.assertIsNone(launches[3]['ended_at'])
        self.assertIsNone(launches[3]['duration_seconds'])
        self.assertEqual(launches[0]['started_at'], agent_stamp(self.native_events, 'agent-1'))
        self.assertAlmostEqual(launches[0]['duration_seconds'],
                               ao_native_outcome.timestamp(launches[0]['ended_at'])
                               - ao_native_outcome.timestamp(launches[0]['started_at']), places=6)
        self.assertEqual(view['plan'], {
            'source': 'task_tools', 'updated_at': self.task_list_result['timestamp'],
            'steps': [{'label': 'Implement parser', 'status': 'completed'},
                      {'label': 'Gate evidence', 'status': 'pending'}],
            'counts': {'pending': 1, 'in_progress': 0, 'completed': 1}})
        self.assertEqual(view['last_text'], self.LONG_TEXT[:400])
        self.assertEqual(len(view['last_text']), 400)
        self.assertNotIn(str(self.transcript), json.dumps(view))

    def test_last_text_bound_and_omission(self):
        self.assertEqual(len(self.view(max_text_chars=10)['last_text']), 10)
        self.assertNotIn('last_text', self.view(max_text_chars=0))
        self.assertEqual(len(self.view(max_text_chars=2000)['last_text']), len(self.LONG_TEXT))
        for invalid in (-1, 2001, '400', True, None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ao.RoomError):
                    self.view(max_text_chars=invalid)

    def test_explicit_and_unknown_request_selection(self):
        view = self.view(request_id='spec_review')
        self.assertEqual(view['request']['request_id'], 'spec_review')
        self.assertTrue(view['turn']['anchor_found'])
        self.assertEqual(turn_timestamps(view['turn']), (self.trailing['timestamp'], self.trailing['timestamp']))
        self.assertEqual(view['turn']['assistant_rows'], 0)
        self.assertEqual(self.view(request_id='impl-1')['request']['request_id'], 'impl-1')
        with self.assertRaises(ao.RoomError):
            self.view(request_id='no-such-request')

    def test_unregistered_source_reports_unavailable_without_raising(self):
        state = self.state()
        del state['native_outcome_source']
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view()
        self.assertEqual(view['request']['request_id'], 'impl-1')
        self.assertFalse(view['source']['available'])
        self.assertIsInstance(view['source']['reason'], str)
        self.assertTrue(view['source']['reason'])
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['plan'], {'source': 'none', 'updated_at': None, 'steps': [],
                                        'counts': {'pending': 0, 'in_progress': 0, 'completed': 0}})
        self.assertEqual(view['launches'], [])
        self.assertEqual(view['malformed_rows'], 0)
        self.assertNotIn('last_text', view)
        self.assertNotIn('/', json.dumps(view))

    def test_anchor_outside_the_window_reports_only_turn_boundary(self):
        with patch.object(ao_progress, 'MAX_WINDOW_BYTES', 400):
            view = self.view()
        self.assertTrue(view['source']['available'])
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['plan']['source'], 'none')
        self.assertEqual(view['launches'], [])
        self.assertEqual(view['malformed_rows'], 1)

    def test_mcp_listing_marks_progress_read_only(self):
        response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                          project_room.Service(self.home))
        matches = [item for item in response['result']['tools'] if item['name'] == 'ao_room_progress']
        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0]['annotations']['readOnlyHint'], True)
        self.assertIs(matches[0]['annotations']['destructiveHint'], False)
        self.assertEqual(set(matches[0]['inputSchema']['required']), {'room_id'})

    def test_cli_round_trip_prints_the_same_view(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(project_room.signal, 'signal'):
            exit_code = project_room.main(['--home', str(self.home), 'call', 'ao_room_progress',
                                           '--args', json.dumps({'room_id': self.room})])
        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed['request']['request_id'], 'impl-1')
        self.assertEqual(printed['source'], {'available': True})
        self.assertEqual(printed['plan'], self.view()['plan'])
        self.assertNotIn(str(self.transcript), stdout.getvalue())

    def test_view_writes_no_state(self):
        before = (self.directory() / 'state.json').read_bytes()
        self.view()
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)


def agent_stamp(events, label):
    return next(row['timestamp'] for row in events if row.get('uuid', '').startswith('impl-1-' + label))


def turn_timestamps(turn):
    return turn['started_at'], turn['last_activity_at']


class TodoWritePlanTests(Fixture):
    def test_todo_write_fold_uses_active_form_labels(self):
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        request = self.state()['requests']['impl-1']
        stamp = _iso(request['created_at'] + 1.0)
        workspace = self.native_row_workspace()
        row = {'type': 'assistant', 'uuid': _identity(), 'sessionId': self.NATIVE, 'timestamp': stamp,
               'cwd': workspace, 'isSidechain': False,
               'message': {'role': 'assistant', 'model': MODEL, 'id': 'todo-m', 'stop_reason': 'tool_use',
                           'content': [{'type': 'tool_use', 'id': 'tu-t', 'name': 'TodoWrite',
                                        'input': {'todos': [
                                            {'content': 'Read the evidence', 'status': 'in_progress',
                                             'activeForm': 'Reading the evidence'},
                                            {'content': 'Write the report', 'status': 'pending'}]}}]}}
        self.native_events.append(row)
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events))
        view = ao_progress.inspect(self.directory(), self.state())
        self.assertEqual(view['plan'], {
            'source': 'todo_write', 'updated_at': stamp,
            'steps': [{'label': 'Reading the evidence', 'status': 'in_progress'},
                      {'label': 'Write the report', 'status': 'pending'}],
            'counts': {'pending': 1, 'in_progress': 1, 'completed': 0}})


if __name__ == '__main__':
    unittest.main()
