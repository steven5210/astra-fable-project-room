"""Real preparation and routing-refresh integration for the engineering-model transition core.

The room, its pinned routing files, its private guard and its immutable routing-refresh record are built by the
supplied normal fixture and the real ao_routing / ao_routing_refresh APIs. Only the AO transport is an injected
fake: preparation, refresh validation, ao_routing.validate_local, ao_routing_refresh.effective, the room lock,
the create-once journal and state publication run for real, and every audit/transition call below leaves
validate_local and effective unpatched. Nothing here claims these tests were executed, and no AO, model,
account or network call is made.
"""

import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_engineering_transition as et
import ao_model_qualification as qmod
import ao_native_outcome
import ao_project_room as ao
import ao_routing
import ao_routing_guard
import ao_routing_refresh as refresh
from room import RoomError
from test_ao_normal import Fixture

OPUS = 'claude-opus-5-5'
CLI_VERSION = '2.1.282 (Claude Code)'
OLD_GUARD = (b'"""Synthetic historical deny-only guard without quota inspection."""\n'
             b'import json,sys\njson.loads(sys.stdin.read())\n')


class TransitionAfterRealRefreshTests(Fixture):
    """One real preparation, one real routing refresh, then the transition with routing validation unpatched."""

    def register_native_source(self):
        '''Register the one real synthetic owner database and transcript before the first charter.

        The inherited normal fixture fabricates an owner reader and a different native identity; this
        subclass instead creates the actual SQLite owner its bound engineer session uses, creates the
        owned transcript, registers that exact source through the supported preflight and keeps it for
        the refresh, the transition and the freeze.
        '''
        if getattr(self, 'native_outcome_source', None) is not None:
            return
        state = self.state()
        binding = (state.get('bindings') or {}).get('engineer')
        if not binding:
            return
        self.native_session = '00000000-0000-4000-8000-000000000010'
        self.NATIVE = self.native_session
        self.database = self.root / 'synthetic-ao.db'
        with sqlite3.connect(self.database) as db:
            db.executescript(
                'CREATE TABLE sessions(id TEXT, project_id TEXT, harness TEXT, session_mode TEXT,'
                ' is_terminated INTEGER, activity_state TEXT, workspace_path TEXT,'
                ' provider_conversation_id TEXT, controller_generation TEXT);'
                'CREATE TABLE conversations(id TEXT, current_session_id TEXT, active_branch_id TEXT);'
                'CREATE TABLE conversation_branches(id TEXT, conversation_id TEXT, provider_conversation_id TEXT,'
                ' session_id TEXT, strategy TEXT, replay_truncated INTEGER);')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                       (binding['session_id'], state['ao_project_id'], 'claude-code', 'chat', 0, 'idle',
                        self.native_row_workspace(), self.native_session, 'generation-one'))
            db.execute('INSERT INTO conversations VALUES(?,?,?)',
                       (binding['conversation_id'], binding['session_id'], binding['branch_id']))
            db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                       (binding['branch_id'], binding['conversation_id'], self.native_session,
                        binding['session_id'], 'native', 0))
        self.database.chmod(0o600)
        config_root = self.native_config_root()
        project_dir = config_root / 'projects' / str(self.repo).replace('/', '-')
        project_dir.mkdir(parents=True, exist_ok=True)
        self.transcript = project_dir / (self.native_session + '.jsonl')
        self.transcript.write_text('')
        self.native_events = []
        self.native_requests = set()
        source = {'database': str(self.database), 'transcript': str(self.transcript),
                  'session_id': binding['session_id'], 'native_session_id': self.native_session}
        ao_native_outcome.preflight_source(self.directory(), state, source['database'], source['transcript'])
        ao.atomic(self.directory() / 'state.json', state)
        self.native_outcome_source = source

    def setUp(self):
        super().setUp()
        self.cli = self.root / 'fake-claude-version'
        self.cli.write_text('#!' + sys.executable + '\nprint("' + CLI_VERSION + '")\n')
        self.cli.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.cli), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open()
        self.spec()
        original_bytes, original_definition = Path.read_bytes, ao_routing.agent_definition
        guard_source = Path(ao_routing_guard.__file__)

        def historical(path):
            return OLD_GUARD if path == guard_source else original_bytes(path)

        def archived_definition(name, model=None):
            # Forward the optional model so real qualified rendering keeps its own signature.
            text = original_definition(name) if model is None else original_definition(name, model)
            return text + '\nSynthetic archived worker instructions.\n'

        with patch.object(Path, 'read_bytes', historical), patch.object(
                ao_routing, 'agent_definition', side_effect=archived_definition), \
                patch.object(ao_routing, 'foreground_settings', side_effect=lambda settings: settings):
            self.bind()
        self.agree()
        self.patches = []
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        self.fake.snapshots['engineer'].update(controller='stopped', hasMoreBefore=False, activities=[],
                                               branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original_request = self.fake.request

        def request(method, path, payload=None):
            base = path.split('?')[0]
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots[path.split('/')[2]])
            if method == 'PATCH' and base.endswith('/conversation/settings'):
                session = base.split('/')[2]
                self.patches.append((base, copy.deepcopy(payload)))
                self.fake.snapshots[session]['settings'].update(copy.deepcopy(payload))
                return {'settings': copy.deepcopy(self.fake.snapshots[session]['settings'])}
            return original_request(method, path, payload)

        self.fake.request = request
        self.refresh_result = refresh.refresh(
            self.service, self.room, 'refresh-before-transition', str(self.database), self.native_session,
            'User authorizes this exact routing refresh',
            'The retained routing predates the current guard and bounded-worker instructions.')
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.write_transcript()
        self.result = et.audit(self.service, self.room, OPUS, str(self.database), str(self.transcript))

    def write_transcript(self):
        rows = ({'uuid': 'h-1', 'type': 'user', 'sessionId': self.native_session, 'cwd': str(self.repo),
                 'message': {'role': 'user', 'content': 'Synthetic retained instruction.'}},
                {'uuid': 'a-1', 'type': 'assistant', 'sessionId': self.native_session, 'cwd': str(self.repo),
                 'message': {'role': 'assistant',
                             'content': [{'type': 'text', 'text': 'Synthetic retained answer.'}]}})
        existing = []
        if self.transcript.exists():
            existing = [json.loads(line) for line in self.transcript.read_text().splitlines() if line.strip()]
        for row in existing:
            row.setdefault('cwd', self.native_row_workspace())
            message = row.setdefault('message', {})
            if row.get('type') == 'assistant':
                message.setdefault('role', 'assistant')
                message.setdefault('content', [{'type': 'text',
                                                'text': 'Synthetic retained native answer.'}])
            elif row.get('type') == 'user':
                message.setdefault('role', 'user')
        preserved = existing or list(rows)
        self.transcript.write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in preserved))
        # Seed the inherited append helper with the exact normalized rows just written, so a later
        # note_native_turn append preserves every prior semantic caller/model/stop/UUID event in the
        # same registered source instead of truncating it.
        self.native_events = [json.loads(json.dumps(row, sort_keys=True)) for row in preserved]

    def arguments(self, request_id='model-refreshed'):
        self.assertTrue(self.result['eligible'], self.result.get('reason'))
        return {'room_id': self.room, 'request_id': request_id,
                'source_model': self.result['source']['configured_model'],
                'target_model': self.result['target']['configured_model'],
                'audit_sha256': self.result['audit_sha256'],
                'spec_record_sha256': self.result['spec_record_sha256'],
                'candidate_sha256': self.result['candidate_sha256'],
                'native_history_sha256': self.result['native_history_sha256'],
                'native_owner_database': str(self.database),
                'native_transcript_path': str(self.transcript),
                'authorization': 'The user authorized this exact engineering model transition',
                'reason': 'The qualified engineering orchestrator replaces the original for this room'}

    def hook_pair(self, guard_sha256, tool_id, launch_uuid, result_uuid):
        """One Agent launch plus the hook-error result a nonzero pinned-guard exit records."""
        content = ('PreToolUse:Agent hook error: [/opt/python3 /ao/launchers/' + guard_sha256 +
                   '.py; s=$?; [ "$s" -eq 0 ] || exit 2]: Project Room routing guard error: '
                   'synthetic guard failure\n')
        return ({'uuid': launch_uuid, 'type': 'assistant', 'sessionId': self.native_session,
                 'cwd': self.native_row_workspace(),
                 'message': {'role': 'assistant', 'content': [
                     {'type': 'tool_use', 'id': tool_id, 'name': 'Agent',
                      'input': {'subagent_type': 'pr-opus', 'prompt': 'Synthetic fragment.'}}]}},
                {'uuid': result_uuid, 'type': 'user', 'sessionId': self.native_session,
                 'cwd': self.native_row_workspace(), 'sourceToolAssistantUUID': launch_uuid,
                 'message': {'role': 'user', 'content': [
                     {'type': 'tool_result', 'tool_use_id': tool_id, 'content': content,
                      'is_error': True}]},
                 'toolUseResult': 'Error: ' + content})

    def append_native(self, *rows):
        with self.transcript.open('a') as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True) + '\n')

    def test_audit_accepts_hook_blocks_from_the_whole_verified_guard_history(self):
        prepared = ao_delegates.preparation(self.directory(), self.state())
        original = prepared['routing']['guard_sha256']
        effective = refresh.effective(self.directory(), self.state(), prepared)['guard_sha256']
        self.assertNotEqual(original, effective)
        rows = self.hook_pair(original, 'toolu_old', 'a-10', 'h-10')
        rows += self.hook_pair(effective, 'toolu_new', 'a-11', 'h-11')
        self.append_native(*rows)
        result = et.audit(self.service, self.room, OPUS, str(self.database), str(self.transcript))
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['children']['agent_launches'], 2)
        self.assertEqual(result['children']['hook_blocked_launches'], 2)

    def test_audit_refuses_a_hook_block_of_a_sha_outside_the_verified_history(self):
        self.append_native(*self.hook_pair('2' * 64, 'toolu_foreign', 'a-10', 'h-10'))
        result = et.audit(self.service, self.room, OPUS, str(self.database), str(self.transcript))
        self.assertFalse(result['eligible'])
        self.assertIn('no validated terminal result', result['reason'])

    def test_audit_binds_the_current_refresh_record_guard_settings_and_executable(self):
        self.assertTrue(self.result['eligible'], self.result.get('reason'))
        prepared = ao_delegates.preparation(self.directory(), self.state())
        current = refresh.effective(self.directory(), self.state(), prepared)
        self.assertEqual(self.result['evidence']['routing'], current)
        self.assertEqual(self.state()['routing_refresh'],
                         {'path': self.refresh_result['path'], 'sha256': self.refresh_result['sha256']})
        self.assertEqual(self.result['evidence']['executable'],
                         {'path': str(self.cli), 'version': CLI_VERSION, 'error': None})
        self.assertEqual({key: value for key, value in self.result['patch']['payload'].items()
                          if key != 'approvalMode'},
                         {'model': OPUS, 'reasoningEffort': 'max'})
        self.assertEqual(self.patches, [])
        outcome = et.transition(self.service, **self.arguments())
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(len(self.patches), 1)
        settings = self.fake.snapshots['engineer']['settings']
        self.assertEqual((settings['model'], settings['reasoningEffort']), (OPUS, 'max'))
        self.assertEqual(self.state()[em.POINTER_KEY]['state'], 'committed')

    def test_pinned_file_guard_executable_or_head_drift_refuses_before_patch(self):
        self.assertTrue(self.result['eligible'], self.result.get('reason'))
        prepared = ao_delegates.preparation(self.directory(), self.state())
        routing = refresh.effective(self.directory(), self.state(), prepared)
        arguments = self.arguments()
        drifts = (
            ('pinned-file', self.repo / '.claude/agents/pr-opus.md', b'\nSynthetic pinned-file drift.\n'),
            ('guard', Path(routing['guard_path']), b'\n# Synthetic guard drift.\n'),
            ('executable', self.cli, b'\n# Synthetic executable drift.\n'),
            ('head', self.repo / 'feature.txt', b'synthetic candidate drift\n'),
        )
        for name, path, addition in drifts:
            with self.subTest(drift=name):
                original = path.read_bytes()
                info = path.stat()
                path.write_bytes(original + addition)
                try:
                    with self.assertRaises(RoomError):
                        et.transition(self.service, **arguments)
                finally:
                    path.write_bytes(original)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.assertEqual(self.patches, [])
                self.assertIsNone(self.state().get(em.POINTER_KEY))
                self.assertFalse((self.directory() / em.BASE).exists())
        fresh = et.audit(self.service, self.room, OPUS, str(self.database), str(self.transcript))
        self.assertTrue(fresh['eligible'], fresh.get('reason'))
        self.result = fresh
        outcome = et.transition(self.service, **self.arguments())
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(len(self.patches), 1)

    def test_qualified_family_target_admits_the_effective_refresh_and_freezes_the_exact_model(self):
        result = et.audit(self.service, self.room, 'opus', str(self.database), str(self.transcript))
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(result['target']['selector'], {'kind': 'family', 'family': 'opus'})
        self.assertEqual(result['expected_model'], OPUS)
        self.assertEqual(result['expected_model_basis'], 'operator-selected family qualification')
        self.assertEqual(result['qualification_sha256'], qmod.digest(self.qualification_artifact()))
        self.assertIsNone(result['observed_served_model'])
        self.assertEqual(self.patches, [])
        arguments = {'room_id': self.room, 'request_id': 'model-qualified',
                     'source_model': result['source']['configured_model'],
                     'target_model': result['target']['configured_model'],
                     'audit_sha256': result['audit_sha256'],
                     'spec_record_sha256': result['spec_record_sha256'],
                     'candidate_sha256': result['candidate_sha256'],
                     'native_history_sha256': result['native_history_sha256'],
                     'native_owner_database': str(self.database), 'native_transcript_path': str(self.transcript),
                     'authorization': 'The user authorized this exact engineering model transition',
                     'reason': 'The operator-selected family qualification pins the exact expected model'}
        outcome = et.transition(self.service, **arguments)
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(len(self.patches), 1)
        self.assertEqual(outcome['expected_model'], OPUS)
        self.assertEqual(outcome['qualification_sha256'], qmod.digest(self.qualification_artifact()))
        self.assertIsNone(outcome['observed_served_model'])
        self.assertEqual(outcome['effective_effort']['effective'], 'not evidenced')
        state = self.state()
        epoch = em.current(self.directory(), state)
        self.assertTrue(epoch['family_qualified'])
        self.assertEqual(epoch['expected_model'], OPUS)
        self.assertEqual(epoch['qualification_sha256'], qmod.digest(self.qualification_artifact()))
        # Every retained source byte verifies from the room alone, and the frozen target keeps its
        # pre-inference expectation without any model call.
        context = em.qualification_context(self.directory(), state, 'model-qualified')
        self.assertEqual(context['source'], 'committed_target')
        self.assertEqual(context['expected_model'], OPUS)
        pinned = qmod.verify_reference(self.directory(), context['reference'], 'synthetic retained proof')
        self.assertTrue(pinned['retained_evidence'])
        self.assertEqual(pinned['sha256'], qmod.digest(self.qualification_artifact()))
        # The next qualified request really reaches freeze: the supported pre-dispatch preflight
        # revalidates the exact source this room registered before its first charter, and the real
        # freeze guard validates that same registered identity before any request could be created.
        from ao_native_outcome import preflight_source
        state = self.state()
        source_before = copy.deepcopy(state['native_outcome_source'])
        database_before = Path(self.database).read_bytes()
        registered = preflight_source(self.directory(), state, str(self.database), str(self.transcript))
        self.assertEqual(registered['source'], source_before)
        self.assertEqual(registered['source']['database'], str(self.database))
        self.assertEqual(registered['source']['transcript'], str(self.transcript))
        self.assertEqual(registered['source']['native_session_id'], self.native_session)
        self.assertEqual(registered['registered'], 'identical')
        self.assertEqual(Path(self.database).read_bytes(), database_before)
        self.assertEqual(self.state()['native_outcome_source'], source_before)
        ao.atomic(self.directory() / 'state.json', state)
        frozen = em.freeze_request(self.directory(), state)
        self.assertEqual(frozen['version'], 2)
        self.assertEqual(frozen['selector'], {'kind': 'family', 'family': 'opus'})
        self.assertEqual(frozen['configured_model'], 'opus')
        self.assertEqual(frozen['expected_model'], OPUS)
        self.assertEqual(frozen['qualification_sha256'], qmod.digest(self.qualification_artifact()))
        self.assertIsNone(frozen['resolution_sha256'])

    def test_registered_transcript_bytes_survive_model_audit_and_transition(self):
        baseline = Path(self.transcript).read_bytes()
        result = et.audit(self.service, self.room, OPUS, str(self.database), str(self.transcript))
        self.assertTrue(result['eligible'], result.get('reason'))
        self.assertEqual(Path(self.transcript).read_bytes(), baseline)
        outcome = et.transition(self.service, **self.arguments())
        self.assertTrue(outcome['transitioned'], outcome)
        self.assertEqual(Path(self.transcript).read_bytes(), baseline)

    def test_first_delegation_observes_zero_launch_worker_coverage_from_the_registered_source(self):
        before = [json.loads(line) for line in Path(self.transcript).read_text().splitlines() if line.strip()]
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        after = [json.loads(line) for line in Path(self.transcript).read_text().splitlines() if line.strip()]
        self.assertEqual(after[:len(before)], before)
        request = self.state()['requests']['implementation']
        self.assertIn('native_worker_expectations', request)
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'completed')
        outcome = ao.read(self.directory() / request['semantic_outcome'])
        worker = outcome['worker_observations']
        self.assertTrue(worker['qualified'])
        self.assertEqual(worker['coverage'], 'complete')
        self.assertEqual(worker['executions'], [])
        self.assertEqual(outcome['outcome']['kind'], 'final_available')
