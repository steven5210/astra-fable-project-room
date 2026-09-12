"""Synthetic one-time provider amendment regressions; no real credentials, endpoints or native model calls."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_provider_transition as transition
import ao_reviewer_recovery as recovery
import ao_routing
import ao_delegates
import ao_delegate_launcher as launcher
import ao_workflow
import deepseek_adapter
import project_room
import project_room_mcp
from test_ao_normal import DelegateFixture
from test_ao_adoption import AdoptionFixture
import test_ao_routing_v1_fixture as frozen


class CompleteHistoryPaginationTests(unittest.TestCase):
    class RawPages:
        def __init__(self, pages):
            self.pages = copy.deepcopy(pages)
            self.calls = []

        def request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            # Fresh JSON objects for the actual raw GET/pagination path, with no conversation normalizer mock.
            return json.loads(json.dumps(self.pages[len(self.calls) - 1]))

    def pages(self):
        identity = {'sessionId': 'engineer', 'conversationId': 'retained-native', 'activeBranchId': 'retained-native:root',
                    'controller': 'ready', 'settings': {'model': ao_workflow.FABLE_MODEL, 'reasoningEffort': 'max'},
                    'branchMaterialization': {'strategy': 'native', 'replayTruncated': False}}
        first = {'id': 'turn-1', 'providerTurnId': 'native-turn-1', 'state': 'completed'}
        second = {'id': 'turn-2', 'providerTurnId': 'native-turn-2', 'state': 'completed'}
        earlier = {'id': 'message-1', 'turnId': 'turn-1', 'role': 'assistant', 'text': 'Earlier completed result', 'sequence': 10}
        later = {'id': 'message-2', 'turnId': 'turn-2', 'role': 'assistant', 'text': 'Latest completed result', 'sequence': 30}
        return [{**copy.deepcopy(identity), 'turns': [second], 'messages': [later], 'hasMoreBefore': True, 'oldestSequence': 30},
                {**copy.deepcopy(identity), 'turns': [first, copy.deepcopy(second)], 'messages': [earlier, copy.deepcopy(later)],
                 'hasMoreBefore': False, 'oldestSequence': 10}]

    def test_identical_cross_page_overlap_is_retained_once(self):
        pages = self.pages()
        # JSON member order is immaterial; complete canonical object contents must agree.
        pages[1]['turns'][-1] = dict(reversed(list(pages[1]['turns'][-1].items())))
        pages[1]['messages'][-1] = dict(reversed(list(pages[1]['messages'][-1].items())))
        client = self.RawPages(pages)
        result = transition._CompleteClient(client).conversation('engineer')
        self.assertEqual({t['id']: t for t in result['turns']}, {t['id']: t for t in pages[1]['turns']})
        self.assertEqual(result['messages'], pages[1]['messages'])
        self.assertFalse(result['history_truncated'])
        self.assertEqual(client.calls, [('GET', '/sessions/engineer/conversation?limit=500', None),
                                        ('GET', '/sessions/engineer/conversation?limit=500&beforeSequence=30', None)])

    def test_conflicting_cross_page_turn_and_message_values_refuse(self):
        for collection, field, changed in (('turns', 'state', 'streaming'), ('turns', 'providerTurnId', 'different-native-turn'),
                                           ('messages', 'text', 'Changed result body'), ('messages', 'turnId', 'different-turn'),
                                           ('messages', 'sequence', 30.0)):
            with self.subTest(collection=collection, field=field):
                pages = self.pages()
                pages[1][collection][-1][field] = changed
                client = self.RawPages(pages)
                with self.assertRaisesRegex(ao.RoomError, 'conflicting ' + collection + ' across pages'):
                    transition._CompleteClient(client).conversation('engineer')
                self.assertEqual(len(client.calls), 2)

    def test_native_identity_and_runtime_metadata_must_stay_identical_across_pages(self):
        changes = [('sessionId', 'different-session'), ('conversationId', 'different-conversation'),
                   ('activeBranchId', 'different-branch'), ('controller', 'stopped'),
                   ('settings', {'model': 'different-model', 'reasoningEffort': 'max'}),
                   ('settings', {'model': ao_workflow.FABLE_MODEL, 'reasoningEffort': 'low'}),
                   ('branchMaterialization', {'strategy': 'replay', 'replayTruncated': False}),
                   ('branchMaterialization', {'strategy': 'native', 'replayTruncated': True}),
                   ('controller', None), ('settings', None), ('branchMaterialization', None)]
        for field, changed in changes:
            with self.subTest(field=field, changed=changed):
                pages = self.pages()
                if changed is None:
                    pages[1].pop(field)
                else:
                    pages[1][field] = changed
                client = self.RawPages(pages)
                with self.assertRaisesRegex(ao.RoomError, 'identity changed during bounded history observation'):
                    transition._CompleteClient(client).conversation('engineer')
                self.assertEqual(len(client.calls), 2)


class ProviderTransitionTests(DelegateFixture):
    routing_kind = 'v1'
    install_frozen_routing = frozen.FrozenV1RoutingTests.install_frozen_routing

    def setUp(self):
        with patch.object(project_room.Service, '_deepseek_config',
                          lambda controller, path: deepseek_adapter.validate_config({'backend': 'deepinfra'}, controller.home)):
            super().setUp()
        # A retained adapter may lack read-only Ledger construction while its pure validator remains unchanged.
        # This is synthetic fixture construction before any preparation or native binding exists.
        state = self.state()
        self.adapter = Path(next(p for p in state['delegate']['inventory']['files'] if p.endswith('.py')))
        old = self.adapter.read_bytes()
        retained = old.replace(b'def __init__(self, home, initialize=True):', b'def __init__(self, home):\n        initialize = True')
        self.assertNotEqual(old, retained)
        self.pin_adapter(retained)
        original_request = self.fake.request

        def raw_request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                result = self.fake.conversation(path.split('/')[2])
                result['hasMoreBefore'] = False
                for field in self.missing_raw_fields:
                    result.pop(field, None)
                return result
            return original_request(method, path, payload)

        self.missing_raw_fields = set()
        self.fake.request = raw_request
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native'})
        self.fixture = json.loads((frozen.FIXTURES / 'fixture.json').read_text())
        self.claude_env = self.claude_config
        if self.routing_kind == 'v1':
            with patch.object(ao_routing, 'prepare', self.install_frozen_routing):
                self.bind()
        elif self.routing_kind is None:
            with patch.object(ao_routing, 'prepare', return_value=None):
                self.bind()
        else:
            self.bind()  # Real current v2 preparation, never a relabeled frozen v1 fixture.
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.ledger = deepseek_adapter.Ledger(self.home)
        self.add_job(1)
        self.key = self.home / 'secrets' / 'synthetic-official-key'
        self.key.parent.mkdir(mode=0o700, exist_ok=True)
        self.key.write_text('synthetic-fake-token\n')
        self.key.chmod(0o600)
        self.target = {**transition.TARGET, 'api_key_file': str(self.key)}
        no_secret = patch.object(deepseek_adapter, 'read_api_key', side_effect=AssertionError('Unexpected credential read'))
        no_secret.start()
        self.addCleanup(no_secret.stop)

    def pin_adapter(self, data):
        state = self.state()
        self.adapter.write_bytes(data)
        state['delegate']['inventory']['files'][str(self.adapter)] = ao.digest(data)
        settings = ao.read(self.directory() / 'settings.json')
        settings['provider_inventory'] = state['delegate']['inventory']
        ao.atomic(self.directory() / 'settings.json', settings)
        state['delegate']['files'][str(self.directory() / 'settings.json')] = ao.digest((self.directory() / 'settings.json').read_bytes())
        if state.get('preparation'):
            prepared = ao.read(self.directory() / state['preparation'])
            prepared['delegate_sha256'] = ao.digest(state['delegate'])
            ao.atomic(self.directory() / state['preparation'], prepared)
            state['preparation_sha256'] = ao.digest(prepared)
            handoff = ao.read(self.directory() / state['handoff'])
            handoff.update(preparation_sha256=state['preparation_sha256'], delegate_sha256=ao.digest(state['delegate']))
            ao.atomic(self.directory() / state['handoff'], handoff)
            state['handoff_sha256'] = ao.digest(handoff)
        self.service.save(self.directory(), state)

    def add_job(self, number, job_state=deepseek_adapter.REJECTED):
        job_id = format(number, '032x')
        profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,'
                       'requested_model,thinking,reasoning_effort,max_tokens,input_bytes,possibly_billed,reserved_bytes) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, self.room, 'source-' + str(number), 'deep', 'a' * 64, profile, job_state,
                        '2026-01-01T00:00:' + str(number).zfill(2) + '+00:00', transition.SOURCE['model'],
                        'enabled', 'max', 131072, 10, 0 if job_state == deepseek_adapter.REJECTED else 1, 1))
        return job_id

    def audit_transition(self, target=None):
        return self.service.ao_room_provider_transition_audit(self.room, self.target if target is None else target)

    def args(self, audit=None):
        return dict(room_id=self.room, audit_sha256=(audit or self.audit_transition())['audit_sha256'],
                    diagnosis='Synthetic pre-generation overload', authorization='Synthetic actual user provider decision',
                    request_id='switch-once', native_stop_record='Synthetic operator stopped the retained native session')

    def commit_transition(self, args=None):
        args = args or self.args()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        return self.service.ao_room_provider_transition(**args)

    def assert_refused_without_change(self, operation, message=None):
        before = (self.directory() / 'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaises(ao.RoomError) as raised:
            operation()
        if message:
            self.assertIn(message, str(raised.exception))
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)

    def test_provider_epoch_preserves_history_counters_old_files_and_selects_new_config(self):
        before = self.state()
        original_files = {p: Path(p).read_bytes() for p in before['delegate']['files'] | before['delegate']['inventory']['files']}
        original_files[str(self.directory() / before['preparation'])] = (self.directory() / before['preparation']).read_bytes()
        original_files[str(self.directory() / before['handoff'])] = (self.directory() / before['handoff']).read_bytes()
        snapshot = copy.deepcopy(self.fake.snapshots)
        audited = self.audit_transition()
        self.assertEqual(self.state(), before)
        self.assertFalse(audited['target']['validation']['whole_adapter_equal'])
        self.assertEqual(audited['target']['validation']['key_access_check'], 'comparison_only_ast')
        self.assertEqual(audited['target']['model'], 'deepseek-flash')
        self.assertNotIn('api_key_file', audited['target'])
        args = self.args(audited)
        result = self.commit_transition(args)
        state = self.state()
        for key in ('requests', 'bindings', 'spec', 'spec_record_sha256', 'handoff', 'handoff_sha256', 'verifications', 'acceptances'):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state.get('delegate_ledger_observed'), before.get('delegate_ledger_observed'))
        self.assertEqual(self.fake.snapshots['engineer']['turns'], snapshot['engineer']['turns'])
        self.assertEqual(self.fake.snapshots['engineer']['messages'], snapshot['engineer']['messages'])
        for path, data in original_files.items():
            self.assertEqual(Path(path).read_bytes(), data)
        transition.validate(self.service, state)
        ao_workflow.handoff_record(self.directory(), state)
        prepared = ao_delegates.validate_preparation(self.directory(), state, 'engineer')
        directory, _, server = launcher.select(self.home, self.repo, 'engineer', prepared['launcher_path'])
        self.assertEqual(directory, self.directory())
        config = Path(server['args'][server['args'].index('--config') + 1])
        self.assertEqual(json.loads(config.read_text())['model'], 'deepseek-flash')
        self.assertEqual(self.service.ao_room_provider_transition(**args), result)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['delegate']['jobs']['items'][0]['epoch'], 1)
        self.assertEqual(status['delegate']['jobs']['items'][0]['backend'], 'deepinfra')
        self.assertEqual(status['delegate']['jobs']['backend'], 'official')
        self.assertEqual(len(self.fake.posts), 1)

    def test_durable_intent_survives_crash_and_only_identical_request_reconciles(self):
        args = self.args()
        before = (self.directory() / 'state.json').read_bytes()
        with patch.object(transition, '_place', side_effect=OSError('Synthetic crash after durable intent')):
            with self.assertRaises(OSError):
                self.commit_transition(args)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.service.ao_room_status(self.room)['provider_transition']['state'], 'pending_uncommitted')
        self.assert_refused_without_change(lambda: self.service.ao_room_sync(self.room), 'uncommitted')
        self.assert_refused_without_change(lambda: self.service.ao_room_provider_transition(**{**args, 'diagnosis': 'Changed'}), 'another payload')
        result = self.commit_transition(args)
        self.assertEqual(self.service.ao_room_provider_transition(**args), result)
        self.assert_refused_without_change(lambda: self.service.ao_room_provider_transition(**{**args, 'request_id': 'different'}), 'another payload')

    def test_exact_proposed_launcher_state_is_checked_before_first_intent_or_epoch_write(self):
        import ao_routing_adoption
        arguments = self.args()
        original = self.state()
        checked = []
        check = ao_routing_adoption.launcher_compatibility

        def refuse_proposed(service, state, prepared):
            if state.get('provider_transition'):
                self.assertEqual(state['preparation'], transition.EPOCH_DIR + '/preparation.json')
                self.assertEqual(state['preparation_sha256'], ao.digest(prepared))
                self.assertEqual(ao.digest(state['delegate']), prepared['delegate_sha256'])
                self.assertEqual(state['bindings'], original['bindings'])
                self.assertEqual(state['requests'], original['requests'])
                self.assertIsNone(state['checkpoint'])
                checked.append(copy.deepcopy(state))
                raise ao.RoomError('Synthetic proposed-state retained launcher limit refusal')
            return check(service, state, prepared)

        with patch.object(ao_routing_adoption, 'launcher_compatibility', side_effect=refuse_proposed):
            self.assert_refused_without_change(lambda: self.commit_transition(arguments), 'proposed-state')
        self.assertEqual(len(checked), 1)
        self.assertFalse(transition._pending_receipts(self.directory()))
        self.assertFalse((self.directory() / transition.EPOCH_DIR).exists())

    def test_preexec_launch_does_not_qualify_native_attachment_or_dispatch(self):
        self.commit_transition()
        state = self.state()
        prepared = ao_delegates.preparation(self.directory(), state)
        launcher.record_launch(self.directory(), prepared, 'engineer')
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.assert_refused_without_change(lambda: transition.dispatch_gate(self.service, self.directory(), state,
                                                                            self.fake.snapshots['engineer']), 'startup')
        summary = self.service.ao_room_status(self.room)['provider_transition']
        self.assertEqual(summary['attachment'], 'launch_observed')
        self.assertFalse(summary['attached'])

    def test_spec_review_before_routing_adoption_refuses_without_persisting_an_epoch2_request(self):
        self.commit_transition()
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.assert_refused_without_change(lambda: self.service.ao_room_send(self.room, 'engineer', 'Review the exact charter.',
                                          'premature-spec', purpose='spec_review'), 'launch')
        self.assertNotIn('premature-spec', self.state()['requests'])
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.assertTrue(self.service.ao_room_routing_adoption_audit(self.room)['eligible'])

    def test_provider_requires_actual_historical_v1_routing_before_any_transition_intent(self):
        for kind in (None, 'v2'):
            with self.subTest(routing=kind):
                fixture = ProviderTransitionTests()
                fixture.routing_kind = kind
                try:
                    fixture.setUp()
                    prepared = ao_delegates.preparation(fixture.directory(), fixture.state())
                    self.assertEqual((prepared.get('routing') or {}).get('version'), 2 if kind == 'v2' else None)
                    fixture.assert_refused_without_change(fixture.audit_transition, 'historical v1 routing')
                    self.assertFalse((fixture.directory() / transition.BASE).exists())
                finally:
                    fixture.doCleanups()

    def test_changed_owner_bindings_require_the_existing_verified_recovery_chain(self):
        self.commit_transition()
        original = self.state()
        for role in ('engineer', 'reviewer'):
            with self.subTest(role=role):
                state = copy.deepcopy(original)
                state['bindings'][role]['session_id'] = 'unverified-replacement'
                with self.assertRaises(ao.RoomError):
                    transition.validate(self.service, state)
        state = copy.deepcopy(original)
        state['bindings']['extra'] = copy.deepcopy(state['bindings']['reviewer'])
        with self.assertRaisesRegex(ao.RoomError, 'owner bindings'):
            transition.validate(self.service, state)

    def test_historical_report_keeps_source_profile_and_cannot_be_accepted_for_new_epoch(self):
        self.implement(routing_log=[{'delegate_job_ids': [format(1, '032x')]}])
        source_request = self.state()['requests']['implementation']
        self.assertIn('engineering_record', source_request)
        self.commit_transition()
        state = self.state()
        self.assert_refused_without_change(lambda: ao_workflow.engineering_ready(self.service, self.directory(), self.state()),
                                          'current provider epoch')
        report = ao_workflow.final_json(self.directory(), source_request)
        with self.assertRaisesRegex(ao.RoomError, 'pinned provider configuration'):
            ao_delegates.verify_delegation(self.home, self.directory(), state, report)
        source_state = transition.report_state(self.directory(), state, source_request)
        evidence = ao_delegates.verify_delegation(self.home, self.directory(), source_state, report)
        self.assertEqual(evidence[0]['requested_model'], transition.SOURCE['model'])
        self.assertEqual(evidence[0]['classification'], 'non_result')

    def test_complete_ledger_refuses_old_active_unknown_and_abandoned_jobs(self):
        for number in range(2, 26):
            self.add_job(number)
        self.assertTrue(self.service.ao_room_status(self.room)['delegate']['jobs']['truncated'])
        for state in (deepseek_adapter.STREAMING, deepseek_adapter.UNKNOWN, deepseek_adapter.ABANDONED_CANCELLED,
                      deepseek_adapter.ABANDONED_DEADLINE, 'unrecognized_state'):
            with self.subTest(state=state):
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET state=? WHERE id=?', (state, format(1, '032x')))
                self.assert_refused_without_change(self.audit_transition)

    def test_missing_recorded_ledger_and_orphan_job_metadata_refuse(self):
        state = self.state()
        state['delegate_ledger_observed'] = True
        self.service.save(self.directory(), state)
        data = self.ledger.path.read_bytes()
        self.ledger.path.unlink()
        self.assert_refused_without_change(self.audit_transition, 'ledger is missing')
        self.ledger.path.write_bytes(data)
        orphan = self.home / 'deepseek' / 'jobs' / ('f' * 32)
        orphan.mkdir(parents=True)
        (orphan / 'request.json').write_text(json.dumps({'job_id': orphan.name, 'room_id': self.room}))
        self.assert_refused_without_change(self.audit_transition, 'lost a recorded job')

    def test_native_missing_raw_history_receipt_or_completed_identity_refuses(self):
        for field in ('turns', 'messages', 'hasMoreBefore'):
            with self.subTest(field=field):
                self.missing_raw_fields = {field}
                self.assert_refused_without_change(self.audit_transition, 'raw AO response')
        self.missing_raw_fields = set()
        self.fake.snapshots['engineer']['turns'][0]['state'] = 'recovered'
        self.assert_refused_without_change(self.audit_transition, 'not completed')
        self.fake.snapshots['engineer']['turns'][0]['state'] = 'completed'
        request = self.state()['requests']['spec_review']
        (self.directory() / request['receipt']).write_text('{}')
        self.assert_refused_without_change(self.audit_transition, 'receipt was modified')

    def test_either_native_session_terminated_or_unknown_lifecycle_refuses(self):
        for owner in ('engineer', 'reviewer'):
            for value in (True, None, 0, 'false'):
                with self.subTest(owner=owner, isTerminated=value):
                    session = self.fake.sessions[owner]
                    if value is None:
                        session.pop('isTerminated')
                    else:
                        session['isTerminated'] = value
                    self.assert_refused_without_change(self.audit_transition, 'isTerminated=false')
                    session['isTerminated'] = False
        self.audit_transition()

    def test_single_page_either_owner_replay_truncation_requires_absent_or_exactly_false(self):
        for owner in ('engineer', 'reviewer'):
            materialization = self.fake.snapshots[owner]['branchMaterialization']
            for value in (True, None, 0, 1, 'false', [], {}):
                with self.subTest(owner=owner, replayTruncated=value):
                    materialization['replayTruncated'] = value
                    self.assert_refused_without_change(self.audit_transition, 'replayTruncated')
            materialization.pop('replayTruncated')
            self.audit_transition()
            materialization['replayTruncated'] = False
            self.audit_transition()

    def test_profile_validation_metadata_and_fixed_max_values_refuse_changes(self):
        for field, value in (('model', 'deepseek-other'), ('max_tokens', 131072), ('reasoning_effort', 'high'),
                             ('base_url', 'https://example.invalid'), ('api_key', 'synthetic-forbidden-material')):
            with self.subTest(field=field):
                self.assert_refused_without_change(lambda: self.audit_transition({**self.target, field: value}))
        self.key.chmod(0o644)
        self.assert_refused_without_change(self.audit_transition, 'metadata')

    def test_changed_validator_is_refused_even_with_a_consistent_synthetic_pin(self):
        self.pin_adapter(self.adapter.read_bytes().replace(b'return type(value) is int and value > 0', b'return True'))
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET profile_sha256=?', (ao_delegates.expected_profile(self.state()['delegate']['inventory']),))
        self.assert_refused_without_change(self.audit_transition, 'validation contract differs')

    def test_changed_retained_key_metadata_or_reader_contract_refuses_without_secret_execution(self):
        original = self.adapter.read_bytes()
        changes = ((b'metadata.st_size == 0', b'metadata.st_size < 0'),
                   (b'if raw.endswith(b"\\n"):', b'if False:'))
        for old, new in changes:
            with self.subTest(changed=old.decode()):
                changed = original.replace(old, new)
                self.assertNotEqual(changed, original)
                self.pin_adapter(changed)
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET profile_sha256=?', (ao_delegates.expected_profile(self.state()['delegate']['inventory']),))
                self.assert_refused_without_change(self.audit_transition, 'key-access contract differs')
                self.assertFalse(transition._pending_receipts(self.directory()))

    def test_stale_candidate_and_missing_authorization_refuse_before_commit(self):
        args = self.args()
        self.assert_refused_without_change(lambda: self.service.ao_room_provider_transition(**{**args, 'authorization': ' '}), 'nonempty')
        self.assert_refused_without_change(lambda: self.service.ao_room_provider_transition(**args), 'not positively stopped')
        (self.repo / 'feature.txt').write_text('changed after audit\n')
        self.assert_refused_without_change(lambda: self.commit_transition(args), 'audit is stale')

    def test_corrupt_epoch_files_never_return_an_idempotent_success(self):
        args = self.args()
        self.commit_transition(args)
        state = self.state()
        config = Path(next(p for p in state['delegate']['inventory']['files'] if p.endswith('.json')))
        config.write_text('{}')
        self.assert_refused_without_change(lambda: self.service.ao_room_provider_transition(**args), 'epoch file was modified')
        self.assertEqual(self.service.ao_room_status(self.room)['provider_transition']['state'], 'damaged')


    def test_pending_routing_status_verifies_only_unchanged_provider_base_and_never_attaches(self):
        self.commit_transition()
        state = self.state()
        state['routing_adoption'] = {'phase': 'pending', 'request_id': 'synthetic-overlay'}
        self.service.save(self.directory(), state)
        status = self.service.ao_room_status(self.room)['provider_transition']
        self.assertEqual(status['state'], 'committed')
        self.assertEqual(status['attachment'], 'routing_pending')
        self.assertTrue(status['routing_pending'])
        self.assertFalse(status['attached'])
        self.assert_refused_without_change(lambda: self.service.ao_room_sync(self.room), 'pending')
        state['preparation_sha256'] = '0' * 64
        self.service.save(self.directory(), state)
        self.assertEqual(self.service.ao_room_status(self.room)['provider_transition']['state'], 'damaged')


class ProviderToolSchemaTests(unittest.TestCase):
    def test_mcp_discovery_and_strict_call_dispatch_cover_all_five_apis(self):
        import ao_routing_adoption
        cases = [
            ('ao_room_provider_transition_audit', transition, 'audit', {'room_id': 'synthetic-room', 'target_profile': {'model': 'deepseek-flash'}}),
            ('ao_room_provider_transition', transition, 'transition', {'room_id': 'synthetic-room', 'audit_sha256': 'a' * 64,
                'diagnosis': 'Synthetic diagnosis', 'authorization': 'Synthetic actual decision', 'request_id': 'provider-once', 'native_stop_record': 'Synthetic stopped record'}),
            ('ao_room_routing_adoption_audit', ao_routing_adoption, 'audit', {'room_id': 'synthetic-room'}),
            ('ao_room_routing_adoption_stage', ao_routing_adoption, 'stage', {'room_id': 'synthetic-room', 'audit_sha256': 'a' * 64,
                'authorization': 'Synthetic operating authorization', 'diagnosis': 'Synthetic diagnosis', 'request_id': 'routing-once'}),
            ('ao_room_routing_adoption_activate', ao_routing_adoption, 'activate', {'room_id': 'synthetic-room', 'request_id': 'routing-once'}),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            controller = object.__new__(project_room.Service)
            controller.home = Path(temporary)
            listing = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, controller)
            definitions = {entry['name']: entry for entry in listing['result']['tools']}
            for name, module, function, arguments in cases:
                with self.subTest(tool=name), patch.object(module, function, return_value={'dispatched': name}) as called:
                    self.assertEqual(definitions[name]['inputSchema'], ao.TOOL_SCHEMAS[name][1])
                    self.assertFalse(definitions[name]['annotations']['readOnlyHint'])
                    self.assertFalse(definitions[name]['inputSchema']['additionalProperties'])
                    envelope = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
                    result = project_room_mcp.handle(envelope, controller)['result']
                    self.assertFalse(result['isError'], result)
                    self.assertEqual(result['structuredContent'], {'dispatched': name})
                    self.assertEqual(called.call_args.args[0].root, Path(temporary).resolve() / 'ao')
                    self.assertEqual(called.call_args.args[1:], tuple(arguments.values()))
                    called.assert_called_once()
                    called.reset_mock()
                    for invalid in ({**arguments, 'unexpected': True}, {k: v for k, v in arguments.items() if k != 'room_id'}):
                        invalid_envelope = {**envelope, 'params': {'name': name, 'arguments': invalid}}
                        self.assertTrue(project_room_mcp.handle(invalid_envelope, controller)['result']['isError'])
                    called.assert_not_called()


class LaterSpecHandoffTests(AdoptionFixture):
    def agree(self, decision='accept', revision=1, key='spec_review'):
        # Keep the ordinary review budget for a later exact specification; no review-limit patch or reset.
        if key in ('charter-1', 'charter-2'):
            return self.service.ao_room_status(self.room)
        return super().agree(decision, revision, key)

    def test_later_spec_handoff_uses_adopted_preparation_and_requires_fresh_engineering(self):
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.implement()
        original = self.state()
        old_handoff = self.directory() / original['handoff']
        old_bytes = old_handoff.read_bytes()
        old_result = copy.deepcopy(original['requests']['implementation'])
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        prepared = self.configure_routing()
        active_preparation = self.state()['preparation_sha256']
        self.assertNotEqual(active_preparation, original['preparation_sha256'])
        self.assertEqual(ao_workflow.handoff_record(self.directory(), self.state())['preparation_sha256'],
                         original['preparation_sha256'])

        self.service.ao_room_spec_put(self.room, 2, 'Confirm the implementation under a newly authorized exact specification.',
                                      self.gates, 'Synthetic user approves this revised scope')
        self.fake.snapshots['engineer']['controller'] = 'ready'
        before = (self.directory() / 'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'launch'):
            self.service.ao_room_send(self.room, 'engineer', 'Review the revised specification.', 'premature-revised-spec',
                                      purpose='spec_review')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)
        self.start_attachment(prepared)
        self.agree(revision=2, key='revised-specification')
        revision_request = self.state()['requests']['revised-specification']
        self.assertEqual(revision_request['provider_epoch'], 2)
        self.assertIn('provider_amendment_sha256', revision_request['carried'])
        self.assertIn('routing_amendment_sha256', revision_request['carried'])
        handoff = self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(handoff['preparation_sha256'], active_preparation)
        self.assertEqual(handoff['spec_revision'], 2)
        self.assertNotEqual(self.state()['handoff'], original['handoff'])
        self.assertEqual(old_handoff.read_bytes(), old_bytes)
        self.assertEqual(self.state()['requests']['implementation'], old_result)
        with self.assertRaisesRegex(ao.RoomError, 'current provider epoch'):
            self.service.ao_room_verify(self.room, str(self.repo))

        # A new completed native result and new gates are still required, even when the candidate bytes are unchanged.
        self.service.ao_room_send(self.room, 'engineer', 'Implement the newly agreed specification.', 'new-implementation',
                                  purpose='implementation')
        self.assertEqual(self.state()['requests']['new-implementation']['text'], 'Implement the newly agreed specification.')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        result = ao_workflow.engineering_ready(self.service, self.directory(), self.state())
        self.assertEqual(result['request_id'], 'new-implementation')
        self.assertEqual(result['provider_epoch'], 2)
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        self.assertEqual(old_handoff.read_bytes(), old_bytes)
        self.assertEqual(self.state()['requests']['implementation'], old_result)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 2)
        transition.validate(self.service, self.state())


class ReviewerRecoveryAdoptionTests(AdoptionFixture):
    def bind(self):
        self.fake.add('replacement')
        for name in ('reviewer', 'replacement'):
            snapshot = self.fake.snapshots[name]
            snapshot.update(harness='codex', mode='chat', controller='stopped' if name == 'reviewer' else 'ready',
                            activeBranchId=snapshot['conversationId'] + ':root', latestSequence=0,
                            nativeForkAvailableAfterSequence=0, hasMoreBefore=False, branchedFromEarlierMessage=False,
                            activities=[], branchMaterialization={'strategy': 'native', 'replayTruncated': False})
            self.fake.sessions[name]['isTerminated'] = False
            workspace = self.root / (name + '-workspace')
            subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(workspace), 'HEAD'],
                           check=True, capture_output=True)
            self.fake.workspaces[name] = workspace
        super().bind()

    def test_public_unused_reviewer_recovery_then_sync_first_review_and_acceptance_preserve_adoption(self):
        import ao_routing_adoption
        prepared = self.configure_routing()
        process = self.start_attachment(prepared)
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.implement()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        # Independent acceptance does not depend on a currently running engineer attachment.
        process.stdin.close()
        process.wait(timeout=10)
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        before = self.state()
        original_epoch = (self.directory() / before['provider_transition']['epoch_record']).read_bytes()
        original_overlay = (self.directory() / before['routing_adoption']['receipt']).read_bytes()
        posts = len(self.fake.posts)
        audit = self.service.ao_room_reviewer_recovery_audit(self.room)
        arguments = dict(room_id=self.room, audit_sha256=audit['audit_sha256'], replacement_session_id='replacement',
                         diagnosis='Synthetic unused reviewer cannot resume', authorization='Synthetic actual recovery decision',
                         request_id='recover-unused')
        result = self.service.ao_room_reviewer_recover(**arguments)
        self.assertTrue(result['recovered'])
        state = self.state()
        self.assertEqual(state['bindings']['reviewer']['session_id'], 'replacement')
        self.assertEqual(len(self.fake.posts), posts)
        comparable = copy.deepcopy(state)
        comparable.pop('reviewer_recovery')
        comparable['bindings']['reviewer'] = before['bindings']['reviewer']
        self.assertEqual(comparable, before)
        transition.validate(self.service, state)
        ao_routing_adoption.validate(self.service, state)
        record = recovery.validate(self.service, state)
        self.assertEqual(record['original'], before['bindings']['reviewer'])
        wrong_original = copy.deepcopy(before['bindings'])
        wrong_original['reviewer']['session_id'] = 'another-original'
        with self.assertRaisesRegex(ao.RoomError, 'pinned reviewer'):
            transition.validate_bindings(self.service, state, wrong_original)
        self.service.ao_room_sync(self.room)
        self.service.ao_room_send(self.room, 'reviewer', 'Independently review the exact candidate.', 'first-review', purpose='acceptance_review')
        request = self.state()['requests']['first-review']
        self.assertEqual(request['session_id'], 'replacement')
        self.fake.finish('replacement', json.dumps({**request['review'], 'decision': 'approved', 'review': 'Synthetic exact candidate and gates checked.'}))
        self.service.ao_room_sync(self.room)
        self.assertTrue(self.service.ao_room_accept(self.room, 'first-review')['accepted'])
        self.assertEqual(self.service.ao_room_reviewer_recover(**arguments), result)
        self.assertEqual(sum(r['role'] == 'reviewer' for r in self.state()['requests'].values()), 1)
        self.assertEqual((self.directory() / before['provider_transition']['epoch_record']).read_bytes(), original_epoch)
        self.assertEqual((self.directory() / before['routing_adoption']['receipt']).read_bytes(), original_overlay)
