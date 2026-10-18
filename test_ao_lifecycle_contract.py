"""Executable AO lifecycle contract: end-to-end scenarios through the production controller and MCP
boundaries with fake backends only; no account, network, AO process or model call."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture

OPEN_GAPS = (
    {"id": "GAP-FS-1", "family": "failed_startup",
     "contract": ("after a definitive failed native startup (prepared, never bound, AO removed the "
                  "workspace, seed session with zero turns/no owner), a supported audited re-preparation "
                  "exists once no delivered work and no live owner are proven; ambiguous or progressed "
                  "state is refused"),
     "baseline": "no supported lane: every ordinary path refuses (see demonstrated_by)",
     "owner": ("failed-startup recovery task, controller branch codex/startup-bootstrap-engineer "
               "(latest candidate ef81704c), not published in this repository"),
     "demonstrated_by": "FailedStartupTests.test_dead_end_is_safe_but_has_no_supported_repreparation_lane"},
    {"id": "GAP-AR-1", "family": "automation_reconciliation",
     "contract": ("after an AO automation (origin=automation, e.g. the CI-failure automation) completes an "
                  "instruction-bearing turn in the retained engineer session that no Project Room request "
                  "owns, an explicit read-only audit followed by a narrowly authorized reconciliation of "
                  "that exact positively proven external turn permits a NEW ordinary report in the SAME "
                  "retained session; capture, verification and independent acceptance still follow"),
     "baseline": ("safe refusal only: ao_room_outcome_audit and a report-only ao_room_send both refuse "
                  "before dispatch with `Cannot attribute semantic outcome to an unchanged completed "
                  "native result` (ao_outcomes.observe); no supported progress lane"),
     "owner": ("automation-reconciliation repair, controller branch "
               "codex/automation-reconciliation-engineer (baseline bd6a326; last complete candidate "
               "e49f1937 independently rejected), unpublished"),
     "demonstrated_by": "ExternalTurnTests.test_automation_origin_turn_is_refused_safely_but_has_no_reconciliation_lane"},
    {"id": "GAP-CAP-1", "family": "capacity",
     "contract": ("complete native histories above 64,000,000 bytes are read under finite budgets before "
                  "completion/ownership evidence is judged"),
     "baseline": "rejected above 64,000,000 bytes",
     "owner": ("PR #218 codex/native-evidence-capacity-engineer (its own tests "
               "test_ao_native_transcript_capacity.py / test_ao_native_transcript_reader.py)"),
     "demonstrated_by": None},
    {"id": "GAP-TT-1", "family": "terminal_turn_binding",
     "contract": ("the immutable terminal-turn fields of an owned completed request (state, "
                  "providerTurnId, completedAt, stopReason) match the saved receipt at every later "
                  "observation and admission; a change refuses without writing"),
     "baseline": ("ao_outcomes.observe binds turn id, providerTurnId, messages and conversation/branch "
                  "identity but not completedAt or stopReason: a changed completedAt is admitted and "
                  "mints a new semantic-outcome record"),
     "owner": ("unassigned: Delivery 2 candidate pending evidence of how AO rewrites live turn rows on "
               "restart; overlaps review finding B5 of the automation-reconciliation lane"),
     "demonstrated_by": "ChangedCandidateTests.test_changed_completed_at_after_capture_is_admitted_and_mints_a_new_semantic_record"},
)


class LifecycleFixture(Fixture):
    """Shared lifecycle harness: durable-effect snapshots, exact refusals and the MCP boundary."""
    TEMPLATE_SETUP = True

    def effects(self):
        """Immutable durable evidence plus dispatch/counter surface.

        Covers POST count, every saved request state, acceptances, verifications, the checkpoint/spec/
        preparation/handoff digests, the purpose counters, and the bytes of every immutable artifact a
        refused operation must never touch: preparation.json, the spec record, and each request's
        receipt, engineering record and completion-candidate file. state.json itself is excluded
        because a refused operation may legitimately rewrite it through reconciliation invalidation.
        """
        state = self.state()
        directory = self.directory()
        artifacts = {}
        preparation = directory / 'preparation.json'
        if preparation.exists():
            artifacts['preparation.json'] = hashlib.sha256(preparation.read_bytes()).hexdigest()
        if state.get('spec'):
            path = directory / state['spec']
            artifacts[state['spec']] = hashlib.sha256(path.read_bytes()).hexdigest()
        for request in state['requests'].values():
            for key in ('receipt', 'engineering_record', 'completion_candidate'):
                relative = request.get(key)
                if isinstance(relative, str):
                    artifacts[relative] = hashlib.sha256((directory / relative).read_bytes()).hexdigest()
        status = self.service.ao_room_status(self.room)
        counters = {key: status[key] for key in
                    ('spec_review_attempts', 'acceptance_review_attempts', 'spec_verdict_recovery_attempts')}
        return {'posts': len(self.fake.posts),
                'request_states': {key: request['state'] for key, request in state['requests'].items()},
                'acceptances': len(state['acceptances']), 'verifications': len(state['verifications']),
                'checkpoint_sha256': state.get('checkpoint_sha256'),
                'spec_record_sha256': state.get('spec_record_sha256'),
                'preparation_sha256': state.get('preparation_sha256'),
                'handoff_sha256': state.get('handoff_sha256'),
                'counters': counters, 'artifacts': artifacts}

    def assert_no_effects(self, before):
        self.assertEqual(self.effects(), before)

    def refuses(self, pattern, fn, *args, **kwargs):
        with self.assertRaisesRegex((ao.RoomError, OSError), pattern) as caught:
            fn(*args, **kwargs)
        return caught.exception

    def _mcp_call(self, name, arguments):
        service = project_room.Service(self.root / 'mcp-state')
        with patch.object(project_room.ao_project_room, 'Service', return_value=self.service):
            return project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                            'params': {'name': name,
                                                       'arguments': {'room_id': self.room, **arguments}}},
                                           service)['result']

    def mcp(self, name, **arguments):
        result = self._mcp_call(name, arguments)
        self.assertFalse(result['isError'], result)
        return result['structuredContent']

    def mcp_error(self, name, **arguments):
        result = self._mcp_call(name, arguments)
        self.assertTrue(result['isError'], result)
        text = result['content'][0]['text']
        try:
            return json.loads(text)['error']
        except (ValueError, KeyError, TypeError):
            return text

    def native_failure_rows(self, request_id, error='rate_limit', status=429):
        """Synthetic native quota-failure evidence for one already-sent request.

        Appends the caller row and one isApiErrorMessage row exactly as the quota-recovery fixture does;
        the caller must dispatch through ao_room_send directly (not send()) so this helper owns the
        request's native rows.
        """
        request = self.state()['requests'][request_id]
        stamp = lambda offset: min(datetime.fromtimestamp(request['created_at'] + offset, timezone.utc),
                                   datetime.now(timezone.utc)).isoformat()
        self.native_events.extend([
            {'type': 'user', 'uuid': request_id + '-caller', 'sessionId': self.NATIVE,
             'timestamp': stamp(0.001), 'cwd': self.native_row_workspace(), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': request_id + '-error', 'sessionId': self.NATIVE,
             'timestamp': stamp(0.002), 'cwd': self.native_row_workspace(), 'isSidechain': False,
             'isApiErrorMessage': True, 'error': error, 'apiErrorStatus': status,
             'message': {'role': 'assistant', 'model': '<synthetic>',
                         'content': [{'type': 'text', 'text': 'Synthetic native session limit'}]}}])
        self.transcript.write_text(''.join(json.dumps(row) + '\n' for row in self.native_events))
        self.native_requests.add(request_id)

    def room_listing(self):
        directory = self.directory()
        return sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*'))


class PreparedRoomFixture(LifecycleFixture):
    TEMPLATE_ATTRS = LifecycleFixture.TEMPLATE_ATTRS + ('room',)

    def build_template(self):
        super().build_template()
        self.room = self.open()
        self.spec()


class AgreedRoomFixture(LifecycleFixture):
    TEMPLATE_ATTRS = LifecycleFixture.TEMPLATE_ATTRS + ('room',)

    def build_template(self):
        super().build_template()
        self.room = self.open()
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])


class NormalCompletionTests(AgreedRoomFixture):
    """A: the supported workflow boundary end to end, with durable and counter assertions."""

    def test_supported_workflow_reaches_capture_verification_and_acceptance(self):
        self.implement()
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'completed')
        self.assertIn('engineering_record', request)
        self.assertIn('result_candidate_sha256', request)
        self.assertNotIn('engineering_error', request)
        self.review()
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['latest_verification']['state'], 'passed')
        self.assertTrue(status['checkpoint'])
        result = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(result['accepted'])
        self.assertEqual(len(self.state()['acceptances']), 1)
        self.assertEqual(len(self.fake.posts), 3)
        self.assertEqual(status['spec_review_attempts'], 1)
        self.assertEqual(status['acceptance_review_attempts'], 1)
        self.assertEqual(status['spec_verdict_recovery_attempts'], 0)
        self.assertEqual(result, self.service.ao_room_accept(self.room, 'acceptance_review'))
        self.assertEqual(len(self.state()['acceptances']), 1)
        self.assertEqual(len(self.fake.posts), 3)

    def test_status_views_agree_at_the_accepted_boundary(self):
        self.implement()
        self.review()
        self.service.ao_room_accept(self.room, 'acceptance_review')
        full = self.service.ao_room_status(self.room, view='full')
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(compact['latest_acceptance'], full['latest_acceptance'])
        counts = {}
        for request in full['requests']:
            counts[request['state']] = counts.get(request['state'], 0) + 1
        self.assertEqual(compact['request_counts'], counts)
        self.assertFalse(full['requests_truncated'])
        # The compact view keeps only the latest request per role, so "truncated" means exactly this.
        self.assertEqual(compact['requests_truncated'],
                         len(self.state()['requests']) > len(compact['requests']))


class FailedStartupTests(PreparedRoomFixture):
    """B incident family: preparation+routing succeeded, AO's native session creation then failed
    definitively, AO removed the prepared workspace but kept an empty seed session with zero
    turns/messages/activities, no owner row and no session-worktree row; the room has no engineer
    binding and no requests."""

    def _dead_end(self):
        worktree = self.root / 'engineer-worktree'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(worktree), 'HEAD'],
                       check=True, capture_output=True)
        self.service.ao_room_prepare(self.room, str(worktree))
        self.assertEqual(self.state()['preparation_status'], 'configured')
        shutil.rmtree(worktree)  # AO's removal leaves the stale .git/worktrees registration.
        self.fake.workspaces['engineer'] = str(worktree)  # seed session still names the removed path
        return worktree

    def test_dead_end_is_safe_but_has_no_supported_repreparation_lane(self):
        """Demonstrates GAP-FS-1: the baseline is safe (no dispatch, no counter or spec change) but
        offers no supported re-preparation; the pending owner fix is named in OPEN_GAPS. This test
        pins the dead end; it does not claim the contract is met."""
        worktree = self._dead_end()
        before = self.effects()
        exc = self.refuses('No such file or directory',
                           self.service.ao_room_prepare, self.room, str(worktree))
        self.assertIsInstance(exc, FileNotFoundError)
        self.assert_no_effects(before)
        second = self.root / 'engineer-worktree-2'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(second), 'HEAD'],
                       check=True, capture_output=True)
        self.refuses('Prepared engineer workspace is immutable',
                     self.service.ao_room_prepare, self.room, str(second))
        self.assert_no_effects(before)
        exc = self.refuses('No such file or directory',
                           self.service.ao_room_bind, self.room, 'engineer', 'engineer',
                           ao_workflow.FABLE_MODEL, 'max')
        self.assertIsInstance(exc, FileNotFoundError)
        self.assert_no_effects(before)
        self.refuses('Bind the requested role first',
                     self.service.ao_room_send, self.room, 'engineer', 'x', 'r1', purpose='spec_review')
        self.assert_no_effects(before)
        self.refuses('current exact spec requires completed Fable agreement',
                     self.service.ao_room_handoff, self.room, str(worktree))
        self.assert_no_effects(before)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['delegate']['attachment'], 'unverified')
        self.assertTrue(status['delegate']['error'])
        state = self.state()
        self.assertEqual(state['requests'], {})
        self.assertEqual(state['spec'], 'specs/1.json')
        self.assertEqual(status['spec_review_attempts'], 0)
        self.assertEqual(len(self.fake.posts), 0)

    def test_mcp_surface_reports_the_dead_end_without_side_effects(self):
        self._dead_end()
        before = self.effects()
        error = self.mcp_error('ao_room_bind', role='engineer', session_id='engineer',
                               model=ao_workflow.FABLE_MODEL, reasoning_effort='max')
        # The baseline surfaces the raw OS error through the MCP error payload, not a RoomError diagnosis.
        self.assertIn('No such file or directory', error)
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(compact['delegate']['attachment'], 'unverified')
        self.assert_no_effects(before)


class ProgressedRoomWorkspaceLossTests(AgreedRoomFixture):
    """B3: a progressed room loses the prepared workspace's routing evidence, not the repo itself."""

    def test_progressed_room_with_removed_workspace_refuses_new_dispatch(self):
        self.implement()
        shutil.rmtree(self.repo / '.claude')  # the prepared routing evidence under the worktree
        before = self.effects()
        posts = len(self.fake.posts)
        self.refuses('Native routing evidence is unreadable or inconsistent', self.send, 'correction')
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_no_effects(before)
        routing = self.service.ao_room_status(self.room)['delegate']['routing']
        self.assertEqual(routing['status'], 'unverified')
        self.assertIn('unreadable or inconsistent', routing['error'])


