"""One-time read-admission part delivery and the post-quota session-window notice."""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import ao_progress
import ao_project_room as ao
import ao_read_admission
import ao_residual_escalation
import ao_workflow
import lifecycle_closure
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

    def quota_previous(self, error_message, outcome_error_message=None):
        state = self.state()
        request = state['requests']['impl-1']
        request['semantic_status'] = {'kind': 'quota_limit', 'hold': True,
                                      'reason': 'Correlated native provider failure'}
        if error_message is None:
            request['observed_turn'] = {key: value for key, value in request['observed_turn'].items()
                                        if key != 'errorMessage'}
        else:
            request['observed_turn'] = {**(request.get('observed_turn') or {}),
                                        'errorMessage': error_message}
        if outcome_error_message is not None:
            # The saved semantic outcome's terminal evidence wins over the request snapshot;
            # every field load() verifies must match the request exactly.
            record = {'version': 1, 'room_id': self.directory().name, 'request_id': 'impl-1',
                      'receipt_sha256': request['receipt_sha256'],
                      'text_sha256': request['text_sha256'],
                      'outcome': request['semantic_status'], 'turn_id': request['turn_id'],
                      'provider_turn_id': request['provider_turn_id'],
                      'ao_terminal': {'errorMessage': outcome_error_message}}
            path = self.directory() / 'outcomes' / 'impl-1'
            path.mkdir(parents=True, exist_ok=True)
            relative = 'outcomes/impl-1/' + ao.digest(record) + '.json'
            ao.atomic(path / (ao.digest(record) + '.json'), record)
            request['semantic_outcome'] = relative
            request['semantic_outcome_sha256'] = ao.digest(record)
        ao.atomic(self.directory() / 'state.json', state)

    def test_read_admission_part_delivered_once_with_pinned_digest(self):
        carried = self.state()['requests']['spec_review']['carried']
        self.assertIn(ao_read_admission.PART, carried['parts'])
        self.assertEqual(carried['part_sha256'][ao_read_admission.PART],
                         ao_read_admission.INSTRUCTION_SHA256)
        self.assertEqual(ao.digest(ao_read_admission.INSTRUCTION.encode()),
                         ao_read_admission.INSTRUCTION_SHA256)
        self.assertNotIn(ao_read_admission.PART, self.state()['requests']['impl-1']['carried']['parts'])

    def test_new_parts_arrive_last_in_a_new_session_with_pinned_digests(self):
        carried = self.state()['requests']['spec_review']['carried']
        self.assertEqual(carried['parts'][-3:],
                         [ao_progress.PART_V2, ao_residual_escalation.PART, lifecycle_closure.PART])
        self.assertEqual(carried['part_sha256'][ao_progress.PART_V2], ao_progress.INSTRUCTION_V2_SHA256)
        self.assertEqual(carried['part_sha256'][ao_residual_escalation.PART],
                         ao_residual_escalation.INSTRUCTION_SHA256)
        self.assertEqual(carried['part_sha256'][lifecycle_closure.PART], lifecycle_closure.INSTRUCTION_SHA256)
        impl = self.state()['requests']['impl-1']['carried']
        self.assertNotIn(ao_progress.PART_V2, impl['parts'])
        self.assertNotIn(ao_residual_escalation.PART, impl['parts'])
        self.assertNotIn(lifecycle_closure.PART, impl['parts'])

    def test_a_completed_previous_request_carries_no_notice(self):
        text, carried = self.packet()
        self.assertNotIn('Session window notice', text)
        self.assertNotIn('session_window_notice', carried)

    def test_quota_failure_carries_the_session_window_notice_in_the_named_zone(self):
        self.quota_previous('Session usage limit reached; resets 3am (America/Los_Angeles)')
        moment = datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc)
        notice = ("Session window notice (controller-derived, no new instruction): the previous turn "
                  "impl-1 ended at the account's session limit "
                  "(Session usage limit reached; resets 3am (America/Los_Angeles)). This turn is "
                  "dispatched at 2026-10-03 23:30 PDT; if the provider's five-hour window applies as "
                  "before and starts with this request, expect the limit again no later than about "
                  "04:30 PDT — earlier if the account was used elsewhere since the reset. Size each "
                  "work unit so its worker hands back before then, keep the task list current at "
                  "every unit boundary, and avoid leaving a unit mid-flight at the cut-off.")
        text, carried = self.packet(now=moment)
        self.assertIn(notice, text)
        self.assertEqual(carried['session_window_notice'], {
            'previous_request_id': 'impl-1',
            'reset_text': 'Session usage limit reached; resets 3am (America/Los_Angeles)',
            'window_started_at': '2026-10-04T06:30:00+00:00',
            'expected_cutoff_at': '2026-10-04T11:30:00+00:00',
            'fragment_sha256': ao.digest(notice.encode())})

    def test_the_cutoff_is_five_utc_hours_across_a_dst_transition(self):
        self.quota_previous('Session usage limit reached; resets 3am (America/Los_Angeles)')
        # 2026-03-08 springs forward at 02:00 local: 00:30 PST plus five real hours is 06:30 PDT,
        # never the 05:30 wall-clock arithmetic would claim.
        moment = datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)
        text, carried = self.packet(now=moment)
        self.assertIn('dispatched at 00:30 PST', text)
        self.assertIn('no later than about 06:30 PDT', text)
        self.assertEqual(carried['session_window_notice']['window_started_at'],
                         '2026-03-08T08:30:00+00:00')
        self.assertEqual(carried['session_window_notice']['expected_cutoff_at'],
                         '2026-03-08T13:30:00+00:00')

    def test_the_saved_outcome_terminal_message_wins_over_the_request_snapshot(self):
        self.quota_previous(None, outcome_error_message=
                            "You've hit your session limit \u00b7 resets 3:00am (America/Los_Angeles)")
        moment = datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc)
        text, carried = self.packet(now=moment)
        self.assertIn("(You've hit your session limit \u00b7 resets 3:00am (America/Los_Angeles))",
                      text)
        self.assertIn('23:30 PDT', text)
        self.assertEqual(carried['session_window_notice']['reset_text'],
                         "You've hit your session limit \u00b7 resets 3:00am (America/Los_Angeles)")

    def test_the_notice_is_engineer_only(self):
        self.quota_previous('Session usage limit reached; resets 3am (America/Los_Angeles)')
        self.assertIsNone(ao_workflow.session_window_notice(self.state(), 'reviewer', self.directory()))
        text, carried = self.packet()
        self.assertIn('Session window notice', text)

    def test_an_unnamed_zone_and_a_missing_message_fall_back_to_utc(self):
        self.quota_previous('Session usage limit reached; try again tomorrow (not-a-zone)')
        moment = datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)
        text, _ = self.packet(now=moment)
        self.assertIn('dispatched at 18:00 UTC', text)
        self.assertIn('no later than about 23:00 UTC', text)
        state = self.state()
        request = state['requests']['impl-1']
        request['observed_turn'] = {key: value for key, value in request['observed_turn'].items()
                                    if key != 'errorMessage'}
        ao.atomic(self.directory() / 'state.json', state)
        text, carried = self.packet(now=moment)
        self.assertIn("session limit (the account's session limit)", text)
        self.assertEqual(carried['session_window_notice']['reset_text'], "the account's session limit")


