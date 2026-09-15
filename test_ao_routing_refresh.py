"""Audited routing refresh against synthetic native state; no model or account use."""

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import ao_delegates
import ao_executable_binding
import ao_project_room as ao
import ao_routing
import ao_routing_guard
import ao_routing_refresh as refresh
import deepseek_adapter
from test_ao_normal import Fixture
from test_ao_review_extension import ReviewExtensionFixture

OLD_GUARD = b'"""Synthetic historical deny-only guard with no quota inspection."""\nimport json,sys\njson.loads(sys.stdin.read())\n'


class RoutingRefreshTests(Fixture):
    def setUp(self):
        super().setUp()
        self.cli = self.root / 'fake-claude-version'
        self.cli.write_text('#!' + sys.executable + '\nprint("synthetic (Claude Code)")\n')
        self.cli.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.cli), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open(); self.spec()
        original_bytes, original_definition = Path.read_bytes, ao_routing.agent_definition
        guard_source = Path(ao_routing_guard.__file__)

        def historical(path):
            return OLD_GUARD if path == guard_source else original_bytes(path)

        with patch.object(Path, 'read_bytes', historical), patch.object(ao_routing, 'agent_definition',
                side_effect=lambda name: original_definition(name) + '\nSynthetic archived worker instructions.\n'):
            self.bind()
        self.agree()
        self.prepared = ao_delegates.preparation(self.directory(), self.state())
        self.prepared_bytes = (self.directory() / self.state()['preparation']).read_bytes()
        self.original = self.state()
        self.original_files = {name: (self.repo / name).read_bytes() for name in ao_routing.FILES}
        self.original_candidate = ao.candidate_snapshot(self.repo)
        self.posts = copy.deepcopy(self.fake.posts)
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        self.fake.snapshots['engineer'].update(controller='stopped', hasMoreBefore=False, activities=[],
            branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original_request = self.fake.request

        def request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots[path.split('/')[2]])
            return original_request(method, path, payload)

        self.fake.request = request
        self.database = self.root / 'synthetic-ao.db'
        self.native_session = '00000000-0000-4000-8000-000000000010'
        engineer = self.state()['bindings']['engineer']
        with sqlite3.connect(self.database) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)', ('engineer', 'project', 'claude-code', 'chat', 0,
                'exited', str(self.repo), self.native_session, 'generation-one'))
            db.execute('INSERT INTO conversations VALUES(?,?,?)', (engineer['conversation_id'], 'engineer', engineer['branch_id']))
            db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)', (engineer['branch_id'], engineer['conversation_id'],
                self.native_session, 'engineer', 'native', 0))
        self.database.chmod(0o600)
        self.args = dict(room_id=self.room, request_id='refresh-one', database_path=str(self.database),
            native_session_id=self.native_session, authorization='User authorizes the routing correction for the stopped engineer.',
            diagnosis='Retained routing predates the quota stop and bounded-worker instructions.')

    def do_refresh(self, **changes):
        return refresh.refresh(self.service, **{**self.args, **changes})

    def journal(self):
        return refresh._read(self.directory(), self.state()['routing_refresh'])

    def owner_update(self, sql, values=()):
        with sqlite3.connect(self.database) as db:
            db.execute(sql, values)

    def assert_no_intent(self):
        self.assertEqual(self.state(), self.original)
        self.assertFalse((self.directory() / refresh.BASE).exists())
        self.assertEqual(self.fake.posts, self.posts)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, self.original_files)

    def pending(self):
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.do_refresh()
        self.assertEqual(self.state(), self.original)

    def test_refresh_preserves_preparation_provider_history_and_candidate_with_gets_only(self):
        result = self.do_refresh()
        current = self.state()
        self.assertFalse(result['model_dispatch'])
        self.assertEqual({key: value for key, value in current.items() if key != 'routing_refresh'}, self.original)
        self.assertEqual((self.directory() / current['preparation']).read_bytes(), self.prepared_bytes)
        self.assertEqual(ao_delegates.validate_preparation(self.directory(), current), self.prepared)
        self.assertEqual(ao.candidate_snapshot(self.repo), self.original_candidate)
        self.assertEqual(self.fake.posts, self.posts)
        record = self.journal()
        self.assertEqual(record['source'], self.prepared['routing'])
        self.assertEqual(record['source_bundle']['files'], {name: data.decode() for name, data in self.original_files.items()})
        self.assertEqual(record['source_bundle']['guard'].encode(), OLD_GUARD)
        self.assertEqual(Path(record['source']['guard_path']).read_bytes(), OLD_GUARD)
        self.assertEqual(record['target_bundle']['guard'], Path(ao_routing_guard.__file__).read_text())
        self.assertEqual(refresh.effective(self.directory(), current, self.prepared), record['target'])
        self.assertEqual(record['target']['effort'], 'max')
        self.assertEqual(record['target']['agents'], record['source']['agents'])

    def test_prepared_target_guard_blocks_actual_synthetic_quota_before_opus(self):
        self.do_refresh()
        routing = refresh.effective(self.directory(), self.state(), self.prepared)
        transcript = self.root / (self.native_session + '.jsonl')
        common = dict(sessionId=self.native_session, cwd=str(self.repo), isSidechain=False)
        rows = [{**common, 'type': 'user', 'uuid': 'actual-native-caller', 'origin': {'kind': 'human'},
                 'message': {'role': 'user', 'content': 'Continue.'}},
                {**common, 'type': 'assistant', 'uuid': 'typed-native-quota', 'isApiErrorMessage': True,
                 'error': 'rate_limit', 'apiErrorStatus': 429,
                 'quotaLimits': {'status': 'rejected', 'rateLimitType': 'five_hour'},
                 'message': {'role': 'assistant', 'model': '<synthetic>', 'content': 'Native session quota rejected.'}}]
        transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows)); transcript.chmod(0o600)
        event = {'session_id': self.native_session, 'cwd': str(self.repo), 'transcript_path': str(transcript),
                 'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-opus', 'prompt': 'Try again'}}
        before = subprocess.run([sys.executable, self.prepared['routing']['guard_path']], input=json.dumps(event),
            capture_output=True, text=True, check=True)
        self.assertEqual(before.stdout, '')
        after = subprocess.run([sys.executable, routing['guard_path']], input=json.dumps(event), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(after.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_idempotent_reply_and_different_inputs_preserve_exact_receipt(self):
        first = self.do_refresh()
        before = (self.directory() / 'state.json').read_bytes()
        record = (self.directory() / first['path']).read_bytes()
        self.assertTrue(self.do_refresh()['idempotent'])
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual((self.directory() / first['path']).read_bytes(), record)
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.do_refresh(diagnosis='Different diagnosis')

    def test_matching_bundle_does_not_append_an_unnecessary_refresh(self):
        self.do_refresh()
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'already matches'):
            self.do_refresh(request_id='unnecessary-refresh')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(refresh._entries(self.directory())), 1)

    def test_dynamic_observation_ages_do_not_prevent_exact_reconciliation(self):
        original = self.fake.request
        calls = []

        def changing_age(method, path, payload=None):
            value = original(method, path, payload)
            if method == 'GET' and '/conversation?' in path:
                calls.append(True)
                value.update(elapsedMs=len(calls), observedAt='synthetic-observation-' + str(len(calls)))
                value['activities'].append({'id': 'stable-activity', 'status': 'completed', 'sequence': 1,
                                            'elapsedMs': len(calls)})
            return value

        self.fake.request = changing_age
        self.pending()
        self.do_refresh()
        self.assertGreater(len(calls), 2)
        self.assertEqual(self.journal()['observed_snapshot']['elapsedMs'], 1)

    def test_restart_preserves_owner_but_foreign_native_session_fails(self):
        self.do_refresh()
        self.owner_update("UPDATE sessions SET activity_state='idle',controller_generation='generation-two'")
        ao_delegates.validate_preparation(self.directory(), self.state())
        self.owner_update('UPDATE sessions SET provider_conversation_id=?', ('unrelated-native',))
        self.owner_update('UPDATE conversation_branches SET provider_conversation_id=?', ('unrelated-native',))
        with self.assertRaisesRegex(ao.RoomError, 'native owner changed'):
            refresh.effective(self.directory(), self.state(), self.prepared)

    def test_foreign_input_owner_running_owner_and_public_live_controller_refuse(self):
        with self.assertRaisesRegex(ao.RoomError, 'same stopped native owner'):
            self.do_refresh(native_session_id='different-native')
        self.assert_no_intent()
        self.owner_update("UPDATE sessions SET activity_state='running'")
        with self.assertRaisesRegex(ao.RoomError, 'same stopped native owner'):
            self.do_refresh()
        self.owner_update("UPDATE sessions SET activity_state='exited'")
        self.fake.snapshots['engineer']['controller'] = 'ready'
        with self.assertRaisesRegex(ao.RoomError, 'Stop the idle'):
            self.do_refresh()
        self.assert_no_intent()

    def test_wrong_branch_or_foreign_database_owner_refuses(self):
        self.owner_update("UPDATE sessions SET project_id='unrelated-project'")
        with self.assertRaisesRegex(ao.RoomError, 'same stopped native owner'):
            self.do_refresh()
        self.assert_no_intent()
        self.owner_update("UPDATE sessions SET project_id='project'")
        self.database.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'ownership is unsafe'):
            self.do_refresh()
        self.assert_no_intent()

    def test_unknown_truncated_or_unsettled_native_history_cannot_refresh(self):
        snapshot = self.fake.snapshots['engineer']
        before = copy.deepcopy(snapshot)
        for change in ('missing', 'truncated', 'active'):
            snapshot.clear(); snapshot.update(copy.deepcopy(before))
            if change == 'missing': snapshot.pop('hasMoreBefore')
            elif change == 'truncated': snapshot['history_truncated'] = True
            else: snapshot['turns'][-1]['state'] = 'in_progress'
            with self.assertRaises(ao.RoomError):
                self.do_refresh()
            self.assert_no_intent()

    def test_unsettled_owned_request_and_running_verification_refuse(self):
        state = self.state(); state['requests']['spec_review']['state'] = 'uncertain'
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaises(ao.RoomError): self.do_refresh()
        ao.atomic(self.directory() / 'state.json', self.original)
        state = self.state(); state['verifications'].append({'state': 'running'})
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'verification'):
            self.do_refresh()
        self.assertFalse((self.directory() / refresh.BASE).exists())

    def test_active_provider_job_is_not_ignored(self):
        # Provider ledger read is complete and read-only even for an inconsistent
        # synthetic room carrying provider-none and an unexpected recorded job.
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,state,created_at,profile_sha256,requested_model,'
                'lane,payload_sha256,thinking,max_tokens,input_bytes,reserved_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('a' * 32, self.room, 'synthetic-job', deepseek_adapter.STREAMING, 'synthetic-time', 'profile',
                 'synthetic-model', 'deep', 'payload', 'enabled', 393216, 10, 10))
        with self.assertRaisesRegex(ao.RoomError, 'Active delegate job'):
            self.do_refresh()
        self.assert_no_intent()

    def test_semantic_quota_hold_is_preserved_and_never_released(self):
        state = self.state()
        state['requests']['spec_review']['semantic_status'] = 'quota_error'
        state['requests']['spec_review']['semantic_observation_error'] = 'Synthetic native quota retained'
        ao.atomic(self.directory() / 'state.json', state)
        self.original = copy.deepcopy(state)
        self.do_refresh()
        self.assertEqual(self.state()['requests'], state['requests'])
        self.assertNotIn('outcome_resume', self.state()['requests']['spec_review'])

    def test_crash_after_intent_and_after_partial_files_reconciles_only_identical_request(self):
        self.pending()
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_routing.validate_local(self.prepared, self.state(), self.directory())
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            self.do_refresh(request_id='another-request')
        original = refresh._replace_runtime

        def interrupt(worktree, name, source, target):
            if name == '.claude/agents/pr-sonnet.md':
                raise OSError('synthetic crash between runtime files')
            return original(worktree, name, source, target)

        with patch.object(refresh, '_replace_runtime', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'between runtime'):
                self.do_refresh()
        self.assertNotEqual((self.repo / ao_routing.FILES[0]).read_bytes(), self.original_files[ao_routing.FILES[0]])
        self.assertEqual((self.repo / ao_routing.FILES[1]).read_bytes(), self.original_files[ao_routing.FILES[1]])
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_delegates.validate_preparation(self.directory(), self.state())
        self.do_refresh()
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_crash_before_state_save_preserves_intent_and_reconciles(self):
        with patch.object(self.service, 'save', side_effect=OSError('synthetic state-save crash')):
            with self.assertRaisesRegex(OSError, 'state-save crash'):
                self.do_refresh()
        self.assertEqual(self.state(), self.original)
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        raw = path.read_bytes()
        self.do_refresh()
        self.assertEqual(path.read_bytes(), raw)

    def test_partial_intent_write_failure_never_publishes_and_identical_refresh_retries(self):
        written = []

        def disk_failure(fd, raw):
            written.append(os.write(fd, raw[:31]))
            os.fsync(fd)
            raise OSError('synthetic disk failure after a real partial write')

        with patch.object(refresh, '_write_intent', side_effect=disk_failure):
            with self.assertRaisesRegex(OSError, 'real partial write'):
                self.do_refresh()
        self.assertEqual(written, [31])
        self.assertEqual(refresh._entries(self.directory()), set())
        self.assertEqual(self.state(), self.original)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, self.original_files)
        ao_delegates.validate_preparation(self.directory(), self.state())
        self.do_refresh()
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_killed_partial_intent_writer_leaves_only_ignored_staging_and_same_retry_publishes(self):
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        record = {'version': 1, 'synthetic': 'Complete bounded test publication bytes.'}
        code = '''import json, os, sys
from pathlib import Path
import ao_routing_refresh as refresh
def interrupted(fd, raw):
    os.write(fd, raw[:19])
    os.fsync(fd)
    os._exit(27)
refresh._write_intent = interrupted
refresh._publish_intent(Path(sys.argv[1]), json.loads(sys.argv[2]))
'''
        child = subprocess.run([sys.executable, '-c', code, str(path), json.dumps(record)],
            cwd=Path(refresh.__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 27, child.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(refresh._entries(self.directory()), set())
        leftovers = list((self.directory() / refresh.INTENT_STAGING).iterdir())
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(leftovers[0].read_bytes(), refresh._json(record)[:19])
        ao_delegates.validate_preparation(self.directory(), self.state())
        refresh._publish_intent(path, record)
        self.assertEqual(path.read_bytes(), refresh._json(record))
        self.assertEqual(refresh._entries(self.directory()), {refresh.BASE + '/refresh-one.json'})
        self.assertEqual(leftovers[0].read_bytes(), refresh._json(record)[:19])

    def test_intent_publication_never_overwrites_an_existing_corrupt_final(self):
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        path.parent.mkdir()
        path.write_bytes(b'{"partial":')
        with self.assertRaises(FileExistsError):
            refresh._publish_intent(path, {'version': 1, 'synthetic': 'replacement refused'})
        self.assertEqual(path.read_bytes(), b'{"partial":')
        with self.assertRaises(ValueError):
            self.do_refresh()
        self.assertEqual(path.read_bytes(), b'{"partial":')
        self.assertEqual(self.state(), self.original)

    def test_complete_intent_after_directory_sync_failure_is_synced_before_retry_mutates_runtime(self):
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        fsync = os.fsync

        def journal_fd(fd):
            return path.parent.exists() and os.fstat(fd).st_ino == path.parent.stat().st_ino

        def sync_failure(fd):
            if path.exists() and journal_fd(fd):
                raise OSError('synthetic journal directory sync failure')
            return fsync(fd)

        with patch.object(refresh.os, 'fsync', side_effect=sync_failure), \
                patch.object(refresh, '_place_guard', wraps=refresh._place_guard) as place_guard:
            with self.assertRaisesRegex(OSError, 'directory sync failure'):
                self.do_refresh()
            place_guard.assert_not_called()
        raw = path.read_bytes()
        self.assertIsInstance(json.loads(raw), dict)
        self.assertEqual(self.state(), self.original)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, self.original_files)
        synced = []
        place_guard = refresh._place_guard

        def sync_retry(fd):
            if journal_fd(fd):
                synced.append(True)
            return fsync(fd)

        def require_sync(*args):
            self.assertTrue(synced)
            return place_guard(*args)

        with patch.object(refresh.os, 'fsync', side_effect=sync_retry), \
                patch.object(refresh, '_place_guard', side_effect=require_sync):
            self.do_refresh()
        self.assertEqual(path.read_bytes(), raw)
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_intent_publication_size_bound_precedes_directory_or_file_creation(self):
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        with patch.object(refresh, 'MAX_RECORD_BYTES', 32):
            with self.assertRaisesRegex(ao.RoomError, 'bounded readable size'):
                refresh._publish_intent(path, {'synthetic': 'x' * 64})
        self.assertFalse(path.parent.exists())
        self.assertFalse((self.directory() / refresh.INTENT_STAGING).exists())

    def test_pending_state_candidate_owner_history_or_retained_receipt_drift_refuses(self):
        self.pending()
        receipt = self.directory() / self.state()['requests']['spec_review']['receipt']
        for kind in ('state', 'candidate', 'owner', 'history', 'receipt'):
            snapshot = copy.deepcopy(self.fake.snapshots['engineer'])
            receipt_raw = receipt.read_bytes()
            feature = (self.repo / 'feature.txt').read_bytes()
            try:
                if kind == 'state':
                    state = self.state(); state['refresh_drift'] = True; ao.atomic(self.directory() / 'state.json', state)
                elif kind == 'candidate': (self.repo / 'feature.txt').write_text('changed\n')
                elif kind == 'owner': self.owner_update("UPDATE sessions SET controller_generation='new-generation'")
                elif kind == 'history': self.fake.snapshots['engineer']['activities'].append({'id': 'new-activity', 'sequence': 1})
                else: receipt.write_bytes(receipt_raw + b' ')
                with self.subTest(kind=kind), self.assertRaises(ao.RoomError):
                    self.do_refresh()
            finally:
                ao.atomic(self.directory() / 'state.json', self.original)
                (self.repo / 'feature.txt').write_bytes(feature)
                self.owner_update("UPDATE sessions SET controller_generation='generation-one'")
                self.fake.snapshots['engineer'] = snapshot
                receipt.write_bytes(receipt_raw)

    def test_pending_foreign_runtime_bytes_are_never_overwritten(self):
        self.pending()
        path = self.repo / '.claude/agents/pr-sonnet.md'
        path.write_text('Foreign edit preserved\n')
        with self.assertRaisesRegex(ao.RoomError, 'neither recorded source nor target'):
            self.do_refresh()
        self.assertEqual(path.read_text(), 'Foreign edit preserved\n')
        self.assertEqual(self.state(), self.original)

    def test_exact_pending_write_prefix_can_reconcile_and_unknown_temporary_bytes_refuse(self):
        self.pending()
        record = json.loads((self.directory() / refresh.BASE / 'refresh-one.json').read_text())
        path = self.repo / '.claude/settings.local.json'
        pending = path.with_name('.pending-' + path.name)
        pending.write_bytes(b'unknown synthetic temporary bytes'); pending.chmod(0o600)
        with self.assertRaisesRegex(ao.RoomError, 'Pending routing refresh bytes'):
            self.do_refresh()
        self.assertEqual(pending.read_bytes(), b'unknown synthetic temporary bytes')
        pending.write_bytes(record['target_bundle']['files']['.claude/settings.local.json'].encode()[:17])
        self.do_refresh()
        self.assertFalse(pending.exists())

    def test_orphaned_other_intent_blocks_even_the_identical_pending_request(self):
        self.pending()
        root = self.directory() / refresh.BASE
        (root / 'unclaimed.json').write_bytes((root / 'refresh-one.json').read_bytes())
        with self.assertRaisesRegex(ao.RoomError, 'state or ancestry changed'):
            self.do_refresh()
        self.assertEqual(self.state(), self.original)

    def test_pointer_rollback_missing_record_and_source_guard_tamper_refuse(self):
        self.do_refresh()
        state = self.state(); state.pop('routing_refresh')
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            refresh.effective(self.directory(), state, self.prepared)
        pointer = self.state()['routing_refresh']
        path = self.directory() / pointer['path']; raw = path.read_bytes(); path.unlink()
        with self.assertRaises(OSError):
            refresh.effective(self.directory(), self.state(), self.prepared)
        path.write_bytes(raw)
        Path(self.prepared['routing']['guard_path']).write_text('Changed old guard')
        with self.assertRaisesRegex(ao.RoomError, 'Retained routing refresh guard'):
            refresh.effective(self.directory(), self.state(), self.prepared)

    def test_archive_tamper_and_current_runtime_tamper_refuse(self):
        self.do_refresh()
        pointer = self.state()['routing_refresh']; record = self.journal()
        record['source_bundle']['files']['.claude/agents/pr-sonnet.md'] += 'Changed archive'
        ao.atomic(self.directory() / pointer['path'], record)
        with self.assertRaisesRegex(ao.RoomError, 'Immutable routing refresh record'):
            refresh.effective(self.directory(), self.state(), self.prepared)
        state = self.state(); state['routing_refresh']['sha256'] = ao.digest(record)
        with self.assertRaisesRegex(ao.RoomError, 'Archived routing refresh file'):
            refresh.effective(self.directory(), state, self.prepared)

    def test_current_target_file_tampering_remains_blocked(self):
        self.do_refresh()
        path = self.repo / '.claude/agents/pr-opus.md'
        path.write_bytes(path.read_bytes() + b'changed runtime')
        with self.assertRaisesRegex(ao.RoomError, 'differs from its committed target'):
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_second_refresh_extends_exact_source_chain_and_preserves_first_bytes(self):
        first = self.do_refresh(); original = (self.directory() / first['path']).read_bytes()
        definition = ao_routing.agent_definition
        with patch.object(ao_routing, 'agent_definition', side_effect=lambda name: definition(name) + '\nNew bounded instruction.\n'):
            self.do_refresh(request_id='refresh-two')
        self.assertEqual((self.directory() / first['path']).read_bytes(), original)
        record = self.journal()
        self.assertEqual(record['previous'], {'path': first['path'], 'sha256': first['sha256']})
        self.assertEqual(record['source'], json.loads(original)['target'])
        self.assertEqual(record['source_bundle'], json.loads(original)['target_bundle'])
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_full_chain_refuses_before_build_or_inspection_without_changing_any_bytes(self):
        with patch.object(refresh, 'MAX_CHAIN', 1):
            self.do_refresh()
            state = (self.directory() / 'state.json').read_bytes()
            journal = {path.name: path.read_bytes() for path in (self.directory() / refresh.BASE).iterdir()}
            runtime = {name: (self.repo / name).read_bytes() for name in ao_routing.FILES}
            definition = ao_routing.agent_definition
            with patch.object(ao_routing, 'agent_definition', side_effect=lambda name: definition(name) + '\nNew target.\n'), \
                    patch.object(refresh, '_build', wraps=refresh._build) as build, \
                    patch.object(refresh, '_inspect', wraps=refresh._inspect) as inspect:
                with self.assertRaisesRegex(ao.RoomError, 'at capacity; no new intent'):
                    self.do_refresh(request_id='over-capacity')
                build.assert_not_called()
                inspect.assert_not_called()
            self.assertEqual((self.directory() / 'state.json').read_bytes(), state)
            self.assertEqual({path.name: path.read_bytes() for path in (self.directory() / refresh.BASE).iterdir()}, journal)
            self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, runtime)
            ao_delegates.validate_preparation(self.directory(), self.state())

    def test_unsafe_symlink_and_tracked_runtime_files_refuse_before_intent(self):
        path = self.repo / '.claude/agents/pr-sonnet.md'
        outside = self.root / 'outside-agent'; outside.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(outside)
        with self.assertRaises((ao.RoomError, ValueError)):
            self.do_refresh()
        path.unlink(); path.write_bytes(self.original_files['.claude/agents/pr-sonnet.md'])
        subprocess.run(['git', '-C', str(self.repo), 'add', '-f', '.claude/agents/pr-sonnet.md'], check=True, capture_output=True)
        with self.assertRaisesRegex(ao.RoomError, 'tracked'):
            self.do_refresh()
        self.assertFalse((self.directory() / refresh.BASE).exists())

    def test_rebound_executable_is_checked_against_original_preparation(self):
        replacement = self.root / 'replacement-claude'; replacement.write_bytes(self.cli.read_bytes()); replacement.chmod(0o700)
        launch = self.root / 'claude-launch'; launch.symlink_to(replacement)
        self.cli.unlink()
        ao_executable_binding.bind(self.service, self.room, 'executable-repair', str(replacement), str(launch),
            str(self.database), 'Authorized synthetic executable repair', 'Synthetic original binary removed')
        before = self.state()
        self.do_refresh()
        self.assertEqual(self.state()['executable_binding'], before['executable_binding'])
        ao_delegates.validate_preparation(self.directory(), self.state())
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), self.prepared_bytes)

    def test_executable_repair_after_refresh_preserves_both_journals_and_original_preparation(self):
        self.do_refresh()
        before = self.state()
        routing = refresh.effective(self.directory(), before, self.prepared)
        journal_path = self.directory() / before['routing_refresh']['path']
        journal_raw = journal_path.read_bytes()
        history = copy.deepcopy(self.fake.snapshots['engineer'])
        replacement = self.root / 'replacement-claude'; replacement.write_bytes(self.cli.read_bytes()); replacement.chmod(0o700)
        launch = self.root / 'claude-launch'; launch.symlink_to(replacement)
        self.cli.unlink()
        result = ao_executable_binding.bind(self.service, self.room, 'repair-after-refresh', str(replacement), str(launch),
            str(self.database), 'Authorized synthetic executable repair', 'Synthetic original binary removed after refresh')
        current = self.state()
        self.assertFalse(result['model_dispatch'])
        self.assertEqual({key: value for key, value in current.items() if key != 'executable_binding'}, before)
        self.assertEqual(journal_path.read_bytes(), journal_raw)
        self.assertEqual((self.directory() / current['preparation']).read_bytes(), self.prepared_bytes)
        self.assertEqual(ao_delegates.validate_preparation(self.directory(), current), self.prepared)
        self.assertEqual(refresh.effective(self.directory(), current, self.prepared), routing)
        self.assertEqual(ao_executable_binding.effective(self.directory(), current, self.prepared)['path'], str(replacement))
        self.assertEqual(self.fake.snapshots['engineer'], history)
        self.assertEqual(ao.candidate_snapshot(self.repo), self.original_candidate)
        self.assertEqual(self.fake.posts, self.posts)

    def test_pending_routing_intent_blocks_new_executable_repair(self):
        self.pending()
        journal_path = self.directory() / refresh.BASE / 'refresh-one.json'
        journal_raw = journal_path.read_bytes()
        replacement = self.root / 'replacement-claude'; replacement.write_bytes(self.cli.read_bytes()); replacement.chmod(0o700)
        launch = self.root / 'claude-launch'; launch.symlink_to(replacement)
        self.cli.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_executable_binding.bind(self.service, self.room, 'repair-during-refresh', str(replacement), str(launch),
                str(self.database), 'Authorized synthetic executable repair', 'Synthetic original binary removed during refresh')
        self.assertEqual(self.state(), self.original)
        self.assertEqual(journal_path.read_bytes(), journal_raw)
        self.assertFalse((self.directory() / ao_executable_binding.BASE).exists())
        self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, self.original_files)
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), self.prepared_bytes)
        self.assertEqual(ao.candidate_snapshot(self.repo), self.original_candidate)
        self.assertEqual(self.fake.posts, self.posts)


