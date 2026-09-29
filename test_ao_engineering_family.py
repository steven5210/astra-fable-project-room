"""Latest-available selection within a family (design v3); fake AO, synthetic native evidence, no model calls.

Family epochs resolve their exact expected model only from verified native evidence of a completed
owned turn, recorded once at the next engineer dispatch boundary; exact and legacy rooms keep exact
equality and their historical native result shapes. Every "transition" is synthetic journal data
written through the module's own create-once writer; nothing sends a PATCH.
"""
import contextlib
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

import ao_engineering_model as em
import ao_model_boundaries
import ao_native_outcome
import ao_project_room as ao
import ao_quality_review
import ao_workflow
from test_ao_engineering_model import FABLE, OPUS, ModelFixture, files_under, selector

EFFORT_BASIS = 'configured max intent; effective effort not evidenced'
RESOLUTIONS = em.BASE + '/resolutions'
FAMILY_OPUS = {'kind': 'family', 'family': 'opus'}

# A fixed legacy events_outcome input. Its digest was computed with the pre-change runtime
# (ao_native_outcome.py sha256 bed39d2abdb14452fdfbe783b4180109d86247658567682addbb5192675e139d).
LEGACY_EVENTS_SHA256 = 'f579941ba480974fc4ebd04ae63fc46d168dd639485a51590e2cd57f4af62be9'
LEGACY_TEXT = 'Continue the exact legacy request.'
LEGACY_REQUEST = {'text': LEGACY_TEXT, 'text_sha256': ao.digest(LEGACY_TEXT.encode()), 'created_at': 1767225600.0,
                  'model': FABLE}
LEGACY_EVENTS = [
    {'type': 'user', 'uuid': 'caller', 'sessionId': 'native-legacy', 'timestamp': '2026-01-01T00:00:00.100000+00:00',
     'origin': {'kind': 'human'}, 'message': {'content': LEGACY_TEXT}},
    {'type': 'assistant', 'uuid': 'tool', 'sessionId': 'native-legacy', 'timestamp': '2026-01-01T00:00:00.200000+00:00',
     'message': {'model': FABLE, 'id': 'm-tool', 'stop_reason': 'tool_use'}},
    {'type': 'assistant', 'uuid': 'retry', 'sessionId': 'native-legacy', 'timestamp': '2026-01-01T00:00:00.300000+00:00',
     'isApiErrorMessage': True, 'error': 'overloaded', 'apiErrorStatus': 529, 'message': {'model': '<synthetic>'}},
    {'type': 'assistant', 'uuid': 'final', 'sessionId': 'native-legacy', 'timestamp': '2026-01-01T00:00:00.400000+00:00',
     'message': {'model': FABLE, 'id': 'm-final', 'stop_reason': 'end_turn'}},
]


def frozen(selected, configured, expected=None, resolution=None):
    return {'selector': selected, 'configured_model': configured, 'expected_model': expected,
            'resolution_sha256': resolution, 'effort_basis': EFFORT_BASIS}


def family_request(expected=None, resolution=None):
    text = 'Continue the family request.'
    return {'text': text, 'text_sha256': ao.digest(text.encode()), 'created_at': 1767225600.0, 'model': 'opus',
            'engineering_resolution': frozen(FAMILY_OPUS, 'opus', expected, resolution)}


def events_for(request, *models):
    stamp = lambda s: datetime.fromtimestamp(request['created_at'] + s, timezone.utc).isoformat()
    rows = [{'type': 'user', 'uuid': 'caller', 'sessionId': 'native', 'timestamp': stamp(0.1),
             'origin': {'kind': 'human'}, 'message': {'content': request['text']}}]
    for index, model in enumerate(models):
        rows.append({'type': 'assistant', 'uuid': 'reply-%d' % index, 'sessionId': 'native',
                     'timestamp': stamp(0.2 + index / 1000),
                     'message': {'model': model, 'id': 'm-%d' % index,
                                 'stop_reason': 'end_turn' if index == len(models) - 1 else 'tool_use'}})
    return rows


class FamilyFixture(ModelFixture):
    """A room plus synthetic native owner/transcript evidence for its engineer session."""
    NATIVE = 'native-family'
    WORKER_FLOOR = '2.1.280'
    WORKER_OPUS = 'claude-opus-5-5'
    WORKER_SONNET = 'claude-sonnet-5-5'

    def open_room(self, feature, model, claude='2.1.280'):
        if claude:
            self.install_claude('claude-' + claude.replace('.', '-'), claude + ' (Claude Code)')
        self.room = self.open_model(feature, model=model)['room_id']
        self.spec()
        # The room records its own root epoch first; its preparation independently selects the
        # qualified worker artifact for both enabled families and restores the private pointer.
        self.bind_at(em.initial(self.directory(), self.state())['configured_model'])
        self.configure_native()

    def configure_native(self):
        '''Register the bound engineer's complete disclosed synthetic owner and prepared-root transcript.

        The family fixture keeps its long-standing deepcopy ``read_owner`` contract: consumer tests
        mutate ``self.owner`` and require the retained-source conflict guard for a different
        synthetic database path. Only this fixture's owner reader is disclosed as synthetic; the
        native transcript, receipts and worker consumers still read the real prepared-root source.
        Repeated identical setup returns the registered source instead of rewriting retained
        history or stacking another owner patch.
        '''
        if getattr(self, 'native_outcome_source', None) is not None:
            return self.native_outcome_source
        state = self.state()
        binding = state['bindings']['engineer']
        workspace = self.native_row_workspace()
        config_root = self.native_config_root()
        project_dir = config_root / 'projects' / str(self.repo).replace('/', '-')
        project_dir.mkdir(parents=True, exist_ok=True)
        self.transcript = project_dir / (self.NATIVE + '.jsonl')
        self.transcript.write_text('')
        self.transcript.chmod(0o600)
        self.database = self.root / 'owner.db'
        self.owner = {'id': binding['session_id'], 'project_id': state['ao_project_id'],
                      'harness': 'claude-code', 'session_mode': 'chat', 'is_terminated': 0,
                      'activity_state': 'idle', 'workspace_path': workspace,
                      'provider_conversation_id': self.NATIVE, 'controller_generation': 'generation-1',
                      'ao_conversation_id': binding['conversation_id'], 'active_branch_id': binding['branch_id'],
                      'branch_provider_conversation_id': self.NATIVE, 'branch_session_id': binding['session_id'],
                      'strategy': 'native', 'replay_truncated': 0}
        patcher = patch('ao_native_identity.read_owner', side_effect=lambda *a: copy.deepcopy(self.owner))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events = []
        self.native_requests = set()
        self.source = {'database': str(self.database), 'transcript': str(self.transcript),
                       'session_id': binding['session_id'], 'native_session_id': self.NATIVE}
        ao_native_outcome.preflight_source(self.directory(), state, self.source['database'], self.source['transcript'])
        ao.atomic(self.directory() / 'state.json', state)
        self.native_outcome_source = self.source
        return self.source

    def write_native(self):
        self.transcript.write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def send(self, purpose, key=None):
        '''Send one family request without the inherited automatic native success row.

        ``native_turn`` is the single explicit producer of this fixture's caller and stop rows, so a
        deliberately contradicting model is never pre-empted by an inherited expected-model row.
        '''
        role = 'reviewer' if purpose == 'acceptance_review' else 'engineer'
        return self.service.ao_room_send(self.room, role, 'Perform the exact authorized purpose.', key or purpose,
                                         purpose=purpose)

    def native_turn(self, request_id, *models):
        '''The owned caller row, then one full stop row per served model (the last one ends the turn).'''
        request = self.state()['requests'][request_id]
        now = datetime.now(timezone.utc)
        stamp = lambda s: min(datetime.fromtimestamp(request['created_at'] + s, timezone.utc), now).isoformat()
        workspace = self.native_row_workspace()
        self.events.append({'type': 'user', 'uuid': request_id + '-caller', 'sessionId': self.NATIVE,
                            'timestamp': stamp(0.001), 'cwd': workspace, 'isSidechain': False,
                            'origin': {'kind': 'human'},
                            'message': {'role': 'user', 'content': request['text']}})
        for index, model in enumerate(models):
            self.events.append({'type': 'assistant', 'uuid': '%s-reply-%d' % (request_id, index),
                                'sessionId': self.NATIVE, 'timestamp': stamp(0.002 + index / 1000),
                                'cwd': workspace, 'isSidechain': False,
                                'message': {'role': 'assistant', 'model': model, 'id': '%s-m%d' % (request_id, index),
                                            'content': [{'type': 'text', 'text': 'Synthetic family turn.'}],
                                            'stop_reason': 'end_turn' if index == len(models) - 1 else 'tool_use'}})
        self.write_native()

    def turn(self, purpose, key, text, *models):
        self.send(purpose, key)
        if models:
            self.native_turn(key, *models)
        self.fake.finish('engineer', text)
        return self.service.ao_room_sync(self.room)

    def historical_send(self, purpose, key=None):
        """Record one pre-correction request exactly as this room saved it before the dispatch guard.

        The corrected freeze_request refuses a new family request in an epoch with no pre-inference
        operator qualification. Tests of the historical version 2 records therefore construct that
        saved record explicitly through the module's own original freezing rule; the guard itself is
        never disabled for any other call and no test relaxes it globally.
        """
        def call_time_freeze(*args, **kwargs):
            directory = args[0] if args else kwargs.get('directory')
            state = args[1] if len(args) > 1 else kwargs.get('state')
            epoch = em.current(directory, state)
            order = len((state or {}).get('requests') or {}) + 1
            return em._frozen(epoch, order)

        patches = [patch.object(em, 'freeze_request', side_effect=call_time_freeze)]
        if hasattr(ao, 'freeze_request'):
            patches.append(patch.object(ao, 'freeze_request', side_effect=call_time_freeze))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return self.send(purpose, key)

    def historical_turn(self, purpose, key, text, *models):
        self.historical_send(purpose, key)
        if models:
            self.native_turn(key, *models)
        self.fake.finish('engineer', text)
        return self.service.ao_room_sync(self.room)

    def historical_review(self, *models):
        return self.historical_turn('spec_review', 'spec_review', self.verdict(), *models)

    def historical_implement(self, *models, key='implementation'):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.historical_send('implementation', key)
        if models:
            self.native_turn(key, *models)
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        return self.service.ao_room_sync(self.room)

    def historical_correct(self, key, *models):
        self.historical_send('correction', key)
        self.native_turn(key, *models)
        self.fake.finish('engineer', json.dumps(self.report()))
        return self.service.ao_room_sync(self.room)

    def verdict(self):
        spec = self.service.spec(self.directory(), self.state())
        return json.dumps({'interpretation': 'Implement the supplied pure behavior.', 'findings': [],
                           'decision': 'accept', 'spec_revision': spec['revision'], 'spec_sha256': spec['sha256']})

    def review_turn(self, *models):
        return self.turn('spec_review', 'spec_review', self.verdict(), *models)

    def implement_with(self, *models, key='implementation'):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation', key)
        if models:
            self.native_turn(key, *models)
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        return self.service.ao_room_sync(self.room)

    def correct_with(self, key, *models):
        self.send('correction', key)
        self.native_turn(key, *models)
        self.fake.finish('engineer', json.dumps(self.report()))
        return self.service.ao_room_sync(self.room)

    def request(self, key):
        return self.state()['requests'][key]

    def outcome(self, key):
        return ao.read(self.directory() / self.request(key)['semantic_outcome'])

    def resolutions(self):
        return self.state().get(em.RESOLUTIONS_KEY)


