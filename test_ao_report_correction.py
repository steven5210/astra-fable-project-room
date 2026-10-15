"""Attribution-only report recovery; hermetic AO, candidate and provider ledger."""
import copy
import json
import unittest

import ao_delegates
import ao_project_room as ao
import ao_workflow
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
        # The real shared producer binds this exact saved request's caller/model/owner/session and
        # timestamps, so the intended correction completion has its own native rows before the sync.
        self.note_native_turn('correction')
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
        self.job(); self.capture(ids=[self.BAD, self.JOB]); self.correction()
        self.finish_correction(ids=[self.BAD, self.JOB])
        self.service.ao_room_send(self.room, 'engineer', 'Correct only the report.', 'second', purpose='correction')
        self.note_native_turn('second')  # A direct second correction needs its own exact native rows.
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
        self.note_native_turn('next-work')  # This later direct completion needs its own native rows too.
        self.assertNotIn('report_correction_admission', self.state()['requests']['next-work']['carried'])
        (self.repo / 'followup.txt').write_text('authorized ordinary engineering')
        self.fake.finish('engineer', json.dumps(self.report(routing_log=[{'delegate_job_ids': [self.JOB]}])))
        self.service.ao_room_sync(self.room)
        self.assertIn('engineering_record', self.state()['requests']['next-work'])
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    # Compound report recovery: malformed native attribution together with malformed operator_requests.
    MALFORMED_REQUESTS = ['Run the agreed gates']
    # The live report's shape: twelve routing entries whose delegate_job_ids name native worker ids
    # (short hex strings), never the room's provider ledger ids.
    NATIVE_IDS = ('a11c02469bd0d0c16', 'b22d13570ce1e1d27', 'c33e24681df2f2e38', 'd44f35792e0303d49',
                  'e55046803f1414e5a', 'f6615791402525f6b', '0772681251363607c', '1883792362474718d',
                  '2994803473585829e', '30a591458469693af', '41b60256957a7a4b0', '52c71367068b8b5c1')
    PROOF_KEYS = {'kind', 'request_id', 'receipt_sha256', 'report_sha256', 'completion_candidate_sha256',
                  'candidate_sha256', 'handoff_sha256', 'spec_record_sha256', 'rejected_attribution_ids',
                  'verified_provider_jobs'}
    VALID_REQUEST = {'task': 'Run the agreed gates on the exact candidate', 'inputs': ['Agreed gate list'],
                     'verification': ['Every agreed gate exits 0']}

    def live_routing(self, ids=None):
        """The live room's malformed routing log; ids, when given, replaces it with one flat entry."""
        if ids is not None:
            return [{'delegate_job_ids': ids}]
        return [{'delegate_job_ids': [native]} for native in self.NATIVE_IDS]

    def compact(self, report):
        """The compact native form of a full report; the controller attaches the bound identifiers."""
        value = {k: v for k, v in report.items() if k not in ('spec_revision', 'spec_sha256', 'baseline_commit')}
        return {**value, 'report_format': 'project_room_engineering_v2'}

    def capture_compound(self, ids=None, compact=False, **changes):
        """Complete the implementation with both report defects in the full or compact form."""
        (self.repo / 'feature.txt').write_text('implemented\n')
        report = self.report(routing_log=self.live_routing(ids), operator_requests=self.MALFORMED_REQUESTS,
                             **changes)
        self.fake.finish('engineer', json.dumps(report if not compact else self.compact(report)))
        self.service.ao_room_sync(self.room)
        return self.state()['requests']['implementation']

    def fresh(self, index):
        """Each later subtest starts from a new room; the first reuses the room unittest just set up."""
        if index:
            self.setUp()

    def restart(self):
        self.service = ao.Service(self.home, lambda url: self.fake)

    def send_correction(self, request_id, message='Continue.'):
        return self.service.ao_room_send(self.room, 'engineer', message, request_id, purpose='correction')

    def admission(self, request_id):
        return self.state()['requests'][request_id]['carried']['report_correction_admission']

    def finish_turn(self, request_id, text):
        # A directly sent correction needs its own exact native rows before its completion sync.
        self.note_native_turn(request_id)
        self.fake.finish('engineer', text)
        self.service.ao_room_sync(self.room)
        return self.state()['requests'][request_id]

    def finish_report(self, request_id, ids=None, **changes):
        report = self.report(routing_log=[{'delegate_job_ids': [self.JOB] if ids is None else ids}], **changes)
        return self.finish_turn(request_id, json.dumps(report))

    def refused_send(self, request_id, text=None, errors=ao.RoomError):
        """A refused correction leaves no intent, POST or saved-state change; an earlier evidence guard may refuse first."""
        before = (self.directory() / 'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(errors, text or ''):
            self.send_correction(request_id)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)
        self.assertNotIn(request_id, self.state()['requests'])

    def test_compound_attribution_and_operator_requests_defects_admit_one_report_only_correction(self):
        for ids in (None, [self.BAD, self.JOB], [self.JOB, self.BAD]):
            for compact in (False, True):
                with self.subTest(ids=ids, compact=compact):
                    if compact or ids is not None:
                        self.setUp()
                    self.job()
                    original = copy.deepcopy(self.capture_compound(ids=ids, compact=compact))
                    receipt = (self.directory() / original['receipt']).read_bytes()
                    self.assertIn('Malformed operator_requests', original['engineering_error'])
                    self.assertNotIn('engineering_record', original)
                    with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
                    posts = len(self.fake.posts); answer = self.correction()
                    self.assertEqual(answer, self.correction())
                    self.assertEqual(len(self.fake.posts), posts + 1)
                    self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
                    proof = self.state()['requests']['correction']['carried']['report_correction_admission']
                    self.assertEqual(proof['kind'], 'malformed_delegate_attribution')
                    if ids is None:
                        self.assertEqual(proof['rejected_attribution_ids'], list(self.NATIVE_IDS))
                        self.assertEqual(proof['verified_provider_jobs'], [])
                    else:
                        self.assertEqual(proof['rejected_attribution_ids'], [self.BAD])
                        self.assertEqual([x['job_id'] for x in proof['verified_provider_jobs']], [self.JOB])
                    self.assertIs(proof['malformed_operator_requests'], True)
                    self.assertEqual(set(proof), self.PROOF_KEYS | {'malformed_operator_requests'})
                    self.assertEqual(self.state()['requests']['implementation'], original)
                    self.assertEqual((self.directory() / original['receipt']).read_bytes(), receipt)
                    corrected = self.finish_correction(operator_requests=[])
                    self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
                    self.assertEqual(corrected['result_candidate_sha256'], proof['candidate_sha256'])
                    self.assertEqual(self.state()['requests']['implementation'], original)
                    self.assertEqual((self.directory() / original['receipt']).read_bytes(), receipt)
                    self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
                    self.assertEqual(len(self.fake.posts), posts + 2)

    def test_attribution_only_proof_is_unchanged_and_other_callers_stay_strict(self):
        for index, extra in enumerate(({}, {'operator_requests': []})):
            with self.subTest(extra=extra):
                self.fresh(index); self.job()
                original = copy.deepcopy(self.capture(ids=[self.BAD, self.JOB], **extra))
                self.correction()
                state = self.state()
                verified = ao_delegates.verify_delegation(self.home, self.directory(), state,
                                                          {'routing_log': [{'delegate_job_ids': [self.JOB]}]})
                completion = ao.read(self.directory() / original['completion_candidate'])['candidate']['sha256']
                expected = {'kind': 'malformed_delegate_attribution', 'request_id': 'implementation',
                            'receipt_sha256': original['receipt_sha256'],
                            'report_sha256': ao.digest(ao_workflow.engineering_report(self.directory(), state, original)),
                            'completion_candidate_sha256': original['completion_candidate_sha256'],
                            'candidate_sha256': completion, 'handoff_sha256': original['handoff_sha256'],
                            'spec_record_sha256': original['spec_record_sha256'],
                            'rejected_attribution_ids': [self.BAD], 'verified_provider_jobs': verified}
                self.assertEqual(self.admission('correction'), expected)
                self.assertEqual(set(expected), self.PROOF_KEYS)
        with self.subTest(compound=True):
            self.setUp(); self.job(); compound = self.capture_compound()
            with self.assertRaisesRegex(ao.RoomError,
                                        r'^Malformed operator_requests entry; preserve the response and correct the report$'):
                ao_workflow.engineering_report(self.directory(), self.state(), compound)
            report, malformed = ao_workflow.recovery_report(self.directory(), self.state(), compound)
            self.assertIs(malformed, True)
            self.assertEqual(report['operator_requests'], self.MALFORMED_REQUESTS)
            self.correction()
            self.assertEqual(self.admission('correction')['report_sha256'], ao.digest(report))

    def test_malformed_operator_requests_alone_keep_the_ordinary_correction(self):
        self.job(); original = self.capture(ids=[self.JOB], operator_requests=self.MALFORMED_REQUESTS)
        self.assertIn('Malformed operator_requests', original['engineering_error'])
        posts = len(self.fake.posts); self.correction()
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertNotIn('report_correction_admission', self.state()['requests']['correction']['carried'])
        (self.repo / 'followup.txt').write_text('ordinary engineering may change the candidate\n')
        corrected = self.finish_report('correction', operator_requests=[])
        self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
        completion = ao.read(self.directory() / original['completion_candidate'])['candidate']['sha256']
        self.assertNotEqual(corrected['result_candidate_sha256'], completion)
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_each_single_defect_and_the_compound_defect_admit_one_correction(self):
        """The three-case matrix: each defect alone already admitted its correction; the compound now does."""
        for index, (name, ids, requests) in enumerate((
                ('attribution_only', [self.BAD, self.JOB], None),
                ('operator_requests_only', [self.JOB], self.MALFORMED_REQUESTS),
                ('compound', None, self.MALFORMED_REQUESTS))):
            with self.subTest(name=name):
                self.fresh(index); self.job()
                if name == 'compound':
                    original = self.capture_compound()
                else:
                    changes = {} if requests is None else {'operator_requests': requests}
                    original = self.capture(ids=ids, **changes)
                self.assertIn('engineering_error', original)
                self.assertNotIn('engineering_record', original)
                before = copy.deepcopy(self.state()['requests']['implementation'])
                posts = len(self.fake.posts)
                self.correction()
                self.assertEqual(len(self.fake.posts), posts + 1)
                self.assertEqual(self.state()['requests']['implementation'], before)
                carried = self.state()['requests']['correction']['carried']
                if name == 'operator_requests_only':
                    self.assertNotIn('report_correction_admission', carried)
                    continue
                proof = carried['report_correction_admission']
                self.assertEqual(proof['kind'], 'malformed_delegate_attribution')
                if name == 'compound':
                    self.assertIs(proof['malformed_operator_requests'], True)
                else:
                    self.assertNotIn('malformed_operator_requests', proof)

    def test_a_corrected_report_without_operator_requests_must_disposition_explicitly(self):
        self.job(); self.capture_compound(); self.correction()
        corrected = self.finish_correction()  # Strict shape, but operator_requests is simply omitted.
        self.assertNotIn('engineering_record', corrected)
        self.assertEqual(corrected['engineering_error'], ao_workflow.DISPOSITION_REQUIRED)
        posts = len(self.fake.posts)
        self.send_correction('dispositioned', 'State the operator_requests disposition.')
        self.assertEqual(len(self.fake.posts), posts + 1)
        fixed = self.finish_report('dispositioned', operator_requests=[])
        self.assertIn('engineering_record', fixed, fixed.get('engineering_error'))
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_compound_lost_ack_reconciles_without_a_second_post(self):
        self.job(); self.capture_compound()
        self.fake.lose_ack = True
        posts = len(self.fake.posts)
        first = self.correction()
        self.assertEqual(first['state'], 'uncertain')
        self.assertEqual(self.correction(), first)
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.fake.lose_ack = False
        corrected = self.finish_correction(operator_requests=[])
        self.assertEqual(corrected['state'], 'completed')
        self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
        self.assertEqual(self.correction()['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertIs(self.admission('correction')['malformed_operator_requests'], True)
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_compound_admission_survives_restart_before_and_after_admission(self):
        self.job(); self.capture_compound()
        self.restart()
        posts = len(self.fake.posts)
        answer = self.correction()
        proof = self.admission('correction')
        self.restart()
        self.assertEqual(self.correction(), answer)
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(self.admission('correction'), proof)
        self.assertIs(proof['malformed_operator_requests'], True)
        corrected = self.finish_correction(operator_requests=[])
        self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
        self.restart()
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_compound_successor_with_valid_operator_requests_blocks_acceptance_until_ordinary_correction(self):
        self.job(); self.capture_compound(); self.correction()
        corrected = self.finish_correction(operator_requests=[self.VALID_REQUEST])
        self.assertIn('engineering_record', corrected, corrected.get('engineering_error'))
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_verify(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'pending operator requests'):
            self.service.ao_room_accept(self.room, 'acceptance_review')
        posts = len(self.fake.posts)
        self.send_correction('next-work', 'Report the completed authorized operator work.')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertNotIn('report_correction_admission', self.state()['requests']['next-work']['carried'])
        (self.repo / 'followup.txt').write_text('authorized ordinary engineering after the report repair\n')
        later = self.finish_report('next-work', operator_requests=[])
        self.assertIn('engineering_record', later, later.get('engineering_error'))
        self.assertNotEqual(later['result_candidate_sha256'], corrected['result_candidate_sha256'])
        self.review(); self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    # Negatives for a compound predecessor, at the production entry points.
    def test_compound_provider_claim_failures_refuse_before_admission_in_either_order(self):
        cases = [(kind, reverse) for kind in ('unknown', 'foreign', 'profile', 'model', 'content')
                 for reverse in (False, True)]
        for index, (kind, reverse) in enumerate(cases):
            with self.subTest(kind=kind, reverse=reverse):
                self.fresh(index)
                if kind != 'unknown':
                    output = self.job(room='foreign' if kind == 'foreign' else None,
                                      profile='f'*64 if kind == 'profile' else None,
                                      model='wrong' if kind == 'model' else None)
                    if kind == 'content': output.write_text('changed')
                self.capture_compound(ids=[self.JOB, self.BAD] if reverse else [self.BAD, self.JOB])
                self.refused()
                self.assertNotIn('correction', self.state()['requests'])

    def test_compound_unreported_active_unknown_jobs_and_missing_ledger_refuse(self):
        for index, unsafe in enumerate((ds.STREAMING, ds.UNKNOWN)):
            with self.subTest(unsafe=unsafe):
                self.fresh(index); self.job(unsafe); self.capture_compound(ids=[self.BAD])
                self.refused('Active or unresolved')
        with self.subTest(missing_ledger=True):
            self.setUp(); self.capture_compound(ids=[self.BAD]); (self.home / 'deepseek/ledger.sqlite3').unlink()
            self.refused('ledger is missing')

    def test_compound_candidate_drift_before_or_during_correction_refuses(self):
        with self.subTest(drift='before'):
            self.job(); self.capture_compound()
            (self.repo / 'feature.txt').write_text('drift\n')
            self.refused('candidate changed')
        with self.subTest(drift='during'):
            self.setUp(); self.job(); self.capture_compound(); self.correction()
            (self.repo / 'unexpected.txt').write_text('drift during the compound report-only correction')
            request = self.finish_correction(operator_requests=[])
            self.assertNotIn('engineering_record', request)
            self.assertIn('candidate changed', request['engineering_error'])
            with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))

    def test_compound_scope_change_incomplete_shape_and_other_spec_have_no_recovery(self):
        cases = (({'outcome': 'scope_change'}, 'scope change'), ({'implementation_complete': 'yes'}, 'incomplete'),
                 ({'tests_reported': {}}, 'incomplete'), ({'spec_sha256': '0'*64}, 'another spec'))
        for index, (changes, text) in enumerate(cases):
            with self.subTest(changes=changes):
                self.fresh(index); self.job(); self.capture_compound(**changes)
                self.refused(text)

    def test_compound_completion_evidence_damage_and_absence_refuse(self):
        for index, absent in enumerate((False, True)):
            with self.subTest(absent=absent):
                self.fresh(index); self.job(); req = self.capture_compound()
                file = self.directory() / req['completion_candidate']
                if absent: file.unlink()
                else: file.write_text('{}')
                posts = len(self.fake.posts)
                with self.assertRaises((ao.RoomError, OSError)):
                    self.correction()
                self.assertNotIn('correction', self.state()['requests'])
                self.assertEqual(len(self.fake.posts), posts)

    def test_compound_preserved_job_content_is_rechecked_during_capture(self):
        output = self.job(); self.capture_compound(); self.correction()
        output.write_text('changed')
        request = self.finish_correction(operator_requests=[])
        self.assertNotIn('engineering_record', request)
        self.assertIn('recorded digest', request['engineering_error'])

    def test_compound_receipt_bound_admission_tamper_and_deletion_refuse(self):
        for index, change in enumerate(('flag', 'candidate', 'admission', 'carried')):
            with self.subTest(change=change):
                self.fresh(index); output = self.job(); self.capture_compound(); self.correction()
                self.assertIn('engineering_record', self.finish_correction(operator_requests=[]))
                if change in ('admission', 'carried'):
                    output.write_text('damaged original provider result')
                state = self.state(); request = state['requests']['correction']
                proof = request['carried']['report_correction_admission']
                if change == 'flag': del proof['malformed_operator_requests']
                elif change == 'candidate': proof['candidate_sha256'] = '0'*64
                elif change == 'admission': del request['carried']['report_correction_admission']
                else: del request['carried']
                ao.atomic(self.directory() / 'state.json', state)
                with self.assertRaisesRegex(ao.RoomError, 'metadata differs'):
                    self.service.ao_room_verify(self.room, str(self.repo))
                with self.assertRaisesRegex(ao.RoomError, 'metadata differs'):
                    self.service.ao_room_accept(self.room, 'acceptance_review')
                self.refused_send('second')  # The quality-evidence integrity guard may refuse before the walk.


if __name__ == '__main__':
    unittest.main()
