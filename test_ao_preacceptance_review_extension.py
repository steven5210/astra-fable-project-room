"""The pre-acceptance exact-spec review extension: offline synthetic-room contracts.

The one-time, one-ever fourth charter-review allowance has two lanes that share the same
``ao_review_extension`` machinery. ``test_ao_review_extension.py`` covers the original,
implicit ``accepted_candidate`` lane and must stay untouched. This module covers the new
``pre_acceptance`` lane -- granted before any independent acceptance, against an unaccepted,
unverified candidate whose latest completed engineering turn proposed a scope change -- plus the
few places the two lanes interact (authority, audits, grants and the shared receipt).
"""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ao_project_room as ao
import ao_review_extension as extension
import ao_reviewer_recovery as recovery
import ao_workflow
import project_room
import project_room_mcp
from test_ao_normal import Fixture
from test_ao_review_extension import ReviewExtensionFixture


class PreAcceptanceFixture(ReviewExtensionFixture):
    """A normal room through its three agreed charter reviews; no acceptance, no implementation yet.

    The pre-acceptance lane applies only before any independent acceptance, so this fixture stops
    short of ``ReviewExtensionFixture.setUp``'s own implement/review/accept and instead lets each
    test propose its own scope-change completion (or a normal one, for the negative-path tests).
    """

    CHARTER_REVIEWS = 3

    def setUp(self):
        Fixture.setUp(self)
        self.configure_runtime()
        self.room = self.open()
        self.spec()
        original_request = self.fake.request

        def raw_request(method, path, payload=None):
            if method == 'GET' and '/conversation?' in path:
                return {**self.fake.conversation(path.split('/')[2]), 'hasMoreBefore': False}
            return original_request(method, path, payload)

        self.fake.request = raw_request
        for session in self.fake.sessions.values():
            session['isTerminated'] = False
        for snapshot in self.fake.snapshots.values():
            snapshot.update(controller='ready', branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.bind()
        for number in range(1, self.CHARTER_REVIEWS + 1):
            self.agree(key='charter-' + str(number))
        self.target_spec = None

    def room_files(self):
        """Every file under this room's own directory, keyed by its relative path.

        Mirrors ``ExtensionEvolutionTests.room_files`` in ``test_ao_review_extension.py`` (a sibling
        test class there, not the shared fixture, so it is not inherited here).
        """
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}

    def assert_refuses_without_mutation(self, pattern, action):
        """``action`` refuses matching ``pattern``, makes no AO POST, and mutates no room bytes."""
        before_files = self.room_files()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, pattern):
            action()
        self.assertEqual(self.room_files(), before_files)
        self.assertEqual(len(self.fake.posts), posts)

    def register(self):
        if self.target_spec is None:
            self.target_spec = self.service.ao_room_spec_put(self.room, 2,
                'Review the newly authorized validation charter after the proposed scope change.',
                self.gates, 'Astra approves this exact next charter under the actual scope decision')
        return self.target_spec

    def scope_change_report(self, **changes):
        handoff = ao_workflow.handoff_record(self.directory(), self.state())
        return {'outcome': 'scope_change', 'implementation_complete': False, 'changes': [],
                'tests_reported': [], 'review_findings': [],
                'remaining_gaps': ['The agreed charter conflicts with the actual repository state.'],
                'backlog': [], 'routing_log': [{'delegate_job_ids': []}],
                'spec_revision': handoff['spec_revision'], 'spec_sha256': handoff['spec_sha256'],
                'baseline_commit': handoff['baseline_commit'], **changes}

    def propose_scope_change(self, **changes):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.scope_change_report(**changes)))
        return self.service.ao_room_sync(self.room)

    def implement_after_fourth(self, **changes):
        """A fresh handoff and a real completed implementation, under a distinct request_id.

        ``request_id='implementation'`` already names this fixture's scope-change proposal, so the
        genuine post-fourth-review implementation turn needs its own explicit identity; it otherwise
        follows ``Fixture.implement`` exactly.
        """
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'post-fourth-implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report(**changes)))
        return self.service.ao_room_sync(self.room)

    def candidate_sha256(self, request_id='implementation'):
        request = self.state()['requests'][request_id]
        saved = ao.read(self.directory() / request['completion_candidate'])
        return saved['candidate']['sha256']

    def audit(self, **changes):
        spec = self.register()
        inputs = {'room_id': self.room, 'spec_revision': 2, 'spec_sha256': spec['sha256'],
                  'scope_request_id': 'implementation', 'native_session_id': self.NATIVE,
                  'native_owner_database': str(self.database)}
        if 'candidate_sha256' not in changes:
            # Lazy: a caller proving a *tampered* completion-candidate file supplies its own
            # pre-tamper value, since self.candidate_sha256() re-reads the (now corrupted) file.
            inputs['candidate_sha256'] = self.candidate_sha256()
        inputs.update(changes)
        return self.service.ao_room_preacceptance_review_audit(**inputs)

    def grant_inputs(self, audit=None, **changes):
        return {'room_id': self.room, 'audit_sha256': (audit or self.audit())['audit_sha256'],
                'authorization': ('User answered "I approve" to the explicit proposal for one additional charter '
                                  'review before any acceptance, in this retained room and session.'),
                'diagnosis': 'Three prior source/spec review intents remain; the scope-change charter needs one exact review.',
                'request_id': 'pre-acceptance-extension', **changes}

    def grant(self):
        self.inputs = self.grant_inputs()
        return self.service.ao_room_preacceptance_review_extend(**self.inputs)

    def write_native(self, folder):
        """Materialize one coherent native interval for the latest engineer request.

        Copied unchanged from ``test_ao_review_extension.ExtensionEvolutionTests.write_native``: the
        room's currently registered transcript is the retained prefix, reused byte-for-byte, and one
        complete caller/response pair is appended with ordered timestamps clamped inside the
        request's own completed window.
        """
        import ao_outcomes
        request = ao_outcomes.latest_for_role(self.state(), 'engineer')
        folder = Path(folder)
        folder.mkdir(exist_ok=True)
        path = folder / (self.NATIVE + '.jsonl')
        registered = Path(self.state()['native_outcome_source']['transcript'])
        if not path.exists():
            path.write_text(registered.read_text() if registered.is_file() else '')
        existing = path.read_text()
        if '"' + request['request_id'] + '-caller"' not in existing:
            workspace = self.native_row_workspace()
            model = (request.get('engineering_resolution') or {}).get('expected_model') or request['model']
            receipt = ao.read(self.directory() / request['receipt']) if request.get('receipt') else {}
            completed = receipt['turn'].get('completedAt') if isinstance(receipt.get('turn'), dict) else None
            bound = (datetime.fromisoformat(completed).timestamp() if isinstance(completed, str)
                     else receipt.get('observed_at', datetime.now(timezone.utc).timestamp()))
            stamp = lambda seconds: min(request['created_at'] + seconds, bound)
            rows = [{'type': 'user', 'sessionId': self.NATIVE, 'cwd': workspace,
                     'uuid': request['request_id'] + '-caller',
                     'timestamp': datetime.fromtimestamp(stamp(0.001), timezone.utc).isoformat(),
                     'isSidechain': False, 'origin': {'kind': 'human'},
                     'message': {'role': 'user', 'content': request['text']}},
                    {'type': 'assistant', 'sessionId': self.NATIVE, 'cwd': workspace,
                     'uuid': request['request_id'] + '-reply',
                     'timestamp': datetime.fromtimestamp(stamp(0.002), timezone.utc).isoformat(),
                     'isSidechain': False,
                     'message': {'role': 'assistant', 'id': request['request_id'] + '-m0', 'model': model,
                                 'content': [{'type': 'text', 'text': 'Done'}],
                                 'stop_reason': 'end_turn'}}]
            path.write_text(existing + ''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def moved_source_hold(self, kind):
        """Copied from ``ExtensionEvolutionTests.moved_source_hold``, with its own scope-change grant."""
        self.propose_scope_change()
        self.grant()
        snapshot = self.fake.snapshots['engineer']
        if kind == 'quota_limit':
            snapshot['turns'][-1]['error'] = {'type': 'rate_limit'}
        else:
            snapshot['sessionFailures'] = [{'type': 'unclassified'}]
        self.service.ao_room_sync(self.room)
        original_request = self.state()['requests']['implementation']
        original_outcome = (self.directory() / original_request['semantic_outcome']).read_bytes()
        path = self.write_native(self.root / 'native-first')
        first = self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(path))
        self.assertEqual(first['outcome']['kind'], kind)
        if kind == 'quota_limit':
            self.service.ao_room_outcome_resume(self.room, 'implementation', first['outcome_sha256'], 'fourth-after-source',
                'The original correlated quota stop was diagnosed.', 'Actual approval for only the named continuation.')
        previous = self.state()['requests']['implementation']
        release_bytes = ((self.directory() / previous['outcome_resume']).read_bytes() if previous.get('outcome_resume') else None)
        snapshot['turns'][-1].pop('error', None)
        snapshot['sessionFailures'] = []
        moved_folder = self.root / 'native-moved'; moved_folder.mkdir()
        moved_path = moved_folder / path.name; path.rename(moved_path)
        moved = self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(moved_path))
        current = self.state()['requests']['implementation']
        self.assertEqual(moved['outcome'], first['outcome'])
        self.assertTrue(moved['outcome']['hold'])
        self.assertNotEqual(moved['outcome_sha256'], first['outcome_sha256'])
        self.assertEqual(current.get('outcome_resume'), previous.get('outcome_resume'))
        self.assertEqual(current.get('outcome_resume_sha256'), previous.get('outcome_resume_sha256'))
        if release_bytes is not None:
            self.assertEqual((self.directory() / current['outcome_resume']).read_bytes(), release_bytes)
        self.assertEqual((self.directory() / original_request['semantic_outcome']).read_bytes(), original_outcome)
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: ' + kind):
            self.send('spec_review', 'fourth-after-source')
        self.assertNotIn('fourth-after-source', self.state()['requests'])
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        return moved


