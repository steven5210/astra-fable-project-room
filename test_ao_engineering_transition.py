"""Offline contracts for the audited engineering-model transition core; no AO, account or model calls.

The room is one temporary synthetic normal room built from private records: one retained engineer request with
its immutable receipt, a real local Git worktree, a real read-only owner database, a plain native transcript and
an injected fake AO transport. The routing bundle validation, the AO project-rules read and the recorded Claude
executable evidence are stubbed at their module boundary because those frozen contracts have their own suites;
everything else, including ao_engineering_model, the room lock, state publication and the create-once journal, is
exercised for real. Nothing here claims these tests were executed.
"""

import copy
import hashlib
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_engineering_transition as et
import ao_history_reconciliation as reconciliation
import ao_project_room as ao
import ao_routing
import deepseek_adapter
from room import RoomError

FABLE = 'claude-fable-5-1'
OPUS = 'claude-opus-5-5'
ROOM = 'ao-synthetic'
SESSION = 'engineer'
NATIVE = 'native-1'
CONVERSATION = 'conversation-1'
BRANCH = 'branch-1'
TEXT = 'Review the exact synthetic specification.'
NOTIFICATION = ('<task-notification>\n<task-id>task1</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n'
                '<output-file>/synthetic/output</output-file>\n<status>completed</status>\n'
                '<summary>finished</summary>\n<note>terminal</note>\n</task-notification>')
RULES = {'clause_present': True, 'consistent': True, 'source': 'sync'}
EXECUTABLE = {'path': '/synthetic/claude', 'version': '2.1.280 (Claude Code)', 'error': None}


def files_under(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        relative = str(path.relative_to(directory))
        if relative == 'state.json' or relative == em.BASE or relative.startswith(em.BASE + '/'):
            continue
        if path.is_file():
            info = path.stat()
            result[relative] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size,
                                stat.S_IMODE(info.st_mode))
    return result


class FakeAO:
    """Injected AO transport. Only the paths this controller uses are served."""

    def __init__(self, repo):
        self.repo = str(repo)
        self.calls = []
        self.sessions = {}
        self.snapshots = {}
        self.workspaces = {}
        self.patch = 'apply'
        self.patches = 0

    def reset(self):
        self.calls = []
        self.sessions = {}
        self.snapshots = {}
        self.workspaces = {}
        self.patches = 0
        self.patch = 'apply'

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        base = path.split('?')[0]
        if method == 'PATCH':
            return self._patch(base, payload)
        if base.startswith('/desktop/sessions/'):
            return {'sessionId': base.split('/')[3], 'workspacePath': self.workspaces[base.split('/')[3]]}
        if base.startswith('/sessions/') and base.endswith('/conversation'):
            return copy.deepcopy(self.snapshots[base.split('/')[2]])
        if base.startswith('/sessions/'):
            return {'session': copy.deepcopy(self.sessions[base.split('/')[2]])}
        raise AssertionError('unexpected AO path ' + path)

    def conversation(self, session_id):
        return copy.deepcopy(self.snapshots[session_id])

    def _patch(self, path, payload):
        session = path.split('/')[2]
        self.patches += 1
        if self.patch == 'apply':
            self.snapshots[session]['settings'].update(copy.deepcopy(payload))
            return {'settings': copy.deepcopy(self.snapshots[session]['settings'])}
        if self.patch == 'lose':
            self.snapshots[session]['settings'].update(copy.deepcopy(payload))
            raise RoomError('AO PATCH failed (URLError); inspect AO and reconcile, never blindly retry')
        if self.patch == 'refuse':
            raise RoomError('AO PATCH failed (URLError); inspect AO and reconcile, never blindly retry')
        raise AssertionError('unknown patch mode ' + self.patch)


class TransitionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', '-C', str(self.repo), 'init', '-q'], check=True, capture_output=True)
        (self.repo / 'feature.txt').write_text('start\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'feature.txt'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Synthetic',
                        '-c', 'user.email=synthetic@example.invalid', 'commit', '-q', '-m', 'synthetic'],
                       check=True, capture_output=True)
        self.home = self.root / 'state'
        self.fake = FakeAO(self.repo)
        self.service = ao.Service(self.home, lambda url: self.fake)
        for patcher in self.stubs():
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate_local_stub(self, prepared, state=None, directory=None):
        routing = (prepared or {}).get('routing')
        return copy.deepcopy(routing) if isinstance(routing, dict) else None

    def stubs(self):
        return [patch.object(ao_routing, 'validate_local', self.validate_local_stub),
                patch.object(ao_routing, 'observe_rules', lambda client, state: copy.deepcopy(RULES)),
                patch.object(ao_routing, 'rules_match', lambda observed, recorded: observed == recorded),
                patch.object(em, 'executable_identity',
                             lambda directory, state, prepared: copy.deepcopy(EXECUTABLE))]

    # --- synthetic room ----------------------------------------------------------------
    def build_room(self, model=FABLE, selection=False, approval=None, rows=(), outcome=None):
        directory = self.home / 'ao' / 'rooms' / ROOM
        directory.mkdir(parents=True, mode=0o700)
        self.directory = directory
        spec = {'revision': 1, 'content': 'Implement the synthetic contract.\n', 'gates': [['true']],
                'approval': 'Astra approves this exact scope', 'approver': 'astra',
                'workflow': 'fable_engineering'}
        spec['sha256'] = ao.digest(spec['content'].encode())
        ao.atomic(directory / 'specs' / '1.json', spec)
        receipt = {'turn': {'id': 'turn-1', 'state': 'completed', 'providerTurnId': 'provider-1',
                            'stopReason': 'end_turn'},
                   'messages': [{'id': 'm-1', 'turnId': 'turn-1', 'role': 'user', 'text': TEXT},
                                {'id': 'm-2', 'turnId': 'turn-1', 'role': 'assistant',
                                 'text': 'synthetic final answer'}],
                   'history_truncated': False}
        ao.atomic(directory / 'receipts' / 'spec_review' / 'receipt.json', receipt)
        request = {'request_id': 'spec_review', 'key': ao.digest({'synthetic': 1}), 'role': 'engineer',
                   'session_id': SESSION, 'harness': 'claude-code', 'model': model, 'reasoning_effort': 'max',
                   'conversation_id': CONVERSATION, 'branch_id': BRANCH, 'text': TEXT,
                   'text_sha256': ao.digest(TEXT.encode()), 'state': 'completed', 'turn_id': 'turn-1',
                   'provider_turn_id': 'provider-1', 'created_order': 1, 'created_at': 1700000000.0,
                   'spec_record_sha256': ao.digest(spec), 'purpose': 'spec_review',
                   'baseline': {'turn_ids': [], 'conversation_id': CONVERSATION, 'branch_id': BRANCH,
                                'usage': {}},
                   'receipt': 'receipts/spec_review/receipt.json', 'receipt_sha256': ao.digest(receipt)}
        if outcome is not None:
            record = {'version': 1, 'room_id': ROOM, 'request_id': 'spec_review',
                      'receipt_sha256': request['receipt_sha256'], 'text_sha256': request['text_sha256'],
                      'turn_id': 'turn-1', 'provider_turn_id': 'provider-1', 'native': None,
                      'outcome': {'kind': outcome, 'hold': True, 'reason': 'synthetic unverified result'}}
            value = ao.digest(record)
            ao.atomic(directory / 'outcomes' / 'spec_review' / (value + '.json'), record)
            request.update(semantic_outcome='outcomes/spec_review/' + value + '.json',
                           semantic_outcome_sha256=value, semantic_status=record['outcome'])
        engineer = {'session_id': SESSION, 'model': model, 'reasoning_effort': 'max', 'harness': 'claude-code',
                    'fable_reason': 'Designated Fable engineering role',
                    'identity_basis': 'AO configured settings; not provider attestation',
                    'conversation_id': CONVERSATION, 'branch_id': BRANCH}
        reviewer = {'session_id': 'reviewer', 'model': 'astra', 'reasoning_effort': 'max', 'harness': 'codex',
                    'fable_reason': None, 'identity_basis': 'AO configured settings; not provider attestation',
                    'conversation_id': 'reviewer-conversation', 'branch_id': 'reviewer-branch'}
        state = {'version': 2, 'room_id': ROOM, 'project_path': str(self.repo),
                 'git_common_dir': str(ao.common_dir(self.repo)), 'feature': 'synthetic',
                 'ao_project_id': 'project', 'ao_url': 'http://127.0.0.1:1234',
                 'workflow': 'fable_engineering', 'authorization': 'User authorized this synthetic room',
                 'bindings': {'engineer': engineer, 'reviewer': reviewer},
                 'requests': {'spec_review': request}, 'exception_authorization': None, 'verifications': [],
                 'acceptances': [], 'created_at': 1700000000.0, 'spec': 'specs/1.json',
                 'spec_record_sha256': ao.digest(spec)}
        if selection:
            pinned = em.selection(None, self.service.root)
            em.store_once(directory, pinned['policy_record'], pinned['policy'])
            state[em.SELECTION_KEY] = pinned
        ao_delegates.initialize(self.service, directory, state, 'none')
        prepared = {'room_id': ROOM, 'worktree': str(self.repo), 'provider': 'none',
                    'delegate_sha256': ao.digest(state['delegate']),
                    'routing': {'version': 2, 'execution_policy': 'orchestrator', 'matcher': '.*',
                                'effort': 'max', 'env': dict(ao_routing.ENV), 'deny': list(ao_routing.DENY),
                                'browser_skill': ao_routing.BROWSER_SKILL, 'files': {'claude/agents/pr-opus.md': '0' * 64},
                                'guard_path': str(self.service.root / 'launchers' / ('0' * 64 + '.py')),
                                'guard_sha256': '0' * 64, 'hook_command': 'synthetic-hook',
                                'python': sys.executable, 'claude': copy.deepcopy(EXECUTABLE),
                                'claude_config_dir': str(self.root / 'claude'), 'worktree': str(self.repo),
                                'rules': copy.deepcopy(RULES), 'agents': {'pr-opus': 'opus'},
                                'agent_selection': {'pr-opus': {'kind': 'family', 'family': 'opus'}},
                                'agent_identity_basis': 'configured family selector; not attributed'}}
        ao.atomic(directory / 'preparation.json', prepared)
        state.update(preparation='preparation.json', preparation_sha256=ao.digest(prepared),
                     preparation_status='configured')
        self.database = self.make_owner()
        self.transcript = self.root / (NATIVE + '.jsonl')
        self.write_transcript(rows)
        snapshot = {'sessionId': SESSION, 'conversationId': CONVERSATION, 'activeBranchId': BRANCH,
                    'controller': 'ready', 'settings': {'model': model, 'reasoningEffort': 'max'},
                    'history_truncated': False,
                    'turns': [{'id': 'turn-1', 'state': 'completed', 'providerTurnId': 'provider-1'}],
                    'messages': [{'id': 'm-1', 'turnId': 'turn-1', 'role': 'user', 'text': TEXT, 'sequence': 1},
                                 {'id': 'm-2', 'turnId': 'turn-1', 'role': 'assistant',
                                  'text': 'synthetic final answer', 'sequence': 2}],
                    'activities': [], 'hasMoreBefore': False}
        if approval is not None:
            snapshot['settings']['approvalMode'] = approval
        self.fake.sessions[SESSION] = {'id': SESSION, 'projectId': 'project', 'harness': 'claude-code',
                                       'mode': 'chat', 'kind': 'worker', 'isTerminated': False}
        self.fake.snapshots[SESSION] = snapshot
        self.fake.workspaces[SESSION] = str(self.repo)
        ao.atomic(directory / 'state.json', state)
        return state

    def write_transcript(self, rows):
        base = [{'uuid': 'h-1', 'type': 'user', 'sessionId': NATIVE, 'cwd': str(self.repo),
                 'message': {'role': 'user', 'content': TEXT}},
                *rows,
                {'uuid': 'a-1', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': str(self.repo),
                 'message': {'role': 'assistant', 'id': 'msg-1', 'model': FABLE, 'stop_reason': 'end_turn',
                             'content': [{'type': 'text', 'text': 'synthetic final answer'}]}}]
        self.transcript.write_text(''.join(json_line(row) + '\n' for row in base))

    def make_owner(self, activity='idle', generation='generation-1', project='project',
                   conversation=CONVERSATION, branch=BRANCH, workspace=None, native=NATIVE):
        path = self.root / 'ao.sqlite'
        if path.exists():
            path.unlink()
        database = sqlite3.connect(path)
        database.executescript(
            'CREATE TABLE sessions(id TEXT, project_id TEXT, harness TEXT, session_mode TEXT,'
            ' is_terminated INTEGER, activity_state TEXT, workspace_path TEXT,'
            ' provider_conversation_id TEXT, controller_generation TEXT);'
            'CREATE TABLE conversations(id TEXT, current_session_id TEXT, active_branch_id TEXT);'
            'CREATE TABLE conversation_branches(id TEXT, conversation_id TEXT, provider_conversation_id TEXT,'
            ' session_id TEXT, strategy TEXT, replay_truncated INTEGER);')
        database.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                         (SESSION, project, 'claude-code', 'chat', 0, activity, workspace or str(self.repo),
                          native, generation))
        database.execute('INSERT INTO conversations VALUES(?,?,?)', (conversation, SESSION, branch))
        database.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                         (branch, conversation, native, SESSION, 'native', 0))
        database.commit()
        database.close()
        path.chmod(0o600)
        return path

    def make_ledger(self, state_name, resolved=False):
        folder = self.home / 'deepseek'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = sqlite3.connect(folder / 'ledger.sqlite3')
        database.executescript(
            'CREATE TABLE IF NOT EXISTS jobs(id TEXT, room_id TEXT, request_id TEXT, state TEXT,'
            ' profile_sha256 TEXT, requested_model TEXT, created_at TEXT, finished_at TEXT,'
            ' possibly_billed INTEGER, content_sha256 TEXT, content_path TEXT);'
            'CREATE TABLE IF NOT EXISTS resolutions(job_id TEXT, room_id TEXT);')
        database.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         ('job-1', ROOM, 'request-1', state_name, 'a' * 64, 'synthetic-model',
                          '2026-01-01T00:00:00+00:00', None, 0, None, None))
        if resolved:
            database.execute('INSERT INTO resolutions VALUES(?,?)', ('job-1', ROOM))
        database.commit()
        database.close()

    # --- helpers -----------------------------------------------------------------------
    def state(self):
        return ao.read(self.directory / 'state.json')

    def complete_room_bytes(self):
        """Every room file, including state.json and the whole transition journal.

        The module-level files_under helper deliberately excludes the mutable state and the
        transition journal a legitimate transition writes, so it cannot show that a read-only
        operation changed nothing there. This snapshot covers those exact bytes and every new or
        absent file, so an audit or a pure replay can be compared against it without weakening any
        transition assertion that legitimately expects its own journal to change.
        """
        result = {}
        for path in sorted(Path(self.directory).rglob('*')):
            if path.is_file():
                info = path.stat()
                result[str(path.relative_to(self.directory))] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size, stat.S_IMODE(info.st_mode))
        return result

    def reset(self, **kwargs):
        rooms = self.home / 'ao' / 'rooms'
        if rooms.exists():
            shutil.rmtree(rooms)
        ledger = self.home / 'deepseek'
        if ledger.exists():
            shutil.rmtree(ledger)
        self.fake.reset()
        return self.build_room(**kwargs)

    def audit(self, target=OPUS):
        return et.audit(self.service, ROOM, target, str(self.database), str(self.transcript))

    def request_arguments(self, target=OPUS, request_id='model-2'):
        result = self.audit(target)
        self.assertTrue(result['eligible'], result.get('reason'))
        return result, {'room_id': ROOM, 'request_id': request_id,
                        'source_model': result['source']['configured_model'],
                        'target_model': result['target']['configured_model'],
                        'audit_sha256': result['audit_sha256'],
                        'spec_record_sha256': result['spec_record_sha256'],
                        'candidate_sha256': result['candidate_sha256'],
                        'native_history_sha256': result['native_history_sha256'],
                        'native_owner_database': str(self.database),
                        'native_transcript_path': str(self.transcript),
                        'authorization': 'The user authorized this exact engineering model transition',
                        'reason': 'The qualified engineering orchestrator replaces the original for this room'}

    def run_transition(self, arguments):
        return et.transition(self.service, **arguments)

    def abandon(self, request_id='model-2', **changes):
        arguments = {'room_id': ROOM, 'request_id': request_id,
                     'native_owner_database': str(self.database),
                     'native_transcript_path': str(self.transcript),
                     'authorization': 'The user authorized abandoning this uncertain attempt',
                     'diagnosis': 'The AO PATCH response was lost; a fresh observation still shows the source'}
        arguments.update(changes)
        return et.abandon(self.service, **arguments)

    def assert_message(self, expected, call):
        with self.assertRaisesRegex(RoomError, expected):
            call()

    # --- shared mutations ---------------------------------------------------------------
    def patch_state(self, mutate):
        state = self.state()
        mutate(state)
        ao.atomic(self.directory / 'state.json', state)

    def set_outcome(self, kind):
        state = self.state()
        request = state['requests']['spec_review']
        record = {'version': 1, 'room_id': ROOM, 'request_id': 'spec_review',
                  'receipt_sha256': request['receipt_sha256'], 'text_sha256': request['text_sha256'],
                  'turn_id': 'turn-1', 'provider_turn_id': 'provider-1', 'native': None,
                  'outcome': {'kind': kind, 'hold': True, 'reason': 'synthetic outcome'}}
        value = ao.digest(record)
        ao.atomic(self.directory / 'outcomes' / 'spec_review' / (value + '.json'), record)
        request.update(semantic_outcome='outcomes/spec_review/' + value + '.json',
                       semantic_outcome_sha256=value, semantic_status=record['outcome'])
        ao.atomic(self.directory / 'state.json', state)

    def agent_launch(self, background=False, uuid='a-2', tool_id='toolu_1'):
        return {'uuid': uuid, 'type': 'assistant', 'sessionId': NATIVE, 'cwd': str(self.repo),
                'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': tool_id, 'name': 'Agent',
                     'input': {'subagent_type': 'pr-opus', 'prompt': 'Review the exact synthetic fragment.',
                               'run_in_background': background}}]}}

    def tool_result(self, tool_use_id='toolu_1', tool_use_result=None, source_uuid='a-2',
                    content='launched', is_error=None, row_uuid='h-2'):
        block = {'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': content}
        if is_error is not None:
            block['is_error'] = is_error
        row = {'uuid': row_uuid, 'type': 'user', 'sessionId': NATIVE, 'cwd': str(self.repo),
               'sourceToolAssistantUUID': source_uuid,
               'message': {'role': 'user', 'content': [block]}}
        if tool_use_result is not None:
            row['toolUseResult'] = tool_use_result
        return row

    DENIAL_TEXT = ('Project Room routing guard: Agent parameters model are refused (no model '
                   'override, isolation, resume or team routing)')

    def denial_result(self, content=None, structured=None, **kwargs):
        content = self.DENIAL_TEXT if content is None else content
        if structured is None:
            structured = 'Error: ' + content
        return self.tool_result(content=content, is_error=True, tool_use_result=structured, **kwargs)

    def notification_row(self, text, row_uuid='h-3'):
        return {'uuid': row_uuid, 'type': 'user', 'sessionId': NATIVE, 'cwd': str(self.repo),
                'origin': {'kind': 'task-notification'}, 'promptSource': 'sdk', 'queueSkipAttachments': True,
                'userType': 'external', 'entrypoint': 'sdk-ts', 'isSidechain': False,
                'message': {'role': 'user', 'content': text}}

    def orphan_intent(self, result=None, arguments=None, recorded_at=1700000000.5):
        if result is None:
            result, arguments = self.request_arguments()
        policy = em.effective_policy(self.service.root)[0]
        selector = {'kind': 'exact', 'model': OPUS}
        target = {'selector': selector, 'configured_model': OPUS, 'reasoning_effort': 'max',
                  'harness': 'claude-code', 'qualification': copy.deepcopy(policy['models'][OPUS]),
                  'policy': policy, 'policy_sha256': ao.digest(policy)}
        inputs = {key: arguments[key] for key in ('request_id', 'source_model', 'target_model', 'audit_sha256',
                                                  'spec_record_sha256', 'candidate_sha256',
                                                  'native_history_sha256', 'native_owner_database',
                                                  'native_transcript_path', 'authorization', 'reason')}
        intent = {'version': 1, 'room_id': ROOM, 'request_id': inputs['request_id'], 'inputs': inputs,
                  'inputs_sha256': ao.digest(inputs), 'audit_sha256': inputs['audit_sha256'],
                  'evidence': copy.deepcopy(result['evidence']),
                  'before_state_sha256': result['evidence']['state_sha256'], 'previous': None,
                  'source': copy.deepcopy(result['evidence']['source']), 'target': target,
                  'patch': {'method': 'PATCH', 'path': '/sessions/engineer/conversation/settings',
                            'payload': {'model': OPUS, 'reasoningEffort': 'max'}},
                  'recorded_at': recorded_at, 'provider_transition_receipt_sha256': None}
        em.store_once(self.directory, em.BASE + '/requests/' + inputs['request_id'] + '.json', intent)
        return intent

    def publish_pending(self, result=None, arguments=None):
        """Publish the exact pending head of one intent with no attempt marker written."""
        if result is None:
            result, arguments = self.request_arguments()
        intent = self.orphan_intent(result, arguments)
        state = self.state()
        state[em.POINTER_KEY] = {'request_id': arguments['request_id'],
                                 'intent': em.BASE + '/requests/' + arguments['request_id'] + '.json',
                                 'intent_sha256': ao.digest(intent), 'state': 'pending',
                                 'record': None, 'record_sha256': None}
        em.journal(self.directory, state, allow_pending=True)
        ao.atomic(self.directory / 'state.json', state)
        return intent


