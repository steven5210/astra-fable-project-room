"""Qualified family identity: operator-selected qualification artifacts and pre-inference expectations.

Synthetic offline records only: the artifact and its retained evidence file are ordinary private
files inside the fixture's temporary directory. No network call is made and no model is invoked.
Version 2 rooms and their observation-based resolutions keep their own tests; every fixture here
pins an explicit private qualification pointer before the room opens.
"""
import copy
import hashlib
import json
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_model_qualification as qmod
import ao_native_outcome
import ao_project_room as ao
from test_ao_engineering_family import EFFORT_BASIS, FAMILY_OPUS, RESOLUTIONS, FamilyFixture
from test_ao_engineering_model import FABLE, OPUS, files_under


class QualificationFixture(FamilyFixture):
    """A synthetic private qualification artifact with its own retained evidence file."""

    def evidence(self, text='Synthetic operator-retained documentation excerpt.\n', name='opus-family.txt'):
        path = self.root / 'evidence' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
        return path

    def artifact(self, families=None, sources=None, **changes):
        path = self.evidence()
        descriptor = {'id': 'opus-family-doc', 'uri': 'https://docs.invalid.example/claude/opus-family',
                      'captured_at': '2026-06-01T00:00:00Z',
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence_file': str(path)}
        value = {'format': qmod.FORMAT, 'revision': 3, 'qualified_at': '2026-06-02T00:00:00Z',
                 'scope': copy.deepcopy(qmod.SCOPE),
                 'families': {'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                                       'minimum_claude_code_version': '2.1.280'}},
                 'sources': [descriptor]}
        if families is not None:
            value['families'] = families
        if sources is not None:
            value['sources'] = sources
        value.update(changes)
        return value

    def configure(self, artifact, path=None):
        path = Path(path) if path is not None else self.root / 'qualification.json'
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
        path.chmod(0o600)
        config = self.home / 'ao' / 'config.json'
        value = ao.read(config) if config.exists() else {}
        value['family_qualification'] = {'path': str(path), 'sha256': qmod.digest(artifact)}
        ao.atomic(config, value)
        return path

    WORKER_SONNET = 'claude-sonnet-5-5'

    def worker_artifact(self):
        """A newer artifact mapping every enabled worker family, for a modern fresh preparation.

        The room's own root epoch keeps whatever artifact was selected when it opened; a fresh
        preparation additionally selects the operator qualification for both enabled worker families,
        so this artifact is configured only for the preparation itself.
        """
        artifact = self.artifact(families={
            'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': '2.1.280'},
            'sonnet': {'expected_model': self.WORKER_SONNET, 'source_ids': ['opus-family-doc']}})
        artifact['revision'] += 1
        return artifact

    def qualified_room(self, feature, artifact=None, version='2.1.282', model='opus'):
        if version:
            self.install_claude('claude-' + version.replace('.', '-'), version + ' (Claude Code)')
        artifact = self.artifact() if artifact is None else artifact
        self.configure(artifact)
        self.room = self.open_model(feature, model=model)['room_id']
        self.spec()
        # The room's root epoch pins the artifact selected at open. A modern fresh preparation
        # additionally selects the operator-selected qualification for both enabled worker families,
        # so a newer worker-capable artifact is configured here: the root epoch stays the independently
        # selected artifact while the workers keep their own newer snapshot.
        self.configure(self.worker_artifact())
        self.bind_at(em.initial(self.directory(), self.state())['configured_model'])
        self.configure_native()
        return artifact


class QualificationSelectionTests(QualificationFixture):
    """Version 3 policy/selection pinning and pre-inference expectations."""

    def test_qualified_room_pins_version_three_policy_selection_and_retained_artifact(self):
        artifact = self.qualified_room('qualified-open')
        sha = qmod.digest(artifact)
        record = self.state()[em.SELECTION_KEY]
        self.assertEqual(record['version'], em.SELECTION_VERSION_V3)
        self.assertEqual(set(record), em.SELECTION_FIELDS_V3)
        self.assertEqual(record['policy']['format'], em.FORMAT_V3)
        self.assertEqual(record['policy']['family_qualification'], {'artifact': artifact, 'sha256': sha})
        self.assertEqual(record['family_qualification'],
                         {'artifact': artifact, 'sha256': sha, 'record': qmod.BASE + '/' + sha + '.json',
                          'evidence': qmod.evidence_entries(artifact, sha)})
        entry = record['policy']['families']['opus']
        self.assertEqual(entry['execution_qualification'],
                         {'version': 1, 'expected_model': OPUS, 'qualification_sha256': sha})
        self.assertEqual(entry['minimum_claude_code_version'], '2.1.280')
        self.assertIsNone(record['policy']['families']['fable']['execution_qualification'])
        self.assertEqual(record['provenance']['family_qualification_sha256'], sha)
        self.assertEqual(record['provenance']['qualified_families'], ['opus'])
        saved = self.directory() / record['family_qualification']['record']
        self.assertEqual(saved.read_bytes(), em.json_bytes(artifact))
        self.assertEqual(stat.S_IMODE(saved.lstat().st_mode), 0o600)
        retained_entry = record['family_qualification']['evidence'][0]
        retained = self.directory() / retained_entry['record']
        self.assertEqual(retained.read_bytes(), (self.root / 'evidence' / 'opus-family.txt').read_bytes())
        self.assertEqual(stat.S_IMODE(retained.lstat().st_mode), 0o600)
        self.assertEqual(em.initial(self.directory(), self.state())['qualification'], entry)
        epoch = em.current(self.directory(), self.state())
        self.assertEqual((epoch['expected_model'], epoch['qualification_sha256'], epoch['family_qualified']),
                         (OPUS, sha, True))
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual((summary['resolution']['status'], summary['resolution']['expected_model'],
                          summary['resolution']['qualification_sha256']), ('qualified', OPUS, sha))
        self.assertEqual(summary['effort'], {'configured': 'max', 'effective': 'not evidenced'})
        for claim in ('"available"', '"account"', '"quota"', 'adopted', 'attested'):
            self.assertNotIn(claim, json.dumps(summary))

    def test_retained_qualification_evidence_is_immutable_and_required(self):
        artifact = self.qualified_room('qualified-evidence')
        directory = self.directory()
        record = self.state()[em.SELECTION_KEY]['family_qualification']
        saved = directory / record['record']
        raw = saved.read_bytes()
        saved.write_bytes(em.json_bytes({**artifact, 'revision': 4}))
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        saved.write_bytes(raw)
        em.initial(directory, self.state())
        saved.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        saved.write_bytes(raw)
        em.initial(directory, self.state())
        retained = directory / record['evidence'][0]['record']
        raw_capture = retained.read_bytes()
        retained.write_bytes(b'changed capture\n')
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.write_bytes(raw_capture)
        em.initial(directory, self.state())
        retained.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.symlink_to(self.root / 'evidence' / 'opus-family.txt')
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.unlink()
        retained.write_bytes(raw_capture)
        em.initial(directory, self.state())

    def test_new_frozen_expectation_is_pre_inference_and_qualified(self):
        artifact = self.qualified_room('qualified-freeze')
        sha = qmod.digest(artifact)
        self.send('spec_review')
        request = self.request('spec_review')
        self.assertEqual(request['engineering_resolution'],
                         {'version': 2, 'selector': FAMILY_OPUS, 'configured_model': 'opus', 'expected_model': OPUS,
                          'qualification_sha256': sha, 'resolution_sha256': None, 'effort_basis': EFFORT_BASIS})
        self.assertEqual(request['model'], 'opus')
        self.assertIsNone(self.resolutions())
        self.assertIsNone(em.current(self.directory(), self.state())['resolution_sha256'])
        self.assertFalse((self.directory() / RESOLUTIONS).exists())

    def test_observation_never_resolves_or_replaces_a_qualified_expectation(self):
        artifact = self.qualified_room('qualified-no-adopt')
        sha = qmod.digest(artifact)
        self.review_turn(OPUS)
        self.assertIsNone(em.adopt_resolution(self.directory(), self.state()))
        self.assertIsNone(self.resolutions())
        self.assertFalse((self.directory() / RESOLUTIONS).exists())
        self.assertEqual(em.current(self.directory(), self.state())['expected_model'], OPUS)
        native = self.outcome('spec_review')['native']
        self.assertEqual(native['observed_models'], [OPUS])
        self.assertEqual(native['expected_model'], OPUS)
        self.assertEqual(native['qualification_sha256'], sha)
        self.assertEqual(native['stop_row_ids'], ['spec_review-reply-0'])
        self.assertNotIn('model_contradiction', native)
        self.assertEqual(self.outcome('spec_review')['outcome']['kind'], 'final_available')
        self.implement_with(OPUS)
        self.assertEqual(self.request('implementation')['engineering_resolution'],
                         {'version': 2, 'selector': FAMILY_OPUS, 'configured_model': 'opus', 'expected_model': OPUS,
                          'qualification_sha256': sha, 'resolution_sha256': None, 'effort_basis': EFFORT_BASIS})
        self.assertIsNone(self.resolutions())

    def test_exact_id_compatibility_under_version_three_keeps_exact_matching(self):
        self.install_claude('claude-qualified', '2.1.282 (Claude Code)')
        artifact = self.artifact()
        self.configure(artifact)
        self.room = self.open_model('qualified-exact', model=OPUS)['room_id']
        self.spec()
        self.fake.snapshots['engineer']['settings']['model'] = OPUS
        # The root epoch pins this exact-opus artifact; the fresh preparation independently selects
        # its own both-worker-family artifact through the scoped fixture.
        with self.worker_preparation(self.worker_artifact()):
            self.service.ao_room_prepare(self.room, str(self.repo))
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', OPUS, 'max')
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', 'astra', 'max')
        self.configure_native()
        record = self.state()[em.SELECTION_KEY]
        self.assertEqual((record['version'], record['selector']), (3, {'kind': 'exact', 'model': OPUS}))
        self.assertIsNone(record['qualification'].get('execution_qualification'))
        self.send('spec_review')
        frozen_value = self.request('spec_review')['engineering_resolution']
        self.assertEqual((frozen_value['version'], frozen_value['configured_model'], frozen_value['expected_model']),
                         (2, OPUS, OPUS))
        self.assertEqual(frozen_value['qualification_sha256'], qmod.digest(artifact))
        self.assertIsNone(frozen_value['resolution_sha256'])
        self.assertEqual(frozen_value['selector'], {'kind': 'exact', 'model': OPUS})

    def test_frozen_qualified_expectation_is_immutable(self):
        artifact = self.qualified_room('qualified-immutable')
        sha = qmod.digest(artifact)
        self.review_turn(OPUS)
        directory, state = self.directory(), self.state()
        for change in ({'expected_model': 'claude-opus-5-6'}, {'expected_model': None},
                       {'qualification_sha256': 'f' * 64}, {'qualification_sha256': None},
                       {'resolution_sha256': sha}, {'configured_model': OPUS}, {'version': 1},
                       {'selector': {'kind': 'exact', 'model': OPUS}}, {'effort_basis': 'effective max'}):
            with self.subTest(change=sorted(change)):
                candidate = copy.deepcopy(state)
                candidate['requests']['spec_review']['engineering_resolution'].update(change)
                with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
                    em.check_request(directory, candidate, candidate['requests']['spec_review'])
        candidate = copy.deepcopy(state)
        candidate['requests']['spec_review']['engineering_resolution'].pop('qualification_sha256')
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            em.check_request(directory, candidate, candidate['requests']['spec_review'])
        tampered = copy.deepcopy(state)
        tampered['requests']['spec_review']['engineering_resolution']['expected_model'] = 'claude-opus-5-6'
        with self.assertRaisesRegex(ao.RoomError, 'contradicts its recorded engineering epoch'):
            ao_native_outcome.inspect(directory, tampered, tampered['requests']['spec_review'], self.source,
                                      self.fake.conversation('engineer'))

    def test_unqualified_family_epoch_refuses_dispatch_and_reports_readiness(self):
        artifact = self.artifact(families={'fable': {'expected_model': FABLE, 'source_ids': ['opus-family-doc']}})
        self.qualified_room('unqualified', artifact=artifact)
        epoch = em.current(self.directory(), self.state())
        self.assertIsNone(epoch['expected_model'])
        self.assertFalse(epoch['family_qualified'])
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual((summary['resolution']['status'], summary['resolution']['expected_model']),
                         ('unqualified', None))
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('spec_review')
        self.assertNotIn('spec_review', self.state()['requests'])


class QualificationAdmissionTests(QualificationFixture):
    """Admission evidence: verified bytes, executable floor and honest unobserved runtime facts."""

    def setUp(self):
        super().setUp()
        self.artifact_value = self.qualified_room('qualified-admission')
        self.sha = qmod.digest(self.artifact_value)

    def test_admission_returns_bound_evidence_and_verified_source_bytes(self):
        prepared = ao_delegates.preparation(self.directory(), self.state())
        evidence = qmod.admission(self.directory(), self.state(), prepared=prepared)
        self.assertEqual(evidence['family'], 'opus')
        self.assertEqual(evidence['qualification'],
                         {'sha256': self.sha, 'revision': 3, 'qualified_at': '2026-06-02T00:00:00Z',
                          'record': qmod.BASE + '/' + self.sha + '.json', 'expected_model': OPUS,
                          'minimum_claude_code_version': '2.1.280', 'source_ids': ['opus-family-doc']})
        self.assertEqual(evidence['scope'], qmod.SCOPE)
        path = self.root / 'evidence' / 'opus-family.txt'
        self.assertEqual(evidence['sources'],
                         [{'id': 'opus-family-doc', 'uri': 'https://docs.invalid.example/claude/opus-family',
                           'captured_at': '2026-06-01T00:00:00Z',
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}])
        self.assertNotIn('evidence_file', json.dumps(evidence['sources']))
        self.assertNotIn(str(self.root), json.dumps(evidence['sources']))
        self.assertIn('retained family qualification bytes', evidence['checked'])
        self.assertIn('user, managed, project and local Claude settings sources', evidence['checked'])
        self.assertIn('the actual provider that served the turn', evidence['runtime_unobserved'])
        self.assertEqual(evidence['executable']['version'], '2.1.282 (Claude Code)')
        self.assertIn('not provider availability', evidence['basis'])

    def test_admission_refuses_unmapped_family_below_floor_and_settings_remap(self):
        prepared = ao_delegates.preparation(self.directory(), self.state())
        with self.assertRaisesRegex(ao.RoomError, 'does not map family fable'):
            qmod.admission(self.directory(), self.state(), 'fable', prepared=prepared)
        forged = copy.deepcopy(prepared)
        forged['routing']['claude']['version'] = '2.1.268 (Claude Code)'
        with self.assertRaisesRegex(ao.RoomError, 'requires Claude Code 2.1.280 or newer'):
            qmod.admission(self.directory(), self.state(), prepared=forged)
        (self.claude_env / 'settings.json').write_text(
            json.dumps({'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-5'}}))
        with self.assertRaisesRegex(ao.RoomError, 'remaps a family worker alias'):
            qmod.admission(self.directory(), self.state(), prepared=prepared)
        (self.claude_env / 'settings.json').unlink()
        self.assertEqual(qmod.admission(self.directory(), self.state(), prepared=prepared)['family'], 'opus')