class SelectionAndBindTests(FamilyFixture):
    """S1/S2: the default family selector, explicit families with floors, and exact compatibility."""

    def test_new_default_room_records_family_fable_and_freezes_unknown_expectation(self):
        self.room = self.open_model('default-family')['room_id']
        record = self.state()[em.SELECTION_KEY]
        self.assertEqual((record['selector'], record['configured_model'], record['selection']),
                         ({'kind': 'family', 'family': 'fable'}, 'fable', 'default'))
        self.assertEqual(record['qualification'], em.BUNDLED_FAMILIES['fable'])
        self.assertIn('latest available member', record['basis'])
        self.spec()
        self.bind_at()
        self.assertEqual(ao_workflow.FABLE_MODEL, 'fable')
        self.assertEqual(self.state()['bindings']['engineer']['model'], 'fable')
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('spec_review')  # a new family dispatch requires a pre-inference qualification
        self.assertNotIn('spec_review', self.state()['requests'])
        # The pre-correction record is preserved through the module's own original freezing rule.
        self.historical_send('spec_review')
        self.assertEqual(self.request('spec_review')['engineering_resolution'],
                         frozen({'kind': 'family', 'family': 'fable'}, 'fable'))
        self.assertEqual(self.request('spec_review')['model'], 'fable')
        status = self.service.ao_room_status(self.room)
        self.assertEqual(status['requests'][-1]['configured_model'], 'fable')
        summary = status['engineering_model']
        self.assertEqual(summary['resolution']['status'], 'unverified')
        self.assertIsNone(summary['resolution']['expected_model'])
        self.assertEqual(summary['effort'], {'configured': 'max', 'effective': 'not evidenced'})
        for claim in ('"available"', '"account"', '"quota"', 'adopted', 'attested'):
            self.assertNotIn(claim, json.dumps(summary))

    def floor_case(self, version, accepted):
        self.install_claude('claude-' + version.replace('.', '-'), version + ' (Claude Code)')
        self.room = self.open_model('opus-family', model='opus')['room_id']
        record = self.state()[em.SELECTION_KEY]
        self.assertEqual((record['selector'], record['configured_model']), (FAMILY_OPUS, 'opus'))
        self.assertEqual(record['qualification']['minimum_claude_code_version'], '2.1.280')
        self.spec()
        self.fake.snapshots['engineer']['settings']['model'] = 'opus'
        if accepted:
            self.prepare()
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', 'opus', 'max')
            self.assertEqual(self.state()['bindings']['engineer']['model'], 'opus')
            self.assertEqual(em.effective_binding(self.directory(), self.state())['model'], 'opus')
        else:
            # A deliberately old executable cannot carry the modern source-qualified worker
            # preparation, so the recorded historical v2 bytes keep the root compatibility boundary
            # this case exercises: the qualified worker floor never fires first.
            self.historical_preparation()
            with self.assertRaisesRegex(ao.RoomError, r'opus requires Claude Code 2\.1\.280 or newer'):
                self.service.ao_room_bind(self.room, 'engineer', 'engineer', 'opus', 'max')
            self.assertNotIn('engineer', self.state()['bindings'])

    def test_family_opus_floor_refuses_2_1_268(self):
        self.floor_case('2.1.268', False)

    def test_family_opus_floor_admits_2_1_280(self):
        self.floor_case('2.1.280', True)

    def test_family_opus_floor_admits_2_1_282(self):
        self.floor_case('2.1.282', True)

    def test_exact_opus_remains_selectable_and_sonnet_family_is_not_an_engineer_selector(self):
        self.install_claude('claude-qualified', '2.1.280 (Claude Code)')
        self.room = self.open_model('exact-opus', model=OPUS)['room_id']
        self.assertEqual(self.state()[em.SELECTION_KEY]['selector'], {'kind': 'exact', 'model': OPUS})
        self.spec()
        self.bind_at(OPUS)
        self.send('spec_review')
        self.assertEqual(self.request('spec_review')['engineering_resolution'],
                         frozen({'kind': 'exact', 'model': OPUS}, OPUS, OPUS))
        with self.assertRaisesRegex(ao.RoomError, 'Qualified engineering selectors are'):
            self.open_model('sonnet-engineer', model='sonnet')
        self.assertFalse((self.home / 'ao' / 'rooms' / self.room_id_for('sonnet-engineer')).exists())
        schema = ao.TOOL_SCHEMAS['ao_room_open'][0]
        self.assertIn('family alias', schema)
        self.assertNotIn('default claude-fable-5-1', schema)


