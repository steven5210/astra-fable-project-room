"""A new operating amendment never replays old context or grants continuation."""
import copy
import json
from unittest.mock import patch

import ao_project_room as ao
import ao_report_contract as contract
import ao_routing
import ao_workflow
from test_ao_normal import Fixture


class DelegationEfficiencyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind()
        old_parts = tuple(p for p in ao_workflow.PARTS if p != contract.DELEGATION_PART)
        # Freeze real fake-AO receipts from a controller preceding this update.
        with patch.object(ao_workflow, 'PARTS', old_parts):
            self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation')
            self.fake.finish('engineer', json.dumps(self.report(
                outcome='changes_required', implementation_complete=False,
                remaining_gaps=['One bounded supporting unit remains'])))
            self.service.ao_room_sync(self.room)

    def test_retained_session_gets_only_new_amendment_once_across_restart(self):
        before = copy.deepcopy(self.state())
        receipt_paths = [self.directory() / r['receipt'] for r in before['requests'].values()]
        receipts = {p: p.read_bytes() for p in receipt_paths}
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'updated', purpose='correction')
        request = self.state()['requests']['updated']
        self.assertEqual(request['text'], contract.DELEGATION_INSTRUCTION + '\nContinue.')
        self.assertEqual(request['carried']['parts'], [contract.DELEGATION_PART])
        self.assertIsNone(request['carried']['spec_delivery'])
        self.assertEqual(request['carried']['part_sha256'], {
            contract.DELEGATION_PART: ao.digest(contract.DELEGATION_INSTRUCTION.encode())})
        self.assertEqual(request['session_id'], before['bindings']['engineer']['session_id'])
        self.fake.finish('engineer', json.dumps(self.report(
            outcome='changes_required', implementation_complete=False)))
        self.service.ao_room_sync(self.room)
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_send(self.room, 'engineer', 'A new correction only.', 'later', purpose='correction')
        later = self.state()['requests']['later']
        self.assertEqual(later['text'], 'A new correction only.')
        self.assertEqual(later['carried']['parts'], [])
        for key, value in before['requests'].items():
            self.assertEqual(self.state()['requests'][key], value)
        for path, raw in receipts.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_new_amendment_cannot_pass_an_existing_quota_hold(self):
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit', 'httpStatus': 429}
        self.service.ao_room_sync(self.room)
        before = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'semantic hold'):
            self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'blocked', purpose='correction')
        self.assertEqual(len(self.fake.posts), before)
        self.assertNotIn('blocked', self.state()['requests'])

    def test_repeated_request_id_does_not_repeat_the_amendment(self):
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'same', purpose='correction')
        original = copy.deepcopy(self.state()['requests']['same'])
        before = len(self.fake.posts)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'same', purpose='correction')
        self.assertEqual(len(self.fake.posts), before)
        self.assertEqual(self.state()['requests']['same'], original)

    def test_generated_workers_keep_model_effort_and_submission_boundaries(self):
        for name, model in ao_routing.MODELS.items():
            text = ao_routing.agent_definition(name)
            fields = ao_routing.validate_definition(name, text)
            self.assertEqual(fields['model'], model)
            self.assertEqual(fields['effort'], 'max')
            self.assertIn('Agent', fields['disallowedTools'])
            self.assertIn('mcp__deepseek__deepseek_submit', fields['disallowedTools'])
            self.assertIn('read-only assignment never grants', text)
        sonnet = ao_routing.agent_definition('pr-sonnet')
        self.assertIn('routine local implementation choices', sonnet)
        self.assertNotIn('No design decisions', sonnet)
