"""Source-qualified worker preparation, dispatch and explicit qualified refresh.

Synthetic Git repositories, synthetic private qualification artifacts, a version-reporting fake
Claude executable and the in-process fake AO only; no model, account or network is used. Nothing
here observes or attributes a served child model, provider or effective provider effort.
"""

import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_executable_binding
import ao_model_qualification as qmod
import ao_project_room as ao
import ao_routing
import ao_routing_guard
import ao_routing_refresh as refresh
import test_ao_routing_refresh as refresh_tests
from test_ao_normal import Fixture

OPUS = 'claude-opus-5-5'
NEW_OPUS = 'claude-opus-5-6'
NEW_SONNET = 'claude-sonnet-5-6'


class QualifiedWorkerFixture(Fixture):
    """A room whose operator configuration maps both enabled worker families to exact models."""

    def setUp(self):
        super().setUp()
        self.room = self.open(provider='none'); self.spec()

    def prepared(self):
        return ao.read(self.directory() / self.state()['preparation'])

    def routing(self):
        return self.service.ao_room_status(self.room)['delegate']['routing']

    def configure_newer_workers(self, **families):
        """Configure a newer synthetic artifact that changes only the named worker families."""
        artifact = copy.deepcopy(self.qualification_artifact())
        artifact['revision'] += 1
        for family, expected in families.items():
            artifact['families'][family] = {**artifact['families'][family], 'expected_model': expected}
        self.configure_qualification(artifact)
        return artifact


