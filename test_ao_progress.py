"""Read-only progress view of one owned request: bounded transcript fold, one-time part and surface registration."""

import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import ao_native_outcome
import ao_progress
import ao_project_room as ao
import ao_workflow
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
        # The spec review carries its own text: identical delivery texts correlate by the
        # request's own time bounds, so a shared 'Perform the exact authorized purpose.'
        # caller would otherwise sit inside the next request's five-second start grace.
        self.service.ao_room_send(self.room, 'engineer', 'Review the supplied specification exactly.',
                                  'spec_review', purpose='spec_review')
        self.note_native_turn('spec_review')
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish('engineer', json.dumps({'interpretation': 'Implement the supplied pure behavior.',
                                                 'findings': [], 'decision': 'accept', 'spec_revision': 1,
                                                 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(self.room)
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

    def test_progress_part_is_superseded_by_v3_and_never_delivered_with_its_pinned_digest_preserved(self):
        # Re-pinned for progress_plan_v3 (see SuccessorDigestAndSupersessionTests in this module):
        # progress_plan_v1's frozen text and digest stay exactly pinned forever, but it is superseded
        # and is no longer delivered to any session, old or new -- progress_plan_v3 is sent instead.
        self.assertEqual(ao.digest(ao_progress.INSTRUCTION.encode()), ao_progress.INSTRUCTION_SHA256)
        carried = self.state()['requests']['spec_review']['carried']
        self.assertNotIn(ao_progress.PART, carried['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION, self.state()['requests']['spec_review']['text'])
        self.assertIn(ao_progress.PART_V3, carried['parts'])
        self.assertEqual(self.state()['requests']['impl-1']['carried']['parts'], [])

    def test_progress_plan_v2_part_is_superseded_by_v3_and_never_delivered_with_its_pinned_digest_preserved(self):
        self.assertEqual(ao.digest(ao_progress.INSTRUCTION_V2.encode()), ao_progress.INSTRUCTION_V2_SHA256)
        carried = self.state()['requests']['spec_review']['carried']
        self.assertNotIn(ao_progress.PART_V2, carried['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, self.state()['requests']['spec_review']['text'])
        self.assertNotIn(ao_progress.PART_V2, self.state()['requests']['impl-1']['carried']['parts'])

    def test_the_provider_rate_limit_reading_folds_into_the_view(self):
        session = self.state()['requests']['impl-1']['session_id']
        database = self.root / 'owner.db'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE conversation_provider_events '
                               '(session_id TEXT, method TEXT, received_at TEXT, payload_json TEXT)')
        payload = {'rateLimits': {'PrimaryUsedPercent': 91, 'SecondaryUsedPercent': -1,
                                  'PrimaryResetsInSeconds': 1200, 'SecondaryResetsInSeconds': -1,
                                  'PlanLabel': 'five hour', 'CodexCapacity': None}}
        with sqlite3.connect(database) as connection:
            connection.execute('INSERT INTO conversation_provider_events VALUES (?,?,?,?)',
                               (session, 'account.rateLimits',
                                '2026-10-07 23:49:24.123456 +0000 UTC', json.dumps(payload)))
        view = self.view()
        self.assertTrue(view['rate_limits']['available'])
        self.assertEqual(view['rate_limits']['source'], 'ao_provider_events')
        self.assertEqual(view['rate_limits']['window']['label'], 'five hour')
        self.assertEqual(view['rate_limits']['window']['used_percent'], 91)
        self.assertEqual(view['rate_limits']['observed_at'],
                         datetime(2026, 10, 7, 23, 49, 24, 123456, tzinfo=timezone.utc).isoformat())

    def test_trailing_timestampless_metadata_rows_keep_the_last_timestamped_activity(self):
        self.native_events.extend([
            {'type': 'system', 'subtype': 'metadata', 'uuid': _identity(),
             'sessionId': self.NATIVE, 'cwd': self.workspace},
            {'type': 'system', 'subtype': 'metadata', 'uuid': _identity(), 'timestamp': 'not-a-timestamp',
             'sessionId': self.NATIVE, 'cwd': self.workspace},
        ])
        self._write_transcript()
        turn = self.view()['turn']
        self.assertTrue(turn['anchor_found'])
        self.assertEqual(turn['last_activity_at'], self.last_row['timestamp'])
        self.assertIsNotNone(turn['elapsed_seconds'])

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
            'steps': [{'label': 'Implement parser', 'status': 'completed', 'stale_in_progress': False},
                      {'label': 'Gate evidence', 'status': 'pending', 'stale_in_progress': False}],
            'counts': {'pending': 1, 'in_progress': 0, 'completed': 1},
            'total': 2, 'shown': 2,
            'omitted': {'pending': 0, 'in_progress': 0, 'completed': 0},
            'groups': [{'key': 'other', 'counts': {'pending': 1, 'in_progress': 0, 'completed': 1},
                        'completed_of_total': '1/2', 'active': []}]})
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
        # The trailing echo repeats spec_review's text but was stamped after the next
        # request's creation bound, so the request keeps its own caller and turn rows.
        spec_caller = next(row for row in self.native_events
                           if row.get('uuid') == 'spec_review-caller')
        todo_result = next(row for row in self.native_events
                           if row.get('uuid', '').startswith('impl-1-todo-write-result'))
        self.assertEqual(turn_timestamps(view['turn']),
                         (spec_caller['timestamp'], todo_result['timestamp']))
        self.assertEqual(view['turn']['assistant_rows'], 2)
        self.assertEqual(view['turn']['tool_calls'], {'TodoWrite': 1})
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
                                        'counts': {'pending': 0, 'in_progress': 0, 'completed': 0},
                                        'total': 0, 'shown': 0,
                                        'omitted': {'pending': 0, 'in_progress': 0, 'completed': 0},
                                        'groups': []})
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
            'steps': [{'label': 'Implement parser', 'status': 'completed', 'stale_in_progress': False},
                      {'label': 'Gate evidence', 'status': 'pending', 'stale_in_progress': False}],
            'counts': {'pending': 1, 'in_progress': 0, 'completed': 1},
            'total': 2, 'shown': 2,
            'omitted': {'pending': 0, 'in_progress': 0, 'completed': 0},
            'groups': [{'key': 'other', 'counts': {'pending': 1, 'in_progress': 0, 'completed': 1},
                        'completed_of_total': '1/2', 'active': []}]})

    def test_identical_request_texts_anchor_within_their_time_bounds(self):
        state = self.state()
        first = state['requests']['impl-1']
        state['requests']['impl-2'] = dict(first, request_id='impl-2',
                                         created_order=first['created_order'] + 1,
                                         created_at=first['created_at'] + 10.0)
        ao.atomic(self.directory() / 'state.json', state)
        stamp = _iso(first['created_at'] + 10.5)
        second = [
            {'type': 'user', 'uuid': 'impl-2-caller', 'sessionId': self.NATIVE, 'timestamp': stamp,
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('second-agent', _iso(first['created_at'] + 10.6),
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

    def test_last_text_stays_inside_the_request_turn(self):
        state = self.state()
        first = state['requests']['impl-1']
        state['requests']['impl-2'] = dict(first, request_id='impl-2',
                                         created_order=first['created_order'] + 1,
                                         created_at=first['created_at'] + 10.0)
        ao.atomic(self.directory() / 'state.json', state)
        second = [
            {'type': 'user', 'uuid': 'impl-2-caller', 'sessionId': self.NATIVE,
             'timestamp': _iso(first['created_at'] + 10.5), 'cwd': self.workspace,
             'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('second-tools', _iso(first['created_at'] + 10.6),
                            [self._use('tu-st', 'TaskList', {})]),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = second
        self._write_transcript()
        # impl-2's turn has tool calls but no assistant text yet; the previous turn's
        # LONG_TEXT must not leak across the anchor boundary.
        view = self.view(request_id='impl-2')
        self.assertTrue(view['turn']['anchor_found'])
        self.assertEqual(view['turn']['tool_calls'], {'TaskList': 1})
        self.assertIsNone(view['last_text'])
        self.assertEqual(self.view(request_id='impl-1')['last_text'], self.LONG_TEXT[:400])

    def test_an_undelivered_same_text_request_finds_no_anchor(self):
        state = self.state()
        first = state['requests']['impl-1']
        state['requests']['impl-2'] = dict(first, request_id='impl-2',
                                         created_order=first['created_order'] + 1,
                                         created_at=first['created_at'] + 10.0)
        state['requests']['impl-3'] = dict(first, request_id='impl-3',
                                         created_order=first['created_order'] + 2,
                                         created_at=first['created_at'] + 20.0)
        ao.atomic(self.directory() / 'state.json', state)
        # impl-2 was saved but never delivered; impl-3's own row lies inside its bounds only.
        stamp = _iso(first['created_at'] + 20.5)
        tail = [
            {'type': 'user', 'uuid': 'impl-3-caller', 'sessionId': self.NATIVE, 'timestamp': stamp,
             'cwd': self.workspace, 'isSidechain': False, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': self.request['text']}},
            self._assistant('third-agent', _iso(first['created_at'] + 20.6),
                            [self._use('tu-ta', 'Agent', {'subagent_type': 'Plan',
                                                        'description': 'Third turn launch'})]),
        ]
        self.native_events[self.native_events.index(self.trailing):0] = tail
        self._write_transcript()
        first_view = self.view(request_id='impl-1')
        self.assertEqual(first_view['turn']['started_at'], self.anchor['timestamp'])
        self.assertEqual([launch['description'] for launch in first_view['launches']][0],
                         'Survey the parser module')
        missing = self.view(request_id='impl-2')
        self.assertEqual(missing['turn'], {'anchor_found': False})
        self.assertEqual(missing['launches'], [])
        third = self.view(request_id='impl-3')
        self.assertEqual(third['turn']['started_at'], stamp)
        self.assertEqual([launch['description'] for launch in third['launches']],
                         ['Third turn launch'])

    def test_a_matching_row_before_the_start_grace_is_no_anchor(self):
        state = self.state()
        request = state['requests']['impl-1']
        # A recorded creation that drifted later than the real delivery leaves the only
        # matching row before start - 5: no anchor rather than a borrowed one.
        request['created_at'] = request['created_at'] + 60.0
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view(request_id='impl-1')
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['launches'], [])

    def test_observed_turn_started_at_overrides_a_stale_created_at(self):
        state = self.state()
        request = state['requests']['impl-1']
        request['created_at'] = request['created_at'] + 60.0
        anchor_epoch = ao_native_outcome.timestamp(self.anchor['timestamp'])
        request['observed_turn'] = {'startedAt': _iso(anchor_epoch - 1.0)}
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view(request_id='impl-1')
        self.assertEqual(view['turn']['started_at'], self.anchor['timestamp'])
        self.assertEqual(view['turn']['last_activity_at'], self.last_row['timestamp'])

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
        self.assertIn({'label': 'Fix <path> and src/a.ts', 'status': 'pending',
                       'stale_in_progress': False}, view['plan']['steps'])
        self.assertIn('Open <path> and src/b.ts',
                      [launch['description'] for launch in view['launches']])
        self.assertEqual(view['last_text'], 'Wrote <path> then src/c.ts')
        serialized = json.dumps(view)
        self.assertNotIn('/Users/x/secret.json', serialized)
        self.assertNotIn('/etc/hosts', serialized)
        self.assertNotIn('/var/log/out.log', serialized)

    def _task_rows(self, stamp, task_id, subject, status=None, active=None):
        """A TaskCreate pair plus an optional TaskUpdate pair, like the fixture's own rows."""
        use = 'tu-t' + task_id
        rows = [self._assistant('create-' + task_id, stamp,
                                [self._use(use, 'TaskCreate',
                                           {'subject': subject, 'activeForm': active})]),
                self._result('create-' + task_id + '-result', stamp, use,
                             'Task #' + task_id + ' created successfully')]
        if status is not None:
            given = {'taskId': task_id, 'status': status}
            if active is not None:
                given['activeForm'] = active
            rows += [self._assistant('update-' + task_id, stamp,
                                     [self._use(use + 'u', 'TaskUpdate', given)]),
                     self._result('update-' + task_id + '-result', stamp, use + 'u',
                                  'Task #' + task_id + ' updated')]
        return rows

    def test_group_key_codes_and_rounds(self):
        cases = [('EXIT-12: shipped', 'EXIT'), ('BARS-1c: open unit', 'BARS'),
                 ('UI-PROOF-2 remains', 'UI-PROOF'), ('FINAL-REVIEW-2: verdict', 'FINAL-REVIEW'),
                 ('P3 remaining', 'P3'), ('CLAMP-9', 'CLAMP'), ('123: digits', '123'),
                 ('implement the parser', 'other'), ('lower-1: no code', 'other'),
                 ('  spaced lead', 'other'), ('', 'other'), (None, 'other')]
        for subject, key in cases:
            with self.subTest(subject=subject):
                self.assertEqual(ao_progress._group_key(subject), key)

    def test_the_bounded_plan_counts_everything_and_prefers_active_and_newest(self):
        base = self.request['created_at'] + 1.0
        tick = [0]

        def at():
            tick[0] += 1
            return _iso(base + tick[0] / 1000)

        rows = []
        for index in range(26):  # tasks 100-125 completed
            task_id = str(100 + index)
            rows += self._task_rows(at(), task_id, 'EXIT-' + task_id + ': shipped unit', 'completed')
        for index in range(3):   # tasks 126-128 in progress
            task_id = str(126 + index)
            rows += self._task_rows(at(), task_id, 'BARS-1' + 'abc'[index] + ': open unit',
                                    'in_progress', active='Running BARS unit ' + task_id)
        for index in range(39):  # tasks 129-167 pending
            task_id = str(129 + index)
            rows += self._task_rows(at(), task_id, 'P3 pending unit ' + task_id)
        self.native_events.extend(rows)
        self._write_transcript()
        # 70 tasks with the fixture's own two: 40 pending, 3 in progress, 27 completed.
        plan = self.view()['plan']
        self.assertEqual(plan['total'], 70)
        self.assertEqual(plan['shown'], 64)
        self.assertEqual(plan['counts'], {'pending': 40, 'in_progress': 3, 'completed': 27})
        self.assertEqual(plan['omitted'], {'pending': 0, 'in_progress': 0, 'completed': 6})
        steps = plan['steps']
        self.assertEqual([step['status'] for step in steps[:3]], ['in_progress'] * 3)
        self.assertEqual([step['label'] for step in steps[:3]],
                         ['Running BARS unit 126', 'Running BARS unit 127', 'Running BARS unit 128'])
        self.assertTrue(all(step['stale_in_progress'] is False for step in steps))
        pending_labels = [step['label'] for step in steps[3:43]]
        self.assertEqual(pending_labels[:2], ['P3 pending unit 167', 'P3 pending unit 166'])
        self.assertEqual(pending_labels[-1], 'Gate evidence')  # the oldest pending still makes it
        completed = steps[43:]
        self.assertEqual(len(completed), 21)
        self.assertTrue(all(step['status'] == 'completed' for step in completed))
        self.assertEqual(completed[0]['label'], 'EXIT-125: shipped unit')
        self.assertEqual(completed[-1]['label'], 'EXIT-105: shipped unit')
        self.assertNotIn('Implement parser', [step['label'] for step in steps])
        self.assertEqual([group['key'] for group in plan['groups']], ['BARS', 'EXIT', 'P3', 'other'])
        groups = {group['key']: group for group in plan['groups']}
        self.assertEqual(groups['EXIT']['counts'], {'pending': 0, 'in_progress': 0, 'completed': 26})
        self.assertEqual(groups['EXIT']['completed_of_total'], '26/26')
        self.assertEqual(groups['BARS']['completed_of_total'], '0/3')
        self.assertEqual(groups['BARS']['active'],
                         ['Running BARS unit 126', 'Running BARS unit 127', 'Running BARS unit 128'])
        self.assertEqual(groups['P3']['completed_of_total'], '0/39')
        self.assertEqual(groups['other']['completed_of_total'], '1/2')
        self.assertEqual(sum(group['counts'][status] for group in plan['groups']
                             for status in ('pending', 'in_progress', 'completed')), 70)

    def test_a_task_list_sourced_plan_counts_all_of_the_list(self):
        rows = [
            self._assistant('list-65', _iso(self.request['created_at'] + 4.0),
                            [self._use('tu-l65', 'TaskList', {})]),
            self._result('list-65-result', _iso(self.request['created_at'] + 4.1), 'tu-l65',
                         '\n'.join('#%d [pending] Task %d' % (index, index) for index in range(1, 66))),
        ]
        self.native_events.extend(rows)
        self._write_transcript()
        plan = self.view()['plan']
        self.assertEqual(plan['total'], 65)
        self.assertEqual(plan['shown'], 64)
        self.assertEqual(plan['counts'], {'pending': 65, 'in_progress': 0, 'completed': 0})
        self.assertEqual(plan['omitted'], {'pending': 1, 'in_progress': 0, 'completed': 0})

    def test_stale_in_progress_marks_only_unmatched_old_updates(self):
        base = self.request['created_at'] + 1.0
        old, recent = _iso(base - 3700), _iso(base + 2.0)
        rows = (self._task_rows(old, '200', 'EXIT-7: stuck unit', 'in_progress', 'Running the stuck unit')
                + self._task_rows(old, '201', 'BARS-9: drifting unit', 'in_progress',
                                  'Running the drifting unit')
                + self._task_rows(old, '202', 'stray plain unit', 'in_progress', 'Working the stray unit')
                + self._task_rows(recent, '203', 'EXIT-8: fresh unit', 'in_progress',
                                  'Running the fresh unit'))
        # A launch inside the turn covers BARS-9; every other coded unit stays unmatched.
        index = self.native_events.index(self.trailing)
        agent = self._assistant('agent-bars', _iso(base + 0.5),
                                [self._use('tu-ab', 'Agent', {'description': 'BARS-9: investigating'})])
        agentr = self._result('agent-bars-r', _iso(base + 0.6), 'tu-ab', 'done')
        self.native_events[index:index] = [agent, agentr]
        self.native_events.extend(rows)
        self._write_transcript()
        steps = {step['label']: step['stale_in_progress'] for step in self.view()['plan']['steps']}
        self.assertIs(steps['Running the stuck unit'], True)
        self.assertIs(steps['Running the drifting unit'], False)
        self.assertIs(steps['Working the stray unit'], False)
        self.assertIs(steps['Running the fresh unit'], False)

    def test_stale_in_progress_needs_a_found_anchor_even_with_zero_launches(self):
        spec_request = self.state()['requests']['spec_review']
        old = _iso(spec_request['created_at'] + 1.0 - 4000)
        self.native_events.extend(self._task_rows(old, '500', 'EXIT-7: stuck unit', 'in_progress',
                                                  'Running the stuck unit'))
        self._write_transcript()
        # The spec turn anchored and launched nothing: an empty launch list is known
        # launch data, not missing launch data.
        view = self.view(request_id='spec_review')
        self.assertTrue(view['turn']['anchor_found'])
        self.assertEqual(view['launches'], [])
        steps = {step['label']: step['stale_in_progress'] for step in view['plan']['steps']}
        self.assertIs(steps['Running the stuck unit'], True)
        # Without an anchor the same rows cannot flag anything.
        state = self.state()
        state['requests']['spec_review']['text_sha256'] = '0' * 64
        ao.atomic(self.directory() / 'state.json', state)
        view = self.view(request_id='spec_review')
        self.assertFalse(view['turn']['anchor_found'])
        steps = {step['label']: step['stale_in_progress'] for step in view['plan']['steps']}
        self.assertIs(steps['Running the stuck unit'], False)

    def test_task_list_reads_keep_update_stamps_and_unknown_is_never_stale(self):
        base = self.request['created_at'] + 1.0
        old, listed = _iso(base - 4000), _iso(base + 3.0)
        rows = self._task_rows(old, '300', 'EXIT-1: long unit', 'in_progress', 'Running EXIT-1')
        rows += self._task_rows(old, '302', 'EXIT-3: flipped unit')
        rows += [
            self._assistant('list-after', listed, [self._use('tu-la', 'TaskList', {})]),
            self._result('list-after-result', listed, 'tu-la',
                         '#300 [in_progress] EXIT-1: long unit\n'
                         '#301 [in_progress] EXIT-2: first seen\n'
                         '#302 [in_progress] EXIT-3: flipped unit'),
        ]
        self.native_events.extend(rows)
        self._write_transcript()
        steps = {step['label']: step['stale_in_progress'] for step in self.view()['plan']['steps']}
        # EXIT-1's unchanged status carries its old stamp forward: still stale.
        self.assertIs(steps['EXIT-1: long unit'], True)
        # EXIT-2 first appears in the list read: no stamp means never stale.
        self.assertIs(steps['EXIT-2: first seen'], False)
        # EXIT-3's status changed in the read, so the list time is its stamp: recent.
        self.assertIs(steps['EXIT-3: flipped unit'], False)

    def test_long_codes_group_and_match_on_full_keys(self):
        # Grouping and launch matching keep the full code; only the displayed key is bounded.
        self.assertEqual(ao_progress._group_key('T' * 5000 + ': unit'), 'T' * 5000)
        self.assertEqual(ao_progress._leading_code('T' * 5000 + ': unit'), 'T' * 5000)
        base = self.request['created_at'] + 1.0
        stamp, old = _iso(base + 3.0), _iso(base - 4000)
        rows = (self._task_rows(stamp, '400', 'T' * 64 + 'AAA: first', 'completed')
                + self._task_rows(stamp, '401', 'T' * 64 + 'BBB: second', 'completed')
                + self._task_rows(old, '402', 'A' * 64 + '-TWO: unit', 'in_progress', 'Running A-TWO'))
        # A launch for a different long code does not cover the in-progress unit.
        index = self.native_events.index(self.trailing)
        agent = self._assistant('agent-one', _iso(base + 0.5),
                                [self._use('tu-one', 'Agent',
                                           {'description': 'A' * 64 + '-ONE: launched work'})])
        agentr = self._result('agent-one-r', _iso(base + 0.6), 'tu-one', 'done')
        self.native_events[index:index] = [agent, agentr]
        self.native_events.extend(rows)
        self._write_transcript()
        plan = self.view()['plan']
        keys = [group['key'] for group in plan['groups']]
        self.assertTrue(all(len(key) <= 64 for key in keys))
        self.assertEqual(len(keys), len(set(keys)))
        # 'T'*64+'AAA' and 'T'*64+'BBB' share the first 64 chars but are different codes:
        # two groups whose displayed keys differ only in the digest suffix.
        long_keys = sorted(key for key in keys if key.startswith('T' * 52 + '…'))
        self.assertEqual(len(long_keys), 2)
        steps = {step['label']: step['stale_in_progress'] for step in plan['steps']}
        self.assertIs(steps['Running A-TWO'], True)

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
            'steps': [{'label': 'Reading the evidence', 'status': 'in_progress',
                       'stale_in_progress': False},
                      {'label': 'Write the report', 'status': 'pending', 'stale_in_progress': False}],
            'counts': {'pending': 1, 'in_progress': 1, 'completed': 0},
            'total': 2, 'shown': 2,
            'omitted': {'pending': 0, 'in_progress': 0, 'completed': 0},
            'groups': [{'key': 'other', 'counts': {'pending': 1, 'in_progress': 1, 'completed': 0},
                        'completed_of_total': '0/2', 'active': ['Reading the evidence']}]})


class SuccessorDigestAndSupersessionTests(unittest.TestCase):
    """progress_plan_v3: pinned digest, SUPERSEDED_PARTS, and frozen v1/v2 history stay valid forever."""

    # Independently copied pin: a change to either the frozen text or its digest is caught, not just one.
    INSTRUCTION_V3_SHA256 = "9e559c9b963f8a883a979f1f97e3936aba0d2df5c7f7b5c682634da3380b665b"

    def test_instruction_v3_digest_matches_its_pin_and_part_name(self):
        self.assertEqual(ao_progress.PART_V3, "progress_plan_v3")
        self.assertEqual(hashlib.sha256(ao_progress.INSTRUCTION_V3.encode()).hexdigest(), self.INSTRUCTION_V3_SHA256)
        self.assertEqual(ao_progress.INSTRUCTION_V3_SHA256, self.INSTRUCTION_V3_SHA256)
        self.assertEqual(ao.digest(ao_progress.INSTRUCTION_V3.encode()), ao_progress.INSTRUCTION_V3_SHA256)
        self.assertTrue(ao_progress.INSTRUCTION_V3.startswith("Progress plan v3"))
        self.assertTrue(ao_progress.INSTRUCTION_V3.endswith("delegate evidence or review."))
        self.assertNotIn("\n", ao_progress.INSTRUCTION_V3)

    def test_superseded_parts_names_exactly_v1_and_v2(self):
        self.assertEqual(ao_progress.SUPERSEDED_PARTS, (ao_progress.PART, ao_progress.PART_V2))

    def test_v3_is_appended_last_and_v1_v2_keep_their_historical_positions_in_parts(self):
        # ao_workflow.PARTS' exact order/length is independently pinned in test_lifecycle_closure.py;
        # this only checks the three facts this unit's own correctness depends on.
        self.assertEqual(ao_workflow.PARTS[-1], ao_progress.PART_V3)
        self.assertEqual(ao_workflow.PARTS.index(ao_progress.PART), 10)
        self.assertEqual(ao_workflow.PARTS.index(ao_progress.PART_V2), 12)
        self.assertEqual(len(ao_workflow.PARTS), len(set(ao_workflow.PARTS)))

    def test_historical_v1_v2_carried_record_still_validates_through_carried_by(self):
        # A completed request recorded before this unit existed, naming only progress_plan_v1/v2 in its
        # carried record: frozen history stays valid forever, even though no future packet resends it.
        request = {'carried': {'parts': [ao_progress.PART, ao_progress.PART_V2],
                                'part_sha256': {ao_progress.PART: ao_progress.INSTRUCTION_SHA256,
                                                ao_progress.PART_V2: ao_progress.INSTRUCTION_V2_SHA256},
                                'spec_record_sha256': None, 'spec_delivery': None}}
        record, names = ao_workflow.carried_by(request)
        self.assertIsNone(record)
        self.assertEqual(set(names), {ao_progress.PART, ao_progress.PART_V2})


class NewSessionSuccessorDeliveryTests(Fixture):
    """A brand-new session receives progress_plan_v3 only; v1/v2 text is never sent to it."""

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def test_new_session_receives_only_the_successor_part(self):
        request = self.state()['requests']['spec_review']
        carried = request['carried']
        self.assertIn(ao_progress.PART_V3, carried['parts'])
        self.assertEqual(carried['part_sha256'][ao_progress.PART_V3], ao_progress.INSTRUCTION_V3_SHA256)
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)
        # Superseded parts are never sent to a brand-new session either -- not merely "already delivered".
        self.assertNotIn(ao_progress.PART, carried['parts'])
        self.assertNotIn(ao_progress.PART_V2, carried['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, request['text'])
        # Already delivered with the first packet; a later packet in the same session omits it.
        self.assertNotIn(ao_progress.PART_V3, self.state()['requests']['impl-1']['carried']['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION_V3, self.state()['requests']['impl-1']['text'])

    def test_context_summary_reports_the_successor_as_verified_delivered(self):
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertEqual(summary['progress_plan']['successor'], ao_progress.PART_V3)
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'verified_delivered')
        self.assertEqual(summary['progress_plan']['delivered_history'], 'receipt_verified')
        self.assertIsInstance(summary['progress_plan']['meaning'], str)
        for part in (ao_progress.PART, ao_progress.PART_V2):
            # Never actually delivered to this session (superseded before its first packet), yet
            # truthfully satisfied because the receipt-verified successor now covers it.
            self.assertFalse(summary['progress_plan']['superseded'][part]['delivered'])
            self.assertTrue(summary['progress_plan']['superseded'][part]['satisfied'])
        self.assertNotIn(ao_progress.PART, summary['undelivered_parts'])
        self.assertNotIn(ao_progress.PART_V2, summary['undelivered_parts'])
        self.assertNotIn(ao_progress.PART_V3, summary['undelivered_parts'])

    def test_context_summary_without_a_directory_is_metadata_only_and_never_satisfied(self):
        # Fail-closed by design: with no directory to verify receipts against, the successor's
        # delivery is "unverified" (not "verified_delivered") and satisfied is always False.
        summary = ao_workflow.context_summary(self.state(), directory=None)
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'unverified')
        # No directory at all means no authenticated chain to read it from, so the delivered/satisfied
        # fields above are metadata-only, even though this same session's successor is in fact verified
        # (see test_context_summary_reports_the_successor_as_verified_delivered with a directory).
        self.assertEqual(summary['progress_plan']['delivered_history'], 'metadata_only')
        for part in (ao_progress.PART, ao_progress.PART_V2):
            self.assertFalse(summary['progress_plan']['superseded'][part]['satisfied'])

    def test_context_summary_without_a_directory_is_unverified_even_when_v3_is_absent_from_metadata(self):
        # Regression for a fixed code defect: with no directory, successor_delivery must stay "unverified"
        # even when the metadata-only parts genuinely lack progress_plan_v3 -- never "undelivered", which
        # would misreport an unauthenticated read as a verified absence.
        state = copy.deepcopy(self.state())
        request = state['requests']['spec_review']
        request['carried']['parts'].remove(ao_progress.PART_V3)
        request['carried']['part_sha256'].pop(ao_progress.PART_V3)
        held = ao_workflow.delivered(state, state['bindings']['engineer']['session_id'])
        self.assertNotIn(ao_progress.PART_V3, held['parts'])
        summary = ao_workflow.context_summary(state, directory=None)
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'unverified')
        self.assertEqual(summary['progress_plan']['delivered_history'], 'metadata_only')
        for part in (ao_progress.PART, ao_progress.PART_V2):
            self.assertFalse(summary['progress_plan']['superseded'][part]['satisfied'])

    def test_ao_room_status_engineer_context_exposes_the_same_accounting(self):
        status = self.service.ao_room_status(self.room)
        progress_plan = status['engineer_context']['progress_plan']
        self.assertEqual(progress_plan['successor'], ao_progress.PART_V3)
        self.assertEqual(progress_plan['successor_delivery'], 'verified_delivered')
        self.assertEqual(progress_plan['delivered_history'], 'receipt_verified')
        self.assertTrue(progress_plan['superseded'][ao_progress.PART]['satisfied'])
        self.assertTrue(progress_plan['superseded'][ao_progress.PART_V2]['satisfied'])


class RetainedV1OnlySuccessorDeliveryTests(Fixture):
    """A session retained from when only progress_plan_v1 existed (not yet v2) gets v3 once, never v1/v2
    text again -- even though this exact session genuinely never received progress_plan_v2."""

    # Real historical prefix of PARTS ending at progress_plan_v1, before read_admission_v1/progress_plan_v2
    # existed. Relies on test_lifecycle_closure.py's own pin of PARTS' order and length.
    V1_ONLY_PARTS = ao_workflow.PARTS[:11]

    def setUp(self):
        super().setUp()
        # Supersession is also disabled while building this simulated-historical first packet: the
        # real (current) ao_progress.SUPERSEDED_PARTS would otherwise skip progress_plan_v1 too, since
        # packet()'s skip is unconditional and does not look at the patched PARTS tuple's own content.
        with patch.object(ao_workflow, 'PARTS', self.V1_ONLY_PARTS), patch.object(ao_progress, 'SUPERSEDED_PARTS', ()):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def test_correction_carries_the_successor_once_never_v1_or_v2_text(self):
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertIn(ao_progress.PART, held_before['parts'])        # genuinely delivered under V1_ONLY_PARTS
        self.assertNotIn(ao_progress.PART_V2, held_before['parts'])  # did not exist yet at that point
        self.assertNotIn(ao_progress.PART_V3, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'].count(ao_progress.PART_V3), 1)
        self.assertNotIn(ao_progress.PART, request['carried']['parts'])     # already delivered, not resent
        self.assertNotIn(ao_progress.PART_V2, request['carried']['parts'])  # superseded, never backfilled
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, request['text'])
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)


class RetainedV2OnlySuccessorDeliveryTests(Fixture):
    """A synthetic retained session that has progress_plan_v2 recorded but never progress_plan_v1: proves
    the supersession skip is unconditional per-part, never "whichever superseded part came first"."""

    # Synthetic, not a real historical PARTS value -- constructed only to prove the point above.
    V2_ONLY_PARTS = ao_workflow.PARTS[:10] + (ao_progress.PART_V2,)

    def setUp(self):
        super().setUp()
        # Supersession is also disabled while building this simulated first packet; see the matching
        # comment in RetainedV1OnlySuccessorDeliveryTests.setUp for why both patches are needed together.
        with patch.object(ao_workflow, 'PARTS', self.V2_ONLY_PARTS), patch.object(ao_progress, 'SUPERSEDED_PARTS', ()):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def test_progress_plan_v1_is_never_backfilled_even_though_genuinely_undelivered(self):
        held = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                     self.directory())
        self.assertNotIn(ao_progress.PART, held['parts'])  # genuinely never delivered to this session
        self.assertIn(ao_progress.PART_V2, held['parts'])  # genuinely delivered

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertNotIn(ao_progress.PART, request['carried']['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertEqual(request['carried']['parts'].count(ao_progress.PART_V3), 1)

        # undelivered_parts never lists a superseded part, even one this exact session truly lacks.
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_progress.PART, summary['undelivered_parts'])
        self.assertFalse(summary['progress_plan']['superseded'][ao_progress.PART]['delivered'])
        self.assertTrue(summary['progress_plan']['superseded'][ao_progress.PART]['satisfied'])


class RetainedPreV1SuccessorDeliveryTests(Fixture):
    """A retained session whose first completed turn predates progress_plan_v1 itself: neither superseded
    part was ever delivered to it, yet the successor still delivers once and both are reported satisfied."""

    # Real historical prefix of PARTS ending before progress_plan_v1 ever existed.
    PRE_V1_PARTS = ao_workflow.PARTS[:10]

    def setUp(self):
        super().setUp()
        # This historical prefix predates progress_plan_v1 itself, so no superseded part can be
        # carried and no SUPERSEDED_PARTS patch is needed.
        with patch.object(ao_workflow, 'PARTS', self.PRE_V1_PARTS):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def test_successor_delivers_once_and_both_superseded_parts_report_satisfied_though_never_delivered(self):
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertNotIn(ao_progress.PART, held_before['parts'])     # genuinely never existed for this session
        self.assertNotIn(ao_progress.PART_V2, held_before['parts'])  # genuinely never existed for this session
        self.assertNotIn(ao_progress.PART_V3, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        # This historical prefix also predates read_admission_v1/residual_escalation_v1/lifecycle_closure_v1,
        # so the correction legitimately carries those too; the point here is only that progress_plan_v3
        # is carried exactly once and neither superseded name is ever carried.
        self.assertEqual(request['carried']['parts'].count(ao_progress.PART_V3), 1)
        self.assertNotIn(ao_progress.PART, request['carried']['parts'])
        self.assertNotIn(ao_progress.PART_V2, request['carried']['parts'])
        self.assertNotIn(ao_progress.INSTRUCTION, request['text'])
        self.assertNotIn(ao_progress.INSTRUCTION_V2, request['text'])
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)

        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'verified_delivered')
        self.assertEqual(summary['progress_plan']['delivered_history'], 'receipt_verified')
        for part in (ao_progress.PART, ao_progress.PART_V2):
            self.assertFalse(summary['progress_plan']['superseded'][part]['delivered'])
            self.assertTrue(summary['progress_plan']['superseded'][part]['satisfied'])

        # ao_room_status exposes the identical accounting through engineer_context.
        status = self.service.ao_room_status(self.room)
        progress_plan = status['engineer_context']['progress_plan']
        self.assertEqual(progress_plan['successor_delivery'], 'verified_delivered')
        self.assertEqual(progress_plan['delivered_history'], 'receipt_verified')
        self.assertTrue(progress_plan['superseded'][ao_progress.PART]['satisfied'])
        self.assertTrue(progress_plan['superseded'][ao_progress.PART_V2]['satisfied'])


