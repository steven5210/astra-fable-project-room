"""Synthetic acceptance-review continuation compatibility probes; no account, provider or network access.

Three areas, reusing existing fixtures by cooperative inheritance rather than duplicating their setup:

- Delegate-evidence probes (spec bullet 5): a normal room with a synthetic deepseek delegate, driven to
  three retained reviewer attempts with a passed verification, whose completed engineering report names one
  verified completed delegate job. A positive control proves the intact/settled path is admitted with exactly
  one POST and that the saved audit evidence records the engineering delegation facts. Four fault categories
  (unsettled, foreign, missing, corrupt) each prove audit, extend and the named admission all refuse without
  consuming or posting, then that the same named review succeeds once the evidence is restored exactly.
- Provider-transition/routing/history compatibility (spec bullet 8): see the fixture and tests near the
  bottom of this file for what was actually representable from the existing fixtures, and what was not.
- Freeze-guard reachability for two operations whose own earlier gate hides ao_acceptance_extension.guard_unused
  behind an unrelated refusal in the plain acceptance-continuation fixture: ao_executable_binding.bind (its
  own _source_failure raises first) and ao_routing_refresh.refresh (its own _build raises first). Each is
  composed, by the same cooperative-inheritance technique, with the exact recipe an existing fixture already
  uses to get past that earlier gate (test_ao_executable_binding.py's claude_bin setup;
  test_ao_routing_refresh.py's deliberately-stale initial routing bundle), so guard_unused itself is reached
  and probed under a real unused grant, plus the no-grant positive control.
"""
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_acceptance_extension as extension
import ao_delegates
import ao_executable_binding
import ao_native_outcome
import ao_provider_transition
import ao_routing
import ao_routing_adoption
import ao_routing_guard
import ao_routing_refresh
import deepseek_adapter
from test_ao_acceptance_extension import AcceptanceContinuationFixture
from test_ao_adoption import AdoptionFixture
from test_ao_normal import DelegateFixture, Fixture