class AcceptedLaneUnchangedTests(ReviewExtensionFixture):
    """Coverage item 5 (accepted side) and a direct regression check on the shared receipt shape."""

    def test_preacceptance_audit_refuses_in_a_room_with_a_retained_acceptance(self):
        spec = self.register()
        with self.assertRaisesRegex(ao.RoomError, 'zero retained independent acceptances'):
            self.service.ao_room_preacceptance_review_audit(
                room_id=self.room, spec_revision=2, spec_sha256=spec['sha256'], scope_request_id='implementation',
                candidate_sha256='0' * 64, native_session_id=self.NATIVE, native_owner_database=str(self.database))

    def test_accepted_lane_audit_grant_and_meaning_carry_no_lane_field(self):
        audited = self.audit()
        self.assertNotIn('lane', audited)
        saved = extension._audit(self.directory(), audited['audit_sha256'])
        self.assertNotIn('lane', saved)
        self.assertNotIn('lane', saved['evidence'])
        self.assertNotIn('lane', saved['evidence']['target'])
        granted = self.grant()
        self.assertNotIn('lane', self.state()['spec_review_extension'])
        self.assertEqual(granted['lane'], extension.ACCEPTED)
        self.assertEqual(extension.MEANINGS[extension.ACCEPTED],
                         'One fourth charter-review intent only; source-review and acceptance allowances and all holds stay unchanged')


class PreAcceptanceEligibilityTests(PreAcceptanceFixture):
    """Coverage item 1: the eligible happy path, including the fourth send consuming the grant once."""

    def test_eligible_room_audits_then_grants_once_and_the_fourth_send_consumes_it(self):
        self.propose_scope_change()
        self.register()  # Registering the exact next charter is itself a precondition, not the audit's own effect.
        before = (self.directory() / 'state.json').read_bytes()
        posts = len(self.fake.posts)
        audited = self.audit()
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['lane'], extension.PRE)
        self.assertEqual(audited['acceptances'], 0)
        self.assertEqual(audited['candidate_status'], 'unaccepted_unverified')
        self.assertEqual(audited['prior_spec_review_attempts'], 3)
        self.assertEqual(audited['scope_proposal']['request_id'], 'implementation')
        self.assertEqual(audited['scope_proposal']['outcome'], 'scope_change')
        self.assertEqual(audited['scope_proposal']['evidence'], 'engineering_record')
        self.assertIsNone(audited['scope_proposal']['engineering_error'])
        self.assertEqual(self.state()['acceptances'], [])
        self.assertIsNone(self.state().get('checkpoint'))
        self.assertNotIn('spec_review_extension', self.state())
        with self.assertRaisesRegex(ao.RoomError, 'Three Fable'):
            self.send('spec_review', 'ungranted-fourth')
        granted = self.grant()
        self.assertEqual(granted['lane'], extension.PRE)
        self.assertEqual(granted['remaining_spec_reviews'], 1)
        self.assertEqual(granted['meaning'], extension.MEANINGS[extension.PRE])
        self.assertEqual(self.service.ao_room_preacceptance_review_extend(**self.inputs), granted)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['lane'], extension.PRE)
        sent = self.send('spec_review', 'fourth')
        self.assertEqual(sent['state'], 'submitted')
        intent = self.state()['requests']['fourth']
        self.assertEqual(intent['carried']['spec_review_extension_sha256'], granted['receipt_sha256'])
        self.assertNotIn(granted['receipt_sha256'], intent['text'])
        status = self.finish_fourth()
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth')
        with self.assertRaisesRegex(ao.RoomError, 'fourth.*exhausted'):
            self.send('spec_review', 'fifth')
        posts = len(self.fake.posts)
        self.assertEqual(self.send('spec_review', 'fourth')['state'], 'completed')
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'cannot renew or renumber'):
            self.service.ao_room_preacceptance_review_extend(**{**self.inputs, 'request_id': 'renumbered'})


class PreAcceptanceReportFormTests(PreAcceptanceFixture):
    """Coverage item 3: both the full and the compact ``project_room_engineering_v2`` report forms.

    Registering the exact next charter revision is itself a precondition of auditing (see
    ``_inspect``'s exact-next-charter-revision check), and once registered, the unagreed revision
    blocks any further implementation/correction send (``agreement()`` requires the current spec to
    already be agreed). So only the single latest implementation/correction request is ever
    auditable; each report form gets its own fresh room rather than sharing one.
    """

    def test_full_report_form_is_accepted(self):
        self.propose_scope_change()  # the default full form: no report_format key.
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['scope_proposal']['evidence'], 'engineering_record')

    def test_compact_report_form_is_accepted(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        compact = {'report_format': 'project_room_engineering_v2', 'outcome': 'scope_change',
                   'implementation_complete': False, 'changes': [], 'tests_reported': [], 'review_findings': [],
                   'remaining_gaps': ['The agreed charter conflicts with the actual repository state.'],
                   'backlog': [], 'routing_log': []}
        self.fake.finish('engineer', json.dumps(compact))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertIsNotNone(request.get('engineering_record'))
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['scope_proposal']['evidence'], 'engineering_record')


class PreAcceptanceRevisionTests(PreAcceptanceFixture):
    """Coverage item 4: exact-next-revision-only, at both audit time and (re-checked) grant time."""

    def test_wrong_spec_revision_or_sha256_refuses_the_audit(self):
        self.propose_scope_change()
        spec = self.register()
        with self.assertRaisesRegex(ao.RoomError, 'Register the exact next charter revision'):
            self.service.ao_room_preacceptance_review_audit(
                room_id=self.room, spec_revision=3, spec_sha256=spec['sha256'], scope_request_id='implementation',
                candidate_sha256=self.candidate_sha256(), native_session_id=self.NATIVE,
                native_owner_database=str(self.database))
        with self.assertRaisesRegex(ao.RoomError, 'Register the exact next charter revision'):
            self.service.ao_room_preacceptance_review_audit(
                room_id=self.room, spec_revision=2, spec_sha256='1' * 64, scope_request_id='implementation',
                candidate_sha256=self.candidate_sha256(), native_session_id=self.NATIVE,
                native_owner_database=str(self.database))

    def test_registering_a_further_charter_between_audit_and_grant_makes_the_audit_stale(self):
        self.propose_scope_change()
        audited = self.audit()
        self.service.ao_room_spec_put(self.room, 3, 'A further charter registered before granting.',
            self.gates, 'Astra approves this further scope decision')
        with self.assertRaisesRegex(ao.RoomError, 'Register the exact next charter revision'):
            self.service.ao_room_preacceptance_review_extend(
                self.room, audit_sha256=audited['audit_sha256'], authorization='User approves the exact next review.',
                diagnosis='Diagnosis for the stale-audit scenario.', request_id='stale-after-further-charter')


