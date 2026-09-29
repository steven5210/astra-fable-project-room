"""Family worker selectors and retained exact-pin compatibility.

Synthetic Git repositories, a version-only fake Claude executable and the in-process
fake AO only; no model, account or network is used. Aliases are configured intent:
nothing here observes or attributes a served child model or effective effort.
"""

import copy
import json
import sys
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_routing
import ao_routing_refresh as refresh
import ao_workflow
from test_ao_normal import Fixture
import test_ao_routing_refresh as refresh_tests

EXACT = {'pr-sonnet': 'claude-sonnet-5', 'pr-opus': 'claude-opus-5'}
FAMILY = {'pr-sonnet': 'sonnet', 'pr-opus': 'opus'}
SELECTION = {'pr-sonnet': {'kind': 'family', 'family': 'sonnet'}, 'pr-opus': {'kind': 'family', 'family': 'opus'}}
BASIS = 'configured family selector; child execution identity is not attributed by the controller'
QUALIFIED_WORKERS = {'pr-sonnet': 'claude-sonnet-5-5', 'pr-opus': 'claude-opus-5-5'}
# The routing part an exact-pin v2 preparation delivered before family selectors existed
# (SHA-256 of the unchanged renderer's bytes, recorded before this change).
EXACT_V2_ROUTING_SHA256 = '7c53eca9d427c66c55d05d46197cc3b514d25cc2f55ef95b20d5bffb4b6f5041'


def historical_exact(test):
    """Rewrite a fresh source-qualified preparation into the retained exact-pin shape recorded before family selectors.

    Only each agent's model line and the recorded selector fields differ from a fresh render. The file
    pins and preparation digest are then recorded exactly as the historical writer recorded them. Old
    format fixtures build their own saved historical bytes here, in a scoped fixture, instead of
    weakening the new source-qualified runtime requirement globally.
    """
    return refresh_tests.historical_routing(test, EXACT, None, None)


def historical_family(test):
    """Rewrite a fresh source-qualified preparation into the retained v2 family-alias shape."""
    return refresh_tests.historical_routing(test, FAMILY, SELECTION, BASIS)


def prepare_family(test):
    """Every fresh preparation made in this context keeps the historical family-alias selectors."""
    original = test.service.ao_room_prepare

    def prepare(*args, **kwargs):
        original(*args, **kwargs)
        return historical_family(test)

    return patch.object(test.service, 'ao_room_prepare', side_effect=prepare)


def prepare_exact(test):
    """Every fresh preparation made in this context keeps the historical exact pins."""
    original = test.service.ao_room_prepare

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        return prepared if prepared['routing']['agents'] == EXACT else historical_exact(test)

    return patch.object(test.service, 'ao_room_prepare', side_effect=prepare)


def agent_fields(test, name):
    return ao_routing.parse_definition((test.repo / '.claude/agents' / (name + '.md')).read_text())


class SelectorFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.fake_claude = self.root / 'fake-claude-version'
        self.fake_claude.write_text('#!' + sys.executable + '\nimport sys\n'
                                    'print("2.1.282 (Claude Code)" if sys.argv[1:] == ["--version"] else "unexpected")\n')
        self.fake_claude.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.fake_claude), 'claude_config_dir': str(self.claude_env)})
        self.room = self.open(provider='none'); self.spec()

    def prepared(self):
        return ao.read(self.directory() / self.state()['preparation'])

    def routing(self):
        return self.service.ao_room_status(self.room)['delegate']['routing']

    def prepare(self, exact=False, qualified=False):
        if qualified:
            return self.service.ao_room_prepare(self.room, str(self.repo))
        if exact:
            with prepare_exact(self):
                return self.service.ao_room_prepare(self.room, str(self.repo))
        with prepare_family(self):
            return self.service.ao_room_prepare(self.room, str(self.repo))

    def assert_tampering_refuses(self, prepared, agents):
        other = {'pr-sonnet': 'pr-opus', 'pr-opus': 'pr-sonnet'}
        for name, model in agents.items():
            relative = '.claude/agents/' + name + '.md'
            path = self.repo / relative
            original = path.read_bytes()
            line = 'model: ' + json.dumps(model)
            replacements = [(line, 'model: "claude-' + FAMILY[name] + '-5-5"', 'pinned mapping'),  # a fixed newer id
                            (line, 'model: ' + json.dumps(EXACT[name] if agents == FAMILY else FAMILY[name]), 'pinned mapping'),
                            (line, 'model: ' + json.dumps(agents[other[name]]), 'pinned mapping'),
                            (line, 'model: "' + FAMILY[name] + '[1m]"', 'pinned mapping'),
                            (line, 'model: "inherit"', 'inherits'), (line, 'model: "fable"', 'Fable'),
                            (line, 'model: "claude-fable-5-1"', 'Fable'), (line + '\n', '', 'omits'),
                            ('effort: "max"', 'effort: "high"', 'effort')]
            for old, new, message in replacements:
                with self.subTest(agent=name, new=new or 'omitted'):
                    self.assertIn(old.encode(), original)
                    tampered = original.replace(old.encode(), new.encode())
                    path.write_bytes(tampered)
                    try:
                        with self.assertRaisesRegex(ao.RoomError, 'Pinned routing file changed'):
                            ao_routing.validate_local(prepared)
                        repinned = copy.deepcopy(prepared)  # even a consistent record cannot pin another selector
                        repinned['routing']['files'][relative] = ao.digest(tampered)
                        with self.assertRaisesRegex(ao.RoomError, message):
                            ao_routing.validate_local(repinned)
                    finally:
                        path.write_bytes(original)
        self.assertEqual(ao_routing.validate_local(prepared), prepared['routing'])


