"""Offline AO compaction defaults and historical preparation/adoption contracts.

Only synthetic Git repositories, a version-only fake Claude executable, and the
existing in-process fake AO/provider fixtures are used. No model or account is
contacted, and these tests make no claim about actual native compaction cost.
"""

import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_routing
import ao_routing_adoption as adoption
import ao_routing_guard
import ao_workflow
import test_ao_adoption as adoption_tests
import test_ao_normal as normal
import test_ao_routing_v1_fixture as frozen


WINDOW_ENV = 'CLAUDE_CODE_AUTO_COMPACT_WINDOW'
OVERRIDE_ENV = ('DISABLE_COMPACT', 'DISABLE_AUTO_COMPACT',
                'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE', 'CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE')
LEGACY_ENV = {'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH': '1',
              'CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS': '2',
              'CLAUDE_CODE_DISABLE_WORKFLOWS': '1',
              'CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS': '1'}
SOURCES = ('process', 'user', 'project', 'local', 'managed', 'ao')


def isolate_compaction_environment(test):
    """Never let real host compaction preferences affect synthetic preparations."""
    managed = test.root / 'synthetic-managed-settings.json'
    patcher = patch.dict(os.environ, {'CLAUDE_CODE_MANAGED_SETTINGS_PATH': str(managed)})
    patcher.start()
    test.addCleanup(patcher.stop)
    for key in (WINDOW_ENV, *OVERRIDE_ENV):
        os.environ.pop(key, None)
    test.managed_settings = managed


class CompactionFixture(normal.Fixture):
    def setUp(self):
        super().setUp()
        isolate_compaction_environment(self)
        self.fake_claude = self.root / 'fake-claude-version'
        self.claude_log = self.root / 'fake-claude-argv.jsonl'
        self.fake_claude.write_text(
            '#!' + sys.executable + '\nimport json,sys\nfrom pathlib import Path\n'
            'with Path(' + repr(str(self.claude_log)) + ').open("a") as stream:\n'
            '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'assert sys.argv[1:] == ["--version"], "inference is forbidden"\n'
            'print("0.0-fake (Claude Code)")\n')
        self.fake_claude.chmod(0o700)
        ao.atomic(self.home / 'config.json', {'claude_bin': str(self.fake_claude),
                                             'claude_config_dir': str(self.claude_env)})
        self.room = self.open(provider='none')
        self.spec()

    def prepare(self):
        return self.service.ao_room_prepare(self.room, str(self.repo))

    def set_window(self, value):
        ao.atomic(self.service.root / 'config.json', {'auto_compact_window': value})

    def settings(self):
        return ao.read(self.repo / '.claude/settings.local.json')

    def immutable_bytes(self, prepared):
        files = [self.directory() / self.state()['preparation'], Path(prepared['routing']['guard_path'])]
        files += [self.repo / relative for relative in prepared['routing']['files']]
        return {str(path): path.read_bytes() for path in files}

    @contextlib.contextmanager
    def settings_override(self, source, data):
        if source == 'process':
            with patch.dict(os.environ, data['env']):
                yield
            return
        if source == 'ao':
            previous = copy.deepcopy(self.fake.config)
            self.fake.config.update(copy.deepcopy(data))
            try:
                yield
            finally:
                self.fake.config = previous
            return
        paths = {'user': self.claude_env / 'settings.json',
                 'project': self.repo / '.claude/settings.json',
                 'local': self.repo / '.claude/settings.local.json',
                 'managed': self.managed_settings}
        path = paths[source]
        previous = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        try:
            yield
        finally:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)


