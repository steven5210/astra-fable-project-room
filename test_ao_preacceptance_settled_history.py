"""The pre-acceptance lane's settled-history opt-in: engineer-owned, historically-settled native
quota failures (``state == 'settled_failure'``) admitted into native history evidence.

Covers ``ao_review_extension.py``'s ``_native_evidence``, ``_settled_failures`` and the
``_HistoryEvidence.proof`` failed-turn admission, scoped to the pre-acceptance lane
(``extension.PRE``) and to the bound engineer role only. Every other lane and role keeps the
original baseline behaviour in ``ao_provider_transition._native``: a native history turn that is
not completed, settled-quota or a proven recovered import refuses with the exact message

    "Native history contains a turn that is not completed (state failed); failed, interrupted,
    cancelled, recovered or active work refuses"

Synthetic data only: no real user names, machine paths, room ids or private context.
"""
import copy
import json
import threading
from unittest.mock import patch

import ao_history_reconciliation as reconciliation
import ao_native_outcome as native_outcome
import ao_outcomes
import ao_project_room as ao
import ao_provider_transition
import ao_review_extension as extension
from test_ao_preacceptance_review_extension import PreAcceptanceFixture, PreAcceptanceReviewerRecoveryFixture
from test_ao_review_extension import ReviewExtensionFixture


def _quota_native(tag):
    """A fresh, structurally valid native quota-failure observation for ``inspect()`` to return."""
    return {'anchor_uuid': 'owned-native-packet-' + tag, 'next_human_uuid': None,
            'errors': [{'uuid': 'native-quota-' + tag, 'error': 'rate_limit', 'http_status': 429}],
            'stop_reasons': [], 'source_sha256': 'f' * 64}


def _build_settled_history(fixture):
    """Shared by ``SettledHistoryFixture`` and ``SettledHistoryReviewerRecoveryFixture`` below: the
    handoff and the full settled-failure/completed-turn history build and registration, run after
    whatever binding the calling fixture's own ``setUp()``/``bind()`` has already established."""
    fixture.service.ao_room_handoff(fixture.room, str(fixture.repo))
    fixture._id_seq = 0
    fixture.settled = []    # Ordered list of {'request_id', 'turn_id', 'successor_request_id'}.
    fixture.completed = []  # Ordered list of completed implementation/correction request ids.
    fixture._build_history()


class SettledHistoryFixture(PreAcceptanceFixture):
    """A pre-acceptance room with a long mixed engineer history: many settled quota failures
    interleaved with completed implementation/correction turns, ending in a completed scope-change
    report and ``register()``.

    ``ao_workflow.packet()`` (ao_workflow.py, send-admission; see its "Use implementation once per
    handoff, then correction only for known completed work" message around line 758) requires the
    first-ever engineer send under one retained handoff to be purpose ``implementation`` and every
    later one to be purpose ``correction``, each only once its immediate predecessor has reached a
    terminal ``completed`` or ``settled_failure`` state. No cap on the number of corrections under
    one handoff was found by inspection (grepped ``ao_workflow.py``/``ao_project_room.py`` for a
    correction-count bound); the one cited, verified cap that *does* apply is the charter-review
    limit, ``extension.LIMIT = 3`` (checked by ``_inspect``), already satisfied by this fixture's
    inherited three ``self.agree()`` charter rounds before any of this history is built.
    """

    SETTLED_FAILURES_PER_CYCLE = 2
    CYCLES = 12
    SETTLED_FAILURES = SETTLED_FAILURES_PER_CYCLE * CYCLES  # 24: >= the required 24 settled failures.
    COMPLETED_PER_CYCLE_TURNS = CYCLES  # 12 interleaved completed corrections, besides the final one.
    FINAL_COMPLETED_TURNS = 1  # The completed scope-change-report turn.
    COMPLETED_TURNS = COMPLETED_PER_CYCLE_TURNS + FINAL_COMPLETED_TURNS  # 13: >= the required 12.

    def setUp(self):
        super().setUp()
        _build_settled_history(self)

    def _fresh_id(self):
        self._id_seq += 1
        return 'history-turn-%d' % self._id_seq

    def _settle(self, request_id, purpose, presend=True):
        """Send (unless already sent), fail, and authorize-settle one native quota failure.

        Returns its reserved successor id, already sent as the next 'correction' turn.
        """
        if presend:
            self.send(purpose, request_id)
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests'][request_id]['state'], 'uncertain')
        successor = self._fresh_id()
        with patch.object(native_outcome, 'inspect', return_value=_quota_native(request_id)):
            audit = self.service.ao_room_outcome_audit(self.room)
            self.assertEqual(audit['outcome']['kind'], 'quota_limit')
            self.assertTrue(audit['resume_eligible'])
            self.service.ao_room_outcome_resume(
                self.room, request_id, audit['outcome_sha256'], successor,
                'Native quota rejection identified at ' + request_id + '; user confirmed availability.',
                'User approved the exact one-time continuation for ' + request_id + '.')
            request = self.state()['requests'][request_id]
            self.assertEqual(request['state'], 'settled_failure')
            self.settled.append({'request_id': request_id, 'turn_id': request['turn_id'],
                                 'successor_request_id': successor})
            # ao_outcomes.gate() (ao_outcomes.py:1006) re-observes the latest-for-role request
            # fresh on every later send and only bypasses its hold when that fresh reading names
            # the exact reserved successor and is not inconclusive, so the successor's own send
            # must happen inside this same mocked window -- exactly like the reference recipe in
            # test_ao_outcomes.py's test_failed_ao_turn_needs_exact_native_quota_and_authorized_settlement,
            # which sends its 'resume' successor before leaving the patched inspect() context.
            self.send('correction', successor)
        return successor

    def _complete(self, request_id, purpose, presend=True, **changes):
        """Finish (and send, unless already sent) one normally completed implementation/correction."""
        if presend:
            self.send(purpose, request_id)
        self.fake.finish('engineer', json.dumps(self.report(**changes)))
        self.service.ao_room_sync(self.room)
        request = self.state()['requests'][request_id]
        self.assertEqual(request['state'], 'completed')
        self.completed.append(request_id)

    def _build_history(self):
        pending_id = self._fresh_id()
        presend = True
        purpose = 'implementation'
        for _cycle in range(self.CYCLES):
            for _slot in range(self.SETTLED_FAILURES_PER_CYCLE):
                pending_id = self._settle(pending_id, purpose, presend=presend)
                purpose = 'correction'
                presend = False  # _settle already sent its successor inside the mocked window.
            self._complete(pending_id, purpose, presend=presend)
            purpose = 'correction'
            pending_id = self._fresh_id()
            presend = True
        self.assertEqual(len(self.settled), self.SETTLED_FAILURES)
        self.assertEqual(len(self.completed), self.COMPLETED_PER_CYCLE_TURNS)
        self.final_request_id = pending_id
        self.send(purpose, self.final_request_id)
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests'][self.final_request_id]['state'], 'completed')
        self.completed.append(self.final_request_id)
        self.assertEqual(len(self.completed), self.COMPLETED_TURNS)
        self.register()

    def audit(self, **changes):
        # PreAcceptanceFixture.audit() defaults scope_request_id/candidate_sha256 to the
        # 'implementation' request id; this fixture's retained scope-change proposal is instead
        # self.final_request_id, so both defaults are overridden together (the lazy
        # candidate_sha256 default in the parent would otherwise silently read the wrong file).
        changes.setdefault('scope_request_id', self.final_request_id)
        if 'candidate_sha256' not in changes:
            changes['candidate_sha256'] = self.candidate_sha256(self.final_request_id)
        return super().audit(**changes)

    def saved_audit(self, audit_sha256):
        return ao.read(self.directory() / extension.BASE / 'audits' / (audit_sha256 + '.json'))

    def expected_settled_failures(self):
        """The exact settled_failures entry list this fixture's engineer history should produce,
        independently recomputed from retained request/release evidence (not from production's own
        _settled_failures helper), ordered like the production code orders it: by turn_id."""
        directory = self.directory()
        state = self.state()
        entries = []
        for item in self.settled:
            request = state['requests'][item['request_id']]
            release = ao_outcomes.release_record(directory, {'room_id': directory.name}, request)
            entries.append({'request_id': request['request_id'], 'turn_id': request['turn_id'],
                            'provider_turn_id': ao.native_turn_identity(request),
                            'settlement_kind': (release.get('native_failure_settlement') or {}).get('kind', 'quota_limit'),
                            'receipt_sha256': request['receipt_sha256'],
                            'outcome_resume': request['outcome_resume'],
                            'outcome_resume_sha256': request['outcome_resume_sha256'],
                            'outcome_sha256': release['outcome_sha256']})
        entries.sort(key=lambda entry: entry['turn_id'])
        return entries

    def _assert_fourth_send_refuses(self, pattern):
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, pattern):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertNotIn('fourth', self.state()['requests'])
        self.assertEqual(extension._consumptions(self.directory()), [])