class UpgradingSessionSuccessorDeliveryTests(Fixture):
    """A session retained with both v1 and v2 already delivered, fully caught up through
    lifecycle_closure_v1, gets only the new successor once, then nothing further (already-upgraded)."""

    # The real 15-name PARTS tuple immediately before this unit (everything except progress_plan_v3).
    PARTS_BEFORE_V3 = ao_workflow.PARTS[:15]

    def setUp(self):
        super().setUp()
        # Supersession is also disabled while building this simulated first packet; see the matching
        # comment in RetainedV1OnlySuccessorDeliveryTests.setUp for why both patches are needed together.
        with patch.object(ao_workflow, 'PARTS', self.PARTS_BEFORE_V3), patch.object(ao_progress, 'SUPERSEDED_PARTS', ()):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def packet(self):
        return ao_workflow.packet(self.service, self.directory(), self.state(), 'engineer',
                                  'correction', 'Continue.')

    def test_successor_delivers_once_then_an_already_upgraded_session_carries_nothing(self):
        held_before = ao_workflow.delivered(self.state(), self.state()['bindings']['engineer']['session_id'],
                                            self.directory())
        self.assertIn(ao_progress.PART, held_before['parts'])
        self.assertIn(ao_progress.PART_V2, held_before['parts'])
        self.assertNotIn(ao_progress.PART_V3, held_before['parts'])

        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'], [ao_progress.PART_V3])
        self.assertEqual(request['carried']['part_sha256'], {ao_progress.PART_V3: ao_progress.INSTRUCTION_V3_SHA256})
        self.assertEqual(request['text'].count(ao_progress.INSTRUCTION_V3), 1)

        # Already upgraded: a further completed request leaves nothing left to send.
        text2, carried2 = self.packet()
        self.assertEqual(carried2['parts'], [])
        self.assertNotIn(ao_progress.INSTRUCTION_V3, text2)


