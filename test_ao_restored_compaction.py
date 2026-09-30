"""Offline restored autocompact-thrashing observation tests; fake AO and synthetic fixtures only.

Every room, receipt, transcript, owner row and AO message is synthetic. No account, model, network,
real room identity, real user path or real transcript is used, and nothing here claims these tests
were executed.
"""
import copy
import json
from datetime import datetime, timezone
import unittest

import ao_engineering_transition as transition
import ao_native_outcome as native
import ao_outcomes as outcomes
import ao_project_room as ao
import ao_routing_refresh as refresh
from test_ao_compaction_failure import CompactionServiceFixture

ERROR_TEXT = native.COMPACTION_THRASHING_TEXT
RESTORED_ID = '00000000-0000-4000-8000-0000000000e1'
SECOND_ID = '00000000-0000-4000-8000-0000000000e2'
FIRST_DIAGNOSIS = ('Exact native autocompact-thrashing terminal error inspected; one bounded recovery '
                   'authorized')
FIRST_AUTHORIZATION = 'Continue once with bounded reads'


class RestoredCompactionTests(CompactionServiceFixture):
    """The supported restoration: AO appends one provider error row to the same failed turn."""

    def setUp(self):
        super().setUp()
        self.settle()

    # --- synthetic helpers ---------------------------------------------------------
    def request(self):
        return self.fixture.state()['requests'][self.request_id]

    def record(self):
        request = self.request()
        return ao.read(self.fixture.directory() / request['semantic_outcome'])

    def files(self, area):
        root = self.fixture.directory() / area
        return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*.json'))}

    def turn_rows(self):
        turn_id = self.request()['turn_id']
        return [m for m in self.fixture.fake.snapshots['engineer']['messages']
                if isinstance(m, dict) and m.get('turnId') == turn_id]

    def restored_row(self, **changes):
        row = {'kind': 'message', 'id': RESTORED_ID, 'turnId': self.request()['turn_id'], 'sequence': 2141,
               'revision': 1, 'role': 'assistant', 'origin': 'provider', 'text': ERROR_TEXT,
               'editAvailable': False, 'streaming': False, 'createdAt': '2026-09-29T23:55:52Z'}
        row.update(changes)
        return row

    def restore(self, **changes):
        row = self.restored_row(**changes)
        self.fixture.fake.snapshots['engineer']['messages'].append(row)
        return row

    def authorize(self):
        audit = self.audit()
        self.assertTrue(audit['resume_eligible'])
        return audit

    def renew(self, audit):
        f = self.fixture
        f.service.ao_room_outcome_resume(
            f.room, self.request_id, audit['outcome_sha256'], 'resume',
            'AO restored the exact native autocompact-thrashing error into the failed turn; the renewal binds the fresh audit',
            FIRST_AUTHORIZATION)
        return self.request()

    def stamp_for(self, request, delta):
        return min(datetime.fromtimestamp(request['created_at'] + delta, timezone.utc),
                   datetime.now(timezone.utc)).isoformat()

    def write_transcript(self, rows):
        self.transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.transcript.chmod(0o600)

    def unchanged(self, area, before):
        after = self.files(area)
        for name, raw in before.items():
            self.assertEqual(after.get(name), raw)

    def assert_refuses_without_writes(self):
        f = self.fixture
        outcomes_before = self.files('outcomes')
        receipts_before = self.files('receipts')
        state_before = copy.deepcopy(f.state())
        posts = self.posts()
        with self.assertRaises(ao.RoomError):
            self.audit()
        self.assertEqual(self.files('outcomes'), outcomes_before)
        self.assertEqual(self.files('receipts'), receipts_before)
        self.assertEqual(f.state(), state_before)
        self.assert_no_posts(posts)

    def prepare_model_audit(self):
        """The public model audit requires a ready controller and an idle retained owner."""
        f = self.fixture
        f.fake.snapshots['engineer']['controller'] = 'ready'
        f.owner['activity_state'] = 'idle'
        return f

    def model_audit(self):
        f = self.prepare_model_audit()
        return f.service.ao_room_engineer_model_audit(
            f.room, 'claude-opus-5-5', str(f.database), str(self.transcript))

    # --- the exact supported recovery path -----------------------------------------
    def test_explicit_audit_authorizes_restoration_renewal_and_one_continuation(self):
        f = self.fixture
        receipts_before = self.files('receipts')
        outcomes_before = self.files('outcomes')
        prior = copy.deepcopy(self.request())
        restored = self.restore()

        posts = self.posts()
        with self.assertRaises(ao.RoomError):
            f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        with self.assertRaises(ao.RoomError):
            f.service.ao_room_outcome_resume(f.room, self.request_id, prior['semantic_outcome_sha256'],
                                            'resume', FIRST_DIAGNOSIS, FIRST_AUTHORIZATION)
        self.assert_no_posts(posts)
        self.assertEqual(self.request()['semantic_outcome_sha256'], prior['semantic_outcome_sha256'])

        audit = self.authorize()
        self.assertNotEqual(audit['outcome_sha256'], prior['semantic_outcome_sha256'])
        record = self.record()
        observation = record['restored_compaction_observation']
        live = [m for m in f.fake.snapshots['engineer']['messages'] if m.get('turnId') == prior['turn_id']]
        self.assertEqual(observation['prior_outcome_sha256'], prior['semantic_outcome_sha256'])
        self.assertEqual(observation['added_message'], restored)
        self.assertEqual(observation['added_message_sha256'], ao.digest(restored))
        self.assertEqual(observation['live_messages_sha256'], ao.digest(live))
        self.assertEqual(observation['receipt_sha256'], prior['receipt_sha256'])
        self.assertEqual(observation['room_id'], f.room)
        self.assertEqual(observation['request_id'], self.request_id)
        self.assertEqual(observation['session_id'], prior['session_id'])
        self.assertEqual(observation['conversation_id'], prior['baseline']['conversation_id'])
        self.assertEqual(observation['branch_id'], prior['baseline']['branch_id'])
        self.assertEqual(observation['turn_id'], prior['turn_id'])
        self.assertEqual(observation['provider_turn_id'], prior['provider_turn_id'])
        self.assertEqual(observation['native_source'], record['native']['source'])
        self.assertEqual(observation['native_source_sha256'], record['native']['source_sha256'])
        self.assertEqual(observation['native_compaction_failure'], record['native']['compaction_failure'])
        self.assertNotIn('restored_compaction_observation',
                         ao.read(f.directory() / prior['semantic_outcome']))
        self.unchanged('receipts', receipts_before)

        outcomes_after = self.files('outcomes')
        request_after = copy.deepcopy(self.request())
        again = self.authorize()
        self.assertEqual(again['outcome_sha256'], audit['outcome_sha256'])
        self.assertEqual(self.files('outcomes'), outcomes_after)
        self.assertEqual(self.request(), request_after)
        self.assert_no_posts(posts)

        renewed = self.renew(audit)
        self.assertNotEqual(renewed['outcome_resume_sha256'], prior['outcome_resume_sha256'])
        self.assertNotEqual(renewed['outcome_resume'], prior['outcome_resume'])
        self.assertTrue((f.directory() / prior['outcome_resume']).is_file())
        posts = self.posts()
        result = f.do_refresh()
        self.assertFalse(result['model_dispatch'])
        self.assert_no_posts(posts)
        f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assertEqual(self.posts(), posts + 1)

        successor = f.state()['requests']['resume']
        self.assertEqual(successor['state'], 'submitted')
        workspace = f.native_row_workspace()
        model = (successor.get('engineering_resolution') or {}).get('expected_model') or successor['model']
        self.write_transcript(list(self.events) + [
            {'type': 'user', 'uuid': 'resume-caller', 'sessionId': f.native_session,
             'timestamp': self.stamp_for(successor, 0.001), 'cwd': workspace, 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': successor['text']}},
            {'type': 'assistant', 'uuid': 'resume-reply', 'sessionId': f.native_session,
             'timestamp': self.stamp_for(successor, 0.002), 'cwd': workspace, 'isSidechain': False,
             'message': {'role': 'assistant', 'model': model, 'id': 'resume-m0',
                         'content': [{'type': 'text', 'text': json.dumps(f.report())}], 'stop_reason': 'end_turn'}}])
        (f.repo / 'feature.txt').write_text('implemented\n')
        f.fake.finish('engineer', json.dumps(f.report()))
        f.service.ao_room_sync(f.room)

        directory, state = f.directory(), f.state()
        self.assertEqual(state['requests'][self.request_id]['state'], 'settled_failure')
        outcomes.validate_settlement(directory, state['requests'][self.request_id])
        history = transition._native_history(directory, state, state['bindings']['engineer'],
                                             f.fake.conversation('engineer'))
        owned = [item['request_id'] for item in history['owned_turns']]
        self.assertIn(self.request_id, owned)
        self.assertIn('resume', owned)
        self.unchanged('receipts', receipts_before)
        self.unchanged('outcomes', outcomes_before)

    # --- every other path stays strict ---------------------------------------------
    def test_unfixed_restoration_is_never_authorized_by_send_sync_transition_or_refresh(self):
        f = self.fixture
        self.restore()
        before = copy.deepcopy(self.request())
        posts = self.posts()
        with self.assertRaises(ao.RoomError):
            f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assert_no_posts(posts)
        f.service.ao_room_sync(f.room)
        request = self.request()
        self.assertEqual(request['semantic_outcome_sha256'], before['semantic_outcome_sha256'])
        self.assertNotIn('restored_compaction_observation', self.record())
        self.assertIn('semantic_observation_error', request)
        with self.assertRaises(ao.RoomError):
            f.do_refresh()
        self.assertFalse((f.directory() / refresh.BASE).exists())
        with self.assertRaises(ao.RoomError):
            transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                       f.fake.conversation('engineer'))

    def test_restoration_shape_variants_are_never_authorized(self):
        variants = ({'role': 'user'}, {'origin': 'human'}, {'streaming': True},
                    {'text': 'Autocompact is thrashing.'}, {'id': ''}, {'id': 'not a message id!'},
                    {'kind': 'system'}, {'error': 'safety'}, {'apiErrorStatus': 429},
                    {'stopReason': 'refusal'}, {'stop_reason': 'safety'},
                    {'httpStatus': 429}, {'http_status': 429},
                    {'stopReason': 'unknown'}, {'http_status': 'malformed'})
        for changes in variants:
            with self.subTest(changes=changes):
                row = self.restore(**changes)
                try:
                    self.assert_refuses_without_writes()
                finally:
                    self.fixture.fake.snapshots['engineer']['messages'].remove(row)

    def test_zero_numeric_contradictory_metadata_is_never_authorized(self):
        """A falsy numeric stop/status value is malformed metadata, never absence.

        Python evaluates 0 == False and 0.0 == False, so the previous membership check
        admitted a zero value under every contradictory key. Each of the four keys with 0
        and 0.0 must refuse the explicit audit with no outcome, receipt or state write and
        no POST, exactly like the nonempty variants.
        """
        for key in ('stopReason', 'stop_reason', 'httpStatus', 'http_status'):
            for value in (0, 0.0):
                with self.subTest(key=key, value=value):
                    row = self.restore(**{key: value})
                    try:
                        self.assert_refuses_without_writes()
                    finally:
                        self.fixture.fake.snapshots['engineer']['messages'].remove(row)

    def test_duplicate_identity_or_second_extra_is_never_authorized(self):
        f = self.fixture
        saved_id = self.turn_rows()[0]['id']
        row = self.restore(id=saved_id)
        try:
            self.assert_refuses_without_writes()
        finally:
            f.fake.snapshots['engineer']['messages'].remove(row)
        first = self.restore()
        second = self.restore(id=SECOND_ID)
        try:
            self.assert_refuses_without_writes()
        finally:
            f.fake.snapshots['engineer']['messages'].remove(second)
            f.fake.snapshots['engineer']['messages'].remove(first)

    def test_missing_or_changed_saved_row_is_never_authorized(self):
        f = self.fixture
        rows = f.fake.snapshots['engineer']['messages']
        saved = self.turn_rows()
        index = rows.index(saved[0])
        removed = rows.pop(index)
        self.restore()
        try:
            self.assert_refuses_without_writes()
        finally:
            rows.pop()
            rows.insert(index, removed)
        changed = dict(removed)
        changed['text'] = 'Changed saved prefix text.'
        rows[index] = changed
        self.restore()
        try:
            self.assert_refuses_without_writes()
        finally:
            rows.pop()
            rows[index] = removed

    def test_foreign_turn_restoration_row_is_not_an_observation(self):
        self.restore(turnId='some-other-turn')
        before = self.request()['semantic_outcome_sha256']
        audit = self.audit()
        self.assertEqual(audit['outcome_sha256'], before)
        self.assertNotIn('restored_compaction_observation', self.record())

    def test_incomplete_or_reverted_restoration_is_never_authorized(self):
        f = self.fixture
        self.restore()
        f.fake.snapshots['engineer']['history_truncated'] = True
        try:
            self.assert_refuses_without_writes()
        finally:
            f.fake.snapshots['engineer']['history_truncated'] = False
        audit = self.authorize()
        rows = f.fake.snapshots['engineer']['messages']
        restored = self.turn_rows()[-1]
        rows.remove(restored)
        try:
            self.assert_refuses_without_writes()
        finally:
            rows.append(restored)
        self.assertEqual(self.request()['semantic_outcome_sha256'], audit['outcome_sha256'])

    def test_changed_native_evidence_or_owner_refuses_and_preserves_evidence(self):
        f = self.fixture
        self.restore()
        self.authorize()
        base = [copy.deepcopy(row) for row in self.events]
        workspace = f.native_row_workspace()
        stamp = self.stamp_for(self.request(), 0.9)
        variants = [
            {'type': 'user', 'uuid': 'later-human', 'sessionId': f.native_session, 'timestamp': stamp,
             'cwd': workspace, 'origin': {'kind': 'human'},
             'message': {'role': 'user', 'content': 'A later instruction.'}},
            {'type': 'assistant', 'uuid': 'later-substantive', 'sessionId': f.native_session, 'timestamp': stamp,
             'cwd': workspace,
             'message': {'role': 'assistant', 'model': 'synthetic-model',
                         'content': [{'type': 'text', 'text': 'Still working.'}]}},
            {'type': 'assistant', 'uuid': 'later-refusal', 'sessionId': f.native_session, 'timestamp': stamp,
             'cwd': workspace,
             'message': {'role': 'assistant', 'model': 'synthetic-model', 'id': 'refusal-message',
                         'stop_reason': 'refusal', 'content': []}},
            {**base[1], 'uuid': 'second-error', 'message': {**base[1]['message']}},
        ]
        for extra in variants:
            with self.subTest(uuid=extra['uuid']):
                self.write_transcript(base + [extra])
                try:
                    self.assert_refuses_without_writes()
                finally:
                    self.write_transcript(base)
        quota = {**base[1], 'apiErrorStatus': 429}
        self.write_transcript([base[0], quota])
        try:
            self.assert_refuses_without_writes()
        finally:
            self.write_transcript(base)
        owner = copy.deepcopy(f.owner)
        f.owner['provider_conversation_id'] = 'foreign-native'
        try:
            self.assert_refuses_without_writes()
        finally:
            f.owner.clear()
            f.owner.update(owner)

    def test_changed_restoration_blocks_refresh_and_history_without_writes(self):
        f = self.fixture
        self.restore()
        audit = self.authorize()
        self.renew(audit)
        restored = self.turn_rows()[-1]
        restored['text'] = 'Changed restoration text.'
        posts = self.posts()
        state_before = copy.deepcopy(f.state())
        outcomes_before = self.files('outcomes')
        receipts_before = self.files('receipts')
        with self.assertRaises(ao.RoomError):
            f.do_refresh()
        with self.assertRaises(ao.RoomError):
            transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                       f.fake.conversation('engineer'))
        self.assertEqual(f.state(), state_before)
        self.assertEqual(self.files('outcomes'), outcomes_before)
        self.assertEqual(self.files('receipts'), receipts_before)
        self.assertFalse((f.directory() / refresh.BASE).exists())
        self.assert_no_posts(posts)

    def test_changed_restoration_identity_metadata_refuses_refresh_and_history(self):
        f = self.fixture
        self.restore()
        audit = self.authorize()
        self.renew(audit)
        restored = self.turn_rows()[-1]
        for field, value in (('sequence', 9999), ('createdAt', '2026-09-29T23:55:53Z'),
                             ('revision', 2), ('editAvailable', True)):
            with self.subTest(field=field):
                original = restored.get(field)
                restored[field] = value
                try:
                    posts = self.posts()
                    state_before = copy.deepcopy(f.state())
                    outcomes_before = self.files('outcomes')
                    receipts_before = self.files('receipts')
                    with self.assertRaises(ao.RoomError):
                        f.do_refresh()
                    with self.assertRaises(ao.RoomError):
                        transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                                   f.fake.conversation('engineer'))
                    self.assertEqual(f.state(), state_before)
                    self.assertEqual(self.files('outcomes'), outcomes_before)
                    self.assertEqual(self.files('receipts'), receipts_before)
                    self.assertFalse((f.directory() / refresh.BASE).exists())
                    self.assert_no_posts(posts)
                finally:
                    if original is None:
                        restored.pop(field, None)
                    else:
                        restored[field] = original

    def test_public_model_audit_refuses_removed_restoration_without_writes(self):
        f = self.fixture
        restored = self.restore()
        audit = self.authorize()
        self.renew(audit)
        f.fake.snapshots['engineer']['messages'].remove(restored)
        self.prepare_model_audit()
        state_before = copy.deepcopy(f.state())
        outcomes_before = self.files('outcomes')
        receipts_before = self.files('receipts')
        posts = self.posts()
        result = self.model_audit()
        self.assertFalse(result['eligible'])
        self.assertIn('restored compaction', result['reason'].lower())
        self.assertEqual(f.state(), state_before)
        self.assertEqual(self.files('outcomes'), outcomes_before)
        self.assertEqual(self.files('receipts'), receipts_before)
        self.assert_no_posts(posts)
        with self.assertRaises(ao.RoomError):
            transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                       f.fake.conversation('engineer'))

    def test_public_model_audit_refuses_corrupt_or_missing_retained_observation_without_writes(self):
        f = self.fixture
        self.restore()
        audit = self.authorize()
        self.renew(audit)
        request = self.request()
        outcome_path = f.directory() / request['semantic_outcome']
        raw = outcome_path.read_bytes()
        self.prepare_model_audit()
        for label, mutate in (('corrupt', lambda: outcome_path.write_text('{}')),
                              ('missing', lambda: outcome_path.unlink())):
            with self.subTest(label=label):
                mutate()
                try:
                    outcomes_after_mutation = self.files('outcomes')
                    state_before = copy.deepcopy(f.state())
                    receipts_before = self.files('receipts')
                    posts = self.posts()
                    result = self.model_audit()
                    self.assertFalse(result['eligible'])
                    self.assertEqual(self.files('outcomes'), outcomes_after_mutation)
                    self.assertEqual(f.state(), state_before)
                    self.assertEqual(self.files('receipts'), receipts_before)
                    self.assert_no_posts(posts)
                finally:
                    outcome_path.write_bytes(raw)

    def test_prior_outcome_tamper_or_missing_refuses_audit_without_writes(self):
        f = self.fixture
        self.restore()
        audit = self.authorize()
        self.renew(audit)
        record = self.record()
        prior_sha = record['restored_compaction_observation']['prior_outcome_sha256']
        prior_path = f.directory() / 'outcomes' / self.request_id / (prior_sha + '.json')
        raw = prior_path.read_bytes()
        self.prepare_model_audit()

        def tamper():
            # A canonical parsed-object digest ignores added whitespace, so the real corruption
            # changes a parsed native proof identity field without re-binding the evidence digest.
            value = json.loads(raw)
            value['native']['compaction_failure']['assistant_uuid'] = 'changed-native-identity'
            self.assertNotEqual(ao.digest(value), prior_sha)
            prior_path.write_text(json.dumps(value))

        for label, mutate in (('tamper', tamper), ('missing', prior_path.unlink)):
            with self.subTest(label=label):
                mutate()
                try:
                    outcomes_after_mutation = self.files('outcomes')
                    state_before = copy.deepcopy(f.state())
                    receipts_before = self.files('receipts')
                    posts = self.posts()
                    result = self.model_audit()
                    self.assertFalse(result['eligible'])
                    self.assertEqual(self.files('outcomes'), outcomes_after_mutation)
                    self.assertEqual(f.state(), state_before)
                    self.assertEqual(self.files('receipts'), receipts_before)
                    self.assert_no_posts(posts)
                finally:
                    prior_path.write_bytes(raw)

    def supported_successor(self):
        """Run the exact supported recovery through one legitimate successor and its sync."""
        f = self.fixture
        restored = self.restore()
        audit = self.authorize()
        record = self.record()
        self.assertIn('restored_compaction_observation', record)
        self.assertEqual(record['restored_compaction_observation']['added_message'], restored)
        self.renew(audit)
        posts = self.posts()
        f.do_refresh()
        self.assert_no_posts(posts)
        f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
        self.assertEqual(self.posts(), posts + 1)
        successor = f.state()['requests']['resume']
        workspace = f.native_row_workspace()
        model = (successor.get('engineering_resolution') or {}).get('expected_model') or successor['model']
        self.write_transcript(list(self.events) + [
            {'type': 'user', 'uuid': 'resume-caller', 'sessionId': f.native_session,
             'timestamp': self.stamp_for(successor, 0.001), 'cwd': workspace, 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': successor['text']}},
            {'type': 'assistant', 'uuid': 'resume-reply', 'sessionId': f.native_session,
             'timestamp': self.stamp_for(successor, 0.002), 'cwd': workspace, 'isSidechain': False,
             'message': {'role': 'assistant', 'model': model, 'id': 'resume-m0',
                         'content': [{'type': 'text', 'text': json.dumps(f.report())}],
                         'stop_reason': 'end_turn'}}])
        (f.repo / 'feature.txt').write_text('implemented\n')
        f.fake.finish('engineer', json.dumps(f.report()))
        f.service.ao_room_sync(f.room)
        self.assertEqual(f.state()['requests']['resume']['state'], 'completed')
        return successor

    def test_historical_after_successor_requires_the_selected_current_outcome_pointer(self):
        """A consumed settled turn keeps its proof, but its selected current pointer stays required.

        The release-named settlement record only detects the retained restoration obligation. With the
        restored AO rows intact and a legitimately grown native transcript, the same historical proof
        stays attributable through the request's own selected current outcome; corrupting or losing
        that pointer refuses with no writes and no POST.
        """
        f = self.fixture
        self.supported_successor()
        record = self.record()
        self.assertNotEqual(ao.digest(self.transcript.read_bytes()), record['native']['source_sha256'])
        history = transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                             f.fake.conversation('engineer'))
        owned = [item['request_id'] for item in history['owned_turns']]
        self.assertIn(self.request_id, owned)
        self.assertIn('resume', owned)

        request = self.request()
        pointer = {key: request.get(key) for key in ('semantic_outcome', 'semantic_outcome_sha256')}
        release = ao.read(f.directory() / request['outcome_resume'])
        self.assertEqual(release['outcome_sha256'], pointer['semantic_outcome_sha256'])
        self.assertIn('restored_compaction_observation',
                      ao.read(f.directory() / pointer['semantic_outcome']))
        outcomes_before = self.files('outcomes')
        receipts_before = self.files('receipts')

        def corrupt_sha(row):
            row['semantic_outcome_sha256'] = 'f' * 64

        def foreign_path(row):
            row['semantic_outcome'] = ('outcomes/foreign-request/' + row['semantic_outcome_sha256']
                                       + '.json')

        def lose_pointer(row):
            row.pop('semantic_outcome', None)
            row.pop('semantic_outcome_sha256', None)

        def rewrite(mutate):
            state = f.state()
            mutate(state['requests'][self.request_id])
            ao.atomic(f.directory() / 'state.json', state)

        def restore_pointer():
            state = f.state()
            state['requests'][self.request_id].update(pointer)
            ao.atomic(f.directory() / 'state.json', state)

        for label, mutate in (('corrupt_sha', corrupt_sha), ('foreign_path', foreign_path),
                              ('pointer_lost', lose_pointer)):
            with self.subTest(label=label):
                rewrite(mutate)
                try:
                    posts = self.posts()
                    with self.assertRaises(ao.RoomError):
                        transition._native_history(f.directory(), f.state(),
                                                   f.state()['bindings']['engineer'],
                                                   f.fake.conversation('engineer'))
                    self.assertEqual(self.files('outcomes'), outcomes_before)
                    self.assertEqual(self.files('receipts'), receipts_before)
                    self.assert_no_posts(posts)
                finally:
                    restore_pointer()
        restored = transition._native_history(f.directory(), f.state(), f.state()['bindings']['engineer'],
                                              f.fake.conversation('engineer'))
        self.assertIn(self.request_id, [item['request_id'] for item in restored['owned_turns']])

    def test_missing_registered_native_source_cannot_replace_retained_restoration_evidence(self):
        """An absent current native source never rewrites the retained outcome or its pointer.

        Routine sync separately records its own semantic_observation_error instead of preserving an
        unchanged state; the retained outcome, selected pointer, receipts and release stay
        byte-identical, no send is authorized and no POST occurs. Restoring the exact source lets a
        fresh explicit audit succeed against the same retained evidence.
        """
        f = self.fixture
        self.restore()
        audit = self.authorize()
        self.renew(audit)
        request = self.request()
        pointer = {key: request.get(key) for key in ('semantic_outcome', 'semantic_outcome_sha256')}
        outcomes_before = self.files('outcomes')
        receipts_before = self.files('receipts')
        source = copy.deepcopy(f.state()['native_outcome_source'])

        def set_source(value):
            state = f.state()
            if value is None:
                state.pop('native_outcome_source', None)
            else:
                state['native_outcome_source'] = copy.deepcopy(value)
            ao.atomic(f.directory() / 'state.json', state)

        set_source(None)
        try:
            posts = self.posts()
            with self.assertRaises(ao.RoomError):
                self.audit()
            self.assert_no_posts(posts)
            self.assertEqual(self.files('outcomes'), outcomes_before)
            self.assertEqual(self.files('receipts'), receipts_before)
            held = self.request()
            self.assertEqual(held.get('semantic_outcome'), pointer['semantic_outcome'])
            self.assertEqual(held.get('semantic_outcome_sha256'), pointer['semantic_outcome_sha256'])

            f.service.ao_room_sync(f.room)
            synced = self.request()
            self.assertIn('semantic_observation_error', synced)
            self.assertEqual(synced.get('semantic_outcome'), pointer['semantic_outcome'])
            self.assertEqual(synced.get('semantic_outcome_sha256'), pointer['semantic_outcome_sha256'])
            self.assertEqual(self.files('outcomes'), outcomes_before)
            self.assertEqual(self.files('receipts'), receipts_before)
            self.assert_no_posts(posts)

            with self.assertRaises(ao.RoomError):
                f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume', purpose='correction')
            self.assert_no_posts(posts)
        finally:
            set_source(source)

        fresh = self.authorize()
        self.assertEqual(fresh['outcome_sha256'], pointer['semantic_outcome_sha256'])
        self.assertIn('restored_compaction_observation',
                      ao.read(f.directory() / pointer['semantic_outcome']))
        self.assertEqual(self.files('outcomes'), outcomes_before)

    # --- compound selected-current-proof loss ---------------------------------------
    def fresh_compound_fixture(self):
        """One fresh supported restoration fixture per independent public entrypoint.

        The real fixture is instantiated directly, exactly as the adoption probe did, so public
        audit, resume, gate and send each start from their own untouched valid
        restore/authorize/renew/refresh state. The outer cleanup also tears the fixture down when a
        check fails before its explicit teardown.
        """
        helper = RestoredCompactionTests('runTest')
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        return helper

    def compound_state(self, variant, live_state):
        """Valid restoration, then the exact compound state with every retained byte preserved.

        The live restored row is removed back to the saved receipt rows, the live turn status becomes
        ``live_state``, and the request's selected current outcome pointer is either removed entirely
        (missing) or made non-owned while its real file and SHA bytes stay (malformed). Receipts,
        outcomes, the release, the native source and the candidate stay byte-identical.
        """
        f = self.fixture
        restored = self.restore()
        audit = self.authorize()
        self.renew(audit)
        result = f.do_refresh()
        self.assertFalse(result['model_dispatch'])
        request = self.request()
        self.assertEqual(request['state'], 'settled_failure')
        record = self.record()
        self.assertIn('restored_compaction_observation', record)
        self.assertEqual(record['restored_compaction_observation']['added_message'], restored)
        pointer = {key: request.get(key) for key in ('semantic_outcome', 'semantic_outcome_sha256')}
        release = ao.read(f.directory() / request['outcome_resume'])
        self.assertEqual(release['outcome_sha256'], pointer['semantic_outcome_sha256'])
        self.assertIn('restored_compaction_observation', ao.read(
            f.directory() / ('outcomes/' + self.request_id + '/' + release['outcome_sha256'] + '.json')))
        saved_state = f.state()
        row = saved_state['requests'][self.request_id]
        if variant == 'missing':
            row.pop('semantic_outcome', None)
            row.pop('semantic_outcome_sha256', None)
        else:
            self.assertEqual(variant, 'malformed')
            # Keep the real selected file and SHA bytes; only the state path becomes non-owned.
            row['semantic_outcome'] = ('outcomes/foreign-request/' + pointer['semantic_outcome_sha256']
                                       + '.json')
        ao.atomic(f.directory() / 'state.json', saved_state)
        snapshot = f.fake.snapshots['engineer']
        self.assertIn(restored, snapshot['messages'])
        snapshot['messages'].remove(restored)
        turns = [turn for turn in snapshot['turns'] if turn.get('id') == request['turn_id']]
        self.assertEqual(len(turns), 1)
        turns[0]['state'] = live_state
        self.assertEqual(self.turn_rows(), outcomes.receipt(f.directory(), self.request())['messages'])
        return {'audit': audit, 'pointer': pointer, 'record': record, 'release': release}

    def compound_operation(self, name, audit):
        """The exact public entrypoints the adoption probe exercised, one per independent check."""
        f = self.fixture
        if name == 'public_audit':
            return self.audit
        if name == 'public_resume':
            return lambda: self.renew(audit)
        if name == 'gate':
            return lambda: outcomes.gate(f.service, f.directory(), f.state(), 'engineer', 'resume',
                                         f.fake.conversation('engineer'))
        if name == 'public_send':
            return lambda: f.service.ao_room_send(f.room, 'engineer', 'Continue.', 'resume',
                                                  purpose='correction')
        raise AssertionError('Unexpected compound operation')

    def assert_refuses_before_any_write(self, operation):
        """One entrypoint refuses with every room file byte-identical, no native change and no POST."""
        f = self.fixture
        native = (self.transcript.read_bytes(), f.database.read_bytes())

        def snapshot():
            return {str(path.relative_to(f.directory())): path.read_bytes()
                    for path in sorted(f.directory().rglob('*')) if path.is_file()}

        before = snapshot()
        posts = self.posts()
        with self.assertRaises(ao.RoomError):
            operation()
        self.assertEqual(snapshot(), before)
        self.assertEqual((self.transcript.read_bytes(), f.database.read_bytes()), native)
        self.assert_no_posts(posts)

    def assert_retained_observation_intact(self, path):
        """The retained restored outcome stays readable at its real selected path."""
        self.assertIn('restored_compaction_observation', ao.read(self.fixture.directory() / path))

    def test_compound_pointer_loss_refuses_public_operations_before_writes(self):
        """Missing-both and malformed selected pointers refuse before any write on every public entrypoint.

        Each case starts from its own fresh valid restore -> explicit audit -> renewal -> routing
        refresh fixture and keeps every retained byte. The live restored row is reverted to the saved
        receipt rows, so row equality cannot hide the obligation; the live turn status is covered as
        failed and completed to show the obligation does not depend on it. Public audit, resume, gate
        and send each refuse with no outcome, pointer, state or POST write.
        """
        for live_state in ('failed', 'completed'):
            for variant in ('missing', 'malformed'):
                for operation in ('public_audit', 'public_resume', 'gate', 'public_send'):
                    with self.subTest(live_state=live_state, variant=variant, operation=operation):
                        helper = self.fresh_compound_fixture()
                        state = helper.compound_state(variant, live_state)
                        helper.assert_refuses_before_any_write(
                            helper.compound_operation(operation, state['audit']))
                        helper.assert_retained_observation_intact(state['pointer']['semantic_outcome'])
                        helper.doCleanups()

    def test_compound_pointer_loss_cannot_start_the_audit_renew_gate_send_chain(self):
        """The former audit -> renew -> gate -> send exploit never starts without an eligible audit.

        Only the public audit's own returned evidence may feed that chain, and no digest is fabricated
        here. Because the compound public audit refuses before writing anything, the request keeps its
        retained evidence, the retained real audit digest renews nothing, and the gate and public send
        keep the same refusal, the same untouched files and the same POST count.
        """
        for live_state in ('failed', 'completed'):
            for variant in ('missing', 'malformed'):
                with self.subTest(live_state=live_state, variant=variant):
                    helper = self.fresh_compound_fixture()
                    state = helper.compound_state(variant, live_state)
                    helper.assert_refuses_before_any_write(helper.audit)
                    helper.assert_refuses_before_any_write(lambda: helper.renew(state['audit']))
                    helper.assert_refuses_before_any_write(lambda: outcomes.gate(
                        helper.fixture.service, helper.fixture.directory(), helper.fixture.state(),
                        'engineer', 'resume', helper.fixture.fake.conversation('engineer')))
                    helper.assert_refuses_before_any_write(lambda: helper.fixture.service.ao_room_send(
                        helper.fixture.room, 'engineer', 'Continue.', 'resume', purpose='correction'))
                    helper.assert_retained_observation_intact(state['pointer']['semantic_outcome'])
                    helper.doCleanups()

    def test_explicit_audit_requires_positive_complete_history_at_internal_boundary(self):
        """Narrow boundary check for the restoration lane.

        The real ao_history reader always produces a Boolean; this test exercises the internal
        contract directly with None and a missing key, which the restoration lane must positively
        reject rather than treat as complete.
        """
        f = self.fixture
        self.restore()
        snapshot = f.fake.snapshots['engineer']
        original = snapshot.get('history_truncated', 'absent')
        for label, value in (('none', None), ('missing', 'absent')):
            with self.subTest(label=label):
                if value == 'absent':
                    snapshot.pop('history_truncated', None)
                else:
                    snapshot['history_truncated'] = value
                try:
                    with self.assertRaisesRegex(ao.RoomError, 'positively complete native history'):
                        self.audit()
                finally:
                    if original == 'absent':
                        snapshot.pop('history_truncated', None)
                    else:
                        snapshot['history_truncated'] = original


class RestoredMetadataAbsenceTests(unittest.TestCase):
    """Direct type edge coverage for the restored-row contradictory-metadata predicate."""

    def test_exact_absent_values_only_without_zero_false_conflation(self):
        predicate = outcomes._restored_metadata_absent
        for value in (None, False, '', [], {}):
            with self.subTest(absent=value):
                self.assertTrue(predicate(value))
        for value in (0, 0.0, -0.0, 1, -1, True, '0', 'false', 'False', [0], ['x'], {'x': 0}):
            with self.subTest(malformed=value):
                self.assertFalse(predicate(value))


if __name__ == '__main__':
    unittest.main()