class SettledHistoryPositiveTests(SettledHistoryFixture):
    """Group 2: positive production-entry proof that settled history is admitted and granted."""

    def test_audit_admits_settled_failures_and_full_cycle_consumes_the_grant(self):
        audit = self.audit()
        self.assertEqual(audit['lane'], extension.PRE)
        self.assertEqual(audit['scope_request_id'], self.final_request_id)
        saved = self.saved_audit(audit['audit_sha256'])
        self.assertEqual(saved['lane'], extension.PRE)
        self.assertEqual(saved['evidence']['lane'], extension.PRE)
        native = saved['evidence']['native']
        engineer_session = self.state()['bindings']['engineer']['session_id']
        self.assertEqual(native['engineer']['session_id'], engineer_session)
        expected = self.expected_settled_failures()
        self.assertEqual(len(expected), self.SETTLED_FAILURES)
        self.assertEqual(native['engineer']['settled_failures'], expected)
        for entry in native['engineer']['settled_failures']:
            self.assertEqual(set(entry), {'request_id', 'turn_id', 'provider_turn_id', 'settlement_kind',
                                          'receipt_sha256', 'outcome_resume', 'outcome_resume_sha256',
                                          'outcome_sha256'})
            self.assertEqual(entry['settlement_kind'], 'quota_limit')
            self.assertRegex(entry['receipt_sha256'], '^[0-9a-f]{64}$')
            self.assertRegex(entry['outcome_resume_sha256'], '^[0-9a-f]{64}$')
            self.assertRegex(entry['outcome_sha256'], '^[0-9a-f]{64}$')
        # Byte-compat: a reviewer binding was never used for settled_history, so its own evidence
        # (and that of a room with no settled failures at all; see BytesCompatibilityTests) carries
        # no settled_failures key at all.
        self.assertNotIn('settled_failures', native['reviewer'])
        # owned_turns must include every completed AND settled request's turn for the engineer.
        owned_turn_ids = {row['turn_id'] for row in native['engineer']['owned_turns']}
        for item in self.settled:
            self.assertIn(item['turn_id'], owned_turn_ids)
        for request_id in self.completed:
            self.assertIn(self.state()['requests'][request_id]['turn_id'], owned_turn_ids)
        self.assertEqual(native['engineer']['unowned_completed_turns'], [])

        self.grant()
        posts_before_send = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts_before_send + 1)
        self.finish_fourth()
        self.assertEqual(self.state()['requests']['fourth']['state'], 'completed')
        # The single fourth-review slot is now consumed; a fifth attempt is refused, not dispatched.
        with self.assertRaisesRegex(ao.RoomError, 'exhausted'):
            self.send('spec_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts_before_send + 1)


class BytesCompatibilityTests(PreAcceptanceFixture):
    """Group 2 byte-compat check: a room without any settled failure keeps today's exact bytes."""

    def test_room_without_settled_failures_has_no_settled_failures_key(self):
        self.propose_scope_change()
        audit = self.audit()
        saved = ao.read(self.directory() / extension.BASE / 'audits' / (audit['audit_sha256'] + '.json'))
        native = saved['evidence']['native']
        self.assertNotIn('settled_failures', native['engineer'])
        self.assertNotIn('settled_failures', native['reviewer'])


STRICT_MESSAGE = ('Native history contains a turn that is not completed \\(state failed\\); failed, '
                  'interrupted, cancelled, recovered or active work refuses')


class AcceptedSettledFailureFixture(ReviewExtensionFixture):
    """A normal, already-accepted room with one settled native quota failure in the engineer's
    history. Neither the accepted-candidate lane nor the acceptance-review-audit entry point ever
    opts into settled-history admission, so both must keep refusing on it like the baseline."""

    def setUp(self):
        super().setUp()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('correction', 'post-accept-failure')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['post-accept-failure']['state'], 'uncertain')
        with patch.object(native_outcome, 'inspect', return_value=_quota_native('post-accept-failure')):
            audit = self.service.ao_room_outcome_audit(self.room)
            self.assertEqual(audit['outcome']['kind'], 'quota_limit')
            self.service.ao_room_outcome_resume(
                self.room, 'post-accept-failure', audit['outcome_sha256'], 'post-accept-resume',
                'Native quota rejection identified at post-accept-failure; user confirmed availability.',
                'User approved the exact one-time continuation for post-accept-failure.')
        self.assertEqual(self.state()['requests']['post-accept-failure']['state'], 'settled_failure')


class AcceptedCandidateLaneScopingTests(AcceptedSettledFailureFixture):
    """Group 4(b)/4(c): the two production entries that share _native_evidence outside the
    pre-acceptance lane never admit a settled failure either, on the same accepted room."""

    def test_accepted_candidate_lane_refuses_on_a_settled_failure_in_history(self):
        with self.assertRaisesRegex(ao.RoomError, STRICT_MESSAGE):
            self.audit()  # ao_room_spec_review_extension_audit: the original, implicit lane.

    def test_acceptance_review_extension_never_opts_into_settled_history(self):
        # ao_acceptance_extension.py keeps the original positional _native_evidence call with no
        # settled_history keyword; SettledHistoryStrictControlTests'
        # test_plain_positional_native_evidence_call_keeps_the_strict_baseline_refusal proves that
        # exact call form refuses a settled failure.
        import inspect
        import ao_acceptance_extension
        source = inspect.getsource(ao_acceptance_extension)
        self.assertIn('ao_review_extension._native_evidence(directory, state, bindings[role], snapshots[role])',
                      source)
        self.assertNotIn('settled_history', source)


class SettledHistoryStrictControlTests(SettledHistoryFixture):
    """Group 4: the settled-history opt-in stays scoped to the pre-acceptance lane's engineer role."""

    def test_plain_positional_native_evidence_call_keeps_the_strict_baseline_refusal(self):
        # settled_history is keyword-only (ao_review_extension.py's _native_evidence signature);
        # the original 4-positional-argument call a non-opted-in caller still uses never admits a
        # settled failure, even though this exact room has 24 of them.
        engineer = self.state()['bindings']['engineer']
        snapshot = ao_provider_transition._ReadOnlyIdentity(self.service).identity(
            ao_provider_transition._CompleteClient(self.service.client(self.state())),
            self.state(), engineer)
        with self.assertRaisesRegex(ao.RoomError, STRICT_MESSAGE):
            extension._native_evidence(self.directory(), self.state(), engineer, snapshot)
        with self.assertRaises(TypeError):
            extension._native_evidence(self.directory(), self.state(), engineer, snapshot, True)

    def test_reviewer_side_unsettled_failed_turn_refuses_pre_acceptance_audit(self):
        self.fake.snapshots['reviewer']['turns'].append(
            {'id': 'reviewer-foreign-turn', 'providerTurnId': 'native-reviewer-foreign-turn', 'state': 'failed'})
        self.assert_refuses_without_mutation(STRICT_MESSAGE, self.audit)

    def test_engineer_side_unowned_failed_interrupted_cancelled_or_active_turn_refuses_the_audit(self):
        for state in ('failed', 'interrupted', 'cancelled', 'running'):
            with self.subTest(state=state):
                turn = {'id': 'engineer-foreign-' + state, 'providerTurnId': 'native-engineer-foreign-' + state,
                        'state': state}
                self.fake.snapshots['engineer']['turns'].append(turn)
                try:
                    self.assert_refuses_without_mutation(
                        'Native history contains a turn that is not completed \\(state ' + state + '\\)', self.audit)
                finally:
                    self.fake.snapshots['engineer']['turns'].remove(turn)

    def test_recovered_turn_without_proof_under_the_settled_history_opt_in_refuses_the_audit(self):
        self.fake.snapshots['engineer']['turns'].append(
            {'id': 'recovered-turn-1', 'providerTurnId': 'native-recovered-turn-1', 'state': 'recovered'})
        self.assert_refuses_without_mutation('not completed \\(state recovered\\)', self.audit)

    def test_only_the_engineer_binding_is_evaluated_with_settled_history(self):
        calls = []
        original = extension._native_evidence

        def spy(directory, state, binding, snapshot, *, settled_history=False):
            calls.append((binding['session_id'], settled_history))
            return original(directory, state, binding, snapshot, settled_history=settled_history)

        with patch.object(extension, '_native_evidence', side_effect=spy):
            self.grant()
            self.send('spec_review', 'fourth')
        bindings = self.state()['bindings']
        self.assertEqual(set(calls), {(bindings['engineer']['session_id'], True),
                                      (bindings['reviewer']['session_id'], False)})


