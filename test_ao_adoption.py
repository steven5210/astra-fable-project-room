"""Offline provider/routing adoption against real frozen v1 and synthetic MCP.

No real account, model request, network endpoint, or saved room is used.
"""
import copy
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_provider_transition as provider
import ao_routing
import ao_routing_adoption as routing
import ao_workflow
import deepseek_adapter
from implementation import candidate_snapshot
from test_ao_normal import DelegateFixture
import test_ao_routing_v1_fixture as frozen


class AdoptionFixture(DelegateFixture):
    install_frozen_routing = frozen.FrozenV1RoutingTests.install_frozen_routing

    def open(self, feature='normal', provider='none'):
        if provider == 'deepseek':
            (self.root / 'provider.json').write_text(json.dumps({
                'backend': 'deepinfra', 'model': 'deepseek-ai/DeepSeek-V4.1-Flash',
                'api_key_file': str(self.home / 'secrets' / 'synthetic-deepinfra-key')}))
        return super().open(feature, provider)

    def setUp(self):
        super().setUp()
        self.fixture = json.loads((frozen.FIXTURES / 'fixture.json').read_text())
        self.claude_env = self.claude_config
        self.fake.config['agentRules'] += '\nUse only DeepInfra; no official DeepSeek.'
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for s in self.fake.snapshots.values():
            s['controller'] = 'ready'
            s['branchMaterialization'] = {'strategy': 'native', 'replayTruncated': False}
        original_request = self.fake.request
        def request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                self.fake.gets += 1
                return {**copy.deepcopy(self.fake.snapshots[path.split('/')[2]]), 'hasMoreBefore': False}
            return original_request(method, path, payload)
        self.fake.request = request
        with patch.object(ao_routing, 'prepare', self.install_frozen_routing):
            self.bind()
        self.agree(decision='changes_required', key='charter-1')
        self.agree(decision='changes_required', key='charter-2')
        self.agree(key='charter-3')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.key = self.home / 'secrets' / 'synthetic-official-key'
        self.key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key.write_text('synthetic-no-account-key')
        self.key.chmod(0o600)
        self.target = {**provider.TARGET, 'api_key_file': str(self.key)}
        self.ledger = deepseek_adapter.Ledger(self.home)
        self.original = copy.deepcopy(self.state())
        self.original_bytes = {str(p): p.read_bytes() for p in self.directory().rglob('*') if p.is_file()}
        self.candidate_before = candidate_snapshot(self.repo)
        self.posts_before = len(self.fake.posts)

    def provider_commit(self):
        audit = provider.audit(self.service, self.room, self.target)
        return provider.transition(self.service, self.room, audit['audit_sha256'],
                                   'Definite pre-generation rejection; user selected official transport.',
                                   'User authorized official provider switch preserving existing review continuity.',
                                   'provider-change', 'Operator recorded positively stopped native controller.')

    def stage_routing(self):
        self.provider_commit()
        audit = routing.audit(self.service, self.room)
        self.stage_args = (self.service, self.room, audit['audit_sha256'],
                           'User authorizes v2 orchestration and official-provider operating instructions.',
                           'Historical v1 routing and DeepInfra-only rules prevent safe continuation.', 'routing-change')
        return routing.stage(*self.stage_args)

    def configure_routing(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        routing.activate(self.service, self.room, 'routing-change')
        return ao_delegates.preparation(self.directory(), self.state())

    def start_attachment(self, prepared):
        command = [prepared['registration']['command'], *prepared['registration']['args']]
        env = {**os.environ, **prepared['server']['env'], 'AO_SESSION_ID': 'engineer'}
        process = subprocess.Popen(command, cwd=self.repo, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(cleanup)
        def send(value):
            process.stdin.write(json.dumps(value).encode() + b'\n')
            process.stdin.flush()
        def receive():
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), 'Synthetic MCP handshake timed out')
            raw = process.stdout.readline()
            if not raw:
                process.wait(timeout=5)
                self.fail('Synthetic attachment exited: ' + process.stderr.read().decode())
            return json.loads(raw)
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'synthetic-test', 'version': '1'}}})
        self.assertIn('result', receive())
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        self.assertEqual({t['name'] for t in receive()['result']['tools']}, set(routing.TOOLS))
        import ao_mcp_attachment
        deadline = time.monotonic() + 5
        while True:
            try:
                ao_mcp_attachment.validate_attachment(self.directory(), self.state(), prepared)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        return process