class QualificationMismatchTests(QualificationFixture):
    """Qualified native mismatch: structured actual identity, no fallback, no silent resolution."""

    def test_older_first_response_is_retained_with_its_source_and_never_falls_back(self):
        artifact = self.qualified_room('qualified-older')
        sha = qmod.digest(artifact)
        self.turn('spec_review', 'spec_review', self.verdict(), 'claude-opus-5')
        outcome = self.outcome('spec_review')
        native = outcome['native']
        self.assertEqual(native['observed_models'], ['claude-opus-5'])
        self.assertEqual(native['expected_model'], OPUS)
        self.assertEqual(native['qualification_sha256'], sha)
        self.assertEqual(native['stop_row_ids'], ['spec_review-reply-0'])
        self.assertEqual(native['model_contradiction'],
                         {'expected_model': OPUS, 'observed_models': ['claude-opus-5'],
                          'stop_row_ids': ['spec_review-reply-0'], 'qualification_sha256': sha,
                          'source_sha256': native['source_sha256']})
        self.assertEqual(native['source'], self.source)
        self.assertEqual(native['source_sha256'], ao.digest(self.transcript.read_bytes()))
        self.assertIn('contradicts the qualified expected model', native['unknown'])
        self.assertEqual(outcome['outcome']['kind'], 'unknown')
        self.assertEqual(em.current(self.directory(), self.state())['expected_model'], OPUS)
        self.assertIsNone(self.resolutions())
        before = files_under(self.directory())
        with self.assertRaisesRegex(ao.RoomError, 'Native semantic hold: unknown'):
            self.send('correction', 'fix-1')
        self.assertNotIn('fix-1', self.state()['requests'])
        after = files_under(self.directory())
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        self.assertFalse((self.directory() / RESOLUTIONS).exists())

    def test_two_observed_models_retain_both_root_row_identities(self):
        self.qualified_room('qualified-two')
        self.turn('spec_review', 'spec_review', self.verdict(), OPUS, 'claude-opus-5-6')
        native = self.outcome('spec_review')['native']
        self.assertEqual(native['observed_models'], [OPUS, 'claude-opus-5-6'])
        self.assertEqual(native['stop_row_ids'], ['spec_review-reply-0', 'spec_review-reply-1'])
        self.assertEqual(native['model_contradiction']['observed_models'], [OPUS, 'claude-opus-5-6'])
        self.assertEqual(self.outcome('spec_review')['outcome']['kind'], 'unknown')


