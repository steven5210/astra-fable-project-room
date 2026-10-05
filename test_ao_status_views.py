"""Compact ``ao_room_status`` view: same object minus the request and delegate bulk."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import deepseek_adapter
import ao_project_room as ao
import project_room
import project_room_mcp
from test_ao_normal import DelegateFixture


class CompactStatusTests(DelegateFixture):
    """A completed normal room with ledger jobs, so both bulk sections have content."""

    def setUp(self):
        super().setUp()
        self.bind(); self.agree(); self.implement(); self.review()
        # A busy room: settled requests beyond the per-role latest, like the live rooms
        # the compact view exists for. Cloned records keep every summary field real.
        state = self.state()
        first = state['requests']['implementation']
        base_order = max(request['created_order'] for request in state['requests'].values())
        for index in range(2, 19):
            state['requests']['impl-%d' % index] = dict(
                first, request_id='impl-%d' % index,
                created_order=base_order + index - 1,
                created_at=first['created_at'] + index * 60)
        ao.atomic(self.directory() / 'state.json', state)
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            rows = [('a' * 32, 'completed', '2026-09-09T00:00:01+00:00', '2026-09-09T00:01:01+00:00'),
                    ('b' * 32, 'streaming', '2026-09-09T00:00:02+00:00', None),
                    ('c' * 32, 'unknown_delivery', '2026-09-09T00:00:03+00:00',
                     '2026-09-09T00:02:03+00:00')]
            for job_id, state, created, finished in rows:
                db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,'
                           'state,created_at,requested_model,thinking,reasoning_effort,max_tokens,'
                           'input_bytes,reserved_bytes,possibly_billed,finished_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (job_id, self.room, 'r-' + job_id[:2], 'deep', 'p' * 64, 'q' * 64, state,
                            created, deepseek_adapter.DEFAULT_MODEL, 'enabled', 'max', 393216,
                            100, 0, 1, finished))

    def test_default_and_full_are_the_unchanged_summary(self):
        state = self.state()
        direct = self.service.summary(self.directory(), state)
        default = self.service.ao_room_status(self.room)
        full = self.service.ao_room_status(self.room, view='full')
        self.assertEqual(full, direct)
        self.assertEqual(default, direct)
        self.assertNotIn('view', full)
        self.assertNotIn('request_counts', full)
        self.assertEqual(ao.digest(json.dumps(full, sort_keys=True).encode()),
                         ao.digest(json.dumps(direct, sort_keys=True).encode()))

    def test_compact_keeps_everything_except_the_listed_sections(self):
        full = self.service.ao_room_status(self.room)
        compact = self.service.ao_room_status(self.room, view='compact')
        changed = {'requests', 'request_counts', 'requests_truncated', 'delegate', 'view'}
        self.assertEqual(set(compact), set(full) | {'request_counts', 'view'})
        self.assertEqual({key: value for key, value in compact.items() if key not in changed},
                         {key: value for key, value in full.items() if key not in changed})
        self.assertEqual(compact['view'], 'compact')

    def test_compact_requests_are_the_latest_per_role_with_counts(self):
        compact = self.service.ao_room_status(self.room, view='compact')
        self.assertEqual([request['request_id'] for request in compact['requests']],
                         ['acceptance_review', 'impl-18'])
        self.assertEqual([request['role'] for request in compact['requests']], ['reviewer', 'engineer'])
        full_impl = next(request for request in self.service.ao_room_status(self.room)['requests']
                         if request['request_id'] == 'impl-18')
        self.assertEqual(compact['requests'][1], full_impl)
        self.assertEqual(compact['request_counts'], {'completed': 20})
        self.assertIs(compact['requests_truncated'], True)
        self.assertEqual(len(self.service.ao_room_status(self.room)['requests']), 20)

    def test_compact_delegate_summarizes_jobs_and_reduces_routing(self):
        delegate = self.service.ao_room_status(self.room, view='compact')['delegate']
        self.assertNotIn('items', delegate['jobs'])
        self.assertEqual(delegate['jobs']['summary'], {
            'counts': {'completed': 1, 'streaming': 1, 'unknown_delivery': 1},
            'active': 1, 'unresolved': 1,
            'latest': {'id': 'c' * 32, 'state': 'unknown_delivery',
                       'created_at': '2026-09-09T00:00:03+00:00',
                       'finished_at': '2026-09-09T00:02:03+00:00'}})
        full_routing = self.service.ao_room_status(self.room)['delegate']['routing']
        self.assertEqual(delegate['routing'],
                         {key: full_routing[key] for key in
                          ('status', 'guard_sha256', 'execution_policy', 'agents') if key in full_routing})
        self.assertIn('status', delegate['routing'])

    def test_compact_is_much_smaller_than_full(self):
        full = self.service.ao_room_status(self.room)
        compact = self.service.ao_room_status(self.room, view='compact')
        ratio = len(json.dumps(compact)) / len(json.dumps(full))
        self.assertLess(ratio, 0.4, 'compact %.1f%% of full' % (ratio * 100))

    def test_an_unknown_view_refuses(self):
        for invalid in ('COMPACT', 'latest', '', None, 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ao.RoomError):
                    self.service.ao_room_status(self.room, view=invalid)

    def test_mcp_schema_exposes_the_view_enum(self):
        response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                                           project_room.Service(self.home))
        schema = next(item for item in response['result']['tools'] if item['name'] == 'ao_room_status')
        self.assertEqual(schema['inputSchema']['properties']['view'],
                         {'type': 'string', 'enum': ['full', 'compact']})
        self.assertEqual(set(schema['inputSchema']['required']), {'room_id'})
        self.assertIs(schema['annotations']['readOnlyHint'], True)

    def test_cli_round_trip_accepts_the_optional_view(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(project_room.signal, 'signal'):
            exit_code = project_room.main(['--home', str(self.home), 'call', 'ao_room_status',
                                           '--args', json.dumps({'room_id': self.room,
                                                                 'view': 'compact'})])
        self.assertEqual(exit_code, 0)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed['view'], 'compact')
        self.assertEqual([request['request_id'] for request in printed['requests']],
                         ['acceptance_review', 'impl-18'])

    def test_a_missing_ledger_reports_null_totals_and_the_reason(self):
        (self.home / 'deepseek' / 'ledger.sqlite3').unlink()
        jobs = self.service.ao_room_status(self.room, view='compact')['delegate']['jobs']
        self.assertNotIn('items', jobs)
        self.assertEqual(jobs['summary'], {'counts': None, 'active': None, 'unresolved': None,
                                           'latest': None, 'unavailable_reason': 'ledger_missing'})


class CompactStatusCompleteCountsTests(DelegateFixture):
    """Counts come from the complete ledger, not the bounded latest-20 projection."""

    def setUp(self):
        super().setUp()
        self.bind(); self.agree(); self.implement(); self.review()
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            for index in range(21):
                state = 'failed_after_send' if index == 0 else (
                    'unknown_delivery' if index == 1 else 'completed')
                created = '2026-09-09T00:%02d:00+00:00' % index
                finished = '2026-09-09T00:%02d:30+00:00' % index
                db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,'
                           'state,created_at,requested_model,thinking,reasoning_effort,max_tokens,'
                           'input_bytes,reserved_bytes,possibly_billed,finished_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('%032d' % index, self.room, 'r-%02d' % index, 'deep', 'p' * 64, 'q' * 64,
                            state, created, deepseek_adapter.DEFAULT_MODEL, 'enabled', 'max', 393216,
                            100, 0, 1, finished))
            # The second job is a resolved stop state: it stays out of unresolved.
            db.execute('INSERT INTO resolutions(job_id,room_id,note_sha256,note,created_at) '
                       'VALUES(?,?,?,?,?)',
                       ('%032d' % 1, self.room, 'n' * 64, 'resolved by the user',
                        '2026-09-09T01:00:00+00:00'))

    def test_counts_cover_all_ledger_rows_beyond_the_projection(self):
        full_jobs = self.service.ao_room_status(self.room)['delegate']['jobs']
        self.assertEqual(len(full_jobs['items']), 20)
        self.assertIs(full_jobs['truncated'], True)
        # The oldest job is the unresolved stop state and is outside the projection entirely.
        self.assertNotIn('%032d' % 0, {item['job_id'] for item in full_jobs['items']})
        summary = self.service.ao_room_status(self.room, view='compact')['delegate']['jobs']['summary']
        self.assertEqual(sum(summary['counts'].values()), 21)
        self.assertEqual(summary['counts'],
                         {'completed': 19, 'failed_after_send': 1, 'unknown_delivery': 1})
        self.assertEqual(summary['active'], 0)
        self.assertEqual(summary['unresolved'], 1)
        self.assertEqual(summary['latest'], {'id': '%032d' % 20, 'state': 'completed',
                                             'created_at': '2026-09-09T00:20:00+00:00',
                                             'finished_at': '2026-09-09T00:20:30+00:00'})


if __name__ == '__main__':
    unittest.main()