class SettledHistoryUncertainLatestTurnTests(PreAcceptanceFixture):
    """Group 4(e): the latest engineer turn failed and was never settled (still 'uncertain').

    ao_review_extension._inspect's own first line is ``service.settled(state, ...)``
    (ao_project_room.py:411), which refuses any owned
    request outside TERMINAL -- including plain 'uncertain' -- before native evidence is ever
    computed and before the missing completed scope-change proposal could raise its own, different
    SCOPE_REQUEST message. This is a more fundamental guard than the native-history strict message:
    an unsettled failure never even reaches the settled-history opt-in this module otherwise
    exercises, confirming settlement (not mere native-turn completeness) is the real gate.
    """

    def test_latest_engineer_turn_failed_and_unsettled_refuses_pre_acceptance_audit(self):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['implementation']['state'], 'uncertain')
        with self.assertRaisesRegex(ao.RoomError, 'An owned request is active or uncertain'):
            self.audit(candidate_sha256='0' * 64)


class SettledHistorySettlementEvidenceNegativeTests(SettledHistoryFixture):
    """Group 5: a clean audit and grant already exist; tampering the settlement evidence one
    admitted settled failure depends on, afterwards, refuses the very next production entry (the
    fourth-review send) without dispatching a POST or consuming the one-time grant.

    Two different guards actually fire first, ahead of ``admission()``'s own fresh
    ``_native_evidence`` re-check loop, because every ``ao_room_send`` first calls
    ``Service.settled`` -> ``ao_review_extension.validate`` -> ``_retained``: a raw-bytes
    manifest-digest loop over every path recorded in the grant's own evidence manifest
    (``'Review-extension retained evidence is missing or modified'``), preceded by a plain
    state-field diff against the grant-time baseline for every request field that is not in
    ``OBSERVATIONS``/``HISTORY_POINTERS`` (``'Review-extension prior requests, receipts or counters
    changed'``). ``outcome_resume`` and ``outcome_resume_sha256`` are themselves listed in
    ``OBSERVATIONS`` (ao_review_extension.py:40), so re-pointing them escapes that earlier diff and
    reaches the deeper, settlement-specific checks this module adds.
    """

    def _settled_request(self, index=0):
        request_id = self.settled[index]['request_id']
        return request_id, self.state()['requests'][request_id]

    def test_tampered_receipt_bytes_refuse_the_fourth_send(self):
        self.grant()
        _, request = self._settled_request()
        path = self.directory() / request['receipt']
        original = path.read_bytes()
        try:
            path.write_text('{}')
            # _retained's own manifest-digest loop catches this before admission()'s own
            # _native_evidence re-check (which would otherwise have raised
            # ao_workflow.completed_receipt's own 'Native result receipt was modified', confirmed
            # separately by test_first_audit_sees_a_modified_receipt_bytes_cleanly below).
            self._assert_fourth_send_refuses('Review-extension retained evidence is missing or modified')
        finally:
            path.write_bytes(original)

    def test_deleted_outcome_resume_release_before_any_grant_refuses_the_audit_without_mutation(self):
        """A deleted outcome-resume release refuses the first audit with no audit file, state change or
        POST. The refusal currently surfaces from the pre-existing, lane-independent
        ``Service.settled`` -> ``ao_outcomes.validate_settlement`` read (outside this change) as a raw
        OSError rather than a RoomError; the exception class is deliberately left open so a later fix
        that converts it does not break this test."""
        _, request = self._settled_request()
        path = self.directory() / request['outcome_resume']
        original = path.read_bytes()
        try:
            path.unlink()
            before_files = self.room_files()
            posts = len(self.fake.posts)
            with self.assertRaises((ao.RoomError, OSError)):
                self.audit()
            self.assertEqual(self.room_files(), before_files)
            self.assertEqual(len(self.fake.posts), posts)
        finally:
            path.write_bytes(original)

    def test_deleted_receipt_before_any_grant_refuses_cleanly(self):
        """C4: unlike the outcome-resume release file above, a deleted *receipt* is never read by
        ``Service.settled``'s own ``validate_settlement`` call; it is ``_settled_failures``'s own
        ``ao_workflow.completed_receipt`` call that hits it, and that function's try/except now
        wraps the resulting OSError into a clean refusal instead of letting it propagate raw."""
        _, request = self._settled_request()
        path = self.directory() / request['receipt']
        original = path.read_bytes()
        try:
            path.unlink()
            self.assert_refuses_without_mutation(
                'Settled native failure evidence is missing or unreadable', self.audit)
        finally:
            path.write_bytes(original)

    def test_deleted_outcome_resume_release_after_a_grant_refuses_the_fourth_send_cleanly(self):
        """Confirms the raw-OSError defect above is specific to the pre-grant audit entry: the
        identical deletion, reached instead through the already-granted send path's
        ``ao_review_extension.validate`` (whose own try/except catches OSError), is converted into
        a clean RoomError before admission()'s own native-evidence loop runs."""
        self.grant()
        _, request = self._settled_request()
        path = self.directory() / request['outcome_resume']
        original = path.read_bytes()
        try:
            path.unlink()
            self._assert_fourth_send_refuses('Review-extension evidence is unreadable or inconsistent')
        finally:
            path.write_bytes(original)

    def test_first_audit_sees_a_modified_receipt_bytes_cleanly(self):
        """Contrast with the deletion defect above: a merely *modified* (not deleted) receipt,
        reached the same way at the first audit before any grant exists, is already caught
        cleanly -- ao_workflow.completed_receipt's own digest comparison (line 44-45) raises a
        plain RoomError, with no bare filesystem read of a possibly-missing path involved."""
        _, request = self._settled_request()
        path = self.directory() / request['receipt']
        original = path.read_bytes()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ao.RoomError, 'Native result receipt was modified'):
                self.audit()
        finally:
            path.write_bytes(original)

    def test_modified_outcome_resume_release_refuses_the_fourth_send(self):
        self.grant()
        _, request = self._settled_request()
        path = self.directory() / request['outcome_resume']
        original = path.read_bytes()
        try:
            record = json.loads(original)
            record['authorization'] = 'A different, unauthorized continuation justification.'
            path.write_text(json.dumps(record))
            self._assert_fourth_send_refuses('Review-extension retained evidence is missing or modified')
        finally:
            path.write_bytes(original)

    def test_outcome_resume_repointed_to_another_requests_record_refuses_the_fourth_send(self):
        self.grant()
        _, first = self._settled_request(0)
        second_id, second = self._settled_request(1)
        self.assertNotEqual(first['outcome_resume'], second['outcome_resume'])
        tampered = copy.deepcopy(self.state())
        tampered['requests'][second_id]['outcome_resume'] = first['outcome_resume']
        tampered['requests'][second_id]['outcome_resume_sha256'] = first['outcome_resume_sha256']
        ao.atomic(self.directory() / 'state.json', tampered)
        self._assert_fourth_send_refuses('Outcome continuation has no exact owned path')

    def test_settled_failure_state_flipped_to_completed_refuses_the_fourth_send(self):
        # 'state' is an ordinary tracked request field (not in OBSERVATIONS/HISTORY_POINTERS), so
        # _retained's own earlier baseline-diff loop catches this before the manifest-digest loop
        # or _native_evidence's own STRICT_MESSAGE check run.
        _, request = self._settled_request()
        self.grant()
        tampered = copy.deepcopy(self.state())
        tampered['requests'][request['request_id']]['state'] = 'completed'
        ao.atomic(self.directory() / 'state.json', tampered)
        self._assert_fourth_send_refuses('Review-extension prior requests, receipts or counters changed')

    def test_release_record_tamper_refuses_the_grant_and_full_restoration_then_grants(self):
        """Settlement re-verification is live at each entry, not a one-time latch: a tampered
        outcome-resume release refuses the grant without mutation, and restoring its exact bytes
        lets the identical grant succeed."""
        audited = self.audit()
        _, request = self._settled_request()
        path = self.directory() / request['outcome_resume']
        original = path.read_bytes()
        record = json.loads(original)
        record['authorization'] = 'A transient, unauthorized rewrite that is fully reverted below.'
        inputs = self.grant_inputs(audit=audited)
        try:
            path.write_text(json.dumps(record))
            self.assert_refuses_without_mutation(
                'Outcome continuation evidence was modified',
                lambda: self.service.ao_room_preacceptance_review_extend(**inputs))
        finally:
            path.write_bytes(original)
        result = self.service.ao_room_preacceptance_review_extend(**inputs)
        self.assertEqual(result['remaining_spec_reviews'], 1)

    def test_outcome_resume_repointed_to_a_rewritten_valid_release_refuses_the_fourth_send(self):
        """A self-consistent, content-addressed release written after the grant passes
        validate_settlement, the baseline diff (outcome_resume fields are OBSERVATIONS) and the
        manifest loop (the granted file is untouched); only admission's settled_failures
        comparison refuses it."""
        self.grant()
        request_id, request = self._settled_request()
        record = ao.read(self.directory() / request['outcome_resume'])
        record['diagnosis'] = 'A different, unaudited diagnosis written after the grant.'
        sha256 = ao.digest(record)
        relative = 'outcome-resumes/' + request_id + '/' + sha256 + '.json'
        ao.atomic(self.directory() / relative, record)
        tampered = copy.deepcopy(self.state())
        tampered['requests'][request_id].update(outcome_resume=relative, outcome_resume_sha256=sha256)
        ao.atomic(self.directory() / 'state.json', tampered)
        self._assert_fourth_send_refuses('Native history or metadata changed after the review extension')

    def test_settled_turn_provider_identity_drift_after_grant_refuses_the_fourth_send(self):
        self.grant()
        turn_id = self.settled[0]['turn_id']
        turn = next(t for t in self.fake.snapshots['engineer']['turns'] if t['id'] == turn_id)
        turn['providerTurnId'] = 'native-drifted-' + turn_id
        self._assert_fourth_send_refuses('Observed native provider turn identity differs from the saved request')

    def test_non_json_settled_receipt_refuses_the_audit_without_mutation(self):
        _, request = self._settled_request()
        path = self.directory() / request['receipt']
        original = path.read_bytes()
        try:
            path.write_text('not json')
            self.assert_refuses_without_mutation(
                'Settled native failure evidence is missing or unreadable', self.audit)
        finally:
            path.write_bytes(original)