class QualifiedPreparationTests(QualifiedWorkerFixture):
    def test_new_preparation_derives_exact_qualified_worker_map_from_the_source_artifact(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        artifact = self.qualification_artifact()
        self.assertEqual(routing['version'], 3)
        self.assertEqual(routing['agents'], self.QUALIFIED_WORKERS)
        self.assertEqual(routing['effort'], 'max')
        self.assertEqual(routing['agent_selection'], ao_routing.AGENT_SELECTION)
        self.assertEqual(routing['agent_identity_basis'], ao_routing.QUALIFIED_IDENTITY_BASIS)
        value = routing['worker_qualification']
        self.assertEqual(value['snapshot']['sha256'], qmod.digest(artifact))
        self.assertEqual(value['snapshot']['record'], qmod.record_path(qmod.digest(artifact)))
        self.assertEqual(value['snapshot']['evidence'], qmod.evidence_entries(artifact, qmod.digest(artifact)))
        self.assertEqual(value['families']['pr-opus']['expected_model'], self.QUALIFIED_WORKERS['pr-opus'])
        self.assertEqual(value['families']['pr-sonnet']['family'], 'sonnet')
        self.assertEqual(value['families']['pr-opus']['minimum_claude_code_version'], self.WORKER_FLOOR)
        for role, expected in self.QUALIFIED_WORKERS.items():
            fields = ao_routing.parse_definition((self.repo / '.claude/agents' / (role + '.md')).read_text())
            self.assertEqual((fields['model'], fields['effort']), (expected, 'max'))
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)
        self.assertTrue((self.directory() / value['snapshot']['record']).is_file())
        for entry in value['snapshot']['evidence']:
            self.assertTrue((self.directory() / entry['record']).is_file())
        status = self.routing()
        self.assertEqual(status['status'], 'configured')
        self.assertEqual(status['worker_selectors']['pr-opus'],
                         'source-qualified exact model id ' + self.QUALIFIED_WORKERS['pr-opus'] + ' (family intent opus)')
        self.assertEqual(status['worker_qualification']['sha256'], qmod.digest(artifact))
        self.assertIn('does not attribute actual child model identity', status['worker_identity'])
        text = ao_routing.packet_text(prepared)
        self.assertIn('pr-sonnet (source-qualified exact model ' + self.QUALIFIED_WORKERS['pr-sonnet'], text)
        self.assertIn('pr-opus (source-qualified exact model ' + self.QUALIFIED_WORKERS['pr-opus'], text)
        self.assertIn('The guard denies root shell commands, edits, tests, browser work and unknown execution tools',
                      text)
        self.assertIn('One layer, at most two concurrent, no model overrides', text)
        self.assertNotIn('family alias opus', text)

    def test_missing_worker_family_refuses_before_runtime_writes(self):
        original = self.qualification_artifact()
        artifact = copy.deepcopy(original)
        artifact['revision'] += 1
        artifact['families'].pop('sonnet')
        self.configure_qualification(artifact)
        with self.assertRaisesRegex(ao.RoomError, 'does not map worker family sonnet'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude').exists())
        self.assertIsNone(self.state().get('preparation'))
        self.assertTrue(list(self.directory().glob('routing-error-*.json')))
        self.configure_qualification(original)

    def test_old_executable_refuses_before_runtime_writes(self):
        old = self.root / 'old-claude-version'
        old.write_text('#!' + sys.executable + '\nprint("2.1.100 (Claude Code)")\n')
        old.chmod(0o700)
        config_path = self.home / 'config.json'
        config = ao.read(config_path)
        config['claude_bin'] = str(old)
        ao.atomic(config_path, config)
        with self.assertRaisesRegex(ao.RoomError, 'pr-opus requires Claude Code 2.1.280 or newer'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude').exists())
        self.assertIsNone(self.state().get('preparation'))

    def test_retained_proof_survives_removed_originals_but_changed_retained_bytes_refuse(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        snapshot = routing['worker_qualification']['snapshot']
        Path(snapshot['artifact']['sources'][0]['evidence_file']).unlink()
        (self.root / 'qualification.json').write_text(json.dumps({'forged': True}))
        with self.assertRaises(ao.RoomError):
            qmod.load(em.configured_qualification(self.home / 'ao'))
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)
        self.assertEqual(self.routing()['status'], 'configured')
        retained = self.directory() / snapshot['evidence'][0]['record']
        raw = retained.read_bytes()
        retained.write_bytes(raw + b'changed')
        with self.assertRaisesRegex(ao.RoomError, 'retained'):
            ao_routing.validate_local(prepared, self.state(), self.directory())
        self.assertEqual(self.routing()['status'], 'unverified')
        retained.write_bytes(raw)
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)

    def test_pointer_changes_do_not_reinterpret_the_pinned_worker_artifact(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        root_policy = json.dumps(self.state()['engineering_model'], sort_keys=True)
        pinned = (self.directory() / self.state()['preparation']).read_bytes()
        self.configure_newer_workers(opus=NEW_OPUS, sonnet=NEW_SONNET)
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), pinned)
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)
        self.assertEqual(routing['agents']['pr-opus'], OPUS)
        self.assertEqual(json.dumps(self.state()['engineering_model'], sort_keys=True), root_policy)

    def test_lower_helper_and_caller_validation_have_distinct_responsibilities(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        snapshot = routing['worker_qualification']['snapshot']
        configuration = {'worktree': str(self.repo), 'agents': routing['agents']}
        with patch.object(ao_routing, 'validate_local', side_effect=AssertionError('routing file validation')):
            lower = qmod.admit_qualification({'artifact': snapshot['artifact'], 'sha256': snapshot['sha256'],
                                              'record': snapshot['record'], 'evidence': snapshot['evidence']},
                                             'sonnet', directory=self.directory(),
                                             executable=routing['claude'], configuration=configuration,
                                             origin={'context': 'pending_routing_snapshot'},
                                             worker_roles=ao_routing.WORKER_FAMILIES)
        self.assertEqual(lower['qualification']['sha256'], snapshot['sha256'])
        self.assertEqual(lower['effective_minimum_claude_code_version'],
                         lower['qualification']['effective_minimum_claude_code_version'])
        self.assertEqual(lower['worker_expected_models'], self.QUALIFIED_WORKERS)
        with patch.object(qmod, 'admission', side_effect=AssertionError('qualification admission')):
            self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)

    def test_user_or_project_alias_remap_refuses_qualified_preparation(self):
        (self.claude_env / 'settings.json').write_text(
            json.dumps({'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': NEW_OPUS}}))
        with self.assertRaisesRegex(ao.RoomError, 'ANTHROPIC_DEFAULT_OPUS_MODEL'):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertFalse((self.repo / '.claude').exists())

    def test_custom_gateway_refuses_qualified_preparation_without_echoing_the_url(self):
        secret = 'https://gateway.invalid.example/private-token'
        (self.claude_env / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': secret}}))
        with self.assertRaisesRegex(ao.RoomError, 'ANTHROPIC_BASE_URL') as refused:
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.assertNotIn(secret, str(refused.exception))
        self.assertFalse((self.repo / '.claude').exists())

    def test_process_environment_remap_and_gateway_refuse_qualified_dispatch(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        for key in ao_routing.ALIAS_ENV + ao_routing.GATEWAY_ENV:
            with self.subTest(key=key), patch.dict(os.environ, {key: 'unverified-value'}):
                with self.assertRaisesRegex(ao.RoomError, key):
                    ao_routing.validate_local(prepared, self.state(), self.directory())
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)

    def test_guard_refuses_inherited_force_remap_gateway_and_limit_contradictions(self):
        event = {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'pr-opus', 'prompt': 'review'}}
        with patch.dict(os.environ, ao_routing.FOREGROUND_ENV):
            self.assertIsNone(ao_routing_guard.decide(event))
            for key, value in (('CLAUDE_CODE_SUBAGENT_MODEL_FORCE', NEW_OPUS),
                               ('ANTHROPIC_DEFAULT_OPUS_MODEL', NEW_OPUS),
                               ('ANTHROPIC_BASE_URL', 'https://gateway.invalid.example/private-token'),
                               ('CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', '8')):
                with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                    reason = ao_routing_guard.decide(event)
                    self.assertIsNotNone(reason)
                    self.assertIn(key, reason)
                    self.assertNotIn(value, reason)

    def test_process_model_force_and_managed_settings_override_use_the_supported_mapping(self):
        prepared = self.service.ao_room_prepare(self.room, str(self.repo))
        routing = prepared['routing']
        managed = self.root / 'managed-settings.json'
        managed.write_text(json.dumps({'env': {'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '9'}}))
        with patch.dict(os.environ, {'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'claude-opus-5-5'}):
            with self.assertRaisesRegex(ao.RoomError, 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE'):
                ao_routing.validate_local(prepared, self.state(), self.directory())
        with patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)}):
            self.assertEqual(ao_routing.managed_settings_path(os.environ), managed)
            with self.assertRaisesRegex(ao.RoomError, 'managed'):
                ao_routing.validate_local(prepared, self.state(), self.directory())
            # An explicitly supplied empty mapping never falls back to ambient process state.
            self.assertIsNone(ao_routing.contradictions(routing['claude_config_dir'], self.repo, {}))
        with self.assertRaisesRegex(ao.RoomError, 'text path'):
            ao_routing.managed_settings_path({'CLAUDE_CODE_MANAGED_SETTINGS_PATH': 7})
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)

    def test_project_routing_destination_change_refuses_qualified_dispatch(self):
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        secret = 'https://gateway.invalid.example/private-token'
        self.fake.config['env'] = {'ANTHROPIC_BASE_URL': secret}
        try:
            with self.assertRaisesRegex(ao.RoomError, 'ANTHROPIC_BASE_URL') as refused:
                self.send('implementation')
            self.assertNotIn(secret, str(refused.exception))
            self.assertNotIn('implementation', self.state()['requests'])
        finally:
            self.fake.config['env'] = {}
        self.send('implementation')
        self.assertIn('implementation', self.state()['requests'])


class HistoricalWorkerDispatchTests(QualifiedWorkerFixture):
    def test_unqualified_old_record_stays_readable_but_new_delegation_refuses(self):
        self.bind()
        refresh_tests.historical_routing(self, ao_routing.FAMILY_AGENTS, ao_routing.AGENT_SELECTION,
                                         ao_routing.AGENT_IDENTITY_BASIS)
        prepared = self.prepared()
        self.assertEqual(prepared['routing']['version'], 2)
        self.assertNotIn('worker_qualification', prepared['routing'])
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), prepared['routing'])
        self.agree()
        self.assertEqual(self.routing()['status'], 'verified')
        self.assertIn('family alias sonnet', ao_routing.packet_text(prepared))
        with self.assertRaisesRegex(ao.RoomError, 'source-qualified'):
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])
        self.assertEqual(self.service.ao_room_status(self.room)['delegate']['attachment'], 'configuration_verified')


