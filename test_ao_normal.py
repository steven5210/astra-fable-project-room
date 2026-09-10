"""Offline normal-role contracts and private MCP attachment; no account/model calls."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ao_project_room as ao
import ao_delegates
import ao_delegate_launcher as launcher
import ao_workflow
import deepseek_adapter
import project_room
from test_ao_project_room import FakeAO


class NativeFake(FakeAO):
    def __init__(self, path):
        super().__init__(path)
        self.sessions['engineer']['harness'] = 'claude-code'
        self.snapshots['engineer']['settings']['model'] = ao_workflow.FABLE_MODEL
        self.workspaces = {'engineer': path, 'reviewer': path}

    def request(self, method, path, payload=None):
        if path.startswith('/desktop/sessions/'):
            sid = path.split('/')[3]
            return {'sessionId': sid, 'workspacePath': str(self.workspaces[sid])}
        return super().request(method, path, payload)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        for args in (('init',), ('config', 'user.name', 'Test'), ('config', 'user.email', 'fixture@example.invalid')):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
        (self.repo / 'feature.txt').write_text('start\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-m', 'fixture'], check=True, capture_output=True)
        self.home = self.root / 'state'
        self.fake = NativeFake(self.repo)
        self.service = ao.Service(self.home, lambda url: self.fake)
        self.gates = [[sys.executable, '-c', "from pathlib import Path; assert Path('feature.txt').read_text() == 'implemented\\n'"]]

    def open(self, feature='normal', provider='none'):
        return self.service.ao_room_open(str(self.repo), feature, 'project', 'User authorized this feature',
                                         'http://127.0.0.1:1234', delegate_provider=provider)['room_id']

    def directory(self, room=None):
        return self.home / 'ao' / 'rooms' / (room or self.room)

    def state(self):
        return ao.read(self.directory() / 'state.json')

    def spec(self, revision=1):
        return self.service.ao_room_spec_put(self.room, revision, 'Implement the exact test contract.', self.gates, 'Astra approves this scope')

    def bind(self):
        self.service.ao_room_prepare(self.room, str(self.repo))
        self.service.ao_room_bind(self.room, 'engineer', 'engineer', ao_workflow.FABLE_MODEL, 'max')
        self.service.ao_room_bind(self.room, 'reviewer', 'reviewer', 'astra', 'max')

    def send(self, purpose, key=None):
        role = 'reviewer' if purpose == 'acceptance_review' else 'engineer'
        return self.service.ao_room_send(self.room, role, 'Perform the exact authorized purpose.', key or purpose, purpose=purpose)

    def agree(self, decision='accept', revision=1, key='spec_review'):
        self.send('spec_review', key)
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish('engineer', json.dumps({'interpretation': 'Implement the supplied pure behavior.', 'findings': [],
                         'decision': decision, 'spec_revision': revision, 'spec_sha256': spec['sha256']}))
        return self.service.ao_room_sync(self.room)

    def report(self, **changes):
        handoff = ao_workflow.handoff_record(self.directory(), self.state())
        return {'outcome': 'completed', 'implementation_complete': True, 'changes': ['Implemented feature'],
                'tests_reported': ['Focused tests pass'], 'review_findings': [], 'remaining_gaps': [],
                'backlog': [], 'routing_log': [], 'spec_revision': handoff['spec_revision'],
                'spec_sha256': handoff['spec_sha256'], 'baseline_commit': handoff['baseline_commit'], **changes}

    def implement(self, **changes):
        self.service.ao_room_handoff(self.room, str(self.repo))
        self.send('implementation')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report(**changes)))
        return self.service.ao_room_sync(self.room)

    def review(self):
        self.service.ao_room_verify(self.room, str(self.repo))
        self.send('acceptance_review')
        request = self.state()['requests']['acceptance_review']
        self.fake.finish('reviewer', json.dumps({**request['review'], 'decision': 'approved', 'review': 'Independently inspected exact behavior and gates.'}))
        return self.service.ao_room_sync(self.room)


class NormalWorkflowTests(Fixture):
    def setUp(self):
        super().setUp()
        self.room = self.open()
        self.spec()

    def test_default_roles_and_missing_provider_fail_closed(self):
        self.assertEqual(self.state()['workflow'], 'fable_engineering')
        with self.assertRaisesRegex(ao.RoomError, 'missing selection'):
            self.service.ao_room_open(str(self.repo), 'missing', 'project', 'Authorized', 'http://127.0.0.1:1234')
        ao.atomic(self.home / 'ao' / 'config.json', {'engineering_preference': 'astra'})
        with self.assertRaisesRegex(ao.RoomError, 'default'):
            self.open('another')
        self.assertEqual(self.service.ao_room_open(str(self.repo), 'normal', 'project', 'Existing authorization', 'http://127.0.0.1:1234')['room_id'], self.room)

    def test_explicit_astra_exception_and_historical_room_readability(self):
        with self.assertRaisesRegex(ao.RoomError, 'exception_authorization'):
            self.service.ao_room_open(str(self.repo), 'exception', 'project', 'Build', 'http://127.0.0.1:1234', workflow='astra_led')
        args = (str(self.repo), 'exception', 'project', 'Build', 'http://127.0.0.1:1234')
        kwargs = dict(workflow='astra_led', exception_authorization='Actual user one-off choice', delegate_provider='none')
        value = self.service.ao_room_open(*args, **kwargs)
        self.assertEqual(self.service.ao_room_open(*args, **kwargs)['room_id'], value['room_id'])
        directory = Path(value['room_path']); state = ao.read(directory / 'state.json')
        state['version'] = 1; state.pop('exception_authorization')
        ao.atomic(directory / 'state.json', state); before = (directory / 'state.json').read_bytes()
        self.assertEqual(self.service.ao_room_open(str(self.repo), 'exception', 'project', 'Read existing', 'http://127.0.0.1:1234')['workflow'], 'astra_led')
        self.assertEqual((directory / 'state.json').read_bytes(), before)

    def test_role_identity_and_preparation_required(self):
        for role, sid, model, effort in [('engineer', 'reviewer', 'astra', 'max'), ('reviewer', 'engineer', ao_workflow.FABLE_MODEL, 'max'), ('engineer', 'engineer', 'opus', 'max'), ('engineer', 'engineer', ao_workflow.FABLE_MODEL, 'high')]:
            with self.subTest(role=role, model=model), self.assertRaisesRegex(ao.RoomError, 'Normal roles'):
                self.service.ao_room_bind(self.room, role, sid, model, effort)
        with self.assertRaisesRegex(ao.RoomError, 'Prepare'):
            self.service.ao_room_bind(self.room, 'engineer', 'engineer', ao_workflow.FABLE_MODEL, 'max')
        self.bind()
        self.assertEqual(self.state()['bindings']['engineer']['fable_reason'], 'Designated Fable engineering role')

    def test_rejected_stale_and_fabricated_agreement_cannot_implement(self):
        self.bind()
        for action in (lambda: self.send('implementation'), lambda: self.service.ao_room_handoff(self.room, str(self.repo))):
            with self.assertRaises(ao.RoomError): action()
        self.agree(decision='changes_required')
        with self.assertRaisesRegex(ao.RoomError, 'rejected'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.agree(key='review2')
        self.spec(2)
        self.assertFalse(self.service.ao_room_status(self.room)['agreement']['agreed'])
        with self.assertRaisesRegex(ao.RoomError, 'current exact spec'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.agree(revision=1, key='stale-review')
        with self.assertRaises(ao.RoomError): self.service.ao_room_handoff(self.room, str(self.repo))
        with self.assertRaisesRegex(ao.RoomError, 'Three Fable'):
            self.send('spec_review', 'fourth')
        self.assertEqual(len(self.fake.posts), 3)

    def test_full_flow_records_exact_engineering_and_independent_acceptance(self):
        self.bind(); status = self.agree()
        self.assertTrue(status['agreement']['agreed'])
        self.implement(); self.review()
        result = self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertTrue(result['accepted']); self.assertEqual(result['workflow'], 'fable_engineering')
        packet = self.state()['requests']['implementation']['text']
        self.assertIn('Fable owns implementation', packet)
        self.assertNotIn('No routine Fable', packet)
        self.assertIn('Fable is the implementation orchestrator', packet)
        self.assertEqual(self.service.ao_room_status(self.room)['delegate']['provider'], 'none')
        self.assertFalse(self.service.ao_room_status(self.room)['usage']['includes_delegates'])

    def test_lost_ack_duplicates_do_not_resend(self):
        self.bind(); self.fake.lose_ack = True
        first = self.send('spec_review')
        self.assertEqual(first['state'], 'uncertain'); self.assertEqual(self.send('spec_review'), first)
        with self.assertRaisesRegex(ao.RoomError, 'active or uncertain'):
            self.send('spec_review', 'another')
        spec = self.service.spec(self.directory(), self.state())
        self.fake.finish('engineer', json.dumps({'interpretation': 'Exact scope', 'findings': [], 'decision': 'accept', 'spec_revision': 1, 'spec_sha256': spec['sha256']}))
        self.service.ao_room_sync(self.room)
        self.assertTrue(self.service.ao_room_status(self.room)['agreement']['agreed'])
        self.assertEqual(len(self.fake.posts), 1)

    def test_changed_candidate_and_incomplete_reports_cannot_be_accepted(self):
        self.bind(); self.agree(); self.implement(remaining_gaps=['unfinished'])
        with self.assertRaisesRegex(ao.RoomError, 'remaining gaps'):
            self.service.ao_room_verify(self.room, str(self.repo))
        self.send('correction')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        (self.repo / 'feature.txt').write_text('tampered\n')
        with self.assertRaisesRegex(ao.RoomError, 'changed since Fable'):
            self.service.ao_room_verify(self.room, str(self.repo))

    def test_malformed_engineering_report_blocks_but_known_completion_can_be_corrected(self):
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.fake.finish('engineer', 'Done')
        self.service.ao_room_sync(self.room)
        self.assertIn('engineering_error', self.state()['requests']['implementation'])
        with self.assertRaisesRegex(ao.RoomError, 'captured completed'):
            self.service.ao_room_verify(self.room, str(self.repo))
        # The completed native turn can receive a report correction, never a replay.
        self.send('correction')
        (self.repo / 'feature.txt').write_text('implemented\n')
        self.fake.finish('engineer', json.dumps(self.report()))
        self.service.ao_room_sync(self.room)
        self.review()
        self.assertTrue(self.service.ao_room_accept(self.room, 'acceptance_review')['accepted'])
        self.assertEqual(len(self.fake.posts), 4)

    def test_worktree_identity_and_wrong_candidate_refuse(self):
        self.bind(); self.agree()
        other = self.root / 'other'
        subprocess.run(['git', '-C', str(self.repo), 'worktree', 'add', '-b', 'other', str(other)], check=True, capture_output=True)
        with self.assertRaisesRegex(ao.RoomError, 'actual bound'):
            self.service.ao_room_handoff(self.room, str(other))
        self.fake.workspaces['engineer'] = other
        with self.assertRaisesRegex(ao.RoomError, 'differs'):
            self.service.ao_room_handoff(self.room, str(self.repo))

    def test_missing_final_can_be_corrected_but_modified_receipt_cannot(self):
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.fake.finish('engineer', '')
        self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertIn('no final response', request['engineering_error'])
        receipt_path = self.directory() / request['receipt']
        original = receipt_path.read_bytes()
        receipt_path.write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'modified'): self.send('correction')
        receipt_path.write_bytes(original)
        self.send('correction')
        self.assertEqual(len(self.fake.posts), 3)

    def test_failed_first_candidate_capture_is_durable_until_correction(self):
        self.bind(); self.agree(); self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.fake.finish('engineer', json.dumps(self.report()))
        with patch.object(ao_workflow, 'candidate_snapshot', side_effect=ao_workflow.ImplementationError('snapshot failed')):
            self.service.ao_room_sync(self.room)
        request = self.state()['requests']['implementation']
        self.assertEqual(request['state'], 'completed')
        self.assertEqual(request['engineering_error'], 'snapshot failed')
        self.assertTrue((self.directory() / request['receipt']).is_file())
        with patch.object(ao_workflow, 'candidate_snapshot', side_effect=AssertionError('must not recapture')):
            self.service.ao_room_sync(self.room)
        self.assertNotIn('engineering_record', self.state()['requests']['implementation'])
        self.send('correction')

    def test_late_engineer_reroute_blocks_acceptance(self):
        self.bind(); self.agree(); self.implement(); self.review()
        snapshot = self.fake.snapshots['engineer']
        snapshot['modelReroute'] = {'fromModel': ao_workflow.FABLE_MODEL, 'toModel': 'opus',
                                  'providerTurnId': snapshot['turns'][-1]['providerTurnId']}
        with self.assertRaisesRegex(ao.RoomError, 'substitution'):
            self.service.ao_room_accept(self.room, 'acceptance_review')
        self.assertIn('model_reroute', self.state()['requests']['implementation'])

    def test_implementation_is_single_purpose_and_unknown_completion_blocks(self):
        self.bind(); self.agree()
        with self.assertRaisesRegex(ao.RoomError, 'purpose'):
            self.service.ao_room_send(self.room, 'engineer', 'Do work', 'ambiguous')
        self.service.ao_room_handoff(self.room, str(self.repo)); self.send('implementation')
        self.fake.finish('engineer', state='failed')
        self.service.ao_room_sync(self.room)
        with self.assertRaises(ao.RoomError): self.send('correction')
        self.assertEqual(len(self.fake.posts), 2)

    def test_late_engineer_reroute_blocks_handoff_and_retains_evidence(self):
        self.bind(); self.agree()
        turn = self.fake.snapshots['engineer']['turns'][-1]
        self.fake.snapshots['engineer']['modelReroute'] = {'fromModel': ao_workflow.FABLE_MODEL, 'toModel': 'opus', 'providerTurnId': turn['providerTurnId']}
        with self.assertRaisesRegex(ao.RoomError, 'substitution'):
            self.service.ao_room_handoff(self.room, str(self.repo))
        self.assertIn('model_reroute', self.state()['requests']['spec_review'])
        self.fake.snapshots['engineer'].pop('modelReroute')
        with self.assertRaisesRegex(ao.RoomError, 'substitution'): self.send('implementation')

    def test_reroute_changing_current_settings_is_saved_before_refusal(self):
        self.bind(); self.send('spec_review')
        snapshot = self.fake.snapshots['engineer']; turn = snapshot['turns'][-1]
        snapshot['settings']['model'] = 'opus'
        snapshot['modelReroute'] = {'fromModel': ao_workflow.FABLE_MODEL, 'toModel': 'opus', 'providerTurnId': turn['providerTurnId']}
        with self.assertRaisesRegex(ao.RoomError, 'substitution'): self.service.ao_room_sync(self.room)
        self.assertIn('model_reroute', self.state()['requests']['spec_review'])

    def test_unrelated_or_ambiguous_reroute_is_not_reattributed(self):
        self.bind(); self.send('spec_review')
        snapshot = self.fake.snapshots['engineer']; request = self.state()['requests']['spec_review']
        snapshot['modelReroute'] = {'fromModel': ao_workflow.FABLE_MODEL, 'toModel': 'opus', 'providerTurnId': 'historical'}
        self.assertIsNone(ao.conflicting_reroute(request, snapshot))
        snapshot['modelReroute']['providerTurnId'] = snapshot['turns'][-1]['providerTurnId']
        self.assertIsNotNone(ao.conflicting_reroute(request, snapshot))
        snapshot['messages'].append(copy.deepcopy(snapshot['messages'][0]))
        self.assertIsNone(ao.conflicting_reroute(request, snapshot))


class DelegatePreparationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.claude_config = self.root / 'claude-config'; self.claude_config.mkdir()
        self.fake_cli = self.root / 'fake-claude'
        self.fake_cli.write_text('#!' + sys.executable + '\n' + '''import json,os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
assert args[:3]==['mcp','add-json','deepseek'] and args[-2:]==['--scope','local']
p=Path(os.environ['CLAUDE_CONFIG_DIR'])/'.claude.json'
project=subprocess.check_output(['git','worktree','list','--porcelain'],text=True).splitlines()[0][9:]
value=json.loads(p.read_text()) if p.exists() else {}
value.setdefault('projects',{}).setdefault(project,{}).setdefault('mcpServers',{})['deepseek']=json.loads(args[3])
p.write_text(json.dumps(value))
''')
        self.fake_cli.chmod(0o700)
        provider = self.root / 'provider.json'
        provider.write_text(json.dumps({'api_key_file': str(self.home / 'secrets' / 'synthetic-key')}))
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude_config)}):
            project_room.Service(self.home).setup(claude_bin=str(self.fake_cli), deepseek_config=str(provider), delegate_provider='deepseek')
        self.room = self.open(provider='deepseek'); self.spec()

    def prepare(self, path=None):
        return self.service.ao_room_prepare(self.room, str(path or self.repo))

    def test_private_setup_is_idempotent_and_does_not_touch_candidate(self):
        before = ao.candidate_snapshot(self.repo)
        first = self.prepare(); self.assertEqual(first, self.prepare())
        self.assertEqual(before, ao.candidate_snapshot(self.repo))
        self.assertFalse((self.repo / '.mcp.json').exists())
        self.assertEqual(self.service.ao_room_status(self.room)['delegate']['attachment'], 'configuration_verified')
        self.assertEqual(self.state()['delegate']['inventory']['room_id'], self.room)
        with patch.object(Path, 'read_text', side_effect=AssertionError('No plaintext reads')):
            directory, prepared, server = launcher.select(self.home, self.repo, 'engineer', first['launcher_path'])
        self.assertEqual(server['env'], {'PROJECT_ROOM_WORKTREE': str(self.repo)})
        launcher.record_launch(directory, prepared, 'engineer')
        with self.assertRaisesRegex(ValueError, 'another AO session'):
            launcher.record_launch(directory, prepared, 'other')

    def test_two_worktrees_share_registration_but_select_distinct_pinned_rooms(self):
        one = self.prepare()
        other = self.root / 'worktree-two'
        subprocess.run(['git','-C',str(self.repo),'worktree','add','-b','second',str(other)],check=True,capture_output=True)
        room2 = self.open(feature='second', provider='deepseek')
        two = self.service.ao_room_prepare(room2, str(other))
        self.assertEqual(one['registration'], two['registration'])
        self.assertEqual(one['registry_project'], two['registry_project'])
        d1, _, _ = launcher.select(self.home, self.repo, 'worker1', one['launcher_path'])
        d2, _, _ = launcher.select(self.home, other, 'worker2', two['launcher_path'])
        self.assertEqual(d1.name, self.room); self.assertEqual(d2.name, room2)
        with self.assertRaisesRegex(ao.RoomError, 'immutable'):
            self.service.ao_room_prepare(self.room, str(other))

    def test_conflicting_registry_and_changed_snapshot_refuse(self):
        registry = self.claude_config / '.claude.json'
        ao.atomic(registry, {'projects':{str(self.repo):{'mcpServers':{'deepseek':{'command':'another'}}}}})
        with self.assertRaisesRegex(ao.RoomError, 'already exists'): self.prepare()
        registry.unlink(); prepared = self.prepare()
        (self.directory() / 'profiles' / 'deepseek.json').write_text('{}')
        with self.assertRaisesRegex(ao.RoomError, 'inventory changed'): self.prepare()
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            launcher.select(self.home, self.repo, 'engineer', prepared['launcher_path'])

    def test_actual_launch_and_dispatch_refuse_same_path_wrong_repository(self):
        prepared = self.prepare()
        shutil.move(str(self.repo / '.git'), str(self.root / 'original-git'))
        subprocess.run(['git', '-C', str(self.repo), 'init', '--separate-git-dir', str(self.root / 'different-git')], check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, 'repository changed'):
            launcher.select(self.home, self.repo, 'engineer', prepared['launcher_path'])
        with self.assertRaisesRegex(ao.RoomError, 'repository'): self.prepare()

    def test_native_session_binding_and_registry_drift_refuse(self):
        prepared = self.prepare()
        directory, launch, _ = launcher.select(self.home, self.repo, 'wrong-session', prepared['launcher_path'])
        launcher.record_launch(directory, launch, 'wrong-session')
        with self.assertRaisesRegex(ao.RoomError, 'contradicts'):
            self.service.ao_room_bind(self.room,'engineer','engineer',ao_workflow.FABLE_MODEL,'max')
        registry = self.claude_config / '.claude.json'
        ao.atomic(registry, {'projects': {}})
        self.assertEqual(self.service.ao_room_status(self.room)['delegate']['attachment'], 'unverified')
        with self.assertRaisesRegex(ao.RoomError, 'attachment'): self.prepare()

    def test_missing_ambiguous_and_modified_launcher_refuse(self):
        prepared = self.prepare()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            launcher.select(self.home, self.root, 'engineer', prepared['launcher_path'])
        shadow = self.directory().parent / 'shadow'; shadow.mkdir()
        shutil.copyfile(self.directory()/'state.json',shadow/'state.json')
        shutil.copyfile(self.directory()/'preparation.json',shadow/'preparation.json')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            launcher.select(self.home,self.repo,'engineer',prepared['launcher_path'])
        shutil.rmtree(shadow)
        Path(prepared['launcher_path']).write_text('# drift')
        with self.assertRaisesRegex(ao.RoomError, 'launcher changed'): self.prepare()

    def test_entire_ledger_blocks_even_when_stop_is_outside_latest_twenty(self):
        self.prepare()
        ledger = deepseek_adapter.Ledger(self.home)
        with ledger.transaction() as db:
            for index in range(22):
                db.execute('INSERT INTO jobs(id,room_id,request_id,lane,payload_sha256,profile_sha256,state,created_at,requested_model,thinking,reasoning_effort,max_tokens,input_bytes,reserved_bytes,possibly_billed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (f'{index:032x}',self.room,f'r{index}','deep','p'*64,'q'*64,'unknown_delivery' if index==0 else 'completed',f'2026-09-09T00:00:{index:02d}+00:00',deepseek_adapter.DEFAULT_MODEL,'enabled','max',393216,100,0,1))
        status = self.service.ao_room_status(self.room)['delegate']['jobs']
        self.assertTrue(status['truncated']); self.assertEqual(len(status['items']),20)
        self.assertNotIn('0'*32,[x['job_id'] for x in status['items']])
        with self.assertRaisesRegex(ao.RoomError,'unresolved delegate'):
            ao_delegates.assert_settled(self.home,self.state())
        self.assertFalse(self.service.ao_room_status(self.room)['usage']['includes_delegates'])


if __name__ == '__main__':
    unittest.main()
