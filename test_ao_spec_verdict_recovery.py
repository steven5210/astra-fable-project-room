"""Offline real-Service verdict-format recovery contracts. No paid/provider calls."""
import copy
import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import ao_project_room as ao
import ao_spec_verdict_recovery as recovery
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture


class RecoveryFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()
        original = self.fake.request
        def raw(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original(method, path, payload)
        self.fake.request = raw
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.history_before_source()
        self.send('spec_review', 'review3')
        self.fake.finish('engineer', 'I accept the exact current specification. Preserve the stated behavior and gates; there are no additional requirements.')
        self.service.ao_room_sync(self.room)

    def history_before_source(self):
        self.agree(key='review1')
        self.agree(key='review2')

    def audit_args(self, **changes):
        return {'room_id': self.room, 'source_request_id': 'review3', 'recovery_request_id': 'format1',
                'native_owner_database': self.native_outcome_source['database'], 'native_session_id': self.NATIVE,
                **changes}

    def audit(self, **changes):
        return self.service.ao_room_spec_verdict_recovery_audit(**self.audit_args(**changes))

    def grant(self, **changes):
        value = self.audit()
        return self.service.ao_room_spec_verdict_recovery_grant(self.room, value['audit_sha256'],
            'Actual user approved exactly one same-session format-only request; original limits remain.',
            'Synthetic human answer to explicit one-use proposal', **changes)

    def dispatch(self):
        grant = self.grant()
        result = self.service.ao_room_send(**grant['send_args'])
        self.note_native_turn('format1')
        return result

    def verdict(self, **changes):
        spec = self.service.spec(self.directory(), self.state())
        request = self.state()['requests']['review3']
        raw = ao_workflow.raw_final_text(self.directory(), request)
        return {'interpretation': 'Preserve the exact stated behavior and gates, with no additional requirements.',
                'decision': 'accept', 'findings': [], 'meaning_unchanged': True,
                'spec_revision': spec['revision'], 'spec_sha256': spec['sha256'],
                'source_request_id': 'review3', 'source_receipt_sha256': request['receipt_sha256'],
                'source_final_text_sha256': ao.digest(raw.encode()), **changes}

    def finish(self, text=None, **changes):
        self.dispatch()
        self.fake.finish('engineer', text if text is not None else json.dumps(self.verdict(**changes)))
        return self.service.ao_room_sync(self.room)

    def commit_args(self, **changes):
        request = self.state()['requests']['format1']
        raw = ao_workflow.raw_final_text(self.directory(), request)
        return {'room_id': self.room, 'recovery_request_id': 'format1', 'receipt_sha256': request['receipt_sha256'],
                'final_text_sha256': ao.digest(raw.encode()),
                'astra_review': 'I independently read both complete responses: same interpretation, decision, requirements and absence of blockers.',
                'confirm_same_verdict': True, **changes}

    def commit(self, **changes):
        return self.service.ao_room_spec_verdict_recovery_commit(**self.commit_args(**changes))

    def files(self):
        return {str(p.relative_to(self.directory())): p.read_bytes() for p in self.directory().rglob('*') if p.is_file()}


class SpecVerdictRecoveryTests(RecoveryFixture):
    def test_real_entrypoint_preserves_three_reviews_and_source_receipt_then_handoff_and_implementation(self):
        original = copy.deepcopy(self.state()['requests'])
        receipts = {key: (self.directory() / row['receipt']).read_bytes() for key, row in original.items()}
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.finish()
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        result = self.commit()
        self.assertTrue(result['agreed'])
        status = self.service.ao_room_status(self.room)
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_review_attempts'], 3)
        self.assertEqual(status['spec_verdict_recovery_attempts'], 1)
        self.assertEqual(status['acceptance_review_attempts'], 0)
        self.assertEqual(len(self.fake.posts), 4)
        after = self.state()
        for key, row in original.items():
            self.assertEqual(after['requests'][key], row)
            self.assertEqual((self.directory() / row['receipt']).read_bytes(), receipts[key])
        self.service.ao_room_handoff(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'Three Fable'):
            self.send('spec_review', 'review4')
        self.send('implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def test_audit_and_grant_do_not_dispatch_or_change_source(self):
        source = copy.deepcopy(self.state()['requests'])
        grant = self.grant()
        self.assertFalse(grant['model_dispatch'])
        self.assertFalse(grant['consumed'])
        self.assertEqual(source, self.state()['requests'])
        self.assertEqual(len(self.fake.posts), 3)
        record = ao.read(self.directory() / recovery._path('grant'))
        self.assertIn('Actual user', record['authorization'])
        self.assertIn('Synthetic human', record['authorization_reference'])

    def test_exact_same_grant_and_commit_are_idempotent_but_changed_authority_refuses(self):
        grant = self.grant()
        record = ao.read(self.directory() / recovery._path('grant'))
        again = self.service.ao_room_spec_verdict_recovery_grant(self.room, record['audit_sha256'],
                         record['authorization'], record['authorization_reference'])
        self.assertEqual(grant, again)
        with self.assertRaisesRegex(ao.RoomError, 'already belongs'):
            self.service.ao_room_spec_verdict_recovery_grant(self.room, record['audit_sha256'], 'Different human text', record['authorization_reference'])
        self.service.ao_room_send(**grant['send_args']); self.note_native_turn('format1')
        self.fake.finish('engineer', json.dumps(self.verdict())); self.service.ao_room_sync(self.room)
        self.assertEqual(self.commit(), self.commit())
        with self.assertRaisesRegex(ao.RoomError, 'different inputs'):
            self.commit(astra_review='Different judgment')

    def test_pending_grant_blocks_other_send_spec_and_handoff(self):
        self.grant(); posts = len(self.fake.posts)
        for purpose in ('spec_review', 'implementation', 'correction', 'acceptance_review'):
            with self.subTest(purpose=purpose), self.assertRaises(ao.RoomError):
                self.send(purpose, 'escape-' + purpose)
        with self.assertRaisesRegex(ao.RoomError, 'pending'):
            self.spec(revision=2)
        with self.assertRaisesRegex(ao.RoomError, 'pending'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(len(self.fake.posts), posts)

    def test_format_request_requires_exact_fixed_message_and_grant(self):
        with self.assertRaises(ao.RoomError):
            self.send(recovery.PURPOSE, 'format1')
        grant = self.grant()
        for changes in ({'message': 'Continue implementation.'}, {'request_id': 'format2'}, {'role': 'reviewer'}):
            with self.subTest(changes=changes), self.assertRaises(ao.RoomError):
                self.service.ao_room_send(**{**grant['send_args'], **changes})
        self.assertEqual(len(self.fake.posts), 3)

    def test_source_must_be_latest_exact_current_review(self):
        with self.assertRaisesRegex(ao.RoomError, 'latest engineer'):
            self.audit(source_request_id='review2')
        self.spec(revision=2)
        with self.assertRaisesRegex(ao.RoomError, 'exact current'):
            self.audit()

    def test_candidate_drift_after_audit_refuses_grant(self):
        audit = self.audit(); (self.repo / 'feature.txt').write_text('changed\n')
        with self.assertRaisesRegex(ao.RoomError, 'stale'):
            self.service.ao_room_spec_verdict_recovery_grant(self.room, audit['audit_sha256'], 'Authorized', 'human')
        self.assertNotIn(recovery.KEY, self.state())

    def test_candidate_drift_after_grant_refuses_before_intent_or_post(self):
        grant = self.grant(); (self.repo / 'feature.txt').write_text('changed\n')
        with self.assertRaisesRegex(ao.RoomError, 'candidate changed'):
            self.service.ao_room_send(**grant['send_args'])
        self.assertNotIn('format1', self.state()['requests'])
        self.assertFalse((self.directory() / recovery._path('intent')).exists())

    def test_candidate_drift_observed_at_completion_cannot_be_restored_for_commit(self):
        self.dispatch(); (self.repo / 'feature.txt').write_text('changed\n')
        self.fake.finish('engineer', json.dumps(self.verdict())); self.service.ao_room_sync(self.room)
        (self.repo / 'feature.txt').write_text('start\n')
        with self.assertRaisesRegex(ao.RoomError, 'candidate.*changed'):
            self.commit()

    def test_operator_cannot_omit_explicit_semantic_comparison(self):
        self.finish()
        with self.assertRaisesRegex(ao.RoomError, 'compare both complete'):
            self.commit(confirm_same_verdict=False)
        with self.assertRaises(ao.RoomError):
            self.commit(astra_review='')
        with self.assertRaisesRegex(ao.RoomError, 'exact native'):
            self.commit(final_text_sha256='0' * 64)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])

    def test_rejection_or_additional_findings_stays_blocking(self):
        self.finish(decision='changes_required', findings=['Required new behavior'], meaning_unchanged=False)
        with self.assertRaisesRegex(ao.RoomError, 'changed meaning'):
            self.commit()
        with self.assertRaises(ao.RoomError):
            self.send('correction', 'cannot-escape')
        with self.assertRaises(ao.RoomError):
            self.audit(recovery_request_id='another-format')
        self.assertEqual(len(self.fake.posts), 4)

    def test_malformed_paid_result_cannot_use_ordinary_normalization(self):
        self.finish(text='Review completed.\n' + json.dumps(self.verdict()))
        with self.assertRaisesRegex(ao.RoomError, 'strict native JSON'):
            self.commit()
        args = self.commit_args(); raw = ao_workflow.raw_final_text(self.directory(), self.state()['requests']['format1'])
        start = raw.find('{'); _, end = json.JSONDecoder().raw_decode(raw, start)
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_response_normalize(self.room, 'format1', args['receipt_sha256'],
                args['final_text_sha256'], start, end, 'Envelope adds no verdict', True)

    def test_preserved_request_deletion_blocks_downstream_agreement(self):
        self.finish(); self.commit()
        state = self.state(); del state['requests']['review1']; ao.atomic(self.directory() / 'state.json', state)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_handoff(self.room, str(self.repo))

    def test_source_and_recovery_receipt_symlink_refuse_downstream(self):
        self.finish(); self.commit()
        for key in ('review3', 'format1'):
            state = self.state(); path = self.directory() / state['requests'][key]['receipt']
            raw = path.read_bytes(); external = self.root / (key + '-copy.json'); external.write_bytes(raw)
            path.unlink(); path.symlink_to(external)
            self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
            path.unlink(); path.write_bytes(raw)

    def test_deleted_consumption_cannot_be_reauthorized_or_replayed(self):
        self.dispatch(); path = self.directory() / recovery._path('intent'); path.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'missing, changed or unclaimed'):
            self.service.ao_room_send(self.room, 'engineer', recovery.MESSAGE, 'format1', recovery.PURPOSE)
        with self.assertRaises(ao.RoomError):
            self.audit()
        self.assertEqual(len(self.fake.posts), 4)

    def test_conflicting_model_or_owner_refuses_audit(self):
        self.fake.snapshots['engineer']['settings']['model'] = 'other-model'
        with self.assertRaisesRegex(ao.RoomError, 'model/effort'):
            self.audit()
        self.assertNotIn(recovery.KEY, self.state())

    def test_same_id_send_returns_status_without_second_post_after_lost_ack(self):
        grant = self.grant(); original = self.fake.request
        def uncertain(method, path, payload=None):
            result = original(method, path, payload)
            if method == 'POST':
                raise OSError('synthetic lost acknowledgement')
            return result
        self.fake.request = uncertain
        first = self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(first['state'], 'uncertain')
        again = self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(first, again)
        self.assertEqual(len(self.fake.posts), 4)
        self.fake.request = original; self.note_native_turn('format1')
        self.fake.finish('engineer', json.dumps(self.verdict())); self.service.ao_room_sync(self.room)
        self.assertTrue(self.commit()['agreed'])

    def test_format_dispatch_enforces_root_readiness_before_consumption(self):
        import ao_engineering_model as model
        grant = self.grant()
        with patch.object(model, 'required_minimum', return_value='99.0.0'):
            with self.assertRaises(ao.RoomError):
                self.service.ao_room_send(**grant['send_args'])
        self.assertNotIn('format1', self.state()['requests'])
        self.assertFalse((self.directory() / recovery._path('intent')).exists())
        self.assertEqual(len(self.fake.posts), 3)

    def test_changed_frozen_source_model_cannot_receive_format_request(self):
        import ao_engineering_model as model
        original = model.freeze_request
        def changed(directory, state):
            return {**original(directory, state), "expected_model": "claude-fable-5-2"}
        with patch.object(model, 'freeze_request', side_effect=changed):
            with self.assertRaisesRegex(ao.RoomError, "source request's exact model"):
                self.audit()
        self.assertEqual(len(self.fake.posts), 3)

    def test_working_or_unknown_native_owner_refuses_audit(self):
        for activity in ('working', None, 'unknown'):
            with self.subTest(activity=activity):
                self.owner['activity_state'] = activity
                with self.assertRaisesRegex(ao.RoomError, 'positively idle'):
                    self.audit()
        self.assertEqual(len(self.fake.posts), 3)

    def test_other_owner_database_cannot_replace_registered_source(self):
        with self.assertRaisesRegex(ao.RoomError, 'exact registered'):
            self.audit(native_owner_database=str(self.root / 'another-owner.db'))

    def test_original_native_source_text_drift_after_dispatch_refuses_commit(self):
        self.finish()
        rows = self.fake.snapshots['engineer']['messages']
        source_turn = self.state()['requests']['review3']['turn_id']
        target = next(row for row in rows if row.get('turnId') == source_turn and row.get('role') == 'assistant')
        target['text'] = 'Changed original interpretation'
        with self.assertRaisesRegex(ao.RoomError, 'original native history'):
            self.commit()

    def test_schemas_expose_only_named_recovery_and_dispatch_purpose(self):
        for name in ('audit', 'grant', 'commit'):
            self.assertIn('ao_room_spec_verdict_recovery_' + name, project_room.TOOL_SCHEMAS)
        self.assertIn(recovery.PURPOSE, project_room.TOOL_SCHEMAS['ao_room_send'][1]['properties']['purpose']['enum'])