class DispatchObservationTests(AdoptionFixture):
    def setUp(self):
        super().setUp()
        self.prepared = self.configure_routing()
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.attachment = self.start_attachment(self.prepared)
        # Exercise the real ordinary normalizer over synthetic raw transport.
        # The transitioned path must select strict observation instead of
        # inheriting its permissive missing-field and deduplication defaults.
        self.fake.conversation = lambda session_id: ao.Client.conversation(self.fake, session_id)

    def assert_dispatch_refused(self, pattern):
        before = (self.directory() / 'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, pattern):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'unproven-dispatch', purpose='implementation')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)
        self.assertNotIn('unproven-dispatch', self.state()['requests'])
        self.assertEqual(sum(r.get('purpose') == 'spec_review' for r in self.state()['requests'].values()), 3)
        self.assertEqual(candidate_snapshot(self.repo), self.candidate_before)
        self.assertIsNone(self.attachment.poll(), 'The initialized synthetic attachment must remain alive')

    def test_missing_raw_completeness_refuses_before_request_or_post(self):
        original = self.fake.request
        def incomplete(method, path, payload=None):
            value = original(method, path, payload)
            if method == 'GET' and path.startswith('/sessions/engineer/conversation?'):
                value.pop('hasMoreBefore')
            return value
        self.fake.request = incomplete
        self.assert_dispatch_refused('explicit complete native history')

    def test_contradictory_raw_truncation_refuses_before_request_or_post(self):
        self.fake.snapshots['engineer']['history_truncated'] = True
        self.assert_dispatch_refused('contradictory truncation')

    def test_conflicting_raw_turn_duplicate_refuses_before_request_or_post(self):
        turns = self.fake.snapshots['engineer']['turns']
        turns.insert(0, {**turns[0], 'providerTurnId': 'contradictory-native-turn'})
        self.assert_dispatch_refused('conflicting turns')

    def test_stopped_controller_with_live_initialized_attachment_cannot_send(self):
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.assert_dispatch_refused('ready native controller')

    def test_unknown_or_terminated_native_lifecycle_cannot_send(self):
        session = self.fake.sessions['engineer']
        session.pop('isTerminated')
        self.assert_dispatch_refused('isTerminated=false')
        session['isTerminated'] = True
        self.assert_dispatch_refused('isTerminated=false')

    def test_complete_ready_observation_allows_exactly_one_new_request(self):
        before = len(self.fake.posts)
        result = self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'proven-dispatch', purpose='implementation')
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), before + 1)
        self.assertEqual(self.state()['requests']['proven-dispatch']['provider_epoch'], 2)
        self.assertIsNone(self.attachment.poll())


