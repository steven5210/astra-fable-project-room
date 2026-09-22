"""Misleading AO/native metadata probes for the acceptance-review continuation lane; no account,
provider or network access. Synthetic offline state only.

Two evidence sources feed ao_acceptance_extension._inspect (ao_acceptance_extension.py:431-585), and
each condition below corrupts exactly one of them, drives only the public Service operations, and
restores it exactly:

- The retained native owner database (ao_native_identity.read_owner/read_codex_owner, called from
  ao_acceptance_extension._owner, ao_acceptance_extension.py:368-390). A raised ValueError/OSError/
  KeyError/TypeError from the read/validate functions is wrapped into "The retained native <role> owner
  evidence is unavailable or contradictory"; a post-validation field mismatch against the retained
  room/session/workspace/supplied native session raises "The native <role> owner differs from the
  retained room, session, workspace or supplied native session". This file calls the first family
  UNAVAILABLE and the second DIFFERS.
- The live AO snapshot (ao_provider_transition._CompleteClient/_ReadOnlyIdentity.identity and
  ao_review_extension._native_evidence -> ao_provider_transition._native, all reached from
  ao_acceptance_extension._inspect, ao_acceptance_extension.py:508-525).

Every probe uses the same four-phase shape (see NativeMetadataFixture._probe): (1) audit refuses with
the pinned message and stores nothing; (2) with a valid audit taken before the condition, extend
refuses and writes no journal record; (3) with a valid unused grant made before the condition, the
named send refuses with no POST, no consumption, unchanged journal bytes, and the grant's summary
state still 'unconsumed'; (4) once the metadata is restored exactly, the named send succeeds with
exactly one POST. Phase 3's expected state is always 'unconsumed', never 'inconsistent': validate()
and summary() are offline-only (ao_acceptance_extension.py docstrings at :338 "Offline integrity and
accounting only; never performs an AO call or a save" and the summary() docstring at :818) -- neither
one opens the native owner database or calls the live AO client, so an owner-database or live-snapshot
condition can only ever be diagnosed by the dispatch-time _inspect() checked here, never by status.

Must not duplicate: test_p8_freshness_drift_categories in test_ao_acceptance_extension_matrix.py
(native_reviewer_extra_turn/native_engineer_extra_turn already cover one extra 'completed'-state turn
per role; owner_reviewer_conversation already covers a conversation_branches-only provider_conversation_id
replacement for the reviewer, producing UNAVAILABLE; owner_engineer_workspace already covers an
engineer workspace_path change, producing DIFFERS) or
test_substituted_reviewer_model_or_effort_refuses_before_grant in test_ao_acceptance_extension.py
(reviewer-only live model/effort substitution). This file's provider-conversation-id condition updates
both the sessions and conversation_branches tables consistently (unlike p8's branches-only mutation) to
reach DIFFERS rather than duplicate p8's UNAVAILABLE case, and its model/effort condition targets the
engineer, not the reviewer.

Every condition drawn from the assignment's list was reachable and refused with the fakes available
here; there is no "not refused" or "not expressible" table below the two test classes because none was
needed -- each condition's code path was traced against the current source before writing its test.
"""
import copy
import sqlite3
import unittest

import ao_project_room as ao
import ao_acceptance_extension as extension
from test_ao_acceptance_extension import AcceptanceContinuationFixture


