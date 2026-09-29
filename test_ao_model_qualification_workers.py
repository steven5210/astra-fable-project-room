'''Focused synthetic compatibility contracts for the lower qualification admission helper.

These tests call the real ao_model_qualification.admit_qualification against real room bytes retained
inside an ordinary owned temporary directory and real private source files. No room service, AO
transport, model, account or network call is involved, no production function is replaced, and no
admission result is fabricated. They pin the additive worker-directory parameter: the default
sentinel keeps every omitted-parameter caller's exact behavior and result shape, an explicit room
path locates the independent worker's own retained proof, and an explicit None is prospective worker
proof only. Nothing here claims these tests were executed.
'''

import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

import ao_model_qualification as qmod
import ao_routing
from room import RoomError

OPUS = 'claude-opus-5-5'
SONNET = 'claude-sonnet-5-5'
WORKER_MODELS = {'opus': OPUS, 'sonnet': SONNET}
EXECUTABLE = {'path': '/synthetic/claude', 'size': 0, 'mtime_ns': 0, 'sha256': 'a' * 64,
              'version': '2.1.282 (Claude Code)', 'error': None}
RESULT_FIELDS = frozenset((
    'version', 'family', 'selector', 'expected_model', 'minimum_claude_code_version',
    'effective_minimum_claude_code_version', 'worker_families', 'worker_expected_models', 'worker_proof',
    'worker_reference', 'worker_sources', 'qualification', 'sources', 'scope', 'proof', 'executable',
    'executable_basis', 'configuration', 'origin', 'checked', 'not_checked', 'runtime_unobserved', 'basis',
    'transaction_evidence', 'reusable_authorization_token', 'meaning'))