class TruncatedSettledFailureFixture(PreAcceptanceFixture):
    """Group 6: one settled native quota failure whose SAVED receipt was captured with a truncated
    native history, reconciled through an explicit outcome audit into a
    ``complete_history_native_quota_failed_turn`` proof (ao_history_reconciliation.py:360-367)
    before settlement.

    Built as its own minimal fixture rather than interleaved into SettledHistoryFixture's 24
    ordinary failures: ``ao_history_reconciliation._failed_inputs`` requires the native
    source dict embedded in the fresh observation to equal this room's own registered
    ``native_outcome_source`` exactly, and requires an unexplained-turn-free baseline against the
    live snapshot -- both far more exact requirements than an ordinary settled quota failure (which
    never touches ``ao_history_reconciliation.py`` at all, by ``_HistoryEvidence.retained``'s early
    return when ``HISTORY_POINTERS`` is absent) ever exercises, and easiest to satisfy cleanly with
    a single dedicated failure rather than interleaved among many.
    """

    def setUp(self):
        super().setUp()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'truncated-failure')
        snapshot = self.fake.snapshots['engineer']
        self.fake.finish('engineer', state='failed')
        snapshot['history_truncated'] = True
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['truncated-failure']['state'], 'uncertain')
        receipt_path = self.directory() / self.state()['requests']['truncated-failure']['receipt']
        self.assertTrue(ao.read(receipt_path)['history_truncated'])
        # The native side never actually recovers -- only the fresh audit's own snapshot stops
        # reporting truncation, which is exactly what authorizes reading past the saved truncation.
        snapshot['history_truncated'] = False
        native = {'anchor_uuid': 'owned-native-packet-truncated-failure', 'next_human_uuid': None,
                  'errors': [{'uuid': 'native-quota-truncated-failure', 'error': 'rate_limit', 'http_status': 429}],
                  'stop_reasons': [], 'source_sha256': 'e' * 64,
                  'source': self.state()['native_outcome_source']}
        with patch.object(native_outcome, 'inspect', return_value=native):
            audit = self.service.ao_room_outcome_audit(self.room)
            self.assertEqual(audit['outcome']['kind'], 'quota_limit')
            self.assertTrue(audit['resume_eligible'])
            request = self.state()['requests']['truncated-failure']
            self.proof_sha256 = request.get('history_reconciliation_sha256')
            self.assertRegex(self.proof_sha256 or '', '^[0-9a-f]{64}$')
            self.service.ao_room_outcome_resume(
                self.room, 'truncated-failure', audit['outcome_sha256'], 'truncated-resume',
                'Native quota rejection identified at the truncated turn; user confirmed availability.',
                'User approved the exact one-time continuation for the truncated turn.')
            self.assertEqual(self.state()['requests']['truncated-failure']['state'], 'settled_failure')
            self.send('correction', 'truncated-resume')
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        self.final_request_id = 'truncated-resume'
        self.assertEqual(self.state()['requests'][self.final_request_id]['state'], 'completed')
        self.register()

    def audit(self, **changes):
        changes.setdefault('scope_request_id', self.final_request_id)
        if 'candidate_sha256' not in changes:
            changes['candidate_sha256'] = self.candidate_sha256(self.final_request_id)
        return super().audit(**changes)

    def saved_audit(self, audit_sha256):
        return ao.read(self.directory() / extension.BASE / 'audits' / (audit_sha256 + '.json'))

    def reconciliation_path(self):
        return 'history-reconciliations/truncated-failure/' + self.proof_sha256 + '.json'


class TruncatedSettledFailureTests(TruncatedSettledFailureFixture):
    def test_audit_admits_it_with_a_history_reconciliation_manifest_path(self):
        audit = self.audit()
        saved = self.saved_audit(audit['audit_sha256'])
        native = saved['evidence']['native']['engineer']
        self.assertEqual(len(native['settled_failures']), 1)
        entry = native['settled_failures'][0]
        self.assertEqual(entry['request_id'], 'truncated-failure')
        self.assertEqual(entry['settlement_kind'], 'quota_limit')
        manifest = saved['evidence']['manifest']
        self.assertIn(self.reconciliation_path(), manifest)
        self.assertRegex(manifest[self.reconciliation_path()], '^[0-9a-f]{64}$')

    def test_tampering_the_reconciliation_record_after_grant_refuses_the_fourth_send(self):
        self.grant()
        path = self.directory() / self.reconciliation_path()
        original = path.read_bytes()
        record = json.loads(original)
        record['text_sha256'] = '0' * 64
        try:
            path.write_text(json.dumps(record))
            posts = len(self.fake.posts)
            with self.assertRaisesRegex(ao.RoomError, 'Review-extension retained history evidence is inconsistent'):
                self.send('spec_review', 'fourth')
            self.assertEqual(len(self.fake.posts), posts)
            self.assertNotIn('fourth', self.state()['requests'])
            self.assertEqual(extension._consumptions(self.directory()), [])
        finally:
            path.write_bytes(original)

    def test_removed_history_reconciliation_sha256_refuses_the_audit(self):
        tampered = copy.deepcopy(self.state())
        del tampered['requests']['truncated-failure']['history_reconciliation_sha256']
        ao.atomic(self.directory() / 'state.json', tampered)
        with self.assertRaisesRegex(
                ao.RoomError,
                'Settled native failure with a truncated receipt requires its retained '
                'failed-turn reconciliation proof'):
            self.audit()

    def test_forged_saved_turn_anchor_refuses_the_audit_without_mutation(self):
        """C5: ``proof()`` anchors a failed-turn proof's ``inputs['saved_turn_sha256']`` /
        ``inputs['saved_error_message_sha256']`` against ``digest(receipt['turn'])`` /
        ``digest(receipt['turn'].get('errorMessage'))``. A copy of the real, minted proof with
        either anchor alone forged -- written content-addressed under its own new digest in the
        same retained folder, with the request repointed at it -- must refuse exactly like any
        other inconsistent retained record, before any mutation."""
        real = ao.read(self.directory() / self.reconciliation_path())
        for forged_field in ('saved_turn_sha256', 'saved_error_message_sha256'):
            with self.subTest(forged_field=forged_field):
                before = copy.deepcopy(self.state())
                forged = copy.deepcopy(real)
                forged['inputs'][forged_field] = '0' * 64
                forged_sha256 = ao.digest(forged)
                self.assertNotEqual(forged_sha256, self.proof_sha256)
                forged_path = (self.directory() / 'history-reconciliations' / 'truncated-failure'
                              / (forged_sha256 + '.json'))
                ao.atomic(forged_path, forged)
                tampered = copy.deepcopy(self.state())
                tampered['requests']['truncated-failure']['history_reconciliation_sha256'] = forged_sha256
                ao.atomic(self.directory() / 'state.json', tampered)
                try:
                    self.assert_refuses_without_mutation(
                        'Review-extension retained history evidence is inconsistent', self.audit)
                finally:
                    ao.atomic(self.directory() / 'state.json', before)
                    forged_path.unlink()

    def test_over_limit_retained_failed_turn_proof_refuses_the_audit_without_mutation(self):
        with patch.object(extension, 'MAX_HISTORY_RECORD_BYTES', 64):
            self.assert_refuses_without_mutation(
                'Review-extension retained history evidence is unavailable, unsafe or over its bound', self.audit)