class UncertainSuccessorDeliveryTests(Fixture):
    """A lost native acknowledgement leaves the successor carried-but-uncertain; the controller withholds
    credit (and 'satisfied') until a verified completion, matching the lifecycle_closure_v1 pattern."""

    PARTS_BEFORE_V3 = ao_workflow.PARTS[:15]

    def setUp(self):
        super().setUp()
        # Supersession is also disabled while building this simulated first packet; see the matching
        # comment in RetainedV1OnlySuccessorDeliveryTests.setUp for why both patches are needed together.
        with patch.object(ao_workflow, 'PARTS', self.PARTS_BEFORE_V3), patch.object(ao_progress, 'SUPERSEDED_PARTS', ()):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def test_uncertain_carried_successor_is_undelivered_and_unsatisfied_until_verified(self):
        session_id = self.state()['bindings']['engineer']['session_id']
        self.fake.lose_ack = True
        result = self.send('correction', 'corr-uncertain')
        self.assertEqual(result['state'], 'uncertain')
        request = self.state()['requests']['corr-uncertain']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('receipt', request)
        self.assertEqual(request['carried']['parts'], [ao_progress.PART_V3])

        held = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertNotIn(ao_progress.PART_V3, held['parts'])
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertIn(ao_progress.PART_V3, summary['undelivered_parts'])
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'undelivered')
        # A directory was given and its own receipt chain authenticated cleanly, so this "undelivered"
        # (unlike the no-directory case) is still receipt-verified history, just not yet satisfied.
        self.assertEqual(summary['progress_plan']['delivered_history'], 'receipt_verified')
        self.assertFalse(summary['progress_plan']['superseded'][ao_progress.PART]['satisfied'])
        self.assertFalse(summary['progress_plan']['superseded'][ao_progress.PART_V2]['satisfied'])
        # ao_room_status exposes the identical accounting through engineer_context.
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['engineer_context']['progress_plan']['successor_delivery'], 'undelivered')
        self.assertEqual(status['engineer_context']['progress_plan']['delivered_history'], 'receipt_verified')

        # The completed-request pattern: resolve the identical request (no replay) to a
        # completed, verified turn, then confirm the successor is delivered and satisfied.
        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['corr-uncertain']['state'], 'completed')

        held2 = ao_workflow.delivered(self.state(), session_id, self.directory())
        self.assertIn(ao_progress.PART_V3, held2['parts'])
        summary2 = ao_workflow.context_summary(self.state(), self.directory())
        self.assertNotIn(ao_progress.PART_V3, summary2['undelivered_parts'])
        self.assertEqual(summary2['progress_plan']['successor_delivery'], 'verified_delivered')
        self.assertEqual(summary2['progress_plan']['delivered_history'], 'receipt_verified')
        self.assertTrue(summary2['progress_plan']['superseded'][ao_progress.PART]['satisfied'])
        self.assertTrue(summary2['progress_plan']['superseded'][ao_progress.PART_V2]['satisfied'])
        status2 = self.service.ao_room_status(self.room)
        self.assertEqual(status2['engineer_context']['progress_plan']['successor_delivery'], 'verified_delivered')
        self.assertEqual(status2['engineer_context']['progress_plan']['delivered_history'], 'receipt_verified')