class MalformedReportTests(AgreedRoomFixture):
    """C: malformed or contradicting engineering reports never convert to captured success."""

    def _implement_with_text(self, text, request_id='implementation', purpose='implementation'):
        self.send(purpose, request_id)
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', text)
        self.service.ao_room_sync(self.room)
        return self.state()['requests'][request_id]

    def test_missing_fields_report_is_captured_as_engineering_error_not_success(self):
        request = self._implement_with_text(json.dumps({'outcome': 'completed'}))
        self.assertEqual(request['state'], 'completed')
        self.assertEqual(request['semantic_status']['kind'], 'final_available')
        self.assertRegex(request['engineering_error'], 'Engineering report is incomplete')
        self.assertNotIn('engineering_record', request)
        # Observed baseline: verification itself refuses before the gates run because no completed
        # engineering result exists, so the acceptance-review attempt can never be dispatched.
        self.refuses('Acceptance requires a captured completed engineering result',
                     self.service.ao_room_verify, self.room, str(self.repo))
        self.refuses('Create a passed verification checkpoint first', self.send, 'acceptance_review')
        self.refuses('Acceptance requires a captured completed engineering result',
                     self.service.ao_room_accept, self.room, 'acceptance_review')
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['acceptance_review_attempts'], 0)
        self.assertEqual(self.state()['acceptances'], [])

    def test_wrong_outcome_value_and_contradicting_metadata_refuse_capture(self):
        cases = [('implementation', 'implementation', self.report(outcome='partial'),
                  'incomplete or refers to another spec/handoff'),
                 ('bad-metadata', 'correction', self.report(spec_sha256='0' * 64),
                  'incomplete or refers to another spec/handoff'),
                 ('bad-compact', 'correction', self.report(report_format='project_room_engineering_v2',
                                                          spec_sha256='0' * 64),
                  'contradicts the exact bound metadata')]
        for request_id, purpose, report, pattern in cases:
            with self.subTest(request_id=request_id):
                request = self._implement_with_text(json.dumps(report), request_id, purpose)
                self.assertEqual(request['state'], 'completed')
                self.assertRegex(request['engineering_error'], pattern)
                self.assertNotIn('engineering_record', request)

    def test_prose_only_final_is_captured_as_engineering_error_and_admits_correction(self):
        request = self._implement_with_text('All done, no JSON.')
        self.assertEqual(request['state'], 'completed')
        self.assertEqual(request['semantic_status']['kind'], 'final_available')
        self.assertRegex(request['engineering_error'], 'unambiguous JSON object')
        posts = len(self.fake.posts)
        self.send('correction')  # the diagnosed correction loop admits a report retry
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['correction']['state'], 'completed')

    def test_malformed_report_never_auto_converts_to_success_on_resync(self):
        self._implement_with_text(json.dumps({'outcome': 'completed'}))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_sync(self.room)
        self.mcp('ao_room_sync')
        request = self.state()['requests']['implementation']
        self.assertIn('engineering_error', request)
        self.assertNotIn('engineering_record', request)
        self.assertTrue(request['completion_capture_attempted'])
        self.assertEqual(len(list((self.directory() / 'completion-candidates').iterdir())), 1)


