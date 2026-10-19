"""Offline two-room coordination contracts (fake AO, synthetic homes only)."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import ao_coordination
import ao_native_outcome
import ao_project_room as ao
import ao_workflow
from room import RoomError
from test_ao_normal import Fixture


class CoordinationFixture(Fixture):
    """The shared fixture plus a second normal room bound to a linked worktree of the same repo."""

    OWNED = 'owned/part.txt'
    NATIVE_B = 'native-b'

    def _populate_root(self):
        super()._populate_root()
        # An owned file at the baseline enables rename/delete coverage below it.
        (self.repo / 'owned').mkdir()
        (self.repo / 'owned' / 'old.txt').write_text('base\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', 'owned base'], check=True, capture_output=True)
        self.assign_wt = self.root / 'assign'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', str(self.assign_wt), 'HEAD'],
                       check=True, capture_output=True)

    def _activate(self):
        super()._activate()
        self.owners = {}
        patcher = patch('ao_native_identity.read_owner',
                        side_effect=lambda database, session_id: copy.deepcopy(self.owners.get(session_id)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake.add('engineer-b', 'claude-code')
        self.fake.add('reviewer-b')
        self.fake.snapshots['engineer-b']['settings']['model'] = ao_workflow.FABLE_MODEL
        self.fake.workspaces['engineer-b'] = self.assign_wt
        self.fake.workspaces['reviewer-b'] = self.assign_wt
        self.b_native = None
        self.gates_b = [[sys.executable, '-c',
                         "from pathlib import Path; assert Path('owned/part.txt').read_text() == 'done\\n'"]]

    def _patch_owner_reader(self):
        # The per-session selector patch installed in _activate dispatches to this map.
        self.owners[self.owner['id']] = copy.deepcopy(self.owner)

    def state_b(self):
        return ao.read(self.directory(self.assign_room) / 'state.json')

    def git(self, path, *args):
        result = subprocess.run(['git', '-C', str(path), *args], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode().strip()

    def commit_wt(self, path, message='work'):
        self.git(path, 'add', '-A')
        self.git(path, 'commit', '-m', message)

    def open_rooms(self, owned=('owned',)):
        self.room = self.open('owner')
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assign_room = self.service.ao_room_open(
            str(self.repo), 'assignment', 'project', 'User authorized this assignment',
            'http://127.0.0.1:1234', delegate_provider='none')['room_id']
        self.spec_b()
        self.bind_b()
        return self.service.ao_room_assignment_open(
            self.room, 'A1', self.assign_room, list(owned),
            'The interface contract for the owned paths.', 'User authorized this bounded assignment')

    def spec_b(self, content='Implement the bounded assignment.', gates=None, revision=1):
        return self.service.ao_room_spec_put(self.assign_room, revision, content,
                                             gates if gates is not None else self.gates_b,
                                             'Astra approves this scope')

    def bind_b(self):
        self.service.ao_room_prepare(self.assign_room, str(self.assign_wt))
        self.service.ao_room_bind(self.assign_room, 'engineer', 'engineer-b', ao_workflow.FABLE_MODEL, 'max')
        self.service.ao_room_bind(self.assign_room, 'reviewer', 'reviewer-b', 'astra', 'max')
        self.register_native_source_b()

    def native_config_root_b(self):
        path = self.directory(self.assign_room) / 'preparation.json'
        routing = ao.read(path).get('routing') or {} if path.exists() else {}
        value = routing.get('claude_config_dir')
        return Path(value) if isinstance(value, str) and value else self.claude_env

    def register_native_source_b(self):
        directory = self.directory(self.assign_room)
        state = ao.read(directory / 'state.json')
        binding = state['bindings']['engineer']
        config_root = self.native_config_root_b()
        project_dir = config_root / 'projects' / str(self.assign_wt).replace('/', '-')
        transcript = project_dir / (self.NATIVE_B + '.jsonl')
        project_dir.mkdir(parents=True, exist_ok=True)
        transcript.write_text('')
        self.owners[binding['session_id']] = {
            'id': binding['session_id'], 'project_id': state['ao_project_id'],
            'harness': 'claude-code', 'session_mode': 'chat', 'is_terminated': 0,
            'activity_state': 'idle', 'workspace_path': str(self.assign_wt),
            'provider_conversation_id': self.NATIVE_B, 'controller_generation': 'generation-1',
            'ao_conversation_id': binding['conversation_id'], 'active_branch_id': binding['branch_id'],
            'branch_provider_conversation_id': self.NATIVE_B, 'branch_session_id': binding['session_id'],
            'strategy': 'native', 'replay_truncated': 0}
        source = {'database': str(self.root / 'owner.db'), 'transcript': str(transcript),
                  'session_id': binding['session_id'], 'native_session_id': self.NATIVE_B}
        ao_native_outcome.preflight_source(directory, state, source['database'], source['transcript'])
        ao.atomic(directory / 'state.json', state)
        self.b_native = {'transcript': transcript, 'events': [], 'requests': set(), 'source': source}

    def note_native_turn_b(self, request_id):
        bag = self.b_native
        if bag is None or request_id in bag['requests']:
            return
        request = self.state_b()['requests'][request_id]
        if request.get('state') in ('completed', 'settled_failure'):
            return
        model = (request.get('engineering_resolution') or {}).get('expected_model') or request['model']
        stamp = lambda seconds: min(datetime.fromtimestamp(request['created_at'] + seconds, timezone.utc),
                                  datetime.now(timezone.utc)).isoformat()
        bag['events'].extend([
            {'type': 'user', 'uuid': request_id + '-caller', 'sessionId': self.NATIVE_B,
             'timestamp': stamp(0.001), 'cwd': str(self.assign_wt), 'isSidechain': False,
             'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': request['text']}},
            {'type': 'assistant', 'uuid': request_id + '-reply', 'sessionId': self.NATIVE_B,
             'timestamp': stamp(0.002), 'cwd': str(self.assign_wt),
             'message': {'role': 'assistant', 'model': model, 'id': request_id + '-m0',
                         'content': [{'type': 'text', 'text': 'Done'}], 'stop_reason': 'end_turn'}}])
        bag['transcript'].write_text(''.join(json.dumps(event) + '\n' for event in bag['events']))
        bag['requests'].add(request_id)

    def send_b(self, purpose, key=None):
        role = 'reviewer' if purpose == 'acceptance_review' else 'engineer'
        result = self.service.ao_room_send(self.assign_room, role, 'Perform the exact authorized purpose.',
                                           key or purpose, purpose=purpose)
        if role == 'engineer':
            self.note_native_turn_b(key or purpose)
        return result

    def agree_b(self, decision='accept', revision=1, key='spec_review'):
        self.send_b('spec_review', key)
        spec = self.service.spec(self.directory(self.assign_room), self.state_b())
        self.fake.finish('engineer-b', json.dumps({'interpretation': 'Implement the supplied pure behavior.',
                         'findings': [], 'decision': decision, 'spec_revision': revision,
                         'spec_sha256': spec['sha256']}))
        return self.service.ao_room_sync(self.assign_room)

    def report_b(self, **changes):
        handoff = ao_workflow.handoff_record(self.directory(self.assign_room), self.state_b())
        return {'outcome': 'completed', 'implementation_complete': True,
                'changes': ['Implemented assignment'], 'tests_reported': ['Focused tests pass'],
                'review_findings': [], 'remaining_gaps': [], 'backlog': [], 'routing_log': [],
                'spec_revision': handoff['spec_revision'], 'spec_sha256': handoff['spec_sha256'],
                'baseline_commit': handoff['baseline_commit'], **changes}

    def implement_b(self, change=None, commit=True, request='implementation'):
        self.send_b('implementation', request)
        if change is None:
            (self.assign_wt / self.OWNED).write_text('done\n')
        else:
            change()
        if commit:
            self.commit_wt(self.assign_wt, 'assignment work')
        self.fake.finish('engineer-b', json.dumps(self.report_b()))
        return self.service.ao_room_sync(self.assign_room)

    def review_b(self, request='acceptance_review'):
        self.service.ao_room_verify(self.assign_room, str(self.assign_wt))
        self.send_b('acceptance_review', request)
        saved = self.state_b()['requests'][request]
        self.fake.finish('reviewer-b', json.dumps({**saved['review'], 'decision': 'approved',
                         'review': 'Independently inspected exact behavior and gates.'}))
        return self.service.ao_room_sync(self.assign_room)

    def accept_b(self, request='acceptance_review'):
        return self.service.ao_room_accept(self.assign_room, request)

    def assignment_accepted(self):
        """Open A1, then take the assignment room through agree, implement, review and accept."""
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        return self.accept_b()

    def handed_back(self):
        self.assignment_accepted()
        return self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def assignments_root(self):
        return self.home / 'ao' / 'coordination' / 'assignments'

    def lifecycle(self, assignment_id='A1'):
        return ao.read(self.assignments_root() / assignment_id / 'state.json')


class SuccessPathTests(CoordinationFixture):
    def test_two_room_success_path(self):
        self.open_rooms()
        # A captured first implementation result (no worktree change) keeps the owner
        # clean for a ready preflight and authorizes a correction turn after the import.
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        self.accept_b()
        lineage = ao.read(self.assignments_root() / 'A1' / 'lineage.json')
        baseline = lineage['owner']['baseline_commit']
        commit = self.git(self.assign_wt, 'rev-parse', 'HEAD')
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.assertEqual(record['commit'], commit)
        self.assertEqual(record['baseline_commit'], baseline)
        self.assertEqual(record['owner_room_id'], self.room)
        self.assertEqual(record['assignment_room_id'], self.assign_room)
        changed = {entry['path']: entry['status'] for entry in record['changed_paths']}
        self.assertEqual(changed, {'owned/part.txt': 'A'})
        patch = subprocess.run(['git', '-C', str(self.assign_wt), 'diff', '--binary', baseline, commit],
                               capture_output=True, check=True)
        self.assertEqual(record['patch_sha256'], hashlib.sha256(patch.stdout).hexdigest())
        self.assertTrue((self.assignments_root() / 'A1' / 'handbacks' / (record['handback_sha256'] + '.json')).is_file())
        self.assertEqual(self.lifecycle()['state'], 'handback_recorded')
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertTrue(preflight['ready'])
        self.assertEqual(preflight['preview'], 'clean')
        self.assertEqual(preflight['conflicts'], [])
        self.assertEqual(preflight['owner_head_before'], self.git(self.repo, 'rev-parse', 'HEAD'))
        self.git(self.repo, 'cherry-pick', '-x', commit)
        imported = self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'],
                                                    'git cherry-pick -x ' + commit)
        self.assertEqual(imported['kind'], 'faithful')
        self.assertIsNone(imported['resolution'])
        self.assertEqual(imported['owner_head_after'], self.git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(self.lifecycle()['state'], 'imported')
        # A fresh post-import engineering result lets the owner accept.
        self.send('correction')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.review()
        result = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(result['accepted'])
        status = self.service.ao_room_status(self.room)['coordination']
        self.assertEqual(status['role'], 'owner')
        entry = status['assignments'][0]
        self.assertEqual(entry['assignment_id'], 'A1')
        self.assertEqual(entry['state'], 'imported')
        self.assertEqual(entry['assignment_room_id'], self.assign_room)
        self.assertEqual(entry['owner_room_id'], self.room)
        self.assertEqual(entry['owned_paths'], ['owned'])
        self.assertEqual(entry['latest_handback']['sha256'], record['handback_sha256'])
        self.assertEqual(entry['import']['sha256'], imported['import_sha256'])
        self.assertEqual(entry['import']['kind'], 'faithful')
        self.assertEqual(entry['import']['owner_head_after'], imported['owner_head_after'])
        self.assertEqual(entry['blockers'], [])
        compact = self.service.ao_room_status(self.room, 'compact')['coordination']
        self.assertEqual(compact['role'], 'owner')
        self.assertEqual(compact['assignments'][0]['state'], 'imported')
        assign_status = self.service.ao_room_status(self.assign_room)['coordination']
        self.assertEqual(assign_status['role'], 'assignment')
        self.assertEqual(assign_status['assignments'][0]['owner_room_id'], self.room)
        progress = self.service.ao_room_progress(self.room)['coordination']
        self.assertEqual(progress['role'], 'owner')
        self.assertEqual(progress['assignments'][0]['assignment_id'], 'A1')
        rooms = {item['room_id']: item for item in self.service.ao_room_list()['rooms']}
        self.assertEqual(rooms[self.room]['coordination'], {'role': 'owner'})
        self.assertEqual(rooms[self.assign_room]['coordination'], {'role': 'assignment'})
        pointers = self.state()['coordination']
        self.assertEqual(pointers, {'role': 'owner', 'assignment_ids': ['A1']})
        self.assertEqual(self.state_b()['coordination'],
                         {'role': 'assignment', 'assignment_ids': ['A1']})


class OwnerAcceptanceTests(CoordinationFixture):
    def test_owner_candidate_touching_owned_path_refuses(self):
        self.open_rooms()
        self.send('implementation')
        (self.repo / 'owned' / 'part.txt').write_text('owner touched\n')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.commit_wt(self.repo, 'owner work')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.review()
        with self.assertRaisesRegex(RoomError, 'owned_by_active_assignment'):
            self.service.ao_room_accept(self.room, 'acceptance_review')

    def test_owner_accept_after_import_requires_post_import_result(self):
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        self.accept_b()
        self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertTrue(preflight['ready'])
        self.git(self.repo, 'cherry-pick', '-x', record['commit'])
        # A pre-import implementation result alone does not satisfy the post-import
        # freshness rule; correction needs one completed implementation first.
        self.send('correction')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'],
                                           'git cherry-pick -x ' + record['commit'])
        self.review()
        with self.assertRaisesRegex(RoomError, 'integration_requires_fresh_engineering'):
            self.service.ao_room_accept(self.room, 'acceptance_review')
        # A fresh engineering result recorded after the import allows acceptance.
        self.send('correction', 'correction-2')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send('acceptance_review', 'acceptance_review-2')
        saved = self.state()['requests']['acceptance_review-2']
        self.fake.finish('reviewer', json.dumps({**saved['review'], 'decision': 'approved',
                                                 'review': 'Independently inspected exact behavior and gates.'}))
        self.service.ao_room_sync(self.room)
        result = self.service.ao_room_accept(self.room, 'acceptance_review-2')
        self.assertTrue(result['accepted'])


class AdmissionTests(CoordinationFixture):
    def test_concurrent_overlapping_admission_exactly_one_wins(self):
        self.room = self.open('owner')
        self.spec()
        self.bind()
        self.agree()
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.assign_room = self.service.ao_room_open(
            str(self.repo), 'assignment', 'project', 'User authorized this assignment',
            'http://127.0.0.1:1234', delegate_provider='none')['room_id']
        second_room = self.service.ao_room_open(
            str(self.repo), 'assignment-two', 'project', 'User authorized another assignment',
            'http://127.0.0.1:1234', delegate_provider='none')['room_id']
        self.spec_b()
        self.service.ao_room_spec_put(second_room, 1, 'Implement the other assignment.',
                                      self.gates_b, 'Astra approves this scope')
        other_service = ao.Service(self.home, lambda url: self.fake)
        outcomes = {}

        def admit(tag, service, assignment_id, room_id):
            try:
                outcomes[tag] = service.ao_room_assignment_open(
                    self.room, assignment_id, room_id, ['owned'], 'interface', 'authorization')
            except RoomError as exc:
                outcomes[tag] = exc

        threads = [threading.Thread(target=admit, args=('a', self.service, 'A1', self.assign_room)),
                   threading.Thread(target=admit, args=('b', other_service, 'A2', second_room))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wins = [value for value in outcomes.values() if isinstance(value, dict)]
        losses = [value for value in outcomes.values() if isinstance(value, RoomError)]
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)
        self.assertIn('overlap', str(losses[0]))
        self.assertEqual(len(list(self.assignments_root().glob('*/lineage.json'))), 1)

    def test_refused_admission_leaves_no_durable_record(self):
        self.open_rooms(owned=('owned',))
        second_room = self.service.ao_room_open(
            str(self.repo), 'assignment-two', 'project', 'User authorized another assignment',
            'http://127.0.0.1:1234', delegate_provider='none')['room_id']
        self.service.ao_room_spec_put(second_room, 1, 'Implement the other assignment.',
                                      self.gates_b, 'Astra approves this scope')
        with self.assertRaisesRegex(RoomError, 'overlap'):
            self.service.ao_room_assignment_open(self.room, 'A2', second_room, ['owned/part.txt'],
                                                 'interface', 'authorization')
        self.assertFalse((self.assignments_root() / 'A2').exists())
        # The clean retry succeeds.
        result = self.service.ao_room_assignment_open(self.room, 'A2', second_room, ['unrelated'],
                                                      'interface', 'authorization')
        self.assertTrue(result['opened'])

    def test_supersede_marks_old_and_admits_new(self):
        self.open_rooms()
        result = self.service.ao_room_assignment_open(
            self.room, 'A2', self.assign_room, ['unrelated'], 'interface', 'authorization',
            supersedes='A1')
        self.assertTrue(result['opened'])
        old = self.lifecycle('A1')
        self.assertEqual(old['state'], 'superseded')
        self.assertEqual(old['superseded_by'], 'A2')
        self.assertEqual(self.lifecycle('A2')['state'], 'active')
        self.assertEqual(self.state_b()['coordination']['assignment_ids'], ['A1', 'A2'])
        with self.assertRaisesRegex(RoomError, 'terminal'):
            self.service.ao_room_assignment_open(
                self.room, 'A3', self.assign_room, ['other'], 'interface', 'authorization',
                supersedes='A1')

    def test_wrong_repository_and_role_refusals(self):
        self.open_rooms()
        other = self.root / 'other-repo'
        other.mkdir()
        (other / 'f.txt').write_text('x\n')
        for args in (('init',), ('config', 'user.name', 'T'), ('config', 'user.email', 't@i'),
                     ('add', '.'), ('commit', '-m', 'foreign')):
            subprocess.run(['git', '-C', str(other), *args], check=True, capture_output=True)
        self.fake.path = str(other)
        try:
            foreign = self.service.ao_room_open(
                str(other), 'foreign', 'project', 'User authorized this foreign feature',
                'http://127.0.0.1:1234', delegate_provider='none')['room_id']
        finally:
            self.fake.path = str(self.repo)
        self.service.ao_room_spec_put(foreign, 1, 'Foreign work.', self.gates_b, 'Astra approves')
        with self.assertRaisesRegex(RoomError, 'Git repository'):
            self.service.ao_room_assignment_open(self.room, 'AX', foreign, ['x'], 'i', 'a')
        with self.assertRaisesRegex(RoomError, 'different room'):
            self.service.ao_room_assignment_open(self.room, 'AY', self.room, ['x'], 'i', 'a')
        with self.assertRaisesRegex(RoomError, 'participates'):
            self.service.ao_room_assignment_open(self.room, 'AZ', self.assign_room, ['x'], 'i', 'a')


class PathValidationTests(CoordinationFixture):
    def test_owned_path_grammar(self):
        bad = (['../x'], ['/abs'], ['a//b'], ['a/'], ['./a'], ['.git/x'], ['a\\b'],
               ['a', 'a'], ['a', 'a/b'], [], [1], ['a', ''], ['.'], ['a/../b'], ['a//b'])
        for paths in bad:
            with self.subTest(paths=paths):
                with self.assertRaises(RoomError):
                    self.service.ao_room_assignment_open('ao-x', 'V1', 'ao-y', paths, 'i', 'a')
        with self.assertRaises(RoomError):
            self.service.ao_room_assignment_open('ao-x', 'V1', 'ao-y', ['x'] * 201, 'i', 'a')
        for paths in (['.github/workflows/ci.yml'], ['.gitignore']):
            with self.subTest(paths=paths):
                self.assertEqual(ao_coordination._owned_paths(paths), paths)
        for paths in (['.git/x'], ['a/.git/x'], ['a\x00b']):
            with self.subTest(paths=paths):
                with self.assertRaises(RoomError):
                    ao_coordination._owned_paths(paths)

    def test_rename_and_delete_scope(self):
        # Deleting an owned file records D; renaming inside owned paths passes.
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))

        def change():
            (self.assign_wt / self.OWNED).write_text('done\n')
            self.git(self.assign_wt, 'mv', 'owned/old.txt', 'owned/new.txt')

        self.implement_b(change)
        self.review_b()
        self.accept_b()
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        statuses = {entry['path']: entry['status'] for entry in record['changed_paths']}
        self.assertEqual(statuses['owned/part.txt'], 'A')
        renames = [entry for entry in record['changed_paths'] if entry['status'] == 'R']
        self.assertEqual(renames, [{'status': 'R', 'path': 'owned/new.txt', 'old_path': 'owned/old.txt'}])

    def _drifting_acceptance(self, change):
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))

        def apply():
            (self.assign_wt / self.OWNED).write_text('done\n')
            change()

        self.implement_b(apply)
        self.review_b()

    def test_rename_owned_to_unowned_refuses(self):
        self._drifting_acceptance(lambda: self.git(self.assign_wt, 'mv', 'owned/old.txt', 'moved.txt'))
        with self.assertRaisesRegex(RoomError, 'scope_drift'):
            self.accept_b()
        with self.assertRaisesRegex(RoomError, 'missing_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def test_unowned_delete_refuses(self):
        self._drifting_acceptance(lambda: self.git(self.assign_wt, 'rm', 'feature.txt'))
        with self.assertRaisesRegex(RoomError, 'scope_drift'):
            self.accept_b()
        with self.assertRaisesRegex(RoomError, 'missing_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def test_owned_delete_records_d(self):
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))

        def change():
            (self.assign_wt / self.OWNED).write_text('done\n')
            self.git(self.assign_wt, 'rm', 'owned/old.txt')

        self.implement_b(change)
        self.review_b()
        self.accept_b()
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        statuses = {entry['path']: entry['status'] for entry in record['changed_paths']}
        self.assertEqual(statuses['owned/old.txt'], 'D')


class EvidenceTests(CoordinationFixture):
    def test_handback_requires_acceptance_and_fresh_evidence(self):
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        # No acceptance recorded yet.
        with self.assertRaisesRegex(RoomError, 'missing_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.accept_b()
        # Newer candidate work makes the earlier acceptance stale.
        self.send_b('correction', 'correction-1')
        (self.assign_wt / 'owned' / 'more.txt').write_text('more\n')
        self.commit_wt(self.assign_wt, 'more work')
        self.fake.finish('engineer-b', json.dumps(self.report_b()))
        self.service.ao_room_sync(self.assign_room)
        self.service.ao_room_verify(self.assign_room, str(self.assign_wt))
        with self.assertRaisesRegex(RoomError, 'stale_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def test_tampered_checkpoint_and_receipt_refuse(self):
        self.assignment_accepted()
        checkpoint_path = self.directory(self.assign_room) / self.state_b()['checkpoint']
        checkpoint = ao.read(checkpoint_path)
        original = checkpoint_path.read_bytes()
        checkpoint['id'] = 'tampered'
        checkpoint_path.write_text(json.dumps(checkpoint))
        with self.assertRaisesRegex(RoomError, 'tampered_checkpoint'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        checkpoint_path.write_bytes(original)
        request = self.state_b()['requests']['acceptance_review']
        receipt_path = self.directory(self.assign_room) / request['receipt']
        receipt = ao.read(receipt_path)
        original = receipt_path.read_bytes()
        receipt['tampered'] = True
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(RoomError, 'tampered_receipt'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        receipt_path.write_bytes(original)
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.assertEqual(record['assignment_id'], 'A1')

    def test_tampered_handback_blocks_preflight(self):
        record = self.handed_back()
        path = self.assignments_root() / 'A1' / 'handbacks' / (record['handback_sha256'] + '.json')
        saved = ao.read(path)
        saved['commit'] = '0' * 40
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(RoomError, 'tampered_handback'):
            self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])

    def test_mismatched_assignment_and_room_refuse(self):
        self.assignment_accepted()
        with self.assertRaisesRegex(RoomError, 'mismatched_assignment_id'):
            self.service.ao_room_handback_record(self.room, 'A1', 'acceptance_review')
        with self.assertRaisesRegex(RoomError, 'coordination assignment'):
            self.service.ao_room_handback_record(self.assign_room, 'ZZ', 'acceptance_review')


class RevisionTests(CoordinationFixture):
    def test_owner_spec_change_blocks_preflight(self):
        record = self.handed_back()
        self.service.ao_room_spec_put(self.room, 2, 'Revised owner content.',
                                      self.gates, 'Astra approves this revision')
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertFalse(preflight['ready'])
        self.assertIn('owner_spec_changed', preflight['blockers'])

    def test_assignment_spec_content_change_refuses_handback(self):
        self.assignment_accepted()
        self.service.ao_room_spec_put(self.assign_room, 2, 'Revised assignment content.',
                                      self.gates_b, 'Astra approves this revision')
        with self.assertRaisesRegex(RoomError, 'assignment_spec_changed'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def test_gates_only_revision_remains_allowed(self):
        self.open_rooms()
        # The same spec content under a different gate list keeps the lineage binding.
        gates = self.gates_b + [[sys.executable, '-c', 'pass']]
        self.service.ao_room_spec_put(self.assign_room, 2, 'Implement the bounded assignment.',
                                      gates, 'Astra approves this revision')
        self.agree_b(revision=2)
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        self.review_b()
        self.accept_b()
        record = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.assertEqual(record['spec']['revision'], 2)

    def test_bad_baseline_and_scope_drift_refuse(self):
        self.open_rooms()
        self.agree_b()
        # An orphan HEAD in the assignment worktree does not descend from the lineage baseline.
        self.git(self.assign_wt, 'checkout', '--orphan', 'unrelated')
        (self.assign_wt / self.OWNED).write_text('done\n')
        self.commit_wt(self.assign_wt, 'orphan root')
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.send_b('implementation')
        self.fake.finish('engineer-b', json.dumps(self.report_b()))
        self.service.ao_room_sync(self.assign_room)
        self.review_b()
        with self.assertRaisesRegex(RoomError, 'baseline_mismatch'):
            self.accept_b()
        with self.assertRaisesRegex(RoomError, 'refused'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')


class InterruptionTests(CoordinationFixture):
    def test_repeated_records_are_idempotent(self):
        recorded = self.handed_back()
        first = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.assertEqual(first, recorded)
        self.assertEqual(len(list((self.assignments_root() / 'A1' / 'handbacks').glob('*.json'))), 1)
        lineage_dir = self.assignments_root() / 'A1'
        self.assertEqual(first['handback_sha256'], self.lifecycle()['latest_handback']['sha256'])
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', first['handback_sha256'])
        again = self.service.ao_room_import_preflight(self.room, 'A1', first['handback_sha256'])
        self.assertEqual(again['preflight_sha256'], preflight['preflight_sha256'])
        self.assertEqual(len(list((lineage_dir / 'import-preflights').glob('*.json'))), 1)
        self.git(self.repo, 'cherry-pick', '-x', first['commit'])
        imported = self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'],
                                                    'git cherry-pick -x ' + first['commit'])
        repeat = self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'],
                                                  'git cherry-pick -x ' + first['commit'])
        self.assertEqual(repeat['import_sha256'], imported['import_sha256'])
        self.assertEqual(len(list((lineage_dir / 'imports').glob('*.json'))), 1)
        with self.assertRaisesRegex(RoomError, 'immutable_import'):
            self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'], 'git merge other')

    def test_interrupted_handback_retry_completes_pointers(self):
        self.assignment_accepted()
        real = ao.atomic

        def flaky(path, value):
            if Path(path).name == 'state.json' and 'coordination' in str(path):
                raise OSError('simulated crash after the immutable write')
            return real(path, value)

        with patch.object(ao, 'atomic', side_effect=flaky):
            with self.assertRaises(OSError):
                self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        files = list((self.assignments_root() / 'A1' / 'handbacks').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.lifecycle()['handbacks'], [])
        result = self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')
        self.assertEqual(result['handback_sha256'], files[0].stem)
        self.assertEqual(self.lifecycle()['latest_handback']['sha256'], files[0].stem)
        self.assertEqual(len(list((self.assignments_root() / 'A1' / 'handbacks').glob('*.json'))), 1)
        with self.assertRaisesRegex(RoomError, 'missing_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'different-request')

    def test_lineage_immutability(self):
        self.open_rooms()
        again = self.service.ao_room_assignment_open(
            self.room, 'A1', self.assign_room, ['owned'],
            'The interface contract for the owned paths.', 'User authorized this bounded assignment')
        self.assertTrue(again['opened'])
        with self.assertRaisesRegex(RoomError, 'immutable'):
            self.service.ao_room_assignment_open(
                self.room, 'A1', self.assign_room, ['other'], 'interface', 'authorization')


class ImportConflictTests(CoordinationFixture):
    def test_owner_conflict_requires_recorded_resolution(self):
        record = self.handed_back()
        # The owner modifies the same owned path the assignment changed.
        (self.repo / 'owned' / 'part.txt').write_text('owner version\n')
        self.commit_wt(self.repo, 'owner touch')
        preflight = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertFalse(preflight['ready'])
        self.assertEqual(preflight['preview'], 'conflicts')
        self.assertEqual(preflight['blockers'], [])
        self.assertIn('owned/part.txt', preflight['owner_touched_assigned_paths'])
        self.assertIn('owned/part.txt', preflight['conflicts'])
        # An import record requires an owner HEAD that descends from the preflight head.
        with self.assertRaisesRegex(RoomError, 'import_not_performed'):
            self.service.ao_room_import_record(self.room, 'A1', preflight['preflight_sha256'], 'git merge')
        # The operator writes the merged content and commits; the destination differs
        # from the handback bytes, so the record requires a nonempty resolution.
        (self.repo / 'owned' / 'part.txt').write_text('merged content\n')
        self.commit_wt(self.repo, 'resolve owned/part.txt')
        with self.assertRaisesRegex(RoomError, 'integration_modifications_unresolved'):
            self.service.ao_room_import_record(
                self.room, 'A1', preflight['preflight_sha256'], 'manual merge then record')
        imported = self.service.ao_room_import_record(
            self.room, 'A1', preflight['preflight_sha256'], 'manual merge then record',
            resolution='Merged owner and assignment bytes in owned/part.txt by hand.')
        self.assertEqual(imported['kind'], 'resolved_with_modifications')
        self.assertEqual(imported['modified_paths'], ['owned/part.txt'])
        self.assertEqual(imported['resolution'],
                         'Merged owner and assignment bytes in owned/part.txt by hand.')

    def test_owner_head_advance_reports_stale_preflight(self):
        record = self.handed_back()
        clean = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertTrue(clean['ready'])
        # The owner may advance its own unowned scope; a new preflight stays ready.
        (self.repo / 'feature.txt').write_text('advanced\n')
        self.commit_wt(self.repo, 'owner advances unowned path')
        advanced = self.git(self.repo, 'rev-parse', 'HEAD')
        later = self.service.ao_room_import_preflight(self.room, 'A1', record['handback_sha256'])
        self.assertTrue(later['ready'])
        self.assertEqual(later['owner_touched_assigned_paths'], [])
        self.assertEqual(later['owner_head_before'], advanced)
        # Only the latest recorded preflight may be imported.
        with self.assertRaisesRegex(RoomError, 'stale_preflight'):
            self.service.ao_room_import_record(self.room, 'A1', clean['preflight_sha256'], 'git merge')
        # An owner HEAD that does not descend from the preflight head is stale too.
        self.git(self.repo, 'reset', '--hard', 'HEAD~1')
        with self.assertRaisesRegex(RoomError, 'preflight_stale'):
            self.service.ao_room_import_record(self.room, 'A1', later['preflight_sha256'], 'git merge')
        self.git(self.repo, 'reset', '--hard', advanced)
        self.git(self.repo, 'cherry-pick', '-x', record['commit'])
        imported = self.service.ao_room_import_record(self.room, 'A1', later['preflight_sha256'],
                                                      'git cherry-pick -x ' + record['commit'])
        self.assertEqual(imported['kind'], 'faithful')


class OrdinaryRoomTests(CoordinationFixture):
    def test_ordinary_room_is_unchanged(self):
        self.room = self.open('normal')
        self.spec()
        self.bind()
        self.agree()
        self.implement()
        self.review()
        result = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(result['accepted'])
        self.assertEqual(self.service.ao_room_status(self.room)['coordination'],
                         {'role': None, 'assignments': []})
        self.assertNotIn('coordination', self.state())
        compact = self.service.ao_room_status(self.room, 'compact')['coordination']
        self.assertEqual(compact, {'role': None, 'assignments': []})
        progress = self.service.ao_room_progress(self.room)['coordination']
        self.assertEqual(progress, {'role': None, 'assignments': []})
        rooms = {item['room_id']: item for item in self.service.ao_room_list()['rooms']}
        self.assertEqual(rooms[self.room]['coordination'], {'role': None})


class GateTests(CoordinationFixture):
    def test_mutating_failing_gate_blocks_handback(self):
        mutating = [[sys.executable, '-c',
                     "from pathlib import Path; Path('owned/part.txt').write_text('mutated\\n')"]]
        self.open_rooms()
        self.service.ao_room_spec_put(self.assign_room, 2, 'Implement the bounded assignment.',
                                      mutating, 'Astra approves this revision')
        self.agree_b(revision=2)
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))
        self.implement_b()
        result = self.service.ao_room_verify(self.assign_room, str(self.assign_wt))
        self.assertFalse(result['passed'])
        with self.assertRaises(RoomError):
            self.accept_b()
        with self.assertRaisesRegex(RoomError, 'missing_checkpoint'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')

    def test_scope_drift_refuses_at_accept_and_handback(self):
        self.open_rooms()
        self.agree_b()
        self.service.ao_room_handoff(self.assign_room, str(self.assign_wt))

        def change():
            (self.assign_wt / self.OWNED).write_text('done\n')
            (self.assign_wt / 'feature.txt').write_text('out of scope\n')

        self.implement_b(change)
        self.review_b()
        with self.assertRaisesRegex(RoomError, 'scope_drift'):
            self.accept_b()
        with self.assertRaisesRegex(RoomError, 'missing_acceptance'):
            self.service.ao_room_handback_record(self.assign_room, 'A1', 'acceptance_review')


if __name__ == '__main__':
    unittest.main()
