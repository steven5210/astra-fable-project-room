"""Truthful provenance and stage evidence in the read-only progress view.

Both objects are additive controller facts. provenance says who authored the plan and last_text and how they
relate to the selected request; stages reports the saved engineering, verification and acceptance records,
each checked only against its recorded digest. Every view in this module is also proven to write nothing under
the fixture root (the room included), to keep exact key sets and to carry the fixed saved-identities-only
currentness disclosure.
"""

import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import ao_native_outcome
import ao_progress
import ao_project_room as ao
import project_room
import project_room_mcp
from test_ao_deliverables import LEAK, DeliverablesFixture

PROVENANCE_KEYS = {'request_selection', 'selected_request_role', 'selected_request_is_engineer_turn',
                   'selected_request_created_at', 'latest_engineer_request', 'selected_is_latest_engineer_request',
                   'plan', 'turn_anchor_reason', 'last_text', 'meaning', 'truncated'}
PLAN_KEYS = {'author', 'source', 'updated_at', 'scope', 'refreshable_by_selected_request', 'activity'}
LATEST_KEYS = {'request_id', 'purpose', 'state', 'created_at', 'semantic_status'}
STAGES_KEYS = {'engineering', 'verification', 'acceptance', 'currentness', 'meaning', 'truncated'}
ENGINEERING_KEYS = {'status', 'request_id', 'candidate_sha256', 'report_sha256', 'selected_request_has_record',
                    'digest_valid'}
VERIFICATION_KEYS = {'status', 'recorded_status', 'request_id', 'candidate_sha256', 'spec_sha256',
                     'spec_record_sha256', 'gates', 'identity_match', 'digest_valid'}
ACCEPTANCE_KEYS = {'status', 'recorded_status', 'request_id', 'candidate_sha256', 'spec_revision', 'spec_sha256',
                   'identity_match'}
CURRENTNESS = {'checked': False, 'worktree': 'unknown', 'basis': 'saved_identities_only'}
MISSING_VERIFICATION = {'status': 'missing', 'recorded_status': None, 'request_id': None, 'candidate_sha256': None,
                        'spec_sha256': None, 'spec_record_sha256': None, 'gates': None,
                        'identity_match': 'unknown', 'digest_valid': None}
MISSING_ACCEPTANCE = {'status': 'missing', 'recorded_status': None, 'request_id': None, 'candidate_sha256': None,
                      'spec_revision': None, 'spec_sha256': None, 'identity_match': 'unknown'}
CLOSED = {'request_selection': {'latest', 'explicit'}, 'selected_request_role': {'engineer', 'reviewer', 'unknown'},
          'turn_anchor_reason': {None, 'selected_request_not_engineer_turn', 'anchor_not_found',
                                 'source_unavailable'}}