class PreAcceptanceAttributionTests(PreAcceptanceFixture):
    """Coverage item 2: malformed delegate attribution is preserved verbatim and still supports the proposal."""

    def test_malformed_delegate_attribution_is_preserved_verbatim_and_still_supports_the_proposal(self):
        self.propose_scope_change(routing_log=[{'delegate_job_ids': ['not-a-provider-job']}])
        request = self.state()['requests']['implementation']
        self.assertIn('malformed delegate job identifier', request['engineering_error'])
        self.assertEqual(request['reported_delegate_job_ids'], ['not-a-provider-job'])
        self.assertIsNone(request.get('engineering_record'))
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['scope_proposal']['evidence'], 'rejected_report_diagnostic')
        self.assertEqual(audited['scope_proposal']['engineering_error'], request['engineering_error'])
        granted = self.grant()
        self.assertEqual(granted['remaining_spec_reviews'], 1)


class PreAcceptanceZeroAcceptanceTests(PreAcceptanceFixture):
    """Coverage item 5 (pre-acceptance side): zero retained acceptances is required in both directions.

    ``AcceptedLaneUnchangedTests.test_preacceptance_audit_refuses_in_a_room_with_a_retained_acceptance``
    proves the refusal when an acceptance is retained; this proves the success when none is.
    """

    def test_pre_acceptance_audit_succeeds_with_zero_retained_acceptances(self):
        self.assertEqual(self.state()['acceptances'], [])
        self.propose_scope_change()
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.assertEqual(audited['acceptances'], 0)


class PreAcceptanceScopeRequestTests(PreAcceptanceFixture):
    """Coverage item 6: scope-proposal requirement violations, with the exact catalogued messages."""

    def test_unknown_scope_request_id_refuses(self):
        self.propose_scope_change()
        with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_REQUEST):
            self.audit(scope_request_id='no-such-request')

    def test_scope_request_naming_a_non_implementation_purpose_request_refuses(self):
        self.propose_scope_change()
        with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_REQUEST):
            self.audit(scope_request_id='charter-3')

    def test_non_latest_scope_request_refuses(self):
        # A completed scope_change report immediately blocks any further implementation/correction
        # send (``packet()`` refuses once the prior report's outcome is scope_change) until the spec
        # is revised, so the "non-latest" request must precede the scope-change one, not follow it.
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'first-implementation')
        self.fake.finish('engineer', json.dumps(self.report(outcome='changes_required', implementation_complete=False)))
        self.service.ao_room_sync(self.room)
        # The same handoff's "implementation once, then correction only" rule means the second,
        # actually-latest proposal must be a 'correction', not another 'implementation' send.
        self.send('correction', 'scope-change-correction')
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_REQUEST):
            self.audit(scope_request_id='first-implementation', candidate_sha256=self.candidate_sha256('first-implementation'))

    def test_not_yet_completed_scope_request_refuses(self):
        # ``settled()``'s general active-request guard blocks every ``ao_room_*`` call -- including
        # the register-next-revision precondition the public audit entrypoint itself requires --
        # while any request stays non-terminal, so no normal flow can ever present a *pending*
        # scope_request_id to ``ao_room_preacceptance_review_audit``. This calls the real validator
        # directly to prove its own "not completed" branch still refuses on its own terms.
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        target = {'scope_request_id': 'implementation', 'candidate_sha256': '0' * 64}
        actual = ao_workflow.workspace(self.service, self.directory(), self.state(), check_routing=False)
        with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_REQUEST):
            extension._scope_proposal(self.directory(), self.state(), target, actual)

    def test_registering_the_next_charter_itself_refuses_while_the_scope_request_is_still_active(self):
        """Complements the private-call test above with the production-path proof of that same
        reachability boundary: ``ao_room_spec_put`` -- the audit's own precondition, since the exact
        next revision must already be registered -- refuses first, through ``quiet()``'s general
        active-request guard, so the audit itself can never be reached with a pending scope request."""
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.assertEqual(self.state()['requests']['implementation']['state'], 'submitted')
        self.assert_refuses_without_mutation('active or uncertain', self.register)

    def test_wrong_outcome_report_refuses_with_the_request_message(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))  # outcome == 'completed', not scope_change.
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_REQUEST):
            self.audit(candidate_sha256=self.candidate_sha256())

    def test_quota_held_outcome_refuses_the_audit_until_independently_resolved(self):
        self.propose_scope_change()
        self.fake.snapshots['engineer']['turns'][-1]['error'] = {'type': 'rate_limit'}
        self.service.ao_room_sync(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: quota_limit'):
            self.audit(candidate_sha256=self.candidate_sha256())

    def test_missing_or_modified_candidate_evidence_refuses(self):
        self.propose_scope_change()
        candidate_sha256 = self.candidate_sha256()
        request = self.state()['requests']['implementation']
        path = self.directory() / request['completion_candidate']
        original = path.read_bytes()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_EVIDENCE):
                self.audit(candidate_sha256=candidate_sha256)
        finally:
            path.write_bytes(original)

    def test_missing_or_modified_engineering_record_refuses(self):
        self.propose_scope_change()
        request = self.state()['requests']['implementation']
        path = self.directory() / request['engineering_record']
        original = path.read_bytes()
        candidate_sha256 = self.candidate_sha256()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ao.RoomError, extension.SCOPE_EVIDENCE):
                self.audit(candidate_sha256=candidate_sha256)
        finally:
            path.write_bytes(original)

    def test_wrong_candidate_sha256_refuses_with_the_exact_identity_message(self):
        self.propose_scope_change()
        with self.assertRaisesRegex(ao.RoomError, 'Use the exact immutable candidate-at-completion identity'):
            self.audit(candidate_sha256='0' * 64)


class PreAcceptanceCandidateDriftTests(PreAcceptanceFixture):
    """Coverage item 7: live-candidate equality at audit, at grant and at send, plus restore-and-retry."""

    def test_worktree_drift_between_audit_and_grant_refuses_and_restoring_allows_a_fresh_audit(self):
        self.propose_scope_change()
        audited = self.audit()
        (self.repo / 'feature.txt').write_text('drifted\n')
        try:
            with self.assertRaisesRegex(ao.RoomError, 'unaccepted candidate changed since the scope-change proposal completed'):
                self.service.ao_room_preacceptance_review_extend(
                    self.room, audit_sha256=audited['audit_sha256'], authorization='User approves.',
                    diagnosis='Diagnosis.', request_id='drifted-grant')
        finally:
            (self.repo / 'feature.txt').write_text('start\n')
        fresh = self.audit()
        granted = self.service.ao_room_preacceptance_review_extend(
            self.room, audit_sha256=fresh['audit_sha256'], authorization='User approves.',
            diagnosis='Diagnosis.', request_id='restored-grant')
        self.assertEqual(granted['remaining_spec_reviews'], 1)

    def test_worktree_drift_between_commit_and_send_refuses_the_fourth_send(self):
        self.propose_scope_change()
        self.grant()
        (self.repo / 'feature.txt').write_text('drifted-after-commit\n')
        try:
            # ``admission()`` re-runs ``_scope_proposal`` live before the fourth send is accepted; a
            # changed worktree fails that same live check and surfaces its own exact message directly
            # (the admission-view-mismatch message guards a narrower residual delta, not this one).
            with self.assertRaisesRegex(ao.RoomError,
                    'unaccepted candidate changed since the scope-change proposal completed'):
                self.send('spec_review', 'fourth')
            self.assertNotIn('fourth', self.state()['requests'])
        finally:
            (self.repo / 'feature.txt').write_text('start\n')


class PreAcceptanceAfterGrantDriftTests(PreAcceptanceFixture):
    """Coverage item 8: drift families after a committed grant -- spec re-registration and native owner."""

    def test_reregistering_an_unused_charter_refuses_but_an_identical_put_is_idempotent(self):
        self.propose_scope_change()
        spec = self.register()
        self.audit()
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'a different spec was not saved'):
            self.service.ao_room_spec_put(self.room, 3, 'A further charter after granting.', self.gates,
                                          'Astra approves a further scope decision')
        same = self.service.ao_room_spec_put(self.room, 2,
            'Review the newly authorized validation charter after the proposed scope change.',
            self.gates, 'Astra approves this exact next charter under the actual scope decision')
        self.assertEqual(same['sha256'], spec['sha256'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)

    def test_native_owner_drift_after_a_committed_grant_blocks_without_mutation(self):
        self.propose_scope_change()
        self.grant()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='replacement-native'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='replacement-native'")
        before = (self.directory() / 'state.json').read_bytes()
        with self.assertRaisesRegex(ao.RoomError, 'same native owner'):
            self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database),
                                             native_transcript_path=str(self.root / 'replacement-native.jsonl'))
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        with self.assertRaises(ao.RoomError):
            self.send('spec_review', 'blocked-after-owner-drift')


