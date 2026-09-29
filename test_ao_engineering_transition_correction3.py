"""Correction-3 regression tests for the engineering-model transition core; no AO, model or network calls.

These tests exercise the narrow child-evidence parser and the exact full-state transition-head digest.
The promised real preparation/routing-refresh integration chain lives in
test_ao_engineering_transition_refresh.py, which inherits the supplied normal fixture instead of inheriting a
test-bearing class that duplicates unrelated cases. Nothing here claims to have been run.
"""

import copy
import json
import unittest

import ao_engineering_model as em
import ao_engineering_transition as et
from room import RoomError

NATIVE = 'native-1'
WORKSPACE = '/synthetic/worktree'


def line(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n'


def notification(task_id, tool_use_id='toolu_1', status='completed'):
    return ('<task-notification>\n<task-id>' + task_id + '</task-id>\n<tool-use-id>' + tool_use_id
            + '</tool-use-id>\n<output-file>/synthetic/output</output-file>\n<status>' + status
            + '</status>\n<summary>finished</summary>\n<note>terminal</note>\n</task-notification>')


def base_user():
    return {'uuid': 'h-1', 'type': 'user', 'sessionId': NATIVE, 'cwd': WORKSPACE,
            'message': {'role': 'user', 'content': 'Synthetic human instruction.'}}


def tool_use(tool_id='toolu_1', name='Agent', background=False, prompt='Do work'):
    inputs = {'subagent_type': 'pr-opus', 'prompt': prompt}
    if name == 'Agent':
        inputs['run_in_background'] = background
    return {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tool_id, 'name': name,
                                                           'input': inputs}]}}


def tool_result(tool_id='toolu_1', content='launched', is_error=None, structured=None):
    block = {'type': 'tool_result', 'tool_use_id': tool_id, 'content': content}
    if is_error is not None:
        block['is_error'] = is_error
    row = {'uuid': 'r-1', 'type': 'user', 'sessionId': NATIVE, 'cwd': WORKSPACE,
           'sourceToolAssistantUUID': 'a-1', 'message': {'role': 'user', 'content': [block]}}
    if structured is not None:
        row['toolUseResult'] = structured
    return row


def notification_row(text, row_uuid='n-1', tool_use_id='toolu_1'):
    return {'uuid': row_uuid, 'type': 'user', 'sessionId': NATIVE, 'cwd': WORKSPACE,
            'origin': {'kind': 'task-notification'}, 'promptSource': 'sdk', 'queueSkipAttachments': True,
            'userType': 'external', 'entrypoint': 'sdk-ts', 'isSidechain': False,
            'message': {'role': 'user', 'content': text}}


def raw(rows):
    return ''.join(line(row) for row in rows).encode()