class NativeMetadataFixture(AcceptanceContinuationFixture):
    """Shared probe helpers on top of the complete base fixture (three retained reviewer attempts, a
    passed verification, and the synthetic native-owner database at self.database; see
    AcceptanceContinuationFixture.setUp in test_ao_acceptance_extension.py)."""

    def sql(self, statement):
        with sqlite3.connect(self.database) as db:
            db.execute(statement)

    def _lane_files(self):
        base = self.directory() / extension.BASE
        if not base.exists():
            return None
        return sorted(str(p.relative_to(base)) for p in base.rglob('*') if p.is_file())

    def _probe(self, break_evidence, restore_evidence, fragment, review):
        """Drive audit/extend/send through the misleading-then-restored condition; see the module
        docstring for the four phases and why phase 3 always expects 'unconsumed'."""
        posts = len(self.fake.posts)

        before_lane = self._lane_files()
        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.audit(review)
            self.assertEqual(self._lane_files(), before_lane, 'a refused audit must store nothing new in the lane')
        finally:
            restore_evidence()

        audit_sha256 = self.audit(review)['audit_sha256']
        inputs = dict(room_id=self.room, audit_sha256=audit_sha256, request_id='grant-' + review,
                      authorization='Actual synthetic user approval for needed additional reviews.',
                      authorization_reference='synthetic-user-message',
                      diagnosis='A necessary further independent review in the same scope.')

        before_lane = self._lane_files()
        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.service.ao_room_acceptance_review_extend(**inputs)
            self.assertEqual(self._lane_files(), before_lane, 'a refused extend must write no journal record')
        finally:
            restore_evidence()

        self.service.ao_room_acceptance_review_extend(**inputs)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        self.assertEqual(len(self.fake.posts), posts)

        break_evidence()
        try:
            with self.assertRaisesRegex(ao.RoomError, fragment):
                self.send('acceptance_review', review)
            self.assertEqual(len(self.fake.posts), posts, 'a refused send must not POST')
            self.assertNotIn(review, self.state()['requests'], 'a refused send must not consume the grant')
            self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
        finally:
            restore_evidence()

        result = self.send('acceptance_review', review)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
        self.finish_review(review, 'rejected')

    def _db_probe(self, statements, fragment, review):
        """Owner-database conditions: mutate self.database's bytes in place, restore the originals."""
        if isinstance(statements, str):
            statements = [statements]
        original = self.database.read_bytes()

        def break_evidence():
            for statement in statements:
                self.sql(statement)

        def restore_evidence():
            self.database.write_bytes(original)

        self._probe(break_evidence, restore_evidence, fragment, review)

    def _snapshot_probe(self, role, mutate, fragment, review):
        """Live-AO-snapshot conditions: deep-copy self.fake.snapshots[role], mutate it, restore it."""
        original = copy.deepcopy(self.fake.snapshots[role])

        def break_evidence():
            mutate(self.fake.snapshots[role])

        def restore_evidence():
            self.fake.snapshots[role] = copy.deepcopy(original)

        self._probe(break_evidence, restore_evidence, fragment, review)

    def _session_probe(self, role, mutate, fragment, review):
        """Live-AO-session conditions (e.g. isTerminated): deep-copy self.fake.sessions[role]."""
        original = copy.deepcopy(self.fake.sessions[role])

        def break_evidence():
            mutate(self.fake.sessions[role])

        def restore_evidence():
            self.fake.sessions[role] = copy.deepcopy(original)

        self._probe(break_evidence, restore_evidence, fragment, review)


