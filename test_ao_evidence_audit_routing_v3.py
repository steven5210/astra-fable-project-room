"""The evidence and usage audits bind a routing version 3 preparation to its qualification.

A real ao_room_prepare already writes version 3 routing records; these tests pin that the audit
accepts exactly that shape through its execution-policy and source-qualified worker binding and
still refuses a tampered record with preparation_unbound. Versions 1 and 2 keep their existing
coverage in test_ao_evidence_audit and test_ao_usage_audit.
"""

import copy
import json
import sqlite3
import unittest
import uuid

import ao_evidence_audit
import ao_project_room as ao
import ao_usage_audit

from test_ao_evidence_audit import canon
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
        return path

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
        self.assertNotIn('preparation_unbound', report['reasons'])


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


if __name__ == '__main__':
    unittest.main()