class QualificationPinTests(QualificationFixture):
    """Pinned mappings, explicit audited boundaries and same-alias idempotence."""

    def test_audited_same_alias_update_is_authorized_by_its_committed_record_only(self):
        artifact = self.qualified_room('pinned-mapping')
        self.review_turn(OPUS)
        historical = copy.deepcopy(self.request('spec_review'))
        hold, bindings = copy.deepcopy(self.state()['requests']['spec_review']), copy.deepcopy(self.state()['bindings'])
        changed = self.artifact(families={'opus': {'expected_model': 'claude-opus-5-6',
                                                   'source_ids': ['opus-family-doc'],
                                                   'minimum_claude_code_version': '2.1.280'}})
        self.configure(changed)
        # Configuration alone never replaces the pinned expectation.
        self.assertEqual(em.current(self.directory(), self.state())['expected_model'], OPUS)
        self.assertEqual(self.state()[em.SELECTION_KEY]['family_qualification']['sha256'], qmod.digest(artifact))
        # The audited refresh unit retains the target's artifact before its commit.
        qmod.retain(self.directory(), qmod.snapshot({'artifact': changed, 'sha256': qmod.digest(changed)}))
        parts = self.parts(request_id='reset-opus', source_model='opus', target_model='opus')
        self.assertEqual(parts['intent']['target']['qualification']['execution_qualification']['expected_model'],
                         'claude-opus-5-6')
        self.assertIsNone(parts['intent']['patch'])
        self.assertIsNone(parts['marker'])
        self.publish(parts)
        directory, state = self.directory(), self.state()
        history = em.epochs(directory, state)
        self.assertEqual([(e['configured_model'], e['expected_model'], e['family_qualified']) for e in history],
                         [('opus', OPUS, True), ('opus', 'claude-opus-5-6', True)])
        self.assertEqual(history[-1]['qualification_sha256'], qmod.digest(changed))
        self.assertEqual(state['requests']['spec_review'], hold)
        self.assertEqual(state['bindings'], bindings)
        self.assertEqual(em.check_request(directory, state, state['requests']['spec_review'])['expected_model'], OPUS)
        self.assertEqual(self.request('spec_review'), historical)
        prepared = ao_delegates.preparation(directory, state)
        evidence = qmod.admission(directory, state, prepared=prepared)
        self.assertEqual((evidence['expected_model'], evidence['selector'], evidence['qualification']['sha256']),
                         ('claude-opus-5-6', FAMILY_OPUS, qmod.digest(changed)))
        audit = qmod.admission(directory, state, target='reset-opus', prepared=prepared)
        self.assertEqual((audit['expected_model'], audit['source']), ('claude-opus-5-6', 'committed_target'))

    def test_same_alias_replacement_without_a_qualified_target_is_refused(self):
        self.qualified_room('pinned-downgrade')
        downgraded = self.artifact(families={'fable': {'expected_model': FABLE, 'source_ids': ['opus-family-doc']}})
        self.configure(downgraded)
        parts = self.parts(request_id='reset-downgrade', source_model='opus', target_model='opus')
        self.assertIsNone(parts['intent']['target']['qualification'].get('execution_qualification'))
        self.assert_refused(parts, 'cannot silently replace')
        self.assertEqual(em.current(self.directory(), self.state())['expected_model'], OPUS)

    def test_same_alias_qualified_reset_opens_a_new_qualified_epoch_and_preserves_holds(self):
        artifact = self.qualified_room('qualified-reset')
        sha = qmod.digest(artifact)
        self.review_turn(OPUS)
        state = self.state()
        state['requests']['spec_review']['model_reroute'] = {'fromModel': 'opus', 'toModel': 'claude-opus-5',
                                                             'providerTurnId': 'p-turn', 'at': 1.0}
        ao.atomic(self.directory() / 'state.json', state)
        held, bindings = copy.deepcopy(self.state()['requests']['spec_review']), copy.deepcopy(state['bindings'])
        before = files_under(self.directory())
        # The room opened on this test's own root artifact while a fresh preparation selected a newer
        # worker artifact; the intended same-alias root target is selected again before the reset, so
        # both qualified epochs keep the same origin artifact and the same expected model.
        self.configure(artifact)
        parts = self.parts(request_id='reset-opus', source_model='opus', target_model='opus')
        self.assertIsNone(parts['intent']['patch'])
        self.assertIsNone(parts['marker'])
        self.publish(parts)
        directory, state = self.directory(), self.state()
        history = em.epochs(directory, state)
        self.assertEqual([(e['configured_model'], e['expected_model'], e['from_order'], e['family_qualified'])
                          for e in history], [('opus', OPUS, 1, True), ('opus', OPUS, 2, True)])
        self.assertEqual([e['qualification_sha256'] for e in history], [sha, sha])
        self.assertEqual(state['requests']['spec_review'], held)
        self.assertEqual(state['bindings'], bindings)
        self.assertEqual({k: v for k, v in files_under(directory).items() if k in before}, before)
        self.assertEqual(em.check_request(directory, state, state['requests']['spec_review'])['expected_model'], OPUS)
        self.assertIsNone(self.resolutions())
        self.assertIsNone(em.adopt_resolution(directory, state))