class RenewedTruncatedSettledFailureFixture(PreAcceptanceFixture):
    """Group 6b (C1 regression): like TruncatedSettledFailureFixture, but the failed AO turn row
    keeps its real provider ``errorMessage``, and a refused ``ao_room_send`` attempted while the
    request is still 'uncertain' -- between the first explicit outcome audit and the resume --
    invalidates the first proof out-of-band (``Service.locked``'s own except clause,
    ao_project_room.py:300-305, calls ``ao_history_reconciliation.invalidate_latest`` after *any*
    refused locked operation, not only a successful one); the next explicit audit then renews it.
    The resulting lineage holds one invalidation, the outcome it names, and both the original and
    the renewed proof -- the shape that first makes it matter that ``_HistoryEvidence.lineage``
    (ao_review_extension.py:196-204) strips ``errorMessage`` from a referenced outcome's own raw
    ``ao_terminal`` before comparing it with the failed-turn proof's own already-stripped
    ``ao_terminal_sha256`` input (C1): the outcome row stores the full live turn, but the proof
    that names it as superseded was minted from the stripped identity.
    """

    PROVIDER_TEXT = 'API Error: Rate limit reached'

    def native(self, sha):
        return {'anchor_uuid': 'owned-native-packet-truncated-failure', 'next_human_uuid': None,
                'errors': [{'uuid': 'native-quota-truncated-failure', 'error': 'rate_limit', 'http_status': 429}],
                'stop_reasons': [], 'source_sha256': sha, 'source': self.state()['native_outcome_source']}

    def setUp(self):
        super().setUp()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', 'truncated-failure')
        snapshot = self.fake.snapshots['engineer']
        self.fake.finish('engineer', state='failed')
        self.failed_turn = snapshot['turns'][-1]
        self.failed_turn['errorMessage'] = self.PROVIDER_TEXT
        snapshot['history_truncated'] = True
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['truncated-failure']
        self.assertEqual(request['state'], 'uncertain')
        receipt = ao.read(self.directory() / request['receipt'])
        self.assertTrue(receipt['history_truncated'])
        self.assertEqual(receipt['turn']['errorMessage'], self.PROVIDER_TEXT)
        snapshot['history_truncated'] = False
        with patch.object(native_outcome, 'inspect', return_value=self.native('e' * 64)):
            first = self.service.ao_room_outcome_audit(self.room)
            self.assertTrue(first['resume_eligible'])
            self.p1 = self.state()['requests']['truncated-failure']['history_reconciliation_sha256']
            self.o1 = first['outcome_sha256']
            self.assertRegex(self.p1 or '', '^[0-9a-f]{64}$')
            # The refusal itself propagates unmodified through Service.locked; the out-of-band
            # invalidation it triggers is a side effect of the lock's own except clause (which
            # invalidates on *any* RoomError/OSError/ValueError/KeyError/TypeError, not only a
            # defect), not of anything this refused send itself does on a happy path.
            with self.assertRaisesRegex(
                    ao.RoomError, 'An owned request is active or uncertain; sync it without resending'):
                self.send('correction', 'premature-correction')
            self.assertNotIn('premature-correction', self.state()['requests'])
            request = self.state()['requests']['truncated-failure']
            self.inv1 = request.get('history_reconciliation_invalidation_sha256')
            self.assertRegex(self.inv1 or '', '^[0-9a-f]{64}$')
            self.assertEqual(request['history_reconciliation_sha256'], self.p1)  # Not yet renewed.
            second = self.service.ao_room_outcome_audit(self.room)
            self.assertTrue(second['resume_eligible'])
            request = self.state()['requests']['truncated-failure']
            self.p2 = request['history_reconciliation_sha256']
            self.assertEqual(request['history_reconciliation_invalidation_sha256'], self.inv1)
            self.assertNotEqual(self.p1, self.p2)
            self.o2 = second['outcome_sha256']
            self.assertNotEqual(self.o1, self.o2)  # Each outcome embeds its own minted proof sha256.
            self.service.ao_room_outcome_resume(
                self.room, 'truncated-failure', self.o2, 'truncated-resume',
                'Native quota rejection identified at the truncated turn; user confirmed availability.',
                'User approved the exact one-time continuation for the truncated turn.')
            self.assertEqual(self.state()['requests']['truncated-failure']['state'], 'settled_failure')
            self.send('correction', 'truncated-resume')
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        self.service.ao_room_sync(self.room)
        self.final_request_id = 'truncated-resume'
        self.assertEqual(self.state()['requests'][self.final_request_id]['state'], 'completed')
        self.register()
        # The superseded outcome O1 retained the raw provider text; P1 bound only its stripped identity.
        o1 = ao.read(self.directory() / 'outcomes' / 'truncated-failure' / (self.o1 + '.json'))
        self.assertEqual(o1['ao_terminal'].get('errorMessage'), self.PROVIDER_TEXT)
        self.assertEqual(o1['history_reconciliation_sha256'], self.p1)

    def audit(self, **changes):
        changes.setdefault('scope_request_id', self.final_request_id)
        if 'candidate_sha256' not in changes:
            changes['candidate_sha256'] = self.candidate_sha256(self.final_request_id)
        return super().audit(**changes)

    def saved_audit(self, audit_sha256):
        return ao.read(self.directory() / extension.BASE / 'audits' / (audit_sha256 + '.json'))

    def reconciliation_path(self, sha256):
        return 'history-reconciliations/truncated-failure/' + sha256 + '.json'

    def invalidation_path(self):
        return 'history-reconciliation-invalidations/truncated-failure/' + self.inv1 + '.json'


class RenewedTruncatedSettledFailureTests(RenewedTruncatedSettledFailureFixture):
    def test_full_cycle_authenticates_the_renewed_lineage_and_admits_the_fourth_send(self):
        audit = self.audit()
        saved = self.saved_audit(audit['audit_sha256'])
        entries = saved['evidence']['native']['engineer']['settled_failures']
        self.assertEqual([entry['request_id'] for entry in entries], ['truncated-failure'])
        manifest = saved['evidence']['manifest']
        for path in (self.reconciliation_path(self.p1), self.reconciliation_path(self.p2),
                    self.invalidation_path()):
            self.assertIn(path, manifest)
            self.assertRegex(manifest[path], '^[0-9a-f]{64}$')
        self.grant()
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_controller_restart_clearing_the_live_error_message_after_settlement_still_succeeds(self):
        """_HistoryEvidence reads only retained, content-addressed evidence (the saved receipt and
        the minted proofs/outcomes), never the live snapshot; a controller restart clearing the
        long-settled turn's own live errorMessage (as AO does) therefore cannot matter to this
        already-settled lineage. This pins that invariant rather than merely assuming it."""
        self.failed_turn['errorMessage'] = None
        audit = self.audit()
        self.assertRegex(audit['audit_sha256'], '^[0-9a-f]{64}$')
        self.grant()
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_raw_terminal_digest_without_normalization_refuses_the_renewed_lineage(self):
        """In-suite negative control for the lineage normalization: with ``_terminal_identity`` patched
        to return its argument unchanged, the lineage's errorMessage-bearing outcome rows no longer
        digest to their failed-turn proofs' stripped ao_terminal_sha256 inputs, and the audit
        refuses. The positive full-cycle test above is what fails if the normalization is removed."""
        with patch.object(reconciliation, '_terminal_identity', side_effect=lambda turn: turn):
            with self.assertRaisesRegex(
                    ao.RoomError, 'Review-extension retained history evidence is inconsistent'):
                self.audit()