class PreAcceptanceAuthorityTests(PreAcceptanceFixture):
    """Coverage item 9: authority -- empty inputs, wrong audit, replay, and both cross-lane refusals."""

    def test_empty_authorization_or_diagnosis_refuses(self):
        self.propose_scope_change()
        audited = self.audit()
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_preacceptance_review_extend(
                self.room, audit_sha256=audited['audit_sha256'], authorization='', diagnosis='Diagnosis.',
                request_id='empty-authorization')
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_preacceptance_review_extend(
                self.room, audit_sha256=audited['audit_sha256'], authorization='User approves.', diagnosis='',
                request_id='empty-diagnosis')

    def test_wrong_audit_sha256_refuses(self):
        self.propose_scope_change()
        self.audit()
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_preacceptance_review_extend(
                self.room, audit_sha256='0' * 64, authorization='User approves.', diagnosis='Diagnosis.',
                request_id='wrong-audit')

    def test_identical_grant_replay_reconciles_without_a_second_receipt(self):
        self.propose_scope_change()
        granted = self.grant()
        pending_before = extension._pending(self.directory())
        self.assertEqual(self.service.ao_room_preacceptance_review_extend(**self.inputs), granted)
        self.assertEqual(extension._pending(self.directory()), pending_before)

    def test_changed_payload_replay_refuses(self):
        self.propose_scope_change()
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'cannot renew or renumber'):
            self.service.ao_room_preacceptance_review_extend(
                **{**self.inputs, 'authorization': 'A completely different approval text.'})

    def test_cross_lane_audit_refuses_before_a_commit(self):
        synthetic = {'version': 3, 'evidence': {'target': {}}, 'observed_at': 0.0, 'observed_snapshots': {}}
        sha256 = extension._store_audit(self.directory(), synthetic)
        with self.assertRaisesRegex(ao.RoomError, 'audit belongs to the other lane'):
            self.service.ao_room_preacceptance_review_extend(
                self.room, audit_sha256=sha256, authorization='User approves.', diagnosis='Diagnosis.',
                request_id='cross-lane-before-commit')

    def test_cross_lane_grant_refuses_after_a_commit(self):
        self.propose_scope_change()
        self.grant()
        with self.assertRaisesRegex(ao.RoomError, 'already belongs to the other lane'):
            self.service.ao_room_spec_review_extend(
                self.room, audit_sha256='0' * 64, authorization='User approves.', diagnosis='Diagnosis.',
                request_id='cross-lane-after-commit')


class PreAcceptanceDurabilityTests(PreAcceptanceFixture):
    """Coverage item 10: durability under interruption, tampering and a bounded history prefix."""

    def test_grant_commit_interruption_before_state_write_reconciles_without_a_model_call(self):
        self.propose_scope_change()
        inputs = self.grant_inputs()
        before = (self.directory() / 'state.json').read_bytes()
        posts = len(self.fake.posts)
        with patch.object(self.service, 'save', side_effect=OSError('synthetic state write interruption')):
            with self.assertRaises(OSError):
                self.service.ao_room_preacceptance_review_extend(**inputs)
        self.assertEqual((self.directory() / 'state.json').read_bytes(), before)
        self.assertEqual(len(self.fake.posts), posts)
        pending = extension._pending(self.directory())
        self.assertEqual(len(pending), 1)
        receipt_bytes = pending[0].read_bytes()
        status = self.service.ao_room_status(self.room)['spec_review_extension']
        self.assertEqual(status['state'], 'pending_uncommitted')
        self.assertEqual(status['lane'], extension.PRE)
        with self.assertRaisesRegex(ao.RoomError, 'another payload'):
            self.service.ao_room_preacceptance_review_extend(**{**inputs, 'request_id': 'other-grant'})
        result = self.service.ao_room_preacceptance_review_extend(**inputs)
        self.assertEqual(result['remaining_spec_reviews'], 1)
        self.assertEqual(pending[0].read_bytes(), receipt_bytes)
        self.assertEqual(len(self.fake.posts), posts)

    def test_pending_receipt_tampering_refuses_reconciliation(self):
        self.propose_scope_change()
        inputs = self.grant_inputs()
        with patch.object(self.service, 'save', side_effect=OSError('synthetic crash')):
            with self.assertRaisesRegex(OSError, 'synthetic crash'):
                self.service.ao_room_preacceptance_review_extend(**inputs)
        path = extension._pending(self.directory())[0]
        record = ao.read(path)
        record['maximum_spec_review_attempts'] = 5
        ao.atomic(path, record)
        with self.assertRaisesRegex(ao.RoomError, 'recomputed grant'):
            self.service.ao_room_preacceptance_review_extend(**inputs)
        self.assertNotIn('spec_review_extension', self.state())

    def test_consumption_receipt_without_state_projection_requires_diagnosis_not_resend(self):
        self.propose_scope_change()
        self.grant()
        save = self.service.save
        posts = len(self.fake.posts)

        def interrupted_projection(directory, state):
            if 'orphan-fourth' in state['requests']:
                raise OSError('Synthetic crash after the durable consumption receipt, before state projection')
            save(directory, state)

        with patch.object(self.service, 'save', side_effect=interrupted_projection):
            with self.assertRaisesRegex(OSError, 'Synthetic crash after the durable consumption receipt, '
                                                 'before state projection'):
                self.send('spec_review', 'orphan-fourth')
        self.assertNotIn('orphan-fourth', self.state()['requests'])
        self.assertEqual(len(extension._consumptions(self.directory())), 1)
        self.assertEqual(len(self.fake.posts), posts)
        with self.assertRaisesRegex(ao.RoomError, 'projection is missing; diagnose without resending'):
            self.service.ao_room_status(self.room)
        with self.assertRaisesRegex(ao.RoomError, 'projection is missing'):
            self.send('spec_review', 'replacement-fourth')

    def test_committed_receipt_and_retained_file_tampering_block_without_mutation(self):
        self.propose_scope_change()
        self.grant()
        pointer = self.state()['spec_review_extension']
        paths = [self.directory() / pointer['receipt'],
                 self.directory() / extension.BASE / 'audits' / (self.inputs['audit_sha256'] + '.json'),
                 self.directory() / self.state()['requests']['charter-1']['receipt']]
        for path in paths:
            original = path.read_bytes()
            try:
                path.write_text('{}')
                state_bytes = (self.directory() / 'state.json').read_bytes()
                with self.subTest(path=path.name):
                    with self.assertRaises(ao.RoomError):
                        self.service.ao_room_status(self.room)
                    with self.assertRaises(ao.RoomError):
                        self.send('spec_review', 'blocked-fourth')
                self.assertEqual((self.directory() / 'state.json').read_bytes(), state_bytes)
            finally:
                path.write_bytes(original)

    def test_tampered_state_reference_cannot_be_rewritten(self):
        self.propose_scope_change()
        self.grant()
        original = self.state()
        changes = {
            'delete-request': lambda s: s['requests'].pop('charter-1'),
            'renumber-request': lambda s: s['requests']['charter-1'].update(created_order=99),
            'grant-request-id': lambda s: s['spec_review_extension'].update(request_id='new-round'),
            'bound-effort': lambda s: s['bindings']['engineer'].update(reasoning_effort='low'),
            'lane-removed': lambda s: s['spec_review_extension'].pop('lane', None),
        }
        for name, change in changes.items():
            state = copy.deepcopy(original)
            change(state)
            ao.atomic(self.directory() / 'state.json', state)
            try:
                with self.subTest(change=name):
                    with self.assertRaises(ao.RoomError):
                        self.send('spec_review', 'blocked-' + name)
            finally:
                ao.atomic(self.directory() / 'state.json', original)

    def test_prior_failed_verification_remains_an_immutable_prefix_after_the_grant(self):
        """A failed verification before the scope-change report gives the pre-acceptance lane a
        non-empty retained ``verifications`` baseline, so its immutable-prefix bound is not vacuous."""
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        checkpoint = self.service.ao_room_verify(self.room, str(self.repo))
        self.assertFalse(checkpoint['passed'])
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        self.register()
        audited = self.audit(scope_request_id='correction', candidate_sha256=self.candidate_sha256('correction'))
        self.inputs = self.grant_inputs(audit=audited)
        self.service.ao_room_preacceptance_review_extend(**self.inputs)
        original = self.state()
        self.assertEqual(len(original['verifications']), 1)
        tampered = copy.deepcopy(original)
        tampered['verifications'][0]['state'] = 'passed'
        ao.atomic(self.directory() / 'state.json', tampered)
        try:
            with self.assertRaisesRegex(ao.RoomError, 'retained acceptance or verification history changed'):
                self.service.ao_room_status(self.room)
        finally:
            ao.atomic(self.directory() / 'state.json', original)


