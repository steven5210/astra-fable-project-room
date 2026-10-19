"""The recovered-context lifecycle, post-consumption retention and live-error
contradiction groups of the settled-history lane, moved out of
test_ao_preacceptance_settled_history so the two halves run as separate
processes under the worker pool; the classes are independent (each builds its
own template) and share only the fixture base and helpers imported from the
original module.

Synthetic data only: no real user names, machine paths, room ids or private context.
"""
import copy
import json
from unittest.mock import patch

import ao_native_outcome as native_outcome
import ao_outcomes
import ao_project_room as ao
import ao_review_extension as extension
from test_ao_preacceptance_review_extension import PreAcceptanceFixture
from test_ao_preacceptance_settled_history import (
    SETTLEMENT_EVIDENCE_CHANGED_MESSAGE, SettledHistoryFixture)


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

    TEMPLATE_ATTRS = PreAcceptanceFixture.TEMPLATE_ATTRS + ('recovered_turn',)

    def build_template(self):
        super().build_template()
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

    TEMPLATE_ATTRS = SettledHistoryFixture.TEMPLATE_ATTRS + ('recovered_turn',)

    def build_template(self):
        super().build_template()
        self.recovered_turn = _splice_compaction_import_proof(self)['recovered_turn']

    def after_restore(self):
        # Re-bind ``self.recovered_turn`` to the live snapshot row; a deepcopy restores it detached.
        super().after_restore()
        self.recovered_turn = next(
            turn for turn in self.fake.snapshots['engineer']['turns']
            if turn['id'] == 'recovered-turn-1')

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

    TEMPLATE_ATTRS = SettledHistoryFixture.TEMPLATE_ATTRS + ('splice', 'recovered_turn')

    def build_template(self):
        super().build_template()
        self.splice = _splice_compaction_import_proof(self)
        self.recovered_turn = self.splice['recovered_turn']

    def after_restore(self):
        # Re-bind both names to the live snapshot row: a test mutates ``splice['recovered_turn']``
        # to drift the live history, which a deepcopied dict could never reach.
        super().after_restore()
        live = next(turn for turn in self.fake.snapshots['engineer']['turns']
                    if turn['id'] == 'recovered-turn-1')
        self.splice['recovered_turn'] = live
        self.recovered_turn = live

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