class InterruptedAndQuotaTests(AgreedRoomFixture):
    """E: interrupted delivery never replays; a quota failure holds until one authorized successor."""

    def test_quota_failure_holds_then_one_authorized_successor_proceeds(self):
        posts = len(self.fake.posts)
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'implementation', purpose='implementation')
        self.native_failure_rows('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertEqual(request['observed_turn']['state'], 'failed')
        self.assertIn('reconciliation', request)
        receipt_bytes = (self.directory() / request['receipt']).read_bytes()
        before = self.effects()
        self.refuses('An owned request is active or uncertain', self.send, 'correction', 'blocked')
        self.assert_no_effects(before)
        self.assertEqual(len(self.fake.posts), posts + 1)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertEqual(audit['outcome']['kind'], 'quota_limit')
        self.assertTrue(audit['resume_eligible'])
        request = self.state()['requests']['implementation']
        self.assertEqual((self.directory() / request['receipt']).read_bytes(), receipt_bytes)
        resume = self.service.ao_room_outcome_resume(
            self.room, 'implementation', audit['outcome_sha256'], 'resume-1',
            'Synthetic native rate-limit diagnosis; standing quota continuation authority',
            'User authorized one continuation after the diagnosed quota failure')
        self.assertEqual(self.state()['requests']['implementation']['state'], 'settled_failure')
        listing = self.room_listing()
        self.assertEqual(self.service.ao_room_outcome_resume(
            self.room, 'implementation', audit['outcome_sha256'], 'resume-1',
            'Synthetic native rate-limit diagnosis; standing quota continuation authority',
            'User authorized one continuation after the diagnosed quota failure'), resume)
        self.assertEqual(self.room_listing(), listing)
        self.refuses('Native semantic hold: quota_limit', self.send, 'correction', 'resume-2')
        posts = len(self.fake.posts)
        self.send('correction', 'resume-1')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        resumed = self.state()['requests']['resume-1']
        self.assertEqual(resumed['state'], 'completed')
        self.assertIn('engineering_record', resumed)

    def test_interrupted_turn_stays_uncertain_and_is_never_replayed(self):
        posts = len(self.fake.posts)
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'implementation', purpose='implementation')
        self.fake.finish('engineer', state='interrupted')
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertIn('reconciliation', request)
        before = self.effects()
        again = self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                          'implementation', purpose='implementation')
        self.assertEqual(again['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.refuses('An owned request is active or uncertain', self.send, 'correction')
        self.service.ao_room_sync(self.room)
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['implementation']['state'], 'uncertain')
        # Observed baseline: the audit refuses at the owned-request gate before any outcome judgment.
        self.refuses('An owned request is active or uncertain',
                     self.service.ao_room_outcome_audit, self.room)
        self.assert_no_effects(before)

    def test_resume_refuses_when_evidence_changed_after_the_audit(self):
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'implementation', purpose='implementation')
        self.native_failure_rows('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        audit = self.service.ao_room_outcome_audit(self.room)
        listing = self.room_listing()
        self.fake.snapshots['engineer']['turns'].append(
            {'id': 'automation-1', 'state': 'completed', 'providerTurnId': 'native-automation-1',
             'stopReason': 'end_turn'})
        self.refuses('Cannot attribute semantic outcome to an unchanged completed native result',
                     self.service.ao_room_outcome_resume, self.room, 'implementation',
                     audit['outcome_sha256'], 'resume-1', 'Diagnosis recorded after the audit',
                     'Authorized one named continuation')
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('outcome_resume', request)
        self.assertEqual(self.room_listing(), listing)

    def test_resume_refuses_when_the_failed_turns_own_evidence_changed_after_the_audit(self):
        self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                  'implementation', purpose='implementation')
        self.native_failure_rows('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        audit = self.service.ao_room_outcome_audit(self.room)
        listing = self.room_listing()
        snapshot = self.fake.snapshots['engineer']
        snapshot['turns'][-1]['errorMessage'] = 'changed after audit'
        snapshot.setdefault('sessionFailures', []).append(
            {'turnId': snapshot['turns'][-1]['id'], 'type': 'provider_error'})
        self.refuses('Outcome evidence changed or does not establish an eligible diagnosed failure',
                     self.service.ao_room_outcome_resume, self.room, 'implementation',
                     audit['outcome_sha256'], 'resume-1', 'Diagnosis recorded after the audit',
                     'Authorized one named continuation')
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertNotIn('outcome_resume', request)
        # Observed baseline: the refusal happens after observe() has already minted the changed-evidence
        # outcome record and moved the semantic_outcome pointer, so it is not write-free.
        self.assertNotEqual(request['semantic_outcome_sha256'], audit['outcome_sha256'])
        self.assertEqual(self.room_listing(),
                         sorted(listing + ['outcomes/implementation/'
                                           + request['semantic_outcome_sha256'] + '.json']))


class DuplicateAndUncertainDeliveryTests(AgreedRoomFixture):
    """D: idempotent resend reads the record; lost acknowledgements reconcile without replay."""

    def test_identical_resend_reads_the_record_and_different_payload_refuses(self):
        posts = len(self.fake.posts)
        first = self.send('implementation')
        again = self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                          'implementation', purpose='implementation')
        self.assertEqual(again, first)
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.refuses('request_id already belongs to another payload',
                     self.service.ao_room_send, self.room, 'engineer', 'A different instruction',
                     'implementation', purpose='implementation')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_lost_acknowledgement_with_delivered_message_reconciles_without_replay(self):
        self.fake.lose_ack = True
        posts = len(self.fake.posts)
        result = self.send('implementation')
        self.assertEqual(result['state'], 'uncertain')
        self.assertIn('delivery_error', result)
        self.assertEqual(len(self.fake.posts), posts + 1)  # fake records the turn before raising
        self.fake.lose_ack = False
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'completed')
        self.assertTrue(request['turn_id'])
        self.assertIn('engineering_record', request)
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_lost_acknowledgement_with_duplicate_identical_messages_stays_uncertain(self):
        self.fake.lose_ack = True
        posts = len(self.fake.posts)
        self.send('implementation')
        snapshot = self.fake.snapshots['engineer']
        original = snapshot['messages'][-1]
        snapshot['turns'].append({'id': 'turn-dup', 'providerTurnId': 'native-turn-dup',
                                  'state': 'completed'})
        snapshot['messages'].append({**original, 'id': 'user-turn-dup', 'turnId': 'turn-dup'})
        self.fake.lose_ack = False
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertRegex(request['reconciliation'], 'No unique exact sent message observed')
        again = self.service.ao_room_send(self.room, 'engineer', 'Perform the exact authorized purpose.',
                                          'implementation', purpose='implementation')
        self.assertEqual(again['state'], 'uncertain')
        self.refuses('active or uncertain', self.send, 'correction')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_lost_acknowledgement_with_no_observed_message_stays_held(self):
        self.fake.lose_ack = True
        posts = len(self.fake.posts)
        self.send('implementation')
        snapshot = self.fake.snapshots['engineer']
        snapshot['turns'].pop()
        snapshot['messages'].pop()  # AO never persisted the accepted message
        self.fake.lose_ack = False
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'uncertain')
        self.assertRegex(request['reconciliation'], 'No unique exact sent message observed')
        self.refuses('active or uncertain', self.send, 'correction')
        self.assertEqual(len(self.fake.posts), posts + 1)