class NativeFamilyMatchingTests(unittest.TestCase):
    """S4 events_outcome: exact members only, the frozen expectation once set, one model per turn."""

    def test_unresolved_family_request_accepts_one_exact_member(self):
        request = family_request()
        for models in ((OPUS,), ('claude-opus-5-6',), ('claude-opus-5',), (OPUS, OPUS)):
            with self.subTest(models=models):
                result = ao_native_outcome.events_outcome(events_for(request, *models), request, 'native')
                self.assertEqual(result['observed_models'], [models[0]])
                self.assertEqual(result['stop_reasons'], ['end_turn'])
        for models in (('claude-sonnet-5',), (OPUS, 'claude-opus-5-6'), ('opus',), ('claude-opus',),
                       ('claude-opus-latest',), ('Claude-opus-5-5',), (None,)):
            with self.subTest(models=models), self.assertRaisesRegex(ao.RoomError, 'contradicts the owned family request'):
                ao_native_outcome.events_outcome(events_for(request, *models), request, 'native')

    def test_resolved_family_request_accepts_only_its_frozen_model(self):
        request = family_request(OPUS, 'a' * 64)
        self.assertEqual(ao_native_outcome.events_outcome(events_for(request, OPUS), request, 'native')['observed_models'],
                         [OPUS])
        for model in ('claude-opus-5', 'claude-opus-5-6', 'claude-sonnet-5', 'opus'):
            with self.subTest(model=model), self.assertRaisesRegex(ao.RoomError, 'contradicts the owned family request'):
                ao_native_outcome.events_outcome(events_for(request, model), request, 'native')

    def test_no_stop_rows_record_an_empty_observation_and_exact_requests_keep_their_shape(self):
        request = family_request()
        rows = events_for(request) + [{**LEGACY_EVENTS[2], 'sessionId': 'native'}]
        self.assertEqual(ao_native_outcome.events_outcome(rows, request, 'native')['observed_models'], [])
        self.assertEqual(ao.digest(ao_native_outcome.events_outcome(LEGACY_EVENTS, dict(LEGACY_REQUEST), 'native-legacy')),
                         LEGACY_EVENTS_SHA256)
        exact = {**LEGACY_REQUEST, 'engineering_resolution': frozen(selector(FABLE), FABLE, FABLE)}
        self.assertEqual(ao.digest(ao_native_outcome.events_outcome(LEGACY_EVENTS, exact, 'native-legacy')),
                         LEGACY_EVENTS_SHA256)


