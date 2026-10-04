"""Read-only progress view of one owned request: bounded transcript fold, one-time part and surface registration."""

import contextlib
from datetime import datetime, timezone
import io
import json
import os
import unittest
import uuid
from unittest.mock import patch

import ao_native_outcome
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
        # spec_review's text appears twice (its caller and the trailing echo); a single request in
        # the same-text group aligns to the LAST visible match — a text delivered twice anchors late.
        self.assertEqual(turn_timestamps(view['turn']), (self.trailing['timestamp'], self.trailing['timestamp']))
        self.assertEqual(view['turn']['assistant_rows'], 0)
        self.assertEqual(view['launches'], [])
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
        stray = self._assistant('window-stray', _iso(self.request['created_at'] + 6.0),
                                [self._use('tu-ws', 'Agent', {'subagent_type': 'Explore',
                                                            'description': 'unattributable launch'})])
        self.native_events.append(stray)
        self._write_transcript(extra=['{not json'])
        with patch.object(ao_progress, 'MAX_WINDOW_BYTES', 1200):
            view = self.view()
        self.assertTrue(view['source']['available'])
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['plan']['source'], 'none')
        # The stray launch is inside the window but nothing is attributable without an anchor.
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

    def _fstat_growth(self, step):
        real = os.fstat
        target = self.transcript.stat().st_ino
        counts = {}

        def observed(fd):
            result = real(fd)
            if result.st_ino != target:
                return result
            order = counts.get(fd, 0)
            counts[fd] = order + 1
            if order:
                fields = list(result)
                fields[6] += order * step  # st_size moves after the first stat on this descriptor
                result = os.stat_result(fields)
            return result

        return observed

    def test_a_growing_live_transcript_stays_available(self):
        with patch.object(ao_progress.os, 'fstat', self._fstat_growth(4096)):
            view = self.view()
        self.assertTrue(view['source']['available'])
        self.assertTrue(view['turn']['anchor_found'])
        self.assertEqual(view['last_text'], self.LONG_TEXT[:400])

    def test_a_truncated_transcript_reports_unavailable(self):
        with patch.object(ao_progress.os, 'fstat', self._fstat_growth(-4096)):
            view = self.view()
        self.assertFalse(view['source']['available'])
        self.assertIn('changed', view['source']['reason'])
        self.assertNotIn('/', view['source']['reason'])

    def test_a_partially_appended_last_line_is_dropped_not_malformed(self):
        self._write_transcript()
        with self.transcript.open('a') as stream:
            stream.write('{"type": "assistant", "message": {"con')
        view = self.view()
        self.assertTrue(view['source']['available'])
        self.assertEqual(view['malformed_rows'], 0)
        self.assertEqual(view['last_text'], self.LONG_TEXT[:400])
        self.assertEqual(view['turn']['assistant_rows'], 16)

    def test_launches_scope_to_the_request_turn(self):
        stamp = _iso(self.request['created_at'] + 0.5)
        early = [
            self._assistant('earlier-agent', stamp, [self._use('tu-ea', 'Agent',
                            {'subagent_type': 'Explore', 'description': 'Earlier turn launch'})]),
            self._result('earlier-agent-result', stamp, 'tu-ea', 'done'),
        ]
        self.native_events[self.native_events.index(self.anchor):0] = early
        self._write_transcript()
        view = self.view()
        self.assertEqual([launch['description'] for launch in view['launches']],
                         ['Survey the parser module', 'd' * 120, 'Long survey', 'Deep plan'])

    def test_launches_cap_at_the_64_most_recent(self):
        extra = [
            self._assistant('extra-agent-%d' % index,
                            _iso(self.request['created_at'] + 2.0 + index / 1000),
                            [self._use('tu-x%d' % index, 'Agent',
                                       {'subagent_type': 'Explore',
                                        'description': 'unit %d' % index})])
            for index in range(70)]
        self.native_events[self.native_events.index(self.trailing):0] = extra
        self._write_transcript()
        launches = self.view()['launches']
        self.assertEqual(len(launches), 64)
        self.assertEqual(launches[0]['description'], 'unit 6')
        self.assertEqual(launches[-1]['description'], 'unit 69')
        self.assertEqual(launches[0]['status'], 'running')

    def test_source_reason_never_names_a_path(self):
        with patch.object(ao_native_outcome, 'validate_registered_source',
                          side_effect=ao.RoomError('transcript /Users/x/y.jsonl missing')):
            view = self.view()
        self.assertFalse(view['source']['available'])
        self.assertNotIn('/Users', json.dumps(view))
        self.assertIn('<path>', view['source']['reason'])

    def test_progress_bypasses_the_dispatch_transcript_cap(self):
        with self.assertRaises(ao.RoomError):
            ao_native_outcome.validate_registered_source(self.directory(), self.state(),
                                                         transcript_size_limit=10)
        accepted = ao_native_outcome.validate_registered_source(self.directory(), self.state(),
                                                                transcript_size_limit=None)
        self.assertTrue(accepted['transcript_present'])
        calls = []
        real = ao_native_outcome.validate_registered_source

        def observed(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with patch.object(ao_native_outcome, 'validate_registered_source', side_effect=observed):
            view = self.view()
        self.assertTrue(view['source']['available'])
        self.assertEqual(calls, [{'transcript_size_limit': None}])

    def test_no_anchor_means_no_launches(self):
        state = self.state()
        state['requests']['impl-1']['text_sha256'] = '0' * 64
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view(request_id='impl-1')
        self.assertTrue(view['source']['available'])
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['launches'], [])
        self.assertTrue(view['plan']['steps'])

    def test_failed_task_results_do_not_advance_the_plan(self):
        stamp = _iso(self.request['created_at'] + 3.0)
        failures = [
            self._assistant('failed-update', stamp,
                            [self._use('tu-fu', 'TaskUpdate', {'taskId': '3', 'status': 'completed'})]),
            self._result('failed-update-result', stamp, 'tu-fu', 'update refused', is_error=True),
            self._assistant('false-update', stamp,
                            [self._use('tu-su', 'TaskUpdate', {'taskId': '1', 'status': 'in_progress'})]),
            self._result('false-update-result', stamp, 'tu-su',
                         json.dumps({'success': False, 'error': 'refused'})),
            self._assistant('failed-create', stamp,
                            [self._use('tu-fc', 'TaskCreate', {'subject': 'Phantom task'})]),
            self._result('failed-create-result', stamp, 'tu-fc', 'boom', is_error=True),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = failures
        self._write_transcript()
        self.assertEqual(self.view()['plan'], {
            'source': 'task_tools', 'updated_at': self.task_list_result['timestamp'],
            'steps': [{'label': 'Implement parser', 'status': 'completed'},
                      {'label': 'Gate evidence', 'status': 'pending'}],
            'counts': {'pending': 1, 'in_progress': 0, 'completed': 1}})

    def test_identical_request_texts_pick_the_ordered_anchor(self):
        state = self.state()
        first = state['requests']['impl-1']
        state['requests']['impl-2'] = dict(first, request_id='impl-2',
                                         created_order=first['created_order'] + 1,
                                         created_at=first['created_at'] + 0.5)
        ao.atomic(self.directory() / 'state.json', state)
        # With one visible row the second same-text request tail-aligns onto it.
        self.assertEqual(self.view(request_id='impl-2')['turn']['started_at'], self.anchor['timestamp'])
        stamp = _iso(self.request['created_at'] + 4.5)
        second = [
            {'type': 'user', 'uuid': _identity(), 'sessionId': self.NATIVE, 'timestamp': stamp,
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('second-agent', _iso(self.request['created_at'] + 4.6),
                            [self._use('tu-sa', 'Agent', {'subagent_type': 'Explore',
                                                        'description': 'Second turn launch'})]),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = second
        self._write_transcript()
        first_view = self.view(request_id='impl-1')
        self.assertEqual(first_view['turn']['started_at'], self.anchor['timestamp'])
        self.assertEqual(first_view['turn']['last_activity_at'], self.last_row['timestamp'])
        self.assertNotIn('Second turn launch',
                         [launch['description'] for launch in first_view['launches']])
        second_view = self.view(request_id='impl-2')
        self.assertEqual(second_view['turn']['started_at'], stamp)
        self.assertEqual(second_view['turn']['assistant_rows'], 1)
        self.assertEqual([launch['description'] for launch in second_view['launches']],
                         ['Second turn launch'])

    def test_suffix_alignment_maps_same_text_requests_to_the_visible_tail(self):
        state = self.state()
        first = state['requests']['impl-1']
        for name, order, at in (('impl-2', first['created_order'] + 1, first['created_at'] + 0.5),
                                ('impl-3', first['created_order'] + 2, first['created_at'] + 1.0)):
            state['requests'][name] = dict(first, request_id=name, created_order=order,
                                           created_at=at)
        ao.atomic(self.directory() / 'state.json', state)
        stamp = _iso(self.request['created_at'] + 4.5)
        tail = [
            {'type': 'user', 'uuid': 'impl-2-caller', 'sessionId': self.NATIVE, 'timestamp': stamp,
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('second-agent', _iso(self.request['created_at'] + 4.6),
                            [self._use('tu-sa', 'Agent', {'subagent_type': 'Explore',
                                                        'description': 'Second turn launch'})]),
            {'type': 'user', 'uuid': 'impl-3-caller', 'sessionId': self.NATIVE,
             'timestamp': _iso(self.request['created_at'] + 4.7),
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('third-agent', _iso(self.request['created_at'] + 4.8),
                            [self._use('tu-ta', 'Agent', {'subagent_type': 'Plan',
                                                        'description': 'Third turn launch'})]),
        ]
        # Three requests share the text but only the last two anchors remain visible in the tail.
        self.native_events = [row for row in self.native_events
                              if row.get('uuid') != self.anchor.get('uuid')]
        self.native_events[self.native_events.index(self.trailing):0] = tail
        self._write_transcript()
        second = self.view(request_id='impl-2')
        self.assertEqual(second['turn']['started_at'], stamp)
        self.assertEqual([launch['description'] for launch in second['launches']],
                         ['Second turn launch'])
        third = self.view(request_id='impl-3')
        self.assertEqual(third['turn']['started_at'], _iso(self.request['created_at'] + 4.7))
        self.assertEqual([launch['description'] for launch in third['launches']],
                         ['Third turn launch'])
        oldest = self.view(request_id='impl-1')
        self.assertEqual(oldest['turn'], {'anchor_found': False})
        self.assertEqual(oldest['launches'], [])

    def test_redact_keeps_urls_and_relative_paths(self):
        self.assertEqual(ao_progress._redact('Inspect https://example.com/issue/42'),
                         'Inspect https://example.com/issue/42')
        self.assertEqual(ao_progress._redact('Fix /Users/x/secret.json and src/a.ts'),
                         'Fix <path> and src/a.ts')
        self.assertEqual(ao_progress._redact('see (/tmp/x) "/etc/y"'), 'see (<path> "<path>"')
        self.assertEqual(ao_progress._redact('path/with/slashes'), 'path/with/slashes')

    def test_any_human_row_ends_the_turn_window(self):
        stamp = _iso(self.request['created_at'] + 4.0)
        rows = [
            {'type': 'user', 'uuid': _identity(), 'sessionId': self.NATIVE, 'timestamp': stamp,
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': 'an unregistered human note'}},
            self._assistant('late-agent', _iso(self.request['created_at'] + 4.1),
                            [self._use('tu-la', 'Agent', {'subagent_type': 'Explore',
                                                        'description': 'must not appear'})]),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = rows
        self._write_transcript()
        view = self.view()
        self.assertEqual(view['turn']['last_activity_at'], self.last_row['timestamp'])
        self.assertNotIn('must not appear', [launch['description'] for launch in view['launches']])

    def test_an_empty_task_list_replaces_the_plan(self):
        stamp = _iso(self.request['created_at'] + 3.0)
        rows = [
            self._assistant('list-empty', stamp, [self._use('tu-le', 'TaskList', {})]),
            self._result('list-empty-result', stamp, 'tu-le', 'No tasks found'),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = rows
        self._write_transcript()
        plan = self.view()['plan']
        self.assertEqual(plan['steps'], [])
        self.assertEqual(plan['counts'], {'pending': 0, 'in_progress': 0, 'completed': 0})
        self.assertEqual(plan['source'], 'task_tools')
        self.assertEqual(plan['updated_at'], rows[1]['timestamp'])

    def test_engineered_strings_redact_absolute_paths(self):
        stamp = _iso(self.request['created_at'] + 3.0)
        rows = [
            self._assistant('path-create', stamp, [self._use('tu-pc', 'TaskCreate',
                            {'subject': 'Fix /Users/x/secret.json and src/a.ts'})]),
            self._result('path-create-result', stamp, 'tu-pc', 'Task #9 created successfully'),
            self._assistant('path-agent', stamp, [self._use('tu-pa', 'Agent',
                            {'subagent_type': 'Explore', 'description': 'Open /etc/hosts and src/b.ts'})]),
            self._assistant('path-text', stamp,
                            [{'type': 'text', 'text': 'Wrote /var/log/out.log then src/c.ts'}],
                            stop='end_turn'),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = rows
        self._write_transcript()
        view = self.view()
        self.assertIn({'label': 'Fix <path> and src/a.ts', 'status': 'pending'}, view['plan']['steps'])
        self.assertIn('Open <path> and src/b.ts',
                      [launch['description'] for launch in view['launches']])
        self.assertEqual(view['last_text'], 'Wrote <path> then src/c.ts')
        serialized = json.dumps(view)
        self.assertNotIn('/Users/x/secret.json', serialized)
        self.assertNotIn('/etc/hosts', serialized)
        self.assertNotIn('/var/log/out.log', serialized)

    def test_histogram_keys_are_bounded(self):
        long_name = 'T' * 70
        blocks = [self._use('tu-h-long', long_name, {})] + [
            self._use('tu-h%d' % index, 'Tool%02d' % index, {}) for index in range(39)]
        row = self._assistant('many-tools', _iso(self.request['created_at'] + 3.0), blocks)
        self.native_events.insert(self.native_events.index(self.trailing), row)
        self._write_transcript()
        tool_calls = self.view()['turn']['tool_calls']
        named = {key: count for key, count in tool_calls.items() if key != 'other'}
        self.assertEqual(len(named), 31)
        self.assertEqual(named[long_name[:64]], 1)
        self.assertEqual(tool_calls['other'], 14)
        self.assertLessEqual(len(tool_calls), 32)


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
