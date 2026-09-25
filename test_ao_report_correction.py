"""Attribution-only report recovery; hermetic AO, candidate and provider ledger."""
import copy
import json
import unittest

import ao_delegates
import ao_project_room as ao
import deepseek_adapter as ds
from test_ao_normal import DelegateFixture
import test_ao_continuation as continuation


class ReportCorrectionTests(DelegateFixture):
    JOB = 'a' * 32
    BAD = 'native-agent-123'
    job = continuation.DelegateEvidenceTests.job
    capture = continuation.DelegateEvidenceTests.capture
    correction = continuation.DelegateEvidenceTests.correction

    def setUp(self):
        super().setUp()
        self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.ledger = ds.Ledger(self.home)

    def refused(self, text=None):
        before = (self.directory() / 'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaises(ao.RoomError) as raised:
            self.correction()
        if text:
            self.assertIn(text, str(raised.exception))
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)

    def finish_correction(self, ids=None, **changes):
        self.fake.finish('engineer', json.dumps(self.report(
            routing_log=[{'delegate_job_ids': [self.JOB] if ids is None else ids,
                          'native_agent_ids': [self.BAD]}], **changes)))
        self.service.ao_room_sync(self.room)
        return self.state()['requests']['correction']

    def test_mixed_attribution_corrects_once_then_requires_normal_verification_and_acceptance(self):
        for ids in ([self.BAD, self.JOB], [self.JOB, self.BAD]):
            with self.subTest(ids=ids):
                self.setUp(); self.job(); original = copy.deepcopy(self.capture(ids=ids))
                receipt = (self.directory() / original['receipt']).read_bytes()
                self.assertIn('engineering_error', original)
                self.assertNotIn('engineering_record', original)
                with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
                posts = len(self.fake.posts); answer = self.correction()
                self.assertEqual(answer, self.correction())
                self.assertEqual(len(self.fake.posts), posts + 1)
                self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
                proof = self.state()['requests']['correction']['carried']['report_correction_admission']
                self.assertEqual(proof['rejected_attribution_ids'], [self.BAD])
                self.assertEqual([x['job_id'] for x in proof['verified_provider_jobs']], [self.JOB])
                corrected = self.finish_correction()
                self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
                self.assertEqual(self.state()['requests']['implementation'], original)
                self.assertEqual((self.directory() / original['receipt']).read_bytes(), receipt)
                with self.assertRaises(ao.RoomError): self.service.ao_room_accept(self.room, 'acceptance_review')
                self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_malformed_only_is_never_a_verified_provider_job(self):
        self.capture(ids=[self.BAD]); self.correction()
        proof = self.state()['requests']['correction']['carried']['report_correction_admission']
        self.assertEqual(proof['verified_provider_jobs'], [])
        corrected = self.finish_correction(ids=[])
        self.assertIn('engineering_record', corrected)

    def test_malformed_never_hides_other_provider_claim_failures_in_either_order(self):
        for kind in ('unknown', 'foreign', 'profile', 'model', 'content'):
            for reverse in (False, True):
                with self.subTest(kind=kind, reverse=reverse):
                    self.setUp()
                    if kind != 'unknown':
                        output = self.job(room='foreign' if kind == 'foreign' else None,
                            profile='f'*64 if kind == 'profile' else None,
                            model='wrong' if kind == 'model' else None)
                        if kind == 'content': output.write_text('changed')
                    self.capture(ids=[self.JOB, self.BAD] if reverse else [self.BAD, self.JOB])
                    self.refused()

    def test_unreported_active_and_unknown_jobs_still_block(self):
        for unsafe in (ds.STREAMING, ds.UNKNOWN):
            with self.subTest(unsafe=unsafe):
                self.setUp(); self.job(unsafe); self.capture(ids=[self.BAD])
                self.refused('Active or unresolved')

    def test_missing_ledger_still_blocks_malformed_only(self):
        self.capture(ids=[self.BAD]); (self.home / 'deepseek/ledger.sqlite3').unlink()
        self.refused('ledger is missing')

    def test_candidate_drift_before_correction_refuses(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB])
        (self.repo / 'feature.txt').write_text('drift\n')
        self.refused('candidate changed')

    def test_candidate_drift_during_correction_cannot_capture_or_verify(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
        (self.repo / 'feature.txt').write_text('drift\n')
        request = self.finish_correction()
        self.assertNotIn('engineering_record', request)
        self.assertIn('candidate changed', request['engineering_error'])
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))

    def test_invalid_report_cannot_be_accepted_after_recovery_dispatch(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
        request = self.finish_correction(ids=[self.BAD, self.JOB])
        self.assertNotIn('engineering_record', request)
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))

    def test_new_unknown_job_in_corrected_report_is_still_refused(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
        request = self.finish_correction(ids=['b'*32])
        self.assertNotIn('engineering_record', request)
        self.assertIn('unknown or foreign', request['engineering_error'])

    def test_scope_change_report_and_incomplete_shape_have_no_recovery(self):
        for changes in ({'outcome': 'scope_change'}, {'implementation_complete': 'yes'}, {'tests_reported': {}}):
            with self.subTest(changes=changes):
                self.setUp(); self.job(); self.capture(ids=[self.BAD, self.JOB], **changes)
                self.refused()

    def test_completion_evidence_damage_and_absence_refuse(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                self.setUp(); self.job(); req = self.capture(ids=[self.BAD, self.JOB])
                file = self.directory() / req['completion_candidate']
                if absent: file.unlink()
                else: file.write_text('{}')
                with self.assertRaises((ao.RoomError, OSError)):
                    self.correction()
                self.assertNotIn('correction', self.state()['requests'])

    def test_bound_report_id_metadata_mismatch_cannot_be_recovered(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB], spec_sha256='0'*64)
        self.refused('another spec')

    def test_preserved_job_content_is_rechecked_during_capture(self):
        output = self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
        output.write_text('changed')
        request = self.finish_correction()
        self.assertNotIn('engineering_record', request)
        self.assertIn('recorded digest', request['engineering_error'])

    def test_receipt_bound_admission_tamper_refuses(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction(); self.finish_correction()
        state = self.state(); proof = state['requests']['correction']['carried']['report_correction_admission']
        proof['candidate_sha256'] = '0'*64
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))


    def test_deleted_admission_or_carried_cannot_hide_damaged_original_evidence(self):
        for deletion in ('admission', 'carried'):
            with self.subTest(deletion=deletion):
                self.setUp(); output = self.job(); self.capture(ids=[self.BAD, self.JOB])
                self.correction(); self.finish_correction(ids=[])
                output.write_text('damaged original provider result')
                state = self.state(); request = state['requests']['correction']
                if deletion == 'admission': del request['carried']['report_correction_admission']
                else: del request['carried']
                ao.atomic(self.directory() / 'state.json', state)
                with self.assertRaisesRegex(ao.RoomError, 'metadata differs'):
                    self.service.ao_room_verify(self.room, str(self.repo))
                posts = len(self.fake.posts)
                with self.assertRaises(ao.RoomError):
                    self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'second', purpose='correction')
                self.assertEqual(len(self.fake.posts), posts)

    def test_second_correction_cannot_reset_candidate_after_drift_with_valid_or_invalid_report(self):
        for ids in ([self.BAD, self.JOB], [self.JOB]):
            with self.subTest(ids=ids):
                self.setUp(); self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
                (self.repo / 'unexpected.txt').write_text('drift during report-only correction')
                self.assertNotIn('engineering_record', self.finish_correction(ids=ids))
                posts = len(self.fake.posts)
                with self.assertRaisesRegex(ao.RoomError, 'candidate changed'):
                    self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'second', purpose='correction')
                self.assertEqual(len(self.fake.posts), posts)
                self.assertNotIn('second', self.state()['requests'])

    def test_unchanged_second_report_correction_keeps_original_evidence(self):
        self.job(); original = self.capture(ids=[self.BAD, self.JOB]); self.correction()
        self.finish_correction(ids=[self.BAD, self.JOB])
        self.service.ao_room_send(self.room, 'engineer', 'Correct only the report.', 'second', purpose='correction')
        self.fake.finish('engineer', json.dumps(self.report(routing_log=[{'delegate_job_ids': [self.JOB]}])))
        self.service.ao_room_sync(self.room)
        second = self.state()['requests']['second']
        self.assertIn('engineering_record', second, second.get('engineering_error'))
        self.assertEqual(second['result_candidate_sha256'],
                         second['carried']['report_correction_admission']['candidate_sha256'])
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_successful_repair_allows_later_normal_engineering_correction(self):
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction(); self.finish_correction()
        self.service.ao_room_send(self.room, 'engineer', 'Implement a reviewed follow-up.', 'next-work', purpose='correction')
        self.assertNotIn('report_correction_admission', self.state()['requests']['next-work']['carried'])
        (self.repo / 'followup.txt').write_text('authorized ordinary engineering')
        self.fake.finish('engineer', json.dumps(self.report(routing_log=[{'delegate_job_ids': [self.JOB]}])))
        self.service.ao_room_sync(self.room)
        self.assertIn('engineering_record', self.state()['requests']['next-work'])
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])


if __name__ == '__main__':
    unittest.main()