class OwnerDatabaseIntegrityTests(NativeMetadataFixture):
    """Every condition here corrupts the synthetic native-owner sqlite database consulted by
    ao_acceptance_extension._owner (ao_acceptance_extension.py:368-390) for one role, engineer or
    reviewer, while leaving the other role's row untouched."""

    UNAVAILABLE = 'The retained native {role} owner evidence is unavailable or contradictory'
    DIFFERS = 'The native {role} owner differs from the retained room, session, workspace or supplied native session'

    def test_harness_mismatch_refuses_for_each_role(self):
        """ao_native_identity._validated/_validated_codex require harness=='claude-code' for the
        engineer and 'codex' for the reviewer (ao_native_identity.py:46, :104)."""
        cases = [
            ('engineer', "UPDATE sessions SET harness='codex' WHERE id='engineer'"),
            ('reviewer', "UPDATE sessions SET harness='claude-code' WHERE id='reviewer'"),
        ]
        for role, statement in cases:
            with self.subTest(role=role):
                self._db_probe(statement, self.UNAVAILABLE.format(role=role), 'fourth-harness-' + role)

    def test_session_terminated_refuses(self):
        """_validated requires is_terminated==0 (ao_native_identity.py:46)."""
        self._db_probe("UPDATE sessions SET is_terminated=1 WHERE id='engineer'",
                       self.UNAVAILABLE.format(role='engineer'), 'fourth-terminated')

    def test_wrong_session_mode_refuses(self):
        """_validated_codex requires session_mode=='chat' (ao_native_identity.py:107)."""
        self._db_probe("UPDATE sessions SET session_mode='background' WHERE id='reviewer'",
                       self.UNAVAILABLE.format(role='reviewer'), 'fourth-mode')

    def test_branch_strategy_not_native_refuses(self):
        """_validated requires strategy in ('', 'native'); an unknown nonempty strategy is never
        normalized (ao_native_identity.py:49)."""
        self._db_probe("UPDATE conversation_branches SET strategy='legacy' WHERE session_id='engineer'",
                       self.UNAVAILABLE.format(role='engineer'), 'fourth-strategy')

    def test_replay_truncated_refuses(self):
        """_validated_codex requires replay_truncated==0 (ao_native_identity.py:108)."""
        self._db_probe("UPDATE conversation_branches SET replay_truncated=1 WHERE session_id='reviewer'",
                       self.UNAVAILABLE.format(role='reviewer'), 'fourth-replay')

    def test_current_session_or_active_branch_elsewhere_refuses(self):
        """The owner QUERY joins conversations.current_session_id=sessions.id and
        conversation_branches.id=conversations.active_branch_id (ao_native_identity.py:17-19); pointing
        either elsewhere leaves zero matching rows, the same 'missing or ambiguous' ValueError
        (ao_native_identity.py:38) as the harness/mode/strategy conditions above."""
        cases = [
            ('current_session', 'engineer',
             "UPDATE conversations SET current_session_id='elsewhere' WHERE id='engineer-native'"),
            ('active_branch', 'reviewer',
             "UPDATE conversations SET active_branch_id='elsewhere' WHERE id='reviewer-native'"),
        ]
        for name, role, statement in cases:
            with self.subTest(case=name):
                self._db_probe(statement, self.UNAVAILABLE.format(role=role), 'fourth-elsewhere-' + name)

    def test_provider_conversation_id_differs_from_supplied_native_session_id(self):
        """Updating both sessions.provider_conversation_id and the matching
        conversation_branches.provider_conversation_id keeps _validated's internal
        branch_provider_conversation_id==provider_conversation_id check satisfied
        (ao_native_identity.py:53), so this reaches _owner's own post-validation comparison against the
        native session ID supplied to audit() (ao_acceptance_extension.py:380) rather than
        test_p8_freshness_drift_categories's branches-only owner_reviewer_conversation mutation, which
        instead breaks that internal check and produces UNAVAILABLE."""
        statements = ["UPDATE sessions SET provider_conversation_id='drifted-native-id' WHERE id='engineer'",
                      "UPDATE conversation_branches SET provider_conversation_id='drifted-native-id' "
                      "WHERE session_id='engineer'"]
        self._db_probe(statements, self.DIFFERS.format(role='engineer'), 'fourth-provider-drift')

    def test_project_id_differs_from_room_project(self):
        """_owner compares owner['project_id'] against the room's own ao_project_id
        (ao_acceptance_extension.py:377); _validated_codex only requires project_id to be a nonempty
        string (ao_native_identity.py:54), so an unequal-but-nonempty value passes storage validation
        and is caught here instead."""
        self._db_probe("UPDATE sessions SET project_id='other-project' WHERE id='reviewer'",
                       self.DIFFERS.format(role='reviewer'), 'fourth-project-drift')

    def test_duplicate_ambiguous_rows_refuse(self):
        """A second sessions row for the same id makes the owner QUERY return two rows instead of one,
        the same ValueError as a JOIN miss (ao_native_identity.py:37-38)."""
        self._db_probe("INSERT INTO sessions SELECT * FROM sessions WHERE id='engineer'",
                       self.UNAVAILABLE.format(role='engineer'), 'fourth-duplicate')

    def test_swapped_native_session_ids_refuse_audit_before_any_storage(self):
        """The two native session IDs are audit()'s own caller arguments (engineer_native_session_id,
        reviewer_native_session_id), baked permanently into that one audit's saved target
        (ao_acceptance_extension.py:595-598) and re-verified unchanged on every later phase: extend
        reads target = saved['evidence']['target'] from the SAVED audit record, never fresh arguments
        (ao_acceptance_extension.py:663), and admission's target comes from the committed grant, not
        from any per-send argument either. A swap therefore can never reach a stored audit, extend or
        admission: audit() is the only gate that ever accepts these two arguments, and _owner verifies
        them (engineer first, since 'engineer' precedes 'reviewer' in the _inspect owners dict literal
        at ao_acceptance_extension.py:530-533) before any write. Only phase (1) applies here: phases
        (2)-(4) have no meaning for an argument pair that is never mutable state to introduce and then
        restore around an existing grant. The positive control below shows the same review name, given
        the correct (unswapped) arguments, produces a normal eligible audit."""
        before = self._lane_files()
        posts = len(self.fake.posts)
        with self.assertRaisesRegex(ao.RoomError, self.DIFFERS.format(role='engineer')):
            self.service.ao_room_acceptance_review_audit(
                self.room, 'fourth-swapped', ao.digest(self.message.encode()), str(self.database),
                'synthetic-native-reviewer', 'synthetic-native-engineer')
        self.assertEqual(self._lane_files(), before, 'a refused audit must store nothing new in the lane')
        self.assertEqual(len(self.fake.posts), posts)
        self.assertTrue(self.audit('fourth-swapped')['eligible'])