class RecoveredContextWithoutProofTests(PreAcceptanceFixture):
    """Group 7 negative: a bare 'recovered' native turn with no admitted import proof refuses the
    pre-acceptance audit exactly like the baseline in a room with no settled failure, where the
    settled-history opt-in is inactive; SettledHistoryStrictControlTests covers the same turn under
    the opt-in."""

    def test_recovered_turn_without_proof_refuses_with_its_exact_state(self):
        self.propose_scope_change()
        self.fake.snapshots['engineer']['turns'].append(
            {'id': 'recovered-turn-1', 'providerTurnId': 'native-recovered-turn-1', 'state': 'recovered'})
        with self.assertRaisesRegex(ao.RoomError, 'not completed \\(state recovered\\)'):
            self.audit()


class RecoveredContextWithProofWithoutSettledFailureTests(PreAcceptanceFixture):
    """Group 7 negative (C2): the exact same compaction-import-proven recovered turn that
    RecoveredContextWithProofTests below admits, built here with no settled failure anywhere in
    this room's history, still refuses with the baseline's strict message -- the settled-history
    opt-in (``settled_history=True``) is what admits a proven recovered import at all, and C2
    gates that opt-in on the engineer session owning at least one settled_failure request."""

    def setUp(self):
        super().setUp()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        self.recovered_turn = {'id': 'recovered-turn-1', 'providerTurnId': 'native-recovered-turn-1',
                               'state': 'recovered'}
        self.fake.finish('engineer', json.dumps(self.scope_change_report()))
        # Appended only after finish() so FakeAO.finish's own turns[-1] lookup (test_ao_project_room.py:65)
        # completes 'implementation's own turn, not this one; this is exactly the unexplained extra
        # turn known_compaction_turns' proof must name to be admitted (if the opt-in were active).
        self.fake.snapshots['engineer']['turns'].append(self.recovered_turn)
        # ao_worker_identity._collect (ao_worker_identity.py:443) independently re-scans this same
        # transcript for 'implementation's W3 worker-identity evidence and refuses with 'source_changed'
        # unless this matches the transcript's real bytes digest exactly as ao_native_outcome.inspect
        # itself computes it (ao_native_outcome.py:723); an arbitrary placeholder is not accepted.
        native = {'anchor_uuid': 'owned-native-packet-implementation', 'next_human_uuid': None,
                  'errors': [], 'stop_reasons': ['end_turn'], 'source_sha256': ao.digest(self.transcript.read_bytes()),
                  'source': self.state()['native_outcome_source'],
                  'compaction_imports': [{'turn_id': 'recovered-turn-1',
                                          'turn_sha256': ao.digest(self.recovered_turn),
                                          'messages_sha256': ao.digest([])}]}
        with patch.object(native_outcome, 'inspect', return_value=native):
            self.service.ao_room_sync(self.room)
        self.assertEqual(self.state()['requests']['implementation']['state'], 'completed')
        self.register()

    def test_recovered_turn_with_compaction_import_proof_without_settled_failures_refuses_the_audit(self):
        with self.assertRaisesRegex(ao.RoomError, 'not completed \\(state recovered\\)'):
            self.audit()


def _splice_compaction_import_proof(fixture):
    """Append a 'recovered' engineer turn and splice a verified compaction-import proof for it onto
    the latest engineer request's own semantic-outcome record, content-addressed under its own new
    digest, re-pointing ``semantic_outcome``/``semantic_outcome_sha256`` to it. Shared by
    ``RecoveredContextWithProofTests`` and ``RecoveredContextLifecycleTests`` below.

    Returns a dict with ``recovered_turn`` (the exact object living in
    ``fixture.fake.snapshots['engineer']['turns']``, so mutating it in place drifts the live
    history), ``original``/``original_path``/``original_sha256`` (the pre-splice record and its own
    still-valid content-addressed pointer) and ``modified``/``new_path``/``new_sha256`` (the spliced
    record this function wrote and pointed the request to).
    """
    request = fixture.state()['requests'][fixture.final_request_id]
    original_path, original_sha256 = request['semantic_outcome'], request['semantic_outcome_sha256']
    original = ao.read(fixture.directory() / original_path)
    recovered_turn = {'id': 'recovered-turn-1', 'providerTurnId': 'native-recovered-turn-1',
                      'state': 'recovered'}
    fixture.fake.snapshots['engineer']['turns'].append(recovered_turn)
    modified = copy.deepcopy(original)
    modified['native'] = {**modified['native'], 'compaction_imports': [
        {'turn_id': 'recovered-turn-1', 'turn_sha256': ao.digest(recovered_turn),
         'messages_sha256': ao.digest([])}]}
    new_sha256 = ao.digest(modified)
    fixture.assertNotEqual(new_sha256, original_sha256)
    new_path = 'outcomes/' + fixture.final_request_id + '/' + new_sha256 + '.json'
    ao.atomic(fixture.directory() / new_path, modified)
    tampered = copy.deepcopy(fixture.state())
    tampered['requests'][fixture.final_request_id]['semantic_outcome'] = new_path
    tampered['requests'][fixture.final_request_id]['semantic_outcome_sha256'] = new_sha256
    ao.atomic(fixture.directory() / 'state.json', tampered)
    return {'recovered_turn': recovered_turn, 'original': original, 'original_path': original_path,
            'original_sha256': original_sha256, 'modified': modified, 'new_path': new_path,
            'new_sha256': new_sha256}


class RecoveredContextWithProofTests(SettledHistoryFixture):
    """Group 7 positive: a settled history (SettledHistoryFixture, so the settled-history opt-in
    is active -- C2) plus a 'recovered' native turn backed by a verified compaction-import proof
    (ao_outcomes.known_compaction_turns, ao_outcomes.py:1149-1173) on the latest engineer request's
    own semantic-outcome record is admitted and excluded from ``unowned_completed_turns``. Contrast
    with RecoveredContextWithProofWithoutSettledFailureTests above: the identical proof mechanism,
    with no settled failure anywhere in the room's history, instead refuses (C2).

    The proof is spliced onto the already-built history's own final semantic-outcome record,
    rather than sent as one more engineer turn: ``SettledHistoryFixture.setUp()`` ends with
    ``register()``, which puts a fresh charter revision in place, and ``ao_workflow.agreement()``
    (ao_workflow.py:99-106, run for every later ``implementation``/``correction`` send) then
    refuses any further engineer turn until a new ``spec_review`` is agreed against that revision
    -- exactly the budget this room's three initial charter reviews and the review-extension's one
    granted fourth exist to spend on actual production entries, not on rebuilding the fixture.
    ``known_compaction_turns`` itself only ever reads ``latest_for_role(state, 'engineer')``'s own
    semantic-outcome record (ao_outcomes.py:1152+1161), so splicing it directly -- content-addressed
    under its own new digest, exactly like the forged-anchor negative controls elsewhere in this
    module -- reaches the identical admission path a real compaction-import turn would.
    """

    def setUp(self):
        super().setUp()
        self.recovered_turn = _splice_compaction_import_proof(self)['recovered_turn']

    def test_recovered_turn_with_compaction_import_proof_is_admitted_and_excluded_from_unowned(self):
        audit = self.audit()
        saved = self.saved_audit(audit['audit_sha256'])
        native = saved['evidence']['native']['engineer']
        unowned_ids = {row['turn_id'] for row in native['unowned_completed_turns']}
        self.assertNotIn('recovered-turn-1', unowned_ids)
        owned_ids = {row['turn_id'] for row in native['owned_turns']}
        self.assertNotIn('recovered-turn-1', owned_ids)