class PostConsumptionSettlementRetentionTests(SettledHistoryFixture):
    """Group 12: once the fourth review is consumed and completed, the settlement evidence the
    grant was authenticated against stays pinned, not only the ordinary tracked fields _retained's
    own baseline-diff loop already covers. A content-addressed copy of one settled request's own
    outcome-resume release, re-pointed from state.json with the original release retained
    untouched, refuses every later evidence check -- ordinary validation, an identical grant
    replay, the routing-refresh guard and the handoff before any further send -- with the
    pinned-entry message. A re-point that also breaks the release's own authentication -- only
    the state-recorded pointer digest drifting, or an outcome_sha256 naming no record of this
    request -- is refused by that same pin with the settlement validator's own message.
    """

    TEMPLATE_ATTRS = SettledHistoryFixture.TEMPLATE_ATTRS + ('inputs',)

    def build_template(self):
        super().build_template()
        self.grant()
        self.send('spec_review', 'fourth')
        self.finish_fourth()

    def _settled_request(self, index=0):
        request_id = self.settled[index]['request_id']
        return request_id, self.state()['requests'][request_id]

    def _repoint(self, request_id, **record_changes):
        """Write a content-addressed copy of ``request_id``'s own outcome-resume release with
        ``record_changes`` applied -- the original release file stays retained, untouched -- then
        re-point that request's outcome_resume/outcome_resume_sha256 to the copy. Returns the new
        record's own room-relative path."""
        state = self.state()
        request = state['requests'][request_id]
        record = ao.read(self.directory() / request['outcome_resume'])
        record.update(record_changes)
        sha256 = ao.digest(record)
        new_path = 'outcome-resumes/' + request_id + '/' + sha256 + '.json'
        ao.atomic(self.directory() / new_path, record)
        tampered = copy.deepcopy(state)
        tampered['requests'][request_id].update(outcome_resume=new_path, outcome_resume_sha256=sha256)
        ao.atomic(self.directory() / 'state.json', tampered)
        return new_path

    def test_post_consumption_release_diagnosis_repoint_refuses_every_later_entry_point(self):
        first_id, first = self._settled_request(0)
        original_path = first['outcome_resume']
        original_bytes = (self.directory() / original_path).read_bytes()
        before_files = self.room_files()

        new_path = self._repoint(
            first_id, diagnosis='Hostile replacement of the previously granted settlement diagnosis.')

        # The original release stays retained, byte-for-byte, beside the new content-addressed copy.
        self.assertEqual((self.directory() / original_path).read_bytes(), original_bytes)
        posts = len(self.fake.posts)

        with self.assertRaisesRegex(ao.RoomError, SETTLEMENT_EVIDENCE_CHANGED_MESSAGE):
            extension.validate(self.service, self.state())
        with self.assertRaisesRegex(ao.RoomError, SETTLEMENT_EVIDENCE_CHANGED_MESSAGE):
            self.service.ao_room_preacceptance_review_extend(**self.inputs)
        with self.assertRaisesRegex(ao.RoomError, SETTLEMENT_EVIDENCE_CHANGED_MESSAGE):
            extension.guard_routing_refresh(
                self.service, self.state(),
                extension.ao_native_identity.read_owner(str(self.database), 'engineer'))
        with self.assertRaisesRegex(ao.RoomError, SETTLEMENT_EVIDENCE_CHANGED_MESSAGE):
            self.implement_after_fourth()

        self.assertEqual(len(self.fake.posts), posts)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)
        after_files = self.room_files()
        changed = {path for path in set(before_files) | set(after_files)
                   if before_files.get(path) != after_files.get(path)}
        self.assertEqual(changed, {new_path, 'state.json'})

    def test_outcome_resume_sha256_pointer_drift_refuses_ordinary_validate(self):
        """Pointer digest drift only: the release file at outcome_resume is untouched, but the
        state-recorded outcome_resume_sha256 no longer matches its real bytes, so the pinned
        entry's own release authentication refuses before any entry comparison."""
        first_id, first = self._settled_request(0)
        digit = '1' if first['outcome_resume_sha256'][0] == '0' else '0'
        bogus = digit + first['outcome_resume_sha256'][1:]
        tampered = copy.deepcopy(self.state())
        tampered['requests'][first_id]['outcome_resume_sha256'] = bogus
        ao.atomic(self.directory() / 'state.json', tampered)
        with self.assertRaisesRegex(ao.RoomError, 'Outcome continuation has no exact owned path'):
            extension.validate(self.service, self.state())

    def test_release_outcome_sha256_source_repoint_refuses_ordinary_validate(self):
        """A distinct mutation from the diagnosis-repoint test above, sharing only its copy-and-
        repoint mechanism: the release copy's own outcome_sha256 pointer -- the one release field
        _settled_failures itself surfaces into the pinned settled_failures entry -- now carries
        another settled request's outcome digest, which names no record under this request's own
        outcomes directory, so the pinned entry's settlement validation refuses it as missing
        before any entry comparison."""
        first_id, first = self._settled_request(0)
        second_id, second = self._settled_request(1)
        foreign_release = ao_outcomes.release_record(self.directory(), {'room_id': self.directory().name}, second)
        original = ao.read(self.directory() / first['outcome_resume'])
        self.assertNotEqual(original['outcome_sha256'], foreign_release['outcome_sha256'])
        self._repoint(first_id, outcome_sha256=foreign_release['outcome_sha256'])
        with self.assertRaisesRegex(ao.RoomError, 'Settled native failure evidence is missing or unreadable'):
            extension.validate(self.service, self.state())

    def test_without_mutation_ordinary_validate_passes_and_implement_after_fourth_dispatches_once(self):
        record, evidence = extension.validate(self.service, self.state())
        self.assertEqual(record['lane'], extension.PRE)
        posts = len(self.fake.posts)
        self.implement_after_fourth()
        self.assertEqual(len(self.fake.posts), posts + 1)

    def test_a_later_post_consumption_settlement_keeps_ordinary_validate_passing(self):
        """Maintenance preservation: a fresh engineer request settled through the fixture's own
        supported settlement recipe, after the fourth review is already consumed and completed,
        neither disturbs ordinary validate nor blocks the correction send that recipe itself sends
        for its successor."""
        self.service.ao_room_handoff(self.room, str(self.repo))
        new_request_id = self._fresh_id()
        successor = self._settle(new_request_id, 'implementation', presend=True)
        self.assertEqual(self.state()['requests'][new_request_id]['state'], 'settled_failure')
        self.assertEqual(self.state()['requests'][successor]['state'], 'submitted')
        record, evidence = extension.validate(self.service, self.state())
        self.assertEqual(record['lane'], extension.PRE)