class PreAcceptanceOutcomeSourceTests(PreAcceptanceFixture):
    """Coverage item 11: a moved outcome-source pointer without a hold; a held outcome with one."""

    def test_outcome_pointer_move_without_a_hold_does_not_block_admission(self):
        self.propose_scope_change()
        self.grant()
        path = self.write_native(self.root / 'native-first')
        self.service.ao_room_outcome_audit(self.room, ao_database_path=str(self.database), native_transcript_path=str(path))
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)
        moved_db = self.root / 'moved-ao.db'; shutil.copyfile(self.database, moved_db); self.database.unlink()
        moved_folder = self.root / 'native-moved'; moved_folder.mkdir()
        moved_path = moved_folder / path.name; path.rename(moved_path)
        self.service.ao_room_outcome_audit(self.room, ao_database_path=str(moved_db), native_transcript_path=str(moved_path))
        self.send('spec_review', 'fourth')
        self.finish_fourth()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_held_outcome_refuses_audit_and_send_until_a_separate_exact_outcome_audit(self):
        self.moved_source_hold('unknown')
        audited = self.service.ao_room_outcome_audit(self.room)
        self.assertFalse(audited['outcome']['hold'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 1)


class PreAcceptanceLifecycleTests(PreAcceptanceFixture):
    """Coverage item 12: the full lifecycle after the fourth review, through independent acceptance."""

    def test_full_lifecycle_after_fourth_review_reaches_independent_acceptance(self):
        self.propose_scope_change()
        self.grant()
        self.send('spec_review', 'fourth')
        self.finish_fourth()
        self.implement_after_fourth()
        self.review()
        accepted = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(accepted['accepted'])
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(self.state()['acceptances'][-1]['candidate_sha256'], accepted['candidate_sha256'])


class PreAcceptanceCliMcpTests(PreAcceptanceFixture):
    """Coverage item 13: both new tool names are CLI/MCP callable and require explicit complete inputs."""

    def test_cli_and_mcp_expose_both_new_tools_and_require_explicit_complete_inputs(self):
        self.propose_scope_change()
        controller = project_room.Service(self.home)
        listed = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, controller)
        names = {item['name'] for item in listed['result']['tools']}
        self.assertIn('ao_room_preacceptance_review_audit', names)
        self.assertIn('ao_room_preacceptance_review_extend', names)
        with patch.object(ao, 'Service', return_value=self.service):
            with self.assertRaisesRegex(ao.RoomError, 'Invalid arguments'):
                controller.call('ao_room_preacceptance_review_extend', {'room_id': self.room})
            spec = self.register()
            audit_inputs = {'room_id': self.room, 'spec_revision': 2, 'spec_sha256': spec['sha256'],
                            'scope_request_id': 'implementation', 'candidate_sha256': self.candidate_sha256(),
                            'native_session_id': self.NATIVE, 'native_owner_database': str(self.database)}
            response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'ao_room_preacceptance_review_audit', 'arguments': audit_inputs}}, controller)
            self.assertFalse(response['result']['isError'])
            self.assertTrue(response['result']['structuredContent']['eligible'])
            inputs = self.grant_inputs(audit={'audit_sha256': response['result']['structuredContent']['audit_sha256']})
            extend_response = project_room_mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'ao_room_preacceptance_review_extend', 'arguments': inputs}}, controller)
        self.assertFalse(extend_response['result']['isError'])
        self.assertEqual(extend_response['result']['structuredContent']['remaining_spec_reviews'], 1)