class ProviderAdoptionTests(AdoptionFixture):
    def test_audit_and_commit_preserve_history_source_counters_and_old_bytes(self):
        raw = (self.directory() / 'state.json').read_bytes()
        audit = provider.audit(self.service, self.room, self.target)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), raw)
        self.assertTrue(audit['eligible'])
        self.provider_commit()
        state = self.state()
        for name in ('room_id', 'bindings', 'spec', 'spec_record_sha256', 'requests', 'acceptances', 'verifications', 'handoff', 'handoff_sha256'):
            self.assertEqual(state[name], self.original[name], name)
        for path, old in self.original_bytes.items():
            if path != str(self.directory() / 'state.json'):
                self.assertEqual(Path(path).read_bytes(), old, path)
        self.assertEqual(candidate_snapshot(self.repo), self.candidate_before)
        self.assertEqual(len(self.fake.posts), self.posts_before)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 3)
        active = ao_delegates.validate_provider(self.directory(), state)['delegate_settings']
        self.assertEqual((active['backend'], active['model'], active['max_tokens']), ('official', 'deepseek-flash', 393216))

    def test_provider_audit_refuses_live_native_or_incorrect_target(self):
        self.fake.snapshots['engineer']['turns'][0]['state'] = 'running'
        with self.assertRaises(ao.RoomError):
            provider.audit(self.service, self.room, self.target)
        self.fake.snapshots['engineer']['turns'][0]['state'] = 'completed'
        for key, value in [('model', 'wrong'), ('max_tokens', 131072), ('reasoning_effort', 'low'), ('context_tokens', 999999)]:
            with self.subTest(key=key), self.assertRaises(ao.RoomError):
                provider.audit(self.service, self.room, {**self.target, key: value})
        self.assertEqual(self.state(), self.original)

    def test_provider_config_commit_does_not_authorize_dispatch_before_attachment(self):
        self.provider_commit()
        self.fake.snapshots['engineer']['controller'] = 'ready'
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'too-early', purpose='implementation')
        self.assertEqual(len(self.fake.posts), self.posts_before)


