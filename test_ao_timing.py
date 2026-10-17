"""Read-only ao_room_timing delivery-timing view: intervals, gaps, bounds, closures and limits.

Synthetic request/verification records are written straight into the saved state through
ao.atomic — the accepted way to seed states, semantic_status and timestamps — while the
verification and wiring tests exercise the real ao_room_verify, MCP and CLI paths.
"""
import contextlib
from datetime import datetime, timezone
import io
import json
import time
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_timing
import project_room
import project_room_mcp
from test_ao_normal import Fixture


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class TimingViewTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()
        self.base = time.time() - 10000.0  # Finished synthetic history stays in the past.

    def view(self):
        return self.service.ao_room_timing(self.room)

    def edit(self, fn):
        state = self.state()
        fn(state)
        ao.atomic(self.directory() / 'state.json', state)

    def add_request(self, key, role='engineer', purpose='implementation', req_state='completed',
                    order=1, created=None, started=None, completed=None, receipt=None,
                    semantic=None, **extra):
        """One synthetic saved request; started/completed are epochs rendered as provider stamps."""
        t = self.base
        request = {'request_id': key, 'role': role, 'purpose': purpose, 'state': req_state,
                   'created_at': t + order * 100.0 if created is None else created,
                   'created_order': order}
        turn = {}
        if started is not None:
            turn['startedAt'] = _iso(started)
        if completed is not None:
            turn['completedAt'] = _iso(completed)
        request['observed_turn'] = turn
        if receipt is not None:
            request['receipt'] = receipt
        if semantic is not None:
            request['semantic_status'] = semantic
        request.update(extra)
        self.edit(lambda state: state['requests'].__setitem__(key, request))
        return request

    def interval(self, view, request_id):
        return next(i for i in view['intervals'] if i.get('request_id') == request_id)

    def test_live_request_is_open_and_moves_no_frontier(self):
        t = self.base
        self.add_request('a', created=t, completed=t + 10, order=1)
        self.add_request('live', req_state='submitted', created=t + 20, order=2)
        self.add_request('b', created=t + 30, completed=t + 40, order=3)
        self.add_request('c', created=t + 50, completed=t + 60, order=4)
        view = self.view()
        live = self.interval(view, 'live')
        self.assertEqual(live['end_basis'], 'open')
        self.assertIsNone(live['ended_at'])
        self.assertIsNone(live['seconds'])
        self.assertEqual(view['coverage']['open_intervals'], 1)
        # An open interval is an unresolved predecessor, never a frontier: the gap before it
        # still measures from the last known end, but every gap after it stays unmeasured,
        # named after the still-open interval.
        self.assertEqual([(g['after'], g['before'], g['kind'], g['seconds'], g['after_end_basis'])
                          for g in view['gaps']],
                         [('a', 'live', 'unattributed', 10.0, None),
                          ('live', 'b', 'unknown_bounds', None, 'open'),
                          ('live', 'c', 'unknown_bounds', None, 'open')])
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 2)

    def test_an_open_predecessor_leaves_every_later_gap_unmeasured(self):
        t = self.base
        self.add_request('quota', req_state='settled_failure', created=t, completed=t + 10,
                         semantic={'kind': 'quota_limit', 'hold': True, 'reason': 'limit'},
                         order=1)
        self.add_request('live', req_state='running', created=t + 20, order=2)
        self.add_request('c', created=t + 30, completed=t + 40, order=3)
        self.add_request('d', created=t + 50, completed=t + 60, order=4)
        view = self.view()
        gaps = view['gaps']
        # The measured quota wait before the open interval is exact.
        self.assertEqual((gaps[0]['after'], gaps[0]['before'], gaps[0]['kind'],
                          gaps[0]['seconds'], gaps[0]['after_end_basis']),
                         ('quota', 'live', 'quota_wait', 10.0, None))
        # The open interval stays pending: both later gaps are unmeasured, after it.
        self.assertEqual([(g['after'], g['before'], g['kind'], g['after_end_basis'])
                          for g in gaps[1:]],
                         [('live', 'c', 'unknown_bounds', 'open'),
                          ('live', 'd', 'unknown_bounds', 'open')])
        self.assertEqual(view['totals']['gaps']['quota_wait'], 10.0)
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 2)

    def test_an_open_predecessor_survives_later_unknown_intervals(self):
        # An open interval stays unresolved across a later terminal interval whose own end is
        # unknown: the unknown end bounds only its one gap and never clears the open
        # predecessor, so every gap after the open interval stays unmeasured.
        t = self.base
        self.add_request('live', req_state='running', created=t, order=1)
        self.add_request('mid', req_state='failed', created=t + 20, receipt='missing/never.json',
                         order=2)
        self.add_request('c', created=t + 30, completed=t + 40, order=3)
        self.add_request('d', created=t + 50, completed=t + 60, order=4)
        view = self.view()
        self.assertEqual([(g['after'], g['before'], g['kind'], g['after_end_basis'])
                          for g in view['gaps']],
                         [('live', 'mid', 'unknown_bounds', 'open'),
                          ('mid', 'c', 'unknown_bounds', 'unknown'),
                          ('live', 'd', 'unknown_bounds', 'open')])
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 3)
        self.assertEqual(view['totals']['gaps']['unattributed'], 0.0)
        self.assertEqual(view['totals']['gaps']['quota_wait'], 0.0)

    def test_an_open_predecessor_survives_later_anomaly_intervals(self):
        # The same with an inverted end: the anomaly bounds its one gap, then the still-open
        # predecessor names the next — nothing is measured.
        t = self.base
        self.add_request('live', req_state='running', created=t, order=1)
        self.add_request('mid', req_state='failed', created=t + 20, started=t + 20,
                         completed=t + 10, order=2)
        self.add_request('c', created=t + 30, completed=t + 40, order=3)
        self.add_request('d', created=t + 50, completed=t + 60, order=4)
        view = self.view()
        self.assertIn('clock_anomaly', view['limitations'])
        self.assertEqual([(g['after'], g['before'], g['kind'], g['after_end_basis'])
                          for g in view['gaps']],
                         [('live', 'mid', 'unknown_bounds', 'open'),
                          ('mid', 'c', 'unknown_bounds', 'anomaly'),
                          ('live', 'd', 'unknown_bounds', 'open')])
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 3)
        self.assertEqual(view['totals']['gaps']['unattributed'], 0.0)

    def test_a_terminal_unknown_end_bounds_only_the_next_gap(self):
        t = self.base
        # Completed with neither a provider completedAt nor a readable receipt.
        self.add_request('mid', created=t + 10, receipt='missing/never.json', order=1)
        self.add_request('next', created=t + 40, completed=t + 50, order=2)
        view = self.view()
        self.assertEqual(len(view['gaps']), 1)
        gap = view['gaps'][0]
        self.assertEqual((gap['after'], gap['before'], gap['kind'], gap['after_end_basis']),
                         ('mid', 'next', 'unknown_bounds', 'unknown'))
        self.assertIsNone(gap['started_at'])
        self.assertIsNone(gap['seconds'])
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 1)
        self.assertEqual(view['totals']['gaps']['unattributed'], 0.0)

    def test_an_inverted_end_bounds_the_next_gap_as_anomaly(self):
        t = self.base
        # A held turn whose saved completedAt precedes its startedAt: the inverted end is an
        # anomaly, never a frontier, so the gap after it is unmeasured and nothing is quota wait.
        self.add_request('held', req_state='failed', created=t + 50, started=t + 50,
                         completed=t + 10,
                         semantic={'kind': 'quota_limit', 'hold': True, 'reason': 'x'}, order=1)
        self.add_request('next', created=t + 100, completed=t + 110, order=2)
        view = self.view()
        self.assertIn('clock_anomaly', view['limitations'])
        held = self.interval(view, 'held')
        self.assertIsNone(held['seconds'])
        self.assertEqual(len(view['gaps']), 1)
        gap = view['gaps'][0]
        self.assertEqual((gap['after'], gap['before'], gap['kind'], gap['after_end_basis']),
                         ('held', 'next', 'unknown_bounds', 'anomaly'))
        self.assertIsNone(gap['seconds'])
        self.assertEqual(view['totals']['gaps']['quota_wait'], 0.0)

    def test_unusable_saved_timestamps_degrade_to_limitations(self):
        t = self.base
        receipt = 'receipts/badend/result.json'
        ao.atomic(self.directory() / receipt, {'observed_at': 10 ** 400})
        self.add_request('ok', created=t, completed=t + 10, order=1)
        # 10**400 overflows float conversion; 10**12 is finite but out of datetime range.
        self.add_request('big-created', created=10 ** 400, completed=t + 20, order=2)
        self.add_request('huge-created', created=10 ** 12, completed=t + 30, order=3)
        self.add_request('big-order', created=t + 40, completed=t + 50, order=10 ** 400)
        self.add_request('bad-end', created=t + 60, receipt=receipt, order=4)
        self.edit(lambda state: state['verifications'].append(
            {'id': 'verify-big', 'state': 'passed', 'started_at': 10 ** 12,
             'finished_at': t + 70, 'candidate_sha256': '0' * 64, 'path': 'x'}))
        view = self.view()
        # The view returns; every unusable saved timestamp is an unknown bound, labelled.
        self.assertIn('timestamp_unusable', view['limitations'])
        self.assertIn('start_time_unavailable', view['limitations'])
        self.assertIn('end_time_unavailable', view['limitations'])
        self.assertIn('verification_timing_not_recorded', view['limitations'])
        intervals = {i.get('request_id'): i for i in view['intervals']}
        self.assertEqual(intervals['big-created']['start_basis'], 'unknown')
        self.assertIsNone(intervals['big-created']['started_at'])
        self.assertEqual(intervals['huge-created']['start_basis'], 'unknown')
        self.assertIsNone(intervals['huge-created']['started_at'])
        self.assertEqual(intervals['bad-end']['end_basis'], 'unknown')
        self.assertIsNone(intervals['bad-end']['ended_at'])
        # An unusable created_order falls back for sorting; the interval itself is intact, as
        # is every other request's interval.
        self.assertAlmostEqual(intervals['big-order']['seconds'], 10.0, places=6)
        self.assertAlmostEqual(intervals['ok']['seconds'], 10.0, places=6)
        attempt = next(a for a in view['verification']['attempts'] if a['id'] == 'verify-big')
        self.assertEqual(attempt['timing'], 'not_recorded')
        self.assertIsNone(attempt['started_at'])
        self.assertIsNotNone(attempt['finished_at'])

    def test_completed_request_uses_provider_times(self):
        t = self.base
        self.add_request('impl-1', created=t, started=t + 5, completed=t + 15, order=1)
        view = self.view()
        interval = self.interval(view, 'impl-1')
        self.assertEqual(interval['start_basis'], 'provider_started_at')
        self.assertEqual(interval['end_basis'], 'provider_completed_at')
        self.assertAlmostEqual(interval['seconds'], 10.0, places=6)
        self.assertEqual(interval['category'], 'engineering')
        self.assertEqual(interval['started_at'], _iso(t + 5))
        self.assertEqual(interval['ended_at'], _iso(t + 15))

    def test_receipt_observed_at_bounds_the_end(self):
        t = self.base
        receipt = 'receipts/r1/result.json'
        ao.atomic(self.directory() / receipt, {'observed_at': t + 7})
        self.add_request('impl-1', created=t, receipt=receipt, order=1)
        interval = self.interval(self.view(), 'impl-1')
        self.assertEqual(interval['end_basis'], 'receipt_observed_at')
        self.assertEqual(interval['ended_at'], _iso(t + 7))
        self.assertAlmostEqual(interval['seconds'], 7.0, places=6)

    def test_terminal_without_end_evidence_makes_unknown_bounds_gaps(self):
        t = self.base
        self.add_request('prev', created=t, completed=t + 10, order=1)
        # Completed with neither a provider completedAt nor a readable receipt.
        self.add_request('mid', created=t + 20, receipt='missing/never.json', order=2)
        self.add_request('next', created=t + 40, completed=t + 50, order=3)
        # A known end after the unknown one resumes normal measurement.
        self.add_request('last', created=t + 70, completed=t + 80, order=4)
        view = self.view()
        mid = self.interval(view, 'mid')
        self.assertEqual(mid['end_basis'], 'unknown')
        self.assertIsNone(mid['ended_at'])
        self.assertIn('end_time_unavailable', view['limitations'])
        self.assertEqual(view['gaps'][0]['kind'], 'unattributed')
        gap = view['gaps'][1]
        self.assertEqual((gap['after'], gap['before']), ('mid', 'next'))
        self.assertEqual(gap['kind'], 'unknown_bounds')
        self.assertEqual(gap['after_end_basis'], 'unknown')
        self.assertIsNone(gap['started_at'])
        self.assertIsNone(gap['seconds'])
        resumed = view['gaps'][2]
        self.assertEqual((resumed['after'], resumed['before'], resumed['kind'],
                          resumed['seconds']), ('next', 'last', 'unattributed', 20.0))
        self.assertIsNone(resumed['after_end_basis'])
        self.assertEqual(view['totals']['gaps']['unknown_bounds'], 1)
        self.assertEqual(view['coverage']['end_unknown'], 1)

    def test_held_quota_turn_opens_a_quota_wait(self):
        t = self.base
        self.add_request('quota', req_state='settled_failure', created=t, completed=t + 60,
                         semantic={'kind': 'quota_limit', 'hold': True, 'reason': 'limit'},
                         order=1)
        self.add_request('after', created=t + 160, completed=t + 200, order=2)
        view = self.view()
        gap = view['gaps'][0]
        self.assertEqual((gap['after'], gap['before'], gap['kind']), ('quota', 'after', 'quota_wait'))
        self.assertEqual(gap['semantic_kind'], 'quota_limit')
        self.assertAlmostEqual(gap['seconds'], 100.0, places=3)
        self.assertEqual(view['totals']['gaps']['quota_wait'], gap['seconds'])

    def test_held_turn_of_another_kind_opens_a_hold_wait(self):
        t = self.base
        self.add_request('held', req_state='failed', created=t, completed=t + 60,
                         semantic={'kind': 'model_mismatch', 'hold': True, 'reason': 'x'},
                         order=1)
        self.add_request('after', created=t + 90, completed=t + 100, order=2)
        gap = self.view()['gaps'][0]
        self.assertEqual(gap['kind'], 'hold_wait')
        self.assertEqual(gap['semantic_kind'], 'model_mismatch')
        self.assertAlmostEqual(gap['seconds'], 30.0, places=3)

    def test_an_unheld_terminal_turn_leaves_the_gap_unattributed(self):
        t = self.base
        self.add_request('done', created=t, completed=t + 60,
                         semantic={'kind': 'final_available', 'hold': False}, order=1)
        self.add_request('after', created=t + 90, completed=t + 100, order=2)
        gap = self.view()['gaps'][0]
        self.assertEqual(gap['kind'], 'unattributed')
        self.assertAlmostEqual(gap['seconds'], 30.0, places=3)

    def test_overlap_is_reported_separately_and_never_added(self):
        t = self.base
        self.add_request('a', created=t, completed=t + 100, order=1)
        # Overlaps a and ends later: the frontier is the furthest known end, so c's gap
        # measures from b's end and no gap is emitted between a and b.
        self.add_request('b', created=t + 50, completed=t + 120, order=2)
        self.add_request('c', created=t + 150, completed=t + 160, order=3)
        view = self.view()
        self.assertEqual([(g['after'], g['before'], g['seconds']) for g in view['gaps']],
                         [('b', 'c', 30.0)])
        self.assertAlmostEqual(view['totals']['sum_seconds'], 180.0, places=6)
        self.assertAlmostEqual(view['totals']['union_seconds'], 130.0, places=6)
        self.assertAlmostEqual(view['totals']['overlap_seconds'], 50.0, places=6)
        self.assertAlmostEqual(view['totals']['overlap_seconds'],
                               view['totals']['sum_seconds'] - view['totals']['union_seconds'],
                               places=6)

    def test_verification_attempts_and_intervals(self):
        self.agree()
        self.implement()
        result = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertTrue(result['passed'])
        entry = self.state()['verifications'][-1]
        self.assertIsInstance(entry['started_at'], float)
        self.assertIsInstance(entry['finished_at'], float)
        view = self.view()
        self.assertEqual(view['verification']['recorded'], 1)
        self.assertEqual(view['verification']['not_recorded'], 0)
        interval = next(i for i in view['intervals'] if i['kind'] == 'verification')
        self.assertEqual((interval['start_basis'], interval['end_basis'], interval['category'],
                          interval['purpose'], interval['role']),
                         ('recorded', 'recorded', 'verification', 'verification', 'controller'))
        self.assertIsNotNone(interval['seconds'])
        self.assertGreaterEqual(interval['seconds'], 0)
        self.assertEqual(view['closure']['verifications']['passed'], 1)
        # An attempt saved without timing keeps its ledger row but cannot enter the timeline.
        self.edit(lambda state: state['verifications'].append(
            {'id': 'verify-old', 'state': 'passed', 'candidate_sha256': '0' * 64, 'path': 'x'}))
        view = self.view()
        self.assertEqual(view['verification']['attempts_total'], 2)
        self.assertEqual(view['verification']['not_recorded'], 1)
        self.assertIn('verification_timing_not_recorded', view['limitations'])
        old = next(a for a in view['verification']['attempts'] if a['id'] == 'verify-old')
        self.assertEqual(old['timing'], 'not_recorded')
        self.assertIsNone(old['seconds'])
        self.assertFalse(any(i.get('id') == 'verify-old' for i in view['intervals']))

    def test_clock_anomaly_reports_null_seconds_and_keeps_the_interval(self):
        t = self.base
        self.add_request('odd', created=t, started=t + 50, completed=t + 10, order=1)
        view = self.view()
        interval = self.interval(view, 'odd')
        self.assertIsNone(interval['seconds'])
        self.assertIn('clock_anomaly', view['limitations'])
        self.assertEqual(view['totals']['sum_seconds'], 0.0)

    def test_malformed_saved_data_degrades_to_limitations(self):
        t = self.base
        self.add_request('no-turn', created=t, semantic='quota_limit', receipt='../escape.json',
                         observed_turn='not-a-dict', order=1)
        self.add_request('no-created', role='reviewer', purpose='acceptance_review',
                         req_state='failed', created=None, order=2)
        state = self.state()
        state['requests']['no-created'].pop('created_at')
        state['requests']['nan'] = {'role': 'engineer', 'purpose': 'implementation',
                                    'state': 'completed', 'created_at': float('nan'),
                                    'created_order': 3,
                                    'observed_turn': {'startedAt': 'junk', 'completedAt': 'junk2'},
                                    'semantic_status': {'kind': 7, 'hold': 'yes'}}
        state['requests']['stray'] = 'not-a-request'
        state['verifications'] = 'not-a-list'
        state['acceptances'] = {'not': 'a list'}
        # allow_nan=False keeps atomic honest; the malformed fixture writes the file itself.
        (self.directory() / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        view = self.view()
        intervals = {i.get('request_id'): i for i in view['intervals']}
        self.assertEqual(intervals['no-turn']['semantic_kind'], 'quota_limit')
        self.assertIsNone(intervals['no-turn']['hold'])
        self.assertEqual(intervals['no-turn']['end_basis'], 'unknown')  # '..' receipt is never read
        self.assertEqual(intervals['no-created']['start_basis'], 'unknown')
        self.assertEqual(intervals['nan']['start_basis'], 'unknown')
        self.assertEqual(intervals['nan']['end_basis'], 'unknown')
        self.assertIsNone(intervals['nan']['semantic_kind'])
        self.assertEqual(view['coverage']['start_unknown'], 2)
        self.assertEqual(view['coverage']['end_unknown'], 3)
        self.assertIn('start_time_unavailable', view['limitations'])
        self.assertIn('end_time_unavailable', view['limitations'])
        self.assertEqual(view['closure']['verifications'],
                         {'passed': 0, 'failed': 0, 'running': 0, 'other': 0})
        self.assertEqual(view['closure']['acceptances_recorded'], 0)
        self.assertIsNone(view['closure']['latest_acceptance_request_id'])

    def test_intervals_and_gaps_keep_the_newest_200(self):
        t = self.base
        def build(state):
            for i in range(205):
                state['requests'][f'r{i:03}'] = {
                    'request_id': f'r{i:03}', 'role': 'engineer', 'purpose': 'implementation',
                    'state': 'completed', 'created_at': t + i * 10, 'created_order': i + 1,
                    'observed_turn': {'completedAt': _iso(t + i * 10 + 5)}}
        self.edit(build)
        view = self.view()
        self.assertEqual(view['intervals_total'], 205)
        self.assertEqual(len(view['intervals']), 200)
        self.assertTrue(view['intervals_truncated'])
        self.assertIn('intervals_truncated', view['limitations'])
        self.assertEqual(view['intervals'][0]['request_id'], 'r005')
        self.assertEqual(view['gaps_total'], 204)
        self.assertEqual(len(view['gaps']), 200)
        self.assertTrue(view['gaps_truncated'])
        self.assertIn('gaps_truncated', view['limitations'])

    def test_view_writes_nothing_and_reads_no_transcript_or_rate_limits(self):
        self.agree()
        self.implement()
        snapshot = {p: p.read_bytes() for p in self.directory().rglob('*') if p.is_file()}
        gets = self.fake.gets
        view = self.view()
        self.assertEqual({p: p.read_bytes() for p in self.directory().rglob('*') if p.is_file()},
                         snapshot)
        self.assertEqual(self.fake.gets, gets)
        self.assertNotIn('rate_limits', view)
        self.assertEqual(view['native_worker_usage'],
                         {'coverage': 'unavailable', 'reason': 'native_worker_usage_not_attributed'})
        self.assertIn('native_worker_usage_not_attributed', view['limitations'])

    def test_mcp_listing_cli_round_trip_and_unknown_room(self):
        self.add_request('a', created=self.base, completed=self.base + 5, order=1)
        response = project_room_mcp.handle(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, project_room.Service(self.home))
        matches = [tool for tool in response['result']['tools'] if tool['name'] == 'ao_room_timing']
        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0]['annotations']['readOnlyHint'], True)
        self.assertIs(matches[0]['annotations']['destructiveHint'], False)
        self.assertEqual(set(matches[0]['inputSchema']['required']), {'room_id'})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch.object(project_room.signal, 'signal'):
            exit_code = project_room.main(['--home', str(self.home), 'call', 'ao_room_timing',
                                           '--args', json.dumps({'room_id': self.room})])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), self.view())
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_timing('not-a-room')

    def test_terminal_vocabulary_and_category_table(self):
        self.assertEqual(ao_timing._TERMINAL_REQUEST_STATES, set(ao.TERMINAL))
        cases = [('reviewer', 'acceptance_review', 'acceptance_review'),
                 ('reviewer', 'implementation', 'acceptance_review'),
                 ('engineer', 'implementation', 'engineering'),
                 ('engineer', 'correction', 'engineering'),
                 ('engineer', 'spec_review', 'spec_review'),
                 ('engineer', 'spec_verdict_repair', 'spec_review'),
                 ('engineer', 'acceptance_review', 'other'),
                 ('engineer', 'unlisted', 'other'),
                 ('operator', 'implementation', 'other')]
        for index, (role, purpose, _expected) in enumerate(cases):
            self.add_request(f'c{index}', role=role, purpose=purpose,
                             created=self.base + index, completed=self.base + index + 0.5,
                             order=index + 1)
        categories = {i.get('request_id'): i['category'] for i in self.view()['intervals']}
        for index, (role, purpose, expected) in enumerate(cases):
            with self.subTest(role=role, purpose=purpose):
                self.assertEqual(categories[f'c{index}'], expected)

    def test_mixed_clock_bases_are_labelled(self):
        t = self.base
        self.add_request('provider', created=t, started=t, completed=t + 10, order=1)
        self.assertNotIn('clock_sources_mixed', self.view()['limitations'])
        self.add_request('controller', created=t + 20, completed=t + 30, order=2)
        self.assertIn('clock_sources_mixed', self.view()['limitations'])
        # A receipt-bound end is a controller basis too.
        self.edit(lambda state: state['requests'].__delitem__('controller'))
        self.assertNotIn('clock_sources_mixed', self.view()['limitations'])


if __name__ == '__main__':
    unittest.main()