class LowerWorkerDirectoryCase(unittest.TestCase):
    '''One owned temporary root; every test creates its own room directory and artifacts.'''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def room_directory(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        return path

    def evidence(self, name):
        path = self.root / ('evidence-' + name + '.txt')
        path.write_text('Synthetic operator-retained excerpt for ' + name + '.\n')
        path.chmod(0o600)
        return path

    def artifact(self, families, revision, name):
        path = self.evidence(name)
        descriptor = {'id': 'synthetic-doc', 'uri': 'https://docs.invalid.example/claude/family',
                      'captured_at': '2026-06-01T00:00:00Z',
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'evidence_file': str(path)}
        return {'format': qmod.FORMAT, 'revision': revision, 'qualified_at': '2026-06-02T00:00:00Z',
                'scope': copy.deepcopy(qmod.SCOPE), 'families': families, 'sources': [descriptor]}

    def root_artifact(self, revision=1):
        return self.artifact({'opus': {'expected_model': OPUS, 'source_ids': ['synthetic-doc'],
                                       'minimum_claude_code_version': '2.1.280'}}, revision, 'root')

    def worker_artifact(self, revision=5):
        families = {}
        for family in sorted(set(ao_routing.WORKER_FAMILIES.values())):
            entry = {'expected_model': WORKER_MODELS[family], 'source_ids': ['synthetic-doc']}
            if family == 'opus':
                entry['minimum_claude_code_version'] = '2.1.280'
            families[family] = entry
        return self.artifact(families, revision, 'worker')

    def snapshot(self, artifact):
        return qmod.snapshot({'artifact': artifact, 'sha256': qmod.digest(artifact)})

    def retain(self, directory, snapshot):
        qmod.retain(directory, snapshot)
        return snapshot

    def worker_value(self, snapshot):
        return ao_routing.worker_qualification_value(snapshot)

    def admit(self, snapshot, *, directory=None, **changes):
        return qmod.admit_qualification(
            snapshot, 'opus', directory=directory, executable=copy.deepcopy(EXECUTABLE),
            configuration={}, origin={'context': 'pending_engineering_target',
                                      'request_id': 'synthetic-request'}, **changes)

    # --- default/inheritance compatibility -------------------------------------------------
    def test_omitted_parameter_inherits_the_root_location_and_keeps_the_result_shape(self):
        room = self.room_directory('room-inherit')
        root = self.retain(room, self.snapshot(self.root_artifact()))
        worker = self.retain(room, self.snapshot(self.worker_artifact()))
        value = self.worker_value(worker)
        inherited = self.admit(root, directory=room, worker_qualification=value)
        explicit = self.admit(root, directory=room, worker_qualification=value, worker_directory=room)
        self.assertEqual(inherited, explicit)
        self.assertEqual(set(inherited), set(RESULT_FIELDS))
        self.assertEqual(inherited['proof'], 'retained')
        self.assertEqual(inherited['worker_proof'], 'retained')
        self.assertEqual(inherited['worker_reference']['sha256'], worker['sha256'])
        prospective = self.snapshot(self.root_artifact(revision=2))
        inherited_private = self.admit(prospective, directory=None, worker_qualification=value)
        explicit_private = self.admit(prospective, directory=None, worker_qualification=value,
                                      worker_directory=None)
        self.assertEqual(inherited_private, explicit_private)
        self.assertEqual(inherited_private['proof'], 'private_prospective')
        self.assertEqual(inherited_private['worker_proof'], 'private_prospective')

    def test_worker_directory_is_not_a_proof_source_without_an_independent_worker(self):
        room = self.room_directory('room-no-worker')
        self.retain(room, self.snapshot(self.root_artifact()))
        prospective = self.snapshot(self.root_artifact(revision=2))
        omitted = self.admit(prospective, directory=None)
        explicit = self.admit(prospective, directory=None, worker_directory=room)
        self.assertEqual(omitted, explicit)
        self.assertIsNone(omitted['worker_proof'])
        self.assertIsNone(omitted['worker_reference'])
        self.assertIsNone(omitted['worker_sources'])
        self.assertIsNone(omitted['worker_families'])
        self.assertEqual(omitted['worker_expected_models'], {})

    # --- independently located worker proof -------------------------------------------------
    def test_explicit_worker_directory_uses_only_the_workers_retained_bytes(self):
        room = self.room_directory('room-located')
        self.retain(room, self.snapshot(self.root_artifact()))
        worker = self.retain(room, self.snapshot(self.worker_artifact()))
        value = self.worker_value(worker)
        prospective_root = self.root_artifact(revision=2)
        root_evidence = Path(prospective_root['sources'][0]['evidence_file'])
        worker_evidence = Path(worker['artifact']['sources'][0]['evidence_file'])
        length = worker_evidence.stat().st_size
        worker_evidence.unlink()
        result = self.admit(self.snapshot(prospective_root), directory=None, worker_directory=room,
                            worker_qualification=value)
        self.assertEqual(result['proof'], 'private_prospective')
        self.assertEqual(result['worker_proof'], 'retained')
        self.assertEqual(result['worker_reference']['sha256'], worker['sha256'])
        self.assertEqual([item['sha256'] for item in result['worker_sources']],
                         [descriptor['sha256'] for descriptor in worker['artifact']['sources']])
        self.assertEqual([item['bytes'] for item in result['worker_sources']], [length])
        self.assertFalse(worker_evidence.exists())
        self.assertTrue(root_evidence.exists())

    def test_explicit_prospective_mode_differs_from_inherited_retained_worker_proof(self):
        room = self.room_directory('room-prospective')
        root = self.retain(room, self.snapshot(self.root_artifact()))
        worker = self.retain(room, self.snapshot(self.worker_artifact()))
        value = self.worker_value(worker)
        inherited = self.admit(root, directory=room, worker_qualification=value)
        prospective = self.admit(root, directory=room, worker_qualification=value, worker_directory=None)
        self.assertEqual(inherited['proof'], prospective['proof'])
        self.assertEqual(inherited['worker_proof'], 'retained')
        self.assertEqual(prospective['worker_proof'], 'private_prospective')
        self.assertEqual(inherited['worker_reference'], prospective['worker_reference'])
        self.assertEqual(inherited['worker_expected_models'], prospective['worker_expected_models'])

    def test_missing_or_changed_retained_worker_bytes_never_fall_back_to_private(self):
        for case in ('missing-capture', 'changed-capture', 'missing-record'):
            with self.subTest(case=case):
                room = self.room_directory('room-' + case)
                self.retain(room, self.snapshot(self.root_artifact()))
                worker = self.retain(room, self.snapshot(self.worker_artifact()))
                value = self.worker_value(worker)
                prospective_root = self.snapshot(self.root_artifact(revision=2))
                worker_evidence = Path(worker['artifact']['sources'][0]['evidence_file'])
                length = worker_evidence.stat().st_size
                capture = room / worker['evidence'][0]['record']
                record = room / worker['record']
                original = capture.read_bytes()
                if case == 'missing-capture':
                    capture.unlink()
                elif case == 'changed-capture':
                    capture.write_bytes(original + b'changed')
                else:
                    record.unlink()
                self.assertTrue(worker_evidence.exists())
                with self.assertRaisesRegex(RoomError,
                                            'independent worker qualification retained evidence is missing or '
                                            'changed'):
                    self.admit(prospective_root, directory=None, worker_directory=room,
                               worker_qualification=value)
                self.assertEqual(worker_evidence.stat().st_size, length)
                if case == 'missing-capture':
                    self.assertFalse(capture.exists())
                elif case == 'changed-capture':
                    self.assertEqual(capture.read_bytes(), original + b'changed')
                else:
                    self.assertFalse(record.exists())


if __name__ == '__main__':
    unittest.main()
