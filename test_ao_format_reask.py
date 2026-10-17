"""Budget-neutral per-source format re-ask contracts; synthetic rooms and fake backends only."""
import copy
import json
from datetime import datetime, timezone
from unittest.mock import patch

import ao_project_room as ao
import ao_spec_review_budget as budget
import ao_spec_verdict_recovery as recovery
import ao_workflow
import project_room
from test_ao_spec_verdict_recovery import RecoveryFixture


class ReaskFixture(RecoveryFixture):
    """A completed prose-only review at two spec-review dispatches: accept R1,
    handoff + implement + accept the candidate, register R2, then one prose review."""

    def history_before_source(self):
        self.agree(key='review1')
        self.implement()
        self.review()
        self.spec(revision=2)

    def grant(self, **changes):
        value = self.audit()
        return self.service.ao_room_spec_verdict_recovery_grant(self.room, value['audit_sha256'], **changes)

    def audit_for(self, source, recovery_request_id):
        return self.service.ao_room_spec_verdict_recovery_audit(
            self.room, source, recovery_request_id,
            self.native_outcome_source['database'], self.NATIVE)

    def prose_source(self, key, text):
        self.send('spec_review', key)
        self.fake.finish('engineer', text)
        self.service.ao_room_sync(self.room)

    def verdict_for(self, source, **changes):
        spec = self.service.spec(self.directory(), self.state())
        request = self.state()['requests'][source]
        raw = ao_workflow.raw_final_text(self.directory(), request)
        return {'interpretation': 'Preserve the exact stated behavior and gates, with no additional requirements.',
                'decision': 'accept', 'findings': [], 'meaning_unchanged': True,
                'spec_revision': spec['revision'], 'spec_sha256': spec['sha256'],
                'source_request_id': source, 'source_receipt_sha256': request['receipt_sha256'],
                'source_final_text_sha256': ao.digest(raw.encode()), **changes}

    def close(self, source='review3', recovery_request_id='format1', **changes):
        request = self.state()['requests'][recovery_request_id]
        raw = ao_workflow.raw_final_text(self.directory(), request)
        args = {'room_id': self.room, 'recovery_request_id': recovery_request_id,
                'receipt_sha256': request['receipt_sha256'], 'final_text_sha256': ao.digest(raw.encode()),
                'astra_review': 'I read both complete responses and cannot affirm the same verdict; the lane closes without agreement.',
                **changes}
        return self.service.ao_room_spec_verdict_recovery_close(**args)

    def reask(self, source, recovery_request_id, **verdict_changes):
        """Full positive chain: audit -> no-authorization grant -> fixed send -> strict JSON -> commit."""
        audited = self.audit_for(source, recovery_request_id)
        granted = self.service.ao_room_spec_verdict_recovery_grant(self.room, audited['audit_sha256'])
        self.service.ao_room_send(**granted['send_args'])
        self.note_native_turn(recovery_request_id)
        self.fake.finish('engineer', json.dumps(self.verdict_for(source, **verdict_changes)))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests'][recovery_request_id]
        raw = ao_workflow.raw_final_text(self.directory(), request)
        return self.service.ao_room_spec_verdict_recovery_commit(
            self.room, recovery_request_id, request['receipt_sha256'], ao.digest(raw.encode()),
            'I compared both complete native responses. The structured response preserves the original '
            'exact-spec agreement with no new findings or scope.', True)