class ResolutionTests(FamilyFixture):
    """S3/S4: resolution only from verified native evidence, adopted at the next dispatch, then frozen."""

    def setUp(self):
        super().setUp()
        self.open_room('family-opus', 'opus')

    def resolve(self, model=OPUS):
        """The pre-correction saved spec review observed on `model`; nothing is adopted until the next boundary."""
        self.historical_review(model)
        self.assertEqual(self.outcome('spec_review')['native']['observed_models'], [model])
        self.assertEqual(self.request('spec_review')['engineering_resolution'], frozen(FAMILY_OPUS, 'opus'))
        self.assertIsNone(self.resolutions())
        self.assertIsNone(em.current(self.directory(), self.state())['expected_model'])

    def test_first_observed_turn_is_adopted_at_the_next_dispatch_boundary_and_frozen(self):
        self.resolve()
        before = files_under(self.directory())
        self.historical_implement(OPUS)
        pointers = self.resolutions()
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0]['path'], RESOLUTIONS + '/spec_review.json')
        value = ao.read(self.directory() / pointers[0]['path'])
        spec_review = self.request('spec_review')
        self.assertEqual(value, {'version': 1, 'room_id': self.room, 'request_id': 'spec_review',
                                 'epoch_request_id': None, 'selector': FAMILY_OPUS,
                                 'configured_model': 'opus', 'observed_model': OPUS,
                                 'outcome_record_sha256': spec_review['semantic_outcome_sha256'],
                                 'receipt_sha256': spec_review['receipt_sha256'], 'created_order': 1,
                                 'recorded_at': value['recorded_at']})
        self.assertEqual(pointers[0]['sha256'], ao.digest(value))
        saved = self.directory() / pointers[0]['path']
        self.assertEqual(saved.read_bytes(), em.json_bytes(value))
        self.assertEqual(stat.S_IMODE(saved.lstat().st_mode), 0o600)
        implementation = self.request('implementation')
        self.assertEqual(implementation['engineering_resolution'],
                         frozen(FAMILY_OPUS, 'opus', OPUS, pointers[0]['sha256']))
        self.assertEqual(implementation['model'], 'opus')  # the configured value, never the observation
        self.assertEqual(self.outcome('implementation')['native']['observed_models'], [OPUS])
        self.assertEqual(implementation['semantic_status']['kind'], 'final_available')
        epoch = em.current(self.directory(), self.state())
        self.assertEqual((epoch['expected_model'], epoch['resolution_sha256'], epoch['resolution_order']),
                         (OPUS, pointers[0]['sha256'], 1))
        after = files_under(self.directory())  # every earlier byte is unchanged
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual(summary['resolution']['status'], 'observed')
        self.assertEqual(summary['resolution']['expected_model'], OPUS)
        self.assertEqual(summary['resolutions'], [{'request_id': 'spec_review', 'observed_model': OPUS,
                                                   'epoch_request_id': None, 'sha256': pointers[0]['sha256']}])
        self.assertEqual(em.check_request(self.directory(), self.state(), implementation)['expected_model'], OPUS)
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('correction', 'prequalified-required')  # observation never qualifies a new dispatch
        self.assertNotIn('prequalified-required', self.state()['requests'])
        self.historical_send('correction', 'fix-1')  # the saved historical epoch keeps its own reader semantics
        self.assertEqual(self.resolutions(), pointers)
        self.assertEqual(self.request('fix-1')['engineering_resolution']['expected_model'], OPUS)

    def assert_contradiction(self, *models):
        self.resolve()
        self.historical_implement(*models)
        outcome = self.outcome('implementation')
        self.assertEqual(outcome['native'], {'unknown': 'Native response model contradicts the owned family request'})
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        pointers = copy.deepcopy(self.resolutions())
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: unknown'):
            self.historical_send('correction', 'fix-1')
        self.assertNotIn('fix-1', self.state()['requests'])
        self.assertEqual(self.resolutions(), pointers)  # the contradiction never advances the epoch
        self.assertEqual(len(pointers), 1)
        self.assertEqual(em.current(self.directory(), self.state())['expected_model'], OPUS)
        self.assertEqual(sorted(os.listdir(self.directory() / RESOLUTIONS)), ['spec_review.json'])

    def test_older_same_family_model_after_resolution_is_a_contradiction(self):
        self.assert_contradiction('claude-opus-5')

    def test_newer_same_family_model_after_resolution_is_a_contradiction(self):
        self.assert_contradiction('claude-opus-5-6')

    def test_other_family_model_is_a_contradiction(self):
        self.assert_contradiction('claude-sonnet-5')

    def test_more_than_one_model_in_one_turn_is_a_contradiction(self):
        self.assert_contradiction(OPUS, 'claude-opus-5-6')

    def test_alias_on_a_native_row_is_never_an_observation(self):
        self.assert_contradiction('opus')

    def test_unresolved_first_turn_contradiction_adopts_nothing(self):
        self.historical_review('claude-sonnet-5', OPUS)
        self.assertIn('contradicts the owned family request', self.outcome('spec_review')['native']['unknown'])
        with self.assertRaises(ao.RoomError):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertIsNone(self.resolutions())
        self.assertIsNone(em.adopt_resolution(self.directory(), self.state()))
        self.assertFalse((self.directory() / RESOLUTIONS).exists())

    def test_without_a_verified_native_source_expectation_stays_unverified(self):
        state = self.state()
        state.pop('native_outcome_source')
        ao.atomic(self.directory() / 'state.json', state)
        self.historical_review(OPUS)
        self.assertIsNone(self.outcome('spec_review')['native'])
        self.historical_implement(OPUS)
        self.assertIsNone(self.resolutions())
        self.assertEqual(self.request('implementation')['engineering_resolution'], frozen(FAMILY_OPUS, 'opus'))
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual(summary['resolution']['status'], 'unverified')
        self.assertIsNone(summary['resolution']['expected_model'])
        self.assertIsNone(summary['pending_resolution'])

    def test_frozen_request_identity_is_checked_against_ancestry(self):
        self.resolve()
        self.historical_implement(OPUS)
        directory = self.directory()
        for change in ({'expected_model': 'claude-opus-5-6'}, {'expected_model': None},
                       {'resolution_sha256': None}, {'configured_model': OPUS},
                       {'selector': {'kind': 'exact', 'model': OPUS}}, {'effort_basis': 'effective max'}):
            with self.subTest(change=change):
                state = self.state()
                state['requests']['implementation']['engineering_resolution'].update(change)
                with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
                    em.check_request(directory, state, state['requests']['implementation'])
        state = self.state()
        state['requests']['implementation'].pop('engineering_resolution')
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            em.check_request(directory, state, state['requests']['implementation'])
        # A tampered saved expectation is refused before native matching can rely on it.
        state = self.state()
        state['requests']['implementation']['engineering_resolution']['expected_model'] = 'claude-opus-5-6'
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            ao_native_outcome.inspect(directory, state, state['requests']['implementation'], self.source,
                                      self.fake.conversation('engineer'))
        # A caller's copy may carry stale observation fields, never a different frozen identity.
        stale = copy.deepcopy(self.request('implementation'))
        stale.pop('semantic_outcome')
        self.assertEqual(ao_native_outcome.inspect(directory, self.state(), stale, self.source,
                                                   self.fake.conversation('engineer'))['observed_models'], [OPUS])
        forged = copy.deepcopy(self.request('implementation'))
        forged['engineering_resolution']['expected_model'] = 'claude-opus-5-6'
        with self.assertRaisesRegex(ao.RoomError, "not this room's retained record"):
            ao_native_outcome.inspect(directory, self.state(), forged, self.source, self.fake.conversation('engineer'))

    def test_interrupted_adoption_record_is_claimed_idempotently_not_duplicated(self):
        self.resolve()
        directory = self.directory()
        unsaved = self.state()
        pointer = em.adopt_resolution(directory, unsaved, now=1700000000.0)  # the state save never happens
        self.assertEqual(unsaved[em.RESOLUTIONS_KEY], [pointer])
        self.assertNotIn(em.RESOLUTIONS_KEY, self.state())
        chain = em.journal(directory, self.state())
        self.assertEqual(chain['pending_resolution']['sha256'], pointer['sha256'])
        self.assertEqual(chain['resolutions'], [])
        self.assertIsNone(em.current(directory, self.state())['expected_model'])  # reported, never applied
        self.assertEqual(self.service.ao_room_status(self.room)['engineering_model']['pending_resolution'],
                         {'request_id': 'spec_review', 'sha256': pointer['sha256']})
        raw = (directory / pointer['path']).read_bytes()
        self.historical_implement(OPUS)
        self.assertEqual(self.resolutions(), [pointer])
        self.assertEqual((directory / pointer['path']).read_bytes(), raw)  # the same bytes, claimed as is
        self.assertEqual(self.request('implementation')['engineering_resolution']['resolution_sha256'],
                         pointer['sha256'])
        self.assertIsNone(em.journal(directory, self.state())['pending_resolution'])

    def test_unclaimed_duplicate_tampered_or_out_of_epoch_resolutions_refuse(self):
        self.resolve()
        self.historical_implement(OPUS)
        directory, pointers, state = self.directory(), self.resolutions(), self.state()
        value = ao.read(directory / pointers[0]['path'])
        implementation = self.request('implementation')
        second = {**value, 'request_id': 'implementation', 'created_order': 2,
                  'receipt_sha256': implementation['receipt_sha256'],
                  'outcome_record_sha256': implementation['semantic_outcome_sha256']}
        cases = [('duplicate', {**state, em.RESOLUTIONS_KEY: pointers * 2}, None),
                 ('digest', {**state, em.RESOLUTIONS_KEY: [{**pointers[0], 'sha256': 'f' * 64}]}, None),
                 ('shape', {**state, em.RESOLUTIONS_KEY: [{**pointers[0], 'extra': 1}]}, None),
                 ('not-a-list', {**state, em.RESOLUTIONS_KEY: pointers[0]}, None),
                 ('missing', {**state, em.RESOLUTIONS_KEY: [{'path': RESOLUTIONS + '/other.json',
                                                            'sha256': pointers[0]['sha256']}]}, None),
                 ('second-for-resolved-epoch', state, ('implementation', second)),
                 ('stray-name', state, ('not an identifier', value))]
        for label, candidate, extra in cases:
            with self.subTest(case=label):
                path = None
                if extra is not None:
                    path = directory / RESOLUTIONS / (extra[0] + '.json')
                    path.write_bytes(em.json_bytes(extra[1]))
                try:
                    for call in (em.journal, em.epochs):
                        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                            call(directory, candidate)
                finally:
                    if path is not None:
                        path.unlink()
        target = directory / pointers[0]['path']
        original = target.read_bytes()
        for change in ({'observed_model': 'claude-opus-5-6'}, {'observed_model': 'opus'},
                       {'observed_model': 'claude-sonnet-5'}, {'configured_model': OPUS},
                       {'selector': {'kind': 'exact', 'model': OPUS}}, {'epoch_request_id': 'other'},
                       {'created_order': 2}, {'receipt_sha256': 'f' * 64}, {'outcome_record_sha256': 'f' * 64},
                       {'room_id': 'ao-other'}, {'version': 2}, {'recorded_at': 'later'}, {'extra': 1}):
            with self.subTest(change=sorted(change)):
                forged = {**value, **change}
                target.write_bytes(em.json_bytes(forged))
                try:
                    candidate = {**state, em.RESOLUTIONS_KEY: [{'path': pointers[0]['path'],
                                                                 'sha256': ao.digest(forged)}]}
                    with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                        em.journal(directory, candidate)
                finally:
                    target.write_bytes(original)
        # Without its claim the file is not the current latest request's interrupted record: refused.
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.journal(directory, {k: v for k, v in state.items() if k != em.RESOLUTIONS_KEY})
        em.journal(directory, state)

    def test_resolution_reset_transition_opens_a_new_unresolved_epoch(self):
        self.resolve()
        self.historical_implement(OPUS)
        parts = self.parts(request_id='reset-opus', source_model='opus', target_model='opus')
        self.assertIsNone(parts['intent']['patch'])
        self.assertIsNone(parts['marker'])
        self.publish(parts)
        directory, state = self.directory(), self.state()
        history = em.epochs(directory, state)
        self.assertEqual([(e['configured_model'], e['expected_model'], e['from_order']) for e in history],
                         [('opus', OPUS, 1), ('opus', None, 3)])
        self.assertEqual(em.effective_binding(directory, state)['model'], 'opus')
        # The earlier epoch keeps its resolution; the historical implementation keeps its frozen expectation.
        self.assertEqual(em.check_request(directory, state, state['requests']['implementation'])['expected_model'],
                         OPUS)
        self.historical_correct('fix-1', 'claude-opus-5-6')
        self.assertEqual(self.request('fix-1')['engineering_resolution'], frozen(FAMILY_OPUS, 'opus'))
        self.assertEqual(self.outcome('fix-1')['native']['observed_models'], ['claude-opus-5-6'])
        self.historical_send('correction', 'fix-2')
        pointers = self.resolutions()
        self.assertEqual(len(pointers), 2)
        second = ao.read(self.directory() / pointers[1]['path'])
        self.assertEqual((second['epoch_request_id'], second['observed_model'], second['request_id']),
                         ('reset-opus', 'claude-opus-5-6', 'fix-1'))
        self.assertEqual(self.request('fix-2')['engineering_resolution'],
                         frozen(FAMILY_OPUS, 'opus', 'claude-opus-5-6', pointers[1]['sha256']))
        self.assertEqual([e['expected_model'] for e in em.epochs(self.directory(), self.state())],
                         [OPUS, 'claude-opus-5-6'])

    def test_reset_shape_is_strict(self):
        self.resolve()
        self.historical_implement(OPUS)
        session = self.state()['bindings']['engineer']['session_id']
        reset = dict(request_id='reset-opus', source_model='opus', target_model='opus')
        payload = {'method': 'PATCH', 'path': '/sessions/%s/conversation/settings' % session,
                   'payload': {'model': 'opus', 'reasoningEffort': 'max'}}
        self.assert_refused(self.parts(**reset, intent_changes={'patch': payload}))
        parts = self.parts(**reset)
        em.store_once(self.directory(), em.BASE + '/attempts/reset-opus.json',
                      {'version': 1, 'room_id': self.room, 'request_id': 'reset-opus',
                       'intent_sha256': parts['intent_sha256'], 'patch': None, 'started_at': 1700000001.0})
        self.assert_refused(parts)
        self.assert_refused(self.parts(**reset, record_changes={'patch_result': {
            'acknowledged': True, 'error': None, 'response_sha256': 'd' * 64}}))
        after = self.parts(**reset)['record']['observed_after']
        for change in ({'controller': 'stopped'}, {'settings': {'model': OPUS, 'reasoningEffort': 'max'}}):
            with self.subTest(change=sorted(change)):
                self.assert_refused(self.parts(**reset, record_changes={'observed_after': {**after, **change}}))
        # Only a same-selector family may reset; an exact identifier is never "reset" to itself.
        self.assert_refused(self.parts(request_id='same-exact', source_model=FABLE, target_model=FABLE))
        self.assertIsNotNone(self.parts(request_id='to-exact', source_model='opus', target_model=OPUS)['intent']['patch'])