class ExternalTurnTests(AgreedRoomFixture):
    """F: history the controller did not send. GAP-AR-1 pins the observed safe refusal of an
    AO-automation turn (origin=automation); the pending owner lane is named in OPEN_GAPS."""

    def _automation_turn(self):
        snapshot = self.fake.snapshots['engineer']
        sequence = len(snapshot['messages']) + 1
        snapshot['turns'].append({'id': 'automation-1', 'state': 'completed',
                                  'providerTurnId': 'native-automation-1', 'stopReason': 'end_turn'})
        snapshot['messages'].append({'id': 'user-automation-1', 'role': 'user', 'turnId': 'automation-1',
                                     'sequence': sequence, 'origin': 'automation',
                                     'text': 'CI failed on Python 3.12; fix the test portability.'})
        snapshot['messages'].append({'id': 'answer-automation-1', 'role': 'assistant',
                                     'turnId': 'automation-1', 'sequence': sequence + 1,
                                     'text': 'Fixed.'})

    def test_automation_origin_turn_is_refused_safely_but_has_no_reconciliation_lane(self):
        """Demonstrates GAP-AR-1: the baseline refuses safely before dispatch; no supported
        audit/reconciliation lane exists; the pending owner fix is named in OPEN_GAPS."""
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        self._automation_turn()
        before = self.effects()
        posts = len(self.fake.posts)
        self.refuses('Cannot attribute semantic outcome to an unchanged completed native result',
                     self.service.ao_room_outcome_audit, self.room)
        self.refuses('Cannot attribute semantic outcome to an unchanged completed native result',
                     self.service.ao_room_send, self.room, 'engineer', 'Continue.', 'fix-ci',
                     purpose='correction')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('fix-ci', self.state()['requests'])
        self.assert_no_effects(before)
        status = self.service.ao_room_status(self.room)
        self.assertTrue(status['checkpoint'])
        self.assertIsNone(status['latest_acceptance'])

    def test_unowned_recovered_turn_requires_positive_proof(self):
        self.implement()
        self.fake.snapshots['engineer']['turns'].append(
            {'id': 'unowned-1', 'state': 'recovered', 'providerTurnId': 'foreign-native-1'})
        posts = len(self.fake.posts)
        self.refuses('unverified recovered context', self.send, 'correction')
        self.assertEqual(len(self.fake.posts), posts)


