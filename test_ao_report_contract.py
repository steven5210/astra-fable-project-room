"""Compact report projection preserves the native verdict and exact evidence."""
import copy
import json

import ao_project_room as ao
import ao_workflow
import ao_report_contract as contract
from test_ao_normal import Fixture


class CompactReportTests(Fixture):
    def setUp(self):
        super().setUp(); self.room = self.open(); self.spec(); self.bind(); self.agree()

    def compact(self, **changes):
        report = self.report(**changes)
        for key in ('spec_revision', 'spec_sha256', 'baseline_commit'):
            report.pop(key)
        return {'report_format': contract.FORMAT, **report}

    def finish(self, **changes):
        self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.raw = self.compact(**changes)
        self.fake.finish('engineer', json.dumps(self.raw))
        self.service.ao_room_sync(self.room)

    def test_compact_report_full_acceptance_preserves_and_labels_native_vs_controller(self):
        self.finish(); state = self.state(); request = state['requests']['implementation']
        native = ao_workflow.final_json(self.directory(), request)
        self.assertEqual(native, self.raw)
        report = ao_workflow.engineering_report(self.directory(), state, request)
        self.assertEqual(report['controller_metadata']['authored_by'], 'project_room_controller')
        self.assertEqual(report['controller_metadata']['native_report_sha256'], ao.digest(self.raw))
        self.assertEqual(report['outcome'], self.raw['outcome'])
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_projection_never_fixes_wrong_types_verdicts_or_missing_judgment(self):
        self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.compact())); self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        for change in ({'outcome': 'partial'}, {'tests_reported': {}}, {'implementation_complete': 'true'}, {'spec_sha256': '0'*64}, {'controller_metadata': {}}):
            value = {**self.compact(), **change}
            with self.subTest(change=change), self.assertRaises(ao.RoomError):
                ao_workflow.engineering_report(self.directory(), self.state(), request, report=value)
        missing = self.compact(); del missing['remaining_gaps']
        with self.assertRaises(ao.RoomError): ao_workflow.engineering_report(self.directory(), self.state(), request, report=missing)

    def test_incomplete_and_scope_change_reports_stay_incomplete(self):
        self.finish(outcome='changes_required', implementation_complete=False, remaining_gaps=['A decision is unresolved'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.compact(outcome='scope_change', implementation_complete=False)))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'scope change'): self.send('correction', 'must-not-send')

    def test_efficiency_update_sent_once_with_no_spec_replay_on_continue(self):
        first = self.state()['requests']['spec_review']
        self.assertIn(contract.PART, first['carried']['parts'])
        self.assertIn('does not require Fable to author all supporting documents', first['text'])
        self.assertIn('Exact-spec review still has no delegation', first['text'])
        self.finish(outcome='changes_required', implementation_complete=False)
        self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'next', purpose='correction')
        self.assertEqual(self.state()['requests']['next']['text'], 'Continue.')
        self.assertEqual(self.state()['requests']['next']['carried']['parts'], [])

    def test_legacy_report_keeps_native_identifiers_and_snapshot(self):
        self.implement()
        request = self.state()['requests']['implementation']
        report = ao_workflow.engineering_report(self.directory(), self.state(), request)
        self.assertNotIn('controller_metadata', report)
        self.assertNotIn('report_format', report)