class CompactionPreparationTests(CompactionFixture):
    def assert_window(self, selected):
        prepared = self.prepare()
        self.assertEqual(prepared['routing']['compaction'], {'version': 1, 'window': selected})
        self.assertEqual(prepared['routing']['env'], {**LEGACY_ENV, WINDOW_ENV: str(selected)})
        settings = self.settings()
        self.assertIs(settings['autoCompactEnabled'], True)
        self.assertEqual(settings['autoCompactWindow'], selected)
        self.assertIs(type(settings['autoCompactWindow']), int)
        self.assertEqual(settings['env'][WINDOW_ENV], str(selected))
        return prepared

    def test_new_default_preserves_max_guards_candidate_and_has_no_model_dispatch(self):
        candidate = ao.candidate_snapshot(self.repo)
        config = copy.deepcopy(self.fake.config)
        delegate = copy.deepcopy(self.state()['delegate'])
        prepared = self.assert_window(250000)
        routing = prepared['routing']
        self.assertEqual((routing['version'], routing['execution_policy'], routing['matcher']),
                         (2, 'orchestrator', '.*'))
        self.assertEqual(routing['effort'], 'max')
        self.assertEqual(routing['rules']['preserved_fields']['worker.model'], 'claude-fable-5-1')
        self.assertEqual(self.fake.snapshots['engineer']['settings']['reasoningEffort'], 'max')
        self.assertEqual(Path(routing['guard_path']).read_bytes(), Path(ao_routing_guard.__file__).read_bytes())
        for name, model in (('pr-sonnet', 'claude-sonnet-5'), ('pr-opus', 'claude-opus-5')):
            fields = ao_routing.parse_definition((self.repo / '.claude/agents' / (name + '.md')).read_text())
            self.assertEqual((fields['model'], fields['effort']), (model, 'max'))
        self.assertEqual(self.state()['delegate'], delegate)
        self.assertEqual(self.fake.config, config)
        self.assertEqual(ao.candidate_snapshot(self.repo), candidate)
        self.assertEqual(self.fake.posts, [])
        self.assertEqual([json.loads(line) for line in self.claude_log.read_text().splitlines()], [['--version']])
        self.assertEqual(ao_routing.validate_local(prepared), routing)

    def test_lower_bound_is_supported(self):
        self.set_window(100000)
        self.assert_window(100000)

    def test_upper_bound_is_supported(self):
        self.set_window(1000000)
        self.assert_window(1000000)

    def test_custom_default_comes_from_ao_specific_configuration(self):
        self.set_window(425000)
        shared = ao.read(self.home / 'config.json')
        ao.atomic(self.home / 'config.json', {**shared, 'auto_compact_window': 700000})
        self.assert_window(425000)

    def test_invalid_windows_refuse_without_preparing_or_dispatching(self):
        for value in (99999, 1000001, 0, -1, True, False, '250000', 250000.0, None, [], {}):
            with self.subTest(value=value):
                self.set_window(value)
                with self.assertRaisesRegex(ao.RoomError, 'integer from 100000 to 1000000'):
                    self.prepare()
                self.assertIsNone(self.state().get('preparation'))
                self.assertFalse((self.repo / '.claude/settings.local.json').exists())
        self.assertEqual(self.fake.posts, [])

    def test_reprepare_keeps_exact_saved_default_and_never_rereads_future_policy(self):
        prepared = self.assert_window(250000)
        before = self.immutable_bytes(prepared)
        state_bytes = (self.directory() / 'state.json').read_bytes()
        self.set_window(500000)
        with patch.object(ao_routing, 'compaction_policy', side_effect=AssertionError('old preparation must stay pinned')):
            self.assertEqual(self.prepare(), prepared)
            self.assertEqual(ao_routing.validate_local(prepared), prepared['routing'])
        self.assertEqual(self.immutable_bytes(prepared), before)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_bytes)

    def test_runtime_setting_tampering_is_rejected(self):
        prepared = self.prepare()
        path = self.repo / '.claude/settings.local.json'
        original = path.read_bytes()
        for change in ('enabled', 'window', 'environment'):
            with self.subTest(change=change):
                settings = json.loads(original)
                if change == 'enabled':
                    settings['autoCompactEnabled'] = False
                elif change == 'window':
                    settings['autoCompactWindow'] = 600000
                else:
                    settings['env'][WINDOW_ENV] = '600000'
                path.write_text(json.dumps(settings))
                with self.assertRaises(ao.RoomError):
                    ao_routing.validate_local(prepared)
                path.write_bytes(original)

    def test_all_nonempty_disable_and_threshold_overrides_refuse_in_each_source(self):
        for source in SOURCES:
            for key in OVERRIDE_ENV:
                with self.subTest(source=source, key=key):
                    with self.settings_override(source, {'env': {key: '0'}}):
                        with self.assertRaisesRegex(ao.RoomError, 'overrides or disables automatic compaction'):
                            self.prepare()
                    self.assertIsNone(self.state().get('preparation'))
        self.assertEqual(self.fake.posts, [])

    def test_conflicting_explicit_window_refuses_in_each_source(self):
        for source in SOURCES:
            with self.subTest(source=source):
                with self.settings_override(source, {'env': {WINDOW_ENV: '350000'}}):
                    with self.assertRaisesRegex(ao.RoomError, 'conflicts with the prepared compaction window'):
                        self.prepare()
                self.assertIsNone(self.state().get('preparation'))
        self.assertEqual(self.fake.posts, [])

    def test_window_environment_must_be_the_selected_plain_integer_string(self):
        for value in ('0250000', '250000.0', ' 250000', '250000 ', 250000, True, None, ''):
            with self.subTest(value=value):
                with self.settings_override('local', {'env': {WINDOW_ENV: value}}):
                    with self.assertRaisesRegex(ao.RoomError, 'conflicts with the prepared compaction window'):
                        self.prepare()
                self.assertIsNone(self.state().get('preparation'))

    def test_matching_environment_and_empty_overrides_are_compatible(self):
        env = {WINDOW_ENV: '250000', **{key: '' for key in OVERRIDE_ENV}}
        with contextlib.ExitStack() as stack:
            for source in SOURCES:
                stack.enter_context(self.settings_override(source, {'env': env}))
            self.assert_window(250000)

    def test_disabled_settings_refuse_before_preparation(self):
        for source in ('user', 'project', 'local', 'managed'):
            with self.subTest(source=source):
                with self.settings_override(source, {'autoCompactEnabled': False}):
                    with self.assertRaisesRegex(ao.RoomError, 'settings disable automatic compaction'):
                        self.prepare()
                self.assertIsNone(self.state().get('preparation'))

    def test_surrounding_settings_cannot_disable_a_prepared_window(self):
        prepared = self.prepare()
        for source in ('user', 'project', 'managed'):
            with self.subTest(source=source):
                with self.settings_override(source, {'autoCompactEnabled': False}):
                    with self.assertRaisesRegex(ao.RoomError, 'settings disable automatic compaction'):
                        ao_routing.validate_local(prepared)
        self.assertEqual(self.fake.posts, [])

    def test_new_preparation_checks_current_ao_environment_at_dispatch_and_sync(self):
        prepared = self.prepare()
        for env in ({WINDOW_ENV: '350000'}, {'DISABLE_AUTO_COMPACT': '1'}):
            with self.subTest(env=env):
                self.fake.config['env'] = env
                with self.assertRaises(ao.RoomError):
                    ao_routing.before_dispatch(self.service, self.directory(), self.state(), prepared, 'implementation')
                ao_routing.observe_on_sync(self.service, self.directory(), self.state())
                self.assertFalse(self.state()['routing_rules']['consistent'])
        self.fake.config['env'] = {WINDOW_ENV: '250000'}
        ao_routing.observe_on_sync(self.service, self.directory(), self.state())
        self.assertTrue(self.state()['routing_rules']['consistent'])
        self.assertEqual(self.fake.posts, [])