class PriorHandoffRecoveryTests(RecoveryFixture):
    def history_before_source(self):
        self.agree(key='review1')
        self.agree(key='review2')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.implement(outcome='changes_required', implementation_complete=False, remaining_gaps=['Finish R2 follow-up'])
        subprocess.run(['git', '-C', str(self.repo), 'add', 'feature.txt'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', 'Preserved partial R1 implementation'], check=True, capture_output=True)
        self.service.ao_room_spec_put(self.room, 2, 'Preserve the implemented behavior and finish the exact new R2 follow-up.',
                                      self.gates, 'Astra approves R2 under the actual changed scope decision')

    def test_prior_r1_handoff_and_committed_candidate_recover_distinct_r2_normally(self):
        old = copy.deepcopy(self.state())
        old_handoff = ao_workflow.retained_handoff(self.directory(), old)
        self.finish(); self.commit()
        new_handoff = self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertEqual(old_handoff['spec_revision'], 1)
        self.assertEqual(new_handoff['spec_revision'], 2)
        self.assertNotEqual(new_handoff['baseline_commit'], old_handoff['baseline_commit'])
        self.assertEqual(ao.read(self.directory() / old['handoff']), old_handoff)
        self.send('implementation', 'r2-implementation')
        (self.repo / 'r2-follow-up.txt').write_text('new required behavior\n')
        self.fake.finish('engineer', json.dumps(self.report())); self.service.ao_room_sync(self.room)
        self.send('correction', 'r2-correction')
        self.fake.finish('engineer', json.dumps(self.report())); self.service.ao_room_sync(self.room)
        status = self.service.ao_room_status(self.room)
        self.assertTrue(status['agreement']['agreed']); self.assertEqual(status['agreement']['spec_revision'], 2)
        self.assertEqual(status['spec_review_attempts'], 3); self.assertEqual(status['acceptance_review_attempts'], 0)
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        for key, row in old['requests'].items():
            self.assertEqual(self.state()['requests'][key], row)


class SettledQuotaRecoveryTests(RecoveryFixture):
    def history_before_source(self):
        self.agree(key='review1')
        self.agree(key='review2')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.service.ao_room_send(self.room, 'engineer', 'Implement this authorized feature.', 'quota-failure', 'implementation')
        request = self.state()['requests']['quota-failure']
        stamp = lambda offset: min(datetime.fromtimestamp(request['created_at'] + offset, timezone.utc), datetime.now(timezone.utc)).isoformat()
        self.native_events.extend([
            {'type': 'user', 'uuid': 'quota-failure-caller', 'sessionId': self.NATIVE,
             'timestamp': stamp(.001), 'cwd': str(self.repo), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': 'quota-failure-error', 'sessionId': self.NATIVE,
             'timestamp': stamp(.002), 'cwd': str(self.repo), 'isSidechain': False,
             'isApiErrorMessage': True, 'error': 'rate_limit', 'apiErrorStatus': 429,
             'message': {'role': 'assistant', 'model': '<synthetic>',
                         'content': [{'type': 'text', 'text': 'Synthetic native session limit'}]}}])
        self.transcript.write_text(''.join(json.dumps(row) + chr(10) for row in self.native_events))
        self.native_requests.add('quota-failure')
        self.fake.finish('engineer', state='failed'); self.service.ao_room_sync(self.room)
        audit = self.service.ao_room_outcome_audit(self.room)
        self.assertTrue(audit['resume_eligible'])
        self.service.ao_room_outcome_resume(self.room, 'quota-failure', audit['outcome_sha256'], 'quota-resume',
            'Actual standing human quota continuation authority; synthetic reset checked', 'Continue once')
        self.send('correction', 'quota-resume')
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required', implementation_complete=False,
                                                          remaining_gaps=['Finish after scope decision'])))
        self.service.ao_room_sync(self.room)

    def test_prior_supported_quota_settlement_remains_failed_native_history_and_admits_format_only(self):
        old = copy.deepcopy(self.state()['requests']['quota-failure'])
        self.assertEqual(old['state'], 'settled_failure')
        self.assertEqual(ao.read(self.directory() / old['receipt'])['turn']['state'], 'failed')
        self.finish(); self.assertTrue(self.commit()['agreed'])
        self.assertEqual(self.state()['requests']['quota-failure'], old)
        self.assertEqual(ao.read(self.directory() / old['receipt'])['turn']['state'], 'failed')
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 3)


if __name__ == '__main__':
    import unittest
    unittest.main()