LIVE_ERROR_CONTRADICTION_MESSAGE = 'Settled native failure live error contradicts its settled receipt'
LIVE_STATE_CONTRADICTION_MESSAGE = 'Settled native failure live state contradicts its settled receipt'


class SettledFailureLiveErrorFixture(SettledHistoryFixture):
    """Group 13: a settled failed turn's live row is checked against that same request's
    authenticated, settled receipt before the pre-acceptance audit, the grant and the fourth-review
    send admit it. The live row must still read 'failed', as its receipt does: a row flipped to
    'completed' would otherwise pass ``_native`` as a plain completed turn, so any other live state
    refuses first with ``LIVE_STATE_CONTRADICTION_MESSAGE``. Its live errorMessage is tolerated only
    while it is absent, ``None`` or ``''`` (a native controller restart clears it, while the saved
    receipt keeps the original provider text) or exactly the receipt's own saved text; any other
    value contradicts what was actually settled and refuses with
    ``LIVE_ERROR_CONTRADICTION_MESSAGE``. Each refusal happens at all three boundaries, before any
    room mutation, POST or consumption. Only a live row that passes both checks is replaced by
    ``_terminal_identity(turn)`` (the live turn minus errorMessage) before the native-history
    comparison, so such a restart between grant and send leaves the audited evidence unchanged.

    ``fake.finish``'s own default 'failed' turn carries no errorMessage at all, so this class's
    ``_build_history`` override wraps ``fake.finish`` to stamp ``SAVED_ERROR_TEXT`` on the live
    turn the instant it is marked failed, before ``ao_room_sync`` reads it into the receipt. The one
    stock-fixture control test lives in the sibling class below, whose ``STAMPS_ERROR_TEXT = False``
    keeps the unmodified stock fixture (saved errorMessage ``None``); the template builds the whole
    class history once, so the per-test switch the old fixture used cannot survive templating.
    """

    SAVED_ERROR_TEXT = 'Rate limit'
    STAMPS_ERROR_TEXT = True

    def _build_history(self):
        if not self.STAMPS_ERROR_TEXT:
            super()._build_history()
            return
        finish = self.fake.finish

        def wrapped(session, *args, **kwargs):
            value = finish(session, *args, **kwargs)
            if kwargs.get('state') == 'failed':
                self.fake.snapshots[session]['turns'][-1]['errorMessage'] = self.SAVED_ERROR_TEXT
            return value

        self.fake.finish = wrapped
        super()._build_history()

    def _settled_request(self, index=0):
        request_id = self.settled[index]['request_id']
        return request_id, self.state()['requests'][request_id]

    def _settled_turn(self, index=0):
        """The exact live turn row ``self.settled[index]`` names, found by turn id."""
        turn_id = self.settled[index]['turn_id']
        return next(t for t in self.fake.snapshots['engineer']['turns'] if t['id'] == turn_id)

    def _saved_error_message(self, index=0):
        _, request = self._settled_request(index)
        receipt = ao.read(self.directory() / request['receipt'])
        return receipt['turn'].get('errorMessage')

    def _flip_settled_turn_to_completed(self, index=0):
        """Rewrite that live row as a plain completed turn: state 'completed', no errorMessage key."""
        turn = self._settled_turn(index)
        self.assertEqual((turn['state'], turn['errorMessage']), ('failed', self.SAVED_ERROR_TEXT))
        turn['state'] = 'completed'
        del turn['errorMessage']