class TamperedSuccessorDeliveryTests(Fixture):
    """Fail-closed tampering: a shape-valid but receipt-inconsistent carried record never reports the
    successor as delivered or the superseded parts as satisfied -- and is reported through its own
    "unavailable_integrity" status, never "undelivered" (which would misread tampering as a verified
    absence)."""

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def test_tampered_carried_record_fails_closed_never_satisfied(self):
        state = self.state()
        session_id = state['bindings']['engineer']['session_id']
        summary = ao_workflow.context_summary(state, self.directory())
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'verified_delivered')
        self.assertEqual(summary['progress_plan']['delivered_history'], 'receipt_verified')
        self.assertTrue(summary['progress_plan']['superseded'][ao_progress.PART]['satisfied'])
        self.assertTrue(summary['progress_plan']['superseded'][ao_progress.PART_V2]['satisfied'])

        original = copy.deepcopy(state)
        req = state['requests']['spec_review']
        # Self-consistent (shape-valid) removal: carried_by()'s own shape check still passes, but this
        # no longer matches the immutable receipt's carried_sha256 recorded at dispatch time.
        req['carried']['parts'].remove(ao_progress.PART_V3)
        req['carried']['part_sha256'].pop(ao_progress.PART_V3)
        self.service.save(self.directory(), state)
        try:
            tampered = self.state()
            with self.assertRaises(ao.RoomError):
                ao_workflow.delivered(tampered, session_id, self.directory())
            summary2 = ao_workflow.context_summary(tampered, self.directory())
            # Design correction: a broken receipt chain is its own distinct status, never "undelivered".
            self.assertEqual(summary2['progress_plan']['successor_delivery'], 'unavailable_integrity')
            self.assertEqual(summary2['progress_plan']['delivered_history'], 'metadata_only')
            self.assertFalse(summary2['progress_plan']['superseded'][ao_progress.PART]['satisfied'])
            self.assertFalse(summary2['progress_plan']['superseded'][ao_progress.PART_V2]['satisfied'])
            # The read-only status projection never raises even though the evidence beneath it is broken.
            status = self.service.ao_room_status(self.room)
            self.assertEqual(status['engineer_context']['progress_plan']['successor_delivery'],
                             'unavailable_integrity')
            self.assertEqual(status['engineer_context']['progress_plan']['delivered_history'], 'metadata_only')
        finally:
            self.service.save(self.directory(), original)