class PreAcceptanceUncertainScopeRequestTests(PreAcceptanceFixture):
    """Coverage item 14 (OP-Q2 family 1, highest priority): a genuinely uncertain scope request.

    ``PreAcceptanceScopeRequestTests.test_quota_held_outcome_refuses_the_audit_until_independently_resolved``
    and ``PreAcceptanceOutcomeSourceTests.test_held_outcome_refuses_audit_and_send_until_a_separate_exact_outcome_audit``
    already cover a *settled* scope request whose native outcome later becomes a held quota/unknown
    observation -- refusing the audit, and after a committed grant refusing the fourth send, until a
    separate exact outcome audit resolves it -- and are kept unchanged. This proves the different,
    lower-level case instead: the scope request itself never reaches a terminal state because its
    native acknowledgement was lost, reached through the real ``ao_room_send`` production path (the
    fixture's ``lose_ack`` knob), not fixture state tampering.
    """

    def test_uncertain_scope_request_via_lost_acknowledgement_refuses_the_audit(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.fake.lose_ack = True
        try:
            sent = self.send('implementation')
        finally:
            self.fake.lose_ack = False
        self.assertEqual(sent['state'], 'uncertain')
        self.assert_refuses_without_mutation('active or uncertain',
            lambda: self.audit(scope_request_id='implementation', candidate_sha256='0' * 64))


class PreAcceptanceBaselineDriftTests(PreAcceptanceFixture):
    """Coverage item 15 (OP-Q2 family 2): a no-op commit after the grant moves HEAD although every
    working file stays byte-identical. ``candidate_snapshot`` binds HEAD itself (``implementation.py``
    ``candidate_snapshot``), so the live candidate's identity still changes and the fourth send still
    refuses; restoring the baseline afterward is not required for this family.
    """

    def test_new_commit_with_identical_working_files_still_refuses_the_fourth_send(self):
        self.propose_scope_change()
        self.grant()
        ao.git(self.repo, 'commit', '--allow-empty', '-m', 'No-op: HEAD moves, working files unchanged')
        self.assert_refuses_without_mutation(
            'unaccepted candidate changed since the scope-change proposal completed',
            lambda: self.send('spec_review', 'fourth-after-empty-commit'))
        self.assertNotIn('fourth-after-empty-commit', self.state()['requests'])


class PreAcceptanceSourceDriftTests(PreAcceptanceFixture):
    """Coverage item 16 (OP-Q2 family 3): a changed registered-engineer source/executable binding
    after a committed grant must be refused, not silently accepted or left unrevalidated.
    """

    def test_changed_engineer_source_after_grant_refuses(self):
        self.propose_scope_change()
        self.grant()
        original_native = self.write_native(self.root / 'source-register-native')
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE sessions SET provider_conversation_id='replacement-source-native'")
            db.execute("UPDATE conversation_branches SET provider_conversation_id='replacement-source-native'")
        # preflight_source validates the prospective transcript's own filename against the native
        # owner row it reads fresh from the database, so the replacement transcript must be named
        # after the tampered identity to get past that filename check.
        transcript = original_native.parent / 'replacement-source-native.jsonl'
        transcript.write_text(original_native.read_text())
        # Every room this fixture builds already has a registered source (bind() -> *
        # register_native_source()), so the generic "never replaces a conflicting registered
        # source" guard refuses unconditionally here, before ever reaching the grant's own narrower
        # guard_native_owner check inside ao_room_engineer_source_register; that narrower branch
        # appears unreachable through this production entry point once any source already exists --
        # see the final report for this finding.
        self.assert_refuses_without_mutation('never replaced',
            lambda: self.service.ao_room_engineer_source_register(self.room, str(self.database), str(transcript)))


class PreAcceptanceRoutingDriftTests(PreAcceptanceFixture):
    """Coverage item 17 (OP-Q2 family 4): a new routing adoption after the grant is unsupported.

    ``ao_room_routing_adoption_audit`` itself also runs ``guard_replacement`` on every fresh call --
    not only ``_stage``/``_activate`` -- so (unlike a plain ``ao_room_status`` read of an already-
    committed result) a brand-new audit attempt refuses too, with no prior adoption to re-read in
    this fixture; see the final report for this corrected assumption.
    """

    def test_routing_adoption_stage_and_a_fresh_audit_both_refuse_after_the_grant(self):
        self.propose_scope_change()
        self.grant()
        self.assert_refuses_without_mutation('unsupported after that grant',
            lambda: self.service.ao_room_routing_adoption_stage(
                self.room, '0' * 64, 'Actual approval', 'Diagnosed', 'adoption'))
        self.assert_refuses_without_mutation('unsupported after that grant',
            lambda: self.service.ao_room_routing_adoption_audit(self.room))


class PreAcceptanceProviderTransitionDriftTests(PreAcceptanceFixture):
    """Coverage item 18 (OP-Q2 family 5): a provider transition after the grant refuses before any
    transition receipt, pending projection or profile write."""

    def test_provider_transition_after_grant_refuses_before_any_transition_receipt(self):
        self.propose_scope_change()
        self.grant()
        self.assert_refuses_without_mutation('unsupported after that grant',
            lambda: self.service.ao_room_provider_transition(
                self.room, '0' * 64, 'Diagnosed', 'Actual approval', 'transition', 'Stopped'))


class PreAcceptanceNativeSettingsDriftTests(PreAcceptanceFixture):
    """Coverage item 19 (OP-Q2 family 6): a pinned binding tampered with after the grant refuses the
    fourth send with the shared pinned-metadata message, without writing the send. (The reasoning
    effort sub-case is the one exercised here; a tampered pinned *model* was not added separately --
    see the final report for why.)
    """

    def test_tampered_pinned_reasoning_effort_refuses_the_fourth_send(self):
        self.propose_scope_change()
        self.grant()
        original = self.state()
        tampered = copy.deepcopy(original)
        tampered['bindings']['engineer']['reasoning_effort'] = 'low'
        ao.atomic(self.directory() / 'state.json', tampered)
        try:
            self.assert_refuses_without_mutation(
                'Review-extension room, native binding, authorization or pinned metadata changed',
                lambda: self.send('spec_review', 'blocked-reasoning-effort'))
        finally:
            ao.atomic(self.directory() / 'state.json', original)
        self.assertNotIn('blocked-reasoning-effort', self.state()['requests'])


class PreAcceptanceHistoryBoundsTests(PreAcceptanceFixture):
    """Coverage item 20 (OP-Q2 family 7, part 1): truncated native history and an oversized audit
    record both refuse before any audit publication. Each test registers the exact next charter
    first (the audit's own precondition, not the effect under test) so the mutation check below
    isolates only the audit attempt itself."""

    def test_truncated_native_history_refuses_the_audit(self):
        self.propose_scope_change()
        self.register()
        self.fake.snapshots['engineer']['history_truncated'] = True
        try:
            # The raw collector (ao_history.py) rejects an explicit truthy ``history_truncated`` on
            # the native page itself before ``ao_provider_transition._native``'s own later truncation
            # check is ever reached; this is still a genuine "truncated history refuses" proof, just
            # through that earlier, more fundamental guard -- see the final report.
            self.assert_refuses_without_mutation('contradictory truncation evidence',
                lambda: self.audit(candidate_sha256=self.candidate_sha256()))
        finally:
            self.fake.snapshots['engineer']['history_truncated'] = False

    def test_oversized_audit_record_refuses_before_publication(self):
        self.propose_scope_change()
        self.register()
        for role in ('engineer', 'reviewer'):
            self.fake.snapshots[role]['activities'].append({
                'id': role + '-oversized-observation', 'summary': 'x' * 20_000, 'detail': 'not retained'})
        with patch.object(extension, 'MAX_RECORD_BYTES', 100_000):
            self.assert_refuses_without_mutation('exceeds its readable size bound',
                lambda: self.audit(candidate_sha256=self.candidate_sha256()))


class PreAcceptanceTwoReviewsFixture(PreAcceptanceFixture):
    """Exactly two consumed charter reviews instead of three, for the review-count boundary below."""
    CHARTER_REVIEWS = 2


class PreAcceptanceReviewCountBoundaryTests(PreAcceptanceTwoReviewsFixture):
    """Coverage item 20 (OP-Q2 family 7, part 2): the exact retained-review-count boundary.

    The brief describes this boundary as refusing with the existing "Three Fable" exhaustion
    message. In the actual source that message is raised only by ``admission()``, and only once
    ``len(reviews) >= LIMIT`` with no grant committed (``ao_review_extension.py``); a room with only
    two consumed reviews instead fails ``_inspect``'s own, earlier, distinct review-count
    precondition. This asserts the message the source actually raises; see the final report for this
    brief/source discrepancy. The family's remaining item -- a fifth send refusing after the fourth
    consumes the grant -- is already covered unchanged by
    ``PreAcceptanceEligibilityTests.test_eligible_room_audits_then_grants_once_and_the_fourth_send_consumes_it``.
    """

    def test_two_consumed_reviews_refuses_the_audit(self):
        self.propose_scope_change()
        self.register()
        self.assert_refuses_without_mutation('exactly three retained review intents',
            lambda: self.audit(candidate_sha256=self.candidate_sha256()))


class PreAcceptanceCliDispatchTests(PreAcceptanceFixture):
    """Coverage item 21 (OP-Q2 family 8): the real ``project_room.py`` CLI parser and ``main()`` for
    both new tool names, each asserting the printed JSON equals the Service result exactly.

    ``audit_preacceptance`` stamps a fresh ``observed_at`` on every call and its storage is not
    write-safe onto an already-existing identical path (confirmed against
    ``ExtensionEvolutionTests.test_exact_serialized_audit_bound_retains_full_unicode_snapshots_and_remains_readable``
    in ``test_ao_review_extension.py``, which deletes its first audit file before repeating the call
    under the same frozen time), so ``time.time`` is frozen and the direct call's own audit file is
    removed before the identical CLI-dispatched call recomputes and re-stores it, to make the two
    results byte-identical. The grant call is naturally idempotent on an identical replay and needs
    neither the freeze nor the deletion.
    """

    def test_cli_dispatch_prints_the_same_json_as_the_service_for_both_new_tools(self):
        self.propose_scope_change()
        controller = project_room.Service(self.home)
        spec = self.register()
        audit_inputs = {'room_id': self.room, 'spec_revision': 2, 'spec_sha256': spec['sha256'],
                        'scope_request_id': 'implementation', 'candidate_sha256': self.candidate_sha256(),
                        'native_session_id': self.NATIVE, 'native_owner_database': str(self.database)}
        with patch.object(ao, 'Service', return_value=self.service):
            with patch.object(extension.time, 'time', return_value=1_700_000_000.0):
                direct_audit = controller.call('ao_room_preacceptance_review_audit', audit_inputs)
                audit_path = (self.directory() / extension.BASE / 'audits'
                             / (direct_audit['audit_sha256'] + '.json'))
                audit_path.unlink()
                args_path = self.root / 'audit-args.json'
                args_path.write_text(json.dumps(audit_inputs))
                audit_output = io.StringIO()
                with redirect_stdout(audit_output), patch.object(project_room, 'Service', return_value=controller), \
                        patch.object(project_room.signal, 'signal'):
                    audit_exit_code = project_room.main(['--home', str(self.home), 'call',
                        'ao_room_preacceptance_review_audit', '--args-file', str(args_path)])
                self.assertEqual(audit_exit_code, 0)
                self.assertEqual(json.loads(audit_output.getvalue()), direct_audit)

            inputs = self.grant_inputs(audit=direct_audit)
            granted = controller.call('ao_room_preacceptance_review_extend', inputs)
            grant_args_path = self.root / 'grant-args.json'
            grant_args_path.write_text(json.dumps(inputs))
            grant_output = io.StringIO()
            with redirect_stdout(grant_output), patch.object(project_room, 'Service', return_value=controller), \
                    patch.object(project_room.signal, 'signal'):
                grant_exit_code = project_room.main(['--home', str(self.home), 'call',
                    'ao_room_preacceptance_review_extend', '--args-file', str(grant_args_path)])
        self.assertEqual(grant_exit_code, 0)
        self.assertEqual(granted['remaining_spec_reviews'], 1)
        self.assertEqual(json.loads(grant_output.getvalue()), granted)


PINNED = 'Review-extension room, native binding, authorization or pinned metadata changed'
NOT_STRICT_JSON = 'Native final response must be one unambiguous JSON object'
RECOVERY_MODIFIED = 'Committed reviewer recovery evidence or binding was modified'


class PreAcceptanceReviewerRecoveryFixture(PreAcceptanceFixture):
    """F1: a pre-acceptance grant leaves the bound Codex reviewer unused, so it stays recoverable.

    The reviewer is shaped before binding exactly as ``test_ao_reviewer_recovery`` shapes it (a native
    root branch and its own Git worktree); a separate empty replacement gets its own worktree too.
    """

    def bind(self):
        snapshot = self.fake.snapshots['reviewer']
        snapshot.update(mode='chat', harness='codex', activeBranchId=snapshot['conversationId'] + ':root',
                        latestSequence=0, nativeForkAvailableAfterSequence=0, hasMoreBefore=False,
                        branchedFromEarlierMessage=False)
        for name in ('reviewer', 'replacement'):
            target = self.root / (name + '-workspace')
            subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(target), 'HEAD'],
                           check=True, capture_output=True)
            self.fake.workspaces[name] = target
        super().bind()

    def verified_after_fourth(self):
        """Grant and consume the fourth review, then implement and verify the newly agreed charter."""
        self.propose_scope_change()
        self.grant()
        self.send('spec_review', 'fourth')
        self.assertTrue(self.finish_fourth()['agreement']['agreed'])
        self.implement_after_fourth()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])

    def recover_reviewer(self):
        """Stop the never-used reviewer, then audit and commit the one supported recovery."""
        self.fake.snapshots['reviewer']['controller'] = 'stopped'
        self.fake.add('replacement')
        snapshot = self.fake.snapshots['replacement']
        snapshot.update(controller='ready', mode='chat', harness='codex',
                        activeBranchId=snapshot['conversationId'] + ':root', latestSequence=0,
                        nativeForkAvailableAfterSequence=0, hasMoreBefore=False, branchedFromEarlierMessage=False,
                        branchMaterialization={'strategy': 'native', 'replayTruncated': False})
        self.fake.sessions['replacement']['isTerminated'] = False
        audited = self.service.ao_room_reviewer_recovery_audit(self.room)
        self.assertTrue(audited['eligible'])
        return self.service.ao_room_reviewer_recover(
            room_id=self.room, audit_sha256=audited['audit_sha256'], replacement_session_id='replacement',
            diagnosis='The never-used native reviewer thread stopped before any review',
            authorization='User approved the audited unused-reviewer recovery', request_id='unused-reviewer-1')

    def refuses_status_and_sends(self, pattern):
        for name, action in (('status', lambda: self.service.ao_room_status(self.room)),
                             ('acceptance', lambda: self.send('acceptance_review')),
                             ('spec-review', lambda: self.send('spec_review', 'fifth'))):
            with self.subTest(action=name):
                self.assert_refuses_without_mutation(pattern, action)