class ExactHistoryTests(FamilyFixture):
    """S3/S4 historical checks: an exact epoch keeps exact equality after a later family epoch."""

    def setUp(self):
        super().setUp()
        self.open_room('exact-then-family', FABLE, claude=None)
        self.review_turn(FABLE)
        self.historical = copy.deepcopy(self.request('spec_review'))
        self.historical_outcome = (self.directory() / self.historical['semantic_outcome']).read_bytes()
        self.publish(self.parts(request_id='to-opus-family', source_model=FABLE, target_model='opus'))
        self.fake.snapshots['engineer']['settings']['model'] = 'opus'

    def test_historical_exact_request_is_never_widened_to_family_matching(self):
        directory, state = self.directory(), self.state()
        self.assertEqual([(e['selector']['kind'], e['configured_model']) for e in em.epochs(directory, state)],
                         [('exact', FABLE), ('family', 'opus')])
        self.assertEqual(em.check_request(directory, state, state['requests']['spec_review'])['configured_model'],
                         FABLE)
        self.assertNotIn('observed_models', self.outcome('spec_review')['native'])
        historical = state['requests']['spec_review']
        self.assertTrue(em.model_matches(historical, FABLE))
        for model in ('claude-fable-5-2', 'claude-fable-5', 'fable', OPUS, 'opus'):
            with self.subTest(model=model):
                self.assertFalse(em.model_matches(historical, model))
                rows = events_for(historical, model)
                with self.assertRaisesRegex(ao.RoomError, 'Native response model contradicts the owned request$'):
                    ao_native_outcome.events_outcome(rows, historical, 'native')
        for model in ('fable', 'claude-fable-5-2', 'opus', OPUS):
            with self.subTest(tampered=model):
                tampered = copy.deepcopy(state)
                tampered['requests']['spec_review']['model'] = model
                with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
                    em.check_request(directory, tampered, tampered['requests']['spec_review'])
        # Sync re-reads the historical exact turn without rewriting its request or outcome record.
        self.service.ao_room_sync(self.room)
        self.assertEqual(self.request('spec_review'), self.historical)
        self.assertEqual((self.directory() / self.historical['semantic_outcome']).read_bytes(),
                         self.historical_outcome)

    def test_first_family_turn_after_the_boundary_resolves_only_its_own_epoch(self):
        # A new dispatch in this unqualified family epoch refuses with actionable readiness ...
        self.service.ao_room_handoff(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('implementation')
        self.assertNotIn('implementation', self.state()['requests'])
        # ... while a saved pre-correction family turn still resolves only its own epoch.
        self.historical_implement(OPUS)
        implementation = self.request('implementation')
        self.assertEqual((implementation['model'], implementation['created_order']), ('opus', 2))
        self.assertEqual(implementation['engineering_resolution'], frozen(FAMILY_OPUS, 'opus'))
        self.assertEqual(self.outcome('implementation')['native']['observed_models'], [OPUS])
        self.historical_send('correction', 'fix-1')
        value = ao.read(self.directory() / self.resolutions()[0]['path'])
        self.assertEqual((value['epoch_request_id'], value['request_id'], value['observed_model']),
                         ('to-opus-family', 'implementation', OPUS))
        self.assertEqual([e['expected_model'] for e in em.epochs(self.directory(), self.state())], [FABLE, OPUS])
        self.assertEqual(self.request('spec_review'), self.historical)


class QualityReviewConsumerTests(FamilyFixture):
    """S5: quality review follows ancestry (the integration probe case) and still refuses tampering."""

    def test_post_transition_send_is_accepted_and_tampered_models_refused(self):
        self.open_room('quality-exact', FABLE, claude=None)
        self.review_turn(FABLE)
        self.publish(self.parts(request_id='to-opus', source_model=FABLE, target_model=OPUS))
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        self.implement_with(OPUS)
        self.assertEqual(self.request('implementation')['model'], OPUS)
        self.assertEqual(self.state()['bindings']['engineer']['model'], FABLE)  # never rewritten
        directory = self.directory()
        ao_quality_review.inspect(directory, self.state())
        for key, model in (('implementation', FABLE), ('implementation', 'claude-other-1-1'),
                           ('implementation', 'opus'), ('spec_review', OPUS)):
            with self.subTest(request=key, model=model):
                state = self.state()
                original = state['requests'][key]['model']
                state['requests'][key]['model'] = model
                ao.atomic(directory / 'state.json', state)
                try:
                    with self.assertRaisesRegex(ao.RoomError, 'quality_evidence_integrity'):
                        ao_quality_review.inspect(directory, self.state())
                finally:
                    state['requests'][key]['model'] = original
                    ao.atomic(directory / 'state.json', state)
        self.send('correction', 'fix-1')
        self.assertEqual(self.request('fix-1')['model'], OPUS)

    def test_family_transition_send_follows_ancestry(self):
        self.open_room('quality-family', FABLE, claude=None)
        self.review_turn(FABLE)
        self.publish(self.parts(request_id='to-opus', source_model=FABLE, target_model='opus'))
        self.fake.snapshots['engineer']['settings']['model'] = 'opus'
        self.historical_implement(OPUS)  # a saved pre-correction family turn, not a new unqualified dispatch
        ao_quality_review.inspect(self.directory(), self.state())
        request = self.request('implementation')
        binding = em.binding_for_request(self.directory(), self.state(), self.state()['bindings']['engineer'], request)
        self.assertEqual(binding['model'], 'opus')
        self.historical_send('correction', 'fix-1')
        self.assertEqual(self.request('fix-1')['engineering_resolution']['expected_model'], OPUS)


class ReviewFindingRegressionTests(ModelFixture):
    """R1/R2 counterexamples from the second-pass review, reproduced against the current module."""

    def setUp(self):
        super().setUp()
        self.room = self.open_model('regressions', model=FABLE)['room_id']
        self.spec()
        self.bind_at(FABLE)
        self.agree()

    def test_r1_identical_retry_repeats_a_failed_existing_ancestor_barrier(self):
        directory = self.directory()
        base = directory / em.BASE
        base.mkdir(mode=0o700)  # the journal base exists durably; requests/ is absent
        actual, calls, failed = em._fsync, [], [False]

        def injected(path, folder=False):
            calls.append(str(Path(path).relative_to(directory)))
            if Path(path) == base and not failed[0]:
                failed[0] = True
                raise OSError('synthetic parent fsync failure')
            return actual(path, folder)

        relative = em.BASE + '/requests/probe.json'
        with patch.object(em, '_fsync', injected):
            with self.assertRaises(OSError):
                em.store_once(directory, relative, {'probe': True})
            first = list(calls)
            calls.clear()
            digest = em.store_once(directory, relative, {'probe': True})
        self.assertEqual(first, [em.BASE])  # mkdir(requests), then its failed publication into BASE
        self.assertEqual(digest, ao.digest({'probe': True}))
        self.assertEqual(calls, [em.BASE + '/requests', em.BASE, '.'])  # new file: fsynced on its descriptor
        self.assertIn(em.BASE, calls)  # the failed ancestor barrier is crossed again by the identical retry

    def record_call(self, outcome='committed', **changes):
        parts = self.parts(outcome=outcome)
        after = parts['record']['observed_after']
        for key, value in changes.items():
            after['native_owner' if key == 'owner' else key] = value
        state = self.state()
        pointer_state = 'committed' if outcome == 'committed' else 'abandoned'
        return lambda: em._record(state, 'switch-model', parts['intent_sha256'], pointer_state, parts['intent'],
                                  parts['record'], parts['marker'])

    def test_r2_committed_observations_keep_the_owner_and_admit_no_intervening_work(self):
        owner = self.parts()['record']['observed_after']['native_owner']
        self.record_call()()  # control
        for label, changes in (('different_native_owner', {'owner': {**owner, 'provider_conversation_id': 'other'}}),
                               ('empty_native_owner', {'owner': {}}),
                               ('changed_transcript', {'transcript_sha256': 'e' * 64}),
                               ('active_controller', {'controller': 'running'}),
                               ('stopped_controller', {'controller': 'stopped'}),
                               ('new_generation', {'owner': {**owner, 'controller_generation': 'generation-2'}}),
                               ('exited_owner', {'owner': {**owner, 'activity_state': 'exited'}}),
                               ('changed_history', {'history_sha256': 'e' * 64})):
            with self.subTest(case=label), self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                self.record_call(**changes)()
        before = self.parts()['record']['observed_before']
        for label, value in (('owner', {**before, 'native_owner': {**before['native_owner'], 'activity_state': 'busy'}}),
                             ('transcript', {**before, 'transcript_sha256': 'e' * 64}),
                             ('controller', {**before, 'controller': 'stopped'})):
            with self.subTest(before=label):
                self.assert_refused(self.parts(record_changes={'observed_before': value}))

    def test_r2_abandonment_admits_only_liveness_and_generation_changes(self):
        owner = self.parts(outcome='abandoned_unchanged')['record']['observed_after']['native_owner']
        for changes in ({}, {'controller': 'stopped'},
                        {'owner': {**owner, 'activity_state': 'exited', 'controller_generation': 'generation-2'}},
                        {'controller': 'stopped', 'owner': {**owner, 'activity_state': 'exited'}}):
            with self.subTest(accepted=sorted(changes)):
                self.record_call('abandoned_unchanged', **changes)()
        for changes in ({'controller': 'running'},
                        {'owner': {**owner, 'provider_conversation_id': 'other'}},
                        {'owner': {**owner, 'ao_conversation_id': 'other'}},
                        {'owner': {**owner, 'activity_state': 'active'}},
                        {'owner': {**owner, 'controller_generation': ''}},
                        {'owner': {k: v for k, v in owner.items() if k != 'controller_generation'}},
                        {'owner': {}}, {'transcript_sha256': 'e' * 64}):
            with self.subTest(refused=sorted(changes)), \
                    self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
                self.record_call('abandoned_unchanged', **changes)()


class LegacyExactRoomTests(FamilyFixture):
    """A room without a selection record stays exact claude-fable-5-1 end to end."""

    def setUp(self):
        super().setUp()
        self.room = self.open_model('legacy-exact')['room_id']
        directory, state = self.directory(), self.state()
        record = state.pop(em.SELECTION_KEY)
        (directory / record['policy_record']).unlink()
        (directory / 'engineering-model').rmdir()
        ao.atomic(directory / 'state.json', state)
        self.spec()
        self.bind_at(FABLE)
        self.configure_native()

    def test_legacy_room_flow_keeps_exact_identity_and_historical_shapes(self):
        self.assertEqual(em.initial(self.directory(), self.state())['source'], 'legacy_default')
        self.review_turn(FABLE)
        native = self.outcome('spec_review')['native']
        self.assertEqual(set(native), {'anchor_uuid', 'next_human_uuid', 'errors', 'settled_errors', 'stop_reasons',
                                       'source', 'source_sha256', 'compaction_imports'})
        historical = copy.deepcopy(self.request('spec_review'))
        historical_bytes = {name: data for name, data in files_under(self.directory()).items()
                            if name.startswith(('outcomes/spec_review/', 'receipts/spec_review/'))}
        self.assertEqual(historical['engineering_resolution'], frozen(selector(FABLE), FABLE, FABLE))
        self.implement_with(FABLE)
        self.assertNotIn('observed_models', self.outcome('implementation')['native'])
        self.assertEqual(self.outcome('implementation')['outcome']['kind'], 'final_available')
        self.send('correction', 'fix-1')
        self.assertNotIn(em.RESOLUTIONS_KEY, self.state())
        self.assertFalse((self.directory() / em.BASE).exists())
        self.assertNotIn(em.SELECTION_KEY, self.state())
        self.assertEqual(self.request('spec_review'), historical)
        self.assertEqual({k: v for k, v in files_under(self.directory()).items() if k in historical_bytes},
                         historical_bytes)
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual((summary['initial']['source'], summary['resolution']['status'],
                          summary['resolution']['expected_model']), ('legacy_default', 'exact', FABLE))

    def test_legacy_room_newer_same_family_model_is_a_contradiction(self):
        self.review_turn('claude-fable-5-2')
        self.assertEqual(self.outcome('spec_review')['native'],
                         {'unknown': 'Native response model contradicts the owned request'})


class RerouteRuleTests(unittest.TestCase):
    """S4 conflicting_reroute and the shared matcher used by every reroute consumer."""

    def request(self, frozen_value=None, model='opus'):
        value = {'request_id': 'r', 'model': model, 'provider_turn_id': 'p', 'baseline': {}}
        if frozen_value is not None:
            value['engineering_resolution'] = frozen_value
        return value

    def test_family_and_exact_reroute_rules(self):
        unresolved = self.request(frozen(FAMILY_OPUS, 'opus'))
        resolved = self.request(frozen(FAMILY_OPUS, 'opus', OPUS, 'a' * 64))
        exact = self.request(frozen(selector(OPUS), OPUS, OPUS), model=OPUS)
        historical = self.request(model=FABLE)
        cases = [(unresolved, OPUS, False), (unresolved, 'claude-opus-5-6', False), (unresolved, 'claude-opus-5', False),
                 (unresolved, 'opus', True), (unresolved, 'claude-sonnet-5', True), (unresolved, None, True),
                 (unresolved, 'claude-opus-latest', True), (unresolved, 'claude-opus-5-5[1m]', True),
                 (resolved, OPUS, False), (resolved, 'claude-opus-5-6', True), (resolved, 'claude-opus-5', True),
                 (exact, OPUS, False), (exact, 'claude-opus-5-6', True), (exact, 'opus', True),
                 (historical, FABLE, False), (historical, 'claude-fable-5-2', True), (historical, 'fable', True)]
        for request, target, conflicting in cases:
            with self.subTest(request=request.get('engineering_resolution'), target=target):
                snapshot = {'modelReroute': {'fromModel': request['model'], 'toModel': target, 'providerTurnId': 'p'}}
                self.assertEqual(ao.conflicting_reroute(request, snapshot) is not None, conflicting)
                self.assertEqual(em.model_matches(request, target), not conflicting)
        malformed = self.request({**frozen(FAMILY_OPUS, 'opus'), 'selector': {'kind': 'family'}})
        self.assertFalse(em.model_matches(malformed, OPUS))
        mismatched = self.request(frozen(FAMILY_OPUS, 'opus'), model=OPUS)  # configured value is not the alias
        self.assertFalse(em.model_matches(mismatched, OPUS))

    def test_family_member_is_exact_and_never_an_alias(self):
        for family, model, expected in (('opus', OPUS, True), ('opus', 'claude-opus-5', True),
                                        ('opus', 'claude-opus-5-6-1', True), ('opus', 'opus', False),
                                        ('opus', 'claude-opus', False), ('opus', 'claude-opus-latest', False),
                                        ('opus', 'claude-sonnet-5', False), ('opus', 'claude-opusx-5', False),
                                        ('opus', 'Claude-opus-5-5', False), ('opus', 'claude-opus-5-5 ', False),
                                        ('op.s', 'claude-opus-5', False), ('fable', FABLE, True)):
            with self.subTest(family=family, model=model):
                self.assertEqual(em.family_member(family, model), expected)


class NoticeTimingTests(FamilyFixture):
    """R1-R4 residuals: a root notice is authenticated at the order that actually delivered it.

    The transitions here are the fixture's disclosed synthetic journal records written through the
    module's own create-once writer; native ownership uses the fixture's disclosed synthetic owner
    reader, and every pre-qualification family request is built with the same scoped
    original-root-freeze helper the existing historical tests use. These synthetic constructions are
    not real-SQL, live-room, provider or fresh qualified dispatch evidence.
    """

    def family_after_exact(self, feature):
        """One exact room with a completed review, then the fixture's committed family transition."""
        self.open_room(feature, FABLE, claude=None)
        self.review_turn(FABLE)
        self.publish(self.parts(request_id='to-opus-family', source_model=FABLE, target_model='opus'))
        self.fake.snapshots['engineer']['settings']['model'] = 'opus'

    def root_notices(self, key):
        carried = self.request(key).get('carried') or {}
        return [n for n in carried.get('boundary_notices') or []
                if n['kind'] in (ao_model_boundaries.ROOT_CHANGE, ao_model_boundaries.ROOT_RESET)]

    def root_notice(self, key):
        notices = self.root_notices(key)
        self.assertEqual(len(notices), 1)
        return self.request(key), notices[0]

    def test_only_unqualified_family_roots_depend_on_the_authenticated_dispatch_order(self):
        family = em._epoch(FAMILY_OPUS, {'harness': 'claude-code', 'reasoning_effort': 'max'}, 2,
                           request_id='to-opus-family', record_sha256='a' * 64)
        self.assertIsNone(family['expected_model'])
        self.assertIs(ao_model_boundaries._root_epoch_at(family, None), family)
        resolved = {**family, 'expected_model': OPUS, 'resolution_sha256': 'b' * 64, 'resolution_order': 1}
        with self.assertRaisesRegex(ao.RoomError, 'authenticated dispatch order'):
            ao_model_boundaries._root_epoch_at(resolved, None)
        self.assertIsNone(ao_model_boundaries._root_epoch_at(resolved, 1)['expected_model'])
        self.assertIsNone(ao_model_boundaries._root_epoch_at(resolved, 1)['resolution_sha256'])
        self.assertEqual(ao_model_boundaries._root_epoch_at(resolved, 2)['expected_model'], OPUS)
        exact = em._epoch(selector(OPUS), {'harness': 'claude-code', 'reasoning_effort': 'max'}, 2)
        self.assertIs(ao_model_boundaries._root_epoch_at(exact, None), exact)
        self.assertIs(ao_model_boundaries._root_epoch_at(exact, 1), exact)
        qualified = {**family, 'family_qualified': True, 'qualification_sha256': 'c' * 64,
                     'expected_model': OPUS}
        self.assertIs(ao_model_boundaries._root_epoch_at(qualified, None), qualified)
        self.assertIs(ao_model_boundaries._root_epoch_at(qualified, 1), qualified)

    def test_before_resolution_notice_stays_verifiable_after_genuine_adoption(self):
        self.family_after_exact('notice-before-resolution')
        self.historical_implement(OPUS)
        request, notice = self.root_notice('implementation')
        self.assertEqual(request['created_order'], 2)
        directory, before = self.directory(), self.state()
        unresolved = ao_model_boundaries.notice_fragment(directory, before, notice, request=request)
        self.assertIn('No pre-inference source qualification and no completed owned turn establish', unresolved)
        self.assertNotIn('was observed on a completed owned turn', unresolved)
        self.assertEqual(ao.digest(unresolved.encode()), notice['fragment_sha256'])
        self.assertEqual(ao_model_boundaries.notice_fragment(directory, before, notice), unresolved)
        identity = ao_model_boundaries.notice_identity(notice)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, before, notice, request), identity)
        before_files = files_under(directory)
        posts = len(self.fake.posts)
        adopted = copy.deepcopy(before)
        pointer = em.adopt_resolution(directory, adopted)  # the module's own resolution writer, no send
        self.assertIsNotNone(pointer)
        self.service.save(directory, adopted)
        after = self.state()
        epoch = em.current(directory, after)
        self.assertEqual((epoch['expected_model'], epoch['resolution_sha256'], epoch['resolution_order']),
                         (OPUS, pointer['sha256'], 2))
        self.assertEqual(ao_model_boundaries.notice_fragment(directory, after, notice, request=request), unresolved)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, after, notice, request), identity)
        self.assertIn('was observed on a completed owned turn',
                      ao_model_boundaries.fragment_text(
                          ao_model_boundaries.current_root_boundary(directory, after)))
        self.assertIn(identity, ao_workflow.delivered(after, request['session_id'], directory)['notices'])
        with self.assertRaisesRegex(ao.RoomError, 'authenticated dispatch order'):
            ao_model_boundaries.notice_fragment(directory, after, notice)
        self.assertEqual(self.request('implementation'), request)
        self.assertEqual({k: v for k, v in files_under(directory).items() if k in before_files}, before_files)
        self.assertEqual(len(self.fake.posts), posts)
        saved = directory / pointer['path']
        self.assertEqual(saved.read_bytes(), em.json_bytes(ao.read(saved)))
        self.assertEqual(ao.digest(ao.read(saved)), pointer['sha256'])
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('correction', 'still-unqualified')
        self.assertNotIn('still-unqualified', self.state()['requests'])
        self.assertEqual(len(self.fake.posts), posts)

    def test_notice_first_rendered_after_an_already_validated_resolution_stays_exact(self):
        self.open_room('notice-after-resolution', 'opus')
        self.historical_review(OPUS)
        self.assertEqual(self.root_notices('spec_review'), [])
        # The same-family reset is committed at the request boundary that already existed, so this
        # earlier genuine request stays exactly its own record, without notice metadata.
        self.publish(self.parts(request_id='reset-opus', source_model='opus', target_model='opus',
                                boundary=0, evidence_changes={'requests': []}))
        self.assertEqual([(e['configured_model'], e['expected_model'], e['from_order'])
                          for e in em.epochs(self.directory(), self.state())],
                         [('opus', None, 1), ('opus', None, 1)])
        self.historical_implement(OPUS)
        request, notice = self.root_notice('implementation')
        self.assertEqual(request['created_order'], 2)
        directory, state = self.directory(), self.state()
        self.assertEqual(em.current(directory, state)['resolution_order'], 1)
        fragment = ao_model_boundaries.notice_fragment(directory, state, notice, request=request)
        self.assertIn('was observed on a completed owned turn', fragment)
        self.assertIn('was re-committed as a new family epoch', fragment)
        self.assertEqual(ao.digest(fragment.encode()), notice['fragment_sha256'])
        self.assertEqual(ao_model_boundaries.fragment_text(
            ao_model_boundaries.current_root_boundary(directory, state)), fragment)
        identity = ao_model_boundaries.notice_identity(notice)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice, request), identity)
        self.historical_correct('fix-1', OPUS)
        self.assertEqual(self.root_notices('fix-1'), [])
        self.assertIn(identity, ao_workflow.delivered(self.state(), request['session_id'], directory)['notices'])
        self.assertEqual(self.request('fix-1')['engineering_resolution']['expected_model'], OPUS)

    def test_same_family_reset_keeps_each_epoch_notice_at_its_own_timing(self):
        self.open_room('notice-reset-timing', 'opus')
        self.historical_review(OPUS)
        self.historical_implement(OPUS)
        self.assertEqual(self.root_notices('implementation'), [])
        self.publish(self.parts(request_id='reset-opus', source_model='opus', target_model='opus'))
        self.historical_correct('fix-1', 'claude-opus-5-6')
        request, notice = self.root_notice('fix-1')
        self.assertEqual(request['created_order'], 3)
        directory, before = self.directory(), self.state()
        unresolved = ao_model_boundaries.notice_fragment(directory, before, notice, request=request)
        self.assertIn('starts unresolved again', unresolved)
        self.assertIn('No pre-inference source qualification and no completed owned turn establish', unresolved)
        self.assertEqual(ao.digest(unresolved.encode()), notice['fragment_sha256'])
        self.historical_send('correction', 'fix-2')
        after = self.state()
        pointers = self.resolutions()
        self.assertEqual(len(pointers), 2)
        self.assertEqual(ao.read(directory / pointers[1]['path'])['observed_model'], 'claude-opus-5-6')
        self.assertEqual(ao_model_boundaries.notice_fragment(directory, after, notice, request=request), unresolved)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, after, notice, request),
                         ao_model_boundaries.notice_identity(notice))
        self.assertIn('was observed on a completed owned turn',
                      ao_model_boundaries.fragment_text(
                          ao_model_boundaries.current_root_boundary(directory, after)))
        self.assertEqual(self.request('fix-2')['engineering_resolution']['expected_model'], 'claude-opus-5-6')

    def test_later_authorized_delivery_does_not_replay_a_verified_notice(self):
        self.family_after_exact('notice-no-replay')
        self.historical_implement(OPUS)
        request, notice = self.root_notice('implementation')
        identity = ao_model_boundaries.notice_identity(notice)
        directory = self.directory()
        self.historical_correct('fix-1', OPUS)
        self.assertEqual(self.root_notices('fix-1'), [])
        held = ao_workflow.delivered(self.state(), request['session_id'], directory)
        self.assertEqual(held['notices'].count(identity), 1)
        self.assertEqual(self.request('fix-1')['engineering_resolution']['expected_model'], OPUS)

    def test_exact_root_and_worker_notices_keep_their_standalone_semantics(self):
        self.open_room('notice-exact-root', FABLE, claude=None)
        self.review_turn(FABLE)
        self.publish(self.parts(request_id='to-opus', source_model=FABLE, target_model=OPUS))
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        self.implement_with(OPUS)
        request = self.request('implementation')
        carried = request['carried']['boundary_notices']
        self.assertEqual(sorted(n['kind'] for n in carried),
                         [ao_model_boundaries.ROOT_CHANGE, ao_model_boundaries.WORKER_ROUTING])
        directory, state = self.directory(), self.state()
        for notice in carried:
            with self.subTest(kind=notice['kind']):
                identity = ao_model_boundaries.notice_identity(notice)
                self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice), identity)
                self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice, request), identity)
                self.assertEqual(ao_model_boundaries.notice_fragment(directory, state, notice),
                                 ao_model_boundaries.notice_fragment(directory, state, notice, request=request))

    def test_qualified_root_notice_delivery_is_not_order_dependent(self):
        self.open_room('notice-qualified-root', FABLE, claude=None)
        self.review_turn(FABLE)
        with self.worker_preparation():
            target = self.parts(request_id='to-opus-family', source_model=FABLE, target_model='opus')
        self.assertIn('execution_qualification', target['intent']['target']['qualification'])
        self.publish(target)
        self.fake.snapshots['engineer']['settings']['model'] = 'opus'
        self.implement_with('claude-opus-5-5')
        request, notice = self.root_notice('implementation')
        directory, state = self.directory(), self.state()
        fragment = ao_model_boundaries.notice_fragment(directory, state, notice, request=request)
        self.assertIn('source-qualified exact expected model is claude-opus-5-5', fragment)
        self.assertEqual(ao_model_boundaries.notice_fragment(directory, state, notice), fragment)
        identity = ao_model_boundaries.notice_identity(notice)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice), identity)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice, request), identity)
        self.correct_with('fix-1', 'claude-opus-5-5')
        self.assertEqual(self.root_notices('fix-1'), [])
        self.assertIn(identity, ao_workflow.delivered(self.state(), request['session_id'], directory)['notices'])

    def test_notice_reader_refuses_foreign_borrowed_and_tampered_context(self):
        self.family_after_exact('notice-negatives')
        self.historical_implement(OPUS)
        request, notice = self.root_notice('implementation')
        directory, state = self.directory(), self.state()
        identity = ao_model_boundaries.delivered_notice(directory, state, notice, request)
        before_files = files_under(directory)
        posts = len(self.fake.posts)
        cases = [('foreign-request', 'stale or foreign request copy', notice, self.request('spec_review')),
                 ('stale-request-copy', 'stale or foreign request copy', notice,
                  {**request, 'text_sha256': 'f' * 64}),
                 ('stale-order', 'stale or foreign request copy', notice, {**request, 'created_order': 1}),
                 ('uncommitted-authority', 'does not name this room',
                  {**notice, 'authority': {**notice['authority'], 'record_sha256': 'e' * 64}}, request),
                 ('kind-mismatch', 'does not match its own committed boundary',
                  {**notice, 'kind': ao_model_boundaries.ROOT_RESET}, request),
                 ('fragment-mismatch', 'does not match its own committed boundary',
                  {**notice, 'fragment_sha256': 'f' * 64}, request)]
        for label, message, candidate, owner in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(ao.RoomError, message):
                    ao_model_boundaries.delivered_notice(directory, state, candidate, owner)
        self.assertEqual(ao_model_boundaries.delivered_notice(directory, state, notice, request), identity)
        self.assertEqual({k: v for k, v in files_under(directory).items() if k in before_files}, before_files)
        self.assertEqual(len(self.fake.posts), posts)


if __name__ == '__main__':
    unittest.main()