class LiveNativeEvidenceIntegrityTests(NativeMetadataFixture):
    """Every condition here corrupts the live AO snapshot/session consulted by
    ao_provider_transition._native (via ao_review_extension._native_evidence),
    ao_provider_transition._CompleteClient, or ao_project_room.Service.identity -- all reached from
    ao_acceptance_extension._inspect (ao_acceptance_extension.py:508-525) before ao_outcomes.gate or the
    native-owner-database checks run, so none of these conditions can be masked or altered by that
    later machinery."""

    def test_branch_materialization_replay_truncation_or_non_native_strategy_refuses(self):
        """ao_provider_transition._native requires branchMaterialization.strategy=='native' and
        replayTruncated absent-or-false."""
        cases = [
            ('strategy', 'engineer', lambda snapshot: snapshot['branchMaterialization'].__setitem__('strategy', 'legacy'),
             'Provider transition requires retained native materialization'),
            ('replay_truncated', 'reviewer',
             lambda snapshot: snapshot['branchMaterialization'].__setitem__('replayTruncated', True),
             'Native materialization replayTruncated must be absent or exactly false; replay truncation refuses'),
        ]
        for name, role, mutate, fragment in cases:
            with self.subTest(case=name):
                self._snapshot_probe(role, mutate, fragment, 'fourth-materialization-' + name)

    def test_session_reported_terminated_refuses(self):
        """ao_provider_transition._CompleteClient enforces isTerminated==False on every /sessions/<id>
        response before _inspect's own busy() or _native() checks even run."""
        self._session_probe('engineer', lambda session: session.__setitem__('isTerminated', True),
                            'Provider adoption requires native session isTerminated=false; terminated or '
                            'unknown lifecycle refuses', 'fourth-live-terminated')

    def test_engineer_model_or_effort_substituted_refuses_before_grant(self):
        """ao_project_room.Service.identity compares snapshot.settings against the bound engineer's
        pinned model/reasoning_effort (ao_project_room.py:322); the equivalent reviewer condition is
        already covered by test_substituted_reviewer_model_or_effort_refuses_before_grant in
        test_ao_acceptance_extension.py, so only the engineer is exercised here."""
        for field, value in (('model', 'other-model'), ('reasoningEffort', 'high')):
            with self.subTest(field=field):
                def mutate(snapshot, field=field, value=value):
                    snapshot['settings'][field] = value
                self._snapshot_probe('engineer', mutate, 'AO configured model/effort changed or is unavailable',
                                     'fourth-engineer-' + field)

    def test_retained_turn_missing_from_native_history_refuses(self):
        """Removing a retained request's own turn from the live history reaches _native's final
        ownership check, 'Owned native turns are missing from the observed history': the earlier
        per-request receipt/sent_message checks in the same loop all read the saved receipt and
        snapshot['messages'] (untouched here), not snapshot['turns'], so they pass before this one
        fires."""
        turn_id = self.state()['requests']['prior-1']['turn_id']

        def mutate(snapshot, turn_id=turn_id):
            snapshot['turns'][:] = [turn for turn in snapshot['turns'] if turn['id'] != turn_id]

        self._snapshot_probe('reviewer', mutate, 'Owned native turns are missing from the observed history',
                             'fourth-turn-missing')

    def test_retained_turn_provider_id_contradicts_saved_request_refuses(self):
        """Changing a retained request's own turn's providerTurnId in the live history (without
        removing it) passes the ownership-presence check above but then fails _native's later
        cross-check against the saved request's own provider_turn_id. This targets the engineer's
        'charter-1' spec-review turn rather than 'implementation': ao_workflow.engineering_ready
        (ao_workflow.py:241-272) re-observes 'implementation' specifically (the request named by
        ao_workflow.latest(state, {'implementation', 'correction'}), ao_workflow.py:246) through
        ao_outcomes.observe (ao_outcomes.py:157-173) before _inspect ever reaches _native
        (ao_acceptance_extension.py:497 precedes :519-525), and observe's own providerTurnId cross-check
        (ao_outcomes.py:170) would misattribute a tampered 'implementation' turn to that earlier,
        differently-worded generic check ('Cannot attribute semantic outcome to an unchanged completed
        native result', ao_outcomes.py:173) instead of _native's own message. observe() only inspects
        the single turn whose id equals its own target request's turn_id (ao_outcomes.py:165), so a
        tampered 'charter-1' turn (baked into 'implementation's own retained baseline turn_ids, since
        'charter-1' was sent and completed before 'implementation' began, and therefore never treated
        as a disqualifying extra/unaccounted turn either, ao_outcomes.py:169) is invisible to that
        earlier check and reaches _native's own cross-check at every phase, confirmed empirically for
        audit, extend and admission alike."""
        turn_id = self.state()['requests']['charter-1']['turn_id']

        def mutate(snapshot, turn_id=turn_id):
            for turn in snapshot['turns']:
                if turn['id'] == turn_id:
                    turn['providerTurnId'] = 'tampered-provider-turn-id'

        self._snapshot_probe('engineer', mutate,
                             'Observed native provider turn identity differs from the saved request',
                             'fourth-turn-contradicts')

    def test_non_completed_native_turn_refuses_for_each_role(self):
        """Setting the state of a retained, already-baselined turn to a terminal-but-not-completed
        value (failed/interrupted/cancelled) reaches _native's own turn-state check, 'Native history
        contains a turn that is not completed' (ao_provider_transition.py:313-318, which scans every
        turn unconditionally, not just owned ones). The mutated turn is deliberately not each role's
        LATEST retained request (engineer: 'charter-1', sent and completed before 'implementation';
        reviewer: 'prior-1', sent and completed before 'prior-3') so that the same earlier, coarser
        ao_outcomes.observe() re-verification that motivated retargeting the providerTurnId test above
        -- reached for the engineer through ao_workflow.engineering_ready (ao_workflow.py:252) and for
        the reviewer through ao_outcomes.gate (ao_acceptance_extension.py:527, and again, for an actual
        send, through ao_project_room.Service.quiet at ao_project_room.py:371 before dispatch even
        reaches ao_acceptance_extension) -- never inspects it either: observe()/gate() only ever look at
        the single latest-for-role request's own turn (ao_outcomes.py:165, :295), and the "extra turn
        beyond baseline" comparison (ao_outcomes.py:169) subtracts by turn id, not by state, so an
        earlier turn's state change never appears as a new, unaccounted turn. Confirmed empirically
        (jobs/acceptance-continuation/review-3/nativeworker_scratch_turnstate.py, deleted after use) to
        produce the identical _native message at all three call sites (audit, extend, admission) for
        every one of these three states, for both roles. 'active' is deliberately excluded here: it is
        not in ao_project_room.NATIVE_TERMINAL (ao_project_room.py:33-36), so it is caught earlier by an
        ao.busy() gate instead of by this turn-state check at all -- and, unlike this test's three
        states, busy() is reached by a different call site with a different exact message for audit/
        extend (ao_acceptance_extension.py:517) versus a live send (ao_project_room.py:371-372, via
        Service.quiet, reached before ao_acceptance_extension is even imported); see the dedicated
        active-turn test below for that three-message shape."""
        not_completed = 'Native history contains a turn that is not completed'
        baselined_turn_id = {'engineer': self.state()['requests']['charter-1']['turn_id'],
                             'reviewer': self.state()['requests']['prior-1']['turn_id']}
        for role in ('engineer', 'reviewer'):
            for state in ('failed', 'interrupted', 'cancelled'):
                with self.subTest(role=role, state=state):
                    def mutate(snapshot, turn_id=baselined_turn_id[role], state=state):
                        for turn in snapshot['turns']:
                            if turn['id'] == turn_id:
                                turn['state'] = state
                    self._snapshot_probe(role, mutate, not_completed, 'fourth-turn-state-' + role + '-' + state)

    def test_active_native_turn_refuses_with_a_different_message_at_each_call_site(self):
        """The 'active' turn state is not in ao_project_room.NATIVE_TERMINAL, so it never reaches
        _native's own turn-state check at all (ao_provider_transition.py:290 is only ever called once
        ao.busy() has already let a snapshot through); every phase still refuses, but audit/extend and
        an actual send are refused by two different call sites with two different exact messages, so
        this condition cannot use the shared four-phase _probe helper (which pins one fragment for every
        phase) and is verified explicitly here instead. Audit and extend call
        ao_acceptance_extension.audit()/extend() directly, reaching only _inspect's own busy() loop
        (ao_acceptance_extension.py:515-518, 'A bound AO conversation has active work; the
        acceptance-review continuation refuses before any write'). An actual send instead goes through
        ao_project_room.Service.ao_room_send, which calls self.quiet(state) (ao_project_room.py:608)
        before ao_acceptance_extension.before_send is even imported; quiet() (ao_project_room.py:367-372)
        checks busy() against every bound role's live snapshot itself and raises the shorter 'A bound AO
        conversation has active work' (no trailing clause) first. Confirmed empirically
        (jobs/acceptance-continuation/review-3/nativeworker_scratch_turnstate.py, deleted after use)."""
        extension_busy = ('A bound AO conversation has active work; the acceptance-review continuation '
                          'refuses before any write')
        send_busy = 'A bound AO conversation has active work'
        baselined_turn_id = {'engineer': self.state()['requests']['charter-1']['turn_id'],
                             'reviewer': self.state()['requests']['prior-1']['turn_id']}
        for role in ('engineer', 'reviewer'):
            with self.subTest(role=role):
                review = 'fourth-turn-state-' + role + '-active'
                turn_id = baselined_turn_id[role]

                def mutate(turn_id=turn_id):
                    for turn in self.fake.snapshots[role]['turns']:
                        if turn['id'] == turn_id:
                            turn['state'] = 'active'

                def restore(turn_id=turn_id):
                    for turn in self.fake.snapshots[role]['turns']:
                        if turn['id'] == turn_id:
                            turn['state'] = 'completed'

                posts = len(self.fake.posts)
                before_lane = self._lane_files()
                mutate()
                try:
                    with self.assertRaisesRegex(ao.RoomError, extension_busy):
                        self.audit(review)
                    self.assertEqual(self._lane_files(), before_lane)
                finally:
                    restore()

                audit_sha256 = self.audit(review)['audit_sha256']
                inputs = dict(room_id=self.room, audit_sha256=audit_sha256, request_id='grant-' + review,
                              authorization='Actual synthetic user approval for needed additional reviews.',
                              authorization_reference='synthetic-user-message',
                              diagnosis='A necessary further independent review in the same scope.')
                before_lane = self._lane_files()
                mutate()
                try:
                    with self.assertRaisesRegex(ao.RoomError, extension_busy):
                        self.service.ao_room_acceptance_review_extend(**inputs)
                    self.assertEqual(self._lane_files(), before_lane)
                finally:
                    restore()

                self.service.ao_room_acceptance_review_extend(**inputs)
                self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
                self.assertEqual(len(self.fake.posts), posts)

                mutate()
                try:
                    with self.assertRaisesRegex(ao.RoomError, send_busy):
                        self.send('acceptance_review', review)
                    self.assertEqual(len(self.fake.posts), posts)
                    self.assertNotIn(review, self.state()['requests'])
                    self.assertEqual(extension.summary(self.service, self.state())['state'], 'unconsumed')
                finally:
                    restore()

                result = self.send('acceptance_review', review)
                self.assertEqual(result['state'], 'submitted')
                self.assertEqual(len(self.fake.posts), posts + 1)
                self.assertEqual(extension.summary(self.service, self.state())['state'], 'consumed')
                self.finish_review(review, 'rejected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