class PreAcceptanceReviewerRecoveryTests(PreAcceptanceReviewerRecoveryFixture):
    """F1 / R3: the ordinary lifecycle stays possible and only the exact committed recovery is accepted."""

    def test_recovered_unused_reviewer_keeps_the_grant_valid_through_independent_acceptance(self):
        self.verified_after_fourth()
        before = self.state()
        self.assertEqual(before['acceptances'], [])
        self.assertIsNone(before.get('reviewer_recovery'))
        recovered = self.recover_reviewer()
        after = self.state()
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['original_binding'], before['bindings']['reviewer'])
        self.assertEqual(after['bindings']['reviewer'], recovered['binding'])
        self.assertEqual(after['bindings']['reviewer']['session_id'], 'replacement')
        self.assertEqual(after['bindings']['engineer'], before['bindings']['engineer'])
        self.assertEqual(after['spec_review_extension'], before['spec_review_extension'])
        record, evidence = extension.validate(self.service, after)
        self.assertEqual(record['lane'], extension.PRE)
        self.assertEqual(evidence['state']['bindings']['reviewer'], before['bindings']['reviewer'])
        self.assertIsNone(evidence['state'].get('reviewer_recovery'))
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_extension']['lane'], extension.PRE)
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth')
        self.assertEqual(status['reviewer_recovery']['replacement_session_id'], 'replacement')
        self.send('acceptance_review')
        request = self.state()['requests']['acceptance_review']
        self.assertEqual(request['session_id'], 'replacement')
        self.fake.finish('replacement', json.dumps({**request['review'], 'decision': 'approved',
                                                    'review': 'Independently inspected exact behavior and gates.'}))
        self.service.ao_room_sync(self.room)
        accepted = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(accepted['accepted'])
        self.assertEqual(accepted['reviewer_session'], 'replacement')
        self.assertEqual(self.state()['acceptances'][-1]['candidate_sha256'], accepted['candidate_sha256'])
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth')
        self.assertEqual(extension.validate(self.service, self.state())[0], record)

    def test_forged_reviewer_binding_without_a_recovery_record_refuses_without_mutation(self):
        self.verified_after_fourth()
        original = self.state()
        forged = copy.deepcopy(original)
        forged['bindings']['reviewer'] = {**forged['bindings']['reviewer'], 'session_id': 'replacement'}
        ao.atomic(self.directory() / 'state.json', forged)
        try:
            self.refuses_status_and_sends(PINNED)
            with self.assertRaisesRegex(ao.RoomError, PINNED):
                extension.validate(self.service, forged)
        finally:
            ao.atomic(self.directory() / 'state.json', original)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_tampered_or_reforged_recovery_record_refuses(self):
        self.verified_after_fourth()
        baseline_reviewer = self.state()['bindings']['reviewer']
        self.recover_reviewer()
        original = self.state()
        reference = original['reviewer_recovery']
        receipt = self.directory() / reference['receipt']
        receipt_bytes = receipt.read_bytes()
        record = ao.read(receipt)
        other = {**baseline_reviewer, 'session_id': 'other-reviewer'}
        # The committed receipt's digest or original binding no longer matches its state reference.
        for name, tampered in (('digest', {**record, 'recorded_at': record['recorded_at'] + 1}),
                               ('original-binding', {**record, 'original': other})):
            ao.atomic(receipt, tampered)
            try:
                with self.subTest(tamper=name):
                    self.refuses_status_and_sends(RECOVERY_MODIFIED)
                    with self.assertRaisesRegex(ao.RoomError, RECOVERY_MODIFIED):
                        extension.validate(self.service, original)
            finally:
                receipt.write_bytes(receipt_bytes)
        # A valid record whose replacement is not the current reviewer binding fails its own validation.
        forged = copy.deepcopy(original)
        forged['bindings']['reviewer'] = {**forged['bindings']['reviewer'], 'session_id': 'third-reviewer'}
        with self.assertRaisesRegex(ao.RoomError, RECOVERY_MODIFIED):
            extension.validate(self.service, forged)
        # A chain the recovery module authenticates, re-forged so it recovers a reviewer other than the
        # grant baseline: the extension's own original-binding check refuses it.
        audits = self.directory() / 'reviewer-recovery' / 'audits'
        saved = ao.read(audits / (record['inputs']['audit_sha256'] + '.json'))
        forged_audit = {**saved, 'evidence': {**saved['evidence'], 'reviewer': other}}
        forged_audit_path = audits / (ao.digest(forged_audit) + '.json')
        recovery._store_once(forged_audit_path, forged_audit)
        inputs = {**record['inputs'], 'audit_sha256': ao.digest(forged_audit)}
        forged_record = {**record, 'inputs': inputs, 'original': other}
        forged = copy.deepcopy(original)
        forged['reviewer_recovery'] = {**reference, 'key': ao.digest(inputs), 'receipt_sha256': ao.digest(forged_record)}
        ao.atomic(receipt, forged_record)
        ao.atomic(self.directory() / 'state.json', forged)
        try:
            self.assertEqual(recovery.validate(self.service, forged), forged_record)
            self.refuses_status_and_sends(PINNED)
            with self.assertRaisesRegex(ao.RoomError, PINNED):
                extension.validate(self.service, forged)
        finally:
            ao.atomic(self.directory() / 'state.json', original)
            receipt.write_bytes(receipt_bytes)
            forged_audit_path.unlink()
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_recovery_replacement_matching_the_engineer_session_refuses_without_mutation(self):
        """A self-consistent forged chain cannot launder the engineer's own session as the reviewer.

        ``original`` is the real grant-time reviewer (passes the extension's own anchor check);
        ``replacement`` is the engineer binding itself, named in both the receipt and ``bindings.reviewer``.
        ``ao_reviewer_recovery.validate`` has no cross-room claim check of its own, so it authenticates this
        chain; only the extension's own added native-identity invariant refuses it.
        """
        self.verified_after_fourth()
        self.recover_reviewer()
        original = self.state()
        reference = original['reviewer_recovery']
        receipt = self.directory() / reference['receipt']
        receipt_bytes = receipt.read_bytes()
        record = ao.read(receipt)
        forged = copy.deepcopy(original)
        engineer_binding = forged['bindings']['engineer']
        forged_inputs = {**record['inputs'], 'replacement_session_id': engineer_binding['session_id']}
        forged_record = {**record, 'inputs': forged_inputs, 'replacement': engineer_binding}
        forged['reviewer_recovery'] = {**reference, 'key': ao.digest(forged_inputs),
                                       'receipt_sha256': ao.digest(forged_record)}
        forged['bindings']['reviewer'] = engineer_binding
        ao.atomic(receipt, forged_record)
        ao.atomic(self.directory() / 'state.json', forged)
        try:
            self.assertEqual(recovery.validate(self.service, forged), forged_record)
            self.refuses_status_and_sends(PINNED)
            with self.assertRaisesRegex(ao.RoomError, PINNED):
                extension.validate(self.service, forged)
        finally:
            ao.atomic(self.directory() / 'state.json', original)
            receipt.write_bytes(receipt_bytes)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)

    def test_recovery_replacement_equal_to_the_original_reviewer_refuses_without_mutation(self):
        """A self-consistent forged chain cannot claim a no-op ``replacement`` as an authenticated recovery.

        ``original`` and ``replacement`` are both the real grant-time reviewer binding, with
        ``reviewer_recovery`` present and ``bindings.reviewer`` unchanged; ``ao_reviewer_recovery.validate``
        still authenticates this chain, so only the extension's own added replacement-equals-original
        invariant refuses it.
        """
        self.verified_after_fourth()
        baseline_reviewer = self.state()['bindings']['reviewer']
        self.recover_reviewer()
        original = self.state()
        reference = original['reviewer_recovery']
        receipt = self.directory() / reference['receipt']
        receipt_bytes = receipt.read_bytes()
        record = ao.read(receipt)
        forged = copy.deepcopy(original)
        forged_inputs = {**record['inputs'], 'replacement_session_id': baseline_reviewer['session_id']}
        forged_record = {**record, 'inputs': forged_inputs, 'replacement': baseline_reviewer}
        forged['reviewer_recovery'] = {**reference, 'key': ao.digest(forged_inputs),
                                       'receipt_sha256': ao.digest(forged_record)}
        forged['bindings']['reviewer'] = baseline_reviewer
        ao.atomic(receipt, forged_record)
        ao.atomic(self.directory() / 'state.json', forged)
        try:
            self.assertEqual(recovery.validate(self.service, forged), forged_record)
            self.refuses_status_and_sends(PINNED)
            with self.assertRaisesRegex(ao.RoomError, PINNED):
                extension.validate(self.service, forged)
        finally:
            ao.atomic(self.directory() / 'state.json', original)
            receipt.write_bytes(receipt_bytes)
        self.assertEqual(self.service.ao_room_status(self.room)['spec_review_extension']['remaining_spec_reviews'], 0)


