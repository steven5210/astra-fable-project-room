"""Synthetic acceptance continuation contracts; no account, provider or network access."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_acceptance_extension as extension
import ao_executable_binding
import ao_response_normalization as normalization
import project_room
import project_room_mcp
from test_ao_normal import Fixture


class AcceptanceContinuationFixture(Fixture):
    review_count = 3
    last_review_decision = 'rejected'
    spec_review_count = 1
    prior_review_default_purpose = False

    def setUp(self):
        super().setUp()
        original_request = self.fake.request

        def complete_history(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original_request(method, path, payload)

        self.fake.request = complete_history
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.review_repo = self.root / 'review-repo'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(self.review_repo), 'HEAD'],
                       check=True, capture_output=True)
        self.fake.workspaces['reviewer'] = self.review_repo
        self.room = self.open()
        self.spec()
        self.bind()
        for number in range(self.spec_review_count):
            self.agree(key='charter-' + str(number + 1))
        self.implement()
        self.service.ao_room_verify(self.room, str(self.repo))
        for number in range(1, self.review_count + 1):
            key = 'prior-' + str(number)
            if self.prior_review_default_purpose:
                self.service.ao_room_send(self.room, 'reviewer', 'Perform the exact authorized purpose.', key)
            else:
                self.send('acceptance_review', key)
            self.finish_review(key, self.last_review_decision if number == self.review_count else 'rejected')
        self.database = self.root / 'synthetic-owner.db'
        with sqlite3.connect(self.database) as db:
            db.executescript('''CREATE TABLE sessions(id,project_id,harness,session_mode,is_terminated,
              activity_state,workspace_path,provider_conversation_id,controller_generation);
              CREATE TABLE conversations(id,current_session_id,active_branch_id);
              CREATE TABLE conversation_branches(id,conversation_id,provider_conversation_id,session_id,strategy,replay_truncated);''')
            for role, binding in self.state()['bindings'].items():
                native = 'synthetic-native-' + role
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                           (role, 'project', binding['harness'], 'chat', 0, 'idle', str(self.fake.workspaces[role]), native, 'generation'))
                db.execute('INSERT INTO conversations VALUES(?,?,?)',
                           (binding['conversation_id'], role, binding['branch_id']))
                db.execute('INSERT INTO conversation_branches VALUES(?,?,?,?,?,?)',
                           (binding['branch_id'], binding['conversation_id'], native, role, 'native', 0))
        self.message = 'Perform the exact authorized purpose.'

    def finish_review(self, request_id, decision='approved'):
        request = self.state()['requests'][request_id]
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': decision,
                                              'review': 'Synthetic independent behavior verdict.'}))
        return self.service.ao_room_sync(self.room)

    def audit(self, review='fourth'):
        return self.service.ao_room_acceptance_review_audit(self.room, review, ao.digest(self.message.encode()),
                               str(self.database), 'synthetic-native-engineer', 'synthetic-native-reviewer')

    def grant_inputs(self, review='fourth'):
        return dict(room_id=self.room, audit_sha256=self.audit(review)['audit_sha256'],
                    request_id='grant-' + review, authorization='Actual synthetic user approval for needed additional reviews.',
                    authorization_reference='synthetic-user-message', diagnosis='A necessary further independent review in the same scope.')

    def grant(self, review='fourth'):
        inputs = self.grant_inputs(review)
        return inputs, self.service.ao_room_acceptance_review_extend(**inputs)


class AcceptanceContinuationTests(AcceptanceContinuationFixture):
    def test_mcp_and_cli_dispatch_complete_explicit_arguments(self):
        controller = project_room.Service(self.home)
        listed = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, controller)
        names = {tool['name']: tool for tool in listed['result']['tools']}
        for name in ('ao_room_acceptance_review_audit', 'ao_room_acceptance_review_extend'):
            self.assertIn(name, names)
            self.assertFalse(names[name]['annotations']['readOnlyHint'])
        audit_args = dict(room_id=self.room, review_request_id='fourth', message_sha256=ao.digest(self.message.encode()),
                          native_owner_database=str(self.database), engineer_native_session_id='synthetic-native-engineer',
                          reviewer_native_session_id='synthetic-native-reviewer')
        with patch.object(ao, 'Service', return_value=self.service):
            with self.assertRaisesRegex(ao.RoomError, 'Invalid arguments'):
                controller.call('ao_room_acceptance_review_audit', {'room_id': self.room})
            response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'ao_room_acceptance_review_audit', 'arguments': audit_args}}, controller)
            self.assertFalse(response['result']['isError'])
            inputs = dict(room_id=self.room, audit_sha256=response['result']['structuredContent']['audit_sha256'],
                          request_id='grant-fourth', authorization='Actual synthetic user authorization for needed reviews.',
                          authorization_reference='synthetic-message', diagnosis='One necessary further review.')
            granted = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'ao_room_acceptance_review_extend', 'arguments': inputs}}, controller)
            self.assertFalse(granted['result']['isError'])
            args = self.root / 'grant-args.json'
            args.write_text(json.dumps(inputs))
            output = io.StringIO()
            with patch.object(project_room, 'Service', return_value=controller), \
                    patch.object(project_room.signal, 'signal'), redirect_stdout(output):
                result = project_room.main(['--home', str(self.home), 'call', 'ao_room_acceptance_review_extend',
                                            '--args-file', str(args)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), granted['result']['structuredContent'])

    def test_fourth_review_keeps_rejected_receipts_and_requires_approval(self):
        before = self.state()
        receipts = {r['receipt']: (self.directory() / r['receipt']).read_bytes()
                    for r in before['requests'].values()}
        inputs, grant = self.grant()
        self.assertEqual(self.service.ao_room_acceptance_review_extend(**inputs), grant)
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        posts = len(self.fake.posts)
        summary_before = extension.summary(self.service, self.state())
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        # The ordinary rejected-verdict gate refuses here (ao_project_room.py:906-907), not a
        # guard_unused freeze; ao_outcomes.observe (called unconditionally at ao_project_room.py:
        # 892, before this check) legitimately persists an outcome observation, so state.json
        # equality is not asserted -- only the acceptance-review evidence must stay untouched.
        with self.assertRaisesRegex(ao.RoomError, 'Reviewer rejected or did not approve'):
            self.service.ao_room_accept(self.room, 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)
        for path, content in receipts.items():
            self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_new_fifth_grant_after_fourth_is_settled(self):
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        _, fifth = self.grant('fifth')
        self.assertEqual(fifth['review_request_id'], 'fifth')
        self.assertEqual(fifth['remaining_additional_reviews'], 1)
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])

    def test_wrong_message_does_not_consume_or_post(self):
        self.grant()
        posts = len(self.fake.posts)
        summary_before = extension.summary(self.service, self.state())
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ao.RoomError, 'does not match the exact audited caller-message digest'):
            self.service.ao_room_send(self.room, 'reviewer', 'Changed caller bytes.', 'fourth', purpose='acceptance_review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)

    def test_lost_ack_repeat_never_posts_twice(self):
        self.grant()
        self.fake.lose_ack = True
        first = self.send('acceptance_review', 'fourth')
        self.assertEqual(first['state'], 'uncertain')
        posts = len(self.fake.posts)
        self.assertEqual(self.send('acceptance_review', 'fourth'), first)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')

    def test_pending_grant_identical_recovery_preserves_receipt(self):
        inputs = self.grant_inputs()
        def failed_projection(directory, state):
            if state.get(extension.KEY):
                raise OSError('synthetic interruption before state projection')
            return saved(directory, state)
        saved = self.service.save
        with patch.object(self.service, 'save', side_effect=failed_projection):
            with self.assertRaises(OSError):
                self.service.ao_room_acceptance_review_extend(**inputs)
        path = self.directory() / extension.BASE / 'journal' / '000001.json'
        receipt = path.read_bytes()
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'pending_uncommitted')
        self.assertEqual(self.service.ao_room_acceptance_review_extend(**inputs)['remaining_additional_reviews'], 1)
        self.assertEqual(path.read_bytes(), receipt)

    def test_consumption_without_projection_stays_inconsistent_no_post(self):
        self.grant()
        posts = len(self.fake.posts)
        saved = self.service.save
        def failed_projection(directory, state):
            if 'fourth' in state['requests']:
                raise OSError('synthetic interruption after consumption before projection')
            return saved(directory, state)
        with patch.object(self.service, 'save', side_effect=failed_projection):
            with self.assertRaises(OSError):
                self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'inconsistent')
        summary_before = extension.summary(self.service, self.state())
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ao.RoomError, 'is durable but its request projection is missing'):
            self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)

    def test_no_grant_keeps_three_review_cap(self):
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Three review attempts exhausted'):
            self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertIsNone(extension.summary(self.service, self.state()))

    def test_changed_grant_provenance_cannot_reuse_identity(self):
        inputs, _ = self.grant()
        receipt = self.directory() / extension.BASE / 'journal' / '000001.json'
        original = receipt.read_bytes()
        posts = len(self.fake.posts)
        summary_before = extension.summary(self.service, self.state())
        for key in ('authorization', 'authorization_reference', 'diagnosis'):
            with self.subTest(key=key), self.assertRaisesRegex(
                    ao.RoomError, 'already belongs to another payload; it cannot be renewed or renumbered'):
                self.service.ao_room_acceptance_review_extend(**{**inputs, key: 'Changed field.'})
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

    def test_native_provider_replacement_refuses_before_grant(self):
        inputs = self.grant_inputs()
        # grant_inputs() already ran one successful audit, so BASE/audits holds that record;
        # only its journal (never created without a successful extend) must stay absent.
        lane = {str(p.relative_to(self.directory())): p.read_bytes()
                for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='replacement' WHERE id='reviewer'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='replacement' WHERE session_id='reviewer'")
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'native reviewer owner differs from the retained room'):
            self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertFalse((self.directory() / extension.BASE / 'journal').exists())
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, lane)

    def test_stale_unused_grant_is_never_replaced(self):
        self.grant()
        (self.repo / 'changed.txt').write_text('external candidate drift\n')
        posts = len(self.fake.posts)
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        # The candidate-drift send refuses at the ordinary pre-existing checkpoint/candidate
        # freshness gate (ao_project_room.py:591-592, reached from inside admission's own
        # _inspect call), not a bespoke acceptance-review staleness message.
        with self.assertRaisesRegex(ao.RoomError, 'Candidate changed after verification'):
            self.send('acceptance_review', 'fourth')
        # A fresh audit while a grant is unused instead refuses at the acceptance-review
        # lane's own "already exists" gate; it never attempts to replace the stale grant.
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant already exists'):
            self.audit('different-review')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)
        self.assertEqual(extension.summary(self.service, self.state())['remaining_additional_reviews'], 1)

    def test_dropping_projection_cannot_hide_consumed_journal(self):
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        state = self.state()
        state.pop(extension.KEY)
        ao.atomic(self.directory() / 'state.json', state)
        posts = len(self.fake.posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'inconsistent')
        summary_before = extension.summary(self.service, self.state())
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ao.RoomError, 'does not match its projected entries'):
            self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'does not match its projected entries'):
            self.audit('fifth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)

    def test_consumed_grant_allows_correction_then_fresh_fifth_review(self):
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth', 'rejected')
        original = {str(p.relative_to(self.directory())): p.read_bytes()
                    for p in (self.directory() / extension.BASE).rglob('*.json')}
        self.send('correction', 'new-correction')
        (self.repo / 'added.txt').write_text('a legitimate new candidate\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_verify(self.room, str(self.repo))
        self.grant('fifth')
        self.send('acceptance_review', 'fifth')
        self.finish_review('fifth')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fifth')['accepted'])
        for path, content in original.items():
            self.assertEqual((self.directory() / path).read_bytes(), content)

    def test_unused_grant_freezes_mutations_but_identical_spec_remains_readable(self):
        self.grant()
        prior = (self.directory() / 'state.json').read_bytes()
        self.assertEqual(self.spec()['revision'], 1)
        for operation in (lambda: self.spec(2),
                          lambda: self.service.ao_room_verify(self.room, str(self.repo)),
                          lambda: self.send('correction', 'new-correction'),
                          lambda: self.send('spec_review', 'new-spec-review')):
            with self.subTest(operation=operation), self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant'):
                operation()
            self.assertEqual((self.directory() / 'state.json').read_bytes(), prior)
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['acceptance_review_attempts'], 3)
        self.assertEqual(status['acceptance_review_extension']['state'], 'unconsumed')

    def test_missing_authority_and_competing_grants_never_create_an_allowance(self):
        inputs = self.grant_inputs()
        posts = len(self.fake.posts)
        # grant_inputs() already ran one successful audit, so BASE/audits holds that record;
        # only its journal (never created without a successful extend) must stay absent.
        lane = {str(p.relative_to(self.directory())): p.read_bytes()
                for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        fragments = {'authorization': 'authorization: actual user authorization and its approval context '
                                       'must be nonempty text',
                     'authorization_reference': 'authorization_reference: source and provenance reference '
                                                 'must be nonempty text',
                     'diagnosis': '^diagnosis must be nonempty text'}
        for key in ('authorization', 'authorization_reference', 'diagnosis'):
            with self.subTest(key=key), self.assertRaisesRegex(ao.RoomError, fragments[key]):
                self.service.ao_room_acceptance_review_extend(**{**inputs, key: '  '})
        self.assertIsNone(extension.summary(self.service, self.state()))
        self.assertFalse((self.directory() / extension.BASE / 'journal').exists())
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, lane)
        self.service.ao_room_acceptance_review_extend(**inputs)
        summary_before = extension.summary(self.service, self.state())
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant already exists'):
            self.service.ao_room_acceptance_review_extend(**{**inputs, 'request_id': 'competing-grant'})
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state()), summary_before)
        self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                          for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)
        self.assertEqual(len(extension.validate(self.service, self.state())['entries']), 1)

    def test_wrong_request_and_purpose_refuse_and_default_purpose_consumes(self):
        self.grant()
        posts = len(self.fake.posts)
        fragments = {('other-review', 'acceptance_review'): 'Only the exact reviewer request identity named '
                                                              'by the unused grant may be dispatched',
                     ('fourth', 'correction'): 'An additional acceptance review must use the '
                                                'acceptance_review purpose'}
        for request_id, purpose in (('other-review', 'acceptance_review'), ('fourth', 'correction')):
            summary_before = extension.summary(self.service, self.state())
            journal = {str(p.relative_to(self.directory())): p.read_bytes()
                       for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
            with self.subTest(request_id=request_id, purpose=purpose), self.assertRaisesRegex(
                    ao.RoomError, fragments[(request_id, purpose)]):
                self.service.ao_room_send(self.room, 'reviewer', self.message, request_id, purpose=purpose)
            self.assertEqual(len(self.fake.posts), posts)
            self.assertEqual(extension.summary(self.service, self.state()), summary_before)
            self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                              for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)
            self.assertEqual(extension.summary(self.service, self.state())['remaining_additional_reviews'], 1)
        self.service.ao_room_send(self.room, 'reviewer', self.message, 'fourth')
        self.assertEqual(self.state()['requests']['fourth']['purpose'], 'acceptance_review')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_exact_native_owner_survives_controller_generation_change(self):
        inputs = self.grant_inputs()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET controller_generation='next-generation'")
        self.assertEqual(self.service.ao_room_acceptance_review_extend(**inputs)['remaining_additional_reviews'], 1)

    def test_stale_audit_rejects_each_immutable_evidence_change(self):
        inputs = self.grant_inputs()
        state = self.state()
        checkpoint = self.service.checkpoint(self.directory(), state)
        # gate log drift refuses at the ordinary pre-existing checkpoint-integrity gate
        # (ao_project_room.py:589-590, reached while extend recomputes eligibility), not the
        # acceptance-review lane's own "audit is stale" message; every other immutable-evidence
        # path is caught by that lane-specific staleness comparison (ao_acceptance_extension.py:667).
        paths = [(state['spec'], 'The acceptance-review audit is stale'),
                 (state['checkpoint'], 'The acceptance-review audit is stale'),
                 (checkpoint['gates'][0]['log'], 'Verification evidence was modified'),
                 (state['requests']['implementation']['engineering_record'], 'The acceptance-review audit is stale'),
                 (state['requests']['prior-1']['receipt'], 'The acceptance-review audit is stale')]
        posts = len(self.fake.posts)
        # grant_inputs() already ran one successful audit, so BASE/audits holds that one record;
        # nothing new (no journal, no further audit) may appear alongside it for any of these
        # refused extend() attempts.
        lane = {str(p.relative_to(self.directory())): p.read_bytes()
                for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        for name, fragment in paths:
            path = self.directory() / name
            before = path.read_bytes()
            try:
                path.write_bytes(before + b'\n')
                with self.subTest(path=name), self.assertRaisesRegex(ao.RoomError, fragment):
                    self.service.ao_room_acceptance_review_extend(**inputs)
            finally:
                path.write_bytes(before)
            self.assertFalse((self.directory() / extension.BASE / 'journal').exists())
            self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                              for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, lane)
        self.assertEqual(len(self.fake.posts), posts)

    def test_corrupt_missing_duplicate_and_symlink_journal_records_fail_closed(self):
        self.grant()
        journal = self.directory() / extension.BASE / 'journal'
        path = journal / '000001.json'
        original = path.read_bytes()
        # 'corrupt' and 'symlink' both surface through _load's generic OSError/ValueError
        # catch (ao_acceptance_extension.py:90-94); 'missing' and 'duplicate' instead hit the
        # journal-listing/projection comparisons directly (ao_acceptance_extension.py:202-203
        # and :217-218), so each needs its own fragment even though two pairs coincide.
        fragments = {'corrupt': 'evidence is unreadable or inconsistent',
                     'missing': 'does not match its projected entries',
                     'duplicate': 'unknown, renamed or reordered record',
                     'symlink': 'evidence is unreadable or inconsistent'}
        for change in ('corrupt', 'missing', 'duplicate', 'symlink'):
            other = journal / '000009.json'
            posts = len(self.fake.posts)
            try:
                if change == 'corrupt':
                    path.write_text('{')
                elif change == 'missing':
                    path.unlink()
                elif change == 'duplicate':
                    other.write_bytes(original)
                else:
                    path.unlink()
                    other.write_bytes(original)
                    path.symlink_to(other)
                self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'], 'inconsistent')
                with self.subTest(change=change), self.assertRaisesRegex(ao.RoomError, fragments[change]):
                    self.service.ao_room_sync(self.room)
                self.assertEqual(len(self.fake.posts), posts)
            finally:
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(original)
                if other.exists():
                    other.unlink()

    def test_fifo_journal_evidence_refuses_without_blocking_status(self):
        self.grant()
        path = self.directory() / extension.BASE / 'journal' / '000001.json'
        path.unlink()
        os.mkfifo(path)
        program = ('import sys; from pathlib import Path; from ao_project_room import Service; '
                   "status=Service(Path(sys.argv[1])).ao_room_status(sys.argv[2]); "
                   "print(status['acceptance_review_extension']['state'])")
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.home), self.room],
                                cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'inconsistent')

    def test_removed_attempt_and_forged_linkage_cannot_change_allowance(self):
        self.grant()
        original = self.state()
        journal = {str(p.relative_to(self.directory())): p.read_bytes()
                   for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}
        # 'remove' and 'role' both shrink the counted retained-reviewer set, so both trip the
        # same reviewer-attempt-accounting check (ao_acceptance_extension.py:293-294); only the
        # forged linkage itself is caught by the dedicated linkage-integrity gate (:312-313).
        fragments = {'remove': 'Reviewer attempt accounting does not match',
                     'role': 'Reviewer attempt accounting does not match',
                     'linkage': 'is forged, detached or modified'}
        for change in ('remove', 'role', 'linkage'):
            state = copy.deepcopy(original)
            if change == 'remove':
                del state['requests']['prior-1']
            elif change == 'role':
                state['requests']['prior-1']['role'] = 'engineer'
            else:
                state['requests']['prior-1']['acceptance_review_grant'] = {'request_id': 'forged', 'sha256': '0' * 64}
            try:
                ao.atomic(self.directory() / 'state.json', state)
                posts = len(self.fake.posts)
                with self.subTest(change=change), self.assertRaisesRegex(ao.RoomError, fragments[change]):
                    self.send('acceptance_review', 'fourth')
                self.assertEqual(len(self.fake.posts), posts)
                self.assertEqual({str(p.relative_to(self.directory())): p.read_bytes()
                                  for p in (self.directory() / extension.BASE).rglob('*') if p.is_file()}, journal)
            finally:
                ao.atomic(self.directory() / 'state.json', original)

    def test_crash_after_projection_before_post_keeps_consumed_known_request(self):
        self.grant()
        posts = len(self.fake.posts)
        saved = self.service.save
        def interrupted(directory, state):
            saved(directory, state)
            if 'fourth' in state['requests']:
                raise OSError('synthetic failure after request projection before POST')
        with patch.object(self.service, 'save', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.send('acceptance_review', 'fourth')
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.assertEqual(self.send('acceptance_review', 'fourth')['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts)

    def test_late_native_quota_does_not_consume_or_dispatch(self):
        self.grant()
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit', 'httpStatus': 429}
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'quota_limit'):
            self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        self.assertNotIn('fourth', self.state()['requests'])

    def test_pending_operator_work_prevents_audit(self):
        self.send('correction', 'needs-operator')
        self.fake.finish('engineer', json.dumps(self.report(operator_requests=[{
            'task': 'Run the synthetic pending gate.', 'inputs': ['Synthetic candidate'],
            'verification': ['Save the gate output.']}])))
        self.service.ao_room_sync(self.room)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'operator'):
            self.audit()
        self.assertEqual(len(self.fake.posts), posts)

    def test_busy_retained_reviewer_cannot_receive_a_grant(self):
        self.fake.request('POST', '/sessions/reviewer/conversation/messages',
                          {'text': 'Synthetic externally started work.', 'clientMessageId': 'external-work'})
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'bound AO conversation has active work'):
            self.audit()
        self.assertEqual(len(self.fake.posts), posts)
        self.assertFalse((self.directory() / extension.BASE).exists())

    def test_incomplete_native_history_prevents_audit(self):
        original = self.fake.request
        def incomplete(method, path, payload=None):
            value = original(method, path, payload)
            if method == 'GET' and '/conversation?' in path:
                value = {**value, 'hasMoreBefore': True, 'nextBefore': None}
            return value
        self.fake.request = incomplete
        posts = len(self.fake.posts)
        # Raised from the low-level pagination-follower (ao_history.py:110), not
        # ao_acceptance_extension.py itself; the audit's own eligibility gate never runs.
        with self.assertRaisesRegex(ao.RoomError, 'Native history pagination is incomplete or ambiguous'):
            self.audit()
        self.assertEqual(len(self.fake.posts), posts)
        self.assertFalse((self.directory() / extension.BASE).exists())

    def test_provider_and_routing_changes_cannot_invalidate_unused_grant(self):
        self.grant()
        before = {str(p.relative_to(self.directory())): p.read_bytes()
                  for p in self.directory().rglob('*') if p.is_file()}
        for operation in (lambda: self.service.ao_room_provider_transition_audit(self.room, {}),
                          lambda: self.service.ao_room_routing_adoption_audit(self.room)):
            with self.subTest(operation=operation), self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant'):
                operation()
            after = {str(p.relative_to(self.directory())): p.read_bytes()
                     for p in self.directory().rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_readonly_extra_review_tolerates_unrelated_live_delegation_drift(self):
        definitions = self.repo / '.claude/agents/pr-sonnet.md'
        definitions.write_text(definitions.read_text() + '\nSynthetic unrelated live configuration drift.\n')
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fourth')['accepted'])

    def test_substituted_reviewer_model_or_effort_refuses_before_grant(self):
        settings = self.fake.snapshots['reviewer']['settings']
        original = copy.deepcopy(settings)
        # Both fields refuse at the ordinary, pre-existing binding-freshness check inside
        # Service.identity (ao_project_room.py:319-322), reached while _inspect reads the
        # reviewer's live snapshot -- not the acceptance-review lane's own "reviewer cannot
        # be replaced" message (ao_acceptance_extension.py:440-441), which only fires for a
        # rebound identity, not a live in-place settings drift on the still-bound session.
        for field, value in (('model', 'other-model'), ('reasoningEffort', 'high')):
            try:
                settings[field] = value
                posts = len(self.fake.posts)
                with self.subTest(field=field), self.assertRaisesRegex(
                        ao.RoomError, 'AO configured model/effort changed or is unavailable'):
                    self.audit()
                self.assertEqual(len(self.fake.posts), posts)
                self.assertFalse((self.directory() / extension.BASE).exists())
            finally:
                settings.clear()
                settings.update(original)

class PrematureAcceptanceContinuationTests(AcceptanceContinuationFixture):
    review_count = 2

    def test_cannot_bank_grant_before_third_attempt(self):
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'before the ordinary three'):
            self.audit()
        self.assertEqual(len(self.fake.posts), posts)
        self.assertIsNone(extension.summary(self.service, self.state()))


class AcceptanceRecordContinuationTests(AcceptanceContinuationFixture):
    last_review_decision = 'approved'

    def test_unused_grant_blocks_new_acceptance_record(self):
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'a new acceptance'):
            self.service.ao_room_accept(self.room, 'prior-3')
        self.assertEqual(self.state()['acceptances'], [])

    def test_unused_grant_allows_identical_saved_acceptance(self):
        accepted = self.service.ao_room_accept(self.room, 'prior-3')
        self.grant()
        self.assertEqual(self.service.ao_room_accept(self.room, 'prior-3'), accepted)


class CharterAndAcceptanceContinuationTests(AcceptanceContinuationFixture):
    spec_review_count = 3
    last_review_decision = 'approved'

    def test_consumed_fourth_charter_and_acceptance_grants_preserve_both_histories(self):
        accepted = self.service.ao_room_accept(self.room, 'prior-3')
        spec = self.spec(2)
        audit = self.service.ao_room_spec_review_extension_audit(self.room, 2, spec['sha256'],
                    accepted['candidate_sha256'], 'synthetic-native-engineer', str(self.database))
        self.service.ao_room_spec_review_extend(self.room, audit['audit_sha256'],
                    'Actual synthetic user approval of one further charter review.',
                    'The approved next charter needs its one additional review.', 'grant-fourth-charter')
        self.agree(revision=2, key='fourth-charter')
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'second-charter-implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_verify(self.room, str(self.repo))
        original = self.state()
        self.grant()
        self.send('acceptance_review', 'fourth')
        self.finish_review('fourth')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fourth')['accepted'])
        current = self.state()
        self.assertEqual(current['spec_review_extension'], original['spec_review_extension'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.service.ao_room_status(self.room)['acceptance_review_extension']['state'], 'consumed')


class AcceptanceFreezeListProbeTests(AcceptanceContinuationFixture):
    """Freeze-list probes for guard_unused/guard_outcome_resume call sites reachable through public
    Service entry points with this file's existing fixtures. New specification, verification, engineer
    correction dispatch, spec_review dispatch, new acceptance record, provider-transition audit and
    routing-adoption audit are already covered elsewhere in this file and in
    test_ao_acceptance_extension_compat.py; these probes cover the remaining listed freeze operations."""

    def tree(self):
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}

    def test_response_normalization_freezes_new_but_permits_identical_saved_read(self):
        # normalize() reads an already-saved result through its early-return branch
        # (ao_response_normalization.py:218-226) and never calls guard_unused at all; a genuinely new
        # normalization instead falls through to guard_unused (line 228), which fires before the
        # "already strict JSON" check (lines 230-234). Build the saved record BEFORE granting -- sending
        # while a grant is unused is itself refused (see
        # test_unused_grant_freezes_mutations_but_identical_spec_remains_readable above).
        self.send('correction', 'prose-correction')
        prefix = 'Some prose preamble that is not itself JSON.\n'
        self.fake.finish('engineer', prefix + json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        saved_request = self.state()['requests']['prose-correction']
        saved_raw, saved_identity = normalization.source(self.directory(), saved_request)
        saved_args = dict(room_id=self.room, request_id='prose-correction', receipt_sha256=saved_identity['receipt_sha256'],
                           final_text_sha256=saved_identity['final_text_sha256'], json_start=len(prefix), json_end=len(saved_raw),
                           astra_review='Inspected the entire raw reply; the preamble adds no contradictory verdict.',
                           confirm_no_additional_verdict=True)
        first = self.service.ao_room_response_normalize(**saved_args)

        self.grant()
        new_request = self.state()['requests']['implementation']
        new_raw, new_identity = normalization.source(self.directory(), new_request)
        new_args = dict(room_id=self.room, request_id='implementation', receipt_sha256=new_identity['receipt_sha256'],
                         final_text_sha256=new_identity['final_text_sha256'], json_start=0, json_end=len(new_raw),
                         astra_review='Inspected the entire raw reply; no additional verdict outside the JSON.',
                         confirm_no_additional_verdict=True)
        state_before = (self.directory() / 'state.json').read_bytes()
        tree_before = self.tree()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant'):
            self.service.ao_room_response_normalize(**new_args)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_before)
        self.assertEqual(self.tree(), tree_before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        # The identical, already-saved normalization stays readable while the grant is still unused,
        # because it never reaches guard_unused.
        self.assertEqual(self.service.ao_room_response_normalize(**saved_args), first)
        self.assertEqual(len(self.fake.posts), posts)

        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_outcome_resume_freezes_unless_naming_the_grants_exact_review_id(self):
        # ao_outcomes.resume() calls guard_outcome_resume (ao_outcomes.py:367) after its own early-return
        # checks but before observe() ever writes. guard_outcome_resume (ao_acceptance_extension.py:710-717)
        # passes only for a reviewer continuation naming exactly the grant's own review_request_id; the
        # engineer's own latest request, and a reviewer naming any other successor, are both frozen first.
        # outcome_sha256 is never validated before this guard, so a dummy hex value is enough to reach it.
        self.grant()
        state_before = (self.directory() / 'state.json').read_bytes()
        tree_before = self.tree()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant'):
            self.service.ao_room_outcome_resume(self.room, 'implementation', 'a' * 64, 'engineer-resume',
                                                'Synthetic diagnosis', 'Synthetic authorization')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_before)
        self.assertEqual(self.tree(), tree_before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        with self.assertRaisesRegex(ao.RoomError, 'unused acceptance-review grant'):
            self.service.ao_room_outcome_resume(self.room, 'prior-3', 'a' * 64, 'wrong-successor',
                                                'Synthetic diagnosis', 'Synthetic authorization')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_before)
        self.assertEqual(self.tree(), tree_before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        # Naming exactly the grant's own review_request_id ('fourth') gets past the freeze guard; the
        # dummy outcome_sha256 then fails the ordinary, unrelated evidence check instead -- not the freeze
        # message -- and nothing is consumed. observe() legitimately writes on this path (as it does for
        # every ordinary outcome observation elsewhere in this file), so state.json equality is not
        # asserted for this sub-case.
        with self.assertRaisesRegex(ao.RoomError,
                                     'Outcome evidence changed or does not establish an eligible diagnosed failure'):
            self.service.ao_room_outcome_resume(self.room, 'prior-3', 'a' * 64, 'fourth',
                                                'Synthetic diagnosis', 'Synthetic authorization')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('fourth', self.state()['requests'])
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_executable_binding_repair_is_unreachable_before_source_failure_gate(self):
        # ao_executable_binding.bind has no Service or MCP entry point: no ao_room_* method references it
        # (confirmed by grep across ao_project_room.py, project_room.py's TOOL_SCHEMAS and
        # project_room_mcp.py) and the only non-test callers of this module (ao_acceptance_extension.py,
        # ao_review_extension.py, ao_routing.py, ao_routing_refresh.py) use _chain/_read/effective, never
        # bind. It is reachable only as a direct Python function call, which is what this probe does.
        # Even called directly, its own guard_unused call (ao_executable_binding.py:230) is unreachable
        # with this fixture: _inspect() (line 161) calls _source_failure(source) (line 135), which raises
        # at line 137 whenever routing has no previously recorded successful identity. This fixture's
        # preparation (test_ao_normal.Fixture.bind) never configures a working claude_bin, so
        # ao_routing.claude_evidence() always yields that failing shape, identically whether or not an
        # acceptance-review grant is unused (other suites, e.g. test_ao_routing_refresh.py, configure a
        # working claude_bin first and do reach guard_unused, but that setup lives in a different fixture
        # this task may not touch). This probe records the actual, non-freeze refusal in both grant states
        # rather than bending the test to assert an unreachable gate.
        posts = len(self.fake.posts)
        state_before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'Executable repair requires a previously recorded successful identity'):
            ao_executable_binding.bind(self.service, self.room, 'repair-attempt',
                                        str(self.root / 'synthetic-new-claude'), str(self.root / 'synthetic-launch.sh'),
                                        str(self.database), 'Synthetic authorization', 'Synthetic diagnosis')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_before)
        self.assertEqual(len(self.fake.posts), posts)

        self.grant()
        state_before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'Executable repair requires a previously recorded successful identity'):
            ao_executable_binding.bind(self.service, self.room, 'repair-attempt',
                                        str(self.root / 'synthetic-new-claude'), str(self.root / 'synthetic-launch.sh'),
                                        str(self.database), 'Synthetic authorization', 'Synthetic diagnosis')
        self.assertEqual((self.directory() / 'state.json').read_bytes(), state_before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        self.send('acceptance_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
