"""Regression cases for the qualification foundation correction.

Covers retained room evidence, epoch/target admission, the new-dispatch guard, full prospective
owner/source verification and historical policy reading. Synthetic private files only: no model,
provider, network or live service call is made, and nothing here was executed while writing it.
"""
import copy
import os
import stat
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_model_qualification as qmod
import ao_project_room as ao
from test_ao_engineering_family import FAMILY_OPUS, FamilyFixture
from test_ao_engineering_model import FABLE, OPUS
from test_ao_model_qualification import QualificationFixture


class RetainedEvidenceTests(QualificationFixture):
    """Defect 1: the room keeps the complete selected capture, not a reference to private files."""

    def test_retention_survives_the_private_originals_and_verifies_retained_bytes(self):
        artifact = self.qualified_room('retained-survives')
        directory, state = self.directory(), self.state()
        snapshot = state[em.SELECTION_KEY]['family_qualification']
        entry = snapshot['evidence'][0]
        retained = directory / entry['record']
        self.assertEqual(retained.read_bytes(), (self.root / 'evidence' / 'opus-family.txt').read_bytes())
        # The private originals are deleted; the room proof is complete on its own.
        (self.root / 'evidence' / 'opus-family.txt').unlink()
        (self.root / 'qualification.json').unlink()
        self.assertEqual(em.initial(directory, state), em.initial(directory, self.state()))
        prepared = ao_delegates.preparation(directory, state)
        admission = qmod.admission(directory, state, prepared=prepared)
        self.assertEqual(admission['qualification']['sha256'], snapshot['sha256'])
        self.assertEqual(admission['sources'][0]['bytes'], retained.stat().st_size)
        # Changed, missing and symlinked retained bytes refuse.
        raw = retained.read_bytes()
        retained.write_bytes(b'changed\n')
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.write_bytes(raw)
        em.initial(directory, self.state())
        retained.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.symlink_to(self.root / 'qualification.json')
        with self.assertRaisesRegex(ao.RoomError, 'inconsistent with its pinned policy'):
            em.initial(directory, self.state())
        retained.unlink()
        retained.write_bytes(raw)
        em.initial(directory, self.state())

    def test_retention_copies_only_the_selected_capture_and_repeats_idempotently(self):
        artifact = self.qualified_room('retained-only')
        directory = self.directory()
        snapshot = qmod.snapshot({'artifact': artifact, 'sha256': qmod.digest(artifact)})
        entries = qmod.evidence_entries(artifact, snapshot['sha256'])
        self.assertEqual(sorted(entries, key=lambda item: item['record']), snapshot['evidence'])
        # The room legitimately retains two independently selected artifact trees: the root epoch's
        # own artifact and the newer worker snapshot the fresh preparation selected. The selected
        # exact union, and nothing unrelated, is what retention wrote.
        worker = self.worker_artifact()
        worker_snapshot = qmod.snapshot({'artifact': worker, 'sha256': qmod.digest(worker)})
        expected = sorted([qmod.record_path(snapshot['sha256'])] + [item['record'] for item in entries]
                          + [qmod.record_path(worker_snapshot['sha256'])]
                          + [item['record'] for item in worker_snapshot['evidence']])
        retained = sorted(str(path.relative_to(directory)) for path in (directory / qmod.BASE).rglob('*')
                          if path.is_file())
        self.assertEqual(retained, expected)
        # Repeated identical retention republishes durability and needs no private original.
        for path in (self.root / 'evidence').iterdir():
            path.unlink()
        self.assertEqual(qmod.retain(directory, snapshot), snapshot)
        self.assertEqual(sorted(str(path.relative_to(directory)) for path in (directory / qmod.BASE).rglob('*')
                                if path.is_file()), expected)
        observed = []
        real = os.fsync

        def recording(fd):
            observed.append('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real(fd)

        with patch.object(os, 'fsync', recording):
            self.assertEqual(qmod.retain(directory, snapshot), snapshot)
        self.assertTrue(observed and observed[0] == 'file')
        self.assertGreaterEqual(observed.count('dir'), 6)


class DispatchGuardTests(FamilyFixture):
    """Defect 4: no new family dispatch without a pre-inference qualified expectation."""

    def test_default_family_room_refuses_new_dispatch_with_actionable_readiness(self):
        self.room = self.open_model('guard-default')['room_id']
        self.spec()
        with self.worker_preparation():
            self.bind_at('fable')
        record = self.state()[em.SELECTION_KEY]
        self.assertEqual((record['version'], record['selector']), (2, {'kind': 'family', 'family': 'fable'}))
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('spec_review')
        self.assertNotIn('spec_review', self.state()['requests'])
        summary = self.service.ao_room_status(self.room)['engineering_model']
        self.assertEqual(summary['resolution']['status'], 'unverified')

    def test_historical_room_next_request_is_refused_but_readers_stay_unchanged(self):
        self.open_room('guard-history', 'opus')
        self.historical_review(OPUS)                 # a saved pre-correction completed turn
        historical = copy.deepcopy(self.request('spec_review'))
        self.assertEqual(self.outcome('spec_review')['native']['observed_models'], [OPUS])
        with self.assertRaisesRegex(ao.RoomError, 'supported audited qualification refresh'):
            self.send('correction', 'new-turn')
        self.assertNotIn('new-turn', self.state()['requests'])
        self.assertEqual(self.request('spec_review'), historical)
        self.assertEqual(em.check_request(self.directory(), self.state(), historical)['configured_model'], 'opus')

    def test_exact_identifier_compatibility_still_dispatches(self):
        self.room = self.open_model('guard-exact', model=FABLE)['room_id']
        self.spec()
        with self.worker_preparation():
            self.bind_at(FABLE)
        self.send('spec_review')
        self.assertEqual(self.request('spec_review')['engineering_resolution'],
                         {'selector': {'kind': 'exact', 'model': FABLE}, 'configured_model': FABLE,
                          'expected_model': FABLE, 'resolution_sha256': None,
                          'effort_basis': 'configured max intent; effective effort not evidenced'})


class EpochAdmissionTests(QualificationFixture):
    """Defect 2: admission follows the exact epoch's own artifact and named committed targets."""

    def test_a_legacy_version_two_room_admits_its_committed_qualified_epoch(self):
        self.install_claude('claude-qualified', '2.1.282 (Claude Code)')
        self.room = self.open_model('legacy-qualified', model='opus')['room_id']
        self.spec()
        # The legacy root epoch keeps its unqualified version 2 opening record; the preparation
        # selects its own qualified worker artifact only for the duration of the bind.
        with self.worker_preparation():
            self.bind_at('opus')
        self.configure_native()
        artifact = self.artifact()
        self.configure(artifact)
        qmod.retain(self.directory(), qmod.snapshot({'artifact': artifact, 'sha256': qmod.digest(artifact)}))
        prepared = ao_delegates.preparation(self.directory(), self.state())
        with self.assertRaisesRegex(ao.RoomError, 'no operator-selected family qualification'):
            qmod.admission(self.directory(), self.state(), prepared=prepared)
        parts = self.parts(request_id='to-qualified', source_model='opus', target_model='opus')
        self.publish(parts)
        directory, state = self.directory(), self.state()
        history = em.epochs(directory, state)
        self.assertEqual([(e['configured_model'], e['expected_model'], e['family_qualified']) for e in history],
                         [('opus', None, False), ('opus', OPUS, True)])
        self.assertEqual(state[em.SELECTION_KEY]['version'], 2)   # the opening record is unchanged
        prepared = ao_delegates.preparation(directory, state)
        admission = qmod.admission(directory, state, prepared=prepared)
        self.assertEqual((admission['family'], admission['expected_model'], admission['selector']),
                         ('opus', OPUS, FAMILY_OPUS))
        self.assertEqual(admission['qualification']['sha256'], qmod.digest(artifact))
        self.assertEqual(admission['source'], 'current_epoch')
        audit = qmod.admission(directory, state, target='to-qualified', prepared=prepared)
        self.assertEqual((audit['expected_model'], audit['source']), (OPUS, 'committed_target'))
        with self.assertRaisesRegex(ao.RoomError, 'not a committed record'):
            qmod.admission(directory, state, target='never-committed', prepared=prepared)


class OwnerVerificationTests(QualificationFixture):
    """Defect 5: qualified freeze verifies the full prospective owner and source before send."""

    def setUp(self):
        super().setUp()
        self.qualified_room('qualified-owner')

    def test_full_owner_and_prospective_path_are_verified_before_send(self):
        directory, state = self.directory(), self.state()
        self.assertEqual(em.freeze_request(directory, state)['expected_model'], OPUS)
        original = copy.deepcopy(self.owner)
        for change in ({'ao_conversation_id': 'other-conversation'}, {'active_branch_id': 'other-branch'},
                       {'workspace_path': str(self.root / 'other-worktree')},
                       {'provider_conversation_id': 'other-native'}):
            with self.subTest(change=sorted(change)):
                self.owner = {**original, **change}
                with self.assertRaisesRegex(ao.RoomError, 'prospective native outcome source'):
                    em.freeze_request(directory, state)
        self.owner = copy.deepcopy(original)
        for change in ({'transcript': str(self.root / 'wrong-name.jsonl')},
                       {'native_session_id': 'other-native'}):
            changed = copy.deepcopy(state)
            changed['native_outcome_source'] = {**changed['native_outcome_source'], **change}
            with self.subTest(change=sorted(change)):
                with self.assertRaisesRegex(ao.RoomError, 'prospective native outcome source'):
                    em.freeze_request(directory, changed)
        missing = copy.deepcopy(state)
        missing.pop('native_outcome_source')
        with self.assertRaisesRegex(ao.RoomError, 'prospective native outcome source'):
            em.freeze_request(directory, missing)

    def test_a_genuinely_prospective_transcript_may_not_exist_yet(self):
        directory, state = self.directory(), self.state()
        raw = self.transcript.read_bytes()
        self.transcript.unlink()
        try:
            self.assertEqual(em.freeze_request(directory, state)['expected_model'], OPUS)
        finally:
            self.transcript.write_bytes(raw)


class HistoricalPolicyTests(QualificationFixture):
    """Defect 7: later bundled floors cannot damage preserved evidence but block new readiness."""

    def test_a_later_bundled_floor_keeps_history_readable_and_blocks_readiness(self):
        self.qualified_room('later-floor')
        self.review_turn(OPUS)
        directory, state = self.directory(), self.state()
        before = em.initial(directory, state)
        request = copy.deepcopy(state['requests']['spec_review'])
        bundled = {**em.BUNDLED_FAMILIES['opus'], 'minimum_claude_code_version': '2.1.300'}
        with patch.dict(em.BUNDLED_FAMILIES, {'opus': bundled}):
            # Preserved history still reads: the recorded policy is validated against its own snapshot.
            self.assertEqual(em.initial(directory, state), before)
            self.assertEqual(em.check_request(directory, state, request)['expected_model'], OPUS)
            self.assertEqual([e['expected_model'] for e in em.epochs(directory, state)], [OPUS])
            # A newer bundled floor blocks new readiness without touching the record.
            prepared = ao_delegates.preparation(directory, state)
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                em.require_executable(directory, state, prepared)
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                qmod.admission(directory, state, prepared=prepared)
            # Current creation rules may refuse the older artifact outright.
            with self.assertRaisesRegex(ao.RoomError, 'never weakens a bundled compatibility floor'):
                em.effective_policy(self.service.root)
        self.assertEqual(em.initial(directory, state), before)


class AdmissionReadinessTests(QualificationFixture):
    """Defect 8 and the malformed read-only artifact type checks."""

    def test_admission_without_a_supported_preparation_is_bounded(self):
        self.configure(self.artifact())
        self.room = self.open_model('unprepared-admission', model='opus')['room_id']
        with self.assertRaisesRegex(ao.RoomError, 'supported preparation'):
            qmod.admission(self.directory(), self.state())

    def test_admission_loads_the_room_preparation_instead_of_guessing(self):
        self.qualified_room('loaded-admission')
        evidence = qmod.admission(self.directory(), self.state())
        self.assertEqual((evidence['family'], evidence['expected_model']), ('opus', OPUS))

    def test_unhashable_source_ids_are_a_clean_structural_refusal(self):
        for member in ({'nested': 'member'}, ['nested'], 7):
            artifact = self.artifact()
            artifact['families']['opus']['source_ids'] = [member]
            with self.subTest(member=member):
                with self.assertRaisesRegex(ao.RoomError, 'retained source descriptors'):
                    qmod.validate_qualification(artifact)


class QualificationReferenceTests(QualificationFixture):
    """A1: a historical no-capture snapshot keeps its own reference and admission keeps refusing."""

    def test_historical_snapshot_reports_only_its_artifact_and_refuses_admission(self):
        artifact = self.qualified_room('historical-reference')
        directory, state = self.directory(), self.state()
        prepared = ao_delegates.preparation(directory, state)  # the room's own supported preparation
        sha = qmod.digest(artifact)
        snapshot = copy.deepcopy(state[em.SELECTION_KEY]['family_qualification'])
        historical = {key: snapshot[key] for key in ('artifact', 'sha256', 'record')}
        state[em.SELECTION_KEY]['family_qualification'] = historical
        ao.atomic(directory / 'state.json', state)
        self.assertEqual(sorted(historical), ['artifact', 'record', 'sha256'])
        # The authentic historical reader still accepts it and reports that it holds no capture.
        verified = qmod.read_retained(directory, historical, 'synthetic reference regression')
        self.assertFalse(verified['retained_evidence'])
        self.assertEqual(em.initial(directory, self.state())['family_qualification'], verified)
        # The epoch exposes its own artifact reference and invents no capture metadata.
        context = em.qualification_context(directory, self.state())
        self.assertEqual(context['reference'], {'sha256': sha, 'record': qmod.record_path(sha)})
        refusal = 'holds no retained source bytes; a supported audited qualification refresh'
        # Case one: byte-identical captures for this digest are retained in the room by another
        # selection of the same artifact tree; they are not this snapshot's retained evidence.
        capture = directory / snapshot['evidence'][0]['record']
        self.assertTrue(capture.is_file())
        raw = capture.read_bytes()
        with self.assertRaisesRegex(ao.RoomError, refusal):
            qmod.admission(directory, self.state(), prepared=prepared)
        self.assertTrue(capture.is_file())  # the refusal is about the claim, not missing bytes
        # Case two: those captures are absent; the same actionable refusal remains.
        capture.unlink()
        try:
            with self.assertRaisesRegex(ao.RoomError, refusal):
                qmod.admission(directory, self.state(), prepared=prepared)
        finally:
            capture.write_bytes(raw)
            capture.chmod(0o600)

    def test_modern_snapshot_declares_its_captures_and_admits(self):
        artifact = self.qualified_room('modern-reference')
        directory, state = self.directory(), self.state()
        sha = qmod.digest(artifact)
        snapshot = copy.deepcopy(state[em.SELECTION_KEY]['family_qualification'])
        self.assertEqual(sorted(snapshot), ['artifact', 'evidence', 'record', 'sha256'])
        verified = qmod.read_retained(directory, snapshot, 'synthetic reference regression')
        self.assertTrue(verified['retained_evidence'])
        reference = em.qualification_context(directory, state)['reference']
        self.assertEqual(reference['evidence'], snapshot['evidence'])
        prepared = ao_delegates.preparation(directory, state)
        evidence = qmod.admission(directory, state, prepared=prepared)
        self.assertEqual((evidence['source'], evidence['family'], evidence['expected_model']),
                         ('current_epoch', 'opus', OPUS))
        self.assertEqual(evidence['qualification']['sha256'], sha)
        self.assertIn('retained source evidence bytes', evidence['checked'])

    def test_two_field_policy_and_committed_target_keep_deriving_their_captures(self):
        artifact = self.qualified_room('two-field-reference')
        directory, state = self.directory(), self.state()
        sha = qmod.digest(artifact)
        entries = qmod.evidence_entries(artifact, sha)
        self.assertEqual(em._qualification_reference({'artifact': artifact, 'sha256': sha}, sha),
                         {'sha256': sha, 'record': qmod.record_path(sha), 'evidence': entries})
        # A committed same-alias reset target pins the same two-field policy qualification.
        self.configure(artifact)
        parts = self.parts(request_id='reset-opus', source_model='opus', target_model='opus')
        self.assertIsNone(parts['intent']['patch'])
        self.assertEqual(parts['intent']['target']['policy']['family_qualification'],
                         {'artifact': artifact, 'sha256': sha})
        self.publish(parts)
        state = self.state()
        context = em.qualification_context(directory, state, 'reset-opus')
        self.assertEqual(context['source'], 'committed_target')
        self.assertEqual(context['reference'], {'sha256': sha, 'record': qmod.record_path(sha),
                                                 'evidence': entries})
        prepared = ao_delegates.preparation(directory, state)
        evidence = qmod.admission(directory, state, target='reset-opus', prepared=prepared)
        self.assertEqual((evidence['source'], evidence['expected_model']), ('committed_target', OPUS))
        self.assertEqual(evidence['qualification']['sha256'], sha)


if __name__ == '__main__':
    unittest.main()