class ChildEvidenceCorrection3Tests(unittest.TestCase):
    def test_unrelated_tool_search_reference_result_does_not_block_quiescence(self):
        rows = [base_user(), tool_use('toolu_search', name='ToolSearch'),
                tool_result('toolu_search', content=[{'type': 'tool_reference', 'tool_name': 'Agent'}])]
        result = et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        self.assertEqual(result['agent_launches'], 0)
        self.assertEqual(result['parent_tool_uses'], 1)

    def test_foreground_launch_error_without_structured_failure_refuses(self):
        rows = [base_user(), tool_use(), tool_result(is_error=True, content='boom')]
        with self.assertRaisesRegex(RoomError, 'no validated terminal result'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_foreground_launch_proven_failed_structured_result_is_terminal(self):
        structured = {'status': 'failed', 'agentId': 'agent-1', 'prompt': 'Do work'}
        rows = [base_user(), tool_use(), tool_result(is_error=True, content='boom', structured=structured)]
        result = et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        self.assertEqual(result['agent_launches'], 1)

    def test_background_notification_task_id_must_match_structured_identity(self):
        launch = tool_use(background=True)
        result_row = tool_result(structured={'status': 'completed', 'agentId': 'agent-1'})
        mismatch = [base_user(), launch, result_row, notification_row(notification('other-task'))]
        with self.assertRaisesRegex(RoomError, 'does not name the structured child agent identity'):
            et.child_evidence(raw(mismatch), NATIVE, WORKSPACE)
        matched = [base_user(), launch, result_row, notification_row(notification('agent-1'))]
        self.assertEqual(et.child_evidence(raw(matched), NATIVE, WORKSPACE)['agent_launches'], 1)

    def test_conflicting_duplicate_structured_identities_refuse(self):
        first = tool_result(structured={'status': 'completed', 'agentId': 'agent-1'})
        second = copy.deepcopy(first)
        second['uuid'] = 'r-2'
        second['toolUseResult'] = {'status': 'completed', 'agentId': 'agent-2'}
        rows = [base_user(), tool_use(), first, second]
        with self.assertRaisesRegex(RoomError, 'Conflicting duplicate native tool result lifecycle projections'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_foreground_start_acknowledgement_refuses(self):
        rows = [base_user(), tool_use(), tool_result(structured={'status': 'running', 'agentId': 'agent-1'})]
        with self.assertRaisesRegex(RoomError, 'start acknowledgement'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_explicit_task_identity_is_the_task_binding_for_a_background_launch(self):
        launch = tool_use(background=True)
        failed = tool_result(is_error=True,
                             structured={'status': 'failed', 'agentId': 'agent-1', 'taskId': 'task-9'})
        accepted = [base_user(), launch, failed, notification_row(notification('task-9'), row_uuid='n-1')]
        self.assertEqual(et.child_evidence(raw(accepted), NATIVE, WORKSPACE)['agent_launches'], 1)
        for task_id in ('agent-1', 'other-task'):
            with self.subTest(task_id=task_id):
                conflicting = [base_user(), launch, copy.deepcopy(failed),
                               notification_row(notification(task_id), row_uuid='n-2')]
                with self.assertRaisesRegex(RoomError, 'explicit task identity'):
                    et.child_evidence(raw(conflicting), NATIVE, WORKSPACE)

    def test_conflicting_task_identifier_fields_refuse(self):
        rows = [base_user(), tool_use(),
                tool_result(structured={'status': 'failed', 'agentId': 'agent-1', 'taskId': 'task-9',
                                        'task_id': 'task-10'})]
        with self.assertRaisesRegex(RoomError, 'conflicting task identities'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_malformed_explicit_task_identity_refuses_instead_of_falling_back(self):
        for key, value in (('taskId', ['task-other']), ('taskId', {'task': 'other'}),
                           ('taskId', ''), ('taskId', None), ('task_id', ['task-other']),
                           ('task_id', ''), ('task_id', None)):
            with self.subTest(key=key, value=value):
                structured = {'status': 'completed', 'agentId': 'agent-1', key: value}
                rows = [base_user(), tool_use(background=True), tool_result(structured=structured),
                        notification_row(notification('agent-1'))]
                with self.assertRaisesRegex(RoomError, 'malformed ' + key):
                    et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        rows = [base_user(), tool_use(background=True),
                tool_result(structured={'status': 'completed', 'agentId': ['agent-1']}),
                notification_row(notification('agent-1'))]
        with self.assertRaisesRegex(RoomError, 'malformed agent identity'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        rows = [base_user(), tool_use(background=True),
                tool_result(structured={'status': 'completed', 'agentId': 'agent-1'}),
                notification_row(notification('agent-1'))]
        self.assertEqual(et.child_evidence(raw(rows), NATIVE, WORKSPACE)['agent_launches'], 1)

    def test_shared_foreground_agent_identity_refuses_a_second_fresh_launch(self):
        launch = {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                       'input': {'prompt': 'One', 'run_in_background': False}},
                      {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent',
                       'input': {'prompt': 'Two', 'run_in_background': False}}]}}
        first = tool_result('toolu_1', content='one failed', is_error=True,
                            structured={'status': 'failed', 'agentId': 'agent-1', 'prompt': 'One'})
        second = tool_result('toolu_2', content='two failed', is_error=True,
                             structured={'status': 'failed', 'agentId': 'agent-1', 'prompt': 'Two'})
        second['uuid'] = 'r-2'
        with self.assertRaisesRegex(RoomError, 'structured child identity'):
            et.child_evidence(raw([base_user(), launch, first, second]), NATIVE, WORKSPACE)

    def test_shared_background_task_identity_refuses_a_second_fresh_launch(self):
        launch = {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                       'input': {'prompt': 'One', 'run_in_background': True}},
                      {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent',
                       'input': {'prompt': 'Two', 'run_in_background': True}}]}}
        first = tool_result('toolu_1', content='one failed', is_error=True,
                            structured={'status': 'failed', 'agentId': 'agent-1', 'taskId': 'task-9'})
        second = tool_result('toolu_2', content='two failed', is_error=True,
                             structured={'status': 'failed', 'agentId': 'agent-2', 'taskId': 'task-9'})
        second['uuid'] = 'r-2'
        rows = [base_user(), launch, first, second,
                notification_row(notification('task-9', 'toolu_1'), row_uuid='n-1'),
                notification_row(notification('task-9', 'toolu_2'), row_uuid='n-2')]
        with self.assertRaisesRegex(RoomError, 'structured child identity'):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_same_launch_repeated_identical_identity_stays_valid(self):
        first = tool_result(content='failed', is_error=True,
                            structured={'status': 'failed', 'agentId': 'agent-1', 'prompt': 'Do work'})
        second = copy.deepcopy(first)
        second['uuid'] = 'r-2'
        result = et.child_evidence(raw([base_user(), tool_use(), first, second]), NATIVE, WORKSPACE)
        self.assertEqual(result['agent_launches'], 1)

    def test_agent_identity_is_only_the_fallback_without_an_explicit_task_id(self):
        launch = tool_use(background=True)
        failed = tool_result(is_error=True, structured={'status': 'failed', 'agentId': 'agent-1'})
        rows = [base_user(), launch, failed, notification_row(notification('agent-1'), row_uuid='n-1')]
        self.assertEqual(et.child_evidence(raw(rows), NATIVE, WORKSPACE)['agent_launches'], 1)
        wrong = [base_user(), launch, copy.deepcopy(failed),
                 notification_row(notification('task-9'), row_uuid='n-2')]
        with self.assertRaises(RoomError):
            et.child_evidence(raw(wrong), NATIVE, WORKSPACE)

    def test_a_shared_row_level_projection_cannot_authorize_two_agent_results(self):
        launch = {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                       'input': {'prompt': 'One', 'run_in_background': False}},
                      {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent',
                       'input': {'prompt': 'Two', 'run_in_background': False}}]}}
        shared = {'uuid': 'r-1', 'type': 'user', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'sourceToolAssistantUUID': 'a-1',
                  'toolUseResult': {'status': 'failed', 'agentId': 'agent-1', 'prompt': 'One'},
                  'message': {'role': 'user', 'content': [
                      {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'one failed',
                       'is_error': True},
                      {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': 'two failed',
                       'is_error': True}]}}
        with self.assertRaisesRegex(RoomError, 'row-level structured tool result is shared'):
            et.child_evidence(raw([base_user(), launch, shared]), NATIVE, WORKSPACE)

    def test_distinct_owned_foreground_failures_with_their_own_rows_succeed(self):
        launch = {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                       'input': {'prompt': 'One', 'run_in_background': False}},
                      {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent',
                       'input': {'prompt': 'Two', 'run_in_background': False}}]}}
        first = tool_result('toolu_1', content='one failed', is_error=True,
                            structured={'status': 'failed', 'agentId': 'agent-1', 'prompt': 'One'})
        second = tool_result('toolu_2', content='two failed', is_error=True,
                             structured={'status': 'failed', 'agentId': 'agent-2', 'prompt': 'Two'})
        second['uuid'] = 'r-2'
        result = et.child_evidence(raw([base_user(), launch, first, second]), NATIVE, WORKSPACE)
        self.assertEqual(result['agent_launches'], 2)
        self.assertEqual(result['correlated_results'], 2)

    def test_duplicate_results_must_agree_in_full_lifecycle_not_text_alone(self):
        first = tool_result('toolu_1', content='same text', is_error=True,
                            structured={'status': 'failed', 'agentId': 'agent-1', 'prompt': 'Do work'})
        second = tool_result('toolu_1', content='same text', is_error=True,
                             structured={'status': 'failed', 'agentId': 'agent-2', 'prompt': 'Do work'})
        second['uuid'] = 'r-2'
        with self.assertRaisesRegex(RoomError, 'Conflicting duplicate native tool result lifecycle projections'):
            et.child_evidence(raw([base_user(), tool_use(), first, second]), NATIVE, WORKSPACE)

    API_ERROR_DETAIL = 'synthetic quota (error type rate_limit, HTTP 429)'
    API_ERROR_CONTENT = 'Agent terminated early due to an API error: ' + API_ERROR_DETAIL
    API_ERROR_RESULT = 'Error: ' + API_ERROR_CONTENT

    def test_exact_foreground_agent_api_error_is_a_distinct_terminal_proof(self):
        rows = [base_user(), tool_use(),
                tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT)]
        result = et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        self.assertEqual(result['agent_launches'], 1)
        self.assertEqual(result['terminal_api_errors'], 1)
        duplicate = tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT)
        duplicate['uuid'] = 'r-2'
        rows = [base_user(), tool_use(),
                tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT), duplicate]
        self.assertEqual(et.child_evidence(raw(rows), NATIVE, WORKSPACE)['terminal_api_errors'], 1)

    def test_unproven_api_error_text_or_inconsistent_projection_refuses(self):
        for content, structured in (
                (self.API_ERROR_RESULT, self.API_ERROR_RESULT),
                (self.API_ERROR_CONTENT, self.API_ERROR_CONTENT),
                (self.API_ERROR_CONTENT, 'Error: Error: ' + self.API_ERROR_CONTENT),
                (self.API_ERROR_CONTENT, 'Error: Agent terminated early due to an API error: different details'),
                ('API Error: Agent terminated early due to an API error: details',
                 'Error: API Error: Agent terminated early due to an API error: details'),
                ('Error: Agent terminated early due to an API error: details',
                 'Error: Error: Agent terminated early due to an API error: details'),
                ('boom', 'Error: boom'),
                ('Agent terminated early due to an API error:',
                 'Error: Agent terminated early due to an API error:'),
                ('Agent terminated early due to an API error:   ',
                 'Error: Agent terminated early due to an API error:   ')):
            with self.subTest(content=content, structured=structured):
                rows = [base_user(), tool_use(),
                        tool_result(content=content, is_error=True, structured=structured)]
                with self.assertRaises(RoomError):
                    et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        rows = [base_user(), tool_use(),
                tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured='a different row text')]
        with self.assertRaises(RoomError):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)

    def test_shared_result_projection_cannot_authorize_an_api_error(self):
        launch = {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                       'input': {'prompt': 'One', 'run_in_background': False}},
                      {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Agent',
                       'input': {'prompt': 'Two', 'run_in_background': False}}]}}
        shared = {'uuid': 'r-1', 'type': 'user', 'sessionId': NATIVE, 'cwd': WORKSPACE,
                  'sourceToolAssistantUUID': 'a-1',
                  'toolUseResult': self.API_ERROR_RESULT,
                  'message': {'role': 'user', 'content': [
                      {'type': 'tool_result', 'tool_use_id': 'toolu_1',
                       'content': self.API_ERROR_CONTENT, 'is_error': True},
                      {'type': 'tool_result', 'tool_use_id': 'toolu_2',
                       'content': self.API_ERROR_CONTENT, 'is_error': True}]}}
        with self.assertRaisesRegex(RoomError, 'row-level structured tool result is shared'):
            et.child_evidence(raw([base_user(), launch, shared]), NATIVE, WORKSPACE)

    def test_foreign_source_api_error_result_refuses(self):
        row = tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT)
        row['sourceToolAssistantUUID'] = 'a-other'
        with self.assertRaisesRegex(RoomError, 'not owned by the exact launching message'):
            et.child_evidence(raw([base_user(), tool_use(), row]), NATIVE, WORKSPACE)

    def test_background_api_error_text_is_not_a_terminal_proof(self):
        rows = [base_user(), tool_use(background=True),
                tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT)]
        with self.assertRaises(RoomError):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)
        rows = [base_user(), tool_use(background=True),
                tool_result(content=self.API_ERROR_CONTENT, is_error=True, structured=self.API_ERROR_RESULT),
                notification_row(notification('agent-1'))]
        with self.assertRaises(RoomError):
            et.child_evidence(raw(rows), NATIVE, WORKSPACE)


class TransitionStateDigestTests(unittest.TestCase):
    """Digest-only unit check of the exact transition head; the real refresh chain is a separate file."""

    def test_state_digest_and_exact_pointer_normalization(self):
        state = {'room_id': 'ao-synthetic', em.POINTER_KEY: {'request_id': 'a'}, 'value': 1}
        self.assertEqual(et._state_digest(state), __import__('ao_project_room').digest(state))
        changed = copy.deepcopy(state)
        changed[em.POINTER_KEY] = {'request_id': 'b'}
        self.assertNotEqual(et._state_digest(state), et._state_digest(changed))
        self.assertNotIn(em.POINTER_KEY, et._state_with_pointer(state, None))
