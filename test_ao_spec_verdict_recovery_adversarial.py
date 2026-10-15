"""Independent boundary probes using synthetic AO/native evidence, never live rooms."""
import json
from unittest.mock import patch

import ao_project_room as ao
import ao_response_normalization as normalization
from test_ao_normal import Fixture


class VerdictRecoveryAdversarialTests(Fixture):
    SOURCE = 'original-third-review'
    RECOVERY = 'format-only-recovery'

    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()
        original = self.fake.request
        def complete_history(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original(method, path, payload)
        self.fake.request = complete_history
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.agree(key='first-review')
        self.agree(key='second-review')
        self.send('spec_review', self.SOURCE)
        self.fake.finish('engineer', 'I agree to the exact current specification. No specification change is required.')
        self.service.ao_room_sync(self.room)

    def audit(self):
        return self.service.ao_room_spec_verdict_recovery_audit(
            self.room, self.SOURCE, self.RECOVERY,
            self.native_outcome_source['database'], self.NATIVE)

    def grant(self, audit=None):
        return self.service.ao_room_spec_verdict_recovery_grant(
            self.room, (audit or self.audit())['audit_sha256'],
            'The human authorizes one format-only reissue of this exact completed verdict.',
            'synthetic-human-decision')

    def dispatch(self, grant=None):
        result = self.service.ao_room_send(**(grant or self.grant())['send_args'])
        self.note_native_turn(self.RECOVERY)
        return result

    def verdict(self, **changes):
        state = self.state()
        source = state['requests'][self.SOURCE]
        raw, identity = normalization.source(self.directory(), source)
        spec = self.service.spec(self.directory(), state)
        return {'interpretation': 'I agree to the exact current specification. No specification change is required.',
                'findings': [], 'decision': 'accept', 'spec_revision': spec['revision'],
                'spec_sha256': spec['sha256'], 'source_request_id': self.SOURCE,
                'source_receipt_sha256': identity['receipt_sha256'],
                'source_final_text_sha256': ao.digest(raw.encode()), 'meaning_unchanged': True, **changes}

    def finish(self, **changes):
        self.fake.finish('engineer', json.dumps(self.verdict(**changes)))
        self.service.ao_room_sync(self.room)

    def commit(self, **changes):
        request = self.state()['requests'][self.RECOVERY]
        raw, identity = normalization.source(self.directory(), request)
        args = {'room_id': self.room, 'recovery_request_id': self.RECOVERY,
                'receipt_sha256': identity['receipt_sha256'], 'final_text_sha256': ao.digest(raw.encode()),
                'astra_review': 'I compared both complete native responses. The structured response faithfully preserves the original exact-spec agreement without new findings or scope.',
                'confirm_same_verdict': True, **changes}
        return self.service.ao_room_spec_verdict_recovery_commit(**args)

    def test_budget_and_original_receipt_survive_then_engineering_can_change_candidate(self):
        before = self.state()['requests'][self.SOURCE]
        receipt_bytes = (self.directory() / before['receipt']).read_bytes()
        self.dispatch(); self.finish(); self.commit()
        state = self.state()
        self.assertEqual((self.directory() / before['receipt']).read_bytes(), receipt_bytes)
        self.assertEqual(sum(r.get('purpose') == 'spec_review' for r in state['requests'].values()), 3)
        self.assertEqual(sum(r.get('role') == 'reviewer' for r in state['requests'].values()), 0)
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.agree(key='fourth-review'); self.agree(key='fifth-review')
        with self.assertRaisesRegex(ao.RoomError, 'reached the room ceiling'):
            self.send('spec_review', 'not-a-free-sixth-review')
        self.implement()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.send('correction', 'normal-later-correction')

    def test_candidate_drift_after_audit_refuses_grant_without_post(self):
        audit = self.audit(); posts = len(self.fake.posts)
        (self.repo / 'feature.txt').write_text('changed after audit\n')
        with self.assertRaises(ao.RoomError): self.grant(audit)
        self.assertEqual(len(self.fake.posts), posts)

    def test_candidate_drift_after_grant_refuses_send(self):
        grant = self.grant(); posts = len(self.fake.posts)
        (self.repo / 'feature.txt').write_text('changed after grant\n')
        with self.assertRaises(ao.RoomError): self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(len(self.fake.posts), posts)

    def test_candidate_drift_after_native_result_refuses_commit(self):
        self.dispatch(); self.finish()
        (self.repo / 'feature.txt').write_text('changed after recovery\n')
        with self.assertRaises(ao.RoomError): self.commit()
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])

    def test_native_owner_became_active_refuses_audit(self):
        self.owner['activity_state'] = 'working'
        posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.audit()
        self.assertEqual(len(self.fake.posts), posts)

    def test_unknown_native_source_refuses_audit(self):
        self.transcript.write_text('')
        with self.assertRaises(ao.RoomError): self.audit()

    def test_source_receipt_symlink_even_to_identical_bytes_refuses(self):
        request = self.state()['requests'][self.SOURCE]
        original = self.directory() / request['receipt']
        elsewhere = self.root / 'identical-receipt.json'
        elsewhere.write_bytes(original.read_bytes())
        original.unlink(); original.symlink_to(elsewhere)
        with self.assertRaises(ao.RoomError): self.audit()

    def test_new_spec_after_grant_refuses_format_dispatch(self):
        grant = self.grant()
        # A spec update may itself refuse while a recovery is pending. Either way
        # the old grant must never dispatch for a newer spec.
        try:
            self.spec(revision=2)
        except ao.RoomError:
            return
        with self.assertRaises(ao.RoomError): self.service.ao_room_send(**grant['send_args'])

    def test_operator_cannot_omit_or_deny_semantic_comparison(self):
        self.dispatch(); self.finish()
        with self.assertRaises(ao.RoomError): self.commit(confirm_same_verdict=False)
        with self.assertRaises(ao.RoomError): self.commit(astra_review='')
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])

    def test_positive_model_claim_cannot_override_a_new_blocker(self):
        self.dispatch(); self.finish(findings=['BLOCKER: a new requirement is missing'])
        with self.assertRaises(ao.RoomError): self.commit()
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])

    def test_recovery_request_map_deletion_cannot_leave_agreement(self):
        self.dispatch(); self.finish(); self.commit()
        state = self.state(); del state['requests'][self.RECOVERY]
        ao.atomic(self.directory() / 'state.json', state)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_handoff(self.room, str(self.repo))

    def test_original_request_map_deletion_cannot_fall_back_to_older_agreement(self):
        self.dispatch(); self.finish(); self.commit()
        state = self.state(); del state['requests'][self.SOURCE]
        ao.atomic(self.directory() / 'state.json', state)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_handoff(self.room, str(self.repo))

    def test_request_payload_substitution_blocks_downstream_handoff(self):
        self.dispatch(); self.finish(); self.commit()
        state = self.state(); state['requests'][self.RECOVERY]['text'] = 'A different instruction'
        ao.atomic(self.directory() / 'state.json', state)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_handoff(self.room, str(self.repo))

    def test_recovery_receipt_symlink_refuses_commit(self):
        self.dispatch(); self.finish()
        request = self.state()['requests'][self.RECOVERY]
        original = self.directory() / request['receipt']
        elsewhere = self.root / 'identical-recovery.json'
        elsewhere.write_bytes(original.read_bytes())
        original.unlink(); original.symlink_to(elsewhere)
        with self.assertRaises(ao.RoomError): self.commit()

    def test_generic_correction_cannot_escape_pending_format_grant(self):
        self.grant(); posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError): self.send('correction', 'escape-the-grant')
        self.assertEqual(len(self.fake.posts), posts)

    def test_edited_format_instruction_cannot_use_grant(self):
        grant = self.grant(); posts = len(self.fake.posts)
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_send(**{**grant['send_args'], 'message': 'Implement new product requirements now.'})
        self.assertEqual(len(self.fake.posts), posts)

    def test_crash_after_consumption_before_state_save_never_recreates_intent(self):
        grant = self.grant(); posts = len(self.fake.posts)
        save = self.service.save
        def crash(directory, state):
            if self.RECOVERY in state['requests']:
                raise OSError('synthetic disk failure after durable consumption')
            return save(directory, state)
        with patch.object(self.service, 'save', side_effect=crash):
            with self.assertRaises(OSError): self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaises(ao.RoomError): self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(len(self.fake.posts), posts)

    def test_crash_after_saved_intent_before_post_returns_uncertain_without_replay(self):
        grant = self.grant(); posts = len(self.fake.posts)
        save = self.service.save
        def crash(directory, state):
            save(directory, state)
            if self.RECOVERY in state['requests']:
                raise OSError('synthetic crash before provider POST')
        with patch.object(self.service, 'save', side_effect=crash):
            with self.assertRaises(OSError): self.service.ao_room_send(**grant['send_args'])
        result = self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)

    def close(self, **changes):
        request = self.state()['requests'][self.RECOVERY]
        raw, identity = normalization.source(self.directory(), request)
        args = {'room_id': self.room, 'recovery_request_id': self.RECOVERY,
                'receipt_sha256': identity['receipt_sha256'], 'final_text_sha256': identity['final_text_sha256'],
                'astra_review': 'I compared both complete native responses and cannot affirm the same verdict; the lane closes without agreement.',
                **changes}
        return self.service.ao_room_spec_verdict_recovery_close(**args)

    def test_deleted_closure_is_damaged_and_never_pending(self):
        self.dispatch(); self.finish(decision='changes_required', findings=['New requirement'], meaning_unchanged=False)
        self.close()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_verdict_recovery']['status'], 'closed')
        (self.directory() / 'spec-verdict-recovery' / 'chains' / self.SOURCE / 'closure.json').unlink()
        state = self.state()
        with self.assertRaisesRegex(ao.RoomError, 'missing, changed or unclaimed'):
            self.service.quiet(state)
        with self.assertRaisesRegex(ao.RoomError, 'missing, changed or unclaimed'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        status = self.service.ao_room_status(self.room)
        self.assertFalse(status['agreement']['agreed'])
        self.assertIn('missing, changed or unclaimed', status['agreement']['reason'])
        self.assertEqual(status['spec_verdict_recovery']['status'], 'damaged')
        self.assertFalse(status['spec_verdict_recovery']['pending'])

    def test_forged_agreement_over_a_rejected_result_cannot_validate(self):
        self.dispatch(); self.finish(decision='changes_required', findings=['New requirement'], meaning_unchanged=False)
        state = self.state(); request = state['requests'][self.RECOVERY]
        forged = {'version': 1, 'room_id': self.room,
                  'grant_sha256': state['spec_verdict_recovery']['grant_sha256'],
                  'intent_sha256': state['spec_verdict_recovery']['intent_sha256'],
                  'completion_sha256': request['spec_verdict_recovery_completion_sha256'],
                  'inputs': {'recovery_request_id': self.RECOVERY, 'receipt_sha256': request['receipt_sha256'],
                             'final_text_sha256': '0' * 64, 'astra_review': 'forged', 'confirm_same_verdict': True},
                  'verdict_sha256': '0' * 64}
        ao.atomic(self.directory() / 'spec-verdict-recovery' / 'chains' / self.SOURCE / 'agreement.json', forged)
        state['spec_verdict_recovery']['agreement_sha256'] = ao.digest(forged)
        ao.atomic(self.directory() / 'state.json', state)
        status = self.service.ao_room_status(self.room)
        self.assertFalse(status['agreement']['agreed'])
        self.assertEqual(status['spec_verdict_recovery']['status'], 'damaged')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_handoff(self.room, str(self.repo))

    def test_lost_ack_reconciles_original_delivery_without_second_post(self):
        grant = self.grant(); request = self.fake.request
        def lose_ack(method, path, payload=None):
            result = request(method, path, payload)
            if method == 'POST' and path.endswith('/conversation/messages'):
                raise OSError('synthetic acknowledgement loss after delivery')
            return result
        with patch.object(self.fake, 'request', side_effect=lose_ack):
            result = self.service.ao_room_send(**grant['send_args'])
        self.assertEqual(result['state'], 'uncertain')
        posts = len(self.fake.posts)
        self.assertEqual(self.service.ao_room_send(**grant['send_args'])['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)
        self.note_native_turn(self.RECOVERY)
        self.finish(); self.commit()
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.assertEqual(len(self.fake.posts), posts)