class FamilySelectorTests(SelectorFixture):
    def test_historical_family_preparation_keeps_family_selectors_and_rendering(self):
        prepared = self.prepare()
        routing = prepared['routing']
        self.assertEqual((routing['agents'], routing['agent_selection'], routing['agent_identity_basis'], routing['effort']),
                         (FAMILY, SELECTION, BASIS, 'max'))
        for name, model in FAMILY.items():
            self.assertEqual((agent_fields(self, name)['model'], agent_fields(self, name)['effort']), (model, 'max'))
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), routing)
        status = self.routing()
        self.assertEqual((status['status'], status['agents'], status['effort']), ('configured', FAMILY, 'max'))
        self.assertEqual(status['worker_selectors'], {'pr-sonnet': 'family alias sonnet', 'pr-opus': 'family alias opus'})
        for phrase in ('the controller does not attribute actual child model identity',
                       'effort max is configured intent, not observed effective effort',
                       "runs on the parent's exact model (an Opus 5.5 parent runs pr-opus on Opus 5.5)",
                       'such as sonnet under an Opus parent, resolves on its own'):
            self.assertIn(phrase, status['worker_identity'])
        text = ao_routing.packet_text(prepared)
        for phrase in ('pr-sonnet (family alias sonnet, bounded implementation and tests)',
                       'pr-opus (family alias opus, bounded judgment/review',
                       'the controller does not attribute actual child model identity',
                       'effort max is configured intent, not observed effective effort',
                       'an Opus 5.5 parent runs pr-opus on Opus 5.5',
                       'with the configured selector and any observed native model evidence; never report an alias as an observed model'):
            self.assertIn(phrase, text)
        self.assertNotIn('requested model', text)
        self.assertEqual(self.prepare(), prepared)  # re-preparation returns the immutable record

    def test_historical_exact_preparation_stays_readable_but_new_delegation_refuses(self):
        with prepare_exact(self):
            self.bind()
        prepared = self.prepared()
        before = (self.directory() / self.state()['preparation']).read_bytes()
        self.assertEqual(prepared['routing']['agents'], EXACT)
        self.assertNotIn('agent_selection', prepared['routing']); self.assertNotIn('agent_identity_basis', prepared['routing'])
        self.assertNotIn('worker_qualification', prepared['routing'])
        self.assertEqual(ao_routing.validate_local(prepared, self.state(), self.directory()), prepared['routing'])
        self.assertEqual(self.service.ao_room_prepare(self.room, str(self.repo)), prepared)  # never relabelled to family
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        self.assertEqual(ao.digest(ao_workflow.part_texts(prepared, policy)['routing'].encode()), EXACT_V2_ROUTING_SHA256)
        self.agree()  # a read-only specification review still reaches the exact-pin room
        self.assertEqual(self.state()['requests']['spec_review']['carried']['part_sha256']['routing'], EXACT_V2_ROUTING_SHA256)
        # New delegating execution requires the explicit source-qualified refresh; reading stays permitted.
        with self.assertRaisesRegex(ao.RoomError, 'source-qualified'):
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])
        status = self.routing()
        self.assertEqual((status['status'], status['agents']), ('verified', EXACT))
        self.assertEqual(status['worker_selectors'],
                         {'pr-sonnet': 'exact model id claude-sonnet-5', 'pr-opus': 'exact model id claude-opus-5'})
        self.assertIn('does not attribute actual child model identity', status['worker_identity'])
        self.assertNotIn('Opus 5.5', status['worker_identity'])
        self.assertNotIn('original_agents', status)
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), before)
        for name, model in EXACT.items():
            self.assertEqual(agent_fields(self, name)['model'], model)

    def test_tampered_family_definitions_refuse(self):
        self.assert_tampering_refuses(self.prepare(), FAMILY)

    def test_tampered_exact_definitions_refuse(self):
        self.assert_tampering_refuses(self.prepare(exact=True), EXACT)

    def test_recorded_selector_map_must_be_one_of_the_two_supported_maps(self):
        prepared = self.prepare()
        changes = [(lambda r: r.update(agents={'pr-sonnet': 'sonnet', 'pr-opus': 'claude-opus-5-5'}), 'neither'),
                   (lambda r: r.update(agents={'pr-sonnet': 'claude-sonnet-5', 'pr-opus': 'opus'}), 'neither'),
                   (lambda r: r.update(agents={**FAMILY, 'pr-haiku': 'haiku'}), 'neither'),
                   (lambda r: r.update(agents={'pr-sonnet': 'inherit', 'pr-opus': 'inherit'}), 'neither'),
                   (lambda r: r.update(agents={'pr-sonnet': 'fable', 'pr-opus': 'fable'}), 'neither'),
                   (lambda r: r.update(agents=['sonnet', 'opus']), 'neither'),
                   (lambda r: r.pop('agents'), 'neither'),
                   (lambda r: r.update(agents=dict(EXACT)), 'neither'),  # exact pins never carry family selection records
                   (lambda r: r.pop('agent_selection'), 'selection and identity basis'),
                   (lambda r: r['agent_selection']['pr-opus'].update(family='fable'), 'selection and identity basis'),
                   (lambda r: r.update(agent_identity_basis='served by Opus 5.5'), 'selection and identity basis'),
                   (lambda r: r.update(effort='high'), 'effort is not max')]
        for index, (change, message) in enumerate(changes):
            with self.subTest(case=index):
                changed = copy.deepcopy(prepared)
                change(changed['routing'])
                with self.assertRaisesRegex(ao.RoomError, message):
                    ao_routing.validate_local(changed)
                self.assertEqual(ao_routing.routing_status(changed, {})['status'], 'unverified')
        text = (self.repo / '.claude/agents/pr-opus.md').read_text()
        for agents in ({'pr-sonnet': 'sonnet', 'pr-opus': 'claude-opus-5-5'}, {}, {'pr-opus': 'opus'}):
            with self.assertRaisesRegex(ao.RoomError, 'neither'):
                ao_routing.validate_definition('pr-opus', text, agents)
        ao_routing.validate_definition('pr-opus', text, FAMILY)
        with self.assertRaisesRegex(ao.RoomError, 'pinned mapping'):
            ao_routing.validate_definition('pr-opus', text, EXACT)

    def assert_restrictions(self, agents, admitted, refused):
        settings = self.claude_env / 'settings.json'
        project = self.repo / '.claude/settings.json'
        for allowed in admitted:
            with self.subTest(allowed=allowed):
                settings.write_text(json.dumps({'availableModels': allowed}))
                status = self.routing()
                self.assertEqual(status['status'], 'configured', status.get('error'))
        for allowed in refused:
            for path in (settings, project):
                with self.subTest(allowed=allowed, source=path.name):
                    settings.unlink(missing_ok=True)
                    path.write_text(json.dumps({'availableModels': allowed}))
                    self.assertIn('availableModels', self.routing()['error'])
                    path.unlink()
        for content, message in (({'modelOverrides': {agents['pr-opus']: 'claude-fable-5-1'}}, 'modelOverrides'),
                                 ({'env': {'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'claude-opus-5-5'}}, 'override')):
            settings.write_text(json.dumps(content))
            self.assertIn(message, self.routing()['error'])
        settings.unlink()
        self.assertEqual(self.routing()['status'], 'configured')
        routing = self.prepared()['routing']
        local = json.loads((self.repo / '.claude/settings.local.json').read_text())
        ao_routing.check_settings({**local, 'availableModels': list(agents.values())}, routing)
        with self.assertRaisesRegex(ao.RoomError, 'availableModels'):
            ao_routing.check_settings({**local, 'availableModels': list((FAMILY if agents == EXACT else EXACT).values())}, routing)

    def test_available_models_restrictions_require_family_aliases_for_historical_family_preparations(self):
        local = self.repo / '.claude/settings.local.json'; local.parent.mkdir()
        # A fresh source-qualified preparation requires its own exact qualified models admitted...
        local.write_text(json.dumps({'availableModels': ['claude-fable-5-1', 'sonnet', 'opus']}))
        with self.assertRaisesRegex(ao.RoomError, 'availableModels'):
            self.prepare(qualified=True)
        self.assertIsNone(self.state().get('preparation'))
        # ...and the retained historical family-alias shape additionally requires its aliases.
        local.write_text(json.dumps({'availableModels': ['claude-fable-5-1', 'sonnet', 'opus',
                                                         *QUALIFIED_WORKERS.values()]}))
        self.prepare()
        self.assert_restrictions(FAMILY, admitted=(['sonnet', 'opus'], ['claude-opus-5-5', 'sonnet', 'opus', 'claude-sonnet-5']),
                                 refused=(list(EXACT.values()), ['sonnet'], ['opus', 'claude-sonnet-5'], 'sonnet, opus'))

    def test_available_models_restrictions_keep_exact_ids_for_historical_preparations(self):
        self.prepare(exact=True)
        self.assert_restrictions(EXACT, admitted=(list(EXACT.values()), ['claude-fable-5-1', *EXACT.values(), 'opus']),
                                 refused=(list(FAMILY.values()), ['claude-sonnet-5'], ['claude-opus-5-5', 'claude-sonnet-5']))

    def test_family_alias_remaps_refuse_preparation_status_and_delegating_dispatch(self):
        user = self.claude_env / 'settings.json'
        for key in ao_routing.ALIAS_ENV:  # a remapped alias could leave its family (even for a Fable model)
            for source in ('AO project', 'user settings'):
                with self.subTest(key=key, source=source):
                    try:
                        if source == 'AO project':
                            self.fake.config['env'] = {key: 'claude-fable-5-1'}
                        else:
                            user.write_text(json.dumps({'env': {key: 'claude-fable-5-1'}}))
                        with self.assertRaisesRegex(ao.RoomError, key):
                            self.prepare()
                        self.assertIsNone(self.state().get('preparation'))
                    finally:
                        # Cleanup regardless of the assertion: a leaked remap would contaminate
                        # every later subtest and the final supported sync.
                        self.fake.config['env'] = {}
                        user.unlink(missing_ok=True)
        self.fake.config['env'] = {key: '' for key in ao_routing.ALIAS_ENV}  # empty values leave the aliases unmapped
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo))
        self.fake.config['env'] = {}
        for key in ao_routing.ALIAS_ENV:
            with self.subTest(key=key, source='user settings after preparation'):
                retained = self.state()['routing_rules']
                try:
                    user.write_text(json.dumps({'env': {key: 'claude-opus-5'}}))
                    self.assertIn(key, self.routing()['error'])
                    with self.assertRaisesRegex(ao.RoomError, key):
                        self.send('implementation')
                    # Local user-settings validation refuses before before_dispatch's observation
                    # catch, so the retained routing-rules record is never rewritten.
                    self.assertEqual(self.state()['routing_rules'], retained)
                finally:
                    user.unlink(missing_ok=True)
            with self.subTest(key=key, source='AO project after preparation'):
                try:
                    self.fake.config['env'] = {key: 'glm-4'}
                    with self.assertRaisesRegex(ao.RoomError, key):
                        self.send('implementation')
                    # The AO-project refusal is caught by the dispatch observation path itself,
                    # which records a fresh inconsistent observation naming the remapped key.
                    observed = self.state()['routing_rules']
                    self.assertEqual(observed['source'], 'dispatch')
                    self.assertFalse(observed['consistent'])
                    self.assertIn(key, observed['error'])
                finally:
                    self.fake.config['env'] = {}
        self.assertNotIn('implementation', self.state()['requests'])
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.routing()['status'], 'verified')

    def test_historical_family_routing_refuses_delegation_before_its_first_handoff(self):
        """The retained historical family-alias record is built before this room's first handoff.

        The rewrite is scoped to a case whose handoff does not exist yet, so the recorded preparation
        bytes and the handoff record stay consistent; the unqualified routing boundary then refuses a
        new delegating dispatch by name.
        """
        self.bind(); self.agree()
        historical_family(self)
        self.service.ao_room_handoff(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'source-qualified'):
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])
        self.assertEqual(self.routing()['status'], 'verified')

    def test_alias_remap_checks_leave_historical_exact_rooms_unchanged(self):
        with prepare_exact(self):
            self.bind()
        self.agree()
        remaps = {key: 'unrelated-remap' for key in ao_routing.ALIAS_ENV}
        (self.claude_env / 'settings.json').write_text(json.dumps({'env': remaps}))
        self.fake.config['env'] = dict(remaps)
        self.assertEqual(self.routing()['status'], 'verified')  # exact ids are not subject to alias remapping
        self.service.ao_room_handoff(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'source-qualified'):
            self.send('implementation')