class DelegateAcceptanceContinuationFixture(AcceptanceContinuationFixture, DelegateFixture):
    """Three retained reviewer attempts and a passed verification (AcceptanceContinuationFixture) on a room
    with a synthetic deepseek delegate (DelegateFixture) whose completed engineering report names one
    verified completed delegate job.

    Cooperative multiple inheritance: DelegateFixture.setUp prepares the deepseek adapter configuration and
    opens the room with provider='deepseek'; AcceptanceContinuationFixture.setUp (next in the MRO) then
    drives spec agreement, implementation and the three reviewer attempts. `open` is overridden so
    AcceptanceContinuationFixture's internal `self.open()` (default provider='none') resolves to the same
    already-open deepseek room instead of conflicting with it (ao_room_open's idempotent-by-digest reopen
    would otherwise raise "Existing room delegate provider is immutable"). `implement` is overridden to
    record one verifiable completed delegate job in the synthetic ledger and to name it in the engineering
    report's routing_log, exactly as a real delegated implementation turn would, before the base class's
    implement() sends that report and syncs it.
    """

    def open(self, feature='normal', provider='none'):
        return super().open(feature, 'deepseek')

    def implement(self, **changes):
        self.ledger = deepseek_adapter.Ledger(self.home)
        self.delegate_model = ao_delegates.validate_provider(self.directory(), self.state())['delegate_settings']['model']
        self.delegate_profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        self.delegate_content = b'Synthetic delegate answer.\n'
        self.delegate_job_id = self.add_delegate_job(1, content=self.delegate_content)
        changes.setdefault('routing_log', [{'delegate_job_ids': [self.delegate_job_id]}])
        return super().implement(**changes)

    def add_delegate_job(self, number, job_state=deepseek_adapter.COMPLETED, room_id=None,
                          content=b'Synthetic delegate answer.\n'):
        """Insert one synthetic ledger row exactly as the pinned deepseek adapter would leave it (modeled on
        test_ao_provider_transition.ProviderTransitionTests.add_job). A COMPLETED job also gets its verifiable
        exported content bytes on disk at the first candidate path ao_delegates.verify_content checks, so
        ordinary content-digest verification passes without any real provider call."""
        job_id = format(number, '032x')
        profile = ao_delegates.expected_profile(self.state()['delegate']['inventory'])
        model = ao_delegates.validate_provider(self.directory(), self.state())['delegate_settings']['model']
        content_sha256 = ao.digest(content) if job_state == deepseek_adapter.COMPLETED else None
        with self.ledger.transaction() as db:
            db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,'
                       'created_at,requested_model,thinking,reasoning_effort,max_tokens,input_bytes,'
                       'possibly_billed,reserved_bytes,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, room_id or self.room, 'source-' + str(number), 'deep', 'a' * 64, profile,
                        job_state, '2026-01-01T00:00:' + str(number).zfill(2) + '+00:00', model, 'enabled',
                        'max', 393216, 10, 1, 1, content_sha256))
        if job_state == deepseek_adapter.COMPLETED:
            export = self.home / 'deepseek' / 'jobs' / job_id
            export.mkdir(parents=True, mode=0o700)
            (export / 'content').write_bytes(content)
        return job_id

    def _delete_job(self, job_id):
        with self.ledger.transaction() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))

    def _set_job(self, job_id, column, value):
        assert column in ('room_id', 'profile_sha256'), 'fixed, known-safe column allowlist; never caller/user input'
        with self.ledger.transaction() as db:
            db.execute('UPDATE jobs SET ' + column + '=? WHERE id=?', (value, job_id))

    def _lane_files(self):
        """Sorted relative paths of every file under the acceptance-review-extension lane directory, or None
        if that directory does not exist yet; the precise scope the task asks refusals to leave untouched."""
        base = self.directory() / extension.BASE
        if not base.exists():
            return None
        return sorted(str(p.relative_to(base)) for p in base.rglob('*') if p.is_file())

    def _probe_refusal(self, break_evidence, restore_evidence, fragment, review):
        """Prove audit, extend and the named admission all refuse -- using the real gate's own refusal
        fragment -- while the delegate evidence is broken, storing nothing new under the lane directory,
        writing no journal record, making no POST and leaving the grant's 'unconsumed' status alone; then
        restore the evidence exactly and show the same named review is admitted with exactly one POST."""
        posts = len(self.fake.posts)

        # (i) audit refuses and stores nothing new under the lane directory.
        before_lane = self._lane_files()
        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.audit(review)
            self.assertEqual(self._lane_files(), before_lane, 'a refused audit must store nothing new in the lane')
        finally:
            restore_evidence()

        # A valid audit now makes the room eligible; capture its audit_sha256 for the extend attempt.
        audit_sha256 = self.audit(review)['audit_sha256']
        inputs = dict(room_id=self.room, audit_sha256=audit_sha256, request_id='grant-' + review,
                      authorization='Actual synthetic user approval for needed additional reviews.',
                      authorization_reference='synthetic-user-message',
                      diagnosis='A necessary further independent review in the same scope.')

        # (ii) after a valid audit, the condition makes extend refuse and no journal record appears.
        before_lane = self._lane_files()
        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.service.ao_room_acceptance_review_extend(**inputs)
            self.assertEqual(self._lane_files(), before_lane, 'a refused extend must leave no journal record')
        finally:
            restore_evidence()

        # A valid grant now exists and is unconsumed.
        self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        self.assertEqual(len(self.fake.posts), posts)

        # (iii) after a valid grant, the condition makes the named send refuse: no consumption record, no
        # POST, status stays 'unconsumed'.
        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.send('acceptance_review', review)
            self.assertEqual(len(self.fake.posts), posts, 'a refused admission must never POST')
            self.assertNotIn(review, self.state()['requests'], 'a refused admission must leave no consumption record')
            self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        finally:
            restore_evidence()

        # Restored exactly: the named send now succeeds with exactly one POST.
        result = self.send('acceptance_review', review)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.finish_review(review, 'rejected')


class DelegateEvidencePositiveControlTests(DelegateAcceptanceContinuationFixture):
    def test_settled_delegate_evidence_admits_named_review_and_records_delegation_facts(self):
        """Intact, settled delegate evidence: the audit is eligible, a grant is issued, the named fourth send
        is admitted with exactly one POST, and the saved audit evidence records the engineering delegation
        facts (the completed job's verified digest, requested_model and profile_sha256)."""
        audit = self.audit()
        self.assertTrue(audit['eligible'])
        saved = extension._audit(self.directory(), audit['audit_sha256'])
        self.assertEqual(saved['evidence']['engineering']['delegation'],
                          [{'job_id': self.delegate_job_id, 'state': deepseek_adapter.COMPLETED,
                            'classification': 'completed', 'content_sha256': ao.digest(self.delegate_content),
                            'requested_model': self.delegate_model, 'profile_sha256': self.delegate_profile}])
        self.assertEqual(saved['evidence']['engineering']['delegate_job_ids'], [self.delegate_job_id])

        inputs, grant = self.grant()
        self.assertTrue(grant['extended'])
        self.assertEqual(grant['remaining_additional_reviews'], 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')

        posts = len(self.fake.posts)
        result = self.send('acceptance_review', 'fourth')
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')

        self.finish_review('fourth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fourth')['accepted'])


