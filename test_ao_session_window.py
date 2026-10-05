"""One-time read-admission part delivery and the post-quota session-window notice."""

from datetime import datetime, timezone
import json
import unittest

import ao_project_room as ao
import ao_read_admission
import ao_workflow
from test_ao_normal import Fixture


class SessionWindowTests(Fixture):
    """A room whose only terminal engineer result is a settled implementation send."""

    def setUp(self):
        super().setUp()
        self.room = self.open(); self.spec(); self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'impl-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)

    def packet(self, purpose='correction', now=None):
        return ao_workflow.packet(self.service, self.directory(), self.state(), 'engineer',
                                  purpose, 'Continue.', now=now)

    def quota_previous(self, error_message):
        state = self.state()
        request = state['requests']['impl-1']
        request['semantic_status'] = {'kind': 'quota_limit', 'hold': True,
                                      'reason': 'Correlated native provider failure'}
        request['observed_turn'] = {**(request.get('observed_turn') or {}),
                                    'errorMessage': error_message}
        ao.atomic(self.directory() / 'state.json', state)

    def test_read_admission_part_delivered_once_with_pinned_digest(self):
        carried = self.state()['requests']['spec_review']['carried']
        self.assertIn(ao_read_admission.PART, carried['parts'])
        self.assertEqual(carried['part_sha256'][ao_read_admission.PART],
                         ao_read_admission.INSTRUCTION_SHA256)
        self.assertEqual(ao.digest(ao_read_admission.INSTRUCTION.encode()),
                         ao_read_admission.INSTRUCTION_SHA256)
        self.assertNotIn(ao_read_admission.PART, self.state()['requests']['impl-1']['carried']['parts'])

    def test_a_completed_previous_request_carries_no_notice(self):
        text, carried = self.packet()
        self.assertNotIn('Session window notice', text)
        self.assertNotIn('session_window_notice', carried)

    def test_quota_failure_carries_the_session_window_notice_in_the_named_zone(self):
        self.quota_previous('Session usage limit reached; resets 3am (America/Los_Angeles)')
        moment = datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc)
        notice = ("Session window notice (controller-derived, no new instruction): the previous turn "
                  "impl-1 ended at the account's session limit "
                  "(Session usage limit reached; resets 3am (America/Los_Angeles)). This turn starts a "
                  "new window at 2026-10-03 23:30 PDT; if the provider's five-hour window applies as "
                  "before, expect the limit again near 04:30 PDT. Size each work unit so its worker "
                  "hands back before then, keep the task list current at every unit boundary, and "
                  "avoid leaving a unit mid-flight at the cut-off.")
        text, carried = self.packet(now=moment)
        self.assertIn(notice, text)
        self.assertEqual(carried['session_window_notice'], {
            'previous_request_id': 'impl-1',
            'reset_text': 'Session usage limit reached; resets 3am (America/Los_Angeles)',
            'window_started_at': '2026-10-04T06:30:00+00:00',
            'expected_cutoff_at': '2026-10-04T11:30:00+00:00',
            'fragment_sha256': ao.digest(notice.encode())})

    def test_the_notice_is_engineer_only(self):
        self.quota_previous('Session usage limit reached; resets 3am (America/Los_Angeles)')
        self.assertIsNone(ao_workflow.session_window_notice(self.state(), 'reviewer'))
        text, carried = self.packet()
        self.assertIn('Session window notice', text)

    def test_an_unnamed_zone_and_a_missing_message_fall_back_to_utc(self):
        self.quota_previous('Session usage limit reached; try again tomorrow (not-a-zone)')
        moment = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        text, _ = self.packet(now=moment)
        self.assertIn('new window at 18:00 UTC', text)
        self.assertIn('near 23:00 UTC', text)
        state = self.state()
        request = state['requests']['impl-1']
        request['observed_turn'] = {key: value for key, value in request['observed_turn'].items()
                                    if key != 'errorMessage'}
        ao.atomic(self.directory() / 'state.json', state)
        text, carried = self.packet(now=moment)
        self.assertIn("session limit (the account's session limit)", text)
        self.assertEqual(carried['session_window_notice']['reset_text'], "the account's session limit")


if __name__ == '__main__':
    unittest.main()