class AuditTests(TransitionCase):
    def test_audit_binds_complete_evidence_and_writes_nothing(self):
        self.reset(approval='auto')
        before, calls = files_under(self.directory), len(self.fake.calls)
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['audit_sha256'], ao.digest(result['evidence']))
        self.assertEqual(result['source'], {'selector': {'kind': 'exact', 'model': FABLE},
                                            'configured_model': FABLE, 'reasoning_effort': 'max'})
        self.assertEqual(result['target']['selector'], {'kind': 'exact', 'model': OPUS})
        self.assertEqual(result['target']['qualification']['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(result['controller'], 'ready')
        self.assertEqual(result['native_owner']['activity_state'], 'idle')
        self.assertEqual(result['native_transcript_sha256'], ao.digest(self.transcript.read_bytes()))
        self.assertEqual(result['candidate_sha256'], ao.candidate_snapshot(self.repo)['sha256'])
        self.assertEqual(result['boundary_order'], 1)
        self.assertEqual(result['children']['agent_launches'], 0)
        self.assertEqual(result['children']['limitation'], et.LIMITATION)
        self.assertEqual(result['ledger'], {'present': False, 'count': 0, 'sha256': ao.digest([])})
        self.assertEqual(result['patch']['payload'],
                         {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'})
        self.assertEqual(result['patch']['path'], '/sessions/engineer/conversation/settings')
        self.assertEqual(files_under(self.directory), before)
        self.assertEqual(self.fake.patches, 0)
        self.assertEqual([call[0] for call in self.fake.calls[calls:]], ['GET'] * (len(self.fake.calls) - calls))
        self.assertEqual(self.audit()['audit_sha256'], result['audit_sha256'])

    def test_audit_refuses_unqualified_identical_and_unknown_targets(self):
        self.reset()
        for target in ('sonnet', 'claude-opus-5-9', 'not-a-model'):
            with self.subTest(target=target):
                result = self.audit(target)
                self.assertFalse(result['eligible'])
                self.assertIn('Qualified engineering selectors are', result['reason'])
        result = self.audit(FABLE)
        self.assertFalse(result['eligible'])
        self.assertIn('identical family selector reset', result['reason'])

    def test_audit_refuses_source_owner_and_transcript_drift(self):
        for name, mutate, expected in (
                ('settings', lambda: self.fake.snapshots[SESSION]['settings'].__setitem__('model', OPUS),
                 'AO configured model/effort changed'),
                ('active-owner', lambda: self.make_owner(activity='active'), 'must be idle'),
                ('stopped-controller', lambda: self.fake.snapshots[SESSION].__setitem__('controller', 'stopped'),
                 'live idle controller'),
                ('other-project', lambda: self.make_owner(project='elsewhere'), 'another AO project'),
                ('other-branch', lambda: self.make_owner(branch='branch-2'), 'conversation or branch'),
                ('other-workspace', lambda: self.make_owner(workspace=str(self.root)), 'prepared engineer worktree'),
                ('missing-transcript', lambda: self.transcript.unlink(), 'transcript is unavailable'),
                ('truncated-history', lambda: self.fake.snapshots[SESSION].__setitem__('history_truncated', True),
                 'Complete native history')):
            with self.subTest(case=name):
                self.reset()
                mutate()
                result = self.audit()
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])
                self.assertEqual(self.fake.patches, 0)

    def test_audit_refuses_unsettled_unowned_and_orphan_evidence(self):
        for name, mutate, expected in (
                ('unowned-turn', lambda: self.fake.snapshots[SESSION]['turns'].append(
                    {'id': 'turn-2', 'state': 'completed', 'providerTurnId': 'provider-2'}), 'unowned or intervening'),
                ('failed-turn', lambda: self.fake.snapshots[SESSION]['turns'].append(
                    {'id': 'turn-2', 'state': 'failed', 'providerTurnId': 'provider-2'}), 'not completed'),
                ('uncertain-request', lambda: self.patch_state(
                    lambda state: state['requests']['spec_review'].__setitem__('state', 'running')),
                 'active or uncertain'),
                ('running-verification', lambda: self.patch_state(
                    lambda state: state.__setitem__('verifications', [{'state': 'running'}])), 'unfinished verification'),
                ('pending-transition', lambda: self.orphan_intent(), 'engineering model transition'),
                ('unknown-outcome', lambda: self.set_outcome('unknown'), 'semantic outcome is unknown')):
            with self.subTest(case=name):
                self.reset()
                mutate()
                result = self.audit()
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])

    def test_audit_refuses_unknown_child_evidence(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.assertEqual(result['children']['agent_launches'], 0)
        for name, rows, expected in (
                ('launch-without-result', (self.agent_launch(),), 'no correlated result'),
                ('background-without-notification', (self.agent_launch(background=True), self.tool_result()),
                 'no terminal task notification'),
                ('unknown-notification', (self.notification_row('<task-notification>\n<status>open</status>\n'
                                                                '</task-notification>'),), 'unknown shape'),
                ('notification-without-launch', (self.notification_row(NOTIFICATION),),
                 'names no recorded parent Agent/Task launch')):
            with self.subTest(case=name):
                self.reset()
                self.write_transcript(rows)
                result = self.audit()
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])
        self.reset()
        self.write_transcript((self.agent_launch(background=True),
                               self.tool_result(tool_use_result={'status': 'completed', 'agentId': 'agent-1'}),
                               self.notification_row(NOTIFICATION.replace('task1', 'agent-1'))))
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 1)
        self.assertEqual(result['children']['background_launches'], 1)
        self.assertEqual(result['children']['terminal_notifications'], 1)

    def test_audit_accepts_a_guard_denied_foreground_launch(self):
        self.reset()
        self.write_transcript((self.agent_launch(), self.denial_result()))
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 1)
        self.assertEqual(result['children']['guard_denied_launches'], 1)
        self.assertEqual(result['children']['terminal_api_errors'], 0)

    def test_audit_accepts_a_guard_denial_before_a_completed_launch(self):
        self.reset()
        self.write_transcript((
            self.agent_launch(), self.denial_result(),
            self.agent_launch(uuid='a-3', tool_id='toolu_2'),
            self.tool_result(tool_use_id='toolu_2', source_uuid='a-3', row_uuid='h-4',
                             tool_use_result={
                                 'status': 'completed', 'agentId': 'agent-1',
                                 'prompt': 'Review the exact synthetic fragment.',
                                 'content': [{'type': 'text', 'text': 'Finished.'}],
                                 'totalToolUseCount': 1, 'totalDurationMs': 1, 'totalTokens': 0,
                                 'usage': {'input_tokens': 0, 'output_tokens': 0,
                                           'cache_creation_input_tokens': None,
                                           'cache_read_input_tokens': None, 'server_tool_use': None,
                                           'service_tier': None, 'cache_creation': None}})))
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 2)
        self.assertEqual(result['children']['guard_denied_launches'], 1)

    def test_audit_refuses_lookalike_denial_results(self):
        for name, result_row, expected in (
                ('plain-error', self.tool_result(content='something else', is_error=True,
                                                 tool_use_result='Error: something else'),
                 'no validated terminal result'),
                ('internal-error-form',
                 self.denial_result(content='Project Room routing guard error: boom'),
                 'no validated terminal result'),
                ('structured-mismatch', self.denial_result(structured='Error: something else'),
                 'no validated terminal result')):
            with self.subTest(case=name):
                self.reset()
                self.write_transcript((self.agent_launch(), result_row))
                result = self.audit()
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])
        self.reset()
        self.write_transcript((self.agent_launch(), self.denial_result(),
                               self.notification_row(NOTIFICATION)))
        result = self.audit()
        self.assertFalse(result['eligible'])
        self.assertIn('guard-denied launch', result['reason'])

    GUARD_SHA = '0' * 64
    HOOK_STDERR = ('Project Room routing guard error: a caller record combines an origin.kind=human '
                   'identity with a different turnOrigin; the identity is contradictory')

    def hook_result(self, content=None, structured=None, stderr=None, command_sha=None, **kwargs):
        sha = self.GUARD_SHA if command_sha is None else command_sha
        stderr = self.HOOK_STDERR if stderr is None else stderr
        if content is None:
            content = ('PreToolUse:Agent hook error: [/opt/python3 /room/launchers/' + sha +
                       '.py; s=$?; [ "$s" -eq 0 ] || exit 2]: ' + stderr + '\n')
        if structured is None:
            structured = 'Error: ' + content
        return self.tool_result(content=content, is_error=True, tool_use_result=structured, **kwargs)

    def completed_result(self, **kwargs):
        return self.tool_result(tool_use_result={
            'status': 'completed', 'agentId': 'agent-1',
            'prompt': 'Review the exact synthetic fragment.',
            'content': [{'type': 'text', 'text': 'Finished.'}],
            'totalToolUseCount': 1, 'totalDurationMs': 1, 'totalTokens': 0,
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': None,
                      'cache_read_input_tokens': None, 'server_tool_use': None, 'service_tier': None,
                      'cache_creation': None}}, **kwargs)

    def test_audit_accepts_a_hook_blocked_launch_of_the_pinned_guard(self):
        self.reset()
        self.write_transcript((self.agent_launch(), self.hook_result()))
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 1)
        self.assertEqual(result['children']['hook_blocked_launches'], 1)
        self.assertEqual(result['children']['guard_denied_launches'], 0)

    def test_audit_refuses_lookalike_hook_blocked_results(self):
        cases = (
            ('other-guard-sha',
             {'command_sha': '1' * 64}, 'no validated terminal result'),
            ('foreign-stderr',
             {'stderr': 'Traceback (most recent call last): boom'}, 'no validated terminal result'),
            ('structured-mismatch',
             {'structured': 'Error: something else'}, 'no validated terminal result'),
            ('wrong-tool-name',
             {'content': 'PreToolUse:Task hook error: [/opt/python3 /room/launchers/' + self.GUARD_SHA +
                         '.py; s=$?; [ "$s" -eq 0 ] || exit 2]: ' + self.HOOK_STDERR},
             'no validated terminal result'))
        for name, kwargs, expected in cases:
            with self.subTest(case=name):
                self.reset()
                self.write_transcript((self.agent_launch(), self.hook_result(**kwargs)))
                result = self.audit()
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])
        self.reset()
        self.write_transcript((self.agent_launch(), self.hook_result()))
        with self.assertRaisesRegex(RoomError, 'no validated terminal result'):
            et.child_evidence(self.transcript.read_bytes(), NATIVE, str(self.repo),
                              guard_sha256=None)
        self.write_transcript((self.agent_launch(), self.hook_result(),
                               self.notification_row(NOTIFICATION)))
        result = self.audit()
        self.assertFalse(result['eligible'])
        self.assertIn('hook-blocked launch', result['reason'])

    def test_audit_accepts_denied_and_hook_blocked_launches_before_a_completed_launch(self):
        self.reset()
        rows = [
            self.agent_launch(), self.denial_result(),
            self.agent_launch(uuid='a-3', tool_id='toolu_2'),
            self.denial_result(tool_use_id='toolu_2', source_uuid='a-3', row_uuid='h-4'),
            self.agent_launch(uuid='a-4', tool_id='toolu_3'),
            self.hook_result(tool_use_id='toolu_3', source_uuid='a-4', row_uuid='h-6'),
            self.agent_launch(uuid='a-5', tool_id='toolu_4'),
            self.hook_result(tool_use_id='toolu_4', source_uuid='a-5', row_uuid='h-8'),
            self.agent_launch(uuid='a-6', tool_id='toolu_5'),
            self.hook_result(tool_use_id='toolu_5', source_uuid='a-6', row_uuid='h-10'),
            self.agent_launch(uuid='a-7', tool_id='toolu_6'),
            self.completed_result(tool_use_id='toolu_6', source_uuid='a-7', row_uuid='h-12')]
        self.write_transcript(tuple(rows))
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 6)
        self.assertEqual(result['children']['guard_denied_launches'], 2)
        self.assertEqual(result['children']['hook_blocked_launches'], 3)

    def test_audit_refuses_an_active_delegate_ledger(self):
        self.reset()
        self.make_ledger(next(iter(deepseek_adapter.ACTIVE_STATES)))
        result = self.audit()
        self.assertFalse(result['eligible'])
        self.assertIn('delegate job', result['reason'])
        self.reset()
        self.make_ledger(next(iter(deepseek_adapter.STOP_STATES)))
        self.assertFalse(self.audit()['eligible'])
        self.reset()
        self.make_ledger(deepseek_adapter.COMPLETED)
        result = self.audit()
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['ledger']['count'], 1)

    def test_audits_leave_state_and_the_complete_transition_journal_unchanged(self):
        self.reset()
        before = self.complete_room_bytes()
        self.assertIn('state.json', before)
        eligible = self.audit()
        self.assertTrue(eligible['eligible'], eligible.get('reason'))
        self.assertEqual(self.complete_room_bytes(), before)
        self.assertEqual(self.fake.patches, 0)
        for target, expected in ((FABLE, 'identical family selector reset'),
                                 ('not-a-model', 'Qualified engineering selectors are')):
            with self.subTest(target=target):
                result = self.audit(target)
                self.assertFalse(result['eligible'])
                self.assertIn(expected, result['reason'])
                self.assertEqual(self.complete_room_bytes(), before)
                self.assertEqual(self.fake.patches, 0)
        # A real pending transition refuses the audit through the journal and writes nothing either.
        self.publish_pending()
        pending = self.complete_room_bytes()
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        result = self.audit()
        self.assertFalse(result['eligible'])
        self.assertIn('uncommitted engineering model transition exists', result['reason'])
        self.assertEqual(self.complete_room_bytes(), pending)
        self.assertEqual(self.fake.patches, 0)