class DamagedReceiptSuccessorDeliveryTests(Fixture):
    """Unrelated damage to a different completed request's own receipt (missing from disk, or
    unparsable bytes) must never be misread as a verified-absent successor: delivered() raises while
    scanning that unrelated request, so context_summary reports the distinct "unavailable_integrity"
    status. The superseded parts' literal delivered-from-metadata history stays truthful (v1/v2 really
    were delivered before progress_plan_v3 existed) and only their receipt-authenticated "satisfied"
    flag withdraws."""

    PARTS_BEFORE_V3 = ao_workflow.PARTS[:15]

    def setUp(self):
        super().setUp()
        # A retained session with v1/v2 genuinely delivered, like UpgradingSessionSuccessorDeliveryTests.
        with patch.object(ao_workflow, 'PARTS', self.PARTS_BEFORE_V3), patch.object(ao_progress, 'SUPERSEDED_PARTS', ()):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)
        # Deliver the successor through a correction, with the real (unpatched) PARTS/SUPERSEDED_PARTS;
        # impl-1's own receipt (above, already completed) carries no progress part and is unrelated to
        # this delivery.
        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertIn(ao_progress.PART_V3, self.state()['requests']['corr-1']['carried']['parts'])

    def _assert_unavailable_integrity(self):
        summary = ao_workflow.context_summary(self.state(), self.directory())
        self.assertEqual(summary['progress_plan']['successor_delivery'], 'unavailable_integrity')
        # The delivered/satisfied fields above fall back to metadata-only: this request's own carried
        # record is untouched, but the chain-wide authentication scan failed on unrelated evidence, so
        # nothing from this read can be called receipt-verified.
        self.assertEqual(summary['progress_plan']['delivered_history'], 'metadata_only')
        for part in (ao_progress.PART, ao_progress.PART_V2):
            self.assertTrue(summary['progress_plan']['superseded'][part]['delivered'])
            self.assertFalse(summary['progress_plan']['superseded'][part]['satisfied'])
        # The read-only status projection never raises even though the evidence beneath it is broken.
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['engineer_context']['progress_plan']['successor_delivery'],
                         'unavailable_integrity')
        self.assertEqual(status['engineer_context']['progress_plan']['delivered_history'], 'metadata_only')

    def test_missing_receipt_on_an_unrelated_request_reports_unavailable_integrity(self):
        path = self.directory() / self.state()['requests']['impl-1']['receipt']
        original = path.read_bytes()
        path.unlink()
        try:
            self._assert_unavailable_integrity()
        finally:
            path.write_bytes(original)

    def test_corrupt_receipt_on_an_unrelated_request_reports_unavailable_integrity(self):
        path = self.directory() / self.state()['requests']['impl-1']['receipt']
        original = path.read_bytes()
        path.write_text('{')
        try:
            self._assert_unavailable_integrity()
        finally:
            path.write_bytes(original)

    def test_receipt_replaced_by_different_valid_json_on_an_unrelated_request_reports_unavailable_integrity(self):
        # Distinct from the corrupt-bytes case above: this receipt is syntactically valid JSON (read()
        # parses it cleanly) with the same shape, but the rewritten receipt bytes no longer match the
        # request's recorded receipt_sha256 digest, so completed_receipt() refuses ("Native result
        # receipt was modified"), the authenticated delivered() call raises, and status reports
        # unavailable_integrity.
        path = self.directory() / self.state()['requests']['impl-1']['receipt']
        original = path.read_bytes()
        receipt = json.loads(original.decode('utf-8'))
        self.assertIn('carried_sha256', receipt)
        receipt['carried_sha256'] = hashlib.sha256(b'a different, syntactically valid receipt').hexdigest()
        path.write_text(json.dumps(receipt))
        try:
            self._assert_unavailable_integrity()
        finally:
            path.write_bytes(original)