class AcceptedLaneReviewerRecoveryUnchangedTests(ReviewExtensionFixture):
    """F1 (accepted side): that lane's grant always retains an acceptance, so recovery stays ineligible."""

    room_files = PreAcceptanceFixture.room_files

    def test_reviewer_recovery_stays_ineligible_after_an_accepted_lane_grant(self):
        self.grant()
        self.fake.snapshots['reviewer']['controller'] = 'stopped'
        for stage in ('granted', 'consumed'):
            if stage == 'consumed':
                self.send('spec_review', 'fourth')
                self.finish_fourth()
            with self.subTest(stage=stage):
                before = self.room_files()
                with self.assertRaisesRegex(ao.RoomError, 'A reviewer was already used; recovery cannot replace it '
                                                          'or renew review attempts'):
                    self.service.ao_room_reviewer_recovery_audit(self.room)
                self.assertEqual(self.room_files(), before)
                self.assertIsNone(self.state().get('reviewer_recovery'))

    def test_accepted_lane_never_consults_a_recovery_for_pinned_drift(self):
        self.grant()
        state = self.state()
        original = state['bindings']['reviewer']
        state['bindings']['reviewer'] = {**original, 'session_id': 'replacement'}
        state['reviewer_recovery'] = {'request_id': 'unused-reviewer-1', 'key': '0' * 64,
                                      'receipt': 'reviewer-recovery/requests/unused-reviewer-1.json',
                                      'receipt_sha256': '0' * 64}
        authenticated = {'original': original, 'replacement': state['bindings']['reviewer']}
        with patch.object(recovery, 'validate', return_value=authenticated) as validated:
            with self.assertRaisesRegex(ao.RoomError, PINNED):
                extension.validate(self.service, state)
        validated.assert_not_called()


class PreAcceptanceNormalizedProposalTests(PreAcceptanceFixture):
    """F2: a response-normalized scope_change proposal retains its normalization evidence verbatim."""

    PROSE = 'The agreed charter cannot hold; a revised charter decision is needed. Final engineering report follows.\n\n'
    REVIEW = ('I read the complete final response and all surrounding prose. It adds no additional or contradictory '
              'verdict; the exact object contains the complete outcome.')

    def test_normalized_scope_change_proposal_audits_grants_and_consumes_with_its_normalization_evidence(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', self.PROSE + json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        before = self.state()['requests']['implementation']
        diagnostic = before['engineering_error']
        self.assertEqual(diagnostic, NOT_STRICT_JSON)
        self.assertNotIn('engineering_record', before)
        raw = ao_workflow.raw_final_text(self.directory(), before)
        start = raw.find('{')
        _, end = json.JSONDecoder().raw_decode(raw, start)
        posts = len(self.fake.posts)
        self.service.ao_room_response_normalize(self.room, 'implementation', before['receipt_sha256'],
                                                ao.digest(raw.encode()), start, end, self.REVIEW, True)
        self.assertEqual(len(self.fake.posts), posts)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['engineering_error'], diagnostic)
        self.assertTrue(request.get('engineering_record'))
        self.assertNotIn('normalization_capture_error', request)
        audited = self.audit()
        self.assertEqual(audited['scope_proposal']['evidence'], 'engineering_record')
        self.assertEqual(audited['scope_proposal']['engineering_error'], diagnostic)
        proposal = extension._audit(self.directory(), audited['audit_sha256'])['evidence']['retained']['scope_proposal']
        normalization = ao.read(self.directory() / request['response_normalization'])
        self.assertEqual(proposal['evidence'], 'engineering_record')
        self.assertEqual(proposal['engineering_error'], diagnostic)
        self.assertEqual(proposal['response_normalization'], request['response_normalization'])
        self.assertEqual(proposal['response_normalization_sha256'], request['response_normalization_sha256'])
        self.assertEqual(proposal['response_normalization_sha256'], ao.digest(normalization))
        self.assertIsNone(proposal['normalization_capture_error'])
        self.assertEqual(proposal['native_report_sha256'], normalization['result_sha256'])
        granted = self.service.ao_room_preacceptance_review_extend(**self.grant_inputs(audit=audited))
        self.assertEqual((granted['lane'], granted['remaining_spec_reviews']), (extension.PRE, 1))
        self.send('spec_review', 'fourth')
        status = self.finish_fourth()
        self.assertTrue(status['agreement']['agreed'])
        self.assertEqual(status['spec_review_extension']['consumed_by'], 'fourth')
        self.assertEqual(status['spec_review_extension']['remaining_spec_reviews'], 0)


class PreAcceptanceReportGapTests(PreAcceptanceFixture):
    """Review gaps: an incomplete strict-JSON proposal keeps its diagnostic; a stale handoff digest refuses."""

    INCOMPLETE = 'Engineering report is incomplete or refers to another spec/handoff'

    def test_incomplete_strict_json_scope_change_audits_with_its_rejected_report_diagnostic(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        report = self.scope_change_report()
        for field in ('changes', 'tests_reported', 'review_findings', 'backlog'):
            report.pop(field)
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(report))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['engineering_error'], self.INCOMPLETE)
        self.assertNotIn('engineering_record', request)
        self.assertIsNone(request.get('engineering_record_sha256'))
        audited = self.audit()
        self.assertEqual(audited['scope_proposal']['evidence'], 'rejected_report_diagnostic')
        self.assertEqual(audited['scope_proposal']['engineering_error'], self.INCOMPLETE)
        proposal = extension._audit(self.directory(), audited['audit_sha256'])['evidence']['retained']['scope_proposal']
        self.assertEqual(proposal['evidence'], 'rejected_report_diagnostic')
        self.assertEqual(proposal['engineering_error'], self.INCOMPLETE)
        self.assertIsNone(proposal['engineering_record'])
        self.assertIsNone(proposal['response_normalization'])

    def test_stale_handoff_digest_refuses_the_audit_without_mutation(self):
        self.propose_scope_change()
        self.register()
        original = self.state()
        candidate_sha256 = self.candidate_sha256()
        for name, change, message in (
                ('request', lambda s: s['requests']['implementation'].update(handoff_sha256='0' * 64), extension.SCOPE_REQUEST),
                ('state', lambda s: s.update(handoff_sha256='0' * 64), 'Engineering handoff is stale or changed')):
            stale = copy.deepcopy(original)
            change(stale)
            ao.atomic(self.directory() / 'state.json', stale)
            try:
                with self.subTest(digest=name):
                    self.assert_refuses_without_mutation(
                        message, lambda: self.audit(candidate_sha256=candidate_sha256))
                    self.assertEqual(list((self.directory() / extension.BASE / 'audits').glob('*.json')), [])
            finally:
                ao.atomic(self.directory() / 'state.json', original)


if __name__ == '__main__':
    unittest.main()
