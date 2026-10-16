"""Finding-provenance token projection: pure project() tests plus saved-status integration."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import ao_findings
import ao_project_room as ao
import ao_workflow
import project_room
import project_room_mcp
import test_ao_normal as fixtures
from test_ao_deliverables import LEAK


class ProjectionTests(unittest.TestCase):
    """ao_findings.project is pure: it folds one parsed report dict and never raises."""

    def test_fully_tagged_report_counts_every_vocabulary(self):
        report = {'review_findings': [
                      'title one origin=introduced scope=required disposition=open invariant=inv1 evidence=rec1 owner=core',
                      'title two origin=preexisting scope=optional disposition=deferred invariant=inv2 evidence=rec2 owner=ui',
                      'title three origin=integration scope=required disposition=decision_needed invariant=inv3 evidence=rec3 owner=api'],
                  'remaining_gaps': [
                      'gap a origin=preexisting scope=required disposition=open invariant=inv4 evidence=rec4 owner=db',
                      'gap b origin=introduced scope=optional disposition=fixed invariant=inv5 evidence=rec5 owner=docs']}
        view = ao_findings.project(report)
        self.assertEqual(view['version'], 1)
        self.assertEqual(view['assessment'], 'reported')
        self.assertEqual(view['fields']['review_findings'],
                         {'present': True, 'total': 3, 'tagged': 3, 'untagged': 0, 'malformed': 0})
        self.assertEqual(view['fields']['remaining_gaps'],
                         {'present': True, 'total': 2, 'tagged': 2, 'untagged': 0, 'malformed': 0})
        self.assertEqual(view['fields']['backlog'],
                         {'present': False, 'total': 0, 'tagged': 0, 'untagged': 0, 'malformed': 0})
        self.assertEqual((view['total'], view['tagged'], view['untagged'], view['malformed']),
                         (5, 5, 0, 0))
        self.assertEqual(view['by_origin'], {'introduced': 2, 'preexisting': 2, 'integration': 1, 'unknown': 0})
        self.assertEqual(view['by_scope'], {'required': 3, 'optional': 2, 'unknown': 0})
        self.assertEqual(view['by_disposition'],
                         {'fixed': 1, 'open': 2, 'deferred': 1, 'decision_needed': 1, 'unknown': 0})
        self.assertEqual(view['required_open'], 3)
        self.assertEqual(view['preexisting_required_open'], 1)
        self.assertEqual(view['unknown_scope_open'], 0)
        self.assertEqual(view['decision_needed'], 1)
        self.assertEqual(len(view['items']), 5)
        self.assertFalse(view['truncated'])
        first = view['items'][0]
        self.assertEqual(first['field'], 'review_findings')
        self.assertEqual((first['origin'], first['scope'], first['disposition']),
                         ('introduced', 'required', 'open'))
        self.assertEqual((first['invariant'], first['evidence'], first['owner']),
                         ('inv1', 'rec1', 'core'))
        self.assertIsNone(first['origin_raw'])
        self.assertIsNone(first['scope_raw'])
        self.assertIsNone(first['disposition_raw'])
        self.assertTrue(first['tagged'])
        self.assertEqual(first['text'], 'title one')
        self.assertEqual([item['field'] for item in view['items']],
                         ['review_findings'] * 3 + ['remaining_gaps'] * 2)

    def test_partially_tagged_entry_reads_unknown_elsewhere(self):
        view = ao_findings.project({'review_findings': ['found earlier origin=preexisting']})
        item = view['items'][0]
        self.assertTrue(item['tagged'])
        self.assertEqual(item['origin'], 'preexisting')
        self.assertEqual(item['scope'], 'unknown')
        self.assertEqual(item['disposition'], 'unknown')
        self.assertIsNone(item['invariant'])
        self.assertIsNone(item['evidence'])
        self.assertIsNone(item['owner'])
        self.assertEqual(item['text'], 'found earlier')
        self.assertEqual(view['unknown_scope_open'], 1)
        self.assertEqual(view['required_open'], 0)
        self.assertEqual(view['by_scope']['unknown'], 1)
        self.assertEqual(view['by_disposition']['unknown'], 1)

    def test_untagged_report_counts_everything_as_unknown(self):
        view = ao_findings.project({'review_findings': ['first finding', 'second finding'],
                                    'remaining_gaps': ['a gap']})
        self.assertEqual(view['assessment'], 'reported')
        self.assertEqual((view['total'], view['tagged'], view['untagged']), (3, 0, 3))
        self.assertEqual(view['by_origin']['unknown'], 3)
        self.assertEqual(view['by_scope']['unknown'], 3)
        self.assertEqual(view['by_disposition']['unknown'], 3)
        self.assertEqual(view['unknown_scope_open'], 3)
        for item in view['items']:
            self.assertFalse(item['tagged'])
            self.assertEqual((item['origin'], item['scope'], item['disposition']),
                             ('unknown', 'unknown', 'unknown'))

    def test_invalid_vocabulary_values_keep_the_bounded_raw(self):
        view = ao_findings.project({'review_findings': [
            'x origin=upstream scope=maybe disposition=done',
            'y origin=' + 'x' * 40]})
        first, second = view['items']
        self.assertEqual((first['origin'], first['scope'], first['disposition']),
                         ('unknown', 'unknown', 'unknown'))
        self.assertEqual((first['origin_raw'], first['scope_raw'], first['disposition_raw']),
                         ('upstream', 'maybe', 'done'))
        self.assertEqual(second['origin'], 'unknown')
        self.assertEqual(second['origin_raw'], 'x' * 32)
        self.assertEqual(view['by_origin']['unknown'], 2)
        self.assertEqual(view['by_scope']['unknown'], 2)
        self.assertEqual(view['by_disposition']['unknown'], 2)

    def test_preexisting_required_open_combinations(self):
        open_report = {'remaining_gaps': ['gap origin=preexisting scope=required disposition=open']}
        view = ao_findings.project(open_report)
        self.assertEqual(view['required_open'], 1)
        self.assertEqual(view['preexisting_required_open'], 1)
        fixed_report = {'remaining_gaps': ['gap origin=preexisting scope=required disposition=fixed']}
        view = ao_findings.project(fixed_report)
        self.assertEqual(view['required_open'], 0)
        self.assertEqual(view['preexisting_required_open'], 0)
        deferred = {'remaining_gaps': ['gap origin=integration scope=required disposition=deferred']}
        view = ao_findings.project(deferred)
        self.assertEqual(view['required_open'], 1)
        self.assertEqual(view['preexisting_required_open'], 0)

    def test_last_occurrence_wins_and_empty_values_are_absent(self):
        view = ao_findings.project({'review_findings': [
            'a origin=introduced middle origin=preexisting',
            'b scope=']})
        first, second = view['items']
        self.assertEqual(first['origin'], 'preexisting')
        self.assertEqual(first['text'], 'a middle')
        self.assertFalse(second['tagged'])
        self.assertEqual(second['scope'], 'unknown')
        self.assertIsNone(second['scope_raw'])
        self.assertEqual(second['text'], 'b')

    def test_paths_in_text_and_tokens_are_redacted(self):
        view = ao_findings.project({'review_findings': [
            'see ' + LEAK + ' now evidence=' + LEAK + ' owner=' + LEAK + ' scope=required']})
        self.assertNotIn(LEAK, json.dumps(view))
        item = view['items'][0]
        self.assertEqual(item['text'], 'see <path> now')
        self.assertEqual(item['evidence'], '<path>')
        self.assertEqual(item['owner'], '<path>')

    def test_items_are_bounded_and_text_is_clipped(self):
        view = ao_findings.project({'review_findings': ['entry %02d origin=introduced' % i
                                                        for i in range(20)]})
        self.assertEqual(len(view['items']), 16)
        self.assertEqual(view['items_total'], 20)
        self.assertTrue(view['truncated'])
        long_entry = 'word ' * 60 + 'origin=preexisting'
        view = ao_findings.project({'review_findings': [long_entry]})
        self.assertEqual(len(view['items'][0]['text']), 160)

    def test_malformed_entries_and_fields_degrade_without_raising(self):
        view = ao_findings.project({'review_findings': ['kept origin=introduced', 3, None]})
        self.assertEqual(view['fields']['review_findings'],
                         {'present': True, 'total': 3, 'tagged': 1, 'untagged': 0, 'malformed': 2})
        self.assertEqual(view['malformed'], 2)
        self.assertEqual(view['items_total'], 1)
        self.assertEqual([item['text'] for item in view['items']], ['kept'])
        view = ao_findings.project({'review_findings': 'not a list'})
        self.assertEqual(view['fields']['review_findings'],
                         {'present': True, 'total': 0, 'tagged': 0, 'untagged': 0, 'malformed': 1})
        self.assertEqual(view['items'], [])
        for value in ({'outcome': 'x'}, {}, None, 'not a dict', 4):
            with self.subTest(value=value):
                view = ao_findings.project(value)
                self.assertEqual(view['assessment'], 'not_reported')
                self.assertEqual(view['total'], 0)
                self.assertEqual(view['items'], [])

    def test_backlog_counts_like_the_other_fields(self):
        view = ao_findings.project({'backlog': ['idea origin=introduced scope=optional disposition=deferred']})
        self.assertEqual(view['fields']['backlog'],
                         {'present': True, 'total': 1, 'tagged': 1, 'untagged': 0, 'malformed': 0})
        self.assertEqual(view['total'], 1)
        self.assertEqual(view['items'][0]['field'], 'backlog')
        self.assertEqual(view['by_scope']['optional'], 1)
        self.assertEqual(view['unknown_scope_open'], 0)
        self.assertEqual(view['required_open'], 0)


class StatusIntegrationTests(fixtures.Fixture):
    """The projection is a saved-status view only: it reads the captured report and grants nothing."""

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()
        self.agree()

    def findings(self, view='full'):
        return self.service.ao_room_status(self.room, view=view)['review_followups']

    def test_findings_in_full_and_compact_status(self):
        self.implement(review_findings=['new defect origin=introduced scope=required disposition=open'],
                       remaining_gaps=['old defect origin=preexisting scope=required disposition=open'],
                       backlog=['idea origin=introduced scope=optional disposition=deferred'])
        findings = self.findings()['engineering']['findings']
        self.assertEqual(findings['required_open'], 2)
        self.assertEqual(findings['preexisting_required_open'], 1)
        self.assertEqual(findings['tagged'], 3)
        self.assertEqual(findings['by_origin'], {'introduced': 2, 'preexisting': 1,
                                                 'integration': 0, 'unknown': 0})
        compact = self.service.ao_room_status(self.room, view='compact')
        self.assertEqual(compact['review_followups']['engineering']['findings'], findings)

    def test_spec_review_and_missing_engineering_carry_no_findings(self):
        summaries = self.findings()
        self.assertEqual(summaries['engineering']['status'], 'no_request')
        self.assertNotIn('findings', summaries['engineering'])
        self.assertNotIn('findings', summaries['spec_review'])
        self.implement()
        self.assertNotIn('findings', self.findings()['spec_review'])

    def test_tagged_gaps_do_not_change_acceptance_and_status_writes_nothing(self):
        self.implement(remaining_gaps=['gap one origin=preexisting scope=required disposition=fixed'])
        with self.assertRaisesRegex(ao.RoomError, 'Engineering is incomplete or has remaining gaps'):
            ao_workflow.engineering_ready(self.service, self.directory(), self.state())
        before = (self.directory() / 'state.json').read_bytes()
        findings = self.findings()['engineering']['findings']
        self.assertEqual(findings['by_disposition']['fixed'], 1)
        self.assertEqual(findings['required_open'], 0)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)

    def test_mcp_call_and_cli_round_trip_include_findings(self):
        self.implement(remaining_gaps=['gap one origin=preexisting scope=required disposition=open'])
        controller = project_room.Service(self.home)
        with patch.object(project_room.ao_project_room, 'Service', return_value=self.service):
            response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                               'params': {'name': 'ao_room_status',
                                                          'arguments': {'room_id': self.room}}},
                                               controller)
        self.assertFalse(response['result']['isError'], response)
        findings = response['result']['structuredContent']['review_followups']['engineering']['findings']
        self.assertEqual(findings['required_open'], 1)
        self.assertEqual(findings['preexisting_required_open'], 1)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(project_room.signal, 'signal'):
            exit_code = project_room.main(['--home', str(self.home), 'call', 'ao_room_status',
                                           '--args', json.dumps({'room_id': self.room})])
        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed['review_followups']['engineering']['findings'], findings)


if __name__ == '__main__':
    unittest.main()