class QualifiedRefreshTests(unittest.TestCase):
    """Qualified refresh flows composed from the ordinary synthetic refresh fixture.

    The fixture class is instantiated, not subclassed, so its unrelated contract tests are not run
    twice here.
    """

    def setUp(self):
        self.case = refresh_tests.RoutingRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.case.prepared = ao_delegates.preparation(self.case.directory(), self.case.state())
        self.assertEqual(self.case.prepared['routing']['version'], 3)

    def refresh(self, **changes):
        return self.case.do_refresh(**changes)

    def test_qualified_refresh_pins_the_selected_artifact_once_and_preserves_history(self):
        case = self.case
        artifact = case.qualification_artifact()
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['model_dispatch'])
        self.assertEqual(record['inputs']['agent_selection'], 'qualified')
        self.assertEqual(record['target']['agents'], case.QUALIFIED_WORKERS)
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(artifact))
        # The intent freezes the exact concrete executable identity the target's floors were checked
        # against: the recorded pinned executable itself when no audited replacement is bound.
        self.assertEqual(record['evidence']['current_executable'], case.prepared['routing']['claude'])
        self.assertEqual({key: value for key, value in case.state().items() if key != 'routing_refresh'}, case.original)
        self.assertEqual((case.directory() / case.state()['preparation']).read_bytes(), case.prepared_bytes)
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared), record['target'])
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_same_exact_ids_with_new_evidence_is_still_an_explicit_boundary(self):
        case = self.case
        artifact = case.qualification_artifact()
        case.configure_qualification({**copy.deepcopy(artifact), 'revision': artifact['revision'] + 1})
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        change = record['evidence']['agent_selection_change']
        self.assertEqual(change['from'], change['to'])  # the same exact ids, a new qualification boundary
        self.assertEqual(change['previous_qualification_sha256'], qmod.digest(artifact))
        self.assertEqual(change['worker_qualification_sha256'],
                         record['target']['worker_qualification']['snapshot']['sha256'])
        self.assertNotEqual(change['worker_qualification_sha256'], change['previous_qualification_sha256'])
        self.assertEqual(result['agent_selection_change'], change)
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_qualified_selection_is_never_downgraded_to_a_historical_map(self):
        case = self.case
        self.refresh(agent_selection='qualified')
        pointer = case.state()['routing_refresh']
        path = case.directory() / pointer['path']
        raw = path.read_bytes()
        state_bytes = (case.directory() / 'state.json').read_bytes()
        runtime = {name: (case.repo / name).read_bytes() for name in ao_routing.FILES}
        try:
            record = json.loads(raw)
            record['target']['version'] = 2
            record['target']['agents'] = {'pr-sonnet': 'sonnet', 'pr-opus': 'opus'}
            record['target']['agent_selection'] = dict(ao_routing.AGENT_SELECTION)
            record['target']['agent_identity_basis'] = ao_routing.AGENT_IDENTITY_BASIS
            record['target'].pop('worker_qualification')
            ao.atomic(path, record)
            state = case.state()
            state['routing_refresh'] = {**pointer, 'sha256': ao.digest(record)}
            # The downgraded metadata no longer agrees with the retained bundle it names: the real
            # refusal is the pinned worker-definition boundary, and nothing is committed.
            with self.assertRaisesRegex(ao.RoomError, 'Agent definition model is not the pinned mapping'):
                refresh.effective(case.directory(), state, case.prepared)
            self.assertEqual((case.directory() / 'state.json').read_bytes(), state_bytes)
            self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES}, runtime)
        finally:
            path.write_bytes(raw)
        # The committed epoch still resolves to the source-qualified exact map: the forged
        # historical downgrade never became effective.
        committed = refresh.effective(case.directory(), case.state(), case.prepared)
        self.assertEqual(committed['version'], 3)
        self.assertEqual(committed['agents'], case.QUALIFIED_WORKERS)
        self.assertIsNotNone(committed.get('worker_qualification'))

    def test_pending_qualified_refresh_uses_the_stored_target_after_the_pointer_moves(self):
        case = self.case
        artifact = case.qualification_artifact()
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        self.assertEqual(case.state(), case.original)
        newer = copy.deepcopy(artifact)
        newer['revision'] += 1
        newer['families']['opus']['expected_model'] = NEW_OPUS
        case.configure_qualification(newer)
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(record['target']['agents']['pr-opus'], OPUS)
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(artifact))
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared)['agents']['pr-opus'], OPUS)
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_root_epoch_and_worker_artifact_stay_independently_bound(self):
        case = self.case
        artifact = case.qualification_artifact()
        root_policy = json.dumps(case.state()['engineering_model'], sort_keys=True)
        newer = copy.deepcopy(artifact)
        newer['revision'] += 1
        newer['families']['opus']['expected_model'] = NEW_OPUS
        case.configure_qualification(newer)
        self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertEqual(record['target']['agents']['pr-opus'], NEW_OPUS)
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(newer))
        self.assertEqual(json.dumps(case.state()['engineering_model'], sort_keys=True), root_policy)
        self.assertEqual(em.current(case.directory(), case.state())['expected_model'], case.QUALIFIED_MODEL)
        third = copy.deepcopy(newer)
        third['revision'] += 1
        third['families']['sonnet']['expected_model'] = NEW_SONNET
        case.configure_qualification(third)
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared)['agents']['pr-opus'], NEW_OPUS)
        self.assertEqual(em.current(case.directory(), case.state())['expected_model'], case.QUALIFIED_MODEL)

    def test_effective_worker_selection_reference_survives_a_later_refresh(self):
        case = self.case
        reference = ao_routing.worker_qualification_reference(case.prepared['routing'])
        frozen = ao_routing.effective_worker_selection(case.prepared, case.state(), case.directory())
        self.assertTrue(frozen['source_qualified'])
        self.assertEqual(frozen['agents'], case.prepared['routing']['agents'])
        self.assertEqual(frozen['routing_sha256'], reference['routing_sha256'])
        self.refresh(agent_selection='qualified')
        again = ao_routing.effective_worker_selection(case.prepared, case.state(), case.directory())
        self.assertNotEqual(again['routing_sha256'], frozen['routing_sha256'])
        historical = ao_routing.historical_routing(case.directory(), case.state(), case.prepared, reference)
        self.assertEqual(historical['source'], 'preparation')
        self.assertEqual(historical['routing']['agents'], frozen['agents'])
        self.assertEqual(ao_routing.worker_qualification_reference(historical['routing']), reference)

    def test_effective_worker_selection_pairs_the_authenticated_preparation_with_its_digest(self):
        case = self.case
        state = case.state()
        frozen = ao_routing.effective_worker_selection(case.prepared, state, case.directory())
        self.assertTrue(frozen['source_qualified'])
        self.assertEqual(frozen['preparation'], state['preparation'])
        self.assertEqual(frozen['preparation_sha256'], state['preparation_sha256'])
        self.assertEqual(frozen['preparation_sha256'], ao.digest(case.prepared))

    def test_pending_qualified_refresh_rechecks_the_stored_target_retained_capture(self):
        case = self.case
        # The target keeps its own independently retained source capture: a newer worker artifact is
        # selected for this refresh, so damaging the target's proof cannot damage the room's own root
        # selection proof, which shares no retained bytes with it.
        artifact = case.qualification_artifact()
        newer = copy.deepcopy(artifact)
        newer['revision'] += 1
        newer['families']['opus']['expected_model'] = NEW_OPUS
        case.configure_qualification(newer)
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        self.assertEqual(case.state(), case.original)
        record = json.loads((case.directory() / refresh.BASE / 'refresh-one.json').read_text())
        snapshot = record['target']['worker_qualification']['snapshot']
        self.assertEqual(snapshot['sha256'], qmod.digest(newer))
        self.assertNotEqual(snapshot['sha256'], qmod.digest(artifact))
        self.assertEqual(record['target']['agents']['pr-opus'], NEW_OPUS)
        retained = case.directory() / snapshot['evidence'][0]['record']
        raw = retained.read_bytes()
        for damage in ('changed', 'missing'):
            with self.subTest(damage=damage):
                if damage == 'changed':
                    retained.write_bytes(raw + b'changed')
                else:
                    retained.unlink()
                try:
                    with self.assertRaisesRegex(ao.RoomError, 'retained evidence'):
                        self.refresh(agent_selection='qualified')
                    # The room's own root selection proof is untouched and still validates alone.
                    root = case.state()[em.SELECTION_KEY]['family_qualification']
                    self.assertIsNotNone(qmod.verify_reference(
                        case.directory(),
                        {'sha256': root['sha256'], 'record': root['record'], 'evidence': root['evidence']},
                        'The root selection retained evidence changed unexpectedly'))
                    self.assertIsNotNone(em.initial(case.directory(), case.state())['family_qualification'])
                    self.assertEqual(case.state(), case.original)
                    self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
                                     case.original_files)
                finally:
                    retained.write_bytes(raw)
        # The positive replay uses the retained target proof alone after the target's private
        # originals are gone and the active operator pointer has moved to a third artifact.
        for descriptor in newer['sources']:
            Path(descriptor['evidence_file']).unlink()
        (case.root / 'qualification.json').write_text(json.dumps({'forged': True}))
        third = copy.deepcopy(newer)
        third['revision'] += 1
        case.configure_qualification(third)
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(newer))
        self.assertEqual(record['target']['agents']['pr-opus'], NEW_OPUS)
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_pending_qualified_refresh_refuses_process_environment_changes_after_intent(self):
        case = self.case
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        for key in ao_routing.GATEWAY_ENV + ao_routing.ALIAS_ENV + ao_routing.CONTRADICTORY_ENV:
            with self.subTest(key=key), patch.dict(os.environ, {key: 'unverified-value'}):
                with self.assertRaisesRegex(ao.RoomError, key):
                    self.refresh(agent_selection='qualified')
                self.assertEqual(case.state(), case.original)
                self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
                                 case.original_files)
        self.assertFalse(self.refresh(agent_selection='qualified')['idempotent'])
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_pending_qualified_refresh_refuses_a_project_gateway_after_intent(self):
        case = self.case
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        config = copy.deepcopy(case.fake.config)
        secret = 'https://gateway.invalid.example/private-token'
        case.fake.config['env'] = {'ANTHROPIC_BASE_URL': secret}
        try:
            with self.assertRaisesRegex(ao.RoomError, 'ANTHROPIC_BASE_URL') as refused:
                self.refresh(agent_selection='qualified')
            self.assertNotIn(secret, str(refused.exception))
            self.assertEqual(case.state(), case.original)
            self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
                             case.original_files)
        finally:
            case.fake.config = config
        self.assertFalse(self.refresh(agent_selection='qualified')['idempotent'])
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_pending_qualified_refresh_refuses_an_incompatible_executable_after_intent(self):
        case = self.case
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        recorded = case.prepared['routing']['claude']
        original = case.cli.read_bytes()
        changed = original.replace(b'2.1.282', b'2.1.100')
        self.assertEqual(len(changed), len(original))
        try:
            case.cli.write_bytes(changed)
            os.utime(case.cli, ns=(recorded['mtime_ns'], recorded['mtime_ns']))
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.280 or newer'):
                self.refresh(agent_selection='qualified')
            self.assertEqual(case.state(), case.original)
            self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
                             case.original_files)
        finally:
            case.cli.write_bytes(original)
            os.utime(case.cli, ns=(recorded['mtime_ns'], recorded['mtime_ns']))
        self.assertFalse(self.refresh(agent_selection='qualified')['idempotent'])
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_pending_qualified_refresh_completes_from_retained_proof_and_changed_pointer(self):
        case = self.case
        artifact = case.qualification_artifact()
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        journal = (case.directory() / refresh.BASE / 'refresh-one.json').read_bytes()
        preparation = (case.directory() / case.state()['preparation']).read_bytes()
        for descriptor in case.prepared['routing']['worker_qualification']['snapshot']['artifact']['sources']:
            Path(descriptor['evidence_file']).unlink()
        (case.root / 'qualification.json').write_text(json.dumps({'forged': True}))
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(artifact))
        self.assertEqual((case.directory() / refresh.BASE / 'refresh-one.json').read_bytes(), journal)
        self.assertEqual((case.directory() / case.state()['preparation']).read_bytes(), preparation)
        self.assertEqual({key: value for key, value in case.state().items() if key != 'routing_refresh'}, case.original)
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared)['agents'],
                         case.QUALIFIED_WORKERS)
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_historical_lookup_validates_the_reference_and_the_preparation_bytes(self):
        case = self.case
        reference = ao_routing.worker_qualification_reference(case.prepared['routing'])
        for change in ({'routing_version': 999}, {'routing_version': True}, {'qualification_sha256': None},
                       {'qualification_record': qmod.record_path('f' * 64)}):
            with self.subTest(change=sorted(change)):
                with self.assertRaises(ao.RoomError):
                    ao_routing.historical_routing(case.directory(), case.state(), case.prepared,
                                                  {**reference, **change})
        with self.assertRaisesRegex(ao.RoomError, 'preparation bytes'):
            ao_routing.historical_routing(case.directory(), case.state(), {}, reference)
        historical = ao_routing.historical_routing(case.directory(), case.state(), case.prepared, reference)
        self.assertEqual(historical['source'], 'preparation')
        self.assertEqual(historical['routing']['agents'], case.QUALIFIED_WORKERS)

    def test_historical_lookup_refuses_a_cyclic_ancestry(self):
        case = self.case
        reference = ao_routing.worker_qualification_reference(case.prepared['routing'])
        self.refresh(agent_selection='qualified')
        pointer = case.state()['routing_refresh']
        path = case.directory() / pointer['path']
        raw = path.read_bytes()
        try:
            record = json.loads(raw)
            record['previous'] = pointer
            ao.atomic(path, record)
            state = case.state()
            state['routing_refresh'] = {**pointer, 'sha256': ao.digest(record)}
            with self.assertRaisesRegex(ao.RoomError, 'cyclic|bound'):
                ao_routing.historical_routing(case.directory(), state, case.prepared, reference)
        finally:
            path.write_bytes(raw)

    def test_packet_text_and_reference_authenticate_the_routing_record(self):
        case = self.case
        forged = copy.deepcopy(case.prepared['routing'])
        forged['agents'] = {'pr-sonnet': case.QUALIFIED_WORKERS['pr-sonnet'], 'pr-opus': case.QUALIFIED_MODEL}
        with self.assertRaisesRegex(ao.RoomError, 'exact map'):
            ao_routing.packet_text({'routing': forged})
        with self.assertRaisesRegex(ao.RoomError, 'exact map'):
            ao_routing.worker_qualification_reference(forged)
        self.assertIsNotNone(ao_routing.worker_qualification_reference(case.prepared['routing']))

    def test_convenience_admission_reads_the_committed_refreshed_routing(self):
        case = self.case
        original = case.qualification_artifact()
        newer = copy.deepcopy(original)
        newer['revision'] += 1
        newer['families']['opus']['expected_model'] = NEW_OPUS
        case.configure_qualification(newer)
        self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(newer))
        prepared = ao_delegates.preparation(case.directory(), case.state())
        evidence = qmod.admission(case.directory(), case.state(), prepared=prepared)
        self.assertEqual(evidence['qualification']['sha256'], qmod.digest(original))
        self.assertEqual(evidence['expected_model'], case.QUALIFIED_MODEL)
        self.assertEqual(evidence['executable']['version'], '2.1.282 (Claude Code)')

    def test_qualified_refresh_rejects_an_unbound_newer_configured_executable(self):
        case = self.case
        artifact = case.qualification_artifact()
        newer = copy.deepcopy(artifact)
        newer['revision'] += 1
        newer['families']['opus']['minimum_claude_code_version'] = '2.1.300'
        case.configure_qualification(newer)
        unrelated = case.root / 'unrelated-claude'
        unrelated.write_text('#!' + sys.executable + '\nimport sys\n'
                             'print("2.1.300 (Claude Code)" if sys.argv[1:] == ["--version"] else "unexpected")\n')
        unrelated.chmod(0o700)
        config = ao.read(case.home / 'config.json')
        config['claude_bin'] = str(unrelated)
        ao.atomic(case.home / 'config.json', config)
        # Even when today's mutable configuration claims the newer binary, the target's own floors are
        # applied to the effective pinned executable alone and that configuration is never read.
        with patch.object(ao_routing, 'context',
                          side_effect=lambda service, directory, state: {'claude_bin': str(unrelated),
                                                                         'claude_config_dir': str(case.claude_env),
                                                                         'python': sys.executable}):
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                self.refresh(agent_selection='qualified')
        self.assertEqual(case.state(), case.original)
        self.assertFalse((case.directory() / refresh.BASE).exists())
        self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES}, case.original_files)

    def test_qualified_refresh_admits_an_audited_replacement_and_freezes_the_checked_identity(self):
        case = self.case
        artifact = case.qualification_artifact()
        newer = copy.deepcopy(artifact)
        newer['revision'] += 1
        newer['families']['opus']['minimum_claude_code_version'] = '2.1.300'
        case.configure_qualification(newer)
        replacement = case.root / 'replacement-claude'
        replacement.write_text('#!' + sys.executable + '\nimport sys\n'
                               'print("2.1.300 (Claude Code)" if sys.argv[1:] == ["--version"] else "unexpected")\n')
        replacement.chmod(0o700)
        launch = case.root / 'claude-launch'
        launch.symlink_to(replacement)
        case.cli.unlink()
        ao_executable_binding.bind(case.service, case.room, 'executable-repair', str(replacement), str(launch),
                                   str(case.database), 'Authorized synthetic executable repair',
                                   'Synthetic original binary removed')
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(newer))
        self.assertEqual(record['evidence']['current_executable']['path'], str(replacement))
        self.assertEqual(record['evidence']['current_executable']['version'], '2.1.300 (Claude Code)')
        self.assertEqual(record['evidence']['executable_binding'], case.state()['executable_binding'])
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared)
                         ['worker_qualification']['snapshot']['sha256'], qmod.digest(newer))
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_pending_qualified_refresh_refuses_a_changed_effective_executable_before_commit(self):
        case = self.case
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.refresh(agent_selection='qualified')
        recorded = case.prepared['routing']['claude']
        original = case.cli.read_bytes()
        try:
            case.cli.write_bytes(original + b'# changed pinned executable bytes\n')
            with self.assertRaisesRegex(ao.RoomError, 'changed'):
                self.refresh(agent_selection='qualified')
            self.assertEqual(case.state(), case.original)
            self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES},
                             case.original_files)
        finally:
            case.cli.write_bytes(original)
            os.utime(case.cli, ns=(recorded['mtime_ns'], recorded['mtime_ns']))
        self.assertFalse(self.refresh(agent_selection='qualified')['idempotent'])
        ao_delegates.validate_preparation(case.directory(), case.state())

    def test_bound_replacement_sidecar_version_change_refuses_without_committing(self):
        case = self.case
        artifact = copy.deepcopy(case.qualification_artifact())
        artifact['revision'] += 1
        artifact['families']['opus']['minimum_claude_code_version'] = '2.1.300'
        case.configure_qualification(artifact)
        sidecar = case.root / 'bound-claude-version.txt'
        sidecar.write_text('2.1.300 (Claude Code)')
        replacement = case.root / 'bound-replacement-claude'
        replacement.write_text('#!' + sys.executable + chr(10) + 'print(__import__(' + repr('pathlib') + ').Path('
                             + repr(str(sidecar)) + ').read_text().strip())' + chr(10))
        replacement.chmod(0o700)
        launch = case.root / 'claude-launch'
        launch.symlink_to(replacement)
        case.cli.unlink()
        ao_executable_binding.bind(case.service, case.room, 'executable-repair', str(replacement), str(launch),
                                   str(case.database), 'Authorized synthetic executable repair',
                                   'Synthetic original binary removed')
        bound = case.state()
        bound_files = {name: (case.repo / name).read_bytes() for name in ao_routing.FILES}
        self.assertEqual(ao_executable_binding.effective(case.directory(), bound, case.prepared)['version'],
                         '2.1.300 (Claude Code)')
        sidecar.write_text('2.1.200 (Claude Code)')
        try:
            # The bound launcher's bytes and hash are unchanged; only the version it reports moved.
            # Re-probing the effective bound path refuses the target's own floor before any commit.
            with self.assertRaisesRegex(ao.RoomError, 'requires Claude Code 2.1.300 or newer'):
                self.refresh(agent_selection='qualified')
            self.assertEqual(case.state(), bound)
            self.assertFalse((case.directory() / refresh.BASE).exists())
            self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES}, bound_files)
        finally:
            sidecar.write_text('2.1.300 (Claude Code)')
        result = self.refresh(agent_selection='qualified')
        record = refresh._read(case.directory(), case.state()['routing_refresh'])
        self.assertFalse(result['model_dispatch'])
        self.assertEqual(record['evidence']['current_executable']['version'], '2.1.300 (Claude Code)')
        self.assertEqual(record['target']['worker_qualification']['snapshot']['sha256'], qmod.digest(artifact))
        ao_delegates.validate_preparation(case.directory(), case.state())