class RoutingAdoptionTests(AdoptionFixture):
    def test_stage_checks_configured_state_before_writing_its_first_intent(self):
        self.provider_commit()
        audit = routing.audit(self.service, self.room)
        before = (self.directory() / 'state.json').read_bytes()
        check = routing.launcher_compatibility
        final_checks = []
        def at_boundary(service, state, prepared, reserve_bytes=0):
            if state.get('routing_adoption', {}).get('phase') == 'configured':
                final_checks.append(copy.deepcopy(state))
                # Emulate an otherwise valid room with no room for the final
                # routing observation. Admission must stop before any intent.
                size = len((json.dumps(state, ensure_ascii=False, sort_keys=True) + '\n').encode())
                with patch.object(routing, 'LEGACY_LAUNCH_LIMIT', size + reserve_bytes - 1):
                    return check(service, state, prepared, reserve_bytes)
            return check(service, state, prepared, reserve_bytes)
        with patch.object(routing, 'launcher_compatibility', side_effect=at_boundary):
            with self.assertRaisesRegex(ao.RoomError, 'launcher compatibility'):
                routing.stage(self.service, self.room, audit['audit_sha256'], 'Authorized adoption', 'Historical v1 guard', 'size-boundary')
        self.assertEqual(len(final_checks), 1)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertFalse((self.directory() / routing.BASE / 'requests').exists())

    def test_activation_size_check_covers_exact_saved_configured_state(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        checked = []
        check = routing.launcher_compatibility
        def observe(service, state, prepared, reserve_bytes=0):
            checked.append(json.dumps(state, ensure_ascii=False, sort_keys=True) + '\n')
            return check(service, state, prepared, reserve_bytes)
        with patch.object(routing, 'launcher_compatibility', side_effect=observe):
            routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(checked[-1].encode(), (self.directory() / 'state.json').read_bytes())
        self.assertEqual(json.loads(checked[-1])['routing_adoption']['phase'], 'configured')
        self.assertEqual(json.loads(checked[-1])['routing_rules']['source'], 'routing-adoption')

    def test_target_provider_jobs_refuse_audit_even_after_definite_rejection(self):
        self.provider_commit()
        before = copy.deepcopy(self.state())
        profile = ao_delegates.expected_profile(before['delegate']['inventory'])
        job = 'e' * 32
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,reasoning_effort,max_tokens,input_bytes,possibly_billed,reserved_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job, self.room, 'synthetic-early-target', 'deep', 'a' * 64, profile, deepseek_adapter.REJECTED,
                 '2026-01-01T00:00:00+00:00', 'deepseek-flash', 'enabled', 'max', 393216, 10, 0, 1))
        for outcome in (deepseek_adapter.REJECTED, deepseek_adapter.COMPLETED):
            with self.ledger.transaction() as db:
                db.execute('UPDATE jobs SET state=? WHERE id=?', (outcome, job))
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ao.RoomError, 'target-provider ledger job'):
                routing.audit(self.service, self.room)
        self.assertEqual(self.state(), before)
        self.assertFalse((self.directory() / routing.BASE / 'requests').exists())
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_paid_probe_outside_ledger_and_incomplete_probe_evidence_refuse(self):
        self.provider_commit()
        probes = self.home / 'deepseek/probes'
        probes.mkdir(mode=0o700)
        name = '20260101T000000Z-' + 'e' * 32
        (probes / name).mkdir(mode=0o700)
        with self.assertRaisesRegex(ao.RoomError, 'incomplete probe'):
            routing.audit(self.service, self.room)
        receipt = {'kind': 'deepseek_probe', 'room_id': 'other-room', 'artifacts': name,
                   'backend': 'official', 'requested': {'backend': 'official', 'model': 'deepseek-flash'},
                   'state': deepseek_adapter.REJECTED}
        ao.atomic(probes / (name + '.json'), receipt)
        self.assertTrue(routing.audit(self.service, self.room)['eligible'])
        receipt['room_id'] = self.room
        ao.atomic(probes / (name + '.json'), receipt)
        with self.assertRaisesRegex(ao.RoomError, 'target-provider probe'):
            routing.audit(self.service, self.room)
        self.assertNotIn('routing_adoption', self.state())

    def test_legacy_launcher_oversized_neighbor_refuses_before_provider_commit(self):
        other = self.service.root / 'rooms/oversized-neighbor/state.json'
        other.parent.mkdir(mode=0o700)
        ao.atomic(other, {'room_id': 'neighbor', 'history': 'x' * routing.LEGACY_LAUNCH_LIMIT})
        with self.assertRaisesRegex(ao.RoomError, 'launcher compatibility'):
            self.provider_commit()
        self.assertEqual(self.state(), self.original)
        self.assertFalse((self.directory() / provider.BASE / 'requests').exists())

    def test_derived_audit_above_legacy_limit_roundtrips_and_bound_refuses_before_write(self):
        # An audit embeds several kinds of evidence; unlike launch inputs its
        # combined size may exceed the unchanged launcher's individual limit.
        evidence = {'derived': 'x' * (routing.LEGACY_LAUNCH_LIMIT + 1)}
        with patch.object(routing, '_inspect', return_value=evidence):
            result = routing.audit(self.service, self.room)
        self.assertEqual(routing._audit(self.directory(), result['audit_sha256'])['evidence'], evidence)
        previous = list((self.directory() / routing.BASE / 'audits').iterdir())
        with patch.object(routing, '_inspect', return_value=evidence), patch.object(routing, 'MAX_EVIDENCE', 1024):
            with self.assertRaisesRegex(ao.RoomError, 'no audit was written'):
                routing.audit(self.service, self.room)
        self.assertEqual(list((self.directory() / routing.BASE / 'audits').iterdir()), previous)

    def test_overlay_refuses_prior_or_intervening_epoch2_native_launch(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        launch = self.directory() / 'delegate-launch.json'
        launch.write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'launch occurred'):
            routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')

    def test_activation_refuses_modified_provider_and_original_guard_evidence(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        original_prepared = ao.read(self.directory() / self.original['preparation'])
        paths = [self.directory() / self.original['preparation'],
                 self.directory() / self.original['handoff'],
                 Path(original_prepared['routing']['guard_path']),
                 *map(Path, self.original['delegate']['inventory']['files'])]
        for path in paths:
            previous = path.read_bytes()
            try:
                path.write_bytes(previous + b'\ncorrupt\n')
                with self.subTest(path=path.name), self.assertRaises(ao.RoomError):
                    routing.activate(self.service, self.room, 'routing-change')
                self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')
            finally:
                path.write_bytes(previous)

    def test_abandoned_and_changed_ledger_or_lost_completed_content_refuse(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        job = 'd' * 32
        current = self.state()
        profile = ao_delegates.expected_profile(self.original['delegate']['inventory'])
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,reasoning_effort,max_tokens,input_bytes,possibly_billed,reserved_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job, self.room, 'synthetic-new-job', 'deep', 'a' * 64, profile, 'abandoned_cancelled', '2026-01-01T00:00:00+00:00', 'deepseek-ai/DeepSeek-V4.1-Flash', 'enabled', 'max', 131072, 10, 1, 1))
        with self.assertRaisesRegex(ao.RoomError, 'Abandoned'):
            routing.activate(self.service, self.room, 'routing-change')
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET state=? WHERE id=?', (deepseek_adapter.REJECTED, job))
        with self.assertRaisesRegex(ao.RoomError, 'ledger changed'):
            routing.activate(self.service, self.room, 'routing-change')
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET state=?,content_sha256=? WHERE id=?', (deepseek_adapter.COMPLETED, 'f' * 64, job))
        with self.assertRaisesRegex(ao.RoomError, 'no verifiable content'):
            routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')

    def test_configured_adoption_refuses_extra_unclaimed_intent(self):
        self.configure_routing()
        (self.directory() / routing.BASE / 'requests/unclaimed.json').write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            routing.activate(self.service, self.room, 'routing-change')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'orphan-after-configured', purpose='implementation')
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_missing_ledger_and_shared_project_refuse_before_staging(self):
        self.provider_commit()
        ledger = self.home / 'deepseek/ledger.sqlite3'
        previous = ledger.read_bytes()
        ledger.unlink()
        with self.assertRaises(ao.RoomError):
            routing.audit(self.service, self.room)
        self.assertFalse(ledger.exists(), 'The audit must never reconstruct history')
        ledger.write_bytes(previous)
        other = self.service.root / 'rooms' / 'other' / 'state.json'
        other.parent.mkdir(mode=0o700)
        other.write_text(json.dumps({'room_id': 'other', 'ao_project_id': self.state()['ao_project_id']}))
        with self.assertRaisesRegex(ao.RoomError, 'Another room'):
            routing.audit(self.service, self.room)
        self.assertNotIn('routing_adoption', self.state())

    def test_reconcile_same_intent_after_crash_before_state_pointer(self):
        self.provider_commit()
        audit = routing.audit(self.service, self.room)
        args = (self.service, self.room, audit['audit_sha256'], 'Authorized exact adoption', 'Retained legacy routing', 'crash-before-pointer')
        before = copy.deepcopy(self.state())
        with patch.object(self.service, 'save', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                routing.stage(*args)
        self.assertEqual(self.state(), before)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'orphan-blocked', purpose='implementation')
        changed = list(args)
        changed[3] += ' changed'
        with self.assertRaises(ao.RoomError):
            routing.stage(*changed)
        result = routing.stage(*args)
        self.assertEqual(result['phase'], 'pending')
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        routing.activate(self.service, self.room, args[-1])
        self.assertEqual(self.state()['routing_adoption']['phase'], 'configured')
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_reconcile_partial_runtime_writes_then_lost_activation_ack(self):
        with patch.object(ao_routing, '_write', side_effect=OSError('synthetic runtime interruption')):
            with self.assertRaises(OSError):
                self.stage_routing()
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')
        with self.assertRaisesRegex(ao.RoomError, 'pending'):
            self.service.ao_room_sync(self.room)
        result = routing.stage(*self.stage_args)
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        with patch.object(self.service, 'save', side_effect=OSError('synthetic crash before activation commit')):
            with self.assertRaises(OSError):
                routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')
        activated = routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(activated, routing.activate(self.service, self.room, 'routing-change'))
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_complete_history_required_and_changed_native_bytes_refuse(self):
        self.provider_commit()
        request = self.fake.request
        def missing(method, path, payload=None):
            result = request(method, path, payload)
            if method == 'GET' and '/conversation?' in path:
                result.pop('hasMoreBefore')
            return result
        with patch.object(self.fake, 'request', side_effect=missing), self.assertRaises(ao.RoomError):
            routing.audit(self.service, self.room)
        audit = routing.audit(self.service, self.room)
        args = (self.service, self.room, audit['audit_sha256'], 'Authorized exact adoption', 'Retained legacy routing', 'history-drift')
        result = routing.stage(*args)
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        self.fake.snapshots['engineer']['messages'][-1]['text'] += ' changed after audit'
        with self.assertRaisesRegex(ao.RoomError, 'history changed'):
            routing.activate(self.service, self.room, 'history-drift')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')

    def test_stage_blocks_new_work_and_requires_exact_external_project_update(self):
        result = self.stage_routing()
        self.assertEqual(result['phase'], 'pending')
        self.assertNotIn(routing.INSTRUCTION, self.fake.config['agentRules'])
        with self.assertRaisesRegex(ao.RoomError, 'pending'):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'too-early', purpose='implementation')
        with self.assertRaisesRegex(ao.RoomError, 'configuration'):
            routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(routing.stage(*self.stage_args), result)
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_v2_adoption_archives_old_guard_and_changes_only_runtime_configuration(self):
        prepared = self.configure_routing()
        self.assertEqual(prepared['routing']['version'], 2)
        self.assertEqual(prepared['routing']['execution_policy'], 'orchestrator')
        self.assertEqual(prepared['routing']['matcher'], '.*')
        self.assertIn(routing.INSTRUCTION, self.fake.config['agentRules'])
        self.assertEqual(candidate_snapshot(self.repo), self.candidate_before)
        self.assertEqual(self.state()['requests'], self.original['requests'])
        self.assertEqual(self.state()['bindings'], self.original['bindings'])
        original = ao.read(self.directory() / 'preparation.json')
        self.assertEqual(original['routing']['version'], 1)
        original_settings = self.directory() / routing.BASE / 'original-runtime/.claude/settings.local.json'
        self.assertEqual(ao.digest(original_settings.read_bytes()), original['routing']['files']['.claude/settings.local.json'])
        decision = subprocess.run([prepared['routing']['python'], prepared['routing']['guard_path']],
            input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'synthetic-never-executed'}}),
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(decision.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_runtime_drift_and_new_native_activity_refuse_activation(self):
        result = self.stage_routing()
        self.fake.config = json.loads(Path(result['project_config_payload']).read_text())['config']
        self.fake.snapshots['engineer']['controller'] = 'ready'
        with self.assertRaisesRegex(ao.RoomError, 'Stop'):
            routing.activate(self.service, self.room, 'routing-change')
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        (self.repo / '.claude/settings.local.json').write_text('{}')
        with self.assertRaises(ao.RoomError):
            routing.activate(self.service, self.room, 'routing-change')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'pending')

    def test_active_record_corruption_and_changed_request_ids_refuse(self):
        prepared = self.configure_routing()
        self.assertEqual(routing.activate(self.service, self.room, 'routing-change')['phase'], 'configured')
        changed = list(self.stage_args)
        changed[-1] = 'different'
        with self.assertRaises(ao.RoomError):
            routing.stage(*changed)
        (self.directory() / routing.REL / 'project-config.json').write_text('{}')
        with self.assertRaises(ao.RoomError):
            provider.validate(self.service, self.state())

    def test_real_synthetic_mcp_handshake_unlocks_one_amendment_then_only_continue(self):
        prepared = self.configure_routing()
        self.fake.snapshots['engineer']['controller'] = 'ready'
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'before-handshake', purpose='implementation')
        self.start_attachment(prepared)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'after-handshake', purpose='implementation')
        sent = self.fake.posts[-1][1]['text']
        self.assertIn(routing.INSTRUCTION, sent)
        self.assertNotIn('<specification>', sent)
        self.assertTrue(sent.endswith('Continue.'))
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required', implementation_complete=False)))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'next', purpose='correction')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 3)


if __name__ == '__main__':
    unittest.main()
