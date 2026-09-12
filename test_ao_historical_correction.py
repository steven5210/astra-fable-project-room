"""Known-completed historical-provider report correction; synthetic AO and ledger only."""
import copy
import hashlib
import json
import unittest

import ao_delegates
import ao_project_room as ao
import ao_provider_transition as transition
import ao_workflow
import deepseek_adapter as ds
from test_ao_adoption import AdoptionFixture


class HistoricalCorrectionTests(AdoptionFixture):
    def setUp(self):
        super().setUp()
        self.source_jobs = [self.add_job(i, source=True) for i in range(1, 4)]

    def add_job(self, number, source=False, state=ds.REJECTED, content=None):
        job_id = format(number, '032x')
        profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        model = transition.SOURCE['model'] if source else transition.TARGET['model']
        content_path = None
        if content is not None:
            (self.home/'deepseek/jobs').mkdir(mode=0o700, exist_ok=True)
            path = self.home / 'deepseek/jobs' / job_id / 'content'
            path.parent.mkdir(mode=0o700)
            path.write_bytes(content)
            path.chmod(0o600)
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,'
                       'requested_model,thinking,reasoning_effort,max_tokens,input_bytes,possibly_billed,reserved_bytes,'
                       'content_sha256,content_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, self.room, 'job-'+str(number), 'deep', 'a'*64, profile, state,
                        '2026-01-01T00:00:00+00:00', model, 'enabled', 'max', 131072 if source else 393216,
                        10, 0 if state == ds.REJECTED else 1, 1,
                        hashlib.sha256(content).hexdigest() if content is not None else None, content_path))
        return job_id

    def ready(self):
        self.prepared = self.configure_routing()
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.start_attachment(self.prepared)

    def complete(self, ids=None, **overrides):
        self.service.ao_room_send(self.room, 'engineer', 'Author one bounded section.', 'section', purpose='implementation')
        report = self.report(outcome='changes_required', implementation_complete=False, remaining_gaps=['Other sections'],
                             routing_log=[{'step':'current native work', 'delegate_job_ids':[]},
                                          {'step':'previous provider attempts', 'delegate_job_ids':self.source_jobs if ids is None else ids}])
        report.update(overrides)
        self.fake.finish('engineer', json.dumps(report))
        self.service.ao_room_sync(self.room)
        return self.state()['requests']['section']

    def correct(self, key='correction', text='Continue.'):
        return self.service.ao_room_send(self.room, 'engineer', text, key, purpose='correction')

    def assert_refused(self, pattern=None):
        before = (self.directory()/'state.json').read_bytes()
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaises(ao.RoomError) as raised:
            self.correct()
        if pattern:
            self.assertIn(pattern, str(raised.exception))
        self.assertEqual((self.directory()/'state.json').read_bytes(), before)
        self.assertEqual(self.fake.posts, posts)

    def test_historical_nonresults_allow_new_correction_without_accepting_prior_report(self):
        self.ready()
        original = self.complete()
        self.assertIn('pinned provider configuration', original['engineering_error'])
        self.assertNotIn('engineering_record', original)
        receipt_path = self.directory()/original['receipt']
        receipt = receipt_path.read_bytes()
        completion = (self.directory()/original['completion_candidate']).read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'captured completed engineering result'):
            ao_workflow.engineering_ready(self.service, self.directory(), self.state())
        # Operator persistence after the response belongs to the next candidate, never recaptured as the old one.
        (self.repo/'saved-section.md').write_text('Exact synthetic authored section.')
        result = self.correct(text='Correct the historical routing claims and continue.')
        self.assertEqual(result['state'], 'submitted')
        request = self.state()['requests']['correction']
        self.assertEqual(request['text'], 'Correct the historical routing claims and continue.')
        self.assertEqual(request['carried']['parts'], [])
        self.assertIsNone(request['carried']['spec_delivery'])
        self.assertEqual(self.state()['requests']['section'], original)
        self.assertEqual(receipt_path.read_bytes(), receipt)
        self.assertEqual((self.directory()/original['completion_candidate']).read_bytes(), completion)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_attempts'], 3)



    def test_admission_is_audited_and_idempotent_then_normal_acceptance_requires_new_result(self):
        self.ready()
        original = self.complete()
        first = self.correct()
        request = self.state()['requests']['correction']
        admission = request['carried']['correction_admission']
        self.assertTrue(admission['admission_only'])
        self.assertEqual(admission['prior_request_id'], 'section')
        self.assertEqual(admission['prior_receipt_sha256'], original['receipt_sha256'])
        self.assertEqual([r['job_id'] for r in admission['historical_jobs']], self.source_jobs)
        self.assertEqual(admission['current_jobs'], [])
        posts = len(self.fake.posts)
        self.assertEqual(self.correct(), first)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'another payload'):
            self.correct(text='Changed same-request bytes')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_verify(self.room, str(self.repo))
        (self.repo/'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        corrected = self.state()['requests']['correction']
        self.assertIn('engineering_record', corrected)
        self.assertNotIn('engineering_error', corrected)
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.assertEqual(self.state()['requests']['section'], original)

    def test_mixed_current_and_historical_nonresults_are_verified_separately(self):
        self.ready()
        current = self.add_job(4)
        self.complete(ids=self.source_jobs+[current])
        self.correct()
        admission = self.state()['requests']['correction']['carried']['correction_admission']
        self.assertEqual([r['job_id'] for r in admission['current_jobs']], [current])
        self.assertEqual({r['requested_model'] for r in admission['historical_jobs']}, {transition.SOURCE['model']})
        self.assertEqual(admission['current_jobs'][0]['requested_model'], transition.TARGET['model'])

    def test_unreported_original_row_cannot_disappear_or_change(self):
        self.ready()
        self.complete(ids=self.source_jobs[:1])
        job = self.source_jobs[-1]
        with self.ledger.reading() as db:
            original = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone())
        with self.ledger.transaction() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (job,))
        self.assert_refused('captured transition-ledger rows')
        with self.ledger.transaction() as db:
            columns = list(original)
            db.execute('INSERT INTO jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                       [original[k] for k in columns])
        for field, value in [('request_id','changed'), ('created_at','changed'), ('finished_at','changed'),
                             ('possibly_billed',1), ('content_sha256','b'*64)]:
            with self.subTest(field=field):
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET '+field+'=? WHERE id=?', (value,job))
                self.assert_refused('captured transition-ledger rows')
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET '+field+'=? WHERE id=?', (original[field],job))

    def test_new_source_and_unknown_profile_rows_refuse_even_when_unreported(self):
        source_profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        self.ready()
        self.complete()
        job = self.add_job(4)
        for profile, model in [(source_profile, transition.SOURCE['model']), ('b'*64, transition.TARGET['model'])]:
            with self.subTest(profile=profile):
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET profile_sha256=?,requested_model=? WHERE id=?', (profile,model,job))
                self.assert_refused('unaudited provider job')

    def test_foreign_claim_refuses(self):
        self.ready()
        job = self.add_job(4)
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET room_id=? WHERE id=?', ('foreign-room',job))
        self.complete(ids=self.source_jobs+[job])
        self.assert_refused('unknown or foreign')

    def test_unknown_claim_refuses(self):
        self.ready()
        self.complete(ids=self.source_jobs+[format(99,'032x')])
        self.assert_refused('unknown or foreign')

    def test_malformed_claim_refuses(self):
        self.ready()
        self.complete(ids=self.source_jobs+['malformed'])
        self.assert_refused('malformed delegate job')

    def test_current_only_wrong_profile_does_not_gain_a_recovery_lane(self):
        self.ready()
        job = self.add_job(4)
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET requested_model=? WHERE id=?', ('wrong-model',job))
        self.complete(ids=[job])
        self.assert_refused()

    def test_unreported_unsafe_rows_outside_status_window_refuse(self):
        self.ready()
        for number in range(4,30):
            self.add_job(number)
        self.complete()
        job = format(4,'032x')
        shown = self.service.ao_room_status(self.room)['delegate']['jobs']
        self.assertTrue(shown['truncated'])
        self.assertNotIn(job, {row['job_id'] for row in shown['items']})
        for state in (ds.STREAMING, ds.UNKNOWN, ds.ABANDONED_DEADLINE, ds.ABANDONED_CANCELLED, 'unrecognized_state'):
            with self.subTest(state=state):
                with self.ledger.transaction() as db:
                    db.execute('UPDATE jobs SET state=? WHERE id=?', (state,job))
                self.assert_refused()
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET state=? WHERE id=?', (ds.REJECTED,job))

    def test_original_completed_content_still_requires_its_digest_when_unreported(self):
        job = self.add_job(4, source=True, state=ds.COMPLETED, content=b'Original source result')
        self.ready()
        self.complete()
        (self.home/'deepseek/jobs'/job/'content').write_bytes(b'Changed source result')
        self.assert_refused('does not match its recorded digest')

    def test_current_completed_content_still_requires_its_digest(self):
        self.ready()
        job = self.add_job(4, state=ds.COMPLETED, content=b'Current result')
        self.complete(ids=self.source_jobs+[job])
        (self.home/'deepseek/jobs'/job/'content').write_bytes(b'Changed current result')
        self.assert_refused('does not match its recorded digest')

    def test_transition_receipt_and_completion_evidence_cannot_be_changed(self):
        self.ready()
        request = self.complete()
        state = self.state()
        epoch = ao.read(self.directory()/state['provider_transition']['epoch_record'])
        audit = transition._audit_path(self.directory(), epoch['audit_sha256'])
        paths = [audit, self.directory()/state['provider_transition']['epoch_record'],
                 self.directory()/request['receipt'], self.directory()/request['completion_candidate']]
        for path in paths:
            with self.subTest(path=path.name):
                old = path.read_bytes()
                path.write_text('{}')
                self.assert_refused()
                path.write_bytes(old)

    def test_live_completed_result_and_controller_must_remain_intact(self):
        self.ready()
        self.complete()
        original = copy.deepcopy(self.fake.snapshots['engineer'])
        cases = [('controller', 'stopped'), ('controller', None), ('history_truncated',True)]
        for key, value in cases:
            with self.subTest(key=key,value=value):
                self.fake.snapshots['engineer'][key] = value
                self.assert_refused()
                self.fake.snapshots['engineer'] = copy.deepcopy(original)
        self.fake.snapshots['engineer']['messages'][-1]['text'] = 'Different completed response'
        self.assert_refused('unchanged completed native result')
        self.fake.snapshots['engineer'] = copy.deepcopy(original)
        self.fake.snapshots['engineer']['turns'][-1]['state'] = 'interrupted'
        self.assert_refused()

    def test_scope_change_still_requires_new_agreement(self):
        self.ready()
        self.complete(outcome='scope_change')
        self.assert_refused('scope change')

    def test_claimed_complete_result_does_not_gain_partial_report_recovery(self):
        self.ready()
        self.complete(outcome='completed', implementation_complete=True, remaining_gaps=[])
        self.assert_refused('completed partial report')

    def test_completed_admission_metadata_is_receipt_bound_and_not_repeated_in_prompt(self):
        self.ready()
        self.complete()
        self.correct()
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required',
                            implementation_complete=False, remaining_gaps=['Next section'])))
        self.service.ao_room_sync(self.room)
        path = self.directory()/'state.json'
        original = path.read_bytes()
        state = self.state()
        state['requests']['correction']['carried']['correction_admission']['prior_report_sha256'] = 'f'*64
        ao.atomic(path, state)
        posts = copy.deepcopy(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'immutable native receipt'):
            self.correct(key='next-section')
        self.assertEqual(self.fake.posts, posts)
        path.write_bytes(original)
        self.correct(key='next-section')
        request = self.state()['requests']['next-section']
        self.assertEqual(request['text'], 'Continue.')
        self.assertNotIn('correction_admission', request['carried'])


if __name__ == '__main__':
    unittest.main()