class QualificationRefusalTests(QualificationFixture):
    """Malformed, remapped, provider, scope and private-file refusals before any room exists."""

    def open_with(self, artifact, feature, path=None):
        self.configure(artifact, path=path)
        with self.assertRaises(ao.RoomError) as caught:
            self.open_model(feature, model='opus')
        self.assertFalse((self.home / 'ao' / 'rooms' / self.room_id_for(feature)).exists())
        return str(caught.exception)

    def test_malformed_provider_scope_and_floor_declarations_refuse(self):
        cases = [({'revision': 0}, 'positive integer'),
                 ({'qualified_at': '2026-06-02T00:00:00'}, 'UTC'),
                 ({'scope': {'provider_class': 'bedrock', 'alias_remaps': 'forbidden',
                             'availability': 'not_asserted'}}, 'scope'),
                 ({'scope': {'provider_class': 'anthropic_first_party', 'alias_remaps': 'allowed',
                             'availability': 'not_asserted'}}, 'scope'),
                 ({'scope': {'provider_class': 'anthropic_first_party', 'alias_remaps': 'forbidden',
                             'availability': 'asserted'}}, 'scope'),
                 ({'format': 'project_room_family_qualification_v2'}, 'Unsupported family qualification format'),
                 ({'extra': 1}, 'declares exactly format')]
        for index, (change, message) in enumerate(cases):
            with self.subTest(change=change):
                self.assertIn(message, self.open_with(self.artifact(**change), 'refused-%d' % index))
        families = {'haiku': {'expected_model': 'claude-haiku-4-5', 'source_ids': ['opus-family-doc']}}
        self.assertIn('bundled families', self.open_with(self.artifact(families=families), 'refused-family'))
        member = {'opus': {'expected_model': 'claude-sonnet-5', 'source_ids': ['opus-family-doc'],
                           'minimum_claude_code_version': '2.1.280'}}
        self.assertIn('exact identifier of that same family',
                      self.open_with(self.artifact(families=member), 'refused-member'))
        alias = {'opus': {'expected_model': 'opus', 'source_ids': ['opus-family-doc'],
                          'minimum_claude_code_version': '2.1.280'}}
        self.assertIn('exact identifier of that same family',
                      self.open_with(self.artifact(families=alias), 'refused-alias'))
        unknown = {'opus': {'expected_model': OPUS, 'source_ids': ['absent-doc'],
                            'minimum_claude_code_version': '2.1.280'}}
        self.assertIn('retained source descriptors',
                      self.open_with(self.artifact(families=unknown), 'refused-source'))
        weak = {'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                         'minimum_claude_code_version': '2.1.9'}}
        self.assertIn('never weakens a bundled compatibility floor',
                      self.open_with(self.artifact(families=weak), 'refused-weak'))
        removed = {'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc']}}
        self.assertIn('never removed', self.open_with(self.artifact(families=removed), 'refused-removed'))
        missing = self.artifact()
        missing['sources'][0]['evidence_file'] = str(self.root / 'evidence' / 'missing.txt')
        self.assertIn('unreadable', self.open_with(missing, 'refused-evidence'))
        credentials = self.artifact()
        credentials['sources'][0]['uri'] = 'https://user:token@docs.invalid.example/claude/opus-family'
        self.assertIn('https documentation source', self.open_with(credentials, 'refused-uri'))

    def test_artifact_and_evidence_symlinks_changes_and_digest_mismatch_refuse(self):
        artifact = self.artifact()
        real = self.configure(artifact)
        link = self.root / 'linked-artifact.json'
        link.symlink_to(real)
        self.assertIn('symlink', self.open_with(artifact, 'refused-link', path=link))
        config = ao.read(self.home / 'ao' / 'config.json')
        config['family_qualification'] = {'path': str(real), 'sha256': 'f' * 64}
        ao.atomic(self.home / 'ao' / 'config.json', config)
        with self.assertRaisesRegex(ao.RoomError, 'does not match its configured digest'):
            self.open_model('refused-digest', model='opus')
        self.assertFalse((self.home / 'ao' / 'rooms' / self.room_id_for('refused-digest')).exists())
        shadow = self.root / 'evidence' / 'shadow.txt'
        shadow.write_text('shadow evidence\n')
        linked = self.root / 'evidence' / 'linked.txt'
        linked.symlink_to(shadow)
        symlinked = self.artifact()
        symlinked['sources'][0]['evidence_file'] = str(linked)
        symlinked['sources'][0]['sha256'] = hashlib.sha256(shadow.read_bytes()).hexdigest()
        self.assertIn('symlink', self.open_with(symlinked, 'refused-evidence-link'))
        changed = self.artifact()
        (self.root / 'evidence' / 'opus-family.txt').write_text('changed evidence\n')
        self.assertIn('does not match its retained SHA256',
                      self.open_with(changed, 'refused-evidence-change'))