class FormatReaskTests(ReaskFixture):
    def test_two_dispatch_success_counts_no_budget_and_recovers_agreement(self):
        posts = len(self.fake.posts)
        granted = self.grant()
        self.assertEqual((granted['mode'], granted['consumed'], granted['model_dispatch']),
                         ('format_reask', False, False))
        self.assertEqual(granted['send_args']['message'], recovery.MESSAGE)
        self.assertEqual(len(self.fake.posts), posts)
        result = self.service.ao_room_send(**granted['send_args'])
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.note_native_turn('format1')
        self.fake.finish('engineer', json.dumps(self.verdict()))
        self.service.ao_room_sync(self.room)
        self.assertTrue(self.commit()['agreed'])
        status = self.service.ao_room_status(self.room)
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['agreement']['spec_verdict_recovery_sha256'],
                         status['spec_verdict_recovery']['agreement_sha256'])
        # The re-ask turn consumed no spec-review budget: still two dispatches, zero debate.
        self.assertEqual(status['spec_review_attempts'], 2)
        self.assertEqual(status['spec_review_budget']['debate_attempts'], 0)
        self.assertEqual(status['spec_review_budget']['dispatches'], 2)
        source = self.state()['requests']['review3']
        self.assertEqual(budget.counted_as(self.directory(), self.state(), source), 'accepting')
        self.assertEqual(status['spec_verdict_recovery']['status'], 'agreed')
        self.assertEqual(status['spec_verdict_recovery']['used_sources'], ['review3'])
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_second_audit_for_the_same_source_refuses_without_post(self):
        self.finish()
        self.commit()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'already had its one format re-ask'):
            self.audit(recovery_request_id='format2')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('format2', self.state()['requests'])

    def test_a_later_prose_review_opens_its_own_chain_and_rotates_history(self):
        self.finish()
        self.commit()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.spec(revision=3)
        self.prose_source('review4', 'I accept the exact current specification. No change is required.')
        first_pointer = copy.deepcopy(self.state()['spec_verdict_recovery'])
        result = self.reask('review4', 'format2')
        self.assertTrue(result['agreed'])
        status = self.service.ao_room_status(self.room)
        state = self.state()
        self.assertEqual(state['spec_verdict_recovery_history'], [first_pointer])
        self.assertEqual(state['spec_verdict_recovery']['source_request_id'], 'review4')
        self.assertEqual(status['spec_verdict_recovery']['history'],
                         [{'source_request_id': 'review3', 'recovery_request_id': 'format1', 'status': 'agreed'}])
        self.assertEqual(status['spec_verdict_recovery']['used_sources'], ['review3', 'review4'])
        self.assertEqual(recovery.agreed_sources(self.directory(), state), {'review3', 'review4'})
        # The rotated-out source keeps its accepting credit through retained history.
        source = state['requests']['review3']
        self.assertEqual(budget.counted_as(self.directory(), state, source), 'accepting')
        counted = {row['request_id']: row['counted_as']
                   for row in status['spec_review_budget']['reviews']}
        self.assertEqual(counted, {'review1': 'accepting', 'review3': 'accepting', 'review4': 'accepting'})
        self.assertEqual(status['spec_review_budget']['debate_attempts'], 0)
        self.assertEqual(status['spec_review_attempts'], 3)
        self.assertTrue(status['agreement']['agreed'])

    def test_an_embedded_json_verdict_is_not_a_prose_only_source(self):
        self.finish()
        self.commit()
        self.spec(revision=3)
        self.prose_source('review4', 'Accepted. ' + json.dumps({'decision': 'accept'}))
        with self.assertRaisesRegex(ao.RoomError, 'prose-only'):
            self.audit_for('review4', 'format2')
        self.assertNotIn('format2', self.state()['requests'])

    def test_provider_quota_failure_settles_keeps_debate_and_blocks_a_second_reask(self):
        grant = self.grant()
        self.service.ao_room_send(**grant['send_args'])
        request = self.state()['requests']['format1']
        stamp = lambda offset: min(datetime.fromtimestamp(request['created_at'] + offset, timezone.utc),
                                   datetime.now(timezone.utc)).isoformat()
        self.native_events.extend([
            {'type': 'user', 'uuid': 'format1-caller', 'sessionId': self.NATIVE,
             'timestamp': stamp(.001), 'cwd': str(self.repo), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': 'format1-error', 'sessionId': self.NATIVE,
             'timestamp': stamp(.002), 'cwd': str(self.repo), 'isSidechain': False,
             'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
             'message': {'role': 'assistant', 'model': '<synthetic>',
                         'content': [{'type': 'text', 'text': 'Synthetic native session limit'}]}}])
        self.transcript.write_text(''.join(json.dumps(row) + chr(10) for row in self.native_events))
        self.native_requests.add('format1')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['format1']['state'], 'uncertain')
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertTrue(audit['resume_eligible'])
        self.service.ao_room_outcome_resume(self.room, 'format1', audit['outcome_sha256'], 'review4',
            'Actual standing human quota continuation authority; synthetic reset checked', 'Continue once')
        self.assertEqual(self.state()['requests']['format1']['state'], 'settled_failure')
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery']['status'], 'settled_failure')
        # No budget change: the source stays debate and only dispatches moved.
        self.assertEqual(status['spec_review_attempts'], 2)
        source = self.state()['requests']['review3']
        self.assertEqual(budget.counted_as(self.directory(), self.state(), source), 'debate')
        # Ordinary review admission is untouched; a second re-ask for the source refuses.
        self.spec(revision=3)
        self.assertEqual(self.send('spec_review', 'review4')['state'], 'submitted')
        with self.assertRaisesRegex(ao.RoomError, 'already had its one format re-ask'):
            self.audit(recovery_request_id='format2')

    def test_negative_closure_keeps_debate_and_blocks_only_that_source(self):
        self.finish(decision='changes_required', findings=['Required new behavior'], meaning_unchanged=False)
        with self.assertRaisesRegex(ao.RoomError, 'changed meaning'):
            self.commit()
        self.assertTrue(self.close()['closed'])
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery']['status'], 'closed')
        source = self.state()['requests']['review3']
        self.assertEqual(budget.counted_as(self.directory(), self.state(), source), 'debate')
        with self.assertRaisesRegex(ao.RoomError, 'already had its one format re-ask'):
            self.audit(recovery_request_id='format2')
        self.spec(revision=3)
        self.prose_source('review4', 'I accept the exact current specification. No change is required.')
        self.reask('review4', 'format2')
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery']['history'],
                         [{'source_request_id': 'review3', 'recovery_request_id': 'format1', 'status': 'closed'}])
        self.assertEqual(status['spec_verdict_recovery']['status'], 'agreed')
        self.assertTrue(status['agreement']['agreed'])
        source = self.state()['requests']['review3']
        self.assertEqual(budget.counted_as(self.directory(), self.state(), source), 'debate')

    def test_legacy_flat_chain_layout_reads_identically(self):
        self.finish()
        self.commit()
        before = {'status': self.service.ao_room_status(self.room)['spec_verdict_recovery'],
                  'agreement': self.service.ao_room_status(self.room)['agreement'],
                  'sources': recovery.agreed_sources(self.directory(), self.state())}
        state = self.state()
        chain_dir = self.directory() / 'spec-verdict-recovery' / 'chains' / 'review3'
        for name in recovery.CHAIN_FILES:
            path = chain_dir / (name + '.json')
            if path.exists():
                path.rename(self.directory() / 'spec-verdict-recovery' / path.name)
        chain_dir.rmdir()
        (self.directory() / 'spec-verdict-recovery' / 'chains').rmdir()
        pointer = state['spec_verdict_recovery']
        del pointer['source_request_id']
        del pointer['chain']
        ao.atomic(self.directory() / 'state.json', state)
        state = self.state()
        self.assertEqual(recovery.journal(self.directory(), state)['status'], 'agreed')
        self.assertEqual(recovery.agreed_sources(self.directory(), state), before['sources'])
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery'], before['status'])
        self.assertEqual(status['agreement'], before['agreement'])
        self.assertTrue(status['agreement']['agreed'])
        # A legacy-shaped pointer over the new chains/ layout is damage, not a chain.
        chain_dir = self.directory() / 'spec-verdict-recovery' / 'chains' / 'review3'
        chain_dir.mkdir(parents=True)
        for name in recovery.CHAIN_FILES:
            path = self.directory() / 'spec-verdict-recovery' / (name + '.json')
            if path.exists():
                path.rename(chain_dir / path.name)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery']['status'], 'damaged')
        self.assertFalse(status['agreement']['agreed'])
        with self.assertRaisesRegex(ao.RoomError, 'missing, changed or unclaimed'):
            recovery.journal(self.directory(), self.state())

    def _closed_first_chain_after_rotation(self):
        """format1 finished with a transient capture error, closed, and rotated by a
        granted format2 chain on a later prose source. Returns (granted2, pinned request)."""
        self.dispatch()
        self.fake.finish('engineer', json.dumps(self.verdict()))
        original = recovery._source
        def flaky(directory, request):
            if request is not None and request.get('purpose') == recovery.PURPOSE:
                raise ao.RoomError('injected transient capture failure')
            return original(directory, request)
        with patch.object(recovery, '_source', flaky):
            self.service.ao_room_sync(self.room)
        request = self.state()['requests']['format1']
        self.assertEqual(request['state'], 'completed')
        self.assertEqual(request['spec_verdict_recovery_error'], 'injected transient capture failure')
        self.assertNotIn('spec_verdict_recovery_completion_sha256', request)
        self.assertTrue(self.close()['closed'])
        self.spec(revision=3)
        self.prose_source('review4', 'I accept the exact current specification. No change is required.')
        audited = self.audit_for('review4', 'format2')
        granted2 = self.service.ao_room_spec_verdict_recovery_grant(self.room, audited['audit_sha256'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_verdict_recovery']['status'], 'granted')
        return granted2, copy.deepcopy(self.state()['requests']['format1'])

    def test_capture_retry_resolves_a_finished_chains_own_request_after_rotation(self):
        granted2, pinned = self._closed_first_chain_after_rotation()
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['format1'], pinned)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_verdict_recovery']['status'], 'granted')
        self.assertEqual(status['spec_verdict_recovery']['history'],
                         [{'source_request_id': 'review3', 'recovery_request_id': 'format1', 'status': 'closed'}])
        self.assertEqual(self.service.ao_room_send(**granted2['send_args'])['state'], 'submitted')

    def test_capture_retry_resolves_a_finished_chain_across_controller_restart(self):
        granted2, pinned = self._closed_first_chain_after_rotation()
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['format1'], pinned)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_verdict_recovery']['status'], 'granted')
        self.assertEqual(self.service.ao_room_send(**granted2['send_args'])['state'], 'submitted')

    def _format2_send_and_commit(self, granted2):
        self.service.ao_room_send(**granted2['send_args'])
        self.note_native_turn('format2')
        self.fake.finish('engineer', json.dumps(self.verdict_for('review4')))
        self.service.ao_room_sync(self.room)
        request2 = self.state()['requests']['format2']
        raw2 = ao_workflow.raw_final_text(self.directory(), request2)
        return self.service.ao_room_spec_verdict_recovery_commit(
            self.room, 'format2', request2['receipt_sha256'], ao.digest(raw2.encode()),
            'I compared both complete native responses. The structured response preserves the original '
            'exact-spec agreement with no new findings or scope.', True)

    def test_finished_chain_grant_commit_and_close_retries_reconcile_on_exact_inputs(self):
        audit1 = self.audit()
        granted1 = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit1['audit_sha256'])
        self.service.ao_room_send(**granted1['send_args'])
        self.note_native_turn('format1')
        self.fake.finish('engineer', json.dumps(self.verdict()))
        self.service.ao_room_sync(self.room)
        committed = self.commit()
        self.assertTrue(committed['agreed'])
        self.spec(revision=3)
        self.prose_source('review4', 'I accept the exact current specification. No change is required.')
        audit2 = self.audit_for('review4', 'format2')
        granted2 = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit2['audit_sha256'])

        posts = len(self.fake.posts)
        # While format2's chain is still pending, exact retries of format1's finished
        # chain authenticate against retained history; changed inputs refuse; no POSTs.
        retry = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit1['audit_sha256'])
        self.assertEqual(retry['grant_sha256'], granted1['grant_sha256'])
        self.assertEqual((retry['request_id'], retry['consumed'], retry['model_dispatch']), ('format1', True, False))
        with self.assertRaisesRegex(ao.RoomError, 'already belongs to different'):
            self.service.ao_room_spec_verdict_recovery_grant(
                self.room, audit1['audit_sha256'], 'Different authorization', 'different-reference')
        self.assertEqual(self.commit(), committed)
        with self.assertRaisesRegex(ao.RoomError, 'already has different inputs'):
            self.commit(astra_review='A changed comparison text')
        with self.assertRaisesRegex(ao.RoomError, 'already agreed; closure is impossible'):
            self.close()
        self.assertEqual(len(self.fake.posts), posts)

        self.assertTrue(self._format2_send_and_commit(granted2)['agreed'])
        posts = len(self.fake.posts)
        # After format2's chain finishes, the same retries still reconcile, never re-mint.
        self.assertEqual(self.service.ao_room_spec_verdict_recovery_grant(
            self.room, audit1['audit_sha256'])['grant_sha256'], granted1['grant_sha256'])
        self.assertEqual(self.commit(), committed)
        with self.assertRaisesRegex(ao.RoomError, 'already has different inputs'):
            self.commit(astra_review='A changed comparison text')
        with self.assertRaisesRegex(ao.RoomError, 'already agreed; closure is impossible'):
            self.close()
        self.assertEqual(len(self.fake.posts), posts)

    def test_finished_closed_chain_retries_reconcile_on_exact_inputs(self):
        audit1 = self.audit()
        granted1 = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit1['audit_sha256'])
        self.service.ao_room_send(**granted1['send_args'])
        self.note_native_turn('format1')
        self.fake.finish('engineer', json.dumps(self.verdict(
            decision='changes_required', findings=['Required new behavior'], meaning_unchanged=False)))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'changed meaning'):
            self.commit()
        closed = self.close()
        self.assertTrue(closed['closed'])
        self.spec(revision=3)
        self.prose_source('review4', 'I accept the exact current specification. No change is required.')
        audit2 = self.audit_for('review4', 'format2')
        granted2 = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit2['audit_sha256'])

        posts = len(self.fake.posts)
        # While format2's chain is pending, format1's closed chain reconciles retries.
        retry = self.service.ao_room_spec_verdict_recovery_grant(self.room, audit1['audit_sha256'])
        self.assertEqual((retry['grant_sha256'], retry['consumed']), (granted1['grant_sha256'], True))
        self.assertEqual(self.close(), closed)
        with self.assertRaisesRegex(ao.RoomError, 'already has different inputs'):
            self.close(astra_review='A changed comparison is a different closure')
        with self.assertRaisesRegex(ao.RoomError, 'closed without agreement'):
            self.commit()
        self.assertEqual(len(self.fake.posts), posts)

        self.assertTrue(self._format2_send_and_commit(granted2)['agreed'])
        posts = len(self.fake.posts)
        self.assertEqual(self.close(), closed)
        with self.assertRaisesRegex(ao.RoomError, 'already has different inputs'):
            self.close(astra_review='A changed comparison is a different closure')
        with self.assertRaisesRegex(ao.RoomError, 'closed without agreement'):
            self.commit()
        self.assertEqual(len(self.fake.posts), posts)

    def test_grant_authorization_shape_validation_and_version_records(self):
        audit = self.audit()
        for changes in ({'authorization': 'Only one field'},
                        {'authorization_reference': 'Only one field'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ao.RoomError, 'both authorization'):
                self.service.ao_room_spec_verdict_recovery_grant(self.room, audit['audit_sha256'], **changes)
        granted = self.service.ao_room_spec_verdict_recovery_grant(
            self.room, audit['audit_sha256'],
            'The human authorizes one format-only reissue of this exact completed verdict.',
            'synthetic-human-decision')
        self.assertEqual(granted['mode'], 'authorized')
        record = ao.read(self.chain_file('grant'))
        self.assertEqual(record['version'], 1)
        self.assertIn('authorization', record)
        # Replaying with a different record for the same source refuses; identical calls reconcile.
        with self.assertRaisesRegex(ao.RoomError, 'already belongs'):
            self.service.ao_room_spec_verdict_recovery_grant(self.room, audit['audit_sha256'])
        self.assertEqual(self.service.ao_room_spec_verdict_recovery_grant(
            self.room, audit['audit_sha256'],
            'The human authorizes one format-only reissue of this exact completed verdict.',
            'synthetic-human-decision'), granted)

    def test_grant_schema_requires_only_room_and_audit(self):
        entry = project_room.TOOL_SCHEMAS['ao_room_spec_verdict_recovery_grant'][1]
        self.assertEqual(entry['required'], ['room_id', 'audit_sha256'])
        for field in ('authorization', 'authorization_reference'):
            self.assertIn(field, entry['properties'])


if __name__ == '__main__':
    import unittest
    unittest.main()