class RecoveredContextLifecycleTests(SettledHistoryFixture):
    """Group 7 lifecycle: the proven recovered-context turn ``RecoveredContextWithProofTests`` admits
    at audit is carried through the production grant and send entry points, plus post-grant changes
    to its compaction-import evidence. ``ao_project_room.ao_room_send`` first validates the grant
    (``Service.quiet`` -> ``ao_review_extension.validate``/``_retained``), then runs
    ``ao_outcomes.gate``, which re-observes ``latest_for_role(state, 'engineer')`` through a fresh
    ``ao_native_outcome.inspect`` call, refuses any extra native turn that observation does not name
    as an import, and re-persists that request's semantic-outcome record from it. Only then does
    ``ao_workflow.packet`` call ``ao_review_extension.admission``, which re-runs ``validate()`` and
    recomputes native evidence (``_native_evidence`` -> ``ao_provider_transition._native`` ->
    ``ao_outcomes.known_context_turns`` -> ``known_compaction_turns``), and only after admission
    succeeds does ``ao_room_send`` call ``ao_review_extension.consume``. Every refusal below is
    therefore pre-consumption and pre-POST, at whichever of the two checks it names.

    Because the gate re-persists its fresh observation, that observation, not the stored record,
    decides what admission later reads: a record that is only re-pointed or rewritten is replaced
    by the gate. Tests that must pass the gate unchanged patch ``ao_native_outcome.inspect`` to
    return the exact ``native`` dict of the record they leave in place, so the gate's re-persist is
    a content-identical no-op and admission reads that record; the state-only re-point test instead
    lets the observation differ from the re-pointed record to show the gate replacing it.
    """

    def setUp(self):
        super().setUp()
        self.splice = _splice_compaction_import_proof(self)
        self.recovered_turn = self.splice['recovered_turn']

    def test_grant_admits_the_proven_context_turn_and_the_fourth_send_consumes_it_once(self):
        """The recovered turn is counted in turn_count yet excluded from both owned_turns and
        unowned_completed_turns -- a proven context import, not an inferred absence -- and the
        grant's one-time fourth-review slot is consumed exactly once; a fifth stays exhausted."""
        audited = self.audit()
        native = self.saved_audit(audited['audit_sha256'])['evidence']['native']['engineer']
        turns = self.fake.snapshots['engineer']['turns']
        owned_ids = {row['turn_id'] for row in native['owned_turns']}
        unowned_ids = {row['turn_id'] for row in native['unowned_completed_turns']}
        self.assertIn(self.recovered_turn, turns)
        self.assertNotIn('recovered-turn-1', owned_ids)
        self.assertNotIn('recovered-turn-1', unowned_ids)
        self.assertEqual(native['turn_count'], len(turns))
        self.assertEqual(len(owned_ids) + len(unowned_ids), native['turn_count'] - 1)
        inputs = self.grant_inputs(audit=audited)
        self.service.ao_room_preacceptance_review_extend(**inputs)
        posts_before_send = len(self.fake.posts)
        with patch.object(native_outcome, 'inspect', return_value=self.splice['modified']['native']):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts_before_send + 1)
        self.assertEqual([ao.read(path)['request']['request_id'] for path in extension._consumptions(self.directory())],
                         ['fourth'])
        self.assertIn('fourth', self.state()['requests'])
        self.finish_fourth()
        self.assertEqual(self.state()['requests']['fourth']['state'], 'completed')
        with self.assertRaisesRegex(ao.RoomError, 'exhausted'):
            self.send('spec_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts_before_send + 1)

    def test_post_grant_import_proof_drift_refuses_the_fourth_send_at_admission(self):
        """A new content-addressed semantic-outcome record whose compaction-import messages digest
        no longer matches the live recovered turn refuses the fourth send at admission, even when
        ``ao_outcomes.gate``'s own fresh re-observation reasserts that exact same drifted proof."""
        self.grant()
        drifted = copy.deepcopy(self.splice['modified'])
        drifted['native']['compaction_imports'][0]['messages_sha256'] = ao.digest(['a-drifted-message'])
        new_sha256 = ao.digest(drifted)
        self.assertNotEqual(new_sha256, self.splice['new_sha256'])
        new_path = 'outcomes/' + self.final_request_id + '/' + new_sha256 + '.json'
        ao.atomic(self.directory() / new_path, drifted)
        tampered = copy.deepcopy(self.state())
        tampered['requests'][self.final_request_id]['semantic_outcome'] = new_path
        tampered['requests'][self.final_request_id]['semantic_outcome_sha256'] = new_sha256
        ao.atomic(self.directory() / 'state.json', tampered)
        before_files = self.room_files()
        with patch.object(native_outcome, 'inspect', return_value=drifted['native']):
            self._assert_fourth_send_refuses('Verified native compaction import changed')
        self.assertEqual(self.room_files(), before_files)

    def test_post_grant_live_import_loss_refuses_the_fourth_send_at_the_outcome_gate(self):
        """A fresh native re-observation that no longer carries the recovered turn's compaction import
        (the un-spliced observation) refuses the fourth send at ``ao_outcomes.gate``'s own
        unexplained-turn check, before admission is reached, with the granted spliced record still in
        place. Re-pointing only ``state.json`` to the un-spliced record is not a removal: the gate
        re-persists whatever the fresh observation carries (see the state-only re-point test below)."""
        self.grant()
        before_files = self.room_files()
        with patch.object(native_outcome, 'inspect', return_value=self.splice['original']['native']):
            self._assert_fourth_send_refuses('Native history contains unverified recovered context')
        self.assertEqual(self.room_files(), before_files)

    def test_post_grant_state_only_repoint_is_superseded_by_the_gates_fresh_observation(self):
        """Re-pointing only ``state.json`` to the un-spliced record does not remove the import proof:
        while the fresh native re-observation still names the recovered turn, ``ao_outcomes.gate``
        re-persists the spliced record and re-points the request back to it, and the fourth send
        dispatches exactly once."""
        self.grant()
        tampered = copy.deepcopy(self.state())
        tampered['requests'][self.final_request_id]['semantic_outcome'] = self.splice['original_path']
        tampered['requests'][self.final_request_id]['semantic_outcome_sha256'] = self.splice['original_sha256']
        ao.atomic(self.directory() / 'state.json', tampered)
        posts = len(self.fake.posts)
        with patch.object(native_outcome, 'inspect', return_value=self.splice['modified']['native']):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)
        request = self.state()['requests'][self.final_request_id]
        self.assertEqual(request['semantic_outcome'], self.splice['new_path'])
        self.assertEqual(request['semantic_outcome_sha256'], self.splice['new_sha256'])

    def test_post_grant_live_recovered_turn_drift_refuses_the_fourth_send_at_admission(self):
        """A live recovered-turn row that no longer digests to its granted proof's turn_sha256 refuses
        the fourth send at admission's own ``known_compaction_turns`` re-verification, with the granted
        proof record untouched. The patched re-observation deliberately reasserts the granted, now-stale
        proof so the gate passes; unpatched, the gate refuses first (see the live-import-loss test)."""
        self.grant()
        self.splice['recovered_turn']['providerTurnId'] = 'native-drifted-recovered-turn-1'
        before_files = self.room_files()
        with patch.object(native_outcome, 'inspect', return_value=self.splice['modified']['native']):
            self._assert_fourth_send_refuses('Verified native compaction import changed')
        self.assertEqual(self.room_files(), before_files)