class SourcePreflightTests(FamilyFixture):
    """Pre-dispatch ownership preflight and source registration without any observed response."""

    def setUp(self):
        super().setUp()
        self.room = self.open_model('preflight')['room_id']
        self.spec()
        # The room's own root epoch stays unqualified; the worker preparation is the only consumer
        # of the temporarily selected qualified artifact.
        with self.worker_preparation():
            self.bind_at('fable')
        self.configure_native()

    def test_preflight_registers_an_existing_identical_source_without_replacing_it(self):
        state = self.state()
        evidence = ao_native_outcome.preflight_source(self.directory(), state, self.source['database'],
                                                      self.source['transcript'])
        self.assertEqual(evidence['registered'], 'identical')
        self.assertEqual(evidence['source'], self.source)
        self.assertEqual(state['native_outcome_source'], self.source)
        self.assertTrue(evidence['transcript_present'])
        self.assertEqual(evidence['native_owner']['provider_conversation_id'], self.NATIVE)
        self.assertIn('unobserved', evidence['basis'])

    def test_preflight_creates_the_binding_and_refuses_conflicts_and_unsafe_paths(self):
        state = self.state()
        state.pop('native_outcome_source')
        ao.atomic(self.directory() / 'state.json', state)
        state = self.state()
        evidence = ao_native_outcome.preflight_source(self.directory(), state, self.source['database'],
                                                      self.source['transcript'])
        self.assertEqual(evidence['registered'], 'created')
        self.assertEqual(state['native_outcome_source'], self.source)
        ao.atomic(self.directory() / 'state.json', state)  # the caller persists the registered binding
        conflict = self.root / 'other-owner.db'
        with self.assertRaisesRegex(ao.RoomError, 'never replaced'):
            ao_native_outcome.preflight_source(self.directory(), self.state(), str(conflict),
                                               self.source['transcript'])
        self.owner = {**self.owner, 'active_branch_id': 'other-branch'}
        with self.assertRaisesRegex(ao.RoomError, 'conversation or branch'):
            ao_native_outcome.preflight_source(self.directory(), self.state(), self.source['database'],
                                               self.source['transcript'])
        self.owner = {**self.owner, 'active_branch_id': 'root'}
        target = self.root / 'elsewhere.jsonl'
        target.write_text('')
        self.transcript.unlink()
        self.transcript.symlink_to(target)
        with self.assertRaisesRegex(ao.RoomError, 'without symlinks'):
            ao_native_outcome.preflight_source(self.directory(), self.state(), self.source['database'],
                                               str(self.transcript))
        self.transcript.unlink()
        self.write_native()
        wrong = self.root / 'other-name.jsonl'
        with self.assertRaisesRegex(ao.RoomError, 'provider conversation identity'):
            ao_native_outcome.preflight_source(self.directory(), self.state(), self.source['database'], str(wrong))
        with patch('ao_native_identity.read_owner', side_effect=ValueError('missing owner')):
            with self.assertRaisesRegex(ao.RoomError, 'prospective native outcome owner'):
                ao_native_outcome.preflight_source(self.directory(), self.state(), self.source['database'],
                                                   self.source['transcript'])


