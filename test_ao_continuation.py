"""Retained-session continuations and delegate evidence: synthetic AO/SQLite only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ao_delegates
import ao_project_room as ao
import ao_workflow
import deepseek_adapter as ds
from test_ao_normal import Fixture, DelegateFixture


class ContinuationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()
        self.bind()

    def ready(self):
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))

    def message(self, key, purpose='correction', text='Continue.'):
        return self.service.ao_room_send(self.room, 'engineer', text, key, purpose=purpose)

    def finish(self, text=None):
        self.fake.finish('engineer', text if text is not None else json.dumps(self.report(
            outcome='changes_required', implementation_complete=False, remaining_gaps=['Unfinished work'])))
        self.service.ao_room_sync(self.room)

    def begin(self):
        self.ready()
        self.message('implementation', 'implementation')
        self.finish()

    def test_initial_spec_and_workflow_once_then_exact_caller_bytes(self):
        self.ready()
        initial = self.state()['requests']['spec_review']
        spec = self.service.spec(self.directory(), self.state())
        self.assertIn(spec['content'], initial['text'])
        self.assertIn(spec['sha256'], initial['text'])
        self.assertEqual(initial['carried']['parts'], list(ao_workflow.PARTS))
        text = 'Implement the agreed plan.\n\nKeep these exact bytes.  '
        self.message('implementation', 'implementation', text)
        self.assertEqual(self.fake.posts[-1][1]['text'], text)
        self.assertEqual(self.state()['requests']['implementation']['text'], text)

    def test_three_identical_corrections_settle_as_distinct_turns(self):
        self.begin()
        previous = copy.deepcopy(self.state()['requests'])
        ids = []
        for index in range(3):
            key = 'continue-' + str(index)
            self.message(key)
            self.finish()
            req = self.state()['requests'][key]
            self.assertEqual(req['state'], 'completed')
            self.assertEqual(req['text'], 'Continue.')
            ids.append(req['provider_turn_id'])
        self.assertEqual(len(set(ids)), 3)
        for key, value in previous.items():
            self.assertEqual(self.state()['requests'][key], value)

    def test_restart_compaction_and_completed_quota_need_no_reanchor(self):
        self.begin()
        before = copy.deepcopy(self.state())
        self.fake.snapshots['engineer']['compaction'] = {'state': 'completed', 'automatic': True}
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.message('after-restart')
        self.finish('You have hit your usage limit. Synthetic known-completed turn.')
        self.message('after-quota')
        self.finish()
        state = self.state()
        for key in ('bindings', 'spec_record_sha256', 'handoff_sha256', 'authorization', 'delegate'):
            self.assertEqual(state[key], before[key])
        for key in ('after-restart', 'after-quota'):
            self.assertEqual(state['requests'][key]['text'], 'Continue.')
            self.assertEqual(state['requests'][key]['state'], 'completed')

    def test_duplicate_and_changed_payload_after_restart_do_not_send(self):
        self.begin()
        first = self.message('same-id')
        posts = len(self.fake.posts)
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.assertEqual(first, self.message('same-id'))
        with self.assertRaisesRegex(ao.RoomError, 'another payload'):
            self.message('same-id', text='New requirement')
        self.assertEqual(len(self.fake.posts), posts)

    def test_lost_ack_reconciles_same_text_without_replay(self):
        self.begin()
        self.message('prior'); self.finish()
        self.fake.lose_ack = True
        self.assertEqual(self.message('lost')['state'], 'uncertain')
        self.finish()
        self.assertEqual(self.state()['requests']['lost']['state'], 'completed')
        posts = len(self.fake.posts)
        self.assertEqual(self.message('lost')['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts)

    def test_ambiguous_lost_ack_stays_blocked(self):
        self.begin()
        self.fake.lose_ack = True
        self.message('lost')
        snapshot = self.fake.snapshots['engineer']
        other = copy.deepcopy(snapshot['messages'][-1])
        other.update(id='other-message', turnId='other-turn')
        snapshot['messages'].append(other)
        snapshot['turns'].append({'id': 'other-turn', 'providerTurnId': 'other-native', 'state': 'completed'})
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['lost']['state'], 'uncertain')
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.message('no-replay')

    def test_acknowledged_turn_cannot_attach_to_other_identical_turn(self):
        self.begin()
        self.message('ack')
        self.fake.snapshots['engineer']['messages'][-1]['turnId'] = 'foreign-turn'
        self.finish()
        self.assertNotEqual(self.state()['requests']['ack']['state'], 'completed')
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.message('no-replay')

    def test_interrupted_unknown_and_recovered_turns_cannot_continue(self):
        for native_state in ('interrupted', 'failed', 'recovered'):
            with self.subTest(state=native_state):
                self.setUp()
                self.ready()
                self.message('implementation', 'implementation')
                self.fake.finish('engineer', 'Partial', state=native_state)
                self.service.ao_room_sync(self.room)
                before = copy.deepcopy(self.state())
                posts = len(self.fake.posts)
                with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
                    self.message('forbidden')
                self.assertEqual(self.state(), before)
                self.assertEqual(len(self.fake.posts), posts)

    def test_revision_is_changes_only_even_when_diff_is_larger(self):
        self.agree()
        old = self.service.spec(self.directory(), self.state())
        self.service.ao_room_spec_put(self.room, 2, 'X', self.gates, 'Changed requirement approved')
        status = self.agree(revision=2, key='revision-2')
        req = self.state()['requests']['revision-2']
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(req['carried']['spec_delivery'], 'changes')
        self.assertIn('-' + old['content'], req['text'])
        self.assertIn('+X', req['text'])
        self.assertNotIn('<specification>', req['text'])
        self.assertNotIn('Agreed gates:', req['text'])
        self.assertGreater(len(req['text']), len('X'))
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.message('implementation', 'implementation')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')

    def test_revision_omits_unchanged_surrounding_requirements(self):
        self.agree()
        self.service.ao_room_spec_put(self.room, 2, 'UNCHANGED FIRST\nold\nUNCHANGED LAST\n', self.gates, 'Approved')
        self.agree(revision=2, key='rev2')
        self.service.ao_room_spec_put(self.room, 3, 'UNCHANGED FIRST\nnew\nUNCHANGED LAST\n', self.gates, 'Approved')
        self.agree(revision=3, key='rev3')
        text = self.state()['requests']['rev3']['text']
        self.assertIn('-old\n+new\n', text)
        self.assertNotIn('UNCHANGED', text)

    def test_same_spec_review_and_caller_missing_context_clarification(self):
        self.agree()
        self.agree(key='review-again')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Perform the exact authorized purpose.')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.message('implementation', 'implementation'); self.finish('I need the missing timeout requirement.')
        clarification = 'The newly clarified timeout applies only to HTTP observation.'
        self.message('clarification', text=clarification)
        self.assertEqual(self.fake.posts[-1][1]['text'], clarification)

    def test_new_gates_are_delivered_only_when_changed(self):
        self.agree()
        gates = self.gates + [['git', 'diff', '--check']]
        self.service.ao_room_spec_put(self.room, 2, 'Implement the exact test contract.', gates, 'Approved gate change')
        self.agree(revision=2, key='new-gates')
        text = self.fake.posts[-1][1]['text']
        self.assertIn('No specification text changes.', text)
        self.assertIn(json.dumps(gates), text)
        self.assertNotIn('<specification>', text)

    def test_changed_final_newline_is_unambiguous(self):
        delta = ao_workflow.spec_changes({'revision': 1, 'content': 'x'}, {'revision': 2, 'content': 'x\n'})
        self.assertEqual(delta, '--- revision 1\n+++ revision 2\n@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+x\n')

    def test_unicode_separators_and_bare_cr_do_not_create_fake_diff_lines(self):
        for separator in ('\u2028', '\u2029', '\r'):
            with self.subTest(separator=repr(separator)):
                old, new = 'a' + separator + 'old\n', 'a' + separator + 'new\n'
                delta = ao_workflow.spec_changes({'revision': 1, 'content': old}, {'revision': 2, 'content': new})
                self.assertEqual(delta, '--- revision 1\n+++ revision 2\n@@ -1 +1 @@\n-' + old + '+' + new)

    def test_repetitive_spec_delta_is_small_and_applies_to_exact_new_bytes(self):
        for count in (400, 40000):
            with self.subTest(lines=count):
                old = 'x\n' * count
                new = 'x\n' * (count // 4) + 'y\n' + 'x\n' * (count // 2 - 1) + 'z\n' + 'x\n' * (count // 4 - 1)
                with patch.dict(os.environ, {'GIT_DIFF_OPTS': '--unified=99999'}):
                    delta = ao_workflow.spec_changes({'revision': 1, 'content': old}, {'revision': 2, 'content': new})
                # Both edits stay bounded even when popular lines surround and separate them. Hunk section names
                # must not leak unchanged text. Apply the patch as independent ground truth, not a string mirror.
                self.assertLess(len(delta), 200)
                self.assertTrue(all(line.endswith(' @@') for line in delta.split('\n') if line.startswith('@@ ')))
                target = self.repo / 'spec.txt'
                target.write_bytes(old.encode())
                body = delta.split('\n', 2)[2]
                applied = subprocess.run(['git', 'apply', '--unidiff-zero', '-'], cwd=self.repo,
                                         input=('--- a/spec.txt\n+++ b/spec.txt\n' + body).encode(), capture_output=True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual(target.read_bytes(), new.encode())

    def test_delta_failure_refuses_without_full_spec_fallback(self):
        before = {'revision': 1, 'content': 'old\n'}
        after = {'revision': 2, 'content': 'new\n'}
        for result in (OSError('git unavailable'), subprocess.TimeoutExpired('git', 30)):
            with self.subTest(error=type(result).__name__), patch('ao_workflow.subprocess.run', side_effect=result):
                with self.assertRaisesRegex(ao.RoomError, 'Cannot compute specification changes'):
                    ao_workflow.spec_changes(before, after)

    def test_git_configuration_cannot_add_context_between_close_edits(self):
        old = 'before\nold first\nkeep alpha\nkeep beta\nold second\nafter\n'
        new = old.replace('old', 'new')
        run = subprocess.run
        def configured(argv, **kwargs):
            # Real Git config precedence without changing the user's environment or global Git files.
            return run([argv[0], '-c', 'diff.interHunkContext=5', '-c', 'diff.algorithm=histogram', *argv[1:]], **kwargs)
        with patch('ao_workflow.subprocess.run', side_effect=configured):
            delta = ao_workflow.spec_changes({'revision': 1, 'content': old}, {'revision': 2, 'content': new})
        self.assertNotIn('keep alpha', delta)
        self.assertNotIn('keep beta', delta)
        self.assertFalse(any(line.startswith(' ') for line in delta.split('\n')))
        target = self.repo / 'spec.txt'; target.write_bytes(old.encode())
        body = delta.split('\n', 2)[2]
        result = run(['git', 'apply', '--unidiff-zero', '-'], cwd=self.repo,
                     input=('--- a/spec.txt\n+++ b/spec.txt\n' + body).encode(), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), new.encode())

    def test_unexpected_git_context_refuses_instead_of_resending_requirements(self):
        result = subprocess.CompletedProcess([], 1, stdout=b'header\n@@ -1,2 +1,2 @@\n-old\n+new\n unchanged\n')
        with patch('ao_workflow.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ao.RoomError, 'unexpectedly contains unchanged context'):
                ao_workflow.spec_changes({'revision': 1, 'content': 'old\nunchanged\n'},
                                         {'revision': 2, 'content': 'new\nunchanged\n'})

    def test_first_implementation_rejects_head_drift_before_any_post(self):
        self.ready()
        subprocess.run(['git', '-C', str(self.repo), 'commit', '--allow-empty', '-m', 'External commit'], check=True, capture_output=True)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'HEAD changed since handoff'):
            self.message('implementation', 'implementation')
        self.assertEqual(len(self.fake.posts), posts)

    def test_malformed_routing_shape_can_receive_report_correction(self):
        self.ready(); self.message('implementation', 'implementation')
        for index, report in enumerate(({'routing_log': None}, {'routing_log': 42}, {'routing_log': [{'delegate_job_ids': None}]})):
            self.finish(json.dumps(report))
            self.message('report-fix-' + str(index))
            self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')

    def historical_review(self):
        self.send('spec_review')
        state = self.state(); req = state['requests']['spec_review']
        spec = self.service.spec(self.directory(), state)
        req.pop('carried')
        req['text'] = ('[Project Room historical request spec_review]\nWorkflow: Fable engineering with independent Astra acceptance.\n'
                       'Fable owns engineering interpretation. Review this exact specification read-only, without implementation or delegates.\n'
                       'Exact specification revision 1, SHA256 ' + spec['sha256'] + '\n<specification>\n' + spec['content']
                       + '\n</specification>\nAgreed gates: ' + json.dumps(spec['gates']) + '\nTask instruction:\nReview.')
        req['text_sha256'] = ao.digest(req['text'].encode())
        self.service.save(self.directory(), state)
        self.fake.snapshots['engineer']['messages'][-1]['text'] = req['text']
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact scope', 'findings': [], 'decision': 'accept',
                                              'spec_revision': 1, 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(self.room)

    def test_existing_room_gets_only_undelivered_workflow_parts_once(self):
        self.historical_review()
        old = copy.deepcopy(self.state()['requests']['spec_review'])
        receipt = self.directory() / old['receipt']; original = receipt.read_bytes()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.message('implementation', 'implementation')
        text = self.fake.posts[-1][1]['text']
        self.assertNotIn('Implement the exact test contract.', text)
        self.assertNotIn('Specification review turns:', text)
        self.assertIn('Implementation and correction turns:', text)
        self.finish()
        self.message('later')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
        self.assertEqual(self.state()['requests']['spec_review'], old)
        self.assertEqual(receipt.read_bytes(), original)

    def test_historical_implementation_and_correction_continue_without_repeating_spec_or_policy(self):
        self.historical_review()
        self.service.ao_room_handoff(self.room, str(self.repo))
        for purpose in ('implementation', 'correction'):
            self.message('old-' + purpose, purpose)
            state = self.state(); req = state['requests']['old-' + purpose]
            spec = self.service.spec(self.directory(), state)
            req.pop('carried')
            # The canonical substring is the pre-upgrade implementation/correction template, also verified
            # against a retained real implementation packet during review. Other old instructions stay opaque.
            req['text'] = ('[Project Room historical request ' + purpose + ']\nHistorical workflow/policy/report/settings/routing.\n'
                           'Exact specification revision 1, SHA256 ' + spec['sha256'] + '\n<specification>\n' + spec['content']
                           + '\n</specification>\nAgreed gates: ' + json.dumps(spec['gates']) + '\nTask instruction:\nContinue.')
            req['text_sha256'] = ao.digest(req['text'].encode())
            self.service.save(self.directory(), state)
            self.fake.snapshots['engineer']['messages'][-1]['text'] = req['text']
            self.finish()
        previous = copy.deepcopy(self.state()['requests'])
        receipts = {r['receipt']: (self.directory() / r['receipt']).read_bytes() for r in previous.values()}
        self.message('new-correction')
        text = self.fake.posts[-1][1]['text']
        self.assertIn('baseline_commit in the engineering report', text)
        self.assertNotIn('Exact specification', text)
        self.assertNotIn('Pinned delegate settings', text)
        self.assertNotIn('Implementation and correction turns:', text)
        self.finish(); self.message('new-next')
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
        for key, record in previous.items(): self.assertEqual(self.state()['requests'][key], record)
        for name, raw in receipts.items(): self.assertEqual((self.directory() / name).read_bytes(), raw)

    def test_modified_delivery_notes_and_receipts_refuse_without_post(self):
        self.ready()
        state = self.state(); original = copy.deepcopy(state)
        for mutate in ('notes', 'text', 'receipt'):
            with self.subTest(mutate=mutate):
                state = copy.deepcopy(original)
                req = state['requests']['spec_review']
                path = self.directory() / req['receipt']; original_bytes = path.read_bytes()
                if mutate == 'notes':
                    req['carried']['parts'].remove('policy'); req['carried']['part_sha256'].pop('policy')
                elif mutate == 'text':
                    req['text'] += '\nFabricated delivery'
                else:
                    path.write_text('{}')
                self.service.save(self.directory(), state)
                posts = len(self.fake.posts)
                with self.assertRaises(ao.RoomError):
                    self.message('implementation', 'implementation')
                self.assertEqual(len(self.fake.posts), posts)
                path.write_bytes(original_bytes)
        self.service.save(self.directory(), original)

    def test_canonical_and_native_drift_still_refuse_short_message(self):
        self.begin()
        spec_path = self.directory() / self.state()['spec']
        original = spec_path.read_bytes()
        modified = json.loads(original); modified['content'] += '\nUnauthorized change'
        spec_path.write_text(json.dumps(modified))
        with self.assertRaises(ao.RoomError): self.message('bad-spec')
        spec_path.write_bytes(original)
        self.fake.snapshots['engineer']['activeBranchId'] = 'different'
        with self.assertRaises(ao.RoomError): self.message('bad-native')


class DelegateEvidenceTests(DelegateFixture):
    JOB = 'a' * 32

    def setUp(self):
        # Reuse only the hermetic provider setup, not its test methods.
        super().setUp()
        self.bind(); self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.ledger = ds.Ledger(self.home)

    def job(self, state=ds.COMPLETED, job_id=None, room=None, profile=None, model=None):
        job_id = job_id or self.JOB
        inventory = self.state()['delegate']['inventory']
        config = ao_delegates.validate_provider(self.directory(), self.state())['delegate_settings']
        output = Path(inventory['export_dir']) / (job_id + '.md')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('Verified synthetic answer\n')
        output.chmod(0o600)
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,reasoning_effort,max_tokens,input_bytes,reserved_bytes,possibly_billed,content_sha256,content_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, room or self.room, 'job-' + job_id, 'deep', 'b'*64,
                        profile or ao_delegates.expected_profile(inventory), state, '2026-01-01T00:00:00Z',
                        model or config['model'], 'enabled', 'max', config['max_tokens'], 100, 0, 1,
                        hashlib.sha256(output.read_bytes()).hexdigest(), str(output)))
        return output

    def capture(self, ids=None, **changes):
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report(routing_log=[{'delegate_job_ids': ids or [self.JOB]}], **changes)))
        self.service.ao_room_sync(self.room)
        return self.state()['requests']['implementation']

    def correction(self):
        return self.service.ao_room_send(self.room, 'engineer', 'Continue.', 'correction', purpose='correction')

    def test_completed_job_verifies_capture_and_acceptance(self):
        self.job(); req = self.capture()
        record = ao.read(self.directory() / req['engineering_record'])
        self.assertEqual(record['delegation'][0]['classification'], 'completed')
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])

    def test_foreign_unknown_and_wrong_profile_refuse_capture_and_continuation(self):
        for kind in ('foreign', 'unknown', 'profile', 'model', 'malformed'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind != 'unknown':
                    self.job(room='foreign' if kind == 'foreign' else None,
                             profile='f'*64 if kind == 'profile' else None, model='other-model' if kind == 'model' else None)
                req = self.capture(ids=['bad-id'] if kind == 'malformed' else None)
                self.assertIn('engineering_error', req)
                self.assertNotIn('engineering_record', req)
                posts = len(self.fake.posts)
                with self.assertRaises(ao.RoomError): self.correction()
                with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
                self.assertEqual(len(self.fake.posts), posts)

    def test_pending_then_completed_job_allows_one_new_correction(self):
        self.job(ds.STREAMING)
        self.capture(outcome='changes_required', implementation_complete=False, remaining_gaps=['Await delegate'])
        posts = len(self.fake.posts)
        for _ in range(3): self.service.ao_room_status(self.room)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'Active or unresolved'): self.correction()
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET state=? WHERE id=?', (ds.COMPLETED, self.JOB))
        old = copy.deepcopy(self.state()['requests']['implementation'])
        result = self.correction()
        self.assertEqual(result, self.correction())
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')
        self.assertEqual(self.state()['requests']['implementation'], old)

    def test_omitted_active_job_still_blocks_entire_ledger(self):
        self.job()
        self.job(ds.UNKNOWN, job_id='0'*32)
        for index in range(22): self.job(ds.REJECTED, job_id=f'{index + 10:032x}')
        self.capture()
        with self.assertRaisesRegex(ao.RoomError, 'Active or unresolved'): self.correction()
        with self.assertRaisesRegex(ao.RoomError, 'Active or unresolved'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_loss_before_report_capture_and_after_capture_refuses(self):
        for after_capture in (False, True):
            with self.subTest(after_capture=after_capture):
                self.setUp(); self.job()
                if after_capture: self.capture()
                path = self.home / 'deepseek' / 'ledger.sqlite3'; path.unlink()
                if not after_capture: self.capture()
                with self.assertRaisesRegex(ao.RoomError, 'ledger is missing'): self.correction()
                with self.assertRaises(ao.RoomError): self.service.ao_room_verify(self.room, str(self.repo))
                self.assertFalse(path.exists())

    def test_launch_evidence_blocks_missing_ledger_without_reported_ids(self):
        ao.atomic(self.directory() / 'delegate-launch.json', {
            'preparation_sha256': self.state()['preparation_sha256'],
            'worktree': str(self.repo), 'session_id': 'engineer'})
        (self.home / 'deepseek' / 'ledger.sqlite3').unlink()
        self.fake.finish('engineer', 'Known completed quota stop, no report')
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'ledger is missing'): self.correction()

    def test_readonly_ledger_does_not_repair_empty_or_corrupt_database(self):
        self.job(); self.capture()
        path = self.home / 'deepseek' / 'ledger.sqlite3'
        for data in (b'', b'not a sqlite database'):
            with self.subTest(data=data):
                path.write_bytes(data)
                with self.assertRaisesRegex(ao.RoomError, 'ledger is unavailable'): self.correction()
                self.assertEqual(path.read_bytes(), data)

    def test_recreated_ledger_cannot_hide_jobs_omitted_by_a_quota_stop(self):
        self.job(ds.STREAMING)
        directory = self.home / 'deepseek' / 'jobs' / self.JOB
        directory.mkdir(parents=True)
        ao.atomic(directory / 'request.json', {'job_id': self.JOB, 'room_id': self.room})
        self.fake.finish('engineer', 'Known completed quota stop; no routing report')
        self.service.ao_room_sync(self.room)
        path = self.home / 'deepseek' / 'ledger.sqlite3'; path.unlink()
        ds.Ledger(self.home)  # A frozen old adapter can still initialize a database during status startup.
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'lost a recorded job'): self.correction()
        with self.assertRaisesRegex(ao.RoomError, 'lost a recorded job'):
            self.service.ao_room_verify(self.room, str(self.repo))
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue((directory / 'request.json').exists())

    def test_other_room_orphan_metadata_does_not_block_this_room(self):
        self.job(); self.capture()
        directory = self.home / 'deepseek' / 'jobs' / ('f'*32)
        directory.mkdir(parents=True)
        ao.atomic(directory / 'request.json', {'job_id': directory.name, 'room_id': 'another-room'})
        self.correction()
        self.assertEqual(self.fake.posts[-1][1]['text'], 'Continue.')

    def test_unreadable_or_inconsistent_orphan_metadata_refuses(self):
        self.job(); self.capture()
        directory = self.home / 'deepseek' / 'jobs' / ('f'*32)
        directory.mkdir(parents=True)
        for record in (None, {'job_id': 'wrong', 'room_id': self.room}):
            with self.subTest(record=record):
                if record is not None: ao.atomic(directory / 'request.json', record)
                with self.assertRaisesRegex(ao.RoomError, 'Orphan delegate job evidence'):
                    self.correction()

    def test_result_tampering_after_capture_blocks_verify_and_continuation(self):
        output = self.job(); self.capture()
        output.write_text('Modified answer')
        with self.assertRaisesRegex(ao.RoomError, 'recorded digest'): self.correction()
        with self.assertRaisesRegex(ao.RoomError, 'recorded digest'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_missing_symlink_and_foreign_export_refuse_capture(self):
        for kind in ('missing', 'symlink', 'foreign'):
            with self.subTest(kind=kind):
                self.setUp(); output = self.job()
                if kind == 'missing': output.unlink()
                if kind == 'symlink':
                    target = output.with_suffix('.txt'); output.rename(target); output.symlink_to(target)
                if kind == 'foreign':
                    with self.ledger.transaction() as db:
                        db.execute('UPDATE jobs SET content_path=? WHERE id=?', (str(self.repo / 'feature.txt'), self.JOB))
                self.assertIn('engineering_error', self.capture())
                with self.assertRaises(ao.RoomError): self.correction()

    def test_rejected_and_resolved_failures_are_never_successful_results(self):
        for state in (ds.REJECTED, ds.TRUNCATED, ds.NOT_STARTED, ds.ABANDONED_DEADLINE, ds.ABANDONED_CANCELLED, ds.FAILED_AFTER_SEND, ds.UNKNOWN):
            with self.subTest(state=state):
                self.setUp(); self.job(state)
                if state in ds.STOP_STATES:
                    req = self.capture()
                    self.assertIn('engineering_error', req)
                    with self.assertRaises(ao.RoomError): self.correction()
                    # Synthetic recorded user resolution only; never a real operator resolution.
                    with self.ledger.transaction() as db:
                        db.execute('INSERT INTO resolutions VALUES(?,?,?,?,?)', (self.JOB, self.room, 'c'*64, 'Synthetic resolution', '2026-01-01'))
                    evidence = ao_delegates.verify_delegation(self.home, self.directory(), self.state(), self.report(routing_log=[{'delegate_job_ids': [self.JOB]}]))
                    self.assertEqual(evidence[0]['classification'], 'resolved_failure')
                else:
                    req = self.capture()
                    evidence = ao.read(self.directory() / req['engineering_record'])['delegation']
                    self.assertEqual(evidence[0]['classification'], 'non_result')
                self.assertIsNone(evidence[0]['content_sha256'])

    def test_unknown_state_refuses_instead_of_becoming_non_result(self):
        self.job('made_up_state')
        self.assertIn('engineering_error', self.capture())
        with self.assertRaisesRegex(ao.RoomError, 'unknown state'): self.correction()


if __name__ == '__main__':
    unittest.main()