class DelegateEvidenceFaultTests(DelegateAcceptanceContinuationFixture):
    """Each condition below is defined and raised inside ao_delegates.py / ao_workflow.engineering_ready, at
    the exact call chain _inspect() -> ao_workflow.engineering_ready() shares across every phase (audit,
    extend, admission). assertRaisesRegex fragments are copied verbatim from those raise sites."""

    def test_a_unsettled_delegate_job_refuses_all_three_phases(self):
        """a. unsettled: a still-active job (ledger.active) and an unresolved stop-state job (ledger
        .stopping_jobs) both refuse via ao_delegates.assert_settled (ao_delegates.py:255)."""
        cases = (('active', deepseek_adapter.STREAMING), ('unresolved_stop', deepseek_adapter.UNKNOWN))
        for index, (label, job_state) in enumerate(cases):
            with self.subTest(unsettled=label):
                job_id = format(900 + index, '032x')

                def break_evidence(job_id=job_id, job_state=job_state, number=900 + index):
                    self.add_delegate_job(number, job_state=job_state)

                def restore_evidence(job_id=job_id):
                    self._delete_job(job_id)

                self._probe_refusal(break_evidence, restore_evidence,
                                    'Active or unresolved delegate jobs stop this room',
                                    review='fourth-unsettled-' + label)

    def test_b_foreign_delegate_job_refuses_all_three_phases(self):
        """b. foreign: the named job belongs to another room (ledger.job's room_id filter, converted in
        ao_delegates.verify_delegation to "unknown or foreign", ao_delegates.py:350) or was produced under a
        different pinned provider configuration (ao_delegates.py:352)."""
        with self.subTest(foreign='room'):
            def break_evidence():
                self._set_job(self.delegate_job_id, 'room_id', 'ao-foreign-unrelated-room')

            def restore_evidence():
                self._set_job(self.delegate_job_id, 'room_id', self.room)

            self._probe_refusal(break_evidence, restore_evidence,
                                'unknown or foreign delegate job', review='fourth-foreign-room')

        with self.subTest(foreign='profile'):
            real_profile = self.delegate_profile

            def break_evidence():
                self._set_job(self.delegate_job_id, 'profile_sha256', 'f' * 64)

            def restore_evidence(real_profile=real_profile):
                self._set_job(self.delegate_job_id, 'profile_sha256', real_profile)

            self._probe_refusal(break_evidence, restore_evidence,
                                "was not produced by this room's pinned provider configuration",
                                review='fourth-foreign-profile')

    def test_c_missing_delegate_evidence_refuses_all_three_phases(self):
        """c. missing: a pinned delegate file gone refuses via ao_delegates.validate_provider
        (ao_delegates.py:49-50,61-62); the preparation receipt gone refuses via ao_delegates.preparation
        (ao_delegates.py:86-87); the ledger file gone after recorded delegation refuses via
        ao_delegates.assert_settled (ao_delegates.py:247-249)."""
        with self.subTest(missing='pinned_delegate_file'):
            path = Path(list(self.state()['delegate']['files'])[0])
            original = path.read_bytes()

            def break_evidence(path=path):
                path.unlink()

            def restore_evidence(path=path, original=original):
                path.write_bytes(original)

            self._probe_refusal(break_evidence, restore_evidence,
                                'Delegate evidence is unreadable or inconsistent', review='fourth-missing-pinned')

        with self.subTest(missing='preparation_receipt'):
            prep_path = self.directory() / self.state()['preparation']
            original = prep_path.read_bytes()

            def break_evidence(prep_path=prep_path):
                prep_path.unlink()

            def restore_evidence(prep_path=prep_path, original=original):
                prep_path.write_bytes(original)

            self._probe_refusal(break_evidence, restore_evidence,
                                'Prepared delegate/workspace receipt is unreadable', review='fourth-missing-preparation')

        with self.subTest(missing='ledger_after_recorded_delegation'):
            ledger_path = self.ledger.path
            original = ledger_path.read_bytes()

            def break_evidence(ledger_path=ledger_path):
                ledger_path.unlink()

            def restore_evidence(ledger_path=ledger_path, original=original):
                ledger_path.write_bytes(original)

            self._probe_refusal(break_evidence, restore_evidence,
                                'Delegate ledger is missing after recorded delegation', review='fourth-missing-ledger')

    def test_d_corrupt_delegate_evidence_refuses_all_three_phases(self):
        """d. corrupt: pinned delegate file bytes changed refuses via ao_delegates.validate_provider's own
        digest check (ao_delegates.py:48-50); an unreadable/garbage ledger file refuses via
        ao_delegates.assert_settled's except clause (ao_delegates.py:256-257)."""
        with self.subTest(corrupt='pinned_delegate_file'):
            path = Path(self.state()['delegate']['policy_path'])
            original = path.read_bytes()

            def break_evidence(path=path, original=original):
                path.write_bytes(original + b'\ncorrupted-synthetic-drift\n')

            def restore_evidence(path=path, original=original):
                path.write_bytes(original)

            self._probe_refusal(break_evidence, restore_evidence,
                                'Pinned delegate configuration or policy changed', review='fourth-corrupt-pinned')

        with self.subTest(corrupt='ledger_file'):
            ledger_path = self.ledger.path
            original = ledger_path.read_bytes()

            def break_evidence(ledger_path=ledger_path):
                ledger_path.write_bytes(b'not a sqlite database at all, just synthetic garbage bytes 0123456789')

            def restore_evidence(ledger_path=ledger_path, original=original):
                ledger_path.write_bytes(original)

            self._probe_refusal(break_evidence, restore_evidence,
                                'Delegate ledger is unavailable', review='fourth-corrupt-ledger')