class ChangedCandidateTests(AgreedRoomFixture):
    """G: candidate/receipt/spec mutation binds to exact recorded bytes and is refused downstream."""

    def test_candidate_changed_before_first_observation_is_captured_silently(self):
        self.send('implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        (self.repo / 'feature.txt').write_text('implemented-more\n')  # changed before first observation
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'completed')
        # Observed baseline: the candidate snapshot is taken at first observation, so the second write
        # is already inside it; capture succeeds and binds the changed bytes rather than refusing.
        self.assertIn('engineering_record', request)
        self.assertNotIn('engineering_error', request)
        self.assertTrue(request['completion_capture_attempted'])
        self.assertEqual(len(list((self.directory() / 'completion-candidates').iterdir())), 1)
        result = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(result['passed'])  # the recorded gate then fails against the changed bytes
        self.refuses('Create a passed verification checkpoint first', self.send, 'acceptance_review')

    def test_candidate_changed_after_verification_is_refused_at_acceptance(self):
        self.implement()
        verified = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertTrue(verified['passed'])
        (self.repo / 'extra.txt').write_text('new\n')
        posts = len(self.fake.posts)
        self.refuses('Candidate changed after verification', self.send, 'acceptance_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.state()['acceptances'], [])

    def test_modified_receipt_or_evidence_is_refused_at_acceptance(self):
        self.implement()
        self.review()
        request = self.state()['requests']['acceptance_review']
        receipt_path = self.directory() / request['receipt']
        original = receipt_path.read_bytes()
        receipt = json.loads(original)
        receipt['messages'][-1]['text'] += ' '  # valid JSON, one changed byte of evidence
        receipt_path.write_text(json.dumps(receipt) + '\n')
        # Observed baseline: the exact-byte refusal surfaces through the receipt-admission check.
        self.refuses('receipt was modified', self.service.ao_room_accept, self.room, 'acceptance_review')
        receipt_path.write_bytes(original)
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.assertEqual(len(self.state()['acceptances']), 1)

    def test_spec_change_after_agreement_invalidates_handoff_and_review(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        self.service.ao_room_spec_put(self.room, 2, 'Revised exact contract.', self.gates,
                                      'Astra approves this scope')
        self.refuses('Create a passed verification checkpoint first', self.send, 'acceptance_review')
        self.refuses('current exact spec requires completed Fable agreement',
                     self.service.ao_room_accept, self.room, 'acceptance_review')
        self.refuses('current exact spec requires completed Fable agreement',
                     self.service.ao_room_handoff, self.room, str(self.repo))

    def test_prefix_or_wrong_identity_in_reviewer_verdict_is_refused(self):
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        checkpoint = ao.read(self.directory() / self.state()['checkpoint'])
        for key, wrong in (('acceptance_review', checkpoint['candidate_sha256'][:16]),
                           ('acceptance_review-2', 'f' * 64)):
            with self.subTest(request_id=key):
                self.send('acceptance_review', key)
                request = self.state()['requests'][key]
                verdict = {**request['review'], 'candidate_sha256': wrong,
                           'decision': 'approved', 'review': 'Approved with a corrupted identity.'}
                self.fake.finish('reviewer', json.dumps(verdict))
                self.service.ao_room_sync(self.room)
                self.refuses('did not approve the exact identities',
                             self.service.ao_room_accept, self.room, key)
                self.assertEqual(self.state()['acceptances'], [])

    def test_changed_completed_at_after_capture_is_admitted_and_mints_a_new_semantic_record(self):
        """Demonstrates GAP-TT-1: the baseline admits a changed completedAt after capture and mints a
        new semantic-outcome record; this pins the admission, it does not claim the contract is met."""
        self.implement()
        request = self.state()['requests']['implementation']
        outcome_sha256 = request['semantic_outcome_sha256']
        self.fake.snapshots['engineer']['turns'][-1]['completedAt'] = '1999-01-01T00:00:00Z'
        posts = len(self.fake.posts)
        # Observed baseline pins this as an admission: the strict observed-vs-receipt check binds turn
        # identity and messages but not the mutable completedAt field, so the correction dispatches and
        # a fresh semantic-outcome record (new digest) replaces the pointer.
        result = self.send('correction')
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertNotEqual(self.state()['requests']['implementation']['semantic_outcome_sha256'],
                            outcome_sha256)


class LongHistoryTests(AgreedRoomFixture):
    """H: a long retained request history keeps status and sync truthful."""

    def test_many_retained_requests_keep_status_and_sync_truthful(self):
        self.implement()
        for number in range(21):
            self.send('correction', 'correction-' + str(number))
            self.fake.finish('engineer', json.dumps(self.report()))
            self.service.ao_room_sync(self.room)
        state = self.state()
        self.assertEqual(len(state['requests']), 23)
        self.assertTrue(all(request['state'] == 'completed' for request in state['requests'].values()))
        full = self.service.ao_room_status(self.room, view='full')
        self.assertTrue(full['requests_truncated'])
        self.assertEqual(len(full['requests']), 20)
        compact = self.mcp('ao_room_status', view='compact')
        self.assertEqual(compact['request_counts'], {'completed': 23})
        self.assertIn('engineering_record', state['requests']['correction-20'])
        result = self.service.ao_room_sync(self.room, wait_seconds=0)
        self.assertNotIn('wait', result)
        self.assertTrue(all(request['state'] == 'completed'
                            for request in self.state()['requests'].values()))
        self.service.ao_room_timing(self.room)
        self.service.ao_room_progress(self.room)

    def test_history_truncated_snapshot_refuses_dispatch_without_state_change(self):
        self.fake.snapshots['engineer']['history_truncated'] = True
        before = self.effects()
        posts = len(self.fake.posts)
        self.refuses('Conversation history exceeds the bounded accounting window',
                     self.send, 'implementation')
        self.assertEqual(len(self.fake.posts), posts)
        self.assert_no_effects(before)


class GapRegistryTests(unittest.TestCase):
    """I: the open-gap registry and the coverage-map document stay consistent."""

    def test_open_gaps_registry_is_complete_and_resolvable(self):
        required = {'id', 'family', 'contract', 'baseline', 'owner', 'demonstrated_by'}
        for gap in OPEN_GAPS:
            with self.subTest(gap=gap['id']):
                self.assertEqual(set(gap), required)
                for key in ('id', 'family', 'contract', 'baseline', 'owner'):
                    self.assertIsInstance(gap[key], str)
                    self.assertTrue(gap[key].strip())
                demonstrated = gap['demonstrated_by']
                if demonstrated is None:
                    continue
                class_name, _, method = demonstrated.rpartition('.')
                self.assertTrue(callable(getattr(globals()[class_name], method)))

    def test_every_gap_id_appears_in_the_coverage_map(self):
        path = Path(__file__).resolve().parent / 'docs' / 'reference' / 'lifecycle-contract.md'
        self.assertTrue(path.exists())
        text = path.read_text()
        for gap in OPEN_GAPS:
            self.assertIn(gap['id'], text)
