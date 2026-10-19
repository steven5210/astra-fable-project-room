"""Focused engineering_capture status projection tests: read-only statuses, the verification-worded
refusal and durable-effect pins through the production controller and MCP boundaries; fake backends
only; no account, network, AO process or model call."""
import json

from test_ao_lifecycle_contract import AgreedRoomFixture


class EngineeringCaptureStatusTests(AgreedRoomFixture):
    """The read-only engineering_capture projection follows the production capture path."""

    def test_capture_status_follows_the_production_path(self):
        self.assertEqual(self.service.ao_room_status(self.room)['engineering_capture'],
                         {'status': 'none'})
        self.send('implementation')
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'pending')
        self.assertEqual(capture['request_state'], 'submitted')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        full = self.service.ao_room_status(self.room, view='full')
        capture = full['engineering_capture']
        self.assertEqual(capture['status'], 'captured')
        self.assertEqual(capture['request_id'], 'implementation')
        self.assertEqual(capture['engineering_record_sha256'], request['engineering_record_sha256'])
        self.assertEqual(capture['result_candidate_sha256'], request['result_candidate_sha256'])
        self.assertEqual(capture['current'], {'handoff': True, 'spec_record': True})
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(compact['engineering_capture'], capture)

    def test_malformed_report_is_capture_failed_and_verify_names_it(self):
        self.send('implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps({'outcome': 'completed'}))
        self.service.ao_room_sync(self.room)
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'capture_failed')
        self.assertEqual(capture['request_id'], 'implementation')
        self.assertRegex(capture['engineering_error'], 'Engineering report is incomplete')
        self.refuses('Verification requires a captured completed engineering result; request '
                     'implementation recorded engineering_error: Engineering report is incomplete',
                     self.service.ao_room_verify, self.room, str(self.repo))
        self.refuses('^Acceptance requires a captured completed engineering result$',
                     self.service.ao_room_accept, self.room, 'acceptance_review')
        self.assertEqual(self.state()['verifications'], [])

    def test_held_outcomes_are_held_not_capture_failed(self):
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'implementation', purpose='implementation')
        self.native_failure_rows('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'held')
        self.assertIsNone(capture['semantic_kind'])
        audit = self.service.ao_room_outcome_audit(self.room)
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'held')
        self.assertEqual(capture['semantic_kind'], 'quota_limit')
        self.service.ao_room_outcome_resume(
            self.room, 'implementation', audit['outcome_sha256'], 'resume-1',
            'Synthetic native rate-limit diagnosis; standing quota continuation authority',
            'User authorized one continuation after the diagnosed quota failure')
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'held')
        self.send('correction', 'resume-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'captured')
        self.assertEqual(capture['request_id'], 'resume-1')
        self.send('correction', 'prose')
        self.fake.finish('engineer', 'All done, no JSON.')
        self.service.ao_room_sync(self.room)
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['status'], 'capture_failed')
        self.assertEqual(capture['request_id'], 'prose')

    def test_currentness_flags_follow_spec_and_handoff_changes(self):
        self.implement()
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['current'], {'handoff': True, 'spec_record': True})
        self.service.ao_room_spec_put(self.room, 2, 'Revised exact contract.', self.gates,
                                      'Astra approves this scope')
        capture = self.service.ao_room_status(self.room)['engineering_capture']
        self.assertEqual(capture['current'], {'handoff': True, 'spec_record': False})

    def test_legacy_room_status_has_no_capture_projection(self):
        legacy = self.service.ao_room_open(
            str(self.repo), 'exception', 'project', 'Build', 'http://127.0.0.1:1234',
            workflow='astra_led',
            exception_authorization='User approved this task-scoped Astra exception',
            delegate_provider='none')['room_id']
        self.assertNotIn('engineering_capture', self.service.ao_room_status(legacy))
