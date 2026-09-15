"""Foreground native routing using synthetic preparation/adoption/recovery only.

Command hooks are run as copied Python files, never through Claude or a model.
Native startup and background scheduling behavior have separate source probes.
"""

import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_routing
import ao_routing_adoption as adoption
import ao_routing_guard as guard
import ao_routing_refresh as refresh
import test_ao_adoption as adoption_fixtures
import test_ao_compaction as compaction_fixtures
import test_ao_routing_quota as quota_fixtures
import test_ao_routing_refresh as refresh_fixtures
import test_ao_routing_v1_fixture as frozen


FLAG = 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'


def agent(name='pr-sonnet', **params):
    return {'tool_name': 'Agent', 'tool_input': {'subagent_type': name, 'prompt': 'Synthetic bounded task.', **params}}


class ForegroundTests(unittest.TestCase):
    def fixture(self, kind):
        case = kind('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def run_guard(self, prepared, settings, event, **overrides):
        env = {**os.environ, **settings['env'], **overrides}
        for key in tuple(env):
            if env[key] is None:
                del env[key]
        return subprocess.run([sys.executable, prepared['routing']['guard_path']], env=env,
                              input=json.dumps(event), text=True, capture_output=True, check=True)

    def test_agent_requires_exact_inherited_flag_and_rejects_explicit_background(self):
        with patch.dict(os.environ):
            for value in (None, '0', '', 'true', '01', '1 ', ' 1'):
                if value is None:
                    os.environ.pop(FLAG, None)
                else:
                    os.environ[FLAG] = value
                for name in ('pr-sonnet', 'pr-opus'):
                    with self.subTest(value=value, name=name):
                        self.assertIn('inherited ' + FLAG + '=1', guard.decide(agent(name)))
                        event = agent(name)
                        event['env'] = {FLAG: '1'}
                        self.assertIn('inherited ' + FLAG + '=1', guard.decide(event))
            os.environ[FLAG] = '1'
            for name in ('pr-sonnet', 'pr-opus'):
                self.assertIsNone(guard.decide(agent(name)))
                self.assertIsNone(guard.decide(agent(name, run_in_background=False)))
                self.assertIn('run_in_background=true', guard.decide(agent(name, run_in_background=True)))
                for value in ('false', 'true', 0, 1, None, [], {}):
                    self.assertIn('exactly false', guard.decide(agent(name, run_in_background=value)))
            self.assertIn('isolation', guard.decide(agent(isolation='remote')))
            self.assertIn('forceAsync', guard.decide(agent(forceAsync=True)))

    def test_provider_submissions_results_and_worker_execution_do_not_require_the_flag(self):
        with patch.dict(os.environ):
            os.environ.pop(FLAG, None)
            for tool in ('mcp__deepseek__deepseek_submit', 'mcp__deepseek__deepseek_ask',
                         'mcp__deepseek__deepseek_result', 'mcp__deepseek__deepseek_status', 'Read', 'TaskOutput'):
                self.assertIsNone(guard.decide({'tool_name': tool, 'tool_input': {}}))
            self.assertIsNone(guard.decide({'tool_name': 'Bash', 'tool_input': {},
                                           'agent_type': 'pr-sonnet', 'agent_id': 'synthetic-child'}))
            self.assertIn('Fable orchestrates', guard.decide({'tool_name': 'Bash', 'tool_input': {}}))

    def test_typed_quota_and_unverified_native_evidence_keep_priority(self):
        case = self.fixture(quota_fixtures.NativeQuotaGuardTests)
        case.write([case.caller(), case.error()])
        with patch.dict(os.environ):
            os.environ.pop(FLAG, None)
            event = case.event()
            event['tool_input']['run_in_background'] = True
            self.assertIn('quota was rejected', guard.decide(event))
            self.assertIn('quota was rejected', guard.decide(case.event('mcp__deepseek__deepseek_submit')))
            self.assertIsNone(guard.decide(case.event('mcp__deepseek__deepseek_result')))
            case.transcript.unlink()
            with self.assertRaises(FileNotFoundError):
                guard.decide(event)

    def test_renderer_opt_in_preserves_historical_env_and_input_bytes(self):
        original = {'env': {'UNRELATED': 'kept'}, 'autoCompactEnabled': False}
        before = copy.deepcopy(original)
        legacy = ao_routing.settings_document(original, 'synthetic-hook')
        current = ao_routing.settings_document(original, 'synthetic-hook', foreground=True)
        self.assertEqual(original, before)
        self.assertNotIn(FLAG, legacy['env'])
        self.assertEqual(current, {**legacy, 'env': {**legacy['env'], FLAG: '1'}})
        self.assertEqual(ao_routing.ENV, compaction_fixtures.LEGACY_ENV)
        self.assertNotIn(FLAG, ao_routing.RECORDED_ENV)
        for value in ('0', '', True, False, 1, None):
            existing = {'env': {FLAG: value}}
            self.assertEqual(ao_routing.settings_document(existing, 'synthetic-hook')['env'][FLAG], value)
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                ao_routing.settings_document(existing, 'synthetic-hook', foreground=True)

    def test_fresh_prepare_pins_flag_and_copied_guard_inherits_settings_without_rewriting_preparation(self):
        case = self.fixture(compaction_fixtures.CompactionFixture)
        prepared = case.prepare()
        settings = case.settings()
        self.assertEqual(settings['env'][FLAG], '1')
        self.assertNotIn(FLAG, prepared['routing']['env'])
        self.assertNotIn(FLAG, prepared['routing']['prepare_environment'])
        self.assertNotIn(FLAG, prepared['routing']['rules']['recorded_env'])
        original = case.immutable_bytes(prepared)
        self.assertEqual(case.prepare(), prepared)
        self.assertEqual(ao_routing.validate_local(prepared), prepared['routing'])
        for name in ao_routing.MODELS:
            self.assertEqual(self.run_guard(prepared, settings, agent(name)).stdout, '')
            self.assertEqual(self.run_guard(prepared, settings, agent(name, run_in_background=False)).stdout, '')
            result = self.run_guard(prepared, settings, agent(name, run_in_background=True))
            self.assertIn('foreground', json.loads(result.stdout)['hookSpecificOutput']['permissionDecisionReason'])
            for value in (None, '0'):
                result = self.run_guard(prepared, settings, agent(name), **{FLAG: value})
                self.assertIn('inherited', json.loads(result.stdout)['hookSpecificOutput']['permissionDecisionReason'])
        self.assertEqual(case.immutable_bytes(prepared), original)
        self.assertEqual(prepared['routing']['effort'], 'max')
        self.assertEqual(prepared['routing']['agents'], ao_routing.MODELS)
        self.assertEqual(case.fake.posts, [])

    def test_every_external_flag_zero_refuses_fresh_prepare_before_runtime_publication(self):
        case = self.fixture(compaction_fixtures.CompactionFixture)
        original = case.state()
        for source in compaction_fixtures.SOURCES:
            with self.subTest(source=source), case.settings_override(source, {'env': {FLAG: '0'}}):
                with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                    case.prepare()
                self.assertEqual(case.state(), original)
                self.assertFalse((case.repo / '.claude/agents').exists())
                self.assertFalse((case.service.root / 'launchers').exists())
        self.assertEqual(case.fake.posts, [])

    def test_external_flag_one_is_compatible_and_later_ao_conflict_blocks_dispatch(self):
        case = self.fixture(compaction_fixtures.CompactionFixture)
        with contextlib.ExitStack() as stack:
            for source in compaction_fixtures.SOURCES:
                stack.enter_context(case.settings_override(source, {'env': {FLAG: '1'}}))
            prepared = case.prepare()
            self.assertEqual(case.settings()['env'][FLAG], '1')
            self.assertEqual(ao_routing.before_dispatch(case.service, case.directory(), case.state(), prepared,
                                                      'spec_review'), prepared['routing'])
            case.fake.config['env'] = {FLAG: '0'}
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                ao_routing.before_dispatch(case.service, case.directory(), case.state(), prepared, 'implementation')
        self.assertEqual(case.fake.posts, [])

    def test_old_v1_and_unflagged_v2_remain_readable_with_external_flag_zero(self):
        for kind in (frozen.FrozenV1RoutingTests, refresh_fixtures.RoutingRefreshTests):
            with self.subTest(kind=kind.__name__):
                case = self.fixture(kind)
                prepared = case.prepared
                before = {name: (case.repo / name).read_bytes() for name in ao_routing.FILES}
                before_state = (case.directory() / 'state.json').read_bytes()
                with patch.dict(os.environ, {FLAG: '0'}):
                    self.assertEqual(ao_routing.validate_local(prepared, case.state(), case.directory()), prepared['routing'])
                    self.assertEqual(case.service.ao_room_prepare(case.room, str(case.repo)), prepared)
                self.assertEqual((case.directory() / 'state.json').read_bytes(), before_state)
                self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES}, before)
                self.assertNotIn(FLAG, json.loads(before['.claude/settings.local.json'])['env'])

    def test_retained_refresh_and_exact_retry_pin_foreground_without_rewriting_source_or_receipts(self):
        case = self.fixture(refresh_fixtures.RoutingRefreshTests)
        self.assertNotIn(FLAG, json.loads(case.original_files['.claude/settings.local.json'])['env'])
        with patch.object(refresh, '_place_guard', side_effect=OSError('synthetic interruption after intent')):
            with self.assertRaisesRegex(OSError, 'synthetic interruption'):
                case.do_refresh()
        path = case.directory() / refresh.BASE / 'refresh-one.json'
        pending = path.read_bytes()
        record = json.loads(pending)
        self.assertEqual(json.loads(record['target_bundle']['files']['.claude/settings.local.json'])['env'][FLAG], '1')
        self.assertNotIn(FLAG, record['source']['env'])
        self.assertEqual(record['source']['env'], record['target']['env'])
        self.assertEqual(record['source_bundle']['files'], {name: raw.decode() for name, raw in case.original_files.items()})
        with patch.dict(os.environ, {FLAG: '0'}):
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                case.do_refresh()
        self.assertEqual(path.read_bytes(), pending)
        self.assertEqual(case.state(), case.original)
        self.assertEqual({name: (case.repo / name).read_bytes() for name in ao_routing.FILES}, case.original_files)
        case.do_refresh()
        final = (case.directory() / 'state.json').read_bytes()
        self.assertTrue(case.do_refresh()['idempotent'])
        self.assertEqual(path.read_bytes(), pending)
        self.assertEqual((case.directory() / 'state.json').read_bytes(), final)
        self.assertEqual((case.directory() / case.state()['preparation']).read_bytes(), case.prepared_bytes)
        self.assertEqual({key: value for key, value in case.state().items() if key != 'routing_refresh'}, case.original)
        current = refresh.effective(case.directory(), case.state(), case.prepared)
        settings = ao.read(case.repo / '.claude/settings.local.json')
        self.assertEqual(settings['env'][FLAG], '1')
        self.assertEqual(self.run_guard({**case.prepared, 'routing': current}, settings, agent()).stdout, '')
        self.assertEqual(case.fake.posts, case.posts)
        self.assertEqual(ao.candidate_snapshot(case.repo), case.original_candidate)

    def test_refresh_serializes_new_flag_even_with_unchanged_guard_command(self):
        case = self.fixture(refresh_fixtures.RoutingRefreshTests)
        case.do_refresh()
        source = refresh.effective(case.directory(), case.state(), case.prepared)
        raw = (case.repo / '.claude/settings.local.json').read_bytes()
        settings = json.loads(raw)
        del settings['env'][FLAG]
        no_flag = (json.dumps(settings, indent=2, sort_keys=True) + '\n').encode()
        source = {**source, 'files': {**source['files'], '.claude/settings.local.json': ao.digest(no_flag)}}
        read = refresh.owned_bytes
        with patch.object(refresh, 'owned_bytes', side_effect=lambda path: no_flag if path == case.repo / '.claude/settings.local.json' else read(path)):
            target, old, new = refresh._build(case.service, case.prepared, source)
        self.assertEqual(source['hook_command'], target['hook_command'])
        self.assertNotEqual(source['files'], target['files'])
        self.assertNotIn(FLAG, json.loads(old['files']['.claude/settings.local.json'])['env'])
        self.assertEqual(json.loads(new['files']['.claude/settings.local.json'])['env'][FLAG], '1')
        self.assertEqual((case.repo / '.claude/settings.local.json').read_bytes(), raw)

    def test_external_process_and_ao_zero_refuse_new_refresh_without_intent(self):
        case = self.fixture(refresh_fixtures.RoutingRefreshTests)
        with patch.dict(os.environ, {FLAG: '0'}):
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                case.do_refresh()
        case.assert_no_intent()
        case.fake.config['env'] = {FLAG: '0'}
        with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
            case.do_refresh()
        case.assert_no_intent()

    def test_unflagged_pending_refresh_reconciles_exact_old_target_after_update(self):
        case = self.fixture(refresh_fixtures.RoutingRefreshTests)
        with patch.object(ao_routing, 'foreground_settings', side_effect=lambda settings: settings):
            case.pending()
        path = case.directory() / refresh.BASE / 'refresh-one.json'
        before = path.read_bytes()
        record = json.loads(before)
        self.assertNotIn(FLAG, json.loads(record['target_bundle']['files']['.claude/settings.local.json'])['env'])
        with patch.dict(os.environ, {FLAG: '0'}):
            case.do_refresh()
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(FLAG, ao.read(case.repo / '.claude/settings.local.json')['env'])
        self.assertEqual(refresh.effective(case.directory(), case.state(), case.prepared), record['target'])
        self.assertEqual(case.fake.posts, case.posts)

    def old_audit(self, case):
        # Exact pre-foreground audit schema. The evidence comes from the real
        # read-only audit of a supported synthetic provider amendment and v1.
        case.provider_commit()
        value = {'evidence': adoption._inspect(case.service, case.directory(), case.state()),
                 'attachment_id': '00000000000040008000000000000001', 'recorded_at': time.time()}
        sha = ao.digest(value)
        adoption._store_once(case.directory() / adoption.BASE / 'audits' / (sha + '.json'), value)
        return value, (case.service, case.room, sha, 'Synthetic authorized routing adoption.',
                       'Synthetic legacy routing migration.', 'legacy-audit')

    def test_pre_update_pending_adoption_reconstructs_exact_unmarked_bytes(self):
        case = self.fixture(adoption_fixtures.AdoptionFixture)
        source_guard = Path(adoption.__file__).with_name('ao_routing_guard.py')
        read = adoption.owned_bytes

        def historical(path, *args):
            return refresh_fixtures.OLD_GUARD if Path(path) == source_guard else read(path, *args)

        with patch.object(adoption, 'owned_bytes', side_effect=historical):
            saved, args = self.old_audit(case)
            result = adoption.stage(*args)
        before_state = (case.directory() / 'state.json').read_bytes()
        prepared_path = case.directory() / adoption.REL / 'preparation.json'
        before_prepared = prepared_path.read_bytes()
        request_path = case.directory() / case.state()['routing_adoption']['receipt']
        before_request = request_path.read_bytes()
        settings = ao.read(case.repo / '.claude/settings.local.json')
        self.assertNotIn('foreground_env', saved)
        self.assertNotIn(FLAG, settings['env'])
        # Preserve the pre-existing installed-source identity requirement: a
        # release never silently replaces a pending audit's old guard bytes.
        with self.assertRaisesRegex(ao.RoomError, 'Installed guard or attachment wrapper changed'):
            adoption.stage(*args)
        self.assertEqual((case.directory() / 'state.json').read_bytes(), before_state)
        self.assertEqual(prepared_path.read_bytes(), before_prepared)
        self.assertEqual(request_path.read_bytes(), before_request)
        with patch.object(adoption, 'owned_bytes', side_effect=historical), \
                patch.dict(os.environ, {FLAG: '0'}), patch.object(ao_routing, 'foreground_settings',
                side_effect=AssertionError('Historical bundle must not acquire current foreground defaults')):
            self.assertEqual(adoption.stage(*args), result)
            self.assertEqual((case.directory() / 'state.json').read_bytes(), before_state)
            case.fake.config = ao.read(Path(result['project_config_payload']))['config']
            adoption.activate(case.service, case.room, 'legacy-audit')
        self.assertEqual(prepared_path.read_bytes(), before_prepared)
        self.assertEqual(request_path.read_bytes(), before_request)
        self.assertNotIn(FLAG, ao.read(case.repo / '.claude/settings.local.json')['env'])
        self.assertEqual(case.state()['requests'], case.original['requests'])
        self.assertEqual(len(case.fake.posts), case.posts_before)

    def test_new_marked_adoption_retry_and_activation_propagate_flag(self):
        case = self.fixture(adoption_fixtures.AdoptionFixture)
        result = case.stage_routing()
        state = case.state()
        request = ao.read(case.directory() / state['routing_adoption']['receipt'])
        saved = adoption._audit(case.directory(), request['inputs']['audit_sha256'])
        self.assertEqual(saved['foreground_env'], {FLAG: '1'})
        prepared_path = case.directory() / adoption.REL / 'preparation.json'
        prepared = ao.read(prepared_path)
        before = prepared_path.read_bytes()
        self.assertNotIn(FLAG, prepared['routing']['env'])
        self.assertEqual(ao.read(case.repo / '.claude/settings.local.json')['env'][FLAG], '1')
        self.assertNotIn(FLAG, json.loads(saved['evidence']['runtime_files']['.claude/settings.local.json'])['env'])
        with patch.dict(os.environ, {FLAG: '0'}):
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                adoption.stage(*case.stage_args)
        self.assertEqual(case.state(), state)
        self.assertEqual(prepared_path.read_bytes(), before)
        self.assertEqual(adoption.stage(*case.stage_args), result)
        case.fake.config = ao.read(Path(result['project_config_payload']))['config']
        case.fake.config['env'] = {FLAG: '0'}
        with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
            adoption.activate(case.service, case.room, 'routing-change')
        self.assertEqual(case.state(), state)
        case.fake.config = ao.read(Path(result['project_config_payload']))['config']
        adoption.activate(case.service, case.room, 'routing-change')
        self.assertEqual(prepared_path.read_bytes(), before)
        self.assertEqual(case.state()['requests'], case.original['requests'])
        self.assertEqual(len(case.fake.posts), case.posts_before)
        self.assertEqual(self.run_guard(prepared, ao.read(case.repo / '.claude/settings.local.json'), agent()).stdout, '')

    def test_fresh_adoption_audit_refuses_conflict_and_markers_are_exact(self):
        case = self.fixture(adoption_fixtures.AdoptionFixture)
        case.provider_commit()
        before = copy.deepcopy(case.state())
        with patch.dict(os.environ, {FLAG: '0'}):
            with self.assertRaisesRegex(ao.RoomError, 'foreground native delegation'):
                adoption.audit(case.service, case.room)
        self.assertEqual(case.state(), before)
        self.assertFalse((case.directory() / adoption.BASE / 'audits').exists())
        for value in (None, True, 1, '1', {}, {FLAG: 1}, {FLAG: '0'}, {FLAG: '1', 'extra': '1'}):
            with self.subTest(marker=value):
                with self.assertRaisesRegex(ao.RoomError, 'marker is unsupported'):
                    adoption._foreground({'foreground_env': value})
        self.assertFalse(adoption._foreground({}))
        self.assertTrue(adoption._foreground({'foreground_env': {FLAG: '1'}}))


if __name__ == '__main__':
    unittest.main()
