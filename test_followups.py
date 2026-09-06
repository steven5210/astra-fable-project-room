"""Synthetic behavior probes for current handoff state, worker liveness and recent activity."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import handoff_status
import heartbeat
import implementation
import progress
import project_room
import recovery
import room
import test_progress


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jobs = self.root / 'jobs'
        self.job = {'id': 'a' * 32, 'status': 'running', 'started_at': '2026-09-06T00:00:00+00:00'}
        self.execution = {'execution_id': 'b' * 32, 'started_at': self.job['started_at']}
        self.path = self.jobs / self.job['id']
        self.path.mkdir(parents=True)
        self.tick = 0.0
        self.now = room.parse_timestamp(self.job['started_at'])
        self.emitter = heartbeat.Emitter(self.jobs, self.job, self.execution['execution_id'], lambda: 2,
                                         monotonic=lambda: self.tick, clock=lambda: (self.now + dt.timedelta(seconds=self.tick)).isoformat())

    def observe(self, **kwargs):
        return heartbeat.observe(self.jobs, kwargs.get('job', self.job), kwargs.get('execution', self.execution),
                                 kwargs.get('now', self.now + dt.timedelta(seconds=self.tick)), kwargs.get('attempt', 2))

    def test_cadence_private_atomic_replace_and_terminal_freeze(self):
        self.assertTrue(self.emitter.pulse())
        p = self.path / 'heartbeat.json'
        first = p.read_bytes()
        self.assertLessEqual(len(first), 4096)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for tick in (0.1, 1, 4.99):
            self.tick = tick
            self.assertFalse(self.emitter.pulse())
            self.assertEqual(first, p.read_bytes())
        self.tick = 5
        self.assertTrue(self.emitter.pulse())
        self.assertEqual(self.observe()['reported_at'], '2026-09-06T00:00:05Z')
        self.assertTrue(self.emitter.pulse(final=True))
        frozen = p.read_bytes()
        self.tick = 100
        self.assertFalse(self.emitter.pulse())
        self.assertEqual(p.read_bytes(), frozen)
        with mock.patch.object(recovery, 'read_owned', side_effect=AssertionError('terminal cannot read heartbeat')):
            self.assertEqual(self.observe(job={**self.job, 'status': 'succeeded'})['unavailable_reason'], 'terminal')
            self.assertEqual(self.observe(job={**self.job, 'status': 'queued'})['unavailable_reason'], 'queued')
        self.assertEqual(list(self.path.iterdir()), [p])

    def test_unavailable_inputs_never_claim_health_or_other_attempt_liveness(self):
        self.assertEqual(self.observe(execution=None)['unavailable_reason'], 'legacy_worker')
        self.assertEqual(self.observe()['unavailable_reason'], 'missing')
        self.emitter.pulse()
        p = self.path / 'heartbeat.json'
        good = json.loads(p.read_text())
        for field, replacement, reason in (
            ('execution_id', 'c' * 32, 'ownership_mismatch'), ('job_id', 'd' * 32, 'ownership_mismatch'),
            ('job_started_at', '2025-01-01T00:00:00Z', 'ownership_mismatch'),
            ('reported_at', '2026-09-06T00:00:03Z', 'future_timestamp'),
            ('attempt', 1, 'attempt_mismatch'), ('attempt', True, 'attempt_mismatch'),
            ('reported_at', 'PRIVATE_PROSE', 'malformed')):
            p.write_text(json.dumps({**good, field: replacement}))
            result = self.observe()
            self.assertEqual(result['unavailable_reason'], reason)
            self.assertFalse(result['available'])
            self.assertNotIn('PRIVATE', json.dumps(result))
        for raw, reason in ((b'{', 'malformed'), (b'{}' * 3000, 'oversized')):
            p.write_bytes(raw)
            self.assertEqual(self.observe()['unavailable_reason'], reason)
        p.write_text(json.dumps({**good, 'private_error': 'PRIVATE_PROSE'}))
        delayed = self.observe(now=self.now + dt.timedelta(days=1))
        self.assertTrue(delayed['available'])
        self.assertEqual(delayed['meaning'], 'worker_liveness_only')
        self.assertNotIn('PRIVATE', json.dumps(delayed))
        self.assertNotIn('healthy', json.dumps(delayed))

    def test_owned_writes_never_follow_symlinks_or_retry_io_at_loop_speed(self):
        outside = self.root / 'outside'
        outside.write_text('untouched')
        (self.path / 'heartbeat.json').symlink_to(outside)
        self.assertTrue(self.emitter.pulse())
        self.assertEqual(outside.read_text(), 'untouched')
        self.assertFalse((self.path / 'heartbeat.json').is_symlink())
        (self.path / 'heartbeat.json').unlink()
        (self.path / 'heartbeat.json').symlink_to(outside)
        self.assertEqual(self.observe()['unavailable_reason'], 'unsafe_or_unreadable')
        self.tick = 5
        with mock.patch.object(recovery, 'write_below', side_effect=OSError('private')) as writer:
            self.assertFalse(self.emitter.pulse())
            self.assertFalse(self.emitter.pulse())
            self.assertEqual(writer.call_count, 1)
        (self.path / 'heartbeat.json').unlink()
        self.path.rmdir()
        destination = self.root / 'other-job'
        destination.mkdir()
        self.path.symlink_to(destination)
        self.tick = 10
        self.assertFalse(self.emitter.pulse())
        self.assertEqual(list(destination.iterdir()), [])


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_progress.ProgressUnitTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_order_collapse_limit_and_private_fields(self):
        f = self.fx
        tools = ['Read', 'Read', 'Edit', 'Bash', 'Skill', 'WebFetch', 'Read', 'Edit']
        records = [f.parent('assistant', f.at(10 + index), [test_progress.tool_use(name, str(index), command='PRIVATE_COMMAND')])
                   for index, name in enumerate(tools)]
        # Transcript append order is not necessarily timestamp order.
        test_progress.write_jsonl(f.transcript, records[::-1])
        output = f.observe()
        value = output['recent_activity']
        self.assertEqual([x['category'] for x in value['items']], ['shell', 'skill', 'other', 'read', 'edit'])
        self.assertTrue(value['truncated'])
        self.assertEqual(value, f.observe()['recent_activity'])
        self.assertNotIn('PRIVATE_COMMAND', json.dumps(value))
        self.assertTrue(all(x['actor'] is None for x in value['items']))
        self.assertEqual(output['activity']['category'], value['items'][-1]['category'])
        self.assertEqual([x['observed_at'] for x in value['items']], sorted(x['observed_at'] for x in value['items']))

    def test_child_attribution_ties_and_incomplete_windows(self):
        f = self.fx
        test_progress.write_jsonl(f.transcript, [
            f.parent('assistant', f.at(1), [test_progress.tool_use('Agent', 'request')], record_uuid='origin'),
            f.parent('assistant', f.at(3), [test_progress.tool_use('Read', 'parent')])])
        f.write_child('valid', [f.child('assistant', f.at(3), 'origin', [test_progress.tool_use('Edit', 'child')])])
        value = f.observe()['recent_activity']
        self.assertEqual([x['category'] for x in value['items']], ['delegate', 'edit', 'read'])
        self.assertEqual(value['items'][1]['actor'], hashlib.sha256(b'request').hexdigest()[:12])
        self.assertNotIn('origin', json.dumps(value))
        self.assertNotIn('valid', json.dumps(value))
        test_progress.write_jsonl(f.transcript, [b'{malformed-private'])
        self.assertTrue(f.observe()['recent_activity']['window_incomplete'])
        for state in ('queued', 'succeeded', 'uncertain'):
            job = progress.job_progress(f.job(state), f.now, handoff=f.handoff())
            self.assertEqual(job['recent_activity']['items'], [])
            self.assertIsNotNone(job['recent_activity']['unavailable_reason'])


class CurrentHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_progress.ProgressServiceTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def snapshot(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.fx.home.rglob('*') if p.is_file()}

    def test_saved_accepted_handoff_and_historical_reads_keep_job_frozen(self):
        f = self.fx
        handoff, manifest = f.prepare()
        job = f.service.room_implementation_submit(f.room_id, handoff['handoff_id'], 'initial')
        terminal = f.service.room_job_status(job['id'], 15)
        self.assertEqual(terminal['status'], 'succeeded')
        frozen = test_progress.without_observation_time(terminal['progress'])
        f.service.room_implementation_review(f.room_id, handoff['handoff_id'], True, 'Verified fixture behavior')
        f.service.room_spec_put(f.room_id, 2, 'Newer planning scope')
        before = self.snapshot()
        with mock.patch.object(implementation, 'candidate_snapshot', side_effect=AssertionError('status must not fingerprint')), \
             mock.patch.object(implementation, 'run_implementation', side_effect=AssertionError('status must not execute')), \
             mock.patch.object(f.service, '_refresh', side_effect=AssertionError('handoff status must not refresh')):
            saved = f.service.room_implementation_status(f.room_id, handoff['handoff_id'])
        self.assertEqual((saved['phase'], saved['spec_revision'], saved['astra_accepted']), ('accepted', 1, True))
        self.assertEqual(saved['meaning'], 'current_saved_handoff_record')
        self.assertTrue(saved['gates_passed'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(frozen, test_progress.without_observation_time(f.service.room_job_status(job['id'])['progress']))
        other = f.service.room_open(str(f.project), 'Other room')
        for rid, hid in ((other['id'], handoff['handoff_id']), (f.room_id, '../../private'), (f.room_id, '0' * 64)):
            with self.assertRaises(room.RoomError):
                f.service.room_implementation_status(rid, hid)
        with self.assertRaises(room.RoomError):
            f.service.room_implementation_submit(f.room_id, handoff['handoff_id'], 'stale')
        # The same read-only result is exposed by both command-line and MCP surfaces.
        args = {'room_id': f.room_id, 'handoff_id': handoff['handoff_id']}
        cli = subprocess.run([sys.executable, str(project_room.ROOT / 'project_room.py'), '--home', str(f.home), 'call',
                              'room_implementation_status', '--args', json.dumps(args)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(cli.stdout), saved)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'room_implementation_status', 'arguments': args}}
        mcp = subprocess.run([sys.executable, str(project_room.ROOT / 'project_room_mcp.py')], input=json.dumps(request)+'\n',
                             capture_output=True, text=True, env={**os.environ, 'PROJECT_ROOM_HOME': str(f.home)}, check=True)
        result = json.loads(mcp.stdout)['result']
        self.assertEqual(result['structuredContent'], saved)
        self.assertEqual(json.loads(result['content'][0]['text']), saved)

    def test_rejection_correction_lineage_and_allowlist(self):
        f = self.fx
        handoff, manifest = f.prepare()
        job = f.service.room_implementation_submit(f.room_id, handoff['handoff_id'], 'initial')
        terminal = f.service.room_job_status(job['id'], 15)
        f.service.room_implementation_review(f.room_id, handoff['handoff_id'], False, 'PRIVATE_REVIEW')
        saved = f.service.room_implementation_status(f.room_id, handoff['handoff_id'])
        self.assertEqual(saved['phase'], 'changes_required')
        self.assertNotIn('PRIVATE_REVIEW', json.dumps(saved))
        f.service.room_implementation_revise(f.room_id, handoff['handoff_id'], 'PRIVATE_CORRECTION')
        self.assertEqual(f.service.room_implementation_status(f.room_id, handoff['handoff_id'])['phase'], 'correction_pending')
        _, _, state = implementation._load(handoff['handoff_path'])
        state.update(phase='recovery_prepared', report={'secret': 'PRIVATE_REPORT'}, error='PRIVATE_ERROR',
                     recovery={'recovery_id': 'c'*32, 'predecessor_job_id': job['id'], 'predecessor_attempt': 1, 'raw': 'PRIVATE_CONTEXT'},
                     recovery_history=[{'status': 'invalidated', 'reason': 'PRIVATE_ERROR', 'at': room.now()}] * 20)
        state['gate_results'][0]['argv'] = ['PRIVATE_COMMAND']
        projected = handoff_status.project(f.room_id, manifest, state)
        self.assertEqual(projected['phase'], 'recovery_prepared')
        self.assertEqual(projected['lineage']['active_recovery']['predecessor_job_id'], job['id'])
        self.assertEqual(len(projected['lineage']['history']), 16)
        self.assertTrue(projected['lineage']['truncated'])
        self.assertNotIn('PRIVATE_', json.dumps(projected))
        self.assertNotIn(str(f.base), json.dumps(projected))
        self.assertEqual(terminal['result'], f.service.room_job_status(job['id'])['result'])

    def test_worker_emits_liveness_without_changing_deadline_or_terminal_result(self):
        f = self.fx
        handoff, manifest = f.prepare(wait=True)
        job = f.service.room_implementation_submit(f.room_id, handoff['handoff_id'], 'heartbeat')
        f.wait_model_started()
        live = f.service.room_job_status(job['id'])['progress']
        self.assertTrue(live['heartbeat']['available'], live)
        self.assertEqual(live['heartbeat']['meaning'], 'worker_liveness_only')
        beat = f.service._job_path(job['id']) / 'heartbeat.json'
        before = beat.read_bytes()
        with mock.patch.object(progress, 'clock', return_value=dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)):
            overdue = f.service.room_job_status(job['id'])['progress']
        self.assertTrue(overdue['deadline']['expired'])
        self.assertTrue(overdue['heartbeat']['available'])
        self.assertEqual(before, beat.read_bytes())
        (f.base / 'release-model').touch()
        terminal = f.service.room_job_status(job['id'], 15)
        self.assertEqual(terminal['status'], 'succeeded')
        self.assertEqual(terminal['progress']['heartbeat']['unavailable_reason'], 'terminal')
        self.assertEqual(terminal['progress']['recent_activity']['items'], [])


if __name__ == '__main__':
    unittest.main()