def json_line(value):
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class TransitionTests(TransitionCase):
    def test_exact_legacy_to_opus_commits_with_exactly_one_patch(self):
        self.reset()
        bindings = copy.deepcopy(self.state()['bindings'])
        before = files_under(self.directory)
        result, arguments = self.request_arguments()
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertFalse(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['patch_result']['acknowledged'], True)
        self.assertEqual(outcome['target'], {'configured_model': OPUS, 'reasoning_effort': 'max'})
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertEqual(outcome['boundary_order'], 1)
        self.assertEqual(self.fake.snapshots[SESSION]['settings'],
                         {'model': OPUS, 'reasoningEffort': 'max'})
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'committed')
        self.assertEqual(em.current(self.directory, state)['model'], OPUS)
        self.assertEqual(em.effective_binding(self.directory, state)['model'], OPUS)
        self.assertEqual([epoch['model'] for epoch in em.epochs(self.directory, state)], [FABLE, OPUS])
        self.assertEqual(state['bindings'], bindings)
        self.assertEqual(files_under(self.directory), before)
        summary = em.summary(self.directory, state)
        self.assertEqual(summary['attempts'][0]['outcome'], 'committed')
        self.assertEqual(summary['observed']['settings'], {'model': OPUS, 'reasoningEffort': 'max'})

    def test_identical_request_is_idempotent_and_changed_inputs_refuse(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.assertTrue(self.run_transition(arguments)['transitioned'])
        record = ao.read(self.directory / (em.BASE + '/records/model-2.json'))
        again = self.run_transition(arguments)
        self.assertTrue(again['idempotent'])
        self.assertTrue(again['transitioned'])
        self.assertEqual(again['record_sha256'], ao.digest(record))
        self.assertEqual(self.fake.patches, 1)
        self.assert_message('different engineering model transition payload',
                            lambda: self.run_transition({**arguments, 'reason': 'A different reason text'}))
        # Reusing this audit for a new request_id after the target is already in force has nothing to
        # apply and is refused; only a fresh audit of a genuinely different target may transition again.
        self.assert_message('identical family selector reset',
                            lambda: self.run_transition({**arguments, 'request_id': 'model-3'}))
        self.assertEqual(self.fake.patches, 1)
        fresh, renewed = self.request_arguments(target=FABLE, request_id='model-4')
        self.assertTrue(fresh['eligible'], fresh.get('reason'))
        self.assertTrue(self.run_transition(renewed)['transitioned'])
        self.assertEqual(self.fake.patches, 2)
        self.assertEqual(em.current(self.directory, self.state())['model'], FABLE)

    def test_unqualified_family_reset_now_requires_the_operator_qualification(self):
        self.reset(model='fable', selection=True)
        result = self.audit('fable')
        self.assertFalse(result['eligible'])
        self.assertIn('operator-selected family qualification', result['reason'])
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(self.state().get(em.POINTER_KEY))
        self.assertFalse((self.directory / em.BASE).exists())
        # A genuinely different unqualified family target is refused the same way, and a read-only
        # audit of the changed room remains available.
        result = self.audit('opus')
        self.assertFalse(result['eligible'])
        self.assertIn('operator-selected family qualification', result['reason'])
        self.assertEqual(self.fake.patches, 0)

    def test_approval_mode_is_preserved_in_the_patch_and_the_observation(self):
        self.reset(approval='auto')
        result, arguments = self.request_arguments()
        self.assertEqual(result['patch']['payload'],
                         {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'})
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(outcome['observed']['settings'],
                         {'model': OPUS, 'reasoningEffort': 'max', 'approvalMode': 'auto'})
        record = ao.read(self.directory / (em.BASE + '/records/model-2.json'))
        self.assertEqual(record['observed_after']['settings'], outcome['observed']['settings'])
        self.assertEqual(record['patch_result']['acknowledged'], True)
        self.assertEqual(self.fake.patches, 1)

    def test_patch_observation_must_keep_every_other_setting(self):
        self.reset(approval='auto')
        result, arguments = self.request_arguments()
        original = self.fake._patch
        self.fake._patch = lambda path, payload: self.drop_approval(path, payload)
        try:
            outcome = self.run_transition(arguments)
        finally:
            self.fake._patch = original
        self.assertFalse(outcome['transitioned'])
        self.assertTrue(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        self.assertIn('stays pending', outcome['reason'])

    def drop_approval(self, path, payload):
        session = path.split('/')[2]
        self.fake.patches += 1
        settings = copy.deepcopy(payload)
        settings.pop('approvalMode', None)
        self.fake.snapshots[session]['settings'].update(settings)
        self.fake.snapshots[session]['settings'].pop('approvalMode', None)
        return {'settings': copy.deepcopy(self.fake.snapshots[session]['settings'])}

    def test_stale_audit_refuses_each_frozen_drift(self):
        for name, mutate in (
                ('source-settings', lambda: self.fake.snapshots[SESSION]['settings'].__setitem__('model', OPUS)),
                ('candidate', lambda: (self.repo / 'feature.txt').write_text('drifted\n')),
                ('history', lambda: self.fake.snapshots[SESSION]['messages'].append(
                    {'id': 'm-3', 'turnId': 'turn-1', 'role': 'assistant', 'text': 'more', 'sequence': 3})),
                ('owner-generation', lambda: self.make_owner(generation='generation-2')),
                ('ledger', lambda: self.make_ledger(deepseek_adapter.COMPLETED)),
                ('specification', self.drift_spec)):
            with self.subTest(drift=name):
                self.reset()
                result, arguments = self.request_arguments()
                mutate()
                with self.assertRaises(RoomError):
                    self.run_transition(arguments)
                self.assertEqual(self.fake.patches, 0)
                self.assertIsNone(self.state().get(em.POINTER_KEY))
                self.assertFalse((self.directory / em.BASE).exists())

    def drift_spec(self):
        state = self.state()
        second = {'revision': 2, 'content': 'Implement the revised synthetic contract.\n', 'gates': [['true']],
                  'approval': 'Astra approves this revision', 'approver': 'astra',
                  'workflow': 'fable_engineering'}
        second['sha256'] = ao.digest(second['content'].encode())
        ao.atomic(self.directory / 'specs' / '2.json', second)
        state.update(spec='specs/2.json', spec_record_sha256=ao.digest(second))
        ao.atomic(self.directory / 'state.json', state)

    def test_uncertain_patch_stays_pending_and_blocks_other_work(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        outcome = self.run_transition(arguments)
        self.assertFalse(outcome['transitioned'])
        self.assertTrue(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['patch_result']['acknowledged'], False)
        self.assertIn('PATCH failed', outcome['patch_result']['error'])
        self.assertIsNone(outcome['record'])
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'pending')
        self.assert_message('uncommitted engineering model transition exists',
                            lambda: em.journal(self.directory, state))
        self.assert_message('uncommitted engineering model transition exists',
                            lambda: self.service.settled(state))
        before_status = files_under(self.directory)
        status = self.service.ao_room_status(ROOM)
        self.assertEqual(status['room_id'], ROOM)
        self.assertEqual(status['engineering_model']['pending']['request_id'], 'model-2')
        self.assertEqual(files_under(self.directory), before_status)
        self.assert_message('reconcile only that identical request',
                            lambda: self.run_transition({**arguments, 'request_id': 'model-other'}))
        again = self.run_transition(arguments)
        self.assertTrue(again['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(self.fake.snapshots[SESSION]['settings'], {'model': FABLE, 'reasoningEffort': 'max'})

    def test_uncertain_patch_reconciles_later_without_a_second_patch(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        self.assertTrue(self.run_transition(arguments)['pending'])
        self.fake.snapshots[SESSION]['settings'] = {'model': OPUS, 'reasoningEffort': 'max'}
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['patch_result']['acknowledged'], False)
        self.assertIn('already attempted', outcome['patch_result']['error'])
        self.assertEqual(em.current(self.directory, self.state())['model'], OPUS)
        self.assertEqual(self.run_transition(arguments)['idempotent'], True)

    def test_lost_patch_response_commits_from_the_observation(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'lose'
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(outcome['patch_result']['acknowledged'], False)
        self.assertIn('PATCH failed', outcome['patch_result']['error'])
        again = self.run_transition(arguments)
        self.assertTrue(again['idempotent'])
        self.assertEqual(self.fake.patches, 1)

    def test_crash_windows_are_recovered_from_the_exact_files(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.orphan_intent(result, arguments)
        self.assert_message('Unclaimed, missing or inconsistent',
                            lambda: em.journal(self.directory, self.state(), allow_pending=True))
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(self.fake.patches, 1)
        committed_state = self.state()
        record = ao.read(self.directory / (em.BASE + '/records/model-2.json'))
        rolled = {key: value for key, value in committed_state.items() if key != em.POINTER_KEY}
        ao.atomic(self.directory / 'state.json', rolled)
        self.assert_message('Unclaimed, missing or inconsistent',
                            lambda: em.journal(self.directory, self.state()))
        retry = self.run_transition(arguments)
        self.assertTrue(retry['transitioned'], retry)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(ao.read(self.directory / (em.BASE + '/records/model-2.json')), record)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'committed')
        self.assertIsNone(em.journal(self.directory, self.state())['pending'])

    def test_abandonment_closes_only_an_unchanged_pending_attempt(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'])
        self.fake.snapshots[SESSION]['controller'] = 'stopped'
        self.make_owner(activity='exited', generation='generation-2')
        outcome = self.abandon()
        self.assertTrue(outcome['abandoned'])
        self.assertFalse(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertTrue(outcome['patch_attempted'])
        self.assertEqual(outcome['attempt_sha256'], pending['attempt_sha256'])
        self.assertEqual(outcome['observed']['controller'], 'stopped')
        record = ao.read(self.directory / (em.BASE + '/records/model-2.json'))
        self.assertEqual(record['outcome'], 'abandoned_unchanged')
        self.assertEqual(record['attempt_sha256'], pending['attempt_sha256'])
        self.assertEqual(record['patch_result'], et.NO_PATCH)
        self.assertEqual(record['observed_after']['native_owner']['activity_state'], 'exited')
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'abandoned')
        self.assertIsNone(em.journal(self.directory, state)['pending'])
        self.service.settled(state)
        self.assertEqual([epoch['model'] for epoch in em.epochs(self.directory, state)], [FABLE])
        self.assertTrue(self.abandon()['idempotent'])
        self.assert_message('different terminal record',
                            lambda: self.abandon(authorization='A different authorization text'))

    def test_abandonment_refuses_changed_settings_or_owner(self):
        for name, mutate in (
                ('target-applied', lambda: self.fake.snapshots[SESSION].__setitem__(
                    'settings', {'model': OPUS, 'reasoningEffort': 'max'})),
                ('owner-changed', lambda: self.make_owner(branch='branch-2')),
                ('transcript-changed',
                 lambda: self.write_transcript((self.agent_launch(), self.tool_result())))):
            with self.subTest(case=name):
                self.reset()
                result, arguments = self.request_arguments()
                self.fake.patch = 'refuse'
                self.assertTrue(self.run_transition(arguments)['pending'])
                mutate()
                self.assert_message('unchanged frozen evidence', lambda: self.abandon())
                self.assertEqual(self.fake.patches, 1)
                self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')

    def test_a_pre_attempt_pending_request_can_be_abandoned(self):
        self.reset()
        result, arguments = self.request_arguments()
        intent = self.publish_pending(result, arguments)
        outcome = self.abandon()
        self.assertTrue(outcome['abandoned'])
        self.assertFalse(outcome['pending'])
        self.assertFalse(outcome['patch_attempted'])
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(outcome['attempt_sha256'])
        record = ao.read(self.directory / (em.BASE + '/records/model-2.json'))
        self.assertIsNone(record['attempt_sha256'])
        self.assertEqual(record['patch_result'], et.NO_PATCH)
        self.assertEqual(record['observed_before'], et._before(intent['evidence']))
        self.assertEqual(ao.digest(intent), outcome['intent_sha256'])
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'abandoned')

    def test_an_unpublished_orphan_intent_refuses_abandonment(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.orphan_intent(result, arguments)
        path = self.directory / (em.BASE + '/requests/model-2.json')
        raw = path.read_bytes()
        with self.assertRaisesRegex(RoomError, 'Unclaimed'):
            self.abandon()
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.directory / (em.BASE + '/records/model-2.json')).exists())
        self.assertEqual(self.fake.patches, 0)
        self.assertIsNone(self.state().get(em.POINTER_KEY))

    def test_known_semantic_hold_is_retained_and_historical_records_survive(self):
        self.reset(outcome='quota_limit')
        bindings = copy.deepcopy(self.state()['bindings'])
        requests = copy.deepcopy(self.state()['requests'])
        before = files_under(self.directory)
        result, arguments = self.request_arguments()
        self.assertTrue(self.run_transition(arguments)['transitioned'])
        state = self.state()
        self.assertEqual(state['requests'], requests)
        self.assertEqual(state['bindings'], bindings)
        self.assertEqual(state['acceptances'], [])
        self.assertEqual(state['verifications'], [])
        self.assertEqual(files_under(self.directory), before)
        self.assertEqual(state['requests']['spec_review']['semantic_outcome_sha256'],
                         requests['spec_review']['semantic_outcome_sha256'])
        self.assertEqual(state['requests']['spec_review']['semantic_status']['kind'], 'quota_limit')
        # The hold is retained and a read-only audit of a genuinely different target stays available.
        other = self.audit(FABLE)
        self.assertTrue(other['eligible'], other.get('reason'))
        self.assertFalse(self.audit(OPUS)['eligible'])
        self.assertEqual([epoch['model'] for epoch in em.epochs(self.directory, state)], [FABLE, OPUS])


class Correction3Tests(TransitionCase):
    def pending_attempt(self, **kwargs):
        self.reset(**kwargs)
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'])
        return result, arguments, pending

    def test_full_state_digest_binds_the_exact_transition_head(self):
        self.reset()
        state = self.state()
        self.assertEqual(et._state_digest(state), ao.digest(state))
        changed = copy.deepcopy(state)
        changed[em.POINTER_KEY] = {'synthetic': 'different-head'}
        self.assertNotEqual(et._state_digest(changed), et._state_digest(state))
        self.assertNotIn(em.POINTER_KEY, et._state_with_pointer(state, None))

    def test_pending_resolution_claim_uses_store_once_and_verifies_sha(self):
        self.reset()
        state = {'room_id': ROOM}
        pending = {'path': em.BASE + '/resolutions/spec_review.json', 'sha256': 'a' * 64,
                   'value': {'version': 1, 'synthetic': True}}
        with patch.object(em, 'store_once', return_value='b' * 64) as store:
            with self.assertRaisesRegex(RoomError, 'changed before it could be claimed'):
                et._claim_pending_resolution(None, self.directory, state, pending)
            store.assert_called_once_with(self.directory, pending['path'], pending['value'])

    def test_abandonment_refuses_each_full_frozen_evidence_drift(self):
        drifts = (
            ('candidate', lambda: (self.repo / 'feature.txt').write_text('drifted\n')),
            ('spec', lambda: self.patch_state(lambda state: state.update(spec_record_sha256='0' * 64))),
            ('ledger', lambda: self.make_ledger(deepseek_adapter.COMPLETED)),
            ('routing', lambda: self.patch_state(lambda state: state.update(routing_adoption={'synthetic': True}))),
            ('executable', lambda: self.patch_state(lambda state: state.update(executable_binding={'synthetic': True}))),
            ('requests', lambda: self.patch_state(lambda state: state['requests'].update(
                extra={'request_id': 'extra', 'created_order': 2, 'role': 'engineer', 'session_id': SESSION,
                       'state': 'completed'}))),
            ('history', lambda: self.fake.snapshots[SESSION]['messages'].append(
                {'id': 'm-extra', 'turnId': 'turn-1', 'role': 'assistant', 'text': 'drift'})),
            ('state', lambda: self.patch_state(lambda state: state.update(synthetic_drift=True))),
            ('settings', lambda: self.fake.snapshots[SESSION]['settings'].__setitem__('approvalMode', 'never')),
        )
        for name, mutate in drifts:
            with self.subTest(drift=name):
                self.pending_attempt(approval='auto')
                mutate()
                with self.assertRaises(RoomError):
                    self.abandon()
                self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')

    def test_abandonment_after_a_record_write_recovers_the_exact_stored_bytes(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        self.assertTrue(self.run_transition(arguments)['pending'])
        original_save = self.service.save

        def crash(directory, state):
            raise OSError('synthetic crash after the durable abandonment record')

        self.service.save = crash
        try:
            with self.assertRaises(OSError):
                self.abandon()
        finally:
            self.service.save = original_save
        record_path = self.directory / (em.BASE + '/records/model-2.json')
        raw = record_path.read_bytes()
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        outcome = self.abandon()
        self.assertTrue(outcome['abandoned'])
        self.assertFalse(outcome['pending'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(outcome['record_sha256'], ao.digest(ao.read(record_path)))
        self.assertEqual(outcome['attempt_sha256'], ao.read(record_path)['attempt_sha256'])
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'abandoned')

    def test_abandonment_retry_republishes_the_record_across_a_failed_ancestor_barrier(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        self.assertTrue(self.run_transition(arguments)['pending'])
        original_fsync = em._fsync

        def barrier(path, folder=False):
            if folder and Path(path).name == 'records':
                raise OSError('synthetic ancestor barrier failure')
            return original_fsync(path, folder=folder)

        with patch.object(em, '_fsync', side_effect=barrier):
            with self.assertRaises(OSError):
                self.abandon()
        record_path = self.directory / (em.BASE + '/records/model-2.json')
        raw = record_path.read_bytes()
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        calls = []
        store, save = em.store_once, self.service.save

        def recording_store(directory, relative, value):
            calls.append(('store', relative))
            return store(directory, relative, value)

        def recording_save(directory, state):
            calls.append(('save', None))
            return save(directory, state)

        with patch.object(em, 'store_once', side_effect=recording_store), \
                patch.object(self.service, 'save', side_effect=recording_save):
            outcome = self.abandon()
        self.assertTrue(outcome['abandoned'])
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual([name for name, _ in calls], ['store', 'save'])
        self.assertEqual(calls[0][1], em.BASE + '/records/model-2.json')
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'abandoned')

    def test_abandonment_before_any_record_is_written_retries_from_the_pending_head(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.fake.patch = 'refuse'
        self.assertTrue(self.run_transition(arguments)['pending'])
        record_path = self.directory / (em.BASE + '/records/model-2.json')
        original = em.store_once

        def refused(directory, relative, value):
            if relative == em.BASE + '/records/model-2.json' and not record_path.exists():
                raise OSError('synthetic crash before the abandonment record')
            return original(directory, relative, value)

        with patch.object(em, 'store_once', side_effect=refused):
            with self.assertRaises(OSError):
                self.abandon()
        self.assertFalse(record_path.exists())
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        outcome = self.abandon()
        self.assertTrue(outcome['abandoned'])
        self.assertTrue(record_path.exists())
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'abandoned')

    def test_deferred_changed_input_refusal_leaves_state_and_proof_bytes(self):
        self.reset()
        result, arguments = self.request_arguments()
        self.assertTrue(self.run_transition(arguments)['transitioned'])
        state = self.state()
        state['requests']['spec_review']['history_reconciliation_sha256'] = 'a' * 64
        ao.atomic(self.directory / 'state.json', state)
        proof_dir = self.directory / 'history-reconciliations' / 'spec_review'
        proof_dir.mkdir(parents=True, exist_ok=True)
        proof = proof_dir / ('a' * 64 + '.json')
        proof.write_bytes(b'synthetic proof bytes')
        before_state = (self.directory / 'state.json').read_bytes()
        before_proof = proof.read_bytes()
        with self.assertRaisesRegex(RoomError, 'different engineering model transition payload'):
            self.run_transition({**arguments, 'reason': 'A different reason text'})
        self.assertEqual((self.directory / 'state.json').read_bytes(), before_state)
        self.assertEqual(proof.read_bytes(), before_proof)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'committed')

    def durable_abandonment(self, request_id='model-2'):
        """One real pending attempt plus the exact durable abandonment record a crashed save leaves.

        The attempt marker and its single refused PATCH are real; the abandonment record is written
        and validated by the real abandon lane, and only its pointer save is failed. Nothing here
        synthesizes an intent, record or pointer by hand.
        """
        self.reset()
        arguments = self.request_arguments(request_id=request_id)[1]
        self.fake.patch = 'refuse'
        pending = self.run_transition(arguments)
        self.assertTrue(pending['pending'], pending)
        record_path = self.directory / (em.BASE + '/records/' + request_id + '.json')
        original_save = self.service.save

        def crash(directory, state):
            raise OSError('synthetic crash after the durable abandonment record')

        self.service.save = crash
        try:
            with self.assertRaises(OSError):
                self.abandon(request_id=request_id)
        finally:
            self.service.save = original_save
        self.assertTrue(record_path.is_file())
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        return arguments, ao.read(record_path), record_path.read_bytes()

    def owner_liveness_change(self):
        """The only owner change an abandonment may legitimately observe: liveness and generation."""
        self.fake.snapshots[SESSION]['controller'] = 'stopped'
        self.make_owner(activity='exited', generation='generation-2')

    def transcript_drift(self):
        """One benign extra parent row: the same room with changed retained transcript bytes."""
        self.write_transcript(({'uuid': 'a-3', 'type': 'assistant', 'sessionId': NATIVE, 'cwd': str(self.repo),
                                'message': {'role': 'assistant',
                                            'content': [{'type': 'text', 'text': 'more'}]}},))

    def test_identical_transition_recovers_a_durable_abandonment_without_a_second_patch(self):
        arguments, record, raw = self.durable_abandonment()
        record_path = self.directory / (em.BASE + '/records/model-2.json')
        self.assertEqual(record['outcome'], em.OUTCOMES['abandoned'])
        self.assertEqual(record['patch_result'], dict(et.NO_PATCH))
        self.assertTrue(record['attempt_sha256'])
        outcome = self.run_transition(arguments)
        self.assertTrue(outcome['abandoned'], outcome)
        self.assertFalse(outcome['transitioned'])
        self.assertFalse(outcome['pending'])
        self.assertFalse(outcome['idempotent'])
        self.assertEqual(outcome['outcome'], em.OUTCOMES['abandoned'])
        self.assertEqual(outcome['settings_patch'], 0)
        self.assertEqual(outcome['patch_result'], dict(et.NO_PATCH))
        self.assertTrue(outcome['patch_attempted'])
        self.assertEqual(outcome['attempt_sha256'], record['attempt_sha256'])
        self.assertEqual(outcome['record_sha256'], ao.digest(record))
        self.assertEqual(self.fake.patches, 1)
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'abandoned')
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(ao.read(record_path), record)
        self.assertIsNone(em.journal(self.directory, state)['pending'])
        self.assertEqual([epoch['configured_model'] for epoch in em.epochs(self.directory, state)], [FABLE])
        # The recorded abandonment is the existing authorization; the transition's own
        # authorization and reason are never substituted for it.
        self.assertEqual(record['abandonment']['authorization'],
                         'The user authorized abandoning this uncertain attempt')
        self.assertEqual(record['abandonment']['diagnosis'],
                         'The AO PATCH response was lost; a fresh observation still shows the source')
        self.assertNotEqual(record['abandonment']['authorization'], arguments['authorization'])

    def test_durable_abandonment_recovery_permits_liveness_and_refuses_drift(self):
        cases = (
            ('unchanged', lambda: None, None),
            ('owner-liveness', self.owner_liveness_change, None),
            ('source-settings', lambda: self.fake.snapshots[SESSION]['settings'].__setitem__('model', OPUS),
             'AO configured model/effort changed'),
            ('candidate', lambda: (self.repo / 'feature.txt').write_text('drifted\n'),
             'evidence changed (candidate)'),
            ('owner-identity', lambda: self.make_owner(branch='branch-2'), 'conversation or branch differs'),
            ('history-transcript', self.transcript_drift, 'evidence changed (native.transcript_sha256)'),
        )
        for name, mutate, refusal in cases:
            with self.subTest(case=name):
                arguments, record, raw = self.durable_abandonment()
                record_path = self.directory / (em.BASE + '/records/model-2.json')
                patches = self.fake.patches
                mutate()
                if refusal is None:
                    outcome = self.run_transition(arguments)
                    self.assertTrue(outcome['abandoned'], outcome)
                    self.assertFalse(outcome['pending'])
                    self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'abandoned')
                else:
                    before = self.complete_room_bytes()
                    with self.assertRaisesRegex(RoomError, re.escape('Abandonment requires the unchanged frozen '
                                                                     'evidence') + '.*' + re.escape(refusal)):
                        self.run_transition(arguments)
                    self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
                    self.assertEqual(self.complete_room_bytes(), before)
                self.assertEqual(record_path.read_bytes(), raw)
                self.assertEqual(ao.read(record_path), record)
                self.assertEqual(self.fake.patches, patches)

    def test_pending_abandonment_refusals_and_terminal_replay_are_no_write(self):
        arguments, record, raw = self.durable_abandonment()
        record_path = self.directory / (em.BASE + '/records/model-2.json')
        self.assertEqual(record['outcome'], em.OUTCOMES['abandoned'])
        before_state = (self.directory / 'state.json').read_bytes()
        before = self.complete_room_bytes()
        # The exact original transition inputs are required before any reconciliation; a changed
        # payload refuses without touching the state, the journal, the durable record or any proof.
        self.assert_message('different engineering model transition payload',
                            lambda: self.run_transition({**arguments, 'reason': 'A different reason text'}))
        self.assertEqual((self.directory / 'state.json').read_bytes(), before_state)
        self.assertEqual(self.complete_room_bytes(), before)
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'pending')
        self.assertEqual(self.fake.patches, 1)
        recovered = self.run_transition(arguments)
        self.assertTrue(recovered['abandoned'], recovered)
        self.assertEqual(self.fake.patches, 1)
        self.assertEqual(record_path.read_bytes(), raw)
        self.assertEqual(ao.read(record_path), record)
        state = self.state()
        self.assertEqual(state[em.POINTER_KEY]['state'], 'abandoned')
        # Marker-only observation, deliberately narrow: this is not a validated reconciliation
        # proof; it only lets a delegating spy report whether a pure path calls invalidation.
        state['requests']['spec_review'][reconciliation.PROOF] = 'a' * 64
        ao.atomic(self.directory / 'state.json', state)
        proof_dir = self.directory / 'history-reconciliations' / 'spec_review'
        proof_dir.mkdir(parents=True, exist_ok=True)
        proof = proof_dir / ('a' * 64 + '.json')
        proof.write_bytes(b'synthetic proof bytes')
        calls = []
        original = reconciliation.invalidate_latest

        def spy(service, directory, state, reason):
            calls.append(reason)
            return original(service, directory, state, reason)

        with patch.object(reconciliation, 'invalidate_latest', side_effect=spy):
            frozen_state = (self.directory / 'state.json').read_bytes()
            frozen = self.complete_room_bytes()
            ao_calls = len(self.fake.calls)
            replay = self.run_transition(arguments)
            self.assertTrue(replay['idempotent'], replay)
            self.assertTrue(replay['abandoned'])
            self.assertFalse(replay['transitioned'])
            self.assertEqual(len(self.fake.calls), ao_calls)
            self.assertEqual(self.fake.patches, 1)
            self.assertEqual(self.complete_room_bytes(), frozen)
            self.assert_message('different engineering model transition payload',
                                lambda: self.run_transition({**arguments, 'reason': 'Another reason text'}))
            self.assertEqual(self.complete_room_bytes(), frozen)
            self.assertEqual((self.directory / 'state.json').read_bytes(), frozen_state)
            self.assertEqual(proof.read_bytes(), b'synthetic proof bytes')
            self.assertEqual(calls, [])


class HistoryProofRenewalTests(unittest.TestCase):
    """Renewed reconciliations: the proof must cover the validated invalidation head in force."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'synthetic-room'
        self.directory.mkdir(mode=0o700)
        self.request = {'request_id': 'spec_review', 'receipt_sha256': 'a' * 64, 'text_sha256': 'b' * 64}
        self.state = {'room_id': 'synthetic-room', 'requests': {'spec_review': self.request}}
        ao.atomic(self.directory / 'state.json', self.state)

    def write_proof(self, invalidation=None):
        value = {'version': 1, 'kind': 'complete_history_native_quota', 'room_id': 'synthetic-room',
                 'request_id': 'spec_review', 'receipt_sha256': 'a' * 64, 'text_sha256': 'b' * 64,
                 'saved_history_truncated': True, 'inputs': {}, 'invalidation_sha256': invalidation,
                 'supersedes_outcome_sha256': None}
        sha = reconciliation._write(self.directory, 'history-reconciliations', value)
        self.request[reconciliation.PROOF] = sha
        ao.atomic(self.directory / 'state.json', self.state)
        return sha

    def test_a_superseded_proof_refuses_and_a_renewed_proof_is_current(self):
        superseded = self.write_proof()
        et._require_complete_history_proof(self.directory, self.request)

        class Save:
            def save(self, directory, state):
                ao.atomic(Path(directory) / 'state.json', state)

        reconciliation.invalidate(Save(), self.directory, self.state, 'spec_review',
                                  'Synthetic evidence drift')
        head = reconciliation.invalidation_head(self.directory, self.request)
        self.assertEqual(self.request[reconciliation.INVALIDATION], head)
        with self.assertRaisesRegex(RoomError, 'complete-history reconciliation proof'):
            et._require_complete_history_proof(self.directory, self.request)
        renewed = self.write_proof(invalidation=head)
        self.assertNotEqual(renewed, superseded)
        et._require_complete_history_proof(self.directory, self.request)
        self.assertEqual(self.request[reconciliation.PROOF], renewed)
        self.assertTrue((self.directory / 'history-reconciliations' / 'spec_review'
                         / (superseded + '.json')).is_file())
        self.assertTrue((self.directory / 'history-reconciliation-invalidations' / 'spec_review'
                         / (head + '.json')).is_file())


if __name__ == '__main__':
    unittest.main()