class AcceptedFourthReviewRefreshTests(ReviewExtensionFixture):
    def configure_runtime(self):
        cli = self.root / 'fake-refresh-claude'
        cli.write_text('#!' + sys.executable + '\nprint("synthetic (Claude Code)")\n'); cli.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(cli), 'claude_config_dir': str(self.claude_env)})

    def bind(self):
        original = Path.read_bytes
        source = Path(ao_routing_guard.__file__)
        with patch.object(Path, 'read_bytes', lambda path: OLD_GUARD if path == source else original(path)):
            return super().bind()

    def accepted_fourth(self):
        self.grant()
        self.send('spec_review', 'fourth-charter')
        self.finish_fourth()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.before = self.state()
        self.prepared = ao_delegates.preparation(self.directory(), self.before)
        self.original_preparation = (self.directory() / self.before['preparation']).read_bytes()
        self.posts = copy.deepcopy(self.fake.posts)

    def refresh(self):
        return refresh.refresh(self.service, self.room, 'consumed-fourth-refresh', str(self.database),
            'synthetic-native-owner', 'User authorizes the retained routing correction.',
            'The quota guard predates the accepted fourth review.')

    def assert_preserved(self):
        import ao_review_extension
        self.assertEqual({key: value for key, value in self.state().items() if key != 'routing_refresh'}, self.before)
        self.assertEqual((self.directory() / self.before['preparation']).read_bytes(), self.original_preparation)
        self.assertEqual(self.fake.posts, self.posts)
        result = ao_review_extension.validate(self.service, self.state())
        self.assertIsNotNone(result)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.service.ao_room_status(self.room)['agreement']['request_id'], 'fourth-charter')
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_refresh_before_grant_belongs_to_its_baseline_and_does_not_deadlock_fourth_review(self):
        import ao_review_extension
        self.assertNotIn('spec_review_extension', self.state())
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        result = refresh.refresh(self.service, self.room, 'before-fourth-grant', str(self.database),
            'synthetic-native-owner', 'User authorizes this existing routing update.',
            'Refresh before any fourth-review grant exists.')
        pointer = self.state()['routing_refresh']
        journal = self.directory() / result['path']; original = journal.read_bytes()
        self.assertIsNone(refresh._read(self.directory(), pointer)['evidence']['review_extension'])
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.grant()
        _, evidence = ao_review_extension.validate(self.service, self.state())
        # _retained stops at this exact baseline pointer. A pre-grant refresh is
        # never mistaken for a later change that needs consumed-grant proof.
        self.assertEqual(evidence['state']['routing_refresh'], pointer)
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        self.send('spec_review', 'fourth-after-refresh')
        self.finish_fourth()
        self.assertIsNotNone(ao_review_extension.validate(self.service, self.state()))
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['agreement']['request_id'], 'fourth-after-refresh')
        self.assertEqual(journal.read_bytes(), original)
        with self.assertRaises(ao.RoomError):
            self.send('spec_review', 'fifth-after-refresh')

    def test_completed_accepted_fourth_review_can_refresh_without_new_allowance(self):
        self.accepted_fourth()
        self.refresh()
        self.assert_preserved()
        record = refresh._read(self.directory(), self.state()['routing_refresh'])
        self.assertEqual(record['evidence']['review_extension']['consumed_by'], 'fourth-charter')
        self.assertEqual(record['evidence']['review_extension']['agreement']['request_id'], 'fourth-charter')

    def test_exact_pending_refresh_reconciles_through_consumed_grant_ancestry(self):
        self.accepted_fourth()
        with patch.object(self.service, 'save', side_effect=OSError('synthetic consumed-grant crash')):
            with self.assertRaisesRegex(OSError, 'consumed-grant crash'):
                self.refresh()
        self.assertEqual(self.state(), self.before)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_delegates.validate_preparation(self.directory(), self.state())
        self.refresh()
        self.assert_preserved()