class MalformedCarriedRecordSuccessorDeliveryTests(Fixture):
    """A completed engineer request's own saved carried record can be malformed independently of its
    receipt -- the PR reviewer's example: a duplicated name in carried.parts. carried_by() rejects this
    shape (len(set(names)) != len(names)) before any receipt is even considered, so the metadata-only
    delivered() call that context_summary's *first*, unauthenticated read makes raises RoomError. That
    early error return must still carry a full "unavailable_integrity"/"unavailable" progress_plan with
    the healthy branch's exact key set, never drop the key entirely."""

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def test_duplicated_carried_part_name_reports_unavailable_integrity_progress_plan(self):
        session_id = self.state()['bindings']['engineer']['session_id']
        healthy_keys = set(ao_workflow.context_summary(self.state(), self.directory())['progress_plan'].keys())

        state = self.state()
        original = copy.deepcopy(state)
        req = state['requests']['implementation']
        # Shape-invalid (unlike TamperedSuccessorDeliveryTests' shape-valid-but-receipt-inconsistent
        # removal): a duplicated name in carried.parts, left otherwise consistent with part_sha256, so
        # only carried_by()'s own duplicate check fires. The receipt is left untouched -- irrelevant here,
        # since the unauthenticated delivered(state, session_id) call below never reads it.
        req['carried']['parts'] = [ao_progress.PART_V3, ao_progress.PART_V3]
        req['carried']['part_sha256'] = {ao_progress.PART_V3: ao_progress.INSTRUCTION_V3_SHA256}
        self.service.save(self.directory(), state)
        try:
            tampered = self.state()
            # Positive control: the identical record with the name listed once is accepted, so the
            # duplicate-name check alone is what rejects the tampered record below.
            control = copy.deepcopy(tampered['requests']['implementation'])
            control['carried']['parts'] = [ao_progress.PART_V3]
            self.assertEqual(ao_workflow.carried_by(control)[1], (ao_progress.PART_V3,))
            with self.assertRaises(ao.RoomError):
                ao_workflow.carried_by(tampered['requests']['implementation'])
            with self.assertRaises(ao.RoomError):
                ao_workflow.delivered(tampered, session_id)

            # The read-only status projection never raises even though the evidence beneath it is broken.
            status = self.service.ao_room_status(self.room)
            context = status['engineer_context']
            self.assertIsInstance(context['error'], str)
            self.assertTrue(context['error'])
            plan = context['progress_plan']
            self.assertEqual(plan['successor'], ao_progress.PART_V3)
            self.assertEqual(plan['successor_delivery'], 'unavailable_integrity')
            self.assertEqual(plan['delivered_history'], 'unavailable')
            for part in (ao_progress.PART, ao_progress.PART_V2):
                self.assertIsNone(plan['superseded'][part]['delivered'])
                self.assertFalse(plan['superseded'][part]['satisfied'])
            self.assertEqual(set(plan.keys()), healthy_keys)

            # Both the receipt-authenticated and the unverified callers land on the identical plan.
            self.assertEqual(ao_workflow.context_summary(tampered, self.directory())['progress_plan'], plan)
            self.assertEqual(ao_workflow.context_summary(tampered, None)['progress_plan'], plan)
        finally:
            self.service.save(self.directory(), original)


if __name__ == '__main__':
    unittest.main()