class SettledFailureLiveErrorContradictionTests(SettledFailureLiveErrorFixture):
    def test_different_live_error_before_audit_refuses_the_audit_without_mutation(self):
        self.assertEqual(self._saved_error_message(), self.SAVED_ERROR_TEXT)
        turn = self._settled_turn()
        turn['errorMessage'] = 'Permission denied'
        self.assert_refuses_without_mutation(LIVE_ERROR_CONTRADICTION_MESSAGE, self.audit)
        self.assertEqual(extension._consumptions(self.directory()), [])

    def test_different_live_error_after_a_clean_audit_refuses_the_grant_without_mutation(self):
        audited = self.audit()
        turn = self._settled_turn()
        turn['errorMessage'] = 'Permission denied'
        inputs = self.grant_inputs(audit=audited)
        self.assert_refuses_without_mutation(
            LIVE_ERROR_CONTRADICTION_MESSAGE,
            lambda: self.service.ao_room_preacceptance_review_extend(**inputs))
        self.assertIsNone(self.state().get('spec_review_extension'))
        self.assertEqual(list((self.directory() / extension.BASE / 'requests').glob('*.json')), [])

    def test_different_live_error_after_the_grant_refuses_the_fourth_send_before_consumption(self):
        self.grant()
        _, request = self._settled_request()
        receipt_path = self.directory() / request['receipt']
        original_receipt = receipt_path.read_bytes()
        before_files = self.room_files()
        turn = self._settled_turn()
        turn['errorMessage'] = 'Permission denied'
        self._assert_fourth_send_refuses(LIVE_ERROR_CONTRADICTION_MESSAGE)
        self.assertEqual(self.room_files(), before_files)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)

    def test_original_live_error_text_still_audits_grants_and_sends_the_fourth_review_once(self):
        self.assertEqual(self._settled_turn()['errorMessage'], self.SAVED_ERROR_TEXT)
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.grant()
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)

    def test_cleared_live_error_none_after_the_grant_still_sends_the_fourth_review_once(self):
        self.grant()
        turn = self._settled_turn()
        turn['errorMessage'] = None
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)

    def test_cleared_live_error_empty_string_after_the_grant_still_sends_the_fourth_review_once(self):
        self.grant()
        turn = self._settled_turn()
        turn['errorMessage'] = ''
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)

    def test_cleared_live_error_before_audit_still_audits_grants_and_sends_the_fourth_review_once(self):
        turn = self._settled_turn()
        turn['errorMessage'] = None
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self.grant()
        posts = len(self.fake.posts)
        self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), posts + 1)
        self.assertEqual(len(extension._consumptions(self.directory())), 1)

    def test_live_state_flip_of_a_settled_turn_before_audit_refuses_the_audit_without_mutation(self):
        self._flip_settled_turn_to_completed()
        self.assert_refuses_without_mutation(LIVE_STATE_CONTRADICTION_MESSAGE, self.audit)
        self.assertEqual(extension._consumptions(self.directory()), [])

    def test_live_state_flip_of_a_settled_turn_after_a_clean_audit_refuses_the_grant_without_mutation(self):
        audited = self.audit()
        self.assertTrue(audited['eligible'])
        self._flip_settled_turn_to_completed()
        inputs = self.grant_inputs(audit=audited)
        self.assert_refuses_without_mutation(
            LIVE_STATE_CONTRADICTION_MESSAGE,
            lambda: self.service.ao_room_preacceptance_review_extend(**inputs))
        self.assertIsNone(self.state().get('spec_review_extension'))
        self.assertEqual(list((self.directory() / extension.BASE / 'requests').glob('*.json')), [])

    def test_live_state_flip_of_a_settled_turn_after_the_grant_refuses_the_fourth_send_before_consumption(self):
        self.grant()
        _, request = self._settled_request()
        receipt_path = self.directory() / request['receipt']
        original_receipt = receipt_path.read_bytes()
        before_files = self.room_files()
        self._flip_settled_turn_to_completed()
        self._assert_fourth_send_refuses(LIVE_STATE_CONTRADICTION_MESSAGE)
        self.assertEqual(self.room_files(), before_files)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)


class SettledFailureLiveErrorStockTests(SettledFailureLiveErrorFixture):
    """The stock-fixture control from the class above, in its own template: ``STAMPS_ERROR_TEXT =
    False`` leaves ``fake.finish`` unwrapped, so this room's settled turns keep the fake's default
    no-errorMessage row and the ``assertIsNone`` precondition below stays a genuine precondition."""

    STAMPS_ERROR_TEXT = False

    def test_nonempty_live_error_on_a_turn_settled_without_error_text_refuses_the_fourth_send(self):
        self.assertIsNone(self._saved_error_message())
        self.grant()
        before_files = self.room_files()
        turn = self._settled_turn()
        turn['errorMessage'] = 'Permission denied'
        self._assert_fourth_send_refuses(LIVE_ERROR_CONTRADICTION_MESSAGE)
        self.assertEqual(self.room_files(), before_files)