class ProviderTransitionAcceptanceContinuationFixture(AdoptionFixture):
    """A transitioned (epoch 2) room, with retained provider-transition and routing-adoption
    history, driven to three retained reviewer attempts against a passed verification.

    Subclasses AdoptionFixture (test_ao_adoption.py) directly rather than the base
    ProviderTransitionTests (test_ao_provider_transition.py): a v1-frozen-routing-only
    fixture commits a provider transition but can never reach a working post-transition
    engineer dispatch, because ao_provider_transition.dispatch_gate unconditionally
    requires ``directory / "delegate-launch.json"`` once ``state["provider_transition"]``
    is set (ao_provider_transition.py:750-752), and that file is only ever produced by a
    real MCP subprocess attachment (ao_delegate_launcher.record_launch, or here,
    AdoptionFixture.start_attachment's synthetic stdio-only JSON-RPC handshake simulation
    -- no network socket is used). AdoptionFixture is therefore the smallest existing
    fixture that reaches a genuinely dispatchable epoch-2 room, matching the pattern
    already exercised end to end by ReviewerRecoveryAdoptionTests in
    test_ao_provider_transition.py.

    The acceptance-review-extension helper methods below (finish_review/audit/
    grant_inputs/grant) intentionally reproduce AcceptanceContinuationFixture's own
    (test_ao_acceptance_extension.py) rather than reusing it by inheritance: that
    fixture's setUp implements immediately after bind(), while AdoptionFixture's setUp
    must configure_routing()/start_attachment() before implement() -- the two setUp
    sequences are not cooperative under multiple inheritance, so the smallest honest
    equivalent reproduces these few small helpers rather than forcing an incompatible
    MRO or editing either existing file.
    """

    prior_attempt_numbers = (1, 2, 3)

    def setUp(self):
        super().setUp()
        prepared = self.configure_routing()
        self.attachment = self.start_attachment(prepared)
        self.fake.snapshots['engineer']['controller'] = 'ready'
        self.implement()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        # Independent acceptance does not depend on a currently running engineer
        # attachment (mirrors ReviewerRecoveryAdoptionTests in test_ao_provider_transition.py).
        self.attachment.stdin.close()
        self.attachment.wait(timeout=10)
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.review_repo = self.root / 'review-repo'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '--detach', str(self.review_repo), 'HEAD'],
                       check=True, capture_output=True)
        self.fake.workspaces['reviewer'] = self.review_repo
        for number in self.prior_attempt_numbers:
            self.prior_attempt(number)
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

    def prior_attempt(self, number):
        """One ordinary retained reviewer attempt; overridable per-number for the settled-failure probe."""
        key = 'prior-' + str(number)
        self.send('acceptance_review', key)
        self.finish_review(key, 'rejected')

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

    def _record_files(self, base):
        directory = self.directory() / base
        if not directory.exists():
            return {}
        return {str(p.relative_to(self.directory())): p.read_bytes() for p in directory.rglob('*') if p.is_file()}


class ProviderTransitionCompatibilityTests(ProviderTransitionAcceptanceContinuationFixture):
    def test_continuation_preserves_transition_and_adoption_records_and_reports_consumed(self):
        transition_before = self._record_files(ao_provider_transition.BASE)
        adoption_before = self._record_files(ao_routing_adoption.BASE)
        self.assertTrue(transition_before, 'the transitioned room must already carry provider-transition records')
        self.assertTrue(adoption_before, 'the transitioned room must already carry routing-adoption records')
        self.assertFalse((self.directory() / 'routing-refresh').exists(),
                          'no routing refresh was performed in this fixture; nothing to compare')
        audit = self.audit()
        self.assertTrue(audit['eligible'])
        inputs, grant = self.grant()
        self.assertTrue(grant['extended'])
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        posts = len(self.fake.posts)
        result = self.send('acceptance_review', 'fourth')
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.finish_review('fourth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fourth')['accepted'])
        self.assertEqual(self._record_files(ao_provider_transition.BASE), transition_before)
        self.assertEqual(self._record_files(ao_routing_adoption.BASE), adoption_before)
        # Still validates as a normal committed provider transition with intact routing adoption.
        ao_provider_transition.validate(self.service, self.state())
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['provider_transition']['state'], 'committed')
        self.assertEqual(status['provider_transition']['epoch'], 2)
        self.assertEqual(status['acceptance_review_extension']['state'], 'consumed')