class SettledHistoryDurabilityAndConcurrencyTests(SettledHistoryFixture):
    """Group 8: durability under interruption and genuine concurrency, for a room whose committed
    grant evidence includes settled-history admission. ``Service.locked`` (ao_project_room.py:289-292)
    takes a real exclusive ``fcntl.flock`` for the whole of ``_extend``/``ao_room_send``, so two
    threads never race inside the production code; each test proves the serialized outcome is still
    exactly one durable commit or one delivered POST, not that the lock itself is being tested."""

    def test_grant_commit_interruption_before_the_state_write_reconciles_on_an_identical_retry(self):
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
        status = self.service.ao_room_status(self.room)['spec_review_extension']
        self.assertEqual(status['state'], 'pending_uncommitted')
        self.assertEqual(status['lane'], extension.PRE)
        result = self.service.ao_room_preacceptance_review_extend(**inputs)
        self.assertEqual(result['remaining_spec_reviews'], 1)
        self.assertEqual(len(self.fake.posts), posts)

    def test_lost_acknowledgement_on_the_fourth_review_send_consumes_the_slot_without_a_second_post(self):
        self.grant()
        posts = len(self.fake.posts)
        self.fake.lose_ack = True
        try:
            sent = self.send('spec_review', 'fourth')
        finally:
            self.fake.lose_ack = False
        self.assertEqual(sent['state'], 'uncertain')
        self.assertEqual(len(self.fake.posts), posts + 1)
        # The one real native turn is already dispatched, so a distinct-id retry refuses on the
        # still-uncertain owned request, never reaching a second POST.
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'fourth-retry')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.finish_fourth()
        self.assertEqual(self.state()['requests']['fourth']['state'], 'completed')
        with self.assertRaisesRegex(ao.RoomError, 'exhausted'):
            self.send('spec_review', 'fifth')
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_two_threads_granting_concurrently_commit_exactly_one_grant_file(self):
        inputs = self.grant_inputs()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def attempt():
            barrier.wait(timeout=5)
            try:
                results.append(self.service.ao_room_preacceptance_review_extend(**inputs))
            except ao.RoomError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        grants = list((self.directory() / extension.BASE / 'requests').glob('*.json'))
        self.assertEqual(len(grants), 1)

    def test_two_threads_sending_the_fourth_review_with_distinct_ids_deliver_exactly_one_post(self):
        self.grant()
        posts = len(self.fake.posts)
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(key):
            barrier.wait(timeout=5)
            try:
                outcomes.append(('sent', self.send('spec_review', key)))
            except ao.RoomError as exc:
                outcomes.append(('refused', str(exc)))

        threads = [threading.Thread(target=attempt, args=(key,)) for key in ('fourth-a', 'fourth-b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len(self.fake.posts), posts + 1)
        sent = [outcome for kind, outcome in outcomes if kind == 'sent']
        refused = [outcome for kind, outcome in outcomes if kind == 'refused']
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(refused), 1)
        self.assertIn('active or uncertain', refused[0])


class SettledHistoryPostGrantMaintenanceTests(SettledHistoryFixture):
    """Post-grant maintenance keeps the settled-history grant's own agreement evidence exactly like
    the baseline. Reviewer recovery after a settled-history grant is covered by
    SettledHistoryReviewerRecoveryTests below."""

    def read_native_owner(self):
        return extension.ao_native_identity.read_owner(str(self.database), 'engineer')

    def test_routing_refresh_guard_returns_the_fourth_reviews_agreement_evidence(self):
        self.grant()
        self.send('spec_review', 'fourth')
        self.finish_fourth()
        before = copy.deepcopy(self.state())
        result = extension.guard_routing_refresh(self.service, self.state(), self.read_native_owner())
        self.assertEqual(result['consumed_by'], 'fourth')
        self.assertEqual(result['agreement']['request_id'], 'fourth')
        self.assertEqual(result['grant_receipt_sha256'], before['spec_review_extension']['receipt_sha256'])
        self.assertEqual(self.state(), before)


class SettledHistoryReviewerRecoveryFixture(PreAcceptanceReviewerRecoveryFixture, SettledHistoryFixture):
    """Group 10 (T6): the reviewer-recovery-after-grant combination the class docstring above
    explains was previously attempted and dropped, retried with the one fix that docstring
    identifies as missing -- ``PreAcceptanceReviewerRecoveryFixture``'s own ``bind()`` override
    must run before any history is built, not retrofitted onto an already-bound fixture.

    No ``setUp()`` override is written here. Through this class's own MRO -- this class,
    ``PreAcceptanceReviewerRecoveryFixture``, ``SettledHistoryFixture``, ``PreAcceptanceFixture``,
    their shared ``Fixture`` ancestor -- attribute lookup for ``setUp`` finds
    ``SettledHistoryFixture.setUp`` (the only ``setUp`` defined anywhere in this chain), whose
    ``super().setUp()`` call reaches ``PreAcceptanceFixture.setUp``, whose own ``self.bind()`` call
    resolves, through this same instance's MRO, to ``PreAcceptanceReviewerRecoveryFixture.bind`` --
    shaping the reviewer snapshot and creating its two real git worktrees before any charter review
    -- and only then does ``SettledHistoryFixture.setUp`` call ``_build_settled_history(self)``,
    the shared helper both fixtures now build on, to build the settled history on top of the
    correctly-shaped binding. Empirically confirmed: this fixture's own ``setUp()`` builds cleanly
    (the ``Service.identity`` branch-change refusal the dropped attempt hit does not recur).

    A second, separate and genuinely structural limit was found empirically once ``setUp()`` itself
    worked, unrelated to settled history: ``recover_reviewer()`` (test_ao_preacceptance_review_
    extension.py:1208) calls ``ao_room_reviewer_recovery_audit`` -> ``ao_reviewer_recovery.audit``
    -> ``ao_workflow.engineering_ready`` -> ``ao_workflow.agreement`` (ao_workflow.py:99-106), which
    requires the room's *latest* ``spec_review`` request to already be agreed against the room's
    *current* spec revision. ``SettledHistoryFixture.setUp()`` ends with ``register()``, putting a
    fresh charter revision in place with nothing yet agreed against it; ``grant()`` consumes the
    one-time review-extension budget but still does not itself agree anything (it only authorizes
    one more ``spec_review`` send). So calling ``recover_reviewer()`` directly after ``grant()`` --
    before the fourth review is both sent and finished -- raised, unwrapped, exactly:
    ``room.RoomError: The current exact spec requires completed Fable agreement``
    (from ``ao_workflow.spec_review_report``, ao_workflow.py:82). This is lane-independent and
    reproduces identically on a plain ``PreAcceptanceReviewerRecoveryFixture`` room with no settled
    history at all if recovery is attempted in that same window, which is why the reference suite's
    own ``test_recovered_unused_reviewer_keeps_the_grant_valid_through_independent_acceptance``
    always calls ``recover_reviewer()`` only after its own ``verified_after_fourth()`` has fully
    sent and finished (agreed) the fourth review -- never in between. The test below follows that
    same achievable ordering, adapted to a settled history: recovery is attempted immediately after
    the fourth review's own completion, not before it.

    A third limit, found empirically once the second was fixed, explains why
    ``verified_after_fourth()`` always also calls ``implement_after_fourth()`` before any recovery
    attempt: ``recover_reviewer()``'s own audit also calls ``ao_workflow.engineering_ready``, which
    re-observes the room's *latest* ``implementation``/``correction`` request fresh
    (``ao_outcomes.py:632-639``). In a settled history that request is this fixture's own
    settled-history build's final scope-change-report turn, whose retained baseline predates the
    fourth review; re-observing it after 'fourth' has been sent finds one more live, non-recovered
    turn than its own baseline accounts for ('fourth' itself) and refuses, unwrapped, with exactly:
    ``room.RoomError: Cannot attribute semantic outcome to an unchanged completed native result``.
    A fresh ``implement_after_fourth()``-style implementation turn -- sent, completed, and so
    itself becoming the latest implementation/correction with a baseline that already accounts for
    'fourth' -- clears this the same way it does in the reference fixture. ``ao_reviewer_recovery``'s
    own audit then separately requires a passed verification checkpoint to already exist
    (``ao_project_room.checkpoint``, ao_project_room.py:750: ``'Create a passed verification
    checkpoint first'``), so ``ao_room_verify`` must also run once, exactly as
    ``verified_after_fourth()`` already does in the reference fixture.
    """


class SettledHistoryReviewerRecoveryTests(SettledHistoryReviewerRecoveryFixture):
    def test_reviewer_recovery_after_the_settled_historys_fourth_review_keeps_the_grant_valid(self):
        self.grant()
        self.send('spec_review', 'fourth')
        self.assertTrue(self.finish_fourth()['agreement']['agreed'])
        self.implement_after_fourth()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        before = copy.deepcopy(self.state())
        recovered = self.recover_reviewer()
        self.assertTrue(recovered['recovered'])
        after = self.state()
        self.assertEqual(after['bindings']['reviewer']['session_id'], 'replacement')
        self.assertEqual(after['bindings']['engineer'], before['bindings']['engineer'])
        self.assertEqual(after['spec_review_extension'], before['spec_review_extension'])
        record, evidence = extension.validate(self.service, after)
        self.assertEqual(record['lane'], extension.PRE)
        self.assertEqual(evidence['state']['bindings']['reviewer'], before['bindings']['reviewer'])