class QualifiedSourceBindingTests(QualificationFixture):
    """Qualified dispatch requires the verifiable prospective source binding."""

    def test_qualified_dispatch_requires_the_registered_source(self):
        artifact = self.qualified_room('qualified-source')
        sha = qmod.digest(artifact)
        state = self.state()
        state.pop('native_outcome_source')
        ao.atomic(self.directory() / 'state.json', state)
        with self.assertRaisesRegex(ao.RoomError, 'prospective native outcome source'):
            self.send('spec_review')
        self.assertNotIn('spec_review', self.state()['requests'])
        state = self.state()
        evidence = ao_native_outcome.preflight_source(self.directory(), state, self.source['database'],
                                                      self.source['transcript'])
        self.assertEqual(evidence['registered'], 'created')
        ao.atomic(self.directory() / 'state.json', state)  # the caller persists the mutated preflight state
        self.send('spec_review')
        request = self.request('spec_review')
        self.assertEqual(request['engineering_resolution']['qualification_sha256'], sha)
        self.assertEqual(request['engineering_resolution']['expected_model'], OPUS)


class HistoricalVersionTwoTests(QualificationFixture):
    """A pinned version 2 room never picks up a later private qualification pointer."""

    def test_pinned_version_two_room_ignores_a_later_qualification_pointer(self):
        self.room = self.open_model('v2-room')['room_id']
        self.spec()
        # The root epoch stays the unqualified version 2 selection recorded at open; only the worker
        # preparation temporarily selects its own qualified artifact, and the pointer is restored.
        with self.worker_preparation(self.worker_artifact()):
            self.bind_at('fable')
        self.configure_native()
        self.configure(self.artifact())
        self.assertEqual(self.state()[em.SELECTION_KEY]['version'], 2)
        self.assertNotIn(em.QUALIFICATION_KEY, self.state()[em.SELECTION_KEY])
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('spec_review')  # a new family dispatch requires a pre-inference qualification
        self.assertNotIn('spec_review', self.state()['requests'])
        self.historical_review(FABLE)  # the pre-correction record is preserved exactly
        historical = copy.deepcopy(self.request('spec_review'))
        self.assertNotIn('version', historical['engineering_resolution'])
        self.assertEqual(self.outcome('spec_review')['native']['observed_models'], [FABLE])
        epoch = em.current(self.directory(), self.state())
        self.assertIsNone(epoch['qualification_sha256'])
        self.assertFalse(epoch['family_qualified'])
        pointer = em.adopt_resolution(self.directory(), self.state())
        self.assertEqual(pointer['path'], RESOLUTIONS + '/spec_review.json')
        chain = em.journal(self.directory(), self.state())
        self.assertEqual(chain['pending_resolution']['sha256'], pointer['sha256'])
        self.assertEqual(chain['resolutions'], [])
        self.assertIsNone(em.current(self.directory(), self.state())['expected_model'])
        self.assertEqual(self.request('spec_review'), historical)


