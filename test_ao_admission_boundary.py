"""Foundation3 admission boundary regressions; synthetic offline evidence only.

These cases exercise the lower explicit qualification admission helper, the structural target
context helper, the strengthened current-floor enforcement and the read-only configuration
boundary. Nothing here calls a model, provider, network or live service, and no test globally
disables an identity guard.
"""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import ao_delegates
import ao_engineering_model as em
import ao_executable_binding as binding
import ao_model_qualification as qmod
import ao_project_room as ao
import ao_routing
from test_ao_engineering_model import FABLE, OPUS, files_under
from test_ao_model_qualification import QualificationFixture


class AdmissionBoundaryRegressionTests(QualificationFixture):
    """The lower helper and the structural target context, without preparation or root state."""

    def executable(self, version='2.1.282 (Claude Code)'):
        return {'path': '/synthetic/claude', 'size': 1, 'mtime_ns': 2, 'version': version, 'error': None}

    def test_pending_engineering_target_before_any_record_or_preparation(self):
        artifact = self.artifact()
        qualification = {'artifact': artifact, 'sha256': qmod.digest(artifact)}
        with patch.object(ao_delegates, 'preparation', side_effect=AssertionError('preparation must not run')), \
                patch.object(ao_routing, 'validate_local', side_effect=AssertionError('validate_local must not run')), \
                patch.object(binding, 'effective', side_effect=AssertionError('executable binding must not run')), \
                patch.object(qmod, 'admission', side_effect=AssertionError('convenience admission must not run')):
            evidence = qmod.admit_qualification(
                qualification, 'opus', executable=self.executable(), configuration={},
                origin={'context': 'pending_engineering_target', 'request_id': 'pending-opus',
                        'digest': qmod.digest(artifact)})
        self.assertEqual(evidence['proof'], 'private_prospective')
        self.assertEqual(evidence['expected_model'], OPUS)
        self.assertEqual(evidence['qualification']['minimum_claude_code_version'], '2.1.280')
        self.assertEqual(evidence['qualification']['effective_minimum_claude_code_version'], '2.1.280')
        self.assertFalse((self.home / 'ao' / 'rooms').exists())

    def test_independent_worker_qualification_leaves_exact_root_history_unchanged(self):
        self.room = self.open_model('exact-root', model=FABLE)['room_id']
        self.spec()
        # An exact root epoch pins no family qualification of its own; the modern fresh preparation
        # still selects the operator qualification for both enabled worker families, so an
        # independent, newer worker-capable artifact is configured for it while the exact root
        # history stays exactly as it was opened.
        worker = self.worker_artifact()
        self.configure(worker)
        self.bind_at(FABLE)
        before = files_under(self.directory())
        artifact = self.artifact()
        evidence = qmod.admit_qualification(
            {'artifact': artifact, 'sha256': qmod.digest(artifact)}, 'opus',
            executable=self.executable(), configuration={'agents': ao_routing.FAMILY_AGENTS},
            origin={'context': 'pending_routing_snapshot', 'request_id': 'worker-opus'})
        self.assertEqual(evidence['expected_model'], OPUS)
        self.assertEqual(evidence['configuration']['agents'], ao_routing.FAMILY_AGENTS)
        self.assertEqual(files_under(self.directory()), before)
        prepared = ao_delegates.preparation(self.directory(), self.state())
        self.assertEqual(prepared['routing']['worker_qualification']['snapshot']['sha256'], qmod.digest(worker))
        self.assertGreater(worker['revision'], artifact['revision'])

    def test_private_and_retained_proof_are_distinguished_without_fallback(self):
        artifact = self.qualified_room('proof-boundary')
        sha = qmod.digest(artifact)
        snapshot = self.state()[em.SELECTION_KEY]['family_qualification']
        private = qmod.admit_qualification({'artifact': artifact, 'sha256': sha}, 'opus', directory=None,
                                           executable=self.executable(), configuration={},
                                           origin={'context': 'pending_engineering_target'})
        self.assertEqual(private['proof'], 'private_prospective')
        retained = qmod.admit_qualification(snapshot, 'opus', directory=self.directory(),
                                            executable=self.executable(), configuration={},
                                            origin={'context': 'current_epoch'})
        self.assertEqual(retained['proof'], 'retained')
        self.assertEqual(retained['qualification']['sha256'], sha)
        retained_path = self.directory() / snapshot['evidence'][0]['record']
        retained_path.unlink()
        self.assertTrue((self.root / 'evidence' / 'opus-family.txt').exists())
        with self.assertRaisesRegex(ao.RoomError, 'retained evidence'):
            qmod.admit_qualification(snapshot, 'opus', directory=self.directory(),
                                     executable=self.executable(), configuration={},
                                     origin={'context': 'current_epoch'})

    def test_missing_or_changed_selected_private_source_refuses(self):
        artifact = self.artifact()
        qualification = {'artifact': artifact, 'sha256': qmod.digest(artifact)}
        path = self.root / 'evidence' / 'opus-family.txt'
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'unreadable'):
            qmod.admit_qualification(qualification, 'opus', executable=self.executable(), configuration={},
                                     origin={'context': 'pending_engineering_target'})
        path.write_bytes(original + b'changed\n')
        with self.assertRaisesRegex(ao.RoomError, 'does not match its retained SHA256'):
            qmod.admit_qualification(qualification, 'opus', executable=self.executable(), configuration={},
                                     origin={'context': 'pending_engineering_target'})
        path.write_bytes(original)

    def test_missing_or_changed_retained_capture_refuses_without_private_fallback(self):
        self.qualified_room('retained-change')
        snapshot = self.state()[em.SELECTION_KEY]['family_qualification']
        retained = self.directory() / snapshot['evidence'][0]['record']
        original = retained.read_bytes()
        retained.write_bytes(b'changed\n')
        with self.assertRaisesRegex(ao.RoomError, 'retained evidence'):
            qmod.admit_qualification(snapshot, 'opus', directory=self.directory(),
                                     executable=self.executable(), configuration={},
                                     origin={'context': 'current_epoch'})
        retained.write_bytes(original)
        retained.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'retained evidence'):
            qmod.admit_qualification(snapshot, 'opus', directory=self.directory(),
                                     executable=self.executable(), configuration={},
                                     origin={'context': 'current_epoch'})
        retained.write_bytes(original)

    def test_target_context_rejects_a_frozen_entry_that_disagrees_with_its_artifact(self):
        self.qualified_room('target-mismatch')
        parts = self.parts(request_id='target-mismatch', source_model='opus', target_model='opus')
        target = copy.deepcopy(parts['intent']['target'])
        target['qualification']['execution_qualification']['expected_model'] = 'claude-opus-5-6'
        with self.assertRaisesRegex(ao.RoomError, 'Unclaimed, missing or inconsistent'):
            em.qualification_context_for_target(target)

    def test_null_and_raised_current_floors_bind_the_lower_helper(self):
        base = self.artifact()
        fable = {**base, 'families': {'fable': {'expected_model': FABLE, 'source_ids': ['opus-family-doc']}}}
        qualification = {'artifact': fable, 'sha256': qmod.digest(fable)}
        bundled = {**em.BUNDLED_FAMILIES['fable'], 'minimum_claude_code_version': '2.1.300'}
        with patch.dict(em.BUNDLED_FAMILIES, {'fable': bundled}):
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                qmod.admit_qualification(qualification, 'fable', executable=self.executable('2.1.282 (Claude Code)'),
                                         configuration={}, origin={'context': 'pending_engineering_target'})
            evidence = qmod.admit_qualification(qualification, 'fable', executable=self.executable('2.1.300 (Claude Code)'),
                                                configuration={}, origin={'context': 'pending_engineering_target'})
            self.assertIsNone(evidence['qualification']['minimum_claude_code_version'])
            self.assertEqual(evidence['qualification']['effective_minimum_claude_code_version'], '2.1.300')
        raised = {**base, 'families': {'fable': {'expected_model': FABLE, 'source_ids': ['opus-family-doc'],
                                                 'minimum_claude_code_version': '2.1.280'}}}
        raised_qualification = {'artifact': raised, 'sha256': qmod.digest(raised)}
        with patch.dict(em.BUNDLED_FAMILIES, {'fable': bundled}):
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                qmod.admit_qualification(raised_qualification, 'fable', executable=self.executable('2.1.282 (Claude Code)'),
                                         configuration={}, origin={'context': 'pending_engineering_target'})
            evidence = qmod.admit_qualification(raised_qualification, 'fable', executable=self.executable('2.1.300 (Claude Code)'),
                                                configuration={}, origin={'context': 'pending_engineering_target'})
            self.assertEqual(evidence['qualification']['minimum_claude_code_version'], '2.1.280')
            self.assertEqual(evidence['qualification']['effective_minimum_claude_code_version'], '2.1.300')

    def test_admission_convenience_enforces_a_new_bundled_floor_on_the_current_root(self):
        artifact = self.artifact(families={'fable': {'expected_model': FABLE, 'source_ids': ['opus-family-doc']}})
        self.qualified_room('new-floor-root', artifact=artifact, version='2.1.282', model='fable')
        prepared = ao_delegates.preparation(self.directory(), self.state())
        routing = prepared['routing']
        # The root epoch pins the fable-only artifact; the workers keep an independent, newer snapshot.
        self.assertEqual(routing['worker_qualification']['snapshot']['sha256'], qmod.digest(self.worker_artifact()))
        self.assertGreater(self.worker_artifact()['revision'], artifact['revision'])
        self.assertEqual(em.current(self.directory(), self.state())['qualification_sha256'], qmod.digest(artifact))
        evidence = qmod.admission(self.directory(), self.state(), prepared=prepared)
        self.assertEqual((evidence['family'], evidence['expected_model']), ('fable', FABLE))
        self.assertEqual(evidence['qualification']['sha256'], qmod.digest(artifact))
        self.assertEqual(evidence['executable']['version'], '2.1.282 (Claude Code)')
        bundled = {**em.BUNDLED_FAMILIES['fable'], 'minimum_claude_code_version': '2.1.300'}
        with patch.dict(em.BUNDLED_FAMILIES, {'fable': bundled}):
            with self.assertRaisesRegex(ao.RoomError, r'requires Claude Code 2\.1\.300 or newer'):
                qmod.admission(self.directory(), self.state(), prepared=prepared)

    def test_unsupported_configuration_refuses_without_leaking_environment_values(self):
        artifact = self.artifact()
        qualification = {'artifact': artifact, 'sha256': qmod.digest(artifact)}
        cases = [({'unknown': True}, 'configuration declares only'),
                 ({'foreground': 'yes'}, 'foreground'),
                 ({'agents': {'pr-opus': 'opus'}}, 'worker map'),
                 ({'local_settings': []}, 'local_settings'),
                 ({'environ': ['X']}, 'environment evidence'),
                 ({'local_settings': {'modelOverrides': {'opus': 'claude-fable-5-1'}}}, 'modelOverrides'),
                 ({'agents': ao_routing.FAMILY_AGENTS,
                   'local_settings': {'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-5'}}},
                  'remaps a family worker alias'),
                 ({'environ': {'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': 'claude-opus-5-6'}}, 'override')]
        for configuration, message in cases:
            with self.subTest(configuration=configuration), self.assertRaisesRegex(ao.RoomError, message):
                qmod.admit_qualification(qualification, 'opus', executable=self.executable(),
                                         configuration=configuration,
                                         origin={'context': 'pending_engineering_target'})
        secret = 'synthetic-secret-value'
        evidence = qmod.admit_qualification(qualification, 'opus', executable=self.executable(),
                                            configuration={'environ': {'UNRELATED_SECRET': secret}},
                                            origin={'context': 'pending_engineering_target'})
        self.assertNotIn(secret, json.dumps(evidence))

    def test_lower_helper_never_calls_validate_local_or_preparation(self):
        self.qualified_room('lower-guard')
        snapshot = self.state()[em.SELECTION_KEY]['family_qualification']
        with patch.object(ao_routing, 'validate_local', side_effect=AssertionError('validate_local must not run')), \
                patch.object(ao_delegates, 'preparation', side_effect=AssertionError('preparation must not run')), \
                patch.object(binding, 'effective', side_effect=AssertionError('executable binding must not run')), \
                patch.object(qmod, 'admission', side_effect=AssertionError('convenience admission must not run')):
            evidence = qmod.admit_qualification(
                snapshot, 'opus', directory=self.directory(), executable=self.executable(),
                configuration={'config_dir': str(self.claude_env), 'worktree': str(self.repo),
                               'agents': ao_routing.FAMILY_AGENTS},
                origin={'context': 'current_epoch'})
        self.assertEqual(evidence['proof'], 'retained')
        self.assertIn('user Claude settings source', evidence['configuration']['checked'])
        self.assertIn('managed Claude settings source', evidence['configuration']['checked'])

    def test_current_and_committed_context_wrappers_remain_compatible(self):
        self.qualified_room('wrapper-compatible')
        self.review_turn(OPUS)
        parts = self.parts(request_id='reset-opus', source_model='opus', target_model='opus')
        self.publish(parts)
        target = parts['intent']['target']
        before = files_under(self.directory())
        structural = em.qualification_context_for_target(target)
        self.assertEqual(files_under(self.directory()), before)
        committed = em.qualification_context(self.directory(), self.state(), 'reset-opus')
        self.assertEqual(committed['source'], 'committed_target')
        for key in ('selector', 'qualification', 'reference', 'expected_model'):
            self.assertEqual(committed[key], structural[key])
        current = em.qualification_context(self.directory(), self.state())
        self.assertEqual(current['source'], 'current_epoch')
        self.assertEqual(current['expected_model'], OPUS)

    def test_worker_qualification_keyword_is_accepted_without_bridges(self):
        """The lower helper takes an independently pinned worker snapshot as its own authority.

        A root artifact that maps only its own family must still be admitted, with the worker map
        and worker floors derived from the newer worker snapshot and never from the root artifact.
        """
        shared = self.artifact(families={
            'opus': {'expected_model': OPUS, 'source_ids': ['opus-family-doc'],
                     'minimum_claude_code_version': '2.1.280'},
            'sonnet': {'expected_model': 'claude-sonnet-5-5', 'source_ids': ['opus-family-doc']}})
        sha = qmod.digest(shared)
        worker = ao_routing.worker_qualification_value({'artifact': shared, 'sha256': sha,
                                                       'record': qmod.record_path(sha),
                                                       'evidence': qmod.evidence_entries(shared, sha)})
        root = self.artifact()  # the room's own root epoch artifact maps only its own family
        evidence = qmod.admit_qualification({'artifact': root, 'sha256': qmod.digest(root)}, 'opus',
                                            executable=self.executable(),
                                            configuration={'agents': ao_routing.FAMILY_AGENTS},
                                            origin={'context': 'pending_engineering_target'},
                                            worker_roles=ao_routing.WORKER_FAMILIES,
                                            worker_qualification=worker)
        self.assertEqual(evidence['expected_model'], OPUS)
        self.assertEqual(evidence['qualification']['sha256'], qmod.digest(root))
        self.assertEqual(evidence['worker_families'], ao_routing.WORKER_FAMILIES)
        self.assertEqual(evidence['worker_expected_models'],
                         {'pr-opus': OPUS, 'pr-sonnet': 'claude-sonnet-5-5'})

    def worker_source_artifact(self, sonnet_floor=None):
        """One synthetic independent worker artifact with its own private retained source capture."""
        path = self.evidence('Synthetic independent worker-retained documentation excerpt.\n',
                             name='worker-family.txt')
        descriptor = {'id': 'worker-family-doc', 'uri': 'https://docs.invalid.example/claude/worker-family',
                      'captured_at': '2026-06-01T00:00:00Z',
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence_file': str(path)}
        sonnet = {'expected_model': self.WORKER_SONNET, 'source_ids': ['worker-family-doc']}
        if sonnet_floor is not None:
            sonnet['minimum_claude_code_version'] = sonnet_floor
        families = {'opus': {'expected_model': OPUS, 'source_ids': ['worker-family-doc'],
                             'minimum_claude_code_version': '2.1.280'}, 'sonnet': sonnet}
        return self.artifact(families=families, sources=[descriptor]), path

    def retain_distinct_proof(self, name):
        """Retain one root artifact and one distinct worker artifact in a fresh owned directory."""
        root = self.artifact()
        worker, worker_path = self.worker_source_artifact()
        directory = self.home / 'ao' / 'rooms' / name
        directory.mkdir(parents=True)
        root_snapshot = qmod.retain(directory, qmod.snapshot({'artifact': root, 'sha256': qmod.digest(root)}))
        worker_snapshot = qmod.retain(directory,
                                      qmod.snapshot({'artifact': worker, 'sha256': qmod.digest(worker)}))
        return directory, root_snapshot, worker_snapshot, worker_path

    def test_independent_worker_snapshot_authenticates_its_own_retained_proof(self):
        directory, root_snapshot, worker_snapshot, worker_path = self.retain_distinct_proof('worker-proof')
        (self.root / 'evidence' / 'opus-family.txt').unlink()
        worker_path.unlink()
        evidence = qmod.admit_qualification(
            root_snapshot, 'opus', directory=directory, executable=self.executable(), configuration={},
            origin={'context': 'pending_routing_snapshot'}, worker_roles=ao_routing.WORKER_FAMILIES,
            worker_qualification=ao_routing.worker_qualification_value(worker_snapshot))
        self.assertEqual(evidence['proof'], 'retained')
        self.assertEqual(evidence['worker_proof'], 'retained')
        self.assertEqual(evidence['worker_reference']['sha256'], qmod.digest(worker_snapshot['artifact']))
        self.assertEqual(evidence['worker_reference']['record'], worker_snapshot['record'])
        self.assertEqual(evidence['worker_expected_models'],
                         {'pr-opus': OPUS, 'pr-sonnet': self.WORKER_SONNET})
        self.assertEqual([item['id'] for item in evidence['worker_sources']], ['worker-family-doc'])

    def test_missing_or_changed_worker_only_bytes_refuse_without_root_substitution(self):
        directory, root_snapshot, worker_snapshot, _ = self.retain_distinct_proof('worker-damage')
        value = ao_routing.worker_qualification_value(worker_snapshot)
        capture = directory / worker_snapshot['evidence'][0]['record']
        record = directory / worker_snapshot['record']
        raw_capture, raw_record = capture.read_bytes(), record.read_bytes()
        for damage in ('capture changed', 'capture missing', 'artifact changed', 'artifact missing'):
            with self.subTest(damage=damage):
                if damage == 'capture changed':
                    capture.write_bytes(raw_capture + b'changed')
                elif damage == 'capture missing':
                    capture.unlink()
                elif damage == 'artifact changed':
                    # A parsed artifact value changes while the reference's expected digest stays the
                    # old one; whitespace or any other canonically irrelevant edit is not a semantic
                    # change to a canonically digested artifact.
                    artifact = json.loads(raw_record)
                    artifact['revision'] += 1
                    record.write_bytes(json.dumps(artifact, sort_keys=True, ensure_ascii=False,
                                                  allow_nan=False).encode() + b'\n')
                else:
                    record.unlink()
                try:
                    with self.assertRaisesRegex(ao.RoomError, 'worker qualification retained evidence'):
                        qmod.admit_qualification(
                            root_snapshot, 'opus', directory=directory, executable=self.executable(),
                            configuration={}, origin={'context': 'pending_routing_snapshot'},
                            worker_roles=ao_routing.WORKER_FAMILIES, worker_qualification=value)
                    # The root snapshot's own retained proof is untouched and is never substituted
                    # for the damaged worker bytes.
                    root_only = qmod.admit_qualification(
                        root_snapshot, 'opus', directory=directory, executable=self.executable(),
                        configuration={}, origin={'context': 'pending_routing_snapshot'})
                    self.assertEqual(root_only['proof'], 'retained')
                    self.assertIsNone(root_only['worker_proof'])
                finally:
                    capture.write_bytes(raw_capture)
                    record.write_bytes(raw_record)

    def test_root_and_worker_retained_proofs_are_not_substituted(self):
        directory, root_snapshot, worker_snapshot, _ = self.retain_distinct_proof('worker-cross')
        root_capture = directory / root_snapshot['evidence'][0]['record']
        raw_root = root_capture.read_bytes()
        root_capture.write_bytes(raw_root + b'changed')
        try:
            with self.assertRaisesRegex(ao.RoomError, 'candidate family qualification retained evidence'):
                qmod.admit_qualification(
                    root_snapshot, 'opus', directory=directory, executable=self.executable(),
                    configuration={}, origin={'context': 'pending_routing_snapshot'},
                    worker_roles=ao_routing.WORKER_FAMILIES,
                    worker_qualification=ao_routing.worker_qualification_value(worker_snapshot))
            # The independent worker snapshot still authenticates from its own retained bytes alone.
            worker_only = qmod.admit_qualification(
                worker_snapshot, 'sonnet', directory=directory, executable=self.executable(),
                configuration={}, origin={'context': 'pending_routing_snapshot'})
            self.assertEqual(worker_only['proof'], 'retained')
        finally:
            root_capture.write_bytes(raw_root)

    def test_prospective_worker_proof_authenticates_its_own_private_capture(self):
        root = self.artifact()
        worker, worker_path = self.worker_source_artifact()
        value = ao_routing.worker_qualification_value(
            qmod.snapshot({'artifact': worker, 'sha256': qmod.digest(worker)}))

        def attempt():
            return qmod.admit_qualification(
                {'artifact': root, 'sha256': qmod.digest(root)}, 'opus', executable=self.executable(),
                configuration={}, origin={'context': 'pending_routing_snapshot'},
                worker_roles=ao_routing.WORKER_FAMILIES, worker_qualification=value)

        evidence = attempt()
        self.assertEqual((evidence['proof'], evidence['worker_proof']),
                         ('private_prospective', 'private_prospective'))
        self.assertEqual(evidence['worker_reference']['sha256'], qmod.digest(worker))
        raw = worker_path.read_bytes()
        worker_path.unlink()
        with self.assertRaisesRegex(ao.RoomError, 'unreadable'):
            attempt()
        worker_path.write_bytes(raw + b'changed')
        with self.assertRaisesRegex(ao.RoomError, 'does not match its retained SHA256'):
            attempt()
        worker_path.write_bytes(raw)
        self.assertEqual(attempt()['worker_proof'], 'private_prospective')

    def test_worker_map_mismatch_and_worker_only_floors_refuse(self):
        root = self.artifact()
        worker, _ = self.worker_source_artifact(sonnet_floor='2.1.300')
        value = ao_routing.worker_qualification_value(
            qmod.snapshot({'artifact': worker, 'sha256': qmod.digest(worker)}))
        with self.assertRaisesRegex(ao.RoomError, 'must map exactly the enabled worker roles'):
            qmod.admit_qualification(
                {'artifact': root, 'sha256': qmod.digest(root)}, 'opus', executable=self.executable(),
                configuration={}, origin={'context': 'pending_routing_snapshot'},
                worker_roles={'pr-sonnet': 'opus', 'pr-opus': 'sonnet'}, worker_qualification=value)
        with self.assertRaisesRegex(ao.RoomError, 'pr-sonnet requires Claude Code 2.1.300 or newer'):
            qmod.admit_qualification(
                {'artifact': root, 'sha256': qmod.digest(root)}, 'opus', executable=self.executable(),
                configuration={}, origin={'context': 'pending_routing_snapshot'},
                worker_roles=ao_routing.WORKER_FAMILIES, worker_qualification=value)


if __name__ == '__main__':
    unittest.main()