class LegacyRenderingTests(unittest.TestCase):
    def test_shared_legacy_settings_renderer_does_not_add_compaction_defaults(self):
        command = '/synthetic/python /synthetic/guard.py'
        existing = {'env': {'PRESERVED': 'value'}, 'autoCompactEnabled': False}
        expected = {'env': {**LEGACY_ENV, 'PRESERVED': 'value'}, 'autoCompactEnabled': False,
                    'permissions': {'deny': ['Workflow', 'Skill(code-review)', 'Skill(simplify)', 'Skill(security-review)']},
                    'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [{'type': 'command', 'command': command, 'timeout': 30}]}]}}
        self.assertEqual(ao_routing.settings_document(copy.deepcopy(existing), command), expected)
        self.assertEqual(ao_routing.ENV, LEGACY_ENV)
        self.assertNotIn(WINDOW_ENV, ao_routing.RECORDED_ENV)


class HistoricalCompactionTests(CompactionFixture):
    install_frozen_routing = frozen.FrozenV1RoutingTests.install_frozen_routing

    def historical(self, version):
        if version == 1:
            self.fixture = json.loads((frozen.FIXTURES / 'fixture.json').read_text())
            with patch.object(ao_routing, 'prepare', self.install_frozen_routing):
                return self.prepare()
        # The v2 guard is unchanged. Restore the complete pre-compaction v2
        # settings/preparation shape, then repin only these synthetic bytes.
        prepared = self.prepare()
        prepared['routing'].pop('compaction')
        prepared['routing']['env'].pop(WINDOW_ENV)
        settings = self.settings()
        settings.pop('autoCompactEnabled')
        settings.pop('autoCompactWindow')
        settings['env'].pop(WINDOW_ENV)
        path = self.repo / '.claude/settings.local.json'
        ao.atomic(path, settings)
        prepared['routing']['files']['.claude/settings.local.json'] = ao.digest(path.read_bytes())
        ao.atomic(self.directory() / 'preparation.json', prepared)
        state = self.state()
        state['preparation_sha256'] = ao.digest(prepared)
        ao.atomic(self.directory() / 'state.json', state)
        return prepared

    def assert_historical_compatibility(self, version):
        prepared = self.historical(version)
        self.assertNotIn('compaction', prepared['routing'])
        self.assertEqual(prepared['routing']['env'], LEGACY_ENV)
        before = self.immutable_bytes(prepared)
        policy = ao_delegates.validate_provider(self.directory(), self.state())
        workflow = ao_workflow.part_texts(prepared, policy)
        if version == 1:
            self.assertEqual(workflow, self.fixture['workflow_parts'])
        self.set_window(99999)  # Invalid future preferences do not relabel history.
        self.fake.config['env'] = {WINDOW_ENV: '350000', 'DISABLE_AUTO_COMPACT': '1'}
        with self.settings_override('user', {'autoCompactEnabled': False}):
            with patch.object(ao_routing, 'compaction_policy', side_effect=AssertionError('historical settings must not be reread')):
                self.assertEqual(self.prepare(), prepared)
                self.assertEqual(ao_routing.validate_local(prepared), prepared['routing'])
                observed = ao_routing.observe_rules(self.fake, self.state())
                self.assertTrue(ao_routing.rules_match(observed, prepared['routing']['rules']))
                ao_routing.before_dispatch(self.service, self.directory(), self.state(), prepared, 'implementation')
                ao_routing.observe_on_sync(self.service, self.directory(), self.state())
        self.assertTrue(self.state()['routing_rules']['consistent'])
        self.assertEqual(ao_workflow.part_texts(prepared, policy), workflow)
        self.assertEqual(self.immutable_bytes(prepared), before)
        self.assertEqual(self.fake.posts, [])

    def test_frozen_v1_and_its_workflow_bytes_remain_compatible(self):
        self.assert_historical_compatibility(1)

    def test_pre_compaction_v2_remains_compatible_without_rewriting_saved_evidence(self):
        self.assert_historical_compatibility(2)


class HistoricalAdoptionCompactionTests(adoption_tests.AdoptionFixture):
    def setUp(self):
        # This fixture's setup builds an actual frozen v1 preparation and uses
        # only fake transport plus synthetic provider keys in its temp home.
        super().setUp()
        isolate_compaction_environment(self)

    def test_pending_adoption_reconstruction_does_not_acquire_new_defaults(self):
        with patch.object(ao_routing, 'compaction_policy', side_effect=AssertionError('adoption must keep historical rendering')):
            result = self.stage_routing()
            state = self.state()
            request_path = self.directory() / state['routing_adoption']['receipt']
            request = request_path.read_bytes()
            staged_path = self.directory() / adoption.REL / 'preparation.json'
            prepared = ao.read(staged_path)
            self.assertNotIn('compaction', prepared['routing'])
            self.assertEqual(prepared['routing']['env'], LEGACY_ENV)
            settings = ao.read(self.repo / '.claude/settings.local.json')
            for key in ('autoCompactEnabled', 'autoCompactWindow'):
                self.assertNotIn(key, settings)
            self.assertNotIn(WINDOW_ENV, settings['env'])
            staged = staged_path.read_bytes()
            ao.atomic(self.service.root / 'config.json', {'auto_compact_window': 99999})
            self.assertEqual(adoption.stage(*self.stage_args), result)
            self.assertEqual(staged_path.read_bytes(), staged)
            self.assertEqual(request_path.read_bytes(), request)
            self.fake.config = ao.read(Path(result['project_config_payload']))['config']
            adoption.activate(self.service, self.room, 'routing-change')
        self.assertEqual(self.state()['routing_adoption']['phase'], 'configured')
        self.assertEqual(self.state()['requests'], self.original['requests'])
        self.assertEqual(self.state()['bindings'], self.original['bindings'])
        self.assertEqual(len(self.fake.posts), self.posts_before)

    def test_configured_adoption_accepts_unrecorded_project_compaction_environment(self):
        prepared = self.configure_routing()
        self.assertNotIn('compaction', prepared['routing'])
        path = self.directory() / self.state()['preparation']
        before = path.read_bytes()
        self.fake.config['env'] = {WINDOW_ENV: '250000'}
        with patch.object(ao_routing, 'compaction_policy', side_effect=AssertionError('old adoption is immutable')):
            adoption.validate(self.service, self.state())
            observed = ao_routing.observe_rules(self.fake, self.state())
            self.assertTrue(ao_routing.rules_match(observed, prepared['routing']['rules']))
            ao_routing.before_dispatch(self.service, self.directory(), self.state(), prepared, 'implementation')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.fake.posts), self.posts_before)


if __name__ == '__main__':
    unittest.main()