class ProviderTransitionUnusedGrantTests(ProviderTransitionAcceptanceContinuationFixture):
    def test_provider_and_routing_audits_refuse_before_any_write_while_grant_unused(self):
        """Mirrors test_provider_and_routing_changes_cannot_invalidate_unused_grant
        (test_ao_acceptance_extension.py:451-461) on a genuinely transitioned/adopted room:
        guard_unused (ao_acceptance_extension.py:702-707) is the first substantive check in
        both ao_provider_transition._inspect (ao_provider_transition.py:361-366) and
        ao_routing_adoption._inspect (ao_routing_adoption.py:241-246), firing before either
        module's own "already used"/"already pending" checks, so both refuse for the
        unused-grant reason even though this room already carries a committed transition.
        """
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
        # ao_routing_refresh.refresh (routing-refresh, the third named operation) is not
        # attempted here: it is deliberately CLI-only, never exposed as an ao_room_* Service
        # method (ao_routing_refresh.py:1 docstring; grep of ao_project_room.py finds no
        # ao_room_routing_refresh), and no existing fixture in this suite ever combines it
        # with a transitioned/adopted room (test_ao_routing_refresh.py's own fixture is
        # ReviewExtensionFixture, a room that never adopts v2 routing or a provider
        # transition at all). Its own refresh() reaches ao_acceptance_extension.guard_unused
        # only after building a target routing bundle and comparing it against the
        # currently-installed one (ao_routing_refresh.py:219-246, _build()); on an untouched
        # adopted room those bundles are identical, so refresh() raises "Routing already
        # matches the installed bundle; no refresh is needed" (ao_routing_refresh.py:244-246)
        # before ever reaching guard_unused. Reaching the guard would first require
        # synthesizing a genuine routing-bundle drift purely to defeat that unrelated
        # early guard -- machinery with no existing-fixture precedent and orthogonal to the
        # acceptance-extension lane under test -- so this one sub-case is reported here
        # rather than forced.


class SettledQuotaFailureAttemptTests(ProviderTransitionAcceptanceContinuationFixture):
    """An epoch-2 reviewer attempt that ends as a settled quota failure ('settled_failure')
    still counts toward the three retained reviewer-role intents: docs/acceptance-review-
    continuation.md states 'The first grant requires exactly three retained reviewer-role
    intents, including rejected or failed attempts.' ao_acceptance_extension._reviewer_requests
    (ao_acceptance_extension.py:115-119) counts every role=='reviewer' request regardless of
    its state, and the evidence-building code (ao_acceptance_extension.py:555-561) reads each
    retained attempt's fields with .get(...), tolerating a settled-failure attempt that never
    received a normal reviewer verdict.

    Settling 'prior-2' as a quota failure authorizes exactly one named successor
    (ao_outcomes.gate, ao_outcomes.py:294-305, releases the semantic hold only for the
    exact resume_request_id named by ao_room_outcome_resume); that successor is itself a
    fresh role=='reviewer' request, so it -- not a separate third 'prior-3' -- is the room's
    third retained attempt (prior-1, prior-2 settled_failure, prior-2-resume rejected = 3).
    """

    prior_attempt_numbers = (1, 2)

    def prior_attempt(self, number):
        if number != 2:
            return super().prior_attempt(number)
        key = 'prior-' + str(number)
        self.send('acceptance_review', key)
        self.fake.finish('reviewer', state='failed')
        self.service.ao_room_sync(self.room)
        state = self.state()
        # ao_native_outcome.source_keys('reviewer') (ao_native_outcome.py:229-230) names this
        # key native_reviewer_outcome_source, distinct from the engineer's native_outcome_source.
        state['native_reviewer_outcome_source'] = {'session_id': 'reviewer'}
        ao.atomic(self.directory() / 'state.json', state)
        observed = {'anchor_uuid': 'owned-native-packet', 'next_human_uuid': None,
                    'errors': [{'uuid': 'native-quota', 'error': 'rate_limit', 'http_status': 429}],
                    'stop_reasons': [], 'source_sha256': 'f' * 64}
        resume_key = key + '-resume'
        with patch.object(ao_native_outcome, 'inspect', return_value=observed):
            outcome_audit = self.service.ao_room_outcome_audit(self.room, role='reviewer')
            self.assertTrue(outcome_audit['resume_eligible'])
            self.service.ao_room_outcome_resume(self.room, key, outcome_audit['outcome_sha256'], resume_key,
                                                'Native quota rejection identified; user confirmed availability',
                                                'Continue once necessary')
            request = self.state()['requests'][key]
            self.assertEqual(request['state'], 'settled_failure')
            self.quota_request_id = key
            self.quota_receipt_path = self.directory() / request['receipt']
            self.quota_receipt_before = self.quota_receipt_path.read_bytes()
            self.quota_outcome_path = self.directory() / request['outcome_resume']
            self.quota_outcome_before = self.quota_outcome_path.read_bytes()
            # The named successor authorized by the resume; ao_outcomes.gate (ao_outcomes.py:
            # 294-305) re-observes the still-blocked 'prior-2' request when dispatching its
            # named successor, so the same patched native evidence must still be active here.
            self.send('acceptance_review', resume_key)
        # Clear the sticky per-role native evidence source once its one diagnosis is done:
        # source_keys('reviewer') matches by session_id alone (ao_outcomes.py:182), and every
        # reviewer request shares the one bound reviewer session_id, so leaving it set would
        # make every later reviewer completion attempt a real (unpatched, and here absent)
        # native inspection instead of an ordinary decision-only classification.
        cleared = self.state()
        del cleared['native_reviewer_outcome_source']
        ao.atomic(self.directory() / 'state.json', cleared)
        self.finish_review(resume_key, 'rejected')

    def test_settled_quota_failure_attempt_counts_toward_three_and_stays_byte_identical(self):
        self.assertEqual(sum(r['role'] == 'reviewer' for r in self.state()['requests'].values()), 3)
        self.assertEqual(self.state()['requests'][self.quota_request_id]['state'], 'settled_failure')
        audit = self.audit()
        self.assertTrue(audit['eligible'])
        self.assertEqual(audit['retained_reviewer_attempts'], 3)
        saved = extension._audit(self.directory(), audit['audit_sha256'])
        matched = [entry for entry in saved['evidence']['reviews'] if entry['request_id'] == self.quota_request_id]
        self.assertEqual(len(matched), 1, 'the settled-failure attempt must be retained as one of the three reviews')
        inputs, grant = self.grant()
        self.assertTrue(grant['extended'])
        self.assertEqual(self.quota_receipt_path.read_bytes(), self.quota_receipt_before)
        self.assertEqual(self.quota_outcome_path.read_bytes(), self.quota_outcome_before)
        posts = len(self.fake.posts)
        result = self.send('acceptance_review', 'fourth')
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.finish_review('fourth', 'approved')
        self.assertTrue(self.service.ao_room_accept(self.room, 'fourth')['accepted'])
        self.assertEqual(self.quota_receipt_path.read_bytes(), self.quota_receipt_before)
        self.assertEqual(self.quota_outcome_path.read_bytes(), self.quota_outcome_before)