class ResolutionDurabilityTests(FamilyFixture):
    """The reproduced pending-resolution publication barrier."""

    def setUp(self):
        super().setUp()
        self.open_room('durable', 'opus')

    def test_interrupted_pending_adoption_republishes_its_ancestors_before_claiming(self):
        self.historical_review(OPUS)
        directory = self.directory()
        record = directory / RESOLUTIONS / 'spec_review.json'
        real, calls, interrupted = em._fsync, [], [False]

        def injected(path, folder=False):
            calls.append(str(Path(path).relative_to(directory)))
            if folder and Path(path) == directory and record.exists() and not interrupted[0]:
                interrupted[0] = True
                raise OSError('synthetic room-directory fsync failure')
            return real(path, folder)

        with patch.object(em, '_fsync', injected):
            with self.assertRaises(OSError):
                em.adopt_resolution(directory, self.state())
            self.assertTrue(interrupted[0])
            self.assertTrue(record.exists())
            raw = record.read_bytes()
            self.assertNotIn(em.RESOLUTIONS_KEY, self.state())
            chain = em.journal(directory, self.state())
            self.assertEqual(chain['pending_resolution']['path'], RESOLUTIONS + '/spec_review.json')
            self.assertEqual(chain['resolutions'], [])
            # The identical retry runs inside the same spy scope and republishes the record's own
            # ancestors, the journal base and the room directory before the pointer is claimed.
            calls.clear()
            state = self.state()
            pointer = em.adopt_resolution(directory, state)
        republished = list(calls)
        self.assertEqual(pointer['sha256'], chain['pending_resolution']['sha256'])
        self.assertIn(RESOLUTIONS + '/spec_review.json', republished)
        self.assertIn(RESOLUTIONS, republished)
        self.assertIn(em.BASE, republished)
        self.assertIn('.', republished)
        self.assertEqual(record.read_bytes(), raw)
        self.assertEqual(state[em.RESOLUTIONS_KEY], [pointer])


if __name__ == '__main__':
    unittest.main()