class HistoricalExactRefreshTests(refresh_tests.RoutingRefreshTests):
    """Every refresh contract rerun on a retained exact-pin room, plus its one audited selector upgrade."""

    def bind(self):
        with prepare_exact(self):
            super().bind()

    def upgrade(self, **changes):
        return self.do_refresh(**{'agent_selection': 'family', **changes})

    def test_fixture_is_a_retained_exact_pin_preparation(self):
        self.assertEqual(self.prepared['routing']['agents'], EXACT)
        self.assertNotIn('agent_selection', self.prepared['routing'])
        for name, model in EXACT.items():
            self.assertEqual(agent_fields(self, name)['model'], model)

    def test_unchanged_refresh_keeps_exact_pins_and_historical_input_shape(self):
        first = self.do_refresh()
        record = self.journal()
        self.assertEqual((record['source']['agents'], record['target']['agents']), (EXACT, EXACT))
        self.assertEqual(set(record['inputs']), {'request_id', 'database_path', 'native_session_id', 'authorization', 'diagnosis'})
        self.assertNotIn('agent_selection_change', record['evidence'])
        self.assertNotIn('agent_selection_change', first)
        self.assertNotIn('agent_selection', record['target'])
        for name, model in EXACT.items():
            self.assertEqual(agent_fields(self, name)['model'], model)
            # Only the recorded model line differs from a fresh family render of the same current instructions.
            self.assertEqual(record['target_bundle']['files']['.claude/agents/' + name + '.md'],
                             ao_routing.agent_definition(name).replace('model: "' + FAMILY[name] + '"', 'model: "' + model + '"'))
        # A later audited upgrade extends the same immutable chain from the exact target.
        self.upgrade(request_id='later-upgrade')
        later = self.journal()
        self.assertEqual(later['previous'], {'path': first['path'], 'sha256': first['sha256']})
        self.assertEqual(later['source'], record['target'])
        self.assertEqual((later['target']['agents'], later['target']['guard_sha256']), (FAMILY, record['target']['guard_sha256']))
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_family_upgrade_records_evidence_preserves_history_and_is_idempotent(self):
        result = self.upgrade()
        record = self.journal()
        self.assertEqual((record['source']['agents'], record['target']['agents']), (EXACT, FAMILY))
        self.assertEqual((record['target']['agent_selection'], record['target']['agent_identity_basis']), (SELECTION, BASIS))
        self.assertNotIn('agent_selection', record['source'])
        self.assertEqual((record['source']['effort'], record['target']['effort']), ('max', 'max'))
        self.assertEqual(record['inputs']['agent_selection'], 'family')
        change = record['evidence']['agent_selection_change']
        self.assertEqual((change['from'], change['to'], change['effort'], change['selection'], change['identity_basis']),
                         (EXACT, FAMILY, 'max', SELECTION, BASIS))
        self.assertIn('no child identity is attributed', change['meaning'])
        self.assertEqual(result['agent_selection_change'], change)
        self.assertFalse(result['model_dispatch'])
        self.assertEqual(self.fake.posts, self.posts)
        self.assertEqual({key: value for key, value in self.state().items() if key != 'routing_refresh'}, self.original)
        self.assertEqual((self.directory() / self.state()['preparation']).read_bytes(), self.prepared_bytes)
        routing = refresh.effective(self.directory(), self.state(), self.prepared)
        self.assertEqual(routing, record['target'])
        self.assertEqual(ao_routing.validate_local(self.prepared, self.state(), self.directory()), routing)
        for name, model in FAMILY.items():
            self.assertEqual((agent_fields(self, name)['model'], agent_fields(self, name)['effort']), (model, 'max'))
        status = self.service.ao_room_status(self.room)['delegate']['routing']
        self.assertEqual((status['original_agents'], status['agents']), (EXACT, FAMILY))
        self.assertEqual(status['worker_selectors'], {'pr-sonnet': 'family alias sonnet', 'pr-opus': 'family alias opus'})
        self.assertIn('an Opus 5.5 parent runs pr-opus on Opus 5.5', status['worker_identity'])
        state_bytes = (self.directory() / 'state.json').read_bytes()
        raw = (self.directory() / result['path']).read_bytes()
        self.assertTrue(self.upgrade()['idempotent'])
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_bytes)
        self.assertEqual((self.directory() / result['path']).read_bytes(), raw)
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.do_refresh()  # the same request without its selector change
        with self.assertRaisesRegex(ao.RoomError, 'already matches'):
            self.upgrade(request_id='second-upgrade')  # nothing remains to change
        self.assertEqual(len(refresh._entries(self.directory())), 1)
        # The v2 family-alias target is unqualified for new delegating execution: the next work unit
        # must select 'qualified' explicitly, while the delivered routing part is not resent.
        with self.assertRaisesRegex(ao.RoomError, 'source-qualified'):
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])

    def test_reverse_or_other_selector_requests_refuse_before_any_intent(self):
        for value in ('exact', 'opus', 'sonnet', 'inherit', 'claude-opus-5-5', 'fable', ''):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ao.RoomError, "only agent_selection 'family'"):
                    self.upgrade(agent_selection=value)
                self.assert_no_intent()

    def test_upgrade_requires_surrounding_restrictions_to_admit_family_aliases(self):
        settings = self.claude_env / 'settings.json'
        config = copy.deepcopy(self.fake.config)
        for content, project_env, message in (
                ({'availableModels': list(EXACT.values())}, {}, 'availableModels'),
                ({'env': {'ANTHROPIC_DEFAULT_SONNET_MODEL': 'claude-sonnet-5'}}, {}, 'ANTHROPIC_DEFAULT_SONNET_MODEL'),
                ({}, {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-5'}, 'ANTHROPIC_DEFAULT_OPUS_MODEL')):
            with self.subTest(message=message):
                settings.write_text(json.dumps(content))
                self.fake.config['env'] = {**config.get('env', {}), **project_env}
                ao_delegates.validate_preparation(self.directory(), self.state())  # the exact room itself remains valid
                with self.assertRaisesRegex(ao.RoomError, message):
                    self.upgrade()
                self.assert_no_intent()
                self.fake.config = copy.deepcopy(config)
        settings.write_text(json.dumps({'availableModels': [*EXACT.values(), *FAMILY.values()]}))
        self.upgrade()
        self.assertEqual(self.journal()['target']['agents'], FAMILY)
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_pending_upgrade_reconciles_only_its_identical_request(self):
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic crash after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.upgrade()
        self.assertEqual(self.state(), self.original)
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed'):
            ao_routing.validate_local(self.prepared, self.state(), self.directory())
        with self.assertRaisesRegex(ao.RoomError, 'different immutable inputs'):
            self.do_refresh()
        path = self.directory() / refresh.BASE / 'refresh-one.json'
        raw = path.read_bytes()
        forged = json.loads(raw)
        del forged['inputs']['agent_selection']  # an intent whose selector change lacks the explicit request
        path.write_text(json.dumps(forged))
        with self.assertRaisesRegex(ao.RoomError, 'lacks its explicit request'):
            self.do_refresh()
        path.write_bytes(raw)
        self.assertEqual({name: (self.repo / name).read_bytes() for name in ao_routing.FILES}, self.original_files)
        result = self.upgrade()
        self.assertFalse(result['idempotent'])
        self.assertEqual(self.journal()['target']['agents'], FAMILY)
        ao_delegates.validate_preparation(self.directory(), self.state())

    def test_forged_selector_changes_in_committed_history_refuse(self):
        self.upgrade()
        pointer = self.state()['routing_refresh']
        path = self.directory() / pointer['path']
        raw = path.read_bytes()

        def reverse(record):
            record['source'], record['target'] = record['target'], record['source']
            record['source_bundle'], record['target_bundle'] = record['target_bundle'], record['source_bundle']

        forgeries = [(reverse, 'may change worker models only from'),
                     (lambda r: r['target'].update(agents={'pr-sonnet': 'sonnet', 'pr-opus': 'claude-opus-5-5'}), 'neither'),
                     (lambda r: r['target'].update(effort='high'), 'complete pinned v2 or v3 MAX bundle'),
                     (lambda r: r['target']['agent_selection']['pr-opus'].update(family='fable'), 'selection and identity basis'),
                     (lambda r: r['evidence'].pop('agent_selection_change'), 'explicit request or recorded evidence'),
                     (lambda r: r['evidence']['agent_selection_change'].update(effort='high'), 'explicit request or recorded evidence'),
                     (lambda r: r['inputs'].pop('agent_selection'), 'explicit request or recorded evidence'),
                     (lambda r: r['inputs'].update(agent_selection='exact'), 'explicit request or recorded evidence')]
        for index, (forge, message) in enumerate(forgeries):
            with self.subTest(case=index):
                record = json.loads(raw)
                forge(record)
                ao.atomic(path, record)
                state = self.state()
                state['routing_refresh'] = {**pointer, 'sha256': ao.digest(record)}
                try:
                    with self.assertRaisesRegex(ao.RoomError, message):
                        refresh.effective(self.directory(), state, self.prepared)
                finally:
                    path.write_bytes(raw)
        self.assertEqual(refresh.effective(self.directory(), self.state(), self.prepared)['agents'], FAMILY)


class FamilyPreparationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.case = refresh_tests.RoutingRefreshTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        # The historical family-alias shape is built explicitly here, in a scoped fixture, rather
        # than weakening the new source-qualified preparation requirement globally.
        refresh_tests.historical_routing(self.case, ao_routing.FAMILY_AGENTS, ao_routing.AGENT_SELECTION,
                                         ao_routing.AGENT_IDENTITY_BASIS)
        self.case.prepared = ao_delegates.preparation(self.case.directory(), self.case.state())
        self.case.prepared_bytes = (self.case.directory() / self.case.state()['preparation']).read_bytes()
        self.case.original = self.case.state()
        self.case.original_files = {name: (self.case.repo / name).read_bytes() for name in ao_routing.FILES}

    def test_family_request_on_a_family_preparation_keeps_selectors(self):
        case = self.case
        self.assertEqual(case.prepared['routing']['agents'], FAMILY)
        result = case.do_refresh(agent_selection='family')
        record = case.journal()
        self.assertEqual((record['source']['agents'], record['target']['agents']), (FAMILY, FAMILY))
        self.assertEqual(record['inputs']['agent_selection'], 'family')
        self.assertNotIn('agent_selection_change', record['evidence'])
        self.assertNotIn('agent_selection_change', result)
        self.assertEqual({key: record['target'][key] for key in ('agent_selection', 'agent_identity_basis')},
                         {'agent_selection': SELECTION, 'agent_identity_basis': BASIS})
        ao_delegates.validate_preparation(case.directory(), case.state())


if __name__ == '__main__':
    unittest.main()