class OwnerSourceRestorationTests(AcceptanceContinuationFixture):
    """Exercise source-path diagnosis with real synthetic owner storage and a stubbed native parser."""

    def test_outcome_audit_source_path_drift_refuses_then_restores_named_send(self):
        alternate = self.root / 'synthetic-owner-copy.db'
        alternate.write_bytes(self.database.read_bytes())
        transcript = self.root / 'synthetic-native-engineer.jsonl'
        transcript.write_text('Synthetic native parser boundary.\n')

        def inspect(directory, state, request, source, snapshot):
            return {'anchor_uuid': 'synthetic-owned-packet', 'next_human_uuid': None,
                    'errors': [], 'stop_reasons': ['end_turn'], 'source': dict(source),
                    'source_sha256': ao.digest(transcript.read_bytes()), 'compaction_imports': []}

        def audit_source(path):
            return self.service.ao_room_outcome_audit(self.room, role='engineer',
                       ao_database_path=str(path), native_transcript_path=str(transcript))

        with patch.object(ao_native_outcome, 'inspect', side_effect=inspect):
            audit_source(self.database)
            self.grant()
            lane = self.directory() / extension.BASE
            journal = {str(p.relative_to(lane)): p.read_bytes()
                       for p in lane.rglob('*') if p.is_file()}
            posts = len(self.fake.posts)
            audit_source(alternate)
            self.assertEqual(self.state()['native_outcome_source']['database'], str(alternate))
            with self.assertRaisesRegex(ao.RoomError,
                                        'Retained native source evidence contradicts the supplied owner database or session'):
                self.send('acceptance_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts)
            self.assertNotIn('fourth', self.state()['requests'])
            self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
            self.assertEqual({str(p.relative_to(lane)): p.read_bytes()
                              for p in lane.rglob('*') if p.is_file()}, journal)
            audit_source(self.database)
            self.assertEqual(self.state()['native_outcome_source']['database'], str(self.database))
            self.send('acceptance_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts + 1)
            self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')


class ExecutableRepairClaudeFixture(Fixture):
    """A real, valid claude_bin routing pin, present before bind() so AcceptanceContinuationFixture's own
    bind() call (next in the cooperative MRO below; ao_delegates.prepare -> ao_routing.prepare,
    ao_delegates.py:178) freezes a genuine executable fingerprint into the room's one immutable
    preparation record, instead of the plain fixture's absent/error placeholder
    (ao_routing.claude_evidence returns {'error': 'no configured claude_bin', ...} when claude_bin is
    unset; ao_routing.py:453-454). Mirrors ExecutableBindingTests.setUp's own claude_bin/executable setup
    (test_ao_executable_binding.py:16-23) -- not its TestCase, so its own tests are not re-collected here.
    """

    def setUp(self):
        super().setUp()
        self.original_claude = self.root / 'original-claude'
        self.original_claude.write_text('#!/bin/sh\nprintf "original (Claude Code)\\n"\n')
        self.original_claude.chmod(0o700)
        ao.atomic(self.home / 'config.json',
                 {'claude_bin': str(self.original_claude), 'claude_config_dir': str(self.claude_env)})


class ExecutableRepairUnusedGrantFixture(AcceptanceContinuationFixture, ExecutableRepairClaudeFixture):
    """Cooperative multiple inheritance (see DelegateAcceptanceContinuationFixture above):
    AcceptanceContinuationFixture.setUp's own super().setUp() call (next in this class's MRO) resolves to
    ExecutableRepairClaudeFixture.setUp, pinning a valid claude_bin before AcceptanceContinuationFixture's
    own body calls self.bind() -- so ao_executable_binding.bind's _source_failure
    (ao_executable_binding.py:135-146) sees a previously recorded successful identity instead of
    immediately refusing with 'Executable repair requires a previously recorded successful identity'
    (the plain fixture's outcome, per test_ao_acceptance_extension.py's
    AcceptanceFreezeListProbeTests.test_executable_binding_repair_is_unreachable_before_source_failure_gate).

    The engineer controller is then forced to 'stopped', which ao_executable_binding._inspect requires
    (ao_executable_binding.py:182-183, 'Stop the idle native engineer through AO before executable
    repair') and which ao_provider_transition._native also accepts (it allows either 'ready' or stopped),
    so the acceptance-review lane's own live checks used by self.grant() stay satisfied too. The original
    executable is then deleted -- a diagnosed loss that makes _source_failure return 'missing' rather than
    raise (ao_executable_binding.py:141-142) -- and a replacement executable plus its launch symlink are
    prepared so bind() can run all the way to guard_native_owner/guard_unused
    (ao_executable_binding.py:227-230). The existing synthetic native-owner database (self.database) is
    reused unchanged as bind()'s database_path: it already carries a correct engineer row, so
    ao_executable_binding._inspect's own read_owner check (ao_executable_binding.py:186-189) passes without
    any additional setup.
    """

    def setUp(self):
        super().setUp()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.replacement_claude = self.root / 'replacement-claude'
        self.replacement_claude.write_text('#!/bin/sh\nprintf "replacement (Claude Code)\\n"\n')
        self.replacement_claude.chmod(0o700)
        self.launch_claude = self.root / 'launch-claude'
        self.launch_claude.symlink_to(self.replacement_claude)
        self.original_claude.unlink()  # diagnosed loss of the previously bound executable
        self.repair_args = dict(room_id=self.room, request_id='repair-native',
                                executable_path=str(self.replacement_claude), launch_path=str(self.launch_claude),
                                database_path=str(self.database),
                                authorization='Actual synthetic user authorization for this repair',
                                diagnosis='Synthetic diagnosed loss of the previously bound native executable')

    def _room_tree(self):
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}


class ExecutableRepairUnusedGrantTests(ExecutableRepairUnusedGrantFixture):
    def test_no_grant_positive_control_gets_past_guard_unused_and_repairs(self):
        """Without any acceptance-review grant, guard_unused (ao_acceptance_extension.py:709-714) returns
        immediately (committed is None), so the repair proceeds and actually succeeds."""
        posts = len(self.fake.posts)
        result = ao_executable_binding.bind(self.service, **self.repair_args)
        self.assertFalse(result['model_dispatch'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(result['claude']['path'], str(self.replacement_claude))
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue((self.directory() / 'executable-bindings' / 'repair-native.json').exists())

    def test_repair_refuses_under_an_unused_grant_before_any_write(self):
        """With a valid unused grant made before the repair is attempted, ao_executable_binding.bind
        reaches its own guard_unused call and refuses with the freeze message, writing nothing."""
        self.grant('fourth')
        before = self._room_tree()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError,
                                    'unused acceptance-review grant freezes this room until its one named review '
                                    'is dispatched; executable binding repair was refused before any write'):
            ao_executable_binding.bind(self.service, **self.repair_args)
        self.assertEqual(self._room_tree(), before, 'a refused repair must write nothing to the room tree')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')


_STALE_ROUTING_GUARD = (b'"""Synthetic historical routing guard content, distinct from the installed one."""\n'
                        b'import json, sys\njson.loads(sys.stdin.read())\n')


class HistoricalRoutingFixture(Fixture):
    """Deliberately captures a v2 routing bundle using stale guard bytes, stale agent definitions and a
    suppressed foreground-settings override at bind() time, mirroring RoutingRefreshTests.setUp's own
    patch scope (test_ao_routing_refresh.py:33-42) -- not its TestCase, so its own tests are not
    re-collected here. A later, unpatched ao_routing_refresh.refresh call then genuinely finds its
    freshly-built target bundle differs from this stale installed one, so ao_routing_refresh._build
    (ao_routing_refresh.py:219-246) does not immediately refuse with 'Routing already matches the
    installed bundle; no refresh is needed' (ao_routing_refresh.py:245) before ever reaching guard_unused
    (ao_routing_refresh.py:300) -- the plain fixture's outcome, per the docstring on
    ProviderTransitionUnusedGrantTests.test_provider_and_routing_audits_refuse_before_any_write_while_grant_unused
    above.
    """

    def setUp(self):
        super().setUp()
        self.native_cli = self.root / 'native-claude-version'
        self.native_cli.write_text('#!/bin/sh\nprintf "synthetic (Claude Code)\\n"\n')
        self.native_cli.chmod(0o700)
        ao.atomic(self.home / 'config.json',
                 {'claude_bin': str(self.native_cli), 'claude_config_dir': str(self.claude_env)})

    def bind(self):
        original_read_bytes = Path.read_bytes
        guard_source = Path(ao_routing_guard.__file__)

        def stale_read_bytes(path):
            return _STALE_ROUTING_GUARD if path == guard_source else original_read_bytes(path)

        original_definition = ao_routing.agent_definition

        def stale_definition(name):
            return original_definition(name) + '\nSynthetic archived worker instructions.\n'

        # Scoped to only this call: everything AcceptanceContinuationFixture does afterwards (spec
        # agreement, implementation, the three reviewer attempts) sees the real, unpatched functions.
        with patch.object(Path, 'read_bytes', stale_read_bytes), \
             patch.object(ao_routing, 'agent_definition', side_effect=stale_definition), \
             patch.object(ao_routing, 'foreground_settings', side_effect=lambda settings: settings):
            super().bind()


class RoutingRefreshUnusedGrantFixture(AcceptanceContinuationFixture, HistoricalRoutingFixture):
    """Cooperative multiple inheritance, the same shape as ExecutableRepairUnusedGrantFixture above:
    AcceptanceContinuationFixture.setUp's own super().setUp() call (next in this class's MRO) resolves to
    HistoricalRoutingFixture.setUp, and its overridden bind() then wraps AcceptanceContinuationFixture's
    internal self.bind() call so the room's one immutable preparation captures the deliberately stale
    bundle. The existing synthetic native-owner database (self.database) is reused unchanged as
    refresh()'s database_path/native_session_id: ao_routing_refresh._inspect's own owner check
    (ao_routing_refresh.py:293-297) passes against the unchanged engineer row.
    """

    def setUp(self):
        super().setUp()
        self.fake.snapshots['engineer']['controller'] = 'stopped'
        self.refresh_args = dict(room_id=self.room, request_id='refresh-native', database_path=str(self.database),
                                 native_session_id='synthetic-native-engineer',
                                 authorization='User authorizes the routing correction for the stopped engineer.',
                                 diagnosis='Retained routing predates bounded-worker instructions.')

    def _room_tree(self):
        return {str(p.relative_to(self.directory())): p.read_bytes()
                for p in self.directory().rglob('*') if p.is_file()}


class RoutingRefreshUnusedGrantTests(RoutingRefreshUnusedGrantFixture):
    def test_no_grant_positive_control_gets_past_guard_unused_and_refreshes(self):
        """Without any acceptance-review grant, guard_unused (ao_acceptance_extension.py:709-714) returns
        immediately (committed is None), so the refresh proceeds and actually succeeds."""
        posts = len(self.fake.posts)
        result = ao_routing_refresh.refresh(self.service, **self.refresh_args)
        self.assertFalse(result['model_dispatch'])
        self.assertFalse(result['idempotent'])
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue((self.directory() / 'routing-refresh' / 'refresh-native.json').exists())

    def test_refresh_refuses_under_an_unused_grant_before_any_write(self):
        """With a valid unused grant made before the refresh is attempted, ao_routing_refresh.refresh
        reaches its own guard_unused call and refuses with the freeze message, writing nothing."""
        self.grant('fourth')
        before = self._room_tree()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError,
                                    'unused acceptance-review grant freezes this room until its one named review '
                                    'is dispatched; routing refresh was refused before any write'):
            ao_routing_refresh.refresh(self.service, **self.refresh_args)
        self.assertEqual(self._room_tree(), before, 'a refused refresh must write nothing to the room tree')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')


if __name__ == '__main__':
    unittest.main()