class RetainedSessionPartTests(Fixture):
    """A retained session whose delivered packets predate the two newest parts."""

    def setUp(self):
        super().setUp()
        newest = {ao_progress.PART_V2, ao_residual_escalation.PART}
        with patch.object(ao_workflow, 'PARTS', tuple(p for p in ao_workflow.PARTS if p not in newest)):
            self.room = self.open(); self.spec(); self.bind(); self.agree()
            self.service.ao_room_handoff(self.room, str(self.repo))
            self.send('implementation', 'impl-1')
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)

    def packet(self, purpose='correction', now=None):
        return ao_workflow.packet(self.service, self.directory(), self.state(), 'engineer',
                                  purpose, 'Continue.', now=now)

    def test_the_two_new_parts_deliver_once_to_a_retained_session(self):
        self.send('correction', 'corr-1')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['corr-1']
        self.assertEqual(request['carried']['parts'], [ao_progress.PART_V2, ao_residual_escalation.PART])
        self.assertEqual(request['carried']['part_sha256'],
                         {ao_progress.PART_V2: ao_progress.INSTRUCTION_V2_SHA256,
                          ao_residual_escalation.PART: ao_residual_escalation.INSTRUCTION_SHA256})
        self.assertIn(ao_progress.INSTRUCTION_V2, request['text'])
        self.assertIn(ao_residual_escalation.INSTRUCTION, request['text'])
        _, carried = self.packet()
        self.assertNotIn(ao_progress.PART_V2, carried['parts'])
        self.assertNotIn(ao_residual_escalation.PART, carried['parts'])


if __name__ == '__main__':
    unittest.main()
