"""The evidence and usage audits bind a routing version 3 preparation to its qualification.

A real ao_room_prepare already writes version 3 routing records; these tests pin that the audit
accepts exactly that shape through its execution-policy and source-qualified worker binding and
still refuses a tampered record with preparation_unbound. Versions 1 and 2 keep their existing
coverage in test_ao_evidence_audit and test_ao_usage_audit.
"""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import uuid

import ao_evidence_audit
import ao_project_room as ao
import ao_usage_audit

from test_ao_evidence_audit import canon, completed_result
from test_ao_normal import Fixture


class RoutingV3AuditFixture(Fixture):
    """A completed engineer request in a real room whose preparation carries routing version 3."""

    def setUp(self):
        super().setUp()
        self.room = self.open(provider='none')
        self.spec()
        self.bind()
        self.agree()
        self.implement()
        self.request_id = 'implementation'
        self.write_database()

    def write_database(self, provider_uuid=None):
        """Materialize the registered native owner row as a real read-only AO database."""
        provider_uuid = provider_uuid or str(uuid.uuid4())
        request = self.state()['requests'][self.request_id]
        owner = dict(self.owner)
        owner['provider_conversation_id'] = provider_uuid
        owner['branch_provider_conversation_id'] = provider_uuid
        owner['ao_conversation_id'] = request['baseline']['conversation_id']
        owner['active_branch_id'] = request['baseline']['branch_id']
        path = self.native_outcome_source['database']
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);""")
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (owner['id'], owner['project_id'], owner['harness'], owner['session_mode'],
                        owner['is_terminated'], owner['activity_state'], owner['workspace_path'],
                        owner['provider_conversation_id'], owner['controller_generation']))
            db.execute("INSERT INTO conversations VALUES (?,?,?)",
                       (owner['ao_conversation_id'], owner['id'], owner['active_branch_id']))
            db.execute("INSERT INTO conversation_branches VALUES (?,?,?,?,?,?)",
                       (owner['active_branch_id'], owner['ao_conversation_id'],
                        owner['branch_provider_conversation_id'], owner['branch_session_id'],
                        owner['strategy'], owner['replay_truncated']))
        self.database = path
        self.write_transcript(provider_uuid, request)
        return path

    def write_transcript(self, provider_uuid, request):
        """The request's synthetic caller and response records under the scanned native id."""
        config_root = Path(self.prepared_routing()['claude_config_dir'])
        project_dir = config_root / 'projects' / str(self.repo).replace('/', '-')
        project_dir.mkdir(parents=True, exist_ok=True)
        stamp = lambda seconds: min(datetime.fromtimestamp(request['created_at'] + seconds,
                                                          timezone.utc),
                                    datetime.now(timezone.utc)).isoformat()
        records = [
            {'type': 'user', 'uuid': self.request_id + '-caller', 'sessionId': provider_uuid,
             'timestamp': stamp(0.001), 'cwd': str(self.repo), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': self.request_id + '-reply', 'sessionId': provider_uuid,
             'timestamp': stamp(0.002), 'cwd': str(self.repo),
             'message': {'role': 'assistant', 'model': request['model'],
                         'id': self.request_id + '-m0',
                         'content': [{'type': 'text', 'text': 'Done'}],
                         'stop_reason': 'end_turn',
                         'usage': {'input_tokens': 1, 'output_tokens': 1,
                                   'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                                   'server_tool_use': None, 'service_tier': None,
                                   'cache_creation': None}}}]
        (project_dir / (provider_uuid + '.jsonl')).write_text(
            ''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')

    def usage(self):
        return ao_usage_audit.audit(str(self.home), self.room, self.request_id, str(self.database))

    def rewrite_preparation(self, mutator, update_state=True):
        """Rewrite the recorded preparation; state records the new digest unless update_state pins the old."""
        prepared = copy.deepcopy(ao.read(self.directory() / 'preparation.json'))
        mutator(prepared)
        (self.directory() / 'preparation.json').write_text(
            json.dumps(prepared, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        if update_state:
            state = self.state()
            state['preparation_sha256'] = canon(prepared)
            ao.atomic(self.directory() / 'state.json', state)
        return prepared


class RealVersionThreeTests(RoutingV3AuditFixture, unittest.TestCase):
    def test_real_version3_preparation_binds_and_the_usage_audit_runs(self):
        self.assertEqual(self.prepared_routing()['version'], 3)
        binding = ao_evidence_audit._bind(str(self.home), self.room, self.request_id,
                                          str(self.database), None)
        try:
            self.assertEqual(binding['prepared']['routing']['version'], 3)
            self.assertEqual(binding['prepared']['routing']['execution_policy'],
                             self.prepared_routing()['execution_policy'])
            self.assertEqual(binding['prepared']['routing']['worker_qualification'],
                             self.prepared_routing()['worker_qualification'])
        finally:
            binding['room_root'].close()
        report = self.usage()
        self.assertEqual(report['coverage'], 'complete')
        self.assertEqual(report['reasons'], [])
        self.assertEqual(report['parent']['coverage'], 'complete')
        self.assertEqual(report['parent']['reasons'], [])
        self.assertEqual(report['parent']['counters'],
                         {'input_tokens': 1, 'output_tokens': 1,
                          'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0})


class TamperedVersionThreeTests(RoutingV3AuditFixture, unittest.TestCase):
    def assert_unbound(self):
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('preparation_unbound', report['reasons'])

    def test_changed_execution_policy_refuses(self):
        self.rewrite_preparation(lambda p: p['routing'].update(execution_policy='unrestricted'))
        self.assert_unbound()

    def test_removed_worker_qualification_refuses(self):
        self.rewrite_preparation(lambda p: p['routing'].pop('worker_qualification'))
        self.assert_unbound()

    def test_altered_agents_map_refuses(self):
        self.rewrite_preparation(lambda p: p['routing']['agents'].popitem())
        self.assert_unbound()

    def test_preparation_digest_mismatch_refuses(self):
        self.rewrite_preparation(lambda p: p.update(room_id='room-other'), update_state=False)
        self.assert_unbound()


class RetainedQualificationEvidenceTests(RoutingV3AuditFixture, unittest.TestCase):
    """The audit reads the retained qualification record and every declared source capture itself:
    missing, changed or symlinked retained bytes are preparation_unbound, not complete coverage."""

    def snapshot(self):
        return self.prepared_routing()['worker_qualification']['snapshot']

    def captures(self):
        from ao_model_qualification import evidence_path
        snapshot = self.snapshot()
        return [self.directory() / evidence_path(snapshot['sha256'], descriptor)
                for descriptor in snapshot['artifact']['sources']]

    def assert_unbound(self):
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('preparation_unbound', report['reasons'])

    def test_intact_retained_evidence_is_the_positive_control(self):
        captures = self.captures()
        self.assertTrue(captures and all(path.is_file() for path in captures))
        self.assertTrue((self.directory() / self.snapshot()['record']).is_file())
        self.assertEqual(self.usage()['coverage'], 'complete')

    def test_deleted_source_capture_refuses(self):
        self.captures()[0].unlink()
        self.assert_unbound()

    def test_changed_source_capture_refuses(self):
        path = self.captures()[0]
        path.write_bytes(path.read_bytes() + b'\n')
        self.assert_unbound()

    def test_symlinked_source_capture_refuses(self):
        path = self.captures()[0]
        copy_path = path.with_name(path.name + '.copy')
        copy_path.write_bytes(path.read_bytes())  # identical bytes behind a symlink are still refused
        path.unlink()
        path.symlink_to(copy_path)
        self.assert_unbound()

    def test_changed_retained_record_refuses(self):
        path = self.directory() / self.snapshot()['record']
        record = ao.read(path)
        record['note'] = 'edited after preparation'
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assert_unbound()

    def test_missing_retained_record_refuses(self):
        (self.directory() / self.snapshot()['record']).unlink()
        self.assert_unbound()

    def test_worker_qualification_check_refuses_on_its_own(self):
        """Discriminating case: the engineering-model binding check happens to read the same retained
        artifact in this room, so it is neutralized here; the version-3 worker qualification check
        itself must still translate a deleted capture to preparation_unbound (complete before the fix)."""
        import ao_engineering_model
        self.captures()[0].unlink()
        with patch.object(ao_engineering_model, 'check_request', lambda *args, **kwargs: None), \
                patch.object(ao_engineering_model, 'binding_for_request',
                             lambda root, state, binding, request: binding):
            self.assert_unbound()


if __name__ == '__main__':
    unittest.main()


class FrozenWorkerSelectionTests(RoutingV3AuditFixture, unittest.TestCase):
    """After a qualified refresh, a request's children carry the map that request actually froze."""

    NEW_SONNET = 'claude-sonnet-6-7'

    def qualified_refresh(self):
        """Commit a qualified routing refresh that moves pr-sonnet to NEW_SONNET."""
        artifact = json.loads((self.root / 'qualification.json').read_text())
        artifact['families']['sonnet']['expected_model'] = self.NEW_SONNET
        self.configure_qualification(artifact)
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        self.fake.snapshots['engineer'].update(
            controller='stopped', hasMoreBefore=False, activities=[],
            branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        original_request = self.fake.request

        def request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return copy.deepcopy(self.fake.snapshots[path.split('/')[2]])
            return original_request(method, path, payload)

        self.fake.request = request
        import ao_routing_refresh
        with sqlite3.connect(self.database) as db:
            native_session = db.execute('SELECT provider_conversation_id FROM sessions').fetchone()[0]
        return ao_routing_refresh.refresh(
            self.service, room_id=self.room, request_id='refresh-one',
            database_path=str(self.database), native_session_id=native_session,
            authorization='User authorizes the routing correction for the stopped engineer.',
            diagnosis='Retained routing predates the bounded-worker instructions.',
            agent_selection='qualified')

    def rewrite_database(self, provider_uuid):
        """Replace the AO database with one session row for the currently selected request."""
        Path(self.database).unlink(missing_ok=True)
        return self.write_database(provider_uuid=provider_uuid)

    def send_correction(self):
        self.send('correction', 'correction-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['correction-1']['state'], 'completed')

    def write_child_launch(self, provider_uuid):
        """Append an Agent launch + result to the request's parent transcript and write its child."""
        request = self.state()['requests'][self.request_id]
        config_root = Path(self.prepared_routing()['claude_config_dir'])
        project_dir = config_root / 'projects' / str(self.repo).replace('/', '-')
        stamp = lambda seconds: min(datetime.fromtimestamp(request['created_at'] + seconds,
                                                          timezone.utc),
                                    datetime.now(timezone.utc)).isoformat()
        prompt = 'Implement the isolated child task.'
        additions = [
            {'type': 'assistant', 'uuid': 'launch-record', 'sessionId': provider_uuid,
             'timestamp': stamp(0.0015), 'cwd': str(self.repo), 'isSidechain': False,
             'message': {'role': 'assistant', 'model': request['model'], 'id': 'launch-message',
                         'content': [{'type': 'tool_use', 'id': 'agent-tool', 'name': 'Agent',
                                      'input': {'prompt': prompt, 'subagent_type': 'pr-sonnet'}}],
                         'stop_reason': 'tool_use',
                         'usage': {'input_tokens': 1, 'output_tokens': 1,
                                   'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                                   'server_tool_use': None, 'service_tier': None,
                                   'cache_creation': None}}},
            {'type': 'user', 'uuid': 'result-record', 'sessionId': provider_uuid,
             'timestamp': stamp(0.0018), 'cwd': str(self.repo), 'isSidechain': False,
             'toolUseResult': completed_result('a1', prompt),
             'sourceToolAssistantUUID': 'launch-record',
             'message': {'role': 'user',
                         'content': [{'type': 'tool_result', 'tool_use_id': 'agent-tool',
                                      'content': 'done'}]}}]
        parent = project_dir / (provider_uuid + '.jsonl')
        existing = [json.loads(line) for line in parent.read_text().splitlines() if line.strip()]
        records = [existing[0]] + additions + existing[1:]
        parent.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        subagents = project_dir / provider_uuid / 'subagents'
        subagents.mkdir(parents=True, exist_ok=True)
        child = [
            {'type': 'user', 'sessionId': provider_uuid, 'cwd': str(self.repo),
             'uuid': 'child-caller', 'timestamp': stamp(0.0016), 'isSidechain': True,
             'agentId': 'a1', 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'sessionId': provider_uuid, 'cwd': str(self.repo),
             'uuid': 'child-reply', 'timestamp': stamp(0.0017), 'isSidechain': True,
             'agentId': 'a1',
             'message': {'role': 'assistant', 'model': self.NEW_SONNET, 'id': 'child-m0',
                         'content': [{'type': 'text', 'text': 'Done'}], 'stop_reason': 'end_turn',
                         'usage': {'input_tokens': 2, 'output_tokens': 2,
                                   'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                                   'server_tool_use': None, 'service_tier': None,
                                   'cache_creation': None}}}]
        (subagents / 'agent-a1.jsonl').write_text(
            ''.join(json.dumps(record) + '\n' for record in child), encoding='utf-8')

    def request_uuid(self, request_id):
        request = self.state()['requests'][request_id]
        with sqlite3.connect(self.database) as db:
            row = db.execute(
                'SELECT s.provider_conversation_id FROM sessions s '
                'JOIN conversation_branches b ON s.id = b.session_id '
                'WHERE b.id = ? AND b.conversation_id = ?',
                (request['baseline']['branch_id'], request['baseline']['conversation_id'])
            ).fetchone()
        return row[0]

    def mutate_expectation(self, request_id, mutator):
        state = self.state()
        frozen = copy.deepcopy(state['requests'][request_id]['native_worker_expectations'])
        mutator(frozen)
        state['requests'][request_id]['native_worker_expectations'] = frozen
        ao.atomic(self.directory() / 'state.json', state)

    def refreshed_sonnet(self):
        record = ao.read(self.directory() / self.state()['routing_refresh']['path'])
        return record['target']['agents']['pr-sonnet']

    def test_refreshed_request_labels_children_from_its_frozen_map(self):
        # (a) after a qualified refresh the new request freezes the new map and its
        # children are labelled with it, not the initial preparation's.
        initial = self.prepared_routing()['agents']['pr-sonnet']
        self.qualified_refresh()
        self.assertIn('routing_refresh', self.state())
        self.assertEqual(self.refreshed_sonnet(), self.NEW_SONNET)
        self.assertNotEqual(self.NEW_SONNET, initial)
        self.send_correction()
        frozen = self.state()['requests']['correction-1']['native_worker_expectations']
        self.assertEqual(frozen['authority']['kind'], 'routing_refresh')
        self.assertEqual(frozen['agents']['pr-sonnet'], self.NEW_SONNET)
        self.request_id = 'correction-1'
        self.rewrite_database('00000000-0000-4000-8000-000000000099')
        self.write_child_launch('00000000-0000-4000-8000-000000000099')
        report = self.usage()
        self.assertEqual(report['coverage'], 'complete')
        self.assertEqual(report['children'][0]['configured_agent'], 'pr-sonnet')
        self.assertEqual(report['children'][0]['usage']['configured_model'], self.NEW_SONNET)

    def test_pre_refresh_request_keeps_the_initial_map(self):
        # (b) a request frozen before the refresh keeps its own map; a request with no
        # frozen selection falls back to the initial preparation's map.
        initial = self.prepared_routing()['agents']['pr-sonnet']
        self.qualified_refresh()
        self.send_correction()
        self.assertEqual(
            self.state()['requests']['implementation']['native_worker_expectations']['authority'],
            {'kind': 'preparation'})
        self.request_id = 'implementation'
        self.write_child_launch(self.request_uuid('implementation'))
        report = self.usage()
        self.assertEqual(report['coverage'], 'complete')
        self.assertEqual(report['children'][0]['usage']['configured_model'], initial)
        # A request record stripped of its frozen selection is receipt_integrity, not a
        # silent fallback: the initial-map fallback exists for other routing shapes.
        state = self.state()
        state['requests']['implementation'].pop('native_worker_expectations')
        ao.atomic(self.directory() / 'state.json', state)
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('receipt_integrity', report['reasons'])

    def _bound_new_request(self):
        self.qualified_refresh()
        self.send_correction()
        self.request_id = 'correction-1'
        self.rewrite_database('00000000-0000-4000-8000-000000000099')
        self.write_child_launch('00000000-0000-4000-8000-000000000099')

    def test_tampered_frozen_agents_refuse(self):
        # (c) a tampered request record is caught by request integrity; the worker-map
        # disagreement itself is exercised in the authority-side test below.
        self._bound_new_request()
        self.mutate_expectation(
            'correction-1', lambda frozen: frozen['agents'].update(pr_sonnet='claude-sonnet-9-9'))
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('request_integrity', report['reasons'])

    def test_authority_side_map_change_refuses(self):
        # (c') an intact request whose named authority's own map changed is preparation_unbound.
        self._bound_new_request()
        path = self.directory() / self.state()['routing_refresh']['path']
        record = ao.read(path)
        record['target']['agents']['pr-sonnet'] = 'claude-sonnet-9-9'
        path.write_text(json.dumps(record), encoding='utf-8')
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('preparation_unbound', report['reasons'])

    def test_missing_authority_refuses(self):
        # (d) a tampered authority digest is caught by receipt integrity; the unreachable
        # authority inside the worker-map branch is exercised below.
        self._bound_new_request()
        self.mutate_expectation(
            'correction-1', lambda frozen: frozen['authority'].update(sha256='f' * 64))
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('receipt_integrity', report['reasons'])

    def test_deleted_refresh_record_refuses(self):
        # (d') the named authority's committed record missing is preparation_unbound.
        self._bound_new_request()
        (self.directory() / self.state()['routing_refresh']['path']).unlink()
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('preparation_unbound', report['reasons'])

    def test_deleted_refreshed_qualification_capture_refuses(self):
        # (e) the refreshed routing's own retained qualification bytes are verified too.
        self.qualified_refresh()
        self.send_correction()
        self.request_id = 'correction-1'
        self.rewrite_database('00000000-0000-4000-8000-000000000099')
        self.write_child_launch('00000000-0000-4000-8000-000000000099')
        from ao_model_qualification import evidence_path
        record = ao.read(self.directory() / self.state()['routing_refresh']['path'])
        snapshot = record['target']['worker_qualification']['snapshot']
        path = self.directory() / evidence_path(snapshot['sha256'], snapshot['artifact']['sources'][0])
        self.assertTrue(path.is_file())
        path.unlink()
        report = self.usage()
        self.assertEqual(report['coverage'], 'unavailable')
        self.assertIn('preparation_unbound', report['reasons'])