class SidecarExecutableRefreshTests(unittest.TestCase):
    """A refresh case whose pinned launcher reports --version from a mutable sidecar file.

    The launcher bytes and metadata never change after preparation; only the sidecar does, so the
    pinned-executable identity mismatch is reproduced without a bridge, a mock or fabricated
    metadata.
    """

    def setUp(self):
        self.case = refresh_tests.RoutingRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        case = self.case
        original_bind = case.bind

        def bind_with_sidecar_launcher():
            self.sidecar = case.root / 'fake-claude-version.txt'
            self.sidecar.write_text('2.1.282 (Claude Code)')
            case.cli.write_text('#!' + sys.executable + chr(10) + 'print(__import__(' + repr('pathlib') + ').Path('
                                + repr(str(self.sidecar)) + ').read_text().strip())' + chr(10))
            case.cli.chmod(0o700)
            return original_bind()

        case.bind = bind_with_sidecar_launcher
        case.setUp()

    def runtime_files(self):
        return {name: (self.case.repo / name).read_bytes() for name in ao_routing.FILES}

    def test_pinned_sidecar_version_change_refuses_without_touching_runtime_or_committed_state(self):
        case = self.case
        launcher = case.cli.stat()
        launcher_bytes = case.cli.read_bytes()
        self.assertEqual(ao_routing.claude_evidence(str(case.cli))['version'], '2.1.282 (Claude Code)')
        artifact = copy.deepcopy(case.qualification_artifact())
        artifact['revision'] += 1
        artifact['families']['opus']['minimum_claude_code_version'] = '2.1.300'
        case.configure_qualification(artifact)
        runtime = self.runtime_files()
        self.sidecar.write_text('2.1.300 (Claude Code)')
        try:
            # The pinned launcher's bytes and metadata are byte-identical; changing only the sidecar
            # makes the live --version disagree with the retained effective authority (2.1.282),
            # so the refresh refuses and neither runtime files nor committed state change.
            with self.assertRaisesRegex(ao.RoomError, 'no longer reports the version recorded'):
                case.do_refresh(agent_selection='qualified')
            self.assertEqual(case.state(), case.original)
            self.assertFalse((case.directory() / refresh.BASE).exists())
            self.assertEqual(self.runtime_files(), runtime)
        finally:
            self.sidecar.write_text('2.1.282 (Claude Code)')
        current = case.cli.stat()
        self.assertEqual((case.cli.read_bytes(), current.st_size, current.st_mtime_ns),
                         (launcher_bytes, launcher.st_size, launcher.st_mtime_ns))


if __name__ == '__main__':
    unittest.main()