class ProvenanceFixture(DeliverablesFixture):
    """impl-1 is sent and running; helpers finish engineering, review it and observe the view."""

    def write_transcript(self):
        self.transcript.write_text(''.join(json.dumps(event) + '\n' for event in self.native_events))

    def fingerprint(self):
        """Every path under the fixture root (room, controller home, candidate and transcript) with its
        mode, size, mtime and content digest."""
        root, found = self.root, {}
        for folder, directories, files in os.walk(root):
            for name in directories + files:
                path = Path(folder) / name
                info = path.lstat()
                content = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() \
                    else None
                found[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
        return found

    def observe(self, state=None, **kwargs):
        """One view that wrote nothing under the fixture root, with exact key sets and the fixed currentness."""
        self.write_transcript()
        before = self.fingerprint()
        view = ao_progress.inspect(self.directory(), self.state() if state is None else state, **kwargs)
        self.assertEqual(self.fingerprint(), before)
        json.dumps(view)
        json.dumps([view['provenance'], view['stages']], allow_nan=False)  # strict JSON: finite numbers only
        self.assert_shape(view)
        return view

    def assert_shape(self, view):
        provenance, stages = view['provenance'], view['stages']
        self.assertEqual(set(provenance), PROVENANCE_KEYS)
        self.assertEqual(set(provenance['plan']), PLAN_KEYS)
        self.assertEqual(set(provenance['last_text']), {'author', 'scope'})
        for key, allowed in CLOSED.items():
            self.assertIn(provenance[key], allowed)
        self.assertEqual((provenance['plan']['author'], provenance['last_text']['author']), ('engineer', 'engineer'))
        self.assertIn(provenance['plan']['source'], {'registered_engineer_native_source', 'unavailable'})
        self.assertIn(provenance['plan']['scope'], {'turn', 'window', None})
        self.assertIn(provenance['plan']['activity'], {'observed', 'unknown'})
        self.assertIn(provenance['last_text']['scope'], {'turn', 'window', None})
        self.assertEqual(provenance['meaning'], ao_progress.PROVENANCE_MEANING)
        latest = provenance['latest_engineer_request']
        if latest is not None:
            self.assertEqual(set(latest), LATEST_KEYS)
            if latest['semantic_status'] is not None:
                self.assertEqual(set(latest['semantic_status']), {'kind', 'hold', 'reason'})
        self.assertEqual(set(stages), STAGES_KEYS)
        self.assertEqual(set(stages['engineering']), ENGINEERING_KEYS)
        self.assertEqual(set(stages['verification']), VERIFICATION_KEYS)
        self.assertEqual(set(stages['acceptance']), ACCEPTANCE_KEYS)
        self.assertIn(stages['engineering']['status'], {'captured', 'error', 'missing'})
        self.assertIn(stages['verification']['status'], {'missing', 'pending', 'passed', 'failed', 'error'})
        self.assertIn(stages['acceptance']['status'],
                      {'missing', 'pending', 'approved', 'rejected', 'uncertain', 'error'})
        for stage in ('verification', 'acceptance'):
            self.assertIn(stages[stage]['identity_match'], {'matches', 'mismatched', 'unknown'})
        self.assertEqual(stages['currentness'], CURRENTNESS)
        self.assertEqual(stages['meaning'], ao_progress.STAGES_MEANING)
        for flag in (provenance['truncated'], stages['truncated']):
            self.assertIsInstance(flag, bool)
        for value in (provenance, stages):
            # No fixture path, native session id or transcript text is ever carried by either object.
            encoded = json.dumps(value)
            for private in (str(self.root), self.NATIVE, 'Perform the exact authorized purpose.'):
                self.assertNotIn(private, encoded)

    def plan_rows(self):
        """The implementation turn's task rows: a superseded unit and its in-progress successor."""
        self.create('1', 'EXIT-4: first attempt', metadata={'req': 'R1'})
        self.update('1', status='in_progress')
        self.create('2', 'EXIT-5: residual fix', metadata={'req': 'R1', 'from': 'EXIT-4'})
        self.update('1', status='pending', metadata={'superseded_by': '2'})
        self.update('2', status='in_progress')
        self.say('Implementation evidence recorded.')

    def finish_engineering(self, request_id='impl-1', content='implemented\n'):
        self.write_transcript()
        (self.repo / 'feature.txt').write_text(content)
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        return self.state()['requests'][request_id]

    def correct(self, request_id='fix-1', content='implemented\n'):
        self.send('correction', request_id)
        return self.finish_engineering(request_id, content)

    def approve(self):
        self.review()
        return self.service.ao_room_accept(self.room, 'acceptance_review')

    def current_spec(self, state):
        return ao.read(self.directory() / state['spec'])


class ProvenanceTests(ProvenanceFixture):
    def test_latest_reviewer_request_names_the_engineer_plan_and_matching_stages(self):
        self.plan_rows()
        self.finish_engineering()
        self.approve()
        state = self.state()
        view = self.observe()
        provenance, stages = view['provenance'], view['stages']
        # Every existing field keeps its meaning: the newest request is the reviewer's acceptance review,
        # while the plan and last_text are still the engineer's implementation-turn rows.
        self.assertEqual(view['request']['request_id'], 'acceptance_review')
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(view['plan']['counts'], {'pending': 1, 'in_progress': 1, 'completed': 0})
        self.assertEqual(view['deliverables']['closure']['superseded'], 1)
        self.assertEqual(view['last_text'], 'Implementation evidence recorded.')
        implementation = state['requests']['impl-1']
        semantic = implementation.get('semantic_status')
        self.assertEqual(provenance['request_selection'], 'latest')
        self.assertEqual(provenance['selected_request_role'], 'reviewer')
        self.assertIs(provenance['selected_request_is_engineer_turn'], False)
        self.assertEqual(provenance['selected_request_created_at'],
                         state['requests']['acceptance_review']['created_at'])
        self.assertEqual(provenance['latest_engineer_request'], {
            'request_id': 'impl-1', 'purpose': 'implementation', 'state': 'completed',
            'created_at': implementation['created_at'],
            'semantic_status': ({key: semantic.get(key) for key in ('kind', 'hold', 'reason')}
                                if isinstance(semantic, dict) else None)})
        self.assertIs(provenance['selected_is_latest_engineer_request'], False)
        self.assertIsNotNone(view['plan']['updated_at'])
        self.assertEqual(provenance['plan'], {'author': 'engineer', 'source': 'registered_engineer_native_source',
                                              'updated_at': view['plan']['updated_at'], 'scope': 'window',
                                              'refreshable_by_selected_request': False, 'activity': 'observed'})
        self.assertEqual(provenance['turn_anchor_reason'], 'selected_request_not_engineer_turn')
        self.assertEqual(provenance['last_text'], {'author': 'engineer', 'scope': 'window'})
        self.assertIs(provenance['truncated'], False)
        record = ao.read(self.directory() / implementation['engineering_record'])
        spec = self.current_spec(state)
        candidate = implementation['result_candidate_sha256']
        self.assertEqual(stages['engineering'], {'status': 'captured', 'request_id': 'impl-1',
                                                 'candidate_sha256': candidate,
                                                 'report_sha256': record['report_sha256'],
                                                 'selected_request_has_record': False, 'digest_valid': True})
        self.assertEqual(stages['verification'], {'status': 'passed', 'recorded_status': 'passed', 'request_id': None,
                                                  'candidate_sha256': candidate, 'spec_sha256': spec['sha256'],
                                                  'spec_record_sha256': state['spec_record_sha256'],
                                                  'gates': len(self.gates), 'identity_match': 'matches',
                                                  'digest_valid': True})
        self.assertEqual(stages['acceptance'], {'status': 'approved', 'recorded_status': 'completed',
                                                'request_id': 'acceptance_review', 'candidate_sha256': candidate,
                                                'spec_revision': 1, 'spec_sha256': spec['sha256'],
                                                'identity_match': 'matches'})
        self.assertEqual(state['acceptances'][-1]['candidate_sha256'], candidate)
        self.assertIs(stages['truncated'], False)
        # The service tool returns both objects unchanged.
        served = self.service.ao_room_progress(self.room)
        self.assertEqual((served['provenance'], served['stages']), (provenance, stages))
        # The tool description names both objects and their saved-identities-only basis, without a path.
        description = ao.TOOL_SCHEMAS['ao_room_progress'][0]
        for named in ('provenance', 'stages', 'recorded digests', 'never the live worktree'):
            self.assertIn(named, description)
        self.assertNotIn('/', description.replace('AO/network/model', ''))

    def test_explicit_implementation_request_scopes_the_plan_to_its_own_turn(self):
        self.plan_rows()
        live = self.observe()
        # While the engineer's own turn is recorded live, that turn is the only one that can refresh the plan.
        self.assertEqual(live['request']['request_id'], 'impl-1')
        self.assertEqual(live['provenance']['selected_request_role'], 'engineer')
        self.assertIs(live['provenance']['selected_is_latest_engineer_request'], True)
        self.assertIs(live['provenance']['plan']['refreshable_by_selected_request'], True)
        self.assertEqual(live['provenance']['plan']['scope'], 'turn')
        self.assertEqual(live['stages']['engineering']['status'], 'missing')  # nothing captured yet
        self.assertEqual(live['stages']['engineering']['request_id'], 'impl-1')
        self.assertEqual(live['stages']['verification'], MISSING_VERIFICATION)
        self.assertEqual(live['stages']['acceptance'], MISSING_ACCEPTANCE)
        self.finish_engineering()
        self.approve()
        latest = self.observe()
        view = self.observe(request_id='impl-1')
        provenance = view['provenance']
        self.assertEqual(view['request']['request_id'], 'impl-1')
        self.assertTrue(view['turn']['anchor_found'])
        self.assertEqual(provenance['request_selection'], 'explicit')
        self.assertEqual(provenance['selected_request_role'], 'engineer')
        self.assertIs(provenance['selected_request_is_engineer_turn'], True)
        self.assertIs(provenance['selected_is_latest_engineer_request'], True)
        self.assertEqual(provenance['latest_engineer_request'], latest['provenance']['latest_engineer_request'])
        # A completed turn can no longer refresh the plan; its last change still lies inside that turn.
        self.assertEqual(provenance['plan'], {**latest['provenance']['plan'], 'scope': 'turn'})
        self.assertIs(provenance['plan']['refreshable_by_selected_request'], False)
        self.assertIsNone(provenance['turn_anchor_reason'])
        self.assertEqual(provenance['last_text'], {'author': 'engineer', 'scope': 'turn'})
        self.assertEqual(view['last_text'], 'Implementation evidence recorded.')
        # Stages are room facts: identical for either selection apart from the selected request's own record.
        self.assertEqual(view['stages'], {**latest['stages'], 'engineering': {
            **latest['stages']['engineering'], 'selected_request_has_record': True}})

    def test_an_absent_native_source_keeps_request_provenance_and_stages(self):
        self.plan_rows()
        self.finish_engineering()
        expected = self.observe()['stages']
        state = self.state()
        del state['native_outcome_source']
        view = self.observe(state=state)
        provenance = view['provenance']
        self.assertFalse(view['source']['available'])
        self.assertEqual(view['request']['request_id'], 'impl-1')
        self.assertEqual(provenance['selected_request_role'], 'engineer')
        self.assertEqual(provenance['plan'], {'author': 'engineer', 'source': 'unavailable', 'updated_at': None,
                                              'scope': None, 'refreshable_by_selected_request': False,
                                              'activity': 'unknown'})
        self.assertEqual(provenance['turn_anchor_reason'], 'source_unavailable')
        self.assertEqual(provenance['last_text'], {'author': 'engineer', 'scope': None})
        self.assertEqual(view['stages'], expected)
        self.assertEqual(view['stages']['engineering']['status'], 'captured')
        # The failure text and its path stay out of both objects; the existing reason stays redacted.
        failure = 'unreadable transcript at ' + LEAK
        with patch.object(ao_native_outcome, 'validate_registered_source', side_effect=ao.RoomError(failure)):
            view = self.observe()
        self.assertEqual(view['source']['reason'], 'unreadable transcript at <path>')
        self.assertEqual(view['provenance']['turn_anchor_reason'], 'source_unavailable')
        for key in ('provenance', 'stages'):
            self.assertNotIn('unreadable', json.dumps(view[key]))
            self.assertNotIn('/', json.dumps(view[key]))
        self.assertNotIn(LEAK, json.dumps(view))
        self.assertEqual(view['stages'], expected)

    def test_verification_missing_failed_and_passed_are_distinguishable(self):
        draft = self.finish_engineering(content='draft\n')
        missing = self.observe()['stages']
        self.assertEqual(missing['engineering']['status'], 'captured')
        self.assertEqual(missing['verification'], MISSING_VERIFICATION)
        self.assertFalse(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        state = self.state()
        spec = self.current_spec(state)
        failed = self.observe()['stages']['verification']
        # A failed checkpoint has no recorded digest to confirm, so digest_valid stays None.
        self.assertEqual(failed, {'status': 'failed', 'recorded_status': 'failed', 'request_id': None,
                                  'candidate_sha256': draft['result_candidate_sha256'], 'spec_sha256': spec['sha256'],
                                  'spec_record_sha256': state['spec_record_sha256'], 'gates': 1,
                                  'identity_match': 'matches', 'digest_valid': None})
        corrected = self.correct()
        self.assertNotEqual(corrected['result_candidate_sha256'], draft['result_candidate_sha256'])
        stale = self.observe()['stages']
        self.assertEqual(stale['engineering']['request_id'], 'fix-1')
        self.assertEqual(stale['engineering']['candidate_sha256'], corrected['result_candidate_sha256'])
        self.assertEqual(stale['verification'], {**failed, 'identity_match': 'mismatched'})
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        passed = self.observe()['stages']['verification']
        self.assertEqual(passed, {**failed, 'status': 'passed', 'recorded_status': 'passed',
                                  'candidate_sha256': corrected['result_candidate_sha256'],
                                  'identity_match': 'matches', 'digest_valid': True})

    def test_acceptance_absent_pending_approved_and_candidate_drift(self):
        implementation = self.finish_engineering()
        self.assertEqual(self.observe()['stages']['acceptance'], MISSING_ACCEPTANCE)
        self.review()  # the reviewer approved; nothing is recorded until the acceptance itself
        state = self.state()
        spec = self.current_spec(state)
        candidate = implementation['result_candidate_sha256']
        pending = self.observe()['stages']['acceptance']
        self.assertEqual(pending, {'status': 'pending', 'recorded_status': 'completed',
                                   'request_id': 'acceptance_review', 'candidate_sha256': candidate,
                                   'spec_revision': 1, 'spec_sha256': spec['sha256'], 'identity_match': 'matches'})
        self.service.ao_room_accept(self.room, 'acceptance_review')
        approved = self.observe()['stages']['acceptance']
        self.assertEqual(approved, {**pending, 'status': 'approved'})
        corrected = self.correct(content='implemented\nfollow-up\n')
        self.assertNotEqual(corrected['result_candidate_sha256'], candidate)
        drifted = self.observe()['stages']
        self.assertEqual(drifted['engineering']['candidate_sha256'], corrected['result_candidate_sha256'])
        self.assertEqual(drifted['acceptance'], {**approved, 'identity_match': 'mismatched'})
        self.assertEqual(drifted['verification']['identity_match'], 'mismatched')
        self.assertEqual(drifted['verification']['status'], 'passed')

    def test_spec_drift_mismatches_acceptance_but_keeps_its_recorded_spec_identity(self):
        self.finish_engineering()
        self.approve()
        approved = self.observe()['stages']['acceptance']
        self.spec(revision=2)  # the same content under a newer immutable revision record
        state = self.state()
        current = self.current_spec(state)
        self.assertEqual(current['revision'], 2)
        drifted = self.observe()['stages']
        self.assertEqual(drifted['acceptance'], {**approved, 'identity_match': 'mismatched'})
        self.assertEqual(drifted['acceptance']['spec_revision'], 1)
        self.assertEqual(drifted['acceptance']['spec_sha256'], current['sha256'])  # same bytes, other record
        # Verification applies the same three-way rule; its checkpoint still names the first spec record.
        self.assertEqual(drifted['verification']['status'], 'passed')
        self.assertEqual(drifted['verification']['identity_match'], 'mismatched')
        self.assertNotEqual(drifted['verification']['spec_record_sha256'], state['spec_record_sha256'])

    def test_spec_drift_mismatches_a_passed_verification_but_keeps_its_recorded_spec_identities(self):
        implementation = self.finish_engineering()
        self.approve()  # a passed verification, an approving review and the recorded acceptance
        first = self.state()
        first_spec = self.current_spec(first)
        candidate = implementation['result_candidate_sha256']
        before = self.observe()['stages']
        self.assertEqual(before['verification'], {'status': 'passed', 'recorded_status': 'passed', 'request_id': None,
                                                  'candidate_sha256': candidate, 'spec_sha256': first_spec['sha256'],
                                                  'spec_record_sha256': first['spec_record_sha256'],
                                                  'gates': len(self.gates), 'identity_match': 'matches',
                                                  'digest_valid': True})
        self.assertEqual((before['acceptance']['status'], before['acceptance']['identity_match']),
                         ('approved', 'matches'))
        self.spec(revision=2)  # the fixture's spec helper: the same bytes under a newer immutable spec record
        state = self.state()
        self.assertNotEqual(state['spec_record_sha256'], first['spec_record_sha256'])
        self.assertEqual(self.current_spec(state)['sha256'], first_spec['sha256'])
        drifted = self.observe()['stages']
        # The passed status, its confirmed digest and the first spec's identities stay; only the match changes.
        self.assertEqual(drifted['verification'], {**before['verification'], 'identity_match': 'mismatched'})
        self.assertEqual(drifted['acceptance'], {**before['acceptance'], 'identity_match': 'mismatched'})
        self.assertEqual(drifted['engineering'], before['engineering'])
        # New spec bytes as well: the checkpoint keeps naming the first spec digest, which no longer matches.
        self.service.ao_room_spec_put(self.room, 3, 'Implement the revised test contract.', self.gates,
                                      'Astra approves this scope')
        self.assertNotEqual(self.current_spec(self.state())['sha256'], first_spec['sha256'])
        revised = self.observe()['stages']
        self.assertEqual(revised['verification'], drifted['verification'])
        self.assertEqual(revised['acceptance'], drifted['acceptance'])

    def test_verification_without_a_known_spec_identity_never_matches(self):
        implementation = self.finish_engineering()
        candidate = implementation['result_candidate_sha256']
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        passed = self.observe()['stages']['verification']
        self.assertEqual((passed['status'], passed['identity_match']), ('passed', 'matches'))
        # Without the room's current spec identity two comparisons are unknown and none differs.
        state = self.state()
        state['spec_record_sha256'] = None
        self.assertEqual(self.observe(state=state)['stages']['verification'], {**passed, 'identity_match': 'unknown'})
        # A running attempt has no checkpoint, so it records no spec identity and never matches; a different
        # candidate still mismatches.
        state = self.state()
        state['verifications'].append({'id': 'verify-running', 'state': 'running', 'candidate_sha256': candidate})
        pending = self.observe(state=state)['stages']['verification']
        self.assertEqual(pending, {**MISSING_VERIFICATION, 'status': 'pending', 'recorded_status': 'running',
                                   'candidate_sha256': candidate})
        state['verifications'][-1]['candidate_sha256'] = 'f' * 64
        self.assertEqual(self.observe(state=state)['stages']['verification'],
                         {**pending, 'candidate_sha256': 'f' * 64, 'identity_match': 'mismatched'})

    def test_review_outcomes_map_to_closed_acceptance_statuses(self):
        self.finish_engineering()
        self.assertTrue(self.service.ao_room_verify(self.room, str(self.repo))['passed'])
        self.send('acceptance_review')
        running = self.observe()['stages']['acceptance']
        self.assertEqual((running['status'], running['recorded_status'], running['identity_match']),
                         ('pending', 'submitted', 'matches'))
        request = self.state()['requests']['acceptance_review']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'rejected',
                                                 'review': 'Gate evidence is incomplete.'}))
        self.service.ao_room_sync(self.room)
        rejected = self.observe()
        self.assertEqual(rejected['stages']['acceptance'],
                         {**running, 'status': 'rejected', 'recorded_status': 'completed'})
        self.assertNotIn('incomplete', json.dumps(rejected['stages']))
        # Recorded request states map without any detail; an unknown delivery stays uncertain.
        for recorded, status in (('uncertain', 'uncertain'), ('running', 'pending'),
                                 ('settled_failure', 'error'), ('cancelled', 'error')):
            with self.subTest(recorded=recorded):
                state = self.state()
                state['requests']['acceptance_review']['state'] = recorded
                acceptance = self.observe(state=state)['stages']['acceptance']
                if status == 'error':
                    self.assertEqual(acceptance, {**MISSING_ACCEPTANCE, 'status': 'error', 'recorded_status': recorded,
                                                  'request_id': 'acceptance_review'})
                else:
                    self.assertEqual(acceptance, {**running, 'status': status, 'recorded_status': recorded})

    def test_long_ids_and_absolute_paths_are_bounded_and_redacted(self):
        self.finish_engineering()
        long_id = 'c' * 101  # a valid request identifier past the 64-character display bound
        self.send('correction', long_id)
        view = self.observe()
        self.assertEqual(view['provenance']['latest_engineer_request']['request_id'],
                         long_id[:ao_progress.MAX_ID_CHARS])
        self.assertIs(view['provenance']['truncated'], True)
        self.assertEqual(view['stages']['engineering']['request_id'], long_id[:ao_progress.MAX_ID_CHARS])
        self.assertEqual(view['stages']['engineering']['status'], 'missing')
        self.assertIs(view['stages']['truncated'], True)
        # Saved strings carrying an absolute path are redacted; a malformed digest is never shown as an identity.
        state = self.state()
        state['requests'][long_id]['semantic_status'] = {'kind': 'unknown', 'hold': True, 'reason': 'held at ' + LEAK}
        state['verifications'] = [{'id': 'verify-tampered', 'state': LEAK, 'candidate_sha256': LEAK}]
        view = self.observe(state=state, request_id='impl-1')
        self.assertNotIn(LEAK, json.dumps(view))
        self.assertEqual(view['provenance']['latest_engineer_request']['semantic_status'],
                         {'kind': 'unknown', 'hold': True, 'reason': 'held at <path>'})
        self.assertEqual(view['stages']['verification'], {**MISSING_VERIFICATION, 'status': 'error',
                                                          'recorded_status': '<path>'})

    def test_saved_records_that_fail_their_digest_report_error_without_detail(self):
        self.finish_engineering()
        self.approve()
        state = self.state()
        state['requests']['impl-1']['engineering_record_sha256'] = '0' * 64
        state['checkpoint_sha256'] = '0' * 64
        stages = self.observe(state=state)['stages']
        self.assertEqual(stages['engineering'], {'status': 'error', 'request_id': 'impl-1', 'candidate_sha256': None,
                                                 'report_sha256': None, 'selected_request_has_record': False,
                                                 'digest_valid': False})
        self.assertEqual(stages['verification'], {**MISSING_VERIFICATION, 'status': 'error',
                                                  'recorded_status': 'passed', 'digest_valid': False})
        # Without a confirmed engineering candidate the acceptance's candidate cannot be compared.
        self.assertEqual((stages['acceptance']['status'], stages['acceptance']['identity_match']),
                         ('approved', 'unknown'))
        # A recorded capture failure is an error as well, and its text never appears.
        state = self.state()
        request = state['requests']['impl-1']
        for key in ('engineering_record', 'engineering_record_sha256', 'result_candidate_sha256'):
            request.pop(key)
        request['engineering_error'] = 'capture failed at ' + LEAK
        view = self.observe(state=state, request_id='impl-1')
        self.assertEqual(view['stages']['engineering'], {'status': 'error', 'request_id': 'impl-1',
                                                         'candidate_sha256': None, 'report_sha256': None,
                                                         'selected_request_has_record': False, 'digest_valid': None})
        self.assertNotIn('capture failed', json.dumps(view))
        self.assertNotIn(LEAK, json.dumps(view))

    def test_anchor_absence_activity_and_last_text_scope(self):
        quiet = self.observe()
        # No task-tool row yet: the source was read, but plan activity is unknown and has no scope.
        self.assertEqual(quiet['provenance']['plan'], {'author': 'engineer',
                                                       'source': 'registered_engineer_native_source',
                                                       'updated_at': None, 'scope': None,
                                                       'refreshable_by_selected_request': True,
                                                       'activity': 'unknown'})
        self.assertEqual(quiet['provenance']['last_text'], {'author': 'engineer', 'scope': 'turn'})
        omitted = self.observe(max_text_chars=0)
        self.assertNotIn('last_text', omitted)
        self.assertEqual(omitted['provenance']['last_text'], {'author': 'engineer', 'scope': None})
        self.plan_rows()
        state = self.state()
        state['requests']['impl-1']['text_sha256'] = '0' * 64  # no delivered human row carries this text
        view = self.observe(state=state)
        provenance = view['provenance']
        self.assertEqual(view['turn'], {'anchor_found': False})
        self.assertEqual(provenance['turn_anchor_reason'], 'anchor_not_found')
        self.assertEqual((provenance['plan']['scope'], provenance['plan']['activity']), ('window', 'observed'))
        self.assertEqual(provenance['last_text'], {'author': 'engineer', 'scope': 'window'})

    def test_a_request_from_an_unbound_session_has_an_unknown_role(self):
        self.plan_rows()
        state = self.state()
        state['requests']['impl-1']['session_id'] = 'unbound-session'
        view = self.observe(state=state, request_id='impl-1')
        provenance = view['provenance']
        self.assertEqual(provenance['selected_request_role'], 'unknown')
        self.assertIs(provenance['selected_request_is_engineer_turn'], False)
        self.assertIs(provenance['plan']['refreshable_by_selected_request'], False)
        # Only requests sent from the bound engineer session count as engineer turns.
        self.assertEqual(provenance['latest_engineer_request']['request_id'], 'spec_review')
        self.assertIs(provenance['selected_is_latest_engineer_request'], False)
        self.assertNotIn('unbound-session', json.dumps(view['provenance']))
        # Role and anchor are independent facts: the existing text and time correlation still anchors the turn.
        self.assertTrue(view['turn']['anchor_found'])
        self.assertIsNone(provenance['turn_anchor_reason'])
        state['bindings'] = {}
        view = self.observe(state=state)
        # Without bindings the source cannot be validated; a non-engineer selection keeps its own reason.
        self.assertFalse(view['source']['available'])
        self.assertEqual(view['provenance']['plan']['source'], 'unavailable')
        self.assertEqual(view['provenance']['selected_request_role'], 'unknown')
        self.assertIsNone(view['provenance']['latest_engineer_request'])
        self.assertIs(view['provenance']['selected_is_latest_engineer_request'], False)
        self.assertEqual(view['provenance']['turn_anchor_reason'], 'selected_request_not_engineer_turn')

    def test_provenance_and_stages_round_trip_over_mcp_tools_call(self):
        self.plan_rows()
        self.finish_engineering()
        self.approve()
        # Constructing the Service creates its own registry database file; that is the constructor's
        # own side effect, not a write performed by the view, so it happens before the fingerprint.
        service = project_room.Service(self.home)
        self.write_transcript()
        before = self.fingerprint()
        with patch.object(project_room.ao_project_room, 'Service', return_value=self.service):
            response = project_room_mcp.handle(
                {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                 'params': {'name': 'ao_room_progress', 'arguments': {'room_id': self.room}}}, service)
        direct = self.service.ao_room_progress(self.room)
        self.assertEqual(self.fingerprint(), before)
        self.assertNotIn('error', response)
        self.assertFalse(response['result']['isError'])
        served = json.loads(response['result']['content'][0]['text'])
        self.assert_shape(served)
        provenance, stages = served['provenance'], served['stages']
        self.assertIn(provenance['request_selection'], {'latest', 'explicit'})
        self.assertEqual(provenance['plan']['author'], 'engineer')
        self.assertEqual(stages['currentness'], CURRENTNESS)
        self.assertIn(stages['engineering']['status'], {'captured', 'error', 'missing'})
        self.assertIn(stages['verification']['status'], {'missing', 'pending', 'passed', 'failed', 'error'})
        self.assertIn(stages['acceptance']['status'],
                      {'missing', 'pending', 'approved', 'rejected', 'uncertain', 'error'})
        self.assertEqual(json.loads(json.dumps(direct)), served)


if __name__ == '__main__':
    unittest.main()
